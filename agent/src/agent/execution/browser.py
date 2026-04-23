from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from playwright.async_api import Browser, BrowserContext, Frame, Page, Playwright, async_playwright

from agent.core.ids import (
    generate_browser_context_id,
    generate_browser_session_id,
    generate_frame_id,
    generate_tab_id,
)
from agent.core.logging import get_logger

StorageStateInput = str | Path | Mapping[str, Any]


class BrowserSessionError(RuntimeError):
    pass


class BrowserSession:
    def __init__(
        self,
        *,
        browser_name: str = "chromium",
        headless: bool = True,
        launch_options: Mapping[str, Any] | None = None,
    ) -> None:
        self._logger = get_logger(__name__)
        self._browser_session_id = generate_browser_session_id()
        self._browser_name = browser_name
        self._headless = headless
        self._launch_options = dict(launch_options or {})

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

        self._contexts_by_id: dict[str, BrowserContext] = {}
        self._context_ids_by_obj: dict[int, str] = {}

        self._tabs_by_id: dict[str, Page] = {}
        self._tab_ids_by_obj: dict[int, str] = {}
        self._tab_context_id: dict[str, str] = {}

        self._frames_by_id: dict[str, Frame] = {}
        self._frame_ids_by_obj: dict[int, str] = {}
        self._frame_tab_id: dict[str, str] = {}
        self._frame_parent_id: dict[str, str | None] = {}
        self._frame_children: dict[str, set[str]] = {}

    @property
    def browser_session_id(self) -> str:
        return self._browser_session_id

    @property
    def is_started(self) -> bool:
        return self._browser is not None

    async def start(self) -> str:
        if self._browser is not None:
            return self._browser_session_id

        self._playwright = await async_playwright().start()
        browser_type = getattr(self._playwright, self._browser_name, None)
        if browser_type is None:
            await self._playwright.stop()
            self._playwright = None
            msg = f"Unsupported browser type: {self._browser_name}"
            raise BrowserSessionError(msg)

        try:
            self._browser = await browser_type.launch(
                headless=self._headless,
                **self._launch_options,
            )
        except Exception:
            await self._playwright.stop()
            self._playwright = None
            raise

        self._logger.info(
            "browser_session_started",
            browser_session_id=self._browser_session_id,
            browser_name=self._browser_name,
            headless=self._headless,
        )
        return self._browser_session_id

    async def stop(self) -> None:
        context_ids = list(self._contexts_by_id.keys())
        for context_id in context_ids:
            context = self._contexts_by_id.get(context_id)
            if context is None:
                continue
            try:
                await context.close()
            except Exception as exc:
                self._logger.warning(
                    "browser_context_close_failed",
                    browser_session_id=self._browser_session_id,
                    context_id=context_id,
                    error=str(exc),
                )

        if self._browser is not None:
            try:
                await self._browser.close()
            finally:
                self._browser = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            finally:
                self._playwright = None

        self._clear_tracking()
        self._logger.info(
            "browser_session_stopped",
            browser_session_id=self._browser_session_id,
        )

    async def new_context(
        self,
        *,
        storage_state: StorageStateInput | None = None,
        **context_options: Any,
    ) -> tuple[str, BrowserContext]:
        browser = self._require_browser()

        resolved_options = dict(context_options)
        if storage_state is not None:
            resolved_options["storage_state"] = self._normalize_storage_state(storage_state)

        context = await browser.new_context(**resolved_options)
        context_id = generate_browser_context_id()

        self._contexts_by_id[context_id] = context
        self._context_ids_by_obj[id(context)] = context_id

        context.on("close", lambda: self._on_context_closed(context_id))
        context.on("page", lambda page: self._register_page(context_id, page))

        for page in context.pages:
            self._register_page(context_id, page)

        self._logger.info(
            "browser_context_created",
            browser_session_id=self._browser_session_id,
            context_id=context_id,
        )
        return context_id, context

    async def save_storage_state(
        self,
        *,
        context_id: str | None = None,
        path: str | Path | None = None,
    ) -> dict[str, Any]:
        context = self._resolve_context(context_id)
        if context is None:
            msg = "No browser context is available to save storage state."
            raise BrowserSessionError(msg)

        if path is None:
            return await context.storage_state()

        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        state = await context.storage_state(path=str(output_path))
        self._logger.info(
            "browser_storage_state_saved",
            browser_session_id=self._browser_session_id,
            context_id=self.get_context_id(context),
            path=str(output_path),
        )
        return state

    def get_context(self, context_id: str) -> BrowserContext | None:
        return self._contexts_by_id.get(context_id)

    def get_context_id(self, context: BrowserContext) -> str | None:
        return self._context_ids_by_obj.get(id(context))

    def get_tab(self, tab_id: str) -> Page | None:
        return self._tabs_by_id.get(tab_id)

    def get_tab_id(self, page: Page) -> str | None:
        return self._tab_ids_by_obj.get(id(page))

    def list_tab_ids(self) -> list[str]:
        return list(self._tabs_by_id.keys())

    def get_tab_context_id(self, tab_id: str) -> str | None:
        return self._tab_context_id.get(tab_id)

    def get_frame(self, frame_id: str) -> Frame | None:
        return self._frames_by_id.get(frame_id)

    def get_frame_id(self, frame: Frame) -> str | None:
        return self._frame_ids_by_obj.get(id(frame))

    def get_frame_path(self, frame: Frame) -> list[str]:
        frame_id = self.get_frame_id(frame)
        if frame_id is None:
            return []

        path: list[str] = [frame_id]
        parent_id = self._frame_parent_id.get(frame_id)
        while parent_id:
            path.insert(0, parent_id)
            parent_id = self._frame_parent_id.get(parent_id)
        return path

    def resolve_frame_path(self, frame_path: list[str]) -> Frame | None:
        if not frame_path:
            return None
        return self._frames_by_id.get(frame_path[-1])

    def _resolve_context(self, context_id: str | None) -> BrowserContext | None:
        if context_id:
            return self._contexts_by_id.get(context_id)

        if not self._contexts_by_id:
            return None
        first_context_id = next(iter(self._contexts_by_id))
        return self._contexts_by_id[first_context_id]

    def _require_browser(self) -> Browser:
        if self._browser is None:
            msg = "BrowserSession.start() must be called before creating contexts."
            raise BrowserSessionError(msg)
        return self._browser

    def _normalize_storage_state(self, storage_state: StorageStateInput) -> str | Mapping[str, Any]:
        if isinstance(storage_state, Path):
            return str(storage_state)
        return storage_state

    def _register_page(self, context_id: str, page: Page) -> str:
        page_key = id(page)
        tab_id = self._tab_ids_by_obj.get(page_key)
        if tab_id is None:
            tab_id = generate_tab_id()
            self._tab_ids_by_obj[page_key] = tab_id
            self._tabs_by_id[tab_id] = page
            self._tab_context_id[tab_id] = context_id

            page.on("close", lambda: self._on_page_closed(page))
            page.on("frameattached", lambda frame: self._on_frame_attached(tab_id, frame))
            page.on("framenavigated", lambda frame: self._on_frame_navigated(tab_id, frame))
            page.on("framedetached", lambda frame: self._on_frame_detached(frame))

            self._register_frame_tree(tab_id, page.main_frame, parent_frame=None)
            self._logger.info(
                "browser_tab_registered",
                browser_session_id=self._browser_session_id,
                context_id=context_id,
                tab_id=tab_id,
            )

        return tab_id

    def _on_context_closed(self, context_id: str) -> None:
        tab_ids = [
            tab_id
            for tab_id, tab_context_id in self._tab_context_id.items()
            if tab_context_id == context_id
        ]
        for tab_id in tab_ids:
            self._drop_tab(tab_id)

        context = self._contexts_by_id.pop(context_id, None)
        if context is not None:
            self._context_ids_by_obj.pop(id(context), None)

    def _on_page_closed(self, page: Page) -> None:
        tab_id = self._tab_ids_by_obj.get(id(page))
        if tab_id is not None:
            self._drop_tab(tab_id)

    def _drop_tab(self, tab_id: str) -> None:
        page = self._tabs_by_id.pop(tab_id, None)
        if page is not None:
            self._tab_ids_by_obj.pop(id(page), None)

        self._tab_context_id.pop(tab_id, None)

        frame_ids = [frame_id for frame_id, owner_tab_id in self._frame_tab_id.items() if owner_tab_id == tab_id]
        for frame_id in frame_ids:
            self._drop_frame(frame_id)

    def _register_frame_tree(self, tab_id: str, frame: Frame, parent_frame: Frame | None) -> str:
        frame_id = self._register_frame(tab_id, frame, parent_frame)
        for child_frame in frame.child_frames:
            self._register_frame_tree(tab_id, child_frame, frame)
        return frame_id

    def _register_frame(self, tab_id: str, frame: Frame, parent_frame: Frame | None) -> str:
        frame_key = id(frame)
        frame_id = self._frame_ids_by_obj.get(frame_key)
        if frame_id is None:
            frame_id = generate_frame_id()
            self._frame_ids_by_obj[frame_key] = frame_id
            self._frames_by_id[frame_id] = frame

        parent_id: str | None = None
        if parent_frame is not None:
            parent_id = self._frame_ids_by_obj.get(id(parent_frame))
            if parent_id is None:
                parent_id = self._register_frame(tab_id, parent_frame, parent_frame.parent_frame)

        old_parent = self._frame_parent_id.get(frame_id)
        if old_parent and old_parent != parent_id:
            self._frame_children.get(old_parent, set()).discard(frame_id)

        self._frame_tab_id[frame_id] = tab_id
        self._frame_parent_id[frame_id] = parent_id
        self._frame_children.setdefault(frame_id, set())
        if parent_id is not None:
            self._frame_children.setdefault(parent_id, set()).add(frame_id)

        return frame_id

    def _on_frame_attached(self, tab_id: str, frame: Frame) -> None:
        self._register_frame(tab_id, frame, frame.parent_frame)

    def _on_frame_navigated(self, tab_id: str, frame: Frame) -> None:
        self._register_frame(tab_id, frame, frame.parent_frame)

    def _on_frame_detached(self, frame: Frame) -> None:
        frame_id = self._frame_ids_by_obj.get(id(frame))
        if frame_id is not None:
            self._drop_frame(frame_id)

    def _drop_frame(self, frame_id: str) -> None:
        for child_id in list(self._frame_children.get(frame_id, set())):
            self._drop_frame(child_id)

        parent_id = self._frame_parent_id.pop(frame_id, None)
        if parent_id is not None:
            self._frame_children.get(parent_id, set()).discard(frame_id)

        self._frame_children.pop(frame_id, None)
        self._frame_tab_id.pop(frame_id, None)

        frame = self._frames_by_id.pop(frame_id, None)
        if frame is not None:
            self._frame_ids_by_obj.pop(id(frame), None)

    def _clear_tracking(self) -> None:
        self._contexts_by_id.clear()
        self._context_ids_by_obj.clear()
        self._tabs_by_id.clear()
        self._tab_ids_by_obj.clear()
        self._tab_context_id.clear()
        self._frames_by_id.clear()
        self._frame_ids_by_obj.clear()
        self._frame_tab_id.clear()
        self._frame_parent_id.clear()
        self._frame_children.clear()
