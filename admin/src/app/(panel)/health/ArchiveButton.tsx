"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { archiveQuestion } from "./actions";

/** Archive one question (a second click confirms); refreshes the list on success. */
export function ArchiveButton({ id, disabled }: { id: string; disabled?: boolean }) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [armed, setArmed] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function click() {
    if (!armed) {
      setArmed(true);
      return;
    }
    startTransition(async () => {
      const result = await archiveQuestion(id);
      if (result.ok) {
        setArmed(false);
        router.refresh();
      } else {
        setError(result.messages.join("; "));
      }
    });
  }

  return (
    <span className="inline-flex flex-col items-start gap-1">
      <button
        type="button"
        className={`rounded border px-2 py-0.5 text-xs ${armed ? "border-red-600 bg-red-600 text-white" : "border-red-600 text-red-800"} disabled:opacity-50`}
        disabled={disabled || isPending}
        onClick={click}
        onBlur={() => setArmed(false)}
      >
        {isPending ? "archiving…" : armed ? "click again to archive" : "Archive"}
      </button>
      {error && <span className="text-xs text-red-700">{error}</span>}
    </span>
  );
}
