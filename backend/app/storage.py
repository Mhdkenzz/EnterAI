"""Storage boundary: local disk now, swappable cloud adapters later."""
import os
from pathlib import Path
from typing import Protocol
from uuid import uuid4


class Storage(Protocol):
    def save(self, folder: str, filename: str, content: bytes) -> str: ...


class LocalStorage:
    def __init__(self, root: str | Path = "uploads"):
        self.root = Path(root)

    def save(self, folder: str, filename: str, content: bytes) -> str:
        target_dir = self.root / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename).name or "upload"
        target = target_dir / f"{uuid4()}-{safe_name}"
        target.write_bytes(content)
        return str(target)


def get_storage() -> Storage:
    backend = os.getenv("STORAGE_BACKEND", "local").lower()
    if backend != "local":
        raise RuntimeError(f"Unsupported STORAGE_BACKEND={backend!r}; install a cloud adapter before enabling it")
    return LocalStorage(os.getenv("STORAGE_ROOT", "uploads"))
