# AIC — Design Notes

Why the thing is shaped the way it is, and what was measured rather than assumed.
Organised by topic; the historical record is at the back in §6.

| | |
|---|---|
| [1. Thesis](#1-thesis) | what this is, and the disclosure it comes from |
| [2. Architecture](#2-architecture) | representation, engine, surfaces, and the probe seam |
| [3. What is measured](#3-what-is-measured) | blast radius, two taint corpora, dogfooding |
| [4. Positioning](#4-positioning) | why diff-time wins, and what agents can actually do |
| [5. Roadmap and gates](#5-roadmap-and-gates) | what is done, what is next, what unlocks it |
| [6. Record](#6-record) | v1's defects, language choice, superseded numbers |

---

## 1. Thesis

`aic` is built from [Graph-based AI Compiler](https://www.tdcommons.org/dpubs_series/8241/)
(Rhodes Floyd Davis Jr., Technical Disclosure Commons, 17 Jun 2025, CC-BY 4.0), which
describes a codebase graph plus dirty propagation:

> "A node of the graph is collapsed to code when all its dependencies are collapsed as
> well. A prompt is converted to code by collapsing each node to code."

> "A node can be marked as dirty, and doing so causes a recursive update of all of its
> sources as dirty. Marking a node as dirty does not generate any new code but is
> important to enable multiple batched changes. A separate process collapses the dirty
> nodes in multiple passes till the graph is free of dirty nodes."

That is Make/Bazel with an LLM as the build rule. In the disclosure a node's source of
truth is **authored intent** and code is the compiled artifact; the fixpoint being sought
is generation.

**AIC runs the same machinery with the arrow reversed.** Nodes hold extracted facts, and
the fixpoint sought is verification. Dirty propagation, dependency edges and
drain-until-clean are identical; the payload and the direction differ.

The v1 of this repo did not implement that. It produced a lossy skeleton for *reading* —
a symbol table, not a compiler — and `mark_dirty()` wrote `status='DIRTY'` that nothing
ever read. v2 keeps the engine and changes what a node's payload is. See [§6.1](#61-what-v1-got-wrong).

---

## 2. Architecture

### 2.1 Representation

Per-file facts plus two graphs.

Facts retain what a signature skeleton destroys — decorators, module-level assignments,
annotated parameter lists, return annotations — with a line number on every node. Nothing
is summarised into a lossy string; an LLM-facing projection, if it ever comes back, is a
*view* over these facts rather than the storage format.

- **Import graph**, resolved exactly (no suffix matching), unresolved imports counted
  rather than guessed.
- **Call graph**, name-based, constrained to targets visible through the caller's imports.

**Not built: control flow and data flow as a unified graph.** A true Code Property Graph
(Yamaguchi et al.) unifies AST + CFG + PDG, and that is what makes real taint analysis
expressible. What exists is the AST layer, coarse call/import edges, and a per-function
CFG built on demand for the taint pass.

That gap is the single largest source of imprecision and it shows up twice: impact is
file-granular, and call resolution over-approximates within the import-visible set.

Still deferred, and worth doing before any contract work:

- Stable node IDs independent of path, so renames don't churn the graph.
- External/stdlib imports retained as first-class nodes rather than only counted.

### 2.2 The engine

```
file changes
  → recompute node hash
  → mark node dirty
  → recursively mark dependents dirty (reverse edges)
  → drain the dirty set
  → repeat until no dirty nodes remain
```

For the analysis application this *is* incremental re-analysis: the dirty set is exactly
the code whose properties may have changed. Nothing else is re-examined.

Two things this required that v1 lacked: eviction of deleted nodes, and a `status` that
something actually reads.

### 2.3 Two surfaces, one query layer

`aic/query.py` computes and returns data. `aic/surfaces/` renders it. Neither surface holds
analysis logic, which is what lets them differ as much as they do:

| | `surfaces/cli.py` | `surfaces/mcp.py` |
|---|---|---|
| caller | a human evaluating or debugging | a coding agent mid-task |
| cost per call | ~110ms interpreter start | amortised; the process is resident |
| dependencies | none, Python 3.9+ | ~13 transitive, Python 3.10+ |
| output | formatted for a terminal | ranked, truncated, token-budgeted |

Once the MCP server landed, the obvious tidy-up was to delete the CLI and have one way in.
Deliberately not doing that, for four reasons:

1. **It is how the claim gets verified.** `aic index` run twice — 2.6s then 51ms — is the
   thirty-second version of the entire argument, and it needs no agent, no client and no
   protocol. Someone assessing whether this is worth their time should not have to stand
   up an MCP client to find out.
2. **Dropping it would make the dependency story worse.** The MCP SDK pulls ~13 transitive
   dependencies and needs 3.10+. CLI-less means the only way to ask about import edges
   drags in pydantic, starlette and uvicorn.
3. **It is the debugging surface.** When the server misbehaves you need to ask the same
   question with no protocol in the way. That is exactly how the [§3.3](#33-dogfooding-the-mcp-server)
   bug was confirmed — reproduced against `query.py` directly, in six lines.
4. **Deleting it would pre-answer an open question.** [§2.4](#24-keeping-the-platform-probe-agnostic)
   notes the guidance that a CLI beats an MCP server for known, deterministic operations,
   and the MCP work was set up so "would the CLI have been enough?" could come back *yes*.
   Removing the comparison arm to tidy the surface would decide that by fiat.

`bench/run.py` times `query.refresh` directly rather than the CLI — benchmarking through a
printer meant redirecting stdout, and made the numbers depend on a presentation layer.
Nothing outside `surfaces/` and its tests depends on either surface now, which is the
property that matters: they are *surfaces*, not dependencies.

### 2.4 Keeping the platform probe-agnostic

Can the rest of the roadmap ship without the engine turning into a security product?
Yes, and the seam already exists: **a probe decides what is _interesting_; the engine
decides what is _affected_.** Everything downstream of a probe is probe-agnostic today,
and `cpg.py` is explicitly policy-free — sources, sanitizers and sinks are supplied by the
caller. Three places where that could quietly stop being true:

**1. Summaries with taint vocabulary.** "Source summary / sink summary / TITO" is Pysa's
taint-specific naming. The abstraction is not: *a per-function fact, computed once,
invalidated by dirty propagation, composed along call edges to a fixpoint.* Taint is one
lattice; others fall out of the same machinery:

| instantiation | fact per function | answers |
|---|---|---|
| taint | does a parameter reach a sink; does it reach the return | what did I put at risk |
| test reachability | which tests transitively exercise this | what must I re-run |
| API propagation | does a signature change reach a public entry point | whose contract did I break |
| effects | does this do IO, mutate global state, block | is this safe to call from here |

So stage 4 should extend `cpg`'s existing pattern: a `SummaryPolicy` alongside
`TaintPolicy`, with `probes/security.py` supplying the taint instantiation the way it
already supplies the taint policy. Fixpoint, SCC condensation, persistence and
invalidation are shared; only the lattice and transfer functions differ.

**2. Contracts named for security.** Ship `Contract(kind=...)`. See [§4.2](#42-contracts).

**3. The tool surface.** Currently clean: `aic_review(probe=...)`, findings shaped as
`kind`/`detail` rather than as vulnerabilities. `probe="security"` as a default is
defensible — it is the probe with dataflow behind it — but it must stay a *default*, never
an assumption. What to refuse is a tool named `aic_vulnerabilities`; the moment the surface
names a domain, the platform claim is gone.

**The honest cost.** The platform claim is thinner than the probe table suggests.
`security` is the only probe with dataflow behind it; `api` and `tests` mark nodes and
stop. If stage 4 serves taint alone, one probe gets an inter-procedural engine and the
other two stay grep-with-extra-steps.

The cheap fix is to make **test selection** the second consumer of stage 4, possibly the
first. Its ground truth is objective and free (run the suite, see what fails); it is the
use case Google and Meta actually deploy; it needs no source/sink modelling, just
reachability with a trivial lattice; and it *falsifies* the seam rather than asserting it.
The counter-argument is that taint is the harder, more differentiated problem — true, but
the differentiator claimed here is the stateful incremental layer, not the analysis, and
test selection exercises that layer just as hard for far less verification cost.

---

## 3. What is measured

### 3.1 Blast radius

Django's median is 3 files, its mean is 140. The gap is the 162-file import cycle. This is
the single most useful number the tool produces, because it predicts per-repo payoff
*before* deployment — and it is a scheduling input, not just a benchmark: when verification
costs money and latency, the distribution says which edits deserve the expensive pass.

Full distributions for five pinned packages: [bench/RESULTS.md](bench/RESULTS.md).

One related finding worth keeping: naive name-based call resolution saturated at **64%** of
functions and made every probe return the same answer. Constraining calls to import-visible
targets brought `security` to 8.6%. Precision there is load-bearing, not a refinement.

### 3.2 Taint: two corpora

**Intra-procedural** (`tests/fixtures/taint_cases.py`, 19 cases): 1.00 precision / 1.00
recall, false negatives gated to zero in CI. On Django the dataflow pass clears 33% of
heuristic sinks as static (256 → 171). Cost: cold index +72% (1.5s → 2.6s), paid once;
incremental unchanged, since taint runs only on reparsed files.

**Inter-procedural** (`tests/fixtures/interproc/`, 45 cases in 9 categories, 25 tainted /
20 safe). Structure follows [PyCG](https://arxiv.org/abs/2103.00587)'s micro-benchmark
suite — source + expected JSON + description, one execution path per case — and
SecuriBench Micro's discipline of annotating benign flows, which are what actually
discriminate between analyzers. Baseline:

```
TP=21 FN=4 FP=16 TN=4  precision=0.57 recall=0.84
call-graph ceiling: 24/25 flows (96%) have a resolvable call path
```

**Read this the right way round.** Recall of 0.84 is not evidence the engine half-works
inter-procedurally — it does not work inter-procedurally at all. The current policy treats
*every parameter* as attacker-controlled, so it flags any function whose parameter reaches
a sink regardless of what is ever passed. That accidentally satisfies most tainted cases
while failing 16 of 20 safe ones. The blindness shows up as **over-approximation, not
omission**, which inverts the earlier assumption that stage 4 would buy *coverage*: its job
on this corpus is **precision**, holding recall.

The four genuine misses need real sources (`os.environ`, `sys.argv`, `input()`) or taint
stored on `self` — not propagation. `sources/request_get` passes only because `request`
happens to be a parameter.

The single unresolvable flow is `dispatch_ambiguity/duck_typed_param`: a callable passed as
an argument and invoked. Higher-order flow is the same limitation PyCG names for its own
recall loss.

**Do not read 96% as the call-graph ceiling in the field.** These fixtures are minimal by
construction, which flatters resolution. PyCG reports ~69.9% recall on real packages and
its resolution is better than aic's. 96% is the ceiling on a friendly corpus.

### 3.3 Dogfooding the MCP server

Three headless Claude Code sessions (`claude -p --mcp-config`) against a purpose-built
9-file sandbox with a hub module (7 of 9 files depend on it), two pre-existing injection
sinks, and a leaf. The agent got the three MCP tools plus Read/Edit/Grep/Glob and nothing
else. ~$1.19, 67 turns.

**The tools get used without being asked for.** Run 1's prompt never mentioned aic or
impact analysis — it asked for a code change and ended with "tell me what else in this repo
my change could have put at risk." The agent called the tools off their descriptions alone,
and its reasoning is visibly grounded in the output: *"models.py is imported by 7 of 9
files, so it has the widest blast radius in the repo."* It then separated pre-existing
findings from ones its own diff caused — the attribution property [§4.1](#41-the-evidence)
argues for, appearing unprompted.

**The probe axis is used deliberately.** It picked `api` to check a rename hadn't broken
callers, `tests` for what to re-run, `security` for risk.

**Call distribution: `aic_review` 7, `aic_impact` 3, `aic_overview` 0.** The obvious reading
is that overview should go — but three runs on one synthetic repo is not a sample that
justifies deleting a tool, and all three were *edit-then-verify* tasks, precisely the shape
with no use for an orientation call. Keeping it, on the expectation that its moment is the
start of a session on an unfamiliar repo. `.aic/mcp-calls.jsonl` accumulates the evidence
for free.

**Response sizes: 262–1,870 bytes**, against Django's 3.8 kB worst case and a 25k-token cap.

#### The bug this found

Run 2 called `aic_review` three times with different probes. The first returned 14 findings;
the next two returned **zero**, while `aic_impact` on the same file returned 7. Two tools,
same scope, contradictory answers.

Cause: `refresh` calls `mark_clean_all()` on every invocation, so DIRTY means "dependents of
the most recent change set". Correct for a one-shot CLI run, wrong for a resident server,
where the *second* call's no-op refresh erased the dependents established by the first.
Scope collapsed from 7 files to 1, and the server reported that a change to a hub module
reached nothing — a false negative, the expensive kind.

Fixed by deriving review's scope from the session's own seed files rather than the stored
flag. Re-running the identical scenario: `api` 14 → 14, `tests` 0 → 2, `security` 0 → 7,
with `security` now agreeing with `aic_impact`. Regression tests pin the call sequence.

Worth stating plainly: **the bug was unreachable from the CLI, invisible to 70 passing
tests, and took one live agent loop to surface.** No amount of taint precision would have
mattered while the server answered "nothing to re-check" for a change to a hub file.

#### Known limitation

`aic_review` reports nothing on a cold start. If no graph exists when the server begins, the
first call indexes the whole repo and everything is baseline rather than change — correct,
but it means edits made *before* the server started are invisible to `review`. Run 1 hit
this and fell back to `aic_impact`. Worth a warmer message than "Nothing changed since this
server started", which reads as reassurance when it should read as "I have no baseline".

---

## 4. Positioning

### 4.1 The evidence

**Diff-time beats everything else, and it isn't close.** Meta ran Infer two ways with the
same analyzer and same false-positive rate: batch/offline deployment produced a near-zero
fix rate; diff-time bot comments produced **over 70%**
([Distefano et al., CACM 2019](https://cacm.acm.org/research/scaling-static-analyses-at-facebook/)).
A diff supplies two things nothing else does — the developer already has mental context
loaded, and the finding is *attributable* to the change that caused it.

**IDE-time was tried and failed.** Google's Tricorder paper is explicit that tools which
"displayed results too early, while developers were still experimenting with their code in
the editor" fell out of use, alongside those that reported too late. Their FindBugs CLI was
used by 35 developers in all of 2014, 20 of them once
([Sadowski et al., ICSE 2015](https://research.google/pubs/tricorder-building-a-program-analysis-ecosystem/)).
They enforce a hard rule: >10% not-useful puts an analyzer on probation, >25% turns it off.

**Implication.** "Continuous vs. gate" is the wrong axis. The winning property is
**attribution** — the smallest unit of change that can be blamed on someone who currently
cares. Leaving the PR gate requires a different unit with that same property. A coding
agent mid-task is one.

**What agents can and cannot do:**

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
([SecLLMHolmes](https://arxiv.org/abs/2312.12575)) — precisely the failure mode that trips
Google's 25% rule.

**Conclusion: do not build a discovery engine.** Build the thing that works.

### 4.2 Contracts

Make a node's payload a **contract** — an obligation attached to a code unit ("this
function receives untrusted input; it must not reach a SQL sink unparameterized"). This is
the slot the disclosure fills with a prompt.

Division of labour:

- **The graph** determines which contracts a change could invalidate. Deterministic, cheap,
  no hallucination.
- **Dirty propagation** re-opens exactly those obligations and nothing else.
- **The agent** only ever discharges a *specific, localized, pre-identified* obligation —
  the 34–90% task, never the 3.5% one.

This recovers attribution without the PR gate: the unit of blame is the invalidated
contract, which — unlike a diff — knows *why* it matters and can be checked the moment a
file is saved.

**Name it `Contract(kind=...)`, not `SecurityContract`.** Nothing about the mechanism is
security-specific. "This function is public API and must keep its signature" and "this
module must not import from that layer" are the same shape. Retrofitting generality onto a
security-named type is the kind of thing that never gets done.

### 4.3 Open risk

Authoring contracts is real work. If they drift from the code they degrade into the
"repository overview" that [ETH's AGENTS.md study](https://arxiv.org/abs/2602.11988) found
does not improve task success while adding >20% inference cost. Contracts must be
**derivable and verifiable**, not hand-maintained prose.

Pysa is the cautionary evidence: a mature production analyzer that still requires
hand-written `.pysa` model files and a `taint.config` to declare sources and sinks. Even
with full inter-procedural machinery, the specification stays human-authored. Derived by
default with human override, or not at all.

This is the biggest unresolved question in the design.

---

## 5. Roadmap and gates

**Shipped.**

| | |
|---|---|
| Facts + graphs | decorators, module assignments, annotated signatures, line numbers throughout |
| Dirty propagation | `status` is read, deleted files evicted, one-file edit reparses one file |
| Probe seam | three probes at 8.6% / 83.6% / 0.3% selectivity on Django |
| Benchmarks | five pinned PyPI packages, blast radius for every file |
| Incremental path | `touch` skips the walk; mtime+size pre-filter took warm re-index 87ms → 50ms |
| CPG stages 1–3 | per-function CFG + worklist taint engine, policy supplied by the probe |
| MCP server | three read-only tools, lazy stat-diff, ranked and truncated output |
| Inter-procedural corpus | 45 cases, baseline recorded ([§3.2](#32-taint-two-corpora)) |

**Next: inter-procedural summaries (stage 4).** The corpus gate is cleared. Order of work:

1. **Real sources** — `request.GET`, `os.environ`, `sys.argv`, `input()`. Cheap, independent
   of the fixpoint, and it addresses all four genuine false negatives. Do this first and let
   the corpus say how much of the gap it closes alone.
2. **A generic summary framework** — `SummaryPolicy`, per [§2.4](#24-keeping-the-platform-probe-agnostic).
   Fixpoint over the SCC condensation, reusing `analyze.strongly_connected`; re-analyze only
   functions whose callees' summaries changed, as Pysa does. Summaries persist in a new
   table — the first payload expensive enough to be worth storing, and the thing that
   finally gives dirty propagation something to invalidate besides a status flag.
3. **The taint instantiation**, measured against the corpus *and* the call-graph ceiling.

Invalidation should follow **Reviser** (Arzt & Bodden, ICSE 2014): clear-and-propagate,
where affected nodes are those transitively reachable from changed nodes in the updated
graph. Their two findings that matter here — over-approximating the affected set is always
*safe*, and computing a precise affected set can cost more than recomputing — plus the
measured result: up to **80% savings versus full recomputation with identical results**.
That is the closest published analogue to what this project claims.

*Exit gate:* precision/recall reported alongside the ceiling. Do not ship a stage-4 number
without the corpus behind it.

**Then: contracts** ([§4.2](#42-contracts)) — only after stage 4, and only if [§4.3](#43-open-risk)'s
derivation question has an answer.

**Deliberately not doing.** A filesystem watcher: the stat-diff is taken on demand instead,
because ~50ms per tool call is cheaper than a daemon thread and a debounce policy for a cost
that was never binding. A Go rewrite: revisit when multi-language support forces
tree-sitter ([§6.2](#62-language-choice)).

---

## 6. Record

### 6.1 What v1 got wrong

v1 produced a lossy skeleton for reading. The disqualifying part: the representation could
not distinguish a vulnerability from its fix. These two inputs produced byte-identical
output, because decorators were dropped entirely —

```python
@requires_admin
def delete_all():        vs        def delete_all():
    db.drop()                          db.drop()
```

— which makes `@login_required`, `@app.route("/admin")` and `@csrf_exempt` invisible.
Module-level assignments were never visited, so hardcoded secrets vanished;
`cur.execute("SELECT ... " + uid)` and `pickle.loads(blob)` both collapsed to
`# CALLS: <name>`; signatures lost annotations, defaults, `*args` and `**kwargs`; deleted
files were never evicted; external imports were discarded, which is backwards for security
since that is where sinks live; and nothing carried a line number, so no finding could cite
a location.

What was *not* a problem, measured so effort didn't go there: compression was real (87%),
and performance was fine (505 files in 0.6s).

### 6.2 Language choice

**Stay on Python for now.** Deliberately revisited after v2 shipped. Go is still the right
answer for a *product* — single static binary, tree-sitter bindings, cheap concurrency — and
the wrong answer for demonstrating an argument, where velocity beats distribution and the
cold-index cost is nowhere near binding.

Revisit when: multi-language support is required (Python's `ast` stops here, which forces
tree-sitter, which is where a compiled host earns its keep); or distribution to non-Python
users matters.

**Grammar warning for whenever that happens.** tree-sitter parse tables are enormous:
`tree-sitter-c-sharp`'s `parser.c` is ~32MB (~5.3MB compiled); some grammars need >20GB RAM
to build. Ship precompiled grammars for a fixed set of 6–8 languages; do not vendor the full
corpus.

### 6.3 Superseded numbers

Kept so the deltas are traceable.

- Cold vs. warm index was **1432ms vs 87ms** on Django before the taint pass and the mtime
  pre-filter. Now **2.6s vs ~50ms**.
- The security probe was reported at **4.4%** selectivity in prose that predated the taint
  pass; adding `tainted-*` markers roughly doubled the reachable set. The real figure is
  **8.6%**, and `bench/RESULTS.md` — being generated — had been right all along.
- Stage 4 was framed as buying *coverage*. The corpus baseline says it buys *precision*;
  see [§3.2](#32-taint-two-corpora).
