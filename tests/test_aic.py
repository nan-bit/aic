import textwrap

import pytest

from aic import analyze, probes
from aic.cli import cmd_index, cmd_touch, db_for
from aic.store import Store


class Args:
    def __init__(self, repo, **kw):
        self.repo = str(repo)
        self.probe = kw.get("probe", probes.DEFAULT)
        self.top = kw.get("top", 0)
        self.file = kw.get("file")
        self.files = kw.get("files", [])
        self.rehash = kw.get("rehash", False)


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

        def query(uid):
            cur = sqlite3.connect(":memory:").cursor()
            cur.execute("SELECT * FROM u WHERE id=" + uid)

        def _internal():
            pass
    """)
    write(root, "svc.py", """
        from proj.db import query

        def handle(uid):
            return query(uid)
    """)
    write(root, "unrelated.py", """
        def add(a: int, b: int = 2) -> int:
            return a + b
    """)
    write(root, "tests/test_svc.py", """
        from proj.svc import handle

        def test_handle():
            assert handle("1") is None
    """)
    return root


def index(root):
    cmd_index(Args(root))
    return Store(db_for(root))


# --- extraction --------------------------------------------------------

def test_decorators_and_annotations_survive():
    """v1's skeletonizer dropped both; a security probe is useless without them."""
    _, facts = analyze.extract("m.py", textwrap.dedent("""
        @requires_admin
        def drop(user_id: int, *, force: bool = False, **kw) -> dict:
            pass
    """))
    fn = facts.functions[0]
    assert fn.decorators == ["requires_admin"]
    assert "user_id: int" in fn.args
    assert "*" not in fn.args[0]
    assert "**kw" in fn.args
    assert fn.returns == "dict"


def test_module_level_assignments_captured():
    _, facts = analyze.extract("m.py", 'API_KEY = "sk-not-a-real-key-1234"\n')
    assert ("API_KEY", 1, "sk-not-a-real-key-1234") in facts.assignments


def test_relative_import_module_is_not_stringified_none():
    """v1 emitted the literal text `from None import db`."""
    _, facts = analyze.extract("m.py", "from . import db\n")
    mod, level, names = facts.imports[0]
    assert mod == "" and level == 1 and names == ["db"]


# --- probes ------------------------------------------------------------

def test_probes_disagree(repo):
    """The engine is probe-agnostic only if probes actually select differently."""
    with index(repo) as st:
        counts = {p: len(st.marked_functions(p)) for p in ("security", "api", "tests")}
    assert counts["security"] >= 1
    assert counts["api"] > counts["security"]
    assert counts["tests"] >= 1
    assert counts["api"] != counts["tests"]


def test_security_probe_flags_sql_sink(repo):
    with index(repo) as st:
        kinds = {k for _, _, k, _, _ in st.sample_markers("security", 50)}
    assert "sql" in kinds


def test_security_probe_ignores_placeholder_secrets():
    probe = probes.get("security")
    _, facts = analyze.extract("m.py", 'PASSWORD = "changeme"\nTOKEN = ""\n')
    assert list(probe.inspect("m.py", None, facts)) == []


def test_api_probe_skips_private(repo):
    with index(repo) as st:
        names = {q for _, q in st.marked_functions("api")}
    assert "query" in names
    assert "_internal" not in names


# --- incrementality ----------------------------------------------------

def test_second_index_reparses_nothing(repo, capsys):
    cmd_index(Args(repo))
    capsys.readouterr()
    cmd_index(Args(repo))
    out = capsys.readouterr().out
    assert "reparsed                0" in out


def test_edit_reparses_only_that_file(repo, capsys):
    cmd_index(Args(repo))
    (repo / "db.py").write_text(
        (repo / "db.py").read_text(encoding="utf-8") + "\n# touched\n", encoding="utf-8"
    )
    capsys.readouterr()
    cmd_index(Args(repo))
    out = capsys.readouterr().out
    assert "reparsed                1" in out


def test_dependents_are_marked_dirty(repo):
    cmd_index(Args(repo))
    (repo / "db.py").write_text(
        (repo / "db.py").read_text(encoding="utf-8") + "\n# touched\n", encoding="utf-8"
    )
    cmd_index(Args(repo))
    with Store(db_for(repo)) as st:
        dirty = st.dirty()
    assert "svc.py" in dirty          # imports db
    assert "db.py" not in dirty       # it was reparsed, not merely invalidated
    assert "unrelated.py" not in dirty


def test_touch_reparses_without_walking(repo, capsys):
    cmd_index(Args(repo))
    (repo / "db.py").write_text(
        (repo / "db.py").read_text(encoding="utf-8") + "\n# touched\n", encoding="utf-8"
    )
    capsys.readouterr()
    cmd_touch(Args(repo, files=["db.py"]))
    out = capsys.readouterr().out
    assert "reparsed                  1" in out
    with Store(db_for(repo)) as st:
        assert "svc.py" in st.dirty()


def test_touch_accepts_absolute_paths(repo):
    cmd_index(Args(repo))
    cmd_touch(Args(repo, files=[str(repo / "db.py")]))
    with Store(db_for(repo)) as st:
        assert "db.py" in st.all_paths()


def test_touch_evicts_a_deleted_file(repo):
    cmd_index(Args(repo))
    (repo / "unrelated.py").unlink()
    cmd_touch(Args(repo, files=["unrelated.py"]))
    with Store(db_for(repo)) as st:
        assert "unrelated.py" not in st.all_paths()


def test_unchanged_content_with_moved_mtime_is_not_reparsed(repo, capsys):
    """Touching a file's timestamp without changing bytes must not invalidate
    dependents -- mtime is a hint, the hash is the truth."""
    import os
    cmd_index(Args(repo))
    p = repo / "db.py"
    st_before = p.stat()
    os.utime(p, ns=(st_before.st_mtime_ns + 10**9, st_before.st_mtime_ns + 10**9))
    capsys.readouterr()
    cmd_index(Args(repo))
    out = capsys.readouterr().out
    assert "stat-changed            1" in out
    assert "reparsed                0" in out
    with Store(db_for(repo)) as st:
        assert st.dirty() == set()


def test_rehash_ignores_mtime(repo, capsys):
    cmd_index(Args(repo))
    capsys.readouterr()
    cmd_index(Args(repo, rehash=True))
    out = capsys.readouterr().out
    assert f"stat-changed            {len(list(repo.rglob('*.py')))}" in out
    assert "reparsed                0" in out


def test_schema_version_bump_rebuilds(repo):
    cmd_index(Args(repo))
    with Store(db_for(repo)) as st:
        st.conn.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
        st.commit()
    with Store(db_for(repo)) as st:      # reopening must wipe, not crash
        assert st.all_paths() == []


def test_deleted_files_are_evicted(repo):
    cmd_index(Args(repo))
    (repo / "unrelated.py").unlink()
    cmd_index(Args(repo))
    with Store(db_for(repo)) as st:
        assert "unrelated.py" not in st.all_paths()
        rows = st.conn.execute(
            "SELECT COUNT(*) FROM functions WHERE path='unrelated.py'"
        ).fetchone()[0]
    assert rows == 0


# --- graph -------------------------------------------------------------

def test_import_resolution_is_exact_not_suffix_matched(tmp_path):
    """`db.models` must not resolve to `contrib.gis.db.models`."""
    root = tmp_path / "proj"
    write(root, "__init__.py", "")
    write(root, "db/__init__.py", "")
    write(root, "db/models.py", "X = 1\n")
    write(root, "contrib/__init__.py", "")
    write(root, "contrib/db/__init__.py", "")
    write(root, "contrib/db/models.py", "Y = 2\n")
    write(root, "app.py", "from proj.db import models\n")

    cmd_index(Args(root))
    with Store(db_for(root)) as st:
        edges = st.import_edges()

    targets = edges.get("app.py", set())
    # Importing from a package runs its __init__, so both are genuine edges.
    assert targets == {"db/models.py", "db/__init__.py"}
    # The point of the test: no suffix match into the similarly-named tree.
    assert not any(t.startswith("contrib/") for t in targets)


def test_fanout_matches_explicit_propagation(repo):
    with index(repo) as st:
        paths, edges = st.all_paths(), st.import_edges()
    fan = analyze.fanout(paths, edges)
    rev = analyze.reverse(edges)
    for p in paths:
        assert fan[p] == len(analyze.propagate({p}, rev) & set(paths))


def test_cycle_members_share_fanout(tmp_path):
    root = tmp_path / "proj"
    write(root, "__init__.py", "")
    write(root, "a.py", "from proj import b\n")
    write(root, "b.py", "from proj import a\n")
    write(root, "leaf.py", "from proj import a\n")

    cmd_index(Args(root))
    with Store(db_for(root)) as st:
        fan = analyze.fanout(st.all_paths(), st.import_edges())
    assert fan["a.py"] == fan["b.py"]
    assert fan["leaf.py"] < fan["a.py"]
