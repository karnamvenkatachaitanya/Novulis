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
import re
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

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, KeepTogether, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

from .compliance_agent import run_compliance_check, choose_compliance_model
from .github_client import create_github_issue
from .retrieval_engine import retrieve_matching_rules
from .scraper import AUTH_STATE_PATH, PageCapture, absolute_url, login_and_save_state, scrape_page_state

BASELINE_FILE = Path("visual_baselines.json")
current_baselines = {}
new_baselines = {}

def load_baselines() -> dict[str, Any]:
    if BASELINE_FILE.exists():
        try:
            return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Failed to load visual baselines: %s", exc)
    return {}


def save_baselines(baselines: dict[str, Any]) -> None:
    try:
        BASELINE_FILE.write_text(json.dumps(baselines, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Saved visual baselines to %s", BASELINE_FILE.resolve())
    except Exception as exc:
        logger.error("Failed to save visual baselines: %s", exc)


def detect_visual_regressions(target_path: str, current_dom: dict[str, list[dict[str, Any]]], baseline_page_dom: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    findings = []
    if not baseline_page_dom:
        return findings

    # Create maps of baseline elements for easy lookup
    baseline_map = {}
    for group, elements in baseline_page_dom.items():
        if not isinstance(elements, list):
            continue
        for elem in elements:
            sel = elem.get("selector", "")
            txt = elem.get("text", "")
            key = (group, sel, txt)
            baseline_map[key] = elem
            key_sel = (group, sel)
            if key_sel not in baseline_map:
                baseline_map[key_sel] = elem

    # Track counts of elements by group
    for group in ["buttons", "links", "fields", "headings"]:
        curr_count = len(current_dom.get(group, []))
        base_count = len(baseline_page_dom.get(group, []))
        if curr_count != base_count:
            findings.append({
                "element_selector": f"group:{group}",
                "expected_behavior": f"Page should contain exactly {base_count} {group} matching baseline design layout.",
                "observed_behavior": f"Visual count regression: count of {group} changed from {base_count} to {curr_count}.",
                "severity": "low",
                "type": "visual_count_diff"
            })

    for group, elements in current_dom.items():
        if not isinstance(elements, list):
            continue
        for elem in elements:
            sel = elem.get("selector", "")
            txt = elem.get("text", "")
            
            # Find matching baseline element
            base_elem = baseline_map.get((group, sel, txt))
            if not base_elem:
                base_elem = baseline_map.get((group, sel))
            
            if not base_elem:
                # Element not found in baseline
                continue
            
            # Compare styles!
            curr_styles = elem.get("styles", {})
            base_styles = base_elem.get("styles", {})
            
            if not curr_styles or not base_styles:
                continue
                
            # Capture background color change
            curr_bg = curr_styles.get("backgroundColor")
            base_bg = base_styles.get("backgroundColor")
            if curr_bg and base_bg and curr_bg != base_bg:
                findings.append({
                    "element_selector": sel,
                    "expected_behavior": f"Element background color should match baseline: {base_bg}",
                    "observed_behavior": f"Visual regression: background color changed from {base_bg} to {curr_bg}",
                    "severity": "medium",
                    "type": "visual_style_diff"
                })
                
            # Capture text color change
            curr_fg = curr_styles.get("color")
            base_fg = base_styles.get("color")
            if curr_fg and base_fg and curr_fg != base_fg:
                findings.append({
                    "element_selector": sel,
                    "expected_behavior": f"Element text color should match baseline: {base_fg}",
                    "observed_behavior": f"Visual regression: text color changed from {base_fg} to {curr_fg}",
                    "severity": "medium",
                    "type": "visual_style_diff"
                })
                
            # Capture border visibility/style change
            curr_b_vis = curr_styles.get("borderVisible")
            base_b_vis = base_styles.get("borderVisible")
            curr_b_style = curr_styles.get("borderStyle")
            base_b_style = base_styles.get("borderStyle")
            curr_b_width = curr_styles.get("borderWidth")
            base_b_width = base_styles.get("borderWidth")
            
            if curr_b_vis != base_b_vis or curr_b_style != base_b_style or curr_b_width != base_b_width:
                findings.append({
                    "element_selector": sel,
                    "expected_behavior": f"Element border should match baseline (visible={base_b_vis}, style={base_b_style}, width={base_b_width})",
                    "observed_behavior": f"Visual regression: border changed (visible={curr_b_vis}, style={curr_b_style}, width={curr_b_width})",
                    "severity": "medium",
                    "type": "visual_style_diff"
                })

            # Capture text/content changes (normalize whitespace to avoid false positives)
            import re as _re
            txt_norm = _re.sub(r'\s+', ' ', txt).strip()
            base_txt_norm = _re.sub(r'\s+', ' ', base_elem.get("text", "")).strip()
            if txt_norm != base_txt_norm:
                findings.append({
                    "element_selector": sel,
                    "expected_behavior": f"Element text content should match baseline: '{base_elem.get('text', '')}'",
                    "observed_behavior": f"Content regression: text changed from '{base_elem.get('text', '')}' to '{txt}'",
                    "severity": "low",
                    "type": "visual_text_diff"
                })

    return findings


def build_pdf_report(reports: list[dict[str, Any]], run_id: str, output_path: Path) -> None:
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )
    
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=22,
        leading=26,
        textColor=colors.HexColor("#1A365D"),
        spaceAfter=15
    )
    
    h1_style = ParagraphStyle(
        "SectionHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=18,
        textColor=colors.HexColor("#2B6CB0"),
        spaceBefore=15,
        spaceAfter=8,
        keepWithNext=True
    )
    
    meta_label_style = ParagraphStyle(
        "MetaLabel",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=12,
        textColor=colors.HexColor("#4A5568")
    )
    
    meta_value_style = ParagraphStyle(
        "MetaValue",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=12,
        textColor=colors.HexColor("#2D3748")
    )
    
    cell_header_style = ParagraphStyle(
        "CellHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=11,
        textColor=colors.white
    )
    
    cell_body_style = ParagraphStyle(
        "CellBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=11,
        textColor=colors.HexColor("#2D3748")
    )
    
    cell_code_style = ParagraphStyle(
        "CellCode",
        parent=styles["Normal"],
        fontName="Courier",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#1A365D")
    )

    severity_styles = {
        "critical": ParagraphStyle("SevCritical", parent=cell_body_style, fontName="Helvetica-Bold", textColor=colors.HexColor("#9B2C2C")),
        "high": ParagraphStyle("SevHigh", parent=cell_body_style, fontName="Helvetica-Bold", textColor=colors.HexColor("#C05621")),
        "medium": ParagraphStyle("SevMedium", parent=cell_body_style, fontName="Helvetica-Bold", textColor=colors.HexColor("#B7791F")),
        "low": ParagraphStyle("SevLow", parent=cell_body_style, fontName="Helvetica-Bold", textColor=colors.HexColor("#4A5568"))
    }
    
    story = []
    
    # 1. Document Header / Title
    story.append(Paragraph("WaiverPro Compliance Audit Report", title_style))
    story.append(Spacer(1, 5))
    
    # 2. Metadata Table
    scanned_count = len(reports)
    flagged_count = sum(1 for r in reports if len(r.get("findings", [])) > 0)
    total_findings = sum(len(r.get("findings", [])) for r in reports)
    
    meta_data = [
        [Paragraph("Run ID:", meta_label_style), Paragraph(run_id, meta_value_style),
         Paragraph("Date / Time:", meta_label_style), Paragraph(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), meta_value_style)],
        [Paragraph("Pages Scanned:", meta_label_style), Paragraph(str(scanned_count), meta_value_style),
         Paragraph("Pages Flagged:", meta_label_style), Paragraph(str(flagged_count), meta_value_style)],
        [Paragraph("Total Findings:", meta_label_style), Paragraph(str(total_findings), meta_value_style),
         Paragraph("Overall Status:", meta_label_style), 
         Paragraph(
             "<font color='#9B2C2C'><b>NON-COMPLIANT</b></font>" if total_findings > 0 else "<font color='#2F855A'><b>COMPLIANT</b></font>", 
             meta_value_style
         )]
    ]
    
    meta_table = Table(meta_data, colWidths=[100, 170, 100, 170])
    meta_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor("#EDF2F7")),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('PADDING', (0,0), (-1,-1), 8),
        ('BOX', (0,0), (-1,-1), 1, colors.HexColor("#CBD5E0")),
        ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor("#E2E8F0")),
    ]))
    
    story.append(meta_table)
    story.append(Spacer(1, 20))
    
    # 3. Individual Pages Loop
    for page_idx, report in enumerate(reports):
        target_path = report.get("target_path", "unknown")
        page_url = report.get("page_url") or report.get("target_url", "")
        findings = report.get("findings", [])
        screenshot_path = report.get("screenshot_path")
        
        page_elements = []
        page_elements.append(Paragraph(f"Page Path: {target_path}", h1_style))
        
        # URL info
        page_elements.append(Paragraph(f"<b>URL:</b> {page_url}", meta_value_style))
        page_elements.append(Spacer(1, 8))
        
        if not findings:
            page_elements.append(Paragraph("<font color='#2F855A'><b>✓ No compliance or style issues found on this page.</b></font>", meta_value_style))
            page_elements.append(Spacer(1, 15))
            story.append(KeepTogether(page_elements))
            continue
            
        # Findings Table
        table_data = [[
            Paragraph("Selector", cell_header_style), 
            Paragraph("Expected Behavior", cell_header_style), 
            Paragraph("Observed Behavior", cell_header_style), 
            Paragraph("Severity", cell_header_style)
        ]]
        
        for finding in findings:
            selector = finding.get("element_selector", "body")
            expected = finding.get("expected_behavior", "")
            observed = finding.get("observed_behavior", "")
            severity = finding.get("severity", "medium").lower()
            
            sev_style = severity_styles.get(severity, cell_body_style)
            
            table_data.append([
                Paragraph(selector, cell_code_style),
                Paragraph(expected, cell_body_style),
                Paragraph(observed, cell_body_style),
                Paragraph(severity.upper(), sev_style)
            ])
        
        findings_table = Table(table_data, colWidths=[110, 160, 200, 70])
        findings_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#2B6CB0")),
            ('ALIGN', (0,0), (-1,-1), 'LEFT'),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('PADDING', (0,0), (-1,-1), 6),
            ('BOX', (0,0), (-1,-1), 1, colors.HexColor("#CBD5E0")),
            ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor("#E2E8F0")),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor("#F7FAFC")]),
        ]))
        
        page_elements.append(findings_table)
        page_elements.append(Spacer(1, 15))
        
        # Screenshot
        if screenshot_path and os.path.exists(screenshot_path):
            try:
                # Page width for letter size is 612 pt. Margins are 36 pt on each side, leaving 540 pt.
                img_w = 480
                img_h = 320 # Use 3:2 ratio approx
                screenshot_flowable = Image(screenshot_path, width=img_w, height=img_h)
                
                page_elements.append(Paragraph("<b>Live Page Screenshot Evidence:</b>", meta_label_style))
                page_elements.append(Spacer(1, 5))
                page_elements.append(screenshot_flowable)
                page_elements.append(Spacer(1, 20))
            except Exception as exc:
                logger.warning("Could not embed screenshot %s: %s", screenshot_path, exc)
        else:
            page_elements.append(Paragraph("<i>No screenshot evidence available.</i>", meta_value_style))
            page_elements.append(Spacer(1, 20))
            
        story.append(KeepTogether(page_elements))
        if page_idx < len(reports) - 1:
            story.append(PageBreak())
            
    # Compliance disclaimer
    disclaimer_style = ParagraphStyle(
        "Disclaimer",
        parent=styles["Normal"],
        fontName="Helvetica-Oblique",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#718096"),
        spaceBefore=15,
        spaceAfter=10
    )
    story.append(Spacer(1, 15))
    story.append(Paragraph(
        "<b>Disclaimer:</b> This report was generated by an automated compliance agent using "
        "LLM-based analysis and RAG-retrieved guideline rules. It is not a substitute for manual "
        "QA review. All findings should be verified by a human reviewer before taking corrective action.",
        disclaimer_style
    ))
    
    # Build PDF
    doc.build(story)
    logger.info("Compiled single unified PDF report: %s", output_path.resolve())

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


DEFAULT_TARGET_PATHS = [
    "/",
    "/login",
    "/dashboard/my-applications",
    "/dashboard/facilities",
    "/dashboard/action-items",
    "/dashboard/user-management",
    "/dashboard/announcements",
    "/dashboard/settings",
    "/dashboard/faqs",
    "/dashboard/tickets",
    "/dashboard/contact",
    "/privacy",
    "/terms",
]
RETRIEVAL_DIR = Path("retrieved_context")
REPORT_DIR = Path("reports")
CAPTURE_DIR = Path("captured_states")
DEFAULT_EMAIL = "admin@gmail.com"

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
    is_infra_error = any(keyword in message.lower() for keyword in [
        "402 payment required", "429 too many requests", "500 internal server",
        "inference failed", "scrape failed", "timeout", "network",
    ])
    finding_type = "infrastructure_error" if is_infra_error else "compliance_failure"
    is_secure = "/dashboard" in target_path
    expected_behavior = (
        f"{target_path} should load as an authenticated WaiverPro application page."
        if is_secure
        else f"{target_path} should load successfully as a public page."
    )
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
                "expected_behavior": expected_behavior,
                "observed_behavior": f"CRITICAL {finding_type.upper().replace('_', ' ')}: {message}",
                "severity": "critical",
                "type": finding_type,
                "guideline_reference": "",
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

    global current_baselines, new_baselines
    visual_findings = []
    baseline_dom = current_baselines.get(target_path)
    if baseline_dom:
        logger.info("Running visual style regression check against baseline for %s", target_path)
        visual_findings = detect_visual_regressions(target_path, capture.dom, baseline_dom)
        logger.info("Found %d visual style discrepancy findings for %s", len(visual_findings), target_path)

    if getattr(args, "save_baseline", False) or not baseline_dom:
        logger.info("Caching baseline DOM structure for %s", target_path)
        new_baselines[target_path] = capture.dom

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

    selected_model = args.compliance_model
    # Dynamic routing matches model size to rule size and complexity
    if args.compliance_model == os.environ.get("COMPLIANCE_MODEL", "Qwen/Qwen2.5-7B-Instruct"):
        selected_model = choose_compliance_model(capture_payload, guidelines_to_text(guidelines))

    try:
        if selected_model == "BYPASS_LLM":
            logger.info("Bypassing LLM compliance check for %s: No RAG guideline rules retrieved.", target_path)
            findings = []
        else:
            findings = await asyncio.to_thread(
                run_compliance_check,
                target_path,
                capture_payload,
                guidelines_to_text(guidelines),
                None,
                model_name=selected_model,
                max_new_tokens=args.max_new_tokens,
                temperature=args.temperature,
                repair_attempts=args.repair_attempts,
            )
        # Merge compliance findings and visual style regressions
        findings.extend(visual_findings)
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
    findings_list = report.get("findings")
    findings_len = len(findings_list) if isinstance(findings_list, list) else 0
    logger.info("Completed audit for %s with %d finding(s)", target_path, findings_len)
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


# NOTE: The WaiverPro app is hosted externally on Azure Static Apps.
# These source file mappings are advisory labels for GitHub issues only.
# The auto_healer.py cannot modify the live app source code.
ROUTE_TO_FILE_MAPPING = {
    "/dashboard/my-applications": "WaiverPro:MyApplications",
    "/dashboard/facilities": "WaiverPro:Facilities",
    "/dashboard/action-items": "WaiverPro:ActionItems",
    "/dashboard/faqs": "WaiverPro:FAQs",
    "/dashboard/user-management": "WaiverPro:UserManagement",
    "/dashboard/announcements": "WaiverPro:Announcements",
    "/dashboard/settings": "WaiverPro:Settings",
    "/dashboard/tickets": "WaiverPro:Tickets",
    "/dashboard/contact": "WaiverPro:Contact",
}


def build_github_issue_body(report: dict[str, Any], run_id: str) -> str:
    findings = report.get("findings", [])
    screenshot = report.get("screenshot_path") or "No screenshot captured"
    target_path = str(report.get("target_path") or "")

    source_file = "WaiverPro:UnknownComponent"
    for route, filepath in ROUTE_TO_FILE_MAPPING.items():
        if target_path.rstrip("/") == route.rstrip("/"):
            source_file = filepath
            break

    expected_texts = []
    observed_texts = []
    for f in findings:
        expected_texts.append(f.get("expected_behavior", "Not specified"))
        observed_texts.append(f.get("observed_behavior", "Not specified"))

    expected_combined = " ; ".join(expected_texts) if expected_texts else "None"
    observed_combined = " ; ".join(observed_texts) if observed_texts else "None"

    lines = [
        "## Compliance Discrepancy Report",
        "",
        f"**Run ID:** `{run_id}`",
        f"**Target Path:** `{target_path}`",
        f"Source File: {source_file}",
        f"Expected: {expected_combined}",
        f"Observed: {observed_combined}",
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
        <p>The unified visual compliance report PDF is attached.</p>
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
        # Allow PDF, JSON, and PNG attachments to include evidence and metadata
        suffix = path.suffix.lower()
        if suffix not in {".pdf", ".json", ".png"}:
            continue
        seen.add(path)
        data = path.read_bytes()
        if suffix == ".pdf":
            maintype, subtype = "application", "pdf"
        elif suffix == ".json":
            maintype, subtype = "application", "json"
        elif suffix == ".png":
            maintype, subtype = "image", "png"
        else:
            continue
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)

    return message


def is_alert_cooldown_active(flagged: list[dict[str, Any]]) -> bool:
    """Return True if an alert with identical findings was sent within the 1-hour cooldown window."""
    cooldown_file = REPORT_DIR / ".last_sent_alert.json"
    if not cooldown_file.exists():
        return False
    try:
        data = json.loads(cooldown_file.read_text(encoding="utf-8"))
        last_timestamp = float(data.get("timestamp", 0.0))
        last_findings = data.get("findings", [])
        
        # Check if 1 hour (3600 seconds) has elapsed
        if time.time() - last_timestamp > 3600:
            return False
            
        # Compare findings
        current_findings_summary = []
        for report in flagged:
            for f in report.get("findings", []):
                current_findings_summary.append({
                    "path": report.get("target_path"),
                    "selector": f.get("element_selector"),
                    "expected": f.get("expected_behavior"),
                    "observed": f.get("observed_behavior")
                })
                
        # If lengths match, check if all elements match
        if len(last_findings) != len(current_findings_summary):
            return False
            
        for lf, cf in zip(last_findings, current_findings_summary):
            if lf != cf:
                return False
                
        return True
    except Exception as exc:
        logger.warning("Failed to parse alert cooldown file: %s", exc)
        return False


def record_sent_alert(flagged: list[dict[str, Any]]) -> None:
    """Record current alert details to enforce future cooldown checks."""
    cooldown_file = REPORT_DIR / ".last_sent_alert.json"
    try:
        findings_summary = []
        for report in flagged:
            for f in report.get("findings", []):
                findings_summary.append({
                    "path": report.get("target_path"),
                    "selector": f.get("element_selector"),
                    "expected": f.get("expected_behavior"),
                    "observed": f.get("observed_behavior")
                })
        data = {
            "timestamp": time.time(),
            "findings": findings_summary
        }
        save_json(cooldown_file, data)
    except Exception as exc:
        logger.warning("Failed to record sent alert: %s", exc)


def send_smtp_email(message: EmailMessage, use_starttls: bool) -> None:
    host = env_required("SMTP_HOST")
    port = int(env_required("SMTP_PORT"))
    username = env_required("SMTP_USERNAME")
    password = env_required("SMTP_PASSWORD")
    context = ssl.create_default_context()

    logger.info("Sending SMTP alert through %s:%s", host, port)
    if use_starttls or port == 587:
        with smtplib.SMTP(host, port, timeout=300) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=300) as server:
            server.login(username, password)
            server.send_message(message)


async def run_pipeline(args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[Path], str]:
    run_id = time.strftime("%Y%m%d-%H%M%S")
    RETRIEVAL_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    global current_baselines, new_baselines
    current_baselines = load_baselines()
    new_baselines = {}

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

    if new_baselines:
        current_baselines.update(new_baselines)
        save_baselines(current_baselines)

    return reports, attachments, run_id


def prune_old_runs(keep_count: int = 3) -> None:
    """Keep only the last keep_count compliance runs' files, deleting older ones from:
       - reports/
       - retrieved_context/
       - captured_states/
    """
    logger.info("Pruning old runs, keeping only the last %d runs...", keep_count)
    if not REPORT_DIR.exists():
        return

    # Find all summary-*.json files to identify run IDs
    summary_files = sorted(
        [f for f in REPORT_DIR.iterdir() if f.name.startswith("summary-") and f.name.endswith(".json")],
        key=lambda f: f.name
    )

    if len(summary_files) <= keep_count:
        logger.info("Total runs (%d) is <= keep_count (%d). No pruning needed.", len(summary_files), keep_count)
        return

    # Split into runs to keep and runs to prune
    files_to_keep = summary_files[-keep_count:]

    # Extract run IDs to keep
    keep_run_ids = set()
    for f in files_to_keep:
        match = re.search(r"summary-(\d{8}-\d{6})\.json", f.name)
        if match:
            keep_run_ids.add(match.group(1))

    # The oldest run ID to keep determines the threshold for captured_states files
    sorted_keep_ids = sorted(list(keep_run_ids))
    oldest_keep_id = sorted_keep_ids[0] if sorted_keep_ids else ""

    logger.info("Keeping run IDs: %s. Oldest kept run ID threshold: %s", keep_run_ids, oldest_keep_id)

    # 1. Clean up files in reports/
    for path in REPORT_DIR.iterdir():
        if not path.is_file():
            continue
        match = re.search(r"(\d{8}-\d{6})", path.name)
        if match:
            timestamp = match.group(1)
            if timestamp not in keep_run_ids:
                try:
                    path.unlink()
                    logger.info("Deleted old report file: %s", path.name)
                except Exception as exc:
                    logger.warning("Failed to delete %s: %s", path.name, exc)

    # 2. Clean up files in retrieved_context/
    if RETRIEVAL_DIR.exists():
        for path in RETRIEVAL_DIR.iterdir():
            if not path.is_file():
                continue
            match = re.search(r"(\d{8}-\d{6})", path.name)
            if match:
                timestamp = match.group(1)
                if timestamp not in keep_run_ids:
                    try:
                        path.unlink()
                        logger.info("Deleted old retrieval file: %s", path.name)
                    except Exception as exc:
                        logger.warning("Failed to delete %s: %s", path.name, exc)

    # 3. Clean up files in captured_states/
    if CAPTURE_DIR.exists() and oldest_keep_id:
        for path in CAPTURE_DIR.iterdir():
            if not path.is_file():
                continue
            match = re.search(r"(\d{8}-\d{6})", path.name)
            if match:
                timestamp = match.group(1)
                if timestamp < oldest_keep_id:
                    try:
                        path.unlink()
                        logger.info("Deleted old captured state file: %s", path.name)
                    except Exception as exc:
                        logger.warning("Failed to delete %s: %s", path.name, exc)


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
    parser.add_argument("--save-baseline", action="store_true", help="Overwrite the cached baseline styles in visual_baselines.json with the current run's styles")
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

        # Generate the coverage/completeness report (GAP-02)
        ALL_KNOWN_ROUTES = [
            "/", "/login", "/dashboard/my-applications", "/dashboard/facilities",
            "/dashboard/action-items", "/dashboard/user-management", "/dashboard/announcements",
            "/dashboard/settings", "/dashboard/faqs", "/dashboard/tickets",
            "/dashboard/contact", "/privacy", "/terms"
        ]
        audited_paths = {r["target_path"] for r in reports}
        coverage_pct = (len(audited_paths) / len(ALL_KNOWN_ROUTES)) * 100
        
        # Determine guideline citations found
        citations = set()
        infra_errors_count = 0
        compliance_findings_count = 0
        for r in reports:
            for f in r.get("findings", []):
                ref = f.get("guideline_reference")
                if ref:
                    citations.add(ref)
                if f.get("type") == "infrastructure_error":
                    infra_errors_count += 1
                else:
                    compliance_findings_count += 1
                    
        coverage_data = {
            "run_id": run_id,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "path_coverage": {
                "total_routes_count": len(ALL_KNOWN_ROUTES),
                "audited_routes_count": len(audited_paths),
                "coverage_percentage": round(coverage_pct, 2),
                "audited_paths": list(sorted(audited_paths)),
                "skipped_paths": list(sorted(set(ALL_KNOWN_ROUTES) - audited_paths)),
            },
            "findings_summary": {
                "total_compliance_findings": compliance_findings_count,
                "total_infrastructure_errors": infra_errors_count,
                "guideline_sections_cited": list(sorted(citations)),
            }
        }
        coverage_path = REPORT_DIR / f"coverage-report-{run_id}.json"
        save_json(coverage_path, coverage_data)
        attachments.append(coverage_path)

        # Generate the unified PDF compliance report
        pdf_path = REPORT_DIR / f"compliance-report-{run_id}.pdf"
        try:
            build_pdf_report(reports, run_id, pdf_path)
            attachments.append(pdf_path)
        except Exception as exc:
            logger.exception("Failed to build unified PDF report: %s", exc)

        # Automatically ingest scraped snapshots into Supabase for RAG chatbot
        try:
            from .ingest_snapshots import ingest_snapshots
            audited_paths = [r["target_path"] for r in reports]
            logger.info("Automatically ingesting snapshots for audited paths: %s", audited_paths)
            ingest_snapshots(pages=audited_paths)
        except Exception as exc:
            logger.warning("Failed to automatically ingest snapshots: %s", exc)

        # Call cleanup function to keep only last 3 runs
        try:
            prune_old_runs(keep_count=3)
        except Exception as exc:
            logger.warning("Failed to prune old runs: %s", exc)

        if not flagged:
            logger.info("No compliance issues found across %d page(s)", len(reports))
            return 0

        logger.warning("Compliance issues found on %d page(s)", len(flagged))
        if args.no_email:
            logger.info("--no-email enabled; SMTP alert skipped")
            return 2

        message = build_email_summary(reports, attachments, run_id)
        if is_alert_cooldown_active(flagged):
            logger.info("SMTP alert skipped: identical findings detected within the 1-hour cooldown window.")
        else:
            await asyncio.to_thread(send_smtp_email, message, args.smtp_starttls)
            record_sent_alert(flagged)
        return 2
    except KeyboardInterrupt:
        logger.error("Interrupted by user")
        return 130
    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(async_main()))
