"use server";

import * as api from "@/lib/api/client";
import type { ActionResult } from "@/lib/api/errors";
import type { Category, CategoryCreate, CategoryUpdate } from "@/lib/api/types";

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

export async function createCategory(data: CategoryCreate): Promise<ActionResult<Category>> {
  return wrap(() => api.createCategory(data));
}

export async function updateCategory(id: string, patch: CategoryUpdate): Promise<ActionResult<Category>> {
  return wrap(() => api.updateCategory(id, patch));
}
