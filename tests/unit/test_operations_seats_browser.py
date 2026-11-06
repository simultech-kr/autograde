"""Seat monitoring against its real server-rendered DOM with deterministic I/O."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from autograde.operations_seats_browser import SCRIPT
from autograde.operations_seats_view import render_seats, seat_view


ROOT = Path(__file__).resolve().parents[2]
HARNESS = r"""
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const scenario = process.argv[1];
const flush = () => new Promise(resolve => setImmediate(resolve));
const clone = value => JSON.parse(JSON.stringify(value));
const report = () => clone(input.report);
async function setup() {
  const dom = new JSDOM(input.html, {url:'https://class.example/instructor/operations', runScripts:'outside-only', pretendToBeVisual:true});
  const w=dom.window, d=w.document;
  if (d.readyState !== 'complete') await new Promise(resolve => w.addEventListener('load', resolve, {once:true}));
  let now=0, serial=0, hidden=false, online=true;
  const timers=new Map(), requests=[];
  w.setTimeout=(callback, delay=0) => {const id=++serial; timers.set(id,{callback,at:now+delay}); return id;};
  w.clearTimeout=id=>timers.delete(id);
  Object.defineProperty(d,'hidden',{get:()=>hidden,configurable:true});
  Object.defineProperty(w.navigator,'onLine',{get:()=>online,configurable:true});
  for (const key of ['localStorage','sessionStorage','indexedDB']) Object.defineProperty(w,key,{get(){throw Error('Unexpected persistence');}});
  w.fetch=(url,options)=>new Promise((resolve,reject)=>{
    requests.push({url,options,resolve,reject});
    options.signal.addEventListener('abort',()=>reject(Error('aborted')));
  });
  w.eval(input.script);
  const advance=async delta=>{
    const target=now+delta; let iterations=0;
    while(true) {
      const next=[...timers].sort((a,b)=>a[1].at-b[1].at)[0];
      if(!next || next[1].at>target)break;
      if(++iterations>100)throw Error('unbounded timer loop');
      timers.delete(next[0]); now=Math.max(now,next[1].at); next[1].callback(); await flush();
    }
    now=target; await flush();
  };
  const finish=async(request,data,status=200,raw)=>{
    request.resolve({status,ok:status>=200&&status<300,headers:{get:name=>name.toLowerCase()==='content-type'?'application/json':null},
      text:async()=>raw===undefined?JSON.stringify(data):raw});
    await flush();
  };
  const panel=d.querySelector('#operations-seats'), tbody=panel.querySelector('table tbody');
  const refresh=panel.querySelector('[data-seat-refresh]');
  return {w,d,panel,tbody,requests,timers,advance,finish,refresh,
    rows:()=>[...tbody.querySelectorAll('tr[data-result-key]')],
    row:id=>tbody.querySelector(`tr[data-result-key="${id}"]`),
    visible:()=>[...tbody.querySelectorAll('tr[data-result-key]')].filter(row=>!row.hidden).map(row=>row.dataset.resultKey),
    filter:key=>panel.querySelector(`[data-seat-filter="${key}"]`).click(),
    search:query=>{const field=panel.querySelector('[data-seat-search]');field.value=query;field.dispatchEvent(new w.Event('input'));},
    status:panel.querySelector('[data-seat-refresh-status]'),
    poll:async()=>{refresh.click();await advance(0);return requests.at(-1);},
    hidden:value=>{hidden=value;d.dispatchEvent(new w.Event('visibilitychange'));},
    online:value=>{online=value;w.dispatchEvent(new w.Event(value?'online':'offline'));},
    close:()=>dom.window.close()};
}
(async()=>{
  const e=await setup();
  try {
    if(scenario==='filter') {
      assert.equal(e.panel.querySelector('[data-seat-controls]').hidden,false);
      for(const row of e.rows()) {
        const select=row.querySelector('select'), form=row.querySelector('form');
        assert.ok(form && form.querySelector('button[type="submit"]'),'every seat retains a usable policy form');
        assert.equal(select.closest('form'),form);
      }
      assert.deepEqual(e.visible(),['lab-failed','lab-stale','lab-ready','lab-disabled']);
      e.filter('attention'); assert.deepEqual(e.visible(),['lab-failed','lab-stale']);
      e.search('STALE'); assert.deepEqual(e.visible(),['lab-stale']);
      e.search('no such seat'); assert.deepEqual(e.visible(),[]);
      assert.equal(e.panel.querySelector('[data-seat-filter-empty]').hidden,false);
      e.search(''); e.filter('recent'); assert.deepEqual(e.visible(),['lab-failed','lab-ready']);
      e.filter('disabled'); assert.deepEqual(e.visible(),['lab-disabled']);
      e.filter('all'); assert.equal(e.visible().length,4);
      assert.equal(e.panel.querySelector('[data-seat-count="attention"]').textContent,'2');
      assert.equal(e.requests.length,0,'filtering must not fetch or start a probe');
    } else if(scenario==='safe-refresh') {
      const original=e.row('lab-ready'), detail=e.panel.querySelector('details'); detail.open=true;
      await e.advance(15000); assert.equal(e.requests.length,1);
      const data=report(), seat=data.seats.find(item=>item.seat_id==='lab-ready');
      seat.label='<img src=x onerror="window.compromised=true">';
      seat.cleanup_label='<script>window.compromised=true</script>';
      await e.finish(e.requests[0],data);
      assert.equal(e.row('lab-ready'),original); assert.equal(detail.open,true);
      assert.equal(original.querySelector('[data-seat-field="label"]').textContent,seat.label);
      assert.ok([...original.querySelectorAll('[data-seat-field="label"]')].every(node=>node.textContent===seat.label));
      assert.equal(e.d.querySelector('img'),null); assert.equal(e.d.querySelector('script'),null);
      assert.equal(e.w.compromised,undefined); assert.equal(e.panel.dataset.seatCurrent,'true');
      await e.advance(15000); assert.equal(e.requests.length,2);
      await e.finish(e.requests[1],report());
    } else if(scenario==='editing') {
      const row=e.row('lab-ready'), select=row.querySelector('select');
      const order=e.rows().map(item=>item.dataset.resultKey);
      select.focus(); select.value='next_start'; select.dispatchEvent(new e.w.Event('change',{bubbles:true}));
      const data=report(), seat=data.seats.find(item=>item.seat_id==='lab-ready');
      seat.priority=0; seat.attention=true; seat.ide_version='1.200.0';
      await e.finish(await e.poll(),data);
      assert.equal(select.value,'next_start'); assert.equal(select.dataset.serverValue,'on_exit');
      assert.equal(e.d.activeElement,select); assert.equal(e.row('lab-ready'),row);
      assert.deepEqual(e.rows().map(item=>item.dataset.resultKey),order);
      assert.equal(row.querySelector('[data-seat-field="ide_version"]').textContent,'1.200.0');
      select.blur(); await flush();
      assert.deepEqual(e.rows().map(item=>item.dataset.resultKey),order,'unsaved choice also prevents reordering');
      const other=e.row('lab-stale').querySelector('select');
      const updated=report(), stale=updated.seats.find(item=>item.seat_id==='lab-stale');
      stale.cleanup_mode='next_start'; stale.cleanup_mode_label='다음 IDE 시작';
      await e.finish(await e.poll(),updated);
      assert.equal(other.value,'next_start','unfocused clean selection follows the server');
      assert.equal(select.value,'next_start','unsaved selection remains unchanged');
    } else if(scenario==='focused-clean') {
      const select=e.row('lab-ready').querySelector('select'); select.focus();
      const data=report(), seat=data.seats.find(item=>item.seat_id==='lab-ready');
      seat.cleanup_mode='next_start'; seat.cleanup_mode_label='다음 IDE 시작';
      await e.finish(await e.poll(),data);
      assert.equal(e.d.activeElement,select); assert.equal(select.value,'on_exit');
      assert.equal(select.dataset.serverValue,'next_start');
    } else if(scenario==='dirty-roundtrip') {
      const select=e.row('lab-ready').querySelector('select');
      select.value='next_start'; select.dispatchEvent(new e.w.Event('change',{bubbles:true}));
      const matching=report(), seat=matching.seats.find(item=>item.seat_id==='lab-ready');
      seat.cleanup_mode='next_start'; seat.cleanup_mode_label='다음 IDE 시작';
      await e.finish(await e.poll(),matching);
      assert.equal(select.dataset.serverValue,'next_start'); assert.equal(select.value,'next_start');
      // Another operator happened to select the same value, then changed it.
      // A temporary match must not silently erase this operator's unsaved intent.
      await e.finish(await e.poll(),report());
      assert.equal(select.dataset.serverValue,'on_exit');
      assert.equal(select.value,'next_start','an intervening matching poll must not clear the dirty choice');
      // Explicitly returning to the current server value ends the local edit.
      select.value='on_exit'; select.dispatchEvent(new e.w.Event('change',{bubbles:true}));
      await e.finish(await e.poll(),matching);
      assert.equal(select.value,'next_start','a deliberate reset allows subsequent server updates');
    } else if(scenario==='inventory') {
      const removed=e.row('lab-failed');
      const data=report(); data.seats=data.seats.filter(item=>item.seat_id!=='lab-failed');
      data.seats.push({...clone(data.seats[0]),seat_id:'lab-new',label:'New seat',priority:0});
      await e.finish(await e.poll(),data);
      assert.equal(removed.hidden,true);
      assert.ok([...removed.querySelectorAll('select,button[type="submit"]')].every(node=>node.disabled));
      assert.equal(e.panel.querySelector('[data-seat-count="all"]').textContent,'4');
      const added=e.row('lab-new'), select=added.querySelector('select'), form=added.querySelector('form');
      assert.equal(form.getAttribute('action'),'/instructor/operations/seats/lab-new/policy');
      assert.equal(form.method,'post');
      assert.equal(form.querySelector('[name="csrf"]').value,'synthetic-csrf');
      assert.equal(added.querySelector('label').htmlFor,select.id);
      assert.equal(added.querySelector('[data-seat-field="seat_id"]').textContent,'lab-new');
      assert.equal(added.querySelector('[data-seat-record]').dataset.seatId,'lab-new');
      e.search('new'); assert.deepEqual(e.visible(),['lab-new']);
      await e.finish(await e.poll(),report()); e.search('');
      assert.equal(removed.hidden,false); assert.equal(removed.querySelector('select').disabled,false);
      assert.equal(added.hidden,true);
    } else if(scenario==='auth401'||scenario==='auth403') {
      await e.finish(await e.poll(),{secret:'private body'},Number(scenario.slice(4)));
      assert.match(e.status.textContent,/로그인|권한/); assert.equal(e.refresh.disabled,true);
      assert.ok(e.rows().every(row=>row.querySelector('select').disabled));
      e.hidden(true);e.hidden(false);e.online(false);e.online(true);e.refresh.click();
      await e.advance(300000); assert.equal(e.requests.length,1);
      assert.doesNotMatch(e.d.body.textContent,/private body/);
    } else if(['malformed','duplicate','oversize','unavailable'].includes(scenario)) {
      const row=e.row('lab-ready'), initial=row.textContent, data=report();
      if(scenario==='malformed') delete data.seats[0].cleanup_label;
      if(scenario==='duplicate') data.seats.push(clone(data.seats[0]));
      if(scenario==='unavailable') data.available=false;
      await e.finish(await e.poll(),data,200,scenario==='oversize'?' '.repeat(16777217):undefined);
      assert.equal(row.textContent,initial); assert.equal(e.row('lab-ready'),row);
      assert.equal(e.panel.dataset.seatCurrent,'false'); assert.equal(row.querySelector('select').disabled,true);
      assert.match(e.status.textContent,/마지막 확인/);
      await e.finish(await e.poll(),report());
      assert.equal(e.panel.dataset.seatCurrent,'true'); assert.equal(row.querySelector('select').disabled,false);
    } else if(scenario==='pause') {
      e.hidden(true);await e.advance(60000);assert.equal(e.requests.length,0);
      e.online(false);e.hidden(false);await e.advance(60000);assert.equal(e.requests.length,0);
      assert.match(e.status.textContent,/오프라인/);
      e.online(true);await e.advance(0);assert.equal(e.requests.length,1);
      e.hidden(true);assert.equal(e.requests[0].options.signal.aborted,true);
      const late=report();late.seats[0].label='late response';await e.finish(e.requests[0],late);
      assert.doesNotMatch(e.d.body.textContent,/late response/);
      await e.advance(60000);assert.equal(e.requests.length,1);
      e.hidden(false);await e.advance(0);assert.equal(e.requests.length,2);
      await e.finish(e.requests[1],report());assert.equal(e.panel.dataset.seatCurrent,'true');
    } else if(scenario==='pagehide') {
      const first=await e.poll();e.w.dispatchEvent(new e.w.Event('pagehide'));
      assert.equal(first.options.signal.aborted,true);await e.advance(60000);assert.equal(e.requests.length,1);
      e.w.dispatchEvent(new e.w.Event('pageshow'));await e.advance(0);assert.equal(e.requests.length,2);
      await e.finish(e.requests[1],report());
    } else if(scenario==='backoff') {
      await e.advance(15000);
      for(let i=0;i<3;i++) {
        e.requests[i].reject(Error('private-token-error'));await flush();
        if(i<2)await e.advance(i===0?30000:60000);
      }
      assert.match(e.status.textContent,/자동 확인을 중단/);assert.doesNotMatch(e.d.body.textContent,/private-token-error/);
      await e.advance(300000);assert.equal(e.requests.length,3);
      await e.finish(await e.poll(),report());assert.equal(e.requests.length,4);assert.equal(e.panel.dataset.seatCurrent,'true');
    } else if(scenario==='timeout') {
      const first=await e.poll();await e.advance(5000);
      assert.equal(first.options.signal.aborted,true);assert.equal(e.panel.dataset.seatCurrent,'false');
      assert.equal(e.row('lab-ready').querySelector('select').disabled,true);
    } else if(scenario==='submit') {
      const first=await e.poll();
      e.row('lab-ready').querySelector('form').dispatchEvent(new e.w.Event('submit',{bubbles:true,cancelable:true}));
      assert.equal(first.options.signal.aborted,true);await e.advance(60000);assert.equal(e.requests.length,1);
    } else throw Error('unknown scenario');
    for(const request of e.requests) {
      assert.equal(request.url,'/instructor/operations/seats/status');
      assert.equal(request.options.method,'GET'); assert.equal(request.options.credentials,'same-origin');
      assert.equal(request.options.cache,'no-store'); assert.equal(request.options.redirect,'error');
      assert.deepEqual(Object.keys(request.options.headers),['Accept']); assert.equal(request.options.body,undefined);
    }
    console.log('seats '+scenario+' passed');
  } finally {e.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


@pytest.mark.parametrize('scenario', [
    'filter', 'safe-refresh', 'editing', 'focused-clean', 'dirty-roundtrip', 'inventory', 'auth401', 'auth403',
    'malformed', 'duplicate', 'oversize', 'unavailable', 'pause', 'pagehide', 'backoff', 'timeout', 'submit',
])
def test_seat_monitoring_browser(scenario):
    node = ROOT / '.cache/node-v26.13.0-darwin-arm64/bin/node'
    if not node.is_file():
        node = shutil.which('node')
    modules = next((folder for folder in (ROOT / 'web/node_modules', ROOT / '.cache/operations-dom-tests/node_modules')
                    if (folder / 'jsdom/package.json').is_file()), None)
    if not node or modules is None:
        pytest.skip('Node.js and jsdom are required for DOM behavior tests')
    base = {'active': True, 'presence': 'recent', 'report_state': 'valid', 'cleanup': 'ok', 'update': 'current',
            'platform': 'win32', 'policy_state': 'matched', 'cleanup_mode': 'on_exit', 'reported_cleanup_mode': 'on_exit',
            'ide_version': '1.143.0', 'extension_version': '0.5.22', 'last_seen_at': '2026-11-06T09:00:00Z'}
    seats = [{**base, 'seat_id': 'lab-ready', 'label': 'Ready seat'},
             {**base, 'seat_id': 'lab-failed', 'label': 'Failed seat', 'cleanup': 'failed'},
             {**base, 'seat_id': 'lab-stale', 'label': 'Stale seat', 'presence': 'stale', 'policy_state': 'unknown'},
             {**base, 'seat_id': 'lab-disabled', 'label': 'Disabled seat', 'active': False}]
    payload = {'script': SCRIPT,
               'report': {'schema': 'autograde-seats-status-v1', 'available': True,
                          'observed_at': '2026-11-06T09:00:00Z', 'seats': [seat_view(seat) for seat in seats]},
               'html': '<!doctype html><html><body>' + render_seats(seats, 'synthetic-csrf') + '</body></html>'}
    result = subprocess.run([str(node), '-e', HARNESS, scenario], input=json.dumps(payload), text=True,
                            capture_output=True, cwd=ROOT, timeout=15,
                            env={**os.environ, 'NODE_PATH': str(modules)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert f'seats {scenario} passed' in result.stdout
