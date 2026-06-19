# Cast and Copy — opcodes `0x47` / `0x46`

`Copy` (opcode **`0x46`**) and `Cast` (opcode **`0x47`**) are the GPSIMD POOL-side
**dtype pass-through** kernels: a single `op = Bypass` walk over **one** 4-D strided tensor
that copies every element from a source `Tensor4d` to a destination `Tensor4d`. They are the
*same* operation — one operand struct, one ALU op value, one inline body family — and differ in
exactly one thing: whether the source and destination element dtypes are **required to be
equal**. `Copy` forces `out_dtype == in_dtype` and is a **bit-accurate element move**; `Cast`
relaxes that one clause and converts the element dtype **through an FP32 intermediate**
(`in → fp32 → out`, numpy-`astype` rounding). When `Cast`'s input and output dtypes happen to
match, it degenerates to the `Copy` move.

On the GPSIMD Vision-Q7 *Cairo* `ncore2gp` engine these are concrete **Q7-POOL software
kernels**: the POOL `kernel_info_table` routes `0x46 → pool_copy` and `0x47 → pool_cast` to two
thin FLIX-scheduled trampoline+body blocks, self-named `"P%i: Copy : num_chans = %0d"` and
`"P%i: Cast : num_chans = %0d"` in the DEBUG firmware. They are the **only two** of the eleven
`S4D4_TR`-struct opcodes that reach the GPSIMD Q7 ucode (see [§7](#7-what-0x460x47-are-not--the-s4d4_tr-family)).

This page documents: the two dispatch chains; the **shared** `NEURON_ISA_TPB_S4D4_TR` operand
struct (compile-verified 64 B, byte-consistent with [stream-transpose](./stream-transpose.md)
and [tensor-reduce](./tensor-reduce.md)); the `Cast` FP32-intermediate conversion model; the
recovered `NEURON_ISA_TPB_DTYPE` ordinals (matching [the dtype model](./dtype-model.md)); the
`Copy` bit-accuracy proof; the `ivp_dsel`/`ivp_float*` convert/move datapath; and per-generation
presence.

Confidence convention on this page: `[HIGH/OBSERVED]` = read directly from byte / header /
compile / native disasm; `[MED/INFERRED]` = reasoned over an OBSERVED fact; `[…/CARRIED]` =
re-used from a sibling report at its stated confidence without re-reading the artifact this
pass. Every count is re-grounded to `nm` / `rg -c` on the shipped binary, never the decompile.

> **NOTE — provenance.** Every primary fact below derives from the shipped customop-lib package
> `aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64`: the in-package arch-isa C interface headers
> `…/c10/include/neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/` (the authoritative
> opcode / operand-struct / enum / validity contract), a struct compile-verify against those
> headers (`gcc -I<tpb>`, this pass), the `ncore2gp` ISA encode table
> `tools/ncore2gp/config/libisa-core.so` read with `nm`, and the native device disassembler
> `gpsimd_tools/tools/XtensaTools/bin/xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) for the
> convert/move datapath. The `kernel_info_table` opcode→funcVA bytes, the FLIX trampoline
> prologues, and the DEBUG self-name strings are carried from the firmware-carve survey (the
> carves are sha256-verified against the firmware manifest). The headers live under `extracted/`
> (gitignored — reach with `fd --no-ignore` or an absolute path).

---

## 1. TL;DR — the verdict in eight facts

1. **`Copy = 0x46`, `Cast = 0x47`**, byte-exact in `aws_neuron_isa_tpb_common.h:175-176`
   (`OPCODE_COPY = 0x46 // Y`, `OPCODE_CAST = 0x47 // Y`), all four gens. `[HIGH/OBSERVED — §2]`
2. **One operand struct: `NEURON_ISA_TPB_S4D4_TR_STRUCT`, 64 B, compile-verified this pass.**
   `instruction_mapping.json` binds **both** `COPY` and `CAST` (plus nine reduce/cumulative/
   reciprocal/stream-transpose/shuffle opcodes — eleven total) to this single struct.
   `[HIGH/OBSERVED — §3]`
3. **Dispatch surface = Q7-POOL software kernel.** The POOL `kernel_info_table` routes
   `0x46 → pool_copy` (funcVA `0x010040c0`) and `0x47 → pool_cast` (funcVA `0x01004160`); both
   are inline FLIX trampoline+body blocks, **byte-near-identical** (they differ only in one
   selector immediate `0x34`/`0x3f` plus `cast`'s extra convert-mode setup). `[opcodes
   HIGH/OBSERVED; funcVA route HIGH/OBSERVED from carve / MED through the FLIX trampoline — §2]`
4. **The copy/cast distinction is one validity clause.** `is_valid_copy` and `is_valid_cast`
   are **identical** except `is_valid_copy` adds `s4d4_tr_same_src_dst_type` (`out_dtype ==
   in_dtype`), which `is_valid_cast` omits. Both pin `op == Bypass(0x00)`, `op_dim`
   zero-subdim, `negated == 0`, `mask_enable == 0`, `reserved1[5] == 0`, equal src/dst element
   count. `[HIGH/OBSERVED — header verbatim, §3]`
5. **`Copy` = dtype-preserving bit-accurate lane MOVE.** Because `out == in` is enforced, the
   element width is identical on both ends and the kernel is a raw `ivp_dsel*` lane move — **no
   convert datapath is touched**. `[HIGH/OBSERVED gate forces it; MED on the exact in-body slot — §5]`
6. **`Cast` = dtype CONVERT through an FP32 hub** (`in → fp32 → out`), numpy-`astype` semantics:
   round-to-nearest-even for float targets, round-toward-zero for int targets. There is **no
   per-pair convert matrix** — FP32 is the universal pivot. `[model HIGH/OBSERVED; RNE/sat
   MED/INFERRED — §6]`
7. **Cast dtype matrix = full any→any over the gated set.** `in_dtype` ∈ the 12 plain dtypes
   (no 64-bit, no FP32R-in); `out_dtype` ∈ those 12 **+ FP32R** (output-only). `Copy` = the same
   12 input dtypes, constrained to the 12 identity pairs only. `[HIGH/OBSERVED — §6.2]`
8. **Per-gen stable.** `0x46`/`0x47` present in CAYMAN / MARIANA(+0x40 build delta) /
   MAVERICK(relative funcVA) / SUNDA (`pool_copy = op70`, `pool_cast = op71`); **absent** from
   the CAYMAN codec sub-image. Struct + opcodes + binding byte-identical across the four arch-isa
   generations. `[HIGH/OBSERVED — §8]`

---

## 2. The two dispatch chains (byte-exact)

### 2.1 SEQ → POOL bridge

`0x46` (`'F'`) and `0x47` (`'G'`) are the POOL/extended-instruction opcodes the iTPB SEQ
front-end routes to the Q7 POOL compute core via the `0xf0` `ExtendedInst` bridge / per-opcode
handler. On a `Copy`/`Cast` the per-core DEBUG trace prints `"P%i: In dispatch, … got opcode
0x%x"` (`= 0x46`/`0x47`), then the kernel logs its self-name. `[HIGH opcode / HIGH self-name —
DEBUG-string anchors `@0x2137` "Copy : num_chans", `@0x2154` "Cast : num_chans".]`

### 2.2 The POOL `kernel_info_table` (byte-exact)

The POOL `kernel_info_table` is a linear array of `{ u8 0; u8 0; u8 spec; u8 opcode; u32_le
funcVA }` records; lookup is a linear scan matching the packed `(spec, opcode)` key, then a
`callx8 funcVA` (`funcVA` is the only `R_XTENSA_RELATIVE` field). The two relevant rows
(CAYMAN, table VMA `0x02000380`, file `0x7400`):

| idx | opcode | spec | funcVA | kernel | self-name |
|---|:-:|:-:|---|---|---|
| 12 | `0x46` (70) | 0 | `0x010040c0` | `pool_copy` | `"P%i: Copy : num_chans = %0d"` |
| 13 | `0x47` (71) | 0 | `0x01004160` | `pool_cast` | `"P%i: Cast : num_chans = %0d"` |

`[HIGH/OBSERVED — `xxd -s 0x7460 -l 16` of the carved CAYMAN POOL EXTISA image; `.text` VMA
`0x01000000` == file off `0x100`.]`

### 2.3 The two trampolines

`pool_copy @0x010040c0` (file `0x41c0`) and `pool_cast @0x01004160` (file `0x4260`) are thin
FLIX-scheduled entry trampolines. Both open with the windowed-ABI prologue
(`entry a1, 32` = `36 41 00`) and are **byte-near-identical**:

```text
pool_copy @0x010040c0:  entry a1,32 ; <F-format FLIX bundle> ; s8i a12,a2,0x34  ; const16 a7,0x2f04 ; … retw.n
pool_cast @0x01004160:  entry a1,32 ; <same FLIX bundle lead> ; s8i a12,a2,0x3f ; const16+s8i (extra convert-mode setup) ; … retw.n
```

Over the first `0xa0` bytes the two kernels differ only in (i) the `s8i` selector immediate
`0x34` (copy) vs `0x3f` (cast); (ii) `cast`'s extra `const16` + `s8i` pair (the
dtype-convert-mode configuration — the 4-byte length delta that makes `cast` longer than
`copy`); (iii) a 4-byte phase shift of the shared tail. Both end in `retw.n`. **The two kernels
are the same body, with `cast` prepending the dtype-convert setup.** `[HIGH that they differ only
by a selector const + cast's extra convert-setup; MED on the exact mid-bundle semantics —
FLIX-literal desync, §10.]`

> **CORRECTION — the "j 0x100ce56" in the cast trampoline is a disassembler artifact.** An
> earlier survey reported the cast trampoline branching to `0x0100ce56`. That address is
> **outside** `.text` (which ends at `0x01006f1e`), so the `j` is a stock-objdump FLIX-desync
> mis-decode, **not** a real branch target. The HIGH facts are the `kernel_info_table` bytes
> (§2.2), the entry prologues, and the byte-level copy-vs-cast delta (§2.3); the mid-bundle
> dispatch arms are MED, reported structurally and never fabricated. `[HIGH/OBSERVED that the
> target is out-of-range; the corrected reading MED.]`

> **NOTE — there is no standalone `pool_copy`/`pool_cast` symbol.** A full `.xt.prop`
> function-start sweep of the carved POOL image (`xtensa-elf-readelf -S`, c++filt-demangled)
> finds **no** `pool_copy` / `pool_cast` section: the `0x010040c0`/`0x01004160` funcVAs land in a
> gap not covered by any function-start record — the compiler inlined these two small kernels as
> trampoline+body blocks. The only copy-family `.xt.prop` function is `pool_extended_inst_copy`
> (the 2-D extended variant, §2.4). The named bodies are pinned by the `kernel_info_table`
> opcode→funcVA route + the SUNDA opcode JSON + the DEBUG self-names — a multi-source pin, not a
> single fragile decode. `[HIGH/OBSERVED — `.xt.prop` sweep.]`

### 2.4 The adjacent 2-D extended copy

`pool_extended_inst_copy` (idx 7, opcode `0xf0` spec 1, funcVA `0x01003380`) **is** a real
`.xt.prop` function (`_Z23pool_extended_inst_copyv`, extent `0x01003380..0x01003474`,
self-named `"P%i: Decode : ExtendedInstCopy"` `@0x193a`). It is the **EXTENDED-INST 2-D** variant
of `Copy` on a different struct — *not* the plain `Copy(0x46)`. Its FLIX body (recovered in
merged-prop code-mode) uses `ivp_dselnx16t` + `ivp_dseln_2x32t` (the dual-select lane MOVE),
confirming that `ivp_dsel*` is the GPSIMD copy/move datapath that the plain `Copy(0x46)`/`Cast(0x47)`
reuse (§5). `[HIGH func-start / HIGH ivp set — OBSERVED.]`

---

## 3. The operand struct — `NEURON_ISA_TPB_S4D4_TR` (64 B, compile-verified)

`COPY` and `CAST` decode from the **`NEURON_ISA_TPB_S4D4_TR_STRUCT`**, the shared 4-D
tensor-reduce/transpose operand struct. Its header docstring lists the eight logical
instructions it implements: *TensorReduce[Arith/Bitvec]*, *TransposeTensorReduce*,
*TensorCumulative*, **Copy** (*"copies data from one tensor to another, with optional reshape"*),
**Cast** (*"casts the data type of a tensor"*), *Reciprocal*, *StreamShuffle*, *StreamTranspose*.
Verbatim from `aws_neuron_isa_tpb_s4d4_tr.h`:

```c
typedef struct NEURON_ISA_TPB_S4D4_TR_STRUCT {
    NEURON_ISA_TPB_HEADER        header;                  // 4    ( 0 -  3)   opcode 0x46 Copy | 0x47 Cast
    NEURON_ISA_TPB_EVENTS        events;                  // 8    ( 4 - 11)   wait/update semaphore sync
    NEURON_ISA_TPB_TENSOR4D      src_mem_pattern;         // 20   (12 - 31)   INPUT  (4-D strided; read-only)
    NEURON_ISA_TPB_DTYPE         in_dtype;                // 1    (32     )   source element dtype  (no FP32R)
    NEURON_ISA_TPB_DTYPE         out_dtype;               // 1    (33     )   dest   element dtype  (FP32R allowed)
    uint8_t                      num_active_channels;     // 1    (34     )   partition count (1..128)
    uint8_t                      negated;                 // 1    (35     )   <== MUST be 0 (has_zero_negated_field)
    NEURON_ISA_TPB_ALU_OP        op;                      // 1    (36     )   <== MUST be Bypass(0x00) (s4d4_tr_op_bypass)
    NEURON_ISA_TPB_TENSOR_SUBDIM op_dim;                  // 1    (37     )   <== MUST be UNUSED(0) or X(2) (is_zero_subdim)
    uint8_t                      mask_enable;             // 1    (38     )   <== MUST be 0 (mask_enable_zero)
    uint8_t                      reserved1[5];            // 5    (39 - 43)   <== all 0 (s4d4_tr_reserved_zero)
    NEURON_ISA_TPB_TENSOR4D      dst_mem_pattern;         // 20   (44 - 63)   OUTPUT (4-D strided; written)
} NEURON_ISA_TPB_S4D4_TR_STRUCT;

ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_S4D4_TR_STRUCT) == 64, "Error: …is NOT 64B.");
```

### 3.1 Compile-verify (this pass)

Built against the shipped CAYMAN tpb header with `gcc -I<cayman tpb dir>`, `offsetof`/`sizeof`:

```
sizeof = 64
header@0 events@4 src@12 in_dtype@32 out_dtype@33 nac@34 negated@35 op@36 op_dim@37 mask_enable@38 reserved1@39 dst@44
sizeof: TENSOR4D=20  DTYPE=1  SUBDIM=1  ALU_OP=1
```

Every offset matches the header annotation exactly; `ISA_STATIC_ASSERT(… == 64)` holds.
This is byte-identical to the S4D4_TR layout documented on [stream-transpose](./stream-transpose.md)
§4 and [tensor-reduce](./tensor-reduce.md) §6 (`negated@35`, `op@36`=ALU_OP, `op_dim@37`,
`mask_enable@38`, `reserved1[5]@39-43`, `dst@44-63`). `[HIGH/OBSERVED — gcc `-Wall` clean, struct
executed this pass; cross-checked against both sibling pages.]`

`NEURON_ISA_TPB_TENSOR4D` (20 B, `common.h:660`) is `{ NEURON_ISA_TPB_ADDR4 start_addr (4);
int16_t step_elem[4] (8); uint16_t num_elem[4] (8) }` — base address (carrying the SBUF
partition offset) plus per-dim `(stride, count)`. `NEURON_ISA_TPB_HEADER` (4 B, `common.h:411`)
is `{ opcode; inst_word_len; debug_cmd; debug_hint }`. `[HIGH/OBSERVED.]`

> **NOTE — `mariana`/`maverick` rename the field type but not the layout.** On the MARIANA and
> MAVERICK headers the `src_mem_pattern`/`dst_mem_pattern` field type is
> `NEURON_ISA_TPB_MEM_PATTERN4D` (a thin wrapper over the same 20-B `TENSOR4D`), a cosmetic
> rename only — the offsets and `sizeof == 64` are unchanged. `[HIGH/OBSERVED — struct diff.]`

### 3.2 Struct → opcode binding

`instruction_mapping.json`'s `struct2opcode` map binds **eleven** opcodes to `S4D4_TR_STRUCT`
(`jq` this pass):

```
TENSOR_REDUCE_ARITH_OP   TRANSPOSE_TENSOR_REDUCE_ARITH_OP   TENSOR_REDUCE_BITVEC_OP
TRANSPOSE_TENSOR_REDUCE_BITVEC_OP   TENSOR_CUMULATIVE_ARITH_OP   TENSOR_CUMULATIVE_BITVEC_OP
COPY   CAST   RECIPROCAL   STREAM_SHUFFLE   STREAM_TRANSPOSE
```

So `COPY(0x46)` and `CAST(0x47)` share one wire-format with the Tensor-Reduce / Cumulative /
Reciprocal / Stream families; the decoder sub-selects by opcode through the `is_valid_*`
disjunction. `[HIGH/OBSERVED — `jq` over `neuron_cayman_arch_isa/tpb/instruction_mapping.json`.]`

### 3.3 The copy-vs-cast distinction — one validity clause

The header ships the legality algebra as Rust-syntax comments (the spec both the host ucode
decoder and the device firmware implement). `is_valid_copy` and `is_valid_cast` are **identical
line-for-line** except for one clause. Verbatim (`s4d4_tr.h:238-277`), reduced to the diff:

```rust
fn is_valid_copy(i) -> bool {
       has_valid_neuron_header(i) && has_valid_neuron_events(i)
    && has_copy_opcode(i)                                     // (a) opcode == 0x46
    && s4d4_tr_reserved_zero(i)                               // reserved1[5] all 0
    && mask_enable_zero(i)                                    // mask_enable == 0
    && is_valid_dtype(i.s4d4_tr.in_dtype,  DtypeAllowFP32R::False)   // in  : no FP32R
    && is_valid_dtype(i.s4d4_tr.out_dtype, DtypeAllowFP32R::True)    // out : FP32R allowed
    && is_valid_enum(EnumList::AluOp, i.s4d4_tr.op)
    && is_zero_subdim(i.s4d4_tr.op_dim)                       // op_dim in {UNUSED=0, X=2}
    && has_valid_active_channel_range(i…num_active_channels, POOLING_NUM_CHANNELS)  // 1..128
    && start_addr_active_channels(src…) && start_addr_active_channels(dst…)
    && tensor4d_valid(src, in_dtype,  WriteTensor::False, …)  // src read-only
    && tensor4d_valid(dst, out_dtype, WriteTensor::True,  …)  // dst written
    && s4d4_tr_same_src_dst_count(i)                          // equal element count
    && s4d4_tr_op_bypass(i)                                   // op == Bypass(0x00)
    && s4d4_tr_same_src_dst_type(i)        // <== COPY-ONLY: out_dtype == in_dtype
    && has_zero_negated_field(i)                             // negated == 0
}

fn is_valid_cast(i) -> bool {
    /* … byte-identical to is_valid_copy EXCEPT it OMITS s4d4_tr_same_src_dst_type … */
}
```

The supporting predicates, verbatim:

```rust
fn s4d4_tr_op_bypass(i)        { i.s4d4_tr.op == AluOp::Bypass }              // AluOp::Bypass = 0x00 (common.h:940)
fn s4d4_tr_same_src_dst_type(i){ i.s4d4_tr.out_dtype == i.s4d4_tr.in_dtype } // the ONLY copy-extra clause
fn is_zero_subdim(subdim)      { subdim == TensorSubdim::UNUSED || subdim == TensorSubdim::X }  // 0x00 or 0x02
fn has_valid_active_channel_range(channels, max) { channels <= max && channels >= 1 }           // 1..128
fn has_zero_negated_field(i)   { i.s4d4_tr.negated == 0 }
```

So **`Copy` = `Bypass` + same-dtype** ⇒ a dtype-preserving bit-accurate move; **`Cast` =
`Bypass` + dtype-may-differ** ⇒ convert via FP32 intermediate. `[HIGH/OBSERVED — every predicate
body read verbatim from the CAYMAN `s4d4_tr.h` + `common.h` this pass.]`

> **GOTCHA — the ISA struct allows PSUM, but the GPSIMD engine cannot reach it.** Both
> `tensor4d_valid` calls pass `AllowedInPSUM::True, AllowedInSBUF::True`, so at the *wire-format*
> level `Copy`/`Cast` may name PSUM operands. The GPSIMD (Q7-POOL) engine **physically cannot
> access PSUM** (the NKI engine layer asserts *"GpSimd engine cannot access PSUM"*), so a
> PSUM-resident `Copy`/`Cast` routes to the Vector/Scalar engine, and the **GPSIMD** `pool_copy`/
> `pool_cast` operate **SBUF-only**. The constraint is engine-physical, not a struct field. This
> is the one struct-vs-engine nuance on this page. `[struct-allows-PSUM HIGH/OBSERVED; GPSIMD
> SBUF-only HIGH/OBSERVED from the NKI engine assert.]`

> **NOTE — `Copy`/`Cast` accept any 1..128 active channels, unlike `StreamTranspose`.** Where
> [`StreamTranspose`](./stream-transpose.md) adds `has_multiple_32_channels` (so
> `num_active_channels` in `{32,64,96,128}`), `Copy`/`Cast` use only
> `has_valid_active_channel_range(…, 128)` = `1..128` — any partition count is legal. Do not
> carry StreamTranspose's multiple-of-32 rule onto Copy/Cast. `[HIGH/OBSERVED — both validators
> read this pass.]`

---

## 4. The `NEURON_ISA_TPB_DTYPE` ordinals (the cast matrix index)

`in_dtype` (off 32) and `out_dtype` (off 33) are each a **full byte** of the one-byte packed
`NEURON_ISA_TPB_DTYPE` enum — *not* a 4-bit nibble pair (contrast the predicated sibling
[`CastPredicated`](./castpredicated.md), whose `DTYPE_PAIR` packs src dtypes into nibbles). The
base 16-code enum, read verbatim from `aws_neuron_isa_tpb_common.h:723-738` this pass, matching
[the dtype model](./dtype-model.md) exactly:

| code | name | width | sign | role for Cast/Copy |
|:-:|---|:-:|:-:|---|
| `0x0` | `INVALID`  | — | — | rejected (`dtype_invalid_check`) |
| `0x1` | `UINT64`   | 8 B | u | rejected (plain `is_valid_dtype`, no `AllowU64`) |
| `0x2` | `INT8`     | 1 B | s | in & out |
| `0x3` | `UINT8`    | 1 B | u | in & out |
| `0x4` | `INT16`    | 2 B | s | in & out |
| `0x5` | `UINT16`   | 2 B | u | in & out |
| `0x6` | `BFLOAT16` | 2 B | s | in & out (via FP32 hub) |
| `0x7` | `FP16`     | 2 B | s | in & out (native cvt) |
| `0x8` | `INT32`    | 4 B | s | in & out |
| `0x9` | `UINT32`   | 4 B | u | in & out |
| `0xA` | `FP32`     | 4 B | s | in & out (native cvt) — the convert hub |
| `0xB` | `FP32R`    | 4 B | s | **out-only** (`AllowFP32R::True` on out, `False` on in) |
| `0xC` | `INT64`    | 8 B | s | rejected (plain `is_valid_dtype`, no `AllowI64`) |
| `0xD` | `FP8_EXP3` | 1 B | s | in & out — FP8 E3M4 (via FP32 hub) |
| `0xE` | `FP8_EXP4` | 1 B | s | in & out — FP8 E4M3 (via FP32 hub) |
| `0xF` | `FP8_EXP5` | 1 B | s | in & out — FP8 E5M2 (via FP32 hub) |

`[HIGH/OBSERVED — `rg 'NEURON_ISA_TPB_DTYPE_… = 0x'` over the CAYMAN `common.h` this pass; the
per-gen 16/16/24/30 enumerator counts and the `0x10..0x1F` extension codes are owned by [the
dtype model](./dtype-model.md).]`

> **GOTCHA — `FP8_EXPn` is the EXPONENT width and is *not* monotone with the code.**
> `FP8_EXP3 = E3M4` (`0xD`), `FP8_EXP4 = E4M3` (`0xE`), `FP8_EXP5 = E5M2` (`0xF`). The familiar
> deep-learning `e4m3`/`e5m2` are `FP8_EXP4`/`FP8_EXP5`, **not** `FP8_EXP3`. Read the suffix as
> "exponent-bit count", mantissa = `7 − exp`. `[HIGH/OBSERVED — header comments + the dtype model.]`

> **NOTE — the extended `0x10..0x1F` dtypes never reach `Copy`/`Cast`.** On MAVERICK the
> universal `is_valid_dtype` gate additionally rejects the 4-bit (`FP4_EXP2`, `INT4`) and scale
> (`SFP8_E8..E5`) dtypes (`dtype_4bit_illegal_check`/`dtype_scale_illegal_check`). So a `Cast`
> can never name a sub-byte FP4/INT4/NF4/FP6 input — those are [TensorDequantize](./tensor-dequantize.md)'s
> `DEQUANT_FMT` micro-formats, not a Cast datapath. `Cast` consumes only the 12-code plain set.
> `[HIGH/OBSERVED — the gate bodies; CARRIED from the dtype model for the MAVERICK additions.]`

---

## 5. The `Copy` datapath — dtype-preserving bit-accurate MOVE

`Copy` (`op == Bypass`, `in_dtype == out_dtype`) is a straight strided element move: read the
src 4-D `Tensor4d`, write the dst 4-D `Tensor4d`, **no arithmetic, no dtype conversion**. Because
`s4d4_tr_same_src_dst_type` forces the same dtype on both ends, the element width is identical
and the move is a raw bit copy — **bit-accurate**. This is the structural proof that `Copy` does
not touch the convert datapath: a dtype change is impossible by construction, so no
`ivp_float*`/`ivp_trunc*` is needed; the FLIX schedule that `cast` prepends as its
convert-mode setup (§2.3) is **absent** from `copy`.

```c
// pool_copy : NEURON_ISA_TPB_OPCODE_COPY (0x46), op == NEURON_ISA_TPB_ALU_OP_BYPASS (0x00).
// Contract OBSERVED from is_valid_copy; the in-body lane slot MED under FLIX desync.
// Symbols: ivp_dselnx16t / ivp_dseln_2x32t (dual-select lane MOVE),
//          ivp_lvn_2x16s_i / ivp_lsn_16x256_i (strided vector LOAD / STORE).
void pool_copy(const NEURON_ISA_TPB_S4D4_TR_STRUCT *i)
{
    /* is_valid_copy guarantees: op == Bypass, op_dim zero-subdim, negated == 0,
       mask_enable == 0, reserved1 == 0, src_count == dst_count, AND out_dtype == in_dtype. */
    const unsigned bytes = element_size(i->in_dtype);   /* same on both ends -> raw bit move */
    for (size_t e = 0; e < element_count(i->src_mem_pattern); ++e) {
        vec lane = ivp_lvn(i->src_mem_pattern, e, bytes);   /* strided load  */
        ivp_dsel_move(lane);                                /* lane plumbing -- no convert */
        ivp_lsn(i->dst_mem_pattern, e, lane, bytes);        /* strided store */
    }
    /* NO ivp_float* / ivp_trunc* -- the convert datapath is never entered. */
}
```

**Value primitive (OBSERVED in the body + `pool_extended_inst_copy`):**

* `ivp_dselnx16t` — dual-select 32×int16 lane MOVE, the dominant op (16× in the multi-phase body
  scan). The canonical GPSIMD copy/move op; it also dominates `pool_extended_inst_copy` (§2.4).
* `ivp_dseln_2x32t` — dual-select 16×int32 lane move (the 32-bit-element path). The `dsel` width
  (`2nx8`/`nx16`/`n_2x32`) selects 8/16/32-bit element granularity.
* `ivp_lvn_2x16s_i` / `ivp_lsn_16x256_i` — strided vector LOAD / STORE realizing the `Tensor4d`
  access pattern.

All four are confirmed present in the `ncore2gp` `libisa-core.so` encode table
(`nm … | rg 'Opcode_ivp_(dselnx16t|dseln_2x32t)_Slot'` — `dselnx16t` ×4, `dseln_2x32t`
present this pass). `[HIGH that copy is a `dsel`-move; MED on the exact widths chosen per
element-size — FLIX desync, §10.]`

**SIMD lanes** run along `num_active_channels` (the 128-channel partition axis,
`POOLING_NUM_CHANNELS = 128`); each POOL core (`P%i`) handles its `get_cpu_id()` share of the
channels, and the element stream within a partition runs along the `Tensor4d` iteration space.
There are **no predicate variants** for plain `Copy` (`mask_enable` MUST be 0); the predicated
forms are separate opcodes — `COPY_PREDICATED = 0x72` / `CAST_PREDICATED = 0x99` on the
`S3S3D3_TT` struct (see [CastPredicated](./castpredicated.md)), `COPY_PREDICATED_SCALAR = 0xe8`,
and `INDIRECT_COPY = 0xe7` on `S4D4_IC` — **not** this kernel. `[HIGH/OBSERVED — `common.h`
opcodes + `instruction_mapping.json`.]`

---

## 6. The `Cast` datapath — dtype CONVERT via FP32 intermediate

### 6.1 The conversion model

`Cast` permits `in_dtype != out_dtype` (it omits `s4d4_tr_same_src_dst_type`) and converts
**through a single FP32 hub**: `in → widen-to-fp32 → narrow-to-out`. This is a *general
any→any* convert, not a per-pair lookup — FP32 is the universal pivot. The model is
triple-witnessed:

* **The device convert library is fp16+fp32 only.** The single native float-width converts are
  `fp16 ↔ fp32`; `bf16` and all three `fp8` formats have **no native convert op** and are
  realised *through* FP32 (a negative-control sweep finds zero bf16/fp8 convert primitives). So
  even `bf16 → fp16` is two hops (`bf16 → fp32 → fp16`). `[HIGH/OBSERVED — CARRIED from the
  [dtype model](./dtype-model.md) §2.3, where both witnesses are byte-grounded.]`
* **The functional reference is numpy `astype`.** The NKI simulator reference computes
  `src.astype(dst.dtype)` on dtype mismatch and identity on match — i.e. round-to-nearest-even
  for float targets, round-toward-zero for int targets. `[HIGH/OBSERVED — NKI
  `backends/simulator/copy.py` reference impl.]`
* **The NKI public-API docstring** (`nki.isa.tensor_copy`) states verbatim the two modes:
  *"(1) bit-accurate copy when input and output data types are the same or (2) intermediate FP32
  cast when input and output data types differ"*. `[HIGH/OBSERVED — NKI `isa/copy.py`.]`

```c
// pool_cast : NEURON_ISA_TPB_OPCODE_CAST (0x47), op == NEURON_ISA_TPB_ALU_OP_BYPASS (0x00).
// FP32 is the universal hub: there is NO per-pair (in,out) converter table.
// Symbols: ivp_float16nx16t / ivp_floatn_2x32t / ivp_ufloat* (int->fp),
//          ivp_trunc16nxf16t / ivp_truncn_2xf32t (fp->int), ivp_cvt* (fp narrow/widen),
//          ivp_dselnx16t (the lane plumbing).
void pool_cast(const NEURON_ISA_TPB_S4D4_TR_STRUCT *i)
{
    /* is_valid_cast == is_valid_copy MINUS s4d4_tr_same_src_dst_type => in_dtype may != out_dtype. */
    for (size_t e = 0; e < element_count(i->src_mem_pattern); ++e) {
        vec  in   = ivp_lvn(i->src_mem_pattern, e, element_size(i->in_dtype));
        f32  pivot = widen_to_fp32(in, i->in_dtype);   /* fp16: native cvt; bf16/fp8: unpack+scale into fp32 */
        vec  out  = narrow_from_fp32(pivot, i->out_dtype); /* fp16: native cvt; bf16/fp8: pack/round; int: trunc */
        ivp_lsn(i->dst_mem_pattern, e, out, element_size(i->out_dtype));
    }
    /* when in_dtype == out_dtype, this degenerates to the pool_copy bit-move. */
}
```

### 6.2 The dtype matrix

`Cast` uses the plain (non-`_64`) `is_valid_dtype` gate, so:

* **Input set (12):** `is_valid_dtype(in, AllowFP32R::False)` admits every enum code **except**
  `INVALID(0x0)`, `FP32R(0xB)`, `UINT64(0x1)`, `INT64(0xC)` ⇒
  `{INT8, UINT8, INT16, UINT16, BFLOAT16, FP16, INT32, UINT32, FP32, FP8_EXP3, FP8_EXP4, FP8_EXP5}`.
* **Output set (13):** `is_valid_dtype(out, AllowFP32R::True)` ⇒ the 12 above **+ FP32R(0xB)**
  (output-only — the rounded "FP22" partial-fp32 target the RTL uses).

Any `in → out` from this matrix is legal (`Cast` omits same-type), so the convert matrix is the
full **12 × 13** cross-product: int↔int (narrow/widen), int↔fp, fp↔fp (`fp32 → bf16/fp16`,
`fp16 → fp8`, `bf16 → fp32`), fp8↔fp16/bf16/fp32. **`Copy`** is the same 12-input set constrained
to the 12 identity pairs only. There are **no 64-bit Cast/Copy dtypes** (plain `is_valid_dtype`,
not `is_valid_dtype_64`) — contrast [TensorReduce](./tensor-reduce.md), which uses the `_64` gate
and *can* take INT64/UINT64. `[HIGH/OBSERVED — the validity contract.]`

### 6.3 The convert micro-ops

In addition to the `ivp_dsel*` move (the lane plumbing), the cast body issues the `ncore2gp`
convert family. The full roster, all confirmed present in `libisa-core.so`
(`nm … | rg 'Opcode_ivp_(float16nx16t|floatn_2x32t|ufloat16nx16t|ufloatn_2x32t|trunc16nxf16t|truncn_2xf32t)_Slot'`
this pass):

| micro-op | direction | lanes |
|---|---|---|
| `ivp_float16nx16t`  | INT16 → FP16 | 32×i16 (`float16nx16t` ×6 in the encode table) |
| `ivp_floatn_2x32t`  | INT32 → FP32 | 16×i32 |
| `ivp_ufloat16nx16t` | UINT16 → FP16 | 32×u16 |
| `ivp_ufloatn_2x32t` | UINT32 → FP32 | 16×u32 |
| `ivp_trunc16nxf16t` | FP16 → INT16 | 32×f16 |
| `ivp_truncn_2xf32t` | FP32 → INT32 | 16×f32 |

The narrowing fp pack (`fp32 → fp16/bf16/fp8`) and the widening (`fp8/fp16/bf16 → fp32`) ride the
same `float`/`trunc`/`cvt` family. `[HIGH that these convert ops exist and `ivp_float16nx16t` is
emitted; MED on the EXACT op selected per dtype-pair — the per-pair switch sits in the
FLIX-desynced body, reported structurally.]`

### 6.4 Rounding and saturation

The functional model is numpy `astype`: **round-to-nearest-even** for float targets,
**round-toward-zero (truncate)** for int targets. On-device, the `ncore2gp` `ivp_float*`/
`ivp_trunc*`/`ivp_cvt*` family default is RNE on the soft-float/widen path, and the int-narrowing
path truncates per `astype`. **No explicit round-mode control opcode** was isolated in the
decoded cast bundles — rounding is implicit in the convert intrinsic. Out-of-range narrowing
(e.g. `fp32 → fp8`) saturates to the representable range via the same `ivp_bmin*`/`ivp_bmax*`
clamp idiom the dequant path uses; not separately byte-isolated in the desynced cast body.
`[MED/INFERRED — RNE/truncate from the numpy-`astype` reference + the `ncore2gp` convert default;
saturation from the FP32→fp8 range + the clamp idiom; no explicit round/sat opcode pinned. §10.]`

---

## 7. What `0x46`/`0x47` are NOT — the `S4D4_TR` family

`S4D4_TR` is shared by eleven opcodes (§3.2), but only `Copy`/`Cast` reach the GPSIMD Q7 POOL
ucode:

* **The Tensor-Reduce / Cumulative / Reciprocal / Stream-Shuffle / Stream-Transpose opcodes**
  (`TENSOR_REDUCE_ARITH = 0x42`, `TENSOR_REDUCE_BITVEC = 0x52`, `RECIPROCAL = 0x48`,
  `STREAM_SHUFFLE = 0x6a`, `STREAM_TRANSPOSE = 0x6b`) also bind to `S4D4_TR` but are **absent
  from the Q7 POOL `kernel_info_table`** — they are handled by a separate (host-ACE / HW)
  datapath, not the GPSIMD Q7 software kernel. The **only** `S4D4_TR` instructions reaching the
  GPSIMD POOL kernel are `Copy(0x46)` and `Cast(0x47)`. See [tensor-reduce](./tensor-reduce.md)
  (which corrects an earlier mis-attribution that `0x46`/`0x47` were reduces — they are
  copy/cast) and [stream-transpose](./stream-transpose.md). `[HIGH for Copy/Cast being the only
  S4D4_TR ops in the Q7 table; the HW-datapath home of the others INFERRED-HIGH from their absence.]`
* **The PREDICATED Copy/Cast** (`COPY_PREDICATED = 0x72`, `CAST_PREDICATED = 0x99`) bind to a
  **different** struct (`S3S3D3_TT`, two source tensors + a predicate) and run **hardware-native
  on the DVE engine**, not POOL. They are the mirror of the plain pair, one validator-leg up:
  `CastPredicated` relaxes the `dtype_hi == out_dtype` equality exactly as plain `Cast` relaxes
  plain `Copy`'s. Do not conflate. See [CastPredicated](./castpredicated.md). `[HIGH/OBSERVED —
  `instruction_mapping.json`.]`
* **`ALU_OP_BYPASS = 0x00`** (`common.h:940`) is the op both `Copy` and `Cast` carry — "pass
  through src0", the no-arithmetic identity. The full 60-op `ALU_OP` enum is the same enum, but
  `Copy`/`Cast` pin `op` to `Bypass` via `s4d4_tr_op_bypass`. `[HIGH/OBSERVED.]`

> **CORRECTION (for the Part-5 reconcile) — there are TWO Cast/Copy surfaces, and this page is
> the POOL one.** [CastPredicated](./castpredicated.md) §4 notes the **ACT** engine's native
> `Cast`/`Copy` (device self-names `"S: Cast\n"`/`"S: Copy\n"`, count 3 = ACT family) and
> summarises plain Cast/Copy as "POOL software kernels / ACT-native". Both are true: the plain
> `Cast(0x47)`/`Copy(0x46)` *wire-format* lowers to a Q7-POOL software kernel (`pool_copy`/
> `pool_cast`, this page) **and** to an ACT-engine hardware-native path, selected by the
> `engine` parameter of the one MLIR `tensor_copy` op (§8). This page documents the **POOL Q7
> software kernel** specifically (the `kernel_info_table` funcVAs); the ACT-native variant is the
> hardware Activation-engine cast. The two are not in conflict — they are the same op on
> different engines. `[both surfaces HIGH/OBSERVED — POOL via `kernel_info_table`, ACT via the
> DEBUG self-name count.]`

---

## 8. The compiler → device binding

One MLIR op lowers to both kernels, parametrized by engine (NKI ISA, in-package, authoritative):

```
nki/isa/copy.py        : tensor_copy(dst, src, engine in {vector, scalar, gpsimd, unknown})
                         GpSimd path asserts SBUF-only (no PSUM); two-mode docstring.
mlir_tracer/isa.py     : tensor_copy(...) -> emit_tensor_copy(...) ->
mlir_tracer/isa_emit.py:   _nki_irbuilder.tensor_copy(dst, src, engine)   # ONE MLIR op, engine-parametrized
backends/simulator     : numpy reference -- astype-on-dtype-mismatch (cast), identity-on-match (copy)
```

So the single MLIR `tensor_copy` op lowers, for `engine = GpSimd`, to the GPSIMD POOL
`Copy(0x46)` when `in_dtype == out_dtype` and `Cast(0x47)` when they differ — **the opcode split
is the dtype-equality decision**, made by the compiler at lowering and re-checked by the device
validity predicate (§3.3). `[HIGH/OBSERVED — the chain `tensor_copy → emit → MLIR → opcode`.]`

---

## 9. Per-gen presence

| gen | `Copy(0x46)` funcVA | `Cast(0x47)` funcVA | notes |
|---|---|---|---|
| **CAYMAN** (v3)   | `0x010040c0` | `0x01004160` | canonical (byte-exact `kernel_info_table`) |
| **MARIANA(+)** (v4) | `0x01004100` | `0x010041a0` | same opcodes, `+0x40` build delta |
| **MAVERICK** (v5) | rel `0x000041c0` | rel `0x00004260` | ET_DYN stripped — relative funcVAs |
| **SUNDA** (v2)    | `op70` (`0x46`)  | `op71` (`0x47`)  | opcode JSON (symtab-light, no `.xt.prop` body) |
| CAYMAN codec sub-image | — | — | **absent** — 9-entry codec image has no Copy/Cast |

The `S4D4_TR` struct (64 B), the `COPY=0x46`/`CAST=0x47` opcodes, the `DTYPE` enum, and the
`COPY+CAST → S4D4_TR` `instruction_mapping` binding are **byte-identical** across the
cayman/mariana/maverick/sunda arch-isa headers (verified this pass: same opcodes, same 64-B
static-assert, same binding; mariana/maverick only rename the field type `TENSOR4D →
MEM_PATTERN4D`, same 20-B layout). `[HIGH/OBSERVED — header diff; the per-gen funcVAs carried
from the firmware-carve survey.]`

> **v5/MAVERICK caution.** The MAVERICK opcode line, struct, and binding are **header-OBSERVED**;
> the MAVERICK device funcVAs are OBSERVED in the stripped ET_DYN as relative offsets, but the v5
> device *interior* (the per-gen FLIX body / convert micro-schedule) is **not** byte-disassembled
> here and is treated as INFERRED where it touches v5-specific behavior. The convert/move op
> *set* and the validity contract are stable across gens; whether the MAVERICK/SUNDA micro-
> schedule differs is out of scope (LOW, not claimed).

---

## 10. FLIX / literal-pool desync note

The `pool_copy`/`pool_cast` trampoline+body blocks `@0x010040c0`/`@0x01004160` are hand-scheduled
FLIX VLIW with interleaved literal/selector bytes that desync a stock linear sweep, and they are
**not** covered by any `.xt.prop` function-start record (so even the merged-prop code-mode trick,
which only resyncs at `.xt.prop` starts, does not enter FLIX mode here). The **HIGH/OBSERVED**
anchors are: the `kernel_info_table` opcode→funcVA bytes; the entry prologues; the byte-level
copy-vs-cast delta (the `s8i 0x34`-vs-`0x3f` selector + cast's extra `const16`/`s8i`
convert-setup); and the phase-stable IVP ops surviving a multi-phase binary scan
(`ivp_dselnx16t` move, `ivp_float16nx16t` convert, `ivp_lvn`/`ivp_lsn` load/store). **MED:** the
exact mid-bundle dispatch arms, the exact per-dtype-pair convert-op selection, the exact
rounding/saturation opcodes. The mis-decoded `j 0x100ce56` is a desync artifact (target outside
`.text`), not trusted (§2.3). The **model** — the `S4D4_TR` struct, `Bypass`, the copy/cast
validity delta, the dtype matrix, the FP32-intermediate cast — is HIGH/OBSERVED from the
in-package arch-isa header + `instruction_mapping.json` + the NKI public-API docstring + the
simulator numpy reference: a multi-source agreement, not a fragile single decode. `[NOTE]`

---

## 11. Adversarial self-verification — five strongest claims re-challenged

1. **`Copy=0x46`/`Cast=0x47` dispatch.** `OPCODE_COPY = 0x46`, `OPCODE_CAST = 0x47`
   (`common.h:175-176`) — re-read this pass with `rg`. The `kernel_info_table` idx12/idx13 →
   funcVA `0x010040c0`/`0x01004160` is carried from the firmware carve (sha256-verified vs the
   manifest) — opcodes are OBSERVED here; the funcVA route through the FLIX trampoline is MED. ✓
2. **The shared `S4D4_TR` offsets.** Compile-verified independently this pass
   (`gcc -I<cayman tpb>`): `sizeof = 64`, all 12 offsets exact, and byte-identical to
   stream-transpose.md / tensor-reduce.md (`negated@35`, `op@36`, `op_dim@37`, `mask_enable@38`,
   `reserved1@39`, `dst@44`). ✓ HIGH/OBSERVED.
3. **The FP32-intermediate cast + RNE.** The `is_valid_cast` body (header verbatim) omits
   `s4d4_tr_same_src_dst_type`, so `in != out` is permitted; the FP32-hub fact is byte-grounded
   in the dtype model (native `fp16↔fp32` only). RNE/truncate is INFERRED from the numpy-`astype`
   reference + the `ncore2gp` default — flagged MED. ✓
4. **The `Copy` bit-accuracy proof.** `is_valid_copy` adds `s4d4_tr_same_src_dst_type`
   (`out_dtype == in_dtype`), read verbatim — a dtype change is impossible by construction, so the
   element width is identical and no convert op is needed; the cast-only convert-mode setup is
   absent from `copy`'s trampoline. ✓ HIGH/OBSERVED.
5. **The `DTYPE` ordinals.** Re-read `common.h:723-738` with `rg` this pass: `INVALID 0x0 …
   FP8_EXP5 0xF` — matches [the dtype model](./dtype-model.md) exactly, including the canonical
   `FP8_EXP3/4/5` names. ✓ HIGH/OBSERVED.

All five survive re-challenge.

---

## 12. Cross-references

* [The Unified Datatype Model](./dtype-model.md) — the `NEURON_ISA_TPB_DTYPE` enum (§4), the
  FP32 convert hub, and the legality gates that bound the Cast matrix.
* [CastPredicated](./castpredicated.md) — the predicated sibling (`0x99`/`0x72` on `S3S3D3_TT`,
  DVE-native); the same FP32-hub cast math wrapped in an integer predicate + merge write.
* [StreamTranspose](./stream-transpose.md) — another `S4D4_TR` consumer; cross-check the 64-B
  struct layout (agrees) and note its multiple-of-32 channel rule that Copy/Cast do **not** share.
* [Tensor-Reduce family](./tensor-reduce.md) — the `S4D4_TR` reduce opcodes (`0x42`/`0x52`) that
  share the struct but use the `_64` dtype gate and a separate datapath; corrects the
  `0x46`/`0x47`-as-reduce mis-attribution.
* [TensorDequantize](./tensor-dequantize.md) — where the sub-byte FP4/INT4/NF4/FP6 micro-formats
  (`DEQUANT_FMT`) live; Cast does **not** consume them.
* [ISA Batch 13 — fp32 Convert (sp_cvt)](../../isa/ref/b13-sp-cvt.md) and
  [ISA Batch 20 — fp16 Convert (hp_cvt)](../../isa/ref/b20-hp-cvt.md) — the `ivp_float*`/
  `ivp_trunc*`/`ivp_cvt*` convert/pack ISA encodings the FP32-hub path draws on.
* [Convert / Pack / FP Semantics ISS](../../iss/cas-convert-pack-fp.md) — *planned*: the
  instruction-set-simulator round/saturation semantics of the convert family.
