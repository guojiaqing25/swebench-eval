"""Convert official SWE-bench logs/reports into a bounded repair feedback instance."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

MAX_LOG_CHARS = 12_000
MAX_CONTEXTS = 5


def _failure_excerpt(text: str) -> str:
    markers = re.compile(r"(?:FAILED|FAIL:|panic:|Traceback|error\[|AssertionError|Exception)", re.IGNORECASE)
    lines = text.splitlines()
    indexes = [index for index, line in enumerate(lines) if markers.search(line)]
    if not indexes:
        return text[-MAX_LOG_CHARS:]
    start = max(0, indexes[-1] - 25)
    return "\n".join(lines[start:])[-MAX_LOG_CHARS:]


def _source_contexts(code: str, filename: str, log: str) -> list[str]:
    lines = code.splitlines()
    patterns = (
        re.compile(re.escape(filename) + r"[:(](\d+)", re.IGNORECASE),
        re.compile(re.escape(Path(filename).name) + r"[:(](\d+)", re.IGNORECASE),
    )
    numbers: list[int] = []
    for pattern in patterns:
        for match in pattern.finditer(log):
            number = int(match.group(1))
            if number not in numbers:
                numbers.append(number)
    contexts = []
    for number in numbers[-MAX_CONTEXTS:]:
        start = max(0, number - 8)
        end = min(len(lines), number + 7)
        contexts.append("\n".join(f"{index + 1}: {lines[index]}" for index in range(start, end)))
    return contexts


def build_feedback(
    instance: dict[str, Any], prediction: dict[str, Any], log_text: str,
    evaluation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    patch = str(prediction.get("model_patch", ""))
    if not patch.strip():
        raise ValueError("Prediction has no model_patch")
    feedback = dict(instance)
    excerpt = _failure_excerpt(log_text)
    contexts = _source_contexts(
        str(instance.get("code", "")), str(instance.get("filename", "")), log_text,
    )
    status = json.dumps(evaluation or {}, ensure_ascii=False, sort_keys=True)
    feedback["problem_statement"] = (
        str(instance.get("problem_statement", "")).rstrip()
        + "\n\nPrevious candidate failed independent regression. "
        "The original problem_statement and FAIL_TO_PASS tests remain the only repair target. "
        "Use the latest observed failure and source context; do not repeat the failed diff.\n"
        + f"Evaluation summary: {status}\n"
        + "Previous candidate diff:\n" + patch
        + "\nObserved regression output:\n" + excerpt
        + "\nCandidate source around reported locations:\n" + "\n\n".join(contexts)
    )
    feedback["previous_attempt"] = {
        "model_patch": patch,
        "evaluation": evaluation or {},
        "log_excerpt": excerpt,
        "source_contexts": contexts,
    }
    return feedback


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--prediction", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    instance = json.loads(args.input.read_text(encoding="utf-8"))
    prediction = json.loads(args.prediction.read_text(encoding="utf-8"))
    evaluation = (
        json.loads(args.evaluation.read_text(encoding="utf-8"))
        if args.evaluation and args.evaluation.is_file() else None
    )
    feedback = build_feedback(
        instance, prediction, args.log.read_text(encoding="utf-8", errors="replace"), evaluation,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(feedback, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
