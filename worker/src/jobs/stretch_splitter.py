# ABOUTME: Deterministic splitter that cuts one transcript document into "stretches".
# ABOUTME: Pure function, no I/O; stretches are later LLM-enriched by another job.
import hashlib
import re

PAUSE_SECONDS = 120      # a gap strictly greater than this starts a new stretch
MAX_SECONDS = 240        # a stretch may not span more than this (first -> last line)
MAX_WORDS = 120          # a stretch may not hold more than this many words
MIN_WORDS = 15           # stretches with fewer words are dropped

_LINE_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]\s*(.*)$")


def _parse(content: str) -> list[tuple[str, int, int, str]]:
    """Return (raw_line, seconds, word_count, hhmmss) for each timestamped line."""
    out = []
    for raw in content.splitlines():
        m = _LINE_RE.match(raw.strip())
        if not m:
            continue
        h, mi, s, text = int(m[1]), int(m[2]), int(m[3]), m[4]
        out.append((raw.strip(), h * 3600 + mi * 60 + s, len(text.split()),
                    f"{m[1]}:{m[2]}:{m[3]}"))
    return out


def _delta(later: int, earlier: int) -> int:
    d = later - earlier
    return d + 86400 if d < 0 else d  # tolerate a midnight wrap


def split_document(doc_id: str, mic_id: str, content: str, day: str) -> list[dict]:
    """`day` is the office-local date (YYYY-MM-DD, from documents.metadata.day); start/end are
    composed as full local ISO datetimes `{day}THH:MM:SS` so event-time windows work across days."""
    groups: list[list[tuple[str, int, int, str]]] = []
    cur: list[tuple[str, int, int, str]] = []
    words = 0
    for line in _parse(content):
        _, secs, n, _ = line
        if cur:
            gap = _delta(secs, cur[-1][1])
            span = _delta(secs, cur[0][1])
            if gap > PAUSE_SECONDS or span > MAX_SECONDS or words + n > MAX_WORDS:
                groups.append(cur)
                cur, words = [], 0
        cur.append(line)
        words += n
    if cur:
        groups.append(cur)

    result = []
    for g in groups:
        if sum(l[2] for l in g) < MIN_WORDS:
            continue
        text = "\n".join(l[0] for l in g)
        result.append({
            "stretch_id": f"{doc_id}:{len(result)}",
            "doc_id": doc_id,
            "mic_id": mic_id,
            "start": f"{day}T{g[0][3]}",
            "end": f"{day}T{g[-1][3]}",
            "text": text,
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        })
    return result
