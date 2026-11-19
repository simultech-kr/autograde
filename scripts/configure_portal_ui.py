#!/usr/bin/env python3
"""Review/apply the Next results opt-in without changing listeners or identities.

Run with the server's Python environment. By default this only previews changes;
--apply validates a candidate in the same directory, saves a byte-exact backup,
and atomically replaces the CSV. No database, Nginx or service is changed.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path
import re
import tempfile


def configure(path, courses, *, apply=False):
    from autograde.pilot_config import load_pilot_config
    original = load_pilot_config(path)
    source = original.source
    courses = tuple(courses)
    if not courses or len(courses) > 100 or len(set(courses)) != len(courses) or any(
        not re.fullmatch(r'[a-z0-9_-]{1,96}', key) for key in courses
    ):
        raise ValueError('실제 course_key를 중복 없이 지정하세요 (최대 100개).')
    if not original.values['instructor_assignment_web_enabled'] or not {'web_port', 'web_public_base_url'} <= original.values.keys():
        raise ValueError('교수자 웹과 web_port·web_public_base_url을 먼저 설정하세요.')
    info = source.stat()
    raw = source.read_bytes()
    changes = {'instructor_next_results_enabled': 'true', 'instructor_next_course_keys': ';'.join(courses)}
    result = {'applied': False, 'config': str(source), 'changes': changes,
              'note': '담당 수업 권한은 유지됩니다. Next.js 실행과 Nginx /app 연결은 별도입니다.'}
    if original.values['instructor_next_results_enabled'] and original.values['instructor_next_course_keys'] == courses:
        return {**result, 'unchanged': True}
    rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
    seen = set()
    for row in rows[1:]:
        if len(row) == 2 and row[0].strip() in changes:
            key = row[0].strip()
            row[1] = changes[key]
            seen.add(key)
    rows.extend([key, value] for key, value in changes.items() if key not in seen)
    out = io.StringIO(newline='')
    csv.writer(out).writerows(rows)
    candidate_bytes = out.getvalue().encode('utf-8')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=source.parent, prefix='.portal-ui-', suffix='.csv', delete=False) as candidate:
            temporary = Path(candidate.name)
            candidate.write(candidate_bytes)
            candidate.flush()
            os.fsync(candidate.fileno())
        checked = load_pilot_config(temporary)
        # Validation resolves relative data paths using this same parent.
        if any(checked.values[key] != value for key, value in original.values.items() if key not in changes):
            raise ValueError('새 화면 외의 설정이 변경되어 적용하지 않았습니다.')
        if not apply:
            return result
        # Detect an operator edit while the proposed change was being prepared.
        load_pilot_config(source)
        current = source.stat()
        if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) != (
            info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size
        ) or source.read_bytes() != raw:
            raise ValueError('설정 파일이 변경되었습니다. 다시 확인하고 실행하세요.')
        with tempfile.NamedTemporaryFile(dir=source.parent, prefix=source.name + '.ui-backup-', delete=False) as backup:
            backup.write(raw)
            backup.flush()
            os.fsync(backup.fileno())
            result['backup'] = backup.name
        os.chmod(temporary, info.st_mode & 0o777)
        os.replace(temporary, source)
        temporary = None
        return {**result, 'applied': True}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='pilot/portal.https.local')
    parser.add_argument('--courses', nargs='+', required=True, help='허용할 실제 course_key (예: come2201 come3105)')
    parser.add_argument('--apply', action='store_true', help='백업 후 검증된 변경을 적용합니다. 생략하면 미리보기만 합니다.')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(configure(Path(args.config), args.courses, apply=args.apply), ensure_ascii=False))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
