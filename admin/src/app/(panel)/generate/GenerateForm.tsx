"use client";

/**
 * Chat-style prompt → parsed params (editable) → Confirm creates the job.
 * The prompt is sent along with the confirmed params so the job records what
 * the admin asked for as well as what was run.
 */
import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import {
  DIFFICULTIES,
  GRADE_BANDS,
  LOCALES,
  REGIONS,
  type Category,
  type GenerationParams,
  type GradeBand,
  type Locale,
  type Region,
} from "@/lib/api/types";
import { createJob, parsePrompt } from "./actions";

const EXAMPLE = "40 world history questions, first grade to high school, global audience";

export function GenerateForm({ categories }: { categories: Category[] }) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [prompt, setPrompt] = useState("");
  const [params, setParams] = useState<GenerationParams | null>(null);
  const [notes, setNotes] = useState<string[]>([]);
  const [errors, setErrors] = useState<string[]>([]);
  const [created, setCreated] = useState<string | null>(null);

  function parse() {
    const text = prompt.trim();
    if (!text) return;
    setErrors([]);
    setCreated(null);
    startTransition(async () => {
      const result = await parsePrompt(text);
      if (result.ok) {
        setParams(result.data.params);
        setNotes(result.data.notes);
      } else {
        setParams(null);
        setNotes([]);
        setErrors(result.messages);
      }
    });
  }

  function confirm() {
    if (!params) return;
    setErrors([]);
    startTransition(async () => {
      const result = await createJob(prompt.trim(), {
        ...params,
        style_notes: params.style_notes?.trim() ? params.style_notes.trim() : null,
      });
      if (result.ok) {
        setCreated(result.data.job_id);
        setParams(null);
        setNotes([]);
        setPrompt("");
        router.refresh(); // job list picks up the new row and starts polling
      } else {
        setErrors(result.messages);
      }
    });
  }

  const field = "rounded border border-gray-300 bg-white px-2 py-1";
  const btn = "rounded border px-3 py-1 disabled:opacity-50";

  return (
    <div className="flex flex-col gap-3">
      <form
        className="flex flex-col gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          parse();
        }}
      >
        <label className="text-sm text-gray-600" htmlFor="prompt">
          What should be generated?
        </label>
        <textarea
          id="prompt"
          className={`${field} w-full`}
          rows={3}
          value={prompt}
          placeholder={EXAMPLE}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              parse();
            }
          }}
        />
        <div className="flex items-center gap-3">
          <button type="submit" className={`${btn} border-gray-900`} disabled={isPending || !prompt.trim()}>
            {params ? "Parse again" : "Parse"} <kbd className="text-xs">⌘↵</kbd>
          </button>
          {isPending && <span className="text-gray-400">working…</span>}
        </div>
      </form>

      {errors.length > 0 && (
        <ul className="rounded border border-red-300 bg-red-50 p-3 text-red-800">
          {errors.map((line, i) => (
            <li key={i}>{line}</li>
          ))}
        </ul>
      )}

      {created && (
        <p className="rounded border border-green-300 bg-green-50 p-3 text-green-800">
          Job {created.slice(0, 8)} queued. Progress appears in the list below.
        </p>
      )}

      {params && (
        <ParamsForm
          params={params}
          notes={notes}
          categories={categories}
          disabled={isPending}
          onChange={setParams}
          onConfirm={confirm}
          onCancel={() => {
            setParams(null);
            setNotes([]);
          }}
        />
      )}
    </div>
  );
}

function ParamsForm({
  params,
  notes,
  categories,
  disabled,
  onChange,
  onConfirm,
  onCancel,
}: {
  params: GenerationParams;
  notes: string[];
  categories: Category[];
  disabled: boolean;
  onChange: (p: GenerationParams) => void;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const field = "rounded border border-gray-300 bg-white px-2 py-1";
  const set = <K extends keyof GenerationParams>(key: K, value: GenerationParams[K]) =>
    onChange({ ...params, [key]: value });

  function toggle<T extends string>(list: T[], value: T): T[] {
    return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
  }

  const rangeInvalid = params.difficulty_min > params.difficulty_max;
  const noLocale = params.locales.length === 0;

  return (
    <form
      className="flex flex-col gap-3 rounded border border-gray-200 bg-white p-4"
      onSubmit={(e) => {
        e.preventDefault();
        onConfirm();
      }}
    >
      <div className="flex items-baseline justify-between">
        <h2 className="font-semibold">Confirm the job</h2>
        <span className="text-xs text-gray-400">edit anything before confirming</span>
      </div>

      {notes.length > 0 && (
        <ul className="rounded border border-yellow-300 bg-yellow-50 p-2 text-yellow-900">
          {notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}

      <div className="grid grid-cols-2 gap-x-6 gap-y-3 md:grid-cols-3">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-gray-500">category</span>
          <select
            className={field}
            value={params.category_slug}
            onChange={(e) => set("category_slug", e.target.value)}
          >
            {!categories.some((c) => c.slug === params.category_slug) && (
              <option value={params.category_slug}>{params.category_slug} (unknown)</option>
            )}
            {categories.map((c) => (
              <option key={c.id} value={c.slug}>
                {c.slug}
                {c.is_active ? "" : " (inactive)"}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-xs text-gray-500">count (1–200)</span>
          <input
            className={field}
            type="number"
            min={1}
            max={200}
            value={params.count}
            onChange={(e) => set("count", Math.max(1, Math.min(200, Number(e.target.value) || 1)))}
          />
        </label>

        <div className="flex flex-col gap-1">
          <span className="text-xs text-gray-500">difficulty range</span>
          <div className="flex items-center gap-2">
            <select
              className={field}
              value={params.difficulty_min}
              onChange={(e) => set("difficulty_min", Number(e.target.value))}
            >
              {DIFFICULTIES.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
            <span>to</span>
            <select
              className={field}
              value={params.difficulty_max}
              onChange={(e) => set("difficulty_max", Number(e.target.value))}
            >
              {DIFFICULTIES.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          </div>
          {rangeInvalid && <span className="text-xs text-red-700">min must be ≤ max</span>}
        </div>

        <fieldset className="flex flex-col gap-1">
          <legend className="text-xs text-gray-500">grade bands (none = any)</legend>
          <div className="flex flex-wrap gap-2">
            {GRADE_BANDS.map((g) => (
              <label key={g} className="flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={params.grade_bands.includes(g)}
                  onChange={() => set("grade_bands", toggle<GradeBand>(params.grade_bands, g))}
                />
                {g}
              </label>
            ))}
          </div>
        </fieldset>

        <label className="flex flex-col gap-1">
          <span className="text-xs text-gray-500">region</span>
          <select className={field} value={params.region} onChange={(e) => set("region", e.target.value as Region)}>
            {REGIONS.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>

        <fieldset className="flex flex-col gap-1">
          <legend className="text-xs text-gray-500">locales</legend>
          <div className="flex gap-3">
            {LOCALES.map((l) => (
              <label key={l} className="flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={params.locales.includes(l)}
                  onChange={() => set("locales", toggle<Locale>(params.locales, l))}
                />
                {l}
              </label>
            ))}
          </div>
          {noLocale && <span className="text-xs text-red-700">pick at least one</span>}
        </fieldset>

        <label className="col-span-2 flex flex-col gap-1 md:col-span-3">
          <span className="text-xs text-gray-500">style notes (passed to the writer verbatim)</span>
          <textarea
            className={field}
            rows={2}
            value={params.style_notes ?? ""}
            onChange={(e) => set("style_notes", e.target.value)}
          />
        </label>
      </div>

      <div className="flex items-center gap-2">
        <button
          type="submit"
          className="rounded border border-gray-900 bg-gray-900 px-3 py-1 text-white disabled:opacity-50"
          disabled={disabled || rangeInvalid || noLocale || !params.category_slug}
        >
          Confirm and queue {params.count} questions
        </button>
        <button type="button" className="rounded border px-3 py-1" onClick={onCancel} disabled={disabled}>
          Discard
        </button>
      </div>
    </form>
  );
}
