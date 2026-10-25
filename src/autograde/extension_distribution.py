"""Private VSIX distribution. Never extracts or executes uploaded code on server."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
import zlib

from .classroom_commands import ClassroomCommands
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, utc_iso

MAX_VSIX_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_COURSE_BYTES = 1024 * 1024 * 1024
MAX_COURSE_RELEASES = 200
_RELEASE = re.compile(r'er_[0-9a-f]{32}\Z')
_PART = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{0,95}\Z')
_NUMBER = r'(?:0|[1-9][0-9]*)'
_PRE = r'(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)'
_SEMVER = _NUMBER + r'\.' + _NUMBER + r'\.' + _NUMBER + r'(?:-' + _PRE + r'(?:\.' + _PRE + r')*)?'
_VERSION = re.compile(_SEMVER + r'\Z')
_COMPARATOR = re.compile(r'(?:\^|~|>=|>|<=|<|=)?' + _SEMVER + r'\Z')
_MAX_SAFE_INTEGER = 9007199254740991
_TARGETS = {'universal', 'darwin-x64', 'darwin-arm64', 'linux-x64', 'linux-arm64', 'win32-x64', 'win32-arm64'}
_CODES = {
    'offered': {'approval_required'}, 'declined': {'student_declined'},
    'installing': {'installation_started'}, 'reload_required': {'reload_required'},
    'installed': {'version_verified'},
    'failed': {'unsupported_client', 'untrusted_workspace', 'restricted_policy', 'incompatible_platform',
               'incompatible_engine', 'download_failed', 'hash_mismatch', 'install_failed', 'scope_changed',
               'package_invalid', 'newer_version_installed', 'built_in_extension'},
}


class ExtensionStorageError(RuntimeError):
    pass


def _strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('VSIX 메타데이터에 중복 항목이 있습니다.')
            result[key] = value
        return result
    return json.loads(data.decode('utf-8'), object_pairs_hook=pairs)


def _valid_version(value):
    if not isinstance(value, str) or len(value) > 96 or not _VERSION.fullmatch(value):
        return False
    return all(int(component) <= _MAX_SAFE_INTEGER for component in value.split('-', 1)[0].split('.'))


def _valid_comparator(value):
    version = value.lstrip('^~<>=')
    if not _COMPARATOR.fullmatch(value) or not _valid_version(version):
        return False
    parts = [int(part) for part in version.split('-', 1)[0].split('.')]
    # npm semver expands caret/tilde ranges by incrementing a component. Its
    # implicit upper bound must also fit a JavaScript safe integer.
    increment = 1 if value.startswith('~') else (0 if parts[0] else 1 if parts[1] else 2)
    return not value.startswith(('^', '~')) or parts[increment] < _MAX_SAFE_INTEGER


def _engine_range(value):
    """Accept the common, explicitly supported npm semver range subset.

    Avoid accepting arbitrary letters/invalid ranges which would poison the
    whole student's catalog. Complex partial-version ranges need normalization
    by the extension publisher before uploading this pilot package.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return False
    for clause in value.split('||'):
        tokens = clause.split()
        if tokens == ['*']:
            continue
        if len(tokens) == 3 and tokens[1] == '-':
            if _valid_version(tokens[0]) and _valid_version(tokens[2]):
                continue
            return False
        if not tokens or not all(_valid_comparator(token) for token in tokens):
            return False
    return True


def inspect_vsix(data, filename):
    if not isinstance(data, bytes) or not data or len(data) > MAX_VSIX_BYTES:
        raise ValueError('VSIX 파일은 1바이트 이상 64 MiB 이하여야 합니다.')
    if (not isinstance(filename, str) or len(filename) > 255 or not filename.lower().endswith('.vsix')
            or any(c in filename for c in '/\\') or any(ord(c) < 32 or ord(c) == 127 for c in filename)):
        raise ValueError('경로가 없는 .vsix 파일을 선택하세요.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if not 1 <= len(members) <= 20000:
                raise ValueError('VSIX 내부 파일 수 제한을 초과했습니다.')
            names, file_names, total = set(), set(), 0
            for item in members:
                name = item.filename.rstrip('/') if item.is_dir() else item.filename
                parts = PurePosixPath(name).parts
                canonical = unicodedata.normalize('NFC', name).casefold()
                if (not name or len(name.encode('utf-8')) > 512 or '\\' in name or ':' in name
                        or name.startswith('/') or any(ord(c) < 32 or ord(c) == 127 for c in name)
                        or any(part in {'.', '..'} or part.endswith((' ', '.')) for part in name.split('/'))
                        or canonical in names or str(PurePosixPath(name)) != name
                        or any(re.fullmatch(r'(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', p, re.I) for p in parts)):
                    raise ValueError('VSIX 내부 경로가 안전하지 않거나 중복됩니다.')
                names.add(canonical)
                if not item.is_dir():
                    file_names.add(canonical)
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                    raise ValueError('VSIX에 링크 또는 특수 파일을 넣을 수 없습니다.')
                if item.flag_bits & 1 or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise ValueError('암호화되었거나 지원하지 않는 VSIX 압축입니다.')
                total += item.file_size
                if total > MAX_EXPANDED_BYTES or item.file_size > MAX_EXPANDED_BYTES:
                    raise ValueError('VSIX 압축 해제 크기는 256 MiB 이하여야 합니다.')
                # Stream each member to validate CRC, actual size and truncated input.
                count = 0
                with archive.open(item) as stream:
                    while chunk := stream.read(1024 * 1024):
                        count += len(chunk)
                        if count > item.file_size or count > MAX_EXPANDED_BYTES:
                            raise ValueError('VSIX 파일 크기가 올바르지 않습니다.')
                if count != item.file_size:
                    raise ValueError('VSIX 파일 크기가 올바르지 않습니다.')
            if any(str(parent) in file_names for name in names for parent in PurePosixPath(name).parents if str(parent) != '.'):
                raise ValueError('VSIX 파일과 폴더 경로가 충돌합니다.')
            for name in ('extension/package.json', 'extension.vsixmanifest'):
                if archive.getinfo(name).file_size > 1024 * 1024:
                    raise ValueError('VSIX 메타데이터가 너무 큽니다.')
            package = _strict_json(archive.read('extension/package.json'))
            xml = archive.read('extension.vsixmanifest')
            if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
                raise ValueError('VSIX XML 선언을 확인하세요.')
            document = ET.fromstring(xml)
            identities = [node for node in document.iter() if node.tag.split('}')[-1] == 'Identity']
            if len(identities) != 1 or not isinstance(package, dict):
                raise ValueError('VSIX 확장 식별자를 확인하세요.')
            identity = identities[0].attrib
            publisher, name, version = (package.get(field) for field in ('publisher', 'name', 'version'))
            if (not all(isinstance(v, str) and _PART.fullmatch(v) for v in (publisher, name))
                    or not _valid_version(version)
                    or identity.get('Publisher') != publisher or identity.get('Id') != name
                    or identity.get('Version') != version):
                raise ValueError('VSIX manifest와 package.json의 확장 이름·버전이 일치해야 합니다.')
            for field in ('extensionDependencies', 'extensionPack'):
                if field in package and package[field] != []:
                    raise ValueError('다른 확장을 자동 설치하는 dependencies·extensionPack은 지원하지 않습니다.')
            target = identity.get('TargetPlatform', 'universal')
            if target not in _TARGETS:
                raise ValueError('지원하지 않는 VSIX 실행 플랫폼입니다.')
            engine = package.get('engines', {}).get('vscode') if isinstance(package.get('engines'), dict) else None
            if not _engine_range(engine):
                raise ValueError('VS Code 버전 범위는 ^1.80.0, >=1.80.0 <2.0.0 또는 * 형식으로 지정하세요.')
            metadata = package.get('__metadata')
            if isinstance(metadata, dict) and metadata.get('targetPlatform', target) != target:
                raise ValueError('VSIX 실행 플랫폼 메타데이터가 일치하지 않습니다.')
    except (zipfile.BadZipFile, KeyError, UnicodeError, json.JSONDecodeError, ET.ParseError, RuntimeError, zlib.error, EOFError) as exc:
        raise ValueError('VSIX 구조 또는 압축 파일이 올바르지 않습니다.') from exc
    return dict(extension_id=(publisher + '.' + name).lower(), version=version, target_platform=target,
                vscode_engine=engine, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data), filename=filename)


class ExtensionDistribution:
    def __init__(self, state, root):
        self.state, self.root = state, Path(root).absolute()

    @staticmethod
    def _course(db, course_key, *, writable=False):
        row = db.execute('SELECT status FROM admin_courses WHERE course_key=?', (course_key,)).fetchone()
        if not row:
            raise PlatformNotFound('교과목을 찾을 수 없습니다.')
        if writable and row['status'] != 'active':
            raise PlatformAccessDenied('운영 중인 수업에서만 확장을 배포할 수 있습니다.')

    def _path(self, digest, *, create=False):
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ExtensionStorageError('Invalid package digest')
        try:
            for folder in reversed((self.root, *self.root.parents)):
                if not folder.exists() and create:
                    folder.mkdir(mode=0o700)
                info = folder.lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise ExtensionStorageError('Unsafe extension storage')
        except OSError as exc:
            raise ExtensionStorageError('Extension storage unavailable') from exc
        return self.root / (digest + '.vsix')

    def _verify_file(self, row):
        target = self._path(row['sha256'])
        try:
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
            with os.fdopen(os.open(target, flags), 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != row['size_bytes']:
                    raise ExtensionStorageError('Extension package changed')
                digest = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                if digest.hexdigest() != row['sha256']:
                    raise ExtensionStorageError('Extension package checksum mismatch')
        except OSError as exc:
            raise ExtensionStorageError('Extension package unavailable') from exc
        return target

    def upload(self, course_key, data, filename, actor, *, reauthorize=None):
        metadata = inspect_vsix(data, filename)
        if reauthorize:
            reauthorize()
        now, release_id = utc_iso(), 'er_' + secrets.token_hex(16)
        with self.state._write() as db:
            self._course(db, course_key, writable=True)
            existing = db.execute('SELECT * FROM ide_extension_releases WHERE course_key=? AND extension_id=? AND version=? AND target_platform=?',
                (course_key, metadata['extension_id'], metadata['version'], metadata['target_platform'])).fetchone()
            if existing:
                if existing['sha256'] != metadata['sha256']:
                    raise PlatformConflict('같은 확장 버전의 파일을 교체할 수 없습니다. 버전을 올려 등록하세요.')
                self._verify_file(existing)
                return self._public(db, existing)
            counts = db.execute('SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM ide_extension_releases WHERE course_key=?', (course_key,)).fetchone()
            if counts[0] >= MAX_COURSE_RELEASES or counts[1] + len(data) > MAX_COURSE_BYTES:
                raise PlatformConflict('수업별 확장 보관 한도(200개·1 GiB)를 초과했습니다.')
            target = self._path(metadata['sha256'], create=True)
            temporary = self.root / ('.upload-' + secrets.token_hex(16))
            try:
                with temporary.open('xb') as stream:
                    os.chmod(temporary, 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    # Atomic immutable publication: interrupted writes never leave
                    # a partial hash-named artifact that could be served later.
                    os.link(temporary, target)
                except FileExistsError:
                    self._verify_file(metadata)
            except OSError as exc:
                raise ExtensionStorageError('Extension package could not be stored') from exc
            finally:
                temporary.unlink(missing_ok=True)
            if reauthorize:
                reauthorize()
            db.execute('''INSERT INTO ide_extension_releases(release_id,course_key,extension_id,version,target_platform,vscode_engine,sha256,size_bytes,filename,created_at,actor)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (release_id, course_key, metadata['extension_id'], metadata['version'],
                metadata['target_platform'], metadata['vscode_engine'], metadata['sha256'], len(data), filename, now, actor))
            db.execute("INSERT INTO ide_extension_audit(release_id,action,actor,recorded_at) VALUES (?,'uploaded',?,?)", (release_id, actor, now))
            return self._public(db, db.execute('SELECT * FROM ide_extension_releases WHERE release_id=?', (release_id,)).fetchone())

    @staticmethod
    def _release(db, course_key, release_id):
        if not isinstance(release_id, str) or not _RELEASE.fullmatch(release_id):
            raise PlatformNotFound('확장 배포본을 찾을 수 없습니다.')
        row = db.execute('SELECT * FROM ide_extension_releases WHERE course_key=? AND release_id=?', (course_key, release_id)).fetchone()
        if not row:
            raise PlatformNotFound('확장 배포본을 찾을 수 없습니다.')
        return row

    @staticmethod
    def _public(db, row):
        result = dict(row)
        desired = db.execute('SELECT published_at FROM ide_extension_desired WHERE release_id=?', (row['release_id'],)).fetchone()
        published = db.execute("SELECT MAX(recorded_at) FROM ide_extension_audit WHERE release_id=? AND action='published'", (row['release_id'],)).fetchone()[0]
        result.update(status='published' if desired else 'withdrawn' if published else 'draft',
                      published_at=desired['published_at'] if desired else published)
        return result

    def publish(self, course_key, release_id, actor, *, reauthorize=None):
        if reauthorize:
            reauthorize()
        with self.state._write() as db:
            self._course(db, course_key, writable=True)
            row = self._release(db, course_key, release_id)
            self._verify_file(row)
            if reauthorize:
                reauthorize()
            previous = db.execute('SELECT release_id FROM ide_extension_desired WHERE course_key=? AND extension_id=? AND target_platform=?',
                (course_key, row['extension_id'], row['target_platform'])).fetchone()
            if previous and previous['release_id'] == release_id:
                return self._public(db, row)
            now = utc_iso()
            if previous:
                db.execute("INSERT INTO ide_extension_audit(release_id,action,actor,recorded_at) VALUES (?,'superseded',?,?)", (previous['release_id'], actor, now))
            db.execute('''INSERT INTO ide_extension_desired(course_key,extension_id,target_platform,release_id,published_at)
                VALUES (?,?,?,?,?) ON CONFLICT(course_key,extension_id,target_platform) DO UPDATE SET release_id=excluded.release_id,published_at=excluded.published_at''',
                (course_key, row['extension_id'], row['target_platform'], release_id, now))
            db.execute("INSERT INTO ide_extension_audit(release_id,action,actor,recorded_at) VALUES (?,'published',?,?)", (release_id, actor, now))
            return self._public(db, row)

    def withdraw(self, course_key, release_id, actor, *, reauthorize=None):
        if reauthorize:
            reauthorize()
        with self.state._write() as db:
            self._course(db, course_key, writable=True)
            row = self._release(db, course_key, release_id)
            if db.execute('DELETE FROM ide_extension_desired WHERE course_key=? AND release_id=?', (course_key, release_id)).rowcount:
                db.execute("INSERT INTO ide_extension_audit(release_id,action,actor,recorded_at) VALUES (?,'withdrawn',?,?)", (release_id, actor, utc_iso()))
            return self._public(db, row)

    def list_for_instructor(self, course_key):
        with self.state._connection() as db:
            self._course(db, course_key)
            return [self._public(db, row) for row in db.execute('SELECT * FROM ide_extension_releases WHERE course_key=? ORDER BY created_at DESC,release_id', (course_key,))]

    def list_for_student(self, course_key):
        with self.state._connection() as db:
            self._course(db, course_key, writable=True)
            fields = ('release_id', 'extension_id', 'version', 'target_platform', 'vscode_engine', 'sha256', 'size_bytes', 'filename', 'published_at')
            return [{key: row[key] for key in fields} for row in db.execute('''SELECT r.*,d.published_at FROM ide_extension_releases r
                JOIN ide_extension_desired d ON d.release_id=r.release_id WHERE r.course_key=? ORDER BY r.extension_id,r.target_platform''', (course_key,))]

    def package_for_student(self, course_key, release_id):
        with self.state._connection() as db:
            self._course(db, course_key, writable=True)
            row = self._release(db, course_key, release_id)
            if not db.execute('SELECT 1 FROM ide_extension_desired WHERE course_key=? AND release_id=?', (course_key, release_id)).fetchone():
                raise PlatformNotFound('현재 배포 중인 확장이 아닙니다.')
            target = self._verify_file(row)
            return target, dict(row)

    def report(self, *, session, access_token_hash, release_id, payload, at=None):
        if not isinstance(payload, dict) or set(payload) - {'client_session_id', 'status', 'result_code', 'installed_version', 'platform', 'architecture'}:
            raise ValueError('확장 설치 보고 형식을 확인하세요.')
        status, code, installed = payload.get('status'), payload.get('result_code'), payload.get('installed_version')
        if not isinstance(status, str) or not isinstance(code, str) or code not in _CODES.get(status, set()):
            raise ValueError('확장 설치 상태를 확인하세요.')
        if installed is not None and not _valid_version(installed):
            raise ValueError('설치된 확장 버전을 확인하세요.')
        for key, allowed in [('platform', {'darwin', 'linux', 'win32'}), ('architecture', {'x64', 'arm64'})]:
            if payload.get(key) is not None and (not isinstance(payload[key], str) or payload[key] not in allowed):
                raise ValueError('확장 설치 환경을 확인하세요.')
        now = utc_iso(at)
        with self.state._write() as db:
            ClassroomCommands(self.state)._student(db, session, access_token_hash, payload.get('client_session_id'), now)
            row = self._release(db, session.course_key, release_id)
            if not db.execute('SELECT 1 FROM ide_extension_desired WHERE course_key=? AND release_id=?', (session.course_key, release_id)).fetchone():
                raise PlatformConflict('확장 배포가 변경되었습니다. 최신 목록을 확인하세요.')
            if status == 'installed' and installed != row['version']:
                raise ValueError('설치 완료 보고에는 배포 버전과 같은 실행 버전이 필요합니다.')
            db.execute('''INSERT INTO ide_extension_client_status(release_id,client_session_id,status,result_code,installed_version,platform,architecture,reported_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(release_id,client_session_id) DO UPDATE SET
                status=excluded.status,result_code=excluded.result_code,installed_version=excluded.installed_version,
                platform=excluded.platform,architecture=excluded.architecture,reported_at=excluded.reported_at''',
                (release_id, payload['client_session_id'], status, code, installed, payload.get('platform'), payload.get('architecture'), now))
        return {'accepted': True, 'status': status, 'reported_at': now, 'client_reported': True}

    def status_for_instructor(self, course_key):
        with self.state._connection() as db:
            self._course(db, course_key)
            return [dict(row) for row in db.execute('''SELECT s.*,c.student_id,p.student_key,COALESCE(profile.name,'') student_name,
                r.extension_id,r.version FROM ide_extension_client_status s
                JOIN ide_extension_releases r ON r.release_id=s.release_id
                JOIN classroom_clients c ON c.client_session_id=s.client_session_id
                JOIN platform_students p ON p.id=c.student_id
                LEFT JOIN admin_student_profiles profile ON profile.student_id=p.id
                WHERE r.course_key=? ORDER BY s.reported_at DESC LIMIT 1000''', (course_key,))]
