"""Trusted server-terminal provisioning; passwords never appear in argv or output."""
import argparse
import getpass
import json
import sqlite3
import warnings

from .course_admin import CourseAdminService
from .instructor_identity import InstructorIdentityStore
from .pilot_config import load_pilot_config
from .platform_state import PlatformStateStore
from .settings import AppPaths


def _password():
    # getpass's echoing fallback is unsuitable for provisioning credentials.
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        first = getpass.getpass('교수자 비밀번호 (12자 이상): ')
        second = getpass.getpass('비밀번호 다시 입력: ')
    if first != second:
        raise ValueError('입력한 비밀번호가 서로 다릅니다.')
    return first


def main(argv=None):
    parser = argparse.ArgumentParser(description='교수자 개인 계정 관리: 신뢰할 수 있는 서버 터미널에서만 실행')
    parser.add_argument('--config', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init')
    commands.add_parser('list')
    create = commands.add_parser('create')
    create.add_argument('--username', required=True)
    create.add_argument('--name', required=True)
    create.add_argument('--role', choices=('admin', 'instructor'), default='instructor')
    for name in ('password', 'enable', 'disable', 'grant', 'revoke'):
        sub = commands.add_parser(name)
        sub.add_argument('--username', required=True)
        if name in {'grant', 'revoke'}:
            sub.add_argument('--course', required=True)
    args = parser.parse_args(argv)
    try:
        config = load_pilot_config(args.config)
        paths = AppPaths.from_value(config.values['data_root']).ensure()
        store = InstructorIdentityStore(paths.root / 'instructor-identities.sqlite3')
        result = {'action': args.command}
        if args.command == 'init':
            store.initialize()
        elif args.command == 'create':
            result['user_id'] = store.create(args.username, args.name, args.role, _password())
        elif args.command == 'password':
            store.set_password(args.username, _password())
        elif args.command in {'enable', 'disable'}:
            store.set_active(args.username, args.command == 'enable')
        elif args.command in {'grant', 'revoke'}:
            if args.command == 'grant':
                if not paths.database.is_file():
                    raise ValueError('먼저 수업을 초기화하거나 등록하세요.')
                CourseAdminService(PlatformStateStore(paths.database)).get_course(args.course)
            store.set_grant(args.username, args.course, allowed=args.command == 'grant')
        else:
            result['accounts'] = store.list_accounts()
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
        return 0
    except (ValueError, RuntimeError, OSError, sqlite3.Error, EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print(json.dumps({'ok': False, 'error': 'instructor_account_operation_failed',
                          'message': '계정·수업·비밀번호 조건과 전용 DB를 확인하세요. 기존 DB는 삭제하지 마세요. 비밀번호는 대화형 터미널에서 입력해야 합니다.'}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
