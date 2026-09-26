/**
 * Server-side configuration. The `server-only` import makes `next build` fail
 * if this module is ever pulled into a client bundle; scripts/check-client-secrets.mjs
 * additionally scans the built client assets for the values.
 *
 * Nothing here carries the public-env prefix — the browser never talks to the
 * API directly.
 */
import "server-only";

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is not set (see admin/.env.example)`);
  }
  return value;
}

export const env = {
  /** Base URL of the FastAPI service, e.g. http://localhost:8000 */
  get API_URL(): string {
    return required("API_URL").replace(/\/+$/, "");
  },
  /** Bearer token for /admin/* (api/.env ADMIN_TOKEN). */
  get ADMIN_TOKEN(): string {
    return required("ADMIN_TOKEN");
  },
  /** Password for the panel's login gate; also the HMAC key of the session cookie. */
  get ADMIN_PASSWORD(): string {
    return required("ADMIN_PASSWORD");
  },
};
