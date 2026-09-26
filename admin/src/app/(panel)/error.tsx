"use client";

export default function PanelError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div className="rounded border border-red-300 bg-red-50 p-4 text-red-800">
      <p className="font-medium">Something failed outside the API&apos;s validation.</p>
      <p className="mt-1 font-mono text-xs">{error.message}</p>
      <p className="mt-2 text-gray-600">Usually the API is down or admin/.env.local points at the wrong place.</p>
      <button type="button" onClick={reset} className="mt-3 rounded border border-gray-400 bg-white px-2 py-1">
        Retry
      </button>
    </div>
  );
}
