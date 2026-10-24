"""Editable non-graded personal material distributions (platform migration 19).

Current publications may change, but displaced private files and their delivery
request snapshots are append-only. Explicit whole-distribution editions remain.
"""

SCHEMA = """
ALTER TABLE student_material_files ADD COLUMN file_revision INTEGER NOT NULL DEFAULT 1 CHECK(file_revision>=1);
CREATE TABLE student_material_file_history (
 material_id TEXT NOT NULL REFERENCES student_materials(material_id) ON DELETE RESTRICT,
 student_id INTEGER NOT NULL REFERENCES platform_students(id) ON DELETE RESTRICT,
 file_revision INTEGER NOT NULL CHECK(file_revision>=1),
 filename TEXT NOT NULL,
 format TEXT NOT NULL CHECK(format IN ('pdf','zip')),
 size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 26214400),
 sha256 TEXT NOT NULL CHECK(length(sha256)=64),
 first_requested_at TEXT,
 last_requested_at TEXT,
 request_count INTEGER NOT NULL CHECK(request_count>=0),
 changed_at TEXT NOT NULL,
 change_type TEXT NOT NULL CHECK(change_type IN ('replaced','removed')),
 actor TEXT NOT NULL,
 PRIMARY KEY(material_id,student_id,file_revision),
 CHECK((request_count=0 AND first_requested_at IS NULL AND last_requested_at IS NULL)
    OR (request_count>0 AND first_requested_at IS NOT NULL AND last_requested_at IS NOT NULL))
);
CREATE TRIGGER student_material_history_insert BEFORE INSERT ON student_material_file_history
WHEN NOT EXISTS(SELECT 1 FROM student_material_files f JOIN student_materials m ON m.material_id=f.material_id
 LEFT JOIN student_material_requests r ON r.material_id=f.material_id AND r.student_id=f.student_id
 WHERE f.material_id=NEW.material_id AND f.student_id=NEW.student_id AND m.status='published'
   AND f.file_revision=NEW.file_revision AND f.filename=NEW.filename AND f.format=NEW.format
   AND f.size_bytes=NEW.size_bytes AND f.sha256=NEW.sha256
   AND COALESCE(r.request_count,0)=NEW.request_count
   AND r.first_requested_at IS NEW.first_requested_at AND r.last_requested_at IS NEW.last_requested_at)
BEGIN SELECT RAISE(ABORT,'material history must preserve current file and requests'); END;
CREATE TRIGGER student_material_history_no_update BEFORE UPDATE ON student_material_file_history
BEGIN SELECT RAISE(ABORT,'material file history is immutable'); END;
CREATE TRIGGER student_material_history_no_delete BEFORE DELETE ON student_material_file_history
BEGIN SELECT RAISE(ABORT,'material file history is immutable'); END;

DROP TRIGGER student_material_publication;
CREATE TRIGGER student_material_publication BEFORE UPDATE ON student_materials
WHEN OLD.published_at IS NOT NULL AND NEW.published_at IS NOT OLD.published_at
BEGIN SELECT RAISE(ABORT,'material publication time is immutable'); END;
DROP TRIGGER student_material_nonempty;
CREATE TRIGGER student_material_nonempty BEFORE UPDATE ON student_materials
WHEN OLD.status='draft' AND NEW.status='published'
 AND NOT EXISTS(SELECT 1 FROM student_material_files f WHERE f.material_id=OLD.material_id)
BEGIN SELECT RAISE(ABORT,'published material requires recipients'); END;
DROP TRIGGER student_material_lifecycle;
CREATE TRIGGER student_material_lifecycle BEFORE UPDATE ON student_materials
WHEN OLD.status='archived' OR (OLD.status='published' AND NEW.status NOT IN ('published','archived'))
BEGIN SELECT RAISE(ABORT,'invalid material lifecycle'); END;
CREATE TRIGGER student_material_pending_revision BEFORE UPDATE ON student_materials
WHEN OLD.status='published' AND NEW.status='published' AND EXISTS(
 SELECT 1 FROM student_material_versions v JOIN student_materials child ON child.material_id=v.material_id
 WHERE v.previous_material_id=OLD.material_id AND child.status='draft')
BEGIN SELECT RAISE(ABORT,'finish or archive pending material revision first'); END;
DROP TRIGGER student_material_version_publish;
CREATE TRIGGER student_material_version_publish BEFORE UPDATE ON student_materials
WHEN OLD.status='draft' AND NEW.status='published' AND (
 NOT EXISTS(SELECT 1 FROM student_material_versions v WHERE v.material_id=NEW.material_id)
 OR EXISTS(SELECT 1 FROM student_material_versions v
   LEFT JOIN student_materials previous ON previous.material_id=v.previous_material_id
   WHERE v.material_id=NEW.material_id AND v.previous_material_id IS NOT NULL
     AND (previous.status IS NULL OR previous.status!='published')))
BEGIN SELECT RAISE(ABORT,'material predecessor is no longer current'); END;

DROP TRIGGER student_material_file_insert;
CREATE TRIGGER student_material_file_insert BEFORE INSERT ON student_material_files
WHEN NOT EXISTS(SELECT 1 FROM student_materials m WHERE m.material_id=NEW.material_id AND m.status IN ('draft','published'))
 OR EXISTS(SELECT 1 FROM student_material_versions v JOIN student_materials child ON child.material_id=v.material_id
   WHERE v.previous_material_id=NEW.material_id AND child.status='draft')
 OR (NOT EXISTS(SELECT 1 FROM student_material_files f WHERE f.material_id=NEW.material_id AND f.student_id=NEW.student_id)
   AND NEW.file_revision!=(SELECT COALESCE(max(file_revision),0)+1 FROM student_material_file_history
     WHERE material_id=NEW.material_id AND student_id=NEW.student_id))
BEGIN SELECT RAISE(ABORT,'material file is not editable or file revision is invalid'); END;
DROP TRIGGER student_material_file_update;
CREATE TRIGGER student_material_file_update BEFORE UPDATE ON student_material_files
WHEN NEW.material_id IS NOT OLD.material_id OR NEW.student_id IS NOT OLD.student_id
 OR NOT EXISTS(SELECT 1 FROM student_materials m WHERE m.material_id=OLD.material_id AND m.status IN ('draft','published'))
 OR EXISTS(SELECT 1 FROM student_material_versions v JOIN student_materials child ON child.material_id=v.material_id
   WHERE v.previous_material_id=OLD.material_id AND child.status='draft')
 OR (EXISTS(SELECT 1 FROM student_materials m WHERE m.material_id=OLD.material_id AND m.status='published') AND (
   ((NEW.sha256 IS NOT OLD.sha256 OR NEW.format IS NOT OLD.format OR NEW.size_bytes IS NOT OLD.size_bytes) AND (
     NEW.file_revision!=OLD.file_revision+1 OR NOT EXISTS(
       SELECT 1 FROM student_material_file_history h WHERE h.material_id=OLD.material_id AND h.student_id=OLD.student_id
       AND h.file_revision=OLD.file_revision AND h.sha256=OLD.sha256 AND h.format=OLD.format
       AND h.size_bytes=OLD.size_bytes AND h.filename=OLD.filename AND h.change_type='replaced')))
   OR ((NEW.sha256 IS OLD.sha256 AND NEW.format IS OLD.format AND NEW.size_bytes IS OLD.size_bytes)
       AND NEW.file_revision!=OLD.file_revision)))
BEGIN SELECT RAISE(ABORT,'material file must retain its history before replacement'); END;
DROP TRIGGER student_material_file_delete;
CREATE TRIGGER student_material_file_delete BEFORE DELETE ON student_material_files
WHEN NOT EXISTS(SELECT 1 FROM student_materials m WHERE m.material_id=OLD.material_id AND m.status IN ('draft','published'))
 OR EXISTS(SELECT 1 FROM student_material_versions v JOIN student_materials child ON child.material_id=v.material_id
   WHERE v.previous_material_id=OLD.material_id AND child.status='draft')
 OR (EXISTS(SELECT 1 FROM student_materials m WHERE m.material_id=OLD.material_id AND m.status='published')
   AND NOT EXISTS(SELECT 1 FROM student_material_file_history h WHERE h.material_id=OLD.material_id
     AND h.student_id=OLD.student_id AND h.file_revision=OLD.file_revision AND h.sha256=OLD.sha256
     AND h.filename=OLD.filename AND h.format=OLD.format AND h.size_bytes=OLD.size_bytes AND h.change_type='removed'))
BEGIN SELECT RAISE(ABORT,'material file must retain its history before removal'); END;
"""
