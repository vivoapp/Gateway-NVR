# Block C, milestone "M16, M17": results

Status: **built and tested on the workstation lab; the gateway run follows when the milestone is
funded.** Lab: the recorder's own lab with Frigate 0.18.0 and go2rtc 1.9.14 (the gateway's pinned
versions) and four fake cameras sending what Vivo's test cameras send (H.265 main stream). The
gateway rows use the same scripts and the 7 days of footage on the reference N150.

## M16: Historical playback and seeking

| Done when (contract section 13) | Lab result (Nov 8, 2026) | Evidence | Gateway |
|---|---|---|---|
| First picture within 3 s | Pass: median 0.37 s over 5 starts (2.33, 0.52, 0.37, 0.25, 0.23 s; the first start in a fresh browser includes starting its H.265 decoder) | `m16-playback-lab-20261108T061936` | after funding |
| A seek anywhere in the last 24 hours plays within 2 s | Pass in the lab's range: 20 random seeks, median 0.37 s, slowest 0.98 s. The lab had 14 minutes of footage; the gateway run covers 24 hours | `m16-playback-lab-20261108T061936` | after funding |
| Gaps are skipped; an empty range is `404 no_recording` | Pass | contract suite, `tests/unit/test_playback.py` | after funding |
| The playback link expires with its grant; a changed link is refused | Pass | `m14-check-lab-20261108-061624`, unit tests | after funding |

## M17: Clip export and download

| Done when (contract section 13) | Lab result (Nov 8, 2026) | Evidence | Gateway |
|---|---|---|---|
| A 5-minute clip is ready within 30 s | Pass: ready in 4.8 s (16 MB); a whole MP4 of 303.9 s, H.265 and AAC, 0 decode errors | `m17-clips-lab-20261108-062240` | after funding |
| Clips over 30 minutes are refused | Pass (413) | `m17-clips-lab-20261108-062240` | after funding |
| At most 2 are prepared at once and the rest queue | Pass: two running, one queued, all three finished; also correct with five requests at the same moment (unit test) | `m17-clips-lab-20261108-062240`, `tests/unit/test_clips.py` | after funding |
| Finished clips are deleted after 1 hour | Pass with the keep time set to 60 s in the lab | `m17-clips-lab-20261108-062240` | after funding |
| Only the requester can see or download a clip | Pass | `m14-check-lab-20261108-061624` | after funding |

## Findings

- A 5-minute clip came out 3.9 s longer than asked: Frigate's `clip.mp4` starts and ends on whole
  recording segments. Noted as a known limitation; the contract promises the asked range is covered.
- The first playback in a fresh browser takes about 2 s longer than later ones (the browser starts
  its H.265 decoder); the median over 5 starts is the agreed measure.

## Open points

- The gateway run over 24 hours of real footage, after funding.
