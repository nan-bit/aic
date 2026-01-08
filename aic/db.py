import sqlite3
import os
from pathlib import Path

DB_PATH = Path(".aic/graph.db")

def init_db():
    if not DB_PATH.parent.exists():
        DB_PATH.parent.mkdir(parents=True)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Nodes table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS nodes (
        path TEXT PRIMARY KEY,
        hash TEXT,
        skeleton TEXT,
        status TEXT
    )
    ''')
    
    # Edges table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS edges (
        source TEXT,
        target TEXT,
        PRIMARY KEY (source, target),
        FOREIGN KEY(source) REFERENCES nodes(path)
    )
    ''')
    
    conn.commit()
    conn.close()

def upsert_node(path, file_hash, skeleton, status="CLEAN"):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO nodes (path, hash, skeleton, status)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(path) DO UPDATE SET
        hash=excluded.hash,
        skeleton=excluded.skeleton,
        status=excluded.status
    ''', (path, file_hash, skeleton, status))
    conn.commit()
    conn.close()

def update_edges(source, dependencies):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Clear existing edges for this source
    cursor.execute('DELETE FROM edges WHERE source = ?', (source,))
    
    # Add new edges
    for target in dependencies:
        cursor.execute('INSERT OR IGNORE INTO edges (source, target) VALUES (?, ?)', (source, target))
    
    conn.commit()
    conn.close()

def get_node(path):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT skeleton, hash, status FROM nodes WHERE path = ?', (path,))
    row = cursor.fetchone()
    conn.close()
    return row

def get_dependencies(path):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT target FROM edges WHERE source = ?', (path,))
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]

def mark_dirty(path):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Find all source nodes that depend on this path (where target = path)
    # Recursively? Or just immediate? Plan says "Propagate Dirty" but maybe just immediate for now.
    # Plan: "Find all nodes where edges.target == current_file and mark them DIRTY"
    cursor.execute('''
        UPDATE nodes SET status = 'DIRTY'
        WHERE path IN (SELECT source FROM edges WHERE target = ?)
    ''', (path,))
    conn.commit()
    conn.close()
