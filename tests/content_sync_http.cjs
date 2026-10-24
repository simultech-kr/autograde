/* Real extension modules against the synthetic Python HTTP test server. */
const fs = require('node:fs/promises');
const path = require('node:path');
const {HttpTransport, AutogradeClient} = require('../extensions/vscode/out/api.js');
const {ContentApi} = require('../extensions/vscode/out/contentApi.js');
const {syncContent, exportContent} = require('../extensions/vscode/out/contentSync.js');

(async () => {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  let objectRequests = 0;
  const transport = new HttpTransport(() => input.origin, async (url, init) => {
    if (new URL(url).pathname.includes('/objects/')) objectRequests++;
    return fetch(url, init);
  });
  const tokens = {getSessionGeneration: () => 1, hasSession: async () => true,
    getAccessToken: async () => input.token, storeSession: async () => {}, clear: async () => {}};
  const api = new ContentApi(new AutogradeClient(transport, tokens));
  const context = await api.context();
  const catalog = await api.catalog(context);
  if (catalog.collections.length !== 1) throw new Error('Expected one synthetic collection');
  const manifest = await api.manifest(catalog.collections[0].release_id, context);
  const options = {root: input.root, origin: input.origin, context, assertCurrent: () => {},
    assignmentDirectories: [input.workspace]};
  const first = await syncContent(api, manifest, options);
  const beforeRepeat = objectRequests;
  const repeat = await syncContent(api, manifest, options);
  if (objectRequests !== beforeRepeat) throw new Error('Unchanged repeat requested file bytes');
  const exported = await exportContent(api, manifest, options, input.exports);
  for (const file of manifest.files) {
    const content = await fs.readFile(path.join(exported, file.path));
    if (content.length !== file.size_bytes) throw new Error('Export size mismatch');
  }
  process.stdout.write(JSON.stringify({generation: manifest.generation, downloaded: first.downloadedFiles,
    reused: first.reusedFiles, repeated: repeat.downloadedFiles, objectRequests, exported}));
})().catch(error => {
  // No tokens, HTTP headers or arbitrary server responses in test output.
  process.stderr.write(`content sync failed: ${error.name} ${error.code || ''} ${error.status || ''}: ${error.message}\n`);
  process.exitCode = 1;
});
