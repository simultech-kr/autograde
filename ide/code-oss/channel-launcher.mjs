// Stable icon entrypoint. The approved release's original launcher still owns student cleanup.
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchActive } from './release-channel.mjs';
import {acquireChannelLease, preparePendingUpdate, failUpdateTrial, confirmUpdateTrial, watchForUpdates, updateSettings, cleanupInterruptedDownloads} from './auto-update.mjs';

function readObject(file) {
  const stat = fs.lstatSync(file);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size > 1024 * 1024
    || process.platform !== 'win32' && stat.mode & 0o022) throw new Error('Unsafe channel configuration');
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}
export function loadChannelConfiguration(file) {
  const selection = readObject(file);
  if (selection.schema !== 1 || !path.isAbsolute(selection.store ?? '') || !path.isAbsolute(selection.context ?? '')) throw new Error('Invalid channel location');
  const context = readObject(selection.context);
  if (context.platform !== process.platform || context.architecture !== process.arch) throw new Error('Channel does not match this device');
  if (!Array.isArray(context.sessionRoots) || !context.sessionRoots.length || context.sessionRoots.some(root => {
    if (!path.isAbsolute(root)) return true;
    return [selection.store, selection.context, file].some(candidate => {
      const relative = path.relative(root, path.resolve(candidate));
      return !relative || relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
    });
  })) throw new Error('Release store and configuration must be outside student sessions');
  return {store: selection.store, context};
}
export async function runChannel(file, report = event => process.stdout.write(`${JSON.stringify(event)}\n`), {watch = watchForUpdates} = {}) {
  let releaseLease, updates;
  const controller = new AbortController();
  let stopping = false;
  const stop = () => { stopping = true; controller.abort(); };
  try {
    const selection = loadChannelConfiguration(file);
    releaseLease = acquireChannelLease(selection.store);
    cleanupInterruptedDownloads(selection.store);
    let trial;
    // Invalid update configuration must not prevent a still-authorized installed
    // release from running. An unconfirmed failed trial is different: fail closed.
    let configured = false;
    try { configured = Boolean(updateSettings(selection.context)); }
    catch { report({event: 'update-status', phase: 'kept-current', reason: 'invalid-update-configuration'}); }
    trial = preparePendingUpdate({...selection, report, allowActivation: configured});
    const requiredScript = {darwin: 'mac-launcher.mjs', linux: 'linux-launcher.mjs'}[process.platform];
    for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(signal, stop);
    // Network failures stay isolated from launch and never delay starting class.
    if (configured) updates = watch({...selection, report, signal: controller.signal}).catch(() => {
      report({event: 'update-status', phase: 'kept-current', reason: 'update-check-failed'});
    });
    let started = false, cleaned = false, childError;
    const observe = event => {
      if (event.event === 'ide-started') { started = true; cleaned = false; }
      if (event.event === 'session-cleaned') cleaned = true;
      if (event.event === 'error') { childError = event; return; }
      report(event);
    };
    let result;
    try { result = await launchActive({...selection, requiredScript, onEvent: observe}); }
    catch { result = {code: 1}; }
    controller.abort(); await updates;
    if (trial) {
      try {
        if (result.code !== 0 || childError) throw new Error('New IDE failed');
        confirmUpdateTrial({...selection, cleaned, started});
      } catch {
        const recovery = failUpdateTrial({...selection, report});
        if (recovery.restored && !started && !stopping) {
          // No student has started this failed build. Start the verified previous
          // one only after its predecessor's cleanup gate allowed the rollback.
          childError = undefined; cleaned = false;
          result = await launchActive({...selection, requiredScript, onEvent: observe});
        } else result = {code: 1};
      }
    }
    if (childError) report(childError);
    if (result.code !== 0 || childError) throw new Error('Approved IDE launcher failed');
    // The inner shared launcher emits its actual session-cleaned receipt. Do not invent one here.
    return 0;
  } catch {
    report({event: 'error', message: '승인된 IDE 버전 또는 실습 정리 상태를 확인할 수 없습니다. 담당자에게 업데이트 승인·서명·버전 호환성과 이전 실습 종료를 확인해 달라고 요청하세요. 학생 코드와 로그인 정보를 다른 설치본에 전달하지 않았습니다.'});
    return 1;
  } finally {
    controller.abort(); await updates;
    for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.removeListener(signal, stop);
    releaseLease?.();
  }
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await runChannel(path.join(path.dirname(fileURLToPath(import.meta.url)), 'channel.json'));
}
