# PE Engine — the Systolic Matmul Array

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). Functions live in `libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset), `libBIR.so` (the `bir::` IR + enums), and `libBIRSimulator.so` (the `birsim::` reference model). Other wheels differ; treat every address as version-pinned.*

## Abstract

The **PE (Processing Element) engine** is Trainium's tensor core: a **128×128 weight-stationary systolic array** — the same dataflow class as a Google TPU MXU. Where this engine diverges from a textbook MXU is in three places a reimplementer must get right: (1) the matmul is **not one instruction** but a *two-phase* `LoadStationary`-then-`MatMul` pair, with a compiler-side **weight cache** that elides the load when the same weight tile is reused; (2) accumulation across the contraction (`K`) dimension is steered by a **three-bit calc flag** — START zeroes the PSUM bank, ACCUMULATE adds, STOP drains — set by an upstream legalizer, not by the matmul instruction itself; and (3) the modern **MX path** (FP4/FP8 microscaling, gen4 only) abandons the weight cache and the perf-mode machinery entirely, threading a paired E8M0 block-scale stream through a new descriptor.

The substrate is fixed: 128 *partitions* (the array rows, carrying the contraction `K`) by 128 *columns* (the output `N`), tiled internally as four 32-lane **quadrants** per axis. That geometry is **identical across all three generations** — gen2 (`CoreV2`), gen3 (`CoreV3`), gen4 (`CoreV4`). MX is a new *data format* (4-narrow elements + per-block scale) on the same array, not a wider array. The accumulator is **always fp32 in PSUM**, regardless of whether the inputs are bf16, fp16, fp8, or fp4 — the reference model widens every operand to float before the multiply-accumulate.

This page is Part 1 (the functional/architectural model): the datapath, weight caching, perf-mode packing, and accumulation semantics — enough to map a tiled matmul onto the array. It does **not** cover the bit-level field encoding of the 64-byte instruction bundles; that is [ISA: PE matmul encoding](../isa/pe-matmul-encoding.md) (2.10). It anchors to the dense (`InstMatmult`) path first, then shows where sparse and MX diverge.

For reimplementation, the contract is:

- **The two-phase protocol** — every matmul lowers to a weight-load bundle (`LoadStationary`/`LdWeightMx`) followed by a compute bundle (`MatMul`/`MatmultMx`), modelling "latch the stationary tile, then stream the moving tile through it."
- **The `WeightTileState` cache** — keyed on the PE tile-group, the encoder skips the weight-load when the array already holds the right tile; this is the compiler-visible face of "load weights once, multiply many."
- **The perf modes** (`MatmultPerfMode`) — how DoubleRow folds two contraction rows into one systolic pass when the element is narrower than the array cell, and why MX has no perf mode at all.
- **The accumulate chain** — the START/STOP/ACCUMULATE flag triad that turns a sequence of matmuls into a single `K`-dimension reduction into one fp32 PSUM bank.
- **The MX dispatch divergence** — no weight cache, no two-pass, no perf byte, a dual-address (data + E8M0 scale) descriptor, and a raw pre-packed accumulate byte.

| | |
|---|---|
| **Engine** | `EngineType::PE` (= 3); `getDefaultEngine` @ `libBIR 0x3e2cb0` → 3 for all three matmul ops |
| **Array** | 128 partitions (rows = `K`) × 128 columns (`N`), four 32-lane quadrants per axis |
| **Dense orchestrator** | `CoreV2GenImpl::visitInstMatmult` @ `0x126a850`; `CoreV3GenImpl::visitInstMatmult` @ `0x1368750` |
| **Weight cache** | `WeightTileState` `std::_Rb_tree` keyed on `pair<u8,u8>` (row,col group); CoreV2 `this+704`, CoreV3 `this+840` |
| **Phase opcodes** | dense `1` LoadStationary + `2` MatMul; sparse `6` LoadTags + `1` + `7`; MX `0x1009` LdWeightMx + `0x100A` MatmultMx |
| **Perf-mode enum** | `MatmultPerfMode` @ `libBIR 0x400be0` — `{0 None, 1 DoubleRow, 2 DoubleColumn, 3 DoublePixel, 4 DoubleRowSwInterleave}`; field at `InstMatmultBase+0x2D0` |
| **Accumulate flag** | bundle `+0x2B`: bit0 START (zero), bit1 STOP (drain), bit2 ACCUMULATE (add) — set upstream by `legalize_mm_accumulation_groups` |
| **Accumulator** | fp32 PSUM, always (operands widened to fp32 before the MAC); `PSUMLegalization::widen_psum` |
| **Reference model** | `birsim::InstVisitor::visitInstMatmult` @ `0x3fd74b0`; `matmultImpl<InstMatmult>` @ `0x2883a0` |

---

## The Two-Phase Protocol — Load Stationary, then Stream Moving

### Purpose

A systolic array computes `out = ifmapᵀ · weights` by holding one operand **stationary** in the cell latches while the other **moves** through, draining partial sums to an accumulator. The PE engine exposes this as two distinct instructions, and the BIR `InstMatmult` (one logical matmul) **lowers to a pair**:

```text
phase 1 — LoadStationary : stage the WEIGHT tile into the 128×128 cell latches
phase 2 — MatMul         : stream the MOVING ifmap through the latched array,
                           multiply-accumulate, drain to a PSUM bank
```

This is the same "load weights, then push activations" contract as a TPU MXU. The divergence is that the *compiler* owns the decision of whether phase 1 is actually emitted — the array's weight latches are stateful across instructions, so a second matmul that reuses the same weight tile can skip straight to phase 2.

### Operand Roles

Three operands, bound **positionally** by `getArgument`/`getOutput` on the dense path:

| Role | What it is | Argument | Bundle slot (MatMul) | Confidence |
|---|---|---|---|---|
| **Stationary** (weights) | latched into the 128×128 array | `getArgument(1)` | *none* — implicit, already latched | CERTAIN |
| **Moving** (ifmap) | streamed through the array | `getArgument(0)` | `+0x10` (SB-resident AP) | CERTAIN |
| **PSUM** (dst) | fp32 accumulator output | `getOutput(0)` | `+0x30` (PSUM AP) | CERTAIN |

> **QUIRK — the stationary weights have no operand slot in the MatMul bundle.** They were loaded by the preceding `LoadStationary` and physically reside in the array's latches; the `MatMul` bundle carries only the moving ifmap and the PSUM destination, plus the weights' *dtype*, base-partition, and tile-group (so the array knows which quadrant the latched tile occupies). A reimplementer who tries to find a weights address in the compute bundle will not find one — the data dependence is carried by hardware state, and the compiler must therefore never reorder a `MatMul` ahead of its `LoadStationary` (the cache logic below enforces this).

### Partition Geometry

The 128 partitions are the **contraction axis `K`** (array rows); the 128 columns are the **output `N`**. The partition axis is `Pattern[0]` — the outermost dimension of an access pattern — and is special: it is **never** emitted as a `{step, num}` loop word. Instead it folds into the start-address as a base-partition term (`SB: part<<18`, `PSUM: part<<15`), and the free-dimension loop iterates `Pattern[1..3]` only. The contraction extent per partition is `getNumElementsPerPartition`.

The array is tiled as **four 32-partition quadrants per axis** (`quadrant = partition >> 5`). A matmul's *tile group* is a bitmask telling the array which quadrant(s) the tile occupies — the same `{1,2,4,8}` quadrant LUT for all three generations (see [The PE Tile-Group Model](#the-pe-tile-group-model)).

### Algorithm

```c
// CoreV2GenImpl::visitInstMatmult @ 0x126a850  (gen3 sibling @ 0x1368750)
function visitInstMatmult(InstMatmult &I):
    perf   = I.perf_mode                          // InstMatmultBase+0x2D0
    group  = (translateToRowGroup(...), translateToColGroup(...))  // pair<u8,u8> key

    // ---- fp32 path: a two-pass, cache-invalidating 2-FMA lowering ----
    if I.dtype == 16 /*float32*/:                 // cmp [r15+0x30], 0x10 @ 0x126bab0
        generateLoadWeights(I, perf, ...)         //   pass 1: stage weights
        generateMatMul(I, perf, /*twoPass*/1, /*second*/0, ...)   //  START-zeroes
        generateLoadWeights(I, perf, ...)         //   pass 2: re-stage
        generateMatMul(I, perf, /*twoPass*/1, /*second*/1, ...)   //  STOP-drains
        invalidateWeightTileCache()               //   jmp @ 0x126bf5e — never cache fp32
        return

    // ---- normal path: consult the WeightTileState cache ----
    if cache_hit(group, I.weights_AP, calc_perf_signature, no_intervening_writer):
        generateMatMul(I, perf, ...)              // HIT: emit MatMul ONLY (weights warm)
    else:                                          // MISS or hasTilingConfigChanged
        generateLoadWeights(I, perf, ...)         // stage the tile (op 1)
        generateMatMul(I, perf, ...)              // then compute (op 2)
        cache_insert(group, ...)                  // _Rb_tree _M_get_insert_hint_unique_pos
```

> **GOTCHA — the fp32 matmul is a two-pass sequence, and the simulator does not model it as such.** The encoder lowers a `float32` matmul to `LoadWeights → MatMul(START) → LoadWeights → MatMul(STOP) → invalidateWeightTileCache` — the four call sites plus the tail `jmp invalidateWeightTileCache` at `0x126bf5e`. The two emitted `MatMul`s split the calc flags so pass 1 zeroes the bank and pass 2 drains it. This is the hardware's way of doing a full-precision fp32 product on a bf16-native array (a two-FMA decomposition); the cache is force-invalidated so the second weight load is never elided. The **reference model**, however, treats `float32r` (Dtype 17) as a single-pass *reduced-precision* operand round (`sub_2A4750` round/truncate, then one fp32 Eigen product) — it does **not** simulate a hi/low FMA correction. A reimplementer of the *encoder* must emit two passes; a reimplementer of the *simulator* models one rounded pass. Do not assume the two agree numerically on the low bits.

### The WeightTileState Cache

The dense orchestrator holds a `std::map<pair<u8,u8>, WeightTileState>` — backed by a `std::_Rb_tree`, per the demangled `_Rb_tree<…WeightTileState…>::_M_get_insert_hint_unique_pos` call at `0x126c098` — at `this+704` (CoreV2) / `this+840` (CoreV3). The key is the `(rowGroup, colGroup)` tile-group byte pair.

| Cache aspect | Behavior | Confidence |
|---|---|---|
| **Key** | `pair<u8,u8>` = `(rowGroup, colGroup)` of the stationary tile | CERTAIN (`_Rb_tree` type) |
| **Match conditions** | same group **and** same weights AP **and** same calc/perf signature **and** no intervening writer to the weights memory | HIGH |
| **HIT** | skip `LoadStationary`, emit `MatMul` only — "weights still warm in the array" | CERTAIN |
| **MISS / tiling change** | `hasTilingConfigChanged` → emit **both** `LoadStationary` + `MatMul`, then insert | CERTAIN |
| **Invalidation** | `invalidateWeightTileCache` on fp32 (always) and on tiling reconfiguration | CERTAIN |

This cache is the compiler-visible face of weight-stationary reuse: when a model multiplies many activation tiles against the **same** weight tile (the common case — one weight matrix, a stream of token tiles), the encoder loads the weights *once* and emits a run of bare `MatMul`s. A reimplementer who emits a `LoadStationary` before every `MatMul` produces correct but ~2× slower code; the cache is a throughput optimization, not a correctness requirement.

---

## The Perf Modes — Folding the Array 2:1

### Purpose

When the element width is narrower than the array's native cell width, an axis of the array can be **packed 2:1** to raise utilization — two logical elements share one systolic pass. `MatmultPerfMode` (`libBIR 0x400be0`, with a byte-exact `MatmultPerfMode2string` switch and an inverse `string2MatmultPerfMode`) selects which axis is folded. The field rides the IR node at `InstMatmultBase+0x2D0` (wire JSON key `"perf_mode"`).

| Value | Name | Folds 2:1 | Confidence |
|---|---|---|---|
| `0` | `None` | nothing — standard 1-pass matmul (baseline) | CERTAIN |
| `1` | `DoubleRow` | two **contraction rows** (partitions / `K`) per pass → ~2× on `K` | CERTAIN name; HIGH behavior |
| `2` | `DoubleColumn` | two **output columns** (PSUM cols) per pass → ~2× on `N` | CERTAIN name; MEDIUM wire bytes |
| `3` | `DoublePixel` | two **free-axis pixels** (moving-fmap elements) per pass → ~2× free | CERTAIN name; MEDIUM wire bytes |
| `4` | `DoubleRowSwInterleave` | `DoubleRow` with SW-interleaved row pairs (when the AP stride cannot satisfy the HW even-pairing) | CERTAIN name; HIGH behavior |

The enum names are read directly from the `MatmultPerfMode2string` jump table (`aNone`/`aDoublerow`/`aDoublecolumn`/`aDoublepixel`/`aDoublerowswinterleave` at `0x400c00..0x400c60`, default `"Unknown MatmultPerfMode"`).

### Algorithm

The only perf mode byte-proven in an encoder is `DoubleRow`. It manifests as a `scale = 2` factor:

```c
// CoreV2GenImpl::generateMatMul @ 0x1248650
scale = (I.perf_mode == DoubleRow) ? 2 : 1          // cmp [rax+0x2D0], 1  @ 0x12488c7

// the scale drives two derived quantities:
num_active_cols = getNumElementsPerPartition(out) / scale   // bundle +0x27
assert out.partitions * scale == weights.elemsPerPartition   // K-fold consistency

// DoubleRow AP halving (CoreV2): force the ifmap's first free dim to 2, then
//   halve the moving num and the K extent (mirrored on the dst AP), so two
//   K-rows are physically interleaved into one systolic pass.
```

> **NOTE — gen2 and gen3 express DoubleRow differently.** CoreV2 (`generateMatMul` @ `0x1248650`) halves the access-pattern counts directly (`num >>= 1`). CoreV3 (`generateMatMul` @ `0x13643d0`) instead performs an **AP dimension-swap** and forces the first free dim to 2, sharing the `DoubleRowSwInterleave` (value 4) path with `DoubleRow`. The *wire bytes* differ; the *silicon effect* — two contraction rows per pass — is the same. The reference model confirms the K-fold: for `perf ∈ {1, 4}` it doubles the contraction extent (`Ki *= 2; Pi *= 2`) before the Eigen product, in the `matmultImpl` perf branch.

> **GOTCHA — `DoubleColumn` and `DoublePixel` have no byte-proven encoding.** Their selector values are validated (the `sub_1203630` perf-format validator throws on out-of-range), but no encoder in this build was observed emitting their *distinct* bundle bytes — only `None` and `DoubleRow` are exercised on the paths that reach silicon. Their `+0x21`/`+0x23` perf bytes are **INFERRED** to ride the same fields as `DoubleRow`. A reimplementer targeting only the modes the compiler actually emits can treat `{None, DoubleRow, DoubleRowSwInterleave}` as the live set and re-derive `DoubleColumn`/`DoublePixel` if needed.

---

## The Accumulate Chain — START / STOP / ACCUMULATE into fp32 PSUM

### Purpose

A real matmul almost never fits one array pass: the contraction `K` exceeds 128, or one logical matmul is tiled into many array-sized sub-matmuls. These are reduced into **one PSUM bank** by an **accumulation group** — a sequence of matmuls whose partial sums add. The group is formed and flagged by the pass `legalize_mm_accumulation_groups` (registry order 94); the encoder merely *reads* the flags it set.

```text
   matmul[0]  →  START      : zero the PSUM bank, then write   (head of the K-reduction)
   matmul[1]  →  ACCUMULATE : add into the bank                (interior)
   ...
   matmul[n]  →  STOP       : add into the bank, then drain it (tail; result is read out)
```

This maps cleanly onto the familiar "accumulator initialize / accumulate / read-out" pattern — but here the initialize and read-out are *flags on the matmul instructions themselves*, not separate ops.

### The Flag Byte

The triad is a **3-bit field at bundle `+0x2B`** (byte 43), built by OR-ing bits as the decoded `getCalcStart`/`getCalcStop`/`getCalcAccu` booleans come back true:

| Bit | Value | Calc flag | Meaning | Evidence |
|---|---|---|---|---|
| 0 | `0x01` | START | zero-then-write the PSUM bank | `or [r15+0x2B], 1` @ `0x124963d` (CoreV2), `or [rbx+0x2B], 1` @ `0x1428a60` (CoreV3) |
| 1 | `0x02` | STOP | drain / read the bank out | `or [r15+0x2B], 2` @ `0x1249664` (CoreV2), `or [rbx+0x2B], 2` @ `0x1428a30` (CoreV3) |
| 2 | `0x04` | ACCUMULATE | add into the bank (interior matmul) | `or [rbx+0x2B], 4` @ `0x14288b5` (CoreV3) |

```c
// CoreV2GenImpl::generateMatMul @ 0x1248650 (inline) — the canonical byte build
bundle[0x2B] = 0                              // init (memset / mov byte [r15+0x2B],0)
if I.getCalcStart() && !two_pass_predicate:  bundle[0x2B] |= 1   // START
if I.getCalcStop()  && ...:                  bundle[0x2B] |= 2   // STOP
// CoreV3 generateMatMulAccumulateFlag<S3D3_MM_STRUCT> @ 0x1428630 adds:
if I.getCalcAccu():                          bundle[0x2B] |= 4   // ACCUMULATE
```

> **QUIRK — gen3 arch-gates the accumulate byte; gen2 does not.** `CoreV3GenImpl::generateMatMulAccumulateFlag` @ `0x1428630` opens with `cmp [module+0xAC], 0x28; jg …` — if the module's arch level (`+0xAC` = 172) is **greater than 40** it returns *without touching* `+0x2B` (the accumulate byte is omitted entirely). CoreV2 has no such gate — it always writes the byte. A reimplementer who unconditionally writes the calc byte for every architecture will produce wrong instructions on the gen3 arch-40+ path, where the accumulate window is managed differently, through the explicit-memset path described below.

### The fp32 PSUM Accumulator

The accumulator is **fp32 in PSUM regardless of input dtype**. The reference model widens *every* operand to float before the multiply-accumulate, in `matmultImpl`:

```c
cast_to(ifmapObj,   Dtype 16 /*fp32*/)   // bir::CastToNewDType @ 0x28db30
cast_to(weightsObj, Dtype 16 /*fp32*/)
// ... build Eigen::Map<Matrix<float,RowMajor>> over the fp32 buffers ...
out[p_out, n] = Σ_k (ifmap[k,n] - zpI) * (weights[k,p_out] - zpW)   // in fp32
```

On the codegen side, `PSUMLegalization::widen_psum` / `widen_memset` rewrite any sub-4-byte matmul destination to a 4-byte (fp32) PSUM cell. This fp32 accumulation is **hardware-fixed** and **independent** of `--enable-mixed-precision-accumulation` — that flag governs only the *Activation/Pool* engine ALU reduce/BN accumulators, never the PE array.

> **NOTE — keep two accumulator vocabularies apart.** The PE `Calc{Start,Stop,Accu}` triad above is the PSUM-bank command. The separate `bir` enum `EngineAccumulationType` `{Idle, ZeroAccumulate, AddAccumulate, LoadAccumulate}` belongs to the Activation/DVE engine accumulator path (`visitInstReadActivationAccumulator`), a different axis. A reimplementer should not conflate the two; only the `+0x2B` triad steers the PE PSUM.

### The Reference-Model Writeback

The simulator decodes the flags into the two booleans handed to `Memory::writeAccumulate` (vtable+88), confirming the silicon contract:

```c
// birsim doMatmult<InstMatmult> @ 0x289450
startReset = I.getCalcStart()        // true → resetAccumulation (memset the bank to 0)
accInto    = !I.getCalcStart()       // true → += into the existing PSUM region
Memory.writeAccumulate(dstAP, result, startReset, I, accInto)
// resetAccumulation @ 0x2931f0 : per-partition memset(dst, fill, partBytes)
// the += path (runAP @ 0x298390) is taken iff writer isa<InstMatmultBase> && dst.isPSUM
```

> **GOTCHA — PSUM accumulation does not span banks.** The simulator carries the guard string `do not support psum multibank`; the accumulation-group legalizer keeps each group's zero-region inside one pow2-aligned bank range and clusters overlapping groups (`find_overlapped_psum_zero_region_groups` @ `0x16dee50`) so a single START cannot mis-serve two interleaved chains. A reimplementer must legalize an accumulation group to fit one PSUM bank before mapping it onto the array.

---

## The MX Dispatch Path — No Cache, No Two-Pass, Paired Scale

### Purpose

The gen4 (`CoreV4`) **MX (microscaling)** path multiplies **4-narrow** elements — FP4 (`e2m1`) or FP8 (`e4m3`/`e5m2`) packed four-per-container-lane — each carrying a **per-block E8M0 scale** (OCP-MXFP, block size 32 = 8 partitions × 4 columns). It runs on the *same* 128×128 array; MX is a data-format path, not a wider array. Its lowering is radically simpler than the dense path.

### Algorithm

```c
// CoreV4GenImpl::visitInstMatmultMx @ 0x143f410 — a NAKED 15-instruction dispatcher
function visitInstMatmultMx(InstMatmultMx &I):
    generateLdweightMx(I)        // op 0x1009  — call @ 0x143f41d
    generateMatmultMx(I)         // op 0x100A  — tail-jmp @ 0x143f42f
    // NO WeightTileState cache. NO fp32 two-pass. NO perf-mode branch. Always both.
```

> **QUIRK — MX always emits both bundles; there is nothing to cache.** Unlike the dense orchestrator's branchy ~700-instruction body, `visitInstMatmultMx` is a 15-line `call generateLdweightMx; jmp generateMatmultMx` — that is the entire body. MX matmul is always a single full-tile compute (no column tiling, no transpose, no START/STOP chaining across passes), so there is no reuse window to cache and no two-pass to split. A reimplementer should not add a weight cache to the MX path — it would be dead code.

### Operand Binding and the Paired Scale

MX uses **six** access patterns, with the two E8M0 scale streams **split across the two bundles**:

| Bundle | Operand | Argument | Carries | Confidence |
|---|---|---|---|---|
| `LdWeightMx` | weights ×4 DATA | `arg1` | weight data | CERTAIN |
| `LdWeightMx` | weights E8M0 SCALE | `arg3` | stationary scale | CERTAIN |
| `MatmultMx` | moving ×4 DATA | `arg0` | ifmap data | CERTAIN |
| `MatmultMx` | weights (K-rows) | `arg1` | active-partition count only | CERTAIN |
| `MatmultMx` | ifmap E8M0 SCALE | `arg2` | moving scale | CERTAIN |
| `MatmultMx` | PSUM dst | `out0` | fp32 accumulator | CERTAIN |

> **QUIRK — each PE input carries its own block-scale, bound as a *second address inside one descriptor*.** Where the dense path uses a `TENSOR3D` access pattern with a single start address, the MX inputs use an `MXMEM_PATTERN1D` descriptor that holds **two** ADDR4 addresses — the data and its E8M0 scale — as a pair (`assignAccessForMX` @ `0x150e2f0`: `assignStartAddr<ADDR4>(scaleFlag=0)` for data, `(scaleFlag=1)` for scale). This dual-address descriptor is the structural MX divergence; the PSUM destination, by contrast, stays a plain 3D pattern at `+0x30`. The descriptor's **logical K is ×4-unpacked**: `if (dt==2 || (unsigned)(dt-8)<=1) K *= 4` (`cmp edx,2; sub edx,8; shl eax,2; mov [rcx+8],ax` @ `0x150e603`), so a 128-partition tile spans up to `128×4 = 512` logical contraction elements.

### The MX Accumulate Byte

MX does **not** recompute the calc triad — it copies the already-bit-packed accumulation flag straight from the IR node:

```c
// CoreV4GenImpl::generateMatmultMx @ 0x143ebd0
bundle[0x2B] = inst[0xF0]              // RAW pre-packed accumulate byte — mov [r13+0x2B],al @ 0x143ef1d
bundle[0x2F] = inst[0x118]             // psum_zero_region (the bank window START clears) @ 0x143ef3b
bundle[0x2D] = 0x0F                    // COL group FORCED full — mov byte [r13+0x2D],0x0F @ 0x143eec4
// checkAccumulationFlag @ 0x150db50 validates the byte ∈ {0,1,2,3,4,6}
```

> **NOTE — same bit contract, different provenance.** The MX accumulate byte uses the identical `{bit0 START, bit1 STOP, bit2 ACCUMULATE}` semantics, but it is packed upstream by the accumulation-group legalizer and copied verbatim — no `getCalcStart/Stop/Accu` recompute, and **no arch-gate** (MX lives at exactly arch level 40, where the byte is always meaningful). MX also forces the column group to full (`0x0F`) because, per the verifier, the MX column is **never tiled** (`col_tile_pos ∈ {-1,0}`, `col_tile_size ∈ {-1,128}`).

---

## The PE Tile-Group Model

### Purpose

A tile smaller than the full array occupies one or more of the four 32-lane quadrants. The **tile group** is a bitmask the array uses to know which quadrant(s) the stationary tile spans. The mapping is **a single LUT, unchanged across all three generations**:

```text
rowGroupLUT == colGroupLUT @ 0x1DD33F0  =  01 00 00 00  02 00 00 00  04 00 00 00  08 00 00 00
                                            → LUT[4*idx] = {1, 2, 4, 8}   (idx = pos >> 5)
```

### Algorithm

```c
// backend::translateToRowGroup(pos, size) @ 0x1089e70   (col variant @ 0x1089ec0)
idx = pos >> 5                                  // which 32-lane quadrant
if size <= 32:  return LUT[4*idx]               // {1,2,4,8} = a single quadrant
if size <= 64:  return {3, 12}                  // a two-quadrant span
else:           return 15                        // whole axis
// translateToColGroup(pos, size, force): force → 15 (full col; fp32r / transpose / MX)
```

| Group value | Meaning |
|---|---|
| `1, 2, 4, 8` | one specific 32-lane quadrant |
| `3, 12` | a two-quadrant span (64 lanes) |
| `15` (or `16` normalized) | the whole 128-lane axis |

> **NOTE — the group bytes moved between generations, but the geometry did not.** CoreV2 stores row at `+0x24`, col at `+0x25`, and a normalized col at `+0x2E`. CoreV3 packs `(col<<8 | row)` into one word at `+0x2C` and drops the normalization. CoreV4 **reuses CoreV3's `translateToRowGroup` by symbol** (row at `+0x2C`, col forced `0x0F` at `+0x2D`). The LUT and the four-quadrant model are byte-identical across all three — confirming the array itself is the same silicon; only the data-format and bundle-layout evolve.

---

## Shared vs. Diverged

| Facet | CoreV2 (gen2) | CoreV3 (gen3) | CoreV4 MX (gen4) |
|---|---|---|---|
| Array geometry | 128×128, 4×32 quadrants | same | same |
| Group LUT `{1,2,4,8}` | `@0x1DD33F0` | same | same (symbol-reused) |
| Phase opcodes | `1` + `2` | `1` + `2` (+ sparse `6`/`7`) | `0x1009` + `0x100A` |
| Weight cache | `this+704` | `this+840` | **none** |
| Two-pass fp32 | yes (+ invalidate) | yes | **no** |
| Perf modes | `None`/`DoubleRow` (count halve) | `None`/`DoubleRow` (AP dim-swap) + bg-transpose | **none** (implicit in ×4 geometry) |
| Accumulate byte | inline, always | `generateMatMulAccumulateFlag`, **arch>40-gated** | raw `inst+0xF0` copy, no gate |
| Operand descriptor | `TENSOR3D` (single addr) | `TENSOR3D` | `MXMEM_PATTERN1D` (data + E8M0 scale) |
| fp4 dtype wiretag | `0x05` | `0x05` | `0x10` |

The single dtype-LUT delta across generations is index 2 (fp4 `e2m1_x4`): the CoreV2/CoreV3 table `byte_1DF5760` maps it to `0x05` (colliding with `uint16`), while the CoreV4 MX table `byte_1DFBAD0` gives it its own MX-era tag `0x10`. All other 19 entries are identical; both 20-byte LUTs were dumped from rodata.

## Limits of this reading

Three things this page describes are not settled by the binaries.

The **distinct wire encoding for `DoubleColumn` and `DoublePixel`** is **INFERRED**. Only `None` and `DoubleRow` are byte-proven in any encoder reached in this build; the other modes' selectors are validated, but their bundle bytes are assumed to share the `DoubleRow` fields.

The **fp32 two-pass numerics** are asserted by the encoder and contradicted by the reference model. The encoder's two-pass emission is plainly visible; the simulator treats `float32r` as a single rounded pass and offers no corroboration of a hi/low FMA split.

The **MX indirect gather descriptor** (`MXINDIRECT16B`) was not field-walked. The static `MXMEM_PATTERN1D` path is fully read; the gather variant at `assignIndirectPatternForMX` @ `0x150de90`, reached only for `TensorIndirect` MX data, is not.

## Cross-References

- [Arch Object Model](arch-object-model.md) — the `EngineType`/arch-level model the matmul ops bind to (`getDefaultEngine → PE`, arch `+0xAC` gate).
- [ISA: PE Matmul Encoding](../isa/pe-matmul-encoding.md) — the bit-level 64-byte bundle layout this page describes functionally (opcodes, field offsets, ADDR4 region math).
- [BIR Instruction Hierarchy](../bir/) — `InstMatmult` / `InstMatmultSparse` / `InstMatmultMx` as `bir::Instruction` nodes, and the `getCalc*` accessors.
- [MX Microscaling & fp32-PSUM Accumulation](../numerics/mx-microscaling.md) — the E8M0 block-scale format, the ×4 unpack, and why PSUM is always fp32.
- [NKI Matmul Kernels](../nki/) — how a tiled matmul kernel maps onto this two-phase array from the NKI front door.
