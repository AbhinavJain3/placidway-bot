# PlacidWay Website Knowledge Chatbot

A small, grounded chatbot that answers patient questions **only** from 7 PlacidWay pages (ITC Immunity Therapy
Center in Tijuana, its treatment packages, price list and videos, plus the alternative-medicine cost comparison
page). Every answer cites its source URL. When the answer is not on the pages, the bot says so and offers a free quote.

- **Live demo:** `<RENDER_URL>`
- **Test results:** [`docs/test_results.md`](docs/test_results.md) (the 8 required questions + 7 extra edge cases); UI screenshot in [`docs/screenshots/`](docs/screenshots/)
- **Write-up (improvements & limitations):** [`docs/WRITEUP.md`](docs/WRITEUP.md)

## Quick start (local)

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # fill in LLM credentials (see below)
python -m scripts.ingest      # optional: re-crawl the 7 pages (a snapshot is already committed in data/)
uvicorn app.main:app --reload # open http://localhost:8000
pytest                        # 19 offline tests (no network / LLM needed)
python -m scripts.run_eval    # runs the test questions against the real LLM -> docs/test_results.md
```

Docker: `docker build -t placid-bot . && docker run -p 8000:8000 --env-file .env placid-bot`

### Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `azure` (Azure OpenAI) or `openai` (api.openai.com). Same SDK; only env changes. |
| `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION` | Azure credentials (when `azure`) |
| `OPENAI_API_KEY` | OpenAI credentials (when `openai`) |
| `CHAT_MODEL`, `CHAT_REASONING_EFFORT` | Answer model / deployment. Set the effort (e.g. `low`) only for reasoning models. |
| `REWRITE_MODEL`, `REWRITE_REASONING_EFFORT` | Router / query-rewrite model |
| `ADMIN_TOKEN` | Protects `/api/admin/*` |
| `CRAWLER_CONTACT` | Contact shown in the crawler's User-Agent |

No secrets live in the code; `.env` is git-ignored.

## Architecture

```
            ┌───────────── ingestion (scripts/ingest.py or POST /api/admin/refresh) ─────────────┐
 7 URLs ──► │ fetcher: robots.txt check · UA "PlacidWayKBBot/1.0 (+contact)" · 1 req/s · ETag cache  │
            │ extractor: drop site chrome · tables → "Header: value" rows · JSON-LD Product prices  │
            │ chunker: heading-aware sections (~350 tokens) prefixed with [Page] + [Section]        │
            │ embed: bge-small-en-v1.5 (local ONNX) → data/chunks.json + embeddings.npy (atomic)    │
            └───────────────────────────────────────────────────────────────────────────────────────┘

 question ─► 1. Router LLM (JSON): language · intent · standalone English search query (resolves follow-ups)
             2. Hybrid retrieval: BM25 + dense, Reciprocal Rank Fusion over {rewritten query, user's words}, top 10
             3. Answer LLM (JSON): answer only from numbered passages, cite [n], status answered/partial/not_found/declined
             4. Guardrails (deterministic):
                · every price-like figure must appear in the cited passages (or the user's own message) →
                  one corrective retry → otherwise a safe "I don't know + free quote" fallback
                · a factual answer with no citations is rejected
                · citations become URLs server-side (the model never writes links); [n] renumbered per page
             5. Response + side effects: lead form on booking intent · medical disclaimer once per session ·
                unanswered/partial questions logged to SQLite (content gaps) · chat log
```

| Path | What it does |
|---|---|
| `app/sources.py` | The 7 allowed URLs, the single source of truth (nothing else is ever fetched) |
| `app/ingest/fetcher.py` | Polite crawler: robots.txt, User-Agent, rate limit, retries, conditional GET, falls back to last good copy |
| `app/ingest/extractor.py` | HTML → clean sections; tables flattened into self-describing rows; schema.org prices → facts |
| `app/ingest/chunker.py` | Sections → chunks with page/section headers; merges tiny sections, splits long ones with overlap |
| `app/ingest/pipeline.py` | Orchestrates ingestion, writes a snapshot atomically, reports new/changed pages |
| `app/retrieval/index.py` | In-memory hybrid index (BM25 + embeddings + RRF), hot-swappable on refresh |
| `app/llm/client.py` | Azure/OpenAI client factory, JSON mode, reasoning-model support |
| `app/llm/prompts.py` | Router and answer prompts, all behaviour rules in one place |
| `app/chat/pipeline.py` | Orchestration: route → retrieve → answer → verify → respond |
| `app/chat/guardrails.py` | Number grounding (handles `18,995` / `18.995` / Arabic-Indic digits), citation repair |
| `app/chat/session.py` | Per-session history (last 6 turns) and disclaimer-shown flag |
| `app/storage.py` | SQLite: unanswered questions, leads, chat log (also logged to stdout) |
| `app/main.py` | FastAPI routes; `app/static/index.html` is the dependency-free chat UI |

### API

| Method & path | Description |
|---|---|
| `GET /` | Chat UI |
| `POST /api/chat` `{session_id, message}` | `{answer, sources[{title,url}], status, intent, language, show_lead_form, offer_quote}` |
| `POST /api/lead` `{session_id, name, email, treatment?, message?}` | Stores a quote request (email validated) |
| `GET /health` | Liveness + knowledge-base build time |
| `GET /api/admin/sources` | Public: which pages are indexed, when, and their content hashes |
| `POST /api/admin/refresh` (header `X-Admin-Token`) | Re-crawls the 7 pages, rebuilds and hot-swaps the index, and reports which pages changed |
| `GET /api/admin/unanswered` / `GET /api/admin/leads` (header `X-Admin-Token`) | Content-gap log and captured leads |

### Refreshing content
- **Manual:** `python -m scripts.ingest` locally, or `curl -X POST $URL/api/admin/refresh -H "X-Admin-Token: …"` on the live bot (no restart needed).
- **Scheduled:** `.github/workflows/refresh.yml` calls the refresh endpoint every Monday (set the `BOT_URL` and `ADMIN_TOKEN` repo secrets).
- A failed crawl never empties the bot: the fetcher falls back to the last good copy, and the snapshot is only swapped after a successful build.

## Tools, models and why

| Choice | Why |
|---|---|
| **Python + FastAPI**, vanilla HTML/JS UI | Small and easy to read; every step of ingestion, grounding and fallback is visible code, not a hosted black box |
| **requests-style crawler (httpx) + BeautifulSoup/lxml** | Pages are server-rendered (prices are in the HTML and JSON-LD), so no headless browser is needed |
| **Hybrid retrieval: BM25 (`rank_bm25`) + `BAAI/bge-small-en-v1.5` via `fastembed`** | BM25 catches exact names and prices; embeddings catch paraphrases. The embedding model is local ONNX (~130 MB, CPU), so there are no extra API costs and it fits a 512 MB free host. 128 chunks fit in memory, so there's no vector DB |
| **Answer model: `gpt-5.6-luna` (OpenAI in production, Azure during development; reasoning effort `low`)** | In testing it kept figures attached to the right scope (e.g. didn't present a worldwide range as "the price in Mexico") better than gpt-4o, at similar latency (~2–5 s) |
| **Router: `gpt-4o`** | Fast, reliable JSON for intent, language and follow-up rewriting |
| **Azure OpenAI ↔ OpenAI via one SDK** | The provider is an env switch (`LLM_PROVIDER`), so deployment can use whichever key is available |
| **SQLite** | Zero-ops logging of content gaps and leads; rows are also printed to logs |
| **Render (Docker, free tier)** | Public URL from a GitHub repo; health check on `/health`; the image bakes in the embedding model and KB snapshot so cold starts don't download or crawl anything |

## Deploying to Render
1. Push this repo to GitHub.
2. In Render, choose **New → Blueprint**, pick the repo (`render.yaml` is detected), and fill in the secret env vars (`OPENAI_API_KEY` or the Azure vars, plus `CRAWLER_CONTACT`). `ADMIN_TOKEN` is generated automatically.
3. Open the service URL. Optionally add an UptimeRobot monitor on `/health` (every 10 min) so the free instance doesn't sleep between reviewers' visits.
