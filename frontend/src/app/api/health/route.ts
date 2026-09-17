export function GET(): Response {
  return Response.json(
    { status: "ok", service: "frontend" },
    {
      headers: { "cache-control": "no-store" },
    },
  );
}
