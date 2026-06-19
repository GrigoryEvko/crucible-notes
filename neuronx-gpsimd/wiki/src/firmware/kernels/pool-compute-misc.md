# POOL/ACT Compute Gap-Cluster

This page consolidates the remaining decoded POOL- and ACT-band opcodes that the
[opcode-catalog-ledger](opcode-catalog-ledger.md) surfaced but that do not warrant a
dedicated page each: fourteen instructions across four clusters. None of them is a
windowed-pooling variant (real avg/max pooling is `0x45 Pool` / `0x58 MaxPoolSelect`); the
"POOL" attribution here is mostly an **engine/queue** fact, not an operation fact. The set
breaks down as two fixed-function/data-movement compute ops, one region fill, one
gather-buffer load, two control/config loads, two integer/transposed arithmetic ops, one
predicated copy that actually lives on DVE, a custom-op marshalling pair, and the NC-v4/v5
fused-activation pair.

Every name is read byte-exact from the four shipped generation `common.h`
`NEURON_ISA_TPB_OPCODE` enums (SUNDA/CAYMAN/MARIANA/MAVERICK). Every operand struct is
`gcc`-compile-verified at 64 B with `offsetof()` matching the header columns. Every dispatch
attribution is grounded in the `libnrtucode_internal.so` self-name (`S:`) table multiplicity
and `nm -S` blob ranges, or in the Q7 `kernel_info_table` (KIT) membership carried from the
[kernel-info-table](../pool/kernel-info-table.md) decode.

> **Coherence pack (Part 5).** GPSIMD = Cadence Vision-Q7 NX "Cairo" 512-bit FLIX DSP
> (`ncore2gp`), engines PE/DVE/POOL/ACT/SP/TOP_SP/SEQ. Generations
> SUNDA(v2)/CAYMAN(v3)/MARIANA(v4)/MARIANA_PLUS(v4+)/MAVERICK(v5); v2–v4 are byte-grounded,
> v5 interiors are inferred. The DTYPE enum is
> `INVALID=0, UINT64=1, INT8=2, UINT8=3, INT16=4, UINT16=5, BFLOAT16=6, FP16=7, INT32=8,`
> `UINT32=9, FP32=0xA, FP32R=0xB, INT64=0xC, FP8_EXP3=0xD, FP8_EXP4=0xE, FP8_EXP5=0xF`.
> `0x72 CopyPredicated` is the base of the predicated-op family — see
> [CastPredicated](castpredicated.md), [CopyPredicatedScalar](copypredicatedscalar.md),
> [CopyPredicatedReduce](copypredicatedreduce.md). `0x25`/`0x26` relate to
> [Activate-PWL](activate-pwl.md).

---

## 0. Master matrix — fourteen opcodes, one row each

Confidence tags: **HIGH/MED/LOW** × **OBSERVED** (read/compiled this analysis) /
**INFERRED** (derived) / **CARRIED** (from a prior cross-referenced decode). The `FLAG`
column is `[SU CA MA MV]` presence in the four generation enums.

| Cluster | Op | Name (`NEURON_ISA_TPB_OPCODE_…`) | Struct (64 B) | Surface (SU / CA+) | Class | FLAG |
|---|---|---|---|---|---|---|
| A | `0x48` | `RECIPROCAL` | `S4D4_TR` | SEQ / SEQ | fixed-fn 1/x | `YYYY` |
| A | `0x49` | `MEMSET` | `D4_MR` | Q7KIT(SU) / SEQ | region fill | `YYYY` |
| A | `0x67` | `POOL_BUFFER_LOAD` | `S4_PB` | Q7KIT(SU) / SEQ | gather-buffer load | `YYYY` |
| A | `0x69` | `LOAD_MASK_SELECT` | `CTRL_IM` | SEQ(ctrl) / SEQ(ctrl) | mask-table config | `YYYY` |
| A | `0x6a` | `STREAM_SHUFFLE` | `S4D4_TR` | SEQ / SEQ | lane-permute mover | `YYYY` |
| B | `0x72` | `COPY_PREDICATED` | `S3S3D3_TT` | **DVE-native** | predicated copy | `YYYY` |
| B | `0x74` | `TENSOR_SCALAR_ADDR` | `S2D2_ADDR` | Q7KIT(SU)+POOL / POOL | 64-bit address arith | `YYYY` |
| B | `0x7a` | `LOAD_POOL_ARGUMENT` | `CTRL_IM` | Q7KIT(SU)+POOL / POOL | 8-slot arg load | `YYYY` |
| B | `0x93` | `TRANSPOSE_TENSOR_SCALAR_ARITH_OP` | `S3D3_TS` | TS-inline (POOL) | transposed-src arith | `YYYY` |
| B | `0x95` | `MODIFY_POOL_CONFIG` | `MODIFY_POOL_CONFIG` | POOL config | ucode-lib load/unload | `YYYY` |
| Cop | `0x85` | `CUSTOM_OP_HEADER` | `CUSTOM_OP_HEADER` | marshalling (no compute surface) | custom-op envelope | `YYYY` |
| Cop | `0x86` | `CUSTOM_OP_PAYLOAD` | `CUSTOM_OP_PAYLOAD` | marshalling (no compute surface) | custom-op arg block | `YYYY` |
| ACT | `0x25` | `ACTIVATE2` | `S2D2_AC` | ACT-SEQ (MA) → DVE (MV) | fused act+ALU+reduce | `--YY` |
| ACT | `0x26` | `ACTIVATE_MULTIPASS` | `S1S2D2_AM` | spec-present, image-dormant | multi-pass activation | `---Y` |

Two corrections to earlier ledger entries are folded in below: `0x72` is **DVE-native**, not a
POOL software kernel; and `0x85`/`0x86` are **custom-op marshalling**, not POOL arithmetic.

> **VERIFICATION GOTCHAS used throughout.** (1) `.data` VMA − file-offset delta is `0x200000`
> for `ncore2gp` config DLLs — confirm per-section with `readelf -SW`; `.text`/`.rodata` are
> VMA==file-offset. (2) The `S:` self-name **multiplicity** is the engine discriminator:
> count the exact newline-terminated token (`S: <Name>\n`) with a byte scan, *not* a
> substring `grep` — `S: CopyPredicated` substring-matches `CopyPredicatedReduce`/`…Scalar`
> and inflates the count. (3) All struct sizes via `gcc -I<gen>/tpb`, `sizeof`/`offsetof`.

---

## 1. The name pull — byte-exact from the four `common.h` enums

Re-grepped `NEURON_ISA_TPB_OPCODE_… = 0x..  // Y` in each
`neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_common.h`. All five cluster-A and all five
cluster-B opcodes plus the custom-op pair are `// Y` (maintained) and byte-identical in name,
value, and flag across SUNDA→MAVERICK. The ACT pair is generation-gated. Enum line numbers
(maverick / sunda where both exist):

| Op | Name | maverick line | sunda line | flag `[SU CA MA MV]` |
|---|---|---|---|---|
| `0x25` | `ACTIVATE2` | 172 | — | `--YY` |
| `0x26` | `ACTIVATE_MULTIPASS` | 173 | — | `---Y` |
| `0x48` | `RECIPROCAL` | 185 | 174 | `YYYY` |
| `0x49` | `MEMSET` | 186 | 175 | `YYYY` |
| `0x67` | `POOL_BUFFER_LOAD` | 203 | 192 | `YYYY` |
| `0x69` | `LOAD_MASK_SELECT` | 205 | 194 | `YYYY` |
| `0x6a` | `STREAM_SHUFFLE` | 206 | 195 | `YYYY` |
| `0x72` | `COPY_PREDICATED` | 214 | 203 | `YYYY` |
| `0x74` | `TENSOR_SCALAR_ADDR` | 216 | 205 | `YYYY` |
| `0x7a` | `LOAD_POOL_ARGUMENT` | 221 | 210 | `YYYY` |
| `0x85` | `CUSTOM_OP_HEADER` | 231 | 219 | `YYYY` |
| `0x86` | `CUSTOM_OP_PAYLOAD` | 232 | 220 | `YYYY` |
| `0x93` | `TRANSPOSE_TENSOR_SCALAR_ARITH_OP` | 235 | 230 | `YYYY` |
| `0x95` | `MODIFY_POOL_CONFIG` | 237 | 232 | `YYYY` |

[All **HIGH/OBSERVED** — re-grepped all four enums this analysis.]

> **GOTCHA — opcode-byte aliasing in unrelated enums.** The byte `0x49` is *also*
> `NEURON_ISA_TPB_EVT_SEM_NONBLOCKING_CMD_SEM_WRITE = 0x49` in mariana/maverick's
> event-semaphore *command* enum; `0x93`/`0x95` reappear as `WAIT_MODE`/`UPDATE_MODE`
> semaphore-mode enum values. None of those are opcodes. The **opcode** enum value is
> unambiguous: `0x49 = MEMSET`, `0x93 = TRANSPOSE_TENSOR_SCALAR_ARITH_OP`,
> `0x95 = MODIFY_POOL_CONFIG` in all four gens. [HIGH/OBSERVED.]

The `struct2opcode` reverse map (jq over each gen's `instruction_mapping.json`) binds the names
to structs exactly as the matrix shows. On CAYMAN/MARIANA/MAVERICK the `0x85`/`0x86` enum
values persist but carry **no** `struct2opcode` entry and ship **no** struct header — the
custom-op format is frozen at the SUNDA (v2) layout (see §13). [HIGH/OBSERVED — `fd` finds
two `custom_op*.h` only under `neuron_sunda_arch_isa/tpb`, zero elsewhere.]

---

## 2. The dispatch discriminator (method)

Two surfaces decode these opcodes, and the discriminator is purely empirical:

1. **Q7 `kernel_info_table` (KIT) membership** ⇒ the opcode is a POOL *software kernel*.
   Absence ⇒ engine-native or control-decoded. The SUNDA flat 18-entry KIT and the CAYMAN+
   17-entry EXTISA KIT are carried from the
   [kernel-info-table](../pool/kernel-info-table.md) decode (the SUNDA EXTISA container ships
   as `SUNDA_Q7_POOL_RELEASE_EXTISA_0_SO_get` / `…_JSON_get`, partially out-of-corpus).
2. **`S: <Name>\n` self-name multiplicity** in `libnrtucode_internal.so` (10,276,288 B) selects
   the engine for the non-KIT ops: **3×** = POOL/ACT/PE-class DEBUG image, **4×** = DVE,
   **16×** = the iTPB sequencer. The 3 POOL copies live one each in the
   `CAYMAN_/MARIANA_/MARIANA_PLUS_NX_POOL_DEBUG_DRAM` blobs; the 4 DVE copies in the four
   `NX_DVE_DEBUG_DRAM` blobs. SUNDA ships **RELEASE-only** images
   (`SUNDA_*_RELEASE_*`), which are string-stripped — so a SUNDA `S:` absence is a build
   artifact, not a kernel absence.

`nm -S libnrtucode_internal.so` grounds the blob ranges used below:

```
CAYMAN_NX_POOL_DEBUG_DRAM_get.data       0x1cdc40 .. 0x1d4b60   (+0x6f20)
MARIANA_NX_POOL_DEBUG_DRAM_get.data      0x4675c0 .. 0x4685c0   (+0x7000)
MARIANA_PLUS_NX_POOL_DEBUG_DRAM_get.data 0x731a40 .. 0x738ba0   (+0x7160)
CAYMAN_NX_DVE_DEBUG_DRAM_get.data        0x18b320 .. 0x192080   (+0x6d60)
SUNDA_Q7_POOL_RELEASE_*                  (RELEASE images, no S: table)
```

Applied to this page's non-KIT ops (exact-token byte counts this analysis):

| Op | KIT(SU)? | `S:` token | count | resident blob | ⇒ engine |
|---|---|---|---|---|---|
| `0x72` | no | `CopyPredicated` | **4×** `0x18dc20/0x427f00/0x6efc20/0x8affd0` | NX_DVE | **DVE** |
| `0x74` | yes | `TensorScalarAddr` | **3×** `0x1d02d0/0x469c90/0x734110` | NX_POOL | POOL |
| `0x7a` | yes | `LoadPoolArgument` | **3×** `0x1d0370/0x469d30/0x7341b0` | NX_POOL | POOL |
| `0x93` | no | — | **0×** (TensorScalar-folded) | — | POOL/TS |
| `0x95` | no | `ModifyPoolConfig` | **3×** `0x1d0340/0x469d00/0x734180` | NX_POOL | POOL |
| `0x25` | no | `Activate2` | **2×** `0x4050a0/0x6cba20` | (ACT) | ACT→DVE |
| `0x26` | no | `ActivateMultipass` | **0×** | — | dormant |

Cross-check: `CopyPredicated` @ `0x18dc20` falls inside `CAYMAN_NX_DVE_DEBUG_DRAM`
`0x18b320..0x192080`, and `TensorScalarAddr`/`LoadPoolArgument`/`ModifyPoolConfig` @ `0x1d02d0`
+ all fall inside `CAYMAN_NX_POOL_DEBUG_DRAM` `0x1cdc40..0x1d4b60`. The `CastPredicated`
neighbour (`0x18dc33/0x427f13/0x6efc33/0x8affe3`) sits `0x13` bytes above each `CopyPredicated`
token. [All **HIGH/OBSERVED** — byte scan + `nm -S` this analysis.]

> **CORRECTION — `CopyPredicated` count.** Earlier prose described "3 distinct copies per DVE
> blob." The exact newline-terminated token count is **1 per DVE blob × 4 blobs = 4** — the
> standard DVE multiplicity. The inflation came from substring matching the longer
> `CopyPredicatedReduce`/`CopyPredicatedScalar` names. [HIGH/OBSERVED.]

The cluster-A ops (`Reciprocal`/`Memset`/`PoolBufferLoad`/`LoadMaskSelect`/`StreamShuffle`)
have **0× `S:`** tokens in this blob — consistent with them being reached via the SUNDA Q7 KIT
(`0x49`/`0x67`) or the SEQ ASCII compare-chain (the others), neither of which emits a named
DEBUG kernel here.

---

## Cluster A — fixed-function compute, fill, gather-load, mask-config, lane-permute

### 3. `0x48 RECIPROCAL` — element-wise tensor 1/x (fixed-function)

**Struct** `S4D4_TR` (64 B, the tensor-reduce/transform family struct shared with Copy `0x46`,
Cast `0x47`, StreamShuffle `0x6a`, StreamTranspose `0x6b`, and the reduce/cumulative arms):

| off | field | width | constraint for reciprocal |
|---|---|---|---|
| 0 | `header` | 4 | opcode `0x48` |
| 4 | `events` | 8 | |
| 12 | `src_mem_pattern` (`MEM_PATTERN4D`) | 20 | one 4-D src |
| 32 | `in_dtype` (`DTYPE`) | 1 | `is_valid_dtype(.., FP32R::False)` |
| 33 | `out_dtype` (`DTYPE`) | 1 | `is_valid_dtype(.., FP32R::True)` |
| 34 | `num_active_channels` | 1 | range ≤ `DVE_NUM_CHANNELS` (=128) |
| 35 | `negated` | 1 | `has_zero_negated_field` ⇒ 0 |
| 36 | `op` (`ALU_OP`) | 1 | `s4d4_tr_op_bypass` ⇒ `Bypass=0x00` |
| 37 | `op_dim` (`TENSOR_SUBDIM`) | 1 | `is_zero_subdim` (UNUSED or X) |
| 38 | `mask_enable` | 1 | `mask_enable_zero` ⇒ 0 |
| 39 | `reserved1[5]` | 5 | 0 |
| 44 | `dst_mem_pattern` (`MEM_PATTERN4D`) | 20 | one 4-D dst |

`offsetof`: `src@12 in_dtype@32 out_dtype@33 op@36 op_dim@37 mask_enable@38 dst@44`,
`sizeof=64`. [HIGH/OBSERVED — `gcc` maverick & sunda.]

**Dispatch.** Not in any Q7 KIT (neither SUNDA-18 nor CAYMAN+-17); reached via the SEQ ASCII
dispatch. Because `0x48` shares `S4D4_TR` with Copy/Cast (which *do* hold KIT rows on CAYMAN+),
a `0xF0`-ExtendedInst-bridged path into the same decode machinery is structurally possible but
not byte-confirmed. [HIGH/OBSERVED for KIT absence; bridge MED/INFERRED.]

**Semantics.** Header line 20, verbatim: *"Reciprocal — computes the element-wise reciprocal of
each element in a tensor."* The validator arm:

```c
// is_valid_reciprocal(i):  (s4d4_tr.h:288-308, verbatim)
bool is_valid_reciprocal(Inst i) {
    return has_valid_neuron_header(i) && has_valid_neuron_events(i)
        && has_reciprocal_opcode(i)                                  // opcode == 0x48
        && s4d4_tr_reserved_zero(i) && mask_enable_zero(i)
        && is_valid_dtype(i.s4d4_tr.in_dtype,  /*FP32R*/ false)      // FP32R illegal as input
        && is_valid_dtype(i.s4d4_tr.out_dtype, /*FP32R*/ true)       // FP32R legal as output
        && is_valid_enum(AluOp, i.s4d4_tr.op)
        && is_zero_subdim(i.s4d4_tr.op_dim)                          // no reduction axis
        && has_valid_active_channel_range_with_tile(i.s4d4_tr.num_active_channels,
                                                    DVE_NUM_CHANNELS, i.s4d4_tr.header.inst_flags)
        && /* src/dst mem4d valid, both AllowedInPSUM AND AllowedInSBUF */
           s4d4_tr_same_src_dst_count(i)   // element-count(src) == element-count(dst)
        && s4d4_tr_op_bypass(i)            // op == AluOp::Bypass  -> ALU is NOT used
        && has_zero_negated_field(i);
}
```

It is a **fixed-function** 1/x: the ALU is bypassed (`op == Bypass`), there is no reduction
axis, and the src/dst element counts match (pure same-shape element-wise). The dedicated HW
reciprocal datapath is the Vision-Q7 `IVP_RECIP0*` seed plus the `IVP_RECIPQLIN_*` refinement —
see §4. This is distinct from the batch-norm Newton reciprocal/rsqrt Param-RAM machinery and
from the avg-pool host-precomputed `1/N` multiply. [HIGH/OBSERVED — header text + validator.]

**Per-gen.** `// Y` SU/CA/MA/MV; struct 64 B and the reciprocal arm present in all four
`s4d4_tr.h`. [HIGH/OBSERVED.]

### 4. The reciprocal datapath — seed-and-refine (device-ISA grounded)

The RECIPROCAL kernel compiles down to a Vision-Q7 **seed-then-Newton-refine** reciprocal,
byte-grounded by the symbol table of the shipped `ncore2gp` ISS model
`libfiss-base.so` (`nm -D`). The slot-fill symbols place the seed and refinement opcodes in the
**S3_ALU slot 3** of the FLIX bundle, present across every FLIX format variant (F1/F2/F3):

```
slotfill__F3__F3_S3_ALU_slot3__IVP_RECIP0NXF16        // fp16 reciprocal SEED
slotfill__F3__F3_S3_ALU_slot3__IVP_RECIP0N_2XF32      // 2xfp32 reciprocal SEED
slotfill__F3__F3_S3_ALU_slot3__IVP_RECIPQLIN_2XF32_0  // refine stage 0 (quadratic/linear interp)
slotfill__F3__F3_S3_ALU_slot3__IVP_RECIPQLIN_2XF32_1  // refine stage 1
module__xdref_recip0_1_1_16f_16f   /  module__xdref_recip0_1_1_32f_32f   // value-functions
module__xdref_recipqli_1_1_1_1_1_32f_32f                                 // refinement value-fn
```

Reconstructed C pseudocode of the per-element reciprocal:

```c
// Reciprocal(x): IVP_RECIP0 seed + IVP_RECIPQLIN Newton-style refinement.
// (datapath inferred from the libfiss-base symbol roster: recip0 = seed, recipqli = refine.)
float reciprocal_f32(float x) {
    float y0 = ivp_recip0_2xf32(x);        // ~8-bit-accurate seed from a mantissa table
    // RECIPQLIN: quadratic/linear interpolation refinement (two staged corrections):
    float y1 = ivp_recipqlin_2xf32_0(x, y0);   //  y1 = y0 * (2 - x*y0)  family, stage 0
    float y  = ivp_recipqlin_2xf32_1(x, y1);   //  stage 1 polishes to full fp32 precision
    return y;                                  // == 1/x to fp32
}
```

The `recip0` (seed) + `recipqli` (refine) split, the per-precision pairing (`16f`/`32f`), and
the divide path `module__xdref_div0_*`/`module__xdref_divn_*` (reciprocal-then-multiply) are
all present in the ISS model. The numeric `1/x` identity is the documented behaviour of the
Cadence Vision `IVP_RECIP0` seed instruction.
[Symbol existence & naming **HIGH/OBSERVED** (`nm -D libfiss-base.so`); the staged-Newton
refinement *order* **MED/INFERRED**; the `IVP_RECIP0` numeric contract **CARRIED**.]

> **NOTE — value functions not driven live.** The `module__xdref_recip0_*` value functions
> in `libfiss-base.so` take an ISS *processor-state* pointer (the disassembly of
> `opcode__recip0_s__stage_5` reads `%rdi` at offsets `0x3c/0x88/0xd8/…` — the FR/SR register
> file), not plain `float` arguments, and the DLL ships no DWARF to recover that ABI. A
> wrong-ABI `ctypes` call would fabricate a result, so the value semantic is grounded on the
> symbol roster (seed + refine + per-precision pairing) rather than a synthesized invocation.

### 5. `0x49 MEMSET` — fill a 4-D dst region with a constant

**Struct** `D4_MR` ("memset-region", one 4-D dst), 64 B:

| off | field | width | note |
|---|---|---|---|
| 12 | `reserved0[16]` | 16 | 0 |
| 28 | `dst_element_count` (u32) | 4 | redundant element count |
| 32 | `dtype` (`DTYPE`) | 1 | `is_valid_dtype(.., FP32R::True)` |
| 34 | `num_active_channels` | 1 | |
| 36 | `serialization_mode` (`REG_SERIAL_MODE`) | 1 | must be `Serial` |
| 40 | `set_value` (u32) | 4 | **the fill value** |
| 44 | `dst_mem_pattern` (`TENSOR4D`) | 20 | |

`offsetof`: `dst_element_count@28 dtype@32 ser_mode@36 set_value@40 dst@44`, `sizeof=64`.
[HIGH/OBSERVED.]

`D4_MR` is shared by `MEMSET (0x49)`, `RNG (0x4d)`, `REG_STORE (0x4b)`, and — on CAYMAN+ only —
`DVE_READ_INDICES (0xe9)`. The SUNDA `struct2opcode` set is exactly `{MEMSET, REG_STORE, RNG}`
(no `DVE_READ_INDICES`), a shared-struct *validator-set* evolution that does **not** change
MEMSET's own encoding. [HIGH/OBSERVED — `jq` diff of the SUNDA vs maverick map.]

**Dispatch.** In the SUNDA flat Q7 KIT (`pool_memset`); on CAYMAN+ reached via the SEQ ASCII
handler (not in the 17-entry KIT). ⇒ surface `Q7KIT(SU) / SEQ`. [HIGH — KIT carried from
[kernel-info-table](../pool/kernel-info-table.md).]

**Semantics.** Header lines 24–25, verbatim: *"Set a memory region with a given value."* The
fill value is `set_value @40`, written across `num_active_channels`. The legal value/dtype
rules are enforced by `memset_set_value_type`:

```c
// memset_set_value_type(i):  (d4_mr.h:124-140, verbatim sense)
// 4-byte dtypes take the full set_value; narrow dtypes require the high bytes be ZERO.
bool memset_set_value_type(Inst i) {
    Dtype d = i.d4_mr.dtype;
    return  d==FP32 || d==UINT32 || d==INT32                                    // full 32-bit
        || ((d==UINT16 || d==INT16 || d==BFLOAT16 || d==FP16)
            && (i.d4_mr.set_value & 0xffff0000) == 0)                           // hi-16 == 0
        || ((d==UINT8 || d==INT8 || d==FP8_EXP3 || d==FP8_EXP4 || d==FP8_EXP5)
            && (i.d4_mr.set_value & 0xffffff00) == 0);                          // hi-24 == 0
}
// serialization_mode must be Serial:
//   d4_mr_zero_serialization(i): (i.d4_mr.serialization_mode == RegSerialMode::Serial)
```

A region **fill**, not arithmetic and not pooling. The RNG sibling (`0x4d`) header note —
*"common practice to follow an RNG instruction with a normalization … e.g. read 16 random bits
as UINT16, and divide by (2^16-1)"* — is what ties the `0x48`/`0x49`/`0x4d` family together.
[HIGH/OBSERVED.]

> **QUIRK — verbatim source artifacts.** `memset_set_value_type` lists `Dtype::UINT32`
> **twice** in the 4-byte set (a copy-paste in the shipped header), and the helper is named
> `d4_mr_zero_serialization` even though its body asserts `== Serial` (not zero). Neither
> changes behaviour. [HIGH/OBSERVED.]

### 6. `0x67 POOL_BUFFER_LOAD` — fill the pooling-engine gather buffer

**Struct** `S4_PB` ("pool-buffer", one 4-D src), 64 B:

| off | field | width | note |
|---|---|---|---|
| 12 | `src_mem_pattern` (`TENSOR4D`) | 20 | the src to load (≤ 512 elem/channel) |
| 32 | `in_dtype` (`DTYPE`) | 1 | `is_valid_dtype(.., FP32R::False)` |
| 34 | `num_active_channels` | 1 | |
| 40 | `start_index` (u32) | 4 | `pool_buffer_start_index` |
| 44 | `mask` (u32) | 4 | `pool_buffer_mask` (subset-match bits) |
| 48 | `reserved2[16]` | 16 | 0 |

`offsetof`: `src@12 in_dtype@32 start_index@40 mask@44`, `sizeof=64`. [HIGH/OBSERVED.] This is a
single-op struct.

**Dispatch.** In the SUNDA flat Q7 KIT (`pool_pool_buffer_load`); SEQ on CAYMAN+. ⇒
`Q7KIT(SU) / SEQ`. [HIGH — KIT carried.]

**Semantics.** Header lines 20–44, verbatim: *"This instruction loads up to 512 elements per
channel into a storage buffer in the Pooling Engine … all of the param data is brought in as
1/2/4 bytes and stored as raw bytes (no data conversion). Those stored element can then be used
for the Gather instruction."* It is the **load-half** of the gather pair (its consumer is
`Gather 0x68`). `start_index`/`mask` define which gather indices match this subset — the
header's own example: `start_index=0xab400, mask=0x1ff` ⇒ gather indices in `0xab400..0xab5ff`
match.

```c
// is_valid_pool_buffer_load(i): (s4_pb.h:69-87, verbatim sense)
bool is_valid_pool_buffer_load(Inst i) {
    return has_valid_neuron_header(i) && has_valid_neuron_events(i)
        && has_pool_buffer_load_opcode(i) && s4_pb_reserved_zero(i)
        && is_valid_dtype(i.s4_pb.in_dtype, /*FP32R*/ false)
        && start_addr_active_channels(i.s4_pb.src_mem_pattern.start_addr, i.s4_pb.num_active_channels)
        && tensor4d_valid(i.s4_pb.src_mem_pattern, i.s4_pb.in_dtype, /*write*/false,
                          /*PSUM*/false, /*SBUF*/true)                 // SBUF-only
        && valid_pool_buffer_load_element_count(i);
}
// valid_pool_buffer_load_element_count(i): product of the 4 num_elem dims <= 512,
//   OR the shape comes from a register (shape_from_register).
```

Supported dtypes (header line 33): `FP16/BFLOAT16/UINT8/UINT16/FP32/UINT32/INT32`; stored as raw
bytes, so the dtype only sizes the element (1/2/4 B). Class = a buffer **load**, SBUF-only, not
compute. [HIGH/OBSERVED.]

### 7. `0x69 LOAD_MASK_SELECT` — load a 32-byte channel-select mask table

**Struct** `CTRL_IM` (control / immediate-load), 64 B, shared by `LoadParameterRam (0x66)`,
`LOAD_MASK_SELECT (0x69)`, `LoadPoolArgument (0x7a)`, `EngineNop (0x9f)`:

| off | field | width | constraint for mask-select |
|---|---|---|---|
| 12 | `param_ram_offset` (u16) | 2 | `has_zero_pram_offset` ⇒ 0 |
| 14 | `imm_ptr_select` (u8) | 1 | `ctrl_imm_ptr_select_zero` ⇒ 0 |
| 15 | `reserved0[17]` | 17 | 0 |
| 32 | `immediate` (`MOVE_IMMEDIATE`) | 32 | the 32-byte mask payload |

`offsetof`: `param_ram_offset@12 imm_ptr_select@14 immediate@32`, `sizeof=64`. [HIGH/OBSERVED.]

**Dispatch.** Not in any Q7 KIT — the `CTRL_IM` class is a control/argument-load family decoded
on the SEQ control surface (same struct as `EngineNop`/`LoadParameterRam`). [HIGH/OBSERVED — KIT
absence; SEQ-control class.]

**Semantics.** Writes a 32-entry channel-select MASK table from `immediate[0..31]` into the
pooling engine config state. Each byte is range-validated:

```c
// is_valid_load_mask_select(i): (ctrl_im.h:54-63)
//   has_zero_pram_offset && has_valid_masksel_data && ctrl_im_reserved_zero
//   && ctrl_imm_ptr_select_zero        (imm_ptr_select MUST be 0 -> distinguishes from LoadPoolArgument)
// valid_masksel_byte(b):  (ctrl_im.h:173-176)
bool valid_masksel_byte(uint8_t b) { return (b < 64) || (b == 255); }
// has_valid_masksel_data(i): valid_masksel_byte applied to all 32 immediate.uint8[0..31], AND-ed.
```

Interpretation: 32 select-indices, one per output channel; a value `0..63` selects a source
channel (the POOL channel range is 64-wide), and `255` is the disabled/pass sentinel. A
config/argument **load**, not compute. The natural consumer is a downstream channel select/permute
(plausibly StreamShuffle `0x6a` or AffineSelect `0x92`), but that consumer edge is not
byte-traced. [Struct+validator **HIGH/OBSERVED**; per-value meaning + consumer link
**MED/INFERRED**.]

### 8. `0x6a STREAM_SHUFFLE` — cross-channel lane-permute within 32 channels

**Struct** `S4D4_TR` — the *same* 64-B struct as RECIPROCAL (§3), same field layout. [HIGH/OBSERVED.]

**Dispatch.** Not in any Q7 KIT; SEQ ASCII dispatch. Same `0xF0`-bridge caveat as `0x48` (shares
the struct with Copy/Cast, which do hold KIT rows). [HIGH/OBSERVED for KIT absence; bridge
MED/INFERRED.]

**Semantics.** Header line 21, verbatim: *"StreamShuffle — allows cross-channel data movement
within a set of 32 channels."* The validator is RECIPROCAL's arm plus two extra constraints — a
same-type requirement and a 32-channel-multiple requirement:

```c
// is_valid_stream_shuffle(i): (s4d4_tr.h:310-331, the deltas vs reciprocal)
bool is_valid_stream_shuffle(Inst i) {
    return /* ...the reciprocal common gates... */
           s4d4_tr_op_bypass(i)                      // op == Bypass  -> NO arithmetic
        && s4d4_tr_same_src_dst_type(i)              // in_dtype == out_dtype  (same-type permute)
        && s4d4_tr_same_src_dst_count(i)
        && has_multiple_32_channels(i.s4d4_tr.num_active_channels)  // channels in {32,64,96,128}
        && has_zero_negated_field(i) && is_zero_subdim(i.s4d4_tr.op_dim);
}
```

A pure **data-movement** lane-permute: it reorders elements across the 32-channel group with no
type change and no arithmetic. Its same-struct sibling StreamTranspose (`0x6b`) is the 32×32
transpose; StreamShuffle is the in-group permute. The underlying device primitive is the
`xdref_dsel*`/`xdref_sel*` (`ivp_dselnx16t`/`ivp_selnx16t`) lane-select family in
`libfiss-base.so` (semantic match, not a byte-traced dispatch edge). [Header+validator
**HIGH/OBSERVED**; execution primitive **MED/INFERRED**.]

---

## Cluster B — predicated copy, address arith, control loads, transposed arith, engine config

### 9. `0x72 COPY_PREDICATED` — the predicated-op family base (DVE-native)

**Struct** `S3S3D3_TT` (64 B), shared with `TensorTensorArith (0x41)`, `TensorTensorBitvec`,
`CopyPredicated (0x72)`, `CastPredicated (0x99)`, `BatchNormBackProp`:

| off | field | width | role in CopyPredicated |
|---|---|---|---|
| 12 | `in0_in1_dtype` (`DTYPE_PAIR`) | 1 | `dtype_lo` = src0 **predicate** dtype; `dtype_hi` = src1 **data** dtype |
| 13 | `out_dtype` (`DTYPE`) | 1 | must equal `dtype_hi` (copy cannot change dtype) |
| 14 | `op` (`ALU_OP`) | 1 | `s3s3d3_tt_is_zero_op` ⇒ `Bypass` |
| 15 | `num_active_channels` | 1 | |
| 16 | `src0_mem_pattern` | 16 | predicate mask tensor (read) |
| 32 | `src1_mem_pattern` | 16 | data tensor (read) |
| 48 | `dst_mem_pattern` | 16 | dst (write, **merge**) |

`offsetof`: `in0_in1_dtype@12 out@13 op@14 src0@16 src1@32 dst@48`, `sizeof=64`. [HIGH/OBSERVED.]

**Dispatch — DVE, hardware-native.** Absent from both the SUNDA-18 and CAYMAN-17 Q7 KITs; the
`S: CopyPredicated\n` token appears **4×** co-resident with `S: CastPredicated` in the four
`NX_DVE_DEBUG` blobs (§2). This **corrects** an earlier ledger entry that tagged `0x72` as POOL.
[HIGH/OBSERVED.]

**Semantics — predicate-gated, dtype-preserving copy (the family base):**

```c
// CopyPredicated:  per output element e:
for (e : elements) {
    int p = src0_pred[e];                 // integer predicate, dtype in {I/U 8/16/32}
    if (p != 0) dst[e] = src1_data[e];    // dtype-preserving copy (op == Bypass, NO cast)
    // else:    dst[e] retains its prior value  (vbool bitkillt MERGE, not zero-fill)
}
// is_valid_copy_cast_predicated(i): (s3s3d3_tt.h:101-125) gates:
//   s3s3d3_copy_pred_src0_dtype(i):  is_valid_int_dtype_datapath(dtype_lo)   // src0 = integer predicate
//   s3s3d3_copy_cast_pred_src1_dst_dtype(i):                                 // (s3s3d3_tt.h:201-204)
//       (opcode == CastPredicated)                  //  cast: may change dtype (short-circuit true)
//       || (dtype_hi == out_dtype)                  //  COPY: src1 dtype MUST equal out dtype
//   s3s3d3_tt_is_zero_op(i):  op == AluOp::Bypass    // inline comment: "Cast can change dtype, copy cannot"
```

The only difference from `CastPredicated (0x99)` is the `s3s3d3_copy_cast_pred_src1_dst_dtype`
clause: COPY requires `dtype_hi == out_dtype`; CAST short-circuits it (permits a cast via the
FP32 hub) — exactly the plain Copy `0x46` vs Cast `0x47` split. `0x72` is the minimal member of
the predicated-op family: predicate-gated copy, no cast, no reduce, two tensors (data + mask).
[Validator **HIGH/OBSERVED**; the per-lane `selnx16t`/`bitkillt` merge body **MED/INFERRED** from
the DVE co-residence — body not byte-traced.]

The family, now closed:

| Op | Name | Struct | cast? | reduce? | Page |
|---|---|---|---|---|---|
| `0x72` | `COPY_PREDICATED` | `S3S3D3_TT` | no | no | *(this page)* |
| `0x99` | `CAST_PREDICATED` | `S3S3D3_TT` | yes | no | [castpredicated.md](castpredicated.md) |
| `0xe8` | `COPY_PREDICATED_SCALAR` | `S3D3_CP_PRED_SCALAR` | no | no | [copypredicatedscalar.md](copypredicatedscalar.md) |
| `0xea` | `SELECT_REDUCE` (dev. `CopyPredicatedReduce`) | `S2S2D2_STT` | no | MAX | [copypredicatedreduce.md](copypredicatedreduce.md) |

All four are DVE-native, src0-integer-predicate, vbool `_t` bitkillt MERGE. [HIGH/OBSERVED.]

### 10. `0x74 TENSOR_SCALAR_ADDR` — 64-bit integer address arithmetic (POOL)

**Struct** `S2D2_ADDR` (its own struct, sole member), 64 B:

| off | field | width | note |
|---|---|---|---|
| 12 | `src_mem_pattern` (`TENSOR2D`) | 12 | indices/offsets, SBUF-only |
| 24 | `imm0` (`IMM64`) | 8 | 64-bit immediate or pointer |
| 32 | `imm1` (`IMM64`) | 8 | 64-bit immediate or pointer |
| 40 | `imm0_imm1_dtype` (`DTYPE_PAIR`) | 1 | imm0 lo / imm1 hi |
| 41 | `in_out_dtype` (`DTYPE_PAIR`) | 1 | in lo / out hi |
| 42 | `imm0_is_ptr` (`IMM_SRC`) | 1 | `{Inst=0, PtrImm=1, RegPtr=2}` |
| 43 | `imm1_is_ptr` (`IMM_SRC`) | 1 | |
| 44 | `op0` (`ALU_OP`) | 1 | |
| 45 | `op1` (`ALU_OP`) | 1 | |
| 46 | `num_active_channels` | 1 | ≤ `POOLING_NUM_CHANNELS` (=128) |
| 47 | `reverse_operands` (`TENS_SCALAR_REV_OPS`) | 1 | |
| 52 | `dst_mem_pattern` (`TENSOR2D`) | 12 | result addrs/mask, SBUF-only |

`offsetof`: `src@12 imm0@24 imm1@32 op0@44 op1@45 nac@46 rev@47 dst@52`, `sizeof=64`.
[HIGH/OBSERVED.]

**Dispatch.** In the SUNDA Q7 KIT; 3× POOL self-name on CAYMAN+ (§2). Both src and dst are
`AllowedInPSUM::False` — the header states *"Neuron POOL cannot access PSUM"* (line 287).
[HIGH/OBSERVED.]

**Semantics.** Header: *"Designed for 64 bit address calculations. Specialized version of
TensorScalar/TensorScalarPtr."* The base formula is the TensorScalar
`dst[i] = ((src[i] op0 imm0) op1 imm1)`, but `ts_addr_valid_ops` hard-pins it to **exactly five
integer op/dtype cases**:

```c
// ts_addr_valid_ops(i): (s2d2_addr.h:190-223, verbatim cases)
//  A) table index:   addr<u64,dst> = base<u64,imm1> + index<i32,src> * elem_size<i32,imm0>
//                    op0=Mult, op1=Add ;  in=i32 out=u64
//  B) base+offset:   addr<u64,dst> = base<u64,imm0> + scaled_index<i32,src>
//                    op0=Add,  op1=Bypass ; imm1 = InstructionImmediate ; in=i32 out=u64
//  C) scale index:   scaled<i32,dst> = scale<i32,imm0> * index0<i32,src> [+ index1<i32,imm1>]
//                    op0=Mult, op1=Bypass|Add ; in=i32 out=i32
//  D) u64 range chk: in_range<dst> = (addr<u64,src> - min<u64,imm0>) < size<u64,imm1>
//                    op0=Subtract, op1=IsLT ; in=u64 ; out = any int datapath (NOT u64/i64)
//  E) u32 range chk: same as D but addr/imm0/imm1 are u32 ; in=u32
// ts_addr_valid_types(i):  imm0/imm1, in/out each in {UINT32, INT32, UINT64, INT64}
//                          (out also: is_valid_int_dtype_datapath -> I/U 8/16/32)
```

Cases A/B/C turn a tensor of indices/offsets into a tensor of 64-bit (or 32-bit) memory
addresses; cases D/E produce a range-check boolean mask. It is the **index→address staging op**
feeding the gather/scatter/indirect-copy POOL primitives (Gather `0x68`, IndirectCopy `0xe7`,
EmbeddingUpdate `0x79`). Class = integer address arithmetic, not float compute. Per-imm
`IMM_SRC` chooses inline u64/i64 immediate, SBUF/PSUM pointer, or register-held pointer.
[HIGH/OBSERVED — the 5 cases are verbatim header pseudocode.]

### 11. `0x7a LOAD_POOL_ARGUMENT` — load 8 argument words into pool state (POOL control)

**Struct** `CTRL_IM` (the same 64-B control-immediate struct as `0x69`; §7). For LoadPoolArgument
the `imm_ptr_select @14` u8 is an **8-bit per-slot mode mask**, and `immediate @32` is `8 × u32`.
[HIGH/OBSERVED.]

**Dispatch.** In the SUNDA Q7 KIT; 3× POOL self-name on CAYMAN+ (§2). ⇒ `Q7KIT(SU)+POOL / POOL`.
[HIGH/OBSERVED.]

**Semantics.** Loads up to 8 "pool argument" words — each chosen per-slot as a direct inline
value or an SBUF pointer — into the pool engine's per-instruction argument state, staging
parameters for a subsequent POOL kernel. A **control/setup** op, not element-wise compute:

```c
// is_valid_load_pool_argument(i): (ctrl_im.h:65-73)
//   has_zero_pram_offset && ld_pool_arg_has_valid_imm_ptrs && ctrl_im_reserved_zero
//   (NOTE: does NOT require imm_ptr_select == 0 -- unlike LoadMaskSelect/LoadParameterRam --
//    because imm_ptr_select is a REAL per-slot pointer-mode bitmask here.)
// for slot index = 0..7:
bool lpa_inst_imm(Inst i, uint8_t idx)   { return ((i.ctrl_im.imm_ptr_select >> idx) & 1) == 0; }
//   bit[idx]==0 -> slot is a DIRECT inline value ; bit[idx]==1 -> slot is an SBUF POINTER
bool lpa_valid_imm_ptr(Inst i, uint8_t idx) {
    uint32_t a = i.ctrl_im.immediate.uint32[idx];
    return addr_aligned_dtype(a, UINT32) && addr_in_sbuf(a) && tpb_addr_active_channels(a, 1);
}
// ld_pool_arg_has_valid_imm_ptrs(i): for each slot, lpa_inst_imm(i,idx) || lpa_valid_imm_ptr(i,idx)
```

Contrast with its `CTRL_IM` siblings: `LoadParameterRam (0x66)` uses `param_ram_offset`;
`LoadMaskSelect (0x69)` carries 32 masksel bytes with `imm_ptr_select == 0`; `EngineNop (0x9f)`
carries no immediates. [HIGH/OBSERVED.]

### 12. `0x93 TRANSPOSE_TENSOR_SCALAR_ARITH_OP` — TensorScalar arith with a transposed source

**Struct** `S3D3_TS` (64 B), shared with `TensorScalarArith (0x43)`, `TensorScalarBitvec`,
`TensorScalarPtr*`, `Exponential`, `TensorScalarCache{Reduce,Cumulative}`. Field offsets:
`accumulator_cmd@12 src@16 in_dtype@32 out_dtype@33 nac@34 op0@36 op1@37 rev@38 imm0@40 imm1@44
dst@48`, `sizeof=64`. [HIGH/OBSERVED.]

**Dispatch.** Has **no** dedicated self-name (`0× S: TransposeTensorScalar*`, confirmed this
analysis) and is absent from both Q7 KITs. It shares the `is_valid_tensor_scalar_op` validator
with the regular `TensorScalarArith (0x43)` and is handled by the **same TensorScalar datapath**,
the transpose being a source-pattern variant rather than a separate kernel — hence no separate
self-name and no separate KIT row. [Struct+validator **HIGH/OBSERVED**; exact host worker
(DVE Tensor-Scalar vs a POOL transpose path) **MED/INFERRED** — no surface token.]

**Semantics.** The TensorScalar arith `op1(op0(tensor, scalar0), scalar1)` with the source read
in a **transposed** access pattern. Two extra constraints, unique to `0x93`:

```c
// s3d3_transpose_check(i): (s3d3_ts.h:440-444, verbatim)
bool s3d3_transpose_check(Inst i) {
    return (i.header.opcode != TransposeTensorScalarArithOp)              // non-transpose ops: true
        || (has_transpose_src_element_count_m3d(i.s3d3_ts.src_mem_pattern)  // src must be transpose-shaped
            && has_multiple_32_channels(i.s3d3_ts.num_active_channels));    // channels in {32,64,96,128}
}
// tensor_scalar_arith(i) (s3d3_ts.h:249-256) lists TransposeTensorScalarArithOp among the
//   ARITH (not bitvec) opcodes -> 0x93 is arith-only (there is NO transpose-bitvec opcode).
```

Everything else (AluOp acceptance, dtype rules, `reverse_operands`, per-imm `IMM_SRC`) is
identical to `TensorScalarArith` — only the source is read transposed and the channel count is
32-aligned. `in_dtype` `FP32R::False`, `out_dtype` `FP32R::True`. [HIGH/OBSERVED.]

### 13. `0x95 MODIFY_POOL_CONFIG` — ucode-library load/unload (POOL engine config)

**Struct** `MODIFY_POOL_CONFIG` (its own struct, sole member), 64 B:

| off | field | width | note |
|---|---|---|---|
| 12 | `modify_op` (`MODIFY_POOL_OP`) | 1 | `{Invalid=0, LoadLib=1, UnloadLib=2}` |
| 13 | `core_mask` (u8) | 1 | which Q7 cores to load/unload on |
| 16 | `soc_addr` (u64) | 8 | 64-bit HBM base address of the library |
| 24 | `library_index` (u32) | 4 | unique library identifier |
| 28 | `library_size` (u32) | 4 | ≤ `64 * 1024` (64 KiB) |
| 32 | `reserved1[32]` | 32 | 0 |

`offsetof`: `modify_op@12 core_mask@13 soc_addr@16 library_index@24 library_size@28`,
`sizeof=64`. [HIGH/OBSERVED.]

> **CORRECTION — `library_size` vs the ASCII-layout comment.** The in-header ASCII layout
> comment labels offset 28 as `reserved0[u8;4]`, but the actual C struct (line 69) declares
> `uint32_t library_size` there, and the validator `has_valid_modify_pool_config_library_size`
> bounds `library_size <= 64*1024`. The struct + validator are authoritative: **offset 28 =
> `library_size`**, not reserved. (Present in all four gens.) [HIGH/OBSERVED — `gcc` + validator.]

**Dispatch.** 3× POOL self-name (§2); absent from both Q7 KITs (a POOL front-end control
instruction, not a compute KIT kernel). [HIGH/OBSERVED.]

**Semantics.** Header purpose block, verbatim: *"intended to be used for NRT to … change the
POOL engine's config between model switches … this instruction can also be used at the start of
model execution to trigger a load of any ucode library (extisa or custom-op), so that the load
latency … can be hidden behind any other ops."*

```c
// is_valid_modify_pool_config(i):
//   valid header/events && has_modify_pool_config_opcode
//   && modify_op != Invalid && library_size <= 64*1024 && reserved zero
// LoadLib(1):  map a ucode library from HBM (soc_addr, library_index, library_size)
//              onto the cores selected by core_mask, ahead of a model switch.
// UnloadLib(2): evict it BEFORE the switch (the gap the instruction closes).
```

Engine config / dynamic-library management, not compute. [HIGH/OBSERVED — header + the
`MODIFY_POOL_OP` enum + validator.]

---

## Custom-op pair — the marshalling envelope (not POOL arithmetic)

`0x85`/`0x86` sit in the `0x8x` band that the queue model attributes to the POOL engine, but
their **name and struct** are custom-op marshalling, not POOL compute. They are how the GPSIMD
custom-op subsystem encodes a call into a customer C++ op. They carry **no** compute dtype field
and perform **no** tensor math. The struct + the `CUSTOM_OP_ARG_TYPE/LOCATION/UNION` enum block
exist **only** in the SUNDA arch-isa (the format was frozen at v2); CAYMAN+ keep the enum value
`// Y` but ship no struct. [HIGH/OBSERVED — `fd`: 2 `custom_op*.h` under sunda, 0 elsewhere.]

### 14. `0x85 CUSTOM_OP_HEADER` — `CUSTOM_OP_HEADER_STRUCT` (64 B, SUNDA-only)

| off | field | width | note |
|---|---|---|---|
| 12 | `num_payloads` (u16) | 2 | # of following `0x86` blocks |
| 14 | `function_id` (u8) | 1 | compiler-assigned; RT/ucode resolves it to the library to call |
| 15 | `num_arguments` (u8) | 1 | # C++ input args (≠ `num_payloads`: a `tuple<Tensor,Tensor>` = 3 payloads) |
| 16 | `has_scratch_space` (u8) | 1 | |
| 20 | `scratch_space_addr` (`TPBADDR`) | 4 | |
| 24 | `scratch_space_size` (u32) | 4 | size **per partition** |
| 28 | `scratch_space_num_partitions` (u8) | 1 | |
| 32 | `reserved2[32]` | 32 | 0 |

`offsetof`: `num_payloads@12 function_id@14 num_arguments@15 has_scratch_space@16
scratch_space_addr@20 scratch_space_size@24 scratch_space_num_partitions@28`, `sizeof=64`.
[HIGH/OBSERVED.]

Validator `is_valid_custom_op_header`: valid header/events + CustomOpHeader opcode + reserved
zero + `scratch_space_valid` (`has_scratch_space == 0` ⟺ addr/size/num_partitions all 0; else all
non-zero). The HEADER is the first 64 B of a multi-64 B custom-op instruction: one HEADER + N
PAYLOAD blocks. Dispatch: the runtime/ucode reads the HEADER, resolves `function_id` to an
external custom-op library entry, copies the payload args, and calls the customer C++ op.
[Struct/validator/semantics **HIGH/OBSERVED**; the ucode external-lib dispatch path
**MED/INFERRED** — not byte-traced.]

### 15. `0x86 CUSTOM_OP_PAYLOAD` — `CUSTOM_OP_PAYLOAD_STRUCT` (64 B, SUNDA-only)

| off | field | width | note |
|---|---|---|---|
| 12 | `reserved0[3]` | 3 | 0 |
| 15 | `arg_type` (`CUSTOM_OP_ARG_TYPE`) | 1 | `{INVALID=0, TENSOR=1, ARRAY_OF_TENSOR=2}` |
| 16 | `arg` (`CUSTOM_OP_ARG_UNION`) | 48 | opaque `{tensor \| array_of_tensor}`; `tensor.location {SBUF=1, HBM=2}` |

`offsetof`: `arg_type@15 arg@16`, `sizeof(arg union)=48`, `sizeof(struct)=64`. [HIGH/OBSERVED.]

Each payload carries **one** argument; per the header comment, output arguments precede input
arguments, and `ARRAY_OF_TENSOR` is a meta-type recording the array's tensor count. The header's
own tag reads `… / POOL / CUSTOM_OP_PAYLOAD` — the only sense in which the pair is "POOL" is that
it is issued into the POOL engine's instruction queue. No POOL arithmetic is performed.
[HIGH/OBSERVED.]

---

## ACT pair — the NC-v4/v5 fused activation, and the v5 engine fold

`0x25`/`0x26` are activation-engine opcodes in the ACT block `0x21–0x26`. They are the NC-v4/v5
evolution of base `ACTIVATE 0x21`; their `activation_func` byte indexes the same PWL
breakpoint/slope table that `ACTIVATION_TABLE_LOAD 0x23` installs — see
[Activate-PWL](activate-pwl.md) for the table format. Both are gated on `nc >= V4`.

### 16. `0x25 ACTIVATE2` — fused activation + dual-ALU + reduce, 2-D (`S2D2_AC`, 64 B)

| off | field | width | note |
|---|---|---|---|
| 12 | `src_mem_pattern` (`MEM_PATTERN2D`) | 12 | |
| 24 | `relu_param_src` (`IMM_SRC`) | 1 | 3rd-immediate source |
| 25 | `imm0_src` (`IMM_SRC`) | 1 | |
| 26 | `reduce_cmd` (`REDUCE_CMD`) | 1 | `{Idle, Reset, Reduce, ResetReduce}` |
| 27 | `imm1_src` (`IMM_SRC`) | 1 | |
| 28 | `imm_dtype` (`DTYPE`) | 1 | imm0/imm1/relu_param share this dtype |
| 29 | `op0` (`ALU_OP`) | 1 | fused tensor-scalar op 0 |
| 30 | `op1` (`ALU_OP`) | 1 | fused tensor-scalar op 1 |
| 31 | `reduce_op` (`ALU_OP`) | 1 | reduction op |
| 32 | `in_dtype` (`DTYPE`) | 1 | `FP32R::False` |
| 33 | `out_dtype` (`DTYPE`) | 1 | `FP32R::True` |
| 34 | `num_active_channels` | 1 | ≤ `DVE_NUM_CHANNELS` (=128) |
| 35 | `activation_func` (u8) | 1 | **index into the loaded PWL table** |
| 36 | `imm0` | 4 | |
| 40 | `imm1` | 4 | |
| 44 | `relu_param` | 4 | |
| 48 | `dst_mem_pattern` (`MEM_PATTERN2D`) | 12 | |
| 60 | `reverse_operands` (`TENS_SCALAR_REV_OPS`) | 1 | |

`offsetof`: `reduce_cmd@26 activation_func@35 imm0@36 relu_param@44 dst@48`, `sizeof=64`.
[HIGH/OBSERVED.]

**Semantics.** Like base ACTIVATE it applies a scalar activation FUNCTION (selected by
`activation_func`, evaluated by the HW PWL datapath) to a tensor, but on a **2-D** pattern and
with a fused dual tensor-scalar ALU stage (`op0`,`op1` with `imm0`/`imm1`), an integrated
reduction (`reduce_cmd`+`reduce_op`), and a `relu_param`. The validator constrains the op pairs
and reduction tightly:

```c
// is_valid_activate2(i, nc): (s2d2_ac.h:58-86) key gates:
//   has_valid_activate2_nc(nc): nc >= NeuronCoreVersion::V4
//   src/dst mem2d valid in BOTH PSUM and SBUF (AllowedInPSUM::True)
//   in_dtype FP32R::False ; out_dtype FP32R::True ; imm_dtype FP32R::False
//   3 immediates (imm0, imm1, relu_param), each has_valid_activation_immediate
//   channel check vs DVE_NUM_CHANNELS
// has_valid_activation_ts_ops(op0,op1): (common.h:1863-1876) -- exactly 6 legal pairs:
//   (Mult,Add) (Mult,Subtract) (Mult,Bypass) (Add,Bypass) (Subtract,Bypass) (Bypass,Bypass)
// has_valid_reduce_op(reduce_op, reduce_cmd): (common.h:1887-1892)
//   (reduce_cmd==Idle && reduce_op==0) || (reduce_cmd!=Idle && reduce_op!=0)
```

⇒ ACTIVATE2 fuses `{affine via op0/op1}` + `{activation_func PWL}` + `{reduce}` into one pass.
The exact affine-then-PWL-then-reduce ordering needs a device-body decode (deferred). [Struct +
validator **HIGH/OBSERVED**; the fusion *order* **MED/INFERRED**.]

### 17. `0x26 ACTIVATE_MULTIPASS` — multi-pass activation with a prev-pass accumulator (`S1S2D2_AM`, 64 B)

| off | field | width | delta vs ACTIVATE2 |
|---|---|---|---|
| 24 | `imm0_src` (`IMM_SRC`) | 1 | (no `relu_param_src`) |
| 25 | `reduce_cmd` (`REDUCE_CMD`) | 1 | |
| 27 | `imm_dtype` (`DTYPE`) | 1 | |
| 28 | `op0` / 29 `op1` / 30 `reduce_op` (`ALU_OP`) | 1 ea | |
| 31 | `in_dtype` / 32 `out_dtype` (`DTYPE`) | 1 ea | |
| 33 | `num_active_channels` | 1 | ≤ `DVE_NUM_CHANNELS` |
| 34 | `activation_func` (u8) | 1 | PWL table index |
| 35 | `reverse_operands` | 1 | |
| 36 | `imm0` | 4 | |
| 40 | `prev_pass_mem_pattern` (`TENSOR1D`) | 8 | **the multipass feature: prev-pass accumulator input** |
| 48 | `dst_mem_pattern` (`MEM_PATTERN2D`) | 12 | |
| 60 | `imm1` | 4 | |

`offsetof`: `activation_func@34 prev_pass_mem_pattern@40 imm1@60`, `sizeof=64`. [HIGH/OBSERVED.]

Deltas vs ACTIVATE2: (i) **adds** a 1-D `prev_pass_mem_pattern @40` — the accumulator carried
between passes; (ii) **drops** the 3rd immediate (`relu_param`); (iii) src, dst, **and** prev_pass
are **SBUF-only** (`AllowedInPSUM::False` on all three — ACTIVATE2 allowed PSUM); (iv) prev_pass
`start_addr` aligned to `num_active_channels`, typed with `out_dtype`. The validator floor is
`nc >= V4`, though the opcode only **ships on MAVERICK (NC-v5)**.

```c
// "Multipass": activation streamed in multiple passes over a tensor too large for one PSUM pass.
// Each pass reads the prior pass's 1D accumulator (prev_pass) and folds it (reduce_cmd
// REDUCE/RESET_REDUCE) into the running activation + reduction, writing SBUF.
// The SBUF-only constraint is consistent with a software/DVE-driven multi-pass loop rather
// than the single-shot PSUM-drain of 0x21/0x25.
```

[Struct + validator **HIGH/OBSERVED**; the streaming semantics **MED/INFERRED** from the struct
shape + the SBUF-only + reduce_cmd IR.]

### 18. The MAVERICK ACT→DVE fold (no opcode move)

Does `0x25`/`0x26` move to DVE on MAVERICK? **No opcode move** — both keep their ACT opcode
bytes. But there is a real **engine/scheduling migration** of `0x25`:

- **MARIANA (v4):** `0x25` has its own named ACT SEQ handler.
- **MAVERICK (v5):** the entire ACT-specific named handler set
  (`Activate*`/`ActivateQuantize`/`ActivationTableLoad`/`ActivationReadAccumulator`) is **absent
  firmware-wide**, and the MAVERICK DVE PROF_CAM arms `0x23 + 0x25` — i.e. ACTIVATE2 is now
  scheduled/decoded on the **DVE engine lane**, with no separate ACT handler image.
- `0x26 ACTIVATE_MULTIPASS` is **not** in the MAVERICK DVE PROF arm list and has **0× self-name**
  (verified this analysis) — spec-present (enum+struct+validator) but **image-dormant** in the
  shipped carves.
- Microarch corroboration: `0x21`/`0x25`/`0x26` all validate channels against
  `DVE_NUM_CHANNELS` — the activation family was already sized to the DVE channel grid at NC-v4+,
  consistent with the v5 fold.

[Opcode-stable + `0x25`-on-DVE + `0x26`-dormant **HIGH/OBSERVED**; the `DVE_NUM_CHANNELS` causal
reading **MED/INFERRED**.]

> **NOTE — empirical self-name counts.** `Activate2` resolves **2×** (not the full POOL/ACT 3×)
> in `libnrtucode_internal.so` (`0x4050a0`/`0x6cba20`), `Activate` and `ActivationTableLoad`
> resolve **3×**, and `ActivateMultipass` resolves **0×**. The `2×` for ACTIVATE2 reflects its
> presence in two engine images (the MARIANA ACT handler and one other) and absence from the
> third, consistent with the v4→v5 transition. [HIGH/OBSERVED.]

The PROFILER (`PROF_CAM`) is the per-engine HW-decode instruction profiler — it *arms* which
opcodes get decode-profiled. It is **not** the activation PWL lookup, which is the separate
ACT-only DMA-staged table (see [Activate-PWL](activate-pwl.md)).

---

## 19. Per-generation presence summary

All fourteen opcodes are `// Y` at the ISA-header (roster) level in the generations shown; the
cluster-A/B and custom-op rows are `YYYY` (every gen), the ACT rows are gated. Struct wire
layouts are generation-invariant (SUNDA == MAVERICK, 64 B, same field offsets — `gcc`-verified
on both); the only per-gen drift in a *shared* struct is `D4_MR` gaining a 4th validator member
(`DveReadIndices 0xe9`, CAYMAN+) and the channel check moving `POOLING_→DVE_NUM_CHANNELS`
(both = 128) — not a change to any of these ops' own encodings.

| Op | SU (v2) | CA (v3) | MA (v4) | MV (v5) | device self-name residency |
|---|---|---|---|---|---|
| `0x48`/`0x49`/`0x67`/`0x69`/`0x6a` | `// Y` + struct | `// Y` + struct | `// Y` + struct | `// Y` + struct | none in this blob (KIT/SEQ-decoded) |
| `0x72` `COPY_PREDICATED` | header (RELEASE-stripped) | DVE DEBUG | DVE DEBUG | DVE DEBUG | DVE ×4 |
| `0x74`/`0x7a`/`0x95` | header + Q7 KIT (0x74/0x7a) | NX_POOL DEBUG | NX_POOL DEBUG | NX_POOL DEBUG | POOL ×3 |
| `0x93` | header | TS-folded | TS-folded | TS-folded | none (TensorScalar-folded) |
| `0x85`/`0x86` | enum + struct | enum-only | enum-only | enum-only | none (marshalling) |
| `0x25` `ACTIVATE2` | absent | absent | `// Y` + `S2D2_AC` | `// Y` + `S2D2_AC` (→DVE) | ×2 |
| `0x26` `ACTIVATE_MULTIPASS` | absent | absent | absent | `// Y` + `S1S2D2_AM` | ×0 (dormant) |

SUNDA device-string absence for the engine-native ops is a RELEASE-build artifact (SUNDA ships
`SUNDA_*_RELEASE_*` only); presence rests on the header. [Opcode+struct **HIGH/OBSERVED** all
gens; SUNDA self-name absence **MED/INFERRED** (RELEASE).]

---

## 20. Honesty ledger

**HIGH / OBSERVED (read/compiled/byte-scanned this analysis).**

- All fourteen names + `// Y` flags re-grepped byte-exact from the four `common.h` enums
  (line refs §1); the `0x49`/`0x93`/`0x95` non-opcode enum aliases flagged.
- All fourteen `struct2opcode` bindings via `jq`; all fourteen structs `gcc`-compiled to
  `sizeof=64` (maverick + sunda) with `offsetof` matching the header columns. `CUSTOM_OP_ARG_UNION
  = 48 B`. SUNDA `D4_MR` set = `{MEMSET, REG_STORE, RNG}` (no `DVE_READ_INDICES`).
- Dispatch surfaces: `S: <Name>\n` exact-token byte counts — `CopyPredicated ×4` (DVE,
  `0x18dc20`+ inside `CAYMAN_NX_DVE_DEBUG_DRAM`), `TensorScalarAddr`/`LoadPoolArgument`/
  `ModifyPoolConfig ×3` (POOL, `0x1d0xxx`+ inside `CAYMAN_NX_POOL_DEBUG_DRAM`), `Activate2 ×2`,
  `ActivateMultipass ×0`, cluster-A `×0`, `0x93 ×0`. `nm -S` blob ranges quoted §2.
- Semantics from verbatim header text + in-header validator pseudocode: reciprocal
  (`op==Bypass`, no reduce), memset (`set_value` + dtype masks + `==Serial`), pool_buffer_load
  (≤512/ch, `start_index`+`mask`, feeds Gather), load_mask_select (`byte<64 \|\| ==255`),
  stream_shuffle (`op==Bypass`, same-type, mult-of-32 channels), copy_predicated (the
  copy-vs-cast dtype clause + `op==Bypass`), tensor_scalar_addr (the 5 integer cases),
  load_pool_argument (8-slot inline/pointer `imm_ptr_select` bitmask), transpose-tensor-scalar
  (`s3d3_transpose_check`), modify_pool_config (`{Invalid/LoadLib/UnloadLib}` + 64 KiB cap),
  the custom-op pair (function_id + arg union), activate2/multipass (the activation+ALU+reduce
  validator, `has_valid_activation_ts_ops` 6 pairs, `has_valid_reduce_op`).
- The reciprocal seed-and-refine datapath: `IVP_RECIP0*` seed + `IVP_RECIPQLIN_*` refinement
  slot-3 ALU symbols in `libfiss-base.so` (`nm -D`).
- The MAVERICK ACT→DVE fold: opcode-stable, `0x25` DVE-armed, `0x26` `0×` self-name.

**HIGH / CARRIED (from a prior cross-referenced decode).**

- The SUNDA 18-entry flat Q7 KIT and CAYMAN+ 17-entry EXTISA KIT contents (the SUNDA EXTISA
  container `SUNDA_Q7_POOL_RELEASE_EXTISA_0_SO/JSON` is partially out-of-corpus); the avg/max
  pool = `0x45`/`0x58` distinction; the `IVP_RECIP0` numeric `1/x` contract. See
  [kernel-info-table](../pool/kernel-info-table.md).

**MED / INFERRED.**

- The per-lane `selnx16t`/`bitkillt` merge body of `0x72`; the staged-Newton refinement *order*
  of the reciprocal; the `0x6a` execution primitive (`xdref_dsel*`/`xdref_sel*`); the
  load_mask_select per-value meaning + consumer link; `0x93`'s exact host worker; the
  `0x85`/`0x86` ucode external-lib dispatch path; the `0x25`/`0x26` op0/op1/reduce/PWL fusion
  order; the `DVE_NUM_CHANNELS ⇒ v5 fold` causal reading; SUNDA device-string absence = RELEASE
  stripping. The `0xF0`-ExtendedInst-bridge possibility for `0x48`/`0x6a`.

**LOW / NOT CLAIMED.**

- Any dispatch slot for `0x26` (image-dormant); whether the MAVERICK DVE lane physically reuses
  or replaces the ACT PWL HW datapath (→ a future ACT-PWL datapath expansion); the in-kernel
  FLIX micro-schedule of any compute body (not disassembled — the struct + validator + header
  semantics are the basis); whether `0x95` also has a dual SEQ front-end inline handler.

**Live value-function caveat.** The `libfiss-base.so` `xdref` reciprocal value functions take an
ISS processor-state pointer (no DWARF to recover the ABI), so the reciprocal value semantic is
grounded on the symbol roster (seed + refine + per-precision pairing) rather than a synthesized
`ctypes` call — see the §4 note.

No FLIX desync was entered: every primary fact rests on a `common.h` enum line, a `gcc`-verified
struct offset, an in-header validator arm, a verbatim header comment, a `libnrtucode_internal.so`
self-name byte offset / `nm -S` range, or a `libfiss-base.so` symbol.
