# DocuSense AI — Project Context (for AI assistants)

> **Read this first.** This is a single, self-contained briefing of the entire
> DocuSense codebase — its purpose, architecture, data model, APIs, the RAG
> pipeline, the LLM layer, deployment, configuration, and known
> gotchas. If you are an AI helping change this project, everything you need to
> orient yourself is here. Secrets are shown as **placeholder names only** —
> never ask for or print real keys.
>
> *Last verified against the source on 2026-08-28.*

---

## 1. What DocuSense is

DocuSense AI is a **document-review web application**. A user uploads a document
(PDF, DOCX, image, or text), the app ingests it into a searchable knowledge base,
and the user can then:

- **Chat with the document** — ask questions and get answers that are *grounded
  only in the document*, with inline `[Page N]` citations that highlight the
  exact regions on the page. The assistant is instructed to say
  *"I could not find that in this document."* rather than hallucinate.
- **Get an AI summary** — a structured digest: an executive summary plus tables
  of key points, risks (with severity), deadlines, and action items. Exportable
  as Markdown or PDF.
- **Annotate** — select text on a page and ask the AI to explain / summarize /
  identify risks, optionally saving the result as a persistent annotation with
  tags.

It is a **single-owner / small-team** product built to run on a **100% free
hosting tier** (no credit card). It implements an SRS (software requirements
spec) with functional-requirement tags used throughout the code: **FR-01**
(ingest), **FR-03** (retrieval/chat), **FR-04** (summary), **FR-05**
(annotations).

---

## 2. Tech stack

**Backend** — Python, **FastAPI** + **Uvicorn** (ASGI). Data access via
**SQLAlchemy 2.x**. Vector search via **pgvector**. Auth via **JWT / OAuth2
password flow** (`python-jose` for tokens, `passlib` + `bcrypt` for hashing).
Document parsing via **PyMuPDF (fitz)**, **python-docx**, **Pillow**,
**pytesseract**. HTTP to LLM providers via **httpx**. PDF export via
**reportlab**. Object storage via **boto3** (S3-compatible).

**Database** — **PostgreSQL** with the **`vector`** extension (pgvector) and
Postgres **full-text search** (`tsvector`/`ts_rank`).

**Object storage** — any **S3-compatible** store (AWS S3, MinIO, **Supabase
Storage**, Cloudflare R2) behind a boto3 abstraction.

**LLM / embeddings** — **Gemini** (Google Generative Language API — see §9)
covers chat, summaries, and vision OCR. Embeddings are separately configurable:
local (sentence-transformers), Gemini, or disabled ("keyword").

**Frontend** — a **single-file** app: `frontend/index.html` (~1200 lines of
HTML/CSS/vanilla JS). Loads **PDF.js 3.11.174** from a CDN for rendering — the
only external script; summary export is done **server-side** (§6), not in the
browser. No build step. Talks to the backend over `fetch` + Server-Sent Events
(SSE).

**Async ingestion** — optional **Celery + Redis**, but on free hosting it runs
in an in-process **background thread** (no broker needed). See `INGEST_MODE`.

**Deploy** — **Docker** on **Render** (free web service) + **Supabase** (free
Postgres + Storage). See §11.

---

## 3. Architecture at a glance

```
                         ┌──────────────────────────────────────────────┐
   Browser               │  FastAPI app (app.main)                      │
  ┌──────────┐  HTTPS    │  ┌────────┬──────────┬────────┬───────────┐  │
  │index.html│ ───────▶  │  │ auth   │ documents│  chat  │  summary  │  │
  │ PDF.js   │  fetch/   │  │ router │  router  │ router │  router   │  │
  └──────────┘  SSE      │  ├────────┴──────────┴────────┴───────────┤  │
        ▲                │  │ annotations router │ admin router      │  │
        │ static         │  └─────────┬──────────────────┬───────────┘  │
        └────────────────┤         rag.py             llm.py            │
     (SERVE_FRONTEND)    │     (extract/chunk/   (Gemini REST API:      │
                         │      embed/retrieve)   chat / stream /       │
                         │                         OCR / embed)         │
                         └──────┬──────────────┬───────────┬────────────┘
                                │              │           │
                         ┌──────▼─────┐  ┌─────▼─────┐ ┌───▼──────────┐
                         │ PostgreSQL │  │ S3 store  │ │ Gemini API   │
                         │ + pgvector │  │(Supabase) │ │ (Generative  │
                         │ + FTS      │  │           │ │  Language)   │
                         └────────────┘  └───────────┘ └──────────────┘
```

**Two core request flows:**

1. **Ingest (upload):** `POST /api/documents` validates the file → stores bytes
   in S3 → creates a `documents` row with `status="processing"` → kicks off
   ingestion (thread or Celery). Ingestion extracts text (OCR for scanned
   pages), chunks it, embeds the chunks, and bulk-inserts `document_chunks`,
   then flips `status` to `ready` (or `failed`). The client **polls**
   `GET /api/documents/{id}` until ready.

2. **Chat (query):** `POST /api/chat/stream` retrieves the most relevant chunks
   (hybrid dense + sparse), builds a grounded prompt, and **streams** the LLM
   answer back token-by-token over SSE, ending with a `done` event carrying
   page/bbox **citations**.

---

## 4. Repository layout / file map

```
docusense_srs/
├── CONTEXT.md                 ← this file
├── README.md                  project overview
├── DEPLOY.md                  step-by-step free-tier deploy guide
├── Dockerfile                 root image (app + frontend)
├── backend/Dockerfile         backend image
├── docker-compose.yml         local full stack (postgres, redis, minio, api, worker, nginx)
├── render.yaml                Render deploy blueprint (env, health check, autoDeploy)
├── .dockerignore
├── .env.example               local/dev env template (placeholders only)
├── .env.render.example        cloud env template for Render (placeholders only)
├── db/init.sql                bootstrap SQL (pgvector extension etc.)
├── deploy/hf-space-README.md  alt deploy notes (Hugging Face Space)
├── frontend/
│   └── index.html             entire single-file frontend (~1200 lines)
└── backend/
    ├── requirements.txt        prod deps (pins bcrypt==4.0.1 — see gotchas)
    ├── requirements-local.txt  adds sentence-transformers + torch (local embeddings)
    └── app/
        ├── __init__.py
        ├── main.py             FastAPI app: routers, CORS, security headers, /health, lifespan
        ├── config.py           ALL settings/env vars (pydantic-settings) — single source
        ├── database.py         SQLAlchemy engine/session, init_db()
        ├── deps.py             FastAPI dependencies (get_current_user, etc.)
        ├── security.py         password hashing + JWT encode/decode
        ├── models.py           SQLAlchemy ORM models (5 tables) + indexes
        ├── schemas.py          Pydantic request/response schemas (API contract)
        ├── llm.py              GEMINI LLM LAYER — chat / json / stream / OCR / embed
        ├── rag.py              RAG pipeline — extract, chunk, embed, hybrid retrieve, citations
        ├── storage.py          boto3 S3 abstraction (upload/download/presign/delete)
        ├── tasks.py            ingestion orchestration (thread / Celery), run_ingest()
        ├── celery_app.py       Celery app (only used when INGEST_MODE uses a broker)
        └── routers/
            ├── auth.py         register / login / refresh / me
            ├── documents.py    upload / list / get / file / content / delete
            ├── chat.py         SSE streaming chat + non-streaming chat + history
            ├── summary.py      get / regenerate / export (md|pdf) summary
            ├── annotations.py  CRUD annotations + selection actions
            └── admin.py        admin-only endpoints (RBAC)
```

**Where to look for what:** config/env → `config.py`. Data shape → `models.py` +
`schemas.py`. LLM calls → `llm.py`. Retrieval/ingest logic → `rag.py`. Any HTTP
endpoint → the matching file in `routers/`. Storage → `storage.py`. Startup /
wiring → `main.py`.

---

## 5. Data model (PostgreSQL)

Five tables (defined in `backend/app/models.py`). pgvector column dimension is
**derived from the active embedding backend** via `settings.effective_embed_dim`
(local=384, gemini=768) — see §9/§12 for the migration implication.

**`users`**
`id` (int PK) · `email` (unique) · `name` · `password_hash` · `role`
(`user`|`admin`) · `created_at`.

**`documents`**
`id` (**UUID string** PK) · `user_id` (FK→users) · `name` · `mime_type` ·
`storage_key` (S3 object key `"{user_id}/{doc_id}/{filename}"`) · `page_count` ·
`status` (`processing`|`ready`|`failed`) · `error` (nullable text) · `summary`
(**JSONB**, cached summary) · `uploaded_at`.

**`document_chunks`**
`id` (int PK) · `doc_id` (FK→documents) · `page_num` · `chunk_index` (global
order) · `bbox_json` (JSONB — normalized `[x0,y0,x1,y1]` union box for citation
highlighting) · `content` (text) · `tsv` (**Computed** `to_tsvector('english',
content)` — sparse search) · `embedding` (**`Vector(effective_embed_dim)`** —
dense search, nullable when embeddings disabled).
Indexes: **GIN** on `tsv`; **HNSW** on `embedding` with `vector_cosine_ops`.

**`chat_messages`**
`id` · `doc_id` · `user_id` · `role` (`user`|`assistant`) · `content` ·
`citations_json` (JSONB) · `created_at`.

**`annotations`**
`id` · `doc_id` · `user_id` · `page_num` · `rect_coords` (JSON normalized
rect(s)) · `selected_text` · `ai_notes` · `tags` (JSON list) · `action`
(`explain`|`summarize`|`risks`|`custom`|`manual`) · `created_at`.

Deleting a document cascades to its chunks, messages, and annotations.

---

## 6. API surface

All routes are under `/api/*`; the frontend is served at `/` when
`SERVE_FRONTEND=true`. Auth is **Bearer JWT** (except register/login/refresh).
`GET /health` is public (host health checks).

**Auth** (`routers/auth.py`, prefix `/api/auth`)
- `POST /register` — `{email, name, password}` → creates user.
  `validate_password_strength` requires **≥ 8 chars and at least one letter, one
  digit, and one special character**. Emails in `ADMIN_EMAILS` get `admin`.
- `POST /login` — **OAuth2 password flow**; the form field is `username` but
  holds the **email**. Returns `{access_token, refresh_token, user}`.
- `POST /refresh` — `{refresh_token}` → a **full `Token`**: new access *and*
  refresh token plus the user object (same payload as login, not just an
  access token).
- `GET /me` — current user profile.

**Documents** (`routers/documents.py`, prefix `/api/documents`)
- `POST ""` — multipart upload. Validates extension (`.pdf/.docx/.txt/.png/.jpg/
  .jpeg`), size ≤ `MAX_UPLOAD_MB`, **magic-byte sniff** (defends against spoofed
  types), PDF page cap ≤ `MAX_PAGES`. Stores to S3, returns doc with
  `status="processing"`, kicks off ingestion.
- `GET ""` — list caller's documents (newest first).
- `GET /{id}` — one document (poll this for `status`).
- `GET /{id}/file` — short-lived **presigned S3 URL** (direct download).
- `GET /{id}/content` — streams raw bytes **through the API** (same-origin) for
  the in-app viewer, avoiding CORS issues rendering PDFs/images.
- `DELETE /{id}` — delete doc (best-effort S3 delete; DB is source of truth).

**Chat** (`routers/chat.py`, prefix `/api/chat`)
- `POST /stream` — **SSE**. Events: `token` (delta), `notice` (free-tier
  auto-retry countdown), `done` (`{content, citations}`), `error`. Retrieves
  chunks → builds grounded prompt → streams. Auto-retries on rate-limit **only
  before any token has streamed** (bounded by `_MAX_QUOTA_RETRIES=2`).
- `POST ""` — non-streaming equivalent → full `{role, content, citations}`.
- `GET /{doc}/history` — persisted conversation for the split-pane.

**Summary** (`routers/summary.py`, prefix `/api/summary`)
- `GET /{doc}` — cached summary; generated on first request and cached to
  `documents.summary`. **Never caches an empty summary** (would trap the doc) —
  raises a retryable 502 instead.
- `POST /{doc}/regenerate` — force fresh generation (bypasses cache).
- `GET /{doc}/export?format=md|pdf` — download Markdown or a reportlab PDF.

**Annotations** (`routers/annotations.py`, prefix `/api/annotations`)
- `POST ""` — create annotation.
- `GET /{doc}` — list annotations for a doc.
- `PATCH /{id}` — update notes/tags/text.
- `DELETE /{id}` — delete.
- `POST /selection` — run an AI action on selected text
  (`explain`|`summarize`|`risks`|`custom`), optionally `save` it as an
  annotation.

**Admin** (`routers/admin.py`, prefix `/api/admin`) — every route gated by
`Depends(require_role("admin"))`; a non-admin gets **403**.
- `GET /stats` — instance-wide counts, including `documents_by_status`.
- `GET /users` — list all users.

---

## 7. Ingestion pipeline (FR-01)

Entry point `tasks.run_ingest(doc_id)` (idempotent): download bytes from S3 →
`rag.process_document(content, filename)` → insert chunks → set
`status="ready"` (or `failed` with `error`). Two failure modes are decided here
rather than in `rag.py`: a doc whose *extracted* `page_count` exceeds `MAX_PAGES`
fails as `too_many_pages` (the upload-time check in §6 is PDF-only, so this is
the only cap that catches DOCX/images), and a doc that yields **zero chunks**
fails with *"No extractable text was found in this document."*

`rag.process_document` = **extract → chunk → embed**:

1. **Extract** (`extract_pages`, dispatch on extension):
   - **PDF** (`_extract_pdf`, PyMuPDF): per page, read digital text + positioned
     blocks (normalized bboxes). A page with no text or **printable density <
     `LOW_TEXT_DENSITY` (0.20)** is treated as **scanned** and queued for OCR
     (if its **page index** < `OCR_PAGE_LIMIT`, default 60 — a *cutoff*, not a
     budget, so a 100-page PDF scanned only from page 70 gets no OCR at all).
     OCR runs in a **second batched pass** (see below).
   - **DOCX** (`_extract_docx`, python-docx): paragraphs + table rows.
   - **Image** (`_extract_image`): single-page OCR.
   - **TXT** (`_extract_txt`): decoded text.
2. **OCR** (`_ocr_pages`) — for scanned pages. Sends page images to
   **Gemini's multimodal model** in **batches of `OCR_BATCH_PAGES` (default 3)**
   per request to stay under free-tier request limits (a 26-page scan → ~9 calls,
   not 26). Multi-page replies are split on a `<<<PAGE-BREAK>>>` delimiter.
   Resilience: a malformed/failed batch retries page-by-page; a per-page Gemini
   failure degrades to local **Tesseract**, then to `""`; once a hard **quota**
   error is seen, a **sticky `gemini_dead` flag** switches the rest of the doc to
   Tesseract (no more doomed API calls). The Gemini path is taken when
   `OCR_BACKEND=gemini`, or `auto` (the default) with `GEMINI_API_KEY` set, and
   its batched calls go through `llm.ocr_images`. The single-image helpers
   `rag._ocr_image` / `_gemini_ocr` serve `_extract_image` only.
3. **Chunk** (`chunk_pages`): ~`CHUNK_CHARS` (1000) chars with `CHUNK_OVERLAP`
   (150), never spanning pages, carrying a **union bbox** per chunk so citations
   can be highlighted. Over-long single blocks are hard-split with overlap.
4. **Embed** (`embed_texts`): dense vectors via the configured `EMBED_BACKEND`
   (L2-normalized). Embedding failure degrades to **sparse-only** — chunks are
   still stored, retrieval still works.

---

## 8. Retrieval — hybrid RAG (FR-03)

`rag.retrieve(db, doc_id, query, top_k)` fuses two arms:

- **Dense arm:** embed the query → pgvector **cosine distance** over
  `document_chunks.embedding` (HNSW index), scoped to the doc, top `fetch`.
  Chunks whose `embedding` is NULL — stored during a degraded, sparse-only
  ingest — are filtered out, so they can only ever surface via the sparse arm.
- **Sparse arm:** Postgres **full-text** `plainto_tsquery` + `ts_rank` over the
  `tsv` column (GIN index).
- **Fusion:** **weighted Reciprocal Rank Fusion** (RRF, `k=60`). `HYBRID_ALPHA`
  (default 0.5) weights dense vs sparse (0 = sparse only, 1 = dense only). Top
  `RETRIEVAL_TOP_K` (8) chunks are returned in reading order.
- **Fallback:** if neither arm returns anything (e.g. `EMBED_BACKEND=keyword`
  with no FTS hits), the leading chunks are returned.

`build_citations` collapses retrieved chunks into per-page boxes
`[{page, boxes:[[x0,y0,x1,y1],…]}]`. `chat.py` then **narrows** citations to the
pages the answer actually referenced via `[Page N]` regex.

---

## 9. The LLM layer (`llm.py`) — IMPORTANT

Every model call in the app funnels through `llm.py`, which talks to the
**Google Generative Language REST API** and nothing else. No router ever calls a
model directly, and there is no vendor SDK — just **httpx**.

**Public API (stable — callers never change):**
`generate(messages/prompt, json_mode, temperature)`,
`generate_json(prompt)`, `stream(prompt) -> generator of str deltas`,
`ocr_images(list[bytes]) -> list[str]`, `ocr_image(bytes) -> str`,
`embed(texts, dim, task_type)`.
`LLMError(message, *, retry_after=None)` is the shared error type — the
`retry_after` attribute is what powers chat's auto-retry.

**Endpoints** (base `https://generativelanguage.googleapis.com/v1beta/models`):
`:generateContent` for chat, summaries, and OCR; `:streamGenerateContent?alt=sse`
for streaming; `:batchEmbedContents` for embeddings. The key is passed as a
`?key=` **query parameter** — so avoid logging raw request URLs.

**Models:** `GEMINI_MODEL` (`gemini-3.6-flash`) serves chat, summaries **and**
vision OCR — one multimodal model for all three. `GEMINI_EMBED_MODEL`
(`gemini-embedding-001`, 768-dim) serves embeddings, sent `EMBED_BATCH` (100)
texts per request.

**Free-tier behaviour.** A single `generate_content_free_tier_requests` bucket
(~**20 requests/minute**) is shared across chat + OCR + summaries — the source of
most rate-limit pain. A 429 carries a structured `RetryInfo.retryDelay`, read by
`_parse_retry_delay` (falling back to scraping `"retry in Ns"` from the message);
the wait is used verbatim **+1 s of slack, capped at 60 s**, falling back to
30 s if neither source parses.

**Empty-response hardening.** `generate()` **raises** `LLMError` rather than
returning `""` when a 200 comes back with no text part, surfacing `finishReason`
/ `blockReason` so a "thinking model burned the whole budget" result is
diagnosable instead of silently propagating (see §12). `generate_json()` retries
once in plain-text mode if JSON mode returns empty, and parses loosely via
`_loads_loose`, which strips ```` ```json ```` fences and, failing that, grabs the
outermost `{...}`. Multi-page OCR replies are split on the `<<<PAGE-BREAK>>>`
delimiter (`_OCR_PAGE_DELIM`).

**Embeddings backends** (`EMBED_BACKEND`): `local` (sentence-transformers
all-MiniLM-L6-v2, 384-dim, needs torch/~1.5 GB RAM), `gemini` (768-dim), or
`keyword` (**no dense vectors** — sparse full-text only, zero quota, zero DB
migration). `settings.effective_embed_dim` returns `GEMINI_EMBED_DIM` for the
`gemini` backend and `EMBED_DIM` otherwise. ⚠️ **The pgvector column dimension is
fixed per backend.** Switching between backends of different dims (e.g. local 384
→ gemini 768) requires **recreating `document_chunks.embedding` at the new
dimension and re-ingesting every document**. `keyword` avoids any migration.

> **If you add a second provider.** `llm.py` is single-provider today: there is
> no `LLM_PROVIDER` setting and no dispatch anywhere in the tree. The clean shape
> is a `_provider()` helper plus one guard at the top of `generate()`, `stream()`
> and `ocr_images()`, leaving the Gemini path below byte-identical — the public
> API above is what keeps `chat.py`, `summary.py`, `annotations.py` and `rag.py`
> untouched. A provider with a different embedding dimension also triggers the
> migration described above.

---

## 10. Configuration / environment variables

All config lives in `backend/app/config.py` (`pydantic-settings`; reads env or a
`.env` file). **Secrets are set in the host's environment only — never committed.**
Below, secret values are placeholder names.

**App / security**
`APP_NAME`, `ENV` (`development`|`production` — note `settings.is_production`
also treats `prod` and `staging` as production), `DEBUG`,
`SECRET_KEY` (**secret**; app refuses to boot in production with the default),
`ALGORITHM` (HS256), `ACCESS_TOKEN_EXPIRE_MINUTES` (30),
`REFRESH_TOKEN_EXPIRE_DAYS` (7), `ADMIN_EMAILS` (list granted admin role).

**Database**
`DATABASE_URL` (**secret**; Postgres + pgvector. On Supabase use the **Session
pooler** connection string — IPv4-reachable — with `?sslmode=require`).

**Ingestion / Celery**
`INGEST_MODE` (`auto`|`celery`|`thread`; **`thread`** on free hosts — daemon
thread, no broker), `REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`.

**Object storage (S3-compatible)**
`S3_ENDPOINT_URL`, `S3_PUBLIC_ENDPOINT_URL` (browser-reachable host for presigned
URLs), `S3_ACCESS_KEY` (**secret**), `S3_SECRET_KEY` (**secret**), `S3_BUCKET`,
`S3_REGION`, `S3_ADDRESSING_STYLE` (`auto`|`path`|`virtual`; **`path`** for
Supabase/R2), `S3_USE_SSE` (**false** for Supabase/R2), `PRESIGN_EXPIRE_SECONDS`.

**LLM (Gemini)**
`GEMINI_API_KEY` (**secret**), `GEMINI_MODEL` (`gemini-3.6-flash`),
`LLM_MAX_OUTPUT_TOKENS` (8192 — do not lower; see §12).

**Embeddings / retrieval**
`EMBED_BACKEND` (`local`|`gemini`|`keyword`; **the code default is `local`**,
but `render.yaml` and the root `Dockerfile` both set `gemini` — so the default
silently wants torch *and* the 384-dim column), `EMBED_MODEL`, `EMBED_DIM` (384),
`GEMINI_EMBED_MODEL` (`gemini-embedding-001`), `GEMINI_EMBED_DIM` (768),
`EMBED_BATCH` (100), `RETRIEVAL_TOP_K` (8), `HYBRID_ALPHA` (0.5).

**Ingestion limits / OCR**
`MAX_UPLOAD_MB` (50), `MAX_PAGES` (300), `OCR_PAGE_LIMIT` (60),
`LOW_TEXT_DENSITY` (0.20), `OCR_BACKEND` (`auto`|`gemini`|`tesseract`),
`OCR_BATCH_PAGES` (3), `CHUNK_CHARS` (1000), `CHUNK_OVERLAP` (150).

**Frontend / CORS**
`SERVE_FRONTEND` (serve `index.html` at `/` same-origin), `FRONTEND_DIR`,
`CORS_ORIGINS` (only needed for a split/separately-hosted UI).

---

## 11. Deployment (free tier: Render + Supabase)

**Live setup:**
- **Host:** **Render** free **web service**, **Docker** runtime, deploys from
  GitHub repo **`mystieirder/docusense`**, branch **`my-branch-name`**,
  `autoDeploy: true` (via `render.yaml`). Public URL on `*.onrender.com`. Free
  instance **sleeps after ~15 min idle** and has ~0.1 shared vCPU / 512 MB RAM.
- **Database + storage:** **Supabase** free — Postgres via the **Session pooler**
  string (the direct host is IPv6-only and Render can't reach it), `vector`
  extension enabled, Storage bucket `docusense-documents` via the S3 gateway.
- **LLM:** Gemini (`GEMINI_API_KEY`). `INGEST_MODE=thread` (no Redis/Celery).
  `SERVE_FRONTEND=true` (UI served same-origin by the API).

**`render.yaml`** declares: web/docker/free, `healthCheckPath: /health`,
`autoDeploy: true`, and env — `ENV=production`, `DEBUG=false`,
`SERVE_FRONTEND=true`, `INGEST_MODE=thread`, embedding + dim vars,
`SECRET_KEY` (generateValue), `S3_ADDRESSING_STYLE=path`, `S3_USE_SSE=false`,
`CORS_ORIGINS="[]"`; and `sync:false` secrets that must be filled in the Render
dashboard: `DATABASE_URL`, `GEMINI_API_KEY`, and the six `S3_*` entries (only
`S3_ACCESS_KEY`/`S3_SECRET_KEY` are true secrets — endpoint, bucket and region are
`sync:false` merely because they are per-project).

**Startup (`main.py` lifespan):** logs app name, `ENV`, `EMBED_BACKEND` and
`INGEST_MODE`; refuses to boot in
production with the default `SECRET_KEY`; runs `init_db()` (creates
extension/tables/indexes) and `storage.ensure_bucket()`. Adds baseline security
headers on every response (no CSP — the frontend uses CDN scripts). Mounts the
static frontend **last** so `/api/*` and `/health` win.

---

## 12. Known gotchas & hard-won lessons

- **`bcrypt` pin.** `requirements.txt` pins **`bcrypt==4.0.1`**. Without the cap,
  `passlib[bcrypt]` pulls bcrypt ≥ 4.1, whose stricter input handling breaks
  passlib 1.7.4's backend self-test → **all password hashing fails** (register
  500s with "password cannot be longer than 72 bytes"). Do not unpin.
- **Empty-summary caching.** A model can return a 200 with **no text** (e.g. a
  "thinking" model spending its whole token budget on reasoning). `summary.py`
  **never caches an empty summary** (it would trap the doc into re-serving blank
  forever) — it raises a retryable 502. To recover a doc already stuck on a
  cached blank: **Regenerate** (bypasses cache) or `UPDATE documents SET
  summary=NULL`.
- **Scanned PDFs.** A PDF with no text layer routes every page through OCR. On
  the tiny free instance, **local Tesseract** is slow/memory-heavy and can
  OOM-kill the ingest thread, leaving the doc stuck in `processing`. Mitigations
  in place: **Gemini vision OCR** is preferred when a key is set, **batched**
  (`OCR_BATCH_PAGES`) to survive rate limits, with a sticky fallback to
  Tesseract. The viewer only renders once `status=ready`. Diagnose a PDF with
  poppler: `pdffonts file.pdf` (empty font table ⇒ scanned).
- **Free-tier rate limits (Gemini).** The ~20 req/min bucket is **shared** across
  chat + OCR + summaries; a burst starves the next chat turn. Chat **auto-waits
  and resends** (honoring the server's retry delay, bounded, only before the
  first token) and shows a countdown in the UI. If it still errors after the
  countdown the bucket is under sustained pressure — wait a full minute.
- **Embedding dimension is fixed per backend.** Switching `EMBED_BACKEND` between
  different dims requires recreating the `document_chunks.embedding` column and
  re-ingesting. `EMBED_BACKEND=keyword` is the zero-migration escape hatch.
- **Supabase specifics.** Use the **Session pooler** DB URL (IPv4);
  `S3_ADDRESSING_STYLE=path` and `S3_USE_SSE=false` for Supabase Storage.
- **There is no `.gitignore` in this repo — add one before the next push.**
  `.dockerignore` only filters `.env`, `.env.*`, `*.md`, `.git/`, caches and
  venvs, so *any* other file holding credentials (scratch notes, extensionless
  files, exported connection strings) is both **committed to GitHub and baked
  into the image**. Credentials belong only in **Render → Environment**. If one
  ever does get committed, rotating the credential is mandatory — deleting the
  file does not remove it from history.

---

## 13. How to make changes (workflow)

**The app runs on Render, not locally.** To change the running app:

1. Edit code locally (VS Code) in the `docusense_srs` project.
2. Commit and **push to branch `my-branch-name`** of **`mystieirder/docusense`**.
3. Render **auto-redeploys** from that branch (watch the deploy log; health
   check hits `/health`).

**Config-only changes** (model id, limits, keys, `EMBED_BACKEND`,
`OCR_BACKEND`, `INGEST_MODE`) don't need a code push — set them in
**Render → Environment** and redeploy. Changing the *embedding* backend is the
one exception: if the dimension changes it also needs a DB migration + re-ingest
(§9).

**Guardrails:** never commit a real `.env` or print real secrets — the
`.env*.example` files are placeholders only. If a dimension-changing embedding
switch is requested, remember it needs a DB migration + re-ingest.

---

## 14. Quick orientation for an AI picking this up

- **Add / change an endpoint** → the relevant `routers/*.py`; update `schemas.py`
  for request/response shape; auth via `Depends(get_current_user)`.
- **Change how documents are parsed/chunked/retrieved** → `rag.py`.
- **Change the model, or add a second provider** → `llm.py` (keep the public API
  — `generate` / `generate_json` / `stream` / `ocr_images` / `ocr_image` /
  `embed` — stable so no caller changes; add settings in `config.py`; see the
  note at the end of §9).
- **Change a limit, model id, or toggle** → `config.py` (then set the env var in
  the host).
- **Change the UI** → `frontend/index.html` (single file; SSE for chat streaming;
  PDF.js for rendering + citation highlights).
- **Storage behavior** → `storage.py`. **Ingestion dispatch** → `tasks.py`.
- **Never** hardcode secrets; **never** unpin `bcrypt==4.0.1`; **never** cache an
  empty summary; remember embedding-dim migrations.