"""SQLite-backed incremental graph store.

Holds three things: file hashes (so we know what changed), the import graph (so
we know what a change reaches), and probe markers (so we know what is worth
rechecking). `status` is CLEAN or DIRTY and is actually queried -- dirty
propagation is the engine, not a decoration.
"""

import sqlite3
from pathlib import Path

# Bump when the schema changes shape. A mismatch drops and rebuilds rather than
# migrating -- the graph is a derived artifact, always cheaper to regenerate
# than to migrate correctly.
SCHEMA_VERSION = 2

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
"""


class Store:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
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
            for table in ("files", "functions", "markers", "calls", "imports", "meta"):
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
