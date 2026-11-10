import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';

test('Windows PowerShell wrapper forwards optional paths without prompts from another working directory', {
  skip:process.platform !== 'win32' && 'Requires Windows PowerShell',
}, t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'autograde-quick-wrapper-'));
  t.after(() => fs.rmSync(directory, {recursive:true, force:true}));
  const scripts = path.join(directory, 'script location');
  const caller = path.join(directory, 'caller location');
  const bin = path.join(directory, 'test node');
  for (const folder of [scripts, caller, bin]) fs.mkdirSync(folder);
  const wrapper = path.join(scripts, 'build-windows-quick.ps1');
  const entrypoint = path.join(scripts, 'build-windows-quick.mjs');
  const record = path.join(directory, 'arguments.json');
  fs.copyFileSync(new URL('./build-windows-quick.ps1', import.meta.url), wrapper);
  fs.writeFileSync(entrypoint, `
import fs from 'node:fs';
fs.writeFileSync(process.env.QUICK_WRAPPER_TEST_RECORD, JSON.stringify({
  script:process.argv[1], arguments:process.argv.slice(2), cwd:process.cwd(),
}));
process.exit(Number(process.env.QUICK_WRAPPER_TEST_EXIT));
`);
  fs.writeFileSync(path.join(bin, 'node.cmd'), '@echo off\r\n"%QUICK_WRAPPER_TEST_NODE%" %*\r\nexit /b %errorlevel%\r\n');
  const powershell = process.env.SystemRoot
    ? path.join(process.env.SystemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    : 'powershell.exe';
  const environment = Object.fromEntries(Object.entries(process.env)
    .filter(([key]) => key.toLowerCase() !== 'path'));
  environment.PATH = `${bin}${path.delimiter}${process.env.PATH ?? ''}`;
  const invoke = (args, expected, exitCode = 0) => {
    fs.rmSync(record, {force:true});
    const result = spawnSync(powershell, [
      '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', wrapper, ...args,
    ], {
      cwd:caller, encoding:'utf8', timeout:15_000,
      env:{...environment,
        QUICK_WRAPPER_TEST_NODE:process.execPath, QUICK_WRAPPER_TEST_RECORD:record,
        QUICK_WRAPPER_TEST_EXIT:String(exitCode)},
    });
    assert.ifError(result.error);
    assert.equal(result.status, exitCode, result.stderr || result.stdout);
    assert.deepEqual(JSON.parse(fs.readFileSync(record, 'utf8')), {
      script:entrypoint, arguments:expected, cwd:caller,
    });
  };
  invoke([], []);
  invoke(['-Archive'], ['--archive']);
  invoke(['-BuildOutput', '.\\completed build'], ['--build-output', path.join(caller, 'completed build')]);
  invoke(['-Source', '.\\source [copy]'], ['--source', path.join(caller, 'source [copy]')]);
  invoke(['-Source', '.\\source [copy]', '-BuildOutput', '.\\completed build',
    '-Output', '.\\new output', '-Archive'], [
    '--source', path.join(caller, 'source [copy]'),
    '--build-output', path.join(caller, 'completed build'),
    '--output', path.join(caller, 'new output'), '--archive',
  ]);
  invoke(['-Output', '.\\new output'], ['--output', path.join(caller, 'new output')], 29);
});
