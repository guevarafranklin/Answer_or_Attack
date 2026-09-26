/**
 * Password gate for the whole panel. Every request except /login and Next's
 * own assets needs a valid session cookie, or it is redirected to /login.
 *
 * This is the outer fence; the API client (src/lib/api/client.ts) re-checks
 * the session before every backend call, so a Server Action reached by a
 * direct POST is refused too.
 */
import { NextResponse, type NextRequest } from "next/server";
import { SESSION_COOKIE, verifySessionToken } from "@/lib/session";
import { env } from "@/lib/env";

export async function proxy(request: NextRequest) {
  const token = request.cookies.get(SESSION_COOKIE)?.value;
  if (await verifySessionToken(token, env.ADMIN_PASSWORD)) {
    return NextResponse.next();
  }
  const login = new URL("/login", request.nextUrl);
  const next = request.nextUrl.pathname + request.nextUrl.search;
  if (next !== "/") login.searchParams.set("next", next);
  return NextResponse.redirect(login);
}

export const config = {
  matcher: ["/((?!login|_next/static|_next/image|favicon.ico).*)"],
};
