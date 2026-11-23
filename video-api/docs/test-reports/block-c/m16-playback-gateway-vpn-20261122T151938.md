# M16 playback and seeking: gateway-vpn

Run 2026-11-22T15:19:38Z. API https://192.168.30.200:8443, camera cam01. Playback of 2026-11-21T15:18:32Z to 2026-11-22T15:18:32Z
(1440 minutes in the playlist; gaps are skipped). hls.js in headless Chrome.
First picture is measured from the playback request to the first frame shown.

Result: **0 passed, 2 failed**

| Check | Result | Target | |
|---|---|---|---|
| First picture, median of 5 starts | 3072 ms (slowest 4696 ms) | 3 s or less | FAIL |
| 20 random seeks | median 1607 ms, slowest 2618 ms | each 2 s or less | FAIL |

| Start | First picture | Request | Playlist | First piece | Buffered |
|---|---|---|---|---|---|
| 1 | 4696 ms | 1280 ms | 1874 ms | 4287 ms | 4322 ms |
| 2 | 2930 ms | 739 ms | 1484 ms | 2883 ms | 2912 ms |
| 3 | 3417 ms | 786 ms | 1509 ms | 3363 ms | 3390 ms |
| 4 | 3072 ms | 699 ms | 1416 ms | 3017 ms | 3043 ms |
| 5 | 2983 ms | 685 ms | 1352 ms | 2928 ms | 2955 ms |

| Seek to (s into the playlist) | Next frame after |
|---|---|
| 40167 | 1767 ms |
| 62808 | 2618 ms |
| 66540 | 2283 ms |
| 13765 | 1367 ms |
| 58142 | 2266 ms |
| 40537 | 918 ms |
| 336 | 1048 ms |
| 19698 | 2601 ms |
| 14982 | 749 ms |
| 61743 | 1951 ms |
| 37006 | 1516 ms |
| 41267 | 2135 ms |
| 76851 | 1282 ms |
| 71517 | 1300 ms |
| 55174 | 1401 ms |
| 48042 | 1732 ms |
| 15342 | 818 ms |
| 58113 | 1698 ms |
| 50017 | 2350 ms |
| 51026 | 967 ms |
