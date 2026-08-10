ALTER TABLE sandbox_record
ADD COLUMN IF NOT EXISTS labels JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_sandbox_record_labels_gin
ON sandbox_record
USING GIN (labels jsonb_path_ops);
