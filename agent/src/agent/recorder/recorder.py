# Ported from playwright-repo-test/lib/browser/inject.js and lib/record.js — adapted for agent/
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import unquote

from playwright.async_api import FileChooser, Frame, Locator, Page
from pydantic import BaseModel, ConfigDict, Field

from agent.core.ids import generate_run_id
from agent.core.logging import configure_logging, get_logger
from agent.execution.browser import BrowserSession, StorageStateInput
from agent.locator.engine import LocatorEngine
from agent.stepgraph.models import (
    LocatorBundle,
    Step,
    StepEdge,
    StepGraph,
    StepMode,
    TimeoutPolicy,
)
from agent.storage.files import get_run_layout

RECORDER_OPERATOR_MODES: tuple[str, ...] = ("auto", "assert_visible", "assert_text")

_CAPTURE_QUEUE_INIT_SCRIPT = """
(() => {
  if (window.__agentRecorderInstalled) return;
  window.__agentRecorderInstalled = true;
  window.__agentRecorderSeq = 0;
  window.__agentRecorderQueue = [];
  window.__agentRecorderArmed = true;

  window.__agentRecorderDrain = () => {
    const snapshot = window.__agentRecorderQueue.slice();
    window.__agentRecorderQueue = [];
    return snapshot;
  };

  function toXPath(node) {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return '';
    if (node.id) return `//*[@id="${node.id}"]`;
    const parts = [];
    let current = node;
    while (current && current.nodeType === Node.ELEMENT_NODE) {
      let index = 1;
      let sibling = current.previousElementSibling;
      while (sibling) {
        if (sibling.tagName === current.tagName) index += 1;
        sibling = sibling.previousElementSibling;
      }
      const tag = current.tagName.toLowerCase();
      parts.unshift(index > 1 ? `${tag}[${index}]` : tag);
      current = current.parentElement;
    }
    return '/' + parts.join('/');
  }

  function parentTrail(node) {
    const out = [];
    let current = node && node.parentElement;
    let depth = 0;
    while (current && current.tagName !== 'BODY' && depth < 4) {
      out.push({
        tag: current.tagName.toLowerCase(),
        id: current.id || '',
        className: typeof current.className === 'string' ? current.className : '',
        testid:
          current.getAttribute('data-testid') ||
          current.getAttribute('data-test-id') ||
          current.getAttribute('data-qa') ||
          '',
      });
      current = current.parentElement;
      depth += 1;
    }
    return out;
  }

  function dataAttrs(node) {
    const attrs = {};
    if (!node || !node.attributes) return attrs;
    for (const attr of node.attributes) {
      if (attr.name.startsWith('data-')) attrs[attr.name] = attr.value;
    }
    return attrs;
  }

  function collectTarget(node) {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return null;
    const text = (node.innerText || node.textContent || '').trim().slice(0, 120);
    const testid =
      node.getAttribute('data-testid') ||
      node.getAttribute('data-test-id') ||
      node.getAttribute('data-qa') ||
      '';

    return {
      tag: node.tagName.toLowerCase(),
      id: node.id || '',
      className: typeof node.className === 'string' ? node.className : '',
      testid,
      text,
      placeholder: node.getAttribute('placeholder') || '',
      ariaLabel: node.getAttribute('aria-label') || '',
      role: node.getAttribute('role') || '',
      inputType: node.getAttribute('type') || '',
      name: node.getAttribute('name') || '',
      dataAttrs: dataAttrs(node),
      siblingIndex: node.parentElement ? Array.from(node.parentElement.children).indexOf(node) : -1,
      parents: parentTrail(node),
      absoluteXPath: toXPath(node),
      frameContext: [],
      targetSemanticKey: [node.tagName.toLowerCase(), testid || node.id || node.getAttribute('name') || text]
        .filter(Boolean)
        .join(':'),
    };
  }

  /** Find a file input associated with a click (native input, child, or ancestor subtree). */
  function __agentFindAssociatedFileInput(node) {
    if (!node || node.nodeType !== 1) return null;
    const tag = node.tagName.toLowerCase();
    const typ = String(node.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && typ === 'file') return node;
    try {
      if (node.querySelector) {
        const inner = node.querySelector('input[type="file"],input[type=file]');
        if (inner) return inner;
      }
    } catch (_) {}
    try {
      let s = node.previousElementSibling;
      while (s) {
        const tg = s.tagName && s.tagName.toLowerCase();
        const ty = String(s.getAttribute('type') || '').toLowerCase();
        if (tg === 'input' && ty === 'file') return s;
        const inner = s.querySelector && s.querySelector('input[type="file"],input[type=file]');
        if (inner) return inner;
        s = s.previousElementSibling;
      }
      s = node.nextElementSibling;
      while (s) {
        const tg = s.tagName && s.tagName.toLowerCase();
        const ty = String(s.getAttribute('type') || '').toLowerCase();
        if (tg === 'input' && ty === 'file') return s;
        const inner = s.querySelector && s.querySelector('input[type="file"],input[type=file]');
        if (inner) return inner;
        s = s.nextElementSibling;
      }
    } catch (_) {}
    let p = node.parentElement;
    for (let depth = 0; depth < 8 && p; depth++) {
      try {
        if (p.querySelector) {
          const scoped = p.querySelector('input[type="file"],input[type=file]');
          if (scoped) return scoped;
        }
      } catch (_) {}
      p = p.parentElement;
    }
    return null;
  }

  window.__agentPickIntent = null;

  // ── Inspect-style pick UI (ported from playwright-repo-test panel hover + flash) ──
  let __agentPickHoverEl = null;
  let __agentPickUiReady = false;

  function __agentPickIntentActive() {
    const pi = window.__agentPickIntent;
    return !!(pi && typeof pi === 'object' && pi.kind);
  }

  function __agentEnsurePickUi() {
    if (__agentPickUiReady) return;
    __agentPickUiReady = true;
    const st = document.createElement('style');
    st.id = '__agent_pick_styles';
    st.textContent = `
      .__agent_pick_hover {
        outline: 2px solid #2979ff !important;
        outline-offset: 2px !important;
        background: rgba(41, 121, 255, 0.08) !important;
        cursor: crosshair !important;
      }
      #__agent_pick_tip {
        position: fixed;
        bottom: 16px;
        left: 50%;
        transform: translateX(-50%);
        background: #0f172a;
        color: #e2e8f0;
        font: 12px/1.35 ui-monospace, SFMono-Regular, Menlo, monospace;
        padding: 6px 14px;
        border-radius: 6px;
        z-index: 2147483645;
        pointer-events: none;
        white-space: nowrap;
        max-width: 92vw;
        overflow: hidden;
        text-overflow: ellipsis;
        border: 1px solid #334155;
        box-shadow: 0 4px 24px rgba(0,0,0,0.45);
      }
    `;
    (document.head || document.documentElement).appendChild(st);
    const tip = document.createElement('div');
    tip.id = '__agent_pick_tip';
    tip.style.display = 'none';
    (document.body || document.documentElement).appendChild(tip);
  }

  function __agentPickPreview(el) {
    if (!el || el.nodeType !== 1) return '';
    const tag = el.tagName.toLowerCase();
    let idPart = '';
    if (el.id) {
      try {
        idPart = typeof CSS !== 'undefined' && CSS.escape ? '#' + CSS.escape(el.id) : '#' + el.id;
      } catch (_) {
        idPart = '#' + el.id;
      }
    }
    const tid = el.getAttribute('data-testid');
    const tidStr = tid ? '[data-testid="' + String(tid).replace(/"/g, '\\"') + '"]' : '';
    const text = (el.innerText || el.textContent || '').trim().slice(0, 48);
    const cls =
      typeof el.className === 'string' && el.className
        ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.')
        : '';
    return '<' + tag + (idPart || tidStr || cls) + '>' + (text ? ' "' + text + '"' : '');
  }

  function __agentPickClearHoverOnly() {
    if (__agentPickHoverEl) {
      try {
        __agentPickHoverEl.classList.remove('__agent_pick_hover');
      } catch (_) {}
      __agentPickHoverEl = null;
    }
    const tip = document.getElementById('__agent_pick_tip');
    if (tip) tip.style.display = 'none';
    try {
      document.documentElement.style.removeProperty('cursor');
    } catch (_) {
      document.documentElement.style.cursor = '';
    }
  }

  function __agentFlashPick(el) {
    if (!el || el.nodeType !== 1) return;
    try {
      el.classList.remove('__agent_pick_hover');
    } catch (_) {}
    const prev = el.getAttribute('style') || '';
    try {
      el.setAttribute(
        'style',
        prev +
          ';outline:3px solid #22c55e!important;outline-offset:2px!important;background:rgba(34,197,94,0.14)!important;'
      );
      setTimeout(() => {
        try {
          el.setAttribute('style', prev);
        } catch (_) {}
      }, 750);
    } catch (_) {}
  }

  window.__agentPickCancelUi = function () {
    window.__agentPickIntent = null;
    __agentPickClearHoverOnly();
  };

  document.addEventListener(
    'mousemove',
    (e) => {
      if (!__agentPickIntentActive()) {
        if (__agentPickHoverEl) __agentPickClearHoverOnly();
        return;
      }
      __agentEnsurePickUi();
      document.documentElement.style.cursor = 'crosshair';
      const el = document.elementFromPoint(e.clientX, e.clientY);
      if (!el || el.nodeType !== 1) {
        __agentPickClearHoverOnly();
        return;
      }
      if (el.closest && el.closest('[data-agent-recorder-hud]')) {
        __agentPickClearHoverOnly();
        return;
      }
      if (el.id === '__agent_pick_tip') {
        __agentPickClearHoverOnly();
        return;
      }
      if (__agentPickHoverEl === el) return;
      if (__agentPickHoverEl) {
        try {
          __agentPickHoverEl.classList.remove('__agent_pick_hover');
        } catch (_) {}
      }
      __agentPickHoverEl = el;
      try {
        el.classList.add('__agent_pick_hover');
      } catch (_) {}
      const tip = document.getElementById('__agent_pick_tip');
      if (tip) {
        tip.textContent = __agentPickPreview(el);
        tip.style.display = 'block';
      }
    },
    true
  );

  document.addEventListener(
    'keydown',
    (e) => {
      if (e.key !== 'Escape') return;
      if (!__agentPickIntentActive()) return;
      e.preventDefault();
      e.stopPropagation();
      window.__agentPickCancelUi();
    },
    true
  );

  function emitCapture(raw) {
    const entry = {
      ...raw,
      seq: ++window.__agentRecorderSeq,
      capturedAt: new Date().toISOString(),
      frameUrl: window.location.href,
      pageUrl: window.location.href,
    };
    window.__agentRecorderQueue.push(entry);
    if (typeof window.__agentRecordEmit === 'function') {
      window.__agentRecordEmit(entry);
    }
  }

  document.addEventListener(
    'click',
    (event) => {
      const pi = window.__agentPickIntent;
      if (pi && typeof pi === 'object' && pi.kind) {
        const node = event.target instanceof Element ? event.target.closest('*') : null;
        if (node && node.closest && node.closest('[data-agent-recorder-hud]')) return;
        const target = collectTarget(node);
        if (!target) return;
        event.preventDefault();
        event.stopPropagation();
        const pickEntry = {
          eventType: 'click',
          target,
          modifiers: {
            altKey: !!event.altKey,
            ctrlKey: !!event.ctrlKey,
            metaKey: !!event.metaKey,
            shiftKey: !!event.shiftKey,
          },
          seq: ++window.__agentRecorderSeq,
          capturedAt: new Date().toISOString(),
          frameUrl: window.location.href,
          pageUrl: window.location.href,
          pickIntent: {
            kind: pi.kind,
            state: pi.state || 'visible',
            timeoutMs: pi.timeoutMs || 30000,
            contains: pi.contains !== false,
          },
        };
        void (async () => {
          try {
            __agentFlashPick(node);
            if (typeof window.__agentPickEmit === 'function') {
              await window.__agentPickEmit(pickEntry);
            }
          } catch (err) {
            console.error('[agent-recorder] __agentPickEmit failed', err);
          } finally {
            window.__agentPickIntent = null;
            __agentPickClearHoverOnly();
          }
        })();
        return;
      }
      if (!window.__agentRecorderArmed) return;
      const node = event.target instanceof Element ? event.target.closest('*') : null;
      if (node && node.closest && node.closest('[data-agent-recorder-hud]')) return;
      const target = collectTarget(node);
      if (!target) return;
      const fi = __agentFindAssociatedFileInput(node);
      const payload = {
        eventType: 'click',
        target,
        modifiers: {
          altKey: !!event.altKey,
          ctrlKey: !!event.ctrlKey,
          metaKey: !!event.metaKey,
          shiftKey: !!event.shiftKey,
        },
      };
      if (fi && fi !== node) {
        payload.associatedFileInputTarget = collectTarget(fi);
      }
      emitCapture(payload);
    },
    true
  );

  document.addEventListener(
    'input',
    (event) => {
      if (!window.__agentRecorderArmed) return;
      const node = event.target instanceof Element ? event.target : null;
      if (node && node.closest && node.closest('[data-agent-recorder-hud]')) return;
      const tag = node && node.tagName ? String(node.tagName).toLowerCase() : '';
      const typ = node && node.getAttribute ? String(node.getAttribute('type') || '').toLowerCase() : '';
      if (tag === 'input' && typ === 'file') return;
      const target = collectTarget(node);
      if (!target) return;
      const value = node && 'value' in node ? node.value : (node?.textContent || '').trim();
      emitCapture({
        eventType: 'input',
        target,
        value,
      });
    },
    true
  );

  document.addEventListener(
    'keydown',
    (event) => {
      if (!window.__agentRecorderArmed) return;
      const node = event.target instanceof Element ? event.target : null;
      if (node && node.closest && node.closest('[data-agent-recorder-hud]')) return;
      const target = collectTarget(node);
      if (!target) return;
      emitCapture({
        eventType: 'keydown',
        target,
        key: event.key,
        code: event.code,
      });
    },
    true
  );
})();
""".strip()

_RECORDER_HUD_INIT_SCRIPT = """
(() => {
  if (window.__agentRecorderHudInstalled) return;

  function mount() {
    if (document.getElementById('__agent_recorder_hud_root')) return;
    const root = document.createElement('div');
    root.id = '__agent_recorder_hud_root';
    root.setAttribute('data-agent-recorder-hud', 'true');
    root.innerHTML = `
<div style="font:12px/1.4 system-ui,sans-serif;color:#e2e8f0;background:#0f172a;border:1px solid #334155;
border-radius:8px;padding:8px 10px;min-width:220px;box-shadow:0 4px 24px rgba(0,0,0,0.45);">
  <div id="__agent_hud_status" style="margin-bottom:8px;font-size:11px;color:#94a3b8;">Recorder HUD</div>
  <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
    <button type="button" id="__agent_hud_arm" style="cursor:pointer;padding:4px 8px;border-radius:4px;border:1px solid #475569;background:#1e293b;color:#e2e8f0;">Arm</button>
    <button type="button" id="__agent_hud_disarm" style="cursor:pointer;padding:4px 8px;border-radius:4px;border:1px solid #475569;background:#1e293b;color:#e2e8f0;">Disarm</button>
    <button type="button" id="__agent_hud_del" style="cursor:pointer;padding:4px 8px;border-radius:4px;border:1px solid #64748b;background:#334155;color:#e2e8f0;">Del last</button>
    <button type="button" id="__agent_hud_finish" style="cursor:pointer;padding:4px 8px;border-radius:4px;border:1px solid #2563eb;background:#1d4ed8;color:#fff;font-weight:600;">Finish</button>
  </div>
  <div style="margin-top:8px;display:flex;align-items:center;gap:6px;">
    <label style="font-size:11px;color:#94a3b8;">Mode</label>
    <select id="__agent_hud_mode" style="flex:1;font-size:11px;padding:3px 6px;border-radius:4px;border:1px solid #475569;background:#1e293b;color:#e2e8f0;">
      <option value="auto">auto</option>
      <option value="assert_visible">assert_visible</option>
      <option value="assert_text">assert_text</option>
    </select>
  </div>
</div>`;
    root.style.cssText = 'position:fixed;top:12px;right:12px;z-index:2147483646;margin:0;padding:0;';
    (document.body || document.documentElement).appendChild(root);

    function ctrl(payload) {
      const fn = window.__agentRecorderControl;
      if (typeof fn !== 'function') return;
      fn(payload).catch(() => {});
    }
    document.getElementById('__agent_hud_arm').addEventListener('click', (e) => {
      e.stopPropagation();
      ctrl({ action: 'arm' });
    });
    document.getElementById('__agent_hud_disarm').addEventListener('click', (e) => {
      e.stopPropagation();
      ctrl({ action: 'disarm' });
    });
    document.getElementById('__agent_hud_del').addEventListener('click', (e) => {
      e.stopPropagation();
      ctrl({ action: 'delete_last_step' });
    });
    document.getElementById('__agent_hud_finish').addEventListener('click', (e) => {
      e.stopPropagation();
      ctrl({ action: 'finish' });
    });
    document.getElementById('__agent_hud_mode').addEventListener('change', (e) => {
      e.stopPropagation();
      ctrl({ action: 'set_mode', mode: e.target.value });
    });
    window.__agentRecorderHudInstalled = true;
  }
  if (document.body) mount();
  else document.addEventListener('DOMContentLoaded', mount);
})();
""".strip()

# Ported from playwright-repo-test/lib/browser/panel-script.js (__rec_file_chooser + __recShowFileChooser) —
# adapted for agent/ dashboard-controlled recording (exposeBinding __agentFilePathSubmit).
_FILE_CHOOSER_PANEL_INIT_SCRIPT = """
(() => {
  if (window.__agentFcPanelInstalled) return;
  window.__agentFcPanelInstalled = true;

  function mount() {
    if (document.getElementById('__agent_fc_panel')) return;
    const wrap = document.createElement('div');
    wrap.id = '__agent_fc_panel';
    wrap.setAttribute('data-agent-recorder-hud', 'true');
    wrap.style.cssText =
      'display:none;position:fixed;bottom:20px;left:50%;transform:translateX(-50%);z-index:2147483646;';
    wrap.innerHTML =
      '<div style="font:12px system-ui,sans-serif;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:10px 12px;min-width:300px;max-width:min(96vw,520px);box-shadow:0 8px 32px rgba(0,0,0,0.55);">' +
      '<div id="__agent_fc_label" style="margin-bottom:8px;color:#94a3b8;font-size:11px;line-height:1.35;"></div>' +
      '<input id="__agent_fc_input" type="text" placeholder="/path/to/resume.pdf" ' +
      'style="width:100%;box-sizing:border-box;padding:8px;border-radius:6px;border:1px solid #475569;background:#020617;color:#f1f5f9;font:12px ui-monospace,Menlo,monospace;" />' +
      '<div style="margin-top:10px;display:flex;gap:8px;justify-content:flex-end;">' +
      '<button type="button" id="__agent_fc_cancel" style="cursor:pointer;padding:6px 12px;border-radius:6px;border:1px solid #64748b;background:#334155;color:#e2e8f0;">Cancel</button>' +
      '<button type="button" id="__agent_fc_set" style="cursor:pointer;padding:6px 12px;border-radius:6px;border:1px solid #166534;background:#15803d;color:#fff;font-weight:600;">Set file</button>' +
      '</div></div>';
    (document.body || document.documentElement).appendChild(wrap);

    async function submit(val) {
      if (typeof window.__agentFilePathSubmit !== 'function') return;
      await window.__agentFilePathSubmit(val);
    }
    document.getElementById('__agent_fc_set').addEventListener('click', async (ev) => {
      ev.stopPropagation();
      const v = document.getElementById('__agent_fc_input').value.trim();
      if (v) await submit(v);
    });
    document.getElementById('__agent_fc_input').addEventListener('keydown', async (ev) => {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        ev.stopPropagation();
        const v = document.getElementById('__agent_fc_input').value.trim();
        if (v) await submit(v);
      }
    });
    document.getElementById('__agent_fc_cancel').addEventListener('click', async (ev) => {
      ev.stopPropagation();
      await submit('__CANCEL__');
    });
  }

  window.__agentShowFileChooser = function (show, hint) {
    mount();
    const p = document.getElementById('__agent_fc_panel');
    if (!p) return;
    p.style.display = show ? 'block' : 'none';
    if (show) {
      const lab = document.getElementById('__agent_fc_label');
      if (lab) {
        lab.textContent =
          hint ||
          'Native file dialog — paste absolute path (file:// and %20 are fixed on the server). Then Set file.';
      }
      const inp = document.getElementById('__agent_fc_input');
      if (inp) {
        inp.value = '';
        inp.focus();
      }
    }
  };

  if (document.body) mount();
  else document.addEventListener('DOMContentLoaded', mount);
})();
""".strip()


class CapturedTarget(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    tag: str = "div"
    id: str | None = None
    class_name: str | None = Field(default=None, alias="className")
    testid: str | None = None
    text: str | None = None
    placeholder: str | None = None
    aria_label: str | None = Field(default=None, alias="ariaLabel")
    role: str | None = None
    input_type: str | None = Field(default=None, alias="inputType")
    name: str | None = None
    data_attrs: dict[str, str] = Field(default_factory=dict, alias="dataAttrs")
    sibling_index: int = Field(default=-1, alias="siblingIndex")
    parents: list[dict[str, Any]] = Field(default_factory=list)
    absolute_xpath: str | None = Field(default=None, alias="absoluteXPath")
    frame_context: list[str] = Field(default_factory=list, alias="frameContext")
    target_semantic_key: str | None = Field(default=None, alias="targetSemanticKey")


class RecorderCaptureEvent(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    seq: int = Field(ge=1)
    event_type: Literal["click", "input", "keydown"] = Field(alias="eventType")
    captured_at: str | None = Field(default=None, alias="capturedAt")
    frame_url: str | None = Field(default=None, alias="frameUrl")
    page_url: str | None = Field(default=None, alias="pageUrl")
    key: str | None = None
    code: str | None = None
    value: str | None = None
    target: CapturedTarget
    modifiers: dict[str, bool] = Field(default_factory=dict)
    associated_file_input_target: CapturedTarget | None = Field(
        default=None,
        alias="associatedFileInputTarget",
        description="Hidden or nearby input[type=file] when clicking a custom upload control.",
    )


class RecorderArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    run_id: str = Field(alias="runId")
    stepgraph_path: str = Field(alias="stepgraphPath")
    manifest_path: str = Field(alias="manifestPath")
    step_count: int = Field(alias="stepCount")
    source_url: str = Field(alias="sourceUrl")


@dataclass
class RecorderModeState:
    selected_mode: str = "auto"


def _playwright_binding_frame_url(source: Any) -> str:
    """Resolve frame URL from Playwright ``expose_binding`` first argument.

    The driver passes ``source`` as a mapping ``{context, page, frame}`` (not attributes); see
    ``BrowserContext.expose_binding`` examples in Playwright's async_api docs.
    """
    if isinstance(source, dict):
        frame = source.get("frame")
        if frame is not None:
            u = getattr(frame, "url", None)
            if isinstance(u, str) and u.strip():
                return u
        page = source.get("page")
        if page is not None:
            u = getattr(page, "url", None)
            if isinstance(u, str) and u.strip():
                return u
        return ""
    frame = getattr(source, "frame", None)
    if frame is not None:
        u = getattr(frame, "url", None)
        if isinstance(u, str):
            return u
    page = getattr(source, "page", None)
    if page is not None:
        u = getattr(page, "url", None)
        if isinstance(u, str):
            return u
    return ""


class StepGraphRecorder:
    def __init__(
        self,
        *,
        url: str,
        run_id: str | None = None,
        headless: bool = False,
        storage_state: StorageStateInput | None = None,
        poll_interval_ms: int = 250,
        browser_ui: bool = False,
        recording_armed_start: bool = False,
        dashboard_control: bool = False,
        default_record_upload_path: str | None = None,
    ) -> None:
        self._run_id = run_id or generate_run_id()
        configure_logging(self._run_id)
        self._logger = get_logger(__name__)

        self._url = url
        self._storage_state = storage_state
        self._poll_interval_ms = max(50, poll_interval_ms)
        self._browser_ui = browser_ui
        self._dashboard_control = dashboard_control
        self._recording_armed_start = recording_armed_start
        dr = (default_record_upload_path or "").strip()
        self._default_record_upload_path: str | None = dr or None
        self._pending_upload_paths: list[str] | None = None
        self._file_path_submit_future: asyncio.Future[str] | None = None
        self._recording_armed = True
        self._finish_event: asyncio.Event | None = None

        self._session = BrowserSession(headless=headless)
        self._locator_engine = LocatorEngine()
        self._mode_state = RecorderModeState()

        self._context_id: str | None = None
        self._tab_id: str | None = None
        self._page: Page | None = None
        self._started_at = datetime.now(UTC)
        self._stopped = False

        self._captures_by_seq: dict[int, RecorderCaptureEvent] = {}
        self._seen_sequences: set[int] = set()
        self._poll_task: asyncio.Task[None] | None = None
        self._synthetic_capture_seq = 900_000_000
        self._skip_capture_upload_until: float = 0.0
        self._chooser_recorded_selector: str | None = None
        self._chooser_recorded_at: float = 0.0

        self._graph = StepGraph(runId=self._run_id, steps=[], edges=[], version="1.0")

    def _bundle_from_selector(self, selector: str) -> LocatorBundle:
        return LocatorBundle(
            primarySelector=selector,
            fallbackSelectors=[],
            confidenceScore=0.85,
            reasoningHint="manual selector from control UI",
            frameContext=[],
        )

    def _ensure_recorded_upload_placeholder_file(self) -> Path:
        """Create a tiny file under this run for recorded upload steps (replace at replay time)."""
        layout = get_run_layout(self._run_id)
        run_dir = layout.run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "_recorded_upload_replace_this_file.txt"
        if not path.is_file():
            path.write_text(
                "Placeholder for a recorded file upload. Replace metadata.filePaths in stepgraph.json "
                "with your real file path(s), or overwrite this file before replay.\n",
                encoding="utf-8",
            )
        return path.resolve()

    def _normalize_upload_path_token(self, token: str) -> str | None:
        """Decode %xx (e.g. %20), expand ~, resolve when the path is an existing file."""
        raw = unquote((token or "").strip())
        if not raw:
            return None
        p = Path(raw).expanduser()
        try:
            if p.is_file():
                return str(p.resolve())
        except OSError:
            pass
        if p.exists() and not p.is_file():
            return None
        return str(p)

    def _split_upload_path_csv(self, raw: str) -> list[str]:
        parts: list[str] = []
        for chunk in raw.split(","):
            n = self._normalize_upload_path_token(chunk)
            if n:
                parts.append(n)
        return parts

    def _static_upload_path_sources(self) -> str | None:
        if self._default_record_upload_path:
            return self._default_record_upload_path
        env = (os.environ.get("AGENT_RECORD_UPLOAD_PATH") or "").strip()
        return env or None

    async def _prompt_record_upload_path_tty(self) -> str | None:
        """Mirror playwright-repo-test/recorder3.js ``ask()`` for file path (TTY only)."""
        if self._browser_ui or self._dashboard_control:
            return None
        if not sys.stdin.isatty():
            return None
        loop = asyncio.get_running_loop()
        try:
            line = await loop.run_in_executor(
                None,
                lambda: input(
                    "\n📎 File path to upload (absolute; comma-separated for multiple; Enter to skip): ",
                ).strip(),
            )
        except (EOFError, KeyboardInterrupt):
            return None
        return line or None

    async def _resolve_record_upload_paths(self) -> list[str] | None:
        if self._pending_upload_paths:
            raw_list = list(self._pending_upload_paths)
            self._pending_upload_paths = None
            out: list[str] = []
            for item in raw_list:
                n = self._normalize_upload_path_token(item)
                if n:
                    out.append(n)
            if out:
                return out
        static_src = self._static_upload_path_sources()
        if static_src:
            paths = self._split_upload_path_csv(static_src)
            if paths:
                return paths
        prompted = await self._prompt_record_upload_path_tty()
        if prompted:
            paths = self._split_upload_path_csv(prompted)
            if paths:
                return paths
        return None

    def _resolve_record_upload_paths_static_only(self) -> list[str] | None:
        """Paths for ``page.on('filechooser')`` — env / session default only (does not consume pending)."""
        static_src = self._static_upload_path_sources()
        if not static_src:
            return None
        paths = self._split_upload_path_csv(static_src)
        return paths or None

    def _next_synthetic_capture_seq(self) -> int:
        self._synthetic_capture_seq += 1
        return self._synthetic_capture_seq

    async def _target_dict_from_file_input_element(self, element: Any) -> dict[str, Any] | None:
        """DOM snapshot aligned with in-page ``collectTarget`` (playwright-repo-test ``chooser.element()``)."""
        try:
            raw = await element.evaluate(
                """(node) => {
                  if (!node || node.nodeType !== 1) return null;
                  function toXPath(n) {
                    if (!n || n.nodeType !== 1) return '';
                    if (n.id) return '//*[@id="' + n.id + '"]';
                    const parts = [];
                    let current = n;
                    while (current && current.nodeType === 1) {
                      let index = 1;
                      let sibling = current.previousElementSibling;
                      while (sibling) {
                        if (sibling.tagName === current.tagName) index += 1;
                        sibling = sibling.previousElementSibling;
                      }
                      const tag = current.tagName.toLowerCase();
                      parts.unshift(index > 1 ? tag + '[' + index + ']' : tag);
                      current = current.parentElement;
                    }
                    return '/' + parts.join('/');
                  }
                  function parentTrail(node) {
                    const out = [];
                    let current = node && node.parentElement;
                    let depth = 0;
                    while (current && current.tagName !== 'BODY' && depth < 4) {
                      out.push({
                        tag: current.tagName.toLowerCase(),
                        id: current.id || '',
                        className: typeof current.className === 'string' ? current.className : '',
                        testid:
                          current.getAttribute('data-testid') ||
                          current.getAttribute('data-test-id') ||
                          current.getAttribute('data-qa') ||
                          '',
                      });
                      current = current.parentElement;
                      depth += 1;
                    }
                    return out;
                  }
                  function dataAttrs(node) {
                    const attrs = {};
                    if (!node || !node.attributes) return attrs;
                    for (const attr of node.attributes) {
                      if (attr.name.startsWith('data-')) attrs[attr.name] = attr.value;
                    }
                    return attrs;
                  }
                  const text = (node.innerText || node.textContent || '').trim().slice(0, 120);
                  const testid =
                    node.getAttribute('data-testid') ||
                    node.getAttribute('data-test-id') ||
                    node.getAttribute('data-qa') ||
                    '';
                  return {
                    tag: node.tagName.toLowerCase(),
                    id: node.id || '',
                    className: typeof node.className === 'string' ? node.className : '',
                    testid,
                    text,
                    placeholder: node.getAttribute('placeholder') || '',
                    ariaLabel: node.getAttribute('aria-label') || '',
                    role: node.getAttribute('role') || '',
                    inputType: node.getAttribute('type') || '',
                    name: node.getAttribute('name') || '',
                    dataAttrs: dataAttrs(node),
                    siblingIndex: node.parentElement
                      ? Array.from(node.parentElement.children).indexOf(node)
                      : -1,
                    parents: parentTrail(node),
                    absoluteXPath: toXPath(node),
                    frameContext: [],
                  };
                }"""
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "recorder_filechooser_target_eval_failed",
                run_id=self._run_id,
                error=str(exc),
            )
            return None
        return raw if isinstance(raw, dict) else None

    def _normalize_file_path_panel_raw(self, raw: str) -> str:
        """Normalize pasted path (playwright-repo-test ``normalizeFilePath``) before ``Path.is_file()`` check."""
        p = (raw or "").strip()
        if p.lower().startswith("file://"):
            p = p[7:]
        try:
            p = unquote(p)
        except Exception:  # noqa: BLE001
            pass
        return str(Path(p).expanduser())

    def _consume_pending_upload_paths_for_chooser(self) -> list[str] | None:
        """Consume dashboard ``set_pending_upload_paths`` for the next file chooser (existing files only)."""
        if not self._pending_upload_paths:
            return None
        raw_list = list(self._pending_upload_paths)
        self._pending_upload_paths = None
        out: list[str] = []
        for item in raw_list:
            n = self._normalize_upload_path_token(item)
            if n:
                out.append(n)
        return out if out else None

    async def _on_file_path_binding(self, source: Any, raw: Any) -> None:
        """Receive path from in-page file chooser panel (``__agentFilePathSubmit``)."""
        fut = self._file_path_submit_future
        if fut is None or fut.done():
            return
        if raw is None:
            return
        s = str(raw).strip()
        if not s:
            return
        if s.lower() == "__cancel__":
            fut.set_result("__CANCEL__")
            return
        fut.set_result(s)

    async def _await_file_path_from_page_panel(self, page: Page, *, hint: str) -> str | None:
        """Show ported panel UI and wait for ``__agentFilePathSubmit`` (or cancel), up to 120s."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._file_path_submit_future = fut
        raw = "__CANCEL__"
        try:
            await page.evaluate(
                """(hint) => {
                  if (window.__agentShowFileChooser) window.__agentShowFileChooser(true, hint || '');
                }""",
                hint,
            )
            raw = await asyncio.wait_for(fut, timeout=120.0)
        except asyncio.TimeoutError:
            raw = "__CANCEL__"
        except asyncio.CancelledError:
            raise
        finally:
            self._file_path_submit_future = None
            try:
                await page.evaluate(
                    """() => {
                      if (window.__agentShowFileChooser) window.__agentShowFileChooser(false, '');
                    }"""
                )
            except Exception:  # noqa: BLE001
                pass
        if not isinstance(raw, str) or raw.strip().upper() == "__CANCEL__":
            return None
        return raw.strip()

    def _schedule_dashboard_filechooser(self, chooser: FileChooser) -> None:
        task = asyncio.create_task(
            self._on_dashboard_filechooser(chooser),
            name=f"recorder-filechooser-{self._run_id}",
        )

        def _done(t: asyncio.Task[None]) -> None:
            try:
                t.result()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                self._logger.exception(
                    "recorder_filechooser_task_failed",
                    run_id=self._run_id,
                    error=str(exc),
                )

        task.add_done_callback(_done)

    async def _on_dashboard_filechooser(self, chooser: FileChooser) -> None:
        """Mirror ``playwright-repo-test/recorder2.js`` ``page.on('filechooser')`` for dashboard sessions."""
        if not self._dashboard_control or self._stopped:
            return
        page = self._page
        if page is None:
            return

        dismiss_only = self._use_capture_gating() and not self._recording_armed
        placeholder = self._ensure_recorded_upload_placeholder_file()
        pending_real = None if dismiss_only else self._consume_pending_upload_paths_for_chooser()
        static_paths = self._resolve_record_upload_paths_static_only() or []
        static_real = [p for p in static_paths if Path(p).expanduser().is_file()]

        files_for_chooser: list[str]
        placeholder_meta: bool

        async def _dismiss_chooser_with_placeholder() -> None:
            try:
                await chooser.set_files([str(placeholder)], timeout=30_000)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning(
                    "recorder_filechooser_placeholder_dismiss_failed",
                    run_id=self._run_id,
                    error=str(exc),
                )

        if dismiss_only:
            files_for_chooser = [str(placeholder)]
            placeholder_meta = True
        elif pending_real:
            files_for_chooser = pending_real
            placeholder_meta = False
        elif static_real:
            files_for_chooser = static_real
            placeholder_meta = False
        else:
            hint = (
                "📁 FILE CHOOSER — paste local path (file:// and %20 auto-fixed). "
                "Cancel skips recording this upload."
            )
            chosen_raw = await self._await_file_path_from_page_panel(page, hint=hint)
            if chosen_raw is None:
                await _dismiss_chooser_with_placeholder()
                return
            normalized = self._normalize_file_path_panel_raw(chosen_raw)
            p = Path(normalized)
            if not p.is_file():
                self._logger.warning(
                    "recorder_filechooser_panel_path_not_file",
                    run_id=self._run_id,
                    path=normalized,
                )
                await _dismiss_chooser_with_placeholder()
                return
            files_for_chooser = [str(p.resolve())]
            placeholder_meta = False

        try:
            await chooser.set_files(files_for_chooser, timeout=30_000)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "recorder_filechooser_set_files_failed",
                run_id=self._run_id,
                error=str(exc),
            )
            return

        if dismiss_only:
            return

        el = chooser.element
        if el is None:
            self._logger.warning("recorder_filechooser_no_element", run_id=self._run_id)
            return

        target_dict = await self._target_dict_from_file_input_element(el)
        if not target_dict:
            return

        frame_url = ""
        try:
            frame = await el.owner_frame()
        except Exception:  # noqa: BLE001
            frame = None
        if frame is not None:
            fu = getattr(frame, "url", None)
            if isinstance(fu, str):
                frame_url = fu

        fc_page = chooser.page
        page_url = ""
        if fc_page is not None:
            pu = getattr(fc_page, "url", None)
            if isinstance(pu, str):
                page_url = pu

        seq = self._next_synthetic_capture_seq()
        now_iso = datetime.now(UTC).isoformat()
        try:
            capture = RecorderCaptureEvent(
                seq=seq,
                eventType="click",
                capturedAt=now_iso,
                frameUrl=frame_url or None,
                pageUrl=page_url or None,
                target=CapturedTarget.model_validate(target_dict),
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "recorder_filechooser_capture_invalid",
                run_id=self._run_id,
                error=str(exc),
            )
            return

        root_page = fc_page if fc_page is not None else page
        locator_bundle = await self._build_locator_bundle(root_page, capture)
        if locator_bundle is None:
            self._logger.warning("recorder_filechooser_no_bundle", run_id=self._run_id)
            return

        if not placeholder_meta:
            file_paths_meta = list(files_for_chooser)
            ph_meta: dict[str, Any] = {
                "recordedUploadPlaceholder": False,
                "recordedUploadSource": "page_filechooser",
            }
        else:
            file_paths_meta = [str(placeholder)]
            ph_meta = {
                "recordedUploadPlaceholder": True,
                "recordedUploadSource": "page_filechooser",
                "recordedUploadHint": (
                    "This step was captured from a native file chooser. Set AGENT_RECORD_UPLOAD_PATH, "
                    "session record_upload_path, or dashboard pending path before upload so replay uses real files."
                ),
            }

        self._append_upload_step_with_bundle(
            bundle=locator_bundle,
            file_paths=file_paths_meta,
            timeout_ms=30_000,
            extra_meta=ph_meta,
            page_url=page_url or None,
            frame_url=frame_url or None,
        )
        self._skip_capture_upload_until = time.monotonic() + 1.5
        self._chooser_recorded_selector = locator_bundle.primary_selector
        self._chooser_recorded_at = time.monotonic()
        self._logger.info(
            "recorder_filechooser_step_appended",
            run_id=self._run_id,
            step_id=self._graph.steps[-1].step_id if self._graph.steps else None,
        )
        await self._sync_armed_to_page()
        await self._refresh_hud()

    async def _perform_live_record_upload(
        self,
        page: Page,
        scope: Page | Frame,
        *,
        file_input_selector: str,
        trigger_selector: str | None,
        paths: list[str],
    ) -> str | None:
        """Apply files during recording (playwright-repo-test: setFiles + file chooser fallback)."""
        existing = [p for p in paths if Path(p).expanduser().is_file()]
        if not existing:
            self._logger.info(
                "recorder_upload_live_skipped_no_existing_files",
                run_id=self._run_id,
                paths=paths,
            )
            return None
        floc = scope.locator(file_input_selector).first
        try:
            await floc.set_input_files(existing, timeout=20_000)
            self._logger.info(
                "recorder_upload_live_ok",
                run_id=self._run_id,
                method="setInputFiles",
                count=len(existing),
            )
            return "setInputFiles"
        except Exception as exc:  # noqa: BLE001
            self._logger.info(
                "recorder_upload_live_set_input_files_failed",
                run_id=self._run_id,
                error=str(exc),
            )
        node: Locator = floc
        for _ in range(14):
            bucket = node.locator('input[type="file"],input[type=file]')
            if await bucket.count() > 0:
                try:
                    await bucket.first.set_input_files(existing, timeout=20_000)
                    self._logger.info(
                        "recorder_upload_live_ok",
                        run_id=self._run_id,
                        method="ancestor_file_input",
                        count=len(existing),
                    )
                    return "ancestor_file_input"
                except Exception as exc2:  # noqa: BLE001
                    self._logger.info(
                        "recorder_upload_live_ancestor_set_input_failed",
                        run_id=self._run_id,
                        error=str(exc2),
                    )
            parent = node.locator("xpath=..").first
            if await parent.count() == 0:
                break
            node = parent
        click_sel = (trigger_selector or file_input_selector or "").strip()
        if click_sel:
            tloc = scope.locator(click_sel).first
            removed_fc_listener = False
            if self._dashboard_control and page is self._page:
                try:
                    page.remove_listener("filechooser", self._schedule_dashboard_filechooser)
                    removed_fc_listener = True
                except Exception:  # noqa: BLE001
                    removed_fc_listener = False
            try:
                async with page.expect_file_chooser(timeout=12_000) as fc_info:
                    await tloc.click(timeout=8_000)
                chooser = await fc_info.value
                await chooser.set_files(existing)
                self._logger.info(
                    "recorder_upload_live_ok",
                    run_id=self._run_id,
                    method="fileChooser",
                    count=len(existing),
                )
                return "fileChooser"
            except Exception as exc:  # noqa: BLE001
                self._logger.warning(
                    "recorder_upload_live_file_chooser_failed",
                    run_id=self._run_id,
                    error=str(exc),
                )
            finally:
                if removed_fc_listener:
                    try:
                        page.on("filechooser", self._schedule_dashboard_filechooser)
                    except Exception:  # noqa: BLE001
                        pass
        return None

    def _rebuild_linear_edges(self) -> None:
        self._graph.edges.clear()
        prev: Step | None = None
        for step in self._graph.steps:
            if prev is not None:
                self._graph.edges.append(
                    StepEdge(
                        fromStepId=prev.step_id,
                        toStepId=step.step_id,
                        condition="on_success",
                    )
                )
            prev = step

    def delete_step_by_id(self, step_id: str) -> bool:
        idx = next((i for i, s in enumerate(self._graph.steps) if s.step_id == step_id), None)
        if idx is None:
            self._logger.info("recorder_delete_step_skipped", run_id=self._run_id, reason="not_found", step_id=step_id)
            return False
        self._graph.steps.pop(idx)
        self._graph.edges = [
            e for e in self._graph.edges if e.from_step_id != step_id and e.to_step_id != step_id
        ]
        self._rebuild_linear_edges()
        self._logger.info(
            "recorder_step_deleted_by_id",
            run_id=self._run_id,
            removed_step_id=step_id,
            remaining_steps=len(self._graph.steps),
        )
        return True

    def move_step(self, step_id: str, direction: Literal["up", "down"]) -> bool:
        steps = self._graph.steps
        idx = next((i for i, s in enumerate(steps) if s.step_id == step_id), None)
        if idx is None:
            return False
        if direction == "up" and idx == 0:
            return False
        if direction == "down" and idx >= len(steps) - 1:
            return False
        j = idx - 1 if direction == "up" else idx + 1
        steps[idx], steps[j] = steps[j], steps[idx]
        self._rebuild_linear_edges()
        self._logger.info(
            "recorder_step_moved",
            run_id=self._run_id,
            step_id=step_id,
            direction=direction,
        )
        return True

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def mode_state(self) -> RecorderModeState:
        return self._mode_state

    @property
    def step_graph(self) -> StepGraph:
        return self._graph

    @property
    def finish_event(self) -> asyncio.Event | None:
        return self._finish_event

    @property
    def recording_armed(self) -> bool:
        return self._recording_armed

    @property
    def dashboard_control(self) -> bool:
        return self._dashboard_control

    def _use_capture_gating(self) -> bool:
        return self._browser_ui or self._dashboard_control

    def delete_last_step(self) -> bool:
        if not self._graph.steps:
            self._logger.info("recorder_delete_last_skipped", run_id=self._run_id, reason="empty")
            return False
        removed = self._graph.steps.pop()
        rid = removed.step_id
        self._graph.edges = [e for e in self._graph.edges if e.to_step_id != rid and e.from_step_id != rid]
        self._rebuild_linear_edges()
        self._logger.info(
            "recorder_step_deleted",
            run_id=self._run_id,
            removed_step_id=rid,
            remaining_steps=len(self._graph.steps),
        )
        return True

    def set_operator_mode(self, mode: str) -> None:
        self._mode_state.selected_mode = mode.strip() or "auto"
        self._logger.info("recorder_mode_updated", run_id=self._run_id, mode=self._mode_state.selected_mode)

    async def _sync_armed_to_page(self) -> None:
        page = self._page
        if page is None:
            return
        await page.evaluate("(v) => { window.__agentRecorderArmed = !!v; }", self._recording_armed)

    async def _refresh_hud(self) -> None:
        page = self._page
        if page is None or not self._browser_ui:
            return
        status = (
            f"Capture: {'ARMED' if self._recording_armed else 'disarmed'} | "
            f"Steps: {len(self._graph.steps)} | Mode: {self._mode_state.selected_mode}"
        )
        await page.evaluate(
            """(payload) => {
              const t = document.getElementById('__agent_hud_status');
              if (t) t.textContent = payload.status;
              const sel = document.getElementById('__agent_hud_mode');
              if (sel) sel.value = payload.mode;
            }""",
            {"status": status, "mode": self._mode_state.selected_mode},
        )

    async def _on_control_binding(self, source: Any, payload: Any) -> None:
        del source  # unused
        if not self._browser_ui or not isinstance(payload, dict):
            return
        await self.apply_control_action(payload)

    async def _on_pick_binding(self, source: Any, payload: Any) -> None:
        if not self._dashboard_control or not isinstance(payload, dict):
            return
        pi = payload.get("pickIntent") if isinstance(payload.get("pickIntent"), dict) else {}
        kind = str(pi.get("kind") or "").strip()
        if kind not in ("wait_for", "assert_visible", "assert_text", "upload"):
            return
        state = str(pi.get("state") or "visible").strip() or "visible"
        timeout_ms = int(pi.get("timeoutMs") or pi.get("timeout_ms") or 30_000)
        enriched = dict(payload)
        enriched.setdefault("frameUrl", _playwright_binding_frame_url(source))
        try:
            capture = RecorderCaptureEvent.model_validate(enriched)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("recorder_pick_invalid", run_id=self._run_id, error=str(exc))
            return
        page = self._page
        tab_id = self._tab_id
        if page is None or tab_id is None:
            return
        upload_pick_skipped = False
        locator_bundle = await self._build_locator_bundle(page, capture)
        if locator_bundle is None:
            self._logger.warning("recorder_pick_no_bundle", run_id=self._run_id, sequence=capture.seq)
            return
        if kind == "wait_for":
            self._append_wait_for_step(bundle=locator_bundle, state=state, timeout_ms=timeout_ms)
        elif kind == "assert_visible":
            self._append_assert_visible_step(bundle=locator_bundle, timeout_ms=timeout_ms)
        elif kind == "assert_text":
            expected = (capture.target.text or "").strip()
            if not expected:
                self._logger.warning(
                    "recorder_pick_assert_text_skipped",
                    run_id=self._run_id,
                    reason="empty_text",
                )
                return
            contains = bool(pi.get("contains", True))
            self._append_assert_text_step(
                bundle=locator_bundle,
                expected=expected,
                contains=contains,
                timeout_ms=timeout_ms,
            )
        elif kind == "upload":
            primary_sel = locator_bundle.primary_selector
            if (
                self._chooser_recorded_selector
                and primary_sel == self._chooser_recorded_selector
                and (time.monotonic() - self._chooser_recorded_at) < 1.0
            ):
                upload_pick_skipped = True
                self._logger.info(
                    "recorder_pick_upload_skipped_dedupe_filechooser",
                    run_id=self._run_id,
                    selector=primary_sel,
                )
            else:
                scope = self._resolve_scope(page, capture.frame_url)
                resolved_paths = await self._resolve_record_upload_paths()
                placeholder = self._ensure_recorded_upload_placeholder_file()
                if resolved_paths:
                    file_paths_meta = list(resolved_paths)
                    ph_meta: dict[str, Any] = {"recordedUploadPlaceholder": False}
                else:
                    file_paths_meta = [str(placeholder)]
                    ph_meta = {
                        "recordedUploadPlaceholder": True,
                        "recordedUploadHint": (
                            "Set AGENT_RECORD_UPLOAD_PATH, session record_upload_path, or --upload-path when recording "
                            "so this step stores real file path(s)."
                        ),
                    }
                primary = locator_bundle.primary_selector
                live_method: str | None = None
                if resolved_paths:
                    try:
                        live_method = await self._perform_live_record_upload(
                            page,
                            scope,
                            file_input_selector=primary,
                            trigger_selector=primary,
                            paths=file_paths_meta,
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._logger.warning(
                            "recorder_pick_upload_live_failed",
                            run_id=self._run_id,
                            error=str(exc),
                        )
                if live_method:
                    ph_meta["recordedUploadLiveMethod"] = live_method
                pu = None
                try:
                    pu = page.url
                except Exception:  # noqa: BLE001
                    pu = None
                self._append_upload_step_with_bundle(
                    bundle=locator_bundle,
                    file_paths=file_paths_meta,
                    timeout_ms=timeout_ms,
                    extra_meta=ph_meta,
                    page_url=pu,
                    frame_url=pu,
                )
        if not (kind == "upload" and upload_pick_skipped):
            self._logger.info("recorder_pick_appended", run_id=self._run_id, kind=kind)
        await self._sync_armed_to_page()
        await self._refresh_hud()

    async def read_pick_state(self) -> dict[str, Any]:
        page = self._page
        if page is None:
            return {"pickPending": False, "pickIntent": None}
        try:
            intent = await page.evaluate(
                """() => {
                  const p = window.__agentPickIntent;
                  if (!p || !p.kind) return null;
                  return {
                    kind: p.kind,
                    state: p.state || 'visible',
                    timeoutMs: p.timeoutMs || 30000,
                    contains: p.contains !== false,
                  };
                }"""
            )
        except Exception:  # noqa: BLE001
            return {"pickPending": False, "pickIntent": None}
        if intent is None:
            return {"pickPending": False, "pickIntent": None}
        return {"pickPending": True, "pickIntent": intent}

    async def apply_control_action(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        action = str(payload.get("action") or "").strip().lower()
        if action == "arm":
            self._recording_armed = True
        elif action == "disarm":
            self._recording_armed = False
        elif action == "toggle_arm":
            self._recording_armed = not self._recording_armed
        elif action == "set_mode":
            raw = str(payload.get("mode") or "auto").strip()
            if raw in RECORDER_OPERATOR_MODES:
                self.set_operator_mode(raw)
        elif action == "delete_last_step":
            self.delete_last_step()
        elif action == "delete_step":
            step_id = str(payload.get("stepId") or payload.get("step_id") or "").strip()
            if step_id:
                self.delete_step_by_id(step_id)
        elif action == "move_step_up":
            step_id = str(payload.get("stepId") or payload.get("step_id") or "").strip()
            if step_id:
                self.move_step(step_id, "up")
        elif action == "move_step_down":
            step_id = str(payload.get("stepId") or payload.get("step_id") or "").strip()
            if step_id:
                self.move_step(step_id, "down")
        elif action == "begin_pick":
            kind = str(payload.get("kind") or "").strip()
            if kind not in ("wait_for", "assert_visible", "assert_text", "upload"):
                self._logger.warning("recorder_begin_pick_bad_kind", run_id=self._run_id, kind=kind)
            else:
                page = self._page
                if page is None:
                    self._logger.warning("recorder_begin_pick_no_page", run_id=self._run_id)
                else:
                    state = str(payload.get("state") or "visible").strip() or "visible"
                    timeout_ms = int(payload.get("timeoutMs") or payload.get("timeout_ms") or 30_000)
                    contains = bool(payload.get("contains", True))
                    await page.evaluate(
                        """(intent) => { window.__agentPickIntent = intent; }""",
                        {"kind": kind, "state": state, "timeoutMs": timeout_ms, "contains": contains},
                    )
        elif action == "cancel_pick":
            self._pending_upload_paths = None
            page = self._page
            if page is not None:
                try:
                    await page.evaluate(
                        """() => {
                          if (typeof window.__agentPickCancelUi === 'function') window.__agentPickCancelUi();
                          else { window.__agentPickIntent = null; }
                        }"""
                    )
                except Exception:  # noqa: BLE001
                    pass
        elif action == "set_pending_upload_paths":
            raw = payload.get("filePaths") or payload.get("file_paths") or payload.get("paths")
            paths: list[str] = []
            if isinstance(raw, list):
                paths = [str(x).strip() for x in raw if isinstance(x, str) and str(x).strip()]
            elif isinstance(raw, str) and raw.strip():
                paths = [raw.strip()]
            self._pending_upload_paths = paths or None
            self._logger.info(
                "recorder_pending_upload_paths_set",
                run_id=self._run_id,
                count=len(paths) if paths else 0,
            )
        elif action == "add_wait_for":
            selector = str(payload.get("selector") or "").strip()
            if not selector:
                self._logger.warning("recorder_add_wait_for_skipped", run_id=self._run_id, reason="empty_selector")
            else:
                state = str(payload.get("state") or "visible").strip() or "visible"
                timeout_ms = int(payload.get("timeoutMs") or payload.get("timeout_ms") or 30_000)
                self._append_wait_for_step(
                    bundle=self._bundle_from_selector(selector),
                    state=state,
                    timeout_ms=timeout_ms,
                )
        elif action == "add_assert_url":
            expected = str(payload.get("expected") or "").strip()
            if not expected:
                self._logger.warning("recorder_add_assert_url_skipped", run_id=self._run_id, reason="empty_expected")
            else:
                contains = bool(payload.get("contains", True))
                self._append_assert_url_step(expected=expected, contains=contains)
        elif action == "add_navigate":
            url = str(payload.get("url") or "").strip()
            if not url:
                self._logger.warning("recorder_add_navigate_skipped", run_id=self._run_id, reason="empty_url")
            else:
                timeout_ms = int(payload.get("timeoutMs") or payload.get("timeout_ms") or 30_000)
                self._append_navigate_step(url=url, timeout_ms=timeout_ms)
        elif action == "add_assert_title":
            expected = str(payload.get("expected") or "").strip()
            if not expected:
                self._logger.warning("recorder_add_assert_title_skipped", run_id=self._run_id, reason="empty_expected")
            else:
                contains = bool(payload.get("contains", True))
                self._append_assert_title_step(expected=expected, contains=contains)
        elif action == "add_wait_timeout":
            timeout_ms = int(payload.get("timeoutMs") or payload.get("timeout_ms") or 1000)
            self._append_wait_timeout_step(timeout_ms=max(1, timeout_ms))
        elif action == "add_frame_enter":
            selector = str(payload.get("selector") or "").strip()
            if not selector:
                self._logger.warning("recorder_add_frame_enter_skipped", run_id=self._run_id, reason="empty_selector")
            else:
                timeout_ms = int(payload.get("timeoutMs") or payload.get("timeout_ms") or 30_000)
                self._append_frame_enter_step(selector=selector, timeout_ms=timeout_ms)
        elif action == "add_frame_exit":
            self._append_frame_exit_step()
        elif action == "add_upload":
            selector = str(payload.get("selector") or "").strip()
            raw_paths = payload.get("filePaths") or payload.get("file_paths") or payload.get("paths")
            paths: str | list[str]
            if isinstance(raw_paths, list) and raw_paths and all(isinstance(p, str) and p.strip() for p in raw_paths):
                paths = [p.strip() for p in raw_paths]
            elif isinstance(raw_paths, str) and raw_paths.strip():
                paths = raw_paths.strip()
            else:
                paths = []
            if not selector or not paths:
                self._logger.warning(
                    "recorder_add_upload_skipped",
                    run_id=self._run_id,
                    reason="missing_selector_or_paths",
                )
            else:
                timeout_ms = int(payload.get("timeoutMs") or payload.get("timeout_ms") or 30_000)
                self._append_upload_step(selector=selector, file_paths=paths, timeout_ms=timeout_ms)
        elif action == "add_dialog_handle":
            accept = bool(payload.get("accept", True))
            prompt_raw = payload.get("promptText") or payload.get("prompt_text")
            prompt_text = str(prompt_raw).strip() if isinstance(prompt_raw, str) else None
            timeout_ms = int(payload.get("timeoutMs") or payload.get("timeout_ms") or 15_000)
            self._append_dialog_handle_step(
                accept=accept,
                prompt_text=prompt_text or None,
                timeout_ms=timeout_ms,
            )
        elif action == "finish":
            if self._finish_event is not None and not self._finish_event.is_set():
                self._finish_event.set()
                self._logger.info("recorder_finish_requested", run_id=self._run_id)
        else:
            self._logger.warning("recorder_control_unknown_action", run_id=self._run_id, action=action)
            return

        await self._sync_armed_to_page()
        await self._refresh_hud()

    def _append_wait_for_step(self, *, bundle: LocatorBundle, state: str, timeout_ms: int) -> None:
        sel = bundle.primary_selector
        step = Step(
            mode=StepMode.ACTION,
            action="wait_for",
            target=bundle,
            metadata={"state": state, "target": sel},
            timeout_policy=TimeoutPolicy(timeoutMs=timeout_ms),
        )
        self._append_step(step)
        self._logger.info(
            "recorder_step_appended",
            run_id=self._run_id,
            action="wait_for",
            step_id=step.step_id,
        )

    def _append_assert_visible_step(self, *, bundle: LocatorBundle, timeout_ms: int) -> None:
        step = Step(
            mode=StepMode.ASSERTION,
            action="assert_visible",
            target=bundle,
            metadata={},
            timeout_policy=TimeoutPolicy(timeoutMs=timeout_ms),
        )
        self._append_step(step)
        self._logger.info(
            "recorder_step_appended",
            run_id=self._run_id,
            action="assert_visible",
            step_id=step.step_id,
        )

    def _append_assert_text_step(
        self,
        *,
        bundle: LocatorBundle,
        expected: str,
        contains: bool,
        timeout_ms: int,
    ) -> None:
        step = Step(
            mode=StepMode.ASSERTION,
            action="assert_text",
            target=bundle,
            metadata={"expected": expected, "contains": contains},
            timeout_policy=TimeoutPolicy(timeoutMs=timeout_ms),
        )
        self._append_step(step)
        self._logger.info(
            "recorder_step_appended",
            run_id=self._run_id,
            action="assert_text",
            step_id=step.step_id,
        )

    def _append_navigate_step(self, *, url: str, timeout_ms: int) -> None:
        step = Step(
            mode=StepMode.NAVIGATION,
            action="navigate",
            target=None,
            metadata={"url": url},
            timeout_policy=TimeoutPolicy(timeoutMs=timeout_ms),
        )
        self._append_step(step)
        self._logger.info(
            "recorder_step_appended",
            run_id=self._run_id,
            action="navigate",
            step_id=step.step_id,
        )

    def _append_assert_title_step(self, *, expected: str, contains: bool) -> None:
        step = Step(
            mode=StepMode.ASSERTION,
            action="assert_title",
            target=None,
            metadata={"expected": expected, "contains": contains},
            timeout_policy=TimeoutPolicy(timeoutMs=15_000),
        )
        self._append_step(step)
        self._logger.info(
            "recorder_step_appended",
            run_id=self._run_id,
            action="assert_title",
            step_id=step.step_id,
        )

    def _append_wait_timeout_step(self, *, timeout_ms: int) -> None:
        step = Step(
            mode=StepMode.ACTION,
            action="wait_timeout",
            target=None,
            metadata={"timeoutMs": timeout_ms},
            timeout_policy=TimeoutPolicy(timeoutMs=max(timeout_ms, 1)),
        )
        self._append_step(step)
        self._logger.info(
            "recorder_step_appended",
            run_id=self._run_id,
            action="wait_timeout",
            step_id=step.step_id,
        )

    def _append_frame_enter_step(self, *, selector: str, timeout_ms: int) -> None:
        bundle = self._bundle_from_selector(selector)
        step = Step(
            mode=StepMode.ACTION,
            action="frame_enter",
            target=bundle,
            metadata={"target": selector},
            timeout_policy=TimeoutPolicy(timeoutMs=timeout_ms),
        )
        self._append_step(step)
        self._logger.info(
            "recorder_step_appended",
            run_id=self._run_id,
            action="frame_enter",
            step_id=step.step_id,
        )

    def _append_frame_exit_step(self) -> None:
        step = Step(
            mode=StepMode.ACTION,
            action="frame_exit",
            target=None,
            metadata={},
            timeout_policy=TimeoutPolicy(timeoutMs=15_000),
        )
        self._append_step(step)
        self._logger.info(
            "recorder_step_appended",
            run_id=self._run_id,
            action="frame_exit",
            step_id=step.step_id,
        )

    def _append_upload_step_with_bundle(
        self,
        *,
        bundle: LocatorBundle,
        file_paths: str | list[str],
        timeout_ms: int,
        extra_meta: dict[str, Any] | None = None,
        page_url: str | None = None,
        frame_url: str | None = None,
    ) -> None:
        meta: dict[str, Any] = {"filePaths": file_paths}
        if extra_meta:
            meta.update(extra_meta)
        u = (page_url or "").strip()
        f = (frame_url or "").strip()
        if u.startswith(("http://", "https://")):
            meta.setdefault("pageUrl", u)
        if f.startswith(("http://", "https://")):
            meta.setdefault("frameUrl", f)
        elif u.startswith(("http://", "https://")):
            meta.setdefault("frameUrl", u)
        step = Step(
            mode=StepMode.ACTION,
            action="upload",
            target=bundle,
            metadata=meta,
            timeout_policy=TimeoutPolicy(timeoutMs=timeout_ms),
        )
        self._append_step(step)
        self._logger.info(
            "recorder_step_appended",
            run_id=self._run_id,
            action="upload",
            step_id=step.step_id,
        )

    def _append_upload_step(
        self,
        *,
        selector: str,
        file_paths: str | list[str],
        timeout_ms: int,
    ) -> None:
        pu: str | None = None
        if self._page is not None:
            try:
                pu = self._page.url
            except Exception:  # noqa: BLE001
                pu = None
        self._append_upload_step_with_bundle(
            bundle=self._bundle_from_selector(selector),
            file_paths=file_paths,
            timeout_ms=timeout_ms,
            page_url=pu,
            frame_url=pu,
        )

    def _append_dialog_handle_step(
        self,
        *,
        accept: bool,
        prompt_text: str | None,
        timeout_ms: int,
    ) -> None:
        meta: dict[str, Any] = {"accept": accept}
        if prompt_text:
            meta["promptText"] = prompt_text
        body_bundle = LocatorBundle(
            primarySelector="body",
            fallbackSelectors=[],
            confidenceScore=0.5,
            reasoningHint="placeholder target for dialog_handle (page-level)",
            frameContext=[],
        )
        step = Step(
            mode=StepMode.ACTION,
            action="dialog_handle",
            target=body_bundle,
            metadata=meta,
            timeout_policy=TimeoutPolicy(timeoutMs=timeout_ms),
        )
        self._append_step(step)
        self._logger.info(
            "recorder_step_appended",
            run_id=self._run_id,
            action="dialog_handle",
            step_id=step.step_id,
        )

    def _append_assert_url_step(self, *, expected: str, contains: bool) -> None:
        step = Step(
            mode=StepMode.ASSERTION,
            action="assert_url",
            target=None,
            metadata={"expected": expected, "contains": contains},
            timeout_policy=TimeoutPolicy(timeoutMs=15_000),
        )
        self._append_step(step)
        self._logger.info(
            "recorder_step_appended",
            run_id=self._run_id,
            action="assert_url",
            step_id=step.step_id,
        )

    async def start(self) -> None:
        self._finish_event = asyncio.Event()
        if self._use_capture_gating():
            self._recording_armed = self._recording_armed_start
        else:
            self._recording_armed = True

        await self._session.start()
        self._context_id, context = await self._session.new_context(storage_state=self._storage_state)
        page = await context.new_page()
        self._page = page
        self._tab_id = self._session.get_tab_id(page)
        if self._tab_id is None:
            raise RuntimeError("Failed to resolve tab id for recorder page.")

        await context.expose_binding("__agentRecordEmit", self._on_capture_binding)
        await context.add_init_script(_CAPTURE_QUEUE_INIT_SCRIPT)
        if self._dashboard_control:
            await context.add_init_script(_FILE_CHOOSER_PANEL_INIT_SCRIPT)
            await context.expose_binding("__agentFilePathSubmit", self._on_file_path_binding)
            await context.expose_binding("__agentPickEmit", self._on_pick_binding)
        if self._browser_ui:
            await context.expose_binding("__agentRecorderControl", self._on_control_binding)
            await context.add_init_script(_RECORDER_HUD_INIT_SCRIPT)

        page.on("framenavigated", self._on_frame_navigated)
        page.on("console", self._on_console)
        if self._dashboard_control:
            page.on("filechooser", self._schedule_dashboard_filechooser)

        await page.goto(self._url, wait_until="domcontentloaded", timeout=15_000)
        await page.wait_for_function("window.__agentRecorderInstalled === true", timeout=15_000)
        await self._sync_armed_to_page()
        if self._browser_ui:
            await page.wait_for_selector("#__agent_recorder_hud_root", timeout=15_000)
            await self._refresh_hud()

        self._poll_task = asyncio.create_task(self._poll_inpage_queue(), name=f"recorder-poll-{self._run_id}")

        self._logger.info(
            "recorder_started",
            run_id=self._run_id,
            url=self._url,
            context_id=self._context_id,
            tab_id=self._tab_id,
            browser_ui=self._browser_ui,
            dashboard_control=self._dashboard_control,
            recording_armed=self._recording_armed,
        )

    def _on_frame_navigated(self, frame: Frame) -> None:
        self._logger.info(
            "recorder_frame_navigated",
            run_id=self._run_id,
            is_main_frame=bool(self._page and frame == self._page.main_frame),
            frame_url=frame.url,
        )

    def _on_console(self, message: Any) -> None:
        self._logger.info(
            "recorder_console_message",
            run_id=self._run_id,
            message_type=getattr(message, "type", "unknown"),
            text=getattr(message, "text", ""),
        )

    async def stop(self) -> RecorderArtifact:
        if self._stopped:
            return self._build_artifact()
        self._stopped = True

        try:
            fut = self._file_path_submit_future
            if fut is not None and not fut.done():
                fut.cancel()
            self._file_path_submit_future = None
            if self._dashboard_control and self._page is not None:
                try:
                    self._page.remove_listener("filechooser", self._schedule_dashboard_filechooser)
                except Exception:  # noqa: BLE001
                    pass
            await self._flush_inpage_queue()
            await self._process_new_sequences()

            if self._poll_task is not None:
                self._poll_task.cancel()
                try:
                    await self._poll_task
                except asyncio.CancelledError:
                    pass
                self._poll_task = None

            return self._write_artifacts()
        finally:
            await self._session.stop()
            self._logger.info("recorder_stopped", run_id=self._run_id, step_count=len(self._graph.steps))

    async def _poll_inpage_queue(self) -> None:
        while True:
            await self._flush_inpage_queue()
            await self._process_new_sequences()
            await asyncio.sleep(self._poll_interval_ms / 1000)

    async def _on_capture_binding(self, source: Any, payload: dict[str, Any]) -> None:
        enriched = dict(payload)
        enriched.setdefault("frameUrl", _playwright_binding_frame_url(source))
        await self._ingest_capture(enriched)
        await self._process_new_sequences()

    async def _flush_inpage_queue(self) -> None:
        page = self._page
        if page is None:
            return
        for attempt in range(2):
            try:
                entries = await asyncio.wait_for(
                    page.evaluate("window.__agentRecorderDrain ? window.__agentRecorderDrain() : []"),
                    timeout=2.0,
                )
                break
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                if attempt == 0 and "Execution context was destroyed" in message:
                    await asyncio.sleep(0.1)
                    continue
                self._logger.warning(
                    "recorder_queue_flush_failed",
                    run_id=self._run_id,
                    error=message,
                )
                return

        if not isinstance(entries, list):
            return
        for entry in entries:
            if isinstance(entry, dict):
                await self._ingest_capture(entry)

    async def _ingest_capture(self, payload: dict[str, Any]) -> None:
        try:
            capture = RecorderCaptureEvent.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("recorder_capture_invalid", run_id=self._run_id, error=str(exc))
            return

        if capture.seq in self._seen_sequences:
            return
        self._seen_sequences.add(capture.seq)
        self._captures_by_seq[capture.seq] = capture

    async def _process_new_sequences(self) -> None:
        pending = sorted(self._captures_by_seq.items(), key=lambda item: item[0])
        if not pending:
            return
        self._captures_by_seq.clear()

        for _, capture in pending:
            await self._capture_to_step(capture)

    async def _capture_to_step(self, capture: RecorderCaptureEvent) -> None:
        intent = self._resolve_intent(capture)
        if intent is None:
            return

        page = self._page
        tab_id = self._tab_id
        if page is None or tab_id is None:
            return

        metadata: dict[str, Any] = {
            "tabId": tab_id,
            "capturedSeq": capture.seq,
            "capturedAt": capture.captured_at,
            "sourceEventType": capture.event_type,
            "semanticIntent": intent["semantic_intent"],
            "intentConfidence": intent["confidence"],
            "operatorMode": self._mode_state.selected_mode,
            "frameUrl": capture.frame_url,
            "pageUrl": capture.page_url,
            **intent["metadata"],
        }

        if intent["action"] == "upload":
            if self._dashboard_control and time.monotonic() < self._skip_capture_upload_until:
                self._logger.info(
                    "recorder_upload_capture_skipped_after_filechooser",
                    run_id=self._run_id,
                    sequence=capture.seq,
                )
                return
            bundle_target = capture.associated_file_input_target or capture.target
            upload_capture = capture.model_copy(
                update={"target": bundle_target, "associated_file_input_target": None}
            )
            locator_bundle = await self._build_locator_bundle(page, upload_capture)
            if locator_bundle is None:
                self._logger.warning(
                    "recorder_upload_auto_skipped_no_bundle",
                    run_id=self._run_id,
                    sequence=capture.seq,
                )
                return
            scope = self._resolve_scope(page, capture.frame_url)
            resolved_paths = await self._resolve_record_upload_paths()
            placeholder = self._ensure_recorded_upload_placeholder_file()
            if resolved_paths:
                file_paths_meta: list[str] = list(resolved_paths)
                ph_meta: dict[str, Any] = {"recordedUploadPlaceholder": False}
            else:
                file_paths_meta = [str(placeholder)]
                ph_meta = {
                    "recordedUploadPlaceholder": True,
                    "recordedUploadHint": (
                        "Set AGENT_RECORD_UPLOAD_PATH, pass --upload-path to agent record, or use the dashboard "
                        "record_upload_path when starting a session. Replay uses setInputFiles on the file input "
                        "locator (or overwrite _recorded_upload_replace_this_file.txt)."
                    ),
                }
            live_method: str | None = None
            if resolved_paths:
                trigger_sel: str | None = None
                if capture.associated_file_input_target is not None:
                    trig_cap = capture.model_copy(update={"associated_file_input_target": None})
                    trig_bundle = await self._build_locator_bundle(page, trig_cap)
                    if trig_bundle is not None:
                        trigger_sel = trig_bundle.primary_selector
                try:
                    live_method = await self._perform_live_record_upload(
                        page,
                        scope,
                        file_input_selector=locator_bundle.primary_selector,
                        trigger_selector=trigger_sel,
                        paths=file_paths_meta,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._logger.warning(
                        "recorder_capture_upload_live_failed",
                        run_id=self._run_id,
                        sequence=capture.seq,
                        error=str(exc),
                    )
            meta = {
                **metadata,
                "filePaths": file_paths_meta,
                **ph_meta,
            }
            if live_method:
                meta["recordedUploadLiveMethod"] = live_method
            step = Step(
                mode=StepMode.ACTION,
                action="upload",
                target=locator_bundle,
                metadata=meta,
                timeout_policy=TimeoutPolicy(timeoutMs=30_000),
            )
            self._append_step(step)
            self._logger.info(
                "recorder_step_captured",
                run_id=self._run_id,
                step_id=step.step_id,
                action=step.action,
                sequence=capture.seq,
            )
            if self._browser_ui:
                await self._refresh_hud()
            return

        locator_bundle = await self._build_locator_bundle(page, capture)

        if intent["action"] in {"click", "fill", "type", "press", "assert_visible", "assert_text"}:
            step = Step(
                mode=StepMode.ACTION if not intent["action"].startswith("assert") else StepMode.ASSERTION,
                action=intent["action"],
                target=locator_bundle,
                metadata=metadata,
            )
        else:
            return

        if self._should_coalesce_fill(step):
            self._coalesce_fill(step)
            return

        self._append_step(step)
        self._logger.info(
            "recorder_step_captured",
            run_id=self._run_id,
            step_id=step.step_id,
            action=step.action,
            sequence=capture.seq,
        )
        if self._browser_ui:
            await self._refresh_hud()

    async def _build_locator_bundle(self, page: Page, capture: RecorderCaptureEvent) -> LocatorBundle | None:
        descriptor = capture.target.model_dump(by_alias=True, exclude_none=True)
        scope: Page | Frame = self._resolve_scope(page, capture.frame_url)
        try:
            return await self._locator_engine.build(scope, descriptor, force=True)
        except Exception as exc:  # noqa: BLE001
            xpath = capture.target.absolute_xpath
            if isinstance(xpath, str) and xpath.strip():
                return LocatorBundle(
                    primarySelector=f"xpath={xpath}",
                    fallbackSelectors=[],
                    confidenceScore=0.2,
                    reasoningHint=f"fallback absolute xpath due to locator build error: {exc}",
                    frameContext=list(capture.target.frame_context),
                )
            self._logger.warning(
                "recorder_locator_bundle_failed",
                run_id=self._run_id,
                sequence=capture.seq,
                error=str(exc),
            )
            return None

    def _resolve_scope(self, page: Page, frame_url: str | None) -> Page | Frame:
        if not frame_url:
            return page
        for frame in page.frames:
            if frame.url == frame_url:
                return frame
        return page

    def _resolve_intent(self, capture: RecorderCaptureEvent) -> dict[str, Any] | None:
        mode = self._mode_state.selected_mode
        target = capture.target

        if capture.event_type == "click":
            if capture.associated_file_input_target is not None:
                return {
                    "action": "upload",
                    "semantic_intent": "file_upload_custom_trigger",
                    "confidence": 0.92,
                    "metadata": {},
                }
            if target.tag.lower() == "input" and (target.input_type or "").lower() == "file":
                return {
                    "action": "upload",
                    "semantic_intent": "file_upload_native_input",
                    "confidence": 0.95,
                    "metadata": {},
                }

        if mode == "assert_visible" and capture.event_type == "click":
            return {
                "action": "assert_visible",
                "semantic_intent": "operator_assert_visible",
                "confidence": 0.95,
                "metadata": {},
            }
        if mode == "assert_text" and capture.event_type == "click":
            expected = (target.text or "").strip()
            if expected:
                return {
                    "action": "assert_text",
                    "semantic_intent": "operator_assert_text",
                    "confidence": 0.9,
                    "metadata": {"expected": expected, "contains": True},
                }

        if capture.event_type == "input":
            if target.tag.lower() == "input" and (target.input_type or "").lower() == "file":
                return None
            value = capture.value or ""
            if self._is_sensitive_input(capture.target):
                return {
                    "action": "fill",
                    "semantic_intent": "field_input_redacted",
                    "confidence": 0.9,
                    "metadata": {"valueRef": "redacted"},
                }
            return {
                "action": "fill",
                "semantic_intent": "field_input",
                "confidence": 0.9,
                "metadata": {"text": value},
            }

        if capture.event_type == "keydown" and (capture.key or "").lower() == "enter":
            semantic_intent = "submit_form"
            if self._looks_like_search_target(target):
                semantic_intent = "search_submit"
            return {
                "action": "press",
                "semantic_intent": semantic_intent,
                "confidence": 0.75,
                "metadata": {"key": "Enter"},
            }

        if capture.event_type == "click":
            semantic_intent = "click"
            lowered_text = (target.text or "").strip().lower()
            if target.tag == "button" and any(token in lowered_text for token in ("submit", "save", "search")):
                semantic_intent = "submit_button_click"
            return {
                "action": "click",
                "semantic_intent": semantic_intent,
                "confidence": 0.8,
                "metadata": {},
            }

        return None

    def _is_sensitive_input(self, target: CapturedTarget) -> bool:
        input_type = (target.input_type or "").lower().strip()
        if input_type == "password":
            return True
        combined = " ".join(
            token
            for token in [
                target.id or "",
                target.name or "",
                target.placeholder or "",
                target.aria_label or "",
                target.target_semantic_key or "",
            ]
            if token
        ).lower()
        return "password" in combined or "passcode" in combined

    def _looks_like_search_target(self, target: CapturedTarget) -> bool:
        input_type = (target.input_type or "").lower()
        if input_type == "search":
            return True
        search_hints = " ".join(filter(None, [target.placeholder, target.aria_label, target.name, target.text])).lower()
        return "search" in search_hints

    def _append_step(self, step: Step) -> None:
        previous_step = self._graph.steps[-1] if self._graph.steps else None
        self._graph.steps.append(step)
        if previous_step is not None:
            self._graph.edges.append(
                StepEdge(
                    fromStepId=previous_step.step_id,
                    toStepId=step.step_id,
                    condition="on_success",
                )
            )

    def _should_coalesce_fill(self, incoming: Step) -> bool:
        if incoming.action != "fill" or not self._graph.steps:
            return False
        latest = self._graph.steps[-1]
        if latest.action != "fill":
            return False
        if latest.target is None or incoming.target is None:
            return False
        return latest.target.primary_selector == incoming.target.primary_selector

    def _coalesce_fill(self, incoming: Step) -> None:
        latest = self._graph.steps[-1]
        latest.metadata.update(incoming.metadata)
        self._logger.info(
            "recorder_fill_coalesced",
            run_id=self._run_id,
            step_id=latest.step_id,
            target=latest.target.primary_selector if latest.target else None,
        )

    def _write_artifacts(self) -> RecorderArtifact:
        layout = get_run_layout(self._run_id)
        stepgraph_path = layout.run_dir / "stepgraph.json"
        stepgraph_path.write_text(self._graph.model_dump_json(indent=2, by_alias=True), encoding="utf-8")

        manifest_payload = {
            "runId": self._run_id,
            "sourceUrl": self._url,
            "recordedAt": datetime.now(UTC).isoformat(),
            "startedAt": self._started_at.isoformat(),
            "stepCount": len(self._graph.steps),
            "edgeCount": len(self._graph.edges),
            "tabId": self._tab_id,
            "contextId": self._context_id,
            "artifacts": {
                "stepgraph": str(stepgraph_path),
                "log": str(layout.log_jsonl),
                "manifest": str(layout.manifest_json),
            },
            "notes": {
                "captureModel": "in-page queue + binding fast-path",
                "intentModel": "heuristic + operator mode state",
            },
        }
        layout.manifest_json.write_text(json.dumps(manifest_payload, indent=2), encoding="utf-8")

        self._logger.info(
            "recorder_artifacts_written",
            run_id=self._run_id,
            stepgraph_path=str(stepgraph_path),
            manifest_path=str(layout.manifest_json),
            step_count=len(self._graph.steps),
        )
        return self._build_artifact()

    def _build_artifact(self) -> RecorderArtifact:
        layout = get_run_layout(self._run_id)
        return RecorderArtifact(
            runId=self._run_id,
            stepgraphPath=str(layout.run_dir / "stepgraph.json"),
            manifestPath=str(layout.manifest_json),
            stepCount=len(self._graph.steps),
            sourceUrl=self._url,
        )
