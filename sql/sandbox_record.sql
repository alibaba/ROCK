CREATE TABLE sandbox_record (
	sandbox_id VARCHAR(128) NOT NULL, 
	user_id VARCHAR(128) NOT NULL, 
	image VARCHAR(512) NOT NULL, 
	experiment_id VARCHAR(128) NOT NULL, 
	namespace VARCHAR(128) NOT NULL, 
	cluster_name VARCHAR(128) NOT NULL, 
	state VARCHAR(32) NOT NULL, 
	host_ip VARCHAR(128) NOT NULL, 
	create_time VARCHAR(64) NOT NULL, 
	start_time VARCHAR(64), 
	stop_time VARCHAR(64), 
	host_name VARCHAR(255), 
	auth_token VARCHAR(512), 
	rock_authorization_encrypted VARCHAR(1024), 
	cpus FLOAT, 
	memory VARCHAR(64), 
	create_user_gray_flag BOOLEAN, 
	phases JSONB, 
	port_mapping JSONB, 
	spec JSONB, 
	status JSONB, 
	PRIMARY KEY (sandbox_id)
);

CREATE INDEX ix_sandbox_record_image ON sandbox_record (image);

CREATE INDEX ix_sandbox_record_host_ip ON sandbox_record (host_ip);

CREATE INDEX ix_sandbox_record_host_name ON sandbox_record (host_name);

CREATE INDEX ix_sandbox_record_state ON sandbox_record (state);

CREATE INDEX ix_sandbox_record_create_user_gray_flag ON sandbox_record (create_user_gray_flag);

CREATE INDEX ix_sandbox_record_user_id ON sandbox_record (user_id);

CREATE INDEX ix_sandbox_record_namespace ON sandbox_record (namespace);

CREATE INDEX ix_sandbox_record_experiment_id ON sandbox_record (experiment_id);

CREATE INDEX ix_sandbox_record_cluster_name ON sandbox_record (cluster_name);
