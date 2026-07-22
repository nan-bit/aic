"""Ground-truth precision test for the dataflow pass.

Parses tests/fixtures/taint_cases.py, reads each function's verdict from its
docstring, runs the security probe's taint analysis, and checks agreement.
Prints a confusion matrix on failure so a regression says *what* moved.
"""

import ast
from pathlib import Path

import pytest

from aic import cpg
from aic.probes.security import _SecurityTaint

FIXTURE = Path(__file__).parent / "fixtures" / "taint_cases.py"


def load_cases():
    tree = ast.parse(FIXTURE.read_text(encoding="utf-8"))
    cases = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node) or ""
        verdict = doc.split()[0] if doc else ""
        if verdict not in ("TAINTED", "SAFE"):
            continue
        kind = doc.split()[1] if len(doc.split()) > 1 else None
        cases.append((node.name, node, verdict, kind))
    return cases


CASES = load_cases()


def run(node):
    return cpg.analyze_function(node, _SecurityTaint())


@pytest.mark.parametrize("name,node,verdict,kind", CASES, ids=[c[0] for c in CASES])
def test_case(name, node, verdict, kind):
    findings = run(node)
    if verdict == "TAINTED":
        assert findings, f"{name}: expected a {kind} finding, got none (false negative)"
        assert any(k == kind for k, _, _ in findings), \
            f"{name}: expected kind {kind}, got {[k for k, _, _ in findings]}"
    else:
        assert not findings, \
            f"{name}: expected SAFE, got {[(k, d) for k, _, d in findings]} (false positive)"


def test_corpus_summary(capsys):
    """Not a pass/fail gate -- prints the confusion matrix for the record."""
    tp = fp = tn = fn = 0
    for name, node, verdict, _ in CASES:
        flagged = bool(run(node))
        if verdict == "TAINTED":
            tp += flagged
            fn += not flagged
        else:
            fp += flagged
            tn += not flagged
    total = len(CASES)
    with capsys.disabled():
        print(f"\n  taint corpus: {total} cases  "
              f"TP={tp} FP={fp} TN={tn} FN={fn}  "
              f"precision={tp/(tp+fp):.2f} recall={tp/(tp+fn):.2f}"
              if (tp + fp and tp + fn) else "")
    assert fn == 0, "a false negative is a missed vulnerability -- fix before shipping"
