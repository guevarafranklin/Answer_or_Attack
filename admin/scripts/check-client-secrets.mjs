#!/usr/bin/env node
/**
 * Fails the build if the API token (or anything else that must stay on the
 * server) can reach the browser.
 *
 *   node scripts/check-client-secrets.mjs src      (prebuild)  scan source
 *   node scripts/check-client-secrets.mjs bundle   (postbuild) scan .next output
 *
 * src:    no `NEXT_PUBLIC_` anywhere under src/ — this panel exposes nothing to
 *         the client; API_URL and ADMIN_TOKEN in particular must never be.
 * bundle: the client assets (.next/static) and prerendered HTML/RSC payloads
 *         (.next/server/app) contain neither the names nor the *values* of
 *         ADMIN_TOKEN, ADMIN_PASSWORD, API_URL, read from .env.local/.env like
 *         `next build` does.
 *
 * `import "server-only"` in src/lib/env.ts and src/lib/api/client.ts is the
 * structural guard (a client import is a compile error); this is the belt to
 * that brace.
 */
import { readdirSync, readFileSync, statSync, existsSync } from "node:fs";
import { join, relative } from "node:path";

const root = new URL("..", import.meta.url).pathname;
const mode = process.argv[2];
const SECRET_NAMES = ["ADMIN_TOKEN", "ADMIN_PASSWORD", "API_URL"];

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry);
    if (statSync(p).isDirectory()) yield* walk(p);
    else yield p;
  }
}

function loadEnv() {
  const values = { ...process.env };
  // Same precedence as Next: .env.local overrides .env.
  for (const file of [".env", ".env.local"]) {
    const p = join(root, file);
    if (!existsSync(p)) continue;
    for (const line of readFileSync(p, "utf8").split("\n")) {
      const m = line.match(/^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
      if (m) values[m[1]] = m[2].replace(/^(['"])(.*)\1$/, "$2");
    }
  }
  return values;
}

const failures = [];

function scanFile(path, needles) {
  const text = readFileSync(path, "utf8");
  for (const [label, needle] of needles) {
    if (needle && text.includes(needle)) {
      failures.push(`${relative(root, path)} contains ${label}`);
    }
  }
}

if (mode === "src") {
  for (const file of walk(join(root, "src"))) {
    scanFile(file, [["NEXT_PUBLIC_ (nothing in this panel may be public)", "NEXT_PUBLIC_"]]);
  }
} else if (mode === "bundle") {
  const env = loadEnv();
  const needles = [["NEXT_PUBLIC_", "NEXT_PUBLIC_"]];
  for (const name of SECRET_NAMES) {
    needles.push([`the identifier ${name}`, name]);
    const value = env[name];
    if (!value) {
      console.warn(`check-client-secrets: ${name} not set, scanning for its name only`);
    } else if (value.length < 8) {
      console.warn(`check-client-secrets: ${name} is shorter than 8 chars, scanning for its name only`);
    } else {
      needles.push([`the value of ${name}`, value]);
    }
  }
  // Client assets: everything. Server output: only what is shipped to the
  // browser (prerendered HTML and RSC payloads), not the server JS, which
  // legitimately reads these variables.
  const targets = [
    [join(root, ".next", "static"), /\.(js|mjs|css|html|txt|json|map)$/],
    [join(root, ".next", "server", "app"), /\.(html|rsc|txt)$/],
  ];
  let scanned = 0;
  for (const [dir, pattern] of targets) {
    if (!existsSync(dir)) continue;
    for (const file of walk(dir)) {
      if (!pattern.test(file)) continue;
      scanFile(file, needles);
      scanned++;
    }
  }
  if (scanned === 0) failures.push("no build output found under .next — run after `next build`");
} else {
  console.error("usage: check-client-secrets.mjs <src|bundle>");
  process.exit(2);
}

if (failures.length) {
  console.error("check-client-secrets FAILED:\n  " + failures.join("\n  "));
  process.exit(1);
}
console.log(`check-client-secrets: ${mode} ok`);
