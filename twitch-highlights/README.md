# twitch-highlights

Automatic highlight clips from Rachel's Twitch and YouTube streams (channel: Untumbled).

Code lives in this repo. Videos, audio and the per-stream cache live on the PC only
(see `.gitignore`).

## How it works

1. Download each VOD's audio (yt-dlp).
2. Transcribe it and score candidate moments.
3. Cache candidates and transcript per stream in `packages/<source-key>/`, so an
   interrupted run resumes instead of starting over.
4. Cut and render clips with ffmpeg.

## Commands

Short form (1080x1920, facecam over gameplay, burned-in captions):

    python highlights.py shorts candidates.json --pick 0,2,5 --max-clip 26

Long form (1920x1080 compilation, title card and date per stream, playthrough order):

    python highlights.py package --game "Hades 2" --match "hades,olympus,gods,fates" --per-vod 6

## Known gotchas

- YouTube 403 on every download: update yt-dlp (`pip install -U yt-dlp`).
- Black "starting soon" screens are rejected via `--blank-reject` (default 0.5).
- Facecam region is measured per layout (currently x=1479, y=309, 3px inset).

## Workflow

- PC session: anything that downloads, transcribes or renders. Commit and push code.
- Phone or cloud session: design, code changes, planning. Pull before PC work.

## Getting the code in (first push from the PC)

From `C:\Claude\twitch-highlights`:

    git init
    git remote add origin https://github.com/JZ-sys88/Claude-Repository.git
    git fetch origin ccr-9630db9a-vccy26
    git checkout -b ccr-9630db9a-vccy26 origin/ccr-9630db9a-vccy26
    # copy highlights.py and th/ into twitch-highlights/ here, then:
    git add . && git commit -m "Add highlights tool" && git push -u origin ccr-9630db9a-vccy26
