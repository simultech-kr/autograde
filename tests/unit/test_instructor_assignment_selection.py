"""Exercise bulk selection controls without browser persistence or a network."""
import shutil
import subprocess

import pytest

from autograde.instructor_browser import SCRIPT


HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = require('node:fs').readFileSync(0, 'utf8');
class Control {
  constructor(properties = {}) {
    Object.assign(this, {checked: false, disabled: false, hidden: false,
      indeterminate: false, dataset: {}, textContent: '', listeners: {}}, properties);
  }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  fire(type, event = {}) { for (const handler of this.listeners[type] || []) handler(event); }
}
class BulkForm {
  constructor(states) {
    this.choices = states.map(archived => new Control({dataset: {archived}, checked: true}));
    this.toggle = new Control({checked: true});
    this.clear = new Control({hidden: true});
    this.status = new Control();
    this.label = new Control({hidden: true});
    this.archive = new Control({value: 'archive'});
    this.delete = new Control({value: 'delete'});
  }
  querySelectorAll(selector) {
    return ({'[data-assignment-select]': this.choices,
      '[data-assignment-bulk-action]': [this.archive, this.delete],
      '[data-assignment-selection-control]': [this.label]})[selector] || [];
  }
  querySelector(selector) {
    return ({'[data-assignment-select-all]': this.toggle,
      '[data-assignment-clear]': this.clear,
      '[data-assignment-selection-status]': this.status})[selector] || null;
  }
  select(index, checked) { this.choices[index].checked = checked; this.choices[index].fire('change'); }
  selectAll(checked) { this.toggle.checked = checked; this.toggle.fire('change'); }
}
const mixed = new BulkForm(['true', 'false', 'true']);
const archived = new BulkForm(['true', 'true']);
const oversized = new BulkForm(Array(21).fill('true'));
const empty = new BulkForm([]);
const forms = [mixed, archived, oversized, empty];
const window = new Control();
const document = new Control();
document.querySelectorAll = selector => selector === 'form[data-assignment-bulk]' ? forms : [];
const context = {window, document};
for (const key of ['fetch', 'localStorage', 'sessionStorage', 'indexedDB']) {
  Object.defineProperty(context, key, {get() { throw new Error(`Unexpected browser access: ${key}`); }});
  Object.defineProperty(window, key, {get() { throw new Error(`Unexpected browser access: ${key}`); }});
}
vm.runInNewContext(source, context);

for (const form of forms) {
  assert.equal(form.label.hidden, false);
  assert.equal(form.clear.hidden, false);
  assert.equal(form.archive.disabled, true);
  assert.equal(form.delete.disabled, true);
  assert.equal(form.clear.disabled, true);
  assert.equal(form.toggle.checked, false);
  assert.equal(form.toggle.indeterminate, false);
  assert.ok(form.choices.every(choice => !choice.checked));
}
assert.equal(empty.toggle.disabled, true);

mixed.select(0, true);
assert.equal(mixed.archive.disabled, false);
assert.equal(mixed.delete.disabled, false);
assert.equal(mixed.toggle.indeterminate, true);
assert.equal(mixed.toggle.checked, false);
assert.match(mixed.status.textContent, /1개 선택됨/);
assert.ok(archived.choices.every(choice => !choice.checked), 'selection stays in its own form');

mixed.select(1, true);
assert.equal(mixed.archive.disabled, false);
assert.equal(mixed.delete.disabled, true);
assert.match(mixed.status.textContent, /먼저 보관/);
mixed.select(1, false);
assert.equal(mixed.delete.disabled, false);
mixed.selectAll(true);
assert.equal(mixed.toggle.checked, true);
assert.equal(mixed.toggle.indeterminate, false);
assert.ok(mixed.choices.every(choice => choice.checked));
assert.equal(mixed.delete.disabled, true);
mixed.clear.fire('click');
assert.ok(mixed.choices.every(choice => !choice.checked));
assert.equal(mixed.toggle.checked, false);
assert.equal(mixed.archive.disabled, true);
assert.equal(mixed.delete.disabled, true);
assert.match(mixed.status.textContent, /현재 페이지에서 과제를 선택/);

archived.selectAll(true);
assert.equal(archived.archive.disabled, false, 'archiving an archived selection is valid');
assert.equal(archived.delete.disabled, false);
archived.selectAll(false);
assert.equal(archived.archive.disabled, true);
assert.equal(archived.delete.disabled, true);

oversized.selectAll(true);
assert.equal(oversized.choices.filter(choice => choice.checked).length, 20);
assert.equal(oversized.toggle.indeterminate, true);
assert.equal(oversized.archive.disabled, false);
oversized.select(20, true);
assert.equal(oversized.archive.disabled, true);
assert.equal(oversized.delete.disabled, true);
assert.match(oversized.status.textContent, /최대 20개/);

archived.selectAll(true);
window.fire('pageshow', {persisted: true});
for (const form of forms) {
  assert.ok(form.choices.every(choice => !choice.checked), 'bfcache must not restore stale selections');
  assert.equal(form.toggle.checked, false);
  assert.equal(form.toggle.indeterminate, false);
  assert.equal(form.archive.disabled, true);
  assert.equal(form.delete.disabled, true);
}
archived.select(0, true);
window.fire('pageshow', {persisted: false});
assert.ok(archived.choices.every(choice => !choice.checked), 'all pageshow events clear selection');
console.log('bulk selection behavior scenarios passed');
"""


def test_bulk_assignment_selection_behavior():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the deterministic JavaScript behavior checks')
    result = subprocess.run([node, '-e', HARNESS], input=SCRIPT, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'bulk selection behavior scenarios passed' in result.stdout
