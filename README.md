# AIC — incremental impact analysis

**One question:** *I changed this file. What else do I now need to look at?*

Most analysis tools answer that with "everything," because they are stateless —
they rebuild their understanding of the repo on every invocation. That is fine
at PR time, when it happens once. It is not fine inside an agent loop, where a
coding agent might touch forty files before it stops to think.

AIC keeps a persistent graph of the codebase and answers the question
incrementally. Change one file and it reparses one file, then tells you exactly
which of your obligations that change put at risk.

On Django (883 files, 9,213 functions):

```
cold index                1432 ms
re-index, nothing changed   87 ms
re-index after one edit    101 ms   (1 file reparsed, 882 skipped)
```

The same one-file edit produces a very different blast radius depending on
*which* file:

| edited file | dependents invalidated |
|---|---:|
| `contrib/gis/db/backends/mysql/schema.py` | **1** |
| `db/models/query.py` | **570** |

A stateless scanner cannot tell those two apart. It does identical full work
either way.

## Install

```bash
pip install -e .            # no runtime dependencies
pip install -e ".[test]"    # to run the suite
```

Python 3.9+.

## Usage

```bash
aic index  path/to/repo                     # build or update the graph
aic touch  path/to/repo src/models.py       # invalidate one file, no repo walk
aic status path/to/repo --probe security --top 5
aic impact path/to/repo src/models.py       # what this change implicates
aic fanout path/to/repo                     # blast radius across every file
```

`index` is the interesting one — run it twice. The graph lives in
`<repo>/.aic/graph.db`.

`touch` is the agent-facing path: an editor hook or coding agent already knows
which file it changed, so there is nothing to discover. On Django a single-file
`touch` completes in ~15ms of analysis (peripheral file) — the remaining
wall-clock is Python interpreter startup, which a resident process removes.

```jsonc
// .claude/settings.json — re-verify on every write
"hooks": { "PostToolUse": [{ "matcher": "Write|Edit",
  "hooks": [{ "type": "command", "command": "aic touch . \"$CLAUDE_FILE_PATH\"" }] }] }
```

```console
$ aic index django/
mode                      incremental
files on disk             883
  reparsed                1  (added 0, changed 1)
  skipped (unchanged)     882
marked dirty (dependents) 570
graph                     883 files / 9213 functions / 3295 import edges
elapsed                   101 ms
```

## Probes

A **probe** decides what is *interesting*. The engine decides what is
*affected*. Everything downstream of a probe — reachability, dirty propagation,
blast radius — is probe-agnostic, which is the point: the incremental machinery
is not a security feature.

| probe | marks | answers |
|---|---|---|
| `security` | dangerous sinks (exec, SQL, deserialization), hardcoded credentials, and — via dataflow — sinks a parameter actually reaches | what did I put at risk? |
| `api` | public functions and methods | whose contract might I have broken? |
| `tests` | test functions | what do I have to re-run? |

They select very differently, which is how you know the seam is real — on
Django, `security` reaches 4.4% of functions, `api` reaches 83.6%, `tests`
reaches 0.3%.

The `security` probe runs two passes. A cheap heuristic pass marks every
dangerous *call site*. A dataflow pass then promotes a sink to `tainted-*` only
when a function parameter actually flows into it — telling
`cur.execute("... " + uid)` apart from `cur.execute("SELECT 1")`. On Django the
dataflow pass clears a third of the heuristic sinks (256 → 171) as
static/non-reachable. Against a ground-truth corpus of 19 hand-written cases
(`tests/fixtures/taint_cases.py`) it scores 1.00 precision / 1.00 recall,
including parameterized queries, sanitized inputs, and reassigned-clean locals —
the cases that separate dataflow from grep.

Adding one means implementing a single method:

```python
class MyProbe(Probe):
    name = "deprecations"
    description = "calls into APIs scheduled for removal"

    def inspect(self, path, tree, facts):
        for call in facts.calls:
            if call.dotted in DOOMED:
                yield Marker(call.caller, "deprecated", call.dotted, call.line)
```

Register it in `aic/probes/__init__.py`. There is deliberately no plugin
discovery and no config DSL.

## Benchmarks

```bash
python bench/run.py          # pinned sdists from PyPI, writes bench/RESULTS.md
```

Results across five well-known packages are in [bench/RESULTS.md](bench/RESULTS.md).
The headline is the **blast-radius distribution** — computed for every file, not
two hand-picked ones:

| package | files | median | p90 | max | mean | largest import cycle |
|---|---:|---:|---:|---:|---:|---:|
| requests 2.32.3 | 18 | 6 | 9 | 14 | 6.0 | 1 (6%) |
| flask 3.0.3 | 24 | 22 | 22 | 23 | 19.5 | 19 (79%) |
| celery 5.4.0 | 158 | 6 | 89 | 123 | 41.2 | 34 (22%) |
| sqlalchemy 2.0.36 | 255 | 40 | 245 | 248 | 127.1 | 121 (47%) |
| django 5.2.16 | 883 | **3** | 571 | 588 | **140.4** | 162 (18%) |

Median far below mean is the whole finding: **most changes are cheap to verify,
a minority are catastrophic, and the average tells you nothing about either.**
The expensive minority are the files sitting inside the largest import cycle.
For Django that is 162 files — 18% of the repo where incremental analysis buys
comparatively little, and 82% where it buys nearly everything.

That distribution is computable for any repo, and it predicts how much a given
codebase will benefit before you deploy anything.

## How it works

1. **Parse** every file to facts — functions with line numbers, calls,
   decorators, module-level assignments, argument annotations. Nothing is
   summarized away; a lossy signature skeleton cannot represent the difference
   between `@requires_admin def drop()` and `def drop()`.
2. **Resolve imports exactly.** No suffix matching. (An early draft matched on
   the last dotted component, which linked `db.models` to
   `contrib.gis.db.models`, fabricated ~93% of Django's edges, and collapsed the
   repo into one component. Unresolved imports are counted and reported, never
   guessed.)
3. **Run probes** to mark interesting nodes.
4. **On change:** hash-diff the tree, reparse only what moved, evict what was
   deleted, then propagate DIRTY transitively through reverse import edges.
5. **On query:** intersect the invalidated set with the probe's reachable set.

### Known approximations

Stated plainly, because they bound what the numbers mean.

- **Taint is intra-procedural.** The dataflow pass tracks a parameter to a sink
  *within one function*. It does not yet follow taint across calls, so a source
  read in one function and passed to a sink in another is missed. Cross-file
  sources (`request.GET`, `os.environ`) are not yet modelled as sources at all.
  Inter-procedural summaries are the next stage; the design for it is in
  DESIGN.md §7.
- **Call resolution is name-based**, constrained to targets visible through the
  caller's imports. Python's dynamic dispatch is not statically decidable, so
  this over-approximates — the safe direction for a filter, which may flag too
  much but never too little. Without the import constraint the closure saturates
  at ~64% of all functions and every probe returns the same answer; with it,
  `security` lands at 4.4%. Closing the remaining gap needs real type inference.
- **Blast radius is file-granular.** Reducing it to function granularity is the
  same work as inter-procedural taint, and would most help files inside a large
  import cycle — exactly where the current approach is weakest.
- **Python only.**

## Lineage

The dirty-propagation model comes from
[Graph-based AI Compiler](https://www.tdcommons.org/dpubs_series/8241/) (Rhodes
Floyd Davis Jr., Technical Disclosure Commons, June 2025) — a codebase graph
where marking a node dirty recursively invalidates its dependents, and a
separate pass resolves dirty nodes until the graph is clean.

That disclosure describes generation: a node holds authored intent and is
"collapsed" into code. AIC runs the same machinery in the analysis direction —
nodes hold extracted facts, and the fixpoint being sought is verification rather
than generation. Design notes and the reasoning behind the rewrite are in
[DESIGN.md](DESIGN.md).

## Acknowledgements

Inspired by ideas from [@rodydavis](https://github.com/rodydavis).

## License

MIT — see [LICENSE](LICENSE).
