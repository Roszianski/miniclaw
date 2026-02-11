"""Browser automation tool using Playwright."""

import json
from pathlib import Path
from typing import Any

from loguru import logger

from miniclaw.agent.tools.base import Tool

try:
    from playwright.async_api import async_playwright, Browser, Page, BrowserContext
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


class BrowserTool(Tool):
    """Full browser automation using Playwright with CDP accessibility snapshots."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self._browser: "Browser | None" = None
        self._context: "BrowserContext | None" = None
        self._pages: list["Page"] = []
        self._current_tab: int = 0
        self._playwright: Any = None
        self._console_logs: list[str] = []

    @property
    def name(self) -> str:
        return "browser"

    @property
    def description(self) -> str:
        return (
            "Control a headless Chromium browser. Actions: navigate, click, type, hover, "
            "scroll, select, drag, file_upload, dialog_accept/dismiss, screenshot, "
            "get_snapshot, get_content, console_logs, evaluate_js, back, forward, reload, "
            "cookies_get/set/clear, storage_get/set, set_geolocation, set_viewport, "
            "new_tab, close_tab, list_tabs, switch_tab, close."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "navigate", "click", "type", "hover", "scroll", "select", "drag",
                        "file_upload", "dialog_accept", "dialog_dismiss",
                        "screenshot", "get_content", "get_snapshot", "console_logs",
                        "evaluate_js", "back", "forward", "reload",
                        "cookies_get", "cookies_set", "cookies_clear",
                        "storage_get", "storage_set",
                        "set_geolocation", "set_viewport",
                        "new_tab", "close_tab", "list_tabs", "switch_tab", "close",
                    ],
                    "description": "Browser action to perform",
                },
                "url": {"type": "string", "description": "URL for navigate"},
                "selector": {"type": "string", "description": "CSS selector or [ref=N] for click/type/hover/select/drag"},
                "text": {"type": "string", "description": "Text to type"},
                "value": {"type": "string", "description": "Value for select"},
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "description": "Scroll direction"},
                "amount": {"type": "integer", "description": "Scroll amount in pixels (default 500)"},
                "expression": {"type": "string", "description": "JS expression for evaluate_js"},
                "index": {"type": "integer", "description": "Tab index for switch_tab"},
                "from": {"type": "string", "description": "Drag source selector"},
                "to": {"type": "string", "description": "Drag target selector"},
                "path": {"type": "string", "description": "File path for upload"},
                "cookies": {
                    "type": "array",
                    "description": "Cookies to set",
                    "items": {"type": "object"},
                },
                "storage_type": {"type": "string", "enum": ["local", "session"], "description": "Storage type"},
                "key": {"type": "string", "description": "Storage key"},
                "data": {"type": "object", "description": "Storage key/values"},
                "lat": {"type": "number", "description": "Latitude"},
                "lng": {"type": "number", "description": "Longitude"},
                "width": {"type": "integer", "description": "Viewport width"},
                "height": {"type": "integer", "description": "Viewport height"},
            },
            "required": ["action"],
        }

    async def _ensure_browser(self) -> "Page":
        """Lazy-init browser and return current page."""
        if not HAS_PLAYWRIGHT:
            raise RuntimeError("playwright not installed. Run: pip install 'miniclaw[browser]' && playwright install chromium")

        if not self._browser:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) miniclaw-browser/0.2",
            )
            page = await self._context.new_page()
            page.on("console", lambda msg: self._console_logs.append(msg.text()))
            self._pages = [page]
            self._current_tab = 0

        if not self._pages:
            page = await self._context.new_page()
            page.on("console", lambda msg: self._console_logs.append(msg.text()))
            self._pages = [page]
            self._current_tab = 0

        return self._pages[self._current_tab]

    def _resolve_selector(self, selector: str) -> str:
        """Resolve [ref=N] selectors to data attributes."""
        if not selector:
            return selector
        sel = selector.strip()
        if sel.startswith("[ref=") and sel.endswith("]"):
            num = sel[len("[ref="):-1]
            return f'[data-miniclaw-ref="{num}"]'
        if sel.startswith("ref="):
            num = sel[len("ref="):]
            return f'[data-miniclaw-ref="{num}"]'
        return sel

    async def _ensure_refs(self, page: "Page") -> list[dict[str, Any]]:
        """Inject data-miniclaw-ref attributes and return a snapshot list."""
        script = """
        () => {
          const els = Array.from(document.querySelectorAll('body *'));
          let i = 0;
          const items = [];
          for (const el of els) {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) continue;
            const tag = el.tagName.toLowerCase();
            const text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
            const aria = el.getAttribute('aria-label') || '';
            const role = el.getAttribute('role') || tag;
            el.setAttribute('data-miniclaw-ref', String(i));
            items.push({ ref: i, role, tag, text: text.slice(0, 80), aria });
            i++;
          }
          return items;
        }
        """
        return await page.evaluate(script)

    async def execute(self, action: str, **kwargs: Any) -> str:
        try:
            handler = getattr(self, f"_action_{action}", None)
            if not handler:
                return f"Unknown browser action: {action}"
            return await handler(**kwargs)
        except Exception as e:
            logger.error(f"Browser action '{action}' failed: {e}")
            return f"Browser error: {e}"

    # === Navigation ===

    async def _action_navigate(self, url: str = "", **kw: Any) -> str:
        if not url:
            return "Error: url is required"
        page = await self._ensure_browser()
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        status = resp.status if resp else "unknown"
        return f"Navigated to {url} (status: {status})"

    async def _action_back(self, **kw: Any) -> str:
        page = await self._ensure_browser()
        await page.go_back(wait_until="domcontentloaded")
        return f"Navigated back to {page.url}"

    async def _action_forward(self, **kw: Any) -> str:
        page = await self._ensure_browser()
        await page.go_forward(wait_until="domcontentloaded")
        return f"Navigated forward to {page.url}"

    async def _action_reload(self, **kw: Any) -> str:
        page = await self._ensure_browser()
        await page.reload(wait_until="domcontentloaded")
        return f"Reloaded {page.url}"

    # === Interaction ===

    async def _action_click(self, selector: str = "", **kw: Any) -> str:
        if not selector:
            return "Error: selector is required"
        page = await self._ensure_browser()
        selector = self._resolve_selector(selector)
        await page.click(selector, timeout=5000)
        return f"Clicked {selector}"

    async def _action_type(self, selector: str = "", text: str = "", **kw: Any) -> str:
        if not selector or not text:
            return "Error: selector and text are required"
        page = await self._ensure_browser()
        selector = self._resolve_selector(selector)
        await page.fill(selector, text, timeout=5000)
        return f"Typed into {selector}"

    async def _action_hover(self, selector: str = "", **kw: Any) -> str:
        if not selector:
            return "Error: selector is required"
        page = await self._ensure_browser()
        selector = self._resolve_selector(selector)
        await page.hover(selector, timeout=5000)
        return f"Hovered over {selector}"

    async def _action_scroll(self, direction: str = "down", amount: int = 500, **kw: Any) -> str:
        page = await self._ensure_browser()
        dx, dy = 0, 0
        if direction == "down":
            dy = amount
        elif direction == "up":
            dy = -amount
        elif direction == "right":
            dx = amount
        elif direction == "left":
            dx = -amount
        await page.mouse.wheel(dx, dy)
        return f"Scrolled {direction} by {amount}px"

    async def _action_select(self, selector: str = "", value: str = "", **kw: Any) -> str:
        if not selector or not value:
            return "Error: selector and value are required"
        page = await self._ensure_browser()
        selector = self._resolve_selector(selector)
        await page.select_option(selector, value, timeout=5000)
        return f"Selected '{value}' in {selector}"

    async def _action_drag(self, **kw: Any) -> str:
        from_sel = kw.get("from", "")
        to_sel = kw.get("to", "")
        if not from_sel or not to_sel:
            return "Error: from and to selectors are required"
        page = await self._ensure_browser()
        from_sel = self._resolve_selector(from_sel)
        to_sel = self._resolve_selector(to_sel)
        await page.drag_and_drop(from_sel, to_sel, timeout=5000)
        return f"Dragged from {from_sel} to {to_sel}"

    async def _action_file_upload(self, selector: str = "", path: str = "", **kw: Any) -> str:
        if not selector or not path:
            return "Error: selector and path are required"
        page = await self._ensure_browser()
        selector = self._resolve_selector(selector)
        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = (self.workspace / path).resolve()
        await page.set_input_files(selector, str(file_path))
        return f"Uploaded {file_path} to {selector}"

    async def _action_dialog_accept(self, **kw: Any) -> str:
        page = await self._ensure_browser()
        dialog = await page.wait_for_event("dialog", timeout=5000)
        await dialog.accept()
        return "Dialog accepted."

    async def _action_dialog_dismiss(self, **kw: Any) -> str:
        page = await self._ensure_browser()
        dialog = await page.wait_for_event("dialog", timeout=5000)
        await dialog.dismiss()
        return "Dialog dismissed."

    # === Inspection ===

    async def _action_screenshot(self, **kw: Any) -> str:
        page = await self._ensure_browser()
        screenshot_dir = self.workspace / "media"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = screenshot_dir / "browser_screenshot.png"
        await page.screenshot(path=str(path), full_page=False)
        return f"Screenshot saved to {path}"

    async def _action_get_content(self, **kw: Any) -> str:
        page = await self._ensure_browser()
        content = await page.content()
        # Return truncated for sanity
        if len(content) > 50000:
            content = content[:50000] + "\n... (truncated)"
        return content

    async def _action_get_snapshot(self, **kw: Any) -> str:
        """Get CDP accessibility tree snapshot with numbered refs."""
        page = await self._ensure_browser()
        try:
            items = await self._ensure_refs(page)
            if not items:
                return "No snapshot available"
            lines = []
            for item in items:
                ref = item.get("ref")
                role = item.get("role") or item.get("tag") or "element"
                label = item.get("aria") or item.get("text") or ""
                line = f"[ref={ref}] {role}"
                if label:
                    line += f' "{label}"'
                lines.append(line)
            return "\n".join(lines)
        except Exception:
            # Fallback: get simplified DOM
            return await self._action_get_content(**kw)

    async def _action_console_logs(self, **kw: Any) -> str:
        if not self._console_logs:
            return "No console logs captured."
        logs = self._console_logs[-50:]
        return "\n".join(logs)

    async def _action_evaluate_js(self, expression: str = "", **kw: Any) -> str:
        if not expression:
            return "Error: expression is required"
        page = await self._ensure_browser()
        result = await page.evaluate(expression)
        return json.dumps(result, default=str, ensure_ascii=False)[:10000]

    # === State ===

    async def _action_cookies_get(self, **kw: Any) -> str:
        await self._ensure_browser()
        cookies = await self._context.cookies()
        return json.dumps(cookies, ensure_ascii=False)[:10000]

    async def _action_cookies_set(self, cookies: list[dict] | None = None, **kw: Any) -> str:
        await self._ensure_browser()
        if not cookies:
            return "Error: cookies array is required"
        await self._context.add_cookies(cookies)
        return f"Set {len(cookies)} cookies"

    async def _action_cookies_clear(self, **kw: Any) -> str:
        await self._ensure_browser()
        await self._context.clear_cookies()
        return "Cleared cookies"

    async def _action_storage_get(self, storage_type: str = "local", **kw: Any) -> str:
        page = await self._ensure_browser()
        if storage_type not in ("local", "session"):
            return "Error: storage_type must be 'local' or 'session'"
        script = "(type) => Object.fromEntries(Object.entries(window[type + 'Storage']))"
        data = await page.evaluate(script, storage_type)
        return json.dumps(data, ensure_ascii=False)[:10000]

    async def _action_storage_set(self, storage_type: str = "local", key: str = "", data: dict | None = None, **kw: Any) -> str:
        page = await self._ensure_browser()
        if storage_type not in ("local", "session"):
            return "Error: storage_type must be 'local' or 'session'"
        if data:
            await page.evaluate(
                "(args) => { const { type, payload } = args; for (const [k,v] of Object.entries(payload)) { window[type + 'Storage'].setItem(k, String(v)); } }",
                {"type": storage_type, "payload": data},
            )
            return f"Set {len(data)} storage items"
        if not key or "value" not in kw:
            return "Error: key and value are required"
        value = kw.get("value", "")
        await page.evaluate(
            "(args) => { const { type, k, v } = args; window[type + 'Storage'].setItem(k, String(v)); }",
            {"type": storage_type, "k": key, "v": value},
        )
        return f"Set storage key '{key}'"

    async def _action_set_geolocation(self, lat: float = 0.0, lng: float = 0.0, **kw: Any) -> str:
        await self._ensure_browser()
        await self._context.grant_permissions(["geolocation"])
        await self._context.set_geolocation({"latitude": lat, "longitude": lng})
        return f"Set geolocation to {lat},{lng}"

    async def _action_set_viewport(self, width: int = 1280, height: int = 720, **kw: Any) -> str:
        page = await self._ensure_browser()
        await page.set_viewport_size({"width": width, "height": height})
        return f"Viewport set to {width}x{height}"

    # === Tab Management ===

    async def _action_new_tab(self, **kw: Any) -> str:
        await self._ensure_browser()
        page = await self._context.new_page()
        page.on("console", lambda msg: self._console_logs.append(msg.text()))
        self._pages.append(page)
        self._current_tab = len(self._pages) - 1
        return f"Opened new tab (index: {self._current_tab})"

    async def _action_close_tab(self, **kw: Any) -> str:
        if not self._pages:
            return "No tabs to close"
        page = self._pages[self._current_tab]
        await page.close()
        self._pages.pop(self._current_tab)
        if self._pages:
            self._current_tab = min(self._current_tab, len(self._pages) - 1)
        return f"Closed tab. {len(self._pages)} tabs remaining."

    async def _action_list_tabs(self, **kw: Any) -> str:
        if not self._pages:
            return "No open tabs"
        lines = []
        for i, page in enumerate(self._pages):
            marker = " *" if i == self._current_tab else ""
            lines.append(f"  [{i}]{marker} {page.url}")
        return "Tabs:\n" + "\n".join(lines)

    async def _action_switch_tab(self, index: int = 0, **kw: Any) -> str:
        if not self._pages or index < 0 or index >= len(self._pages):
            return f"Invalid tab index: {index} (have {len(self._pages)} tabs)"
        self._current_tab = index
        return f"Switched to tab {index}: {self._pages[index].url}"

    async def _action_close(self, **kw: Any) -> str:
        """Close the browser entirely."""
        if self._browser:
            await self._browser.close()
            self._browser = None
            self._context = None
            self._pages = []
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        return "Browser closed."
