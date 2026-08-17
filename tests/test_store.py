"""Tests for the store's two non-obvious guarantees.

Both are invisible in normal use and expensive when wrong: the baseline
outliving a schema rebuild, and two writers not colliding. Neither is
observable from the query layer, so they are pinned here.

No MCP import, deliberately -- these must run without the optional extra, or
the concurrency guarantee would only be checked on machines that happened to
have the SDK installed.
"""

import sqlite3
import threading

import pytest

from aic import analyze, query, store
from aic.store import Store


def write(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    write(root, "__init__.py", "")
    write(root, "db.py", "import sqlite3\n\ndef query(uid):\n    pass\n")
    write(root, "svc.py", "from proj.db import query\n\ndef handle(u):\n    return query(u)\n")
    return root


# --- baseline across a schema rebuild ----------------------------------

def test_a_schema_bump_rebuilds_the_graph_and_keeps_the_baseline(repo, monkeypatch):
    """The graph is derived and cheap to rebuild. The baseline is neither."""
    db = query.db_for(repo)
    with Store(db) as st:
        query.refresh(st, repo)
        before = st.baseline_info()
    assert before is not None

    monkeypatch.setattr(store, "SCHEMA_VERSION", store.SCHEMA_VERSION + 1)
    with Store(db) as st:
        assert st.counts()["files"] == 0, "the graph should have been dropped"
        assert st.baseline_info() is None, (
            "a baseline written under the old schema must read as absent, not "
            "as an answer -- a wrong baseline answers confidently"
        )
        # The rows are still there; it is the provenance that disqualifies them.
        assert st.conn.execute("SELECT count(*) FROM baseline").fetchone()[0] > 0

    # And re-recording under the new version brings it back into use, rather
    # than the data having been thrown away and needing a re-index to recover.
    with Store(db) as st:
        query.refresh(st, repo)
        assert st.baseline_info() is not None


def test_a_matching_schema_leaves_everything_alone(repo):
    db = query.db_for(repo)
    with Store(db) as st:
        query.refresh(st, repo)
        first = st.baseline_info()
        files = st.counts()["files"]
    with Store(db) as st:
        assert st.baseline_info() == first
        assert st.counts()["files"] == files


# --- concurrent writers ------------------------------------------------

def test_two_threads_refreshing_do_not_collide(repo):
    """SDK v2 runs sync tool handlers on worker threads, so this is now reachable.

    Every tool call refreshes, and refresh writes. Without a busy timeout the
    second writer gets SQLITE_BUSY immediately rather than waiting.
    """
    db = query.db_for(repo)
    with Store(db) as st:
        query.refresh(st, repo)

    errors = []

    def worker():
        try:
            for _ in range(6):
                with Store(db) as st:
                    query.refresh(st, repo)
        except sqlite3.OperationalError as exc:  # pragma: no cover -- the bug
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent refresh raised {errors[0]}"


def test_busy_timeout_is_actually_set(tmp_path):
    """Pinning the pragma, because the ordering in __init__ is what makes it work.

    It has to precede journal_mode and the schema script: both are writes, so a
    timeout set after them would not cover the statements most likely to
    contend with a concurrent indexer.
    """
    with Store(tmp_path / "g.db") as st:
        got = st.conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert got == store.BUSY_TIMEOUT_MS


def test_baseline_round_trips(tmp_path):
    with Store(tmp_path / "g.db") as st:
        assert st.baseline_info() is None
        assert st.baseline() == {}
        st.set_baseline({"a.py": "h1", "b.py": "h2"}, "2026-07-29T00:00:00+00:00")
        st.commit()
        assert st.baseline() == {"a.py": "h1", "b.py": "h2"}
        assert st.baseline_info() == ("2026-07-29T00:00:00+00:00", 2)

        st.set_baseline({"a.py": "h3"}, "2026-07-30T00:00:00+00:00")
        st.commit()
        assert st.baseline() == {"a.py": "h3"}, "a reset replaces, never merges"


def test_analyze_is_untouched_by_any_of_this():
    """The engine has no idea baselines exist; keep it that way."""
    assert not any("baseline" in n for n in dir(analyze))
