/**
 * Turn a FastAPI error body into messages the review UI can show inline.
 * Safe to import from client components: no env, no fetch.
 */
import type { ErrorDetail, ValidationErrorItem } from "./types";

/** What a Server Action hands back when the API refused the call. */
export interface ApiFailure {
  ok: false;
  status: number;
  /** Human-readable lines, e.g. "es · option 3: options must be distinct". */
  messages: string[];
}

export interface ApiSuccess<T> {
  ok: true;
  data: T;
}

export type ActionResult<T> = ApiSuccess<T> | ApiFailure;

/** Render Pydantic's `loc` as a short field path: ["body","translations","es","options",2] → "es · option 3". */
function describeLoc(loc: (string | number)[]): string {
  const parts = loc.filter((p) => p !== "body" && p !== "translations");
  const out: string[] = [];
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    if (p === "options" && typeof parts[i + 1] === "number") {
      out.push(`option ${(parts[i + 1] as number) + 1}`);
      i++;
    } else {
      out.push(String(p));
    }
  }
  return out.join(" · ");
}

export function detailToMessages(detail: ErrorDetail | undefined, status: number): string[] {
  if (detail === undefined || detail === null) return [`API error ${status}`];
  if (typeof detail === "string") return [detail];
  if (Array.isArray(detail)) {
    return detail.map((item: ValidationErrorItem) => {
      const where = describeLoc(item.loc ?? []);
      // Pydantic prefixes custom-validator messages with "Value error, ".
      const msg = (item.msg ?? "").replace(/^Value error, /, "");
      return where ? `${where}: ${msg}` : msg;
    });
  }
  return [JSON.stringify(detail)];
}
