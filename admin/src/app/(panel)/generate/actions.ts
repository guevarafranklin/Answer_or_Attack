"use server";

/**
 * Generate screen: parse (read-only, hits the model), create (queues a job),
 * and the poll the job list uses while a job is queued or running. API
 * refusals come back as ActionResult so the form can show them inline.
 */
import * as api from "@/lib/api/client";
import type { ActionResult } from "@/lib/api/errors";
import type { GenerationJob, GenerationParams, ParseResponse } from "@/lib/api/types";

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

export async function parsePrompt(prompt: string): Promise<ActionResult<ParseResponse>> {
  return wrap(() => api.parsePrompt(prompt));
}

export async function createJob(
  prompt: string,
  params: GenerationParams,
): Promise<ActionResult<{ job_id: string }>> {
  return wrap(() => api.createJob({ prompt, params, kind: "category" }));
}

export async function fetchJobs(limit = 50): Promise<GenerationJob[]> {
  return api.listJobs(limit);
}
