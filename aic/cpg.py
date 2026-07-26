"""Control flow and intra-procedural dataflow.

Stage 1 of a Code Property Graph: a real CFG per function, plus a worklist
dataflow engine over it. The engine is policy-free -- what counts as a source,
a sanitizer, or a sink is supplied by the caller (see aic/probes/security.py).

Deliberate scope limits, so the numbers downstream mean what they say:
  - Intra-procedural only. No cross-function summaries yet.
  - Tracks taint on plain names and simple attribute chains (`self.x`), not on
    container elements or aliased objects.
  - `try` edges are approximated: every statement in a `try` body is treated as
    able to reach every handler, which over-approximates reachability in the
    safe direction.

Nothing here is persisted. CFGs are cheap to rebuild from one file's AST, and
storing them would wreck the incremental story we just built -- only findings
and (eventually) per-function summaries need to survive a run.
"""

import ast

# Statement types that end a linear run of control flow.
_TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)


class CFG:
    """Statement-granular control flow graph for one function.

    Nodes are AST statements, identified by id(). Using statements rather than
    basic blocks costs a few more nodes and saves a lot of construction code;
    the dataflow result is identical.
    """

    def __init__(self, entry):
        self.entry = entry
        self.succ = {}        # id(stmt) -> [stmt, ...]
        self.nodes = {}       # id(stmt) -> stmt

    def add(self, stmt, successors):
        self.nodes[id(stmt)] = stmt
        self.succ[id(stmt)] = [s for s in successors if s is not None]

    def successors(self, stmt):
        return self.succ.get(id(stmt), [])

    def __len__(self):
        return len(self.nodes)


class _Builder:
    def __init__(self):
        self.cfg = None
        self.loops = []        # (continue_target, break_target)

    def build(self, body):
        self.cfg = CFG(body[0] if body else None)
        self._seq(body, None)
        return self.cfg

    def _seq(self, stmts, after):
        """Wire a statement list so each flows to the next, last flows to `after`.
        Returns the entry statement of the list."""
        entry = None
        for i, stmt in enumerate(stmts):
            nxt = stmts[i + 1] if i + 1 < len(stmts) else after
            head = self._stmt(stmt, nxt)
            if entry is None:
                entry = head
        return entry or after

    def _stmt(self, stmt, after):
        if isinstance(stmt, ast.If):
            then_entry = self._seq(stmt.body, after)
            else_entry = self._seq(stmt.orelse, after) if stmt.orelse else after
            self.cfg.add(stmt, [then_entry, else_entry])
            return stmt

        if isinstance(stmt, (ast.While, ast.For, ast.AsyncFor)):
            # Loop header flows into the body and past the loop.
            exit_target = self._seq(stmt.orelse, after) if stmt.orelse else after
            self.loops.append((stmt, exit_target))
            body_entry = self._seq(stmt.body, stmt)   # back-edge to the header
            self.loops.pop()
            self.cfg.add(stmt, [body_entry, exit_target])
            return stmt

        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            body_entry = self._seq(stmt.body, after)
            self.cfg.add(stmt, [body_entry])
            return stmt

        if isinstance(stmt, ast.Try):
            handler_entries = [
                self._seq(h.body, after) for h in stmt.handlers if h.body
            ]
            final_entry = self._seq(stmt.finalbody, after) if stmt.finalbody else after
            else_entry = self._seq(stmt.orelse, final_entry) if stmt.orelse else final_entry
            body_entry = self._seq(stmt.body, else_entry)
            # Over-approximate: the try header may reach any handler.
            self.cfg.add(stmt, [body_entry] + handler_entries)
            return stmt

        if isinstance(stmt, (ast.Break, ast.Continue)):
            if self.loops:
                header, exit_target = self.loops[-1]
                target = header if isinstance(stmt, ast.Continue) else exit_target
                self.cfg.add(stmt, [target])
            else:
                self.cfg.add(stmt, [])
            return stmt

        if isinstance(stmt, (ast.Return, ast.Raise)):
            self.cfg.add(stmt, [])
            return stmt

        # Nested definitions are analyzed separately; do not descend here.
        self.cfg.add(stmt, [after])
        return stmt


def build_cfg(fn_node):
    """CFG for a FunctionDef / AsyncFunctionDef body."""
    return _Builder().build(fn_node.body)


# --- taint dataflow ----------------------------------------------------

def target_names(node):
    """Names bound by an assignment target."""
    out = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, ast.Name):
            out.append(cur.id)
        elif isinstance(cur, ast.Attribute):
            base = _attr_chain(cur)
            if base:
                out.append(base)
        elif isinstance(cur, (ast.Tuple, ast.List)):
            stack.extend(cur.elts)
        elif isinstance(cur, ast.Starred):
            stack.append(cur.value)
    return out


def _attr_chain(node):
    """`self.conn.raw` -> 'self.conn.raw', or None if not a pure chain."""
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


class TaintPolicy:
    """What the analysis treats as dangerous. Supplied by a probe."""

    def all_kinds(self):
        """Every sink kind this policy knows about.

        Taint is tracked per kind, not as a single bit, because sanitising is
        kind-specific: shlex.quote makes a string safe to hand to a shell and
        does nothing whatsoever for SQL. A value is therefore "still dangerous
        for {sql}" rather than simply "tainted".
        """
        return frozenset()

    def is_source_call(self, call):
        """This call returns attacker-controlled data."""
        return False

    def sanitizer_kinds(self, call):
        """Which sink kinds this call neutralises. Empty means: not a sanitiser."""
        return frozenset()

    def seed_names(self, fn_node):
        """Variables tainted on entry -- normally the parameters."""
        return set()

    def sink_for(self, call):
        """Return (kind, [arg_indices_that_matter]) or None."""
        return None


def analyze_function(fn_node, policy):
    """Run taint to a fixpoint over the function's CFG.

    Returns a list of (kind, call_node, tainted_expression_description).
    """
    cfg = build_cfg(fn_node)
    if not cfg.entry:
        return []

    every = policy.all_kinds()
    seed = {name: every for name in policy.seed_names(fn_node)}
    state_in = {id(cfg.entry): seed}
    findings = {}
    worklist = [cfg.entry]
    guard = 0
    limit = max(200, len(cfg) * 12)      # loops converge fast; this is a backstop

    while worklist and guard < limit:
        guard += 1
        stmt = worklist.pop()
        incoming = state_in.get(id(stmt), {})
        out, hits = _transfer(stmt, incoming, policy)

        for kind, call, desc in hits:
            findings.setdefault((kind, id(call)), (kind, call, desc))

        for succ in cfg.successors(stmt):
            prev = state_in.get(id(succ))
            merged = out if prev is None else _merge(prev, out)
            if prev is None or merged != prev:
                state_in[id(succ)] = merged
                worklist.append(succ)

    return list(findings.values())


def _merge(a, b):
    """Join two states: a name is dangerous for the union of both kind sets."""
    out = dict(a)
    for name, kinds in b.items():
        out[name] = out[name] | kinds if name in out else kinds
    return out


def _transfer(stmt, tainted, policy):
    """Apply one statement. Returns (new_state, findings_in_this_statement)."""
    state = dict(tainted)
    hits = []

    # Every call in the statement is a potential sink, evaluated against the
    # state entering the statement. A sink of kind K only fires if the argument
    # is still dangerous *for K* -- a shell-quoted string is safe for os.system
    # and still an injection when it reaches cursor.execute.
    for node in ast.walk(stmt):
        if isinstance(node, ast.Call):
            sink = policy.sink_for(node)
            if not sink:
                continue
            kind, arg_indices = sink
            for i in arg_indices:
                if i < len(node.args) and kind in taint_kinds(node.args[i], state, policy):
                    hits.append((kind, node, _describe(node.args[i])))
                    break

    def rebind(names, kinds, keep_on_clean=False):
        for name in names:
            if kinds:
                state[name] = state[name] | kinds if keep_on_clean and name in state else kinds
            elif not keep_on_clean:
                state.pop(name, None)

    if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        value = stmt.value
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        names = [n for t in targets for n in target_names(t)]
        kinds = taint_kinds(value, state, policy) if value is not None else frozenset()
        # x += clean keeps whatever x already carried; x = clean clears it.
        rebind(names, kinds, keep_on_clean=isinstance(stmt, ast.AugAssign))

    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        rebind(target_names(stmt.target), taint_kinds(stmt.iter, state, policy))

    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        for item in stmt.items:
            if item.optional_vars is not None:
                rebind(target_names(item.optional_vars),
                       taint_kinds(item.context_expr, state, policy))

    return state, hits


def taint_kinds(node, state, policy):
    """Which sink kinds this expression is still dangerous for.

    Empty means safe. This replaces a boolean because sanitising is
    kind-specific -- `shlex.quote(x)` leaves a value safe for a shell and every
    bit as dangerous for SQL, and a single bit cannot say that.
    """
    if node is None or isinstance(node, ast.Constant):
        return frozenset()

    if isinstance(node, ast.Name):
        return state.get(node.id, frozenset())

    if isinstance(node, ast.Attribute):
        chain = _attr_chain(node)
        if chain and chain in state:
            return state[chain]
        return taint_kinds(node.value, state, policy)

    if isinstance(node, ast.Call):
        if policy.is_source_call(node):
            return policy.all_kinds()
        inner = _union(taint_kinds(a, state, policy)
                       for a in list(node.args) + [k.value for k in node.keywords])
        # Conservative: str(x), "".join(parts), tmpl.format(x) all carry taint,
        # and so does the receiver -- `tainted.format(x)`.
        inner |= taint_kinds(node.func, state, policy)
        # The sanitiser applies to whatever the call produces, receiver included:
        # `cursor.mogrify(name)` is safe for SQL even though `cursor` is itself a
        # parameter. It removes only what it covers; the rest survives.
        return inner - policy.sanitizer_kinds(node)

    if isinstance(node, ast.Subscript):
        return taint_kinds(node.value, state, policy)

    if isinstance(node, ast.JoinedStr):                # f-strings
        return _union(taint_kinds(v, state, policy) for v in node.values)

    if isinstance(node, ast.FormattedValue):
        return taint_kinds(node.value, state, policy)

    if isinstance(node, ast.BinOp):                    # includes % formatting and +
        return (taint_kinds(node.left, state, policy)
                | taint_kinds(node.right, state, policy))

    if isinstance(node, ast.BoolOp):
        return _union(taint_kinds(v, state, policy) for v in node.values)

    if isinstance(node, ast.IfExp):
        return (taint_kinds(node.body, state, policy)
                | taint_kinds(node.orelse, state, policy))

    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return _union(taint_kinds(e, state, policy) for e in node.elts)

    if isinstance(node, ast.Dict):
        return _union(taint_kinds(v, state, policy) for v in node.values if v)

    if isinstance(node, (ast.Starred, ast.UnaryOp, ast.Await)):
        inner = getattr(node, "value", None) or getattr(node, "operand", None)
        return taint_kinds(inner, state, policy)

    return frozenset()


def _union(sets):
    out = frozenset()
    for s in sets:
        out |= s
    return out


def is_tainted(node, state, policy):
    """Dangerous for anything at all. Kept for callers that want one bit."""
    return bool(taint_kinds(node, state, policy))


def _describe(node):
    try:
        text = ast.unparse(node)
    except Exception:
        return "<expr>"
    return text if len(text) <= 60 else text[:57] + "..."
