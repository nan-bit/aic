"""Benchmark AIC against well-known PyPI packages.

    python bench/run.py                 # all targets, writes bench/RESULTS.md
    python bench/run.py --only django
    python bench/run.py --keep          # keep downloaded sources for poking at

Targets are pinned sdists from PyPI so anyone can reproduce the numbers. First
run downloads (~30s); sources are cached under bench/.cache afterwards.

What is measured, per package:
  cold ms      full index from scratch
  warm ms      re-index with nothing changed
  edit ms      re-index after touching exactly one file
  dirty        dependents marked DIRTY by that one-file edit
  fanout       distribution of blast radius over every file in the package

The fanout distribution is the point. Two hand-picked files prove nothing; the
distribution says how often incremental analysis pays off, and where it stops.
"""

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import analyze                      # noqa: E402
from aic.cli import cmd_index                # noqa: E402
from aic.store import Store                  # noqa: E402

BENCH = Path(__file__).resolve().parent
CACHE = BENCH / ".cache"

# (pypi name, pinned version, package subdir inside the sdist)
TARGETS = [
    ("requests",   "2.32.3",  "src/requests"),
    ("flask",      "3.0.3",   "src/flask"),
    ("celery",     "5.4.0",   "celery"),
    ("sqlalchemy", "2.0.36",  "lib/sqlalchemy"),
    ("django",     "5.2.16",  "django"),
]


def fetch(name, version):
    """Download and unpack a pinned sdist. Returns the extracted root."""
    dest = CACHE / f"{name}-{version}"
    if dest.exists():
        return dest
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = CACHE / f".dl-{name}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    print(f"  downloading {name}=={version} ...", flush=True)
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "download", f"{name}=={version}",
         "--no-deps", "--no-binary", ":all:", "-d", str(tmp)],
        capture_output=True, text=True,
    )
    archives = list(tmp.glob("*.tar.gz")) + list(tmp.glob("*.zip"))
    if proc.returncode != 0 or not archives:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"download failed for {name}=={version}: {proc.stderr[-300:]}")

    with tarfile.open(archives[0]) as tf:
        members = tf.getnames()
        root = members[0].split("/")[0]
        tf.extractall(tmp)
    (tmp / root).rename(dest)
    shutil.rmtree(tmp, ignore_errors=True)
    return dest


class _Args:
    def __init__(self, repo):
        self.repo = repo


def timed_index(pkg_dir, quiet=True):
    """Run a full index pass, return elapsed ms."""
    t0 = time.time()
    if quiet:
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_index(_Args(str(pkg_dir)))
    else:
        cmd_index(_Args(str(pkg_dir)))
    return (time.time() - t0) * 1000


def pick_edit_target(pkg_dir):
    """A mid-sized file that is not the largest hub -- representative, not
    cherry-picked for a flattering number."""
    with Store(pkg_dir / ".aic" / "graph.db") as st:
        paths = st.all_paths()
        fan = analyze.fanout(paths, st.import_edges())
    if not fan:
        return None
    ordered = sorted(fan.items(), key=lambda kv: kv[1])
    return ordered[len(ordered) // 2][0]      # median blast radius


def measure(name, version, subdir):
    src = fetch(name, version)
    pkg_dir = src / subdir
    if not pkg_dir.exists():
        raise RuntimeError(f"{name}: expected package dir {subdir!r} not found in sdist")

    shutil.rmtree(pkg_dir / ".aic", ignore_errors=True)

    cold = timed_index(pkg_dir)
    warm = timed_index(pkg_dir)

    target = pick_edit_target(pkg_dir)
    edit_ms = dirty = None
    if target:
        f = pkg_dir / target
        original = f.read_text(encoding="utf-8")
        try:
            f.write_text(original + "\n# aic bench touch\n", encoding="utf-8")
            edit_ms = timed_index(pkg_dir)
            with Store(pkg_dir / ".aic" / "graph.db") as st:
                dirty = len(st.dirty())
        finally:
            f.write_text(original, encoding="utf-8")
        timed_index(pkg_dir)   # restore graph to a clean state

    with Store(pkg_dir / ".aic" / "graph.db") as st:
        counts = st.counts()
        markers = st.marker_counts()
        paths = st.all_paths()
        edges = st.import_edges()
        fan = analyze.fanout(paths, edges)
        comps = analyze.strongly_connected(paths, edges)
        reach = {
            p: len(analyze.marker_reachable(
                st.marked_functions(p), st.call_edges(), st.functions_by_name(), edges))
            for p in ("security", "api", "tests")
        }

    values = sorted(fan.values())
    pct = analyze.percentiles(values)
    n = len(values) or 1

    return {
        "package": f"{name} {version}",
        "files": counts["files"],
        "functions": counts["functions"],
        "imports": counts["imports"],
        "markers": markers,
        "reachable": reach,
        "cold_ms": round(cold),
        "warm_ms": round(warm),
        "edit_ms": round(edit_ms) if edit_ms else None,
        "edit_file": target,
        "edit_dirty": dirty,
        "sccs": len(comps),
        "core_scc": len(comps[0]) if comps else 0,
        "fanout": {
            "min": values[0] if values else 0,
            "p50": pct[50], "p75": pct[75], "p90": pct[90], "p99": pct[99],
            "max": values[-1] if values else 0,
            "mean": round(statistics.mean(values), 1) if values else 0,
            "share_le_10": round(sum(1 for v in values if v <= 10) / n * 100, 1),
        },
    }


def render(rows):
    L = []
    L.append("# Benchmarks\n")
    L.append("Generated by `python bench/run.py`. Sources are pinned sdists from "
             "PyPI, so these numbers are reproducible.\n")

    L.append("\n## Cost of staying current\n")
    L.append("| package | files | functions | cold | warm | 1-file edit | dependents dirtied |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        L.append(f"| {r['package']} | {r['files']} | {r['functions']} | {r['cold_ms']} ms | "
                 f"{r['warm_ms']} ms | {r['edit_ms']} ms | {r['edit_dirty']} |")
    L.append("\nA stateless analyzer pays the *cold* column on every invocation. "
             "An agent making 40 edits pays it 40 times.\n")

    L.append("\n## Blast radius\n")
    L.append("Files implicated by a change to one file, computed for every file "
             "in the package.\n")
    L.append("| package | median | p75 | p90 | p99 | max | mean | <=10 files | largest import cycle |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        f = r["fanout"]
        L.append(f"| {r['package']} | {f['p50']} | {f['p75']} | {f['p90']} | {f['p99']} | "
                 f"{f['max']} | {f['mean']} | {f['share_le_10']}% | "
                 f"{r['core_scc']} ({round(100*r['core_scc']/max(r['files'],1))}%) |")
    L.append("\nMedian far below mean is the signal: most changes are cheap to verify, "
             "a minority are not. The expensive minority are the files inside the "
             "largest import cycle.\n")

    L.append("\n## Probe selectivity\n")
    L.append("How much of the codebase each probe considers relevant. A probe that "
             "flags most of the repo is not a filter.\n")
    L.append("| package | security | api | tests |")
    L.append("|---|---:|---:|---:|")
    for r in rows:
        fn = max(r["functions"], 1)
        cells = " | ".join(
            f"{r['reachable'][p]} ({100*r['reachable'][p]/fn:.1f}%)"
            for p in ("security", "api", "tests")
        )
        L.append(f"| {r['package']} | {cells} |")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run a single target by name")
    ap.add_argument("--keep", action="store_true", help="keep downloaded sources")
    ap.add_argument("--json", action="store_true", help="also emit bench/results.json")
    args = ap.parse_args()

    targets = [t for t in TARGETS if not args.only or t[0] == args.only]
    if not targets:
        sys.exit(f"no target named {args.only!r}; have: {', '.join(t[0] for t in TARGETS)}")

    rows = []
    for name, version, subdir in targets:
        print(f"{name} {version}", flush=True)
        try:
            row = measure(name, version, subdir)
        except Exception as exc:                     # keep going; report at the end
            print(f"  SKIPPED: {exc}", file=sys.stderr)
            continue
        rows.append(row)
        f = row["fanout"]
        print(f"  {row['files']} files, cold {row['cold_ms']}ms, warm {row['warm_ms']}ms, "
              f"edit {row['edit_ms']}ms, median blast radius {f['p50']}", flush=True)

    if not rows:
        sys.exit("no results")

    out = BENCH / "RESULTS.md"
    out.write_text(render(rows), encoding="utf-8")
    print(f"\nwrote {out.relative_to(BENCH.parent)}")

    if args.json:
        (BENCH / "results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    if not args.keep:
        print("(sources cached in bench/.cache -- delete to reclaim space)")


if __name__ == "__main__":
    main()
