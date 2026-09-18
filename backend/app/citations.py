"""Citation tracking for retrieved document chunks."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class Citation:
    document_id: str
    chunk_index: int
    document_name: str
    snippet: str
    similarity_score: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "chunk_index": self.chunk_index,
            "document_name": self.document_name,
            "snippet": self.snippet,
            "similarity_score": self.similarity_score,
        }
