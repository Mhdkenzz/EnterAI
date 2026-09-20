"""Reusable evaluation harness foundation.

Framework: MODEL × PROVIDER × CONFIG × TEST SUITE
Does NOT delete or move existing results.
New runs must be immutable and timestamped.
"""
from __future__ import annotations
import json
import time
from typing import Any


class EvaluationRun:
    def __init__(self, model: str, provider: str, config: dict[str, Any], suite_name: str):
        self.timestamp = time.strftime("%Y%m%d_%H%M%S")
        self.run_id = f"{self.timestamp}_{model}"
        self.model = model
        self.provider = provider
        self.config = config
        self.suite_name = suite_name
        self.results: list[dict[str, Any]] = []

    def add_result(self, result: dict[str, Any]) -> None:
        self.results.append({**result, "run_id": self.run_id, "timestamp": self.timestamp})

    def write(self, output_dir: str) -> None:
        import os, json
        path = os.path.join(output_dir, self.run_id)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "config.json"), "w") as f:
            json.dump({"model": self.model, "provider": self.provider, "config": self.config, "suite": self.suite_name, "timestamp": self.timestamp}, f)
        with open(os.path.join(path, "raw_results.jsonl"), "w") as f:
            for r in self.results:
                f.write(json.dumps(r) + "\n")
