# M16 playback and seeking: gateway-vpn

Run 2026-11-22T15:13:14Z. API https://192.168.30.200:8443, camera cam01. Playback of 2026-11-21T15:12:05Z to 2026-11-22T15:12:05Z
(1440 minutes in the playlist; gaps are skipped). hls.js in headless Chrome.
First picture is measured from the playback request to the first frame shown.

Result: **0 passed, 2 failed**

| Check | Result | Target | |
|---|---|---|---|
| First picture, median of 5 starts | 3354 ms (slowest 5259 ms) | 3 s or less | FAIL |
| 20 random seeks | median 1642.5 ms, slowest 2500 ms | each 2 s or less | FAIL |

| Start | First picture | Request | Playlist | First piece | Buffered |
|---|---|---|---|---|---|
| 1 | 5259 ms | 1273 ms | 2636 ms | 4846 ms | 4882 ms |
| 2 | 3354 ms | 705 ms | 2150 ms | 3299 ms | 3327 ms |
| 3 | 3290 ms | 829 ms | 2016 ms | 3249 ms | 3276 ms |
| 4 | 3163 ms | 721 ms | 1689 ms | 3111 ms | 3138 ms |
| 5 | 3379 ms | 1367 ms | 2121 ms | 3327 ms | 3354 ms |

| Seek to (s into the playlist) | Next frame after |
|---|---|
| 18054 | 1283 ms |
| 76603 | 2068 ms |
| 85398 | 2400 ms |
| 1303 | 1532 ms |
| 45973 | 2500 ms |
| 86016 | 2216 ms |
| 78163 | 1433 ms |
| 5371 | 1818 ms |
| 476 | 964 ms |
| 83264 | 1750 ms |
| 47636 | 1384 ms |
| 84858 | 1468 ms |
| 39271 | 1465 ms |
| 62862 | 2117 ms |
| 82489 | 1535 ms |
| 17559 | 1500 ms |
| 22497 | 2299 ms |
| 72452 | 2233 ms |
| 68621 | 1865 ms |
| 30906 | 1233 ms |
