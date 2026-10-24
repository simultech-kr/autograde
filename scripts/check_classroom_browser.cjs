// Optional browser check: synthetic records and real local HTTP/SSE only.
// node scripts/check_classroom_browser.cjs FIXTURE.html PLAYWRIGHT_MODULE [CHROME_BINARY] [NEXT_ORIGIN]
const fs = require('node:fs');
const http = require('node:http');
const assert = require('node:assert/strict');
const path = require('node:path');
const [fixture, playwrightModule = 'playwright', executablePath, nextOrigin] = process.argv.slice(2);
if (!fixture) throw new Error('Run test_instructor_classroom.py and supply its classroom.html fixture');
const { chromium } = require(playwrightModule);
const html = fs.readFileSync(fixture, 'utf8');
const streams = new Set();
let revision = 1, denied = false, unavailable = false, detailDelay = 0, detailRequests = 0, removedStudent = false;
const commandRecords = new Map(); let loseNextCommandResponse = false, commandPosts = 0;
const kinds = ['notice','open_assignment','sync_materials','check_environment','save_submit'];
const date = '2026-10-25T10:00:00Z';
const old = { submission_id:'old',received_at:'2026-10-24T10:00:00Z',score:90,max_score:100,state:'published',code_version:'a'.repeat(64),delivery_mode:'bundle' };
let attempt = null;
const meta = assignment => ({contract_version:'1',request_id:'synthetic',course_key:'come2201',version:String(revision).padStart(64,'0'),server_time:date,selected_assignment_id:assignment || null});
const students = assignment => Array.from({length:20},(_,i) => ({student_key:`TEST-${String(i+1).padStart(3,'0')}`,student_name:i ? `가상 학생 ${i+1}` : '가상 <img src=x onerror=alert(1)>',commands_version:String(revision).padStart(64,'0'),connection:{state:i === 19?'never_connected':i === 18?'offline':i === 17?'stale':'online',last_seen_at:i === 19?null:date,environment:null},activity:{kind:revision > 1 && !i?'file_saved':'workspace_opened',received_at:date,assignment_id:'assignment-1'},submission:{state:assignment?(i===16?'not_assigned':attempt&&!i?attempt.state:'unsubmitted'):'assignment_required',submitted:Boolean(assignment&&attempt&&!i),latest:assignment&&!i?attempt:null,previous_best:assignment&&attempt&&!i?old:null}})).filter(s=>!removedStudent||s.student_key!=='TEST-001');
const snapshot = assignment => ({...meta(assignment),assignments:[{assignment_id:'assignment-1',assignment_key:'week1',release_id:'r1',title:'가상 실습',delivery_mode:'bundle'}],summary:{total:removedStudent?19:20,online:removedStudent?16:17,stale:1,offline:1,never_connected:1,submitted:assignment&&attempt?1:0,pending:attempt?.state==='received'?1:0},students:students(assignment)});
const detail = (assignment,key) => ({...meta(assignment),student:{...students(assignment).find(s=>s.student_key===key),activities:[{kind:'file_saved',received_at:date,assignment_id:'assignment-1'}],submissions:assignment&&attempt?[attempt,old]:[],code_storage:{state:attempt?'receipt_recorded':'none',verified:false,message:'서버 제출 보관 기록입니다. 자동 백업과 다릅니다.'},analysis:{workspace_opened:1,file_saved:revision,submission_started:attempt?1:0,submission_finished:attempt?1:0,window:'recent100',note:'최근 최대 100개 IDE 보고 · 집중도를 뜻하지 않습니다.'},controls:{supported:true,actions:kinds,reason:'학생 확인 후 IDE 내부에서 실행합니다.'},commands:key==='TEST-001'?[...commandRecords.values()].reverse():[]}});
function publish(event='snapshot') { for (const client of streams) client.response.write(`id: ${String(revision).padStart(64,'0')}\nevent: ${event}\ndata: ${JSON.stringify(event==='snapshot'?snapshot(client.assignment):{error:{code:'access_revoked'}})}\n\n`); }
const server = http.createServer((request,response)=>{
  const url = new URL(request.url,'http://localhost');
  if (!url.pathname.startsWith('/instructor/api/')) {
    if (nextOrigin) { const upstream=http.request(new URL(request.url,nextOrigin),{headers:request.headers},remote=>{response.writeHead(remote.statusCode,remote.headers);remote.pipe(response);});upstream.on('error',()=>{response.writeHead(502);response.end();});request.pipe(upstream);return; }
    response.writeHead(200,{'Content-Type':'text/html;charset=utf-8'}); response.end(html); return;
  }
  if (denied || unavailable) { response.writeHead(denied?403:503,{'Content-Type':'application/json'}); response.end(JSON.stringify({error:{code:denied?'forbidden':'unavailable',message:'Synthetic access status'}})); return; }
  const assignment=url.searchParams.get('assignment_id') || '';
  if(url.pathname.endsWith('/bootstrap')) { response.writeHead(200,{'Content-Type':'application/json'});response.end(JSON.stringify({contract_version:'1',request_id:'synthetic',server_time:date,principal:{id:'test',display_name:'Test instructor',mode:'shared'},courses:[{course_key:'come2201',name:'Test Course',code:'come2201',section:'01',status:'active'}],csrf:'test',capabilities:['classroom.read','classroom.control']}));return; }
  if(url.pathname.endsWith('/commands')) {
    assert.equal(request.method,'POST');assert.equal(request.headers['content-type'],'application/json');
    assert.equal(request.headers.origin,`http://${request.headers.host}`);
    let body='';request.on('data',part=>body+=part);request.on('end',()=>{
      const value=JSON.parse(body);commandPosts++;assert.equal(value.csrf,'test');assert.ok(kinds.includes(value.kind));assert.ok(value.request_key);
      const duplicate=commandRecords.has(value.request_key);
      if(!duplicate)commandRecords.set(value.request_key,{command_id:`cmd_${commandRecords.size+1}`,kind:value.kind,assignment_id:value.assignment_id||null,message:value.message,status:'queued',created_by:'Synthetic teacher',created_at:date,expires_at:'2026-10-25T10:10:00Z',received_at:null,accepted_at:null,finished_at:null,result_code:null,result:null});
      revision++;publish();
      if(loseNextCommandResponse){loseNextCommandResponse=false;response.writeHead(503,{'Content-Type':'application/json'});response.end(JSON.stringify({error:{code:'synthetic_lost_ack',message:'Synthetic acknowledgement unavailable'}}));return;}
      response.writeHead(duplicate?200:201,{'Content-Type':'application/json'});response.end(JSON.stringify({contract_version:'1',request_id:'synthetic',server_time:date,command:commandRecords.get(value.request_key),duplicate}));
    });return;
  }
  if(url.pathname.endsWith('/events')) {
    response.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-cache','Connection':'keep-alive'});
    const client={response,assignment}; streams.add(client);
    response.write(`event: snapshot\ndata: ${JSON.stringify(snapshot(assignment))}\n\n`);
    request.on('close',()=>streams.delete(client)); return;
  }
  const isDetail=url.pathname.includes('/students/'); if(isDetail)detailRequests++;
  const send=()=>{response.writeHead(200,{'Content-Type':'application/json'});response.end(JSON.stringify(isDetail?detail(assignment,decodeURIComponent(url.pathname.split('/').at(-1))):snapshot(assignment)));};
  if(isDetail&&detailDelay)setTimeout(send,detailDelay);else send();
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const base=`http://127.0.0.1:${server.address().port}`;
  const browser=await chromium.launch({headless:true,...(executablePath?{executablePath}:{})});
  const page=await browser.newPage(); const errors=[]; page.on('pageerror',error=>errors.push(error.message));
  const cardSelector=nextOrigin?'button[data-student]':'.cr-card';
  const streamSelector=nextOrigin?'[data-stream]':'[data-cr-stream]';
  const detailSelector=nextOrigin?'dialog':'[data-cr-detail]';
  const closeSelector=nextOrigin?'button[aria-label="학생 상세 닫기"]':'[data-cr-close]';
  const tabButton=tab=>nextOrigin?page.locator('dialog nav').getByRole('button',{name:{overview:'코드·제출',activity:'활동·분석',control:'IDE 요청'}[tab],exact:true}):page.locator(`[data-cr-tab="${tab}"]`);
  const filterSelect=()=>nextOrigin?page.getByLabel('제출 상태',{exact:true}):page.locator('[data-cr-filter]');
  const assignmentSelect=()=>nextOrigin?page.getByLabel('공식 제출 확인 과제',{exact:true}):page.locator('[data-cr-assignment]');
  const refreshButton=()=>nextOrigin?page.getByRole('button',{name:'다시 연결',exact:true}).first():page.locator('[data-cr-refresh]');
  const wait=async predicate=>{for(let i=0;i<80;i++){if(await predicate())return;await page.waitForTimeout(50);}throw new Error('State transition timed out');};
  let layouts=0;
  try {
    await page.goto(base+(nextOrigin?'/app/classroom?course_key=come2201&assignment_id=assignment-1':'/courses/come2201/instructor/classroom?assignment_id=assignment-1'));
    await wait(async()=>await page.locator(cardSelector).count()===20);
    assert.equal(await page.locator('img').count(),0,'untrusted names remain text');
    await wait(async()=>(await page.locator(streamSelector).textContent()).includes('실시간 수신'));
    const first=page.locator('[data-student="TEST-001"]');
    await first.focus(); await page.keyboard.press('Enter');
    await wait(async()=>await page.locator('dialog').isVisible());
    await wait(async()=>(await page.locator(detailSelector).textContent()).includes('서버 코드 보관'));
    await page.keyboard.press('Escape');
    assert.equal(await first.evaluate(node=>document.activeElement===node),true,'Escape restores card focus');
    await first.click();
    attempt={...old,submission_id:'new',received_at:date,state:'received',score:null,max_score:null}; revision++; publish();
    await wait(async()=>(await page.locator(detailSelector).textContent()).includes('점수 미확정'));
    assert.match(await page.locator(detailSelector).textContent(),/90 \/ 100점 · 최신 제출 제외/);
    assert.equal(await page.locator('[data-student]').first().getAttribute('data-student'),'TEST-001','updates never reorder cards');
    attempt={...attempt,state:'published',score:80,max_score:100};revision++;publish();
    await wait(async()=>(await page.locator(detailSelector).textContent()).includes('80 / 100점'));
    assert.match(await page.locator(detailSelector).textContent(),/90 \/ 100점 · 최신 제출 제외/);
    // A newly accepted receipt must immediately invalidate the old detail grade,
    // even if its detail request is slow. Previous best is never the new result.
    detailDelay=750;const requestsBefore=detailRequests;
    attempt={...attempt,submission_id:'newer',state:'received',score:null,max_score:null};revision++;publish();
    await wait(async()=>detailRequests>requestsBefore);
    assert.doesNotMatch(await page.locator(detailSelector).textContent(),/80 \/ 100점/);
    await wait(async()=>(await page.locator(detailSelector).textContent()).includes('점수 미확정'));detailDelay=0;
    for(const colorScheme of ['light','dark']) for(const width of [320,375,768,1280]) {
      await page.emulateMedia({colorScheme});await page.setViewportSize({width,height:900});
      await page.locator(closeSelector).click();
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth+1),`${width} ${colorScheme} cards overflow`);layouts++;
      await first.click();
      for(const tab of ['overview','activity','control']) {
        await tabButton(tab).click();
        assert.ok(await page.locator('dialog').evaluate(node=>node.scrollWidth<=node.clientWidth+1),`${width} ${colorScheme} ${tab} overflow`);
        assert.ok(await page.locator('dialog').evaluate(node=>node.getBoundingClientRect().left>=0&&node.getBoundingClientRect().right<=innerWidth));layouts++;
      }
      if(width===375&&colorScheme==='dark') await page.screenshot({path:path.join(path.dirname(fixture),`${nextOrigin?'next-':''}classroom-375-dark.png`),fullPage:true});
    }
    // Stable form and safe POST: a lost response reuses the original key even
    // after closing/reopening the popup. SSE must not replace the typed fields.
    const requestKind=()=>page.getByLabel('요청 기능',{exact:true});
    const requestText=()=>page.getByLabel('학생 안내',{exact:true});
    await requestText().fill('새 자료 <img src=x onerror=alert(2)>를 확인하세요.');
    const textarea=await requestText().elementHandle();revision++;publish();await page.waitForTimeout(200);
    assert.equal(await textarea.evaluate(node=>node===document.querySelector('textarea[aria-label="학생 안내"]')),true,'SSE preserves form DOM and focus');
    assert.match(await requestText().inputValue(),/새 자료/);
    loseNextCommandResponse=true;await page.getByRole('button',{name:'학생에게 요청',exact:true}).click();
    await wait(async()=>await page.getByRole('button',{name:'같은 요청 다시 확인',exact:true}).isVisible());
    assert.equal(commandRecords.size,1);assert.equal(commandPosts,1);
    await page.locator(closeSelector).click();await first.click();await tabButton('control').click();
    await wait(async()=>await page.getByRole('button',{name:'같은 요청 다시 확인',exact:true}).isEnabled());
    await page.getByRole('button',{name:'같은 요청 다시 확인',exact:true}).click();
    await wait(async()=>commandPosts===2);assert.equal(commandRecords.size,1,'uncertain retry cannot duplicate command');
    await wait(async()=>await page.getByRole('button',{name:'학생에게 요청',exact:true}).isVisible());
    assert.equal(await page.locator('img').count(),0,'notice remains plain text');
    const command=()=>[...commandRecords.values()].at(-1);
    const transition=async(status,extra={})=>{Object.assign(command(),{status,...extra});revision++;publish();await wait(async()=>(await page.locator(`[data-command-id="${command().command_id}"]`).textContent()).includes(({received:'학생 확인 대기',accepted:'학생 수락',completed:'처리 완료',declined:'학생 거절',failed:'처리 실패',expired:'만료 · 처리 결과 미확인'})[status]));};
    await transition('received',{received_at:date});
    detailDelay=750;const beforeCommandDetail=detailRequests;
    Object.assign(command(),{status:'accepted',accepted_at:date});revision++;publish();
    await wait(async()=>detailRequests>beforeCommandDetail);
    await wait(async()=>await page.getByText('마지막 확인한 요청 이력입니다.',{exact:false}).isVisible());
    await wait(async()=>(await page.locator(`[data-command-id="${command().command_id}"]`).textContent()).includes('학생 수락'));
    detailDelay=0;await transition('completed',{finished_at:date,result_code:'notice_acknowledged'});
    for(const kind of kinds.slice(1)) {
      await requestKind().selectOption(kind);await requestText().fill(`가상 ${kind} 요청`);await page.getByRole('button',{name:'학생에게 요청',exact:true}).click();
      await wait(async()=>await page.getByRole('button',{name:'학생에게 요청',exact:true}).isEnabled());
      await transition('received',{received_at:date});await transition('accepted',{accepted_at:date});
      await transition('completed',{finished_at:date,result_code:{open_assignment:'assignment_opened',sync_materials:'materials_synced',check_environment:'environment_checked',save_submit:'submission_received'}[kind],result:kind==='check_environment'?{checks:{server:'ok',workspace:'ok',compiler:'not_checked',os:'macos',remote:'none'}}:kind==='save_submit'?{submission_id:'sub-official'}:null});
    }
    assert.equal(commandRecords.size,5);
    assert.match(await page.locator('dialog').textContent(),/컴파일러: 점검하지 않음/);
    assert.match(await page.locator('dialog').textContent(),/sub-official/);
    await requestKind().selectOption('notice');await requestText().fill('학생 거절 시험');await page.getByRole('button',{name:'학생에게 요청',exact:true}).click();await wait(async()=>commandRecords.size===6);await transition('declined',{finished_at:date,result_code:'student_declined'});
    await wait(async()=>await page.getByRole('button',{name:'학생에게 요청',exact:true}).isEnabled());
    await requestKind().selectOption('save_submit');await requestText().fill('만료 시험');await page.getByRole('button',{name:'학생에게 요청',exact:true}).click();await wait(async()=>commandRecords.size===7);await transition('accepted',{accepted_at:date,received_at:date});await transition('expired',{finished_at:date,result_code:'expired'});
    assert.match(await page.locator('dialog').textContent(),/취소되었다는 뜻은 아닙니다/);
    await page.locator('dialog').evaluate(node=>node.scrollTop=0);
    await page.locator('dialog').screenshot({path:path.join(path.dirname(fixture),`${nextOrigin?'next-':''}classroom-requests-dark.png`)});
    await page.locator(closeSelector).click();
    await filterSelect().selectOption('submitted');assert.equal(await page.locator(cardSelector).count(),1);
    await filterSelect().selectOption('unsubmitted');assert.equal(await page.locator(cardSelector).count(),18,'non-target students excluded from unsubmitted');
    await assignmentSelect().selectOption('');
    await wait(async()=>await page.locator(cardSelector).count()===20);
    assert.match(await first.textContent(),/과제 선택 필요/);
    await first.click();await tabButton('control').click();await requestKind().selectOption('save_submit');
    assert.equal(await page.getByRole('button',{name:'학생에게 요청',exact:true}).isEnabled(),false,'assignment required for non-notice requests');
    await page.locator(closeSelector).click();
    // Lose the browser stream, not the student's IDE connection.
    unavailable=true;for(const client of [...streams])client.response.end();
    await wait(async()=>nextOrigin?await page.getByText('브라우저의 수신이 중단되었거나 재연결 중입니다.',{exact:false}).isVisible():await page.locator('[data-cr-stale]').isVisible());
    assert.match(await first.textContent(),/IDE 연결/);
    unavailable=false;await refreshButton().click();
    await wait(async()=>(await page.locator(streamSelector).textContent()).includes('실시간 수신'));
    await first.click();removedStudent=true;revision++;publish();
    await wait(async()=>await page.locator(cardSelector).count()===19);
    assert.equal(await page.locator('dialog').isVisible(),false,'removed roster member closes only their popup');
    await page.locator('[data-student="TEST-002"]').click();denied=true;publish('access_revoked');
    await wait(async()=>await page.locator(cardSelector).count()===0);
    assert.equal(await page.locator('dialog').isVisible(),false);
    assert.equal(await (nextOrigin?page.getByRole('link',{name:'교수자 인증',exact:true}):page.locator('[data-cr-auth]')).isVisible(),true);
    assert.deepEqual(errors,[]);
    console.log(`${layouts} responsive/theme layouts + HTTP/SSE, five IDE requests, safe retry, confirmation/results, score, focus, reconnect and revocation passed`);
  } finally { for(const c of streams)c.response.end();await browser.close();await new Promise(resolve=>server.close(resolve)); }
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
