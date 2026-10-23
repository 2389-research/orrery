# ABOUTME: Unit tests for the pure meridian work-unit resolver (no FS, no git, no network).
# ABOUTME: Plain-dict fixtures mirror the live meridian record/agent/session shapes.
from src.jobs.work_unit import normalize_remote, resolve_work_units

GH = "/Users/x/Documents/GitHub"


def rec(agent, cwd, root=None, remote=None, branch=None):
    raw = {"cwd": cwd}
    if branch:
        raw["gitBranch"] = branch
    if root:
        gs = {"root": root, "branch": branch or "main"}
        if remote:
            host, _, rest = remote.partition("/")
            gs["visibility"] = {"origin": {"host": host, "remote": rest}}
        raw["serverClassifierContext"] = {"context": {"git_state": gs}}
    return {"agent_id": agent, "uuid": f"u{id(raw)}", "seq": 0, "raw": raw}


def agent(aid, parent=None, prs=None, cwd=None):
    return {"agent_id": aid, "agent_type": "x", "parent_agent_id": parent,
            "parent_tool_use_id": None,
            "rollup": {"context": {"cwd": cwd}, "outputs": {"pr_links": prs or []}}}


def session(cwd=GH, prs=None, branch="whatever"):
    return {"session_key": "p/s", "cwd": cwd, "git_branch": branch, "started_at": 1,
            "lifecycle": "ended",
            "rollup": {"context": {"cwd": cwd, "git_branch": branch},
                       "outputs": {"pr_links": prs or []}}}


def test_normalize_remote_variants():
    n = "github.com/2389-research/meadow"
    assert normalize_remote("git@github.com:2389-research/meadow.git") == n
    assert normalize_remote("https://github.com/2389-research/meadow") == n
    assert normalize_remote("https://user:tok@GitHub.com/2389-research/meadow.git") == n
    assert normalize_remote("ssh://git@github.com/2389-research/meadow.git") == n
    assert normalize_remote("http://github.com/2389-research/meadow") == n
    assert normalize_remote(None) is None
    assert normalize_remote("") is None


def test_pr_ground_truth_with_cwd():
    recs = [rec("main", f"{GH}/meadow") for _ in range(3)]
    out = resolve_work_units([agent("main", prs=["https://github.com/2389-research/meadow/pull/2"])],
                             session(f"{GH}/meadow"), recs)
    u = out["main"]
    assert u["key"] == "github.com/2389-research/meadow"
    assert u["source"] == "pr"
    assert u["pr_links"] == ["github.com/2389-research/meadow/pull/2"]
    assert out["__session__"]["key"] == u["key"]


def test_pr_beats_cwd():
    recs = [rec("main", f"{GH}/other-folder", root=f"{GH}/other-folder",
                remote="github.com/someone/other") for _ in range(3)]
    out = resolve_work_units([agent("main", prs=["https://github.com/2389-research/meadow/pull/2"])],
                             session(), recs)
    assert out["main"]["key"] == "github.com/2389-research/meadow"
    assert out["main"]["source"] == "pr"


def test_git_remote_and_misleading_branch_ignored():
    recs = [rec("main", f"{GH}/meadow", root=f"{GH}/meadow", remote="github.com/2389-research/meadow",
                branch="totally-other-repo") for _ in range(4)]
    out = resolve_work_units([agent("main")], session(f"{GH}/meadow", branch="totally-other-repo"), recs)
    u = out["main"]
    assert u["key"] == "github.com/2389-research/meadow"
    assert u["source"] == "git_remote"
    assert u["repo_name"] == "meadow"
    assert u["repo_root"] == f"{GH}/meadow"
    assert u["subproject"] is None
    assert "totally" not in u["key"]


def test_monorepo_subproject():
    recs = [rec("main", f"{GH}/DS-scratch/jev-plays-40k", root=f"{GH}/DS-scratch",
                remote="github.com/2389-research/ds-scratch") for _ in range(5)]
    out = resolve_work_units([agent("main")], session(f"{GH}/DS-scratch/jev-plays-40k"), recs)
    u = out["main"]
    assert u["key"] == "github.com/2389-research/ds-scratch :: jev-plays-40k"
    assert u["subproject"] == "jev-plays-40k"
    assert u["anchor_dir"] == f"{GH}/DS-scratch/jev-plays-40k"
    assert u["anchor_share"] == 1.0


def test_nested_repo_with_own_remote_is_not_subproject():
    recs = [rec("main", f"{GH}/DS-scratch", root=f"{GH}/DS-scratch",
                remote="github.com/2389-research/ds-scratch")]
    recs += [rec("main", f"{GH}/DS-scratch/watcher", root=f"{GH}/DS-scratch/watcher",
                 remote="github.com/2389-research/watcher") for _ in range(9)]
    out = resolve_work_units([agent("main")], session(f"{GH}/DS-scratch/watcher"), recs)
    u = out["main"]
    assert u["key"] == "github.com/2389-research/watcher"
    assert u["subproject"] is None


def test_worktree_folds_to_main_root():
    main = f"{GH}/meadow"
    wt = f"{main}/.worktrees/feat"
    recs = [rec("main", main, root=main, remote="github.com/2389-research/meadow")]
    recs += [rec("main", wt, root=wt) for _ in range(9)]  # worktree: unknown remote
    out = resolve_work_units([agent("main")], session(wt), recs)
    u = out["main"]
    assert u["key"] == "github.com/2389-research/meadow"
    assert u["repo_root"] == main
    assert u["subproject"] is None  # relative to the worktree root, not '.worktrees'
    assert len(u["units"]) == 1


def test_stub_agent_no_cwd_gets_folder_key():
    out = resolve_work_units([agent("main"), agent("sub1", parent="main")], session(GH), [])
    for aid in ("main", "sub1"):
        assert out[aid]["key"]
        assert out[aid]["source"] == "folder"
    assert out["main"]["key"] == "GitHub"
    assert out["__session__"]["key"]


def test_nothing_at_all_is_unresolved_not_null():
    s = session(cwd=None)
    s["rollup"]["context"]["cwd"] = None
    out = resolve_work_units([agent("main")], s, [])
    assert out["main"]["key"] == "unresolved"


def test_folder_when_no_git_root():
    recs = [rec("main", GH) for _ in range(3)]
    out = resolve_work_units([agent("main")], session(GH), recs)
    assert out["main"]["key"] == "GitHub"
    assert out["main"]["source"] == "folder"


def test_git_name_when_root_has_no_remote():
    recs = [rec("main", f"{GH}/localrepo", root=f"{GH}/localrepo") for _ in range(3)]
    out = resolve_work_units([agent("main")], session(f"{GH}/localrepo"), recs)
    assert out["main"]["key"] == "localrepo"
    assert out["main"]["source"] == "git_name"


def test_subagent_in_session_repo_inherits_no_subproject():
    root = f"{GH}/DS-scratch"
    remote = "github.com/2389-research/ds-scratch"
    recs = [rec("main", f"{root}/jev", root=root, remote=remote) for _ in range(4)]
    recs += [rec("sub1", f"{root}/jev/deeper", root=root, remote=remote) for _ in range(3)]
    out = resolve_work_units([agent("main"), agent("sub1", parent="main")], session(f"{root}/jev"), recs)
    assert out["main"]["key"] == f"{remote} :: jev"
    assert out["sub1"]["key"] == remote
    assert out["sub1"]["subproject"] is None
    s = out["__session__"]
    assert s["key"] == f"{remote} :: jev"
    assert s["keys"] == [f"{remote} :: jev"]  # bare repo key not double-listed


def test_multi_valued_agent_carries_both_roots():
    a, b = f"{GH}/alpha", f"{GH}/beta"
    recs = [rec("main", a, root=a, remote="github.com/o/alpha") for _ in range(7)]
    recs += [rec("main", b, root=b, remote="github.com/o/beta") for _ in range(3)]
    out = resolve_work_units([agent("main")], session(a), recs)
    u = out["main"]
    assert u["key"] == "github.com/o/alpha"
    assert [x["key"] for x in u["units"]] == ["github.com/o/alpha", "github.com/o/beta"]
    assert set(out["__session__"]["keys"]) == {"github.com/o/alpha", "github.com/o/beta"}


def test_anchor_requires_80_percent():
    recs = [rec("main", f"{GH}/meadow/a") for _ in range(5)]
    recs += [rec("main", f"{GH}/meadow/b") for _ in range(5)]
    out = resolve_work_units([agent("main")], session(), recs)
    assert out["main"]["anchor_dir"] == f"{GH}/meadow"
    assert out["main"]["anchor_share"] == 1.0
