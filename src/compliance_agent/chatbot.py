"""
RAG Chatbot for WaiverPro Compliance Dashboard.

Classifies user intent via LLM prompting and routes queries to:
  - dashboard_snapshots (current live data)
  - guideline_embeddings (historical PDF guidelines)
  - Action execution (trigger scraping)

Usage:
    python -m compliance_agent.chatbot --message "What applications are pending?"
    python -m compliance_agent.chatbot --message "Scrape /dashboard/tickets" --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Generator

from huggingface_hub import InferenceClient
from supabase import Client, create_client

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


DEFAULT_LLM_MODEL = os.environ.get("CHATBOT_LLM_MODEL", "Qwen/Qwen2.5-7B-Instruct")
DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
MAX_CONTEXT_CHARS = 12_000
MAX_NEW_TOKENS = int(os.environ.get("CHATBOT_MAX_NEW_TOKENS", "1024"))

logger = logging.getLogger("chatbot")


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

# ── All known page paths for intent route extraction ──
ALL_PAGE_PATHS = [
    "/", "/login", "/dashboard/my-applications", "/dashboard/facilities",
    "/dashboard/action-items", "/dashboard/user-management", "/dashboard/announcements",
    "/dashboard/settings", "/dashboard/faqs", "/dashboard/tickets",
    "/dashboard/contact", "/privacy", "/terms",
]

PAGE_ALIASES = {
    "/": ("home", "landing", "root"),
    "/login": ("login", "sign in", "signin"),
    "/dashboard/my-applications": ("my applications", "applications", "application status"),
    "/dashboard/facilities": ("facilities", "facility"),
    "/dashboard/action-items": ("action items", "actions", "tasks"),
    "/dashboard/user-management": ("user management", "users", "user list"),
    "/dashboard/announcements": ("announcements", "announcement"),
    "/dashboard/settings": ("settings", "setting"),
    "/dashboard/faqs": ("faqs", "faq", "questions"),
    "/dashboard/tickets": ("tickets", "ticket", "support ticket"),
    "/dashboard/contact": ("contact", "support contact"),
    "/privacy": ("privacy", "privacy policy"),
    "/terms": ("terms", "terms of service"),
}

GENERAL_MESSAGES = {
    "hi",
    "hello",
    "hey",
    "thanks",
    "thank you",
    "bye",
    "goodbye",
}

ACTION_KEYWORDS = (
    "scrape",
    "crawl",
    "refresh",
    "rescrape",
    "re-scrape",
    "update data",
    "fetch latest",
    "pull latest",
)

GUIDELINE_KEYWORDS = (
    "should",
    "expected",
    "guideline",
    "guidelines",
    "policy",
    "policies",
    "documentation",
    "docs",
    "requirement",
    "requirements",
    "compliance rule",
    "supposed to",
)

CURRENT_KEYWORDS = (
    "current",
    "currently",
    "live",
    "latest",
    "right now",
    "now",
    "displayed",
    "open",
    "pending",
    "count",
    "status",
    "show me",
    "what is on",
    "what's on",
)

DASHBOARD_KEYWORDS = (
    "waiverpro",
    "dashboard",
    "compliance",
    "scrape",
    "guideline",
    "guidelines",
    "page",
    "ticket",
    "tickets",
    "application",
    "applications",
    "facility",
    "facilities",
    "login",
)

OFF_TOPIC_KEYWORDS = (
    "poem",
    "prime number",
    "history of",
    "recipe",
    "weather",
    "stock price",
    "write code",
    "write a code",
    "joke",
)

# ── Intent Classification ──

INTENT_SYSTEM_PROMPT = """You are an intent classifier for a web application compliance dashboard chatbot.

Given a user message, classify it into exactly ONE of these categories:

- QUERY_CURRENT: User asks about current/live/latest dashboard data, page content, application statuses, user lists, ticket counts, what is currently displayed, etc.
- QUERY_GUIDELINES: User asks about rules, policies, how things SHOULD work, what the documentation says, expected behavior, compliance requirements, what should be displayed.
- ACTION_SCRAPE: User wants to trigger a fresh scrape/refresh of dashboard data. They might say "scrape", "refresh", "update", "fetch latest", etc.
- GENERAL: Friendly greetings (e.g., "hi", "hello"), farewells, thanks, or small talk.
- OFF_TOPIC: Questions completely unrelated to WaiverPro, compliance guidelines, or dashboard operations (e.g. general coding requests, history, general knowledge, poems, writing tasks, math, etc.).

Also extract any specific page path mentioned. Known paths: /, /login, /dashboard/my-applications, /dashboard/facilities, /dashboard/action-items, /dashboard/user-management, /dashboard/announcements, /dashboard/settings, /dashboard/faqs, /dashboard/tickets, /dashboard/contact, /privacy, /terms.

Respond in this exact JSON format only:
{"intent": "CATEGORY_NAME", "page_path": "/path/or/null"}

Examples:
- "What tickets are open?" → {"intent": "QUERY_CURRENT", "page_path": "/dashboard/tickets"}
- "What should the login page show?" → {"intent": "QUERY_GUIDELINES", "page_path": "/login"}
- "Scrape the facilities page" → {"intent": "ACTION_SCRAPE", "page_path": "/dashboard/facilities"}
- "Hi there!" → {"intent": "GENERAL", "page_path": null}
- "write a code for prime number" → {"intent": "OFF_TOPIC", "page_path": null}
- "tell me a poem on chaitanya" → {"intent": "OFF_TOPIC", "page_path": null}
- "What's on the dashboard right now?" → {"intent": "QUERY_CURRENT", "page_path": null}"""


OFF_TOPIC_REFUSAL = "I am sorry, but I can only assist with questions regarding the WaiverPro Compliance Dashboard, scraping operations, and compliance guidelines. How can I help you with WaiverPro compliance today?"


@dataclass
class IntentResult:
    intent: str  # QUERY_CURRENT | QUERY_GUIDELINES | ACTION_SCRAPE | GENERAL
    page_path: str | None


@dataclass
class ChatResponse:
    answer: str
    intent: str
    source: str  # "live_data" | "guidelines" | "action" | "general"
    page_path: str | None
    chunks_used: int


def extract_page_path_fast(message: str) -> str | None:
    """Extract a known page path without an LLM call."""
    text = message.lower()
    for page_path in sorted(ALL_PAGE_PATHS, key=len, reverse=True):
        if page_path != "/" and page_path.lower() in text:
            return page_path

    for page_path, aliases in PAGE_ALIASES.items():
        if page_path == "/" and not any(alias in text for alias in aliases):
            continue
        if any(alias in text for alias in aliases):
            return page_path

    return None


def classify_intent_fast(message: str) -> IntentResult | None:
    """Classify obvious messages locally to avoid a remote LLM round trip."""
    text = " ".join(message.lower().strip().split())
    if not text:
        return IntentResult(intent="GENERAL", page_path=None)

    page_path = extract_page_path_fast(text)

    if text in GENERAL_MESSAGES or re.fullmatch(r"(hi|hello|hey)[!. ]*", text):
        return IntentResult(intent="GENERAL", page_path=None)

    if any(keyword in text for keyword in ACTION_KEYWORDS):
        return IntentResult(intent="ACTION_SCRAPE", page_path=page_path)

    has_dashboard_context = page_path is not None or any(keyword in text for keyword in DASHBOARD_KEYWORDS)

    if has_dashboard_context and any(keyword in text for keyword in GUIDELINE_KEYWORDS):
        return IntentResult(intent="QUERY_GUIDELINES", page_path=page_path)

    if has_dashboard_context and any(keyword in text for keyword in CURRENT_KEYWORDS):
        return IntentResult(intent="QUERY_CURRENT", page_path=page_path)

    if any(keyword in text for keyword in OFF_TOPIC_KEYWORDS) and not has_dashboard_context:
        return IntentResult(intent="OFF_TOPIC", page_path=None)

    return None


# ── Infrastructure ──

def get_supabase_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url:
        raise RuntimeError("Missing SUPABASE_URL")
    if not key:
        raise RuntimeError("Missing SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY")
    return create_client(url, key)


def get_llm_client() -> InferenceClient:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACEHUB_API_TOKEN")
    return InferenceClient(token=token)


def embed_query(text: str, model_name: str = DEFAULT_EMBED_MODEL) -> list[float]:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACEHUB_API_TOKEN")
    client = InferenceClient(token=token)

    # 1. Try serverless feature extraction for lightning-fast embeddings
    for attempt in range(1, 4):
        try:
            res = client.feature_extraction(text, model=model_name)
            if hasattr(res, "tolist"):
                res = res.tolist()
            if isinstance(res, (list, tuple)):
                if len(res) > 0 and isinstance(res[0], (list, tuple)):
                    res = res[0]
                return [float(v) for v in res]
        except Exception as exc:
            if attempt == 3:
                logger.warning("HF Serverless embedding failed, falling back to local: %s", exc)
                break
            time.sleep(0.5)

    # 2. Local fallback if API is rate-limited or offline (lazy loaded)
    logger.info("Falling back to local SentenceTransformer for embedding...")
    from sentence_transformers import SentenceTransformer
    local_model = SentenceTransformer(model_name)
    embedding = local_model.encode(text, normalize_embeddings=True, show_progress_bar=False)
    return [float(v) for v in embedding.tolist()]


# ── Intent Classification ──

def classify_intent(message: str, llm_client: InferenceClient, model: str = DEFAULT_LLM_MODEL) -> IntentResult:
    """Use LLM to classify user intent and extract target page path."""
    logger.info("Classifying intent for message: %s", message[:80])

    fast_result = classify_intent_fast(message)
    if fast_result is not None:
        logger.info("Fast intent: %s, Page: %s", fast_result.intent, fast_result.page_path)
        return fast_result

    # Try model candidates sequentially
    candidate_models = [model, "Qwen/Qwen2.5-Coder-7B-Instruct", "Qwen/Qwen2.5-7B-Instruct"]
    seen = set()
    candidate_models = [m for m in candidate_models if not (m in seen or seen.add(m))]

    last_err = None
    for candidate in candidate_models:
        try:
            logger.info("Attempting intent classification using model: %s", candidate)
            response = llm_client.chat_completion(
                model=candidate,
                messages=[
                    {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                    {"role": "user", "content": f"{message}\n/no_think"},
                ],
                max_tokens=100,
                temperature=0.1,
            )
            raw = response.choices[0].message.content
            if raw is None:
                raw = ""
            raw = raw.strip()
            logger.debug("Intent raw response: %s", raw)

            # Parse the JSON response
            json_match = re.search(r"\{.*\}", raw, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
                intent = parsed.get("intent", "GENERAL").upper()
                page_path = parsed.get("page_path")
                if page_path and page_path not in ALL_PAGE_PATHS:
                    page_path = None
                return IntentResult(intent=intent, page_path=page_path)
        except Exception as exc:
            logger.warning("Intent classification failed for model %s: %s", candidate, exc)
            last_err = exc

    logger.warning("All classification models failed. Defaulting to GENERAL.")
    return IntentResult(intent="GENERAL", page_path=None)


# ── Retrieval ──

def retrieve_current_data(
    query: str,
    page_path: str | None,
    client: Client,
    top_k: int = 8,
) -> list[dict]:
    """Retrieve matching chunks from dashboard_snapshots."""
    query_embedding = embed_query(query)
    params: dict[str, Any] = {
        "query_embedding": query_embedding,
        "similarity_threshold": 0.1,
        "limit_count": top_k,
    }
    if page_path:
        params["filter_page_path"] = page_path

    response = None
    import time as _time
    for attempt in range(1, 4):
        try:
            response = client.rpc("match_dashboard_snapshots", params).execute()
            break
        except Exception as exc:
            if attempt == 3:
                raise RuntimeError(f"Supabase RPC 'match_dashboard_snapshots' failed after 3 attempts: {exc}") from exc
            wait_sec = 2 ** attempt
            logger.warning("Supabase connection attempt %s failed; retrying in %ss: %s", attempt, wait_sec, exc)
            _time.sleep(wait_sec)

    if response is None:
        return []

    rows = response.data
    if not isinstance(rows, list):
        rows = []
    
    dict_rows = [dict(item) for item in rows if isinstance(item, dict)]
    logger.info("Retrieved %d current data chunk(s)", len(dict_rows))
    return dict_rows


def retrieve_guidelines(
    query: str,
    page_path: str | None,
    client: Client,
    top_k: int = 8,
) -> list[dict]:
    """Retrieve matching chunks from guideline_embeddings."""
    query_embedding = embed_query(query)
    params: dict[str, Any] = {
        "query_embedding": query_embedding,
        "similarity_threshold": 0.1,
        "limit_count": top_k,
        "filter_url_path": page_path,
    }

    response = None
    import time as _time
    for attempt in range(1, 4):
        try:
            response = client.rpc("match_guidelines", params).execute()
            break
        except Exception as exc:
            if attempt == 3:
                raise RuntimeError(f"Supabase RPC 'match_guidelines' failed after 3 attempts: {exc}") from exc
            wait_sec = 2 ** attempt
            logger.warning("Supabase connection attempt %s failed; retrying in %ss: %s", attempt, wait_sec, exc)
            _time.sleep(wait_sec)

    if response is None:
        return []

    rows = response.data
    if not isinstance(rows, list):
        rows = []

    dict_rows = [dict(item) for item in rows if isinstance(item, dict)]
    logger.info("Retrieved %d guideline chunk(s)", len(dict_rows))
    return dict_rows


# ── Answer Generation ──

def build_context(chunks: list[dict], source_type: str) -> str:
    """Build a context string from retrieved chunks."""
    if not chunks:
        return "No relevant data found."

    parts: list[str] = []
    total_len = 0
    for chunk in chunks:
        content = chunk.get("content", "")
        label = chunk.get("section_label") or chunk.get("section_name", "")
        path = chunk.get("page_path") or chunk.get("url_path", "")
        entry = f"[{source_type}] Page: {path} | Section: {label}\n{content}"
        if total_len + len(entry) > MAX_CONTEXT_CHARS:
            break
        parts.append(entry)
        total_len += len(entry)

    return "\n\n---\n\n".join(parts)


def inject_overview_if_needed(message: str, context: str) -> str:
    lower_msg = message.lower()
    is_overview = any(k in lower_msg for k in [
        "what is waiverpro", "what is waiver pro", "services", "provide", "about waiverpro", 
        "what do you do", "features", "what tools", "healthcare waiver", "overview"
    ])
    if is_overview:
        overview = """[Core Overview] Page: / | Section: About WaiverPro
WaiverPro is a healthcare waiver management platform. It provides tools for managing waiver applications, facilities, action items, user management, announcements, and settings.
The brand appears at the top of the sidebar with the label "Healthcare Waiver Management".
Additional support items include FAQs, Tickets, Contact, and a Take a Tour option."""
        if not context or context == "No relevant data found.":
            return overview
        return f"{overview}\n\n---\n\n{context}"
    return context


def generate_answer_stream(
    message: str,
    context: str,
    source_type: str,
    llm_client: InferenceClient,
    model: str = DEFAULT_LLM_MODEL,
) -> Generator[str, None, None]:
    """Generate a streaming answer using retrieved context."""
    if source_type == "General Knowledge":
        system_prompt = """You are a helpful compliance chatbot assistant for WaiverPro.
WaiverPro is a healthcare waiver management platform. It provides tools for managing waiver applications, facilities, action items, user management, announcements, and settings.
The brand appears at the top of the sidebar with the label "Healthcare Waiver Management".
Additional support items include FAQs, Tickets, Contact, and a Take a Tour option.

Greet the user, be friendly, and answer their question based on the above information about WaiverPro. Also explain that you can help them view detailed compliance guidelines, check current live dashboard data, or trigger a fresh scrape."""
        user_prompt = f"User Question: {message}"
    else:
        system_prompt = f"""You are a helpful assistant for the WaiverPro compliance dashboard.
You answer questions based on the provided context data.

Data source: {source_type}
- If source is "Live Dashboard Data": You are describing what is CURRENTLY on the dashboard.
- If source is "PDF Guidelines": You are describing what SHOULD be displayed according to the official documentation.

Rules:
- Answer based ONLY on the provided context. Do not make up information.
- Treat the retrieved context as the only source of truth.
- Be concise and specific.
- If the context does not contain enough information, say exactly what is missing and do not guess.
- Use bullet points for lists.
- Mention the page path when relevant.
- Do not show reasoning steps.
- Do not use outside knowledge about WaiverPro."""

        user_prompt = f"""Context:
{context}

User Question: {message}

Answer:
/no_think"""

    # Try model candidates sequentially
    candidate_models = [model, "Qwen/Qwen2.5-Coder-7B-Instruct", "Qwen/Qwen2.5-7B-Instruct"]
    seen = set()
    candidate_models = [m for m in candidate_models if not (m in seen or seen.add(m))]

    last_err = None
    for candidate in candidate_models:
        try:
            logger.info("Attempting generation using model: %s", candidate)
            stream = llm_client.chat_completion(
                model=candidate,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=MAX_NEW_TOKENS,
                temperature=0.3,
                stream=True,
            )

            # Test connection immediately by pulling the first chunk
            stream_iter = iter(stream)
            first_chunk = next(stream_iter)

            # Define a helper to reconstruct the stream
            def wrapper_generator():
                yield first_chunk
                for item in stream_iter:
                    yield item

            buffer = ""
            stripped = False
            for chunk in wrapper_generator():
                if chunk.choices and chunk.choices[0].delta.content:
                    val = chunk.choices[0].delta.content
                    if not stripped:
                        buffer += val
                        if len(buffer) >= 10:
                            if buffer.startswith("/no_think"):
                                buffer = buffer[len("/no_think"):].lstrip()
                            yield buffer
                            buffer = ""
                            stripped = True
                    else:
                        yield val
            if buffer:
                if not stripped and buffer.startswith("/no_think"):
                    buffer = buffer[len("/no_think"):].lstrip()
                yield buffer

            return # Success! Exit generator.
        except Exception as exc:
            logger.warning("Generation failed for model %s: %s", candidate, exc)
            last_err = exc

    if last_err:
        raise last_err


def generate_answer(
    message: str,
    context: str,
    source_type: str,
    llm_client: InferenceClient,
    model: str = DEFAULT_LLM_MODEL,
) -> str:
    """Generate a non-streaming answer (for CLI usage)."""
    parts = list(generate_answer_stream(message, context, source_type, llm_client, model))
    return "".join(parts)


# ── Action Execution ──

def execute_scrape_action(page_path: str | None) -> str:
    """Trigger a fresh crawl scrape and ingest for the specified page(s)."""
    import subprocess
    import sys
    
    target_desc = page_path or "all pages"
    logger.info("Executing scrape action for: %s", target_desc)
    
    # We call main.py with --no-email and --no-github-issues
    cmd = [sys.executable, "main.py", "--no-email", "--no-github-issues"]
    if page_path:
        cmd.extend(["--target-path", page_path])
        
    try:
        logger.info("Running compliance scraper command: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            cwd=".",  # Executed from root directory
            capture_output=True,
            text=True,
            timeout=180,  # 3-minute timeout
        )
        if result.returncode in (0, 2):  # 0 = clean, 2 = issues found. Both mean successful run.
            output = result.stdout + "\n" + result.stderr
            match = re.search(r"Finished inserting (\d+) snapshot record\(s\)", output)
            count_str = match.group(1) if match else "some"
            return f"Successfully scraped {target_desc} and indexed {count_str} chunk(s) into Supabase! You can now ask questions about the latest content."
        else:
            return f"Scraper execution finished with code {result.returncode}. Error details:\n{result.stderr[-400:]}"
    except subprocess.TimeoutExpired:
        return f"Scrape action timed out after 3 minutes for {target_desc}."
    except Exception as exc:
        logger.exception("Scrape action failed: %s", exc)
        return f"Scrape action failed to launch: {exc}"


# ── Main Chat Handler ──

def chat(message: str, dry_run: bool = False) -> ChatResponse:
    """Process a single chat message end-to-end."""
    llm_client = get_llm_client()

    # Step 1: Classify intent
    intent_result = classify_intent(message, llm_client)
    logger.info("Intent: %s, Page: %s", intent_result.intent, intent_result.page_path)

    # Step 2: Route based on intent
    if intent_result.intent == "OFF_TOPIC":
        return ChatResponse(
            answer=OFF_TOPIC_REFUSAL,
            intent=intent_result.intent,
            source="general",
            page_path=None,
            chunks_used=0,
        )

    if intent_result.intent == "ACTION_SCRAPE":
        if dry_run:
            answer = f"[DRY RUN] Would scrape: {intent_result.page_path or 'all pages'}"
        else:
            answer = execute_scrape_action(intent_result.page_path)
        return ChatResponse(
            answer=answer,
            intent=intent_result.intent,
            source="action",
            page_path=intent_result.page_path,
            chunks_used=0,
        )

    if intent_result.intent == "GENERAL":
        answer = generate_answer(
            message,
            context="No specific data context needed.",
            source_type="General Knowledge",
            llm_client=llm_client,
        )
        return ChatResponse(
            answer=answer,
            intent=intent_result.intent,
            source="general",
            page_path=None,
            chunks_used=0,
        )

    # For QUERY_CURRENT and QUERY_GUIDELINES, retrieve context from Supabase
    client = get_supabase_client()

    if intent_result.intent == "QUERY_CURRENT":
        chunks = retrieve_current_data(message, intent_result.page_path, client)
        source_type = "Live Dashboard Data"
        source = "live_data"
    else:  # QUERY_GUIDELINES
        chunks = retrieve_guidelines(message, intent_result.page_path, client)
        source_type = "PDF Guidelines"
        source = "guidelines"

    context = build_context(chunks, source_type)
    context = inject_overview_if_needed(message, context)
    answer = generate_answer(message, context, source_type, llm_client)

    return ChatResponse(
        answer=answer,
        intent=intent_result.intent,
        source=source,
        page_path=intent_result.page_path,
        chunks_used=len(chunks),
    )


def chat_stream(message: str) -> Generator[dict, None, None]:
    """Process a chat message with streaming response. Yields dicts for SSE."""
    llm_client = get_llm_client()

    # Step 1: Classify intent
    intent_result = classify_intent(message, llm_client)
    logger.info("Intent: %s, Page: %s", intent_result.intent, intent_result.page_path)

    # Emit intent metadata
    yield {
        "type": "intent",
        "intent": intent_result.intent,
        "page_path": intent_result.page_path,
    }

    # Step 2: Route based on intent
    if intent_result.intent == "OFF_TOPIC":
        yield {"type": "token", "data": OFF_TOPIC_REFUSAL}
        yield {"type": "done", "source": "general", "chunks_used": 0}
        return

    if intent_result.intent == "ACTION_SCRAPE":
        yield {"type": "status", "data": "Scraping in progress..."}
        answer = execute_scrape_action(intent_result.page_path)
        yield {"type": "token", "data": answer}
        yield {"type": "done", "source": "action", "chunks_used": 0}
        return

    if intent_result.intent == "GENERAL":
        for token in generate_answer_stream(
            message, "No specific data context needed.", "General Knowledge", llm_client
        ):
            yield {"type": "token", "data": token}
        yield {"type": "done", "source": "general", "chunks_used": 0}
        return

    # QUERY_CURRENT or QUERY_GUIDELINES
    client = get_supabase_client()

    if intent_result.intent == "QUERY_CURRENT":
        yield {"type": "status", "data": "Searching live dashboard data..."}
        chunks = retrieve_current_data(message, intent_result.page_path, client)
        source_type = "Live Dashboard Data"
        source = "live_data"
    else:
        yield {"type": "status", "data": "Searching PDF guidelines..."}
        chunks = retrieve_guidelines(message, intent_result.page_path, client)
        source_type = "PDF Guidelines"
        source = "guidelines"

    context = build_context(chunks, source_type)
    context = inject_overview_if_needed(message, context)

    for token in generate_answer_stream(message, context, source_type, llm_client):
        yield {"type": "token", "data": token}

    yield {"type": "done", "source": source, "chunks_used": len(chunks)}


# ── CLI ──

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RAG Chatbot for WaiverPro Compliance Dashboard.")
    parser.add_argument("--message", "-m", required=True, help="Chat message to process.")
    parser.add_argument("--dry-run", action="store_true", help="Classify intent only, skip actions.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args()


def main() -> int:
    if load_dotenv is not None:
        load_dotenv()

    args = parse_args()
    configure_logging(args.verbose)

    try:
        response = chat(args.message, dry_run=args.dry_run)
        print(json.dumps(asdict(response), indent=2, ensure_ascii=False))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logger.exception("Chat failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

# EOF
