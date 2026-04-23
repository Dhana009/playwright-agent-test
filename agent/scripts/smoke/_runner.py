from __future__ import annotations

import traceback
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from time import perf_counter
from typing import Iterator

from agent.core.ids import generate_run_id
from agent.testing.bug_log import BugLogWriter, ErrorClass


@dataclass
class CaseResult:
    name: str
    ok: bool
    elapsed_ms: int
    stdout: str
    stderr: str
    error: str | None = None


class SmokeRunner:
    def __init__(self, *, phase: str, run_id: str | None = None, default_task: str = "unspecified") -> None:
        self.phase = phase
        self.run_id = run_id or generate_run_id()
        self.default_task = default_task
        self.bugs = BugLogWriter(run_id=self.run_id)
        self.results: list[CaseResult] = []

    @contextmanager
    def case(
        self,
        name: str,
        *,
        task: str | None = None,
        feature: str = "smoke",
        error_class: ErrorClass = "runtime",
    ) -> Iterator[None]:
        started = perf_counter()
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        failure: Exception | None = None

        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
            try:
                yield
            except Exception as exc:
                failure = exc

        elapsed_ms = int((perf_counter() - started) * 1000)
        captured_stdout = stdout_buffer.getvalue().strip()
        captured_stderr = stderr_buffer.getvalue().strip()

        if failure is None:
            result = CaseResult(
                name=name,
                ok=True,
                elapsed_ms=elapsed_ms,
                stdout=captured_stdout,
                stderr=captured_stderr,
            )
            self.results.append(result)
            self._print_case(result)
            return

        error_text = "".join(traceback.format_exception_only(type(failure), failure)).strip()
        result = CaseResult(
            name=name,
            ok=False,
            elapsed_ms=elapsed_ms,
            stdout=captured_stdout,
            stderr=captured_stderr,
            error=error_text,
        )
        self.results.append(result)
        self.bugs.append_new(
            phase=self.phase,
            task=task or self.default_task,
            feature=feature,
            error_class=error_class,
            summary=error_text,
            hypothesis="pending triage",
            change="none",
            outcome="open",
            artifact_refs=[f"runs/{self.run_id}/log.jsonl"],
        )
        self._print_case(result)

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def finalize(self) -> int:
        passed = sum(1 for result in self.results if result.ok)
        failed = len(self.results) - passed
        print()
        print(f"Phase {self.phase}: {passed} passed, {failed} failed, run_id={self.run_id}")
        if failed:
            print(f"bugs_jsonl={self.bugs.path}")
        return 1 if failed else 0

    def _print_case(self, result: CaseResult) -> None:
        status = "PASS" if result.ok else "FAIL"
        print(f"{status:<5} {result.name:<36} {result.elapsed_ms:>5} ms")
        if result.error:
            print(f"  error: {result.error}")
        if result.stdout:
            print("  stdout:")
            for line in result.stdout.splitlines():
                print(f"    {line}")
        if result.stderr:
            print("  stderr:")
            for line in result.stderr.splitlines():
                print(f"    {line}")
