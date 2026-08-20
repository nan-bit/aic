"""Benchmark AIC against well-known PyPI packages.

    python bench/run.py                 # all targets, writes bench/RESULTS.md
    python bench/run.py --only django
    python bench/run.py --keep          # keep downloaded sources for poking at
    python bench/run.py --edit-dist     # also time an edit to every file (slow)

Targets are pinned sdists from PyPI so anyone can reproduce the numbers. First
run downloads (~30s); sources are cached under bench/.cache afterwards.

What is measured, per package:
  cold ms      full index from scratch
  warm ms      re-index with nothing changed
  edit ms      re-index after touching exactly one file
  dirty        dependents marked DIRTY by that one-file edit
  fanout       distribution of blast radius over every file in the package
  edit_dist    with --edit-dist, the same for edit cost: every file timed, plus
               the rank correlation between a file's blast radius and its cost

The fanout distribution is the point. Two hand-picked files prove nothing; the
distribution says how often incremental analysis pays off, and where it stops.
"""

import argparse
import json
import platform
import shutil
import statistics
import subprocess
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import analyze                      # noqa: E402
from aic import query                        # noqa: E402
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


def timed_index(pkg_dir):
    """Run a full index pass, return elapsed ms.

    Times the query layer directly rather than the CLI. Benchmarking through a
    printer meant redirecting stdout to keep the output clean, and it made the
    numbers depend on a presentation layer that has nothing to do with them.
    """
    t0 = time.time()
    with Store(query.db_for(pkg_dir)) as st:
        query.refresh(st, pkg_dir)
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


def spearman(xs, ys):
    """Rank correlation. Pearson would be wrong here: blast radius is heavily
    skewed (Django's median is 3 and its max is 588), so a handful of hub files
    would dominate a linear fit. Ranks ask the question actually being asked --
    do costlier edits tend to be the wider-reaching ones -- without assuming the
    relationship is linear."""
    n = len(xs)
    if n < 2:
        return None

    def ranked(vs):
        order = sorted(range(n), key=lambda i: vs[i])
        r = [0.0] * n
        i = 0
        while i < n:                      # average the ranks of ties
            j = i
            while j + 1 < n and vs[order[j + 1]] == vs[order[i]]:
                j += 1
            share = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = share
            i = j + 1
        return r

    rx, ry = ranked(xs), ranked(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx)
    dy = sum((b - my) ** 2 for b in ry)
    if not dx or not dy:
        return None
    return round(num / (dx * dy) ** 0.5, 2)


def measure_edit_distribution(pkg_dir, fan):
    """Time absorbing a one-file edit, for every file in the package.

    The single `edit_ms` above is one sample from this distribution, taken at the
    median-blast-radius file. That is enough to say what a typical edit costs and
    not enough to say anything about spread, which is what the docs were claiming
    from two hand-measured examples.

    Two index passes per file: one timed, absorbing the edit, and one untimed to
    put the graph back in a clean state so the next file starts from the same
    place. Expensive by construction, hence opt-in.
    """
    samples = []
    paths = sorted(fan)
    total = len(paths)
    for i, rel in enumerate(paths, 1):
        f = pkg_dir / rel
        try:
            original = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue          # unreadable or not utf-8; nothing to time
        try:
            f.write_text(original + "\n# aic bench touch\n", encoding="utf-8")
            ms = timed_index(pkg_dir)
        finally:
            f.write_text(original, encoding="utf-8")
        timed_index(pkg_dir)  # back to clean, untimed
        samples.append({
            "path": rel,
            "ms": round(ms, 1),
            "radius": fan[rel],
            "bytes": len(original.encode("utf-8")),
        })
        if i % 50 == 0 or i == total:
            print(f"    edit distribution {i}/{total}", flush=True)
    return samples


def summarize_edits(samples):
    """Percentiles over the per-file edit costs, plus what the cost actually
    tracks.

    Two correlations, because the interesting result is the contrast. The docs
    have long explained the spread in edit cost as a blast-radius effect. It is
    not: absorbing an edit reparses and re-probes the file that changed, which
    scales with that file's size, and then marks its dependents dirty, which is
    a flag on each and costs almost nothing. Measuring both says so instead of
    asserting it."""
    if not samples:
        return None
    ms = sorted(s["ms"] for s in samples)
    pct = analyze.percentiles(ms)
    return {
        "files_timed": len(samples),
        "min": ms[0],
        "p50": pct[50], "p75": pct[75], "p90": pct[90], "p99": pct[99],
        "max": ms[-1],
        "mean": round(statistics.mean(ms), 1),
        "radius_correlation": spearman(
            [s["radius"] for s in samples], [s["ms"] for s in samples]
        ),
        "size_correlation": spearman(
            [s["bytes"] for s in samples], [s["ms"] for s in samples]
        ),
        "samples": samples,
    }


def measure(name, version, subdir, edit_dist=False):
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

    edits = summarize_edits(measure_edit_distribution(pkg_dir, fan)) if edit_dist else None

    values = sorted(fan.values())
    pct = analyze.percentiles(values)
    n = len(values) or 1

    return {
        "_edit_dist": edits,          # split out before writing; see main()
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


def host():
    """One line describing where the timings came from."""
    return (f"{platform.system()} {platform.machine()}, "
            f"Python {platform.python_version()}")


def render(rows):
    L = []
    L.append("# Benchmarks\n")
    L.append("Generated by `python bench/run.py`. Sources are pinned sdists from "
             "PyPI, so the *structural* numbers below reproduce anywhere: blast radius "
             "and probe selectivity are properties of the graph, not of the machine.\n")
    L.append(f"\nTimings are not. They were measured on {host()}, on a machine that was "
             "also doing other things. Treat them as one sample, and compare within a "
             "run rather than across runs.\n")

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
    L.append("\nMedian far below mean is the signal where it appears: most changes are "
             "cheap to verify, a minority are not, and the expensive minority are the "
             "files inside the largest import cycle.\n")
    L.append("\nIt does not appear everywhere, and the rows where it is absent say "
             "something too. A package whose median sits at or above its mean has no "
             "cheap majority to find, because enough of it is one import cycle that "
             "nearly every file reaches nearly every other. That is the shape that "
             "tells you incremental analysis will not help.\n")

    if any(r.get("_edit_dist") for r in rows):
        L.append("\n## Cost of absorbing an edit\n")
        L.append("Time to re-index after touching one file, measured for *every* file "
                 "in the package rather than a sampled one.\n")
        L.append("| package | files timed | min | median | p90 | max | mean | floor share | vs blast radius | vs file size |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for r in rows:
            e = r.get("_edit_dist")
            if not e:
                continue
            def corr(k):
                return "n/a" if e[k] is None else f"{e[k]:+.2f}"
            floor = round(100 * e["min"] / e["mean"]) if e["mean"] else 0
            L.append(f"| {r['package']} | {e['files_timed']} | {e['min']} ms | {e['p50']} ms | "
                     f"{e['p90']} ms | {e['max']} ms | {e['mean']} ms | {floor}% | "
                     f"{corr('radius_correlation')} | {corr('size_correlation')} |")
        L.append("\nThe correlations are Spearman rank correlations against a file's blast "
                 "radius and against its size in bytes. Cost tracks the size of the file you "
                 "edited, not how far the edit reaches: absorbing an edit reparses and "
                 "re-probes the changed file, then sets a flag on each dependent, and setting "
                 "a flag is nearly free.\n")
        L.append("\n*Floor share* is the cheapest edit as a percentage of the mean, and it is "
                 "why the size correlation falls off on the largest target. Where the floor is "
                 "high, most of the measurement is the constant re-walk of the tree and only "
                 "the remainder varies with the file, so the correlation is computed on a small "
                 "residual and is not stable between runs. Read the floor on those rows, not "
                 "the coefficient. A high floor is the incremental result stated another way: "
                 "the edit costs about the same whichever file you touch.\n")

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
    ap.add_argument("--edit-dist", action="store_true",
                    help="time an edit to every file, not just one (slow: two "
                         "index passes per file, minutes on the larger targets)")
    args = ap.parse_args()

    targets = [t for t in TARGETS if not args.only or t[0] == args.only]
    if not targets:
        sys.exit(f"no target named {args.only!r}; have: {', '.join(t[0] for t in TARGETS)}")

    rows = []
    for name, version, subdir in targets:
        print(f"{name} {version}", flush=True)
        try:
            row = measure(name, version, subdir, edit_dist=args.edit_dist)
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

    # Two artifacts, deliberately. viz/export.py reads both to put the timings on
    # the page, and the page previously carried hand-typed numbers that drifted
    # from the table above precisely because nothing generated them.
    #
    # results.json is written every run. The edit distribution is not: it only
    # exists under --edit-dist, and folding it into the same file meant an
    # ordinary run would overwrite a measured distribution with null. Separate
    # files mean a fast run cannot destroy the slow run's output, and neither
    # file ever carries a value its own run did not produce.
    dists = {r["package"]: r.pop("_edit_dist") for r in rows}

    js = BENCH / "results.json"
    js.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {js.relative_to(BENCH.parent)}")

    if args.edit_dist:
        ed = BENCH / "edit-distribution.json"
        ed.write_text(json.dumps(dists, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {ed.relative_to(BENCH.parent)}")
    elif (BENCH / "edit-distribution.json").exists():
        print("(edit-distribution.json left alone; pass --edit-dist to remeasure)")
    if not args.keep:
        print("(sources cached in bench/.cache -- delete to reclaim space)")


if __name__ == "__main__":
    main()
