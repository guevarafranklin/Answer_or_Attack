/** Small display helpers shared by the panel screens. No env, no fetch. */
import type { GenerationJob, GenerationStatus } from "@/lib/api/types";

export function cents(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `$${(value / 100).toFixed(2)}`;
}

export function percent(ratio: number, digits = 0): string {
  return `${(ratio * 100).toFixed(digits)}%`;
}

/** "2026-09-25 14:03" in the viewer's local time. */
export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export const ACTIVE_STATUSES: readonly GenerationStatus[] = ["queued", "running"];

export function isActive(job: GenerationJob): boolean {
  return ACTIVE_STATUSES.includes(job.status);
}

/** The rejection reasons with the highest counts, "reason ×n". */
export function topRejections(job: GenerationJob, n = 3): string[] {
  return Object.entries(job.stats.rejections ?? {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, n)
    .map(([reason, count]) => `${reason} ×${count}`);
}

export const STATUS_CLASS: Record<GenerationStatus, string> = {
  queued: "bg-gray-100 text-gray-700",
  running: "bg-blue-100 text-blue-800",
  succeeded: "bg-green-100 text-green-800",
  partial: "bg-yellow-100 text-yellow-800",
  failed: "bg-red-100 text-red-800",
};
