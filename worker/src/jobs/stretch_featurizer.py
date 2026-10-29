# ABOUTME: Batched LLM featurizer that turns transcript stretches into structured facets.
# ABOUTME: Pure of DB I/O; the relay is injected. quote_ok is computed by code, not trusted.
import json
import logging
import re

SYSTEM_PROMPT = """You read short stretches of noisy office-microphone transcript (speech-to-text from
open-office mics: fragments, mis-hearings, cross-talk) and write a careful facet for EACH stretch.

Topic tags are short, specific lowercase phrases (2-5 words) naming the subject: a particular
tool, project, demo, analogy, personal or social topic.

For every stretch return:
- leaf_id: copied exactly.
- summary: 2-4 coherent sentences on what is being discussed and what is said about it. If the stretch is
  pure chatter, greetings, or unintelligible, return exactly "none" (and empty lists below).
- topics: 1-3 entries, one per distinct topic the stretch touches: {"tag": topic tag,
  "statement": one sentence on what was said about THAT topic}.
- ideas: 0-3 notable items: {"kind": "proposal"|"question"|"decision"|"problem", "text": one sentence,
  "quote": a span copied VERBATIM from the stretch (<= 20 words) that shows it, "tag": the topic tag it
  belongs to}. Only include real ideas; an empty list is fine.
- nouns: concrete tools, products, projects, or systems mentioned (not people).
- quote: one span copied VERBATIM from the stretch (<= 20 words) supporting the summary ("" for chatter).
Never put people's names in summaries, statements, ideas, or tags.
Return {"facets": [...]} with one entry per stretch, in order."""

IDEA_KINDS = {"proposal", "question", "decision", "problem"}
BATCH_SUFFIX = 'Respond with strict JSON only: {"facets": [...]}'


def _hhmm(start) -> str:
    m = re.search(r"(\d{1,2}):(\d{2})", str(start))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else str(start)


def build_prompt(stretches: list[dict]) -> str:
    blocks = [f"[{s['stretch_id']}] ({s['mic_id']} {_hhmm(s['start'])})\n{s['text']}" for s in stretches]
    return f"{SYSTEM_PROMPT}\n\nSTRETCHES:\n\n" + "\n\n".join(blocks) + f"\n\n{BATCH_SUFFIX}"


def _norm(t: str) -> str:
    return " ".join(str(t).lower().split())


def normalize_facet(raw: dict, stretch_text: str) -> dict:
    summary = raw.get("summary") if isinstance(raw.get("summary"), str) else ""
    summary = summary.strip()
    concepts, ideas, nouns = [], [], []
    for c in raw.get("topics") or [] if isinstance(raw.get("topics"), list) else []:
        if isinstance(c, dict) and isinstance(c.get("tag"), str) and c["tag"].strip():
            st = c.get("statement")
            concepts.append({"tag": c["tag"].strip(), "statement": st if isinstance(st, str) else ""})
    haystack = _norm(stretch_text)
    for i in raw.get("ideas") if isinstance(raw.get("ideas"), list) else []:
        if not isinstance(i, dict) or i.get("kind") not in IDEA_KINDS or not isinstance(i.get("text"), str):
            continue
        quote = i.get("quote") if isinstance(i.get("quote"), str) else ""
        nq = _norm(quote)
        ideas.append({"kind": i["kind"], "text": i["text"], "quote": quote,
                      "quote_ok": bool(nq) and nq in haystack})
    for n in raw.get("nouns") if isinstance(raw.get("nouns"), list) else []:
        if isinstance(n, str) and n.strip():
            nouns.append(n.strip())
    if summary == "none":
        concepts, ideas, nouns = [], [], []
    return {"summary": summary, "concepts": concepts, "ideas": ideas, "nouns": nouns}


def _parse(text: str) -> list:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0] if "\n" in text else ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    facets = parsed.get("facets") if isinstance(parsed, dict) else None
    return facets if isinstance(facets, list) else []


log = logging.getLogger(__name__)


async def _call(relay, model: str, stretches: list[dict]) -> dict[str, dict]:
    try:
        response = await relay.complete(
            model=model, max_tokens=8192, format="json",
            messages=[{"role": "user", "content": build_prompt(stretches)}],
        )
    except Exception as e:
        log.warning("stretch featurizer relay call failed (%d stretches): %s", len(stretches), e)
        return {}
    by_id = {s["stretch_id"]: s for s in stretches}
    out = {}
    for raw in _parse(getattr(response, "text", "")):
        if not isinstance(raw, dict):
            continue
        sid = raw.get("leaf_id", raw.get("stretch_id"))
        if isinstance(sid, str) and sid in by_id and sid not in out and isinstance(raw.get("summary"), str):
            out[sid] = normalize_facet(raw, by_id[sid]["text"])
    return out


async def enrich_batch(relay, model: str, stretches: list[dict]) -> dict[str, dict]:
    result = await _call(relay, model, stretches)
    missing = [s for s in stretches if s["stretch_id"] not in result]
    if missing:
        result.update(await _call(relay, model, missing))
    return result
