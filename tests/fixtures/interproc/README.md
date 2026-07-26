# Inter-procedural ground truth

45 cases in 9 categories. Each case is a directory:

```
<category>/<case>/
    *.py            one or more modules -- multi-file where the category needs it
    expected.json   the verdict
```

```json
{
  "description": "Source and sink live in different modules.",
  "flows": [{"source": "views.view", "sink": "db.lookup", "kind": "sql"}],
  "safe_because": null
}
```

Functions are named `<module>.<qualname>` (`app.Repo.lookup`). Safe cases carry
`"flows": []` and a populated `safe_because`. Line numbers are deliberately absent
-- they churn on any edit to a fixture and add nothing to the verdict.

Run `pytest tests/test_interproc.py -rX -s` for the confusion matrix.

## Rules

**One execution path per case**, following PyCG's micro-benchmark convention, so
the correspondence between source and expected result is unambiguous. The
`recursion_cycles` category is the exception: a base case requires a branch.

**Safe cases are the point.** Every safe case is built so that a naive analysis
*will* flag it -- the callee really does sink a parameter, it is just never
passed anything dangerous. An engine that flags everything scores perfectly on
the tainted cases and is useless.

**Sources are held constant except where they are the subject.** Eight
categories use an entry function's parameter as the source, so they measure
inter-procedural propagation and nothing else. The `sources` category is the one
that varies real sources (`request.GET`, `os.environ`, `sys.argv`, `input()`),
so a failure there is attributable to source modelling rather than to
propagation.

## Categories

| category | n | isolates |
|---|---:|---|
| `direct` | 4 | one hop, caller to callee |
| `multi_hop` | 4 | 2-3 intermediate frames |
| `tito` | 6 | taint carried out through return values |
| `sanitize_across` | 6 | sanitizer in a different frame than the sink |
| `partial_param` | 5 | which argument the taint lands in |
| `methods` | 6 | `self` state, inheritance, MRO |
| `recursion_cycles` | 4 | recursive and mutually-recursive call cycles |
| `sources` | 5 | real cross-file sources |
| `dispatch_ambiguity` | 5 | same name, two implementations |

`dispatch_ambiguity` is load-bearing. It separates **call-graph error** from
**summary error** -- without it, a bad stage-4 number cannot be attributed to
either, and the fix for one looks like the fix for the other.

## The ceiling

An inter-procedural analysis cannot follow a call the call graph does not
resolve, so `analyze.resolved_calls` bounds anything built on top of it. The
runner reports how many expected flows have a resolvable call path at all, and
stage 4 should be judged against that rather than against 1.00.

Note the ceiling measured here (96%) is flattered by the fixtures being minimal
by construction. PyCG, a purpose-built Python call-graph tool, reports ~69.9%
recall on real packages. Treat 96% as an upper bound on a friendly corpus, not
as a property of the call graph in the field.
