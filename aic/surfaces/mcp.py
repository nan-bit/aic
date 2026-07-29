"""MCP server -- the agent-facing surface.

Deliberately a thin adapter: schema declarations, ranking, truncation, and
calls into aic/query.py. No analysis logic lives here. That was a hedge against
MCP's 2026-07-28 revision (stateless core, no initialize handshake) and the
Python SDK v2 rewrite that shipped with it, and it paid: the port touched
imports, two field names and a constructor argument. Nothing analytical moved.

What did have to move went *down*, not sideways. `review` needs a point to
measure changes from, and this file used to keep it in a module-level set of
everything reparsed since the process started. A stateless protocol has nowhere
to put that, which was the right pressure: the baseline was analysis state
sitting in a protocol adapter. It now lives in the graph (`query.changed_since`),
so it survives restarts, cannot drift across refreshes, and is reachable from
the CLI -- which finally has a `review` of its own.

Two properties still matter more than anything else here:

  * **Nothing may write to stdout.** Under stdio transport stdout carries
    JSON-RPC; a stray print corrupts the protocol. Diagnostics go to stderr.
  * **Responses must stay small.** A change to django/db/models/query.py dirties
    570 files. That list is useless to an agent and expensive to carry, so it is
    reported as a count and the response body is the ranked intersection with
    what the probe actually marks.
"""

import sys

_MIN_PY = (3, 10)
if sys.version_info < _MIN_PY:  # pragma: no cover -- exercised by packaging, not tests
    sys.exit(
        "aic-mcp needs Python 3.10+ (the MCP SDK's floor); "
        f"this is {sys.version_info.major}.{sys.version_info.minor}. "
        "The `aic` CLI itself still runs on 3.9."
    )

import argparse  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from mcp.server.caching import CacheHint  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp_types import ToolAnnotations  # noqa: E402
from typing_extensions import TypedDict  # noqa: E402  -- pydantic cannot resolve
                                         # typing.TypedDict below 3.12

from .. import probes, query  # noqa: E402

DEFAULT_LIMIT = 20
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# The tool list is three tools declared at import time: no auth, no per-caller
# filtering, nothing repo-dependent, so it is byte-identical for every caller of
# a given aic version -- which is what "public" means. An hour bounds how long a
# client can keep serving the old list after `pip install -U aic`.
#
# This is a correctness declaration, not an optimisation. A stdio client fetches
# tools/list once per session; the benchmark will show it as noise. Saying so
# here so nobody later reads it as a performance claim and goes looking for the
# measurement that would have to back one.
TOOL_LIST_CACHE = CacheHint(ttl_ms=3_600_000, scope="public")

mcp = MCPServer(
    "aic",
    instructions=(
        "Incremental impact analysis. Call aic_review after making edits to find "
        "out what those edits put at risk; call aic_impact to ask about one "
        "specific file. The graph refreshes itself -- there is nothing to index."
    ),
    cache_hints={"tools/list": TOOL_LIST_CACHE},
)

# Written once by main() before run(), read-only thereafter -- which is why they
# survive the stateless core while `_TOUCHED` did not. The distinction is
# configuration versus accumulated state, not "holds no variables": these are
# argv, identical on every restart, so any number of servers started the same
# way agree. A per-session set was reconstructible from nothing.
_REPO = Path(".")
_DB = None


# --- result shapes -----------------------------------------------------

class Finding(TypedDict):
    path: str
    line: int
    qualname: str
    kind: str
    detail: str


class ReviewResult(TypedDict):
    summary: str
    probe: str
    baseline: str
    changed_files: int
    dependent_files: int
    files_to_recheck: int
    functions_to_recheck: int
    findings: list[Finding]
    showing: int
    total_findings: int
    elapsed_ms: float


class ImpactResult(TypedDict):
    summary: str
    probe: str
    changed_file: str
    dependent_files: int
    dependent_files_pct: float
    files_to_recheck: int
    functions_to_recheck: int
    work_avoided_pct: float
    findings: list[Finding]
    showing: int
    total_findings: int
    elapsed_ms: float


class OverviewResult(TypedDict):
    summary: str
    files: int
    functions: int
    import_edges: int
    markers_by_probe: dict[str, int]
    blast_radius_median: int
    blast_radius_p90: int
    blast_radius_max: int
    blast_radius_mean: float
    largest_import_cycle: int
    largest_import_cycle_pct: float
    elapsed_ms: float


# --- plumbing ----------------------------------------------------------

def _store():
    """A fresh connection per call.

    Sub-millisecond, and it sidesteps SQLite's same-thread rule entirely. The
    cost that mattered -- the ~110 ms interpreter start the CLI paid on every
    invocation -- is already gone by virtue of this process being resident.

    Chosen for that reason, and load-bearing for another one since: SDK v2 runs
    sync handlers on worker threads, and sqlite3 connections default to
    `check_same_thread=True`. A shared connection would have started raising the
    moment two tool calls overlapped.
    """
    return query.create_store(_REPO, _DB)


def _refresh(st):
    """Bring the graph in line with the filesystem before answering.

    A stat-diff over the tree (~50 ms on Django, 2 ms on a small repo). Cheaper
    and more reliable than asking the agent to remember to tell us what it
    changed, and it means a missing graph is simply built on first call.

    No session bookkeeping here any more. `refresh` establishes a baseline if
    the graph has none and never moves one that exists, so the change set stays
    a diff rather than a tally -- there is no state for a second refresh to
    erase, which is what went wrong when this tracked reparses by hand.
    """
    return query.refresh(st, _REPO)


def _probe(name):
    if name not in probes.REGISTRY:
        raise ValueError(
            f"unknown probe {name!r}; available: {', '.join(sorted(probes.REGISTRY))}"
        )
    return name


def _findings(st, probe, fns, module_paths, limit):
    """Ranked, truncated markers for the functions needing recheck.

    Returns (findings, total_before_truncation).
    """
    rows = query.markers_for(st, probe, fns, module_paths)
    total = len(rows)
    # Fanout is a whole-graph computation; it only changes the answer when
    # there is more to show than fits, so don't pay for it otherwise.
    fan = query.fanout_stats(st)["fanout"] if total > limit else None
    ranked = query.rank_markers(rows, fan)[:limit]
    return [
        Finding(path=p, line=ln, qualname=q or "<module>", kind=kind, detail=detail)
        for p, q, kind, detail, ln in ranked
    ], total


def _elided(showing, total):
    if showing >= total:
        return ""
    return f" Showing {showing} of {total}; raise limit= or narrow with probe=."


def _log(tool, args, result, elapsed_ms):
    """One JSON line per call, so dogfooding produces data and not just vibes.

    No lock, deliberately, now that v2 can run two handlers at once: each call
    opens its own handle, writes one ~150-byte line and closes. A sub-PIPE_BUF
    write to an O_APPEND fd is atomic, which covers concurrent threads and
    concurrent processes alike -- the latter being the case a lock would not
    have helped with anyway.
    """
    try:
        path = query.db_for(_REPO, _DB).parent / "mcp-calls.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "tool": tool,
                "args": args,
                "showing": result.get("showing"),
                "total": result.get("total_findings"),
                "bytes": len(json.dumps(result)),
                "elapsed_ms": round(elapsed_ms, 1),
            }) + "\n")
    except OSError as exc:  # never let telemetry break a query
        print(f"aic: could not write call log: {exc}", file=sys.stderr)


# --- tools -------------------------------------------------------------

@mcp.tool(annotations=READ_ONLY, structured_output=True)
def aic_review(probe: str = "security", limit: int = DEFAULT_LIMIT) -> ReviewResult:
    """What the edits made so far put at risk. Call this before declaring work done.

    Covers every file changed since this session started plus everything
    downstream of those changes, intersected with what the probe marks as
    interesting. Probes: security (dangerous sinks, hardcoded credentials, and
    dataflow-confirmed tainted sinks), api (public surface a caller can break
    against), tests (what a change forces you to re-run).
    """
    t0 = time.time()
    probe = _probe(probe)
    with _store() as st:
        refreshed = _refresh(st)
        seeds, info = query.changed_since(st)
        r = query.review(st, probe, seeds=seeds)
        findings, total = _findings(st, probe, r["recheck_fns"], r["scope"], limit)

    changed = len(r["seeds"])
    dependents = len(set(r["scope"]) - set(r["seeds"]))
    at, n = info
    baseline = f"{at} ({n} files)"
    if refreshed["baseline_established"]:
        # This call created the baseline, so everything in the graph is the
        # starting point rather than something the agent did. Distinct from
        # "nothing changed", and the difference matters: one says your edits
        # were safe, the other says nothing was measured. Conflating them is how
        # the old message read as reassurance. Name the tool that still works,
        # because an agent that hits this needs somewhere to go.
        summary = (
            f"No baseline existed -- this call indexed {refreshed['files_on_disk']} "
            "file(s) and recorded them as the starting point, so edits made "
            "before now are invisible to review. Ask aic_impact about a specific "
            "file for an answer that does not need a baseline; call this again "
            "after your next edit."
        )
    elif not changed:
        summary = (
            f"Nothing has changed since the baseline recorded {at}; "
            "nothing to re-check."
        )
    else:
        summary = (
            f"{changed} file(s) changed since the baseline recorded {at}, "
            f"reaching {dependents} dependent file(s). "
            f"{len(r['recheck_fns'])} function(s) match the {probe} probe "
            "and are worth re-checking."
        )
    out = ReviewResult(
        summary=summary + _elided(len(findings), total),
        probe=probe,
        baseline=baseline,
        changed_files=changed,
        dependent_files=max(dependents, 0),
        files_to_recheck=len(r["recheck_files"]),
        functions_to_recheck=len(r["recheck_fns"]),
        findings=findings,
        showing=len(findings),
        total_findings=total,
        elapsed_ms=round((time.time() - t0) * 1000 + refreshed["elapsed_ms"], 1),
    )
    _log("aic_review", {"probe": probe, "limit": limit}, out, out["elapsed_ms"])
    return out


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def aic_impact(file: str, probe: str = "security",
               limit: int = DEFAULT_LIMIT) -> ImpactResult:
    """What a change to one file implicates, repo-relative path.

    Use when you want the blast radius of a specific file rather than of the
    session so far. The dependent-file count is context, not a work list -- what
    to act on is the findings array.
    """
    t0 = time.time()
    probe = _probe(probe)
    with _store() as st:
        refreshed = _refresh(st)
        try:
            r = query.impact(st, file, probe)
        except query.UnknownFile:
            raise ValueError(
                f"{file!r} is not in the graph. Paths are repo-relative "
                f"(e.g. 'django/db/models/query.py'), and only .py files are indexed."
            )
        findings, total = _findings(st, probe, r["recheck_fns"], r["impacted"], limit)

    counts = r["counts"]
    n_fns = len(r["recheck_fns"])
    total_fn = max(counts["functions"], 1)
    avoided = 100 * (1 - n_fns / total_fn)
    out = ImpactResult(
        summary=(
            f"{file} is depended on by {len(r['impacted'])} of {counts['files']} files. "
            f"{n_fns} of {counts['functions']} functions match the {probe} probe and are "
            f"worth re-checking ({avoided:.1f}% of a full scan avoided)."
        ) + _elided(len(findings), total),
        probe=probe,
        changed_file=file,
        dependent_files=len(r["impacted"]),
        dependent_files_pct=round(100 * len(r["impacted"]) / max(counts["files"], 1), 1),
        files_to_recheck=len(r["recheck_files"]),
        functions_to_recheck=n_fns,
        work_avoided_pct=round(avoided, 1),
        findings=findings,
        showing=len(findings),
        total_findings=total,
        elapsed_ms=round((time.time() - t0) * 1000 + refreshed["elapsed_ms"], 1),
    )
    _log("aic_impact", {"file": file, "probe": probe, "limit": limit}, out,
         out["elapsed_ms"])
    return out


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def aic_overview() -> OverviewResult:
    """Shape of this repo: size, what each probe marks, and how far changes travel.

    Worth one call at the start of a session. The blast-radius distribution says
    whether changes here are usually cheap to verify or usually expensive -- a
    median far below the mean means most changes are local and a minority are
    not, and the expensive minority are the files inside the largest import cycle.
    """
    t0 = time.time()
    with _store() as st:
        refreshed = _refresh(st)
        f = query.fanout_stats(st)

    counts = f["counts"]
    pct = f["percentiles"]
    cycle_pct = 100 * f["largest_component"] / max(counts["files"], 1)
    out = OverviewResult(
        summary=(
            f"{counts['files']} files / {counts['functions']} functions. "
            f"A typical change reaches {pct[50]} file(s) (median) but the worst "
            f"reaches {f['max']}. The {f['largest_component']}-file import cycle "
            f"({cycle_pct:.0f}% of the repo) is where incremental analysis stops paying."
        ),
        files=counts["files"],
        functions=counts["functions"],
        import_edges=counts["imports"],
        markers_by_probe=refreshed["markers_by_probe"],
        blast_radius_median=pct[50],
        blast_radius_p90=pct[90],
        blast_radius_max=f["max"],
        blast_radius_mean=round(f["mean"], 1),
        largest_import_cycle=f["largest_component"],
        largest_import_cycle_pct=round(cycle_pct, 1),
        elapsed_ms=round((time.time() - t0) * 1000, 1),
    )
    _log("aic_overview", {}, out, out["elapsed_ms"])
    return out


# --- entry point -------------------------------------------------------

def main(argv=None):
    global _REPO, _DB
    ap = argparse.ArgumentParser(
        prog="aic-mcp", description="Serve aic's impact analysis over MCP (stdio).")
    ap.add_argument("repo", nargs="?", default=".",
                    help="repository root to analyze (default: cwd)")
    ap.add_argument("--db", metavar="PATH",
                    help="where to keep the graph (default <repo>/.aic/graph.db). "
                         "Use this, or AIC_DB_DIR=DIR, to analyze a tree you "
                         "cannot write to")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        sys.exit(f"{args.repo!r} is not a directory")
    _REPO = repo
    _DB = args.db

    try:
        query.create_store(repo, _DB).close()
    except query.GraphUnwritable as exc:
        sys.exit(f"aic-mcp: {exc}")

    print(f"aic-mcp serving {repo} (graph at {query.db_for(repo, _DB)})", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
