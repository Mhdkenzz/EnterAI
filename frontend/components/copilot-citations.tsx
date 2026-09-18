import React from "react";

export function CopilotCitations({ citations }: { citations: Array<{ snippet: string; document_name: string; chunk_index: number; similarity_score?: number }> }) {
  return (
    <div className="mt-3 rounded-md border border-gray-200 bg-gray-50 p-3">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">Sources</h4>
      <ul className="space-y-2">
        {citations.map((c, i) => (
          <li key={i} className="flex gap-2 text-xs text-gray-700">
            <span className="font-medium text-blue-600 shrink-0">{c.document_name || `Doc #${i + 1}`}</span>
            <span>chunk {c.chunk_index}</span>
            {c.similarity_score !== undefined && (
              <span className="text-gray-400">(sim: {c.similarity_score.toFixed(2)})</span>
            )}
            <span className="line-clamp-1">{c.snippet}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
