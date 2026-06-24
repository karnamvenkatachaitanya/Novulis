"""
Small GitHub REST API client for compliance issue creation.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests


logger = logging.getLogger("github_client")


def _github_config() -> tuple[str, str, str] | None:
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
        logger.error("GitHub issue creation skipped; missing environment variables: %s", ", ".join(missing))
        return None

    return token, owner, repo


def create_github_issue(title: str, body: str, labels: list[str]) -> int | None:
    """Create a GitHub issue and return its issue number when successful."""
    config = _github_config()
    if config is None:
        return None

    token, owner, repo = config
    url = f"https://api.github.com/repos/{owner}/{repo}/issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "waiverpro-compliance-agent",
    }
    payload: dict[str, Any] = {
        "title": title,
        "body": body,
        "labels": labels,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
    except requests.RequestException as exc:
        logger.error("GitHub issue creation request failed: %s", exc)
        return None

    if response.status_code not in {200, 201}:
        logger.error(
            "GitHub issue creation failed with status %s. Raw response: %s",
            response.status_code,
            response.text,
        )
        return None

    try:
        issue = response.json()
    except ValueError:
        logger.error("GitHub issue creation returned non-JSON response: %s", response.text)
        return None

    issue_number = issue.get("number")
    if not isinstance(issue_number, int):
        logger.error("GitHub issue creation response did not include issue number: %s", response.text)
        return None

    logger.info("Created GitHub issue #%s: %s", issue_number, title)
    return issue_number
