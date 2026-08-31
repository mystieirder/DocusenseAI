---
title: DocuSense AI
emoji: 📄
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

<!-- ═══════════════════════════════════════════════════════════════════
  HUGGING FACE SPACES — deploy template
  ═══════════════════════════════════════════════════════════════════
  HF Spaces (Docker SDK) reads the YAML frontmatter above from the Space's
  root README.md. To deploy DocuSense as a Space:

  1. Create a new Space → SDK: "Docker" → "Blank".
  2. Push this whole repo to the Space's git remote. Replace the Space's
     auto-created README.md with THIS file's content (the frontmatter above
     is what makes app_port=7860 work — the root Dockerfile already binds it).
  3. Space → Settings → "Variables and secrets" → add (as SECRETS):
        SECRET_KEY            (python -c "import secrets;print(secrets.token_urlsafe(48))")
        DATABASE_URL          (Supabase, postgresql+psycopg://...?sslmode=require)
        GEMINI_API_KEY
        S3_ENDPOINT_URL  S3_PUBLIC_ENDPOINT_URL  S3_ACCESS_KEY  S3_SECRET_KEY  S3_BUCKET
     and (as VARIABLES): S3_REGION, plus anything you want to override.
     The image already defaults ENV=production, SERVE_FRONTEND=true,
     INGEST_MODE=thread, EMBED_BACKEND=gemini, S3_ADDRESSING_STYLE=path.
  4. The Space builds the Dockerfile and boots on port 7860. Open the Space
     URL — the DocuSense UI is served at "/".

  Why HF Spaces is a good free alternative: the free CPU Space has 16 GB RAM,
  so you *can* set EMBED_BACKEND=local instead of gemini (build with the local
  requirements). It does not sleep as aggressively as Render's free web service.
  Trade-off: Spaces are public by default (the app's own login still gates data).
═══════════════════════════════════════════════════════════════════════ -->

# DocuSense AI

SRS-compliant document review: hybrid RAG, grounded chat with page-anchored
citations, structured summaries, and annotations. See `DEPLOY.md` in the repo
for the full free-tier walkthrough (Supabase + Gemini).
