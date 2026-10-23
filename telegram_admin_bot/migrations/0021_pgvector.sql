-- pgvector: vector columns, distances and indexes in Postgres itself.
--
-- The compose file runs the pgvector build of the same Postgres 16
-- (pgvector/pgvector:*-pg16-trixie, same Debian as postgres:16, so the data
-- directory and collations carry over). Nothing uses it yet: reply examples
-- keep their embeddings as REAL[] (0018) and compare them in the worker; a
-- column moves over with ALTER ... TYPE vector(384) USING embedding::vector(384)
-- once there are enough of them to need an index.
--
-- Skipped where the server has no pgvector (a plain postgres:16, a test
-- database), so this migration never blocks a deploy.

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
    CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
  ELSE
    RAISE NOTICE 'pgvector is not available on this server; the vector extension was not created';
  END IF;
END
$$;
