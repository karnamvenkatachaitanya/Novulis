"""
Async stateful Playwright scraper for WaiverPro compliance captures.

This module has two phases:
  1. login_and_save_state(...) caches authenticated browser storage in auth_state.json.
  2. scrape_page_state(...) opens a fresh context from auth_state.json for each secure page.

The explicit auth redirect guard prevents login-page DOMs from being sent to the LLM judge.
"""

from __future__ import annotations

import argparse
import asyncio
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

from playwright.async_api import Browser, Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


AUTH_STATE_PATH = Path("auth_state.json")
CAPTURE_DIR = Path("captured_states")
LOGIN_PATH = "/login"
DASHBOARD_LANDING_PATH = "/dashboard/my-applications"
NAVIGATION_TIMEOUT_MS = 30_000
POST_LOAD_SETTLE_MS = 1_000

logger = logging.getLogger("scraper")


@dataclass(frozen=True)
class PageCapture:
    target_url: str
    current_url: str
    title: str
    captured_at_unix: float
    screenshot_path: str
    html_path: str
    json_path: str
    inner_text: str
    dom: dict[str, list[dict[str, Any]]]


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def absolute_url(base_url: str, path_or_url: str) -> str:
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    return urljoin(base_url.rstrip("/") + "/", path_or_url.lstrip("/"))


def safe_filename(url: str) -> str:
    parsed = urlparse(url)
    raw = parsed.path.strip("/") or "home"
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw)
    return f"{clean}-{time.strftime('%Y%m%d-%H%M%S')}"


def is_secure_target(target_url: str) -> bool:
    return "/dashboard" in urlparse(target_url).path


async def wait_for_application_idle(page: Page) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        logger.warning("Timed out waiting for domcontentloaded at %s", page.url)

    try:
        await page.wait_for_load_state("networkidle", timeout=NAVIGATION_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        logger.warning("Network never reached idle at %s; continuing after timeout", page.url)

    await page.wait_for_timeout(POST_LOAD_SETTLE_MS)


async def login_and_save_state(
    browser: Browser,
    base_url: str,
    username: str,
    password: str,
    *,
    auth_state_path: Path = AUTH_STATE_PATH,
) -> None:
    """Log in once and persist Playwright storage state to auth_state.json."""
    context = await browser.new_context(
        viewport={"width": 1440, "height": 1200},
        device_scale_factor=2,
    )
    context.set_default_timeout(NAVIGATION_TIMEOUT_MS)
    context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
    page = await context.new_page()

    try:
        login_url = absolute_url(base_url, LOGIN_PATH)
        logger.info("Opening login page: %s", login_url)
        await page.goto(login_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)

        await page.fill("#email", username, timeout=NAVIGATION_TIMEOUT_MS)
        await page.fill("#password", password, timeout=NAVIGATION_TIMEOUT_MS)
        await page.click("[data-testid='btn-login']", timeout=NAVIGATION_TIMEOUT_MS)

        landing_url = absolute_url(base_url, DASHBOARD_LANDING_PATH)
        logger.info("Waiting for dashboard landing page: %s", landing_url)
        try:
            await page.wait_for_url(f"**{DASHBOARD_LANDING_PATH}**", timeout=NAVIGATION_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            await wait_for_application_idle(page)
            if DASHBOARD_LANDING_PATH not in page.url:
                raise RuntimeError(f"LOGIN_DID_NOT_REACH_DASHBOARD current_url={page.url}")

        await wait_for_application_idle(page)
        auth_state_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(auth_state_path))
        logger.info("Saved authenticated browser state to %s", auth_state_path.resolve())
    except Exception:
        logger.exception("Authentication state capture failed")
        raise
    finally:
        await context.close()


async def extract_dom_layout(page: Page) -> dict[str, list[dict[str, Any]]]:
    raw = await page.evaluate(
        """
        () => {
          const nodes = Array.from(document.querySelectorAll(
            'button, a, input, textarea, select, label, [role], h1, h2, h3, h4, h5, h6, p, span'
          ));
          const textFor = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
          const attrsFor = (node) => {
            const out = {};
            for (const name of ['id', 'name', 'type', 'placeholder', 'aria-label', 'role', 'href', 'data-testid']) {
              const value = node.getAttribute(name);
              if (value) out[name] = value;
            }
            return out;
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
          const selectorFor = (node) => {
            if (node.id) return `#${node.id}`;
            const testid = node.getAttribute('data-testid');
            if (testid) return `[data-testid="${testid}"]`;
            const name = node.getAttribute('name');
            if (name) return `${node.tagName.toLowerCase()}[name="${name}"]`;
            const aria = node.getAttribute('aria-label');
            if (aria) return `${node.tagName.toLowerCase()}[aria-label="${aria}"]`;
            return node.tagName.toLowerCase();
          };
          return nodes.map((node) => {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            const visible = !!(rect.width && rect.height) &&
              style.display !== 'none' &&
              style.visibility !== 'hidden' &&
              Number(style.opacity || 1) > 0;
            return {
              role: roleFor(node),
              tag: node.tagName.toLowerCase(),
              text: textFor(node).slice(0, 500),
              selector: selectorFor(node),
              attributes: attrsFor(node),
              bounds: visible ? {
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                width: Math.round(rect.width),
                height: Math.round(rect.height)
              } : null,
              visible
            };
          }).filter((item) => item.visible && (item.text || Object.keys(item.attributes).length));
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
    for item in raw:
        role = item.get("role")
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
        }.get(role, "other")
        grouped[bucket].append(item)
    return grouped


async def scrape_page_state(
    browser: Browser,
    target_url: str,
    *,
    auth_state_path: Path = AUTH_STATE_PATH,
    output_dir: Path = CAPTURE_DIR,
) -> PageCapture:
    """Scrape a secure page from cached auth state and fail fast on login redirects."""
    if not auth_state_path.exists():
        raise RuntimeError(f"AUTH_STATE_MISSING path={auth_state_path}")

    context = await browser.new_context(
        storage_state=str(auth_state_path),
        viewport={"width": 1440, "height": 1200},
        device_scale_factor=2,
    )
    context.set_default_timeout(NAVIGATION_TIMEOUT_MS)
    context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
    page = await context.new_page()

    try:
        logger.info("Scraping target page: %s", target_url)
        await page.goto(target_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        await wait_for_application_idle(page)

        current_url = page.url
        if "/login" in current_url and is_secure_target(target_url):
            raise RuntimeError("AUTH_REDIRECT_TRIGGERED")

        output_dir.mkdir(parents=True, exist_ok=True)
        basename = safe_filename(target_url)
        screenshot_path = output_dir / f"{basename}.png"
        html_path = output_dir / f"{basename}.html"
        json_path = output_dir / f"{basename}.json"

        body = page.locator("body")
        inner_text = await body.inner_text(timeout=NAVIGATION_TIMEOUT_MS)
        html = await body.evaluate("node => node.innerHTML")
        dom = await extract_dom_layout(page)
        await page.screenshot(path=str(screenshot_path), full_page=True, type="png")
        html_path.write_text(html, encoding="utf-8")

        capture = PageCapture(
            target_url=target_url,
            current_url=current_url,
            title=await page.title(),
            captured_at_unix=time.time(),
            screenshot_path=str(screenshot_path.resolve()),
            html_path=str(html_path.resolve()),
            json_path=str(json_path.resolve()),
            inner_text=inner_text,
            dom=dom,
        )
        json_path.write_text(json.dumps(asdict(capture), indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Saved page capture: %s", json_path.resolve())
        return capture
    except RuntimeError:
        raise
    except Exception:
        logger.exception("Failed to scrape target page: %s", target_url)
        raise
    finally:
        await context.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cache auth state or scrape a page using saved auth.")
    parser.add_argument("--base-url", default=os.environ.get("APP_BASE_URL"))
    parser.add_argument("--username", default=os.environ.get("APP_LOGIN_EMAIL", "m@example.com"))
    parser.add_argument("--password", default=os.environ.get("APP_LOGIN_PASSWORD"))
    parser.add_argument("--target-url", help="Full target URL to scrape after auth state exists.")
    parser.add_argument("--login", action="store_true", help="Only refresh auth_state.json.")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


async def async_main() -> int:
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
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=not args.headed)
            try:
                if args.login or not args.target_url:
                    await login_and_save_state(browser, args.base_url, args.username, args.password)
                if args.target_url:
                    await scrape_page_state(browser, args.target_url)
            finally:
                await browser.close()
    except Exception as exc:
        logger.exception("Scraper command failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(async_main()))
