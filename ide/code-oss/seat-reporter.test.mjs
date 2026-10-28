import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createSeatReporter, loadSeatConfig, seatConfigPath } from './seat-reporter.mjs';

function fixture(t, changes = {}) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'ag-seat-')));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const file = path.join(root, 'device.json');
  fs.writeFileSync(file, JSON.stringify({ apiUrl: 'https://example.edu:20000', seatId: 'lab-01', reportToken: 'a'.repeat(43), ...changes }), { mode: 0o600 });
  return file;
}
test('device configuration is separate from student folders and accepts HTTPS only', t => {
  assert.match(seatConfigPath('darwin', '/Users/test'), /Autograde Operator\/device.json$/);
  const file = fixture(t);
  assert.equal(loadSeatConfig(file).seatId, 'lab-01');
  fs.chmodSync(file, 0o644);
  assert.throws(() => loadSeatConfig(file, 'linux'), /invalid/);
});
for (const changes of [{ apiUrl: 'http://example.edu' }, { apiUrl:'https://example.edu?token=secret' }, { apiUrl:'https://user:secret@example.edu' }, { extra:'private' }]) {
  test('rejects unsafe or secret-bearing device configuration ' + Object.keys(changes)[0] + JSON.stringify(changes), t => {
    assert.throws(() => loadSeatConfig(fixture(t, changes)), /invalid/);
  });
}
test('sends only machine fields, no redirects, fixed timeout and cleanup evidence', async t => {
  const calls = [];
  const reporter = createSeatReporter({ configFile: fixture(t), version:'0.5.20', platform:'darwin', architecture:'arm64',
    send: async (url, options) => { calls.push({ url, options }); return { ok:true }; } });
  reporter.event({ event:'ide-started', pid:42, path:'/private/student', student:'secret' });
  await new Promise(resolve => setImmediate(resolve));
  reporter.event({ event:'session-cleaned' });
  await reporter.close();
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url.pathname, '/v1/operations/seats/report');
  assert.equal(calls[0].options.redirect, 'error');
  assert.equal(JSON.parse(calls[0].options.body).cleanup, 'unknown');
  assert.equal(JSON.parse(calls[1].options.body).cleanup, 'ok');
  assert.doesNotMatch(calls.map(c=>c.options.body).join(), /private|secret|student|pid/);
});
test('network failure is bounded and never stops the IDE or leaks exception text', async t => {
  const warnings = [];
  const reporter = createSeatReporter({ configFile:fixture(t), onWarning:c=>warnings.push(c), send:async()=>{ throw new Error('secret-token'); } });
  for(let i=0;i<50;i++) reporter.event({event:'ide-started'});
  await reporter.close();
  assert.equal(warnings.length,2);
  assert.deepEqual(new Set(warnings),new Set(['device_report_unconfirmed']));
});
