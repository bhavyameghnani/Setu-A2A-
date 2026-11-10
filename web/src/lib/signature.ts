// Checks a result's signature in the browser. Setu signs each result (EdDSA JWS); this verifies it
// against the public JWKS with WebCrypto, so the badge says "verified" only when the check passed.
// The format is defined by server/app/core/signing.py: the digest is SHA-256 over the parts as
// canonical JSON (sorted keys, no spaces, UTF-8, whole-number floats as integers).

export type Jwks = { keys: { kid?: string; x?: string; kty?: string; crv?: string }[] };
export type Claims = { iss: string; sub: string; task: string; room: string; artifact: string; sha256: string; iat: number; trace?: string };
export type SignatureStatus =
  | { state: "verified"; claims: Claims }
  | { state: "invalid"; reason: string }
  | { state: "unchecked"; reason: string }; // this browser cannot check Ed25519

const b64uToBytes = (s: string): Uint8Array<ArrayBuffer> => {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + pad);
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
};

function canonical(v: unknown): string {
  if (v === null || typeof v !== "object") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
  const o = v as Record<string, unknown>;
  return "{" + Object.keys(o).sort().map((k) => JSON.stringify(k) + ":" + canonical(o[k])).join(",") + "}";
}

export async function partsDigest(parts: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(canonical(parts));
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(hash)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function verifySignature(token: string, parts: unknown, jwks: Jwks): Promise<SignatureStatus> {
  const segs = token.split(".");
  if (segs.length !== 3) return { state: "invalid", reason: "malformed signature" };
  let header: { kid?: string; alg?: string };
  let claims: Claims;
  try {
    header = JSON.parse(new TextDecoder().decode(b64uToBytes(segs[0])));
    claims = JSON.parse(new TextDecoder().decode(b64uToBytes(segs[1])));
  } catch {
    return { state: "invalid", reason: "malformed signature" };
  }
  if (header.alg !== "EdDSA") return { state: "invalid", reason: "unexpected algorithm" };
  const jwk = jwks.keys.find((k) => k.kid === header.kid);
  if (!jwk?.x) return { state: "invalid", reason: "signing key not published" };
  let key: CryptoKey;
  try {
    key = await crypto.subtle.importKey("raw", b64uToBytes(jwk.x), { name: "Ed25519" }, false, ["verify"]);
  } catch {
    return { state: "unchecked", reason: "this browser cannot verify Ed25519" };
  }
  const signed = new TextEncoder().encode(segs[0] + "." + segs[1]);
  const ok = await crypto.subtle.verify({ name: "Ed25519" }, key, b64uToBytes(segs[2]), signed);
  if (!ok) return { state: "invalid", reason: "signature does not match" };
  if (claims.sha256 !== (await partsDigest(parts))) return { state: "invalid", reason: "content changed after signing" };
  return { state: "verified", claims };
}
