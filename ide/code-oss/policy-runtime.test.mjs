import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import test from 'node:test';
import { compilePolicyRuntime } from './policy-runtime.mjs';

// CI installs the extension dependencies; use its real TypeScript compiler behind
// a checkout-local shim so the tests also verify compiler lookup and options.
const typescript = createRequire(new URL('../../extensions/vscode/package.json', import.meta.url)).resolve('typescript');
const digest = value => createHash('sha256').update(value).digest('hex');
const sourceKey = 'src/vs/policy/install.ts';
const outputKey = 'out/vs/policy/install.js';
const policySource = 'export interface Policy { denied: boolean }\nexport class InstallPolicy implements Policy { constructor(public denied: boolean) {} canInstall(): boolean { return !this.denied; } }\n';

function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'autograde-policy-runtime-')));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const source = path.join(root, 'Code OSS checkout');
  const write = (relative, value) => {
    const file = path.join(source, relative);
    fs.mkdirSync(path.dirname(file), {recursive: true});
    fs.writeFileSync(file, typeof value === 'object' ? JSON.stringify(value) : value);
    return file;
  };
  const compilerCalls = path.join(root, 'compiler-calls.jsonl');
  const compiler = write('node_modules/typescript/lib/typescript.js', `
const ts = require(${JSON.stringify(typescript)});
module.exports = {...ts, transpileModule(input, options) {
  require('node:fs').appendFileSync(${JSON.stringify(compilerCalls)}, JSON.stringify(options) + '\\n');
  return ts.transpileModule(input, options);
}};
`);
  const setSources = entries => {
    const policySources = {};
    for (const [relative, bytes] of entries) {
      write(relative, bytes); policySources[relative] = digest(bytes);
    }
    write('.autograde-ide-overlay.json', {policySources});
  };
  setSources([[sourceKey, policySource]]);
  return {root, source, write, setSources, compiler, compilerCalls,
    read: relative => fs.readFileSync(path.join(source, relative), 'utf8'),
    stages: () => fs.readdirSync(source).filter(name => name.startsWith('.autograde-policy-build-'))};
}

test('verified TypeScript uses the checkout compiler and replaces stale output with executable policy', async t => {
  const f = fixture(t);
  f.write(outputKey, '// stale build without installation policy\n');
  f.write('out/unrelated.js', '// preserve upstream output\n');
  const result = compilePolicyRuntime(f.source);
  assert.equal(result.files, 1);
  assert.deepEqual(f.stages(), [path.basename(result.backup)]);
  assert.equal(fs.readFileSync(path.join(result.backup, 'previous', outputKey), 'utf8'), '// stale build without installation policy\n');
  assert.equal(f.read('out/unrelated.js'), '// preserve upstream output\n');
  const calls = fs.readFileSync(f.compilerCalls, 'utf8').trim().split('\n').map(line => JSON.parse(line));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].fileName, path.join(f.source, sourceKey));
  assert.equal(calls[0].reportDiagnostics, true);
  assert.equal(calls[0].compilerOptions.experimentalDecorators, true);
  assert.equal(calls[0].compilerOptions.sourceMap, false);
  const module = await import(`data:text/javascript;base64,${Buffer.from(f.read(outputKey)).toString('base64')}`);
  assert.equal(new module.InstallPolicy(true).canInstall(), false);
  assert.equal(new module.InstallPolicy(false).canInstall(), true);
  assert.equal(fs.existsSync(path.join(f.source, `${outputKey}.map`)), false);
});

test('changed policy source hash fails before any output is replaced', t => {
  const f = fixture(t);
  f.write(outputKey, '// keep previous build\n');
  f.write(sourceKey, policySource.replace('return !this.denied', 'return true'));
  assert.throws(() => compilePolicyRuntime(f.source), /policy source changed/i);
  assert.equal(f.read(outputKey), '// keep previous build\n');
  assert.equal(fs.existsSync(f.compilerCalls), false);
  assert.deepEqual(f.stages(), []);
});

test('a syntax error in the last policy source leaves all previous generated files intact', t => {
  const f = fixture(t), second = 'src/vs/policy/other.ts', secondOutput = 'out/vs/policy/other.js';
  f.setSources([[sourceKey, policySource], [second, 'export function broken(: boolean { return false; }']]);
  f.write(outputKey, '// first previous output\n');
  f.write(secondOutput, '// second previous output\n');
  assert.throws(() => compilePolicyRuntime(f.source), /Cannot compile IDE policy/);
  assert.equal(f.read(outputKey), '// first previous output\n');
  assert.equal(f.read(secondOutput), '// second previous output\n');
  assert.deepEqual(f.stages(), []);
});

test('missing preparation and missing checkout compiler fail without generated output', async t => {
  for (const kind of ['missing sources', 'empty sources', 'missing compiler']) await t.test(kind, t => {
    const f = fixture(t);
    if (kind === 'missing sources') f.write('.autograde-ide-overlay.json', {});
    else if (kind === 'empty sources') f.write('.autograde-ide-overlay.json', {policySources: {}});
    else fs.unlinkSync(f.compiler);
    assert.throws(() => compilePolicyRuntime(f.source), /Prepare the IDE installation policy|pinned Code-OSS build dependencies/);
    assert.equal(fs.existsSync(path.join(f.source, 'out')), false);
    assert.deepEqual(f.stages(), []);
  });
});

test('unsafe policy source paths cannot select a different file or create generated output', async t => {
  for (const relative of ['src/../outside.ts', 'src/./policy.ts', '../src/policy.ts', '/src/policy.ts', 'src/policy.js', 'src/policy.ts/../../outside.ts', 'src\\policy.ts']) {
    await t.test(relative, t => {
      const f = fixture(t);
      f.write('.autograde-ide-overlay.json', {policySources: {[relative]: digest(policySource)}});
      assert.throws(() => compilePolicyRuntime(f.source), /Invalid IDE policy source/);
      assert.equal(fs.existsSync(path.join(f.source, 'out')), false);
      assert.deepEqual(f.stages(), []);
    });
  }
});

test('linked policy sources and source parent directories are refused', async t => {
  for (const kind of ['source file', 'source parent', 'checkout root']) await t.test(kind, t => {
    const f = fixture(t);
    if (kind === 'source file') {
      const outside = path.join(f.root, 'outside.ts');
      fs.writeFileSync(outside, policySource);
      fs.unlinkSync(path.join(f.source, sourceKey)); fs.symlinkSync(outside, path.join(f.source, sourceKey));
    } else if (kind === 'source parent') {
      const outside = path.join(f.root, 'outside-source');
      fs.renameSync(path.join(f.source, 'src/vs/policy'), outside);
      fs.symlinkSync(outside, path.join(f.source, 'src/vs/policy'));
    }
    const selected = kind === 'checkout root' ? path.join(f.root, 'linked-checkout') : f.source;
    if (kind === 'checkout root') fs.symlinkSync(f.source, selected);
    assert.throws(() => compilePolicyRuntime(selected), /real directory|regular file/);
    assert.equal(fs.existsSync(path.join(f.source, 'out')), false);
    assert.deepEqual(f.stages(), []);
  });
});

test('linked or non-directory output paths fail without replacing any generated files', async t => {
  for (const kind of ['linked directory', 'linked output', 'dangling output', 'file parent']) await t.test(kind, t => {
    const f = fixture(t);
    f.setSources([['src/first.ts', 'export const first: boolean = true;'], [sourceKey, policySource]]);
    f.write('out/first.js', '// keep earlier generated output\n');
    const outside = path.join(f.root, 'outside'); fs.mkdirSync(outside);
    const externalFile = path.join(outside, 'install.js');
    if (kind === 'linked directory') fs.symlinkSync(outside, path.join(f.source, 'out/vs'));
    else if (kind === 'file parent') f.write('out/vs', 'keep non-directory file');
    else {
      fs.mkdirSync(path.join(f.source, 'out/vs/policy'), {recursive: true});
      if (kind === 'linked output') fs.writeFileSync(externalFile, '// keep external output\n');
      fs.symlinkSync(externalFile, path.join(f.source, outputKey));
    }
    assert.throws(() => compilePolicyRuntime(f.source), /real directory|regular file/);
    assert.equal(f.read('out/first.js'), '// keep earlier generated output\n');
    assert.deepEqual(f.stages(), []);
    if (kind === 'linked output') assert.equal(fs.readFileSync(externalFile, 'utf8'), '// keep external output\n');
    else assert.deepEqual(fs.readdirSync(outside), []);
    if (kind === 'linked output' || kind === 'dangling output') assert.equal(fs.lstatSync(path.join(f.source, outputKey)).isSymbolicLink(), true);
  });
});
