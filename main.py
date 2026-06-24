"""
End-to-end Documentation Compliance Agent orchestration.

Runs:
  Part 2: Browser capture with scraper.py
  Part 3: Guideline retrieval with retrieval_engine.py
  Part 4: AI compliance comparison with compliance_agent.py

If violations are found, sends an email alert with JSON reports and screenshots.

Required environment variables for the pipeline:
  APP_BASE_URL
  APP_LOGIN_PASSWORD
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY

Required environment variables for email alerts:
  SMTP_HOST
  SMTP_PORT
  SMTP_USERNAME
  SMTP_PASSWORD
  ALERT_FROM
  ALERT_TO

Example:
  python main.py --url-path /dashboard --url-path /workspace
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import ssl
import sys
import time
from dataclasses import asdict
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from compliance_agent import run_compliance_check
from retrieval_engine import retrieve_matching_rules
from scraper import create_browser_context, login, capture_page_state

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional local convenience
    load_dotenv = None


CAPTURE_DIR = Path("captured_states")
RETRIEVAL_DIR = Path("retrieved_context")
REPORT_DIR = Path("reports")
DEFAULT_LOGIN_PATH = "/login"
DEFAULT_EMAIL = "m@example.com"


logger = logging.getLogger("main")


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def env_required(name: str) -> str:
    value = os.environ.get(name)
    if not value or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def parse_csv_env(name: str) -> list[str]:
    value = os.environ.get(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]


def normalize_url_path(url_path: str) -> str:
    return url_path if url_path.startswith("/") else f"/{url_path}"


def safe_path_name(url_path: str) -> str:
    clean = "".join(char if char.isalnum() or char in "._-" else "_" for char in url_path.strip("/"))
    return clean or "home"


def page_state_to_search_text(page_state: dict[str, Any]) -> str:
    parts = [str(page_state.get("title", ""))]
    elements = page_state.get("elements", {})
    if isinstance(elements, dict):
        for group in elements.values():
            if isinstance(group, list):
                for item in group:
                    if isinstance(item, dict):
                        text = str(item.get("text", "")).strip()
                        if text:
                            parts.append(text)
                        attrs = item.get("attributes", {})
                        if isinstance(attrs, dict):
                            parts.extend(str(value) for value in attrs.values() if value)
    return " ".join(parts).strip()


def has_compliance_issue(report: list[dict[str, Any]]) -> bool:
    return len(report) > 0


def summarize_findings(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for report in reports:
        discrepancies = report.get("discrepancies", [])
        if has_compliance_issue(discrepancies):
            findings.append(report)
    return findings


def build_email(findings: list[dict[str, Any]], attachments: list[Path], run_id: str) -> EmailMessage:
    sender = env_required("ALERT_FROM")
    recipients = [item.strip() for item in env_required("ALERT_TO").split(",") if item.strip()]
    if not recipients:
        raise RuntimeError("ALERT_TO must contain at least one recipient.")

    total_issues = sum(len(item.get("discrepancies", [])) for item in findings)
    subject = f"Documentation Compliance Alert: {total_issues} compliance issue(s) detected"

    lines = [
        "Documentation Compliance Automation Agent Alert",
        "===============================================",
        f"Run ID: {run_id}",
        f"Pages Flagged: {len(findings)}",
        f"Total Violations: {total_issues}",
        "",
    ]
    for page in findings:
        lines.extend([
            f"Page: {page.get('page_title')} ({page.get('url_path')})",
            f"URL: {page.get('page_url')}",
            f"Screenshot: {Path(page.get('screenshot_path')).name}",
            "Violations:",
        ])
        for disc in page.get('discrepancies', []):
            lines.extend([
                f"  - Element: {disc.get('element_selector')}",
                f"    Expected: {disc.get('expected_rule_behavior')}",
                f"    Observed: {disc.get('observed_live_behavior')}",
                f"    Severity: {disc.get('severity_level')}",
                "",
            ])

    html_parts = [
        "<html>",
        "<head>",
        "<style>",
        "  body { font-family: Arial, sans-serif; color: #333; line-height: 1.6; }",
        "  h2 { color: #d9534f; border-bottom: 2px solid #d9534f; padding-bottom: 8px; }",
        "  h3 { color: #333; margin-top: 25px; }",
        "  table { border-collapse: collapse; width: 100%; margin-top: 10px; margin-bottom: 20px; box-shadow: 0 2px 3px rgba(0,0,0,0.05); }",
        "  th, td { border: 1px solid #dddddd; text-align: left; padding: 12px; }",
        "  th { background-color: #f8f9fa; font-weight: bold; color: #495057; }",
        "  tr:nth-child(even) { background-color: #f8f9fa; }",
        "  .severity-High { color: #721c24; font-weight: bold; background-color: #f8d7da; border-color: #f5c6cb; }",
        "  .severity-Medium { color: #856404; font-weight: bold; background-color: #fff3cd; border-color: #ffeeba; }",
        "  .severity-Low { color: #0c5460; font-weight: bold; background-color: #d1ecf1; border-color: #bee5eb; }",
        "  .element-name { font-family: monospace; background-color: #f1f3f5; padding: 2px 4px; border-radius: 3px; }",
        "</style>",
        "</head>",
        "<body>",
        "<h2>Documentation Compliance Alert</h2>",
        f"<p><strong>Run ID:</strong> {run_id}</p>",
        f"<p><strong>Pages Flagged:</strong> {len(findings)}<br><strong>Total Violations:</strong> {total_issues}</p>",
    ]

    for page in findings:
        html_parts.extend([
            f"<h3>Page: {page.get('page_title')} (<a href='{page.get('page_url')}'>{page.get('url_path')}</a>)</h3>",
            f"<p><strong>Screenshot File:</strong> <code>{Path(page.get('screenshot_path')).name}</code></p>",
            "<table>",
            "  <thead>",
            "    <tr>",
            "      <th>Element Selector</th>",
            "      <th>Expected Rule Behavior</th>",
            "      <th>Observed Live Behavior</th>",
            "      <th>Severity</th>",
            "    </tr>",
            "  </thead>",
            "  <tbody>",
        ])
        for disc in page.get('discrepancies', []):
            sev = disc.get('severity_level', 'Medium')
            html_parts.extend([
                "    <tr>",
                f"      <td><span class='element-name'>{disc.get('element_selector')}</span></td>",
                f"      <td>{disc.get('expected_rule_behavior')}</td>",
                f"      <td>{disc.get('observed_live_behavior')}</td>",
                f"      <td class='severity-{sev}'>{sev}</td>",
                "    </tr>",
            ])
        html_parts.extend([
            "  </tbody>",
            "</table>",
        ])

    html_parts.extend([
        "<p>Please see the attached JSON files and screenshots for full execution logs and inspection details.</p>",
        "</body>",
        "</html>"
    ])

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content("\n".join(lines))
    message.add_alternative("\n".join(html_parts), subtype="html")

    for path in attachments:
        if not path.exists() or not path.is_file():
            logger.warning("Skipping missing email attachment: %s", path)
            continue
        data = path.read_bytes()
        extension = path.suffix.lower()
        if extension == ".json":
            maintype, subtype = "application", "json"
        elif extension == ".png":
            maintype, subtype = "image", "png"
        elif extension in {".jpg", ".jpeg"}:
            maintype, subtype = "image", "jpeg"
        else:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)

    return message


def send_email_alert(message: EmailMessage, use_ssl: bool) -> None:
    host = env_required("SMTP_HOST")
    port = int(env_required("SMTP_PORT"))
    username = env_required("SMTP_USERNAME")
    password = env_required("SMTP_PASSWORD")
    context = ssl.create_default_context()

    logger.info("Sending compliance alert email through SMTP host %s", host)
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=300) as server:
            server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=300) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(username, password)
            server.send_message(message)
    logger.info("Email alert sent")


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def run_pipeline(args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[Path], str]:
    run_id = time.strftime("%Y%m%d-%H%M%S")
    url_paths = [normalize_url_path(path) for path in args.url_path]
    if not url_paths:
        raise RuntimeError("At least one --url-path is required.")

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    RETRIEVAL_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    reports: list[dict[str, Any]] = []
    attachments: list[Path] = []

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

            for url_path in url_paths:
                logger.info("Starting compliance sweep for %s", url_path)
                page_state = capture_page_state(
                    url_path=url_path,
                    page=page,
                    base_url=args.base_url,
                    output_dir=CAPTURE_DIR,
                )
                page_state_dict = asdict(page_state)
                screenshot_path = Path(str(page_state.screenshot_path))

                search_text = page_state_to_search_text(page_state_dict)
                rules = retrieve_matching_rules(
                    page_text=search_text,
                    target_url_path=url_path,
                    model_name=args.embedding_model,
                    match_count=args.match_count,
                    similarity_threshold=args.similarity_threshold,
                )
                rules_payload = [asdict(rule) for rule in rules]
                rules_path = RETRIEVAL_DIR / f"{safe_path_name(url_path)}-{run_id}-rules.json"
                save_json(rules_path, rules_payload)

                report = run_compliance_check(
                    page_state=page_state_dict,
                    guidelines=rules_payload,
                    model_name=args.compliance_model,
                    dtype=args.dtype,
                    max_new_tokens=args.max_new_tokens,
                    temperature=args.temperature,
                    repair_attempts=args.repair_attempts,
                )
                report_path = REPORT_DIR / f"{safe_path_name(url_path)}-{run_id}-compliance.json"
                save_json(report_path, report)

                reports.append({
                    "url_path": url_path,
                    "page_title": page_state.title,
                    "page_url": page_state.url,
                    "screenshot_path": str(screenshot_path),
                    "discrepancies": report
                })
                attachments.extend([rules_path, report_path, screenshot_path])
                logger.info("Completed compliance sweep for %s with %d violations", url_path, len(report))
        finally:
            context.close()
            browser.close()

    return reports, attachments, run_id


def parse_args() -> argparse.Namespace:
    default_paths = parse_csv_env("COMPLIANCE_URL_PATHS")

    parser = argparse.ArgumentParser(description="Run the full Documentation Compliance Agent pipeline.")
    parser.add_argument("--base-url", default=os.environ.get("APP_BASE_URL"), help="Application base URL.")
    parser.add_argument("--login-path", default=env_or_default("APP_LOGIN_PATH", DEFAULT_LOGIN_PATH))
    parser.add_argument("--email", default=env_or_default("APP_LOGIN_EMAIL", DEFAULT_EMAIL))
    parser.add_argument("--password", default=os.environ.get("APP_LOGIN_PASSWORD"))
    parser.add_argument("--url-path", action="append", default=default_paths, help="Page path to sweep. Repeatable.")
    parser.add_argument("--ready-selector", default=os.environ.get("DASHBOARD_READY_SELECTOR"))
    parser.add_argument("--headed", action="store_true", help="Show browser window for debugging.")
    parser.add_argument("--slow-mo", type=int, default=0, help="Slow browser actions by N milliseconds.")

    parser.add_argument("--embedding-model", default="sentence-transformers/all-MiniLM-L6-v2")
    parser.add_argument("--match-count", type=int, default=5)
    parser.add_argument("--similarity-threshold", type=float, default=0.2)

    parser.add_argument("--compliance-model", default=os.environ.get("COMPLIANCE_MODEL", "mistralai/Mistral-7B-Instruct-v0.3"))
    parser.add_argument("--dtype", choices=["auto", "float16", "bfloat16", "float32"], default=os.environ.get("COMPLIANCE_DTYPE", "auto"))
    parser.add_argument("--max-new-tokens", type=int, default=1_800)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--repair-attempts", type=int, default=1)

    parser.add_argument("--alert-on-needs-review", action="store_true", help="Email when the AI marks a page needs_review.")
    parser.add_argument("--no-email", action="store_true", help="Run the sweep without sending alert emails.")
    parser.add_argument("--smtp-starttls", action="store_true", help="Use STARTTLS instead of SMTP over SSL.")
    parser.add_argument("--verbose", action="store_true")
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
        reports, attachments, run_id = run_pipeline(args)
        findings = summarize_findings(reports)

        if not findings:
            logger.info("No compliance violations detected. No alert sent.")
            return 0

        logger.warning("Compliance issues detected on %s page(s)", len(findings))
        if args.no_email:
            logger.info("--no-email enabled. Alert email skipped.")
            return 2

        message = build_email(findings=findings, attachments=attachments, run_id=run_id)
        send_email_alert(message, use_ssl=not args.smtp_starttls)
        return 2
    except KeyboardInterrupt:
        logger.error("Interrupted by user")
        return 130
    except PlaywrightTimeoutError as exc:
        logger.exception("Browser automation timed out: %s", exc)
        return 1
    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
