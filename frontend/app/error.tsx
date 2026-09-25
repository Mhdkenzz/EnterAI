"use client";
import { useEffect } from "react";

/** Without this, an exception while rendering leaves a blank page with no way back. */
export default function WorkspaceError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);
  return (
    <main className="grid min-h-screen place-items-center bg-canvas p-6 text-ink">
      <div role="alert" className="max-w-sm space-y-4 text-center">
        <h1 className="text-lg font-semibold">Something went wrong</h1>
        <p className="text-sm text-zinc-400">The page hit an unexpected error. Try again, or reload to start fresh.</p>
        <div className="flex justify-center gap-2">
          <button className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-slate-950" onClick={reset}>
            Try again
          </button>
          <button className="rounded-lg border border-line px-4 py-2 text-sm" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      </div>
    </main>
  );
}
