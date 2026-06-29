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
        logger.debug("Reached networkidle state at %s", page.url)
    except PlaywrightTimeoutError:
        logger.warning("Network never reached idle at %s; continuing after timeout", page.url)

    # Smart-waiting: Wait for loading spinners/placeholders to disappear
    try:
        loader_selectors = [
            ".loading", ".spinner", ".loader", "[data-testid*='loader']", 
            "[data-testid*='spinner']", ".skeleton", ".animate-pulse"
        ]
        for selector in loader_selectors:
            loc = page.locator(selector)
            if await loc.count() > 0:
                logger.info("Waiting for loading placeholder '%s' to resolve...", selector)
                try:
                    await loc.first.wait_for(state="hidden", timeout=3000)
                except PlaywrightTimeoutError:
                    logger.warning("Timeout waiting for placeholder '%s' to resolve, proceeding", selector)
    except Exception as exc:
        logger.debug("Error during smart-waiting: %s", exc)

    # Mandatory safe render delay
    await page.wait_for_timeout(3000)



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
        r"""
        () => {
          const nodes = Array.from(document.querySelectorAll(
            'button, a, input, textarea, select, label, [role], h1, h2, h3, h4, h5, h6, p, span, div, li, td'
          )).filter(node => {
            const tag = node.tagName.toLowerCase();
            if (['div', 'li', 'td'].includes(tag)) {
              const hasMatchChild = node.querySelector('button, a, input, textarea, select, label, h1, h2, h3, h4, h5, h6, p, span, div, li, td');
              return !hasMatchChild && (node.innerText || node.textContent || '').trim().length > 0;
            }
            return true;
          });
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
            
            const path = [];
            let current = node;
            while (current && current.nodeType === Node.ELEMENT_NODE) {
              const tag = current.tagName.toLowerCase();
              if (current.id) {
                path.unshift(`#${current.id}`);
                break;
              }
              const currentTestid = current.getAttribute('data-testid');
              if (currentTestid) {
                path.unshift(`[data-testid="${currentTestid}"]`);
                break;
              }
              
              let selector = tag;
              if (current.className && typeof current.className === 'string') {
                const classes = current.className.trim().split(/\s+/).filter(c => c && !c.includes(':')).join('.');
                if (classes) {
                  selector = `${tag}.${classes}`;
                }
              }
              
              if (current.parentNode) {
                const siblings = Array.from(current.parentNode.children);
                const sameTagSiblings = siblings.filter(s => s.tagName === current.tagName);
                if (sameTagSiblings.length > 1) {
                  const idx = sameTagSiblings.indexOf(current) + 1;
                  selector += `:nth-of-type(${idx})`;
                }
              }
              
              path.unshift(selector);
              current = current.parentNode;
              if (current && current.tagName.toLowerCase() === 'body') {
                break;
              }
            }
            return path.join(' > ');
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
              visible,
              styles: {
                backgroundColor: style.backgroundColor,
                color: style.color,
                borderStyle: style.borderStyle,
                borderWidth: style.borderWidth,
                borderColor: style.borderColor,
                borderVisible: style.borderStyle !== 'none' && parseFloat(style.borderWidth || '0') > 0
              }
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
    """Scrape a secure or public page with custom interactive workflows."""
    context_args = {
        "viewport": {"width": 1440, "height": 1200},
        "device_scale_factor": 2,
    }
    if is_secure_target(target_url):
        if not auth_state_path.exists():
            raise RuntimeError(f"AUTH_STATE_MISSING path={auth_state_path}")
        context_args["storage_state"] = str(auth_state_path)

    context = await browser.new_context(**context_args)
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
        
        # Take initial base screenshot
        await page.screenshot(path=str(screenshot_path), full_page=True, type="png")

        # Custom interactive actions for Waiver Application wizard
        parsed_url = urlparse(target_url)
        path = parsed_url.path.rstrip("/")
        
        if path == "/dashboard/my-applications":
            logger.info("Executing interactive actions for Waiver Application wizard...")
            try:
                new_app_btn = page.locator("[data-testid='btn-new-application']")
                if await new_app_btn.count() > 0:
                    await new_app_btn.click()
                    await page.wait_for_timeout(1000)
                    step1_dom = await extract_dom_layout(page)
                    
                    # Select type and click next
                    await page.click("[data-testid*='waiver-type-radio-2a7afc48']", force=True)
                    await page.click("[data-testid='btn-next']", force=True)
                    await page.wait_for_timeout(1000)
                    step2_dom = await extract_dom_layout(page)
                    
                    # Search facility & select facility and type
                    search_input = page.locator("[data-testid='input-facility-search']")
                    await search_input.click(force=True)
                    await search_input.fill("California")
                    await page.wait_for_timeout(1000)
                    await page.click("[data-testid='facility-option-0']", force=True)
                    await page.wait_for_timeout(500)
                    
                    type_select = page.locator("[data-testid='input-facility-type']")
                    await type_select.click(force=True)
                    await page.wait_for_timeout(1000)
                    await page.click("[data-testid*='facility-type-option-']", force=True)
                    await page.wait_for_timeout(1000)
                    
                    # Click next to step 3
                    await page.click("[data-testid='btn-next']", force=True)
                    await page.wait_for_timeout(2000)
                    step3_dom = await extract_dom_layout(page)
                    
                    # Merge DOM structures
                    for key in dom.keys():
                        dom[key].extend(step1_dom.get(key, []))
                        dom[key].extend(step2_dom.get(key, []))
                        dom[key].extend(step3_dom.get(key, []))
                        
                    # Re-capture inner text, html and screenshot for evidence reporting
                    inner_text += "\n" + await body.inner_text(timeout=NAVIGATION_TIMEOUT_MS)
                    html = await body.evaluate("node => node.innerHTML")
                    await page.screenshot(path=str(screenshot_path), full_page=True, type="png")
                    
                    # Close sheet to clean up
                    await page.click("[data-testid='sheet-close']", force=True)
                    await page.wait_for_timeout(500)
            except Exception as exc:
                logger.error("Waiver Application overlay wizard interactive actions failed: %s", exc)

        # Custom interactive actions for Support Tickets overlay
        elif path == "/dashboard/tickets":
            logger.info("Executing interactive actions for Support Tickets overlay...")
            try:
                new_ticket_btn = page.locator("[data-testid='btn-new-ticket']")
                if await new_ticket_btn.count() > 0:
                    await new_ticket_btn.click()
                    await page.wait_for_timeout(1500)
                    ticket_dom = await extract_dom_layout(page)
                    
                    # Merge Ticket DOM structures
                    for key in dom.keys():
                        dom[key].extend(ticket_dom.get(key, []))
                        
                    # Re-capture inner text, html and screenshot
                    inner_text += "\n" + await body.inner_text(timeout=NAVIGATION_TIMEOUT_MS)
                    html = await body.evaluate("node => node.innerHTML")
                    await page.screenshot(path=str(screenshot_path), full_page=True, type="png")
                    
                    # Close ticket drawer
                    await page.click("[data-testid='sheet-close']", force=True)
                    await page.wait_for_timeout(500)
            except Exception as exc:
                logger.error("Support tickets overlay interactive actions failed: %s", exc)

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
    parser.add_argument("--username", default=os.environ.get("APP_LOGIN_EMAIL", "admin@gmail.com"))
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
