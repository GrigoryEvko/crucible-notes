# DGE Descriptor-Builder + SDMA QoS / Arbitration

A GPSIMD DMA is **built** then **scheduled**. This page reconstructs both halves end to
end:

1. **The builder** — how a logical N-D tensor transfer (`bir::InstDMA` on the host,
   lowered through the kbin descriptor IR) compiles into the device DGE's
   `GENERATE` / `DIMPUSH` / `REGWRITE` micro-op stream and lands as 16-byte SDMA
   *block descriptors* (BDs) in a queue ring: the multi-dim loop-nest → `DIMPUSH`
   stride stream, the 64-B→16-B descriptor packing, and the **swdge↔hwdge** split
   (HWDGE = the device RTL backend, NeuronCore-v3+).
2. **The scheduler** — the SDMA queue arbitration / QoS: the `DMAQoSClass` priority
   model, where it surfaces on the 16 M2S/S2M per-queue CSRs, the **three RR↔QoS
   arbiters** each UDMA engine runs over its 16 queues, the **priority cap**
   (`prio_cap` / `cc_cce_reduce_prio_pkt_size[class]`), and how a custom-op DMA
   (the queue-bundle CUSTOM_OP rel-index `16`) is scheduled relative to compute and
   collective DMA.

This page is the **synthesis** page for the DMA subsystem. The per-stage device-firmware
detail lives on the firmware DGE pages — [DGE Setup + Context Init](../firmware/dge/dge-setup.md),
[DGE 3-Backend Selector](../firmware/dge/dge-backend-selector.md),
[DGE Descriptor-Emit Path](../firmware/dge/dge-emit.md); the host-private
configuration trio on [The DGE Host-Private API](../runtime/dge-host-api.md); and the
bit-exact micro-op/BD encoding is deferred to [DGE Micro-Op Encoding](dge-microop-encoding.md)
and the [descriptor model](descriptor-model.md). This page stitches them into the
single build→schedule pipeline and grounds the QoS half in the byte-exact UDMA CSR
schemas.

> **Scope & provenance.** Every fact derives from static analysis of shipped,
> redistributable artifacts: the GPSIMD NX-POOL device firmware (`libnrtucode.a`
> members, decoded with the native Cadence `xtensa-elf-objdump`,
> `XTENSA_CORE=ncore2gp`); the host `libnrtucode_internal.so` / `libnrtucode.so`
> (x86-64, the DGE format-string corpus + the priority/mailbox API); the shipped C
> ISA headers (`neuron_cayman_arch_isa/tpb`, compile-checked with `gcc`); the shipped
> Cayman UDMA CSR JSON schemas (`csrs/sdma/udma_m2s.json`, `udma_s2m.json`); and the
> host runtime `libnrt.so` (the custom-op DMA dispatch + the CCE prio-cap, decompiled
> via the IDA sidecars). Confidence per the [confidence model](../reference/confidence-model.md):
> HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED. Callouts: **QUIRK** (counter-intuitive but
> real), **GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a naive or
> sibling reading), **NOTE** (orientation).

> **GOTCHA — the device-side DGE logic lives in `libnrtucode_internal.so`, the host
> custom-op dispatch + CCE prio-cap in `libnrt.so` — two distinct host binaries.** The
> DGE format-string corpus (`S: push GENERATE …`, `S: DGE contexts … init sw=%d …`,
> `S: DGE: Select backend Pool`) and the `nrtucode_core_dge_set_priority_class_map`
> API are in `libnrtucode_internal.so` / `libnrtucode.so` (the customop export sits at
> `0x3094e0`). The custom-op-DMA detection (`dma_is_custom_op_dma_v2` `@0x22e120`),
> the queue-bundle setup (`dma_ring_setup_queue_bundles` `@0x22e630`), and the prio-cap
> (`encd_get_cce_reduce_packet_size` `@0x2396f0`) are in the **host runtime** `libnrt.so`
> — a separate package (`aws-neuronx-runtime-lib`), not the GPSIMD customop distribution.
> Their addresses below are byte-verified in that runtime binary's decompile; treat them
> as **cross-package CARRIED** facts about the host runtime, not facts about the GPSIMD
> firmware image. **[binary partition HIGH/OBSERVED — symbol present-in/absent-from each
> sidecar this session.]**

---

## 0. One-screen orientation — build, then schedule

```
  PART 1 — BUILD  (host → device DGE → 16-B SDMA BD ring):

    [compile]  bir::InstDMA{ DGEType, DMAQoSClass, ADDR4 MEM_PATTERN3D }     (host cc)
    [load]     kbin lowers → dma_desc / mem_ref IR → typed ring
                  (KBIN_DMA_RING_TYPE_CUSTOM_OP = 16 for the GPSIMD/Pool stream)
    [setup]    device DGE binds a (tx,rx) context PAIR:
                  "S: DGE contexts … init sw=%d engine=%u queue_idx=%u queue_num=%u"
                  + "S: rx.base=0x%llx, tx.base=0x%llx"                       (NX-POOL fw)
    [reshape]  analyze_tensor_reshape → pair-assess → {Reshape Kind, #partitions, #DMA};
                  the N-D loop nest → dge_shape = { num[4], step[4] } ($S[i], 4-deep);
                  a transpose = a SIGNED stride PERMUTATION
    [select]   backend = Pool(2-dim, fw) / RTL(5+2-dim, HW = HWDGE) / software(4-dim, Q7
                  = SWDGE), from a runtime-populated BSS availability table
    [emit]     per channel DMA[d]:
                  [REGWRITE]*           control/trigger reg-write (RETIRED on NC-v4+)
                  GENERATE              addr + elem_size + sem_num (one data BD)
                  DIMPUSH × #dims       [num,num][step,step] (the loop nest, SIGNED steps)
    [pack]     each 64-B DIRECT2D / INDIRECT1D / GATHER_XPOSE word EXPANDS into N× 16-B
                  BDs in the DGE Pool carveout (16-B unit pinned by MEMCOPY_CARVEOUT_CFG)
    [fire]     bump the queue tail-pointer inc CSR (M2S TDRTP_inc / S2M RDRTP_inc) by
                  the descriptor count → RDM tail write-back + 2-bit ringId inject

  PART 2 — SCHEDULE  (the 16-queue arbiter + QoS):

    DMAQoSClass (P0..P14)  →  priority_class[2:0] (saturated 0..4, 5 classes)
        ├─ M2S_Q.cfg.AXI_qos[30:28]        — per-queue AXI QoS (prefetch + completion)
        ├─ M2S_Q.dwrr_cfg_2.q_qos[7:0]     — per-queue DWRR scheduling level
        ├─ M2S_Q.dwrr_cfg_3.weight[7:0]    — per-queue DWRR weight
        └─ AXI_M2S_MLA.…high_priority.enable — per-queue HP descriptor-fetch candidate
    THREE arbiters per engine, each RR↔QoS switchable:
        prefetch (M2S_rd/S2M_rd)  · DWRR packet sched (M2S_dwrr; M2S only) · completion
        (M2S_comp/S2M_comp)   + a two-level token-bucket rate limiter (M2S only)
    prio_cap : a collective-reduce (CCE) packet is byte-capped per class:
        min(dtype_size*N, cc_cce_reduce_prio_pkt_size[class]) & ~0x1F
    custom-op DMA (CUSTOM_OP rel index 16) is a PEER of compute/collective DMA on the
        same 16-queue arbiter — separated only by bundle routing + its DMAQoSClass.
```

The single direction bit threaded through both halves: **READ = HBM→local = M2S =
`TDRTP_inc`**; **WRITE = local→HBM = S2M = `RDRTP_inc`** — the `%s` of the
`GENERATE` format string resolves to `RD` (M2S, `addr` = source) or `WR` (S2M,
`addr` = destination).

---

# PART 1 — THE DGE DESCRIPTOR-BUILDER

## 1. The compile→load front — `bir::InstDMA` → kbin → typed ring

### 1.1 `bir::InstDMA` (host compiler) — two descriptor-relevant fields

The compiler emits a `bir::InstDMA` (`InstDMACopy` / `InstLoad` / `InstSave` /
`InstAbstractCopy` / `CollectiveCompute`); each carries two fields that steer the
device build and the schedule:

| field | enum values | what it steers |
|---|---|---|
| `DGEType` (`bir::InstDMA+0xF8`) | `None=0`, `SWDGE=1`, `HWDGE=2`, `Unassigned=3` (ctor sentinel) | **which device backend builds the descriptors** (§4) |
| `DMAQoSClass` | `Unassigned=0`, `Default=1`, `P0..P14 = 2..16` (17 values) | the **QoS class** the SDMA arbiter uses (§7–§8) |

The access pattern is a 64-B compute word = three 16-B `MEM_PATTERN3D` slots
(`src0`/`src1`/`dst`), addresses `ADDR4`. The `DGEType` is resolved during codegen by
the swdge↔hwdge selector (§4.4): a `GenericLoad`/`Store`-codegen predicate
(`can_use_dge`) gated by `dge_par_min_size` (a 16-element partition-alignment
threshold) **and** `enable_dge_on_indirect_dma`; `HWDGE` is **NeuronCore-v3+ only**
("HWDGE is only supported for NeuronCore-v3+").

**[CARRIED/HIGH — the `bir::InstDMA` field set and enum values are recovered from the
host neuronx-cc analysis (DX-DMA-01/DX-CC-02); they are not in the GPSIMD ISA headers,
which begin at the device descriptor. Treat as cross-component CARRIED.]**

### 1.2 kbin lowering — the typed ring (CUSTOM_OP = 16)

At load, the host runtime lowers the NEFF into an in-memory `dma_desc` / `mem_ref`
descriptor IR:

```
dma_desc { dma_desc_data        → the D1/D2/D3 transfer descriptor(s),
           dma_desc_event       → a REGWRITE control entry,
           dma_desc_inc_semaphore → the local/remote semaphore BDs };
mem_ref  { mem_ref_sp, _io, _buffer, _tmp_buf, _pointer, _remote_variable,
           _list / _ptr_table };
```

These lower onto a **typed ring** keyed by `kbin_dma_ring_type`.
`KBIN_DMA_RING_TYPE_CUSTOM_OP(16)` is the GPSIMD/Pool custom-op transfer ring; a
GPSIMD kernel's descriptors are exactly `mem_ref_sp` + `dma_desc_inc_semaphore` on
ring 16. The literal **16** is the *routing marker* (the CUSTOM_OP ring type), **not**
a physical queue number — §10 settles that the physical queue is the
`rel_queue_idx` the queue bundle maps it to within the engine's 16. **[CARRIED/HIGH —
host runtime; the `16`↔CUSTOM_OP binding is byte-confirmed in `dma_is_custom_op_dma_v2`,
§10.]**

---

## 2. The device DGE pipeline — where the build happens

The DGE that **builds** the descriptors runs **device-side**, on the Q7/POOL GPSIMD
cores — the shipped `gather_xpose` ISA header names the backend verbatim: *"This
instruction uses the SW-DGE backend with Q7 processors in the Gpsimd engine"*
(`aws_neuron_isa_tpb_dma_gather_xpose.h:17`, OBSERVED this session). The host only
*configures* it (the priority/mailbox tables, §7.2); the device *generates*. The build
is a four-stage pipeline, all in the `*_NX_POOL` firmware images
([DGE Setup](../firmware/dge/dge-setup.md) / [Selector](../firmware/dge/dge-backend-selector.md) /
[Emit](../firmware/dge/dge-emit.md)):

| stage | translation unit | what it does | string anchor |
|---|---|---|---|
| 1. SETUP | `dge_decode_fast.cpp` | bind a `(tx,rx)` context pair; classify into a Reshape Kind | `S: DGE contexts 0x%x/0x%x init sw=%d engine=%u queue_idx=%u queue_num=%u` |
| 2. RESHAPE | `dge_reshape.cpp` | lower the N-D reshape/transpose into `dge_shape{num[4],step[4]}` (§3) | `S: DGE Reshape: Analyzed tensor (…)` |
| 3. SELECT | `dge_backend_rtl.cpp` | one BSS availability read → Pool / RTL / software (§4) | `S: DGE: Select backend Pool` (`@VA 0x83157`) |
| 4. EMIT | (same TU) | `[REGWRITE]* + GENERATE + DIMPUSH×#dims` onto `DMA[d]` (§5) | `S: push GENERATE to DMA[%d]: …` |

The setup string and the `rx.base`/`tx.base` line were re-found this session in
`libnrtucode_internal.so` `.rodata` (the host build embeds the same DEBUG corpus the
firmware DRAM carries). **[stage strings HIGH/OBSERVED.]**

### 2.1 The setup context — the `(tx, rx)` pair binds to the rings

```
S: DGE contexts 0x%x/0x%x init sw=%d engine=%u queue_idx=%u queue_num=%u
S: rx.base=0x%llx, tx.base=0x%llx
```

| field | meaning |
|---|---|
| `sw=%d` | the SOFTWARE-vs-HARDWARE backend tier: `sw=1` → Q7 SWDGE; `sw=0` → a HW backend (Pool/RTL). The device realization of the compiler `DGEType` (SWDGE↔`sw=1`; HWDGE↔`sw=0`/RTL). |
| `engine=%u` | which DMA engine instance the context binds to. |
| `queue_idx` / `queue_num` | the UDMA queue binding: `queue_idx` is this stream's slot within a pool of `queue_num` queues (the 16 M2S/S2M queues, §8). |
| `rx.base` / `tx.base` | the SoC base addrs of the **S2M (rx, write)** and **M2S (tx, read)** descriptor rings this context owns. |

So `tx` = the M2S/read ring (`TDRTP_inc` doorbell), `rx` = the S2M/write ring
(`RDRTP_inc` doorbell). **[`sw=%d`/`engine`/`queue_idx`/`queue_num` + `rx.base`/`tx.base`
HIGH/OBSERVED; the tx↔M2S / rx↔S2M binding HIGH/INFERRED from the direction model.]**

---

## 3. The multi-dim loop nest → DIMPUSH strides — the reshape→emit core

This is the heart of the builder: how an N-D logical transfer becomes the `DIMPUSH`
stride stream. The reshape engine reasons in SBUF `(partition, free)` coordinates,
resolves the loop nest into a 4-deep shape, and the emit issues **one `DIMPUSH` per
dimension**.

### 3.1 The reshape analysis

`analyze_tensor_reshape` (per tensor):

```
S: DGE Reshape: Analyzed tensor (nelem_target=%u, step_target=0x%X (%dP),
   base=0x%X (%dP)): Reshape Strategy=%d, Requested Reshape Kind=%d,
   Partition Range=%d, Num Partitions=%d
```

→ per tensor: total element count, target stride (in **partitions**, the `%dP`), base,
a Reshape Strategy + a (Requested) Reshape Kind, a Partition Range + Num Partitions.

Tensor-PAIR assess resolves the FINAL Reshape Kind + the `#DMA` fanout (a reshape
spanning P partitions across D DMA channels):

```
… Assessed tensor pair params (Requested Reshape Kind=%d, Min Num Partitions=%u,
   #DMA Avail=%u): Reshape Kind=%d, #DMA=%u
```

`#DMA Avail` is the `MEMCOPY_DMA_CFG` (local-reg **40**) **lower-16** bitfield ("which
DMAs in the TPB may be used by this engine", header verbatim §5.4); the upper-16 is
"which queues … may be used" (§5.4). **[reshape strings HIGH/OBSERVED; the
`#DMA Avail` ↔ `MEMCOPY_DMA_CFG.lo16` binding HIGH/OBSERVED — header comment.]**

### 3.2 The working state — `dge_shape = { num[4], step[4] }`

The reshape's internal tensor representation (logged BEFORE/AFTER, byte-exact):

```
{ elem_size:u16, num_elem[4]:u16, step_elem[4]:i32, is_src:bool }
```

== the `dge_shape { num[4], step[4] }` held in the `$S[i]` shape-register file, which
is **4-deep** (`NEURON_ISA_TPB_NUM_DGE_SHAPE_REGISTERS = 4U`,
`aws_neuron_isa_tpb_common.h:34`, OBSERVED). `num_elem[4]` / `step_elem[4]` ARE the
loop-nest bounds + strides of the (up to) 4-D walk.

> **CORRECTION — the shape-register symbol IS `NEURON_ISA_TPB_NUM_DGE_SHAPE_REGISTERS
> = 4U`.** The [backend-selector page §4](../firmware/dge/dge-backend-selector.md) carries
> a CORRECTION claiming the symbol name is *not* `NUM_DGE_SHAPE_REGISTERS` (it offered
> `NEURON_ISA_TPB_n`). Re-grepping the shipped header this session, the symbol is
> verbatim `static const uint32_t NEURON_ISA_TPB_NUM_DGE_SHAPE_REGISTERS = 4U;` at
> `aws_neuron_isa_tpb_common.h:34`, and `is_valid_dge_shape_reg` checks `reg_num <
> NUM_DGE_SHAPE_REGISTERS` at `:2077`. The [emit page §5.1](../firmware/dge/dge-emit.md)
> uses the same correct symbol. The 4-deep fact holds on every page; the *name* the
> selector page disputes is in fact present. The `NEURON_ISA_TPB_n` artifact is a
> name-obfuscated placeholder in that header, not the real symbol. **[HIGH/OBSERVED —
> `rg -n NUM_DGE_SHAPE_REGISTERS` this session.]**

### 3.3 Transpose = a SIGNED stride permutation

A transpose is realized as a **permutation of the `step_elem[]` / `num_elem[]`
arrays** — the BEFORE log differs from the AFTER log; the axis-swap becomes a stride
reorder. The `i32` SIGNED `step_elem` allows **negative strides** (reverse-axis
walks). For the 2-byte gather-transpose Kind, `tensor_reshape_transpose` builds a 64-B
`GATHER_XPOSE` descriptor whose DMA/xbar HW transposes **16×128 tiles for 2-B
dtypes** (BF16/FP16); the shipped header pins the constraints
(`aws_neuron_isa_tpb_dma_gather_xpose.h`, OBSERVED this session):

```
// - Only 2B data types supported initially (BF16/FP16)
// - Xbar transpose tiles are 16x128 for 2B dtypes
// - Index tensor must be UINT32, multiple of 16 indices
// - Destination must be 32B aligned (xbar transpose requirement)
```

**[transpose field set + the four constraints HIGH/OBSERVED; the BEFORE≠AFTER
permutation reading HIGH/INFERRED.]**

### 3.4 The loop-nest → DIMPUSH mapping — one DIMPUSH per dimension

Once `num_elem[4]` / `step_elem[4]` are set (post-transpose), the DGE issues **one
`DIMPUSH` per dimension**:

```
S: push DIMPUSH to DMA[%d]: [%u,%u][%d,%d]
       [%u,%u] = a (num, num) pair — the u16 COUNTS (loop bounds): SRC and DST counts
       [%d,%d] = a (step, step) pair — the i32 SIGNED STRIDES: SRC and DST strides
```

As annotated C (the body is FLIX-desynced — see the [emit page](../firmware/dge/dge-emit.md)
§0 — so this is grounded on the format string + the symmetric `src_*`/`dst_*` struct
arrays, MED on the exact src/dst pairing):

```c
/* one DIMPUSH per tensor dimension. The bracket is a PAIR-OF-PAIRS: the two entries in
 * each bracket are the SRC and DST legs of THIS dim — DIMPUSH advances both cursors in
 * lock-step, matching the descriptor's parallel src_*/dst_* step/num arrays.          */
void push_DIMPUSH(int d, uint16_t num_src, uint16_t num_dst,
                  int32_t step_src, int32_t step_dst)
{
    LOG("S: push DIMPUSH to DMA[%d]: [%u,%u][%d,%d]", d, num_src, num_dst, step_src, step_dst);

    desc.src_num_elem[k]  = num_src;   desc.dst_num_elem[k]  = num_dst;   /* u16        */
    desc.src_step_elem[k] = step_src;  desc.dst_step_elem[k] = step_dst;  /* i32 SIGNED */
    /* k advances per DIMPUSH; #DIMPUSH == the backend's descriptor dim count (§4)       */
}
```

The number of `DIMPUSH`es == the descriptor's dim count for the chosen backend:
**Pool = 2, software(SWDGE) = 4, RTL(HWDGE) = 5+2** (§4.1). The accumulated `(num,step)`
pairs form the descriptor's nested loop-nest, which the engine then expands into the
BD stream — one inner-loop iteration → one strided BD, or the inner two dims fold into
a single 2-D strided BD via `src/dst_step_elem[2]` + `num_elem[2]` of the `DIRECT2D`
struct.

> **QUIRK — `DIMPUSH` is a pair-of-pairs, not one `(count, stride)`.** A reimplementer
> who reads `[%u,%u][%d,%d]` as a single `(num, step)` will mis-walk the dst cursor.
> The first entry in each bracket is SRC, the second is DST; both advance together for
> the dimension. The signedness of `[%d,%d]` is the point: a negative stride is a
> reverse-axis walk, exactly how a transpose realizes an axis permutation by reordering
> `step_elem[]` rather than touching data. **[`[%u,%u][%d,%d]` shape + `int32_t
> step_elem[]` widths HIGH/OBSERVED; src/dst pairing MED.]**

---

## 4. The swdge↔hwdge split + the backend selector

The descriptor BUILDER differs by backend; the backend is a **two-tier** decision —
the compiler picks `DGEType`, the device DGE picks Pool/RTL/software among the
available backends.

### 4.1 The three device backends — the field set is the signature

Each backend dumps the descriptor it built; the field set IS its signature (decoded in
full on the [backend selector §2](../firmware/dge/dge-backend-selector.md)):

| backend | dims | cast | indir | reshape | compute_op | expansion | exec engine | reached via |
|---|---|---|---|---|---|---|---|---|
| **Pool** | 2 | yes | no | no | yes | firmware | POOL descriptor path | `table[0]` set — the PRIMARY/default |
| **software** (SWDGE) | 4 | yes | yes | yes | no (in log) | firmware | Q7 cores (SW-DGE) | context `sw=1` |
| **RTL** (HWDGE) | 5+2 | no | no | no | no | **hardware** | RTL DGE + RDM | RTL slot available (NC-v3+) |

`DIRECT2D` (Pool) `compute_op` can fuse a reduction: `NONE=0, ADD=1, MULTIPLY=2,
MAX=3, MIN=4` — so a Pool descriptor can do `B op= A` in transfer. RTL is the *widest
but barest* (pure transport, no compute), and is the **priority-class-aware HW path**.
All three emit 16-B BDs into the DGE Pool carveout and ring the same M2S/S2M
tail-pointer doorbell; the difference is **where the multi-dim expansion happens** —
Pool/software in firmware (the `DIMPUSH` loop-nest / Q7 spray), RTL in hardware (the
RTL DGE expands the 5+2-dim form). **[field sets HIGH/OBSERVED; expansion-locus split
HIGH/INFERRED — backend-selector page.]**

### 4.2 The selector logic — a single availability-table read

After reshape resolves Kind + #DMA, the DGE reads one backend-availability table in a
fixed BSS global (CAYMAN `0x5da0` / MARIANA+ `0x5fe0`); the byte-exact Pool-select
anchor (CAYMAN NX-POOL IRAM `@0xf544`):

```text
f544: const16 a4, 0x5da0   ; &avail_table (BSS)
f547: const16 a10,0x3157   ; &"S: DGE: Select backend Pool"
f54c: l32i.n  a3, a4, 0    ; a3 = table[0]  (Pool availability flag)
f54e: beqz    a3, 0xf59f   ; !available → "NO BACKEND FOUND, doing nothing"
```

`table[0]!=0` → Pool (log + emit); else fall through to RTL/software (the
`dge_backend_rtl.cpp` body site that loads the same table base into `a2` is
FLIX-desynced past the read). The table is runtime-populated at boot/setup from a HW-
capability probe (the BSS carve is zero). **[`table[0]=Pool` + `beqz` HIGH/OBSERVED;
RTL/software slot indices MED — desynced.]**

### 4.3 The two-tier selection

| tier | when | decides |
|---|---|---|
| **1** (context, at setup) | the `sw=%d` flag | SOFTWARE (`sw=1`, Q7 SWDGE) vs a HARDWARE backend (`sw=0`). The software backend has NO "Select backend software" label — it is the `sw=1` path, not an arm of the availability selector. |
| **2** (per-descriptor, among HW backends) | the availability-table gate | Pool (`table[0]`) vs RTL vs NO-BACKEND. |

### 4.4 The compiler swdge↔hwdge mapping — the 16-element threshold

The compiler resolves `DGEType` in `codegenGenericLoad/Store`. The indirect-DMA path
is gated by `enable_dge_on_indirect_dma` AND `dge_par_min_size` (a partition-count
threshold) → `can_use_dge`:

- **True** → `NeuronIndirect{Load,Save,RMW}` with `DGEType ∈ {SWDGE=1, HWDGE=2}`
  (HWDGE NeuronCore-v3+ only → the device RTL backend).
- **False** → `must_be_indirect_dma`, a non-DGE indirect DMA (`DMA_INDIRECT 0xbb`)
  with CPU-precomputed descriptors.

The 16-element threshold, grounded at a call site:

```python
dge_mode = dge_mode.unknown if T % 16 == 0 else dge_mode.swdge   # moe_token_gen.py
```

→ when the partition count `T` is 16-aligned the compiler lets the device pick
(Unassigned → `can_use_dge` resolves HW/SW); a ragged tail forces SWDGE. So **HWDGE
(the device RTL backend, NC-v3+) wants 16-element-aligned partition tiles**; otherwise
SWDGE. The reconciliation across layers:

| layer | SWDGE | HWDGE |
|---|---|---|
| compiler `DGEType` | `SWDGE=1` | `HWDGE=2` (NC-v3+ only) |
| device context | `sw=1` | `sw=0` (HW backend) |
| device backend | software (4-dim, Q7 walks) | RTL (5+2-dim, RTL DGE HW expands) |
| expansion | firmware (Q7 spray/decode) | hardware (RTL DGE + RDM) |
| feature set | cast + indirection + reshape | pure wide transport, prio-aware |

**[CARRIED/HIGH each compiler row (host neuronx-cc); each device row HIGH/OBSERVED; the
column identity INFERRED-HIGH.]**

---

## 5. The emit micro-ops + the 64-B → 16-B descriptor packing

### 5.1 GENERATE — emit one data-transfer descriptor

```text
S: push GENERATE to DMA[%d]: %s : addr=0x%llx, elem_size=%d, sem_num=%i
```

| operand | meaning | 64-B `DIRECT2D` field | lowers to 16-B BD |
|---|---|---|---|
| `%d` (`DMA[d]`) | target DMA channel (`MEMCOPY_DMA_CFG.lo16` index) | — (channel) | selects which ring |
| `%s` (`RD`/`WR`) | direction: read-from-src (M2S) vs write-to-dst (S2M) | direction | M2S vs S2M ring |
| `addr=0x%llx` | 64-bit SoC addr | `src_start_addr`(+16) / `dst_start_addr`(+40), `ADDR8` | `buf_ptr` |
| `elem_size=%d` | per-element byte size | `src_elem_size`(+36) / `dst_elem_size`(+60), u16 | × `DIMPUSH` counts → BD length |
| `sem_num=%i` | completion semaphore | `semaphore`(+13) + `sem_increment`(+14) | BD completion sem (fired `@dmacomplete`) |

The `%s` resolves to the byte-adjacent `.rodata` literals `RD` / `WR` (read this
session in the emit-page carve). One `GENERATE` = one data BD; for an indirect gather,
one `GENERATE` per gathered row. **[string HIGH/OBSERVED; the addr/elem_size/sem→field
map HIGH/INFERRED — emit page §4.1.]**

### 5.2 REGWRITE — an inline control-register write

```text
S: push REGWRITE to DMA[%d]
```

The leanest op (no field args) — it pushes a register-write entry into the descriptor
stream (e.g. programming a trigger/broadcast base before the data BDs), a **control-op**
BD distinct from the COPY-op data BD. **RETIRED on MARIANA_PLUS (NC-v4+)** — folded
into the `dge_reshape_memcopy_transpose_fast` streamlined emit. Per-gen presence
(`rg -c -a` over the carved DRAM `.rodata`, from the [emit page §4.3](../firmware/dge/dge-emit.md)):

| gen | `push GENERATE` | `push DIMPUSH` | `push REGWRITE` |
|---|---|---|---|
| SUNDA (v2) | 0 | 0 | 0 (no DGE emit on v2) |
| CAYMAN (v3) | 1 | 1 | **1** |
| MARIANA (v4) | 1 | 1 | **1** |
| MARIANA_PLUS (v4+) | 1 | 1 | **0** ← retired |

**[op existence + retirement HIGH/OBSERVED; the exact control-op BD optype MED —
FLIX-desynced body, forward-resolved on [dge-microop-encoding](dge-microop-encoding.md).]**

### 5.3 The descriptor opcodes — struct → opcode binding

The `instruction_mapping.json` `struct2opcode` table (OBSERVED this session) binds
each 64-B descriptor struct to its TPB opcode:

| 64-B struct | TPB opcode | DGE Kind |
|---|---|---|
| `NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT` | `OPCODE_DMA_MEMCPY` | `DGE_OPCODE_DMA_DIRECT2D` |
| `NEURON_ISA_TPB_DMA_DIRECT2D_XPOSE_STRUCT` | `OPCODE_DMA_TRANSPOSE` | `DGE_OPCODE_DMA_TRANSPOSE` |
| `NEURON_ISA_TPB_DMA_INDIRECT1D_STRUCT` | `OPCODE_DMA_INDIRECT` | `DGE_OPCODE_DMA_INDIRECT1D` |
| `NEURON_ISA_TPB_DMA_GATHER_XPOSE_STRUCT` | `OPCODE_DMA_GATHER_TRANSPOSE` | `DGE_OPCODE_DMA_GATHER_TRANSPOSE` |

The DGE-side opcode namespace is a 4-member enum (`DGE_OPCODE_MAX_PLUS_ONE = 4`,
`aws_neuron_isa_tpb_common.h:100`, OBSERVED) — `DIRECT2D` / `INDIRECT1D` / `TRANSPOSE` /
`GATHER_TRANSPOSE`, indices 0..3 in declaration order. **[struct→opcode binding
HIGH/OBSERVED; the numeric 0..3 ordering INFERRED from declaration order.]**

> **NOTE — there is a fourth DGE Kind, `DIRECT2D_XPOSE` (`DMA_TRANSPOSE`).** The backing
> report and the firmware backend logs enumerate three descriptor Kinds (DIRECT2D,
> INDIRECT1D, GATHER_XPOSE). The `struct2opcode` table OBSERVED this session carries a
> **fourth**, `DMA_DIRECT2D_XPOSE_STRUCT → OPCODE_DMA_TRANSPOSE` (a non-gather 2-D
> transpose), distinct from the gather-transpose. It is a `DGE_OPCODE` member but does
> not surface as a separate backend log line — it shares the Pool/RTL emit path. A
> reimplementer enumerating DGE descriptor Kinds must include all four. **[HIGH/OBSERVED
> — `struct2opcode` + the `DGE_OPCODE` enum this session.]**

### 5.4 The 64-B → 16-B expansion + the carveout

`GENERATE` writes the 64-B HIGH-LEVEL TPB DMA word (`DIRECT2D` / `INDIRECT1D` /
`GATHER_XPOSE`, all `ISA_STATIC_ASSERT(sizeof == 64)`, `gcc`-verified); the backend
EXPANDS it into 16-B BD ring entries (one or more per partition / per length-chunk).
The 16-B unit is PINNED by `MEMCOPY_CARVEOUT_CFG` (local-reg **39**, header comment
verbatim this session):

```c
// DGE Pool carveout configuration (only usable on Pool).
// - lower 16 bits are the carveout region's offset in each partition *in number of descriptors*
//   (that is, the byte offset / 16)
//   - The carveout must be aligned to 64 bytes, a DMA restriction
// - upper 16 bits are the carveout region size in number of descriptors.
//   - The size of the carveout region (minus the reserved ones) limits the maximum number
//     of descriptors in a single memcopy
#define AWS_NEURON_ISA_XT_MEMCOPY_CARVEOUT_CFG  39
```

The "byte offset / 16" comment is the proof that **one descriptor = 16 bytes** and the
region must be 64-byte aligned; the upper-16 hard-caps the per-memcopy descriptor
count. The companion `MEMCOPY_DMA_CFG` (local-reg **40**) is `{ lo16 = which DMAs this
engine may use, hi16 = which queues }`. The composed BDs land in the DGE Pool carveout.

> **CORRECTION — `SDMA_CME_BD_DESC` is NOT a C struct in this distribution.** The
> backing report and the [backend-selector page §2](../firmware/dge/dge-backend-selector.md)
> both name `SDMA_CME_BD_DESC` as the 16-byte BD struct. Re-checking this session
> (`rg --no-ignore` over the whole `c10/include` tree), **no such C struct exists in
> the `0.21.2.0` GPSIMD distribution** — the name is a carried cross-analysis label.
> What is OBSERVED here is the **16-byte descriptor *unit*** (pinned by
> `MEMCOPY_CARVEOUT_CFG` above) and the three 64-B high-level structs. The 16-B BD's
> internal layout (`buf_ptr` slot, the u16 length-with-64-KiB-chunking, the WORD0/WORD1
> COPY-vs-control optype, the 2-bit ringId) is **CARRIED cross-analysis**, and its
> bit-exact packing is deferred to [dge-microop-encoding](dge-microop-encoding.md). The
> per-field 64-B→16-B *mapping* (which high-level field feeds which BD slot) is
> HIGH/INFERRED. **[16-B unit HIGH/OBSERVED; BD field layout CARRIED; mapping
> HIGH/INFERRED.]**

### 5.5 Stream assembly + fire + the three bound layers

Per channel `DMA[d]`, the program assembles, in issue order:

```
  (1) [REGWRITE]*   control/trigger        (CAYMAN/MARIANA; gone NC-v4+)
  (2)  GENERATE     the data BD (addr + elem_size + sem)
  (3)  DIMPUSH × N  the loop bounds + strides, one per dim
  (4)  the TAIL-POINTER DOORBELL: bump M2S TDRTP_inc (read) / S2M RDRTP_inc (write) by
       num_descriptors → RDM (the Ring-Descriptor-Manager) write-backs the tail +
       injects the 2-bit ringId
```

Three orthogonal bound layers gate every build (from the [emit page §5.4](../firmware/dge/dge-emit.md)):

1. **Per-descriptor ADDRESS** — `src`/`dst` `addr <= limit`, register-indirect via
   two `BOUND_CHECK_REG`s (`{bc_reg:6, bc_disable_oob_error_notif:1, bc_enabled:1}`,
   `sizeof == 1`); log `bounds:(%1d 0x%llx<=0x%llx, %1d 0x%llx<=0x%llx)` (src then dst).
2. **Total-BYTE equality** — `is_valid_src_dst_element_count_without_cce`:
   `(src_num_elem * src_elem_size) == (dst_num_elem * dst_elem_size)`.
3. **Descriptor-COUNT vs ring** — the Q7 `DescriptorStream` guard
   `P%i: ERROR: DescriptorStream wrote %d descriptors, expected %d` plus the
   `MEMCOPY_CARVEOUT_CFG` size cap.

**[composition + bound layers HIGH/OBSERVED; the exact (1)–(3) issue order MED —
FLIX-desynced body.]**

---

## 6. Part-1 reconciliation — the per-gen builder map

| feature | SUNDA (v2) | CAYMAN (v3) | MARIANA (v4) | MARIANA_PLUS (v4+) | MAVERICK (v5) |
|---|---|---|---|---|---|
| `dge_shape{num/step}` | yes | yes | yes | yes | INFERRED |
| reshape strategy / transpose | stub | yes | yes | yes | INFERRED |
| DGE setup + backend select + push | no | yes | yes | yes | INFERRED |
| GENERATE / DIMPUSH | no | yes | yes | yes | INFERRED |
| REGWRITE | no | yes | yes | **NO** (folded) | INFERRED |
| `gather_xpose` descriptor (HWDGE path) | no\* | yes | yes | yes | header-only |
| `dge_reshape_memcopy_transpose_fast` | no | no | no | **YES** | INFERRED |

\* SUNDA ships `dma_indirect1d.h` but not `gather_xpose.h`; the full DGE builder
arrives with CAYMAN. **No MAVERICK NX-POOL firmware ships in this `libnrtucode.a`**
(`ar t | rg -i maverick` empty); the MAVERICK *ISA headers* do ship (the
`gather_xpose` descriptor survives with the same "SW-DGE backend with Q7 processors"
comment), so the ISA surface persists, but MAVERICK runtime backend behaviour is **not
derivable here** — every v5-interior claim is INFERRED. **[per-gen presence
HIGH/OBSERVED CAYMAN..MARIANA_PLUS; MAVERICK header-OBSERVED / runtime-INFERRED.]**

---

# PART 2 — THE SDMA QUEUE ARBITRATION / QoS MODEL

## 7. The DMAQoSClass priority model — host class → wire → per-queue CSR

### 7.1 The class enum + the wire mapping

`DMAQoSClass` (compiler, 17 values): `Unassigned=0`, `Default=1`, `P0..P14 = 2..16`.
The wire-byte QoS = `class − 1` for `P0..P14`; `0` for Unassigned/Default. It lands in
`NEURON_ISA_TPB_DMA_CONFIGS.priority_class`, a **3-bit** field (OBSERVED, header
`aws_neuron_isa_tpb_common.h:713–716`):

```c
typedef struct NEURON_ISA_TPB_DMA_CONFIGS {
    uint8_t priority_class    : 3;   // priority class
    uint8_t reserved_bitfield : 5;   // must be zero
} NEURON_ISA_PACKED NEURON_ISA_TPB_DMA_CONFIGS;   // sizeof == 1

// is_valid_dma_configs:  (priority_class <= 4) && (reserved_bitfield == 0)   // :2070–2071
```

`priority_class <= 4` → **five valid classes, 0..4**. `DMA_CONFIGS` sits at **+12 in
DIRECT2D**, **+13 in GATHER_XPOSE**, **+61 in INDIRECT1D** (all OBSERVED this session,
header offsets). So the 17-value compiler `DMAQoSClass` is **saturated** into a 3-bit,
5-class device field: `P0..P3` map to wire 1..4; `P4..P14` and Default/Unassigned
collapse onto the legal 0..4 band. **[the 3-bit field + the `<=4` gate + the three
descriptor offsets HIGH/OBSERVED; the `DMAQoSClass` enum CARRIED (host cc); the 17→5
saturation INFERRED-HIGH from the gate.]**

### 7.2 The host priority-class map — `dram_base + 0x18`, a *bytes-per-packet* table

Before the device DGE runs, the host writes the class table via
`nrtucode_core_dge_set_priority_class_map(core, priority_classes, n)`
(`libnrtucode.so` `@0x3094e0`, byte-verified this session; `libnrtucode_internal.so`
copy `@0x9b1000`). It is a single bulk `n*4`-byte write of a `uint32_t[≤4]` to
`dram_base + 0x18`, gated on `boot_state == 1` and an NX-POOL core-kind mask:

```c
// libnrtucode.so @0x3094e0 ; rdi=core, rsi=priority_classes (uint32_t[]), rdx=n
nrtucode_result_t nrtucode_core_dge_set_priority_class_map(
        nrtucode_core_t *core, const uint32_t *priority_classes, size_t num_elements)
{
    if (core == NULL) abort();                         // null-arg → hard abort (programmer error)
    if (core->boot_state != 1 /*BOOTED_LEGACY*/) return 8;
    uint64_t k = core->coretype;                       // NX_POOL-kind gate:
    if (k > 0x20 || !((0x102020204ULL >> k) & 1)) return 8;  // bits {2,9,17,25,32}
    if (priority_classes == NULL) abort();
    if (num_elements > 4) return 8;                     // cap = FOUR user classes
    size_t len = num_elements << 2;                     // n*4 bytes — flat bulk copy
    return core->context->write(core->context,
                                core->dram_base + 0x18, len, priority_classes);  // host→device DRAM
}
```

> **CORRECTION — each `u32` in the `+0x18` table is the per-class *preferred bytes per
> packet for DGE data transfer*, NOT an absolute priority / weight / queue-selector.**
> The backing report (and NX-030) flagged the semantic of these `u32`s as LOW
> ("absolute priority vs weight vs queue-selector — Q7-side, not in the host
> binaries"). The [host-api page](../runtime/dge-host-api.md) resolves it from the
> shipped `nrtucode.h` doc, byte-confirmed: ISA class 0 is the **reserved firmware
> default** (not host-writable through this table); `priority_classes[0..3]` map to ISA
> classes **1..4**; each value is the **"preferred bytes per packet for DGE data
> transfer"** for that class — a transfer-tuning hint, not a queue or weight field. The
> write is a single bulk `4*n`-byte transfer (`shl rdx,2`; one `write` call; no
> per-element loop). So `4` user-settable (ISA 1..4) + `1` reserved default (ISA 0) =
> the 5 classes the device `priority_class<=4` gate reads. The queue/arbitration the
> table appears to "configure" is realized device-side; the host value supplies only
> the bytes-per-packet hint. **[HIGH/OBSERVED — the `cmp rdx,0x4` cap + the `+0x18`
> base from disasm; the bytes-per-packet semantics from the shipped header.]**

The NX-POOL kind mask `0x102020204` sets bits `{2,9,17,25,32}` =
`{SUNDA,CAYMAN,MARIANA,MARIANA_PLUS,MAVERICK}_NX_POOL`. The customop build
(`libnrtucode.so`) gates MAVERICK out (`cmp eax,0x19`; mask `0x2020204` =
`{2,9,17,25}`); the internal build keeps it (`movabs 0x102020204`). **[mask + per-build
delta HIGH/OBSERVED — host-api page.]**

> **QUIRK — there is a SECOND host throttle: the DGE *mailbox* (`dram_base + 0x28`),
> a per-priority 16-bit DMA-channel enable mask.** Distinct from the bytes-per-packet
> table, the host also stages up to **four** `nrtucode_dge_mailbox_t` entries
> (`{uint8_t reserved; uint8_t priority; uint16_t bitmask}`, `sizeof == 4`) at
> `dram_base + 0x28` (`nrtucode_core_get_dge_mailbox_addr` `@0x9b11e0`). The runtime
> rule (header doc): the device starts with all DMAs enabled (`0xFFFF`); **for each
> mailbox, if the DGE operation's priority > the mailbox priority, AND-mask the 16-bit
> DMA bitmask** — i.e. a higher-numbered (lower-priority) DGE op may be denied some of
> the 16 DMA channels. The 16-bit `bitmask` is exactly the `MEMCOPY_DMA_CFG.lo16`
> "which DMAs" space (§5.4). A default `{0, 0xFF, 0xFFFF}` never masks (priority `0xFF`
> is lowest → nothing exceeds it). This is the device-side `dma_mask` the descriptor
> emit gates on — the host's per-priority *channel-budget* knob, complementing the
> bytes-per-packet table and the per-queue QoS below. **[HIGH/OBSERVED — host-api page;
> the `+0x28` base, the 4-byte struct, the per-index `+0x28+idx*4`, and the
> priority>mailbox AND-mask rule.]**

### 7.3 Where the class surfaces on the M2S queue (Cayman CSR, byte-exact)

`priority_class` → the per-queue M2S CSR fields (all re-read this session from
`csrs/sdma/udma_m2s.json`, the Cayman schema):

| CSR field | abs offset | bits | reset | role |
|---|---|---|---|---|
| `M2S_Q.cfg.AXI_qos` | `0x1020` | `[30:28]` | `0x0` | the queue's AXI QoS — used in AXI transactions AND by the prefetch + completion arbiters |
| `M2S_Q.dwrr_cfg_2.q_qos` | `0x1084` | `[7:0]` | `0x0` | the per-queue DWRR QoS level (queues with equal `q_qos` are RR-scheduled) |
| `M2S_Q.dwrr_cfg_3.weight` | `0x1088` | `[7:0]` | `0x0` | the per-queue DWRR weight |
| `AXI_M2S_MLA.cfg_tdr_req_candidate_high_priority.enable` | `0x0008` | `[15:0]` | `0x0000` | per-queue HIGH-PRIORITY descriptor-fetch candidate (one bit per queue; "introduced in Cayman") |

> **CORRECTION — `q_qos` is at `dwrr_cfg_2` (`0x1084`), not the "`+0x1080` band".** The
> backing report places `q_qos` in a "`+0x1080` band". The Cayman schema is exact:
> `0x1080` is `M2S_Q.dwrr_cfg_1` (`{pause[25], strict[24], max_deficit_cnt_size[23:0]}`),
> `0x1084` is `dwrr_cfg_2.q_qos[7:0]`, `0x1088` is `dwrr_cfg_3.weight[7:0]`. The
> report conflated `dwrr_cfg_1` and `dwrr_cfg_2`. A reimplementer must write `q_qos` to
> `queue_base + 0x84`, not `+0x80`. **[HIGH/OBSERVED — `udma_m2s.json` register offsets
> this session.]**

So the compiler's `DMAQoSClass` becomes a per-queue AXI QoS + a DWRR `q_qos`/`weight`
+ a high-priority-candidate bit. **[all four fields HIGH/OBSERVED — the JSON schema;
the `priority_class → these fields` programming edge HIGH/INFERRED.]**

---

## 8. The 16-queue arbitration — three arbiters, RR↔QoS switchable

Each UDMA M2S (outbound) and S2M (inbound) engine owns **16 queues**
(`M2S_Q`/`S2M_Q` `ArraySize = 16`, each `BundleSizeInBytes = 4096 = 0x1000`,
OBSERVED). The M2S register window is `131072 = 0x20000`; the S2M window is
`98304 = 0x18000` — the S2M is smaller because it **drops the egress shaping** (§8.5).
Each engine runs **three** arbiters over its 16 queues, each flippable RR↔QoS. All
offsets below are byte-exact from the Cayman `udma_m2s.json` / `udma_s2m.json` this
session.

### 8.1 The DWRR packet scheduler (`M2S_dwrr @0x340`; M2S only)

```c
// M2S_dwrr.cfg_sched @0x340 (RW):
en_dwrr      [0]      rst=0  // "Enable the DWRR scheduler. If this bit is 0, queues with
                            //  same QoS will be served with RR."  → RR is the DEFAULT.
pkt_mode_en  [4]      rst=0  // 0 = byte mode, 1 = packet mode
weight_inc   [9:8]    rst=2  // exponential weight increment between DWRR iterations
inc_factor   [19:16]  rst=7  // weight multiply factor, power of 2 ; 7 → 128 bytes
// M2S_dwrr.ctrl_deficit_cnt @0x344 : init[23:0] — initialises all queues' deficit counters
```

Per-queue deficit is read back via `M2S.sel_dwrr_status.deficit_cnt[23:0]` (`@0x244`,
in the `M2S` indirect-status block `@0x200`), selected by `M2S.indirect_ctrl.q_num`
(`@0x234`, `[11:0]`). When `en_dwrr=1` the scheduler serves queues in proportion to
`q_qos`/`weight` (deficit-weighted round-robin); when `0`, plain RR among equal-QoS
queues. **[all fields + offsets HIGH/OBSERVED.]**

### 8.2 The prefetch arbiter (`M2S_rd @0x300`)

```c
// M2S_rd.desc_pref_cfg_2 @0x304 :
pref_force_rr [16]   rst=1  // "Force RR arbitration in the prefetch arbiter and packet
                          //  scheduler (post rate control). 0 = standard arbitration based
                          //  on queue QoS ; 1 = force round robin."  *** RESET = 1 → RR ***
max_desc_per_pkt [11:0] rst=0x040
// M2S_rd.desc_pref_cfg_3 @0x308 : pref_thr[15:8]=0x10 (fetch threshold),
//   min_burst_above_thr[7:4]=0x4, min_burst_below_thr[3:0]=0x1  — the FIFO refill policy
```

The descriptor-prefetch arbiter **defaults to RR**; QoS-based prefetch is opt-in (clear
`pref_force_rr`). **[HIGH/OBSERVED.]**

### 8.3 The completion arbiter (`M2S_comp @0x400`)

```c
// M2S_comp.cfg_1c @0x400 :
force_rr      [25]    rst=0   // 0 → QoS-based completion arbitration is the DEFAULT
q_promotion   [24]    rst=1   // promote the in-progress queue in the completion scheduler (ON by default)
q_free_min    [31:28] rst=0   // min free completion entries to qualify for promotion
unack_fifo_depth [19:8] rst=0x100   // unacknowledged FIFO size (descriptors)
comp_fifo_depth  [7:0]  rst=0x40    // completion FIFO size (descriptors per queue)
// M2S_comp.cfg_coal @0x404 : timer[31:0] = 0x0186A0 (=100000) — the completion-coalescing timer
```

> **QUIRK — the completion arbiter is the only one that defaults to QoS.** `force_rr`
> resets to `0` (QoS) on M2S_comp, whereas the prefetch (`pref_force_rr` rst 1) and DWRR
> (`en_dwrr` rst 0) default to RR. And `q_promotion` resets to **1** — the in-progress
> queue is promoted by default. **[HIGH/OBSERVED — note: the backing report omitted the
> `q_promotion` reset = 1.]**

### 8.4 The two-level rate limiter (M2S only)

```c
// M2S_rate_limiter.gen_cfg @0x380 : short_cycle_size[15:0]=0x0FA, pkt_mode_en[24]=0
//   — the byte/packet token-fill cycle.  (block has gen_cfg/ctrl_cycle_cnt/ctrl_token only)
// M2S_stream_rate_limiter.cfg_1s @0x3C0 : en[24]=0, pause[25]=0, max_burst_size[23:0]=0xFFFFFF
//   — the stream-side token bucket cap.
// M2S_stream_rate_limiter.mask @0x3D4 : internal_rate_limiter[1]=1, external_rate_limiter[0]=1
//   — MASKED at reset → no rate limiting until armed.
// Per-queue override: M2S_Q.rate_limit_cfg_1 @0x1060 : en[24], pause[25], max_burst_size[23:0]=0xFFFFFF
// Token state read-back: M2S.sel_rate_limit_status.token_cnt[23:0] @0x240 (via indirect_ctrl.q_num)
```

> **CORRECTION — the rate-limiter knobs span two distinct blocks; the report folds
> them.** The backing report attributes `short_cycle_size`, the stream `en`/`max_burst`,
> and the `mask` all to one block. The Cayman schema splits them: `short_cycle_size`
> (`0x0FA`) is in **`M2S_rate_limiter.gen_cfg @0x380`** (which has no `mask` and no
> `en`/`max_burst_size`); the stream `en`[24] / `max_burst_size`[23:0]=`0xFFFFFF` is in
> **`M2S_stream_rate_limiter.cfg_1s @0x3C0`**; and the `mask`
> (`internal_rate_limiter[1]` / `external_rate_limiter[0]`, both reset `1`) is in
> **`M2S_stream_rate_limiter.mask @0x3D4`**. Both rate limiters are masked at reset.
> **[HIGH/OBSERVED — block-by-block register lists this session.]**

### 8.5 The S2M (RX) side — QoS without egress shaping

`S2M_Q` is the ring renamed `TDR→RDR`, but the S2M window is `0x18000` (vs M2S
`0x20000`) because S2M **drops the egress shaping**: the JSON has **no `S2M_dwrr`
block, no rate-limiter blocks** (verified: zero `dwrr` / `rate_limit` keys). S2M keeps
only QoS tagging + the two RR↔QoS arbiters:

| S2M field | abs offset | bits | reset | note |
|---|---|---|---|---|
| `S2M_Q.cfg.AXI_qos` | `0x1020` | `[30:28]` | `0x0` | per-queue AXI QoS |
| `S2M_Q.qos_cfg.q_qos` | `0x1060` | `[7:0]` | `0x0` | per-queue QoS (no DWRR weight reg — "RX does not shape") |
| `S2M_rd.desc_pref_cfg_2.pref_force_rr` | `0x0304` | `[16]` | `0x1` | RX prefetch arbiter — RR by default, like M2S |
| `S2M_comp.cfg_1c.force_rr` | `0x0380` | `[16]` | `0x0` | RX completion arbiter — QoS by default (bit **16** here, vs bit **25** on M2S) |

So inbound DMA is QoS-tagged (`AXI_qos` + `q_qos`) but NOT bandwidth-shaped — the
egress M2S side carries the DWRR + rate-limiter machinery. **[all S2M fields + the
no-DWRR/no-rate-limiter absence HIGH/OBSERVED — `udma_s2m.json` this session.]**

### 8.6 The arbiter summary

| arbiter | block | RR↔QoS knob (reset) | QoS source | M2S/S2M |
|---|---|---|---|---|
| prefetch | `M2S_rd` / `S2M_rd` | `pref_force_rr` (rst **1 = RR**) | `AXI_qos` / `q_qos` | both |
| packet sched | `M2S_dwrr` | `en_dwrr` (rst **0 = RR**); DWRR(`q_qos`,`weight`) when 1 | `q_qos` + `weight` | **M2S only** |
| completion | `M2S_comp` / `S2M_comp` | `force_rr` (rst **0 = QoS**), `q_promotion` rst 1 | `AXI_qos` | both |
| rate limit | `M2S_rate_limiter` + `M2S_stream_rate_limiter` | two-level token bucket (`mask` rst 1 = **masked**) | per-queue `max_burst` + global cycle | **M2S only** |

> **NOTE — at RESET the SDMA is biased toward FAIRNESS.** Prefetch RR, DWRR off,
> rate-limiters masked — only the completion arbiter is QoS by default. QoS-based
> scheduling is ARMED by clearing `pref_force_rr` + setting `en_dwrr` + programming the
> per-queue `AXI_qos`/`q_qos`/`weight` from the `DMAQoSClass` map. A reimplementer who
> programs `q_qos`/`weight` but never clears `pref_force_rr` / sets `en_dwrr` gets a
> pure round-robin schedule regardless of class. **[HIGH/OBSERVED — the four reset
> values.]**

---

## 9. The priority cap (`prio_cap`) — per-class CCE reduce packet size

The QoS class also bounds the **size** of a collective-reduce (CCE) DMA packet. The
prio_cap is `encd_get_cce_reduce_packet_size` in the host runtime `libnrt.so`
(`@0x2396f0`), decompiled byte-exact this session:

```c
// libnrt.so @0x2396f0 ; /opt/workspace/KaenaRuntime/tdrv/encd.c
size_t encd_get_cce_reduce_packet_size(const encd_comm *comm, SDMA_DTYPE dtype)
{
    /* ctx pointer + its current priority class (NOT the descriptor field directly): */
    encd_context *ctx = comm->...->mark;                    // asserts ctx != NULL  (encd.c:0x1200)
    uint8_t cls = encd_get_ctx_curr_priority_class(ctx);
    assert(cls <= 4);                                       // MAX_DMA_PKT_PRIO_NUM   (encd.c:0x1202)

    size_t cap = nrt_gconf()->cc_cce_reduce_prio_pkt_size[cls];   // 5-entry per-class cap table
    size_t esz = sdma_data_type_size[dtype];
    assert(esz > 0);                                        // (encd.c:0x1204)
    size_t want = esz * N;                                  // N = element count (*(int*)(mark+24))

    size_t v = (want <= cap) ? want : cap;                  // min(want, cap)
    if (cap) want = v;
    return want & 0xFFFFFFFFFFFFFFE0ULL;                    // & ~0x1F  → round DOWN to 32 B
}
```

So a CCE reduce packet is `min(dtype_size * N, cc_cce_reduce_prio_pkt_size[class])`
rounded **down to a 32-byte multiple** (`& ~0x1F`). Two facts ground this:

- the class index is `encd_get_ctx_curr_priority_class(ctx)`, asserted `<= 4`
  (`MAX_DMA_PKT_PRIO_NUM`) — the **same 5 classes** as `DMA_CONFIGS.priority_class<=4`;
- `cc_cce_reduce_prio_pkt_size` is a 5-entry per-class cap table in `nrt_gconf()`.

The 32-B floor is the SDMA descriptor alignment for the in-flight compute path; the cap
is **per priority class** — a higher-priority class may be allotted a different
per-packet byte budget, bounding how much bandwidth one reduce packet consumes before
the arbiter re-evaluates. Element/packet-count limits compound it
(`aws_hal_get_sdma_max_cce_elements`: arch 2 → 1024; arch 3/4 → 2048).

> **NOTE — the prio_cap is the *size* complement of the §7/§8 QoS model.** §7/§8 decide
> **which** queue is served next (`AXI_qos` + DWRR `q_qos`/`weight`); the prio_cap
> decides **how big** each CCE reduce packet on that queue may be, per class. Together
> they shape a collective reduce's bandwidth share. **[the `min()`/`& ~0x1F`/per-class
> cap-table read HIGH/OBSERVED — `libnrt.so` decompile this session; the "which + how-
> much" joint reading INFERRED-HIGH.]**

---

## 10. Custom-op DMA (CUSTOM_OP rel index 16) vs compute / collective DMA

### 10.1 The custom-op queue-bundle detection — byte-exact

A DMA is identified as a custom-op DMA by `dma_is_custom_op_dma_v2(eng_id, queue_id)`
(`libnrt.so` `@0x22e120`), decompiled verbatim this session:

```c
// libnrt.so @0x22e120
bool dma_is_custom_op_dma_v2(uint32_t eng_id, uint32_t queue_id)
{
    encd_rel_queue_idx_table_t *v2 = (encd_rel_queue_idx_table_t *)v2_queue_bundle_alloc_table;
    do {
        if ( v2->queue_id == 16                  // the CUSTOM_OP marker slot
          && v2->rel_queue_idx == queue_id       // this queue's rel index matches
          && eng_id >= v2[1].queue_id            // engine-id range lo  (the PAIRED entry)
          && eng_id <  v2[1].rel_queue_idx )     // engine-id range hi
            return 1;
        v2 += 2;                                 // step by TWO entries (marker-pair, range-pair)
    } while ( v2 != mariana_queue_idx );         // sentinel = end of table
    return 0;
}
```

So the table `v2_queue_bundle_alloc_table` is a flat array of
`encd_rel_queue_idx_table_t {uint32_t queue_id; uint32_t rel_queue_idx;}` (8 B each),
walked in **pairs**: `entry[0]` is the `(marker=16, rel_index)` pair, `entry[1]` is the
`(eng_lo, eng_hi)` range pair; the walk stops at `mariana_queue_idx`. The literal **16**
is the CUSTOM_OP rel-index marker (matching `KBIN_DMA_RING_TYPE_CUSTOM_OP(16)`, §1.2),
**not a hardware queue number**. **[HIGH/OBSERVED — `libnrt.so` decompile this session;
the `==16` marker + the pair-walk + the sentinel byte-exact.]**

### 10.2 The queue-bundle routing record

Each queue's routing is a `dma_queue_info_t` (360 B) carrying `{pcore, qid,
dma_engine_idx, dma_engine_offset, eng_type, s2m_prefetch, name, sem_id,
pseudo_serialization_sem_id, vring_t* vring (+320), completion,
static_ring_info_{rx,tx}, dma_queue_bundle_t* queue_bundle}`.
`dma_ring_setup_queue_bundles` (`libnrt.so` `@0x22e630`) iterates the bundle instances
(stride 6096) and the per-instance queues (stride 360), counting template rings
re-materialized per inference into `dynamic_ring_count`. The `queue_bundle` is the
per-queue routing record `dma_is_custom_op_dma_v2` keys on. **[CARRIED/HIGH — host
runtime; the two symbol addresses byte-verified this session.]**

### 10.3 How custom-op DMA is scheduled vs compute / collective

The custom-op DMA does **not** get a separate arbiter — it rides the **same** 16-queue
M2S/S2M engine and the **same** three arbiters (§8). The distinctions are:

1. **ROUTING.** Custom-op DMA is routed to the queue bundle whose CUSTOM_OP rel index
   is `16` (`dma_is_custom_op_dma_v2`), within the Pool/Q7 engine's engine-id range.
   Compute DMA (instruction-fetch, model-exec) and collective DMA (the CCE reduce path)
   use other `kbin_dma_ring_type` rings on their own queues.
2. **PRIORITY.** Each stream carries its own `DMAQoSClass` → `priority_class` →
   per-queue `AXI_qos`/`q_qos`/`weight` (§7). The host priority-class map
   (`dram_base + 0x18`, §7.2) supplies the per-class bytes-per-packet; the DGE mailbox
   (`dram_base + 0x28`, §7.2 QUIRK) can per-priority AND-mask which of the 16 DMA
   channels a stream may use. A higher class is served first by the prefetch/DWRR/
   completion arbiters **when QoS mode is armed** (§8.6).
3. **PACKET SIZE.** A collective-reduce (CCE) DMA is additionally bounded by the
   prio_cap (§9, `cc_cce_reduce_prio_pkt_size[class]`); a plain custom-op COPY DMA is
   not — its size is the reshape `#DMA`/chunk decision (§3). So **collective DMA is the
   one path whose per-packet byte budget is class-capped.**
4. **COMPLETION.** Custom-op staging copies poll the BD gen-tag inline; the DGE/execute
   path uses the completion ring + a `DGE_METADATA_NOTIFICATION` (`dma_map:16` = the 16
   M2S queues) → SW notification queue; collective DMA uses the `EVT_SEM` two-semaphore,
   with a `dma_group_id:1` bit marking a collective group.

> **NOTE — the arbiter never special-cases "queue 16".** Custom-op, compute, and
> collective DMA are **peers** on the 16-queue arbiter, separated by (a) bundle routing
> and (b) `DMAQoSClass`. `16` is the CUSTOM_OP *ring type / rel-index marker*, not a
> physical queue number; the physical queue is the `rel_queue_idx` the bundle maps it
> to within the engine's 16. **[routing + notification HIGH/OBSERVED (host runtime); the
> "peers on one arbiter, separated by routing + class" reading INFERRED-HIGH from the
> shared 16-queue model + the per-stream `DMAQoSClass`.]**

---

## 11. The end-to-end schedule picture

```
  bir::InstDMA{ DMAQoSClass = Pk }  --compile-->  priority_class[2:0] = (k saturated 0..4)
       |                                              |
       |  host: nrtucode_core_dge_set_priority_class_map → dram_base+0x18 (u32[≤4], bytes/pkt)
       |        nrtucode_core_get_dge_mailbox_addr        → dram_base+0x28 (4× {prio, 16b mask})
       v                                              v
  kbin → ring (CUSTOM_OP=16 / compute / collective)   DMA_CONFIGS.priority_class (+12/+13/+61)
       |                                              |
       v                                              v
  device DGE builds 16-B BDs (Part 1) into the queue's M2S(TX)/S2M(RX) ring; the QoS class
  programs M2S_Q.cfg.AXI_qos[30:28] (0x1020) + dwrr_cfg_2.q_qos (0x1084) + dwrr_cfg_3.weight
  (0x1088) + the HP-candidate bit (AXI_M2S_MLA …high_priority.enable, 0x08)
       |
       v
  TDRTP_inc (read/M2S) / RDRTP_inc (write/S2M) doorbell → the engine's 16-queue arbiters:
       prefetch   : pref_force_rr ? RR : QoS(AXI_qos)          [rst RR  ; M2S_rd/S2M_rd]
       DWRR sched : en_dwrr ? DWRR(q_qos,weight) : RR          [rst RR  ; M2S only]
       completion : force_rr ? RR : QoS, + q_promotion         [rst QoS ; M2S_comp/S2M_comp]
       rate limit : two-level token bucket                     [rst masked ; M2S only]
       |
       v   (collective / CCE reduce only:)
  packet size = min(dtype_size*N, cc_cce_reduce_prio_pkt_size[class]) & ~0x1F   ← prio_cap
       |
       v
  DMA engine executes the BD stream → RDM tail write-back + 2-bit ringId; completion via
  gen-tag poll (custom-op inline) / completion ring + DGE_METADATA (dma_map:16) / EVT_SEM
  two-semaphore (collective).
```

---

## 12. Confidence & gaps

- **HIGH / OBSERVED (this session):** the device descriptor fields — `DMA_CONFIGS`
  `priority_class:3` + `is_valid_dma_configs: priority_class<=4` (header `:713–716`,
  `:2070–2071`); `dma_configs` at +12 (DIRECT2D) / +13 (GATHER_XPOSE) / +61
  (INDIRECT1D); the 4-deep `NEURON_ISA_TPB_NUM_DGE_SHAPE_REGISTERS=4U` (`:34`); the
  `struct2opcode` map (four DGE Kinds incl. `DIRECT2D_XPOSE`); the `gather_xpose` 2-B /
  16×128 / UINT32-multiple-of-16 / 32-B-aligned constraints; `MEMCOPY_CARVEOUT_CFG`=39
  "byte offset / 16" 64-B-aligned + `MEMCOPY_DMA_CFG`=40 {DMAs, queues}. The host
  `nrtucode_core_dge_set_priority_class_map` `@0x3094e0` (`+0x18`, `n≤4`, bytes-per-
  packet) + the DGE mailbox `@0x9b11e0` (`+0x28`, per-priority 16-bit AND-mask). The
  Cayman UDMA M2S/S2M schema — every block base (`M2S_rd 0x300`, `M2S_dwrr 0x340`,
  `M2S_rate_limiter 0x380`, `M2S_stream_rate_limiter 0x3c0`, `M2S_comp 0x400`, `M2S_Q
  0x1000`), `M2S_Q ArraySize=16`, window `0x20000`/`0x18000`, `en_dwrr[0]` rst 0,
  `inc_factor[19:16]=7`, `weight_inc[9:8]=2`, `pref_force_rr[16]` rst 1, `force_rr[25]`
  rst 0 (M2S) / bit 16 (S2M), `q_promotion[24]` rst 1, `cfg_coal.timer=0x186A0`,
  `AXI_qos[30:28]`, `q_qos@0x1084`, `weight@0x1088`, the masked rate limiters, the
  S2M no-DWRR/no-rate-limiter. The host-runtime `dma_is_custom_op_dma_v2` `@0x22e120`
  (`==16` pair-walk) + `dma_ring_setup_queue_bundles` `@0x22e630` +
  `encd_get_cce_reduce_packet_size` `@0x2396f0` (`min()` + `& ~0x1F` + per-class cap).
  The DGE format strings (setup/reshape/push/select) in `libnrtucode_internal.so`.
- **MED:** the DIMPUSH src/dst pairing (the two-pair log shape + symmetric struct
  arrays); the exact (1)–(3) emit issue order, REGWRITE's WORD1 control optype, and the
  RTL/software selector fall-through (all FLIX-desynced bodies); the per-stream
  "peers on one 16-queue arbiter, separated by routing + class" reading; the joint
  prio_cap + arbiter "which + how-much" interpretation; the Cayman-vs-Maverick per-queue
  offset stability (offsets are Cayman; the within-engine ring stride is arch-stable).
- **CARRIED:** the `bir::InstDMA{DGEType, DMAQoSClass}` enum values + the kbin
  `dma_desc`/`mem_ref` ring lowering (host neuronx-cc, not in the GPSIMD ISA headers);
  the 16-B BD internal layout (no `SDMA_CME_BD_DESC` struct in this distribution, §5.4);
  the `dma_queue_info_t` 360-B routing record + the custom-op symbols (host-runtime
  `libnrt.so`, a separate package).
- **LOW / flagged:** any MAVERICK (v5) runtime backend behaviour (no v5 NX-POOL firmware
  ships; the ISA surface persists in the v5 headers, but runtime behaviour is not
  derivable — every v5-interior claim, incl. HWDGE on v5, is INFERRED); the absolute
  ordering semantics if a `priority_class>4` ever reached the wire (the `<=4` gate
  rejects it, so the device never sees more than 5 classes).

---

## Cross-references

- [DGE Setup + Context Init](../firmware/dge/dge-setup.md) — the `(tx,rx)` context pair,
  the `sw=%d`/`engine`/`queue_idx`/`queue_num` binding, the `rx.base`/`tx.base` rings,
  and the tail-pointer doorbell this page's Part 1 fires.
- [DGE 3-Backend Selector](../firmware/dge/dge-backend-selector.md) — the Pool/RTL/
  software pick (§4), the full `DIRECT2D` 64-B field table + `compute_op` enum, the
  three backend dump lines, and the RDM ring/tail-writeback/ringId schema.
- [DGE Descriptor-Emit Path](../firmware/dge/dge-emit.md) — the GENERATE/DIMPUSH/REGWRITE
  micro-ops (§5), the per-descriptor bounds check, and the `MEMCOPY_CARVEOUT_CFG` 16-B
  unit.
- [The DGE Host-Private API](../runtime/dge-host-api.md) — the host `+0x18` priority
  (bytes-per-packet) table, the `+0x28` DGE mailbox (per-priority DMA-channel mask), and
  the NX-POOL kind / boot-state gates (§7).
- [UDMA M2S CSRs](../control/csr/udma-m2s.md) — the per-queue M2S register window the
  QoS class programs and the three arbiter blocks (§8).
- [UDMA S2M CSRs](../control/csr/udma-s2m.md) — the RX-side QoS-without-shaping window
  (§8.5).
- [The DMA / Descriptor / Memory Subsystem](descriptor-model.md) — the descriptor-model
  overview this page's builder feeds.
- [DGE Micro-Op Encoding (byte-level)](dge-microop-encoding.md) — the bit-exact 16-B BD
  WORD0/WORD1 packing the GENERATE/DIMPUSH/REGWRITE pushes lower into (the forward
  resolution of §5.4's CARRIED BD layout).
- [The Confidence & Walls Model](../reference/confidence-model.md) — the `[CONF/PROV]`
  tag definitions and the FLIX-desync MED ceiling cited throughout.

---

## Reproduction

```sh
# --- Cayman UDMA CSR schema (the byte-exact QoS/arbiter ground truth) ---
M2S=extracted/nested/cayman-arch-regs_tgz/csrs/sdma/udma_m2s.json   # NOT under arch-headers/cayman/
S2M=extracted/nested/cayman-arch-regs_tgz/csrs/sdma/udma_s2m.json
python3 -c "import json;d=json.load(open('$M2S'))['RegFile']; \
  print(d['SizeInBytes']); print([(b['Name'],b['AddressOffset'],b['ArraySize']) for b in d['RegistersBundleArrays']])"
#   131072 ; M2S_rd 0x300, M2S_dwrr 0x340, M2S_rate_limiter 0x380, M2S_stream_rate_limiter 0x3c0,
#            M2S_comp 0x400, M2S_Q 0x1000 (ArraySize 16) ; S2M SizeInBytes 98304, no *_dwrr/rate_limiter

# --- device descriptor fields (compile-checked headers) ---
INC=.../neuron_cayman_arch_isa/tpb
rg -n 'priority_class|is_valid_dma_configs' $INC/aws_neuron_isa_tpb_common.h        # :713-716, :2070-2071
rg -n 'dma_configs' $INC/aws_neuron_isa_tpb_dma_{direct2d,gather_xpose,indirect1d}.h # +12 / +13 / +61
rg -n 'NUM_DGE_SHAPE_REGISTERS' $INC/aws_neuron_isa_tpb_common.h                     # :34 = 4U  (selector CORRECTION)
python3 -c "import json;d=json.load(open('.../instruction_mapping.json'));print(d['struct2opcode']['NEURON_ISA_TPB_DMA_DIRECT2D_XPOSE_STRUCT'])"  # ['OPCODE_DMA_TRANSPOSE'] — 4th DGE Kind

# --- host runtime symbols (libnrt.so — aws-neuronx-runtime-lib, a SEPARATE package) ---
# (IDA sidecar decompiles; addresses byte-verified)
#   dma_is_custom_op_dma_v2          @0x22e120  (==16 pair-walk, sentinel mariana_queue_idx)
#   dma_ring_setup_queue_bundles     @0x22e630
#   encd_get_cce_reduce_packet_size  @0x2396f0  (min(dtype*N, cc_cce_reduce_prio_pkt_size[cls]) & ~0x1F)
# --- host config API (libnrtucode.so customop export) ---
#   nrtucode_core_dge_set_priority_class_map @0x3094e0  (+0x18, n<=4, bytes-per-packet)
#   nrtucode_core_get_dge_mailbox_addr       @0x9b11e0  (+0x28, 4× {prio:u8, mask:u16})
```
