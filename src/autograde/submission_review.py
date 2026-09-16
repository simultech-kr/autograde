"""Read-only instructor source viewer; never extracts, renders or runs uploads."""
from html import escape
import hashlib
import tarfile
import unicodedata
from urllib.parse import quote

from .platform_bundle import BundleError, BundleStorageError
from .platform_state import PlatformNotFound
from .web_theme import THEME_CSS

MAX_SOURCE_BYTES = 256 * 1024
MAX_LINES = 5000
MAX_RENDERED_CODE_BYTES = 256 * 1024


def attention_reason(state, score=None, max_score=None):
    if state in {'rejected', 'infra_failed', 'assessment_failed'}:
        return '처리 실패 · 확인 필요'
    if score is not None and max_score is not None and score < max_score:
        return '감점 · 확인 필요'
    if not state:
        return '미제출'
    if state not in {'published', 'graded'}:
        return '처리 중'
    return '채점 완료'


def _e(value):
    return escape(str(value if value is not None else ''), quote=True)


def _visible_controls(text):
    # Bidi overrides and zero-width control characters must not disguise source.
    return ''.join(f'\\u{ord(c):04x}' if unicodedata.category(c) in {'Cc', 'Cf'} and c not in '\n\t' else c
                   for c in text.replace('\r\n', '\n').replace('\r', '\n'))


def _source_preview(text):
    lines = _visible_controls(text).split('\n')
    rendered, size = [], 0
    truncated = len(lines) > MAX_LINES
    for number, line in enumerate(lines[:MAX_LINES], 1):
        numbered = f'{number:4} | {line}'
        escaped = _e(numbered)
        if size + len(escaped.encode('utf-8')) + 1 > MAX_RENDERED_CODE_BYTES:
            # One original character expands to at most six escaped UTF-8 bytes.
            # Trim before escaping, never through an HTML entity or UTF-8 sequence.
            remaining = max(0, (MAX_RENDERED_CODE_BYTES - size - 1) // 6)
            rendered.append(_e(numbered[:remaining]))
            truncated = True
            break
        rendered.append(escaped)
        size += len(escaped.encode('utf-8')) + 1
    return '\n'.join(rendered), truncated


def render_review(state, store, course_key, submission_id, file_index=None):
    row, history = state.get_instructor_submission_review(course_key=course_key, submission_id=submission_id)
    base = '/courses/' + quote(course_key, safe='') + '/instructor'
    root = base + '/submissions/' + quote(submission_id, safe='')
    score = '미확정' if row['score'] is None else f"{row['score']:g} / {row['max_score']:g}"
    body = (f'<p><a href="{base}">수업 관리</a> · <a href="{base}/submissions">제출·채점 현황</a></p>'
            f'<h1>제출 코드 확인</h1><h2>{_e(row["student_key"])} · {_e(row["title"])}</h2>'
            f'<p>과제 {_e(row["assignment_key"])} / 릴리스 {_e(row["release_id"])}</p>'
            f'<p>제출 ID <code>{_e(submission_id)}</code><br>접수 {_e(row["received_at"])}</p>'
            f'<p>상태 {_e(row["state"])} · 점수 {_e(score)} · {_e(attention_reason(row["state"], row["score"], row["max_score"]))}</p>'
            f'<p>실패 코드: {_e(row["failure_code"] or "없음")}</p>'
            '<p class="notice">제출 당시 원본을 읽기 전용으로 표시합니다. 실행·수정·재채점하지 않습니다. '
            '감점이나 실패는 부정행위 판정이 아닙니다. 비공개 채점 자료는 포함하지 않습니다.</p>'
            '<details><summary>이 학생의 동일 과제 제출 이력 (최근 100건)</summary><ul>')
    for item in history[:100]:
        body += (f'<li><a href="{base}/submissions/{quote(item["submission_id"], safe="")}">'
                 f'{_e(item["received_at"])} · {_e(item["submission_id"])} · {_e(item["state"])}</a></li>')
    body += '</ul>' + ('<p>이전 이력이 더 있습니다.</p>' if len(history) > 100 else '') + '</details>'
    status = 200
    try:
        if store is None:
            raise BundleStorageError('source storage unavailable')
        artifact = store.get(row['source_digest'])
        if artifact.metadata.kind != 'submission':
            raise BundleStorageError('not a student source bundle')
        files = artifact.metadata.files
        body += f'<p>원본 SHA-256 <code>{_e(artifact.digest)}</code> · 파일 {len(files)}개</p>'
        # Paginate the file list around the selected index without user paths.
        if file_index is not None and not 0 <= file_index < len(files):
            raise PlatformNotFound('file not found')
        start = ((file_index or 0) // 200) * 200
        body += '<h2>제출 파일</h2><ul>'
        for index in range(start, min(start + 200, len(files))):
            entry = files[index]
            label = _visible_controls(entry.path)
            label = label[:200] + '…' if len(label) > 200 else label
            body += f'<li><a href="{root}/files/{index}">{_e(label)}</a> ({entry.size} bytes)</li>'
        body += '</ul>'
        if start:
            body += f'<a href="{root}/files/{start - 200}">이전 파일 200개</a> '
        if start + 200 < len(files):
            body += f'<a href="{root}/files/{start + 200}">다음 파일 200개</a>'
        if not files:
            body += '<p>제출된 파일이 없습니다.</p>'
        elif file_index is None:
            body += '<p>위에서 확인할 파일을 선택하세요.</p>'
        else:
            entry = files[file_index]
            body += f'<h2>{_e(_visible_controls(entry.path))}</h2>'
            if entry.size > MAX_SOURCE_BYTES:
                body += '<p>256 KiB를 초과하여 웹 미리보기를 제공하지 않습니다.</p>'
            else:
                with tarfile.open(artifact.path, mode='r:gz') as archive:
                    member = archive.getmember(entry.path)
                    if not member.isfile() or member.size != entry.size:
                        raise BundleStorageError('source changed')
                    with archive.extractfile(member) as stream:
                        raw = stream.read(MAX_SOURCE_BYTES + 1)
                if len(raw) != entry.size or 'sha256:' + hashlib.sha256(raw).hexdigest() != entry.sha256:
                    raise BundleStorageError('source changed')
                try:
                    if b'\x00' in raw:
                        raise UnicodeDecodeError('utf-8', raw, 0, 1, 'binary')
                    text = raw.decode('utf-8-sig', errors='strict')
                except UnicodeDecodeError:
                    body += '<p>바이너리 또는 UTF-8이 아닌 파일은 미리보기를 제공하지 않습니다.</p>'
                else:
                    preview, truncated = _source_preview(text)
                    body += '<p class="hint">줄 번호 표시 · 제어/방향 전환 문자는 \\uXXXX로 표시합니다.</p>'
                    if truncated:
                        body += '<p>표시 한도(5,000줄 또는 변환 후 256 KiB)에 도달해 일부만 표시합니다.</p>'
                    body += '<pre class="source-code"><code>' + preview + '</code></pre>'
    except (BundleError, OSError, tarfile.TarError, KeyError):
        status = 503
        body += '<p class="error" role="alert">제출 원본을 읽을 수 없습니다. 보관 상태를 확인하고 다시 시도하세요.</p>'
    style = ('body{margin:0;font:16px/1.6 system-ui}main{max-width:1200px;margin:24px auto;padding:24px}'
             'code{overflow-wrap:anywhere}li{margin:6px 0}a{overflow-wrap:anywhere}.notice{padding:16px}' + THEME_CSS)
    return status, ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>제출 코드 확인 · Autograde</title><style>' + style + '</style></head>'
                    '<body><main>' + body + '</main></body></html>')
