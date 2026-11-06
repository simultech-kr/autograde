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
for (const changes of [{ apiUrl: 'http://example.edu' }, { apiUrl:'https://example.edu?token=secret' }, { apiUrl:'https://user:secret@example.edu' }, { extra:'private' }, {cleanup_mode: 'on_exit'}]) {
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
  assert.equal('cleanup_mode' in JSON.parse(calls[0].options.body), false);
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

for (const mode of ['on_exit', 'next_start']) test(`reports the pinned ${mode} mode on startup and heartbeat without implying cleanup success`, async t => {
  const calls = [];
  let heartbeat;
  const reported = new Promise(resolve => { heartbeat = resolve; });
  const options = { configFile: fixture(t), platform: 'win32', architecture: 'x64', cleanupMode: mode,
    intervalMilliseconds: 5, send: async (_url, request) => {
      calls.push(JSON.parse(request.body));
      if (calls.length >= 2) heartbeat();
      return {ok: true};
    } };
  const reporter = createSeatReporter(options);
  options.cleanupMode = mode === 'on_exit' ? 'next_start' : 'on_exit';
  reporter.event({event: 'ide-started', cleanup_mode: options.cleanupMode, student: 'private-student'});
  let timeout;
  try {
    await Promise.race([reported, new Promise((_resolve, reject) => {
      timeout = setTimeout(() => reject(new Error('heartbeat missing')), 1000);
    })]);
  } finally { clearTimeout(timeout); await reporter.close(); }
  assert.ok(calls.length >= 2);
  assert.ok(calls.every(report => report.cleanup_mode === mode && report.cleanup === 'unknown'));
  assert.deepEqual(Object.keys(calls[0]).sort(), ['architecture', 'cleanup', 'cleanup_mode', 'extension_version',
    'ide_version', 'platform', 'report_id', 'seat_id', 'update']);
  assert.doesNotMatch(JSON.stringify(calls), /private-student|reportToken|apiUrl/);
  const count = calls.length;
  reporter.event({event: 'session-cleaned'});
  await reporter.close();
  assert.equal(calls.length, count); // Bootstrap close cannot claim native cleanup completed later.
});

for (const mode of [null, 'on_exit\n', 'unsupported', {student: 'private-student', token: 'private-token'}]) {
  test(`invalid cleanup mode ${typeof mode} is omitted without disrupting reporting`, async t => {
    const calls = [], warnings = [];
    const reporter = createSeatReporter({configFile: fixture(t), platform: 'win32', cleanupMode: mode,
      onWarning: code => warnings.push(code), send: async (_url, request) => {
        calls.push(JSON.parse(request.body)); return {ok: true};
      }});
    reporter.event({event: 'ide-started'});
    await reporter.close();
    assert.equal(calls.length, 1);
    assert.equal('cleanup_mode' in calls[0], false);
    assert.equal(calls[0].cleanup, 'unknown');
    assert.deepEqual(warnings, ['device_report_mode_invalid']);
    assert.doesNotMatch(JSON.stringify(calls), /private-student|private-token/);
  });
}
