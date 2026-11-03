"""Exercise the shipped legacy consent dialog against a real DOM."""
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from autograde.student_browser import SCRIPT
from autograde.student_privacy import POLICY_VERSION
from autograde.student_privacy_web import prompt_html

ROOT = Path(__file__).resolve().parents[2]
HARNESS = r"""
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const scenario = process.argv[1], flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  const dom = new JSDOM(input.html, {url:'https://class.example/courses/come2201/portal', runScripts:'outside-only', pretendToBeVisual:true});
  const w=dom.window, d=w.document, requests=[];
  try {
    for (const key of ['localStorage','sessionStorage','indexedDB']) Object.defineProperty(w,key,{get(){throw Error('unexpected persistence');}});
    w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
    w.HTMLDialogElement.prototype.close=function(){this.open=false;};
    w.fetch=(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}));
    d.getElementById('before').focus(); w.eval(input.script); await flush();
    assert.equal(requests.length,1); assert.equal(requests[0].url,'/student/api/course/come2201/privacy');
    assert.equal(requests[0].options.credentials,'same-origin'); assert.equal(requests[0].options.cache,'no-store');
    const finish=async (request,result,status=200)=>{request.resolve({ok:status===200,headers:{get:()=> 'application/json'},json:async()=>({ok:status===200,result})});await flush();};
    const dialog=d.querySelector('dialog'), form=d.querySelector('[data-privacy-choice]');
    const grant=form.querySelector('[value="granted"]'), decline=form.querySelector('[value="declined"]');
    assert.equal(grant.textContent,'동의합니다'); assert.equal(decline.textContent,'동의하지 않습니다');
    assert.equal(grant.className,decline.className,'participation and refusal have equal visual emphasis');
    const result={student_key:'20260001',course_key:'come2201',decision:'unanswered',prompt_dismissed:false,revision:0,policy:{version:input.version}};
    if (scenario==='answered') result.decision='declined';
    if (scenario==='dismissed') result.prompt_dismissed=true;
    if (scenario==='wrong-account') result.student_key='20260002';
    if (scenario==='new-policy') result.policy.version='other';
    await finish(requests[0],result);
    if (['answered','dismissed','wrong-account','new-policy'].includes(scenario)) {
      assert.equal(dialog.open,false); assert.equal(requests.length,1);
    } else {
      assert.equal(dialog.open,true); assert.equal(requests.length,1,'reading never records a decision');
      if (scenario==='later' || scenario==='escape') {
        if (scenario==='later') d.querySelector('[data-privacy-later]').click();
        else dialog.dispatchEvent(new w.Event('cancel',{cancelable:true}));
        await flush(); assert.equal(dialog.open,false); assert.equal(requests.length,2);
        assert.equal(requests[1].url,'/courses/come2201/privacy/dismiss');
        const fields=new URLSearchParams(requests[1].options.body);
        assert.deepEqual([...fields.keys()].sort(),['csrf','policy_version']);
        assert.equal(d.activeElement.id,'before');
        await finish(requests[1],{dismissed:true});
      } else {
        const decision=scenario==='grant'?'granted':'declined';
        const button=form.querySelector('[value="'+decision+'"]');
        form.dispatchEvent(new w.SubmitEvent('submit',{bubbles:true,cancelable:true,submitter:button}));
        await flush(); assert.equal(requests.length,2); assert.equal(button.disabled,true);
        const fields=new URLSearchParams(requests[1].options.body);
        assert.equal(fields.get('decision'),decision); assert.equal(fields.get('revision'),'0');
        assert.equal(fields.get('csrf'),'synthetic-csrf'); assert.equal(fields.has('student_key'),false);
        if (scenario==='lost-response') {
          await finish(requests[1],{},500); assert.equal(dialog.open,true); assert.equal(form.hidden,true);
          assert.match(d.querySelector('[data-privacy-status]').textContent,/저장 결과를 확인하지 못했습니다/);
          assert.equal(requests.length,2,'never retries an uncertain write');
          d.querySelector('[data-privacy-later]').click(); await flush();
          assert.equal(dialog.open,false); assert.equal(requests.length,2);
        } else {
          await finish(requests[1],{...result,decision,revision:1}); assert.equal(dialog.open,false);
        }
      }
    }
    console.log('consent '+scenario+' passed');
  } finally {dom.window.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


@pytest.mark.parametrize('scenario', ['answered', 'dismissed', 'wrong-account', 'new-policy', 'later', 'escape', 'grant', 'decline', 'lost-response'])
def test_legacy_consent_prompt(scenario):
    node = ROOT / '.cache/node-v24.18.0-darwin-arm64/bin/node'
    if not node.is_file():
        node = shutil.which('node')
    if not node or not (ROOT / 'web/node_modules/jsdom').is_dir():
        pytest.skip('Node.js and web development dependencies are needed for DOM tests')
    session = SimpleNamespace(credential=SimpleNamespace(student_key='20260001'), csrf='synthetic-csrf')
    payload = {'html':'<!doctype html><html><body><button id="before">수업 화면</button>' + prompt_html('come2201', session, '합성 수업') + '</body></html>',
               'script':SCRIPT, 'version':POLICY_VERSION}
    result = subprocess.run([str(node), '-e', HARNESS, scenario], input=json.dumps(payload), text=True,
                            capture_output=True, cwd=ROOT / 'web', timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f'consent {scenario} passed' in result.stdout
