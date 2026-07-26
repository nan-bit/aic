# AIC — incremental impact analysis

**AIC** treats code analysis the way a build system treats compilation: do the
expensive work once, then on every change redo only what that change actually
touched. It keeps a persistent graph of the codebase and answers one question
incrementally —

> **I changed this file. What else do I now need to look at?**

Most analysis tools answer "everything," because they are stateless — they
rebuild their understanding of the repo on every invocation. That is fine at PR
time, when it happens once. It is the wrong cost model inside an agent loop,
where a coding agent might touch forty files before it stops to think. Forty
edits should not mean forty full scans.

## The cost model

*Why state is the whole point.* The evidence says placement beats precision:
Meta ran Infer at diff time and offline with the same analyzer and got **over
70%** fix rates versus near-zero, because a diff is *attributable* to whoever
caused it. A coding agent mid-task is a smaller unit with that same property —
but only if re-verifying it is cheap. ([Evidence and sources](DESIGN.md#41-the-evidence).)

On Django (883 files, 9,213 functions):

```
first index (one time)        2.6 s    full parse + probes + taint dataflow
re-index, nothing changed      51 ms   stat-diff finds no work
one file                    14–60 ms   reparse one file, propagate
```

That last number is the product — and it is not one number, because the blast
radius isn't:

| edited file | dependents invalidated | recheck cost |
|---|---:|---:|
| `contrib/gis/db/backends/mysql/schema.py` | **1** | 14 ms |
| `db/models/query.py` | **570** | 60 ms |

A stateless scanner cannot tell those apart; it does identical full work either
way.

Worth separating two things that get conflated: **a diff is what you changed;
blast radius is what your change reached.** A one-line edit to
`db/models/query.py` is a one-line diff and a 571-file impact. Only the second
needs a graph.

> **Why "compiler"?** The engine is incremental compilation — a dependency
> graph, dirty-marking on change, and a pass that re-resolves dirty nodes until
> the graph is clean — run in the analysis direction. See [Lineage](#lineage).

## Agent integration

*The surface the cost model exists for.* The CLI pays ~110 ms of interpreter
startup per invocation, more than the analysis itself, so the agent-facing
surface is a resident MCP server.

```bash
pip install -e ".[mcp]"
claude mcp add aic -- aic-mcp /path/to/repo
```

| tool | answers |
|---|---|
| `aic_review` | what the edits so far put at risk — the checkpoint call before declaring work done |
| `aic_impact` | the same question for one named file |
| `aic_overview` | how far changes travel in this repo, and where they stop being cheap |

All read-only. Every call stat-diffs the tree first (~50 ms on Django) and
reparses only what moved, so there is no index step, no hook to install, and a
missing graph is built on first call.

The binding constraint is response size, not speed. Django's `db/models/query.py`
reaches 571 files — a useless and expensive thing to return. So the count goes in
the summary and the body carries the ranked intersection with what the probe
marks, truncated with an explicit note of what was elided:

```console
$ aic_impact("db/models/query.py")
db/models/query.py is depended on by 571 of 883 files. 789 of 9213 functions
match the security probe and are worth re-checking (91.4% of a full scan
avoided). Showing 20 of 426; raise limit= or narrow with probe=.

core/cache/backends/filebased.py:38  [tainted-deserialization]  zlib.decompress(f.read()) -> pickle.loads
```

~3.8 kB for the worst file in the repo, against a 25k-token cap.

**Is the server actually faster?** Measured against the installed console scripts
over the real transport, asking the same question of the same graph:

| package | `aic index` + `impact` | MCP call | speedup |
|---|---:|---:|---:|
| requests | 71 ms | **9 ms** | 7.9× |
| django | 185 ms | **124 ms** | 1.5× |

A resident process removes ~60 ms of fixed process overhead per question, and
repays its own ~300 ms startup after roughly **5–7 questions**. The speedup shrinks
on large repos because the query itself starts to dominate — which says the next
optimization is the reachability computation, not the transport. Full tables and
the per-call breakdown: [bench/SURFACES.md](bench/SURFACES.md).

**It works on agents that were not told about it.** In three headless sessions
the agent found and called the tools off their descriptions alone, chose probes
deliberately, and separated pre-existing findings from ones its own diff caused.
One session also exposed a bug that 70 passing tests did not — the kind that is
unreachable from a CLI, where every invocation is a fresh process doing one
thing. [Write-up](DESIGN.md#33-dogfooding-the-mcp-server).

## Probes

*Why this isn't a security tool.* A probe decides what is *interesting*; the
engine decides what is *affected*. Everything downstream of a probe —
reachability, dirty propagation, blast radius — is probe-agnostic.

| probe | marks | answers |
|---|---|---|
| `security` | dangerous sinks, hardcoded credentials, and — via dataflow — sinks a parameter actually reaches | what did I put at risk? |
| `api` | public functions and methods | whose contract might I have broken? |
| `tests` | test functions | what do I have to re-run? |

They select very differently, which is how you know the seam is real rather than
a security tool wearing a platform costume: on Django, `security` reaches 4.4% of
functions, `api` 83.6%, `tests` 0.3%.

Adding one means implementing a single `inspect()` method and registering it in
`aic/probes/__init__.py`. There is deliberately no plugin discovery and no config
DSL.

## Security, one level deeper

*One probe taken further than the others, to show what the seam supports.* A
cheap heuristic pass marks dangerous call sites; a dataflow pass — real
per-function CFG plus a worklist taint engine — promotes a sink to `tainted-*`
only when a parameter actually reaches it, telling `cur.execute("... " + uid)`
apart from `cur.execute("SELECT 1")`. On Django that clears nearly half the
heuristic sinks as static (256 → 138). The engine is policy-free: sources,
sanitizers and sinks come from the probe.

Taint is tracked *per sink kind* rather than as one bit, because sanitizing is
kind-specific: `shlex.quote` makes a value safe to hand to a shell and does
nothing at all for SQL. A value is "still dangerous for {sql}", not simply
"tainted".

Two corpora, and the honest one is the second:

| corpus | cases | result |
|---|---:|---|
| intra-procedural | 30 | 1.00 precision / 1.00 recall |
| **inter-procedural** | **45** | **0.58 precision / 0.84 recall** |

Recall on the second is high *by accident* — every parameter is currently treated
as attacker-controlled, so the engine flags any function whose parameter reaches
a sink regardless of what is passed. Its inter-procedural blindness shows up as
over-approximation, not omission. That corpus is the gate on the next stage, and
it records a call-graph ceiling so future numbers are judged against what the
call graph can actually resolve rather than against 1.00. [Baseline and
categories](DESIGN.md#32-taint-two-corpora).

## Blast radius predicts payoff before you deploy

*The number that generalizes.* Computed for every file, not two hand-picked ones:

| package | files | median | p90 | max | mean | largest import cycle |
|---|---:|---:|---:|---:|---:|---:|
| requests 2.32.3 | 18 | 6 | 9 | 14 | 6.0 | 1 (6%) |
| celery 5.4.0 | 158 | 6 | 89 | 123 | 41.2 | 34 (22%) |
| sqlalchemy 2.0.36 | 255 | 40 | 245 | 248 | 127.1 | 121 (47%) |
| django 5.2.16 | 883 | **3** | 571 | 588 | **140.4** | 162 (18%) |

Median far below mean is the finding: **most changes are cheap to verify, a
minority are catastrophic, and the average tells you nothing about either.** The
expensive minority sit inside the largest import cycle — for Django, 18% of the
repo where incremental analysis buys little and 82% where it buys nearly
everything.

It is also a scheduling input. When verification costs money and latency, this
says which edits deserve the expensive pass. Full results and the flask outlier:
[bench/RESULTS.md](bench/RESULTS.md) (`python bench/run.py` to reproduce).

## CLI

*The human surface.* Same query layer as the MCP server, no dependencies, Python
3.9+.

```bash
pip install -e .
aic index  path/to/repo      # build or update the graph — run it twice
aic impact path/to/repo src/models.py
aic fanout path/to/repo      # blast-radius distribution
aic status path/to/repo --probe security --top 5
aic touch  path/to/repo src/models.py    # invalidate one file, no repo walk
```

`index` twice is the thirty-second version of the whole argument.

The graph lives in `<repo>/.aic/graph.db` by default, which is right for your own
checkout and wrong for a tree you do not own. `--db PATH` puts it anywhere;
`AIC_DB_DIR=DIR` keeps one file per repository outside the tree entirely, so a
read-only export or CI checkout can be analysed without being written to.

## How it works

```
aic/
  analyze.py     parse to facts; import graph, call graph, SCCs, fanout
  cpg.py         per-function CFG + worklist taint engine (policy-free)
  store.py       SQLite: hashes, edges, markers, CLEAN/DIRTY
  query.py       the shared API — computes, never prints
  probes/        what counts as interesting: security, api, tests
  surfaces/      how you ask: cli.py (human), mcp.py (agent)
```

1. **Parse** every file to facts — functions, calls, decorators, module-level
   assignments, annotations, all with line numbers. Nothing is summarized away.
2. **Resolve imports exactly.** No suffix matching; unresolved imports are
   counted, never guessed.
3. **Run probes** to mark interesting nodes.
4. **On change:** hash-diff the tree, reparse what moved, evict what was deleted,
   propagate DIRTY through reverse import edges.
5. **On query:** intersect the invalidated set with the probe's reachable set.

### Known approximations

- **Taint is intra-procedural** — it does not follow taint across calls, and
  cross-file sources (`request.GET`, `os.environ`) are not modelled as sources.
- **Call resolution is name-based**, constrained to import-visible targets. It
  over-approximates, which is the safe direction for a filter. Unconstrained, the
  closure saturates at 64% of functions and every probe returns the same answer.
- **Blast radius is file-granular.** Function granularity is the same work as
  inter-procedural taint.
- **Python only.**

### What this is not

~2,200 lines of dependency-free Python. A working argument about a cost model,
not a security product: not a SAST engine (the sink list is short by design), not
a reachability product (file-granular is coarse next to function-level), not
benchmarked against a real vulnerability corpus (both corpora are hand-authored).

The transferable parts are the incremental engine, the probe seam, and the
measurement discipline.

<a name="lineage"></a>
## Lineage

The dirty-propagation model comes from [Graph-based AI
Compiler](https://www.tdcommons.org/dpubs_series/8241/) (Rhodes Floyd Davis Jr.,
Technical Disclosure Commons, June 2025, CC-BY 4.0) — a codebase graph where
marking a node dirty recursively invalidates its dependents, and a separate pass
resolves dirty nodes until the graph is clean.

That disclosure describes generation: a node holds authored intent and is
"collapsed" into code. AIC runs the same machinery in the analysis direction —
nodes hold extracted facts, and the fixpoint sought is verification rather than
generation.

Design notes, every measurement, and the reasoning behind each decision are in
[DESIGN.md](DESIGN.md).

## Acknowledgements

Inspired by ideas from [@rodydavis](https://github.com/rodydavis).

## License

MIT — see [LICENSE](LICENSE).
