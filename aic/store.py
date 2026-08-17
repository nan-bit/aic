"""SQLite-backed incremental graph store.

Holds four things: file hashes (so we know what changed), the import graph (so
we know what a change reaches), probe markers (so we know what is worth
rechecking), and one baseline (so we know what "changed" is measured against).
`status` is CLEAN or DIRTY and is actually queried -- dirty propagation is the
engine, not a decoration.

The baseline is the odd one out, and the reason `_check_version` has an
exception in it: everything else here is derived from the tree and can be
rebuilt from it, and the baseline cannot.
"""

import sqlite3
from pathlib import Path

# Bump when the schema changes shape. A mismatch drops and rebuilds rather than
# migrating -- the graph is a derived artifact, always cheaper to regenerate
# than to migrate correctly. `baseline` is exempt; see _check_version.
SCHEMA_VERSION = 3

# A second writer gets SQLITE_BUSY immediately at the default of 0, and every
# MCP tool call writes (refresh commits once, at the end). `refresh` holds the
# write lock for essentially the whole of a cold index -- 2.6s on Django -- so
# this has to cover one of those. Much longer and a stuck lock reads as a hang
# rather than an error, which is worse.
BUSY_TIMEOUT_MS = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path     TEXT PRIMARY KEY,
    hash     TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'CLEAN',   -- CLEAN | DIRTY
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    size     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS functions (
    path     TEXT NOT NULL,
    qualname TEXT NOT NULL,
    line     INTEGER NOT NULL,
    PRIMARY KEY (path, qualname)
);
CREATE TABLE IF NOT EXISTS markers (
    path     TEXT NOT NULL,
    qualname TEXT NOT NULL,
    probe    TEXT NOT NULL,
    kind     TEXT NOT NULL,
    detail   TEXT NOT NULL,
    line     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    path     TEXT NOT NULL,
    qualname TEXT NOT NULL,
    callee   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    PRIMARY KEY (src, dst)
);
CREATE INDEX IF NOT EXISTS idx_imports_dst   ON imports(dst);
CREATE INDEX IF NOT EXISTS idx_calls_callee  ON calls(callee);
CREATE INDEX IF NOT EXISTS idx_fn_path       ON functions(path);
CREATE INDEX IF NOT EXISTS idx_markers_probe ON markers(probe);
CREATE INDEX IF NOT EXISTS idx_markers_path  ON markers(path);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS baseline (
    path TEXT PRIMARY KEY,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS baseline_meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Dropped on a schema-version mismatch. The two `baseline` tables are
# deliberately absent, and `baseline_meta` exists separately from `meta` for
# exactly that reason: provenance dropped along with the graph could not say
# whether what survived was still trustworthy. See _check_version.
DERIVED_TABLES = ("files", "functions", "markers", "calls", "imports", "meta")


class Store:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        # First, before anything else touches the file. Setting journal_mode
        # takes a lock and `CREATE TABLE IF NOT EXISTS` is a write, so opening a
        # Store already contends -- a timeout installed further down would not
        # cover the two statements most likely to meet a concurrent indexer.
        self.conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._check_version()
        self.conn.commit()

    def _check_version(self):
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row and int(row[0]) == SCHEMA_VERSION:
            return
        if row:
            # `baseline` survives. Every other table is derived from the tree
            # and is cheaper to rebuild than to migrate, which is what makes
            # drop-and-rebuild right for them; a baseline is the one thing here
            # that cannot be recomputed from anything, only re-recorded, and
            # re-recording silently answers a different question than the one
            # that was asked. It carries the version it was written under
            # instead, so a stale one reads as absent rather than as wrong --
            # see baseline_info.
            for table in DERIVED_TABLES:
                self.conn.execute(f"DROP TABLE IF EXISTS {table}")
            self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- file state ----------------------------------------------------

    def hashes(self):
        return dict(self.conn.execute("SELECT path, hash FROM files"))

    def file_state(self):
        """path -> (hash, mtime_ns, size). Lets the indexer skip reading files
        whose stat is unchanged, which is most of them."""
        return {
            path: (h, mt, sz)
            for path, h, mt, sz in self.conn.execute(
                "SELECT path, hash, mtime_ns, size FROM files"
            )
        }

    def touch_stat(self, path, mtime_ns, size):
        """Record a new stat for a file whose content hash was unchanged."""
        self.conn.execute(
            "UPDATE files SET mtime_ns=?, size=? WHERE path=?", (mtime_ns, size, path)
        )

    def evict(self, paths):
        """Drop a file and everything derived from it. v1 never did this, so
        deleted files lived on in the index forever."""
        if not paths:
            return
        rows = [(p,) for p in paths]
        c = self.conn
        c.executemany("DELETE FROM files     WHERE path = ?", rows)
        c.executemany("DELETE FROM functions WHERE path = ?", rows)
        c.executemany("DELETE FROM markers   WHERE path = ?", rows)
        c.executemany("DELETE FROM calls     WHERE path = ?", rows)
        c.executemany("DELETE FROM imports   WHERE src  = ?", rows)

    def put_file(self, path, file_hash, functions, calls, markers_by_probe,
                 status="CLEAN", mtime_ns=0, size=0):
        c = self.conn
        c.execute(
            "INSERT INTO files(path, hash, status, mtime_ns, size) VALUES(?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET hash=excluded.hash, status=excluded.status, "
            "mtime_ns=excluded.mtime_ns, size=excluded.size",
            (path, file_hash, status, mtime_ns, size),
        )
        c.executemany(
            "INSERT OR REPLACE INTO functions(path, qualname, line) VALUES(?,?,?)",
            [(path, f.qualname, f.line) for f in functions],
        )
        c.executemany(
            "INSERT INTO calls(path, qualname, callee) VALUES(?,?,?)",
            [(path, call.caller, call.simple) for call in calls],
        )
        rows = [
            (path, m.qualname, probe, m.kind, m.detail, m.line)
            for probe, markers in markers_by_probe.items()
            for m in markers
        ]
        c.executemany(
            "INSERT INTO markers(path, qualname, probe, kind, detail, line) "
            "VALUES(?,?,?,?,?,?)", rows,
        )

    def put_imports(self, src, dsts):
        self.conn.execute("DELETE FROM imports WHERE src = ?", (src,))
        self.conn.executemany(
            "INSERT OR IGNORE INTO imports(src, dst) VALUES(?,?)", [(src, d) for d in dsts]
        )

    def commit(self):
        self.conn.commit()

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value))
        )

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    # --- baseline ------------------------------------------------------
    #
    # What "changed" is measured against. Written only when asked; never by a
    # refresh. That is the whole design: refresh writes `files`, so no sequence
    # of refreshes can move the thing the change set is computed from, which is
    # the failure that made a resident server report a hub-file edit as
    # reaching nothing.

    def set_baseline(self, hashes, recorded_at):
        self.conn.execute("DELETE FROM baseline")
        self.conn.executemany(
            "INSERT INTO baseline(path, hash) VALUES(?,?)", sorted(hashes.items())
        )
        for key, value in (
            ("recorded_at", recorded_at),
            ("schema_version", SCHEMA_VERSION),
        ):
            self.conn.execute(
                "INSERT INTO baseline_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value))
            )

    def baseline(self):
        """path -> hash as of the baseline. Empty when there isn't a usable one."""
        if not self.baseline_info():
            return {}
        return dict(self.conn.execute("SELECT path, hash FROM baseline"))

    def baseline_info(self):
        """(recorded_at, n_files), or None if there is no baseline to trust.

        A baseline written under an older schema is reported absent rather than
        used. It survives the drop-and-rebuild so that upgrading does not
        silently discard it, but if the graph it described was rebuilt under
        different rules its hashes may no longer mean the same thing -- and a
        wrong baseline is worse than none, because it answers confidently.
        """
        rows = dict(self.conn.execute("SELECT key, value FROM baseline_meta"))
        if not rows or rows.get("schema_version") != str(SCHEMA_VERSION):
            return None
        n = self.conn.execute("SELECT count(*) FROM baseline").fetchone()[0]
        return rows["recorded_at"], n

    # --- queries -------------------------------------------------------

    def all_paths(self):
        return [r[0] for r in self.conn.execute("SELECT path FROM files")]

    def counts(self):
        q = self.conn.execute
        return {
            "files": q("SELECT COUNT(*) FROM files").fetchone()[0],
            "functions": q("SELECT COUNT(*) FROM functions").fetchone()[0],
            "imports": q("SELECT COUNT(*) FROM imports").fetchone()[0],
            "dirty": q("SELECT COUNT(*) FROM files WHERE status='DIRTY'").fetchone()[0],
        }

    def marker_counts(self):
        return dict(self.conn.execute(
            "SELECT probe, COUNT(*) FROM markers GROUP BY probe"
        ))

    def import_edges(self):
        edges = {}
        for src, dst in self.conn.execute("SELECT src, dst FROM imports"):
            edges.setdefault(src, set()).add(dst)
        return edges

    def functions_by_name(self):
        """simple name -> [(path, qualname)] for name-based call resolution."""
        out = {}
        for path, qual in self.conn.execute("SELECT path, qualname FROM functions"):
            out.setdefault(qual.split(".")[-1], []).append((path, qual))
        return out

    def call_edges(self):
        out = {}
        for path, qual, callee in self.conn.execute(
            "SELECT path, qualname, callee FROM calls"
        ):
            out.setdefault((path, qual), set()).add(callee)
        return out

    def marked_functions(self, probe):
        """(path, qualname) pairs carrying a marker from this probe.

        Module-level markers (qualname '') attach to the file, not a function,
        so they are excluded from call-graph reachability and handled by file.
        """
        return {
            (p, q) for p, q in self.conn.execute(
                "SELECT path, qualname FROM markers WHERE probe=? AND qualname<>''", (probe,)
            )
        }

    def marked_files(self, probe):
        return {
            r[0] for r in self.conn.execute(
                "SELECT DISTINCT path FROM markers WHERE probe=?", (probe,)
            )
        }

    def sample_markers(self, probe, limit):
        return list(self.conn.execute(
            "SELECT path, qualname, kind, detail, line FROM markers "
            "WHERE probe=? ORDER BY path LIMIT ?", (probe, limit),
        ))

    def all_markers(self, probe):
        """Every marker row for a probe, unordered.

        Callers filter and rank in Python (see query.markers_for). Pushing the
        path filter into SQL would mean chunking around SQLite's parameter
        limit for a set that is thousands of rows at most.
        """
        return list(self.conn.execute(
            "SELECT path, qualname, kind, detail, line FROM markers WHERE probe=?",
            (probe,),
        ))

    # --- dirty propagation ---------------------------------------------

    def mark_clean_all(self):
        self.conn.execute("UPDATE files SET status='CLEAN'")

    def mark_dirty(self, paths):
        if not paths:
            return
        self.conn.executemany(
            "UPDATE files SET status='DIRTY' WHERE path = ?", [(p,) for p in paths]
        )

    def dirty(self):
        return {r[0] for r in self.conn.execute(
            "SELECT path FROM files WHERE status='DIRTY'"
        )}
