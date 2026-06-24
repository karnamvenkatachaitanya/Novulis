"""
Browser automation for capturing authenticated application page states.

Expected environment variables:
  APP_BASE_URL                  Example: https://app.example.com
  APP_LOGIN_PATH                Default: /login
  APP_LOGIN_EMAIL               Default: m@example.com
  APP_LOGIN_PASSWORD            Required unless --password is provided

Optional selector overrides:
  LOGIN_EMAIL_SELECTOR
  LOGIN_PASSWORD_SELECTOR
  LOGIN_SUBMIT_SELECTOR
  DASHBOARD_READY_SELECTOR

Example:
  python scraper.py --url-path /dashboard
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional local convenience
    load_dotenv = None


DEFAULT_EMAIL = "m@example.com"
DEFAULT_LOGIN_PATH = "/login"
CAPTURE_DIR = Path("captured_states")
NAVIGATION_TIMEOUT_MS = 45_000
ACTION_TIMEOUT_MS = 15_000
DASHBOARD_TIMEOUT_MS = 60_000
NETWORK_IDLE_TIMEOUT_MS = 20_000


logger = logging.getLogger("scraper")

_global_page: Page | None = None
_global_base_url: str | None = None


@dataclass
class CapturedElement:
    role: str
    tag: str
    text: str
    selector_hint: str | None
    attributes: dict[str, str]
    bounds: dict[str, float] | None
    visible: bool


@dataclass
class PageState:
    url: str
    title: str
    captured_at_unix: float
    screenshot_path: str
    html_path: str
    elements: dict[str, list[dict[str, Any]]]


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def normalize_url_path(url_path: str) -> str:
    if not url_path.startswith("/"):
        return f"/{url_path}"
    return url_path


def safe_filename_from_path(url_path: str) -> str:
    parsed = urlparse(url_path)
    raw = parsed.path or "home"
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw.strip("/")) or "home"
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{safe}-{timestamp}"


def env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def first_working_selector(page: Page, selectors: list[str], timeout_ms: int) -> str:
    last_error: Exception | None = None
    for selector in selectors:
        try:
            page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
            return selector
        except PlaywrightTimeoutError as exc:
            last_error = exc
    raise RuntimeError(f"None of these selectors became visible: {selectors}") from last_error


def wait_for_stable_page(page: Page, ready_selector: str | None = None) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        logger.warning("Timed out waiting for domcontentloaded; continuing with additional checks")

    try:
        page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        logger.warning("Network did not become idle; app may keep live connections open")

    if ready_selector:
        logger.info("Waiting for ready selector: %s", ready_selector)
        page.wait_for_selector(ready_selector, state="visible", timeout=DASHBOARD_TIMEOUT_MS)

    page.wait_for_timeout(1_500)


def login(page: Page, base_url: str, login_path: str, email: str, password: str, ready_selector: str | None) -> None:
    global _global_page, _global_base_url
    _global_page = page
    _global_base_url = base_url

    login_url = urljoin(base_url.rstrip("/") + "/", login_path.lstrip("/"))
    logger.info("Navigating to login page: %s", login_url)
    page.goto(login_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    wait_for_stable_page(page)

    email_selector = os.environ.get("LOGIN_EMAIL_SELECTOR")
    password_selector = os.environ.get("LOGIN_PASSWORD_SELECTOR")
    submit_selector = os.environ.get("LOGIN_SUBMIT_SELECTOR")

    email_candidates = [
        email_selector,
        "input[type='email']",
        "input[name='email']",
        "input[name='username']",
        "input[autocomplete='email']",
        "input[id*='email' i]",
    ]
    password_candidates = [
        password_selector,
        "input[type='password']",
        "input[name='password']",
        "input[autocomplete='current-password']",
        "input[id*='password' i]",
    ]
    submit_candidates = [
        submit_selector,
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Log in')",
        "button:has-text('Login')",
        "button:has-text('Sign in')",
    ]

    email_field = first_working_selector(page, [item for item in email_candidates if item], ACTION_TIMEOUT_MS)
    password_field = first_working_selector(page, [item for item in password_candidates if item], ACTION_TIMEOUT_MS)

    logger.info("Filling login form")
    page.fill(email_field, email, timeout=ACTION_TIMEOUT_MS)
    page.fill(password_field, password, timeout=ACTION_TIMEOUT_MS)

    submit_button = first_working_selector(page, [item for item in submit_candidates if item], ACTION_TIMEOUT_MS)
    logger.info("Submitting login form")
    page.click(submit_button, timeout=ACTION_TIMEOUT_MS)

    wait_for_stable_page(page, ready_selector=ready_selector)
    logger.info("Login flow completed at: %s", page.url)


def build_selector_hint(element: dict[str, Any]) -> str | None:
    tag = element.get("tag")
    attrs = element.get("attributes", {})
    if attrs.get("id"):
        return f"#{attrs['id']}"
    if attrs.get("data-testid"):
        return f"[data-testid='{attrs['data-testid']}']"
    if attrs.get("name"):
        return f"{tag}[name='{attrs['name']}']"
    if attrs.get("aria-label"):
        return f"{tag}[aria-label='{attrs['aria-label']}']"
    return tag


def extract_structured_elements(page: Page) -> dict[str, list[dict[str, Any]]]:
    raw_elements = page.evaluate(
        """
        () => {
          const interesting = Array.from(document.querySelectorAll(
            'button, a, input, textarea, select, label, [role], h1, h2, h3, h4, h5, h6, p, span'
          ));

          const visibleText = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
          const attrsFor = (node) => {
            const attrs = {};
            for (const name of ['id', 'name', 'type', 'placeholder', 'aria-label', 'role', 'href', 'data-testid']) {
              const value = node.getAttribute(name);
              if (value) attrs[name] = value;
            }
            return attrs;
          };
          const roleFor = (node) => {
            const tag = node.tagName.toLowerCase();
            const explicit = node.getAttribute('role');
            if (explicit) return explicit;
            if (tag === 'button') return 'button';
            if (tag === 'a') return 'link';
            if (['input', 'textarea', 'select'].includes(tag)) return 'field';
            if (tag === 'label') return 'label';
            if (/^h[1-6]$/.test(tag)) return 'heading';
            return 'text';
          };

          return interesting
            .map((node) => {
              const rect = node.getBoundingClientRect();
              const style = window.getComputedStyle(node);
              const visible = !!(rect.width && rect.height) &&
                style.visibility !== 'hidden' &&
                style.display !== 'none' &&
                Number(style.opacity || 1) > 0;
              return {
                role: roleFor(node),
                tag: node.tagName.toLowerCase(),
                text: visibleText(node).slice(0, 500),
                attributes: attrsFor(node),
                bounds: visible ? {
                  x: Math.round(rect.x),
                  y: Math.round(rect.y),
                  width: Math.round(rect.width),
                  height: Math.round(rect.height)
                } : null,
                visible
              };
            })
            .filter((item) => item.visible && (item.text || Object.keys(item.attributes).length));
        }
        """
    )

    grouped: dict[str, list[dict[str, Any]]] = {
        "buttons": [],
        "links": [],
        "fields": [],
        "labels": [],
        "headings": [],
        "text": [],
        "other": [],
    }

    for item in raw_elements:
        captured = CapturedElement(
            role=item["role"],
            tag=item["tag"],
            text=item.get("text", ""),
            selector_hint=build_selector_hint(item),
            attributes=item.get("attributes", {}),
            bounds=item.get("bounds"),
            visible=bool(item.get("visible")),
        )
        bucket = {
            "button": "buttons",
            "link": "links",
            "field": "fields",
            "textbox": "fields",
            "combobox": "fields",
            "checkbox": "fields",
            "radio": "fields",
            "label": "labels",
            "heading": "headings",
            "text": "text",
        }.get(captured.role, "other")
        grouped[bucket].append(asdict(captured))

    return grouped


def capture_page_state(
    url_path: str,
    page: Page | None = None,
    base_url: str | None = None,
    output_dir: Path = CAPTURE_DIR,
    ready_selector: str | None = None,
) -> PageState:
    global _global_page, _global_base_url
    if page is None:
        page = _global_page
    if base_url is None:
        base_url = _global_base_url or os.environ.get("APP_BASE_URL", "")

    if page is None:
        raise ValueError("Playwright Page object not set. Call login first or pass page explicitly.")
    if not base_url:
        raise ValueError("base_url not set. Pass base_url or set APP_BASE_URL environment variable.")

    normalized_path = normalize_url_path(url_path)
    target_url = urljoin(base_url.rstrip("/") + "/", normalized_path.lstrip("/"))
    basename = safe_filename_from_path(normalized_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    html_path = output_dir / f"{basename}.html"
    screenshot_path = output_dir / f"{basename}.png"
    json_path = output_dir / f"{basename}.json"

    logger.info("Capturing page state: %s", target_url)
    page.goto(target_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    wait_for_stable_page(page, ready_selector=ready_selector)

    html = page.locator("body").evaluate("node => node.innerHTML")
    html_path.write_text(html, encoding="utf-8")

    elements = extract_structured_elements(page)
    page.screenshot(path=str(screenshot_path), full_page=True, type="png")

    state = PageState(
        url=page.url,
        title=page.title(),
        captured_at_unix=time.time(),
        screenshot_path=str(screenshot_path.resolve()),
        html_path=str(html_path.resolve()),
        elements=elements,
    )
    json_path.write_text(json.dumps(asdict(state), indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Saved capture JSON: %s", json_path.resolve())
    logger.info("Saved screenshot: %s", screenshot_path.resolve())
    return state


def create_browser_context(browser: Browser, headless: bool) -> BrowserContext:
    context = browser.new_context(
        viewport={"width": 1440, "height": 1200},
        device_scale_factor=2,
        ignore_https_errors=False,
        java_script_enabled=True,
        reduced_motion="reduce",
    )
    context.set_default_timeout(ACTION_TIMEOUT_MS)
    context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
    return context


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Log in and capture structured browser page states.")
    parser.add_argument("--base-url", default=os.environ.get("APP_BASE_URL"), help="Application base URL.")
    parser.add_argument("--login-path", default=env_or_default("APP_LOGIN_PATH", DEFAULT_LOGIN_PATH), help="Login page path.")
    parser.add_argument("--email", default=env_or_default("APP_LOGIN_EMAIL", DEFAULT_EMAIL), help="Login email.")
    parser.add_argument("--password", default=os.environ.get("APP_LOGIN_PASSWORD"), help="Login password.")
    parser.add_argument(
        "--url-path",
        action="append",
        required=True,
        help="Authenticated page path to capture. Can be passed multiple times.",
    )
    parser.add_argument("--output-dir", default=str(CAPTURE_DIR), help="Folder for screenshots, HTML, and JSON.")
    parser.add_argument("--ready-selector", default=os.environ.get("DASHBOARD_READY_SELECTOR"), help="Selector that proves app loaded.")
    parser.add_argument("--headed", action="store_true", help="Run with a visible browser window for debugging.")
    parser.add_argument("--slow-mo", type=int, default=0, help="Slow browser actions by N milliseconds.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args()


def main() -> int:
    if load_dotenv is not None:
        load_dotenv()

    args = parse_args()
    configure_logging(args.verbose)

    if not args.base_url:
        logger.error("Missing APP_BASE_URL or --base-url.")
        return 1
    if not args.password:
        logger.error("Missing APP_LOGIN_PASSWORD or --password.")
        return 1

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed, slow_mo=args.slow_mo)
            context = create_browser_context(browser, headless=not args.headed)
            page = context.new_page()

            try:
                login(
                    page=page,
                    base_url=args.base_url,
                    login_path=args.login_path,
                    email=args.email,
                    password=args.password,
                    ready_selector=args.ready_selector,
                )

                for url_path in args.url_path:
                    capture_page_state(
                        page=page,
                        base_url=args.base_url,
                        url_path=url_path,
                        output_dir=Path(args.output_dir),
                        ready_selector=None,
                    )
            finally:
                context.close()
                browser.close()

    except KeyboardInterrupt:
        logger.error("Interrupted by user")
        return 130
    except PlaywrightTimeoutError as exc:
        logger.exception("Timed out while automating the browser: %s", exc)
        return 1
    except Exception as exc:
        logger.exception("Browser capture failed: %s", exc)
        return 1

    logger.info("Capture complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
