"use client";

import { useState } from "react";
import Link from "next/link";
import { LOCALES, type Category } from "@/lib/api/types";
import { CategoryForm } from "./CategoryForm";

export function CategoryList({ categories }: { categories: Category[] }) {
  const [editingId, setEditingId] = useState<string | null>(null);

  if (categories.length === 0) return <p className="text-gray-500">No categories yet.</p>;

  return (
    <div className="overflow-x-auto rounded border border-gray-200 bg-white">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase text-gray-500">
          <tr>
            <th className="px-3 py-1">slug</th>
            {LOCALES.map((l) => (
              <th key={l} className="px-3 py-1">
                {l}
              </th>
            ))}
            <th className="px-3 py-1 text-right">order</th>
            <th className="px-3 py-1">active</th>
            <th className="px-3 py-1"></th>
          </tr>
        </thead>
        <tbody>
          {categories.map((c) => {
            const t = Object.fromEntries(c.translations.map((x) => [x.locale, x]));
            const editing = editingId === c.id;
            return (
              <Row key={c.id} editing={editing}>
                {editing ? (
                  <td colSpan={LOCALES.length + 4} className="p-2">
                    <CategoryForm key={c.id} category={c} onDone={() => setEditingId(null)} />
                  </td>
                ) : (
                  <>
                    <td className="px-3 py-1.5 whitespace-nowrap">
                      {c.icon && <span className="mr-1">{c.icon}</span>}
                      <Link href={`/review?category=${encodeURIComponent(c.slug)}`} className="underline">
                        {c.slug}
                      </Link>
                    </td>
                    {LOCALES.map((l) => (
                      <td key={l} className="px-3 py-1.5">
                        {t[l] ? (
                          <>
                            <div>{t[l].name}</div>
                            {t[l].description && <div className="text-xs text-gray-500">{t[l].description}</div>}
                          </>
                        ) : (
                          <span className="text-red-700">missing</span>
                        )}
                      </td>
                    ))}
                    <td className="px-3 py-1.5 text-right tabular-nums">{c.sort_order}</td>
                    <td className="px-3 py-1.5">{c.is_active ? "yes" : <span className="text-yellow-800">no</span>}</td>
                    <td className="px-3 py-1.5 text-right whitespace-nowrap">
                      <button
                        type="button"
                        className="rounded border border-gray-300 px-2 py-0.5 text-xs"
                        onClick={() => setEditingId(c.id)}
                      >
                        Edit
                      </button>
                    </td>
                  </>
                )}
              </Row>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Row({ editing, children }: { editing: boolean; children: React.ReactNode }) {
  return <tr className={`border-t border-gray-100 align-top ${editing ? "bg-gray-50" : ""}`}>{children}</tr>;
}
