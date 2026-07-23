# The Penguin Autotuner — MCTS Engine and Search-Strategy Roster

> *All symbols, addresses, opcodes, and strings on this page are from `neuronx_cc` **2.24.5133.0+58f8de22**, cp310 wheel. The three modules documented here are Cython extensions, **not** native C++: `neuronxcc/starfish/penguin/MCTS.cpython-310-x86_64-linux-gnu.so` (the generic engine, BuildID `ae84fbba…`, 1,487,960 bytes, **with `.debug_info`, not stripped**), `…/penguin/targets/transforms/autotune/_TreeSearch.cpython-310-…so` (the layout subclass, BuildID `518d39ed…`, 746,744 bytes), and `…/autotune/_Search.cpython-310-…so` (the strategy family). Every function cited carries a Cython mangled symbol of the form `__pyx_pw_<module>_<Class>_<slot>_<method>` whose hex suffix is the `.text` offset; the interned `__pyx_n_s_*`/`__pyx_int_*` string-and-constant pool preserves identifiers and small integers verbatim, and `_Pyx_AddTraceback` calls embed the original `.py` source line numbers. cp311/cp312 are byte-identical rebuilds (same roster, different mangled hashes). Treat every address as version-pinned.*

## Abstract

The Penguin layout middle-end exposes an optional **compile-in-the-loop autotuner**, armed by `--layout-transform-heuristic=mcts`, that replaces the deterministic PAG static search with a **Monte-Carlo Tree Search** over layout designs. (The default `none` path and the legacy in-process `LayoutDecisionTree` MCTS are covered by the [layout middle-end page](../penguin/layout-middle-end.md); this page documents the *generic* MCTS machine and the *family of search strategies* that the autotuner can select among.) The autotuner is built in three Cython layers:

1. **`MCTS.so` — the generic UCT engine.** A problem-agnostic Monte-Carlo Tree Search implementing the textbook **select → expand → rollout → backpropagate** loop. Its child-scoring core is the **UCB1-for-trees (UCT)** formula `Q + c·sqrt(2·ln N_parent / n_child)`, isolated in a dedicated method `State.estimateReward`. Three problem hooks — `enumerateActions`, `takeAction`, `getReward` — are `NotImplementedError` stubs left for subclasses to override.

2. **`_TreeSearch.so` — the `layout/_TreeSearch` subclass.** `MCTSTuneState` specializes the engine for the layout-transform problem: an action is a layout **plan id**, a design is an ordered list of plan ids, and the **real reward** is a child-process compile that returns the scheduler's post-schedule estimated latency (`nc_latency`/`PostSchedEstLatency`). The driver `MonteCarloTreeSearch` re-derives an **adaptive** UCT exploration constant every iteration.

3. **`_Search.so` — the strategy roster.** A base `Search` interface plus **five native non-MCTS strategies** — `RandomSearch`, `ExhaustiveSearch`, `GreedySearch`, `ExhaustiveSequentialSearch`, `DownsampledSequentialSearch` — and an imported `MonteCarloTreeSearch`, all selectable by class-name string through the `Search.build` classmethod registry. The native strategies share one selection rule: `_find_best` = `argmin` over a latency metric.

This autotuner is **distinct from the backend PGA** ([`pga-feedback`](pga-feedback.md), in flight): PGA is a brute-force **8×8=64-cell grid search** over `(SB-size level, split threshold)` around the VN-Splitter; this is an **MCTS/UCB1 tree search** over layout-plan designs. They share the word "autotuner" and nothing else. The reward-loop orchestration that wraps `run()` (the per-iteration adaptive-`c` recompute, telemetry, and result commit) is the subject of the autotuner-orchestration page (in flight).

For reimplementation, the contract is:

- **The UCT engine** — node struct, the `estimateReward` UCB1 formula, `chooseBestChild` argmax, the `select`/`expand`/`rollout`/`backpropogate` loop, and the `MCTS.search` driver.
- **The layout subclass** — `MCTSTuneState`'s action space (plan ids), the per-node and per-rollout subprocess compiles, the subclass's UCT override, and the adaptive exploration constant.
- **The strategy roster** — the four flat/sequential strategies' loops, their byte-firm parameter defaults, the shared `_find_best` argmin, and the `Search.build` name→class registry.

| Property | Value |
|---|---|
| **Generic engine** | `MCTS.cpython-310-…so`, classes `Action` / `State` / `MCTS`; source `MCTS.py` (full DWARF line table) |
| **UCT formula** | `child.total_reward/child.num_visits + c·sqrt(2·ln(self.num_visits)/child.num_visits)` — `State.estimateReward` `@0x1fa30` |
| **Layout subclass** | `_TreeSearch.cpython-310-…so`, `MCTSTuneState` / `MCTSTuneAction` / `MonteCarloTreeSearch`; source `_TreeSearch.py` |
| **Real reward** | `ctx.metric.evaluate_with_subprocess_result(design)` → `default_metric` = `nc_latency` (`PostSchedEstLatency`) |
| **Strategy module** | `_Search.cpython-310-…so`, base `Search` + 5 native strategies + imported `MonteCarloTreeSearch` |
| **Registry** | `Search.build(cls, search_name, metric, config_search_settings, logger, baseline_metric)` `@0x210b0`, name→`cls.__subclasses__()` |
| **Selection rule (flat)** | `_find_best` `@0x12c80` = `candidates[metrics.index(min(metrics))]` — argmin (lower latency wins) |
| **Distinct from** | backend PGA grid search ([`pga-feedback`](pga-feedback.md)) — 8×8 grid, not MCTS |

> **Vtable note (binary-accurate).** These three modules are **Cython** extensions. There are **no C++ `_ZTV`/`_ZTI`/`_ZTS` symbols** in `_TreeSearch.so` (verified: `rg _ZTV …names.json` → empty). The "vtable" of `MCTSTuneState` is therefore a **CPython `PyTypeObject`** whose methods are wired through the `__pyx_mdef_*` `PyMethodDef` slot table (`__pyx_mdef_…_MCTSTuneState_15estimateReward`, `…_5takeAction`, etc.), not a C++ virtual-function table. The "two-VA-frame / `vptr = _ZTV + 0x10`" artifact that applies to native libwalrus vtables **does not apply here** — there is no `_ZTV` to offset from. Method dispatch is ordinary Python attribute resolution over the type's `tp_dict`/method table.

---

## 1. The generic UCT engine (`MCTS.so`)

`MCTS.py` was cythonized with a full DWARF line table, so every instruction maps back to a source line. The module defines three classes — `Action`, `State` (the MCTS node, 40+ methods), `MCTS` (the driver) — plus a `default_rng` shim over `numpy.random.Generator`. `State` carries the entire tree-policy machinery; the three problem-specific hooks are abstract.

### 1.1 The node struct — `State.__init__` `@0x196e0` (`MCTS.py:47–55`)

```c
// State.__init__(self, parent, action)  —  __pyx_pw_…_5State_1__init__  @0x196e0
// SetAttr stores, in decompile order.
self.parent        = parent;                    // n_s_parent
self.parent_action = action;                    // n_s_parent_action
self.num_visits    = 0;                          // __pyx_int_0           (decompile L236)
self.total_reward  = 0;                          // __pyx_int_0           (decompile L246)
self.best_reward   = -np.inf;                     // getattr(np,'inf') then PyNumber_Negative()
                                                  //   (decompile L273 getattr inf, L282 Negative)
self.children      = {};                          // PyDict_New()          (decompile L316) ★ a DICT
self.ops           = 0;                           // __pyx_int_0           (decompile L343)
```

A node is `{ parent, parent_action, num_visits:int, total_reward:num, best_reward:float(=-inf), children:dict, ops:int }`. `children` is a **dict** (action → child `State`), not a list — the `PyDict_New` at init and the `.values()`/`.get()` accessors elsewhere prove it. `ops` is a secondary op-cost accumulator (`inc_ops` `@0x2c820` is its mutator).

### 1.2 Abstract problem hooks — `NotImplementedError` stubs

| Hook | Addr | Meaning | Overridden by |
|---|---|---|---|
| `getReward(self, ctx)` | `0x25d90` | terminal reward | `MCTSTuneState.getReward` (§2.4) |
| `takeAction(self, action, ctx)` | `0x21b50` | apply action → child state | `MCTSTuneState.takeAction` (§2.2) |
| `enumerateActions(self, ctx)` | `0x25760` | legal actions at a state | `MCTSTuneState.enumerateActions` (§2.2) |

Each body literally loads `NotImplementedError` and raises. These are the **subclass override points** — the generic engine owns the tree policy; the subclass owns the problem.

### 1.3 ★ The UCT formula — `State.estimateReward` `@0x1fa30` (`MCTS.py:177–191`)

The headline. The per-child UCT score is **not** inlined in `chooseBestChild`; it lives in a dedicated method that `chooseBestChild` maximizes over children. Verified op-by-op against the decompile (line numbers are the `estimateReward` decompile file):

```c
// estimateReward(self, child, exploration)  @0x1fa30
//   self  = the PARENT node (estimateReward is called ON the parent, scoring each child)
//   child = the candidate child
//   exploration = the UCT exploration weight c (caller-supplied, default 0)
// Every step below is a decompiled CPython-API call.
mean = child.total_reward / child.num_visits;       // TrueDivide (L388)  == exploitation Q
//                                                    getattr total_reward (L370), num_visits (L380)
t    = math.log(self.num_visits);                    // math.log (L484); arg = self.num_visits (L494)
t2   = 2 * t;                                         // ★ nb_multiply(__pyx_int_2, t) (L529)
//                                                    float branch: PyNumber_Multiply (L541)
q    = t2 / child.num_visits;                         // TrueDivide (L570); getattr num_visits (L565)
s    = math.sqrt(q);                                  // math.sqrt (getattr L450)
e    = exploration * s;                               // PyNumber_Multiply (L591)
return mean + e;                                      // PyNumber_InPlaceAdd (L597)
```

★ **The exact UCT formula — UCB1-for-trees with the textbook `2·ln N`:**

```
estimateReward(parent, child, c)
    =  child.total_reward / child.num_visits                       // exploitation Q
     + c * sqrt( 2 * log(parent.num_visits) / child.num_visits )    // exploration U
```

- The **`2·log`** is real: `__pyx_int_2` is multiplied into `log(self.num_visits)` via the long-multiply slot (`nb_multiply`, decompile L529) and via `PyNumber_Multiply` on the float branch (L541) — this is the classic Kocsis–Szepesvári `C·sqrt(2 ln N / n)` constant, with `C = exploration`.
- `log` = `math.log` (natural log, ln); `sqrt` = `math.sqrt`. The whole module has **only** the integer constants `__pyx_int_0/1/2` — there is **no baked-in float** (no `sqrt(2)` literal); the exploration weight is entirely caller-supplied.
- There is **no** inf/NaN guard inside `estimateReward`; division-by-zero on an unvisited child is avoided by the **caller** (`chooseBestChild` filters `if child.num_visits`, §1.4).

> **GOTCHA — the numerator is `2·ln N`, not `ln N`.** The `2×` is a real `__pyx_int_2` multiply into `log(self.num_visits)`, not an artefact of the decompiler. Note also which node each term reads: `log`'s argument is **`self`, the parent**, while the denominator is the **child's** `num_visits`. Writing `c·sqrt(log(N)/n)` — or reading `N` off the child — gives a differently-tuned search.

### 1.4 Selection — `chooseBestChild` `@0x3a3c0` (`MCTS.py:185–200`)

`chooseBestChild(self, exploration)` builds a closure scope (`__pyx_scope_struct_1_chooseBestChild`) and runs a generator-expression (generator `@0x33210`):

```c
// the genexpr body  @0x33210
for child in self.children:                         // iterate children
    if child.num_visits:                             // IsTrue guard — SKIP never-visited
                                                      //   children (avoids /0 in §1.3)
        yield ( child.estimateReward(self, exploration=exploration), child );
// wrapper @0x3a3c0 then:
best = max over (score, child) pairs                 // PyObject_RichCompare scan
return self.breakTie(<tied winners>)                 // GetAttr n_s_breakTie on equal scores
```

So `chooseBestChild ≈ argmax over visited children of child.estimateReward(...)`, with `State.breakTie` (`@0x24b00`) resolving equal-score winners — [INFERRED] a uniform random pick among ties via the engine's rng, the only stochastic resource threaded through.

**`chooseKnownBestChild`** (`@0x18a90`, `MCTS.py:202–203`) calls `self.chooseBestChild(*const_tuple)` with a **fixed constant tuple** (`__pyx_tuple__30`) — a **greedy** best-child read with the exploration term neutralized (pure exploitation). The driver uses it at the end of search to read off the answer.

### 1.5 Expansion, rollout, and the tree policy

**`isFullyExpanded`** (`@0x2d540`, `MCTS.py:77–79`): `return all(<pred> for v in self.children.values())` — a node is fully expanded when every dict slot has been realized. **`expand`** (`@0x2b870`, lines 103–126): pick an untried expand-action (rng over `expandActions`/`rolloutExpandAction`), then `children.get(...)`/`takeAction(action, ctx)` to mint and store the new child.

**`rollout`** (`@0x2da80`, lines 138–147): from this state, `while not isLeaf()`: pick a `rolloutAction` (rng), `takeAction` → next state (`rolloutFullSolution` completes a full design); at the terminal leaf call `getReward(ctx)` (the **real** problem reward). The two reward channels — `estimateReward` (the **static UCT prior** used during *selection*) and `getReward` (the **real terminal reward** used during *rollout*) — are distinct; do not conflate them.

**`select`** (`@0x28b60`, lines 115–136) is the textbook UCT descent:

```c
// select(self, exploration, rng, ctx)  @0x28b60
node = self;
while (!node.isLeaf()) {
    if (node.isFullyExpanded())
        node = node.chooseBestChild(exploration);    // descend to UCT-best (§1.4)
    else
        return node.expand(...);                      // expand a frontier child (§1.5)
}
return node;
```

### 1.6 Backpropagation — `backpropogate` `@0x2ab10` [typo preserved] (`MCTS.py:157–167`)

The decompiled `while(1)` walks the parent chain upward, updating each node. The `max` is byte-confirmed via the comparison opcode:

```c
// backpropogate(self, reward, upto)  @0x2ab10
node = self;
while (1) {
    node.num_visits   += 1;                          // _Pyx_PyInt_AddObjC(., int_1)  (decompile L366/385)
    node.ops          += <delta>;                     // PyNumber_InPlaceAdd (L407)
    node.total_reward += reward;                       // PyNumber_InPlaceAdd (L444/455)
    // best_reward = max(best_reward, reward):
    if (PyObject_RichCompare(reward, node.best_reward, Py_GT))   // disasm 0x2ae95: mov edx,4 (Py_GT)
        node.best_reward = reward;                                //   then call RichCompare @0x2aea0
    parent = node.parent;                              // getattr n_s_parent (L563)
    if (parent is upto) break;                          // stop at the upto anchor (root/sub-root)
    node = parent;
}
```

`mov edx, 4` at `0x2ae95` is the `Py_GT` op selector immediately before `PyObject_RichCompare` (`0x2aea0`), which pins the comparison as `max()`. So backprop propagates the rollout reward up to `upto`, incrementing `num_visits`, accumulating `total_reward`, and tracking a per-node running **`best_reward = max`** of rewards seen through that subtree. **`rolloutAndBackprop`** (`@0x26a00`, line 212) is the glue: it checks `isLeaf`, calls `rollout`, **guards the reward with `math.isnan`** (line 259) — NaN rollouts are filtered out before backprop — then `node.backpropogate(reward, root)`.

### 1.7 The driver — `MCTS.search` / `executeNRound` / `executeRound`

```c
// MCTS.__init__(self, iteration_limit, exploration_constant, seed, rng)  @0x170c0  (arg order as written)
self.iteration_limit      = iteration_limit;
self.exploration_constant = exploration_constant;    // default-binds to __pyx_int_0 (0) in the prelude
self.rng                  = default_rng(seed);

// MCTS.search(self, initial_state, ctx)  @0x29f00
n = self.iteration_limit * <factor>;                  // PyNumber_Multiply; the multiplier's identity is INFERRED
initial_state.executeNRound(n=n, exploration_constant=self.exploration_constant, rng=self.rng, ...);
return initial_state.chooseKnownBestChild();          // greedy, zero-exploration answer readout (§1.4)

// executeNRound(self, n, exploration, rng, ctx)  @0x22180  →  for _ in range(n): executeRound(...)
// executeRound(self, exploration, rng, ctx)  @0x20820:
leaf = self.select(exploration, rng, ctx);            // tree policy (§1.5)
leaf.rolloutAndBackprop(rng, ctx);                    // playout + backprop (§1.6)
```

This is the canonical MCTS iteration — **select → expand → rollout → backprop**, repeated `n` times — with the best action extracted greedily via `chooseKnownBestChild`. (`getBestAction` `@0x25130` and `greedySearch` `@0x1b210` are alternate extractors; `depth_first_search`/`dynamic_programming` are non-MCTS exact solvers also exposed on the driver for small problems.)

---

## 2. The `layout/_TreeSearch` subclass (`_TreeSearch.so`)

`_TreeSearch.py` defines three classes: `MCTSTuneState` (the layout-specific `State` subclass), `MCTSTuneAction` (a frozen dataclass), and `MonteCarloTreeSearch` (the driver). The subclass overrides exactly the three abstract hooks from §1.2 plus the UCT scorer and tie-break; the base engine's `select`/`expand`/`backpropogate`/`chooseBestChild` are inherited from `MCTS.so`.

> **Type/vtable (binary-accurate).** `MCTSTuneState` is a CPython heap type; its methods are slots in the `__pyx_mdef_…_MCTSTuneState_*` `PyMethodDef` table (verified in `…names.json`): `_15estimateReward`, `_5takeAction`, `_3enumerateActions`, `_9getReward`, `_11rollout`, `_7isLeaf`, `_13breakTie`, `_17__hash__`, `_19graph_label`, `_1__init__`. There is **no `_ZTV`** for this class — dispatch is Python attribute resolution, and the engine's inherited `chooseBestChild` calls whatever `estimateReward`/`getReward` the instance resolves at run time (the subclass's overrides win).

### 2.1 The class/method map

| Method | Addr | `.py` line | Role |
|---|---|---|---|
| `MCTSTuneState.__init__` | `0x1c3c0` | 34–54 | ★ per-node **real compile** + bookkeeping |
| `MCTSTuneState.enumerateActions` | `0x1ab30` | 56–57 | ★ action space (plan list) |
| `MCTSTuneState.takeAction` | `0x12cd0` | 59–60 | ★ apply layout decision |
| `MCTSTuneState.isLeaf` | `0x166a0` | 65–67 | terminal test |
| `MCTSTuneState.getReward` | `0x14870` | 69–70 | `ctx.reward_f(default_metric)` (normalize only) |
| `MCTSTuneState.rollout` | `0x16cb0` | 72–95 | ★ MC sim: **batch** compile |
| `MCTSTuneState.estimateReward` | `0x15590` | 104–106 | UCT score (subclass override) |
| `MCTSTuneState.breakTie` | `0x11a10` | 97,102 | `max(children, key=(best_reward,plan_id))` |
| `MCTSTuneState.__hash__` | `0xfea0` | 109–110 | `id(self)` (identity, no dedup) |
| `MCTSTuneState.graph_label` | `0x12200` | 112 | graphviz caption |
| `MCTSTuneAction.__eq__` | `0x10270` | 24 | frozen eq on `plan_id` |
| `MonteCarloTreeSearch.__init__` | `0x13690` | — | driver ctor |
| `MonteCarloTreeSearch.compute_exploration_const` | `0x11200` | 128–132 | adaptive `c` |
| `MonteCarloTreeSearch.reward_f` | `0x10a20` | 133–135 | normalized improvement |
| `MonteCarloTreeSearch.run` | `0x1ea20` | 137–168 | driver loop |

### 2.2 The action space — `enumerateActions` / `takeAction`

```c
// enumerateActions(self, ctx)  @0x1ab30  — list comprehension
return [MCTSTuneAction(plan) for plan in self.action_plans];
// ctx is accepted but unused; the legal set was precomputed at __init__ time (§2.3).

// takeAction(self, action, ctx)  @0x12cd0
return MCTSTuneState(self.design + [action.plan_id], ctx);   // design grows by one plan_id
```

A candidate **action** is a **layout plan** wrapped in `MCTSTuneAction`; a **design** is an ordered list of `plan_id`s. The search is a **sequential design-building** problem: each action appends one plan id. `MCTSTuneAction` is a **frozen dataclass** with a single semantic field `plan_id` — `__eq__` (`@0x10270`) compares `self.plan_id == other.plan_id`, `__str__` (`@0x1a510`) is `f"{plan_id}"`, and `__hash__` is dataclass-synthesized on `plan_id`. Action identity = `plan_id` alone. (The concrete transform a `plan_id` encodes lives in `_PerformanceMetric.so`/`transforms.Region`, out of scope here.)

### 2.3 ★ Where the real compile happens — `__init__` `@0x1c3c0` (`_TreeSearch.py:34–54`)

```c
// MCTSTuneState.__init__(self, parent, design, ctx)  @0x1c3c0
super().__init__(parent);                                          // base State.__init__ (§1.1)
self.design = design;
res = ctx.metric.evaluate_with_subprocess_result(self.design);     // ★ CHILD-PROCESS COMPILE
(metric, design2) = res;
self.default_metric = metric;                                       // = nc_latency / PostSchedEstLatency
self.default_design = design;                                       //   (or design2 on the populated branch)
self.action_plans   = [] or design2-actionplans;                    // legal next plans come back from compile
ctx.logger.info("MCTS state created: [len(design)=%d, len(actions)=%d]");
if (self.default_metric < ctx.best_metric)  { ctx.best_metric  = self.default_metric;  ctx.best_design = self.default_design; }
if (self.default_metric > ctx.worst_metric) {  ctx.worst_metric = self.default_metric; }
ctx.num_states   += 1;
ctx.num_branches += len(self.action_plans);
```

★ **The closed loop:** every node, **on creation**, compiles its design via `ctx.metric.evaluate_with_subprocess_result`. The returned metric — the backend `PostSchedEstLatency`/`nc_latency` — is stored as `default_metric` and immediately threaded into the driver's running best/worst. The design-space **expansion** for the node (`action_plans`) also comes back from that same compile, so `enumerateActions` just re-reads what `__init__` cached.

> **GOTCHA — `getReward` is not where the compile happens.** Despite the name, `getReward` only **normalizes** an already-computed `default_metric`. The two real subprocess-compile sites are `__init__` (`evaluate_with_subprocess_result`, one per node) and `rollout` (`evaluate_batch`, in bulk). Budget your compile calls off those two, not off reward lookups.

### 2.4 Reward and the subclass UCT — `getReward` / `reward_f` / `estimateReward`

```c
// getReward(self, ctx)  @0x14870  — NO compile here
return ctx.reward_f(self.default_metric);

// MonteCarloTreeSearch.reward_f(self, val)  @0x10a20  — shape only
return ((self.baseline_metric - val) / self.baseline_metric) * K;   // K stripped; INFERRED as 1 or 100
// → fractional latency improvement vs a baseline compile: 0 at baseline, →1 as latency→0.

// MCTSTuneState.estimateReward(self, child, exploration)  @0x15590  — UCT, subclass override
return child.best_reward
     + exploration * math.sqrt(2 * math.log(self.num_visits) / child.num_visits);
```

The subclass UCT (`@0x15590`) is the same UCB1 shape as the base (§1.3), verified op-by-op: `getattr best_reward` (decompile L530) for the exploitation term, `__pyx_int_2 * log(num_visits)` (the `nb_multiply`/`PyNumber_Multiply` with `__pyx_int_2` at L745/L760) `/ child.num_visits` (L778, TrueDivide L793), `sqrt`, `* exploration` (L825), `+ best_reward` (InPlaceAdd L832). The **one difference** from the base: exploitation uses `child.best_reward` (the running max), not `total_reward/num_visits`. It does **not** reference `est_range`/`expected_deviation`.

**Adaptive exploration constant.** `est_range`/`expected_deviation` are the two **arguments** of `compute_exploration_const` (`@0x11200`), recomputed **every** `run()` iteration. Its shape is `return math.sqrt(f(est_range, expected_deviation)) / g(...)`; the exact tuple operands were stripped by the decompiler, so the precise arithmetic is [INFERRED] — plausibly `est_range·sqrt(expected_deviation/2)`. This makes the UCT exploration **adaptive** rather than a fixed hyper-parameter.

> **GOTCHA — equal designs are not deduplicated.** `MCTSTuneState.__hash__` is `id(self)`, so state identity is *object* identity: two nodes holding the same design list are distinct states and will each pay for their own compile. `graph_label` is a Graphviz caption (`"best: {best_reward}\nvisits: {num_visits}"`), not a content key, so nothing else in the class supplies dedup either. Note also that `est_range`/`expected_deviation` belong to `compute_exploration_const`, not to `estimateReward`.

### 2.5 Rollout and the driver loop

```c
// MCTSTuneState.rollout(self, ctx)  @0x16cb0
if (self.isLeaf()) return ctx.reward_f(self.default_metric);
cands = self.action_plans;                                   // possible_actions
if (len(cands) > ctx.max_simulations)
    cands = random.sample(cands, ctx.max_simulations);        // bound the simulation fan-out
sp_outputs = ctx.metric.evaluate_batch([self.design + [c.plan_id] for c in cands]);   // ★ BATCH compile
best_design, best_metric = _find_best_with_sp_output(zip(cands, sp_outputs));
ctx.best_metric  = best-of(ctx.best_metric,  best_metric);
ctx.worst_metric = worst-of(ctx.worst_metric, best_metric);
return ctx.reward_f(best_metric);

// MonteCarloTreeSearch.run(self, search_tree, search_list, timeout)  @0x1ea20
self.search_tree = search_tree;
self.best_design, self.best_metric, self.worst_metric = <root metrics>;
import time; start = time.time();
for _ in range(self.iterations):
    self.executeRound(exploration_const = self.compute_exploration_const(self.est_range, self.expected_deviation));
    if (time.time() > start + timeout * 60)                   // timeout is in MINUTES (×60)
        { self.logger.info("Search timeout reached. Ending search."); break; }
from .._TreeSearchVisualizer import writeGraph; writeGraph(self.search_tree, ...);
self.logger.info("The nodes visited by MCTS had an average of %f possible_actions" % (num_branches/num_states));
return (self.best_design, self.best_metric);
```

`rollout` is the **second** subprocess-compile site: it batch-compiles up to `ctx.max_simulations` sampled design completions via `evaluate_batch`, picks the best via `_find_best_with_sp_output`, and returns `reward_f(best_metric)` to backprop. `executeRound` itself is **inherited** from `MCTS.so` (§1.7); `run()` only drives it and recomputes the adaptive exploration const each iteration. The native non-MCTS strategies (§3) ignore `timeout`; **only this MCTS path honours it**.

---

## 3. The strategy roster (`_Search.so`)

`_Search.py` defines one abstract base plus **five concrete native strategies**, and imports a sixth (MCTS) so it too is selectable through the same registry:

```
Search                            (abstract base; object subclass)             src 17
 ├─ RandomSearch(Search)                                                        src 45
 ├─ ExhaustiveSequentialSearch(Search)                                          src 55
 │    └─ DownsampledSequentialSearch(ExhaustiveSequentialSearch)                src 92
 ├─ ExhaustiveSearch(Search)                                                    src 108
 ├─ GreedySearch(Search)                                                        src 125
 └─ MonteCarloTreeSearch(Search)   ← imported from ._TreeSearch (§2)   (extern, LAST)
```

The five native classes' wrappers are all present as decompiled `__pyx_pw_…_<Strategy>_*` symbols at the addresses below. `DownsampledSequentialSearch` **subclasses** `ExhaustiveSequentialSearch` (inherits `_do_search`) — it is **not** a sibling.

### 3.1 The base `Search` interface

```c
// Search.__init__(self, metric, logger, requires_design_space)  @0x13300  (src 33)
self.metric = metric;  self.logger = logger;  self.requires_design_space = requires_design_space;

// Search.run(self, search_tree, search_list, timeout)  @0x18d70  (src 38)  — ABSTRACT
raise NotImplementedError;                                  // the uniform contract every strategy overrides

// Search.requires_design_space  @0x129f0  (src 41)  — read-only property → self._requires_design_space
```

`metric` is a `PerformanceMetric` reward oracle; `logger` a logging handle; `requires_design_space` a bool the Autotuner consults to decide whether to pre-materialize the full flat `search_list`. Each subclass forwards a **fixed literal** as the 3rd `super().__init__` arg, visible as a disasm immediate: `True` for `RandomSearch`/`ExhaustiveSearch`/`GreedySearch` (they iterate a pre-enumerated `search_list`), `False` for the sequential pair (they drive incremental plan expansion from `search_tree` via `_SequentialHelperAutotuner`).

### 3.2 The shared selection rule — `_find_best`

```c
// _find_best(candidates, metrics)  @0x12c80  (src 9)
best = min(metrics);                  // __pyx_builtin_min   (decompile L143)
idx  = metrics.index(best);           // .index              (decompile L231)
return candidates[idx];               // subscript           (decompile L265)
// → ARGMIN: LOWER metric (latency) == BETTER. The single shared "best" rule.

// _find_best_with_sp_output(candidates, metrics, sp_outputs)  @0x13b00  (src 15)
// same argmin, but also returns the matching sp_outputs[idx] (the subprocess-compile artifact).
```

This argmin **minimizes** the raw latency metric — the exact inverse of the MCTS engine which **maximizes** `total_reward` (a `-latency`-oriented reward). Same orientation, opposite sign convention.

### 3.3 The four non-MCTS strategy loops

```c
// ── RandomSearch ── __init__ @0x16540 (src 45) | run @0x1fee0 (src 52)
// __init__: super().__init__(metric, logger, True); self.iterations = search_settings['iterations']  // REQUIRED (KeyError if absent)
def run(self, search_tree, search_list, timeout):
    candidates = random.sample(search_list, self.iterations);   // n_s_sample (decompile L154); iterations (L223)
    metrics    = self.metric.evaluate_designs(candidates);       // batch reward (L368)
    return _find_best(candidates, metrics);                      // argmin (L460)
// ONE-SHOT: draw `iterations` random designs (no replacement), evaluate, return argmin. timeout NOT consulted.

// ── ExhaustiveSearch ── __init__ @0x18280 (src 108) | run @0x1cec0 (src 113)
// __init__: super().__init__(metric, logger, True)  // no params
def run(self, search_tree, search_list, timeout):
    metrics = self.metric.evaluate_designs(search_list);         // evaluate the ENTIRE list (L124)
    return _find_best(search_list, metrics);                     // argmin (L230)
// BRUTE FORCE: evaluate every design, return argmin. timeout NOT consulted.

// ── GreedySearch ── __init__ @0x19610 (src 131) | _find_nn @0x11380 | run @0x1ada0 (src 138)
// __init__: super().__init__(metric, logger, True);
//   self.max_neighbors = search_settings.get('max_neighbors', 3);   // edi=3 @0x19a99 (byte-firm)
//   self.min_dist      = search_settings.get('min_dist',      1);   // edi=1 @0x19ba1
//   self.iterations    = search_settings.get('iterations',   10);   // edi=0xA @0x19caa
//   self.nn_dict       = {}                                          // NN cache
def run(self, search_tree, search_list, timeout):
    current = random.choice(search_list);
    cur_m   = self.metric.evaluate_design(current);              // scalar reward
    for _ in range(self.iterations):                             // hill-climb rounds (default 10)
        nbrs   = self._find_nn(current, search_list);            // memoised neighbours ≤ min_dist
        sample = random.sample(nbrs, self.max_neighbors);        // bound fan-out
        ms     = self.metric.evaluate_designs(sample);
        best   = _find_best(sample, ms);                         // argmin neighbour
        if (best improves cur_m) { current = best; cur_m = ...; } // RichCompare accept
// HILL-CLIMB: from a random seed, hop to the best of ≤max_neighbors random neighbours within min_dist, iterations× rounds. timeout NOT consulted.

// ── ExhaustiveSequentialSearch ── __init__ @0x156f0 (src 58) | _do_search @0x1d890 | run @0x143d0 (src 87)
// __init__: super().__init__(metric, logger, False)  // requires_design_space=False; no branch cap
// _do_search(self, candidates, max_branches): for each candidate, set helper.design/.metric/.plans
//   via `from ..Autotuner import _SequentialHelperAutotuner`; random.sample(...) to bound to max_branches children;
//   self.metric.evaluate_batch(...); _find_best_with_sp_output(...) → keep best (and its sp_output); recurse.
// run: self._do_search(search_list, <unbounded>)  // a breadth-bounded plan-expansion beam; full enumeration.

// ── DownsampledSequentialSearch ── __init__ @0x173b0 (src 96) | run @0x14c00 (src 100) | inherits _do_search
// __init__: super().__init__(metric, logger);  self.max_branch_factor = search_settings.get('max_branch_factor', 2);  // edi=2 @0x177eb
// run: return self._do_search(search_list, self.max_branch_factor)  // beam width 2 — the cheap sibling of the exhaustive sequential.
```

**Termination model.** `_Search.so` imports **only** `random` and `typing` — there is **no** `time`/`perf_counter`/`monotonic` import anywhere. Every native `run()` *accepts* `timeout` (to satisfy the uniform contract) but **never reads a clock against it**. Termination is purely structural: `RandomSearch` = one batch of size `iterations`; `ExhaustiveSearch` = full sweep; `GreedySearch` = `iterations` rounds; the sequential pair = (bounded) plan-tree exhaustion. Only the imported `MonteCarloTreeSearch` honours `timeout` (§2.5). An outer wall-clock may live in the Autotuner orchestration [SPECULATIVE].

### 3.4 Strategy selection — `Search.build` registry `@0x210b0`

```c
// Search.build(cls, search_name, metric, config_search_settings, logger, baseline_metric)  @0x210b0  (src 23)
// 1. special-case branch compares search_name against the literal "DownsampledSequentialSearch"
//      (__pyx_n_u_DownsampledSequentialSearch — the only class name embedded as a comparison constant);
// 2. general path: genexpr @0x10a90 over cls.__subclasses__() matching subclass .__name__ vs search_name;
// 3. chosen_cls(metric, config_search_settings, logger);  obj.baseline_metric = baseline_metric;  return obj.
```

Strategy selection = pass a **`search_name` string** — one of `"RandomSearch"`, `"ExhaustiveSearch"`, `"GreedySearch"`, `"ExhaustiveSequentialSearch"`, `"DownsampledSequentialSearch"`, `"MonteCarloTreeSearch"` — into `Search.build`. It resolves the class from the **live `Search.__subclasses__()` set** (so the imported MCTS is selectable too), instantiates it with `(metric, config_search_settings, logger)`, sets `baseline_metric`, and returns it. The registry is **not** a dict literal — it is the genexpr over the subclass set. This is the hook the Autotuner orchestration calls.

### 3.5 Per-strategy parameter table (byte-firm defaults)

| Strategy | `requires_design_space` | params (default) | reward call | terminates by |
|---|---|---|---|---|
| `RandomSearch` | `True` | `iterations` (**REQUIRED**) | `evaluate_designs` | `iterations` (one batch) |
| `ExhaustiveSearch` | `True` | (none) | `evaluate_designs` | list sweep |
| `GreedySearch` | `True` | `max_neighbors`=3, `min_dist`=1, `iterations`=10 | `evaluate_design`/`_designs` | `iterations` rounds |
| `ExhaustiveSequentialSearch` | `False` | `max_branches` (unbounded) | `evaluate_batch` (+sp) | plan-tree exhaustion |
| `DownsampledSequentialSearch` | `False` (inherited) | `max_branch_factor`=2 | `evaluate_batch` (+sp) | beam width 2 |
| `MonteCarloTreeSearch` | — | `iterations`, adaptive `c` | subprocess compile (UCT) | `iterations` / `timeout` |

All defaults are byte-firm `PyLong_FromLong` immediates from the disasm (`edi=3`/`1`/`0xA`/`2`). The `config_search_settings` keys `max_candidates`/`nearest_neighbors`/`neighbor_min_distance` appear in the string pool but are **not** read by any body here — they belong to the Autotuner-side config schema, not the strategy bodies — there is no `FromLong`/`get` site for them in any body here.

---

## 4. Cross-module wiring

`_Search.so` sits **between** the reward oracle below it and the orchestration above it. Its module init (`@0x7b95`) executes, in order: `import random`; `from .._PerformanceMetric import PerformanceMetric`; `from typing import List, Dict, Any`; and **last** `from ._TreeSearch import MonteCarloTreeSearch` (level-1 relative). Inside `_do_search` it additionally does `from ..Autotuner import _SequentialHelperAutotuner`. The reward path — `evaluate_design`/`evaluate_designs`/`evaluate_batch` → `PerformanceMetric` → `PostSchedMetric._evaluate` → `PostSchedEstLatency`/`nc_latency` — is the **same** post-schedule latency the walrus scheduler emits; the autotuner only ever does arithmetic on that scalar.

- **Below:** `_PerformanceMetric.so` — the reward oracle (the `plan_id`→transform decoder + the subprocess-compile harness). Out of scope here.
- **Sibling:** `_TreeSearch.so` (§2) — the MCTS strategy, a selectable `Search` subclass.
- **Above:** `Autotuner.so` — the orchestration that calls `Search.build`, owns `_SequentialHelperAutotuner` plan state, and wraps the reward loop. See the autotuner-orchestration page (in flight).
- **Driver:** the [layout middle-end](../penguin/layout-middle-end.md) arms this whole machine via `--layout-transform-heuristic=mcts`; the default `none` runs the deterministic PAG static search and never touches this autotuner.
- **Not this:** the backend [`pga-feedback`](pga-feedback.md) (in flight) is a brute-force 8×8 grid search around the VN-Splitter — a different algorithm in a different binary (`libwalrus.so`).

---

## 5. What is pinned, and what is not

Most of this page is anchored to a disasm opcode or a decompiled CPython-API call. That covers the UCT formula including the `2·log` factor (§1.3, the `__pyx_int_2` nb_multiply; subclass at `0x15590`); the node struct including `-np.inf` and the dict `children` (§1.1, `PyNumber_Negative` and `PyDict_New`); the backprop `max()` via `Py_GT` (§1.6, `mov edx,4` @ `0x2ae95`); the abstract hooks raising `NotImplementedError` (§1.2); the driver flow `executeRound = select + rolloutAndBackprop` (§1.7); `chooseBestChild` as argmax of `estimateReward` over visited children plus `breakTie` (§1.4); the subclass action space / real-compile / `getReward` split (§2.2–2.4); the five native strategies' loops and their byte-firm defaults (§3.3); `_find_best` as argmin (§3.2); the `Search.build` name→`__subclasses__()` registry (§3.4); the absence of any `_ZTV` in `_TreeSearch.so`; and the absence of a `time` import in `_Search.so` (§3.3).

Four structural shapes are read from unambiguous control flow rather than transcribed instruction by instruction: `select`'s while-loop (§1.5), `expand`'s mint-child sequence (§1.5), both subclass rollout loops (§2.5), and `_do_search`'s per-node expand-evaluate-find_best (§3.3, whose 2123-line wrapper was not fully linearised).

Five items remain [INFERRED]: `breakTie` as an rng pick among base-engine ties (§1.4); the identity of `search`'s `iteration_limit * factor` multiplier (§1.7); the exact `compute_exploration_const` arithmetic over `est_range`/`expected_deviation` (§2.4 — the decompiler stripped the `PyTuple_Pack` operands, so the adaptive-`c` *shape* is solid but the arithmetic is not); the `reward_f` multiplier `K` (§2.4, either 1 or 100, a stripped constant); and what a `plan_id` concretely encodes (§2.2, which lives in `_PerformanceMetric.so`). One item is [SPECULATIVE]: an outer wall-clock deadline in the Autotuner wrapping the native strategies (§3.3).

The two genuinely unresolved items — the `compute_exploration_const` formula and the `reward_f` multiplier — are both scalars. Neither changes the algorithm's structure.
