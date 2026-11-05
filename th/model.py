"""Shared candidate schema + merge/dedupe/ranking."""
import json


def candidate(source, start, duration, score, title="", vod_id=None, detail=None):
    """One highlight candidate. `start` is seconds into the VOD."""
    return {
        "source": source,          # clips | chat | audio
        "vod_id": vod_id,
        "start": float(start),
        "duration": float(duration),
        "score": float(score),     # normalised 0..1 within its source
        "title": title,
        "detail": detail or {},
    }


def normalise(cands):
    """Scale each source's raw scores to 0..1 independently."""
    by_source = {}
    for c in cands:
        by_source.setdefault(c["source"], []).append(c)
    for items in by_source.values():
        # Scale against the max, not the min-max range: with only a handful of
        # candidates a min-max map forces the weakest to exactly 0, discarding
        # a moment that may still be worth keeping.
        hi = max(c["score"] for c in items) or 1.0
        for c in items:
            c["score"] = c["score"] / hi
    return cands


# Trust ordering: humans clipped it > she said something > chat reacted >
# audio got loud. Speech outranks chat because it works on quiet channels,
# where chat density has no signal to offer at all.
SOURCE_WEIGHT = {"clips": 1.0, "speech": 0.9, "chat": 0.65,
                 # onset is high-recall / low-precision: it reliably surfaces
                 # nonverbal reactions the speech tier cannot see, but loudness
                 # rise does not predict which one a human finds clip-worthy
                 # (validated pick ranked 37th of 40). Weighted to populate the
                 # review page, not to lead it.
                 "onset": 0.5, "audio": 0.4}
AGREEMENT_BONUS = 0.15   # per extra source that independently flagged the moment


def merge(cands, window=45.0):
    """Collapse candidates that point at the same moment.

    Overlapping detections from *different* sources are strong evidence, so a
    merged moment keeps the best single score and gains a bonus per extra
    source that agreed.
    """
    if not cands:
        return []
    ordered = sorted(cands, key=lambda c: c["start"])
    groups = [[ordered[0]]]
    for c in ordered[1:]:
        if c["start"] - groups[-1][-1]["start"] <= window:
            groups[-1].append(c)
        else:
            groups.append([c])

    merged = []
    for g in groups:
        sources = {c["source"] for c in g}
        best = max(g, key=lambda c: c["score"] * SOURCE_WEIGHT.get(c["source"], 0.5))
        final = best["score"] * SOURCE_WEIGHT.get(best["source"], 0.5)
        final += AGREEMENT_BONUS * (len(sources) - 1)
        # prefer the earliest start in the group so we don't clip into the payoff
        start = min(c["start"] for c in g)
        end = max(c["start"] + c["duration"] for c in g)
        merged.append({
            **best,
            "start": start,
            "duration": min(end - start, 90.0),
            "final_score": round(min(final, 1.5), 4),
            "sources": sorted(sources),
        })
    merged.sort(key=lambda c: c["final_score"], reverse=True)
    return merged


def apply_padding(cands, pre=8.0, post=3.0, vod_duration=None):
    """Chat and audio *lag* the play, so back the cut up before the reaction."""
    out = []
    for c in dict_iter(cands):
        # speech candidates already start at the first spoken word of the
        # reaction, so they need only a small lead-in; chat and audio lag the
        # play by several seconds and need a real pre-roll.
        pre_roll = pre if c["source"] in ("chat", "audio") else 2.0
        start = max(0.0, c["start"] - pre_roll)
        dur = c["duration"] + pre_roll + post
        if vod_duration:
            dur = min(dur, max(0.0, vod_duration - start))
        out.append({**c, "start": start, "duration": dur})
    return out


def dict_iter(cands):
    return [dict(c) for c in cands]


def save(cands, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cands, f, indent=2)


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def hhmmss(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"
