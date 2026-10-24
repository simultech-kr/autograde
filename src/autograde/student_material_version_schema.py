"""Append-only personal material editions (platform migration 18).

Published payloads are never edited. Publishing an edition atomically archives its
predecessor, while request records and the predecessor's files remain untouched.
"""

SCHEMA = """
CREATE TABLE student_material_versions (
 material_id TEXT PRIMARY KEY REFERENCES student_materials(material_id) ON DELETE RESTRICT,
 series_id TEXT NOT NULL REFERENCES student_materials(material_id) ON DELETE RESTRICT,
 edition INTEGER NOT NULL CHECK(edition>=1),
 previous_material_id TEXT REFERENCES student_materials(material_id) ON DELETE RESTRICT,
 change_note TEXT NOT NULL DEFAULT '' CHECK(length(change_note)<=1000),
 UNIQUE(series_id,edition),
 CHECK((edition=1 AND series_id=material_id AND previous_material_id IS NULL AND change_note='')
    OR (edition>1 AND previous_material_id IS NOT NULL AND previous_material_id!=material_id
        AND length(trim(change_note))>0))
);
INSERT INTO student_material_versions(material_id,series_id,edition)
 SELECT material_id,material_id,1 FROM student_materials;
CREATE INDEX idx_student_material_versions_previous ON student_material_versions(previous_material_id);

CREATE TRIGGER student_material_version_insert BEFORE INSERT ON student_material_versions
WHEN NOT EXISTS(SELECT 1 FROM student_materials m WHERE m.material_id=NEW.material_id
                  AND m.status='draft' AND m.published_at IS NULL)
 OR (NEW.previous_material_id IS NOT NULL AND (
   NOT EXISTS(SELECT 1 FROM student_materials source
     JOIN student_material_versions previous ON previous.material_id=source.material_id
     JOIN student_materials target ON target.material_id=NEW.material_id
     WHERE source.material_id=NEW.previous_material_id AND source.status='published'
       AND source.course_key=target.course_key AND previous.series_id=NEW.series_id)
   OR NEW.edition!=(SELECT COALESCE(max(edition),0)+1 FROM student_material_versions WHERE series_id=NEW.series_id)
   OR EXISTS(SELECT 1 FROM student_material_versions v JOIN student_materials m ON m.material_id=v.material_id
             WHERE v.series_id=NEW.series_id AND m.status='draft')
 ))
BEGIN SELECT RAISE(ABORT,'invalid material edition or active revision draft'); END;
CREATE TRIGGER student_material_version_no_update BEFORE UPDATE ON student_material_versions
BEGIN SELECT RAISE(ABORT,'material edition history is immutable'); END;
CREATE TRIGGER student_material_version_no_delete BEFORE DELETE ON student_material_versions
BEGIN SELECT RAISE(ABORT,'material edition history is immutable'); END;

CREATE TRIGGER student_material_version_publish BEFORE UPDATE ON student_materials
WHEN NEW.status='published' AND (
 NOT EXISTS(SELECT 1 FROM student_material_versions v WHERE v.material_id=NEW.material_id)
 OR EXISTS(SELECT 1 FROM student_material_versions v
   LEFT JOIN student_materials previous ON previous.material_id=v.previous_material_id
   WHERE v.material_id=NEW.material_id AND v.previous_material_id IS NOT NULL
     AND (previous.status IS NULL OR previous.status!='published'))
)
BEGIN SELECT RAISE(ABORT,'material predecessor is no longer current'); END;
CREATE TRIGGER student_material_version_supersede AFTER UPDATE ON student_materials
WHEN NEW.status='published' AND OLD.status='draft'
BEGIN
 UPDATE student_materials SET status='archived',revision=revision+1,updated_at=NEW.updated_at
 WHERE material_id=(SELECT previous_material_id FROM student_material_versions WHERE material_id=NEW.material_id);
END;
"""
