from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import torch
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

try:
    from huggingface_hub import InferenceClient
    HAS_HF_CLIENT = True
except ImportError:
    HAS_HF_CLIENT = False

try:
    from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig
    HAS_TRANSFORMERS = True
except ImportError:
    HAS_TRANSFORMERS = False

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional local convenience
    load_dotenv = None


DEFAULT_MODEL = "mistralai/Mistral-7B-Instruct-v0.3"
DEFAULT_MAX_NEW_TOKENS = 1_800
DEFAULT_TEMPERATURE = 0.0
MAX_ELEMENTS_PER_GROUP = 80
MAX_RULE_CHARS = 12_000


logger = logging.getLogger("compliance_agent")


SYSTEM_PROMPT = """You are a strict compliance judge. You compare a system's documented guidelines against the live user interface state to find violations.

Compare the Documented Rules Context against the Live DOM UI JSON State.
Identify any compliance discrepancies. For each discrepancy, determine the severity level (High, Medium, or Low).
You must act as a strict compliance judge and return a machine-readable, schema-valid raw JSON array of discrepancies.
If the live site complies perfectly and there are no discrepancies, output an empty JSON array `[]`.
Do not output any introductory text, markdown fences, notes, or explanations outside of the JSON array.
"""


USER_PROMPT_TEMPLATE = """Compare the two blocks:

### Documented Rules Context:
{retrieved_guidelines}

### Live DOM UI JSON State:
{live_page_state}

Return a raw JSON array representing the Discrepancy Report.
Each object in the array must detail:
- `element_selector`: selector hint, label, text, or selector description of the UI element in violation
- `expected_rule_behavior`: specific requirement from the guidelines
- `observed_live_behavior`: what was actually observed in the live DOM
- `severity_level`: compliance violation severity ("High", "Medium", or "Low")
- `screenshot_file`: file path of the screenshot captured for this page state

Required JSON Schema:
[
  {{
    "element_selector": "string",
    "expected_rule_behavior": "string",
    "observed_live_behavior": "string",
    "severity_level": "High" | "Medium" | "Low",
    "screenshot_file": "string"
  }}
]
"""


JSON_REPAIR_PROMPT_TEMPLATE = """The previous answer was not valid JSON or did not match the required schema.
Return the same compliance report as strict raw JSON only. No markdown. No commentary.

Invalid answer:
{invalid_answer}
"""


@dataclass(frozen=True)
class ModelBundle:
    tokenizer: Any
    model: Any
    use_api: bool = False
    api_client: Any = None
    model_name: str = ""


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def load_json_file(path: Path) -> Any:
    if not path.exists():
        raise RuntimeError(f"File not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def compact_text(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def compact_element(element: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": element.get("role"),
        "tag": element.get("tag"),
        "text": compact_text(str(element.get("text", "")), 260),
        "selector_hint": element.get("selector_hint"),
        "attributes": element.get("attributes", {}),
        "bounds": element.get("bounds"),
    }


def compact_page_state(page_state: dict[str, Any]) -> dict[str, Any]:
    elements = page_state.get("elements", {})
    compact_groups: dict[str, list[dict[str, Any]]] = {}
    if isinstance(elements, dict):
        for group_name, group_items in elements.items():
            if not isinstance(group_items, list):
                continue
            compact_groups[group_name] = [
                compact_element(item)
                for item in group_items[:MAX_ELEMENTS_PER_GROUP]
                if isinstance(item, dict)
            ]

    return {
        "url": page_state.get("url"),
        "title": page_state.get("title"),
        "screenshot_path": page_state.get("screenshot_path"),
        "html_path": page_state.get("html_path"),
        "elements": compact_groups,
    }


def compact_guidelines(guidelines: Any) -> list[dict[str, Any]]:
    if isinstance(guidelines, dict) and "rules" in guidelines:
        guidelines = guidelines["rules"]
    if not isinstance(guidelines, list):
        raise RuntimeError("Guidelines input must be a list, or an object with a 'rules' list.")

    compacted: list[dict[str, Any]] = []
    used_chars = 0
    for item in guidelines:
        if not isinstance(item, dict):
            continue
        content = compact_text(str(item.get("content", "")), 2_500)
        if not content:
            continue
        used_chars += len(content)
        if used_chars > MAX_RULE_CHARS:
            break
        compacted.append(
            {
                "id": item.get("id"),
                "section_name": item.get("section_name"),
                "url_path": item.get("url_path"),
                "similarity": item.get("similarity"),
                "hybrid_score": item.get("hybrid_score"),
                "content": content,
            }
        )

    if not compacted:
        raise RuntimeError("No usable guideline chunks found.")
    return compacted


def build_messages(page_state: dict[str, Any], guidelines: list[dict[str, Any]]) -> list[dict[str, str]]:
    prompt = USER_PROMPT_TEMPLATE.format(
        live_page_state=json.dumps(page_state, indent=2, ensure_ascii=False),
        retrieved_guidelines=json.dumps(guidelines, indent=2, ensure_ascii=False),
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]


def _use_inference_api() -> bool:
    """Decide whether to use HF Inference API vs local model."""
    if HAS_TORCH and torch.cuda.is_available():
        return False
    if HAS_HF_CLIENT and os.environ.get("HF_TOKEN"):
        return True
    return False


def load_model(model_name: str, dtype: str) -> ModelBundle:
    if _use_inference_api():
        logger.info("No GPU detected. Using Hugging Face Inference API for model: %s", model_name)
        hf_token = os.environ.get("HF_TOKEN", "")
        client = InferenceClient(token=hf_token)
        return ModelBundle(
            tokenizer=None,
            model=None,
            use_api=True,
            api_client=client,
            model_name=model_name,
        )

    logger.info("Loading instruction model locally: %s", model_name)
    tokenizer = AutoTokenizer.from_pretrained(model_name, token=os.environ.get("HF_TOKEN"))
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token

    torch_dtype = {
        "auto": "auto",
        "float16": torch.float16,
        "bfloat16": torch.bfloat16,
        "float32": torch.float32,
    }[dtype]

    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        token=os.environ.get("HF_TOKEN"),
        device_map="auto",
        torch_dtype=torch_dtype,
        low_cpu_mem_usage=True,
    )
    model.eval()
    return ModelBundle(tokenizer=tokenizer, model=model)


def generate_text(
    bundle: ModelBundle,
    messages: list[dict[str, str]],
    *,
    max_new_tokens: int,
    temperature: float,
) -> str:
    if bundle.use_api:
        logger.info("Sending inference request to HF API for model: %s", bundle.model_name)
        for attempt in range(1, 4):
            try:
                response = bundle.api_client.chat_completion(
                    model=bundle.model_name,
                    messages=messages,
                    max_tokens=max_new_tokens,
                    temperature=max(temperature, 0.01),
                )
                return response.choices[0].message.content.strip()
            except Exception as exc:
                if attempt == 3:
                    raise RuntimeError(f"HF Inference API failed after 3 attempts: {exc}") from exc
                wait_s = 2 ** attempt
                logger.warning("HF API attempt %d failed, retrying in %ds: %s", attempt, wait_s, exc)
                time.sleep(wait_s)

    prompt = render_chat_prompt(bundle.tokenizer, messages)
    inputs = bundle.tokenizer(prompt, return_tensors="pt", truncation=True, max_length=24_000)
    inputs = {key: value.to(bundle.model.device) for key, value in inputs.items()}

    do_sample = temperature > 0
    generation_config = GenerationConfig(
        max_new_tokens=max_new_tokens,
        temperature=temperature if do_sample else None,
        do_sample=do_sample,
        pad_token_id=bundle.tokenizer.pad_token_id,
        eos_token_id=bundle.tokenizer.eos_token_id,
        repetition_penalty=1.03,
    )

    with torch.inference_mode():
        output = bundle.model.generate(**inputs, generation_config=generation_config)

    generated_ids = output[0][inputs["input_ids"].shape[-1] :]
    return bundle.tokenizer.decode(generated_ids, skip_special_tokens=True).strip()


def render_chat_prompt(tokenizer: Any, messages: list[dict[str, str]]) -> str:
    if hasattr(tokenizer, "apply_chat_template") and tokenizer.chat_template:
        return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    return "\n\n".join(f"{message['role'].upper()}:\n{message['content']}" for message in messages) + "\n\nASSISTANT:\n"


def extract_json_object(text: str) -> list[dict[str, Any]]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start == -1 or end == -1 or end <= start:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise
            parsed = [json.loads(cleaned[start : end + 1])]
        else:
            parsed = json.loads(cleaned[start : end + 1])

    if not isinstance(parsed, list):
        if isinstance(parsed, dict):
            return [parsed]
        raise RuntimeError("Model output must be a JSON array or object.")
    return parsed


def validate_report(report: Any) -> list[dict[str, Any]]:
    if not isinstance(report, list):
        raise RuntimeError("Report must be a JSON list.")

    required_fields = {
        "element_selector",
        "expected_rule_behavior",
        "observed_live_behavior",
        "severity_level",
        "screenshot_file",
    }
    for index, discrepancy in enumerate(report):
        if not isinstance(discrepancy, dict):
            raise RuntimeError(f"Discrepancy at index {index} must be a JSON object.")

        normalized = {}
        for k, v in discrepancy.items():
            norm_k = k.lower().replace(" ", "_").replace("-", "_")
            normalized[norm_k] = v

        selector = (
            normalized.get("element_selector") or 
            normalized.get("selector") or 
            normalized.get("element_identifier") or 
            normalized.get("element") or 
            "Unknown element"
        )
        expected = (
            normalized.get("expected_rule_behavior") or 
            normalized.get("expected_behavior") or 
            normalized.get("expected_rule") or 
            normalized.get("what_rule_required") or 
            "Required rule not specified"
        )
        observed = (
            normalized.get("observed_live_behavior") or 
            normalized.get("observed_behavior") or 
            normalized.get("observed_live") or 
            normalized.get("what_live_site_has") or 
            "Observed behavior not specified"
        )
        severity = (
            normalized.get("severity_level") or 
            normalized.get("severity") or 
            "Medium"
        )
        screenshot = (
            normalized.get("screenshot_file") or 
            normalized.get("screenshot") or 
            normalized.get("screenshot_reference") or 
            "unknown_screenshot.png"
        )

        severity_str = str(severity).strip().capitalize()
        if severity_str not in {"High", "Medium", "Low"}:
            severity_str = "Medium"

        report[index] = {
            "element_selector": str(selector),
            "expected_rule_behavior": str(expected),
            "observed_live_behavior": str(observed),
            "severity_level": severity_str,
            "screenshot_file": str(screenshot),
        }

    return report


def run_compliance_check(
    page_state: dict[str, Any],
    guidelines: Any,
    *,
    model_name: str = DEFAULT_MODEL,
    dtype: str = "auto",
    max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    repair_attempts: int = 1,
) -> list[dict[str, Any]]:
    screenshot_file = page_state.get("screenshot_path", "")
    if screenshot_file:
        screenshot_file_name = Path(screenshot_file).name
        page_state["screenshot_file"] = screenshot_file_name

    compacted_page_state = compact_page_state(page_state)
    compacted_page_state["screenshot_file"] = page_state.get("screenshot_file", "screenshot.png")
    compacted_guidelines = compact_guidelines(guidelines)
    messages = build_messages(compacted_page_state, compacted_guidelines)
    bundle = load_model(model_name=model_name, dtype=dtype)

    raw_answer = generate_text(
        bundle,
        messages,
        max_new_tokens=max_new_tokens,
        temperature=temperature,
    )

    for attempt in range(repair_attempts + 1):
        try:
            report = validate_report(extract_json_object(raw_answer))
            return report
        except Exception as exc:
            if attempt >= repair_attempts:
                raise RuntimeError(f"Model did not produce valid report JSON: {exc}\nRaw output:\n{raw_answer}") from exc
            logger.warning("Model output failed validation; attempting JSON repair: %s", exc)
            repair_messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": JSON_REPAIR_PROMPT_TEMPLATE.format(invalid_answer=raw_answer)},
            ]
            raw_answer = generate_text(
                bundle,
                repair_messages,
                max_new_tokens=max_new_tokens,
                temperature=0.0,
            )

    raise RuntimeError("Unexpected compliance validation failure.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare live page state against retrieved guideline chunks.")
    parser.add_argument("--page-state", required=True, help="Path to JSON file from scraper.py.")
    parser.add_argument("--guidelines", required=True, help="Path to JSON output from retrieval_engine.py.")
    parser.add_argument("--output", help="Where to save the discrepancy report JSON.")
    parser.add_argument("--model", default=os.environ.get("COMPLIANCE_MODEL", DEFAULT_MODEL), help="Hugging Face instruct model.")
    parser.add_argument(
        "--dtype",
        choices=["auto", "float16", "bfloat16", "float32"],
        default=os.environ.get("COMPLIANCE_DTYPE", "auto"),
        help="Torch dtype for local inference.",
    )
    parser.add_argument("--max-new-tokens", type=int, default=DEFAULT_MAX_NEW_TOKENS)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--repair-attempts", type=int, default=1)
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args()


def main() -> int:
    if load_dotenv is not None:
        load_dotenv()

    args = parse_args()
    configure_logging(args.verbose)

    try:
        page_state = load_json_file(Path(args.page_state).expanduser().resolve())
        guidelines = load_json_file(Path(args.guidelines).expanduser().resolve())
        report = run_compliance_check(
            page_state=page_state,
            guidelines=guidelines,
            model_name=args.model,
            dtype=args.dtype,
            max_new_tokens=args.max_new_tokens,
            temperature=args.temperature,
            repair_attempts=args.repair_attempts,
        )

        output_json = json.dumps(report, indent=2, ensure_ascii=False)
        if args.output:
            output_path = Path(args.output).expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(output_json, encoding="utf-8")
            logger.info("Saved discrepancy report: %s", output_path)
        else:
            print(output_json)
    except KeyboardInterrupt:
        logger.error("Interrupted by user")
        return 130
    except Exception as exc:
        logger.exception("Compliance check failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
