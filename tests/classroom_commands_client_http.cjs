// Real compiled extension protocol against the temporary Python HTTP service.
// UI confirmation is synthetic; this does not control a real student's IDE.
const assert = require('node:assert/strict');
const {AutogradeClient, HttpTransport, TokenManager} = require('../extensions/vscode/out/api.js');
const {ClassroomCommands} = require('../extensions/vscode/out/classroomCommands.js');

(async () => {
  let raw = '';
  for await (const chunk of process.stdin) raw += chunk;
  const input = JSON.parse(raw);
  const transport = new HttpTransport(() => input.url);
  const tokens = new TokenManager(transport);
  await tokens.storeSession({access_token: input.token, refresh_token: 'synthetic-refresh-unused', expires_in: 3600}, input.url);
  const client = new AutogradeClient(transport, tokens);
  const context = {baseUrl: input.url, generation: tokens.getSessionGeneration(), assignmentId: 'asn_come3105', clientSessionId: 'synthetic-macos-001'};
  let confirmed = 0, executed = 0, controller;
  await new Promise((resolve, reject) => {
    const deadline = setTimeout(() => {controller.dispose(); reject(new Error('IDE request did not finish'));}, 8000);
    controller = new ClassroomCommands(client, () => context, async command => {
      assert.equal(command.command_id, input.command_id);
      assert.equal(command.kind, 'notice');
      confirmed++;
      return input.approve;
    }, async (_command, guard) => {
      guard(); executed++;
      return {status: 'completed', result_code: 'notice_acknowledged'};
    }, text => {
      if (text === '교수자 요청 처리·결과 전달 완료' || text === '교수자 요청을 거절했습니다.') {
        clearTimeout(deadline);
        setImmediate(() => {controller.dispose(); resolve();});
      }
    });
    controller.reconcile();
  });
  assert.equal(confirmed, 1);
  assert.equal(executed, input.approve ? 1 : 0);
  assert.deepEqual(await client.pollClassroomCommands(context, new AbortController().signal), []);
  process.stdout.write(JSON.stringify({confirmed, executed}));
})().catch(() => {process.stderr.write('Compiled IDE client HTTP interoperability failed\n'); process.exitCode = 1;});
