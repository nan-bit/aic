"""Security probe: dangerous sinks and hardcoded credentials.

Marks the code an attacker would care about reaching. Kept deliberately narrow --
a short defensible sink list beats a long noisy one, and a probe that fires on a
quarter of the codebase is not a filter.
"""

import ast
import re

from .. import cpg
from .base import Marker, Probe

SINKS = {
    "system": "command-exec",
    "popen": "command-exec",
    "Popen": "command-exec",
    "check_output": "command-exec",
    "run": "command-exec",
    "eval": "code-exec",
    "exec": "code-exec",
    "loads": "deserialization",
    "load": "deserialization",
    "execute": "sql",
    "executescript": "sql",
    "executemany": "sql",
    "raw": "sql",
}

# Which positional argument of each sink carries the dangerous payload.
SINK_ARG = {
    "system": 0, "popen": 0, "Popen": 0, "check_output": 0, "run": 0,
    "eval": 0, "exec": 0, "loads": 0, "load": 0,
    "execute": 0, "executescript": 0, "executemany": 0, "raw": 0,
}

# Calls that neutralize taint. Narrow on purpose -- a wrong entry here creates a
# false negative, which is the expensive kind of mistake.
SANITIZERS = {
    "quote", "shlex_quote", "escape", "escape_string", "quote_ident",
    "quote_identifier", "sql_escape", "literal", "mogrify",
}

DANGEROUS_MODULES = {
    "subprocess", "os", "pickle", "yaml", "marshal", "shelve", "dill",
    "sqlite3", "psycopg2", "pymysql", "MySQLdb",
}

SQLISH = {"execute", "executescript", "executemany", "raw"}

SECRET_NAME = re.compile(
    r"(SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|PRIVATE_?KEY|CREDENTIAL|ACCESS_?KEY)",
    re.I,
)
# Values that look like placeholders rather than live credentials.
PLACEHOLDER = re.compile(r"^(|none|null|changeme|xxx+|<.*>|\{\{.*\}\}|\$\{.*\}|example.*)$", re.I)


class SecurityProbe(Probe):
    name = "security"
    description = "dangerous sinks (exec, SQL, deserialization) and hardcoded credentials"

    def inspect(self, path, tree, facts):
        roots = facts.imported_roots
        risky_import = bool(roots & DANGEROUS_MODULES)

        for call in facts.calls:
            kind = SINKS.get(call.simple)
            if kind and self._binds_dangerously(call, roots, risky_import):
                yield Marker(call.caller, kind, call.dotted or call.simple, call.line)

        for name, line, literal in facts.assignments:
            if not SECRET_NAME.search(name):
                continue
            if not isinstance(literal, str) or PLACEHOLDER.match(literal.strip()):
                continue
            if len(literal) < 8:
                continue
            yield Marker("", "hardcoded-secret", f"{name} = <{len(literal)} chars>", line)

        # Dataflow pass: promote a sink to "tainted-*" only when a parameter
        # actually reaches it. This is the difference between "calls execute"
        # and "builds SQL from an argument", and it is where the CPG earns its
        # keep -- the sink markers above over-report; these do not.
        if tree is not None:
            policy = _SecurityTaint()
            for fn in _functions(tree):
                for kind, call, desc in cpg.analyze_function(fn, policy):
                    yield Marker(
                        fn.aic_qualname, f"tainted-{kind}",
                        f"{desc}  ->  {_call_label(call)}",
                        getattr(call, "lineno", fn.lineno),
                    )

    @staticmethod
    def _binds_dangerously(call, roots, risky_import):
        if call.simple in ("eval", "exec"):
            return True
        if call.dotted and "." in call.dotted:
            if call.dotted.split(".")[0] in DANGEROUS_MODULES:
                return True
            # cursor.execute(...) / conn.executemany(...) -- SQL by convention.
            return call.simple in SQLISH
        # Bare call; only credible if the file imported something dangerous.
        return risky_import


def _call_simple(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _call_label(call):
    try:
        return ast.unparse(call.func)
    except Exception:
        return _call_simple(call) or "<call>"


def _functions(tree):
    """Yield FunctionDefs with a qualname stamped on, so a marker can name the
    function it lives in without re-deriving scope."""
    stack = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = ".".join(prefix + [child.name])
                child.aic_qualname = qual
                yield child
                yield from walk(child, prefix + [child.name])
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, prefix + [child.name])
            else:
                yield from walk(child, prefix)

    yield from walk(tree, stack)


class _SecurityTaint(cpg.TaintPolicy):
    """Parameters are attacker-controlled; SINKS are dangerous; SANITIZERS clear."""

    def seed_names(self, fn_node):
        a = fn_node.args
        names = set()
        for group in (getattr(a, "posonlyargs", []), a.args, a.kwonlyargs):
            for arg in group:
                if arg.arg not in ("self", "cls"):
                    names.add(arg.arg)
        if a.vararg:
            names.add(a.vararg.arg)
        if a.kwarg:
            names.add(a.kwarg.arg)
        return names

    def is_sanitizer_call(self, call):
        return _call_simple(call) in SANITIZERS

    def is_source_call(self, call):
        # Intra-procedural: parameters are the only source for now. Cross-file
        # sources (request.GET etc.) arrive with stage 4 summaries.
        return False

    def sink_for(self, call):
        simple = _call_simple(call)
        kind = SINKS.get(simple)
        if kind is None:
            return None
        return kind, [SINK_ARG.get(simple, 0)]
