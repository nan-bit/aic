"""Parsing, fact extraction, and graph algorithms.

This module is deliberately probe-agnostic. It answers "what is in this file"
and "what depends on what". It does not decide what is *interesting* -- that is
a probe's job (see aic/probes/).

Facts keep what a signature-only skeleton throws away: decorators, module-level
assignments, argument annotations, and a line number on everything.
"""

import ast
import hashlib
import os
import sys
from collections import deque

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", ".tox", ".mypy_cache",
    ".pytest_cache", "build", "dist", ".eggs", "site-packages", ".aic",
}


def file_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FunctionFact:
    __slots__ = ("qualname", "line", "public", "context", "decorators", "args", "returns")

    def __init__(self, qualname, line, public, context, decorators, args, returns):
        self.qualname = qualname
        self.line = line
        self.public = public          # no leading underscore anywhere in the path
        self.context = context        # "module" | "method" | "nested"
        self.decorators = decorators  # list[str]
        self.args = args              # list[str], annotations preserved
        self.returns = returns        # str | None


class CallFact:
    __slots__ = ("caller", "simple", "dotted", "line")

    def __init__(self, caller, simple, dotted, line):
        self.caller = caller
        self.simple = simple          # execute
        self.dotted = dotted          # cursor.execute
        self.line = line


class Facts:
    """Everything we know about one source file."""

    def __init__(self, path):
        self.path = path
        self.imports = []       # (module, level, [names])
        self.functions = []     # FunctionFact
        self.calls = []         # CallFact
        self.assignments = []   # (name, line, literal_value_or_None)

    @property
    def imported_roots(self):
        return {m.split(".")[0] for m, _, _ in self.imports if m}


class _Visitor(ast.NodeVisitor):
    def __init__(self, facts):
        self.f = facts
        self.stack = []        # enclosing def/class names
        self.kinds = []        # parallel: "class" | "func"

    def visit_Import(self, node):
        for a in node.names:
            self.f.imports.append((a.name, 0, []))
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        self.f.imports.append((node.module or "", node.level, [a.name for a in node.names]))
        self.generic_visit(node)

    def visit_Assign(self, node):
        # Module scope only -- where config and hardcoded credentials live.
        if not self.stack:
            literal = node.value.value if isinstance(node.value, ast.Constant) else None
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.f.assignments.append((t.id, node.lineno, literal))
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.stack.append(node.name)
        self.kinds.append("class")
        self.generic_visit(node)
        self.kinds.pop()
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self._fn(node)

    def visit_AsyncFunctionDef(self, node):
        self._fn(node)

    def _fn(self, node):
        if not self.kinds:
            context = "module"
        elif self.kinds[-1] == "class":
            context = "method"
        else:
            context = "nested"

        self.stack.append(node.name)
        self.kinds.append("func")
        qual = ".".join(self.stack)

        self.f.functions.append(FunctionFact(
            qualname=qual,
            line=node.lineno,
            public=not any(p.startswith("_") for p in self.stack),
            context=context,
            decorators=[_src(d) for d in node.decorator_list],
            args=_args(node.args),
            returns=_src(node.returns) if node.returns else None,
        ))

        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                simple, dotted = _call_name(sub.func)
                if simple:
                    self.f.calls.append(
                        CallFact(qual, simple, dotted, getattr(sub, "lineno", node.lineno))
                    )

        self.generic_visit(node)
        self.kinds.pop()
        self.stack.pop()


def _src(node):
    try:
        return ast.unparse(node)
    except Exception:
        return "<unparseable>"


def _args(a):
    out = []
    for group in (getattr(a, "posonlyargs", []), a.args, a.kwonlyargs):
        for arg in group:
            out.append(f"{arg.arg}: {_src(arg.annotation)}" if arg.annotation else arg.arg)
    if a.vararg:
        out.append("*" + a.vararg.arg)
    if a.kwarg:
        out.append("**" + a.kwarg.arg)
    return out


def _call_name(func):
    if isinstance(func, ast.Name):
        return func.id, func.id
    if isinstance(func, ast.Attribute):
        parts, cur = [func.attr], func.value
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return func.attr, ".".join(reversed(parts))
    return None, None


def extract(path, source):
    """Parse one file into (ast, Facts). Raises SyntaxError."""
    tree = ast.parse(source)
    facts = Facts(path)
    _Visitor(facts).visit(tree)
    return tree, facts


# --- repo traversal ----------------------------------------------------

def scan_repo(root):
    """rel -> (mtime_ns, size), stat only.

    Deliberately does not read contents. On a large repo the warm path is
    dominated by discovering that nothing changed, and `stat` is roughly an
    order of magnitude cheaper than read+hash for that job.
    """
    root = str(root)
    out = {}
    stack = [root]
    prefix = len(root) + 1
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name not in SKIP_DIRS:
                        stack.append(e.path)
                elif e.name.endswith(".py"):
                    # scandir caches stat on most platforms, so this is free.
                    st = e.stat(follow_symlinks=False)
                    out[e.path[prefix:]] = (st.st_mtime_ns, st.st_size)
            except OSError:
                continue
    return out


def read_file(root, rel):
    """Source text, or None if unreadable."""
    try:
        with open(os.path.join(root, rel), "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def walk_repo(root):
    for rel in scan_repo(root):
        src = read_file(root, rel)
        if src is not None:
            yield rel, src


def module_key(rel):
    mod = rel[:-3].replace(os.sep, ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def resolve_imports(rel, imports, by_module, pkg_root):
    """Exact resolution only.

    An earlier draft fell back to matching the last dotted component, which
    linked `db.models` to `contrib.gis.db.models` and fabricated ~93% of the
    edges on Django -- enough to collapse the repo into one component and make
    every change look global. Unresolved imports are counted, not guessed.
    """
    parts = os.path.dirname(rel).split(os.sep) if os.path.dirname(rel) else []
    hits, misses = set(), 0

    for mod, level, names in imports:
        if level > 0:
            base = parts[: len(parts) - (level - 1)] if level > 1 else parts
            stem = ".".join([p for p in base if p] + ([mod] if mod else []))
        else:
            stem = mod
            if pkg_root and stem.startswith(pkg_root + "."):
                stem = stem[len(pkg_root) + 1:]
            elif stem == pkg_root:
                stem = ""

        found = False
        for cand in [f"{stem}.{n}" if stem else n for n in names] + ([stem] if stem else []):
            target = by_module.get(cand)
            if target and target != rel:
                hits.add(target)
                found = True
        if not found and (stem or names):
            misses += 1

    return hits, misses


# --- graph algorithms --------------------------------------------------

def reverse(edges):
    out = {}
    for src, dsts in edges.items():
        for d in dsts:
            out.setdefault(d, set()).add(src)
    return out


def propagate(seed, rev):
    """Transitive closure over reverse edges -- the dirty propagation described
    in Davis (2025), 'Graph-based AI Compiler'."""
    seen, queue = set(seed), deque(seed)
    while queue:
        cur = queue.popleft()
        for up in rev.get(cur, ()):
            if up not in seen:
                seen.add(up)
                queue.append(up)
    return seen


def marker_reachable(marked_fns, call_edges, by_name, import_edges=None):
    """Functions that are, or can transitively reach, a marked function.

    Call resolution is name-based, which in Python over-approximates badly:
    `execute` names hundreds of unrelated methods. Left unconstrained the
    closure saturates -- on Django it reached ~64% of all functions and made
    every probe produce the same answer, which is a tell that the graph is
    measuring nothing.

    Constraining a call to targets the caller's file can actually see (itself
    plus its resolved imports) is a cheap, sound-ish precision fix: a call can
    only bind to something reachable through the import graph. It still
    over-approximates within that set. Closing the remaining gap needs real
    type inference -- which is precisely the expensive part that commercial
    reachability engines sell.
    """
    visible = None
    if import_edges is not None:
        visible = {f: set(dsts) | {f} for f, dsts in import_edges.items()}

    rev = {}
    for caller, callees in call_edges.items():
        caller_file = caller[0]
        allowed = visible.get(caller_file, {caller_file}) if visible is not None else None
        for callee in callees:
            for target in by_name.get(callee, ()):
                if target == caller:
                    continue
                if allowed is not None and target[0] not in allowed:
                    continue
                rev.setdefault(target, set()).add(caller)
    return propagate(marked_fns, rev)


def strongly_connected(nodes, edges):
    """Tarjan, iterative. Components sorted largest first."""
    idx, low, onstk, stk, comps, counter = {}, {}, {}, [], [], [0]
    adj = {n: set(edges.get(n, ())) for n in nodes}

    for start in nodes:
        if start in idx:
            continue
        idx[start] = low[start] = counter[0]
        counter[0] += 1
        stk.append(start)
        onstk[start] = True
        work = [(start, iter(adj[start]))]
        while work:
            v, it = work[-1]
            descended = False
            for w in it:
                if w not in idx:
                    idx[w] = low[w] = counter[0]
                    counter[0] += 1
                    stk.append(w)
                    onstk[w] = True
                    work.append((w, iter(adj[w])))
                    descended = True
                    break
                if onstk.get(w):
                    low[v] = min(low[v], idx[w])
            if descended:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == idx[v]:
                comp = []
                while True:
                    w = stk.pop()
                    onstk[w] = False
                    comp.append(w)
                    if w == v:
                        break
                comps.append(comp)

    comps.sort(key=len, reverse=True)
    return comps


def fanout(nodes, edges):
    """path -> number of files transitively depending on it (self included).

    Computed over the SCC condensation, so cost is linear in the condensed graph
    instead of one BFS per file. Files inside a large import cycle all share the
    cycle's fanout -- which is the point: that is where incremental analysis
    stops paying off.
    """
    comps = strongly_connected(nodes, edges)
    comp_of = {n: i for i, c in enumerate(comps) for n in c}

    # Condensed reverse graph: c -> components that depend on c.
    up = {}
    for src, dsts in edges.items():
        cs = comp_of.get(src)
        if cs is None:
            continue
        for d in dsts:
            cd = comp_of.get(d)
            if cd is not None and cd != cs:
                up.setdefault(cd, set()).add(cs)

    memo = {}
    for c in range(len(comps)):
        if c in memo:
            continue
        # Iterative DFS with an explicit stack; the condensation is a DAG.
        stack = [(c, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                acc = {node}
                for parent in up.get(node, ()):
                    acc |= memo[parent]
                memo[node] = acc
                continue
            if node in memo:
                continue
            stack.append((node, True))
            for parent in up.get(node, ()):
                if parent not in memo:
                    stack.append((parent, False))

    sizes = [len(c) for c in comps]
    return {
        n: sum(sizes[c] for c in memo[comp_of[n]])
        for n in nodes if n in comp_of
    }


def percentiles(values, points=(50, 75, 90, 99)):
    if not values:
        return {p: 0 for p in points}
    ordered = sorted(values)
    out = {}
    for p in points:
        k = max(0, min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1)))))
        out[p] = ordered[k]
    return out
