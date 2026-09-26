"use client";

import { usePathname, useRouter } from "next/navigation";
import type { Category, GenerationJob } from "@/lib/api/types";
import { DIFFICULTIES } from "@/lib/api/types";

export interface ReviewFilters {
  category?: string; // slug
  difficulty?: number;
  job?: string; // generation job id (the URL key jobs link with)
}

export function Filters({
  filters,
  categories,
  jobs,
}: {
  filters: ReviewFilters;
  categories: Category[];
  jobs: GenerationJob[];
}) {
  const router = useRouter();
  const pathname = usePathname();

  function update(patch: Partial<Record<keyof ReviewFilters, string>>) {
    const next = { ...filters, ...patch };
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(next)) {
      if (v !== undefined && v !== "") params.set(k, String(v));
    }
    // A filter change always starts from page 1.
    const qs = params.toString();
    router.push(qs ? `${pathname}?${qs}` : pathname);
  }

  const select = "rounded border border-gray-300 bg-white px-2 py-1";

  return (
    <div className="flex flex-wrap items-center gap-2">
      <select
        className={select}
        value={filters.category ?? ""}
        onChange={(e) => update({ category: e.target.value })}
        aria-label="Category"
      >
        <option value="">All categories</option>
        {categories.map((c) => (
          <option key={c.id} value={c.slug}>
            {c.slug}
          </option>
        ))}
      </select>
      <select
        className={select}
        value={filters.difficulty ?? ""}
        onChange={(e) => update({ difficulty: e.target.value })}
        aria-label="Difficulty"
      >
        <option value="">Any difficulty</option>
        {DIFFICULTIES.map((d) => (
          <option key={d} value={d}>
            Difficulty {d}
          </option>
        ))}
      </select>
      <select
        className={`${select} max-w-md`}
        value={filters.job ?? ""}
        onChange={(e) => update({ job: e.target.value })}
        aria-label="Generation job"
      >
        <option value="">All jobs</option>
        {jobs.map((j) => (
          <option key={j.id} value={j.id}>
            {j.created_at.slice(0, 16).replace("T", " ")} · {j.params.category_slug} · {j.status}
            {" · "}
            {j.accepted_count}/{j.requested_count ?? "?"} · {j.id.slice(0, 8)}
          </option>
        ))}
      </select>
      {(filters.category || filters.difficulty || filters.job) && (
        <button
          type="button"
          className="text-gray-500 underline"
          onClick={() => router.push(pathname)}
        >
          Clear
        </button>
      )}
    </div>
  );
}
