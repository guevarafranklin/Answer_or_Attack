/**
 * Edit state for a question (review queue and the standalone editor) and its
 * conversion to the PATCH body. Safe to import from client components: no
 * env, no fetch.
 */
import { LOCALES, OPTION_COUNT } from "@/lib/api/types";
import type {
  GradeBand,
  Locale,
  Question,
  QuestionTranslation,
  QuestionUpdate,
  Region,
} from "@/lib/api/types";

export interface DraftTranslation {
  stem: string;
  options: string[];
  explanation: string;
}

export interface Draft {
  translations: Record<Locale, DraftTranslation>;
  correct_index: number;
  difficulty: number;
  region: Region;
  grade_band: GradeBand | "";
  tags: string; // comma-separated
}

export function byLocale(q: Question): Partial<Record<Locale, QuestionTranslation>> {
  return Object.fromEntries(q.translations.map((t) => [t.locale, t]));
}

export function toDraft(q: Question): Draft {
  const t = byLocale(q);
  const translations = Object.fromEntries(
    LOCALES.map((locale) => {
      const tr = t[locale];
      const options = [...(tr?.options ?? [])];
      while (options.length < OPTION_COUNT) options.push("");
      return [locale, { stem: tr?.stem ?? "", options, explanation: tr?.explanation ?? "" }];
    }),
  ) as Record<Locale, DraftTranslation>;
  return {
    translations,
    correct_index: q.correct_index,
    difficulty: q.difficulty,
    region: q.region,
    grade_band: q.grade_band ?? "",
    tags: q.tags.join(", "),
  };
}

/** Everything is sent (PATCH replaces a locale that is sent); a locale left
 * entirely blank is omitted so a question missing ES can still have its EN
 * text fixed. */
export function toPatch(d: Draft): QuestionUpdate {
  const translations: QuestionUpdate["translations"] = {};
  for (const locale of LOCALES) {
    const t = d.translations[locale];
    const blank = !t.stem.trim() && t.options.every((o) => !o.trim());
    if (blank) continue;
    translations[locale] = {
      stem: t.stem,
      options: t.options,
      explanation: t.explanation.trim() ? t.explanation : null,
    };
  }
  return {
    translations,
    correct_index: d.correct_index,
    difficulty: d.difficulty,
    region: d.region,
    grade_band: d.grade_band === "" ? null : d.grade_band,
    tags: d.tags
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
  };
}
