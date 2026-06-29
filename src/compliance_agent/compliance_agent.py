"""
Strict LLM judge for WaiverPro documentation compliance.

Uses Hugging Face Serverless Inference API with Qwen/Qwen2.5-7B-Instruct.
The agent explicitly bypasses LLM execution when scraping failed due to auth redirect.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from huggingface_hub import InferenceClient

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"
MAX_DOM_CHARS = 35_000
MAX_GUIDELINE_CHARS = 18_000
MAX_NEW_TOKENS = 1_200

logger = logging.getLogger("compliance_agent")


# Known page data-testid markers used to detect cross-page DOM bleeding
PAGE_TESTID_MARKERS = {
    "/dashboard/my-applications": "page-my-applications",
    "/dashboard/facilities": "page-facilities",
    "/dashboard/action-items": "page-action-items",
    "/dashboard/user-management": "page-user-management",
    "/dashboard/announcements": "page-announcements",
    "/dashboard/settings": "page-settings",
    "/dashboard/faqs": "page-faqs",
    "/dashboard/tickets": "page-tickets",
    "/dashboard/contact": "page-contact",
}


def filter_dom_for_target_page(live_dom: Any, target_path: str) -> Any:
    """Remove DOM elements that belong to OTHER pages (cross-page contamination guard).
    
    If the live app renders shared layout with multiple page-specific containers,
    we strip elements whose selectors contain data-testid markers for pages
    other than the current audit target.
    """
    foreign_markers = []
    for path, marker in PAGE_TESTID_MARKERS.items():
        if path != target_path:
            foreign_markers.append(marker)
    
    if not foreign_markers:
        return live_dom
    
    def is_foreign_element(elem: dict) -> bool:
        selector = str(elem.get("selector", ""))
        for marker in foreign_markers:
            if marker in selector:
                return True
        return False
    
    if isinstance(live_dom, dict) and "dom" in live_dom:
        filtered_dom = {}
        dom_data = live_dom["dom"]
        if isinstance(dom_data, dict):
            for group, elements in dom_data.items():
                if isinstance(elements, list):
                    filtered_dom[group] = [e for e in elements if not is_foreign_element(e)]
                else:
                    filtered_dom[group] = elements
        result = dict(live_dom)
        result["dom"] = filtered_dom
        return result
    
    if isinstance(live_dom, dict):
        filtered = {}
        for group, elements in live_dom.items():
            if isinstance(elements, list):
                filtered[group] = [e for e in elements if not is_foreign_element(e)]
            else:
                filtered[group] = elements
        return filtered
    
    return live_dom


SYSTEM_PROMPT = """You are a strict WaiverPro documentation compliance judge.

Constraint block:
- Compare the Live DOM layout structure against the official PDF guideline rules for this specific path.
- Output findings strictly as a valid, parsable JSON array of objects with keys: `element_selector`, `expected_behavior`, `observed_behavior`, `severity`, and `guideline_reference`.
- Do not wrap the output in markdown text wrappers or include pleasantries.
- CRITICAL: Only report elements that deviate or fail to match the guideline rules. If a component complies, or if its actual behavior matches the expected behavior, do NOT include it in the findings.
- CRITICAL: The `element_selector` must be the exact unique CSS selector of the text/element containing the mismatched value (e.g. `div.text-sm.text-muted-foreground:nth-of-type(2)`). Do NOT return general classes (like `.font-medium`) or selectors of unrelated/nearby elements (like form input fields such as `#email` or `#subject`) if the discrepancy is in the static text element itself.
- CRITICAL: Only report discrepancies for elements that belong to the specific target page path being audited. Do NOT report elements from other pages (e.g., do not report Contact page issues when auditing the Settings page).
- Do not invent requirements. Only report discrepancies that are supported by the retrieved guideline rules.
- IMPORTANT: The retrieved guideline rules are a subset of the manual. If an element (like sidebar navigation links, brand logo, notifications bell, profile avatar, buttons, or shared headers) is present in the DOM but not mentioned in the retrieved guidelines, it is NOT a discrepancy. Do NOT report it as "should not be present".
- Only report a discrepancy if a retrieved guideline rule explicitly contradicts the observed state in the DOM (e.g. if the guidelines specify a different email address, phone number, address, or business hours than what is observed in the DOM).
- CRITICAL: "expected_behavior" must be a direct instruction or value explicitly found in the "Retrieved Official Guidelines" text. Do NOT use text from the "Live DOM Layout Matrix" as the "expected_behavior".
- CRITICAL: If the guidelines do not specify what a particular text/label/header should say, do NOT report it as a discrepancy. If they only specify details for specific elements (like input fields or buttons), only verify those elements.
- CRITICAL: Do NOT report page flow instructions, transitions, or future states (e.g. "after successful sign-in you are taken to My Applications dashboard", or "Click Login to continue") as current page text/layout discrepancies.
- CRITICAL: Verify that the text observed in the DOM for `element_selector` matches the text you output in `observed_behavior`. Do not swap selectors or assign incorrect texts to selectors.
- The `guideline_reference` field must cite the specific section of the PDF guidelines that supports the finding (e.g. "Section 11: Support — Contact").
- If all elements on the live page comply, output exactly [].
- Valid severity values are "critical", "high", "medium", and "low".
"""


USER_PROMPT_TEMPLATE = """## Target Path
{target_path}

## Official PDF Guideline Rules Retrieved For This Path
{retrieved_guidelines_text}

## Live DOM Layout Matrix
{live_dom_json}

## Task
Return only a JSON array. Each object must contain exactly:
- element_selector
- expected_behavior
- observed_behavior
- severity
- guideline_reference (e.g. "Section 7: Facilities")
"""


REPAIR_PROMPT_TEMPLATE = """Your previous response was not valid JSON with the required schema.
Return only a valid JSON array of objects with keys:
element_selector, expected_behavior, observed_behavior, severity.

Previous response:
{bad_output}
"""


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def compact(value: Any, max_chars: int) -> str:
    # Safe object truncation to prevent broken JSON strings
    if isinstance(value, (dict, list)):
        try:
            serialized = json.dumps(value, indent=2, ensure_ascii=False)
            if len(serialized) <= max_chars:
                return serialized
            
            # Truncate elements for lists
            if isinstance(value, list):
                truncated_list = []
                current_size = 2
                for item in value:
                    item_str = json.dumps(item, ensure_ascii=False)
                    if current_size + len(item_str) + 2 > max_chars:
                        break
                    truncated_list.append(item)
                    current_size += len(item_str) + 2
                return json.dumps(truncated_list + ["... (truncated)"], indent=2, ensure_ascii=False)
                
            # Truncate elements for dicts
            if isinstance(value, dict):
                truncated_dict = {}
                current_size = 2
                for k, v in value.items():
                    item_str = json.dumps({k: v}, ensure_ascii=False)
                    if current_size + len(item_str) + 2 > max_chars:
                        break
                    truncated_dict[k] = v
                    current_size += len(item_str) + 2
                truncated_dict["_truncated"] = "..."
                return json.dumps(truncated_dict, indent=2, ensure_ascii=False)
        except Exception:
            pass

    if not isinstance(value, str):
        value = json.dumps(value, indent=2, ensure_ascii=False)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."


def static_scrape_failure_report(target_path: str, scrape_error_flag: str) -> list[dict[str, str]]:
    reason = "Redirected to login page. Session dropped."
    if scrape_error_flag and scrape_error_flag != "AUTH_REDIRECT_TRIGGERED":
        reason = f"Scrape failed before DOM extraction: {scrape_error_flag}"

    return [
        {
            "element_selector": "body",
            "expected_behavior": f"{target_path} must render as an authenticated application page using persisted auth_state.json.",
            "observed_behavior": f"CRITICAL COMPLIANCE FAILURE: {reason}",
            "severity": "critical",
        }
    ]


def clean_dom_for_llm(live_dom: Any) -> Any:
    if not isinstance(live_dom, dict):
        return live_dom
    
    # If this is the outer PageCapture dict
    if "dom" in live_dom:
        dom_data = live_dom["dom"]
        cleaned = {
            "title": live_dom.get("title"),
            "current_url": live_dom.get("current_url"),
            "dom": clean_dom_for_llm(dom_data)
        }
        return cleaned
        
    # If this is the raw group-based DOM
    cleaned_groups = {}
    for group, elements in live_dom.items():
        if isinstance(elements, list):
            cleaned_elements = []
            for elem in elements:
                if isinstance(elem, dict):
                    cleaned_elem = {
                        "role": elem.get("role"),
                        "tag": elem.get("tag"),
                        "text": elem.get("text"),
                        "selector": elem.get("selector"),
                    }
                    attrs = elem.get("attributes")
                    if attrs:
                        cleaned_elem["attributes"] = attrs
                    cleaned_elements.append(cleaned_elem)
                else:
                    cleaned_elements.append(elem)
            cleaned_groups[group] = cleaned_elements
        else:
            cleaned_groups[group] = elements
    return cleaned_groups


def build_messages(target_path: str, live_dom_json: Any, retrieved_guidelines_text: str) -> list[dict[str, str]]:
    # Filter out DOM elements that belong to other pages to prevent cross-page contamination
    filtered_dom = filter_dom_for_target_page(live_dom_json, target_path)
    cleaned_dom = clean_dom_for_llm(filtered_dom)
    prompt = USER_PROMPT_TEMPLATE.format(
        target_path=target_path,
        retrieved_guidelines_text=compact(retrieved_guidelines_text, MAX_GUIDELINE_CHARS),
        live_dom_json=compact(cleaned_dom, MAX_DOM_CHARS),
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]


def call_hf_inference(
    messages: list[dict[str, str]],
    *,
    model_name: str,
    max_new_tokens: int,
    temperature: float,
) -> str:
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError("Missing HF_TOKEN for Hugging Face Serverless Inference API.")

    client = InferenceClient(token=token)
    for attempt in range(1, 4):
        try:
            response = client.chat_completion(
                model=model_name,
                messages=messages,
                max_tokens=max_new_tokens,
                temperature=max(temperature, 0.01),
            )
            content = response.choices[0].message.content
            return content.strip() if content is not None else ""
        except Exception as exc:
            if attempt == 3:
                raise RuntimeError(f"Hugging Face inference failed after 3 attempts: {exc}") from exc
            wait_seconds = 2**attempt
            logger.warning("HF inference attempt %s failed; retrying in %ss: %s", attempt, wait_seconds, exc)
            time.sleep(wait_seconds)

    raise RuntimeError("Unexpected Hugging Face inference failure.")


def parse_json_array(raw_output: str) -> list[dict[str, Any]]:
    cleaned = raw_output.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise
        parsed = json.loads(cleaned[start : end + 1])

    if not isinstance(parsed, list):
        raise RuntimeError("LLM output must be a JSON array.")
    return parsed


def normalize_selector(sel: str) -> str:
    sel = sel.replace('\\"', '"').replace("'", '"').strip().lower()
    sel = re.sub(r"\s+", " ", sel)
    return sel


def find_dom_element_text(live_dom: Any, selector: str) -> str | None:
    if not live_dom:
        return None
    
    dom_data = live_dom
    if isinstance(live_dom, dict) and "dom" in live_dom:
        dom_data = live_dom["dom"]
        
    if not isinstance(dom_data, dict):
        return None
        
    norm_target = normalize_selector(selector)
    
    for group, elements in dom_data.items():
        if not isinstance(elements, list):
            continue
        for elem in elements:
            if not isinstance(elem, dict):
                continue
            elem_selector = normalize_selector(str(elem.get("selector", "")))
            if elem_selector == norm_target:
                return str(elem.get("text", ""))
                
    return None


def validate_findings(
    findings: list[dict[str, Any]], 
    target_path: str | None = None,
    live_dom: Any = None
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    seen_selectors: set[str] = set()
    
    # Build list of foreign page markers for cross-page deduplication
    foreign_markers = []
    if target_path:
        for path, marker in PAGE_TESTID_MARKERS.items():
            if path != target_path:
                foreign_markers.append(marker)
    
    for index, item in enumerate(findings):
        if not isinstance(item, dict):
            raise RuntimeError(f"Finding {index} is not a JSON object.")

        normalized_item = {key.lower().replace("-", "_"): value for key, value in item.items() if isinstance(key, str)}
        finding = {
            "element_selector": str(normalized_item.get("element_selector", "")).strip(),
            "expected_behavior": str(normalized_item.get("expected_behavior", "")).strip(),
            "observed_behavior": str(normalized_item.get("observed_behavior", "")).strip(),
            "severity": str(normalized_item.get("severity", "medium")).strip().lower(),
            "guideline_reference": str(normalized_item.get("guideline_reference", "")).strip(),
        }

        required_keys = ["element_selector", "expected_behavior", "observed_behavior"]
        missing = [key for key in required_keys if not finding[key]]
        if missing:
            raise RuntimeError(f"Finding {index} is missing required values: {missing}")
        if finding["severity"] not in {"critical", "high", "medium", "low"}:
            finding["severity"] = "medium"

        # Try to locate the element in the live DOM to correct the observed behavior text
        actual_text = find_dom_element_text(live_dom, finding["element_selector"])
        if actual_text is not None:
            finding["observed_behavior"] = actual_text

        # Drop compliant false positives where expected matches observed (case/non-alphanumeric normalized)
        expected_norm = re.sub(r"[^a-zA-Z0-9]+", "", finding["expected_behavior"]).lower()
        observed_norm = re.sub(r"[^a-zA-Z0-9]+", "", finding["observed_behavior"]).lower()
        if expected_norm == observed_norm:
            logger.info("Dropping compliant false positive finding: selector=%s, expected=%s", finding["element_selector"], finding["expected_behavior"])
            continue

        # Drop findings whose selectors reference elements from OTHER pages (cross-page contamination)
        selector = finding["element_selector"]
        is_foreign = False
        for marker in foreign_markers:
            if marker in selector:
                logger.info("Dropping cross-page contaminated finding: selector=%s contains foreign marker '%s' (target_path=%s)", selector, marker, target_path)
                is_foreign = True
                break
        if is_foreign:
            continue

        # Deduplicate by selector (keep first occurrence)
        if selector in seen_selectors:
            logger.info("Dropping duplicate finding for selector=%s", selector)
            continue
        seen_selectors.add(selector)

        normalized.append(finding)
    return normalized


def choose_compliance_model(live_dom_payload: Any, retrieved_guidelines_text: str) -> str:
    """Dynamically route compliance audits to the optimal model based on payload size."""
    if not retrieved_guidelines_text or not retrieved_guidelines_text.strip() or retrieved_guidelines_text.strip() == "[]":
        logger.info("Routing audit: BYPASS_LLM (No guidelines found)")
        return "BYPASS_LLM"
        
    # Serialize payload to check size
    try:
        payload_str = json.dumps(live_dom_payload)
        payload_len = len(payload_str)
    except Exception:
        payload_len = 0
        
    logger.info("DOM payload length: %d characters", payload_len)
    
    # Check if payload size is small (< 12k chars)
    if payload_len > 0 and payload_len < 12000:
        logger.info("Routing audit to Qwen/Qwen2.5-1.5B-Instruct (Low-latency path)")
        return "Qwen/Qwen2.5-1.5B-Instruct"
        
    logger.info("Routing audit to Qwen/Qwen2.5-7B-Instruct (High-reasoning path)")
    return "Qwen/Qwen2.5-7B-Instruct"



def run_compliance_check(
    target_path: str,
    live_dom_json: Any,
    retrieved_guidelines_text: str,
    scrape_error_flag: str | None = None,
    *,
    model_name: str = DEFAULT_MODEL,
    max_new_tokens: int = MAX_NEW_TOKENS,
    temperature: float = 0.0,
    repair_attempts: int = 1,
) -> list[dict[str, str]]:
    """Return a strict JSON-compatible discrepancy list."""
    if scrape_error_flag:
        logger.warning("Bypassing LLM for %s because scrape_error_flag=%s", target_path, scrape_error_flag)
        return static_scrape_failure_report(target_path, scrape_error_flag)

    messages = build_messages(target_path, live_dom_json, retrieved_guidelines_text)
    raw = call_hf_inference(
        messages,
        model_name=model_name,
        max_new_tokens=max_new_tokens,
        temperature=temperature,
    )

    for attempt in range(repair_attempts + 1):
        try:
            return validate_findings(parse_json_array(raw), target_path=target_path, live_dom=live_dom_json)
        except Exception as exc:
            if attempt >= repair_attempts:
                raise RuntimeError(f"LLM returned invalid discrepancy JSON: {exc}\nRaw output:\n{raw}") from exc
            logger.warning("Invalid LLM JSON; requesting repair: %s", exc)
            raw = call_hf_inference(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": REPAIR_PROMPT_TEMPLATE.format(bad_output=raw)},
                ],
                model_name=model_name,
                max_new_tokens=max_new_tokens,
                temperature=0.0,
            )

    raise RuntimeError("Unexpected LLM validation failure.")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run strict LLM compliance comparison.")
    parser.add_argument("--target-path", required=True)
    parser.add_argument("--live-dom-json", required=True, help="Path to capture JSON.")
    parser.add_argument("--guidelines", required=True, help="Path to retrieved guidelines JSON or text.")
    parser.add_argument("--scrape-error-flag")
    parser.add_argument("--output")
    parser.add_argument("--model", default=os.environ.get("COMPLIANCE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    if load_dotenv is not None:
        load_dotenv()
    args = parse_args()
    configure_logging(args.verbose)

    try:
        live_dom_json = load_json(Path(args.live_dom_json))
        guidelines_path = Path(args.guidelines)
        raw_guidelines = guidelines_path.read_text(encoding="utf-8")
        try:
            guidelines_payload = json.loads(raw_guidelines)
            retrieved_guidelines_text = json.dumps(guidelines_payload, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            retrieved_guidelines_text = raw_guidelines

        findings = run_compliance_check(
            target_path=args.target_path,
            live_dom_json=live_dom_json,
            retrieved_guidelines_text=retrieved_guidelines_text,
            scrape_error_flag=args.scrape_error_flag,
            model_name=args.model,
        )
        output = json.dumps(findings, indent=2, ensure_ascii=False)
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(output, encoding="utf-8")
        else:
            print(output)
    except Exception as exc:
        logger.exception("Compliance judge failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
