"""Export the import graph of each benchmark package as an embeddable blob.

Reads the same pinned sdists `bench/run.py` uses, indexes each with the real
engine, and writes a single self-contained HTML page with the graphs inlined.
Nothing is hand-curated: every number on the page comes out of `aic`.

    python viz/export.py            # -> viz/blast-radius.html
    python viz/export.py --standalone PATH   # also write a full HTML document

The page needs no server, no build step and no network. It is one file.

Two shapes, one source. The default output is body content, which is what an
embedding host wants. `--standalone` wraps the same bytes in a complete document
-- doctype included, because a page served without one renders in quirks mode --
for dropping straight into a static site's public directory.
"""

import argparse
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


DOC = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Click any file in five real Python packages \
and watch which files a change to it would force you to re-check.">
{head}</head>
<body>
{body}</body>
</html>
"""

# Elements that belong in <head> when this becomes a real document. In the
# embedded shape they sit at the top of the body, which browsers tolerate; in a
# standalone document they should be where they belong.
HEAD_TAGS = ("<title", "<link rel=\"preconnect\"", "<link rel=\"stylesheet\"", "<style>")


def split_head(html):
    """Lift <title>, the font links and the stylesheet out of the body."""
    head, body, rest = [], [], html
    while True:
        starts = [(rest.index(t), t) for t in HEAD_TAGS if t in rest]
        if not starts:
            break
        i, tag = min(starts)
        close = "</style>" if tag == "<style>" else ("</title>" if tag == "<title" else ">")
        j = rest.index(close, i) + len(close)
        body.append(rest[:i])
        head.append(rest[i:j])
        rest = rest[j:]
    body.append(rest)
    return "\n".join(h.strip() for h in head), "".join(body).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--standalone", metavar="PATH",
                    help="also write a complete HTML document here "
                         "(e.g. a static site's public/ directory)")
    args = ap.parse_args()

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

    if args.standalone:
        head, body = split_head(html)
        doc = DOC.format(head=head, body=body)
        dest = Path(args.standalone).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(doc, encoding="utf-8")
        print(f"{dest}  {len(doc) / 1024:.0f} kB  (standalone)")


if __name__ == "__main__":
    main()
