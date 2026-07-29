# AIC

*AI Compiler — the same graph model, with the arrow reversed.*

> **I changed this file. What else do I now need to look at?**

A stateless analyzer redoes everything on every run. That is fine at PR time,
where it happens once. It is the wrong cost model inside an agent loop, where the
agent touches forty files before it stops to think. So AIC keeps a persistent
graph and makes the question incremental: do the expensive work once, then on
every change redo only what the change actually reached.

**The engine is language-agnostic; the parser is not — yet.** Three of thirteen
modules import Python's `ast`; the store, the query layer, the propagation and
the surfaces never see one. Runs on Python 3.9+ with no dependencies.

---

## Quickstart

```bash
pip install -e .

aic index .        # first run:  parses everything
aic index .        # second run: stat-diffs, finds nothing changed
```

On this repo — 74 files — that is **99 ms, then 2 ms**. The second run is the
whole argument, and the ratio is what travels: on Django's 883 files it is 2.6 s,
then 51 ms.

Everything else, runnable against this repo as written:

```bash
aic impact . aic/query.py                # what a change here implicates
aic review . --probe security            # what everything you changed implicates
aic fanout .                             # blast-radius distribution
aic status . --probe security --top 5    # what the graph holds
aic touch  . aic/query.py                # invalidate one file, no repo walk
```

The graph lives in `<repo>/.aic/graph.db`. Use `--db PATH`, or `AIC_DB_DIR=DIR`
to keep one file per repo outside the tree entirely, when analysing something you
cannot write to.

## The cost model

On Django (883 files, 9,213 functions):

| | | measured by |
|---|---:|---|
| first index, one time | 2.6 s | full parse + probes + taint dataflow |
| re-index, nothing changed | 51 ms | `index` — stat-diffs the tree, finds no work |
| one file changed | 14–60 ms | `touch` — reparse one file, propagate |

That last row is the product, and it is a range rather than a number because the
blast radius is:

| edited file | dependents invalidated | recheck cost |
|---|---:|---:|
| `contrib/gis/db/backends/mysql/schema.py` | **1** | 14 ms |
| `db/models/query.py` | **570** | 60 ms |

A stateless scanner cannot tell those apart. It does identical full work either way.

Worth separating two things that get conflated: **a diff is what you changed;
blast radius is what your change reached.** A one-line edit to
`db/models/query.py` is a one-line diff and a 571-file impact. Only the second
needs a graph.

**Placement beats precision.** Meta ran Infer at diff time and offline with the
same analyzer and got **over 70%** fix rates versus near-zero, because a diff is
*attributable* to whoever caused it. A coding agent mid-task is a smaller unit
with that same property — but only if re-verifying it is cheap. That is the
result this is chasing. ([Evidence and sources](DESIGN.md#41-the-evidence).)

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

## Probes

*Why this isn't a security tool.* **A probe decides what is _interesting_; the
engine decides what is _affected_.** Everything downstream of a probe —
reachability, dirty propagation, blast radius — is probe-agnostic.

| probe | marks | answers | Django selectivity |
|---|---|---|---:|
| `security` | dangerous sinks, hardcoded credentials, and — via dataflow — sinks a parameter actually reaches | what did I put at risk? | 4.4% |
| `api` | public functions and methods | whose contract might I have broken? | 83.6% |
| `tests` | test functions | what do I have to re-run? | 0.3% |

They select very differently, which is how you know the seam is real rather than
a security tool wearing a platform costume. Adding one means implementing a
single `inspect()` method and registering it in `aic/probes/__init__.py`. There
is deliberately no plugin discovery and no config DSL.

## Where it's soft

`security` is just the probe I took furthest, to show the seam holds. It has real
machinery behind it — a per-function CFG and a worklist taint engine, with taint
tracked *per sink kind* rather than as one bit, because sanitizing is
kind-specific (`shlex.quote` makes a value safe for a shell and does nothing at
all for SQL). On Django that clears nearly half the heuristic sinks as static
(256 → 135).

It also has **two over-approximations, and they compound.**

**1. Taint is intra-procedural.** It does not follow taint across calls. On a
45-case inter-procedural corpus:

| corpus | cases | precision | recall |
|---|---:|---:|---:|
| intra-procedural | 30 | 1.00 | 1.00 |
| **inter-procedural** | **45** | **0.58** | **0.84** |

The recall only looks good by accident. Every parameter is currently treated as
attacker-controlled, so the engine flags any function whose parameter reaches a
sink regardless of what is ever passed. That is over-flagging, not cleverly
catching — the blindness shows up as false positives, not omissions.

The 1.00 row is a sanity check on a hand-authored corpus, not a real
vulnerability benchmark. Neither corpus is one.

**2. Call resolution is name-based**, constrained to targets the caller's file can
see through its imports. So the call graph carries false edges. Unconstrained it
is far worse — the closure saturates at 64% of functions and every probe returns
the same answer — but constrained is not the same as correct.

Both err in the safe direction for a filter. Neither is production.
[Baseline and categories](DESIGN.md#32-taint-two-corpora).

## The open question

The honest next step is **function summaries**, so taint survives across calls.
That is what fixes over-approximation 1.

But summaries ride on the call graph. Precise taint flowing along false edges
does not just cap what can be found — it manufactures *cleaner* false positives,
and makes bad precision look like good precision. Tightening the analysis on top
of an untightened graph could easily make the numbers prettier and the tool
worse.

So the sequencing is a genuine call, and I don't think the evidence in this repo
settles it:

- **Call resolution first** — the graph bounds everything built on it, and a
  summary framework built against false edges validates against a moving target.
- **Summaries first** — the corpus says the current failure is precision, and
  real sources plus summaries address all four genuine false negatives directly.
  Call-graph precision needs type inference, which is the expensive part.

If this were real, which would you do first? That is the call I'd want an
engineer's read on. My current lean and the case for each side:
[DESIGN.md §5](DESIGN.md#5-roadmap-and-gates).

## Use it with an agent

The CLI pays ~110 ms of interpreter startup per invocation, more than the
analysis itself, so the agent-facing surface is a resident MCP server.

```bash
pip install -e ".[mcp]"
claude mcp add aic -- aic-mcp /path/to/repo
```

| tool | answers |
|---|---|
| `aic_review` | what everything changed since the baseline put at risk — the checkpoint call before declaring work done |
| `aic_impact` | the same question for one named file |
| `aic_overview` | how far changes travel in this repo, and where they stop being cheap |

All read-only. Every call stat-diffs the tree first (~50 ms on Django) and
reparses only what moved, so there is no index step and no hook to install —
and since the baseline `review` measures from lives in the graph rather than in
the server, that holds *across* sessions too, not just within one.

**On MCP going stateless.** The 2026-07-28 revision dropped the initialize
handshake and the protocol session, which sounds like an argument against the
paragraph at the top of this file. It is the opposite, and the distinction is
the point: MCP dropped *protocol* state, which for a read-only analyzer was
ceremony that cost routing flexibility and bought nothing. AIC keeps *derived*
state, which is the expensive thing and already lived in a file rather than in a
connection. Derived state can be rebuilt instead of shared — and how cheap
rebuilding is, is the measurement this whole repo is about.

The binding constraint turned out to be response size, not speed: Django's
`db/models/query.py` reaches 571 files, which is useless to return. The count
goes in the summary and the body carries the ranked intersection with what the
probe marks — 20 findings and 4.0 kB for the worst file in the repo, against a
25k-token cap. Surface benchmarks: [bench/SURFACES.md](bench/SURFACES.md).

**It works on agents that were not told about it.** In three headless sessions
the agent found and called the tools off their descriptions alone, chose probes
deliberately, and separated pre-existing findings from ones its own diff caused.
One session also exposed a bug that 70 passing tests did not — a resident server
erasing its own review scope, unreachable from a CLI where every invocation is a
fresh process doing one thing.
[Write-up](DESIGN.md#33-dogfooding-the-mcp-server).

## How it works

1. **Parse** every file to facts — functions, calls, decorators, module-level
   assignments, annotations, all with line numbers. Nothing is summarized away.
2. **Resolve imports exactly.** No suffix matching; unresolved imports are
   counted, never guessed.
3. **Run probes** to mark interesting nodes.
4. **On change:** hash-diff the tree, reparse what moved, evict what was deleted,
   propagate DIRTY through reverse import edges.
5. **On query:** intersect the invalidated set with the probe's reachable set.

Steps 4 and 5 are incremental compilation — a dependency graph, dirty-marking on
change, and a pass that re-resolves dirty nodes until the graph is clean — run in
the analysis direction. That is the arrow the subtitle is about; see
[Lineage](#lineage).

## Limits

~2,500 lines across thirteen modules, of which only `surfaces/mcp.py` has a
dependency. A working argument about a cost model, not a security product.

Beyond the two over-approximations above:

- **Python only, for now.** The `ast` module is the ceiling. Multi-language means
  tree-sitter, which is the point at which a compiled host would earn its keep.
- **Blast radius is file-granular.** Function granularity is the same work as
  inter-procedural taint.

Not a SAST engine — the sink list is short by design — and not a reachability
product, since file-granular is coarse next to function-level. The transferable
parts are the incremental engine, the probe seam, and the measurement discipline.

<a name="lineage"></a>
## Lineage

The name and the dirty-propagation model come from [Graph-based AI
Compiler](https://www.tdcommons.org/dpubs_series/8241/) (Rhodes Floyd Davis Jr.,
Technical Disclosure Commons, June 2025, CC-BY 4.0) — a codebase graph where
marking a node dirty recursively invalidates its dependents, and a separate pass
resolves dirty nodes until the graph is clean.

That disclosure describes **generation**: a node holds authored intent and is
"collapsed" into code, with an LLM as the build rule. AIC runs the same machinery
in the **analysis** direction — nodes hold extracted facts, the fixpoint sought is
verification rather than generation, and there is no model in the loop at all.

Design notes, every measurement, and the reasoning behind each decision are in
[DESIGN.md](DESIGN.md).

## Acknowledgements

Inspired by ideas from [@rodydavis](https://github.com/rodydavis), whose
[implementation of the same disclosure](https://github.com/rodydavis/aic) runs
the arrow the other way.

## License

MIT — see [LICENSE](LICENSE).
