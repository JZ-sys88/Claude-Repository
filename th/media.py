"""Download, thumbnail and render. Wraps yt-dlp + ffmpeg."""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

YTDLP = [sys.executable, "-m", "yt_dlp"]


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def have(tool):
    return shutil.which(tool) is not None


def vod_url(video_id):
    return f"https://www.twitch.tv/videos/{str(video_id).lstrip('v')}"


def stream_url(video_id, quality="best"):
    """Direct media URL for a VOD -- lets ffmpeg seek without downloading."""
    r = _run(YTDLP + ["-f", quality, "-g", vod_url(video_id)])
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp -g failed: {r.stderr[-400:]}")
    urls = [l for l in r.stdout.splitlines() if l.startswith("http")]
    if not urls:
        raise RuntimeError("yt-dlp returned no stream URL")
    return urls[0]


def thumbnail(src_url, t, out_path, width=480):
    """Grab one frame at `t` seconds. Input seeking = fast even on long VODs."""
    r = _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(int(t)), "-i", src_url,
        "-frames:v", "1", "-vf", f"scale={width}:-2",
        "-q:v", "4", out_path,
    ])
    return r.returncode == 0 and os.path.exists(out_path)


# YouTube serves 1080p as video-only DASH, so it needs an explicit merge;
# Twitch's HLS renditions are already muxed.
DEFAULT_QUALITY = {
    "twitch": "best",
    "youtube": "bestvideo[height<=1080]+bestaudio/best",
}
AUDIO_QUALITY = {
    "twitch": "Audio_Only/bestaudio",
    "youtube": "bestaudio",
}


def _url_of(target):
    """Accept a Source, a Twitch VOD id, or a URL."""
    if hasattr(target, "url"):
        return target.url, getattr(target, "kind", "twitch")
    s = str(target)
    if s.startswith("http"):
        return s, ("youtube" if "youtu" in s else "twitch")
    return vod_url(s), "twitch"


def download_section(target, start, duration, out_path, quality=None):
    """Fetch just one time range of a VOD (Twitch id, URL, or Source)."""
    url, kind = _url_of(target)
    quality = quality or DEFAULT_QUALITY.get(kind, "best")
    end = start + duration
    cmd = YTDLP + [
        "-f", quality,
        "--download-sections", f"*{int(start)}-{int(end)}",
        "--force-keyframes-at-cuts",
        "-o", out_path,
        url,
    ]
    if kind == "youtube":
        # YouTube needs a JS runtime for signature deciphering, and merging
        # DASH video+audio otherwise lands in .mkv regardless of the -o suffix.
        cmd = YTDLP + ["--js-runtimes", "node,deno"] + cmd[len(YTDLP):]
        cmd += ["--merge-output-format", "mp4"]
    r = _run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"section download failed: {r.stderr[-400:]}")
    return _settle(out_path)


# --- preflight -----------------------------------------------------------
# She does not record locally, so YouTube/Twitch extraction is the only source
# there will ever be -- a stale yt-dlp has twice 403'd every download and taken
# the whole pipeline down. Prove a real fetch works before committing to an
# hour-long analyse-and-render run.
YTDLP_MIN = "2026.08.19"
STALE_DAYS = 30


def ytdlp_version():
    r = _run(YTDLP + ["--version"])
    return (r.stdout or "").strip().splitlines()[-1] if r.stdout else ""


def _version_date(v):
    try:
        return dt.datetime.strptime(v[:10], "%Y.%m.%d").date()
    except (ValueError, TypeError):
        return None


def preflight(target, probe_seconds=2.0, quality=None):
    """Check yt-dlp's version and prove it can still fetch media from `target`.

    Returns a dict with ok/version/age_days/stale/probe. A version string alone
    is not enough: both outages had a yt-dlp that ran fine and reported a
    version, then returned 403 for every media URL. Only a real byte fetch
    distinguishes the two.
    """
    v = ytdlp_version()
    d = _version_date(v)
    age = (dt.date.today() - d).days if d else None
    info = {
        "version": v,
        "age_days": age,
        "stale": bool(age is not None and age > STALE_DAYS),
        "below_min": bool(d and _version_date(YTDLP_MIN) and d < _version_date(YTDLP_MIN)),
    }

    tmp = tempfile.mkdtemp(prefix="th_preflight_")
    out = os.path.join(tmp, "probe.mp4")
    try:
        download_section(target, 0, probe_seconds, out, quality=quality)
        got = os.path.getsize(out) if os.path.exists(out) else 0
        info["probe_bytes"] = got
        info["ok"] = got > 0
        if not info["ok"]:
            info["error"] = "probe produced an empty file"
    except Exception as e:
        info["ok"] = False
        info["error"] = _first_error_line(str(e))
        info["kind"], info["advice"] = _classify(str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return info


def _first_error_line(text):
    """yt-dlp prefixes real failures with ERROR:, after unrelated warnings."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("ERROR:"):
            return line[:300]
    return (text or "")[-200:].strip()


# A missing VOD and a broken extractor both fail the probe, but the fixes are
# opposite: one needs a different source, the other needs a yt-dlp update.
# Telling the user to upgrade when the video is simply gone wastes their time.
_PATTERNS = [
    ("gone", ("does not exist", "video is unavailable", "has been removed",
              "private video", "members-only", "video unavailable"),
     "The video is gone, private or members-only -- not a yt-dlp problem. "
     "If it was a Twitch id, retention has expired (7-60 days); use the "
     "YouTube archive instead."),
    ("blocked", ("403", "forbidden", "sign in to confirm", "bot"),
     "Extraction is broken or rate-limited: pip install -U yt-dlp"),
    ("format", ("requested format is not available", "no video formats"),
     "Format resolution failed -- usually a stale yt-dlp or a missing JS "
     "runtime: pip install -U yt-dlp, and ensure node is on PATH"),
]


def _classify(text):
    low = (text or "").lower()
    for kind, needles, advice in _PATTERNS:
        if any(n in low for n in needles):
            return kind, advice
    return "unknown", "pip install -U yt-dlp, then retry"


def preflight_report(info, stream=None):
    """Print a human summary; return True if it is safe to continue."""
    out = stream or sys.stdout
    v, age = info.get("version", "?"), info.get("age_days")
    agestr = f"{age}d old" if age is not None else "age unknown"
    print(f"[preflight] yt-dlp {v} ({agestr})", file=out)
    if info.get("below_min"):
        print(f"  WARNING: below known-good {YTDLP_MIN}", file=out)
    elif info.get("stale"):
        print(f"  WARNING: older than {STALE_DAYS}d -- YouTube breaks often, "
              f"consider: pip install -U yt-dlp", file=out)
    if info.get("ok"):
        print(f"  media fetch OK ({info.get('probe_bytes', 0)/1024:.0f} KB probe)",
              file=out)
        return True
    print(f"  MEDIA FETCH FAILED [{info.get('kind', 'unknown')}]: "
          f"{info.get('error', 'unknown')}", file=out)
    print(f"  -> {info.get('advice', 'pip install -U yt-dlp')}", file=out)
    return False


def _settle(out_path):
    """yt-dlp may append its own container suffix; find what it actually wrote."""
    if os.path.exists(out_path):
        return out_path
    base = os.path.basename(out_path)
    d = os.path.dirname(os.path.abspath(out_path)) or "."
    for name in os.listdir(d):
        if name.startswith(base) and name != base:
            actual = os.path.join(d, name)
            try:
                os.replace(actual, out_path)
                return out_path
            except OSError:
                return actual
    raise RuntimeError(f"download produced no file for {out_path}")


def download_audio(target, duration, out_path):
    """Full audio track. Never sectioned -- we want all of it, and yt-dlp's
    native downloader is more reliable than handing ranges to ffmpeg."""
    url, kind = _url_of(target)
    cmd = YTDLP + ["-f", AUDIO_QUALITY.get(kind, "bestaudio"), "-o", out_path]
    if kind == "youtube":
        cmd += ["--js-runtimes", "node,deno"]
    r = _run(cmd + [url])
    if r.returncode != 0:
        raise RuntimeError(f"audio download failed: {r.stderr[-400:]}")
    return _settle(out_path)


def thumbnail_at(target, t, out_path, width=480):
    """One frame at `t`, by whichever route the host allows.

    Twitch hands out a media URL ffmpeg can seek. YouTube's googlevideo URLs
    are client-bound and 403 anything but yt-dlp, so there we pull a two-second
    section first and take the frame from that.
    """
    _url, kind = _url_of(target)
    if kind == "twitch":
        ident = target.id if hasattr(target, "id") else str(target)
        return thumbnail(stream_url(ident), t, out_path, width=width)

    tmp = tempfile.mkdtemp(prefix="th_yt_thumb_")
    clip = os.path.join(tmp, "s.mp4")
    try:
        download_section(target, max(t, 0), 2.0, clip,
                         quality="bestvideo[height<=720]+bestaudio/best")
        return thumbnail(clip, 0.5, out_path, width=width)
    except Exception:
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def download_clip(clip_url, out_path):
    r = _run(YTDLP + ["-o", out_path, clip_url])
    if r.returncode != 0:
        raise RuntimeError(f"clip download failed: {r.stderr[-400:]}")
    return out_path


def normalise(src, dst, width=1920, height=1080, fps=60, fade=0.4):
    """Re-encode to uniform params so the concat demuxer can join cleanly."""
    dur = duration_of(src)
    fo = max(dur - fade, 0)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={fps},format=yuv420p,"
        f"fade=t=in:st=0:d={fade},fade=t=out:st={fo:.2f}:d={fade}"
    )
    af = f"afade=t=in:st=0:d={fade},afade=t=out:st={fo:.2f}:d={fade},loudnorm=I=-16:TP=-1.5:LRA=11"
    r = _run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", src,
        "-vf", vf, "-af", af,
        "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        dst,
    ])
    if r.returncode != 0:
        raise RuntimeError(f"normalise failed: {r.stderr[-400:]}")
    return dst


def duration_of(path):
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "json", path])
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


BLACK_RE = re.compile(r"black_duration:\s*([\d.]+)")


def blank_fraction(path, gameplay=(0, 0, 1479, 1080), pix_th=0.10):
    """Fraction of a clip whose gameplay region is essentially black.

    Reaction moments early in a stream often land on the "starting soon" card:
    she is talking (speech tier fires) over a black screen, so the clip is
    audibly good and visually dead. Measured on the gameplay region only --
    the facecam and chat overlay are lit even when the game is not.
    """
    dur = duration_of(path)
    if dur <= 0:
        return 0.0
    x, y, w, h = gameplay
    r = _run(["ffmpeg", "-nostats", "-i", path,
              "-vf", f"crop={w}:{h}:{x}:{y},blackdetect=d=0.3:pix_th={pix_th}",
              "-f", "null", "-"])
    total = sum(float(m) for m in BLACK_RE.findall(r.stderr or ""))
    return min(total / dur, 1.0)


FONT_BOLD = "C:/Windows/Fonts/arialbd.ttf"


def _dt_escape(s):
    """drawtext's own escaping: backslash, colon and single quote."""
    return (str(s).replace("\\", "\\\\").replace(":", "\\:")
            .replace("'", "\u2019").replace("%", "\\%"))


def title_card(text, out_path, subtitle=None, duration=2.5, width=1920,
               height=1080, fps=60, bg="black", font=FONT_BOLD):
    """A titled black card with silent audio, muxed like a normal clip.

    Silent audio matters: the concat demuxer needs every part to carry the same
    stream layout, so a video-only card would desync or drop the audio track.
    """
    fontfile = font.replace("\\", "/").replace(":", "\\:")
    draw = (f"drawtext=fontfile='{fontfile}':text='{_dt_escape(text)}':"
            f"fontcolor=white:fontsize={int(height*0.075)}:"
            f"x=(w-text_w)/2:y=(h-text_h)/2-{int(height*0.04)}")
    if subtitle:
        draw += (f",drawtext=fontfile='{fontfile}':text='{_dt_escape(subtitle)}':"
                 f"fontcolor=0xB0B0B0:fontsize={int(height*0.038)}:"
                 f"x=(w-text_w)/2:y=(h-text_h)/2+{int(height*0.06)}")
    r = _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c={bg}:s={width}x{height}:r={fps}:d={duration}",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-vf", draw, "-t", str(duration),
        "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-shortest", out_path,
    ])
    if r.returncode != 0:
        raise RuntimeError(f"title card failed: {r.stderr[-400:]}")
    return out_path


def concat(parts, out_path):
    """Join pre-normalised parts losslessly via the concat demuxer."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        for p in parts:
            safe = os.path.abspath(p).replace("\\", "/").replace("'", r"'\''")
            f.write(f"file '{safe}'\n")
        listfile = f.name
    try:
        r = _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat",
                  "-safe", "0", "-i", listfile, "-c", "copy", out_path])
        if r.returncode != 0:
            raise RuntimeError(f"concat failed: {r.stderr[-400:]}")
    finally:
        os.unlink(listfile)
    return out_path


# -- tier 3: audio energy ------------------------------------------------
# ffmpeg >=7 inserts "TARGET:-23 LUFS" between t: and M:, so skip anything
# between them rather than requiring adjacency.
EBUR = re.compile(r"\bt:\s*([\d.]+).*?\bM:\s*(-?(?:[\d.]+|inf))")


def loudness_profile(src, voice_band=False, hp=300, lp=3500):
    """[(t, momentary_LUFS)] via ffmpeg's ebur128 filter.

    voice_band bandpasses to roughly the vocal range first. She does not record
    separate audio tracks, so game audio and her mic are permanently mixed and a
    loud explosion reads like a scream. Most game rumble is below ~200 Hz while
    vocal fundamentals and their lower harmonics sit above it, so cutting the
    low end biases the loudness measurement toward her voice.
    """
    chain = "ebur128"
    if voice_band:
        chain = f"highpass=f={hp},lowpass=f={lp},ebur128"
    r = _run(["ffmpeg", "-nostats", "-i", src, "-filter_complex", chain,
              "-f", "null", "-"])
    out = []
    for line in (r.stderr or "").splitlines():
        m = EBUR.search(line)
        if not m:
            continue
        try:
            out.append((float(m.group(1)), float(m.group(2))))
        except ValueError:
            continue  # '-inf' on silence
    return out


def onset_candidates(profile, top=30, dedupe=45.0, clip_len=25.0,
                     rise_lu=7.0, pre=2.0, post=1.0, floor_pct=60,
                     quiet_pct=25):
    """Sudden loudness *rises* -- screams, gasps, shouts.

    A word-based scorer cannot see nonverbal vocalisation, and a level-based
    one cannot either: a scream is not louder than sustained excited talking,
    it is louder *faster*. This measures the jump from the preceding `pre`
    seconds to the following `post` seconds, which is what makes a scream a
    scream. Validated against a viewer-made clip titled "ah ah wahhh" that both
    other tiers missed.
    """
    from .model import candidate
    if len(profile) < 10:
        return [], {}

    dt = max(profile[1][0] - profile[0][0], 0.01)
    npre, npost = max(int(pre / dt), 1), max(int(post / dt), 1)
    vals = sorted(v for _t, v in profile)
    floor = vals[min(int(len(vals) * floor_pct / 100), len(vals) - 1)]
    quiet = vals[min(int(len(vals) * quiet_pct / 100), len(vals) - 1)]

    scored = []
    for i in range(npre, len(profile) - npost):
        before = profile[i - npre:i]
        after = profile[i:i + npost]
        b = sum(v for _t, v in before) / len(before)
        a_ = max(v for _t, v in after)
        if a_ < floor:            # a rise out of silence into quiet talking
            continue              # is not a reaction
        # ffmpeg reports digital silence as -120.7 LUFS, so an unclamped rise
        # out of a stream-start gap scores ~130 LU and buries every real
        # reaction. Clamp rather than skip: a horror jump-scare *is* a rise out
        # of near-silence, so excluding quiet lead-ins throws away the target.
        b = max(b, quiet)
        scored.append((a_ - b, profile[i][0], a_))
    scored.sort(reverse=True)

    picked, out = [], []
    for rise, t, peak in scored:
        if rise < rise_lu:
            break
        if any(abs(t - p) < dedupe for p in picked):
            continue
        picked.append(t)
        out.append(candidate(
            source="onset", start=max(t - 2.0, 0), duration=clip_len,
            score=rise, title=f"onset +{rise:.1f}LU",
            detail={"rise_lu": round(rise, 1), "peak_lufs": round(peak, 1)},
        ))
        if len(out) >= top:
            break
    return out, {"floor_lufs": round(floor, 1), "onsets": len(out)}


def audio_candidates(profile, top=30, dedupe=90, clip_len=25.0, window=6.0,
                     baseline_pct=85, min_gain=2.5):
    """Sustained-loud windows = streamer reacting.

    The baseline is a high percentile, NOT the median. For a talking stream the
    median sits in silence/game-ambience, so anchoring there just detects
    "someone is speaking" -- on a test VOD that matched 13.7% of the runtime.
    Anchoring at ~p85 puts the baseline at normal speech level, so a candidate
    has to clear her own talking voice to register.
    """
    from .model import candidate
    if not profile:
        return [], {}
    vals = sorted(v for _t, v in profile)
    idx = min(int(len(vals) * baseline_pct / 100), len(vals) - 1)
    median = vals[idx]

    step = max(int(window / max(profile[1][0] - profile[0][0], 0.05)), 1)
    scored = []
    for i in range(0, len(profile) - step):
        chunk = [v for _t, v in profile[i:i + step]]
        scored.append((sum(chunk) / len(chunk), profile[i][0]))
    scored.sort(reverse=True)

    picked, out = [], []
    for mean_lufs, t in scored:
        gain = mean_lufs - median
        if gain < min_gain:       # must clear her own speaking level
            break
        if any(abs(t - p) < dedupe for p in picked):
            continue
        picked.append(t)
        out.append(candidate(
            source="audio", start=t, duration=clip_len, score=gain,
            title=f"audio +{gain:.1f}LU",
            detail={"lufs": round(mean_lufs, 1), "gain_lu": round(gain, 1)},
        ))
        if len(out) >= top:
            break
    return out, {"baseline_lufs": round(median, 1), "baseline_pct": baseline_pct,
                 "points": len(profile)}
