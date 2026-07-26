"""Tests for the MCP surface.

Tools are called in-process rather than over a transport: the protocol is the
SDK's problem, the response shape is ours. What is worth guarding here is
everything that keeps a response usable inside a context window -- ranking,
truncation, saying what was elided, and never emitting a raw dependent-file
list.
"""

import json
import textwrap

import pytest

pytest.importorskip("mcp", reason="MCP SDK is an optional extra (aic[mcp])")

from aic import mcp as M  # noqa: E402


def write(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A repo with the module-level server state pointed at it."""
    root = tmp_path / "proj"
    write(root, "__init__.py", "")
    write(root, "db.py", """
        import sqlite3

        def query(uid):
            sqlite3.connect(":memory:").cursor().execute("SELECT " + uid)
    """)
    write(root, "svc.py", """
        from proj.db import query

        def handle(uid):
            return query(uid)
    """)
    monkeypatch.setattr(M, "_REPO", root)
    monkeypatch.setattr(M, "_TOUCHED", set())
    return root


# --- tool registration -------------------------------------------------

def test_tools_are_registered_read_only(served):
    import asyncio
    tools = asyncio.run(M.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"aic_review", "aic_impact", "aic_overview"}
    for tool in tools:
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.openWorldHint is False
        assert tool.description, f"{tool.name} needs a description; agents select on it"
        assert tool.outputSchema, f"{tool.name} should declare structured output"


def test_tool_surface_stays_small(served):
    """Every tool definition costs context whether or not it is called."""
    import asyncio
    tools = asyncio.run(M.mcp.list_tools())
    assert len(tools) <= 3


# --- review ------------------------------------------------------------

def test_review_is_quiet_when_nothing_changed(served):
    r = M.aic_review()
    assert r["changed_files"] == 0
    assert r["findings"] == []
    assert "Nothing changed" in r["summary"]


def test_review_reports_edits_and_dependents(served):
    M.aic_review()  # establishes the baseline graph
    (served / "db.py").write_text(
        'import sqlite3\n\ndef query(uid):\n'
        '    sqlite3.connect(":memory:").cursor().execute("SELECT " + uid)\n'
        '    import os\n    os.system("ls " + uid)\n',
        encoding="utf-8",
    )
    r = M.aic_review()
    assert r["changed_files"] == 1
    assert r["dependent_files"] >= 1
    kinds = {f["kind"] for f in r["findings"]}
    assert "tainted-command-exec" in kinds


def test_review_findings_are_ranked_dataflow_first(served):
    r = M.aic_review()
    (served / "db.py").write_text(
        'import sqlite3\n\ndef query(uid):\n'
        '    sqlite3.connect(":memory:").cursor().execute("SELECT " + uid)\n',
        encoding="utf-8",
    )
    r = M.aic_review()
    tainted = [i for i, f in enumerate(r["findings"]) if f["kind"].startswith("tainted-")]
    others = [i for i, f in enumerate(r["findings"]) if not f["kind"].startswith("tainted-")]
    if tainted and others:
        assert max(tainted) < min(others), "dataflow-confirmed findings must sort first"


def test_unknown_probe_is_an_actionable_error(served):
    with pytest.raises(ValueError, match="unknown probe"):
        M.aic_review(probe="nonsense")


# --- impact ------------------------------------------------------------

def test_impact_reports_dependents_as_a_count_not_a_list(served):
    r = M.aic_impact("db.py")
    assert isinstance(r["dependent_files"], int)
    blob = json.dumps(r)
    assert "svc.py" not in blob or "findings" in blob
    # The dependent-file list itself must never be in the payload.
    assert "impacted" not in r


def test_impact_truncates_and_says_so(served):
    body = "import sqlite3\n\n" + "".join(
        f'def q{i}(uid):\n'
        f'    sqlite3.connect(":memory:").cursor().execute("SELECT " + uid)\n\n'
        for i in range(40)
    )
    (served / "db.py").write_text(body, encoding="utf-8")
    r = M.aic_impact("db.py", limit=5)
    assert r["showing"] == 5
    assert r["total_findings"] > 5
    assert "Showing 5 of" in r["summary"]
    assert len(r["findings"]) == 5


def test_impact_response_stays_small(served):
    body = "import sqlite3\n\n" + "".join(
        f'def q{i}(uid):\n'
        f'    sqlite3.connect(":memory:").cursor().execute("SELECT " + uid)\n\n'
        for i in range(200)
    )
    (served / "db.py").write_text(body, encoding="utf-8")
    r = M.aic_impact("db.py")
    # Claude Code caps tool responses at 25k tokens; ~4 bytes/token gives 100kB.
    # A default-limit response should not come close.
    assert len(json.dumps(r)) < 20_000


def test_impact_rejects_unknown_file_with_guidance(served):
    with pytest.raises(ValueError) as exc:
        M.aic_impact("ghost.py")
    assert "repo-relative" in str(exc.value)


# --- overview ----------------------------------------------------------

def test_overview_describes_the_repo_shape(served):
    r = M.aic_overview()
    assert r["files"] >= 3
    assert r["functions"] >= 2
    assert set(r["markers_by_probe"]) <= {"security", "api", "tests"}
    assert r["blast_radius_median"] >= 1
    assert r["blast_radius_max"] >= r["blast_radius_median"]


def test_overview_bootstraps_a_missing_graph(served):
    """First call on an unindexed repo must build the graph, not fail."""
    assert not (served / ".aic" / "graph.db").exists()
    r = M.aic_overview()
    assert r["files"] >= 3
    assert (served / ".aic" / "graph.db").exists()


# --- instrumentation ---------------------------------------------------

def test_calls_are_logged_for_later_analysis(served):
    M.aic_overview()
    M.aic_impact("db.py")
    log = served / ".aic" / "mcp-calls.jsonl"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["tool"] for e in entries] == ["aic_overview", "aic_impact"]
    assert all("elapsed_ms" in e and "bytes" in e for e in entries)
