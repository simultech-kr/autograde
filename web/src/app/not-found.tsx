import { AppShell } from "../components/InstructorUI";

export default function NotFound() {
  return <AppShell><section><h1>화면을 찾을 수 없습니다</h1>
    <a href="/app">제출·채점 현황</a> · <a href="/instructor">교수자 포털로 이동</a></section></AppShell>;
}
