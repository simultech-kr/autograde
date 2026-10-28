// Deliberately synthetic. Never import live contracts, clients or account data here.
export type EnrollmentStatus = "matched" | "pending" | "invited";
export type PreviewStudent = { key: string; status: EnrollmentStatus; joined: string };
export const initialStudents: PreviewStudent[] = [
  { key: "20269901", status: "matched", joined: "10.28 09:12" },
  { key: "20269902", status: "matched", joined: "10.28 09:18" },
  { key: "20269903", status: "pending", joined: "10.28 09:24" },
  { key: "20269904", status: "matched", joined: "10.28 09:31" },
  { key: "20269905", status: "invited", joined: "—" },
  { key: "20269906", status: "matched", joined: "10.28 09:42" },
  { key: "20269907", status: "pending", joined: "10.28 09:48" },
  { key: "20269908", status: "invited", joined: "—" },
];
export const statusText: Record<EnrollmentStatus, string> = {
  matched: "수강 연결", pending: "명단 확인 대기", invited: "수강 신청 전",
};
export const sampleRoster = "student_key\n20269903\n20269907\n20269909\n";

// This parser is only for the local preview. The production Python service
// remains the authority for CSV validation, authorization and enrollment.
export function parsePreviewRoster(text: string): string[] {
  const lines = text.replace(/^\uFEFF/, "").trim().split(/\r?\n/);
  if (!["student_key", "학번"].includes(lines[0]?.trim())) {
    throw new Error("시안에서는 첫 줄이 student_key 또는 학번인 한 열 CSV를 사용하세요.");
  }
  const rows = lines.slice(1).map((line, index) => ({ key: line.trim(), line: index + 2 })).filter(row => row.key);
  const keys = rows.map(row => row.key);
  if (!keys.length) throw new Error("학번이 없습니다. 머리글 다음 줄에 학번을 추가하세요.");
  if (keys.length > 10000) throw new Error("한 번에 10,000명까지 확인할 수 있습니다.");
  const invalid = rows.find(row => !/^\d{4,20}$/.test(row.key));
  if (invalid) throw new Error(`${invalid.line}행의 학번을 확인하세요. 숫자 4~20자리가 필요합니다.`);
  return [...new Set(keys)];
}

export function addPreviewRoster(students: PreviewStudent[], keys: string[]): PreviewStudent[] {
  const allowed = new Set(keys);
  return [
    ...students.map(student => student.status === "pending" && allowed.has(student.key)
      ? { ...student, status: "matched" as const } : student),
    ...keys.filter(key => !students.some(student => student.key === key)).map(key => ({
      key, status: "invited" as const, joined: "—",
    })),
  ];
}
