import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { parseOptions, readBuildSource, sha256 } from './build-runtime.mjs';
import { verifyReports, buildTargets } from './finalize-artifacts.mjs';
import { legacyOutputPrefix } from './build-resume.mjs';

function temporaryDirectory(t, prefix) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

function repository(t) {
  const directory = temporaryDirectory(t, 'autograde-local-build-');
  const git = (...args) => execFileSync('git', ['-C', directory, ...args], { encoding: 'utf8' });
  git('init', '--quiet');
  git('config', 'user.name', 'Autograde Fixture');
  git('config', 'user.email', 'fixture@example.invalid');
  git('config', 'commit.gpgsign', 'false');
  git('config', 'core.autocrlf', 'false');
  git('config', 'core.hooksPath', path.join(directory, '.git', 'empty-hooks'));
  fs.writeFileSync(path.join(directory, 'tracked.txt'), 'committed contents\n');
  fs.writeFileSync(path.join(directory, '.gitignore'), 'ignored-output/\n');
  git('add', '.');
  git('commit', '--quiet', '-m', 'fixture');
  return { directory, git, revision: git('rev-parse', 'HEAD').trim() };
}

function snapshot({ directory, git }) {
  const files = Object.fromEntries(fs.readdirSync(directory, { recursive: true, withFileTypes: true })
    .filter(entry => entry.isFile() && !path.relative(directory, path.join(entry.parentPath, entry.name)).split(path.sep).includes('.git'))
    .map(entry => {
      const file = path.join(entry.parentPath, entry.name);
      return [path.relative(directory, file), fs.readFileSync(file).toString('base64')];
    }));
  return { files, revision: git('rev-parse', 'HEAD'), status: git('status', '--porcelain=v1', '--untracked-files=all'),
    index: git('ls-files', '--stage'), staged: git('diff', '--cached', '--binary'), unstaged: git('diff', '--binary'),
    stashes: git('stash', 'list') };
}

const requiredOptions = ['--target', 'linux-x64', '--source', '/new/source', '--output', '/new/output'];

test('candidate remains the CLI default and local mode composes with optional WSL packaging', () => {
  assert.deepEqual(parseOptions(requiredOptions), {
    target: 'linux-x64', source: '/new/source', output: '/new/output', includeWsl: false, local: false, resume: false,
  });
  assert.deepEqual(parseOptions(['--local', ...requiredOptions, '--include-wsl']), {
    target: 'linux-x64', source: '/new/source', output: '/new/output', includeWsl: true, local: true, resume: false,
  });
  assert.throws(() => parseOptions([...requiredOptions, '--local', '--local']), /Usage/);
  assert.throws(() => parseOptions([...requiredOptions, '--local', 'true']), /Usage/);
});

test('a clean committed checkout produces candidate provenance from its actual HEAD', t => {
  const f = repository(t), before = snapshot(f);
  assert.deepEqual(readBuildSource(f.directory), {
    sourceRevision: f.revision, sourceDirty: false, status: 'release-candidate', buildMode: 'candidate',
  });
  assert.deepEqual(snapshot(f), before);
});

const changes = {
  tracked: f => fs.writeFileSync(path.join(f.directory, 'tracked.txt'), 'uncommitted working contents\n'),
  staged: f => {
    fs.writeFileSync(path.join(f.directory, 'tracked.txt'), 'staged contents\n');
    f.git('add', 'tracked.txt');
  },
  untracked: f => {
    fs.mkdirSync(path.join(f.directory, 'new source'));
    fs.writeFileSync(path.join(f.directory, 'new source', 'lesson.txt'), 'untracked lesson\n');
  },
};

for (const [kind, change] of Object.entries(changes)) {
  test(`candidate rejects ${kind} changes with actionable local-mode guidance and preserves the checkout`, t => {
    const f = repository(t);
    change(f);
    const before = snapshot(f);
    assert.throws(() => readBuildSource(f.directory), error => {
      assert.match(error.message, /clean committed checkout/);
      assert.match(error.message, /--local/);
      return true;
    });
    assert.deepEqual(snapshot(f), before);
  });

  test(`local mode accepts ${kind} changes and records dirty provenance without changing source or index`, t => {
    const f = repository(t);
    change(f);
    const before = snapshot(f);
    assert.deepEqual(readBuildSource(f.directory, { local: true }), {
      sourceRevision: f.revision, sourceDirty: true, status: 'local-build', buildMode: 'local',
    });
    assert.deepEqual(snapshot(f), before);
  });
}

test('local mode preserves distinct staged and unstaged versions alongside untracked files', t => {
  const f = repository(t);
  changes.staged(f);
  changes.tracked(f);
  changes.untracked(f);
  const before = snapshot(f);
  assert.equal(readBuildSource(f.directory, { local: true }).sourceDirty, true);
  assert.deepEqual(snapshot(f), before);
});

test('clean local builds remain local and each provenance read reflects the current checkout', t => {
  const f = repository(t);
  assert.deepEqual(readBuildSource(f.directory, { local: true }), {
    sourceRevision: f.revision, sourceDirty: false, status: 'local-build', buildMode: 'local',
  });
  changes.tracked(f);
  assert.equal(readBuildSource(f.directory, { local: true }).sourceDirty, true);
  f.git('add', 'tracked.txt');
  f.git('commit', '--quiet', '-m', 'updated fixture');
  const revision = f.git('rev-parse', 'HEAD').trim();
  assert.notEqual(revision, f.revision);
  assert.deepEqual(readBuildSource(f.directory, { local: true }), {
    sourceRevision: revision, sourceDirty: false, status: 'local-build', buildMode: 'local',
  });
});

test('ignored build output does not mark committed source dirty', t => {
  const f = repository(t);
  fs.mkdirSync(path.join(f.directory, 'ignored-output'));
  fs.writeFileSync(path.join(f.directory, 'ignored-output', 'build.log'), 'generated output\n');
  assert.equal(readBuildSource(f.directory).sourceDirty, false);
});

function inventory(t) {
  const directory = temporaryDirectory(t, 'autograde-local-inventory-');
  const expected = { sourceRevision: 'a'.repeat(40), extensionVersion: '0.5.14', upstream: {
    repository: 'https://github.com/microsoft/vscode.git', tag: '1.143.0', commit: 'b'.repeat(40), node: '26.13.0', sourceNode: '24.18.0',
  } };
  for (const [target, definition] of Object.entries(buildTargets)) {
    if (target === 'windows-launcher') continue;
    const folder = path.join(directory, target);
    fs.mkdirSync(folder);
    const report = { schema: 1, status: 'release-candidate', buildMode: 'candidate', sourceDirty: false,
      buildTarget: target, ...expected, includeWsl: false,
      host: { platform: definition.platform, architecture: definition.architecture, node: expected.upstream.node },
      checks: definition.checks.map(name => ({ name, result: 'passed' })),
      acceptance: { productionReady: false, institution: 'not-tested', windowsWsl2: 'not-tested' }, artifacts: [] };
    for (const artifactTarget of definition.artifacts) {
      const file = `Autograde-IDE-${expected.extensionVersion}-${artifactTarget}.zip`, archive = path.join(folder, file);
      fs.writeFileSync(archive, `fixture archive for ${artifactTarget}`);
      const digest = sha256(archive);
      fs.writeFileSync(`${archive}.sha256`, `${digest}  ${file}\n`);
      report.artifacts.push({ target: artifactTarget, file, bytes: fs.statSync(archive).size, sha256: digest });
    }
    fs.writeFileSync(path.join(folder, `build-${target}.json`), JSON.stringify(report));
  }
  return { directory, expected };
}

for (const localMarker of [{ status: 'local-build' }, { buildMode: 'local' }]) {
  test(`candidate finalization rejects ${JSON.stringify(localMarker)} even from clean committed source`, t => {
    const { directory, expected } = inventory(t);
    assert.equal(verifyReports(directory, expected).length, 3);
    const file = path.join(directory, 'linux-x64', 'build-linux-x64.json');
    const report = { ...JSON.parse(fs.readFileSync(file, 'utf8')), ...localMarker };
    assert.equal(report.sourceDirty, false);
    fs.writeFileSync(file, JSON.stringify(report));
    assert.throws(() => verifyReports(directory, expected), /Local build outputs cannot be finalized/);
  });
}

test('candidate collection excludes preserved legacy reports and nested links without changing the backup', t => {
  const { directory, expected } = inventory(t);
  const backup = fs.mkdtempSync(path.join(directory, legacyOutputPrefix));
  const legacy = path.join(backup, 'output');
  fs.cpSync(path.join(directory, 'linux-x64'), legacy, { recursive: true });
  const duplicate = path.join(legacy, 'build-linux-x64.json');
  const original = fs.readFileSync(duplicate);
  const broken = path.join(legacy, 'build-windows-x64.json');
  fs.writeFileSync(broken, '{interrupted report');
  const outside = temporaryDirectory(t, 'autograde-legacy-outside-');
  fs.writeFileSync(path.join(outside, 'keep.txt'), 'outside the build');
  const link = path.join(legacy, 'partial-runtime-link');
  fs.symlinkSync(outside, link, process.platform === 'win32' ? 'junction' : 'dir');
  const reports = verifyReports(directory, expected);
  assert.equal(reports.length, 3);
  assert.ok(reports.every(({ file }) => !file.includes(legacyOutputPrefix)));
  assert.deepEqual(fs.readFileSync(duplicate), original);
  assert.equal(fs.readFileSync(broken, 'utf8'), '{interrupted report');
  assert.equal(fs.lstatSync(link).isSymbolicLink(), true);
  assert.equal(fs.readFileSync(path.join(outside, 'keep.txt'), 'utf8'), 'outside the build');
});

test('preserved output and its descendants cannot be selected directly or through an alias', t => {
  const { directory, expected } = inventory(t);
  const backup = fs.mkdtempSync(path.join(directory, legacyOutputPrefix));
  const legacy = path.join(backup, 'output');
  fs.mkdirSync(legacy);
  for (const input of [backup, legacy]) {
    assert.throws(() => verifyReports(input, expected), /Preserved legacy output/);
  }
  const aliases = temporaryDirectory(t, 'autograde-legacy-aliases-');
  const alias = path.join(aliases, 'current');
  fs.symlinkSync(legacy, alias, process.platform === 'win32' ? 'junction' : 'dir');
  assert.throws(() => verifyReports(alias, expected), /Preserved legacy output/);
  const inside = path.join(backup, 'alias-to-current');
  fs.symlinkSync(directory, inside, process.platform === 'win32' ? 'junction' : 'dir');
  assert.throws(() => verifyReports(inside, expected), /Preserved legacy output/);
});

test('a link with the reserved backup prefix still fails ordinary candidate input validation', t => {
  const { directory, expected } = inventory(t);
  const outside = temporaryDirectory(t, 'autograde-unowned-backup-');
  const link = path.join(directory, legacyOutputPrefix + 'linked');
  fs.symlinkSync(outside, link, process.platform === 'win32' ? 'junction' : 'dir');
  assert.throws(() => verifyReports(directory, expected), /symbolic links/);
});
