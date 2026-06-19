# SUNDA-only Dual / Deprecated TensorScalarPtr — the lineage extremes of the `TensorScalar*` family

> **Scope.** This page decodes the **two ends of the `TensorScalar*` pointer-scalar lineage** — the
> opposite extremes of the "deprecation spectrum":
>
> - The **SUNDA-only DUAL** pair `0x87`/`0x88` `TensorScalarPtrMultiDual{Arith,Bitvec}` — the **retired**
>   NC-v2 fast-path that merged up to four dual-op `TensorScalarPtr` instructions into one 4-D kernel.
>   Defined **only** in the SUNDA opcode enum, **dropped** entirely on cayman onward, and with **zero**
>   firmware footprint.
> - The **all-gen-DEPRECATED** single-pointer pair `0x44`/`0x54` `TensorScalarPtr{Arith,Bitvec}` — the
>   **predecessor** that the modern [`TensorScalar`](tensor-scalar.md) `0x43`/`0x53` subsumed via its
>   per-immediate `imm_src` field. Present on **all four** gens, flagged deprecated, yet shipping a **full,
>   working** DVE worker.
>
> These bookend the **modern** middle of the lineage that generalised them —
> [`TensorScalarPtrMulti`](ts-ptrmulti.md) (`0x4F`/`0x5F`, the W≤8 single-op survivor) +
> [`TensorScalarImmLd`](ts-immld.md) (`0x70`/`0x71`, the scalar preloader). The whole pointer-scalar
> sub-lineage (`0x44`/`0x54` → `0x87`/`0x88` → `0x4F`/`0x5F` + `0x70`/`0x71`) is the **deprecated
> `// n` branch** of the family; the maintained branch is `0x43`/`0x53` + the Cache/Select cousins
> ([`tensor-scalar.md`](tensor-scalar.md) §10).
>
> This page pins the **real names** from the four-gen `common.h`; the **compile-verified** operand structs
> (`sizeof == 64` on all four gens for both `S4D4_TSM` and `S3D3_TS`); the **verbatim** in-header validator
> pseudo-code (the DUAL fold with its **config-fixed `op1 = Multiply`**, the disabled bitvec DUAL, the
> single-pointer `imm_src` contract); the **per-gen byte-check**; and the **firmware-presence**
> corroboration that grounds the deprecation spectrum (WORKING `0x44`/`0x54` > STUB `0x4F`/`0x5F` >
> ABSENT `0x87`/`0x88`).
>
> Confidence tags use the `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` model defined in
> [`../../reference/confidence-model.md`](../../reference/confidence-model.md).

---

## 1. TL;DR — the pinned facts

| # | Fact | Evidence | Tag |
|---|------|----------|-----|
| 1 | **DUAL pair** `TENSOR_SCALAR_PTR_MULTI_DUAL_ARITH = 0x87` (135), `…_DUAL_BITVEC = 0x88` (136), both `// n, ucode/kaenadve exists, not maintained/used`. **SUNDA-only**: defined at `common.h:221`/`:222` and **absent** from the cayman/mariana/maverick opcode enums. | sunda `common.h:221`/`:222`; re-grepped all four gens | HIGH/OBSERVED |
| 2 | **Deprecated single-pointer pair** `TENSOR_SCALAR_PTR_ARITH_OP = 0x44` (68), `…_BITVEC_OP = 0x54` (84), both `// n, use TensorScalar{Arith,Bitvec}Op instead`. Present on **all four** gens (sunda `:169`/`:170`, cayman `:172`/`:173`, mariana `:177`/`:178`, maverick `:180`/`:181`). | `common.h`, all four gens | HIGH/OBSERVED |
| 3 | The DUAL binds **`NEURON_ISA_TPB_S4D4_TSM_STRUCT`** (64 B) — the **same** struct as the singular [`PtrMulti`](ts-ptrmulti.md). The deprecated pair binds **`NEURON_ISA_TPB_S3D3_TS_STRUCT`** (64 B) — the **same** struct as the maintained [`TensorScalar`](tensor-scalar.md). Both `sizeof == 64`, byte-identical offsets on all four gens. | gcc `offsetof`/`sizeof`, all four gens (§4, §5) | HIGH/OBSERVED |
| 4 | **DUAL semantics** (verbatim): "merges up to four regular `TensorScalarPtr` instructions (dual ALU op per instruction)"; `for i in range(W): dst[i,:,:,:] = (src[i,:,:,:] op0 pair[i][0]) op1 pair[i][1]`, with `1 <= W <= 4` **scalar PAIRS**. | sunda `s4d4_tsm.h:28–47` | HIGH/OBSERVED |
| 5 | **`op1` is HARD-CODED to `Multiply` in `DVE_config`, "not programmable in instruction"** — so the single-`op` `S4D4_TSM` struct serves *both* the singular (`op1 = bypass`) and DUAL (`op1 = config-multiply`) forms with **no byte-layout change**. The "second op" lives at the engine-config level, not the ISA encoding. | sunda `s4d4_tsm.h:52–53` | HIGH/OBSERVED |
| 6 | **DUAL_BITVEC `0x88` is DISABLED** in-header: "we don't have a sensible default 2nd op for bitvec to hard-code yet and we are also very tight on uop table space in DVE." | sunda `s4d4_tsm.h:289–291` | HIGH/OBSERVED |
| 7 | **`0x44`/`0x54` deprecation contract** = the *sole* difference from the maintained `0x43`/`0x53`: `tensor_scalar_ptr_imm_src` **forces `imm0_src == 0 && imm1_src == 0`** and `tensor_scalar_ptr_immediates` forces **both** `imm0`/`imm1` through `ts_ptr_imm_chk` — the scalar is **always** a `PartitionOffset` pointer, never an inline arith value. | mariana `s3d3_ts.h:330–333`, `:341–347`, `:430–433` | HIGH/OBSERVED |
| 8 | **Firmware deprecation spectrum** (byte-checked): `0x44`/`0x54` ship a **FULL** working DVE worker (`"S: Tensor-Scalar-PTR"`, the most-present family self-name); `0x4F`/`0x5F` are **STUB**/log-only thunks; the DUAL `0x87`/`0x88` is **ABSENT** — **zero** `"Dual"` strings anywhere in the blob. | `xtensa-elf-strings` over `libnrtucode_internal.so` (sha256 `b7c67e89…`) | HIGH/OBSERVED |
| 9 | The **`0x87` value did NOT get "reused"** on NC-v3+: SUNDA already carried it in **two** distinct enums simultaneously — `OPCODE_*_DUAL_ARITH = 0x87` (`common.h:221`) **and** `UPDATE_MODE_SEM_SUB_REG_READ = 0x87` (`common.h:350`). On cayman+ only the **OPCODE** name was dropped; the `UPDATE_MODE` constant is gen-stable. | `common.h` enum-boundary scan, all four gens | HIGH/OBSERVED — **CORRECTION** (§3c) |
| 10 | The `struct2opcode` JSON **stale-lists** the DUAL pair under `S4D4_TSM` on **all four** gens — including the three whose opcode enum no longer defines `PTR_MULTI_DUAL_*`. The `S3D3_TS → 0x44/0x54` binding is **not** stale (the names exist everywhere). | `instruction_mapping.json`, all four gens (§6) | HIGH/OBSERVED |

---

## 2. Provenance / carve anchors

All device-firmware facts derive from static analysis of the shipped device-firmware blob (carved from
`libnrtucode_internal.so`, disassembled with the Cadence Xtensa toolchain that ships *inside* the
gpsimd-tools package, `XTENSA_CORE=ncore2gp`, Vision-Q7 FLIX/VLIW) plus the shipped public ISA C headers,
**compile-verified this session with gcc**. No source was consulted.

| Artifact | Value |
|----------|-------|
| Container | `…/custom_op/c10/lib/libnrtucode_internal.so` |
| Container sha256 | `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b` (10,276,288 B) — re-verified in-task (`sha256sum`) |
| Disassembler | `gpsimd_tools/…/bin/xtensa-elf-objdump` (Binutils 2.34.20200201, Xtensa Tools 14.09), `XTENSA_CORE=ncore2gp` |
| Headers root | `…/custom_op/c10/include/neuron_<gen>_arch_isa/tpb/` |
| MARIANA `NX_DVE` DEBUG IRAM / DRAM | `.data` symbol VA `0x408fc0` / `0x425520` (image bytes in `.rodata`, VA == file offset) |
| SUNDA `NX_DVE` **RELEASE** IRAM | `.data` symbol VA `0x10630`, size `0xbab0` (`.rodata`) — the **only** SUNDA DVE image; **name-stripped** (§7) |
| `"S: Tensor-Scalar-PTR"` self-name offsets | `0x18d225 0x1cfcc9 0x427506 0x46967e 0x6ef22b 0x733b03 0x8af5d5` (7 hits) |

The authoritative struct/enum/validator source is the shipped public ISA headers (same package):

- `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_s4d4_tsm.h` — the `S4D4_TSM` struct + the
  `is_valid_tensor_scalar_ptr_multi_dual` validator chain (sunda only) + the verbatim DUAL semantics.
- `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_s3d3_ts.h` — the `S3D3_TS` struct + the
  `is_valid_tensor_scalar_ptr_op` validator chain (all four gens).
- `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_common.h` — the opcode enum; the `UPDATE_MODE` enum;
  `ALU_OP` (`MULT = 0x06`, `BYPASS = 0x00`, `DIVIDE = 0x07`, `POW = 0x1A`, `MOD = 0x1B`); the
  `is_general_arith_op` / `is_bitvec_op` predicate bodies; `POOLING_NUM_CHANNELS == 128`.
- `…/neuron_<gen>_arch_isa/tpb/instruction_mapping.json` — the `struct2opcode` binding.

> **NOTE — `.rodata`/`.text` VMA == file offset, but the ncore2gp config DLLs' `.data`/`.data.rel.ro`
> carry a `+0x200000` VMA−fileoffset delta** (confirm per-section with `readelf -SW`). Both DVE images
> referenced here live in `.rodata` (`readelf -SW` → `.rodata` VA `0x46b0` == file offset `0x46b0`), so
> VA == offset; do not over-generalise the delta to them. `[HIGH/OBSERVED]`

---

## 3. The four opcodes — names from the four-gen `common.h`

### 3a. The SUNDA DUAL pair (`common.h`, SUNDA only)

| Value | Name | Maint | Gen-present | Struct |
|-------|------|-------|-------------|--------|
| `0x87` | `NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_MULTI_DUAL_ARITH` | `// n` | **SUNDA only** | `S4D4_TSM` |
| `0x88` | `NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_MULTI_DUAL_BITVEC` | `// n` | **SUNDA only** | `S4D4_TSM` |

Both carry the comment `// n, ucode/kaenadve exists, not maintained/used` (sunda `common.h:221`/`:222`).
They are **defined only** in the SUNDA opcode enum; cayman/mariana/maverick `common.h` have **no** `0x87`/`0x88`
opcode entry. `[HIGH/OBSERVED]`

### 3b. The deprecated single-pointer pair (`common.h`, all four gens)

| Value | Name | Maint | Gen-present | Struct |
|-------|------|-------|-------------|--------|
| `0x44` | `NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_ARITH_OP` | `// n` | **all 4 gens** | `S3D3_TS` |
| `0x54` | `NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_PTR_BITVEC_OP` | `// n` | **all 4 gens** | `S3D3_TS` |

Both carry `// n, use TensorScalar{Arith,Bitvec}Op instead`. Per-gen line numbers (re-grepped, byte-exact):

| Opcode | sunda | cayman | mariana | maverick |
|--------|-------|--------|---------|----------|
| `0x44` / `0x54` | `169`/`170` | `172`/`173` | `177`/`178` | `180`/`181` |
| `0x87` / `0x88` | `221`/`222` | **absent** | **absent** | **absent** |

`[HIGH/OBSERVED]`

### 3c. `0x87` on NC-v3+ — the precise truth (a CORRECTION to the "reused-as-semaphore" framing)

> **CORRECTION.** Earlier family notes ([`ts-ptrmulti.md`](ts-ptrmulti.md) §9) said `0x87` was "reused as a
> semaphore constant" after the DUAL retired. The enum-boundary scan refutes the "reused" verb: **SUNDA
> already carried the value `0x87` in two distinct enums simultaneously.** `[HIGH/OBSERVED]`

The byte facts (`rg '= 0x87,'` over all four `common.h`):

| Gen | `OPCODE` enum (`…_DUAL_ARITH`) | `UPDATE_MODE` enum (`…_SEM_SUB_REG_READ`) |
|-----|-------------------------------|-------------------------------------------|
| sunda | **`0x87` @ `:221`** | `0x87` @ `:350` |
| cayman | — (no opcode) | `0x87` @ `:356` |
| mariana | — (no opcode) | `0x87` @ `:366` |
| maverick | — (no opcode) | `0x87` @ `:374` |

`NEURON_ISA_TPB_UPDATE_MODE_SEM_SUB_REG_READ = 0x87` belongs to a **different** enum
(`NEURON_ISA_TPB_UPDATE_MODE`) and is **gen-stable across all four gens, including SUNDA**. So `0x87` was
**never** "reused after retirement": SUNDA held both an `OPCODE_*_DUAL_ARITH = 0x87` and an
`UPDATE_MODE_*_SEM_SUB_REG_READ = 0x87` at once, in two namespaces. What changed on NC-v3+ is that the
**OPCODE-enum DUAL name was dropped**; the `UPDATE_MODE` constant is untouched. Opcode dispatch and
update-mode selection are separate decode contexts, so the shared value `0x87` is unambiguous in either.

---

## 4. The DUAL operand struct — `NEURON_ISA_TPB_S4D4_TSM_STRUCT` (64 B)

The DUAL shares its operand struct **byte-for-byte** with the singular [`PtrMulti`](ts-ptrmulti.md): there
is **no** DUAL-specific struct. Compile-verified this session (`#include` the per-gen header, `printf` of
`sizeof`/`offsetof`); the output is **identical** on sunda/cayman/mariana/maverick:

```text
S4D4_TSM sizeof=64  header=0 events=4 src_mem_pattern=12 in_dtype=32 out_dtype=33
         num_active_channels=34 reserved0=35 op=36 op_dim=37 reverse_operands=38
         reserved1=39 dst_mem_pattern=44
```

```c
typedef struct NEURON_ISA_TPB_S4D4_TSM_STRUCT {
    NEURON_ISA_TPB_HEADER              header;               // 4   ( 0 -  3)  opcode = 0x87 / 0x88
    NEURON_ISA_TPB_EVENTS              events;               // 8   ( 4 - 11)  wait/update semaphore
    NEURON_ISA_TPB_TENSOR4D            src_mem_pattern;      // 20  (12 - 31)  [W,Z,Y,X]; W = num_elem[3] = 1..4
    NEURON_ISA_TPB_DTYPE               in_dtype;             // 1   (32     )  FP32R NOT allowed on input
    NEURON_ISA_TPB_DTYPE               out_dtype;            // 1   (33     )  FP32R allowed on output
    uint8_t                            num_active_channels;  // 1   (34     )  1..POOLING_NUM_CHANNELS(128)
    uint8_t                            reserved0[1];         // 1   (35     )  must be 0
    NEURON_ISA_TPB_ALU_OP              op;                   // 1   (36     )  THE op0 (op1 is config-fixed, §4b)
    NEURON_ISA_TPB_TENSOR_SUBDIM       op_dim;               // 1   (37     )  FORCED XYZ
    NEURON_ISA_TPB_TENS_SCALAR_REV_OPS reverse_operands;     // 1   (38     )  DUAL: any valid value (§4c)
    uint8_t                            reserved1[5];         // 6   (39 - 43)  must be 0
    NEURON_ISA_TPB_TENSOR4D            dst_mem_pattern;      // 20  (44 - 63)  same element count as src
} NEURON_ISA_TPB_S4D4_TSM_STRUCT;
ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_S4D4_TSM_STRUCT) == 64, "…NOT 64B.");   // sunda :173
```

> **GOTCHA — there is a SINGLE `op`@36 and ZERO immediate fields.** This is the whole reason the DUAL and
> singular `PtrMulti` can share one struct: neither carries an inline scalar (the scalars are preloaded by
> [`ImmLd`](ts-immld.md)), and the DUAL's *second* op is not encoded in the struct at all — it is fixed by
> DVE config (§4b). A reimplementation must **not** look for an `op1` field in `S4D4_TSM`; it does not exist.
> `[HIGH/OBSERVED — gcc offsetof, sunda s4d4_tsm.h:158–173]`

### 4a. The "Dual" semantics — verbatim (sunda `s4d4_tsm.h:28–47`)

> "TensorScalarPtrMultiDual merges up to four regular TensorScalarPtr instructions (dual ALU op per
> instruction)." … "The value of W must be equal to the number of scalar **pairs** preloaded using
> TensorScalarImmLd (i.e. `1 <= W <= 4`)." … "This instruction effectively does:"
>
> ```text
> for i in range(W):
>     # broadcasting
>     dst[i, :, :, :] = (src[i, :, :, :] op0 scalar_pairs[i][0]) op1 scalar_pairs[i][1]
> ```
>
> "… performs two TensorScalar ALU ops (second one fixed as multiply) on the input src tensor and swaps a
> new pair of scalars every time one round of XYZ dimensions are completed. Note, only a single pair of op
> types is applied for all input elements."

The decode, as annotated C pseudocode (names are the real header identifiers):

```c
/* DUAL fold — TensorScalarPtrMultiDual{Arith,Bitvec} (0x87/0x88), SUNDA only.
 * Validity gates: is_valid_tensor_scalar_ptr_multi_dual() (s4d4_tsm.h:183-189).
 * op0  = i.s4d4_tsm.op   (the struct's single `op` @36)
 * op1  = AluOp::Multiply (CONFIG-FIXED in DVE, NOT a struct field — §4b)
 * The W scalar PAIRS were preloaded by a preceding TensorScalarImmLd (ts-immld.md). */
void tensor_scalar_ptr_multi_dual(const S4D4_TSM *i,
                                  const scalar_pair_t pairs[/*W*/],   /* from ImmLd flops */
                                  AluOp op1_fixed /* == Multiply, from DVE_config */)
{
    const uint8_t W = i->src_mem_pattern.num_elem[3];   /* 1..4 (has_valid_src_slices_tsm_dual) */
    const AluOp   op0 = i->op;                            /* @36; ∈ general_arith / bitvec gate  */

    for (uint8_t w = 0; w < W; ++w) {                    /* outer W-slice; pair swaps per XYZ round */
        for_each_zyx_element(i->src_mem_pattern, w, /*[z,y,x]*/) {
            T  src = load(i->in_dtype,  &SRC[w][z][y][x]);
            /* dst = (src op0 pair[w][0]) op1 pair[w][1],  op1 == Multiply (config-fixed)         */
            T  t0  = alu_apply(op0,      src, pairs[w].s0, i->reverse_operands); /* TensorScalar #1 */
            T  out = alu_apply(op1_fixed, t0, pairs[w].s1, i->reverse_operands); /* * pair[w][1]    */
            store(i->out_dtype, &DST[w][z][y][x], out);
        }
    }
}
```

`[HIGH/OBSERVED for the header contract; the on-device datapath bind is family-level — see §7]`

### 4b. `op1` is hard-coded to `Multiply` in `DVE_config` (the unique "config-op1" form)

The header is explicit (sunda `s4d4_tsm.h:52–53`):

> "Current restriction of op1: hard-coded to multiply in DVE_config (not programmable in instruction).
> Will remove once we get a workaround working."

So the `S4D4_TSM` struct carries **only** `op0` (the `op`@36). `op1` is set to `AluOp::Multiply`
(`NEURON_ISA_TPB_ALU_OP_MULT = 0x06`, `common.h:924`) **by DVE configuration**, not by an instruction field.
This resolves the open question of how a *second* op is expressed on a single-`op` struct: **it is not** —
it is a config-level constant. The same imm-less single-`op` struct therefore serves:

| Form | `op0` source | `op1` source |
|------|--------------|--------------|
| singular `PtrMulti` `0x4F`/`0x5F` | struct `op`@36 | bypass (`ALU_OP_BYPASS = 0x00`) |
| **DUAL** `0x87`/`0x88` | struct `op`@36 | **config-fixed `Multiply`** (`0x06`) |

> **QUIRK — the bitvec DUAL `0x88` is DISABLED.** sunda `s4d4_tsm.h:289–291`: "Disabling the bitvec version
> for now, since we don't have a sensible default 2nd op for bitvec to hard-code yet and we are also very
> tight on uop table space in DVE." There is no sensible bitvec analogue of "multiply" to fix into `op1`,
> so `0x88` is a header-only name with no enabled path even on SUNDA. `[HIGH/OBSERVED]`

### 4c. DUAL-vs-singular validity deltas (same struct, different gates)

The DUAL and singular share `S4D4_TSM` and the common validator
`is_valid_tensor_scalar_ptr_multi_common` (sunda `s4d4_tsm.h:199–214`) — the op-set gate, the dtype gate,
`op_dim == XYZ`, `same_element_count_t4d`, the reserved-zero check. Only **three** gates differ:

```c
/* W cap — has_valid_src_slices_tsm_dual (l.260-263) vs _singular (l.265-268) */
bool has_valid_src_slices_tsm_dual(const S4D4_TSM *i) {        /* DUAL  : scalar PAIRS    */
    uint8_t W = i->src_mem_pattern.num_elem[3];
    return (W >= 1) && (W <= 4);                                /* half the singular cap   */
}
bool has_valid_src_slices_tsm_singular(const S4D4_TSM *i) {    /* SINGULAR: single scalars */
    uint8_t W = i->src_mem_pattern.num_elem[3];
    return (W >= 1) && (W <= 8);
}

/* reverse — tensor_scalar_ptr_multi_reverse_chk (l.270-276) */
bool tensor_scalar_ptr_multi_reverse_chk(const S4D4_TSM *i) {
    if (!is_valid_enum(TensScalarRevOps, i->reverse_operands)) return false;
    if (has_tensor_scalar_ptr_multi_dual_opcode(i))            /* DUAL: short-circuits TRUE — */
        return true;                                           /*   ANY valid RevOps value   */
    /* SINGULAR: restricted to {None, Both} only (the 4+4 scalar-bank reason — ts-ptrmulti §4b) */
    return (i->reverse_operands == TensScalarRevOps_None)
        || (i->reverse_operands == TensScalarRevOps_Both);
}

/* op classification — l.221-233 */
bool has_tensor_scalar_ptr_multi_dual_opcode(const S4D4_TSM *i) {  /* l.221-223 */
    return i->header.opcode == Opcode_TensorScalarPtrMultiDualArith;   /* ONLY DualArith 0x87 */
}
bool tensor_scalar_ptr_multi_bitvec(const S4D4_TSM *i) {           /* l.225-228 */
    return (i->header.opcode == Opcode_TensorScalarPtrMultiBitvec)     /* 0x5F */
        || (i->header.opcode == Opcode_TensorScalarPtrMultiDualBitvec);/* 0x88 (disabled)     */
}
bool tensor_scalar_ptr_multi_arith(const S4D4_TSM *i) {           /* l.230-233 */
    return (i->header.opcode == Opcode_TensorScalarPtrMultiArith)      /* 0x4F */
        || (i->header.opcode == Opcode_TensorScalarPtrMultiDualArith); /* 0x87 */
}
```

> **NOTE — the relaxed-reverse + W≤4 gate keys on the ARITH dual opcode only.**
> `has_tensor_scalar_ptr_multi_dual_opcode` (`l.221-223`) checks **only** `DualArith (0x87)`. The bitvec DUAL
> `0x88` folds into the `bitvec` op-class (`l.225-228`) and the dtype gate, but — being disabled — never
> reaches an enabled worker. `[HIGH/OBSERVED — sunda s4d4_tsm.h:178–285 verbatim]`

### 4d. dtype matrix (shared with the singular `PtrMulti`)

The common validator gates `is_valid_dtype(in_dtype, AllowFP32R::False)` and
`is_valid_dtype(out_dtype, AllowFP32R::True)` (sunda `s4d4_tsm.h:204–205`):

| Class | dtypes | Note |
|-------|--------|------|
| **Arith `0x87`** | `FP16`/`BFLOAT16`/`UINT8`/`INT8`/`FP8*`/`FP32`/`UINT32`/`INT32` | "all converted to fp32 in the DVE"; **input** excludes `FP32R`; **output** may be `FP32R`. |
| **Bitvec `0x88`** | `in == out ∈ {UINT8, UINT16, UINT32, INT32}` | raw bits; `u8`/`u16` zero-extended to 32. **Disabled** (§4b) — header contract for a form that never shipped a worker. |

`[HIGH/OBSERVED — sunda s4d4_tsm.h:58–63, :246–254]`

---

## 5. The deprecated operand struct — `NEURON_ISA_TPB_S3D3_TS_STRUCT` (64 B)

`0x44`/`0x54` bind the **same** `S3D3_TS` as the maintained [`TensorScalar`](tensor-scalar.md) `0x43`/`0x53`
(and Transpose `0x93`, CacheReduce `0x9a`, CacheCumulative `0xe6`, Exponential `0x30`). The header's own
opener lists the deprecation (mariana `s3d3_ts.h:20–21`):

> "`TensorScalarPtrArithOp` (deprecated, use `TensorScalarArithOp` instead)" /
> "`TensorScalarPtrBitvecOp` (deprecated, use `TensorScalarBitvecOp` instead)"

Compile-verified `sizeof == 64`, byte-identical offsets on all four gens:

```c
typedef struct NEURON_ISA_TPB_S3D3_TS_STRUCT {
    NEURON_ISA_TPB_HEADER              header;               // 4   ( 0 -  3)  opcode = 0x44 / 0x54
    NEURON_ISA_TPB_EVENTS              events;               // 8   ( 4 - 11)
    NEURON_ISA_TPB_ACCUM_CMD           accumulator_cmd;      // 1   (12     )  FORCED Idle(0) (has_zero_accum_cmd_field)
    uint8_t                            reserved0[3];         // 3   (13 - 15)  must be 0
    NEURON_ISA_TPB_MEM_PATTERN3D       src_mem_pattern;      // 16  (16 - 31)  3D strided input
    NEURON_ISA_TPB_DTYPE               in_dtype;             // 1   (32     )
    NEURON_ISA_TPB_DTYPE               out_dtype;            // 1   (33     )
    uint8_t                            num_active_channels;  // 1   (34     )  1..128
    NEURON_ISA_TPB_IMM_SRC             imm0_src;             // 1   (35     )  FORCED 0 for the PTR ops (§5b)
    NEURON_ISA_TPB_ALU_OP              op0;                  // 1   (36     )  first AluOp
    NEURON_ISA_TPB_ALU_OP              op1;                  // 1   (37     )  second AluOp
    NEURON_ISA_TPB_TENS_SCALAR_REV_OPS reverse_operands;     // 1   (38     )  {None0,First1,Second2,Both3}
    NEURON_ISA_TPB_IMM_SRC             imm1_src;             // 1   (39     )  FORCED 0 for the PTR ops (§5b)
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD  imm0;                 // 4   (40 - 43)  scalar0 — ALWAYS a PartitionOffset ptr
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD  imm1;                 // 4   (44 - 47)  scalar1 — ALWAYS a PartitionOffset ptr
    NEURON_ISA_TPB_MEM_PATTERN3D       dst_mem_pattern;      // 16  (48 - 63)  3D output
} NEURON_ISA_TPB_S3D3_TS_STRUCT;
ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_S3D3_TS_STRUCT) == 64, "…NOT 64B.");   // mariana :47
```

```text
S3D3_TS sizeof=64  header=0 events=4 accumulator_cmd=12 src_mem_pattern=16 in_dtype=32 out_dtype=33
        num_active_channels=34 imm0_src=35 op0=36 op1=37 reverse_operands=38 imm1_src=39
        imm0=40 imm1=44 dst_mem_pattern=48      (identical on sunda/cayman/mariana/maverick)
```

> **QUIRK — `0x44`/`0x54` use the FULL two-op `S3D3_TS`, unlike the DUAL.** Where the DUAL hides its second
> op in DVE config, `0x44`/`0x54` carry **two real `AluOp` fields** (`op0`@36, `op1`@37). They are the
> *single-pointer* form (3-D, one scalar0/scalar1 pair per instruction), not a 4-D W-vector. `[HIGH/OBSERVED]`

### 5a. The two-AluOp splat semantics

`0x44`/`0x54` run the **same** datapath as the maintained `0x43`/`0x53` (see
[`tensor-scalar.md`](tensor-scalar.md) §3 for the shared base):

```text
result = op1( op0(tensor, scalar0), scalar1 )       /* reverse_operands flips per-op operand order */
```

The scalar is broadcast across vector lanes by the IVP `REPLICATE` family
(`ivp_rep2nx8t` / `ivp_repnx16t` / `ivp_repn_2x32t`), then the per-lane `AluOp` applies. The **only**
difference from `0x43`/`0x53` is the scalar **source**: for `0x44`/`0x54` both `scalar0`/`scalar1` are
**always** fetched from memory via a `PartitionOffset` pointer (never inline). `[HIGH/OBSERVED]`

### 5b. The deprecation contract — the SOLE difference from the maintained `0x43`/`0x53`

```c
/* is_valid_tensor_scalar_ptr_op — mariana s3d3_ts.h:86-95 */
bool is_valid_tensor_scalar_ptr_op(const S3D3_TS *i) {
    return has_valid_neuron_header(i)
        && has_valid_neuron_events(i)
        && has_tensor_scalar_ptr_opcode(i)        /* opcode ∈ {0x44, 0x54}  (l.230-233) */
        && tensor_scalar_valid_ops(i)             /* op0/op1 gate (shared with 0x43/0x53) */
        && tensor_scalar_valid_types(i)
        && tensor_scalar_ptr_immediates(i)        /* << the PTR narrowing                 */
        && tensor_scalar_ptr_imm_src(i)           /* << the PTR narrowing                 */
        && tensor_scalar_shift_chk(i)
        && tensor_scalar_tensor_chk(i)
        && tensor_scalar_reverse_chk(i)
        && s3d3_ts_reserved_zero(i)
        && has_zero_accum_cmd_field(i)            /* accumulator_cmd FORCED Idle(0)       */
        && is_valid_dtype(i->in_dtype,  AllowFP32R_False)
        && is_valid_dtype(i->out_dtype, AllowFP32R_True);
}

/* tensor_scalar_ptr_imm_src — l.430-433 : both scalar sources FORCED to inline/zero off  */
bool tensor_scalar_ptr_imm_src(const S3D3_TS *i) {
    return (i->imm0_src == 0) && (i->imm1_src == 0);
}

/* tensor_scalar_ptr_immediates — l.330-333 : BOTH scalars must be valid PartitionOffset ptrs */
bool tensor_scalar_ptr_immediates(const S3D3_TS *i) {
    return ts_ptr_imm_chk(i, i->imm0)
        && ts_ptr_imm_chk(i, i->imm1);
}

/* ts_ptr_imm_chk — l.341-347 : the pointer is channel-addressable + class-aligned         */
bool ts_ptr_imm_chk(const S3D3_TS *i, ImmValInstField imm) {
    return tpb_addr_active_channels(imm.imm_ptr, i->num_active_channels)
        && (   (tensor_scalar_arith(i)  && four_byte_aligned(imm.imm_ptr))
            || (tensor_scalar_bitvec(i) && addr_aligned_dtype(imm.imm_ptr, i->in_dtype)));
}
```

Contrast the **maintained** `0x43`/`0x53`, whose `tensor_scalar_immediates_check` (mariana `s3d3_ts.h:297–310`)
lets **each** immediate independently choose `ImmSrc ∈ {InstructionImmediate, PointerImmediate, RegPtrImmediate}`.

> **GOTCHA — `0x44` ≡ `0x43` with `imm_src` pinned to `Ptr`.** A maintained `TensorScalarArithOp (0x43)`
> whose `imm0_src == imm1_src == PointerImmediate` is **functionally identical** to a deprecated
> `TensorScalarPtrArithOp (0x44)`. The per-immediate `imm_src` field on `0x43`/`0x53` **subsumes** the
> hard-wired pointer contract of `0x44`/`0x54` — exactly why the header says
> "*use `TensorScalarArithOp` instead*". This is the entire deprecation rationale. `[HIGH/OBSERVED]`

### 5c. AluOp / dtype (shared with the maintained TS)

```c
/* tensor_scalar_valid_ops — mariana s3d3_ts.h:257-280 (shared by 0x43/0x53 and 0x44/0x54) */
//  arith (0x44): op0,op1 ∈ is_general_arith_op  (Add/Sub/Mult/Max/Min/Logical*/IsEQ..LT/AbsDiff/AbsVal,
//                + mariana AbsMax/AbsMin/ReLU/Square); reject Divide/Pow/Mod.
//  bitvec(0x54): op0,op1 ∈ is_bitvec_op  (Bypass/BitwiseNot/{Arith,Logical}Shift{L,R}/And/Or/Xor/Crc32);
//                in_dtype == out_dtype, int-only raw bits.
//  composition rule (l.278-279): (op0 != Bypass) || (op1 == Bypass).
```

`is_bitvec_op` is the **full 10** (sunda `common.h:1748–1759`): `Bypass`, `BitwiseNot`, `ArithShiftLeft`,
`ArithShiftRight`, `LogicalShiftLeft`, `LogicalShiftRight`, `BitwiseAnd`, `BitwiseOr`, `BitwiseXor`,
`Crc32`. `[HIGH/OBSERVED]`

---

## 6. The `struct2opcode` binding — the stale JSON superset

`instruction_mapping.json` `struct2opcode` (verbatim per-gen, this task):

```text
NEURON_ISA_TPB_S3D3_TS_STRUCT  -> { TENSOR_SCALAR_ARITH_OP, TENSOR_SCALAR_BITVEC_OP,
   TENSOR_SCALAR_PTR_ARITH_OP (0x44), TENSOR_SCALAR_PTR_BITVEC_OP (0x54),
   TRANSPOSE_TENSOR_SCALAR_ARITH_OP, TENSOR_SCALAR_CACHE_REDUCE,
   TENSOR_SCALAR_CACHE_CUMULATIVE, EXPONENTIAL }        — 0x44/0x54 bound on ALL 4 gens (NOT stale).

NEURON_ISA_TPB_S4D4_TSM_STRUCT -> { TENSOR_SCALAR_PTR_MULTI_ARITH (0x4F),
   TENSOR_SCALAR_PTR_MULTI_BITVEC (0x5F),
   TENSOR_SCALAR_PTR_MULTI_DUAL_ARITH (0x87),
   TENSOR_SCALAR_PTR_MULTI_DUAL_BITVEC (0x88) }         — DUAL pair listed on ALL 4 gens.
```

The `S4D4_TSM` binding lists `PTR_MULTI_DUAL_{ARITH,BITVEC}` on **every** gen — including
cayman/mariana/maverick, whose **opcode enum no longer defines those names**. So the `S4D4_TSM` JSON is a
**stale superset** on NC-v3+ (it references symbols absent from the live enum); the live `S4D4_TSM` members
there are only `0x4F`/`0x5F`. The `S3D3_TS → 0x44/0x54` binding is **not** stale — those opcode names exist
on all four gens.

> **NOTE — JSON is a name superset, the enum is the ground truth.** A reimplementer parsing
> `instruction_mapping.json` on NC-v3+ must resolve each opcode **name** against the per-gen `common.h`
> enum: `PTR_MULTI_DUAL_*` will fail to resolve on cayman/mariana/maverick. Treat the JSON as a *maximal*
> map and the enum as the *live* one. `[HIGH/OBSERVED — jq over all four instruction_mapping.json]`

---

## 7. Firmware presence — the decisive deprecation-spectrum evidence

Firmware blob: `libnrtucode_internal.so`, sha256 `b7c67e89…` (10,276,288 B).

### 7a. The strings sweep (`xtensa-elf-strings` over the whole blob)

| Self-name | Hits | Reading |
|-----------|------|---------|
| `"Dual"` (any case) | **0** | The DUAL `0x87`/`0x88` has **no** self-name, **no** log string, **no** footprint of any kind. |
| `"S: Tensor-Scalar-PTR"` (`0x44`/`0x54`) | **7** (`0x18d225 0x1cfcc9 0x427506 0x46967e 0x6ef22b 0x733b03 0x8af5d5`) | The **most-present** family self-name — it has a real worker across DEBUG/PERF + other engine images. |
| `"TensorScalarPtrMulti…"` (`0x4F`/`0x5F`) | **8** | The stub-wired singular pair (self-name present, body LOG-only — [`ts-ptrmulti.md`](ts-ptrmulti.md) §8). |

`[HIGH/OBSERVED]`

### 7b. The SUNDA DVE image — a name-stripped RELEASE build (a CORRECTION)

> **CORRECTION.** The GX-OP-07 backing note said "there is **NO** `SUNDA_NX_DVE` image in this container."
> `nm -S` refutes the literal claim: a `SUNDA_NX_DVE_RELEASE` image **does** exist (IRAM `.data` VA
> `0x10630`, size `0xbab0`; plus DRAM/SRAM/EXTRAM). What is **true** is the *qualified* version — SUNDA
> ships **only** a RELEASE build, and the per-gen variant matrix is asymmetric: `[HIGH/OBSERVED]`

| Gen | DVE image variants present (`nm` `_NX_DVE_*_IRAM`) |
|-----|----------------------------------------------------|
| **SUNDA** | **RELEASE** only |
| CAYMAN / MARIANA / MARIANA_PLUS / MAVERICK | DEBUG + PERF + TEST |

The self-name strings (`"S: Tensor-Scalar-PTR"`, and — were it present — any `"Dual"`) live in the
**DEBUG/PERF** images, which embed the human-readable `"S:"` worker-name pool. The SUNDA **RELEASE** image is
**name-stripped** — a strings scan of its `.rodata` window (`0x10630`–`0x1c0e0`) yields only packed binary
(678 short fragments, none human-readable). So the DUAL still has **no identifiable worker** in SUNDA:

- The only SUNDA image is a RELEASE build with no self-name pool, and
- there are **zero** `"Dual"` strings anywhere in the blob.

The honest decode ceiling for `0x87`/`0x88` is therefore **header-only** (`common.h` + `s4d4_tsm.h` + the
stale JSON). The firmware contributes a **negative** result (no Dual worker is identifiable), not a positive
carve. `[HIGH/OBSERVED — nm -S + xtensa-elf-strings region scan]`

### 7c. The `0x44`/`0x54` worker is FULL (the WORKING end of the spectrum)

The MARIANA DVE `"S: Tensor-Scalar-PTR"` worker is a **full compute body**, not a stub. The byte evidence
(MARIANA DVE DEBUG DRAM/IRAM carve, this task) reproduces the SX-FW-50/58 anchors **exactly**:

- Image symbols (`nm -S`): `MARIANA_NX_DVE_DEBUG_DRAM_get.data` @ VA `0x425520` (size `0x7000`),
  `…_IRAM_get.data` @ VA `0x408fc0` (size `0x1c560`) — both in `.rodata`, VA == file offset.
- DRAM self-name offsets: `"S: Tensor-Scalar"` @ `0x1fd4`, `"S: Tensor-Scalar-PTR"` @ `0x1fe6`,
  `ImmLd` @ `0x283f`/`0x285a`, `PtrMulti` @ `0x28a0`/`0x28be`.
- IRAM disassembles to 46,162 lines, exit 0, empty stderr (== FW-50/58).
- The Tensor-Scalar-PTR worker is a **real body**, at two self-name load sites:
  - `funcVA 0xa310` (`entry a1, 64`): `const16 a10, 0x1fe6` (self-name) → `call8 0x188a4` (log) →
    descriptor base `const16 a2, 0x2230` + 4× `l32i.n`/`s32i.n` 16-byte copy → `call8 0x99c8`/`0x99bc`
    (DVE-setup helpers) → `s16i a11, [a3+176]`/`[a3+0x130]` (FLIX config-window writes) →
    **`call0 0xfffbb444`** (the `alu_op` compute kernel) → `retw.n` @ `0xa386`. **118-byte body.**
  - `funcVA ≈0xa298` (loads `0x1fe6` at `0xa2a1`): same structure — descriptor base `const16 a2, 0x2220`,
    `call8 0x99c8`/`0x99bc`, FLIX `s16i` writes, **`call0 0xfffbb3cc`** → `retw.n` @ `0xa30e`.
- **Contrast:** the singular `PtrMulti` thunks are **LOG-only** 28-byte bodies — `funcVA 0xcab5`
  (`TensorScalarPtrMultiArith`, self-name `0x28a0`) and `0xcadd` (`…Bitvec`, `0x28be`): `const16` self-name
  → `call8 0x188a4` (the shared logger; 229 call sites blob-wide) → `j`/`retw.n`. **No `call0`, no
  descriptor `const16 0x22xx`, no `call8 0x99c8`/`0x99bc` setup, no config-window `s16i`** — zero compute.

`[HIGH/OBSERVED — byte-decoded MARIANA DVE IRAM, this task; corroborated by §8]`

> **GOTCHA — FLIX literal-pool desync.** Stock `objdump` desyncs the MARIANA DVE IRAM on the recurring
> `.byte 0x2f/0x8f/0x4f/0x5f` literal-pool lead bytes. The full-worker-vs-stub verdict rests **only** on the
> byte-clean regions (entry prologues, `const16` self-name loaders, `call8`/`call0` edges, `l32i`/`s32i`
> descriptor-copy blocks, `s16i` window writes, `retw.n`); the mis-decoded `.byte` spam between them is **not**
> reported as real instructions. No FLIX desync touches the header/compile/strings/`nm` evidence the DUAL
> decode depends on. `[HIGH/OBSERVED]`

---

## 8. The deprecation spectrum — byte-checked

The task's central question: **`0x87`/`0x88` SUNDA-only-retired vs `0x44`/`0x54` all-gen-deprecated-but-present**.

| Opcode | Enum presence | Maint | Struct | Firmware worker | Deprecation state |
|--------|---------------|-------|--------|-----------------|-------------------|
| `0x44` / `0x54` | **all 4 gens** (sunda `169/170` … maverick `180/181`) | `// n` | `S3D3_TS` | **FULL** worker (MARIANA `≈0xa298`/`0xa310`; real compute) | **DEPRECATED-but-PRESENT-and-WORKING.** Superseded by `0x43`/`0x53`'s per-imm `imm_src`. |
| `0x87` / `0x88` | **SUNDA only** (`221/222`); cayman+ **absent** | `// n` | `S4D4_TSM` | **NONE** (zero `"Dual"` strings; SUNDA ships only a name-stripped RELEASE image) | **RETIRED after NC-v2.** Enum name dropped; JSON binding left stale. Header-only. |

The two deprecation modes are **structurally distinct** (byte-confirmed):

- **`0x44`/`0x54` — soft-deprecated.** The opcode name survives on **every** gen, the JSON binding is valid,
  a **full** DVE worker ships — but the docs steer you to `0x43`/`0x53`. The op still **runs**; it is just
  redundant. *(Firmware reality: WORKING.)*
- **`0x87`/`0x88` — hard-retired.** The opcode name was **removed** from the NC-v3+ opcode enum, no
  identifiable firmware worker exists in this container, and the JSON binding was left dangling (references
  absent symbols). The op is **gone**; only the SUNDA header remembers it. *(Firmware reality: ABSENT.)*
- The **middle** state — "wired-as-stub" — is the singular [`PtrMulti`](ts-ptrmulti.md) `0x4F`/`0x5F`:
  name on all gens, self-name string present, but a LOG-only body.

```text
The deprecation SPECTRUM, ordered by firmware reality:
    WORKING (0x44/0x54)   >   STUB (0x4F/0x5F)   >   ABSENT (0x87/0x88)
    full compute body         log-only thunk         no string, no enabled image
```

```c
/* The per-gen presence gate, as a decode-time predicate over (gen, opcode). */
bool opcode_decodable(Gen gen, uint8_t opcode) {
    switch (opcode) {
    case 0x44: case 0x54:                       /* S3D3_TS, all gens, working          */
        return true;                            /* names exist on sunda..maverick      */
    case 0x87: case 0x88:                       /* S4D4_TSM DUAL                       */
        return (gen == GEN_SUNDA);              /* retired on cayman+ (enum name gone) */
    default:
        return /* … other opcodes … */ false;
    }
}
```

`[HIGH/OBSERVED — enum line numbers + strings sweep + nm + byte-decoded worker bodies]`

---

## 9. Per-generation presence

| Gen | `0x44`/`0x54` (`S3D3_TS`) | `0x87`/`0x88` (`S4D4_TSM` DUAL) | `0x4F`/`0x5F` (singular `PtrMulti`) |
|-----|--------------------------|---------------------------------|-------------------------------------|
| **SUNDA (v2)** | def'd `// n`; 64 B; full datapath | **DEF'D** `// n`; 64 B; W≤4; `op1=fixed-mult`; bitvec **DISABLED**; **no identifiable worker** (RELEASE-only, name-stripped) | def'd `// n` (only a RELEASE DVE image in container) |
| **CAYMAN (v3)** | def'd `// n`; 64 B id | **ABSENT** from opcode enum (JSON stale-lists it) | def'd `// n`; stub-wired DVE |
| **MARIANA (v4)** | def'd `// n`; 64 B id; **FULL** worker `≈0xa298`/`0xa310` | **ABSENT** (JSON stale) | def'd `// n`; stub-wired DVE |
| **MARIANA_PLUS (v4+)** | def'd `// n`; 64 B id | **ABSENT** (JSON stale) | def'd `// n`; stub-wired DVE |
| **MAVERICK (v5)** | def'd `// n`; 64 B id | **ABSENT** (JSON stale) | def'd `// n`; stub-wired DVE |

Notes:

- `0x44`/`0x54`: opcode + `S3D3_TS` struct **byte-identical** on all four gens (compile-verified). MAVERICK
  adds the NC-v5 tile-aware channel-range + int-AluOp-DVE relaxation (see [`tensor-scalar.md`](tensor-scalar.md)).
  `[HIGH/OBSERVED for v2–v4; v5 interior INFERRED — header-observed only per Part-5 ground rule]`
- `0x87`/`0x88`: a pure NC-v2-floor artifact. The DUAL "fast path" (merge 4 dual-op `TensorScalarPtr`) did
  not survive; NC-v3+ kept only the singular `0x4F`/`0x5F` (itself stub-wired). The stale JSON binding is the
  only NC-v3+ footprint. `[HIGH/OBSERVED]`
- The `S4D4_TSM` struct is byte-identical on all four gens (the DUAL and singular share it); only the
  validity functions differ, and the **cayman+** headers simply **omit** the DUAL validity fns — **231 lines
  vs SUNDA's 303** (`wc -l`), zero `has_tensor_scalar_ptr_multi_dual_opcode` / `has_valid_src_slices_tsm_dual`
  references on cayman/mariana/maverick. `[HIGH/OBSERVED — line counts + `rg -c`]`

---

## 10. Lineage synthesis — the deprecated POINTER branch, end to end

The `TensorScalar*` family has a **maintained** branch and a **deprecated pointer** branch. This page decodes
the two **extremes** of the deprecated branch; [`tensor-scalar.md`](tensor-scalar.md) /
[`ts-immld.md`](ts-immld.md) / [`ts-ptrmulti.md`](ts-ptrmulti.md) cover its middle.

**Maintained branch (`// Y`, every gen):** `0x43`/`0x53` `TensorScalar{Arith,Bitvec}` (`S3D3_TS`, base
`out = op1(op0(src,s0),s1)`, per-imm `imm_src ∈ {Inst,Ptr,RegPtr}`) + Transpose `0x93` + Select `0x98` +
CacheReduce `0x9a` + CacheCumulative `0xe6`.

**Deprecated pointer branch (`// n`) — the GX-OP-07 spine:**

| Opcode | Name | Struct | Role |
|--------|------|--------|------|
| `0x44`/`0x54` ← **this page** | `TensorScalarPtr{Arith,Bitvec}` | `S3D3_TS` | **PREDECESSOR**: single pointer-scalar, two ops, `imm_src` forced `Ptr`. **WORKING.** Subsumed by `0x43`/`0x53`'s `imm_src`. |
| `0x4F`/`0x5F` | `TensorScalarPtrMulti{Arith,Bitvec}` | `S4D4_TSM` | **MULTI**: 4-D `[W,Z,Y,X]`, single op, `W ≤ 8` ImmLd-preloaded scalars (one per W-slice). **STUB-WIRED.** |
| `0x87`/`0x88` ← **this page** (SUNDA only) | `TensorScalarPtrMultiDual{Arith,Bitvec}` | `S4D4_TSM` | **SUNDA EXTREME**: 4-D, **DUAL** op (`op1` = config-fixed multiply), `W ≤ 4` scalar **PAIRS**. **RETIRED NC-v3+.** Bitvec disabled. **No firmware.** |
| `0x70`/`0x71` | `TensorScalarImmLd{Arith,Bitvec}` | `S2_BN` | **THE LOADER**: preloads 1..8 (singular) / pairs (dual) scalars into DVE flops for `PtrMulti`/`Dual`. |

**The lineage arrow:**

```text
[0x44/0x54  single pointer-scalar, S3D3_TS, WORKING]
      │  generalise "one pointer-scalar" → "a VECTOR of pointer-loaded scalars
      │  indexed by a new 4th (W) tensor dimension"; move the scalar fetch into a
      │  dedicated preload instruction (ImmLd 0x70/0x71).
      ▼
[SUNDA fork, NC-v2:  0x87/0x88 DUAL — W≤4 scalar PAIRS, op0 + config-fixed-multiply op1]
      │  the v2 "fast path" (merge 4 dual-op TSPtr). Hard restrictions: op1 fixed to
      │  multiply; bitvec disabled (no default 2nd op); tight DVE uop-table space.
      │  RETIRED after NC-v2.
      ▼
[SURVIVING form, NC-v3+:  0x4F/0x5F singular PtrMulti — W≤8, ONE op]
      kept the W-vector idea, dropped the dual-op complexity; survives on every gen
      but is itself "// n" stub-wired.
```

**The five family discriminator axes (with the GX-OP-07 ops placed):**

| Axis | inline-imm | forced-single-ptr | multi-ptr vector | the loader |
|------|-----------|-------------------|------------------|------------|
| 1. scalar source | `0x43`/`0x53` | **`0x44`/`0x54`** | `0x4F`/`0x5F`, **`0x87`/`0x88`** (via ImmLd) | `0x70`/`0x71` |
| 2. scalar count | one/two imm | one/two imm | vector of `W` single (`0x4F`/`0x5F`, W≤8) / vector of `W` **PAIRS** (**`0x87`/`0x88`**, W≤4) | n/a |
| 3. op composition | two struct ops | **two struct ops** | single op (`0x4F`/`0x5F`) / `op0`+**config-fixed-multiply** (**`0x87`/`0x88`**) | n/a |
| 4. struct dim | 3-D `S3D3_TS` | **3-D `S3D3_TS`** | 4-D `S4D4_TSM` (`0x4F`/`0x5F`, `0x87`/`0x88`) | src-only `S2_BN` |
| 5. deprecation | WORKING/maintained | **WORKING** | STUB (`0x4F`/`0x5F`) | STUB |

`ARITH`-vs-`BITVEC` is the orthogonal sub-split on every compute member (arith = `is_general_arith_op`,
fp-hub-fp32; bitvec = `is_bitvec_op`, int-only raw bits) — with the wrinkle that the **DUAL_BITVEC `0x88` is
disabled** (no default 2nd bitvec op to hard-code). `[HIGH/OBSERVED — joined from the verbatim headers + JSON
+ firmware]`

---

## 11. Honesty ledger

**HIGH / OBSERVED** (header-verbatim / compile-output / disasm / byte / symtab):

- Names + values + `// n` flags for `0x87`/`0x88` (sunda `common.h:221`/`:222`, SUNDA-only) and `0x44`/`0x54`
  (all four gens, per-gen line numbers §3b).
- `0x87` co-existence in `OPCODE` (sunda `:221`) + `UPDATE_MODE` (`:350`/cayman `:356`/mariana `:366`/
  maverick `:374`) — **CORRECTION** to "reused as a semaphore".
- Structs: `S4D4_TSM` (64 B) and `S3D3_TS` (64 B), **compile-verified byte-identical** on all four gens (gcc
  `offsetof`/`sizeof` output, §4/§5).
- DUAL semantics (`s4d4_tsm.h:28–53`): "merges up to four TSPtr (dual ALU op)", W≤4 scalar PAIRS,
  `dst[i]=(src[i] op0 pair[i][0]) op1 pair[i][1]`, **`op1` hard-coded to multiply in `DVE_config`**, bitvec
  version **disabled** (`:289–291`), reverse unrestricted for the dual opcode.
- `0x44`/`0x54` contract (`s3d3_ts.h:330–333`, `:341–347`, `:430–433`): `imm0_src == imm1_src == 0`, both
  immediates forced through `ts_ptr_imm_chk` (always a `PartitionOffset` pointer).
- JSON: `S4D4_TSM` stale-lists DUAL on all four gens; `S3D3_TS` lists `0x44`/`0x54` (not stale).
- Firmware: **zero** `"Dual"` strings; SUNDA ships only a name-stripped **RELEASE** DVE image (§7b);
  `0x44`/`0x54` `"S: Tensor-Scalar-PTR"` is a **full** worker (7 string hits; MARIANA `≈0xa298`) vs the
  `0x4F`/`0x5F` LOG-only thunk. Container sha256 `b7c67e89…` re-verified.

**MED / INFERRED:**

- The DUAL's **on-device** compute datapath. There is no identifiable DUAL firmware in this container (no
  string; SUNDA RELEASE is name-stripped), so the dual fold (`op0` then config-fixed multiply, scalar PAIR
  swap per W) is reported from the SUNDA **header** (HIGH for the header contract). Its bind to a concrete
  on-device datapath is reported only at the **family level** (the shared `alu_op` single-op arm + the ImmLd
  preload). `[MED/INFERRED — never fabricated]`
- "`op1 = Multiply` realised in `DVE_config`" is a verbatim **header claim** (HIGH as a header fact); the
  exact DVE config register that fixes it is not observable here (no SUNDA DEBUG firmware). The config
  *mechanism* is `[MED/INFERRED]`.

**LOW / UNRECOVERED:**

- The `0x87`/`0x88` → funcVA descriptor bytes: no SUNDA DEBUG firmware carries them; n/a in this container.
- The `0x44`/`0x54` → funcVA descriptor literal: the stub `l32r` literals resolve outside the carved IRAM
  (== the SX-FW-50 carve limitation).

> **CORRECTION (recorded above, §3c + §7b).** Two backing-note phrasings were over-strong and are corrected
> here: (a) `0x87` was **not** "reused" on NC-v3+ — SUNDA carried it in two enums at once, and only the
> OPCODE name was dropped; (b) SUNDA is **not** missing a DVE image — it ships a **RELEASE** one, but that
> image is name-stripped, which is *why* the DUAL has no identifiable worker. The decode conclusions are
> unchanged; the evidence is now precise.

---

## 12. See also

- [`tensor-scalar.md`](tensor-scalar.md) — `TensorScalar` `0x43`/`0x53` (the maintained base that subsumes
  `0x44`/`0x54`) + the full `Tensor-Scalar-PTR` family map.
- [`ts-ptrmulti.md`](ts-ptrmulti.md) — `TensorScalarPtrMulti` `0x4F`/`0x5F` (the W≤8 single-op survivor that
  shares `S4D4_TSM` with this page's DUAL).
- [`ts-immld.md`](ts-immld.md) — `TensorScalarImmLd` `0x70`/`0x71` (the scalar preloader that feeds the
  `PtrMulti`/`Dual` flops).
- [`../../reference/confidence-model.md`](../../reference/confidence-model.md) — the confidence/evidence tag model.
