# TongaIslSimplifier — Access → Address Rewrite

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, module `neuronxcc/starfish/penguin/targets/transforms/TongaIslSimplifier.cpython-310-x86_64-linux-gnu.so` (cp310 wheel). The `.so` is UNSTRIPPED Cython — qualnames, `__pyx_n_s_` interned names, and `_Pyx_AddTraceback` file/line args are all intact, so each method's call graph and source-line range are directly readable. Other wheels relocate every address; treat each as version-pinned.*

## Abstract

`IslSimplifier` (page [5.19](./isl-simplifier.md)) owns the **generic** convex-hull predicate gist and `shrink_domain`. This module — class `NeuronIslSimplifier` — is the **Tonga-target sibling** that turns the result of that simplification into something the hardware can address. It consumes an ISL-simplified *access relation* (an `isl.Map` relating loop-iteration indices to the tensor element addresses an `AccessPattern` touches) and re-materialises two things: a list of concrete **per-axis address affine expressions** and the **guard predicates** that must hold for those addresses to be in bounds. That pair `(addrs, preds)` is the payoff of the whole polyhedral middle-end on this target — the affines flow down to register lowering (`RegAPAddr = base + (off/step)·part_stride`), and the predicates become the runtime guards on the rewritten load/store.

The integer-set algebra itself is **stock islpy** (~2023.1, a pip dependency): `Map.reverse`, `Map.apply_range`, `Map.intersect_range`, `Map.domain`/`.range`, `Set.empty`/`.union`/`.coalesce`, `project_out`. None of that was reverse-engineered. What this page documents is the **Penguin↔isl glue**: how an `AccessPattern` becomes an `isl.Map` (`access` / `access_impl` / `create_access`), how a simplified `isl.Map` is read back out into address affines (`get_scaled_addrs` / `newaddrs`), and how the index→address rebasing relation is composed (`buildMapping`'s `reverse().apply_range()`).

| | |
|---|---|
| **Class** | `NeuronIslSimplifier` (qualname prefix `…18TongaIslSimplifier_19NeuronIslSimplifier`) |
| **Generic base / sibling** | `IntegerSetAnalysis` · `IslSimplifier` (5.19, the generic gist) |
| **Primary client** | `transforms.MemcpyElimination` (module-top import) |
| **The rebasing relation** | `buildMapping` @ `0x19bb0` — `access(dst).reverse().apply_range(access(src))` |
| **The payoff** | `codegenAddrAndPredicates` @ `0x1f740` → `(addrs, preds)` |
| **The driver** | `rewriteAllLoadAddress` @ `0x1dcb0` → `rewriteLoadAdds` @ `0x17b80` |
| **`__FILE__`** | `neuronxcc/starfish/penguin/targets/transforms/TongaIslSimplifier.py` (verbatim in `.rodata` @ `0x30870`) |

> **NOTE — grounding.** Every method address, qualname, and source-line range below comes from a `_Pyx_AddTraceback` argument plus an interned `__pyx_n_s_` name, with the decompiled body present. Call-*order* and data-flow claims are read off the decompiled CPython-C-API sequence. Claims marked [INFERRED] rest on islpy 2023.1 semantics plus the Penguin AccessPattern model, and are called out individually. The IDA `missing_any_canonical` file for this module is empty: full addresses, full decompile, no synthetic stubs.

## Symbol / source-line map

Addresses are cp310 file-relative; they are the `__pyx_pw_` C-API wrappers (the `__pyx_pf_` method body is inlined into each wrapper). Source lines are from the `_Pyx_AddTraceback` args.

| addr | method (recovered signature) | src line | kind |
|---|---|---|---|
| `0x281a0` | `access(self, ap, inst)` | 53–56 | builds the access `Map` |
| `0x2e330` | `injective_access(self, ap, inst)` | 58–61 | access over the **shrunk** domain |
| `0x1b9a0` | `access_without_predicates(self, ap, inst)` | 77–85 | raw access, no guards |
| `0x19bb0` | `buildMapping(self, dst, src, inst)` | 63–67 | the index→address rebase |
| `0x1f740` | `codegenAddrAndPredicates(self, new_acc, ap, inst)` | ~141–151 | → `(addrs, preds)` |
| `0x1aea0` | `enumerate_predicates(self, space, predicates, loopnest)` | 69 | outward projection (generator) |
| `0x1d290` | `in_domain(self, ap)` | 22–29 | `@contextmanager`, domain memoise |
| `0x12cd0` | `in_predicate_domain(self, inst)` | 44–51 | `@contextmanager`, predicate domain |
| `0x1e6e0` | `in_shrink_domain(self, ap)` | 31–42 | `@contextmanager` + `prune_loopnest` |
| `0x16e50` | `in_shrink_domain.<locals>.prune_loopnest(inst)` | 33–35 | drops address-free loop axes |
| `0x1dcb0` | `rewriteAllLoadAddress(self, tensor, mapping)` | 110–112 | generator over `tensor.users` |
| `0x17b80` | `rewriteLoadAdds(self, dst_acc, dst_load_inst, mapping)` | 123–137 | per-load rewriter |
| `0x21450` | `try_predicate_range_over_access(self, ap, ap_inst, range_set, overapproximate, approximate_predicates)` | 94 | range → guard predicate |
| `0x23290` | `union_ranges(self, tensor, accesses)` | 153 | read-side reach set |
| `0x24a10` | `union_affine_access_ranges(self, t, stores)` | 163–183 | write-side (affine-store) reach set |
| `0x27790` | `loopnest(self, inst)` | 19–20 | loopnest accessor |

All 23 `NeuronIslSimplifier.<method>` qualnames (including the `.<locals>.lambda` / `.genexpr` / `.prune_loopnest` nested closures) are interned verbatim in the string pool. The Cython scope structs `__pyx_scope_struct__in_domain`, `_1_in_shrink_domain`, `_3_in_predicate_domain`, `_4_enumerate_predicates`, `_5_rewriteAllLoadAddress`, `_8_union_affine_access_ranges` confirm which methods carry per-call closures.

## 1 — The access relation (`access` / `access_without_predicates` / `injective_access`)

These three build the `isl.Map` `{ S[ivs] → tensor[addr-affs] }` that relates loop indices to the tensor addresses an `AccessPattern` reaches. They are the Tonga-target specialisation of the generic `IntegerSetAnalysis.access` machinery; the heavy lifting (`access_impl` / `build_new_access_impl` / `create_access`) is inherited, and this class adds the domain-caching and the shrunk-domain variant.

### `access` — the predicated form

```c
// access(self, ap, inst)  @0x281a0  py 53-56
// interned chain @0x281a0: n_s_use_inst -> n_s_in_domain -> n_s_access_impl
//                          -> __pyx_tuple__3 (the ctx-mgr __exit__ arg)
PyObject *access(self, ap, inst) {
    with self.use_inst(inst):          // py 53  — bind "current instruction"
        self.in_domain(ap);            // py 55  — evaluated for SIDE EFFECT: materialise/
                                       //          cache the iteration domain
        return self.access_impl(ap);   // py 56  — inherited Map builder
}
```

`use_inst` is an `AttrRAII`-backed context manager (string `AttrRAII` present @ pool) that temporarily binds the current instruction so domain/predicate construction can see it. The `in_domain(ap)` call on line 55 is *not* used for its return value — it forces the lazy domain to be built and cached in `self.domain_cache` / `self.domain_space_cache` so the subsequent `access_impl` shares one `isl.Set`. The `with` lowering is the classic Cython exc-info-save / `_Pyx__ExceptionReset` / `__exit__(tuple__3)` block.

### `access_without_predicates` — the raw form

This is the explicit constructor that **builds the access map directly** rather than delegating to `access_impl`, and **drops the predicate guards** — the raw affine relation over the full loopnest box.

```c
// access_without_predicates(self, ap, inst)  @0x1b9a0  py 77-85
PyObject *access_without_predicates(self, ap, inst) {
    with self.use_inst(inst):
        tensor = ap.tensor;                       // py 78  n_s_tensor
        domain = self.in_domain(ap);              // py 79  n_s_in_domain
        return self.create_access(                // py 80  n_s_create_access
                 addrs          = get_scaled_addrs(ap),         // py 81  MODULE GLOBAL
                 domain         = domain,
                 out_tuple_name = self.tensor_tuple_name(tensor),// py 83
                 in_tuple_name  = self.make_in_tuple_name(...)); // py 84
}
```

`get_scaled_addrs` is resolved via `_Pyx__GetModuleGlobalName` (decompile `…access_without_predicates_0x1b9a0.c:973`) — it is a **module-level function, not a method**. It turns the AP's raw element offsets into per-axis *scaled* affine address expressions (`stride·iv` terms); this is the same scaling the register-lowering `part_stride` math later consumes. `create_access` is the inherited `isl.Map` factory; `tensor_tuple_name` / `make_in_tuple_name` produce the isl tuple identifiers for the range (tensor) and domain (statement) spaces.

> **GOTCHA — the kwarg key *pairings* are reconstructed.** The three kwarg names `addrs` / `out_tuple_name` / `in_tuple_name` all exist verbatim in the string pool, so the keys are real; but Cython passes them through a cached kwarg-name *tuple const* rather than per-site `__pyx_n_s_` refs, so which name lands at which `PyDict_SetItem` index is [INFERRED]. The pool also carries `origin` / `store_acc` / `write_acc`, which are other `create_access` keys used elsewhere; do not assume they are passed here.

### `injective_access` — over the shrunk domain

The injective sibling of `access`. The decompile (`…injective_access_0x2e330.c`) shows the one critical difference: where `access` calls `in_domain`, `injective_access` calls **`in_shrink_domain`** (`n_s_in_shrink_domain` @ getattr `:444`), then `access_impl` (`:594`).

```c
// injective_access(self, ap, inst)  @0x2e330  py 58-61
PyObject *injective_access(self, ap, inst) {
    with self.use_inst(inst):
        with self.in_shrink_domain(ap):     // py 61  — SHRUNK (pruned) domain
            return self.access_impl(ap);
}
```

**Why "injective".** `in_shrink_domain` (§4.3) runs `prune_loopnest`, which drops loop axes that no address affine references. Over that pruned domain the access `Map` has no degenerate (address-free) domain dimensions, so distinct surviving domain points map to distinct tensor addresses — the access is injective *by construction*. This is exactly the precondition `buildMapping` (§2) needs before it `.reverse()`s an access: a `Map.reverse` is only single-valued when the forward map is injective.

## 2 — The index→address mapping (`buildMapping`)

`buildMapping` builds the relation that **rebases one access pattern's indices onto another's** — the core of address rewriting, called by `MemcpyElimination` to fold a `dst` load's address through a `src` store's access.

```c
// buildMapping(self, dst, src, inst)  @0x19bb0  py 63-67
// arg binding from _Pyx_GetKwValue order {self,dst,src,inst}
PyObject *buildMapping(self, dst, src, inst) {
    src_access = self.access(src, inst);                          // py 64  (:154 n_s_access)
    dst_access = self.access(dst, inst);                          // py 65  (:202 n_s_access)
    mapping    = dst_access.reverse().apply_range(src_access);    // py 66  (:266 reverse,
                                                                  //         :283 apply_range)
    return self.try_simplify(mapping);                            // py 67  (:313 try_simplify)
}
```

This whole chain is read **verbatim** off the decompile (`…buildMapping_0x19bb0.c`): two `n_s_access` getattrs, then `n_s_reverse`, then `n_s_apply_range` (arg = the other access), then `n_s_try_simplify`. The `_Pyx_AddTraceback` confirms qualname `…NeuronIslSimplifier.buildMapping` at py 63.

**ISL semantics (INFERRED from islpy).** `dst_access` is `{ Sdst[i] → tensor[a] }`. `reverse()` gives `{ tensor[a] → Sdst[i] }`. `X.apply_range(Y) = { x→z : ∃y. x→y∈X ∧ y→z∈Y }` requires `X`'s range to share a tuple name with `Y`'s domain. The composition yields the index↔index (or index↔address) rebasing relation between the two accesses, compacted by `try_simplify` (the inherited `coalesce` + light `gist` wrapper from 5.19).

> **NOTE — the composition order is the whole trick.** `reverse()` first turns the forward access into "address → dst-index", then `apply_range(src_access)` chains *through* the source access so the result relates the two index spaces (or rebases dst indices straight onto src addresses). `injective_access` exists precisely so that the `reverse()` here is well-defined. The call order is read off the decompile; the polyhedral *meaning* of the composition is [INFERRED].

## 3 — Access → concrete address + predicates (`codegenAddrAndPredicates`)

The payoff method. Given an already-ISL-simplified access `new_acc` plus the original `AccessPattern` `ap`, it emits (a) the list of concrete per-axis address affine expressions and (b) the guard predicates that must hold.

```c
// codegenAddrAndPredicates(self, new_acc, ap, inst)  @0x1f740  py 141-151
PyObject *codegenAddrAndPredicates(self, new_acc, ap, inst) {
    with self.use_inst(inst):                              // py 141  (:311 use_inst)
        domain = self.in_domain(ap).domain;               // py 144  (:327 in_domain,
                                                          //          :756 reads .domain attr)
        preds  = self.predicates_over_loopnest(... domain ...); // py 145  (:490)
        if (preds == Py_None)                             // py 148  (:756/.. cmp)
            raise NotImplementedError;                     // py 148  (:882 _Pyx_Raise)
        addrs  = list(newaddrs(... new_acc / ap / domain ...)); // py 150  (:895/899
                                                          //          MODULE GLOBAL, 3 kwargs;
                                                          //          PySequence_List)
        return (addrs, preds);                            // py 151  (:956 PyTuple_New)
}
```

* `predicates_over_loopnest` is the same convex-hull-gisted `AffinePredicate` producer as 5.19; its `None` return is the **bail-out** when the loopnest cannot be convex-hulled into a single predicate set, and that bail surfaces as a raised `NotImplementedError` at py 148 (`_pyx_builtin_NotImplementedError`, decompile `:882`).
* `newaddrs` is a **module-level function** (resolved via `_Pyx_GetBuiltinName` / `_Pyx__GetModuleGlobalName` at `:895`/`:899`, exactly like `get_scaled_addrs`). It reads the per-axis affine address expressions back **out** of the simplified relation `new_acc`, over the now-tightened `domain`. Its three `PyDict_SetItem` kwargs (the simplified access, the `ap`, the `domain`) are visibly three dict entries; the key *spellings* are not separately interned, so those are [INFERRED].
* `addrs` is forced to a concrete `list` via `PySequence_List` (the result is even `PyList_Type`-checked first). The return is the 2-tuple `(addrs, preds)`.

The address affines this returns are exactly what register lowering delinearises into partition/free byte offsets (`RegAPAddr = base + (off/step)·part_stride`; SBUF stride `0x40000`, PSUM `0x8000`).

## 4 — Domain + predicate construction

Three of these (`in_domain` / `in_predicate_domain` / `in_shrink_domain`) are `@contextmanager` **generators** — `contextlib` / `contextmanager` / `AttrRAII` are all in the string pool. They temporarily bind a per-AP domain into `self.domain_cache` / `self.domain_space_cache` for the duration of a `with` block, then pop both on exit.

### 4.1 `in_domain` — domain memoisation

```c
// in_domain(self, ap)  @0x1d290  (gen body @0x2af90)  py 22-29   @contextmanager
@contextmanager
def in_domain(self, ap):                                  # py 22
    with AttrRAII(self, lambda: self.access_domain, ...):  # py 22,25
        yield                                              # the cached domain is live here
    self.domain_cache.pop(...)                             # py 26  (n_s_domain_cache, n_s_pop)
    self.domain_space_cache.pop(...)                       # py 29  (n_s_domain_space_cache, n_s_pop)
```

The `in_domain.<locals>.<lambda>(self)` body (`@0x1f100`, py 25) returns `self.access_domain` — the lazily-computed iteration domain for the current `ap`. `AttrRAII` memoises that into `domain_cache` / `domain_space_cache`, so repeated `access()` / `codegenAddrAndPredicates()` calls **under the same `with`** share one `isl.Set` instead of rebuilding it. That is why `access` (§1) calls `in_domain(ap)` purely for its side effect.

### 4.2 `in_predicate_domain`

```c
// in_predicate_domain(self, inst)  @0x12cd0  (gen body @0x2c960)  py 44-51
```

Same shape as `in_domain` (`AttrRAII` bind → `yield` → `domain_cache.pop` py 48 → `domain_space_cache.pop` py 51), but keyed on `inst`: the bound domain is the **predicate-restricted** iteration domain for `inst` — the set on which `inst`'s predicates must hold.

### 4.3 `in_shrink_domain` + `prune_loopnest`

```c
// in_shrink_domain(self, ap)  @0x1e6e0  (gen body @0x29610)  py 31-42
// nested prune_loopnest(inst)  @0x16e50  py 33-35
def prune_loopnest(inst):                                  # py 33
    used = frozenset().union(                              # py 34  (PyFrozenSet, n_s_union)
              *(idx for addr in self.full_addrs            #         (n_s_full_addrs,
                    for idx in addr.indices))              #          genexpr5 over n_s_indices)
    for ad in self.access_domain: ...                      # py 35  (n_s_access_domain)
```

`prune_loopnest` collects, for every address in `self.full_addrs`, the set of loop IVs that address depends on (the `genexpr5` body `…prune_loopnest_2generator5` yields each entry's `indices`). The union is the set of axes **any** address actually references. Axes *not* in `used` carry no address dependence and are projected away — shrinking the domain the generic `shrink_domain` gist (5.19) then has to convex-hull. Net effect: `prune_loopnest = "drop loop axes that no address affine references"`, the Tonga-side feeder for the generic shrink. This is also what makes `injective_access` (§1) injective.

### 4.4 `enumerate_predicates` — outward projection

```c
// enumerate_predicates(self, space, predicates, loopnest)  @0x1aea0
//   gen body __pyx_gb_..._19generator3 @0x13ae0   py 69   (generator)
// pyargnames {self, space, predicates, loopnest}
def enumerate_predicates(self, space, predicates, loopnest):   # py 69
    for loop in loopnest:
        for p in predicates:
            yield p.project(space, loop)   # n_s_project = isl project_out_dims
```

A generator. It walks the `loopnest` from the innermost axis outward, calling `.project(...)` (`n_s_project` in `generator3`) to drop the inner axes and yield each predicate expressed over progressively-**outer** index spaces. The purpose is to let the gist hoist a predicate to the **outermost loop level at which it is still exact**. This is the Tonga analogue of the generic `enumerate_affine_predicates` (5.19); `space` is the `isl.LocalSpace` / `Map` space (`isl.LocalSpace` is imported at module top).

> **NOTE —** the exact `project` argument order (`p.project(space, loop)` projecting axes `< loop`) is [INFERRED] from the islpy `project_out_dims` signature plus the loop/predicate iteration order in `generator3`. The `n_s_project` call and the `{space, predicates, loopnest}` arg names are read directly.

## 5 — The load-address rewriting pass

The top-level driver: given a `tensor` and an index→address `mapping` (from `buildMapping`), re-emit every load of that tensor through the simplified mapping, producing concrete addresses + predicates. Paired with `MemcpyElimination` (module-top import).

### 5.1 `rewriteAllLoadAddress`

```c
// rewriteAllLoadAddress(self, tensor, mapping)  @0x1dcb0
//   gen body __pyx_gb_..._26generator4 @0x14b80   py 110-112   (generator)
def rewriteAllLoadAddress(self, tensor, mapping):              # py 110
    for dst in tensor.users:                                   # py 112  (n_s_users + GetIter)
        dst_acc = NeuronIndicesAP(dst, ...).reinterpret(...)   # py 112  (genexpr6 @0x14700:
                                                              #   n_s_NeuronIndicesAP, n_s_reinterpret)
        with self.use_inst(dst):                               # (n_s_use_inst, n_s_dst)
            yield self.rewriteLoadAdds(dst_acc, dst, mapping)
```

Walk every consumer (load) of `tensor`, wrap each as a `NeuronIndicesAP` reinterpreted onto the tensor's element layout, and push it through `rewriteLoadAdds` under the mapping. (`NeuronIndicesAP`, `reinterpret`, `users` all verbatim in the pool.)

### 5.2 `rewriteLoadAdds` — the per-load rewriter

```c
// rewriteLoadAdds(self, dst_acc, dst_load_inst, mapping)  @0x17b80
//   gen body ..._15rewriteLoadAdds_2generator7 @0x2fa90   py 123-137
def rewriteLoadAdds(self, dst_acc, dst_load_inst, mapping):    # py 123
    self.updateAPIndicies(...)                                # py 124  (:220 updateAPIndicies)
    new_acc = self.access(dst_acc, inst=dst_load_inst)        # py 125  (:258 access)
    new_acc = new_acc.apply_range(mapping)                    # py 126  (:316 apply_range) <-- REBASE
    new_acc = self.try_simplify(new_acc)                      # py 127  (:352 try_simplify)
    if new_acc is None:                                       # py 129
        raise NotImplementedError                             # py 129  (:401 _Pyx_Raise)
    addrs, preds = self.codegenAddrAndPredicates(             # py 131  (:407)
                       new_acc, dst_acc, dst_load_inst)
    if any(<addr not linear> for addr in addrs):              # py 134-135 (genexpr7 guard)
        raise NotImplementedError                             # py 135  (:609 _Pyx_Raise)
    return [(drop_ap_indicies(addr),                          # py 137  (:694/698 GLOBAL)
             keep_ap_indicies_linear_expr(addr))              #         (:723/729 GLOBAL)
            for addr in addrs]
```

**Line 126 is the heart of the rewrite.** `new_acc.apply_range(mapping)` **composes** the load's own access relation with the index→address `mapping` from `buildMapping` — so the load now reads through the rebased addresses. `codegenAddrAndPredicates` then reads those rebased addresses back out as concrete affines.

The return splits each address affine into a pair via two **module-global** helpers (both `_Pyx__GetModuleGlobalName`-resolved, decompile `:694`/`:723`):

* `drop_ap_indicies(addr)` — the address with the AP-index placeholders dropped;
* `keep_ap_indicies_linear_expr(addr)` — the index-linear expression that is written onto the rewritten load (the backend spells it `keepApIndiciesLinearExpr`).

Two distinct `NotImplementedError` bail-outs: py 129 if the simplified access collapses to `None`, and py 135 if **any** rebased address fails the linearity check (`generator7` guard).

### 5.3 `try_predicate_range_over_access`

Restricts an access to a **value range** and derives the predicates that hold on the restricted domain — "under what guard does this access stay inside `range_set`?".

```c
// try_predicate_range_over_access(self, ap, ap_inst, range_set,
//        overapproximate, approximate_predicates)  @0x21450  py 94
def try_predicate_range_over_access(self, ap, ap_inst, range_set,
                                    overapproximate, approximate_predicates):
    acc = self.access_without_predicates(ap, ap_inst)   # raw access (no guards)
    acc = self.try_simplify(acc)
    acc = acc.intersect_range(range_set)                # constrain RANGE to allowed values
    with self.in_domain(ap):
        return self.predicates_over_loopnest(acc.domain, acc.range,
                   overapproximate, approximate_predicates)   # the guard predicate
```

`intersect_range(range_set)` constrains the access map's **range** (tensor addresses) to the allowed value range; the surviving domain's convex-hull predicate (`predicates_over_loopnest`, 5.19) is the guard. `overapproximate` / `approximate_predicates` pass straight through to the gist (same knobs as 5.19). The `access_without_predicates → try_simplify → intersect_range → in_domain → predicates_over_loopnest` order is read from the interned chain; the exact return shape is [INFERRED].

### 5.4 `union_ranges` — read-side reach

```c
// union_ranges(self, tensor, accesses)  @0x23290  py 153
def union_ranges(self, tensor, accesses):
    rng = isl.Set.empty(self.tensor_space(tensor))   # n_s_isl/Set/empty/tensor_space
    with self.use_inst(...):
        for a in accesses:
            rng = rng.union(self.access(a, ...).range())
    return self.try_simplify(rng.coalesce())
```

`isl.Set` / `isl.Set.empty` are stock islpy; `tensor_space` builds the isl space for the tensor's address dimensions. The union of all access **ranges** is the full set of tensor addresses any access in `accesses` reaches. `coalesce` + `try_simplify` keep it compact.

### 5.5 `union_affine_access_ranges` — write-side reach

The store-side analogue of `union_ranges`, specialised for **affine** (linearly addressable) stores. The genexprs `…_2generator8` / `…_5generator9` plus `lambda7` flatten `stores` into their `NeuronIndirectSave` destination access-patterns, keeping only the affine ones; each is `reinterpret`ed onto the tensor's element layout before unioning so all ranges live in one isl space.

```c
// union_affine_access_ranges(self, t, stores)  @0x24a10
//   genexpr8/9 @0x16440 / @0x15aa0 + lambda7 @0x117b0   py 163-183
def union_affine_access_ranges(self, t, stores):                 # py 163
    affine_stores = (d for s in stores if isinstance(s, NeuronIndirectSave)
                                       for d in s.dsts)           # py 163-165 genexpr  [INFERRED reshape]
    rng = isl.Set.empty(self.tensor_space(t))                    # py 183
    for s in affine_stores:
        acc = self.access(s, ...).reinterpret(... t.tensor_shape ...)
        rng = rng.union(acc.range())
    return self.try_simplify(rng.coalesce())
```

This is the write-side counterpart that pairs with `union_ranges`' read-side reach for `MemcpyElimination`'s legality check: if the affine-store write set and the load read set are compatible, the copy can be folded.

All the names in that reshape are interned. `NeuronIndirectSave`, `tensor_shape`, `access_shape`, `reinterpret`, and `tensor_space` appear verbatim in the cp310 string pool, and the module's `__Pyx_CreateStringTabAndInitStrings` table carries `__pyx_k_dsts` ("dsts", @ `0x61fb`) and `__pyx_k_rank` ("rank", @ `0x7498`). What remains [INFERRED] is only the genexpr's *shape* — how those names compose into the per-store reshape — not the names themselves.

> **GOTCHA —** grepping only the `dst_*` family (`dst_acc` / `dst_load` / `dst_load_inst` / `dst_load_acc`) misses `dsts`, which lives in the string tab rather than as a `__pyx_n_s_` reference. Read `__Pyx_CreateStringTabAndInitStrings` directly before concluding a name is absent.

## 6 — End-to-end flow

How the pieces compose when `MemcpyElimination` (or a Tonga simplification pass) wants to fold a copy/load:

1. **Build the rebase** (§2):
   `mapping = buildMapping(dst, src, inst) = access(dst,inst).reverse().apply_range(access(src,inst)) |> try_simplify`
   — the relation that rebases dst-indices onto src-addresses. `injective_access` guarantees the `reverse()` is well-defined.
2. **Rewrite each load** (§5.1 → §5.2):
   `rewriteAllLoadAddress(tensor, mapping)` walks `tensor.users`; per load,
   `new_acc = access(dst_acc).apply_range(mapping) |> try_simplify`, then
   `addrs, preds = codegenAddrAndPredicates(new_acc, dst_acc, dst)` (§3) —
   `in_domain` (§4.1) caches the domain, `predicates_over_loopnest` (5.19) gists the guard,
   `newaddrs` reads the affines back out. Each addr is written back as `(drop_ap_indicies, keep_ap_indicies_linear_expr)`.
3. **Range guards when needed** (§5.3–§5.5):
   `union_ranges` / `union_affine_access_ranges` build the read/write reach sets;
   `try_predicate_range_over_access` turns a value-range constraint into the guard predicate.
4. **Down to hardware**:
   the emitted address affines are delinearised into partition/free byte offsets
   (`RegAPAddr = base + (off/step)·part_stride`).

**ISL verbs used (all stock islpy ~2023.1):** `Map.reverse`, `Map.apply_range`, `Map.intersect_range`, `Map.domain`, `Map.range`, `Set.empty`, `Set.union`, `Set.coalesce`, `Set/Map.project(_out)`. **Penguin glue reversed here:** `access` / `access_impl` / `create_access` (build the `Map`), `get_scaled_addrs` / `newaddrs` (scale / read-back the address affines), `tensor_space` / `tensor_tuple_name` / `make_in_tuple_name` (isl spaces & tuple ids), `AttrRAII` + `domain_cache` / `domain_space_cache` (domain memoisation), `drop_ap_indicies` / `keep_ap_indicies_linear_expr` / `updateAPIndicies` (AP-index ↔ affine marshalling), `try_simplify` (coalesce + gist), `predicates_over_loopnest` (the 5.19 gist).

## Evidence summary

The five structural claims and what pins each:

1. **`buildMapping` = `access(dst).reverse().apply_range(access(src))`.** Decompile `…buildMapping_0x19bb0.c` shows `n_s_access` ×2 (`:154`,`:202`), then `n_s_reverse` (`:266`), `n_s_apply_range` (`:283`), `n_s_try_simplify` (`:313`), in that order, with `_Pyx_AddTraceback` py 63.
2. **`codegenAddrAndPredicates` returns `(addrs, preds)` and bails to `NotImplementedError` when `predicates_over_loopnest` is `None`.** Decompile shows `in_domain` (`:327`) → `.domain` attr (`:756`) → `predicates_over_loopnest` (`:490`) → `_Pyx_Raise NotImplementedError` (`:882`) → `newaddrs` module-global (`:895`/`899`) → `PyTuple_New` (`:956`).
3. **`rewriteLoadAdds` rebases via `apply_range(mapping)` then splits via `drop_ap_indicies` / `keep_ap_indicies_linear_expr`.** Decompile shows `updateAPIndicies`(`:220`) → `access`(`:258`) → `apply_range`(`:316`) → `try_simplify`(`:352`) → `codegenAddrAndPredicates`(`:407`) → `drop_ap_indicies`(`:698`) + `keep_ap_indicies_linear_expr`(`:723`).
4. **`injective_access` uses the shrunk domain; `access` does not.** `…injective_access_0x2e330.c` getattrs `n_s_in_shrink_domain` (`:444`) then `n_s_access_impl` (`:594`); `…access_0x281a0.c` uses `n_s_in_domain` instead.
5. **`get_scaled_addrs` / `newaddrs` / `drop_ap_indicies` / `keep_ap_indicies_linear_expr` are module-level globals.** All four resolve via `_Pyx__GetModuleGlobalName` rather than `tp_getattro` on `self`, and each appears exactly once in the string pool.

Two things remain [INFERRED]: the kwarg key→index pairings into `create_access` / `newaddrs` (the keys are present, the pairing is not separately interned), and the `enumerate_predicates` projection argument order, taken from islpy semantics.

## See also

* [5.19 — IslSimplifier](./isl-simplifier.md) — the generic convex-hull gist / `shrink_domain` / `predicates_over_loopnest` this module specialises.
* [5.4 — Affine Expression Algebra](./affine-expr-algebra.md) — the affine model the address expressions live in.

