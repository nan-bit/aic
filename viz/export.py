"""Export the import graph of each benchmark package as an embeddable blob.

Reads the same pinned sdists `bench/run.py` uses, indexes each with the real
engine, and writes a single self-contained HTML page with the graphs inlined.
Nothing is hand-curated: every number on the page comes out of `aic`.

    python viz/export.py            # -> viz/blast-radius.html

The page needs no server, no build step and no network. It is one file.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bench"))

from aic import analyze, query                  # noqa: E402
from aic.store import Store                     # noqa: E402
from run import TARGETS, fetch                  # noqa: E402

OUT = ROOT / "viz" / "blast-radius.html"
TEMPLATE = ROOT / "viz" / "template.html"


def group_of(rel):
    """Top-level directory, which is the coarsest structure a reader already
    has a mental model for. Root-level files get their own bucket."""
    head, _, tail = rel.partition("/")
    return head if tail else "·"


def graph_for(name, version, subdir):
    src = fetch(name, version)
    pkg_dir = src / subdir
    if not pkg_dir.exists():
        raise RuntimeError(f"{name}: {subdir!r} not in sdist")

    with Store(query.db_for(pkg_dir)) as st:
        query.refresh(st, pkg_dir)
        paths = sorted(st.all_paths())
        edges = st.import_edges()
        fan = analyze.fanout(paths, edges)
        comps = analyze.strongly_connected(paths, edges)

    idx = {p: i for i, p in enumerate(paths)}
    flat = sorted(
        [idx[s], idx[d]] for s, dsts in edges.items() for d in dsts
        if s in idx and d in idx
    )

    biggest = max(comps, key=len) if comps else []
    cycle = sorted(idx[p] for p in biggest if p in idx) if len(biggest) > 1 else []

    values = sorted(fan.get(p, 1) for p in paths)
    pct = analyze.percentiles(values)
    return {
        "name": name,
        "version": version,
        "files": [{"p": p, "g": group_of(p), "f": fan.get(p, 1)} for p in paths],
        "edges": flat,
        "cycle": cycle,
        "stats": {
            "median": pct[50],
            "p90": pct[90],
            "max": values[-1],
            "mean": round(sum(values) / len(values), 1),
            "cheap": round(100 * sum(1 for v in values if v <= 10) / len(values), 1),
        },
    }


def main():
    graphs = []
    for name, version, subdir in TARGETS:
        try:
            g = graph_for(name, version, subdir)
        except Exception as exc:                          # keep going, report at the end
            print(f"  SKIPPED {name}: {exc}", file=sys.stderr)
            continue
        print(f"{name} {version}: {len(g['files'])} files, {len(g['edges'])} edges")
        graphs.append(g)

    if not graphs:
        sys.exit("no packages indexed")

    blob = json.dumps(graphs, separators=(",", ":"))
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", blob)
    OUT.write_text(html, encoding="utf-8")
    print(f"\n{OUT.relative_to(ROOT)}  {len(html) / 1024:.0f} kB")


if __name__ == "__main__":
    main()
