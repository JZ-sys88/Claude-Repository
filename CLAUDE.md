# twitch-highlights

Highlight pipeline for Untumbled's Twitch/YouTube streams. Read README.md first — it documents the tiers, recipes and known limits.

## Workflow (desktop does the work, phone drives it)
1. `analyze` a VOD, then `review` to build the contact sheet and send it to Jason's phone (SendUserFile) so he can pick moments.
2. `render` (long form) / `shorts` (9:16) the picked moments.
3. Put finished files in `outbox/youtube/` (long form) or `outbox/tiktok/` (shorts) and send them to Jason's phone.

## Rules
- Never upload or post anywhere. Jason uploads to YouTube/TikTok himself.
- Never print or send `.env` / `.token_cache.json` contents.
- Keep yt-dlp current (README: >= 2026.08.19) — YouTube extraction breaks regularly.

## Repository

`github.com/JZ-sys88/Claude-Repository`, branch `ccr-9630db9a-vccy26`.
The git root is this folder, deliberately: `C:\Claude` also holds
job-applications, techbridge-business and web-clients, and **the repo is
public**, so keeping `.git` here makes committing those impossible.

Code and docs only. `.gitignore` excludes secrets, rendered media and every
piece of derived data about the streamer — transcripts, contact sheets (which
embed frames of her face as base64) and candidate lists. Do not add any of it,
and do not relax those rules; it all regenerates from the VODs.

Note `.token_cache.json` holds a live Twitch token and does **not** match a
`*.token` glob. It is listed by name. Keep it that way.

## What a cloud session can and cannot do

Can: read and edit code, review logic, adjust tuning, update docs.

Cannot: analyse or render. That needs ffmpeg, the faster-whisper model, the
cached `packages/` analysis and `.env` — none of which are in the repo. Clip
rendering also produces ~400MB files that would be stranded off-PC.

So: **code changes via the repo, all rendering on the PC via Remote Control.**
A cloud session that wants a render should ask the desktop session to run it.
