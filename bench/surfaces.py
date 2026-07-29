"""Surface benchmark: CLI vs MCP server, same question, same graph.

    python bench/surfaces.py          # writes bench/SURFACES.md

Run it from an environment where `aic` and `aic-mcp` are installed as console
scripts -- the point is to measure what a caller actually pays, and that includes
interpreter startup and, for MCP, the JSON-RPC round trip. Importing the library
in-process would hide both.

Three arms, because two of them answer different questions:

  cli-query    `aic impact` alone. Fast, but answers from whatever the graph
               last knew. If the tree moved since the last index, it is stale.
  cli-fresh    `aic index` then `aic impact`. A current answer the CLI way:
               two processes, two interpreter starts.
  mcp          One `aic_impact` round trip. The tool stat-diffs the tree before
               answering, so this is current by construction -- the same work as
               cli-fresh, minus the process starts.

cli-fresh vs mcp is the honest comparison. cli-query is reported to show how much
of the CLI's cost is startup rather than analysis.
"""

import asyncio
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run import TARGETS, fetch                     # noqa: E402
from aic import analyze, query                     # noqa: E402
from aic.store import Store                        # noqa: E402

BENCH = Path(__file__).resolve().parent
BIN = Path(sys.executable).parent                  # console scripts live next to python
AIC = BIN / "aic"
AIC_MCP = BIN / "aic-mcp"
ROUNDS = 10                                        # measured calls per arm, after a warm-up


def _median_ms(samples):
    return statistics.median(samples) * 1000


def target_file(pkg_dir):
    """A file with median blast radius -- representative, not cherry-picked."""
    with Store(query.db_for(pkg_dir)) as st:
        fan = analyze.fanout(st.all_paths(), st.import_edges())
    if not fan:
        return None, 0
    ordered = sorted(fan.items(), key=lambda kv: kv[1])
    return ordered[len(ordered) // 2]


def time_cmd(*argv):
    t0 = time.perf_counter()
    proc = subprocess.run([str(a) for a in argv], capture_output=True, text=True)
    dt = time.perf_counter() - t0
    if proc.returncode != 0:
        raise RuntimeError(f"{argv[1] if len(argv) > 1 else argv[0]} failed: {proc.stderr[-300:]}")
    return dt


def measure_cli(pkg_dir, rel):
    """(cli_query_samples, cli_fresh_samples), seconds."""
    time_cmd(AIC, "impact", pkg_dir, rel)                       # warm-up
    q = [time_cmd(AIC, "impact", pkg_dir, rel) for _ in range(ROUNDS)]
    fresh = []
    for _ in range(ROUNDS):
        t0 = time.perf_counter()
        time_cmd(AIC, "index", pkg_dir)
        time_cmd(AIC, "impact", pkg_dir, rel)
        fresh.append(time.perf_counter() - t0)
    return q, fresh


async def measure_mcp(pkg_dir, rel):
    """(startup_seconds, call_samples, response_bytes).

    **Startup is spawn to first usable answer, minus one warm call.** It used to
    be spawn until `initialize()` returned, which the 2026-07-28 revision made
    unmeasurable -- there is no handshake to wait for any more. Keeping that
    definition would have reported startup collapsing to nearly nothing while
    the server still paid its whole import cost before it could answer, which is
    a win manufactured by moving the goalposts rather than by getting faster.

    Subtracting one warm call is what keeps the number comparable to the old
    one: it removes the query itself and leaves process start plus imports,
    which is what the old handshake-completion point was standing in for.
    """
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=str(AIC_MCP), args=[str(pkg_dir)])

    async def spawn_once(measure_calls):
        t0 = time.perf_counter()
        async with Client(stdio_client(params)) as session:
            res = await session.call_tool("aic_impact", {"file": rel})
            first = time.perf_counter() - t0
            size = len(res.content[0].text)
            calls = []
            if measure_calls:
                for _ in range(ROUNDS):
                    t = time.perf_counter()
                    await session.call_tool("aic_impact", {"file": rel})
                    calls.append(time.perf_counter() - t)
            return first, calls, size

    # Startup is the median of three spawns after a throwaway. The very first
    # spawn on a cold import/filesystem cache runs ~2x the steady state, which
    # made whichever package happened to be measured first look like an outlier.
    await spawn_once(False)
    firsts = [(await spawn_once(False))[0] for _ in range(2)]
    first, samples, payload = await spawn_once(True)
    firsts.append(first)
    warm = statistics.median(samples) if samples else 0
    return max(statistics.median(firsts) - warm, 0), samples, payload


def measure(name, version, subdir):
    src = fetch(name, version)
    pkg_dir = src / subdir
    if not pkg_dir.exists():
        raise RuntimeError(f"{name}: package dir {subdir!r} not in sdist")

    time_cmd(AIC, "index", pkg_dir)                              # graph must exist
    rel, fan = target_file(pkg_dir)
    if rel is None:
        return None

    with Store(query.db_for(pkg_dir)) as st:
        files = st.counts()["files"]

    cli_q, cli_fresh = measure_cli(pkg_dir, rel)
    startup, mcp_calls, payload = asyncio.run(measure_mcp(pkg_dir, rel))

    # Where an MCP call's time actually goes, measured in-process. Neither part
    # is transport: both surfaces pay them.
    with Store(query.db_for(pkg_dir)) as st:
        query.refresh(st, pkg_dir)
        t = time.perf_counter(); query.refresh(st, pkg_dir)
        refresh_ms = (time.perf_counter() - t) * 1000
        t = time.perf_counter(); query.impact(st, rel, "security")
        query_ms = (time.perf_counter() - t) * 1000

    q_ms, fresh_ms, mcp_ms = _median_ms(cli_q), _median_ms(cli_fresh), _median_ms(mcp_calls)
    saved = fresh_ms - mcp_ms
    return {
        "package": f"{name} {version}",
        "files": files,
        "target": rel,
        "fanout": fan,
        "cli_query_ms": q_ms,
        "cli_fresh_ms": fresh_ms,
        "mcp_ms": mcp_ms,
        "mcp_startup_ms": startup * 1000,
        "speedup": fresh_ms / mcp_ms if mcp_ms else 0,
        # How many questions before the resident server has repaid its own start.
        "breakeven": (startup * 1000 / saved) if saved > 0 else None,
        "payload_bytes": payload,
        "refresh_ms": refresh_ms,
        "query_ms": query_ms,
    }


def render(rows):
    out = ["# Surface benchmark — CLI vs MCP", ""]
    out += [
        "Generated by `python bench/surfaces.py`, with `aic` and `aic-mcp` installed as",
        "console scripts. Medians over "
        f"{ROUNDS} calls after a warm-up, asking the same question of the same graph.",
        "",
        "The question is `impact` on a median-blast-radius file: representative of a",
        "typical edit rather than the worst case.",
        "",
        "## Cost of one answer",
        "",
        "| package | files | `aic impact` | `aic index` + `impact` | MCP call | speedup |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        out.append(
            f"| {r['package']} | {r['files']} | {r['cli_query_ms']:.0f} ms | "
            f"{r['cli_fresh_ms']:.0f} ms | **{r['mcp_ms']:.0f} ms** | {r['speedup']:.1f}× |"
        )
    out += [
        "",
        "`aic impact` answers from whatever the graph last knew. The middle column is a",
        "*current* answer the CLI way -- index, then ask -- which is the like-for-like",
        "comparison, because the MCP tool stat-diffs the tree before every answer.",
        "",
        "## What the resident process costs, and when it pays for itself",
        "",
        "| package | server startup | saved per question | break-even |",
        "|---|---:|---:|---:|",
    ]
    for r in rows:
        be = f"{r['breakeven']:.1f} questions" if r["breakeven"] else "immediate"
        out.append(
            f"| {r['package']} | {r['mcp_startup_ms']:.0f} ms | "
            f"{r['cli_fresh_ms'] - r['mcp_ms']:.0f} ms | {be} |"
        )
    out += [
        "",
        "Startup is paid once per session; the client spawns the server and keeps it.",
        "Break-even is how many questions it takes to repay that -- below it, shelling out",
        "to the CLI is genuinely cheaper, which is worth knowing before assuming a server",
        "is the right answer.",
        "",
        "## Where an MCP call's time goes",
        "",
        "| package | stat-diff refresh | impact query | process overhead removed |",
        "|---|---:|---:|---:|",
    ]
    for r in rows:
        out.append(
            f"| {r['package']} | {r['refresh_ms']:.0f} ms | {r['query_ms']:.0f} ms | "
            f"{r['cli_fresh_ms'] - r['mcp_ms']:.0f} ms |"
        )
    out += [
        "",
        "Neither of the first two columns is transport -- both surfaces pay them. What a",
        "resident process removes is the third: a fixed cost of process starts, roughly",
        "35 ms per invocation.",
        "",
        "This is why the speedup shrinks as repos grow. On `requests` the fixed overhead",
        "is most of the total, so removing it is worth ~9x. On Django the query itself",
        "dominates and the surface barely matters. The next optimisation is therefore not",
        "the transport: it is `marker_reachable`, which recomputes reachability across the",
        "whole call graph on every query and is the obvious candidate for caching against",
        "the dirty set.",
        "",
        "## Response size",
        "",
        "| package | target file | dependents | MCP response |",
        "|---|---|---:|---:|",
    ]
    for r in rows:
        out.append(
            f"| {r['package']} | `{r['target']}` | {r['fanout']} | "
            f"{r['payload_bytes'] / 1024:.1f} kB |"
        )
    out += [
        "",
        "Responses are ranked and truncated to a default of 20 findings, so size tracks",
        "the cap rather than the size of the repo. See the README for the worst-case",
        "figure on Django's most-depended-on file.",
        "",
    ]
    return "\n".join(out)


def main():
    for exe in (AIC, AIC_MCP):
        if not exe.exists():
            sys.exit(f"{exe} not found -- install with `pip install -e \".[mcp]\"` first")

    rows = []
    for name, version, subdir in TARGETS:
        print(f"  {name} ...", flush=True)
        row = measure(name, version, subdir)
        if row:
            rows.append(row)

    (BENCH / "SURFACES.md").write_text(render(rows), encoding="utf-8")
    (BENCH / "surfaces.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\nwrote {BENCH / 'SURFACES.md'}")
    for r in rows:
        print(f"  {r['package']:22s} cli-fresh {r['cli_fresh_ms']:6.0f} ms   "
              f"mcp {r['mcp_ms']:6.0f} ms   {r['speedup']:.1f}x")


if __name__ == "__main__":
    main()
