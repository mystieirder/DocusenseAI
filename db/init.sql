-- Runs once on first Postgres container init (before the API creates tables).
-- Enables pgvector so the document_chunks.embedding column works. Its dimension
-- is set by the API from the active embedding backend (local=384, gemini=768),
-- so it isn't fixed here. Also used by the full-text config for sparse retrieval.

CREATE EXTENSION IF NOT EXISTS vector;

-- (Tables + indexes are created by the API on startup via SQLAlchemy:
--   * HNSW index on document_chunks.embedding  (dense / cosine)
--   * GIN  index on document_chunks.tsv         (sparse / full-text)
--  The API also runs CREATE EXTENSION IF NOT EXISTS vector defensively.)
