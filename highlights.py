#!/usr/bin/env python
"""Twitch highlight reel builder.

  python highlights.py channel <name> [--days 7]
  python highlights.py analyze <vod_id> [--samples 240] [--clips] [--audio]
  python highlights.py review candidates.json [--top 30]
  python highlights.py render candidates.json [--top 15 | --pick 0,3,7]
"""
import argparse
import json
import os
import pathlib
import sys
import tempfile

from th import chat, media, model, review

ROOT = pathlib.Path(__file__).resolve().parent


def load_env():
    """Read .env if present; real environment variables win."""
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def get_twitch():
    """Helix client, or None with an explanation (clips tier is optional)."""
    from th.auth import Twitch, AuthError
    try:
        return Twitch()
    except AuthError as e:
        print(f"  [clips tier unavailable] {e}", file=sys.stderr)
        return None


# -- commands ------------------------------------------------------------
def cmd_channel(a):
    from th import clips as clipmod
    tw = get_twitch()
    if not tw:
        return 1
    uid, name = tw.user_id(a.channel)
    print(f"{name} (id {uid})\n")
    vods = clipmod.recent_vods(tw, uid, limit=a.limit)
    print(f"recent archives ({len(vods)}):")
    for v in vods:
        print(f"  {v['id']:>12}  {v.get('duration',''):>10}  {v.get('created_at','')[:10]}  {v.get('title','')[:50]}")
    cl = clipmod.fetch(tw, uid, days=a.days, limit=100)
    print(f"\nclips in last {a.days}d: {len(cl)}")
    for c in cl[:10]:
        print(f"  {c.get('view_count'):>6} views  vod={c.get('video_id') or '-':>12}  "
              f"off={c.get('vod_offset')}  {c.get('title','')[:45]}")
    return 0


CHAT_FLOOR = 0.05   # msg/sec below which density has nothing to work with


def cmd_analyze(a):
    cands, stats = [], {}
    length = chat.vod_length(a.vod_id)

    if not a.no_chat:
        print(f"[chat] probing VOD {a.vod_id} ...")
        pts, length = chat.probe(a.vod_id, samples=a.samples,
                                 progress=lambda i, n: print(f"  probe {i}/{n}", end="\r"))
        cc, cstats = chat.probe_candidates(pts, top=a.top)
        median = cstats.get("median_rate", 0)
        if median < CHAT_FLOOR:
            # Ratios stay large on tiny counts, so a "5x spike" over 1 msg/min
            # is noise. Report and discard rather than poisoning the merge.
            print(f"  chat too sparse ({median:.3f} msg/s < {CHAT_FLOOR}) "
                  f"-- discarding {len(cc)} chat candidates")
            cc = []
        else:
            print(f"  {len(cc)} chat candidates | peak {cstats.get('peak_ratio')}x")
        cands += cc
        stats.update({f"chat_{k}": v for k, v in cstats.items()})

    if a.clips:
        from th import clips as clipmod
        tw = get_twitch()
        if tw:
            info = tw.get("videos", id=str(a.vod_id).lstrip("v")).get("data", [])
            if info:
                uid = info[0]["user_id"]
                raw = clipmod.fetch(tw, uid, days=a.days, limit=a.clip_limit)
                cl, orphans = clipmod.to_candidates(raw, vod_id=str(a.vod_id).lstrip("v"))
                cands += cl
                stats["clips_matched"] = len(cl)
                print(f"[clips] {len(raw)} fetched, {len(cl)} matched this VOD, "
                      f"{len(orphans)} without vod_offset")
            else:
                print("[clips] VOD not found via Helix")

    audio_path = a.audio_file
    if (a.audio or a.speech or a.onset) and not audio_path:
        # one download serves both tiers
        print("[audio] downloading audio-only track ...")
        tmp = tempfile.mkdtemp(prefix="th_audio_")
        audio_path = os.path.join(tmp, "a.m4a")
        try:
            # Twitch labels this format "Audio_Only"; fall back if that changes
            media.download_section(a.vod_id, 0, length, audio_path,
                                   quality="Audio_Only/bestaudio")
            print(f"  {os.path.getsize(audio_path)/1e6:.0f} MB -> {audio_path}")
        except Exception as e:
            print(f"  [audio download failed] {e}", file=sys.stderr)
            audio_path = None

    loud_prof = None
    if (a.audio or a.onset) and audio_path:
        loud_prof = media.loudness_profile(audio_path)

    if a.onset and loud_prof:
        try:
            oc, ostats = media.onset_candidates(loud_prof, top=a.top,
                                                rise_lu=a.onset_rise)
            cands += oc
            stats.update({f"onset_{k}": v for k, v in ostats.items()})
            print(f"[onset] {len(oc)} candidates (sudden rises >= {a.onset_rise} LU)")
        except Exception as e:
            print(f"  [onset tier failed] {e}", file=sys.stderr)

    if a.audio and audio_path:
        try:
            prof = loud_prof
            ac, astats = media.audio_candidates(prof, top=a.top,
                                                baseline_pct=a.audio_baseline)
            cands += ac
            stats.update({f"audio_{k}": v for k, v in astats.items()})
            print(f"[audio] {len(ac)} candidates "
                  f"(baseline p{a.audio_baseline} = {astats.get('baseline_lufs')} LUFS)")
        except Exception as e:
            print(f"  [audio tier failed] {e}", file=sys.stderr)

    if a.speech and audio_path:
        try:
            from th import speech as sp
            if a.reuse_transcript and os.path.exists(a.transcript):
                with open(a.transcript, encoding="utf-8") as f:
                    segs = json.load(f)
                txt = a.transcript
                print(f"[speech] reusing {a.transcript} ({len(segs)} segments)")
            else:
                print(f"[speech] transcribing with {a.model} ...")
                segs, _info = sp.transcribe(
                    audio_path, model_size=a.model,
                    progress=lambda n, t: print(
                        f"  {n} segments, at {t/60:.0f} min", end="\r"))
                txt = sp.save_transcript(segs, a.transcript)
            sc = sp.moments(segs, top=a.top, min_score=a.speech_min)
            cands += sc
            stats["speech_segments"] = len(segs)
            print(f"  {len(segs)} segments -> {len(sc)} candidates | transcript: {txt}")
        except Exception as e:
            print(f"  [speech tier failed] {e}", file=sys.stderr)

    cands = model.normalise(cands)
    merged = model.merge(cands, window=a.merge_window)
    merged = model.apply_padding(merged, pre=a.pre, post=a.post, vod_duration=length)
    for c in merged:
        c["vod_id"] = str(a.vod_id).lstrip("v")

    model.save(merged, a.out)
    print(f"\n{len(merged)} merged candidates -> {a.out}")
    for c in merged[:15]:
        print(f"  {model.hhmmss(c['start'])}  {c['final_score']:.2f}  "
              f"{'+'.join(c['sources']):<12} {c.get('title','')[:40]}")
    return 0


def analyse_source(src, workdir, top=40, model_size="base.en", speech_min=2.5,
                   onset_rise=7.0, keep_audio=False):
    """Analyse one VOD into workdir, reusing anything already cached.

    A 3-hour VOD costs ~10 min to fetch and ~5 min to transcribe, so a package
    spanning several streams must be resumable or it is unusable.
    """
    from th import speech as sp

    d = pathlib.Path(workdir) / src.key
    d.mkdir(parents=True, exist_ok=True)
    cand_p, tr_p, au_p = d / "candidates.json", d / "transcript.json", d / "audio.m4a"

    if cand_p.exists():
        print(f"  [cached] {src.key}")
        return model.load(cand_p), tr_p

    if not au_p.exists():
        print(f"  downloading audio for {src.key} ...")
        media.download_audio(src, src.duration or 0, str(au_p))
    cands = []

    prof = media.loudness_profile(str(au_p))
    oc, _ = media.onset_candidates(prof, top=top, rise_lu=onset_rise)
    cands += oc

    if tr_p.exists():
        with open(tr_p, encoding="utf-8") as f:
            segs = json.load(f)
    else:
        print(f"  transcribing {src.key} ...")
        segs, _i = sp.transcribe(str(au_p), model_size=model_size)
        sp.save_transcript(segs, str(tr_p))
    cands += sp.moments(segs, top=top, min_score=speech_min)

    cands = model.normalise(cands)
    merged = model.merge(cands)
    merged = model.apply_padding(merged, vod_duration=src.duration)
    for c in merged:
        c["vod_id"] = src.id
        c["source_url"] = src.url
        c["stream_title"] = src.title
        c["stream_date"] = src.date
    model.save(merged, cand_p)
    if not keep_audio:
        try:
            au_p.unlink()
        except OSError:
            pass
    print(f"  {len(merged)} candidates -> {cand_p}")
    return merged, tr_p


def cmd_package(a):
    """Collate one game's best moments across many streams into a long cut."""
    from th import source as srcmod, shorts

    streams = srcmod.channel_streams(a.channel, limit=a.scan)
    if a.ids:
        want = [i.strip() for i in a.ids.split(",") if i.strip()]
        by_id = {s.id: s for s in streams}
        picked = [by_id[i] for i in want if i in by_id]
        missing = [i for i in want if i not in by_id]
        for i in missing:                      # not in the scanned window
            picked.append(srcmod.fetch_meta(srcmod.resolve(i)))
        keys = want
    else:
        keys = [k.strip().lower() for k in a.match.split(",") if k.strip()]
        if not keys:
            print("give --match or --ids", file=sys.stderr)
            return 1
        picked = [s for s in streams
                  if any(k in (s.title or "").lower() for k in keys)]
    picked.sort(key=lambda s: streams.index(s) if s in streams else -1,
                reverse=True)                  # oldest first
    if a.max_vods:
        picked = picked[: a.max_vods]

    print(f"{len(streams)} streams scanned, {len(picked)} match {keys}:")
    for s in picked:
        mins = (s.duration or 0) / 60
        print(f"  {s.id}  {mins:5.0f}m  {s.title}")
    if not picked:
        return 1

    work = pathlib.Path(a.workdir)
    per_vod = []
    for s in picked:
        if not s.duration or not s.date:
            # flat-playlist listing carries duration but not upload_date, and
            # the per-stream title card wants the date
            srcmod.fetch_meta(s)
        cands, tr = analyse_source(s, work, top=a.top, model_size=a.model,
                                   speech_min=a.speech_min)
        per_vod.append((s, cands, tr))

    if a.analyse_only:
        print("\nanalysis complete (--analyse-only)")
        return 0

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="th_pkg_"))
    parts = []
    media.title_card(a.game, str(tmp / "intro.mp4"),
                     subtitle=f"{len(picked)} streams", duration=3.0, fps=a.fps)
    parts.append(str(tmp / "intro.mp4"))

    n = 0
    for si, (s, cands, _tr) in enumerate(per_vod):
        # Over-select: a blank clip is replaced from further down the ranking
        # rather than just leaving a hole.
        pool = cands[: max(a.per_vod * 3, a.per_vod)]
        kept, rejected = [], 0
        for ci, c in enumerate(pool):
            if len(kept) >= a.per_vod:
                break
            raw = str(tmp / f"r{si:02d}_{ci:02d}.mp4")
            norm = str(tmp / f"n{si:02d}_{ci:02d}.mp4")
            dur = min(c["duration"], a.max_clip)
            print(f"[{s.key} {len(kept)+1}/{a.per_vod}] {model.hhmmss(c['start'])} ({int(dur)}s)")
            try:
                media.download_section(c.get("source_url") or s.url,
                                       c["start"], dur, raw)
                if a.blank_reject > 0:
                    frac = media.blank_fraction(raw)
                    if frac >= a.blank_reject:
                        rejected += 1
                        print(f"  rejected: {frac:.0%} black (loading/starting screen)")
                        continue
                media.normalise(raw, norm, fps=a.fps)
                kept.append((c["start"], norm))
                n += 1
            except Exception as e:
                print(f"  skipped: {e}", file=sys.stderr)

        if not kept:
            continue
        if rejected:
            print(f"  {rejected} clip(s) replaced for being blank")
        kept.sort(key=lambda kv: kv[0])          # chronological within stream
        date = s.date or ""
        pretty = f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else ""
        card = str(tmp / f"card{si:02d}.mp4")
        media.title_card((s.title or "")[:56], card, subtitle=pretty,
                         duration=2.5, fps=a.fps)
        parts.append(card)
        parts.extend(p for _t, p in kept)

    if n == 0:
        print("no clips rendered", file=sys.stderr)
        return 1
    media.concat(parts, a.out)
    print(f"\n{n} clips from {len(per_vod)} streams -> {a.out} "
          f"({media.duration_of(a.out)/60:.1f} min)")
    return 0


def cmd_review(a):
    cands = model.load(a.candidates)[: a.top]
    vod = cands[0]["vod_id"] if cands else None
    thumbs = {}
    if vod and not a.no_thumbs:
        print("[thumbs] resolving stream URL ...")
        try:
            url = media.stream_url(vod)
            tmp = pathlib.Path(tempfile.mkdtemp(prefix="th_thumbs_"))
            for i, c in enumerate(cands):
                p = str(tmp / f"{i:03d}.jpg")
                # seek a little past the pre-roll so the frame shows the action
                if media.thumbnail(url, c["start"] + min(c["duration"] * 0.5, 12), p):
                    thumbs[i] = p
                print(f"  {i+1}/{len(cands)}", end="\r")
            print(f"  {len(thumbs)}/{len(cands)} thumbnails      ")
        except Exception as e:
            print(f"  [thumbnails unavailable] {e}", file=sys.stderr)

    review.build(cands, a.out, thumbs=thumbs,
                 title=f"Highlight candidates - VOD {vod}")
    print(f"review page -> {a.out}")
    return 0


def _pick(cands, a):
    if getattr(a, "pick", None):
        idx = [int(x) for x in a.pick.split(",") if x.strip() != ""]
        return [cands[i] for i in idx if 0 <= i < len(cands)]
    return cands[: a.top]


def cmd_shorts(a):
    """One vertical file per moment -- these are posted individually."""
    from th import shorts, source as srcmod

    cands = _pick(model.load(a.candidates), a)
    if not cands:
        print("nothing selected", file=sys.stderr)
        return 1

    segs = []
    if not a.no_captions and os.path.exists(a.transcript):
        with open(a.transcript, encoding="utf-8") as f:
            segs = json.load(f)
        print(f"[captions] {len(segs)} transcript segments")
    elif not a.no_captions:
        print(f"[captions] {a.transcript} not found -- rendering without", file=sys.stderr)

    src = srcmod.resolve(a.source) if a.source else None
    outdir = pathlib.Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix="th_shorts_"))

    made = []
    for i, c in enumerate(cands):
        target = src or c["vod_id"]
        dur = min(c["duration"], a.max_clip)
        label = model.hhmmss(c["start"]).replace(":", "")
        raw = str(work / f"raw{i:03d}.mp4")
        out = str(outdir / f"short_{i:02d}_{label}.mp4")
        print(f"[{i+1}/{len(cands)}] {model.hhmmss(c['start'])} ({int(dur)}s) ...")
        try:
            media.download_section(target, c["start"], dur, raw)
            ass = None
            if segs:
                ass = str(work / f"c{i:03d}.ass")
                _p, n = shorts.build_ass(segs, c["start"], dur, ass,
                                         size=a.font_size)
                if n == 0:
                    ass = None
            shorts.render(raw, out, layout_name=a.layout, cam_h=a.cam_height,
                          ass=ass, fps=a.fps)
            made.append(out)
        except Exception as e:
            print(f"  skipped: {e}", file=sys.stderr)

    print(f"\n{len(made)} shorts -> {outdir}")
    for m in made:
        print(f"  {os.path.basename(m)}  {media.duration_of(m):.0f}s")
    return 0


def cmd_render(a):
    cands = model.load(a.candidates)
    if a.pick:
        idx = [int(x) for x in a.pick.split(",") if x.strip() != ""]
        sel = [cands[i] for i in idx if 0 <= i < len(cands)]
    else:
        sel = cands[: a.top]
    if not sel:
        print("nothing selected", file=sys.stderr)
        return 1
    if not a.keep_order:
        sel.sort(key=lambda c: c["start"])
    if a.max_clip:
        # Speech clusters can span a minute or more; trim from the end so the
        # reaction that triggered the candidate stays in frame.
        sel = [{**c, "duration": min(c["duration"], a.max_clip)} for c in sel]

    work = pathlib.Path(tempfile.mkdtemp(prefix="th_render_"))
    parts = []
    for i, c in enumerate(sel):
        raw = str(work / f"raw{i:03d}.mp4")
        norm = str(work / f"n{i:03d}.mp4")
        label = model.hhmmss(c["start"])
        print(f"[{i+1}/{len(sel)}] {label} ({int(c['duration'])}s) ...")
        try:
            clip_url = (c.get("detail") or {}).get("url")
            if c["source"] == "clips" and clip_url and a.prefer_clip_source:
                media.download_clip(clip_url, raw)
            else:
                media.download_section(c["vod_id"], c["start"], c["duration"],
                                       raw, quality=a.quality)
            media.normalise(raw, norm, width=a.width, height=a.height,
                            fps=a.fps, fade=a.fade)
            parts.append(norm)
        except Exception as e:
            print(f"  skipped: {e}", file=sys.stderr)

    if not parts:
        print("no clips rendered", file=sys.stderr)
        return 1
    media.concat(parts, a.out)
    print(f"\n{len(parts)} clips -> {a.out} ({media.duration_of(a.out):.0f}s)")
    return 0


# -- arg parsing ---------------------------------------------------------
def main():
    load_env()
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("channel", help="list a channel's VODs and clips")
    c.add_argument("channel")
    c.add_argument("--days", type=int, default=7)
    c.add_argument("--limit", type=int, default=10)
    c.set_defaults(func=cmd_channel)

    a = sub.add_parser("analyze", help="find highlight candidates in a VOD")
    a.add_argument("vod_id")
    a.add_argument("--samples", type=int, default=240)
    a.add_argument("--top", type=int, default=40)
    a.add_argument("--clips", action="store_true", help="add viewer clips (needs API keys)")
    a.add_argument("--audio", action="store_true", help="add audio-energy tier")
    a.add_argument("--speech", action="store_true", help="add transcription tier")
    a.add_argument("--onset", action="store_true",
                   help="add sudden-loudness tier (screams, gasps)")
    a.add_argument("--onset-rise", type=float, default=7.0)
    a.add_argument("--no-chat", action="store_true", help="skip chat (quiet channels)")
    a.add_argument("--model", default="base.en", help="whisper model, e.g. small.en")
    a.add_argument("--speech-min", type=float, default=2.5, help="min reaction score")
    a.add_argument("--audio-baseline", type=int, default=85, help="loudness percentile")
    a.add_argument("--audio-file", help="reuse an already-downloaded audio track")
    a.add_argument("--transcript", default="transcript.json")
    a.add_argument("--reuse-transcript", action="store_true",
                   help="load --transcript instead of re-transcribing")
    a.add_argument("--days", type=int, default=14, help="clip search window")
    a.add_argument("--clip-limit", type=int, default=300)
    a.add_argument("--merge-window", type=float, default=45.0)
    a.add_argument("--pre", type=float, default=8.0, help="pre-roll seconds")
    a.add_argument("--post", type=float, default=3.0)
    a.add_argument("-o", "--out", default="candidates.json")
    a.set_defaults(func=cmd_analyze)

    r = sub.add_parser("review", help="build an HTML contact sheet")
    r.add_argument("candidates")
    r.add_argument("--top", type=int, default=30)
    r.add_argument("--no-thumbs", action="store_true")
    r.add_argument("-o", "--out", default="review.html")
    r.set_defaults(func=cmd_review)

    pk = sub.add_parser("package", help="collate one game across many streams")
    pk.add_argument("--game", required=True, help="display name for the intro card")
    pk.add_argument("--match", default="", help="title keywords, comma separated")
    pk.add_argument("--ids", default="", help="explicit video ids, comma separated "
                                              "(use when titles do not name the game)")
    pk.add_argument("--channel", default="Untumbled", help="YouTube handle")
    pk.add_argument("--scan", type=int, default=200, help="streams to scan")
    pk.add_argument("--max-vods", type=int, default=0, help="0 = all matches")
    pk.add_argument("--per-vod", type=int, default=6, help="clips per stream")
    pk.add_argument("--top", type=int, default=40)
    pk.add_argument("--max-clip", type=float, default=35.0)
    pk.add_argument("--model", default="base.en")
    pk.add_argument("--speech-min", type=float, default=2.5)
    pk.add_argument("--fps", type=int, default=30)
    pk.add_argument("--blank-reject", type=float, default=0.5,
                    help="drop a clip if this fraction of its gameplay region "
                         "is black (0 disables)")
    pk.add_argument("--analyse-only", action="store_true")
    pk.add_argument("--workdir", default="packages")
    pk.add_argument("-o", "--out", default="package.mp4")
    pk.set_defaults(func=cmd_package)

    sh = sub.add_parser("shorts", help="one 9:16 vertical file per moment")
    sh.add_argument("candidates")
    sh.add_argument("--top", type=int, default=5)
    sh.add_argument("--pick", help="explicit indices, e.g. 0,3,7")
    sh.add_argument("--source", help="render from this URL/id instead of the "
                                     "analysed VOD (e.g. the YouTube copy)")
    sh.add_argument("--layout", default="untumbled",
                    choices=["untumbled", "plain"])
    sh.add_argument("--cam-height", type=int, default=640,
                    help="facecam band height in the 1920px frame")
    sh.add_argument("--max-clip", type=float, default=45.0)
    sh.add_argument("--font-size", type=int, default=62)
    sh.add_argument("--fps", type=int, default=30)
    sh.add_argument("--no-captions", action="store_true")
    sh.add_argument("--transcript", default="transcript.json")
    sh.add_argument("--outdir", default="shorts")
    sh.set_defaults(func=cmd_shorts)

    d = sub.add_parser("render", help="cut and join the reel")
    d.add_argument("candidates")
    d.add_argument("--top", type=int, default=15)
    d.add_argument("--pick", help="explicit indices, e.g. 0,3,7")
    d.add_argument("--keep-order", action="store_true", help="rank order, not chronological")
    d.add_argument("--prefer-clip-source", action="store_true",
                   help="download viewer clips directly instead of re-cutting the VOD")
    d.add_argument("--max-clip", type=float, default=30.0,
                   help="cap each clip length in seconds (0 = no cap)")
    d.add_argument("--quality", default="best")
    d.add_argument("--width", type=int, default=1920)
    d.add_argument("--height", type=int, default=1080)
    d.add_argument("--fps", type=int, default=60)
    d.add_argument("--fade", type=float, default=0.4)
    d.add_argument("-o", "--out", default="reel.mp4")
    d.set_defaults(func=cmd_render)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
