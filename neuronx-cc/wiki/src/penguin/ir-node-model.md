# Penguin IR Node Model — the SSA Def-Use Graph

> *All symbols, mangled names, and strings on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (the cp310 Cython modules under `neuronxcc/starfish/penguin/ir/`). Other wheels differ; treat every symbol as version-pinned. Every claim is grounded in the `.so` symbol pool — these IR modules ship `debug_info` and are **not** stripped, so the public method roster of each class is recoverable byte-for-byte from its `__pyx_pw_…` wrapper symbols.*

## Abstract

Penguin is the tensorizer middle-end's IR: the level that sits **below** the MLIR front half ([Part 4](../hlo-opt/)) and **above** the C++ BIR the backend schedules and allocates ([Part 7](../bir/)). Structurally it is a textbook **SSA def-use graph** — the same `Value`/`User` skeleton an LLVM reader already owns — but realized as a galaxy of Cython-compiled Python classes (one `.so` per file) that are thin wrappers over a C++ uniqued-value layer (`neuronxcc.pelican.ir`). Every node is a `Value`; every node that consumes operands is a `User`; every node that *computes* a result is a `ComputeValue`, which forks into the two concrete worlds the rest of Part 5 is about: `Tensor` (a tensor-valued definition) and `Instruction`/`Operator` (an op that produces results). This page is the foundation: it fixes the class hierarchy, the def-use edge representation, the ~274-class registry, and the `Module ⊃ Function ⊃ BasicBlock ⊃ Stmt ⊃ Instruction` containment that every later 5.x page assumes.

Three facts shape everything downstream and are easy to get wrong. First, the IR object identity model is **two layers**: a Python face (`Value.py`, `Instruction.py`, …) and a C++ value (`pelican::Value`); the Python classes are regular `__dict__`-backed classes — Cython emitted **no** C-level instance struct for them — so field *names* are recoverable from the string pool but field *types and offsets are not* (a structural gap, marked throughout). Second, the def-use use-list lives on the **value** side, not on `User`: `addUser`/`removeUser`/`replaceAllUsesWith` are methods of the value (confirmed in `ComputeValue.so`), and `User.so` is a near-empty mixin. Third — the single biggest divergence from LLVM — Penguin does **not** encode loop-nest guards as CFG branches; each `Instruction` carries a list of `AffinePredicate`s, and roughly half of `Instruction`'s 51 methods are the predicate sub-model. Control flow is mostly affine masking, not basic blocks.

The page proceeds: the master registry (§ how the 274 classes are typed and re-exported), the SSA value base (`Value`→`User`→`ComputeValue`→{`Tensor`, `Instruction`/`Operator`}), the def-use edge representation, then the scope/region containment, and finally how this node graph maps up to MLIR ([Part 4](../hlo-opt/)) and down to BIR ([Part 7](../bir/)).

For reimplementation, the contract is:

- **The class hierarchy** — `Value` → `User` → `ComputeValue` → {`ScalarValue`, `Tensor`, `Instruction` → `Operator`}, with the use-list machinery on the value, and the predicate list on the instruction.
- **The def-use edge representation** — how a `Value` tracks its `User`s (the use-list + the `Tensor`-level `loads`/`stores`/`accessing_insts` overlay), distinct from the `Function`-level scheduling dependency graph.
- **The container nesting** — `Module ⊃ Function ⊃ BasicBlock ⊃ Stmt{Block/StmtGroup/Macro/DAGMacro} ⊃ Instruction`, and where each field lives.
- **The registry mechanism** — `ir.so` as the umbrella re-export module that interns every IR class name into one pool.

| | |
|---|---|
| **Package** | `neuronxcc/starfish/penguin/ir/*.cpython-310-x86_64-linux-gnu.so` (Cython, `debug_info`, unstripped) |
| **Umbrella module** | `ir.cpython-310…so` — re-exports the whole package; the 274-class registry |
| **SSA root** | `Value` (`ir/Value.py`) → C++ `pelican::Value` (`neuronxcc.pelican.ir`) |
| **Op-node base** | `Instruction` (`ir/Instruction.py`) — 51 methods, `ComputeValue`+`User` subclass; ~25 predicate methods |
| **HLO op layer** | `Operator` (`ir/Operator.py`) ⊂ `Instruction` — the §8 TensorOp family |
| **Use-list owner** | the **value** — `addUser`/`removeUser`/`replaceAllUsesWith` in `ComputeValue.so` |
| **Containment** | `Module ⊃ Function ⊃ BasicBlock ⊃ Stmt ⊃ Instruction` |
| **Confidence basis** | method roster = CONFIRMED (mangled `__pyx_pw_` symbol); field name = CONFIRMED; field *type/owner* = STRONG/INFERRED |

---

## The Master Class Registry

### Purpose

`ir.cpython-310…so` is the **umbrella re-export module**: importing `neuronxcc.starfish.penguin.ir` pulls every class out of every sibling module (`Value`, `User`, `ComputeValue`, `Tensor`, `Instruction`, `Operator`, `Axis`, `AffineExpr`, `Access`, `Dependency`, `Module`, `Function`, `Stmt`, …) into one namespace. Because it touches every class, its interned-name string pool **is** the package's master class list — the closest thing Penguin has to an op-registry enum. The backing analysis counts **274** IR class names in that pool after excluding the CPython runtime internals (`PyObject`, `PyTypeObject`, `PyListObject`, … — Cython-runtime types, not IR nodes).

> **NOTE — the 274 is a curated count, not a raw `strings` count.** A naive `rg -o '__pyx_n_s_[A-Z][A-Za-z0-9_]+' ir.cpython-310…so | sort -u` yields ~264 CamelCase interned names; the 274 figure is the IR-class set across the package (several classes live nested inside a sibling module's pool — e.g. `ScalarValue` inside `ComputeValue.so`, `TensorView`/`TensorUtils`/`TensorType` inside `Tensor.so`, `BasicBlock` inside `Function.so` — and are not separately interned in `ir.so`). The umbrella pool is the orientation list; the per-module pools are authoritative for the nested classes.

### Registration mechanism

There is no opcode enum and no `Op::classof` table. A node's "kind" is its **Python class** plus, for the typed leaves, an enum field:

```text
  ir.so  (umbrella)  ── re-exports every class name → one master pool (274)
    │
    ├─ Value.so       Value
    ├─ User.so        User                         (near-empty mixin)
    ├─ ComputeValue.so  ComputeValue, ScalarValue, wrap_compute_value()
    ├─ Tensor.so      Tensor, TensorType, TensorView, TensorUtils, …
    ├─ Instruction.so Instruction                  (51 methods)
    ├─ Operator.so    NullaryOp…TensorContractOp…  (the §8 TensorOp family)
    ├─ Axis.so        Axis, AffineAxis, DynamicAxis, SequentialAxis, AxisType
    ├─ AffineExpr.so  Expr, AffineExpr, SumExpr…    (thin wrappers over pelican::Expr)
    ├─ Access.so      Access, AffineAccess, AffineLoad/Store…
    ├─ Dependency.so  DependencyEdge, EdgeKind
    ├─ Module.so      Module, ModuleKind
    ├─ Function.so    Function, BasicBlock, AliasType
    └─ Stmt.so        Stmt, Block, StmtGroup, Macro, DAGMacro
```

The 274 classes group into nine node-model layers, each owned by a later 5.x page:

| Layer | Representative classes | Owned by |
|---|---|---|
| SSA value base | `Value`, `User`, `ComputeValue`, `ScalarValue`, `Instruction`, `Operator` | **this page** |
| Tensor / buffer | `Tensor`, `TensorType`, `TensorView`, `SingleValueTensor`, `BroadcastScalar`, `VNCAddrSpace` | [5.2](tensor-buffer-node.md) |
| Access-pattern | `Access`, `AffineAccess`, `AffineLoad/Store/AtomicRMW`, `Generic*` | [5.2](tensor-buffer-node.md) |
| Axis / loop | `Axis`, `AffineAxis`, `DynamicAxis`, `SequentialAxis`, `AxisType` | [5.3](axis-loop-model.md) |
| Affine-expr algebra | `Expr`, `AffineExpr`, `Sum/Mult/Modulo/FloorDiv/Compound`, `CC{Div,Mod}Expr` | [5.3](axis-loop-model.md) |
| Scope / region / CF | `Module`, `Function`, `BasicBlock`, `Stmt`, `Block`, `Macro`, `While`, `Branch*` | **this page** (§ containment) |
| High-level Operator | `NullaryOp`…`TensorContractOp`…`*TensorOp` family | [Operator family](tensor-op-family.md) |
| Native-kernel / NKI | `NativeKernel`, `NativeNkiKernel`, `BIRKernel`, `MLPKernel`, … | [Operator family](tensor-op-family.md) |
| Dependency / offloaded | `DependencyEdge`, `EdgeKind`, `Offloaded*` | [Dependency model](dependency-model.md) |

> **NOTE — canonical Part-5 sub-numbering.** The bracketed `[5.N]` tags elsewhere on this page are a page-local reading index, not the canonical Part-5 sequence. In the canonical Part-5 numbering, **§5.4 is the AffineExpr algebra over `pelican::Expr`** ([affine-expr-algebra.md](affine-expr-algebra.md)) and **§5.5 is the dependency model — `DependencyEdge` & `EdgeKind`** ([dependency-model.md](dependency-model.md)); the High-Level Operator (TensorOp) family lives at [tensor-op-family.md](tensor-op-family.md). The three rows above are linked by slug to avoid the `[5.4]`/`[5.5]` collision.

> **QUIRK — the "registry" is open, not closed.** Unlike an MLIR dialect (a fixed `Op` table registered at startup) or LLVM (a fixed `Instruction::Opcode` enum), Penguin's node set is just "whatever classes the package defines," discovered by import. A reimplementer building from the front half must emit *constructor calls* by class name (`MhloToPythonPrinter` does exactly this, [Part 4](../hlo-opt/mhlo-to-python-printer-driver.md)); there is no integer opcode to switch on until the BIR lowering ([Part 7](../bir/)).

---

## The SSA Value Base

### Purpose

The whole node model is one SSA def-use graph rooted at `Value`. The inheritance chain is confirmed from the import strings each module carries plus the mangled method rosters:

```text
  pelican::Value            (C++; neuronxcc.pelican.ir — the uniqued value)
        ▲   thin Python wrappers, every ir/*.py imports neuronxcc.pelican.ir
  Value           (ir/Value.py)        fields: name, kind
   └─ User         (ir/User.py)         the operand-holder mixin (near-empty)
       └─ ComputeValue (ir/ComputeValue.py)   the use-list + RAUW + DCE contract
            ├─ ScalarValue   a scalar SSA value (the one concrete class in ComputeValue.so)
            ├─ Tensor        a tensor-valued def                       → [5.2]
            └─ Instruction   an op that produces results               → §8, Operator family
                 └─ Operator   the HLO-op layer (TensorContractOp, …)  → Operator family
```

### Value and User

`Value` (`ir/Value.py`) is the base SSA value. Its two distinctive interned identifiers are `name` and `kind`: `name` is the SSA name string (which, on the BIR side, becomes the dependency-edge hash key — BIR keys its inline dep-sets by the target's *name*, [Part 7](../bir/)), and `kind` is a `ValueKind` tag. `Value.py` imports `neuronxcc.pelican.ir`, confirming it is the Python face of the C++ `pelican::Value`. *(CONFIRMED: `Value.py` path string, `pelican.ir` import string, `name`/`kind` in pool.)*

`User` (`ir/User.py`) is the LLVM-`User` analog — the operand-list holder that `Instruction`/`Operator` inherit. But the binary tells an important nuance:

> **GOTCHA — `User.so` is an almost-empty mixin; the use-list lives on the value.** `User.cpython-310…so` interns essentially nothing but its own name and an import of `Value` (`__pyx_n_s_User`, `__pyx_n_s_Value`, `User.py`, `User.c`, `PyInit_User`). It carries **no** `addUser`/`removeUser`/`operands` method strings of its own. The use-list machinery — `addUser`, `removeUser`, `replaceUseOfWith`, `replaceAllUsesWith`, `replaceAllUsesWithExcept` — is confirmed in **`ComputeValue.so`** (on the value being used), not in `User.so`. So the def-use edge is tracked **def-side**: a `Value` owns the list of `User`s that reference it, and a `User` reaches its operands through the inherited value accessors. A reimplementer who puts the use-list on the `User` (LLVM's layout) will mis-model RAUW.

### ComputeValue and ScalarValue — the value contract

`ComputeValue` (`ir/ComputeValue.py`) is the value that *computes* — it imports `Value`, `User`, `AffineExpr`, and support modules, and defines the use-list contract every concrete node inherits. The one concrete class **in** `ComputeValue.so` is `ScalarValue`, a scalar SSA value, whose 17-method roster is the clean statement of the contract (every method below CONFIRMED from a `__pyx_pw_9neuronxcc_8starfish_7penguin_2ir_12ComputeValue_11ScalarValue_<n><method>` wrapper symbol):

```c
class ScalarValue(ComputeValue):                 // ComputeValue.so, 17 pyx methods
    __init__, __eq__, __hash__, __str__
    // --- the def-use core (use-list + RAUW + operand rewrite) ---
    addUser, removeUser                          // the use-list edge insert/erase
    replaceAllUsesWith, replaceAllUsesWithExcept // RAUW (+ a one-exception variant)
    replaceUseOfWith                             // operand-level rewrite
    // --- analysis / folding ---
    indices_dfs                                  // walk the index DAG
    is_const, is_cast, is_floating, is_nan       // value classifiers
    stripCast                                    // peel cast nodes
    eval                                         // constant-fold
    recursivelyDCE                               // dead-code-eliminate the def cone
```

The module-level helper `wrap_compute_value` (CONFIRMED in `ComputeValue.so`) boxes a raw `pelican::Value` into a Python `ComputeValue` — the boundary crossing from the C++ uniqued layer back to the Python IR.

> **QUIRK — RAUW carries an "Except" variant.** `replaceAllUsesWithExcept` (CONFIRMED, distinct pyx symbol `…ScalarValue_17replaceAllUsesWithExcept`) replaces all uses *but one* — the pattern a rewriter uses when it must keep its own reference live while redirecting every other consumer. LLVM has no built-in for this; Penguin needs it because the rewriters run in Python and frequently hold the old value while substituting.

### Instruction — the op-node base

`Instruction` (`ir/Instruction.py`) is the base of every op node. It is a `ComputeValue`+`User` subclass (CONFIRMED: imports `neuronxcc.starfish.penguin.ir.ComputeValue`, `User`, `Value`, `Tensor`, plus the impl-binding back-edge `neuronxcc.starfish.penguin.targets.sunda.SundaISAInst`). Its roster is **exactly 51 distinct pyx methods** (CONFIRMED by counting `__pyx_pw_9neuronxcc_8starfish_7penguin_2ir_11Instruction_11Instruction_<n><method>` symbols). The fields (CONFIRMED present in pool; owner INFERRED to `Instruction`): `operands`, `results`, `attrs`, `_predicates`/`predicates`, `axes`, `loopnest`, `loopdepth`.

```c
class Instruction(ComputeValue, User):           // ir/Instruction.py, 51 pyx methods
    // fields (names CONFIRMED; types/offsets NOT recoverable — regular Python class)
    operands         // list of (Tensor, Access) operand bindings → [5.2]
    results          // list of result Tensors
    attrs            // open dict, keyed by op-specific names (Opcodes, dma_qos, …)
    _predicates      // list[AffinePredicate] — the loop-nest guards (the dominant feature)
    axes, loopnest, loopdepth   // the enclosing loop-axis nest → [5.3]

    // --- operand / result model ---
    operands, results, result_indices, result_shape, create_result_tensor,
    loadTensor, storeTensor, output_dependencies_indices_set, …
    // --- PREDICATE / affine-guard model (~25 of the 51 methods) ---
    predicates, is_predicated, addPredicate, resetPredicates,
    projectPredicates, approxPredicates, canonicalizePredicates, evalMasks,
    enumerate_predicates_in_codegen_order, apply_predicate_to, trivial_ub, …
    // --- dataflow / scheduling ---
    enumerate_use_insts, breakDefDataflow, enumerate_reduce_axes, has_reduce
    // --- typing / folding / io ---
    is_cast, stripCast, is_reduce_add, eval, serialize, __str__
```

The seven predicate methods named above are all CONFIRMED from individually-indexed pyx symbols (`…Instruction_53is_predicated`, `_61addPredicate`, `_65projectPredicates`, `_67approxPredicates`, `_71canonicalizePredicates`, `_75evalMasks`, `_81enumerate_predicates_in_codegen_order`).

> **QUIRK — predication, not branching, is the dominant feature.** Roughly half of `Instruction`'s 51 methods manage a list of `AffinePredicate` guards (`addPredicate`/`projectPredicates`/`evalMasks`/`canonicalizePredicates`/…). Each guard is an integer comparison over the loop-nest axes; `evalMasks` turns the list into the masking the engine applies. This is the Penguin twin of MLIR's `scf.if`/masking — **and the reason the CFG layer (`BasicBlock`/`Branch*`/`While`) is thin**: an affine-bounded conditional that LLVM would lower to a branch, Penguin keeps as a per-instruction predicate. A reimplementer who models conditionals only as CFG edges will fail to reproduce the tiler's masking semantics. See [5.3](axis-loop-model.md) for the `AffinePredicate`/`pelican::ICmpExpr` algebra.

### Operator — the HLO-op layer

`Operator` (`ir/Operator.py`) ⊂ `Instruction` is the high-level, HLO-facing op layer — the graph the front-half printer ([Part 4](../hlo-opt/mhlo-to-python-printer-driver.md)) and `GradIRBuilder` build. The base adds `__init__`, `rhs_str`, `serialize`, `verify`, `verifyOperandType` (CONFIRMED), and subclasses add per-op operands/indices/axes (the whole §8 TensorOp family, owned by [the Operator family page](tensor-op-family.md)). Its distinctive module helpers are the **axis-role queries** the tiler and scheduler use to classify each `Axis` of an op (all CONFIRMED in `Operator.so`):

```c
// Operator.so module-level axis-role classifiers (used by the layout/tiling middle-end)
used_as_reduce_axis(op, axis)       // axis is reduced over
used_as_contract_axis(op, axis)     // axis is a matmul contraction dim
used_as_lhs_free_axis(op, axis)     // free dim of the stationary operand
used_as_rhs_free_axis(op, axis)     // free dim of the streaming operand
used_as_reduce_like_axis(op, axis)
```

These roles are what the layout solver and tiler read to assign each axis to Partition / Free / Block — the bridge from this node model into the P/F/B tiling vocabulary ([5.9](ir-mlir-bir-mapping.md), and the broader tiler in the [Part 5 overview](tensor-op-family.md)). `make_cast`/`custom_op`/`act_identity` are the cast and custom-op constructors. *(CONFIRMED: `used_as_*_axis`, `verifyOperandType`, `make_cast` in `Operator.so` pool.)*

---

## The Def-Use Edge Representation

A reimplementer must be precise about *which* graph an edge belongs to, because Penguin maintains **two** distinct, co-existing edge structures, and conflating them is the classic mistake.

### Layer 1 — the SSA use-list (always present)

The primary def-use graph is the use-list described above: a `Value` owns the list of `User`s that reference it (`addUser`/`removeUser`), RAUW redirects them (`replaceAllUsesWith{,Except}`), and operand rewriting (`replaceUseOfWith`) edits a single edge. This is intrinsic to the IR — it exists the moment a node is constructed, and it is what DCE (`recursivelyDCE`) walks.

For `Tensor` values specifically, this use-list is overlaid with a **memory-access** view, because a tensor is read and written through `Access` descriptors rather than as a scalar SSA operand (CONFIRMED methods in `Tensor.so`):

```c
class Tensor(ComputeValue):                      // ir/Tensor.py — the def-use overlay
    loads, stores                                // the load/store accesses on this tensor
    accessing_insts, sorted_users                // the instructions that touch it
    link_use_inst, unlink_use_inst               // insert/erase a use edge
    single_assign, single_def_single_use         // SSA-ness queries
    replaceAllUsesWith, replaceUseOfWith         // RAUW on the tensor
```

So for a tensor, "who uses this value" is answered by `accessing_insts`/`loads`/`stores`, and the edge is maintained by `link_use_inst`/`unlink_use_inst`. The full `Tensor` schema (119 methods — shape/dtype/layout/placement) is [5.2](tensor-buffer-node.md)'s subject; here the point is only that **`Tensor` is a `ComputeValue` and its uses are the load/store accesses**.

### Layer 2 — the Function-level dependency graph (scheduling)

The **scheduling** edges are a separate, first-class structure stored on the `Function`, not on the value:

```c
class DependencyEdge:                             // ir/Dependency.py — CONFIRMED
    src   // an Instruction
    dst   // an Instruction
    kind  // Optional[EdgeKind]          (CONFIRMED type annotation "Optional[EdgeKind]")
    __init__, serialize

enum EdgeKind { FLOW, ANTI, OUTPUT, ORDERED }    // CONFIRMED members in Dependency.so pool
    // FLOW = RAW (true producer→consumer); ANTI = WAR; OUTPUT = WAW;
    // ORDERED = scheduler/programmer-injected ordering
```

The edges are held by the `Function` (CONFIRMED in `Function.so`: `add_dep_edge`, `dep_edges`, `dep_edges_for_inst`, plus `_dep_edges` as the backing field):

```c
class Function:                                  // ir/Function.py
    add_dep_edge(src, dst, kind), remove_dep_edge(edge)
    dep_edges                                    // the DependencyEdge set
    dep_edges_for_inst(inst), depending_insts(inst)
    replace_inst_in_dependencies, replace_with_list_in_dependencies
```

> **GOTCHA — two graphs, two owners. Do not store dependency edges on the instruction.** The SSA use-list (Layer 1) is on the *value*; the scheduling dependency graph (Layer 2) is a flat list of `DependencyEdge` objects on the *Function*. This is the inverse of BIR ([Part 7](../bir/)), where the C++ `bir::Instruction` stores its edges **inline** in three concurrent dep-sets keyed by the target name, MAX-merging a duplicate `(A→B)` to the strongest `EdgeKind`. The Penguin Function-level list is the **source**; the per-instruction inline name-keyed MAX-merged form is a BIR-side representation choice applied during lowering ([5.9](ir-mlir-bir-mapping.md)). The dep-analysis passes (anti-dependency analysis, ordering constraints) build Layer 2 *on top of* Layer 1; they never replace it.

`EdgeKind`'s member set `{FLOW, ANTI, OUTPUT, ORDERED}` matches BIR's `bir::EdgeKind` `{Invalid0, Ordered1, Anti2, Output3, Flow4}` one-to-one (Penguin omits the `Invalid0` sentinel). The **numeric** values on the Penguin side are not directly recovered (the members are CONFIRMED, the integer ordering is INFERRED from the BIR twin).

---

## The Container Nesting

### Purpose

The op graph is wrapped in a lexical container hierarchy, confirmed module-by-module from method rosters and docstrings:

```text
  Module      { functions, subgraph_functions, root_function, kind }
    └─ Function   { args, inputs, outputs, tensors, dep_edges, opt_level, target }
         └─ BasicBlock        (nested in Function.so)
              └─ Stmt   { Block(+block-local tensors) / StmtGroup / Macro / DAGMacro }
                   └─ Instruction / Operator
                        { operands=(Tensor,Access), results=Tensor,
                          attrs, predicates=[AffinePredicate], axes=AffineAxis-nest }
```

### Module

`Module` (`ir/Module.py`, 14 methods + a `ModuleKind` enum) is the top container. Its docstring is verbatim in the pool: *"Modules contains a list of connected functions forming a neural network."* Fields (CONFIRMED): `functions`, `subgraph_functions`, `root_function`, `kind`. The assert *"expect exactly one function with attr 'main'"* (CONFIRMED string) fixes the invariant: a `Module` has exactly one main `Function`; `subgraph_functions` are the callee sub-graphs. *(CONFIRMED: docstring, `subgraph_functions`/`root_function`/`has_root_function` in pool.)*

### Function

`Function` (`ir/Function.py`, 53 methods) is the compilation unit — the object the `IRBuilder` ("cu") builds into. Fields (CONFIRMED): `args`, `inputs`, `outputs`, `tensors` (with `ordered_all_tensors`/`findTensorByName`), `dep_edges` (Layer 2 above), `opt_level`, `target`. Nested classes: `BasicBlock`, `AliasType`. Its method groups cover blocks (`addBasicBlock`, `stmts_under_bb`), IO marking (`markInput`/`markOutput`/`ioTensors`), the alias model (`aliasTensors`/`findAliasTensor`/`buildAliasTensorMap`), the dependency graph (above), SPMD/sharding (`createShardId`/`replica_groups_table`), cost (`isComputeBound`/`isMemoryBound`), and serialization (`serialize_ir_string`/`verify`/`finalize`). *(CONFIRMED: `addBasicBlock`, `add_dep_edge`, `dep_edges`, `BasicBlock` nested, `ordered_all_tensors`.)*

### BasicBlock and the Stmt layer

`BasicBlock` (nested in `Function.so`: `__init__`, `__str__`, `verify`) is the CFG node holding an ordered `Stmt` sequence — the Penguin analog of an MLIR `Block` / `bir::BasicBlock`. Inside a `BasicBlock`, the loop-nest/grouping wrappers live in `Stmt.so` (CONFIRMED nested classes `Block`, `Macro`, `DAGMacro`, `StmtGroup`):

```c
class Stmt:                                       // ir/Stmt.py
    Block      // the loop-body / tensor-scope statement; OWNS block-local tensors:
               //   addTensor, all_tensors, dropDeadTensors, stealChildren  (CONFIRMED)
    StmtGroup  // an ordered group of statements
    Macro      // a fused tile-level body (TiledSoftmax/TiledRmsNorm-style)
    DAGMacro   // a macro whose body is a DAG (vs a linear Macro)
```

A `Block` **owns** the tensors declared in its scope (block-local tensors) via `addTensor`/`all_tensors`/`dropDeadTensors`/`stealChildren` (CONFIRMED). `Macro`/`DAGMacro` are the schedulable fused units the tiler emits — they wrap a span of the §3-Inst roster into one node ([the Operator family page](tensor-op-family.md)).

### Control-flow markers — two co-existing forms

Beyond the affine-predicate masking that dominates, Penguin keeps two explicit control-flow forms:

```c
// (a) STRUCTURED — ir/StructuralControlFlow.py
class While:                                      // CONFIRMED docstring (verbatim):
    """A while loop, with the following cannonical structure:
       >>> if guard:
       >>>   while (continue_condition) ..."""
    guard, continue_condition (set_continue_condition), is_do_while, name

// (b) UNSTRUCTURED CFG — ir/Branch.py
class BranchInst:               __init__, isTriviallyDead
class ConditionalBranchInst:    condition (field), operands, serialize   // BB→BB edge w/ cond
class UnconditionalBranchInst:  operands, serialize                      // terminator
```

`While` is the structured data-dependent loop (guard + `continue_condition` + body), distinct from the affine-bounded `DynamicAxis` ([5.3](axis-loop-model.md)). `ScopeRegion` (`ir/ScopeRegion.py`, 7 methods) is a lighter named lexical region — a labeled scope-begin/scope-end pair bracketing a span of statements for analysis/codegen (e.g. a fusion-cluster or kernel boundary), with fields `parent`/`end`. `OptBarrier`/`CoreBarrierIntrinsic` are the no-reorder scheduling fences. *(CONFIRMED: `While` docstring + `continue_condition`/`is_do_while`/`guard`; `Branch*` rosters.)*

---

## The Round-Trip — Penguin IR as Runnable Python

A consequence of the open, constructor-based registry is that the IR's textual form **is** runnable Python. `IRWriter` (`ir/IRWriter.py`, 37 methods) is the textual emitter; it writes Python constructor calls and class assignments, and `roundtripIO` reads them back by executing the emitted code (CONFIRMED: `IRWriter.roundtripIO`, `IRWriter.roundtripIO.<locals>.read_ir_from_code`). Its `serialize_*` helpers (CONFIRMED) are: `serialize_func_begin`, `serialize_basic_block_begin`, `serialize_dep_edge`, plus axis/dtype/block-end emitters.

> **NOTE — the printer and the in-memory IR share one textual form.** The Python-constructor surface `MhloToPythonPrinter` emits from the MLIR front half ([Part 4](../hlo-opt/mhlo-to-python-printer-driver.md)) is the **same** surface `IRWriter` writes for round-trip. `serialize_dep_edge` confirms the `DependencyEdge` graph is part of the serialized IR — the dependency edges survive a write/read cycle. Two serialize-format anchors are verbatim in the pools and pin the leaf nodes: a `Tensor` prints as `{attr}{dtype} {shape} {name}{init}` (CONFIRMED, `Tensor.so`), and an `AffineAxis` loop prints as `for ({it}: range({lb}, {ub}, {stride}))` (CONFIRMED, `Axis.so`) — a Penguin loop axis literally *is* a Python `for`-over-`range`.

---

## Compiled-Opaque Limits (honest gaps)

These are the boundaries of what the binary yields, stated so a reimplementer does not over-trust the schema:

- **Field types and offsets are not recoverable.** These IR classes are *regular* Python classes — Cython emitted no C-level `__pyx_obj_…<Class>` instance struct and no `tp_members`/`tp_getsets` for them, so their fields live in `__dict__`. The string pool gives field **names** (CONFIRMED present in the module) but not their static types or byte-offsets. A field's **owning class** is CONFIRMED only when a method roster, docstring, or serialize-format pins it; cross-cutting names like `shape`/`axes` are attributed by nearest method context (tagged STRONG/INFERRED in the per-family pages).
- **`EdgeKind` integer values** are INFERRED from the BIR twin, not directly recovered on the Penguin side (members CONFIRMED).
- **The `pelican` C++ layer is stripped.** `neuronxcc.pelican.cpython-310…so` ships **no** `debug_info`; the `pelican::Value`/`pelican::Expr` hierarchy is reconstructed from typeinfo/RTTI and assert source-lines only ([5.3](axis-loop-model.md)). Member offsets of the C++ uniqued-value layout are not recovered, and the Python↔pelican method correspondence is *named*, not traced.
- **The `attrs` dictionary schema is open.** `Instruction.attrs`/`get_attrs_dict` is an open dict keyed by op-specific names (Opcodes, `dma_qos`, `dge_mode`, `engine`, …); a full per-op attr-key table is out of this node-schema page's scope (it needs the generated `<Op>Gen` property lists, [Part 4](../hlo-opt/)).

---

## Related Components

| Component | Relationship |
|---|---|
| `MhloToPythonPrinter` ([Part 4](../hlo-opt/mhlo-to-python-printer-driver.md)) | Emits the Python constructor calls that build this node graph — the upward (MLIR→Penguin) boundary |
| `IRBuilder` ([Part 4](../hlo-opt/)) | The construction surface ("cu") that materializes these nodes into a `Function` |
| `BirCodeGenLoop` ([Part 7](../bir/)) | Walks the lowered node graph and emits C++ `bir::` nodes — the downward (Penguin→BIR) boundary |
| `pelican::Value` / `pelican::Expr` | The C++ uniqued-value and affine-expr layer every Python node wraps |

## Cross-References

- [Tensor & Buffer Node Families](tensor-buffer-node.md) — the `Tensor`/`Access` schema and the SBUF/PSUM/DRAM placement
- [Axis & Loop-Axis Node Families](axis-loop-model.md) — `AffineAxis`/`AxisType` and the loop-axis nest
- [§5.4 — AffineExpr Algebra over pelican::Expr](affine-expr-algebra.md) — the quasi-affine address algebra (`Sum`/`Mult`/`Modulo`/`FloorDiv`/`CC{Div,Mod}`) behind every `Access`
- [§5.5 — Dependency Model — DependencyEdge & EdgeKind](dependency-model.md) — the `DependencyEdge` graph, `EdgeKind` taxonomy, and host-offloaded primitives
- [High-Level Operator (TensorOp) Family](tensor-op-family.md) — the §8 TensorOp roster (`TensorContractOp`, reductions, fused macros) plus the NKI-kernel embedding and collective op nodes
- [Penguin → BIR Node Mapping](ir-mlir-bir-mapping.md) — the per-node `codegen<Op>` correspondence and the dep-edge MAX-merge
- [Part 4 — hlo2penguin / MhloToPythonPrinter](../hlo-opt/mhlo-to-python-printer-driver.md) — the C-strand front half that emits textual Penguin
- [Part 7 — BIR & libBIR](../bir/) — the C++ IR this node graph lowers into
