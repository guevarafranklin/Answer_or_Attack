"use server";

import * as api from "@/lib/api/client";
import type { ActionResult } from "@/lib/api/errors";
import type { Question } from "@/lib/api/types";

export async function archiveQuestion(id: string): Promise<ActionResult<Question>> {
  try {
    return { ok: true, data: await api.archiveQuestion(id) };
  } catch (err) {
    if (err instanceof api.ApiError) {
      return { ok: false, status: err.status, messages: err.messages };
    }
    throw err;
  }
}
