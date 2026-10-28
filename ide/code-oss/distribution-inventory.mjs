import * as fs from 'node:fs';
import * as path from 'node:path';
import { randomUUID } from 'node:crypto';
import { canonical, digest } from './release-channel.mjs';

export function institutionProfile(value) {
  if (!value || value.schema !== 'autograde.ide-institution.v1' || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(value.institutionId ?? '')
    || Object.keys(value).some(key => !['schema', 'institutionId', 'apiUrl', 'portalUrl'].includes(key))) throw new Error('Institution profile must contain only public institution and endpoint fields');
  const origin = raw => {
    const url = new URL(raw);
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('Institution endpoints must be HTTPS origins without credentials');
    return url.origin;
  };
  return {schema: value.schema, institutionId: value.institutionId, apiUrl: origin(value.apiUrl), portalUrl: origin(value.portalUrl)};
}
function readRegular(file, max = 4 * 1024 * 1024) {
  const stat = fs.lstatSync(file);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size > max) throw new Error('Distribution input must be a bounded regular file');
  return fs.readFileSync(file);
}
/** Called inside an owned build directory, before any OS signing or archive creation. */
export function applyInstitutionProfile(runtimeDirectory, profileFile) {
  const profile = institutionProfile(JSON.parse(readRegular(profileFile).toString('utf8')));
  const settingsFile = path.join(runtimeDirectory, 'runtime-settings.json'), settings = JSON.parse(readRegular(settingsFile).toString('utf8'));
  settings['autograde.serviceBaseUrl'] = profile.apiUrl;
  settings['autograde.studentPortalBaseUrl'] = profile.portalUrl;
  const profilePath = path.join(runtimeDirectory, 'institution-profile.json');
  fs.writeFileSync(profilePath, `${JSON.stringify(profile, null, 2)}\n`, {flag: 'wx', mode: 0o644});
  fs.writeFileSync(settingsFile, `${JSON.stringify(settings, null, 2)}\n`);
  return {profile, sha256: digest(canonical(profile))};
}

/** CycloneDX inventory of shipped package manifests plus the bundled native runtimes.
 * License declarations are evidence for review, never a redistribution approval.
 */
export function distributionInventory(root, {ideVersion, extensionVersion, nodeVersion, upstreamCommit}) {
  root = path.resolve(root);
  if (!fs.lstatSync(root).isDirectory() || fs.lstatSync(root).isSymbolicLink()) throw new Error('Distribution root must be a real directory');
  const packages = [], notices = []; let electronVersion = 'unknown';
  const relative = file => path.relative(root, file).split(path.sep).join('/');
  let visited = 0;
  function walk(directory) {
    for (const name of fs.readdirSync(directory).sort()) {
      if (++visited > 200000) throw new Error('Distribution file limit exceeded');
      const file = path.join(directory, name), stat = fs.lstatSync(file);
      // Link contents are represented by the signed release inventory, not followed twice here.
      if (stat.isSymbolicLink()) continue;
      if (stat.isDirectory()) { walk(file); continue; }
      if (!stat.isFile()) throw new Error('Unsupported distribution entry');
      if (name === 'Electron-version' || name === 'version' && /(?:^|\/)electron\//i.test(relative(file))) {
        const version = readRegular(file, 1024).toString('utf8').trim().replace(/^v/, '');
        if (/^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$/.test(version)) electronVersion = version;
      }
      if (/^(?:licen[cs]e|copying|notice|thirdpartynotices)(?:[.-].*)?$/i.test(name)) notices.push({path: relative(file), sha256: digest(readRegular(file, 100 * 1024 * 1024))});
      if (name !== 'package.json') continue;
      const bytes = readRegular(file), pkg = JSON.parse(bytes.toString('utf8'));
      if (typeof pkg.name !== 'string' || typeof pkg.version !== 'string') continue;
      const license = typeof pkg.license === 'string' ? pkg.license : typeof pkg.license?.type === 'string' ? pkg.license.type : 'NOASSERTION';
      packages.push({type: pkg.publisher ? 'application' : 'library', 'bom-ref': `package:${relative(file)}`, name: pkg.name, version: pkg.version,
        licenses: [{license: {name: license}}], hashes: [{alg: 'SHA-256', content: digest(bytes)}],
        properties: [{name: 'autograde:manifest', value: relative(file)}, {name: 'autograde:license-reviewed', value: 'false'}]});
    }
  }
  walk(root);
  const components = [
    {type: 'application', 'bom-ref': 'autograde-ide', name: 'Autograde IDE', version: ideVersion},
    {type: 'application', 'bom-ref': 'autograde-extension', name: 'Autograde extension', version: extensionVersion, licenses: [{license: {name: 'UNLICENSED'}}]},
    {type: 'application', 'bom-ref': 'node-runtime', name: 'Node.js', version: nodeVersion, licenses: [{license: {name: 'See bundled Node LICENSE'}}]},
    {type: 'application', 'bom-ref': 'electron-runtime', name: 'Electron', version: electronVersion, licenses: [{license: {name: 'See bundled Electron LICENSE and third-party notices'}}]},
    {type: 'application', 'bom-ref': 'code-oss', name: 'Code-OSS', version: ideVersion, properties: [{name: 'autograde:upstream-commit', value: upstreamCommit}]},
    ...packages,
  ];
  return {bomFormat: 'CycloneDX', specVersion: '1.6', serialNumber: `urn:uuid:${randomUUID()}`, version: 1,
    metadata: {timestamp: new Date().toISOString(), component: {type: 'application', name: 'Autograde IDE distribution', version: ideVersion},
      properties: [{name: 'autograde:license-review-complete', value: 'false'}, {name: 'autograde:inventory-scope', value: 'Shipped package manifests and bundled runtimes; native/transitive attribution requires review'}]},
    components, properties: notices.map(item => ({name: `autograde:notice:${item.path}`, value: item.sha256}))};
}
export function prepareDistribution(root, runtimeDirectory, metadata, profileFile) {
  const profile = profileFile ? applyInstitutionProfile(runtimeDirectory, profileFile) : undefined;
  const sbom = distributionInventory(root, metadata);
  fs.writeFileSync(path.join(runtimeDirectory, 'sbom.cdx.json'), `${JSON.stringify(sbom, null, 2)}\n`, {flag: 'wx', mode: 0o644});
  return {institutionProfileSha256: profile?.sha256 ?? null, sbomComponents: sbom.components.length, licenseReviewComplete: false};
}
