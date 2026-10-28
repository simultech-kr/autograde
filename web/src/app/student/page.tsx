import type { Metadata } from "next";
import { StudentPortal } from "../../features/student/StudentPortal";

export const metadata: Metadata = { title: "학생 포털 | Autograde", description: "회원가입·수강 선택·과제와 강의자료·공식 제출 결과" };
export default function Page() { return <StudentPortal />; }
