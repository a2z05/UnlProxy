# Brag plan — UnlProxy (TikTok, 9:16)

## Run
- Format: vertical 1080×1920, 30fps, 21.0s
- Tone: `default`, creative direction **late-night terminal precision** (unchanged)
- Output: `brag-output-tiktok-2026-10-21/`
- Music: the synthesised `unlproxy-bed.wav` from the landscape run — the bundled
  "Happy Beats" catalogue is still **not to be used** (standing user directive).
- Voice: off (no `--voice`).

## Why vertical is a re-author, not a crop
A 1920×1080 frame cropped to 9:16 loses both node cards and half the log. The
layout is rebuilt for a phone instead:
- the panel becomes **one column**: 2×2 stat grid, stacked action bar, three
  stacked node cards, event log underneath — the same `ui.html` CSS idioms at
  vertical proportions.
- the hook's IPv6 wraps onto **two lines at 124px** instead of one 74px line, so
  the address stays the biggest thing in frame.
- the terminal command breaks after the SOCKS5 port, so the URL gets its own line.
- the transport scene stacks name / transport / verdict instead of running them
  as one row.
- everything sits inside TikTok's safe area: no content in the top 120px (username
  + tabs) or the bottom 240px (caption + action rail).

## Storyboard (same story, re-timed)
| # | Scene | In–out | Content |
|---|---|---|---|
| 1 | Hook — the address escapes | 0.0–3.0 | `egress ip`, two-line IPv6, red `BANNED` stamp at 1.0s, per-char scramble from 1.5s, resolves 2.35s |
| 2 | The pool rotates itself | 3.0–8.0 | vertical control UI assembles, cursor clicks `rotate all` at 4.0s, three node cards swap addresses on beats 4.50 / 5.00 / 5.50, log lines push down |
| 3 | The proof | 8.0–11.5 | terminal types the real `curl -x socks5h://127.0.0.1:18090 …` trace, prints `ip=` / `warp=on` / `colo=FRA` |
| 4 | Why it works at all | 11.5–15.5 | `WireGuard · UDP 2408 · blackholed` struck, `MASQUE · TCP 443 · connects`, then the quote |
| 5 | Outro | 15.5–21.0 | `python3 warppool.py setup` → `2 answers, done` → `unlp` → lockup + repo |

Deliberate change vs. landscape: **+1.0s of end card**. The vertical lockup needs
the extra beat to read at thumbnail size, and TikTok benefits from a settled
final frame.

## Hook
Unchanged in meaning, bigger in type: the ban is a function of the egress
address, and the address moves. The stamp is the joke; the scramble is the proof.

## Audio cues (unchanged bed, unchanged grid)
120 BPM → beat every 0.5s. Strong cues: 3.0 (drop / UI), 8.0 (proof),
15.5 (outro lift), 18.4 (logo hit). The three card swaps land on 4.50 / 5.00 /
5.50. SFX are the same nine, re-timed to the vertical's event times.

## Copy (verbatim from the project, unchanged)
`egress ip`, `up`, `warp on`, `colo FRA`, `UnlProxy` / `egress control` /
`watching`, `nodes` `3/3`, `distinct ips`, `drained`, `avg age`, `run ensure`,
`rotate all`, `manage`, `event log`, `18090 rotated (reconnect) -> <ip>`,
`curl -x socks5h://127.0.0.1:18090 https://www.cloudflare.com/cdn-cgi/trace`,
`ip=`, `warp=on`, `colo=FRA`, `WireGuard` / `UDP 2408`, `MASQUE` / `TCP 443`,
`TCP 443 is what every censorship regime has to leave open.`,
`python3 warppool.py setup`, `2 answers, done`, `unlp`,
`no docker · no TUN · no admin · one Python file`, `github.com/a2z05/UnlProxy`.

No invented numbers, no invented quotes, no customer names, no secrets.
