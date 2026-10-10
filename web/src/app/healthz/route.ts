export function GET() {
  return Response.json({ ok: true, service: "autograde-instructor-web", contract_version: "1" }, {
    headers: { "Cache-Control": "no-store" },
  });
}
