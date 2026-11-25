CREATE TABLE rock_snapshot (
	snapshot_id VARCHAR(128) NOT NULL,
	kind VARCHAR(16) NOT NULL,
	source_sandbox_id VARCHAR(128),
	source_worker_ip VARCHAR(128),
	format_version INTEGER DEFAULT 1 NOT NULL,
	oci_ref VARCHAR(1024),
	oci_digest VARCHAR(71),
	oci_platform VARCHAR(256),
	base_image_builder_version VARCHAR(256),
	base_image_build_key VARCHAR(71),
	product_metadata JSONB,
	resource_spec JSONB,
	spec JSONB,
	state VARCHAR(16) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	remote_ready_at TIMESTAMP WITH TIME ZONE,
	deleted_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (snapshot_id),
	CONSTRAINT rock_snapshot_kind_ck CHECK (kind IN ('BASE_IMAGE', 'FULL')),
	CONSTRAINT rock_snapshot_format_version_ck CHECK (format_version >= 1),
	CONSTRAINT rock_snapshot_state_ck CHECK (state IN ('CREATING', 'LOCAL_READY', 'REMOTE_READY', 'DELETED', 'FAILED')),
	CONSTRAINT rock_snapshot_source_ck CHECK ((kind = 'BASE_IMAGE' AND source_sandbox_id IS NULL) OR (kind = 'FULL' AND source_sandbox_id IS NOT NULL)),
	CONSTRAINT rock_snapshot_remote_ready_ck CHECK (state <> 'REMOTE_READY' OR (spec IS NOT NULL AND remote_ready_at IS NOT NULL)),
	CONSTRAINT rock_snapshot_deleted_ck CHECK (state <> 'DELETED' OR deleted_at IS NOT NULL),
	CONSTRAINT rock_snapshot_base_image_identity_ck CHECK ((oci_ref IS NULL AND oci_digest IS NULL AND oci_platform IS NULL AND base_image_builder_version IS NULL AND base_image_build_key IS NULL) OR (kind = 'BASE_IMAGE' AND oci_ref IS NOT NULL AND oci_digest IS NOT NULL AND oci_platform IS NOT NULL AND base_image_builder_version IS NOT NULL AND base_image_build_key IS NOT NULL))
);

CREATE UNIQUE INDEX rock_snapshot_base_image_build_key_uq ON rock_snapshot (base_image_build_key) WHERE kind = 'BASE_IMAGE' AND state <> 'DELETED' AND base_image_build_key IS NOT NULL AND product_metadata IS NULL;

CREATE INDEX rock_snapshot_state_created_ix ON rock_snapshot (state, created_at);
