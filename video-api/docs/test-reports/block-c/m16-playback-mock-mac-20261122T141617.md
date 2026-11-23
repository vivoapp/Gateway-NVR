# M16 playback and seeking: mock-mac

Run 2026-11-22T14:16:17Z. API https://127.0.0.1:8443, camera cam01. Playback of 2026-11-22T13:45:46Z to 2026-11-22T14:15:46Z
(19 minutes in the playlist; gaps are skipped). hls.js in headless Chrome.
First picture is measured from the playback request to the first frame shown.

Result: **2 passed, 0 failed**

| Check | Result | Target | |
|---|---|---|---|
| First picture, median of 5 starts | 71 ms (slowest 1597 ms) | 3 s or less | PASS |
| 20 random seeks | median 49.5 ms, slowest 384 ms | each 2 s or less | PASS |

| Start | First picture | Request | Playlist | First piece | Buffered |
|---|---|---|---|---|---|
| 1 | 1597 ms | 71 ms | 948 ms | 1066 ms | 1105 ms |
| 2 | 147 ms | 7 ms | 13 ms | 22 ms | 48 ms |
| 3 | 66 ms | 9 ms | 16 ms | 25 ms | 53 ms |
| 4 | 69 ms | 9 ms | 16 ms | 26 ms | 55 ms |
| 5 | 71 ms | 7 ms | 13 ms | 24 ms | 54 ms |

| Seek to (s into the playlist) | Next frame after |
|---|---|
| 852 | 84 ms |
| 226 | 384 ms |
| 573 | 66 ms |
| 1018 | 33 ms |
| 480 | 101 ms |
| 19 | 32 ms |
| 122 | 67 ms |
| 921 | 33 ms |
| 854 | 17 ms |
| 305 | 50 ms |
| 561 | 49 ms |
| 1100 | 33 ms |
| 1007 | 34 ms |
| 43 | 51 ms |
| 19 | 33 ms |
| 186 | 51 ms |
| 581 | 16 ms |
| 1051 | 34 ms |
| 896 | 51 ms |
| 507 | 66 ms |
