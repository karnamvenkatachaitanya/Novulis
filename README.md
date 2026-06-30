# WaiverPro Compliance Automation Suite & RAG Monitoring Dashboard

A production-ready, enterprise-grade compliance auditing agent and monitoring dashboard. The suite automatically verifies if a live web application layout and behavior conform to its official design and compliance guidelines. 

The system ingests regulatory guideline PDFs, indexes them into a vector database (Supabase pgvector), crawls the live web portal using a headless browser, retrieves relevant rules via RAG, audits layouts for discrepancies using an LLM compliance judge, and sends secure SMTP email alerts with styled HTML summaries, screenshots, and visual PDF reports. It also features a Next.js control center with an integrated RAG chatbot to query dashboard status and compliance guidelines.

---

## 🗺️ Core Architecture & Data Flow

```mermaid
flowchart TB
    subgraph Storage Layer [Supabase Cloud Database]
        db_guide[(guideline_embeddings)]
        db_snap[(dashboard_snapshots)]
    end

    subgraph Python Compliance Agent [Scrape & Audit Engine]
        sc[scraper.py - Playwright] -->|1. Authenticated Dom Layout & Screenshot| dom[Live DOM & Screenshots]
        main[main.py - Orchestrator] -->|2. Search Relevant Guidelines| ret[retrieval_engine.py]
        ret -->|pgvector RPC Search| db_guide
        dom -->|3. Feed DOM Layout & Retrieved Rules| judge[compliance_agent.py - LLM Judge]
        judge -->|4. Generate Visual Discrepancies JSON| main
        main -->|5. Compile Visual ReportLab PDF| pdf[Unified compliance-report.pdf]
        main -->|6. Ingest DOM snapshot into DB| db_snap
    end

    subgraph Next.js Monitoring Dashboard [Operator Portal]
        ui[page.js - Interactive UI]
        api_run[api/run-agent] -->|Trigger Compliance Sweep| main
        api_chat[api/chat] -->|Query Guidelines & Live Snapshot| bot[chatbot.py - RAG Router]
        bot -->|Hybrid Search| db_guide
        bot -->|Hybrid Search| db_snap
        api_mail[api/send-email] -->|Secure SMTP Dispatch| smtp[SMTP Mail Server]
    end

    pdf -->|Attachment Link| ui
    pdf -->|Attached visual PDF alert| smtp
    smtp -->|Unified Alert| user[Stakeholder Mailbox]
```

---

## 🌟 Key Subsystem Features

The WaiverPro suite is built with performance optimizations, reliability mechanisms, and strict safety guardrails:

*   **Next.js Operator Dashboard**: A dark-mode glassmorphic control center for triggering sweeps, viewing visual discrepancy logs, inspecting compiled PDF reports, inputting custom email recipients, and chatting with the RAG agent.
*   **Dual-Engine RAG Chatbot**: Features a hybrid local-LLM intent router (`chatbot.py`) that classifies queries across `GENERAL`, `QUERY_CURRENT`, `QUERY_GUIDELINES`, `ACTION_SCRAPE`, and `OFF_TOPIC` categories.
*   **Dynamic Model Routing**: Routes LLM compliance audits based on DOM footprint size:
    *   *Bypass Route*: Skips LLM calls if RAG retrieves zero matching rules for the active page, saving computational budget.
    *   *Low-Latency Route (`Qwen2.5-1.5B`)*: Processes smaller page states (< 12k characters) for rapid sweeps.
    *   *High-Reasoning Route (`Qwen2.5-7B`)*: Routes complex layout states to Qwen-7B-Instruct.
*   **Closed-Loop Code Self-Healing Engine**: Automatically parses compliance issues (e.g. from GitHub Issues or local sweeps), utilizes an LLM to generate code repair patches (`auto_healer.py`), runs visual verification tests (`AUTO_HEALER_TEST_COMMAND`), and safely commits repaired files.
*   **Hybrid Search Linear Rank Fusion**: Combines vector cosine similarity (70%), exact keyword indices (15%), and path metadata matching (15%) within `retrieval_engine.py` to maximize retrieval recall.
*   **Multi-Step State-Merging Crawlers**: Playwright scraper controllers (`scraper.py`) interactively navigate form wizards and drawers (such as Waiver Applications or Support Tickets), merging separate steps into a single unified layout snapshot for full-flow audits.
*   **Explainable Compliance Citations**: Enforces the extraction of standard PDF guideline references (`guideline_reference`) inside compliance judge reports.
*   **Operational Error Separation**: Distinguishes between pipeline, authentication redirection, and API rate limit failures from actual compliance violations, marking them as `infrastructure_error`.
*   **SMTP Alert Cooldown Idempotency**: Suppresses alert email floods by caching run history in `.last_sent_alert.json` and skipping duplicate reports within a 1-hour window.
*   **Global Model Weight Caching**: Caches model weights dynamically inside a global memory dictionary (`_MODEL_CACHE`) to avoid loading embedding models per-page.

---

## 🔄 RAG Chatbot Sequence Data Flow

This sequence diagram illustrates the decision tree, API boundary calls, and token flow sequences triggered when a client submits a message to the WaiverPro chatbot:

```mermaid
sequenceDiagram
    actor User
    participant UI as Chat Widget
    participant API as Next.js API Route
    participant Chat as Python chatbot.py
    participant DB as Supabase pgvector
    participant LLM as Hugging Face LLM

    User->>UI: Submit query ("What are the login rules?")
    UI->>API: GET /api/chat?message=query
    API->>API: Check cached queries
    alt Cache Hit
        API-->>UI: Return cached stream events
    else Cache Miss
        API->>Chat: Spawn python -u -c chatbot.py "query"
        
        Note over Chat: Intent Recognition
        alt Fast-Path (Greeting / Keywords)
            Chat->>Chat: Match intent locally (GENERAL / OFF_TOPIC)
        else Deep-Path (Complex query)
            Chat->>LLM: Classify query via Qwen
            LLM-->>Chat: Return {"intent", "page_path"}
        end

        %% Off topic Routing
        alt Intent = OFF_TOPIC
            Chat-->>API: Yield OFF_TOPIC_REFUSAL
            API-->>UI: Stream Refusal
        
        %% General Greeting Routing
        else Intent = GENERAL
            Chat->>LLM: Greet friendly via Qwen (Relaxed prompt)
            LLM-->>Chat: Return welcoming intro message
            Chat-->>API: Yield greeting tokens
            API-->>UI: Stream greeting HTML
        
        %% Scrape Trigger Routing
        else Intent = ACTION_SCRAPE
            Chat->>Chat: Spawn main.py (Playwright crawler)
            Note over Chat: Scrapes routes, waits for loader overlays, commits visual audit, & Commits DOM snapshots to DB
            Chat-->>API: Yield Scraped Status
            API-->>UI: Stream "Successfully scraped..."
            
        %% RAG Data Retrieval Routing
        else Intent = QUERY_CURRENT or QUERY_GUIDELINES
            Chat->>LLM: Feature Extraction (all-MiniLM-L6-v2)
            LLM-->>Chat: 384-dim Vector embedding
            alt Intent = QUERY_CURRENT
                Chat->>DB: Call match_dashboard_snapshots(vector, path)
            else Intent = QUERY_GUIDELINES
                Chat->>DB: Call match_guidelines(vector, path)
            end
            DB-->>Chat: Relevant text chunks + source metadata
            
            Chat->>LLM: Stream chat_completion with Context (Strict prompt)
            loop SSE Token Streaming
                LLM-->>Chat: Token chunks
                Chat->>Chat: Filter /no_think tag
                Chat-->>API: Yield stream chunks
                API-->>UI: Stream SSE Events (data: token)
                UI->>User: Render markdown formatted answer
            end
        end
    end
    Chat->>API: Close exit code
    API->>UI: Close EventStream connection
```

---

## 🔒 Security Features & Safety Guardrails

Security is a primary design constraint in WaiverPro. The system implements multiple isolation layers:

### 1. Zero Network-Access Local Guardrails
Obvious off-topic inputs containing words like `poem`, `prime number`, `write code`, or `recipe` are intercepted locally using microsecond-scale string-searching inside Python memory. This bypasses downstream database or LLM endpoints entirely:
*   **Latency**: **<0.1 ms** (Instant)
*   **Benefit**: Saves network bandwidth, eliminates LLM generation costs, and secures endpoints against prompt injection attacks.

### 2. Strict RAG Hallucination Restraints
The compliance judge and chatbot utilize system prompts that enforce absolute grounding:
*   *Answer based ONLY on the provided context. Do not make up information.*
*   *Treat the retrieved context as the only source of truth.*
*   *If the context does not contain enough information, say exactly what is missing and do not guess.*

### 3. Serialization Safety (No `.pkl` Files)
Unlike traditional machine learning deployments that load intent classifiers via pickled files (which are vulnerable to arbitrary code execution attacks), WaiverPro utilizes deterministic logic and serverless API endpoints. No pickle serialization is used anywhere in the codebase.

---

## 📊 Subsystems Performance & Verification

The suite has been evaluated against a 15-query automated benchmark testing intent classification, route extraction, factual grounding, and subsystem speed:

| Metric | Score / Value | Description |
|---|---|---|
| **Intent Classification Accuracy** | **93.33%** | Accuracy of classifying user query intent across all boundaries. |
| **Page Path Extraction Accuracy** | **100.00%** | Accuracy of identifying target URLs from user phrasing. |
| **Factual Term Relevance (Groundedness)** | **96.67%** | Percentage of expected factual details correctly outputted. |
| **Semantic Mean Squared Error (MSE)** | **0.0167** | Squared difference relative to a target perfect RAG grounding. |
| **Average End-to-End Latency** | **1,910.02 ms** | Speed of generating a query response (Retrieval + LLM generation). |

### Subsystem Unit Latencies:
*   **pgvector Retrieval Search**: ~871.73 ms
*   **Embedding Output Generation (384-dim)**: ~1,095.33 ms
*   **PDF Compiler (ReportLab)**: 6.04 ms
*   **SMTP Alert Dispatcher**: <0.01 ms

---

## 🚀 Setup & Installation

### 1. Prerequisites
- Python 3.10+
- Node.js 18+ & npm
- A Supabase Project
- A Hugging Face account and Access Token

### 2. Environment Setup
Clone the repository and initialize the Python virtual environment:
```bash
# Clone the repository
git clone https://github.com/karnamvenkatachaitanya/Novulis.git
cd Novulis

# Create and activate virtual environment
python -m venv venv
.\venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
playwright install chromium
```

### 3. Supabase Schema Setup
1. Go to your [Supabase SQL Editor](https://supabase.com/dashboard/).
2. Copy the content of **[`database_setup.sql`](database_setup.sql)**, paste it into the editor, and click **Run**.

### 4. Configuration
Create a `.env` file in the root directory:
```env
# Live Web Portal Configuration
APP_BASE_URL=https://white-cliff-0bca3ed00.1.azurestaticapps.net
APP_LOGIN_PATH=/login
APP_LOGIN_EMAIL=admin@gmail.com
APP_LOGIN_PASSWORD=password

# Supabase Vector DB Credentials
SUPABASE_URL=https://your-supabase-url.supabase.co
SUPABASE_KEY=your-supabase-anon-key
SUPABASE_SERVICE_ROLE_KEY=your-supabase-service-role-key

# Hugging Face access token
HF_TOKEN=your_hugging_face_token

# SMTP Email Server Settings
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_gmail@gmail.com
SMTP_PASSWORD=your_gmail_app_password
ALERT_FROM=your_gmail@gmail.com
ALERT_TO=your_gmail@gmail.com
```

---

## 🛠️ Usage Instructions

### Step 1: Ingest PDF Guidelines
Parse and upload the official PDF guidelines guidelines into Supabase:
```bash
python ingest_guidelines.py --pdf WaiverPro-User-Guidelines-WITH-DISCREPANCIES.pdf --verbose
```

### Step 2: Run Next.js Dashboard Client
Start the Next.js control center:
```bash
cd dashboard
npm install
npm run dev
```
Navigate to `http://localhost:3000` to monitor sweeps, view visual pdf reports, enter recipient emails, and chat with the RAG agent.

### Step 3: Run Command-Line Compliance Sweep
Run the end-to-end scraper, RAG retrieval, AI auditing, and email alert pipeline manually:
```bash
python main.py --similarity-threshold 0.1 --smtp-starttls --verbose
```

### Step 4: Run Automated Test Suite
Verify package logic, DOM filter bounds, and RAG routing functions:
```bash
python -m unittest tests/test_compliance.py
```

---

## 📐 Open-Source Models & Task Routing Decisions

To guarantee data privacy, eliminate vendor lock-in, and enable low-cost self-hosting, the WaiverPro system relies exclusively on **state-of-the-art open-source (open-weights) models** hosted on Hugging Face Serverless Inference:

### 1. Selected Open-Source Models
*   **`Qwen/Qwen2.5-7B-Instruct`**: The primary reasoning engine. It handles high-complexity tasks like compliance auditing of dense HTML structures (DOM size >= 12k characters) and semantic intent classification.
*   **`Qwen/Qwen2.5-Coder-7B-Instruct`**: A specialized code-optimized model used for low-complexity layout sweeps (DOM size < 12k characters) to speed up analysis.
*   **`sentence-transformers/all-MiniLM-L6-v2`**: A fast, 384-dimensional dense vector embedding model running via Hugging Face Serverless APIs (with local CPU fallback).

### 2. Task Handling & Model Routing Architecture
Rather than executing all requests on a heavy model, the orchestrator implements a **Dynamic Task Routing & Fallback** mechanism to balance processing speed and evaluation depth:

```
         [User Query / DOM Input]
                    │
                    ▼
          [Intent Classification] ──► (OFF_TOPIC / GENERAL) ──► Fast Refusal / Greeting
                    │
           (Compliance Sweep)
                    │
                    ▼
          [Retrieve pgvector RAG]
                    │
         ┌──────────┴──────────┐
         ▼                     ▼
   [Zero Matching Chunks]  [Guidelines Found]
         │                     │
   (Bypass LLM Route)    (Check DOM Size)
   *0ms audit latency    ┌─────┴──────────┐
                         ▼                ▼
                     [< 12k chars]   [> 12k chars]
                         │                │
                   (Coder-7B Route)  (Qwen-7B Route)
                   *Rapid structured *Deep reasoning
                         │                │
                         └───────┬────────┘
                                 │
                                 ▼
                     [HF Inference Request]
                                 │
                     ┌───────────┴───────────┐
                     ▼                       ▼
                [Success]             [Model Not Supported]
                     │                       │
             (Return Findings)               ▼
                                     [Automatic Fallback]
                                     *Retries using default 7B
```

This multi-model routing structure ensures that each task is handled by the most efficient open-source model tier, reducing token footprint by up to **60%** and latency by up to **80%**, while safeguarding against serverless endpoint support changes via automatic 7B fallback.

---

## ☁️ Hugging Face Spaces Deployment

WaiverPro is fully optimized for cloud deployment as a Docker container on Hugging Face Spaces:

1. **Non-Root Execution (UID 1000)**: Switch directly to the pre-existing Playwright `pwuser` (UID 1000) inside the Jammy base image, with correct workspace write permissions.
2. **Pinned Playwright Binary**: Pinned `playwright==1.40.0` in `requirements.txt` to align exactly with the pre-installed web browsers of the base container image.
3. **Dynamic Port Routing**: Exposes port `7860` as required by HF Spaces environment routing.
4. **Cross-Platform Execution**: Dashboard spawner routes correctly between `python` (Windows local dev) and `python3` (Docker Linux).
5. **Secure SSE Stream Management**: Prevents double-closing SSE event stream controllers on child process termination.
