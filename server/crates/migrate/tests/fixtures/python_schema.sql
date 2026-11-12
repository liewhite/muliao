CREATE TABLE agent_history (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	agent_id VARCHAR(32) NOT NULL, 
	day DATE NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	summary TEXT NOT NULL, 
	refs JSON NOT NULL, 
	importance SMALLINT NOT NULL, 
	created_at DATETIME NOT NULL
);
CREATE VIRTUAL TABLE agent_history_fts USING fts5(title, summary, refs, tokenize='trigram');
CREATE TABLE "agents" (
	id VARCHAR(32) NOT NULL, 
	name VARCHAR(64) NOT NULL, 
	deleting_by VARCHAR(64), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, is_default BOOLEAN DEFAULT 0 NOT NULL, archived_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);
CREATE TABLE alembic_version (
	version_num VARCHAR(32) NOT NULL, 
	CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);
CREATE TABLE approval_requests (
	id INTEGER NOT NULL, 
	session_id VARCHAR(36) NOT NULL, 
	call_id VARCHAR(100) NOT NULL, 
	created_at DATETIME NOT NULL, 
	taken_at DATETIME, pin VARCHAR(100), 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE
);
CREATE TABLE compactions (
	id INTEGER NOT NULL, 
	session_id VARCHAR(36) NOT NULL, 
	through_message_id INTEGER NOT NULL, 
	at_message_id INTEGER NOT NULL, 
	summary TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE
);
CREATE TABLE files (
	id VARCHAR(36) NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	media_type VARCHAR(255) NOT NULL, 
	size INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE "issue_activities" (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	issue_id INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	actor VARCHAR(32) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	body TEXT, 
	change JSON, session_id VARCHAR(36), 
	FOREIGN KEY(issue_id) REFERENCES issues (id) ON DELETE CASCADE
);
CREATE TABLE issue_dependencies (
	issue_id INTEGER NOT NULL, 
	depends_on_id INTEGER NOT NULL, 
	PRIMARY KEY (issue_id, depends_on_id), 
	FOREIGN KEY(depends_on_id) REFERENCES issues (id) ON DELETE CASCADE, 
	FOREIGN KEY(issue_id) REFERENCES issues (id) ON DELETE CASCADE
);
CREATE TABLE "issues" (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	title VARCHAR(300) NOT NULL, 
	description TEXT NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	priority VARCHAR(16) NOT NULL, 
	creator VARCHAR(32) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	owner_agent_id VARCHAR(32), 
	assignee VARCHAR(32)
);
CREATE TABLE jobs (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	session_id VARCHAR(36) NOT NULL, 
	agent_id VARCHAR(32) NOT NULL, 
	command TEXT NOT NULL, 
	secrets JSON NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	exit_code INTEGER, 
	log_path VARCHAR(255) NOT NULL, 
	pid INTEGER, 
	started_at DATETIME NOT NULL, 
	finished_at DATETIME
, check_seconds DOUBLE DEFAULT '60' NOT NULL);
CREATE TABLE "mail" (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	session_id VARCHAR(36) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	source VARCHAR(100), 
	content TEXT NOT NULL, 
	attachments JSON NOT NULL, 
	answers JSON NOT NULL, 
	awaited BOOLEAN NOT NULL, 
	state VARCHAR(16) NOT NULL, 
	taken_by INTEGER, 
	created_at DATETIME NOT NULL, 
	check_in BOOLEAN DEFAULT '0' NOT NULL, 
	origin VARCHAR(16), 
	delegation_id INTEGER, 
	CONSTRAINT fk_mail_delegation_id FOREIGN KEY(delegation_id) REFERENCES thread_calls (id) ON DELETE SET NULL, 
	FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE, 
	FOREIGN KEY(taken_by) REFERENCES tasks (id) ON DELETE CASCADE
);
CREATE TABLE message_texts (
	message_id INTEGER NOT NULL, 
	text TEXT NOT NULL, 
	PRIMARY KEY (message_id), 
	FOREIGN KEY(message_id) REFERENCES messages (id) ON DELETE CASCADE
);
CREATE TABLE messages (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	session_id VARCHAR(36) NOT NULL, 
	task_id INTEGER NOT NULL, 
	payload TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE
);
CREATE TABLE model_usage (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	created_at DATETIME NOT NULL, 
	agent_id VARCHAR(32) NOT NULL, 
	session_id VARCHAR(36), 
	kind VARCHAR(16) NOT NULL, 
	model VARCHAR(200) NOT NULL, 
	requests INTEGER NOT NULL, 
	input_tokens INTEGER NOT NULL, 
	output_tokens INTEGER NOT NULL, 
	cache_read_tokens INTEGER NOT NULL, 
	cache_write_tokens INTEGER NOT NULL
);
CREATE TABLE notifications (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	title TEXT NOT NULL, 
	body TEXT NOT NULL, 
	session_id VARCHAR(36), 
	unread BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL
, issue_id INTEGER);
CREATE TABLE push_subscriptions (
	endpoint TEXT NOT NULL, 
	p256dh TEXT NOT NULL, 
	auth TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (endpoint)
);
CREATE TABLE script_grants (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	agent_id VARCHAR(32) NOT NULL, 
	skill VARCHAR(100) NOT NULL, 
	script TEXT NOT NULL, 
	args JSON NOT NULL, 
	secrets JSON NOT NULL, 
	fingerprint VARCHAR(64) NOT NULL, 
	"key" VARCHAR(64) NOT NULL, 
	session_id VARCHAR(36) NOT NULL, 
	call_id VARCHAR(100) NOT NULL, 
	created_at DATETIME NOT NULL, 
	hits INTEGER NOT NULL, 
	last_used_at DATETIME, 
	UNIQUE ("key")
);
CREATE TABLE "session_holds" (
	id INTEGER NOT NULL, 
	session_id VARCHAR(36) NOT NULL, 
	holder VARCHAR(200), 
	call_id VARCHAR(100), 
	awaited BOOLEAN NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	created_at DATETIME NOT NULL, 
	ended_at DATETIME, 
	thread_call_id INTEGER, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_session_holds_thread_call_id FOREIGN KEY(thread_call_id) REFERENCES thread_calls (id) ON DELETE SET NULL, 
	FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE
);
CREATE TABLE "sessions" (
	id VARCHAR(36) NOT NULL, 
	agent_id VARCHAR(32) NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	origin VARCHAR(16) NOT NULL, 
	origin_ref VARCHAR(64), 
	parent_session_id VARCHAR(36), 
	thread VARCHAR(100), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_sessions_parent_session_id FOREIGN KEY(parent_session_id) REFERENCES sessions (id) ON DELETE SET NULL
);
CREATE TABLE tasks (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	kind VARCHAR(16) NOT NULL, 
	lane VARCHAR(100) NOT NULL, 
	agent_id VARCHAR(32), 
	session_id VARCHAR(36), 
	payload JSON NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	attempts INTEGER NOT NULL, 
	error TEXT, 
	not_before DATETIME NOT NULL, 
	created_at DATETIME NOT NULL, 
	started_at DATETIME, 
	finished_at DATETIME
);
CREATE TABLE thread_calls (
	id INTEGER NOT NULL, 
	parent_session_id VARCHAR(36) NOT NULL, 
	call_id VARCHAR(100) NOT NULL, 
	thread_session_id VARCHAR(36) NOT NULL, check_seconds DOUBLE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(parent_session_id) REFERENCES sessions (id) ON DELETE CASCADE, 
	CONSTRAINT fk_thread_calls_thread_session_id FOREIGN KEY(thread_session_id) REFERENCES sessions (id) ON DELETE CASCADE
);
CREATE TABLE "triggers" (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	name VARCHAR(64) NOT NULL, 
	agent_id VARCHAR(32) NOT NULL, 
	prompt TEXT NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	config JSON NOT NULL, 
	state JSON NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	last_fired_at DATETIME, 
	precheck JSON, 
	last_checked_at DATETIME, 
	precheck_output TEXT, 
	disabled_reason TEXT, 
	created_at DATETIME NOT NULL, 
	finished_at DATETIME, 
	faults JSON DEFAULT '[]' NOT NULL, 
	last_session_id VARCHAR(36)
);
CREATE TABLE wakeups (
	id INTEGER NOT NULL, 
	holder VARCHAR(200) NOT NULL, 
	session_id VARCHAR(36) NOT NULL, 
	due_at DATETIME NOT NULL, 
	note TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE
);
CREATE TRIGGER agent_history_no_delete BEFORE DELETE ON agent_history BEGIN SELECT RAISE(ABORT, 'agent_history is append-only'); END;
CREATE TRIGGER agent_history_no_update BEFORE UPDATE ON agent_history BEGIN SELECT RAISE(ABORT, 'agent_history is append-only'); END;
CREATE INDEX ix_agent_history_agent_day ON agent_history (agent_id, day);
CREATE INDEX ix_compactions_session_id ON compactions (session_id);
CREATE INDEX ix_issue_activities_issue_id ON issue_activities (issue_id);
CREATE INDEX ix_issue_activities_session_id ON issue_activities (session_id);
CREATE INDEX ix_issue_dependencies_depends_on_id ON issue_dependencies (depends_on_id);
CREATE INDEX ix_issues_assignee ON issues (assignee);
CREATE INDEX ix_issues_owner_agent_id ON issues (owner_agent_id);
CREATE INDEX ix_issues_status ON issues (status);
CREATE INDEX ix_jobs_agent_id ON jobs (agent_id);
CREATE INDEX ix_jobs_session_id ON jobs (session_id);
CREATE INDEX ix_jobs_status ON jobs (status);
CREATE INDEX ix_mail_created_at ON mail (created_at);
CREATE INDEX ix_mail_delegation_id ON mail (delegation_id) WHERE delegation_id IS NOT NULL;
CREATE INDEX ix_mail_queued ON mail (session_id) WHERE state = 'queued';
CREATE INDEX ix_mail_session_id ON mail (session_id);
CREATE INDEX ix_mail_taken_by ON mail (taken_by) WHERE taken_by IS NOT NULL;
CREATE INDEX ix_messages_created_at ON messages (created_at);
CREATE INDEX ix_messages_session_id ON messages (session_id);
CREATE INDEX ix_model_usage_created_at ON model_usage (created_at);
CREATE INDEX ix_model_usage_session_id ON model_usage (session_id);
CREATE INDEX ix_notifications_issue_id ON notifications (issue_id);
CREATE INDEX ix_notifications_session_id ON notifications (session_id);
CREATE INDEX ix_notifications_unread ON notifications (unread);
CREATE INDEX ix_script_grants_agent_id ON script_grants (agent_id);
CREATE INDEX ix_session_holds_session_id ON session_holds (session_id);
CREATE INDEX ix_sessions_agent_id ON sessions (agent_id);
CREATE INDEX ix_sessions_origin_ref ON sessions (origin, origin_ref);
CREATE INDEX ix_sessions_parent_session_id ON sessions (parent_session_id);
CREATE INDEX ix_sessions_updated_at ON sessions (updated_at);
CREATE INDEX ix_tasks_agent_id ON tasks (agent_id);
CREATE INDEX ix_tasks_session_id ON tasks (session_id);
CREATE INDEX ix_tasks_status_lane ON tasks (status, lane);
CREATE INDEX ix_thread_calls_thread_session_id ON thread_calls (thread_session_id);
CREATE INDEX ix_triggers_agent_id ON triggers (agent_id);
CREATE INDEX ix_wakeups_session_id ON wakeups (session_id);
CREATE UNIQUE INDEX uq_agents_default ON agents (is_default) WHERE is_default;
CREATE UNIQUE INDEX uq_approval_requests_call ON approval_requests (session_id, call_id);
CREATE UNIQUE INDEX uq_session_holds_holder ON session_holds (holder);
CREATE UNIQUE INDEX uq_session_holds_thread_call_id ON session_holds (thread_call_id) WHERE thread_call_id IS NOT NULL;
CREATE UNIQUE INDEX uq_sessions_thread ON sessions (parent_session_id, agent_id, thread) WHERE parent_session_id IS NOT NULL;
CREATE UNIQUE INDEX uq_thread_calls_call ON thread_calls (parent_session_id, call_id);
CREATE UNIQUE INDEX uq_triggers_name_unfinished ON triggers (name) WHERE finished_at IS NULL;
CREATE UNIQUE INDEX uq_wakeups_holder ON wakeups (holder);
