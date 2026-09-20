"""Read-only instructor source viewer; never extracts, renders or runs uploads."""
from html import escape
import hashlib
import difflib
import tarfile
import unicodedata
from urllib.parse import quote

from .platform_bundle import BundleError, BundleStorageError
from .platform_state import PlatformNotFound
from .web_theme import THEME_CSS
from .instructor_responsive import RESPONSIVE_CSS, result_table

MAX_SOURCE_BYTES = 256 * 1024
MAX_LINES = 5000
MAX_RENDERED_CODE_BYTES = 256 * 1024
MAX_DIFF_LINES = 1000


class ComparisonUnavailable(ValueError):
    """Safe user-facing explanation, never a raw archive or filesystem error."""


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


def _comparison_source(store, row, entry):
    if entry is None:
        return ''
    if entry.size > MAX_SOURCE_BYTES:
        raise ComparisonUnavailable('파일이 256 KiB를 초과하여 비교를 생략합니다.')
    artifact = store.get(row['source_digest'])
    with tarfile.open(artifact.path, mode='r:gz') as archive:
        member = archive.getmember(entry.path)
        if not member.isfile() or member.size != entry.size:
            raise BundleStorageError('source changed')
        with archive.extractfile(member) as stream:
            raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) != entry.size or 'sha256:' + hashlib.sha256(raw).hexdigest() != entry.sha256:
        raise BundleStorageError('source changed')
    if b'\x00' in raw:
        raise ComparisonUnavailable('바이너리 파일은 코드 비교를 제공하지 않습니다.')
    try:
        text = _visible_controls(raw.decode('utf-8-sig'))
    except UnicodeDecodeError:
        raise ComparisonUnavailable('UTF-8이 아닌 파일은 코드 비교를 제공하지 않습니다.') from None
    if len(text.splitlines()) > MAX_DIFF_LINES:
        raise ComparisonUnavailable('1,000줄을 초과하여 비교를 생략합니다. 제출 원본 화면에서 확인하세요.')
    return text


def render_comparison(state, store, course_key, submission_id, previous_id, file_index=None):
    current, _ = state.get_instructor_submission_review(course_key=course_key, submission_id=submission_id)
    previous, _ = state.get_instructor_submission_review(course_key=course_key, submission_id=previous_id)
    if (current['student_id'], current['assignment_id']) != (previous['student_id'], previous['assignment_id']) or (
            previous['received_at'], previous['receipt_order']) >= (current['received_at'], current['receipt_order']):
        raise PlatformNotFound('comparison unavailable')
    base = '/courses/' + quote(course_key, safe='') + '/instructor/submissions/' + quote(submission_id, safe='')
    root = base + '/compare/' + quote(previous_id, safe='')
    body = (f'<nav class="page-actions"><a href="{base}">제출 이력으로 돌아가기</a></nav>'
            f'<h1>제출 코드 비교</h1><h2>{_e(current["student_key"])} · {_e(current["title"])}</h2>'
            f'<p>이전: {_e(previous["received_at"])} · {_e(previous_id)}<br>현재: {_e(current["received_at"])} · {_e(submission_id)}</p>'
            '<p>− 이전 제출에서 삭제 / + 현재 제출에 추가. 코드 변경과 점수 변화의 인과관계를 자동 판정하지 않습니다. 줄바꿈은 정규화하여 비교합니다.</p>')
    before_score = '미확정' if previous['score'] is None else f'{previous["score"]:g} / {previous["max_score"]:g}'
    after_score = '미확정' if current['score'] is None else f'{current["score"]:g} / {current["max_score"]:g}'
    delta = '비교 불가'
    if previous['score'] is not None and current['score'] is not None and previous['max_score'] == current['max_score']:
        delta = f'{current["score"] - previous["score"]:+g}'
    body += f'<p>점수: {_e(before_score)} → {_e(after_score)} · 증감 {_e(delta)}</p>'
    status = 200
    try:
        if store is None:
            raise BundleStorageError('source unavailable')
        artifacts = [store.get(row['source_digest']) for row in (previous, current)]
        if any(a.metadata.kind != 'submission' for a in artifacts):
            raise BundleStorageError('not source')
        old, new = [{entry.path: entry for entry in a.metadata.files} for a in artifacts]
        paths = sorted(set(old) | set(new))
        if file_index is not None and not 0 <= file_index < len(paths):
            raise PlatformNotFound('file unavailable')
        start = ((file_index or 0) // 200) * 200
        body += '<h2>파일 변경 목록</h2><ul class="source-files">'
        for index in range(start, min(start + 200, len(paths))):
            name = paths[index]
            change = '추가' if name not in old else '삭제' if name not in new else '변경 없음' if old[name].sha256 == new[name].sha256 else '수정'
            body += f'<li>{change} · <a href="{root}/files/{index}">{_e(_visible_controls(name))}</a></li>'
        body += '</ul>'
        if start:
            body += f'<a href="{root}/files/{start - 200}">이전 파일 200개</a> '
        if start + 200 < len(paths):
            body += f'<a href="{root}/files/{start + 200}">다음 파일 200개</a>'
        if current['source_digest'] == previous['source_digest']:
            body += '<p>두 제출은 동일한 소스입니다. 제출 시각과 접수번호는 별도로 보존됩니다.</p>'
        elif file_index is None:
            body += '<p>변경 내용을 볼 파일을 선택하세요.</p>'
        else:
            name = paths[file_index]
            body += '<h2>' + _e(_visible_controls(name)) + '</h2>'
            try:
                before = _comparison_source(store, previous, old.get(name))
                after = _comparison_source(store, current, new.get(name))
                lines = difflib.unified_diff(before.splitlines(), after.splitlines(), fromfile='이전 제출', tofile='현재 제출', lineterm='')
                rendered, size = [], 0
                for line in lines:
                    escaped = _e(line)
                    if line.startswith(('+', '-')):
                        escaped = '<span class="' + ('diff-added' if line.startswith('+') else 'diff-removed') + '">' + escaped + '</span>'
                    size += len(escaped.encode('utf-8')) + 1
                    if size > MAX_RENDERED_CODE_BYTES:
                        body += '<p>비교 표시 한도(256 KiB)에 도달해 일부만 표시합니다.</p>'
                        break
                    rendered.append(escaped)
                body += ('<pre class="source-code" tabindex="0" role="region" aria-label="코드 변경 비교"><code>' + '\n'.join(rendered) + '</code></pre>') if rendered else '<p>정규화된 텍스트 내용에 차이가 없습니다.</p>'
            except ComparisonUnavailable as exc:
                body += '<p>' + _e(exc) + '</p>'
    except (BundleError, OSError, tarfile.TarError, KeyError):
        status = 503
        body += '<p role="alert">제출 원본을 읽을 수 없습니다. 보관 상태를 확인하세요.</p>'
    return status, ('<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>제출 코드 비교</title><style>body{margin:0;font:16px/1.6 system-ui}main{max-width:1200px;margin:24px auto;padding:24px}'
        '.diff-added{background:var(--soft)}.diff-removed{background:var(--error-bg);color:var(--error)}'
        + THEME_CSS + RESPONSIVE_CSS + '</style></head><body class="responsive-instructor"><main>' + body + '</main></body></html>')


def render_review(state, store, course_key, submission_id, file_index=None, history_offset=0):
    row, history = state.get_instructor_submission_review(course_key=course_key, submission_id=submission_id, offset=history_offset)
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
            '감점이나 실패는 부정행위 판정이 아닙니다. 비공개 채점 자료는 포함하지 않습니다.</p>')
    best = state.get_bundle_previous_best(submission_id)
    body += '<p>선택한 제출 이전 최고점: ' + (f'{best["score"]:g} / {best["max_score"]:g} · {_e(best["received_at"])} (공개 결과만)' if best else '이전 공개 점수 없음') + '</p>'
    body += '<details' + (' open' if file_index is None else '') + '><summary>이 학생의 동일 과제 제출 이력 (100건씩)</summary>'
    timeline = []
    for item in history[:100]:
        url = base + '/submissions/' + quote(item['submission_id'], safe='')
        delta = '—'
        if item['score'] is not None and item['previous_score'] is not None and item['max_score'] == item['previous_max_score']:
            delta = f'{item["score"] - item["previous_score"]:+g}'
        earned = '미확정' if item['score'] is None else f'{item["score"]:g} / {item["max_score"]:g}'
        comparison = '첫 제출'
        if item['previous_id']:
            comparison = f'<a href="{url}/compare/{quote(item["previous_id"], safe="")}">직전 제출과 코드 비교</a>'
            if item['source_digest'] == item['previous_digest']:
                comparison += '<br>동일 소스 재제출'
        timeline.append((f'<a href="{url}">#{item["version"]} 제출' + (' · 선택됨' if item['submission_id'] == submission_id else '') + '</a>',
                         _e(item['received_at']), '<code>' + _e(item['source_digest']) + '</code>',
                         _e(earned), _e(delta), _e(item['state']), comparison))
    body += result_table(('접수 순번', '제출 시각 (UTC)', '코드 버전 (SHA-256)', '점수', '직전 대비 증감', '상태', '코드 비교'), timeline, '학생별 점수·코드 변경 이력')
    body += '<p>순번은 보관된 접수 순서이며 SHA-256은 제출 묶음의 식별값입니다. 증감은 바로 이전 제출과 같은 배점일 때만 계산합니다. 미채점·실패·배점 변경은 0점으로 간주하지 않습니다. 과제 릴리스별 이력이며 과거에 중복 처리된 제출 클릭은 복원할 수 없습니다.</p>'
    if history_offset:
        body += f'<a href="{root}/history/{max(0, history_offset - 100)}">더 최근 제출 100건</a> '
    if len(history) > 100:
        body += f'<a href="{root}/history/{history_offset + 100}">이전 제출 100건</a>'
    body += '</details>'
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
        body += '<h2>제출 파일</h2><ul class="source-files">'
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
                    body += '<pre class="source-code" tabindex="0" role="region" aria-label="제출 코드 원문 · 가로 스크롤 가능"><code>' + preview + '</code></pre>'
    except (BundleError, OSError, tarfile.TarError, KeyError):
        status = 503
        body += '<p class="error" role="alert">제출 원본을 읽을 수 없습니다. 보관 상태를 확인하고 다시 시도하세요.</p>'
    style = ('body{margin:0;font:16px/1.6 system-ui}main{max-width:1200px;margin:24px auto;padding:24px}'
             'code{overflow-wrap:anywhere}li{margin:6px 0}a{overflow-wrap:anywhere}.notice{padding:16px}' + THEME_CSS + RESPONSIVE_CSS)
    return status, ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>제출 코드 확인 · Autograde</title><style>' + style + '</style></head>'
                    '<body class="responsive-instructor"><main>' + body + '</main></body></html>')
