"""Bounded private review before an instructor batch changes student materials.

Review ZIPs are temporary SQLite blobs: they never become student-visible files
until explicit application. One review per instructor/material and a global
budget bound abandoned uploads. Expired blobs are purged on prepare/cancel/apply.
Course deletion/privacy closure includes these course-scoped rows automatically.
"""
from __future__ import annotations

from datetime import timedelta
import hashlib
import io
import json
import re
import secrets
import zipfile

from .exam_papers import _text
from .platform_state import PlatformConflict, PlatformNotFound
from .domain import utc_iso

REVIEW_TTL = timedelta(hours=1)
MAX_PENDING_BYTES = 200 * 1024 * 1024
MAX_PENDING_REVIEWS = 8
_ID = re.compile(r"mbatch_[0-9a-f]{32}\Z")
_FIELDS = "review_id,course_key,material_id,actor,revision,created_at,expires_at,summary_json"


def _purge(db, now):
    db.execute('PRAGMA secure_delete=ON')
    db.execute('DELETE FROM student_material_batch_reviews WHERE expires_at<=?', (now,))


def cleanup_expired(service):
    """Startup/opportunistic cleanup, including installations upgraded in place."""
    with service.state._write() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='student_material_batch_reviews'").fetchone():
            _purge(db, service._now())


def _row(db, course_key, material_id, review_id, actor, now, *, data=False):
    if not isinstance(review_id, str) or not _ID.fullmatch(review_id):
        raise PlatformNotFound('자료 업로드 검토본을 찾을 수 없습니다.')
    row = db.execute('SELECT ' + _FIELDS + (',data' if data else '') +
        ' FROM student_material_batch_reviews WHERE review_id=? AND course_key=? AND material_id=? AND actor=?',
        (review_id, course_key, material_id, actor)).fetchone()
    if row is None:
        raise PlatformNotFound('검토본이 없거나 이미 반영·취소되었습니다. 자료 화면에서 현재 배정을 확인하세요.')
    if row['expires_at'] <= now:
        raise PlatformConflict('업로드 검토 시간이 만료되었습니다. 기존 학생 자료는 유지됩니다. ZIP을 다시 선택해 검토하세요.')
    return dict(row)


def _mapping(db, course_key, material_id, entries):
    rows = []
    for key, extension, filename, content in entries:
        row = db.execute('SELECT s.id,COALESCE(p.name,\'\') name,f.filename,f.sha256,f.size_bytes,f.format '
            'FROM platform_students s JOIN platform_enrollments e ON e.student_id=s.id '
            'LEFT JOIN admin_student_profiles p ON p.student_id=s.id '
            'LEFT JOIN student_material_files f ON f.student_id=s.id AND f.material_id=? '
            'WHERE s.student_key=? AND s.active=1 AND e.course_key=? AND e.active=1',
            (material_id, key, course_key)).fetchone()
        if row is None:
            raise ValueError(f'{filename}: 학번 {key}은(는) 이 수업의 활성 수강생이 아닙니다. '
                             '파일명과 수강 명단을 확인하세요. 기존 배정은 변경되지 않았습니다.')
        digest = hashlib.sha256(content).hexdigest()
        change = ('new' if row['filename'] is None else 'unchanged' if
                  (row['sha256'], row['size_bytes'], row['format']) == (digest, len(content), extension) else 'replace')
        rows.append(dict(student_id=row['id'], student_key=key, name=row['name'], filename=filename,
                         format=extension, size_bytes=len(content), sha256=digest,
                         previous_filename=row['filename'], change=change))
    return rows


def prepare(service, course_key, material_id, *, revision, data, actor):
    from .student_materials import _course, _editable, _material, _revision
    actor = _text(actor, '작업자', 200)
    entries, validated_bytes = service._batch_entries(data)
    now = service.clock()
    with service.state._write() as db:
        _purge(db, utc_iso(now))
        _course(db, course_key, writable=True)
        material = _material(db, course_key, material_id)
        _revision(material, revision)
        _editable(material)
        rows = _mapping(db, course_key, material_id, entries)
        # Invalid replacement uploads never discard the earlier valid review.
        size, count = db.execute('SELECT COALESCE(SUM(length(data)),0),COUNT(*) '
            'FROM student_material_batch_reviews WHERE NOT(material_id=? AND actor=?)', (material_id, actor)).fetchone()
        if size + len(data) > MAX_PENDING_BYTES or count >= MAX_PENDING_REVIEWS:
            raise PlatformConflict('서버의 임시 검토 공간이 가득 찼습니다. 미완료 검토를 반영·취소한 뒤 다시 업로드하세요. '
                                   '기존 학생 자료는 변경되지 않았습니다.')
        review_id = 'mbatch_' + secrets.token_hex(16)
        summary = dict(rows=rows, uploaded_bytes=len(data), validated_bytes=validated_bytes,
                       unchanged_other=material['file_count'] - sum(row['change'] != 'new' for row in rows))
        db.execute('DELETE FROM student_material_batch_reviews WHERE material_id=? AND actor=?', (material_id, actor))
        db.execute('INSERT INTO student_material_batch_reviews '
            '(review_id,course_key,material_id,actor,revision,created_at,expires_at,summary_json,data) VALUES(?,?,?,?,?,?,?,?,?)',
            (review_id, course_key, material_id, actor, revision, utc_iso(now), utc_iso(now + REVIEW_TTL),
             json.dumps(summary, ensure_ascii=False), data))
    return get(service, course_key, material_id, review_id, actor=actor)


def get(service, course_key, material_id, review_id, *, actor):
    from .student_materials import _course, _material
    with service.state._connection() as db:
        db.execute('BEGIN')
        _course(db, course_key)
        material = _material(db, course_key, material_id)
        row = _row(db, course_key, material_id, review_id, actor, service._now())
        summary = json.loads(row.pop('summary_json'))
        row.update(summary)
        row['stale'] = row['revision'] != material['revision'] or material['status'] == 'archived' or bool(material['revision_draft_id'])
        return row


def pending(service, course_key, material_id, *, actor):
    cleanup_expired(service)
    with service.state._connection() as db:
        row = db.execute('SELECT review_id FROM student_material_batch_reviews '
            'WHERE course_key=? AND material_id=? AND actor=? AND expires_at>?',
            (course_key, material_id, actor, service._now())).fetchone()
    return get(service, course_key, material_id, row[0], actor=actor) if row else None


def apply(service, course_key, material_id, review_id, *, actor):
    from .student_materials import _course, _editable, _material, _revision
    # Parse outside the write lock; identity and version fences are repeated in
    # the atomic transaction that updates the files and consumes the review.
    with service.state._connection() as db:
        row = _row(db, course_key, material_id, review_id, actor, service._now(), data=True)
    entries, _ = service._batch_entries(row['data'])
    with service.state._write() as db:
        current = _row(db, course_key, material_id, review_id, actor, service._now())
        _course(db, course_key, writable=True)
        material = _material(db, course_key, material_id)
        _revision(material, current['revision'])
        _editable(material)
        mapped = _mapping(db, course_key, material_id, entries)
        if mapped != json.loads(current['summary_json'])['rows']:
            raise PlatformConflict('검토 후 학생 정보나 파일 배정이 변경되었습니다. 기존 파일은 유지됩니다. ZIP을 다시 검토하세요.')
        result = service._write_entries(db, course_key, material_id, current['revision'], entries, actor, 'batch_review_applied')
        _purge(db, service._now())
        db.execute('DELETE FROM student_material_batch_reviews WHERE review_id=?', (review_id,))
        return result


def cancel(service, course_key, material_id, review_id, *, actor):
    with service.state._write() as db:
        _row(db, course_key, material_id, review_id, actor, service._now())
        _purge(db, service._now())
        db.execute('DELETE FROM student_material_batch_reviews WHERE review_id=?', (review_id,))


def refresh(service, course_key, material_id, review_id, *, actor):
    with service.state._connection() as db:
        row = _row(db, course_key, material_id, review_id, actor, service._now(), data=True)
    material = service.get(course_key, material_id)
    return prepare(service, course_key, material_id, revision=material['revision'], data=row['data'], actor=actor)


def file_content(service, course_key, material_id, review_id, student_id, *, actor):
    from .student_materials import MAX_MATERIAL_BYTES, _read_zip_entry
    with service.state._connection() as db:
        db.execute('BEGIN')
        row = _row(db, course_key, material_id, review_id, actor, service._now())
        summary = json.loads(row['summary_json'])
        metadata = next((item for item in summary['rows'] if item['student_id'] == student_id), None)
        if metadata is None:
            raise PlatformNotFound('이 검토본에 해당 학생의 파일이 없습니다.')
        # Read only the selected outer entry. No repeated decompression of all
        # students or nested ZIPs: the stored blob was fully checked at prepare.
        with zipfile.ZipFile(_BlobReader(db, review_id)) as archive:
            content = _read_zip_entry(archive, archive.getinfo(metadata['filename']), [MAX_MATERIAL_BYTES])
        if len(content) != metadata['size_bytes'] or hashlib.sha256(content).hexdigest() != metadata['sha256']:
            raise PlatformConflict('검토 파일의 무결성을 확인하지 못했습니다. ZIP을 다시 업로드하세요.')
        return content, metadata


class _BlobReader(io.RawIOBase):
    """Seekable SQLite slices, compatible with Python 3.10 (no blobopen needed)."""

    def __init__(self, db, review_id):
        self.db, self.review_id, self.position = db, review_id, 0
        self.size = db.execute('SELECT length(data) FROM student_material_batch_reviews WHERE review_id=?', (review_id,)).fetchone()[0]

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        position = offset + (self.position if whence == 1 else self.size if whence == 2 else 0)
        if whence not in (0, 1, 2) or position < 0:
            raise ValueError('invalid review ZIP seek')
        self.position = position
        return position

    def read(self, size=-1):
        size = max(0, min(self.size - self.position, size if size >= 0 else self.size))
        result = self.db.execute('SELECT substr(data,?,?) FROM student_material_batch_reviews WHERE review_id=?',
                                 (self.position + 1, size, self.review_id)).fetchone()[0]
        self.position += len(result)
        return result
