"""aic -- incremental impact analysis.

    aic index  <repo>                  build or update the graph
    aic status <repo> [--probe P]      what the graph holds
    aic impact <repo> <file> [--probe P]   what a change to <file> implicates
    aic touch  <repo> <file>...        invalidate named files, no repo walk
    aic fanout <repo>                  distribution of blast radius across the repo

The claim `index` exists to demonstrate: the second run is nearly free. A
stateless analyzer redoes the whole repo on every invocation, so an agent making
forty edits pays for forty full scans.
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

from . import analyze, probes
from .store import Store


def db_for(repo):
    return Path(repo) / ".aic" / "graph.db"


def _open(repo):
    p = db_for(repo)
    if not p.exists():
        sys.exit("no graph yet -- run `index` first")
    return Store(p)


def _reverse_imports(st):
    return analyze.reverse(st.import_edges())


# --- commands ----------------------------------------------------------

def _reindex(st, root, targets, all_paths, pkg_root, stats):
    """Parse `targets`, store them, then propagate DIRTY to dependents.

    Shared by `index` and `touch`. `all_paths` is every path the module map
    should know about -- `touch` takes it from the graph rather than walking.
    """
    by_module = {analyze.module_key(rel): rel for rel in all_paths}
    active = list(probes.REGISTRY.values())
    failures = unresolved = 0

    for rel in targets:
        src = analyze.read_file(root, rel)
        if src is None:
            failures += 1
            continue
        try:
            tree, facts = analyze.extract(rel, src)
        except SyntaxError:
            failures += 1
            continue
        markers = {p.name: list(p.inspect(rel, tree, facts)) for p in active}
        mtime_ns, size = stats.get(rel, (0, 0))
        st.put_file(rel, analyze.file_hash(src), facts.functions, facts.calls,
                    markers, mtime_ns=mtime_ns, size=size)
        dsts, misses = analyze.resolve_imports(rel, facts.imports, by_module, pkg_root)
        unresolved += misses
        st.put_imports(rel, dsts)

    return failures, unresolved


def _propagate_dirty(st, seeds, reparsed):
    st.mark_clean_all()
    if not seeds:
        return set()
    affected = analyze.propagate(set(seeds), _reverse_imports(st)) - set(reparsed)
    st.mark_dirty(affected)
    return affected


def cmd_index(args):
    repo = Path(args.repo).resolve()
    pkg_root = repo.name
    t0 = time.time()

    stats = analyze.scan_repo(repo)

    with Store(db_for(repo)) as st:
        known = st.file_state()

        # Cheap pass: only files whose stat moved are worth reading.
        suspect = [
            rel for rel, sig in stats.items()
            if getattr(args, "rehash", False) or rel not in known or known[rel][1:] != sig
        ]
        deleted = [r for r in known if r not in stats]

        # Expensive pass: hash the suspects; a moved mtime does not imply
        # changed content (git checkout, touch, format-on-save no-ops).
        changed, unchanged_stat_only = [], []
        for rel in suspect:
            src = analyze.read_file(repo, rel)
            if src is None:
                continue
            if rel in known and analyze.file_hash(src) == known[rel][0]:
                unchanged_stat_only.append(rel)
            else:
                changed.append(rel)

        st.evict(deleted + [r for r in changed if r in known])
        for rel in unchanged_stat_only:
            st.touch_stat(rel, *stats[rel])

        failures, unresolved = _reindex(st, repo, changed, stats, pkg_root, stats)
        affected = _propagate_dirty(st, set(changed) | set(deleted), changed)

        st.set_meta("pkg_root", pkg_root)
        st.commit()
        counts, markers_by_probe = st.counts(), st.marker_counts()

    elapsed = (time.time() - t0) * 1000
    print(f"mode                      {'cold' if not known else 'incremental'}")
    print(f"files on disk             {len(stats)}")
    print(f"  stat-changed            {len(suspect)}")
    print(f"  reparsed                {len(changed)}")
    print(f"  skipped (unchanged)     {len(stats) - len(changed)}")
    print(f"  evicted (deleted)       {len(deleted)}")
    if failures:
        print(f"  parse failures          {failures}")
    print(f"marked dirty (dependents) {len(affected)}")
    print(f"graph                     {counts['files']} files / {counts['functions']} functions "
          f"/ {counts['imports']} import edges")
    print(f"markers                   " + ", ".join(
        f"{k}={v}" for k, v in sorted(markers_by_probe.items())) or "none")
    print(f"unresolved imports        {unresolved} (external/stdlib or dynamic)")
    print(f"elapsed                   {elapsed:.0f} ms")


def cmd_touch(args):
    """Invalidate named files without walking the repo.

    This is the agent-facing path: an editor hook or coding agent already knows
    what it changed, so detection costs nothing. The module map comes from the
    graph, not the filesystem.
    """
    repo = Path(args.repo).resolve()
    t0 = time.time()

    with _open(repo) as st:
        pkg_root = st.get_meta("pkg_root", repo.name)
        known = set(st.all_paths())

        targets, missing = [], []
        for raw in args.files:
            p = Path(raw)
            rel = str(p.resolve().relative_to(repo)) if p.is_absolute() else str(p)
            if (repo / rel).exists():
                targets.append(rel)
            elif rel in known:
                missing.append(rel)
            else:
                sys.exit(f"{raw!r} is neither on disk nor in the graph")

        stats = {}
        for rel in targets:
            try:
                s = (repo / rel).stat()
                stats[rel] = (s.st_mtime_ns, s.st_size)
            except OSError:
                stats[rel] = (0, 0)

        st.evict(missing + [r for r in targets if r in known])
        all_paths = (known | set(targets)) - set(missing)
        failures, _ = _reindex(st, repo, targets, all_paths, pkg_root, stats)
        affected = _propagate_dirty(st, set(targets) | set(missing), targets)
        st.commit()
        counts = st.counts()

    elapsed = (time.time() - t0) * 1000
    print(f"reparsed                  {len(targets) - failures}")
    if missing:
        print(f"evicted (gone)            {len(missing)}")
    if failures:
        print(f"parse failures            {failures}")
    print(f"marked dirty (dependents) {len(affected)}")
    print(f"graph                     {counts['files']} files / {counts['functions']} functions")
    print(f"elapsed                   {elapsed:.1f} ms")


def cmd_status(args):
    probe = probes.get(args.probe).name
    with _open(args.repo) as st:
        counts = st.counts()
        reachable = analyze.marker_reachable(
            st.marked_functions(probe), st.call_edges(), st.functions_by_name(),
            st.import_edges(),
        )
        print(f"files                     {counts['files']}")
        print(f"functions                 {counts['functions']}")
        print(f"import edges              {counts['imports']}")
        print(f"dirty files               {counts['dirty']}")
        print()
        for name, n in sorted(st.marker_counts().items()):
            flag = " <- active" if name == probe else ""
            print(f"  {name:9s} {n:6d} markers{flag}")
        print()
        print(f"probe                     {probe}  ({probes.get(probe).description})")
        print(f"marked functions          {len(st.marked_functions(probe))}")
        print(f"reachable functions       {len(reachable)}  "
              f"({100*len(reachable)/max(counts['functions'],1):.1f}% of all)")
        if args.top:
            print(f"\nsample markers ({args.top}):")
            for p, q, kind, detail, ln in st.sample_markers(probe, args.top):
                label = q or "<module>"
                print(f"  {p}:{ln}  {label}  [{kind}]  {detail}")


def cmd_impact(args):
    probe = probes.get(args.probe).name
    with _open(args.repo) as st:
        paths = set(st.all_paths())
        if args.file not in paths:
            sys.exit(f"{args.file!r} is not in the graph")

        t0 = time.time()
        impacted = analyze.propagate({args.file}, _reverse_imports(st)) & paths
        reachable = analyze.marker_reachable(
            st.marked_functions(probe), st.call_edges(), st.functions_by_name(),
            st.import_edges(),
        )
        recheck_fns = {n for n in reachable if n[0] in impacted}
        recheck_files = {p for p, _ in recheck_fns} | (st.marked_files(probe) & impacted)
        counts = st.counts()
        elapsed = (time.time() - t0) * 1000

    total_fn = max(counts["functions"], 1)
    print(f"probe                     {probe}")
    print(f"changed file              {args.file}")
    print(f"dependent files           {len(impacted)}  "
          f"({100*len(impacted)/max(counts['files'],1):.1f}% of repo)")
    print(f"files needing recheck     {len(recheck_files)}")
    print(f"functions needing recheck {len(recheck_fns)}")
    print()
    print(f"stateless scan            {counts['files']} files / {counts['functions']} functions")
    print(f"stateful recheck          {len(recheck_files)} files / {len(recheck_fns)} functions")
    print(f"work avoided              {100*(1-len(recheck_fns)/total_fn):.1f}%")
    print(f"query time                {elapsed:.0f} ms")


def cmd_fanout(args):
    """Blast radius for every file, not two hand-picked ones.

    This is the number that generalizes: it says how often incremental analysis
    actually pays off in a given codebase, and where it stops paying.
    """
    with _open(args.repo) as st:
        paths = st.all_paths()
        edges = st.import_edges()
        t0 = time.time()
        fan = analyze.fanout(paths, edges)
        comps = analyze.strongly_connected(paths, edges)
        elapsed = (time.time() - t0) * 1000
        counts = st.counts()

    values = sorted(fan.values())
    pct = analyze.percentiles(values)
    n = len(values) or 1
    core = len(comps[0]) if comps else 0

    print(f"files                     {counts['files']}")
    print(f"import cycles (SCCs)      {len(comps)}; largest {core} files "
          f"({100*core/max(counts['files'],1):.0f}% of repo)")
    print()
    print("blast radius (files implicated by a change to one file)")
    print(f"  min                     {values[0] if values else 0}")
    print(f"  median                  {pct[50]}")
    print(f"  p75                     {pct[75]}")
    print(f"  p90                     {pct[90]}")
    print(f"  p99                     {pct[99]}")
    print(f"  max                     {values[-1] if values else 0}")
    print(f"  mean                    {statistics.mean(values):.1f}" if values else "")
    print()
    for bound in (1, 5, 10, 50, 100):
        share = sum(1 for v in values if v <= bound) / n
        print(f"  <= {bound:4d} files          {share*100:5.1f}%")
    print()
    print(f"computed in               {elapsed:.0f} ms")


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
