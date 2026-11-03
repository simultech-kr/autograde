"""Actual readiness script against a DOM with deterministic network and time."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from autograde.instructor_browser import SCRIPT
from autograde.operations_readiness_view import render


ROOT = Path(__file__).resolve().parents[2]
HARNESS = r"""
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const scenario = process.argv[1];
const flush = () => new Promise(resolve => setImmediate(resolve));
const clone = value => JSON.parse(JSON.stringify(value));
const output = (changes = {}) => ({...clone(input.report), ...changes});
function setup() {
  const dom = new JSDOM(input.html, {url:'https://class.example/instructor/operations', runScripts:'outside-only', pretendToBeVisual:true});
  const w = dom.window, d = w.document;
  let now = 0, serial = 0, hidden = false, online = true;
  const timers = new Map(), requests = [];
  w.setTimeout = (callback, delay = 0) => { const id = ++serial; timers.set(id, {callback, at:now + delay}); return id; };
  w.clearTimeout = id => timers.delete(id);
  Object.defineProperty(w.performance, 'now', {value: () => now});
  Object.defineProperty(d, 'hidden', {get: () => hidden, configurable:true});
  Object.defineProperty(w.navigator, 'onLine', {get: () => online, configurable:true});
  for (const key of ['localStorage','sessionStorage','indexedDB']) Object.defineProperty(w, key, {get() { throw Error('Unexpected persistence'); }});
  w.fetch = (url, options) => new Promise((resolve, reject) => {
    requests.push({url, options, resolve, reject});
    options.signal.addEventListener('abort', () => reject(new Error('aborted')));
  });
  w.eval(input.script);
  const advance = async delta => {
    const target = now + delta;
    let iterations = 0;
    while (true) {
      const next = [...timers].sort((a,b) => a[1].at - b[1].at)[0];
      if (!next || next[1].at > target) break;
      if (++iterations > 100) throw Error('unbounded timer loop');
      timers.delete(next[0]); now = Math.max(now, next[1].at); next[1].callback(); await flush();
    }
    now = target; await flush();
  };
  const finish = async (request, data, status=200, type='application/json') => {
    request.resolve({status, ok: status >= 200 && status < 300, headers:{get: () => type}, text:async () => JSON.stringify(data)});
    await flush();
  };
  return {w,d,requests,timers,advance,finish,close:() => dom.window.close(),
    hidden(value) { hidden=value; d.dispatchEvent(new w.Event('visibilitychange')); },
    online(value) { online=value; w.dispatchEvent(new w.Event(value?'online':'offline')); },
    setNow(value) {now=value;},
    panel:d.querySelector('[data-readiness-panel]'), overall:d.querySelector('[data-readiness-overall]'),
    submit:d.querySelector('[data-readiness-submit]'), retry:d.querySelector('[data-readiness-retry]')};
}
const complete = changes => output({state:'ready', checking:false, stale:false, checked_at:'2026-10-31T01:00:00Z', retry_after_seconds:0, fresh_for_seconds:300,
  checks: input.report.checks.map(item=>({...item, state:'ok', summary:'현재 정상', details:['확인 완료'], actions:[], checked_at:'2026-10-31T01:00:00Z'})), ...changes});
(async () => {
  const env = setup();
  try {
    if (scenario === 'complete') {
      const details = env.d.querySelector('details'); details.open = true;
      const summary = details.querySelector('summary'); summary.focus();
      await env.advance(2000); assert.equal(env.requests.length,1);
      const request = env.requests[0];
      assert.equal(request.url,'/instructor/operations/readiness/status');
      assert.equal(request.options.method,'GET'); assert.equal(request.options.credentials,'same-origin');
      assert.equal(request.options.cache,'no-store'); assert.equal(request.options.redirect,'error');
      assert.deepEqual(Object.keys(request.options.headers), ['Accept']);
      await env.finish(request,output({retry_after_seconds:28}));
      await env.advance(2000); assert.equal(env.requests.length,2);
      const ready = complete({retry_after_seconds:5});
      ready.checks[0].summary='<img src=x onerror="window.compromised=true">';
      ready.checks[0].actions=['<script>bad()</script>'];
      await env.finish(env.requests[1],ready);
      assert.equal(env.d.querySelector('img'),null); assert.equal(env.w.compromised,undefined);
      assert.equal(env.d.querySelector('[data-readiness-summary]').textContent,ready.checks[0].summary);
      assert.equal(details.open,true); assert.equal(env.d.activeElement,summary);
      assert.equal(env.submit.disabled,true); assert.match(env.retry.textContent,/5초/);
      await env.advance(2000); assert.match(env.retry.textContent,/3초/);
      assert.equal(env.requests.length,2); assert.equal(env.submit.disabled,true);
      await env.advance(3000); assert.match(env.retry.textContent,/서버에서 확인/);
      assert.equal(env.requests.length,2); assert.equal(env.submit.disabled,true);
      await env.advance(100); assert.equal(env.requests.length,3); assert.equal(env.submit.disabled,true);
      await env.finish(env.requests[2],complete()); assert.equal(env.submit.disabled,false);
      assert.match(env.overall.textContent,/정상/); assert.equal(env.panel.dataset.readinessState,'ready');
      await env.advance(10000); assert.equal(env.requests.length,3);
    } else if (scenario === 'auth401' || scenario === 'auth403') {
      await env.advance(2000); await env.finish(env.requests[0],{private:'do not render'},Number(scenario.slice(4)));
      assert.match(env.overall.textContent,/로그인 또는 점검 권한/); assert.equal(env.submit.disabled,true);
      env.hidden(true); env.hidden(false); env.online(false); env.online(true);
      await env.advance(60000); assert.equal(env.requests.length,1);
      assert.match(env.overall.textContent,/로그인 또는 점검 권한/);
      assert.doesNotMatch(env.d.body.textContent,/do not render/);
    } else if (scenario === 'malformed') {
      const original = env.d.querySelector('[data-readiness-summary]').textContent;
      await env.advance(2000); const data = complete(); data.checks[1].id='next';
      await env.finish(env.requests[0],data);
      assert.match(env.overall.textContent,/결과 형식/); assert.equal(env.submit.disabled,true);
      assert.equal(env.d.querySelector('[data-readiness-summary]').textContent,original);
      await env.advance(60000); assert.equal(env.requests.length,1);
    } else if (scenario === 'pause') {
      env.hidden(true); await env.advance(10000); assert.equal(env.requests.length,0);
      env.online(false); env.hidden(false); await env.advance(10000); assert.equal(env.requests.length,0);
      assert.match(env.overall.textContent,/오프라인/);
      env.online(true); await env.advance(0); assert.equal(env.requests.length,1);
      env.hidden(true); assert.equal(env.requests[0].options.signal.aborted,true);
      await env.finish(env.requests[0],complete()); assert.notEqual(env.panel.dataset.readinessState,'ready');
      await env.advance(10000); assert.equal(env.requests.length,1);
      env.hidden(false); await env.advance(0); assert.equal(env.requests.length,2);
      await env.finish(env.requests[1],complete({state:'stale',stale:true}));
      assert.equal(env.panel.dataset.readinessCurrent,'false'); assert.match(env.overall.textContent,/이전 점검/);
      assert.ok(env.requests.every(request=>request.options.method==='GET'));
    } else if (scenario === 'pagehide') {
      await env.advance(2000); env.w.dispatchEvent(new env.w.Event('pagehide'));
      assert.equal(env.requests[0].options.signal.aborted,true);
      await env.advance(10000); assert.equal(env.requests.length,1);
      env.w.dispatchEvent(new env.w.Event('pageshow')); await env.advance(0);
      assert.equal(env.requests.length,2); await env.finish(env.requests[1],complete());
    } else if (scenario === 'failures') {
      await env.advance(2000);
      for (let index=0;index<3;index++) {
        env.requests[index].reject(new Error('secret external exception must not be shown')); await flush();
        if(index<2) await env.advance(2000*2**index);
      }
      assert.equal(env.requests.length,3); assert.match(env.overall.textContent,/자동 확인을 중단/);
      assert.doesNotMatch(env.d.body.textContent,/secret external/); assert.equal(env.submit.disabled,true);
      await env.advance(60000); assert.equal(env.requests.length,3);
    } else if (scenario === 'timeout') {
      await env.advance(7000); assert.equal(env.requests[0].options.signal.aborted,true);
      assert.match(env.overall.textContent,/마지막 확인 상태/);
      env.setNow(61000); await env.advance(0);
      assert.equal(env.requests.length,1); assert.match(env.overall.textContent,/자동 확인 시간이 지났/);
    } else if (scenario === 'freshness') {
      await env.advance(2000); await env.finish(env.requests[0],complete({fresh_for_seconds:3}));
      await env.advance(3100); assert.equal(env.requests.length,2);
      await env.finish(env.requests[1],complete({state:'stale',stale:true,fresh_for_seconds:0}));
      assert.equal(env.panel.dataset.readinessState,'stale'); assert.match(env.overall.textContent,/이전 점검 결과/);
      assert.equal(env.panel.dataset.readinessCurrent,'false');
      await env.advance(600000); assert.equal(env.requests.length,2,'expiry reads only the cached result once');
      assert.ok(env.requests.every(request=>request.options.method==='GET'));
    } else if (scenario === 'initial-freshness') {
      assert.equal(env.requests.length,0);
      await env.advance(2100); assert.equal(env.requests.length,1);
      await env.finish(env.requests[0],complete({state:'stale',stale:true,fresh_for_seconds:0}));
      assert.equal(env.panel.dataset.readinessCurrent,'false');
      await env.advance(600000); assert.equal(env.requests.length,1);
    } else if (scenario === 'cooldown-pause') {
      await env.advance(2000); await env.finish(env.requests[0],complete({retry_after_seconds:5,fresh_for_seconds:10}));
      await env.advance(1000); assert.match(env.retry.textContent,/4초/);
      env.online(false); await env.advance(10000);
      assert.equal(env.requests.length,1); assert.match(env.retry.textContent,/4초/);
      assert.equal(env.submit.disabled,true);
      env.online(true); await env.advance(0); assert.equal(env.requests.length,2);
      await env.finish(env.requests[1],complete({retry_after_seconds:2,fresh_for_seconds:3}));
      await env.advance(1000); assert.match(env.retry.textContent,/1초/);
      env.hidden(true); await env.advance(4000);
      assert.equal(env.requests.length,2); assert.match(env.retry.textContent,/1초/);
      assert.equal(env.submit.disabled,true);
      env.hidden(false); await env.advance(0); assert.equal(env.requests.length,3);
      await env.finish(env.requests[2],complete({fresh_for_seconds:1}));
      assert.equal(env.submit.disabled,false);
      await env.advance(1100); assert.equal(env.requests.length,4,'freshness timer resumes from the server lifetime');
      await env.finish(env.requests[3],complete({state:'stale',stale:true,fresh_for_seconds:0}));
      assert.equal(env.panel.dataset.readinessCurrent,'false');
      await env.advance(600000); assert.equal(env.requests.length,4);
    } else if (scenario === 'idle') {
      await env.advance(600000); assert.equal(env.requests.length,0,'opening the page never starts a check');
      assert.equal(env.submit.disabled,false);
    } else throw Error('unknown scenario');
    console.log('readiness '+scenario+' passed');
  } finally {env.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


@pytest.mark.parametrize('scenario', ['complete', 'auth401', 'auth403', 'malformed', 'pause', 'pagehide',
                                     'failures', 'timeout', 'freshness', 'initial-freshness', 'cooldown-pause', 'idle'])
def test_readiness_polling_behavior(scenario):
    node = ROOT / '.cache/node-v24.18.0-darwin-arm64/bin/node'
    if not node.is_file():
        node = shutil.which('node')
    if not node or not (ROOT / 'web/node_modules/jsdom').is_dir():
        pytest.skip('Node.js and the web development dependencies are needed for DOM tests')
    report = {'schema':'autograde-classroom-readiness-v1', 'state':'checking', 'checking':True, 'stale':False,
              'checked_at':None, 'retry_after_seconds':30, 'fresh_for_seconds':0,
              'checks':[{'id':key, 'title':key, 'state':'checking', 'summary':'이전 표시',
                         'details':['기존 점검 내용'], 'actions':['기존 안내'], 'checked_at':None}
                        for key in ('next','external','workers','backup')]}
    if scenario == 'initial-freshness':
        report.update(state='ready', checking=False, checked_at='2026-10-31T01:00:00Z', retry_after_seconds=0,
                      fresh_for_seconds=2)
        for check in report['checks']:
            check.update(state='ok', checked_at=report['checked_at'])
    elif scenario == 'idle':
        report.update(state='not_checked', checking=False, retry_after_seconds=0)
        for check in report['checks']:
            check['state'] = 'unknown'
    payload = {'script':SCRIPT, 'report':report, 'html':'<!doctype html><html><body>' + render(report, 'synthetic-csrf') + '</body></html>'}
    result = subprocess.run([str(node), '-e', HARNESS, scenario], input=json.dumps(payload), text=True,
                            capture_output=True, cwd=ROOT / 'web', timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f'readiness {scenario} passed' in result.stdout
