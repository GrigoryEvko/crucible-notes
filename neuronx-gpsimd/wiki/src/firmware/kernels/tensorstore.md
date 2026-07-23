# TensorStore (the sequencer register/immediate → memory scatter, `0xab`)

> **Scope.** TensorStore (`NEURON_ISA_TPB_OPCODE_TENSOR_STORE == 0xab`, `mem_2d` format) is the iTPB
> **sequencer**'s scalar STORE primitive: it pushes up to 32 values **from sequencer registers
> *or* from in-instruction immediates** out to a 2-D strided window of the Neuron Address Space. It is
> the exact mirror of [TensorLoad](tensorload.md) (`0xaa`) — the two share **one** 64-byte operand
> struct (`NEURON_ISA_TPB_MEM_2D_STRUCT`) and **one** spec file (`aws_neuron_isa_tpb_mem_2d.h`). This
> page decodes the struct (compile-verified `sizeof == 64`, byte-identical to TensorLoad); proves the
> **`mem_2d` 2-D strided** addressing; documents the **one thing TensorStore has that TensorLoad does
> not** — the **immediate-source store path** (`src_datasrc == Immediate`, packed typed immediates
> overlaying the 32-byte `data[]` field, with per-dtype element caps); pins the **dtype-truncate on
> store** (wide→narrow, the reverse of TensorLoad's zero/sign-extend); enumerates the destination
> memory windows (SBUF / PSUM / dataram / DRAM / HBM via the dynamic 16 MB window); and proves
> per-gen presence SUNDA(NC-v2) → MAVERICK(NC-v5).
>
> Confidence tags use the `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` model defined in
> [`../../reference/confidence-model.md`](../../reference/confidence-model.md). All device-firmware
> facts derive from static analysis of shipped binaries and the shipped public ISA C headers; no
> source was consulted.

---

## 1. TL;DR — the pinned facts

| # | Fact | Evidence | Tag |
|---|------|----------|-----|
| 1 | TensorStore = opcode **`0xab`**, **maintained** (`// Y`) in **all four** shipped gens. | `common.h` enum: sunda L254 / cayman L251 / mariana L256 / maverick L259, each `= 0xab, // Y` | HIGH/OBSERVED |
| 2 | It binds the **same** 64-B struct as TensorLoad: `NEURON_ISA_TPB_MEM_2D_STRUCT` maps to **both** `TENSOR_LOAD` *and* `TENSOR_STORE`. NOT a distinct STORE struct. | `instruction_mapping.json` (4/4 gens): `"NEURON_ISA_TPB_MEM_2D_STRUCT": [ TENSOR_LOAD, TENSOR_STORE ]` (sunda L88–90) | HIGH/OBSERVED |
| 3 | Struct `sizeof == 64`; offsets header=0 events=4 dtype=12 **src_datasrc=13** num_elem=14 start_addr=16 step_elem=24 data=32. | `mem_2d.h:106–128` + `ISA_STATIC_ASSERT==64` + **in-task gcc compile-verify** (all `_Static_assert` pass) | HIGH/OBSERVED |
| 4 | **The new content vs LOAD** — `src_datasrc` (offset 13) may be `0` (Register) **or** `1` (Immediate). When Immediate, the same 32-byte `data[]` field is **reinterpreted** as packed typed immediates via the `MEM2D_DATA` union. | `common.h:754–757` (`DATA_SRC{REGISTER=0,IMMEDIATE=1}`) + `mem2d_datasrc_valid` (`mem_2d.h:232–235`) + union `common.h:738–747` | HIGH/OBSERVED |
| 5 | Immediate element caps = `floor(32 / sizeof(dtype))`: **u8/i8 ≤32, u16/i16 ≤16, u32/i32/fp32 ≤8, u64 ≤4**; Register src is always **≤32**. | `mem2d_store_num_elem_check` (`mem_2d.h:253–268`), byte-cited; spec prose `mem_2d.h:44–50,86–96` | HIGH/OBSERVED |
| 6 | **dtype-truncate on store** — NO numeric cast. The source datum is written at dtype **width** (low N bits for a register source; the high bits drop). The reverse of TensorLoad's zero/sign-extend. | spec Store paragraph carries no cast verb (`mem_2d.h:22–52`); symmetric mirror of the Load extend table (`mem_2d.h:54–73`) | HIGH for "truncate not cast"; MED/INFERRED for exact narrowing |
| 7 | **Dispatch** is iTPB-sequencer-native: decoded in the `0xa5..0xab` control block, NOT a Q7-POOL software kernel and NOT a DVE op. **`"S: TensorStore"` appears 16×** in the host ucode lib (the 16 sequencer blobs), matching WRITE/MOVE/TensorLoad. | `strings libnrtucode_internal.so` → `S: TensorStore` ×16 (in-task) ; absent from carved POOL `kernel_info_table` | HIGH/OBSERVED |
| 8 | Destination = the **full 64-bit Neuron Address Space** (`NEURON_ADDR = uint64_t`): SBUF, PSUM, per-core dataram/DRAM, HBM via the dynamic window. Whole tensor must lie in **one aligned 16 MB region**. | `common.h:443` + `STATE_BUF`/`PSUM_BUF` offsets `common.h:68–71` + `mem_2d.h:52` | HIGH/OBSERVED |
| 9 | **Present + maintained in all four gens** SUNDA(NC-v2) → MAVERICK(NC-v5); store-spec body byte-identical (only the line-3 NC-version comment differs). | per-gen enum `// Y` + JSON + `diff <(tail -n+4 …)` empty (in-task) | HIGH/OBSERVED; v5/MAVERICK interior INFERRED |

---

## 2. Provenance / carve anchors

All struct/enum facts derive from the shipped public ISA C headers; the dispatch-surface facts derive
from static analysis of the shipped host x86-64 ucode library and carved POOL firmware. No vendor
source was referenced.

| Artifact | Value |
|----------|-------|
| Header package | `aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64` |
| Spec file | `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_mem_2d.h` (the `mem_2d` 2-D load/store format) |
| Enums / struct fields | `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_common.h` (`OPCODE`, `DTYPE`, `DATA_SRC`, `MEM2D_DATA`, `ADDR8`, `HEADER`, `EVENTS`, buffer offsets) |
| Struct→opcode binding | `…/neuron_<gen>_arch_isa/tpb/instruction_mapping.json` (`struct2opcode`) |
| Host ucode container | `…/custom_op/c10/lib/libnrtucode_internal.so` — ELF x86-64, **not stripped** |
| Container sha256 | `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b` (10,276,288 B) — re-verified in-task |
| Container BuildID | `sha1:9cbf78c6f59cdb5839f155fdb2113bbe51e585fd` |
| Self-name DEBUG tag | `"S: TensorStore"` ×16 (the 16 sequencer blobs); adjacent `"S: Dispatch opcode=0x%x"` |
| Carved POOL images | `<gen>_Q7_POOL_PERF_EXTISA_*` getters embedded in the container; `kernel_info_table` byte-decoded (8-B entries, opcode @+3) |

The four `mem_2d.h` are **body-byte-identical** across sunda/cayman/mariana/maverick: a `diff` of each
file minus its line-3 NC-version comment is empty for all three pairs. The `MEM2D_DATA` union and the
`DATA_SRC` enum are byte-identical in all four `common.h`. `[HIGH/OBSERVED — in-task]`

> **NOTE — host x86 vs Vision-Q7 FLIX.** None of this decode crosses the Q7 Vision FLIX bundle
> decoder. The struct/enum facts are from C headers; the dispatch facts are from host x86-64 ASCII
> tags + a POOL `kernel_info_table` **data** dump. The `ncore2gp` op0=`e`/`f` FLIX-vs-scalar-LX
> mis-decode artifact (the NCFW core is scalar LX) therefore does **not** apply to anything on this
> page. `[HIGH — no code-stream desync]`

---

## 3. The opcode and its cluster

Read verbatim from `aws_neuron_isa_tpb_common.h` (sunda; the byte values are identical on every gen):

```c
NEURON_ISA_TPB_OPCODE_WRITE              = 0xa5,   // Y
NEURON_ISA_TPB_OPCODE_NOTIFY             = 0xa6,   // Y
NEURON_ISA_TPB_OPCODE_MOVE               = 0xa7,   // Y
NEURON_ISA_TPB_OPCODE_ALU_OP            = 0xa8,   // Y
NEURON_ISA_TPB_OPCODE_COMPARE_BRANCH     = 0xa9,   // Y
NEURON_ISA_TPB_OPCODE_TENSOR_LOAD        = 0xaa,   // Y   <- TensorLoad  (tensorload.md)
NEURON_ISA_TPB_OPCODE_TENSOR_STORE       = 0xab,   // Y   <- THIS PAGE
…
NEURON_ISA_TPB_OPCODE_PSEUDO_TENSOR_STORE = 0xcd,        // compiler pseudo (no // Y)
NEURON_ISA_TPB_OPCODE_PSEUDO_TENSOR_LOAD  = 0xce,        // compiler pseudo (no // Y)
```

`0xab` is the **top** of the `0xa5..0xab` block — the iTPB **sequencer** control / register-access
opcodes (`WRITE`, `NOTIFY`, `MOVE`, `ALU_OP`, `COMPARE_BRANCH`, `TENSOR_LOAD`, `TENSOR_STORE`). These
are *not* PE / ACT / POOL / DVE compute ops; they are the control engine's own instruction set.
`TENSOR_LOAD`/`TENSOR_STORE` are its scalar **memory↔register access pair** — `0xab` is the store half.
`[HIGH/OBSERVED]`

Per-gen byte pins (every gen carries the trailing `// Y` "tested / maintained / not-deprecated" flag
on `0xab`):

| gen | NC-ver | `TENSOR_STORE` (0xab) | `TENSOR_LOAD` (0xaa) | `PSEUDO_TENSOR_STORE` (0xcd) |
|-----|--------|----------------------|----------------------|------------------------------|
| sunda | NC-v2 | L254 `// Y` | L253 `// Y` | present, no `// Y` |
| cayman | NC-v3 | L251 `// Y` | L250 `// Y` | present, no `// Y` |
| mariana | NC-v4 | L256 `// Y` | L255 `// Y` | present, no `// Y` |
| maverick | NC-v5 | L259 `// Y` | L258 `// Y` | present, no `// Y` |

> **GOTCHA — do not confuse `0xab` with the other "stores".** Three distinct things carry "store" in
> their name:
> - **`0xab` TensorStore** (`mem_2d`) — the sequencer's ≤32-scalar reg/imm → memory scatter (this page).
> - **`REG_STORE`** (`D4_MR` struct, with MEMSET/RNG) — a different register-store format.
> - **`DMA_MEMCPY 0xb8` / `DMA_INDIRECT 0xbb`** — the **bulk** tile/tensor DMA movers (these *are* POOL
>   kernels; see [DMA / transpose cluster](dma-transpose-opcode-cluster.md)).
>
> TensorStore is the narrow scalar scatter; it is **not** the bulk DMA tile mover. `[HIGH/OBSERVED]`

The compiler-emitted **`PSEUDO_TENSOR_STORE` (`0xcd`)** uses a parallel 64-B struct
`NEURON_ISA_TPB_PSEUDO_MEM_2D_STRUCT` (`aws_neuron_isa_tpb_pseudo_mem_2d.h:102–115`) in which the
concrete `start_addr` (offset 16, `ADDR8`) is replaced by a symbolic `uint32_t var_id` (16–19) +
`uint32_t var_offset` (20–23); NRT resolves the symbol to a real `ADDR8` and lowers `0xcd → 0xab` at
bind time. The `dtype`/`src_datasrc`/`num_elem`/`step_elem`/`data` fields are identical. `[HIGH/OBSERVED]`

---

## 4. The operand struct — `NEURON_ISA_TPB_MEM_2D_STRUCT` (byte-identical to TensorLoad)

One struct, one spec file, two instructions. The struct **is shared with [TensorLoad](tensorload.md)** —
the per-gen `instruction_mapping.json` proves it:

```jsonc
"NEURON_ISA_TPB_MEM_2D_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_TENSOR_LOAD",
    "NEURON_ISA_TPB_OPCODE_TENSOR_STORE"
]
```

Verbatim layout (`aws_neuron_isa_tpb_mem_2d.h:106–128`):

```c
typedef struct NEURON_ISA_TPB_MEM_2D_STRUCT {
    NEURON_ISA_TPB_HEADER     header;          // 4   ( 0 -  3)  opcode 0xab lives here
    NEURON_ISA_TPB_EVENTS     events;          // 8   ( 4 - 11)  wait/update semaphore sync
    NEURON_ISA_TPB_DTYPE      dtype;           // 1   (12     )  element dtype (8-type set)
    NEURON_ISA_TPB_DATA_SRC   src_datasrc;     // 1   (13     )  0=Register, 1=Immediate  <== STORE-ONLY
    uint8_t                   num_elem[2];     // 2   (14 - 15)  2-D counts; product = # of stores
    NEURON_ISA_TPB_ADDR8      start_addr;      // 8   (16 - 23)  u64 Neuron DST start address
    int32_t                   step_elem[2];    // 8   (24 - 31)  int32 element strides per dim
    NEURON_ISA_TPB_MEM2D_DATA data;            // 32  (32 - 63)  32 src reg# OR packed immediates
} NEURON_ISA_TPB_MEM_2D_STRUCT;

ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_MEM_2D_STRUCT) == 64, "…NOT 64B.");
```

### Compile-verify (in-task, gcc, SUNDA headers)

A standalone TU compiled clean (`gcc -I <sunda hdr>`) with every `_Static_assert` passing:

```c
_Static_assert(sizeof(NEURON_ISA_TPB_MEM_2D_STRUCT)       == 64, "");
_Static_assert(offsetof(…, header)      ==  0, "");
_Static_assert(offsetof(…, events)      ==  4, "");
_Static_assert(offsetof(…, dtype)       == 12, "");
_Static_assert(offsetof(…, src_datasrc) == 13, "");   /* the STORE-only field */
_Static_assert(offsetof(…, num_elem)    == 14, "");
_Static_assert(offsetof(…, start_addr)  == 16, "");
_Static_assert(offsetof(…, step_elem)   == 24, "");
_Static_assert(offsetof(…, data)        == 32, "");
_Static_assert(sizeof(NEURON_ISA_TPB_MEM2D_DATA)          == 32, "");
_Static_assert(sizeof(NEURON_ISA_TPB_DATA_SRC)            ==  1, "");   /* PACKED */
_Static_assert(NEURON_ISA_TPB_DATA_SRC_REGISTER  == 0, "");
_Static_assert(NEURON_ISA_TPB_DATA_SRC_IMMEDIATE == 1, "");
_Static_assert(NEURON_ISA_TPB_OPCODE_TENSOR_STORE == 0xab, "");
_Static_assert(NEURON_ISA_TPB_OPCODE_TENSOR_LOAD  == 0xaa, "");
_Static_assert(sizeof(uint64_t[4]) == 32, "");   /* the implicit u64 immediate footprint, §6 */
```

Result: **all assertions pass** in the SUNDA header set. `[HIGH/OBSERVED — in-task gcc]` The whole
`mem_2d.h` store-spec body is byte-identical across all four gens, so the layout holds gen-for-gen.

> **NOTE — keep this struct byte-identical to TensorLoad.** The 64-B layout above is **literally the
> same struct** the [TensorLoad](tensorload.md) page documents (same offsets, same sub-sizes). The
> STORE/LOAD asymmetry is entirely in how `src_datasrc` and `data[]` are *interpreted at validate /
> execute*, never in the bytes. If the two pages ever disagree on a field offset, this one is the
> compile-verified reference. `[HIGH/OBSERVED]`

Field roles for TensorStore:

| off | size | field | TensorStore role |
|-----|------|-------|------------------|
| 0 | 4 | `header` | `header.opcode == 0xab`; `inst_word_len`, `debug_cmd`, `debug_hint` |
| 4 | 8 | `events` | `wait_mode`/`wait_idx`/`update_mode`/`update_idx`/`semaphore_value` — standard sync |
| 12 | 1 | `dtype` | element width/signedness; one of the 8 accepted dtypes (§7) |
| 13 | 1 | `src_datasrc` | **`0` Register \| `1` Immediate** — the LOAD-forbidden degree of freedom |
| 14 | 2 | `num_elem[2]` | 2-D counts, each `1..32`; `num_elem[0]*num_elem[1]` = number of stores |
| 16 | 8 | `start_addr` | `ADDR8` — u64 Neuron destination start address (imm / reg / table form) |
| 24 | 8 | `step_elem[2]` | two `int32` **element** strides (one per 2-D dim) |
| 32 | 32 | `data` | Register: 32 `REG_NUM` source register ids · Immediate: packed typed immediates |

### Validation contract

`is_valid_tensor_store` (`mem_2d.h:149–158`) ANDs eight predicates:

```c
is_valid_tensor_store(i) =
       has_valid_neuron_header(i)
    && has_valid_neuron_events(i)
    && has_tensor_store_opcode(i)          // header.opcode == TensorStore (mem_2d.h:274-276)
    && mem2d_shared_constraints(i)         // num_elem 1..32 each + dtype in the 8-type set
    && mem2d_datasrc_valid(i)              // <== STORE DELTA: src_datasrc in {Register, Immediate}
    && mem2d_store_num_elem_check(i)       // <== STORE DELTA: per-dtype immediate caps (§6)
    && mem2d_u64_register_check(i)         // if u64 + Register, all 32 reg# even
    && valid_start_addr(i);                // is_aligned_start_addr8(start_addr, dtype)
```

The **only two** clauses that differ from `is_valid_tensor_load` are the two marked `STORE DELTA`.
Everything else is shared verbatim with LOAD:

| clause | LOAD (`0xaa`) | STORE (`0xab`) |
|--------|---------------|----------------|
| datasrc | `mem2d_datasrc_zero` — `src_datasrc == Register` **forced** (`mem_2d.h:228–230`) | `mem2d_datasrc_valid` — Register **or** Immediate (`mem_2d.h:232–235`) |
| element-count | `mem2d_32_elem_check` — uniform `≤ 32` (`mem_2d.h:237–239`) | `mem2d_store_num_elem_check` — per-dtype caps (`mem_2d.h:253–268`) |

`[HIGH/OBSERVED]`

---

## 5. `mem_2d` addressing — the 2-D strided destination walk

The destination is a 2-D strided tensor anchored at `start_addr`. `num_elem[0]`/`num_elem[1]` are the
inner/outer counts (each `1..32`, with `num_elem[0]*num_elem[1] ≤ 32` from `mem2d_num_elem_check`,
`mem_2d.h:169–174`); `step_elem[0]`/`step_elem[1]` are signed **element** strides (multiplied by
`sizeof(dtype)` to get byte deltas). The header notes the fields are "individually broken out in
struct due to alignment/packing requirements" precisely because `start_addr` is a u64 and the strides
are `i32` — they will not pack into a standard tensor-pattern struct (`mem_2d.h:31–33,111–123`).

```c
// 2-D destination address generator (mem_2d format, shared with TensorLoad)
static inline uint64_t mem2d_elem_addr(
        uint64_t start_addr,                  // i.mem_2d.start_addr  (Neuron Address)
        const int32_t step_elem[2],           // i.mem_2d.step_elem[2] (signed element strides)
        unsigned i0, unsigned i1,             // inner/outer 2-D indices
        unsigned dtype_bytes)                 // sizeof(dtype)
{
    return start_addr
         + (int64_t)i0 * step_elem[0] * dtype_bytes   // inner dimension
         + (int64_t)i1 * step_elem[1] * dtype_bytes;  // outer dimension
}
```

> **GOTCHA — single 16 MB window.** The spec requires "full tensor must be in *one* aligned 16 MB
> region (to allow sequencer to use one memory window)" (`mem_2d.h:52`). The sequencer maps the
> destination through **one** 16 MB hardware memory-window TLB entry, so the entire 2-D span — every
> address the generator above produces — must stay inside one aligned 16 MB region. This is the same
> constraint TensorLoad carries. `[HIGH/OBSERVED]`

`start_addr` is an `ADDR8` (`common.h:553–558`): a union whose marker byte selects an **immediate**
address, an address **from a register**, or an address **from a table** (with optional wide offset).
Alignment is enforced only for the immediate form:

```c
// valid_start_addr  ->  is_aligned_start_addr8(addr, dtype)   (common.h:2080-2083)
//   ( !addr8_addr_immediate(addr) )           // register-/table-supplied addr: trusted at runtime
//   || addr8_aligned_dtype(addr, dtype)       // immediate addr: must be sizeof(dtype)-aligned
//   where addr8_aligned_dtype -> addr_aligned_bytes(addr, sizeof(dtype)) -> (addr % sizeof(dtype) == 0)
```

So an **immediate** start address must be naturally aligned to the element width; a **register**-
supplied address is not statically checkable and is trusted at runtime (`mem_2d.h:81`: "valid Neuron
Address (no assertion for this, depends on system)"). There is **no** per-element destination-range
assertion in the validate set — destination legality beyond alignment + single-window is a runtime /
system property. `[HIGH/OBSERVED]`

---

## 6. The immediate-source store path — the central NEW content vs LOAD

This is the one capability TensorStore has and [TensorLoad](tensorload.md) does **not**. It has two
facets: where the immediate data lives, and how many fit.

### 6a. The immediate data overlays `data[]` (no separate region)

`data` is a 32-byte union (`common.h:738–747`, byte-identical in all four gens):

```c
typedef union NEURON_ISA_TPB_MEM2D_DATA {
    NEURON_ISA_TPB_REG_NUM registers[32];   // Register src: 32 register ids (REG_NUM = uint8)
    uint8_t   uint8[32];   int8_t   int8[32];     // Immediate src overlays:
    uint16_t  uint16[16];  int16_t  int16[16];
    uint32_t  uint32[8];   int32_t  int32[8];
    float     fp32[8];
} NEURON_ISA_TPB_MEM2D_DATA;                 // sizeof == 32 (compile-verified)
```

When `src_datasrc == Register`, the executor reads `data.registers[k]` (a `uint8` register number) and
fetches `R[that]`. When `src_datasrc == Immediate`, the executor reads `data.<dtype-overlay>[k]` — the
**same 32 bytes** reinterpreted as packed typed immediates. The immediate data does **not** occupy a
separate field; it overlays `data[]`. One field, two readings. `[HIGH/OBSERVED]`

> **QUIRK — the u64 immediate overlay is implicit.** The union lists eight members
> (`registers`/`uint8`/`int8`/`uint16`/`int16`/`uint32`/`int32`/`fp32`) — there is **no** explicit
> `uint64[4]` member. Yet the validator and prose both name a `u64 ≤ 4` immediate case. The footprint
> is consistent — `sizeof(uint64_t[4]) == 32` (compile-verified) exactly fills `data[]` — so four u64
> immediates reuse the 32-byte field as the `uint32[8]` overlay read **pairwise** (lo/hi). The
> *footprint* is HIGH/OBSERVED; the exact lo/hi **byte order** of a u64 immediate is **MED/INFERRED**
> (inferred from the `uint32[8]` overlay, not spelled out in the header). `[mixed — flagged]`

### 6b. The per-dtype immediate caps = `floor(32 / sizeof(dtype))`

`mem2d_store_num_elem_check` (`mem_2d.h:253–268`), byte-for-byte:

```c
mem2d_store_num_elem_check(i) =
       ( src_datasrc == Register                       && (n0*n1 <= 32) )   // Register: always 32
    || ( dtype in {UINT8,  INT8}                        && (n0*n1 <= 32) )   // u8 /i8  -> 32
    || ( dtype in {UINT16, INT16}                       && (n0*n1 <= 16) )   // u16/i16 -> 16
    || ( dtype in {UINT32, INT32, FP32}                 && (n0*n1 <=  8) )   // u32/i32/f32 -> 8
    || ( dtype == UINT64                                && (n0*n1 <=  4) );  // u64     -> 4
```

The first disjunct keys on `src_datasrc == Register`: a register store is always capped at the flat
`≤ 32` (32 register ids fit in `data[]` regardless of dtype). Otherwise (Immediate), the dtype selects
the cap — and each cap is exactly `32 / sizeof(dtype)`: the number of dtype-width immediates that fit
in the 32-byte `data[]` field. The gate that *permits* Immediate at all is `mem2d_datasrc_valid`;
LOAD's `mem2d_datasrc_zero` forbids it. `[HIGH/OBSERVED — both the validator body and the prose,
`mem_2d.h:44–50,86–96`]`

> **NOTE — the immediate path is also how TensorStore broadcasts a small constant table.** Because the
> immediates are packed *in the instruction*, a single `0xab` can scatter up to 32 small constants
> (e.g. an init pattern, a clear value replicated, a tiny lookup row) to a 2-D window with **no**
> register-file traffic and **no** DMA — the sequencer's cheapest way to push a handful of literals
> into Neuron memory. `[HIGH for the mechanism; the "init pattern / clear" use is INFERRED]`

---

## 7. dtype on store — NO numeric cast; truncating-width store

TensorStore accepts exactly the same 8 dtypes as TensorLoad (`mem2d_dtype_check`, `mem_2d.h:176–185`).
`DTYPE` enum bytes (`common.h:704–721`):

| dtype | enum | on-store to memory | Register cap | Immediate cap |
|-------|------|--------------------|--------------|---------------|
| `UINT8` | `0x3` | write low 8 b | 32 | 32 |
| `INT8` | `0x2` | write low 8 b | 32 | 32 |
| `UINT16` | `0x5` | write low 16 b | 32 | 16 |
| `INT16` | `0x4` | write low 16 b | 32 | 16 |
| `UINT32` | `0x9` | bit-copy 32 b | 32 | 8 |
| `INT32` | `0x8` | bit-copy 32 b | 32 | 8 |
| `FP32` | `0xA` | bit-copy raw 32 b | 32 | 8 |
| `UINT64` | `0x1` | lo/hi → 64 b | 32 (even-reg pair) | 4 |

The on-store "conversion" is **not** a numeric cast (no fp↔int, no fp32↔bf16, no rounding, no
saturation). It is the **mirror of TensorLoad's width-normalize**:

- **Load** width-*normalizes* a narrow element **up** into a 32-bit register: `u8`/`u16` zero-extend,
  `i8`/`i16` sign-extend, `i32`/`u32`/`fp32` bit-copy, `u64` even-register pair (`mem_2d.h:54–73`).
- **Store** does the reverse — a **truncating-width** store: the source datum (a 32-bit GPR for a
  register source, or a packed immediate of dtype width for an immediate source) is written to memory
  at **dtype width**. For a register source storing a sub-32-bit dtype, only the **low dtype-width
  bits** of the 32-bit GPR are written; the high bits drop. An `i32`-register store with `dtype==i8`
  narrows to the low byte.

`[HIGH for the "no numeric cast / truncating-width" classification — the Store paragraph carries no
cast verb and is the exact mirror of the documented Load extend table. MED/INFERRED for the exact
"low-N-bits of the 32-b GPR" narrowing on a register source, which is the standard scalar narrowing
store but is not spelled out element-by-element the way Load's extend table is.]`

> **CORRECTION — `FP32R` is in the dtype enum but NOT in the store set.** `NEURON_ISA_TPB_DTYPE_FP32R`
> (`0xB`) exists in the `DTYPE` enum (`common.h:712`, the "RTL FP22 partial fp32 type") but is **not**
> one of the 8 dtypes `mem2d_dtype_check` accepts. Neither are `bf16`/`fp16`/`fp8`. TensorStore's
> dtype set is the integer + `fp32` + `u64` **scalar** set, *not* the wider compute-dtype set. Do not
> assume a data-movement op accepts every `DTYPE` value. `[HIGH/OBSERVED]`

Data-movement family, dtype-convert column:

| instr | opcode | struct | dtype-convert on move? |
|-------|--------|--------|------------------------|
| **TensorStore** | `0xab` | `MEM_2D` | **NO cast**; truncating store to dtype width |
| [TensorLoad](tensorload.md) | `0xaa` | `MEM_2D` | NO cast; int zero/sign-extend to 32 b only |
| [Move](move-dtype.md) | `0xa7` | `CTRL_MV` | NO cast; full-reg move, gated `{u32,i32,fp32}` |
| [Copy](cast-copy.md) | — | `S4D4_TR` | element-copy, *may* convert dtype |
| Cast | — | `S4D4_TR` | element-copy, *does* convert dtype (fp pack) |

`[HIGH/OBSERVED]`

---

## 8. The execute algorithm — the 2-D scalar-scatter store loop

TensorStore is a hardware sequencer instruction (no Q7 IVP/TIE op named `tensor_store` exists in the
device ISA roster — it is decoded and executed by the iTPB sequencer itself). The struct + spec
prescribe the following per-element scatter, naming the real validator-referenced fields:

```c
// TensorStore (opcode 0xab) execute model — fields named from NEURON_ISA_TPB_MEM_2D_STRUCT.
// SRC = sequencer GPRs r0..r31 OR packed in-instruction immediates; DST = Neuron Address Space.
void exec_tensor_store(const NEURON_ISA_TPB_MEM_2D_STRUCT *i)
{
    const unsigned       n0    = i->num_elem[0];        // inner 2-D count (1..32)
    const unsigned       n1    = i->num_elem[1];        // outer 2-D count (1..32), n0*n1 <= 32
    const unsigned       db    = dtype_bytes(i->dtype); // 1 / 2 / 4 / 8
    const uint64_t       base  = addr8_resolve(i->start_addr); // imm/reg/table -> 64-b Neuron addr
    const bool           is_imm = (i->src_datasrc == NEURON_ISA_TPB_DATA_SRC_IMMEDIATE);

    unsigned k = 0;
    for (unsigned i1 = 0; i1 < n1; ++i1) {              // outer dimension
        for (unsigned i0 = 0; i0 < n0; ++i0) {          // inner dimension
            uint64_t addr = base
                          + (int64_t)i0 * i->step_elem[0] * db    // signed element strides
                          + (int64_t)i1 * i->step_elem[1] * db;

            uint64_t v;                                  // value, then truncated to dtype width
            if (!is_imm) {                               // src_datasrc == Register
                v = R[i->data.registers[k]];             // 32-b GPR (even-reg PAIR for u64)
            } else {                                     // src_datasrc == Immediate
                v = read_immediate(&i->data, i->dtype, k); // data.<dtype-overlay>[k]
            }

            // dtype-TRUNCATE on store (no numeric cast): write only the low dtype-width bits.
            store_dtype_width(addr, v, i->dtype);
            ++k;
        }
    }
    // Caps:   Register -> k<=32 ; Immediate -> u8/i8<=32, u16/i16<=16, u32/i32/fp32<=8, u64<=4.
    // Window: every 'addr' must lie in ONE aligned 16 MB region (mem_2d.h:52).
    // Align:  immediate start_addr must be sizeof(dtype)-aligned (valid_start_addr).
    // u64+Register: all 32 source register numbers must be EVEN (mem2d_u64_register_check).
}
```

**u64 register pairing.** For a `u64` **register** store, each 64-bit value comes from a register
**pair**, so `mem2d_u64_register_check` (`mem_2d.h:187–226`, via `mem2d_odd_register_used`) requires
**all 32** source register numbers to be even (`r0`, `r2`, …, `r14`, …): an odd register id in any of
the 32 slots fails validation. (For a `u64` **immediate**, the value is in the `data[]` overlay — §6a.)
`[HIGH/OBSERVED]`

**No multi-tile continuation.** TensorStore is bounded to ≤32 (or fewer, per dtype) scalars in one
window — a single pass, `O(n)` with `n ≤ 32`. Larger transfers are not its job: those use
`DMA_MEMCPY (0xb8)` / `DMA_INDIRECT (0xbb)` (POOL kernels). `[HIGH/OBSERVED]`

---

## 9. Destination memory windows

The destination is the **full 64-bit Neuron Address Space** — `NEURON_ISA_TPB_NEURON_ADDR = uint64_t`
(`common.h:443`), the type behind `ADDR8.addr_immediate`. The address generator of §5 can target any
window the sequencer's 16 MB memory-window TLB can map for this NeuronCore:

| window | partition-offset anchor (from `common.h`) | notes |
|--------|-------------------------------------------|-------|
| State Buffer (SBUF) | `STATE_BUF_OFFSET = 0x0`, `STATE_BUF_SZ = 0x2000000` (32 MB) | the on-chip scratch; `addr_in_sbuf(addr) = addr < STATE_BUF_OFFSET + STATE_BUF_SZ` (`common.h:68,70,2219–2221`) |
| PSUM | `PSUM_BUF_OFFSET = 0x2000000`, `PSUM_BUF_SZ = 0x400000` (4 MB) | `addr_in_psum(addr) = PSUM_BUF_OFFSET <= addr < PSUM_BUF_OFFSET + PSUM_BUF_SZ` (`common.h:69,71,2223–2226`) |
| per-core dataram / Q7 DRAM | reached as a Neuron-address region via the window | the sequencer's own DRAM-resident scratch |
| HBM / general DRAM | reached through the dynamic 16 MB `ADDR8` window | the same TLB path the LOAD twin uses |

> **NOTE — partition-offset constants vs the 64-bit `ADDR8`.** The `STATE_BUF`/`PSUM_BUF` constants
> above are **partition-local offsets** (`PartitionOffset = uint32_t`, `common.h:441`) used by the
> partition-window predicates (`addr_in_sbuf`/`addr_in_psum`). TensorStore's `start_addr` is the wider
> 64-bit `ADDR8` Neuron Address; the SBUF/PSUM windows are the *low* region of that space. The only
> **encode-time** destination constraint TensorStore enforces is (a) `start_addr` alignment for the
> immediate form and (b) the whole span inside one 16 MB window — there is no per-element SBUF/PSUM
> range assertion (`mem_2d.h:81`). See the [memory model](../../dma/sbuf-psum-banks.md) /
> [LSU memory](../../uarch/lsu-memory.md) and the planned
> [SBUF/PSUM banks](../../dma/sbuf-psum-banks.md) for the full map. `[HIGH/OBSERVED]`

---

## 10. Dispatch surface — sequencer-native, not a POOL kernel

TensorStore is decoded by the **same iTPB sequencer front-end** as the rest of the `0xa5..0xab`
control block. Two independent surfaces confirm it is sequencer-native and **not** a POOL software
kernel:

1. **Host ucode self-name tags.** In the non-stripped `libnrtucode_internal.so`, `"S: TensorStore"`
   appears **16 times** — the count of the 16 sequencer-engine blobs — matching `"S: WRITE"` (16),
   `"S: MOVE"` (16), and `"S: TensorLoad"` (16). A DVE op tags differently: `"S: FindIndex8"` appears
   **4 times** (DVE). The `"S: TensorStore"` strings sit adjacent to `"S: Dispatch opcode=0x%x"`,
   confirming opcode-keyed sequencer dispatch. `[HIGH/OBSERVED — in-task `strings` counts]`

2. **POOL `kernel_info_table` absence.** The carved POOL images' `kernel_info_table` (8-byte entries,
   opcode at byte +3) was byte-decoded across multiple carves: opcode `0xab` is **absent** from every
   POOL table (and so is its twin `0xaa`), while the bulk movers `0xb8` (`DMA_MEMCPY`) and `0xbb`
   (`DMA_INDIRECT`) **are** present. TensorStore is therefore **not** a POOL kernel; the bulk DMA
   movers are. `[HIGH/OBSERVED — report SX-FW-65 §6, re-decoded across 3 carves]`

**Dispatch chain (identical all four gens):**

```text
(compile)   compiler may emit PSEUDO_TENSOR_STORE (0xcd, NEURON_ISA_TPB_PSEUDO_MEM_2D_STRUCT,
            symbolic var_id/var_offset). NRT lowers 0xcd -> real 0xab MEM_2D at bind time.   [HIGH/OBS]
(encode)    instruction = NEURON_ISA_TPB_MEM_2D_STRUCT, 64 B, header.opcode = 0xab,
            src_datasrc = 0|1, num_elem[0..1], start_addr (ADDR8 dst), step_elem[0..1],
            data = 32 src reg# OR packed immediates.                                          [HIGH/OBS]
(validate)  is_valid_tensor_store (§4) — incl. the two STORE deltas
            (mem2d_datasrc_valid + mem2d_store_num_elem_check).                               [HIGH/OBS]
(decode)    iTPB sequencer front-end matches header.opcode == 0xab in the 0xa5..0xab control
            block and routes to the mem_2d STORE handler (the "S: TensorStore" blob, 16 copies).
            NO kernel_info_table lookup, NO funcVA, NO POOL callx8.                            [HIGH/OBS
            for "no POOL hop"; MED for exact handler VA — inlined/templated, no host symbol]
(execute)   the 2-D scalar scatter of §8 (truncate-on-store).                                 [HIGH/OBS]
```

`[HIGH/OBSERVED for the surface verdict; MED for the exact in-firmware handler VA — the store handler
is inlined/templated with no discrete host symbol.]`

---

## 11. Per-gen presence

ISA-definition presence (opcode enum byte + `// Y` + struct map + spec block + 64-B compile-verify),
all from the shipped headers + JSON:

| gen | NC-ver | enum `0xab` `// Y` | store spec body | JSON → `MEM_2D` | struct 64 B | verdict |
|-----|--------|--------------------|-----------------|-----------------|-------------|---------|
| sunda | NC-v2 | YES (cmn L254) | YES (body-identical) | YES (LOAD+STORE) | YES (gcc) | **PRESENT** `[HIGH/OBS]` |
| cayman | NC-v3 | YES (cmn L251) | YES (body-identical) | YES (LOAD+STORE) | YES (gcc) | **PRESENT** `[HIGH/OBS]` |
| mariana | NC-v4 | YES (cmn L256) | YES (body-identical) | YES (LOAD+STORE) | YES (gcc) | **PRESENT** `[HIGH/OBS]` |
| maverick | NC-v5 | YES (cmn L259) | YES (body-identical) | YES (LOAD+STORE) | YES (gcc) | **PRESENT** `[HIGH/OBS; v5 interior INFERRED]` |

`TENSOR_STORE` (`0xab`) is ISA-defined + maintained (`// Y`) + 64-B-struct-identical + store-spec-body-
byte-identical in **all four** gens, from the earliest (SUNDA / NC-v2). No gen-presence asymmetry, no
deprecation. The only per-gen file difference is the line-3 NC-version comment. The dispatch-surface
absence from the POOL `kernel_info_table` holds across every decoded gen. Per the standing rule, the
v5 / MAVERICK column is **header-OBSERVED**; its firmware **interior** behavior is **INFERRED** from
the byte-identical v2–v4 contract. `[HIGH/OBSERVED for the header presence]`

---

## 12. LOAD / STORE pair — side by side

Closing the `0xaa`/`0xab` pair (the [TensorLoad](tensorload.md) page is the mirror; keep the `mem_2d`
struct byte-identical between the two):

| property | `0xaa` [TensorLoad](tensorload.md) | `0xab` TensorStore (this page) |
|----------|-------------------------------------|--------------------------------|
| opcode byte (4 gens) | `0xaa` `// Y` | `0xab` `// Y` |
| direction | Neuron MEM → sequencer regs | regs / imm → Neuron MEM |
| struct | `NEURON_ISA_TPB_MEM_2D_STRUCT` | **same** struct (64 B, shared) |
| spec file | `mem_2d.h` "## TensorLoad" | `mem_2d.h` "## TensorStore" |
| `src_datasrc` (off 13) | **forced `0`** (`mem2d_datasrc_zero`) | `0` Register **or** `1` Immediate (`mem2d_datasrc_valid`) |
| `data[]` (off 32, 32 B) | 32 **dst** register numbers | 32 **src** reg# **or** packed immediates |
| immediate path | NONE (forbidden) | **YES** — packed in `data[]` overlay |
| element-count check | `mem2d_32_elem_check` (`≤32`) | `mem2d_store_num_elem_check` (Reg ≤32; u8 ≤32, u16 ≤16, u32/f32 ≤8, u64 ≤4) |
| dtype behavior | NO cast; int zero/sign-extend to 32 b; fp32/i32/u32 copy; u64 even pair | NO cast; **truncating-width** store; low-N bits written; u64 pair/imm-slot |
| dtype set | `{u8,i8,u16,i16,i32,u32,fp32,u64}` | **same 8** dtypes |
| alignment | immediate `start_addr` dtype-aligned | **same** (`is_aligned_start_addr8`) |
| window | one aligned 16 MB region | **same** |
| u64 + Register | all 32 reg# even | **same** (`mem2d_u64_register_check`) |
| dispatch surface | iTPB sequencer (`0xa5..0xab`) | **same** — its twin |
| POOL `kernel_info_table` | ABSENT (not a POOL kernel) | ABSENT (re-decoded carves) |
| `S:`-tag multiplicity | 16 (sequencer) | 16 (sequencer) |
| pseudo (compiler) | `0xce` `PSEUDO_TENSOR_LOAD` | `0xcd` `PSEUDO_TENSOR_STORE` |
| per-gen presence | SUNDA..MAVERICK, all `// Y` | SUNDA..MAVERICK, all `// Y` |

**Net.** The pair is a structurally symmetric memory↔register scalar access pair. The **only**
asymmetry: STORE adds the `src_datasrc == Immediate` branch (packed immediates in `data[]` with
per-dtype `≤32/16/8/4` caps) and the truncating-width store; LOAD forbids the immediate branch
(`datasrc` forced `0`) and zero/sign-extends instead. `[HIGH/OBSERVED]`

---

## 13. Cross-references

- [TensorLoad (`0xaa`)](tensorload.md) — the mirror; the `mem_2d` struct here is byte-identical to it.
- [Move + dtype gate (`0xa7`)](move-dtype.md) — the sibling sequencer register move (no cast, full-reg).
- [Cast and Copy](cast-copy.md) — the data-movers that *do* convert dtype (contrast §7).
- [DMA / transpose opcode cluster](dma-transpose-opcode-cluster.md) — the bulk movers `0xb8`/`0xbb`
  (what TensorStore is **not**).
- Store ISA batch: [`../../isa/ref/b07-stores.md`](../../isa/ref/b07-stores.md) — the store-mnemonic group.
- ISS store semantics: `../../iss/cas-load-store.md` *(planned)* — the host-emulated store port
  (the device-LSU truncate primitive that realizes "truncate on store").
- SBUF / PSUM banks: `../../dma/sbuf-psum-banks.md` *(planned)* — the destination-window bank map.
- Memory model: [On-Chip State-Buffer (SBUF) + PSUM Bank Model](../../dma/sbuf-psum-banks.md) and
  [`../../uarch/lsu-memory.md`](../../uarch/lsu-memory.md) — the Neuron Address Space + 16 MB windows.

---

## 14. Confidence / provenance summary

**HIGH / OBSERVED**

- opcode `0xab`, `// Y` maintained, all four gen `common.h` enums (sunda L254 / cayman L251 / mariana
  L256 / maverick L259), byte-pinned.
- operand struct `NEURON_ISA_TPB_MEM_2D_STRUCT`, 64 B, all offsets + sub-sizes, **shared** with LOAD
  (`instruction_mapping.json`: `MEM_2D_STRUCT → [TENSOR_LOAD, TENSOR_STORE]`), in-task gcc compile-
  verify all `_Static_assert` pass; store-spec body byte-identical across gens (only line-3 differs).
- SRC = sequencer GPRs r0..r31 **or** packed in-instruction immediates; DST = full 64-b Neuron Address
  Space; ≤32 (or fewer) scalars; one aligned 16 MB window.
- the immediate path: `src_datasrc {0 Register, 1 Immediate}` via `mem2d_datasrc_valid` (LOAD forces 0
  via `mem2d_datasrc_zero`); immediate data overlays the 32-B `data[]` union; per-dtype caps
  u8/i8 ≤32, u16/i16 ≤16, u32/i32/fp32 ≤8, u64 ≤4 (`mem2d_store_num_elem_check`) = `32/sizeof`.
- dtype set = the same 8; on-store = truncating-width (no numeric cast), the mirror of LOAD's
  zero/sign-extend; `FP32R`/bf16/fp16/fp8 excluded (`mem2d_dtype_check`).
- dispatch: `"S: TensorStore"` ×16 in `libnrtucode_internal.so` (in-task) ⇒ sequencer-native; absent
  from the POOL `kernel_info_table` ⇒ not a POOL kernel (bulk movers `0xb8`/`0xbb` are).
- present + maintained in all four gens SUNDA(NC-v2)..MAVERICK(NC-v5).

**MED / INFERRED**

- exact narrowing mechanics of a 32-b GPR → sub-32-b store (Register-src): "low dtype-width bits
  written" is the standard scalar narrowing store, not spelled element-by-element in the header.
- exact lo/hi byte layout of a u64 **immediate** in `data[]` (no explicit `uint64[4]` union member; the
  `32 == 4*u64` footprint is verified, the ordering inferred from the `uint32[8]` overlay read pairwise).
- exact in-firmware STORE handler VA (inlined/templated; no discrete host symbol).
- whether the sequencer scalar store reuses the device-LSU truncate primitive vs a sequencer-local
  narrowing (the classification "truncate not cast" is HIGH regardless).
- v5 / MAVERICK firmware **interior** (header-OBSERVED only; interior INFERRED from the v2–v4 contract).

**Premise confirmations** — SAME struct as LOAD (JSON + compile-verify, not a distinct STORE struct);
TensorStore is **not** a POOL kernel and **not** a bulk tile/tensor DMA — it is the iTPB sequencer
scalar reg/imm → memory store (≤32 scalars), the bulk movers being `DMA_MEMCPY (0xb8)` /
`DMA_INDIRECT (0xbb)`.
