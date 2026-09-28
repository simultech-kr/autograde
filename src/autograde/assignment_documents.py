"""Append-only public instructions, independent of immutable grading releases.

Only reviewed, instructor-authored explanations belong here. Code, test cases,
deadlines and scores are deliberately not editable through this module.
"""
from __future__ import annotations

import hashlib
import json


SCHEMA = """
CREATE TABLE assignment_document_revisions (
    assignment_id TEXT NOT NULL REFERENCES bundle_assignment_releases(assignment_id),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    content TEXT NOT NULL CHECK (length(content) <= 20000),
    sha256 TEXT NOT NULL,
    change_note TEXT NOT NULL CHECK (length(change_note) <= 500),
    actor TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (assignment_id, revision)
);
CREATE TRIGGER assignment_document_no_update BEFORE UPDATE ON assignment_document_revisions
BEGIN SELECT RAISE(ABORT, 'assignment document revisions are immutable'); END;
CREATE TRIGGER assignment_document_no_delete BEFORE DELETE ON assignment_document_revisions
BEGIN SELECT RAISE(ABORT, 'assignment document revisions are immutable'); END;
CREATE TABLE bundle_submission_documents (
    submission_id TEXT PRIMARY KEY REFERENCES bundle_submission_requests(submission_id),
    assignment_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (assignment_id, revision)
        REFERENCES assignment_document_revisions(assignment_id, revision)
);
CREATE TRIGGER submission_document_scope BEFORE INSERT ON bundle_submission_documents
WHEN NOT EXISTS (
    SELECT 1 FROM bundle_submission_requests AS s
    JOIN assignment_document_revisions AS d ON d.assignment_id=s.assignment_id
    WHERE s.submission_id=NEW.submission_id AND d.assignment_id=NEW.assignment_id
      AND d.revision=NEW.revision AND d.sha256=NEW.sha256
)
BEGIN SELECT RAISE(ABORT, 'submission document scope mismatch'); END;
CREATE TRIGGER submission_document_no_update BEFORE UPDATE ON bundle_submission_documents
BEGIN SELECT RAISE(ABORT, 'submission document snapshots are immutable'); END;
CREATE TRIGGER submission_document_no_delete BEFORE DELETE ON bundle_submission_documents
BEGIN SELECT RAISE(ABORT, 'submission document snapshots are immutable'); END;
"""


def digest(content):
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def metadata(document):
    return {key: document[key] for key in ("revision", "sha256", "updated_at", "change_note")}


def current(connection, course, assignment_id, *, with_history=True):
    """Read without mutating old databases; revision zero is the original README."""
    from .platform_state import PlatformNotFound
    release = connection.execute(
        "SELECT created_at FROM bundle_assignment_releases WHERE assignment_id=? AND course_key=?",
        (assignment_id, course),
    ).fetchone()
    if release is None:
        raise PlatformNotFound("과제를 찾을 수 없습니다.")
    row = connection.execute(
        "SELECT * FROM assignment_document_revisions WHERE assignment_id=? ORDER BY revision DESC LIMIT 1",
        (assignment_id,),
    ).fetchone()
    if row is None:
        draft = connection.execute(
            "SELECT document_json FROM instructor_assignment_drafts WHERE course_key=? AND published_assignment_id=?",
            (course, assignment_id),
        ).fetchone()
        if draft is None:
            # CLI releases have no web-authored description. Never invent one.
            return None
        content = json.loads(draft[0])["description"]
        document = dict(assignment_id=assignment_id, revision=0, content=content,
                        sha256=digest(content), updated_at=release["created_at"],
                        change_note="최초 배포 설명", actor="original-release")
        history = [metadata(document)]
    else:
        document = dict(row)
        history = []
        if with_history:
            history = [dict(item) for item in connection.execute(
                "SELECT revision,sha256,updated_at,change_note FROM assignment_document_revisions "
                "WHERE assignment_id=? AND revision<=? ORDER BY revision DESC LIMIT 50",
                (assignment_id, document["revision"]),
            )]
    if with_history:
        document["history"] = history
        document["history_total"] = document["revision"] + 1
    return document


def _insert(connection, document):
    connection.execute(
        "INSERT INTO assignment_document_revisions "
        "(assignment_id,revision,content,sha256,change_note,actor,updated_at) VALUES (?,?,?,?,?,?,?)",
        tuple(document[key] for key in ("assignment_id", "revision", "content", "sha256", "change_note", "actor", "updated_at")),
    )


def ensure_original(connection, course, assignment_id):
    document = current(connection, course, assignment_id, with_history=False)
    if document is not None and not connection.execute(
        "SELECT 1 FROM assignment_document_revisions WHERE assignment_id=? LIMIT 1", (assignment_id,),
    ).fetchone():
        _insert(connection, document)
    return document


def revise(connection, course, assignment_id, revision, content, change_note, *, at):
    from .platform_state import PlatformConflict, PlatformNotFound
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("설명 버전이 올바르지 않습니다.")
    if not isinstance(content, str) or not content.strip() or len(content) > 20000:
        raise ValueError("설명은 1~20,000자로 입력하세요.")
    if not isinstance(change_note, str) or not change_note.strip() or len(change_note) > 500:
        raise ValueError("학생에게 표시할 변경 사유를 1~500자로 입력하세요.")
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    change_note = change_note.strip()
    if any(ord(char) < 32 and char not in "\n\t" for char in content + change_note):
        raise ValueError("설명과 변경 사유에 지원하지 않는 제어 문자가 있습니다.")
    try:
        content_hash = digest(content)
        change_note.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("설명과 변경 사유는 유효한 UTF-8 문자여야 합니다.") from exc
    old = current(connection, course, assignment_id, with_history=False)
    if old is None:
        raise PlatformNotFound("웹에서 등록한 과제만 설명을 수정할 수 있습니다.")
    if revision != old["revision"]:
        raise PlatformConflict("다른 창에서 설명이 변경되었습니다. 최신 버전을 확인하세요.")
    if content_hash == old["sha256"]:
        return current(connection, course, assignment_id)  # Idempotent no-op.
    if revision >= 200:
        raise PlatformConflict("이 과제의 설명 변경 이력 한도에 도달했습니다.")
    ensure_original(connection, course, assignment_id)
    _insert(connection, dict(assignment_id=assignment_id, revision=revision + 1,
        content=content, sha256=content_hash, change_note=change_note,
        actor="instructor-web", updated_at=at))
    return current(connection, course, assignment_id)


def record_submission(connection, course, assignment_id, submission_id, *, at):
    """Server-current version at receipt time, NOT proof of student reading."""
    # Older-schema migration fixtures/maintenance readers do not have this module.
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='assignment_document_revisions' AND type='table'").fetchone():
        return
    document = ensure_original(connection, course, assignment_id)
    if document is not None:
        connection.execute(
            "INSERT INTO bundle_submission_documents VALUES (?,?,?,?,?)",
            (submission_id, assignment_id, document["revision"], document["sha256"], at),
        )
