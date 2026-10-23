# ABOUTME: Pure resolver: which repository / body of work was each agent in a session working on.
# ABOUTME: Works only from meridian records + rollups (no filesystem, git, or network access).
import posixpath
import re
from collections import Counter

ANCHOR_SHARE = 0.80


def normalize_remote(url):
    """Normalize a git remote to ``host/owner/name`` (lowercase host, no scheme/creds/.git)."""
    if not url or not str(url).strip():
        return None
    s = str(url).strip()
    stripped = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", s)
    had_scheme = stripped != s
    s = stripped
    if "@" in s.split("/", 1)[0]:
        s = s.split("@", 1)[1]
    head, _, tail = s.partition("/")
    if ":" in head and not had_scheme:  # scp-like host:owner/name
        host, _, first = head.partition(":")
        rest = first + ("/" + tail if tail else "")
    else:
        host, rest = head.split(":", 1)[0], tail  # drops any :port
    rest = rest.strip("/")
    if rest.endswith(".git"):
        rest = rest[:-4]
    if not host or not rest:
        return None
    return f"{host.lower()}/{rest}"


_PR_SEG = re.compile(r"/(?:-/)?(pull|pulls|merge_requests)/(\d+)(?=[/?#]|$)")


def _norm_pr(url):
    """PR/MR URL -> (normalized link, repo key host/owner/name) or None.

    Only URLs with a recognized /pull/<n>, /pulls/<n> or /merge_requests/<n> segment
    count; anything else (project boards, Jira tickets, ...) is not repo evidence.
    """
    if not url:
        return None
    s = str(url).strip()
    m = _PR_SEG.search(s)
    if not m:
        return None
    key = normalize_remote(s[:m.start()])
    if not key or len(key.split("/")) < 3:
        return None
    return f"{key}/{m.group(1)}/{m.group(2)}", key


def _inside(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _ancestors(path):
    out = []
    while True:
        out.append(path)
        parent = posixpath.dirname(path)
        if parent == path or not parent:
            break
        path = parent
    return out


def _git_state(raw):
    gs = (((raw.get("serverClassifierContext") or {}).get("context") or {}).get("git_state"))
    if not isinstance(gs, dict) or not isinstance(gs.get("root"), str) or not gs["root"]:
        return None, None
    origin = ((gs.get("visibility") or {}).get("origin")) or {}
    remote = None
    if origin.get("host") and origin.get("remote"):
        remote = normalize_remote(f"{origin['host']}/{origin['remote']}")
    return gs["root"].rstrip("/") or "/", remote


def _anchor(cwds):
    """Deepest dir containing >= 80% of the cwd list. Returns (dir, share) or (None, 0.0)."""
    if not cwds:
        return None, 0.0
    counts = Counter()
    for c in cwds:
        for a in _ancestors(c):
            counts[a] += 1
    total = len(cwds)
    best = None
    for d, n in counts.items():
        if n / total >= ANCHOR_SHARE:
            depth = d.count("/")
            if best is None or depth > best[0]:
                best = (depth, d, n / total)
    return (best[1], best[2]) if best else (None, 0.0)


def _fold_roots(roots):
    """roots: {root: remote|None}. Returns (fold_map root->outer, remote_by_outer)."""
    fold = {}
    ordered = sorted(roots, key=lambda r: r.count("/"))
    for r in ordered:
        fold[r] = r
        for outer in ordered:
            if outer == r or not _inside(r, outer) or fold[outer] != outer:
                continue
            ro, rr = roots[outer], roots[r]
            if ro is None or rr is None or ro == rr:
                fold[r] = outer
                break
    remotes = {}
    for r, outer in fold.items():
        if roots[r] and not remotes.get(outer):
            remotes[outer] = roots[r]
        remotes.setdefault(outer, roots[outer])
    for r in fold.values():
        if roots[r] and not remotes.get(r):
            remotes[r] = roots[r]
    return fold, remotes


def _root_for(path, roots):
    """Longest known (unfolded) root containing path."""
    best = None
    for r in roots:
        if _inside(path, r) and (best is None or len(r) > len(best)):
            best = r
    return best


def _pr_info(agent, session):
    """-> (links, repo key, own). own=False when the PR came from the session fallback."""
    # Falling back from the agent's pr_links to the session's is intentional: a stub
    # agent with no evidence of its own inherits the session's PR repo. The caller
    # only lets an inherited PR set the key when the agent has no conflicting identity.
    for src, own in ((agent, True), (session, False)):
        links = (((src or {}).get("rollup") or {}).get("outputs") or {}).get("pr_links") or []
        norm = [n for n in (_norm_pr(x) for x in links) if n]
        if norm:
            return [n[0] for n in norm], norm[0][1], own
    return [], None, True


def _ctx(src, field):
    return ((((src or {}).get("rollup") or {}).get("context")) or {}).get(field) or (src or {}).get(field)


def _repo_identity(root, fold, remotes):
    """-> (key, source, remote, outer_root, name) for a known root."""
    outer = fold.get(root, root)
    remote = remotes.get(outer)
    if remote:
        return remote, "git_remote", remote, outer, remote.rsplit("/", 1)[-1]
    name = posixpath.basename(outer.rstrip("/")) or outer
    return name, "git_name", None, outer, name


def _unit(key, source, remote, root, name, sub, anchor, share, branch, prs, units):
    return {"key": key, "source": source, "repo_remote": remote, "repo_root": root,
            "repo_name": name, "subproject": sub, "anchor_dir": anchor,
            "anchor_share": round(share, 4), "git_branch": branch, "pr_links": prs,
            "units": units}


def resolve_work_units(agents, session, records):
    agents = [a for a in agents if a.get("agent_id")]  # one malformed agent can't crash the resolve
    by_agent = {}
    roots = {}
    for r in records:
        raw = r.get("raw") or {}
        by_agent.setdefault(r.get("agent_id") or "main", []).append(raw)
        root, remote = _git_state(raw)
        if root:
            if root not in roots or (remote and not roots[root]):
                roots[root] = remote
    fold, remotes = _fold_roots(roots)

    main_id = next((a["agent_id"] for a in agents if a.get("agent_id") == "main"), None)
    if main_id is None:
        main_id = next((a["agent_id"] for a in agents if not a.get("parent_agent_id")), None)

    out = {}
    for ag in agents:
        aid = ag["agent_id"]
        raws = by_agent.get(aid, [])
        cwds = [r["cwd"].rstrip("/") or "/" for r in raws if r.get("cwd")]
        prs, pr_key, pr_own = _pr_info(ag, session)
        branch = _ctx(ag, "git_branch") or _ctx(session, "git_branch")
        is_main = aid == main_id

        # Assign each record to a known root (by cwd containment).
        share_by_root = Counter(_root_for(c, roots) for c in cwds)
        share_by_root.pop(None, None)
        total = len(cwds)

        if cwds:
            primary_root = max(share_by_root, key=lambda r: (share_by_root[r], r)) if share_by_root else None
            if primary_root:
                in_root = [c for c in cwds if _root_for(c, roots) == primary_root]
                anchor, _ = _anchor(in_root)
                anchor = anchor or primary_root
                share = sum(1 for c in cwds if _inside(c, anchor)) / total
            else:
                anchor, share = _anchor(cwds)
        else:
            primary_root = None
            anchor = (_ctx(ag, "cwd") or _ctx(session, "cwd") or None)
            anchor = anchor.rstrip("/") or "/" if anchor else None
            share = 0.0
            if anchor:
                primary_root = _root_for(anchor, roots)

        # Primary identity.
        sub = None
        if primary_root:
            key, source, remote, outer, name = _repo_identity(primary_root, fold, remotes)
            repo_root = outer
        else:
            base = posixpath.basename(anchor.rstrip("/")) if anchor else ""
            key, source, remote, repo_root, name = (base or "unresolved"), "folder", None, None, (base or None)
        # An inherited (session) PR must not override a subagent's own, different repo.
        # The main agent IS the session, so its fallback counts as its own.
        if pr_key and not (pr_own or is_main) and primary_root and remote != pr_key:
            pr_key = None
        if pr_key:
            # A root whose known remote differs from the PR repo is a different repo:
            # PR still names the unit, but the cwd-relative subproject doesn't apply.
            repo_ok = not (remote and remote != pr_key)
            key, source, remote = pr_key, "pr", pr_key
            name = pr_key.rsplit("/", 1)[-1]
        else:
            repo_ok = True
        if is_main and primary_root and anchor and repo_ok and anchor != primary_root and _inside(anchor, primary_root):
            seg = anchor[len(primary_root.rstrip("/")) + 1:].split("/", 1)[0]
            if seg:
                sub = seg
        full_key = f"{key} :: {sub}" if sub else key

        primary = _unit(full_key, source, remote, repo_root, name, sub, anchor, share, branch, prs, [])
        units = [dict(primary, units=[])]
        seen = {full_key, key}
        # Additional roots touched (multi-valued), largest share first.
        merged = Counter()
        for r, n in share_by_root.items():
            merged[fold.get(r, r)] += n
        for outer, n in sorted(merged.items(), key=lambda kv: (-kv[1], kv[0])):
            k, src, rem, o, nm = _repo_identity(outer, fold, remotes)
            if k in seen:
                continue
            seen.add(k)
            units.append(_unit(k, src, rem, o, nm, None, outer, n / total if total else 0.0,
                               branch, [], []))
        primary["units"] = units
        out[aid] = primary

    # Session level: main's key first, then distinct others; drop bare repo if a subproject of it is listed.
    ordered = [a["agent_id"] for a in agents]
    if main_id in out:
        ordered.remove(main_id)
        ordered.insert(0, main_id)
    counts = Counter(out[a]["key"] for a in ordered)
    first_keys = []
    for a in ordered:
        for u in out[a]["units"]:
            if u["key"] not in first_keys:
                first_keys.append(u["key"])
    prim = out[main_id]["key"] if main_id in out else (counts.most_common(1)[0][0] if counts else None)
    if prim is None:
        sc = _ctx(session, "cwd")
        prim = (posixpath.basename(sc.rstrip("/")) if sc else "") or "unresolved"
        first_keys = [prim]
    if prim in first_keys:
        first_keys.remove(prim)
    keys = [prim] + first_keys
    keys = [k for k in keys if not any(o.startswith(k + " :: ") for o in keys)]
    by_key = {}
    for a in ordered:
        for u in out[a]["units"]:
            by_key.setdefault(u["key"], u)
    out["__session__"] = {"key": prim, "keys": keys,
                          "units": [by_key[k] for k in keys if k in by_key]}
    return out
