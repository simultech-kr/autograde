import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { linuxEntryScript } from './package-linux.mjs';
import { wslEntryScript } from './package-wsl.mjs';
import { channelDesktopTemplates } from './channel-desktop-templates.mjs';

test('Linux, WSL and channel entries explain an unavailable Node before starting the launcher', { skip: process.platform === 'win32' }, async t => {
  for (const kind of ['linux', 'desktop-install', 'wsl', 'channel']) await t.test(kind, t => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "ag-runtime-학교's "));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    fs.mkdirSync(path.join(root, 'runtime/bin'), { recursive: true });
    fs.mkdirSync(path.join(root, 'bin'));
    // Simulate a loader failure, recording whether the probe ran with clean
    // environment and ensuring a second (launcher) invocation never happens.
    const runtime = '#!/bin/sh\nif [ "$1" = --version ]; then\n' +
      '  if [ -n "${NODE_OPTIONS-}${NODE_PATH-}${PRIVATE_MARKER-}" ]; then exit 99; fi\n' +
      '  printf clean > "$0.probed"\n  printf "private loader details" >&2\n  exit 127\nfi\n' +
      'printf started > "$0.started"\n';
    fs.writeFileSync(path.join(root, 'runtime/bin/node'), runtime, { mode: 0o755 });
    const source = kind === 'channel' ? channelDesktopTemplates('linux', root)['start-channel.sh']
      : kind === 'wsl' ? wslEntryScript()
      : linuxEntryScript(kind === 'desktop-install' ? 'linux-install-desktop.mjs' : 'linux-launcher.mjs');
    const script = path.join(root, 'bin/start');
    fs.writeFileSync(script, source, { mode: 0o755 });
    const result = spawnSync('/bin/sh', [script, '--check'], { encoding: 'utf8',
      env: { PATH: '/usr/bin:/bin', NODE_OPTIONS: '--require /untrusted', NODE_PATH: '/untrusted', PRIVATE_MARKER: 'secret' } });
    assert.equal(result.status, 1);
    assert.equal(fs.readFileSync(path.join(root, 'runtime/bin/node.probed'), 'utf8'), 'clean');
    assert.equal(fs.existsSync(path.join(root, 'runtime/bin/node.started')), false);
    assert.match(result.stderr, /libatomic1 \(Ubuntu\/Debian\) or libatomic/);
    assert.doesNotMatch(result.stderr, /private loader details|secret/);
    assert.equal(result.stdout, '');
  });
});
