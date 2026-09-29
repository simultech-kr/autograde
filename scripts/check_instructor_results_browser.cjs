// Deterministic state-machine tests; no browser control, real clock, or network.
// PYTHONPATH=src python -c 'from autograde.instructor_results_browser import SCRIPT; print(SCRIPT)' | node scripts/check_instructor_results_browser.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(0, 'utf8');
const origin = 'https://school.example';
const pageURL = origin + '/courses/course-one/instructor/submissions';
let checks = 0;
let assignmentChecks = 0;

class EventTarget {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  dispatch(type, options = {}) {
    const event = {type, target: this, button: 0, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; }, ...options};
    for (const listener of this.listeners.get(type) || []) listener(event);
    return event;
  }
}
const attrName = key => 'data-' + key.replace(/[A-Z]/g, letter => '-' + letter.toLowerCase());
const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const decode = value => String(value).replace(/&(amp|quot|lt|gt|#39|apos);/g,
  (_, name) => ({amp: '&', quot: '"', lt: '<', gt: '>', '#39': "'", apos: "'"})[name]);
class Element extends EventTarget {
  constructor(tagName, attributes = {}, ownerDocument = null) {
    super(); this.tagName = tagName.toUpperCase(); this.attrs = new Map(Object.entries(attributes));
    this.childNodes = []; this.parentElement = null; this.ownerDocument = ownerDocument;
    this.dataset = new Proxy({}, {get: (_, key) => this.attrs.get(attrName(key))});
    this.classList = {
      add: name => this.attrs.set('class', [...new Set((this.attrs.get('class') || '').split(' ').filter(Boolean).concat(name))].join(' ')),
      remove: name => this.attrs.set('class', (this.attrs.get('class') || '').split(' ').filter(item => item !== name).join(' ')),
      contains: name => (this.attrs.get('class') || '').split(' ').includes(name)
    };
  }
  get attributes() { return [...this.attrs].map(([name, value]) => ({name, value})); }
  get id() { return this.attrs.get('id') || ''; }
  get href() { return new URL(this.attrs.get('href'), this.ownerDocument?.window?.location.href || pageURL).href; }
  set href(value) { this.attrs.set('href', String(value)); }
  get target() { return this.attrs.get('target') || ''; }
  get hidden() { return this.attrs.has('hidden'); }
  set hidden(value) { value ? this.attrs.set('hidden', '') : this.attrs.delete('hidden'); }
  get open() { return this.attrs.has('open'); }
  set open(value) { value ? this.attrs.set('open', '') : this.attrs.delete('open'); }
  get disabled() { return this.attrs.has('disabled'); }
  set disabled(value) { value ? this.attrs.set('disabled', '') : this.attrs.delete('disabled'); }
  get value() { return this.attrs.get('value') || ''; }
  set value(value) { this.attrs.set('value', String(value)); }
  get options() { return this.querySelectorAll('option'); }
  get innerHTML() { return this.childNodes.map(node => typeof node === 'string' ? escape(node) : node.outerHTML).join(''); }
  get outerHTML() {
    const attrs = [...this.attrs].map(([name, value]) => ` ${name}="${escape(value)}"`).join('');
    return `<${this.tagName.toLowerCase()}${attrs}>${this.innerHTML}</${this.tagName.toLowerCase()}>`;
  }
  get textContent() { return this.childNodes.map(node => typeof node === 'string' ? node : node.textContent).join(''); }
  set textContent(value) { this.childNodes = [String(value)]; }
  hasAttribute(name) { return this.attrs.has(name); }
  getAttribute(name) { return this.attrs.get(name) ?? null; }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  removeAttribute(name) { this.attrs.delete(name); }
  matches(selector) {
    if (selector === '*') return true;
    const match = /^([a-z]*)?(?:\[([\w-]+)\])?$/i.exec(selector);
    if (!match) throw new Error('Unsupported fake DOM selector: ' + selector);
    return (!match[1] || this.tagName === match[1].toUpperCase()) && (!match[2] || this.attrs.has(match[2]));
  }
  querySelectorAll(selector) {
    const result = [];
    for (const node of this.childNodes) if (typeof node !== 'string') {
      if (node.matches(selector)) result.push(node);
      result.push(...node.querySelectorAll(selector));
    }
    return result;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  contains(element) {
    return this === element || this.childNodes.some(node => typeof node !== 'string' && node.contains(element));
  }
  closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
  append(node) {
    this.childNodes.push(node);
    if (typeof node !== 'string') { node.parentElement = this; node.adopt(this.ownerDocument); }
  }
  adopt(document) {
    this.ownerDocument = document;
    this.childNodes.forEach(node => { if (typeof node !== 'string') node.adopt(document); });
  }
  replaceChildren(...children) { this.childNodes = []; children.forEach(child => this.append(child)); }
  focus() { this.ownerDocument.activeElement = this; }
  getBoundingClientRect() {
    const all = this.ownerDocument.querySelectorAll('[data-result-key]').filter(row => !row.hidden);
    const top = 20 + all.indexOf(this) * 40 - this.ownerDocument.window.scrollY;
    return {top, bottom: top + 40};
  }
}
class Document extends EventTarget {
  constructor() {
    super(); this.hidden = false;
    this.documentElement = new Element('html', {}, this);
    this.head = new Element('head', {}, this); this.body = new Element('body', {}, this);
    this.documentElement.append(this.head); this.documentElement.append(this.body);
    this.activeElement = this.body;
  }
  querySelectorAll(selector) {
    return [...(this.documentElement.matches(selector) ? [this.documentElement] : []), ...this.documentElement.querySelectorAll(selector)];
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  createElement(tagName) { return new Element(tagName, {}, this); }
}
function parse(html) {
  const document = new Document(), stack = [document.body];
  for (const token of html.match(/<[^>]+>|[^<]+/g) || []) {
    if (token.startsWith('</')) { stack.pop(); continue; }
    if (token.startsWith('<')) {
      const tag = /^<([\w-]+)/.exec(token)?.[1];
      if (!tag) continue;
      const attrs = {};
      const tail = token.slice(tag.length + 1, -1);
      for (const match of tail.matchAll(/([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) {
        attrs[match[1].toLowerCase()] = decode(match[2] ?? match[3] ?? match[4] ?? '');
      }
      const element = new Element(tag, attrs, document); stack.at(-1).append(element);
      if (!['br', 'hr', 'input', 'img', 'meta', 'link', 'col'].includes(tag)) stack.push(element);
    } else stack.at(-1).append(decode(token));
  }
  return document;
}
const row = (key, submitted = 'yes', pending = 'no', attention = 'no', label = key, assignment = 'assignment-one') =>
  `<tr data-result-key="${key}" data-assignment-id="${escape(assignment)}" data-submitted="${submitted}" data-pending="${pending}" data-attention="${attention}"><td><a data-focus-key="${key}" href="/courses/course-one/instructor/submissions/${key}">${escape(label)}</a></td></tr>`;
const assignmentSummary = (id, label, total, submitted) => {
  const links = [['all', total], ['submitted', submitted], ['unsubmitted', total - submitted]].map(([view, count]) =>
    `<a data-results-choice href="${escape(pageURL + '?' + new URLSearchParams({assignment_id: id, result_view: view}))}"><span>${view} ${count}명</span></a>`).join('');
  return `<section data-result-assignment-summary data-assignment-id="${escape(id)}" data-assignment-label="${escape(label)}" data-total="${total}" data-submitted="${submitted}" data-unsubmitted="${total - submitted}"><h3>${escape(label)}</h3>${links}</section>`;
};
const fragment = (rows = row('one') + row('two', 'no'), labels = {}) => {
  const groups = new Map();
  for (const item of parse(rows).querySelectorAll('[data-result-key]')) {
    const group = groups.get(item.dataset.assignmentId) || {total: 0, submitted: 0};
    group.total++; group.submitted += item.dataset.submitted === 'yes' ? 1 : 0;
    groups.set(item.dataset.assignmentId, group);
  }
  const summaries = [...groups].map(([id, group]) => assignmentSummary(id, labels[id] || `Assignment ${id}`, group.total, group.submitted)).join('');
  return `<section><h2>결과</h2><div data-results-course-summary>교과목 집계</div>${summaries}<p data-results-selection></p><details id="attention"><summary>확인 필요</summary><p>Details</p></details><table><tbody>${rows}</tbody></table><p data-results-empty-filter hidden>해당 학생이 없습니다.</p></section>`;
};
const payload = (html = fragment(), generated_at = '2026-09-28T01:00:00+00:00') => ({course_key: 'course-one', generated_at, html});
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function environment(options = {}) {
  const document = parse(`<main data-live-results="${options.endpoint || '/courses/course-one/instructor/submissions/live'}" data-course-key="course-one"><a data-results-refresh href="${pageURL}">새로고침</a><button data-results-toggle hidden></button><select data-results-assignment disabled></select><select data-results-filter value="all" disabled></select><p data-results-message></p><time data-results-time></time><div data-results-content>${options.html ?? fragment()}</div></main>`);
  const window = new EventTarget(); window.location = new URL(options.pageURL || pageURL); window.scrollX = 0; window.scrollY = 0; window.innerHeight = 900;
  window.scrollTo = (x, y) => { window.scrollX = x; window.scrollY = y; };
  const addresses = [];
  window.history = {replaceState: (_, title, url) => {
    if (options.historyBlocked) throw new Error('history unavailable');
    window.location = new URL(url, window.location.href); addresses.push(window.location.href);
  }};
  let selection = ''; window.getSelection = () => ({isCollapsed: !selection, toString: () => selection});
  document.window = window;
  let now = 0, serial = 0; const timers = new Map(), calls = [], responses = [];
  const setTimeout = (fn, delay) => { const id = ++serial; timers.set(id, {fn, at: now + delay}); return id; };
  const clearTimeout = id => timers.delete(id);
  const navigator = {onLine: true};
  const fetch = (url, init) => {
    calls.push({url, init});
    const next = responses.shift() || {data: payload()};
    if (next.deferred) return next.deferred;
    if (next.never) return new Promise((resolve, reject) => init.signal.addEventListener('abort', () => reject(new Error('aborted'))));
    if (next.error) return Promise.reject(new Error(next.error));
    return Promise.resolve({status: next.status || 200, ok: !next.status || next.status < 400,
      json: next.json || (() => Promise.resolve(next.data || payload()))});
  };
  class DOMParser { parseFromString(html) { return parse(html); } }
  Object.assign(window, {fetch, AbortController, DOMParser});
  const context = vm.createContext({document, window, navigator, fetch, AbortController, DOMParser, URL, URLSearchParams, setTimeout, clearTimeout});
  vm.runInContext(source, context, {timeout: 1000});
  const find = selector => document.querySelector(selector);
  return {
    document, window, navigator, calls, responses, timers, addresses, find,
    rows: () => document.querySelectorAll('[data-result-key]'),
    message: () => find('[data-results-message]').textContent,
    selection: value => { selection = value; },
    click: (element, options = {}) => {
      let event = element.dispatch('click', options);
      for (let parent = element.parentElement; parent; parent = parent.parentElement) event = parent.dispatch('click', event);
      return document.dispatch('click', event);
    },
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        const next = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        now = next[1].at; timers.delete(next[0]); next[1].fn(); await settle();
      }
      now = end; await settle();
    }
  };
}
const deferred = () => { let resolve; return {promise: new Promise(done => { resolve = done; }), resolve: value => resolve(value)}; };
const response = data => ({status: 200, ok: true, json: () => Promise.resolve(data)});
const visibleKeys = e => e.rows().filter(item => !item.hidden).map(item => item.dataset.resultKey);
const summaries = e => e.document.querySelectorAll('[data-result-assignment-summary]');
const assignmentRows = () => row('a-one', 'yes', 'yes', 'no', 'Student One', 'assignment-a') +
  row('a-two', 'no', 'no', 'no', 'Student Two', 'assignment-a') +
  row('b-one', 'no', 'no', 'no', 'Student One', 'assignment-b') +
  row('b-two', 'yes', 'no', 'yes', 'Student Two', 'assignment-b');

(async () => {
  {
    const e = environment({html: fragment(assignmentRows())});
    const assignment = e.find('[data-results-assignment]'), filter = e.find('[data-results-filter]');
    assert.equal(assignment.disabled, false);
    assert.deepEqual(assignment.options.map(option => option.value), ['', 'assignment-a', 'assignment-b']);
    assignment.value = 'assignment-a'; assignment.dispatch('change');
    assert.deepEqual(visibleKeys(e), ['a-one', 'a-two']);
    assert.equal(e.find('[data-results-course-summary]').hidden, true);
    assert.deepEqual(summaries(e).filter(item => !item.hidden).map(item => item.dataset.assignmentId), ['assignment-a']);
    assert.match(e.find('[data-results-selection]').textContent, /현재 명단 2명/);
    for (const [value, keys] of [['submitted', ['a-one']], ['unsubmitted', ['a-two']], ['pending', ['a-one']], ['attention', []]]) {
      filter.value = value; filter.dispatch('change');
      assert.deepEqual(visibleKeys(e), keys, `assignment and ${value} filters must intersect`);
    }
    assert.equal(summaries(e)[0].dataset.submitted, '1', 'display filters must not change the assignment totals');
    assert.equal(summaries(e)[0].dataset.unsubmitted, '1');
    assignment.value = 'assignment-b'; assignment.dispatch('change');
    assert.deepEqual(visibleKeys(e), ['b-two']);
    filter.value = 'unsubmitted'; filter.dispatch('change');
    assert.deepEqual(visibleKeys(e), ['b-one']);
    assignment.value = ''; assignment.dispatch('change');
    assert.deepEqual(visibleKeys(e), ['a-two', 'b-one']);
    assert.equal(e.find('[data-results-course-summary]').hidden, false);
    assert.equal(summaries(e).every(item => !item.hidden), true);
    assert.match(e.find('[data-results-selection]').textContent, /현재 명단 2건/);
    checks++; assignmentChecks++;
  }
  {
    const e = environment({html: fragment(assignmentRows())});
    const group = summaries(e).find(item => item.dataset.assignmentId === 'assignment-b');
    const choice = group.querySelectorAll('[data-results-choice]').find(link => new URL(link.href).searchParams.get('result_view') === 'unsubmitted');
    assert.equal(e.click(choice.querySelector('span')).defaultPrevented, true, 'nested link content participates in delegated filtering');
    assert.equal(e.find('[data-results-assignment]').value, 'assignment-b');
    assert.equal(e.find('[data-results-filter]').value, 'unsubmitted');
    assert.deepEqual(visibleKeys(e), ['b-one']);
    assert.equal(choice.getAttribute('aria-current'), 'true');
    assert.equal(e.document.querySelectorAll('[data-results-choice]').filter(link => link.hasAttribute('aria-current')).length, 1);
    const other = group.querySelectorAll('[data-results-choice]')[0];
    assert.equal(e.click(other, {ctrlKey: true}).defaultPrevented, false, 'modified clicks retain native navigation');
    assert.equal(e.find('[data-results-filter]').value, 'unsubmitted');
    e.responses.push({data: payload(fragment(assignmentRows()))});
    await e.advance(5000);
    assert.equal(e.calls.length, 1, 'a local list choice must not stop polling as page navigation');
    checks++; assignmentChecks++;
  }
  {
    const e = environment({html: fragment(assignmentRows()), pageURL: pageURL + '?assignment_id=assignment-a&result_view=unsubmitted'});
    assert.deepEqual(visibleKeys(e), ['a-two']);
    const completed = row('b-one', 'no', 'no', 'no', 'Student One', 'assignment-b') +
      row('a-one', 'yes', 'no', 'no', 'Student One resubmitted', 'assignment-a') +
      row('a-two', 'yes', 'yes', 'no', 'Student Two submitted', 'assignment-a');
    e.responses.push({data: payload(fragment(completed, {'assignment-a': 'Updated assignment title'}))});
    await e.advance(5000);
    assert.equal(e.find('[data-results-assignment]').value, 'assignment-a');
    assert.equal(e.find('[data-results-filter]').value, 'unsubmitted');
    assert.deepEqual(visibleKeys(e), []);
    assert.equal(e.find('[data-results-empty-filter]').hidden, false);
    const group = summaries(e).find(item => item.dataset.assignmentId === 'assignment-a');
    assert.equal(group.dataset.submitted, '2'); assert.equal(group.dataset.unsubmitted, '0');
    assert.equal(e.find('[data-results-assignment]').options.find(option => option.value === 'assignment-a').textContent, 'Updated assignment title');
    e.responses.push({data: payload(fragment(completed + row('a-three', 'no', 'no', 'no', 'Student Three', 'assignment-a')))});
    await e.advance(5000);
    assert.deepEqual(visibleKeys(e), ['a-three']);
    assert.equal(new URL(e.find('[data-results-refresh]').href).searchParams.get('assignment_id'), 'assignment-a');
    checks++; assignmentChecks++;
  }
  {
    const e = environment({html: fragment(assignmentRows()), pageURL: pageURL + '?assignment_id=assignment-a&result_view=submitted'});
    e.responses.push({data: payload(fragment(row('b-new', 'yes', 'no', 'no', 'Other result', 'assignment-b')))});
    await e.advance(5000);
    const assignment = e.find('[data-results-assignment]');
    assert.equal(assignment.value, 'assignment-a');
    assert.match(assignment.options.find(option => option.value === 'assignment-a').textContent, /현재 조회 대상 없음/);
    assert.deepEqual(visibleKeys(e), [], 'a disappeared selection must never expose the whole course');
    assert.equal(summaries(e).every(item => item.hidden), true);
    assert.equal(e.find('[data-results-empty-filter]').hidden, false);
    assert.match(e.find('[data-results-selection]').textContent, /현재 명단 0명/);
    assert.equal(e.window.location.searchParams.get('assignment_id'), 'assignment-a');
    e.responses.push({data: payload(fragment(row('a-returned', 'yes', 'no', 'no', 'Restored result', 'assignment-a')))});
    await e.advance(5000);
    assert.deepEqual(visibleKeys(e), ['a-returned']);
    assert.equal(assignment.options.filter(option => option.value === 'assignment-a').length, 1);
    checks++; assignmentChecks++;
  }
  {
    const e = environment({html: fragment(assignmentRows()), pageURL: pageURL + '?keep=hello%20world&assignment_id=assignment-b&result_view=submitted#student-results'});
    assert.deepEqual(visibleKeys(e), ['b-two']);
    assert.equal(e.find('[data-results-assignment]').value, 'assignment-b');
    assert.equal(e.find('[data-results-filter]').value, 'submitted');
    e.find('[data-results-filter]').value = 'unsubmitted'; e.find('[data-results-filter]').dispatch('change');
    let address = new URL(e.find('[data-results-refresh]').href);
    assert.equal(address.href, e.window.location.href);
    assert.equal(address.searchParams.get('keep'), 'hello world');
    assert.equal(address.hash, '#student-results');
    assert.equal(address.searchParams.get('assignment_id'), 'assignment-b');
    assert.equal(address.searchParams.get('result_view'), 'unsubmitted');
    e.find('[data-results-assignment]').value = ''; e.find('[data-results-assignment]').dispatch('change');
    e.find('[data-results-filter]').value = 'all'; e.find('[data-results-filter]').dispatch('change');
    address = new URL(e.find('[data-results-refresh]').href);
    assert.equal(address.searchParams.has('assignment_id'), false);
    assert.equal(address.searchParams.has('result_view'), false);
    assert.equal(address.searchParams.get('keep'), 'hello world');
    assert.equal(e.addresses.length >= 2, true);
    checks++; assignmentChecks++;
  }
  {
    const title = '과제 <img src=x onerror=alert(1)> "A&B"';
    const e = environment({html: fragment(row('escaped', 'yes', 'no', 'no', 'Student', 'assignment-a'), {'assignment-a': title})});
    const option = e.find('[data-results-assignment]').options.find(item => item.value === 'assignment-a');
    assert.equal(option.textContent, title);
    assert.equal(option.querySelector('img'), null, 'assignment labels must be inserted as text, not markup');
    assert.match(option.outerHTML, /&lt;img/);
    const missing = 'missing&result_view=all#<img>';
    const unknown = environment({pageURL: pageURL + '?' + new URLSearchParams({assignment_id: missing, result_view: 'unsubmitted'})});
    assert.deepEqual(visibleKeys(unknown), []);
    assert.equal(unknown.find('[data-results-assignment]').value, missing);
    const address = new URL(unknown.find('[data-results-refresh]').href);
    assert.equal(address.searchParams.get('assignment_id'), missing);
    assert.equal(address.searchParams.getAll('result_view').length, 1);
    assert.equal(address.searchParams.get('result_view'), 'unsubmitted');
    assert.equal(address.hash, '');
    assert.equal(unknown.document.querySelector('img'), null);
    checks++; assignmentChecks++;
  }
  {
    const e = environment({html: fragment(assignmentRows()), pageURL: pageURL + '?assignment_id=assignment-a&result_view=unknown', historyBlocked: true});
    assert.equal(e.find('[data-results-filter]').value, 'all');
    assert.deepEqual(visibleKeys(e), ['a-one', 'a-two']);
    e.find('[data-results-filter]').value = 'unsubmitted'; e.find('[data-results-filter]').dispatch('change');
    assert.deepEqual(visibleKeys(e), ['a-two']);
    assert.equal(new URL(e.find('[data-results-refresh]').href).searchParams.get('result_view'), 'unsubmitted');
    assert.equal(e.addresses.length, 0, 'history failures do not disable filtering or the native refresh URL');
    checks++; assignmentChecks++;
  }
  {
    const e = environment();
    const initialRow = e.rows()[0]; initialRow.querySelector('[data-focus-key]').focus();
    assert.equal(e.find('[data-results-toggle]').hidden, false);
    assert.equal(e.find('[data-results-filter]').disabled, false);
    await e.advance(4999); assert.equal(e.calls.length, 0);
    await e.advance(1); assert.equal(e.calls.length, 1);
    assert.equal(e.calls[0].url, pageURL + '/live');
    for (const [key, value] of Object.entries({method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error'})) assert.equal(e.calls[0].init[key], value);
    assert.match(e.message(), /최신/);
    assert.equal(e.rows()[0], initialRow, 'unchanged snapshots preserve the existing DOM and focus');
    assert.equal(e.find('[data-results-refresh]').textContent, '새로고침');
    assert.match(e.find('[data-results-time]').textContent, /마지막 확인/);
    assert.match(e.find('[data-results-time]').textContent, /10:00:00.*KST/);
    await e.advance(5000); assert.equal(e.calls.length, 2); checks++;
  }
  {
    const e = environment(), filter = e.find('[data-results-filter]');
    e.responses.push({data: payload(fragment(row('one', 'yes', 'yes') + row('two', 'no') + row('three', 'yes', 'no', 'yes')))});
    await e.advance(5000);
    for (const [value, keys] of [['all', ['one', 'two', 'three']], ['submitted', ['one', 'three']], ['pending', ['one']], ['attention', ['three']], ['unsubmitted', ['two']]]) {
      filter.value = value; filter.dispatch('change');
      assert.deepEqual(e.rows().filter(row => !row.hidden).map(row => row.dataset.resultKey), keys);
    }
    filter.value = 'pending'; filter.dispatch('change');
    e.responses.push({data: payload(fragment(row('two') + row('one')))});
    await e.advance(5000);
    assert.equal(filter.value, 'pending'); assert.equal(e.find('[data-results-empty-filter]').hidden, false);
    assert.deepEqual(e.rows().map(row => row.dataset.resultKey), ['two', 'one']); checks++;
  }
  {
    const e = environment(); const details = e.find('details[id]'); details.open = true;
    e.window.scrollY = 10;
    const link = e.find('[data-focus-key]'); link.focus();
    e.responses.push({data: payload(fragment(row('two') + row('one', 'yes', 'no', 'no', 'Updated')))});
    await e.advance(5000);
    assert.equal(e.rows()[0].dataset.resultKey, 'one'); assert.match(e.message(), /新|새 결과가/);
    assert.equal(e.find('[data-results-refresh]').textContent, '새 결과 적용');
    const event = e.click(e.find('[data-results-refresh]'));
    assert.equal(event.defaultPrevented, true);
    assert.equal(e.rows()[0].dataset.resultKey, 'two');
    assert.equal(e.document.activeElement.dataset.focusKey, 'one');
    assert.equal(e.find('details[id]').open, true);
    assert.equal(e.window.scrollY, 50, 'keep the previous visible row at the same viewport offset when reading below the page top');
    assert.equal(e.rows()[1].classList.contains('results-updated'), true);
    await e.advance(2500); assert.equal(e.rows()[1].classList.contains('results-updated'), false);
    assert.equal(e.calls.length, 1); checks++;
  }
  {
    const e = environment();
    e.responses.push({data: payload(fragment(row('new') + row('one') + row('two', 'no')))});
    await e.advance(5000); assert.equal(e.rows()[0].dataset.resultKey, 'new');
    assert.equal(e.window.scrollY, 0, 'show new submissions at the top without scrolling down to the previous first row'); checks++;
  }
  {
    const e = environment(); e.selection('selected result text');
    e.responses.push({data: payload(fragment(row('three')))});
    await e.advance(5000); assert.equal(e.rows()[0].dataset.resultKey, 'one');
    e.selection(''); e.responses.push({data: payload(fragment(row('four')))});
    await e.advance(5000); assert.equal(e.rows()[0].dataset.resultKey, 'four'); checks++;
  }
  {
    const e = environment(); e.click(e.find('[data-results-toggle]'));
    await e.advance(30000); assert.equal(e.calls.length, 0);
    e.click(e.find('[data-results-refresh]')); await settle(); assert.equal(e.calls.length, 1);
    await e.advance(30000); assert.equal(e.calls.length, 1);
    e.click(e.find('[data-results-toggle]')); await e.advance(0); assert.equal(e.calls.length, 2); checks++;
  }
  for (const status of [401, 403, 404, 400]) {
    const e = environment(); e.responses.push({status}); await e.advance(5000);
    assert.match(e.message(), /권한/); assert.equal(e.find('[data-results-toggle]').hidden, true);
    await e.advance(120000); assert.equal(e.calls.length, 1);
    assert.equal(e.click(e.find('[data-results-refresh]')).defaultPrevented, false, 'terminal state retains a native reload link'); checks++;
  }
  for (const failure of [{status: 503}, {status: 429}, {error: 'network'}]) {
    const e = environment();
    for (const [index, delay] of [5000, 10000, 20000, 40000, 60000].entries()) {
      e.responses.push(failure); await e.advance(delay); assert.equal(e.calls.length, index + 1);
    }
    assert.match(e.find('[data-results-toggle]').textContent, /다시 시작/);
    await e.advance(120000); assert.equal(e.calls.length, 5);
    e.click(e.find('[data-results-toggle]')); await e.advance(0); assert.equal(e.calls.length, 6); assert.match(e.message(), /최신/); checks++;
  }
  for (const transition of ['hidden', 'offline', 'pagehide', 'logout', 'submit']) {
    const e = environment(), waiting = deferred(); e.responses.push({deferred: waiting.promise});
    await e.advance(5000); assert.equal(e.calls.length, 1);
    if (transition === 'hidden') { e.document.hidden = true; e.document.dispatch('visibilitychange'); }
    if (transition === 'offline') { e.navigator.onLine = false; e.window.dispatch('offline'); }
    if (transition === 'pagehide') e.window.dispatch('pagehide');
    if (transition === 'logout') e.click(new Element('a', {href: '/logout'}, e.document));
    if (transition === 'submit') e.document.dispatch('submit');
    assert.equal(e.calls[0].init.signal.aborted, true);
    waiting.resolve(response(payload(fragment(row('stale'))))); await settle();
    assert.equal(e.rows()[0].dataset.resultKey, 'one', 'ignore a response resolving after abort/navigation');
    await e.advance(30000); assert.equal(e.calls.length, 1);
    if (transition === 'hidden') { e.document.hidden = false; e.document.dispatch('visibilitychange'); }
    else if (transition === 'offline') { e.navigator.onLine = true; e.window.dispatch('online'); }
    else e.window.dispatch('pageshow');
    await e.advance(5000); assert.equal(e.calls.length, 2); checks++;
  }
  {
    const e = environment(), waiting = deferred(); e.responses.push({json: () => waiting.promise});
    await e.advance(5000); e.document.hidden = true; e.document.dispatch('visibilitychange');
    waiting.resolve(payload(fragment(row('stale-json')))); await settle();
    assert.equal(e.rows()[0].dataset.resultKey, 'one'); checks++;
  }
  {
    const e = environment(); e.responses.push({never: true});
    await e.advance(5000); await e.advance(9999); assert.equal(e.calls[0].init.signal.aborted, false);
    await e.advance(1); assert.equal(e.calls[0].init.signal.aborted, true); assert.match(e.message(), /확인하지 못/);
    await e.advance(10000); assert.equal(e.calls.length, 2); checks++;
  }
  for (const data of [
    {...payload(), course_key: 'other-course'}, {...payload(), generated_at: 'yesterday'},
    {...payload(), generated_at: '2026-09-28T01:00:00'}, {...payload(), html: 'x'.repeat(4 * 1024 * 1024 + 1)},
    payload('<p>Missing results marker</p>'), payload(fragment() + '<script>alert(1)</script>'),
    payload(fragment() + '<iframe src="https://evil.example"></iframe>'),
    payload(fragment() + '<form action="/logout"></form>'),
    payload(fragment() + '<a href="javascript:alert(1)">bad</a>'),
    payload(fragment() + '<a href="https://evil.example/">bad</a>'),
    payload(fragment() + '<div onclick="alert(1)">bad</div>'),
    payload(fragment() + '<div style="position:fixed">bad</div>')
  ]) {
    const e = environment(); e.responses.push({data}); await e.advance(5000);
    assert.match(e.message(), /확인하지 못/); assert.equal(e.rows()[0].dataset.resultKey, 'one'); checks++;
  }
  {
    const e = environment(); await e.advance(5000);
    e.responses.push({data: payload(fragment(row('old')), '2026-09-27T01:00:00+00:00')});
    await e.advance(5000); assert.match(e.message(), /확인하지 못/); assert.equal(e.rows()[0].dataset.resultKey, 'one'); checks++;
  }
  {
    const e = environment({pageURL: origin + '/instructor', endpoint: '/v1/instructor/dashboard/live'});
    await e.advance(5000); assert.equal(e.calls.length, 1);
    assert.equal(e.calls[0].url, origin + '/v1/instructor/dashboard/live');
    assert.match(e.message(), /최신/); checks++;
  }
  for (const endpoint of ['https://evil.example/live', '/courses/other/instructor/submissions/live', '/logout', '/v1/instructor/dashboard/live']) {
    const e = environment({endpoint}); await e.advance(60000);
    assert.equal(e.calls.length, 0); assert.equal(e.find('[data-results-toggle]').hidden, true);
    assert.equal(e.click(e.find('[data-results-refresh]')).defaultPrevented, false); checks++;
  }
  console.log(`${assignmentChecks} assignment filter scenarios passed`);
  console.log(`${checks} live results behavior scenarios passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });
