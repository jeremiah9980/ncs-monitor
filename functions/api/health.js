// GET /api/health -- wiring check only, returns no player data.
// Still behind functions/_middleware.js (fail-closed access gate).
export async function onRequestGet({ env }) {
  return Response.json(
    { ok: true, hasDB: !!env.DB },
    { headers: { "cache-control": "no-store" } }
  );
}
