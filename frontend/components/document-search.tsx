import React from "react";

export interface Citation {
  document_id: string;
  chunk_index: number;
  snippet: string;
  document_name?: string;
}

export function CitationBadge({ citation }: { citation: Citation }) {
  return (
    <a
      href={`#document-${citation.document_id}`}
      className="inline-flex items-center gap-1 rounded-full bg-blue-50 px-2 py-0.5 text-xs text-blue-700 hover:bg-blue-100"
      title={`Source: ${citation.document_name || citation.document_id}`}
    >
      <span>📄</span>
      <span>Chunk {citation.chunk_index}</span>
    </a>
  );
}

export function DocumentSearchUI({ results }: { results: Array<{ citation: Citation; snippet: string }> }) {
  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold text-gray-900">Search Results (Document Chunks)</h3>
      {results.map((r, i) => (
        <div key={i} className="rounded-lg border border-gray-200 p-3 bg-white shadow-sm">
          <div className="flex items-center gap-2 mb-2">
            <CitationBadge citation={r.citation} />
          </div>
          <p className="text-sm text-gray-700">{r.snippet}</p>
        </div>
      ))}
    </div>
  );
}
