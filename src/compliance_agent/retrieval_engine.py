"""
Retrieve original guideline chunks for live page text using Supabase hybrid search.

Expected environment variables:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY

Example:
  python retrieval_engine.py --text-file captured_states/dashboard-20260622-130000.html --url-path /dashboard
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sentence_transformers import SentenceTransformer
from supabase import Client, create_client

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - optional, script falls back to regex stripping
    BeautifulSoup = None

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional local convenience
    load_dotenv = None


DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_RPC = "match_guidelines"
DEFAULT_MATCH_COUNT = 5
DEFAULT_SIMILARITY_THRESHOLD = 0.2


logger = logging.getLogger("retrieval_engine")


@dataclass(frozen=True)
class RetrievedRule:
    id: str
    section_name: str
    url_path: str
    content: str
    similarity: float
    keyword_rank: float
    hybrid_score: float


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def normalize_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def html_to_text(raw: str) -> str:
    if "<" not in raw or ">" not in raw:
        return normalize_text(raw)

    if BeautifulSoup is not None:
        soup = BeautifulSoup(raw, "html.parser")
        for node in soup(["script", "style", "svg", "noscript"]):
            node.decompose()
        return normalize_text(soup.get_text(" "))

    stripped = re.sub(r"<(script|style|svg|noscript)[\s\S]*?</\1>", " ", raw, flags=re.IGNORECASE)
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    return normalize_text(stripped)


def get_supabase_client() -> Client:
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")

    if not supabase_url:
        raise RuntimeError("Missing SUPABASE_URL environment variable.")
    if not supabase_key:
        raise RuntimeError("Missing SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY environment variable.")

    return create_client(supabase_url, supabase_key)


_MODEL_CACHE: dict[str, SentenceTransformer] = {}


def get_sentence_transformer(model_name: str) -> SentenceTransformer:
    global _MODEL_CACHE
    if model_name not in _MODEL_CACHE:
        logger.info("Loading embedding model: %s", model_name)
        _MODEL_CACHE[model_name] = SentenceTransformer(model_name)
    return _MODEL_CACHE[model_name]


def embed_text(text: str, model_name: str = DEFAULT_MODEL) -> list[float]:
    cleaned = normalize_text(text)
    if not cleaned:
        raise ValueError("Cannot embed empty page text.")

    model = get_sentence_transformer(model_name)
    embedding = model.encode(cleaned, normalize_embeddings=True, show_progress_bar=False)
    vector = [float(value) for value in embedding.tolist()]

    get_dim_fn = getattr(model, "get_embedding_dimension", None) or getattr(model, "get_sentence_embedding_dimension")
    expected_dim = int(get_dim_fn())
    if len(vector) != expected_dim:
        raise RuntimeError(f"Expected {expected_dim}-dimensional embedding, got {len(vector)} dimensions.")

    return vector


def retrieve_matching_rules(
    page_text: str,
    target_url_path: str | None,
    *,
    client: Client | None = None,
    model_name: str = DEFAULT_MODEL,
    rpc_name: str = DEFAULT_RPC,
    match_count: int = DEFAULT_MATCH_COUNT,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    metadata_weight: float = 0.15,
    vector_weight: float = 0.70,
    keyword_weight: float = 0.15,
) -> list[RetrievedRule]:
    """
    Embed live page text and retrieve the most relevant guideline chunks.

    target_url_path narrows results to exact or prefix-matching guideline url_path values.
    Pass None to search across all stored guidelines.
    """
    cleaned_text = normalize_text(page_text)
    if not cleaned_text:
        raise ValueError("page_text must not be empty.")
    if match_count < 1:
        raise ValueError("match_count must be at least 1.")

    supabase = client or get_supabase_client()
    query_embedding = embed_text(cleaned_text, model_name=model_name)

    params: dict[str, Any] = {
        "query_embedding": query_embedding,
        "filter_url_path": target_url_path,
        "similarity_threshold": similarity_threshold,
        "limit_count": match_count,
    }

    logger.info("Calling Supabase RPC '%s' for filter_url_path=%s", rpc_name, target_url_path or "*")
    
    # Secure connection retry loop for Supabase RPC calls
    import time as _time
    response = None
    for attempt in range(1, 4):
        try:
            response = supabase.rpc(rpc_name, params).execute()
            break
        except Exception as exc:
            if attempt == 3:
                raise RuntimeError(f"Supabase RPC '{rpc_name}' failed after 3 attempts: {exc}") from exc
            wait_sec = 2 ** attempt
            logger.warning("Supabase connection attempt %s failed; retrying in %ss: %s", attempt, wait_sec, exc)
            _time.sleep(wait_sec)

    if response is None:
        raise RuntimeError(f"Supabase RPC '{rpc_name}' did not return a response.")

    rows = response.data
    if not isinstance(rows, list):
        rows = []
    rules: list[RetrievedRule] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            val_similarity: Any = row.get("similarity")
            val_keyword_rank: Any = row.get("keyword_rank")
            val_hybrid_score: Any = row.get("hybrid_score")
            
            similarity = float(val_similarity) if val_similarity is not None else 0.0
            keyword_rank = float(val_keyword_rank) if val_keyword_rank is not None else 0.0
            hybrid_score = float(val_hybrid_score) if val_hybrid_score is not None else 0.0

            rules.append(
                RetrievedRule(
                    id=str(row["id"]),
                    section_name=str(row["section_name"]),
                    url_path=str(row["url_path"]),
                    content=str(row["content"]),
                    similarity=similarity,
                    keyword_rank=keyword_rank,
                    hybrid_score=hybrid_score,
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise RuntimeError(f"RPC response formatting issue or missing field: {exc}") from exc

    logger.info("Retrieved %s matching rule chunk(s)", len(rules))
    return rules


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Retrieve guideline rule context for live page text.")
    parser.add_argument("--text", help="Raw page text to search with.")
    parser.add_argument("--text-file", help="Path to a text, HTML, or captured-state JSON file.")
    parser.add_argument("--url-path", help="Target page path metadata filter, such as /dashboard.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="SentenceTransformer model name.")
    parser.add_argument("--rpc-name", default=DEFAULT_RPC, help="Supabase RPC function name.")
    parser.add_argument("--match-count", type=int, default=DEFAULT_MATCH_COUNT, help="Number of rule chunks to return.")
    parser.add_argument(
        "--similarity-threshold",
        type=float,
        default=DEFAULT_SIMILARITY_THRESHOLD,
        help="Minimum vector similarity before hybrid ranking.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args()


def read_input_text(args: argparse.Namespace) -> str:
    if args.text:
        return normalize_text(args.text)
    if not args.text_file:
        raise RuntimeError("Provide either --text or --text-file.")

    path = Path(args.text_file).expanduser().resolve()
    if not path.exists():
        raise RuntimeError(f"Text file not found: {path}")

    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        payload = json.loads(raw)
        element_groups = payload.get("elements", {})
        parts: list[str] = []
        for group in element_groups.values():
            if isinstance(group, list):
                parts.extend(str(item.get("text", "")) for item in group if isinstance(item, dict))
        if parts:
            return normalize_text(" ".join(parts))

    return html_to_text(raw)


def main() -> int:
    if load_dotenv is not None:
        load_dotenv()

    args = parse_args()
    configure_logging(args.verbose)

    try:
        page_text = read_input_text(args)
        rules = retrieve_matching_rules(
            page_text=page_text,
            target_url_path=args.url_path,
            model_name=args.model,
            rpc_name=args.rpc_name,
            match_count=args.match_count,
            similarity_threshold=args.similarity_threshold,
        )
        print(json.dumps([asdict(rule) for rule in rules], indent=2, ensure_ascii=False))
    except KeyboardInterrupt:
        logger.error("Interrupted by user")
        return 130
    except Exception as exc:
        logger.exception("Retrieval failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
