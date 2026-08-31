# Deploying DocuSense AI for free

This is a step-by-step runbook for hosting the full DocuSense AI in production **without paying anything and without a credit card**. The whole system runs as **one small web service** backed by **Supabase** (Postgres + object storage) and **Google's Gemini API** (chat + embeddings).

You have two good free hosts; both are covered below.

| | **Render** (recommended) | **Hugging Face Spaces** |
| --- | --- | --- |
| Cost | Free, no card | Free, no card |
| RAM | 512 MB | 16 GB |
| Sleeps when idle | Yes (~15 min → 30–60 s cold start) | Less aggressively |
| Embeddings | Gemini API (no torch — fits 512 MB) | Gemini API, **or** local model (16 GB fits it) |
| Privacy | Service is private to you | Space is public by default (app login still gates data) |
| Custom domain | Yes | Space subdomain |

Both deploy the **same repository** and the **same root `Dockerfile`**. The differences are just environment variables.

---

## How the free build differs from the self-host build

The codebase supports two shapes from the same source. Nothing is forked — it is all switched by environment variables:

| Concern | Self-host (docker-compose) | Free cloud (this runbook) |
| --- | --- | --- |
| Services | 6 containers (api, worker, postgres, redis, minio, nginx) | **1 web service** |
| Frontend | nginx serves it | API serves it same-origin (`SERVE_FRONTEND=true`) |
| Ingestion | Celery worker + Redis | In-process background thread (`INGEST_MODE=thread`) |
| Embeddings | Local `all-MiniLM-L6-v2` (torch) | Gemini API (`EMBED_BACKEND=gemini`, no torch) |
| Database | Postgres container | Supabase Postgres |
| Object store | MinIO container | Supabase Storage (S3 gateway) |

The root `Dockerfile` bakes the free-tier defaults in, so a bare deploy already runs in the right mode; you only supply secrets and connection strings.

---

## Before you start

You need three free accounts. No cards.

1. **GitHub** — to hold the code the host builds from.
2. **Supabase** — <https://supabase.com> (database + file storage).
3. **Google AI Studio** — <https://aistudio.google.com/app/apikey> for a **Gemini API key** (used for both chat *and* embeddings). If you already have one, reuse it.

Push this `docusense_srs/` folder to a GitHub repo before continuing:

```bash
cd docusense_srs
git init && git add . && git commit -m "DocuSense AI"
git branch -M main
git remote add origin https://github.com/<you>/docusense.git
git push -u origin main
```

> **Never commit real secrets.** `.env`, `.env.render`, and real keys are git-ignored. Only the `*.example` templates are committed.

---

## Part A — Supabase (database + storage)

### A1. Create the project
1. Sign in to Supabase → **New project**.
2. Name it, pick a **region near you**, and set a **database password** (save it — it goes in the connection string).
3. Wait ~2 minutes for provisioning.

### A2. Enable pgvector
DocuSense tries to run `CREATE EXTENSION IF NOT EXISTS vector` automatically on first boot. To be safe, enable it yourself:

- **Dashboard → Database → Extensions → search `vector` → enable**, *or*
- **SQL Editor** → run:
  ```sql
  create extension if not exists vector;
  ```

### A3. Get the database connection string (IMPORTANT)
Render's free tier connects over **IPv4**, but Supabase's *direct* connection is IPv6-only. **Use the pooler string**, or the app won't connect.

1. Click **Connect** (top of the dashboard).
2. Choose **Session pooler** (not "Direct connection", not "Transaction").
3. Copy the URI. It looks like:
   ```
   postgresql://postgres.abcdefgh:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres
   ```
4. Make **two edits** for DocuSense:
   - change the scheme `postgresql://` → **`postgresql+psycopg://`**
   - append **`?sslmode=require`** at the end
   - replace `[YOUR-PASSWORD]` with your actual DB password

   Final `DATABASE_URL`:
   ```
   postgresql+psycopg://postgres.abcdefgh:YOUR-PASSWORD@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require
   ```

### A4. Create the storage bucket
1. **Dashboard → Storage → New bucket** → name it `docusense-documents` → keep it **Private** → create.
   (DocuSense signs short-lived URLs, so the bucket must stay private.)

### A5. Get S3 access keys
Supabase Storage speaks the S3 protocol.
1. **Dashboard → Storage → Settings** (or **Project Settings → Storage**) → **S3 Connection**.
2. Note the **Endpoint**, which looks like:
   ```
   https://abcdefgh.supabase.co/storage/v1/s3
   ```
   and the **Region** (e.g. `us-east-1`) — it must match your project's region.
3. Click **New access key** → copy the **access key id** and **secret**. You'll only see the secret once.

You now have everything from Supabase:

| Env var | Value |
| --- | --- |
| `DATABASE_URL` | the edited pooler string from A3 |
| `S3_ENDPOINT_URL` | `https://<ref>.supabase.co/storage/v1/s3` |
| `S3_PUBLIC_ENDPOINT_URL` | same as `S3_ENDPOINT_URL` |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | from A5 |
| `S3_BUCKET` | `docusense-documents` |
| `S3_REGION` | your project region |

---

## Part B — Gemini API key

1. Open <https://aistudio.google.com/app/apikey> → **Create API key**.
2. Copy it → this is `GEMINI_API_KEY`.

The free tier covers both the chat model and the embedding model (`gemini-embedding-001`) DocuSense uses. Rate limits apply (see caveats).

> **If chat fails with a model error, confirm the model exists.** Gemini model IDs rotate over time. DocuSense defaults `GEMINI_MODEL` to `gemini-3.6-flash`; if chat/summary return 503 with a model error, list what your key can actually use and set `GEMINI_MODEL` to one of them:
> ```bash
> curl "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY" | grep '"name"'
> ```
> Pick any `models/gemini-*-flash` (fast + free-tier friendly). Embeddings are unaffected — they always use `gemini-embedding-001`.

---

## Part C — Deploy to Render (recommended)

### C1. Create the service from the blueprint
The repo ships a `render.yaml` blueprint, so Render configures itself.

1. Sign in to <https://render.com> → **New +** → **Blueprint**.
2. Connect your GitHub and pick the `docusense` repo.
3. Render reads `render.yaml`, shows a **web service on the Free plan**, and lists the variables it needs. Click **Apply**.

### C2. Fill in the secrets
Render already sets the mode variables (`ENV=production`, `SERVE_FRONTEND=true`, `INGEST_MODE=thread`, `EMBED_BACKEND=gemini`, …) and **auto-generates `SECRET_KEY`**. You supply the ones marked "set in dashboard":

```
DATABASE_URL            = (Part A3)
GEMINI_API_KEY          = (Part B)
S3_ENDPOINT_URL         = https://<ref>.supabase.co/storage/v1/s3
S3_PUBLIC_ENDPOINT_URL  = https://<ref>.supabase.co/storage/v1/s3
S3_ACCESS_KEY           = (Part A5)
S3_SECRET_KEY           = (Part A5)
S3_BUCKET               = docusense-documents
```

Optional — make yourself an admin (enables `/api/admin/*`): add `ADMIN_EMAILS` with value `["you@example.com"]`.

### C3. Deploy & watch the logs
Render builds the Docker image and boots it. In **Logs** you should see:

```
Starting DocuSense AI (env=production, embed_backend=gemini, ingest_mode=thread)
Database initialized
Object storage ready (bucket=docusense-documents)
Serving frontend at / from /app/frontend
Uvicorn running on http://0.0.0.0:10000
```

When the health check at `/health` passes, open the service URL — the DocuSense UI loads. Jump to **[Verify](#verify-it-works)**.

> If you prefer not to use the blueprint, create a **Web Service → Docker** manually, point it at the repo, set Health Check Path to `/health`, and add **all** the env vars from `.env.render.example`.

---

## Part D — Deploy to Hugging Face Spaces (alternative)

1. <https://huggingface.co/spaces> → **Create new Space** → **SDK: Docker** → **Blank** → create.
2. Push the repo to the Space's git remote (`https://huggingface.co/spaces/<you>/<space>`). Replace the Space's `README.md` with `deploy/hf-space-README.md` from this repo — its frontmatter (`sdk: docker`, `app_port: 7860`) is what makes the port wiring work.
3. **Space → Settings → Variables and secrets.** Add the same keys as Render (C2) — put connection strings and keys under **Secrets**, plain values under **Variables**. The image already defaults the mode variables, so at minimum add `DATABASE_URL`, `GEMINI_API_KEY`, the five `S3_*` values, `S3_BUCKET`, and a `SECRET_KEY` (generate one — see below).
4. The Space builds and serves on port 7860. Open the Space URL.

Because a free Space has 16 GB RAM, you *may* run local embeddings instead of Gemini: set `EMBED_BACKEND=local` and build with the local requirements (uncomment/adjust per `requirements-local.txt`). Gemini is still simpler and lighter.

Generate a `SECRET_KEY`:
```bash
python -c "import secrets;print(secrets.token_urlsafe(48))"
```

---

## Verify it works

1. **Health** — visit `/health`; expect `{"status":"ok",...}`.
2. **API docs** — visit `/docs` (Swagger).
3. **Register** — in the UI, create an account. (If you set `ADMIN_EMAILS`, register with that email to get the admin role.)
4. **Upload** — add a small PDF. The tab shows a pulsing dot; within a few seconds it flips to **ready**. (Ingestion runs in a background thread — no worker needed.)
5. **Chat** — ask a question. The answer streams in and shows **page + highlight citations**. Click one to jump to the region.
6. **Summary / Annotate** — open the summary tab; export to PDF; highlight text and run an "Explain" pill.

If all six pass, you're deployed.

---

## Free-tier caveats (read these)

These are real limits of *free* infrastructure, not bugs:

- **Cold starts (Render).** A free web service **sleeps after ~15 min idle**. The next request wakes it in ~30–60 s. Fine for personal/demo use; not for always-on SLAs.
- **Supabase pausing.** Free projects **pause after ~7 days of inactivity**. Un-pause from the dashboard; data is retained.
- **Gemini rate limits.** The free tier has per-minute/day request caps. Heavy bulk ingestion (embeddings) or rapid chatting can hit `429` — DocuSense surfaces a clear "rate limit / quota" message and keeps working once the window resets. Chunks still store without vectors on embedding failure, so **full-text search keeps working** even if embeddings are throttled.
- **Memory (Render 512 MB).** This is exactly why the free build uses the **Gemini** embedding backend — the local torch model would exceed 512 MB. Don't switch `EMBED_BACKEND=local` on Render.
- **Ephemeral disk.** The service has no persistent local disk — that's fine, because all state lives in Supabase (Postgres + Storage). Don't rely on local files.
- **Cold model / first request.** First chat after a wake may be a little slower while connections re-establish.
- **Storage & DB size.** Supabase free includes ~500 MB database and ~1 GB storage. Plenty for personal document sets; watch it if you ingest a lot.

---

## Making yourself an admin

RBAC is enforced: `/api/admin/stats` and `/api/admin/users` require the `admin` role, and a normal token gets `403`.

Set `ADMIN_EMAILS` to a JSON array of emails, e.g. `["you@example.com"]`. The role is applied when that email **registers**, and reconciled on **every login** — so you can promote an existing account by adding the env var and logging in again. Remove the email to demote.

---

## Cloud troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Boot fails: `Refusing to start … default SECRET_KEY` | Set a real `SECRET_KEY` (Render's blueprint auto-generates it; HF needs you to add one). |
| `could not translate host name` / connection timeouts to Postgres | You used the **direct** (IPv6) Supabase string. Switch to the **Session pooler** string (Part A3). |
| `password authentication failed` | Wrong DB password in `DATABASE_URL`, or you left the `[YOUR-PASSWORD]` placeholder. |
| DB errors mentioning `type "vector" does not exist` | Enable the `vector` extension in Supabase (Part A2), then redeploy. |
| Uploads fail with `NoSuchBucket` / `AccessDenied` on create | Create the bucket in the Supabase dashboard (Part A4); managed stores don't allow bucket creation over S3. Confirm `S3_ADDRESSING_STYLE=path`. |
| Uploads fail mentioning SSE / encryption | Ensure `S3_USE_SSE=false` (Supabase rejects AES-256 headers). It's the default in the cloud image. |
| Chat/summary return 503 | `GEMINI_API_KEY` missing/invalid, **or** `GEMINI_MODEL` is not a model your key can access — list models (Part B) and set a valid `gemini-*-flash`. |
| Chat/embeddings intermittently error with "rate limit/quota" | Gemini free-tier throttling — wait for the window to reset. |
| Viewer blank / CORS errors | With `SERVE_FRONTEND=true` the UI is same-origin and needs no CORS. If you host the UI separately, set `CORS_ORIGINS` to its exact origin. |
| App sleeps / first hit slow | Expected on Render free (see caveats). |

---

*Free-tier deployment guide for DocuSense AI. For the multi-container self-host setup, see [README.md](README.md).*
