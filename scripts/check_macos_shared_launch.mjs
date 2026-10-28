// Launch a built Mac app in an isolated synthetic seat and verify exit cleanup.
// Never uses the installed user's app or the normal /tmp/ag-lab-UID seat.
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { pathToFileURL } from 'node:url';

assert.equal(process.platform, 'darwin', 'This smoke test requires macOS');
const app = process.argv[2];
assert.ok(app?.endsWith('.app'), 'Pass the built Autograde IDE.app path');
const resources = path.resolve(app, 'Contents/Resources');
const { loadMacBundle, runMacSession } = await import(pathToFileURL(path.join(resources, 'autograde/mac-launcher.mjs')));
const bundle = loadMacBundle(resources);
assert.equal(bundle.defaults['autograde.serviceBaseUrl'], 'https://ai.cbchoi.info:20000');
const root = fs.mkdtempSync('/private/tmp/agr-');
const seat = path.join(root, 'seat');
const exported = path.join(root, 'export.c');
fs.writeFileSync(exported, 'synthetic exported source');
const events = [];
let pid, timer;
try {
  const result = await runMacSession(bundle, { runtimeBase: seat, report(event) {
    events.push(event.event);
    if (event.event !== 'ide-started') return;
    pid = event.pid;
    for (const relative of ['workspace/main.c', 'materials/notes.txt', 'r/user-data/User/synthetic-auth.json']) {
      const file = path.join(seat, 'current', relative);
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, 'synthetic smoke-test data only');
    }
    // The supervisor must clean the actual Electron process tree on exit.
    timer = setTimeout(() => process.kill(pid, 'SIGTERM'), 8000);
  }});
  assert.ok(events.includes('session-cleaned'));
  assert.equal(fs.existsSync(path.join(seat, 'current')), false);
  assert.equal(fs.existsSync(path.join(seat, '.autograde-shared-lock.json')), false);
  assert.equal(fs.readFileSync(exported, 'utf8'), 'synthetic exported source');
  const report = { platform: `${os.platform()}-${os.arch()}`, synthetic_data_only: true,
    checks: ['built app launches real Electron', 'fixed seat created', 'source/materials/auth profile removed on exit',
      'seat lock removed', 'external export preserved', 'bundled institution API preserved'],
    exit: result, events };
  fs.mkdirSync('build/remote-learning-ui', { recursive: true });
  fs.writeFileSync('build/remote-learning-ui/mac-shared-launch-report.json', JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report));
} finally {
  clearTimeout(timer);
  // Never remove a seat if the supervisor could not prove process cleanup.
  if (!fs.existsSync(path.join(seat, 'current'))) fs.rmSync(root, { recursive: true, force: true });
}
