# AIC — incremental impact analysis

> **I changed this file. What else do I now need to look at?**

**AIC** treats code analysis the way a build system treats compilation: do the
expensive work once, then on every change redo only what that change actually
touched. It keeps a persistent graph of the codebase and answers that one
question incrementally, for a coding agent, as the code is being written.

Most analysis tools answer "everything," because they are stateless — they
rebuild their understanding of the repo on every invocation. That is fine at PR
time, when it happens once. It is the wrong cost model inside an agent loop,
where a coding agent might touch forty files before it stops to think. Forty
edits should not mean forty full scans.

## Why this shape

The strongest evidence in static analysis is that **placement beats precision**.
Meta ran Infer two ways with the same analyzer and the same false-positive rate:
batch/offline deployment produced a near-zero fix rate; diff-time bot comments
produced **over 70%** ([Distefano et al., CACM
2019](https://cacm.acm.org/research/scaling-static-analyses-at-facebook/)). Their
explanation is that a diff supplies two things nothing else does — the developer
has mental context loaded, and the finding is *attributable* to the change that
caused it.

Google's Tricorder found the failure mode on the other side: tools that
"displayed results too early, while developers were still experimenting with
their code in the editor" fell out of use, as did tools that reported too late
([Sadowski et al., ICSE
2015](https://research.google/pubs/tricorder-building-a-program-analysis-ecosystem/)).

So the property that matters is not "continuous vs. gate" — it is
**attribution**: the smallest unit of change that can be blamed on someone who
currently cares. A coding agent mid-task is exactly such a unit, and it is a
smaller one than a PR.

The blocker is cost, not analysis. Re-verifying after every edit is only
affordable if re-verification costs something proportional to what the edit
reached. That requires state: a graph that persists, gets dirtied, and drains.
On Django (883 files, 9,213 functions):

```
first index (one time)        2.6 s    full parse + probes + taint dataflow
re-index, nothing changed      51 ms   stat-diff finds no work
one file, via `aic touch`   14–60 ms   reparse one file, propagate
```

That last number is the product. And it is not one number — the same one-file
edit costs wildly different amounts depending on *which* file, because the blast
radius does:

| edited file | dependents invalidated | recheck cost |
|---|---:|---:|
| `contrib/gis/db/backends/mysql/schema.py` | **1** | 14 ms |
| `db/models/query.py` | **570** | 60 ms |

(570 *dependents*; the impact set is 571 because it includes the edited file
itself. Both numbers appear below and they are the same measurement.)

A stateless scanner cannot tell those two apart; it does identical full work
either way. AIC does work proportional to what the change reached.

Worth being precise about the unit, because it is easy to conflate two things:
**a diff is what you changed; blast radius is what your change reached.** A
one-line edit to `db/models/query.py` is a one-line diff and a 571-file impact.
Scoping to the diff and scoping to the impact are different questions, and only
the second one needs a graph.

> **Why "compiler"?** The engine is incremental compilation: a dependency graph,
> dirty-marking on change, and a pass that re-resolves only dirty nodes until the
> graph is clean. AIC runs that machinery in the analysis direction. The name and
> the model come from the [Graph-based AI Compiler](#lineage) disclosure it is
> built on.

## Install

```bash
pip install -e .            # no runtime dependencies
pip install -e ".[test]"    # to run the suite
pip install -e ".[mcp]"     # to serve the graph to a coding agent
```

Python 3.9+. The optional MCP server needs 3.10+ and is the only thing here that
pulls dependencies.

## Agent integration

This is the point of the whole thing. The CLI pays ~110 ms of Python interpreter
startup per invocation — more than the analysis itself — so the agent-facing
surface is a resident MCP server rather than a shell hook.

```bash
claude mcp add aic -- aic-mcp /path/to/repo
```

Three tools, all read-only. There is nothing to index and nothing the agent has
to remember to call: every tool stat-diffs the tree first (~50 ms on Django) and
reparses only what moved, so the graph is current whenever it is asked, and a
missing graph is simply built on the first call.

| tool | answers |
|---|---|
| `aic_review` | what the edits made so far put at risk — the checkpoint call before declaring work done |
| `aic_impact` | the same question for one named file |
| `aic_overview` | how far changes travel in this repo, and where they stop being cheap |

The binding constraint is response size, not speed. A change to Django's
`db/models/query.py` reaches 571 files; returning that list would be both useless
and expensive. Instead the count is reported and the body is the ranked
intersection with what the probe actually marks — dataflow-confirmed findings
first, then by blast radius of the containing file, truncated with an explicit
note of what was elided:

```console
$ aic_impact("db/models/query.py")
db/models/query.py is depended on by 571 of 883 files. 789 of 9213 functions
match the security probe and are worth re-checking (91.4% of a full scan
avoided). Showing 20 of 426; raise limit= or narrow with probe=.

core/cache/backends/filebased.py:38   [tainted-deserialization]  zlib.decompress(f.read()) -> pickle.loads
...
```

That response is ~3.8 kB — under 1k tokens against a 25k-token cap, for the worst
file in the repo.

### What an agent actually does with it

Three headless Claude Code sessions against a purpose-built sandbox package, with
the agent given the MCP tools and nothing else. Full write-up in
[DESIGN.md §9](DESIGN.md).

- **The tools get used without being asked for.** The first run's prompt never
  mentioned AIC or impact analysis — it asked for a code change and ended with
  "tell me what else in this repo my change could have put at risk." The agent
  found and called the tools off their descriptions alone, and reasoned from the
  output: *"models.py is imported by 7 of 9 files, so it has the widest blast
  radius in the repo."* It then separated pre-existing findings from ones its own
  diff caused — attribution, unprompted.
- **The probe axis gets used deliberately.** Unprompted, it picked `api` to check
  a rename hadn't broken callers, `tests` for what to re-run, `security` for risk.
- **It found a bug that 70 passing tests did not.** Three consecutive `aic_review`
  calls returned 14 findings, then 0, then 0, while `aic_impact` on the same file
  returned 7. `refresh` cleared the dirty flag on every call, so the second call's
  no-op refresh erased the dependents established by the first — the server
  reported that a change to a hub module reached nothing. A false negative, and
  unreachable from the CLI, where each invocation is a fresh process doing one
  thing. It took one live agent loop to surface.

That last one is the argument for building the delivery surface before improving
the analyzer: no amount of taint precision matters while the thing is answering
"nothing to re-check."

## Probes — the engine is not a security engine

A **probe** decides what is *interesting*. The engine decides what is
*affected*. Everything downstream of a probe — reachability, dirty propagation,
blast radius — is probe-agnostic.

| probe | marks | answers |
|---|---|---|
| `security` | dangerous sinks (exec, SQL, deserialization), hardcoded credentials, and — via dataflow — sinks a parameter actually reaches | what did I put at risk? |
| `api` | public functions and methods | whose contract might I have broken? |
| `tests` | test functions | what do I have to re-run? |

They select very differently, which is how you know the seam is real rather than
a security tool wearing a platform costume — on Django, `security` reaches 8.6%
of functions, `api` reaches 83.6%, `tests` reaches 0.3%.

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

## Deep dive: the security probe

One probe taken further than the others, to show what the seam supports.

It runs two passes. A cheap heuristic pass marks every dangerous *call site*. A
dataflow pass — a real per-function CFG plus a worklist taint engine
(`aic/cpg.py`) — then promotes a sink to `tainted-*` only when a function
parameter actually flows into it, telling `cur.execute("... " + uid)` apart from
`cur.execute("SELECT 1")`. On Django the dataflow pass clears a third of the
heuristic sinks (256 → 171) as static.

The taint engine is policy-free: what counts as a source, a sanitizer or a sink
is supplied by the probe, not baked into the engine.

**Two corpora, and the second one is the honest number.**

*Intra-procedural* (`tests/fixtures/taint_cases.py`, 19 cases): 1.00 precision /
1.00 recall, including parameterized queries, sanitized inputs and
reassigned-clean locals — the cases that separate dataflow from grep. That number
is flattering and small.

*Inter-procedural* (`tests/fixtures/interproc/`, 45 cases in 9 categories,
modelled on [PyCG](https://arxiv.org/abs/2103.00587)'s micro-benchmark structure
and SecuriBench Micro's benign-flow discipline): **0.57 precision / 0.84 recall**.
Read the right way round — recall is high *by accident*. The current policy treats
every parameter as attacker-controlled, so it flags any function whose parameter
reaches a sink regardless of what is ever passed. The engine's inter-procedural
blindness shows up as **over-approximation, not omission**, and the 20 safe cases
are what measure it.

The corpus also records a **call-graph ceiling**: an inter-procedural analysis
cannot follow a call the call graph does not resolve, so 24/25 flows (96%) are the
most any summary-based pass could get here. That 96% is flattered by fixtures
being minimal by construction — PyCG reports ~69.9% recall on real packages with
better resolution than this. Judge future numbers against the ceiling, not
against 1.00.

## Blast radius predicts payoff before you deploy

```bash
python bench/run.py          # pinned sdists from PyPI, writes bench/RESULTS.md
```

Computed for every file, not two hand-picked ones
([bench/RESULTS.md](bench/RESULTS.md)):

| package | files | median | p90 | max | mean | largest import cycle |
|---|---:|---:|---:|---:|---:|---:|
| requests 2.32.3 | 18 | 6 | 9 | 14 | 6.0 | 1 (6%) |
| flask 3.0.3 | 24 | 22 | 22 | 23 | 19.5 | 19 (79%) |
| celery 5.4.0 | 158 | 6 | 89 | 123 | 41.2 | 34 (22%) |
| sqlalchemy 2.0.36 | 255 | 40 | 245 | 248 | 127.1 | 121 (47%) |
| django 5.2.16 | 883 | **3** | 571 | 588 | **140.4** | 162 (18%) |

Median far below mean is the whole finding: **most changes are cheap to verify, a
minority are catastrophic, and the average tells you nothing about either.** The
expensive minority are the files sitting inside the largest import cycle. For
Django that is 162 files — 18% of the repo where incremental analysis buys
comparatively little, and 82% where it buys nearly everything.

This is also a scheduling input, not just a benchmark. When verification costs
money and latency — an LLM call, a deep analysis pass — the distribution says
which edits deserve the expensive treatment and which deserve a graph lookup.

## CLI

```bash
aic index  path/to/repo                     # build or update the graph
aic touch  path/to/repo src/models.py       # invalidate one file, no repo walk
aic status path/to/repo --probe security --top 5
aic impact path/to/repo src/models.py       # what this change implicates
aic fanout path/to/repo                     # blast radius across every file
```

`index` is the interesting one — run it twice. The graph lives in
`<repo>/.aic/graph.db`.

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
  *within one function*. It does not follow taint across calls, so a source read
  in one function and passed to a sink in another is missed. Cross-file sources
  (`request.GET`, `os.environ`) are not modelled as sources at all. The corpus
  above exists to gate that work; the design is in DESIGN.md §7.
- **Call resolution is name-based**, constrained to targets visible through the
  caller's imports. Python's dynamic dispatch is not statically decidable, so
  this over-approximates — the safe direction for a filter, which may flag too
  much but never too little. Without the import constraint the closure saturates
  at ~64% of all functions and every probe returns the same answer; with it,
  `security` lands at 8.6%. Closing the remaining gap needs real type inference.
- **Blast radius is file-granular.** Function-granular impact is the same work as
  inter-procedural taint, and would most help files inside a large import cycle —
  exactly where the current approach is weakest.
- **Python only.**

### What this is not

~2,200 lines of dependency-free Python (plus tests). It is a working argument
about a cost model, not a security product.

- **Not a SAST engine.** The sink list is short and hand-written by design; a
  long noisy one would make the probe useless as a filter. Anything real needs a
  maintained rule corpus, a vulnerability database, and language coverage that
  is not one language.
- **Not a reachability product.** File-granular impact is a coarse instrument
  next to function-level reachability, and the gap is inter-procedural analysis
  plus type inference — which is the expensive part, and is stated above as a
  known limit rather than hidden.
- **Not benchmarked against a real vulnerability corpus.** Both corpora here are
  hand-authored. They are useful for regression and for telling dataflow apart
  from grep; they are not evidence of field precision.
- **Not multi-tenant, not authenticated, not hardened.** The MCP server reads a
  local graph and answers questions about it.

The transferable parts are the incremental engine, the probe seam, and the
measurement discipline — the blast-radius distribution, the corpora, and the
before/after numbers on every claim.

<a name="lineage"></a>
## Lineage

The dirty-propagation model comes from
[Graph-based AI Compiler](https://www.tdcommons.org/dpubs_series/8241/) (Rhodes
Floyd Davis Jr., Technical Disclosure Commons, June 2025, CC-BY 4.0) — a codebase
graph where marking a node dirty recursively invalidates its dependents, and a
separate pass resolves dirty nodes until the graph is clean.

That disclosure describes generation: a node holds authored intent and is
"collapsed" into code. AIC runs the same machinery in the analysis direction —
nodes hold extracted facts, and the fixpoint being sought is verification rather
than generation. Design notes, the reasoning behind the rewrite, and every
measurement are in [DESIGN.md](DESIGN.md).

## Acknowledgements

Inspired by ideas from [@rodydavis](https://github.com/rodydavis).

## License

MIT — see [LICENSE](LICENSE).
