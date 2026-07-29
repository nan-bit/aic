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

from aic.surfaces import mcp as M  # noqa: E402


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
    # No session state left to reset: the baseline lives in the graph, and
    # tmp_path gives each test its own. Isolation used to need arranging; it is
    # now a property of where the state is kept.
    monkeypatch.setattr(M, "_REPO", root)
    return root


# --- tool registration -------------------------------------------------

def test_tools_are_registered_read_only(served):
    import asyncio
    tools = asyncio.run(M.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"aic_review", "aic_impact", "aic_overview"}
    for tool in tools:
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is False
        assert tool.description, f"{tool.name} needs a description; agents select on it"
        assert tool.output_schema, f"{tool.name} should declare structured output"


def test_tool_schemas_serialize_camel_case_on_the_wire(served):
    """SDK v2 renamed the fields in Python, not in the protocol.

    Worth pinning: an agent that stops seeing outputSchema stops getting
    structured results and says nothing about it.
    """
    import asyncio
    tools = asyncio.run(M.mcp.list_tools())
    wire = tools[0].model_dump(by_alias=True, exclude_none=True)
    assert "outputSchema" in wire and "inputSchema" in wire
    assert wire["annotations"]["readOnlyHint"] is True


def test_tool_surface_stays_small(served):
    """Every tool definition costs context whether or not it is called."""
    import asyncio
    tools = asyncio.run(M.mcp.list_tools())
    assert len(tools) <= 3


# --- review ------------------------------------------------------------

def test_first_review_says_it_has_no_baseline_rather_than_all_clear(served):
    """The two must not read alike.

    "Nothing changed" on a first call was reassurance about a question that had
    not been asked yet. An agent acting on it skips a check it never ran.
    """
    r = M.aic_review()
    assert r["changed_files"] == 0
    assert r["findings"] == []
    assert "No baseline" in r["summary"]
    assert "aic_impact" in r["summary"], "an agent stuck here needs a way forward"
    assert "Nothing has changed" not in r["summary"]


def test_review_is_quiet_when_nothing_changed(served):
    M.aic_review()  # establishes the baseline
    r = M.aic_review()
    assert r["changed_files"] == 0
    assert r["findings"] == []
    assert "Nothing has changed since the baseline" in r["summary"]
    assert r["baseline"] != "none"


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


def test_repeated_reviews_agree_with_each_other(served):
    """Regression: consecutive reviews must not contradict one another.

    A live agent called aic_review three times with different probes; the first
    reported findings and the rest reported none, because each call's refresh
    cleared the DIRTY flag the previous one had relied on.
    """
    M.aic_review()  # baseline
    (served / "db.py").write_text(
        'import sqlite3\n\ndef query(uid):\n'
        '    sqlite3.connect(":memory:").cursor().execute("SELECT " + uid)\n',
        encoding="utf-8",
    )
    first = M.aic_review(probe="api")
    second = M.aic_review(probe="api")
    third = M.aic_review(probe="security")

    assert first["dependent_files"] == second["dependent_files"]
    assert first["findings"] == second["findings"]
    assert third["changed_files"] == first["changed_files"] == 1
    assert third["findings"], "the sql sink is downstream of the edit and must appear"


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


def test_review_response_stays_small(served):
    """review now carries a baseline string too; the budget is unchanged."""
    M.aic_review()  # baseline
    body = "import sqlite3\n\n" + "".join(
        f'def q{i}(uid):\n'
        f'    sqlite3.connect(":memory:").cursor().execute("SELECT " + uid)\n\n'
        for i in range(200)
    )
    (served / "db.py").write_text(body, encoding="utf-8")
    r = M.aic_review()
    assert r["changed_files"] == 1
    assert len(json.dumps(r)) < 20_000


def test_no_back_channel_deprecation_warnings(served):
    """Roots, sampling and logging are deprecated at 2026-07-28 and raise on
    servers with no back-channel. aic uses none of them -- pin that rather than
    assume it, since the failure would be a client-side error we never see.
    """
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        M.aic_review()
        M.aic_impact("db.py")
        M.aic_overview()
    names = [type(w.message).__name__ for w in caught]
    assert not [n for n in names if "Deprecation" in n], names


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
