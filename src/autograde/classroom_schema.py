"""Minimal classroom telemetry; no local paths, file contents or credentials."""

SCHEMA = """
CREATE TABLE classroom_clients (
    client_session_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    course_key TEXT NOT NULL,
    student_id INTEGER NOT NULL REFERENCES platform_students(id),
    latest_sequence INTEGER NOT NULL,
    latest_payload TEXT NOT NULL,
    environment_json TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX idx_classroom_clients_course ON classroom_clients(course_key,student_id,last_seen_at);
CREATE TABLE classroom_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_session_id TEXT NOT NULL REFERENCES classroom_clients(client_session_id),
    course_key TEXT NOT NULL,
    student_id INTEGER NOT NULL REFERENCES platform_students(id),
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    assignment_id TEXT,
    received_at TEXT NOT NULL,
    UNIQUE(client_session_id,sequence)
);
CREATE INDEX idx_classroom_events_student ON classroom_events(course_key,student_id,id DESC);
CREATE INDEX idx_classroom_events_received ON classroom_events(course_key,student_id,received_at);
"""
