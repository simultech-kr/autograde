"""User-visible save continuity with a deterministic DOM/fetch harness."""
import shutil
import subprocess

import pytest

from autograde.instructor_browser import SCRIPT


HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = require('node:fs').readFileSync(0, 'utf8');
const path = '/courses/come2201/instructor/drafts/draft_test';
class Element {
  constructor(props = {}) { Object.assign(this, {listeners: {}, attrs: {}, dataset: {}, textContent: '', disabled: false, checked: false, type: 'text', name: '', value: ''}, props); }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  fire(type, event = {}) { for (const callback of this.listeners[type] || []) callback(event); }
  getAttribute(name) { return this.attrs[name] ?? null; }
  hasAttribute(name) { return Object.hasOwn(this.attrs, name); }
  setAttribute(name, value) { this.attrs[name] = value; }
  removeAttribute(name) { delete this.attrs[name]; }
}
const input = (name, value, type = 'text') => new Element({name, value, type});
function file(name) {
  const el = input('file', '', 'file');
  el.files = [{name, size: 30, lastModified: 15}];
  Object.defineProperty(el, 'value', {get() { return el.files.length ? 'selected.zip' : ''; }, set(value) { if (value === '') el.files = []; }});
  return el;
}
class Form extends Element {
  constructor(action, kind, elements = [], multipart = false, inline = true) {
    super({attrs: {action, ...(inline ? {'data-inline-draft-save': '', 'data-dirty-guard': ''} : {})}, dataset: {formKind: kind},
      enctype: multipart ? 'multipart/form-data' : 'application/x-www-form-urlencoded'});
    this.status = new Element();
    this.elements = [input('csrf', 'csrf-private', 'hidden'), input('revision', '1', 'hidden'), ...elements, input('', '', 'submit')];
  }
  field(name) { return this.elements.find(el => el.name === name); }
  querySelectorAll(selector) {
    return ({'input[type=file]': this.elements.filter(el => el.type === 'file'),
      'input[type=checkbox]': this.elements.filter(el => el.type === 'checkbox'),
      'button[type=submit],button:not([type])': this.elements.filter(el => el.type === 'submit')})[selector] || [];
  }
  querySelector(selector) { return selector === '[data-save-status]' ? this.status : null; }
}
class FormData {
  constructor(form) { this.map = new Map(form.elements.filter(el => el.name && !el.disabled && (!['checkbox', 'radio'].includes(el.type) || el.checked)).map(el => [el.name, el.type === 'file' ? el.files[0] : el.value])); }
  set(key, value) { this.map.set(key, value); }
  get(key) { return this.map.get(key); }
  [Symbol.iterator]() { return this.map.entries(); }
}
function environment() {
  const problem = new Form(path, 'problem', [input('title', 'Original'), input('description', 'Original description')]);
  const tests = new Form(path, 'tests', [input('tests_present', 'yes', 'hidden'), input('test_0_title', 'Original case'), input('test_0_weight', '100'), input('negative_score', '0')]);
  const starter = new Form(path + '/uploads/starter', '', [input('starter_confirm', 'yes', 'checkbox'), file('starter.zip')], true);
  const grading = new Form(path + '/grading-template', '', [input('grading_confirm', 'yes', 'checkbox'), file('grading.zip')], true);
  const checks = new Form(path + '/checks', '', [], false, false);
  const forms = [problem, tests, starter, grading, checks];
  const document = new Element(); const window = new Element();
  const overview = new Element(); const published = new Element();
  document.querySelector = selector => ({'[data-draft-save-overview]': overview, '#draft-publish': published})[selector] || null;
  document.querySelectorAll = selector => ({'form[data-dirty-guard]': forms.filter(f => f.hasAttribute('data-dirty-guard')), 'form': forms})[selector] || [];
  window.location = {href: 'https://class.example' + path, origin: 'https://class.example', reload() { throw Error('Unexpected navigation'); }};
  window.confirm = () => { throw Error('Unexpected data-loss confirmation'); };
  const requests = [];
  const context = {window, document, URL, URLSearchParams, FormData, AbortController, Event, setTimeout: () => 1, clearTimeout() {},
    fetch(url, options) { return new Promise((resolve, reject) => { requests.push({url, options, resolve, reject}); }); }};
  for (const key of ['localStorage', 'sessionStorage', 'indexedDB']) Object.defineProperty(context, key, {get() { throw Error('Unexpected persistence'); }});
  vm.runInNewContext(source, context);
  const submit = form => { const event = {target: form, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }}; document.fire('submit', event); return event; };
  const dirtyExit = () => { const event = {prevented: false, preventDefault() { this.prevented = true; }}; window.fire('beforeunload', event); return event.prevented; };
  return {problem, tests, starter, grading, checks, forms, document, requests, submit, dirtyExit, overview, published};
}
const receipt = (previous = 1, changes = {}) => ({ok: true, path, previous_revision: previous, revision: previous + 1,
  values: {}, summary: ['deadline', 'starter', 'grading', 'tests', 'recheck'], uploads: {}, can_check: true, title: 'Saved', updated_at: 'now', grading_status: 'stored, not validated', ...changes});
const finish = async (request, data = receipt(), status = 200, type = 'application/json') => {
  request.resolve({ok: status >= 200 && status < 300, status, headers: {get() { return type; }}, async json() { return data; }});
  await new Promise(resolve => setImmediate(resolve));
};

(async () => {
  let env = environment();
  env.problem.field('title').value = 'New title'; env.tests.field('test_0_title').value = 'Unsaved case';
  const chosen = env.starter.field('file').files;
  assert.equal(env.submit(env.problem).defaultPrevented, true);
  assert.equal(env.requests[0].options.credentials, 'same-origin');
  assert.equal(env.requests[0].options.redirect, 'error');
  assert.equal(env.requests[0].options.body.get('csrf'), 'csrf-private');
  assert.equal(env.requests[0].options.body.get('inline_save'), 'yes');
  env.submit(env.tests); assert.equal(env.requests.length, 1, 'serialize mutations');
  assert.equal(env.dirtyExit(), true, 'pending writes guard page exit');
  await finish(env.requests[0], receipt(1, {values: {title: 'Validated title'}}));
  assert.equal(env.problem.field('title').value, 'Validated title');
  assert.equal(env.tests.field('test_0_title').value, 'Unsaved case');
  assert.equal(env.starter.field('file').files, chosen, 'never replace other file controls');
  assert.ok(env.forms.every(f => f.field('revision').value === '2'));
  assert.match(env.problem.status.textContent, /저장 완료/);
  assert.match(env.overview.textContent, /미저장 입력/);
  assert.match(env.published.textContent, /검증한 뒤 공개/);

  env = environment(); env.problem.field('title').value = 'Draft';
  env.submit(env.problem);
  env.problem.field('description').value = 'Typed while request was in flight';
  await finish(env.requests[0], receipt(1, {values: {title: 'Draft', description: 'Older response'}}));
  assert.equal(env.problem.field('description').value, 'Typed while request was in flight');
  assert.match(env.problem.status.textContent, /아직 미저장/);
  assert.equal(env.dirtyExit(), true);
  env.submit(env.problem);
  assert.equal(env.requests[1].options.body.get('revision'), '2');
  await finish(env.requests[1], receipt(2));

  env = environment(); const otherFile = env.grading.field('file').files;
  env.starter.field('starter_confirm').checked = true;
  env.submit(env.starter);
  assert.match(env.starter.status.textContent, /아직 저장 완료가 아닙니다/);
  await finish(env.requests[0], receipt());
  assert.equal(env.starter.field('file').files.length, 0);
  assert.equal(env.grading.field('file').files, otherFile);
  assert.match(env.starter.status.textContent, /저장 완료/);

  env = environment(); env.problem.field('title').value = 'Keep invalid input';
  env.tests.field('test_0_title').value = 'Keep other edits'; const failureFiles = env.grading.field('file').files;
  env.submit(env.problem);
  await finish(env.requests[0], {ok: false, error: {code: 'draft_input', message: 'Check schedule'}}, 400);
  assert.equal(env.problem.field('title').value, 'Keep invalid input');
  assert.equal(env.tests.field('test_0_title').value, 'Keep other edits');
  assert.equal(env.grading.field('file').files, failureFiles);
  assert.ok(env.forms.every(f => f.field('revision').value === '1'));
  assert.match(env.problem.status.textContent, /Check schedule/);
  env.submit(env.problem); assert.equal(env.requests.length, 2, 'known input errors allow explicit retry');
  await finish(env.requests[1]);

  for (const failure of ['network', 'html', 'auth', 'conflict', 'wrong-revision', 'server']) {
    env = environment(); env.problem.field('title').value = 'Never discard';
    const files = env.starter.field('file').files;
    env.submit(env.problem);
    if (failure === 'network') { env.requests[0].reject(Error('offline')); await new Promise(resolve => setImmediate(resolve)); }
    else if (failure === 'html') await finish(env.requests[0], {}, 200, 'text/html');
    else if (failure === 'wrong-revision') await finish(env.requests[0], receipt(2));
    else await finish(env.requests[0], {error: {code: 'error', message: 'not trusted'}}, {auth: 401, conflict: 409, server: 500}[failure]);
    assert.equal(env.problem.field('title').value, 'Never discard', failure);
    assert.equal(env.starter.field('file').files, files, failure);
    assert.ok(env.forms.every(f => f.field('revision').value === '1'), failure);
    assert.match(env.problem.status.textContent, /새 탭/, failure);
    env.submit(env.problem); assert.equal(env.requests.length, 1, 'no uncertain automatic or blind manual replay: ' + failure);
  }

  env = environment(); env.tests.field('test_0_title').value = 'Unsaved case';
  env.submit(env.grading); assert.equal(env.requests.length, 0, 'import cannot overwrite dirty tests');
  assert.match(env.grading.status.textContent, /미저장 변경/);
  env.submit(env.tests); await finish(env.requests[0], receipt());
  env.submit(env.grading);
  await finish(env.requests[1], receipt(2, {tests_replaced: true, values: {test_0_title: 'Imported case', test_0_weight: 50}}));
  assert.equal(env.tests.field('test_0_title').value, 'Imported case');
  assert.equal(env.tests.field('test_0_weight').value, '50');

  env = environment(); env.submit(env.grading);
  env.tests.field('test_0_title').value = 'Entered during import';
  await finish(env.requests[0], receipt(1, {tests_replaced: true, values: {test_0_title: 'Do not clobber'}}));
  assert.equal(env.tests.field('test_0_title').value, 'Entered during import');
  assert.equal(env.tests.field('revision').value, '1', 'overlapping edited form must retain original revision fence');
  assert.equal(env.problem.field('revision').value, '2');
  env.submit(env.tests); assert.equal(env.requests.length, 1);

  env = environment(); env.problem.attrs.action = 'https://attacker.example' + path;
  env.submit(env.problem); assert.equal(env.requests.length, 0, 'never forward CSRF or private content cross-origin');
  console.log('draft continuity behavior scenarios passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def test_draft_edit_continuity_behavior():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for deterministic DOM checks')
    result = subprocess.run([node, '-e', HARNESS], input=SCRIPT, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'draft continuity behavior scenarios passed' in result.stdout
