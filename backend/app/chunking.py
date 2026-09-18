"""Document chunking for RAG indexing.

Splits document text into overlapping chunks suitable for embedding and retrieval.
Uses tiktoken for accurate token counting when available, falls back to character estimation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

try:
    import tiktoken
except ImportError:  # pragma: no cover
    tiktoken = None  # type: ignore


@dataclass(frozen=True)
class Chunk:
    """A single text chunk with metadata."""
    content: str
    token_count: int
    chunk_index: int


# Default chunking parameters
DEFAULT_CHUNK_SIZE = 1000  # tokens
DEFAULT_OVERLAP = 200      # tokens
MAX_CHUNK_SIZE = 8000      # hard limit
MIN_CHUNK_SIZE = 100       # minimum meaningful chunk


def _get_encoding(model: str = "cl100k_base"):
    """Get tiktoken encoding, with fallback."""
    if tiktoken is None:
        return None
    try:
        return tiktoken.get_encoding(model)
    except Exception:
        return None


def _count_tokens(text: str, encoding) -> int:
    """Count tokens in text using tiktoken or character estimation."""
    if encoding is not None:
        return len(encoding.encode(text))
    # Fallback: rough estimation ~4 chars per token for English
    return max(1, len(text) // 4)


def _split_paragraphs(text: str) -> list[str]:
    """Split text into paragraphs, preserving structure."""
    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Split on double newlines (paragraph breaks)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text)]
    # Filter out empty paragraphs
    return [p for p in paragraphs if p]


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences using simple regex."""
    # Simple sentence splitting - handles common cases
    # This is a heuristic; for production consider using nltk or spacy
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    return [s.strip() for s in sentences if s.strip()]


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    encoding_name: str = "cl100k_base",
) -> list[Chunk]:
    """Split text into overlapping chunks respecting token limits.

    Strategy:
    1. Split into paragraphs
    2. Group paragraphs into chunks up to chunk_size tokens
    3. If a single paragraph exceeds chunk_size, split it by sentences
    4. Apply overlap between consecutive chunks

    Args:
        text: Input text to chunk
        chunk_size: Target token count per chunk
        overlap: Token overlap between consecutive chunks
        encoding_name: tiktoken encoding to use

    Returns:
        List of Chunk objects with content, token_count, and chunk_index
    """
    # Sanitize input
    if not text or not text.strip():
        return []

    # Strip null bytes and control characters (except newlines/tabs)
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ch >= " ")

    # Clamp parameters
    chunk_size = max(MIN_CHUNK_SIZE, min(chunk_size, MAX_CHUNK_SIZE))
    overlap = max(0, min(overlap, chunk_size // 2))

    encoding = _get_encoding(encoding_name)
    paragraphs = _split_paragraphs(text)

    chunks: list[Chunk] = []
    current_chunk_parts: list[str] = []
    current_tokens = 0
    chunk_index = 0

    def flush_chunk(parts: list[str], index: int) -> Chunk | None:
        """Create a chunk from accumulated parts."""
        if not parts:
            return None
        content = "\n\n".join(parts).strip()
        if not content:
            return None
        tokens = _count_tokens(content, encoding)
        return Chunk(content=content, token_count=tokens, chunk_index=index)

    for para in paragraphs:
        para_tokens = _count_tokens(para, encoding)

        # If single paragraph exceeds chunk_size, split by sentences
        if para_tokens > chunk_size:
            # Flush current chunk first
            if current_chunk_parts:
                chunk = flush_chunk(current_chunk_parts, chunk_index)
                if chunk:
                    chunks.append(chunk)
                    chunk_index += 1
                current_chunk_parts = []
                current_tokens = 0

            # Split large paragraph into sentences
            sentences = _split_sentences(para)
            sentence_buffer: list[str] = []
            sentence_tokens = 0

            for sent in sentences:
                sent_tokens = _count_tokens(sent, encoding)
                if sentence_tokens + sent_tokens > chunk_size and sentence_buffer:
                    chunk = flush_chunk(sentence_buffer, chunk_index)
                    if chunk:
                        chunks.append(chunk)
                        chunk_index += 1
                    # Start new chunk with overlap
                    overlap_text = _get_overlap_text(sentence_buffer, overlap, encoding)
                    sentence_buffer = overlap_text
                    sentence_tokens = sum(_count_tokens(s, encoding) for s in overlap_text)

                sentence_buffer.append(sent)
                sentence_tokens += sent_tokens

            # Flush remaining sentences
            if sentence_buffer:
                chunk = flush_chunk(sentence_buffer, chunk_index)
                if chunk:
                    chunks.append(chunk)
                    chunk_index += 1
            continue

        # Normal paragraph accumulation
        if current_tokens + para_tokens > chunk_size and current_chunk_parts:
            chunk = flush_chunk(current_chunk_parts, chunk_index)
            if chunk:
                chunks.append(chunk)
                chunk_index += 1
            # Start new chunk with overlap from previous
            overlap_text = _get_overlap_text(current_chunk_parts, overlap, encoding)
            current_chunk_parts = overlap_text
            current_tokens = sum(_count_tokens(p, encoding) for p in overlap_text)

        current_chunk_parts.append(para)
        current_tokens += para_tokens

    # Flush final chunk
    if current_chunk_parts:
        chunk = flush_chunk(current_chunk_parts, chunk_index)
        if chunk:
            chunks.append(chunk)

    return chunks


def _get_overlap_text(parts: list[str], overlap_tokens: int, encoding) -> list[str]:
    """Extract overlap text from the end of previous chunk parts."""
    if overlap_tokens <= 0 or not parts:
        return []

    # Combine all parts and take from the end
    full_text = "\n\n".join(parts)
    if encoding is not None:
        tokens = encoding.encode(full_text)
        if len(tokens) <= overlap_tokens:
            return parts  # Whole thing fits in overlap
        overlap_tokens_decoded = encoding.decode(tokens[-overlap_tokens:])
        # Try to find a clean boundary (paragraph or sentence)
        return [_find_clean_boundary(overlap_tokens_decoded)]
    else:
        # Character-based fallback
        est_chars = overlap_tokens * 4
        if len(full_text) <= est_chars:
            return parts
        overlap_text = full_text[-est_chars:]
        return [_find_clean_boundary(overlap_text)]


def _find_clean_boundary(text: str) -> str:
    """Find a clean boundary (paragraph or sentence) in text."""
    # Try paragraph boundary first
    para_match = re.search(r"\n\s*\n", text)
    if para_match:
        return text[para_match.end():].strip()

    # Try sentence boundary
    sent_match = re.search(r"[.!?]\s+", text)
    if sent_match:
        return text[sent_match.end():].strip()

    # No clean boundary found, return as-is
    return text.strip()


def chunk_document_text(text: str) -> list[Chunk]:
    """Convenience function using default parameters for document indexing."""
    return chunk_text(text, chunk_size=DEFAULT_CHUNK_SIZE, overlap=DEFAULT_OVERLAP)