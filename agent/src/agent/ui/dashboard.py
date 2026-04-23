"""Local aiohttp dashboard for recorder control (no terminal beyond one launch)."""
from __future__ import annotations

import asyncio
import json
import shutil
import signal
import sys
import webbrowser
from pathlib import Path
from typing import Any

from aiohttp import web

from agent.core.ids import generate_run_id
from agent.recorder.recorder import RecorderArtifact, StepGraphRecorder
from agent.stepgraph.models import StepGraph
from agent.storage.files import get_run_layout, resolve_runs_root
from agent.ui.replay_interactive import (
    FORCE_FIX_UI_CAVEAT,
    InteractiveReplaySession,
    build_step_from_interactive_insert_body,
)

_DASHBOARD_HTML = (Path(__file__).resolve().parent / "static" / "dashboard.html").read_text(encoding="utf-8")


def _step_summary(step: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"stepId": step.step_id, "action": step.action}
    if step.target is not None:
        out["primarySelector"] = step.target.primary_selector
        out["hasLocatorTarget"] = True
    md = step.metadata if isinstance(step.metadata, dict) else {}
    detail: str | None = None
    if step.action == "navigate" and isinstance(md.get("url"), str):
        detail = md["url"].strip()[:120] or None
    elif step.action == "assert_url" and isinstance(md.get("expected"), str):
        detail = md["expected"].strip()[:120] or None
    elif step.action in ("assert_title", "assert_text") and isinstance(md.get("expected"), str):
        detail = md["expected"].strip()[:80] or None
    elif step.action == "wait_timeout":
        t = md.get("timeoutMs") or md.get("timeout_ms")
        if t is not None:
            detail = f"{t} ms"
    elif step.action == "dialog_handle":
        detail = "accept" if md.get("accept", True) else "dismiss"
    elif step.action == "upload":
        fp = md.get("filePaths") or md.get("file_paths")
        preview: str | None = None
        if isinstance(fp, str) and fp.strip():
            preview = fp.strip()[:240]
        elif isinstance(fp, list) and fp:
            joined = ", ".join(str(p).strip() for p in fp if isinstance(p, str) and p.strip())
            preview = joined[:240] if joined else None
        if preview:
            out["filePathsPreview"] = preview
            detail = preview[:100] + ("…" if len(preview) > 100 else "")
        elif md.get("filePaths") is not None or md.get("file_paths") is not None:
            detail = "filePaths set but empty"
        else:
            detail = "no filePaths in step"
    if detail:
        out["detail"] = detail
    return out


def _agent_cwd() -> Path:
    """Directory that contains ``pyproject.toml`` (``uv run`` / ``-m agent.cli`` cwd)."""
    return Path(__file__).resolve().parents[3]


def _list_saved_runs() -> list[dict[str, Any]]:
    root = resolve_runs_root()
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for p in root.iterdir():
        if not p.is_dir() or p.name.startswith("."):
            continue
        sg = p / "stepgraph.json"
        if not sg.is_file():
            continue
        mf = p / "manifest.json"
        rows.append(
            {
                "runId": p.name,
                "stepgraphPath": str(sg.resolve()),
                "manifestPath": str(mf.resolve()) if mf.is_file() else None,
                "modifiedAt": int(sg.stat().st_mtime),
            }
        )
    rows.sort(key=lambda r: r["modifiedAt"], reverse=True)
    return rows


class RecorderDashboardApp:
    def __init__(self) -> None:
        self._recorder: StepGraphRecorder | None = None
        self._lock = asyncio.Lock()
        self._shutdown = asyncio.Event()
        self._replay_running = False
        self._replay_proc: asyncio.subprocess.Process | None = None
        self._replay_log_path: Path | None = None
        self._replay_last_exit: int | None = None
        self._replay_task: asyncio.Task[None] | None = None
        self._interactive: InteractiveReplaySession | None = None
        #: True while :meth:`InteractiveReplaySession.start` runs outside the dashboard lock (browser boot).
        self._interactive_start_in_progress: bool = False

    async def handle_index(self, _request: web.Request) -> web.StreamResponse:
        return web.Response(text=_DASHBOARD_HTML, content_type="text/html")

    async def handle_state(self, _request: web.Request) -> web.Response:
        async with self._lock:
            rec = self._recorder
            if rec is None or rec.step_graph is None:
                return web.json_response(
                    {
                        "active": False,
                        "armed": False,
                        "step_count": 0,
                        "steps": [],
                        "run_id": None,
                        "stepgraphPath": None,
                        "manifestPath": None,
                        "pickPending": False,
                        "pickIntent": None,
                    }
                )
            g = rec.step_graph
            layout = get_run_layout(rec.run_id)
            pick = await rec.read_pick_state()
            return web.json_response(
                {
                    "active": True,
                    "armed": rec.recording_armed,
                    "step_count": len(g.steps),
                    "steps": [_step_summary(s) for s in g.steps],
                    "run_id": rec.run_id,
                    "stepgraphPath": str(layout.run_dir / "stepgraph.json"),
                    "manifestPath": str(layout.manifest_json),
                    "pickPending": pick.get("pickPending", False),
                    "pickIntent": pick.get("pickIntent"),
                }
            )

    async def handle_session_start(self, request: web.Request) -> web.Response:
        body = await request.json()
        url = str(body.get("url") or "").strip()
        if not url:
            return web.json_response({"error": "url required"}, status=400)
        storage = body.get("storage_state")
        storage_path = str(storage).strip() if storage else None
        headless = bool(body.get("headless", False))
        record_armed_start = bool(body.get("record_armed_start", False))
        raw_upload = body.get("record_upload_path") or body.get("recordUploadPath")
        record_upload_path = str(raw_upload).strip() if isinstance(raw_upload, str) else None
        if record_upload_path == "":
            record_upload_path = None

        async with self._lock:
            if self._recorder is not None:
                return web.json_response({"error": "session already active"}, status=409)
            if self._replay_running:
                return web.json_response({"error": "stop subprocess replay first"}, status=409)
            if self._interactive is not None and self._interactive.active:
                return web.json_response({"error": "stop interactive replay first"}, status=409)
            if self._interactive_start_in_progress:
                return web.json_response({"error": "wait for interactive replay to finish starting"}, status=409)
            self._recorder = StepGraphRecorder(
                url=url,
                headless=headless,
                storage_state=storage_path,
                browser_ui=False,
                dashboard_control=True,
                recording_armed_start=record_armed_start,
                default_record_upload_path=record_upload_path,
            )
            await self._recorder.start()
            rid = self._recorder.run_id

        return web.json_response({"ok": True, "run_id": rid})

    async def handle_session_stop(self, _request: web.Request) -> web.Response:
        artifact: RecorderArtifact | None = None
        async with self._lock:
            if self._recorder is None:
                return web.json_response({"error": "no active session"}, status=400)
            rec = self._recorder
            self._recorder = None
            artifact = await rec.stop()

        return web.json_response(
            {
                "ok": True,
                "run_id": artifact.run_id,
                "step_count": artifact.step_count,
                "stepgraphPath": artifact.stepgraph_path,
                "manifestPath": artifact.manifest_path,
                "sourceUrl": artifact.source_url,
            }
        )

    async def handle_control(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)

        if not isinstance(body, dict):
            return web.json_response({"error": "body must be object"}, status=400)

        async with self._lock:
            rec = self._recorder
            if rec is None:
                return web.json_response({"error": "no active session"}, status=400)
            await rec.apply_control_action(body)
        return web.json_response({"ok": True})

    async def handle_list_runs(self, _request: web.Request) -> web.Response:
        return web.json_response({"runs": _list_saved_runs()})

    async def handle_run_steps(self, request: web.Request) -> web.Response:
        """Return step summaries for a saved run (disk only — no browser, no replay)."""
        q = request.rel_url.query
        run_id = str(q.get("runId") or q.get("run_id") or "").strip()
        if not run_id:
            return web.json_response({"error": "runId query parameter required"}, status=400)
        if Path(run_id).name != run_id:
            return web.json_response({"error": "runId must not contain path separators"}, status=400)
        run_dir = resolve_runs_root() / run_id
        if not run_dir.is_dir():
            return web.json_response({"error": f"run not found: {run_id!r}"}, status=404)
        sg = run_dir / "stepgraph.json"
        if not sg.is_file():
            return web.json_response({"error": f"stepgraph not found for runId {run_id!r}"}, status=404)
        try:
            graph = StepGraph.model_validate_json(sg.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": f"invalid stepgraph: {exc}"}, status=400)
        return web.json_response(
            {
                "runId": run_id,
                "graphRunId": graph.run_id,
                "stepCount": len(graph.steps),
                "steps": [_step_summary(s) for s in graph.steps],
            }
        )

    async def _replay_worker(self, proc: asyncio.subprocess.Process, log_path: Path) -> None:
        try:
            out, _ = await proc.communicate()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_bytes(out or b"")
            self._replay_last_exit = proc.returncode
        except Exception as exc:  # noqa: BLE001
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(f"replay worker error: {exc}\n", encoding="utf-8")
            self._replay_last_exit = -1
        finally:
            self._replay_running = False
            self._replay_proc = None
            self._replay_task = None

    async def handle_replay(self, request: web.Request) -> web.Response:
        async with self._lock:
            if self._interactive_start_in_progress:
                return web.json_response(
                    {"error": "wait for interactive replay to finish starting before subprocess replay"},
                    status=409,
                )
            if self._interactive is not None and self._interactive.active:
                return web.json_response({"error": "stop interactive replay before subprocess replay"}, status=409)
        if self._replay_running:
            return web.json_response({"error": "replay already running"}, status=409)
        self._replay_last_exit = None
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be object"}, status=400)

        run_id = str(body.get("runId") or body.get("run_id") or "").strip()
        raw_path = body.get("stepgraphPath") or body.get("stepgraph_path")
        if run_id:
            sg = get_run_layout(run_id).run_dir / "stepgraph.json"
        elif isinstance(raw_path, str) and raw_path.strip():
            sg = Path(raw_path.strip()).expanduser().resolve()
        else:
            return web.json_response({"error": "runId or stepgraphPath required"}, status=400)

        if not sg.is_file():
            return web.json_response({"error": f"stepgraph not found: {sg}"}, status=400)

        log_path = sg.parent / "ui_replay.log"
        cwd = _agent_cwd()
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "agent.cli",
                "run",
                "--auto-approve-hard",
                str(sg),
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": f"failed to start replay: {exc}"}, status=500)

        self._replay_proc = proc
        self._replay_log_path = log_path
        self._replay_running = True
        self._replay_task = asyncio.create_task(self._replay_worker(proc, log_path))
        return web.json_response(
            {
                "ok": True,
                "started": True,
                "logPath": str(log_path),
                "stepgraphPath": str(sg),
            }
        )

    async def handle_replay_status(self, _request: web.Request) -> web.Response:
        log_path = self._replay_log_path
        tail = ""
        if log_path is not None and log_path.is_file():
            raw = log_path.read_bytes()
            tail = raw[-12_000:].decode("utf-8", errors="replace")
        return web.json_response(
            {
                "running": self._replay_running,
                "exitCode": self._replay_last_exit,
                "logPath": str(log_path) if log_path else None,
                "logTail": tail,
            }
        )

    async def handle_duplicate_run(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be object"}, status=400)
        src = str(body.get("sourceRunId") or body.get("source_run_id") or "").strip()
        tgt = str(body.get("targetRunId") or body.get("target_run_id") or "").strip() or generate_run_id()
        if not src:
            return web.json_response({"error": "sourceRunId required"}, status=400)
        if src == tgt:
            return web.json_response({"error": "source and target must differ"}, status=400)

        src_layout = get_run_layout(src)
        tgt_layout = get_run_layout(tgt)
        if not src_layout.run_dir.is_dir():
            return web.json_response({"error": "source run not found"}, status=404)
        if tgt_layout.run_dir.exists():
            return web.json_response({"error": "target run already exists"}, status=409)

        try:
            shutil.copytree(src_layout.run_dir, tgt_layout.run_dir)
            sg_path = tgt_layout.run_dir / "stepgraph.json"
            graph = StepGraph.model_validate_json(sg_path.read_text(encoding="utf-8"))
            graph = graph.model_copy(update={"run_id": tgt})
            sg_path.write_text(graph.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
            mf_path = tgt_layout.run_dir / "manifest.json"
            if mf_path.is_file():
                man = json.loads(mf_path.read_text(encoding="utf-8"))
                if isinstance(man, dict):
                    man["runId"] = tgt
                    mf_path.write_text(json.dumps(man, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            if tgt_layout.run_dir.exists():
                shutil.rmtree(tgt_layout.run_dir, ignore_errors=True)
            return web.json_response({"error": str(exc)}, status=500)

        return web.json_response(
            {
                "ok": True,
                "sourceRunId": src,
                "targetRunId": tgt,
                "stepgraphPath": str(sg_path.resolve()),
            }
        )

    async def handle_replay_stop(self, _request: web.Request) -> web.Response:
        async with self._lock:
            proc = self._replay_proc
            task = self._replay_task
            if proc is None and not self._replay_running:
                return web.json_response({"ok": True, "stopped": False})
            if proc is not None and proc.returncode is None:
                try:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=25.0)
                    except TimeoutError:
                        proc.kill()
                        await proc.wait()
                except ProcessLookupError:
                    pass
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    pass
            self._replay_running = False
            self._replay_proc = None
            self._replay_task = None
            exit_code = proc.returncode if proc is not None else None
            self._replay_last_exit = exit_code if exit_code is not None else -1
            return web.json_response({"ok": True, "stopped": True, "exitCode": self._replay_last_exit})

    async def handle_interactive_start(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be object"}, status=400)
        run_id = str(body.get("runId") or body.get("run_id") or "").strip()
        if not run_id:
            return web.json_response({"error": "runId required"}, status=400)
        async with self._lock:
            if self._recorder is not None:
                return web.json_response({"error": "stop recording session first"}, status=409)
            if self._replay_running:
                return web.json_response({"error": "stop subprocess replay first"}, status=409)
            if self._interactive is not None and self._interactive.active:
                return web.json_response({"error": "interactive replay already active"}, status=409)
            if self._interactive_start_in_progress:
                return web.json_response({"error": "interactive start already in progress"}, status=409)
            self._interactive_start_in_progress = True

        sess: InteractiveReplaySession | None = None
        graph: StepGraph | None = None
        try:
            sess = InteractiveReplaySession()
            graph = await sess.start(run_id)
        except Exception as exc:  # noqa: BLE001
            if sess is not None:
                try:
                    await sess.stop()
                except Exception:  # noqa: BLE001
                    pass
            async with self._lock:
                self._interactive_start_in_progress = False
            return web.json_response({"error": str(exc)}, status=400)

        async with self._lock:
            self._interactive_start_in_progress = False
            if self._recorder is not None:
                try:
                    await sess.stop()
                except Exception:  # noqa: BLE001
                    pass
                return web.json_response({"error": "stop recording session first"}, status=409)
            if self._replay_running:
                try:
                    await sess.stop()
                except Exception:  # noqa: BLE001
                    pass
                return web.json_response({"error": "stop subprocess replay first"}, status=409)
            if self._interactive is not None and self._interactive.active:
                try:
                    await sess.stop()
                except Exception:  # noqa: BLE001
                    pass
                return web.json_response({"error": "interactive replay already active"}, status=409)
            self._interactive = sess

        assert graph is not None
        return web.json_response(
            {"ok": True, "runId": run_id, "stepCount": len(graph.steps), "graphRunId": graph.run_id}
        )

    async def handle_interactive_stop(self, _request: web.Request) -> web.Response:
        """Clear the dashboard session pointer immediately, then close Playwright outside the lock.

        Previously we required ``iv.active``; after partial failures ``graph``/``session`` could disagree
        and Stop returned 400 while a Chromium window was still open. A long ``browser.close()`` also
        blocked every other dashboard request while holding the lock.
        """
        async with self._lock:
            self._interactive_start_in_progress = False
            iv = self._interactive
            self._interactive = None
        if iv is not None:
            try:
                await asyncio.wait_for(iv.stop(), timeout=90.0)
            except asyncio.TimeoutError:
                pass
            except Exception:  # noqa: BLE001
                pass
        return web.json_response({"ok": True, "alreadyStopped": iv is None})

    async def handle_interactive_state(self, _request: web.Request) -> web.Response:
        inactive = {
            "active": False,
            "starting": False,
            "runId": None,
            "folderRunId": None,
            "steps": [],
            "lastError": None,
            "runActivity": None,
            "pickPending": False,
            "pickIntent": None,
            "pickResult": None,
        }
        async with self._lock:
            if self._interactive_start_in_progress:
                booting = dict(inactive)
                booting["starting"] = True
                return web.json_response(booting)
            iv = self._interactive
            if iv is None or not iv.active or iv.graph is None:
                return web.json_response(inactive)
            g = iv.graph
            steps_payload: list[dict[str, Any]] = []
            for s in g.steps:
                row = _step_summary(s)
                if (s.action or "").strip().lower() == "upload":
                    ue = iv.last_upload_error_for_step(s.step_id)
                    if ue:
                        row["lastUploadError"] = ue
                steps_payload.append(row)
            folder_run_id = iv.folder_run_id
            run_id = g.run_id
        # Do not hold the dashboard lock across page.evaluate — start() also needs the lock;
        # keeping pick read short avoids blocking the step list after interactive opens.
        try:
            pick_state = await asyncio.wait_for(iv.read_interactive_pick_state(), timeout=3.0)
        except asyncio.TimeoutError:
            pick_state = {"pickPending": False, "pickIntent": None, "pickResult": None}
        except Exception:  # noqa: BLE001
            pick_state = {"pickPending": False, "pickIntent": None, "pickResult": None}
        async with self._lock:
            if self._interactive is not iv or not iv.active or iv.graph is None:
                return web.json_response(inactive)
            last_error = iv.last_error
            run_activity = iv.run_activity_snapshot()
        return web.json_response(
            {
                "active": True,
                "starting": False,
                "runId": run_id,
                "folderRunId": folder_run_id,
                "steps": steps_payload,
                "lastError": last_error,
                "runActivity": run_activity,
                **pick_state,
            }
        )

    async def handle_interactive_begin_pick(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be object"}, status=400)
        kind = str(body.get("kind") or "").strip()
        if kind not in ("wait_for", "assert_visible", "assert_text", "upload"):
            return web.json_response(
                {"error": "kind must be wait_for, assert_visible, assert_text, or upload"},
                status=400,
            )
        state = str(body.get("state") or "visible").strip() or "visible"
        timeout_ms = int(body.get("timeoutMs") or body.get("timeout_ms") or 30_000)
        contains = bool(body.get("contains", True))
        async with self._lock:
            iv = self._interactive
            if iv is None or not iv.active:
                return web.json_response({"error": "interactive replay not active"}, status=400)
            try:
                await iv.begin_pick(kind=kind, state=state, timeout_ms=timeout_ms, contains=contains)
            except Exception as exc:  # noqa: BLE001
                return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"ok": True, "kind": kind})

    async def handle_interactive_cancel_pick(self, _request: web.Request) -> web.Response:
        async with self._lock:
            iv = self._interactive
            if iv is None or not iv.active:
                return web.json_response({"error": "interactive replay not active"}, status=400)
            await iv.cancel_pick()
        return web.json_response({"ok": True})

    async def handle_interactive_consume_pick(self, _request: web.Request) -> web.Response:
        async with self._lock:
            iv = self._interactive
            if iv is None or not iv.active:
                return web.json_response({"error": "interactive replay not active"}, status=400)
            data = iv.consume_pick_result()
        return web.json_response({"ok": True, "pickResult": data})

    async def handle_interactive_run_step(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be object"}, status=400)
        step_id = str(body.get("stepId") or body.get("step_id") or "").strip()
        if not step_id:
            return web.json_response({"error": "stepId required"}, status=400)
        async with self._lock:
            iv = self._interactive
            if iv is None or not iv.active:
                return web.json_response({"error": "interactive replay not active"}, status=400)
            iv.last_error = None
        try:
            await iv.run_step_id(step_id)
        except Exception as exc:  # noqa: BLE001
            async with self._lock:
                if self._interactive is iv:
                    iv.last_error = str(exc)
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"ok": True, "stepId": step_id})

    async def handle_interactive_run_range(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be object"}, status=400)
        a = str(body.get("fromStepId") or body.get("from_step_id") or "").strip()
        b = str(body.get("toStepId") or body.get("to_step_id") or "").strip()
        if not a or not b:
            return web.json_response({"error": "fromStepId and toStepId required"}, status=400)
        async with self._lock:
            iv = self._interactive
            if iv is None or not iv.active:
                return web.json_response({"error": "interactive replay not active"}, status=400)
            iv.last_error = None
        try:
            await iv.run_range(a, b)
        except Exception as exc:  # noqa: BLE001
            async with self._lock:
                if self._interactive is iv:
                    iv.last_error = str(exc)
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"ok": True, "fromStepId": a, "toStepId": b})

    async def handle_interactive_delete_step(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be object"}, status=400)
        step_id = str(body.get("stepId") or body.get("step_id") or "").strip()
        if not step_id:
            return web.json_response({"error": "stepId required"}, status=400)
        async with self._lock:
            iv = self._interactive
            if iv is None or not iv.active:
                return web.json_response({"error": "interactive replay not active"}, status=400)
            ok = iv.delete_step(step_id)
            if not ok:
                return web.json_response({"error": "step not found"}, status=404)
            iv.last_error = None
        return web.json_response({"ok": True, "stepId": step_id})

    async def handle_interactive_force_fix(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be object"}, status=400)
        step_id = str(body.get("stepId") or body.get("step_id") or "").strip()
        if not step_id:
            return web.json_response({"error": "stepId required"}, status=400)
        async with self._lock:
            iv = self._interactive
            if iv is None or not iv.active:
                return web.json_response({"error": "interactive replay not active"}, status=400)
        try:
            probe = await iv.probe_step_target_for_force_fix(step_id)
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {
                    "ok": False,
                    "error": str(exc),
                    "probeFailed": True,
                    "message": str(exc),
                },
                status=500,
            )
        if not probe.get("ok"):
            reason = str(probe.get("reason") or "unknown")
            status = 422 if reason == "not_on_page" else 400
            return web.json_response(
                {
                    "ok": False,
                    "error": probe.get("message", "Could not verify selectors on the interactive tab."),
                    "probeFailed": True,
                    "reason": reason,
                    "currentUrl": probe.get("currentUrl"),
                    "lastError": probe.get("lastError"),
                    "triedCount": probe.get("triedCount"),
                    "state": probe.get("state"),
                },
                status=status,
            )
        upload_area_on_page = False
        step_for_upload = None
        async with self._lock:
            if self._interactive is iv and iv.graph is not None:
                step_for_upload = next((s for s in iv.graph.steps if s.step_id == step_id), None)
        if (
            step_for_upload is not None
            and (step_for_upload.action or "").strip().lower() == "upload"
            and iv._tool_runtime is not None
            and iv._tab_id is not None
        ):
            ok_ua, _ = await iv._tool_runtime.probe_selector(
                tab_id=iv._tab_id,
                selector='[data-testid="upload-area"]',
                state="attached",
                timeout_ms=2000,
            )
            upload_area_on_page = bool(ok_ua)
        async with self._lock:
            if self._interactive is not iv or not iv.active:
                return web.json_response(
                    {
                        "ok": False,
                        "error": "Interactive replay stopped or changed — try again.",
                        "probeFailed": True,
                    },
                    status=409,
                )
            outcome = iv.force_fix_step_target(step_id, upload_area_on_page=upload_area_on_page)
            if outcome is None:
                return web.json_response(
                    {
                        "error": (
                            "This step has no locator target (for example navigate-only). "
                            "Force fix only applies to steps that store a selector bundle."
                        ),
                    },
                    status=400,
                )
            iv.last_error = None
        probe_payload = {
            "ok": True,
            "matchedSelector": probe.get("matchedSelector"),
            "state": probe.get("state"),
            "currentUrl": probe.get("currentUrl"),
            "triedCount": probe.get("triedCount"),
            "uploadAreaPresent": upload_area_on_page,
        }
        return web.json_response(
            {
                "ok": True,
                "stepId": step_id,
                "probe": probe_payload,
                "changed": outcome.changed,
                "primarySelector": outcome.primary_selector,
                "fallbackCount": outcome.fallback_count,
                "message": outcome.message,
                "caveat": FORCE_FIX_UI_CAVEAT,
                "uploadAreaPresent": upload_area_on_page,
            }
        )

    async def handle_interactive_insert_step(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be object"}, status=400)
        after_raw = body.get("afterStepId") or body.get("after_step_id")
        if after_raw is None or after_raw == "":
            after_norm = "__append__"
        else:
            after_norm = str(after_raw).strip()
        async with self._lock:
            iv = self._interactive
            if iv is None or not iv.active:
                return web.json_response({"error": "interactive replay not active"}, status=400)
            try:
                step = build_step_from_interactive_insert_body(body)
                new_id = iv.insert_step(after_norm, step)
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)
            except Exception as exc:  # noqa: BLE001
                return web.json_response({"error": str(exc)}, status=500)
            iv.last_error = None
        return web.json_response({"ok": True, "stepId": new_id})

    async def handle_interactive_save(self, _request: web.Request) -> web.Response:
        async with self._lock:
            iv = self._interactive
            if iv is None or not iv.active:
                return web.json_response({"error": "interactive replay not active"}, status=400)
            try:
                path = iv.save_graph_to_disk()
            except Exception as exc:  # noqa: BLE001
                return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"ok": True, "stepgraphPath": str(path)})

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/api/state", self.handle_state)
        app.router.add_post("/api/session/start", self.handle_session_start)
        app.router.add_post("/api/session/stop", self.handle_session_stop)
        app.router.add_post("/api/control", self.handle_control)
        app.router.add_get("/api/runs", self.handle_list_runs)
        app.router.add_get("/api/runs/steps", self.handle_run_steps)
        app.router.add_post("/api/replay", self.handle_replay)
        app.router.add_post("/api/replay/stop", self.handle_replay_stop)
        app.router.add_get("/api/replay/status", self.handle_replay_status)
        app.router.add_post("/api/runs/duplicate", self.handle_duplicate_run)
        app.router.add_post("/api/interactive/start", self.handle_interactive_start)
        app.router.add_post("/api/interactive/stop", self.handle_interactive_stop)
        app.router.add_get("/api/interactive/state", self.handle_interactive_state)
        app.router.add_post("/api/interactive/begin_pick", self.handle_interactive_begin_pick)
        app.router.add_post("/api/interactive/cancel_pick", self.handle_interactive_cancel_pick)
        app.router.add_post("/api/interactive/consume_pick", self.handle_interactive_consume_pick)
        app.router.add_post("/api/interactive/run_step", self.handle_interactive_run_step)
        app.router.add_post("/api/interactive/run_range", self.handle_interactive_run_range)
        app.router.add_post("/api/interactive/delete_step", self.handle_interactive_delete_step)
        app.router.add_post("/api/interactive/force_fix", self.handle_interactive_force_fix)
        app.router.add_post("/api/interactive/insert_step", self.handle_interactive_insert_step)
        app.router.add_post("/api/interactive/save", self.handle_interactive_save)
        return app

    async def wait_shutdown(self) -> None:
        await self._shutdown.wait()

    def request_shutdown(self) -> None:
        self._shutdown.set()


async def run_dashboard(
    *,
    host: str,
    port: int,
    open_browser: bool,
    auto_url: str | None,
    storage_state: str | None,
    headless: bool,
    record_armed_start: bool,
) -> None:
    dash = RecorderDashboardApp()
    app = dash.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    url = f"http://{host}:{port}/"
    if open_browser:
        webbrowser.open(url)

    loop = asyncio.get_running_loop()

    def _sig() -> None:
        dash.request_shutdown()

    try:
        loop.add_signal_handler(signal.SIGINT, _sig)
        loop.add_signal_handler(signal.SIGTERM, _sig)
    except NotImplementedError:
        pass

    if auto_url and auto_url.strip():
        async with dash._lock:
            if dash._recorder is None:
                dash._recorder = StepGraphRecorder(
                    url=auto_url.strip(),
                    headless=headless,
                    storage_state=storage_state,
                    browser_ui=False,
                    dashboard_control=True,
                    recording_armed_start=record_armed_start,
                    default_record_upload_path=None,
                )
                await dash._recorder.start()

    try:
        await dash.wait_shutdown()
    finally:
        async with dash._lock:
            dash._interactive_start_in_progress = False
            if dash._interactive is not None:
                try:
                    await dash._interactive.stop()
                except Exception:  # noqa: BLE001
                    pass
                dash._interactive = None
            proc = dash._replay_proc
            if proc is not None and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=8.0)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            if dash._recorder is not None:
                try:
                    await dash._recorder.stop()
                except Exception:  # noqa: BLE001
                    pass
                dash._recorder = None
        await runner.cleanup()
