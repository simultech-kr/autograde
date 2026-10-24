"""Independent ZIP reader and shared-session cleanup interoperability checks."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
NODE = ROOT / ".cache/node-v24.18.0-darwin-arm64/bin/node"


def test_exports_survive_shared_session_cleanup_and_open_as_standard_zip(tmp_path):
    node = str(NODE) if NODE.exists() else shutil.which("node")
    compiled = ROOT / "extensions/vscode/out/learningExport.js"
    if not node or not compiled.exists():
        pytest.skip("Compile the VS Code extension and provide Node.js first")
    fixture = r"""
const fs = require('node:fs/promises');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const assert = require('node:assert/strict');
(async () => {
  let input = ''; for await (const chunk of process.stdin) input += chunk;
  const config = JSON.parse(input);
  const engine = require(config.compiled);
  const session = await import(pathToFileURL(config.launcher));
  const base = path.join(config.temp, 'sessions'), output = path.join(config.temp, 'exports');
  await fs.mkdir(base); await fs.mkdir(output);
  const legacy = path.join(base, 'l-' + 'a'.repeat(16)); await fs.mkdir(legacy);
  await fs.writeFile(path.join(legacy, 'preserve.txt'), 'existing personal work');
  const layout = session.sharedSessionLayout(base, 'synthetic-seat');
  session.prepareSharedSession(layout, {});
  const code = path.join(layout.workspace, 'assignment'); await fs.mkdir(code);
  await fs.writeFile(path.join(code, 'main.cpp'), '#include <iostream>\nint main() { std::cout << "hello"; }\n');
  await fs.writeFile(path.join(code, '학습노트.txt'), '반복자 패턴\n');
  await fs.writeFile(path.join(code, '.env'), 'SYNTHETIC_PRIVATE_VALUE=must_not_export');
  await fs.mkdir(path.join(code, 'build')); await fs.writeFile(path.join(code, 'build', 'generated.o'), 'generated');
  const collected = await engine.collectLearningCode(code, () => {});
  const groups = [
    {directory: 'current-code', title: '현재 코드', files: collected.files},
    {directory: 'submissions/version-1', title: '이전 제출본', files: [{path: 'main.cpp', bytes: Buffer.from('int main() { return 0; }\n')}]},
    {directory: 'materials/lecture-1', title: '강의자료', files: [{path: '설명.txt', bytes: Buffer.from('강의자료 복사본\n')}]},
  ];
  const options = {assertCurrent() {}, excludedDestinations: [layout.root, code]};
  await assert.rejects(engine.saveLearningExport(layout.materials, 'zip', groups, options));
  const archive = await engine.saveLearningExport(output, 'zip', groups, options);
  const folder = await engine.saveLearningExport(output, 'folder', groups, options);
  // Cleanup must unlink a session's outbound symlink, never remove the exported target.
  await fs.symlink(output, path.join(layout.materials, 'export-pointer'));
  await fs.mkdir(path.join(layout.runtime, 'user-data', 'Backups'));
  await fs.writeFile(path.join(layout.runtime, 'user-data', 'Backups', 'draft'), 'private unsaved draft');
  session.cleanupSharedSession(layout);
  await assert.rejects(fs.stat(layout.root), {code: 'ENOENT'});
  assert.equal(await fs.readFile(path.join(legacy, 'preserve.txt'), 'utf8'), 'existing personal work');
  const next = session.sharedSessionLayout(base, 'synthetic-seat');
  session.prepareSharedSession(next, {});
  assert.notEqual(next.root, layout.root);
  assert.deepEqual(await fs.readdir(next.workspace), []);
  session.cleanupSharedSession(next);
  process.stdout.write(JSON.stringify({archive: archive.destination, folder: folder.destination, skipped: collected.skipped}));
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
"""
    completed = subprocess.run(
        [node, "-e", fixture],
        input=json.dumps({
            "compiled": str(compiled),
            "launcher": str(ROOT / "ide/code-oss/shared-session.mjs"),
            "temp": str(tmp_path.resolve()),
        }),
        text=True, capture_output=True, check=True, timeout=30,
    )
    result = json.loads(completed.stdout)
    assert result["skipped"] >= 2
    expected = {
        "current-code/main.cpp": b'#include <iostream>\nint main() { std::cout << "hello"; }\n',
        "current-code/학습노트.txt": "반복자 패턴\n".encode(),
        "submissions/version-1/main.cpp": b"int main() { return 0; }\n",
        "materials/lecture-1/설명.txt": "강의자료 복사본\n".encode(),
    }
    with zipfile.ZipFile(result["archive"]) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {*expected, "EXPORT-INFO.json"}
        manifest = json.loads(archive.read("EXPORT-INFO.json"))
        assert str(tmp_path) not in json.dumps(manifest)
        for filename, content in expected.items():
            assert archive.read(filename) == content
            assert (Path(result["folder"]) / filename).read_bytes() == content
        for entry in manifest["files"]:
            assert entry["sha256"] == hashlib.sha256(expected[entry["path"]]).hexdigest()
