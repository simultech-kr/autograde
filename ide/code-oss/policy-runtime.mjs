// Emit the verified policy sources with the pinned editor compiler. Packaging and
// developer launch call this after the normal upstream build, so stale out/ files
// cannot silently omit the installation guard.
import * as fs from 'node:fs';
import * as path from 'node:path';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';

const sha = value => createHash('sha256').update(value).digest('hex');
function statIfPresent(file) {
  try { return fs.lstatSync(file); }
  catch (error) { if (error.code === 'ENOENT') return undefined; throw error; }
}
function checkParents(source, relative) {
  let current = source;
  if (!fs.lstatSync(current).isDirectory()) throw new Error('Policy source root must be a real directory');
  for (const part of path.dirname(relative).split('/')) {
    current = path.join(current, part);
    const info = statIfPresent(current);
    if (info && !info.isDirectory()) throw new Error(`Policy path is not a real directory: ${current}`);
  }
}

export function compilePolicyRuntime(source) {
  const marker = JSON.parse(fs.readFileSync(path.join(source, '.autograde-ide-overlay.json'), 'utf8'));
  if (!marker.policySources || !Object.keys(marker.policySources).length) throw new Error('Prepare the IDE installation policy before building');
  const compiler = path.join(source, 'node_modules/typescript/lib/typescript.js');
  if (!fs.existsSync(compiler)) throw new Error('Install the pinned Code-OSS build dependencies before packaging');
  const ts = createRequire(import.meta.url)(compiler);
  const outputs = new Map();
  for (const [relative, expected] of Object.entries(marker.policySources)) {
    if (!/^src\/[A-Za-z0-9_./-]+\.ts$/.test(relative) || relative.split('/').some(p => p === '..' || p === '.')) throw new Error('Invalid IDE policy source');
    checkParents(source, relative);
    const input = path.join(source, relative);
    if (!fs.lstatSync(input).isFile()) throw new Error(`Policy source must be a regular file: ${relative}`);
    const bytes = fs.readFileSync(input);
    if (sha(bytes) !== expected) throw new Error(`IDE policy source changed: ${relative}`);
    const emitted = ts.transpileModule(bytes.toString('utf8'), {
      fileName: input, reportDiagnostics: true,
      compilerOptions: { target: ts.ScriptTarget.ES2024, module: ts.ModuleKind.ESNext,
        experimentalDecorators: true, useDefineForClassFields: false, sourceMap: false },
    });
    if (emitted.diagnostics?.some(d => d.category === ts.DiagnosticCategory.Error)) throw new Error(`Cannot compile IDE policy: ${relative}`);
    outputs.set(relative.replace(/^src\//, 'out/').replace(/\.ts$/, '.js'), emitted.outputText);
  }
  // Verify the destination chain before writing any generated file.
  for (const relative of outputs.keys()) {
    checkParents(source, relative);
    const target = path.join(source, relative);
    const info = statIfPresent(target);
    if (info && !info.isFile()) throw new Error(`Policy build destination is not a regular file: ${relative}`);
  }
  const stage = fs.mkdtempSync(path.join(source, '.autograde-policy-build-'));
  const installed = [], previous = [];
  try {
    for (const [relative, bytes] of outputs) {
      const target = path.join(source, relative), next = path.join(stage, 'next', relative);
      fs.mkdirSync(path.dirname(next), {recursive: true}); fs.writeFileSync(next, bytes, {flag: 'wx'});
      fs.mkdirSync(path.dirname(target), {recursive: true});
      if (fs.existsSync(target)) {
        const backup = path.join(stage, 'previous', relative);
        fs.mkdirSync(path.dirname(backup), {recursive: true});
        fs.renameSync(target, backup); previous.push([target, backup]);
      }
      fs.renameSync(next, target); installed.push(target);
    }
  } catch (error) {
    for (const target of installed.reverse()) fs.unlinkSync(target);
    for (const [target, backup] of previous.reverse()) fs.renameSync(backup, target);
    throw error;
  }
  return {files: outputs.size, backup: stage};
}
