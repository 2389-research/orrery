# ABOUTME: Ingest tracker code-gen runs into the graph — one collection per run.
# ABOUTME: Summarizes raw runs via orrery-tracksum (or takes a pre-made bundle), wires the
# ABOUTME: trajectory as collection_edges, enqueues extract_batch. Mirrors ingest_repo.

import asyncio
import hashlib
import json
import os
import re
import sys
import uuid

from orrery_relay import Relay
from orrery_tracksum import distill_reader, gather_spec, make_summarize_fn, summarize_runs, working_dir_of
from ..classifier import classify_document
from ..config import get_settings
from ..db import get_connection, mark_graph_dirty
from ..summarization_relay import build_summarization_relay
from ..summarization_route import resolve_summarization
from ..silo import flow_default_kind, resolve_kind

# Ground-truth chain order for the spec-degradation ladder. In production a chain
# is INFERRED (spec-embedding similarity + temporal order + outcome); here we know
# it, so we hardcode the rung order as a stand-in the inference later replaces.
_RUNG_ORDER = {"R6-brief": 0, "R5": 1, "R4-repair": 2, "R3": 2, "R0": 3}

# A run_label is DATA (it comes out of a summary JSON) but gets used as a path segment
# under runs_dir, as the UNIQUE `collections.path` key, and as a document-title prefix. So it
# must be a single safe segment — the same rule scripts/repo-run/prepare.sh already
# applies to repo names for exactly this reason ("a value like '../..' can't escape").
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_label(label) -> bool:
    return (
        isinstance(label, str)
        and label not in (".", "..")
        and bool(_SAFE_LABEL_RE.match(label))
    )


def _parent_of(domain_path: str | None) -> str | None:
    if not domain_path or "/" not in domain_path:
        return None
    return domain_path.rsplit("/", 1)[0]


def _spec_text(run: dict) -> str:
    """The run's spec prose — from the bundle, or re-read from the run's checkout.

    The embedded `text` is the normal path (it works in-container, where the original
    checkout isn't mounted). The fallback re-reads via tracksum's `gather_spec`, which
    resolves only the names in its own SPEC_ARTIFACTS allowlist — deliberately NOT the
    `spec.artifacts` list from the JSON, since that is caller-supplied data and joining
    it onto a directory would read whatever it names.
    """
    spec = run.get("spec") or {}
    if spec.get("text"):
        return spec["text"].strip()
    source_path = run.get("source_path")
    if not source_path:
        return ""
    return gather_spec(working_dir_of(source_path))["text"].strip()


def _first_existing(*candidates: str | None) -> str | None:
    """First candidate path that actually resolves, else None.

    source_path is only useful if GET /documents/{id}/file can open it, so a dangling
    path is worse than no path — it advertises a drill-down that 404s.
    """
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _run_docs(run: dict, runs_dir: str):
    """Yield (title, content, source_path, role, parent_path) for a run's pieces.

    `role` is the collection-neutral structural role ('root' | 'group' | 'leaf'), NOT
    the old `level`. A run used to label its spec a "file" purely to opt into
    co-occurrence — the one behaviour `level == 'file'` gated. That coupling is gone,
    so a run can now describe its own shape honestly.

    source_path points at the raw artifact so GET /documents/{id}/file can serve the
    territory — the same drill-down repos get. Two candidates, in order: the run's own
    directory (resolvable when the worker summarized raw runs itself) then the STAGED
    copy under runs_dir (needed when the bundle was summarized elsewhere and the
    original checkout isn't visible in-container).

    The tree: rollup = root; spec and dip = leaves at the run root; each node summary =
    a leaf under `trace`. Only leaves emit co-occurrence, which is now stated rather
    than inferred from a filesystem label.
    """
    label = run["run_label"]
    staged = os.path.join(runs_dir, label)  # e.g. /data/tracker-ingest/runs/run3
    raw = run.get("source_path") or ""

    rollup = run.get("rollup") or ""
    if rollup.strip():
        # The run's own overview — the collection root, same position a repo-level
        # summary holds. Composed from the bundle, so there is no raw file.
        yield (f"{label} overview", rollup, None, "root", None)

    spec_text = _spec_text(run)
    if spec_text:
        # A spec can be several artifacts (BRIEF + SPEC + CONTRACT), so there is no one
        # raw file to point at; the staged copy is a single concatenated spec.md.
        yield (f"{label} spec", spec_text,
               _first_existing(os.path.join(staged, "spec.md")), "leaf", ".")

    dip_rec = (run.get("dip") or {}).get("recognized")
    if dip_rec and dip_rec.strip():
        dip_path = _first_existing(
            os.path.join(raw, "workflow.dip") if raw else None,
            os.path.join(staged, "workflow.dip"),
        )
        yield (f"{label} dip", dip_rec, dip_path, "leaf", ".")

    # Trace nodes are the one part of a run that is NOT a filesystem: one
    # activity.jsonl distilled into many documents whose order is time, not
    # hierarchy. They still sit in the tree — under a `trace` path so they group
    # together and don't crowd the run root — while their ORDER stays where order
    # belongs, on the run→run `chain_next` edges.
    for n in run.get("nodes", []):
        summ = n.get("summary") or ""
        if summ.strip():
            yield (f"{label}/{n['node']}", summ, None, "leaf", "trace")  # raw trace not staged


def _tracksum_reader(settings):
    """A TraceReader over tracker's `distill`, or a clear failure explaining the fix.

    orrery-tracksum deliberately does not vendor tracker's activity.jsonl parser — that
    schema belongs to tracker and drifts on its own schedule (see the package's
    reader.py). So raw-run summarization needs distill importable.
    """
    if settings.tracker_distill_path:
        sys.path.insert(0, settings.tracker_distill_path)
    try:
        import distill  # noqa: PLC0415 — optional, resolved at call time
    except ImportError as e:
        raise RuntimeError(
            "cannot summarize raw tracker runs: tracker's `distill` module is not "
            "importable. Either set TRACKER_DISTILL_PATH to the directory containing "
            "distill.py, or ingest a pre-made summary bundle (a directory holding "
            "index.json + one JSON per run, produced by "
            "`python -m orrery_tracksum.cli runs <corpus> --out <dir>`)."
        ) from e
    return distill_reader(distill)


def _load_bundles(config: dict, relay, settings, db_path: str | None = None) -> list[dict]:
    """Get per-run summary bundles, either pre-made or summarized here and now.

    Bundle mode (an `index.json` is present) skips the model entirely — the summaries
    already exist. Raw mode is the codesum-shaped path: summarize each run with the
    summarization model through the relay, exactly as ingest_repo summarizes a checkout.
    """
    out_dir = config.get("out_dir")
    index_path = os.path.join(out_dir, "index.json") if out_dir else None

    if index_path and os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
        # Valid JSON is not a valid index. `{}` iterates its string KEYS (so `row.get`
        # raises AttributeError), and `42` raises TypeError on iteration — neither is
        # caught by a ValueError handler, so both surfaced as an opaque crash rather than
        # "your index is malformed".
        if not isinstance(index, list):
            raise RuntimeError(
                f"ingest_tracker_runs: {index_path} must contain a JSON list of run "
                f"objects, got {type(index).__name__}")
        bundles = []
        for row in index:
            if not isinstance(row, dict):
                print(f"[ingest_tracker_runs] skipping non-object index row {row!r}", flush=True)
                continue
            label = row.get("run_label")
            # index.json is data; a label is only ever a filename inside out_dir.
            if not _safe_label(label):
                print(f"[ingest_tracker_runs] skipping unsafe run_label {label!r}", flush=True)
                continue
            # Prefer the filename the writer actually emitted. The writer sanitises and
            # de-duplicates names (`../x` -> `_x.json`, a repeated `foo` -> `foo~2.json`),
            # so reconstructing `<run_label>.json` missed those files and read `foo.json`
            # twice. Falls back to the label for indexes written before `file` existed.
            fname = row.get("file")
            if not (isinstance(fname, str) and _safe_label(fname)):
                fname = label + ".json"
            rp = os.path.join(out_dir, fname)
            if os.path.exists(rp):
                with open(rp, encoding="utf-8") as f:
                    bundles.append(json.load(f))
        print(f"[ingest_tracker_runs] {len(bundles)} pre-made bundle(s) from {out_dir}", flush=True)
        return bundles

    raw_root = config.get("raw_root") or out_dir
    if not raw_root:
        raise RuntimeError("ingest_tracker_runs: neither out_dir nor raw_root given")

    relay_s, model = (relay, settings.summarization_model) if db_path is None else resolve_summarization(
        settings, db_path, local_relay=relay, summ_relay=build_summarization_relay(settings))
    print(f"[ingest_tracker_runs] summarizing raw runs beneath {raw_root} with {model} ...", flush=True)
    bundles = summarize_runs(
        raw_root,
        make_summarize_fn(relay_s, model),
        _tracksum_reader(settings),
        on_progress=lambda event, **f: print(f"[tracksum] {event} {f}", flush=True),
    )
    print(f"[ingest_tracker_runs] summarized {len(bundles)} run(s)", flush=True)
    return bundles


async def run_ingest_tracker_runs(job: dict, db_path: str) -> None:
    settings = get_settings()
    relay = Relay.from_settings(settings)

    config = json.loads(job["config"]) if job["config"] else {}
    spec_id = config["spec_id"]
    base = config.get("out_dir") or config.get("raw_root") or ""
    # Container-resolvable dir of STAGED raw artifacts (workflow.dip, spec.md per run),
    # so GET /documents/{id}/file can serve the territory — same drill-down as repos.
    runs_dir = config.get("runs_dir") or os.path.join(os.path.dirname(base), "runs")

    # Phase 0: the summaries — pre-made, or produced here via orrery-tracksum.
    #
    # Off the event loop, for the same reason ingest_repo moves summarize_repo: in RAW
    # mode this issues one blocking `complete_sync` per node, spec and rollup across the
    # whole corpus, and `poll_loop` awaits `handle_job` inline — so running it here would
    # freeze job polling and the judge sweep for the entire summarization. Bundle mode is
    # only file reads and would not need this, but the call site is shared and paying a
    # thread hand-off for a few reads is not worth branching over.
    runs = await asyncio.to_thread(_load_bundles, config, relay, settings, db_path)
    # A label reaches the filesystem (staged artifacts) and `collections.path`; drop any run
    # whose label isn't a single safe segment rather than trusting bundle data.
    skipped = [r.get("run_label") for r in runs if not _safe_label(r.get("run_label"))]
    if skipped:
        print(f"[ingest_tracker_runs] skipping {len(skipped)} run(s) with unsafe labels: {skipped}", flush=True)
        runs = [r for r in runs if _safe_label(r.get("run_label"))]
    if not runs:
        raise RuntimeError(f"ingest_tracker_runs: no runs found for {base!r}")

    # Existing taxonomy for classification (empty in a fresh noosphere).
    conn = get_connection(db_path)
    try:
        taxonomy = [r[0] for r in conn.execute("SELECT path FROM domains").fetchall()]
    finally:
        conn.close()

    # Classify each run on its SPEC (the problem statement) — like ingest_repo
    # classifies on the repo summary. This small corpus converges to ~one domain.
    run_domain: dict[str, str] = {}
    for run in runs:
        label = run["run_label"]
        excerpt = f"Run {label} — problem spec\n\n{_spec_text(run)[:4000]}"
        cls = await classify_document(
            relay=relay, title=label, excerpt=excerpt,
            existing_taxonomy=taxonomy, model=settings.classification_model,
        )
        run_domain[label] = cls["primary_domain"]
        if cls["primary_domain"] not in taxonomy:
            taxonomy.append(cls["primary_domain"])  # let later runs reuse it
        print(f"[ingest_tracker_runs] {label} -> {run_domain[label]}", flush=True)

    # Chain order: explicit config.chain, else the ground-truth rung order.
    chain = config.get("chain") or [
        r["run_label"] for r in sorted(runs, key=lambda r: _RUNG_ORDER.get(r.get("rung"), 99))
    ]

    conn = get_connection(db_path)
    try:
        # PREFLIGHT every label before writing anything. `collections.path` is UNIQUE, so
        # a label that already exists raises mid-loop — after earlier runs are inserted —
        # which is the partial ingest with silently incomplete chain edges that the route
        # comment claims to prevent. The route can only check when the caller supplies an
        # explicit `chain`, and in raw mode the labels are not even known until the runs
        # are summarized, so THIS is the check that always runs. The route's is an early,
        # friendlier 409; this one is the guarantee.
        labels = [r["run_label"] for r in runs]
        rows = conn.execute(
            "SELECT path FROM collections WHERE path IN (%s)"
            % ",".join("?" * len(labels)), labels).fetchall()
        clash = sorted(r[0] for r in rows)
        if clash:
            raise RuntimeError(
                f"ingest_tracker_runs: collections already exist for run label(s) "
                f"{', '.join(clash)} — refusing to ingest, since a partial trajectory "
                f"would have incomplete chain_next edges without saying so. Delete those "
                f"collections or ingest under different labels.")
        # A duplicate WITHIN this corpus is the same hazard from the other direction: the
        # second insert would collide with the first after both were accepted.
        dupes = sorted({lbl for lbl in labels if labels.count(lbl) > 1})
        if dupes:
            raise RuntimeError(
                f"ingest_tracker_runs: duplicate run label(s) in this corpus: "
                f"{', '.join(dupes)}")

        # An explicit chain must describe exactly the runs that were loaded. The edge
        # loop silently skips members it cannot resolve, so a typo or a duplicate in
        # `config["chain"]` completed the job with MISSING chain_next edges — a
        # trajectory quietly shorter than the corpus, which is the failure this job
        # exists to represent faithfully.
        if config.get("chain"):
            requested = list(config["chain"])
            unknown = sorted(set(requested) - set(labels))
            dup = sorted({c for c in requested if requested.count(c) > 1})
            missing = sorted(set(labels) - set(requested))
            if unknown or dup or missing:
                raise RuntimeError(
                    "ingest_tracker_runs: the explicit chain does not match the loaded "
                    f"runs — unknown={unknown or '[]'} duplicated={dup or '[]'} "
                    f"omitted={missing or '[]'}. Every loaded run must appear exactly "
                    "once, or the trajectory would be silently incomplete.")

        # Every run in this corpus is a `tracker_run` collection, so the flow default
        # (and any explicit override in the job config) is the same for all of them —
        # resolve once rather than per iteration.
        run_kind = resolve_kind(flow_default_kind("tracker_run"), config.get("provenance_kind"))
        collection_ids: dict[str, str] = {}
        for run in runs:
            label = run["run_label"]
            collection_id = str(uuid.uuid4())
            collection_ids[label] = collection_id
            # root_path mirrors source_path resolution: the run's own dir when the
            # worker can see it, else the staged copy.
            root_path = _first_existing(
                run.get("source_path"), os.path.join(runs_dir, label),
            ) or os.path.join(runs_dir, label)
            conn.execute(
                "INSERT INTO collections (id, name, path, root_path, document_count, kind, "
                "provenance_kind) VALUES (?, ?, ?, ?, 0, 'tracker_run', ?)",
                (collection_id, label, label, root_path, run_kind),
            )
            dom = run_domain[label]
            for title, content, source_path, role, parent_path in _run_docs(run, runs_dir):
                doc_id = str(uuid.uuid4())
                content_hash = hashlib.sha256(content.encode()).hexdigest()
                conn.execute(
                    "INSERT INTO documents (id, title, content, content_hash, source_path, content_type, status) "
                    "VALUES (?, ?, ?, ?, ?, 'code_intent', 'classified')",
                    (doc_id, title, content, content_hash, source_path),
                )
                conn.execute(
                    "INSERT INTO chunks (id, document_id, chunk_index, text, offset, length) VALUES (?, ?, 0, ?, 0, ?)",
                    (str(uuid.uuid4()), doc_id, content, len(content)),
                )
                conn.execute(
                    # role + emits_cooccurrence are stated explicitly; this schema has
                    # no `level` column to conflate them back together.
                    "INSERT INTO document_collections (document_id, collection_id, parent_path, role, "
                    "emits_cooccurrence) VALUES (?, ?, ?, ?, ?)",
                    (doc_id, collection_id, parent_path, role, int(role == "leaf")),
                )
                conn.execute("UPDATE collections SET document_count = document_count + 1 WHERE id = ?", (collection_id,))
                conn.execute(
                    "INSERT OR IGNORE INTO domains (id, path, parent_path, document_count) VALUES (?, ?, ?, 0)",
                    (str(uuid.uuid4()), dom, _parent_of(dom)),
                )
                conn.execute(
                    "INSERT INTO document_domains (document_id, domain_path, is_primary, confidence) VALUES (?, ?, 1, 1.0)",
                    (doc_id, dom),
                )
                conn.execute("UPDATE domains SET document_count = document_count + 1 WHERE path = ?", (dom,))

        # Trajectory: connect consecutive runs in the chain at the repo layer.
        edges = 0
        for a, b in zip(chain, chain[1:]):
            if a in collection_ids and b in collection_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO collection_edges (source, target, type, weight) VALUES (?, ?, 'chain_next', 1.0)",
                    (collection_ids[a], collection_ids[b]),
                )
                edges += 1

        # Phase 2: extract entities + co-occurrence over the new code_intent docs.
        conn.execute(
            "INSERT INTO jobs (id, type, target, status, config) VALUES (?, 'extract_batch', ?, 'queued', ?)",
            (str(uuid.uuid4()), "tracker-runs", json.dumps({"spec_id": spec_id, "scope": "code_intent"})),
        )

        result = {"runs": len(runs), "chain": chain, "chain_edges": edges,
                  "domains": run_domain}
        conn.execute("UPDATE jobs SET result = ? WHERE id = ?", (json.dumps(result), job["id"]))
        mark_graph_dirty(conn)
        conn.commit()
        print(f"[ingest_tracker_runs] done: {len(runs)} collections, {edges} chain edges — enqueued extract_batch", flush=True)
    finally:
        conn.close()
