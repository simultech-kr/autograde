import type { Metadata } from "next";
import "../styles/globals.css";

export const metadata: Metadata = {
  title: "교수자 포털 | Autograde",
  description: "Autograde 수업 운영·제출·채점·실습 교실",
  robots: { index: false, follow: false },
};
export const dynamic = "force-dynamic";

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="ko"><body><a className="skip-link" href="#main">본문으로 이동</a>{children}</body></html>;
}
