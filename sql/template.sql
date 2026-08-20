CREATE TABLE template (
	template_id VARCHAR(128) NOT NULL,
	image VARCHAR(512),
	os_type VARCHAR(32) NOT NULL,
	cpu_count INTEGER NOT NULL,
	memory_mb INTEGER NOT NULL,
	disk_size_mb INTEGER NOT NULL,
	spec JSONB,
	status VARCHAR(32) NOT NULL,
	current_step VARCHAR(32),
	artifact_uri VARCHAR(1024),
	fiber_pool_id VARCHAR(128),
	execution_context JSONB,
	error_code VARCHAR(128),
	error_message TEXT,
	created_at TIMESTAMPTZ NOT NULL,
	updated_at TIMESTAMPTZ NOT NULL,
	PRIMARY KEY (template_id)
);

CREATE INDEX ix_template_status ON template (status);
CREATE UNIQUE INDEX ux_template_image ON template (image);
