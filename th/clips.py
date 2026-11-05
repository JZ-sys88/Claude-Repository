"""Tier 1: viewer-created clips. Human-curated, view-ranked ground truth."""
import datetime as dt

from .model import candidate


def _rfc3339(days_ago):
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(tw, broadcaster_id, days=7, limit=200, vod_id=None):
    """Clips for a channel over the last `days`, newest window first.

    Helix returns clips ordered by view count within the requested window.
    """
    params = {
        "broadcaster_id": broadcaster_id,
        "first": 100,
        "started_at": _rfc3339(days),
        "ended_at": _rfc3339(0),
    }
    return list(tw.paginate("clips", limit=limit, **params))


def to_candidates(clips, vod_id=None):
    """Normalise Helix clips into candidates.

    `vod_offset` is where the clip sits inside the source VOD. It is null on
    some older clips and on clips made from a stream whose VOD has expired --
    those cannot be located in a VOD, so they are dropped here and handled by
    the direct-clip download path instead.
    """
    out, orphans = [], []
    for c in clips:
        if vod_id and c.get("video_id") != vod_id:
            continue
        off = c.get("vod_offset")
        if off is None:
            orphans.append(c)
            continue
        out.append(candidate(
            source="clips",
            start=float(off),
            duration=float(c.get("duration") or 30.0),
            score=float(c.get("view_count") or 0),
            title=c.get("title") or "",
            vod_id=c.get("video_id"),
            detail={
                "clip_id": c.get("id"),
                "url": c.get("url"),
                "views": c.get("view_count"),
                "creator": c.get("creator_name"),
                "created_at": c.get("created_at"),
                "thumbnail_url": c.get("thumbnail_url"),
            },
        ))
    return out, orphans


def recent_vods(tw, broadcaster_id, limit=20):
    """Archived broadcasts, newest first."""
    return list(tw.paginate(
        "videos", limit=limit,
        user_id=broadcaster_id, type="archive", first=100,
    ))
