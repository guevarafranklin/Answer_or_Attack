# Admin panel

Next.js 16 (App Router, Tailwind) front end for the FastAPI content API. Every
API call is made on the server — Server Components, Server Actions — with the
admin bearer token; the browser only ever talks to Next.

## Run

```sh
# 1. API up (from api/): docker compose up -d, alembic upgrade head, uvicorn app.main:app
# 2. Configure
cp .env.example .env.local        # set ADMIN_TOKEN to the value in api/.env, pick ADMIN_PASSWORD
# 3. Go
npm install
npm run dev                       # http://localhost:3000 → /login
```

`npm run build` runs `scripts/check-client-secrets.mjs` before (source: no
`NEXT_PUBLIC_` anywhere) and after (client bundles and prerendered payloads:
neither the names nor the values of `ADMIN_TOKEN`, `ADMIN_PASSWORD`,
`API_URL`). `src/lib/env.ts` and `src/lib/api/client.ts` also `import
"server-only"`, so importing them from a client component is a compile error.

## Layout

| Path | What |
| --- | --- |
| `src/proxy.ts` | Password gate: every route except `/login` needs a valid session cookie |
| `src/lib/session.ts` | HMAC-signed httpOnly cookie (`aoa_admin_session`, 12 h); key = `ADMIN_PASSWORD` |
| `src/lib/env.ts` | `API_URL`, `ADMIN_TOKEN`, `ADMIN_PASSWORD` — server only |
| `src/lib/api/types.ts` | TS mirror of `api/app/schemas/*.py` — update together |
| `src/lib/api/client.ts` | Typed fetch wrapper; throws `ApiError{status, messages}` on non-2xx; re-checks the session before every call |
| `src/lib/api/errors.ts` | FastAPI `detail` (string or Pydantic list) → readable lines |
| `src/app/(panel)/review/` | Review queue: `page.tsx` (server, filters → API), `actions.ts` (Server Actions returning `ActionResult`), `ReviewQueue.tsx` (client) |
| other `(panel)/*` | Placeholders for Dashboard, Generate, Health, Categories |

## Review queue keys

| Key | Action |
| --- | --- |
| `A` / `R` | approve / reject current (advances automatically) |
| `E` | edit inline; `1`–`4` set the correct option, `⌘/Ctrl+Enter` save, `Esc` cancel |
| `J` / `K` (or arrows) | next / previous |
| `B` | bulk-select mode; `X` or `Space` toggles the current question; with a selection, `A`/`R` apply to it |

API refusals (422 validation, 409 duplicate stem / missing locale) show inline
above the question; bulk failures are listed per id and stay in the queue.
