"""Validation completion reloads the current filtered URL without a new target."""
import shutil
import subprocess

import pytest

from autograde.instructor_browser import SCRIPT


HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = require('node:fs').readFileSync(0, 'utf8');
class Element {
  constructor(values = {}) { Object.assign(this, {listeners: {}, textContent: '', hidden: false}, values); }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
}
async function scenario(segment, terminalStatus) {
  const href = `https://class.example/courses/come2201/instructor/${segment}?q=Week10&visibility=draft&page=2`;
  const statusUrl = '/courses/come2201/instructor/checks/check_one/status';
  const message = new Element(), toggle = new Element(), checked = new Element();
  const panel = new Element({dataset: {checkPoll: statusUrl, jobId: 'check_one', revision: '1'}});
  panel.querySelector = selector => ({'[data-check-message]': message, '[data-check-toggle]': toggle, '[data-check-time]': checked})[selector];
  const document = new Element();
  document.querySelector = () => null;
  document.querySelectorAll = selector => selector === '[data-check-poll]' ? [panel] : [];
  const window = new Element(), reloads = [], requests = [], timers = new Map();
  const location = {reload() { reloads.push(this.href); }};
  Object.defineProperty(location, 'href', {get: () => href, set() { throw Error('Unexpected URL replacement'); }});
  Object.defineProperty(window, 'location', {get: () => location, set() { throw Error('Unexpected navigation'); }});
  let nextTimer = 0, responseStatus = 'running';
  const environment = {window, document, navigator: {onLine: true}, AbortController,
    setTimeout(callback) { const id = ++nextTimer; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url, options) {
      requests.push({url, options});
      return {ok: true, status: 200, async json() {
        return {job_id: 'check_one', revision: 1, draft_revision: 1, status: responseStatus};
      }};
    }};
  for (const key of ['localStorage', 'sessionStorage', 'indexedDB']) {
    Object.defineProperty(environment, key, {get() { throw Error('Unexpected persistence'); }});
  }
  vm.runInNewContext(source, environment);
  const tick = async () => {
    const [id, callback] = [...timers][0]; timers.delete(id); await callback();
  };
  await tick();
  assert.equal(reloads.length, 0, 'running verification does not navigate');
  responseStatus = terminalStatus;
  await tick();
  assert.deepEqual(reloads, [href], 'completion preserves the entire current URL');
  assert.equal(requests.length, 2);
  assert.ok(requests.every(({url, options}) => url === statusUrl && options.credentials === 'same-origin' && options.redirect === 'error'));
  assert.equal(timers.size, 0, 'terminal verification stops polling');
}
(async () => {
  for (const segment of ['drafts/draft_one', 'checks/check_one']) {
    for (const status of ['succeeded', 'failed', 'interrupted']) await scenario(segment, status);
  }
  console.log('validation reload keeps catalog context');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def test_check_completion_reloads_filtered_draft_and_check_urls():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for deterministic browser behavior checks')
    result = subprocess.run([node, '-e', HARNESS], input=SCRIPT, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'validation reload keeps catalog context' in result.stdout
