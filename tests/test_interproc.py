"""Inter-procedural ground truth -- the gate on stage 4.

The existing corpus (tests/fixtures/taint_cases.py) checks one function at a
time, because that is all the engine can currently see. These cases cannot be
expressed that way: a verdict here belongs to a *flow*, from a source in one
function to a sink in another, sometimes in another module. Hence a separate
format and a separate runner.

**Everything here is expected to fail until stage 4 lands**, so the per-case
tests are non-strict xfail: CI stays green while the number stays visible. Run
`pytest tests/test_interproc.py -rX -s` to see it.

Two things this measures that a single pass/fail rate would hide:

  * **Safe cases are the discriminator.** The current engine treats every
    parameter as attacker-controlled, so it "detects" most tainted flows by
    accident -- it flags any function whose parameter reaches a sink, whether or
    not anything dangerous is ever passed. The interesting number is therefore
    the false-positive rate on the safe cases, not the recall on the tainted
    ones.
  * **The call-graph ceiling.** An inter-procedural analysis cannot follow a
    call the call graph does not resolve, so `analyze.resolved_calls` bounds
    anything built on it. PyCG -- a purpose-built Python call-graph tool --
    reports ~69.9% recall on real packages, and aic's resolution is cruder.
    Reporting stage-4 recall against 1.00 instead of against this ceiling would
    charge taint for a call-graph failure.
"""

import ast
import json
import shutil
from collections import deque
from pathlib import Path

import pytest

from aic import analyze, cpg, query
from aic.probes.security import DANGEROUS_MODULES, _SecurityTaint, _functions
from aic.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "interproc"
XFAIL = pytest.mark.xfail(
    reason="stage 4 (inter-procedural summaries) not implemented", strict=False,
)


class Case:
    def __init__(self, directory):
        self.dir = directory
        self.category = directory.parent.name
        self.name = directory.name
        self.id = f"{self.category}/{self.name}"
        spec = json.loads((directory / "expected.json").read_text(encoding="utf-8"))
        self.description = spec["description"]
        self.flows = spec["flows"]
        self.safe_because = spec["safe_because"]
        self.modules = sorted(p for p in directory.glob("*.py"))

    @property
    def is_safe(self):
        return not self.flows


def load_cases():
    return [Case(d) for d in sorted(FIXTURES.glob("*/*")) if d.is_dir()]


CASES = load_cases()


# --- running the current engine ----------------------------------------

def findings_for(case):
    """(qualified_function, sink_kind) pairs the engine reports today.

    The policy is rebuilt per module, the way SecurityProbe.inspect builds it:
    whether a bare sink name is credible depends on what *that* file imported.
    A single shared policy would report `sqlite3.connect().cursor().execute()`
    as unreachable, because the receiver is a call rather than a dotted name and
    the only remaining evidence is the module's own imports.
    """
    out = set()
    for module in case.modules:
        source = module.read_text(encoding="utf-8")
        tree, facts = analyze.extract(str(module), source)
        policy = _SecurityTaint(bool(facts.imported_roots & DANGEROUS_MODULES))
        prefix = module.stem
        for fn in _functions(tree):
            for kind, _call, _desc in cpg.analyze_function(fn, policy):
                out.add((f"{prefix}.{fn.aic_qualname}", kind))
    return out


# --- call-graph ceiling ------------------------------------------------

def _node(qualified):
    """'app.Repo.lookup' -> ('app.py', 'Repo.lookup')"""
    module, _, qualname = qualified.partition(".")
    return (f"{module}.py", qualname)


def call_path_exists(store, source, sink):
    """Can the resolved call graph get from `source` to `sink` at all?"""
    src, dst = _node(source), _node(sink)
    if src == dst:
        return True
    fwd = analyze.resolved_calls(
        store.call_edges(), store.functions_by_name(), store.import_edges(),
    )
    seen, queue = {src}, deque([src])
    while queue:
        cur = queue.popleft()
        for nxt in fwd.get(cur, ()):
            if nxt == dst:
                return True
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


@pytest.fixture(scope="module")
def graphs(tmp_path_factory):
    """One indexed graph per case, built in a temp copy so fixtures stay clean."""
    built = {}
    for case in CASES:
        root = tmp_path_factory.mktemp(case.name) / case.name
        shutil.copytree(case.dir, root)
        (root / "expected.json").unlink()
        store = Store(query.db_for(root))
        query.refresh(store, root)
        built[case.id] = store
    yield built
    for store in built.values():
        store.close()


# --- the cases ---------------------------------------------------------

@XFAIL
@pytest.mark.parametrize("case", [c for c in CASES if not c.is_safe],
                         ids=lambda c: c.id)
def test_tainted_flow_is_reported(case):
    found = findings_for(case)
    for flow in case.flows:
        assert (flow["sink"], flow["kind"]) in found, (
            f"{case.id}: expected {flow['kind']} at {flow['sink']} "
            f"(from {flow['source']}); got {sorted(found)}"
        )


@XFAIL
@pytest.mark.parametrize("case", [c for c in CASES if c.is_safe],
                         ids=lambda c: c.id)
def test_safe_case_is_not_flagged(case):
    found = findings_for(case)
    assert not found, (
        f"{case.id}: false positive -- {case.safe_because}; got {sorted(found)}"
    )


def test_corpus_is_well_formed():
    """The fixtures themselves must parse and be internally consistent."""
    assert len(CASES) >= 40, "corpus should be broad enough to be worth trusting"
    for case in CASES:
        assert case.modules, f"{case.id} has no modules"
        assert case.description, f"{case.id} has no description"
        for module in case.modules:
            ast.parse(module.read_text(encoding="utf-8"))
        if case.is_safe:
            assert case.safe_because, f"{case.id} is safe but does not say why"
        else:
            assert case.safe_because is None, f"{case.id} has both flows and a safe reason"
            for flow in case.flows:
                assert set(flow) == {"source", "sink", "kind"}, f"{case.id}: bad flow"


def test_baseline_report(capsys, graphs):
    """Not a gate -- prints the confusion matrix and the call-graph ceiling.

    This is the 'before' number stage 4 has to beat, and the ceiling it should
    be judged against.
    """
    tp = fn = fp = tn = 0
    for case in CASES:
        found = findings_for(case)
        if case.is_safe:
            if found:
                fp += 1
            else:
                tn += 1
        else:
            hit = all((f["sink"], f["kind"]) in found for f in case.flows)
            tp += hit
            fn += not hit

    reachable = total_flows = 0
    unresolved = []
    for case in CASES:
        for flow in case.flows:
            total_flows += 1
            if call_path_exists(graphs[case.id], flow["source"], flow["sink"]):
                reachable += 1
            else:
                unresolved.append(f"{case.id}: {flow['source']} -> {flow['sink']}")

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    ceiling = reachable / total_flows if total_flows else 0.0

    with capsys.disabled():
        print(f"\n  inter-procedural corpus: {len(CASES)} cases "
              f"({tp + fn} tainted / {fp + tn} safe)")
        print(f"    TP={tp} FN={fn} FP={fp} TN={tn}  "
              f"precision={precision:.2f} recall={recall:.2f}")
        print(f"    call-graph ceiling: {reachable}/{total_flows} flows "
              f"({ceiling:.0%}) have a resolvable call path")
        if unresolved:
            print(f"    unresolvable ({len(unresolved)}):")
            for line in unresolved:
                print(f"      {line}")
