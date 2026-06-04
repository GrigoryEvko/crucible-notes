# ICI All-Reduce Primitive

> *All addresses, symbols, and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, full-symbol, `.text` VMA == file offset). Other versions will differ; treat every VA as version-pinned.*

## Abstract

`xla::jellyfish::AllReduceEmitter::EmitAllReduce` (`0x13742200`, 7,962 bytes, 1,445 decompiled lines) is the **hardware-level emission entry** for an on-chip-to-on-chip AllReduce on the ICI fabric. It is *not* the algorithm and it is *not* the picker. It is the function that — once the SPMD partitioner has decided an AllReduce is needed and the [SelectNDStrategy picker](../collectives/strategy-nd-picker.md) has chosen a ring shape — constructs exactly one concrete *sub-emitter* (a `RingSumEmitter`, a `UniDirection*RingStrategy`, or one of the `RotatedPincer*`/`AsyncPincer*` family) and drives it to emit the actual per-step LLO program: the colored-ring **reduce-scatter then all-gather** loop where every step is one remote-write DMA, one remote sync-flag bump, one local sync-flag wait, and one VPU reduce. This page owns that dispatch and that per-step emission shape.

The signature is the contract:

```c
EmitAllReduce(absl::Span<const ReplicaGroup>,
              const std::function<LloValue*(LloValue*, LloValue*, LloRegionBuilder)>& fmerge)
```

The second argument — the **merge functor** — is the only place the reduction kind enters the emitter. `EmitAllReduce` never inspects it: it threads the closure into whichever sub-emitter it builds, and the sub-emitter calls it once per step on `(local_copy, just_received_chunk)`. There is no reduction-op field on the wire (see [DMA Descriptor](dma-descriptor.md)); the reduce is always a local VPU op.

A reader who knows MPI ring/recursive-doubling AllReduce owns the frame: this is the function that turns "reduce with these replica groups, using *this* per-element merge" into a concrete LLO instruction stream, by selecting a sub-emitter on topology + size + prefer-flags and then walking its two-phase step loop. The reimplementation contract:

- **The dispatch fork.** `EmitAllReduce` first splits on the inference / size gate (`ShouldUseInferenceShortLatencyEmitter` plus a shard-size threshold) into an **N-D arm** (multi-axis, `SelectNDStrategy`-driven, decompile line 794). When that gate does not fire, the function falls into a second branch that computes `MayUseSinglePhaseRingEmitter` (line 997) and then chooses between a **binomial single-phase ring** (`CreateEmitter`, line 1054) and the **1-D arm** (`GetRingLocationWrapper`-driven, line 1093). The binomial path and the 1-D arm are therefore siblings inside the non-N-D branch — binomial is *not* a fast path that cuts across the N-D arm. Each arm then optionally wraps the base ring strategy in a pincer or quantized-pincer emitter.
- **The per-step emission.** Whatever sub-emitter is chosen, the step body is the same four wire-level events: remote-write unicast DMA, remote sync-flag bump (receiver-side, automatic), local sync-flag wait, VPU reduce via `fmerge`. The phases are `Running phase 0` (reduce-scatter), `Running phase 1` (all-gather), and an optional `Running phase 2` (cleanup).
- **The sub-emitter routing.** The exact set of terminal classes (`RingSumEmitter`, `UniDirection1DRingStrategy`, `StrategyRing`, `UniDirectionNDRingStrategy`, `RotatedPincerEmitter`, `RotatedPincerShortEmitter`, `RotatedPincerQuantizedEmitter`) and the predicate gates that route between them.

| | |
|---|---|
| **Emission entry** | `AllReduceEmitter::EmitAllReduce` @ `0x13742200` (7,962 B / 1,445 lines) |
| **Top-level wrapper** | `AllReduceEmitter::Emit` @ `0x13745de0` (sync → `EmitAllReduce`, fusion → `EmitAllReduceFusion`) |
| **Fusion variant** | `AllReduceEmitter::EmitAllReduceFusion` @ `0x13746360` |
| **Picker (upstream)** | `BaseStrategyND::SelectNDStrategy` @ `0x137c78e0` — see [strategy-nd-picker](../collectives/strategy-nd-picker.md) |
| **Binomial gate** | `RingSumEmitter::MayUseSinglePhaseRingEmitter` @ `0x1375c1c0` |
| **Binomial builder** | `RingSumEmitter::CreateEmitter` @ `0x13760720` |
| **Inference-short gate** | `RingSumEmitter::ShouldUseInferenceShortLatencyEmitter` @ `0x1375ea20` |
| **1-D ring locator** | `AllReduceEmitter::GetRingLocationWrapper` @ `0x137412c0` |
| **Cross-module test** | `cross_replica_sharding_util::IsCrossModuleReduceInstruction` |
| **Supported dtypes** | `kSupportedTypes` @ `.rodata 0x0ae5a56c` = `{F32=11, S32=4, U32=8, BF16=16, PRED=1}` |
| **Source TU** | `platforms/xla/service/jellyfish/lowering/` (`ring_sum_emitter.cc`, `rotated_pincer_emitter*.cc`) |
| **Confidence** | HIGH for the dispatch fork and sub-emitter set (decompile-verified call sites); per-row exceptions noted |

---

## Where This Sits

`EmitAllReduce` is the **bottom of the AllReduce compile stack**, one level above the per-family wire emitter. The chain is:

```text
HloAllReduce  ──(SPMD partitioner, replica groups decided)──▶
  AllReduceEmitter::Emit  (0x13745de0)         # sync vs fusion fork
    ├─ sync     ──▶ AllReduceEmitter::EmitAllReduce       (0x13742200)   ◀── THIS PAGE
    └─ fusion   ──▶ AllReduceEmitter::EmitAllReduceFusion (0x13746360)
                          │
   (inside EmitAllReduce)  ├─ SelectNDStrategy / GetRingLocationWrapper  # choose ring shape
                          ├─ construct ONE sub-emitter (ring / pincer)
                          └─ drive its 2-phase step loop (RS + AG)
                                 per step: DMA → remote sflag bump → local sflag wait → VPU reduce
                                              │
                                  EnqueueDmaInGranules → DMA_TYPE_REMOTE_WRITE_UNICAST  (../ici/dma-descriptor.md)
```

What this page does **not** cover, with links:

- The **collective-level binomial / recursive-doubling algorithm** — the per-rank butterfly partner schedule, the `int32[N×8]` replica table, the viability gate — is on [Binomial / Recursive-Doubling](../collectives/binomial-recursive-doubling.md). This page only documents *that* `EmitAllReduce` routes into it, and how.
- The **strategy picker** — the decision tree that classifies topology into the five terminal ring classes — is `SelectNDStrategy`, documented on [SelectNDStrategy](../collectives/strategy-nd-picker.md). `EmitAllReduce` *calls* it; it does not reimplement it.
- The **bidirectional pincer loop shape** (overlapped send/recv windows, the `[dim][color]` sflag table) is on [Hierarchical AllReduce / Pincer](../collectives/allreduce-hierarchical-pincer.md).
- The **per-generation DMA descriptor byte layout** and the remote sync-flag encoding are on [DMA Descriptor](dma-descriptor.md).
- The **quantized 8-bit wire path** (`RotatedPincerQuantizedEmitter`, the `{S8, F8E5M2, F8E4M3B11FNUZ}` wire set) is on [FP8 Quantized Collective](../collectives/fp8-quantized-collective.md); this page only notes the dispatch hook into it.

---

## The Dispatch Fork

`EmitAllReduce` is a tall `if/else` cascade. After validating the instruction and reading its `BackendConfig` (`HloInstruction::backend_config<jellyfish::BackendConfig>` at line 656, which carries the `BarrierConfig` and any prefer-flags baked at partition time), it splits the world into three mutually-exclusive paths. The decompile site numbers below are from `0x13742200`.

The top-level branch is the **N-D / non-N-D split** at line 775 (`if (!ShouldUseInferenceShortLatencyEmitter && !(size_gate | …))`). The N-D arm lives in the *taken* side (Step 2 below, `SelectNDStrategy` at line 794); the binomial and 1-D arms are siblings in the *else* side (line 985 onward), separated by the `MayUseSinglePhaseRingEmitter` test at line 1010. The Steps below are ordered binomial → N-D → 1-D for narrative clarity, which is *not* the source order; consult the line numbers for the actual control flow.

### Step 0 — prologue: replica groups, cross-module test, barrier config

```text
emit_all_reduce(replica_groups, fmerge):                     # 0x13742200
    is_cross_module = IsCrossModuleReduceInstruction(hlo)     # line 557 — replica × partition fold
    cfg             = hlo.backend_config<BackendConfig>()      # line 656
    barrier         = cfg.has_barrier ? cfg.barrier            # line 682/688 — BarrierConfig
                                      : BarrierConfig_globals_  #   default global barrier
    # (the merge functor `fmerge` is captured here, never inspected)
```

`IsCrossModuleReduceInstruction` (line 557, 560) classifies the AllReduce as cross-module (reduce across replicas *and* partitions simultaneously) vs single-module. It is read as the boolean `IsCrossModuleReduceInstruction` (stored at `[rbp-2Ah]`) and threaded into every downstream builder — it changes which replica-group fold the ring locator uses and gates the binomial cross-module sub-path.

### Step 1 — binomial single-phase ring (sibling of the 1-D arm, line 1010+)

```text
    may_single = RingSumEmitter::MayUseSinglePhaseRingEmitter(mem_unit,     # 0x1375c1c0, line 997
                                  span_size, max_scratch, target, env)
    if env.force_1d_ring != 1 or (size_gate | short_latency                # line 1010 — branch test
                                  | may_single | (num_colors != 3)):
        # build the binomial / single-phase ring emitter; install two closures:
        ring_loc_provider = $_0  # returns net_util::RingLocation   (line 1031)
        group_provider    = $_1  # returns BinomialGroupData         (line 1039)
        emitter = RingSumEmitter::CreateEmitter(span_size, max_scratch,      # 0x13760720, line 1054
                                                ..., fmerge, barrier, ...)
        emitter->Build(); emitter->Emit()                                    # virtual, vtable+8 / vtable+...
        return
```

`MayUseSinglePhaseRingEmitter` (`0x1375c1c0`) is one term of the branch test at line 1010 — the binomial/ring side is taken unless the env's force-1D-ring flag is set *and* none of `{size_gate, short_latency, may_single, num_colors!=3}` holds, in which case control falls through to the 1-D arm. `CreateEmitter` (`0x13760720`) then constructs the concrete `BinomialSinglePhaseRingSumEmitter` (when the viability flag is set) or a plain `SinglePhaseRingSumEmitter` ring (both `SetEmitter` tags — `"BinomialSinglePhaseRingSumEmitter"` and `"SinglePhaseRingSumEmitter"` — are present in the `CreateEmitter` body). The two closures `$_0` and `$_1` are the smoking gun for the binomial datapath: `$_0` yields a `net_util::RingLocation` (the core's ring position) and `$_1` yields a `BinomialGroupData` (the precomputed counterparts vector read from the `int32[N×8]` replica table). The **algorithm those closures feed** — the recursive-doubling butterfly — is documented in full on [Binomial / Recursive-Doubling](../collectives/binomial-recursive-doubling.md); this page's only claim is that `EmitAllReduce` constructs the emitter and supplies the two providers.

> **NOTE —** `MayUseSinglePhaseRingEmitter` is a *prefer* gate, not a correctness gate. The viability constraint (`N` a power of two, `N ≤ 128`) is enforced inside the binomial emitter, not here. If the gate passes but the ring is non-power-of-2, `CreateEmitter` falls back to a ring `RingSumEmitter`. A reimplementer must not treat the binomial path as "always taken when small."

### Step 2 — N-D arm (multi-axis torus)

When the binomial fast path is not taken and the topology is multi-axis, `EmitAllReduce` calls the picker and then constructs a ring or pincer over the result.

```text
    strategy = SelectNDStrategy(target, env, !is_cross_module, hlo, b, ...)  # 0x137c78e0, line 794
    if want_pincer:                                                          # prefer-flag driven
        if quantized:  emitter = make_unique<RotatedPincerQuantizedEmitter>(strategy, ...,  # line 886
                                       fmerge, barrier, primitive_type, QuantizedAllReduceStage, ...)
        else:          emitter = make_unique<RotatedPincerEmitter>(strategy, ...,            # line 907
                                       fmerge, barrier, ...)
        # log tag "Long Pincer" (line 871) or "2-D rotated pincer" (line 630)
    elif short_latency_pincer:
        emitter = RotatedPincerShortEmitter::CreateIfFeasible(strategy, ..., barrier, ...)   # line 847
    else:
        strategy_obj = new UniDirectionNDRingStrategy(strategy, b)   # operator new(0x5B0), line 815/816
        emitter      = strategy_obj                                  # the ring IS the emitter
    emitter->Build()                                                 # (*(vtable+8))(emitter), line 921
    emitter->Emit()
```

`SelectNDStrategy` returns a `BaseStrategyND*` (`v93`/`v136` in the decompile). The N-D arm then either:
- uses that strategy object directly by wrapping it in a `UniDirectionNDRingStrategy` (`operator new(0x5B0)` at line 815 — the 1,456-byte ND-ring emitter object), or
- hands the strategy to a pincer constructor (`RotatedPincerEmitter`, line 907) or its quantized variant (`RotatedPincerQuantizedEmitter`, line 886, which also takes a `PrimitiveType` and a `QuantizedAllReduceStage`), or
- to the short-latency pincer via `RotatedPincerShortEmitter::CreateIfFeasible` (line 847).

The choice among these is prefer-flag driven (the same flags the picker reads); `EmitAllReduce` only reads the result. Whichever object is built, it is driven through the same virtual `Build()`/`Emit()` pair (the `(*(void (**)(...))(*(_QWORD*)v136 + 8LL))(v136)` indirect call at line 921 is `vtable[1]`).

### Step 3 — 1-D arm (single-axis ring)

When the topology resolves to a single usable axis, `EmitAllReduce` takes the 1-D path, anchored by `GetRingLocationWrapper`.

```text
    ring_loc = GetRingLocationWrapper(hlo, replica_groups, is_cross_module,  # 0x137412c0, line 1093
                                      env, ...)                              #   → net_util::RingLocation
    if want_unidir:
        emitter = make_unique<UniDirection1DRingStrategy>(ring_loc, span,    # line 1120
                                                          b, shared_registry, is_bf16)
    else:
        emitter = make_unique<StrategyRing>(ring_loc, span, shared_registry, # line 1134
                                            local_value, is_bf16)
    # optional wrap, same as the N-D arm:
    if short_latency:  emitter = RotatedPincerShortEmitter::CreateIfFeasible(...)   # line 1170
    if quantized:      emitter = make_unique<RotatedPincerQuantizedEmitter>(...)    # line 1227
    elif want_pincer:  emitter = make_unique<RotatedPincerEmitter>(...)             # line 1241
    emitter->Build(); emitter->Emit()                                # (*(vtable+8))(v93), line 1205
```

`GetRingLocationWrapper` (`0x137412c0`) wraps `GetRingLocation` (`0x13740e40`) and `GetRingLocationWithReordering` (`0x137410e0`) — it returns the current core's `net_util::RingLocation{ring_index, position_in_ring, ring_size}` within its color's ring, applying the limited-ICI-routing reorder when the routing table needs it (see [Routing Overview](../routing/overview.md)). The base ring strategy is then either `UniDirection1DRingStrategy` (fixed `+1` CW neighbour, line 1120) or `StrategyRing` (line 1134), with the same optional pincer / quantized wrap as the N-D arm.

> **NOTE —** the 1-D and N-D arms share the *same* terminal pincer classes (`RotatedPincerEmitter`, `RotatedPincerShortEmitter`, `RotatedPincerQuantizedEmitter`). The difference is only the **base strategy object** they wrap: a `RingLocation`-built `UniDirection1DRingStrategy`/`StrategyRing` (1-D) vs a `SelectNDStrategy`-built `UniDirectionNDRingStrategy` (N-D). The pincer is a decorator over the ring, not a separate ring algorithm.

---

## The Sub-Emitter Routing Table

The full set of terminal sub-emitters `EmitAllReduce` can construct, with the decompile call site and the gate. All `make_unique`/`operator new`/`CreateIfFeasible` sites are confirmed present in `0x13742200`.

| Sub-emitter | Built by | Arm | Gate |
|---|---|---|---|
| `BinomialSinglePhaseRingSumEmitter` | `RingSumEmitter::CreateEmitter` @ line 1054 | binomial | `MayUseSinglePhaseRingEmitter` + power-of-2 `N≤128` |
| `SinglePhaseRingSumEmitter` (plain ring) | `RingSumEmitter::CreateEmitter` @ line 1054 | binomial | `MayUseSinglePhaseRingEmitter`, non-binomial fallback |
| `UniDirection1DRingStrategy` | `make_unique` @ line 1120 | 1-D | single axis, unidirectional |
| `StrategyRing` | `make_unique` @ line 1134 | 1-D | single axis, ring (non-unidir) |
| `UniDirectionNDRingStrategy` | `operator new(0x5B0)` @ line 815 | N-D | multi-axis, no pincer |
| `RotatedPincerEmitter` | `make_unique` @ lines 907 / 1241 | both | bandwidth-bound, pincer prefer-flag |
| `RotatedPincerShortEmitter` | `CreateIfFeasible` @ lines 847 / 1170 | both | latency-bound bidirectional |
| `RotatedPincerQuantizedEmitter` | `make_unique` @ lines 886 / 1227 | both | `CanLowerToQuantizedAllReduce` (line 699) |

`RotatedPincerQuantizedEmitter::CanLowerToQuantizedAllReduce` is consulted early (line 699) — if the quantized level and size threshold are met and the dtype is in `kSupportedQuantizationTypes` (`{S8, F8E5M2, F8E4M3B11FNUZ}`), the quantized wrap is taken in whichever arm fires. The constructor takes an extra `PrimitiveType` and a `QuantizedAllReduceStage` argument absent from the non-quantized pincer (compare the mangled `make_unique` signatures at line 886 vs 907). Detail on [FP8 Quantized Collective](../collectives/fp8-quantized-collective.md).

> **NOTE —** the `RotatedPincerQuantizedEmitter`/`RotatedPincerEmitter` constructors take `RotatedPincerEmitterBase::ExtraArgs` by value (visible in the mangled name `...RotatedPincerEmitterBase9ExtraArgsE`). That bundles the per-color VMEM scratch sizing and the reserved-sflag block so the pincer can size its `[dim][color]` recv-flag table — the bidirectionality bookkeeping. The async-pincer family (`AsyncPincerEmitter`) is constructed on the fusion path (`EmitAllReduceFusion`, `0x13746360`), not in the sync `EmitAllReduce` body; the sync body tops out at the rotated pincer.

---

## The Per-Step Emission

Whichever sub-emitter `EmitAllReduce` builds, the LLO program it emits has the same shape: a two-phase loop where each step is one round-trip across one ICI link plus one local reduce. The phase strings `Running phase 0`, `Running phase 1`, and (some emitters) `Running phase 2`, plus `Resetting reduction buffer for phase 0`, are the binary anchors for these phases.

```text
# the shape the chosen sub-emitter's Build()/Emit() produces
for color in 0 .. num_colors-1:                      # concurrent rings, one per torus axis
    N   = ring_size[color]
    pos = ring_position[color]                        # from RingLocation / BinomialGroupData

    # ---- (optional) startup barrier ----
    if barrier.scope != none:                         # BarrierConfig from backend_config
        BarrierStart(barrier_sync_flag, ...)          # TreeBarrierType::kAll / kCrossReplica

    # ---- PHASE 0: reduce-scatter (N-1 steps, ring) / log2(N) steps (binomial) ----
    for i in 0 .. steps-1:
        peer = next_peer(pos, i, color)               # CwCore (ring) or counterparts[i] (binomial)
        EnqueueDmaInGranules(out_shard, peer,         # DMA_TYPE_REMOTE_WRITE_UNICAST
                             remote_sflag_handle)      #   carries the receiver's sflag to bump
        recv = DmaDoneInGranules(...)                  # local sync-flag WAIT ("shard-{cw,ccw}-recv-wait")
        fmerge(local_copy, recv, b)                    # VPU vadd/vmin/vmax/vmul/vand/vor

    # ---- PHASE 1: all-gather (N-1 steps; absent on binomial, which self-completes) ----
    for i in 0 .. N-2:
        peer = next_peer(pos, i, color)
        EnqueueDmaInGranules(reduced_shard, peer, ...)
        DmaDoneInGranules(...)                          # "shard-send-wait"
        SafeMemcopyN(recv -> shard)                     # plain copy, NO merge
```

The four wire-level events per reduce-scatter step are:

1. **Remote-write DMA** — `EnqueueDmaInGranules` emits a `DMA_TYPE_REMOTE_WRITE_UNICAST` descriptor: source = local VMEM offset of the outgoing shard, destination = the slot in the neighbour's reduce-scatter scratch, plus a **remote sync-flag handle**. The descriptor word layout is per-generation; see [DMA Descriptor](dma-descriptor.md).
2. **Remote sync-flag bump** — when the chunk lands, the receiving NodeFabric Ingress Unit auto-increments the encoded remote sync flag (the wire-level `atomic_remote_add_set_done`). The compiler emits no software ack on the receive side.
3. **Local sync-flag wait** — `DmaDoneInGranules` emits the wait on the local receive sync flag, annotated `shard-cw-recv-wait` / `shard-ccw-recv-wait` (reduce side, direction-qualified by the ring leg) or `shard-send-wait` / `shard-cw-send-wait` / `shard-ccw-send-wait` (gather side). The runtime watchdog string is `Sflag wait timeout on op {…}`.
4. **VPU reduce** — once the wait clears, the merge functor `fmerge` is called on `(local_copy, just_received_chunk)`. This is a plain scalar VPU op (`vadd`/`vmin`/`vmax`/`vmul`/`vand`/`vor`), folded into the step body. There is no reduce-on-wire.

The all-gather phase replaces the `fmerge` with a plain `SafeMemcopyN` — it rotates the already-reduced shards around the ring without reducing.

> **QUIRK —** the merge functor `fmerge` is the *second argument* to `EmitAllReduce` and is captured opaquely. The function never branches on the reduction kind; the kind was resolved upstream (`xla::MakeReductionComputation(kind, dtype)`) into the closure body. This is the structural reason the ICI fabric is reduction-op-agnostic: by the time `EmitAllReduce` runs, "SUM vs MAX" is already a compiled VPU instruction inside `fmerge`, not a value the emitter or the wire can see.

---

## Input Validation and Dtype Gate

`EmitAllReduce` rejects unsupported element types before it dispatches. `kSupportedTypes` (`.rodata 0x0ae5a56c`, 20 bytes = five `int32`) is exactly `{F32=11, S32=4, U32=8, BF16=16, PRED=1}` (XLA `PrimitiveType` enum values, byte-confirmed). Anything else must be promoted upstream — the SPMD partitioner inserts the convert.

The boolean (`PRED`) path is further restricted by the MLIR-level verifier: the strings `Vector mask all-reduce must have i32 output` and `Mask all-reduce only supports sum and find_first_set kinds` (recovered from `mlir::tpu::AllReduceOp::verify` @ `0x14b01460`) gate the mask AllReduce to SUM and a find-first-set reduction. The five `REDUCTION_KIND_*` enum values (`UNSPECIFIED=0, SUM=1, PRODUCT=2, MIN=3, MAX=4`) are the kinds the merge functor can encode; `UNSPECIFIED` is rejected at validation.

The BF16 accumulation choice — whether BF16 reduces natively on the wire or upgrades to F32 — is decided upstream (`bf16_inside_cross_replica_sum` @ `0x1373ca60`, plus the SPMD flag `xla_tpu_spmd_f32_accum_for_bf16_ar`), so by the time `EmitAllReduce` runs, the dtype it sees is the dtype the ring carries. The per-step VPU mnemonic the reduce uses (`VADDBF16rr_V0` vs `VADDrr_V0`, etc.) is a property of `fmerge`, not of this function.

---

## Sync vs Fusion Fork (`Emit` @ `0x13745de0`)

The public entry is `AllReduceEmitter::Emit` (`0x13745de0`, 1,112 bytes), a thin wrapper that parses the instruction's operands and forks on whether the AllReduce is fused:

```text
Emit():                                                   # 0x13745de0
    parse operands / shape
    if not fused:  EmitAllReduce(replica_groups, fmerge)   # 0x13742200, line 139 — THIS PAGE
    else:          EmitAllReduceFusion()                   # 0x13746360, line 144
```

The decompile shows exactly two terminal calls: `EmitAllReduce` (line 139) and `EmitAllReduceFusion` (line 144). The fusion variant (`EmitAllReduceFusion` @ `0x13746360`, 7,136 bytes) handles the async (`kAsyncStart`/`kAsyncUpdate`/`kAsyncDone`) and continuation-fusion forms, and is where the `AsyncPincerEmitter` family and `EmitColorwiseFusedAllReduce` (`0x1374c140`) live. The sync `EmitAllReduce` documented here is the single-blocking-call site: all reduce-scatter + all-gather steps emitted inside one LLO region.

> **NOTE —** the distinction between `Emit`'s two arms is the AllReduce's *async-ness*, decided by the SPMD `TpuAsyncCollectiveCreator` pass. A non-async AllReduce always lands in `EmitAllReduce`; an async one in `EmitAllReduceFusion`. Both arms ultimately drive the same sub-emitter families and the same per-step wire format — the fusion arm just interleaves the steps with surrounding compute and synchronizes via sflags rather than blocking.

---

## Reimplementation Checklist

- Take the merge functor `fmerge` as an opaque `std::function<LloValue*(LloValue*, LloValue*, LloRegionBuilder)>`; never branch on the reduction kind inside the emitter — it is already compiled into the closure.
- Reject any element type outside `{F32, S32, U32, BF16, PRED}` before dispatch; restrict the `PRED` path to SUM / find-first-set.
- Read `is_cross_module` from `IsCrossModuleReduceInstruction` and the `BarrierConfig` from the instruction's backend config *before* choosing a sub-emitter; both feed the builders.
- Dispatch in three checks, in order: (1) `MayUseSinglePhaseRingEmitter` → `CreateEmitter` (binomial/ring fast path, installs the `RingLocation` and `BinomialGroupData` providers); (2) multi-axis topology → `SelectNDStrategy` → `UniDirectionNDRingStrategy` (optionally pincer-wrapped); (3) single axis → `GetRingLocationWrapper` → `UniDirection1DRingStrategy`/`StrategyRing` (optionally pincer-wrapped).
- Apply the pincer / quantized-pincer wrap as a *decorator* over the chosen base ring strategy, identically in both the 1-D and N-D arms. Consult `CanLowerToQuantizedAllReduce` before the quantized wrap.
- Drive every constructed sub-emitter through the same virtual `Build()`/`Emit()` pair (vtable+8). The per-step body must emit, per reduce-scatter step: `EnqueueDmaInGranules(REMOTE_WRITE_UNICAST)` → `DmaDoneInGranules` (`shard-{cw,ccw}-recv-wait`) → `fmerge`; the all-gather step replaces `fmerge` with `SafeMemcopyN`.
- The async / continuation-fusion forms belong in `EmitAllReduceFusion`, not here; `EmitAllReduce` is the single-region blocking path only.

---

## Cross-References

- [ICI Overview](overview.md) — where the AllReduce primitive sits in the ICI subsystem (bring-up → discovery → transfer).
- [DMA Descriptor](dma-descriptor.md) — the per-generation descriptor word layout and the remote sync-flag encoding each step emits.
- [Link Bring-Up](link-bringup.md) — the firmware PHY / host DL state machine that brings the links up before any DMA can ride them.
- [Failure Recovery](failure-recovery.md) — the `Sflag wait timeout` watchdog, `VerifyDmaCountDone`, and the link-fatal cascade behind a stalled AllReduce.
- [Binomial / Recursive-Doubling](../collectives/binomial-recursive-doubling.md) — the butterfly partner schedule and replica table behind the binomial fast path this page routes into.
- [SelectNDStrategy](../collectives/strategy-nd-picker.md) — the upstream picker that classifies topology into the ring shape `EmitAllReduce` builds.
- [Hierarchical AllReduce / Pincer](../collectives/allreduce-hierarchical-pincer.md) — the bidirectional pincer loop the `RotatedPincer*` wrap produces.
- [Reduce-Scatter](../collectives/reduce-scatter.md) — the reduce-scatter half of the two-phase decomposition as its own collective.
- [FP8 Quantized Collective](../collectives/fp8-quantized-collective.md) — the `RotatedPincerQuantizedEmitter` 8-bit wire path the quantized wrap dispatches into.
- [Routing Overview](../routing/overview.md) — how `GetRingLocationWrapper`'s peer becomes an ICI-routable remote address (limited-routing reorder).
- [back to index](../index.md)
