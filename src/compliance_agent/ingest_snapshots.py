"""
Ingest scraped dashboard page captures into Supabase dashboard_snapshots table.

Reads the latest captured_states JSON files, chunks the DOM elements into
meaningful text segments, embeds them, and upserts into the dashboard_snapshots
table for RAG chatbot retrieval.

Usage:
    python -m compliance_agent.ingest_snapshots --verbose
    python -m compliance_agent.ingest_snapshots --pages /dashboard/my-applications /login
"""

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

from sentence_transformers import SentenceTransformer
from supabase import Client, create_client

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


CAPTURE_DIR = Path("captured_states")
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_TABLE = "dashboard_snapshots"
MAX_CHUNK_CHARS = 1_500

logger = logging.getLogger("ingest_snapshots")


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def get_supabase_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url:
        raise RuntimeError("Missing SUPABASE_URL environment variable.")
    if not key:
        raise RuntimeError("Missing SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY environment variable.")
    return create_client(url, key)


@dataclass
class SnapshotChunk:
    page_path: str
    section_label: str
    content: str


def path_from_filename(filename: str) -> str:
    """Derive the URL path from a captured_states filename.

    Examples:
        dashboard_my-applications-20260629-162057.json -> /dashboard/my-applications
        home-20260629-155924.json -> /
        login-20260629-155948.json -> /login
        privacy-20260629-160249.json -> /privacy
    """
    # Strip timestamp and extension
    base = re.sub(r"-\d{8}-\d{6}\.\w+$", "", filename)

    if base == "home":
        return "/"

    # Convert underscores back to slashes for dashboard paths
    path = "/" + base.replace("_", "/")
    return path


def find_latest_captures(pages: list[str] | None = None) -> dict[str, Path]:
    """Find the latest JSON capture file for each page path.

    Returns a dict mapping page_path -> Path to the latest JSON file.
    """
    if not CAPTURE_DIR.exists():
        logger.warning("Capture directory '%s' does not exist.", CAPTURE_DIR)
        return {}

    # Group JSON files by their page path
    path_files: dict[str, list[Path]] = {}
    for f in CAPTURE_DIR.iterdir():
        if not f.is_file() or not f.name.endswith(".json"):
            continue
        page_path = path_from_filename(f.name)
        if pages and page_path not in pages:
            continue
        path_files.setdefault(page_path, []).append(f)

    # Select the latest file for each path (lexicographic sort on timestamp)
    result: dict[str, Path] = {}
    for page_path, files in path_files.items():
        latest = sorted(files, key=lambda p: p.name)[-1]
        result[page_path] = latest

    logger.info("Found latest captures for %d page(s): %s", len(result), list(result.keys()))
    return result


def chunk_capture_json(page_path: str, json_path: Path) -> list[SnapshotChunk]:
    """Parse a captured-state JSON file and produce text chunks from DOM elements."""
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read capture JSON %s: %s", json_path, exc)
        return []

    elements = data.get("elements", {})
    if not elements and "dom" in data:
        elements = data["dom"]

    chunks: list[SnapshotChunk] = []

    # Group DOM element types into labeled sections
    group_labels = {
        "headings": "Page Headings",
        "buttons": "Interactive Buttons",
        "links": "Navigation Links",
        "inputs": "Form Inputs",
        "selects": "Dropdown Selects",
        "textareas": "Text Areas",
        "images": "Images & Media",
        "tables": "Data Tables",
        "lists": "List Items",
        "paragraphs": "Text Content",
        "labels": "Form Labels",
        "spans": "Inline Text",
        "divs": "Content Blocks",
    }

    for group_key, items in elements.items():
        if not isinstance(items, list) or len(items) == 0:
            continue

        label = group_labels.get(group_key, group_key.replace("_", " ").title())

        # Extract text from each element in the group
        texts: list[str] = []
        for item in items:
            if isinstance(item, dict):
                text = item.get("text", "") or item.get("innerText", "") or item.get("value", "")
                selector = item.get("selector", "")
                if text and len(text.strip()) > 0:
                    clean = text.strip()[:500]  # cap individual element text
                    texts.append(clean)
            elif isinstance(item, str):
                if item.strip():
                    texts.append(item.strip()[:500])

        if not texts:
            continue

        # Combine texts into chunks, respecting MAX_CHUNK_CHARS
        current_chunk: list[str] = []
        current_len = 0

        for t in texts:
            if current_len + len(t) + 2 > MAX_CHUNK_CHARS and current_chunk:
                content = f"[{label}] " + " | ".join(current_chunk)
                chunks.append(SnapshotChunk(
                    page_path=page_path,
                    section_label=label,
                    content=content,
                ))
                current_chunk = []
                current_len = 0

            current_chunk.append(t)
            current_len += len(t) + 2

        # Flush remaining
        if current_chunk:
            content = f"[{label}] " + " | ".join(current_chunk)
            chunks.append(SnapshotChunk(
                page_path=page_path,
                section_label=label,
                content=content,
            ))

    # Also add the page title if available
    title = data.get("title", "")
    if title:
        chunks.insert(0, SnapshotChunk(
            page_path=page_path,
            section_label="Page Title",
            content=f"[Page Title] {title}",
        ))

    logger.info("Chunked %s into %d chunk(s)", json_path.name, len(chunks))
    return chunks


def build_records(chunks: list[SnapshotChunk], model_name: str) -> list[dict]:
    """Embed all chunks and return Supabase-ready records."""
    if not chunks:
        return []

    logger.info("Loading embedding model: %s", model_name)
    model = SentenceTransformer(model_name)

    texts = [c.content for c in chunks]
    logger.info("Generating embeddings for %d chunk(s)...", len(texts))
    embeddings = model.encode(
        texts,
        batch_size=32,
        normalize_embeddings=True,
        show_progress_bar=True,
    )

    records: list[dict] = []
    for chunk, embedding in zip(chunks, embeddings):
        vector = [float(v) for v in embedding.tolist()]
        if len(vector) != 384:
            raise RuntimeError(f"Expected 384-dim embedding, got {len(vector)}")

        records.append({
            "page_path": chunk.page_path,
            "section_label": chunk.section_label,
            "content": chunk.content,
            "embedding": vector,
        })

    return records


def upsert_records(client: Client, table_name: str, records: list[dict], page_paths: list[str]) -> None:
    """Delete existing rows for the given page paths, then insert new records."""
    # Clear old data for the pages being refreshed
    for path in page_paths:
        try:
            client.table(table_name).delete().eq("page_path", path).execute()
            logger.info("Cleared old snapshots for page_path=%s", path)
        except Exception as exc:
            logger.warning("Failed to clear old snapshots for %s: %s", path, exc)

    # Insert new records in batches
    batch_size = 50
    inserted = 0
    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        for attempt in range(1, 4):
            try:
                client.table(table_name).insert(batch).execute()
                inserted += len(batch)
                logger.info("Inserted batch of %d record(s) (total: %d)", len(batch), inserted)
                break
            except Exception as exc:
                if attempt == 3:
                    raise RuntimeError(f"Failed to insert batch after 3 attempts: {exc}") from exc
                wait = 2 ** attempt
                logger.warning("Insert attempt %d failed, retrying in %ds: %s", attempt, wait, exc)
                time.sleep(wait)

    logger.info("Finished inserting %d snapshot record(s)", inserted)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest scraped dashboard captures into Supabase for RAG chatbot.")
    parser.add_argument("--pages", nargs="*", help="Specific page paths to ingest (e.g., /dashboard/my-applications). Default: all found.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="SentenceTransformer model name.")
    parser.add_argument("--table", default=DEFAULT_TABLE, help="Supabase table name.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and embed without inserting.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args()


def ingest_snapshots(
    pages: list[str] | None = None,
    model_name: str = DEFAULT_MODEL,
    table_name: str = DEFAULT_TABLE,
    dry_run: bool = False,
) -> int:
    """Main ingestion function. Can be called programmatically by the chatbot."""
    captures = find_latest_captures(pages)
    if not captures:
        logger.warning("No capture files found to ingest.")
        return 0

    all_chunks: list[SnapshotChunk] = []
    for page_path, json_path in captures.items():
        chunks = chunk_capture_json(page_path, json_path)
        all_chunks.extend(chunks)

    if not all_chunks:
        logger.warning("No chunks produced from captures.")
        return 0

    records = build_records(all_chunks, model_name)

    if dry_run:
        logger.info("Dry run: %d record(s) prepared, skipping Supabase insert.", len(records))
        return len(records)

    client = get_supabase_client()
    page_paths = list(captures.keys())
    upsert_records(client, table_name, records, page_paths)
    return len(records)


def main() -> int:
    if load_dotenv is not None:
        load_dotenv()

    args = parse_args()
    configure_logging(args.verbose)

    try:
        count = ingest_snapshots(
            pages=args.pages,
            model_name=args.model,
            table_name=args.table,
            dry_run=args.dry_run,
        )
        logger.info("Ingestion complete: %d record(s) processed.", count)
    except KeyboardInterrupt:
        logger.error("Interrupted by user")
        return 130
    except Exception as exc:
        logger.exception("Snapshot ingestion failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
