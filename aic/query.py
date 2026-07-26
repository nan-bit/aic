"""Pure query layer -- computes, never prints.

`cli.py` renders these results for a terminal; `aic/mcp.py` serves them to an
agent. Nothing in this module writes to stdout, and that is not a style
preference: under the MCP stdio transport stdout carries JSON-RPC, so a stray
`print` corrupts the protocol.

The split also keeps the protocol adapter disposable. MCP ships a breaking
revision on 2026-07-28 (stateless core, no initialize handshake) and the Python
SDK's v2 rewrite lands with it. With the analysis logic here, migrating means
rewriting one file that contains none of it.
"""

import time
from pathlib import Path

from . import analyze, probes
from .store import Store

# Ranking tiers for markers. Dataflow-confirmed findings outrank heuristic
# ones, because the heuristic pass deliberately over-reports (see
# probes/security.py) and the dataflow pass deliberately does not.
TAINTED_PREFIX = "tainted-"
SINK_KINDS = frozenset({
    "command-exec", "code-exec", "deserialization", "sql", "hardcoded-secret",
})


class GraphMissing(Exception):
    """No graph has been built for this repo yet."""


class UnknownFile(Exception):
    """Path is neither on disk nor in the graph."""


def db_for(repo):
    return Path(repo) / ".aic" / "graph.db"


def open_store(repo):
    path = db_for(repo)
    if not path.exists():
        raise GraphMissing(str(path))
    return Store(path)


# --- shared internals --------------------------------------------------

def _reindex(st, root, targets, all_paths, pkg_root, stats):
    """Parse `targets`, store them, resolve their imports.

    Shared by `refresh` and `touch`. `all_paths` is every path the module map
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
    rev = analyze.reverse(st.import_edges())
    affected = analyze.propagate(set(seeds), rev) - set(reparsed)
    st.mark_dirty(affected)
    return affected


# --- write paths -------------------------------------------------------

def refresh(st, root, pkg_root=None, rehash=False):
    """Bring the graph in line with the filesystem. Returns a dict of counts.

    The warm path is a `scandir` stat-diff, not a content pass -- discovering
    that nothing changed is most of the work on a large repo, and `stat` is
    roughly an order of magnitude cheaper than read+hash for that job. mtime is
    a hint, never truth: a moved timestamp still forces a hash.
    """
    root = Path(root)
    pkg_root = pkg_root or root.name
    t0 = time.time()

    stats = analyze.scan_repo(root)
    known = st.file_state()

    suspect = [
        rel for rel, sig in stats.items()
        if rehash or rel not in known or known[rel][1:] != sig
    ]
    deleted = [r for r in known if r not in stats]

    changed, unchanged_stat_only = [], []
    for rel in suspect:
        src = analyze.read_file(root, rel)
        if src is None:
            continue
        if rel in known and analyze.file_hash(src) == known[rel][0]:
            unchanged_stat_only.append(rel)
        else:
            changed.append(rel)

    st.evict(deleted + [r for r in changed if r in known])
    for rel in unchanged_stat_only:
        st.touch_stat(rel, *stats[rel])

    failures, unresolved = _reindex(st, root, changed, stats, pkg_root, stats)
    affected = _propagate_dirty(st, set(changed) | set(deleted), changed)

    st.set_meta("pkg_root", pkg_root)
    st.commit()

    return {
        "mode": "cold" if not known else "incremental",
        "files_on_disk": len(stats),
        "stat_changed": len(suspect),
        "reparsed": sorted(changed),
        "skipped": len(stats) - len(changed),
        "evicted": sorted(deleted),
        "failures": failures,
        "dirtied": sorted(affected),
        "unresolved": unresolved,
        "counts": st.counts(),
        "markers_by_probe": st.marker_counts(),
        "elapsed_ms": (time.time() - t0) * 1000,
    }


def touch(st, root, files, pkg_root=None):
    """Invalidate named files without walking the repo.

    The agent-facing write path: whoever edited the file already knows which
    file it was, so detection costs nothing. The module map comes from the
    graph rather than the filesystem.
    """
    root = Path(root)
    pkg_root = pkg_root or st.get_meta("pkg_root", root.name)
    t0 = time.time()

    known = set(st.all_paths())
    targets, missing = [], []
    for raw in files:
        p = Path(raw)
        rel = str(p.resolve().relative_to(root)) if p.is_absolute() else str(p)
        if (root / rel).exists():
            targets.append(rel)
        elif rel in known:
            missing.append(rel)
        else:
            raise UnknownFile(str(raw))

    stats = {}
    for rel in targets:
        try:
            s = (root / rel).stat()
            stats[rel] = (s.st_mtime_ns, s.st_size)
        except OSError:
            stats[rel] = (0, 0)

    st.evict(missing + [r for r in targets if r in known])
    all_paths = (known | set(targets)) - set(missing)
    failures, _ = _reindex(st, root, targets, all_paths, pkg_root, stats)
    affected = _propagate_dirty(st, set(targets) | set(missing), targets)
    st.commit()

    return {
        "reparsed": sorted(targets),
        "evicted": sorted(missing),
        "failures": failures,
        "dirtied": sorted(affected),
        "counts": st.counts(),
        "elapsed_ms": (time.time() - t0) * 1000,
    }


# --- read paths --------------------------------------------------------

def _reachable(st, probe):
    return analyze.marker_reachable(
        st.marked_functions(probe), st.call_edges(), st.functions_by_name(),
        st.import_edges(),
    )


def impact(st, path, probe):
    """What a change to `path` implicates, under `probe`.

    `recheck_fns` is the number that matters: the intersection of what the
    change reached with what the probe considers interesting. The dependent
    file count is context, not a work list -- on Django a change to
    db/models/query.py reaches 570 files, which is useless as a list and
    informative as a count.
    """
    paths = set(st.all_paths())
    if path not in paths:
        raise UnknownFile(path)

    t0 = time.time()
    impacted = analyze.propagate({path}, analyze.reverse(st.import_edges())) & paths
    reachable = _reachable(st, probe)
    recheck_fns = {n for n in reachable if n[0] in impacted}
    recheck_files = {p for p, _ in recheck_fns} | (st.marked_files(probe) & impacted)
    counts = st.counts()

    return {
        "probe": probe,
        "changed_file": path,
        "impacted": sorted(impacted),
        "recheck_fns": sorted(recheck_fns),
        "recheck_files": sorted(recheck_files),
        "counts": counts,
        "elapsed_ms": (time.time() - t0) * 1000,
    }


def review(st, probe, extra_files=()):
    """Everything currently invalidated, intersected with what `probe` marks.

    After a refresh the reparsed files are CLEAN (they are up to date) and only
    their dependents carry DIRTY. `extra_files` lets a caller add the files it
    just reparsed, which is what makes this answer "everything I have touched
    this session" rather than "everything downstream of it".
    """
    t0 = time.time()
    scope = st.dirty() | set(extra_files)
    reachable = _reachable(st, probe)
    recheck_fns = {n for n in reachable if n[0] in scope}
    recheck_files = {p for p, _ in recheck_fns} | (st.marked_files(probe) & scope)

    return {
        "probe": probe,
        "scope": sorted(scope),
        "recheck_fns": sorted(recheck_fns),
        "recheck_files": sorted(recheck_files),
        "counts": st.counts(),
        "elapsed_ms": (time.time() - t0) * 1000,
    }


def status(st, probe):
    counts = st.counts()
    reachable = _reachable(st, probe)
    return {
        "probe": probe,
        "counts": counts,
        "markers_by_probe": st.marker_counts(),
        "marked_functions": len(st.marked_functions(probe)),
        "reachable": len(reachable),
        "reachable_pct": 100 * len(reachable) / max(counts["functions"], 1),
    }


def fanout_stats(st):
    """Blast radius for every file, not two hand-picked ones.

    This is the number that generalizes: it says how often incremental analysis
    pays off in a given codebase, and where it stops paying.
    """
    paths = st.all_paths()
    edges = st.import_edges()
    t0 = time.time()
    fan = analyze.fanout(paths, edges)
    comps = analyze.strongly_connected(paths, edges)
    elapsed = (time.time() - t0) * 1000

    values = sorted(fan.values())
    counts = st.counts()
    n = len(values) or 1
    return {
        "counts": counts,
        "fanout": fan,
        "components": len(comps),
        "largest_component": len(comps[0]) if comps else 0,
        "min": values[0] if values else 0,
        "max": values[-1] if values else 0,
        "mean": sum(values) / n if values else 0.0,
        "percentiles": analyze.percentiles(values),
        "within": {b: sum(1 for v in values if v <= b) / n for b in (1, 5, 10, 50, 100)},
        "elapsed_ms": elapsed,
    }


# --- presentation support ----------------------------------------------

def markers_for(st, probe, fns, module_paths=()):
    """Marker rows (path, qualname, kind, detail, line) for specific functions.

    Filtering by function rather than by file matters: a file needing recheck
    usually also contains markers on functions the change did not reach, and
    reporting those would inflate the answer with work nobody asked for.
    Module-level markers (qualname '') attach to a file, not a function, so they
    are matched against `module_paths` instead.
    """
    wanted = set(fns)
    mods = set(module_paths)
    out = []
    for row in st.all_markers(probe):
        path, qual = row[0], row[1]
        if qual:
            if (path, qual) in wanted:
                out.append(row)
        elif path in mods:
            out.append(row)
    return out


def rank_markers(rows, fan=None):
    """Highest-signal first.

    Dataflow-confirmed (`tainted-*`) outranks the heuristic sink pass, which
    over-reports by design; named sink kinds outrank everything else. Within a
    tier, files with the larger blast radius come first -- a finding in a file
    570 others depend on is worth more of a limited response budget than one in
    a leaf.
    """
    fan = fan or {}

    def key(row):
        path, _qual, kind, _detail, line = row
        if kind.startswith(TAINTED_PREFIX):
            tier = 0
        elif kind in SINK_KINDS:
            tier = 1
        else:
            tier = 2
        return (tier, -fan.get(path, 0), path, line)

    return sorted(rows, key=key)
