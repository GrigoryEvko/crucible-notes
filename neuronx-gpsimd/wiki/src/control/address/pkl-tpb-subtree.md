# pkl TPB-Engine Subtree

The flat YAML SoC map (see [`soc-master-map.md`](./soc-master-map.md)) gives you
the *addresses* of the TPB engine blocks but throws away the structure that
produced them: the array dimensions, the parent-relative offsets, the per-node
RTL parameter overrides, and the schema each block was generated from. That
structure survives intact in the richer source database — the
RTL-generated `al_address_map_db.pkl` and its byte-identical `.json` mirror — and
this page carves the **TPB engine-block subtree** out of it: the PE / POOL / ACT /
DVE / SP sequencers, plus SBUF, EVT_SEM, COLLECTIVE_SYNC and the DGE stub, as the
DB actually encodes them.

For the load primitive and the 23-field record schema, see
[`pkl-db.md`](./pkl-db.md); for the sibling subtrees see
[`pkl-dma-subtree.md`](./pkl-dma-subtree.md) and
[`pkl-hbm-subtree.md`](./pkl-hbm-subtree.md). The block→schema cross-reference
recovered here feeds [`block-schema-xref.md`](./block-schema-xref.md).

> **⚠️ ARCH WALL — read first.** This pkl describes the **MAVERICK** SoC
> (NeuronCore-v5). Its directory is `arch-headers/maverick/ext/` and every node's
> `json` path is rooted at a `…/proj/maverick/…` build tree. The flat YAML cross-
> checked throughout these pages is **CAYMAN** (NeuronCore-v3). The DB *records*
> are **OBSERVED** — read structurally from a shipped artifact, with no
> execution. But **v5 SoC interior behaviour behind any address is INFERRED**: the
> v5 header is observed only; the silicon behind a Maverick base is not
> byte-grounded the way Cayman's is. Where a claim is the DB telling you its own
> structure it is `OBSERVED`; where it is a reading of what the silicon *does*, it
> is `INFERRED`. Cayman flat-YAML numbers are the byte-grounded anchor for any
> cross-gen comparison.

> **⚠️ SAFETY — never `pickle.load()` this file.** The pkl is a protocol-4 framed
> stream and is treated as potentially code-executing on load. Every fact on this
> page was recovered either by **`pickletools.genops()`** structural scanning (no
> object construction, `find_class` never reached) or by streaming the **`.json`
> mirror** one record at a time with a brace-counting iterator that never slurps
> the 514 MB file into memory. This is the same gate [`pkl-db.md`](./pkl-db.md)
> establishes for the whole DB. The carve pseudocode in
> [§7](#7-python-the-tpb-subtree-carve) uses the streaming path.

---

## 1. Provenance and the two artifacts (HIGH · OBSERVED)

Two shipped, RTL-generated files, both binary-derived and citeable:

| file | size (B) | role |
|------|---------:|------|
| `…/arch-headers/maverick/ext/al_address_map_db.pkl` | 216,631,794 | the DB (protocol-4 pickle) |
| `…/arch-headers/maverick/ext/al_address_map_db.json` | 514,276,583 | byte-identical text mirror |

Header (first 16 bytes, no execution): `80 04 95 08 00 01 00 00 00 00 5d 94 28 7d 94 28`
= `PROTO 4` · `FRAME(0x00010000…)` · `EMPTY_LIST` · `MARK` · `EMPTY_DICT` — i.e. the
root object is a **list of dicts**. Streaming the `.json` confirms a flat JSON
array whose first element is the `ADDRESS_MAP` root and whose total length is
**323,198 records** (matches [`pkl-db.md`](./pkl-db.md)). The schema JSONs each
node binds to live under
`…/arch-headers/maverick/vpc-mirror/arch-regs/src/{address_map,csrs}/` and were
each verified on disk.

Every record carries (at most) **23 fields** — the union across the TPB subtree is
exactly: `name short_name short_name_lc size InclSizeInBytes type offset
parent_names base address count parent_size parent_offset instance_index
parent_name_no_idx parent_array_size self_array_size as_array tag legacy
parentName parameters json`. The `ADDRESS_MAP` root omits `json` (it has no
backing schema file), so the *root record alone* shows 22 keys — the 23rd
(`json`) appears on every non-root node.

---

## 2. Where the TPB engine block lives (HIGH · OBSERVED)

The canonical TPB-engine root is **`USER_INT_SENG_0_TPB_0`**:

| field | value |
|-------|-------|
| `name` | `USER_INT_SENG_0_TPB_0` |
| `short_name` | `TPB_0` |
| `type` | `NODE` |
| `base` / `address` / `offset` | `0x0` (view-relative; SENG_0 base is 0) |
| `size` / `InclSizeInBytes` | `0x4000000000` (**256 GiB** decode window) |
| `count` | `4` (the 4 SENGs) |
| `self_array_size` | `"2"` (2 TPBs per SENG) |
| `parent_size` | `4` `parent_offset` `0` `instance_index` `"0x0"` |
| `parent_array_size` | `"1*1*4"` |
| `parent_names` | `['ADDRESS_MAP','user_int','seng_0']` |
| `tag` | `{'DIE_NAME':'C_DIE'}` |
| `json` | `…/address_map/tpb_user_address_map.json` |

The array model (`count=4 × self_array_size=2`) materializes the **8 physical
TPBs**: `USER_INT_SENG_{0..3}_TPB_{0,1}`. Each `(seng, tpb)` chain expands to
**604 descendant records**, so `8 × 604 = 4,832` engine-subtree records — the
`user_int` engine family of the DB's 42,360 TPB-named records. (The remaining TPB
records are the `secure_int` PEB protection plane and the `APB_IO` host-visible
control plane, covered by the sprot and arr-seq pages, not here.)

The extraction prefix is `parent_names[:4] == ['ADDRESS_MAP','user_int','seng_0','tpb_0']`,
which yields exactly **604** records, of which **70** are direct children of the
root (`parent_names == [...,'tpb_0']`).

---

## 3. The 70 direct children — gap-free tiling (HIGH · OBSERVED)

The 70 direct children **tile the 256 GiB window with zero gaps and zero
overlaps**: sorted by base, `first_base = 0x0`, `last_end = 0x4000000000`,
`Σ(child sizes) == 0x4000000000 == window`. Because the parent base is 0, every
direct child has `offset == base`. The layout is a **two-copy design**: a *REAL*
plane at low base, then a full **`LOCAL_*` alias** of the same engines at
`+0x800000000`, and the same idea again for the POOL/cluster-1 plane
(`0x1000000000` real, `0x1800000000` alias) and for SBUF (`0x2000000000` real,
`0x3000000000` alias). The REAL plane is given below; each row has a 1:1
`LOCAL_*` mirror at the indicated alias offset (verified, all 70 present).

| base (view-rel) | size | cnt | sas | type | short_name | → schema |
|-----------------|------|----:|----:|------|------------|----------|
| `0x0` … `0xf00000` | `0x100000` ea | 2 | 16 | NODE | `SP_0`…`SP_15` | `TPB_SP.json` |
| `0x1000000` | `0x80000` | 1 | 1 | NODE | `DGE_MEMORY` | `reserved.json` (512 KiB stub) |
| `0x1080000` | `0x100000` | 2 | 2 | NODE | `PE_0_0` | `TPB_PE.json` |
| `0x1180000` | `0x100000` | 2 | 2 | NODE | `PE_0_1` | `TPB_PE.json` |
| `0x1280000` | `0x700000` | 2 | 2 | NODE | `DVE_0_0` | `TPB_DVE.json` |
| `0x1980000` | `0x700000` | 2 | 2 | NODE | `DVE_0_1` | `TPB_DVE.json` |
| `0x2080000` | `0x100000` | 1 | 1 | NODE | `EVT_SEM_0` | `TPB_EVT_SEM.json` |
| `0x2180000` | `0x100000` | 1 | 1 | REGFILE | `COLLECTIVE_SYNC` | `tpb_coll_sync_sp.json` |
| `0x2280000` | `0x100000` | 1 | 1 | NODE | `SP_SHARED_RAM` | `SHARED_RAM_BLOCK.json` |
| `0x2380000` | `0x7fdc80000` | 1 | 1 | NODE | `TPB_RESERVED0` | `reserved.json` (pad) |
| **— `LOCAL_*` alias plane @ `+0x800000000` (16 SP + DGE + 4 PE/DVE + EVT_SEM + COLL_SYNC + SP_SHARED_RAM + pad) —** |
| `0x1000000000` | `0x200000` | 1 | 1 | NODE | `SUNDA_POOL_RSVD` | `reserved.json` |
| `0x1000200000` | `0x8c0000` | 1 | 1 | NODE | `TPB_POOL` | `TPB_POOL.json` (8×Q7) |
| `0x1000ac0000` | `0x100000` | 2 | 2 | NODE | `PE_1_0` | `TPB_PE.json` |
| `0x1000bc0000` | `0x100000` | 2 | 2 | NODE | `PE_1_1` | `TPB_PE.json` |
| `0x1000cc0000` | `0x700000` | 2 | 2 | NODE | `DVE_1_0` | `TPB_DVE.json` |
| `0x10013c0000` | `0x700000` | 2 | 2 | NODE | `DVE_1_1` | `TPB_DVE.json` |
| `0x1001ac0000` | `0x100000` | 1 | 1 | NODE | `EVT_SEM_1` | `TPB_EVT_SEM.json` |
| `0x1001bc0000` | `0x7fe440000` | 1 | 1 | NODE | `TPB_RESERVED2` | `reserved.json` |
| **— `LOCAL_*` alias of the POOL/cluster-1 plane @ `0x1800000000` (`LOCAL_TPB_POOL` etc.) —** |
| `0x2000000000` | `0x8000000` | 1 | 1 | NODE | `SBUF` | `TPB_SBUF.json` (128 MiB) |
| `0x2008000000` | `0xff8000000` | 1 | 1 | NODE | `TPB_SBUF_RESERVED0` | `reserved.json` |
| `0x3000000000` | `0x8000000` | 1 | 1 | NODE | `LOCAL_SBUF` | `TPB_SBUF.json` (128 MiB alias) |
| `0x3008000000` | `0xff8000000` | 1 | 1 | NODE | `TPB_SBUF_RESERVED1` | `reserved.json` |

(`sas` = `self_array_size`; `ea` = each. `SP_n` is a 16-wide array at `0x100000`
pitch — `SP_0`…`SP_15` are 16 *distinct* materialized records, not one folded
node.)

**Engine census per physical TPB** (REAL plane; each also mirrored in the
`LOCAL_*` plane):

| count | engine | container schema |
|------:|--------|------------------|
| 16 | `SP_0`…`SP_15` | `TPB_SP.json` |
| 4 | `PE_0_0 / 0_1 / 1_0 / 1_1` | `TPB_PE.json` (2 clusters × 2) |
| 4 | `DVE_0_0 / 0_1 / 1_0 / 1_1` | `TPB_DVE.json` (2 clusters × 2; **ACT folded in**) |
| 2 | `EVT_SEM_0 / 1` | `TPB_EVT_SEM.json` |
| 1 | `TPB_POOL` (8×Q7 GPSIMD cluster) | `TPB_POOL.json` |
| 1 | `SBUF` (`STATE_BUF`, 128 MiB) | `TPB_SBUF.json` |
| 1 | `COLLECTIVE_SYNC` | `tpb_coll_sync_sp.json` |
| 1 | `SP_SHARED_RAM` (8 banks) | `SHARED_RAM_BLOCK.json` |
| 1 | `DGE_MEMORY` (512 KiB stub) | `reserved.json` |
| 1 | `SUNDA_POOL_RSVD` | `reserved.json` |

> **GOTCHA — no PSUM node anywhere.** A name match for `"PSUM"` returns **0
> records across all 323,198** (HIGH negative · OBSERVED). Cayman's flat YAML has
> an explicit `TPB_0_PSUM_BUF` (4 MiB @ `0x2802000000`); the Maverick DB carries
> **no PSUM leaf**. On Maverick the PE-array accumulator is not a named
> address-map region — it is driven from the PE-array sequencer host-visible CSRs
> ([§6](#6-the-host-visible-pe-array-sequencer-boundary-high--observed)), not via
> an addressable buffer.

---

## 4. Per-engine sub-block breakdown (HIGH · OBSERVED — parent-relative)

Every Xtensa-driven engine (PE / DVE / SP / POOL) carries the **same canonical
front-matter**: a sequencer IRAM, an Xtensa-NX core pair (`NX_IRAM` + `NX_DRAM`),
an `xt` **`LOCAL_REG` control block at engine-relative `+0x60000`** (→
`tpb_xt_local_reg.json`, `Type=REGFILE`, `AddrWidth=16`, `SizeInBytes=0x10000`),
and profiling (`PROFILE_CAM` + `PROFILE_TABLE`). This is the same IP geometry the
Cayman CSR pages document; the `+0x60000` LOCAL_REG offset and `0x10000` size
match byte-for-byte. The `LOCAL_REG` schema is the
[`../csr/tpb-xt-local-reg.md`](../csr/tpb-xt-local-reg.md) block; the NX core is
[`../csr/xtensa-q7.md`](../csr/xtensa-q7.md).

`MEM_WIDTH` is the per-instance RAM word-byte width from the Verilog parameter
override (`16` for IRAM/most RAMs, `4` for DRAM, `64` for SBUF).

### 4a. PE — the systolic-array sequencer (`PE_0_0`, `0x100000`, → `TPB_PE.json`) — 7 children

| +off | size | type | short_name | params |
|------|------|------|------------|--------|
| `+0x00000` | `0x20000` | NODE | `PE_IRAM` (sequencer IRAM, 128 KiB) | MEM_SIZE 131072, MEM_WIDTH 16 |
| `+0x20000` | `0x20000` | NODE | `PE_NX_IRAM` | MEM_SIZE 131072, MEM_WIDTH 16 |
| `+0x40000` | `0x20000` | NODE | `PE_NX_DRAM` | MEM_SIZE 131072, MEM_WIDTH 4 |
| `+0x60000` | `0x10000` | REGFILE | `PE_LOCAL_REG` | → `tpb_xt_local_reg.json` |
| `+0x70000` | `0x1000` | NODE | `PE_PROFILE_CAM` | MEM_SIZE 4096 |
| `+0x71000` | `0x2000` | NODE | `PE_PROFILE_TABLE` | MEM_SIZE 8192 |
| `+0x73000` | `0x8d000` | NODE | `PE_RESERVED3` (tail pad) | RESERVED_SIZE 577536 |

`PE_LOCAL_REG.tag.HDL_PATH =
'gg_pe_seq_arr[0].u_tpb_pe_seq.sequencer_local_reg.xt_local_reg'` — the PE node is
the matmul **systolic-array sequencer** (engine_idx 0; representative image
[`../../images/cayman-pe`](../../images/cayman-pe.md)). 4 PE blocks per TPB
(2 clusters × 2).

### 4b. DVE — the vector engine, with ACT folded in (`DVE_0_0`, `0x700000`, → `TPB_DVE.json`) — 22 children

The largest engine, and it **absorbs the ACT (activation) datapath**:

| +off | size | type | short_name |
|------|------|------|------------|
| `+0x00000` | `0x8000` | NODE | `DVE_IRAM` (sequencer IRAM, 32 KiB) |
| `+0x08000` | `0x18000` | NODE | `DVE_RESERVED0` |
| `+0x20000` | `0x20000` | NODE | `DVE_NX_IRAM` |
| `+0x40000` | `0x20000` | NODE | `DVE_NX_DRAM` |
| `+0x60000` | `0x10000` | REGFILE | `DVE_LOCAL_REG` → `tpb_xt_local_reg.json` |
| `+0x70000` | `0x10000` | NODE | `DVE_RESERVED1` |
| `+0x80000` | `0x1000` | NODE | `DVE_BANK_CONTROL_RAM_FAST` |
| `+0x81000` | `0x2000` | NODE | `DVE_BANK_CONTROL_RAM_SLOW` |
| `+0x83000` | `0x8000` | NODE | `DVE_BANK_DATAPATH_RAM` |
| `+0x8b000` | `0x400` | NODE | `DVE_BANK_OPCODE_RAM` |
| `+0x8b400` | `0xc00` | NODE | `DVE_RESERVED2` (pad) |
| `+0x8c000` | `0x400` | NODE | `DVE_BANK_PARAMETER_RAM` |
| `+0x8c400` | `0xc00` | NODE | `DVE_RESERVED3` (pad) |
| `+0x8d000` | `0x1000` | NODE | `DVE_PROFILE_CAM` |
| `+0x8e000` | `0x2000` | NODE | `DVE_PROFILE_TABLE` |
| `+0x90000` | `0x10000` | NODE | `DVE_RESERVED4` |
| `+0xa0000` | `0x2000` | NODE | **`ACT_CONTROL_TABLE`** ← ACT table, inside DVE |
| `+0xa2000` | `0xe000` | NODE | `DVE_RESERVED5` |
| `+0xb0000` | `0x10000` | NODE | `PWP_CONTROL_TABLE` (pointwise/activation control) |
| `+0xc0000` | `0x80000` | NODE | `PWP_BUCKETS_TABLE` (activation lookup buckets, 512 KiB) |
| `+0x140000` | `0xc0000` | NODE | `DVE_RESERVED6` |
| `+0x200000` | `0x500000` | NODE | `DVE_RESERVED7` (tail pad) |

> **NOTE — ACT is a DVE sub-block, not a top-level engine.** In Maverick the ACT
> engine's control and bucket tables (`ACT_CONTROL_TABLE`, `PWP_CONTROL_TABLE`,
> `PWP_BUCKETS_TABLE`) live **inside** the DVE node. Cayman's flat YAML had a
> separate `ACT_*` block. The bank RAMs (control fast/slow, datapath, opcode,
> parameter) match Cayman's separate `TPB_0_DVE_*` bank RAMs. *"ACT folded into
> DVE" is OBSERVED node placement* (HIGH); whether Maverick firmware still treats
> ACT as engine_idx 1 at a distinct base is an image-corpus question, not
> resolvable from the address map alone (MED · INFERRED). Representative DVE/ACT
> images: [`../../images/cayman-dve`](../../images/cayman-dve.md),
> [`../../images/cayman-act`](../../images/cayman-act.md).

### 4c. SP — per-NeuronCore sync/scalar processor (`SP_0`, `0x100000`, → `TPB_SP.json`) — 6 children

| +off | size | type | short_name |
|------|------|------|------------|
| `+0x00000` | `0x8000` | NODE | `IRAM` (sequencer IRAM, 32 KiB) |
| `+0x08000` | `0x18000` | NODE | `RESERVED0` |
| `+0x20000` | `0x20000` | NODE | `NX_IRAM` |
| `+0x40000` | `0x20000` | NODE | `NX_DRAM` |
| `+0x60000` | `0x10000` | REGFILE | `LOCAL_REG` → `tpb_xt_local_reg.json` |
| `+0x70000` | `0x90000` | NODE | `RESERVED3` |

> **QUIRK — 16 SP engines per TPB.** Maverick materializes a **16-wide SP array**
> (`SP_0`…`SP_15`, `self_array_size="16"`, `0x100000` pitch). Cayman had a single
> TPB SP block. The 16 SP engines are 16 distinct records, each with the identical
> 6-child geometry. SP is the per-NeuronCore sync/scalar processor (engine_idx 4;
> representative image [`../../images/cayman-sp`](../../images/cayman-sp.md)).

### 4d. POOL — the GPSIMD 8×Q7 cluster (`TPB_POOL`, `0x8c0000`, → `TPB_POOL.json`) — 27 children

| +off | size | type | short_name | params |
|------|------|------|------------|--------|
| `+0x00000` | `0x8000` | NODE | `POOL_IRAM` (32 KiB sequencer IRAM) | MEM_WIDTH 16 |
| `+0x08000` | `0x18000` | NODE | `POOL_RESERVED0` | |
| `+0x20000` | `0x20000` | NODE | `POOL_NX_IRAM` (128 KiB) | MEM_WIDTH 16 |
| `+0x40000` | `0x20000` | NODE | `POOL_NX_DRAM` (128 KiB) | MEM_WIDTH 4 |
| `+0x60000` | `0x10000` | REGFILE | `POOL_LOCAL_REG` → `tpb_xt_local_reg.json` | |
| `+0x70000` | `0x1000` | NODE | `POOL_PROFILE_CAM` | |
| `+0x71000` | `0x2000` | NODE | `POOL_PROFILE_TABLE` | |
| `+0x73000` | `0x80000` | REGFILE | **`GPSIMD_SYNC`** → `tpb_gpsimd_sync.json` (512 KiB) | |
| `+0xf3000` | `0xd000` | NODE | `POOL_RESERVED3` | |
| `+0x100000` | `0x40000` | NODE | `POOL_Q7_CORE0_DRAM` (256 KiB) | MEM_SIZE 262144, MEM_WIDTH 4 |
| `+0x140000` | `0x40000` | NODE | `POOL_RESERVED4` | |
| … | | | CORE1…CORE7 DRAM + interleaved RESERVED, **1 MiB pitch** | |
| `+0x480000` | `0x40000` | NODE | `POOL_Q7_CORE7_DRAM` | MEM_SIZE 262144, MEM_WIDTH 4 |
| `+0x4c0000` | `0x40000` | NODE | `POOL_RESERVED11` | |
| `+0x500000` | `0x100000` | NODE | **`POOL_SHARED_RAM`** (1 MiB, shared across 8 Q7) | MEM_SIZE 1048576, MEM_WIDTH 4 |
| `+0x600000` | `0x100000` | NODE | `POOL_RESERVED12` | |

> **NOTE — per-core DRAM only; no per-core IRAM.** All 8 Q7 cores
> (`POOL_Q7_CORE0_DRAM`…`CORE7_DRAM`, 256 KiB each, `MEM_WIDTH=4`) are present, at
> a `0x100000` (1 MiB) slot pitch with an interleaved `RESERVED` pad per core. The
> cores share the single `POOL_IRAM` and the new 1 MiB `POOL_SHARED_RAM`; there
> are **no per-core `POOL_Q7_CORE*_IRAM` nodes** (0 found). The 8-core count
> matches Cayman's TPB POOL. `POOL_NX_DRAM` is **128 KiB** here (Cayman was
> 64 KiB — a cross-gen bump). Both `GPSIMD_SYNC` and `POOL_SHARED_RAM` are new vs
> Cayman's flat TPB. See [`tpb-pool.md`](./tpb-pool.md) and the representative
> image [`../../images/cayman-pool`](../../images/cayman-pool.md).

### 4e. EVT_SEM — event + semaphore op windows (`EVT_SEM_0`, `0x100000`, → `TPB_EVT_SEM.json`) — 8 children

| +off | size | short_name |
|------|------|------------|
| `+0x0000` | `0x400` | `EVENT_` |
| `+0x0400` | `0xc00` | `EVENT_RESERVED0` (pad) |
| `+0x1000` | `0x400` | `SEMAPHORE_READ` |
| `+0x1400` | `0x400` | `SEMAPHORE_SET` |
| `+0x1800` | `0x400` | `SEMAPHORE_INC` |
| `+0x1c00` | `0x400` | `SEMAPHORE_DEC` |
| `+0x2000` | `0x4000` | **`SEMAPHORE_CNTR_INC`** (new vs Cayman) |
| `+0x6000` | `0xfa000` | `EVENT_RESERVED1` (tail pad) |

The event + four semaphore-op windows of the Cayman EVT_SEM, **plus a new
`0x4000` `SEMAPHORE_CNTR_INC` window**. There are **2 EVT_SEM instances** per TPB
(`EVT_SEM_0` @ `0x2080000`, `EVT_SEM_1` @ `0x1001ac0000`). See
[`evt-sem-regions.md`](./evt-sem-regions.md).

### 4f. SBUF / STATE_BUF (`SBUF`, → `TPB_SBUF.json`) — 1 child

`STATE_BUF`, base `0x2000000000`, size `0x8000000` = **128 MiB**, params
`[('MEM_SIZE','134217728'),('MEM_WIDTH','64')]`. The state buffer at the
SoC-absolute `0x2000000000` anchor.

> **CORRECTION (cross-gen).** Maverick `STATE_BUF` = **128 MiB** (`MEM_WIDTH 64`);
> Cayman `STATE_BUF` = 32 MiB. A 4× SBUF capacity increase (HIGH · OBSERVED).

### 4g. SP_SHARED_RAM (→ `SHARED_RAM_BLOCK.json`) — 8 banks

`SHARED_RAM_BANK_0`…`SHARED_RAM_BANK_7`, each `0x20000` (128 KiB) → 8 × 128 KiB =
**1 MiB**. (Each bank binds `reserved.json` with `RESERVED_SIZE 131072`.)

### 4h. DGE_MEMORY (→ `reserved.json`) — 0 children

Base `0x1000000`, size `0x80000` = **512 KiB**, params
`[('RESERVED_SIZE','524288')]`.

> **GOTCHA — DGE is a 512 KiB stub here.** Cayman's `TPB_0_DGE_MEMORY` was 1 GiB.
> In the Maverick engine subtree `DGE_MEMORY` is a 512 KiB **RESERVED stub with no
> children** (HIGH · OBSERVED size). The large DGE descriptor RAM is *not* in this
> engine subtree on Maverick — whether it moved or was resized elsewhere is not
> established from this subtree (MED · INFERRED). For the DMA-side DGE see
> [`pkl-dma-subtree.md`](./pkl-dma-subtree.md).

---

## 5. The array / instance-expansion model — the dims the YAML lost (HIGH · OBSERVED)

The flat YAML pre-expanded every array into underscore-joined names with **no
dimension metadata**. The pkl carries the full nested-array geometry per node:

| node | `count` | `self_array_size` | `parent_size` | `parent_offset` | `instance_index` | `parent_array_size` |
|------|--------:|------------------:|--------------:|----------------:|-----------------:|---------------------|
| `TPB_0` (engine root) | 4 | `"2"` | 4 | 0 | `"0x0"` | `"1*1*4"` |
| `SP_0` | 2 | `"16"` | 2 | 0 | `"0x0"` | `"1*1*4*2"` |
| `SP_15` | 2 | `"16"` | 2 | 0 | `"0xF"` | `"1*1*4*2"` |
| `PE_0_0` | 2 | `"2"` | 2 | 0 | `"0x0"` | `"1*1*4*2"` |
| `PE_0_1` | 2 | `"2"` | 2 | 0 | `"0x1"` | `"1*1*4*2"` |
| `TPB_POOL` | 1 | `"1"` | 2 | 0 | `"0x0"` | `"1*1*4*2"` |
| `POOL_Q7_CORE0_DRAM` | 1 | `"1"` | 1 | 68721573888 | `"0x0"` | `"1*1*4*2*1"` |

Reading (HIGH · OBSERVED):

- **`parent_array_size`** is the cumulative array dims of the ancestor path. The
  TPB root has `"1*1*4"` = `ROOT(1) · user_int(1) · seng(4)`. Below it every node
  appends its parent's stride: under TPB it becomes `"1*1*4*2"` (the `*2` = the 2
  TPBs per SENG, the root's `self_array_size`). The pkl thus *encodes* the
  underscore-joined dims the YAML names baked in.
- **`self_array_size`** is this node's own array width: `SP_n="16"` (16-wide),
  `PE/DVE="2"` (2-wide), `POOL/EVT_SEM/SBUF="1"` (singleton). `TPB_0` itself is
  `"2"` (2 TPBs per SENG).
- **`count`** is the *parent's* array multiplicity: `TPB_0.count=4` (the 4 SENGs);
  `SP_n.count=2` (the 2 TPBs); a leaf under SP has `count=1`.
- **`instance_index`** is this instance's index within its own array, hex
  (`SP_15 → "0xF"`). All members are **materialized as separate records**
  (`as_array="false"` on every node) — `SP_0`…`SP_15` are 16 distinct records,
  not one folded node.
- **`parent_offset`** is the byte offset within the parent's array stride;
  `POOL_Q7_CORE0_DRAM.parent_offset = 68721573888 = 0x1000200000`, its parent
  `TPB_POOL`'s absolute base (the per-core stride base).

**Full expansion:** 4 SENG × 2 TPB = 8 TPBs; per TPB, 16 SP / 4 PE / 4 DVE
explicit array members + 1 POOL with 8 Q7 cores. *Tiling/contiguity* is verified:
the 70 direct children fill the window with 0 gaps / 0 overlaps, and on every
spot-checked leaf the parent-relative `offset` equals `base − parent_base` (e.g.
`POOL_Q7_CORE0_DRAM` at `0x1000300000` − `TPB_POOL` `0x1000200000` = `0x100000`).

**The richer-than-YAML fields, in full** (per the 604-record subtree):

| field group | what it adds over the flat YAML | coverage |
|-------------|----------------------------------|----------|
| parent-relative `offset` | YAML had only absolute `base` | all 604 |
| explicit hierarchy (`parent_names`, `parentName`, `parent_name_no_idx`) | YAML had flattened names | all 604 |
| array model (`count`/`self_array_size`/`parent_*`/`instance_index`/`parent_array_size`) | YAML had none | all 604 |
| typed leaf taxonomy (`type ∈ {NODE:546, REGFILE:58}`) | YAML had only "has json?" | all 604 |
| RTL back-annotation `tag.HDL_PATH` | YAML had none | **16** records |
| RTL `parameters` (MEM_SIZE/MEM_WIDTH/RESERVED_SIZE/COLL_SYNC_*) | YAML dropped them | 492 records |
| original schema `json` path | YAML had a boolean | all non-root |

The 16 `HDL_PATH` tags are exactly the PE/DVE `LOCAL_REG` nodes (real + alias),
giving the RTL hierarchy path:

- `PE_LOCAL_REG` → `gg_pe_seq_arr[0].u_tpb_pe_seq.sequencer_local_reg.xt_local_reg`
- `DVE_LOCAL_REG` → `sequencer_gen[0].u_dve_seq_wrapper.sequencer_gen[0].sequencer_local_reg.xt_local_reg`

(SP/POOL `LOCAL_REG` nodes carry no `HDL_PATH` in this subtree.) The TPB root
carries the multi-die tag `{'DIE_NAME':'C_DIE'}`.

### 5a. Block → CSR-schema bindings (HIGH · OBSERVED)

Every non-root node's `json` field names its backing schema. The 604-record
subtree references exactly **13 distinct schema files**, all on disk in the
maverick `vpc-mirror/arch-regs/src/` tree:

| count | schema (basename) | on-disk Type / Size / AddrWidth | binds |
|------:|-------------------|---------------------------------|-------|
| 270 | `memory.json` | RegFile (MEM_SIZE/MEM_WIDTH params) | every IRAM/DRAM/RAM/table leaf |
| 218 | `reserved.json` | NODE (RESERVED_SIZE param) | every RESERVED pad / stub |
| 50 | `tpb_xt_local_reg.json` | REGFILE · `0x10000` · AddrW 16 | every engine `LOCAL_REG` (25 real + 25 alias) |
| 32 | `TPB_SP.json` | NODE · `0x100000` · AddrW 64 | the 16 SP containers (×2 planes) |
| 8 | `TPB_PE.json` | NODE · `0x100000` · AddrW 64 | the 4 PE containers (×2 planes) |
| 8 | `TPB_DVE.json` | NODE · `0x700000` · AddrW 64 | the 4 DVE containers (×2 planes) |
| 4 | `TPB_EVT_SEM.json` | NODE · `0x100000` | the 2 EVT_SEM containers (×2 planes) |
| 4 | `tpb_coll_sync.json` | REGFILE · `0x80000` · AddrW 19 | nested coll-sync inside COLLECTIVE_SYNC **and** POOL.GPSIMD_SYNC |
| 2 | `tpb_coll_sync_sp.json` | REGFILE · `0x100000` | the `COLLECTIVE_SYNC` direct child |
| 2 | `SHARED_RAM_BLOCK.json` | NODE · `0x100000` | `SP_SHARED_RAM` (8 banks) |
| 2 | `TPB_POOL.json` | NODE · `0x8c0000` · AddrW 64 | the `TPB_POOL` container (8×Q7) |
| 2 | `tpb_gpsimd_sync.json` | REGFILE · `0x80000` | `POOL.GPSIMD_SYNC` |
| 2 | `TPB_SBUF.json` | NODE · `0x8000000` · AddrW 64 | the `SBUF` (`STATE_BUF`) container |

(Plus `tpb_user_address_map.json` on the root itself; the `STATE_BUF` leaf binds
`memory.json`.)

**Binding semantics** (HIGH · OBSERVED): the node's `type` matches the bound
schema's `RegFile.Type`, and the node's `size` matches the schema's
`SizeInBytes` — verified for `TPB_PE` (`0x100000`), `TPB_DVE` (`0x700000`),
`TPB_POOL` (`0x8C0000`), `TPB_SBUF` (`0x8000000`), and the REGFILE bindings
(`tpb_xt_local_reg` `0x10000`, `tpb_coll_sync`/`tpb_gpsimd_sync` `0x80000`). The
`reserved.json` schema declares `SizeInBytes = RESERVED_SIZE`, and the pkl node
supplies the concrete `RESERVED_SIZE` via its `parameters` (e.g. `DGE_MEMORY →
RESERVED_SIZE 524288`) — the schema is parameterized, the node carries the
per-instance Verilog override. So the `TPB_*.json` files are **sub-address-map
descriptors** (`Type=NODE`), *not* register files; the real register files in this
subtree are `tpb_xt_local_reg.json` ([`../csr/tpb-xt-local-reg.md`](../csr/tpb-xt-local-reg.md)),
`tpb_coll_sync*.json`, and `tpb_gpsimd_sync.json`. This recovers the
[`block-schema-xref.md`](./block-schema-xref.md) relationship directly from the
DB. The overall TPB CSR map is [`../csr/tpb.md`](../csr/tpb.md).

### 5b. COLLECTIVE_SYNC / GPSIMD_SYNC — a new Maverick fabric (HIGH · OBSERVED)

There are **two distinct collective-sync blocks**, with **different sizings** —
this is the synchronization fabric the Q7 GPSIMD kernels and the TPB engines use,
and it has no Cayman flat-YAML equivalent:

- **TPB-wide `COLLECTIVE_SYNC`** (direct child, `0x2180000`, 1 MiB, REGFILE →
  `tpb_coll_sync_sp.json`) contains a `COLL_SYNC` leaf (`0x80000`, 512 KiB,
  REGFILE → `tpb_coll_sync.json`) with parameters
  **`COLL_SYNC_NUM_SEMAPHORES=2048`, `COLL_SYNC_WATCHERS_PER_AGENT=64`,
  `COLL_SYNC_NUM_AGENTS=16`**.
- **POOL-local `GPSIMD_SYNC`** (inside `TPB_POOL` at `+0x73000`, `0x80000`,
  REGFILE → `tpb_gpsimd_sync.json`) contains its own `COLL_SYNC` leaf (same
  `tpb_coll_sync.json`) with parameters **`NUM_SEMAPHORES=64`,
  `WATCHERS_PER_AGENT=16`, `NUM_AGENTS=8`**.

> **CORRECTION vs SX-ADDR-11 §5a.** The backing report states *both* the
> TPB-wide `COLLECTIVE_SYNC` and the POOL `GPSIMD_SYNC` expose "64 semaphores / 16
> watchers-per-agent / 8 agents." The shipped DB shows that this is true **only of
> the POOL `GPSIMD_SYNC`** — the **TPB-wide `COLLECTIVE_SYNC` is 32× larger:
> 2048 semaphores / 64 watchers-per-agent / 16 agents**. The report conflated the
> two parameter sets; the per-block sizings are genuinely different. Verified by
> streaming all four `tpb_coll_sync.json`-bound records (the REAL and `LOCAL_*`
> copies of each block). Both blocks bind the same `tpb_coll_sync.json` schema
> (which is parameterized; the size differences come from the per-node parameter
> overrides). HIGH · OBSERVED.

---

## 6. The host-visible PE-array-sequencer boundary (HIGH · OBSERVED)

The PE-array sequencer **host-visible CSRs** are *not* in the engine-data subtree
of [§3](#3-the-70-direct-children--gap-free-tiling-high--observed); they live on
the **APB control-plane** side, under `SENG_n/APB_IO/c_die/APB_IO`. 256 records
bind the arr_seq schemas across the 4 SENGs:

- 96 `tpb_arr_seq_top_host_visible.json`
- 96 `tpb_arr_seq_cluster_host_visible.json`
- 64 `tpb_arr_seq_top_protected.json`

Each of the 4 PE clusters (`PE_0_0 / 0_1 / 1_0 / 1_1`) gets a `SEQ_TOP` +
`SEQ_CLUSTER` host-visible 4 KiB CSR window, e.g.
`…APB_IO_USER_TPB_0_PE_0_0_SEQ_TOP_HOST_VISIBLE` @ base `0xc000140000`, size
`0x1000`. This is the matmul-array sequencer config face — and it is **where PSUM
addressing is driven**, consistent with [§3](#3-the-70-direct-children--gap-free-tiling-high--observed)'s
negative result (no named PSUM region). The protected/bcast copies sit on the
`secure_int` side under `c_die/PEB_APB_IO[_BCAST]`. These belong to the
host-visible control-plane lane (see the PE-array-sequencer CSR page), noted here
only as the boundary of the engine-data subtree. The arr_seq sequencer CSRs are
documented at [`../csr/pe-array-sequencer.md`](../csr/pe-array-sequencer.md).

---

## 7. Python — the TPB-subtree carve

The carve is the same memory-safe streaming pattern [`pkl-db.md`](./pkl-db.md)
establishes. **Never `pickle.load`/`loads`.** Either scan opcodes with
`pickletools.genops()` (no object construction), or stream the `.json` mirror one
record at a time with a brace-counting iterator. Names below are the *real* field
keys.

```python
import json, os
from collections import Counter

# byte-grounded path: the shipped Maverick mirror (514 MB) — DO NOT slurp it whole
JSON = (".../arch-headers/maverick/ext/al_address_map_db.json")

def stream_records(fh):
    """Yield each top-level dict from a pretty-printed JSON array without
    loading the file into memory. Brace-counts, string-aware (skips braces
    inside quoted strings), so it never constructs more than one record."""
    c = fh.read(1)
    while c and c != "{":          # skip leading '['
        c = fh.read(1)
    while c == "{":
        depth, chars, in_str, esc = 1, ["{"], False, False
        while depth > 0:
            ch = fh.read(1)
            if not ch:
                break
            chars.append(ch)
            if in_str:
                if esc:        esc = False
                elif ch == "\\": esc = True
                elif ch == '"':  in_str = False
            else:
                if   ch == '"': in_str = True
                elif ch == "{": depth += 1
                elif ch == "}": depth -= 1
        yield json.loads("".join(chars))
        c = fh.read(1)
        while c and c not in "{]":  # advance to next record or end-of-array
            c = fh.read(1)
        if c == "]" or not c:
            break

# prefix-filter to the TPB engine-data subtree (one of the 8 physical TPBs)
ROOTCHAIN = ["ADDRESS_MAP", "user_int", "seng_0", "tpb_0"]

def carve_tpb(json_path):
    sub, root = [], None
    with open(json_path) as fh:
        for rec in stream_records(fh):
            pn = rec.get("parent_names", [])
            if rec["name"] == "USER_INT_SENG_0_TPB_0":
                root = rec                       # the engine root, 256 GiB window
            if pn[:4] == ROOTCHAIN:
                sub.append(rec)                  # 604 records
            # cheap early-out once we have left seng_0 and collected the subtree
            if root and len(sub) >= 604 and pn[:3] != ROOTCHAIN[:3]:
                break
    return root, sub

root, sub = carve_tpb(JSON)
assert len(sub) == 604

# direct children = parent_names exactly == ROOTCHAIN -> 70 engine/SBUF/pad nodes
direct = [r for r in sub if r["parent_names"] == ROOTCHAIN]
assert len(direct) == 70

# gap-free tiling check over the 256 GiB window
chs = sorted(direct, key=lambda r: r["base"])
assert sum(r["size"] for r in chs) == root["size"] == 0x4000000000
end = 0
for r in chs:
    assert r["base"] == end, f"gap/overlap at {r['short_name']}"
    end = r["base"] + r["size"]

# block -> schema bindings (13 distinct) and type taxonomy
bindings = Counter(os.path.basename(r["json"]) for r in sub if "json" in r)
types    = Counter(r["type"] for r in sub)         # {'NODE': 546, 'REGFILE': 58}

# the array model fields the flat YAML dropped, per node
def array_model(rec):
    return dict(count=rec["count"],
                self_array_size=rec["self_array_size"],   # e.g. SP_n -> "16"
                parent_size=rec["parent_size"],
                parent_offset=rec["parent_offset"],
                instance_index=rec["instance_index"],     # hex str, "0x0".."0xF"
                parent_array_size=rec["parent_array_size"])  # "1*1*4*2"

# the per-instance Verilog parameter overrides (MEM_SIZE/MEM_WIDTH/RESERVED_SIZE/
# COLL_SYNC_*); list-of-[key, value] pairs in 'parameters'
def params(rec):
    return {k: v for k, v in rec["parameters"]}
```

---

## 8. Reconciliation: MAVERICK pkl vs CAYMAN flat YAML (mixed)

Same DB schema, different SoC instance. The TPB engine block is recognizably the
same IP family, with these byte-verified differences. **v5-only engines/arrays are
INFERRED as v5-specific; matching bases/geometry are OBSERVED on both sides.**

| aspect | CAYMAN (flat YAML) | MAVERICK (this pkl) | conf |
|--------|--------------------|---------------------|------|
| TPB count | 8 (`TPB_0..7`, flat) | 8 = 4 SENG × 2 TPB | HIGH |
| TPB engine window | `0x804000000` (≈32 GiB) | `0x4000000000` (256 GiB) | HIGH |
| STATE_BUF (SBUF) | 32 MiB @ `0x2000000000` | **128 MiB** @ `0x2000000000` | HIGH |
| PSUM_BUF | 4 MiB @ `0x2802000000` (explicit) | **absent (0 records)** | HIGH |
| DGE_MEMORY | 1 GiB @ `0x2040000000` | **512 KiB reserved stub** | HIGH |
| SP engines per TPB | 1 (TPB SP block) | **16** (`SP_0..15`) | HIGH |
| PE engines | 1 PE-array sequencer | **4** (2 clusters × 2) | HIGH |
| ACT engine | separate `ACT_*` block | **folded into DVE node** | HIGH |
| DVE engine | 1 DVE block + bank RAMs | **4** (2×2) + banks | HIGH |
| POOL (8×Q7) | per-core IRAM+DRAM, NX_DRAM 64 KiB | per-core DRAM only; NX_DRAM 128 KiB; **+GPSIMD_SYNC, +POOL_SHARED_RAM** | HIGH |
| EVT_SEM | event + 4 sema windows (1 inst) | **+SEMAPHORE_CNTR_INC; 2 inst** | HIGH |
| COLLECTIVE_SYNC | (not in Cayman flat TPB) | **new: 2048 sema / 64 wpa / 16 agents** | HIGH |
| GPSIMD_SYNC | (not in Cayman flat TPB) | **new: 64 sema / 16 wpa / 8 agents** | HIGH |
| LOCAL_REG geometry | `+0x60000`, `0x10000`, `tpb_xt_local_reg.json` | **identical** | HIGH |
| LOCAL_* alias plane | implicit `0x800000000` cluster pseudo-base | **explicit `LOCAL_*` records** @ `+0x800000000` | HIGH |

**Engine-idx note.** The firmware `engine_idx` (PE=0, ACT=1, POOL=2, DVE=3,
TPB_SP=4 in the image corpus) is computed at boot from `engine_base_addr`; it is
**not** a field in this address-map DB (LOW · the mapping to pkl nodes is by
name/role, not an in-DB id). The 5 logical execution engines map onto the pkl as:
PE → `PE_x_y`; ACT → `ACT_CONTROL_TABLE` inside DVE; POOL(GPSIMD) → `TPB_POOL`;
DVE → `DVE_x_y`; TPB_SP → `SP_n`. Whether Maverick firmware still treats ACT as a
distinct engine_idx 1 base is an image-corpus question (MED · INFERRED).

**Node-count reconciliation** (HIGH · OBSERVED): `user_int` TPB-name records =
15,048, `secure_int` = 27,312, total 42,360. The engine-data subtree = 4,832
(8 chains × 604). The exact numeric Cayman-YAML ↔ Maverick-pkl prune mapping is
**not** re-derived (different SoC instance); the relationship is *structural*
(same schema, same flattening rule), not a 1:1 subset (MED · INFERRED on the
numeric prune).

---

## 9. Confidence ledger

**HIGH · OBSERVED** (re-verified by independent streaming of the shipped `.json`):
root = `USER_INT_SENG_0_TPB_0` (256 GiB window, 4 SENG × 2 TPB = 8 TPBs);
70-child gap-free tiling (Σ == window, 0 gaps / 0 overlaps); per-engine sub-block
breakdowns (PE 7 / DVE 22 / SP 6 / POOL 27 / EVT_SEM 8 / SP_SHARED_RAM 8 / SBUF 1
/ DGE 0), all `LOCAL_REG` @ `+0x60000` → `tpb_xt_local_reg.json`; engine census
(16 SP / 4 PE / 4 DVE / 1 POOL[8×Q7] / 2 EVT_SEM / 1 SBUF[128 MiB] /
1 COLLECTIVE_SYNC / 1 SP_SHARED_RAM / 1 DGE-stub), each mirrored in a `LOCAL_*`
plane; PSUM absent (0/323,198), DGE = 512 KiB stub, SBUF = 128 MiB; array model
fields decoded (arrays materialized, `as_array="false"` all); 13 distinct schema
bindings, all on disk, node type/size == schema Type/SizeInBytes; the
richer-than-YAML field inventory (parent-relative offset, hierarchy, array dims,
16 `HDL_PATH` tags, 492 parameter overrides); the two coll-sync blocks
(**2048/64/16** for COLLECTIVE_SYNC, **64/16/8** for GPSIMD_SYNC); PE-array-seq
host-visible CSRs on the APB plane (256 records).

**MED · INFERRED**: "ACT is a DVE sub-block, not a standalone engine" (OBSERVED
node placement; firmware engine_idx unresolved); the 1 GiB Cayman DGE
moved/resized (only the 512 KiB stub is OBSERVED here); the numeric
Cayman↔Maverick prune mapping (structural, not 1:1).

**LOW · NOTED**: `engine_idx` values are firmware-boot-computed (image corpus),
not address-map fields.

> **CORRECTION summary vs SX-ADDR-11.** One factual correction was made on this
> page: the report's §5a states the TPB-wide `COLLECTIVE_SYNC` exposes
> 64 sema / 16 wpa / 8 agents — the shipped DB shows that is the **POOL
> `GPSIMD_SYNC`** sizing, while the **TPB-wide `COLLECTIVE_SYNC` is 2048 sema /
> 64 wpa / 16 agents** ([§5b](#5b-collective_sync--gpsimd_sync--a-new-maverick-fabric-high--observed)).
> All other report claims reproduced byte-exact against an independent stream of
> the `.json` mirror.

---

*Source artifacts: `al_address_map_db.pkl` (216,631,794 B) and
`al_address_map_db.json` (514,276,583 B) under
`arch-headers/maverick/ext/`, plus the `vpc-mirror/arch-regs/src/` schema JSONs
they bind. Recovered via `pickletools.genops()` structural scanning and
memory-safe `.json` streaming only — no `pickle.load`, no execution. DMCA
1201(f) interoperability analysis from shipped, binary-derived files.*
