"""9:16 vertical renders for TikTok / Reels / Shorts.

Her stream layout puts the facecam top-right with a chat overlay baked in
underneath it, so a naive centre-crop throws away her reaction *and* keeps a
strip of unreadable chat. Instead the facecam is cropped out and stacked above
a centre-crop of the clean gameplay area.

Box coordinates were measured off her VOD by edge detection, not guessed:
a dominant vertical edge at x=1479 and horizontal edge at y=309 in 1920x1080.
"""
import os
import subprocess

# Regions in a 1920x1080 source frame.
LAYOUTS = {
    "untumbled": {
        # Inset ~3px from the detected edges (x=1479, y=309): those pixels are
        # the overlay's own border and show as a coloured strip once scaled up.
        "facecam": (1482, 2, 438, 305),    # x, y, w, h
        # gameplay area excluding the overlay column on the right
        "gameplay": (0, 0, 1479, 1080),
    },
    # whole frame, no facecam extraction
    "plain": {"facecam": None, "gameplay": (0, 0, 1920, 1080)},
}

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},2,2,70,70,{marginv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _even(n):
    n = int(round(n))
    return n if n % 2 == 0 else n + 1


def _ts(sec):
    sec = max(sec, 0)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def build_ass(segments, clip_start, clip_dur, path, W=1080, H=1920,
              font="Arial", size=62, outline=4, marginv=300, max_chars=42):
    """Subtitles for one clip, timed relative to the clip.

    Uses the transcript we already produced for the speech tier, so captions
    cost nothing extra and match what she actually said.
    """
    end = clip_start + clip_dur
    lines = []
    for s in segments:
        if s["end"] < clip_start or s["start"] > end:
            continue
        text = (s.get("text") or "").strip()
        if not text:
            continue
        # ASS uses {} for override tags and \N for newlines
        text = text.replace("{", "(").replace("}", ")").replace("\n", " ")
        if len(text) > max_chars:
            words, cur, out = text.split(), "", []
            for w in words:
                if len(cur) + len(w) + 1 > max_chars:
                    out.append(cur.strip())
                    cur = w
                else:
                    cur += " " + w
            out.append(cur.strip())
            text = "\\N".join(out[:3])
        a = max(s["start"] - clip_start, 0)
        b = min(s["end"] - clip_start, clip_dur)
        if b <= a:
            continue
        lines.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},Default,,0,0,0,,{text}")

    with open(path, "w", encoding="utf-8") as f:
        f.write(ASS_HEADER.format(W=W, H=H, font=font, size=size,
                                  outline=outline, marginv=marginv))
        f.write("\n".join(lines) + "\n")
    return path, len(lines)


def _ass_for_filter(path):
    """ffmpeg's filter parser needs the drive colon escaped on Windows."""
    p = os.path.abspath(path).replace("\\", "/")
    return p.replace(":", "\\:")


def vertical_filter(layout, cam_h=640, W=1080, H=1920, ass=None,
                    src_w=1920, src_h=1080):
    """Build the filtergraph that turns a 16:9 clip into a 9:16 short."""
    face = layout.get("facecam")
    gx, gy, gw, gh = layout["gameplay"]

    parts = []
    if face and cam_h > 0:
        fx, fy, fw, fh = face
        # scale facecam to full width, then trim to the band height
        scaled_h = _even(fh * W / fw)
        cam_h = _even(min(cam_h, scaled_h))
        crop_y = _even(max((scaled_h - cam_h) / 2, 0))
        parts.append(
            f"[0:v]crop={fw}:{fh}:{fx}:{fy},scale={W}:{scaled_h},"
            f"crop={W}:{cam_h}:0:{crop_y},setsar=1[cam]"
        )
        game_h = H - cam_h
    else:
        game_h = H

    # centre-crop the gameplay area to the aspect the remaining band needs
    target_ar = W / game_h
    crop_w = _even(min(gw, gh * target_ar))
    crop_h = _even(min(gh, crop_w / target_ar))
    cx = _even(gx + (gw - crop_w) / 2)
    cy = _even(gy + (gh - crop_h) / 2)
    parts.append(
        f"[0:v]crop={crop_w}:{crop_h}:{cx}:{cy},scale={W}:{game_h},setsar=1[game]"
    )

    if face and cam_h > 0:
        parts.append("[cam][game]vstack=inputs=2[stacked]")
        last = "stacked"
    else:
        last = "game"

    if ass:
        parts.append(f"[{last}]subtitles='{_ass_for_filter(ass)}'[v]")
        last = "v"
    else:
        parts.append(f"[{last}]null[v]")
    return ";".join(parts), last


def render(src_clip, out_path, layout_name="untumbled", cam_h=640,
           ass=None, crf=20, fps=30, W=1080, H=1920):
    layout = LAYOUTS[layout_name]
    fg, _last = vertical_filter(layout, cam_h=cam_h, W=W, H=H, ass=ass)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", src_clip,
        "-filter_complex", fg, "-map", "[v]", "-map", "0:a?",
        "-r", str(fps),
        "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"vertical render failed: {r.stderr[-500:]}")
    return out_path
