"""Tier 2: VOD chat density.

Helix has no chat-replay endpoint (the old v5 /rechat API was removed), so this
uses the public GQL endpoint. It sends a *raw* query rather than a persisted
query hash -- Twitch rotates those hashes and the persisted form breaks without
warning.

Two modes:
  probe() -- one request per sample point. Each page returns a fixed ~59
             comments, so the *time span* those comments cover is an inverse
             density measure: dense moments come back covering 3 seconds, dead
             air comes back covering 3 minutes. An 11-hour VOD profiles in
             ~200 requests instead of ~1700 pages of full pagination.
  fetch_comments() -- full crawl. Accurate, slow; used to refine peaks.
"""
import time

import requests

GQL = "https://gql.twitch.tv/gql"
WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"   # public web client

QUERY = """
query($videoID: ID!, $contentOffsetSeconds: Int, $cursor: Cursor) {
  video(id: $videoID) {
    id
    lengthSeconds
    comments(contentOffsetSeconds: $contentOffsetSeconds, after: $cursor) {
      edges {
        cursor
        node {
          contentOffsetSeconds
          message { fragments { text } }
        }
      }
      pageInfo { hasNextPage }
    }
  }
}"""

# Reactions that mean "something happened", not just ambient chatter.
HYPE = {
    "pog", "poggers", "pogchamp", "pogu", "omegalul", "lul", "lulw", "kekw",
    "clip", "clipit", "clipped", "sheesh", "insane", "actual", "nowaydude",
    "w", "ws", "dub", "ez", "gg", "holy", "yooo", "yoo", "wtf", "lmao",
    "cracked", "wtff", "nani", "widepeepohappy", "pepehands", "monkas",
}


class ChatError(RuntimeError):
    pass


def _post(session, variables):
    payload = {"query": QUERY, "variables": variables}
    headers = {"Client-ID": WEB_CLIENT_ID}
    for attempt in range(4):
        try:
            r = session.post(GQL, json=payload, headers=headers, timeout=30)
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 200:
            body = r.json()
            if isinstance(body, list):
                body = body[0]
            if body.get("errors"):
                raise ChatError(f"GQL errors: {body['errors']}")
            return body
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2.0 * (attempt + 1))
            continue
        raise ChatError(f"GQL {r.status_code}: {r.text[:300]}")
    raise ChatError("GQL failed after retries")


def _page(body):
    video = (body.get("data") or {}).get("video")
    if not video:
        raise ChatError("No chat for this VOD (expired, private, or sub-only?)")
    comments = video.get("comments") or {}
    rows = []
    for e in comments.get("edges") or []:
        node = e.get("node") or {}
        off = node.get("contentOffsetSeconds")
        if off is None:
            continue
        frags = ((node.get("message") or {}).get("fragments")) or []
        text = "".join(f.get("text") or "" for f in frags)
        rows.append((int(off), text, _is_hype(text)))
    edges = comments.get("edges") or []
    cursor = edges[-1].get("cursor") if edges else None
    has_next = (comments.get("pageInfo") or {}).get("hasNextPage", False)
    return rows, cursor, has_next, int(video.get("lengthSeconds") or 0)


def _is_hype(text):
    words = [w.strip(".,!?~").lower() for w in text.split()]
    return any(w in HYPE for w in words if w)


def vod_length(video_id, session=None):
    session = session or requests.Session()
    body = _post(session, {"videoID": str(video_id).lstrip("v"),
                           "contentOffsetSeconds": 0})
    return _page(body)[3]


def probe(video_id, samples=240, progress=None):
    """Sample chat density across the VOD. Returns (points, length_seconds).

    points = [{"t": sec, "rate": msgs_per_sec, "hype": frac, "n": count}]
    """
    video_id = str(video_id).lstrip("v")
    session = requests.Session()

    body = _post(session, {"videoID": video_id, "contentOffsetSeconds": 0})
    rows, _cursor, _next, length = _page(body)
    if not length:
        raise ChatError("Could not read VOD length")

    step = max(int(length / samples), 5)
    points = []
    for i, t in enumerate(range(0, length, step)):
        if i == 0:
            page = rows
        else:
            page = _page(_post(session, {"videoID": video_id,
                                         "contentOffsetSeconds": int(t)}))[0]
        if len(page) < 5:
            continue
        span = max(page[-1][0] - page[0][0], 1)
        rate = len(page) / span
        hype = sum(1 for r in page if r[2]) / len(page)
        points.append({"t": page[0][0], "rate": rate, "hype": round(hype, 3),
                       "n": len(page), "span": span})
        if progress and i % 25 == 0:
            progress(i, len(range(0, length, step)))
    return points, length


def probe_candidates(points, top=40, min_ratio=2.0, hype_weight=1.5,
                     dedupe=90, clip_len=25.0):
    """Turn a density profile into ranked candidates."""
    from .model import candidate
    if not points:
        return [], {}

    rates = sorted(p["rate"] for p in points)
    median = rates[len(rates) // 2] or 0.001

    scored = []
    for p in points:
        ratio = p["rate"] / median
        # a burst that is *also* full of hype emotes outranks a burst of
        # ordinary chatter at the same raw rate
        scored.append((ratio * (1 + hype_weight * p["hype"]), ratio, p))
    scored.sort(reverse=True, key=lambda x: x[0])

    picked, out = [], []
    for weighted, ratio, p in scored:
        if ratio < min_ratio:
            break
        if any(abs(p["t"] - q) < dedupe for q in picked):
            continue
        picked.append(p["t"])
        out.append(candidate(
            source="chat", start=p["t"], duration=clip_len, score=weighted,
            title=f"chat {ratio:.1f}x",
            detail={"rate": round(p["rate"], 2), "ratio": round(ratio, 2),
                    "hype_frac": p["hype"], "sampled": p["n"]},
        ))
        if len(out) >= top:
            break

    stats = {
        "samples": len(points),
        "median_rate": round(median, 3),
        "peak_rate": round(max(p["rate"] for p in points), 2),
        "peak_ratio": round(max(p["rate"] for p in points) / median, 1),
    }
    return out, stats


def fetch_comments(video_id, start=0, end=None, max_pages=4000, progress=None):
    """Full crawl over [start, end]. Slow; use to refine a peak window."""
    video_id = str(video_id).lstrip("v")
    session = requests.Session()
    out, cursor = [], None
    for page_i in range(max_pages):
        variables = {"videoID": video_id}
        if cursor:
            variables["cursor"] = cursor
        else:
            variables["contentOffsetSeconds"] = int(start)
        rows, cursor, has_next, _len = _page(_post(session, variables))
        if not rows:
            break
        out.extend(rows)
        if end is not None and rows[-1][0] >= end:
            return [r for r in out if start <= r[0] <= end]
        if progress and page_i % 20 == 0:
            progress(len(out), rows[-1][0])
        if not has_next or not cursor:
            break
    return out
