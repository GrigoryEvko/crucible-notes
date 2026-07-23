# Looped-Einsum → Collective-Matmul Fusion

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). Both passes live in the `neuronxcc/starfish/bin/hlo-opt` ELF. This binary is **not** mapped VA==fileoff: for `.rodata`, file offset = VA − 0x200000 (section header: VMA `0x20c940`, fileoff `0xc940`); `.text` VMA base is `0x1e6e960`. Every rodata string VA on this page was reconstructed as `fileoff + 0x200000` and re-verified. Other wheels differ — treat every address as version-pinned.*

## Abstract

Two `hlo-opt` passes implement Neuron's **tensor-parallel collective-matmul** rewrite for the "looped einsum" idiom. In a sharded transformer, a weight is `all-gather`ed across the TP group and then contracted (`dot`) against an activation — frequently inside a `while`-loop body, where the gathered weight arrives through a loop-parameter `get-tuple-element`. **`NeuronLoopedEinsumReplacer`** recognizes `dot(all_gather(weight), activation)` in five structural shapes (plain, reshape-wrapped, and an N×D reshape→gte→reshape chain on either the LHS or RHS dot operand) and folds the gather **into** a single `AwsNeuronCollectiveMatmul` custom-call that performs the all-gather and the matmul together. **`NeuronLoopedEinsumTokenReplacer`** is the companion cleanup: once the gather is absorbed, the rank-0 ordering token threaded through the all-gather survives as a `get_tuple_element(all_gather(…))` of rank 0, and this pass reroutes it straight to the gather's pre-gather operand.

Both passes are `xla::OpExpanderPass` subclasses, so each is driven by the shared `xla::OpExpanderPass::Run` @ `0x29f0bb0` (vtable slot vptr+0x18): `Run` walks every computation in post-order, calls the per-pass `InstructionMatchesPattern` (vtable slot vptr+0x40), and on a match calls `ExpandInstruction` (vtable slot vptr+0x48), splices the returned instruction in via `ReplaceAllUsesWith`, and DCEs the dead dot/all-gather chain. The pass objects are 0x30-byte and trivially constructed — the factory closures `…RegisterNeuronLoopedEinsumReplacer…::_M_invoke` @ `0x1e71810` / `…TokenReplacer…::_M_invoke` @ `0x1e71790` each `operator new(0x30)`, zero the body, and store the vptr. **There are no `cl::opt` thresholds and no constructor arguments**: every tuning knob is one of three `NEURON_COLLECTIVE_*` environment variables, read at match/expand time via `xla::hlo_utils::EnvVarSetToOne` / `ReadEnvVarAsInt`.

The binary was archived with the IDA decompiler disabled for this translation unit, so the reconstruction rests on raw `objdump -d -M intel` over the live ELF, the mangled symbol table (`nm`), the vtable byte dumps, and byte-exact rodata reads. The pass names, the custom-call target, all three env-var names, all four `backend_config` keys, every cited symbol address and every vtable slot are read from those sources; the two genuinely open items — the SB-to-SB default-threshold constant and the exact N×D result-shape dim ordering — are flagged where they appear.

For reimplementation, the contract is:

- The recognition heuristic: a `dot` with exactly one contracting dim per side, one of whose operands reaches an `all_gather` of a **weight parameter** through a convert / reshape / get-tuple-element chain (the gte is how a loop-carried weight surfaces), validated by all-gather-dimension and operand rank.
- The five structural matchers and the LHS-vs-RHS weight-side disambiguation that decides which dot operand is the gathered weight.
- The fused custom-call: target `AwsNeuronCollectiveMatmul`, two operands `{reshape(weight), activation}`, `api_version=1`, and a four-field `backend_config` string assembled from the collective geometry and a state-buffer size threshold.
- The token cleanup and its shared `NEURON_COLLECTIVE_MATMUL_NXD` gate with the N×D path.

| | |
|---|---|
| **Pass #63 — NeuronLoopedEinsumReplacer** | `name()` @ `0x1faadb0` = `neuron_looped_einsum_replacer` |
| **— InstructionMatchesPattern** | `0x1fab000` (407 B) |
| **— ExpandInstruction** | `0x1fab280` (4138 B); `.cold` @ `0x1fab198` (unwind only) |
| **Pass #64 — NeuronLoopedEinsumTokenReplacer** | `name()` @ `0x1faadc0` = `neuron_looped_einsum_token_replacer` |
| **— InstructionMatchesPattern** | `0x1faaef0` (93 B) |
| **— ExpandInstruction** | `0x1faadd0` (58 B) |
| **Shared driver** | `xla::OpExpanderPass::Run` @ `0x29f0bb0` |
| **Factory closures** | `_M_invoke` @ `0x1e71810` (#63) / `0x1e71790` (#64), `operator new(0x30)` |
| **vtable / vptr** | `_ZTV…Replacer` @ `0x411d68` (vptr `0x411d78`) · `_ZTV…TokenReplacer` @ `0x411dc8` (vptr `0x411dd8`) |
| **Emitted target** | `AwsNeuronCollectiveMatmul` (VA `0x26e8c4`, len 25) |
| **Opcode field** | `byte ptr [HloInstruction + 0x14]` |

---

## Pass #63 — NeuronLoopedEinsumReplacer

### Purpose

This pass is the fusion: it takes a `dot` whose gathered-weight operand is an `all_gather` of a tensor-parallel weight parameter and replaces the whole `dot`/`all_gather` subtree with one `AwsNeuronCollectiveMatmul` custom-call. The motivation is the standard collective-matmul optimization: rather than materialize the full gathered weight and then matmul, the kernel overlaps the cross-device gather with the contraction, so each device computes its partial result as the gathered shards stream in. The all-gather is **absorbed** into the custom-call; only the (reshaped) gathered weight and the activation survive as operands. The kernel this targets is the NKI collective-matmul kernel (Part 6).

### Entry Point

```text
xla::OpExpanderPass::Run                       @ 0x29f0bb0   ── shared driver (vptr+0x18)
  └─ HloComputation::MakeInstructionPostOrder              ── worklist over each computation
  └─ NeuronLoopedEinsumReplacer::InstructionMatchesPattern @ 0x1fab000  (vptr+0x40)
       ├─ MatchAllGatherInDotLhsRS   @ 0x1fae2e0
       ├─ MatchAllGatherInDotLhs     @ 0x1fae850
       ├─ MatchAllGatherInDotRhsRS   @ 0x1fae4e0
       ├─ MatchAllGatherInDotRhs     @ 0x1fae9e0
       ├─ MatchAllGatherInDotLhsNxD  @ 0x1fae6e0
       ├─ WeightInLHS / WeightInRHS  @ 0x1fb6950 / 0x1fb3070
       ├─ GetRootAllGather           @ 0x1fad8f0   (depth=4)
       └─ xla::hlo_utils::EnvVarSetToOne @ 0x1ebe700
  └─ NeuronLoopedEinsumReplacer::ExpandInstruction         @ 0x1fab280  (vptr+0x48)
  └─ HloInstruction::ReplaceAllUsesWith + DCE              ── splice CC, kill dot/AG
```

> **NOTE —** the five matcher helpers and `GetRootAllGather` / `WeightInLHS` / `WeightInRHS` carry full mangled symbols in this binary (`_ZN3xla4hilo24MatchAllGatherInDotLhsRSE…` etc.), so they are real `xla::hilo`-namespace functions, not inlined fragments. Their addresses are reproduced verbatim from the `call` targets in the IMP disassembly.

### Algorithm — `InstructionMatchesPattern` @ `0x1fab000`

```c
// NeuronLoopedEinsumReplacer::InstructionMatchesPattern(HloInstruction* inst)
bool InstructionMatchesPattern(HloInstruction* inst):
    if inst->opcode() != kDot /*0x2E*/:           return false   // 0x1fab011 cmp [rsi+0x14],0x2e
    DotDimensionNumbers& d = inst->dot_dimension_numbers()        // 0x1fab036
    // single-contraction-axis einsum only: each contracting-dim RepeatedField size must == 1
    if d.rhs_contracting_dimensions_size() != 1:  return false    // 0x1fab03b cmp [rax+0x28],1
    if d.lhs_contracting_dimensions_size() != 1:  return false    // 0x1fab043 cmp [rax+0x10],1

    // try the five structural shapes of "an all-gather reaches a dot operand"
    lhsRS  = MatchAllGatherInDotLhsRS(inst)     // 0x1fab055  dot -> reshape -> AG (LHS)
    lhs    = MatchAllGatherInDotLhs(inst)       // 0x1fab060  dot -> AG          (LHS)
    rhsRS  = MatchAllGatherInDotRhsRS(inst)     // 0x1fab06a  dot -> reshape -> AG (RHS)
    rhs    = MatchAllGatherInDotRhs(inst)       // 0x1fab075  dot -> AG          (RHS)
    lhsNxD = MatchAllGatherInDotLhsNxD(inst)    // 0x1fab080  dot -> reshape -> gte -> reshape -> AG (LHS)

    if (lhsRS | lhs):                           // LHS-gathered branch (0x1fab08a)
        if !WeightInLHS(inst): return false     // weight must be the LHS dot operand
        weightOperand = inst->operand(0)
    else if (rhsRS | rhs | lhsNxD):             // RHS / N×D branch (0x1fab140)
        if rhs:        ok = WeightInRHS(inst)   // 0x1fab146
        else if rhsRS || lhsNxD: ok = true
        else:          ok = WeightInLHS(inst)   // 0x1fab185  (N×D weight on LHS side)
                       ?: EnvVarSetToOne("NEURON_COLLECTIVE_PERMUTE_AGGRESSIVE") // 0x1fab16b
        if !ok: return false
        weightOperand = rhs ? inst->operand(1) : inst->operand(0)
    else:
        return false

    // common all-gather-root validation
    ag = GetRootAllGather(weightOperand, /*depth=*/4)             // 0x1fab0d0
    ag = static_cast<HloAllGatherInstruction*>(ag)
    if EnvVarSetToOne("NEURON_COLLECTIVE_MATMUL_NXD")             // 0x1fab0e5
       && ag->operand_count() /*[ag+0x18]*/ > 5:                  // 0x1fab0ee cmp [r12+0x18],5
        return false                                             //   >5-operand AG blocked in N×D
    if ag->all_gather_dimension() /*[ag+0x258]*/ > 1:            // 0x1fab0fa cmp [r12+0x258],1
        return true                                              //   gather dim in {2,3,…} already OK
    // gather dim <= 1: require the gathered operand to be rank-3
    return ag->operand(0)->shape().rank() == 3                   // 0x1fab11e array_state(); (>>1)==3
```

The recognized idiom is a `dot` with one contracting dim on each side, one operand of which is an (optionally reshaped) `all_gather` of a **weight parameter**. The weight reaches the gather through a convert / reshape / get-tuple-element chain. The get-tuple-element is exactly how a loop-carried (while-body) parameter surfaces, so although the surface op is a single `dot`, its operand provenance is the loop-threaded all-gathered weight — the "einsum expressed as a loop of gathered matmuls". The opcode field is `byte ptr [HloInstruction + 0x14]` throughout, consistent with every other `hlo-opt` pass.

### The five structural matchers

Each matcher is an `xla::match::`-style pattern walk over the `[inst+0x14]` opcode bytes, anchored at the `dot` and descending operand chains; the tail predicate confirms the leaf is a weight parameter reached through a convert/gte chain.

| Helper | Pattern (root → … → leaf) | Tail predicate |
|---|---|---|
| `MatchAllGatherInDotLhs` @ `0x1fae850` | `dot` op0 ⇒ `all_gather` chain (LHS) | op0→op0 is `IsConvertGetTupleElementParameter`, OR `convert → … → parameter` |
| `MatchAllGatherInDotLhsRS` @ `0x1fae2e0` | `dot → reshape → all_gather` (LHS) | same convert / gte → parameter tail |
| `MatchAllGatherInDotLhsNxD` @ `0x1fae6e0` | `dot → reshape → gte → reshape → all_gather` (LHS) | tail-call (returns raw match result) |
| `MatchAllGatherInDotRhs` @ `0x1fae9e0` | as `…Lhs` but **operand index 1** | same convert / gte → parameter tail |
| `MatchAllGatherInDotRhsRS` @ `0x1fae4e0` | `dot → reshape → all_gather` (RHS) | convert → parameter tail |

Two predicate helpers feed these:

- `GetRootAllGather(inst, depth)` @ `0x1fad8f0`: follow `mutable_operand(0)` up to `depth` (=4) times, return the first node whose `opcode() == kAllGather(4)`, else `nullptr`. It skips ≤4 layout/convert/gte hops to reach the actual all-gather root.
- `IsConvertGetTupleElementParameter(inst)` @ `0x1fad890`: `inst == convert(0x25)` ∧ `inst->operand(0) == gte(0x3A)` ∧ `gte->operand(0)->operand(0) == parameter(0x4C)` — the convert wraps a get-tuple-element off (a tuple of) the entry/loop parameter. This is the key loop-carried-weight signature.

`WeightInLHS` @ `0x1fb6950` and `WeightInRHS` @ `0x1fb3070` are large `AnyOfPattern` builders over `{parameter 0x4C, gte 0x3A, convert 0x25, reshape 0x5B}`. They assert the dot's LHS (resp. RHS) subtree resolves to a weight parameter through any layout/convert/gte chain, and capture the matched node into an out-pointer (`[rdx] = inst` @ `0x1fb7694`). The captured node confirms which dot operand is the gathered weight versus the activation — the disambiguation that decides which operand the fold absorbs.

> **GOTCHA —** the contracting-dim *size* checks at `0x1fab03b`/`0x1fab043` read the RepeatedField count word, not a dimension index. `[d+0x28]` is `rhs_contracting_dimensions().size()` and `[d+0x10]` is `lhs_contracting_dimensions().size()`; the dimension **values** live one field over at `[d+0x30]`/`[d+0x18]` and are read in `ExpandInstruction`. A reimplementation that confuses size-with-value here will reject every valid single-contraction einsum.

### Algorithm — `ExpandInstruction` @ `0x1fab280`

```c
// NeuronLoopedEinsumReplacer::ExpandInstruction(HloInstruction* dot)
unique_ptr<HloInstruction> ExpandInstruction(HloInstruction* dot):
    // 1. re-run the five matchers to recover the case (0x1fab2b0..0x1fab2ed)
    lhsRS, lhs, rhsRS, rhs, lhsNxD = <the five matchers>(dot)

    // 2. recover the all-gather root and the "other" (activation) operand
    if (lhs | lhsRS | lhsNxD | rhsRS):
        ag    = GetRootAllGather(dot->mutable_operand(0), 4)   // 0x1fab317
        other = dot->mutable_operand(1)                        // 0x1fab32b
    else /* RHS-only */:
        ag    = GetRootAllGather(dot->mutable_operand(1), 4)   // 0x1fabffd
        other = dot->mutable_operand(0)                        // 0x1fac00e

    // 3. read the collective geometry from the all-gather's replica groups
    tp_degree  = ag->replica_groups()[0].replica_ids_size()    // [grp+0x10]  0x1fab34c
    num_groups = ag->replica_groups().size()                   // span_bytes / 0x18  0x1fab355
    DotDimensionNumbers& d = dot->dot_dimension_numbers()      // 0x1fab384
    lhs_contract = d.lhs_contracting_dimensions()[0]           // [d+0x18]  0x1fab38e
    rhs_contract = d.rhs_contracting_dimensions()[0]           // [d+0x30]  0x1fab392

    // 4. build the fused result shape: dot result element-type with the merged
    //    contracting axes; dims gathered from the operand shapes (loops 0x1fab450/0x1fab498),
    //    then ShapeUtil::MakeValidatedShape(primitiveType, dims)            // 0x1fab500
    // 5. reshape the gathered weight into collective-matmul layout
    reshaped = HloInstruction::CreateReshape(newShape, gatheredWeight, -1)   // 0x1fab683
    comp->AddInstruction(reshaped, /*name=*/"")                              // 0x1fab695

    // 6. SB-to-SB threshold flag
    tensor_bytes = 2 * ByteSizeOfPrimitiveType(elt) * Product(weight_dims)   // 0x1fab790..818
    double mb = tensor_bytes * (1.0/1048576.0)                              // 0x1fab82f mulsd
    int thr; bool have = ReadEnvVarAsInt(
        "NEURON_COLLECTIVE_MATMUL_SB_TO_SB_THRESHOLD_IN_MB", &thr)           // 0x1fab820
    double limit = have ? (double)thr : DEFAULT_THRESHOLD                    // 0x1fab846/0x1fab856
    char use_sb_to_sb = '0' + (mb <= limit)                                 // comisd+setbe+add 0x1fab873..8a6

    // 7. assemble backend_config = absl::CatPieces({...})                    // 0x1fabdcb
    backend_config =
        "rhs_contracting_dim=" + to_string(rhs_contract)   // key @ VA 0x24f59b
      + ",tp_degree="          + to_string(tp_degree)       // key @ VA 0x263087
      + ",num_groups="         + to_string(num_groups)      // key @ VA 0x26e8b7
      + ",use_sb_to_sb="       + string(1, use_sb_to_sb)    // key @ VA 0x2348d9  ("0"/"1")

    // 8. emit the fused custom-call (operands: reshaped weight + activation)
    cc = HloInstruction::CreateCustomCall(
            dotResultShape,
            /*operands=*/{reshaped, other},                 // count = 2
            /*target=*/"AwsNeuronCollectiveMatmul",         // VA 0x26e8c4, len 25
            /*opaque=*/backend_config,
            /*api_version=*/CustomCallApiVersion(1))         // push 1  0x1fabdf8
    comp->AddInstruction(cc, /*name=*/"")                    // 0x1fabe10
    return cc   // Run then ReplaceAllUsesWith(dot, cc) and DCEs the dot / all-gather chain
```

**Net effect:** `dot( all_gather(W)[, reshape…], X )` → `AwsNeuronCollectiveMatmul( reshape(W), X )` with `backend_config = "rhs_contracting_dim=<r>,tp_degree=<g>,num_groups=<n>,use_sb_to_sb=<0|1>"`, `api_version=1`. The all-gather is absorbed into the custom-call (the kernel does gather+matmul together); `use_sb_to_sb` selects the state-buffer-to-state-buffer kernel variant when the weight tile fits under the MB threshold. The `.cold` @ `0x1fab198` is exception-unwind cleanup only (string/vector/Shape destructors) — no rewrite logic.

### `backend_config` schema

The opaque string is a flat comma-separated `key=value` list, assembled by `absl::CatPieces` over an initializer-list of `string_view`s; the four literal key prefixes are interleaved with decimal renderings of integers. Field-by-field:

| Key (verbatim, VA) | Source field | Meaning | Confidence |
|---|---|---|---|
| `rhs_contracting_dim=` (`0x24f59b`, len 20) | `d.rhs_contracting_dimensions()[0]` (`[dot_dim+0x30]`) | contracting axis on the RHS operand of the fused matmul | CERTAIN (string) / HIGH (field) |
| `,tp_degree=` (`0x263087`, len 11) | `replica_groups()[0].replica_ids_size()` (`[replica_group+0x10]`) | tensor-parallel degree = members per replica group | CERTAIN / HIGH |
| `,num_groups=` (`0x26e8b7`, len 12) | `replica_groups().size()` (span bytes / 0x18) | number of replica groups in the collective | CERTAIN / HIGH |
| `,use_sb_to_sb=` (`0x2348d9`, len 14) | `'0' + (weight_MB <= threshold)` | select the SB→SB collective-matmul kernel under the MB threshold | CERTAIN / HIGH |

> **QUIRK —** the four reduce/geometry values are folded into a **string** opaque, not into structured `HloInstruction` attributes. Downstream legalization (Part 8) re-parses this comma list, so the exact key spellings (including the leading comma on the last three, which is part of the literal at each VA) are part of the wire contract. A reimplementation that drops a leading comma or reorders keys produces a `backend_config` the lowering will fail to parse.

> **NOTE —** only `rhs_contracting_dim` reaches the config, not `lhs_contracting_dim`, even though both are read at `0x1fab38e`/`0x1fab392`. The LHS contracting axis is consumed only by the result-shape construction (step 4); the kernel infers the LHS contraction from the fused layout and needs only the RHS axis explicitly. The `lhs_contract` read is therefore not dead — it feeds `MakeValidatedShape`, not the config.

### Considerations

- **N×D gate.** `NEURON_COLLECTIVE_MATMUL_NXD` must be set to `1` (via `EnvVarSetToOne`) for the N×D path's all-gather-operand-count guard to apply (`> 5 ⇒ reject`). The N×D matcher (`MatchAllGatherInDotLhsNxD`) itself is always tried; the env var only adds the `>5`-operand block at `0x1fab0ee`.
- **Aggressive fallback.** When neither `WeightInLHS` nor `WeightInRHS` holds for the N×D/RHS branch, the match falls back to `NEURON_COLLECTIVE_PERMUTE_AGGRESSIVE` (`EnvVarSetToOne` @ `0x1fab16b`) — an opt-in to fold even when the weight side could not be proven a parameter.
- **All-gather-dimension rule.** `[ag+0x258]` is `all_gather_dimension()` — the same offset the other AllGather passes use, and not the `+0x260` an eyeball reading of the struct suggests. The rule is: `dim > 1 ⇒ accept`; `dim ≤ 1 ⇒` require the gathered operand to be rank-3. This is `array_state() >> 1 == 3` at `0x1fab11e` (the `array_state` low bit is a flag; the rank is the high bits).

---

## Pass #64 — NeuronLoopedEinsumTokenReplacer

### Purpose

When the looped form threads a scalar/token tuple element through the all-gather (so the gather is ordered with respect to the loop), folding the gather into the collective-matmul (#63) leaves behind a rank-0 `get_tuple_element(all_gather(…), i)` — the ordering-token artifact. This pass removes it by rerouting the GTE's users directly to the all-gather's pre-gather operand. It is gated by the **same** `NEURON_COLLECTIVE_MATMUL_NXD` env var as #63's N×D path and is registered immediately after #63 (order 63 → 64), so it is the cleanup companion to the N×D rewrite.

### Algorithm — `InstructionMatchesPattern` @ `0x1faaef0`

```c
// NeuronLoopedEinsumTokenReplacer::InstructionMatchesPattern(HloInstruction* inst)
bool InstructionMatchesPattern(HloInstruction* inst):
    if !EnvVarSetToOne("NEURON_COLLECTIVE_MATMUL_NXD"):  return false  // 0x1faaf00 hard env gate
    if inst->opcode() != kGetTupleElement /*0x3A*/:      return false  // 0x1faaf09 cmp [r13+0x14],0x3a
    if inst->operand(0)->opcode() != kAllGather /*0x04*/: return false // 0x1faaf2d cmp [rax+0x14],4
    if inst->shape().rank() != 0:                        return false  // 0x1faaf3e array_state(); rank!=0
    return true   // a rank-0 (token-like) get-tuple-element off an all-gather
```

### Algorithm — `ExpandInstruction` @ `0x1faadd0`

```c
// NeuronLoopedEinsumTokenReplacer::ExpandInstruction(HloInstruction* gte)
unique_ptr<HloInstruction> ExpandInstruction(HloInstruction* gte):
    HloInstruction* ag = gte->mutable_operand(0)    // the all-gather       0x1faade2 (operand 0)
    return ag->mutable_operand(1)                   // its second operand   0x1faadef (operand 1)
    // StatusOr success: [out]=0 (ok status), [out+8]=ag->operand(1)         0x1faadf4/0x1faadfc
```

**Net effect:** `get_tuple_element(all_gather(value, token), k)[rank-0]` → `all_gather->operand(1)`. The all-gather's second operand (the original pre-gather token/value) is threaded directly to the GTE's users, removing the rank-0 GTE-off-all-gather ordering artifact left behind once #63 has folded the gather into the collective-matmul. It returns an **existing** node (`operand(1)`), not a freshly created instruction — this is pure rerouting, which is why the body is only 58 bytes.

> **QUIRK —** unlike a normal `OpExpanderPass`, `ExpandInstruction` here returns a pre-existing instruction rather than a new one. The shared `Run` then `ReplaceAllUsesWith(gte, ag->operand(1))` and DCEs the now-dead GTE. A reimplementer who assumes `ExpandInstruction` always constructs and `AddInstruction`s a node will double-add or leak; #64 deliberately does neither.

---

## Environment-Variable Gates

These three are the only tuning knobs for both passes; no `cl::opt` or `HloPassOptions` field feeds them (the factory takes no args, and the per-TU static-init `_GLOBAL__sub_I…` @ `0x1faaf50` initializes only the logging-level map). They are read through `xla::hlo_utils::EnvVarSetToOne` @ `0x1ebe700` (boolean "== 1") and a `ReadEnvVarAsInt` helper (integer).

| Env var (VA) | Type | Read at | Effect | Confidence |
|---|---|---|---|---|
| `NEURON_COLLECTIVE_MATMUL_NXD` (`0x228c29`) | bool (`==1`) | #63 IMP `0x1fab0e5`; #64 IMP `0x1faaf00` | enables the N×D collective-matmul folding (#63) and the token cleanup (#64); in #63 also blocks all-gathers with operand_count > 5 | CERTAIN |
| `NEURON_COLLECTIVE_PERMUTE_AGGRESSIVE` (`0x31cb28`) | bool (`==1`) | #63 IMP `0x1fab16b` | aggressive fallback match when neither weight-in-LHS nor weight-in-RHS proves the weight side for the N×D/RHS branch | CERTAIN |
| `NEURON_COLLECTIVE_MATMUL_SB_TO_SB_THRESHOLD_IN_MB` (`0x2abfd0`) | int (MB) | #63 Expand `0x1fab820` (`ReadEnvVarAsInt`) | weight tile-size threshold (MB) deciding `use_sb_to_sb=0/1` in the `backend_config`; when unset, an internal default constant is used | CERTAIN (name) / HIGH (default) |

See [3.11 — Environment-Variable Catalog](../frontend/env-var-catalog.md) for the full `NEURON_*` knob taxonomy and the `EnvVarSetToOne` / `ReadEnvVarAsInt` infrastructure.

### Verbatim string evidence

```text
VA 0x25f1f5  neuron_looped_einsum_replacer            (name() #63, len 29)
VA 0x3e6a38  neuron_looped_einsum_token_replacer      (name() #64, len 35)
VA 0x26e8c4  AwsNeuronCollectiveMatmul                (emitted custom-call target, len 25)
VA 0x24f59b  rhs_contracting_dim=                     (backend_config, len 20)
VA 0x263087  ,tp_degree=                              (backend_config, len 11)
VA 0x26e8b7  ,num_groups=                             (backend_config, len 12)
VA 0x2348d9  ,use_sb_to_sb=                           (backend_config, len 14)
VA 0x228c29  NEURON_COLLECTIVE_MATMUL_NXD
VA 0x31cb28  NEURON_COLLECTIVE_PERMUTE_AGGRESSIVE
VA 0x2abfd0  NEURON_COLLECTIVE_MATMUL_SB_TO_SB_THRESHOLD_IN_MB
```

---

## Reconstructed Signatures

```cpp
namespace xla::hilo {
  // both : public xla::OpExpanderPass  (object size 0x30, trivially constructed)
  class NeuronLoopedEinsumReplacer : public OpExpanderPass {
    absl::string_view name() const override;                  // 0x1faadb0 → "neuron_looped_einsum_replacer"
    bool InstructionMatchesPattern(HloInstruction*) override;  // 0x1fab000
    std::unique_ptr<HloInstruction> ExpandInstruction(HloInstruction*) override; // 0x1fab280
  };
  class NeuronLoopedEinsumTokenReplacer : public OpExpanderPass {
    absl::string_view name() const override;                  // 0x1faadc0 → "neuron_looped_einsum_token_replacer"
    bool InstructionMatchesPattern(HloInstruction*) override;  // 0x1faaef0
    std::unique_ptr<HloInstruction> ExpandInstruction(HloInstruction*) override; // 0x1faadd0
  };
  // free helpers (anon/xla::hilo namespace, real symbols in this binary):
  bool MatchAllGatherInDotLhs   (HloInstruction*);   // 0x1fae850
  bool MatchAllGatherInDotLhsRS (HloInstruction*);   // 0x1fae2e0
  bool MatchAllGatherInDotLhsNxD(HloInstruction*);   // 0x1fae6e0
  bool MatchAllGatherInDotRhs   (HloInstruction*);   // 0x1fae9e0
  bool MatchAllGatherInDotRhsRS (HloInstruction*);   // 0x1fae4e0
  HloInstruction* GetRootAllGather(HloInstruction*, int depth);  // 0x1fad8f0
  bool IsConvertGetTupleElementParameter(const HloInstruction*); // 0x1fad890
  bool WeightInLHS(HloInstruction*);                 // 0x1fb6950  (captures into out-ptr)
  bool WeightInRHS(HloInstruction*);                 // 0x1fb3070
}
```

Structure offsets used (verified at the cited call sites): `DotDimensionNumbers` — `[+0x10]`/`[+0x18]` = lhs_contracting size/data, `[+0x28]`/`[+0x30]` = rhs_contracting size/data (RepeatedField<int64>, 0x18-stride layout). `HloAllGatherInstruction` — `[+0x18]` = operand_count (the ≤5 gate), `[+0x258]` = all_gather_dimension. `ReplicaGroup` span stride = 0x18; replica_ids count at `[group+0x10]`. `HloInstruction` opcode = `byte [+0x14]`. `Shape::array_state()` packs rank in the high bits (`(state >> 1)` is the rank used by both passes' rank checks).

### The vtable layout

The byte dump of `_ZTV…Replacer` @ `0x411d68` gives the slot assignment. With vptr = `0x411d78`: `name` sits at vptr+0x10, `OpExpanderPass::Run` at vptr+0x18 (`0x29f0bb0`), `InstructionMatchesPattern` at **vptr+0x40** (`0x1fab000`), and `ExpandInstruction` at **vptr+0x48** (`0x1fab280`). The `…TokenReplacer` vtable @ `0x411dc8` (vptr `0x411dd8`) follows the identical layout, with its IMP at `0x1faaef0` and Expand at `0x1faadd0` in the same slots.

> **GOTCHA — four `HloPassInterface` slots sit between `Run` and the two overrides.** `InstructionMatchesPattern` and `ExpandInstruction` do *not* immediately follow `Run` in the vtable; the intervening slots hold `0x1e7dc10`, `0x1e7c7b0`, `0x1e7c2d0` and `0x1e7c340`. Counting forward from `Run` without accounting for them lands on vptr+0x28 / vptr+0x30 and dispatches into the wrong functions entirely.

---

## Evidence summary

- **The three headline strings.** `rg -a -o` on the live ELF returns `neuron_looped_einsum_replacer`, `neuron_looped_einsum_token_replacer` and `AwsNeuronCollectiveMatmul`; `grep -bo` file offsets `389621` / `1993272` / `452804` map, via `+0x200000`, to VA `0x25f1f5` / `0x3e6a38` / `0x26e8c4`.
- **The core function addresses.** `nm` resolves `…Replacer::ExpandInstruction` @ `0x1fab280`, `…::InstructionMatchesPattern` @ `0x1fab000`, `.cold` @ `0x1fab198`, `…TokenReplacer::ExpandInstruction` @ `0x1faadd0`, `…::InstructionMatchesPattern` @ `0x1faaef0`, both `name()` @ `0x1faadb0` / `0x1faadc0`, and both factory `_M_invoke` @ `0x1e71810` / `0x1e71790` — all from the demangled symbol table.
- **The match sequence.** `objdump -d` of `0x1fab000` shows `cmp [rsi+0x14],0x2e`, `cmp [rax+0x28],1` and `cmp [rax+0x10],1`, then calls into `MatchAllGatherInDotLhsRS`/`Lhs`/`RhsRS`/`Rhs`/`LhsNxD` — the helper addresses on this page are read from those call targets — followed by `GetRootAllGather`, `EnvVarSetToOne`, `cmp [r12+0x18],5` and `cmp [r12+0x258],1`.
- **The four `backend_config` keys.** `grep -bo` locates `rhs_contracting_dim=` at fileoff `325019` (VA `0x24f59b`), `,tp_degree=` at `405639` (VA `0x263087`), `,num_groups=` at `452791` (VA `0x26e8b7`), and `,use_sb_to_sb=` at `215257` (VA `0x2348d9`).
- **The token pass's shape.** `objdump -d` of `0x1faaef0` shows `call EnvVarSetToOne` *before* any opcode test, then `cmp [r13+0x14],0x3a`, `cmp [rax+0x14],4` and the `shape()`/`array_state` rank read; `0x1faadd0` shows `mutable_operand(0)` then `mutable_operand(1)`, with `mov [r12],0` and `mov [r12+8],rax`.

## Limits of this reading

The IDA decompiler was disabled for this translation unit, so everything above comes from raw `objdump -d -M intel`, the mangled symbol table, vtable byte dumps and byte-exact rodata reads — there is no pseudocode. Three things are consequently short of exact:

- The **SB-to-SB default threshold** constant used when the env var is unset, and the bytes→MB factor: both live in `.data.rel.ro` and are [INFERRED] from the `…_IN_MB` naming plus the `cvtsi2sd` / `mulsd` / `comisd` sequence. Their role is clear; the numeric value was not byte-dumped.
- The per-case **N×D result-shape dim ordering** built by `MakeValidatedShape` (loops `0x1fab450` / `0x1fab498`) was not exhaustively enumerated. It merges the gathered operand's dims, but pinning the order would need a worked HLO example.
- The **wiring** of the four config values — RHS contracting axis, replica geometry, SB threshold — is traced from the `[d+0x30]`, `[grp+0x10]`, span-stride and `comisd` reads inside `ExpandInstruction`, but not single-stepped through `CatPieces`.

Registration order is also not run order: the two passes are registered consecutively and the token pass's env gate ties it to the N×D path, but the executed pipeline subset is driver-supplied and was not traced here.

---

## Related Components

| Name | Relationship |
|---|---|
| `OpExpanderPass::Run` (`0x29f0bb0`) | shared driver for all `OpExpander` passes including these two |
| `ConvertCollectivesToCustomCall` ([4.3](collectives-to-customcall.md)) | the forward `all_gather → AwsNeuronAllGather` lowering; #63 instead **absorbs** the gather into a matmul |
| `NeuronRewriteAllGatherTripCount` (`_ZTV…` @ `0x4124e8`) | sibling `xla::hilo` all-gather pass in the same TU |
| AwsNeuronCollectiveMatmul kernel (Part 6 — NKI) | the NKI kernel the fused custom-call dispatches to |

## Cross-References

- [3.11 — Environment-Variable Catalog](../frontend/env-var-catalog.md) — the `NEURON_COLLECTIVE_*` knob family and `EnvVarSetToOne` / `ReadEnvVarAsInt`
- [Collectives → Custom-Call Forward Conversion](collectives-to-customcall.md) — the `AwsNeuron*` custom-call naming contract and opcode gate
- [CollectivePermute → AllGather Lowering](collectivepermute-to-allgather.md) — sibling `all-gather`-producing rewrite, also gated on `NEURON_COLLECTIVE_*` env vars
- Part 6 — NKI Kernel DSL — the gather+matmul kernel the `AwsNeuronCollectiveMatmul` custom-call dispatches to; Part 13 — Distribution & Collectives covers replica groups and TP degree (forward references, pages land later)
