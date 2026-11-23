# Block C, milestone "M16, M17": results

Status: **both pass on the Vivo reference gateway (vivo-n150) over its real recordings** (Nov 22,
2026), measured on the gateway itself, which is what a viewer on the building network gets. Over the
VPN from the workstation in Korea (about 340 ms round trip) playback starts in about 3 s; see M16.

Where it was tested:
- **Lab** (Nov 8): the recorder's own lab with Frigate 0.18.0 and go2rtc 1.9.14 (the gateway's pinned
  versions) and four fake cameras sending what Vivo's test cameras send (H.265 main stream).
- **Gateway** (Nov 22): camera cam01 over the last 24 hours of its recordings. The footage never left
  the gateway: `tests/m16_probe.py` and `tests/m17_clips.py --check-on` decode on the gateway with the
  ffmpeg already in the Frigate image, in a container that is removed afterwards.
- **Mock** (Nov 22): the same M16 and M17 scripts against the mock recorder on the workstation.

The reports are in [test-reports/block-c](test-reports/block-c/README.md).

## M16: Historical playback and seeking

| Done when (contract section 13) | Lab (Nov 8) | Gateway (Nov 22) | Evidence |
|---|---|---|---|
| First picture within 3 s | Pass: median 0.37 s over 5 starts | **Pass on the building network**: median 0.94 s over 5 starts (1.00, 0.88, 0.94, 1.19, 0.94 s), from asking for the playback to the first decoded frame; creating the playback took 0.30 s of it. Over the VPN from Korea: median 3.07 s (see below) | `m16-probe-gateway-20261122-151825`, `m16-playback-gateway-vpn-20261122T151938` |
| A seek anywhere in the last 24 hours plays within 2 s | Pass over the lab's 14 minutes: median 0.37 s, slowest 0.98 s | **Pass on the building network**: 20 of 20 random points across 24 hours, median 0.32 s, slowest 0.40 s. Over the VPN: median 1.61 s, 14 of 20 within 2 s, slowest 2.62 s | the same two reports |
| Gaps are skipped; an empty range is `404 no_recording` | Pass | **Pass** against Frigate | `m18-contract-20261123-123627`, `tests/unit/test_playback.py` |
| The playback link expires with its grant; a changed link is refused | Pass | **Pass** | `m14-check-gateway-20261122-150122`, unit tests |

How the gateway figures are measured: `tests/m16_probe.py` runs on the gateway and plays the
playback the API returns, through the API's own address. A start is timed from the request to the
first decoded frame. A seek is timed the way a player does it once it has the playlist: fetch the
piece that holds the point and its header, and decode its first frame.

**Over the VPN.** A real browser (hls.js in headless Chrome) on the workstation in Korea needed a
median of 3.07 s to the first picture and 1.61 s per seek. The time goes to round trips across the
Pacific: a start is several requests one after another (the playback, the playlist, the header, the
first pieces), and the report shows each step taking several hundred milliseconds. On the building
network, or close to the site, the figures are near the gateway's. The contract does not say which
network the M16 targets are for; this page measures them on the building network.

## M17: Clip export and download

| Done when (contract section 13) | Lab (Nov 8) | Gateway (Nov 22) | Evidence |
|---|---|---|---|
| A 5-minute clip is ready within 30 s | Pass: 4.8 s | **Pass**: ready in 2.3 s, 11,994,463 bytes; a whole MP4 of 303.6 s, H.265 and AAC, every frame decodes (checked on the gateway) | `m17-clips-gateway-20261122-152051` |
| Clips over 30 minutes are refused | Pass (413) | **Pass** (413) | `m17-clips-gateway-20261122-152051` |
| At most 2 are prepared at once and the rest queue | Pass: two running, one queued | **Not shown on the gateway**: each clip took about 2 s, so all three were ready at the first status check and none was seen waiting. The limit is shown by the lab run and by a unit test with five requests at the same moment | `m17-clips-lab-20261108-062240`, `tests/unit/test_clips.py` |
| Finished clips are deleted after 1 hour | Pass with the keep time set to 60 s | **Pass**: the clips left by the run were gone by the next morning (the clip folder was empty on Nov 23 at 05:11 UTC); the keep time is 1 hour | lab report, gateway check of Nov 23 |
| Only the requester can see or download a clip | Pass | **Pass** | `m14-check-gateway-20261122-150122` |

The clip is 3.6 s longer than asked because Frigate cuts on whole recording segments (findings F7).

## Against the mock recorder (Nov 22)

The same scripts passed through the mock recorder on the workstation: M16 2 of 2
(`m16-playback-mock-mac-20261122T141617`) and M17 9 of 9 (`m17-clips-mock-mac-20261122-142353`). The
first M17 run there found that the mock's clips kept their stored times (findings F9); fixed before
the gateway run.

## Findings

- **F8: Frigate refuses one playlist of more than about 1,100 pieces**, so a playback over more than
  about 3 hours failed on the gateway (`502 upstream_unavailable`). Fixed in the Frigate adapter:
  one part per clock hour, joined into one playlist. The lab had only 14 minutes of footage, so it
  could not show this.
- **F9: the mock recorder's clips kept the stored pieces' times**, so a 5-minute clip read as 1,790 s.
  Fixed: clips start at 0. The contract suite now checks a clip taken 7 minutes into the mock's footage.
- **F10: remote playback time is round trips** (above).

Details in [findings.md](findings.md).

## Open points

- A browser run on the building network, for example on a laptop on site, would show the gateway
  figures in a real browser too.
