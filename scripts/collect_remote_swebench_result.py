"""Collect a compact, machine-readable summary from an official SWE-bench run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _find_instance_report(root: Path, instance_id: str) -> dict[str, Any]:
    candidates = sorted(root.rglob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        if "remote_cases" in path.parts or path.name.endswith("_result.json"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            if instance_id in payload and isinstance(payload[instance_id], dict):
                return {"source": str(path), "report": payload[instance_id]}
            if payload.get("instance_id") == instance_id:
                return {"source": str(path), "report": payload}
    return {"source": "", "report": {}}


def collect(instance_id: str, dataset: str, exit_code: int, log: Path, root: Path) -> dict[str, Any]:
    found = _find_instance_report(root, instance_id)
    report = found["report"]
    resolved = report.get("resolved") if isinstance(report, dict) else None
    if not isinstance(resolved, bool):
        resolved = exit_code == 0 and "Resolved: 1" in log.read_text(encoding="utf-8", errors="replace")
    return {
        "instance_id": instance_id,
        "dataset": dataset,
        "harness_exit_code": exit_code,
        "resolved": resolved,
        "report_source": found["source"],
        "report": report,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = collect(args.instance_id, args.dataset, args.exit_code, args.log, args.root)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
