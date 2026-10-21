/**
 * Pages Function — /api/snapshot
 *
 *   GET → the latest encrypted portfolio snapshot (from KV)
 *   PUT → replace it; requires `Authorization: Bearer <UPLOAD_TOKEN>`
 *
 * The body is AES-GCM ciphertext produced by snapshot.py, so this function
 * never sees plaintext portfolio data. Bindings (see wrangler.toml):
 *   SNAPSHOTS     KV namespace
 *   UPLOAD_TOKEN  secret shared with the local Flask app (CF_UPLOAD_TOKEN)
 */

const KEY = 'latest';
const MAX_BYTES = 2_000_000;
const REQUIRED = ['v', 'iterations', 'salt', 'iv', 'ciphertext', 'takenAt'];

const HEADERS = { 'content-type': 'application/json', 'cache-control': 'no-store' };
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: HEADERS });

/** Constant-time compare — hash first so both sides are the same length. */
async function tokenMatches(given, expected) {
  const enc = new TextEncoder();
  const [a, b] = await Promise.all(
    [given, expected].map(s => crypto.subtle.digest('SHA-256', enc.encode(s))));
  return crypto.subtle.timingSafeEqual(a, b);
}

export async function onRequestGet({ env }) {
  const value = await env.SNAPSHOTS.get(KEY);
  if (!value) return json({ error: 'no_snapshot' }, 404);
  return new Response(value, { headers: HEADERS });
}

export async function onRequestPut({ request, env }) {
  const auth = request.headers.get('authorization') ?? '';
  if (!env.UPLOAD_TOKEN || !(await tokenMatches(auth, `Bearer ${env.UPLOAD_TOKEN}`))) {
    return json({ error: 'unauthorized' }, 401);
  }

  const body = await request.text();
  if (body.length > MAX_BYTES) return json({ error: 'too_large' }, 413);

  let envelope;
  try { envelope = JSON.parse(body); } catch { return json({ error: 'bad_json' }, 400); }
  if (REQUIRED.some(k => !(k in envelope))) return json({ error: 'bad_envelope' }, 400);

  await env.SNAPSHOTS.put(KEY, body);
  return json({ ok: true, takenAt: envelope.takenAt });
}
