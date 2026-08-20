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
pip install "aic-graph @ git+https://github.com/nan-bit/aic.git"
```

**Not on PyPI, deliberately.** Install from git or from a clone (`pip install
-e .`); there is no `pip install aic-graph`. Uploading to an index would promise
a maintained package, and the [limits](#limits) below are the honest version of
what this is. `aic-graph` is only the distribution name — `aic` belongs to an
unrelated project there — and everything you type is still `aic`. No
dependencies, Python 3.9+.

```bash
aic index .        # first run:  parses everything
aic index .        # second run: stat-diffs, finds nothing changed
```

The second run is the whole argument. The measured version of it is in
[bench/RESULTS.md](bench/RESULTS.md): on Django's 883 files, **3.2 s to index and
71 ms to confirm nothing changed**.

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
| first index, one time | 3.2 s | full parse + probes + taint dataflow |
| re-index, nothing changed | 71 ms | `index`, stat-diffs the tree, finds no work |
| one file changed, median | 65.3 ms | `touch`, reparse one file, propagate |

That last row is a median because absorbing an edit was timed for every one of
the 883 files rather than sampled. The spread is narrow: 57.1 ms at the cheapest,
85.8 ms at the 90th percentile, and the cheapest is **79% of the mean**. On a tree
this size an edit costs about the same whichever file you touch.

**The spread is not a blast-radius effect**, which is what this section claimed
for a long time on the strength of two files timed by hand. Absorbing an edit
reparses and re-probes the file that changed, then sets a flag on each dependent,
and setting a flag is nearly free. Cost tracks the size of the edited file:
Spearman +0.94, +0.95, +0.96 and +0.84 against size on requests, flask, celery
and sqlalchemy, against -0.18, +0.10, +0.15 and +0.27 for blast radius.

Django's own files make the point. `utils/functional.py` reaches **588** files,
more than anything else in the repo, and absorbs an edit in **70.3 ms**.
`db/models/query.py` reaches **571**, fewer, and takes **118.0 ms**. The wider
one is cheaper.

A stateless scanner cannot tell any of them apart. It does identical full work
every time.

Worth separating two things that get conflated: **a diff is what you changed;
blast radius is what your change reached.** A one-line edit to
`db/models/query.py` is a one-line diff and a 571-file impact. Only the second
needs a graph.

**Placement beats precision.** Meta ran Infer at diff time and offline with the
same analyzer and got **over 70%** fix rates versus near-zero, because a diff is
*attributable* to whoever caused it. A coding agent mid-task is a smaller unit
with that same property — but only if re-verifying it is cheap. That is the
result this is chasing.
([Distefano et al., CACM 2019](https://cacm.acm.org/research/scaling-static-analyses-at-facebook/).)

## Blast radius predicts payoff before you deploy

*The number that generalizes.* Computed for every file, not two hand-picked ones:

| package | files | median | p90 | max | mean | largest import cycle |
|---|---:|---:|---:|---:|---:|---:|
| requests 2.32.3 | 18 | 6 | 9 | 14 | 6.0 | 1 (6%) |
| flask 3.0.3 | 24 | 22 | 22 | 23 | 19.5 | 19 (79%) |
| celery 5.4.0 | 158 | 6 | 89 | 123 | 41.2 | 34 (22%) |
| sqlalchemy 2.0.36 | 255 | 40 | 245 | 248 | 127.1 | 121 (47%) |
| django 5.2.16 | 883 | **3** | 571 | 588 | **140.4** | 162 (18%) |

Median far below mean is the finding on everything but Flask: **most changes are
cheap to verify, a minority are catastrophic, and the average tells you nothing
about either.** The expensive minority sit inside the largest import cycle, which
for Django is 18% of the repo where incremental analysis buys little and 82%
where it buys nearly everything.

Flask is the exception, and it is worth keeping in the table rather than dropping
for tidiness. Its median of 22 sits *above* its mean of 19.5 because 79% of the
package is inside one import cycle: almost every file reaches almost every other,
so there is no cheap majority to find. A graph tells you that too, and it is the
answer that says incremental analysis will not help here.

It is also a scheduling input. When verification costs money and latency, this
says which edits deserve the expensive pass. Full results and the flask outlier:
[bench/RESULTS.md](bench/RESULTS.md) (`python bench/run.py` to reproduce).

**Click it instead of reading it.** [`viz/blast-radius.html`](viz/blast-radius.html)
is the same five graphs as one page: every file is a square, and clicking one
lights up the files a change to it would reach, in waves, one wave per import
hop. Self-contained — no server, no build step, no network. Regenerate with
`python viz/export.py`.

## Probes

*The seam that keeps this general.* **A probe decides what is _interesting_; the
engine decides what is _affected_.** Everything downstream of a probe —
reachability, dirty propagation, blast radius — is probe-agnostic.

| probe | marks | answers | Django selectivity |
|---|---|---|---:|
| `security` | dangerous sinks, hardcoded credentials, and — via dataflow — sinks a parameter actually reaches | what did I put at risk? | 4.4% |
| `api` | public functions and methods | whose contract might I have broken? | 83.6% |
| `tests` | test functions | what do I have to re-run? | 0.3% |

They select very differently, which is how you know the seam is real rather
than one question wearing a general-purpose costume. Adding one means implementing a
single `inspect()` method and registering it in `aic/probes/__init__.py`. There
is deliberately no plugin discovery and no config DSL — generalize on the fourth
probe, not the second.

The seam is also where this stops being about any one question. The expensive
machinery a probe would want next is *a per-function fact, computed once,
invalidated by dirty propagation, composed along call edges to a fixpoint.*
Taint is one instantiation of that; it is not the only one:

| instantiation | fact per function | answers |
|---|---|---|
| taint | does a parameter reach a sink | what did I put at risk |
| test reachability | which tests transitively exercise this | what must I re-run |
| API propagation | does a signature change reach a public entry point | whose contract did I break |
| effects | does this do IO, mutate global state, block | is this safe to call from here |

Fixpoint, cycle condensation, persistence and invalidation are shared; only the
lattice and the transfer functions differ.

**The honest cost.** The platform claim is thinner than that table suggests.
`security` is the only probe with dataflow behind it; `api` and `tests` mark
nodes and stop. Test selection is the obvious second consumer, and arguably the
better first one: its ground truth is objective and free — run the suite, see
what fails — and it *falsifies* the seam rather than asserting it.

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

Both err in the safe direction for a filter. Neither is production. The corpus
itself — source, expected JSON and a description per case — is in
[`tests/fixtures/interproc/`](tests/fixtures/interproc/); its structure follows
[PyCG](https://arxiv.org/abs/2103.00587)'s micro-benchmark suite and
SecuriBench Micro's discipline of annotating benign flows, which are what
actually discriminate between analyzers.

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

One measurement would settle it faster than more reasoning: the false-edge
*rate* in the current call graph, not just the unresolved-call rate. The corpus
records what the graph cannot resolve; it does not record what the graph
resolves wrongly. That number is not in this repo, and it is the one that says
whether summaries would be building on sand.

Current lean: real sources first regardless — `os.environ`, `sys.argv`,
`input()` — since they are cheap, independent of the fixpoint, and address all
four genuine false negatives. Then measure the false-edge rate. Then decide.

## Use it with an agent

The CLI pays ~110 ms of interpreter startup per invocation, more than the
analysis itself, so the agent-facing surface is a resident MCP server.

```bash
pip install "aic-graph[mcp] @ git+https://github.com/nan-bit/aic.git"
aic-mcp /path/to/repo        # speaks MCP over stdio
```

The `mcp` extra needs Python 3.10+ and pulls 27 transitive dependencies, which
is why it is an extra — `aic` itself has none. The server is pinned to v2 of the
SDK, but that is a build-time pin, not a wire one: an SDK v1 client connects
fine and negotiates an older protocol revision.

Point any MCP client at that command:

```json
{ "mcpServers": { "aic": { "command": "aic-mcp", "args": ["/path/to/repo"] } } }
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
against a purpose-built 9-file sandbox, the agent found and called the tools off
their descriptions alone — the prompt never mentioned impact analysis, it asked
for a code change and ended with "tell me what else in this repo my change could
have put at risk." The agent picked probes deliberately (`api` to check a rename
had not broken callers, `tests` for what to re-run), and separated pre-existing
findings from ones its own diff caused. Its reasoning was visibly grounded in
the output: *"models.py is imported by 7 of 9 files, so it has the widest blast
radius in the repo."*

One session also exposed a bug that the test suite, 156 tests today, did not.
`aic_review` was called three times with different probes; the first returned
14 findings and the next two returned **zero**, while `aic_impact` on the same
file returned 7. Two tools, same scope, contradictory answers. Cause: `refresh` called
`mark_clean_all()` on every invocation, so DIRTY meant "dependents of the most
recent change set" — correct for a one-shot CLI run, wrong for a resident server
where the second call's no-op refresh erased what the first established. Scope
collapsed from 7 files to 1, and the server reported that a change to a hub
module reached nothing: a false negative, the expensive kind. The bug was
unreachable from the CLI, invisible to the whole suite, and took one live agent
loop to surface.

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
dependency. A working argument about a cost model, not a finished product.

Beyond the two over-approximations above:

- **Python only, for now.** The `ast` module is the ceiling. Multi-language means
  tree-sitter, which is the point at which a compiled host would earn its keep.
- **Blast radius is file-granular.** Function granularity is the same work as
  inter-procedural taint.

The sink list is short by design and blast radius is coarse next to
function-level. The transferable parts are the incremental engine, the probe
seam, and the measurement discipline — not the analysis.

## Record

Kept because the deltas are the interesting part.

**What the first version got wrong.** v1 produced a lossy skeleton for *reading*
— a symbol table, not a compiler — and `mark_dirty()` wrote `status='DIRTY'` that
nothing ever read. The disqualifying part was that the representation could not
distinguish a problem from its fix. These two inputs produced byte-identical
output, because decorators were dropped entirely:

```python
@requires_admin
def delete_all():        vs        def delete_all():
    db.drop()                          db.drop()
```

Which also makes `@login_required` and `@app.route("/admin")` invisible.
Module-level assignments were never visited; signatures lost annotations,
defaults, `*args` and `**kwargs`; deleted files were never evicted; and nothing
carried a line number, so no finding could cite a location. v2 keeps the engine
and changes what a node's payload is. What was *not* a problem, measured so
effort did not go there: compression was real (87%) and performance was fine.

**Numbers that moved.** Cold vs. warm index was 1432 ms vs. 87 ms on Django
before the taint pass and the mtime pre-filter; now 2.6 s vs. ~50 ms. The
security probe's selectivity went 4.4% → 8.6% → 4.4%: the taint pass doubled the
reachable set and the prose was corrected upward to match, and that 8.6% turned
out to be inflated by a bug — the dataflow pass judged sinks on the bare name, so
every `json.loads` counted as a deserialization sink. With that fixed it is 4.4%
again, arrived at honestly. Twice now the generated numbers were right before the
prose was.

**Why it is still Python.** Go is the right answer for a *product* — single
static binary, tree-sitter bindings, cheap concurrency — and the wrong answer for
demonstrating an argument, where velocity beats distribution and the cold-index
cost is nowhere near binding. Revisit when multi-language support forces
tree-sitter. Fair warning for whenever that happens: tree-sitter parse tables are
enormous (`tree-sitter-c-sharp`'s `parser.c` is ~32 MB), so ship precompiled
grammars for a fixed set of languages rather than vendoring the corpus.

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

## Acknowledgements

Inspired by ideas from [@rodydavis](https://github.com/rodydavis), whose
[implementation of the same disclosure](https://github.com/rodydavis/aic) runs
the arrow the other way.

## License

MIT — see [LICENSE](LICENSE).
