import test from "node:test";
import assert from "node:assert/strict";
import {parseLearningHome, StudentContractError, type Assignment, type Course, type LearningHome, type Receipt} from "../src/features/student/model.ts";
import {learningTasks} from "../src/features/student/learningTasks.ts";
import {allowedStudentPath} from "../src/features/student/client.ts";
import {scoreRowState} from "../src/features/student/scoreboard.ts";

const now = Date.parse("2026-11-15T01:00:00Z"), stamp = (offset:number) => new Date(now+offset).toISOString();
const course:Course = {course_key:"come2201",code:"COME2201",name:"설계",section:"01",status:"matched",request_status:"matched",enrolled:true,eligible:true,can_withdraw:false};
const assignment = (id:string, extra:Partial<Assignment> = {}):Assignment => ({assignment_id:id,assignment_key:id,title:id,opens_at:stamp(-10000),due_at:stamp(86400000*7),accepted_at:null,...extra});
const receipt = (id:string, extra:Partial<Receipt> = {}):Receipt => ({submission_id:`sub_${id}`,assignment_id:id,title:id,assignment_key:id,received_at:stamp(-1000),state:"queued",code_version:"a".repeat(64),score:null,max_score:null,published_at:null,version:2,previous_best_id:"sub_old",previous_best_score:95,previous_best_max_score:100,...extra});
const home = (assignments:Assignment[]=[assignment("first")], latest:Receipt[]=[]):LearningHome => ({student_key:"20260001",server_time:stamp(0),courses_truncated:false,courses:[{course_key:course.course_key,overview:{assignments,latest,assignments_truncated:false,latest_truncated:false,limit:20}}]});
const envelope = (value:LearningHome) => ({ok:true,result:{schema_version:1,...value}});

test("home snapshot rejects other students, unproved courses, duplicate courses and malformed bounds",()=>{
  const value=home();
  assert.equal(parseLearningHome(envelope(value),"20260001",[course.course_key]).courses.length,1);
  assert.throws(()=>parseLearningHome(envelope(value),"20260002",[course.course_key]),StudentContractError);
  assert.throws(()=>parseLearningHome(envelope(value),"20260001",["come3105"]),StudentContractError);
  assert.throws(()=>parseLearningHome(envelope({...value,courses:[...value.courses,...value.courses]}),"20260001",[course.course_key]),StudentContractError);
  assert.throws(()=>parseLearningHome(envelope(home([assignment("same"),assignment("same")])),"20260001",[course.course_key]),StudentContractError);
  assert.throws(()=>parseLearningHome(envelope({...value,server_time:"not-a-date"}),"20260001",[course.course_key]),StudentContractError);
});

test("task queue prioritizes a near deadline and separates pending, expired and published results",()=>{
  const value=home([assignment("soon",{due_at:stamp(3600000),accepted_at:stamp(-1000)}),assignment("pending"),assignment("partial"),assignment("expired",{due_at:stamp(-1000)}),assignment("future",{opens_at:stamp(3600000)}),assignment("done")],
    [receipt("pending",{score:95,max_score:100,published_at:stamp(-1000)}),receipt("partial",{state:"published",score:35,max_score:100,published_at:stamp(-1000)}),receipt("done",{state:"published",score:100,max_score:100,published_at:stamp(-1000)})]);
  const tasks=learningTasks(value,[course],now);
  assert.equal(tasks[0].assignment.assignment_id,"soon"); assert.equal(tasks[0].action,"실습 이어가기");
  assert.equal(tasks.some(item=>item.assignment.assignment_id==="future"),false);
  assert.equal(tasks.find(item=>item.assignment.assignment_id==="pending")?.category,"pending");
  assert.equal(tasks.find(item=>item.assignment.assignment_id==="partial")?.label,"수정할 부분 확인");
  assert.equal(tasks.find(item=>item.assignment.assignment_id==="expired")?.action,"과제 상태 확인");
  assert.equal(tasks.find(item=>item.assignment.assignment_id==="done")?.label,"평가 완료");
});

test("receipts with hidden or invalid grades never become completed tasks",()=>{
  for(const extra of [{state:"graded",score:100,max_score:100,published_at:stamp(-1)},{state:"published",score:101,max_score:100,published_at:stamp(-1)},{state:"published",score:100,max_score:100,published_at:null}]) {
    const task=learningTasks(home([assignment("first")],[receipt("first",extra)]),[course],now)[0];
    assert.equal(task.category,"pending"); assert.notEqual(task.label,"평가 완료");
  }
});

test("withdrawn courses vanish from task feed and server failures remain actionable",()=>{
  assert.deepEqual(learningTasks(home(),[{...course,enrolled:false}],now),[]);
  const task=learningTasks(home([assignment("first")],[receipt("first",{state:"infra_failed"})]),[course],now)[0];
  assert.equal(task.label,"서버 처리 오류");assert.equal(task.destination,"scores");assert.equal(task.priority,0);
});

test("new read endpoints preserve same-origin path boundaries",()=>{
  assert.equal(allowedStudentPath("/student/api/home"),true);
  assert.equal(allowedStudentPath("/student/api/course/come2201/submissions/bsub_a"),true);
  for(const path of ["/student/api/home?student=other","/student/api/course/come2201/submissions/../private","/student/api/course/come2201/submissions/a%2fb","https://evil.test/student/api/home"]) assert.equal(allowedStudentPath(path),false,path);
});

test("truncated latest receipts never turn unknown submissions into missing homework",()=>{
  const value=home([assignment("older")],[receipt("recent",{state:"published",score:35,max_score:100,published_at:stamp(-1)})]);
  value.courses[0].overview.latest_truncated=true;
  const tasks=learningTasks(value,[course],now);
  assert.equal(tasks.find(item=>item.assignment.assignment_id==="older")?.label,"제출 기록 추가 확인 필요");
  const recent=tasks.find(item=>item.assignment.assignment_id==="recent")!;
  assert.equal(recent.label,"수정할 부분 확인");assert.equal(recent.receiptOnly,true);
  assert.equal(tasks.some(item=>item.category==="work"),false);
});

test("expired missing homework does not instruct a student to submit, and unknown stays unknown",()=>{
  const row={assignment_id:"ended",assignment_key:"ended",title:"Ended",due_at:stamp(-1),missingIsUnknown:false};
  assert.equal(scoreRowState(row,now).label,"미제출 · 마감됨");
  assert.match(scoreRowState(row,now).hint,/교수자에게 문의/);
  assert.equal(scoreRowState({...row,missingIsUnknown:true},now).label,"제출 기록 추가 확인 필요");
});
