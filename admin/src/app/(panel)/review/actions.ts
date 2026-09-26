"use server";

/**
 * Review-queue mutations. Each returns an ActionResult instead of throwing, so
 * the client can show the API's 422/409 detail inline next to the question.
 * Anything that is not an API refusal (network down, session expired) still
 * throws and lands in the route's error boundary.
 */
import * as api from "@/lib/api/client";
import type { ActionResult } from "@/lib/api/errors";
import type { BulkAction, Question, QuestionBulkResult, QuestionUpdate } from "@/lib/api/types";

async function wrap<T>(call: () => Promise<T>): Promise<ActionResult<T>> {
  try {
    return { ok: true, data: await call() };
  } catch (err) {
    if (err instanceof api.ApiError) {
      return { ok: false, status: err.status, messages: err.messages };
    }
    throw err;
  }
}

export async function approveQuestion(id: string): Promise<ActionResult<Question>> {
  return wrap(() => api.approveQuestion(id));
}

export async function rejectQuestion(id: string): Promise<ActionResult<Question>> {
  return wrap(() => api.rejectQuestion(id));
}

export async function updateQuestion(
  id: string,
  patch: QuestionUpdate,
): Promise<ActionResult<Question>> {
  return wrap(() => api.updateQuestion(id, patch));
}

export async function bulkQuestions(
  ids: string[],
  action: BulkAction,
): Promise<ActionResult<QuestionBulkResult>> {
  return wrap(() => api.bulkQuestions(ids, action));
}
