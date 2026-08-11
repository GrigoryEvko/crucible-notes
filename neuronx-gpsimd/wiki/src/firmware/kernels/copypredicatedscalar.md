# CopyPredicatedScalar — opcode `0xe8`: the scalar-source member that closes the predicated-op family

**CopyPredicatedScalar** (`CPS`, opcode **`0xe8`**, host ISA enum `COPY_PREDICATED_SCALAR`) is the
**scalar-source** member of the GPSIMD *predicated-op family*. Where its three siblings each take the
copied value from a **data tensor**, `CopyPredicatedScalar` takes it from a **single scalar
immediate** — and writes that one value into the predicate-selected lanes of the destination,
merging the prior `dst` value into the rest. It has **no data-source tensor at all**: only a
predicate tensor, a destination tensor, and an inline scalar. The arch-isa header names the
canonical use exactly — *"replace parts of the destination tensor with a known immediate
(e.g. `-inf`)"* — so this is the masked-**constant-fill** primitive of the family (the attention/
softmax `-inf` mask poke), the complement to the tensor-copy `0x72`, the tensor-cast `0x99`, and
the tensor-copy-plus-reduce `0xea`.

It is opcode **`0xe8 // Y`** (actively maintained) byte-identically in **all four** byte-grounded
GPSIMD ISA generations (SUNDA / CAYMAN / MARIANA / MAVERICK), it rides its **own dedicated 64-byte
operand struct** (`NEURON_ISA_TPB_S3D3_CP_PRED_SCALAR_STRUCT`, sole occupant — not the `S3S3D3_TT`
of copy/cast, not the `S2S2D2_STT` of the reduce), and like the rest of the family it runs
**hardware-native on the DVE engine**, not as a Q7-POOL software kernel.

> **HEADLINE FINDING — `CopyPredicatedScalar`'s default polarity is the INVERSE of the family.**
> `0x72` / `0x99` / `0xea` all write the value where the predicate is **true**. `CopyPredicatedScalar`'s
> **default** (`reversed_pred == 0`) writes the scalar where the predicate is **FALSE** and *keeps*
> `dst` where the predicate is true. A dedicated `reversed_pred` polarity bit (unique to `0xe8`)
> flips it back to the family convention. This is the page's central correction to a naïve reading
> of the family — see [§7](#7-semantics--the-predicate-gated-scalar-blend-the-decoded-behavior).
> `[HIGH/OBSERVED — header pseudocode lines 42-47, verbatim.]`

Confidence convention on this page: `[HIGH/OBSERVED]` = read directly from byte / header / compile;
`[MED/INFERRED]` = reasoned over an OBSERVED fact; `[…/CARRIED]` = re-used from a sibling page at its
stated confidence without re-reading the artifact. Every count is grounded to
`nm` / `rg -c` on the shipped binary, never the decompile.

> **NOTE — provenance.** Every primary fact below derives from the shipped customop-lib package
> `aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64`: the in-package arch-isa C interface headers
> `…/c10/include/neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/` (the authoritative
> opcode / operand-struct / enum / validator contract), a struct compile-verify against those
> headers, and the device firmware container `…/c10/lib/libnrtucode_internal.so` read with `nm`,
> `readelf`, and a byte scan. The headers live under `extracted/` (gitignored — reach with
> `fd --no-ignore` or an absolute path).

---

## 1. The predicated-op family, side by side — all four members

`CopyPredicatedScalar` is the **third** of four. Read this table first; the rest of the page is the
`0xe8` column in full. It is byte-consistent with [CastPredicated](castpredicated.md) §9 and
[CopyPredicatedReduce](copypredicatedreduce.md) §12 (do not infer a `0xea` member on `S3S3D3_TT` —
`0xea` is its own thing; see the dual-name note below).

| opcode | device self-name | host ISA enum (`common.h`) | operand struct | **what the copied value IS** | cast? | reduce? |
|---|---|---|---|---|---|---|
| `0x72` | `S: CopyPredicated` (×4) | `COPY_PREDICATED` | `S3S3D3_TT` | `src1` **data tensor**, element-wise | no | no |
| `0x99` | `S: CastPredicated` (×4) | `CAST_PREDICATED` | `S3S3D3_TT` | `cast(src1` **data tensor**`)`, FP32-hub | **YES** | no |
| **`0xe8`** | **`S: CopyPredicatedScalar` (×4)** | **`COPY_PREDICATED_SCALAR`** | **`S3D3_CP_PRED_SCALAR`** | **one `src0_imm` SCALAR immediate** (inline / per-lane ptr / reg ptr) — **no data tensor** | no | no |
| `0xea` | `S: CopyPredicatedReduce` (×4) | `SELECT_REDUCE` | `S2S2D2_STT` | `src1` **data tensor** + masked reduce-MAX accumulator | no | **MAX** |

> **CORRECTION — the `0xea` dual name, carried unchanged.** `0xea` has *two* names: the host ISA
> opcode enumerator is `NEURON_ISA_TPB_OPCODE_SELECT_REDUCE`, and the device firmware self-names
> that same kernel `CopyPredicatedReduce` (`"S: SelectReduce"` count **0**, `"S: CopyPredicatedReduce"`
> count **4**). There is **no** `COPY_PREDICATED_REDUCE` opcode in any header. This page does not
> disturb that finding — see [CopyPredicatedReduce](copypredicatedreduce.md) §2. By contrast,
> `0xe8`'s host enum and device self-name **agree** (`COPY_PREDICATED_SCALAR` ↔ `CopyPredicatedScalar`).
> `[HIGH/CARRIED — copypredicatedreduce.md]`

What separates the four:

* **value source** — TENSOR for `0x72` / `0x99` / `0xea`; **SCALAR immediate** for `0xe8` only.
* **operand struct** — `S3S3D3_TT` (two source tensors) for `0x72` / `0x99`; `S2S2D2_STT`
  (`op0` + `op1` + `accumulator_cmd`) for `0xea`; the **dedicated** `S3D3_CP_PRED_SCALAR`
  (one predicate tensor + one dst tensor + a scalar-immediate triple) for `0xe8`.
* **extra machinery** — none for `0x72`; the FP32-hub cast for `0x99`; `op1=Max` + PSUM
  accumulator for `0xea`; the `reversed_pred` polarity flip + the per-lane immediate path
  + (MARIANA+) indirect-scatter `dst` for `0xe8`.

What all four **share**: the **DVE engine** (hardware-native, co-resident in the same four
`NX_DVE_DEBUG_DRAM` blobs); the **`src0` integer predicate** (`is_valid_int_dtype_datapath`, the
*same* 6-int set); and the **`bitkillt` MERGE** consume (predicate-non-selected lanes retain `dst`,
not zero). `[HIGH/OBSERVED]`

---

## 2. TL;DR — the verdict in eight facts

1. **Opcode `0xe8 // Y`** byte-identical in all four gens' `aws_neuron_isa_tpb_common.h`
   (sunda:295 / cayman:298 / mariana:308 / maverick:314). Host enum
   `NEURON_ISA_TPB_OPCODE_COPY_PREDICATED_SCALAR`. `[HIGH/OBSERVED — §3]`
2. **Operand struct = `NEURON_ISA_TPB_S3D3_CP_PRED_SCALAR_STRUCT`**, a **dedicated 64-byte struct**,
   **compile-verified** (sizeof 64, every offset matches the header). It is the **sole**
   opcode on its struct in `instruction_mapping.json` (all four gens). `[HIGH/OBSERVED — §4]`
3. **No data-source tensor.** The struct carries one *predicate* tensor, one *dst* tensor, and a
   **scalar-immediate triple** `(src0_imm, src0_imm_src, reversed_pred)`. The replacement value is a
   single immediate, *not* a copied data tensor — the literal "Scalar" of the name. `[HIGH/OBSERVED — §4]`
4. **Dispatch = DVE engine, hardware-native.** `"S: CopyPredicatedScalar"` appears **exactly 4
   times** in `libnrtucode_internal.so`, each inside a per-gen `NX_DVE_DEBUG_DRAM` blob; POOL
   territory count **0**. Count 4 ⇒ DVE per the blob-multiplicity rule. `[HIGH/OBSERVED — §5]`
5. **Predicate = `pred_mem_pattern` (`pred_dtype`), an integer mask** — `is_valid_int_dtype_datapath`
   = exactly `{INT8, INT16, INT32, UINT8, UINT16, UINT32}`, the same 6-int set as the whole family.
   `[HIGH/OBSERVED — §6]`
6. **Semantics — predicate-gated scalar blend, default polarity INVERTED.** Header pseudocode:
   `reversed_pred==0` ⇒ *pred-TRUE keeps `dst`, pred-FALSE writes `src0`*; `reversed_pred==1` ⇒
   *pred-TRUE writes `src0`, pred-FALSE keeps `dst`* (the family convention). Both polarities **MERGE**
   the non-written lane (retain `dst`, not zero). `[HIGH/OBSERVED — §7]`
7. **Copy-only — no cast.** `in_dtype` **must equal** `out_dtype` (`has_same_inout_dtype`; header:
   *"copy only, no casting (DVE data converters off)"*). Both dtypes are `is_valid_dtype(FP32R::False)`
   = any plain ISA dtype **except FP32R / UINT64 / INT64**. `[HIGH/OBSERVED — §8]`
8. **Present in all four gens.** Opcode + full validator + 64-byte struct in every header; device
   self-name in CAYMAN / MARIANA / MARIANA_PLUS / MAVERICK DVE DEBUG blobs (SUNDA RELEASE-stripped).
   SUNDA/CAYMAN use `TENSOR3D` mem patterns; MARIANA/MAVERICK upgrade to `MEM_PATTERN3D` with
   INDIRECT (gather/scatter); MAVERICK adds tile-aware channel range. `[HIGH/OBSERVED — §9]`

---

## 3. Opcode & enum (byte-exact, all four gens)

```c
// aws_neuron_isa_tpb_common.h — the on-instruction opcode enum
NEURON_ISA_TPB_OPCODE_COPY_PREDICATED          = 0x72,   // Y   (predicated COPY, tensor src)
NEURON_ISA_TPB_OPCODE_CAST_PREDICATED          = 0x99,   // Y   (predicated CAST, tensor src)
NEURON_ISA_TPB_OPCODE_COPY_PREDICATED_SCALAR   = 0xe8,   // Y   <-- THIS KERNEL (scalar / immediate src)
NEURON_ISA_TPB_OPCODE_SELECT_REDUCE            = 0xea,   // Y   (device self-name "CopyPredicatedReduce")
```

The `// Y` trailer is the header legend's "tested / maintained / not-deprecated" flag — every
predicated-family opcode is actively maintained. Per-gen line numbers for
`COPY_PREDICATED_SCALAR = 0xe8 // Y` (the enum line is **byte-identical** across all four):

| gen | header line | enum byte |
|---|---|---|
| SUNDA (NC-v2)    | `aws_neuron_isa_tpb_common.h:295` | `0xe8 // Y` |
| CAYMAN (NC-v3)   | `:298` | `0xe8 // Y` |
| MARIANA (NC-v4)  | `:308` | `0xe8 // Y` |
| MAVERICK (NC-v5) | `:314` | `0xe8 // Y` |

`[HIGH/OBSERVED — all four `common.h`.]`

> **GOTCHA — `0xe8` is NOT adjacent to `0xea` in opcode value, but they are neighbours in the
> family.** `0xe8` (scalar copy) and `0xea` (`SELECT_REDUCE` / `CopyPredicatedReduce`) are two apart
> in the enum, with the unrelated `0xe9` in between; do not assume contiguity implies a shared
> struct. Bind by the *enumerator name* and the `instruction_mapping.json` struct, never by opcode
> proximity. The four family members live on **three distinct structs**.

---

## 4. The operand struct — `NEURON_ISA_TPB_S3D3_CP_PRED_SCALAR_STRUCT` (compile-verified, 64 B)

`CopyPredicatedScalar` is the **only** instruction decoded from this struct — a *dedicated*
operand format. Verbatim from
`…/neuron_cayman_arch_isa/tpb/aws_neuron_isa_tpb_s3d3_cp_pred_scalar.h` (struct lines 65-81):

```c
typedef struct NEURON_ISA_TPB_S3D3_CP_PRED_SCALAR_STRUCT {
    NEURON_ISA_TPB_HEADER             header;            // 4    ( 0 -  3)   opcode 0xe8 in header.opcode
    NEURON_ISA_TPB_EVENTS             events;            // 8    ( 4 - 11)   sync / event bits
    uint8_t                           reserved0[4];      // 4    (12 - 15)   must be 0
    NEURON_ISA_TPB_TENSOR3D           pred_mem_pattern;  // 16   (16 - 31)   PREDICATE mask tensor (read)
    NEURON_ISA_TPB_DTYPE              pred_dtype;        // 1    (32     )   predicate dtype (int-datapath only)
    uint8_t                           reversed_pred;     // 1    (33     )   polarity flip {0,1}  <-- THE flip bit
    uint8_t                           num_active_channels;// 1   (34     )   1..DVE_NUM_CHANNELS(=128)
    uint8_t                           reserved1[6];      // 6    (35 - 40)   must be 0
    NEURON_ISA_TPB_DTYPE              in_dtype;          // 1    (41     )   dtype of src0_imm (== out_dtype)
    NEURON_ISA_TPB_DTYPE              out_dtype;         // 1    (42     )   dtype of dst (== in_dtype; no cast)
    NEURON_ISA_TPB_IMM_SRC            src0_imm_src;      // 1    (43     )   {Inst(0), Ptr(1), RegPtr(2)}
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD src0_imm;          // 4    (44 - 47)   THE SCALAR value (or pointer)
    NEURON_ISA_TPB_TENSOR3D           dst_mem_pattern;   // 16   (48 - 63)   destination (read + write; MERGE)
} NEURON_ISA_TPB_S3D3_CP_PRED_SCALAR_STRUCT;

ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_S3D3_CP_PRED_SCALAR_STRUCT) == 64,
                  "Error: NEURON_ISA_TPB_S3D3_CP_PRED_SCALAR_STRUCT is NOT 64B.");
```

> **The structural difference from the family is the absence of a second source tensor.**
> `S3S3D3_TT` (copy/cast) carries `src0_mem_pattern` **and** `src1_mem_pattern` (predicate + data);
> `S2S2D2_STT` (reduce) carries `src0` + `src1` + `op0`/`op1`/`accumulator_cmd`. `S3D3_CP_PRED_SCALAR`
> carries **one** source tensor (the predicate), **one** dst tensor, and trades the missing data
> tensor for the **scalar-immediate triple** `(src0_imm @44, src0_imm_src @43, reversed_pred @33)`.
> No shared struct carries that triple, which is exactly why `0xe8` gets its own format.
> `[HIGH/OBSERVED — all three struct headers.]`

### 4.1 Compile-verify (all four gens)

Built the struct against the shipped CAYMAN tpb header with `gcc -I<cayman tpb dir>` (offsets are
byte-identical for all four gens):

```
sizeof = 64
header=0  events=4  reserved0=12  pred_mem_pattern=16  pred_dtype=32  reversed_pred=33
num_active_channels=34  reserved1=35  in_dtype=41  out_dtype=42  src0_imm_src=43
src0_imm=44  dst_mem_pattern=48
sizeof: TENSOR3D=16  DTYPE=1  IMM_SRC=1  IMM_VAL_INST_FIELD=4
```

Every offset matches the header annotation exactly; the `ISA_STATIC_ASSERT(… == 64)` holds in all
four gens. `[HIGH/OBSERVED — `gcc -Wall` clean.]`

### 4.2 The scalar-immediate triple

The three fields that replace the family's second tensor:

* **`src0_imm` (@44, 4 B)** — a `NEURON_ISA_TPB_IMM_VAL_INST_FIELD` **union** (verbatim,
  `common.h:866-874`): it can hold a typed scalar value (`imm_arith_fp32` / `imm_bitvec_int32` /
  `imm_bitvec_uint32` / `imm_bitvec_uint16[2]` / `imm_bitvec_uint8[4]`), a `PartitionOffset` pointer
  (`imm_ptr`), or a register handle (`imm_reg`). For a typical attention-mask fill this is the
  inline `-inf` / large-negative constant.
* **`src0_imm_src` (@43)** — `NEURON_ISA_TPB_IMM_SRC` (verbatim, `common.h:1207-1211`):
  ```c
  typedef enum NEURON_ISA_TPB_IMM_SRC {
      NEURON_ISA_TPB_IMM_SRC_INSTRUCTION_IMMEDIATE = 0,   // fp32 value embedded in the instruction
      NEURON_ISA_TPB_IMM_SRC_POINTER_IMMEDIATE     = 1,   // PartitionOffset pointer to per-lane immediates
      NEURON_ISA_TPB_IMM_SRC_REG_PTR_IMMEDIATE     = 2,   // PartitionOffset pointer held in a register
  } NEURON_ISA_PACKED NEURON_ISA_TPB_IMM_SRC;
  ```
  Validated by `has_valid_immediates` (`common.h:1356`): `InstructionImmediate` always valid;
  `PointerImmediate` requires the per-lane pointer be active-channel-bounded and dtype-aligned;
  `RegPtrImmediate` requires a valid imm-reg.
* **`reversed_pred` (@33)** — the polarity bit, validated by `has_valid_reversed_pred`
  (`common.h:1365-1368`): `reversed_pred == 0 || reversed_pred == 1` — *exactly two* legal values.

> **QUIRK — `PointerImmediate` makes the "scalar" per-lane, but it is still a scalar, not a tensor.**
> With `src0_imm_src == PointerImmediate(1)` the immediate is a `PartitionOffset` pointer "that has a
> different value for each lane" (header comment line 32) — so the fill value can vary across the 128
> partitions. It is still **one value fetched per lane** from the immediate region, **not** a data
> tensor with its own 3-D `num_elem`/`step`/`num_partitions` access pattern. The "no data tensor"
> distinction from `0x72` holds: there is no `src1_mem_pattern` in this struct at all.
> `[HIGH/OBSERVED — header purpose block + the struct field list.]`

### 4.3 Struct → opcode binding (`instruction_mapping.json`)

The CAYMAN `struct2opcode` map binds **exactly one** opcode to this struct, verbatim:

```json
"NEURON_ISA_TPB_S3D3_CP_PRED_SCALAR_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_COPY_PREDICATED_SCALAR"     // 0xe8 — sole occupant
]
```

A reverse query — *which struct(s) reference `COPY_PREDICATED_SCALAR`* — returns exactly
`NEURON_ISA_TPB_S3D3_CP_PRED_SCALAR_STRUCT`, nothing else, in all four gens. `0xe8` is therefore a
1-of-1 occupant of a dedicated struct, disjoint from the `S3S3D3_TT` of `0x72`/`0x99` and the
`S2S2D2_STT` of `0xea`. `[HIGH/OBSERVED — `instruction_mapping.json`.]`

---

## 5. Dispatch surface — DVE, not POOL

`libnrtucode_internal.so` packs, per generation and per build flavor
(PERF / TEST / DEBUG / RELEASE / DYNAMIC_KERNEL_LOAD), a separate IRAM/DRAM image per compute engine
(NX_DVE, NX_ACT, Q7_POOL, …). Each DEBUG image carries a self-name string table of `"S: <KernelName>\n"`
entries. The **count** of a given self-name across the whole file selects the engine family by the
blob-multiplicity rule: **16 = iTPB SEQUENCER, 4 = DVE, 3 = ACT/PE/POOL**.

### 5.1 DVE-presence — the self-name tag

`"S: CopyPredicatedScalar"` appears **exactly 4 times** in `libnrtucode_internal.so`
(`rg -c -a 'S: CopyPredicatedScalar'` → **4**), each inside a per-gen DVE DEBUG blob:

| file offset | enclosing blob | range (`nm -S`) | contained? |
|---|---|---|---|
| `0x18db90` | `CAYMAN_NX_DVE_DEBUG_DRAM_get.data`       | `0x18b320 .. 0x192080` (size `0x6d60`) | yes |
| `0x427e70` | `MARIANA_NX_DVE_DEBUG_DRAM_get.data`      | `0x425520 .. 0x42c520` (size `0x7000`) | yes |
| `0x6efb90` | `MARIANA_PLUS_NX_DVE_DEBUG_DRAM_get.data` | `0x6ed240 .. 0x6f43a0` (size `0x7160`) | yes |
| `0x8aff40` | `MAVERICK_NX_DVE_DEBUG_DRAM_get.data`     | `0x8ad5c0 .. 0x8b3540` (size `0x5f80`) | yes |

(Decimal byte offsets: `1629072 / 4357744 / 7273360 / 9109312`
= `0x18db90 / 0x427e70 / 0x6efb90 / 0x8aff40`.) Containment is arithmetic, not inferred: e.g.
`0x18b320 ≤ 0x18db90 < 0x192080`. The "×4" is the **four generations'** DVE DEBUG images, not four
lanes. Count 4 ⇒ DVE family. `[HIGH/OBSERVED]`

> **NOTE — the DVE blob lives in `.rodata`, so the offset *is* the file offset (no `.data` delta).**
> `readelf -SW` shows `.rodata` at VMA `0x46b0` == file offset `0x46b0` (`PROGBITS … AMS`), and `nm`
> types `CAYMAN_NX_DVE_DEBUG_DRAM_get.data` as `r` (read-only). The `0x200000` `.data`/`.data.rel.ro`
> delta that bites the ncore2gp *config* DLLs does **not** apply here — these blobs are
> `.rodata`-resident (the writable `.data`/`.data.rel.ro` sections sit at VMA `0x9b8cf0`/`0x9ba4a8`,
> far above these offsets), so a byte read at the file offset lands directly on the string.
> `[HIGH/OBSERVED — `readelf -SW` + `nm` symbol class.]`

The CAYMAN DVE kernel-name roster around the tag (dumped `0x18db40..0x18e260`) places
`CopyPredicatedScalar` squarely inside the **predicate-select cluster**:

```
S: MatchReplace          @0x18db50
S: TensorScalarSelect    @0x18db61
S: CopyPredicatedScalar  @0x18db90   <-- THIS
S: EngineNop             @0x18dba9
S: DveReadAccumulator    @0x18dbe0
S: FindIndex8            @0x18dbf7
S: CopyPredicated        @0x18dc20
S: CastPredicated        @0x18dc33
S: TensorTensorScan      @0x18dc70
…  S: CopyPredicatedReduce @0x18e16a   S: RangeSelect @0x18e1d3
```

It sits beside `TensorScalarSelect` (the immediate-predicate blend) and the rest of the
predicated-op family — all on DVE.

Self-name counts (`rg -c -a 'S: <name>' libnrtucode_internal.so`):

| self-name | count | family |
|---|---|---|
| `S: CopyPredicatedScalar` | 4 | DVE |
| `S: CopyPredicated` | 4 | DVE |
| `S: CastPredicated` | 4 | DVE |
| `S: CopyPredicatedReduce` | 4 | DVE |
| `S: TensorScalarSelect` | 4 | DVE |
| `S: RangeSelect` | 4 | DVE |

> **NOTE — counting `CopyPredicated`.** A naïve `rg -c 'CopyPredicated'` returns **12**, not 4,
> because `CopyPredicated` is a substring of both `CopyPredicatedScalar` and `CopyPredicatedReduce`
> (`12 = 4 + 4 + 4`). Always anchor counts to the full self-name string `S: CopyPredicatedScalar\n`,
> never a substring. `[HIGH/OBSERVED — CARRIED from copypredicatedreduce.md §4.]`

### 5.2 POOL-absence — direct byte scan

Scanning the entire CAYMAN POOL territory (`0x1cdc40 .. 0x310000` — the NX_POOL DEBUG +
`Q7_POOL_PERF_EXTISA` blobs) for `CopyPredicatedScalar` / `copy_predicated_scalar` / `CopyPredicated`
/ `CPS` returns **count 0** for every one (the file-wide `CopyPredicatedScalar` count of 4 is all in
the DVE blobs). The predicated kernels are **not** POOL software kernels. For contrast, plain
`Cast (0x47)` / `Copy (0x46)` **are** POOL kernels (see [Cast and Copy](cast-copy.md)) — the same
surface split that holds for the rest of the family: **plain = POOL, predicated = DVE.**

**Verdict: `CopyPredicatedScalar` dispatches on the DVE engine, hardware-native.**

---

## 6. The predicate — `pred_mem_pattern` (`pred_dtype`), integer mask

The validator gates the predicate dtype (`s3d3_cp_pred_scalar.h:101`, verbatim):

```rust
&& is_valid_int_dtype_datapath(i.s3d3_cp_pred_scalar.pred_dtype)
```

and `is_valid_int_dtype_datapath` is exactly six codes (CAYMAN `common.h:2415`, verbatim):

```rust
fn is_valid_int_dtype_datapath(dtype: Dtype) -> bool {
       (dtype == Dtype::INT8)    // 0x02
    || (dtype == Dtype::INT16)   // 0x04
    || (dtype == Dtype::INT32)   // 0x08
    || (dtype == Dtype::UINT8)   // 0x03
    || (dtype == Dtype::UINT16)  // 0x05
    || (dtype == Dtype::UINT32)  // 0x09
}
```

The header constraint block restates it verbatim: *"its data type `pred_dtype` must be one of
INT8/UINT8/INT16/UINT16/INT32/UINT32."* So the predicate is the **same 6-int set** as
`0x72` / `0x99` / `0xea` — read per element as a plain 8/16/32-bit signed-or-unsigned integer, where
any nonzero value is "true". `[HIGH/OBSERVED]`

> **QUIRK — the predicate excludes 64-bit ints and *all* floats.** `is_valid_int_dtype_datapath` is
> a strict subset of the general `is_valid_int_dtype` (which also admits `INT64`/`UINT64`): the mask
> **cannot** be a 64-bit int, an `INT4`, an `FP4`, or any float. This is the family-invariant
> predicate machinery; only the *scalar* domain (§8) and the *dst* addressing (§9) differ per gen.

There is **one predicate element per dst element**: `same_element_count_t3d(pred_mem_pattern,
dst_mem_pattern)` (SUNDA/CAYMAN) / `s3d3_cp_pred_scalar_src_dst_elements_same` (MARIANA+, §9). On the
DVE datapath the integer predicate is materialised to a `vbool` and consumed through the `bitkillt`
write-enable guard — see [§7](#7-semantics--the-predicate-gated-scalar-blend-the-decoded-behavior).
`[HIGH/OBSERVED validator; MED/CARRIED the vbool/bitkillt consume]`

---

## 7. Semantics — the predicate-gated scalar blend (the decoded behavior)

This is the page's central content. The arch-isa header ships the reference behavior as Python-style
pseudocode (`s3d3_cp_pred_scalar.h:42-47`, **verbatim**):

```python
# pred_tensor  - predicate tensor (pred_mem_pattern)
# src0         - value to replace on predication (src0_imm)
# reversed_pred - invert predication (bool)
# out_tensor   - output tensor (dst_mem_pattern)
for i in len(pred_tensor):
    if reversed_pred:
        out_tensor[i] = src0          if pred_tensor[i] else out_tensor[i]
    else:
        out_tensor[i] = out_tensor[i] if pred_tensor[i] else src0
```

Decomposed per-lane (`src0` = the scalar `src0_imm`; `pred` = `pred_mem_pattern[i]`, "true" = nonzero):

| `reversed_pred` | predicate TRUE (`!= 0`) | predicate FALSE (`== 0`) |
|---|---|---|
| **`0` (default)** | `out[i] = out[i]` — **KEEP `dst`** (no write) | `out[i] = src0` — **WRITE the scalar** |
| **`1`** | `out[i] = src0` — **WRITE the scalar** | `out[i] = out[i]` — **KEEP `dst`** (no write) |

> **CORRECTION — the DEFAULT polarity is INVERTED relative to the rest of the family.** In
> `0x72` / `0x99` / `0xea` the predicate-**true** lane writes the value and the predicate-**false**
> lane retains `dst`. In `0xe8`'s **default** (`reversed_pred == 0`) the predicate-true lane *retains*
> `dst` and the predicate-**false** lane writes the scalar — the exact inverse. Setting
> `reversed_pred == 1` recovers the family convention. This explicit polarity flip is **unique** to
> `0xe8` (no other family member has a `reversed_pred` field). It is what makes CPS convenient as a
> "fill the **un**-selected region with `-inf`" primitive (the default) *and* a "poke the selected
> region with a constant" primitive (reversed) from a single opcode, without ever inverting the
> caller's mask. The header's own narrative example — *"writes the immediate value to the destination
> tensor whenever the corresponding predicate value is true"* — describes the `reversed_pred == 1`
> reading; the struct **default** is the complementary fill. `[HIGH/OBSERVED — the header pseudocode
> + the `has_valid_reversed_pred` validator.]`

### 7.1 The merge — not zero

In **both** polarities the non-written lane **retains its prior `dst` value** — a read-modify-write
merge of `dst`, not a zero-fill and not skip-with-undefined. `dst_mem_pattern` is therefore both a
`WriteTensor::True` target *and* read for the retained lanes. On the DVE datapath this is the
`bitkillt`-guarded select: the integer predicate becomes a `vbool`, the scalar is splatted across
lanes, and the per-lane write-enable is the `bitkillt` mask, which raises an **all-ones** write-mask
in the *killed* (non-selected) lanes so the destination is held. The `reversed_pred` bit selects
`bitkillt` vs `bitkillf` (which polarity of lane is killed). `[HIGH/OBSERVED merge polarity; MED the
`bitkillt`/`bitkillf` + `reversed_pred` mapping — see the
[predicate ISS page](../../iss/cas-predicate-boolean.md).]`

### 7.2 Algorithm — annotated C pseudocode (naming the recovered symbols)

```c
// Symbols: NEURON_ISA_TPB_S3D3_CP_PRED_SCALAR_STRUCT,
//          NEURON_ISA_TPB_OPCODE_COPY_PREDICATED_SCALAR (0xe8),
//          is_valid_copy_predicated_scalar (the validator),
//          is_valid_int_dtype_datapath (the 6-int predicate gate),
//          has_same_inout_dtype (the no-cast gate),
//          NEURON_ISA_TPB_IMM_SRC {InstructionImmediate, PointerImmediate, RegPtrImmediate}.
//
// Contract is OBSERVED from s3d3_cp_pred_scalar.h; the DVE engine body is MED/INFERRED.
void CopyPredicatedScalar(const NEURON_ISA_TPB_S3D3_CP_PRED_SCALAR_STRUCT *i)
{
    /* no ALU op field, no cast (in_dtype == out_dtype, validated by has_same_inout_dtype),
       no reduce, no accumulator. */
    const Dtype d_pred = i->pred_dtype;   /* int8/16/32, uint8/16/32 (is_valid_int_dtype_datapath) */
    const Dtype d_io   = i->out_dtype;    /* == in_dtype, copy-only domain (no FP32R / no 64-bit) */

    /* resolve the single scalar value per src0_imm_src (one value, or one-per-lane for Ptr) */
    /* InstructionImmediate(0): inline; PointerImmediate(1): per-lane fetch; RegPtrImmediate(2): reg ptr */
    Scalar s = resolve_immediate(i->src0_imm, i->src0_imm_src, i->num_active_channels, d_io);

    /* element counts are equal (same_element_count_t3d / s3d3_cp_pred_scalar_src_dst_elements_same):
       one predicate element per dst element. */
    for (size_t e = 0; e < element_count(i->pred_mem_pattern); ++e) {
        int64_t p = load_int(i->pred_mem_pattern, d_pred, e);   /* integer predicate element */

        /* write where (pred-true XNOR reversed_pred): TRUE-and-reversed, or FALSE-and-default. */
        bool write = ((p != 0) == (i->reversed_pred != 0));

        if (write) {
            Scalar v = (i->src0_imm_src == POINTER_IMMEDIATE) ? per_lane(s, e) : s;
            store(i->dst_mem_pattern, d_io, e, v);  /* write the scalar immediate, no cast */
        } else {
            /* MERGE: dst[e] retains its prior value via the bitkillt all-ones write-mask. */
        }
    }
}
```

### 7.3 DVE datapath realisation (engine + name table OBSERVED; the splat/select body MED)

```c
// The per-lane integer predicate is loaded into a vbool; the scalar src0_imm is splatted across
// the 128 partitions (or fetched per-lane on PointerImmediate); the predicated commit goes through
// the bitkillt/bitkillf write-enable guard so the non-selected lanes RETAIN the destination (MERGE).
//
// reversed_pred selects bitkillt vs bitkillf (which polarity of lane is killed); there is NO
// data-converter pass (in_dtype == out_dtype), so the scalar is written bit-accurate.
vboolN pr    = load_predicate_to_vbool(pred_mem_pattern, pred_dtype);     // integer mask -> vbool
vecN   splat = splat_scalar(src0_imm, src0_imm_src);                       // one value across lanes
vboolN kill  = reversed_pred ? bitkillf(pr) : bitkillt(pr);                // pick kill polarity
dst          = select_t(/*killed=*/dst, /*live=*/splat, kill);            // "MERGE, NOT ZERO"
```

> **GOTCHA — the non-written lanes MERGE, they do not zero, and there is no cast.** This is the
> defining behavior. (1) The `bitkillt`/`bitkillf` guard retains the prior `dst` in non-selected
> lanes — *not* a constant fill, *not* skip-with-undefined. (2) Unlike `CastPredicated (0x99)`, there
> is **no** dtype conversion: `in_dtype == out_dtype` is hard-required, the header explicitly says
> *"DVE data converters off"*, and the scalar is written bit-accurate. (3) The written value is a
> single immediate, **not** a copied data tensor — there is no `src1_mem_pattern`.
> `[HIGH/OBSERVED merge polarity + the no-cast/scalar facts; MED the DVE body — see the
> [predicate ISS page](../../iss/cas-predicate-boolean.md).]`

---

## 8. The dtype matrix — copy only, no cast

`CopyPredicatedScalar` performs **no conversion** — the validator hard-requires `in_dtype == out_dtype`
(`s3d3_cp_pred_scalar.h:104`, body lines 126-128, verbatim):

```rust
fn has_same_inout_dtype(i: Inst) -> bool {
    (i.s3d3_cp_pred_scalar.in_dtype == i.s3d3_cp_pred_scalar.out_dtype)
}
```

and the header states it in prose: *"This instruction is copy only, no casting (DVE data converters
off). `in_dtype` has to be the same as `out_dtype` … both can be any valid ISA dtype except
FP32r/UINT64/INT64."* Both dtypes are admitted under `is_valid_dtype(…, DtypeAllowFP32R::False)`
(`s3d3_cp_pred_scalar.h:102-103`), and `is_valid_dtype` with `FP32R::False` *also* hard-codes
`DtypeAllowU64::False` + `DtypeAllowI64::False` (`common.h:1312-1317`, verbatim):

```rust
fn is_valid_dtype(dtype: Dtype, allow_fp32r: DtypeAllowFP32R) -> bool {
       dtype_invalid_check(dtype)
    && dtype_fp32r_illegal_check(dtype, allow_fp32r)        // FP32R::False -> FP32R rejected
    && dtype_uint64_illegal_check(dtype, DtypeAllowU64::False)   // UINT64 rejected
    && dtype_int64_illegal_check(dtype, DtypeAllowI64::False)    // INT64 rejected
    && is_valid_enum(EnumList::Dtype, dtype)
}
```

So the scalar/dst domain is **any plain ISA dtype except `FP32R 0xB`, `UINT64 0x1`, `INT64 0xC`**.
`[HIGH/OBSERVED]`

| field | role | constraint | legal set |
|---|---|---|---|
| `pred_dtype` | **predicate** (int-datapath) | `is_valid_int_dtype_datapath` | exactly `{INT8 0x2, INT16 0x4, INT32 0x8, UINT8 0x3, UINT16 0x5, UINT32 0x9}` |
| `in_dtype` | **scalar `src0_imm`** | `is_valid_dtype(FP32R::False)` **AND** `in_dtype == out_dtype` | `FP16 0x7, BFLOAT16 0x6, FP8_EXP3 0xD / EXP4 0xE / EXP5 0xF, FP32 0xA, INT8/16/32, UINT8/16/32` (+ gen FP4/CPTC at MARIANA+) |
| `out_dtype` | **dst** | `is_valid_dtype(FP32R::False)` **AND** `== in_dtype` | same domain as `in_dtype` |

Contrast with the family dtype rules:

* **`0x72` CopyPredicated** — `src1_dtype == out_dtype` (copy, no cast); `FP32R::False`.
* **`0x99` CastPredicated** — `src1_dtype` **may** `!= out_dtype` (cast via the FP32 hub).
* **`0xea` CopyPredicatedReduce** — data `FP32R::False` **and not a 32-bit int** (the reduce-max
  path lacks a 32-bit-int primitive); `out` allows `FP32R` (the wide accumulator).
* **`0xe8` CopyPredicatedScalar** — `in_dtype == out_dtype` (copy-only); `FP32R::False` **and no
  64-bit int**. **No cast, no FP32R, no 64-bit.** The scalar is a plain typed immediate in that
  domain.

> **NOTE — `FP32R` is excluded here but allowed for the reduce.** `CopyPredicatedScalar` forbids
> `FP32R` on *both* `in_dtype` and `out_dtype`, because it has no wide accumulator — it only copies a
> typed scalar. `CopyPredicatedReduce` allows `FP32R` on `out_dtype` precisely because its `out` is
> the running-max accumulator. The exclusion is structural, not arbitrary. `[HIGH/OBSERVED;
> CARRIED the FP32R-output-only fact]`

See the [unified datatype model](dtype-model.md) for the full code table and the FP32R / 64-bit
gating rationale.

---

## 9. Per-gen presence & evolution (SUNDA → MAVERICK)

| gen | `common.h` `0xe8 // Y` | struct (sizeof 64, compile-verified) | DVE self-name `"S: CopyPredicatedScalar"` |
|---|---|---|---|
| **SUNDA** (v2)    | YES (`:295`) | YES — `TENSOR3D` mem patterns | absent — only a DVE **RELEASE** blob ships (RELEASE strips self-name strings) |
| **CAYMAN** (v3)   | YES (`:298`) | YES — `TENSOR3D` mem patterns | **present** `@0x18db90` (DVE DEBUG_DRAM) |
| **MARIANA** (v4)  | YES (`:308`) | YES — `MEM_PATTERN3D` + INDIRECT | **present** `@0x427e70` (DVE DEBUG_DRAM) |
| **MAVERICK** (v5) | YES (`:314`) | YES — `MEM_PATTERN3D` + INDIRECT + tile | **present** `@0x8aff40` (DVE DEBUG_DRAM) |

`MARIANA_PLUS` is a MARIANA firmware respin — its DVE blob carries `CopyPredicatedScalar @0x6efb90`,
but it has **no** separate arch_isa header (only `sunda` / `cayman` / `mariana` / `maverick`
`neuron_*_arch_isa` directories exist).

### 9.1 The per-gen struct evolution (header diff)

The struct **layout is invariant** — sizeof 64 and every field offset is byte-identical across all
four gens (compile-verified). What evolves is the *type* of the mem-pattern fields and the *richness*
of the validator:

* **SUNDA / CAYMAN** — `pred_mem_pattern` and `dst_mem_pattern` are `NEURON_ISA_TPB_TENSOR3D`. The
  validator uses `tensor3d_valid` + `start_addr_active_channels` + `same_element_count_t3d` — a plain
  3-D tensor predicated scalar write.
* **MARIANA / MAVERICK** — the mem patterns become `NEURON_ISA_TPB_MEM_PATTERN3D` (the
  tensor-OR-indirect pattern union). The validator switches to `mem3d_valid` +
  `s3d3_cp_pred_scalar_src_dst_elements_same` (which matches element counts across tensor-pattern,
  indirect-pattern, and mixed tensor/indirect cases) + `indirect_quadrant_check_src3d` /
  `indirect_quadrant_check_src3d_dst3d`. **MARIANA+ therefore adds INDIRECT (gather/scatter)
  addressing**: the masked scalar write can target an indirect *scatter* destination.
* **MAVERICK additionally** — `has_valid_active_channel_range_with_tile(num_active_channels,
  DVE_NUM_CHANNELS, header.inst_flags)` replaces CAYMAN's `has_valid_active_channel_range`, i.e. a
  **tile-aware** channel-range check driven by `inst_flags` (the MAVERICK tiling feature).

The invariants across all four gens: opcode `0xe8 // Y`, the integer predicate rule, the
`in_dtype == out_dtype` copy-only rule, the `reversed_pred` bit, `DVE_NUM_CHANNELS = 128`, and
sizeof 64. `[HIGH/OBSERVED]`

> **NOTE — SUNDA's missing device string is a build-flavor artifact, not kernel absence.** SUNDA
> ships only a `SUNDA_NX_DVE_RELEASE_*` blob (no DEBUG variant), and RELEASE blobs of *every* gen
> likewise lack the `"S:"` self-name tags (the tags are a DEBUG-build feature). SUNDA presence rests
> on the ISA header (opcode + full validator + compile-verified struct, all OBSERVED), not the
> string. `[HIGH/OBSERVED opcode + validator + struct all four gens; MED SUNDA string-absence]`

> **v5/MAVERICK caution.** The MAVERICK opcode line, validator, and struct are **header-OBSERVED**;
> the MAVERICK DVE DEBUG blob self-name `@0x8aff40` is OBSERVED in the binary, but the v5 device
> *interior* (the per-gen DVE funcVA / Xtensa body — the splat + `bitkillt`/`bitkillf` select) is
> **not** byte-disassembled here and is treated as INFERRED where it touches v5-specific behavior.

---

## 10. Dispatch chain (opcode → struct → validator → DVE)

Host / compiler side (`[HIGH/OBSERVED]`): opcode `0xe8` (header) → `instruction_mapping.json` →
`S3D3_CP_PRED_SCALAR_STRUCT` → the validator `is_valid_copy_predicated_scalar`
(`s3d3_cp_pred_scalar.h:90-107`), which checks, in order: valid header/events, opcode ==
`CopyPredicatedScalar`, `reserved0`/`reserved1` all zero, `has_valid_immediates(src0_imm,
src0_imm_src, num_active_channels, in_dtype)`, pred + dst mem-pattern valid (`tensor3d_valid` on
SUNDA/CAYMAN; `mem3d_valid` + indirect-quadrant checks on MARIANA/MAVERICK), pred/dst start-addr
active-channel checks, `same_element_count_t3d` (pred == dst), `pred_dtype` integer, `in`/`out`
`FP32R::False`, `has_same_inout_dtype`, `has_valid_reversed_pred`, and active-channel range (tile-aware
on MAVERICK). The runtime then hands the 64-byte descriptor to the DVE engine; the gen's NX_DVE image
selects the `CopyPredicatedScalar` body by `header.opcode == 0xe8`.

Device side (`[MED/INFERRED]` — engine + name table OBSERVED; the exact DVE funcVA + Xtensa body not
carved): the DVE body (a) reads the per-lane integer predicate over `pred_mem_pattern` into a `vbool`,
(b) materialises the scalar `src0_imm` per `src0_imm_src` (inline / per-lane pointer / register
pointer) and splats it across lanes, and (c) writes via the `bitkillt`/`bitkillf`-guarded select
(`reversed_pred` selecting which kill polarity) so only the selected lanes take the scalar and the
rest retain `dst` (MERGE). The per-gen DVE funcVA + Xtensa instruction body is the one piece left
MED — the same boundary flagged for [CastPredicated](castpredicated.md) §10 and
[CopyPredicatedReduce](copypredicatedreduce.md) §13. `[HIGH struct/surface; MED device body]`

---

## 11. What CopyPredicatedScalar is FOR — the idiom

The header names the canonical use: *"replace parts of the destination tensor with a known immediate
(e.g. `-inf`)."* This is the **masked-constant-fill** primitive:

* **Attention / softmax masking** — fill the masked positions of a score tensor with `-inf` (or a
  large-negative constant) before the softmax; the predicate is the attention mask, the scalar is
  `-inf`. With `reversed_pred == 0` (default), the *un*-masked region keeps its scores and the masked
  region is filled — exactly the masked-attention pattern, no mask inversion needed.
* **Padding / boundary fill** — poke a constant into a predicate-selected region.
* **Complement of [RangeSelect](rangeselect.md)** — RangeSelect *computes* a predicate from two FP32
  bound compares and fills out-of-range lanes with `-FLT_MAX`; `CopyPredicatedScalar` takes the
  predicate as an **explicit integer mask tensor** and writes an **arbitrary** immediate. Same
  "fill the rejected region" shape, but caller-supplied mask + constant.

The `reversed_pred` bit lets one opcode serve both "fill where pred is true" and "fill where pred is
false" without inverting the caller's mask. The `PointerImmediate` source lets the fill value vary
per lane (a per-lane constant vector) while remaining a scalar-per-lane (not a data tensor).
`[HIGH/OBSERVED — header purpose block + the family relationships.]`

---

## 12. Relationships & the closed family

`CopyPredicatedScalar` **closes** the predicated-op family. The full matrix (byte-consistent with
[castpredicated.md](castpredicated.md) §9 and [copypredicatedreduce.md](copypredicatedreduce.md) §12):

| kernel | opcode | operand struct | value source | cast? | reduce? | **polarity** | predicate | accumulator |
|---|---|---|---|---|---|---|---|---|
| CopyPredicated | `0x72` | `S3S3D3_TT` | `src1` data tensor | no | no | TRUE writes | `src0` int | — |
| [CastPredicated](castpredicated.md) | `0x99` | `S3S3D3_TT` | `cast(src1` tensor`)` | **YES** | no | TRUE writes | `src0` int | — |
| **CopyPredicatedScalar** | **`0xe8`** | **`S3D3_CP_PRED_SCALAR`** | **`src0_imm` SCALAR** (inline / ptr / reg) | no | no | **`reversed_pred` flip; default FALSE writes** | `src0` int | — |
| [CopyPredicatedReduce](copypredicatedreduce.md) | `0xea` | `S2S2D2_STT` | `src1` tensor + accum | no | **MAX** | TRUE writes | `src0` int | Idle/Acc/ZeroAcc |

All four share: the **DVE engine** (×4 DVE DEBUG self-names, POOL count 0); the **`src0` integer
predicate** (`is_valid_int_dtype_datapath`, the same 6-int set); and the **`bitkillt` MERGE**
(predicate-non-selected lanes retain `dst`). They differ exactly as the matrix shows; `0xe8` is the
**only** member whose value is a SCALAR immediate (no data tensor) and the **only** one with an
explicit `reversed_pred` polarity flip.

Direct cross-links:

* **[CastPredicated](castpredicated.md) (`0x99`)** — the documented home of the tensor-copy
  `CopyPredicated (0x72)` and the tensor-cast `0x99`; shares the DVE / `src0` integer predicate /
  `bitkillt` merge machinery. `0xe8` replaces its second tensor source with a scalar immediate and
  adds the `reversed_pred` flip. `[HIGH]`
* **[CopyPredicatedReduce](copypredicatedreduce.md) (`0xea`, device self-name; host
  `SELECT_REDUCE`)** — co-resident in the **same four** DVE DEBUG blobs (`~0x5da` apart from
  `CopyPredicatedScalar` in each gen). Shares the DVE engine + integer predicate; differs by adding a
  masked reduce-MAX with a PSUM accumulator on the `S2S2D2_STT` struct. `[HIGH co-resident /
  OBSERVED]`
* **[RangeSelect](rangeselect.md) (`0xbc`)** — the nearest DVE predicate-select sibling that
  *generates* its predicate internally (FP32 bound compares); the `CopyPredicatedScalar` idiom
  (constant-fill the rejected region) is RangeSelect's with a caller-supplied mask + arbitrary
  constant. `[HIGH co-resident]`
* **[Cast and Copy](cast-copy.md) (`0x47` / `0x46`)** — the plain POOL/ACT base ops the predicated
  family wraps; `0xe8` is the predicated *scalar fill*, not a tensor copy.
* **[The unified datatype model](dtype-model.md)** — the dtype code table, the `FP32R`-output-only
  rule, and the 64-bit gating behind §8.
* **cas / fiss Predicate / Boolean (vbool) semantics** — [the predicate ISS
  page](../../iss/cas-predicate-boolean.md) (planned) — the `vbool` / `bitkillt` / `bitkillf` model
  behind the merge and the `reversed_pred` kill-polarity selection.

---

## 13. FLIX / literal-pool desync note

No host-side FLIX desync affects the primary facts on this page. The opcode enum (`0xe8 // Y`, all
four gens), the `S3D3_CP_PRED_SCALAR` struct layout (sizeof 64 + every offset, compile-verified all
four gens), the validator pseudocode (integer predicate, `in == out` copy-only, `reversed_pred`, the
per-gen indirect/tile evolution), the sole-occupant `instruction_mapping.json` binding, and the
`libnrtucode_internal.so` self-name offsets (`nm -S` range containment) are all sourced from the
in-package C interface headers and from the firmware string/symbol tables (`nm` / `readelf` / byte
scan) — none depend on Vision-Q7 FLIX bundle decode.

The one item **not** carved here — the per-gen DVE-image `CopyPredicatedScalar` funcVA and its
instruction-level body (the scalar splat + the `bitkillt`/`bitkillf` select + the `reversed_pred`
kill-polarity selection) — would require a device Xtensa disassembly
(`gpsimd_tools/tools/XtensaTools/bin/xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp`) of the DVE IRAM and
is the standard FLIX-desync risk surface. It is left MED and was **not** asserted byte-exact — the
same boundary flagged for the rest of the family. The `bitkillt`/`bitkillf` MERGE polarity and the
`reversed_pred` → kill-polarity mapping are OBSERVED at the predicate ISS source and CARRIED, not
re-derived from a device body.

---

## 14. Evidence index

**Headers** (in-package, `aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64`, `…/c10/include/`):

* `neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/aws_neuron_isa_tpb_common.h` — `0xe8 // Y`
  `COPY_PREDICATED_SCALAR` (sunda:295 / cayman:298 / mariana:308 / maverick:314); `IMM_SRC` enum
  (cayman:1207-1211); `IMM_VAL_INST_FIELD` union (cayman:866-874); `has_valid_immediates`
  (cayman:1356); `has_valid_reversed_pred` (cayman:1365-1368); `is_valid_dtype` FP32R/U64/I64 gate
  (cayman:1312-1317); `is_valid_int_dtype_datapath` 6-int set (cayman:2415); `DVE_NUM_CHANNELS = 128`
  (cayman:36).
* `neuron_cayman_arch_isa/tpb/aws_neuron_isa_tpb_s3d3_cp_pred_scalar.h` — struct lines 65-81 (64 B
  static-assert); the reference pseudocode lines 42-47 (the `reversed_pred` if/else); the constraint
  prose lines 26-61; validator `is_valid_copy_predicated_scalar` lines 90-107; `has_same_inout_dtype`
  lines 126-128; `has_copy_predicated_scalar_opcode` lines 109-111; `s3d3_cp_pred_scalar_reserved_zero`
  lines 113-124.
* `neuron_mariana_arch_isa/tpb/aws_neuron_isa_tpb_s3d3_cp_pred_scalar.h` — `MEM_PATTERN3D` patterns
  (lines 69, 78); `mem3d_valid` (96-97); `s3d3_cp_pred_scalar_src_dst_elements_same` (100, 132-145);
  `indirect_quadrant_check_src3d` / `_src3d_dst3d` (101-102).
* `neuron_maverick_arch_isa/tpb/aws_neuron_isa_tpb_s3d3_cp_pred_scalar.h` — `MEM_PATTERN3D` + indirect
  + `has_valid_active_channel_range_with_tile(…, header.inst_flags)` (108).
* `neuron_{…}_arch_isa/tpb/instruction_mapping.json` — `S3D3_CP_PRED_SCALAR_STRUCT →
  [COPY_PREDICATED_SCALAR]` (sole occupant; sunda:231 / cayman:247 / mariana:262 / maverick:280).

**Compile-verify:** `S3D3_CP_PRED_SCALAR` struct against the CAYMAN tpb header →
`sizeof = 64` + all offsets (§4.1), all four gens.

**Firmware** (in-package): `…/c10/lib/libnrtucode_internal.so`

* `"S: CopyPredicatedScalar"` ×4 `@ 0x18db90 / 0x427e70 / 0x6efb90 / 0x8aff40` (DVE DEBUG_DRAM per
  gen; byte offsets `1629072 / 4357744 / 7273360 / 9109312`).
* Blob ranges (`nm -S`): `CAYMAN_NX_DVE_DEBUG_DRAM_get.data @0x18b320 + 0x6d60`;
  `MARIANA @0x425520 + 0x7000`; `MARIANA_PLUS @0x6ed240 + 0x7160`; `MAVERICK @0x8ad5c0 + 0x5f80` —
  all four self-name offsets contained.
* `.rodata` VMA `0x46b0` == file offset `0x46b0` (`readelf -SW`) — DVE blobs are `.rodata`-resident,
  no `.data` delta; writable `.data.rel.ro @0x9b8cf0` / `.data @0x9ba4a8` sit far above.
* CAYMAN POOL territory (`0x1cdc40 .. 0x310000`) → **zero** `CopyPredicatedScalar` (POOL-absence proof).
* CAYMAN DVE roster `@0x18db40..0x18e260` (the predicate-select cluster, §5.1).

**Cross-links:** [CastPredicated](castpredicated.md) · [CopyPredicatedReduce](copypredicatedreduce.md) ·
[RangeSelect](rangeselect.md) · [Cast and Copy](cast-copy.md) · [the dtype model](dtype-model.md) ·
[the predicate ISS page](../../iss/cas-predicate-boolean.md).
