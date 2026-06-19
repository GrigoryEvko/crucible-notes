# CastPredicated — opcode `0x99`

`CastPredicated` is the **predicate-gated dtype CAST**: for every output element it converts the
*data* tensor (`src1`, carried as `dtype_hi`) to `out_dtype` and writes the result to `dst`
**only where the *predicate* tensor (`src0`, `dtype_lo`, an integer mask) is true**. Where the
predicate is false the destination lane is **left unchanged** — a *merge / passthrough* of the
prior `dst` value, not a fill and not a skip-with-undefined. It is opcode **`0x99`**, it is
present in **all four** byte-grounded GPSIMD ISA generations (SUNDA / CAYMAN / MARIANA /
MAVERICK), and unlike its sibling cluster it runs **hardware-native on the DVE engine** rather
than as a Q7-POOL software kernel.

It is the exact mirror of the plain [Cast / Copy](./cast-copy.md) base kernel, one level up: where
plain `Cast (0x47)` differs from plain `Copy (0x46)` only by permitting `src1_dtype != out_dtype`,
`CastPredicated (0x99)` differs from [`CopyPredicated (0x72)`](./copypredicatedscalar.md) only by
that same dtype relaxation. It belongs to the **predicated-op family** —
`CopyPredicated (0x72)` / `CastPredicated (0x99)` / `CopyPredicatedScalar (0xe8)` and the
device-side `CopyPredicatedReduce` (ISA-named `SELECT_REDUCE`, opcode `0xea`) — and pairs most
directly with [CopyPredicatedReduce](./copypredicatedreduce.md): same DVE surface, same integer
predicate, same merge semantics; `CastPredicated` *converts* dtype and does *not* reduce, the
Reduce does the opposite.

Confidence convention on this page: `[HIGH/OBSERVED]` = read directly from byte / header / compile;
`[MED/INFERRED]` = reasoned over an OBSERVED fact; `[…/CARRIED]` = re-used from a sibling report at
its stated confidence without re-reading the artifact this pass. Every count is re-grounded to
`nm`/`rg -c` on the shipped binary, never the decompile.

> **NOTE — provenance.** Every primary fact below derives from the shipped customop-lib package
> `aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64`: the in-package arch-isa C interface headers
> `…/c10/include/neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/` (the authoritative
> opcode / operand-struct / enum / validity contract), a struct compile-verify against those
> headers, and the device firmware container `…/c10/lib/libnrtucode_internal.so` read with `nm`,
> `readelf`, and a byte scan. The headers live under `extracted/` (gitignored — reach with
> `fd --no-ignore` or an absolute path).

---

## 1. TL;DR — the verdict in seven facts

1. **Opcode `0x99 // Y`** in all four gens' `aws_neuron_isa_tpb_common.h`, byte-identical enum
   line. `COPY_PREDICATED = 0x72 // Y` is the dtype-preserving sibling.
   `[HIGH/OBSERVED — §2]`
2. **Operand struct = `NEURON_ISA_TPB_S3S3D3_TT_STRUCT`**, the two-source tensor-tensor struct,
   **64 B, compile-verified this pass** (`gcc -I<cayman tpb hdr>`, `sizeof == 64`, every offset
   matches the header). `instruction_mapping.json` binds `CAST_PREDICATED` + `COPY_PREDICATED` to
   `S3S3D3_TT` — one of **five** opcodes on that struct. `[HIGH/OBSERVED — §3]`
3. **Dispatch surface = DVE engine, hardware-native** — *not* a POOL software kernel. Proven two
   ways: the entire CAYMAN POOL territory contains **zero** occurrences of any predicated-kernel
   name, and `"S: CastPredicated"` appears **exactly 4 times** in `libnrtucode_internal.so`, each
   inside a per-gen DVE `DEBUG_DRAM` blob (count 4 = DVE per the blob-multiplicity rule).
   `[HIGH/OBSERVED — §4]`
4. **Predicate = `src0` (`dtype_lo`)**, an integer mask, gated by
   `s3s3d3_copy_pred_src0_dtype ⇒ is_valid_int_dtype_datapath(dtype_lo)` — exactly the set
   `{INT8, INT16, INT32, UINT8, UINT16, UINT32}`. One predicate element per output element.
   `[HIGH/OBSERVED — §5]`
5. **The cast = `src1` (`dtype_hi`) → `out_dtype` through the FP32 hub** — the *same* any→any
   convert as plain `Cast (0x47)`: native `fp16 ↔ fp32` only; `bf16` and all `fp8`/`fp4` widths
   route through FP32. The validator `s3s3d3_copy_cast_pred_src1_dst_dtype` *short-circuits true*
   on `opcode == CastPredicated`, which is the ENTIRE difference from `CopyPredicated`.
   `[HIGH/OBSERVED — §6]`
6. **Predicate-false lanes KEEP the prior `dst` value (MERGE).** On the DVE datapath the per-lane
   integer predicate becomes a `vbool` and the predicated write commits through the `bitkillt`
   write-enable guard, which raises an all-ones mask in *killed* lanes so they retain their
   destination — "merge, not zero". `op` **must** be `AluOp::Bypass (0x00)` — no arithmetic.
   `[merge polarity HIGH/OBSERVED from the ISS predicate model; that THIS op uses that path
   MED/INFERRED — §7]`
7. **Present in all four gens** (opcode + full validator OBSERVED byte-identical in every header,
   already at SUNDA — unlike RangeSelect which is CAYMAN+). The device self-name string is in the
   CAYMAN/MARIANA/MARIANA_PLUS/MAVERICK DVE *DEBUG* blobs; its absence in SUNDA is a RELEASE-build
   artifact, not kernel absence. `[opcode+validator HIGH/OBSERVED; SUNDA string-absence
   MED/INFERRED — §8]`

---

## 2. Opcode & enum (byte-exact, all four gens)

```c
// aws_neuron_isa_tpb_common.h — the on-instruction opcode enum
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_SELECT  = 0x98,   // SortMerge wip 0x97  // Y
NEURON_ISA_TPB_OPCODE_CAST_PREDICATED       = 0x99,   // Y    <-- THIS KERNEL
NEURON_ISA_TPB_OPCODE_COPY_PREDICATED       = 0x72,   // Y    (dtype-preserving sibling)
NEURON_ISA_TPB_OPCODE_COPY_PREDICATED_SCALAR= 0xe8,   // Y    (scalar-src predicated copy)
NEURON_ISA_TPB_OPCODE_SELECT_REDUCE         = 0xea,   // Y    (device self-name "CopyPredicatedReduce")
```

The `// Y` trailer is the header legend's "tested / maintained / not-deprecated" flag — every
predicated-family opcode is actively maintained.

Per-gen line numbers for `CAST_PREDICATED = 0x99 // Y` (read this pass; the enum line is
**byte-identical** across all four):

| gen | header line | enum byte |
|---|---|---|
| SUNDA (NC-v2)    | `aws_neuron_isa_tpb_common.h:235` | `0x99 // Y` |
| CAYMAN (NC-v3)   | `:232` | `0x99 // Y` |
| MARIANA (NC-v4)  | `:237` | `0x99 // Y` |
| MAVERICK (NC-v5) | `:240` | `0x99 // Y` |

`[HIGH/OBSERVED — `rg -n 'CAST_PREDICATED.*0x99'` over all four `common.h` this pass.]`

> **GOTCHA — `0x98 TENSOR_SCALAR_SELECT` sits immediately below `0x99` and carries the comment
> `SortMerge wip 0x97`.** That neighbour is a *different* op (tensor-scalar select, a DVE
> predicate sibling), and the dangling `0x97` in its comment is a historical SortMerge opcode, not
> CastPredicated. Read the enumerator name, never the trailing wip-comment, when binding an
> opcode.

---

## 3. The operand struct — `NEURON_ISA_TPB_S3S3D3_TT_STRUCT` (compile-verified, 64 B)

`CastPredicated` is decoded from the **two-source tensor-tensor** struct. Verbatim from
`…/neuron_cayman_arch_isa/tpb/aws_neuron_isa_tpb_s3s3d3_tt.h`:

```c
typedef struct NEURON_ISA_TPB_S3S3D3_TT_STRUCT {
    NEURON_ISA_TPB_HEADER     header;               // 4    ( 0 -  3)   opcode 0x99 lives in header.opcode
    NEURON_ISA_TPB_EVENTS     events;               // 8    ( 4 - 11)   sync / event bits
    NEURON_ISA_TPB_DTYPE_PAIR in0_in1_dtype;        // 1    (12     )   dtype_lo=src0 PREDICATE, dtype_hi=src1 DATA
    NEURON_ISA_TPB_DTYPE      out_dtype;            // 1    (13     )   cast TARGET dtype (= dst)
    NEURON_ISA_TPB_ALU_OP     op;                   // 1    (14     )   MUST be AluOp::Bypass (0x00)
    uint8_t                   num_active_channels;  // 1    (15     )
    NEURON_ISA_TPB_TENSOR3D   src0_mem_pattern;     // 16   (16 - 31)   PREDICATE mask tensor   (read)
    NEURON_ISA_TPB_TENSOR3D   src1_mem_pattern;     // 16   (32 - 47)   DATA tensor to cast     (read)
    NEURON_ISA_TPB_TENSOR3D   dst_mem_pattern;      // 16   (48 - 63)   destination             (write)
} NEURON_ISA_TPB_S3S3D3_TT_STRUCT;

ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_S3S3D3_TT_STRUCT) == 64,
                  "Error: NEURON_ISA_TPB_S3S3D3_TT_STRUCT is NOT 64B.");
```

> **The `DTYPE_PAIR` packing is the crux of the data path.** `in0_in1_dtype` is one byte holding
> two 4-bit nibbles: `dtype_lo` (low nibble) types **`src0` = the predicate**, `dtype_hi` (high
> nibble) types **`src1` = the data**. The comment in the header reads verbatim
> `// src0 low, src1 high`. The `out_dtype` (separate full byte @offset 13) types `dst`. So a
> single instruction carries *three* dtype fields: predicate, data, and target. Because the pair
> is nibble-packed, both `src0` and `src1` dtypes are restricted to the BASIC `≤ 0xF` codes — the
> extended `0x10..0x1F` dtypes (FP4 / INT4 / SFP8 / CPTC) cannot ride a `DTYPE_PAIR` nibble. See
> the [dtype model](./dtype-model.md). `[HIGH/OBSERVED — header bitfield + comment.]`

### 3.1 Compile-verify (this pass)

Built the struct against the shipped CAYMAN tpb header with `gcc -I<cayman tpb dir>`:

```
sizeof = 64
header=0  events=4  in0_in1_dtype=12  out_dtype=13  op=14  nac=15
src0=16   src1=32   dst=48
sizeof: TENSOR3D=16  DTYPE_PAIR=1  DTYPE=1  ALU_OP=1
```

Every offset matches the header annotation exactly; the `ISA_STATIC_ASSERT(… == 64)` holds.
`[HIGH/OBSERVED — `gcc -Wall` clean, struct executed this pass.]`

### 3.2 Struct → opcode binding (`instruction_mapping.json`)

The CAYMAN `struct2opcode` map binds **five** opcodes to `S3S3D3_TT`, verbatim:

```json
"NEURON_ISA_TPB_S3S3D3_TT_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_ARITH_OP",
    "NEURON_ISA_TPB_OPCODE_TENSOR_TENSOR_BITVEC_OP",
    "NEURON_ISA_TPB_OPCODE_COPY_PREDICATED",
    "NEURON_ISA_TPB_OPCODE_CAST_PREDICATED",     // <-- 0x99
    "NEURON_ISA_TPB_OPCODE_BATCH_NORM_BACK_PROP"
]
```

So `CAST_PREDICATED` + `COPY_PREDICATED` share the struct with the two element-wise
tensor-tensor ALU ops and BatchNormBackProp — they all carry two source tensors + one
destination, and the validator (§5) sub-selects by opcode. `[HIGH/OBSERVED — `jq` over
`neuron_cayman_arch_isa/tpb/instruction_mapping.json` this pass.]`

> **CORRECTION — `CopyPredicatedScalar (0xe8)` is on a *different* struct, and `0xea` is on a
> third.** `COPY_PREDICATED_SCALAR (0xe8)` binds to a separate scalar-source struct, and
> `SELECT_REDUCE (0xea)` — whose *device firmware self-name* is `CopyPredicatedReduce` — binds to
> `NEURON_ISA_TPB_S2S2D2_STT_STRUCT`, **not** `S3S3D3_TT` (see §9 and the reconciliation note).
> Only `0x72`/`0x99` ride `S3S3D3_TT`.

---

## 4. Dispatch surface — DVE, not POOL

The firmware container `libnrtucode_internal.so` packs, per generation and per build flavor
(PERF / TEST / DEBUG / RELEASE / DYNAMIC_KERNEL_LOAD), a separate IRAM/DRAM image for each compute
engine (NX_DVE, NX_ACT, Q7_POOL, …). Each DEBUG image carries a device self-name string table of
`"S: <KernelName>\n"` entries. The **count** of a given self-name across the whole file selects
the engine family by the blob-multiplicity rule: **16 = iTPB SEQUENCER, 4 = DVE, 3 = ACT/PE/POOL**.

### 4.1 POOL-absence — direct byte scan (this pass)

Scanning the entire CAYMAN POOL territory — the POOL PERF/TEST/DEBUG/DYNAMIC IRAM+DRAM images
(`CAYMAN_Q7_POOL_PERF_DRAM_get.data @0x20abc0 + 0x13200`) plus all four
`CAYMAN_Q7_POOL_PERF_EXTISA_{0..3}_SO` carved ext-isa blobs — for each of
`CastPredicated / CopyPredicated / CopyPredicatedReduce / CopyPredicatedScalar / RangeSelect /
TensorScalarSelect` returns **count 0** for every one, as does a scan for `cast_pred` /
`CAST_PRED`. The predicated kernels are **not** POOL software kernels.

For contrast, the plain `Cast (0x47)` / `Copy (0x46)` base ops **are** POOL kernels (their
`kernel_info_table` entries and POOL funcVAs are documented on [Cast / Copy](./cast-copy.md)) —
confirming the surface split: **plain = POOL, predicated = DVE**. `[HIGH/OBSERVED.]`

### 4.2 DVE-presence — the self-name tag (this pass)

`"S: CastPredicated\n"` appears **exactly 4 times** in `libnrtucode_internal.so`
(`rg -c -a 'S: CastPredicated'` → **4** this pass), at file offsets:

| file offset | enclosing blob | range (`nm -S`) |
|---|---|---|
| `0x18dc33` | `CAYMAN_NX_DVE_DEBUG_DRAM_get.data`       | `0x18b320 .. 0x192080` (size `0x6d60`) |
| `0x427f13` | `MARIANA_NX_DVE_DEBUG_DRAM_get.data`      | `0x425520 .. 0x42c520` |
| `0x6efc33` | `MARIANA_PLUS_NX_DVE_DEBUG_DRAM_get.data` | `0x6ed240 .. 0x6f43a0` |
| `0x8affe3` | `MAVERICK_NX_DVE_DEBUG_DRAM_get.data`     | `0x8ad5c0 .. 0x8b3540` |

The "×4" is the **four generations'** DVE DEBUG images (one CastPredicated per gen-DVE blob), not
four lanes. Count 4 ⇒ DVE family. The CAYMAN offset `0x18dc33` is confirmed inside the DVE blob
range `[0x18b320, 0x192080)` (verified arithmetically this pass).

> **NOTE — the DVE blob lives in `.rodata`, so the offset *is* the file offset (no `.data`
> delta).** `readelf -SW` shows `.rodata` at VMA `0x46b0` == file offset `0x46b0`
> (`PROGBITS … AMS`), and `nm` types `CAYMAN_NX_DVE_DEBUG_DRAM_get.data` as `r` (read-only). The
> `0x200000` `.data`/`.data.rel.ro` delta that bites the ncore2gp *config* DLLs does **not** apply
> here — these blobs are `.rodata`-resident, so `dd skip=<offset>` reads them directly.
> `[HIGH/OBSERVED — `readelf -SW`, `nm` symbol class this pass.]`

The context bytes at `0x18dc00` dump (printable-filtered) read:

```
…S: CopyPredicated\n\0S: CastPredicated\n\0…S: TensorTensorScan…
```

i.e. CastPredicated is emitted in the DVE kernel-name table **immediately after** CopyPredicated.
The full CAYMAN DVE DEBUG kernel-name roster (one `"S: <Name>"` per DVE kernel) is the
**predicate/select cluster + the index/scan family** — all the kernels that share this engine:

```
S: CastPredicated   S: CopyPredicated   S: CopyPredicatedReduce   S: CopyPredicatedScalar
S: RangeSelect      S: TensorScalarSelect   S: DveReadAccumulator  S: DveReadIndices
S: FindIndex8       S: MatchReplace      S: TensorTensorScan       S: EngineNop …
```

Self-name counts re-grounded this pass (`rg -c -a 'S: <name>' libnrtucode_internal.so`):

| self-name | count | family |
|---|---|---|
| `S: CastPredicated` | 4 | DVE |
| `S: CopyPredicated` | 4 | DVE |
| `S: CopyPredicatedReduce` | 4 | DVE |
| `S: CopyPredicatedScalar` | 4 | DVE |
| `S: RangeSelect` | 4 | DVE |
| `S: TensorScalarSelect` | 4 | DVE |
| `S: SelectReduce` | **0** | — (no such device self-name; see §9) |
| `S: Cast\n` | 3 | ACT |
| `S: Copy\n` | 3 | ACT |

The plain `Cast`/`Copy` self-names (**count 3 = ACT family**) live in the *ACT* engine DEBUG blob
(`CAYMAN_NX_ACT_DEBUG_DRAM_get.data @0x169400`), the hardware Activation-engine native cast path.
This 3-vs-4 split is exactly the blob-multiplicity rule and independently corroborates
`CastPredicated = DVE`. `[HIGH/OBSERVED — every count re-run this pass.]`

**Verdict: `CastPredicated` dispatches on the DVE engine, hardware-native.** `[HIGH/OBSERVED]`

---

## 5. The validator — `is_valid_copy_cast_predicated` (header pseudocode, verbatim)

The arch-isa header ships the legality algebra as Rust-syntax comments — the specification both
the host ucode decoder and the device firmware implement. The entry point is the disjunction at
the top of `s3s3d3_tt.h`:

```rust
fn is_valid_s3s3d3_tt(i: Inst) -> bool {
       is_valid_tensor_tensor(i)
    || is_valid_batchnorm_backprop(i)
    || is_valid_copy_cast_predicated(i)        // <-- CastPredicated lands here
}
```

The predicated leg, verbatim (CAYMAN `s3s3d3_tt.h:93-112`):

```rust
fn is_valid_copy_cast_predicated(i: Inst) -> bool {
       has_valid_neuron_header(i)
    && has_valid_neuron_events(i)
    && has_copy_cast_predicated_opcode(i)                                          // (a) opcode 0x72|0x99
    && is_valid_dtype(i.s3s3d3_tt.out_dtype,                       AllowFP32R::False)   // (b) plain out dtype
    && is_valid_dtype(get_dtype_from_pair(i.in0_in1_dtype.dtype_lo), AllowFP32R::False) // (c) plain predicate dtype
    && is_valid_dtype(get_dtype_from_pair(i.in0_in1_dtype.dtype_hi), AllowFP32R::False) // (d) plain data dtype
    && s3s3d3_copy_pred_src0_dtype(i)                                              // (e) PREDICATE = integer
    && s3s3d3_copy_cast_pred_src1_dst_dtype(i)                                     // (f) CAST relaxation
    && s3s3d3_tt_is_zero_op(i)                                                     // (g) op == Bypass
    && check_active_channels(i.s3s3d3_tt.num_active_channels)
    && start_addr_active_channels(i.src0_mem_pattern.start_addr, i.num_active_channels)
    && start_addr_active_channels(i.src1_mem_pattern.start_addr, i.num_active_channels)
    && start_addr_active_channels(i.dst_mem_pattern.start_addr,  i.num_active_channels)
    && tensor3d_valid(i.src0_mem_pattern, dtype_lo, WriteTensor::False, InPSUM::True, InSBUF::True) // predicate READ
    && tensor3d_valid(i.src1_mem_pattern, dtype_hi, WriteTensor::False, InPSUM::True, InSBUF::True) // data READ
    && tensor3d_valid(i.dst_mem_pattern,  out_dtype, WriteTensor::True,  InPSUM::True, InSBUF::True) // dst  WRITE
    && s3s3d3_tt_src_element_cnt_check(i)                                          // (h) src0 == src1 elem count
    && s3s3d3_tt_dst_element_cnt_check(i)                                          // (i) src0 == dst  elem count
    && tt_valid_partitions(i.src0_mem_pattern.start_addr, i.src1_mem_pattern.start_addr)
}
```

The opcode gate (verbatim, `s3s3d3_tt.h:142-145`):

```rust
fn has_copy_cast_predicated_opcode(i: Inst) -> bool {
       (i.header.opcode == Opcode::CopyPredicated)    // 0x72
    || (i.header.opcode == Opcode::CastPredicated)    // 0x99
}
```

### 5.1 The predicate-dtype gate — `src0` is integer-only

```rust
fn s3s3d3_copy_pred_src0_dtype(i: Inst) -> bool {
    is_valid_int_dtype_datapath(get_dtype_from_pair(i.in0_in1_dtype.dtype_lo))
}
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

> **QUIRK — the predicate excludes 64-bit ints and *all* floats.** `is_valid_int_dtype_datapath`
> is a strict subset of `is_valid_int_dtype` (which also admits `INT64=0xC` / `UINT64=0x1`): the
> predicate mask **cannot** be a 64-bit int, an `INT4`, an `FP4`, or any float. The mask is read
> as a plain 8/16/32-bit signed-or-unsigned integer and any nonzero value is "true". This is the
> *same* int-datapath set CopyPredicatedReduce uses for its mask. `[HIGH/OBSERVED — both gate
> bodies read this pass.]`

### 5.2 The `op == Bypass` gate

```rust
fn s3s3d3_tt_is_zero_op(i: Inst) -> bool {
    i.s3s3d3_tt.op == AluOp::Bypass     // AluOp::Bypass = 0x00
}
```

`NEURON_ISA_TPB_ALU_OP_BYPASS = 0x00` (CAYMAN `common.h:940`). There is **no arithmetic** — the
op field must be Bypass; the only work is convert + predicated select. `[HIGH/OBSERVED]`

### 5.3 The shape rule

`s3s3d3_tt_src_element_cnt_check` and `…_dst_element_cnt_check` require `src0`, `src1`, and `dst`
to have **equal total element counts** (`num_elem[0]*num_elem[1]*num_elem[2]`), unless a
start-addr is register-shaped. There is **one predicate element per data element per output
element**, and all three tensors are 3-D mem-patterns that may live in PSUM or SBUF. `dst` is the
only `WriteTensor::True`. `[HIGH/OBSERVED — `s3s3d3_tt.h:116-127`.]`

---

## 6. The cast — `src1 → out_dtype` through the FP32 hub

The *entire* dtype difference between `CastPredicated (0x99)` and `CopyPredicated (0x72)` is one
short-circuit (CAYMAN `s3s3d3_tt.h:195-198`, verbatim):

```rust
fn s3s3d3_copy_cast_pred_src1_dst_dtype(i: Inst) -> bool {
       (i.header.opcode == Opcode::CastPredicated)               // cast: short-circuit TRUE
    || (get_dtype_from_pair(i.in0_in1_dtype.dtype_hi) == i.out_dtype) // copy: dtype_hi must equal out_dtype
}
// header line 208: "Cast can change dtype, copy cannot"
```

So `CastPredicated` *bypasses* the `dtype_hi == out_dtype` equality and permits
`src1_dtype != out_dtype` — that **is** the cast. `CopyPredicated` keeps `dtype_hi == out_dtype`
(a bit-accurate transport, no conversion). This is structurally identical to how plain
`Cast (0x47)` relaxes the equality that plain `Copy (0x46)` enforces; the predicated pair is the
mirror of the plain pair, one validator-leg up.

The conversion arithmetic itself is the **same any→any FP32-hub convert** as plain `Cast`, fully
specified on [the dtype model](./dtype-model.md):

* **Native float converts: `fp16 ↔ fp32` only** (`cvtf16f32` / `cvtf32f16`).
* **`bf16` and all three `fp8` formats have no native convert op** — every one is realised
  *through* the FP32 intermediate: `Cast = in → FP32 → out`, numpy-`astype` semantics,
  round-to-nearest-even at narrowing.
* **`in_dtype == out_dtype` degenerates to a bit-accurate move** (the Copy case).

`out_dtype` is admitted under `is_valid_dtype(…, AllowFP32R::False)` for the predicated path
(line 97) — i.e. plain dtypes, **no FP32R** (the rounded "FP22" output form is *not* a legal
predicated cast target). Both source dtypes are likewise `AllowFP32R::False` (lines 98-99).

### 6.1 The dtype matrix

`[HIGH/OBSERVED for the gate predicates; the FP32-hub routing CARRIED from the dtype model.]`

| field | role | legal set |
|---|---|---|
| `src0` / `dtype_lo` | **predicate** (int-datapath) | exactly `{INT8 0x2, INT16 0x4, INT32 0x8, UINT8 0x3, UINT16 0x5, UINT32 0x9}` |
| `src1` / `dtype_hi` | **data** (`AllowFP32R::False`) | any non-FP32R plain dtype the gen defines, `≤ 0xF` (nibble-packed) |
| `out_dtype` | **cast target** (`AllowFP32R::False`) | same domain as `src1`, `≤ 0xF` |

The legal **cast pairs** are the full `src1 × out` cross-product (`src1` may differ from `out`),
realised via the FP32 hub: floats `{FP16 0x7, BFLOAT16 0x6, FP8_EXP3 0xD, FP8_EXP4 0xE,
FP8_EXP5 0xF, FP32 0xA}` and ints `{INT8/16/32, UINT8/16/32}` (with `INT64 0xC`/`UINT64 0x1`
*excluded* from the nibble-packed pair). Native fp converts are `FP16 ↔ FP32` only; every other
float crossing goes through FP32.

> **NOTE — the data/target domain widens per generation; the predicate set does not.** The base
> 16-code dtype space (`0x0..0xF`, SUNDA == CAYMAN byte-identical) grows at MARIANA
> (`+FP4_EXP2 0x10`, `+CPTC1..7 0x19..0x1F`) and again at MAVERICK
> (`+FP8_EXP2 0x11`, `+INT4 0x12`, `+SFP8_E8..E5 0x13..0x16`). But the `≤ 0xF`
> nibble-packing of `DTYPE_PAIR` means the *predicated* `src1`/`out` cast domain is effectively
> the BASIC subset regardless of gen, and the 6-int **predicate** set is **invariant** across all
> four. `[HIGH/OBSERVED for the predicate set; gen superset CARRIED from the dtype model.]`

---

## 7. Algorithm — the decoded behavior

The contract (validator + struct) is OBSERVED; the device body byte-trace is the one MED piece
(the DVE IRAM was not disassembled this pass — see §10).

```c
// Symbols: NEURON_ISA_TPB_S3S3D3_TT_STRUCT, NEURON_ISA_TPB_OPCODE_CAST_PREDICATED (0x99),
//          NEURON_ISA_TPB_ALU_OP_BYPASS (0x00), s3s3d3_copy_pred_src0_dtype,
//          s3s3d3_copy_cast_pred_src1_dst_dtype, is_valid_int_dtype_datapath.
//
// Contract is OBSERVED from s3s3d3_tt.h; the DVE engine body is MED/INFERRED.
void CastPredicated(const NEURON_ISA_TPB_S3S3D3_TT_STRUCT *i)
{
    /* op MUST be Bypass — validated by s3s3d3_tt_is_zero_op; no arithmetic. */
    /* src0 == src1 == dst element count — validated by *_element_cnt_check. */
    const Dtype d_pred = dtype_lo(i->in0_in1_dtype);   /* int8/16/32, uint8/16/32 */
    const Dtype d_data = dtype_hi(i->in0_in1_dtype);   /* any plain ≤0xF        */
    const Dtype d_out  = i->out_dtype;                 /* cast target, ≤0xF     */

    for (size_t e = 0; e < element_count(i->src0_mem_pattern); ++e) {
        int64_t  p = load_int(i->src0_mem_pattern, d_pred, e);   /* predicate element */
        if (p != 0) {                                            /* PREDICATE TRUE  */
            float v   = to_fp32(load(i->src1_mem_pattern, d_data, e)); /* FP32 hub in  */
            store(i->dst_mem_pattern, d_out, e, from_fp32(v, d_out));  /* RNE narrow == plain Cast 0x47 */
        } else {                                                 /* PREDICATE FALSE */
            /* MERGE: dst[e] retains its prior value — no write, lane kept. */
        }
    }
}
```

### 7.1 DVE datapath realisation (engine + name table OBSERVED; the select/merge body MED)

```c
// The integer predicate is compared/loaded into a vbool; the cast result is computed via the
// IVP float/trunc/cvt + pack primitives (the plain-Cast 0x47 leg); the predicated commit goes
// through the bitkillt write-enable guard so killed lanes RETAIN the destination (MERGE).
//
// Symbols (DVE config DLL primitives): selnx16t / dselnx16t (predicated vector select),
//          module__xdref_bitkillt_<w>_<n> (the write-enable / kill mask), ivp_float / ivp_trunc /
//          ivp_cvt / ivp_pack (the FP32 convert + narrow).
vboolN pr = load_predicate_to_vbool(src0, d_pred);     // per-lane integer mask -> vbool
vecN   cv = pack(cvt_via_fp32(load(src1, d_data)), d_out);   // FP32-hub cast result
//
// bitkillt raises an ALL-ONES write-mask in the KILLED (predicate-false) lanes so the destination
// is held (MERGE), and ALL-ZEROS in the live lanes so the cast result is committed:
dst = selnx16t(/*killed=*/dst, /*live=*/cv, bitkillt(pr));   // "MERGE, NOT ZERO"
```

> **GOTCHA — the false lanes MERGE, they do not zero.** This is the defining behavior and the
> most error-prone to get backwards. The `bitkillt` guard's polarity writes an **all-ones** mask
> into the *killed* (predicate-false) lanes, which on the DVE select retains the prior `dst`
> contents. The op is **not** a fill (no constant on false lanes), **not** skip-with-undefined
> (the lane keeps a defined value), and **not** a bitmask generator (it writes *values*, the cast
> result, not a 0/1 mask). `[merge polarity HIGH/OBSERVED from the predicate ISS model; that THIS
> op consumes that exact path MED/INFERRED from its DVE-cluster co-residence — see the
> [predicate ISS page](../../iss/cas-predicate-boolean.md).]`

---

## 8. Per-gen presence (SUNDA → MAVERICK)

| gen | `common.h` `0x99 // Y` | `s3s3d3_tt` CastPredicated validator | DVE self-name `"S: CastPredicated"` |
|---|---|---|---|
| **SUNDA** (v2)    | YES (`:235`) | YES (`s3s3d3_tt.h:94,101,167,235,240`) | absent — SUNDA ships only a DVE **RELEASE** blob; RELEASE strips self-name strings |
| **CAYMAN** (v3)   | YES (`:232`) | YES (`s3s3d3_tt.h:93-112,142-145,191-198`) | **present** `@0x18dc33` (DVE DEBUG_DRAM) |
| **MARIANA** (v4)  | YES (`:237`) | YES | **present** `@0x427f13` (DVE DEBUG_DRAM) |
| **MAVERICK** (v5) | YES (`:240`) | YES | **present** `@0x8affe3` (DVE DEBUG_DRAM) |

`MARIANA_PLUS` is a MARIANA firmware respin — it has DVE blobs including CastPredicated
`@0x6efc33`, but **no** separate arch_isa header (only `sunda`/`cayman`/`mariana`/`maverick`
`neuron_*_arch_isa` directories exist).

`CastPredicated` is present in **all four** ISA generations — opcode + validator OBSERVED
byte-identical in every header, **already at SUNDA**. Contrast `RangeSelect`, which is CAYMAN+
only with no SUNDA entry; `CastPredicated` predates it.

> **NOTE — SUNDA's missing device string is a build-flavor artifact, not kernel absence.** SUNDA
> ships `SUNDA_NX_DVE_RELEASE_*` (no DEBUG variant), and RELEASE blobs of *every* gen likewise
> lack the `"S:"` self-name tags (the tags are a DEBUG-build feature). SUNDA presence therefore
> rests on the ISA header (opcode + full validator, OBSERVED), not the string.
> `[opcode + validator HIGH/OBSERVED all four gens; SUNDA string-absence MED/INFERRED.]`

> **v5/MAVERICK caution.** The MAVERICK opcode line and validator are **header-OBSERVED**; the
> MAVERICK DVE DEBUG blob self-name `@0x8affe3` is OBSERVED in the binary, but the v5 device
> *interior* (the per-gen DVE funcVA/body) is **not** byte-disassembled here and is treated as
> INFERRED where it touches v5-specific behavior.

---

## 9. Relationships & the predicated-op family

`CastPredicated` is one member of a tight DVE predicate/select cluster. The exact
**opcode ↔ name** mapping observed this pass (host ISA enum name vs device firmware self-name):

| opcode | host ISA enum (`common.h`) | device self-name (`libnrtucode_internal.so`) | operand struct | this kernel? |
|---|---|---|---|---|
| `0x72` | `COPY_PREDICATED` | `S: CopyPredicated` (×4) | `S3S3D3_TT` | sibling (dtype-preserving) |
| `0x99` | `CAST_PREDICATED` | `S: CastPredicated` (×4) | `S3S3D3_TT` | **THIS** |
| `0xe8` | `COPY_PREDICATED_SCALAR` | `S: CopyPredicatedScalar` (×4) | scalar-src struct | sibling (scalar predicate-copy) |
| `0xea` | `SELECT_REDUCE` | `S: CopyPredicatedReduce` (×4) | `S2S2D2_STT` | paired (predicate + reduce) |

> **CORRECTION — the `0xea` name split, reconciled.** A prior pass cross-flagged `0xea` as
> `CopyPredicatedReduce` (from the device self-name) and, separately, as the 4th member of
> `S2S2D2_STT` named `SELECT_REDUCE` (from `scalar-tensor-tensor.md` / `tensor-tensor-scan.md`).
> Both are correct and they are the **same opcode**: the *host ISA opcode enumerator* is
> `NEURON_ISA_TPB_OPCODE_SELECT_REDUCE = 0xea` (no `COPY_PREDICATED_REDUCE` enumerator exists in
> any header), and `instruction_mapping.json` binds it to `NEURON_ISA_TPB_S2S2D2_STT_STRUCT`; the
> *device firmware kernel self-name* for that same opcode is `CopyPredicatedReduce` (count 4 in
> the DVE blobs; `"S: SelectReduce"` count **0**, confirmed this pass). So the predicated-op
> family is **not** four members of one struct — `0x72`/`0x99` ride `S3S3D3_TT`, `0xe8` rides a
> scalar struct, and `0xea` rides `S2S2D2_STT`. This corrects SX-FW-61's implication that
> CopyPredicatedReduce "shares the `src0` integer-predicate machinery" *on the same struct* — it
> shares the *engine* (DVE) and the *integer-predicate concept*, but lives on a different operand
> struct. `[HIGH/OBSERVED — opcode enum, `instruction_mapping.json` `jq`, and self-name `rg -c`
> all re-run this pass.]`

**[Cast / Copy](./cast-copy.md) (`0x47` / `0x46`)** — the dtype engine the predicate wraps. The
cast math (FP32-hub convert, RNE) is *identical*; the validator relaxation (`src1 != out` for
cast) is the *same* short-circuit. Difference: plain Cast/Copy run as POOL software kernels /
ACT-native; the predicated pair runs on DVE and adds the `src0` integer predicate + `bitkillt`
merge write. Plain Cast/Copy use a one-source struct; predicated use `S3S3D3_TT` (two sources:
data + mask). `[HIGH]`

**RangeSelect (`0xbc`)** — the nearest DVE predicate-select sibling: same engine, same
blob-multiplicity proof (4 = DVE), same DVE DEBUG blobs (the CastPredicated and RangeSelect
self-names sit ~`0x5a0` apart in every gen's blob). RangeSelect *generates* its predicate
internally (two FP32 bound compares) and can fill+reduce; `CastPredicated` takes its predicate as
an **explicit `src0` integer mask tensor** and casts — no internal compare, no bound immediates.
Both consume the `vbool` via the `bitkillt` merge select. `[HIGH co-resident / OBSERVED]`

**[CopyPredicatedReduce](./copypredicatedreduce.md) (`0xea`, device self-name)** — the paired
task. `"S: CopyPredicatedReduce"` appears ×4 at `0x18e16a / 0x42844a / 0x6f016a / 0x8b0552` — the
**same four** DVE DEBUG blobs as CastPredicated, ~`0x537` below the CastPredicated tag in each
gen. It shares with CastPredicated the **DVE engine**, the **integer-predicate concept**
(`is_valid_int_dtype_datapath`), and the **`bitkillt` `_t` merge consume**. It differs:
CopyPredicatedReduce is dtype-**preserving** (copy, no cast), it **adds a reduction** over the
selected lanes, and it rides **`S2S2D2_STT`** (not `S3S3D3_TT`). CastPredicated *converts* dtype
and does *not* reduce. `[HIGH co-resident / OBSERVED]`

---

## 10. FLIX / literal-pool desync note

No host-side FLIX desync affects the primary facts on this page: the opcode enum, the struct
layout, the validator pseudocode, and the dispatch surface are all sourced from the in-package C
interface headers and from `libnrtucode_internal.so` symbol/string tables
(`nm` / `readelf` / byte scan), none of which depend on Vision-Q7 FLIX bundle decode.

The one item **not** carved here — the per-gen DVE-image `CastPredicated` funcVA and its
instruction-level body (the precise `selnx16t`/`dselnx16t`/`bitkillt` sequence) — would require a
device Xtensa disassembly (`gpsimd_tools/tools/XtensaTools/bin/xtensa-elf-objdump`,
`XTENSA_CORE=ncore2gp`) of the DVE IRAM and is the standard FLIX-desync risk surface. It is left
as MED and was **not** asserted byte-exact. Flagged for a follow-up DVE-IRAM trace if a body-level
decode is required. `[NOTE]`

---

## 11. Evidence index

**Headers** (in-package, `aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64`,
`…/c10/include/`):
* `neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/aws_neuron_isa_tpb_common.h` — `0x99 // Y`
  `CAST_PREDICATED` (sunda:235 / cayman:232 / mariana:237 / maverick:240); `0x72` `COPY_PREDICATED`;
  `is_valid_int_dtype_datapath` 6-int set (cayman:2415); `ALU_OP_BYPASS = 0x00` (cayman:940);
  `SELECT_REDUCE = 0xea` (cayman:300).
* `neuron_cayman_arch_isa/tpb/aws_neuron_isa_tpb_s3s3d3_tt.h` — struct lines 27-39 (64 B
  static-assert); validators lines 93-112 (`is_valid_copy_cast_predicated`), 129-131
  (`s3s3d3_tt_is_zero_op`), 142-145 (`has_copy_cast_predicated_opcode`), 191-198 (the predicate +
  cast-relaxation gates), 208 (`"Cast can change dtype, copy cannot"`).
* `neuron_sunda_arch_isa/tpb/aws_neuron_isa_tpb_s3s3d3_tt.h` — SUNDA validator present (lines
  94, 101, 167, 235, 240).
* `neuron_cayman_arch_isa/tpb/instruction_mapping.json` — `S3S3D3_TT_STRUCT` →
  `[…, COPY_PREDICATED, CAST_PREDICATED, …]`; `S2S2D2_STT_STRUCT` → `[…, SELECT_REDUCE]`.

**Compile-verify** (this pass): `S3S3D3_TT` struct against the CAYMAN tpb header →
`sizeof = 64` + all offsets (§3.1).

**Firmware** (in-package): `…/c10/lib/libnrtucode_internal.so`
* `"S: CastPredicated"` ×4 `@ 0x18dc33 / 0x427f13 / 0x6efc33 / 0x8affe3` (DVE DEBUG_DRAM per gen).
* `"S: CopyPredicatedReduce"` ×4 `@ 0x18e16a / 0x42844a / 0x6f016a / 0x8b0552`.
* `"S: RangeSelect"` ×4 ; `"S: CopyPredicated"` / `"S: CopyPredicatedScalar"` /
  `"S: TensorScalarSelect"` ×4 each ; `"S: Cast\n"` / `"S: Copy\n"` ×3 (ACT DEBUG blobs);
  `"S: SelectReduce"` ×0.
* Blob ranges (`nm -S`): `CAYMAN_NX_DVE_DEBUG_DRAM_get.data @0x18b320 + 0x6d60`;
  `CAYMAN_NX_ACT_DEBUG_DRAM_get.data @0x169400 + 0x6260`;
  `CAYMAN_Q7_POOL_PERF_DRAM_get.data @0x20abc0 + 0x13200`.
* CAYMAN POOL span scan → **zero** predicated-kernel names (POOL-absence proof).
* `.rodata` VMA == file offset (`readelf -SW`) — DVE blobs are `.rodata`-resident, no `.data` delta.

**Cross-links:** [Cast / Copy base kernel](./cast-copy.md) · [CopyPredicatedReduce](./copypredicatedreduce.md) ·
[CopyPredicatedScalar](./copypredicatedscalar.md) · [the dtype model](./dtype-model.md) ·
[the predicate ISS page](../../iss/cas-predicate-boolean.md).
