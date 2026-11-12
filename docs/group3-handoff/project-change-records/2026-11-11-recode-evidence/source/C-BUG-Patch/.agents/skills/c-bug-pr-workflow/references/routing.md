# Routing and common handoff

For the first group's REcode service, select `--first-group-provider recode`
and `--first-group-url http://127.0.0.1:8090`. Its default Docker analysis uses
`repository.build_profile=safe-generic`. An existing result directory can be
supplied with `--recode-artifacts`; an existing task uses `--recode-job-id`.
The adapter handles submission, polling and downloads, checks version bindings,
then provides the current shared fact contract. Preserve its `partial` coverage,
unresolved declarations/contexts and explicit nonformal evidence. See
`docs/integrations/recode.md` for setup and verified examples.

The host or PR adapter supplies `review-input.v1` to `v2-common/review_pipeline.py`. See `v2-common/review_input.py` for required fields. The current first-group client fetches call chains before `v2-common/coordinator.py` chooses domains; chosen domains receive resource flows (Memory) or synchronization flows (Concurrency). The first group supplies structured facts, not defect conclusions.

The router fetches call-chain, resource-flow, and synchronization-flow facts before classification. A resource flow or a same-context release/use pattern selects Memory; different-context shared accesses, lock operations, or lock-order edges select Concurrency. Both kinds select both domains. If a fact response is missing or neither signal is present, both remain enabled. The older diff/call-chain word scan remains a fallback only for callers that do not provide structured facts. `requested_routes` is an explicit override and must be either `memory`, `concurrency`, or both. Treat the reported route confidence as a heuristic value, not a measured detection probability. These facts choose a reviewer; they do not establish a defect.

Inspect the route against the PR's actual behavior before dispatch:

- Memory signals: changed ownership, allocation/release, borrowing, transfer, reference count, cancellation, or an asynchronous object's lifetime.
- Concurrency signals: shared mutable state, worker or callback overlap, locks and lock order, atomics, condition variables, event-loop serialization, or a lifetime race.
- Run both when a resource can be freed while another context accesses it, when ownership crosses an asynchronous boundary with unclear synchronization, or when one route cannot safely exclude the other.

The coordination report contains `routing`, `first_group`, `route_errors`, `bundle_paths`, and `handoffs`. A `prepared` handoff identifies a domain ContextBundle and registered entrypoint; it does not mean the specialty has run. A `partial` report may still contain usable bundles. Preserve failed-route errors in the final report, and do not infer `no_defect` for a domain whose facts or bundle are missing. If call-chain preparation fails, no domain should be claimed reviewed.

Example local entrypoint (Python 3.12):

```powershell
py -3.12 v2-common/review_pipeline.py --review <review-input.json> --first-group-url <url> --output-dir <run-dir>
```

The local HTTP Mock and fixture inputs exercise integration only. For a real GitHub PR, the host can call `v2-common/acquire_github_pr.py` once to obtain the exact checkout and shared `review-input.json`; inspect `acquisition_coverage_gaps` before routing. Automatic changed-function location is conservative and may require a verified `--changed-functions-json`. A production first-group service is an external dependency. Do not read evaluator-private oracle, healthy/fixed source, or reference patches while reviewing a benchmark case.
