"""Security probe: dangerous sinks and hardcoded credentials.

Marks the code an attacker would care about reaching. Kept deliberately narrow --
a short defensible sink list beats a long noisy one, and a probe that fires on a
quarter of the codebase is not a filter.
"""

import ast
import re

from .. import analyze, cpg
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

# Calls that neutralize taint, and *what they neutralize it for*. Sanitizing is
# sink-specific: shlex.quote makes a string safe to hand to a shell and does
# exactly nothing for SQL. A flat set of names cannot express that, and treating
# one as global turns a real injection into a false negative -- the expensive
# kind of mistake.
#
# Names alone are also not enough to identify the callable. `quote` is
# shlex.quote, urllib.parse.quote and html-ish escapers depending on the import;
# only the first sanitizes anything here. So a bare name is credible only if the
# file imported a module that actually provides it, which mirrors how sinks are
# resolved.
SANITIZERS = {
    "quote": frozenset({"command-exec"}),         # shlex.quote
    "shlex_quote": frozenset({"command-exec"}),
    "escape_string": frozenset({"sql"}),          # MySQLdb / pymysql
    "quote_ident": frozenset({"sql"}),            # psycopg2 identifiers
    "quote_identifier": frozenset({"sql"}),
    "sql_escape": frozenset({"sql"}),
    "literal": frozenset({"sql"}),
    "mogrify": frozenset({"sql"}),                # psycopg2 cursor
}

# Modules that genuinely provide a sanitizer, and for which sink kind.
SANITIZER_MODULES = {
    "shlex": frozenset({"command-exec"}),
    "psycopg2": frozenset({"sql"}),
    "pymysql": frozenset({"sql"}),
    "MySQLdb": frozenset({"sql"}),
}

# Driver methods reached through a connection or cursor rather than a module,
# so the receiver is never a resolvable name. Distinctive enough to accept by
# convention, the same argument SQLISH makes for sinks.
SQL_SANITIZER_METHODS = frozenset({
    "escape_string", "quote_ident", "quote_identifier", "sql_escape",
    "literal", "mogrify",
})

ALL_SINK_KINDS = frozenset(SINKS.values())

DANGEROUS_MODULES = {
    "subprocess", "os", "pickle", "yaml", "marshal", "shelve", "dill",
    "sqlite3", "psycopg2", "pymysql", "MySQLdb",
}

SQLISH = {"execute", "executescript", "executemany", "raw"}

# subprocess entry points, where "does a shell see this?" is decidable from the
# call itself rather than needing type inference.
SUBPROCESS_SINKS = frozenset({"run", "Popen", "call", "check_call", "check_output"})

# Programs that interpret their argument as a command line, so an argv vector
# naming one is every bit as dangerous as shell=True.
SHELL_PROGRAMS = frozenset({"sh", "bash", "zsh", "ksh", "dash", "csh", "tcsh", "fish"})

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
            if kind and binds_dangerously(call.simple, call.dotted, risky_import):
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
            policy = _SecurityTaint(risky_import, roots)
            for fn in _functions(tree):
                for kind, call, desc in cpg.analyze_function(fn, policy):
                    yield Marker(
                        fn.aic_qualname, f"tainted-{kind}",
                        f"{desc}  ->  {_call_label(call)}",
                        getattr(call, "lineno", fn.lineno),
                    )


def binds_dangerously(simple, dotted, risky_import):
    """Could this call plausibly bind to a dangerous implementation?

    The bare name is not enough to decide. `loads` is the motivating case:
    `pickle.loads` executes arbitrary code and `json.loads` does not, and both
    are spelled `loads`. Judging on the name alone flagged every `json.loads` in
    the world as a deserialization sink -- on starlette that was 100% of the
    findings, and they ranked first, because dataflow-confirmed findings outrank
    heuristic ones.

    Both passes must ask this same question, which is why it lives here rather
    than inside either one.
    """
    if simple in ("eval", "exec"):
        return True
    if dotted and "." in dotted:
        if dotted.split(".")[0] in DANGEROUS_MODULES:
            return True
        # cursor.execute(...) / conn.executemany(...) -- SQL by convention.
        return simple in SQLISH
    # A bare name, or a receiver we cannot resolve (`connect().cursor().execute`).
    # Only credible if the file imported something dangerous at all.
    return risky_import


def reaches_a_shell(call):
    """Does this subprocess call hand its argument to a shell?

    `subprocess.run(["ls", user])` does not: with shell=False, which is the
    default, the first argument is an argv vector and no shell parses it. That
    is the form security-conscious code uses on purpose, and flagging it was
    noise on exactly the pattern we want people to write.

    Undecidable cases resolve to True. A non-literal `shell=` value or an argv
    whose program is not a constant could be anything, and a false negative here
    is worth more than a false positive.
    """
    for kw in call.keywords:
        if kw.arg == "shell":
            value = kw.value
            return bool(value.value) if isinstance(value, ast.Constant) else True
        if kw.arg is None:
            return True                      # run(**opts) -- shell may be in there
    if not call.args:
        return False
    first = call.args[0]
    if isinstance(first, (ast.List, ast.Tuple)):
        if not first.elts:
            return False
        program = first.elts[0]
        if isinstance(program, ast.Constant) and isinstance(program.value, str):
            return program.value.rsplit("/", 1)[-1] in SHELL_PROGRAMS
        return True                          # cannot tell what is being launched
    # A bare string with shell=False is a program *name*, not a command line;
    # Python hands it to exec directly. Broken, perhaps, but not an injection.
    return False


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
    """Parameters are attacker-controlled; SINKS are dangerous; SANITIZERS clear.

    `risky_import` carries the file's context, because whether a sink name binds
    to anything dangerous depends on what the file imported. Without it this pass
    judged on the bare name and could not tell `json.loads` from `pickle.loads`.
    """

    def __init__(self, risky_import=False, roots=frozenset()):
        self.risky_import = risky_import
        self.roots = frozenset(roots)

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

    def all_kinds(self):
        return ALL_SINK_KINDS

    def sanitizer_kinds(self, call):
        """What this call actually neutralizes, if anything.

        `html.escape(x)` used to clear taint outright, because the check was on
        the bare name. It escapes markup; it protects no sink in this list.
        """
        simple, dotted = analyze.call_name(call.func)
        covers = SANITIZERS.get(simple)
        if not covers:
            return frozenset()
        if dotted and "." in dotted:
            root = dotted.split(".")[0]
            if root in SANITIZER_MODULES:
                return covers & SANITIZER_MODULES[root]
            # A method on a cursor/connection we cannot name.
            return covers if simple in SQL_SANITIZER_METHODS else frozenset()
        # Bare name: only as credible as the file's imports make it.
        provided = frozenset()
        for root in self.roots & set(SANITIZER_MODULES):
            provided |= SANITIZER_MODULES[root]
        if simple in SQL_SANITIZER_METHODS:
            provided |= frozenset({"sql"})
        return covers & provided

    def is_source_call(self, call):
        # Intra-procedural: parameters are the only source for now. Cross-file
        # sources (request.GET etc.) arrive with stage 4 summaries.
        return False

    def sink_for(self, call):
        simple, dotted = analyze.call_name(call.func)
        kind = SINKS.get(simple)
        if kind is None:
            return None
        if not binds_dangerously(simple, dotted, self.risky_import):
            return None
        # os.system and os.popen always go through a shell. The subprocess
        # family only does when the call says so -- see reaches_a_shell.
        if simple in SUBPROCESS_SINKS and not reaches_a_shell(call):
            return None
        return kind, [SINK_ARG.get(simple, 0)]
