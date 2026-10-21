"""Independent exam-paper distribution schema (platform migration 16)."""

SCHEMA = """
CREATE TABLE exam_papers (
 paper_id TEXT PRIMARY KEY,
 course_key TEXT NOT NULL REFERENCES admin_courses(course_key) ON DELETE RESTRICT,
 title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
 instructions TEXT NOT NULL DEFAULT '' CHECK(length(instructions)<=5000),
 opens_at TEXT NOT NULL,
 closes_at TEXT NOT NULL CHECK(closes_at > opens_at),
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','archived')),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
 pdf_sha256 TEXT,
 pdf_size_bytes INTEGER,
 published_at TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 CHECK((pdf_sha256 IS NULL AND pdf_size_bytes IS NULL) OR
       (length(pdf_sha256)=64 AND pdf_size_bytes BETWEEN 1 AND 10485760)),
 CHECK(status != 'published' OR (published_at IS NOT NULL AND pdf_sha256 IS NOT NULL)),
 CHECK(status != 'draft' OR published_at IS NULL)
);
CREATE INDEX idx_exam_papers_course ON exam_papers(course_key,status,opens_at);
CREATE TABLE exam_paper_requests (
 paper_id TEXT NOT NULL REFERENCES exam_papers(paper_id) ON DELETE RESTRICT,
 student_id INTEGER NOT NULL REFERENCES platform_students(id) ON DELETE RESTRICT,
 first_requested_at TEXT NOT NULL,
 last_requested_at TEXT NOT NULL,
 request_count INTEGER NOT NULL CHECK(request_count>=1),
 PRIMARY KEY(paper_id,student_id)
);
CREATE TABLE exam_paper_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 paper_id TEXT NOT NULL REFERENCES exam_papers(paper_id) ON DELETE RESTRICT,
 revision INTEGER NOT NULL,
 action TEXT NOT NULL,
 actor TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TRIGGER exam_paper_identity_immutable BEFORE UPDATE ON exam_papers
WHEN NEW.paper_id IS NOT OLD.paper_id OR NEW.course_key IS NOT OLD.course_key
 OR NEW.created_at IS NOT OLD.created_at OR NEW.revision != OLD.revision+1
BEGIN SELECT RAISE(ABORT,'exam paper identity/revision is immutable'); END;
CREATE TRIGGER exam_paper_publication_immutable BEFORE UPDATE ON exam_papers
WHEN OLD.published_at IS NOT NULL AND (
 NEW.title IS NOT OLD.title OR NEW.instructions IS NOT OLD.instructions
 OR NEW.opens_at IS NOT OLD.opens_at OR NEW.closes_at IS NOT OLD.closes_at
 OR NEW.pdf_sha256 IS NOT OLD.pdf_sha256 OR NEW.pdf_size_bytes IS NOT OLD.pdf_size_bytes
 OR NEW.published_at IS NOT OLD.published_at)
BEGIN SELECT RAISE(ABORT,'published exam paper is immutable'); END;
CREATE TRIGGER exam_paper_lifecycle BEFORE UPDATE ON exam_papers
WHEN OLD.status='archived' OR
 (OLD.status='published' AND NEW.status!='archived')
BEGIN SELECT RAISE(ABORT,'invalid exam paper lifecycle'); END;
CREATE TRIGGER exam_paper_no_delete BEFORE DELETE ON exam_papers
BEGIN SELECT RAISE(ABORT,'archive exam papers instead of deleting'); END;
CREATE TRIGGER exam_paper_audit_no_update BEFORE UPDATE ON exam_paper_audit
BEGIN SELECT RAISE(ABORT,'exam paper audit is immutable'); END;
CREATE TRIGGER exam_paper_audit_no_delete BEFORE DELETE ON exam_paper_audit
BEGIN SELECT RAISE(ABORT,'exam paper audit is immutable'); END;
"""
