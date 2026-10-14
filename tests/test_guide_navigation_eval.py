from __future__ import annotations

from data_importer.evals.guide_navigation_eval import compare, summary, validate


def _record() -> dict:
    return {
        "guide_path": "AGENTS.md",
        "guide_sha256": "digest",
        "guide_words": 100,
        "runs": [
            {
                "task_id": "add_fetcher",
                "outcome": "passed",
                "files_read": ["docs/adding-fetchers.md", "docs/architecture.md"],
                "evidence": ["dry_run"],
                "input_tokens": 100,
                "elapsed_seconds": 10,
            },
            {
                "task_id": "repair_delta_import",
                "outcome": "passed",
                "files_read": ["docs/clickhouse.md"],
                "evidence": ["final_query", "watermark_check"],
                "input_tokens": 200,
                "elapsed_seconds": 20,
            },
            {
                "task_id": "validate_amc_importer",
                "outcome": "passed",
                "files_read": ["docs/amc-factory.md", "docs/testing.md"],
                "evidence": ["dry_run", "test_run"],
                "input_tokens": 300,
                "elapsed_seconds": 30,
            },
        ],
    }


def test_valid_record_scores_all_tasks() -> None:
    record = _record()

    assert validate(record) == []
    assert summary(record)["passed_tasks"] == 3


def test_missing_navigation_evidence_fails_validation() -> None:
    record = _record()
    record["runs"][1]["evidence"] = ["watermark_check"]

    assert "repair_delta_import: missing evidence: final_query" in validate(record)


def test_compare_returns_candidate_deltas() -> None:
    baseline = _record()
    candidate = _record()
    candidate["runs"][0]["input_tokens"] = 50

    report = compare(candidate, baseline)

    assert report["delta"]["mean_input_tokens"] < 0
