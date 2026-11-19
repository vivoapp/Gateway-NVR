# Milestone 4 results: M25, M26

Status: **built on a workstation (Oct 29, 2026), unit tested; the gateway runs are pending.**
The scope runs M25 and M26 last, once the other modules are accepted. Each "Result" is filled in
from the reference gateway's reports.

## M25 Load and resource acceptance

| Done when (scope) | Result | Evidence |
|---|---|---|
| A final run at 10 cameras with everything in scope on (both streams, retention, health endpoint), for the agreed length (24 h) | pending | m25-load |
| Pass/fail against the agreed limits: CPU, RAM, temperature, disk headroom, unexplained recording gaps (target zero) | pending | m25-load |
| The same report at 4 and 8 cameras for comparison | pending | m25-load |
| Vivo can rerun it from the script | `tests/m25_load.py`, docs/m25-load.md | |

## M26 Fault simulation and handoff

| Done when (scope) | Result | Evidence |
|---|---|---|
| One test runner combining the M10, M11 and M12 tests into a single repeatable test matrix, with a results table | `tests/m26_matrix.py`, docs/m26-handoff.md; reference run pending | m26-matrix |
| Deployment instructions that take a blank N100/N150 to a working gateway | docs/deploy.md | |
| Final source handoff: everything in the Vivo repository, cleaned up, no secrets committed, every version pinned | `tests/handoff_check.py`; run at handoff | handoff-check |
| Known-limitations list | docs/known-limitations.md | |
| Walkthrough call (up to 1 hour) | agenda in docs/m26-handoff.md; to be scheduled | |
| A Vivo engineer deploys a gateway from the instructions alone and runs the test matrix with the same results | pending | |

Workstation: the unit tests cover the M25 limit checks (rolling CPU peak, RAM growth, disk projection,
gap causes), the matrix's step selection, verdicts and whole-window gap check, and the handoff
check's secret and version-pin rules.
