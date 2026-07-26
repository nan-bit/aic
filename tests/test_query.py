"""Tests for the pure query layer.

These exist because cli.py used to compute and print in the same breath, which
made the interesting parts only observable through captured stdout. Everything
here returns data.
"""

import textwrap

import pytest

from aic import query
from aic.store import Store


def write(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    write(root, "__init__.py", "")
    write(root, "db.py", """
        import sqlite3

        SECRET_TOKEN = "not-a-placeholder-value"

        def query(uid):
            cur = sqlite3.connect(":memory:").cursor()
            cur.execute("SELECT * FROM u WHERE id=" + uid)

        def safe():
            pass
    """)
    write(root, "svc.py", """
        from proj.db import query

        def handle(uid):
            return query(uid)
    """)
    write(root, "lonely.py", """
        def unrelated(a: int) -> int:
            return a + 1
    """)
    return root


@pytest.fixture
def st(repo):
    with Store(query.db_for(repo)) as store:
        query.refresh(store, repo)
        yield store


# --- refresh -----------------------------------------------------------

def test_refresh_cold_then_incremental(repo):
    with Store(query.db_for(repo)) as store:
        cold = query.refresh(store, repo)
        assert cold["mode"] == "cold"
        assert len(cold["reparsed"]) == cold["files_on_disk"] == 4

        warm = query.refresh(store, repo)
        assert warm["mode"] == "incremental"
        assert warm["reparsed"] == []
        assert warm["skipped"] == 4


def test_refresh_reparses_only_what_changed_and_dirties_dependents(repo, st):
    (repo / "db.py").write_text("def query(uid):\n    pass\n", encoding="utf-8")
    r = query.refresh(st, repo)
    assert r["reparsed"] == ["db.py"]
    # svc.py imports db.py, so it is invalidated; lonely.py is untouched.
    assert r["dirtied"] == ["svc.py"]


def test_refresh_evicts_deleted_files(repo, st):
    (repo / "lonely.py").unlink()
    r = query.refresh(st, repo)
    assert r["evicted"] == ["lonely.py"]
    assert "lonely.py" not in st.all_paths()


def test_refresh_hash_beats_a_lying_mtime(repo, st):
    """Touching a file must not cost a reparse -- mtime is a hint, not truth."""
    (repo / "db.py").touch()
    r = query.refresh(st, repo)
    assert r["stat_changed"] == 1
    assert r["reparsed"] == []


# --- touch -------------------------------------------------------------

def test_touch_reparses_named_file_without_walking(repo, st):
    r = query.touch(st, repo, ["db.py"])
    assert r["reparsed"] == ["db.py"]
    assert r["dirtied"] == ["svc.py"]


def test_touch_rejects_unknown_path(repo, st):
    with pytest.raises(query.UnknownFile):
        query.touch(st, repo, ["ghost.py"])


# --- impact / review ---------------------------------------------------

def test_impact_counts_dependents_and_narrows_to_probe(st):
    r = query.impact(st, "db.py", "security")
    assert set(r["impacted"]) == {"db.py", "svc.py"}
    # Every function needing recheck lives in an impacted file.
    assert all(p in r["impacted"] for p, _ in r["recheck_fns"])
    assert r["recheck_fns"], "the sql sink in db.py should be reachable"


def test_impact_on_leaf_file_implicates_only_itself(st):
    r = query.impact(st, "lonely.py", "security")
    assert r["impacted"] == ["lonely.py"]


def test_impact_rejects_unknown_file(st):
    with pytest.raises(query.UnknownFile):
        query.impact(st, "ghost.py", "security")


def test_review_scope_covers_edits_and_their_dependents(repo, st):
    (repo / "db.py").write_text(
        'import sqlite3\n\ndef query(uid):\n'
        '    sqlite3.connect(":memory:").cursor().execute("SELECT " + uid)\n',
        encoding="utf-8",
    )
    r = query.refresh(st, repo)
    rev = query.review(st, "security", seeds=r["reparsed"])
    # The edited file is CLEAN (just reparsed); its dependent is DIRTY. Both belong.
    assert set(rev["scope"]) == {"db.py", "svc.py"}


def test_review_is_empty_when_nothing_moved(st):
    assert query.review(st, "security")["scope"] == []


def test_review_survives_a_no_op_refresh(repo, st):
    """Regression: a resident server refreshes on every call.

    `refresh` clears DIRTY each time, so a review that read the stored flag saw
    the second, no-op refresh erase the dependents of the first edit and
    reported that the change reached nothing. Found by an agent calling
    aic_review three times in a row with different probes.
    """
    (repo / "db.py").write_text("def query(uid):\n    pass\n", encoding="utf-8")
    seeds = query.refresh(st, repo)["reparsed"]
    assert set(query.review(st, "security", seeds)["scope"]) == {"db.py", "svc.py"}

    query.refresh(st, repo)          # second tool call, nothing changed on disk
    assert set(query.review(st, "security", seeds)["scope"]) == {"db.py", "svc.py"}


def test_review_accumulates_across_separate_edits(repo, st):
    """Two edits in one session must both stay in scope."""
    seeds = set()
    (repo / "db.py").write_text("def query(uid):\n    pass\n", encoding="utf-8")
    seeds.update(query.refresh(st, repo)["reparsed"])
    (repo / "lonely.py").write_text("def unrelated(a):\n    return a\n", encoding="utf-8")
    seeds.update(query.refresh(st, repo)["reparsed"])

    scope = set(query.review(st, "security", seeds)["scope"])
    assert scope == {"db.py", "svc.py", "lonely.py"}, \
        "the first edit's dependents must survive the second edit"


# --- markers -----------------------------------------------------------

def test_markers_for_filters_by_function_not_by_file(st):
    """A file needing recheck also holds markers the change never reached."""
    fns = [("db.py", "query")]
    rows = query.markers_for(st, "api", fns)
    assert rows, "expected the api probe to mark db.py:query"
    assert {q for _, q, _, _, _ in rows} == {"query"}, \
        "db.py also defines safe(); filtering by file would have included it"


def test_markers_for_includes_module_level_only_for_named_paths(st):
    assert not query.markers_for(st, "security", fns=[], module_paths=[])
    rows = query.markers_for(st, "security", fns=[], module_paths=["db.py"])
    assert [r[2] for r in rows] == ["hardcoded-secret"]


def test_rank_puts_dataflow_confirmed_first_then_blast_radius():
    rows = [
        ("leaf.py", "f", "public-api", "", 1),
        ("leaf.py", "g", "sql", "", 2),
        ("hub.py", "h", "sql", "", 3),
        ("leaf.py", "i", "tainted-sql", "", 4),
    ]
    fan = {"hub.py": 500, "leaf.py": 1}
    kinds = [r[2] for r in query.rank_markers(rows, fan)]
    assert kinds == ["tainted-sql", "sql", "sql", "public-api"]
    # Within the sink tier, the high-fanout file wins.
    ranked = query.rank_markers(rows, fan)
    assert ranked[1][0] == "hub.py"


# --- store bootstrap ---------------------------------------------------

def test_open_store_raises_before_any_index(tmp_path):
    with pytest.raises(query.GraphMissing):
        query.open_store(tmp_path)


# --- where the graph lives ---------------------------------------------

def test_graph_defaults_to_beside_the_repo(tmp_path):
    assert query.db_for(tmp_path) == tmp_path / ".aic" / "graph.db"


def test_explicit_db_path_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("AIC_DB_DIR", str(tmp_path / "shared"))
    assert query.db_for(tmp_path, tmp_path / "explicit.db") == tmp_path / "explicit.db"


def test_db_dir_keeps_graphs_outside_the_tree(tmp_path, monkeypatch):
    shared = tmp_path / "graphs"
    monkeypatch.setenv("AIC_DB_DIR", str(shared))
    one, two = tmp_path / "proj-a", tmp_path / "proj-b"
    one.mkdir(), two.mkdir()
    a, b = query.db_for(one), query.db_for(two)
    assert a.parent == b.parent == shared
    assert a != b, "two repos must not share one graph"
    assert query.db_for(one) == a, "the path must be stable across calls"


def test_unwritable_location_is_an_actionable_error(tmp_path):
    """A read-only checkout is a normal thing to be handed, not a crash."""
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        with pytest.raises(query.GraphUnwritable) as exc:
            query.create_store(ro)
        assert "--db" in str(exc.value) and "AIC_DB_DIR" in str(exc.value)
    finally:
        ro.chmod(0o700)


def test_read_only_repo_indexes_when_the_graph_lives_elsewhere(tmp_path):
    src = tmp_path / "src"
    write(src, "m.py", "import os\n\ndef f(p):\n    os.system('ls ' + p)\n")
    src.chmod(0o500)
    try:
        with query.create_store(src, tmp_path / "elsewhere.db") as store:
            r = query.refresh(store, src)
        assert r["reparsed"] == ["m.py"]
        assert not (src / ".aic").exists(), "must not write into the analysed tree"
    finally:
        src.chmod(0o700)
