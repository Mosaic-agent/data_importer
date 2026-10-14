# Agent-guide benchmark

This benchmark tests whether an agent guide improves real importer work; it
does not call an LLM. Run each task in `tasks.json` in a fresh, equivalent
agent session for the candidate and baseline guides. Record one JSON run per
task with the files read, evidence labels, input-token count, elapsed seconds,
and `passed`/`failed` outcome.

Create a guide-pinned result skeleton, then score it:

```bash
./mosaic.sh python -m data_importer.evals.guide_navigation_eval capture src/data_importer/AGENTS.md
./mosaic.sh python -m data_importer.evals.guide_navigation_eval score candidate.json
./mosaic.sh python -m data_importer.evals.guide_navigation_eval compare candidate.json baseline.json
```

The validator requires every task, its prescribed navigation reads, and
observable safety evidence. Treat a candidate as better only if correctness is
not lower and navigation/token/time metrics improve under equivalent sessions.
