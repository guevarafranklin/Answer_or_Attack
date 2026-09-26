/**
 * Typed client for the FastAPI content API. Server-side only: it carries the
 * admin bearer token, so it may be called from Server Components, Server
 * Actions and Route Handlers — never from client components (the
 * `server-only` import turns that into a build error).
 *
 * Every call first checks the panel session cookie, so an action reached by a
 * direct POST without logging in is refused before it touches the API.
 *
 * Non-2xx responses throw ApiError with the parsed FastAPI `detail`.
 */
import "server-only";
import { redirect } from "next/navigation";
import { env } from "@/lib/env";
import { hasValidSession } from "@/lib/session";
import { detailToMessages } from "./errors";
import type {
  BulkAction,
  Category,
  CategoryCreate,
  CategoryUpdate,
  ErrorDetail,
  GenerationJob,
  GenerationRequest,
  HealthPage,
  HealthSummary,
  HealthView,
  ParseResponse,
  Question,
  QuestionBulkResult,
  QuestionListQuery,
  QuestionPage,
  QuestionUpdate,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: ErrorDetail | undefined;
  readonly messages: string[];

  constructor(status: number, detail: ErrorDetail | undefined, path: string) {
    const messages = detailToMessages(detail, status);
    super(`${status} ${path}: ${messages.join("; ")}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.messages = messages;
  }
}

type Query = Record<string, string | number | boolean | undefined | null>;

interface RequestOptions {
  query?: Query;
  body?: unknown;
}

async function request<T>(
  method: "GET" | "POST" | "PATCH",
  path: string,
  { query, body }: RequestOptions = {},
): Promise<T> {
  // The proxy normally catches a missing/expired session first; this covers
  // a Server Action POSTed directly and a session that expired mid-page.
  if (!(await hasValidSession())) redirect("/login");

  const url = new URL(env.API_URL + path);
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }

  const res = await fetch(url, {
    method,
    headers: {
      Authorization: `Bearer ${env.ADMIN_TOKEN}`,
      Accept: "application/json",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    // Admin data must never be served stale; fetch is uncached by default in
    // Next 16 but be explicit.
    cache: "no-store",
  });

  if (!res.ok) {
    let detail: ErrorDetail | undefined;
    try {
      detail = (await res.json())?.detail;
    } catch {
      detail = undefined;
    }
    throw new ApiError(res.status, detail, path);
  }
  return (await res.json()) as T;
}

// ---------- categories ----------

export const listCategories = () => request<Category[]>("GET", "/admin/categories");

export const createCategory = (data: CategoryCreate) =>
  request<Category>("POST", "/admin/categories", { body: data });

export const updateCategory = (id: string, patch: CategoryUpdate) =>
  request<Category>("PATCH", `/admin/categories/${id}`, { body: patch });

// ---------- review queue ----------

export const listQuestions = (query: QuestionListQuery) =>
  request<QuestionPage>("GET", "/admin/questions", { query: { ...query } });

export const getQuestion = (id: string) => request<Question>("GET", `/admin/questions/${id}`);

export const updateQuestion = (id: string, patch: QuestionUpdate) =>
  request<Question>("PATCH", `/admin/questions/${id}`, { body: patch });

export const approveQuestion = (id: string) =>
  request<Question>("POST", `/admin/questions/${id}/approve`);

export const rejectQuestion = (id: string) =>
  request<Question>("POST", `/admin/questions/${id}/reject`);

export const archiveQuestion = (id: string) =>
  request<Question>("POST", `/admin/questions/${id}/archive`);

export const bulkQuestions = (ids: string[], action: BulkAction) =>
  request<QuestionBulkResult>("POST", "/admin/questions/bulk", { body: { ids, action } });

// ---------- generation jobs ----------

export const listJobs = (limit = 50) =>
  request<GenerationJob[]>("GET", "/admin/generate", { query: { limit } });

export const getJob = (id: string) => request<GenerationJob>("GET", `/admin/generate/${id}`);

/** Free text → §5.1 params for the admin to confirm. Creates nothing. */
export const parsePrompt = (prompt: string) =>
  request<ParseResponse>("POST", "/admin/generate/parse", { body: { prompt } });

/** 202 {job_id}: the job is queued; poll getJob/listJobs for progress. */
export const createJob = (data: GenerationRequest) =>
  request<{ job_id: string }>("POST", "/admin/generate", { body: data });

// ---------- health ----------

export const healthSummary = () => request<HealthSummary>("GET", "/admin/health/summary");

export const healthView = (view: HealthView, page = 1, page_size = 50) =>
  request<HealthPage>("GET", `/admin/health/${view}`, { query: { page, page_size } });
