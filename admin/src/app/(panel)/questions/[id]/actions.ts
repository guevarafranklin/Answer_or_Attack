"use server";

import * as api from "@/lib/api/client";
import type { ActionResult } from "@/lib/api/errors";
import type { Question, QuestionUpdate } from "@/lib/api/types";

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

export async function updateQuestion(id: string, patch: QuestionUpdate): Promise<ActionResult<Question>> {
  return wrap(() => api.updateQuestion(id, patch));
}

export async function archiveQuestion(id: string): Promise<ActionResult<Question>> {
  return wrap(() => api.archiveQuestion(id));
}
