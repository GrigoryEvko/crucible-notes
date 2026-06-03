# Low-Precision / Quantized Collectives

> *All addresses, symbols, offsets, and `.rodata` constants on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped, `.text` VA == file offset). Other versions will differ; treat every VA as version-pinned.*

## Abstract

This page documents the **reduction-precision** of TPU collectives — the two distinct, independent mechanisms by which `libtpu` changes the numeric format used to *reduce* a collective, and an honest present/absent inventory of what the binary actually contains. The two mechanisms are easy to conflate but are emitted by different passes, on different substrates, with different goals:

- **BF16 accumulation-accuracy promotion** (the *up*cast). The SPMD partitioner can rewrite a BF16 `all-reduce` into `convert(bf16→f32) → all-reduce-in-f32 → convert(f32→bf16)` so the reduction *accumulates* in F32 instead of BF16. This is the `MayIncreaseBF16AllReduceAccumulationAccuracy` gate (`0x127a22c0`) that [auto-sharding-spmd.md](../compiler/auto-sharding-spmd.md) cites. It costs more wire bandwidth (F32 payload) but gives a more accurate sum. It is gated by `xla_tpu_spmd_f32_accum_for_bf16_ar` and a companion minimum-subgroup-size flag.
- **8-bit on-wire quantized all-reduce** (the *down*cast). Distinct from the above, `libtpu` carries a complete `RotatedPincerQuantizedEmitter` family that **symmetrically quantizes each shard to 8 bits before the wire write**, reduces step-by-step, and dequantizes at the end — trading numeric range for **half/quarter** the ICI payload. The supported 8-bit formats are `S8`, `F8E5M2`, and `F8E4M3B11FNUZ`. This is selected by the `TpuQuantizedAllReduceBackendConfigSetter` HLO pass (`0x11107b00`) under `xla_tpu_quantized_all_reduce_level` and a size threshold.

The **present/absent inventory is the central result of this page** (see §3). The headline corrections to a naive reading: fp8 collective *compression* **is present** — but it is **all-reduce-only** (there is no quantized all-gather / all-to-all / reduce-scatter symbol in the binary), and the quantize set is exactly `{S8, F8E5M2, F8E4M3B11FNUZ}` — **`F8E4M3Fn` is absent** from the collective quantizer, and there is **no zero-point** (the device quantizer is strictly symmetric).

Contract of the quantized-collective surface as observed in the binary:

- **The two precision knobs are orthogonal and independently gated.** The F32-accumulation promotion runs in the SPMD partitioner (host-side HLO rewrite, before lowering); the 8-bit-on-wire quantization runs as a separate HLO backend-config pass and is realized by a TensorCore lowering emitter. Neither implies the other.
- **The quantized all-reduce reduces in F32, not in 8-bit.** The 8-bit format is the **wire** format only. Each ring step dequantizes both shards to F32, merges in F32, and re-tracks the running absmax before re-quantizing for the next hop — the integer/fp8 bytes never participate in arithmetic.
- **The device quantizer is symmetric absmax** with one scale per shard and **zero-point fixed at 0**. `scale = absmax / qmax`, where `qmax ∈ {127.0 (S8), 57344.0 (F8E5M2), 30.0 (F8E4M3B11FNUZ)}`.
- **Compression is gated on hardware generation and shape.** `CanLowerToQuantizedAllReduce` (`0x13798420`) requires `TpuVersion ∈ {3, 4}` (viperfish / ghostlite) and a shape whose minor/second-minor dimensions are a multiple of lane/sublane count and whose element type is BF16 or F32.

## At a glance

| Aspect | BF16→F32 accumulation promotion | 8-bit on-wire quantized all-reduce |
|--------|---------------------------------|------------------------------------|
| Direction | **up**cast (more accurate, more bandwidth) | **down**cast (less bandwidth, lossy) |
| Gate function | `MayIncreaseBF16AllReduceAccumulationAccuracy` `0x127a22c0` | `RotatedPincerQuantizedEmitter::CanLowerToQuantizedAllReduce` `0x13798420` |
| Realized by | SPMD creator wrapper `$_0` `0x127a4340` (clone + `set_element_type` + `CreateConvert`) | `RotatedPincerQuantizedEmitter` family + `pincer_utils` |
| Selecting pass | `TpuSpmdPartitioner::AllReduceAlongShardingDims` `0x127a28c0` | `TpuQuantizedAllReduceBackendConfigSetter::RunImpl` `0x11107b00` |
| Where it runs | host HLO rewrite (pre-lowering) | HLO backend-config tag → TensorCore lowering |
| Format(s) | F32 accumulate, BF16 endpoints | `S8`(2), `F8E5M2`(19), `F8E4M3B11FNUZ`(23) |
| Primary flag | `xla_tpu_spmd_f32_accum_for_bf16_ar` | `xla_tpu_quantized_all_reduce_level` |
| Collectives covered | `all-reduce` (BF16 reduction) | **`all-reduce` only** |
| Zero-point | n/a (format change) | none (symmetric) |

---

## 1. BF16 → F32 accumulation-accuracy promotion

### 1.1 What it is

The default BF16 all-reduce both *transports* and *accumulates* in BF16: each ring step adds a received BF16 shard into a BF16 partial sum, so rounding error compounds across the ring. The promotion replaces that with an F32 accumulation: the operand is up-converted to F32, the all-reduce sums in F32 (the reduction computation itself is cloned and re-typed to F32), and the result is converted back to BF16. The wire payload doubles; the sum is more accurate.

### 1.2 The gate — `MayIncreaseBF16AllReduceAccumulationAccuracy`

`xla::jellyfish::(anonymous namespace)::MayIncreaseBF16AllReduceAccumulationAccuracy` (`0x127a22c0`) is reached from the TPU SPMD partitioner override `AllReduceAlongShardingDims` (`0x127a28c0`); see the [TPU Overrides](../compiler/auto-sharding-spmd.md) table. It takes an `ObjectView<TpuCompilationEnvironment>` and the `SPMDCollectiveOpsCreator`, and **returns a (possibly wrapped) creator**.

The decompile is a clean two-arm structure keyed on one config byte:

```c
// MayIncreaseBF16AllReduceAccumulationAccuracy — 0x127a22c0
if ( *(_BYTE *)(tpu_comp_env + 4368) )      // xla_tpu_spmd_f32_accum_for_bf16_ar
{
    // TRUE arm: build a NEW SPMDCollectiveOpsCreator whose all-reduce callback
    // is the $_0 wrapper (0x127a4340). The companion field at +4632 (the
    // min-subgroup-size threshold) is captured into the wrapper's closure.
    creator.all_reduce_cb = &__call_func<...::$_0>;       // 0x127a4340
}
else
{
    // FALSE arm: pass the original creator through unchanged (no promotion).
}
```

The byte at `tpu_comp_env + 4368` is the `xla_tpu_spmd_f32_accum_for_bf16_ar` flag; the captured quadword at `+4632` is the `xla_tpu_spmd_f32_accum_for_bf16_ar_min_subgroup_size` companion. When the flag is set, the original creator's all-reduce callback is replaced by the `$_0` closure that performs the actual rewrite.

### 1.3 The rewrite — the `$_0` wrapper

`__call_func<…MayIncreaseBF16AllReduceAccumulationAccuracy…::$_0>` (`0x127a4340`) is the wrapped creator. Its signature is the collective creator's all-reduce slot — `HloInstruction*(SpmdBuilder*, HloInstruction* operand, HloComputation* reduction, CollectiveDeviceListBase const&, long)`. The decoded body is a textbook upcast-reduce-downcast:

```c
// $_0 — 0x127a4340  (the F32-accumulation wrapper)
//  [1] subgroup-size short-circuit:
if ( creator.num_devices() > 0 && subgroup_min < creator[+32] )   // line 66
    return original_creator(operand, reduction, ...);             // too small -> passthrough

//  [2] dtype short-circuit:
if ( element_type(operand) != BF16 /*16*/ || rank-flag == 55 )    // line 75
    return original_creator(operand, reduction, ...);             // not BF16 -> passthrough

//  [3] clone the reduction computation and re-type its BF16 nodes to F32:
clone = HloComputation::Clone("clone");                           // line 84
AddEmbeddedComputation(clone);
for each instr in clone:                                          // lines 90..218
    if element_type(instr) == BF16: set_element_type(instr, F32); // line 159-160
    if shape is TUPLE (prim 4): ForEachMutableSubshapeHelper(... promote BF16->F32);

//  [4] upcast operand, run the F32 all-reduce, downcast result:
f32_in  = CreateConvert(operand, ChangeElementType(shape, F32 /*11*/));   // line 222-223
f32_ar  = original_creator(f32_in, clone, ...);                          // line 227 (F32 reduce)
bf16_out= CreateConvert(f32_ar, ChangeElementType(shape, BF16 /*16*/));  // line 228-230
return bf16_out;
```

So the promotion is **not** a hardware accumulator-width switch — it is an HLO graph rewrite that wraps the all-reduce in BF16↔F32 `convert` ops and re-types the reduction body. `PrimitiveType` integers used: `BF16 = 16`, `F32 = 11`, `TUPLE = 4`. The two short-circuits (subgroup too small; operand not BF16) leave the original creator untouched.

> **NOTE —** the subgroup-size short-circuit (`creator.num_devices() > 0 && subgroup_min < creator[+32]`) is the runtime use of `xla_tpu_spmd_f32_accum_for_bf16_ar_min_subgroup_size`: the F32 promotion is skipped when the all-reduce's subgroup is below the threshold (small reductions do not accumulate enough error to be worth the doubled payload). Whether the threshold compares the subgroup *size* or *count* was not bit-traced (LOW).

### 1.4 The on-wire reduction-dtype companion — `bf16_inside_cross_replica_sum`

A second, lower-level decision exists at lowering time: `AllReduceEmitter::bf16_inside_cross_replica_sum` (`0x1373ca60`) decides whether the ICI ring reduction runs *on the wire* in BF16 vs F32, independently of the SPMD-level promotion above.

```c
// AllReduceEmitter::bf16_inside_cross_replica_sum — 0x1373ca60
reduce_scatter = ExtractInstruction(fusion, {9, 11, 0x3d});      // AR / AR-start / reduce-scatter
CHECK(reduce_scatter != nullptr);                                // all_reduce_emitter.cc:585
if ( GetTpuCompEnv()[+3789] )    return true;                    // xla_jf_bf16_inside_cross_replica_sum
else                            return element_type(to_apply.root) == BF16 /*16*/;
```

When the `xla_jf_bf16_inside_cross_replica_sum` flag (config byte at `TpuCompEnv + 3789`) is set, BF16 in-ring summation is forced; otherwise it follows the reduction computation's root element type. This is the knob the SPMD-level F32 promotion (§1.3) is the *opposite* of — the promotion exists precisely to override this default toward F32 for accuracy-sensitive reductions.

---

## 2. The 8-bit on-wire quantized all-reduce

This is the genuine **lossy compression** path: it quantizes each shard to an 8-bit format before transmission to halve (or quarter) the ICI bytes moved per ring step. It is realized by the `RotatedPincerQuantizedEmitter` (a quantized specialization of the bidirectional [pincer](allreduce-hierarchical-pincer.md) all-reduce) and the `pincer_utils` symmetric-quant helpers.

### 2.1 Selection — `TpuQuantizedAllReduceBackendConfigSetter`

The pass `TpuQuantizedAllReduceBackendConfigSetter::RunImpl` (`0x11107b00`) walks every non-fusion computation, finds `all-reduce` ops (opcode `9`, decompile line 182), and tags the eligible ones with a `QuantizedAllReduceConfig` backend config. The decoded gate sequence:

```c
// TpuQuantizedAllReduceBackendConfigSetter::RunImpl — 0x11107b00
level = GetTpuCompEnv()[+5600];  if (level >= 4) level = 0;   // xla_tpu_quantized_all_reduce_level, clamp 0..3
thr_f = GetTpuCompEnv()[+0x15dc];                            // size threshold (MiB) -> elements via 0x84a2c94
thr_elems = vcvttss2si( 0x84a2c94 * thr_f * 0x84a2c94 );     // line 153-156
for each all-reduce (opcode 9):
    // (a) optional frontend-attribute override of dtype/stage:
    if frontend_attr[kQuantizeAllReduceDtypeFrontendAttribute] present:
        dtype = match{ "S8"->2, "F8E5M2"->19, "F8E4M3B11FNUZ"->23 }  // lines 241..327
                else FAIL "Unsupported quantized type: <name>"
    if frontend_attr[kQuantizeAllReduceStageFrontendAttribute] present:
        stage = match{ name -> 1 | 2 | 3 } else FAIL "Unsupported quantized stage: <name>"
    // (b) size gate (only when level != 0):
    if (level != 0 && GetSizeInBytes(operand) < thr_elems) continue;   // line 565-567
    // (c) emitter feasibility gate:
    if (!CanLowerToQuantizedAllReduce(instr, target))                  // line 581
        { VLOG "Not using quantized all-reduce for <hlo> ..."; continue; }
    // (d) write QuantizedAllReduceConfig { dtype, ?, stage } into backend config:
    cfg.dtype = dtype; cfg[+28] = ...; cfg.stage = stage;              // lines 710..735
```

So the config carries a **quantization dtype** and a **stage** enum (`QuantizedAllReduceStage`, values `1/2/3`). The pass establishes a baseline `{dtype=S8, stage=2}` config (line 234 `v140 = 0x200000001` = the packed `{1, 2}` field pair with default dtype), then overrides from frontend attributes if present. The two failure strings — `"Unsupported quantized type: "` and `"Unsupported quantized stage: "` — are byte-anchored in the TU `tpu_quantized_all_reduce_backend_config_setter.cc`.

| Frontend attribute / config field | Source | Decoded values | Confidence |
|---|---|---|---|
| quantize dtype | `kQuantizeAllReduceDtypeFrontendAttribute` | `S8`=2, `F8E5M2`=19, `F8E4M3B11FNUZ`=23 | Confirmed |
| quantize stage | `kQuantizeAllReduceStageFrontendAttribute` | `QuantizedAllReduceStage` ∈ {1, 2, 3} | Confirmed |
| level | `xla_tpu_quantized_all_reduce_level` (TpuCompEnv+5600) | clamped to 0..3 | Confirmed |
| size threshold | `xla_tpu_quantized_all_reduce_size_threshold_mib` (TpuCompEnv+0x15dc) | MiB → elements | Confirmed |
| operand combine | `xla_tpu_combine_quantized_all_reduce_operands` | bool (default-gen present) | Confirmed |

### 2.2 The feasibility gate — `CanLowerToQuantizedAllReduce`

`RotatedPincerQuantizedEmitter::CanLowerToQuantizedAllReduce` (`0x13798420`) is the per-op + per-target check. Two conjuncts:

```c
// CanLowerToQuantizedAllReduce — 0x13798420
if ( !IsShapeSupported(shape, target) )      // 0x13798560
    { VLOG "Minor/second-minor dimension is not a multiple of lane/sublane count"
          " or type is neither BF16 nor F32. Shape not supported ..."; return 0; }
if ( (unsigned)(TpuVersion(target) - 3) < 2 )  return 1;   // TpuVersion in {3,4}
VLOG "Target is not supported by RotatedPincerQuantizedEmitter. Target: <version>";
return 0;
```

- **Shape support** (`IsShapeSupported` `0x13798560`): the minor and second-minor dimensions must be a multiple of the lane / sublane vector count (so the shard tiles cleanly), and the *operand* element type must be **BF16 or F32** (the value being quantized is BF16/F32; the 8-bit type is the wire format, not the operand type).
- **Target generation**: `TpuVersion - 3 < 2`, i.e. `TpuVersion ∈ {3, 4}` = **viperfish / ghostlite**. Older generations are rejected with the byte-anchored "Target is not supported" VLOG (and the `TpuVersion` enumerator interpolated).

> **NOTE —** the `TpuVersion` numbering matches the [collectives overview](overview.md) §5 internal enum (`0 jellyfish, 1 dragonfish, 2 pufferfish, 3 viperfish, 4 ghostlite, 5 …`). The quantized all-reduce admits exactly `{viperfish, ghostlite}` — the same two generations whose `Target::SupportsVectorConvertF32Stochastic` set carries `{F8E5M2, F8E4M3B11FNUZ}`. That alignment (the SR-capable fp8 set == the AR-quantizable fp8 set) is consistent but was not proven causal here (LOW).

### 2.3 The symmetric-quant kernels — `pincer_utils`

The actual numeric kernels live in `xla::jellyfish::pincer_utils` and are the cleanest symmetric-absmax-8-bit reference in the binary. The scale formula is byte-exact:

```c
// pincer_utils::UpdateScale — 0x137b75c0
//   scale_out = qmax(dtype) / absmax        (the *quant* multiplier reciprocal)
qmax = switch(dtype) {                        // .rodata f32 constants:
   S8           (2):  127.0    // dword_84a2a28
   F8E4M3B11FNUZ(0x17): 30.0   // dword_84a27fc
   F8E5M2       (0x13): 57344.0 // dword_84a2530
   default: FATAL "Unsupported quantized shard type: %s"   // pincer_utils.cc:200
};
m       = VimmF32(qmax);
absmax  = Vld(max_abs_addr);                  // both addrs CHECKed to be in VMEM
scale   = VdivF32(m, absmax);                 // scale = qmax / absmax
Vst(scale_addr, scale);
```

The kernel family and its role in one ring step:

| `pincer_utils` kernel | Address | Role | Confidence |
|---|---|---|---|
| `UpdateMaxLocalChunk` | `0x137b73a0` | running absmax over the shard: `acc = max(acc, |x|)` | Confirmed |
| `UpdateScale` | `0x137b75c0` | `scale = qmax / absmax` (qmax per dtype above) | Confirmed |
| `SymmetricallyQuantizeShardInPlaceTo8Bits` | `0x137b7740` | `q = round(x · scale)` then lane-pack to 8-bit | Confirmed |
| `SymmetricallyDequantizeShardInPlace8Bit` | `0x137b7fc0` | `f = q / scale` (unpack 8-bit → F32) | Confirmed |
| `ReduceSymmetricallyQuantized8BitShardInPlace` | `0x137b8880` | per-step **F32** dequant → merge → re-track absmax | Confirmed |

The decisive structural fact, from `ReduceSymmetricallyQuantized8BitShardInPlace`: the per-step ring reduction **dequantizes both the local and received shard to F32, merges in F32 (the reduction functor), then re-tracks the absmax** for the next hop's re-quantization. The 8-bit integer/fp8 bytes are never summed directly — they are purely the wire representation. This is why the quantized path needs a *per-shard scale shipped alongside the data* and why it can carry an arbitrary reduction functor (sum / max / …).

### 2.4 The emitter — `RotatedPincerQuantizedEmitter`

The emitter is a quantized specialization of the bidirectional [pincer all-reduce](allreduce-hierarchical-pincer.md): it runs the bandwidth-optimal rotated ring in both directions, but inserts a quantize before each send and a dequantize after each receive. Its recovered surface:

| Method | Address | Role |
|---|---|---|
| `Init` | `0x13797700` | allocate scale buffers, scratch |
| `QuantizeShard` | `0x1379c5e0` | per-shard quantize before wire write |
| `DequantizeShard` | `0x1379c940` | per-shard dequantize after receive |
| `ComputeScaleValue` / `ComputeScaleFactor` | `0x1379e0a0` / `0x137a2440` | per-shard / per-step scale |
| `ReductionLoop` | `0x13798ec0` | the F32-merge ring loop |
| `DequantAndReduceShardInPlace` | `0x1379dd00` | dequant + merge fused |
| `SendOrWaitForShards` / `WaitForShardScaleFactor` | `0x1379e460` / `0x1379a500` | ICI handshake (data + scale) |
| `SetSummationPrecision` | `0x137a2400` | sets `LocalDmaPipe::Precision` (delegates to base) |
| `CanLowerToQuantizedAllReduce` | `0x13798420` | the §2.2 gate |

`SetSummationPrecision` (`0x137a2400`) is a thin forward to `RotatedPincerEmitterBase::SetSummationPrecision` — confirming that the *summation* precision (the F32-merge of §2.3) is a separate `LocalDmaPipe::Precision` enum knob from the 8-bit wire format. The quantize/dequantize/pack/unpack leaf ops (`VcvtF32ToS32`, `VpackcB8`, `VunpackCS8` / `VunpackCF8E5M2` / `VunpackCF8E4M3B11`, `VdivF32`) are the shared TensorCore convert surface and are documented with the matmul-epilogue numerics rather than here.

---

## 3. Present / absent inventory

This is the page's primary deliverable. Each row is byte-anchored and was confirmed (or refuted) by a symbol sweep over the decompile.

| Capability | Status | Evidence | Confidence |
|---|---|---|---|
| BF16→F32 all-reduce **accumulation promotion** | **PRESENT** | `MayIncreaseBF16AllReduceAccumulationAccuracy` `0x127a22c0`; wrapper `$_0` `0x127a4340` (clone + `set_element_type` + `CreateConvert` BF16↔F32) | Confirmed |
| On-wire BF16-vs-F32 reduction toggle | **PRESENT** | `AllReduceEmitter::bf16_inside_cross_replica_sum` `0x1373ca60` (flag `xla_jf_bf16_inside_cross_replica_sum`) | Confirmed |
| 8-bit on-wire **quantized all-reduce** | **PRESENT** | `RotatedPincerQuantizedEmitter` (full method surface); `TpuQuantizedAllReduceBackendConfigSetter::RunImpl` `0x11107b00`; `QuantizedAllReduceConfig` proto | Confirmed |
| Quantize formats `S8`, `F8E5M2`, `F8E4M3B11FNUZ` | **PRESENT** | `pincer_utils::UpdateScale` switch cases `{2, 0x13, 0x17}`; setter dtype match `{2, 19, 23}` | Confirmed |
| Symmetric absmax scale (`qmax/absmax`) | **PRESENT** | `UpdateScale` `0x137b75c0`; `.rodata` qmax `{127.0, 57344.0, 30.0}` | Confirmed |
| F32 in-flight reduction (8-bit = wire only) | **PRESENT** | `ReduceSymmetricallyQuantized8BitShardInPlace` `0x137b8880` (dequant→F32 merge→re-absmax) | Confirmed |
| Quantize format **`F8E4M3Fn`** (PrimitiveType 20) | **ABSENT** | not in `UpdateScale` switch nor the setter dtype match (only `{2,19,23}`) | Confirmed |
| **Zero-point** / asymmetric collective quant | **ABSENT** | device quantizer is symmetric (scale only); no zero-point field in `pincer_utils` | Confirmed |
| Quantized **all-gather** | **ABSENT** | no `Quantized*Gather` symbol (only `QuantizedAllReduce*`) | Confirmed |
| Quantized **all-to-all** / **reduce-scatter** | **ABSENT** | no `Quantized{AllToAll,ReduceScatter}` symbol | Confirmed |
| Quantized AR on `TpuVersion ∉ {3,4}` | **ABSENT (gated off)** | `CanLowerToQuantizedAllReduce` `0x13798420`: `TpuVersion - 3 < 2` | Confirmed |

> **NOTE — what "fp8 quantized collective" does and does not mean here.** A reader expecting a general "fp8 compress every collective" facility will not find it: the only collective with an 8-bit-on-wire path is **`all-reduce`**, via the rotated-pincer family. All-gather, reduce-scatter, and all-to-all move data uncompressed. The `F8E4M3Fn` format — the more common fp8 variant elsewhere on the TPU convert surface — is **not** an admissible collective-quantize type; the collective quantizer admits only the `B11FNUZ` fp8 variant plus `F8E5M2` and `S8`. And the quantizer is **symmetric only** — no zero-point — so it is unsuitable for asymmetric/unsigned activation distributions.

> **NOTE —** the `QuantizedAllReduceStage` enum (values `1/2/3`) almost certainly distinguishes a multi-pass quantized reduce (e.g. reduce-scatter phase vs all-gather phase vs combined), matching the separable-arm structure of the [pincer fusion](allreduce-hierarchical-pincer.md), but the per-stage emitter divergence was not unwound (LOW). The `xla_tpu_combine_quantized_all_reduce_operands` flag (an operand-batching knob, default-gen present) was confirmed to exist but its combine logic was not traced (LOW).

---

## 4. How the two precision mechanisms relate

The two mechanisms sit at opposite ends of the accuracy/bandwidth trade and are *mutually exclusive in intent*, though nothing in the binary forbids both flags being set:

```text
operand (BF16)
   │
   ├── xla_tpu_spmd_f32_accum_for_bf16_ar (SPMD, §1)
   │        convert BF16->F32 ─► all-reduce in F32 ─► convert F32->BF16
   │        (MORE accurate, 2x wire bytes; subgroup-size gated)
   │
   └── xla_tpu_quantized_all_reduce_level (backend-config, §2)
            quantize BF16->8bit ─► pincer ring (F32 merge per step) ─► dequant 8bit->BF16
            (LESS bandwidth, lossy; TpuVersion {3,4} + shape + size gated)
```

The accumulation promotion is the SPMD partitioner's lever for *correctness-sensitive* BF16 reductions (it is chosen during sharding, where `GetCommunicationTimeInMilliSec` accounts for the doubled payload — see [SPMD Link-Count Cost](spmd-link-count-cost.md)). The quantized path is the backend's lever for *bandwidth-bound large* reductions on the newest hardware. A reimplementer must keep them in separate passes: the F32 promotion is an HLO graph rewrite emitted before lowering; the quantization is a backend-config tag consumed by the TensorCore lowering emitter.

---

## Cross-References

- [Collectives Overview](overview.md) — the collective family taxonomy, the substrate split, and the `TpuVersion` internal enum.
- [AllReduce Hierarchical / Pincer](allreduce-hierarchical-pincer.md) — the bidirectional pincer family that `RotatedPincerQuantizedEmitter` specializes; the separable reduce-scatter / all-gather arms.
- [Binomial / Recursive-Doubling](binomial-recursive-doubling.md) — the latency-bound all-reduce emitter (the non-pincer topology, not quantizable in this build).
- [SPMD Link-Count Cost](spmd-link-count-cost.md) — `GetCommunicationMultiplier` and the per-kind cost the SPMD partitioner weighs when choosing the F32-accumulation rewrite.
- [Auto-Sharding / SPMD](../compiler/auto-sharding-spmd.md) — the `TpuSpmdPartitioner::AllReduceAlongShardingDims` override and the `MayIncreaseBF16AllReduceAccumulationAccuracy` gate that this page expands.
- [back to index](../index.md)
