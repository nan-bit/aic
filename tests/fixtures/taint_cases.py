"""Ground-truth taint cases.

Each function's docstring first line is a verdict: TAINTED or SAFE, then the
sink kind. The test harness (test_taint.py) parses these and checks the security
probe's dataflow pass agrees.

The SAFE cases matter more than the TAINTED ones -- a taint engine that flags
everything is trivially "correct" on positives and useless in practice. These
are the cases that separate a real dataflow analysis from grep.
"""

import os
import pickle
import shlex
import sqlite3
import subprocess


# --- TAINTED: attacker data reaches a sink -----------------------------

def sql_direct_concat(cursor, uid):
    """TAINTED sql -- parameter concatenated straight into a query."""
    cursor.execute("SELECT * FROM users WHERE id = " + uid)


def sql_via_fstring(cursor, name):
    """TAINTED sql -- f-string interpolation of a parameter."""
    cursor.execute(f"SELECT * FROM t WHERE name = '{name}'")


def sql_via_percent(cursor, uid):
    """TAINTED sql -- percent formatting."""
    cursor.execute("SELECT * FROM t WHERE id = %s" % uid)


def sql_through_local(cursor, uid):
    """TAINTED sql -- taint flows through an intermediate assignment."""
    q = "SELECT * FROM t WHERE id = " + uid
    cursor.execute(q)


def sql_through_two_hops(cursor, uid):
    """TAINTED sql -- taint survives two assignments."""
    part = uid
    q = "SELECT * FROM t WHERE id = " + part
    cursor.execute(q)


def cmd_os_system(path):
    """TAINTED command-exec -- parameter into os.system."""
    os.system("ls " + path)


def cmd_subprocess(user_arg):
    """TAINTED command-exec -- parameter into subprocess."""
    subprocess.run("cat " + user_arg)


def code_eval(expr):
    """TAINTED code-exec -- parameter into eval."""
    return eval(expr)


def deser_pickle(blob):
    """TAINTED deserialization -- parameter into pickle.loads."""
    return pickle.loads(blob)


def sql_in_branch(cursor, uid, flag):
    """TAINTED sql -- reachable on one branch."""
    if flag:
        cursor.execute("SELECT * FROM t WHERE id = " + uid)


def sql_after_loop(cursor, rows):
    """TAINTED sql -- taint picked up inside a loop, used after."""
    q = "SELECT 1"
    for r in rows:
        q = "SELECT * FROM t WHERE id = " + r
    cursor.execute(q)


# --- SAFE: no attacker data reaches the sink ---------------------------

def sql_constant(cursor):
    """SAFE sql -- fully static query."""
    cursor.execute("SELECT * FROM users WHERE active = 1")


def sql_parameterized(cursor, uid):
    """SAFE sql -- parameter goes in the params tuple, not the query text."""
    cursor.execute("SELECT * FROM users WHERE id = ?", (uid,))


def sql_reassigned_clean(cursor, uid):
    """SAFE sql -- tainted local is overwritten with a constant before use."""
    q = "SELECT * FROM t WHERE id = " + uid
    q = "SELECT * FROM t WHERE id = 1"
    cursor.execute(q)


def sql_sanitized(cursor, name):
    """SAFE sql -- parameter passed through a sanitizer first."""
    safe = shlex.quote(name)
    cursor.execute("SELECT * FROM t WHERE name = " + safe)


def cmd_constant():
    """SAFE command-exec -- static command."""
    os.system("uptime")


def code_eval_constant():
    """SAFE code-exec -- eval of a literal."""
    return eval("1 + 1")


def deser_constant():
    """SAFE deserialization -- loads a local literal, no parameter."""
    return pickle.loads(b"\\x80\\x04K\\x01.")


def sql_unrelated_param(cursor, uid, label):
    """SAFE sql -- the tainted parameter never reaches the query."""
    _ = uid
    cursor.execute("SELECT * FROM t WHERE label = 'fixed'")
    return label
