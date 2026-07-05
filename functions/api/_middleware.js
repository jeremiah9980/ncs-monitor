// Fail-closed access gate for the gc-stats dashboard.
//
// gc_stats contains MINORS' statistics. This middleware runs on every request
// to the Pages project (Functions AND static assets) and refuses to serve
// anything unless an access mechanism is configured. It NEVER defaults open.
//
// Order of precedence:
//   1. Cloudflare Access -> the platform injects a verified
//      `Cf-Access-Authenticated-User-Email` header; allow.
//   2. DASHBOARD_PASSWORD secret set -> require HTTP Basic Auth whose password
//      equals it (constant-time compare); otherwise 401.
//   3. Neither configured -> 503, locked.

function constantTimeEqual(a, b) {
  const enc = new TextEncoder();
  const ab = enc.encode(a);
  const bb = enc.encode(b);
  // Compare a fixed-length digest of both so length differences don't short
  // circuit and leak timing. Unequal lengths always fail.
  let result = ab.length === bb.length ? 0 : 1;
  const len = Math.max(ab.length, bb.length, 1);
  for (let i = 0; i < len; i++) {
    result |= (ab[i % ab.length] || 0) ^ (bb[i % bb.length] || 0);
  }
  return result === 0 && ab.length === bb.length;
}

export async function onRequest(context) {
  const { request, env, next } = context;

  // 1. Behind Cloudflare Access: the header is set by the platform after it
  // verifies the JWT, and cannot be spoofed by the client when Access is on.
  if (request.headers.get("Cf-Access-Authenticated-User-Email")) {
    return next();
  }

  // 2. Password gate.
  const password = env.DASHBOARD_PASSWORD;
  if (password) {
    const auth = request.headers.get("Authorization") || "";
    const [scheme, encoded] = auth.split(" ");
    if (scheme === "Basic" && encoded) {
      let decoded = "";
      try {
        decoded = atob(encoded);
      } catch {
        decoded = "";
      }
      const idx = decoded.indexOf(":");
      const provided = idx >= 0 ? decoded.slice(idx + 1) : "";
      if (constantTimeEqual(provided, password)) {
        return next();
      }
    }
    return new Response("Authentication required.\n", {
      status: 401,
      headers: {
        "WWW-Authenticate": 'Basic realm="gc-stats"',
        "content-type": "text/plain; charset=utf-8",
        "cache-control": "no-store",
      },
    });
  }

  // 3. Fail closed: nothing is configured.
  return new Response(
    "gc-stats dashboard is locked.\n\n" +
      "This project serves minors' statistics and refuses to run without an " +
      "access gate. Before exposing it, either:\n" +
      "  - enable Cloudflare Access on this Pages project, or\n" +
      "  - set a password: wrangler pages secret put DASHBOARD_PASSWORD\n",
    {
      status: 503,
      headers: {
        "content-type": "text/plain; charset=utf-8",
        "cache-control": "no-store",
      },
    }
  );
}
