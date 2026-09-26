"use client";

/**
 * Recent generation jobs. Polls the server action every 2 s for as long as
 * any job is queued or running, then stops; a new row (router.refresh from
 * the form) restarts it.
 */
import { useEffect, useState } from "react";
import Link from "next/link";
import type { GenerationJob } from "@/lib/api/types";
import { STATUS_CLASS, cents, isActive, percent, topRejections, when } from "@/lib/format";
import { fetchJobs } from "./actions";

const POLL_MS = 2000;

export function JobList({ initialJobs, compact = false }: { initialJobs: GenerationJob[]; compact?: boolean }) {
  const [seed, setSeed] = useState(initialJobs);
  const [jobs, setJobs] = useState(initialJobs);
  if (seed !== initialJobs) {
    // The server re-rendered (refresh): its list is newer than ours.
    setSeed(initialJobs);
    setJobs(initialJobs);
  }
  const [pollError, setPollError] = useState<string | null>(null);
  const polling = jobs.some(isActive);

  useEffect(() => {
    if (!polling) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const next = await fetchJobs(initialJobs.length > 50 ? initialJobs.length : 50);
        if (!cancelled) {
          setJobs(next);
          setPollError(null);
        }
      } catch (err) {
        if (!cancelled) setPollError(err instanceof Error ? err.message : String(err));
      }
    };
    const id = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [polling, initialJobs.length]);

  if (jobs.length === 0) {
    return <p className="text-gray-500">No jobs yet.</p>;
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-3 text-xs text-gray-400">
        {polling ? <span>polling every {POLL_MS / 1000} s while jobs run</span> : <span>no job running</span>}
        {pollError && <span className="text-red-700">poll failed: {pollError}</span>}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase text-gray-500">
            <tr>
              <th className="py-1 pr-3">created</th>
              <th className="py-1 pr-3">category</th>
              <th className="py-1 pr-3">status</th>
              <th className="py-1 pr-3 text-right">accepted / rejected</th>
              <th className="py-1 pr-3 text-right">cost</th>
              <th className="py-1 pr-3 text-right">repeat</th>
              {!compact && <th className="py-1 pr-3">top rejections</th>}
              {!compact && <th className="py-1 pr-3">prompt</th>}
              <th className="py-1"></th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => (
              <JobRow key={job.id} job={job} compact={compact} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function JobRow({ job, compact }: { job: GenerationJob; compact: boolean }) {
  const requested = job.requested_count ?? job.params.count;
  const done = job.accepted_count + job.rejected_count;
  const progress = requested > 0 ? Math.min(1, done / requested) : 0;
  const rejections = topRejections(job);
  return (
    <tr className="border-t border-gray-200 align-top">
      <td className="py-1.5 pr-3 whitespace-nowrap text-gray-600" title={job.id}>
        {when(job.created_at)}
      </td>
      <td className="py-1.5 pr-3">
        {job.params.category_slug}
        <span className="text-gray-400"> · {requested}</span>
      </td>
      <td className="py-1.5 pr-3">
        <span className={`rounded px-1.5 py-0.5 text-xs ${STATUS_CLASS[job.status]}`}>{job.status}</span>
        {isActive(job) && (
          <div className="mt-1 h-1 w-24 rounded bg-gray-200">
            <div className="h-1 rounded bg-blue-500" style={{ width: `${progress * 100}%` }} />
          </div>
        )}
        {job.error && (
          <div className="mt-1 max-w-xs text-xs text-red-700" title={job.error}>
            {job.error.length > 80 ? job.error.slice(0, 80) + "…" : job.error}
          </div>
        )}
      </td>
      <td className="py-1.5 pr-3 text-right whitespace-nowrap">
        <span className="text-green-800">{job.accepted_count}</span> /{" "}
        <span className="text-red-800">{job.rejected_count}</span>
      </td>
      <td className="py-1.5 pr-3 text-right whitespace-nowrap">{cents(job.cost_cents)}</td>
      <td className="py-1.5 pr-3 text-right whitespace-nowrap">
        {job.stats.emitted > 0 ? percent(job.stats.repeat_rate) : "—"}
      </td>
      {!compact && (
        <td className="py-1.5 pr-3 text-xs text-gray-600">
          {rejections.length ? rejections.join(", ") : "—"}
        </td>
      )}
      {!compact && (
        <td className="max-w-xs truncate py-1.5 pr-3 text-gray-600" title={job.prompt ?? ""}>
          {job.prompt ?? "—"}
        </td>
      )}
      <td className="py-1.5 whitespace-nowrap">
        <Link href={`/review?job=${job.id}`} className="text-blue-700 underline">
          review
        </Link>
      </td>
    </tr>
  );
}
