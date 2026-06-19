# NonzeroWithCount

This page decodes the **NonzeroWithCount** kernel of the NeuronCore GPSIMD ISA — the
**POOL-codec** *index-compaction-with-count* handler (opcode **`0xf2`**) that scans one
3-D source tensor, detects its nonzero elements with a compare-vs-zero `vbool` mask,
**compacts the nonzero element positions** into one 3-D destination, and writes the
**count of nonzeros** into the destination's one extra (`+1`) element. It pins the opcode
(`0xf2`), the 64-byte `S3D3_NONZERO_WITH_COUNT` operand struct byte-for-byte from the
shipped ISA header (compile-verified `sizeof == 64` on all three supporting generations),
the detect→compact→count→pad datapath recovered from the carved POOL firmware, the
**`int<int>`-vs-`float<float>` C++-template split** (proven byte-exact: the two firmware
bodies differ in only 18 of 562 bytes), the input-keyed dtype gate
(`{FP32, INT32} → INT32`), and the `kernel_info_table → trampoline → dtype-branch → body`
dispatch chain.

NonzeroWithCount runs on the **Cadence Tensilica Vision-Q7 NX "Cairo" 512-bit FLIX/VLIW
DSP** (`ncore2gp` config, one per NeuronCore) — specifically on the **POOL** (Pooling)
sequencer, in *both* the engine's MAIN image and its POOL-codec sub-image. Its
self-name strings (`"P%i: NonzeroWithCount : num_chans = %0d"` at decode,
`"S: NonzeroWithCount: num_elements=%d"` in the body) are ASCII-baked in the
CAYMAN/MARIANA/MARIANA_PLUS POOL DEBUG DRAM and are themselves binary evidence. This places
it as the POOL **codec-family** neighbor of [GetSequenceBounds](./get-sequence-bounds.md)
(the `0xbe` min/max-bounds op that immediately precedes it in the `.xt.prop` section
order), a sibling of the POOL value+index [Sort](./sort.md) (`0x96`), and the POOL-engine
analog of the DVE-engine [SparsityCompress](./sparsity-compress-tag.md) (`0xE0`/`0xE1`).
All four compose from the same compare→`vbool` + lane-permute/select ISA primitives — the
[predicate/boolean (vbool) ISS primitives](../../iss/cas-predicate-boolean.md) and the
[SuperGather scatter/gather ISA batch](../../isa/ref/b19-scatter-gather.md).

Confidence and evidence tags follow the project
[Confidence & Walls Model](../../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. Every host-ISA fact is read out of the public
`aws_neuron_isa_tpb_*.h` headers shipped in the customop-lib package and was
re-compile-verified this session; every device fact is byte-pinned to a carve from
`libnrtucode_internal.so`.

> **NOTE — what was carved this session, and the exact objects used.** The firmware
> container is `…/custom_op/c10/lib/libnrtucode_internal.so`
> (`sha256 b7c67e898a116454…fc329b`, ELF64 x86-64 DYN — the FW-60/73 anchor, re-verified
> in-task). The POOL images are 17 EM_XTENSA ELF blobs embedded in `.rodata`
> (VMA `0x01000000` == file offset `0x100`, i.e. `file_off = VMA − 0x01000000 + 0x100`)
> and are disassembled with the native `xtensa-elf-objdump`
> (`XTENSA_CORE=ncore2gp`, GNU Binutils 2.34 / Xtensa Tools 14.09) that ships inside the
> gpsimd-tools package. The recovered `P%i:`/`S:` self-name strings, the
> `s3d3_nonzero_with_count.h` / `common.h` ISA structs, the `instruction_mapping.json`
> binding, and the demangled `.xt.prop` template symbols are all themselves binary evidence
> and are cited as such.
>
> | object | internal.so off / size | sha256 | note |
> |---|---|---|---|
> | `CAYMAN_0` (EXTISA_0 MAIN) | `0x2ef7e0` / `0xa260` | `910d41c3…` | `kernel_info_table` idx15 `0xf2`; bodies `0x01004940`/`0x01004b80` |
> | `CAYMAN_3` (EXTISA_3 CODEC) | `0x2fbf00` / `0x6974` | `052ac31c…` | codec table idx5 `0xf2`; bodies `0x010014e0`/`0x01001720` |
> | `GEN2_main` (MARIANA EXTISA_0) | `0x5893c0` / `0xa280` | `39416fbf…` | `0xf2 → 0x01004890` (== GEN3) |
> | `GEN3_main` (MARIANA_PLUS EXTISA_0) | `0x855240` / `0xa280` | `39416fbf…` | byte-identical body |
> | `MAVERICK_0/_3` (EXTISA, ET_DYN) | `0x994de0`/`0x99ece0` | (stripped) | header-grounded; body not byte-confirmed (§7) |
>
> The two `.xt.prop` template symbols and both compute-bundle byte signatures reproduce
> against the binary in-task: `_Z23nonzero_with_count_implIfEvjiijj` /
> `_Z23nonzero_with_count_implIiEvjiijj` each occur **6×** (one per image:
> CAYMAN_0/3 + MARIANA main/codec + MARIANA_PLUS main/codec); the `float` compute-bundle
> signature `2f 35 f0 65` and the `int` signature `2f 35 d6 61` each occur **12×**, with the
> `int` instance consistently ~`0x240` B after its `float` partner — the body pair layout.
> `[HIGH/OBSERVED]`

---

## 1. The headline

NonzeroWithCount is a **first-class, named ISA instruction with a single architectural
opcode** — *not* a composed host helper, and *not* (as an early draft conflated it) the
batch-norm `S2_BN` op. Nine facts pin it:

1. **Opcode = `0xf2`, struct = `S3D3_NONZERO_WITH_COUNT` (64 B).** From `common.h:303`
   (`NEURON_ISA_TPB_OPCODE_NONZERO_WITH_COUNT = 0xf2, // Y`), the
   `instruction_mapping.json` `struct2opcode` binding
   `NEURON_ISA_TPB_S3D3_NONZERO_WITH_COUNT_STRUCT → [NEURON_ISA_TPB_OPCODE_NONZERO_WITH_COUNT]`
   (`instruction_mapping.json:256-258`), and `debug_assert.h:402-403`
   (`case NEURON_ISA_TPB_OPCODE_NONZERO_WITH_COUNT: check = dbg_is_valid_nonzero_with_count(i, nc);`).
   The struct compile-verifies `sizeof == 64` on cayman/mariana/maverick. `[HIGH/OBSERVED]`

2. **It emits the nonzero POSITIONS + a COUNT — not the nonzero values.** The kernel
   scans one src `Tensor3d`, detects nonzero elements (compare-vs-zero → `vbool`),
   compacts each nonzero element's **flat position** (plus an `index_offset`) into the dst
   `Tensor3d`, fills the remaining dst slots with `padding_val`, and writes the total
   nonzero **count** into the dst's one extra element. The header is verbatim:
   *"only INT32 or FP32 input and INT32 output are supported currently / We need
   dst_mem_pattern to 1 more element than src_mem_pattern, which indicates the nonzero
   count."* (`s3d3_nonzero_with_count.h:93-95`). The output is `[indices…, padding…,
   count]`, all INT32 — the values are the *detection input*, never the dst payload.
   `[HIGH/OBSERVED]`

3. **The dtype gate is the narrowest possible: `in ∈ {FP32(0xA), INT32(0x8)}`, `out == INT32(0x8)`.**
   `has_valid_nonzero_with_count_dtype` (header, all three gens identical) admits exactly
   two input dtypes and exactly one output dtype; every other dtype is rejected at decode.
   The *input* dtype selects the C++-template leg; the *output* is always INT32 (integer
   indices + an integer count). `[HIGH/OBSERVED]`

4. **The `+1` element-count rule is a structural invariant.**
   `s3d3_nonzero_with_count_element_count_check_t3d` requires
   `t3d_element_count(src) + 1 == t3d_element_count(dst)`: the dst is *exactly one element
   larger* than the src, and that one element holds the count. `[HIGH/OBSERVED]`

5. **It is a `int<int>` / `float<float>` C++ TEMPLATE instantiated once per input dtype.**
   The two `.xt.prop` symbols `_Z23nonzero_with_count_implIfEvjiijj` /
   `_Z23nonzero_with_count_implIiEvjiijj` demangle to
   `nonzero_with_count_impl<float>(uint,int,int,uint,uint)` /
   `nonzero_with_count_impl<int>(…)`. The `If E` / `Ii E` mangling fragment **is** the
   template parameter `T` — the dtype split is a *compile-time* instantiation, not a runtime
   `DTYPE` argument (contrast its neighbor [GetSequenceBounds](./get-sequence-bounds.md),
   which carries the dtype as a by-value arg). `[HIGH/OBSERVED]`

6. **The template split is byte-exact: the two 562-byte bodies differ in only 18 of 562 bytes.**
   All 18 differing bytes lie inside the compare/value-load/max-select compute bundle; the
   control structure, the compaction (`sel`/`dsel`), the predicated index/count accumulate
   (`addn_2x32t`), and the store path are byte-IDENTICAL — because the output is always
   INT32. The differing bytes flip the IVP opcode selector
   (`float: ivp_neqn_2x32 + ivp_maxunx16t` vs `int: ivp_lt2nx8 + ivp_maxnx16t`).
   `[HIGH/OBSERVED]`

7. **Dispatch is `kernel_info_table` `0xf2 → trampoline → dtype-branch → callx8` body.** In
   the CAYMAN MAIN image it is `idx15 {0xf2 → 0x0100484c}`; in the CAYMAN codec image
   `idx5 {0xf2 → 0x010013f4}`. The trampoline reads the `in_dtype` byte (struct off 13) and
   branches into one of the two precompiled template bodies. **Present in BOTH the MAIN and
   the codec image** — a first-class POOL instruction, not codec-only. `[HIGH/OBSERVED]`

8. **NC-v3+ only.** `has_valid_nc_nonzero_with_count` requires `nc >= V3`. Present on
   CAYMAN (NC-v3), MARIANA (NC-v4), MAVERICK (NC-v5); **absent** on SUNDA (NC-v2) and TONGA
   (no opcode, no struct header, no firmware body). The struct/opcode/gate/`+1`-rule/body are
   byte-identical CAYMAN→MARIANA→MARIANA_PLUS. `[HIGH/OBSERVED]`

9. **There is no dedicated "nonzero"/"popcount" ISA op — NonzeroWithCount is COMPOSED.** The
   detect is a generic compare→`vbool`; the compaction is generic `sel`/`dsel`; the count is
   the terminal value of a predicated write-cursor (no `popcount`, no `radd` in the body).
   `[detect/compact/accumulate ops HIGH/OBSERVED; count-as-cursor INFERRED-HIGH, §3.3]`

> **CORRECTION — `0xf2` is NonzeroWithCount, NOT GetSequenceBounds and NOT TensorDequantize;
> and NOT the `S2_BN` batch-norm struct.** Two prior misreadings are overturned here.
> **(a)** An earlier draft of this kernel page conflated `0xf2` with the batch-norm `S2_BN`
> struct. That is wrong: `instruction_mapping.json:256` binds `0xf2` to the **distinct**
> `NEURON_ISA_TPB_S3D3_NONZERO_WITH_COUNT_STRUCT` (one 3-D src + one 3-D dst + two INT32
> immediates), a different size/shape from `S2_BN`. **(b)** The committed
> [POOL dispatch](../pool/pool-dispatch.md) §6 map labels the `idx15 0xf2` row
> *"`get_sequence_bounds` / `tensor_dequantize` entry"*. That label is **off-by-neighbor**:
> in `common.h`, `GET_SEQUENCE_BOUNDS = 0xbe` (= `idx14`) and `TENSOR_DEQUANTIZE = 0x7b`
> (= `idx16`); `0xf2` itself is `NONZERO_WITH_COUNT`. The `0xf2` trampoline `0x0100484c`
> *is* the NonzeroWithCount entry — its `0x0200047c` `.bss` state pointer and its
> dtype-branch route to `nonzero_with_count_impl<float>`/`<int>`, with GetSequenceBounds
> and TensorDequantize being the *adjacent* `idx14`/`idx16` rows, not this one. The
> pool-dispatch row's funcVA/opcode/spec bytes are correct; only the resolved *name* on
> that row needs the `0xf2 = NonzeroWithCount` fix. `[HIGH/OBSERVED — the three opcode
> enumerators + the instruction_mapping binding read verbatim]`

---

## 2. Opcode, struct binding, and engine placement

**Opcode.** Read verbatim from `aws_neuron_isa_tpb_common.h` (cayman line 303;
byte-identical in mariana/maverick), shown beside its two POOL-codec neighbors to make the
disambiguation concrete:

```c
NEURON_ISA_TPB_OPCODE_TENSOR_DEQUANTIZE    = 0x7b,    // Y   (= pool-dispatch idx16)
NEURON_ISA_TPB_OPCODE_GET_SEQUENCE_BOUNDS  = 0xbe,    // Y   (= pool-dispatch idx14)
NEURON_ISA_TPB_OPCODE_NONZERO_WITH_COUNT   = 0xf2,    // Y   (= pool-dispatch idx15)  <== THIS op
```

The `// Y` annotation marks `0xf2` a **maintained, wired** op. `[HIGH/OBSERVED]`

**Struct binding.** The shipped `instruction_mapping.json` `struct2opcode` table binds the
operand struct to the opcode exactly once (`:256-258`):

```json
"NEURON_ISA_TPB_S3D3_NONZERO_WITH_COUNT_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_NONZERO_WITH_COUNT"
]
```

and `debug_assert.h:402-403` routes decode-time validation:

```c
case NEURON_ISA_TPB_OPCODE_NONZERO_WITH_COUNT:
    check = dbg_is_valid_nonzero_with_count(i, nc);
    break;
```

`[HIGH/OBSERVED]`

**Engine = POOL.** Three independent signals converge. (a) The kernel is reached through
the **POOL `kernel_info_table`** linear-scan dispatcher (see
[POOL dispatch](../pool/pool-dispatch.md)), both at `idx15 0xf2 → 0x0100484c` (MAIN image)
and at `idx5 0xf2 → 0x010013f4` (codec image). (b) The two self-name strings carry the
POOL `'P%i:'` per-core prefix (`"P%i: NonzeroWithCount : num_chans = %0d"`) and the SEQ
front-end `'S:'` prefix on the body count log (`"S: NonzeroWithCount: num_elements=%d"`),
both ASCII-baked in the POOL DEBUG DRAM. (c) The `.xt.prop` template symbols sit in the
POOL-codec image's codec-family section run. The structural placement contrasts with
[SparsityCompress](./sparsity-compress-tag.md), which lives on the **DVE** engine
(`engine_idx = 3`). `[HIGH/OBSERVED]`

---

## 3. The operand struct — `S3D3_NONZERO_WITH_COUNT_STRUCT` (64 B, compile-verified)

`S3D3` names the shape: **one 3-D source**, **one 3-D destination** — the same descriptor
shape as its neighbor `S3D3_SEQ_BOUNDS`, but with **two INT32 immediates** (`index_offset`,
`padding_val`) that GetSequenceBounds lacks and that make NonzeroWithCount a
compaction-with-offset/padding op. The struct from
`aws_neuron_isa_tpb_s3d3_nonzero_with_count.h` (cayman; mariana/maverick differ only in the
NC-version banner comment):

```c
typedef struct NEURON_ISA_TPB_S3D3_NONZERO_WITH_COUNT_STRUCT {
    NEURON_ISA_TPB_HEADER             header;               //  4   ( 0 -  3)  { opcode=0xf2, … }
    NEURON_ISA_TPB_EVENTS             events;               //  8   ( 4 - 11)  wait/update semaphores
    uint8_t                           num_active_channels;  //  1   (12     )  check_active_channels 1..128
    NEURON_ISA_TPB_DTYPE              in_dtype;             //  1   (13     )  MUST ∈ {INT32(0x8), FP32(0xA)}
    NEURON_ISA_TPB_DTYPE              out_dtype;            //  1   (14     )  MUST == INT32(0x8)
    NEURON_ISA_TPB_IMM_SRC            index_offset_src;     //  1   (15     )  IMM source for index_offset
    NEURON_ISA_TPB_TENSOR3D           src_mem_pattern;      // 16   (16 - 31)  INPUT seq (read-only, SBUF)
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD index_offset;         //  4   (32 - 35)  INT32 added to each emitted index
    NEURON_ISA_TPB_IMM_SRC            padding_val_src;      //  1   (36     )  IMM source for padding_val
    uint8_t                           reserved0[3];         //  3   (37 - 39)  MUST be 0
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD padding_val;          //  4   (40 - 43)  INT32 fills unused dst slots
    NEURON_ISA_TPB_TENSOR3D           dst_mem_pattern;      // 16   (44 - 59)  OUTPUT indices+count (write, SBUF)
    uint8_t                           reserved1[4];         //  4   (60 - 63)  ALL 4 MUST be 0
} NEURON_ISA_TPB_S3D3_NONZERO_WITH_COUNT_STRUCT;

ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_S3D3_NONZERO_WITH_COUNT_STRUCT) == 64,
                  "Error: NEURON_ISA_TPB_S3D3_NONZERO_WITH_COUNT_STRUCT is NOT 64B.");
```

**Compile-verify output (this session, gcc `-std=c11`, all three supporting gens identical):**

```
sizeof=64
header=0 events=4 num_active=12 in_dtype=13 out_dtype=14 index_offset_src=15
src=16 index_offset=32 padding_val_src=36 reserved0=37 padding_val=40 dst=44 reserved1=60
sizeof TENSOR3D=16 DTYPE=1 IMM_SRC=1 IMM_VAL=4 HEADER=4 EVENTS=8
OPCODE_NONZERO=0xf2 INT32=0x8 FP32=0xa FP32R=0xb UINT32=0x9
```

`[HIGH/OBSERVED — ISA_STATIC_ASSERT(==64) passes; offsetof/sizeof reproduced for cayman,
mariana, maverick — bit-identical]`

### 3.1 Field census

| off | size | field | type | role |
|---|---|---|---|---|
| 0 | 4 | `header` | `HEADER` | `{ opcode = 0x96→0xf2, inst_word_len, … }` |
| 4 | 8 | `events` | `EVENTS` | wait/update semaphore sync |
| 12 | 1 | `num_active_channels` | `uint8_t` | `1..128`; **multi-channel** (each channel scans its own row) |
| 13 | 1 | `in_dtype` | `DTYPE` | `INT32(0x8)` \| `FP32(0xA)` — **selects the template leg** |
| 14 | 1 | `out_dtype` | `DTYPE` | `INT32(0x8)` only |
| 15 | 1 | `index_offset_src` | `IMM_SRC` | how `index_offset` is delivered (inline / ptr / reg-ptr) |
| 16 | 16 | `src_mem_pattern` | `TENSOR3D` | INPUT 3-D seq (read-only, SBUF; never PSUM) |
| 32 | 4 | `index_offset` | `IMM_VAL` | INT32 added to each emitted index |
| 36 | 1 | `padding_val_src` | `IMM_SRC` | how `padding_val` is delivered |
| 37 | 3 | `reserved0[3]` | `uint8_t[]` | MUST be 0 |
| 40 | 4 | `padding_val` | `IMM_VAL` | INT32 filling unused dst slots |
| 44 | 16 | `dst_mem_pattern` | `TENSOR3D` | OUTPUT indices+count (write, SBUF; never PSUM) |
| 60 | 4 | `reserved1[4]` | `uint8_t[]` | ALL 4 MUST be 0 |

> **GOTCHA — the dtypes at off 13/14 are FULL BYTES, not a packed nibble pair.** Contrast
> [Sort](./sort.md), whose `in_out_dtype` (off 14) is a packed 4+4 `DTYPE_PAIR` bitfield.
> NonzeroWithCount carries `in_dtype` and `out_dtype` as two **separate** `DTYPE` bytes at
> offsets 13 and 14. A reimplementation that reuses the Sort `DTYPE_PAIR` decode here will
> misread the gate. `[HIGH/OBSERVED]`

### 3.2 The two INT32 immediates — `IMM_VAL_INST_FIELD` + `IMM_SRC`

Both `index_offset` (off 32) and `padding_val` (off 40) are `NEURON_ISA_TPB_IMM_VAL_INST_FIELD`
(a 4-byte union, `common.h:866`):

```c
typedef union NEURON_ISA_TPB_IMM_VAL_INST_FIELD {  // sizeof == 4
    NEURON_ISA_TPB_PARTITION_OFFSET imm_ptr;        // PartitionOffset (pointer immediate)
    NEURON_ISA_TPB_IMM_REG          imm_reg;        // register-number immediate
    float                           imm_arith_fp32;
    int32_t                         imm_bitvec_int32;   // <== how index_offset / padding_val are read
    uint32_t                        imm_bitvec_uint32;
    uint16_t                        imm_bitvec_uint16[2];
    uint8_t                         imm_bitvec_uint8[4];
} NEURON_ISA_TPB_IMM_VAL_INST_FIELD;
```

Each is paired with an `IMM_SRC` byte (`index_offset_src` @15, `padding_val_src` @36)
selecting the delivery mode (`common.h:1208-1210`):

```c
NEURON_ISA_TPB_IMM_SRC_INSTRUCTION_IMMEDIATE = 0,    // INT32 baked in the instruction word
NEURON_ISA_TPB_IMM_SRC_POINTER_IMMEDIATE     = 1,    // PartitionOffset pointer to the INT32
NEURON_ISA_TPB_IMM_SRC_REG_PTR_IMMEDIATE     = 2,    // pointer to the INT32 from a register
```

The validity predicate validates **both** immediates against `Dtype::INT32` (§5) — they are
INT32 values regardless of `in_dtype`. `index_offset` globalizes each emitted index (so a
tiled scan carries correct global positions); `padding_val` fills the dst slots between the
last emitted index and the count slot. `[HIGH/OBSERVED — compile-verified union + the two
IMM_SRC bytes + the has_valid_immediates `Dtype::INT32` arg]`

> **NOTE — the descriptor SHAPE matches GetSequenceBounds, the immediates do not.**
> NonzeroWithCount and [GetSequenceBounds](./get-sequence-bounds.md) are both `S3D3` (one
> 3-D src, one 3-D dst) and sit adjacent in the codec image. But GetSequenceBounds has *no*
> `index_offset`/`padding_val` immediates and is **single-channel** with `dst == 2×src`
> (the `{min, max}` pair); NonzeroWithCount is **multi-channel** (`1..128`) with
> `dst == src + 1` (the count). The two INT32 immediates at off 32/40 are precisely what
> distinguishes a *compaction-with-offset/padding* op from a *bounds* op. `[HIGH/OBSERVED]`

---

## 4. The algorithm — detect → compact → count → pad

The firmware body is a **small (562-byte) per-block inner kernel** called in a loop over
the src stream (the `arg0` element count). It implements classic stream-compaction-with-count
entirely from the standard GPSIMD `vbool`/select primitives — there is **no** dedicated
`nonzero` or `popcount` ISA op; the op is COMPOSED, exactly as
[SparsityCompress](./sparsity-compress-tag.md) is. The primitive census below is from the
per-bundle FLIX disasm of both bodies (CAYMAN codec image), cross-validated by the
`float`-vs-`int` byte-diff (§6).

### 4.1 The compute pipeline (annotated C pseudocode)

```c
// NonzeroWithCount — POOL-codec stream-compaction-with-count. Reconstructed from the
// s3d3_nonzero_with_count.h "Behavior" comment + the carved 562-B body's IVP primitive set.
// Real on-device symbol: nonzero_with_count_impl<T> with T ∈ {float, int}
//   _Z23nonzero_with_count_implIfEvjiijj / _Z23nonzero_with_count_implIiEvjiijj
//   = void nonzero_with_count_impl<T>(unsigned count,        // arg0 j : per-block scan length
//                                     int index_offset,      // arg1 i : struct @32 (INT32 imm)
//                                     int padding_val,       // arg2 i : struct @40 (INT32 imm)
//                                     unsigned src_handle,   // arg3 j : src base / pattern
//                                     unsigned dst_handle);  // arg4 j : dst base / pattern
// T selects ONLY the compare-vs-zero / max-select op (§6); everything else is dtype-agnostic
// because the OUTPUT is always INT32 indices + an INT32 count.
template <typename T>                                  // float<float> | int<int>
void nonzero_with_count_impl(unsigned count, int index_offset, int padding_val,
                             unsigned src_handle, unsigned dst_handle)
{
    vint32 write_cursor = vsplat32(0);                 // predicated 32-bit accumulator (= running ordinal)
    int32_t *dst = dst_base(dst_handle);               // INT32 dst Tensor3d
    int32_t  cursor_scalar = 0;                        // the final value -> the nonzero COUNT

    for (unsigned blk = 0; blk < count; ++blk) {       // loop over the src stream
        // (1) LOAD one src block of element-type T into a 512-bit vector.
        Tvec x = ivp_load_block<T>(src_handle, blk);   // T=float: fp32 lanes; T=int: int32 lanes

        // (2) DETECT nonzero -> vbool mask (the ONLY structurally-divergent step, §6):
        vbool nz;
        if (is_float<T>)                               // FLOAT leg:
            nz = ivp_neqn_2x32(x, vsplat_f32(0.0f));   //   x != 0  (32-bit fp NOT-EQUAL -> vbool),
                                                        //   ORDERED via the loop's ivp_olen_2xf32:
                                                        //   a NaN element is NOT ordered -> NOT counted;
                                                        //   +0.0 and -0.0 both compare == 0 -> NOT counted.
        else                                           // INT leg:
            nz = ivp_lt2nx8(/*signed compare ladder realizing*/ x, /*!=*/ 0);  // x != 0 (signed)

        // (3) COMPACT the nonzero element POSITIONS to the dst front. The vbool gates which
        //     lanes are written; the compacting select streams kept positions contiguously:
        vint32 positions = lane_iota(blk) ;            // flat positions of this block's lanes
        positions = ivp_addn_2x32t(positions, vsplat32(index_offset), /*pred=*/nz);  // + index_offset
        //   ivp_dselnx16t : DUAL-output two-source predicate-masked lane SELECT (the core compaction)
        //   ivp_sel2nx8i / ivp_sel2nx8i_s4 : two-source byte-lane permute (immediate-index packing)
        //   ivp_packvr2nx24 : pack/round the wide vector to the narrow INT32 output lane
        store_compacted_indices(dst, &cursor_scalar, positions, nz);

        // (4) ACCUMULATE: predicated 32-bit ADD advances the write-cursor on each TRUE lane.
        //     The cursor's terminal value IS the number of nonzeros written = the COUNT.
        write_cursor = ivp_addn_2x32t(write_cursor, vsplat32(1), /*pred=*/nz);
    }

    cursor_scalar = horizontal_sum_or_final(write_cursor);   // # of nonzeros written

    // (5) PAD: fill the dst index slots beyond the last emitted index with padding_val.
    for (int32_t k = cursor_scalar; k < (int32_t)count; ++k)
        dst[k] = padding_val;                          // struct @40

    // (6) WRITE the COUNT into the dst's +1 element (the s3d3_..._element_count_check_t3d slot).
    dst[count] = cursor_scalar;                        // header: "+1 element … indicates the nonzero count"
    // DEBUG build logs:  "S: NonzeroWithCount: num_elements=%d", cursor_scalar
}
```

### 4.2 The detect leg — compare-vs-zero → `vbool` `[HIGH presence / MED per-arm]`

The detect bundle (`float` @ `0x01001605` / `int` @ `0x01001845` in the codec image) is the
*only* structurally-meaningful divergence between the two templates:

- **`float` leg:** `ivp_neqn_2x32` (32-bit NOT-EQUAL → `vbool`: the `x != 0` test on the
  fp32 bit-pattern) paired with the loop's `ivp_olen_2xf32` (ordered-fp `<=` → `vbool`).
  NaN handling is **ordered**: an ordered fp compare returns FALSE on NaN, so a NaN input is
  **not** counted as nonzero. Signed zero: the `!= 0` test treats `+0.0` and `−0.0` alike
  (IEEE `0.0 == −0.0`), so a signed-zero element is **not** counted.
  `[neqn/olen OBSERVED; the precise ±0/NaN arm INFERRED-HIGH from the
  [vbool ISS semantics](../../iss/cas-predicate-boolean.md)]`
- **`int` leg:** `ivp_lt2nx8` (SIGNED less-than → `vbool`, the sign-bias compare-ladder
  trick) realizing the `x != 0` integer test. No fp/NaN concerns. `[OBSERVED]`

The compare writes the 16×64-bit `vbool` predicate (1 bit per lane). `[HIGH that a
compare→vbool detect is present + the int/fp op differs; the exact per-lane logic through
the FLIX desync is MED]`

### 4.3 The compaction — pack nonzero POSITIONS via select `[HIGH presence / MED per-arm]`

Shared by **both** legs (byte-identical): the lane-compaction primitives
`ivp_dselnx16t` (dual-output two-source predicate-masked select — the core compaction that
streams nonzero positions to the dst front, with the `vbool` `bitkillt`-merge gating which
lanes advance), `ivp_sel2nx8i` / `ivp_sel2nx8i_s4` (two-source byte-lane permute for the
immediate-index packing of the compacted lanes), and `ivp_packvr2nx24` (pack/round the wide
vector into the narrower INT32 output lane). The compaction writes each nonzero element's
flat position (the `+index_offset` add is the `ivp_addn_2x32t` of §4.4) to the dst,
advancing the write cursor only on `vbool`-true lanes. These are the **same** lane-permute
primitives [Sort](./sort.md) and [SparsityCompress](./sparsity-compress-tag.md) use — see
the [SuperGather scatter/gather ISA batch](../../isa/ref/b19-scatter-gather.md). `[HIGH that
select/dsel compaction is present in both bodies; the exact lane-routing schedule MED through
the FLIX desync]`

### 4.4 The count — predicated 32-bit write-cursor `[HIGH presence / MED mechanism]`

Shared by both legs: `ivp_addn_2x32t` — a **predicated** 32-bit vector ADD (the `_t` form,
`vbool`-gated). On each nonzero lane it increments the cursor; the cursor value *is* the
emitted index ordinal, and its **terminal value is the nonzero count**. All address/index
math is `*_2x32` 32-bit, matching the INT32 output. The count is therefore **not** a
`popcount` (the GPSIMD roster has none) and **not** a boolean-reduce (`randbn`/`rorbn` only
test all/any, not count): it is the terminal value of the predicated write-cursor, written
into the dst's `+1` element. This is consistent with the header (*"the dst's 1-more element
indicates the nonzero count"*), the body DEBUG log `"S: NonzeroWithCount: num_elements=%d"`,
and the `+1` element-count predicate (§5). `[the ivp_addn_2x32t predicated accumulate is
OBSERVED in both bodies; that it is the cursor-that-equals-the-count is INFERRED-HIGH from the
+1 dst rule + the num_elements DEBUG log + the absence of any popcount/radd op]`

> **GOTCHA — the count is the WRITE-CURSOR, not a separate popcount pass.** A reimplementation
> tempted to add a cross-lane `radd` of the `0/1` `vbool` (which would also yield the count)
> diverges from the firmware: **no `ivp_radd*` appears in the 562-byte body**. The count
> falls out *for free* as the terminal value of the same predicated cursor that drives the
> compaction write — one accumulate, two uses (the per-index ordinal *and* the final count).
> `[radd absence OBSERVED; cursor-equals-count INFERRED-HIGH]`

### 4.5 The output layout

The dst `Tensor3d` (INT32, `src_count + 1` elements) holds:

```
[ idx0, idx1, …, idx_{count-1},   padding_val, …, padding_val,   COUNT ]
   └──── nonzero positions ────┘   └──── pad fill ────┘          └ +1 slot
   (each = flat position + index_offset)                         (= # nonzeros)
```

The indices are the flat positions of the nonzeros (`+index_offset`); the unused middle
slots are `padding_val`; the `+1` element holds the count. `[layout INFERRED-HIGH from the
header + struct; the exact count-slot position (leading vs trailing) MED through the FLIX
desync — the header says only that the `+1` element "indicates the nonzero count",
consistent with a trailing or leading count slot]`

---

## 5. The validity / dtype contract

`is_valid_nonzero_with_count` (header, verbatim, identical across cayman/mariana/maverick —
only the NC-version banner differs):

| clause | rule | tag |
|---|---|---|
| `has_valid_neuron_header` / `has_valid_neuron_events` | standard header/events | HIGH/OBS |
| `has_nonzero_with_count_opcode` | `header.opcode == 0xf2` | HIGH/OBS |
| `has_valid_nc_nonzero_with_count` | `nc >= V3` (CAYMAN+; SUNDA NO) | HIGH/OBS |
| `check_active_channels` | `num_active_channels != 0 && <= POOLING_NUM_CHANNELS(128)` → `1..128` | HIGH/OBS |
| `s3d3_nonzero_with_count_reserved_zero` | `reserved0[0..2]==0 && reserved1[0..3]==0` | HIGH/OBS |
| `has_valid_immediates(index_offset, index_offset_src, …, Dtype::INT32)` | `index_offset` is INT32; src inline/ptr/reg-ptr valid | HIGH/OBS |
| `has_valid_immediates(padding_val, padding_val_src, …, Dtype::INT32)` | `padding_val` is INT32; src valid | HIGH/OBS |
| `is_valid_dtype(in_dtype, AllowFP32R::False)` | `in` ∈ valid set minus FP32R/64-b | HIGH/OBS |
| `is_valid_dtype(out_dtype, AllowFP32R::True)` | `out` ∈ valid set (FP32R allowed by the *generic* check) | HIGH/OBS |
| `has_valid_nonzero_with_count_dtype` | `in ∈ {FP32(0xA), INT32(0x8)} AND out == INT32(0x8)` | **HIGH/OBS — THE GATE** |
| `start_addr_active_channels(src/dst, num_active)` | partition-aligned start addrs | HIGH/OBS |
| `tensor3d_valid(src, in_dtype, WriteTensor::False, PSUM::False, SBUF::True)` | src read-only, SBUF only | HIGH/OBS |
| `tensor3d_valid(dst, out_dtype, WriteTensor::True, PSUM::False, SBUF::True)` | dst write, SBUF only | HIGH/OBS |
| `s3d3_nonzero_with_count_element_count_check_t3d` | `t3d_element_count(src) + 1 == t3d_element_count(dst)` | **HIGH/OBS — the `+1` slot** |

where `t3d_element_count(t) = num_elem[0]*num_elem[1]*num_elem[2]` (each `uint16`, cap
65535). `[all clauses read verbatim from the header, all three gens]`

### 5.1 The per-dtype matrix (input-keyed 2-row whitelist + fixed INT32 output)

The gate is the narrowest input-keyed gate of any GPSIMD compaction op: two admitted input
dtypes, one fixed output dtype. The *input* dtype selects the template leg; the *output* is
always INT32 (the op emits integer indices + an integer count).

| `in_dtype` | admitted? | `out_dtype` | compute leg | output |
|---|---|---|---|---|
| `FP32` (`0xA`) | Y | `INT32` (`0x8`) | `float`: `ivp_neqn_2x32` (`!=0`, ordered) + `ivp_maxunx16t` (unsigned) | INT32 indices + INT32 count (+ padding) |
| `INT32` (`0x8`) | Y | `INT32` (`0x8`) | `int`: `ivp_lt2nx8` (signed) + `ivp_maxnx16t` (signed) | INT32 indices + INT32 count (+ padding) |
| `INT8`/`UINT8`/`INT16`/`UINT16`/`UINT32` | **N** | — | gate-rejected | — |
| `FP16`/`BFLOAT16` | **N** | — | gate-rejected | — |
| `FP8_E3`/`E4`/`E5` | **N** | — | gate-rejected | — |
| `FP32R` (`0xB`) | **N** (as input) | — | rejected as INPUT (the generic `is_valid_dtype` allows it for OUTPUT, but the nonzero whitelist excludes it; out MUST be INT32 anyway) | — |
| `INT64`/`UINT64` | **N** | — | rejected (64-b) | — |
| `FP4`/`FP8_E2`/`INT4`/`SFP8` (MAVERICK MX) | **N** | — | rejected — **MX dtypes do NOT widen the gate** | — |

`[HIGH/OBSERVED — has_valid_nonzero_with_count_dtype read verbatim all three gens; the
rejection set is the complement of `{INT32, FP32}` input + the INT32-only output]`

### 5.2 The error path

The host-side decoder runs `dbg_is_valid_nonzero_with_count(i, nc)`
(`debug_assert.h:402-403`) — the full predicate above. A failing instruction (wrong dtype,
`num_active` outside `1..128`, wrong `+1` count, nonzero reserved, PSUM operand, `nc < V3`,
bad immediate) **fails the assert at decode, before dispatch**. On-device, the kernel logs
`"P%i: NonzeroWithCount : num_chans = %0d"` at decode and
`"S: NonzeroWithCount: num_elements=%d"` (the emitted count) in the body. No
nonzero-specific in-kernel error string exists in the carved DEBUG DRAM; validity is
enforced by the decode assert. `[HIGH for the debug_assert binding + the DEBUG strings; the
on-device assert-arm continuation MED]`

---

## 6. The `int<int>`-vs-`float<float>` split — byte-exact

The two 562-byte template bodies (`float` @ `0x010014e0`, `int` @ `0x01001720` in the CAYMAN
codec image) were byte-diffed this session: **only 18 of 562 bytes differ**, all clustered in
the compute bundles, with **no** control-flow / compaction / store byte changed. Their
`.xt.prop` function-descriptor span layouts are byte-for-byte identical
(`46,18,138,70,96,48,16,32,8,19,DATA,12,DATA,8,32,18`; extent 562 B, 3 DATA spans each) —
the smoking gun that they are one templated function.

The 18 differing bytes (`float` VA / `int` VA / `float` bytes / `int` bytes):

| Δ off | float VA | int VA | float | int | note |
|---|---|---|---|---|---|
| +0x61 | `0x1001541` | `0x1001781` | `00` | `80` | compute-bundle field |
| +0x71 | `0x1001551` | `0x1001791` | `40` | `c0` | compute-bundle field |
| +0xf2 | `0x10015d2` | `0x1001812` | `f0 65` | `d6 61` | **the compare/max OPCODE SELECTOR** |
| +0x12a | `0x100160a` | `0x100184a` | `f0 65` | `d6 61` | opcode selector |
| +0x132 | `0x1001612` | `0x1001852` | `f0 a5` | `d6 a1` | opcode selector |
| +0x14d…+0x16b | `0x100162d…` | `0x100186d…` | `65/90/d0e90d` | `7e/ec/50e84d` | load/select-op deltas |

The 16-byte compute bundle at file `+0xf0` is identical **except 2 bytes**:

```
FLOAT: 2f 35 f0 65  00 14 10 41  3f 25 01 09  28 3a 4a 43
INT:   2f 35 d6 61  00 14 10 41  3f 25 01 09  28 3a 4a 43
        ^^^^^ ^^^^^                                       <- the only difference: the IVP opcode-selector
```

The `f0 65` / `d6 61` field is the IVP opcode-selector nibble:

```
FLOAT body @0x01001605:  ivp_neqn_2x32  (!=0 -> vbool)  + ivp_maxunx16t (UNSIGNED max)
INT   body @0x01001845:  ivp_lt2nx8     (signed compare) + ivp_maxnx16t  (SIGNED max)
                         (int also swaps a load to ivp_l2a4nx8_ip)
```

The aligned per-bundle disasm confirms every **other** IVP slot is identical across the two
bodies (`ivp_olen_2xf32`, `ivp_addn_2x32t`, `ivp_dselnx16t`, `ivp_sel2nx8i`,
`ivp_sel2nx8i_s4`, `ivp_packvr2nx24`). This is the definitive proof: **one algorithm, the
C++ template instantiated twice, differing only in the input-dtype compare/max op
(fp/unsigned vs signed).** The compaction, the index/count accumulate, and the store are
dtype-agnostic because the output is always INT32. `[HIGH/OBSERVED — Python byte-diff +
per-bundle disasm; the precise per-op semantics of the differing arm is HIGH for the
mnemonic, MED for the exact lane operands through the FLIX desync]`

---

## 7. Dispatch — `kernel_info_table` `0xf2` → trampoline → dtype-branch → body

NonzeroWithCount is reached through the POOL [`kernel_info_table`](../pool/pool-dispatch.md)
linear-scan dispatcher (the keyed `(opcode<<24)|(spec<<16)` scan + single `callx8`), in
**both** the MAIN and the codec image:

| image | table @ file | row | record | trampoline | bodies |
|---|---|---|---|---|---|
| CAYMAN_0 (MAIN) | `0x7400` (17 entries) | idx15 | `{ spec 0, opcode 0xf2, funcVA 0x0100484c }` | `0x0100484c` (state `0x0200047c`) | `<float> 0x01004940` / `<int> 0x01004b80` |
| CAYMAN_3 (CODEC) | `0x4748` (9 entries) | idx5 | `{ spec 0, opcode 0xf2, funcVA 0x010013f4 }` | `0x010013f4` | `<float> 0x010014e0` / `<int> 0x01001720` |
| MARIANA / MARIANA_PLUS (MAIN) | `0x7400` | idx15 | `{ spec 0, opcode 0xf2 }` | `0x01004890` | byte-identical body |

In the codec table the neighbors are `idx4 0xbe` GetSequenceBounds → `idx5 0xf2`
NonzeroWithCount → `idx6 0x7b` dequant — exactly the POOL-codec family order. In the MAIN
table `idx14 0xbe` / `idx15 0xf2` / `idx16 0x7b` are directly adjacent. `[HIGH/OBSERVED — all
tables byte-decoded]`

### 7.1 The entry-trampoline → dtype-branch → body bridge

The `funcVA` in the `kernel_info_table` is a **trampoline**, not a body func-start. In
annotated C pseudocode (naming the real recovered addresses):

```c
// POOL-codec NonzeroWithCount trampoline (CAYMAN_3 @0x010013f4 / CAYMAN_0 @0x0100484c).
// Reached from the kernel_info_table 0xf2 linear-scan hit via `callx8 funcVA`.
void nonzero_with_count_trampoline(const NEURON_ISA_TPB_S3D3_NONZERO_WITH_COUNT_STRUCT *i)
{
    // (a) entry a1,32                      ; windowed-ABI prologue (codec @0x010013f4)
    // (b) read the in_dtype byte (struct off 13) into a register, then BRANCH on it:
    uint8_t in_dtype = i->in_dtype;         // INT32(0x8) | FP32(0xA)
    // bnei a6,4,<int-path>   @VA 0x0100148d   — the dtype discriminator
    if (in_dtype == FP32) {                  // FLOAT leg
        // const16 a2,0x14e0  @VA 0x010014bb ; callx8 a2
        nonzero_with_count_impl<float>(count, index_offset, padding_val, src, dst);  // body @0x010014e0
    } else {                                 // INT leg
        // const16 a2,0x1720  @VA 0x010014c4 ; callx8 a2
        nonzero_with_count_impl<int>(count, index_offset, padding_val, src, dst);    // body @0x01001720
    }
}
```

The trampoline carries a `float`-leg FLIX setup bundle (@`0x1001461`:
`ivp_float16nx16t` + `ivp_lvn_2x16s_i` — fp16 convert/load setup) corroborating the dtype
branch. This is exactly the [GetSequenceBounds](./get-sequence-bounds.md) /
[decode_pool](../pool/pool-dispatch.md) trampoline pattern (a setup + `const16`-built
`callx8` into the inlined body), here **extended** with a 2-way dtype branch — GetSequenceBounds
branched on a *runtime* DTYPE arg; NonzeroWithCount branches into one of **two precompiled
template bodies**. `[HIGH/OBSERVED — the entry, the `bnei` dtype branch, and BOTH `const16`
body-call sites byte-decoded; the exact branch-condition register/immediate is MED through the
FLIX desync]`

### 7.2 `.xt.prop` section ordering (the POOL-codec family)

The CAYMAN_3 codec `.xt.prop` order is `get_sequence_bounds_impl` (#8) →
`nonzero_with_count_impl<float>` (#9) → `nonzero_with_count_impl<int>` (#10) →
`decode_tensor_dequantize` (#11) → `cptc_decode_impl<1..6>`. NonzeroWithCount sits between
GetSequenceBounds and the dequant/cptc codec ops — the confirmed POOL-codec family.
`[HIGH/OBSERVED — demangled section ordering]`

---

## 8. Per-generation presence

| gen | opcode `0xf2` | `s3d3_nonzero_with_count.h` | dtype gate | firmware body |
|---|---|---|---|---|
| **TONGA (V1)** | — | — | — | (no POOL codec) |
| **SUNDA (NC-v2)** | — (no `0xf2`) | — (no header) | — (`nc < V3`) | ABSENT (not in image) |
| **CAYMAN (NC-v3)** | `0xf2` | present | `{FP32,INT32}→INT32` | MAIN `0x01004940`/`0x01004b80`; CODEC `0x010014e0`/`0x01001720` |
| **MARIANA (NC-v4)** | `0xf2` | present | `{FP32,INT32}→INT32` | byte-identical body (`0x01004890` MAIN) |
| **MAVERICK (NC-v5)** | `0xf2` | present | `{FP32,INT32}→INT32` | header-grounded; firmware body NOT byte-confirmed in the stripped MAVERICK carve (§9) |

**Presence evidence (OBSERVED this session):**

- **HEADER:** opcode `0xf2` + struct + the identical validity predicate present in
  cayman/mariana/maverick `arch_isa`; ABSENT in sunda (no opcode, no header). Struct
  compile-verified `sizeof == 64` with identical offsets for all three supporting gens.
- **FIRMWARE:** the `nonzero<float>` + `<int>` `.xt.prop` symbols (6× each) + the distinctive
  compute-bundle byte signatures (`float "2f 35 f0 65"`, `int "2f 35 d6 61"`, 12× each)
  present in CAYMAN_0, CAYMAN_3, and the MARIANA/MARIANA_PLUS MAIN image
  (`GEN2_main == GEN3_main`, byte-identical sha `39416fbf…`). The body is therefore
  **byte-identical across CAYMAN/MARIANA/MARIANA_PLUS**.
- **DEBUG STRINGS:** `"P%i: NonzeroWithCount : num_chans = %0d"` /
  `"S: NonzeroWithCount: num_elements=%d"` present in the CAYMAN/MARIANA/MARIANA_PLUS POOL
  DEBUG DRAM (7 hits total across the gen regions).

⇒ **NC-v3+ only.** No per-gen algorithm change: the struct (64 B), opcode (`0xf2`), dtype
gate (`{FP32,INT32}→INT32`), channel rule (`1..128`), `+1`-count rule, and the 562-byte
compute body are byte-identical CAYMAN→MARIANA→MARIANA_PLUS. The MAVERICK MX dtypes
(`FP8_E2`, `INT4`, `SFP8`, `FP4`) are **not** admitted — the gate stays `{FP32,INT32}`.

> **CORRECTION — MAVERICK does NOT rename the struct to "S3D3_l"; the typedef is unchanged.**
> A prior report claimed the MAVERICK header "anonymizes the doc-name to `S3D3_l`". Read
> verbatim this session, the MAVERICK `aws_neuron_isa_tpb_s3d3_nonzero_with_count.h` keeps
> the **identical** typedef `NEURON_ISA_TPB_S3D3_NONZERO_WITH_COUNT_STRUCT`, the identical
> `// NonzeroWithCount Instruction` doc-comment, and the identical `has_valid_nonzero_with_count_dtype`
> gate; the **only** difference from MARIANA is the banner line `ISA header for NC-v4` →
> `ISA header for NC-v5`. No `S3D3_l` alias exists anywhere in the MAVERICK `arch_isa`
> headers (grep → 0 hits). Per the standing v5 rule, the MAVERICK *header* difference is
> OBSERVED but the v5 firmware interior is INFERRED (no MAVERICK firmware body was
> byte-decoded — §9). `[header read HIGH/OBSERVED; v5 firmware interior INFERRED]`

---

## 9. Honesty ledger

**HIGH / OBSERVED** (direct disasm / byte / symtab / header / compile this session):

- `OPCODE_NONZERO_WITH_COUNT = 0xf2`; struct `S3D3_NONZERO_WITH_COUNT` (64 B);
  `instruction_mapping.json:256-258` binding; `debug_assert.h:402-403` case — **compile-verified
  all three supporting gens** (`sizeof == 64`; `header@0, events@4, num_active@12, in_dtype@13,
  out_dtype@14, index_offset_src@15, src@16, index_offset@32, padding_val_src@36, padding_val@40,
  dst@44, reserved1@60`).
- The two `.xt.prop` template symbols `_Z23nonzero_with_count_implIfEvjiijj` /
  `_Z23nonzero_with_count_implIiEvjiijj` (6× each in the binary), demangled to
  `nonzero_with_count_impl<float>/<int>(uint,int,int,uint,uint)`.
- The dtype gate `in ∈ {FP32(0xA),INT32(0x8)}`, `out == INT32(0x8)`; the `+1` element-count
  rule (`t3d(src)+1==t3d(dst)`); channels `1..128`; reserved-zero; `nc >= V3` — read verbatim
  all three gens.
- Dispatch byte-exact: CAYMAN_3 codec idx5 `0xf2→0x010013f4→<float>0x010014e0/<int>0x01001720`;
  CAYMAN_0 MAIN idx15 `0xf2→0x0100484c→0x01004940/0x01004b80`; MARIANA/MARIANA_PLUS idx15
  `0xf2→0x01004890`. Present in BOTH the MAIN and the codec image.
- The trampoline → dtype-branch → body bridge: `entry a1,32`; `bnei a6,4` (dtype);
  `const16 a2,0x14e0` (float) / `const16 a2,0x1720; callx8` (int).
- The two 562-B bodies differ in **only 18/562 bytes**, all in the compute bundle; the
  differing IVP opcode-selector = `float ivp_neqn_2x32/maxunx16t` vs `int ivp_lt2nx8/maxnx16t`;
  the compaction (`ivp_dselnx16t/sel2nx8i/sel2nx8i_s4/packvr2nx24`) + accumulate
  (`ivp_addn_2x32t`) + loop compare (`ivp_olen_2xf32`) byte-identical. Both compute-bundle
  signatures (`2f35f065`/`2f35d661`) present 12× each in the binary.
- The DEBUG self-names `"P%i: NonzeroWithCount : num_chans = %0d"` /
  `"S: NonzeroWithCount: num_elements=%d"`.
- Per-gen: NC-v3+ only; ABSENT SUNDA; byte-identical struct/opcode/gate/body across
  CAYMAN/MARIANA/MARIANA_PLUS; MAVERICK header-identical (NC-v5 banner only, **no** `S3D3_l`
  rename); MX not admitted.

**MED / INFERRED:**

- The 5 template args → struct fields mapping (the two signed `int` args = `index_offset` +
  `padding_val` INT32 immediates) — INFERRED-HIGH from the demangled signature + the struct.
- The COUNT = terminal value of the `ivp_addn_2x32t` write-cursor written to the `+1` slot
  (the predicated accumulate is OBSERVED; the cursor-equals-count is inferred from the `+1`
  rule + the `num_elements` DEBUG log + the absence of any popcount/radd).
- The exact compaction lane-routing schedule + the padding fill loop + the count-slot position
  (leading vs trailing) — through the FLIX desync.
- The fp `±0` / NaN nonzero arm (not-counted) — inferred from the ordered/`!=0`
  [vbool ISS semantics](../../iss/cas-predicate-boolean.md), not a byte-pinned arm.

**LOW / NOT CLAIMED:**

- The MAVERICK firmware-body bytes (header-grounded only; the MAVERICK carve is a stripped
  ET_DYN DEBUG/PERF image with no `.xt.prop` names and the nonzero byte-signature absent in
  the carved windows — **v5 interior INFERRED**).
- Any host/NKI API surface for NonzeroWithCount (none found in the customop-lib beyond the
  ISA headers; it is an internal ucode/codec op).
- The exact byte-level micro-schedule of the index/count accumulate per dtype.

> **FLIX-DESYNC FLAG.** The two bodies (`0x010014e0..0x01001712` `<float>` /
> `0x01001720..0x01001952` `<int>`) and the trampoline (`0x010013f4..0x010014e0`) are
> hand-scheduled FLIX VLIW with 3 interleaved DATA spans that desync stock
> `xtensa-elf-objdump`'s linear sweep (spurious `bbci.w15`/`bbsi.w15` + literal-pool
> mislabels). The HIGH/OBSERVED facts are the `.xt.prop` func-starts + span layout, the
> `kernel_info_table` bytes, the entry + the `bnei` dtype branch + both `const16`-built
> body-call sites, the demangled template symbols, the 18-byte diff + the aligned compare
> bundle, the shared compaction/accumulate IVP set, the DEBUG self-names, and the
> compile-verified struct/opcode/gate. The mis-decoded `.byte` / spurious `j`/`call` targets
> (long-literal-pool `l32r` mislabels) are NOT reported as real. `[flagged]`

---

## 10. Relation to the sibling compaction / codec kernels

| | engine | opcode | scope | output | index payload | nc gate |
|---|---|---|---|---|---|---|
| **NonzeroWithCount** (this page) | POOL | `0xf2` | per-partition compaction | nonzero **positions** + a **count**, INT32 out | INT32, positions only | `nc >= V3` |
| [GetSequenceBounds](./get-sequence-bounds.md) | POOL | `0xbe` | per-partition `{min,max}` bounds | `{min, max}` pair, `dst == 2×src` | n/a (bounds, not indices) | (codec) |
| [Sort](./sort.md) | POOL | `0x96` | per-partition full total order | `{sorted values, original indices}`, in == out dtype | u16/u32, every element | none (all gens) |
| [SparsityCompress](./sparsity-compress-tag.md) | **DVE** | `0xE0`/`0xE1` | fixed top-4 reduction per group | top-4 `{value, tag}` | within-vector 16-b tag | V4 |

- **vs GetSequenceBounds (same POOL-codec family, the immediate neighbor).** Both are `S3D3`
  codec ops, both via the entry-trampoline + `const16`-`callx8` pattern, both present in BOTH
  the MAIN and codec images, both sharing the IVP min/max/compare vocabulary
  (`maxun/maxn/olen/neqn/lt`). But GetSequenceBounds takes the DTYPE as a *runtime* by-value
  arg (one body, single-channel, `dst == 2×src`); NonzeroWithCount is a *compile-time*
  template (two bodies, multi-channel `1..128`, `dst == src+1`, out == INT32 only).
  `[HIGH/OBSERVED]`
- **vs Sort (same POOL engine).** Both compose from the same lane-permute + min/max set
  (`ivp_dselnx16t`/`ivp_sel2nx8i`/`ivp_sel2nx8i_s4` + the `_2x32` 32-b index machinery +
  `ivp_lt2nx8`/ordered-fp compare) and both carry an index payload. NonzeroWithCount
  *compacts* (a predicated cursor over positions, INT32-only output, with a count slot); Sort
  additionally *totally orders* (a compare-exchange network) and emits both the value and the
  index in the input dtype. `[HIGH/OBSERVED]`
- **vs SparsityCompress (DVE engine).** They share the compare→`vbool` + select compaction
  core and the `{value, index/tag}` pairing, but SparsityCompress is a *fixed top-4
  reduction* on the **DVE** engine that emits `{value, tag}` for the
  [PE matrix-multiply path](./sparsity-compress-tag.md), whereas NonzeroWithCount is an
  *unbounded-count full compaction* on **POOL** that emits `{positions, count}` with no value
  payload. Both are "compaction" kernels composed from the same `vbool`+select primitives, on
  different engines, for different consumers. `[HIGH/OBSERVED]`
- **vs the predicate / compaction ISS primitives.** The detect leg *is* the
  [vbool predicate](../../iss/cas-predicate-boolean.md) compare→`vbool` (float = ordered-fp
  `!=0`, NaN→not-counted; int = signed compare); the compaction *is* the two-source select
  (`sel`/`dsel`) with the per-lane `bitkillt` predicate guard from the
  [SuperGather scatter/gather batch](../../isa/ref/b19-scatter-gather.md); the count is the
  predicated write-cursor (not the boolean-reduce, which only tests all/any). `[HIGH/OBSERVED
  — the compare/select/accumulate mnemonics match the ISS model]`

---

## See also

- [GetSequenceBounds](./get-sequence-bounds.md) *(planned)* — the `0xbe` codec neighbor that
  immediately precedes NonzeroWithCount in the `.xt.prop` order; the trampoline-template
  ancestor (runtime-DTYPE branch vs this op's compile-time template branch).
- [Sort / DECODE_SORT](./sort.md) — the POOL value+index sort (`0x96`); the full-ordering
  generalization of compaction.
- [SparsityCompress / SparsityCompressTag](./sparsity-compress-tag.md) — the DVE-engine top-4
  `{value, tag}` reduction; the cross-engine compaction relative.
- [POOL Engine Main Dispatch Loop](../pool/pool-dispatch.md) — the `kernel_info_table`
  linear-scan dispatcher; §6 idx15 is the `0xf2` NonzeroWithCount row (CORRECTION: its name
  resolution, §1).
- [Predicate / Boolean (vbool) ISS Semantics](../../iss/cas-predicate-boolean.md) *(planned)*
  — the compare→`vbool` detect + the `bitkillt` merge-mask + the ordered-fp NaN convention.
- [ISA Batch 19 — SuperGather Scatter/Gather](../../isa/ref/b19-scatter-gather.md) — the
  `sel`/`dsel`/gather lane-compaction primitives that pack the nonzero positions.
- [Confidence & Walls Model](../../reference/confidence-model.md) — the tag taxonomy.
