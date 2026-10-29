import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import test from 'node:test';
import {validateDiscoveryProfile, loadMachineDiscoveryProfile, machineDiscoveryPath} from './machine-discovery-settings.mjs';

const profile = () => ({schema: 'autograde.ide-discovery.v1', trustedServers: [
  {institutionId: 'school-a', displayName: '학교 A', apiUrl: 'https://api.school.example', portalUrl: 'https://school.example'},
  {institutionId: 'school-b', displayName: '학교 B', apiUrl: 'https://school-b.example:20000', portalUrl: 'https://school-b.example:20010'},
], selectedInstitutionId: 'school-b'});
function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'ag-discovery-')));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const file = path.join(root, 'institution-discovery.json');
  fs.writeFileSync(file, JSON.stringify(profile()), {mode: 0o600});
  return {root, file};
}
test('public machine choices inject selected endpoints without changing the file', t => {
  const f = fixture(t), before = fs.readFileSync(f.file);
  const loaded = loadMachineDiscoveryProfile(f.file);
  assert.deepEqual(loaded.profile, profile());
  assert.equal(loaded.settings['autograde.serviceBaseUrl'], 'https://school-b.example:20000');
  assert.equal(loaded.settings['autograde.studentPortalBaseUrl'], 'https://school-b.example:20010');
  assert.equal(loaded.settings['autograde.discovery.enabled'], true);
  assert.deepEqual(fs.readFileSync(f.file), before);
  assert.equal(machineDiscoveryPath('/Users/test'), '/Users/test/Library/Application Support/Autograde IDE/institution-discovery.json');
});
test('missing optional profile creates no directories or state', t => {
  const f = fixture(t), absent = path.join(f.root, 'absent', 'file.json');
  assert.equal(loadMachineDiscoveryProfile(absent), undefined);
  assert.equal(loadMachineDiscoveryProfile(null), undefined);
  assert.equal(fs.existsSync(path.dirname(absent)), false);
});
test('invalid metadata, credentials, duplicate IDs and untrusted selections are rejected', async t => {
  for (const [label, mutate] of [
    ['token', p => { p.token = 'secret'; }],
    ['server password', p => { p.trustedServers[0].password = 'secret'; }],
    ['duplicate ID', p => { p.trustedServers[1].institutionId = 'school-a'; }],
    ['unknown selection', p => { p.selectedInstitutionId = 'outside'; }],
    ['unsafe ID', p => { p.trustedServers[0].institutionId = '../other'; }],
    ['control label', p => { p.trustedServers[0].displayName = 'school\npassword'; }],
    ['credentials', p => { p.trustedServers[0].apiUrl = 'https://user:password@school.example'; }],
    ['path', p => { p.trustedServers[0].apiUrl = 'https://school.example/login'; }],
    ['HTTP', p => { p.trustedServers[0].apiUrl = 'http://school.example'; }],
    ['IPv4', p => { p.trustedServers[0].apiUrl = 'https://192.0.2.1'; }],
    ['same origins', p => { p.trustedServers[0].apiUrl = p.trustedServers[0].portalUrl; }],
    ['many entries', p => { p.trustedServers = Array.from({length: 33}, (_, i) => ({...p.trustedServers[0], institutionId: `school-${i}`})); }],
  ]) await t.test(label, () => { const p = profile(); mutate(p); assert.throws(() => validateDiscoveryProfile(p)); });
});
test('linked or shared files, linked parent and excessive file size fail closed', async t => {
  for (const [label, mutate] of [
    ['file mode', f => { fs.chmodSync(f.file, 0o644); }],
    ['parent mode', f => { fs.chmodSync(f.root, 0o755); }],
    ['hardlink', f => { fs.linkSync(f.file, path.join(f.root, 'other.json')); }],
    ['symlink', f => { fs.renameSync(f.file, `${f.file}.real`); fs.symlinkSync(`${f.file}.real`, f.file); }],
    ['size', f => { fs.writeFileSync(f.file, ' '.repeat(32769)); }],
    ['invalid JSON', f => { fs.writeFileSync(f.file, '{'); }],
  ]) await t.test(label, t => { const f = fixture(t); mutate(f); assert.throws(() => loadMachineDiscoveryProfile(f.file)); });
  const f = fixture(t), linked = path.join(f.root, 'alias');
  fs.symlinkSync(f.root, linked);
  assert.throws(() => loadMachineDiscoveryProfile(path.join(linked, 'institution-discovery.json')));
});
