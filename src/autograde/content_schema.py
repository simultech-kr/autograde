"""Additive Git-backed teaching-content storage (platform migration 20)."""

SCHEMA = """
CREATE TABLE content_collections (
 collection_id TEXT PRIMARY KEY,
 course_key TEXT NOT NULL REFERENCES admin_courses(course_key) ON DELETE RESTRICT,
 title TEXT NOT NULL,
 instructions TEXT NOT NULL DEFAULT '',
 change_note TEXT NOT NULL DEFAULT '',
 opens_at TEXT NOT NULL,
 closes_at TEXT,
 student_ids_json TEXT,
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
 generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0),
 current_release_id TEXT REFERENCES content_releases(release_id) ON DELETE RESTRICT,
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','withdrawn')),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX idx_content_collections_course ON content_collections(course_key,created_at);
CREATE TABLE content_draft_files (
 collection_id TEXT NOT NULL REFERENCES content_collections(collection_id) ON DELETE RESTRICT,
 path TEXT NOT NULL,
 sha256 TEXT NOT NULL CHECK(length(sha256)=64),
 size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 0 AND 26214400),
 media_type TEXT NOT NULL,
 PRIMARY KEY(collection_id,path)
);
CREATE TABLE content_releases (
 release_id TEXT PRIMARY KEY,
 collection_id TEXT NOT NULL REFERENCES content_collections(collection_id) ON DELETE RESTRICT,
 generation INTEGER NOT NULL CHECK(generation>=1),
 policy_revision INTEGER NOT NULL CHECK(policy_revision>=1),
 source_revision INTEGER NOT NULL,
 title TEXT NOT NULL,
 instructions TEXT NOT NULL,
 change_note TEXT NOT NULL,
 opens_at TEXT NOT NULL,
 closes_at TEXT,
 student_ids_json TEXT,
 git_commit TEXT NOT NULL CHECK(length(git_commit)=40),
 tree_sha256 TEXT NOT NULL CHECK(length(tree_sha256)=64),
 actor TEXT NOT NULL,
 published_at TEXT NOT NULL,
 UNIQUE(collection_id,generation)
);
CREATE TABLE content_release_files (
 release_id TEXT NOT NULL REFERENCES content_releases(release_id) ON DELETE RESTRICT,
 path TEXT NOT NULL,
 sha256 TEXT NOT NULL CHECK(length(sha256)=64),
 size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 0 AND 26214400),
 media_type TEXT NOT NULL,
 PRIMARY KEY(release_id,path)
);
CREATE TABLE content_publish_jobs (
 collection_id TEXT NOT NULL REFERENCES content_collections(collection_id) ON DELETE RESTRICT,
 idempotency_key TEXT NOT NULL,
 request_sha256 TEXT NOT NULL,
 release_id TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('preparing','published','failed')),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(collection_id,idempotency_key)
);
CREATE TABLE content_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 collection_id TEXT NOT NULL REFERENCES content_collections(collection_id) ON DELETE RESTRICT,
 release_id TEXT,
 revision INTEGER NOT NULL,
 action TEXT NOT NULL,
 actor TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TRIGGER content_collection_identity BEFORE UPDATE ON content_collections
WHEN NEW.collection_id IS NOT OLD.collection_id OR NEW.course_key IS NOT OLD.course_key
 OR NEW.created_at IS NOT OLD.created_at OR NEW.revision!=OLD.revision+1
BEGIN SELECT RAISE(ABORT,'content identity/revision is immutable'); END;
CREATE TRIGGER content_release_no_update BEFORE UPDATE ON content_releases
BEGIN SELECT RAISE(ABORT,'content release is immutable'); END;
CREATE TRIGGER content_release_no_delete BEFORE DELETE ON content_releases
BEGIN SELECT RAISE(ABORT,'content release is immutable'); END;
CREATE TRIGGER content_release_file_no_update BEFORE UPDATE ON content_release_files
BEGIN SELECT RAISE(ABORT,'content release file is immutable'); END;
CREATE TRIGGER content_release_file_no_delete BEFORE DELETE ON content_release_files
BEGIN SELECT RAISE(ABORT,'content release file is immutable'); END;
CREATE TRIGGER content_release_file_no_append BEFORE INSERT ON content_release_files
WHEN EXISTS(SELECT 1 FROM content_audit a WHERE a.release_id=NEW.release_id AND a.action='published')
BEGIN SELECT RAISE(ABORT,'published content file list is immutable'); END;
CREATE TRIGGER content_audit_no_update BEFORE UPDATE ON content_audit
BEGIN SELECT RAISE(ABORT,'content audit is immutable'); END;
CREATE TRIGGER content_audit_no_delete BEFORE DELETE ON content_audit
BEGIN SELECT RAISE(ABORT,'content audit is immutable'); END;
"""
