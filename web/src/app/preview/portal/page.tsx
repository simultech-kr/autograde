import type { Metadata } from "next";
import { PortalPreview } from "../../../features/portal-preview/PortalPreview";

export const metadata: Metadata = {
  title: "포털 디자인 미리보기 | Autograde",
  description: "학생 회원가입·수강 선택·내 수업·교수자 수강 명단의 인터랙티브 디자인 시안. 예시 데이터만 사용합니다.",
};

export default function Page() {
  return <PortalPreview />;
}
