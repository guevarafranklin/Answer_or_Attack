import Link from "next/link";
import { healthView, listCategories } from "@/lib/api/client";
import { HEALTH_VIEWS, LOCALES, type HealthRow, type HealthView } from "@/lib/api/types";
import { optionLabel } from "@/components/QuestionPanes";
import { percent, when } from "@/lib/format";
import { ArchiveButton } from "./ArchiveButton";

const PAGE_SIZE = 50;

const TABS: Record<HealthView, { label: string; ratio: string; blurb: string }> = {
  easy: {
    label: "Easy",
    ratio: "correct",
    blurb: "More than 90% of players get it right — usually the question is too easy for its difficulty rating, or a distractor gives it away.",
  },
  suspect: {
    label: "Suspect",
    ratio: "correct",
    blurb: "Under 25% correct, or more than 3 player reports — usually a wrong answer key. Check the correct option before anything else.",
  },
  dead: {
    label: "Dead",
    ratio: "timeouts",
    blurb: "More than 60% of serves time out — usually the question is too long to read and answer in 10 seconds.",
  },
};

function first(v: string | string[] | undefined): string | undefined {
  return Array.isArray(v) ? v[0] : v;
}

export default async function HealthPage(props: PageProps<"/health">) {
  const sp = await props.searchParams;
  const rawView = first(sp.view);
  const view: HealthView = HEALTH_VIEWS.includes(rawView as HealthView) ? (rawView as HealthView) : "easy";
  const page = Math.max(1, Number(first(sp.page)) || 1);

  const [data, categories] = await Promise.all([healthView(view, page, PAGE_SIZE), listCategories()]);
  const slugOf = new Map(categories.map((c) => [c.id, c.slug]));
  const pages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
  const tab = TABS[view];

  return (
    <div className="flex flex-col gap-3">
      <h1 className="text-lg font-semibold">Health</h1>

      <nav className="flex gap-1 border-b border-gray-200">
        {HEALTH_VIEWS.map((v) => (
          <Link
            key={v}
            href={`/health?view=${v}`}
            className={`-mb-px rounded-t border px-3 py-1 ${
              v === view ? "border-gray-200 border-b-white bg-white font-semibold" : "border-transparent text-gray-600 hover:bg-gray-100"
            }`}
          >
            {TABS[v].label}
          </Link>
        ))}
      </nav>

      <p className="text-gray-700">{tab.blurb}</p>

      {data.items.length === 0 ? (
        <p className="rounded border border-gray-200 bg-white p-6 text-gray-500">
          Nothing here — no question with at least 50 serves meets this view&apos;s threshold.
        </p>
      ) : (
        <div className="overflow-x-auto rounded border border-gray-200 bg-white">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase text-gray-500">
              <tr>
                <th className="px-3 py-1 text-right">{tab.ratio}</th>
                <th className="px-3 py-1 text-right">serves</th>
                <th className="px-3 py-1 text-right">reports</th>
                <th className="px-3 py-1">question</th>
                <th className="px-3 py-1"></th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((row) => (
                <Row key={row.question_id} row={row} category={slugOf.get(row.question.category_id)} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {pages > 1 && (
        <div className="flex items-center gap-3 text-sm">
          {page > 1 && (
            <Link href={`/health?view=${view}&page=${page - 1}`} className="underline">
              ← newer
            </Link>
          )}
          <span className="text-gray-500">
            page {page} of {pages} · {data.total} questions
          </span>
          {page < pages && (
            <Link href={`/health?view=${view}&page=${page + 1}`} className="underline">
              older →
            </Link>
          )}
        </div>
      )}
    </div>
  );
}

function Row({ row, category }: { row: HealthRow; category: string | undefined }) {
  const q = row.question;
  const byLocale = Object.fromEntries(q.translations.map((t) => [t.locale, t]));
  const archived = q.status === "archived";
  return (
    <tr className="border-t border-gray-100 align-top">
      <td className="px-3 py-2 text-right font-semibold tabular-nums">{percent(row.ratio)}</td>
      <td className="px-3 py-2 text-right tabular-nums">{row.serves}</td>
      <td className={`px-3 py-2 text-right tabular-nums ${row.reports > 3 ? "font-semibold text-red-800" : ""}`}>
        {row.reports}
      </td>
      <td className="px-3 py-2">
        <div className="mb-1 flex flex-wrap gap-x-3 text-xs text-gray-500">
          <span>{category ?? q.category_id.slice(0, 8)}</span>
          <span>difficulty {q.difficulty}</span>
          <span className={archived ? "text-yellow-800" : ""}>{q.status}</span>
          <span>correct {row.correct} · wrong {row.incorrect} · timeouts {row.timeouts}</span>
          {row.avg_response_ms !== null && <span>avg {(row.avg_response_ms / 1000).toFixed(1)} s</span>}
          <span>last served {when(row.last_served_at)}</span>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          {LOCALES.map((locale) => {
            const t = byLocale[locale];
            return (
              <div key={locale}>
                <span className="mr-1 text-xs font-semibold uppercase text-gray-400">{locale}</span>
                {t ? (
                  <>
                    <span>{t.stem}</span>
                    <div className="text-xs text-gray-600">
                      {t.options.map((o, i) => (
                        <span key={i} className={`mr-2 ${i === q.correct_index ? "font-semibold text-green-800" : ""}`}>
                          {optionLabel(i)} {o}
                        </span>
                      ))}
                    </div>
                  </>
                ) : (
                  <span className="text-red-700">missing</span>
                )}
              </div>
            );
          })}
        </div>
      </td>
      <td className="px-3 py-2 whitespace-nowrap">
        <div className="flex flex-col items-start gap-1">
          <Link href={`/questions/${q.id}`} className="rounded border border-gray-300 px-2 py-0.5 text-xs">
            Edit
          </Link>
          <ArchiveButton id={q.id} disabled={archived} />
        </div>
      </td>
    </tr>
  );
}
