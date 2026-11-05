# twitch-highlights

Finds highlight moments in a Twitch VOD and cuts them into a reel.

## Why Twitch and not YouTube

Measured on real VODs, not assumed:

| | YouTube (6h stream) | Twitch (11.8h VOD) |
|---|---|---|
| chat fetch | ~25 min, ~330 MB | **31 s, ~0 MB** |
| peak contrast | 3.8x baseline | **6.0x baseline** |
| coverage | most gaming VODs are Twitch re-uploads with **no chat at all** | native |

The speed gap is architectural. YouTube's chat replay only paginates forward, so
profiling means crawling the whole stream. Twitch's GQL `comments` field accepts
a `contentOffsetSeconds`, so you can *jump*. Each page returns a fixed ~59
comments, which makes the time span those comments cover an inverse density
measure — dense moments come back covering 3 seconds, dead air covering 3
minutes. One request per sample point profiles an 11-hour VOD in ~120 requests.

## Setup

```
pip install -r requirements.txt
```

Needs `ffmpeg` and `ffprobe` on PATH.

The chat and audio tiers work with no credentials. For the clips tier, register
a free app at <https://dev.twitch.tv/console/apps> and copy `.env.example` to
`.env`:

```
TWITCH_CLIENT_ID=xxxx
TWITCH_CLIENT_SECRET=xxxx
```

## Use

```bash
python highlights.py channel untumbled --days 7         # list VODs + clips
python highlights.py analyze <vod_id> --speech --no-chat  # small/quiet channel
python highlights.py analyze <vod_id> --clips           # big/chatty channel
python highlights.py review candidates.json --top 30    # eyeball them
python highlights.py render candidates.json --pick 0,3,7 -o reel.mp4
```

Working recipe for a horror/reaction stream, both audio tiers plus clips:

```bash
python highlights.py analyze <vod_id> --no-chat --speech --onset --clips
python highlights.py render candidates.json --top 8 --max-clip 26 -o reel.mp4
```

`--max-clip` matters: speech clusters can span a minute or more, and without a
cap eight clips ran to 6m21s. At 26s the same eight make a 3m29s reel.

For a quiet channel like Untumbled, `--speech --no-chat` is the working recipe.
`--no-chat` skips a probe that has nothing to find; the chat tier also
self-disables below 0.05 msg/s rather than emitting noise.

`review` writes a local HTML contact sheet with a thumbnail per candidate.
Tick the ones you want and it builds the exact `render` command to run.

## The three tiers

1. **clips** — viewer-created clips from Helix, ranked by view count. Human
   curation beats any heuristic: real people decided a moment was worth saving.
   `vod_offset` locates each clip in the VOD. Needs credentials.
2. **chat** — density spikes in VOD chat, weighted by hype emotes. No
   credentials. This is the workhorse.
3. **audio** — loudness above a high percentile of the VOD's own levels via
   ffmpeg `ebur128`. Confirming vote; weak on its own for a calm streamer.
4. **speech** — Whisper transcription, scored on reaction language. **This is
   the tier that works on small channels.** ~35x realtime on CPU (a 49-minute
   VOD transcribes in 85s), and it produces a timestamped transcript you can
   read and rank by hand or with an LLM when the keyword scorer misses
   something. Needs `faster-whisper`.

5. **onset** — sudden loudness *rises* (screams, gasps). A word scorer is blind
   to nonverbal reactions and a level scorer misses them too: a scream is not
   louder than sustained excited talking, it is louder *faster*.

`--audio`, `--speech` and `--onset` share a single audio download and a single
loudness pass. `--audio-file` reuses a downloaded track across runs and
`--reuse-transcript` skips re-transcribing.

## What the one piece of ground truth showed

Untumbled had exactly one viewer-made clip in 30 days ("ah ah wahhh", 5 views,
offset 7125s in VOD 2836021571) — a scream during a horror stream. Testing the
tiers against it:

- **speech missed it entirely.** Whisper transcribed the *game's* dialogue at
  that timestamp; the scream produced no reaction words. Word-based scoring is
  structurally blind to nonverbal vocalisation.
- **onset found it** at 01:58:39, 5s before the clip start — but ranked it
  **37th of 40**. There were ~36 louder onsets in the same 2.5-hour stream.

So onset detection buys *recall*, not *precision*: it surfaces a class of
moment nothing else can see, but loudness rise does not predict which one a
human finds clip-worthy. That is why it is weighted to populate the review
page rather than lead it, and why `review` exists at all. The tool proposes;
you dispose.

The strongest signal in practice is **agreement** — moments flagged by both
onset and speech ranked top of the merged list, e.g. "What? What? Are you
serious?" at 00:37:39.

Candidates from different tiers that land within `--merge-window` seconds are
merged, and a moment flagged independently by two tiers gets a score bonus.
Chat and audio *lag* the play by a few seconds, so those cuts get an 8-second
pre-roll (`--pre`) to catch the setup.

## Two output formats

**Short form** — one 9:16 file per moment, for TikTok / Reels / Shorts:

```bash
python highlights.py shorts candidates.json --pick 0,2,5 --max-clip 26
```

Her layout puts the facecam top-right with chat baked in underneath, so a
centre-crop would throw away her reaction and keep a strip of unreadable chat.
`shorts` crops the facecam out and stacks it above a centre-crop of the clean
gameplay area. Captions come from the Whisper transcript we already have, so
they cost nothing extra and match what she actually said.

The facecam box was measured by edge detection, not guessed — a dominant
vertical edge at x=1479 and horizontal at y=309 in 1920x1080, inset 3px to
avoid the overlay's own border. If she changes her layout, update
`LAYOUTS["untumbled"]` in `th/shorts.py`; `--layout plain` skips facecam
extraction entirely.

**Long form** — one game collated across many streams, with title cards:

```bash
python highlights.py package --game "Hades 2" --match "hades,olympus,gods,fates" \
    --per-vod 6 -o hades2.mp4
```

Per-VOD results cache in `packages/<source-key>/` (candidates + transcript,
~180KB per stream; the audio track is deleted after analysis), so a run that
dies partway resumes instead of re-downloading. `--analyse-only` does the slow
half alone.

### Blank-clip rejection

The speech tier has a specific failure mode: early in a stream she talks over a
"starting soon" screen, so the audio scores well while the video is dead. On the
first Hades 2 build, 1 of 24 clips was a fully black frame at 00:02:28.

`--blank-reject` (default 0.5) measures each downloaded clip with ffmpeg's
`blackdetect`, on the **gameplay region only** — the facecam and chat overlay
stay lit even when the game is not, so a whole-frame check would miss it. Clips
over the threshold are replaced from further down the ranking rather than
leaving a hole; the selection pool is 3x the needed count for that reason.

### Matching streams to a game

`--match` tests keywords against stream titles, because neither host exposes a
reliable per-VOD game field (Helix omits it; YouTube has no category). Untumbled
titles streams thematically rather than by game, so single keywords miss: of her
four Hades 2 streams, only one contains "hades" — the others are "BEFRIENDING
ALL THE GODS", "WE MEET THE FATES TODAY" and "time to explore mt olympus". Pass
several keywords, or `--ids` with explicit video ids when titles give you
nothing to grab.

## YouTube is the better source for back catalogue

Twitch VODs expire in 7-60 days; the same streams sit on her YouTube `/streams`
tab indefinitely — 135 of them versus the 8 still live on Twitch. The tiers that
work here read audio, so they do not care which host it came from.

Two hard requirements for YouTube:

- **yt-dlp >= 2026.08.19.** Older builds fall back to a player client whose
  media URLs return `403 Forbidden` for every download, sectioned or not. The
  2026.07 build failed on all six player clients tested; updating fixed it
  outright. Expect to update again — YouTube breaks extraction regularly.
- **Node.js on PATH**, for signature deciphering. Without a JS runtime yt-dlp
  cannot use the clients that produce working URLs.

YouTube also refuses direct ffmpeg access to its media URLs, so thumbnails there
go via a short section download rather than a seek (`media.thumbnail_at`).
Twitch still allows the fast path.

## Tuning is genre- and channel-specific

These tiers were built against a large competitive-FPS channel and do **not**
transfer unchanged to a small cozy/story-game channel. Measured:

| | summit1g (CS2) | Untumbled (Hades 2) |
|---|---|---|
| median chat rate | 0.353 msg/s | **0.006 msg/s** |
| messages, ~49 min | thousands | **18** |
| chat tier | works, 6.0x peaks | **unusable** |
| audio tier @ p85 | n/a | 1 candidate / 49 min |

Chat density is a statistical method; it needs volume. Below roughly 0.1 msg/s
there is nothing to do statistics on. Loudness likewise assumes a streamer who
gets audibly louder — a calm streamer defeats it. Check
`analyze`'s reported median rate before trusting the chat tier at all.

## Known limits

- **Sampling is approximate.** `--samples 100` and `--samples 240` will not
  return identical peaks; the step alignment changes what each probe sees. Use
  more samples for finer resolution. This is a profiler, not an exact index.
- **Chat spikes are not gameplay.** A store menu, a donation, or off-game drama
  spikes chat as hard as an ace. The `review` step exists because of this —
  don't skip it.
- **Twitch VODs expire** (7d standard, 14d affiliate, 60d partner). Highlights
  persist. Grab things while they exist.
- **`vod_offset` is null** on some older clips; those are reported separately
  and can only be downloaded directly, not located in the VOD.
- **GQL is undocumented.** `th/chat.py` sends a raw query rather than a
  persisted-query hash precisely because Twitch rotates those hashes — the
  hash form broke during development. If chat ever stops working, that file is
  where to look.
