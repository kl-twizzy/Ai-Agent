import base64
import math
import os
import re
import threading
import time
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright


class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._lock = threading.Lock()
        self._last_elements: list[dict[str, Any]] = []
        self._headless = False
        self._action_delay_ms = int(os.getenv("BROWSER_ACTION_DELAY_MS", "180"))

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", (value or "").strip().lower())

    def start(self, headless: bool = False):
        if self.is_started():
            return

        env_headless = os.getenv("BROWSER_HEADLESS")
        if env_headless is not None:
            headless = env_headless.strip().lower() in {"1", "true", "yes", "on"}
        self._headless = headless

        self.playwright = sync_playwright().start()
        browser_engine = (os.getenv("BROWSER_ENGINE") or "playwright").strip().lower()

        yandex_paths = [
            rf"C:\Users\{os.environ.get('USERNAME', '')}\AppData\Local\Yandex\YandexBrowser\Application\browser.exe",
            r"C:\Program Files\Yandex\YandexBrowser\Application\browser.exe",
            r"C:\Program Files (x86)\Yandex\YandexBrowser\Application\browser.exe",
        ]
        edge_paths = [
            rf"C:\Users\{os.environ.get('USERNAME', '')}\AppData\Local\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]

        yandex_path = next((path for path in yandex_paths if os.path.exists(path)), None)
        edge_path = next((path for path in edge_paths if os.path.exists(path)), None)
        launch_kwargs = {
            "headless": headless,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        }
        if browser_engine == "yandex" and yandex_path and not headless:
            launch_kwargs["executable_path"] = yandex_path
        elif browser_engine == "edge" and edge_path and not headless:
            launch_kwargs["executable_path"] = edge_path

        self.browser = self.playwright.chromium.launch(**launch_kwargs)
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
        )
        self.page = self.context.new_page()
        self.page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """
        )

    def is_started(self) -> bool:
        return self.browser is not None and self.context is not None and self.page is not None

    def ensure_started(self, headless: bool = False):
        if not self.is_started():
            self.start(headless=headless)

    def open_new_tab(self, url: str | None = None) -> Page:
        self.ensure_started(headless=self._headless)
        self.page = self.context.new_page()
        self.page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """
        )
        if url:
            self.goto(url)
        return self.page

    def accept_cookies(self) -> bool:
        if not self.page:
            return False

        accept_selectors = [
            "button:has-text('Accept all')",
            "button:has-text('Accept')",
            "button:has-text('OK')",
            "button:has-text('I agree')",
            "button:has-text('Принять')",
            "button:has-text('Принять все')",
            "button:has-text('Согласен')",
            "#L2AGLb",
        ]
        for selector in accept_selectors:
            try:
                locator = self.page.locator(selector)
                if locator.count() > 0 and locator.first.is_visible():
                    locator.first.click(timeout=2000)
                    return True
            except Exception:
                continue
        return False

    def screenshot(self) -> str:
        screenshot_bytes = self.page.screenshot(full_page=False)
        return base64.b64encode(screenshot_bytes).decode("utf-8")

    def get_page_text(self) -> str:
        try:
            text = self.page.evaluate("() => document.body?.innerText || ''")
            return text[:4000] if text else ""
        except Exception:
            return ""

    def get_url(self) -> str:
        return self.page.url if self.page else ""

    def _search_portal_selectors(self) -> list[str]:
        url = (self.get_url() or "").lower()
        if "ya.ru" in url or "yandex" in url:
            return [
                'input[name="text"]',
                'textarea[name="text"]',
                'input[aria-label*="Запрос"]',
                'textarea[aria-label*="Запрос"]',
                'input[placeholder*="Найд"]',
                'textarea[placeholder*="Найд"]',
                'input[placeholder*="Поиск"]',
                'textarea[placeholder*="Поиск"]',
                'input[type="search"]',
            ]
        if "google." in url:
            return [
                'textarea[name="q"]',
                'input[name="q"]',
                'textarea[aria-label*="Search"]',
                'textarea[aria-label*="Поиск"]',
            ]
        if "bing." in url:
            return [
                'textarea[name="q"]',
                'input[name="q"]',
                'input[type="search"]',
            ]
        return []

    def _find_search_input_selector(self) -> str | None:
        if not self.page:
            return None
        for selector in self._search_portal_selectors():
            try:
                locator = self.page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    return selector
            except Exception:
                continue
        return None

    def _search_engine_hosts(self) -> tuple[str, ...]:
        return ("ya.ru", "yandex.", "google.", "bing.com")

    def is_search_results_page(self) -> bool:
        url = (self.get_url() or "").lower()
        return any(host in url for host in self._search_engine_hosts()) and any(
            token in url for token in ("text=", "q=", "query=", "/search")
        )

    def goto(self, url: str):
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self.wait(1.0)
        self.accept_cookies()

    def wait(self, seconds: float = 1.0):
        time.sleep(seconds)

    def _human_pause(self, multiplier: float = 1.0):
        delay = max(self._action_delay_ms * multiplier, 0) / 1000
        if delay > 0:
            time.sleep(delay)

    def _move_mouse_smoothly(self, x: int, y: int):
        if self._headless:
            return
        steps = max(12, min(30, int(math.hypot(x, y) / 80)))
        self.page.mouse.move(x, y, steps=steps)
        self._human_pause(0.35)

    def _click_point(self, x: int, y: int):
        self._move_mouse_smoothly(x, y)
        self.page.mouse.click(x, y)
        self._human_pause(0.6)

    def _refresh_elements(self) -> list[dict[str, Any]]:
        if not self.page:
            self._last_elements = []
            return self._last_elements

        elements = self.page.evaluate(
            """
            () => {
              const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return (
                  style &&
                  style.visibility !== 'hidden' &&
                  style.display !== 'none' &&
                  rect.width > 6 &&
                  rect.height > 6 &&
                  rect.bottom >= 0 &&
                  rect.right >= 0 &&
                  rect.top <= window.innerHeight &&
                  rect.left <= window.innerWidth
                );
              };

              const normalize = (value) =>
                (value || '').replace(/\\s+/g, ' ').trim().slice(0, 120);

              const cssEscape = (value) => {
                if (!value) return '';
                if (window.CSS && window.CSS.escape) return window.CSS.escape(value);
                return value.replace(/[^a-zA-Z0-9_-]/g, '\\\\$&');
              };

              const buildSelector = (el) => {
                if (el.id) return `#${cssEscape(el.id)}`;

                const attrs = ['data-testid', 'data-test', 'data-qa', 'name', 'aria-label', 'placeholder', 'type'];
                for (const attr of attrs) {
                  const value = el.getAttribute(attr);
                  if (value && value.length < 80) {
                    return `${el.tagName.toLowerCase()}[${attr}="${value.replace(/"/g, '\\"')}"]`;
                  }
                }

                const parts = [];
                let current = el;
                let depth = 0;
                while (current && current.nodeType === Node.ELEMENT_NODE && depth < 4) {
                  let part = current.tagName.toLowerCase();
                  const classNames = Array.from(current.classList || [])
                    .filter((name) => name && name.length < 30 && !name.includes(':'))
                    .slice(0, 2);
                  if (classNames.length) {
                    part += classNames.map((name) => `.${cssEscape(name)}`).join('');
                  } else if (current.parentElement) {
                    const siblings = Array.from(current.parentElement.children)
                      .filter((child) => child.tagName === current.tagName);
                    if (siblings.length > 1) {
                      const index = siblings.indexOf(current) + 1;
                      part += `:nth-of-type(${index})`;
                    }
                  }
                  parts.unshift(part);
                  const candidate = parts.join(' > ');
                  try {
                    const found = document.querySelectorAll(candidate);
                    if (found.length === 1 && found[0] === el) return candidate;
                  } catch (e) {}
                  current = current.parentElement;
                  depth += 1;
                }
                return parts.join(' > ');
              };

              const nodes = Array.from(
                document.querySelectorAll('a, button, input, textarea, select, [role="button"], [role="link"], [role="textbox"], [contenteditable="true"]')
              );

              return nodes
                .filter((el) => isVisible(el))
                .slice(0, 80)
                .map((el, idx) => {
                  const rect = el.getBoundingClientRect();
                  const text = normalize(el.innerText || el.textContent);
                  return {
                    index: idx,
                    tag: el.tagName.toLowerCase(),
                    type: el.getAttribute('type') || '',
                    role: el.getAttribute('role') || '',
                    text,
                    placeholder: normalize(el.getAttribute('placeholder')),
                    ariaLabel: normalize(el.getAttribute('aria-label')),
                    name: normalize(el.getAttribute('name')),
                    href: normalize(el.getAttribute('href')),
                    selector: buildSelector(el),
                    x: Math.round(rect.left + rect.width / 2),
                    y: Math.round(rect.top + rect.height / 2),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                  };
                });
            }
            """
        )
        self._last_elements = elements or []
        return self._last_elements

    def get_interactive_elements(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._refresh_elements()

    def get_page_state(self) -> dict[str, Any]:
        elements = self.get_interactive_elements()
        return {
            "url": self.get_url(),
            "title": self.page.title() if self.page else "",
            "text": self.get_page_text(),
            "elements": elements,
            "viewport": {"width": 1280, "height": 800},
        }

    def find_elements(self, query: str, limit: int = 5, action_hint: str | None = None) -> list[dict[str, Any]]:
        normalized_query = self._normalize_text(query)
        if not normalized_query:
            return []

        tokens = [token for token in normalized_query.split(" ") if token]
        elements = self.get_interactive_elements()
        matches: list[dict[str, Any]] = []

        for element in elements:
            haystack_parts = [
                element.get("text", ""),
                element.get("placeholder", ""),
                element.get("ariaLabel", ""),
                element.get("name", ""),
                element.get("href", ""),
                element.get("selector", ""),
                element.get("tag", ""),
                element.get("role", ""),
                element.get("type", ""),
            ]
            haystack = self._normalize_text(" ".join(str(part) for part in haystack_parts if part))
            if not haystack:
                continue

            score = 0.0
            if normalized_query in haystack:
                score += 5.0

            matched_tokens = sum(1 for token in tokens if token in haystack)
            score += matched_tokens * 1.5

            tag = (element.get("tag") or "").lower()
            role = (element.get("role") or "").lower()
            elem_type = (element.get("type") or "").lower()
            if action_hint == "type" and (tag in {"input", "textarea", "select"} or role == "textbox" or elem_type in {"search", "text"}):
                score += 2.0
            if action_hint == "click" and (tag in {"button", "a"} or role in {"button", "link"}):
                score += 1.5

            if matched_tokens == 0 and normalized_query not in haystack:
                continue

            match = dict(element)
            match["match_score"] = round(score, 2)
            matches.append(match)

        matches.sort(key=lambda item: item.get("match_score", 0), reverse=True)
        return matches[:limit]

    def _selector_from_index(self, index: int) -> str | None:
        if not self._last_elements:
            self._refresh_elements()
        for element in self._last_elements:
            if element.get("index") == index:
                return element.get("selector")
        return None

    def click(self, selector: str | None = None, index: int | None = None, x: int | None = None, y: int | None = None):
        if selector:
            locator = self.page.locator(selector).first
            box = locator.bounding_box()
            if box and not self._headless:
                self._click_point(int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
            else:
                locator.click(timeout=5000)
            return
        if index is not None:
            resolved_selector = self._selector_from_index(index)
            if resolved_selector:
                locator = self.page.locator(resolved_selector).first
                box = locator.bounding_box()
                if box and not self._headless:
                    self._click_point(int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
                else:
                    locator.click(timeout=5000)
                return
        if x is not None and y is not None:
            self._click_point(x, y)
            return
        raise ValueError("click requires selector, index, or coordinates")

    def hover(self, selector: str | None = None, index: int | None = None):
        if selector:
            locator = self.page.locator(selector).first
            box = locator.bounding_box()
            if box and not self._headless:
                self._move_mouse_smoothly(int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
            else:
                locator.hover(timeout=5000)
            return
        if index is not None:
            resolved_selector = self._selector_from_index(index)
            if resolved_selector:
                locator = self.page.locator(resolved_selector).first
                box = locator.bounding_box()
                if box and not self._headless:
                    self._move_mouse_smoothly(int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
                else:
                    locator.hover(timeout=5000)
                return
        raise ValueError("hover requires selector or index")

    def click_best_match(self, query: str):
        matches = self.find_elements(query, limit=1, action_hint="click")
        if not matches:
            if self.is_search_results_page():
                self.click_first_search_result()
                return
            interactive = self.get_interactive_elements()
            fallback = next(
                (
                    element
                    for element in interactive
                    if (element.get("tag") or "").lower() in {"button", "a"}
                    or (element.get("role") or "").lower() in {"button", "link"}
                ),
                None,
            )
            if not fallback:
                raise ValueError(f"No element matched query: {query}")
            self.click(index=fallback["index"])
            return
        self.click(index=matches[0]["index"])

    def click_first_search_result(self):
        current_url = (self.get_url() or "").lower()
        search_hosts = self._search_engine_hosts()
        interactive = self.get_interactive_elements()
        candidates = []
        for element in interactive:
            href = (element.get("href") or "").strip()
            tag = (element.get("tag") or "").lower()
            text = (element.get("text") or "").strip()
            if tag != "a" or not href.startswith("http"):
                continue
            href_lower = href.lower()
            if any(host in href_lower for host in search_hosts):
                continue
            if current_url and href_lower == current_url:
                continue
            score = 0
            if text:
                score += min(len(text), 80)
            if "/search" not in href_lower:
                score += 20
            candidates.append((score, element))

        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates:
            raise ValueError("No external search result link found on the page")
        self.click(index=candidates[0][1]["index"])

    def type_text(
        self,
        text: str = "",
        selector: str | None = None,
        index: int | None = None,
        x: int | None = None,
        y: int | None = None,
        submit: bool = False,
        clear: bool = True,
    ):
        def fill_via_dom(locator) -> bool:
            try:
                return bool(
                    locator.evaluate(
                        """(el, value) => {
                            if (!el) return false;
                            el.focus();
                            if ('value' in el) {
                                el.value = value;
                            } else if (el.isContentEditable) {
                                el.innerText = value;
                            } else {
                                return false;
                            }
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        }""",
                        text,
                    )
                )
            except Exception:
                return False

        if selector or index is not None:
            resolved_selector = selector or self._selector_from_index(index)
            if not resolved_selector:
                raise ValueError("Unable to resolve element selector for typing")

            locator = self.page.locator(resolved_selector).first
            locator.wait_for(state="visible", timeout=5000)
            try:
                locator.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass

            box = locator.bounding_box()
            if box and not self._headless:
                self._click_point(int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
            else:
                locator.click(timeout=5000)

            self._human_pause(0.2)
            typed = False
            if clear:
                try:
                    locator.fill(text, timeout=5000)
                    typed = True
                except Exception:
                    typed = fill_via_dom(locator)
            if not typed:
                try:
                    locator.focus(timeout=3000)
                except Exception:
                    pass
                try:
                    locator.press("Control+A", timeout=3000)
                    locator.press("Backspace", timeout=3000)
                    locator.type(text, delay=max(20, self._action_delay_ms // 6), timeout=5000)
                    typed = True
                except Exception:
                    typed = fill_via_dom(locator)
            if not typed:
                raise ValueError(f"Unable to type into element: {resolved_selector}")
        elif x is not None and y is not None:
            self._click_point(x, y)
            self.page.keyboard.type(text, delay=max(20, self._action_delay_ms // 6))
        else:
            raise ValueError("type_text requires selector, index, or coordinates")

        if submit:
            self._human_pause(0.3)
            self.page.keyboard.press("Enter")

    def type_into_best_match(self, query: str, text: str, submit: bool = False):
        search_selector = self._find_search_input_selector()
        if search_selector:
            self.type_text(selector=search_selector, text=text, submit=submit)
            return

        matches = self.find_elements(query, limit=1, action_hint="type")
        if matches:
            self.type_text(index=matches[0]["index"], text=text, submit=submit)
            return

        interactive = self.get_interactive_elements()
        fallback = next(
            (
                element
                for element in interactive
                if (element.get("tag") or "").lower() in {"input", "textarea", "select"}
                or (element.get("role") or "").lower() == "textbox"
                or (element.get("type") or "").lower() in {"search", "text"}
            ),
            None,
        )
        if not fallback:
            raise ValueError(f"No input-like element matched query: {query}")
        self.type_text(index=fallback["index"], text=text, submit=submit)

    def select_option(
        self,
        value: str,
        selector: str | None = None,
        index: int | None = None,
        label: str | None = None,
    ):
        resolved_selector = selector or (self._selector_from_index(index) if index is not None else None)
        if not resolved_selector:
            matches = self.find_elements(label or value, limit=1, action_hint="type")
            if matches:
                resolved_selector = matches[0].get("selector")
        if not resolved_selector:
            raise ValueError("select_option requires selector, index, or a matchable label/value")
        locator = self.page.locator(resolved_selector).first
        try:
            locator.select_option(value=value, timeout=5000)
        except Exception:
            locator.select_option(label=value, timeout=5000)

    def navigate_history(self, direction: str = "back"):
        if direction == "back":
            self.page.go_back(wait_until="domcontentloaded", timeout=10000)
            return
        if direction == "forward":
            self.page.go_forward(wait_until="domcontentloaded", timeout=10000)
            return
        raise ValueError("direction must be 'back' or 'forward'")

    def wait_for_text(self, text: str, timeout_ms: int = 10000) -> bool:
        try:
            self.page.get_by_text(text).first.wait_for(timeout=timeout_ms)
            return True
        except Exception:
            return False

    def press_key(self, key: str):
        self._human_pause(0.2)
        self.page.keyboard.press(key)
        self._human_pause(0.3)

    def scroll(
        self,
        direction: str = "down",
        amount: int = 300,
        selector: str | None = None,
        index: int | None = None,
    ):
        delta = amount if direction == "down" else -amount
        resolved_selector = selector or (self._selector_from_index(index) if index is not None else None)
        if resolved_selector:
            self.hover(selector=resolved_selector)
        self.page.mouse.wheel(0, delta)
        self._human_pause(0.4)

    def close(self):
        if self.context:
            self.context.close()
            self.context = None
        self.page = None
        self._last_elements = []
        if self.browser:
            self.browser.close()
            self.browser = None
        if self.playwright:
            self.playwright.stop()
            self.playwright = None
