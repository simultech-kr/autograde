import {submissionState, type Course, type LearningHome, type Assignment, type Receipt} from "./model.ts";

export type LearningTask = {course:Course; assignment:Assignment; receipt?:Receipt; receiptOnly:boolean; category:"work"|"results"|"pending"; label:string; action:string; destination:"course"|"scores"; priority:number};
export function learningTasks(home: LearningHome, courses: Course[], now: number): LearningTask[] {
  const tasks: LearningTask[] = [];
  for (const entry of home.courses) {
    const course = courses.find(course => course.enrolled && course.course_key === entry.course_key);
    if (!course) continue;
    const knownAssignments = new Set(entry.overview.assignments.map(item=>item.assignment_id));
    const assignments = [...entry.overview.assignments, ...entry.overview.latest.filter(item=>!knownAssignments.has(item.assignment_id)).map(row=>({assignment_id:row.assignment_id, assignment_key:row.assignment_key, title:row.title, opens_at:null, due_at:null, accepted_at:null}))];
    for (const assignment of assignments) {
      const receipt = entry.overview.latest.find(row => row.assignment_id === assignment.assignment_id), state = submissionState(receipt);
      const opens = assignment.opens_at ? Date.parse(assignment.opens_at) : -Infinity;
      const due = assignment.due_at ? Date.parse(assignment.due_at) : Infinity;
      if (!receipt && opens > now) continue;
      const item = {course, assignment, receipt, receiptOnly:!knownAssignments.has(assignment.assignment_id)};
      if (!receipt && entry.overview.latest_truncated) tasks.push({...item,category:"results",label:"제출 기록 추가 확인 필요",action:"제출 기록 확인",destination:"scores",priority:4});
      else if (state.tone === "error") tasks.push({...item,category:"results",label:state.label,action:"처리 상태 확인",destination:"scores",priority:0});
      else if (state.scored) {
        const partial = receipt!.score! < receipt!.max_score!;
        const label = receipt!.feedback_policy === "score_only" ? "총점만 공개" : partial ? "수정할 부분 확인" : "평가 완료";
        tasks.push({...item,category:"results",label,action:"평가 결과 보기",destination:"scores",priority:partial ? 3 : 7});
      }
      else if (receipt) tasks.push({...item,category:"pending",label:state.label,action:"채점 상태 보기",destination:"scores",priority:6});
      else if (due <= now) tasks.push({...item,category:"work",label:"미제출 · 마감됨",action:"과제 상태 확인",destination:"course",priority:5});
      else tasks.push({...item,category:"work",label:due - now <= 86400000 ? "24시간 이내 마감" : assignment.accepted_at ? "작성 중 · 공식 제출 전" : "아직 제출하지 않음",action:assignment.accepted_at ? "실습 이어가기" : "실습 시작하기",destination:"course",priority:due - now <= 86400000 ? 1 : 2});
    }
  }
  return tasks.sort((a,b) => a.priority - b.priority || (a.receipt && b.receipt && a.category === "results" && b.category === "results" ? Date.parse(b.receipt.received_at) - Date.parse(a.receipt.received_at) : (a.assignment.due_at ? Date.parse(a.assignment.due_at) : Infinity) - (b.assignment.due_at ? Date.parse(b.assignment.due_at) : Infinity)) || a.assignment.assignment_id.localeCompare(b.assignment.assignment_id));
}
