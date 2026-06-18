# IslCodeGen — ISL-AST → Penguin IR Loop Regeneration

> **Module:** `neuronxcc/starfish/penguin/IslCodeGen.cpython-310-x86_64-linux-gnu.so`
> (Cython, **unstripped**, ~1.51 MB). Module docstring, recovered verbatim from the
> string pool: **`IslCodeGen -- Generate tensoriser IR from ISL AST`**
> (`__pyx_k_IslCodeGen_Generate_tensoriser`, CONFIRMED).
>
> **Position:** the *back end* of the Penguin polyhedral path. After a loop-transform
> client builds and **validates** an `isl` schedule tree against the dependence relation
> (see [§5.16 ISL Dependence Graph](isl-dependence-graph.md)) and lowers it to an
> `isl.AstNode` tree, this module **walks that tree and re-emits Penguin tensorizer IR**:
> `for` nodes become loop [`Axis`](axis-loop-model.md) nodes, statement instances become
> the original `Inst` re-inserted, and affine bounds/strides become Penguin affine exprs.
> It is the structural **inverse** of the §5.16 domain construction
> (`Inst → isl domain`  ⇒  `isl AST → Inst/Axis`). Used by the software-pipelining and
> tiling/fusion passes ([§5.14](software-pipelining.md)).

---

## 1. Why a separate codegen, and what it consumes

In this build, ISL is **validation-only** — there is no Pluto scheduler. The transform
clients build an `isl` schedule tree by hand, check it for legality, then ask `islpy`'s
AST builder to produce a concrete `isl.AstNode` / `isl.AstExpr` tree describing the
generated loop nest. `IslCodeGen` never *builds* that AST; it only consumes it.

> **NOTE — the AST builder lives in the client, not here.** Neither
> `node_from_schedule` nor `AstBuild` appears anywhere in this `.so`'s string pool
> (CONFIRMED: `strings | rg 'schedule|AstBuild|node_from'` is empty). The only `isl`
> entry points this module references are the **read-side** AstNode/AstExpr accessors
> (`for_get_iterator`, `block_get_children`, `user_get_expr`, `get_op_arg`, …). So the
> schedule→AST lowering is upstream; this module is purely the AST→IR re-emitter.

```
loop-transform client
   ├─ build isl schedule tree                 (§5.16)
   ├─ validate against dependence relation     (§5.16)
   ├─ islpy: schedule → isl.AstNode tree        (in the CLIENT, not here)
   └─ IslCodeGen(ast_build, cu, insert_before, dl).codegenNode(root)   ◀── THIS MODULE
         → Penguin IR: Axis loop nest + re-emitted Inst bodies
           spliced into the IR via builder.insert_before(curstmt, …)
```

---

## 2. Two-class split: walker skeleton vs. tensorizer concretion

The module ships the same **Gen-base + override** shape seen elsewhere in Penguin
(cf. the `NkiCodegen` / `KlirToBirCodegen` pair). Two Cython classes, plus three
module-level helpers and a small `IdWrapper` value type.

| Class (pyx prefix) | Role |
|---|---|
| `IslCodeGenBase` (`14IslCodeGenBase`) | reusable AST-walker skeleton; dispatches every `isl.AstNode` kind and decodes every `isl.AstExpr` into Penguin affine exprs / predicates. Its **target-specific hooks are abstract**. |
| `IslCodeGen(IslCodeGenBase)` (`10IslCodeGen`) | the loop-regeneration tensorizer concretion; overrides the four abstract hooks + `codegenForBody`, adds `addNewBlock`, owns the `IRBuilder`, the compilation unit `cu`, and the `gen_top_loops` result accumulator. |

The class names are confirmed from the pyx symbol prefixes (`nm | rg IslCodeGen`).

### 2.1 Abstract hooks (base bodies are `raise NotImplementedError()`)

Four base methods are pure stubs; the `.so` carries **6** `NotImplementedError`
string references (CONFIRMED), accounting for the four base hooks (two share the
unbound-name path):

| Hook | `IslCodeGenBase` (abstract) | `IslCodeGen` override |
|---|---|---|
| `codegenForInit(expr)` → lower bound | `raise NotImplementedError()` | §3.2 |
| `codegenForCond(it, cond)` → upper bound | `raise NotImplementedError()` | §3.3 |
| `createAxis(it, lb, ub, stride)` | `raise NotImplementedError()` | §3.1 |
| `codegenUser(id, indices)` → statement re-emit | `raise NotImplementedError()` | **NOT overridden here** (see §2.4) |

### 2.2 Imports (from `__pyx_pymod_exec_IslCodeGen`)

All CONFIRMED in the string/symbol pool:

```python
import operator                                              # binary/unary functors
import islpy as isl                                          # ast_node_type / ast_op_type / ast_expr_type
from neuronxcc.starfish.penguin.SCEV import scev             # imported; see GOTCHA below
from neuronxcc.starfish.penguin.common import AttrRAII, DictRAII
from neuronxcc.starfish.penguin.ir.IRBuilder import IRBuilder
import neuronxcc.starfish.penguin.ir.ir                      # Axis, CExpr, wrap_expr, infimum, supremum
from neuronxcc.starfish.support.LogContext import print_debug
```

> **GOTCHA — `scev` is imported but never called inside the codegen methods.**
> No `codegen*` body references it; it is re-exported for the affine-expr machinery
> (`CExpr` / `wrap_expr`) the client subclass uses, or kept live for the IR layer.
> Flagged INFERRED.

### 2.3 Instance state

```c
// IslCodeGenBase.__init__(self):
self.ids        = {}    // dict  IdWrapper(iterator AstExpr) -> Axis   (loop-iterator binding)
self.predicates = []    // stack of active if/loop guard predicates
self.params     = {}    // symbolic-parameter map (params NOT bound to a loop axis)

// IslCodeGen.__init__(self, ast_build, cu, insert_before, dl):
super().__init__();                                  // chains the base ctor
self.ast_build = ast_build;                          // the isl.AstBuild that produced the AST
self.cu        = cu;                                 // compilation unit; source of allocateId()
self.builder   = IRBuilder(...);                     // penguin.ir.IRBuilder
self.builder.insert_before = insert_before;          // the splice point
self.builder.curstmt       = insert_before.parent;   // stmt the loops splice before
self.builder.updateDebugLoc(dl);                     // propagate debug location
self.gen_top_loops = [];                             // accumulator: the regenerated top-level Axes
```

Field names `ids`, `params`, `ast_build`, `cu`, `gen_top_loops`, `natural_axis`,
`insert_before`, `curstmt`, `allocateId`, `updateDebugLoc` are all CONFIRMED string-pool
entries. The `__init__` argument order `(self, ast_build, cu, insert_before, dl)` is
CONFIRMED from `__pyx_pyargnames[]`.

### 2.4 The statement re-emit (`codegenUser`) is left abstract here

`IslCodeGen` overrides `createAxis`, `codegenForInit`, `codegenForCond`, and
`codegenForBody` — but it does **not** override `codegenUser`. That means the
*fully concrete* loop regenerator (the one that maps a statement name `sN` back to the
original Penguin `Inst` and re-inserts it) is a **further subclass** living in the
tiling/fusion/sw-pipeline client, not in this `.so`. This module gives you the loop
*structure* (Axes + bounds) and the *index expressions*; the body re-instantiation
is one layer up.

> **QUIRK — `sN ↔ Inst` table lives upstream.** §5.16 built a statement-name↔`Inst`
> table when it constructed the `isl` domain (each domain tuple is named `sN`). The
> concrete `codegenUser` indexes back into *that* table. Because this `.so`'s
> `codegenUser` is the abstract stub, the table lookup is invisible here — only the
> *protocol* (`codegenUser(id, indices)`) is fixed by `IslCodeGenBase`.

---

## 3. The node walker (`isl.AstNode` kind → Penguin construct)

### 3.1 `codegenNode` — the top dispatcher

Dispatches on `node.get_type()`. CONFIRMED string-pool sequence:
`get_type` → `ast_node_type.for_/user/block/if` → `codegenFor/codegenUserOp/codegenBlock/codegenIf`,
else the `"Not implemented node ("` diagnostic.

```c
void codegenNode(self, node) {                 // @111  IslCodeGenBase
    t = node.get_type();
    if      (t == isl.ast_node_type.for_)   codegenFor(node);     // ◀ Python keyword → isl spells it `for_`
    else if (t == isl.ast_node_type.user)   codegenUserOp(node);  //   (CONFIRMED: __pyx_k_for_)
    else if (t == isl.ast_node_type.block)  codegenBlock(node);
    else if (t == isl.ast_node_type.if_)    codegenIf(node);
    else raise_err("Not implemented node (" + node.to_C_str() + ")");
}
```

The trailing `")"` of the f-string is `__pyx_kp_u__16`. The `user` node routes to
`codegenUserOp` (which unwraps the call expr), **not** to `codegenUser` directly.

### 3.2 `codegenFor` — the core `for` → loop-`Axis` mapping (`IslCodeGenBase` @233)

The pivotal method. It pulls the four `for`-node components, lowers each, builds the
`Axis`, then recurses into the body under two **scoped RAII** contexts. CONFIRMED
sequence: `for_get_iterator`/`init`/`inc`/`cond` → 2×`print_debug` →
`codegenForInit` → `codegenForCond` → `codegenExpr` → `createAxis` →
`AttrRAII('predicates')` `__enter__`/`__exit__` →
`DictRAII(self.ids,…)` `__enter__`/`__exit__` → `codegenForBody` → `IdWrapper`.

```c
void codegenFor(self, node) {                     // @233
    it   = node.for_get_iterator();               // an isl.AstExpr of kind `id`
    init = node.for_get_init();                   // lower-bound expr
    inc  = node.for_get_inc();                     // stride expr (int AstExpr in practice)
    cond = node.for_get_cond();                    // guard:  i < N  or  i <= N
    print_debug(... "for" ...); print_debug(...);  // 2× LogContext trace

    lb     = self.codegenForInit(init);            // -> CExpr / affine     (§3.2 below)
    ub     = self.codegenForCond(it, cond);        // -> CExpr / affine     (§3.3 below)
    stride = self.codegenExpr(inc);                // -> affine (usually CExpr(int))
    axis   = self.createAxis(it, lb, ub, stride);  // build Axis + splice into IR  (§4.1)

    // scope the iterator binding and the predicate stack to the body ONLY:
    with AttrRAII(self, 'predicates', ...):              // save/restore self.predicates
        with DictRAII(self.ids, {IdWrapper(it): axis}):  // temporarily bind ids[it] = axis
            self.codegenForBody(node, axis);
}
```

Two scoping disciplines make nested loops correct:

- **`AttrRAII(self, 'predicates', …)`** (`penguin.common`) saves `self.predicates` on
  `__enter__` and restores it on `__exit__`, so guard predicates accumulated *inside*
  the body do not leak to sibling subtrees.
- **`DictRAII(self.ids, {IdWrapper(it): axis})`** is a scoped dict-overlay: it inserts
  the `iterator → axis` binding for the body's lifetime and removes it on exit. This is
  exactly what lets `codegenId` (§5.7) resolve an index reference to that iterator back
  to *this* loop's `Axis`.

> **GOTCHA — `for_` not `for`.** `for`/`if` are Python keywords, so `islpy` exposes the
> node-type members as **`ast_node_type.for_`** and **`ast_node_type.if_`** (CONFIRMED:
> `__pyx_k_for_`, and the `if`/`block`/`user` literals in the pool). The report's
> pseudocode writes `for`/`if`; the binary spelling is the trailing-underscore form.

### 3.3 `codegenBlock` — sibling sequence (`IslCodeGenBase` @105)

```c
void codegenBlock(self, node) {                   // @105
    node.block_get_children().foreach(            // isl AstNodeList.foreach (not Python iter)
        lambda n: self.codegenNode(n));
}
```

The lambda (`codegenBlock.<locals>.<lambda>`) takes one arg `n` and forwards to
`codegenNode`. The block node is a flat sequence of sibling AST nodes.

### 3.4 `codegenIf` — guard predicate, then-branch only (`IslCodeGenBase` @76)

```c
void codegenIf(self, node) {                      // @76
    assert(!node.if_has_else());                  // else-branch is UNSUPPORTED here
    cond = node.if_get_cond();
    if (cond.get_type() == isl.ast_expr_type.op)
        pred = self.codegenExpr(wrap_expr(cond));  // build predicate (AND-folds via infimum)
    with AttrRAII(self, 'predicates', <push pred>):
        self.codegenNode(node.if_get_then());      // emit then-body under the guard
}
```

CONFIRMED sequence includes `if_has_else`, `if_get_cond`, `if_get_then`,
`ast_expr_type.op`, `codegenExpr`, `infimum`, 2×`wrap_expr`, `AttrRAII`.

> **QUIRK — no `else` handling.** `if_has_else()` is checked **first**; an else-branch
> would require a complementary-predicate path this re-emitter does not model. The
> validated schedule it consumes is expected to produce guard-only ifs (the polyhedral
> AST generator can be driven to do so). The predicate is pushed via `AttrRAII` so it is
> active only while emitting the then-body — matching `codegenFor`'s discipline.

### 3.5 `codegenUserOp` — statement instance `sN[i, j, …]` (`IslCodeGenBase` @95)

`isl` emits each statement instance as a **`call`** op-expression: `op_arg(0)` is the
statement name `sN` (an `id` AstExpr matching the §5.16 domain tuple name), and
`op_arg(1..)` are the per-dimension subscript exprs.

```c
list codegenUserOp(self, node) {                  // @95
    expr = node.user_get_expr();
    if (expr.get_type() != isl.ast_expr_type.op)
        raise_err("Unexpected user expr type!");
    if (expr.get_op_type() != isl.ast_op_type.call)
        raise_err("unexprect user op type!");      // (sic) typo PRESERVED from source binary
    n       = expr.get_op_n_arg();
    stmt_id = expr.get_op_arg(0);                  // the statement id sN (isl Id expr)
    indices = [ self.codegenExpr(wrap_expr(expr.get_op_arg(i)))  // each subscript -> affine
                for i in range(1, n) ];
    return self.codegenUser(stmt_id, indices);     // abstract here -> client re-emits the Inst
}
```

> **QUIRK — the typo is real.** The diagnostic literal is `"unexprect user op type!"`,
> with the `unexprect` typo, byte-for-byte (CONFIRMED: `__pyx_k_unexprect_user_op_type`).
> It is not a transcription error in this page.

---

## 4. The for-node lowering hooks (`IslCodeGen` overrides → `Axis` construction)

### 4.1 `createAxis(it, lb, ub, stride)` — `for` → Penguin `Axis` (`IslCodeGen` @283)

The concrete loop builder. CONFIRMED string-pool sequence:
`extractIdName` → `self.builder` → `builder.cu.allocateId` → `Axis(...)` →
`builder.curstmt` → `builder.insert_before` → `addNewBlock`.

```c
Axis createAxis(self, it, lb, ub, stride) {        // @283
    name   = self.extractIdName(it);               // iterator name, e.g. "c0", "c1"
    new_id = self.builder.cu.allocateId();         // fresh Penguin node id (PyLong)
    axis   = Axis(name=name, id=new_id,            // ir.ir.Axis — kwargs built dynamically
                  lb=lb, ub=ub, stride=stride);    //   (PyDict_New + PyDict_SetItem keyword call)
    self.builder.insert_before(self.builder.curstmt, axis);   // splice axis BEFORE current stmt
    self.addNewBlock(<block derived from axis/new_id>);       // open child block scope (§4.5)
    return axis;
}
```

- **`cu.allocateId()`** gives the `Axis` a unique id in the compilation unit's id-space —
  the same allocator `penguin.ir` uses for every node (CONFIRMED `builder → cu → allocateId`
  chain).
- **`Axis(...)`** is `neuronxcc.starfish.penguin.ir.ir.Axis`, the Penguin loop-axis IR
  node (the representation of an isl `for`). Its kwargs carry the regenerated bounds.
- **`insert_before(curstmt, axis)`** emits the axes as a *prefix* before the statement
  insertion point, building the loop nest that encloses the re-emitted body.

> **INFERRED — exact `Axis(...)` kwarg names.** The kwargs are assembled dynamically via
> `PyDict_SetItem` (empty positional tuple + keyword dict), so the literal keyword names
> `lb`/`ub`/`stride`/`id`/`name` are reconstructed from the call-site dataflow and the
> `penguin.ir.ir` `Axis` schema, not read byte-exact. The `PyNumber_Multiply()` +
> `PyNumber_Add()` near the `addNewBlock` call compute the derived block-id argument
> (axis id combined with current nesting depth) — also INFERRED.

### 4.2 `codegenForInit` — the lower bound (`IslCodeGen` @307)

`isl` emits a loop lower bound as a literal int, a single affine expr, or a
**`max(...)`** of several affine candidates (when multiple constraints lower-bound the
iterator). The `max` folds to Penguin's **`infimum`** combinator. CONFIRMED sequence:
`dyn_cast_int` → `expr_operands` → `ast_op_type.max` → `codegenExpr` → `infimum` →
`"Unsupported expression!"` → `CExpr`.

```c
CExpr codegenForInit(self, expr) {                 // @307
    v = dyn_cast_int(expr);
    if (v != None) return CExpr(v);                // constant LB -> wrapped constant
    (op, args) = expr_operands(expr);
    if (op == isl.ast_op_type.max)                 // lb = max(lb0, lb1, ...)
        return infimum([ self.codegenExpr(a) for a in args ]);  // Penguin affine-domain meet
    else
        return self.codegenExpr(expr);             // general single-affine LB
    // ill-formed kinds -> raise_err("Unsupported expression!")
}
```

### 4.3 `codegenForCond` — the upper bound (`IslCodeGen` @332)

The mirror image. It decodes the guard comparison, verifies one operand is literally the
loop iterator `it`, and treats the other as the bound. `isl` emits the upper bound as a
literal, a single affine, or a **`min(...)`** of candidates — `min` folds to Penguin's
**`supremum`**. CONFIRMED sequence: `expr_icmp` → `ast_op_type.lt` → `ast_op_type.le`
→ `"Unexpected predicate"` → `"Expect compare against iterator!"` → `dyn_cast_int` →
`expr_operands` → `ast_op_type.min` → `codegenExpr` → `supremum` →
`"Unsupported expression!"` → `CExpr`, plus the comprehension closure `genexpr1`
(`codegenForCond.<locals>.genexpr1`) for the `min`→`supremum` fold.

```c
CExpr codegenForCond(self, it, cond) {             // @332
    (lhs, rhs) = expr_icmp(cond);                  // split the comparison's two operands
    op = cond.get_op_type();
    if (op != isl.ast_op_type.lt && op != isl.ast_op_type.le)
        raise_err("Unexpected predicate");         // only `<` / `<=` are canonical guards

    // exactly ONE side must be the loop iterator `it`; the other is the bound expr:
    if      (extractIdName(lhs) == extractIdName(it)) ub_expr = rhs;
    else if (extractIdName(rhs) == extractIdName(it)) ub_expr = lhs;
    else raise_err("Expect compare against iterator!");

    v = dyn_cast_int(ub_expr);
    if (v != None) return CExpr(v);                // constant UB
    (op2, args) = expr_operands(ub_expr);
    if (op2 == isl.ast_op_type.min)                // ub = min(ub0, ub1, ...)
        return supremum([ self.codegenExpr(a) for a in args ]);   // genexpr1 closure
    else
        return self.codegenExpr(ub_expr);          // general single-affine UB
    // ill-formed -> raise_err("Unsupported expression!")
}
```

> **The bound symmetry.** Lower bound uses `max` → `infimum`; upper bound uses
> `min` → `supremum`. These are the affine-domain **meet** and **join** in the Penguin
> expression algebra: the lower envelope of candidate LBs and the upper envelope of
> candidate UBs. `infimum`/`supremum` are both CONFIRMED string-pool entries.

> **INFERRED — the iterator-side selection.** The binary *does* compare one side against
> `it` and raises `"Expect compare against iterator!"` otherwise; the exact comparison
> primitive (`extractIdName` equality vs. `IdWrapper.__eq__`) is reconstructed from the
> presence of `expr_icmp` + `extractIdName` in the call sequence. Tagged INFERRED.

### 4.4 `codegenForBody` — push `natural_axis`, recurse (`IslCodeGen` @279)

```c
void codegenForBody(self, node, natural_axis) {    // @279  (override)
    with AttrRAII(self.builder, 'natural_axis', natural_axis):   // set active loop axis
        self.codegenNode(node.for_get_body());
}
```

`natural_axis` is the `Axis` just built by `createAxis`; it is set on the **`IRBuilder`**
for the body's duration so that re-emitted `Inst` nodes (in the client's `codegenUser`)
know which loop level they sit under. CONFIRMED `natural_axis` string + the
`AttrRAII(self.builder, …)` enter/exit pair.

> **NOTE — base vs. override.** `IslCodeGenBase.codegenForBody` (@230) is the **default**,
> *not* abstract: it simply calls `self.codegenNode(node.for_get_body())` with no
> `AttrRAII`. The `IslCodeGen` override adds only the `natural_axis` push — the rest is
> identical.

### 4.5 `addNewBlock` — record the top-level loop nest (`IslCodeGen` @300)

```c
void addNewBlock(self, block) {                    // @300
    // register the new (top-level) loop nest/block into the result accumulator:
    self.gen_top_loops.append(<genexpr over block>);   // closure addNewBlock.<locals>.genexpr
}
```

`gen_top_loops` is the **result accumulator**: as `createAxis` builds each outermost
`Axis`, it calls `addNewBlock` to record the new top-level loop block. After
`codegenNode(root)` returns, `gen_top_loops` holds the regenerated top-level loop nests
that the caller harvests. CONFIRMED: `gen_top_loops` `tp_getattro`/`tp_setattro` pair +
the `genexpr` closure symbol.

---

## 5. The expression walker (`isl.AstExpr` → Penguin affine expr / predicate)

### 5.1 `codegenExpr` — the expression dispatcher (`IslCodeGenBase` @128)

The whole affine/predicate decode pivots here. CONFIRMED full mapping (`get_type` →
int/id/op; `get_op_type` → the op table). The Penguin affine-expr objects (`CExpr`,
`Axis`, `ir.ir` exprs) **overload Python operators**, so `operator.add(a, b)` literally
builds a Penguin affine-sum node.

```c
codegenExpr(self, expr) {                          // @128
    t = expr.get_type();
    if (t == isl.ast_expr_type.int) return self.codegenInt(expr);
    if (t == isl.ast_expr_type.id)  return self.codegenId(expr);
    if (t == isl.ast_expr_type.op) {
        op = expr.get_op_type();
        switch (op) {
          case add:    return self.codegenBinaryOp(expr, operator.add);      // a + b
          case sub:    return self.codegenBinaryOp(expr, operator.sub);      // a - b
          case mul:    return self.codegenBinaryOp(expr, operator.mul);      // a * b
          case pdiv_q: return self.codegenBinaryOp(expr, operator.floordiv); // ⌊a / b⌋
          case pdiv_r: return self.codegenBinaryOp(expr, operator.mod);      // a mod b
          case minus:  return self.codegenUnaryOp(expr, operator.neg);       // -a
          case ge:     return self.codegenGE(expr);                          // predicate
          case le:     return self.codegenLE(expr);
          case lt:     return self.codegenLT(expr);
          case and:    return self.codegenAndOp(expr);                       // predicate list
          default:     raise_err("Not implemented operator (" + expr.to_C_str() + ")");
        }
    }
    raise_err("Not implemented expr (" + expr.to_C_str() + ")");
}
```

The op-type → functor map is CONFIRMED from the string pool (`add`, `sub`, `mul`,
`floordiv`, `mod`, `neg`, `pdiv_q`, `pdiv_r`, `minus`, `lt`, `le`, `ge`, `and`).

> **GOTCHA — `pdiv_q`/`pdiv_r` are *floored* division.** `isl`'s `pdiv_q`/`pdiv_r` are
> the **non-negative-remainder** (floored) quotient/remainder operators, mapped to
> Python `operator.floordiv` / `operator.mod`. Using C-style truncating `/`/`%` here
> would mis-translate negative-coefficient affine subscripts. This is the single most
> error-prone mapping in the table.

### 5.2 `codegenBinaryOp` (`@200`) and `codegenUnaryOp` (`@207`)

```c
codegenBinaryOp(self, expr, op) {                  // @200
    a = self.codegenExpr(expr.get_op_arg(0));
    b = self.codegenExpr(expr.get_op_arg(1));
    return op(a, b);                               // operator.add/sub/mul/floordiv/mod
}
codegenUnaryOp(self, expr, op) {                   // @207
    return op(self.codegenExpr(expr.get_op_arg(0)));   // operator.neg
}
```

### 5.3 `codegenInt` (`@215`) — the raw literal

```c
codegenInt(self, expr) { return dyn_cast_int(expr); }   // returns the Python int
```

> **NOTE — `codegenInt` returns the raw `int`, not a `CExpr`.** The *callers*
> (`codegenForInit`/`Cond`) wrap a constant bound in `CExpr`; inside affine arithmetic the
> raw int is what the overloaded operators expect. So a literal appearing inside `a + 3`
> stays a bare `3`, but a literal *bound* becomes `CExpr(3)`.

### 5.4 `codegenAndOp` (`@212`) — predicate list

```c
list codegenAndOp(self, expr) {                    // @212
    n = expr.get_op_n_arg();
    return [ self.codegenExpr(expr.get_op_arg(i)) for i in range(n) ];   // list of predicates
}
```

An `isl` `and` of comparisons lowers to the **list** of operand predicates; `codegenIf`
then folds them (via the `predicates` stack and the `infimum` envelope) into the
conjoined guard.

### 5.5 `codegenLT` / `codegenLE` / `codegenGE` (`@176`/`184`/`192`)

Identical shape: lower both operands, apply the Python rich-compare. The Penguin affine
objects' `__lt__`/`__le__`/`__ge__` build the predicate node.

```c
codegenLT(self, lt) { return codegenExpr(lt.get_op_arg(0)) <  codegenExpr(lt.get_op_arg(1)); }
codegenLE(self, lt) { return codegenExpr(lt.get_op_arg(0)) <= codegenExpr(lt.get_op_arg(1)); }
codegenGE(self, ge) { return codegenExpr(ge.get_op_arg(0)) >= codegenExpr(ge.get_op_arg(1)); }
```

> **NOTE — no `codegenGT`.** There is deliberately no `>`-handler: `isl` canonicalises
> `a > b` to `b < a` (swapped operands), and keeps `>=` as the `ge` op. So only
> `lt`/`le`/`ge` ever reach the walker.

### 5.6 `extractIdName` (`@226`) — pull an `id`'s textual name

```c
str extractIdName(self, expr) {                    // @226
    if (expr.get_type() != isl.ast_expr_type.id)
        raise_err("Incorrect expr");
    return expr.get_id().get_name();               // the isl.Id's name string
}
```

Used by `createAxis` (axis name) and `codegenId` (param key). For a statement-instance
id, this name is the `sN` that maps back to the original `Inst`.

### 5.7 `codegenId` (`@218`) — iterator → `Axis`, else → parameter

The dual of `codegenFor`'s `DictRAII` binding. CONFIRMED sequence: `ids` → `IdWrapper`
→ `params` → `extractIdName`.

```c
codegenId(self, expr) {                            // @218
    key = IdWrapper(expr);
    if (key in self.ids)
        return self.ids[key];                      // bound loop iterator -> its Axis
    name = self.extractIdName(expr);
    return self.params[name];                      // otherwise: a symbolic parameter
}
```

Each enclosing `for`-loop inserts `IdWrapper(it) → Axis` into `self.ids` (via `DictRAII`);
when an index expression references that iterator, `codegenId` returns the **same `Axis`
object**, re-tying the regenerated subscript to the loop. `self.params` handles outer
symbolic dimensions (tile sizes / parameters) that are not loop iterators.

---

## 6. Module-level helpers + statement/iterator identification

### 6.1 `dyn_cast_int(expr)` (`@39`) — the "is this an int literal?" probe

```c
dyn_cast_int(expr) {                               // @39  (module-level)
    if (expr.get_type() == isl.ast_expr_type.int) {
        v = expr.get_val();                        // isl.Val
        if (!v.is_int()) raise_err("Not an integer value!");
        return v.get_num_si();                     // signed-int Python value
    }
    return None;                                   // not an int AstExpr -> the dyn_cast "miss"
}
```

Returning `None` on a non-int is what powers the `if v is not None:` constant-folding
tests in `codegenForInit`/`Cond`/`Int`. CONFIRMED: `get_val`, `is_int`, `get_num_si`,
`"Not an integer value!"`.

### 6.2 `expr_operands(expr)` (`@31`) and `expr_icmp(expr)` (`@20`)

```c
expr_operands(expr) {                              // @31  (STRONG)
    assert(expr.get_type() == isl.ast_expr_type.op);
    return (expr.get_op_type(),
            [ expr.get_op_arg(i) for i in range(expr.get_op_n_arg()) ]);
}
expr_icmp(expr) {                                  // @20  (STRONG)
    assert(expr.get_type() == isl.ast_expr_type.op);
    op = expr.get_op_type();
    if (op == isl.ast_op_type.lt) return (expr.get_op_arg(0), expr.get_op_arg(1));
    if (op == isl.ast_op_type.le) return (expr.get_op_arg(0), expr.get_op_arg(1));
    // other ops -> caller's "Unexpected predicate"
}
```

`expr_operands` returns `(op_type, [args])`; used by `codegenForInit`/`Cond` to detect
the `max`/`min` folds. `expr_icmp` splits a `<`/`<=` comparison into its `(lhs, rhs)`
pair; `codegenForCond` uses it to separate the iterator side from the bound side. The
two branches key two distinct constant tuples (`__pyx_tuple__2`/`__pyx_tuple__3`).

### 6.3 `IdWrapper` — making `isl.Id` hashable by value

```c
class IdWrapper {
    __init__(self, expr) { self.expr = expr; }                  // an isl.AstExpr of kind id
    __eq__(self, other)  { return self.expr == other.expr; }    // isl RichCompare on the exprs
    __hash__(self)       { return hash(self.expr.get_id()); }   // hash by the underlying isl.Id
}
```

CONFIRMED bodies: `__eq__` does `PyObject_RichCompare(self.expr, other.expr)`; `__hash__`
does `hash(self.expr.get_id())` (`get_id` then `PyObject_Hash`).

> **Why the split hash/eq matters.** `__hash__` hashes the **underlying `isl.Id`** (not
> the `AstExpr` wrapper) while `__eq__` compares the wrapped exprs. So the *same*
> iterator id appearing in *different* `AstExpr` instances still hashes identically and
> compares equal — which is exactly what lets `self.ids[IdWrapper(it)]` in `codegenFor`
> and the `IdWrapper(expr)` lookup in `codegenId` resolve to the same `Axis`. Two roles:
> **(a)** loop iterators (`ids` keying), and **(b)** statement instances (the client's
> `codegenUser` keys the `sN ↔ Inst` table by `IdWrapper`/`extractIdName`).

---

## 7. End-to-end mapping (the inverse of §5.16)

| `isl.AstNode` kind | Penguin construct | method |
|---|---|---|
| `for_(it, init, inc, cond)` | `Axis(name, id, lb, ub, stride)`, spliced via `insert_before(curstmt)` | `codegenFor` → `createAxis` |
| `user( call sN, idx… )` | re-emitted `Inst sN` with remapped subscripts | `codegenUserOp` → `codegenUser` *(client)* |
| `block[ child… ]` | sibling sequence (`foreach codegenNode`) | `codegenBlock` |
| `if_(cond) then{…}` | guard predicate pushed on `self.predicates` | `codegenIf` |

| `isl.AstExpr` kind | Penguin affine / predicate | method |
|---|---|---|
| `int(v)` | Python `int` (→ `CExpr(v)` at bound sites) | `codegenInt` / `dyn_cast_int` |
| `id(name)` | `Axis` (bound iterator) \| `param` | `codegenId` / `IdWrapper` |
| `op add/sub/mul` | `operator.add/sub/mul` on affines | `codegenBinaryOp` |
| `op pdiv_q / pdiv_r` | `operator.floordiv` / `operator.mod` | `codegenBinaryOp` |
| `op minus` | `operator.neg` | `codegenUnaryOp` |
| `op lt/le/ge` | affine `<` / `<=` / `>=` predicate | `codegenLT` / `LE` / `GE` |
| `op and` | list of predicates (folded by `if`) | `codegenAndOp` |
| `op max` (in for-init) | `infimum([…])` — lower-bound envelope | `codegenForInit` |
| `op min` (in for-cond) | `supremum([…])` — upper-bound envelope | `codegenForCond` |
| `op call` (in user) | statement instance (id + indices) | `codegenUserOp` |

**Scoping invariants:** `codegenFor`/`codegenIf` push onto `self.predicates` via
`AttrRAII` (save/restore), so guard predicates are scoped to exactly the loop body /
then-branch. `codegenFor` binds `self.ids[IdWrapper(it)] = axis` via `DictRAII` for the
body's duration only. Both restore on `__exit__`, so siblings see a clean state.

---

## 8. Diagnostics (exact binary literals — all CONFIRMED)

| Literal | Raised by | Condition |
|---|---|---|
| `Not implemented node (` | `codegenNode` | node type ∉ {for, user, block, if} |
| `Not implemented expr (` | `codegenExpr` | expr type ∉ {int, id, op} |
| `Not implemented operator (` | `codegenExpr` | op type not in the dispatch table |
| `Unexpected user expr type!` | `codegenUserOp` | user expr is not an `op` |
| `unexprect user op type!` | `codegenUserOp` | op is not `call` *(typo preserved)* |
| `Unexpected predicate` | `codegenForCond` | guard op ∉ {lt, le} |
| `Expect compare against iterator!` | `codegenForCond` | neither side is `it` |
| `Unsupported expression!` | `codegenForInit` / `codegenForCond` | bound is neither affine, literal, nor max/min |
| `Not an integer value!` | `dyn_cast_int` | `isl.Val` is not integral |
| `Incorrect expr` | `extractIdName` | expr is not an `id` |

---

## 9. Adversarial self-verification

The five strongest claims, re-challenged against the cp310 `.so`:

1. **Docstring = "IslCodeGen -- Generate tensoriser IR from ISL AST".** ✔ CONFIRMED —
   `strings | rg tensoriser` returns `__pyx_k_IslCodeGen_Generate_tensoriser`, the exact
   British-spelling literal. (Not "tensorizer".)
2. **The 10 diagnostics incl. the `unexprect` typo.** ✔ CONFIRMED — all ten present
   verbatim, `__pyx_k_unexprect_user_op_type` carries the typo byte-for-byte.
3. **`pdiv_q → floordiv`, `pdiv_r → mod`, `minus → neg`.** ✔ CONFIRMED — `pdiv_q`,
   `pdiv_r`, `floordiv`, `minus`, `neg` all present; the `operator` module is imported.
4. **The AST builder (`node_from_schedule`/`AstBuild`) is NOT in this module.** ✔
   CONFIRMED — neither string appears; only read-side `for_get_*`/`block_get_children`/
   `user_get_expr`/`get_op_arg` accessors are present. This module consumes, not builds.
5. **`codegenUser` is abstract here (concrete re-emit is upstream).** ✔ CONFIRMED — 6
   `NotImplementedError` refs cover the 4 base hooks; `IslCodeGen` overrides
   `createAxis`/`codegenForInit`/`codegenForCond`/`codegenForBody` (all confirmed
   symbols) but `codegenUser` has no `IslCodeGen`-prefixed override.

**Items that remain INFERRED (not byte-exact), tagged in-text:** the exact `Axis(...)`
keyword names (`lb`/`ub`/`stride`/`id`/`name` — kwargs built dynamically via
`PyDict_SetItem`); the precise `addNewBlock` argument (a `PyNumber_Multiply`/`Add` of
axis id and depth); `codegenForCond`'s iterator-side selection expressed as
`extractIdName` equality; and `scev` being imported-but-unused inside the codegen.

> **CORRECTION (spelling, vs. the backing report's pseudocode).** The report's
> reconstructed pseudocode writes the isl node-type members as `isl.ast_node_type.for`
> and `.if`. These are Python keywords; the binary spells them **`for_`** and **`if_`**
> (CONFIRMED `__pyx_k_for_`). This page uses the trailing-underscore form throughout.

---

## See also

- [§5.16 ISL Dependence Graph](isl-dependence-graph.md) — the forward build (`Inst → isl
  domain/index map`) and the `sN ↔ Inst` table this codegen's `codegenUser` indexes back into.
- [§5.3 Axis / Loop Model](axis-loop-model.md) — the `Axis` IR node this codegen emits.
- [§5.14 Software Pipelining](software-pipelining.md) — a transform client that builds an
  isl schedule and drives this codegen to regenerate the pipelined loop nest.
