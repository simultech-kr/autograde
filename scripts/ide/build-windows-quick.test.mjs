import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {buildWindowsQuick, parseQuickOptions, extensionChecks} from './build-windows-quick.mjs';
import {openBuildJournal} from './build-resume.mjs';
import {verifyReports} from './finalize-artifacts.mjs';
import {windowsWslInstallerFiles, windowsWslSupport} from '../../ide/code-oss/package-windows.mjs';

const read = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const write = (root, relative, contents) => {
  const file = path.join(root, relative);
  fs.mkdirSync(path.dirname(file), {recursive:true});
  fs.writeFileSync(file, contents);
  return file;
};
const lockPath = directory => path.join(path.dirname(directory), `.${path.basename(directory)}.autograde-build.lock`);
const tree = root => Object.fromEntries(fs.readdirSync(root, {recursive:true}).sort()
  .filter(relative => fs.lstatSync(path.join(root, relative)).isFile())
  .map(relative => [relative, fs.readFileSync(path.join(root, relative)).toString('base64')]));

function fixture(t) {
  const directory = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'autograde-windows-quick-')));
  t.after(() => fs.rmSync(directory, {recursive:true, force:true}));
  const repository = path.join(directory, 'repository'), source = path.join(directory, 'code oss'),
    buildOutput = path.join(directory, 'full build'), output = path.join(directory, 'quick build');
  const pin = {repository:'https://example.invalid/code-oss.git', commit:'a'.repeat(40), tag:'1.143.0', node:'26.13.0', sourceNode:'24.18.0'};
  write(repository, 'ide/code-oss/upstream.json', JSON.stringify(pin));
  write(repository, 'ide/code-oss/windows/launcher.cs', '// original launcher\n');
  write(repository, 'extensions/vscode/package.json', JSON.stringify({version:'0.6.0'}));
  write(repository, 'extensions/vscode/src/extension.ts', '// original extension\n');
  write(repository, 'scripts/ide/build.mjs', '// original build script\n');
  const git = args => execFileSync('git', ['-C', repository, ...args], {encoding:'utf8', stdio:['ignore','pipe','pipe']});
  git(['init', '-q']);
  git(['config', 'user.name', 'Synthetic Build Test']);
  git(['config', 'user.email', 'synthetic@example.invalid']);
  git(['add', '.']);
  git(['-c', 'commit.gpgsign=false', 'commit', '-qm', 'Synthetic quick-build fixture']);
  write(source, 'out/main.js', 'preserved full Code-OSS runtime');
  write(source, 'node_modules/native.node', 'preserved native dependencies');
  write(source, '.build/electron/Autograde.exe', 'preserved Electron');
  write(buildOutput, 'build-windows-x64.json', '{"preserved":"full build report"}\n');
  write(buildOutput, 'previous-package.zip', 'preserved known-working package');
  const events = [], calls = [], packages = [], baselines = [];
  const host = {hostname:'synthetic-windows', platform:'win32', architecture:'x64', node:pin.node};
  const node = path.join(directory, 'runtime/node.exe');
  const publicBaseline = {reportSha256:'b'.repeat(64), checkpointSha256:'c'.repeat(64),
    sourceRevision:pin.commit, sourceDirty:false, buildMode:'candidate',
    upstream:{commit:pin.commit}, artifact:{file:'synthetic-original.zip', sha256:'d'.repeat(64)}};
  const baselineIdentity = {...publicBaseline, source, buildOutput, hostname:'private-baseline-host'};
  const dependencies = {
    repository, host, node,
    validateBaseline(options) {
      events.push('baseline'); baselines.push(options);
      return {baseline:baselineIdentity, verifyCore() {events.push('runtime');}};
    },
    verifyCoreOverlay(value) {assert.equal(value, source); events.push('core-overlay');},
    prepare(value) {assert.equal(value, source); events.push('prepare');},
    verify(value) {assert.equal(value, source); events.push('verify');},
    packageWindows(options) {
      events.push('package'); packages.push(options);
      assert.equal(options.source, source);
      assert.equal(options.node, node);
      assert.equal(options.archive, false);
      assert.notEqual(options.output, output);
      write(options.output, 'windows-package.json', JSON.stringify({
        executable:'editor/electron/Autograde.exe', extensionVersion:'0.6.0', wsl:windowsWslSupport(),
      }));
      write(options.output, 'editor/electron/Autograde.exe', 'synthetic Electron');
      write(options.output, 'editor/app/out/cli.js', 'synthetic packaged CLI');
      write(options.output, 'Autograde IDE.exe', 'synthetic native launcher');
      for (const name of ['Autograde IDE.exe', 'Autograde Launcher Checks.exe', ...windowsWslInstallerFiles]) {
        write(options.output, `wsl/${name}`, 'synthetic checked WSL launcher or installer');
      }
      write(options.output, 'wsl/launcher-version.json', JSON.stringify({schema:1, extensionVersion:'0.6.0', assemblyVersion:'0.6.0.0'}));
    },
    run(command, args, cwd, extra = {}) {
      calls.push({command, args:[...args], cwd, extra});
      if (command === 'tar.exe') {
        events.push('archive');
        assert.deepEqual(args.slice(0, 2), ['-a', '-cf']);
        assert.equal(args[3], '-C');
        assert.equal(args.length, 6);
        assert.equal(cwd, repository);
        const payload = path.join(args[4], args[5]);
        assert.equal(args[2], `${payload}.zip`);
        assert.equal(read(path.join(payload, 'quick-build.json')).packaging, 'folder-and-zip');
        // No real Windows tar/PE process runs on the test host. Capturing the
        // exact archived bytes proves the completed report is present first.
        fs.writeFileSync(args[2], JSON.stringify(tree(payload)));
      } else if (args[0] === '--test') {
        events.push(cwd === repository ? 'launcher-tests' : 'extension-tests');
      } else {
        events.push('cli');
        assert.equal(command, path.join(packages.at(-1).output, 'editor/electron/Autograde.exe'));
        assert.equal(cwd, path.join(packages.at(-1).output, 'editor/app'));
        assert.deepEqual(args, [path.join(cwd, 'out/cli.js'), cwd, '--version']);
        assert.equal(extra.env.ELECTRON_RUN_AS_NODE, '1');
      }
    },
  };
  return {directory, repository, source, buildOutput, output, pin, host, node, git,
    events, calls, packages, baselines, baselineIdentity, publicBaseline, dependencies,
    options:{source, buildOutput, output, archive:false}};
}

function assertUnpublished(f) {
  for (const file of [f.output, `${f.output}.zip`, `${f.output}.zip.sha256`]) assert.equal(fs.existsSync(file), false, file);
  for (const directory of [f.source, f.buildOutput, f.output]) assert.equal(fs.existsSync(lockPath(directory)), false);
}

test('CLI requires the three named paths and defaults to folder-only packaging', () => {
  const args = ['--source', 'existing source', '--build-output', 'completed build', '--output', 'new output'];
  assert.deepEqual(parseQuickOptions(args), {source:'existing source', buildOutput:'completed build', output:'new output', archive:false});
  assert.equal(parseQuickOptions(['--archive', ...args]).archive, true);
  for (const invalid of [[], args.slice(0, -1), args.slice(0, -2), [...args, '--archive', 'false'],
    [...args, '--archive', '--archive'], [...args, '--source', 'other'], [...args, '--local'],
    [...args, '--skip-tests'], [...args, '--resume'], ['--source', '--output', 'out'], [...args, '--output=other']]) {
    assert.throws(() => parseQuickOptions(invalid), /Usage:/);
  }
});

test('host and exact Node pin are checked before any build work', t => {
  const f = fixture(t);
  for (const host of [{...f.host, platform:'darwin'}, {...f.host, architecture:'arm64'}, {...f.host, node:'26.14.0'}]) {
    assert.throws(() => buildWindowsQuick(f.options, {...f.dependencies, host}), /native operating system|pinned Node/);
    assertUnpublished(f);
  }
  assert.deepEqual(f.events, []);
});

test('missing or non-directory baselines and nonboolean archive options cannot prepare a build', t => {
  const f = fixture(t), missing = path.join(f.directory, 'missing');
  const file = write(f.directory, 'ordinary-file', 'not a directory');
  for (const options of [{...f.options, source:missing}, {...f.options, buildOutput:missing},
    {...f.options, source:file}, {...f.options, buildOutput:file}]) {
    assert.throws(() => buildWindowsQuick(options, f.dependencies), /Existing full-build/);
  }
  assert.throws(() => buildWindowsQuick({...f.options, archive:'false'}, f.dependencies), /boolean/);
  assert.deepEqual(f.events, []);
  assertUnpublished(f);
});

test('preexisting output, archive and checksum are preserved rather than replaced', t => {
  const f = fixture(t);
  for (const target of [f.output, `${f.output}.zip`, `${f.output}.zip.sha256`]) {
    fs.writeFileSync(target, 'user-owned original');
    assert.throws(() => buildWindowsQuick(f.options, f.dependencies), /new output/);
    assert.equal(fs.readFileSync(target, 'utf8'), 'user-owned original');
    fs.unlinkSync(target);
  }
  fs.mkdirSync(f.output);
  assert.throws(() => buildWindowsQuick(f.options, f.dependencies), /new output/);
  assert.deepEqual(fs.readdirSync(f.output), []);
  assert.deepEqual(f.events, []);
});

test('overlapping source, full-build and new-output paths are refused before preparation', t => {
  const f = fixture(t), inside = path.join(f.source, 'nested-baseline');
  fs.mkdirSync(inside);
  for (const options of [{...f.options, buildOutput:f.source}, {...f.options, buildOutput:inside},
    {...f.options, source:f.directory}, {...f.options, output:path.join(f.source, 'new-package')},
    {...f.options, output:path.join(f.buildOutput, 'new-package')},
    {...f.options, output:path.join(f.source, 'new', 'output')}]) {
    assert.throws(() => buildWindowsQuick(options, f.dependencies), /separate|overlap/);
  }
  assert.equal(fs.existsSync(path.join(f.source, 'new')), false);
  assert.deepEqual(f.events, []);
  assertUnpublished(f);
});

test('directory aliases cannot bypass baseline or overlap validation', t => {
  const f = fixture(t), alias = path.join(f.directory, 'source-alias');
  fs.symlinkSync(f.source, alias, process.platform === 'win32' ? 'junction' : 'dir');
  for (const options of [{...f.options, source:alias}, {...f.options, buildOutput:alias}]) {
    assert.throws(() => buildWindowsQuick(options, f.dependencies), /Existing full-build/);
  }
  assert.throws(() => buildWindowsQuick({...f.options, output:path.join(alias, 'new', 'output')}, f.dependencies), /overlap/);
  assert.equal(fs.existsSync(path.join(f.source, 'new')), false);
  assert.deepEqual(f.events, []);
  assertUnpublished(f);
});

test('quick builds share actual source locks with full builds and release them on refusal', t => {
  const f = fixture(t), full = openBuildJournal({source:f.source, output:path.join(f.directory, 'active-full'), identity:{test:'full-build'}});
  try {
    assert.throws(() => buildWindowsQuick(f.options, f.dependencies), /locked by process/);
    assert.deepEqual(f.events, []);
    assert.equal(fs.existsSync(lockPath(f.source)), true);
    assert.equal(fs.existsSync(lockPath(f.buildOutput)), false);
    assert.equal(fs.existsSync(lockPath(f.output)), false);
  } finally {full.close();}
  const validate = f.dependencies.validateBaseline;
  f.dependencies.validateBaseline = options => {
    assert.throws(() => openBuildJournal({source:f.source, output:path.join(f.directory, 'concurrent-full'), identity:{test:'contender'}}), /locked by process/);
    return validate(options);
  };
  assert.equal(buildWindowsQuick(f.options, f.dependencies).directory, f.output);
  assert.equal(fs.existsSync(lockPath(f.source)), false);
});

test('baseline verification failure never prepares or packages the source', t => {
  const f = fixture(t), source = tree(f.source), previous = tree(f.buildOutput);
  f.dependencies.validateBaseline = () => {throw new Error('baseline mismatch');};
  assert.throws(() => buildWindowsQuick(f.options, f.dependencies), /baseline mismatch/);
  assert.deepEqual(f.events, []);
  assert.deepEqual(tree(f.source), source);
  assert.deepEqual(tree(f.buildOutput), previous);
  assertUnpublished(f);
});

for (const stage of ['core-overlay', 'prepare', 'extension-tests', 'launcher-tests', 'verify', 'package', 'cli', 'runtime', 'archive']) {
  test(`${stage} failure preserves previous source and build without publishing new output`, t => {
    const f = fixture(t), source = tree(f.source), previous = tree(f.buildOutput);
    const stop = () => {throw new Error(`synthetic ${stage} failure`);};
    const key = {'core-overlay':'verifyCoreOverlay', prepare:'prepare', verify:'verify', package:'packageWindows'}[stage];
    if (key) f.dependencies[key] = stop;
    else if (stage === 'runtime') {
      const original = f.dependencies.validateBaseline;
      f.dependencies.validateBaseline = options => ({...original(options), verifyCore:stop});
    } else {
      const original = f.dependencies.run;
      f.dependencies.run = (command, args, cwd, extra) => {
        const current = command === 'tar.exe' ? 'archive' : args[0] === '--test'
          ? (cwd === f.repository ? 'launcher-tests' : 'extension-tests') : 'cli';
        if (current === stage) stop();
        return original(command, args, cwd, extra);
      };
    }
    assert.throws(() => buildWindowsQuick({...f.options, archive:stage === 'archive'}, f.dependencies), new RegExp(`synthetic ${stage} failure`));
    assert.deepEqual(tree(f.source), source);
    assert.deepEqual(tree(f.buildOutput), previous);
    assertUnpublished(f);
  });
}

test('folder build executes only preparation, bounded tests, packaging and verification in order', t => {
  const f = fixture(t), source = tree(f.source), previous = tree(f.buildOutput);
  // Local iteration deliberately accepts both dirty tracked inputs and new
  // implementation files; it must never masquerade as a release candidate.
  write(f.repository, 'extensions/vscode/src/extension.ts', '// current local edit\n');
  write(f.repository, 'scripts/ide/new-local-test.mjs', '// untracked local work\n');
  const result = buildWindowsQuick(f.options, f.dependencies);
  assert.deepEqual(f.events, ['baseline', 'core-overlay', 'prepare', 'extension-tests', 'launcher-tests', 'verify', 'package', 'cli', 'runtime']);
  assert.equal(f.baselines.length, 1);
  assert.deepEqual(f.baselines[0], {repository:f.repository, source:f.source, buildOutput:f.buildOutput, pin:f.pin, host:f.host});
  assert.equal(f.calls.length, 3);
  assert.deepEqual(f.calls[0], {command:f.node, args:['--test', ...extensionChecks], cwd:path.join(f.repository, 'extensions/vscode'), extra:{}});
  assert.deepEqual(f.calls[1], {command:f.node, args:['--test', 'ide/code-oss/package-windows.test.mjs', 'ide/code-oss/windows-native-bootstrap.test.mjs'], cwd:f.repository, extra:{}});
  assert.ok(!JSON.stringify(f.calls.map(({command, args}) => [command, args]))
    .match(/npm(?:\.cmd)?|gulp|compile-client|compile-extensions/));
  assert.equal(result.schema, 'autograde-windows-quick-build-v1');
  assert.equal(result.sourceRevision, f.git(['rev-parse', 'HEAD']).trim());
  assert.equal(result.sourceDirty, true);
  assert.equal(result.status, 'local-fast-build');
  assert.equal(result.buildMode, 'local-fast');
  assert.equal(result.productionReady, false);
  assert.deepEqual(result.acceptance, {institution:'not-tested', windowsLab:'not-tested', windowsWsl2:'not-tested'});
  assert.deepEqual(result.wsl, {...windowsWslSupport(), launcherIncluded:true, runtimeIncluded:false});
  assert.equal(result.extensionVersion, '0.6.0');
  assert.deepEqual(result.baseline, f.publicBaseline);
  assert.deepEqual(result.host, {platform:'win32', architecture:'x64', node:f.pin.node});
  assert.equal(result.packaging, 'folder');
  assert.equal(result.archive, null);
  assert.equal(result.directory, f.output);
  assert.equal(result.checks.length, 8);
  assert.equal(result.checks.find(stage => stage.name === 'windows-wsl2-launcher-checks')?.result, 'passed');
  assert.ok(result.checks.every(stage => stage.result === 'passed' && stage.milliseconds >= 0));
  const {directory, archive, ...report} = result;
  assert.deepEqual(read(path.join(directory, 'quick-build.json')), report);
  for (const privateValue of [f.host.hostname, f.baselineIdentity.hostname, f.source, f.buildOutput]) {
    assert.equal(JSON.stringify(report).includes(privateValue), false);
  }
  assert.equal(fs.existsSync(path.join(directory, 'Autograde IDE.exe')), true);
  assert.equal(fs.existsSync(path.join(directory, 'wsl/Autograde IDE.exe')), true);
  assert.equal(fs.existsSync(path.join(directory, 'wsl/Install-Shortcut.ps1')), true);
  assert.equal(fs.existsSync(`${f.output}.zip`), false);
  assert.equal(fs.existsSync(`${f.output}.zip.sha256`), false);
  assert.ok(!fs.readdirSync(f.directory).some(name => name.includes('.quick-attempt-')));
  assert.deepEqual(tree(f.source), source);
  assert.deepEqual(tree(f.buildOutput), previous);
  for (const directory of [f.source, f.buildOutput, f.output]) assert.equal(fs.existsSync(lockPath(directory)), false);
});

test('archive mode includes the completed quick report and publishes its actual SHA256', t => {
  const f = fixture(t), result = buildWindowsQuick({...f.options, archive:true}, f.dependencies);
  assert.equal(result.packaging, 'folder-and-zip');
  assert.equal(result.archive, `${f.output}.zip`);
  assert.equal(f.events.at(-1), 'archive');
  const archive = fs.readFileSync(result.archive), contents = JSON.parse(archive.toString());
  const archivedReport = JSON.parse(Buffer.from(contents['quick-build.json'], 'base64').toString());
  assert.deepEqual(archivedReport, read(path.join(f.output, 'quick-build.json')));
  for (const privateValue of [f.host.hostname, f.baselineIdentity.hostname, f.source, f.buildOutput]) {
    assert.equal(JSON.stringify(archivedReport).includes(privateValue), false);
  }
  assert.equal(archivedReport.checks.at(-1).name, 'reused-runtime-verification');
  assert.equal(archivedReport.wsl.launcherIncluded, true);
  assert.equal(archivedReport.wsl.runtimeIncluded, false);
  assert.equal(archivedReport.wsl.runtime, 'separate-linux-payload');
  assert.ok(contents[path.join('wsl', 'Autograde IDE.exe')]);
  assert.ok(contents[path.join('wsl', 'Install-Shortcut.ps1')]);
  assert.equal(fs.readFileSync(`${result.archive}.sha256`, 'utf8'),
    `${createHash('sha256').update(archive).digest('hex')}  ${path.basename(result.archive)}\n`);
});

for (const name of ['Autograde IDE.exe', 'Autograde Launcher Checks.exe', 'launcher-version.json', ...windowsWslInstallerFiles]) {
  test(`a missing required WSL file prevents publication: ${name}`, t => {
    const f = fixture(t), packageWindows = f.dependencies.packageWindows, before = tree(f.buildOutput);
    f.dependencies.packageWindows = options => {
      packageWindows(options); fs.unlinkSync(path.join(options.output, 'wsl', name));
    };
    assert.throws(() => buildWindowsQuick({...f.options, archive:true}, f.dependencies), /required WSL2 package file/);
    assert.deepEqual(tree(f.buildOutput), before); assertUnpublished(f);
    assert.equal(f.events.includes('cli'), false);
    assert.equal(f.events.includes('archive'), false);
  });
}

for (const change of ['missing-metadata', 'wrong-launcher', 'wrong-installer', 'wrong-runtime', 'missing-requirement', 'wrong-version']) {
  test(`invalid WSL support cannot be reported as included: ${change}`, t => {
    const f = fixture(t), packageWindows = f.dependencies.packageWindows;
    f.dependencies.packageWindows = options => {
      packageWindows(options);
      const manifestPath = path.join(options.output, 'windows-package.json'), manifest = read(manifestPath);
      if (change === 'missing-metadata') delete manifest.wsl;
      if (change === 'wrong-launcher') manifest.wsl.launcher = '../outside.exe';
      if (change === 'wrong-installer') manifest.wsl.installer = 'wsl/other.ps1';
      if (change === 'wrong-runtime') manifest.wsl.runtime = 'included';
      if (change === 'missing-requirement') manifest.wsl.requires = ['WSL2'];
      if (change === 'wrong-version') write(options.output, 'wsl/launcher-version.json', JSON.stringify({schema:1, extensionVersion:'0.5.0', assemblyVersion:'0.5.0.0'}));
      fs.writeFileSync(manifestPath, JSON.stringify(manifest));
    };
    assert.throws(() => buildWindowsQuick(f.options, f.dependencies), /required Windows WSL2|Required WSL2 launcher version/);
    assertUnpublished(f);
  });
}

test('linked WSL installer is rejected instead of escaping the staged package', t => {
  const f = fixture(t), packageWindows = f.dependencies.packageWindows;
  const outside = write(f.directory, 'outside-installer.ps1', 'private external script');
  f.dependencies.packageWindows = options => {
    packageWindows(options);
    const installer = path.join(options.output, 'wsl/Install-Shortcut.ps1');
    fs.unlinkSync(installer); fs.linkSync(outside, installer);
  };
  assert.throws(() => buildWindowsQuick(f.options, f.dependencies), /required WSL2 package file/);
  assert.equal(fs.readFileSync(outside, 'utf8'), 'private external script'); assertUnpublished(f);
});

test('WSL directory aliases cannot satisfy mandatory package validation', t => {
  const f = fixture(t), packageWindows = f.dependencies.packageWindows;
  const external = path.join(f.directory, 'external-wsl');
  f.dependencies.packageWindows = options => {
    packageWindows(options);
    const directory = path.join(options.output, 'wsl');
    fs.renameSync(directory, external);
    fs.symlinkSync(external, directory, process.platform === 'win32' ? 'junction' : 'dir');
  };
  assert.throws(() => buildWindowsQuick(f.options, f.dependencies), /WSL2 package directory must be a real directory/);
  assert.equal(fs.existsSync(path.join(external, 'Autograde IDE.exe')), true); assertUnpublished(f);
});

test('the same full baseline can produce a second fresh quick output without altering the first', t => {
  const f = fixture(t);
  buildWindowsQuick(f.options, f.dependencies);
  const previous = tree(f.output), full = tree(f.buildOutput), source = tree(f.source);
  const next = path.join(f.directory, 'quick build second');
  assert.equal(buildWindowsQuick({...f.options, output:next}, f.dependencies).directory, next);
  assert.deepEqual(tree(f.output), previous);
  assert.deepEqual(tree(f.buildOutput), full);
  assert.deepEqual(tree(f.source), source);
  assert.equal(f.baselines.length, 2);
  assert.equal(read(path.join(next, 'quick-build.json')).status, 'local-fast-build');
});

test('release-candidate finalizer refuses quick reports even when renamed as a native build report', t => {
  const f = fixture(t), result = buildWindowsQuick(f.options, f.dependencies);
  const expected = {sourceRevision:result.sourceRevision, extensionVersion:result.extensionVersion, upstream:f.pin};
  const quickReport = path.join(f.output, 'quick-build.json'), original = fs.readFileSync(quickReport);
  assert.throws(() => verifyReports(f.output, expected), /All three native platform build reports are required/);
  const disguisedReport = path.join(f.output, 'build-windows-x64.json');
  fs.copyFileSync(quickReport, disguisedReport, fs.constants.COPYFILE_EXCL);
  assert.throws(() => verifyReports(f.output, expected), /Invalid or duplicate build report/);
  assert.deepEqual(fs.readFileSync(quickReport), original);
  assert.deepEqual(fs.readFileSync(disguisedReport), original);
});

for (const relative of ['extensions/vscode/src/extension.ts', 'ide/code-oss/windows/launcher.cs', 'scripts/ide/new-input.mjs']) {
  test(`changed build input ${relative} is rejected before publication`, t => {
    const f = fixture(t), source = tree(f.source), full = tree(f.buildOutput);
    const original = f.dependencies.packageWindows;
    f.dependencies.packageWindows = options => {
      original(options);
      write(f.repository, relative, '// changed while build was running\n');
    };
    assert.throws(() => buildWindowsQuick(f.options, f.dependencies), /inputs changed/);
    assert.deepEqual(tree(f.source), source);
    assert.deepEqual(tree(f.buildOutput), full);
    assertUnpublished(f);
  });
}

test('a changed Git revision during packaging cannot receive a misleading provenance report', t => {
  const f = fixture(t), packageWindows = f.dependencies.packageWindows;
  const originalRevision = f.git(['rev-parse', 'HEAD']).trim();
  f.dependencies.packageWindows = options => {
    packageWindows(options);
    f.git(['-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', 'Concurrent local commit']);
  };
  assert.throws(() => buildWindowsQuick(f.options, f.dependencies), /inputs changed/);
  assert.notEqual(f.git(['rev-parse', 'HEAD']).trim(), originalRevision);
  assertUnpublished(f);
});

test('PowerShell wrapper resolves caller paths and forwards optional archive without build bypass flags', () => {
  // Static contract only: native PowerShell and Windows lifecycle execution
  // remain Windows acceptance checks, not claims made by this Mac-hosted test.
  const script = fs.readFileSync(new URL('./build-windows-quick.ps1', import.meta.url), 'utf8');
  for (const name of ['Source', 'BuildOutput', 'Output']) {
    assert.match(script, new RegExp(`GetUnresolvedProviderPathFromPSPath\\(\\$${name}\\)`));
    assert.match(script, new RegExp(`\\[Parameter\\(Mandatory = \\$true\\)\\]\\[ValidateNotNullOrEmpty\\(\\)\\]\\[string\\]\\$${name}`));
  }
  assert.match(script, /\[switch\]\$Archive/);
  assert.match(script, /if \(\$Archive\) \{ \$quickBuildArguments \+= '--archive' \}/);
  assert.match(script, /& \$nodeCommand\.Path @quickBuildArguments/);
  assert.match(script, /exit \$LASTEXITCODE/);
  assert.doesNotMatch(script, /--(?:skip-tests|resume|force)|Set-Location|npm ci/);
});
