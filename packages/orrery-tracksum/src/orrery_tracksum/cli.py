"""The eyeball loop — summarize runs (or one dip / one node trace) and print the result.

Kept as a first-class entry point on purpose. The catalog and the node prompt were
tuned by running a single artifact, reading the output, and adjusting; that loop is how
the compressed-catalog regression got caught (a condensed copy dropped the
discriminators paragraph and the model started reporting model-tiering as absent). A
prompt asset with no fast manual loop silently rots.

    python -m orrery_tracksum.cli runs <corpus_dir> [--out out/] [--model gemma4:26b]
    python -m orrery_tracksum.cli dip  <workflow.dip>
    python -m orrery_tracksum.cli node <node_trace.txt>

Requires `orrery-relay` and (for `runs`) tracker's `distill` on the path — pass its
directory with --distill-path.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from .grounding import check_grounding
from .reader import distill_reader
from .runs import build_index, summarize_run, summarize_runs
from .summarize import make_summarize_fn


def _relay(model: str):
    """Build a relay from env settings. Local-first: defaults to the ollama backend."""
    from orrery_relay import Relay

    class _S:
        anthropic_backend = os.environ.get("ANTHROPIC_BACKEND", "ollama")
        ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
        gateway_url = os.environ.get("GATEWAY_URL", "")
        gateway_api_key = os.environ.get("GATEWAY_API_KEY", "")
        aws_access_key = os.environ.get("AWS_ACCESS_KEY", "")
        aws_secret_key = os.environ.get("AWS_SECRET_KEY", "")
        aws_region = os.environ.get("AWS_REGION", "us-east-1")
        # Same pin as every other client of this Ollama server (empty -> default,
        # OLLAMA_NUM_CTX=0 disables pinning), or the clients fight over the loaded context.
        ollama_num_ctx = os.environ.get("OLLAMA_NUM_CTX", "").strip() or 32768
        ollama_keep_alive = os.environ.get("OLLAMA_KEEP_ALIVE", "").strip() or "30m"

    return make_summarize_fn(Relay.from_settings(_S()), model)


def _progress(event, **f):
    if event == "run_start":
        print("\n=== %s (%s)  run_id=%s  nodes=%d ===" % (
            f["run_label"], f["rung"], f["run_id"], f["nodes"]))
    elif event == "spec":
        print("  spec: %s (%d chars) fingerprint=%s" % (f["artifacts"], f["chars"], f["fingerprint"]))
    elif event == "dip":
        print("  dip: recognized (%d chars in); models (from IR)=%s" % (f["chars"], f["models"]))
    elif event == "node":
        flag = "⚠ " + str(f["ungrounded"]) if f["ungrounded"] else "ok"
        print("  node %-16s grounding %d/%d %s" % (f["node"], f["grounded"], f["named"], flag))
    elif event == "coherency":
        print("  coherency: completeness=%s (mandatory %d summarized, %d missing) | conditional fired %s" % (
            f["completeness_pass"], len(f["mandatory_agent_nodes"]) - len(f["missing_mandatory"]),
            len(f["missing_mandatory"]), f["conditional_fired"] or "none"))


_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(label) -> str:
    """A single filesystem-safe segment derived from an untrusted run label.

    Everything outside [A-Za-z0-9._-] collapses to `_`, which removes path separators
    outright, and pure dot segments (".", "..") are rejected rather than sanitised —
    they name a directory, not a file. Mirrors the rule the ingest job applies to the
    same value, so a bundle written here is one ingestion will accept.
    """
    text = _SAFE_LABEL_RE.sub("_", str(label or "").strip())
    text = text.strip("._") or "run"
    return text[:100]


def _cmd_runs(a) -> int:
    if a.distill_path:
        sys.path.insert(0, a.distill_path)
    try:
        import distill
    except ImportError:
        sys.exit("tracker's distill module not importable — pass --distill-path <dir>")

    summarize_fn = _relay(a.model)
    reader = distill_reader(distill)
    runs = reader.find_runs(a.root)
    if not runs:
        sys.exit("no runs found beneath %s" % a.root)
    print("summarizing %d run(s) with %s" % (len(runs), a.model))

    bundles = summarize_runs(a.root, summarize_fn, reader, on_progress=_progress)

    written_names: dict[int, str] = {}
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        emitted: set[str] = set()
        for i, b in enumerate(bundles):
            # `run_label` comes from the CORPUS, so it is untrusted for filesystem use:
            # `../name` escapes --out entirely and an absolute value makes os.path.join
            # discard --out. Two runs sharing a label silently overwrote each other's
            # bundle, losing a run with no error. The ingest side already validated
            # labels; the producer did not, which left the gap open at the source.
            base = _safe_filename(b["run_label"])
            # Track every EMITTED name, not just the base. Counting per base meant the
            # labels `foo`, `foo`, `foo~2` produced `foo`, `foo~2`, `foo~2` — the third
            # silently overwriting the second, which is the exact loss the suffix was
            # added to prevent.
            name, n = base, 1
            while name in emitted:
                n += 1
                name = f"{base}~{n}"
            if name != base:
                print("  ! duplicate output name for run_label %r — writing %s.json"
                      % (b["run_label"], name))
            emitted.add(name)
            written_names[i] = name + ".json"
            with open(os.path.join(a.out, name + ".json"), "w") as fh:
                json.dump(b, fh, indent=1)
        # Built AFTER writing, so it can record the names actually emitted.
        with open(os.path.join(a.out, "index.json"), "w") as fh:
            json.dump(build_index(bundles, written_names), fh, indent=1)
        print("\nwrote %d bundle(s) + index.json to %s" % (len(bundles), a.out))

    index = build_index(bundles, written_names)
    print("\n=== INDEX ===")
    for e in index:
        print("  %-6s %-10s nodes=%-2d dip=%s completeness=%s" % (
            e["run_label"], e["rung"], e["nodes"], e["dip_recognized"], e["completeness"]))
    return 0


def _cmd_one(a, level: str) -> int:
    with open(a.path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    out = _relay(a.model)(level, content=content)
    print("MODEL: %s   %s: %s" % (a.model, level, os.path.basename(a.path)))
    print("=" * 70)
    print(out)
    if level == "node":
        g = check_grounding(out, content)
        print("\n" + "-" * 70)
        print("GROUNDING: %d path(s) named, %d grounded" % (g["named"], g["grounded"]))
        print("  ⚠ UNGROUNDED: %s" % g["ungrounded"] if g["ungrounded"]
              else "  ✓ no ungrounded file-paths — faithful on artifacts")
    return 0


def main(argv=None) -> int:
    # --model must work on BOTH sides of the subcommand: on the top level alone the
    # usage this module's own docstring documents — `... runs <dir> --model X` — exited
    # with SystemExit 2. It is registered twice, and the subparser copy uses SUPPRESS:
    # with a real default there, argparse would OVERWRITE a value given before the
    # subcommand with the subparser's default, silently ignoring `--model X runs ...`.
    # SUPPRESS means the subparser sets the attribute only when the flag is actually
    # present, so whichever side supplies it wins and neither erases the other.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model", default=argparse.SUPPRESS,
                        help="model for summarization (default $EXTRACTION_MODEL)")

    ap = argparse.ArgumentParser(prog="orrery_tracksum.cli")
    ap.add_argument("--model", default=os.environ.get("EXTRACTION_MODEL", "gemma4:26b"),
                    help="model for summarization (accepted before or after the subcommand)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("runs", parents=[common],
                       help="summarize every run beneath a corpus dir")
    p.add_argument("root")
    p.add_argument("--out", default=None, help="write per-run JSONs + index.json here")
    p.add_argument("--distill-path", default=None, help="dir containing tracker's distill.py")

    for name in ("dip", "node"):
        q = sub.add_parser(name, parents=[common],
                           help="summarize a single %s artifact" % name)
        q.add_argument("path")

    a = ap.parse_args(argv)
    if a.cmd == "runs":
        return _cmd_runs(a)
    return _cmd_one(a, a.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
