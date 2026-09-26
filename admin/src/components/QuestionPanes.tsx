"use client";

/** One locale of a question: read-only with the correct option highlighted, or as an editor. */
import type { Locale, QuestionTranslation } from "@/lib/api/types";
import type { DraftTranslation } from "@/lib/questionDraft";

export const optionLabel = (i: number) => String.fromCharCode(65 + i);

export function TranslationView({
  locale,
  translation,
  correctIndex,
}: {
  locale: Locale;
  translation: QuestionTranslation | undefined;
  correctIndex: number;
}) {
  return (
    <div>
      <div className="mb-1 text-xs font-semibold uppercase text-gray-500">{locale}</div>
      {!translation ? (
        <p className="rounded border border-dashed border-red-300 p-3 text-red-700">
          Missing {locale} translation — approve will be refused (409).
        </p>
      ) : (
        <>
          <p className="mb-2 text-base">
            {translation.stem}{" "}
            <span className="text-xs text-gray-400">({translation.stem.length}/120)</span>
          </p>
          <ol className="flex flex-col gap-1">
            {translation.options.map((opt, i) => (
              <li
                key={i}
                className={`rounded border px-2 py-1 ${
                  i === correctIndex
                    ? "border-green-500 bg-green-50 font-medium"
                    : "border-gray-200"
                }`}
              >
                <span className="mr-2 text-gray-400">{optionLabel(i)}</span>
                {opt}
              </li>
            ))}
          </ol>
          {translation.explanation && (
            <p className="mt-2 text-gray-600">{translation.explanation}</p>
          )}
        </>
      )}
    </div>
  );
}

export function TranslationEditor({
  locale,
  value,
  correctIndex,
  autoFocus,
  onChange,
  onPickCorrect,
}: {
  locale: Locale;
  value: DraftTranslation;
  correctIndex: number;
  autoFocus: boolean;
  onChange: (t: DraftTranslation) => void;
  onPickCorrect: (i: number) => void;
}) {
  const field = "w-full rounded border border-gray-300 px-2 py-1";
  return (
    <div className="flex flex-col gap-2">
      <div className="text-xs font-semibold uppercase text-gray-500">{locale}</div>
      <label className="block">
        <span className="text-xs text-gray-500">stem ({value.stem.length}/120)</span>
        <textarea
          className={field}
          rows={2}
          value={value.stem}
          autoFocus={autoFocus}
          onChange={(e) => onChange({ ...value, stem: e.target.value })}
        />
      </label>
      {value.options.map((opt, i) => (
        <label
          key={i}
          className={`flex items-center gap-2 rounded border px-2 py-1 ${
            i === correctIndex ? "border-green-500 bg-green-50" : "border-gray-200"
          }`}
        >
          <input
            type="radio"
            name={`correct-${locale}`}
            checked={i === correctIndex}
            onChange={() => onPickCorrect(i)}
            title={`Correct option (${i + 1})`}
          />
          <span className="w-4 text-gray-400">{optionLabel(i)}</span>
          <input
            className="flex-1 rounded border border-gray-300 px-2 py-0.5"
            value={opt}
            onChange={(e) => {
              const options = [...value.options];
              options[i] = e.target.value;
              onChange({ ...value, options });
            }}
          />
          <span className="text-xs text-gray-400">{opt.length}/60</span>
        </label>
      ))}
      <label className="block">
        <span className="text-xs text-gray-500">explanation</span>
        <textarea
          className={field}
          rows={2}
          value={value.explanation}
          onChange={(e) => onChange({ ...value, explanation: e.target.value })}
        />
      </label>
    </div>
  );
}
