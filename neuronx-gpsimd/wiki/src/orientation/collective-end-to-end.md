# A Collective, End to End

This page follows **one collective — an `ALL_REDUCE` — from the compiler-emitted
trigger opcode to completion**, naming the real opcode, symbol, struct, CSR, and
firmware image at every hop. It is the collective counterpart of
[A Custom Op, End to End](customop-end-to-end.md): a single thread you can pull all
the way through, with each stage pointing at the Part-9 (DMA) and Part-10
(Collectives & NCFW) page that documents it in full.

The mental model to hold first: **a collective pseudo-op never executes on
hardware.** It is a *compiler placeholder* that the host runtime (`libnrt`) lowers,
at model-load time, into a program for the **TOP_SP** — the NX-core sequencer that
walks the collective step-by-step — backed by **NCFW**, the scalar Xtensa-LX
management core whose per-TOP_SP context *is* that program. The actual bytes move
over **DMA** descriptor rings (the `pring`) and, for the intra-node leg, the
**SB2SB** iDMA kernel on the POOL/Q7 engine. Three engines, three ISAs, one
collective. `[HIGH/OBSERVED]`

> **GOTCHA.** The NCFW management core is a **scalar Xtensa-LX** core, *not* a
> Vision-Q7 FLIX core. Its device images carry no shipped TIE/disassembler config,
> so its case-body instruction streams are a hard wall — decode them with the
> scalar-LX length rule (`op0 ∈ {e,f}` → 3-byte), never the FLIX decoder, which
> manufactures a spurious "~26–28% FLIX" artifact under the only shipped `ncore2gp`
> TIE. Every device-side step whose body lives only in those LX images is tagged
> **CARRIED** or **INFERRED**; the host-built *data structures* the decoders read
> are **OBSERVED**. See [The NCFW Scalar-LX Management Core](../uarch/ncfw-lx-core.md).

> **NOTE.** `libncfw.so` and the NCFW firmware blobs live in the sibling
> `neuronx-runtime` corpus, not the gpsimd checkout — the eight
> `v{2,3,4,4_plus}_ncfw_{iram,dram}_bin` symbols are in
> `neuronx-runtime/extracted/.../opt/aws/neuron/lib/libncfw.so`. The NCFW *interior*
> deep pages (Part 10) document them; this orientation traces the seam, marking
> **CARRIED** where the body is not in this checkout.

---

## The pipeline at a glance

| # | Stage | Real anchor | Image / binary | Deep page |
|---|-------|-------------|----------------|-----------|
| 1 | Compiler emits the trigger pseudo-op | `PSEUDO_TRIGGER_ALL_REDUCE = 0xc7` (or `0xc8` `ctype=ALL_REDUCE`) | NEFF instruction stream (TOP_SP, `engine_idx 5`) | [ALL_REDUCE](../collectives/ops/all-reduce.md) |
| 2 | Host NRT recognises + relocates it at load | `PseudoTriggerAll` in `libnrt` 16-char opcode table | `libnrt.so` | [Collective NRT-Load-Time Rewrite](../compiler/collective-loadtime-rewrite.md) |
| 3 | NRT builds the `cc_op` / `enc_operation` record | `enc_op_type = ENC_ALLREDUCE (1)`; reduce-op → `SDMA_CCETYPE` | `libnrt.so` (DWARF) | [Collective Enum Reference](../collectives/ops/collective-enums.md) |
| 4 | Algorithm selection (per leg) | `__select_algorithms @0x14c620` → `enc_can_post_*` → `enc_alg_type` | `libnrt.so` | [ALL_REDUCE](../collectives/ops/all-reduce.md) |
| 5 | Compose the ring/mesh/hier steps | `__compose_allreduce*` → `recv_reduce_*`(`SDMA_CCETYPE`) | `libnrt.so` | [Ring + Kangaring](../collectives/ncfw/ring-kangaring.md), [Mesh](../collectives/ncfw/mesh-collective.md), [Hierarchical](../collectives/ncfw/hierarchical-collective.md) |
| 6 | Build the TOP_SP `cc_op` SPAD program | `create_spad_ctrl_entry @0x232cd0` (byte0 `.cc_op=1`) | `libnrt.so` | [TOP_SP Collective Lowering](../collectives/ops/top-sp-lowering.md) |
| 7 | Build the persistent DMA descriptor ring | `encd_basic_block_create_toplevel_dma_pring @0x23f280` → `vring_dump_to_pring_padded` | `libnrt.so` | [pring (Persistent Descriptor Ring)](../collectives/ncfw/pring-descriptors.md) |
| 8 | Set the per-communicator exec prings | `nrt_cc_prepare @0x7f610` → `encd_set_exec_prings @0x23f9b0` | `libnrt.so` | [pring](../collectives/ncfw/pring-descriptors.md) |
| 9 | Load the SPAD onto the TOP_SP, kick it | "CTRL SPAD to SRAM" / "SLOT SPAD to TPB IRAM"; `host_trigger` LOCAL_REG `+0x15a0` | `libnrt.so` → TOP_SP NX core | [TOP_SP Collective Lowering](../collectives/ops/top-sp-lowering.md) |
| 10 | NCFW main loop dispatches the algorithm | `algo_type` (4-bit) indexes the DRAM`+0xB0` 12-entry table | `v{3,4,4+}_ncfw_iram_bin` (LX) | [NCFW Main Dispatch Loop](../collectives/ncfw/main-dispatch-loop.md) |
| 11 | Run one ring/mesh/hier step | `run_state` cursor; `recv_cnt`/`send_credit`/`m2s_val`/`s2m_val` scoreboard | NCFW LX + TOP_SP NX | [Ring + Kangaring](../collectives/ncfw/ring-kangaring.md) |
| 12 | Move the bytes (intra-node) | `SB2SB_COLLECTIVE = 0xbf` → `decode_sb2sb_collective` (POOL/Q7 iDMA) | `libnrtucode_extisa.so` | [S3D3 Collective (SB2SB)](../collectives/ops/s3d3-collective.md), [RDMA Cross-Die](../dma/rdma-cross-die.md) |
| 13 | Reduce during transfer | `SDMA_CCETYPE` → `add_dma_packet_cce` (SDMA CCE reduce) | `libnrt.so` desc / SDMA HW | [CCE In-Transfer Compute](../dma/cce-in-transfer.md) |
| 14 | Barrier + completion | device `barrier_sema[4]`/`target_sema_val[4]`; host `barrier_done` | NCFW + `libnrt.so` | [NEFF Device Barrier](../collectives/ncfw/neff-device-barrier.md), [NEFF Host Barrier](../collectives/ncfw/neff-host-barrier.md) |

The rest of this page walks those fourteen hops with the reasoning that ties each
to the next.

---

## 1 · The compiler emits a trigger pseudo-op

`ALL_REDUCE` enters the pipeline as a 64-byte TPB instruction word in the NEFF,
targeting the **TOP_SP** engine (`engine_idx 5`). There are three legal carriers,
all `OBSERVED` in the cleartext arch-ISA headers
(`aws_neuron_isa_tpb_common.h`, Cayman):

- **`0xc7` `PSEUDO_TRIGGER_ALL_REDUCE`** — the dedicated, fixed form. 64 B:
  `header`(opcode `0xc7`)/`events`/`op`(reduce ALU\_OP @12)/`dtype`(@13)/
  `input_tensor_id`(@16)/`output_tensor_id`(@20)/`num_elements`(u64 @24). No
  `ctype`, no `group_id`, no offsets — the op *kind* is fixed by the opcode.
- **`0xc8` `PSEUDO_TRIGGER_COLLECTIVE`** with `ctype = COLLECTIVE_TYPE_ALL_REDUCE = 0x1`
  (@32). DRAM tensor-handle operands, a `group_id` (@14) replica group, and
  `src/dst_offset_elems` (@48/@56).
- **`0xd9`/`0xda` `PSEUDO_TRIGGER_COLLECTIVE2(_EXT)`** with `ctype = 0x1` — the v2
  form, SBUF 2-D operands plus explicit `channel_id`/`stream_id`/cc-buffer/DMA
  priority. Two back-to-back 64 B words.

`[HIGH/OBSERVED — opcode enum lines `0xc7`/`0xc8`, `ALL_REDUCE = 0x1`, and the
compile-verified `sizeof==64` struct layouts.]`

The decisive class rule is in the header verbatim: *"all pseudo instructions have
upper three bits of the opcode equal to `0b110` … generated by compiler and
translated into non-pseudo HW instructions by NRT."* `0xc7 = 0b1100_0111` — upper
three bits `0b110` — so it **never runs on hardware**. `[HIGH/OBSERVED]`

> **QUIRK.** `ALL_REDUCE` is *both* a distinct opcode (`0xc7`) *and* a `ctype`
> value (`0x1`) on the generic `0xc8` and v2 `0xd9/0xda` triggers — the runtime
> recognises both mnemonics. The dedicated `0xc7` is the compact single-replica-
> group form; the richer knobs live on `0xc8`/`0xd9`.

The reduce operation (`ADD/MAX/MIN`, or `FMA` on the API path) rides the 1-byte
`op` (`ALU_OP`) field: `ADD=0x04`, `MAX=0x08`, `MIN=0x09`. The dtype rides the
1-byte `dtype` field, and passes through *verbatim* — `ISA DTYPE ≡ SDMA_DTYPE ≡
nrt_dtype`, no re-encoding. `[HIGH/OBSERVED]` See
[ALL_REDUCE](../collectives/ops/all-reduce.md) and
[Collective Enum Reference](../collectives/ops/collective-enums.md).

The sibling pseudo-ops that bracket a lowered sequence are the same family:
`0xdb` `PSEUDO_CUR_PROCESSING_RANK_ID` (per-rank index), `0xcb` `SEND_RECV`,
`0xd8` `PSEUDO_CORE_BARRIER`, `0xd5` `PSEUDO_SYNC_BARRIER`, `0xc3`
`PSEUDO_DMABARRIER`. `[HIGH/OBSERVED — spot-verified in the Cayman `common.h`.]`

---

## 2–3 · Host NRT recognises and relocates the op at load time

At model load, `libnrt` decodes the NEFF stream. The trigger mnemonics
`PseudoTriggerAll` and `PseudoTriggerCol` sit, sixteen ASCII characters each, in
the runtime's opcode-name table — recoverable directly from the binary as a packed
run `…PseudoTriggerAllPseudoTriggerColPseudoReadVar…`. `[HIGH/OBSERVED — `strings`
of `libnrt.so`.]` The `0xd9` path additionally asserts
`ptc2_ins->header.opcode == PSEUDO_TRIGGER_COLLECTIVE2`.

The **load-time rewrite** *relocates the pseudo trigger word into a TOP_SP
instruction series* and emits the backing DMA rings. There is no in-place byte
patch of the 64 B word; NRT consumes the pseudo-op fields and produces a separate
TOP_SP `cc_op` SPAD program plus DMA descriptor rings (stages 6–8). `[HIGH/OBSERVED
for the mnemonic table and the emitted artifacts; the byte-level relocation table is
CARRIED at the NEFF-relocation deep page — the 1:1 expansion is not in the headers.]`
See [The Collective NRT-Load-Time Rewrite](../compiler/collective-loadtime-rewrite.md).

NRT builds an `enc_operation` / `cc_op` record from the decoded fields, mapping the
three ISA vocabularies onto the runtime's:

- **op kind** → `enc_op_type = ENC_ALLREDUCE (1)`.
- **reduce op** → `SDMA_CCETYPE {ADD=0, FMA=1, MAX=2, MIN=3, EXT=4, GCE=5}`. This
  is *byte-exact* the hardware `CCE_OP` encoding — the header even says so:
  `NEURON_ISA_TPB_CCE_OP_ADD = 0x00 // Same encoding as SDMA CCE op encoding`. The
  public-API enum `nrt_op_type` orders identically, and `translate_op_type` is an
  identity map for `ADD/FMA/MAX/MIN`. `[HIGH/OBSERVED — both enum tables + the
  verbatim header comment + the `nrt_cc_prepare` `cmp $0x1` (FMA) / `cmp $0x2`
  (MAX) dispatch.]`
- **dtype** → `SDMA_DTYPE` (verbatim).

`[HIGH/OBSERVED]` See [Collective Enum Reference](../collectives/ops/collective-enums.md).

---

## 4 · Algorithm selection — per leg

`ALL_REDUCE` is not one algorithm; it is a *choice* the selector makes per
communication leg. `enc_hier_primitive::__select_algorithms @0x14c620` probes
capability predicates — `enc_can_post_mesh_operation`,
`enc_can_post_single_cycle_ring`, `enc_can_post_inter_rdh_operation`,
`enc_can_post_kangaring_operation`, … — and assigns an `enc_alg_type` per leg from:

```
ENC_ALG_RING=0  ENC_ALG_HIER=1  ENC_ALG_MESH=2  ENC_ALG_KANGARING=3
ENC_ALG_SINGLE_CYCLE_RING=4 … ENC_ALG_BW_OPT_MESH=10  ENC_ALG_INVALID=11
```

The legal algorithms for the all-reduce legs, read verbatim from `libnrt`
assertion strings:

- intra reduce-scatter / all-gather: `RING` or `KANGARING`
- inter all-reduce: `RING`, `INTER_RDH`, `SINGLE_CYCLE_RING`, or `MESH`

`SINGLE_CYCLE_RING` is an all-reduce-only optimisation — proven by the
`cmp $0x1,%ebp ; jne <reject>` guard (`op_type == 1 == ENC_ALLREDUCE`) inside
`enc_can_post_single_cycle_ring @0xfaa80`. `[HIGH/OBSERVED — call census + the
op_type guard + the per-leg legality strings.]` Which numeric algorithm a given
(world-size, topology, dtype) resolves to depends on the `can_post` thresholds and
is `[MED/INFERRED]`.

---

## 5 · Composing the ring / mesh / hierarchical steps

The chosen algorithm's composer emits the concrete step primitives. For a **ring**
all-reduce, `enc_metaring_primitive::__compose_allreduce_channel @0x171600` emits:

- **reduce-scatter phase:** `recv_reduce_send` / `recv_reduce_copy_send`
  (`SDMA_CCETYPE`) — each rank receives a chunk, CCE-reduces it into its own, and
  forwards it.
- **all-gather phase:** `direct_recv_send` / `direct_recv` / `send` / `post_recv` —
  the fully-reduced chunks rotate the ring *without* reduction.

This is the textbook (N−1 reducing + N−1 copy) ring all-reduce. For a **hierarchical**
all-reduce the page-table builders name the decomposition exactly —
`__build_allreduce_pgt_intra_rdsc` → `__build_allreduce_pgt_inter_allr` →
`__build_allreduce_pgt_intra_allg`, i.e. *intra reduce-scatter → inter all-reduce →
intra all-gather*. The runtime even logs each pipeline slice as
`copy_slice_sz`/`reduce_slice_sz`, directly exposing the split. `[HIGH/OBSERVED —
the composer call census + the builder symbol names + the per-slice log line.]`

See [Ring + Kangaring](../collectives/ncfw/ring-kangaring.md),
[Mesh](../collectives/ncfw/mesh-collective.md), and
[Hierarchical](../collectives/ncfw/hierarchical-collective.md).

---

## 6 · The TOP_SP `cc_op` SPAD program

The composed steps are packed into a **`cc_op` command table** that the TOP_SP NX
core will walk. `create_spad_ctrl_entry @0x232cd0` builds each 8-byte entry with
**byte0 bit0 = `.cc_op = 1`** (the "active cc-op" flag), the `algo_type` (bits
[0:3]) / `algo_sub_type` (bits [4:6]) nibbles, `trigger_next` (bit 7), and a 4-byte
`channel_list`-or-semaphore union. The host logs each entry with a roster that is
the `cc_op` field set verbatim:

```
CTRL mark -alg %d-subalg %d-trignext %d-chlist %d-reporter %d-sema_shift_offset %u-sema_mask %u-safe_mode %d
```

`[HIGH/OBSERVED — `create_spad_ctrl_entry` disasm + the `CTRL mark` string read
directly from `libnrt.so`.]` The per-algorithm config builders
(`encd_populate_metaring_topsp_config @0x240bf0`,
`encd_populate_mesh_topsp_config @0x240330`) chain these entries
(`trigger_next`/`complete_prev`) into the program, so a *hierarchical* all-reduce
becomes **multiple chained `cc_op` entries** — one per leg.

See [TOP_SP Collective Lowering](../collectives/ops/top-sp-lowering.md) and
[NCFW spad-ctrl `cc_op` Table](../collectives/ncfw/spad-ccop-tsync.md).

---

## 7–8 · The persistent DMA descriptor ring (`pring`)

The bytes move over a **persistent DMA descriptor ring** — the `pring`. This is
*not* the collective topology ring; it is the physical array of Annapurna-Labs
`al_udma_desc` (16 B union: `tx`/`tx_meta`/`rx`/`raw`) the DMA engine walks by
advancing a tail pointer. The host builds it in two stages:

- A **`vring`** (344 B template, with *variable*/template descriptors,
  `is_template`, `max_var_id`) is built per compile.
- `encd_basic_block_create_toplevel_dma_pring @0x23f280` allocates HBM
  (`dmem_alloc_aligned`), wraps it in a 32-byte `dma_ring_info` handle
  (`{type, ring_mem, ring_offset_bytes, used/allocated_desc_count}`), and
  **`vring_dump_to_pring_padded @0x3136f0`** copies the template into the physical
  ring — logged verbatim `Copying vring to pring %s` /
  `vring is copied to pring. ndesc=%u`. It builds a **pair** per basic block — an
  M2S (`TX=1`) `pring` and an S2M (`RX=2`) `pring` — logged
  `TopLevelDMA M2S pring for basic block %u …` / `… S2M pring …`.

`[HIGH/OBSERVED — the `0x200000001` `{TX,RX}` type pair, the builder flow, the
`dma_ring_info` DWARF, and all four strings read directly from `libnrt.so`.]`

For collectives the persistence is explicit: **`nrt_cc_prepare @0x7f610` →
`encd_set_exec_prings @0x23f9b0`** sets the rings *once* per communicator (they
live for its `world_size`/`root_comm_id` lifetime; `encd_free_exec_prings` tears
them down) — the literal "exec prings". The standing ring is *switched, not
rebuilt*: `switch_completed` + the `basic_block_switch` doorbell (LOCAL_REG
`+0x15e0`, `0x615e0` Cayman) advance the firmware to the next basic block's pring.
`[HIGH/OBSERVED for the set/free pair, the switch field + doorbell; INFERRED-STRONG
for "survives across iterations" — the device walk is in the LX core.]`

See [pring (Persistent Descriptor Ring)](../collectives/ncfw/pring-descriptors.md),
[The DMA / Descriptor Model](../dma/descriptor-model.md), and
[The al_udma Hardware DMA Engine](../dma/udma-hw-engine.md).

---

## 9 · Loading the program onto the TOP_SP and kicking it

`encd_start_executable @0x2431c0` DMAs the program onto each of the **16** runtime
TOP_SP engines (`tdrv_arch_get_num_topsp_cayman → 0x10`): a **4 KiB CTRL SPAD →
SRAM** (the `cc_op` command table) and a **32 KiB SLOT SPAD → TPB IRAM** (per-slot
data), logged `TOPSP #%u will load CTRL SPAD to SRAM` / `… SLOT SPAD to TPB IRAM`.
It then writes value `1` to the **`host_trigger`** doorbell at LOCAL\_REG `+0x15a0`
— `0x615a0` (Cayman/Mariana), `0x60848` (Sunda) — dispatched through the
`kaena_khal` HAL vtable slot `+0x708` (`aws_hal_sp_topsp_set_host_trigger`; the
adjacent `+0x710` is the *offset getter*, not the write). This one-shot write is the
literal *"it then triggers the operation"* of the `0xc7` header comment.
`[HIGH/OBSERVED — the SPAD-load strings, the size immediates `0x1000`/`0x8000`, and
the `mov esi,1 ; … +0x15a0` doorbell write read from `libnrt.so`.]`

There are thus **two trigger surfaces**: (a) this one-shot NX-program *start*
doorbell, and (b) a **per-step DMA-tail doorbell** baked into the descriptor ring —
`encd_get_trigger_addr @0x23f150` resolves an `EVT_SEM SEMAPHORE_INC` address
(`sp_base 0x8280000000` + `0x1800` + `idx*4`) that a tail-pointer write hits, which
the TOP_SP polls (`WAIT_FOR_SEM_GE`) to advance. `[HIGH/OBSERVED]`

See [TOP_SP Collective Lowering](../collectives/ops/top-sp-lowering.md) and
[CSR — UDMA M2S](../control/csr/udma-m2s.md).

---

## 10–11 · NCFW dispatch and the per-step state machine

Once kicked, the **NCFW management core** runs its main loop. NCFW's runtime
context is rooted at the TOP_SP — `libncfw`'s `ncfw_ctx_log @0x19f01` emits the
whole context under the key `ncfw_ctx_top_sp` — so the NCFW collective program *is*
the TOP_SP's `cc_op` program, and the TOP_SP NX core is the NCFW's per-NeuronCore
executor. `[HIGH/OBSERVED that the context key is `ncfw_ctx_top_sp`;
INFERRED-STRONG for the executor relationship — it crosses into the LX/NX cores.]`

The dispatch loop is the **largest function** in the NCFW IRAM image (Cayman/`v3`
`@0x3bb0`; Mariana/`v4` `@0x3bc8`, the same body relocated `+0x18`). It extracts a
7-bit command/`algo_type` field and indexes a **12-entry jump table at DRAM`+0xB0`**
via `const16 a2,0xB0 ; addx4 ; l32i.n` — a one-shot byte pattern that appears
*exactly once* per image. The `algo_type` 4-bit selector (the low nibble of the
host `enc_alg_type`) picks the union arm:

| `algo_type` leg | device case cluster | deep page |
|-----------------|---------------------|-----------|
| ring / kangaring | `0x3c..` | [Ring + Kangaring](../collectives/ncfw/ring-kangaring.md) |
| mesh | `0x3c..`/`0x3e..` | [Mesh](../collectives/ncfw/mesh-collective.md) |
| hierarchical | `0x3e..` | [Hierarchical](../collectives/ncfw/hierarchical-collective.md) |
| barrier | a case + `0x3e..` | [Device Barrier](../collectives/ncfw/neff-device-barrier.md) |
| default / error | idx 3 → `0x48c4` → `0x48e0` | — |

`[OBSERVED HIGH for the table bytes, the one-shot `const16 0xB0` dispatch read, and
all targets falling inside the main loop; INFERRED MED for which case label
implements which arm — the case **bodies** are FLIX-corrupted under the only
shipped (`ncore2gp`) core, the scalar-LX wall again.]`

> **NOTE.** Sunda (`v2`) is *structurally different*: it has **no** DRAM`+0xB0`
> dispatch table (no `const16 0xB0`) and a more monolithic loop (largest function
> `@0x5f60`, ~1.8× the v3 body). The per-iteration command vector (3 trampolines +
> a default slot) is common to all four gens. `[OBSERVED HIGH.]`

Each iteration runs **one step** of the selected algorithm and advances its
scoreboard. For a ring, the per-channel mutable scoreboard (`OBSERVED` via the host
decoder `ncfw_log_algorithm_ring_channel`) is: `recv_cnt`(@0)/`send_credit`(@2)/
`m2s_val`(@6)/`s2m_val`(@8)/**`run_state`**(@0xf). Each step the firmware bumps
`recv_cnt`, consumes `send_credit`, advances `m2s_val`/`s2m_val` along the ring,
and steps `run_state`; `recv_cnt`+`send_credit` are the classic ring flow-control
pair. Topology lives in the `next_neigh`/`prev_neigh` `ring_neighbor` structs
(28 B each), the per-step rendezvous in the `post_sema`/`dma_compl_sema` SOC
addresses — `dma_compl_sema` is what the DMA engine fires on transfer completion.
`[HIGH/OBSERVED for the scoreboard/topology fields via the host decoders; the
per-step transition logic runs in the LX case bodies — INFERRED MED / CARRIED.]`

The loop parks in a `waiti 15` idle (one per image) until a notification semaphore
wakes it. See [NCFW Main Dispatch Loop](../collectives/ncfw/main-dispatch-loop.md)
and [NCFW DRAM Images + ctx_log](../collectives/ncfw/ncfw-dram-ctx-log.md).

---

## 12–13 · Moving (and reducing) the bytes

A ring/kangaring step is, concretely, a `sendrecv` — and the intra-node leg is the
**SB2SB collective**, `SB2SB_COLLECTIVE = 0xbf`, a *real* (non-pseudo) hardware op
run by the POOL/Q7 iDMA kernel. The device ucode decoder
**`decode_sb2sb_collective`** (in `libnrtucode_extisa.so`) does the SBUF→SBUF copy
with a Pool/Q7 pre-sync handshake — `OBSERVED` strings
`P%i: Decode : SB2SB_Collective` and `SB2SB_Collective : total_src_nelem=… dtype=…
mask=0x%x`. The TOP_SP only *sequences* the tail-pointer increment (`tp_inc_steps[%d]
= m2s %d, s2m %d, repeat %d`) that launches the copy; the POOL/Q7 kernel moves the
bytes. `[HIGH/OBSERVED — the device-ucode strings + the absence of any
`TriggerAllReduce`/`TriggerCollective` op in the device decode set.]`

> **NOTE.** The device decode set is exactly `{ExtendedInstCopy, …, SB2SB_Collective,
> Sbuf2Sbuf}` — **no** trigger pseudo-op among them. This proves the pseudo
> `ALL_REDUCE` is lowered *host-side* before any device decode; the device only ever
> sees the lowered SB2SB/DMA legs. SB2SB itself is **absent on Sunda (v2)** — it
> requires NC≥v3, so Sunda's intra-node collective takes the RDMA leg instead. Cross-
> die transfers use [RDMA Cross-Die SBUF→SBUF P2P](../dma/rdma-cross-die.md).

The **reduce** happens *during* the transfer. `SDMA_CCETYPE` (stage 3) is the arg to
every `recv_reduce_*` step primitive, which builds an SDMA descriptor packet with
the **CCE reduce field set** — `add_dma_packet_cce` /
`al_sdma_m2s_build_cce_ext_meta_ctrl`. The SDMA engine performs the element-wise
reduce on receive, which is exactly the collective's `op`. The CCE-reducible dtype
set (the `cce_dtypes.4` seed table @ `0x9b9f40`) is `{BF16, FP16, FP32R,
FP8_E3/E4/E5}` — plain FP32 (`0x0A`) and integers are absent. `[HIGH/OBSERVED for
the descriptor builders and the dtype table bytes; the exact per-value
`ALU_OP→SDMA_CCETYPE` remap is MED.]` See
[CCE (Compute-DMA) In-Transfer Compute](../dma/cce-in-transfer.md).

---

## 14 · Barriers and completion

Phases are bracketed by barriers. The **device barrier** is a 4-step machine: the
NCFW config carries `barrier_step_config[4]` (52 B each), each step holding 4 peer
`barrier_sema[k]` SOC addresses (u64, stride 8) and 4 `target_sema_val[k]` (u32,
stride 4). A step *arrives* by DMA-broadcast-writing each peer's `barrier_sema[k]`
(`add_semaphore_inc`, device op `0x10A0` subop 21), then *waits*
(`add_semaphore_wait_ge_and_dec`, subop 20) until each reaches its
`target_sema_val[k]`. The compile-time step shape is the `{1,4,4,1}` table
(`barrier_step_sizes`): step0 local arrive → step1 signal up to 4 leaders → step2
wait on 4 → step3 local release. The **host barrier** is a 2-semaphore
`barrier_start`/`barrier_done` pair the host polls to enter/leave the device's
rendezvous — the TOP_SP gates it (`received clearance from top_sp to execute
enc_barrier`). `[HIGH/OBSERVED — the `barrier_sema`/`target_sema_val` arrays, the
`{1,4,4,1}` step-size table, and the `host_barrier` `barrier_start`/`barrier_done`
sub-decoder, all from the `libncfw`/`libnrt` decoders.]`

Completion is reported by writing the completion semaphore CSRs — a `leader`/
`reporter` TOP_SP (matching `cc_op.reporter`) posts it — with the host-visible done
flag `neff_ctx.barrier_completed` and `host_barrier.barrier_done`. The exact device
write is in the FLIX-corrupted LX case bodies — `[fields OBSERVED HIGH; the device
write path CARRIED/INFERRED MED.]` See
[NEFF Device Barrier](../collectives/ncfw/neff-device-barrier.md),
[NEFF Host Barrier](../collectives/ncfw/neff-host-barrier.md), and
[PSEUDO_CORE_BARRIER (0xD8)](../collectives/ops/core-barrier.md).

---

## The wall: v5 (Maverick) collective firmware is file-absent

For the four shipped generations — **Sunda (v2)**, **Cayman (v3)**, **Mariana
(v4)**, **Mariana+ (v4+)** — the NCFW images ship as eight
`v{2,3,4,4_plus}_ncfw_{iram,dram}_bin` blobs in `libncfw.so`, selected by the
`libncfw_get_image @0x1179` `cmpl` ladder `{0x05, 0x0c, 0x14, 0x1c}` (the per-gen
`arch_id`), with the source-file tokens `sunda.c`/`cayman.c`/`mariana.c`/
`mariana_plus.c` in `.rodata`. `[HIGH/OBSERVED — the blob symbols + the selector +
the source strings read directly from `libncfw.so`.]`

> **GOTCHA — a named wall.** **There is no v5 / Maverick NCFW image.** The
> `libncfw_get_image` ladder tops at MARIANA_PLUS (`0x1c`); a higher `arch_id`
> (incl. the inferred Maverick `0x24`) falls to the default leg (`return 2`). There
> is **no `maverick.c` token, no `v5_ncfw_*_bin` blob, and no `v5`/`maverick` string
> anywhere in `libncfw.so`** — the `.rodata` is contiguous and closed, the last
> `v4_plus_ncfw_iram_bin_size` symbol ending immediately before
> `__GNU_EH_FRAME_HDR`, with physically no room for a fifth image. The collective
> *enums* (`COLLECTIVE_TYPE`, `ALU_OP`, the `NC-v5` banner) are byte-identical to the
> earlier gens in the internal-only Maverick arch-ISA header, and Maverick adds **no
> new collective pseudo-op** — its delta is the *lowering* (a sync/transport re-model:
> `WAIT_MODE` gains `SEM_*_REG_OFFSET`/`UNORDERED`, the EVENT family dropped, so a
> `PSEUDO_SYNC_BARRIER 0xd5` cannot lower to an EVENT barrier on v5). But every v5
> collective **interior** — the ring/mesh/hier step bodies, the dispatch table, the
> mesh-event capacity — is **INFERRED only**, with no NCFW firmware to read. `[enums/
> banner SPOT-HIGH; the absence is HIGH/OBSERVED-negative; v5 interiors LOW/OPEN —
> CARRIED as a wall.]`

When you reach a v5 claim anywhere in Part 10, read it as the `coretype−1`
`arch_id` extrapolation plus an enum diff — not as observed firmware. See
[The SUNDA v2 Baseline Topology](../generations/sunda-v2-baseline.md),
[NCFW arch_id Diff](../collectives/ncfw/lx-isa-naming-archid-synthesis.md), and
[Codename ↔ Generation Cross-Walk](../reference/codename-crosswalk.md).

---

## What to carry forward

- A collective is a **host-side lowering**, not a device opcode. `0xc7`/`0xc8`/`0xd9`
  are placeholders the runtime relocates into a TOP_SP `cc_op` program + DMA `pring`s.
- The **TOP_SP** (NX core, `engine_idx 5`) walks the program; the **NCFW** (scalar
  Xtensa-LX) core's per-TOP_SP context *is* that program; the **POOL/Q7 SB2SB**
  kernel and the **SDMA CCE** move-and-reduce the bytes. Three engines, three ISAs.
- The reduce-op carrier `SDMA_CCETYPE` *is* the hardware CCE encoding, end to end.
- The `pring` is a *persistent, switched-not-rebuilt* descriptor ring, set once per
  communicator (`encd_set_exec_prings`).
- The **NCFW interiors and all of v5 are walls** — the LX core has no shipped
  disassembler, and no v5 NCFW image ships. Decode the LX images with the scalar
  length rule, never FLIX; tag v5 claims `INFERRED`.

Part 9 (DMA) and Part 10 (Collectives & NCFW) give the full-reference treatment of
every hop; this page is the thread that connects them.

---

> **Provenance.** Every fact here derives solely from static analysis of shipped,
> redistributable binaries, headers, and config — lawful interoperability reverse
> engineering under DMCA 17 U.S.C. § 1201(f). The opcode/struct anchors are from the
> cleartext `aws_neuron_isa_tpb_*` headers; the runtime symbols, strings, and CSR
> offsets from `libnrt.so` / `libncfw.so` / `libnrtucode_extisa.so`; the NCFW image
> structure from the carved `v{2,3,4,4_plus}_ncfw_{iram,dram}_bin` blobs via the
> shipped XtensaTools. No vendor source tree was referenced, consulted, or quoted.
