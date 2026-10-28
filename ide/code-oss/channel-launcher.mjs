// Stable icon entrypoint. The approved release's original launcher still owns student cleanup.
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchActive } from './release-channel.mjs';

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
  return {store: selection.store, context};
}
export async function runChannel(file, report = event => process.stdout.write(`${JSON.stringify(event)}\n`)) {
  try {
    const selection = loadChannelConfiguration(file);
    const requiredScript = {darwin: 'mac-launcher.mjs', linux: 'linux-launcher.mjs'}[process.platform];
    const result = await launchActive({...selection, requiredScript});
    if (result.code !== 0) throw new Error('Approved IDE launcher failed');
    // The inner shared launcher emits its actual session-cleaned receipt. Do not invent one here.
    return 0;
  } catch {
    report({event: 'error', message: '승인된 IDE 버전 또는 실습 정리 상태를 확인할 수 없습니다. 담당자에게 업데이트 승인·서명·버전 호환성과 이전 실습 종료를 확인해 달라고 요청하세요. 학생 코드와 로그인 정보를 다른 설치본에 전달하지 않았습니다.'});
    return 1;
  }
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await runChannel(path.join(path.dirname(fileURLToPath(import.meta.url)), 'channel.json'));
}
