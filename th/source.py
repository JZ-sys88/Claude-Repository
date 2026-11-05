"""Unify Twitch VODs and YouTube uploads behind one object.

Twitch VODs expire (7-60 days); the same streams live on YouTube indefinitely.
The tiers that actually work here -- speech and onset -- read audio, so they do
not care which host the audio came from. This makes the back catalogue usable.

The one real difference is thumbnails: Twitch hands out a media URL ffmpeg can
seek directly, while YouTube's googlevideo URLs are client-bound and return 403
to anything but yt-dlp. See media.thumbnail_at().
"""
import json
import re
import subprocess
import sys

YTDLP = [sys.executable, "-m", "yt_dlp"]

TWITCH_ID = re.compile(r"^v?(\d{6,})$")
TWITCH_URL = re.compile(r"twitch\.tv/videos/(\d+)")
YT_URL = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/live/)([A-Za-z0-9_-]{11})")
YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class Source:
    def __init__(self, kind, ident, url):
        self.kind = kind          # "twitch" | "youtube"
        self.id = ident
        self.url = url
        self.title = None
        self.duration = None
        self.date = None          # YYYYMMDD

    @property
    def key(self):
        return f"{self.kind}-{self.id}"

    def __repr__(self):
        return f"<Source {self.kind}:{self.id} {self.title!r}>"


def resolve(spec):
    """Accept a Twitch VOD id/URL or a YouTube id/URL."""
    spec = str(spec).strip()

    m = TWITCH_URL.search(spec)
    if m:
        return Source("twitch", m.group(1), f"https://www.twitch.tv/videos/{m.group(1)}")
    m = YT_URL.search(spec)
    if m:
        return Source("youtube", m.group(1), f"https://www.youtube.com/watch?v={m.group(1)}")
    m = TWITCH_ID.match(spec)
    if m:
        return Source("twitch", m.group(1), f"https://www.twitch.tv/videos/{m.group(1)}")
    if YT_ID.match(spec):
        return Source("youtube", spec, f"https://www.youtube.com/watch?v={spec}")
    raise ValueError(f"Unrecognised source: {spec!r}")


def fetch_meta(source, timeout=120):
    """Fill title/duration/date. One yt-dlp metadata call, no download."""
    r = subprocess.run(
        YTDLP + ["--skip-download", "--dump-single-json", source.url],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        return source
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return source
    source.title = d.get("title")
    source.duration = d.get("duration")
    source.date = d.get("upload_date")
    return source


def channel_streams(handle, limit=200, tab="streams"):
    """List a YouTube channel's archived streams, newest first."""
    handle = handle.lstrip("@")
    url = f"https://www.youtube.com/@{handle}/{tab}"
    r = subprocess.run(
        YTDLP + ["--flat-playlist", "--playlist-end", str(limit),
                 "--print", "%(id)s\t%(duration)s\t%(title)s", url],
        capture_output=True, text=True,
    )
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        vid, dur, title = parts[0], parts[1], "\t".join(parts[2:])
        s = Source("youtube", vid, f"https://www.youtube.com/watch?v={vid}")
        s.title = title
        try:
            s.duration = float(dur)
        except (TypeError, ValueError):
            s.duration = None
        out.append(s)
    return out


def group_by_game(sources, games):
    """Bucket sources by matching keywords against their titles.

    `games` maps a display name to a list of title keywords, e.g.
    {"Hades 2": ["hades"], "Fields of Mistria": ["mistria"]}.
    Stream titles are the only reliable game signal -- Twitch's category is not
    exposed per-VOD via Helix, and YouTube has no category at all.
    """
    buckets = {name: [] for name in games}
    unmatched = []
    for s in sources:
        title = (s.title or "").lower()
        hit = None
        for name, keys in games.items():
            if any(k.lower() in title for k in keys):
                hit = name
                break
        if hit:
            buckets[hit].append(s)
        else:
            unmatched.append(s)
    return buckets, unmatched
