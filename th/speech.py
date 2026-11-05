"""Tier 4: what the streamer actually says.

For a channel with a quiet chat and a calm streamer, neither density nor
loudness separates highlights from filler -- but the words do. This transcribes
the VOD and scores segments on reaction language, then clusters nearby hits
into moments.

The transcript is also written out verbatim so it can be ranked semantically
(by eye or by an LLM) rather than only by keyword -- that is the part that
catches "this bit was funny" when no keyword fires.
"""
import json
import re

# Reaction language, weighted. Tuned for cozy/story games rather than FPS:
# the payload is surprise, delight, dismay and laughter, not "clutch".
PATTERNS = [
    (3.0, re.compile(r"\b(ha){2,}\b|\bhaha+\b|\blmao\b|\blmfao\b", re.I)),      # laughter
    (2.5, re.compile(r"\boh my (god|gosh|goodness)\b|\bomg\b", re.I)),
    (2.5, re.compile(r"\bno way\b|\bare you (kidding|serious)\b|\bshut up\b", re.I)),
    (2.2, re.compile(r"\bwhat the\b|\bwhat[?!]|\bhuh[?!]", re.I)),
    (2.0, re.compile(r"\b(oh no|oh god|oh dear)\b", re.I)),
    (2.0, re.compile(r"\bwait[,.! ]|\bhold on\b|\bexcuse me\b", re.I)),
    (2.0, re.compile(r"\bfinally\b|\bfirst time\b|\bi did it\b|\bwe did it\b", re.I)),
    (1.8, re.compile(r"\bso (cute|good|pretty|sweet)\b|\bi love\b|\badorable\b", re.I)),
    (1.8, re.compile(r"\b(died|death|killed me|i'm dead)\b", re.I)),
    (1.6, re.compile(r"\b(no){2,}\b|\bnooo+\b|\byesss+\b|\bwoah+\b|\bwhoa+\b", re.I)),
    (1.5, re.compile(r"\bactually\b.*\b(good|bad|insane|crazy)\b", re.I)),
    (1.2, re.compile(r"!{2,}"),),
]

CAPS = re.compile(r"\b[A-Z]{3,}\b")


def transcribe(audio_path, model_size="base.en", device="cpu",
               compute_type="int8", progress=None):
    """Return ([{start,end,text}], info). VAD skips silence -- a big win on a
    quiet stream where much of the runtime is game audio with no speech."""
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    segments, info = model.transcribe(
        audio_path,
        beam_size=1,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 700},
        condition_on_previous_text=False,
    )
    rows = []
    for s in segments:
        text = (s.text or "").strip()
        if not text:
            continue
        rows.append({"start": float(s.start), "end": float(s.end), "text": text})
        if progress and len(rows) % 100 == 0:
            progress(len(rows), rows[-1]["start"])
    return rows, info


def repetition(text, max_gram=4):
    """Strength of the strongest repeated phrase in a line.

    Panic and delight both make her repeat herself -- the onset tier's
    top-ranked moments were "I'm so dead I'm so dead I'm so dead", "I don't
    understand" three times, and "No, no, no, no, no, no!". A fixed keyword
    list cannot anticipate which phrase gets repeated, but the repeating
    itself is the signal, so measure that directly.

    Returns (strength, phrase, count); strength weights repeat count by
    phrase length, so a trebled three-word phrase beats a doubled single word.
    """
    words = [w.strip(".,!?;:’'\"").lower() for w in text.split()]
    words = [w for w in words if w]
    n = len(words)
    best = (0, "", 0)
    for size in range(1, max_gram + 1):
        if n < size * 2:
            break
        counts = {}
        for i in range(n - size + 1):
            g = tuple(words[i:i + size])
            counts[g] = counts.get(g, 0) + 1
        for g, c in counts.items():
            if c < 2:
                continue
            strength = (c - 1) * size
            if strength > best[0]:
                best = (strength, " ".join(g), c)
    return best


def score_segment(text):
    """Heuristic reaction score for one line, plus which cues fired."""
    score, hits = 0.0, []
    for weight, pat in PATTERNS:
        m = pat.search(text)
        if m:
            score += weight
            hits.append(m.group(0)[:20])
    caps = CAPS.findall(text)
    if caps:
        score += 0.8 * min(len(caps), 3)
        hits.append("CAPS")

    strength, phrase, count = repetition(text)
    if strength >= 2:
        # capped: a long stuttering segment should not outscore everything
        score += min(strength * 0.6, 4.0)
        hits.append(f'x{count} "{phrase[:18]}"')
    return score, hits


def moments(segments, gap=12.0, min_score=2.5, top=40, clip_len=30.0,
            dedupe=45.0):
    """Cluster scored segments into candidate moments."""
    from .model import candidate

    scored = []
    for s in segments:
        sc, hits = score_segment(s["text"])
        if sc > 0:
            scored.append({**s, "score": sc, "hits": hits})

    # merge hits that are close in time -- a reaction is usually several lines
    clusters = []
    for s in scored:
        if clusters and s["start"] - clusters[-1]["end"] <= gap:
            c = clusters[-1]
            c["end"] = s["end"]
            c["score"] += s["score"]
            c["text"] += " " + s["text"]
            c["hits"] += s["hits"]
        else:
            clusters.append({"start": s["start"], "end": s["end"],
                             "score": s["score"], "text": s["text"],
                             "hits": list(s["hits"])})

    clusters.sort(key=lambda c: c["score"], reverse=True)
    picked, out = [], []
    for c in clusters:
        if c["score"] < min_score:
            break
        if any(abs(c["start"] - p) < dedupe for p in picked):
            continue
        picked.append(c["start"])
        span = max(c["end"] - c["start"], 8.0)
        out.append(candidate(
            source="speech", start=c["start"],
            duration=min(max(span + 6.0, 15.0), clip_len * 2),
            score=c["score"],
            title=c["text"][:70],
            detail={"cues": sorted(set(c["hits"]))[:6],
                    "quote": c["text"][:240]},
        ))
        if len(out) >= top:
            break
    return out


def save_transcript(segments, path):
    """Write both machine- and human-readable forms."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(segments, f, indent=1)
    txt = path.rsplit(".", 1)[0] + ".txt"
    with open(txt, "w", encoding="utf-8") as f:
        for s in segments:
            t = int(s["start"])
            f.write(f"[{t//3600:02d}:{(t%3600)//60:02d}:{t%60:02d}] {s['text']}\n")
    return txt
