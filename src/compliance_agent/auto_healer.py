"""
Automatically repair open GitHub compliance issues with an LLM-generated patch.

Expected issue body format:

    Source File: src/components/Sidebar.js
    Expected: The sidebar should include a visible Help link.
    Observed: The Help link is missing from the sidebar.

Configuration is read from environment variables:

    GITHUB_TOKEN
    GITHUB_REPO_OWNER
    GITHUB_REPO_NAME

    LLM_PROVIDER=openai|huggingface
    OPENAI_API_KEY
    OPENAI_MODEL

    HF_TOKEN
    HF_MODEL
    HF_API_URL

    AUTO_HEALER_TEST_COMMAND
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is optional at runtime.
    load_dotenv = None


try:
    from huggingface_hub import InferenceClient
except ImportError:
    InferenceClient = None

GITHUB_API_VERSION = "2022-11-28"
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_HF_MODEL = "Qwen/Qwen2.5-7B-Instruct"
REPAIR_INSTRUCTION = (
    "Modify the provided source code file to resolve the issue described in this "
    "compliance log. Return ONLY the completely updated source code file. Do not "
    "include markdown code block wrappers or chat pleasantries."
)

logger = logging.getLogger("auto_healer")


@dataclass(frozen=True)
class GitHubConfig:
    token: str
    owner: str
    repo: str


@dataclass(frozen=True)
class ComplianceIssue:
    number: int
    title: str
    body: str
    source_file: str
    expected: str
    observed: str


class AutoHealerError(RuntimeError):
    """Raised when the auto healer cannot safely complete a requested action."""


def load_environment() -> None:
    if load_dotenv is not None:
        load_dotenv()


def get_github_config() -> GitHubConfig:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    owner = os.environ.get("GITHUB_REPO_OWNER", "").strip()
    repo = os.environ.get("GITHUB_REPO_NAME", "").strip()

    missing = [
        name
        for name, value in {
            "GITHUB_TOKEN": token,
            "GITHUB_REPO_OWNER": owner,
            "GITHUB_REPO_NAME": repo,
        }.items()
        if not value
    ]
    if missing:
        raise AutoHealerError(f"Missing required environment variables: {', '.join(missing)}")

    return GitHubConfig(token=token, owner=owner, repo=repo)


def github_headers(config: GitHubConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "ai-compliance-auto-healer",
    }


def github_request(
    method: str,
    config: GitHubConfig,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> requests.Response:
    url = f"https://api.github.com/repos/{config.owner}/{config.repo}{path}"
    response = requests.request(
        method,
        url,
        headers=github_headers(config),
        params=params,
        json=payload,
        timeout=60,
    )
    if response.status_code >= 400:
        raise AutoHealerError(
            f"GitHub {method} {path} failed with status {response.status_code}: {response.text}"
        )
    return response


def fetch_open_bug_issues(config: GitHubConfig) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    page = 1

    while True:
        response = github_request(
            "GET",
            config,
            "/issues",
            params={
                "state": "open",
                "labels": "bug",
                "per_page": 100,
                "page": page,
            },
        )
        page_items = response.json()
        if not isinstance(page_items, list):
            raise AutoHealerError(f"Unexpected GitHub issues response: {response.text}")

        issues.extend(issue for issue in page_items if "pull_request" not in issue)
        if len(page_items) < 100:
            return issues
        page += 1


def extract_labeled_value(body: str, labels: list[str]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    next_label_pattern = (
        r"Source\s+File|Expected(?:\s+(?:Error\s+)?Message)?|Observed(?:\s+(?:Error\s+)?Message)?"
    )
    pattern = re.compile(
        rf"^\s*(?:{label_pattern})\s*:\s*(.*?)"
        rf"(?=^\s*(?:{next_label_pattern})\s*:|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(body)
    return match.group(1).strip() if match else ""


def parse_issue(issue: dict[str, Any]) -> ComplianceIssue | None:
    body = issue.get("body") or ""
    source_file = extract_labeled_value(body, ["Source File", "Source Path", "File"])
    expected = extract_labeled_value(body, ["Expected", "Expected Message", "Expected Error Message"])
    observed = extract_labeled_value(body, ["Observed", "Observed Message", "Observed Error Message"])

    if not source_file:
        logger.warning("Skipping issue #%s; no Source File label found.", issue.get("number"))
        return None

    if not expected or not observed:
        logger.warning(
            "Issue #%s is missing expected/observed text; continuing with full issue body context.",
            issue.get("number"),
        )

    issue_number = issue.get("number")
    if not isinstance(issue_number, int):
        logger.warning("Skipping issue without numeric issue number: %s", issue)
        return None

    return ComplianceIssue(
        number=issue_number,
        title=str(issue.get("title") or ""),
        body=body,
        source_file=source_file,
        expected=expected,
        observed=observed,
    )


def resolve_source_path(source_file: str, project_root: Path) -> Path:
    cleaned = source_file.strip().strip("`'\"")
    candidate = Path(cleaned)
    if candidate.is_absolute():
        target = candidate.resolve()
    else:
        target = (project_root / candidate).resolve()

    root = project_root.resolve()
    if target != root and root not in target.parents:
        raise AutoHealerError(f"Refusing to edit path outside project root: {source_file}")
    if not target.is_file():
        raise AutoHealerError(f"Source file does not exist: {target}")
    return target


def build_repair_prompt(issue: ComplianceIssue, source_path: Path, raw_code: str) -> str:
    return "\n".join(
        [
            REPAIR_INSTRUCTION,
            "",
            "Compliance issue:",
            f"Issue Number: {issue.number}",
            f"Issue Title: {issue.title}",
            f"Source File: {issue.source_file}",
            f"Expected: {issue.expected or '(not explicitly provided)'}",
            f"Observed: {issue.observed or '(not explicitly provided)'}",
            "",
            "Full GitHub issue body:",
            issue.body,
            "",
            f"Current contents of {source_path.name}:",
            raw_code,
        ]
    )


def infer_llm_provider() -> str:
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if provider:
        return provider
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("HF_TOKEN"):
        return "huggingface"
    raise AutoHealerError(
        "No LLM provider configured. Set LLM_PROVIDER plus OPENAI_API_KEY or HF_TOKEN."
    )


def call_openai(prompt: str) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise AutoHealerError("OPENAI_API_KEY is required when LLM_PROVIDER=openai.")

    model = os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL).strip()
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an expert AI remediation engineer. Return only a complete, "
                        "valid replacement file."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        },
        timeout=180,
    )
    if response.status_code >= 400:
        raise AutoHealerError(f"OpenAI request failed with status {response.status_code}: {response.text}")

    data = response.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AutoHealerError(f"Unexpected OpenAI response shape: {json.dumps(data)[:1000]}") from exc


def call_huggingface(prompt: str) -> str:
    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        raise AutoHealerError("HF_TOKEN is required when LLM_PROVIDER=huggingface.")

    model = os.environ.get("HF_MODEL", DEFAULT_HF_MODEL).strip()
    api_url = os.environ.get("HF_API_URL", "").strip()

    # Use InferenceClient if we have no custom api_url and InferenceClient is imported
    if InferenceClient is not None and not api_url:
        logger.info("Calling Hugging Face InferenceClient with model: %s", model)
        client = InferenceClient(token=token)
        try:
            response = client.chat_completion(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert AI software remediation engineer. Return only a complete, valid replacement file without markdown fence blocks.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=int(os.environ.get("HF_MAX_NEW_TOKENS", "4096")),
                temperature=0.1,
            )
            return response.choices[0].message.content.strip()
        except Exception as exc:
            logger.warning("InferenceClient chat_completion failed; falling back to direct API. Error: %s", exc)

    # Fallback to direct HTTP post request
    if not api_url:
        api_url = f"https://api-inference.huggingface.co/models/{model}"

    logger.info("Calling Hugging Face raw endpoint: %s", api_url)
    response = requests.post(
        api_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "inputs": prompt,
            "parameters": {
                "temperature": 0.1,
                "return_full_text": False,
                "max_new_tokens": int(os.environ.get("HF_MAX_NEW_TOKENS", "4096")),
            },
        },
        timeout=240,
    )
    if response.status_code >= 400:
        raise AutoHealerError(
            f"Hugging Face request failed with status {response.status_code}: {response.text}"
        )

    data = response.json()
    if isinstance(data, list) and data and isinstance(data[0], dict):
        generated = data[0].get("generated_text")
        if isinstance(generated, str):
            return generated
    if isinstance(data, dict):
        generated = data.get("generated_text")
        if isinstance(generated, str):
            return generated
    raise AutoHealerError(f"Unexpected Hugging Face response shape: {json.dumps(data)[:1000]}")


def generate_repair(prompt: str) -> str:
    provider = infer_llm_provider()
    if provider == "openai":
        repaired = call_openai(prompt)
    elif provider in {"huggingface", "hf"}:
        repaired = call_huggingface(prompt)
    else:
        raise AutoHealerError(f"Unsupported LLM_PROVIDER: {provider}")

    return strip_accidental_markdown_fence(repaired)


def strip_accidental_markdown_fence(text: str) -> str:
    stripped = text.strip()
    fence_match = re.fullmatch(r"```(?:[A-Za-z0-9_.+-]+)?\s*\n(.*)\n```", stripped, re.DOTALL)
    if fence_match:
        return fence_match.group(1)
    return text


def run_verification(project_root: Path) -> bool:
    command = os.environ.get("AUTO_HEALER_TEST_COMMAND", "").strip()
    if not command:
        logger.info("No AUTO_HEALER_TEST_COMMAND set; treating file write as verified.")
        return True

    logger.info("Running verification command: %s", command)
    completed = subprocess.run(
        command,
        cwd=project_root,
        shell=True,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.stdout:
        logger.info("Verification stdout:\n%s", completed.stdout)
    if completed.stderr:
        logger.warning("Verification stderr:\n%s", completed.stderr)

    if completed.returncode != 0:
        logger.error("Verification failed with exit code %s.", completed.returncode)
        return False
    return True


def comment_on_issue(config: GitHubConfig, issue_number: int, body: str) -> None:
    github_request(
        "POST",
        config,
        f"/issues/{issue_number}/comments",
        payload={"body": body},
    )


def close_issue(config: GitHubConfig, issue_number: int) -> None:
    github_request(
        "PATCH",
        config,
        f"/issues/{issue_number}",
        payload={"state": "closed"},
    )


def heal_issue(
    config: GitHubConfig | None,
    issue: ComplianceIssue,
    project_root: Path,
    *,
    dry_run: bool,
) -> bool:
    target_path = resolve_source_path(issue.source_file, project_root)
    logger.info("Healing issue #%s against %s", issue.number, target_path)

    raw_code = target_path.read_text(encoding="utf-8")
    prompt = build_repair_prompt(issue, target_path, raw_code)
    repaired_code = generate_repair(prompt)

    if not repaired_code.strip():
        raise AutoHealerError(f"LLM returned empty repaired code for issue #{issue.number}.")

    if dry_run:
        logger.info("Dry run enabled; not overwriting %s or closing issue #%s.", target_path, issue.number)
        return False

    target_path.write_text(repaired_code, encoding="utf-8")

    if not run_verification(project_root):
        logger.error("Issue #%s left open because verification failed.", issue.number)
        return False

    if config is not None:
        comment_on_issue(config, issue.number, "Resolved automatically by AI Compliance Agent.")
        close_issue(config, issue.number)
    else:
        logger.info("Mock GitHub issue comment and close completed successfully.")
    logger.info("Closed issue #%s.", issue.number)
    return True


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair open GitHub bug-labeled compliance issues using an LLM."
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Local repository root containing source files named in GitHub issue bodies.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and generate repairs without writing files or closing GitHub issues.",
    )
    parser.add_argument(
        "--issue-number",
        type=int,
        help="Limit remediation to a single GitHub issue number.",
    )
    parser.add_argument(
        "--mock-issues-file",
        help="Path to a local JSON file containing mock open GitHub issues for offline testing.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show debug logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    configure_logging(args.verbose)
    load_environment()

    try:
        project_root = Path(args.project_root).resolve()
        
        if args.mock_issues_file:
            logger.info("Running in mock mode. Loading issues from %s", args.mock_issues_file)
            mock_path = Path(args.mock_issues_file).resolve()
            if not mock_path.is_file():
                raise AutoHealerError(f"Mock issues file not found: {mock_path}")
            raw_issues = json.loads(mock_path.read_text(encoding="utf-8"))
            config = None
        else:
            config = get_github_config()
            raw_issues = fetch_open_bug_issues(config)

        parsed_issues = [parsed for issue in raw_issues if (parsed := parse_issue(issue)) is not None]
        if args.issue_number is not None:
            parsed_issues = [issue for issue in parsed_issues if issue.number == args.issue_number]

        if not parsed_issues:
            logger.info("No matching open compliance issues found.")
            return 0

        healed_count = 0
        for issue in parsed_issues:
            try:
                if heal_issue(config, issue, project_root, dry_run=args.dry_run):
                    healed_count += 1
            except AutoHealerError as exc:
                logger.error("Issue #%s failed: %s", issue.number, exc)

        logger.info("Auto-healer finished. Issues closed: %s/%s", healed_count, len(parsed_issues))
        return 0
    except AutoHealerError as exc:
        logger.error("%s", exc)
        return 1
    except requests.RequestException as exc:
        logger.error("Network request failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
