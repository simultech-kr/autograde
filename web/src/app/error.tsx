"use client";

import { AppShell } from "../components/InstructorUI";

export default function ErrorPage({ reset }: { reset: () => void }) {
  return <AppShell><section><h1>화면을 표시하지 못했습니다</h1>
    <p>학생 제출과 채점은 기존 서버에서 계속 처리됩니다.</p>
    <button onClick={reset}>화면 다시 열기</button> <a href="/instructor">교수자 포털로 이동</a></section></AppShell>;
}
