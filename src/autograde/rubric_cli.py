"""Offline rubric operator CLI; never uses the live student platform database."""
import argparse
import json
import sqlite3
import sys

from .management_ses import load_json
from .rubric_composition import compile_profile
from .rubric_engine import RubricError, content_hash, validate_rubric
from .rubric_store import RubricStore


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline rubric evaluation; imported evidence is unverified and results are not published.')
    commands = parser.add_subparsers(dest='command', required=True)
    validate = commands.add_parser('validate')
    validate.add_argument('--rubric', required=True)
    preview = commands.add_parser('compose')
    preview.add_argument('--profile', required=True)
    commands.add_parser('init').add_argument('--database', required=True)
    register = commands.add_parser('register')
    register.add_argument('--database', required=True)
    register.add_argument('--rubric', required=True)
    register.add_argument('--actor', required=True)
    for name in ('approve', 'evaluate'):
        command = commands.add_parser(name)
        command.add_argument('--database', required=True)
        command.add_argument('--course', required=True)
        command.add_argument('--rubric-id', required=True)
        command.add_argument('--version', required=True, type=int)
        command.add_argument('--actor', required=True)
        if name == 'approve':
            command.add_argument('--digest', required=True)
        else:
            command.add_argument('--profile', required=True)
            command.add_argument('--evidence', required=True)
            command.add_argument('--request-key', required=True)
    get = commands.add_parser('result')
    get.add_argument('--database', required=True)
    get.add_argument('--course', required=True)
    get.add_argument('--assessment-id', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'validate':
            rubric = validate_rubric(load_json(args.rubric))
            result = {'valid': True, 'digest': content_hash(rubric), 'rubric': rubric}
        elif args.command == 'compose':
            result = compile_profile(load_json(args.profile))
        else:
            store = RubricStore(args.database)
            if args.command == 'init':
                result = store.initialize()
            elif args.command == 'register':
                result = store.register(load_json(args.rubric), args.actor)
            elif args.command == 'approve':
                result = store.approve(args.course, args.rubric_id, args.version, expected_digest=args.digest, actor=args.actor)
            elif args.command == 'evaluate':
                result = store.assess(args.course, args.rubric_id, args.version, profile=load_json(args.profile),
                                      evidence=load_json(args.evidence), request_key=args.request_key, actor=args.actor)
            else:
                result = store.get_assessment(args.course, args.assessment_id)
        print(json.dumps({'ok': True, 'mode': 'offline_operator', 'result': result}, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, sqlite3.Error, RecursionError) as exc:
        message = str(exc) if isinstance(exc, RubricError) else 'cannot process input or dedicated rubric database'
        print(json.dumps({'ok': False, 'error': 'rubric_operation_failed', 'message': message}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
