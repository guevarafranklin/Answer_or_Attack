"use client";

/**
 * The review screen: one pending question at a time, EN and ES side by side,
 * keyboard-driven. Approve/reject/edit go through the Server Actions in
 * ./actions.ts; API refusals (422/409) are shown inline.
 *
 * Keys (when focus is not in a form field):
 *   A approve · R reject · E edit · J/K or arrows next/previous
 *   B bulk-select mode · X or Space toggle selection (bulk mode)
 *   In bulk mode with a selection, A/R apply to the selection.
 * While editing: 1–4 set the correct option · Ctrl/⌘+Enter save · Esc cancel.
 */
import { useEffect, useRef, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import {
  DIFFICULTIES,
  GRADE_BANDS,
  LOCALES,
  REGIONS,
  type BulkAction,
  type Category,
  type GradeBand,
  type Question,
  type Region,
} from "@/lib/api/types";
import { TranslationEditor, TranslationView } from "@/components/QuestionPanes";
import { byLocale, toDraft, toPatch, type Draft } from "@/lib/questionDraft";
import { approveQuestion, bulkQuestions, rejectQuestion, updateQuestion } from "./actions";

// ---------- component ----------

interface Props {
  initialItems: Question[];
  total: number;
  page: number;
  pageSize: number;
  categories: Category[];
}

interface Feedback {
  kind: "error" | "info";
  lines: string[];
}

export function ReviewQueue({ initialItems, total, page, pageSize, categories }: Props) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();

  // Local working copy of the page. When a refresh delivers new props
  // (same key), adopt them and drop the local removals.
  const [seed, setSeed] = useState(initialItems);
  const [items, setItems] = useState(initialItems);
  const [removed, setRemoved] = useState(0);
  if (seed !== initialItems) {
    setSeed(initialItems);
    setItems(initialItems);
    setRemoved(0);
  }

  const [rawIndex, setIndex] = useState(0);
  const index = Math.min(rawIndex, Math.max(0, items.length - 1));
  const current: Question | undefined = items[index];

  const [draft, setDraft] = useState<Draft | null>(null);
  const editing = draft !== null;

  const [bulk, setBulk] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [feedback, setFeedback] = useState<Feedback | null>(null);

  const categoryName = (id: string) => categories.find((c) => c.id === id)?.slug ?? id.slice(0, 8);

  // ---- list bookkeeping ----

  function removeIds(ids: Iterable<string>) {
    const gone = new Set(ids);
    setItems((prev) => prev.filter((q) => !gone.has(q.id)));
    setRemoved((n) => n + gone.size);
    setSelected((prev) => new Set([...prev].filter((id) => !gone.has(id))));
  }

  // Page exhausted by local removals: pull the next batch of pending
  // questions. `removed` resets to 0 when the new props arrive, so an empty
  // server result does not loop.
  useEffect(() => {
    if (items.length === 0 && removed > 0) router.refresh();
  }, [items.length, removed, router]);

  function replaceItem(q: Question) {
    setItems((prev) => prev.map((x) => (x.id === q.id ? q : x)));
  }

  function move(delta: number) {
    if (items.length === 0) return;
    setIndex((i) => Math.min(items.length - 1, Math.max(0, Math.min(i, items.length - 1) + delta)));
  }

  function toggleSelected(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  // ---- actions ----

  function decide(action: "approve" | "reject") {
    if (isPending) return;
    if (bulk && selected.size > 0) {
      runBulk(action);
      return;
    }
    if (!current) return;
    const id = current.id;
    startTransition(async () => {
      const res = await (action === "approve" ? approveQuestion(id) : rejectQuestion(id));
      if (res.ok) {
        setFeedback(null);
        setDraft(null);
        removeIds([id]);
      } else {
        setFeedback({ kind: "error", lines: [`${action} failed (${res.status})`, ...res.messages] });
      }
    });
  }

  function runBulk(action: BulkAction) {
    if (isPending) return;
    const ids = [...selected];
    if (ids.length === 0) {
      setFeedback({ kind: "info", lines: ["Nothing selected. X or Space selects the current question."] });
      return;
    }
    startTransition(async () => {
      const res = await bulkQuestions(ids, action);
      if (!res.ok) {
        setFeedback({ kind: "error", lines: [`bulk ${action} failed (${res.status})`, ...res.messages] });
        return;
      }
      removeIds(res.data.updated);
      if (res.data.failed.length > 0) {
        setFeedback({
          kind: "error",
          lines: [
            `${action}: ${res.data.updated.length} updated, ${res.data.failed.length} failed (kept in the list):`,
            ...res.data.failed.map((f) => `${f.id.slice(0, 8)}: ${f.detail}`),
          ],
        });
      } else {
        setFeedback({ kind: "info", lines: [`${action}: ${res.data.updated.length} updated`] });
      }
    });
  }

  function startEdit() {
    if (current) setDraft(toDraft(current));
  }

  function cancelEdit() {
    setDraft(null);
  }

  function saveEdit() {
    if (!current || !draft || isPending) return;
    const id = current.id;
    const patch = toPatch(draft);
    startTransition(async () => {
      const res = await updateQuestion(id, patch);
      if (res.ok) {
        replaceItem(res.data);
        setDraft(null);
        setFeedback({ kind: "info", lines: ["Saved."] });
      } else {
        setFeedback({ kind: "error", lines: [`Not saved (${res.status})`, ...res.messages] });
      }
    });
  }

  // ---- keyboard ----

  // The handler closes over the latest state; the listener is attached once.
  const keyHandler = useRef<(e: KeyboardEvent) => void>(() => {});
  keyHandler.current = (e) => {
    const target = e.target as HTMLElement | null;
    const inField =
      !!target &&
      (target.tagName === "INPUT" ||
        target.tagName === "TEXTAREA" ||
        target.tagName === "SELECT" ||
        target.isContentEditable);

    if (editing) {
      if (e.key === "Escape") {
        e.preventDefault();
        cancelEdit();
      } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        saveEdit();
      } else if (!inField && !e.metaKey && !e.ctrlKey && !e.altKey && /^[1-4]$/.test(e.key)) {
        e.preventDefault();
        setDraft((d) => (d ? { ...d, correct_index: Number(e.key) - 1 } : d));
      }
      return;
    }

    if (inField || e.metaKey || e.ctrlKey || e.altKey) return;
    switch (e.key) {
      case "a":
      case "A":
        e.preventDefault();
        decide("approve");
        break;
      case "r":
      case "R":
        e.preventDefault();
        decide("reject");
        break;
      case "e":
      case "E":
        e.preventDefault();
        startEdit();
        break;
      case "j":
      case "J":
      case "ArrowDown":
      case "ArrowRight":
        e.preventDefault();
        move(1);
        break;
      case "k":
      case "K":
      case "ArrowUp":
      case "ArrowLeft":
        e.preventDefault();
        move(-1);
        break;
      case "b":
      case "B":
        e.preventDefault();
        setBulk((v) => !v);
        break;
      case "x":
      case "X":
      case " ":
        if (bulk && current) {
          e.preventDefault();
          toggleSelected(current.id);
        }
        break;
    }
  };
  useEffect(() => {
    const listener = (e: KeyboardEvent) => keyHandler.current(e);
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, []);

  // Keep the current row visible in the sidebar.
  const currentRow = useRef<HTMLLIElement | null>(null);
  useEffect(() => {
    currentRow.current?.scrollIntoView({ block: "nearest" });
  }, [index, items.length]);

  // ---- render ----

  const remaining = Math.max(0, total - removed);
  const btn = "rounded border border-gray-300 bg-white px-2 py-1 hover:bg-gray-100 disabled:opacity-50";

  return (
    <div className="flex gap-4">
      {/* sidebar */}
      <aside className="w-72 shrink-0">
        <div className="mb-2 flex items-center justify-between text-gray-600">
          <span>
            {remaining} pending · page {page}
            {remaining > pageSize && ` of ${Math.ceil(remaining / pageSize)}`}
          </span>
          <button type="button" className="underline" onClick={() => router.refresh()}>
            Refresh
          </button>
        </div>
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={bulk} onChange={(e) => setBulk(e.target.checked)} />
            Bulk select <kbd className="rounded border px-1 text-xs">B</kbd>
          </label>
          {bulk && (
            <>
              <button
                type="button"
                className="underline"
                onClick={() => setSelected(new Set(items.map((q) => q.id)))}
              >
                all shown
              </button>
              <button type="button" className="underline" onClick={() => setSelected(new Set())}>
                none
              </button>
            </>
          )}
        </div>
        {bulk && (
          <div className="mb-2 flex gap-2">
            <button
              type="button"
              className={`${btn} border-green-600 text-green-800`}
              disabled={isPending || selected.size === 0}
              onClick={() => runBulk("approve")}
            >
              Approve {selected.size}
            </button>
            <button
              type="button"
              className={`${btn} border-red-600 text-red-800`}
              disabled={isPending || selected.size === 0}
              onClick={() => runBulk("reject")}
            >
              Reject {selected.size}
            </button>
          </div>
        )}
        <ul className="max-h-[70vh] divide-y divide-gray-200 overflow-y-auto rounded border border-gray-200 bg-white">
          {items.map((q, i) => {
            const en = byLocale(q).en;
            const isCurrent = i === index;
            return (
              <li
                key={q.id}
                ref={isCurrent ? currentRow : null}
                className={`flex cursor-pointer items-start gap-2 px-2 py-1.5 ${
                  isCurrent ? "bg-yellow-50" : "hover:bg-gray-50"
                }`}
                onClick={() => setIndex(i)}
              >
                {bulk && (
                  <input
                    type="checkbox"
                    className="mt-1"
                    checked={selected.has(q.id)}
                    onChange={() => toggleSelected(q.id)}
                    onClick={(e) => e.stopPropagation()}
                  />
                )}
                <span className="w-5 shrink-0 text-right text-gray-400">{i + 1}</span>
                <span className="line-clamp-2 flex-1">{en?.stem ?? <em>(no EN)</em>}</span>
                <span className="shrink-0 rounded bg-gray-100 px-1 text-xs">D{q.difficulty}</span>
              </li>
            );
          })}
          {items.length === 0 && <li className="p-3 text-gray-500">Nothing pending here.</li>}
        </ul>
      </aside>

      {/* main pane */}
      <section className="min-w-0 flex-1">
        {feedback && (
          <div
            className={`mb-3 flex items-start justify-between gap-3 rounded border px-3 py-2 ${
              feedback.kind === "error"
                ? "border-red-300 bg-red-50 text-red-800"
                : "border-blue-200 bg-blue-50 text-blue-800"
            }`}
            role={feedback.kind === "error" ? "alert" : "status"}
          >
            <ul>
              {feedback.lines.map((line, i) => (
                <li key={i} className={i === 0 ? "font-medium" : ""}>
                  {line}
                </li>
              ))}
            </ul>
            <button type="button" onClick={() => setFeedback(null)} aria-label="Dismiss">
              ×
            </button>
          </div>
        )}

        {!current ? (
          <div className="rounded border border-gray-200 bg-white p-6 text-gray-500">
            Queue is empty for these filters.
          </div>
        ) : (
          <div className="rounded border border-gray-200 bg-white p-4">
            {/* toolbar */}
            <div className="mb-3 flex flex-wrap items-center gap-2">
              {editing ? (
                <>
                  <button type="button" className={`${btn} border-gray-900`} disabled={isPending} onClick={saveEdit}>
                    Save <kbd className="text-xs">⌘↵</kbd>
                  </button>
                  <button type="button" className={btn} onClick={cancelEdit}>
                    Cancel <kbd className="text-xs">Esc</kbd>
                  </button>
                  <span className="text-gray-500">Press 1–4 to set the correct option.</span>
                </>
              ) : (
                <>
                  <button
                    type="button"
                    className={`${btn} border-green-600 text-green-800`}
                    disabled={isPending}
                    onClick={() => decide("approve")}
                  >
                    Approve <kbd className="text-xs">A</kbd>
                  </button>
                  <button
                    type="button"
                    className={`${btn} border-red-600 text-red-800`}
                    disabled={isPending}
                    onClick={() => decide("reject")}
                  >
                    Reject <kbd className="text-xs">R</kbd>
                  </button>
                  <button type="button" className={btn} onClick={startEdit}>
                    Edit <kbd className="text-xs">E</kbd>
                  </button>
                  <span className="ml-2 text-gray-500">
                    <kbd className="text-xs">J</kbd>/<kbd className="text-xs">K</kbd> next/prev
                  </span>
                  {bulk && (
                    <span className="text-gray-500">
                      · <kbd className="text-xs">X</kbd> select ({selected.size} selected
                      {selected.size > 0 && "; A/R apply to selection"})
                    </span>
                  )}
                </>
              )}
              {isPending && <span className="ml-auto text-gray-400">working…</span>}
            </div>

            {/* metadata */}
            <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-gray-600">
              <span>
                <b className="text-gray-900">{categoryName(current.category_id)}</b>
              </span>
              {editing && draft ? (
                <>
                  <label>
                    difficulty{" "}
                    <select
                      className="rounded border border-gray-300 px-1"
                      value={draft.difficulty}
                      onChange={(e) => setDraft({ ...draft, difficulty: Number(e.target.value) })}
                    >
                      {DIFFICULTIES.map((d) => (
                        <option key={d} value={d}>
                          {d}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    grade{" "}
                    <select
                      className="rounded border border-gray-300 px-1"
                      value={draft.grade_band}
                      onChange={(e) => setDraft({ ...draft, grade_band: e.target.value as GradeBand | "" })}
                    >
                      <option value="">—</option>
                      {GRADE_BANDS.map((g) => (
                        <option key={g} value={g}>
                          {g}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    region{" "}
                    <select
                      className="rounded border border-gray-300 px-1"
                      value={draft.region}
                      onChange={(e) => setDraft({ ...draft, region: e.target.value as Region })}
                    >
                      {REGIONS.map((r) => (
                        <option key={r} value={r}>
                          {r}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="flex-1">
                    tags{" "}
                    <input
                      className="w-full max-w-md rounded border border-gray-300 px-1"
                      value={draft.tags}
                      onChange={(e) => setDraft({ ...draft, tags: e.target.value })}
                      placeholder="comma, separated"
                    />
                  </label>
                </>
              ) : (
                <>
                  <span>difficulty {current.difficulty}</span>
                  {current.grade_band && <span>{current.grade_band}</span>}
                  <span>{current.region}</span>
                  <span className="flex flex-wrap gap-1">
                    {current.tags.map((t) => (
                      <span key={t} className="rounded bg-gray-100 px-1.5">
                        {t}
                      </span>
                    ))}
                  </span>
                </>
              )}
              <span className="ml-auto text-xs text-gray-400" title={current.id}>
                {current.source} · job {current.generation_job_id?.slice(0, 8) ?? "—"} ·{" "}
                {current.created_at.slice(0, 10)}
              </span>
            </div>

            {/* EN | ES */}
            <div className="grid grid-cols-2 gap-4">
              {LOCALES.map((locale) =>
                editing && draft ? (
                  <TranslationEditor
                    key={locale}
                    locale={locale}
                    value={draft.translations[locale]}
                    correctIndex={draft.correct_index}
                    autoFocus={locale === "en"}
                    onChange={(t) =>
                      setDraft({ ...draft, translations: { ...draft.translations, [locale]: t } })
                    }
                    onPickCorrect={(i) => setDraft({ ...draft, correct_index: i })}
                  />
                ) : (
                  <TranslationView
                    key={locale}
                    locale={locale}
                    translation={byLocale(current)[locale]}
                    correctIndex={current.correct_index}
                  />
                ),
              )}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

