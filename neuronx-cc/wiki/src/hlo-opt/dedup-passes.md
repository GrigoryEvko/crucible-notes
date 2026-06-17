# Duplicate-Parameter & Kernel Dedup Passes

> *All addresses on this page apply to `neuronx_cc 2.24.5133.0+58f8de22` (cp310 wheel, `neuronxcc/starfish/bin/hlo-opt`, not stripped). For `.text` and `.rodata` in this binary VA == file offset; addresses resolve directly in `disasm/` and the IDA `*_function_addresses.json` sidecar. Other versions will differ.*

## Abstract

Two `hlo-opt` passes collapse structurally-identical work back onto a single canonical instance and let a trailing dead-code sweep reap the orphans. They share an idiom — **keep-first / replace-rest / DCE-erase** — but key on completely different things:

- **#106 `neuron_all_gather_duplicate_remover`** (`xla::hilo::NeuronDuplicateParameterAllGatherRemover`, `Run` @0x1f8e890). A CSE pass for all-gathers. For every all-gather it traces the gathered value *up through layout-only / forwarding ops back to a root `parameter`* (`FindRawParameter` @0x1f8d640), then keys a per-computation hash map on **(raw-parameter identity + the gather's effective collective config)**. The first gather for each key is kept; every later gather with an identical key is `ReplaceAllUsesWith`'d onto it. Tensor-parallel / FSDP graphs gather the same sharded weight once per consuming layer; this folds them to one gather.

- **#76 `neuron-preprocess-kernel-duplicate-remover`** (`xla::hilo::NeuronPreprocessKernelDuplicateRemover`, `Run` @0x1fbff50). A dedup pass for **flash-attention preprocess kernels** emitted as `AwsNeuronCustomNativeKernel` custom-calls. It finds the *first* such kernel (`FindFirstPreprocessKernel` @0x1fbf760), validates a `q`/`kv` slice pair, then for every other kernel whose deeply-nested inner kernel carries the **same backend-config string** (`IsFlashAttentionKernel` @0x1fbf9f0) it clones the canonical `first` onto that duplicate's operands — after substituting the canonical `q`/`kv` tile-ref slices via two `HloInstruction::IdenticalInternal` (@0x967d3c0) checks — and re-points the duplicate's users at the clone.

Both are stateless 8-byte `HloModulePass` subclasses (vptr-only, no member state). Both end with a single `xla::HloDCE::Run` over the module that removes the now-unused duplicates. This page documents both passes' **dedup keying** explicitly, reproduces both `Run` bodies and their helpers as annotated C pseudocode against the real symbols, and adversarially re-checks the five strongest claims against the binary.

> **CROSS-REF.** This page covers the dedup half of backing report B23. The *other* half of B23 — `DeletePermute` (#89, `aws_neuron_delete_permute`), the OpExpander that strips degenerate `all-reduce`/`collective-permute` collectives — is documented on **[4.7 — Flip-Collective OpExpander Family](flip-collective-opexpander.md)** and is **not** re-covered here. For the all-gather *combiner* (which fuses sibling gathers rather than deduping identical ones), see **[4.5 — Collective Combiners](collective-combiners.md)**; the combiner and this remover are complementary — the combiner makes one big gather out of *different* gathers, this remover makes one gather out of *identical* ones.

| | |
|---|---|
| **Pass #106** | `neuron_all_gather_duplicate_remover` / `NeuronDuplicateParameterAllGatherRemover` |
| `Run` | @0x1f8e890 (4246 B) · `name()` @0x1f8c830 · vtable `_ZTV…` @0x4115b0 (vptr @0x4115c0) · registrar @0x1e702d0 |
| helpers | `FindRawParameter` @0x1f8d640 (4285 B) · `xla::GetReplicaGroups` @0x1f8ca40 (398 B) |
| **Pass #76** | `neuron-preprocess-kernel-duplicate-remover` / `NeuronPreprocessKernelDuplicateRemover` |
| `Run` | @0x1fbff50 (2719 B) · `name()` @0x1fbf700 · vtable `_ZTV…` @0x411fd0 (vptr @0x411fe0) · registrar @0x1e70820 |
| helpers | `FindFirstPreprocessKernel` @0x1fbf760 (616 B) · `IsFlashAttentionKernel` @0x1fbf9f0 (421 B) · `xla::Cast<HloSliceInstruction>` @0x1fbfd10 |
| **Shared callees** | `IdenticalInternal` @0x967d3c0 · `CloneWithNewOperands` @0x967fbe0 · `ReplaceAllUsesWith` @0x967a910 · `HloComputation::AddInstruction` @0x96370d0 · `HloDCE::Run` @0x913fb70 · `HloOpcodeString` @0x96bb550 |

> **NOTE — decompile-skipped binary.** `hlo-opt` is built with `NVOPEN_IDA_SKIP_DECOMPILE`; there is no Hex-Rays output, only disassembly. Every function symbol, opcode byte, string, and field offset cited here is read straight from the disasm / IDA `_function_addresses.json` and is **CERTAIN**; the exact branch structure of the open-addressing hash probe and the clone-loop operand dance is reconstructed from disasm and tagged where it weakens to HIGH/MED.

---

## Opcode Decode Oracle

Both passes test the single opcode byte at `HloInstruction+0x14`. Every byte below is resolved against the authoritative `xla::HloOpcodeString` switch @0x96bb550 (CERTAIN):

| byte | opcode | used by |
|---|---|---|
| 0x04 | `all-gather` (kAllGather) | #106 outer scan + FindRawParameter family |
| 0x06 | `all-gather-start` | #106 FindRawParameter family `(op & ~2)==4` |
| 0x25 (37) | `convert` | #106 FindRawParameter (shape-guarded forwarding) |
| 0x2B (43) | `custom-call` (kCustomCall) | #76 FindFirst + IsFlashAttentionKernel |
| 0x3A (58) | `get-tuple-element` | #106 FindRawParameter forwarding |
| 0x48 (72) | `opt-barrier` (kOptimizationBarrier) | #106 FindRawParameter forwarding |
| 0x4C (76) | `parameter` (kParameter) | #106 FindRawParameter stop / Run gate |
| 0x5B (91) | `reshape` (kReshape) | #76 Run q/kv user RET_CHECK |
| 0x6E (110) | `slice` (kSlice) | #76 Run q/kv slice RET_CHECK |
| 0x78 (120) | `tuple` (kTuple) | #106 FindRawParameter forwarding |
| 0x0D (13) | PrimitiveType `TUPLE` | #76 FindFirst output-shape gate |

> **QUIRK — operand count is `field >> 1`.** Throughout both bodies, the operand count is read as `[inst+0x18] >> 1`, and user-count tests as `[inst+0x38] & 3`. The low bit of the operand-list field is an inlined-vs-heap flag; the count lives in the high bits. Reading `[inst+0x18]` raw and comparing to a count is a classic off-by-2×-plus-one mistake when reimplementing — always shift right by one first. (CERTAIN — the `>>1` idiom is reused at every operand/user site in both passes.)

---

## #106 — NeuronDuplicateParameterAllGatherRemover

### Purpose

Tensor-parallel and FSDP lowerings shard a weight `parameter` and emit one `all-gather` per layer that consumes the full weight. Those gathers are bit-identical work over the same parameter. This pass keys each gather on its root parameter plus its effective gather config, keeps the first, and rewrites the rest onto it. It is **intra-computation** (the map is rebuilt per computation) and **unconditional** — there is no cost model or threshold; any structurally-identical gather of the same parameter is collapsed.

### Entry Point

`Run` @0x1f8e890. Mangled symbol (CONFIRMED in `_function_addresses.json`):

```
_ZN3xla4hilo40NeuronDuplicateParameterAllGatherRemover3RunEPNS_9HloModuleERK…flat_hash_set…
```

The pass object is 8 bytes (vptr only) — the registrar lambda @0x1e702d0 does `operator new(8)` then stores vptr `off_4115C0` (= `_ZTV…` @0x4115b0 + 0x10). `name()` @0x1f8c830 returns the 0x23-byte StringRef `neuron_all_gather_duplicate_remover`.

### The Dedup Key

The per-Run map is an `absl::flat_hash_map` whose key tuple type is pinned by the `raw_hash_set` template symbol at the `prepare_insert` call site (~0x1f8f475):

```cpp
absl::flat_hash_map<
    std::tuple< xla::HloInstruction*,           // [0] raw parameter pointer (POINTER IDENTITY)
                long,                            // [1] index-path scalar #1 (-1 if none)
                xla::PrimitiveType,              // [2] AG output element dtype
                long,                            // [3] index-path scalar #2 (-1 if none)
                xla::Shape,                      // [4] AG output shape (layout-sensitive)
                std::vector<std::vector<long>> > // [5] replica_groups
  , xla::HloInstruction* >;                      // value = the KEPT all-gather
```

Inside a map node the six discriminators live at fixed offsets (from the FindRawParameter key-struct stores and the Run compares):

```
node+0x000..+0x017 : std::vector<std::vector<long>> replica_groups   (tuple elem [5])
node+0x018..+0x157 : xla::Shape  (AG output shape)                   (tuple elem [4])
node+0x158 (long)  : tuple elem [1]  — first index-path scalar
node+0x160 (int32) : tuple elem [2]  — PrimitiveType (AG out elem type)
node+0x168 (long)  : tuple elem [3]  — second index-path scalar
node+0x170 (ptr)   : tuple elem [0]  — raw parameter HloInstruction*
node+0x178 (ptr)   : VALUE — the kept all-gather HloInstruction*
```

Map equality requires, in order, all of:

1. **pointer-identity of the raw parameter** — `[node+0x170] == key.raw_param`;
2. the three scalars match — `[node+0x168]==idx3`, `[node+0x160]==primtype`, `[node+0x158]==idx1`;
3. **`xla::Shape::Equal`** of the AG **output** shape (full, layout-sensitive);
4. **deep `replica_groups` equality** — per-group size compare then `memcmp` of the `long[]` group data.

> **GOTCHA — the gather dimension is NOT a key column.** Reading the tuple, you might expect `all_gather_dimension` (`[ag+0x258]`), `constrain_layout` (`[ag+0x250]`), and `use_global_device_ids` (`[ag+0x260]`) to be stored explicitly. They are not. They are captured **implicitly**: the output `Shape` (elem [4]) encodes the gather dimension and gathered extent, and the `replica_groups` (elem [5]) encode the device participation. Two gathers with the same parameter, same output shape, same dtype, and same replica-groups are duplicates regardless of how those fields were spelled. (CERTAIN — the key tuple has exactly six elements and none is `dim`/`constrain`/`global-id`.)

So in plain English: **two all-gathers are duplicates iff they trace back to the same `parameter` through the same index chain, and have the same output shape (incl. layout), same element dtype, and identical replica-groups.**

### FindRawParameter — the up-walk that defines "same parameter operand"

`FindRawParameter(this, start, all_gather)` @0x1f8d640 snapshots the all-gather's attributes once at entry, then walks `start` upward through forwarding ops until it bottoms out at a `parameter` (or stops on a non-forwarding op, leaving the raw-param pointer null → the gather is skipped).

```c
// FindRawParameter @0x1f8d640 — returns the key struct (CERTAIN: opcodes & GetReplicaGroups)
KeyStruct FindRawParameter(Self* this, HloInstruction* node, HloInstruction* ag) {
    key.primtype = ag->shape().element_type();          // 0x1f8d693  tuple elem [2]
    // assert ag is the all-gather FAMILY: (opcode & ~2)==4 matches 4 (all-gather) OR 6 (all-gather-start)
    assert((ag->opcode() & ~2) == 4);                   // 0x1f8d6c5  and eax,0FFFFFFFDh ; cmp al,4
    key.replica_groups = xla::GetReplicaGroups(ag);     // 0x1f8d6da  deep-copied vector<vector<long>> -> elem [5]
    key.idx1 = key.idx3 = -1;

    for (;;) {                                          // log "Finding raw parameter for instruction: "
        switch (node->opcode()) {                       // byte at node+0x14
        case 0x4C: /* parameter */                      // 0x1f8d6f1  cmp al,4Ch
            key.raw_param = node;  return key;           //   BOTTOM OUT -> store node @ key+0x170, emit key
        case 0x3A: /* get-tuple-element */              // 0x1f8d6f9  cmp al,3Ah
            // log "Found get-tuple-element at index: "; record tuple_index() -> idx; descend operand(0)
            node = node->operand(0);  continue;
        case 0x78: /* tuple */                          // 0x1f8d701  cmp al,78h
            // log "Found tuple, moving to element at index: "; bounds-check idx in [0, operand_count); descend operand(idx)
            node = node->operand(idx);  continue;
        case 0x48: /* opt-barrier */                    // 0x1f8d709  cmp al,48h
            // log "Found optimization barrier, moving to its input"; descend operand(0)
            node = node->operand(0);  continue;
        case 0x25: /* convert */                        // 0x1f8d711  cmp al,25h
            // SHAPE-GUARDED: only a *pure-bitcast* convert is transparent
            if (!Shape::Equal(node->operand(0)->shape(), node->shape()))  // 0x1f8e142 Shape::Equal::operator()
                return key /* raw_param stays null -> AG skipped */;
            node = node->operand(0);  continue;          // log "Found convert operation, moving to its input"
        case 0x04: case 0x06: /* all-gather[-start] */
            node = node->operand(0);  continue;          // log "Found all-gather operation, moving to its input"
        default:
            return key;                                  // STOP at non-forwarding op -> raw_param null -> AG skipped
        }
    }
}
```

> **NOTE — the convert is shape-guarded.** A `convert` is tunnelled through only when its operand and result shapes are `Shape::Equal` (the `Shape::Equal::operator()` call @0x1f8e142). That restricts it to pure bitcast/layout converts; a dtype-changing convert *stops* the walk (and the gather is left undeduped). This is the only forwarding op with a guard — gte/tuple/opt-barrier/nested-all-gather are tunnelled unconditionally. (CERTAIN — the only `Shape::Equal` call in FindRawParameter sits on the `convert` arm.)

The up-walk logs each hop (`Finding raw parameter for instruction:`, `Found get-tuple-element at index:`, `Found convert operation, moving to its input`, `Found optimization barrier, moving to its input` — all CONFIRMED in the binary), making FindRawParameter trivial to trace at `--v=2`.

### Run — keep-first / replace-rest / DCE

```c
// Run @0x1f8e890 — StatusOr<bool>
bool changed = false;
log("Running NeuronDuplicateParameterAllGatherRemover pass");
for (HloComputation* comp : module->computations()) {         // [module+0x40]..[+0x48]
    log("Processing computation: " + comp->name());
    flat_hash_map<Key, HloInstruction*> seen;                 // PER-COMPUTATION, re-zeroed @0x1f8eedb
    for (HloInstruction* inst : comp->instructions()) {       // stride-0x10 list, elem@+8
        if (inst->opcode() != 0x04 /*kAllGather*/) continue;  // 0x1f8e990
        Key k = FindRawParameter(this, inst->operand(0), inst);// 0x1f8ea1d
        if (k.raw_param == nullptr ||                         // 0x1f8ea29
            k.raw_param->opcode() != 0x4C /*kParameter*/)     // 0x1f8ea2e  MUST bottom at a parameter
            { continue; }                                     //   else skip this gather entirely
        log("Found all-gather instruction: " + inst->ToString());
        auto it = seen.find(k);                               // open-addressing probe ~0x1f8eb83
        if (it != seen.end()) {                               // DUPLICATE
            HloInstruction* keep = it->second;                //   [node+0x178] = first gather for key
            log("Replacing all-gather with previous result: " + keep->ToString());
            inst->ReplaceAllUsesWith(keep, /*name=*/"");      // 0x1f8f66f  re-point users to KEEP
            changed = true;
        } else {                                              // FIRST occurrence
            log("Storing all-gather result for future use");
            seen.emplace(k, inst);                            // value = inst  @0x1f8f502
        }
    }
}
if (changed)                                                  // 0x1f8eea2
    HloDCE().Run(module, execution_threads);                  // 0x1f8ef0e  erase the dead duplicates
log("NeuronDuplicateParameterAllGatherRemover pass complete");
return changed;
```

The duplicate is **never explicitly removed** in the loop — `ReplaceAllUsesWith` makes it unused, and the single trailing `HloDCE::Run` (gated on `changed`) reaps it. The kept gather is the **first inserted** for the key, i.e. the post-order-earliest gather wins.

> **GOTCHA — dedup is intra-computation only.** `seen` is rebuilt per computation (zeroed @0x1f8eedb at the top of each computation loop). Identical gathers of the same parameter in *different* computations are not folded together. (CERTAIN.)

---

## #76 — NeuronPreprocessKernelDuplicateRemover

### Purpose

Flash-attention lowering emits a *preprocess* kernel — an `AwsNeuronCustomNativeKernel` custom-call that packs the `q` and `kv` tiles — and the graph often re-emits the same preprocess for every attention head/layer. This pass finds the first such kernel, treats it as canonical, and rewrites every duplicate onto a clone of the canonical kernel parameterised by the duplicate's own operands. The dedup criterion is the **backend-config string of the deeply-nested inner kernel**, not a flat structural compare.

### Entry Point

`Run` @0x1fbff50. Pass object is 8 bytes (vptr only); registrar @0x1e70820 stores vptr `off_411FE0` (= `_ZTV…` @0x411fd0 + 0x10). `name()` @0x1fbf700 returns the 0x2A-byte StringRef `neuron-preprocess-kernel-duplicate-remover`.

> **CORRECTION.** An earlier sketch (registry survey S2-02 §4.3) described this as a trivial "if `Identical`, replace, erase" loop and placed the `q`/`kv` `kReshape` assertions inside `IsFlashAttentionKernel`. The binary disagrees on three counts, corrected throughout below: (1) the `q`/`kv` slice + reshape RET_CHECKs live in **`Run`**, not in `IsFlashAttentionKernel` (which is a pure backend-config/operand-chain predicate); (2) the rewrite is `CloneWithNewOperands(first)` → `AddInstruction` → `ReplaceAllUsesWith(clone)`, not `ReplaceAllUsesWith(first)`; (3) `IdenticalInternal` is called **twice per duplicate** to substitute the canonical `q`/`kv` slices, not once as a top-level identity gate.

### FindFirstPreprocessKernel — finding the canonical kernel

`FindFirstPreprocessKernel(module)` @0x1fbf760 returns the first `AwsNeuronCustomNativeKernel` custom-call matching a flash-attention fingerprint, or `nullptr`.

```c
// FindFirstPreprocessKernel @0x1fbf760 (HIGH; field semantics of array_state MED)
for (comp in module->computations())
  for (I in comp->instructions()) {
    if (I->opcode() != 0x2B /*kCustomCall*/) continue;                 // 0x1fbf7e3 cmp [..+14h],2Bh
    if (I->custom_call_target() != "AwsNeuronCustomNativeKernel") continue; // 0x1fbf87c -> str @0x24f4f5
    if ((I->operand_count >> 1) != 1) continue;                        // 0x1fbf8a0  exactly 1 operand
    op0 = I->operand(0);
    if ((op0->shape rank >> 1) != 3) continue;                         // 0x1fbf8d1  operand rank == 3
    if (op0->shape().dims[0] != 1) continue;                           // 0x1fbf901  dims[0] == 1
    d_self = I->shape().dims[2];                                       // 0x1fbf929
    d_op0  = op0->shape().dims[2];                                     // 0x1fbf954
    if (2 * d_op0 != d_self) continue;                                 // 0x1fbf958  output trailing dim == 2x operand's
    return I;                                                          // first match
  }
return nullptr;                                                        // 0x1fbf99d
```

The trailing-dim doubling (`2*d_op0 == d_self`) is the flash-attention fingerprint — the kernel packs `q + kv` into one tensor, doubling the last dimension. If `nullptr`, `Run` exits with `changed=false`.

### IsFlashAttentionKernel — the duplicate-match predicate

`IsFlashAttentionKernel(inst, name)` @0x1fbf9f0, where `name` is the **first kernel's backend-config raw string** (captured by `Run`). An instruction is a *duplicate* iff it is an `AwsNeuronCustomNativeKernel` whose deeply-nested inner preprocess kernel carries the **same backend-config string** as `first`, while the outer kernel is **not** byte-identical to `first`.

```c
// IsFlashAttentionKernel @0x1fbf9f0 (HIGH; chain depth CERTAIN, sub-graph semantics MED)
bool IsFlashAttentionKernel(HloInstruction* inst, const std::string& name) {
    if (inst->opcode() != 0x2B /*kCustomCall*/) return false;          // 0x1fbfa01
    n = inst->operand_count >> 1;
    if (inst->custom_call_target() != "AwsNeuronCustomNativeKernel") return false; // -> str @0x24f4f5
    cfg = inst->backend_config().GetRawStringWithoutMutex();           // Mutex-guarded getter @0x96cc310
    if (cfg.len == name.len && memcmp(cfg, name) == 0) return false;   // 0x1fbfb60  EXACTLY first -> exclude
    if (n <= 7) return false;                                          // 0x1fbfa7c  need >= 8 operands
    // fixed operand(0) descent to the inner preprocess custom-call:
    o = inst->operand(n-1);
    if ((o->operand_count>>1)==0) return false;
    o = inst->operand(n-1)->operand(0)->operand(0);                    // 0x1fbfaa4 / 0x1fbfaae
    if ((o->operand_count>>1)==0) return false;
    o = o->operand(0)->operand(0)->operand(0);                         // 0x1fbfad0..0x1fbfae4
    if (o->opcode() != 0x2B /*kCustomCall*/) return false;             // 0x1fbfae9
    inner = o->operand(0)->operand(0)->operand(0);                     // 0x1fbfaf9..0x1fbfb0d -> inner kernel
    icfg = inner->backend_config().GetRawStringWithoutMutex();         // 0x1fbfb24
    if (icfg.len != name.len) return false;                           // 0x1fbfb3f
    return icfg.len == 0 || memcmp(icfg, name) == 0;                  // 0x1fbfb4e
}
```

> **QUIRK — the backend-config string IS the dedup key.** The match is byte-equality of the *inner* kernel's backend-config raw string against `first`'s, with the early `memcmp==0 ⇒ false` excluding `first` itself and any exact clone. The fixed five-/six-level `operand(0)` descent is the canonical flash-attn preprocess wiring (outer custom-call → reshape/slice chain → inner custom-call). The `n <= 7 ⇒ false` guard requires the matched outer kernel to carry **≥ 8 operands** (the `7` is a verbatim immediate at 0x1fbfa7c). The descent depth and the operand count are CERTAIN from disasm; the precise HLO sub-graph each `operand(0)` hop walks is INFERRED (would need a real flash-attn dump to pin) — MED.

### Run — validate q/kv, clone-per-duplicate, DCE

```c
// Run @0x1fbff50 (logic HIGH; clone-loop flow MED)
first = FindFirstPreprocessKernel(module);                            // 0x1fbff94
if (first == nullptr) return false;
name = first->backend_config().GetRawStringWithoutMutex();            // 0x1fbffb7

// FOUR RET_CHECKs (all in Run, all CONFIRMED strings):
RET_CHECK(first->user_count() == 2);                                  // str @0x387ba0
u0 = first->users()[0];  u1 = first->users()[1];
RET_CHECK(u0->opcode()==0x6E && u1->opcode()==0x6E /*kSlice*/);       // str @0x370698
qS = Cast<HloSliceInstruction>(u0);  kS = Cast<HloSliceInstruction>(u1); // @0x1fbfd10 asserts kSlice
if (qS.slice_starts[..] > kS.slice_starts[..]) swap(qS, kS);          // 0x1fc010c  SMALLER start -> q
RET_CHECK(qS->user_count()==1 && qS->users()[0]->opcode()==0x5B/*kReshape*/); // str @0x2d9758
RET_CHECK(kS->user_count()==1 && kS->users()[0]->opcode()==0x5B/*kReshape*/); // str @0x34c108
q_reshape  = qS->users()[0];
kv_reshape = kS->users()[0];

bool changed = false;
for (comp in module->computations())
  for (inst in comp->instructions()) {
    if (!IsFlashAttentionKernel(inst, name)) continue;                // 0x1fc01f9
    ops = inst->operands();                                           // copied into inlined_vector<HloInstruction*,2>
    // substitute canonical q/kv tile-ref slices via per-operand IDENTITY:
    if (IdenticalInternal(*ops[n-2], *q_reshape,  eq_ptr, eq_ptr,
                          /*layout_sensitive=*/true, 0,0,0))          // 0x1fc0287  #1
        ops[n-2] = qS;                                                // 0x1fc032e  substitute canonical q
    if (IdenticalInternal(*ops[n-1], *kv_reshape, eq_ptr, eq_ptr,
                          /*layout_sensitive=*/true, 0,0,0))          // 0x1fc02ef  #2
        ops[n-1] = kS;                                                // 0x1fc0480  substitute canonical kv
    clone = first->CloneWithNewOperands(first->shape(), ops, nullptr);// 0x1fc0382
    newK  = comp->AddInstruction(move(clone), "");                    // 0x1fc0394
    inst->ReplaceAllUsesWith(newK, "");                               // 0x1fc03f4  re-point duplicate's users
    changed = true;
  }
HloDCE::Run(module, execution_threads);                              // 0x1fc05d3  stack-local HloDCE, vtable off_D26ED8
if (VmoduleActivated(".../neuron_preprocess_kernel_duplicate_remover.cc", 2))
    LOG(INFO) << "NeuronPreprocessKernelDuplicateRemover pass complete"; // str @0x2c2ed0, len 52
return changed;
```

> **NOTE — the dedup KEY here is two-level.** The *coarse* key is the inner-kernel backend-config string (IsFlashAttentionKernel) — that decides *which* kernels are duplicates. The *fine* key is per-operand pointer-identity against the canonical `q`/`kv` reshapes (the two `IdenticalInternal` calls) — that decides *which two operands* of the duplicate get rewritten to the canonical tile-ref slices before cloning. The clone re-instantiates `first` once per duplicate, with the duplicate's (substituted) operand set.

### IdenticalInternal as used here

`HloInstruction::IdenticalInternal` @0x967d3c0 (1549 B). Both call sites push the same four trailing bools; on x86-64 the last `push 1` is `arg_0`, so the flags decode (CERTAIN) to:

```
layout_sensitive = true,  ignore_channel_id = false,
ignore_commutative_operand_order = false,  sharding_must_match = false
```

The two `FunctionRef` comparators are `std::equal_to<HloInstruction const*>` and `std::equal_to<HloComputation const*>` — i.e. **pointer-identity**. So `IdenticalInternal` here is *layout-sensitive shape + opcode + backend-config equality with pointer-equal operands/computations*, **not** a recursive structural deep-compare. Per-call, it is the genuine "is this duplicate's q/kv ref the canonical one" decision. Prologue early-exits (CERTAIN): `this==&other ⇒ true`; opcode mismatch ⇒ false; `ShapeUtil::Equal(shape)` when layout-sensitive; then the operand/computation FunctionRefs.

The four RET_CHECKs use `xla::status_macros::MakeErrorStream` with file `hilo/hlo_passes/neuron_preprocess_kernel_duplicate_remover.cc` (str @0x3285e8); a failure returns the StatusOr error early.

---

## The Shared Idiom — Why Both End in HloDCE

Neither pass calls `RemoveInstruction` / `DetachFromOperandsAndUsers` in its rewrite loop. Both rely on **`ReplaceAllUsesWith` → trailing `HloDCE::Run`**:

| | #106 AllGather | #76 Preprocess |
|---|---|---|
| canonical pick | **first** gather inserted for key | `FindFirstPreprocessKernel` |
| coarse key | (raw param, idx scalars, dtype, shape, replica_groups) | inner-kernel backend-config string |
| rewrite | `ReplaceAllUsesWith(keep)` | `CloneWithNewOperands(first, dup-ops)` → `AddInstruction` → `ReplaceAllUsesWith(clone)` |
| fine substitution | — | 2× `IdenticalInternal` → swap q/kv tile-ref slices |
| scope | intra-computation (map per comp) | whole module (single `first`) |
| erase | `HloDCE::Run` @0x913fb70, gated on `changed` | `HloDCE::Run` @0x913fb70, unconditional tail |

> **NOTE — `HloDCE` is the stock XLA pass.** Both passes build a **stack-local** `HloDCE` object (vptr `off_D26ED8`, `name()` → `"dce"`) and call its `Run` @0x913fb70 over the whole module. The dead-instruction removal is therefore not Neuron-specific; the Neuron contribution in both passes is purely the *keying* that produces the dead instructions. This mirrors the stock-vs-Neuron boundary seen in the [combiners](collective-combiners.md).

---

## Adversarial Self-Verification

The five strongest claims, re-challenged against the binary:

**1. "The AllGather dedup key is a 6-element tuple rooted on parameter pointer-identity, and `dim`/`constrain`/`global-id` are NOT key columns."**
*Challenge:* could the dim be folded in as a seventh scalar? *Re-check:* the `raw_hash_set` template symbol at the `prepare_insert` site names exactly `tuple<HloInstruction*, long, PrimitiveType, long, Shape, vector<vector<long>>>` — six elements, last two being `Shape` and `replica_groups`. The node-offset stores (`+0x158/+0x160/+0x168/+0x170`) account for all scalars; there is no store of `[ag+0x258]`. **CONFIRMED** — implicit-via-Shape, not a separate column.

**2. "FindRawParameter tunnels through gte/tuple/opt-barrier/convert/all-gather and stops at parameter, with the convert shape-guarded."**
*Challenge:* are those opcode bytes right, and is the convert really guarded? *Re-check:* read the disasm directly — `cmp al,4Ch` (0x1f8d6f1), `cmp al,3Ah` (0x1f8d6f9), `cmp al,78h` (0x1f8d701), `cmp al,48h` (0x1f8d709), `cmp al,25h` (0x1f8d711); the only `Shape::Equal::operator()` call (0x1f8e142) sits on the convert arm; `and eax,0FFFFFFFDh` (0x1f8d6c5) gives the `(op&~2)==4` all-gather-family test. **CONFIRMED — every opcode byte and the guard verified in-binary.**

**3. "DeletePermute is the OTHER half of B23 and is documented on 4.7, not here."**
*Challenge:* did I accidentally absorb it? *Re-check:* matcher @0x1f79470 (`cmp dl,7` / `cmp dl,1Dh` / `or eax,edx`) and expander @0x1f79490 (`mutable_operand(0)`) are byte-verified but **intentionally excluded** from this page's body per scope; only cross-referenced to [4.7](flip-collective-opexpander.md). **CONFIRMED — scope honored.**

**4. "Preprocess Run runs IdenticalInternal twice with layout_sensitive=true and pointer-identity comparators."**
*Challenge:* one call or two, and which flags? *Re-check:* two distinct call sites (0x1fc0287, 0x1fc02ef) into `IdenticalInternal` @0x967d3c0; both push `1,0,0,0` (so `arg_0=layout_sensitive=true`, rest false); the operand/computation FunctionRefs are `std::equal_to<…const*>` (pointer-equal). **CONFIRMED.** The exact operand-substitution branch order (clone-loop flow) remains **MED** — disasm-reconstructed, not Hex-Rays-verified.

**5. "Both passes are stateless 8-byte HloModulePass subclasses that erase via a trailing stock HloDCE."**
*Challenge:* could either erase inline? *Re-check:* both registrars do `operator new(8)` + a single vptr store (`off_4115C0` / `off_411FE0`); neither `Run`'s callee set contains `RemoveInstruction`/`DetachFromOperandsAndUsers`; both terminate in `HloDCE::Run` @0x913fb70. **CONFIRMED.** #106's HloDCE is `changed`-gated (0x1f8eea2); #76's is an unconditional tail.

> **GAPS (open).** (a) The precise HLO sub-graph at each `operand(0)` hop in `IsFlashAttentionKernel`, and the `>7` operand-count semantics, are INFERRED (MED) — a real flash-attn preprocess dump would pin them. (b) The `array_state` dims byte-offset layout used by `FindFirstPreprocessKernel`'s doubling check is reconstructed from the repeated access idiom (rank HIGH, exact element offsets MED). (c) The dedup-map open-addressing probe (SSE group scan ~0x1f8ec14) is summarised as "absl raw_hash_set find/insert", not stepped. (d) The tuple-key scalars [1]/[3] (`node+0x158`/`+0x168`) both default to -1 and are populated from gte/tuple `tuple_index()`; their outer-vs-inner roles on nested tuples are MED but, being opaque longs in the equality, do not affect the dedup-correctness statement.
