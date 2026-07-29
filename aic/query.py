"""Pure query layer -- computes, never prints.

`surfaces/cli.py` renders these results for a terminal; `surfaces/mcp.py` serves
them to an agent. Nothing in this module writes to stdout, and that is not a
style preference: under the MCP stdio transport stdout carries JSON-RPC, so a
stray `print` corrupts the protocol.

The split also kept the protocol adapter disposable, which was the point of it.
MCP's 2026-07-28 revision shipped a stateless core with no initialize handshake,
and the Python SDK's v2 rewrite landed with it; migrating meant rewriting one
file that contained no analysis logic, and nothing here had to move.

One thing moved the other way. The protocol going stateless left `review` with
nowhere to keep its baseline, so the baseline came down here -- which is where
it should have been anyway, since it is analysis state and not protocol state.
The CLI got a `review` command out of it, having previously been unable to ask
the one question that needed a session.
"""

import datetime as dt
import hashlib
import os
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


class GraphUnwritable(Exception):
    """The graph cannot be created where it was asked to go."""


class UnknownFile(Exception):
    """Path is neither on disk nor in the graph."""


def db_for(repo, db=None):
    """Where the graph for `repo` lives.

    Precedence: an explicit path, then AIC_DB_DIR, then `<repo>/.aic/graph.db`.

    The default keeps the graph beside the thing it describes, which is right
    for your own checkout and wrong for anything you do not own -- a read-only
    export, a CI checkout, a vendored tree, a store path. AIC_DB_DIR names a
    directory and derives one file per repository, keyed by the resolved path so
    two checkouts of the same project cannot collide.
    """
    if db:
        return Path(db)
    shared = os.environ.get("AIC_DB_DIR")
    if shared:
        root = Path(repo).resolve()
        digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
        return Path(shared).expanduser() / f"{root.name}-{digest}.db"
    return Path(repo) / ".aic" / "graph.db"


RELOCATE_HINT = (
    "point it somewhere writable with --db PATH, or set AIC_DB_DIR to a "
    "directory to keep graphs outside the tree entirely"
)


def create_store(repo, db=None):
    """Open the graph for writing, creating it if needed."""
    path = db_for(repo, db)
    try:
        return Store(path)
    except OSError as exc:
        raise GraphUnwritable(f"cannot write the graph to {path} ({exc.strerror or exc}); "
                              f"{RELOCATE_HINT}") from exc


def open_store(repo, db=None):
    path = db_for(repo, db)
    if not path.exists():
        raise GraphMissing(str(path))
    try:
        return Store(path)
    except OSError as exc:
        raise GraphUnwritable(f"cannot open the graph at {path} ({exc.strerror or exc}); "
                              f"{RELOCATE_HINT}") from exc


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


# --- baseline ----------------------------------------------------------
#
# `review` asks "what have I changed, and what did it reach?" -- which needs a
# point to measure from. That used to be a set in the MCP server's memory,
# holding whatever it had reparsed since the process started. Three things were
# wrong with that: it died with the process, so edits made before the server
# started were invisible; it could not be reached from the CLI, so `review` was
# the one question you could not ask without a protocol in the way; and it was
# accumulated rather than derived, so it could drift.
#
# Storing the baseline instead makes the change set a diff between two hash sets
# rather than a running tally. Nothing accumulates, so nothing can drift, and an
# edit that gets reverted correctly stops counting as a change.

def record_baseline(st, at=None):
    """Mark the current state of the graph as the point `review` measures from."""
    at = at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    st.set_baseline(st.hashes(), at)
    st.commit()
    return at


def changed_since(st):
    """(seeds, info) -- files differing from the baseline, and its provenance.

    `info` is None when there is no usable baseline, which is not the same as
    "nothing changed" and must not be reported as it. Callers say so.

    Deletions are included: a path in the baseline that the graph no longer has
    is a change, and its dependents are exactly what needs rechecking. `review`
    propagates before filtering to the graph so they survive.
    """
    info = st.baseline_info()
    if info is None:
        return set(), None
    before, now = st.baseline(), st.hashes()
    changed = {p for p, h in now.items() if before.get(p) != h}
    return changed | (set(before) - set(now)), info


# --- write paths -------------------------------------------------------

def refresh(st, root, pkg_root=None, rehash=False):
    """Bring the graph in line with the filesystem. Returns a dict of counts.

    The warm path is a `scandir` stat-diff, not a content pass -- discovering
    that nothing changed is most of the work on a large repo, and `stat` is
    roughly an order of magnitude cheaper than read+hash for that job. mtime is
    a hint, never truth: a moved timestamp still forces a hash.
    """
    root = Path(root)
    # "" is a meaningful pkg_root (a repo root strips nothing), so this cannot
    # be an `or` -- that would silently re-guess the directory name.
    if pkg_root is None:
        pkg_root = analyze.default_pkg_root(root)
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

    # Establish a baseline if there isn't one; never move one that exists. The
    # rule is "if absent", not "if this was a cold index", because a graph built
    # by an older aic has no baseline either and should get one rather than
    # answer `review` with nothing forever. Refresh moving an existing baseline
    # is the drift this design exists to rule out, so it doesn't.
    baselined = st.baseline_info() is None
    if baselined:
        record_baseline(st)

    return {
        "mode": "cold" if not known else "incremental",
        "baseline_established": baselined,
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
    if pkg_root is None:
        pkg_root = st.get_meta("pkg_root", analyze.default_pkg_root(root))
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


def review(st, probe, seeds=()):
    """Everything `seeds` reaches, intersected with what `probe` marks.

    Scope is recomputed from the seed files rather than read off the stored
    DIRTY flag, and that is deliberate. DIRTY means "dependents of the most
    recent change set" -- correct for a one-shot CLI invocation, wrong for a
    resident server, because `refresh` clears it on every call. A server that
    refreshed twice would see the second (no-op) refresh wipe the dependents of
    the first edit, and answer that a change reached nothing. Propagating from
    the caller's own seeds accumulates correctly across any number of edits and
    any number of refreshes.
    """
    t0 = time.time()
    seeds = set(seeds)
    # Propagate from every seed, then filter -- not the other way round. A
    # deleted file is a seed that is no longer in the graph, and dropping it
    # first would drop its dependents with it, which is precisely the files that
    # most need rechecking. `_propagate_dirty` has always seeded with
    # `changed | deleted` for the same reason.
    known = set(st.all_paths())
    scope = (
        analyze.propagate(seeds, analyze.reverse(st.import_edges())) & known
        if seeds else set()
    )
    reachable = _reachable(st, probe)
    recheck_fns = {n for n in reachable if n[0] in scope}
    recheck_files = {p for p, _ in recheck_fns} | (st.marked_files(probe) & scope)

    return {
        "probe": probe,
        # Reported separately so a caller can say how far the change travelled
        # without subtracting a seed count that may include files the graph
        # never had -- a deletion is a change with no node to point at.
        "seeds": sorted(seeds),
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
