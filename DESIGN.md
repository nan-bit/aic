# AIC v2 — Design Notes

Status: v2 shipped. Records why the rewrite happened and what was measured.

## 1. The thesis we're actually implementing

`aic` v1 was built from [Graph-based AI Compiler](https://www.tdcommons.org/dpubs_series/8241/)
(Rhodes Floyd Davis Jr., Technical Disclosure Commons, 17 Jun 2025). Re-reading the
disclosure, v1 implements the *reverse arrow* from what the paper describes.

The paper:

> "A node of the graph is collapsed to code when all its dependencies are collapsed as
> well. A prompt is converted to code by collapsing each node to code."

In the paper, a node's source of truth is **authored intent** (a prompt / natural-language
description). Code is the *compiled artifact*. Edges are imports. The engine is dirty
propagation:

> "A node can be marked as dirty, and doing so causes a recursive update of all of its
> sources as dirty. Marking a node as dirty does not generate any new code but is
> important to enable multiple batched changes. A separate process collapses the dirty
> nodes in multiple passes till the graph is free of dirty nodes."

That is Make/Bazel with an LLM as the build rule, including hermetic per-node compilation
(different nodes may use different models) and a watch daemon.

What v1 implements is code → lossy skeleton, for *reading*. That's a symbol table, not a
compiler. It is a legitimate thing to build, but it is not the paper's mechanism, and the
paper's central mechanism is present in v1 in name only: `mark_dirty()` writes
`status='DIRTY'` and **nothing ever reads it**. There is no collapse pass, no fixpoint
loop, no daemon.

v2 keeps the dirty-propagation engine and changes what a node's payload is. See §5.

## 2. Verified defects in v1

All confirmed by execution, not inspection.

### 2.1 The representation cannot distinguish a vulnerability from its fix

These two inputs produce byte-identical skeletons:

```python
@requires_admin                    def delete_all():
def delete_all():          vs          db.drop()
    db.drop()

# both →   def delete_all():
#              # CALLS: drop
```

Decorators are dropped entirely (`RichSkeletonizer` has no `visit_*` for them). This is
disqualifying for any security use: `@login_required`, `@app.route("/admin")`, and
`@csrf_exempt` are invisible.

Other losses measured against realistic patterns:

| Pattern | Skeleton output |
|---|---|
| `cur.execute("SELECT ... " + uid)` | `# CALLS: execute` |
| `pickle.loads(blob)` | `# CALLS: loads` |
| `requests.get(u, verify=False)` | `# CALLS: get` |
| `AWS_SECRET = "AKIA..."` (module level) | *(empty — no output at all)* |

Module-level assignments are never visited, so hardcoded secrets and config constants
vanish. `visit_FunctionDef` never calls `generic_visit`, so function-body imports
(`import pickle` inside a function) never register as dependencies.

### 2.2 Signatures lose the type information the README promises

`_handle_func` reads only `node.args.args`. Annotations, defaults, `*args`, `**kwargs`,
and keyword-only args are all discarded:

```
def delete_user(user_id: int, *, force: bool = False, **kwargs) -> dict[str, int]
  →  def delete_user(user_id) -> dict[...]
```

The return-type reconstruction hand-rolls an approximation of `ast.unparse` because the
package targets Python 3.8 (EOL Oct 2024).

### 2.3 Correctness bugs

- `skeleton.py:19` interpolates `node.module` when it is `None`, emitting the literal
  string `from None import db` for `from . import db`.
- Deleted files are never evicted. Removing a file and re-indexing leaves it in the DB as
  `CLEAN`; agents are served skeletons of code that no longer exists.
- No ignore-file support. `os.walk` skips only `.aic`, `__pycache__`, `.git`, so
  `node_modules/` and `.venv/` are indexed despite being in `.gitignore`.
- External imports are discarded (`cli.py:113`). For security this is backwards —
  `import subprocess`, `from django.db import connection` are where sinks live.
- No line numbers anywhere. Nothing anchors back to source, so no finding can cite a
  location.
- `tests/test_imports` asserts nothing and ends in `pass`, with a comment identifying the
  `from . import db` bug. A known defect documented in a test that cannot fail.

### 2.4 What is *not* a problem

Measured, so we don't waste effort here:

- **Compression is real**: 87% reduction on aic's own source (README claims ~90%).
- **Performance is fine**: 505 files in 0.6s, despite opening ~1500 SQLite connections.
  The connection-per-call pattern in `db.py` is untidy but not the bottleneck. Do not
  rewrite for speed.

## 3. Language choice

**Decision: stay on Python for now.** Deliberately revisited after v2 shipped.

The original plan was Go (single static binary, tree-sitter bindings, cheap
concurrency). That is still the right answer for a *product*. It is the wrong answer
for the current goal, which is to demonstrate an argument: velocity beats distribution
while the thesis is still being tested, and the measured cold-index cost (1.4s for
Django's 883 files) is nowhere near being the constraint. Rewriting would buy startup
time and packaging, neither of which is currently binding.

Revisit when one of these becomes true:
- Multi-language support is required. Python's `ast` stops here; that forces tree-sitter,
  and tree-sitter is where a compiled host earns its keep.
- The watcher lands and per-edit latency needs to go below ~10ms.
- Distribution to non-Python users matters.

**Grammar warning for whenever that happens.** tree-sitter parse tables are enormous:
`tree-sitter-c-sharp`'s `parser.c` is ~32MB (~5.3MB compiled); some grammars need >20GB
RAM to build. Ship precompiled grammars for a fixed set of 6-8 languages; do not vendor
the full corpus.

## 4. Representation

**Built:** per-file facts plus two graphs.

Facts retain what v1 destroyed -- decorators, module-level assignments, annotated
parameter lists, return annotations -- with a line number on every node. Nothing is
summarized into a lossy string; the LLM-facing skeleton, if it comes back, is a
*projection* over these facts rather than the storage format.

Graphs:
- **Import graph**, resolved exactly (no suffix matching), with unresolved imports
  counted rather than guessed.
- **Call graph**, name-based, constrained to targets visible through the caller's
  imports.

**Not built: control flow and data flow.** A true Code Property Graph (Yamaguchi et al.)
unifies AST + CFG + PDG, and that is what makes real taint analysis expressible. What
exists here is the AST layer plus coarse call/import edges.

That gap is the single largest source of imprecision, and it shows up in the numbers
twice: impact is file-granular (so a change inside an import cycle implicates 570 files
on Django), and call resolution over-approximates within the import-visible set. Both
would shrink materially with dataflow edges.

Still deferred, and worth doing before any contract work:
- Stable node IDs independent of path, so renames don't churn the graph.
- External/stdlib imports retained as first-class nodes rather than only counted.

## 5. The engine: dirty propagation to fixpoint

This is the paper's mechanism, finally implemented.

```
file changes
  → recompute node hash
  → mark node dirty
  → recursively mark dependents dirty (reverse edges)
  → worker pool drains the dirty set in passes
  → repeat until no dirty nodes remain
```

For the SAST application this *is* incremental re-analysis: the dirty set is exactly the
set of code whose security properties may have changed. Nothing else gets re-examined.

Requirements v1 lacks: transactional batch writes, eviction of deleted nodes, a
`status` that something actually reads, and a watch mode.

## 6. Security contracts — the SAST positioning

### 6.1 The evidence that shaped this

**Diff-time beats everything else, and it isn't close.** Meta ran Infer two ways with the
same analyzer and same false-positive rate: batch/offline deployment produced a near-zero
fix rate; diff-time bot comments produced **over 70%**
([Distefano et al., CACM 2019](https://cacm.acm.org/research/scaling-static-analyses-at-facebook/)).
Their explanation is that a diff supplies two things nothing else does — the developer
already has mental context loaded, and the finding is *attributable* to the change that
caused it.

**IDE-time was tried and failed.** Google's Tricorder paper is explicit that tools which
"displayed results too early, while developers were still experimenting with their code in
the editor" fell out of use, alongside those that reported too late. Their FindBugs CLI
was used by 35 developers in all of 2014, 20 of them once
([Sadowski et al., ICSE 2015](https://research.google/pubs/tricorder-building-a-program-analysis-ecosystem/)).
They enforce a hard rule: >10% not-useful rate puts an analyzer on probation, >25% turns
it off.

**Implication.** "Continuous vs. gate" is the wrong axis. The winning property is
**attribution** — the smallest unit of change that can be blamed on someone who currently
cares. Leaving the PR gate requires a different unit with that same property.

**What agents can and cannot do.** From the benchmark literature:

| Task | Measured ceiling |
|---|---|
| Patch a known, localized vuln | 34–90% ([BountyBench](https://arxiv.org/abs/2505.15216), [SEC-bench](https://arxiv.org/abs/2506.11791)) |
| Exploit a known vuln | 13–67% |
| **Discover a novel vuln** | **3.5–22%, frequently 0%** ([CyberGym](https://arxiv.org/abs/2506.02548)) |

Also: ~60% of agent-generated patches "work" but only **5–11% survive differential testing
against ground truth**
([AutoPatchBench](https://engineering.fb.com/2025/04/29/ai-research/autopatchbench-benchmark-ai-powered-security-fixes/)).
And LLM vulnerability judgment is brittle — **+26% error rate from merely renaming
variables**, with frequent false positives on *patched* code
([SecLLMHolmes](https://arxiv.org/abs/2312.12575)), which is precisely the failure mode
that trips Google's 25% rule.

**Conclusion: do not build a discovery engine.** Build the thing that works.

### 6.2 The design

Make a node's payload a **security contract** — an obligation attached to a code unit
("this function receives untrusted input; it must not reach a SQL sink unparameterized").
This is the slot the paper fills with a prompt.

Division of labor:

- **The graph** determines which contracts a change could invalidate. Deterministic,
  cheap, no hallucination.
- **Dirty propagation** re-opens exactly those obligations and nothing else.
- **The agent** only ever discharges a *specific, localized, pre-identified* obligation —
  the 34–90% task, never the 3.5% one.

This recovers attribution without the PR gate: the unit of blame is the invalidated
contract, which — unlike a diff — knows *why* it matters and can be checked the moment a
file is saved.

### 6.3 Open risk

Authoring contracts is real work. If they drift from the code they degrade into the
"repository overview" that [ETH's AGENTS.md study](https://arxiv.org/abs/2602.11988) found
does not improve task success while adding >20% inference cost. Contracts must be
**derivable and verifiable**, not hand-maintained prose. Deriving an initial contract set
from the CPG (taint sources/sinks are inferable) is the likely answer, with human edits as
overrides.

This is the biggest unresolved question in the design.

## 7. Status and phasing

**Done (v0.2.0):**

1. ~~Port index + graph with real facts and line spans, no LLM in the loop.~~
   Decorators, module-level assignments, and annotated signatures are retained;
   every node carries a line number.
2. ~~Dirty propagation.~~ `status` is queried, deleted files are evicted, and a
   one-file edit reparses one file. The paper's mechanism is live.
3. ~~Probe seam.~~ Three probes (`security`, `api`, `tests`) with measurably
   different selectivity: on Django 4.4% / 83.6% / 0.3% of functions reachable.
4. ~~Benchmarks.~~ Five pinned PyPI packages; blast-radius distribution computed for
   every file. See `bench/RESULTS.md`.

**Measured, worth carrying forward:**

- Cold vs. warm index is 1432ms vs 87ms on Django. The warm path is dominated by
  walking and hashing 883 files, *not* by analysis -- only one file is reparsed. A
  filesystem watcher removes that floor.
- Blast radius is strongly bimodal: Django's median is 3 files, its mean is 140. The
  gap is the 162-file import cycle. This is the single most useful number the tool
  produces, because it predicts per-repo payoff before deployment.
- Naive name-based call resolution saturated at ~64% of functions and made every probe
  return the same answer. Constraining calls to import-visible targets brought
  `security` to 4.4%. Precision is load-bearing, not a refinement.

**Also done:**

5. ~~Watcher path.~~ `aic touch <files>` reparses named files without walking the
   repo (~15ms analysis, peripheral file, Django). `index` gained an mtime+size
   pre-filter (`scandir`-based); warm re-index 87ms -> 50ms. mtime is treated as a
   hint, never truth -- a moved timestamp forces a hash, and `--rehash` forces a full
   content pass. **Finding: the tree walk (~45ms) is now the floor, and interpreter
   startup (~110ms) dominates `touch` wall-clock.** A resident process is therefore
   required to realize the gain, which promotes the MCP server up the list.
6. ~~CPG stages 1-3.~~ Real per-function CFG (`aic/cpg.py`) plus a worklist taint
   engine. The security probe supplies the policy (params = sources, SINKS, a narrow
   sanitizer set) and emits `tainted-*` markers only when a parameter reaches a sink.
   Ground-truth corpus (`tests/fixtures/taint_cases.py`, 19 cases): 1.00 precision /
   1.00 recall, false negatives gated to zero. On Django the pass clears 33% of
   heuristic sinks as static (256 -> 171). Cost: cold index +72% (1.5s -> 2.6s),
   paid once; incremental unchanged (taint runs only on reparsed files).

**The stage-4 decision (inter-procedural), now that stages 1-3 are measured:**

Intra-procedural precision is already 1.00 on the corpus, so stage 4 does not buy
*accuracy* on single functions -- it buys *coverage* of the real-world shape where a
source is read in one function and the sink is in another (`request.GET` -> helper ->
`execute`). That is the majority of actual web vulnerabilities, so the coverage gain
is large. Two cautions weigh against rushing it:

  - **Cost.** Stage 4 is summary computation plus a fixpoint over the call graph. On
    top of a call graph that is already name-based and over-approximate, summaries
    risk propagating that imprecision widely. Precision on a corpus of *inter*-
    procedural cases must be measured before trusting any number.
  - **Strategic.** Inter-procedural reachability precision is the exact IP a
    commercial reachability engine sells. Built here it is a learning exercise and a
    conversation piece, not a differentiator. The differentiator remains the stateful
    incremental layer (stages 5-6 above), which nothing off-the-shelf does.

Recommendation: build the MCP server next (realizes the watcher gain, and is the
honest agent-integration surface), then extend the taint corpus with inter-procedural
cases, then attempt stage 4 against that corpus. Do not ship a stage-4 number without
the corpus behind it.

7. **MCP server.** The interface a coding agent can query mid-session, and the only
   way to escape the ~110ms interpreter-startup tax measured above.
8. **Inter-procedural taint (stage 4)** -- summaries + call-graph fixpoint, gated on
   an inter-procedural corpus.
9. Contract representation (section 6) -- only after 7-8, and only if the derivation
   question in 6.3 has an answer.
