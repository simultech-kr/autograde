// Optional machine-only reporting. No student identifiers, local paths or code.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { randomUUID } from 'node:crypto';

export function seatConfigPath(platform = process.platform, home = os.homedir()) {
  return path.join(home, ...(platform === 'darwin'
    ? ['Library', 'Application Support', 'Autograde Operator', 'device.json']
    : platform === 'win32' ? ['Autograde Operator', 'device.json'] : ['.config', 'autograde-operator', 'device.json']));
}

export function loadSeatConfig(file = seatConfigPath(), platform = process.platform) {
  if (!fs.existsSync(file)) return undefined;
  const info = fs.lstatSync(file);
  if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || info.size > 4096 ||
      (platform !== 'win32' && ((info.mode & 0o077) || (info.uid !== process.getuid() && info.uid !== 0)))) throw new Error('device_config_invalid');
  for (let parent = path.dirname(file); parent !== path.dirname(parent); parent = path.dirname(parent)) {
    if (fs.lstatSync(parent).isSymbolicLink()) throw new Error('device_config_invalid');
  }
  const config = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (!config || Object.keys(config).sort().join(',') !== 'apiUrl,reportToken,seatId' ||
      !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(config.seatId ?? '') || !/^[A-Za-z0-9_-]{43}$/.test(config.reportToken ?? '')) throw new Error('device_config_invalid');
  const origin = new URL(config.apiUrl);
  if (origin.protocol !== 'https:' || origin.username || origin.password || origin.search || origin.hash || origin.pathname !== '/') throw new Error('device_config_invalid');
  return Object.freeze(config);
}

export function createSeatReporter({ configFile, version = 'unknown', extensionVersion = version,
  platform = process.platform, architecture = process.arch, send = globalThis.fetch,
  onWarning = () => {}, intervalMilliseconds = 60000 } = {}) {
  let config;
  try { config = loadSeatConfig(configFile, platform); }
  catch { onWarning('device_config_invalid'); }
  if (!config) return { event() {}, async close() {} };
  let cleanup = 'unknown', stopped = false, queue = Promise.resolve(), pending = 0;
  const report = () => {
    if (pending >= 2) return queue; // Never accumulate unbounded offline reports.
    pending += 1;
    queue = queue.then(async () => {
      try {
        const response = await send(new URL('/v1/operations/seats/report', config.apiUrl), {
          method: 'POST', redirect: 'error', signal: AbortSignal.timeout(3000),
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${config.reportToken}` },
          body: JSON.stringify({ seat_id: config.seatId, report_id: randomUUID(), ide_version: version,
            extension_version: extensionVersion, platform, architecture, cleanup, update: 'unknown' }),
        });
        // A rate-limited quick exit remains unknown; never claim a failed report succeeded.
        await response.body?.cancel();
        if (!response.ok) onWarning('device_report_unconfirmed');
      } catch { onWarning('device_report_unconfirmed'); }
      finally { pending -= 1; }
    });
    return queue;
  };
  const timer = setInterval(() => { if (!stopped) void report(); }, intervalMilliseconds);
  timer.unref?.();
  return {
    event(event) {
      if (stopped) return;
      if (event.event === 'session-cleaned') cleanup = 'ok';
      // Generic startup errors do not prove cleanup failed.
      if (event.event === 'cleanup-failed') cleanup = 'failed';
      if (['ide-started', 'session-cleaned', 'cleanup-failed'].includes(event.event)) void report();
    },
    async close() { stopped = true; clearInterval(timer); await queue; },
  };
}
