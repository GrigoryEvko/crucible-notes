# DGE Micro-Op Encoding (byte-level)

A GPSIMD DGE transfer is not a single object — it is the *same logical copy* re-encoded
across **three distinct byte layers**, with one direction bit threaded through all of them.
This page decodes each layer to the byte, names the real symbol/field behind every value,
and walks a concrete program end-to-end: a 2-D memcpy carved from a NEFF and a 2-D
transpose worked through the encoding. The three layers, and where they live:

| Layer | Encoding | What it is | Primary byte anchor |
|-------|----------|------------|---------------------|
| **L1** | 64-byte TPB instruction slot | the compiler/runtime wire word | `header.opcode` @byte0; `dge_op` / `compute_op` @+15 |
| **L2** | `GENERATE` / `DIMPUSH` / `REGWRITE` push micro-ops | the on-device descriptor-**program** push primitives | firmware emit format strings (carved, §3) |
| **L3** | 16-byte `SDMA_CME_BD_DESC` | the BD the SDMA engine executes | `DGE_MEMORY` carveout; v5 folds it inline as `DESCRIPTOR_RAW` |

The "DGE micro-op" the task asks about is overloaded across L1 and L2; this page pins both
and the edges between them. The device-side emit **mechanism** (control flow, the FLIX-desync
wall, the dram0 base-pointer addressing) lives on [`../firmware/dge/dge-emit.md`](../firmware/dge/dge-emit.md);
this page is the **encoding**. The L3 BD field tables are on
[`descriptor-ring-field-tables.md`](descriptor-ring-field-tables.md), the index-driven
descriptors on [`gather-scatter-descriptors.md`](gather-scatter-descriptors.md), and the
consolidated view on [`data-movement-reference.md`](data-movement-reference.md).

> All facts below are derived from static binary analysis: the shipped compile-grade
> arch-isa headers (every struct carries an `ISA_STATIC_ASSERT(... == 64)`), the firmware
> blobs carved from `libnrtucode_internal.so` and disassembled native with the shipped
> `ncore2gp` `xtensa-elf-objdump`, and the `ncore2gp` device memory map. Confidence is
> tagged HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED per claim.

---

## 0. The device memory map the carve needs

The firmware emit/descriptor strings are addressed dram0-relative; interpreting them
requires the device VMA layout. From the `ncore2gp` app-board `memmap.xmm`
(`tools/ncore2gp/xtensa-elf/lib/app-board/memmap.xmm`, byte-read):

| segment | VMA base | size | sections |
|---------|----------|------|----------|
| `iram0` | `0x00004` | `0x0fffc` | `.iram0.literal` `.iram0.text` |
| `dram0` | `0x80000` | `0x10000` | `.dram0.rodata` `.dram0.data` `.dram0.bss` |
| `sram` | `0x100000` | `0x40000000` | `.text .rodata .data` … `STACK HEAP` |

The reset vector is at iram0 base `0x4` (the carved IRAM begins `06 76 00 00` = `j 0x1dc`,
then `86 77 00 00` = `j 0x1e8` at `+0x6` — the native ncore2gp disassembly of the carved
image). The `[0x80000, 0x90000)` dram0 window is the Q7 NX-local dataram aperture; the DGE
log/format strings live in `.dram0.rodata` at `0x80000 + blob_offset`.
**[HIGH/OBSERVED — `memmap.xmm` + carved-image reset vector.]**

---

## 1. Layer 1 — the 64-byte TPB slot

Every L1 form is exactly 64 bytes: `byte0 = header.opcode` (`NEURON_ISA_TPB_OPCODE`),
`byte1 = inst_word_len = 0x10` (16 four-byte words = 64 B). The little-endian lead word a
casual reader sees as `0x10b8` is `{opcode 0xb8, len 0x10}` — **not** a 16-bit opcode.

### 1.1 The instruction-opcode byte (byte0)

From `aws_neuron_isa_tpb_common.h` `enum NEURON_ISA_TPB_OPCODE` (byte-exact):

| opcode | byte0 | resolved struct | DGE kind |
|--------|-------|-----------------|----------|
| `DMAMEMCPY` | `0xb8` | `NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT` | DIRECT2D (0) |
| `DMA_INDIRECT` | `0xbb` | `NEURON_ISA_TPB_DMA_INDIRECT1D_STRUCT` | INDIRECT1D (1) |
| `DMA_TRANSPOSE` | `0xbd` | `NEURON_ISA_TPB_DMA_DIRECT2D_XPOSE_STRUCT` | TRANSPOSE (2) |
| `DMA_GATHER_TRANSPOSE` | `0xf1` | `NEURON_ISA_TPB_DMA_GATHER_XPOSE_STRUCT` | GATHER_TRANSPOSE (3) |
| `SB2SB_COLLECTIVE` | `0xbf` | *(on-engine collective; not a DGE word)* | — |
| `PSEUDO_DMA_DIRECT2D` | `0xd4` | `NEURON_ISA_TPB_PSEUDO_DMA_DIRECT2D_STRUCT` | carries `dge_op` |
| `PSEUDO_DMAMEMCPY_FULL_IND` | `0xc4` | *(full-indirect pseudo)* | — |
| `DMA_MEMCPY2` | `0xb9` | `NEURON_ISA_TPB_DMA_COPY2D_STRUCT` (v5) | §6 |
| `DMA_IMMEDIATE` | `0xba` | `NEURON_ISA_TPB_DMA_IMMEDIATE_STRUCT` (v5) | §6 |

**[HIGH/OBSERVED — `common.h:257–302` (cayman); `0xb9`/`0xba` at maverick `common.h:268–269`.]**
The pseudo bytes share the `0b110xxxxx` upper-three-bit family (`0xc1..0xdf`); a runtime
relocation pass resolves them to the real bytes (CARRIED, runtime pseudo-never-executed rule).

### 1.2 The resolved DIRECT2D word (`DMAMemcpy`, 0xb8)

`NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT`, `ISA_STATIC_ASSERT == 64`, sunda(NC-v2)…maverick(NC-v5).
Header prose, verbatim: *"This instruction will generate DMA descriptors to initiate a Memcpy.
The descriptors will be supplied by the DGE block and the TPB engine will update the DMA queue
tail pointers when the descriptors are ready."*

| off | size | member | detail |
|-----|------|--------|--------|
| 0 | 4 | `header` | `{opcode, inst_word_len=0x10, debug_cmd, debug_hint}` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` (§1.5) |
| 12 | 1 | `dma_configs` | `{priority_class:3, reserved_bitfield:5}` |
| 13 | 1 | `semaphore` | completion semaphore index (0–255) |
| 14 | 1 | `sem_increment` | bump amount (0 = no increment) |
| **15** | 1 | **`compute_op`** | `NEURON_ISA_TPB_DGE_COMPUTE_OP` — the reduce mode (§1.4) |
| 16 | 8 | `src_start_addr` | `NEURON_ISA_TPB_ADDR8` (§1.3) |
| 24 | 8 | `src_step_elem[2]` | **int32 SIGNED** (x,y step) — the `DIMPUSH` steps |
| 32 | 4 | `src_num_elem[2]` | uint16 (x,y count) — the `DIMPUSH` counts |
| 36 | 2 | `src_elem_size` | uint16 (bytes/element; **0 ⇒ 64 KiB**) |
| 38 | 1 | `src_bound_reg` | `NEURON_ISA_TPB_BOUND_CHECK_REG` (gates SOURCE) |
| 39 | 1 | `dst_bound_reg` | gates DEST |
| 40 | 8 | `dst_start_addr` | `ADDR8` |
| 48 | 8 | `dst_step_elem[2]` | int32 SIGNED |
| 56 | 4 | `dst_num_elem[2]` | uint16 |
| 60 | 2 | `dst_elem_size` | uint16 |
| 62 | 1 | `in_dtype` | `NEURON_ISA_TPB_DTYPE` (src) |
| 63 | 1 | `out_dtype` | (dst) |

**[HIGH/OBSERVED — `aws_neuron_isa_tpb_dma_direct2d.h:24–45`.]**

> **The fields the DGE emits from (the L1→L2 map):** `GENERATE` reads
> `{src_start_addr / dst_start_addr → addr, src_elem_size / dst_elem_size → elem_size,
> semaphore → sem_num}` + the direction tag. `DIMPUSH` reads `{src_step_elem[k] /
> dst_step_elem[k] → the signed steps, src_num_elem[k] / dst_num_elem[k] → the u16 counts}`,
> one `k` per dimension. `REGWRITE` programs a control reg before the data BDs (§3, §4).

**The validity gate** (`is_valid_dma_memcpy_direct_general`, from the header's commented
assertion DSL): `has_valid_neuron_header && has_valid_neuron_events &&
opcode == DMAMemcpy && is_valid_addr8(src/dst) &&
is_valid_dge_shape_reg(addr, step_elem[0])` (the `$S[]` shape-register bind) `&&
zero_num_step_elem_shape_reg_mode` (shape-from-register ⇒ `num/step[1] == 0`) `&&
are_valid_memcpy_dtypes && has_valid_bound_check_reg(src/dst) && is_valid_dma_configs`.
The `is_valid_dma_cce` arm adds aligned-start / aligned-step constraints when
`compute_op != NONE` (the CDMA CCE path — see [`cce-in-transfer.md`](cce-in-transfer.md)).
**[HIGH/OBSERVED — `dma_direct2d.h:63–116`.]**

### 1.3 `ADDR8` / `PSEUDO_ADDR8` — the marker byte

`NEURON_ISA_TPB_ADDR8` is a union keyed by the **high byte** (marker), `MASK = 0xFC`. From
`common.h:508–534` (byte-exact):

| marker | value | addr source / shape source | bit |
|--------|-------|----------------------------|-----|
| `IMM` | `0x00` | imm / imm | — |
| `SHAPE_REG` | `0x40` | imm / **reg** | bit6 |
| `ADDR_REG` | `0x80` | **reg** / imm | bit7 |
| `ADDR_SHAPE_REG` | `0xC0` | reg / reg | bit6\|bit7 |
| `ADDR_TBL_SHAPE_IMM` | `0x20` | **table** / imm | bit5 |
| `ADDR_TBL_SHAPE_REG` | `0x60` | table / reg | bit5\|bit6 |
| `+ADDR_TBL_OFFSET_REG` | `+0x08` | table offset from register | bit3 |
| `+ADDR_TBL_OFFSET_WIDE` | `+0x04` | 64-bit dual-reg offset | bit2 |

When `SHAPE_REG` (bit6) is set, the runtime-sized transfer forces
`src/dst_num_elem == 0` and `src/dst_step_elem[1] == 0` (`zero_num_step_elem_shape_reg_mode`)
— the count comes from the register, not the immediate field. `PSEUDO_ADDR8` adds one bit,
`VAR_ID_BIT = (0x1 << 4) = 0x10` (`common.h:589`), selecting the `addr_var` union arm (a
compiler variable id) over a literal address; `MASK = 0xFC` unchanged. **[HIGH/OBSERVED.]**

### 1.4 `DGE_COMPUTE_OP` (the +15 reduce mode, resolved word)

`enum NEURON_ISA_TPB_DGE_COMPUTE_OP` (`common.h:837–843`):
`NONE=0x00` (B=A, plain memcpy), `ADD=0x01` (B+=A), `MULTIPLY=0x02` (B*=A),
`MAX=0x03` (B=max(A,B)), `MIN=0x04` (B=min(A,B)). Non-NONE routes through the CDMA CCE
in-flight compute engine; the Pool descriptor dump prints it as `compute_op: 0x%x` (§3.4).
**[HIGH/OBSERVED.]**

### 1.5 `EVENTS` / `BOUND_CHECK_REG` / `DMA_CONFIGS`

`NEURON_ISA_TPB_EVENTS` (8 B, `common.h:418–424`):
`wait_mode(1) wait_idx(1) update_mode(1) update_idx(1) semaphore_value(u32)`. This is the
per-instruction events gate (e.g. `wait_mode = 0x04` `WAIT_FOR_SEM_GT_IMM` — wait until
sem **strictly >** value; `0x05` is the **≥** form — and `update_mode = inc-on-complete`;
see [field-tables §4.13](descriptor-ring-field-tables.md)) — **distinct** from the DGE's own
`semaphore`/`sem_increment` completion fields.

`NEURON_ISA_TPB_BOUND_CHECK_REG` (1 B, `common.h:707–711`):
`bc_reg:6` (and `bc_reg+1` holds the high 32 bits in wide-offset mode),
`bc_disable_oob_error_notif:1`, `bc_enabled:1`.

`NEURON_ISA_TPB_DMA_CONFIGS` (1 B, `common.h:713–716`):
`priority_class:3` (valid 0..4, the 5-class QoS band — see
[`dge-builder-qos.md`](dge-builder-qos.md)), `reserved_bitfield:5`. **[HIGH/OBSERVED.]**

---

## 2. The DGE_OPCODE field at +15 — the field the task asks for

### 2.1 The pseudo carrier (`PSEUDO_DMA_DIRECT2D`, 0xd4)

`NEURON_ISA_TPB_PSEUDO_DMA_DIRECT2D_STRUCT`, `ISA_STATIC_ASSERT == 64`. The compiler
interface: *"This pseudo instruction will serve as the compiler interface … Runtime will
translate this into a hardware DMAMemcpy instruction by using the variable ids to populate
the absolute mem pattern."* It is byte-identical to the §1.2 DIRECT2D struct with **two**
field substitutions:

| off | DIRECT2D (0xb8) | PSEUDO_DMA_DIRECT2D (0xd4) |
|-----|-----------------|---------------------------|
| **+15** | `compute_op` (`DGE_COMPUTE_OP`) | **`dge_op` (`NEURON_ISA_TPB_DGE_OPCODE`)** |
| +16 / +40 | `ADDR8` (absolute) | `PSEUDO_ADDR8` (adds `addr_var` / `addr_unknown`) |

**So the "DGE_OPCODE field within the 64-byte TPB slot" is `PSEUDO_DMA_DIRECT2D.dge_op` at
OFFSET +15.** `enum NEURON_ISA_TPB_DGE_OPCODE` (`common.h:829–834`):

| `dge_op` | value | lowers to opcode |
|----------|-------|------------------|
| `DMA_DIRECT2D` | `0x0` | `0xb8` |
| `DMA_INDIRECT1D` | `0x1` | `0xbb` |
| `DMA_TRANSPOSE` | `0x2` | `0xbd` |
| `DMA_GATHER_TRANSPOSE` | `0x3` | `0xf1` |

**[HIGH/OBSERVED — `aws_neuron_isa_tpb_pseudo_dma_direct2d.h:95–116`; `common.h:829–834`.]**
The pseudo's `src_source` / `dst_source` selector (header prose) picks where the start address
comes from: instruction-immediate / register / table-address / compiler-variable.

### 2.2 ⚠ The +15 byte is per-opcode-typed — and the resolved words are NOT a uniform template

> **CORRECTION / GOTCHA — do not over-generalize "+15 = dge_op-or-compute_op".** Offset +15
> holds a *different field in every resolved DGE struct*, and the resolved words are **four
> separate 64-byte layouts**, not the DIRECT2D template with field swaps. Verified byte-exact:
>
> | resolved struct | byte0 | +15 field | other deltas vs DIRECT2D |
> |-----------------|-------|-----------|--------------------------|
> | `DMA_DIRECT2D` | `0xb8` | `compute_op` | — (the reference layout) |
> | `DMA_INDIRECT1D` | `0xbb` | `flags` (`DMA_INDIRECT_FLAGS`) | `compute_op@60`, `src_idx_start_addr@48`, `dst_idx@52`, scalar `src/dst_num_elem` (`dma_indirect1d.h:24–47`) |
> | `DMA_DIRECT2D_XPOSE` | `0xbd` | `out_dtype` | **tile fields**: `tile_src_row_step@36`, `tile_src_rows@60`, `tile_src_cols@61`, `semaphore@62`, `dma_configs@63`; `in_dtype@14` (`dma_direct2d_xpose.h:24–42`) |
> | `DMA_GATHER_XPOSE` | `0xf1` | `src_idx_bound_reg` | gather index + data dst layout (`dma_gather_xpose.h`) |
>
> **The uniform `dge_op@+15` field exists only in the compiler PSEUDO.** Pseudo→resolved is a
> full re-pack (a relocation pass populating the per-kind struct from the variable ids), **not**
> a byte0 rewrite over a shared layout. The §8 worked transpose names this explicitly. The byte0
> family bit `0b110xxxxx` lets the runtime cheaply recognize "this is a pseudo to expand."
> **[HIGH/OBSERVED — the four struct headers; the relocation step INFERRED-HIGH.]**

The compiler emits the pseudo (0xd4) with `dge_op` set; the runtime's pseudo-expansion pass
resolves the `PSEUDO_ADDR8` variable ids to absolute `ADDR8`, picks the target struct by
`dge_op`, and the device DGE consumes the resolved word and runs the §3 emit.

---

## 3. Layer 2 — the three emit micro-ops, byte-decoded

These are the device DGE's descriptor-**program** push primitives — the internal calls that
build a `$S[]` shape-register descriptor and the BD stream. They are **not** 64-byte slots.
Their format strings were carved from the `CAYMAN_NX_POOL_DEBUG` dram0 `.rodata`
blob inside `libnrtucode_internal.so` (image base symbol `CAYMAN_NX_POOL_DEBUG_DRAM_get.data`
@ host file-offset `0x1cdc40`, blob size **28448 B**) and decoded against the §1 struct:

| dram0 blob off | dram0 VMA | string |
|----------------|-----------|--------|
| `0x37ea` | `0x837ea` | `S: push DIMPUSH to DMA[%d]: [%u,%u][%d,%d]` |
| `0x3821` | `0x83821` | `S: push GENERATE to DMA[%d]: %s : addr=0x%llx, elem_size=%d, sem_num=%i` |
| `0x386a` | `0x8386a` | `RD\0` then `WR\0` (the GENERATE `%s` direction tags, inline) |
| `0x3870` | `0x83870` | `S: push REGWRITE to DMA[%d]` |

**[HIGH/OBSERVED — carved + offset-verified: `DIMPUSH 0x37ea`, `GENERATE 0x3821`,
`RD 0x386a` / `WR 0x386d`, `REGWRITE 0x3870`; the bytes between elem_size string end and the
REGWRITE string are `… 0a 00 52 44 00 57 52 00 …` = `…\n\0RD\0WR\0…`.]**

### 3.1 `GENERATE` — emit one data-transfer descriptor

```c
// "push GENERATE to DMA[%d]: %s : addr=0x%llx, elem_size=%d, sem_num=%i"
// Pushes ONE data-transfer descriptor onto channel DMA[d]. [HIGH/OBSERVED string;
// field map HIGH/INFERRED over the §1 DIRECT2D struct.]
void push_GENERATE(int d,               // %d : target DMA channel (MEMCOPY_DMA_CFG index space)
                   const char *dir,      // %s : "RD" (M2S/read, src→buf_ptr) | "WR" (S2M/write, dst→buf_ptr)
                   uint64_t addr,        // %llx: 64-bit SoC addr -> SDMA_CME_BD_DESC.buf_ptr (+8);
                                         //        from DIRECT2D src_start_addr(+16) / dst_start_addr(+40) ADDR8
                   int elem_size,        // %d : per-element bytes -> src_elem_size(+36) / dst_elem_size(+60);
                                         //        combined with DIMPUSH counts -> BD length_meta (64-KiB-chunked)
                   int sem_num);         // %i : completion semaphore (bumped @dmacomplete);
                                         //        DIRECT2D semaphore(+13) / sem_increment(+14)
```

`GENERATE` emits exactly one descriptor. For an indirect gather it is called once per gathered
row (the per-row address is the index-driven address-gen result; see
[`gather-scatter-descriptors.md`](gather-scatter-descriptors.md)). The `RD`/`WR` tag is the
single direction bit threaded through all three layers: `RD` = M2S/read (HBM→local, doorbell
`TDRTP_inc`), `WR` = S2M/write (local→HBM, doorbell `RDRTP_inc`).

### 3.2 `DIMPUSH` — push one loop / dimension level

```c
// "push DIMPUSH to DMA[%d]: [%u,%u][%d,%d]"
// Pushes ONE nesting level. Advances BOTH cursors (src + dst) in lock-step for this dim.
// [op + two-pair format HIGH/OBSERVED; src/dst-leg pairing MED — read from the two-pair
//  string shape + the symmetric §1 struct arrays.]
void push_DIMPUSH(int d,
                  uint16_t src_num,  uint16_t dst_num,   // [%u,%u] = the SRC, DST u16 counts of THIS dim
                                                         //   from src_num_elem[k](+32+2k) / dst_num_elem[k](+56+2k)
                  int32_t  src_step, int32_t  dst_step); // [%d,%d] = the SRC, DST i32 SIGNED strides of THIS dim
                                                         //   from src_step_elem[k](+24+4k) / dst_step_elem[k](+48+4k)
```

`src/dst_step_elem` are **int32 SIGNED** — negative or permuted strides are exactly the
transpose stride-permutation (§5.3). One `DIMPUSH` per tensor dim builds the N-D walk the SDMA
engine expands into BDs (§5).

### 3.3 `REGWRITE` — inline control / trigger register-write

```c
// "push REGWRITE to DMA[%d]"   (no field args in the string — the leanest op)
// An inline control/register-WRITE entry into the descriptor stream. Programs a control reg
// (e.g. sdma_bcast_base — the broadcast base the Q7 SW-DGE writes before the data BDs;
// cf. the "Trigger DMA; sdma_bcast_base = 0x%08x" string, §5.5) AHEAD of the data BDs.
// Maps to a WORD1 CME control optype on the 16-B BD (vs the COPY op of the GENERATE data BD).
// [op + control role HIGH/OBSERVED; exact WORD1 optype MED — FLIX-desynced body.]
void push_REGWRITE(int d);
```

> **QUIRK — REGWRITE was retired on NC-v4+.** The `push REGWRITE` string is present in the
> CAYMAN and MARIANA firmware dram0 blobs but **absent** in MARIANA_PLUS and MAVERICK (SUNDA
> ships no DGE emitter). Byte-confirmed by the per-image presence histogram across the 15
> embedded firmware images: `GENERATE` appears in **15** images, `DIMPUSH` in **15**, `REGWRITE`
> in only **10** — exactly the CAYMAN+MARIANA (×5 engines × DEBUG/PERF) set. On NC-v4+ the
> control write folds into the reshape fast-path. **[HIGH/OBSERVED — `strings … | rg -c` this
> session: 15 / 15 / 10.]**

### 3.4 The descriptor-dump log = the emitted `$S[]` shape-register descriptor — and the dim count

Each backend dumps the descriptor it built; **the field count IS the dim count** (§5.1). All
three carved byte-exact from the NX_POOL dram0 blob:

```
Pool (2-dim, the default firmware backend):
  S: DGE $S[%i]+=%i@dmacomplete src=[0x%x]@0x%llx[0x%x,0x%x][%u,%u]
     dst=[0x%x]@0x%llx[0x%x,0x%x][%u,%u] cast:0x%x->0x%x
     bounds:(%1d 0x%llx<=0x%llx, %1d 0x%llx<=0x%llx) compute_op: 0x%x

software (4-dim, the SWDGE Q7 backend):
  S: DGE software backend: $S[%i]+=%i@dmacomplete src=[0x%x]@0x%llx[0x%x,0x%x,0x%x,0x%x][%u,%u,%u,%u]
     dst=[...][...] cast:0x%x->0x%x, indirection_dim=0x%x, reshape_kind=0x%x

RTL (5+2-dim, the HWDGE backend):
  S: DGE RTL backend: $S[%i]+=%i@dmacomplete src=[0x%x]@0x%llx[0x%x,0x%x,0x%x,0x%x,0x%x|0x%x,0x%x][%u,%u,%u,%u,%u|%u,%u]
     dst=[...|...][...|...]
```

Decode: `$S[i] += sem_inc @dmacomplete`; `src = [bound_reg]@addr8 [step…][num…]`; `dst`
likewise; `cast = in_dtype → out_dtype`; the Pool dump's two `bounds:` pairs are the
`BOUND_CHECK_REG` predicate (`addr <= limit`, src then dst); `compute_op` is the §1.4 reduce
mode. **The bracketed `[steps][nums]` per leg are exactly the `DIMPUSH`-pushed values** — this
dump is the authoritative cross-check on the §1 struct arrays. Only the Pool line carries
`bounds:` and `compute_op:`; RTL is pure wide transport; software carries `indirection_dim` /
`reshape_kind`. **[HIGH/OBSERVED — all three strings carved from the NX_POOL dram0
blob.]**

---

## 4. The stream assembly — how a 64-B word becomes a DGE micro-op program

Per channel `DMA[d]`, the DGE assembles the descriptor program in a fixed order:

```
  (1) [REGWRITE]*    zero or more control reg-writes  (CAYMAN/MARIANA only; gone NC-v4+, §3.3)
                     e.g. programs sdma_bcast_base
  (2) GENERATE       ONE data descriptor: {addr, elem_size, sem_num} + RD/WR direction
  (3) DIMPUSH × Ndim ONE per tensor dim: [num,num][step,step]; Ndim = backend dim count
                     (Pool=2, software=4, RTL=5+2 — §5.1)
  (4) DOORBELL       bump M2S TDRTP_inc (RD) / S2M RDRTP_inc (WR) by num_descriptors.
                     For Q7 SW-DGE: "Trigger DMA; sdma_bcast_base=…, trigger addr=…,
                                     mask=…, n_desc=%d"
```

The composed descriptors **expand** into 16-byte `SDMA_CME_BD_DESC` entries in the
`DGE_MEMORY` carveout (16-B unit; see [`descriptor-ring-field-tables.md`](descriptor-ring-field-tables.md)).
**Three bound layers** gate every build: per-descriptor ADDRESS bound (`BOUND_CHECK_REG`, the
§3.4 `bounds:` pairs); total-BYTE equality (`src_num*src_elem == dst_num*dst_elem`); and
descriptor-COUNT vs ring (the Q7 `"ERROR: DescriptorStream wrote %d descriptors, expected %d"`,
§5.5). **[HIGH/OBSERVED — the doorbell + count-check strings carved; the assembly
order CARRIED + firmware-grounded.]** The device-side control flow of this loop is on
[`../firmware/dge/dge-emit.md`](../firmware/dge/dge-emit.md).

---

## 5. The multi-dim loop-nest → DIMPUSH compilation (order + dim count)

This is the heart of "how the N-D loop nest compiles into a DIMPUSH sequence."

### 5.1 The dim count per backend

| backend | #DIMPUSH | descriptor form | expansion | `$S` dump arity |
|---------|----------|-----------------|-----------|-----------------|
| Pool | 2 | DIRECT2D (2-D) | firmware | `[s0,s1][n0,n1]` |
| software | 4 | SWDGE/Q7 4-D | Q7 walk | `[s0..s3][n0..n3]` |
| RTL | 5+2 | HWDGE 5+2-D | hardware (RDM) | `[s0..s4\|s5,s6][n0..n4\|n5,n6]` |

`#DIMPUSH issued == the descriptor's dim count for the chosen backend.` The `$S[]` shape
register file is **4 deep** (`NEURON_ISA_TPB_NUM_DGE_SHAPE_REGISTERS = 4U`, `common.h:34`).
The working `dge_shape { elem_size:u16, num_elem[4]:u16, step_elem[4]:i32, is_src:bool }` is
filled by the reshape engine BEFORE emit. The Pool 2-D form folds into the DIRECT2D struct's
`src/dst_step_elem[2] + num_elem[2]`; the 4-D / 5+2-D forms are the extension-instruction
descriptors. **[HIGH/OBSERVED — §3.4 field counts + `NUM_DGE_SHAPE_REGISTERS`.]**

### 5.2 The order (reshape → emit)

1. **Reshape analysis** (carved NX_POOL dram0, byte-exact):
   ```
   S: DGE Reshape: Analyzed tensor (nelem_target = %u, step_target = 0x%X (%dP),
      base = 0x%X (%dP)): Reshape Strategy = %d, Requested Reshape Kind = %d,
      Partition Range = %d, Num Partitions = %d
   S: DGE Reshape: Assessed tensor pair params (Requested Reshape Kind = %d,
      Min Num Partitions = %u, #DMA Avail = %u): Reshape Kind = %d, #DMA = %u
   ```
   Per tensor: count, target stride in **partitions** (`%dP`), base, a strategy + kind,
   partition range/count; then the *pair* assess resolves the FINAL Reshape Kind + the `#DMA`
   fan-out. See [`dge-reshape.md`](../firmware/dge/dge-reshape.md).
2. The reshape fills `dge_shape { num_elem[4], step_elem[4] }` post-transpose. A transpose is
   realized as a **permutation** of the `step_elem[] / num_elem[]` arrays (the
   `tensor_reshape_indirect_transpose BEFORE/AFTER` strings, §5.5).
3. The DGE issues **one `DIMPUSH` per dim, in nesting order (outer-to-inner)**, each pushing
   that dim's `(src_num,dst_num)(src_step,dst_step)`. The Q7 SW-DGE descriptor-generation loop
   walks the index explicitly: `Descriptor generation loop index: [%d, %d, %d, %d, %d]`
   (a 5-D counted nest, §5.5). The inner two dims may fold into a single 2-D strided BD via the
   DIRECT2D `step/num[2]`.

**[HIGH/OBSERVED — the reshape + loop strings carved; the one-DIMPUSH-per-dim
mapping HIGH/INFERRED over the OBSERVED `$S` dump arity.]**

### 5.3 Transpose = signed-stride permutation

Because `src/dst_step_elem` are int32 SIGNED, a transpose is a stride **reorder** (plus a sign
flip for reverse-axis walks). For a 2-D transpose the dst dim-0 step is the source's slow-axis
step and vice versa; the DGE pushes the swapped pattern via `DIMPUSH`, and the SBUF `axi2sram`
crossbar (`transpose_en` set) lands the 16×N tile transposed. Both the non-indexed
`DMA_TRANSPOSE` (`0xbd`, `dge_op` kind 2) and the indexed `GATHER_XPOSE` (`0xf1`, kind 3) drive
this. **[HIGH/OBSERVED step-sign field + the axi2sram port; the stride-swap mechanism CARRIED.]**

### 5.4 The compiler side of the loop nest (the NEFF descriptor)

The compiler's per-DMA access pattern (the NEFF `pool.json` `dma[].desc[]`) carries
`{from_sizes[N], from_steps[N], to_sizes[N], to_steps[N]}` — the LOGICAL N-D loop nest. Each
`(sizes[k], steps[k])` maps **1:1** to a `DIMPUSH [num,num][step,step]`; `from_*` is the src
leg, `to_*` is the dst leg. The `bir::InstDMA` carries `DGEType` (SWDGE/HWDGE, selecting the
backend dim count) + `DMAQoSClass`. **[HIGH/OBSERVED — the NEFF JSON; the desc→DIMPUSH 1:1
map INFERRED-HIGH over §3.2 + §1.]** See [`dge-builder-qos.md`](dge-builder-qos.md).

### 5.5 The Q7 SW-DGE descriptor-gen pipeline

Carved Q7_POOL dram0, every string byte-exact (the SWDGE realization of the §4
stream for the indirect/transpose case):

| string | role |
|--------|------|
| `P%i: DGE DIRECT2D` / `DGE INDIRECT` / `DGE GATHER TRANSPOSE` | the `dge_op` kind dispatch (0/1/3) at gen head |
| `gen_spray_info: reshape_kind=%d, do_indirection=%d, is_our_tensor=%d, hbm_to_hbm=%d, ndma=%zu` | spreads the transfer across `ndma` channels |
| `gen_spray_info: applying indirection reshape` / `applying tensor reshape for our tensor` / `skipping reshape (…)` | per-tensor reshape decision |
| `tensor_reshape_indirect_transpose BEFORE/AFTER: elem_size=%u num_elem=[%u,%u,%u,%u] step_elem=[%d,%d,%d,%d] is_src=%d` | the **4-D** dge_shape before/after the transpose stride-permute (§5.3) — proves the 4-D software form |
| `tensor_reshape_indirect_transpose: tile_src_rows=%d tile_src_cols=%d dtype_size=%d` | the xbar tile geometry |
| `make_gather_pattern START: reshape_kind=%d, step_port=%d, step_half=%d, eng_mask=0x%x` / `END: pattern=0x%x, mask=0x%x` | builds the gather access pattern + mask |
| `Tensor shape before Batman: num_elem=[%u,%u,%u,%u,%u] step_elem=[%d,%d,%d,%d,%d]` | the internal "Batman" **5-D** reshape along the shared indirection dim |
| `Generating %i descriptors; ring_ndesc = %i` | descriptor count + ring depth |
| `Descriptor generation loop index: [%d, %d, %d, %d, %d]` | the **5-D** counted emit nest |
| `Trigger DMA; sdma_bcast_base = 0x%08x, trigger addr = 0x%08x, mask = 0x%08x n_desc=%d` | the broadcast doorbell (the REGWRITE-programmed base + tail bump by `n_desc`; §4 step 4) |
| `Q7: rdma_desc_gen [%s] …` / `Q7: rdma_desc_start [%s] ring_num=%d, num_descriptors=%d, sdma_bcast_base=0x%08x` | the P2P RDMA gen/start pair (cross-core, the local/remote sema BDs) — see [`rdma-cross-die.md`](rdma-cross-die.md) |
| `ERROR: DescriptorStream wrote %d descriptors, expected %d` | the descriptor-COUNT bound check (§4) |

**[HIGH/OBSERVED — every string carved from `CAYMAN_Q7_POOL_DEBUG` dram0; the
IRAM disassembles clean under native ncore2gp (windowed `l32e`/`s32e` prologue, `rsr.excvaddr`
exception handler, `entry`/`retw`/`rfwo`/`rfwu` × 1553 instances counted). The per-bundle FLIX
encoding of the gen loop sits in a literal-desynced region (MED) — see §7.]**

---

## 6. The v5 (MAVERICK) inline-descriptor model

NC-v5 supersedes the `GENERATE`/`DIMPUSH`/`REGWRITE` emit with **two inline-descriptor ops**
whose descriptor bytes ride INSIDE the 64-byte instruction. Both read byte-exact from the
maverick headers (`ISA_STATIC_ASSERT == 64`).

> **v5 WALL — the inline-descriptor *interior* is header-OBSERVED only.** The struct field
> layouts below are byte-exact in the shipped maverick headers, but a carved MAVERICK firmware
> image to disassemble the `DMA_IMMEDIATE` dispatch handler and confirm the runtime BD packing
> was **not** decoded. Claims about how `DESCRIPTOR_RAW[16]` maps onto the 16-B
> SDMA BD at runtime, and whether v5 still emits `DIMPUSH` internally, are **INFERRED**.

### 6.1 `DMA_IMMEDIATE` (opcode 0xba) — "send 1–3 immediate descriptors to DGE"

`NEURON_ISA_TPB_DMA_IMMEDIATE_STRUCT`, 64 B (`dma_immediate.h:36–46`):

| off | size | member | detail |
|-----|------|--------|--------|
| 0 | 4 | `header` | |
| 4 | 4 | `wait_sema_value` | u32 (imm or reg) |
| 8 | 1 | `wait_sema_idx` | |
| 9 | 1 | `wait_sema_mode` | `WAIT_MODE` |
| 10 | 1 | `dma_engines` | `DMA_ENGINE_CONFIG` (**restricted to 1 engine** here) |
| 11 | 1 | `dma_flags` | `DMA_FLAGS` (4b `queue_id`, `tdg` must be 0) |
| 12 | 1 | `descriptor_flags` | `DESC_FLAGS` (num_descriptors + per-desc src) |
| 13 | 3 | `descriptor_reg[3]` | register triplet for REGISTER-mode descriptors |
| 16 | 48 | `descriptor_imm[3]` | `DESCRIPTOR_RAW[3]` = **3 × 16-B inline descriptors** |

Up to **three** 16-B SDMA descriptors carried inline at `@16–63`, fired straight at the DGE —
the L3 16-B BD folded into L1. **[HIGH/OBSERVED — header; runtime BD mapping INFERRED — v5 wall.]**

### 6.2 `DMA_MEMCPY2` (opcode 0xb9) — v5 2-D copy with native sem wait/update

`NEURON_ISA_TPB_DMA_COPY2D_STRUCT`, 64 B (`dma_copy2d.h:22–43`):

| off | member | detail |
|-----|--------|--------|
| 0 | `header` | |
| 4 | `wait_sema_value` | u32 |
| 8 / 9 | `wait_sema_idx` / `wait_sema_mode` | |
| 10 | `dma_engines` | `DMA_ENGINE_CONFIG` (5b start ID, 2b count 1/2/4/8) |
| 11 | `dma_flags` | 4b `queue_id`, 1b `tdg` |
| 12 | `addr_mode` | `DMA_ADDR_MODE_PAIR` (low4 = src, high4 = dst) |
| 13 | `sema_update_mode` | `DMA_SEMA_UPDATES` (`rd_done:4`, `wr_done:4`) |
| 14 | `rd_done_sema_update_idx` | u16 |
| 16 | `src_start_addr` | `DMA_ADDR_UNION` (imm64 \| `dma_addr_info`) |
| 24 / 32 / 36 | `src_step_elem[2]` i32 / `src_num_elem[2]` u16 / `src_elem_size` u16 | |
| 38 | `wr_done_sema_update_idx` | u16 |
| 40 / 48 / 56 / 60 | `dst_start_addr` / `dst_step_elem[2]` / `dst_num_elem[2]` / `dst_elem_size` | |
| 62 | `remote_core_id` | `REG_NUM` — the cross-die peer |
| 63 | `reserved` | |

Header prose: *"Next-generation DMA copy … with explicit semaphore wait/update, configurable
DMA engine selection, and loop-friendly address modes."* The 2-D `step/num` arrays are the same
loop nest the `DIMPUSH` stream carried, now native fields. **[HIGH/OBSERVED — header.]**

### 6.3 The v5 descriptor enums/structs (maverick `common.h`, byte-exact)

| symbol | definition | source |
|--------|-----------|--------|
| `DESCRIPTOR_RAW` | `{ uint8 bytes[16] }` | `common.h:1096` |
| `DESC_FLAGS` | `{ num_descriptors:2, desc0_src:1, desc1_src:1, desc2_src:1, reserved:3 }` | `common.h:1100` |
| `DMA_DESC_MODE` | `IMMEDIATE=0` (16-B imm in inst) \| `REGISTER=1` (4×32-b reg quad `descN_src+0..3 << 0/32/64/96`) | `dma_immediate.h:52` |
| `DMA_ENGINE_CONFIG` | `{ start_id:5, engine_count:2, reserved:1 }` | `common.h:1030` |
| `DMA_ENGINE_COUNT` | `ONE=0, TWO=1, FOUR=2, EIGHT=3` (the **1/2/4/8** fan-out) | `common.h:1023` |
| `DMA_FLAGS` | `{ tdg:1, wr_done_sync(SEMA_SYNC):1, queue_id:3, reserved:3 }` | `common.h:1041` |
| `SEMA_SYNC` | `SOW=0, CLEAR_PCIE_RO=1` (PCIe relaxed-order clear) | `common.h:1036` |
| `DMA_ADDR_MODE_PAIR` | `{ src_addr_mode:4, dst_addr_mode:4 }` | `common.h:1076` |
| `DMA_ADDR_STRUCT` | `{ base_reg, offset_reg, inc_reg, reserved[1], offset_imm(u32) }` | `common.h:1048` |
| `DMA_ADDR_UNION` | `{ imm64 \| dma_addr_info(DMA_ADDR_STRUCT) }` | `common.h:1056` |

**The 12 `DMA_ADDR_MODE`s** (`common.h:1061–1074`):

| mode | val | mode | val |
|------|-----|------|-----|
| `IMM64` | `0x0` | `REG_REG_OFFSET64` | `0x6` |
| `REG_PAIR` | `0x1` | `REG_REG_OFFSET64I` | `0x7` |
| `REG_IMM_OFFSET32` | `0x2` | `REG_REG_IMM_SCALE` | `0x8` |
| `REG_IMM_OFFSET32I` | `0x3` | `REG_REG_IMM_SCALE_I` | `0x9` |
| `REG_REG_OFFSET32` | `0x4` | `REG_REG_REG_SCALE` | `0xa` |
| `REG_REG_OFFSET32I` | `0x5` | `REG_REG_REG_SCALE_I` | `0xb` |

The register-rich loop-addressing modes carry base/offset/inc regs + a 32-bit immediate — they
let a v5 `DMA_MEMCPY2` advance its address from registers per loop iteration without re-emitting.

**`DMA_SEMA_UPDATES { rd_done:4, wr_done:4 }`** where each 4-bit field is a
`DMA_SEMA_UPDATE_MODE` (`common.h:1081–1089`):

| mode | val | bits (3=local/remote, 2=sem/coll, 1=inc/inc_counter, 0=enabled) |
|------|-----|----------------------------------------------------------------|
| `NONE` | `0x0` | disabled |
| `LOCAL_SEM_INC` | `0x1` | `0b0001` |
| `LOCAL_SEM_INC_COUNTER` | `0x3` | `0b0011` |
| `LOCAL_COLLSYNC_INC` | `0x5` | `0b0101` |
| `REMOTE_SEM_INC` | `0x9` | `0b1001` |
| `REMOTE_SEM_INC_COUNTER` | `0xb` | `0b1011` |
| `REMOTE_COLLSYNC_INC` | `0xd` | `0b1101` |

So a v5 DMA's read-done and write-done **each independently** increment a semaphore OR a
collective-sync block, **locally OR at a remote peer** — the v5 successor to the cayman RDMA
gen/start local/remote sema BDs. **[HIGH/OBSERVED — every enum/struct read.]**

### 6.4 v5 firmware confirms the model

The maverick Q7_POOL DEBUG image still carries `DGE DIRECT2D`/`INDIRECT`/`GATHER` + the
descriptor-generation loop strings — the SW-DGE descriptor *classes* persist (`DGE DIRECT2D`
counts **7** occurrences across the embedded images). What changed at v5 is the
**instruction-side** encoding: the inline `DMA_IMMEDIATE`/`DMA_MEMCPY2` descriptors over the
cayman SDMA-ring + `DIMPUSH`-emit model, plus the `REGWRITE` retirement (§3.3) and the EVENT
primitive retirement. The `push DIMPUSH/GENERATE/REGWRITE` strings are CAYMAN/MARIANA only —
consistent with v5 having moved to the inline model. **[HIGH/OBSERVED string survival;
CARRIED/INFERRED for the runtime split — v5 wall.]**

---

## 7. A real DGE program — disassembled from carved firmware + a carved NEFF

Two independent OBSERVED groundings.

### 7.1 The firmware (carved CAYMAN_NX_POOL_DEBUG, native ncore2gp)

The CAYMAN NX_POOL DEBUG IRAM image (`CAYMAN_NX_POOL_DEBUG_IRAM_get.data` @ host file-offset
`0x1b1420`, **116,768 B**) disassembles cleanly under the shipped `ncore2gp`
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) — a valid Cairo Vision-Q7 image:

```
00000000 <.data>:
   0:  067600    j      0x1dc        ; reset vector (iram0 base maps to memmap 0x4)
   6:  867700    j      0x1e8
  24:  800109    l32e   a8, a1, -64  ; windowed-ABI register-spill prologue
  27:  900109    l32e   a9, a1, -64
  3c:  803149    s32e   a8, a1, -52
  ...                                 ; rsr.excvaddr exception handler present
```

(native ncore2gp output — `entry`/`retw`/`rfwo`/`rfwu`/`rsr.excvaddr` ×1553).
The DGE emit machinery is byte-PRESENT in the paired dram0 blob: the `GENERATE`/`DIMPUSH`/
`REGWRITE` push strings (§3), the Pool/RTL/software `$S[]` dumps (§3.4), the reshape
analyze/assess strings (§5.2), and the DGE-context-setup strings
`S: DGE contexts 0x%x/0x%x init sw=%d engine=%u queue_idx=%u queue_num=%u` +
`S: rx.base=0x%llx, tx.base=0x%llx`. The Q7_POOL dram0 blob carries the full §5.5
descriptor-generation pipeline.

> **FLIX/literal-desync WALL.** The per-bundle FLIX encoding of the *emit functions* sits in a
> literal-desynced region: objdump cannot bind the dram0-relative format-string load because
> the strings are accessed via a base-pointer (the dram0 base, `0x83400`-rooted) + computed
> offset, not an absolute `l32r`. Bundle-decode resync past this point is uncertain. The
> **string-level program + the struct/enum encodings are byte-OBSERVED**; the per-instruction
> emit body is MED. **[HIGH/OBSERVED image + strings; per-instruction body MED.]**

> **NOTE — sha drift.** The IRAM image carved hashes differently from the value a
> prior pass recorded for the same getter symbol; the **structure** (reset-vector `j`, windowed
> prologue, dram0 blob string offsets `0x37ea`/`0x3821`/`0x3870`) matches exactly, so this is a
> rebuild/version delta, not a different image. Pin the image by getter symbol + dram0 string
> offsets, not by content hash.

### 7.2 The NEFF — a real 2-D DGE memcpy (C08220 sg00)

The compiler's DGE access patterns from the NEFF `pool.json` `dma` array (sample-OBSERVED):

```
dma id 0  queue "q_gradient_in"  desc[0] = {
  from="input", to="gradient_in",
  from_sizes=[128,1], from_steps=[1,128], to_sizes=[128,1], to_steps=[1,128] }
dma id 0  queue "q_gradient_out" desc[0] = {
  from="gradient_out", to="output",
  from_sizes=[128,1], from_steps=[1,128], to_sizes=[128,1], to_steps=[1,128] }
```

`def.json` bindings: `input(var_id 0, element_size 4 = FP32, size 128)`, `output(var_id 2)`,
`gradient_in/out` (tmp-bufs); `q_gradient_in {owner pool, semaphore 10, type in}`,
`q_gradient_out {owner pool, semaphore 11, type out}`.

**How this program compiles, end to end (the §1–§5 chain):**

```
[compile] bir::InstDMACopy -> PSEUDO_DMA_DIRECT2D (opcode 0xd4), dge_op = DMA_DIRECT2D (0),
          src/dst PSEUDO_ADDR8 = compiler vars input / gradient_in.
[load]    runtime resolves the var ids to absolute ADDR8, packs the resolved
          DMA_DIRECT2D_STRUCT and rewrites byte0 -> DMAMEMCPY (0xb8).
          dma_configs.priority_class from DMAQoSClass; semaphore=10, sem_increment=1.
[device]  DGE setup binds the (tx,rx) context for q_gradient_in (the "DGE contexts … init"
          string); reshape resolves the 2-D pattern [128,1]/[1,128]; backend = Pool (2-dim).
          dge_shape = { elem_size=4, num_elem=[128,1], step_elem=[1,128], is_src } for src;
          { [128,1], [1,128] } for dst.
[emit]    on DMA[d]:  GENERATE  (RD, addr=&input, elem_size=4, sem_num=10)
                      DIMPUSH dim0  [128,128][1,1]      ; src_num=128 dst_num=128, steps 1/1
                      DIMPUSH dim1  [1,1][128,128]      ; src_num=1   dst_num=1,   steps 128/128
          -> ONE GENERATE + 2× DIMPUSH (Pool = 2 dims). src==dst (straight copy, no transpose).
[expand]  the DIRECT2D word -> 16-B SDMA_CME_BD_DESC (128×1×4 = 512 B, one chunk) in DGE_MEMORY.
[fire]    TDRTP_inc doorbell (RD leg). Completion bumps semaphore 10.
```

The `q_gradient_out` leg is the symmetric WRITE (S2M/`RDRTP_inc`, sem 11). **[HIGH/OBSERVED —
the NEFF JSON; the compile→emit chain INFERRED-HIGH over the §1–§5 OBSERVED encoding.]**

---

## 8. Worked multi-dim transfer — a 2-D transpose

A concrete 2-D transpose through the §1–§5 encoding. **The field placements, the `dge_op` kind,
the `DIMPUSH` sign convention, and the dim count are HIGH/OBSERVED; the specific numeric values
are illustrative (LOW).**

**GOAL:** transpose a 64(rows)×128(cols) BF16 tile from SBUF src to SBUF dst
(`dst[c][r] = src[r][c]`). `elem_size = 2` (BF16). Row-major src: row stride = 128 elem,
col = 1 elem. dst (transposed, row-major): row (was col) stride = 64, col (was row) = 1.

**COMPILER (L1, the pseudo):** `PSEUDO_DMA_DIRECT2D` (0xd4) with

```c
dge_op        = DMA_TRANSPOSE (0x2);          // [+15] selects the xbar-transpose DGE kind
src_start_addr= &src (PSEUDO_ADDR8, addr_var);// [+16]
src_num_elem  = { 128, 64 };                  // [+32] x=fast=cols, y=slow=rows
src_step_elem = { 1, 128 };                   // [+24] x step 1, y step 128 elem
src_elem_size = 2;                            // [+36]
dst_start_addr= &dst (PSEUDO_ADDR8, addr_var);// [+40]
dst_num_elem  = { 64, 128 };                  // [+56] transposed: x=rows-now-cols, y=cols-now-rows
dst_step_elem = { 1, 64 };                    // [+48] transposed strides
dst_elem_size = 2;                            // [+60]
in_dtype = out_dtype = BF16;                  // [+62/+63]
semaphore = S; sem_increment = 1; dma_configs.priority_class = 0;  // [+13/+14/+12]
```

The transpose is the SWAP of the `(num,step)` pairing between src and dst: src's slow axis
(y, step 128, count 64) becomes dst's fast axis (x, count 64); src's fast axis (x, step 1,
count 128) becomes dst's slow axis (y, count 128). §5.3.

> **GOTCHA — the resolved word is NOT a DIRECT2D with swapped strides.** When `dge_op =
> DMA_TRANSPOSE`, the runtime packs `NEURON_ISA_TPB_DMA_DIRECT2D_XPOSE_STRUCT` (byte0 = `0xbd`),
> which is a *different* 64-B layout: `+15 = out_dtype` (not `compute_op`), and it adds
> `tile_src_row_step@36`, `tile_src_rows@60`, `tile_src_cols@61`, `semaphore@62`,
> `dma_configs@63` (`dma_direct2d_xpose.h:24–42`). The tile geometry — not just swapped strides —
> drives the `axi2sram` crossbar. The §3.4 `$S[]` dump still shows the `[steps][nums]` axis-swap
> because the DGE's internal shape register captures the post-permute walk. **[HIGH/OBSERVED —
> the XPOSE struct header.]**

**DEVICE DGE EMIT (L2, Pool backend, 2 dims):**

```
setup:   "DGE contexts … init sw=0 engine=E queue_idx=Q …"
reshape: "Analyzed tensor … Reshape Kind=K"; BEFORE step_elem=[1,128] num=[128,64]
         -> AFTER permuted for the dst transpose; xbar tile 16 rows × 128 cols
            (src y-count 64 % 16 == 0, OK).
emit on DMA[d]:
  push GENERATE to DMA[d]: RD : addr=&src, elem_size=2, sem_num=S
  push DIMPUSH  to DMA[d]: [128,64][1,1]      ; dim0: src 128 / dst 64 counts, step pair 1/1
  push DIMPUSH  to DMA[d]: [64,128][128,64]   ; dim1: the SIGNED / swapped strides
fire:    TDRTP_inc by num_descriptors. The $S[] dump:
  DGE $S[i]+=1@dmacomplete src=[bnd]@&src[1,128][128,64] dst=[bnd]@&dst[1,64][64,128]
      cast:BF16->BF16 bounds:(…) compute_op:0x0
```

The descriptor's bracketed `[steps][counts]` differ src-vs-dst exactly by the axis swap = the
transpose. **[INFERRED-HIGH over the §1/§3.4/§5.3 OBSERVED encoding.]**

> **CONTRAST — the indexed gather-transpose** (`0xf1`, `dge_op` kind 3) would instead carry a
> `src_idx` vector + `gather_dim`, resolve to `DMA_GATHER_XPOSE_STRUCT`, and run the Q7 SW-DGE
> §5.5 pipeline (`make_gather_pattern` / Batman / per-index `GENERATE`), landing each gathered
> row transposed. The plain `0xbd` transpose above is the non-indexed xbar path.

---

## 9. Reconciliation

| layer | encoding | byte anchor |
|-------|----------|-------------|
| L1 | 64-B TPB slot | `opcode` byte0 (`0xd4` pseudo; `0xb8`/`bb`/`bd`/`f1` resolved; `0xb9`/`ba` v5); `dge_op`/`compute_op` @+15 |
| L2 | `GENERATE`/`DIMPUSH`/`REGWRITE` | the firmware emit strings (carved, §3); the 4-deep `$S[]` shape regs |
| L3 | 16-B `SDMA_CME_BD_DESC` | `DGE_MEMORY` carveout; v5 folds it inline as `DESCRIPTOR_RAW` |

**The +15 byte, pinned:** `PSEUDO_DMA_DIRECT2D.dge_op @+15` = `DGE_OPCODE` kind
`{DIRECT2D 0, INDIRECT1D 1, TRANSPOSE 2, GATHER_TRANSPOSE 3}`; the resolved instruction byte0
= `{0xb8, 0xbb, 0xbd, 0xf1}` respectively; `byte1 = inst_word_len = 0x10` always. In the
resolved words, +15 is `compute_op` (DIRECT2D), `flags` (INDIRECT1D), `out_dtype` (XPOSE), or
`src_idx_bound_reg` (GATHER_XPOSE) — the uniform `dge_op` field exists **only** in the pseudo.

**Confidence summary.** HIGH/OBSERVED: all L1 struct byte-layouts (`ISA_STATIC_ASSERT == 64`);
the opcode-byte map; the `DGE_OPCODE`/`DGE_COMPUTE_OP` enums; the `ADDR8`/`PSEUDO_ADDR8` markers;
`NUM_DGE_SHAPE_REGISTERS=4`; the device memory map; the `GENERATE`/`DIMPUSH`/`REGWRITE` strings +
their dram0 offsets + the RD/WR tags; the Pool(2-D)/software(4-D)/RTL(5+2-D) `$S[]` dumps; the
REGWRITE NC-v4+ retirement (15/15/10 histogram); the full Q7 SW-DGE pipeline strings; the v5
`DMA_IMMEDIATE`/`DMA_MEMCPY2` structs + all v5 enums incl. the 12 `DMA_ADDR_MODE`s; the C08220
NEFF 2-D descriptor; the native ncore2gp clean disasm. MED: the `GENERATE` field→struct map; the
`DIMPUSH` src/dst-leg pairing; the per-instruction FLIX-bundle emit body (literal-desync wall).
LOW: the §8 worked numeric values; runtime register/MMIO values. v5 inline-descriptor *interior*
runtime behavior: **INFERRED** (header-OBSERVED layout only — the v5 wall).
