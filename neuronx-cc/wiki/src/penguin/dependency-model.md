# Penguin Dependency Model — DependencyEdge & EdgeKind

> *All symbols, mangled names, and strings on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (the cp310 Cython modules under `neuronxcc/starfish/penguin/ir/`). Other wheels differ; treat every symbol as version-pinned. The `ir/Dependency.cpython-310-…-.so` and `ir/Function.cpython-310-…-.so` modules ship `debug_info` and are **not** stripped, so the class roster, field names, and method roster are recoverable byte-for-byte from the `__pyx_pw_…` wrapper symbols and the interned-name pool.*

## Abstract

Penguin tracks ordering and data dependencies between operations with a **Function-level edge list**, not inline on each instruction. The unit is `DependencyEdge` (`ir/Dependency.py` → `Dependency.cpython-310-…-.so`): a first-class, directed `src → dst` object between two `Instruction`s, tagged with an `EdgeKind` ∈ `{FLOW, ANTI, OUTPUT, ORDERED}`. The whole set of edges lives on the `Function` (`Function.dep_edges`), and the dependency-analysis passes ([5.13](software-pipeline-and-scheduling.md)) build, query, and prune that set before the Tonga scheduler reorders anything.

This is a deliberately different shape from the BIR side it lowers into ([Part 7](../bir/)). BIR's `bir::Instruction` stores its edges **inline**, in three TBB `concurrent_unordered_set<EdgePtr>` containers keyed by the target instruction's *name*, with a single `A→B` edge collapsed to the **maximum** `EdgeKind`. Penguin instead keeps a flat, un-merged list of `(src, dst, kind)` objects owned by the `Function`. The four `EdgeKind` members are byte-identical in meaning to BIR's `bir::EdgeKind` (`FLOW`=RAW, `ANTI`=WAR, `OUTPUT`=WAW, `ORDERED`=injected order) — Penguin simply omits BIR's `Invalid0` sentinel. `DependencyEdge.serialize` is the explicit Penguin→BIR handoff: BirCodeGenLoop materializes each edge into a `bir::addDependency(target, kind)` call, and the name-hashing + MAX-merge is a BIR-side representation choice applied *during* lowering.

A second, separate distinction matters throughout: the `Tensor` SSA use-list (`loads`/`stores`/`accessing_insts`/`replaceAllUsesWith`, [5.1](ir-node-model.md)) is an **always-present** def-use layer; the `DependencyEdge` graph is the **scheduling** layer the dep passes construct *on top of* it. The two answer different questions — "who reads this value" vs "what ordering constraints bind these two instructions" — and a reimplementer must not conflate them.

For reimplementation, the contract is:

- The `DependencyEdge` node: fields `src`, `dst`, `kind` (`kind: Optional[EdgeKind]`), and `serialize`.
- The `EdgeKind` enum: the four members, their RAW/WAR/WAW/ORDERED meaning, and the proof they map 1:1 onto `bir::EdgeKind` minus `Invalid0`.
- The `Function`-level container: `dep_edges` plus the seven mutate/query methods that own the graph.
- The two-layer separation: SSA use-list (per-`Tensor`) vs the `DependencyEdge` scheduling graph (per-`Function`).

| | |
|---|---|
| **Edge node** | `DependencyEdge` — `ir/Dependency.cpython-310-…-.so` (`__pyx_pw_…DependencyEdge_1__init__`, `_3serialize`) |
| **Edge fields** | `src`, `dst`, `kind` (`kind: Optional[EdgeKind]`) |
| **Kind enum** | `EdgeKind` ∈ `{FLOW, ANTI, OUTPUT, ORDERED}` (all four interned in `Dependency.so`) |
| **Container** | `Function.dep_edges` — `ir/Function.cpython-310-…-.so` |
| **Build path string** | `/opt/workspace/KaenaCompilerNativeBuild-310/build/private/src-3.10.16` |
| **Imports** | `neuronxcc.pelican.ir`, `…ir.AffineExpr`, `…ir.AffinePredicate`, `…ir.Instruction` |
| **BIR mirror** | `bir::EdgeKind` `{Invalid0, Ordered1, Anti2, Output3, Flow4}` (Part 7; `operator<` @`0x26add0`) |

---

## The DependencyEdge Node

### Purpose

A `DependencyEdge` is one directed ordering constraint between two operations: "`src` must be ordered before `dst`, for the reason given by `kind`." It is a *first-class object* — a node you can hold a reference to, put in a list, and `serialize` — which is exactly what makes the Penguin representation differ from BIR (where an edge is a packed `EdgePtr` with no standalone identity, see [BIR contrast](#contrast-with-bir-inline-vs-function-level)).

### Layout

The class is a regular Python class (Cython found no C-level `__pyx_obj_…DependencyEdge` instance struct), so its fields live in `__dict__`; the names below are CONFIRMED present in the module's interned-name pool, with the owner pinned to `DependencyEdge` by the `__init__`/`serialize` method wrappers.

```c
// ir/Dependency.py  →  Dependency.cpython-310-…-.so
class DependencyEdge:                         // __pyx_pw_…DependencyEdge_1__init__
    Instruction        src;                   // the producer / earlier op
    Instruction        dst;                   // the consumer / later op
    Optional[EdgeKind] kind;                  // pool: type-annotation string "Optional[EdgeKind]"

    def __init__(self, src, dst, kind): ...   // DependencyEdge_1__init__   (CONFIRMED pyx symbol)
    def serialize(self): ...                  // DependencyEdge_3serialize  (CONFIRMED pyx symbol)
                                              //   → json {kind, src, dst}  — the Penguin→BIR handoff
```

> **NOTE —** `kind` is `Optional[EdgeKind]` (the verbatim annotation string in the pool), i.e. an edge may be created before its kind is decided. A reimplementer should treat an un-kinded edge as a bare ordering placeholder until a dep pass classifies it.

### The Edge Direction

`src → dst` is producer→consumer for a `FLOW` (RAW) edge; for `ANTI`/`OUTPUT`/`ORDERED` it is still "earlier→later" in the required program order. The same `(src, dst)` pair can appear under more than one `EdgeKind` in the Penguin list (the list is *not* MAX-merged the way BIR is) — see the [BIR contrast](#contrast-with-bir-inline-vs-function-level).

---

## EdgeKind

### Purpose

`EdgeKind` classifies *why* `src` precedes `dst`. The four members are the standard data-hazard taxonomy plus a programmer/scheduler-injected ordering bucket.

### The Four Members

All four tokens (`FLOW`, `ANTI`, `OUTPUT`, `ORDERED`) and the `EdgeKind` type name are CONFIRMED in `Dependency.so`'s interned-name pool.

| Member | Hazard | Meaning |
|---|---|---|
| `FLOW` | RAW | true dataflow — `dst` reads what `src` wrote (producer→consumer) |
| `ANTI` | WAR | `src` reads a location `dst` then overwrites (write-after-read) |
| `OUTPUT` | WAW | `src` and `dst` both write the same location (write-after-write) |
| `ORDERED` | — | scheduler- or programmer-injected ordering with no data hazard (barriers, forced order) |

> **GOTCHA —** the Cython interned-name pool lists these tokens in *first-use* order (observed: `ORDERED`, `OUTPUT`, `FLOW`, `ANTI`), which is **not** the enum's ordinal order. Do not read ordinals off the string-pool order; the ordinal mapping is fixed by the BIR mirror below, not by intern order.

### Byte-Identity with bir::EdgeKind

Penguin's `EdgeKind` member *set* and *meanings* are identical to BIR's `bir::EdgeKind`. The BIR enum is pinned in Part 7 from `bir::operator<` (@`0x26add0`, ordering `Invalid < Ordered < Anti < Output < Flow`) and the `dependency_edge_kind_t` JSON enum:

```c
// BIR side (Part 7 — libBIR, C++):    CONFIRMED ordinals
enum dependency_edge_kind_t {
    INVALID            = 0,   //  ← Penguin OMITS this sentinel
    ORDERED            = 1,   //  ≡ Penguin EdgeKind.ORDERED
    ANTI_DEPENDENCE    = 2,   //  ≡ Penguin EdgeKind.ANTI
    OUTPUT_DEPENDENCE  = 3,   //  ≡ Penguin EdgeKind.OUTPUT
    FLOW_DEPENDENCE    = 4,   //  ≡ Penguin EdgeKind.FLOW   (the MAX kind)
};
```

The four real Penguin kinds map 1:1 onto BIR's non-`Invalid` members (member-set and meaning: CONFIRMED both sides). `DependencyEdge.serialize` emits json `{kind, src, dst}` and is the explicit contract: BirCodeGenLoop reads that and calls `bir::addDependency(target, kind)`.

> **NOTE (confidence) —** that the *member set* `{FLOW, ANTI, OUTPUT, ORDERED}` is byte-identical to BIR's non-`Invalid` set is CONFIRMED on both sides. The exact *integer ordinal* each Penguin member carries (e.g. that Penguin's `ORDERED`=1, `FLOW`=4) is **INFERRED** to match the BIR ordinals — the Penguin enum is an interned name list with no readable backing integers in the `.so`. The 1:1 mapping is by name and meaning, which is what the `serialize`→`addDependency` handoff actually consumes.

---

## Where Dependency Edges Live — the Function-Level Graph

### Purpose

Unlike BIR, Penguin does **not** store dependencies on the instruction. The entire dep graph is a flat set of `DependencyEdge` objects owned by the `Function` (`ir/Function.py`), and the `Function` exposes the mutate/query API the dep passes drive.

### The Container API

All seven methods are CONFIRMED in `Function.cpython-310-…-.so` (`__pyx_pw_…Function_*` wrappers; qualnames in the pool):

| Method | Role |
|---|---|
| `dep_edges` | the edge set (the graph itself) |
| `add_dep_edge` | insert a `DependencyEdge` |
| `remove_dep_edge` | delete an edge |
| `dep_edges_for_inst` | all edges touching one `Instruction` |
| `depending_insts` | the instructions that depend on a given one |
| `replace_inst_in_dependencies` | rewrite an instruction across the graph (used when an op is replaced) |
| `replace_with_list_in_dependencies` | rewrite one instruction into a list (used when an op is expanded/lowered) |

```c
// ir/Function.py  →  Function.cpython-310-…-.so
class Function:
    Set[DependencyEdge] dep_edges;                       // the per-Function dep graph

    def add_dep_edge(self, edge): ...                    // Function_…add_dep_edge
    def remove_dep_edge(self, edge): ...                 // Function_…remove_dep_edge
    def dep_edges_for_inst(self, inst): ...              // edges incident on `inst`
    def depending_insts(self, inst): ...                 // consumers of `inst`
    def replace_inst_in_dependencies(self, old, new): ...        // RAUW at the edge level
    def replace_with_list_in_dependencies(self, old, [new...]): ...   // op→sequence rewrite
```

> **QUIRK —** `replace_with_list_in_dependencies` exists because a single high-level `Operator` (e.g. a fused macro, [5.4](node-families-operators.md)) is lowered into a *list* of ISA `Instruction`s; the edge graph must be rewritten so every edge that pointed at the macro now points at the right member of the expansion. A reimplementation that only offers an instruction-for-instruction `replace_inst_in_dependencies` will silently drop edges during op expansion.

---

## Contrast with BIR — Inline vs Function-Level

The two representations encode the *same* logical graph two different ways. This is the single most important fact for anyone bridging Penguin and BIR.

| Aspect | Penguin (`ir/Dependency`, this page) | BIR (`bir::Instruction`, [Part 7](../bir/)) |
|---|---|---|
| Edge identity | first-class `DependencyEdge` object | packed `EdgePtr` (`target* \| EdgeKind&7`), no standalone object |
| Storage | flat `Set[DependencyEdge]` on the `Function` | three inline TBB `concurrent_unordered_set<EdgePtr>` on each `Instruction` (`dependencies`/`descendents`/`loop_carried`) |
| Keyed by | the `src`/`dst` `Instruction` references | the **target instruction's name** (hash key) |
| Multi-kind `A→B` | may appear once per kind (un-merged list) | collapsed to **one** edge = **MAX** `EdgeKind` (`Flow>Output>Anti>Ordered`) |
| Sentinel | none (four real kinds) | includes `Invalid0` |
| Loop-carried | not a separate container | a dedicated `loop_carried` set |

```text
PENGUIN (logical, pre-schedule)              BIR (physical, post-lowering)
  Function.dep_edges                           Instruction[B] +0xD0 sched block:
   ├─ DependencyEdge(A, B, FLOW)                 dependencies   = { EdgePtr(A | 4) }   // MAX-merged
   ├─ DependencyEdge(A, B, OUTPUT)   ──lower──▶   descendents    = { … }
   └─ DependencyEdge(C, B, ORDERED)              loop_carried   = { … }
                                                 (A→B Flow+Output collapse to one EdgePtr, kind=MAX=Flow)
```

> **NOTE —** the MAX-merge and name-hashing happen **on the BIR side during lowering**, not in Penguin. BirCodeGenLoop walks `Function.dep_edges` and emits an `addDependency(target, kind)` per edge; BIR's `addEdge` then merges by max into the inline sets. After register/SBUF allocation the backend additionally *re-builds* `FLOW`/`ANTI` edges over **physical** accesses (`build_fdeps`, `anti_dependency_analyzer`, Part 7) — the Penguin edges are the *logical/tensor-level* set, the BIR re-derivation is the *physical* set.

---

## The Two Dependency Layers — SSA Use-List vs DependencyEdge

A reimplementer must keep two distinct graphs in mind; they coexist and serve different passes.

```text
LAYER 1 — SSA def-use (always present, per-Tensor)     [5.1 node model]
  Tensor.loads / Tensor.stores / Tensor.accessing_insts
  Tensor.replaceAllUsesWith / replaceUseOfWith / link_use_inst
   → answers "who reads/writes this VALUE"; built implicitly when the IR is built.

LAYER 2 — DependencyEdge scheduling graph (built by dep passes, per-Function)   [this page]
  Function.dep_edges  =  { DependencyEdge(src, dst, kind) }
   → answers "what ORDERING constraints bind these two INSTRUCTIONS";
     built ON TOP of Layer 1 by LoadStoreDependencyAnalysis + AliasDependency*.
```

- **Layer 1** is the `Value`/`User` use-list every node carries by construction ([5.1](ir-node-model.md)). It is a *value*-keyed graph (operands and uses of a `Tensor`/`ScalarValue`).
- **Layer 2** is the `DependencyEdge` graph, an *instruction*-keyed ordering graph. `LoadStoreDependencyAnalysis` builds it from the Layer-1 load/store accesses; the `AliasDependency*` family induces extra `ORDERED` edges where buffers alias so the scheduler cannot illegally reorder aliasing access; `Instruction.output_dependencies_indices_set` / `AffineStore.get_missing_output_dependencies` compute the WAW/`OUTPUT` deps at the access level.

> **GOTCHA —** RAUW on a `Tensor` (`replaceAllUsesWith`) updates **Layer 1 only**. To rewrite an instruction's place in the **Layer-2** scheduling graph you must call `Function.replace_inst_in_dependencies` (or `…replace_with_list_in_dependencies`). Forgetting the second call leaves stale `DependencyEdge` objects pointing at a dead instruction — a class of bug that survives a use-list-only check.

---

## Who Consumes the Edges

- The **Tonga Scheduler** ([5.13](software-pipeline-and-scheduling.md)) builds its data-dependency graph (DDG) from these edges (plus `Dataflow` def/use sets) and greedily reorders SUs to minimize buffer pressure; ties broken by `static_lex_order` (original program order).
- **BirCodeGenLoop** ([5.x lowering](node-to-bir-mapping.md)) emits the post-schedule order verbatim and materializes each `DependencyEdge` into a BIR `addDependency` call, honoring per-`ScopeRegion` `no_reorder` flags.
- The **backend** (Part 7) re-derives physical `FLOW`/`ANTI` edges post-allocation and materializes sync semaphores — none of which exists at the Penguin level.

---

## Function Map

| Symbol | Module | Role | Confidence |
|---|---|---|---|
| `DependencyEdge.__init__` | `ir/Dependency.so` | edge ctor `(src, dst, kind)` | CONFIRMED |
| `DependencyEdge.serialize` | `ir/Dependency.so` | json `{kind, src, dst}` — Penguin→BIR handoff | CONFIRMED |
| `EdgeKind` (enum) | `ir/Dependency.so` | `{FLOW, ANTI, OUTPUT, ORDERED}` | CONFIRMED (members); ordinals INFERRED from BIR |
| `Function.dep_edges` | `ir/Function.so` | the edge set | CONFIRMED |
| `Function.add_dep_edge` / `remove_dep_edge` | `ir/Function.so` | insert / delete | CONFIRMED |
| `Function.dep_edges_for_inst` | `ir/Function.so` | edges incident on an inst | CONFIRMED |
| `Function.depending_insts` | `ir/Function.so` | consumers of an inst | CONFIRMED |
| `Function.replace_inst_in_dependencies` | `ir/Function.so` | inst RAUW at edge level | CONFIRMED |
| `Function.replace_with_list_in_dependencies` | `ir/Function.so` | inst→list rewrite at edge level | CONFIRMED |

---

## Cross-References

- [5.1 — Penguin IR Node Model](ir-node-model.md) — the `Value`/`User` SSA use-list that Layer 2 is built on top of
- [5.4 — High-Level Operator Family](node-families-operators.md) — the `Operator`/`Instruction` nodes that `src`/`dst` point at; macro expansion drives `replace_with_list_in_dependencies`
- [5.13 — Software Pipeline & Scheduling](software-pipeline-and-scheduling.md) — the Tonga scheduler / `LoadStoreDependencyAnalysis` / `AliasDependency*` passes that build and consume these edges
- [4.15 — Control-Dependence Modeling](../hlo-opt/control-dependence.md) — the upstream control-dep representation that becomes `ORDERED` edges here
- [Part 7 — BIR Dependency & Sync Model](../bir/) — the C++ `bir::EdgeKind` `{Invalid0, Ordered1, Anti2, Output3, Flow4}` this enum lowers into, the inline TBB edge sets, and the MAX-merge
