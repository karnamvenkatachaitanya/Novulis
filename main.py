"""
WaiverPro Documentation Compliance Automation Agent.

Pipeline:
  1. Ensure auth_state.json exists, or refresh it with --force-login.
  2. Scrape authenticated dashboard pages using saved Playwright state.
  3. Retrieve matching PDF guideline chunks from Supabase vector search.
  4. Judge compliance through compliance_agent.py.
  5. Send a secure SMTP alert with JSON reports and screenshots when issues exist.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import smtplib
import ssl
import sys
import time
from dataclasses import asdict
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Any

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from compliance_agent import run_compliance_check
from github_client import create_github_issue
from retrieval_engine import retrieve_matching_rules
from scraper import AUTH_STATE_PATH, PageCapture, absolute_url, login_and_save_state, scrape_page_state

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


DEFAULT_TARGET_PATHS = [
    "/dashboard/my-applications",
    "/dashboard/facilities",
    "/dashboard/action-items",
    "/dashboard/faqs",
]
RETRIEVAL_DIR = Path("retrieved_context")
REPORT_DIR = Path("reports")
DEFAULT_EMAIL = "m@example.com"

logger = logging.getLogger("main")


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def env_required(name: str) -> str:
    value = os.environ.get(name)
    if not value or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def parse_csv_paths(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def normalize_path(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def safe_path_name(path: str) -> str:
    clean = "".join(char if char.isalnum() or char in "._-" else "_" for char in path.strip("/"))
    return clean or "home"


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def capture_to_search_text(capture: PageCapture) -> str:
    parts = [capture.title, capture.inner_text]
    for group in capture.dom.values():
        for item in group:
            text = str(item.get("text", "")).strip()
            if text:
                parts.append(text)
            attrs = item.get("attributes", {})
            if isinstance(attrs, dict):
                parts.extend(str(value) for value in attrs.values() if value)
    return " ".join(parts).strip()


def guidelines_to_text(guidelines: list[dict[str, Any]]) -> str:
    return json.dumps(guidelines, indent=2, ensure_ascii=False)


def critical_report(
    *,
    target_path: str,
    target_url: str,
    message: str,
    screenshot_path: str | None = None,
) -> dict[str, Any]:
    return {
        "target_path": target_path,
        "target_url": target_url,
        "page_url": target_url,
        "page_title": "Scrape or pipeline failure",
        "screenshot_path": screenshot_path,
        "scrape_error_flag": message,
        "findings": [
            {
                "element_selector": "body",
                "expected_behavior": f"{target_path} should load as an authenticated WaiverPro application page.",
                "observed_behavior": f"CRITICAL COMPLIANCE FAILURE: {message}",
                "severity": "critical",
            }
        ],
    }


async def retrieve_guidelines_for_capture(
    capture: PageCapture,
    target_path: str,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    search_text = capture_to_search_text(capture)
    rules = await asyncio.to_thread(
        retrieve_matching_rules,
        search_text,
        target_path,
        model_name=args.embedding_model,
        rpc_name=args.rpc_name,
        match_count=args.match_count,
        similarity_threshold=args.similarity_threshold,
    )
    return [asdict(rule) for rule in rules]


async def audit_target_path(browser: Any, base_url: str, target_path: str, run_id: str, args: argparse.Namespace) -> tuple[dict[str, Any], list[Path]]:
    target_path = normalize_path(target_path)
    target_url = absolute_url(base_url, target_path)
    attachments: list[Path] = []
    logger.info("Starting audit for %s", target_path)

    try:
        capture = await scrape_page_state(browser, target_url)
    except RuntimeError as exc:
        if str(exc) == "AUTH_REDIRECT_TRIGGERED":
            findings = run_compliance_check(
                target_path=target_path,
                live_dom_json={},
                retrieved_guidelines_text="",
                scrape_error_flag="AUTH_REDIRECT_TRIGGERED",
                model_name=args.compliance_model,
            )
            report = {
                "target_path": target_path,
                "target_url": target_url,
                "page_url": target_url,
                "page_title": "Authentication redirect",
                "screenshot_path": None,
                "scrape_error_flag": "AUTH_REDIRECT_TRIGGERED",
                "findings": findings,
            }
            report_path = REPORT_DIR / f"{safe_path_name(target_path)}-{run_id}-compliance.json"
            save_json(report_path, report)
            attachments.append(report_path)
            logger.error("Auth redirect detected for %s; skipped DOM extraction and LLM", target_path)
            return report, attachments
        raise
    except (PlaywrightTimeoutError, Exception) as exc:
        logger.exception("Scrape failed for %s", target_path)
        report = critical_report(target_path=target_path, target_url=target_url, message=f"Scrape failed: {exc}")
        report_path = REPORT_DIR / f"{safe_path_name(target_path)}-{run_id}-compliance.json"
        save_json(report_path, report)
        attachments.append(report_path)
        return report, attachments

    capture_payload = asdict(capture)
    attachments.extend([Path(capture.screenshot_path), Path(capture.json_path)])

    try:
        guidelines = await retrieve_guidelines_for_capture(capture, target_path, args)
        guidelines_path = RETRIEVAL_DIR / f"{safe_path_name(target_path)}-{run_id}-rules.json"
        save_json(guidelines_path, guidelines)
        attachments.append(guidelines_path)
    except Exception as exc:
        logger.exception("Guideline retrieval failed for %s", target_path)
        report = critical_report(
            target_path=target_path,
            target_url=target_url,
            message=f"Guideline retrieval failed: {exc}",
            screenshot_path=capture.screenshot_path,
        )
        report_path = REPORT_DIR / f"{safe_path_name(target_path)}-{run_id}-compliance.json"
        save_json(report_path, report)
        attachments.append(report_path)
        return report, attachments

    try:
        findings = await asyncio.to_thread(
            run_compliance_check,
            target_path,
            capture_payload,
            guidelines_to_text(guidelines),
            None,
            model_name=args.compliance_model,
            max_new_tokens=args.max_new_tokens,
            temperature=args.temperature,
            repair_attempts=args.repair_attempts,
        )
        report = {
            "target_path": target_path,
            "target_url": target_url,
            "page_url": capture.current_url,
            "page_title": capture.title,
            "screenshot_path": capture.screenshot_path,
            "scrape_error_flag": None,
            "findings": findings,
        }
    except Exception as exc:
        logger.exception("LLM compliance judge failed for %s", target_path)
        report = critical_report(
            target_path=target_path,
            target_url=target_url,
            message=f"LLM compliance judge failed: {exc}",
            screenshot_path=capture.screenshot_path,
        )

    report_path = REPORT_DIR / f"{safe_path_name(target_path)}-{run_id}-compliance.json"
    save_json(report_path, report)
    attachments.append(report_path)
    logger.info("Completed audit for %s with %d finding(s)", target_path, len(report.get("findings", [])))
    return report, attachments


def report_has_issues(report: dict[str, Any]) -> bool:
    findings = report.get("findings", [])
    return isinstance(findings, list) and len(findings) > 0


def severity_rank(severity: str) -> int:
    return {
        "critical": 4,
        "high": 3,
        "medium": 2,
        "low": 1,
    }.get(severity.strip().lower(), 2)


def worst_severity(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return "low"
    return max((str(item.get("severity", "medium")).lower() for item in findings), key=severity_rank)


def github_labels_for_findings(findings: list[dict[str, Any]]) -> list[str]:
    severity = worst_severity(findings)
    labels = ["bug", "documentation-compliance"]
    if severity == "critical":
        labels.append("compliance-critical")
    elif severity == "high":
        labels.append("compliance-high")
    elif severity == "low":
        labels.append("low-priority")
    else:
        labels.append("compliance-medium")
    return labels


def build_github_issue_title(report: dict[str, Any]) -> str:
    target_path = str(report.get("target_path") or "unknown-path")
    return f"Compliance Violation: {target_path} - Missing/Incorrect elements"


def build_github_issue_body(report: dict[str, Any], run_id: str) -> str:
    findings = report.get("findings", [])
    screenshot = report.get("screenshot_path") or "No screenshot captured"
    lines = [
        "## Compliance Discrepancy Report",
        "",
        f"**Run ID:** `{run_id}`",
        f"**Target Path:** `{report.get('target_path')}`",
        f"**Page URL:** {report.get('page_url') or report.get('target_url')}",
        f"**Screenshot File Reference:** `{screenshot}`",
        "",
        "## Findings",
        "",
    ]

    if not isinstance(findings, list) or not findings:
        lines.append("No individual findings were included in the report payload.")
        return "\n".join(lines)

    for index, finding in enumerate(findings, start=1):
        lines.extend(
            [
                f"### {index}. {finding.get('element_selector', 'Unknown element')}",
                "",
                f"**Expected Behavior:** {finding.get('expected_behavior', 'Not specified')}",
                "",
                f"**Observed Behavior:** {finding.get('observed_behavior', 'Not specified')}",
                "",
                f"**Severity:** `{finding.get('severity', 'medium')}`",
                "",
            ]
        )

    return "\n".join(lines)


def report_path_for(target_path: str, run_id: str) -> Path:
    return REPORT_DIR / f"{safe_path_name(target_path)}-{run_id}-compliance.json"


async def create_github_issue_for_report(report: dict[str, Any], run_id: str) -> int | None:
    findings = report.get("findings", [])
    if not isinstance(findings, list) or not findings:
        return None

    title = build_github_issue_title(report)
    body = build_github_issue_body(report, run_id)
    labels = github_labels_for_findings(findings)
    return await asyncio.to_thread(create_github_issue, title, body, labels)


def build_email_summary(reports: list[dict[str, Any]], attachments: list[Path], run_id: str) -> EmailMessage:
    sender = env_required("ALERT_FROM")
    recipients = [item.strip() for item in env_required("ALERT_TO").split(",") if item.strip()]
    if not recipients:
        raise RuntimeError("ALERT_TO must contain at least one recipient.")

    flagged = [report for report in reports if report_has_issues(report)]
    issue_count = sum(len(report.get("findings", [])) for report in flagged)
    subject = f"WaiverPro Compliance Alert: {issue_count} issue(s) detected"

    text_lines = [
        "WaiverPro Documentation Compliance Automation Agent",
        f"Run ID: {run_id}",
        f"Pages scanned: {len(reports)}",
        f"Pages flagged: {len(flagged)}",
        f"Total findings: {issue_count}",
        "",
    ]

    html_rows: list[str] = []
    for report in flagged:
        findings = report.get("findings", [])
        for finding in findings:
            selector = str(finding.get("element_selector", "body"))
            expected = str(finding.get("expected_behavior", ""))
            observed = str(finding.get("observed_behavior", ""))
            severity = str(finding.get("severity", "medium"))
            text_lines.extend(
                [
                    f"Path: {report.get('target_path')}",
                    f"Element: {selector}",
                    f"Expected: {expected}",
                    f"Observed: {observed}",
                    f"Severity: {severity}",
                    f"Screenshot: {report.get('screenshot_path')}",
                    "",
                ]
            )
            html_rows.append(
                "<tr>"
                f"<td>{escape(str(report.get('target_path')))}</td>"
                f"<td><code>{escape(selector)}</code></td>"
                f"<td>{escape(expected)}</td>"
                f"<td>{escape(observed)}</td>"
                f"<td><strong>{escape(severity)}</strong></td>"
                f"<td>{escape(str(report.get('screenshot_path') or 'n/a'))}</td>"
                "</tr>"
            )

    html = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #202124;">
        <h2>WaiverPro Documentation Compliance Alert</h2>
        <p><strong>Run ID:</strong> {escape(run_id)}<br>
        <strong>Pages scanned:</strong> {len(reports)}<br>
        <strong>Pages flagged:</strong> {len(flagged)}<br>
        <strong>Total findings:</strong> {issue_count}</p>
        <table cellpadding="8" cellspacing="0" border="1" style="border-collapse: collapse; width: 100%;">
          <thead>
            <tr>
              <th>Path</th>
              <th>Element</th>
              <th>Expected Behavior</th>
              <th>Observed Behavior</th>
              <th>Severity</th>
              <th>Screenshot</th>
            </tr>
          </thead>
          <tbody>
            {''.join(html_rows)}
          </tbody>
        </table>
        <p>JSON reports and screenshots are attached.</p>
      </body>
    </html>
    """

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content("\n".join(text_lines))
    message.add_alternative(html, subtype="html")

    seen: set[Path] = set()
    for path in attachments:
        if not path or path in seen or not path.exists() or not path.is_file():
            continue
        seen.add(path)
        data = path.read_bytes()
        suffix = path.suffix.lower()
        if suffix == ".json":
            maintype, subtype = "application", "json"
        elif suffix == ".png":
            maintype, subtype = "image", "png"
        elif suffix in {".jpg", ".jpeg"}:
            maintype, subtype = "image", "jpeg"
        else:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)

    return message


def send_smtp_email(message: EmailMessage, use_starttls: bool) -> None:
    host = env_required("SMTP_HOST")
    port = int(env_required("SMTP_PORT"))
    username = env_required("SMTP_USERNAME")
    password = env_required("SMTP_PASSWORD")
    context = ssl.create_default_context()

    logger.info("Sending SMTP alert through %s:%s", host, port)
    if use_starttls:
        with smtplib.SMTP(host, port, timeout=60) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as server:
            server.login(username, password)
            server.send_message(message)


async def run_pipeline(args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[Path], str]:
    run_id = time.strftime("%Y%m%d-%H%M%S")
    RETRIEVAL_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed, slow_mo=args.slow_mo)
        try:
            if args.force_login or not AUTH_STATE_PATH.exists():
                logger.info("Refreshing Playwright auth state")
                await login_and_save_state(browser, args.base_url, args.email, args.password)

            reports: list[dict[str, Any]] = []
            attachments: list[Path] = []
            for target_path in args.target_path:
                try:
                    report, page_attachments = await audit_target_path(browser, args.base_url, target_path, run_id, args)
                except Exception as exc:
                    logger.exception("Unexpected per-route failure for %s", target_path)
                    target_url = absolute_url(args.base_url, target_path)
                    report = critical_report(
                        target_path=target_path,
                        target_url=target_url,
                        message=f"Unexpected per-route failure: {exc}",
                    )
                    report_path = REPORT_DIR / f"{safe_path_name(target_path)}-{run_id}-compliance.json"
                    save_json(report_path, report)
                    page_attachments = [report_path]
                if not args.no_github_issues and report_has_issues(report):
                    try:
                        issue_number = await create_github_issue_for_report(report, run_id)
                        if issue_number is not None:
                            report["github_issue_number"] = issue_number
                            save_json(report_path_for(str(report.get("target_path", "unknown")), run_id), report)
                            logger.info("Logged GitHub issue #%s for %s", issue_number, report.get("target_path"))
                    except Exception:
                        logger.exception("GitHub issue logging failed for %s", report.get("target_path"))
                reports.append(report)
                attachments.extend(page_attachments)
        finally:
            await browser.close()

    return reports, attachments, run_id


def parse_args() -> argparse.Namespace:
    env_paths = parse_csv_paths(os.environ.get("COMPLIANCE_URL_PATHS"))
    default_paths = env_paths or DEFAULT_TARGET_PATHS

    parser = argparse.ArgumentParser(description="Run WaiverPro compliance automation end to end.")
    parser.add_argument("--base-url", default=os.environ.get("APP_BASE_URL"))
    parser.add_argument("--email", default=env_or_default("APP_LOGIN_EMAIL", DEFAULT_EMAIL))
    parser.add_argument("--password", default=os.environ.get("APP_LOGIN_PASSWORD"))
    parser.add_argument("--force-login", action="store_true")
    parser.add_argument("--target-path", action="append", default=None)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--slow-mo", type=int, default=0)

    parser.add_argument("--embedding-model", default="sentence-transformers/all-MiniLM-L6-v2")
    parser.add_argument("--rpc-name", default=os.environ.get("SUPABASE_RPC_NAME", "match_guidelines"))
    parser.add_argument("--match-count", type=int, default=5)
    parser.add_argument("--similarity-threshold", type=float, default=0.2)

    parser.add_argument("--compliance-model", default=os.environ.get("COMPLIANCE_MODEL", "Qwen/Qwen2.5-7B-Instruct"))
    parser.add_argument("--max-new-tokens", type=int, default=1_200)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--repair-attempts", type=int, default=1)

    parser.add_argument("--no-email", action="store_true")
    parser.add_argument("--no-github-issues", action="store_true")
    parser.add_argument("--smtp-starttls", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    args.target_path = args.target_path or default_paths
    return args


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
        args.target_path = [normalize_path(path) for path in args.target_path]
        reports, attachments, run_id = await run_pipeline(args)
        flagged = [report for report in reports if report_has_issues(report)]

        summary_path = REPORT_DIR / f"summary-{run_id}.json"
        save_json(summary_path, {"run_id": run_id, "reports": reports})
        attachments.append(summary_path)

        if not flagged:
            logger.info("No compliance issues found across %d page(s)", len(reports))
            return 0

        logger.warning("Compliance issues found on %d page(s)", len(flagged))
        if args.no_email:
            logger.info("--no-email enabled; SMTP alert skipped")
            return 2

        message = build_email_summary(reports, attachments, run_id)
        await asyncio.to_thread(send_smtp_email, message, args.smtp_starttls)
        return 2
    except KeyboardInterrupt:
        logger.error("Interrupted by user")
        return 130
    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(async_main()))
