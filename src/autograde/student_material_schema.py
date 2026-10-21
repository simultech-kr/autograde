"""Student-specific private material distribution (platform migration 17)."""

SCHEMA = """
CREATE TABLE student_materials (
 material_id TEXT PRIMARY KEY,
 course_key TEXT NOT NULL REFERENCES admin_courses(course_key) ON DELETE RESTRICT,
 title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
 instructions TEXT NOT NULL DEFAULT '' CHECK(length(instructions)<=5000),
 opens_at TEXT NOT NULL,
 closes_at TEXT NOT NULL CHECK(closes_at>opens_at),
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','archived')),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
 published_at TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 CHECK(status!='published' OR published_at IS NOT NULL),
 CHECK(status!='draft' OR published_at IS NULL)
);
CREATE INDEX idx_student_materials_course ON student_materials(course_key,status,opens_at);
CREATE TABLE student_material_files (
 material_id TEXT NOT NULL REFERENCES student_materials(material_id) ON DELETE RESTRICT,
 student_id INTEGER NOT NULL REFERENCES platform_students(id) ON DELETE RESTRICT,
 filename TEXT NOT NULL,
 format TEXT NOT NULL CHECK(format IN ('pdf','zip')),
 size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 26214400),
 sha256 TEXT NOT NULL CHECK(length(sha256)=64),
 PRIMARY KEY(material_id,student_id)
);
CREATE TABLE student_material_requests (
 material_id TEXT NOT NULL,
 student_id INTEGER NOT NULL,
 first_requested_at TEXT NOT NULL,
 last_requested_at TEXT NOT NULL,
 request_count INTEGER NOT NULL CHECK(request_count>=1),
 PRIMARY KEY(material_id,student_id),
 FOREIGN KEY(material_id,student_id) REFERENCES student_material_files(material_id,student_id) ON DELETE RESTRICT
);
CREATE TABLE student_material_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 material_id TEXT NOT NULL REFERENCES student_materials(material_id) ON DELETE RESTRICT,
 revision INTEGER NOT NULL,
 action TEXT NOT NULL,
 actor TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TRIGGER student_material_identity BEFORE UPDATE ON student_materials
WHEN NEW.material_id IS NOT OLD.material_id OR NEW.course_key IS NOT OLD.course_key
 OR NEW.created_at IS NOT OLD.created_at OR NEW.revision!=OLD.revision+1
BEGIN SELECT RAISE(ABORT,'material identity/revision is immutable'); END;
CREATE TRIGGER student_material_publication BEFORE UPDATE ON student_materials
WHEN OLD.published_at IS NOT NULL AND (NEW.title IS NOT OLD.title OR NEW.instructions IS NOT OLD.instructions
 OR NEW.opens_at IS NOT OLD.opens_at OR NEW.closes_at IS NOT OLD.closes_at
 OR NEW.published_at IS NOT OLD.published_at)
BEGIN SELECT RAISE(ABORT,'published material is immutable'); END;
CREATE TRIGGER student_material_nonempty BEFORE UPDATE ON student_materials
WHEN NEW.status='published' AND NOT EXISTS(SELECT 1 FROM student_material_files f WHERE f.material_id=OLD.material_id)
BEGIN SELECT RAISE(ABORT,'published material requires recipients'); END;
CREATE TRIGGER student_material_lifecycle BEFORE UPDATE ON student_materials
WHEN OLD.status='archived' OR (OLD.status='published' AND NEW.status!='archived')
BEGIN SELECT RAISE(ABORT,'invalid material lifecycle'); END;
CREATE TRIGGER student_material_no_delete BEFORE DELETE ON student_materials
BEGIN SELECT RAISE(ABORT,'archive materials instead of deleting'); END;
CREATE TRIGGER student_material_file_insert BEFORE INSERT ON student_material_files
WHEN NOT EXISTS(SELECT 1 FROM student_materials m WHERE m.material_id=NEW.material_id AND m.status='draft' AND m.published_at IS NULL)
BEGIN SELECT RAISE(ABORT,'material recipients are immutable after publication'); END;
CREATE TRIGGER student_material_file_update BEFORE UPDATE ON student_material_files
WHEN NEW.material_id IS NOT OLD.material_id OR NEW.student_id IS NOT OLD.student_id
 OR NOT EXISTS(SELECT 1 FROM student_materials m WHERE m.material_id=OLD.material_id AND m.status='draft' AND m.published_at IS NULL)
BEGIN SELECT RAISE(ABORT,'material recipients are immutable after publication'); END;
CREATE TRIGGER student_material_file_delete BEFORE DELETE ON student_material_files
WHEN NOT EXISTS(SELECT 1 FROM student_materials m WHERE m.material_id=OLD.material_id AND m.status='draft' AND m.published_at IS NULL)
BEGIN SELECT RAISE(ABORT,'material recipients are immutable after publication'); END;
CREATE TRIGGER student_material_audit_no_update BEFORE UPDATE ON student_material_audit
BEGIN SELECT RAISE(ABORT,'material audit is immutable'); END;
CREATE TRIGGER student_material_audit_no_delete BEFORE DELETE ON student_material_audit
BEGIN SELECT RAISE(ABORT,'material audit is immutable'); END;
"""
