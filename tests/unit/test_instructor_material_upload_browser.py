"""Execute the upload enhancement against a small DOM/XHR event harness."""
import shutil
import subprocess

import pytest

from autograde.instructor_material_upload_browser import SCRIPT


def test_upload_progress_retry_and_review_navigation(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for browser-script event tests')
    harness = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const script = require('node:fs').readFileSync(process.argv[2], 'utf8');
function setup() {
  const listen = () => ({events:{}, addEventListener(name, fn) { this.events[name] = fn; }});
  const input = Object.assign(listen(), {files:[{name:'class.zip',size:1000}]});
  const status = {textContent:'',append(...parts){this.parts=parts;}};
  const meter = {hidden:true,value:0,removeAttribute(name) { delete this[name]; }};
  const submit = {disabled:false};
  const form = Object.assign(listen(), {action:'https://school.example/courses/c1/instructor/materials/mat_a/upload-batch',
    querySelector(s) { return {'input[type=file]':input,'[data-upload-progress]':status,
      '[data-upload-meter]':meter,'button[type=submit],button:not([type])':submit}[s]; },
    setAttribute(){},removeAttribute(){}});
  const requests = [];
  class XHR { constructor() { Object.assign(this,listen()); this.upload=listen(); requests.push(this); }
    open(method,url) {this.method=method; this.url=url;} send(data) {this.data=data;} }
  const location = {href:'https://school.example/material',origin:'https://school.example',assign(url){this.assigned=url;}};
  const window = Object.assign(listen(),{location,confirm:()=>true});
  const context = {document:{querySelectorAll:()=>[form],createElement:()=>({})},window,guarded:[form],dirty:()=>false,
    XMLHttpRequest:XHR,FormData:class {constructor(form){this.form=form;}},URL,
    DOMParser:class {parseFromString(text){return {querySelector:()=>({textContent:text})};}},leaving:false};
  vm.createContext(context); vm.runInContext(script,context);
  const start = () => {let prevented=false; form.events.submit({preventDefault(){prevented=true;}});assert.ok(prevented);};
  return {input,status,meter,submit,requests,start,window,context};
}
let s=setup(); s.input.files[0].size=101*1048576;s.start();
assert.equal(s.requests.length,0);assert.match(s.status.textContent,/100 MiB/);
s=setup();s.start();s.start();assert.equal(s.requests.length,1);assert.equal(s.submit.disabled,true);
let req=s.requests[0];req.upload.events.progress({lengthComputable:true,loaded:50,total:100});
assert.match(s.status.textContent,/50%/);req.upload.events.load();assert.match(s.status.textContent,/파일 형식과 학번/);
req.events.error();assert.equal(s.submit.disabled,false);assert.equal(s.input.files.length,1);assert.match(s.status.textContent,/선택한 파일/);
s.start();req=s.requests[1];req.status=413;req.responseURL=s.window.location.href;req.events.load();
assert.match(s.status.textContent,/413/);assert.equal(s.input.files.length,1);assert.equal(s.window.location.assigned,undefined);
s.start();req=s.requests[2];req.status=200;req.responseURL='https://evil.example/batch-review/mbatch_'+'a'.repeat(32);req.responseText='not permitted';req.events.load();
assert.equal(s.window.location.assigned,undefined);
s.start();req=s.requests[3];req.status=200;req.responseURL=req.url.replace('/upload-batch','/batch-review/mbatch_'+'a'.repeat(32));req.events.load();
assert.equal(s.window.location.assigned,req.responseURL);assert.equal(s.context.leaving,true);
s=setup();s.start();s.context.guarded.push({});s.context.dirty=()=>true;s.window.confirm=()=>false;
req=s.requests[0];req.status=200;req.responseURL=req.url.replace('/upload-batch','/batch-review/mbatch_'+'b'.repeat(32));req.events.load();
assert.equal(s.window.location.assigned,undefined);assert.equal(s.context.leaving,false);
assert.equal(s.status.parts[1].href,req.responseURL);assert.match(s.status.textContent,/다른 영역을 먼저 저장/);
console.log('material upload progress/retry/navigation checks passed');
'''
    source = tmp_path / 'upload.js'
    source.write_text(SCRIPT)
    runner = tmp_path / 'check.cjs'
    runner.write_text(harness)
    result = subprocess.run([node, str(runner), str(source)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
