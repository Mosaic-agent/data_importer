"""Score recorded runs of the data-importer agent-guide benchmark.

This module deliberately does not call an LLM. Run each task in a clean agent
session, record the session's observable metrics, then validate and compare the
records with this CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any


EVAL_DIR = Path(__file__).parent
TASKS_PATH = EVAL_DIR / "tasks.json"
REQUIRED_RUN_FIELDS = {"task_id", "outcome", "files_read", "evidence", "input_tokens", "elapsed_seconds"}


def load_tasks(path: Path = TASKS_PATH) -> dict[str, dict[str, Any]]:
    """Return benchmark tasks keyed by their stable identifier."""
    payload = json.loads(path.read_text())
    return {task["id"]: task for task in payload["tasks"]}


def guide_digest(path: Path) -> str:
    """Return the SHA-256 digest of the exact guide evaluated."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def word_count(path: Path) -> int:
    return len(path.read_text().split())


def capture(guide_path: Path) -> dict[str, Any]:
    """Create a result-record skeleton tied to one immutable guide revision."""
    return {
        "guide_path": str(guide_path),
        "guide_sha256": guide_digest(guide_path),
        "guide_words": word_count(guide_path),
        "runs": [],
    }


def _is_nonnegative_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _missing_values(values: Iterable[str], required: Iterable[str]) -> list[str]:
    present = set(values)
    return sorted(set(required) - present)


def validate(record: dict[str, Any], tasks: dict[str, dict[str, Any]] | None = None) -> list[str]:
    """Validate coverage and navigation evidence; return human-readable errors."""
    tasks = tasks or load_tasks()
    errors: list[str] = []
    runs = record.get("runs")
    if not isinstance(runs, list):
        return ["runs must be a list"]

    run_ids: list[str] = []
    for index, run in enumerate(runs):
        if not isinstance(run, dict):
            errors.append(f"run {index} must be an object")
            continue
        missing_fields = sorted(REQUIRED_RUN_FIELDS - set(run))
        if missing_fields:
            errors.append(f"run {index} missing fields: {', '.join(missing_fields)}")
            continue
        task_id = run["task_id"]
        run_ids.append(task_id)
        task = tasks.get(task_id)
        if task is None:
            errors.append(f"run {index} has unknown task_id: {task_id}")
            continue
        if run["outcome"] not in {"passed", "failed"}:
            errors.append(f"{task_id}: outcome must be passed or failed")
        if not isinstance(run["files_read"], list) or not all(isinstance(item, str) for item in run["files_read"]):
            errors.append(f"{task_id}: files_read must be a list of paths")
        else:
            missing_reads = _missing_values(run["files_read"], task["required_reads"])
            if missing_reads:
                errors.append(f"{task_id}: missing required reads: {', '.join(missing_reads)}")
        if not isinstance(run["evidence"], list) or not all(isinstance(item, str) for item in run["evidence"]):
            errors.append(f"{task_id}: evidence must be a list of labels")
        else:
            missing_evidence = _missing_values(run["evidence"], task["required_evidence"])
            if missing_evidence:
                errors.append(f"{task_id}: missing evidence: {', '.join(missing_evidence)}")
        for metric in ("input_tokens", "elapsed_seconds"):
            if not _is_nonnegative_number(run[metric]):
                errors.append(f"{task_id}: {metric} must be a non-negative number")

    expected_ids = set(tasks)
    if len(run_ids) != len(set(run_ids)):
        errors.append("each task must have exactly one run")
    missing_tasks = sorted(expected_ids - set(run_ids))
    if missing_tasks:
        errors.append(f"missing tasks: {', '.join(missing_tasks)}")
    return errors


def summary(record: dict[str, Any]) -> dict[str, Any]:
    """Return comparable metrics after successful validation."""
    runs = record["runs"]
    count = len(runs)
    passed = sum(run["outcome"] == "passed" for run in runs)
    return {
        "guide_path": record.get("guide_path"),
        "guide_sha256": record.get("guide_sha256"),
        "guide_words": record.get("guide_words"),
        "task_count": count,
        "passed_tasks": passed,
        "success_rate": passed / count if count else 0,
        "mean_input_tokens": sum(run["input_tokens"] for run in runs) / count if count else 0,
        "mean_files_read": sum(len(run["files_read"]) for run in runs) / count if count else 0,
        "mean_elapsed_seconds": sum(run["elapsed_seconds"] for run in runs) / count if count else 0,
    }


def compare(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Return candidate-minus-baseline deltas for the published metrics."""
    candidate_summary = summary(candidate)
    baseline_summary = summary(baseline)
    metrics = ("success_rate", "mean_input_tokens", "mean_files_read", "mean_elapsed_seconds")
    return {
        "candidate": candidate_summary,
        "baseline": baseline_summary,
        "delta": {metric: candidate_summary[metric] - baseline_summary[metric] for metric in metrics},
    }


def _read_record(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture", help="print a result-record skeleton")
    capture_parser.add_argument("guide", type=Path)
    score_parser = subparsers.add_parser("score", help="validate and summarize one result record")
    score_parser.add_argument("record", type=Path)
    compare_parser = subparsers.add_parser("compare", help="compare candidate and baseline records")
    compare_parser.add_argument("candidate", type=Path)
    compare_parser.add_argument("baseline", type=Path)
    args = parser.parse_args()

    if args.command == "capture":
        print(json.dumps(capture(args.guide), indent=2, sort_keys=True))
        return

    candidate = _read_record(args.record if args.command == "score" else args.candidate)
    errors = validate(candidate)
    if errors:
        parser.error("invalid candidate record:\n- " + "\n- ".join(errors))
    if args.command == "score":
        print(json.dumps(summary(candidate), indent=2, sort_keys=True))
        return

    baseline = _read_record(args.baseline)
    errors = validate(baseline)
    if errors:
        parser.error("invalid baseline record:\n- " + "\n- ".join(errors))
    print(json.dumps(compare(candidate, baseline), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
