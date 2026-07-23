# Input/Output Alias Family

> *All addresses on this page apply to `neuronx-cc` 2.24.5133.0+58f8de22 (cp310), binary `neuronxcc/starfish/bin/hlo-opt`. Offsets are virtual addresses as reported by IDA; raw file offset = VA − 0x200000 (.rodata) / VA − 0x201000 (.text). Other versions will differ.*

## Abstract

Seven `hlo-opt` passes manage one table: `xla::HloInputOutputAliasConfig`, the entry-module record of which **output** subshape shares its physical buffer with which **input** parameter. The point of an alias is in-place buffer donation — if output `i` is allowed to overwrite the storage of input parameter `p`, the BIR allocator folds the two tensors onto a single device buffer, eliminating one allocation and one copy. For a training step that hands back a mutated KV-cache, a gradient accumulator, or an optimizer-state slab as an output, this is the difference between holding the buffer once and holding it twice.

The model is a two-kind enum, `AliasKind { kMayAlias = 0, kMustAlias = 1 }` (upstream XLA, reused verbatim). A **must-alias** is a hard contract: the runtime *must* place the output in the donated input's buffer, no copy ever. A **may-alias** is a permission: the runtime *may* reuse the buffer if liveness allows, else it inserts a copy. These seven passes are not from-scratch code — they are thin Neuron drivers over upstream XLA's `HloInputOutputAliasConfig` / `HloBufferDonorConfig` / `OptimizeInputOutputBufferAlias` machinery. The Neuron-authored contribution is the *pipeline staging*: discrete Add → Flip → Promote → Donation-conversion steps, each a one-line wrapper that picks an `AliasKind` and a scan policy. The kind these passes choose is exactly what `xla::ConvertInputOutputAlias` later lowers to the MHLO `mhlo.ArgResultAliasAttr{argIndex, resultIndex, mustAlias}` that the tensorizer reads to decide buffer reuse.

The family splits into two registration blocks. The 44–48 block (`remove-aliases`, `add-must-aliases`, `add-may-aliases`, `flip-must-aliases`, `flip-may-aliases`) runs *before* layout is finalized and matches input↔output pairs by shape string. The 72–73 block (`aws_neuron_alias_to_must_alias`, `aws_neuron_buffer_donation_to_alias`) runs *after* `aws_neuron_ensure_descending_layout_in_root` and `RET_CHECK`s that layout is resolved. This page reconstructs all seven, the must/may state machine, and the donation→alias conversion.

For reimplementation, the contract is:

- The data model: `HloInputOutputAliasConfig` = `ShapeTree<std::optional<Alias>>` + `IndexTable` + result `Shape`; `Alias = {int64 parameter_number @+0, ShapeIndex parameter_index @+8, AliasKind kind}`.
- The state machine: which kind each of the seven passes stamps, and the counter-intuitive Flip naming.
- The central rewrite primitive `SetUpAlias` and *why* its `!alias_.element(output_index)` RET_CHECK forces the Flip/Promote passes to rebuild a fresh config instead of mutating in place.
- The donation matcher (`OptimizeInputOutputBufferAlias::Build`) and why a donation produces a **may**-alias, not a must.

| | |
|---|---|
| **Passes** | 7 (`HloModulePass`, Kind=HloPass, no flag-struct) |
| **Registry slots** | 44–48 (pre-layout block) and 72–73 (post-layout block) |
| **Core table** | `xla::HloInputOutputAliasConfig` (upstream XLA) |
| **Companion table** | `xla::HloBufferDonorConfig` (upstream XLA) |
| **Central primitive** | `HloInputOutputAliasConfig::SetUpAlias` @ `0x9644da0` (2373 B) |
| **MHLO bridge** | `xla::ConvertInputOutputAlias` @ `0x75a93a0` → `mhlo::ArgResultAliasAttr` |
| **Neuron-authored TUs** | `hilo/hlo_passes/{AddAlias,FlipAlias,setup_alias}.cc` |
| **IR level** | HLO (entry computation), pre-tensorizer |

---

## The Alias Model

### Purpose

`HloInputOutputAliasConfig` is the HLO-level source of truth for input/output buffer sharing. It records, per output `ShapeIndex`, which input parameter (param_number + param `ShapeIndex`) donates its buffer, and whether the sharing is a **must**-alias (hard, no copy ever) or a **may**-alias (optional, copy-insertable). "No alias for output `i`" is encoded as the `std::optional` node being `nullopt`. This is the table every pass on this page reads or rewrites; the companion `HloBufferDonorConfig` records inputs the frontend marked *donatable* but not yet paired to a specific output, and pass #73 is what resolves a donor into a concrete alias.

### Data Layout

The `AliasKind` enum is confirmed both by the immediate constants fed to `SetUpAlias` (below) and by `HloInputOutputAliasConfig::ToString`, which prints `may-alias` (@`0x20d400`) for kind 0 and `must-alias` (@`0x231595`) for kind 1.

```c
// upstream XLA, reused verbatim — confirmed by ToString print + SetUpAlias imms
enum AliasKind { kMayAlias = 0, kMustAlias = 1 };

struct Alias {                  // value per node in ShapeTree<optional<Alias>>
    int64_t    parameter_number;  // +0x00  entry parameter index
    ShapeIndex parameter_index;   // +0x08  subshape path; InlinedVector<long,2>
    AliasKind  kind;              // must_alias bool: 0=kMayAlias / 1=kMustAlias
};
```

The `+0x00` / `+0x08` field offsets are recovered from the `AliasToMustAlias` re-register lambda @`0x2009f10`, which reads `mov rcx,[rcx]` (param_number @+0) and `lea r8,[rcx+8]` (param_index @+8) before calling `SetUpAlias`. (CERTAIN.)

The config itself is `ShapeTree<std::optional<Alias>>` (one optional node per output subshape) plus an `xla::internal::IndexTable` (flat index of the result shape) plus the result `Shape`. The demangled `RemoveAliases` callees name this storage type verbatim — `ShapeTree<std::optional<xla::HloInputOutputAliasConfig::Alias>>::CreateNodes` and `InlinedVector<pair<ShapeIndex, optional<...Alias>>, 1>::Assign` (CERTAIN, @`0x1f5e0a3` / `0x1f5e18b`).

### Central Primitive — `SetUpAlias`

Every alias mutation routes through `HloInputOutputAliasConfig::SetUpAlias` @`0x9644da0` (2373 B):

```c
Status SetUpAlias(const ShapeIndex& output_index,    // 0x9644da0
                  int64_t param_number,
                  const ShapeIndex& param_index,
                  AliasKind kind):
    RET_CHECK ShapeUtil::IndexIsValid(alias_.shape(), output_index)  // @0x96450b2
    RET_CHECK param_number >= 0
    RET_CHECK !alias_.element(output_index)                          // @0x96452a2 — output not already aliased
    // on failure: "Trying to set up output alias for param %lld at %s but failed:
    //              output index %s is already aliased with param %lld at %s"  (@0x332ef0)
    alias_.element(output_index) = Alias{param_number, param_index, kind}
```

> **GOTCHA —** the `!alias_.element(output_index)` RET_CHECK is the reason the Flip and Promote passes cannot mutate kinds in place. You cannot re-`SetUpAlias` an output that already carries an alias; it `RET_CHECK`-fails. So `FlipAliases` and `AliasToMustAlias` first **collect** all existing aliases, then build a *fresh* empty config and re-`SetUpAlias` every entry with the new kind. A reimplementation that tries to flip a bool in the live ShapeTree will trip this check.

### The MHLO Bridge

The `must_alias` bool surfaces downstream as the MHLO `mhlo.ArgResultAliasAttr` `mustAlias` field. Two distinct string pools confirm two distinct consumers:

- `may_alias` (@`0x21e9eb`) / `must_alias` (@`0x288162`) are referenced by `xla::ConvertInputOutputAlias(const HloInputOutputAliasConfig&, mlir::Builder*)` @`0x75a93a0` — the HLO→MHLO lowering that emits one `ArgResultAliasAttr` per alias onto the entry function (CERTAIN — `referenced_by_functions` on both strings names the `ConvertInputOutputAlias` per-alias `InvokeObject` lambda).
- `must_alias` (@`0xbd9380`) is referenced by `mlir::mhlo::ArgResultAliasAttr::parse` and `::print` — the attribute's textual round-trip (CERTAIN).

So the kind chosen by these seven passes is exactly what the tensorizer reads from the MHLO arg/result alias attribute. Downstream:

- **must-alias** → buffer shared unconditionally; no copy is ever emitted. The input's storage *is* the output's storage — a hard in-place requirement the backend must satisfy.
- **may-alias** → buffer shared if liveness permits, else a copy is inserted. This is the knob that lets the allocator reclaim a large donated input (KV-cache, weight-grad accumulator) as an output, saving one device allocation and one copy.

---

## The Alias State Machine

Each pass operates on the entry computation's `HloInputOutputAliasConfig` (and, for #73, its `HloBufferDonorConfig`). All seven `RET_CHECK nullptr != entry_computation_` (string @`0x2108c9`). Legend: kind ∈ {may=0, must=1}; "—" = no alias (`nullopt`).

| # | Pass (`name()`) | Trigger / scope | From | To | Conf |
|---|---|---|---|---|---|
| 44 | `remove-aliases` | unconditional, whole module | any aliases | **— (every output `nullopt`)** | CERTAIN |
| 45 | `add-must-aliases` | each entry param whose shape-string == an unaliased output | — | **must (1)** | CERTAIN |
| 46 | `add-may-aliases` | same shape-match trigger as #45 | — | **may (0)** | CERTAIN |
| 47 | `flip-must-aliases` | EVERY existing alias (blanket) | may or must | **may (0)** | CERTAIN |
| 48 | `flip-may-aliases` | EVERY existing alias (blanket) | may or must | **must (1)** | CERTAIN |
| 72 | `aws_neuron_alias_to_must_alias` | EVERY existing alias (blanket) | may or must | **must (1)** | CERTAIN |
| 73 | `aws_neuron_buffer_donation_to_alias` | each donor whose donated input subshape == an unaliased output subshape | donor (no alias) | **may (0)** + donor removed | CERTAIN |

> **QUIRK —** the Flip names are counter-intuitive: the name denotes the *target kind to flip away from*, not a filter on the source. **`flip-must-aliases` makes everything MAY** (demotes must→may, leaves may→may); **`flip-may-aliases` makes everything MUST** (promotes may→must, leaves must→must). Both are *blanket* — they collect every alias via `ForEachAlias` with no per-kind filter, then re-stamp all of them with one fixed kind. There is no "flip only the ones currently must" semantics. The mechanism is a single `xor esi,1` (@`0x1ea5e11`) inverting the bool passed from each `Run` thunk.

> **NOTE —** `flip-may-aliases` (#48) and `aws_neuron_alias_to_must_alias` (#72) reach the *same end-state* (every alias → must) by different implementations. #48 uses the `FlipAliases` collect-then-re-`SetUpAlias` XOR helper; #72 rebuilds the config via a fresh `HloInputOutputAliasConfig(shape)` + `ForEachAliasWithStatus`. They are two entry points for the same intent at different pipeline stages — #48 in the pre-layout 44–48 block, #72 in the post-layout 72–73 block. (HIGH — same observable result, distinct TU.)

---

## Pass 44 — RemoveAliases

### Purpose

Clears every input/output alias, leaving each output `nullopt`. Used to reset the table before a fresh discovery pass, or to strip aliases the frontend set that a later stage will recompute. Does **not** touch `HloBufferDonorConfig`.

### Algorithm

```c
function RemoveAliases::Run(module):              // 0x1f5df70  (1899 B)
    config = module.entry_computation().alias_config()
    empty = Shape(/*tuple of*/ {})                       // empty result shape
    nodes = ShapeTree<optional<Alias>>::CreateNodes(empty)  // @0x1f5e0a3 — all nodes nullopt
    config.alias_.Assign(move(nodes))                    // @0x1f5e18b — InlinedVector Assign
    config.index_table_ = IndexTable(empty)              // @0x1f5e120 — rebuild flat index
    return /*changed=*/ true
```

No source-path string and no logging — a silent config rebuild. It shares the `ShapeTree<optional<Alias>>::CreateNodes` + `IndexTable` helpers with the rest of the family (same TU). (CERTAIN — callees demangled in the disasm.)

---

## Passes 45 / 46 — AddMustAliases / AddMayAliases

### Purpose

Discover structurally identical input↔output pairs that are *not yet aliased* and create an alias for each. `add-must-aliases` stamps kind=must; `add-may-aliases` stamps kind=may. Both are two-line `Run` thunks that tail-call the shared `AddNewAliases` helper with a bool.

### Entry Point

```text
AddMustAliases::Run (0x1e7ea90, 48 B)   mov esi, 1   ──┐
                                                        ├─→ AddNewAliases (0x1e7e280, 2053 B)
AddMayAliases::Run  (0x1e7eac0, 45 B)   xor esi,esi  ──┘    (must_alias bool in esi/sil)
```

`mov esi,1` @`0x1e7ea91` (must) and `xor esi,esi` @`0x1e7eac1` (may) set the bool, then both `call AddNewAliases` (@`0x1e7eaa5` / `0x1e7ead2`). (CERTAIN.)

### Algorithm

```c
function AddNewAliases(module, must_alias):        // 0x1e7e280  (2053 B)  hilo/hlo_passes/AddAlias.cc
    CHECK(entry_computation_ != nullptr)                              // @0x2108c9
    // pass 1 — index entry params by full shape string (with layout)
    multimap<string, int64> by_shape;
    for p in entry_computation.parameter_instructions():
        by_shape.insert({ p->shape().ToString(/*print_layout=*/true), // @0x1e7e403
                          p->parameter_number() });
    // pass 2 — match each result subshape to an unaliased same-shape param
    for each output subshape out_idx:
        if config.OutputHasAlias(out_idx):                            // @0x1e7e660
            continue
        if exists param with identical shape-string (memcmp)
           and not already GetAliasedOutput-bound:                    // @0x1e7e518
            kind = (AliasKind)must_alias;     // movzx eax,r12b @0x1e7e5d8 → var_26C → r9d @0x1e7e863
            status = config.SetUpAlias(out_idx, param_number, param_index, kind)  // @0x1e7e881
            CHECK(status.ok() && "Error in SetUpAlias!")              // @0x2e4618
            by_shape.erase(consumed entry)        // rebalance_for_erase — consume-on-match
    return /*changed=*/ (al)
```

The must/may bool is *only* the kind stamped on each newly created alias; the discovery trigger (a structurally identical, not-yet-aliased input/output pair) is identical for #45 and #46. The multimap allows several params to share a shape; each consumed entry is erased so two outputs don't both claim one param. (Discovery call sequence CERTAIN; the precise tie-break when multiple params share a shape is multimap-iteration order, first-erase-wins — HIGH on edge cases.)

> **NOTE —** the shape match is on `Shape::ToString(print_layout=true)`, i.e. element type + dims + layout, compared by `memcmp` of the printed strings. This block runs *before* layout is finalized, so two tensors that will later get the same layout but currently print differently will not be paired here. The post-layout `aws_neuron_buffer_donation_to_alias` (#73) instead matches by `Shape::Equal` on resolved layouts.

---

## Passes 47 / 48 — FlipMustAliases / FlipMayAliases

### Purpose

Re-stamp every existing alias with a single fixed kind (see the QUIRK above: the name is the kind to flip *away from*). `flip-must-aliases` → all may; `flip-may-aliases` → all must. Two-line thunks over `FlipAliases`.

### Entry Point

```text
FlipMustAliases::Run (0x1ea66c0, 48 B)   mov esi, 1   ──┐
                                                          ├─→ FlipAliases (0x1ea5e10, 2217 B)
FlipMayAliases::Run  (0x1ea66f0, 45 B)   xor esi,esi  ──┘    xor esi,1 → fixed kind = !arg
```

`mov esi,1` @`0x1ea66c1` (Flip**Must**, passes true) and `xor esi,esi` @`0x1ea66f1` (Flip**May**, passes false), both `call FlipAliases` (@`0x1ea66d5` / `0x1ea6702`). (CERTAIN.)

### Algorithm

```c
function FlipAliases(module, flip_from):           // 0x1ea5e10  (2217 B)  hilo/hlo_passes/FlipAlias.cc
    CHECK(entry_computation_ != nullptr)                              // @0x2108c9
    fixed_kind = flip_from XOR 1;     // xor esi,1 @0x1ea5e11 → movzx r12d,sil @0x1ea5e2b
    // collect ALL aliases (no per-kind filter)
    vector<pair<ShapeIndex, Alias>> all;
    config.ForEachAlias([&](out_idx, alias){ all.push_back({out_idx, alias}); })  // lambda 0x1ea4d10
    // rebuild into a freshly-emptied config
    config = empty-config(config.shape())
    for (out_idx, alias) in all:
        status = config.SetUpAlias(out_idx, alias.parameter_number,
                                   alias.parameter_index, fixed_kind)
        CHECK(status.ok() && "Error in SetUpAlias!")                  // @0x2e4618
    return /*changed=*/ true
```

The collector lambda @`0x1ea4d10` (1135 B) appends each `(ShapeIndex, Alias)` to a vector with no filter — confirming the blanket semantics. The fixed kind is `!flip_from`, independent of each alias's current kind. (CERTAIN — `xor esi,1` is the inversion.)

---

## Pass 72 — AliasToMustAlias

### Purpose

Promote every existing alias to must-alias. Same end-state as `flip-may-aliases`, but implemented as a config rebuild rather than the `FlipAliases` XOR helper, and placed in the post-layout 72–73 block (registry key `aws_neuron_alias_to_must_alias` @`0x2ce9f0`).

### Algorithm

```c
function AliasToMustAlias::Run(module):            // 0x200a250  (898 B)
    config = module.entry_computation().alias_config()
    // collect (forward no-op lambda#1 @0x2009e40, 7 B)
    config.ForEachAlias(collector)                                   // @0x200a2a4
    fresh = HloInputOutputAliasConfig(config.shape())
    // re-register every alias as must (lambda#2 @0x2009f10)
    fresh.ForEachAliasWithStatus([&](out_idx, alias) -> Status {     // @0x200a346
        return fresh.SetUpAlias(out_idx,
                                alias.parameter_number,   // mov rcx,[rcx]  @0x2009f27
                                alias.parameter_index,    // lea r8,[rcx+8] @0x2009f11
                                kMustAlias /* mov r9d,1  @0x2009f15 */);
    })
    config.Assign(move(fresh))    // swap alias-vector + IndexTable back
    return /*changed=*/ true
```

The lambda @`0x2009f10` hard-codes `mov r9d,1` (kMustAlias), reads `parameter_number` from `[rcx]` and `parameter_index` from `[rcx+8]` — this is the disasm that pins the `Alias` struct offsets. Re-stamping an already-must alias as must is a no-op. (CERTAIN.)

---

## Pass 73 — BufferDonationToAlias

### Purpose

Convert a **donatable input** (registered in `HloBufferDonorConfig`) into a concrete input/output **may**-alias, and consume the donor entry. This is where the frontend's "you may reuse this input buffer" permission (e.g. a JAX `donate_argnums`-style annotation) becomes an actual alias the allocator can act on. Registry key `aws_neuron_buffer_donation_to_alias` @`0x3daf48`; source TU `hilo/hlo_passes/setup_alias.cc` @`0x28a8e0`.

### Preconditions

Run `RET_CHECK`s, all string-anchored from `BufferDonationToAlias::Run` (CERTAIN):

| Check | String | Addr |
|---|---|---|
| `nullptr != entry_computation_` | — | `0x2108c9` |
| `entry_computation_layout_.has_value()` | `Check failed: entry_computation_layout_.has_value() ` | `0x2956d0` |
| `LayoutUtil::HasLayout(input_shape)` | — | `0x311950` |
| `LayoutUtil::HasLayout(output_shape)` | — | `0x31cc90` |

Both the donated input subshape and the candidate output subshape must be layout-resolved — which is why #73 is sequenced *after* `aws_neuron_ensure_descending_layout_in_root` (#71). (CERTAIN.)

### Algorithm

This pass reuses the upstream XLA matcher `OptimizeInputOutputBufferAlias::Build` (`xla/hlo/transforms/simplifiers/optimize_input_output_buffer_alias.cc` @`0x2f0600`), inlined into `Run`.

```c
function BufferDonationToAlias::Run(module):       // 0x200d0b0  (4027 B)
    CHECK(entry_computation_ != nullptr)
    CHECK(entry_computation_layout_.has_value())
    config       = entry.alias_config()
    donor_config = entry.buffer_donor_config()
    // build a hash table of OUTPUT subshapes keyed by Shape::Hash
    //   (PrimitiveType + dims<long,6> + dynamic-dims<bool,6> + Layout)
    out_table = {}
    for_each_output_subshape(out_idx, out_shape):
        CHECK(LayoutUtil::HasLayout(out_shape))
        if !config.OutputHasAlias(out_idx):
            out_table[Shape::Hash(out_shape)].push_back(out_idx)
    // for each donated input subshape, probe + confirm with Shape::Equal
    for_each_donor(param_number, param_index, in_shape):        // stride 0x158 / entry
        CHECK(LayoutUtil::HasLayout(in_shape))
        out_idx = out_table.probe(Shape::Hash(in_shape))
                  filtered by Shape::Equal(in_shape, out_shape)  // bitwise buffer-compatible
        if out_idx found:
            config.SetUpAlias(out_idx, param_number, param_index,
                              kMayAlias /* xor r9d,r9d @0x200dbe1 */);    // @0x200dbff
            donor_config.RemoveBufferDonor(param_number, param_index);    // @0x200dc2d
    return /*changed=*/ true
```

> **QUIRK —** a donation produces a **kMayAlias**, not a must-alias (`xor r9d,r9d` @`0x200dbe1`). A reader might assume a donated buffer is force-shared in place; it is not. A donation is a *permission* ("you may reuse this input buffer for an output"), so the tensorizer/runtime stays free to honor it or insert a copy if liveness forbids reuse. The donor entry is then removed (`RemoveBufferDonor` @`0x9643cd0`) so the same input is not double-counted as both a free donor and an alias — `HloBufferDonorConfig::Verify` errors with `"Input %lld ... is registered as a buffer donor. However, it is also in the input output alias config."` (@`0x39e6d8`) if both survive.

---

## Alias Family Infrastructure Functions

| Function | Addr | Size | Role | Conf |
|---|---|---|---|---|
| `RemoveAliases::Run` | `0x1f5df70` | 1899 B | clear all aliases (empty ShapeTree) | CERTAIN |
| `AddMustAliases::Run` | `0x1e7ea90` | 48 B | thunk → `AddNewAliases(must=1)` | CERTAIN |
| `AddMayAliases::Run` | `0x1e7eac0` | 45 B | thunk → `AddNewAliases(must=0)` | CERTAIN |
| `(anon)::AddNewAliases` | `0x1e7e280` | 2053 B | discover param↔output matches, `SetUpAlias` | CERTAIN |
| `FlipMustAliases::Run` | `0x1ea66c0` | 48 B | thunk → `FlipAliases(true)` → all may | CERTAIN |
| `FlipMayAliases::Run` | `0x1ea66f0` | 45 B | thunk → `FlipAliases(false)` → all must | CERTAIN |
| `(anon)::FlipAliases` | `0x1ea5e10` | 2217 B | re-register every alias, kind = arg XOR 1 | CERTAIN |
| `FlipAliases collector lambda` | `0x1ea4d10` | 1135 B | `ForEachAlias` → (ShapeIndex,Alias) vector | CERTAIN |
| `AliasToMustAlias::Run` | `0x200a250` | 898 B | rebuild config, all → kMustAlias | CERTAIN |
| `AliasToMustAlias re-register lambda` | `0x2009f10` | 43 B | `SetUpAlias(.., kMustAlias=1)` | CERTAIN |
| `AliasToMustAlias collector lambda` | `0x2009e40` | 7 B | `ForEachAlias` no-op forward | CERTAIN |
| `BufferDonationToAlias::Run` | `0x200d0b0` | 4027 B | donor → may-alias; remove donor | CERTAIN |
| `HloInputOutputAliasConfig::SetUpAlias` | `0x9644da0` | 2373 B | core alias-register primitive | CERTAIN |
| `HloInputOutputAliasConfig::OutputHasAlias` | `0x96414e0` | 73 B | predicate: output already aliased? | CERTAIN |
| `HloInputOutputAliasConfig::GetAliasedOutput` | `0x9642d50` | 189 B | param → output lookup | CERTAIN |
| `HloInputOutputAliasConfig::ForEachAlias` | `0x9642850` | 98 B | visit aliases | CERTAIN |
| `HloInputOutputAliasConfig::ForEachAliasWithStatus` | `0x9642ca0` | 150 B | visit aliases (fallible) | CERTAIN |
| `HloBufferDonorConfig::RemoveBufferDonor` | `0x9643cd0` | 907 B | consume the donor entry | CERTAIN |
| `xla::ConvertInputOutputAlias(config, Builder*)` | `0x75a93a0` | — | HLO → MHLO `ArgResultAliasAttr` | CERTAIN |

### Stock-XLA vs Neuron-authored

> **NOTE —** the dividing line. **Upstream XLA (reused verbatim):** `HloInputOutputAliasConfig` and its `SetUpAlias` / `OutputHasAlias` / `GetAliasedOutput` / `ForEachAlias*`; `HloBufferDonorConfig::RemoveBufferDonor`; the `AliasKind` enum; the `OptimizeInputOutputBufferAlias::Build` matcher (#73 inlines it); `ConvertInputOutputAlias` and `mhlo::ArgResultAliasAttr`. **Neuron-authored (the thin drivers):** the seven `Run` bodies plus the two shared helpers `AddNewAliases` (`AddAlias.cc`) and `FlipAliases` (`FlipAlias.cc`) and `BufferDonationToAlias` (`setup_alias.cc`). Neuron's code is only kind-selection + scan-policy; every actual table mutation goes through an upstream primitive. This is the same "thin XLA wrapper" pattern as the collective combiners (see [Collective Combiners](collective-combiners.md)).

---

## Adversarial Self-Verification

The five strongest claims, each re-challenged against the binary:

1. **All seven passes exist and are thin drivers over one upstream config.** Re-checked: the demangled symbol names for all seven `Run` bodies are present in `disasm/` (`RemoveAliases`, `AddMust/MayAliases`, `FlipMust/MayAliases`, `AliasToMustAlias`, `BufferDonationToAlias`), and Add/Flip `Run` bodies are 45–48 B thunks that `call` the shared helpers. **CONFIRMED.**

2. **AliasKind constants: Add/Flip pass a bool, FlipAliases inverts it, donation forces may, promote forces must.** Re-checked in disasm: `mov esi,1`/`xor esi,esi` in the four Add/Flip thunks; `xor esi,1` @`0x1ea5e11` in `FlipAliases`; `xor r9d,r9d` @`0x200dbe1` then `SetUpAlias` @`0x200dbff` in `BufferDonationToAlias`; `mov r9d,1` @`0x2009f15` in the `AliasToMustAlias` lambda. **CONFIRMED** — including the counter-intuitive Flip direction.

3. **The kind reaches MHLO via `ConvertInputOutputAlias` → `ArgResultAliasAttr`.** Re-checked: `referenced_by_functions` on `may_alias` @`0x21e9eb` and `must_alias` @`0x288162` both name the `ConvertInputOutputAlias(const HloInputOutputAliasConfig&, mlir::Builder*)` per-alias lambda; `must_alias` @`0xbd9380` is referenced by `ArgResultAliasAttr::{parse,print}`. The bridge symbol `ConvertInputOutputAlias` @`0x75a93a0` exists in `context/`. **CONFIRMED.**

4. **The `Alias` struct is `{param_number @+0, param_index @+8, kind}`.** Re-checked: the `AliasToMustAlias` lambda @`0x2009f10` does `mov rcx,[rcx]` (param_number @+0) and `lea r8,[rcx+8]` (param_index @+8) before `SetUpAlias`. The `ShapeTree<optional<Alias>>` storage type appears verbatim in the `RemoveAliases` callee demangling. **CONFIRMED** on offsets; the exact `kind` byte position inside the struct is not separately pinned and is **INFERRED** from the must_alias-bool semantics (the SetUpAlias kind arg).

5. **#73 produces a may-alias because donation is a permission, and removes the donor.** Re-checked: `xor r9d,r9d` (kMayAlias) feeds `SetUpAlias`, immediately followed by `RemoveBufferDonor` @`0x200dc2d`; the `HloBufferDonorConfig::Verify` string @`0x39e6d8` confirms a donor and an alias for the same input is an error state, motivating the removal. The *rationale* ("permission not contract") is **STRONG** (consistent with the may-kind + the Verify invariant) rather than CERTAIN, since no string spells out the design intent.

Residual gaps: which passes actually run, and in what order, is driver-supplied (Python / `HLOToTensorizer.so`), not fixed by registration order; the 44–48 pre-layout / 72–73 post-layout adjacency is the strong proxy. Who *populates* `HloBufferDonorConfig` (`AddBufferDonor`) upstream of #73 is the frontend annotation path, not traced in `hlo-opt`. The Add* tie-break when multiple params share a shape is multimap-iteration order (HIGH, not CERTAIN on edge cases).

---

## Related Passes

| Slot | Name | Relationship |
|---|---|---|
| 43 | `add-logit-output` | immediately precedes the 44–48 alias block |
| 71 | `aws_neuron_ensure_descending_layout_in_root` | resolves layout; gate for #72/#73's `HasLayout` RET_CHECKs |
| 74 | `decompose-scalar-reduce` | follows the 72–73 block |

## Cross-References

- [tensor_map alias](../formats/neff-json-sidecars.md) — §12.3, how the BIR `tensor_map` consumes the resolved alias to fold input/output onto one buffer
- [Output-Operand Aliasing](../bir/memory-location.md) — Part 6, the backend/allocator end of the contract: must-alias = unconditional share, may-alias = liveness-gated
- [TensorizerLegalization Aliasing](tensorizer-legalization.md) — §4.40, where the MHLO `ArgResultAliasAttr` is legalized into the tensorizer dialect
- [Collective Combiners](collective-combiners.md) — the sibling "thin XLA wrapper" family; same Neuron-driver-over-upstream-core pattern
- [The hlo-opt Pass Registry](pass-registry.md) — the `--passes` table that registers slots 44–48 and 72–73
