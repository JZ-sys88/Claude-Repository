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
