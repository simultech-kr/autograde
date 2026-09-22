"""Import reviewed course exercises through the same instructor authoring service.

The catalog contains private solutions; only starter archives go to students.
Import is resumable and refuses to overwrite instructor edits.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import time

from .assignment_admin import AssignmentAdminService, DOCUMENT_FIELDS, _archive_files, _document, _grading_bundle, _zip_files
from .course_admin import CourseAdminService
from .pilot_config import load_pilot_config
from .platform_state import PlatformConflict, PlatformStateStore
from .settings import AppPaths


def load_workshops(directory):
    root = Path(directory)
    workshops = []
    for folder in sorted(root.glob('problem[0-9][0-9]')):
        document = json.loads((folder / 'assignment.json').read_text(encoding='utf-8'))
        # The published README is generated from description; keep the full problem.
        document['description'] = (folder / 'starter' / 'README.md').read_text(encoding='utf-8').rstrip()
        document = _document(document)
        if document['mode'] != 'direct' or document['language'] != 'cpp':
            raise ValueError('실습 자료는 직접 만들기 C++ 과제이어야 합니다.')
        starter = _archive_files({file.name: file.read_bytes() for file in sorted((folder / 'starter').iterdir()) if file.is_file()})
        _zip_files(starter, 'cpp')
        grading = _archive_files({
            'solution/main.cpp': (folder / 'instructor' / 'solution.cpp').read_bytes(),
            'negative/main.cpp': (folder / 'instructor' / 'negative.cpp').read_bytes(),
            'tests.json': json.dumps({key: document[key] for key in ('tests', 'negative_score')}, ensure_ascii=False, indent=2).encode(),
        })
        _grading_bundle(grading, 'cpp')
        workshops.append(dict(key=folder.name, document=document, starter=starter, grading=grading, folder=folder))
    if not workshops:
        raise ValueError('problem01/assignment.json 형식의 실습 자료를 찾지 못했습니다.')
    return workshops


def register_workshop(admin, course, workshop):
    document = workshop['document']
    # A stable identity prevents repeated runs from creating duplicate exercises.
    draft = admin.create_draft(course, creation_key='come2201-2026f-' + workshop['key'], **document)
    if {key: draft[key] for key in DOCUMENT_FIELDS} != document:
        raise PlatformConflict(f'{workshop["key"]}: 교수가 변경한 초안이므로 덮어쓰지 않습니다.')
    expected = {'starter': hashlib.sha256(workshop['starter']).hexdigest()}
    files, _ = _grading_bundle(workshop['grading'], 'cpp')
    for role in ('solution', 'negative'):
        expected[role] = hashlib.sha256(_archive_files({'main.cpp': files[f'{role}/main.cpp']})).hexdigest()
    uploads = {item['role']: item for item in draft['uploads']}
    if any(item['sha256'] != expected[role] for role, item in uploads.items()):
        raise PlatformConflict(f'{workshop["key"]}: 등록한 파일이 변경되어 덮어쓰지 않습니다.')
    if 'starter' not in uploads:
        draft = admin.upload_zip(course, draft['draft_id'], draft['revision'], 'starter', workshop['starter'])
    if not {'solution', 'negative'} <= uploads.keys():
        draft = admin.import_grading_template(course, draft['draft_id'], draft['revision'], workshop['grading'])
    return draft


def run_catalog(admin, course, workshops, *, validate=False, publish=False, progress=None):
    if publish and not validate:
        raise ValueError('공개 전 검증이 필요합니다.')
    results = []
    for workshop in workshops:
        draft = register_workshop(admin, course, workshop)
        if validate and not draft['published_assignment_id']:
            job = admin.queue_check(course, draft['draft_id'], draft['revision'], trusted_code_confirmed=True)
            # The caller holds offline runtime locks; only catalog-owned work is queued.
            limit = time.monotonic() + 180
            while job['status'] in {'queued', 'running'}:
                if time.monotonic() > limit:
                    raise RuntimeError('과제 검증 제한 시간을 초과했습니다.')
                admin.run_one()
                job = admin.get_check(course, job['job_id'])
            if job['status'] != 'succeeded':
                raise RuntimeError(f'{workshop["key"]} 검증 실패: {json.dumps(job["details"], ensure_ascii=False)}')
        draft = admin.get_draft(course, draft['draft_id'])
        release = admin.state.get_bundle_assignment(draft['published_assignment_id']) if draft['published_assignment_id'] else None
        item = dict(problem=workshop['key'], title=draft['title'], draft_id=draft['draft_id'],
                    assignment_id=draft['published_assignment_id'], due_at=release.due_at if release else draft['due_at'],
                    status=(draft.get('latest_check') or {}).get('status', 'draft'))
        results.append(item)
        if progress:
            progress(item)
    # No course work becomes visible if any of the ten validation jobs failed.
    if publish:
        for item in results:
            draft = admin.get_draft(course, item['draft_id'])
            release = admin.publish(course, draft['draft_id'], draft['revision'])
            item['assignment_id'] = release.assignment_id
            item['due_at'] = release.due_at
            item['status'] = 'inactive' if not release.active else 'published' if release.ready else 'hidden'
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description='COME2201 실습 자료 내보내기·등록·검증')
    parser.add_argument('--catalog', type=Path, required=True)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument('--export', type=Path, help='웹 등록용 starter/grading ZIP 저장 (DB 변경 없음)')
    destination.add_argument('--config', type=Path, help='서버와 동일한 설정 CSV. 서버를 중지하고 실행')
    destination.add_argument('--data-root', type=Path, help='별도 로컬 시험 데이터 폴더')
    parser.add_argument('--course', default='come2201')
    parser.add_argument('--validate', action='store_true', help='검토한 교수자 코드를 로컬 컴파일·실행')
    parser.add_argument('--publish', action='store_true', help='전체 검증 통과 후 학생 공개')
    args = parser.parse_args(argv)
    try:
        workshops = load_workshops(args.catalog)
        if args.export:
            if args.validate or args.publish:
                raise ValueError('ZIP 내보내기는 검증·공개 옵션과 함께 사용할 수 없습니다.')
            args.export.mkdir(parents=True, exist_ok=True)
            for item in workshops:
                folder = args.export / item['key']
                folder.mkdir(exist_ok=True)
                (folder / 'starter.zip').write_bytes(item['starter'])
                (folder / 'grading-private.zip').write_bytes(item['grading'])
                (folder / 'assignment.json').write_text(json.dumps(item['document'], ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(dict(ok=True, exported=len(workshops), directory=str(args.export)), ensure_ascii=False))
            return 0
        if args.publish and not args.validate:
            raise ValueError('--publish에는 --validate가 필요합니다.')
        config = load_pilot_config(args.config) if args.config else None
        if config and config.values['grading_runtime'] != 'pilot-local':
            raise ValueError('이 실습 등록기는 pilot-local 서버에서 사용합니다.')
        paths = AppPaths.from_value(config.values['data_root'] if config else args.data_root).ensure()
        from .platform_cli import _exclusive_course_service_lock
        with ExitStack() as stack:
            stack.enter_context(_exclusive_course_service_lock(paths, 'portal-runtime'))
            stack.enter_context(_exclusive_course_service_lock(paths, args.course))
            state = PlatformStateStore(paths.database)
            courses = CourseAdminService(state)
            courses.get_course(args.course)
            with state._connection() as db:
                if db.execute("SELECT 1 FROM instructor_assignment_jobs WHERE status IN ('queued','running')").fetchone():
                    raise PlatformConflict('기존 검증 작업을 완료한 뒤 등록기를 실행하세요.')
            admin = AssignmentAdminService(state, paths, course_status=courses.get_course)
            items = run_catalog(admin, args.course, workshops, validate=args.validate, publish=args.publish,
                progress=lambda row: print(json.dumps(dict(progress=row), ensure_ascii=False), flush=True))
        print(json.dumps(dict(ok=True, course=args.course, count=len(items), assignments=items), ensure_ascii=False))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps(dict(ok=False, error=type(exc).__name__, message=str(exc)), ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
