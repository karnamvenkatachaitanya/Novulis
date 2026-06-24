"""
Ingest WaiverPro guideline PDF content into Supabase pgvector storage.

Expected environment variables:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY

Example:
  python ingest_guidelines.py --pdf WaiverPro-User-Guidelines.pdf
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader
from sentence_transformers import SentenceTransformer
from supabase import Client, create_client

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional convenience dependency
    load_dotenv = None


DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_TABLE = "guideline_embeddings"
DEFAULT_PDF = "WaiverPro-User-Guidelines-WITH-DISCREPANCIES.pdf"
MAX_CHARS_PER_CHUNK = 2_400
CHUNK_OVERLAP_CHARS = 250
DEFAULT_BATCH_SIZE = 100


def determine_url_path(section_name: str, content: str) -> str:
    combined = (section_name + " " + content).lower()
    if "/dashboard/settings" in combined or "settings" in combined or "profile" in combined:
        return "/dashboard/settings"
    elif "/dashboard/my-applications" in combined or "my applications" in combined:
        return "/dashboard/my-applications"
    elif "/dashboard/facilities" in combined or "facilities" in combined:
        return "/dashboard/facilities"
    elif "/dashboard/action-items" in combined or "action items" in combined:
        return "/dashboard/action-items"
    elif "/dashboard/user-management" in combined or "user management" in combined:
        return "/dashboard/user-management"
    elif "/dashboard/announcements" in combined or "announcements" in combined:
        return "/dashboard/announcements"
    elif "/dashboard/faqs" in combined or "faqs" in combined:
        return "/dashboard/faqs"
    elif "/dashboard/tickets" in combined or "tickets" in combined:
        return "/dashboard/tickets"
    elif "/dashboard/contact" in combined or "contact" in combined:
        return "/dashboard/contact"
    elif "privacy" in combined:
        return "/privacy"
    elif "terms" in combined:
        return "/terms"
    elif "/login" in combined or "login" in combined or "signing in" in combined or "credentials" in combined:
        return "/login"
    elif "workspace" in combined or "new waiver" in combined:
        return "/dashboard/my-applications"
    return "/dashboard/my-applications"



logger = logging.getLogger("ingest_guidelines")


@dataclass(frozen=True)
class GuidelineChunk:
    section_name: str
    url_path: str
    content: str
    page_start: int
    page_end: int


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def normalize_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "section"


def is_probable_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 120:
        return False

    # Check for Section/Appendix prefixes
    if re.match(r'^(SECTION|Section|APPENDIX|Appendix)\s+\d+', line, re.IGNORECASE):
        return True

    clean_line = re.sub(r'^[0-9.\s]+', '', line).strip()
    known_headings = {
        "About This Guide", "Accessing WaiverPro — The Landing Page", "Signing In", 
        "The Application Workspace & Navigation", "My Applications", 
        "Submitting a New Waiver Application", "Facilities", "Action Items", 
        "User Management", "Announcements", "Support — FAQs, Tickets & Contact", 
        "Settings", "Legal — Privacy & Terms", "Appendix — Status Reference",
        "CONVENTIONS", "The sidebar", "The header", "TIP", "Note", "Contact", 
        "Support Tickets", "FAQs"
    }
    if clean_line in known_headings or line in known_headings:
        return True

    numbered_heading = re.match(r"^(\d+(\.\d+)*|[A-Z])[\).:-]?\s+[A-Z][A-Za-z0-9 ,/&()'-]{2,}$", line)
    title_case_words = re.findall(r"[A-Za-z][A-Za-z'-]*", line)
    if numbered_heading:
        return True
    if len(title_case_words) < 2:
        return False

    uppercase_ratio = sum(1 for char in line if char.isupper()) / max(1, sum(1 for char in line if char.isalpha()))
    title_case_ratio = sum(1 for word in title_case_words if word[:1].isupper()) / len(title_case_words)
    return uppercase_ratio > 0.55 or title_case_ratio > 0.75


def split_large_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        candidate = text[start:end]

        if end < len(text):
            paragraph_break = candidate.rfind("\n\n")
            sentence_break = max(candidate.rfind(". "), candidate.rfind("? "), candidate.rfind("! "))
            split_at = paragraph_break if paragraph_break > max_chars * 0.55 else sentence_break
            if split_at > max_chars * 0.45:
                end = start + split_at + 1
                candidate = text[start:end]

        candidate = candidate.strip()
        if candidate:
            chunks.append(candidate)

        if end >= len(text):
            break
        start = max(0, end - overlap_chars)

    return chunks


def extract_page_text(pdf_path: Path) -> list[tuple[int, str]]:
    logger.info("Reading PDF: %s", pdf_path)
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as exc:
        raise RuntimeError(f"Unable to open PDF at {pdf_path}: {exc}") from exc

    pages: list[tuple[int, str]] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = normalize_text(page.extract_text() or "")
        except Exception as exc:
            logger.warning("Skipping page %s because text extraction failed: %s", index, exc)
            continue

        if not text:
            logger.warning("Page %s has no extractable text", index)
            continue
        pages.append((index, text))

    if not pages:
        raise RuntimeError("No extractable text found. If this PDF is scanned, run OCR first.")

    logger.info("Extracted text from %s page(s)", len(pages))
    return pages


def hierarchical_chunk_pages(
    pages: list[tuple[int, str]],
    source_name: str,
    max_chars: int,
    overlap_chars: int,
) -> list[GuidelineChunk]:
    chunks: list[GuidelineChunk] = []
    current_heading = "Introduction"
    current_lines: list[str] = []
    current_start_page = pages[0][0]
    current_end_page = pages[0][0]

    def flush() -> None:
        nonlocal current_lines
        content = normalize_text("\n".join(current_lines))
        if not content:
            current_lines = []
            return

        split_parts = split_large_text(content, max_chars=max_chars, overlap_chars=overlap_chars)
        for part_index, part in enumerate(split_parts, start=1):
            section_label = current_heading
            if len(split_parts) > 1:
                section_label = f"{current_heading} - Part {part_index}"
            url_path = determine_url_path(section_label, part)
            chunks.append(
                GuidelineChunk(
                    section_name=section_label,
                    url_path=url_path,
                    content=part,
                    page_start=current_start_page,
                    page_end=current_end_page,
                )
            )
        current_lines = []

    for page_number, text in pages:
        current_end_page = page_number
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in lines:
            if is_probable_heading(line) and current_lines:
                flush()
                current_heading = line
                current_start_page = page_number
                current_end_page = page_number
            else:
                current_lines.append(line)

    flush()

    if not chunks:
        raise RuntimeError("Chunking produced no content.")

    logger.info("Created %s hierarchical chunk(s)", len(chunks))
    return chunks


def get_supabase_client() -> Client:
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")

    if not supabase_url:
        raise RuntimeError("Missing SUPABASE_URL environment variable.")
    if not supabase_key:
        raise RuntimeError("Missing SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY environment variable.")

    return create_client(supabase_url, supabase_key)


def chunked(items: list[dict], size: int) -> Iterable[list[dict]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def build_records(chunks: list[GuidelineChunk], model_name: str) -> list[dict]:
    logger.info("Loading embedding model: %s", model_name)
    model = SentenceTransformer(model_name)

    logger.info("Generating embeddings locally")
    embeddings = model.encode(
        [chunk.content for chunk in chunks],
        batch_size=32,
        normalize_embeddings=True,
        show_progress_bar=True,
    )

    records: list[dict] = []
    for chunk, embedding in zip(chunks, embeddings):
        vector = [float(value) for value in embedding.tolist()]
        if len(vector) != 384:
            raise RuntimeError(f"Expected 384-dimensional embedding, got {len(vector)} dimensions.")

        records.append(
            {
                "section_name": chunk.section_name,
                "url_path": chunk.url_path,
                "content": chunk.content,
                "embedding": vector,
            }
        )

    return records


def insert_records(client: Client | None, table_name: str, records: list[dict], batch_size: int, dry_run: bool) -> None:
    if dry_run:
        logger.info("Dry run enabled. Prepared %s record(s), skipping Supabase insert.", len(records))
        return

    if client is not None:
        target_paths = list(set(record["url_path"] for record in records))
        logger.info("Cleaning existing entries for url_paths %s to guarantee idempotency...", target_paths)
        for path in target_paths:
            try:
                client.table(table_name).delete().eq("url_path", path).execute()
            except Exception as exc:
                logger.warning("Failed to delete existing entries for url_path %s: %s", path, exc)

    logger.info("Inserting %s record(s) into Supabase table '%s'", len(records), table_name)
    inserted = 0
    for batch_number, batch in enumerate(chunked(records, batch_size), start=1):
        for attempt in range(1, 4):
            try:
                if client is None:
                    raise RuntimeError("Supabase client was not initialized.")
                client.table(table_name).insert(batch).execute()
                inserted += len(batch)
                logger.info("Inserted batch %s containing %s record(s)", batch_number, len(batch))
                break
            except Exception as exc:
                if attempt == 3:
                    raise RuntimeError(f"Failed to insert batch {batch_number}: {exc}") from exc
                sleep_seconds = 2**attempt
                logger.warning(
                    "Batch %s insert failed on attempt %s; retrying in %ss: %s",
                    batch_number,
                    attempt,
                    sleep_seconds,
                    exc,
                )
                time.sleep(sleep_seconds)

    logger.info("Finished inserting %s record(s)", inserted)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest WaiverPro guideline PDF into Supabase pgvector.")
    parser.add_argument("--pdf", default=DEFAULT_PDF, help="Path to the local guideline PDF.")
    parser.add_argument("--table", default=DEFAULT_TABLE, help="Supabase table name.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="SentenceTransformer model name.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Supabase insert batch size.")
    parser.add_argument("--max-chars", type=int, default=MAX_CHARS_PER_CHUNK, help="Maximum characters per chunk.")
    parser.add_argument("--overlap-chars", type=int, default=CHUNK_OVERLAP_CHARS, help="Overlap for split long chunks.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and embed without inserting into Supabase.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)
    if load_dotenv is not None:
        load_dotenv()

    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        logger.error("PDF not found: %s", pdf_path)
        return 1
    if args.batch_size < 1:
        logger.error("--batch-size must be at least 1")
        return 1
    if args.overlap_chars >= args.max_chars:
        logger.error("--overlap-chars must be smaller than --max-chars")
        return 1

    try:
        pages = extract_page_text(pdf_path)
        chunks = hierarchical_chunk_pages(
            pages=pages,
            source_name=pdf_path.stem,
            max_chars=args.max_chars,
            overlap_chars=args.overlap_chars,
        )
        records = build_records(chunks, model_name=args.model)
        client = get_supabase_client() if not args.dry_run else None
        insert_records(client, args.table, records, args.batch_size, args.dry_run)
    except KeyboardInterrupt:
        logger.error("Interrupted by user")
        return 130
    except Exception as exc:
        logger.exception("Ingestion failed: %s", exc)
        return 1

    logger.info("Ingestion complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
