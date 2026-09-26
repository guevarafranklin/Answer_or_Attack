"use client";

/**
 * One form for both create (no `category`) and edit. Both locales are always
 * shown; on create both are required by the API, on edit each locale is sent
 * whole (name + description) so a blank name is refused by the API's 422.
 */
import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { LOCALES, type Category, type CategoryTranslationIn, type Locale } from "@/lib/api/types";
import { createCategory, updateCategory } from "./actions";

interface Form {
  slug: string;
  icon: string;
  sort_order: string;
  is_active: boolean;
  translations: Record<Locale, { name: string; description: string }>;
}

function fromCategory(c: Category | undefined): Form {
  const t = Object.fromEntries((c?.translations ?? []).map((x) => [x.locale, x]));
  return {
    slug: c?.slug ?? "",
    icon: c?.icon ?? "",
    sort_order: String(c?.sort_order ?? 0),
    is_active: c?.is_active ?? true,
    translations: Object.fromEntries(
      LOCALES.map((l) => [l, { name: t[l]?.name ?? "", description: t[l]?.description ?? "" }]),
    ) as Form["translations"],
  };
}

function translationsOf(form: Form): Record<Locale, CategoryTranslationIn> {
  return Object.fromEntries(
    LOCALES.map((l) => [
      l,
      {
        name: form.translations[l].name.trim(),
        description: form.translations[l].description.trim() || null,
      },
    ]),
  ) as Record<Locale, CategoryTranslationIn>;
}

export function CategoryForm({ category, onDone }: { category?: Category; onDone?: () => void }) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [form, setForm] = useState(() => fromCategory(category));
  const [errors, setErrors] = useState<string[]>([]);
  const [saved, setSaved] = useState(false);
  const editing = !!category;

  function submit() {
    setErrors([]);
    setSaved(false);
    const sort_order = Number(form.sort_order);
    startTransition(async () => {
      const result = editing
        ? await updateCategory(category.id, {
            icon: form.icon.trim() || null,
            sort_order: Number.isFinite(sort_order) ? sort_order : 0,
            is_active: form.is_active,
            translations: translationsOf(form),
          })
        : await createCategory({
            slug: form.slug.trim(),
            icon: form.icon.trim() || null,
            sort_order: Number.isFinite(sort_order) ? sort_order : 0,
            translations: translationsOf(form),
          });
      if (result.ok) {
        setSaved(true);
        if (!editing) setForm(fromCategory(undefined));
        router.refresh();
        onDone?.();
      } else {
        setErrors(result.messages);
      }
    });
  }

  const field = "w-full rounded border border-gray-300 bg-white px-2 py-1";

  return (
    <form
      className="flex flex-col gap-3 rounded border border-gray-200 bg-white p-4"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-gray-500">slug {editing && "(fixed)"}</span>
          <input
            className={field}
            value={form.slug}
            disabled={editing}
            required={!editing}
            pattern="[a-z0-9]+(-[a-z0-9]+)*"
            title="lowercase letters, digits and single hyphens"
            placeholder="world-history"
            onChange={(e) => setForm({ ...form, slug: e.target.value })}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-gray-500">icon</span>
          <input className={field} value={form.icon} placeholder="🌍" onChange={(e) => setForm({ ...form, icon: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-gray-500">sort order</span>
          <input
            className={field}
            type="number"
            value={form.sort_order}
            onChange={(e) => setForm({ ...form, sort_order: e.target.value })}
          />
        </label>
        {editing && (
          <label className="flex items-end gap-2 pb-1">
            <input
              type="checkbox"
              checked={form.is_active}
              onChange={(e) => setForm({ ...form, is_active: e.target.checked })}
            />
            active
          </label>
        )}
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        {LOCALES.map((locale) => (
          <fieldset key={locale} className="flex flex-col gap-2">
            <legend className="text-xs font-semibold uppercase text-gray-500">{locale}</legend>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-gray-500">name</span>
              <input
                className={field}
                value={form.translations[locale].name}
                required
                onChange={(e) =>
                  setForm({
                    ...form,
                    translations: {
                      ...form.translations,
                      [locale]: { ...form.translations[locale], name: e.target.value },
                    },
                  })
                }
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-gray-500">description</span>
              <textarea
                className={field}
                rows={2}
                value={form.translations[locale].description}
                onChange={(e) =>
                  setForm({
                    ...form,
                    translations: {
                      ...form.translations,
                      [locale]: { ...form.translations[locale], description: e.target.value },
                    },
                  })
                }
              />
            </label>
          </fieldset>
        ))}
      </div>

      {errors.length > 0 && (
        <ul className="rounded border border-red-300 bg-red-50 p-2 text-red-800">
          {errors.map((line, i) => (
            <li key={i}>{line}</li>
          ))}
        </ul>
      )}

      <div className="flex items-center gap-2">
        <button
          type="submit"
          className="rounded border border-gray-900 bg-gray-900 px-3 py-1 text-white disabled:opacity-50"
          disabled={isPending}
        >
          {editing ? "Save" : "Create"}
        </button>
        {onDone && (
          <button type="button" className="rounded border px-3 py-1" onClick={onDone} disabled={isPending}>
            Cancel
          </button>
        )}
        {isPending && <span className="text-gray-400">working…</span>}
        {saved && <span className="text-green-800">Saved.</span>}
      </div>
    </form>
  );
}
