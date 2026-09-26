"use client";

/**
 * Standalone editor for one question: the same panes and draft/PATCH logic as
 * the review queue, always in edit mode. Keys: 1–4 set the correct option,
 * Ctrl/⌘+Enter saves.
 */
import { useEffect, useRef, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import {
  DIFFICULTIES,
  GRADE_BANDS,
  LOCALES,
  REGIONS,
  type GradeBand,
  type Question,
  type Region,
} from "@/lib/api/types";
import { TranslationEditor } from "@/components/QuestionPanes";
import { toDraft, toPatch } from "@/lib/questionDraft";
import { archiveQuestion, updateQuestion } from "./actions";

interface Feedback {
  kind: "error" | "info";
  lines: string[];
}

export function QuestionEditor({ initial }: { initial: Question }) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [question, setQuestion] = useState(initial);
  const [draft, setDraft] = useState(() => toDraft(initial));
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [confirmArchive, setConfirmArchive] = useState(false);

  function save() {
    setFeedback(null);
    startTransition(async () => {
      const result = await updateQuestion(question.id, toPatch(draft));
      if (result.ok) {
        setQuestion(result.data);
        setDraft(toDraft(result.data));
        setFeedback({ kind: "info", lines: ["Saved."] });
        router.refresh();
      } else {
        setFeedback({ kind: "error", lines: [`Not saved (${result.status})`, ...result.messages] });
      }
    });
  }

  function archive() {
    if (!confirmArchive) {
      setConfirmArchive(true);
      return;
    }
    setConfirmArchive(false);
    setFeedback(null);
    startTransition(async () => {
      const result = await archiveQuestion(question.id);
      if (result.ok) {
        setQuestion(result.data);
        setFeedback({ kind: "info", lines: ["Archived. It will no longer be served."] });
        router.refresh();
      } else {
        setFeedback({ kind: "error", lines: [`Not archived (${result.status})`, ...result.messages] });
      }
    });
  }

  // Latest-closure ref: updated after every render, read by the one listener.
  const keyHandler = useRef<(e: KeyboardEvent) => void>(() => {});
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      save();
      return;
    }
    const target = e.target as HTMLElement | null;
    const inField =
      !!target &&
      (target.tagName === "INPUT" ||
        target.tagName === "TEXTAREA" ||
        target.tagName === "SELECT" ||
        target.isContentEditable);
    if (!inField && !e.metaKey && !e.ctrlKey && !e.altKey && /^[1-4]$/.test(e.key)) {
      e.preventDefault();
      setDraft((d) => ({ ...d, correct_index: Number(e.key) - 1 }));
    }
  };
  useEffect(() => {
    keyHandler.current = onKey;
  });
  useEffect(() => {
    const listener = (e: KeyboardEvent) => keyHandler.current(e);
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, []);

  const btn = "rounded border px-3 py-1 disabled:opacity-50";
  const select = "rounded border border-gray-300 px-1";
  const archived = question.status === "archived";

  return (
    <div className="flex flex-col gap-3">
      {feedback && (
        <div
          className={`flex items-start justify-between gap-3 rounded border p-3 ${
            feedback.kind === "error" ? "border-red-300 bg-red-50 text-red-800" : "border-green-300 bg-green-50 text-green-800"
          }`}
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

      <div className="rounded border border-gray-200 bg-white p-4">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <button type="button" className={`${btn} border-gray-900`} disabled={isPending} onClick={save}>
            Save <kbd className="text-xs">⌘↵</kbd>
          </button>
          <button
            type="button"
            className={`${btn} ${confirmArchive ? "border-red-600 bg-red-600 text-white" : "border-red-600 text-red-800"}`}
            disabled={isPending || archived}
            onClick={archive}
            onBlur={() => setConfirmArchive(false)}
          >
            {archived ? "Archived" : confirmArchive ? "Click again to archive" : "Archive"}
          </button>
          <span className="text-gray-500">Press 1–4 to set the correct option.</span>
          {isPending && <span className="ml-auto text-gray-400">working…</span>}
        </div>

        <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-gray-600">
          <label>
            difficulty{" "}
            <select
              className={select}
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
              className={select}
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
              className={select}
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
          <span className="ml-auto text-xs text-gray-400" title={question.id}>
            {question.source} · job {question.generation_job_id?.slice(0, 8) ?? "—"} · updated{" "}
            {question.updated_at.slice(0, 10)}
          </span>
        </div>

        <div className="grid grid-cols-2 gap-4">
          {LOCALES.map((locale) => (
            <TranslationEditor
              key={locale}
              locale={locale}
              value={draft.translations[locale]}
              correctIndex={draft.correct_index}
              autoFocus={false}
              onChange={(t) => setDraft({ ...draft, translations: { ...draft.translations, [locale]: t } })}
              onPickCorrect={(i) => setDraft({ ...draft, correct_index: i })}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
