// Public institution choices survive a disposable student session. This module
// never writes the profile or modifies DNS, certificates, hosts or app resources.
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { isIP } from 'node:net';

const fail = () => { throw new Error('기관 자동 검색 설정을 확인할 수 없습니다. 담당자가 설정 파일과 접근 권한을 확인해 주세요.'); };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const keysAre = (value, keys) => Object.keys(value).every(key => keys.includes(key));
const identifier = value => typeof value === 'string' && /^[a-z0-9][a-z0-9-]{0,63}$/.test(value);

function origin(value) {
  if (typeof value !== 'string' || !value || value.length > 2048 || /[^\x21-\x7e]/.test(value)) fail();
  let url;
  try { url = new URL(value); } catch { fail(); }
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || url.pathname !== '/' || url.port === '0'
    || isIP(url.hostname) || url.hostname.includes(':') || url.hostname.endsWith('.') || !url.hostname.includes('.')
    || url.hostname.length > 253 || url.hostname.split('.').some(label => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) fail();
  return url.origin;
}

export function validateDiscoveryProfile(value) {
  if (!object(value) || !keysAre(value, ['schema', 'trustedServers', 'selectedInstitutionId'])
    || value.schema !== 'autograde.ide-discovery.v1' || !Array.isArray(value.trustedServers)
    || value.trustedServers.length < 1 || value.trustedServers.length > 32) fail();
  const ids = new Set();
  const trustedServers = value.trustedServers.map(server => {
    if (!object(server) || !keysAre(server, ['institutionId', 'displayName', 'apiUrl', 'portalUrl'])
      || !identifier(server.institutionId) || ids.has(server.institutionId)
      || typeof server.displayName !== 'string' || !server.displayName.trim() || server.displayName.length > 100
      || /[\u0000-\u001f\u007f]/.test(server.displayName)) fail();
    ids.add(server.institutionId);
    const apiUrl = origin(server.apiUrl), portalUrl = origin(server.portalUrl);
    if (apiUrl === portalUrl) fail();
    return {institutionId: server.institutionId, displayName: server.displayName, apiUrl, portalUrl};
  });
  if (value.selectedInstitutionId !== undefined && (!identifier(value.selectedInstitutionId) || !ids.has(value.selectedInstitutionId))) fail();
  return {schema: 'autograde.ide-discovery.v1', trustedServers,
    ...(value.selectedInstitutionId !== undefined ? {selectedInstitutionId: value.selectedInstitutionId} : {})};
}

export function machineDiscoveryPath(home = os.homedir()) {
  return path.join(home, 'Library', 'Application Support', 'Autograde IDE', 'institution-discovery.json');
}

function checkPath(file) {
  if (!path.isAbsolute(file) || path.normalize(file) !== file) fail();
  let current = path.parse(file).root;
  for (const name of path.relative(current, path.dirname(file)).split(path.sep).filter(Boolean)) {
    current = path.join(current, name);
    const info = fs.lstatSync(current);
    if (!info.isDirectory() || info.isSymbolicLink()) fail();
  }
  const parent = fs.lstatSync(path.dirname(file));
  if (parent.uid !== process.getuid() || (parent.mode & 0o077)) fail();
}

export function loadMachineDiscoveryProfile(file = machineDiscoveryPath()) {
  if (file === null) return undefined;
  if (typeof file !== 'string' || !file || !path.isAbsolute(file) || path.normalize(file) !== file) fail();
  try { fs.lstatSync(file); } catch (error) { if (error.code === 'ENOENT') return undefined; throw error; }
  checkPath(file);
  let fd;
  try {
    // O_NOFOLLOW prevents a swapped final symlink from becoming a credential read.
    fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
    const info = fs.fstatSync(fd);
    if (!info.isFile() || info.nlink !== 1 || info.uid !== process.getuid() || (info.mode & 0o077) || info.size > 32768) fail();
    const profile = validateDiscoveryProfile(JSON.parse(fs.readFileSync(fd, 'utf8')));
    const selected = profile.trustedServers.find(server => server.institutionId === profile.selectedInstitutionId);
    return {path: file, profile, settings: {
      'autograde.discovery.enabled': true,
      'autograde.discovery.trustedServers': profile.trustedServers,
      ...(selected ? {'autograde.serviceBaseUrl': selected.apiUrl, 'autograde.studentPortalBaseUrl': selected.portalUrl} : {}),
    }};
  } catch { fail(); } finally { if (fd !== undefined) fs.closeSync(fd); }
}
