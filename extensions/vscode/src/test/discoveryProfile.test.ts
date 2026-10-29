import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import test from "node:test";
import {readDiscoveryProfile, selectDiscoveryInstitution, validateDiscoveryProfile, type DiscoveryProfile} from "../discoveryProfile";

const profile = (): DiscoveryProfile => ({schema: "autograde.ide-discovery.v1", trustedServers: [
  {institutionId: "school-a", displayName: "학교 A", apiUrl: "https://api.school.example", portalUrl: "https://school.example"},
  {institutionId: "school-b", displayName: "학교 B", apiUrl: "https://school-b.example:20000", portalUrl: "https://school-b.example:20010"},
]});

function fixture(t: {after(callback: () => void): void}): {root: string; file: string} {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "ag-discovery-profile-")));
  fs.chmodSync(root, 0o700);
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const file = path.join(root, "institution-discovery.json");
  fs.writeFileSync(file, JSON.stringify(profile()), {mode: 0o600});
  return {root, file};
}

test("only the selected institution changes in the existing private public-metadata file", {skip: process.platform === "win32"}, async t => {
  const f = fixture(t), original = await readDiscoveryProfile(f.file);
  await selectDiscoveryInstitution(f.file, "school-b");
  assert.deepEqual(await readDiscoveryProfile(f.file), {...original, selectedInstitutionId: "school-b"});
  assert.equal(fs.statSync(f.file).mode & 0o777, 0o600);
  assert.deepEqual(fs.readdirSync(f.root), ["institution-discovery.json"]);
  await selectDiscoveryInstitution(f.file, "school-a");
  assert.equal((await readDiscoveryProfile(f.file))?.selectedInstitutionId, "school-a");
});

test("unknown institution never changes the file or expands trust", {skip: process.platform === "win32"}, async t => {
  const f = fixture(t), before = fs.readFileSync(f.file);
  await assert.rejects(selectDiscoveryInstitution(f.file, "rogue"));
  assert.deepEqual(fs.readFileSync(f.file), before);
  assert.deepEqual(fs.readdirSync(f.root), ["institution-discovery.json"]);
});

test("concurrent operator edits are preserved instead of being replaced by an older selection", {skip: process.platform === "win32"}, async t => {
  const f = fixture(t), concurrent = {...profile(), selectedInstitutionId: "school-a"};
  const nativeFs = require("node:fs") as typeof fs;
  const sync = nativeFs.fsyncSync;
  t.mock.method(nativeFs, "fsyncSync", (fd: number): void => {
    sync(fd);
    fs.writeFileSync(f.file, JSON.stringify(concurrent));
  });
  await assert.rejects(selectDiscoveryInstitution(f.file, "school-b"));
  assert.deepEqual(await readDiscoveryProfile(f.file), concurrent);
  assert.deepEqual(fs.readdirSync(f.root), ["institution-discovery.json"]);
});

test("missing optional profile is not created by reading or selecting", async t => {
  const f = fixture(t), missing = path.join(f.root, "absent", "profile.json");
  assert.equal(await readDiscoveryProfile(missing), undefined);
  await assert.rejects(selectDiscoveryInstitution(missing, "school-a"));
  assert.equal(fs.existsSync(path.dirname(missing)), false);
});

test("credential fields and unsafe endpoints are rejected before any persistent write", async () => {
  assert.throws(() => validateDiscoveryProfile({...profile(), password: "private"}));
  for (const apiUrl of ["http://api.example", "https://user:pass@api.example", "https://api.example/path", "https://192.0.2.1", "https://api.example?token=x", "https://api.example:0"]) {
    assert.throws(() => validateDiscoveryProfile({...profile(), trustedServers: [{...profile().trustedServers[0], apiUrl}]}));
  }
  assert.throws(() => validateDiscoveryProfile({...profile(), selectedInstitutionId: "unknown"}));
  assert.throws(() => validateDiscoveryProfile({...profile(), trustedServers: [{...profile().trustedServers[0], token: "private"}]}));
  assert.throws(() => validateDiscoveryProfile({...profile(), trustedServers: [profile().trustedServers[0], profile().trustedServers[0]]}));
});

test("shared permissions, links and malformed content cannot be read or rewritten", {skip: process.platform === "win32"}, async t => {
  const cases: Array<[string, (f: {root: string; file: string}) => void]> = [
    ["file permissions", f => fs.chmodSync(f.file, 0o644)],
    ["parent permissions", f => fs.chmodSync(f.root, 0o755)],
    ["hardlink", f => fs.linkSync(f.file, path.join(f.root, "link.json"))],
    ["symlink", f => { fs.renameSync(f.file, `${f.file}.real`); fs.symlinkSync(`${f.file}.real`, f.file); }],
    ["oversize", f => fs.writeFileSync(f.file, " ".repeat(32769))],
    ["invalid JSON", f => fs.writeFileSync(f.file, "{")],
    ["private fields", f => fs.writeFileSync(f.file, JSON.stringify({...profile(), token: "secret"}))],
  ];
  for (const [name, change] of cases) await t.test(name, async t => {
    const f = fixture(t); change(f);
    const before = fs.readFileSync(f.file);
    await assert.rejects(readDiscoveryProfile(f.file));
    await assert.rejects(selectDiscoveryInstitution(f.file, "school-a"));
    assert.deepEqual(fs.readFileSync(f.file), before);
    assert.equal(fs.readdirSync(f.root).some(name => name.endsWith(".tmp")), false);
  });
});

test("symlinked configuration parent is rejected", {skip: process.platform === "win32"}, async t => {
  const f = fixture(t), alias = path.join(f.root, "alias"); fs.symlinkSync(f.root, alias);
  await assert.rejects(readDiscoveryProfile(path.join(alias, "institution-discovery.json")));
});
