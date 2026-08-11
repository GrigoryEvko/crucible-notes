# Consolidated Data-Movement + Collectives Reference

This is the **synthesis capstone** for Part 9 — the single-chapter view that ties the
eleven deep-dive siblings into one coherent data-movement model and walks **one logical
transfer end to end** (logical copy → DGE micro-ops → SDMA BD ring → completion). It does
**not** re-dump the siblings' byte-level field tables — it summarises each layer, names the
one or two facts that bite, and points to the page that owns the bytes. Read this for the
*connective tissue*; read the sibling for the *bytes*.

The six sections mirror the six concerns a re-implementer must reconcile:

| § | concern | owning sibling(s) |
|---|---|---|
| [1](#1-the-descriptor-taxonomy--six-64-b-words--two-hardware-units) | the descriptor taxonomy (six 64-B words + the 16-B BD + the CCE compute descriptor) | [descriptor-model](descriptor-model.md), [gather-scatter](gather-scatter-descriptors.md), [field-tables](descriptor-ring-field-tables.md), [cce-in-transfer](cce-in-transfer.md) |
| [2](#2-the-dge-builder--byte-level-micro-op-encoding) | the DGE builder + GENERATE/DIMPUSH/REGWRITE micro-op encoding (`dge_op@+15`) | [dge-microop-encoding](dge-microop-encoding.md), [dge-builder-qos](dge-builder-qos.md) |
| [3](#3-the-sdma-hardware-engine--qos--arbitration) | the SDMA HW engine + QoS / arbitration (submit→complete, 3 arbiters, `prio_cap`) | [udma-hw-engine](udma-hw-engine.md), [dge-builder-qos](dge-builder-qos.md), [sdma-windows-apb](sdma-windows-apb.md) |
| [4](#4-the-sbufpsum-bank-model) | the SBUF/PSUM bank model (the Q7-cannot-reach-PSUM keystone) | [sbuf-psum-banks](sbuf-psum-banks.md), [onchip-working-memory](onchip-working-memory.md) |
| [5](#5-cross-die-rdma--collective-lowering) | cross-die RDMA + collective lowering (SB2SB `0xBF`, routing-id fold, in-SDMA CCE reduce) | [rdma-cross-die](rdma-cross-die.md), [cce-in-transfer](cce-in-transfer.md) |
| [6](#6-the-unified-completion-model) | the unified completion model (gen-tag / EVT_SEM inc@0x1800 / notification queue) | [udma-hw-engine](udma-hw-engine.md), [rdma-cross-die](rdma-cross-die.md) |

> **Generation anchor.** This wiki reconstructs the Vision-Q7 "Cairo" GPSIMD =
> **CAYMAN / NC-v3** (the ISA headers name the four reachable gens: `sunda = NC-v2`,
> `cayman = NC-v3`, `mariana = NC-v4`, `maverick = NC-v5`). Unless tagged, every byte/offset
> below is the cayman artifact, HIGH/OBSERVED. v5/MAVERICK *interiors* are header-OBSERVED
> only and flagged **INFERRED**. Tags: `HIGH/MED/LOW × OBSERVED` (read from a shipped
> binary/header/CSR-JSON in this extraction) / `INFERRED` (reasoned over OBSERVED bytes) /
> `CARRIED` (grounded in a prior survey of a binary not present here — re-verify before
> reuse). Recovered symbols/strings/headers/`.json`/`.pkl` are binary-derived and citeable.
> Markdown table pipes inside cells are escaped `\|`.

> **The one direction bit, threaded through every layer.**
> **READ** = HBM→local = **M2S** leg = doorbell `TDRTP_inc`; the GENERATE tag is `RD`.
> **WRITE** = local→HBM = **S2M** leg = doorbell `RDRTP_inc`; the GENERATE tag is `WR`.

---

## 1. The descriptor taxonomy — six 64-B words + two hardware units

A GPSIMD data movement is *described* at two coexisting device granularities (the
host/compiler layers above them are CARRIED): a **64-B TPB instruction word** (the *request*,
built/consumed by the DGE) that **expands** into N × **16-B SDMA buffer descriptors** (BDs,
the *atoms* the al_udma engine executes). See [descriptor-model §1-§3](descriptor-model.md).

### 1.1 The six 64-B words (the Layer-C catalog)

Every struct is `ISA_STATIC_ASSERT(sizeof == 64)` in its shipped cayman header
(`neuron_cayman_arch_isa/tpb/`), so each byte layout is gcc-pinned. The struct → wire-opcode
binding is itself shipped, in `instruction_mapping.json` (`struct2opcode`). These are **six
distinct structs with distinct field layouts** — not one template with field swaps (the
[microop-encoding §2.2 GOTCHA](dge-microop-encoding.md) is emphatic: `+15` holds a
*different* field in every resolved word). [HIGH/OBSERVED]

| # | struct | wire opcode | byte | what it is | byte detail |
|---|---|---|---|---|---|
| D1 | `DMA_DIRECT2D_STRUCT` | `DMA_MEMCPY` | `0xb8` | 2-D strided memcpy (DGE bread-and-butter); `compute_op@+15` routes the CCE path | [descriptor-model §2.2](descriptor-model.md) |
| D2 | `DMA_INDIRECT1D_STRUCT` | `DMA_INDIRECT` | `0xbb` | index-driven gather/scatter; two index vectors; `flags@+15`, `compute_op@+60` | [gather-scatter §1](gather-scatter-descriptors.md) |
| D3 | `DMA_DIRECT2D_XPOSE_STRUCT` | `DMA_TRANSPOSE` | `0xbd` | **tile-geometry** xbar transpose (no index); `tile_src_rows@+60` must == 16 | [field-tables §2.3](descriptor-ring-field-tables.md) |
| D4 | `DMA_GATHER_XPOSE_STRUCT` | `DMA_GATHER_TRANSPOSE` | `0xf1` | **index-array** gather-then-xbar-transpose (SW-DGE on Q7); NC-v3+ | [gather-scatter §2](gather-scatter-descriptors.md) |
| D5 | `EXTENDED_RDMA_DESC_GEN_STRUCT` | `EXTENDED_INST` ext-op **8** | `0xf0`/8 | cross-die SBUF→SBUF P2P: build the ring + fold the dst + 2 sema BDs | [rdma-cross-die §1-§2](rdma-cross-die.md) |
| D6 | `EXTENDED_RDMA_DESC_START_STRUCT` | `EXTENDED_INST` ext-op **9** | `0xf0`/9 | drain + PRID role-split + doorbell; carries **no operands of its own** | [rdma-cross-die §4](rdma-cross-die.md) |

The wire opcode set is byte-exact in `aws_neuron_isa_tpb_common.h`: `DMAMEMCPY=0xb8`
(:257), `DMA_INDIRECT=0xbb` (:258), `DMA_TRANSPOSE=0xbd` (:260), `SB2SB_COLLECTIVE=0xbf`
(:262), `GATHER=0x68` (:196), `INDIRECT_COPY=0xe7` (:297), `EXTENDED_INST=0xf0` (:301),
`DMA_GATHER_TRANSPOSE=0xf1` (:302); the RDMA ext-opcodes `EXTENDED_RDMA_DESC_GEN=8` /
`…START=9` are in `aws_neuron_isa_tpb_extended_utils.h:30-31`. [HIGH/OBSERVED]

> **CORRECTION (catalog size — settled).** Earlier drafts cataloged **five** 64-B words and
> folded transpose into one descriptor. The shipped `struct2opcode` map binds **four** DGE
> structs to four *distinct* opcodes — `DIRECT2D_XPOSE → DMA_TRANSPOSE (0xbd)` and
> `GATHER_XPOSE → DMA_GATHER_TRANSPOSE (0xf1)` are **separate** — and the two RDMA extended
> structs are the fifth/sixth. The catalog is **SIX** 64-B words. [HIGH/OBSERVED —
> `struct2opcode`: all four DGE bindings distinct]

> **GOTCHA — two transposes, two gathers, do not conflate.** `DIRECT2D_XPOSE (0xbd)` is
> tile-geometry-driven; `GATHER_XPOSE (0xf1)` is index-array-driven. Separately, the POOL/Q7
> *compute* forms `GATHER (0x68)` and `INDIRECT_COPY (0xe7)` are **not** DMA-side words at all
> — they run an in-SBUF software gather kernel, and `nki.isa.local_gather` lowers to `0xe7`,
> **not** `0x68` ([gather-scatter §0,§7](gather-scatter-descriptors.md)). This page's taxonomy
> is the DMA-side {`0xb8,0xbb,0xbd,0xf1`} + RDMA {8,9}; the compute-side {`0x68,0xe7`} is the
> sibling's subject.

### 1.2 The 16-B BD (`SDMA_CME_BD_DESC`) — the atom the al_udma executes

The 64-B word **expands** into N × 16-B BDs that land in the **DGE_MEMORY** carveout (SoC
`0x2040000000`, 1 GiB; [onchip-working-memory §5](onchip-working-memory.md)) and that the
al_udma SDMA engine drains. The 16-B layout is `{word0 (length + gen/ring tag), word1 (CME
command / CRC controls), buf_ptr (8-B SoC addr the engine reads on M2S / writes on S2M)}`.
The byte-exact bit tables are in [field-tables §1](descriptor-ring-field-tables.md). Two
facts that bite (both CARRIED — `data_transfer.o` DWARF is not in this extraction):

* **The gen-tag repurpose.** Firmware packs the length in `word0[15:0]` and a **2-bit
  generation/ring tag in `word0` byte3 `[1:0]`** (the DWARF `dmb`+`concatenate` positions,
  **not** the DWARF `ringid` at byte3 `[7:6]`); mask `&0xFCFF0000`. The completion busy-poll
  matches **these same two bits** (`comp_BD.byte3 & 0x3`). A clean-room build that trusts the
  DWARF field names mis-tags every descriptor. [CARRIED — [descriptor-model §3 QUIRK](descriptor-model.md)]
* **`optype`/`op`.** A plain staging move is a **CME COPY** (`SDMA_CMETYPE.COPY=0`,
  `SDMAOP.CME=2`); `SDMAOP.CCE=4` drives the in-flight compute engine (§1.3); `SDMAOP.DRE=1`
  is the strided/transpose op.

### 1.3 The CCE compute descriptor — the same 16-B BD, meta-control set

A **CCE** ("Compute-CopyEngine") transfer is the *same* 16-B BD with the meta-control word
set (`op=CCE=4`, `word0` bit-23 "has-meta" flag). The CDMA reads N source streams off the
M2S leg, applies a per-element ALU op (ADD / FMA / MIN / MAX / dtype-convert) **in flight**,
and writes the single reduced result down S2M. **The math is in silicon, not firmware** — the
shipped GPSIMD archive ships only the descriptor *builders*, never an arithmetic kernel
([cce-in-transfer §intro](cce-in-transfer.md)). The op/dtype taxonomy, the FMA operand model
(`acc' = input·scale + acc`), the three dtype nibbles (read-in / accum-wider / write-out),
and the EXT/GCE device-only modes are all in [cce-in-transfer](cce-in-transfer.md). The
collective-reduce tie is §5.3.

> **GOTCHA — `sdma_data_type_size[fp32r] == 0`.** The device size table reports **0** bytes
> for `FP32R`, not 4 — `fp32r` is a round/replicated accumulation tag that does not ride the
> I/O-length path. A reduce-packet sizer computing `dtype_size·N` traps on `fp32r`
> (`encd_get_cce_reduce_packet_size` asserts on the 0). The `kbin_dma_desc_cce_info_t` is
> **140 B**. Both are CARRIED — the host `libnrt.so` carrying them is **not** in this
> extraction (verified absent). [CARRIED — [cce-in-transfer §1.2,§2.1](cce-in-transfer.md)]

---

## 2. The DGE builder + byte-level micro-op encoding

The DGE that **builds** the descriptors runs **device-side**, on the Q7/POOL GPSIMD cores
(the `gather_xpose` header names it: *"uses the SW-DGE backend with Q7 processors in the
Gpsimd engine"*). The host only *configures* it (§3.2's priority/mailbox tables); the device
*generates*. Full pipeline in [dge-builder-qos Part 1](dge-builder-qos.md); byte-level
encoding in [dge-microop-encoding](dge-microop-encoding.md).

### 2.1 The pseudo `dge_op@+15` → resolved word

The compiler emits a **PSEUDO** word (`PSEUDO_DMA_DIRECT2D = 0xd4`) whose byte **`+15` is
`dge_op`** (`NEURON_ISA_TPB_DGE_OPCODE`, the uniform kind selector), with `PSEUDO_ADDR8`
variable-ids for the addresses. The runtime relocation pass resolves the var-ids to absolute
`ADDR8`, **picks the target struct by `dge_op`, and re-packs the per-kind 64-B layout** —
rewriting byte0 to the real opcode:

| `dge_op@+15` (pseudo) | val | resolves to | byte0 | what `+15` becomes in the resolved word |
|---|---|---|---|---|
| `DMA_DIRECT2D` | `0x0` | D1 | `0xb8` | `compute_op` |
| `DMA_INDIRECT1D` | `0x1` | D2 | `0xbb` | `flags` |
| `DMA_TRANSPOSE` | `0x2` | D3 | `0xbd` | `out_dtype` |
| `DMA_GATHER_TRANSPOSE` | `0x3` | D4 | `0xf1` | `src_idx_bound_reg` |

[HIGH/OBSERVED — `DGE_OPCODE` enum (`common.h:831-833`, `DMA_DIRECT2D=0` implied by
declaration order); the four resolved layouts]

> **QUIRK — `+15` is a per-opcode-typed byte.** The uniform `dge_op@+15` field exists **only**
> in the compiler pseudo (`0xd4`). In a *resolved* word the same byte is `compute_op`
> (DIRECT2D), `flags` (INDIRECT1D), `out_dtype` (XPOSE), or `src_idx_bound_reg`
> (GATHER_XPOSE) — keyed by byte0. Pseudo→resolved is a full re-pack, not a byte0 rewrite over
> a shared layout. [HIGH/OBSERVED — [microop-encoding §2.2](dge-microop-encoding.md)]

### 2.2 The three emit micro-ops (GENERATE / DIMPUSH / REGWRITE)

Once the reshape engine resolves `{Reshape Kind, #DMA, dge_shape = num[4]/step[4]}` and a
backend is picked, the DGE emits a descriptor **program** onto a channel `DMA[d]` via three
push primitives. Their byte-exact firmware format strings were carved from the `NX_POOL`
`.rodata` blob in `libnrtucode_internal.so`:

| micro-op | format string | emits |
|---|---|---|
| `GENERATE` | `S: push GENERATE to DMA[%d]: %s : addr=0x%llx, elem_size=%d, sem_num=%i` | ONE data-transfer BD: direction (`%s`=`RD`/`WR`), 64-bit SoC addr → `BD.buf_ptr`, per-element byte size, completion sema |
| `DIMPUSH` | `S: push DIMPUSH to DMA[%d]: [%u,%u][%d,%d]` | ONE loop level — a **pair-of-pairs**: `[src_num,dst_num]` (u16 counts) + `[src_step,dst_step]` (i32 **SIGNED** strides). One DIMPUSH per tensor dim |
| `REGWRITE` | `S: push REGWRITE to DMA[%d]` | inline control reg-write (e.g. `sdma_bcast_base`) ahead of the data BDs; **RETIRED on NC-v4+** (folded into `dge_reshape_memcopy_transpose_fast`) |

[HIGH/OBSERVED — strings carved + offset-verified in [microop-encoding §3](dge-microop-encoding.md)]

Two structural facts the siblings settle:

* **DIMPUSH is a pair-of-pairs, not a `(count, stride)`.** The first entry in each bracket is
  SRC, the second DST; both advance in lock-step. The SIGNED `step` is exactly how a transpose
  is realized — a **stride permutation** of `step_elem[]` (the BEFORE≠AFTER reshape dump),
  with a negative stride reversing an axis. [HIGH/OBSERVED QUIRK — [dge-builder-qos §3.4](dge-builder-qos.md)]
* **The #DIMPUSH == the backend's dim count.** Pool = **2**, software (SWDGE/Q7) = **4**, RTL
  (HWDGE, NC-v3+) = **5+2**. The shape register file `$S[]` is **4-deep**
  (`NUM_DGE_SHAPE_REGISTERS = 4U`). The 64-B word then expands into 16-B BDs in the
  DGE_MEMORY carveout; the **16-B unit is pinned by `MEMCOPY_CARVEOUT_CFG`** (local-reg 39,
  "byte offset / 16", 64-B-aligned). [HIGH/OBSERVED — [dge-builder-qos §4.1,§5.4](dge-builder-qos.md)]

The stream assembly per channel is `[REGWRITE]* + GENERATE + DIMPUSH×Ndims`, then the
tail-pointer doorbell (§3). The byte-exact `$S[]` descriptor-dump templates (Pool/RTL/software
arities) are in [microop-encoding §3.4](dge-microop-encoding.md).

---

## 3. The SDMA hardware engine + QoS / arbitration

The al_udma SDMA is a SoC-mastering Annapurna engine instantiated per DMA channel. A
full-duplex channel = one **M2S** (outbound, reads local) + one **S2M** (inbound, writes
remote), each with **16 queues**, each queue owning a software descriptor ring + an
engine-written completion ring. Register map, submit→complete path, the 3 arbiters, and the
cross-gen deltas are in [udma-hw-engine](udma-hw-engine.md); the QoS class model in
[dge-builder-qos Part 2](dge-builder-qos.md); the SoC channel placement / APB chain in
[sdma-windows-apb](sdma-windows-apb.md).

### 3.1 The two doorbells (the only CPU action that launches a DMA)

The tail-pointer-increment register at queue-relative **`+0x38`** is **THE doorbell**: write
`N` to advance the tail by `N` descriptors. Per-queue stride is `0x1000`, so queue 0's
doorbell is **abs `0x1038`**; the per-queue array (`M2S_Q`/`S2M_Q`) is at engine offset
`0x1000`, `ArraySize 16`, `BundleSizeInBytes 4096`. The field is `val[23:0]`
(`VAL_MASK 0xffffff`), "Increments the value in Q_TDRTP (descriptors)".

| leg | register | abs (queue 0) | from |
|---|---|---|---|
| TX → M2S (READ, HBM→local) | `TDRTP_inc` | `0x1038` | `udma_m2s.json:TDRTP_inc @0x038` |
| RX → S2M (WRITE, local→HBM) | `RDRTP_inc` | `0x1038` | `udma_s2m.json:RDRTP_inc @0x038` |

[HIGH/OBSERVED — both `@0x038`, `M2S_Q`/`S2M_Q @0x1000` ArraySize 16 BundleSize 4096, read
from the Cayman CSR JSON. The descriptor and completion ring layouts are
byte-for-byte identical between the two engines — only the `T`/`R` prefix renames.]

### 3.2 Submit → complete (the HW path)

```
write TDRTP_inc(+0x38) → TDRTP advances → engine sees TDRTP > TDRHP
  → M2S_rd prefetch bursts BDs from TDRBP+TDRHP*16 (TDRHP advances, TDCP = in-flight)
  → data engine reads buf_ptr, moves length_meta bytes (S2M_wr writes on the RX leg)
  → M2S_comp/S2M_comp writes a completion entry (TCRHP/RCRHP advances)  OR
    leaves the gen-tag → semaphore / notification (§6)  OR  gen-tag busy-poll
```

Each step is anchored to its driving register in [udma-hw-engine §3](udma-hw-engine.md); the
inter-step ordering is register-semantics-implied dataflow (MED). The engine is **armed** by
`M2S change_state.normal[0]=1` (a command, not a state — a cold engine is not armed until SW
writes it) and `pref_queue_en.en[15:0]=0xFFFF` (all 16 queues, reset value).

### 3.3 The three arbiters + the rate limiter (RR↔QoS switchable)

Each engine runs **three** arbiters over its 16 queues, each flippable RR↔QoS, plus (M2S
only) a two-level token-bucket rate limiter. At RESET the SDMA is **biased toward FAIRNESS**;
QoS is *armed* by clearing `pref_force_rr`, setting `en_dwrr`, and programming the per-queue
QoS from the `DMAQoSClass` map. [HIGH/OBSERVED — [dge-builder-qos §8](dge-builder-qos.md),
[udma-hw-engine §4](udma-hw-engine.md)]

| arbiter | block | RR↔QoS knob (reset) | QoS source | M2S/S2M |
|---|---|---|---|---|
| prefetch | `M2S_rd@0x300` / `S2M_rd@0x300` | `pref_force_rr` (rst **1 = RR**) | `AXI_qos` / `q_qos` | both |
| packet sched (DWRR) | `M2S_dwrr@0x340` | `en_dwrr` (rst **0 = RR**); DWRR(`q_qos`,`weight`) when 1 | `q_qos`+`weight` | **M2S only** |
| completion | `M2S_comp@0x400` / `S2M_comp@0x380` | `force_rr` (rst **0 = QoS**), `q_promotion` rst 1 | `AXI_qos` | both |
| rate limit | `M2S_rate_limiter@0x380` + `M2S_stream_rate_limiter@0x3c0` | two-level token bucket, **masked at reset** | per-queue `max_burst` + cycle | **M2S only** |

S2M drops the DWRR + rate-limiter blocks entirely (inbound DMA is QoS-tagged but **not**
bandwidth-shaped — the smaller S2M window `0x18000` vs M2S `0x20000` is exactly that missing
egress shaping). The completion arbiter is the *only* one that defaults to QoS.

### 3.4 The two QoS namespaces — `priority_class` (5) vs `DMAQoSClass` (15), and `prio_cap`

The compiler's `DMAQoSClass` is a **17-value** enum (`Unassigned=0`, `Default=1`,
`P0..P14 = 2..16`) — i.e. **15 priority classes P0..P14**. The device-side descriptor field
`DMA_CONFIGS.priority_class` is a **3-bit, 5-value** field gated `<= 4`
(`is_valid_dma_configs`, `common.h:2070-2072`) — **P0..P4**. So the compiler's `DMAQoSClass`
is **saturated** down into the 5-class device band. **Keep them distinct: a descriptor byte
holds `priority_class` (0..4), never `DMAQoSClass`.** The host stages the per-class
*bytes-per-packet* table via `nrtucode_core_dge_set_priority_class_map@0x3094e0`
(`dram_base+0x18`, `n≤4`); the firmware read side is
`nrtucode_core_dge_get_priority_class_map@0x9b10f0` (bounds the index `<= 4`). [HIGH/OBSERVED
— symbol `@0x9b10f0` read; the 17-value `DMAQoSClass` is CARRIED host-cc]

`priority_class` surfaces on the M2S queue as `cfg.AXI_qos` + `dwrr_cfg_2.q_qos@0x84` +
`dwrr_cfg_3.weight@0x88` + the HP-candidate bit; the **`prio_cap`** then bounds the *size* of
a collective-reduce (CCE) packet per class:
`min(dtype_size·N, cc_cce_reduce_prio_pkt_size[class]) & ~0x1F` (32-B-granular,
`encd_get_cce_reduce_packet_size@0x2396f0`, CARRIED host-runtime). §7/§8 decide **which**
queue is served; `prio_cap` decides **how big** each CCE packet on it may be.

> **NOTE — custom-op DMA is a peer, not a special queue.** The `KBIN_DMA_RING_TYPE_CUSTOM_OP`
> marker is the literal **16** — a *ring-type / rel-index marker*, **not** a physical queue
> number. Custom-op, compute, and collective DMA are **peers** on the same 16-queue arbiter,
> separated only by bundle routing + their own `DMAQoSClass`
> ([dge-builder-qos §10](dge-builder-qos.md)). CARRIED — host `libnrt.so`.

---

## 4. The SBUF/PSUM bank model

The descriptors address two on-chip SRAM arrays: a **32 MiB State Buffer (SBUF)** and a
**4 MiB PSUM** accumulator, both organised as **128 partitions** (rows). The full addressing
math, the 16 SBUF / 32 PSUM ECC banks, the matmul→PSUM datapath, and the `axi2sram` crossbar
are in [sbuf-psum-banks](sbuf-psum-banks.md); the region/aperture map in
[onchip-working-memory](onchip-working-memory.md).

### 4.1 The geometry + the addressing keystone

A TPB on-chip address is a **29-bit `PartitionOffset`** (`TPB_PARTITION_ADDR_MASK
= 0x1fffffff`): partition index is the stride-`PARTITION_SIZE` high part, byte offset the low.
SBUF lives in `[0, 0x2000000)`, PSUM in `[0x2000000, 0x2400000)` — **PSUM detection is a pure
magnitude test** (`addr >= 0x2000000`; no bank bit, no region register). Per-gen geometry
(cayman): SBUF **256 KiB partition stride / 224 KiB active**, 128 partitions; PSUM **8 banks ×
2048 B = 16 KiB active**/partition. The SoC region bases are byte-exact in
`cayman/address_map.h`: **STATE_BUF `0x2000000000`** (32 MiB), **STATE_BUF_SCRATCH_RAM
`0x2004000000`** (32 MiB region; the NX *window* the Q7 pins over it is 64 MiB, but the
scratch *region* itself is only 32 MiB —
[onchip-working-memory §1 CORRECTION](onchip-working-memory.md)), **DGE_MEMORY `0x2040000000`**
(1 GiB), **PSUM_BUF `0x2802000000`** (4 MiB). [HIGH/OBSERVED]

### 4.2 The keystone — Q7 reaches SBUF, physically cannot touch PSUM

> **KEYSTONE (HIGH/OBSERVED).** The Q7/GPSIMD cores reach SBUF only as **AXI bus masters**
> through the `axi2sram` bridge + a pinned NX window onto the `0x2000000000` SoC block.
> **PSUM has no AXI aperture and no pinned NX window** — it sits in a *disjoint* top-level
> block based at `0x2800000000` (`STATE_BUF` and `PSUM_BUF` are `0x800000000` = 32 GiB apart),
> reachable only by the PE/ACT/POOL **datapath ports**, which are not AXI-bus-visible. The
> compiler's "customop args/outputs must be in SBUF, never PSUM" rule is therefore **not a
> policy** — it is the physical absence of a PSUM aperture. [HIGH/OBSERVED —
> [sbuf-psum-banks §7](sbuf-psum-banks.md), [onchip-working-memory §3](onchip-working-memory.md)]

This is the reason a GPSIMD-routed `tensor_tensor` op is the `{SBUF, SBUF}` subset of the
verifier's placement predicate, and the reason the `axi2sram` crossbar (`transpose_en` reset
**enabled**) is the *only* path by which a GPSIMD/DGE transpose (D3/D4, the 16-row xbar tile)
lands transposed in 32-B-aligned SBUF. [HIGH/OBSERVED — [sbuf-psum-banks §6](sbuf-psum-banks.md)]

> **NOTE — v5/MAVERICK sizes PSUM out.** The maverick ISA header carries `PSUM_BUF_SZ = 0x0`
> and the maverick address map contains **no PSUM region at all** (`rg PSUM` = 0 matches).
> SBUF re-bases to 128 MiB. v5 interiors are header-OBSERVED → INFERRED.
> [HIGH/OBSERVED the absence; interiors INFERRED]

---

## 5. Cross-die RDMA + collective lowering

Every collective (all-reduce / reduce-scatter / all-gather / send-recv) is built from a
**cross-die SBUF→SBUF** data-plane leg. Full byte-exact decode in
[rdma-cross-die](rdma-cross-die.md); the in-transfer reduce in
[cce-in-transfer §8](cce-in-transfer.md).

### 5.1 The two primitives + the routing-id fold

A cross-die leg = one **`rdma_desc_gen`** (ext-op **8**, `@IRAM 0x161f4`: build the per-engine
CME-COPY BD ring + fold the dst into a routed SoC address + push a LOCAL + a REMOTE sema BD)
followed by one **`rdma_desc_start`** (ext-op **9**, `@IRAM 0x1723c`: drain, PRID-parity
role-split, doorbell). `gen` is off the critical path ("can happen earlier without waiting");
the wait conditions sit on `start`. The on-engine **SB2SB** collective opcode `0xBF` lowers
each leg to exactly this `gen`+`start` pair (call edge `0x3742 → 0x161f4`), but `gen`/`start`
are *also* reusable P2P primitives any kernel can issue directly. [HIGH/OBSERVED —
[rdma-cross-die §1,§6](rdma-cross-die.md)]

**The fold:** `remote_routing_id@+20` (the peer's *routing* id, distinct from
`remote_core_id@+16` the *physical* id) is written into the high bits of the dst SoC address
via the shipped `cayman_addr_decode{,_neighbor}.h` SET macros — either the chip-id view
(`CAYMAN_ID[53:48]` + `CAYMAN_ID_VALID[54]`, cross-package) or the neighbor view
(`EXIT_DIE[51]` + `NEIGHBOR_ROUTE[52]` + `ID_VALID[54]`, other-die-on-package). Bit `[54]` is
the **same physical bit** in both views; the two decoders never coexist in one address.
[HIGH/OBSERVED macros — [rdma-cross-die §2](rdma-cross-die.md)]

### 5.2 The transport + the handshake

The bytes ride the **`io_d2d`** data fabric — DWC-PCIe controller over Marvell XSR SerDes +
iATU OUTBOUND on Cayman; native UCIe on Maverick (re-IP, v5 interiors INFERRED). The
collective DATA bytes and the two completion semaphores ride the `io_d2d` **data** path (AXI
transactions over the SerDes), **NOT** the d2d INTC (which carries zero data-plane triggers).
The TX/RX legs handshake: **RX arms first** (`S2M_Q.RDRTP_inc`, publish empty rx buffers, then
`right_push` signal), **TX fires second** (`left_pop`-wait the signal, then `M2S_Q.TDRTP_inc`
= the CME COPY launch) — ordering RX-before-TX prevents writing into an unarmed receive
aperture. [HIGH/OBSERVED — [rdma-cross-die §3,§4](rdma-cross-die.md)]

### 5.3 The in-SDMA CCE reduce as the AllReduce leg

The "reduce" leg of a ring/mesh all-reduce is a **CCE ADD/MIN/MAX/FMA over the neighbor source
streams**, landing the reduced chunk at the dest on the *same* M2S/S2M ring, same doorbell,
same completion as a plain copy — only the CCE meta-control bits are set. The compiler-level
`PSEUDO_TRIGGER_COLLECTIVE` op (ADD=`0x04`, dtype FP32=`0x0A`) lowers host-side to
`SDMA_CCETYPE_ADD` reduce packets; the on-engine SB2SB collective reduces via the
`encd_mesh_reduce_sb2sb → add_dma_packet_cce` chain. **Reduce-and-broadcast** (the all-reduce
final leg) is a single CCE packet with `num_dests > 1` on Cayman+ (arch > 2), built by the EXT
combo path. [HIGH/OBSERVED callers + reduction-type enum — [cce-in-transfer §4.3,§8](cce-in-transfer.md)]
See the collectives Part for the host-side algorithm selection: the
[S3D3 collective (SB2SB)](../collectives/ops/s3d3-collective.md), the
[unified collective architecture](../collectives/ops/architecture-synthesis.md), the
[ALL_REDUCE op](../collectives/ops/all-reduce.md), and the
[NCFW DMA-reprogram + APB broadcast](../collectives/ncfw/dma-reprogram-apb-bcast.md).

---

## 6. The unified completion model

An SDMA transfer signals completion **three ways**; the choice is per-path. All three
ultimately gate on the same 2-bit BD generation tag underneath. [HIGH/OBSERVED —
[udma-hw-engine §5](udma-hw-engine.md), [rdma-cross-die §5](rdma-cross-die.md)]

| path | mechanism | used by |
|---|---|---|
| **gen-tag busy-poll** | the BD's `word0` byte3 `[1:0]` 2-bit gen tag; SW polls `comp_BD.byte3 & 0x3 == expected` before reusing a ring slot — **no interrupt, no notification** | the custom-op inline memcpy staging path (synchronous) |
| **EVT_SEM semaphore inc** | on completion the engine increments the GENERATE `sem_num` in the EVT_SEM **INC** window | the DGE GENERATE path; the cross-die two-semaphore protocol |
| **NOTIFIC → SW notification queue** | the DDMA NOTIFIC block emits `{dma_map:16, num_packets:12, dma_group_id:1}` into a HW notification queue, mirrored as a phase-bit ring | the DGE/execute async path |

### 6.1 The EVT_SEM increment target — `+0x1800`

The completion-semaphore increment lands in the EVT_SEM aperture. The four 4-B-stride
operation windows on the 256-entry semaphore array are `READ @0x1000`, `SET @0x1400`,
**`INC @0x1800`**, `DEC @0x1C00` (`tpb_semaphores_inc` ArraySize 256). The EVT_SEM region base
is SoC `0x2802700000`, so the **atomic-increment window is abs `0x2802701800`** = base
+ `0x1000` (semaphore sub-block) + `0x800`. The completion sema increment of a local DMA
writes `EVT_SEM.inc(local_sem)` on this core; a cross-die remote sema BD writes
`EVT_SEM.inc(remote_sem)` on the *peer* (routed by `routing_id` over `io_d2d`, exactly like
the data — not an interrupt). [HIGH/OBSERVED — `tpb_semaphores_inc @0x1800` + flat-map
`TPB_0_EVT_SEM_SEMAPHORE_INC base 0x2802701800`]

### 6.2 The completion-ring write-back gate — the resolved `en_comp_ring_update` contradiction

Whether the al_udma writes a completion-ring entry at all is gated per queue by
`comp_cfg.en_comp_ring_update[0]`. Two earlier drafts disagreed on its S2M reset value
(P9.7/[udma-hw-engine §2.2](udma-hw-engine.md) said **S2M reset = 1**; an earlier draft of
P9.10/[field-tables §6.2](descriptor-ring-field-tables.md) claimed **reset = 0 on BOTH
engines**). The Cayman RTL JSON grounds it directly; the field-tables page has
since been corrected to match, and both pages now agree.

> **CORRECTION (resolved — field-tables now fixed to match).** The Cayman RTL JSON shows
> `en_comp_ring_update` reset is **engine-asymmetric**:
>
> | engine | field | abs (queue 0) | reset | source |
> |---|---|---|---|---|
> | **M2S** | `comp_cfg.en_comp_ring_update[0]` | `0x10a0` | **`0x0` (OFF)** | `cayman-arch-regs/csrs/sdma/udma_m2s.json` |
> | **S2M** | `comp_cfg.en_comp_ring_update[0]` | `0x1054` | **`0x1` (ON)** | `cayman-arch-regs/csrs/sdma/udma_s2m.json` |
>
> So **P9.7 (udma-hw-engine) is CORRECT** — S2M write-back is ON by default, the *opposite*
> posture to M2S — and the earlier P9.10 (descriptor-ring-field-tables §6.2) claim of "rst 0
> for both engines" was wrong. **[descriptor-ring-field-tables.md](descriptor-ring-field-tables.md)
> §6.2 has been corrected to match**: its `S2M_Q.comp_cfg.en_comp_ring_update` row now reads
> **rst 1**, and the CORRECTION callout beneath it has been reversed. The S2M *companion*
> defaults P9.10 lists were already correct (`dis_comp_coal=1`, `first_pkt_promotion=1`,
> `buf2_len_location=1`) — only the `en_comp_ring_update` reset was mis-stated. Confirmed on
> **both** the Cayman RTL JSON and
> the Mariana customop-shipped JSON (S2M=1, M2S=0 on both). [HIGH/OBSERVED — `udma_s2m.json`
> `comp_cfg@0x54.en_comp_ring_update ResetValue 0x1`; `udma_m2s.json`
> `comp_cfg@0xa0.en_comp_ring_update ResetValue 0x0`]

The practical consequence: the **inline custom-op staging path leaves M2S at its reset 0** (no
completion-ring entry — it busy-polls the gen tag instead), while the **DGE/execute path turns
S2M write-back on** (reset already ON) and consumes the completion ring + the NOTIFIC
notification. A re-implementer must program `en_comp_ring_update` per engine from these
*asymmetric* resets, not copy the M2S posture onto S2M.

---

## 7. End-to-end — one worked data-movement walkthrough

A 2-D BF16 memcpy from a NEFF, walked through every layer (the field placements + the
`dge_op` kind + the doorbell binding are HIGH/OBSERVED; the numeric values are illustrative).
This is the canonical `q_gradient_in` leg from the C08220 NEFF
([microop-encoding §7.2](dge-microop-encoding.md)): a 128×1 FP32 copy, here re-cast as a
128-element BF16 row for compactness, `src = input` (SBUF), `dst = gradient_in` (SBUF), sem 10.

```
LOGICAL    copy 128 BF16 elements, row-major, SBUF→SBUF, completion semaphore 10.

[A compile]  bir::InstDMACopy{ DGEType, DMAQoSClass=P2, ADDR4 MEM_PATTERN3D }            (CARRIED)
             -> PSEUDO_DMA_DIRECT2D (byte0=0xd4), dge_op@+15 = DMA_DIRECT2D (0x0),
                src/dst = PSEUDO_ADDR8 compiler-var ids.

[B load]     kbin lowers -> dma_desc / mem_ref -> typed ring KBIN_DMA_RING_TYPE_CUSTOM_OP(16)  (CARRIED)
             DMAQoSClass=P2 -> wire QoS = (P2-1) -> saturates into DMA_CONFIGS.priority_class (0..4).

[C emit]     runtime relocation resolves the var-ids -> absolute ADDR8, picks the D1 layout by
             dge_op, rewrites byte0 -> DMAMEMCPY (0xb8); semaphore=10, sem_increment=1, compute_op=NONE.
             device DGE: "DGE contexts ... init sw=0 engine=E queue_idx=Q"; reshape resolves the
             2-D pattern; backend = Pool (2-dim). dge_shape = { elem_size=2, num_elem=[128,1],
             step_elem=[1,128] } for src and dst (no transpose -> src==dst pattern).
             on DMA[d]:   push GENERATE to DMA[d]: RD : addr=&input, elem_size=2, sem_num=10
                          push DIMPUSH  to DMA[d]: [128,128][1,1]      ; dim0 counts 128/128, steps 1/1
                          push DIMPUSH  to DMA[d]: [1,1][128,128]      ; dim1 counts 1/1, steps 128/128
                          -> ONE GENERATE + 2x DIMPUSH (Pool = 2 dims).

[D expand]   the D1 word -> N x 16-B SDMA_CME_BD_DESC in the DGE_MEMORY carveout (0x2040000000):
             128*1*2 = 256 B, one chunk (<= 64 KiB); word0 = length | (gen_tag << 24), mask &0xFCFF0000;
             optype=COPY(0), op=CME(2); buf_ptr = src SoC addr on the M2S/read BD.

[E fire]     memw; memw; write num_descriptors to M2S TDRTP_inc (abs 0x1038 for queue 0) -> launch.
             (the symmetric q_gradient_out WRITE leg fires S2M RDRTP_inc, sem 11.)

[F schedule] the engine's 16-queue arbiters serve the BD: prefetch (rst RR), DWRR (rst RR),
             completion (rst QoS) -> unless QoS is armed (clear pref_force_rr + set en_dwrr +
             program AXI_qos/q_qos/weight from the P2 class), the schedule is plain round-robin.

[G complete] custom-op inline: busy-poll comp_BD.byte3 & 0x3 == expected_gen, then reuse the slot
             (M2S en_comp_ring_update reset 0 -> no completion-ring entry).
             DGE/execute path instead: completion ring write-back (S2M en_comp_ring_update reset 1)
             + NOTIFIC {dma_map:16} -> SW notification queue.
             EVT_SEM.inc(sem 10) lands at abs 0x2802701800 + 10*4 (the INC window).
```

For a **cross-die** transfer the [C emit] step is `rdma_desc_gen` (fold the dst routing-id
into the SoC high bits + build the ring + 2 sema BDs), [E fire] is `rdma_desc_start`
(RX arms S2M tail + `right_push`; TX `left_pop`-waits then rings M2S tail), and [G complete]
is the two-semaphore protocol (local_sem release / remote_sem data-ready, the remote inc
routed over `io_d2d` to the peer's EVT_SEM `+0x1800` aperture). §5; full byte trace in
[rdma-cross-die §8](rdma-cross-die.md).

---

## 8. Confidence ledger + the cross-page reconciliation

**HIGH/OBSERVED (verified against shipped cayman/mariana artifacts):** the six
64-B descriptor structs + the `struct2opcode` bindings + the opcode bytes
`0xb8/0xbb/0xbd/0x68/0xe7/0xf1/0xbf/0xf0` + RDMA ext `8/9` (§1.1); the `dge_op@+15`
pseudo→resolved map (§2.1); the GENERATE/DIMPUSH/REGWRITE strings + the Pool/SW/RTL dim counts
(§2.2); the `TDRTP_inc`/`RDRTP_inc @0x038 = abs 0x1038`, stride `0x1000`, ArraySize 16 (§3.1);
the three arbiters + reset biases + the S2M-no-shaping window asymmetry (§3.3); the 5-value
`priority_class` (`<=4` gate, `…get_priority_class_map@0x9b10f0`) vs the 15-class compiler
`DMAQoSClass` (§3.4); the SoC region bases STATE_BUF `0x2000000000` / SCRATCH `0x2004000000` /
DGE_MEMORY `0x2040000000` (1 GiB) / PSUM_BUF `0x2802000000` (§4.1); the Q7-cannot-reach-PSUM
keystone (disjoint `0x2000000000` vs `0x2800000000` blocks) (§4.2); the RDMA `gen`/`start`
ext-opcodes + the routing-id fold macros + the io_d2d data-vs-INTC split (§5); the EVT_SEM INC
window `@0x1800` = abs `0x2802701800` (§6.1); and **the `en_comp_ring_update` resolution: S2M
reset = `0x1`, M2S reset = `0x0`, on both Cayman and Mariana JSON** (§6.2).

**CARRIED (grounded in a prior survey of `data_transfer.o` / `libnrt.so` — binaries not in
this gpsimd extraction; re-verify before reuse):** the 16-B BD WORD0/WORD1 bitfields + the
gen-tag repurpose (§1.2); the `cce_info` 140-B struct + `sdma_data_type_size[fp32r]=0` +
`encd_get_cce_reduce_packet_size@0x2396f0` `prio_cap` (§1.3, §3.4); the Layer-A/B
compiler/host IR + `KBIN_DMA_RING_TYPE_CUSTOM_OP=16` + `dma_is_custom_op_dma_v2` (§2,§3.4);
the host topology (`EdgeRemoteMLA`, `C2CPortsPerMla=4`, `seng^2`) (§5).

**INFERRED / WALLS:** v5/MAVERICK interiors are header-OBSERVED only — `PSUM_BUF_SZ=0` and the
absent PSUM region are OBSERVED, but the v5 inline-descriptor runtime semantics, the UCIe PHY,
and any v5 firmware-side behaviour are INFERRED (no v5 NX-POOL firmware ships here).

**Reconciliation note (§6.2).** This synthesis resolved the one known open cross-page
contradiction about `S2M_Q.comp_cfg.en_comp_ring_update`: the truth is **S2M rst 1, M2S rst
0** (engine-asymmetric), confirmed on both the Cayman and Mariana JSON.
[udma-hw-engine.md](udma-hw-engine.md) §2.2 was correct, and
[descriptor-ring-field-tables.md](descriptor-ring-field-tables.md) §6.2 — whose earlier draft
claimed rst 0 for both engines — has been corrected to match. All three pages now agree.

---

## Cross-references (all eleven Part-9 siblings + the collectives Part)

- [The DMA / Descriptor / Memory Subsystem](descriptor-model.md) — P9.1, the four-layer
  pipeline + the Layer-C catalog + the udma CSR programming sequence overview.
- [Gather/Scatter + Gather-Transpose Descriptors](gather-scatter-descriptors.md) — P9.2, the
  four index-driven words byte-level + the three address-gen mechanisms.
- [RDMA Cross-Die SBUF→SBUF P2P](rdma-cross-die.md) — P9.3, the `gen`/`start` decode, the
  routing-id fold, the io_d2d transport, the two-semaphore completion.
- [CCE (Compute-DMA) In-Transfer Compute](cce-in-transfer.md) — P9.4, the in-flight ALU op +
  the host→device remap + the EXT/GCE modes + the all-reduce reduce leg.
- [DGE Descriptor-Builder + SDMA QoS / Arbitration](dge-builder-qos.md) — P9.5, the
  build→schedule pipeline + the `DMAQoSClass` model + the three arbiters + the `prio_cap`.
- [On-Chip State-Buffer (SBUF) + PSUM Bank Model](sbuf-psum-banks.md) — P9.6, the bank
  geometry + the matmul→PSUM datapath + the `axi2sram` crossbar + the keystone.
- [The al_udma Hardware DMA Engine](udma-hw-engine.md) — P9.7, the register map + the
  submit→complete HW path + the iDMA + the cross-gen deltas.
- [DGE Micro-Op Encoding (byte-level)](dge-microop-encoding.md) — P9.8, the three byte layers +
  the `dge_op@+15` pinning + the v5 inline-descriptor model + the worked transpose.
- [Descriptor + Ring Field-Table Reference](descriptor-ring-field-tables.md) — P9.10, the
  per-field lookup appendix (the byte rows this page summarises). **Owes the §6.2 fix.**
- [SDMA Address Windows + APB Chain](sdma-windows-apb.md) — P9.11, the SoC channel
  base/stride/aperture + the APB config path.
- [On-Chip Working-Memory Regions](onchip-working-memory.md) — P9.12, the region/aperture map +
  the SoC→Q7 NX-local window TLB.
- Collectives: [S3D3 collective (SB2SB, 0xBF)](../collectives/ops/s3d3-collective.md),
  [the unified collective architecture](../collectives/ops/architecture-synthesis.md),
  [ALL_REDUCE](../collectives/ops/all-reduce.md),
  [NCFW DMA-reprogram + APB broadcast](../collectives/ncfw/dma-reprogram-apb-bcast.md).
