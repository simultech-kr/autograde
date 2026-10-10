"use client";

export default function ErrorPage({ reset }: { reset: () => void }) {
  return <main id="main" className="fallback"><h1>화면을 표시하지 못했습니다</h1>
    <p>학생 제출과 채점은 기존 서버에서 계속 처리됩니다.</p>
    <button onClick={reset}>화면 다시 열기</button> <a href="/instructor">기존 교수자 화면</a></main>;
}
