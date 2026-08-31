# DocuSense AI

A production-grade **document review system**: upload a document, then chat with it, summarize it, and annotate it — every answer **grounded in the source** and clickable back to the exact page and region it came from.

This is the full, SRS-compliant build: FastAPI + PostgreSQL/pgvector + Redis/Celery + MinIO (S3) + JWT/OAuth2, with a single-file frontend that reuses the DocuSense "brass & espresso" design.

> **Want to host it for free?** The same codebase deploys as a **single web service** on Render or Hugging Face Spaces — backed by Supabase (Postgres + Storage) and Gemini embeddings — with **no credit card**. See **[DEPLOY.md](DEPLOY.md)**.

The heavy pieces are **pluggable by config**, so it scales from a laptop to a free cloud box to a full self-host without code changes:

- **Embeddings** — `EMBED_BACKEND = local` (sentence-transformers/torch) · `gemini` (API, no torch) · `keyword` (sparse only)
- **Ingestion** — `INGEST_MODE = celery` (worker) · `thread` (in-process) · `auto` (use the worker if a broker is reachable, else a thread)
- **Frontend** — `SERVE_FRONTEND` serves the UI same-origin from the API, or let nginx serve it
- **Storage** — one S3 abstraction for MinIO, Supabase Storage, Cloudflare R2, or AWS S3

---

## Contents

- [What it does (SRS traceability)](#what-it-does-srs-traceability)
- [Architecture](#architecture)
- [Tech stack](#tech-stack)
- [Quick start (Docker)](#quick-start-docker)
- [Deploy free (Render / Hugging Face + Supabase)](#deploy-free)
- [Configuration](#configuration-environment-variables)
- [API reference](#api-reference)
- [Project structure](#project-structure)
- [How retrieval works](#how-retrieval-works)
- [Running without Docker](#running-without-docker-dev)
- [Security notes](#security-notes)
- [Troubleshooting](#troubleshooting)

---

## What it does (SRS traceability)

| SRS req | Capability | Where it lives |
| --- | --- | --- |
| **FR-01** | Upload PDF / DOCX / TXT / PNG / JPG; validated (extension, size, magic-bytes, PDF page cap); **async ingestion** (extract → OCR fallback → chunk → embed → index) | `routers/documents.py`, `tasks.py`, `rag.py` |
| **FR-02** | In-app viewer: page nav, thumbnails, zoom 25–500%, rotation, selectable text layer, full-text search (TXT) | `frontend/index.html` |
| **FR-03** | **Grounded chat** with streaming (SSE) answers and clickable **page + bounding-box citations**; persisted history; anti-hallucination fallback | `routers/chat.py`, `rag.py` |
| **FR-04** | **Summary**: executive prose + key points + **risk / deadline / action tables**; cached; export to **Markdown / PDF** + copy-to-clipboard | `routers/summary.py` |
| **FR-05** | **Annotations**: highlight-to-act pills (Explain / Summarize / Identify risks / Ask), a Highlight Inspector, save/jump/delete | `routers/annotations.py`, `frontend/index.html` |
| Security | JWT access + refresh, OAuth2 password flow, bcrypt hashing, password-strength rules, **per-user document isolation**, **admin-gated routes** (RBAC), security headers, production secret guard | `security.py`, `deps.py`, `routers/auth.py`, `routers/admin.py`, `main.py` |
| Storage | S3/MinIO/Supabase/R2 object store, presigned download URLs, optional **AES-256** at rest | `storage.py` |

> **Multi-user isolation:** every document, chunk, chat message and annotation is scoped to `user_id`. Two users never see each other's files — this is the point of keeping login even though the file "is right there."

> **Restart behavior:** on login/refresh the app lists your documents as tabs but **does not auto-open the last one** — you always land on the welcome screen. (Requested behavior: "I want the PDF to be closed when the server is restarted.")

---

## Architecture

```
                    ┌───────────────────────────┐
   browser  ───────▶│  frontend (nginx :5500)   │  static single-file UI
                    └────────────┬──────────────┘
                                 │  fetch + SSE  (CORS)
                    ┌────────────▼──────────────┐
                    │     API  (FastAPI :8000)   │  auth, docs, chat, summary, annotations
                    └───┬────────┬────────┬──────┘
          enqueue job   │        │        │  read/write
        ┌───────────────▼─┐  ┌───▼────┐  ┌▼─────────────┐
        │ Redis (broker)  │  │ MinIO  │  │  PostgreSQL   │
        └───────┬─────────┘  │  (S3)  │  │  + pgvector   │
                │            └───┬────┘  └───────────────┘
        ┌───────▼─────────┐     │ bytes         ▲
        │ Celery worker   │─────┘               │ chunks + embeddings + tsvector
        │ extract/OCR/    │─────────────────────┘
        │ chunk/embed     │
        └─────────────────┘
```

**Ingestion flow (async).** Upload returns immediately with `status="processing"`; the API stores the bytes in MinIO and enqueues a Celery job. The worker extracts text (PyMuPDF / python-docx / OCR fallback for scanned pages), chunks it with page + bounding-box metadata, embeds each chunk locally (`all-MiniLM-L6-v2`, 384-dim), and writes rows to `document_chunks`. The frontend polls `GET /api/documents/{id}` until `ready` or `failed`.

**Query flow.** Chat runs **hybrid retrieval** (dense pgvector cosine + sparse Postgres full-text, fused with Reciprocal Rank Fusion), builds a grounded prompt, and streams the LLM answer token-by-token over SSE. Citations are derived from the retrieved chunks and narrowed to the pages the answer actually references.

---

## Tech stack

- **API:** FastAPI, Uvicorn, Pydantic v2
- **DB:** PostgreSQL 16 + `pgvector` (HNSW cosine index) + `tsvector` (GIN full-text index)
- **Queue:** Celery + Redis, **or** an in-process background thread (`INGEST_MODE`)
- **Storage:** MinIO / Supabase Storage / Cloudflare R2 / AWS S3 via boto3
- **Auth:** OAuth2 password flow, JWT (python-jose), bcrypt (passlib), RBAC admin routes
- **RAG:** pluggable embeddings (local sentence-transformers **or** Gemini API), hybrid dense+sparse retrieval, RRF fusion
- **LLM:** Gemini (`generativelanguage` API) — streaming, JSON, and embedding modes
- **Docs/OCR:** PyMuPDF, python-docx, Pillow, pytesseract; reportlab for PDF export
- **Frontend:** single `index.html` (pdf.js viewer, SSE chat), served by nginx **or** by the API same-origin

---

## Quick start (Docker)

**Prerequisites:** Docker + Docker Compose, and a **Gemini API key** (for chat/summary/explain).

```bash
cd docusense_srs

# 1) create your env file
cp .env.example .env

# 2) set two things in .env:
#    - GEMINI_API_KEY=your-key
#    - SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))")

# 3) build + run everything
docker compose up --build
```

Then open:

| URL | What |
| --- | --- |
| **http://localhost:5500** | the DocuSense app |
| http://localhost:8000/docs | interactive API docs (Swagger) |
| http://localhost:9001 | MinIO console (`docusense` / `docusense-secret`) |

First boot downloads the embedding model (~90 MB) into a cached volume; subsequent starts are fast. Register an account in the UI, then upload a document.

> **Heads-up:** the first upload triggers the worker to load the embedding model, so the very first document may take ~20–40s to reach `ready`. The tab shows a pulsing status dot until it's done.

---

<a name="deploy-free"></a>

## Deploy free (Render / Hugging Face + Supabase)

You can run the whole system in the cloud for **$0, no credit card**. The trick is that the heavy dependencies are optional: with `EMBED_BACKEND=gemini` there's no torch (fits a 512 MB box), with `INGEST_MODE=thread` there's no Celery/Redis, and with `SERVE_FRONTEND=true` the API serves the UI itself — so the "6 containers" collapse into **one web service** plus managed Postgres + Storage (Supabase) and the Gemini API.

```
              ┌──────────────────────────────────────┐
  browser ───▶│  ONE web service (Render / HF Spaces) │  API + frontend, same-origin
              │  FastAPI · in-thread ingest · Gemini  │
              └───────────┬───────────────┬───────────┘
                          │               │
                 ┌────────▼───────┐  ┌────▼─────────────┐
                 │ Supabase       │  │ Supabase Storage │
                 │ Postgres+vector│  │  (S3 gateway)    │
                 └────────────────┘  └──────────────────┘
                          │ embeddings + chat
                 ┌────────▼───────┐
                 │  Gemini API    │
                 └────────────────┘
```

The repo includes everything needed:

| File | Purpose |
| --- | --- |
| `Dockerfile` (repo root) | single-service cloud image (slim, no torch; copies frontend; binds `$PORT`/7860) |
| `render.yaml` | Render Blueprint — one free web service, health check, env placeholders, auto-generated `SECRET_KEY` |
| `.env.render.example` | production env template with inline instructions |
| `deploy/hf-space-README.md` | Hugging Face Spaces frontmatter + steps |
| `.dockerignore` | keeps the build context lean and secret-free |

**Follow the step-by-step runbook in [DEPLOY.md](DEPLOY.md)** (Supabase project → pgvector → connection string → bucket → S3 keys → Gemini key → deploy → verify), including honest free-tier caveats (cold starts, Supabase pausing, Gemini rate limits).

---

## Configuration (environment variables)

All config is read from `.env` (see `.env.example`). The important ones:

| Variable | Default | Notes |
| --- | --- | --- |
| `ENV` | `development` | `production`/`staging` enable the secret guard + HSTS |
| `DEBUG` | `true` | set `false` in production |
| `SERVE_FRONTEND` | `false` | `true` → API serves the UI at `/` same-origin (single service) |
| `INGEST_MODE` | `auto` | `celery` (worker) · `thread` (in-process) · `auto` |
| `EMBED_BACKEND` | `local` | `local` (torch) · `gemini` (API) · `keyword` (sparse only) |
| `SECRET_KEY` | `change-me…` | **Change this.** Signs JWTs. Prod refuses to boot on the default. |
| `ADMIN_EMAILS` | `[]` | JSON array of emails granted the `admin` role |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | access-token lifetime |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | refresh-token lifetime |
| `DATABASE_URL` | `postgresql+psycopg://docusense:docusense@postgres:5432/docusense` | Postgres DSN |
| `REDIS_URL` / `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | `redis://redis:6379/{0,1,2}` | Redis + Celery (only when `INGEST_MODE` uses Celery) |
| `S3_ENDPOINT_URL` | `http://minio:9000` | internal S3 endpoint (server-side) |
| `S3_PUBLIC_ENDPOINT_URL` | `http://localhost:9000` | browser-reachable host for presigned URLs |
| `S3_BUCKET` | `docusense-documents` | object bucket |
| `S3_ADDRESSING_STYLE` | `auto` | `path` for Supabase/R2; `auto` for MinIO/AWS |
| `S3_USE_SSE` | `false` | request AES-256 at rest (MinIO/AWS honor it; Supabase/R2 reject it) |
| `GEMINI_API_KEY` | *(empty)* | **required** for chat/summary (and embeddings when `EMBED_BACKEND=gemini`) |
| `GEMINI_MODEL` | `gemini-3.6-flash` | chat/summary model id (IDs rotate — verify via `GET /v1beta/models`, see DEPLOY.md) |
| `EMBED_MODEL` / `EMBED_DIM` | `all-MiniLM-L6-v2` / `384` | local embeddings (dim must match the DB vector column) |
| `GEMINI_EMBED_MODEL` / `GEMINI_EMBED_DIM` | `gemini-embedding-001` / `768` | Gemini embeddings (used when `EMBED_BACKEND=gemini`) |
| `RETRIEVAL_TOP_K` | `8` | passages per query |
| `HYBRID_ALPHA` | `0.5` | dense↔sparse weight (0 = sparse only, 1 = dense only) |
| `MAX_UPLOAD_MB` / `MAX_PAGES` | `50` / `300` | ingestion limits |
| `OCR_PAGE_LIMIT` | `60` | max pages to OCR on scanned docs |
| `CORS_ORIGINS` | `["http://localhost:5500","http://127.0.0.1:5500"]` | frontend origin(s); empty ⇒ CORS disabled |

> **The embedding dimension is fixed at table-creation time.** The DB vector column uses `GEMINI_EMBED_DIM` when `EMBED_BACKEND=gemini`, else `EMBED_DIM` — so choose the backend **before** the first boot. Switching backends later means re-creating `document_chunks` (or a fresh database).

**Using real AWS S3 instead of MinIO:** set `S3_ENDPOINT_URL=""` and `S3_PUBLIC_ENDPOINT_URL=""`, then provide real `S3_ACCESS_KEY` / `S3_SECRET_KEY` / `S3_REGION` / `S3_BUCKET`.

---

## API reference

All routes except `register` / `login` / `refresh` require `Authorization: Bearer <access_token>`.

### Auth
| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| POST | `/api/auth/register` | `{email, name, password}` | `UserOut` (201) |
| POST | `/api/auth/login` | **form**: `username` (=email), `password` | `{access_token, refresh_token, user}` |
| POST | `/api/auth/refresh` | `{refresh_token}` | new token pair |
| GET | `/api/auth/me` | — | `UserOut` |

### Documents
| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/documents` | multipart `file`; returns `DocumentOut` with `status="processing"` |
| GET | `/api/documents` | list your documents |
| GET | `/api/documents/{id}` | one document (poll `status`) |
| GET | `/api/documents/{id}/content` | raw bytes for the in-app viewer (CORS-friendly) |
| GET | `/api/documents/{id}/file` | short-lived presigned S3 URL (direct download) |
| DELETE | `/api/documents/{id}` | delete document + cascade |

### Chat (FR-03)
| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/chat/stream` | `{doc_id, query, history}` → **SSE**: `event: token` deltas, then `event: done` = `{content, citations}`, or `event: error` |
| POST | `/api/chat` | non-streaming equivalent |
| GET | `/api/chat/{doc_id}/history` | persisted messages with citations |

`citations` = `[{ "page": N, "boxes": [[x0,y0,x1,y1], …] }]`, coordinates normalized 0–1.

### Summary (FR-04)
| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/summary/{id}` | cached summary (generated on first call) |
| POST | `/api/summary/{id}/regenerate` | force a fresh build |
| GET | `/api/summary/{id}/export?format=md\|pdf` | download Markdown or PDF |

### Annotations (FR-05)
| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/annotations` | create a highlight/note |
| GET | `/api/annotations/{doc_id}` | list (Highlight Inspector) |
| PATCH | `/api/annotations/{id}` | edit notes / tags |
| DELETE | `/api/annotations/{id}` | remove one |
| POST | `/api/annotations/selection` | run a pill action `{explain\|summarize\|risks\|custom}` on selected text; optionally save |

### Admin (RBAC — requires `admin` role)
| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/admin/stats` | instance-wide counts (users, documents by status, chunks, messages, annotations) |
| GET | `/api/admin/users` | list all accounts |

> A normal user's token gets `403` here. Grant the role via `ADMIN_EMAILS` (applied on register, reconciled on login).

---

## Project structure

```
docusense_srs/
├─ docker-compose.yml        # postgres, redis, minio, api, worker, frontend (self-host)
├─ Dockerfile                # single-service cloud image (Render / HF Spaces)
├─ render.yaml               # Render Blueprint (free web service)
├─ .dockerignore
├─ .env.example              # copy → .env  (docker-compose)
├─ .env.render.example       # production env template (cloud)
├─ DEPLOY.md                 # free-tier deployment runbook
├─ deploy/
│  └─ hf-space-README.md     # Hugging Face Spaces frontmatter + steps
├─ db/
│  └─ init.sql               # CREATE EXTENSION vector (first-boot)
├─ backend/
│  ├─ Dockerfile             # api + worker image; torch optional via INSTALL_LOCAL_EMBED
│  ├─ requirements.txt       # base deps (slim, no torch)
│  ├─ requirements-local.txt # + sentence-transformers/torch (local embedding backend)
│  └─ app/
│     ├─ main.py             # FastAPI app, lifespan, CORS, security headers, secret guard
│     ├─ config.py           # pydantic-settings (all env vars + derived helpers)
│     ├─ database.py         # engine, SessionLocal, Base, init_db
│     ├─ models.py           # users, documents, document_chunks, chat_messages, annotations
│     ├─ schemas.py          # Pydantic request/response contract
│     ├─ security.py         # bcrypt + JWT + password rules
│     ├─ deps.py             # get_current_user + require_role (RBAC)
│     ├─ storage.py          # S3/MinIO/Supabase/R2 (upload/download/presign/delete)
│     ├─ rag.py              # extract → OCR → chunk → embed → hybrid retrieve → citations
│     ├─ llm.py              # Gemini provider (generate / generate_json / stream / embed)
│     ├─ celery_app.py       # Celery config (optional)
│     ├─ tasks.py            # ingest dispatcher: Celery task OR in-process thread
│     └─ routers/
│        ├─ auth.py  documents.py  chat.py  summary.py  annotations.py  admin.py
└─ frontend/
   └─ index.html             # single-file UI (pdf.js viewer + SSE chat + pills + inspector)
```

---

## How retrieval works

1. **Extraction** — PyMuPDF reads text as blocks with normalized bounding boxes; low-text pages fall back to Tesseract OCR (rendered at 150 dpi, capped by `OCR_PAGE_LIMIT`). DOCX and TXT have their own extractors; images go straight to OCR.
2. **Chunking** — ~1000 chars with 150 overlap, never spanning a page, carrying `page_num` + union bbox.
3. **Embedding** — each chunk → 384-dim L2-normalized vector (`all-MiniLM-L6-v2`), stored in a `pgvector` column with an **HNSW cosine** index.
4. **Sparse index** — a generated `tsvector` column (`to_tsvector('english', content)`) with a **GIN** index.
5. **Hybrid retrieval** — dense (cosine) and sparse (`ts_rank`) result lists are fused with **Reciprocal Rank Fusion** (`k=60`), weighted by `HYBRID_ALPHA`. Rank-based fusion avoids mixing incompatible score scales.
6. **Citations** — retrieved chunks are grouped by page into clickable citations; the viewer draws the bounding boxes over the page canvas.

---

## Running without Docker (dev)

You still need Postgres (with pgvector), Redis, and an S3/MinIO endpoint reachable. Then:

```bash
cd backend
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# point .env's hostnames at localhost (postgres→localhost, redis→localhost, minio→localhost)
export $(grep -v '^#' ../.env | xargs)   # or use your shell's env loader

# API
uvicorn app.main:app --reload --port 8000

# worker (separate terminal, same venv + env)
celery -A app.celery_app.celery worker --loglevel=info

# frontend: serve the folder on port 5500 so it matches CORS
cd ../frontend && python -m http.server 5500
```

> On Windows PowerShell, activate the venv with `.venv\Scripts\Activate.ps1` and install with `python -m pip install -r requirements.txt` (avoid pasting an absolute `/c:/…/python.exe` path — use the activated `python`).

> **Lightweight local run (no Redis, no torch):** set `INGEST_MODE=thread`, `EMBED_BACKEND=gemini`, and `SERVE_FRONTEND=true`, then just run the API — no Celery worker, no nginx, no model download. You still need Postgres+pgvector and an S3 endpoint (or point at Supabase and skip local infra entirely, as in [DEPLOY.md](DEPLOY.md)).

---

## Security notes

- **JWT**: short-lived access token + longer refresh token, each carrying a `type` claim; the API rejects a refresh token used as an access token and vice-versa.
- **Passwords**: bcrypt-hashed; strength enforced (≥ 8 chars, letter + number + symbol).
- **Isolation**: every query filters by `user_id`; ownership is checked on every document/annotation route (404, not 403, to avoid leaking existence).
- **RBAC**: `/api/admin/*` is gated by `require_role("admin")`; grant the role via `ADMIN_EMAILS`.
- **Production secret guard**: with `ENV=production`, the app **refuses to boot** on the default `SECRET_KEY`, and warns if `DEBUG=true`.
- **Security headers**: `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` on every response, plus `Strict-Transport-Security` in production. (No CSP by default — the UI loads pdf.js/jsPDF from a CDN; add one if you self-host those assets.)
- **AES-256 at rest is optional/best-effort.** MinIO/AWS honor `ServerSideEncryption=AES256`; Supabase/R2 reject it, so `S3_USE_SSE` defaults off and `upload_bytes` retries without SSE if refused.
- **CORS** is locked to `CORS_ORIGINS`; serving the UI same-origin (`SERVE_FRONTEND=true`) makes CORS unnecessary (leave it empty).
- Change `SECRET_KEY` and the default MinIO/Postgres credentials before any non-local deployment.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Chat/summary return 503 | `GEMINI_API_KEY` is missing or invalid in `.env`. |
| Document stuck on `processing` | Check the **worker** logs (`docker compose logs -f worker`). First run downloads the embedding model. |
| Document `failed` | The tab tooltip / viewer shows the error (e.g. encrypted PDF, no extractable text). |
| Login works but calls 401 shortly after | Access token expired; the frontend auto-refreshes. If refresh also fails, sign in again. |
| Viewer blank for a PDF | Confirm `GET /api/documents/{id}/content` returns 200; check CORS origin matches `:5500`. |
| `pgvector` / `vector` type errors | Ensure the DB image is `pgvector/pgvector:pg16` (the compose file uses it) and `db/init.sql` ran. |
| MinIO "bucket" errors on first upload | `storage.ensure_bucket` retries and recreates on `NoSuchBucket`; verify MinIO is up on `:9000`. |

---

*Built to the DocuSense AI SRS. Frontend design (brass & espresso palette, split-pane workspace) carried over from the original DocuSense UI.*
