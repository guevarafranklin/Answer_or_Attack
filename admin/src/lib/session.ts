/**
 * Password gate session: an httpOnly cookie holding `<expiry>.<hmac>`, signed
 * with ADMIN_PASSWORD (WebCrypto, so the same code runs in proxy.ts and in
 * Server Actions). Changing the password logs every session out — intended.
 *
 * There is no user identity here (one shared password, one shared API token);
 * Phase 4 replaces this with real auth.
 */
import "server-only";
import { cookies } from "next/headers";
import { env } from "@/lib/env";

export const SESSION_COOKIE = "aoa_admin_session";
const SESSION_TTL_SECONDS = 12 * 60 * 60;

const encoder = new TextEncoder();

async function hmacKey(secret: string): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
}

function toHex(buf: ArrayBuffer): string {
  return Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, "0")).join("");
}

function fromHex(hex: string): Uint8Array<ArrayBuffer> | null {
  if (hex.length === 0 || hex.length % 2 !== 0 || /[^0-9a-f]/i.test(hex)) return null;
  const out = new Uint8Array(new ArrayBuffer(hex.length / 2));
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

function payload(expiresAt: number): Uint8Array<ArrayBuffer> {
  return encoder.encode(`aoa-admin-session:${expiresAt}`);
}

export async function createSessionToken(secret: string, now = Date.now()): Promise<string> {
  const expiresAt = now + SESSION_TTL_SECONDS * 1000;
  const sig = await crypto.subtle.sign("HMAC", await hmacKey(secret), payload(expiresAt));
  return `${expiresAt}.${toHex(sig)}`;
}

/** True when `token` carries a valid, unexpired signature under `secret`. */
export async function verifySessionToken(
  token: string | undefined,
  secret: string,
  now = Date.now(),
): Promise<boolean> {
  if (!token) return false;
  const dot = token.indexOf(".");
  if (dot < 0) return false;
  const expiresAt = Number(token.slice(0, dot));
  const sig = fromHex(token.slice(dot + 1));
  if (!Number.isFinite(expiresAt) || expiresAt <= now || sig === null) return false;
  // subtle.verify is the constant-time comparison.
  return crypto.subtle.verify("HMAC", await hmacKey(secret), sig, payload(expiresAt));
}

/** Constant-time password check (both sides hashed so lengths never leak). */
export async function passwordMatches(candidate: string, expected: string): Promise<boolean> {
  const key = await hmacKey("aoa-admin-password");
  const expectedMac = await crypto.subtle.sign("HMAC", key, encoder.encode(expected));
  return crypto.subtle.verify("HMAC", key, expectedMac, encoder.encode(candidate));
}

/** Server Action / Route Handler only (cookies can't be set from a Server Component). */
export async function setSessionCookie(): Promise<void> {
  const store = await cookies();
  store.set({
    name: SESSION_COOKIE,
    value: await createSessionToken(env.ADMIN_PASSWORD),
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: SESSION_TTL_SECONDS,
  });
}

export async function clearSessionCookie(): Promise<void> {
  const store = await cookies();
  store.delete(SESSION_COOKIE);
}

/** Whether the current request carries a valid session (readable anywhere on the server). */
export async function hasValidSession(): Promise<boolean> {
  const store = await cookies();
  return verifySessionToken(store.get(SESSION_COOKIE)?.value, env.ADMIN_PASSWORD);
}
