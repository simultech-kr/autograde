"""Private, expiring material batch review (platform migration 24).

The temporary ZIP is bounded and lives in SQLite so review cancellation, course
privacy erasure and atomic application cannot leave an untracked private file.
"""

SCHEMA = """
CREATE TABLE student_material_batch_reviews (
 review_id TEXT PRIMARY KEY,
 course_key TEXT NOT NULL REFERENCES admin_courses(course_key) ON DELETE RESTRICT,
 material_id TEXT NOT NULL REFERENCES student_materials(material_id) ON DELETE RESTRICT,
 actor TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>=1),
 created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL CHECK(expires_at>created_at),
 summary_json TEXT NOT NULL,
 data BLOB NOT NULL CHECK(length(data) BETWEEN 1 AND 104857600),
 UNIQUE(material_id,actor)
);
CREATE INDEX idx_student_material_batch_expiry ON student_material_batch_reviews(expires_at);
"""
