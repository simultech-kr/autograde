"""Durable, student-confirmed IDE requests. No arbitrary executable payloads."""

SCHEMA = """
CREATE TABLE classroom_commands (
    command_id TEXT PRIMARY KEY,
    course_key TEXT NOT NULL,
    student_id INTEGER NOT NULL REFERENCES platform_students(id),
    request_key TEXT NOT NULL,
    request_json TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('notice','open_assignment','sync_materials','check_environment','save_submit')),
    assignment_id TEXT,
    message TEXT NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','received','accepted','completed','declined','failed','expired')),
    client_session_id TEXT REFERENCES classroom_clients(client_session_id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    received_at TEXT,
    accepted_at TEXT,
    finished_at TEXT,
    result_code TEXT,
    result_json TEXT,
    last_response_json TEXT,
    UNIQUE(course_key,student_id,created_by,request_key)
);
CREATE INDEX idx_classroom_commands_queue ON classroom_commands(course_key,student_id,status,created_at);
CREATE INDEX idx_classroom_commands_expiry ON classroom_commands(course_key,status,expires_at);
CREATE INDEX idx_classroom_commands_history ON classroom_commands(course_key,student_id,created_at DESC,command_id DESC);
CREATE TABLE classroom_command_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id TEXT NOT NULL REFERENCES classroom_commands(command_id),
    course_key TEXT NOT NULL,
    student_id INTEGER NOT NULL REFERENCES platform_students(id),
    status TEXT NOT NULL,
    actor TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX idx_classroom_command_events_course ON classroom_command_events(course_key,student_id,id);
CREATE TRIGGER classroom_command_identity_immutable BEFORE UPDATE ON classroom_commands
WHEN NEW.command_id IS NOT OLD.command_id OR NEW.course_key IS NOT OLD.course_key
 OR NEW.student_id IS NOT OLD.student_id OR NEW.request_key IS NOT OLD.request_key
 OR NEW.request_json IS NOT OLD.request_json OR NEW.kind IS NOT OLD.kind
 OR NEW.assignment_id IS NOT OLD.assignment_id OR NEW.message IS NOT OLD.message
 OR NEW.created_by IS NOT OLD.created_by OR NEW.created_at IS NOT OLD.created_at
 OR NEW.expires_at IS NOT OLD.expires_at
BEGIN SELECT RAISE(ABORT,'classroom request identity is immutable'); END;
CREATE TRIGGER classroom_command_audit_immutable BEFORE UPDATE ON classroom_command_events
BEGIN SELECT RAISE(ABORT,'classroom request audit is immutable'); END;
"""
