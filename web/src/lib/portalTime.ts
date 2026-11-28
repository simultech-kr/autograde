// Course deadlines and receipts use the institution's KST clock on both portals.
const options: Intl.DateTimeFormatOptions = {
  timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23",
};
const minute = new Intl.DateTimeFormat("ko-KR", options);
const second = new Intl.DateTimeFormat("ko-KR", {...options, second: "2-digit"});

export function portalTime(value: string | null | undefined, precision: "minute" | "second" = "minute") {
  const timestamp = value ? Date.parse(value) : NaN;
  if (!Number.isFinite(timestamp)) return "—";
  return (precision === "second" ? second : minute).format(timestamp);
}
