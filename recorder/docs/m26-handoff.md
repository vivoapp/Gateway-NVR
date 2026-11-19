# M26: fault simulation and handoff

M26 has four parts: one test runner for the M10, M11 and M12 tests, deployment instructions for a
blank gateway (docs/deploy.md), a check that the source is ready to hand over, and the
known-limitations list (docs/known-limitations.md). The walkthrough call agenda is at the end.

## Test matrix

`tests/m26_matrix.py` runs the fault tests of M10, M11 and M12 in a fixed order, as one repeatable
test, and writes one results table. It runs on a workstation with SSH key access to the gateway and
passwordless sudo there, because two steps reboot or power-cycle the gateway.

| Step | Module | What | Runs on | Time |
|---|---|---|---|---|
| m10-disconnect | M10 | 5 camera disconnects through the PoE switch | gateway | about 15 min |
| m11-power | M11 | 10 power cuts with the smart plug | workstation | about 1 h |
| m10-kill | M10 | Frigate killed 10 times | gateway | about 20 min |
| m10-reboot | M10 | 10 gateway reboots | workstation | about 1 h |
| m11-full-disk | M11 | recording disk filled to its safe limit | gateway | about 15 min |
| m11-missing-disk | M11 | recording disk taken away and given back | gateway | about 5 min |
| m11-corrupt | M11 | one damaged segment, playback around it | gateway | about 5 min |
| m12-integrity | M12 | every segment recorded since the matrix began | gateway | about 15 min |
| whole window | M10-M12 | no recording gap outside the steps above | gateway | 1 min |

The two steps that may need someone on site (pulling camera cables, switching the power) come
first, so that person is needed for one block of about 1 h 20 min at the start of the run.

With 5 minutes between steps, a full run takes about 4 hours; `--quick` (one event of each kind)
about 1.5 hours.

How it works:

- Each step is the module's own test script, with the counts and limits from its page
  (docs/m10-recovery.md, docs/m11-faults.md, docs/m12-integrity.md) and its own report.
- The gateway steps run as transient systemd units on the gateway (`vivo-m26-<step>-<time>`), with
  their log in `reports/m26-<time>/`. A dropped SSH session cannot stop a step half-way (the
  missing-disk step switches the recording disk off); the matrix reconnects and follows the log.
- Before each step the gateway must be recording every camera, or the matrix stops there.
- The M12 scan covers the whole matrix, with each step's window marked as planned.
- The last check takes every gap in either stream of any camera from the start of the first step
  to the end of the last: each must fall inside a step's window. A gap outside them fails.
- Each step's window is added to `reports/planned-windows.txt` on the gateway.

What it needs:

- PoE control for the disconnect step: `POE_BACKEND=command` in `/etc/vivo/site.env` (unattended),
  or `manual`, and then someone on site unplugs each camera when the matrix asks.
- The smart plug for the power step: `--plug-off` and `--plug-on` commands, or someone switches the
  power when the matrix asks.
- The recording disk on LVM for the missing-disk step (as on the reference gateway). On a plain
  disk, unplug it by hand (docs/m11-faults.md) and skip the step.

```bash
tests/m26_matrix.py --host vivo-n150 --dry-run       # the plan and the preflight checks; changes nothing
tests/m26_matrix.py --host vivo-n150 --quick         # try the runner first: one event of each kind
tests/m26_matrix.py --host vivo-n150 \
    --plug-off "curl -s http://PLUG/relay/0?turn=off" --plug-on "curl -s http://PLUG/relay/0?turn=on"
tests/m26_matrix.py --host vivo-n150 --only m11-corrupt,m12-integrity
tests/m26_matrix.py --host vivo-n150 --skip m10-disconnect,m11-power   # without PoE control or smart plug
```

Recording stops many times during a run: post the planned window to the group first. The matrix
asks for a typed "yes" unless `--yes`.

Report: `reports/m26-matrix-<time>.md` on the workstation and on the gateway: the results table
(step, module, start, minutes, result, checks passed, the step's report, the failed checks), the
preflight, the gap log of the whole window, and the step windows. A `.json` next to it on the
workstation has the same data. The exit code is 0 when every step and the whole-window check pass.

## Handoff check

`tests/handoff_check.py` checks that the repository can be handed over:

- no settings or key file in the repository; no secret in any file (private keys, logins in URLs,
  password and token values); on the gateway with `--secrets /etc/vivo/secrets.env`, none of the
  real passwords appears in any file (the report names the setting, never the value)
- every image pinned by version and digest from `versions.env`, no `latest`, exact Docker versions
- the generated configs match `cameras.yaml`
- every module in scope has its page in `docs/` and its test
- no camera footage or pictures in the repository, no file over 5 MB
- the unit tests pass, and everything is committed

```bash
tests/handoff_check.py                                        # in a clone of the Vivo repository
sudo /opt/vivo-gateway/tests/handoff_check.py --secrets /etc/vivo/secrets.env   # on the gateway
```

## Done when

A Vivo engineer who did not build it deploys a gateway from docs/deploy.md alone, then runs
`tests/m26_matrix.py` and gets the same results as the reference run in docs/milestone-4-results.md.

## Walkthrough call (up to 1 hour)

| Minutes | Topic |
|---|---|
| 10 | How the gateway is built: go2rtc, Frigate, substream archiver, storage guard, health endpoint, systemd units, where files live (docs/m8-storage.md) |
| 10 | Deploying a gateway: docs/deploy.md, `setup.sh` steps, site and secrets files, adding cameras |
| 10 | Day-to-day: health endpoint and alerts, `vivo-disk`, logs, changing retention, maintenance switch |
| 15 | Tests: running the matrix and M25, reading the reports, what each limit means |
| 10 | Known limitations and what to do next (docs/known-limitations.md) |
| 5 | Questions |

## Not included

OTA updates and fleet rollout (M27, M28), dealer training, the field pilot.
