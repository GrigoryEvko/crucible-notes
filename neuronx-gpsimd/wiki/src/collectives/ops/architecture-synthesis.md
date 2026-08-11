# The Unified Collective-Communication Architecture

> **What this page is.** The **capstone** of the collectives chapter — a
> navigation + mental-model layer that ties the thirteen concrete op pages into
> **one end-to-end stack**: *compiler pseudo-op → host NRT lowering
> (SELECT → COMPOSE → EMIT) → TOP_SP on-device sequencer → NCFW ring/mesh/hier
> algorithm → SB2SB/SDMA data plane → EVT_SEM barrier/semaphore completion.* It
> does **not** re-derive any sibling page; its value is the **spine diagram**, the
> **per-boundary handoff table**, the cross-links into every Part-10 page, and the
> **open-items ledger**. Each consolidated fact cites its **source page**; the
> spine HIGH facts additionally carry an **addr/offset/symbol** confirmed
> against the shipped binaries (§0.2).

> **Provenance.** Every structural fact below is established on a committed sibling
> page (cited inline). The spine addresses/opcodes/enums are confirmed
> directly against the shipped artifacts — the cayman ISA headers
> (`aws-neuronx-gpsimd-customop-lib 0.21.2.0`), the host runtime
> `libnrt.so.2.31.24.0` (BuildID `8bb57aba…102e`, x86-64, DWARF, **not stripped**;
> `.text`/`.rodata`/`.data` all `VMA == file-offset`, **no delta**), the NCFW JSON
> pretty-printer `libncfw.so`, and the device ucode `libnrtucode_extisa.so`.
> All output reads as derived from binary/static analysis only. Confidence is
> tagged `HIGH/MED/LOW` × `OBSERVED` (read from bytes/disasm/header/DWARF) /
> `INFERRED` (architectural reading over OBSERVED facts) / `CARRIED`
> (consolidated from a sibling page at *its* stated confidence — never inflated).

---

## 0 · The architecture in one paragraph + the spine spot-checks

### 0.1 · The stack in one paragraph

The GPSIMD/NeuronCore collective stack is a **six-layer pipeline**, with a named
artifact handed across each boundary. **(1)** The **compiler** emits a 64-byte
**pseudo** instruction targeted at the `TOP_SP` engine (`engine 5`) — opcode upper
three bits `0b110`: `TRIGGER_COLLECTIVE 0xC8`, `TRIGGER_ALL_REDUCE 0xC7`,
`TRIGGER_COLLECTIVE2`+`_ext 0xD9`/`0xDA`, `SENDRECV 0xCB`, the barrier trio
`0xD8`/`0xD5`/`0xC3`, `CUR_PROCESSING_RANK_ID 0xDB` — which **never executes on
hardware**. **(2)** The **host NRT runtime** (`libnrt.so`) **decodes + lowers** it
at model load: it re-encodes the ISA `ctype` to an `enc_op_type`, the reduce
`ALU_OP` to an `SDMA_CCETYPE`, the `dtype` verbatim to `SDMA_DTYPE`, **SELECTs** an
`enc_alg_type` per leg (`__select_algorithms @0x14c620` probing
`enc_can_post_*_operation`), **COMPOSEs** the SDMA step primitives, and **EMITs** a
SPAD `cc_op` program (`create_spad_ctrl_entry @0x232cd0`) + DMA descriptor packets
(`add_dma_packet_cce @0x2307d0`) that it DMA-loads onto the device. **(3)** The
**TOP_SP** — an embedded Xtensa-NX core (`engine 5`, one per NeuronCore) — is the
**on-device sequencer**: kicked once by a value-`1` write to the `host_trigger` CSR
(`LOCAL_REG + 0x15a0 = 0x615a0` cayman/mariana, `0x60848` sunda) through the
`kaena_khal` vtable slot **`+0x708`**, it **walks** the `cc_op` program, per step
incrementing `EVT_SEM` semaphores (the `SEMAPHORE_INC` window at `sp_base + 0x1800`,
the same window the DMA legs bump) and posting completion as the elected leader.
**(4)** The **NCFW** management firmware's per-TOP_SP context (rooted at the
`ncfw_ctx_top_sp` key) **is** that program; the 4-bit `algo_type` nibble of each
`cc_op` entry routes it to the resident **ring**, **mesh**, or **hierarchical**
algorithm. **(5)** The **data plane** is the SB2SB collective kernel
(`S3D3_COLLECTIVE 0xBF`, real HW on the POOL/Q7 iDMA): per leg it decodes →
cross-die pre-syncs → maps the remote window → `rdma_desc_gen` (op 8) →
`rdma_desc_start` (op 9, the TX/RX tail-pointer doorbell), moving
SBUF → remote-SBUF and, on reducing legs, folding via the **SDMA CCE** descriptor.
**(6)** **Completion/sync** is the `EVT_SEM` 256-semaphore array
(arrive `INC +0x1800` / wait `READ-GE +0x1000` / reset `DEC +0x1C00`): the per-step
peer-semaphore handshake sequences steps **within** a phase, the NCFW 4-step counted
barrier **brackets** each phase, and the TOP_SP `timestamp_inc` tick keeps the dies'
clocks aligned (tsync). Cross-die addressing rides the 58-bit SoC bitfield over the
`io_d2d` fabric. The transport is **bespoke** (AL/UDMA `hw_exec_queue` + `EVT_SEM`/NQ)
— there is **no Cadence XRP** ([§XRP](xrp-host-dsp-messaging.md)). The one standing
boundary: the host→device-core handoff crosses the **un-disassemblable Xtensa-LX
NCFW core**, so the on-core schedule itself is the MED/LOW frontier (§9).
`[synthesis HIGH/CITED across all anchors; the on-NCFW-core schedule is MED — §9, O-1.]`

### 0.2 · The spine spot-checks (confirmed against the binaries)

These are the facts the whole synthesis hangs on — read directly, not paraphrased:

| # | Spine fact | Where verified | Tag |
|---|---|---|---|
| 1 | **Collective opcodes** `SB2SB=0xbf`@262, `DMABARRIER=0xc3`@265, `ALL_REDUCE=0xc7`@269, `COLLECTIVE=0xc8`@270, `SEND_RECV=0xcb`@273, `SYNC_BARRIER=0xd5`@283, `CORE_BARRIER=0xd8`@286, `COLLECTIVE2=0xd9`@287, `EXTENSION=0xda`@288, `RANK_ID=0xdb`@289, `GID_LOAD=0xdc`@290 | cayman `aws_neuron_isa_tpb_common.h` | HIGH / OBSERVED |
| 2 | **`host_trigger` CSR** `aws_reg_cayman_get_top_sp_nx_local_reg_host_trigger_offset @0x47b080 = mov $0x615a0,%eax; ret`; `set_host_trigger_cayman @0x471b40` → `get_xt_local_reg_offset(4,0)` then `lea 0x15a0(%rax); mov $1,%esi` (write 1 to `0x615a0`) | `libnrt.so` disasm | HIGH / OBSERVED |
| 3 | **vtable split** `set_host_trigger @0x457b30` → `mov 0x708(%rax),%rax ; jmp *%rax` (**write**); `get_host_trigger_reg_offset @0x457ba0` → `mov 0x710(%rax),%rax` (**getter**). Use **`+0x708`** for the write | `libnrt.so` disasm | HIGH / OBSERVED |
| 4 | **reduce-op enums** `CCE_OP {ADD0,MAX2,MIN3}`@1003 ("Same encoding as SDMA CCE op"); `DGE_COMPUTE_OP {NONE0,ADD1,MULTIPLY2,MAX3,MIN4}`@838; `LNC_SIZE_FMT {LNC1=0,LNC2=1,LNC4=2,LNC8=3}`@816; `COLLECTIVE_TYPE 0x0..0x9`@793 | cayman `common.h` | HIGH / OBSERVED |
| 5 | **`enc_alg_type` 11-enum** DWARF DIE `@61ec6…`: `RING0/HIER1/MESH2/KANGARING3/SINGLE_CYCLE_RING4/INTRA_RDH5/SINGLE_STEP_MESH6/INTER_RDH7/TWO_STEP_POD_MESH8/LATENCY_OPT_MESH9/BW_OPT_MESH10` | `libnrt.so` `--dwarf=info` | HIGH / OBSERVED |
| 6 | **NRT lowering symbols** `_ZN18enc_hier_primitive19__select_algorithmsEv @0x14c620`; `add_dma_packet_cce @0x2307d0`; `create_spad_ctrl_entry @0x232cd0`; the `enc_can_post_{mesh@0xfb4d0,single_cycle_ring@0xfaa80,inter_rdh@0xfbf80,intra_rdh@0xfbb00,kangaring@0xfbe70,hierarchical@0xfc130}` family | `libnrt.so` `nm` | HIGH / OBSERVED |
| 7 | **`SDMA_CCETYPE`** DWARF DIE `@337d0…`: `ADD0/FMA1/MAX2/MIN3/EXT4/GCE5` | `libnrt.so` `--dwarf=info` | HIGH / OBSERVED |
| 8 | **`EVT_SEM` INC window** `cayman_get_sp_sema_i_ofst @0x25c2e0`: `shl $0x1e,%rdx` (instance) ; `shl $0x2f,%rax` (die bit 47) ; `lea 0x1800(%rax,%rbx,4),%rax` | `libnrt.so` disasm | HIGH / OBSERVED |
| 9 | **device + firmware rooting** `decode_sb2sb_collective` + `SB2SB_Collective : total_src_nelem…` in `libnrtucode_extisa.so`; `ncfw_ctx_top_sp` + `"run_state"` in `libncfw.so` | `extisa`/`libncfw` strings | HIGH / OBSERVED |

⇒ Every opcode / address / enum the synthesis depends on is grounded; the layers
*below* the binary surface (the on-NCFW-core schedule) are the consolidated MED/LOW
findings of the source pages, carried forward at their stated confidence.

---

## 1 · The layered stack — the handoff at each boundary

The collective is a **vertical pipeline**; at each boundary the producer hands the
consumer a **named artifact**. This is the integrating spine — read top-to-bottom.

### 1.1 · The spine diagram

```text
  ┌────────────────────────────────────────────────────────────────────────────┐
  │ L1  COMPILER (Neuron/XLA backend)                                            │
  │   emits a 64-B PSEUDO word, opcode upper-3-bits 0b110, targeted at TOP_SP #5 │
  │   0xC7 / 0xC8 / 0xD9+0xDA / 0xCB / 0xD8 / 0xD5 / 0xC3 / 0xDB   (never on HW)  │
  └──────── NEFF instr stream + def.json replica_groups/dma_queues ─────────────┘
                                   │
  ┌────────────────────────────────▼───────────────────────────────────────────┐
  │ L2  HOST NRT  (libnrt.so, nrt_collectives.cpp)   — at MODEL LOAD             │
  │   DECODE ctype/op/dtype  →  re-encode enc_op_type / SDMA_CCETYPE / SDMA_DTYPE│
  │   SELECT  __select_algorithms @0x14c620  → enc_can_post_*  → enc_alg_type    │
  │   COMPOSE recv_reduce_* / direct_recv_*  (per-leg SDMA step primitives)      │
  │   EMIT    create_spad_ctrl_entry @0x232cd0 (cc_op table) +                   │
  │           add_dma_packet_cce @0x2307d0 (DMA desc packets)                    │
  └──── (a) SPAD cc_op program (HBM → DMA) ── (b) host_trigger doorbell 0x615a0 ─┘
                                   │  vtable +0x708 (set_host_trigger)
  ┌────────────────────────────────▼───────────────────────────────────────────┐
  │ L3  TOP_SP on-device SEQUENCER  (Xtensa-NX, engine 5, per NeuronCore)        │
  │   kicked once → WALKS the cc_op program; per step issues DMA tail-ptr INCs   │
  │   into EVT_SEM SEMAPHORE_INC (sp_base + 0x1800 + idx*4) and EVT_SEM wait/dec │
  └──────── the NCFW per-TOP_SP context IS this program (algo_type nibble) ──────┘
                                   │
  ┌────────────────────────────────▼───────────────────────────────────────────┐
  │ L4  NCFW ALGORITHM  (libncfw ctx + Xtensa-LX firmware images)               │
  │   cc_op byte0[0:3]=algo_type → dispatch (DRAM+0xB0 12-entry jump table)      │
  │   route → RING (NCFW-04) | MESH (NCFW-05) | HIER (NCFW-06);  emit SB2SB legs │
  └──────── per channel: next/prev neigh + recv/send/post/dma_compl sema addrs ──┘
                                   │
  ┌────────────────────────────────▼───────────────────────────────────────────┐
  │ L5  DATA PLANE  (SB2SB collective kernel, 0xBF S3D3_COLLECTIVE, POOL/Q7 iDMA)│
  │   decode → cross-die PRE-SYNC → program_window → rdma_desc_gen (op8) →       │
  │   rdma_desc_start (op9): TX writes M2S TDRTP_inc / RX writes S2M RDRTP_inc;  │
  │   reduce legs FOLD via the SDMA CCE descriptor                              │
  └──────── local_sem (src release) + peer recv_sema (dst ready) ───────────────┘
                                   │
  ┌────────────────────────────────▼───────────────────────────────────────────┐
  │ L6  COMPLETION / BARRIER / SEMAPHORE  (EVT_SEM 256-array; NCFW barrier; tsync)│
  │   step↔step  = per-peer kring/ring/mesh handshake  (INC +0x1800 / GE +0x1000)│
  │   phase bracket = NCFW 4-step counted barrier (≤4 leaders, steps {1,4,4,1})  │
  │   clock align  = TOP_SP timestamp_inc tick → dev tsync                       │
  │   to host      = reserved-sema INC and/or NQ entry (stride 0xa0)             │
  └──────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 · The per-boundary handoff table

| Layer | Engine | Consumes | Produces / hands off | Anchor page | Tag |
|---|---|---|---|---|---|
| **L1 compiler** | host/XLA | the BIR collective op | a 64-B pseudo word (`0b110` opcode) + NEFF `replica_groups`/`dma_queues` | [trigger-collective](trigger-collective.md), [rank-id](rank-id.md) | HIGH / CARRIED |
| **L2 host NRT** | x86-64 `libnrt.so` | the pseudo word (v2 buffers `0xD9` then combines `0xDA`) | (a) SPAD `cc_op` program (HBM → DMA), (b) `host_trigger` doorbell | [all-reduce](all-reduce.md), [trigger-collective2-ext](trigger-collective2-ext.md), [top-sp-lowering](top-sp-lowering.md) | HIGH / OBSERVED |
| **L3 TOP_SP** | Xtensa-NX, `engine 5` | the `cc_op` program | per-step DMA-tail INC into `EVT_SEM +0x1800`; the program IS the NCFW per-TOP_SP context | [top-sp-lowering](top-sp-lowering.md) | HIGH edges / MED on-core step (§9) |
| **L4 NCFW** | scalar Xtensa-LX | one `cc_op` entry | per-channel: `next/prev_neigh` + recv/send/post/dma_compl sema SoC addrs; one SB2SB leg/step | [main-dispatch-loop](../ncfw/main-dispatch-loop.md), [ring-kangaring](../ncfw/ring-kangaring.md), [mesh](../ncfw/mesh-collective.md), [hier](../ncfw/hierarchical-collective.md) | HIGH dispatch / MED schedule (§9) |
| **L5 SB2SB/Q7** | POOL + Q7 iDMA | the 64-B `0xBF` word | bytes SBUF → remote-SBUF; `local_sem` (src release) + peer `recv_sema` (dst ready) | [s3d3-collective](s3d3-collective.md), [sendrecv](sendrecv.md), [cce-in-transfer](../../dma/cce-in-transfer.md) | HIGH control / MED FLIX inner |
| **L6 EVT_SEM** | EVT_SEM unit | INC/wait/dec ops | step ordering (peer handshake) + phase bracket (NCFW barrier) + host completion (sema/NQ) | [core-barrier](core-barrier.md), [sync-barrier](sync-barrier.md), [neff-device-barrier](../ncfw/neff-device-barrier.md), [neff-host-barrier](../ncfw/neff-host-barrier.md) | HIGH / OBSERVED |

> **NOTE — the two-level trigger model.** L2's `host_trigger` doorbell
> ([top-sp-lowering §3a](top-sp-lowering.md)) **starts** the TOP_SP program once per
> executable; L3's per-step DMA-tail INC into `EVT_SEM +0x1800`
> ([top-sp-lowering §3b](top-sp-lowering.md)) is the **per-iteration** hand-off the
> DMA legs use. The program, once started, spins on the same `+0x1800` window the
> DMA legs increment. Two distinct surfaces, one semaphore array.

---

## 2 · The pseudo-op catalog — the full opcode roster

All opcodes spot-verified in cayman `common.h` (§0.2 #1). Each is a
64-byte TPB instruction word; the **pseudo class** = opcode upper-three-bits `0b110`
(`0xC0..0xDF`). `0xBF` (below `0xC0`) and `0xF1` (EXTISA) are **real HW**, not pseudo.
"Triggers algo" = which L4 path the lowering drives.

| op | mnemonic / struct | key fields | role / triggers | page |
|---|---|---|---|---|
| `0xC7` | `PSEUDO_TRIGGER_ALL_REDUCE` | `op@12`, `dtype@13`, in/out `tensor_id`, `num_elements@24` (DRAM handles) | **fixed-form ALL_REDUCE** (no `ctype`/`group_id`/offset) → `ENC_ALLREDUCE` → ring/hier/mesh/single-cycle | [all-reduce](all-reduce.md) |
| `0xC8` | `PSEUDO_TRIGGER_COLLECTIVE` | `op@12`, `dtype@13`, `group_id@14`, `num_elements@24`, `ctype@32`, `src/dst_offset_elems@48/56` | **generic collective** — `ctype` selects the KIND (AR/RS/AG/A2A/PERMUTE) | [trigger-collective](trigger-collective.md) |
| `0xD9` | `PSEUDO_TRIGGER_COLLECTIVE2` (1st of pair) | `op@12`, `dtype@13`, `ctype@14`, `cc_dim@15`, `src0`/`src1` `ADDR8TENSOR2D` (24 B each) | **v2 / SBUF-tensor collective** — 2-D operands directly on SBUF | [trigger-collective2-ext](trigger-collective2-ext.md) |
| `0xDA` | `PSEUDO_EXTENSION` (2nd of pair; shared) | `instr_chaining@1`, `group_id@2`, `channel_id@4`, `stream_id@6`, `dst ADDR8TENSOR2D@8`, `cc_buf_*`, `dma_configs`, `src1_bitmap`, `cross_sb_out` | **v2 ext**: dst + channel/stream/cc-buf/priority + A2Av variable-all-to-all knobs. Also binds `PSEUDO_DMA_EXT` | [trigger-collective2-ext](trigger-collective2-ext.md), [rdma-gather-pseudo-ops](rdma-gather-pseudo-ops.md) |
| `0xCB` | `PSEUDO_SEND_RECV` | `tensor_id@12`, `peer_id (u64)@16`, `is_send@24`, `offset_bytes@48`, `size_bytes@56` | **P2P send/recv** — `is_send=1` → `ENC_SEND(5)` TX leg, `=0` → `ENC_RECV(6)` RX leg; **no reduce**. *A ring step IS a sendrecv* | [sendrecv](sendrecv.md) |
| `0xDB` | `PSEUDO_CUR_PROCESSING_RANK_ID` | `group_id@12`, `channel_id@14`, `iteration_id@16`, `dst_reg@18`, `stream_id@20` | **the rank loop** — resolves the per-iteration PEER rank into `r[dst_reg]` (RT/NCCL fills it). Backs PERMUTE + the ring rank walk | [rank-id](rank-id.md) |
| `0xDC` | `PSEUDO_GID_LOAD` | (rank-identity → register) | loads **this** rank's GID (not a peer); sibling idiom of `0xDB` | [rank-id](rank-id.md) |
| `0xD8` | `PSEUDO_CORE_BARRIER` | `events@4`, `id (u32)@12`, `semaphore (u32)@16` | **cross-pcore / intra-VNC** counted SEMAPHORE barrier; arrive=INC, wait=GE-N, reset=DEC → `EVENT_SEMAPHORE 0xA0` | [core-barrier](core-barrier.md) |
| `0xD5` | `PSEUDO_SYNC_BARRIER` | `events@4` ONLY (`reserved0[52]`) | **within-core all-engine** EVENT (1-bit) barrier → `EVENT_SEMAPHORE 0xA0` over `tpb_events` | [sync-barrier](sync-barrier.md) |
| `0xC3` | `PSEUDO_DMABARRIER` | `events@4`, `barrier_metadata (u32)@12` | SBUF write-ordering **memory fence** (NOT a rendezvous) → `Nop(NUM_ROWS)` timed stall | [dma-barrier](dma-barrier.md) |
| `0xBF` | `SB2SB_COLLECTIVE` / `S3D3_COLLECTIVE` *(real HW, POOL)* | `events@4`, `in_dtype@12`/`out_dtype@13` (in==out enforced), `src_mem_pattern TENSOR3D@16`, `lnc_size_fmt@32`, `num_active_channels@33`, `dst_mem_pattern TENSOR3D@48` | **intra-node iDMA SBUF→SBUF copy/reduce-copy leg** — LNC1 self / LNC2 pair (LNC4/8 rejected). The concrete data-plane leg the pseudos lower TO | [s3d3-collective](s3d3-collective.md) |
| `0xb8`/`0xbb`/`0xbd`/`0xf1` | `DMAMemcpy`/`DmaIndirect`/`DmaTranspose`/`DmaGatherTranspose` *(real HW DGE)*; pseudo `PseudoDmaDirect2d 0xd4`+ext `0xda` | `dge_op` selector @off 15 | **compute-side DGE descriptor kinds** (direct2d / indirect1d / gather_xpose) — NOT the collective gather (that is the `0xBF` SB2SB leg) | [rdma-gather-pseudo-ops](rdma-gather-pseudo-ops.md) |
| `op6`/`op8`/`op9` | `EXTENDED_SBUF_TO_SBUF` / `EXTENDED_RDMA_DESC_GEN` / `EXTENDED_RDMA_DESC_START` *(Q7 ucode transport)* | op8: `local_sem`, `remote_sem`, `remote_core_id`, `remote_routing_id`, `dma_engine_mask`, src/dst addr, `free_dim_bytes`; op9: `events@4` (launch gate) | the SB2SB decode path (op6); build the per-engine SDMA CME BD ring + local/remote sema descriptors (op8); DRAIN + role-split + M2S/S2M tail-ptr doorbell launch (op9) | [s3d3-collective](s3d3-collective.md), [sendrecv](sendrecv.md) |

> **GOTCHA — the `DMABARRIER` token mismatch.** The opcode **enum** constant is the
> no-underscore `OPCODE_PSEUDO_DMABARRIER` (=`0xc3`) but the **struct/JSON-key** are
> the underscore `PSEUDO_DMA_BARRIER`; `instruction_mapping.json`'s opcode TOKEN is a
> dangling stale Tonga-era underscore form. **Same single `0xC3`** (value 195);
> value-based decoders are unaffected, token-resolving tools see a dangling binding.
> CORE/SYNC have no such mismatch. `[CARRIED — dma-barrier §6, HIGH/OBSERVED]`
> See [dma-barrier](dma-barrier.md), [collective-enums](collective-enums.md).

> **NOTE — `ALL_REDUCE` has three carriers, not one.** It is *both* a distinct opcode
> (`0xC7`) *and* a `ctype` value (`0x1`) on `0xC8` and on `0xD9`/`0xDA`. The
> reimplementer must accept all three forms. `[CARRIED — all-reduce §1, HIGH/OBSERVED]`

---

## 3 · The algorithm family — `enc_alg_type`, selection, reduce carriers

### 3.1 · The `enc_alg_type` selector (11 values, spot-verified §0.2 #5)

`enc_alg_type` is exactly the **low nibble of the firmware `cc_op` `algo_type`**
field. The 11 values, the family they belong to, and the L4 algorithm page each
routes to:

| # | `enc_alg_type` | family | L4 algorithm page |
|---|---|---|---|
| 0 | `ENC_ALG_RING` | ring | [ring-kangaring](../ncfw/ring-kangaring.md) |
| 1 | `ENC_ALG_HIER` | hier | [hierarchical-collective](../ncfw/hierarchical-collective.md) |
| 2 | `ENC_ALG_MESH` | mesh | [mesh-collective](../ncfw/mesh-collective.md) |
| 3 | `ENC_ALG_KANGARING` | ring (N-read/1-write) | [ring-kangaring](../ncfw/ring-kangaring.md) |
| 4 | `ENC_ALG_SINGLE_CYCLE_RING` | ring (all-reduce-only fused RS+AG) | [ring-kangaring](../ncfw/ring-kangaring.md) |
| 5 | `ENC_ALG_INTRA_RDH` | hier / reduce-during-handshake | [hierarchical-collective](../ncfw/hierarchical-collective.md) |
| 6 | `ENC_ALG_SINGLE_STEP_MESH` | mesh | [mesh-collective](../ncfw/mesh-collective.md) |
| 7 | `ENC_ALG_INTER_RDH` | hier / reduce-during-handshake | [hierarchical-collective](../ncfw/hierarchical-collective.md) |
| 8 | `ENC_ALG_TWO_STEP_POD_MESH` | mesh | [mesh-collective](../ncfw/mesh-collective.md) |
| 9 | `ENC_ALG_LATENCY_OPT_MESH` | mesh | [mesh-collective](../ncfw/mesh-collective.md) |
| 10 | `ENC_ALG_BW_OPT_MESH` | mesh | [mesh-collective](../ncfw/mesh-collective.md) |

`enc_alg_mesh_type` (= `cc_op` `algo_sub_type`, 3-bit): `FULL_MESH0` / `GROUPED_MESH1`
/ `MESH_TRN2 2` / `MESH_SWITCH3` / `MESH_INVALID4`.
`[values HIGH/OBSERVED §0.2; the host↔firmware identity is MED/INFERRED — §8 C-7.]`
Full enum reconciliation: [collective-enums](collective-enums.md).

### 3.2 · When each is selected — `__select_algorithms @0x14c620`

`__select_algorithms` (spot-verified §0.2 #6) assigns **one `enc_alg_type` per leg** by
probing the `enc_can_post_*_operation` predicates. A hierarchical all-reduce carries
**up to five** per-leg choices. The legality matrix (from the `libnrt` `.rodata`
assertion strings):

| leg (op × scope) | legal `enc_alg_type` set | page |
|---|---|---|
| intra all-gather | `RING` \| `KANGARING` | [all-reduce](all-reduce.md), [collective-enums](collective-enums.md) |
| intra reduce-scatter | `RING` \| `KANGARING` | [all-reduce](all-reduce.md) |
| inter all-gather | `RING` \| `INTER_RDH` (\| `MESH` \| metaring) | [all-reduce](all-reduce.md) |
| inter all-reduce | `RING` \| `INTER_RDH` \| `SINGLE_CYCLE_RING` (\| `MESH` \| metaring) | [all-reduce](all-reduce.md) |
| inter reduce-scatter | `RING` \| `INTER_RDH` (\| `MESH` \| metaring) | [all-reduce](all-reduce.md) |

**Gates:** `SINGLE_CYCLE_RING` is **ALL-REDUCE-only** (`op_type == ENC_ALLREDUCE`
guard, in `enc_can_post_single_cycle_ring @0xfaa80`); `KANGARING` is **intra-only**
(`enc_can_post_kangaring @0xfbe70` rejects inter comms). The exact numeric algo a given
`(op, scope, world-size, dtype, topology)` resolves to depends on the
`enc_can_post_*` **thresholds**, which are not enumerated. `[HIGH/OBSERVED gates;
threshold→numeric is MED — §9 O-3.]`

### 3.3 · The algorithm decompositions

- **RING all-reduce** = reduce-scatter (N-1 `recv_reduce_*` steps, CCE-fold) +
  all-gather (N-1 `direct_recv`/`send` copy steps), over directed channel rings;
  flow control = `ring_ctx` `recv_cnt`/`send_credit`/`run_state`.
  [ring-kangaring](../ncfw/ring-kangaring.md), [all-reduce](all-reduce.md).
- **KANGARING** = the same ring tape but `reduction_type_t = KANGARING_NR1W(2)`: the
  primary rank reads-and-reduces from a vector of N peers (`kring_peer_semas`) and
  writes to 1 next; latency-opt for **intra**.
  [ring-kangaring](../ncfw/ring-kangaring.md).
- **MESH all-reduce** = a flat event tape (50 events SUNDA / 108 the rest); each event =
  *wait `event_wait_sema ≥ wait_val`* → *fire ≤2 `dma_apb_bcast`* → *INC ≤3
  `direct_trigger_sema` at neighbour dies*; the SoC `CAYMAN_ID`/`DIE` bits route the
  d2d hop. [mesh-collective](../ncfw/mesh-collective.md).
- **HIERARCHICAL all-reduce** = intra reduce-scatter (RING) → inter all-reduce
  (MESH/RING/RDH) → intra all-gather (RING), sequenced by a single `run_state` byte;
  the hier config is an 8-byte handle to a firmware-resident level table — it **reuses**
  the co-resident ring+mesh descriptors, does not embed them.
  [hierarchical-collective](../ncfw/hierarchical-collective.md).
- **SENDRECV** = the irreducible building block: a single directed SB2SB leg, no
  chaining, no fold. `enc_metaring_primitive::__compose_channel_ring` calls
  `__compose_p2p_channel` — *a ring step is literally a sendrecv*. [sendrecv](sendrecv.md).

> **GOTCHA — `pring` is not a value.** There is **no `PRING` `enc_alg_type`** (the
> 11-enum has none). The `enc_metaring` "metaring" family + `__compose_pipeline_allreduce`
> are the nearest landed artifacts; a dedicated pring decode is OPEN (§9 O-2). The
> [pring-descriptors](../ncfw/pring-descriptors.md) page holds what is known.

### 3.4 · The reduce-op + dtype carriers

- **reduce op** rides ISA `op(ALU_OP)@12` → `SDMA_CCETYPE {ADD0,FMA1,MAX2,MIN3,EXT4,GCE5}`
  (== ISA `CCE_OP {ADD0,MAX2,MIN3}` byte-superset; spot-verified §0.2 #4/#7). It is
  realized in the **SDMA CCE descriptor** (`add_dma_packet_cce @0x2307d0`) — **not** in
  the `0xBF` word and **not** in the `cc_op` spad word.
  [all-reduce](all-reduce.md), [collective-enums](collective-enums.md),
  [cce-in-transfer](../../dma/cce-in-transfer.md).
- **`reduction_type_t`** (the fold pattern): `RING_2R1W=0`, `RING_2R2W=1`,
  `KANGARING_NR1W=2`. [ring-kangaring](../ncfw/ring-kangaring.md).
- **dtype** rides `dtype@13` → `SDMA_DTYPE` (byte-identical pass-through). The
  CCE-reducible set = `{BF16, FP16, FP32R, FP8_E3/E4/E5}` (plain FP32 and integers
  absent). [all-reduce](all-reduce.md), [collective-enums](collective-enums.md).

---

## 4 · The barrier / semaphore model

### 4.1 · The EVT_SEM substrate (spot-verified §0.2 #8)

A 1 MiB SoC aperture per instance: 256 events (1-bit) + 256 semaphores (32-bit), with
**op-aliased windows over the same 256 physical counters**:

| window | offset | role |
|---|---|---|
| `SEMAPHORE_READ` | `+0x1000` | the WAIT-GE poll (RO) |
| `SEMAPHORE_SET` | `+0x1400` | init / reset |
| `SEMAPHORE_INC` | `+0x1800` | the ARRIVE / per-step trigger |
| `SEMAPHORE_DEC` | `+0x1C00` | and-dec / reset |

The TOP_SP-hosted copy (`cayman_get_sp_sema_{r,s,i,d}_ofst`) is the **same four windows**
on the per-TOP_SP base `0x8280000000` (stride `0x40000000`, `<<0x2f` die bit).
`[HIGH/OBSERVED; per-page detail: top-sp-lowering §4.]`
[top-sp-lowering](top-sp-lowering.md), [core-barrier](core-barrier.md).

### 4.2 · The three-barrier trilogy (the scope ladder)

| op | barrier | scope | mechanism | lowers to | page |
|---|---|---|---|---|---|
| `0xD5` | `SYNC_BARRIER` | **engines of ONE core** (PE/ACT/POOL/DVE/[T]SP) at a loop boundary | EVENTS (1-bit), no id/sema — FINEST | `EVENT_SEMAPHORE 0xA0` over `tpb_events` | [sync-barrier](sync-barrier.md) |
| `0xD8` | `CORE_BARRIER` | **pcores of one VNC** (LNC1/2/4/8) | 32-bit counted SEMAPHORE (arrive INC / wait GE-N / reset DEC), carries `id`+`semaphore` — CROSS-CORE | `EVENT_SEMAPHORE 0xA0` over `tpb_semaphores` | [core-barrier](core-barrier.md) |
| `0xC3` | `DMABARRIER` | **SBUF writes vs a DMA read** (single-actor) | timed `Nop(NUM_ROWS)` stall — ORTHOGONAL, no event/sema traffic | `Nop(NUM_ROWS)` | [dma-barrier](dma-barrier.md) |

> **NOTE — no timeout in any of the three.** The participant count `N` (CORE_BARRIER
> `semaphore_value`) = the pcores of that VNC; all three spin unconditionally —
> liveness is a compiler/runtime guarantee, not a hardware timeout.
> `[CARRIED — core/sync/dma-barrier §8, HIGH/OBSERVED]`

### 4.3 · EVT_SEM + the NCFW 4-step barrier (the COARSE cross-rank fence)

The NCFW device barrier is a **4-step counted-semaphore** barrier that **brackets**
each ring/mesh phase: `barrier_step_config[4]` (52 B each), `barrier_step_sizes
{1,4,4,1}`, op `{0:SIGNAL/arrive, 1:WAIT-GE-and-dec, 2:NOP}`. Canonical:

```text
  step0 local ARRIVE → step1 SIGNAL ≤4 leader peers → step2 WAIT on those 4 → step3 local RELEASE
                                          ("4" = NEFF_DEVICE_BARRIER_MAX_LEADERS)
```

The **host barrier** (16 B `barrier_start@0` / `barrier_done@8`) merges with the device
done sema in host-CC mode; host-side timeouts exist there
(*"non-leader workers waiting at barrier exceeded %d seconds"*). Named TOP_SP sema-ids:
`ENCD_TRIGGER=0`, `COMPLETION=1`, `BARRIER_SEMA0/1=2/3`, `BARRIER_DMA_SYNC=4`,
`FUNCTION_SWITCH=5`, `HOST_BARRIER0/1=6/7`.
[neff-device-barrier](../ncfw/neff-device-barrier.md),
[neff-host-barrier](../ncfw/neff-host-barrier.md).

### 4.4 · Per-step handshake vs global barrier — they COMPOSE

The per-step **kring/ring/mesh peer handshake** (`mine` + `peers[0..2]` / `recv_sema` /
`send_sema`) is a per-chunk inc+wait-GE that sequences steps **within** a phase. The
NCFW 4-step barrier is the **global fence** that **brackets** the phase. Both hit the
**same** EVT_SEM ops (`add_semaphore_inc 0x10A0/21` = INC `+0x1800`;
`add_semaphore_wait_ge_and_dec 0x10A0/20` = READ-GE `+0x1000` then DEC `+0x1C00`).
`[CARRIED — ring-kangaring §3/§7, neff-device-barrier; HIGH ops / MED schedule]`

### 4.5 · tsync (the clock substrate under the barriers)

The TOP_SP `timestamp_inc` tick (`top_sp_ram +0x4`, RW`[23:0]` reset `0x400`) advances
a global timestamp; the dev `tsync` struct (`timestamp_local` / `timestamp_tpb` /
`timestamp_tpb_val`) is the firmware's read/align, fanned via the EVT_SEM/APB-broadcast
fabric. **Barrier sequences ops; tsync keeps the dies' clocks aligned.**
`[doorbell HIGH; the timestamp→tsync align is MED — runs on the LX/NX cores, §9.]`
[spad-ccop-tsync](../ncfw/spad-ccop-tsync.md), [top-sp-lowering](top-sp-lowering.md).

---

## 5 · The data plane — SB2SB `0xBF`, RDMA desc gen/start, M2S/S2M roles

### 5.1 · SB2SB / S3D3_COLLECTIVE (`0xBF`, real HW, POOL)

The 64-B word: `events@4`, `in_dtype@12`/`out_dtype@13` (validator enforces
`in==out`, a constrained-identity cast slot), `src_mem_pattern TENSOR3D@16` (ADDR4
base + 3 `int16` strides + 3 `uint16` dims, ≤256 elements, SBUF-only),
`lnc_size_fmt@32` (LNC1 self / LNC2 pair; **LNC4/LNC8 rejected** → intra-node ≤2-NC),
`num_active_channels@33` (1..128), `dst_mem_pattern TENSOR3D@48`. The `0xBF` word has
**no reduce-op field** — the reduce lives in the SDMA CCE descriptor on reduce-copy
legs. S3D3 does **both** structured COPY (all-gather legs, `add_dma_packet`) and
REDUCE-COPY (RS/AR legs, `add_dma_packet_cce`). Two co-operating engines: SEQ control
(`0xBF` handler) + POOL/Q7 data plane (moves the bytes).
[s3d3-collective](s3d3-collective.md), [cce-in-transfer](../../dma/cce-in-transfer.md).

### 5.2 · rdma_desc_gen (op 8) → rdma_desc_start (op 9)

- **`rdma_desc_gen` (op 8)** reads `{local_sem, remote_sem, remote_core_id,
  remote_routing_id, dma_engine_mask, src/dst addr, free_dim_bytes}` and builds, per
  selected DMA engine, an SDMA CME BD ring (16-byte BD: length+gen-tag, CME COPY cmd,
  `buf_ptr` u64 SoC addr) copying `[src, src+free_dim_bytes]` across 128 SBUF
  partitions, **plus** a LOCAL sema descriptor (bumps `local_sem` = source release) and
  a REMOTE sema descriptor (bumps `remote_sem` on the peer = dst ready). Can be issued
  early (off the critical path).
- **`rdma_desc_start` (op 9)** does DRAIN → role split (PRID parity) → TX/RX doorbell.

**The M2S/S2M role mapping** (pinned from the DRAM `"TX\0RX\0"`/`"M2S\0S2M\0"` token
block):

| role | host kind | leg | tail-ptr write |
|---|---|---|---|
| **TX** (`is_send=1`, sender) | `ENC_SEND` → `direct_send` | SB2SB TX leg, `left_pop` (waits for RX signal) | M2S `TDRTP_inc (+0x1038)` = the CME COPY LAUNCH |
| **RX** (`is_send=0`, receiver) | `ENC_RECV` → `direct_recv` | SB2SB RX leg, `right_push` (signals TX) | S2M `RDRTP_inc` (publish empty recv buffers) |

Both are `s32i.n a3,a4,0` tail-pointer-increment stores = the `DmaTrigger` primitive.
[s3d3-collective](s3d3-collective.md), [sendrecv](sendrecv.md).

### 5.3 · Cross-die addressing

The descriptor `buf_ptr` carries the SoC bitfield: `LOCAL[46:0]`, `DIE[47]`,
`CAYMAN_ID[53:48]` (64-die mesh), `CAYMAN_ID_VALID[54]` (=1 ⇒ route by chip).
`remote_routing_id` is rewritten into `{CAYMAN_ID, CAYMAN_ID_VALID}` (+ neighbour
`EXIT_DIE[51]`/`NEIGHBOR_ROUTE[52]`) to turn a local `dst_addr` into a remote-die
address; the copy traverses the `io_d2d` fabric. The remote SBUF must first be mapped
into the local Q7 32-bit window by `program_window`.
`[bitfield HIGH/OBSERVED; routing_id→high-bits rewrite MED.]`

### 5.4 · Completion — semaphore-over-DMA, NO interrupt

Two-semaphore (`local_sem` source-release / `remote_sem` data-ready) + a
generation-tag busy-poll. **No interrupt path** for the data plane — completion is
semaphore-over-DMA + poll; a descriptor count/role mismatch is a hard error.
`[HIGH/OBSERVED logs; exact Q7 poll site MED — FLIX-desync, §9.]`

---

## 6 · The three-reduce-layer placement (for `all_reduce`)

There are **three distinct reduce-op enums** (plus the public `nrt_op_type` API enum
and the DGE enum), at **distinct fold scopes** that compose innermost → outermost. They
agree only on the abstract **names** (ADD/MAX/MIN) — they are separate packed types with
**different encodings** (the single most common reimplementation error). All spot-verified
in the cayman header (§0.2 #4).

| scope | enum (verified) | `all_reduce` role | page |
|---|---|---|---|
| **(1) intra-vector** (cross-lane, one register) | `REDUCE_OP {ADD0,AVERAGE1,MAX2,OR3,AND4,XOR5}` (POOL `CrossLaneReduce 0x7c/0x7d`) | the innermost SIMD fold — **NOT** directly a collective leg | [collective-enums](collective-enums.md) |
| **(1.5) cross-partition** (across SBUF partitions) | Tensor-Reduce engine `F`/`G`/`R` (a distinct datapath, not one of these enums) | partition-fold a kernel may use to produce the per-rank tensor | [collective-enums](collective-enums.md) |
| **(3) cross-core / cross-buffer** (the collective ring/mesh RS/AR legs) | ISA `CCE_OP {ADD0,MAX2,MIN3}` == host `SDMA_CCETYPE {ADD0,FMA1,MAX2,MIN3,EXT4,GCE5}` | **★ THE `all_reduce` reduce ★** — the `recv_reduce_*`/`direct_reduce_*` step fold, realized in the SDMA CCE descriptor on the SB2SB reduce-copy leg | [cce-in-transfer](../../dma/cce-in-transfer.md), [all-reduce](all-reduce.md) |
| **(+) DGE compute-on-copy** | `DGE_COMPUTE_OP {NONE0,ADD1,MULTIPLY2,MAX3,MIN4}` | the DGE copy-with-compute (reduction-add DMA / scatter-add) — a FOURTH, distinct reduce enum, NOT the collective CCE reduce | [rdma-gather-pseudo-ops](rdma-gather-pseudo-ops.md) |

**Placement for `all_reduce`.** A hierarchical all-reduce folds at progressively
coarser scopes: the intra-die ring reduce-scatter folds via the **cross-core SDMA CCE**
(`SDMA_CCETYPE`), the inter-die mesh leg folds again via the **same** CCE. Layers (1) and
(1.5) are on-chip compute datapaths a kernel may use to *produce* the per-rank tensor —
distinct from the collective fold. The collective op carries the SDMA CCE op **only**;
the `cc_op` spad word and the `0xBF` word do **not** carry the reduce op.

> **GOTCHA — alignment islands (do not interchange).** `REDUCE_OP` and `CCE_OP`/`SDMA`
> agree on `ADD=0`, `MAX=2` but **disagree** on slot 1 (`AVERAGE` vs `FMA`) and slot 3
> (`OR` vs `MIN`). DGE uses a `NONE`-based numbering. `nrt_op_type {ADD0,FMA1,MAX2,MIN3,
> INVALID15}` == `SDMA_CCETYPE` for `{ADD,FMA,MAX,MIN}`; `translate_op_type` is identity.
> The cayman `CCE_OP_ADD` line even comments *"Same encoding as SDMA CCE op encoding"* and
> annotates `Multiply = 0x01` (the SDMA slot-1 the ISA CCE_OP skips). `[CARRIED —
> collective-enums; HIGH/OBSERVED]`

---

## 7 · Cross-generation architecture notes (full matrix → GEN-08)

The exhaustive per-gen support matrix lives in
[master-capability-matrix](../../generations/master-capability-matrix.md) — **cite it**.
Codename binding (authoritative, not inverted): `arch_id 0x05 = v2 = SUNDA`,
`0x0c = v3 = CAYMAN` (the reference SoC), `0x14 = v4 = MARIANA`, `0x1c = v4+ =
MARIANA_PLUS`; `MAVERICK v5` (coretype 37) is the known-but-NOT-shipped 5th gen.
[lx-isa-naming-archid-synthesis](../ncfw/lx-isa-naming-archid-synthesis.md).

**Schema-stable across the 4 shipped gens (SUNDA..MARIANA_PLUS):**

- The collective **opcodes** + their 64-B struct layouts are byte-identical
  (only the `ISA header for NC-vN` comment differs). `[CARRIED HIGH/OBSERVED]`
- The host `enc_*`/`SDMA_*` enums are single-binary (one `libnrt` serves all gens).
- The NCFW `cc_op` command-word schema + `tsync` struct + barrier step descriptor (52 B)
  + ring channel + mesh event (80 B) + hier (8 B) layouts are schema-wide byte-identical.
- The EVT_SEM 256-array geometry + four op windows are gen-stable.

**Scales / diverges per gen:**

| delta | SUNDA (v2) | CAYMAN (v3) / MARIANA (v4) / v4+ | MAVERICK (v5, un-shipped) |
|---|---|---|---|
| MESH event-tape size | **50** events; `algo_configs 0x2228`; 4 soc_addr CSRs; **no** DRAM`+0xB0` table; monolithic dispatch | **108** events; `0x3448`; 20 CSRs; DRAM`+0xB0` 12-entry table | — |
| `host_trigger` CSR | `0x60848` (compacted `+0x848`) | **`0x615a0`** (`LOCAL_REG + 0x15a0`) | INFERRED |
| SB2SB `0xBF` | **absent** (no `s3d3_collective.h`) | present (requires nc≥V3) | present |
| `gather_xpose` | absent | present (nc≥V3) | present |
| dtype space | 16-code base | base (+ MARIANA `FP4_EXP2`/`CPTC1..7`) | + `FP8_EXP2`/`INT4`/`SFP8_*`/`MXTENSOR_V2` |
| WAIT-MODE family | EVENT + SEM | EVENT + SEM (cayman==mariana==sunda) | **drops EVENT**; adds `_REG_OFFSET`/`UNORDERED`/`EVT_SEM_NONBLOCKING_CMD` |
| d2d model | smaller, earlier fabric | 216-trigger d2d file | 7-entry d2d, DDMA/CDMA/UDMA naming, FIS-shim re-pack |

> **NOTE.** `MARIANA == MARIANA_PLUS` at the device-image level (byte-identical EXTISA +
> NCFW DRAM; only an NCFW-IRAM code-shift differs). MARIANA_PLUS is a feature-flag refresh
> (`NEURON_RT_DBG_V4_PLUS`), adds 0 new ISA/EXTISA bytes. The CCE-reducible collective
> dtype set stays `{BF16,FP16,FP32R,FP8_E3/E4/E5}` across all gens.
> `[CARRIED HIGH/OBSERVED; v5 interiors INFERRED — §9 O-7.]`

> **QUIRK — `SYNC_BARRIER` on v5.** The "event-based `SYNC_BARRIER`" reading holds for
> v2/v3/v4 but **not** maverick: v5 drops the EVENT family, so a `SYNC_BARRIER` there must
> lower via the SEM/NONBLOCKING forms. The struct is byte-identical; the **lowering target**
> differs. `[CARRIED — sync-barrier, collective-enums §9.1, HIGH/OBSERVED]`

---

## 8 · Residual contradictions reconciled

These were divergences among the source findings; resolved (binary wins):

| # | divergence | resolution | tag |
|---|---|---|---|
| C-1 | codename↔v# swap (crossed cayman/mariana labels) | `0x0c=CAYMAN`, `0x14=MARIANA`; structural offsets/counts unaffected | HIGH/OBSERVED |
| C-2 | ring per-channel config stride 0x95=149 vs 0x94=148 | **148** (byte-exact `shl3/+/shl2/+/shl2`; highest field +0x90 → 145 used + 3 pad) | HIGH/OBSERVED |
| C-3 | host_barrier vs device_barrier base | ONE shared cfg base; host_barrier OVERLAYS `device_barrier_step_config[0]` (UNION, `execute_device_barrier@0x124` selects) | HIGH |
| **C-4** | **`kaena_khal` trigger vtable slot** | **`+0x708` is the `set_host_trigger` WRITE; `+0x710` is only the offset GETTER** (§0.2 #3). The all-reduce summary-table cell using `+0x710` shorthand is superseded by [top-sp-lowering](top-sp-lowering.md), which carries the authoritative split | HIGH/OBSERVED |
| C-5 | TOP_SP count 16 (runtime) vs 10/20 (CSR blocks/SoC windows) | both correct, different views (runtime-usable vs architectural); exact mapping MED | MED |
| C-6 | RDMA gen/start framing | both true — `rdma_desc_gen`/`start` are inner phases of the SB2SB decode AND first-class ExtendedInst ops 8/9 | HIGH |
| C-7 | `algo_type ≡ enc_alg_type` identity | **MED** (the 4-bit nibble + shared vocabulary + `cc_op_info{alg}` carrier converge, but no byte-level host→device write was traced — it crosses the un-disassemblable LX core) | MED/INFERRED |

> **CORRECTION carried for the Part-10 reconcile (C-4, naming both pages).**
> [all-reduce](all-reduce.md)'s **summary table row B** describes the host-trigger as
> going *"via `kaena_khal` vtable `+0x710`"*. That shorthand is **imprecise**: the
> trigger **write** (`set_host_trigger`) is slot **`+0x708`**; `+0x710` is only the
> offset getter (§0.2 #3). All-reduce's *prose* is already correct (it
> calls `+0x710` "the *offset getter*; the adjacent `set_host_trigger` performs the
> write"); only the table cell uses the loose form.
> [top-sp-lowering](top-sp-lowering.md) is the authoritative split and already flags
> this. No re-derivation contradiction — a documented, already-CORRECTED divergence.

---

## 9 · The open-items ledger

| # | open item | confidence | feeds |
|---|---|---|---|
| **O-1** | **The NCFW code-body decode limit** — the NCFW management core is a scalar Xtensa-LX with **no shipped disassembler config** (only `ncore2gp`, which mis-decodes ~26–28% of LX bytes as Vision FLIX bundles). The dispatch **spine** (function map, DRAM`+0xB0` jump table, the `(A)` command vector, the idle loop) is HIGH/OBSERVED; the per-algorithm case **bodies**, the exact on-core **step schedule**, the timestamp-align algorithm, and the byte-level host→device write of `algo_type`/the soc_addr integers are **not instruction-decodable**. The universal boundary. | MED/LOW | [main-dispatch-loop](../ncfw/main-dispatch-loop.md), [lx-isa-naming-archid-synthesis](../ncfw/lx-isa-naming-archid-synthesis.md), [ncfw-iram-images](../ncfw/ncfw-iram-images.md) |
| **O-2** | **`pring` NOT LANDED** — no PRING `enc_alg_type` value; the algorithm is undecoded. Nearest landed artifacts: the `enc_metaring` "metaring" family + `__compose_pipeline_allreduce`. | LOW/OPEN | [pring-descriptors](../ncfw/pring-descriptors.md) |
| **O-3** | `enc_can_post_*` **thresholds not enumerated** — which numeric `enc_alg_type` a given `(op, scope, world-size, dtype, topology)` resolves to is MED. | MED | [all-reduce](all-reduce.md) |
| **O-4** | The **SDMA CCE descriptor on-wire bitfield** — the `SDMA_CCETYPE` value drops into the descriptor at the CCE user offset, but the on-wire byte-layout was not dumped. | LOW | [cce-in-transfer](../../dma/cce-in-transfer.md) |
| **O-5** | Runtime-bound slot details — the `cc_op` spad entry's padded width beyond +0x6, the kangaring per-step target sema VALUES, whether all 4 `barrier_sema` / 3 kring peer slots are populated or sparse. | LOW | [spad-ccop-tsync](../ncfw/spad-ccop-tsync.md), [ring-kangaring](../ncfw/ring-kangaring.md) |
| **O-6** | The **transport-control boundaries** — the NCFW↔host command/control protocol (no separate NCFW host queue was found; the NCFW ctx is staged via the collective `hw_exec_queue` + `topsp_init` DMA), the context-memhandle vtable + host↔device staging ABI, and the device-side ring consumer loop (LX-resident). | OPEN | [xrp-host-dsp-messaging](xrp-host-dsp-messaging.md) |
| **O-7** | **Cross-gen** — the full per-gen matrix is deferred to GEN-08 (cite it). The `coretype`→silicon-part binding is OPEN; MAVERICK `arch_id 36*` is INFERRED (no NCFW v5 image — the **v5 NCFW interior is FILE-ABSENT**; do not state it as fact). | OPEN | [master-capability-matrix](../../generations/master-capability-matrix.md) |
| **O-8** | The **host→device-core handoff** (the boundary that makes O-1 + C-7 MED) — the host BUILDS the `cc_op` program + soc_addr tables and DMA-loads them onto the TOP_SP NX core, which WALKS them; the byte-level write of `enc_alg_type` into the `cc_op` nibble and the soc_addr resolution happen across a core boundary no shipped disassembler covers. Pinned structurally (`ncfw_ctx_top_sp` rooting, the spad-load strings), not instruction-traced. | INFERRED-STRONG/MED | [top-sp-lowering](top-sp-lowering.md), [spad-ccop-tsync](../ncfw/spad-ccop-tsync.md) |

---

## 10 · Confidence summary

- **HIGH / OBSERVED or SPOT (§0.2):** all collective opcodes +
  `COLLECTIVE_TYPE`/`CCE_OP`/`DGE_COMPUTE_OP`/`LNC_SIZE_FMT` enums; `enc_alg_type 0..10` +
  `SDMA_CCETYPE`; `host_trigger 0x615a0`/`0x60848` + the vtable `+0x708`/`+0x710` split;
  EVT_SEM windows `+0x1000/1400/1800/1C00`; the NRT lowering symbol addresses
  (`__select_algorithms @0x14c620`, `add_dma_packet_cce @0x2307d0`,
  `create_spad_ctrl_entry @0x232cd0`, the `enc_can_post_*` family); the device SB2SB
  strings + `ncfw_ctx_top_sp`/`run_state`. The pseudo-op catalog (§2), the barrier model
  (§4), the data plane + M2S/S2M roles (§5), the algorithm family + legality (§3), the
  three-reduce reconciliation (§6), the schema-stable + per-gen deltas (§7) — all CITED
  HIGH at their source-page confidence.
- **MED / INFERRED-STRONG:** the host `enc_alg_type` ≡ firmware `algo_type` identity (C-7);
  the per-leg numeric algo per workload (O-3); the leg↔NCFW-step correspondence + the
  per-step schedule; the `routing_id`→SoC high-bit rewrite; the tsync align algorithm;
  *"TOP_SP executes the NCFW firmware"* (O-8).
- **LOW / OPEN:** the NCFW LX-core case-body decode (O-1); `pring` (O-2); the on-wire CCE
  descriptor bitfield (O-4); the runtime-bound spad/kring/barrier slot details (O-5); the
  control-plane boundaries (O-6); the coretype→silicon binding + MAVERICK `arch_id` (O-7).

---

## Cross-references — the whole collectives chapter

**Pseudo-op catalog (Part-10 ops):**
[TriggerCollective `0xC8`](trigger-collective.md) ·
[TriggerCollective2 + Ext `0xD9`/`0xDA`](trigger-collective2-ext.md) ·
[ALL_REDUCE `0xC7`](all-reduce.md) ·
[S3D3 Collective `0xBF`](s3d3-collective.md) ·
[PseudoCurProcessingRankID `0xDB`/`0xDC`](rank-id.md) ·
[CORE_BARRIER `0xD8`](core-barrier.md) ·
[SYNC_BARRIER `0xD5`](sync-barrier.md) ·
[DMABARRIER `0xC3`](dma-barrier.md) ·
[SENDRECV `0xCB`](sendrecv.md) ·
[RDMA/DMA Pseudo-Ops `0xb8`/`0xbb`/`0xf1`](rdma-gather-pseudo-ops.md) ·
[Collective-Type + `cc_op` Enum Reference](collective-enums.md) ·
[TOP_SP Collective Lowering](top-sp-lowering.md) ·
[XRP Host↔DSP Messaging (the bespoke transport verdict)](xrp-host-dsp-messaging.md)

**NCFW algorithm layer:**
[NCFW Main Dispatch Loop](../ncfw/main-dispatch-loop.md) ·
[Ring + Kangaring](../ncfw/ring-kangaring.md) ·
[Mesh Collective](../ncfw/mesh-collective.md) ·
[Hierarchical Collective](../ncfw/hierarchical-collective.md) ·
[NCFW IRAM Images](../ncfw/ncfw-iram-images.md) ·
[LX ISA Naming / arch_id Synthesis](../ncfw/lx-isa-naming-archid-synthesis.md)

**Data plane / reduce / cross-gen / orientation:**
[CCE In-Transfer Compute (the cross-core reduce)](../../dma/cce-in-transfer.md) ·
[Master Capability Matrix (cross-gen support)](../../generations/master-capability-matrix.md) ·
[A Collective, End to End (the orientation trace)](../../orientation/collective-end-to-end.md)
