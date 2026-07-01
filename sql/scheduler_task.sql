CREATE TABLE scheduler_task (
	task_id VARCHAR(32) NOT NULL, 
	taskset_id VARCHAR(32), 
	task_type VARCHAR(64) NOT NULL, 
	target_workers JSONB, 
	creation_timestamp FLOAT NOT NULL, 
	phase VARCHAR(32) NOT NULL, 
	assigned_pod VARCHAR(128) NOT NULL, 
	start_time FLOAT, 
	completion_time FLOAT, 
	conditions JSONB, 
	status JSONB, 
	PRIMARY KEY (task_id)
);

CREATE INDEX ix_scheduler_task_task_type ON scheduler_task (task_type);

CREATE INDEX ix_scheduler_task_taskset_id ON scheduler_task (taskset_id);
