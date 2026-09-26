import Link from "next/link";
import { healthSummary, listJobs } from "@/lib/api/client";
import { LOCALES, type QuestionStatus, type StatusCounts } from "@/lib/api/types";
import { cents } from "@/lib/format";
import { JobList } from "./generate/JobList";

const STATUSES: readonly QuestionStatus[] = ["pending", "live", "archived", "rejected"];
const RECENT_JOBS = 10;
// Enough rows to cover a month of jobs at the current pace; the cost figure
// says so if the window was cut short.
const COST_WINDOW = 200;

function startOfMonthUtc(now = new Date()): Date {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1));
}

export default async function DashboardPage() {
  const [summary, jobs] = await Promise.all([healthSummary(), listJobs(COST_WINDOW)]);

  const monthStart = startOfMonthUtc();
  const thisMonth = jobs.filter((j) => new Date(j.created_at) >= monthStart);
  const monthCost = thisMonth.reduce((sum, j) => sum + (j.cost_cents ?? 0), 0);
  // listJobs is newest-first; if the oldest row is still inside the month
  // there may be older rows we did not fetch.
  const costTruncated =
    jobs.length === COST_WINDOW && new Date(jobs[jobs.length - 1].created_at) >= monthStart;

  const monthLabel = monthStart.toLocaleString("en-US", { month: "long", year: "numeric", timeZone: "UTC" });

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-lg font-semibold">Dashboard</h1>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="pending backlog" value={summary.pending_backlog} href="/review" />
        <Stat label="live questions" value={summary.by_status.live ?? 0} />
        <Stat
          label={`generation cost · ${monthLabel}`}
          value={cents(monthCost)}
          hint={`${thisMonth.length} job${thisMonth.length === 1 ? "" : "s"}${costTruncated ? ` · only the newest ${COST_WINDOW} jobs counted` : ""}`}
          href="/generate"
        />
        <div className="rounded border border-gray-200 bg-white p-3">
          <div className="text-xs uppercase text-gray-500">health</div>
          <div className="mt-1 flex gap-3">
            <Link href="/health?view=easy" className="underline">
              easy {summary.health.easy}
            </Link>
            <Link href="/health?view=suspect" className="underline text-red-800">
              suspect {summary.health.suspect}
            </Link>
            <Link href="/health?view=dead" className="underline text-yellow-800">
              dead {summary.health.dead}
            </Link>
          </div>
        </div>
      </section>

      <section className="grid gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <h2 className="mb-2 font-semibold">By category</h2>
          <CountsTable
            rows={summary.by_category.map((c) => ({
              key: c.slug,
              label: (
                <Link href={`/review?category=${encodeURIComponent(c.slug)}`} className="underline">
                  {c.slug}
                </Link>
              ),
              counts: c.counts,
            }))}
            empty="No categories yet."
          />
        </div>
        <div>
          <h2 className="mb-2 font-semibold">By locale</h2>
          <CountsTable
            rows={LOCALES.map((l) => ({ key: l, label: l, counts: summary.by_locale[l] ?? {} }))}
            empty="No translations yet."
          />
          <p className="mt-1 text-xs text-gray-400">
            A question counts under a locale when it has a translation in it.
          </p>
        </div>
      </section>

      <section>
        <div className="mb-2 flex items-baseline gap-3">
          <h2 className="font-semibold">Recent jobs</h2>
          <Link href="/generate" className="text-sm underline">
            all jobs
          </Link>
        </div>
        <JobList initialJobs={jobs.slice(0, RECENT_JOBS)} compact />
      </section>
    </div>
  );
}

function Stat({
  label,
  value,
  hint,
  href,
}: {
  label: string;
  value: number | string;
  hint?: string;
  href?: string;
}) {
  const body = (
    <>
      <div className="text-xs uppercase text-gray-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold">{value}</div>
      {hint && <div className="text-xs text-gray-400">{hint}</div>}
    </>
  );
  const cls = "rounded border border-gray-200 bg-white p-3";
  return href ? (
    <Link href={href} className={`${cls} hover:bg-gray-50`}>
      {body}
    </Link>
  ) : (
    <div className={cls}>{body}</div>
  );
}

function CountsTable({
  rows,
  empty,
}: {
  rows: { key: string; label: React.ReactNode; counts: Partial<StatusCounts> }[];
  empty: string;
}) {
  if (rows.length === 0) return <p className="text-gray-500">{empty}</p>;
  return (
    <div className="overflow-x-auto rounded border border-gray-200 bg-white">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase text-gray-500">
          <tr>
            <th className="px-3 py-1"></th>
            {STATUSES.map((s) => (
              <th key={s} className="px-3 py-1 text-right">
                {s}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className="border-t border-gray-100">
              <td className="px-3 py-1">{r.label}</td>
              {STATUSES.map((s) => {
                const n = r.counts[s] ?? 0;
                const strong = (s === "pending" || s === "live") && n > 0;
                return (
                  <td key={s} className={`px-3 py-1 text-right tabular-nums ${strong ? "" : "text-gray-400"}`}>
                    {n}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
