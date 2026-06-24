"""
src/utils/browser_pool.py
Stateful session caching and secure route scraping using async Playwright.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger("browser_pool")

AUTH_STATE_PATH = Path("auth_state.json")
CAPTURE_DIR = Path("captured_states")
NAVIGATION_TIMEOUT_MS = 30_000
POST_LOAD_SETTLE_MS = 1_000


def absolute_url(base_url: str, path_or_url: str) -> str:
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    return urljoin(base_url.rstrip("/") + "/", path_or_url.lstrip("/"))


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


async def login_and_cache_session(
    browser: Browser,
    base_url: str,
    username: str,
    password: str,
) -> None:
    """Navigate to login page, authenticate, and cache storage state in auth_state.json."""
    logger.info("Initializing session cache at %s", base_url)
    context = await browser.new_context(
        viewport={"width": 1440, "height": 1200},
        device_scale_factor=2,
    )
    context.set_default_timeout(NAVIGATION_TIMEOUT_MS)
    context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
    page = await context.new_page()

    try:
        login_url = absolute_url(base_url, "/login")
        logger.info("Navigating to login page: %s", login_url)
        await page.goto(login_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)

        await page.fill("#email", username, timeout=NAVIGATION_TIMEOUT_MS)
        await page.fill("#password", password, timeout=NAVIGATION_TIMEOUT_MS)
        await page.click("[data-testid='btn-login']", timeout=NAVIGATION_TIMEOUT_MS)

        # Wait for dashboard navigation
        logger.info("Waiting for redirect to dashboard my-applications page...")
        try:
            await page.wait_for_url("**/dashboard/my-applications", timeout=NAVIGATION_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            await wait_for_application_idle(page)
            if "/dashboard/my-applications" not in page.url:
                raise RuntimeError(f"Login failed to reach dashboard. Current URL: {page.url}")

        await wait_for_application_idle(page)
        AUTH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(AUTH_STATE_PATH))
        logger.info("Auth state cached successfully at %s", AUTH_STATE_PATH.resolve())
    except Exception as exc:
        logger.exception("login_and_cache_session failed: %s", exc)
        raise
    finally:
        await context.close()


async def extract_dom_layout(page: Page) -> dict[str, list[dict[str, Any]]]:
    """Extract interactive and structural elements from the page for auditing."""
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


async def secure_scrape_route(
    browser: Browser,
    target_url: str,
) -> dict[str, Any]:
    """Scrape a secure page using cached auth state and fail fast if redirected to login."""
    if not AUTH_STATE_PATH.exists():
        raise RuntimeError("AUTH_STATE_MISSING")

    logger.info("Initializing context for route: %s", target_url)
    context = await browser.new_context(
        storage_state=str(AUTH_STATE_PATH),
        viewport={"width": 1440, "height": 1200},
        device_scale_factor=2,
    )
    context.set_default_timeout(NAVIGATION_TIMEOUT_MS)
    context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
    page = await context.new_page()

    try:
        logger.info("Navigating to target URL: %s", target_url)
        await page.goto(target_url, wait_until="networkidle", timeout=NAVIGATION_TIMEOUT_MS)
        await wait_for_application_idle(page)

        # Anti-hallucination check
        current_url = page.url
        logger.debug("Current loaded URL is %s", current_url)
        if "/login" in current_url and is_secure_target(target_url):
            logger.error("Auth redirect detected. Page redirected to login screen.")
            raise RuntimeError("AUTH_REDIRECT_TRIGGERED")

        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        parsed = urlparse(target_url)
        path_slug = (parsed.path.strip("/") or "home").replace("/", "_")
        
        screenshot_path = CAPTURE_DIR / f"{path_slug}-{timestamp}.png"
        await page.screenshot(path=str(screenshot_path), full_page=True, type="png")

        body_locator = page.locator("body")
        inner_text = await body_locator.inner_text(timeout=NAVIGATION_TIMEOUT_MS)
        dom = await extract_dom_layout(page)

        payload = {
            "target_url": target_url,
            "current_url": current_url,
            "title": await page.title(),
            "captured_at_unix": time.time(),
            "screenshot_path": str(screenshot_path.resolve()),
            "inner_text": inner_text,
            "dom": dom,
        }
        return payload
    except RuntimeError:
        raise
    except Exception as exc:
        logger.exception("Secure scrape failed for %s: %s", target_url, exc)
        raise
    finally:
        await context.close()
