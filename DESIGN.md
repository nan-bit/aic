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

> **Read with §7.9 and §10.** The evidence in 6.1 still stands and is the reason
> for the whole design. The *naming* in 6.2 does not: a contract is an obligation
> attached to a node and re-opened by dirty propagation, and nothing about that
> mechanism is security-specific. Ship it as `Contract(kind=...)`. This section is
> kept as written because it records why the SAST framing was chosen first, not
> because "security contract" is the intended type.

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
   different selectivity: on Django 8.6% / 83.6% / 0.3% of functions reachable.
4. ~~Benchmarks.~~ Five pinned PyPI packages; blast-radius distribution computed for
   every file. See `bench/RESULTS.md`.

**Measured, worth carrying forward:**

- Cold vs. warm index is 1432ms vs 87ms on Django. The warm path is dominated by
  walking and hashing 883 files, *not* by analysis -- only one file is reparsed. A
  filesystem watcher removes that floor.
  *(Superseded. Cold is now 2.6s, since the taint pass landed -- see item 6. Warm
  is ~50ms after the mtime pre-filter, and the watcher was not built: §7.7 takes
  the stat-diff on demand instead, because 50ms per tool call is cheaper than a
  daemon thread and a debounce policy for a cost that was never binding.)*
- Blast radius is strongly bimodal: Django's median is 3 files, its mean is 140. The
  gap is the 162-file import cycle. This is the single most useful number the tool
  produces, because it predicts per-repo payoff before deployment.
- Naive name-based call resolution saturated at ~64% of functions and made every probe
  return the same answer. Constraining calls to import-visible targets brought
  `security` to 8.6%. Precision is load-bearing, not a refinement.

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

Recommendation **(taken -- see 7 and 8 below)**: build the MCP server next
(realizes the watcher gain, and is the honest agent-integration surface), then
extend the taint corpus with inter-procedural cases, then attempt stage 4 against
that corpus. Do not ship a stage-4 number without the corpus behind it.

7. ~~**MCP server.**~~ Shipped. `aic/query.py` holds the computation, `aic/mcp.py`
   is a thin stdio adapter over it, and `aic/cli.py` is now only a printer. Three
   read-only tools -- `aic_review`, `aic_impact`, `aic_overview` -- each of which
   stat-diffs the tree before answering, so there is no index step and no hook to
   forget. The ~110ms startup tax is gone: the client spawns the server once per
   session.

   The binding constraint turned out to be response size, not latency. A change
   to Django's `db/models/query.py` reaches 571 files; that list is worthless to
   an agent and expensive to carry. What ships instead is the count plus the
   ranked intersection with what the probe marks -- dataflow-confirmed findings
   first, then by blast radius of the containing file, truncated with an explicit
   note of what was elided. Measured on Django: **3.8 kB (~940 tokens) for the
   worst file in the repo**, against Claude Code's 25k-token response cap, in
   ~130ms wall.

   Deliberately thin, because it is expected to be rewritten: MCP's 2026-07-28
   revision removes the initialize handshake and protocol sessions, and the Python
   SDK's v2 replaces the server core. Built against SDK v1 because that is what
   clients negotiate today -- verified by round-tripping a real client, which
   agreed on `2025-11-25`. Migration should cost one file that contains no
   analysis logic.

   Whether an agent reaches for this at all is answered in §9. It does.

8. **Inter-procedural summaries (stage 4).** ~~Gated on an inter-procedural
   corpus.~~ The corpus exists: `tests/fixtures/interproc/`, 45 cases in 9
   categories, 25 tainted / 20 safe, with a recorded baseline (§8).

   Three things changed about this step now that the gate has been cleared:

   - **The job is precision, not coverage.** The framing above said stage 4
     "buys *coverage*". The baseline says otherwise -- 0.57 precision against
     0.84 recall. Because every parameter is treated as attacker-controlled, the
     engine already satisfies most tainted flows by accident and fails 16 of 20
     safe ones. Success is holding recall while precision climbs, and the safe
     cases are the whole measurement.
   - **The remaining misses are source modelling, not propagation.** All four
     genuine false negatives need `os.environ` / `sys.argv` / `input()` as real
     sources, or taint stored on `self`. Modelling sources is cheap and
     independent of the summary fixpoint; do it first, and the corpus will say
     how much of the gap it closes on its own.
   - **The scope is generic summaries, not taint summaries.** See §10.

   Order of work: real sources → generic summary framework → taint instantiation
   → measure against the corpus and the call-graph ceiling together.

9. **Contracts (section 6)** -- still last, still gated on 8, but two things are
   now settled that were open when §6 was written:

   - **Drop "security" from the name.** §6 frames a contract as a security
     obligation. Nothing about the mechanism is security-specific: a contract is
     an obligation attached to a node, checkable, and re-opened by dirty
     propagation. "This function is public API and must keep its signature" and
     "this module must not import from that layer" are the same shape. Ship it as
     `Contract(kind=...)` from the start; retrofitting generality onto a
     security-named type is the kind of thing that never gets done.
   - **The delivery vehicle exists and is proven.** §6.2's argument was that
     contracts recover attribution without a PR gate. The dogfooding runs (§9)
     showed an agent doing exactly that unprompted -- separating "pre-existing"
     from "caused by my diff" from impact output alone. That is the attribution
     property showing up empirically, before any contract exists, which is
     encouraging for the design and also raises the bar: contracts have to beat
     what plain impact output already gets for free.

   §6.3's derivation question remains the blocker. Pysa is the cautionary
   evidence: a mature production analyzer that still needs hand-written `.pysa`
   models and a `taint.config`. Derived-by-default with human override, or not at
   all.

## 8. Inter-procedural baseline

Measured before stage 4 exists, so the "after" number has something to be
compared against. `pytest tests/test_interproc.py -rX -s`:

```
inter-procedural corpus: 45 cases (25 tainted / 20 safe)
  TP=21 FN=4 FP=16 TN=4  precision=0.57 recall=0.84
  call-graph ceiling: 24/25 flows (96%) have a resolvable call path
```

**Read this the right way round.** Recall of 0.84 is not evidence the engine
half-works inter-procedurally -- it does not work inter-procedurally at all. The
current policy treats *every parameter* as attacker-controlled
(`probes/security.py:seed_names`), so it flags any function whose parameter
reaches a sink regardless of what is ever passed. That accidentally satisfies
most tainted cases while failing 16 of 20 safe ones. The engine's real
inter-procedural blindness shows up as **over-approximation, not omission** --
which is the opposite of what §7's framing ("the coverage gain is large")
implied, and worth correcting: stage 4's job on this corpus is to fix
**precision**, while holding recall.

The four genuine misses are exactly the cases that need real sources rather than
propagation -- `os.environ`, `sys.argv`, `input()` -- plus taint stored on `self`
in one method and sunk in another. `sources/request_get` passes only because
`request` happens to be a parameter.

The single unresolvable flow is `dispatch_ambiguity/duck_typed_param`: a callable
passed as an argument and invoked. Higher-order flow is the same limitation PyCG
names for its own recall loss.

**Do not read 96% as the call-graph ceiling in the field.** These fixtures are
minimal by construction (one execution path per case), which flatters resolution.
PyCG reports ~69.9% recall on real packages and its resolution is better than
aic's. Ninety-six percent is the ceiling on a friendly corpus; the field number
is lower and unmeasured.

## 9. Dogfooding the MCP server

Three headless Claude Code sessions (`claude -p --mcp-config`) against a
purpose-built 9-file sandbox package with a hub module (7 of 9 files depend on
it), two pre-existing injection sinks, and a leaf. The agent got the three MCP
tools plus Read/Edit/Grep/Glob and nothing else. Total ~$1.19, 67 turns.

**The tools get used without being asked for.** Run 1's prompt never mentioned
aic, impact analysis, or the server -- it asked for a code change and ended with
"tell me what else in this repo my change could have put at risk." The agent
called `aic_review`, then `aic_impact` twice, off the tool descriptions alone,
and the reasoning in its answer is visibly grounded in the output: *"models.py is
imported by 7 of 9 files, so it has the widest blast radius in the repo."* It
then separated pre-existing findings from ones its own diff caused -- the
attribution property §6.1 argues is what makes a finding actionable.

**The probe axis is used deliberately, not decoratively.** Unprompted, the agent
picked `api` to check a rename hadn't broken callers, `tests` for what to re-run,
and `security` for risk. That is the seam working as designed, and it is
evidence the three probes are legible from their descriptions.

**Call distribution across the three runs: `aic_review` 7, `aic_impact` 3,
`aic_overview` 0.** `aic_overview` was never called once. The obvious reading is
that it should go -- but three runs on one synthetic 9-file repo is not a sample
that justifies deleting a tool, and the runs were all *edit-then-verify* tasks,
which is precisely the shape that has no use for an orientation call. Keeping it,
on the expectation that its moment is the start of a session on an unfamiliar
repo, which none of these runs exercised. Revisit with data from real sessions;
`.aic/mcp-calls.jsonl` accumulates it for free.

**Response sizes: 262-1,870 bytes.** Never close to a budget concern on a repo
this size, and Django's worst case is 3.8 kB. Output shaping is not the
constraint it was designed to be -- though that is partly because the design
worked.

### The bug this found

Run 2 called `aic_review` three times with different probes. The first returned
14 findings; the next two returned **zero**, while `aic_impact` on the same file
returned 7. Two tools, same scope, contradictory answers.

Cause: `refresh` calls `mark_clean_all()` on every invocation, so DIRTY means
"dependents of the most recent change set". That is correct for a one-shot CLI
run and wrong for a resident server, where the *second* tool call's no-op refresh
erased the dependents established by the first. Scope collapsed from 7 files to
1, and the server confidently reported that a change to a hub module reached
nothing -- a false negative, the expensive kind.

Fixed by deriving review's scope from the session's own seed files
(`analyze.propagate` over reverse import edges) rather than from the stored flag.
That accumulates across any number of edits and refreshes, and leaves CLI
behaviour untouched. Re-running the identical scenario: `api` 14 -> 14, `tests`
0 -> 2, `security` 0 -> 7, with `security` now agreeing with `aic_impact`.
Regression tests pin the exact call sequence that exposed it.

Worth stating plainly, because it is the argument for having built this before
stage 4: **the bug was unreachable from the CLI, invisible to 70 passing tests,
and took one live agent loop to surface.** No amount of taint precision would
have mattered while the server was answering "nothing to re-check" for a change
to a hub file.

### Known limitation

`aic_review` reports nothing on a cold start. If no graph exists when the server
begins, the first call indexes the whole repo, and everything is baseline rather
than a change -- correct, but it means edits made *before* the server started are
invisible to `review`. Run 1 hit exactly this and fell back to `aic_impact`. A
repo indexed ahead of time (or a session that outlives its first edit) does not
have the problem. Worth a warmer message than "Nothing changed since this server
started", which reads as reassurance when it should read as "I have no baseline".

## 10. Keeping the platform probe-agnostic

The question this answers: can the rest of the spec ship without the MCP server —
and the engine behind it — turning into a security product?

Yes, and the seam already exists. §6's own framing is the rule: **a probe decides
what is _interesting_; the engine decides what is _affected_.** Everything
downstream of a probe is probe-agnostic today, and `cpg.py` is explicitly
policy-free — sources, sanitizers and sinks are supplied by the caller, not baked
in. Stage 4 and contracts are where that could quietly stop being true, in three
specific places.

### Where the leak would happen

**1. Summaries with taint vocabulary.** "Source summary / sink summary / TITO" is
Pysa's taint-specific naming. The underlying abstraction is not: *a per-function
fact, computed once, invalidated by dirty propagation, composed along call edges
to a fixpoint.* Taint is one lattice. Others fall straight out of the same
machinery:

| instantiation | fact per function | answers |
|---|---|---|
| taint | does a parameter reach a sink; does it reach the return | what did I put at risk |
| test reachability | which tests transitively exercise this | what must I re-run |
| API propagation | does a signature change reach a public entry point | whose contract did I break |
| effects | does this do IO, mutate global state, block | is this safe to call from here |

So stage 4 should extend `cpg`'s existing pattern rather than break it: a
`SummaryPolicy` alongside `TaintPolicy`, with `probes/security.py` supplying the
taint instantiation the way it already supplies the taint policy. The fixpoint,
the SCC condensation, the persistence and the invalidation are shared; only the
lattice and transfer functions differ.

**2. Contracts named for security.** Covered in §7.9 — ship `Contract(kind=...)`.

**3. The tool surface.** Currently clean: `aic_review(probe=...)`, findings shaped
as `kind`/`detail` rather than as vulnerabilities. The soft commitment is
`probe="security"` as the default, which is defensible — it is the probe with
dataflow behind it — but it should stay a *default*, never an assumption. The
thing to refuse is a tool like `aic_vulnerabilities`; the moment the surface names
a domain, the platform claim is gone.

### The honest cost

The platform claim is currently thinner than the probe table makes it look.
`security` is the only probe with dataflow behind it; `api` and `tests` are
marker-only — they mark nodes and stop. If stage 4 serves taint alone, the gap
widens: one probe gets an inter-procedural engine and the other two stay
grep-with-extra-steps.

**The cheap fix is to make test selection the second consumer of stage 4, and
possibly the first.** The argument for going there before taint:

- **Ground truth is objective and free.** Run the suite, see what actually fails.
  No corpus to hand-author, no precision/recall argument, no judgement calls about
  what counts as a source.
- **It is the deployed use case.** Both Google and Meta run test selection at
  scale; `probes/tests.py` already says so.
- **It needs no source/sink/sanitizer modelling at all** — just call-graph
  reachability from tests to changed functions, which is the summary framework
  with a trivial lattice.
- **It falsifies the seam.** If the same summary machinery answers "what do I
  re-run" and "what did I put at risk", the platform claim is demonstrated rather
  than asserted — the same way the three probes' divergent selectivity (8.6% /
  83.6% / 0.3%) is what makes the probe seam credible today.

The counter-argument is that taint is the harder, more differentiated problem and
test selection is well-trodden. True — but the differentiator this project claims
is the *stateful incremental layer*, not the analysis, and test selection
exercises that layer just as hard while being far cheaper to verify.
