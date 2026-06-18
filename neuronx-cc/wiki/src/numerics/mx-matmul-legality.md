# MX-Matmul Legality Contract & Dequant

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). The two BIR verifiers live in `neuronxcc/starfish/lib/libwalrus.so` (`.text`/`.rodata` VA == file-offset); the simulator dequant kernel and the EMAX/byte-size tables live in `neuronxcc/starfish/lib/libBIRSimulator.so` (`.text` `0xf1ad0..0x58e798` and `.rodata` `0x58f000..` are VA == file-offset; `.data` carries a `0x1000` delta). Function bodies have **two addresses**: the nm-body VA (`checkMatmultMxInputs` `0x1007420`) and the per-symbol sidecar / ICF-thunk VA (`0x5fb140`, which `jmp`s through GOT slot `off_3DD3088`). Both are real; this page cites the body VA. Other wheels differ; treat every address as version-pinned.*

## Abstract

An **MX matmul** (`bir::InstMatmultMx`, opcode 95) is the Trainium PE-array primitive that multiplies two **microscaled** operands — FP4 or FP8 mantissas packed four-to-a-lane, each 32-element block sharing one **E8M0** (8-bit, exponent-only) scale byte. Before such an instruction can be encoded into a `LdWeightMx`/`MatmultMx` bundle pair, it must pass **two** legality verifiers in `libwalrus.so`: `checkMatmultMxInputs` (`0x1007420`) enforces the **shape / numeric** contract — 11 asserts on per-partition element counts, the data-vs-scale ×4 ratio, scale start-partition alignment, the PSUM-bank capacity bound, the hard `K ≤ 512` bound, even-block packing, and partition-count agreement — and `checkMatmultMxInstruction` (`0x10139a0`) enforces the **instruction-level** contract — 7 asserts pinning the engine to PE, forcing data and its scale into the same 32-partition SBUF quadrant, requiring 4-partition scale alignment, and forbidding any column tiling, any row sub-tiling, and any weight-replication shift. At runtime the silicon (and the BIR simulator's `dequantizeMx`, `0x27b040`) recovers the true value with one operation per element: `ldexpf(mantissa, E8M0 − 127)` — apply the block's shared exponent as a power of two, with the E8M0 bias (127) removed. The format reference each scale is measured against is the per-format **EMAX** table (`byte_5F7898`, `0x5F7898`): `{fp4_e2m1 → 2, fp8_e4m3 → 8, fp8_e5m2 → 15}`.

This page is the **numeric contract a reimplementer must satisfy to emit a legal MX matmul**. It names every assert, its exact predicate, its numeric bound, the `NeuronAssertion` error code it throws, and the source line; reproduces the dequant formula and the EMAX table as annotated pseudocode against the recovered symbols; and reconciles a partition-set discrepancy (the set is `{32,64,128}`, **not** `{32,64,96,128}`) between two prior decodes. Backing: **D-G05** (verifier line-by-line) and **D-F02** (simulator per-opcode kernels), both re-verified here against the decompiled bodies and `.rodata` tables.

If you want the *encoding* of the bundle pair these asserts gate, see [PE Matmul Encoding](../isa/pe-matmul-encoding.md) (2.10) and [codegen: Matmul + MX](../bir/codegen-matmul-mx.md). For the *quantize* (forward) direction that produces the E8M0 scales, see [MX-FP8 Microscaling Legalization](../hlo-opt/mx-fp8-legalization.md) and the `quantize_mx` / microscaling page (9.8). For the full simulator numeric model, see [Sim Matmul / MX Quantize / Dequantize](../bir/sim-matmul-mx.md). The verifier family overview is [birverifier Per-Op Verification](../walrus/birverifier-per-op.md) (Part 8).

| | |
|---|---|
| **Instruction** | `bir::InstMatmultMx`, opcode **95** (`0x5F`); ISA bundle `LdWeightMx 0x1009` + `MatmultMx 0x100A` |
| **Operands** | 6 access patterns: `arg0` ifmap data · `arg1` weights data · `arg2` ifmap scales · `arg3` weights scales · `out0` PSUM result |
| **Operand dtypes** | data = FP4/FP8 ×4-packed (`{2, 8, 9}`); scales = E8M0 `uint8` (`float8_e8m0fnu`, dtype 6); output = fp32 / bf16 in PSUM |
| **Block geometry** | 1 E8M0 byte per **8 partitions × 4 columns = 32-element** OCP-MXFP block (`block_size = 32`) |
| **Shape verifier** | `birverifier::checkMatmultMxInputs` @ `0x1007420` (body) / `0x5fb140` (thunk) — **11 asserts** |
| **Instr verifier** | `birverifier::checkMatmultMxInstruction` @ `0x10139a0` (body) / `0x6090d0` (thunk) — **7 asserts** |
| **Dequant** | `birsim::InstVisitor::dequantizeMx` @ `0x27b040`: `out = ldexpf(data, E8M0 − 127)` per 32-elem block |
| **EMAX table** | `byte_5F7898` @ `0x5F7898`: `02 00 00 08 00 0f 08 0f` (indexed `dtype − 2`) |
| **Hard K bound** | `K_weights ≤ 128 × 4 = 512`; partitions ∈ `{32, 64, 128}` |
| **Errors thrown** | `logging::NeuronAssertion<neuronxcc::backend::ErrorCode>` codes `204, 205, 212, 213, 214, 228–237, 240, 241, 289` |

---

## 1. The operand model

A single `InstMatmultMx` carries **six** access patterns, queried positionally. The simulator's `visitInstMatmultMx` (`0x27f680`, the full 314-line body — *not* a thunk) fetches exactly four inputs plus one output, in this order:

```c
// birsim::InstVisitor::visitInstMatmultMx @0x27f680, lines 100-118 (verbatim shape)
PhysicalAP = getOrCreatePhysicalAP(I.getIfmap());        // arg0 : ifmap  data  (FP4/FP8 ×4)
v6         = getOrCreatePhysicalAP(I.getWeights());      // arg1 : weights data  (FP4/FP8 ×4)
v8         = getOrCreatePhysicalAP(I.getIfmapScales());  // arg2 : ifmap  scales (E8M0 u8)
v12        = getOrCreatePhysicalAP(I.getWeightsScales());// arg3 : weights scales (E8M0 u8)
// out0 = getInOutPhysicalAP(I, 0, /*isOutput=*/1)       — the PSUM destination
```

Both verifiers read these same six APs. Each `bir::AccessPattern` exposes, at fixed offsets the verifier bodies dereference directly (CONFIRMED against the simulator body, which reads the identical layout):

| AP field | Offset | Read as | Meaning |
|---|---|---|---|
| Dtype | `+0x30` (= 48) | `*(_DWORD*)(ap + 48)` | the BIR `Dtype` enum index; drives the ×4-unpack test |
| `Pattern` SmallVector | `+0x50` | `*(QWORD*)(ap + 80)` | the multi-dim stride/num descriptor |
| `Pattern[0].num` | `+0x50 → +8` | `*(QWORD*)(*(QWORD*)(ap+80) + 8)` | **partitions accessed** (the first AP dimension's count) |
| `Pattern.size` | `+0x58` (= 88) | `*(_DWORD*)(ap + 88)` | guarded `> 0`; else `SmallVector` `"idx < size()"` abort |

> **NOTE — the AP layout is shared, not coincidental.** The simulator (`libBIRSimulator.so`) and the verifier (`libwalrus.so`) are separate binaries with their **own** copies of the dtype tables, yet both read Dtype at `+0x30` and partition-count at `+0x50→+8`. The simulator's `visitInstMatmultMx` line 130 is `v17 = *(_DWORD *)(PhysicalAP + 48)` and line 128 is `v15 = *(_QWORD *)(*(_QWORD *)(PhysicalAP + 80) + 8LL)` — verbatim confirmation of the offsets the verifier asserts read. **(CONFIRMED.)**

### The ×4-unpack predicate — the central numeric primitive

Every per-partition element count in *both* verifiers is the **logical** (post-unpack) count, not the packed-byte count. The FP4/FP8 data lives in an `×4` lane container: one physical lane holds four logical elements. The unpack test appears 5× in `checkMatmultMxInputs` (once per AP) and is byte-identical to the simulator's:

```c
// The ×4 dtype test (sim line 131, identical predicate in the verifier per AP):
dt = *(_DWORD*)(ap + 0x30);                 // AP Dtype field
N  = getNumElementsPerPartition(ap);        // logical, pre-multiply
if (dt == 2 || (unsigned)(dt - 8) <= 1)     // dt ∈ {2, 8, 9}
    N *= 4;                                  // ×4-unpack: 4 logical elems per packed lane
```

The predicate `dt == 2 || (unsigned)(dt - 8) <= 1` admits **exactly** `{2, 8, 9}` — the three ×4-packed MX dtypes:

| Dtype index | Name | Role |
|---|---|---|
| 2 | `float4_e2m1fn_x4` | FP4, four E2M1 mantissas per lane |
| 8 | `float8_e4m3fn_x4` | FP8, four E4M3 mantissas per lane |
| 9 | `float8_e5m2_x4` | FP8, four E5M2 mantissas per lane |
| 6 | `float8_e8m0fnu` | the **scale** type itself (exponent-only, not data) |

> **GOTCHA — "NumElementsPerPartition" inside an MX assert is always the unpacked count.** Reading the asserts literally without the ×4 expansion gives the wrong bounds by 4×. The packed scale-vs-data relationship is then checked as a literal `data == 4 · scale` (asserts 1, 2, 9, 10 in §2) — the `/4` half of the `[P/8, F/4]` scale shape. The other half (`P/8`) is enforced indirectly by the 8-partition block + the partition-count asserts. **(CONFIRMED — the unsigned-wrap form `(unsigned)(dt-8)<=1` is exactly the simulator's; D-F02 §2, D-F13 §1 agree.)**

---

## 2. `checkMatmultMxInputs` — the shape / numeric contract (11 asserts)

`birverifier::checkMatmultMxInputs` @ `0x1007420` computes, in its prologue, the unpacked per-partition element count of each of the five APs and the start-partition / partition-count of each, then fires up to 11 throw sites. Every throw routes through `logging::NeuronAssertion<neuronxcc::backend::ErrorCode>::NeuronAssertion(...)` → `std::throw_with_nested<…>`; the **error code** is the 2nd constructor argument.

Computed locals (names reconstructed from the predicate text; CONFIRMED via D-G05's line-by-line and corroborated by NX-081 at the same source lines):

```c
v105 = ifMapNumElementsPerPartition          // arg0 N, ×4-expanded   (K_ifmap)
v106 = weightsNumElementsPerPartition         // arg1 N, ×4-expanded   (K_weights)
v100 = ifMapScalesNumElementsPerPartition     // arg2 N, ×4-expanded
v107 = weightsScalesNumElementsPerPartition   // arg3 N, ×4-expanded
v108 = outputNumElementsPerPartition          // out0 N, ×4-expanded
ifmapScalesStartPartition   = getStartPartition(arg2)   // getStartPartition @0xfc6e30
weightsScalesStartPartition = getStartPartition(arg3)
v109 = ifmapNumPartitionsAccessed  = arg0.Pattern[0].num
v110 = weightsNumPartitionsAccessed = arg1.Pattern[0].num
v64  = outputNumPartitionsAccessed  = out0.Pattern[0].num
```

The 11 asserts, in source order:

| # | line | Predicate (verbatim assert text) | Numeric meaning | Code |
|---|---|---|---|---|
| 1 | 3067 | `ifMapNumElementsPerPartition == 4 * ifMapScalesNumElementsPerPartition` | ifmap K == 4·(its E8M0 scale count) — the `F/4` ratio on the free axis | 228 |
| 2 | 3069 | `weightsNumElementsPerPartition == 4 * weightsScalesNumElementsPerPartition` | weights K == 4·(its scale count) | 229 |
| 3 | 3072 | `ifmapScalesStartPartition % 32 < 16` | ifmap-scale stream starts in the **lower half** of its 32-partition quadrant (tested `(sp & 0x10)==0`) | 230 |
| 4 | 3073 | `weightsScalesStartPartition % 32 < 16` | weights-scale stream, same lower-half rule | 231 |
| 5 | 3079 | `ifMapNumElementsPerPartition <= max_ifmap_elements` | K_ifmap ≤ `32·(PSUM_bank_bytes / sizeof(out_dtype))` — the PSUM-bank capacity bound (§4) | 232 |
| 6 | 3083 | `weightsNumElementsPerPartition <= 128 * 4` | **K_weights ≤ 512** — the hard contraction bound = 128 partitions × the ×4 container (tested `v106 > 0x200`) | 233 |
| 7 | 3084 | `(weightsNumElementsPerPartition / 4) % 2 == 0` | the unpacked weights K is an **even** number of 4-col blocks (tested `(v106 & 4)==0`) | 289 |
| 8 | 3091 | `ifmapNumPartitionsAccessed == 32 \|\| == 64 \|\| == 128` | ifmap partition count ∈ **{32, 64, 128}** (see CORRECTION below) | 234 |
| 9 | 3096 | `ifmapNumPartitionsAccessed == weightsNumPartitionsAccessed` | ifmap and weights access the **same** partition count (the shared K-contraction dim) | 235 |
| 10 | 3099 | `outputNumPartitionsAccessed * 4 == weightsNumElementsPerPartition` | 4·(output partitions) == K_weights — the `N`/free dim ×4-maps to output partitions | 236 |
| 11 | 3102 | `outputNumElementsPerPartition * 4 == ifMapNumElementsPerPartition` | 4·(output free elems) == K_ifmap | 237 |

> **CORRECTION — the partition set is `{32, 64, 128}`, not `{32, 64, 96, 128}`.** A prior legality-graph decode (NX-096) summarized assert 3091 as "partitions `{32,64,96,128}`," conflating it with the *separate* DVE-engine verifier `checkQuantizeMxInstruction`. The actual machine test in `checkMatmultMxInputs` is `((v109 - 32) & 0xFFFFFFDF) != 0 && v109 != 128` (throw on failure). `(v - 32) & 0xFFFFFFDF == 0` is true iff `v - 32 ∈ {0, 0x20}` — i.e. `v ∈ {32, 64}` — OR'd with the explicit `v == 128`. For `v = 96`: `(96-32) & 0xFFFFFFDF = 0x40 ≠ 0`, so **96 is rejected.** The admissible set is exactly **{32, 64, 128}** (NX-016 line 124 likewise: "stationary/moving partition dim: multiple of 32, ≤ 128, identical" — and the OR-form of 3091 happens to exclude the multiple 96). **(CONFIRMED by the bit-mask arithmetic; D-G05 + NX-081 concur.)**

Two structural facts pin the assert count and the dtype range:

- The body contains **exactly 11** throw sites (lines 3067, 3069, 3072, 3073, 3079, 3083, 3084, 3091, 3096, 3099, 3102). **(CONFIRMED — D-G05; NX-081 independently lists the same source lines.)**
- A pre-assert bounds guard at line 560: if the output dtype index `> 0x13` (19) the body hits `llvm_unreachable "Unknown dtype"` (`bir/IR/Dtype.h`). So a legal output dtype enum ∈ `[0, 19]` — exactly the 20-entry span of the byte-size table (§4).

Every `Inputs` error message carries the same resolution trailer: *"Please open a support ticket at https://github.com/aws-neuron/aws-neuron-sdk/issues/new. You may also be able to obtain more information using the 'XLA_IR_DEBUG' and 'XLA_HLO_DEBUG' environment variables."*

### The K / shape system, restated as equations

Collapsing the 11 asserts into the algebra a reimplementer must satisfy (`K = elements-per-partition`, all ×4-unpacked):

```text
K_ifmap   == 4 · scale_ifmap          (3067)        out_partitions · 4 == K_weights  (3099)
K_weights == 4 · scale_weights        (3069)        out_free       · 4 == K_ifmap    (3102)
K_weights <= 512  ( = 128 × 4 )       (3083)        ifmap_parts == weights_parts     (3096)
(K_weights / 4) is even               (3084)        ifmap_parts ∈ {32, 64, 128}      (3091)
K_ifmap   <= 32 · (PSUM_bank_bytes / sizeof(out_dtype))                              (3079)
scale_start_ifmap   % 32 < 16   and   scale_start_weights % 32 < 16                  (3072/3073)
```

---

## 3. `checkMatmultMxInstruction` — the instruction-level contract (7 asserts)

`birverifier::checkMatmultMxInstruction` @ `0x10139a0` is the second gate: where `Inputs` checked *counts*, `Instruction` checks *placement and tiling*. It reads the engine field, the four start-partitions, the partition count, and the per-tile row/col descriptor vectors, then fires up to 7 throw sites.

```c
engine = *(_DWORD*)(this + 0x90);              // bir::Instruction engine field
sp_ifmap        = getStartPartition(arg0);     // ifmap  data  start partition
sp_weights      = getStartPartition(arg1);     // weights data
sp_ifmapScales  = getStartPartition(arg2);     // ifmap  scales
sp_weightScales = getStartPartition(arg3);     // weights scales
quadrant(x) = x >> 5;                          // partition / 32 → which 32-partition SBUF quadrant
numPartitionsAccessed = arg0.Pattern[0].num;
// 4 parallel per-tile int32 vectors via sub_10125B0(this, &rPos, &cPos, &rSize, &cSize):
//   {row_tile_pos, col_tile_pos, row_tile_size, col_tile_size}  — equal length (invariant below)
```

| # | line | Predicate (verbatim) | Bound / meaning | Code |
|---|---|---|---|---|
| 1 | 2994 | `I.getEngine() == bir::EngineType::PE` | engine `[this+0x90] == 3`. MX matmul is **PE-only** (the systolic Tensor engine) | 205 |
| 2 | 3012 | `ifmapStartQuadrant == ifmapScalesStartQuadrant` | `(sp_ifmap>>5) == (sp_ifmapScales>>5)`: ifmap **data and its scale start in the same 32-partition quadrant** | 212 |
| 3 | 3014 | `weightsStartQuadrant == weightsScalesStartQuadrant` | weights data & its scale, same quadrant | 213 |
| 4 | 3017 | `(ifmapScalesStartPartition % 4 == 0) && (weightsScalesStartPartition % 4 == 0)` | both scale streams begin on a **4-partition boundary** (the 4-col MX block boundary); tested `((sp_w \| sp_if) & 3) != 0` | 214 |
| 5 | 3028 | `(row_tile_size[i] == -1) \|\| ((unsigned)row_tile_size[i] == numPartitionsAccessed)` | for **every** tile `i`: row tile is untiled (`-1`) or equals the full partition count — **rows are never sub-tiled** | 240 |
| 6 | 3031 | `(col_tile_pos[i] ∈ {-1,0}) && (col_tile_size[i] ∈ {-1,128})` | for every tile `i`: the **column is never tiled** — position is none/0, size is none/full-128 | 241 |
| 7 | 3037 | `I.getReplicationShiftAmnt() == 0` | weight-replication shift forbidden (read via a `std::variant` getter; bad tag throws `bad_variant_access` first) | 204 |

Asserts 5 and 6 run inside **one** loop over the tile-descriptor index `i ∈ [0, col_tile_pos.size())`; trip count `= (col_tile_pos.end − col_tile_pos.begin) >> 2` (int32 elements). Inside, the column check is spelled `(unsigned)(col_tile_pos[i]+1) > 1` (detects pos ∉ {-1,0}) and `col_tile_size[i] != 128 && col_tile_size[i] != -1` (detects a bad col size). The extractor `sub_10125B0` itself asserts the four vectors are parallel: *"(row_tile_pos.size() == col_tile_pos.size() && row_tile_pos.size() == row_tile_size.size() && row_tile_pos.size() == col_tile_size.size())"*.

> **QUIRK — `engine == PE` here is an MX-specific gate on top of the generic engine check.** The L1 `checkValidEngines` verifier already validates engine legality for all instructions; `checkMatmultMxInstruction`'s 2994 (code 205) is a *second, MX-specific* assertion with its own code. It is not a duplicate — different assert, different code, different message. The MX path is doubly pinned to PE. **(CONFIRMED — D-G05 §5 vs D-E21.)**

> **GOTCHA — the only legal MX-matmul split is across whole instructions.** Asserts 5/6 together mean an MX matmul occupies a **full 128-wide PE column** and a row block equal to the accessed partition count (∈ {32,64,128}, per Inputs 3091). You cannot column-tile or row-subtile a single `InstMatmultMx`; to cover a larger problem you emit *more instructions*, each whole. A reimplementing scheduler must therefore tile at the op level, never inside the descriptor.

### Where the data/scale must physically sit

The quadrant model the two verifiers jointly enforce (CONFIRMED; `getStartPartition` @ `0xfc6e30` returns `baseByteOffset / firstStride` = the first partition the AP touches):

```text
SBUF partitions → 32-partition QUADRANTS;  quadrant = partition >> 5.
  • DATA and its SCALE co-reside in one quadrant         (Instr 3012 / 3014)
  • each SCALE stream starts in the LOWER half: %32 < 16  (Inputs 3072 / 3073)
  • each SCALE stream is 4-partition aligned: %4 == 0      (Instr 3017)
```

The `%32 < 16` (lower-half) leaves the upper 16 partitions of the quadrant for the data lanes and the 8-partition block stacking; the `%4 == 0` lands each scale stream on the 4-column MX block boundary (one E8M0 byte per 4 columns). The exact intra-quadrant packing of the 8-partition scale blocks is **inferred** from these two rules (not independently confirmed from a NEFF fixture — D-G05 GAP G4).

---

## 4. The PSUM-bank capacity bound (`max_ifmap_elements`)

Assert 5 of `Inputs` (line 3079) is the only bound whose value is **architecture-dependent**. It caps `K_ifmap` at the number of fp32 (or bf16) elements one PSUM bank holds, times 32:

```c
// checkMatmultMxInputs line 3079 derivation (mechanism CONFIRMED; per-arch byte value not pinned here):
PSUM_bank_bytes  = Hwm::getSingleton(arch)->vtable[+0x78](arch);   // per-arch HWM query
perBank          = PSUM_bank_bytes / qword_1DEFBA0[out_dtype];     // elements per PSUM bank
max_ifmap_elements = 32 * perBank;                                 // ×32 = 32 K per block
assert(ifMapNumElementsPerPartition <= max_ifmap_elements);        // code 232
```

`PSUM_bank_bytes` comes from `bir::Hwm::getSingleton(arch)` followed by `call qword ptr [rax+78h]` (the HWM accessor). For Trainium gen2–gen4 the PSUM bank is **2048 bytes** (8 banks; see [SBUF/PSUM Geometry](../arch/sbuf-psum-geometry.md)), so with an fp32 output the concrete bound is `32 · (2048 / 4) = 16384`, and with bf16 `32 · (2048 / 2) = 32768`. **(The formula is CONFIRMED; the concrete per-arch `PSUM_bank_bytes` is HWM-table-strand work, taken from the geometry page rather than re-decoded here.)**

The per-dtype **byte-size** divisor is `qword_1DEFBA0` (`0x1DEFBA0`, `.rodata`, 20 × u64, xxd-verified via the VA==fileoff anchor):

```text
idx:   0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19
bytes: 1  1  2  1  1  1  1  1  4  4  2  2  2  2  4  4  4  4  8  8
```

> **NOTE — three independently-decoded copies of this table agree byte-for-byte.** `qword_1DEFBA0` (libwalrus, this verifier), `qword_1DFC040` (libwalrus, the D-D04 dtype-stride decode), and `qword_5F74E0` (libBIRSimulator) are **byte-identical**: `1 1 2 1 1 1 1 1 4 4 2 2 2 2 4 4 4 4 8 8`. The simulator and the verifier ship their own copies; they were not shared at link time, which is why a reimplementer can trust the values rather than a single decode. The `out_dtype` index must be ≤ 19. **(CONFIRMED — D-G05, D-D04, D-F02 all cite the same 20 values.)**

---

## 5. `dequantizeMx` — the silicon dequant formula

Once the legality contract holds, the actual arithmetic the PE array performs (and the BIR simulator reproduces bit-exactly) is one `ldexpf` per element. `birsim::InstVisitor::dequantizeMx` @ `0x27b040`, the full body:

```c
// birsim::InstVisitor::dequantizeMx(this, rows, K, float* dataF, u8* scale) @0x27b040
//   dataF  : FP4/FP8 mantissas already cast to fp32 (by cast_to(...,16) upstream)
//   scale  : the E8M0 byte stream, block-major reordered by copyScalesFromArray
//   rows   : Pi*8 = the full partition extent;   K : free elements per partition
out = new float[rows * K];                            // operator new[](4 * rows * K)
for (row = 0; row < rows; ++row)
  for (col = 0; col < K; ++col)
    out[row*K + col] = ldexpf( dataF[row*K + col],
                               scale[ (K >> 2) * (row >> 3) + (col >> 2) ] - 127 );
return out;
```

The body, line 26 verbatim:

```c
*(float *)(v8 + 4 * v10) = ldexpf(a4[v10], a5[(a3 >> 2) * (v9 >> 3) + (v10 >> 2)] - 127);
//                                a4=dataF  a5=scale  a3=K  v9=row  v10=col
```

Decoded:

- **Scale index** `= (K/4)·(row/8) + (col/4)`. One E8M0 byte is shared by every `(8 partitions) × (4 columns)` tile = **32 elements** — the OCP-MXFP `block_size = 32`. The `row >> 3` is the 8-partition stride; the `col >> 2` is the 4-column stride; `K >> 2` is the per-row block count.
- **`scale − 127`** removes the `float8_e8m0fnu` bias (E8M0 bias = 127). E8M0 is exponent-only: it has no mantissa, so its decoded value *is* a power of two.
- **`ldexpf(x, e) = x · 2^e`** applies the shared block exponent as a power-of-two scale. The full recovered value is therefore `mantissa · 2^(E8M0 − 127)` — bit-exactly the OCP MX dequant: `value = element · scale · 2^(E8M0 − 127)` with the scale folded into the shared exponent. **(CONFIRMED bit-exact against the decompiled body.)**

The FP4/FP8 mantissa is already unpacked to fp32 by `cast_to(obj, 16)` *before* `dequantizeMx`, and the `×4` lane container was expanded by the `Ki *= 4` extent plus a `deinterleaveMatrices(…, factor=4)` (sim lines 159–163). The `copyScalesFromArray<u8>` (`0x29e5e0`) reorders the raw scale-AP bytes into block-major layout (`idx → (i&3) + 32·(i>>2)`) so `dequantizeMx` sees a contiguous scale grid.

The end-to-end MX matmul numeric model:

```text
out[p, n] = Σ_k  ldexpf( ifmap_fp[k,n],  Si[block(p,k)] − 127 )
               ·  ldexpf( weight_fp[k,p], Sw[block(p,k)] − 127 )       in fp32
where block(p,k) groups 8 partitions × 4 elements = one 32-element OCP-MXFP block.
```

### Round-trip: the quantize inverse

The forward (quantize) direction is the **exact** inverse, which is why the round-trip is lossless in the exponent. `generateMxScales` (`0x29e1d0`) derives the E8M0 byte, and `quantizeDataApplyingMxScales` (`0x29e2a0`) applies it:

```c
// generateMxScales @0x29e1d0  (verbatim lines 31-38):
v8 = findMaxExponentPerInputBlock(a1, row, col) - emaxBias;   // per-block max exponent − EMAX
if (v8 < 0) v8 = 0;                                            // clamp to 0  (max(0, …))
scale[block] = v8;
if (scale[block] == 0) scale[block] = 1;                       // clamp 0 → 1

// quantizeDataApplyingMxScales @0x29e2a0:
out = float_bits( (254 - E8M0) << 23 ) * input;               // = 2^(127 - E8M0) · input
//  = input / 2^(E8M0 - 127)   — the EXACT inverse of dequantizeMx's · 2^(E8M0-127)
```

> **NOTE — the `254` (not 255) is the bias bookkeeping.** `float_bits((254 − E8M0) << 23)` constructs an fp32 whose biased exponent field is `254 − E8M0`, i.e. an unbiased exponent of `(254 − E8M0) − 127 = 127 − E8M0`. So `E8M0 = 127` (unbiased 0) → `2^0 = ×1` (no scale); `E8M0 > 127` → scale down; `E8M0 < 127` → scale up. Same `(row>>3, col>>2)` block index as the dequant. **(CONFIRMED — D-F02 §7, D-F13 §3.)**

---

## 6. EMAX — the per-format exponent reference

`generateMxScales` subtracts an **EMAX bias** per dtype before clamping. That bias is `byte_5F7898[dtype − 2]` (`0x5F7898`, `.rodata`, xxd-verified), 8 valid bytes:

```text
0x5F7898:  02 00 00 08 00 0f 08 0f          (indexed dtype − 2)
```

| dtype | name | byte (EMAX) | role |
|---|---|---|---|
| 2 | `float4_e2m1fn_x4` | **2** | FP4 max-normal exponent (`4.0·1.5 = 6.0`) |
| 3 | `float8e3` (legacy) | 0 | not an OCP MX target |
| 4 | `float8e4` (legacy) | 0 | not an OCP MX target |
| 5 | `float8_e4m3fn` | **8** | E4M3 max-normal exponent (`448`) |
| 6 | `float8_e8m0fnu` | 0 | the scale type itself |
| 7 | `float8e5` (`e5m2`) | **15** | E5M2 max-normal exponent (`57344`) |
| 8 | `float8_e4m3fn_x4` | **8** | ×4-packed E4M3 |
| 9 | `float8_e5m2_x4` | **15** | ×4-packed E5M2 |

The scope's EMAX set is therefore confirmed as **`{fp4 → 2, e4m3 → 8, e5m2 → 15}`** — these are the OCP per-format **max-normal unbiased exponents**, the reference the block's shared exponent is measured *against* in `generateMxScales`. The ×4 MX data dtypes (2, 8, 9) and their base formats (2, 5, 7) carry the same value pairwise.

> **NOTE — `MxAlternativeEmaxEn` shifts EMAX by one in the alternative mode.** The raw table byte is *not* handed to `generateMxScales` directly. The caller `doQuantizeMx` (`0x29e380`) reads `v6 = byte_5F7898[dt − 2]` (guarded `(unsigned)(dt−2) <= 7`; `lea unk_5F7898` at `0x29e398`) and passes `(MxAlternativeEmaxEn == 0) + v6 − 1` as the bias argument. In the **default** mode (`MxAlternativeEmaxEn == 0`) this is `1 + EMAX − 1 = EMAX` (the raw table value); in the alternative mode it is `EMAX − 1`. The NEFF-side table emits `emax_fp8_e4m3 = MxAlternativeEmaxEn ? 7 : 8` and `emax_fp8_e5m2 = ? 14 : 15` — i.e. the `−1` arm. `MxAlternativeEmaxEn` is a `libBIR`-imported global (HIGH — the default value was not independently pinned). **(CONFIRMED for the default path; D-F13 §1; `doQuantizeMx` body re-verified.)**

> **QUIRK — quantize and dequantize live in *different* visitors.** The EMAX-bias path (`doQuantizeMx` → `generateMxScales` + `quantizeDataApplyingMxScales`) is reached only from `visitInstQuantizeMx` (`0x1f6920`, the `QuantizeMx` opcode 96). `visitInstMatmultMx` (`0x27f680`) never calls `doQuantizeMx`; it performs the *inverse* via `dequantizeMx` (`0x27b040`) plus the ×4 `Ki` adjustment. The two are a matched forward/inverse pair across two opcodes, not one routine — a reimplementer building the matmul path needs only the dequant side; the EMAX table matters when authoring the *scales* (the `QuantizeMx` op, page 9.8).

---

## 7. Reimplementation checklist

To emit a *legal* `InstMatmultMx` a backend must satisfy, in order:

1. **Engine** = PE (`EngineType::PE == 3`); no other engine accepts the opcode (Instr 2994 / 205).
2. **Dtypes**: data ∈ `{2, 8, 9}` (FP4/FP8 ×4-packed); scales = E8M0 (dtype 6); output dtype ≤ 19.
3. **Counts** (all ×4-unpacked): `K_ifmap == 4·scale_ifmap`, `K_weights == 4·scale_weights` (Inputs 3067/3069/228/229).
4. **K bound**: `K_weights ≤ 512`, `(K_weights/4)` even, `K_ifmap ≤ 32·(PSUM_bank_bytes / out_dtype_bytes)` (3083/3084/3079/233/289/232).
5. **Partitions**: ifmap parts ∈ `{32, 64, 128}` and `== weights parts`; `out_parts·4 == K_weights`; `out_free·4 == K_ifmap` (3091/3096/3099/3102/234/235/236/237).
6. **Placement**: each scale stream starts `%32 < 16` and `%4 == 0`, in the **same quadrant** as its data (3072/3073/3017/3012/3014 → 230/231/214/212/213).
7. **Tiling**: never column-tile (`col_tile ∈ {none, 0}/{none, 128}`); never row-subtile (`row_tile ∈ {none, parts}`); no replication shift (3031/3028/3037 → 241/240/204).

Satisfy those and the value the array computes is `out[p,n] = Σ_k ldexpf(ifmap_fp, Si−127) · ldexpf(weight_fp, Sw−127)` in fp32 — the formula of §5.

---

## 8. Confidence & gaps

| Claim | Confidence | Anchor |
|---|---|---|
| `checkMatmultMxInputs` @ `0x1007420`, 11 asserts at lines 3067–3102, codes 228–237/289 | CONFIRMED | D-G05 line-by-line; NX-081 same source lines; thunk `0x5fb140` `jmp off_3DD3088` decoded |
| `checkMatmultMxInstruction` @ `0x10139a0`, 7 asserts, codes 204/205/212/213/214/240/241 | CONFIRMED | D-G05; NX-081 cites the same predicates |
| Partition set `{32, 64, 128}` (NOT `{…, 96, …}`) | CONFIRMED | bit-mask `((v-32)&0xFFFFFFDF)!=0 && v!=128` ⇒ 96 rejected; NX-016 "multiple of 32, ≤128" |
| ×4 dtype set `{2, 8, 9}`; AP Dtype `+0x30`, partition-count `+0x50→+8` | CONFIRMED | sim `visitInstMatmultMx` lines 130/128 verbatim |
| `dequantizeMx` = `ldexpf(data, E8M0 − 127)`, block index `(K/4)·(r/8)+(c/4)` | CONFIRMED | `0x27b040` line 26 verbatim |
| EMAX `byte_5F7898` = `02 00 00 08 00 0f 08 0f` ⇒ `{fp4:2, e4m3:8, e5m2:15}` | CONFIRMED | xxd; D-F02 §7, D-F13 §1, generateMxScales `0x29e1d0` |
| dtype byte-size `1 1 2 1 1 1 1 1 4 4 2 2 2 2 4 4 4 4 8 8` (20 u64) | CONFIRMED | `qword_1DEFBA0`/`qword_1DFC040`/`qword_5F74E0` byte-identical |
| quantize inverse `float_bits((254−E8M0)<<23)·in = in / 2^(E8M0−127)` | CONFIRMED | D-F02 §7, D-F13 §3 |
| `max_ifmap_elements = 32·(PSUM_bank_bytes/out_bytes)`; concrete `PSUM_bank_bytes` per arch | STRONG (formula) / INFERRED (concrete) | HWM `[rax+0x78]`; 2048 B from SBUF/PSUM geometry page, not re-decoded here |
| `getReplicationShiftAmnt()` exact struct offset (`std::variant` near `+0xB0/0xC0`) | INFERRED | predicate text + code 204 CONFIRMED; no independent getter body decoded (D-G05 G1) |
| Intra-quadrant 8-partition scale-block packing | INFERRED | implied by `%32<16` + `%4==0`; no NEFF fixture byte-diff (D-G05 G4) |
| `sub_10125B0` per-tile derivation math | INFERRED | 4-vectors-equal-length invariant CONFIRMED; full extraction not transcribed (D-G05 G2) |

**Sources:** D-G05 (`checkMatmultMxInputs` / `checkMatmultMxInstruction` line-by-line), D-F02 (BIR simulator matmul / MX per-opcode kernels), cross-checked against D-F13 (MX quantize), D-D04 (dtype tables), NX-016, NX-081, NX-096. Re-verified for this page against the decompiled `dequantizeMx`/`visitInstMatmultMx`/`generateMxScales` bodies and the `.rodata` EMAX / byte-size tables.
