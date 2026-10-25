"""Course-owned, immutable IDE extension packages and client-reported outcomes."""

SCHEMA = """
CREATE TABLE ide_extension_releases (
 release_id TEXT PRIMARY KEY,
 course_key TEXT NOT NULL REFERENCES admin_courses(course_key) ON DELETE RESTRICT,
 extension_id TEXT NOT NULL,
 version TEXT NOT NULL,
 target_platform TEXT NOT NULL,
 vscode_engine TEXT NOT NULL,
 sha256 TEXT NOT NULL CHECK(length(sha256)=64),
 size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 67108864),
 filename TEXT NOT NULL,
 created_at TEXT NOT NULL,
 actor TEXT NOT NULL,
 UNIQUE(course_key,extension_id,version,target_platform)
);
CREATE INDEX idx_ide_extension_course ON ide_extension_releases(course_key,created_at);
CREATE TABLE ide_extension_desired (
 course_key TEXT NOT NULL REFERENCES admin_courses(course_key) ON DELETE RESTRICT,
 extension_id TEXT NOT NULL,
 target_platform TEXT NOT NULL,
 release_id TEXT NOT NULL REFERENCES ide_extension_releases(release_id) ON DELETE RESTRICT,
 published_at TEXT NOT NULL,
 PRIMARY KEY(course_key,extension_id,target_platform)
);
CREATE TABLE ide_extension_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 release_id TEXT NOT NULL REFERENCES ide_extension_releases(release_id) ON DELETE RESTRICT,
 action TEXT NOT NULL CHECK(action IN ('uploaded','published','withdrawn','superseded')),
 actor TEXT NOT NULL,
 recorded_at TEXT NOT NULL
);
CREATE TABLE ide_extension_client_status (
 release_id TEXT NOT NULL REFERENCES ide_extension_releases(release_id) ON DELETE RESTRICT,
 client_session_id TEXT NOT NULL REFERENCES classroom_clients(client_session_id) ON DELETE CASCADE,
 status TEXT NOT NULL CHECK(status IN ('offered','declined','installing','reload_required','installed','failed')),
 result_code TEXT NOT NULL,
 installed_version TEXT,
 platform TEXT,
 architecture TEXT,
 reported_at TEXT NOT NULL,
 PRIMARY KEY(release_id,client_session_id)
);
CREATE INDEX idx_ide_extension_status_client ON ide_extension_client_status(client_session_id,reported_at);
CREATE TRIGGER ide_extension_release_immutable BEFORE UPDATE ON ide_extension_releases
BEGIN SELECT RAISE(ABORT,'IDE extension release is immutable'); END;
CREATE TRIGGER ide_extension_release_no_delete BEFORE DELETE ON ide_extension_releases
BEGIN SELECT RAISE(ABORT,'IDE extension release is immutable'); END;
CREATE TRIGGER ide_extension_audit_immutable BEFORE UPDATE ON ide_extension_audit
BEGIN SELECT RAISE(ABORT,'IDE extension audit is immutable'); END;
CREATE TRIGGER ide_extension_audit_no_delete BEFORE DELETE ON ide_extension_audit
BEGIN SELECT RAISE(ABORT,'IDE extension audit is immutable'); END;
"""
