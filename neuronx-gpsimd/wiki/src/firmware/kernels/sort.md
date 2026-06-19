# Sort / DECODE_SORT

This page decodes the **Sort** kernel of the NeuronCore GPSIMD ISA — the **POOL-engine**
handler that takes a 1-D vector inside a partition and emits a 2-D result holding *both*
the **sorted values** and the **original indices** of those values, totally ordered. It
pins the opcode (`0x96`), the 64-byte `S1D2_SORT` operand struct byte-for-byte from the
shipped ISA header (compile-verified `sizeof == 64` on all four generations), the
key+index (`value = original-position`) payload model, the direction selector
(ascending `IsLT` / descending `IsGT`), the stable flag, the built-in **top-K** via a
short output Y-dimension, and the in-register **compare-exchange sorting-network**
datapath recovered from the carved POOL firmware images. It also carries one standing
**CORRECTION**: `SortMerge` is a *phantom* — opcode `0x97` is never a real shipped
opcode, `SortMerge` is `wip`, and it is **not** the Sort instruction.

Sort runs on the **Cadence Tensilica Vision-Q7 NX "Cairo" 512-bit FLIX/VLIW DSP**
(`ncore2gp` config, one per NeuronCore) — specifically on the **POOL** (Pooling)
sequencer. The header states it verbatim: *"The entire vector must be read into Pool
working RAM."* The decode/error/self-name strings (`decode_sort`, `"S: Sort"`,
`"unsupported data type for sort"`) live **only** in the POOL image pools — never in the
DVE, PE, ACT, or SP images. This places Sort squarely on POOL, in deliberate contrast to
[SparsityCompress](./sparsity-compress-tag.md) (a DVE-engine top-4 reduction) and
alongside [NonzeroWithCount](./nonzero-with-count.md) (the sibling POOL compaction
kernel). All three compose from the same lane-permute + min/max ISA primitives surveyed
in the [select/shuffle ISA batch](../../isa/ref/b21-select-shuffle.md).

Confidence and evidence tags follow the project
[Confidence & Walls Model](../../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. Every host-ISA fact is read out of the public
`aws_neuron_isa_tpb_*.h` headers shipped in the customop-lib package and was
re-compile-verified this session; every device fact is byte-pinned to a carve from
`libnrtucode_internal.so`.

> **NOTE — what was carved this session, and the exact objects used.** The firmware
> container is
> `…/custom_op/c10/lib/libnrtucode_internal.so`
> (`sha256 b7c67e898a116454…`, `10,276,288 B`, ELF64 x86-64 DYN — the FW-47/60/73
> anchor, re-verified in-task). The POOL images are reached via the host x86 accessors
> `<ARCH>_<NX|Q7>_POOL_<BUILD>_<IRAM|DRAM>_get` (each `lea` gives the blob's file
> pointer, the `movq` immediate its size; the blobs live in `.rodata` where VMA == file
> offset) and disassembled with the native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`,
> GNU Binutils 2.34 / Xtensa Tools 14.09) that ships inside the gpsimd-tools package. The
> recovered `S:`/`DECODE_SORT` log strings and the `s1d2_sort` / `common.h` ISA structs
> are themselves binary evidence and are cited as such.
>
> | object | accessor | file off / size | note |
> |---|---|---|---|
> | `CAYMAN NX_POOL DEBUG IRAM` | `CAYMAN_NX_POOL_DEBUG_IRAM_get` | `0x1b1420` / `0x1c820` | Sort body present |
> | `CAYMAN NX_POOL DEBUG DRAM` | — | `0x1cdc40` / `0x6f20` | `"S: Sort"` @`+0x2d34` |
> | `CAYMAN Q7_POOL DEBUG IRAM` | `CAYMAN_Q7_POOL_DEBUG_IRAM_get` | `0x249020` / `0x1ea40` | Sort body present |
> | `CAYMAN Q7_POOL DEBUG DRAM` | — | `0x267a60` / `0x15d00` | `decode_sort`, `DECODE_SORT`, error str |
> | `SUNDA  NX_POOL RELEASE IRAM` | `SUNDA_NX_POOL_RELEASE_IRAM_get` | `0x2be10` / `0xd040` | **body-deep**; primitives present; RELEASE strips DEBUG strings |
> | `SUNDA  Q7_POOL RELEASE IRAM` | `SUNDA_Q7_POOL_RELEASE_IRAM_get` | `0x48bf0` / `0x42d0` | minimal stub (4 primitive hits — body NOT here) |
>
> The four Sort DEBUG strings reproduce against the binary in-task (28 hits across the
> image-pool copies): `decode_sort`, `"P%i: DECODE_SORT : num_items = %d,
> num_active_channels = %d, is_ascending = %d"`, `"P%i: Error, unsupported data type for
> sort : 0x%x"`, and `"S: Sort"`. `[HIGH/OBSERVED]`

---

## 1. The headline

Sort is a **first-class, named ISA instruction** — not a composed host helper. It has
its own opcode, its own 64-byte operand struct, its own validity predicate, and its own
self-named on-device kernel. Eight facts pin it:

1. **Opcode = `0x96`, struct = `S1D2_SORT` (64 B).** From `common.h:230`
   (`NEURON_ISA_TPB_OPCODE_SORT = 0x96, // Y`), the `instruction_mapping.json`
   `struct2opcode` binding `NEURON_ISA_TPB_S1D2_SORT_STRUCT →
   [NEURON_ISA_TPB_OPCODE_SORT]`, and `debug_assert.h:243` (`case
   NEURON_ISA_TPB_OPCODE_SORT: check = dbg_is_valid_s1d2_sort(i);`). The struct
   compile-verifies `sizeof == 64` on cayman/mariana/maverick/sunda. `[HIGH/OBSERVED]`

2. **It is a key+VALUE sort, the value being the original position.** Output is a 2-D
   tensor: `dst[X=0,Y=k]` is the k-th **sorted value**, `dst[X=1,Y=k]` is the **original
   index** (plus `index_offset`) of that value. So Sort carries *both* the keys (the
   values) and a payload (where each value came from). `[HIGH/OBSERVED — header verbatim
   + worked example]`

3. **Direction is the `comparison` AluOp, restricted to two values.**
   `IsLT (0x16) = ASCENDING`, `IsGT (0x13) = DESCENDING`. These are the *only* two legal
   AluOps (`sort_valid_aluop`); the decode log's `is_ascending` field is exactly the
   `IsLT`-vs-`IsGT` discriminator. `[HIGH/OBSERVED]`

4. **Stable is a 1-bit flag.** `stable == 1` preserves equal elements' relative order;
   `stable == 0` "might be faster" per the header. Both legal (`sort_valid_stable`).
   `[HIGH/OBSERVED]`

5. **dtype is gated to 16-bit and 32-bit, in == out, integer index.** `fp16/bf16/u16/i16`
   → u16 index; `fp32/u32/i32` → u32 index. `in_dtype` must equal `out_dtype`; any other
   dtype is rejected with `"unsupported data type for sort : 0x%x"`. `[HIGH/OBSERVED]`

6. **Top-K is built in.** The output may be *shorter* along Y than the input; with a
   descending sort this yields the top-K largest `{value, index}` pairs — Sort subsumes
   top-K (and, via the index payload, top-K arg-max). `[HIGH/OBSERVED — header note]`

7. **The algorithm is an in-register SIMD compare-exchange sorting network.** A
   data-parallel min/max network (bitonic / odd-even-merge family), *not* a scalar
   insertion sort — recovered from the carved POOL IRAM as ordered-compare→`vbool` driving
   a dual-select swap, with the index payload permuted by the same stage. `[atom
   HIGH/OBSERVED; network family MED/INFERRED through the FLIX desync]`

8. **Present on all four generations, with no nc-version gate.** SUNDA (NC-v2), CAYMAN
   (NC-v3), MARIANA (NC-v4), MAVERICK (NC-v5) all ship the struct, the validity
   predicate, opcode `0x96`, and `SORT_MAX_SIZE = 8192`, byte-identical. Sort is *older
   / more universal* than the codec ops added later. `[HIGH/OBSERVED]`

> **CORRECTION — `SortMerge` (`0x97`) is a PHANTOM.** The header repeatedly mentions a
> *SortMerge* companion that would merge separately-sorted subtensors ("…then the sorted
> subtensors can be merged with the SortMerge instruction… SortMerge can merge across
> partitions"). **It is not shipped, and `0x97` is not a real opcode.** `common.h:231`
> reads `NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_SELECT = 0x98, // SortMerge wip 0x97 // Y` —
> i.e. the `0x97` slot is a *work-in-progress* comment, the slot itself is unassigned in
> the enum, and `0x98` was reassigned to `TENSOR_SCALAR_SELECT`. There is **no**
> `SortMerge` struct, **no** `SortMerge` opcode value, and **no** `SortMerge` DEBUG string
> in any carved image. Do **not** treat `0x97` as Sort, as SortMerge, or as any real
> instruction; do not "discover" it. In v0.21.2.0 only the single-block Sort (`0x96`)
> ships; cross-block / cross-partition merge is host-side or future. `[HIGH/OBSERVED — the
> "wip 0x97" comment + the absent struct/opcode/string]`

---

## 2. Opcode, struct binding, and engine placement

**Opcode.** Read verbatim from `aws_neuron_isa_tpb_common.h` (cayman line 230;
byte-identical in sunda/mariana/maverick):

```c
NEURON_ISA_TPB_OPCODE_SORT                 = 0x96,    // Y
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_SELECT = 0x98,    // SortMerge wip 0x97  // Y
```

The `// Y` annotation marks Sort a **maintained, wired** op. The adjacent line is the
single source of the SortMerge-phantom: `0x97` exists only as the parenthetical "wip"
note, never as an enumerator. `[HIGH/OBSERVED]`

**Struct binding.** The shipped `instruction_mapping.json` `struct2opcode` table binds
the operand struct to the opcode exactly once:

```json
"NEURON_ISA_TPB_S1D2_SORT_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_SORT"
]
```

and `debug_assert.h` routes decode-time validation:

```c
case NEURON_ISA_TPB_OPCODE_SORT:
    check = dbg_is_valid_s1d2_sort(i);
    break;
```

`[HIGH/OBSERVED]`

**Engine = POOL.** Three independent signals converge. (a) The header's own
"Pool working RAM" statement (§5). (b) The `decode_sort` / `"S: Sort"` /
`"DECODE_SORT … is_ascending"` / `"unsupported data type for sort"` DEBUG strings live
*only* in the POOL image DRAM pools (CAYMAN/MARIANA/MARIANA_PLUS NX & Q7 POOL DEBUG;
SUNDA NX/Q7 POOL RELEASE) — carved via the `<ARCH>_<NX|Q7>_POOL_*_get` host accessors. (c)
The compare-exchange body primitives (§5) appear in the POOL IRAMs. This contrasts with
[SparsityCompress](./sparsity-compress-tag.md), which is a DVE op (`engine_idx = 3`).
Note the POOL engine exists in *two* image flavours — `NX_POOL` (the NeuronCore pooling
unit) and `Q7_POOL` (the Vision-Q7 pooling path) — and the Sort body is carried by the
**NX_POOL** image on every generation (the `Q7_POOL` image is the body home on CAYMAN but
a minimal stub on SUNDA; §7). See [decode_pool](./decode-pool.md) for the POOL kernel
disambiguation. `[HIGH/OBSERVED]`

---

## 3. The operand struct — `S1D2_SORT_STRUCT` (64 B, compile-verified all 4 gens)

`S1D2` names the shape: **one 1-D source**, **one 2-D destination**. The struct from
`aws_neuron_isa_tpb_s1d2_sort.h` (cayman; sunda/mariana/maverick differ only in the
NC-version banner comment):

```c
typedef struct NEURON_ISA_TPB_S1D2_SORT_STRUCT {
    NEURON_ISA_TPB_HEADER             header;                  //  4   ( 0 -  3)
    NEURON_ISA_TPB_EVENTS             events;                  //  8   ( 4 - 11)
    uint8_t                           num_active_channels;     //  1   (12     )
    NEURON_ISA_TPB_IMM_SRC            index_offset_src;        //  1   (13     )
    NEURON_ISA_TPB_DTYPE_PAIR         in_out_dtype;            //  1   (14     ) // in low, out high
    NEURON_ISA_TPB_ALU_OP             comparison;              //  1   (15     )
    NEURON_ISA_TPB_TENSOR1D           src_mem_pattern;         //  8   (16 - 23)
    NEURON_ISA_TPB_TENSOR2D           dst_mem_pattern;         // 12   (24 - 35)
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD index_offset;            //  4   (36 - 39)
    uint8_t                           stable;                  //  1   (40     )
    uint8_t                           reserved1[23];           // 23   (41 - 63)
} NEURON_ISA_TPB_S1D2_SORT_STRUCT;

static const uint32_t NEURON_ISA_TPB_SORT_MAX_SIZE = 8192U;
ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_S1D2_SORT_STRUCT) == 64,
                  "Error: NEURON_ISA_TPB_S1D2_SORT_STRUCT is NOT 64B.");
```

**Compile-verify output (this session, gcc, all four gens identical):**

```
sizeof(S1D2_SORT)=64
header=0 events=4 num_active_channels=12 index_offset_src=13
in_out_dtype=14 comparison=15 src=16 dst=24 index_offset=36 stable=40 reserved1=41
OPCODE_SORT=0x96 SORT_MAX_SIZE=8192 IS_LT=0x16 IS_GT=0x13
FP16=0x7 BF16=0x6 U16=0x5 I16=0x4 FP32=0xa U32=0x9 I32=0x8
sizeof TENSOR1D=8 TENSOR2D=12 DTYPE_PAIR=1 IMM_VAL=4 ALU_OP=1 HEADER=4 EVENTS=8
```

`[HIGH/OBSERVED — ISA_STATIC_ASSERT(==64) passes; offsetof/sizeof reproduced for
cayman, sunda, mariana, maverick — bit-identical]`

### 3.1 Field census

| off | size | field | type | role |
|---|---|---|---|---|
| 0 | 4 | `header` | `HEADER` | `{ opcode = 0x96, inst_word_len, … }` |
| 4 | 8 | `events` | `EVENTS` | wait/update semaphore sync |
| 12 | 1 | `num_active_channels` | `uint8_t` | 1..128 partitions; **each sorts its own row independently** |
| 13 | 1 | `index_offset_src` | `IMM_SRC` | how `index_offset` is delivered (inline / ptr / reg) |
| 14 | 1 | `in_out_dtype` | `DTYPE_PAIR` | packed 4+4: `dtype_lo` = in, `dtype_hi` = out |
| 15 | 1 | `comparison` | `ALU_OP` | `IsLT (0x16)` = ASC \| `IsGT (0x13)` = DESC |
| 16 | 8 | `src_mem_pattern` | `TENSOR1D` | INPUT 1-D vector (read-only, SBUF) |
| 24 | 12 | `dst_mem_pattern` | `TENSOR2D` | OUTPUT 2-D `[values \| indices]` (write, SBUF) |
| 36 | 4 | `index_offset` | `IMM_VAL` | u32 added to each emitted index |
| 40 | 1 | `stable` | `uint8_t` | 0 \| 1 (stable-sort flag) |
| 41 | 23 | `reserved1[23]` | `uint8_t[]` | MUST be 0 |

**`in_out_dtype` is a packed 4+4 `DTYPE_PAIR` bitfield** (`sizeof == 1`):

```c
typedef struct NEURON_ISA_TPB_DTYPE_PAIR {
    NEURON_ISA_TPB_DTYPE dtype_lo : 4;   // input dtype
    NEURON_ISA_TPB_DTYPE dtype_hi : 4;   // output dtype
} NEURON_ISA_PACKED NEURON_ISA_TPB_DTYPE_PAIR;
```

Sort does **not** convert dtype: `sort_dtypes_ok` requires `dtype_lo == dtype_hi`
(§4). (Contrast [NonzeroWithCount](./nonzero-with-count.md), whose output is always
INT32.) `[HIGH/OBSERVED]`

**`index_offset_src` selects the immediate-delivery mode** (`NEURON_ISA_TPB_IMM_SRC`):

```c
NEURON_ISA_TPB_IMM_SRC_INSTRUCTION_IMMEDIATE = 0,    // u32 baked in the instruction
NEURON_ISA_TPB_IMM_SRC_POINTER_IMMEDIATE     = 1,    // PartitionOffset pointer to u32
NEURON_ISA_TPB_IMM_SRC_REG_PTR_IMMEDIATE     = 2,    // pointer to u32 from a register
```

This matches the header verbatim — `index_offset` "can be any of: a pointer to u32
values; a u32 instruction immediate; a reg num." Its purpose is to **sort pieces of a
larger array with correct global indices** (each tile carries the right `index_offset`).
`[HIGH/OBSERVED]`

> **NOTE — the struct is mostly reserved tail.** Sort needs few fields, so bytes 41–63
> are `reserved1[23]` and must be zero. The whole control surface is the 4 small scalar
> fields at offsets 12–15 plus the three tensor/immediate fields. There is no
> "algorithm" field, no "network-stage count" field — the network is fixed by the dtype
> and the element count, not parameterised in the instruction word. `[HIGH/OBSERVED]`

---

## 4. dtype + direction + stable + index-width matrix

`is_valid_s1d2_sort` (header, verbatim, identical all four gens):

```c
fn is_valid_s1d2_sort(i: Inst) -> bool {
       has_valid_neuron_header(i)
    && has_valid_neuron_events(i)
    && has_sort_opcode(i)                                            // == 0x96
    && sort_dtypes_ok(i)                                             // 16/32-b, in == out
    && is_valid_enum(EnumList::ImmSrc, i.s1d2_sort.index_offset_src)
    && sort_valid_aluop(i.s1d2_sort.comparison)                      // IsLT | IsGT
    && sort_valid_stable(i.s1d2_sort.stable)                         // 0 | 1
    && sort_tensor_shapes_ok(i)
}
```

### 4.1 The dtype gate (`sort_dtype_ok = is_16b OR is_32b; in == out`)

| sort dtype | enum | accepted | index dtype | firmware compare leg | class |
|---|---|---|---|---|---|
| `FP16` | `0x7` | Y | u16 | `ivp_oltnxf16t`/`ivp_ultnxf16t` (ordered) | 16-b fp |
| `BFLOAT16` | `0x6` | Y | u16 | `ivp_oltnxf16t`/`ivp_ultnxf16t` (ordered) | 16-b fp |
| `UINT16` | `0x5` | Y | u16 | `ivp_lt2nx8`/`ivp_bminunx16`/`ivp_bmaxunx16` | 16-b uint |
| `INT16` | `0x4` | Y | u16 | `ivp_lt2nx8` (signed) / `ivp_bminnx16`/`ivp_bmaxnx16` | 16-b sint |
| `FP32` | `0xA` | Y | u32 | `ivp_oltn_2xf32t` (ordered) | 32-b fp |
| `UINT32` | `0x9` | Y | u32 | `ivp_bminun_2x32`/`ivp_bmaxun_2x32` | 32-b uint |
| `INT32` | `0x8` | Y | u32 | `ivp_bminn_2x32`/`ivp_bmaxn_2x32` | 32-b sint |
| `FP32R` | `0xB` | **N** | — | rejected (not a 16/32-b sort dtype) | — |
| `FP8_E3/E4/E5` | — | **N** | — | rejected | — |
| `INT8`/`UINT8` | — | **N** (today) | — | header note: "input dtype *can be extended* to 8-bit types if need be" — not in the v0.21.2.0 gate | — |
| `INT64`/`UINT64` | — | **N** | — | rejected (64-b) | — |
| `FP4`/`INT4`/`SFP8` (MAVERICK MX) | — | **N** | — | rejected — MX dtypes do not widen the gate | — |

The gate is two `OR`-ed predicates, read verbatim from the header:

```c
fn sort_dtype_is_16b(d) -> bool { d==FP16 || d==BFLOAT16 || d==UINT16 || d==INT16 }
fn sort_dtype_is_32b(d) -> bool { d==FP32 || d==UINT32   || d==INT32  }
fn sort_dtype_ok(d)     -> bool { sort_dtype_is_16b(d) || sort_dtype_is_32b(d) }
fn sort_dtypes_ok(i)    -> bool { sort_dtype_ok(lo(i)) && lo(i)==hi(i) }
```

Any dtype that fails the gate triggers the device error path:
`"P%i: Error, unsupported data type for sort : 0x%x"`, logging the rejected
`in_dtype` byte. `[HIGH/OBSERVED — gate read verbatim; the per-class compare op OBSERVED
in the POOL IRAM primitive census; the u16/u32 index split OBSERVED as the `nx16t`-vs-
`_2x32t` gather/dsel duality — 161 `nx16t` vs 42 `_2x32t` forms in the CAYMAN Q7 POOL
IRAM]`

> **NOTE — the index dtype is implicit and always integer, sized to the output.** The
> header is explicit: *"Indices are necessarily the same size as the output dtype, but
> are always integers, regardless of output dtype: for bf16, u16, etc dtype, the indices
> in Y[1] are u16; for i32, f32, etc, the indices are u32."* There is no field for the
> index dtype — it is fixed at u16 for any 16-b sort and u32 for any 32-b sort. This is
> exactly the `ivp_dselnx16t` (16-b index permute) vs `ivp_dseln_2x32t` (32-b index
> permute) split observed in the firmware. `[HIGH/OBSERVED]`

### 4.2 Direction

`comparison == IsLT (0x16)` → **ASCENDING** (smallest first, a min-first
compare-exchange). `comparison == IsGT (0x13)` → **DESCENDING** (largest first, a
max-first compare-exchange). These are the *only* two legal AluOps; the integer-typed
variants (`IS_GT_INT = 0xD1`, `IS_LT_INT = 0xD4`, `IS_GT_UINT = 0xD7`, `IS_LT_UINT =
0xDA`) exist in the AluOp enum but are **rejected** by `sort_valid_aluop`, which admits
only the plain `IsLT`/`IsGT`. The signedness for integer sorts is carried instead by the
chosen compare/min/max *primitive* (`ivp_lt2nx8` signed vs `ivp_bminunx16` unsigned, §5).
In the firmware the asc/desc choice is the **control bit of the fused `ivp_minormax2nx8`
min-OR-max select** (one op picks min vs max). `is_ascending` in the decode log = the
`IsLT` case. `[HIGH/OBSERVED — the two legal AluOps + the minormax primitive]`

### 4.3 Stable

`stable == 1` preserves equal elements' relative order; `stable == 0` "might be faster"
(header). Because every sorted value carries its original index, stability is *observable*
in `dst[:,1]`: for a stable sort, ties resolve in increasing original-index order (the
index acting as a secondary key). `[HIGH/OBSERVED for the flag + the semantics; the
firmware's stable-vs-unstable code split is MED through the FLIX desync]`

### 4.4 FP ordering / NaN

The fp comparisons are the **ordered** forms (`ivp_oltn_2xf32t`, `ivp_oltnxf16t`),
backed by the NaN-suppressing relative-bounded min/max (`ivp_rbminnumnxf16t` /
`ivp_rbmaxnumnxf16t` — the `num` = IEEE `minNum`/`maxNum`, which return the non-NaN
operand). So NaN handling follows the GPSIMD ordered-compare convention: an ordered
compare returns false on NaN, and `minNum`/`maxNum` quiet NaN. The exact placement of NaN
in the final order is NaN-suppressed by the `num`-min/max, but the precise position
through the desync is MED. `[compare ops HIGH/OBSERVED; NaN order MED]`

### 4.5 Tensor shapes and the size bound

```c
fn sort_tensor_shapes_ok(i) -> bool {
       tensor1d_valid(src, lo(i), WriteTensor::False, AllowedInPSUM::False, AllowedInSBUF::True)
    && tensor2d_valid(dst, hi(i), WriteTensor::True,  AllowedInPSUM::False, AllowedInSBUF::True)
    && src.num_elem[0] == dst.num_elem[1]          // N values  <->  N Y-slots
    && dst.num_elem[0] == 2                         // the {value, index} pair
    && (   (sort_dtype_is_16b(lo(i)) && src.num_elem[0] * num_active_channels <= SORT_MAX_SIZE/2)   // <= 4096
        || (sort_dtype_is_32b(lo(i)) && src.num_elem[0] * num_active_channels <= SORT_MAX_SIZE/4))  // <= 2048
}
```

- `src` is a read-only `TENSOR1D` in **SBUF only** (never PSUM).
- `dst` is a write `TENSOR2D` in **SBUF only**, with `num_elem[0] == 2` (the X dimension =
  the two channels `{value, index}`) and `num_elem[1] == N` (the Y dimension = the N
  sorted positions).
- **Size bound:** `SORT_MAX_SIZE = 8192 B` of Pool working RAM. 16-b dtype ⇒
  `N × num_active_channels ≤ 4096` elements; 32-b dtype ⇒ `≤ 2048`. Larger tensors must be
  tiled and (host-side / future-SortMerge) merged.

`[HIGH/OBSERVED]`

> **GOTCHA — the shapes predicate forbids the very top-K the doc-comment advertises.**
> `sort_tensor_shapes_ok` *hard-requires* `src.num_elem[0] == dst.num_elem[1]` (a full
> sort: N inputs → N outputs). But the same header's options note says the output "can
> optionally be smaller along the Y dimension than the input tensor… useful to implement
> top-K." These two read literally contradict each other. The reconciliation: the *strict
> validity predicate* models the full-sort case; the **top-K mode relaxes** `dst.Y < src.N`
> as a documented capability that the firmware honours but the conservative
> `is_valid_s1d2_sort` does not encode (a common pattern where the host-side static
> validator is stricter than the device). When emitting a top-K, expect the host
> assembler to either special-case the shape check or pad. The firmware short-Y early-exit
> itself is MED through the desync. `[predicate HIGH/OBSERVED; top-K relaxation
> HIGH/OBSERVED from the header note; their reconciliation MED/INFERRED]`

---

## 5. The Sort algorithm — in-register SIMD compare-exchange network

### 5.1 The output contract (worked example from the header)

```
src          = [ 9, 3, 5, 1, 0, 0 ]
comparison   = IsLT (ascending)
index_offset = 100

dst[:,0] (sorted VALUES)       = [   0,   0,   1,   3,   5,   9 ]
dst[:,1] (orig INDICES+offset) = [ 104, 105, 103, 101, 102, 100 ]
```

Read it as: the smallest value `0` came from src position 4 (→ `100 + 4 = 104`); the next
`0` from position 5 (→ `105`); the `1` from position 3 (→ `103`); …; the largest `9` from
position 0 (→ `100`). The index column is the **arg-sort permutation** offset by
`index_offset`. This is the **full-ordering generalization** of
[SparsityCompress](./sparsity-compress-tag.md)'s top-4 `{value, tag}` reduction: Sort
keeps *all* N elements and *all* N indices, totally ordered. `[HIGH/OBSERVED — header
verbatim]`

### 5.2 Network type

The body is a **data-parallel compare-exchange sorting network** (the bitonic /
odd-even-merge family — fixed compare-exchange stages over lane pairs), *not* a scalar
streaming insertion sort and *not* a single top-K fold. Evidence for the network family
(vs insertion/selection):

- The body is dominated by **vector lane-permute + min/max** ops over whole 512-bit
  vectors (`ivp_sel2nx8i`/`ivp_sel2nx8i_s4` lane-pairing, `ivp_dselnx16t`/`ivp_dseln_2x32t`
  compare-exchange swap, `ivp_minormax2nx8` min/max), not a scalar compare loop — the
  fingerprint of a compare-exchange *network*. The SUNDA NX POOL body alone shows **387**
  such primitive hits clustered contiguously in the `0x1000–0x6000` region (§7).
- The **immediate-selector** permute forms (`ivp_sel2nx8i_s4`, the `s4` = a fixed permute
  pattern) implement the **static per-stage lane-pairing** of a sorting network (each
  stage compares lane *i* with lane *i ± 2^k*). An immediate (not dynamic) selector ⇒ a
  *static, data-oblivious* schedule — the hallmark of bitonic / odd-even-merge.
- The `SORT_MAX_SIZE` (fit-in-Pool-RAM) bound + the "tile across partitions, then
  SortMerge" model is the classic **blocked sorting-network-per-block + merge**
  architecture.
- The header's "up to 7.5× faster" parallel-partition claim and the "unstable may be
  faster" note are both consistent with a fixed (data-oblivious) network.

`[primitives HIGH/OBSERVED; the precise network (bitonic vs odd-even-merge vs Batcher)
MED/INFERRED — the compare-exchange ATOM is byte-OBSERVED; the stage WIRING is inferred
through the FLIX desync]`

### 5.3 The compare-exchange primitive (the network's atom)

Byte-coherent FLIX bundles observed in the carved POOL IRAM (CAYMAN Q7 POOL DEBUG;
addresses are file offsets in the carved blob):

```
@0xceac : { … ; ivp_lvn_2x16s_i v9,a8,0x1c40 ;
            ivp_oltn_2xf32t vb0,v3,v24,vb0 ;
            ivp_dselnx16t   v31,v1,v17,v9,v20,vb0 }
        ; LOAD + ORDERED-fp32 COMPARE -> vbool vb0 + DUAL-SELECT swap.
        ; vb0 marks which lanes are out of order; dselnx16t routes v1/v17 into the
        ; sorted min/max lanes v31 while carrying the v9/v20 pair — value AND index.

@0x1514b: { … ; ivp_labvdcmprs2nx8_xp vb0,v20,u0,a4,a8,0 ;
            ivp_oltn_2xf32t vb2,v20,v24,vb8 ;
            ivp_dselnx16t   v24,v20,v1,v1,v0,vb2 }
        ; STREAM-LOAD-COMPRESS + ORDERED COMPARE -> vb2 + DUAL-SELECT swap.

@0x463b / 0x468f : labvdcmprs + ivp_oltnxf16t (fp16 ordered compare) + dselnx16t
        ; the fp16/bf16 leg of the same compare-exchange.

@0x17af8: { … ; ivp_minormax2nx8 v8,v0,v14,vb6 ;
            ivp_bmax2nx8     vb11,v15,v7,v2 }
        ; FUSED min-OR-max select (vb6 = the asc/desc direction control bit).
```

So the compare-exchange atom is: **COMPARE** (ordered-fp / signed-int → `vbool`) **THEN**
`dsel`/`minormax` **SWAP**, with the index vector permuted by the *same* `vbool`. The
per-stage lane permutation is the `ivp_sel2nx8i_s4` immediate pattern (the fixed network
wiring). `[HIGH/OBSERVED — the compare + dsel + minormax co-bundling]`

In annotated C pseudocode, naming the real recovered IVP intrinsics:

```c
// One compare-exchange stage of the GPSIMD Sort network (POOL "decode_sort" body).
// 'lo'/'hi' are the two lane-groups paired by this stage (selected by an immediate
// permute, ivp_sel2nx8i_s4); val_* carry the keys, idx_* the original-position payload.
// 'ascending' = (comparison == IsLT). dtype16 selects the u16 vs u32 index machinery.
static void sort_compare_exchange_stage(
        vec   *val_lo, vec   *val_hi,    // the values being sorted
        vidx  *idx_lo, vidx  *idx_hi,    // the index payload (u16 or u32, riding along)
        int    ascending, int dtype16)
{
    // 1. COMPARE -> vbool: ordered for fp (NaN-suppressing), signed for int.
    vbool swap;
    if      (FP)   swap = ivp_oltn_2xf32t(*val_lo, *val_hi);  // or ivp_oltnxf16t (fp16/bf16)
    else  /* INT */swap = ivp_lt2nx8     (*val_lo, *val_hi);  // sign-bias trick for signed

    // 2. For DESCENDING (IsGT) the comparator is inverted; in firmware this is the
    //    control bit of the fused min-OR-max select, not a separate op.
    //    ivp_minormax2nx8(min_or_max_select = !ascending) realises both directions.

    // 3. EXCHANGE the VALUES with a DUAL-OUTPUT predicate select: one op emits BOTH
    //    the min-lane and the max-lane result of the compare-exchange.
    dsel_pair v = (dtype16) ? ivp_dselnx16t (*val_lo, *val_hi, swap)
                            : ivp_dseln_2x32t(*val_lo, *val_hi, swap);
    *val_lo = v.min_lane;  *val_hi = v.max_lane;

    // 4. PERMUTE the INDEX payload by the SAME vbool, with the width-matched dsel:
    //    u16 indices use the nx16t form, u32 indices the _2x32t form (parallel key+value).
    dsel_pair i = (dtype16) ? ivp_dselnx16t (*idx_lo, *idx_hi, swap)
                            : ivp_dseln_2x32t(*idx_lo, *idx_hi, swap);
    *idx_lo = i.min_lane;  *idx_hi = i.max_lane;
}
```

`[network family MED/INFERRED; the compare→dsel→minormax atom + the dual index permute
HIGH/OBSERVED]`

### 5.4 The key/value (index) handling — parallel permutation of the payload

The index payload is permuted by the *same* `vbool` as the values, in lock-step. Observed
gather + dsel co-bundling (POOL IRAM):

```
@0x1435e: { ivp_gatheran_2x32t gr4,a1,v0,vb4 ; nop ; nop ;
            ivp_dseln_2x32t v17,v11,v6,v24,v7,vb7 }      ; 32-bit index permute (u32)
@0x14ebb: { ivp_gatheranx16t   gr7,a9,v24,vb3 ; nop ; nop ;
            ivp_dselnx16t … }                            ; 16-bit index permute (u16)
@0x1b1df: { ivp_gatheranx8ut   gr5,a4,v1,vb0 ; nop ; nop ;
            ivp_dselnx16t … }
```

For every compare-exchange that swaps two **values**, the corresponding **index** vector
is permuted by the same `vbool` (`ivp_dselnx16t` for u16 indices, `ivp_dseln_2x32t` for
u32 indices; the `gather*` stages assemble the index iota / scatter the result). The
indices originate as an **iota** (positions `0..N-1`, then `+ index_offset`) and ride the
network as the value's tag — a key+VALUE sort. The width duality is direct binary
evidence of the header's u16/u32 index rule: **161 `nx16t` vs 42 `_2x32t`** dsel/gather
forms in the CAYMAN Q7 POOL IRAM. `[HIGH/OBSERVED]`

The index-iota seed in pseudocode:

```c
// Index payload initialization for partition p of a Sort (the arg-sort seed).
// Each lane k gets its own original position; index_offset (struct @36) globalizes it.
// Delivered per index_offset_src (@13): inline imm / SBUF ptr / reg ptr.
static void sort_seed_indices(vidx *idx, int N, uint32_t index_offset, int dtype16)
{
    // ivp_seqnx16 / iota -> 0,1,2,...,N-1 across the lanes, then add the offset.
    for (int k = 0; k < N; ++k)
        idx[k] = (vidx)(k + index_offset);     // u16 for 16-b sort, u32 for 32-b sort
    // From here on idx[] is permuted by every sort_compare_exchange_stage() in parallel
    // with the values, so the final idx[k] = original position of the k-th sorted value.
}
```

`[index-iota init MED/INFERRED through the desync; the gather/dsel index-carry
HIGH/OBSERVED]`

### 5.5 The streaming / working-RAM model

`ivp_labvdcmprs2nx8_xp` (load-align + vector-compress, post-increment) streams the `src`
`TENSOR1D` into Pool working RAM (the `SORT_MAX_SIZE`-bounded buffer); the network runs
in-RAM; the sorted `{value, index}` pairs are written to the `dst` `TENSOR2D`. The 8 KiB
Pool-RAM bound and the tile-and-merge model are the blocked-network architecture.
`[header HIGH; body OBSERVED]`

### 5.6 Per-dtype compare leg (the min/max op family, observed census)

| dtype | compare → vbool | min/max family |
|---|---|---|
| fp16/bf16 | `ivp_oltnxf16t` / `ivp_ultnxf16t` (ordered) | `ivp_rbminnumnxf16t`/`ivp_rbmaxnumnxf16t` (NaN-suppress), `ivp_minnxf16t`/`ivp_maxnxf16t` |
| fp32 | `ivp_oltn_2xf32t` (ordered) | `ivp_minn_2xf32t`/`ivp_maxn_2xf32t`, `ivp_rbminnumn_2xf32t` |
| int16 | `ivp_lt2nx8` (signed) | `ivp_bmaxnx16`/`ivp_bminnx16`/`ivp_bmin2nx8`/`ivp_bmax2nx8` |
| uint16 | `ivp_lt2nx8` (unsigned legs) | `ivp_bmaxunx16`/`ivp_bminunx16`/`ivp_maxunx16t` |
| int32 | `ivp_lt2nx8`-derived | `ivp_bmaxn_2x32`/`ivp_bminn_2x32` |
| uint32 | `ivp_lt2nx8`-derived | `ivp_bmaxun_2x32`/`ivp_bminun_2x32` |

The asc/desc direction selects min-first vs max-first via the `ivp_minormax2nx8` control
bit (= `comparison` `IsLT`/`IsGT`). All present in **both** the CAYMAN Q7/NX POOL and the
SUNDA NX POOL IRAMs. `[HIGH/OBSERVED — census]`

---

## 6. Top-K and SortMerge — the two scaling mechanisms

**6a. Top-K (a built-in degenerate case).** Per the header: *"The output tensor can
optionally be smaller along the Y dimension than the input tensor. This is useful to
implement top-K, if descending sort is specified."* With `comparison = IsGT` (descending)
and `dst.num_elem[1] = K < N`, Sort emits only the **K largest** `{value, index}` pairs —
so Sort *subsumes* top-K and, via the index payload, top-K arg-max. (Relation to
[SparsityCompress](./sparsity-compress-tag.md): that op is a *fixed* top-4 reduction on a
different engine, DVE; Sort's top-K is a short-output full sort on POOL, totally ordered.)
The exact short-Y firmware early-exit is MED through the desync; the capability itself is
HIGH/OBSERVED from the header. `[HIGH/OBSERVED header]`

**6b. SortMerge — the phantom companion (see the §1 CORRECTION).** The header repeatedly
references a `SortMerge` instruction to merge separately-sorted subtensors and to merge
across partitions. **It is not shipped.** `common.h:231` marks it `// SortMerge wip
0x97`: the `0x97` opcode slot is a *work-in-progress comment only* (not an enumerator),
and `0x98` was reassigned to `TENSOR_SCALAR_SELECT`. There is no `SortMerge` struct, no
`SortMerge` opcode value in the enum, and no `SortMerge` DEBUG string in any carved image.
In v0.21.2.0, large-tensor merging is host-side or future; only the single-block Sort
(`0x96`) ships. `[HIGH/OBSERVED — the "0x97 wip" comment + the absent
struct/opcode/string]`

---

## 7. Per-generation presence

| gen | opcode `0x96` | `s1d2_sort.h` | struct / validity | `SORT_MAX_SIZE` | firmware body |
|---|---|---|---|---|---|
| **SUNDA (v2)** | present | present | identical | 8192 | **NX POOL RELEASE body-deep** (387 primitive hits); Q7 POOL = minimal stub (4 hits) |
| **CAYMAN (v3)** | present | present | identical | 8192 | NX/Q7 POOL DEBUG; `"S: Sort"` + `decode_sort` + body |
| **MARIANA (v4)** | present | present | identical | 8192 | NX/Q7 POOL; `"S: Sort"`/`decode_sort` |
| **MAVERICK (v5)** | present | present | identical | 8192 | header-grounded; Q7 POOL IRAM stripped (size 0); NX POOL present |

- The Sort header has **no nc-version gate** (no `has_valid_nc_sort` / `check_core_version`
  clause) — *unlike* [NonzeroWithCount](./nonzero-with-count.md) (`nc >= V3`) and
  [SparsityCompress](./sparsity-compress-tag.md) (V4). So Sort is available on **all gens
  from SUNDA (NC-v2) up**.
- The struct + the full `is_valid_s1d2_sort` predicate + opcode `0x96` + `SORT_MAX_SIZE`
  are **byte-identical** across all four gens (the only diff between the cayman and sunda
  headers is the NC-version banner comment `NC-v3`→`NC-v2` and one word
  `Cayman`→`Neuron`). No MAVERICK rename. MAVERICK MX dtypes (`FP4`/`INT4`/`SFP8`) are
  **not** admitted (the gate stays 16/32-b).

⇒ Sort is a **universal (all-gen) POOL instruction**, older than the codec ops added in
later generations. It is *not* a MARIANA or CAYMAN addition. `[HIGH/OBSERVED — the header
absence of an nc clause + the struct/opcode present in the sunda arch_isa + the sunda POOL
firmware primitive census]`

> **NOTE — the SUNDA body-depth question (the FW-47 / IMG-23 residual), resolved.**
> Opcode `0x96` SORT is defined in the **SUNDA ISA header set**
> (`neuron_sunda_arch_isa/.../s1d2_sort.h`, compile-verified `sizeof == 64`,
> byte-identical to cayman). The separate question — *does the SUNDA POOL firmware
> **image** ship a Sort handler BODY, or only the header definition?* — is now answered
> from the carved SUNDA POOL image: the **SUNDA NX_POOL RELEASE IRAM does carry a real
> Sort compare-exchange body.** A primitive census of the carved blob
> (`SUNDA_NX_POOL_RELEASE_IRAM_get` → file `0x2be10`, size `0xd040`) finds the full
> compare-exchange vocabulary (`ivp_dselnx16t` ×120, `ivp_sel2nx8i` ×196, `ivp_bmaxnx16`
> ×34, `ivp_rbminnumnxf16t` ×29, plus `ivp_dseln_2x32t`/`ivp_labvdcmprs2nx8_xp`/
> `ivp_oltnxf16t`) — **387** total primitive hits, *denser* than CAYMAN NX POOL's 111 —
> packed into valid, contiguous FLIX bundles in the `0x1000–0x6000` region, the same
> compare-exchange networks seen in CAYMAN's Sort region. The `decode_sort`/`"S: Sort"`
> DEBUG strings are **absent** from SUNDA POOL DRAM only because SUNDA ships **RELEASE**
> builds (DEBUG strings stripped) — that absence is *expected*, not a missing body. So
> the SUNDA presence is **body-deep, not merely header-deep**. The substantive body is in
> the **NX** POOL image; the **SUNDA Q7 POOL IRAM is a tiny stub**
> (`SUNDA_Q7_POOL_RELEASE_IRAM_get` → `0x48bf0`, size `0x42d0`, only 4 primitive hits), so
> a Q7-specific SUNDA "body present" claim would be LOW. `[NX body presence HIGH/OBSERVED
> — primitive census; the DEBUG-string absence expected; the Q7 stub OBSERVED]`

---

## 8. The dispatch chain — opcode → `decode_sort` → dtype-gated body

The decode-time path:

```
opcode 0x96  (header.opcode == NEURON_ISA_TPB_OPCODE_SORT)
   |
   v
dbg_is_valid_s1d2_sort(i)        (debug_assert.h case OPCODE_SORT)
   |   header, events, sort_dtypes_ok, valid ImmSrc,
   |   sort_valid_aluop (IsLT/IsGT), sort_valid_stable (0/1), sort_tensor_shapes_ok
   v
POOL ASCII-opcode dispatcher  ->  decode_sort  (the on-device entry)
   |
   (1) log "P%i: DECODE_SORT : num_items=%d, num_active_channels=%d, is_ascending=%d"
   (2) GATE in_dtype: sort_dtype_is_16b OR sort_dtype_is_32b;
       else log "P%i: Error, unsupported data type for sort : 0x%x" and abort
   (3) read comparison -> min/max direction selector; read stable
   (4) run the compare-exchange network (§5), emitting {values, indices}
```

The three printed fields of the `DECODE_SORT` log are the **live operand readout**:
`num_items` (= src `N`), `num_active_channels` (struct @12), `is_ascending` (=
`comparison == IsLT`). The error path logs the rejected `in_dtype` byte. `[STRUCTURAL
HIGH]`

In pseudocode (the decode entry, naming the real on-device symbol `decode_sort` and the
real log strings):

```c
// On-device POOL kernel entry, self-named "decode_sort". Reached from the POOL
// ASCII-opcode dispatcher for opcode 0x96. (funcVA is descriptor-driven — see GOTCHA.)
void decode_sort(const NEURON_ISA_TPB_S1D2_SORT_STRUCT *i)
{
    uint32_t N        = i->src_mem_pattern.num_elem[0];
    uint8_t  chans    = i->num_active_channels;             // 1..128
    int      is_asc   = (i->comparison == IS_LT);           // IsLT(0x16) vs IsGT(0x13)
    dtype_t  in_dt    = i->in_out_dtype.dtype_lo;

    // (1) the decode log — the three live fields:
    log("P%i: DECODE_SORT : num_items = %d, num_active_channels = %d, is_ascending = %d",
        partition, N, chans, is_asc);

    // (2) dtype gate: only 16-b or 32-b sort dtypes; in must equal out (checked at decode).
    if (!(sort_dtype_is_16b(in_dt) || sort_dtype_is_32b(in_dt))) {
        log("P%i: Error, unsupported data type for sort : 0x%x", partition, in_dt);
        return;                                             // abort
    }
    int dtype16   = sort_dtype_is_16b(in_dt);               // selects u16 vs u32 index path
    uint32_t off  = read_index_offset(i->index_offset, i->index_offset_src);

    // (3)+(4): seed the index iota, stream src into Pool RAM, run the network.
    sort_seed_indices(idx_buf, N, off, dtype16);
    sort_network(val_buf, idx_buf, N, is_asc, i->stable, dtype16);   // §5 compare-exchange net
    write_dst_2d(i->dst_mem_pattern, val_buf, idx_buf);    // dst[:,0]=values  dst[:,1]=indices
}
```

`[STRUCTURAL HIGH — opcode/struct/binding/strings; the body shape from the OBSERVED
primitive census; the exact funcVA MED — see GOTCHA]`

> **GOTCHA — the `decode_sort` funcVA is not byte-pinned; the dispatch is structural.**
> The POOL `kernel_info` table carries **no** FW-18-format `{0,0,spec,0x96,funcVA}` record
> (byte-scanned all three carved POOL IRAMs — none found), and the POOL DEBUG IRAM/DRAM
> linear sweep **FLIX-desyncs**: the format strings are *not* whole-string `l32r`'d (a
> brute-force base search over the `"P%i:"` strings found no coherent DRAM-string→IRAM-
> literal base), so the string→code xref is a **descriptor barrier**, not a clean literal
> load. The IVP slots *within* each FLIX bundle are real (and are the basis for the §5
> census and the cited coherent compare-exchange bundles), but the surrounding control-flow
> / branch targets in the desynced sweep are **not** trustworthy and are not reported as
> real. The dispatch is therefore pinned at the **structural** level — opcode `0x96` from
> `common.h` + the `instruction_mapping.json` binding + the `debug_assert.h` case + the
> named `decode_sort`/`DECODE_SORT`/error strings present in the POOL image + the in-image
> compare-exchange body — *not* to a single byte-exact funcVA jump. This is the standing
> GPSIMD POOL FLIX-desync + descriptor-dispatch limitation. `[opcode + names + body
> presence HIGH/OBSERVED; the exact funcVA + the 0x96-indexed jump-table entry MED]`

---

## 9. Relation to the sibling codec / compaction kernels

| | engine | opcode | scope | output | index payload | nc gate |
|---|---|---|---|---|---|---|
| **Sort** | POOL | `0x96` | per-partition, full total order | `{sorted values, original indices}`, in == out dtype | u16/u32, every element | none (all gens) |
| [NonzeroWithCount](./nonzero-with-count.md) | POOL | (codec) | per-partition compaction | nonzero **positions** + a **count**, INT32 out | INT32, positions only | `nc >= V3` |
| [SparsityCompress](./sparsity-compress-tag.md) | **DVE** | (codec) | fixed top-4 reduction per group | top-4 `{value, tag}` | within-vector tag | V4 |

- **vs NonzeroWithCount (same POOL engine).** Both compose from the same lane-permute +
  min/max primitive set (`ivp_dselnx16t`/`ivp_sel2nx8i`/`ivp_sel2nx8i_s4` + the `_2x32`
  32-b index machinery + `ivp_lt2nx8`/ordered-fp compare) and both carry an index payload
  via the `gather + dsel` pattern. NonzeroWithCount *compacts* (a predicated cursor over
  positions, INT32-only output); Sort additionally *totally orders* (the compare-exchange
  network) and emits both the value and the index in the input dtype. `[HIGH/OBSERVED]`
- **vs SparsityCompress (DVE engine).** They share the `{value, index/tag}` pairing and
  the compare→vbool + select + min/max core, but SparsityCompress is a *fixed top-4
  reduction* on the DVE engine (`engine_idx = 3`, MARIANA+), whereas Sort is a *full total
  order* on POOL (`0x96`, all gens). Sort's top-K mode (§6a) is the closest analog to
  SparsityCompress's bounded reduction — but on POOL and totally ordered. `[HIGH/OBSERVED]`
- **vs the select/shuffle ISA batch.** The Sort compare-exchange *is* the
  [select/shuffle](../../isa/ref/b21-select-shuffle.md) model exactly: COMPARE = an
  ordered-fp / signed-int compare → `vbool`; EXCHANGE = the dual-output two-source lane
  SELECT (`ivp_dselnx16t`/`ivp_dseln_2x32t`) gated by that `vbool`, with the immediate
  `ivp_sel2nx8i_s4` as the fixed network lane-pairing; MIN/MAX = the bounded
  `bmin`/`bmax`/`minormax2nx8` family; INDEX = the `gather*` machinery carrying the
  position payload through the same permutation. `[HIGH/OBSERVED — the bundle composition
  matches the select/shuffle model at the mnemonic level]`

---

## 10. Honesty ledger

**HIGH / OBSERVED** (direct disasm / byte / symtab / header / compile this session):

- `OPCODE_SORT = 0x96`; struct `NEURON_ISA_TPB_S1D2_SORT_STRUCT` (64 B);
  `instruction_mapping.json` binding (`S1D2_SORT → SORT`); `debug_assert.h` case
  (`0x96 → dbg_is_valid_s1d2_sort`) — **compile-verified all four gens** (`sizeof == 64`;
  offsets `header@0, events@4, num_active_channels@12, index_offset_src@13, in_out_dtype@14,
  comparison@15, src@16, dst@24, index_offset@36, stable@40, reserved1@41`).
- The 1-D-src / 2-D-dst `{value, index}` layout + the worked example; `in == out` dtype
  rule; the implicit u16/u32 index dtype; `index_offset` + the 3-mode `IMM_SRC`;
  `comparison` restricted to `IsLT (0x16 asc)`/`IsGT (0x13 desc)`; `stable` 0/1; the
  16/32-b dtype gate; `SORT_MAX_SIZE = 8192` + the size bounds — read verbatim (all four
  gens identical bar the NC banner).
- Engine = POOL: the "Pool working RAM" statement + the `decode_sort`/`"S: Sort"`/
  `"DECODE_SORT … is_ascending"`/`"unsupported data type for sort"` strings present *only*
  in the POOL images (28 hits in the binary), carved via the
  `<ARCH>_<NX|Q7>_POOL_*_get` accessors; sha256 `b7c67e89…` reproduced in-task.
- The compare-exchange primitive set in the POOL IRAM with byte-coherent FLIX bundles:
  ordered-fp / signed-int compare → `vbool` + dual-select swap + fused min-or-max + bounded
  min/max + immediate lane-pairing + index gather + stream load-compress; the 16-b
  (`nx16t`, 161×) vs 32-b (`_2x32t`, 42×) index-width split (CAYMAN Q7 POOL).
- Per-gen: present all four (SUNDA NC-v2 .. MAVERICK NC-v5); no nc gate; byte-identical
  struct/validity/opcode/`SORT_MAX_SIZE`; no MAVERICK rename; MX not admitted; **SUNDA NX
  POOL IRAM body-deep (387-hit primitive census, > CAYMAN NX's 111)**.
- `SortMerge = "wip 0x97"` (comment only in `common.h`), `0x98` reassigned; no SortMerge
  struct/opcode/string anywhere — the **phantom**.

**MED / INFERRED:**

- The `decode_sort` funcVA + the exact `0x96`-indexed jump-table entry (no FW-18 record;
  POOL FLIX desync + descriptor barrier) — pinned *structurally*, not byte-exact.
- The network **family** (bitonic / odd-even-merge / Batcher) — inferred from the fixed
  immediate lane-pairing + compare-exchange + blocked `SORT_MAX_SIZE` model; the stage
  wiring is not a resynced trace (the compare-exchange *atom* is byte-OBSERVED).
- The stable-vs-unstable code split; the short-Y top-K early-exit; the NaN order placement;
  the index-iota init — through the FLIX desync.
- The per-class min/max op attribution to a single handler arm (the IVP set is OBSERVED in
  the image; the per-dtype-leg split is INFERRED).
- The reconciliation of `sort_tensor_shapes_ok` (`src.N == dst.Y`) with the header's
  top-K (`dst.Y < src.N`) — a documented capability the strict validator does not encode.

**LOW / NOT CLAIMED:**

- The MAVERICK firmware-body bytes (Q7 POOL IRAM is a size-0 stripped placeholder; MAVERICK
  rests on the compile-verified header + cross-gen invariance — **v5 interiors INFERRED**).
- The SUNDA **Q7** POOL Sort body (the Q7 image is a 4-hit minimal stub — the body lives in
  the SUNDA NX image; a Q7-specific SUNDA body claim is LOW).
- The 8-bit "extended" input dtype path (header says "can be extended if need be" — not in
  the v0.21.2.0 `sort_dtype_ok` gate; future/optional).
- Any host / NKI API surface for Sort (the host x86 `.so` holds only the firmware-image
  accessors; no `at::Tensor`/NKI sort op surfaced in this package).
- The exact device lane-pairing tree / per-stage FLIX-slot schedule of the network.

---

## Do-not-repeat ledger

| claim | status | evidence |
|---|---|---|
| **`SortMerge` (`0x97`) is a real opcode** | **FALSE — phantom** | `common.h:231` = `// SortMerge wip 0x97`; `0x97` is not an enumerator; `0x98` reassigned to `TENSOR_SCALAR_SELECT`; no SortMerge struct/opcode/string in any image. Do not "discover" `0x97`. |
| **`SortMerge` is Sort / is shipped** | **FALSE** | `SortMerge` is `wip`, never shipped in v0.21.2.0; Sort (`0x96`) is the only shipped instruction. |
| Sort is a composed host helper (like NonzeroWithCount) | FALSE | Sort is a first-class named ISA op: own opcode `0x96`, own 64-B struct, own validity predicate, own self-named `decode_sort` kernel. |
| Sort is a DVE op | FALSE | Sort is POOL; the strings live only in POOL images; SparsityCompress is the DVE op. |
| Sort is MARIANA/CAYMAN-only (or nc-gated) | FALSE | No nc gate; present byte-identical on all four gens incl. SUNDA NC-v2 (NX POOL body-deep, 387-hit census). |
| SUNDA defines `0x96` in the header but ships no body | FALSE | SUNDA NX POOL RELEASE IRAM carries the full compare-exchange body (387 primitive hits); DEBUG strings absent only because RELEASE strips them. |
| Index dtype is configurable | FALSE | Index dtype is implicit: u16 for any 16-b sort, u32 for any 32-b sort; no field. |
| Sort converts dtype | FALSE | `sort_dtypes_ok` requires `in == out`. |
