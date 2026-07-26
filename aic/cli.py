"""aic -- incremental impact analysis.

    aic index  <repo>                  build or update the graph
    aic status <repo> [--probe P]      what the graph holds
    aic impact <repo> <file> [--probe P]   what a change to <file> implicates
    aic touch  <repo> <file>...        invalidate named files, no repo walk
    aic fanout <repo>                  distribution of blast radius across the repo

The claim `index` exists to demonstrate: the second run is nearly free. A
stateless analyzer redoes the whole repo on every invocation, so an agent making
forty edits pays for forty full scans.

This module only renders. The computation lives in aic/query.py, which the MCP
server shares -- see the note there about why stdout is off limits to it.
"""

import argparse
import sys
from pathlib import Path

from . import probes, query
from .query import db_for  # noqa: F401  -- re-exported; callers import it from here
from .store import Store


def _open(repo):
    try:
        return query.open_store(repo)
    except query.GraphMissing:
        sys.exit("no graph yet -- run `index` first")


# --- commands ----------------------------------------------------------

def cmd_index(args):
    repo = Path(args.repo).resolve()
    with Store(db_for(repo)) as st:
        r = query.refresh(st, repo, rehash=getattr(args, "rehash", False))

    print(f"mode                      {r['mode']}")
    print(f"files on disk             {r['files_on_disk']}")
    print(f"  stat-changed            {r['stat_changed']}")
    print(f"  reparsed                {len(r['reparsed'])}")
    print(f"  skipped (unchanged)     {r['skipped']}")
    print(f"  evicted (deleted)       {len(r['evicted'])}")
    if r["failures"]:
        print(f"  parse failures          {r['failures']}")
    print(f"marked dirty (dependents) {len(r['dirtied'])}")
    counts = r["counts"]
    print(f"graph                     {counts['files']} files / {counts['functions']} functions "
          f"/ {counts['imports']} import edges")
    print(f"markers                   " + ", ".join(
        f"{k}={v}" for k, v in sorted(r["markers_by_probe"].items())) or "none")
    print(f"unresolved imports        {r['unresolved']} (external/stdlib or dynamic)")
    print(f"elapsed                   {r['elapsed_ms']:.0f} ms")


def cmd_touch(args):
    repo = Path(args.repo).resolve()
    with _open(repo) as st:
        try:
            r = query.touch(st, repo, args.files)
        except query.UnknownFile as exc:
            sys.exit(f"{str(exc)!r} is neither on disk nor in the graph")

    print(f"reparsed                  {len(r['reparsed']) - r['failures']}")
    if r["evicted"]:
        print(f"evicted (gone)            {len(r['evicted'])}")
    if r["failures"]:
        print(f"parse failures            {r['failures']}")
    print(f"marked dirty (dependents) {len(r['dirtied'])}")
    counts = r["counts"]
    print(f"graph                     {counts['files']} files / {counts['functions']} functions")
    print(f"elapsed                   {r['elapsed_ms']:.1f} ms")


def cmd_status(args):
    probe = probes.get(args.probe).name
    with _open(args.repo) as st:
        r = query.status(st, probe)
        counts = r["counts"]
        print(f"files                     {counts['files']}")
        print(f"functions                 {counts['functions']}")
        print(f"import edges              {counts['imports']}")
        print(f"dirty files               {counts['dirty']}")
        print()
        for name, n in sorted(r["markers_by_probe"].items()):
            flag = " <- active" if name == probe else ""
            print(f"  {name:9s} {n:6d} markers{flag}")
        print()
        print(f"probe                     {probe}  ({probes.get(probe).description})")
        print(f"marked functions          {r['marked_functions']}")
        print(f"reachable functions       {r['reachable']}  "
              f"({r['reachable_pct']:.1f}% of all)")
        if args.top:
            print(f"\nsample markers ({args.top}):")
            for p, q, kind, detail, ln in st.sample_markers(probe, args.top):
                label = q or "<module>"
                print(f"  {p}:{ln}  {label}  [{kind}]  {detail}")


def cmd_impact(args):
    probe = probes.get(args.probe).name
    with _open(args.repo) as st:
        try:
            r = query.impact(st, args.file, probe)
        except query.UnknownFile:
            sys.exit(f"{args.file!r} is not in the graph")

    counts = r["counts"]
    total_fn = max(counts["functions"], 1)
    n_fns = len(r["recheck_fns"])
    print(f"probe                     {probe}")
    print(f"changed file              {r['changed_file']}")
    print(f"dependent files           {len(r['impacted'])}  "
          f"({100*len(r['impacted'])/max(counts['files'],1):.1f}% of repo)")
    print(f"files needing recheck     {len(r['recheck_files'])}")
    print(f"functions needing recheck {n_fns}")
    print()
    print(f"stateless scan            {counts['files']} files / {counts['functions']} functions")
    print(f"stateful recheck          {len(r['recheck_files'])} files / {n_fns} functions")
    print(f"work avoided              {100*(1-n_fns/total_fn):.1f}%")
    print(f"query time                {r['elapsed_ms']:.0f} ms")


def cmd_fanout(args):
    with _open(args.repo) as st:
        r = query.fanout_stats(st)

    counts = r["counts"]
    pct = r["percentiles"]
    core = r["largest_component"]
    values = sorted(r["fanout"].values())

    print(f"files                     {counts['files']}")
    print(f"import cycles (SCCs)      {r['components']}; largest {core} files "
          f"({100*core/max(counts['files'],1):.0f}% of repo)")
    print()
    print("blast radius (files implicated by a change to one file)")
    print(f"  min                     {r['min']}")
    print(f"  median                  {pct[50]}")
    print(f"  p75                     {pct[75]}")
    print(f"  p90                     {pct[90]}")
    print(f"  p99                     {pct[99]}")
    print(f"  max                     {r['max']}")
    print(f"  mean                    {r['mean']:.1f}" if values else "")
    print()
    for bound, share in r["within"].items():
        print(f"  <= {bound:4d} files          {share*100:5.1f}%")
    print()
    print(f"computed in               {r['elapsed_ms']:.0f} ms")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aic", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="build or update the graph")
    p.add_argument("repo")
    p.add_argument("--rehash", action="store_true",
                   help="hash every file, ignoring mtime (use if timestamps lie)")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("touch", help="invalidate specific files (no repo walk)")
    p.add_argument("repo")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_touch)

    p = sub.add_parser("status", help="what the graph holds")
    p.add_argument("repo")
    p.add_argument("--probe", default=probes.DEFAULT)
    p.add_argument("--top", type=int, default=0)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("impact", help="what a change implicates")
    p.add_argument("repo")
    p.add_argument("file")
    p.add_argument("--probe", default=probes.DEFAULT)
    p.set_defaults(fn=cmd_impact)

    p = sub.add_parser("fanout", help="blast-radius distribution across the repo")
    p.add_argument("repo")
    p.set_defaults(fn=cmd_fanout)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
