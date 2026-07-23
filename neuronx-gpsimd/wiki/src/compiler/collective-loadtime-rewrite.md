# The Collective NRT-Load-Time Rewrite (byte-level)

A BIR `InstCollective(ALL_REDUCE / ALL_GATHER)` never reaches an engine. It is a
**pseudo trigger** (`0xC7` / `0xC8` / `0xD9`+`0xDA`) the compiler emits into the
NEFF stream, targeted at the TOP_SP sequencer; the host NRT runtime (`libnrt.so`)
expands it, **at model-load time**, into a concrete RING / HIER / MESH SDMA
schedule. This page is the byte-level walk of that expansion — the
`InstCollective → SELECT → COMPOSE → EMIT` rewrite — naming the real symbol and
address at every hop, in four stages:

1. **ALGORITHM SELECTION** — which of the 5 `enc_comm` engines
   (ring / kangaring / rdh / hier / mesh) is chosen, by topology + size + dtype +
   op, via `enc_hier_primitive::__select_algorithms` and the `enc_can_post_*`
   predicate census ([§1](#1-algorithm-selection)).
2. **TOPOLOGY → SCHEDULE** — peer routing + ring/tree construction in the host
   topology library (`libnccom`): `NeuronEdge` / `EdgeRemoteMLA` / `findPath` /
   `seng^2`, materialised into the `enc_ring` / `enc_kangaring` leaf structs the
   per-channel composer consumes ([§2](#2-topology--schedule)).
3. **PER-STEP SDMA DESCRIPTOR GENERATION** — the channel composer's
   `recv_reduce_*` (reduce leg) vs `direct_*` (copy leg) step vocabulary →
   the SB2SB `0xBF` leg (`encd_dma_{copy,reduce_copy}_sb2sb`) → the CCE reduce
   descriptor (`add_dma_packet_cce`) ([§3](#3-per-step-sdma-descriptor-generation)).
4. **SEMAPHORE / COMPLETION WIRING** — the `EVT_SEM` two-semaphore (local / remote)
   chaining, the TOP_SP `cc_op` SPAD program, and the
   `add_semaphore_wait_ge_and_dec` counted barrier across steps
   ([§4](#4-semaphore--completion-wiring)).

[§5](#5-the-two-worked-lowerings) carries AllReduce and AllGather end-to-end with
the **byte-exact channel-composer contrast** — the cleanest proof of the
reduce-vs-copy split.

> **Provenance.** Every symbol, address, enum value, and disassembly line below is
> re-grounded against the shipped binaries: the host NRT runtime
> `libnrt.so.2.31.24.0` (`aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce`, BuildID
> `8bb57aba…c102e`, x86-64, **not stripped**, with `.debug_info`) — the encoder
> that lowers the pseudo-ops — and the host topology library
> `libnccom.so.2.31.24` (`aws-neuronx-collectives 2.31.24.0-1a31ba186`). In
> `libnrt.so` `.text` / `.rodata` are `VMA == file-offset`, so `objdump
> --start-address` reads index the file directly; C++ vtable slots are measured
> from `vptr = _ZTV + 0x10`. Device-leg strings cross-check against
> `libnrtucode_extisa.so`. Confidence tags `[HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED]`:
> **OBSERVED** = read/disassembled directly from a shipped artifact;
> **INFERRED** = reasoned over OBSERVED; **CARRIED** = established on a cited
> committed page. The reduce arithmetic itself is in silicon (the SDMA CCE ALU),
> not the binary — the binary carries only the descriptor *builders*.

This is the host-side opening of the [ALL_REDUCE](../collectives/ops/all-reduce.md)
keystone and the [collective end-to-end](../orientation/collective-end-to-end.md)
thread: those pages give the op-struct, the device walk, and the firmware state
machine; this page gives the **load-time rewrite byte-exactness**.

---

## 0. The headline — a SELECT → COMPOSE → EMIT pipeline, not a single rewrite

The rewrite is a three-phase pipeline, and every named function is OBSERVED at the
cited address:

```text
BIR InstCollective(ctype)                    // compiler emits a PSEUDO trigger 0xC7/0xC8/0xD9
        │   (NRT recognises the pseudo at load; upper 3 opcode bits 0b110 ⇒ never HW-decoded)
        ▼
(1) SELECT   enc_hier_primitive::__select_algorithms        @0x14c620   → enc_alg_type per LEG
                + enc_can_post_*  (mesh×5 / inter_rdh×3 / kangaring×2 / intra_rdh×2 / single_cycle×1)
        ▼
(2) TOPOLOGY topology::findPath                @0x5cc00 (libnccom)        → enc_ring / enc_kangaring
                + sengine_get_d2d_peer_nums    @0x89fb0  (seng^2, xor $0x2)   + routing_ids (rid)
                + NeuronEdge::isIntraMLA        @0x5c430
        ▼
(3) COMPOSE  enc_metaring_primitive::__compose_{allreduce,allgather}_channel  → enc_primitive step seq
    EMIT     enc_primitive::{recv_reduce_*|direct_*}                          → per-channel SDMA descs
                → __encd_dma_common_sb2sb       @0x23e1b0
                  → {encd_dma_copy_sb2sb @0x23ebb0 | encd_dma_reduce_copy_sb2sb @0x23ec20}
                    → add_dma_packet[_cce]   (the SB2SB 0xBF leg + the in-SDMA CCE reduce)   + per-leg EVT_SEM
        ▼
(4) SERIALIZE create_spad_ctrl_entry           @0x232cd0                  → TOP_SP cc_op SPAD program
    COMPLETE  EVT_SEM local_sem/remote_sem + add_semaphore_wait_ge_and_dec @0x273a20  (counted barrier)
```

`[HIGH/OBSERVED — every symbol verified at the cited address by `nm -C` + `objdump`;
the PSEUDO encoding + the device leg CARRIED from
[all-reduce.md](../collectives/ops/all-reduce.md) §1/§8 and
[s3d3-collective.md](../collectives/ops/s3d3-collective.md).]`

**Two structural verdicts up front, both OBSERVED:**

- **AllReduce vs AllGather is byte-exact in the channel composer.** The reduce-vs-copy
  split *is* the `recv_reduce_*` vs `direct_*` primitive split, and it is exactly
  visible in the two RING composers' call census ([§5.3](#53-the-byte-exact-contrast-the-keystone)).
- **The reduce op is NOT in any composed word.** `enc_alg_type`/`algo_type` answers
  *"how to move/route"*; the per-element add/max/min rides the SDMA CCE descriptor
  (`SDMA_CCETYPE`) — *"what to compute"* ([§3.3](#33-the-cce-reduce-descriptor)). The
  `0xBF` SB2SB word and the `cc_op` SPAD entry both carry no reduce field.

---

## 1. ALGORITHM SELECTION

The PSEUDO trigger carries only `(op, dtype, src/dst handles, num_elements)` — **no
algorithm**. At load the runtime *chooses* one, keyed on topology + size + dtype + op.

### 1.1 The 5 engines live in one `enc_comm`

All five algorithm engines are sub-objects of a single `enc_comm` instance; the
runtime picks per collective op which to compose. `[HIGH/CARRIED — the engine
layout carried; the `enc_alg_type` enum that names them OBSERVED.]`

| engine | algorithm class | role |
|---|---|---|
| **ring** | `enc_alg_metaring` (`.type = RING`) | simple bidirectional ring |
| **kangaring** | `enc_alg_metaring` (`.type = KANGARING`) | kangaroo-hop latency ring (multi-hop fan-in) |
| **rdh** | `enc_alg_metaring` (`.type = RDH`) | reduce-double-half ring |
| **hier** | `enc_alg_hier` | `{intra, inter, pipeline}` composite |
| **mesh** | `enc_alg_mesh` | full / grouped / trn2 / switch mesh |

So `ring`, `kangaring`, `rdh`, and `single_cycle_ring` are all the **same**
`enc_alg_metaring` object class distinguished by a `.type` discriminator; `hier`
and `mesh` are distinct classes.

> **GOTCHA — two `metaring` discriminators, do not conflate.** The per-object
> `enc_alg_metaring.type` sub-enum (`RING=0 / KANGARING=1 / SINGLE_CYCLE_RING=2 /
> RDH=3 / INVALID=4`, CARRIED) is **not** the global
> `enc_alg_type` the selector emits and the firmware reads
> ([§1.3](#13-the-enc_alg_type-output-enum)). The global enum places
> `KANGARING=3` and `SINGLE_CYCLE_RING=4` — different ordinals. The global
> `enc_alg_type` is the authoritative one for the `cc_op.algo_type` nibble
> ([§4.3](#43-the-top_sp-cc_op-program)); the per-object `.type` is an internal
> dispatch field. A reimplementation that copies the metaring `.type` value into
> the SPAD nibble will mis-tag `KANGARING`/`SINGLE_CYCLE_RING`.
> `[the per-object sub-enum CARRIED MED (from a non-committed report); the
> global enum HIGH/OBSERVED — see the divergence note in
> [§1.3](#13-the-enc_alg_type-output-enum).]`

### 1.2 The selector — `enc_hier_primitive::__select_algorithms @0x14c620`

The selector probes capability predicates and assigns an `enc_alg_type` per **leg**.
Disassembling `0x14c620 .. 0x14de30`, the `enc_can_post_*` call census is byte-exact:

| predicate | calls | demangled arg signature (selected) |
|---|:---:|---|
| `enc_can_post_mesh_operation` | **5** | `(enc_alg_mesh*, vector<enc_data_array>, size, enc_op_type, SDMA_DTYPE, enc_comm_type, …)` |
| `enc_can_post_inter_rdh_operation` | **3** | `(enc_alg_metaring*, enc_comm_info*, vector<enc_data_array>, size)` |
| `enc_can_post_intra_rdh_operation` | **2** | `(enc_alg_mesh*, enc_comm_info*, vector<enc_data_array>, size, enc_op_type, SDMA_DTYPE, bool)` |
| `enc_can_post_kangaring_operation` | **2** | `(enc_alg_metaring*, enc_comm_info*)` |
| `enc_can_post_single_cycle_ring` | **1** | `(enc_alg_metaring*, enc_comm_info*, size, enc_op_type)` |

`enc_can_post_hierarchical_operation @0xfc130` is the outer gate. Each predicate
returns whether THAT engine is legal for the `(op, scope, size, dtype, topology)`
tuple; `__select_algorithms` picks the per-leg winner. `nm -C` reports exactly
**7** `enc_can_post_*` symbols total. `[HIGH/OBSERVED — the call census + the
demangled signatures read; matches
[all-reduce.md](../collectives/ops/all-reduce.md) §6.1 byte-for-byte. The
per-(world-size) numeric resolution depends on the `can_post` thresholds — MED,
not exhaustively enumerated.]`

> **NOTE — `intra_rdh` takes `enc_alg_mesh*`, the other metaring predicates take
> `enc_alg_metaring*`.** This is not a typo: `enc_can_post_intra_rdh_operation`
> and `enc_can_post_mesh_operation` both take the first argument
> `const enc_alg_mesh*`, while `inter_rdh`, `kangaring`, and `single_cycle_ring`
> take `const enc_alg_metaring*`. The intra-RDH leg is gated against the mesh
> object. `[HIGH/OBSERVED — the demangled symbol first-argument types.]`

### 1.3 The `enc_alg_type` output enum

Read from the `libnrt` DWARF (`DW_TAG_enumeration_type` at DIE
`<0x61ec6>`, each enumerator's `DW_AT_const_value`):

| value | enumerator | routes to (NCFW) |
|---|---|---|
| `0` | `ENC_ALG_RING` | ring channel tape |
| `1` | `ENC_ALG_HIER` | hierarchical intra+inter |
| `2` | `ENC_ALG_MESH` | mesh event tape |
| `3` | `ENC_ALG_KANGARING` | ring channel tape |
| `4` | `ENC_ALG_SINGLE_CYCLE_RING` | ring channel tape (**all-reduce only**, [§1.4](#14-per-leg-legality-for-all_reduce)) |
| `5` | `ENC_ALG_INTRA_RDH` | recursive doubling/halving |
| `6` | `ENC_ALG_SINGLE_STEP_MESH` | mesh event tape |
| `7` | `ENC_ALG_INTER_RDH` | recursive doubling/halving |
| `8` | `ENC_ALG_TWO_STEP_POD_MESH` | mesh event tape |
| `9` | `ENC_ALG_LATENCY_OPT_MESH` | mesh event tape |
| `10` | `ENC_ALG_BW_OPT_MESH` | mesh event tape |
| `11` | `ENC_ALG_INVALID` | — |

All `0..10` fit the firmware's 4-bit `algo_type` nibble. The mesh sub-type
`enc_alg_mesh_type` (firmware `algo_sub_type`, DIE `<0x125d7c>`):
`FULL_MESH=0 / GROUPED_MESH=1 / MESH_TRN2=2 / MESH_SWITCH=3 / MESH_INVALID=4`,
fitting the 3-bit `algo_sub_type`. `[HIGH/OBSERVED — DWARF `const_value` read
at `<0x61eca>`..`<0x61ef6>`; reconciles **byte-for-byte** with
[collective-enums.md](../collectives/ops/collective-enums.md) §3.3/§3.4.]`

> **CORRECTION — `INTRA_RDH=5`, `INTER_RDH=7`, not adjacent.** A naïve reading
> would place the two RDH variants at consecutive ordinals. The DWARF interleaves
> the mesh variants: `INTRA_RDH=5`, then `SINGLE_STEP_MESH=6`, then `INTER_RDH=7`.
> So the RDH pair is **5 and 7**, split by the single-step-mesh ordinal. This page
> and [collective-enums.md](../collectives/ops/collective-enums.md) agree;
> flagged here because a contiguous-RDH assumption is the natural error.
> `[HIGH/OBSERVED.]`

### 1.4 Per-leg legality for ALL_REDUCE

`__select_algorithms` constrains each leg's chosen `enc_alg_type` to a legal set,
expressed as `.rodata` C-assertion strings. A hierarchical
all-reduce logs **five** per-leg choices: *"Hier algorithm selections - alg_intra_allg
%d alg_intra_redsct %d alg_inter_allr %d alg_inter_allg %d alg_inter_redsct %d"*.

| LEG (op × scope) | legal `enc_alg_type` set |
|---|---|
| INTRA reduce-scatter | `RING \|\| KANGARING` |
| INTRA all-gather | `RING \|\| KANGARING` |
| INTER all-reduce | `RING \|\| INTER_RDH \|\| SINGLE_CYCLE_RING \|\| MESH \|\| inter_metaring` |
| INTER reduce-scatter | `RING \|\| INTER_RDH \|\| MESH \|\| inter_metaring` |
| INTER all-gather | `RING \|\| INTER_RDH \|\| MESH \|\| inter_metaring` |

The two gating facts, both OBSERVED:

- **`SINGLE_CYCLE_RING` is all-reduce-only.** `enc_can_post_single_cycle_ring @0xfaa80`
  copies its 4th argument (`enc_op_type`) into `%ebp` and guards
  `cmp $0x1,%ebp ; jne` — `op_type == 1 == ENC_ALLREDUCE` required:
  ```asm
  faa99:  mov  %ecx,%ebp          ; %ebp = op_type (the enc_op_type arg)
  faadd:  cmp  $0x1,%ebp          ; op_type == 1 == ENC_ALLREDUCE ?
  faae0:  jne  fab50              ; no → reject (return false)
  ```
  Strings `"Single Cycle ALLR"`, `"NEURON_RT_DBG_SINGLE_CYCLE_RING_ALLR_CC"`
  confirm. It is the latency-optimised single-pass ring (reduce-scatter + all-gather
  fused into one cycle). `[HIGH/OBSERVED — disasm @0xfaa80 + strings.]`
- **`KANGARING` is intra-only** (intra reduce-scatter + intra all-gather; never an
  inter leg). `enc_can_post_kangaring_operation @0xfbe70`. `[CARRIED
  [collective-enums.md](../collectives/ops/collective-enums.md) §8.]`

### 1.5 The selection inputs (what decides)

The `enc_can_post_*` signatures key the choice on:

| input | source enum / quantity |
|---|---|
| **OP** | `enc_op_type` — `ALLGATHER=0 / ALLREDUCE=1 / … / REDUCE_SCATTER=4 / …` (DIE `<0x3bbd0>`; e.g. single-cycle-ring is all-reduce-only) |
| **SIZE** | byte count vs the per-engine `{min,max}` bands (the mesh `op_{max,min}_limit[13]` is exactly one byte band per `enc_op_type`) |
| **DTYPE** | `SDMA_DTYPE` — the reducible float set `{BF16, FP16, FP32R, FP8_E3/E4/E5}` ([§3.4](#34-per-descriptor-chunking-the-size-tiling)) |
| **SCOPE** | `enc_comm_type` — `H_COMM_INTRA_ID=0 / H_COMM_INTER_ID=1` (DIE `<0x34a15>`); hier splits a collective into an intra leg + an inter leg, each separately selected |
| **TOPOLOGY** | the `NeuronEdge` graph / world-size / pod layout ([§2](#2-topology--schedule)) |

`[the predicate signatures HIGH/OBSERVED (the `nm` types); the per-input threshold
numerics MED — not exhaustively enumerated; the enum ordinals reconcile with
[collective-enums.md](../collectives/ops/collective-enums.md) §3.1/§3.2.]`

---

## 2. TOPOLOGY → SCHEDULE

The host collectives library `libnccom` builds the topology graph and routes the
collective; the per-engine composer turns the route into a per-channel step schedule.

### 2.1 The topology graph (`topo_neuron_*.cc`)

`libnccom` models the system as a graph of `NeuronNode` (a NeuronCore) connected by
`NeuronEdge` (each carrying an `EdgeLocality`). The graph builders + router are
OBSERVED at the cited addresses:

| symbol | addr | role |
|---|---|---|
| `neuronTopoCalculateGraphTRN2` | `0x75910` | wire `NeuronNode`s with `NeuronEdge`s (TRN2) |
| `neuronTopoCalculateGraphTRN3` | `0x78370` | TRN3 |
| `neuronTopoCalculateGraphTRN3SwitchV1` | `0x797b0` | TRN3 + switch |
| `NeuronEdge::isIntraMLA` | `0x5c430` | `FALSE` for `EdgeLocality::EdgeRemoteMLA` (the cross-die / cross-package C2C edge = the host name for the `io_d2d` link); `TRUE` for an on-package intra-MLA edge |
| `topology::findPath` | `0x5cc00` | `(vector<vector<int>>&, int src, int& cost, bool)` — route one collective across the edges |
| `topology::findPathRec` | `0x5cb80` | the DFS over a `std::bitset<128>` visited set + a vector-of-vectors of candidate paths |
| `topology::findPaths` | `0x5d580` | `(bitset<128>&, paths_request const&, int, int, bool&)` — the per-rank ordered path the ring/tree is built from |

So the ring **order** and the per-step **peers** are not hard-coded — they are the
output of `findPath` over the `NeuronEdge` graph for the active replica group. The
`NeuronEdge` is constructed with an `EdgeLocality` value
(`construct<NeuronEdge, int&, int&, EdgeLocality>` in the DWARF), so each edge
carries its locality (intra-MLA vs `EdgeRemoteMLA`) at construction. `[HIGH/OBSERVED
— the graph builders + `isIntraMLA` + the `findPath` family + the `EdgeLocality`
constructor read via `nm -C` + DWARF.]`

### 2.2 The cross-die peer rule + the C2C ports

`NeuronPlatform{TRN2,TRN3}::sengine_get_d2d_peer_nums @0x89fb0` computes the
cross-die peer engine as `peer = local_seng ^ 2` — byte-exact:

```asm
89fc9:  xor  $0x2,%ebx          ; peer engine = local engine ^ 2  (toggle bit 1)
```

This is the die-pairing rule (toggle bit 1 of the engine index) — the host-graph
analogue of the firmware PRID-parity role split. The TRN2 and TRN3 overrides are
**folded to the same address** (`@0x89fb0`, ICF), so both gens share the
`xor $0x2` rule. `caymanGetC2CPortsPerMla @0x757d0` / `marianaGetC2CPortsPerMla
@0x78250` expose **4** C2C (chip-to-chip) ports per MLA to the router — a subset of
the 8 physical `io_d2d` subsystems. So the topology presents 4 cross-die ports the
collective may stripe across. `[the `xor $0x2` + the C2C symbols HIGH/OBSERVED;
`=4` CARRIED (the getter returns a stack-loaded value).]`

### 2.3 The ring / kangaring leaf structs (the constructed schedule)

`findPath`'s output is materialised into the `enc_alg_metaring` leaf structs. Both
struct sizes are OBSERVED from the `libnrt` DWARF:

```c
struct enc_ring {              // byte_size 24  (DWARF DIE <0x1258fb>)
    int   prev;                // ring predecessor rank this step exchanges with
    int   next;                // ring successor rank
    int  *user_ranks;          // the rank ids the §2.1 route assigns
    bool  duplicate;
};

struct enc_kangaring {         // byte_size 1060 (DWARF DIE <0x12594d>)
    int  vnc;
    int  logical_path[256];    // DWARF: "logical_path" — the multi-hop peer sequence
    int  prev, next, port;
    int  peer_rmtv;            // DWARF: "peer_rmtv" — cross-die RDMA peer (the seng^2 peer, §2.2)
    int  peer_rmtv2;
    int  peer_local;
    int  next_peer_rmtv;       // DWARF: "next_peer_rmtv" — the skip-ahead hop
    /* … */
};
```

`enc_channel` (88 B) carries `pattern (RING0 / MESH1 / INVALID2)` + the
`net_recv`/`net_send` connectors + the on-device `drv_channel`. So the host turns
the routed path into: per-channel `{ ring prev/next OR kangaring logical_path }` +
the peer routing_ids (`enc_peer_info.rid`). `[the struct byte_sizes + the
`logical_path`/`peer_rmtv`/`next_peer_rmtv` field names HIGH/OBSERVED (DWARF);
the `prev`/`next`/`user_ranks` member layout + `enc_channel` CARRIED;
the `findPath → leaf-struct` population edge INFERRED-HIGH
from the struct fields + the composer's use of `prev`/`next`/`peer_rmtv`.]`

### 2.4 The peer selector into the SB2SB leg — `encd_neigh`

The per-step composer hands the SB2SB encoder an `encd_neigh` naming WHICH peer the
leg targets (DWARF DIE `<0x3a703>`/`<0x3a717>`):

| value | enumerator | meaning |
|---|---|---|
| `0` | `ENCD_NEIGH_LOCAL` | self-copy (`LNC1` self-test) |
| `1` | `ENCD_NEIGH_NEXT` | ring next neighbour (`enc_ring.next`) |
| `2` | `ENCD_NEIGH_PREV` | ring prev neighbour (`enc_ring.prev`) |
| `3` | `ENCD_NEIGH_GATEWAY` | |
| `4` | `ENCD_NEIGH_PEER_RMTV` | cross-die `LNC2` partner (`enc_kangaring.peer_rmtv`) |
| `5` | `ENCD_NEIGH_PEER_RMTV2` | second cross-die peer |
| `6` | `ENCD_NEIGH_PEER_LOCAL` | `LNC2`-partner peer |
| `7` | `ENCD_NEIGH_NEXT_PEER_RMTV` | the skip-ahead kangaroo hop |
| `8` | `ENCD_NEIGH_INVALID` | `== ENCD_NEIGH_NUM` (count) |

The encoder resolves `encd_neigh` → a concrete src/dst SoC address + the routing_id
via `get_neighbor_metaring @0x235e40` / `get_port_to_neighbor_for_metaring @0x2329e0`
and `set_addr_routing_bits @0x231340`. So the topology's `prev`/`next`/`peer_rmtv`
become an `encd_neigh` per step, which becomes a routed SoC dst address per SB2SB
leg. `[ordinals HIGH/OBSERVED — reconcile byte-for-byte with
[collective-enums.md](../collectives/ops/collective-enums.md) §3.7 and
[s3d3-collective.md](../collectives/ops/s3d3-collective.md) §4.4; the resolver
symbols OBSERVED.]`

---

## 3. PER-STEP SDMA DESCRIPTOR GENERATION

Each composer turns the routed ring/mesh into a SEQUENCE of `enc_primitive` STEP
calls; each step is one SB2SB leg, emitted as per-channel SDMA descriptors.

### 3.1 The step primitives (the channel composer's vocabulary)

`enc_primitive` carries two families (signatures re-read from `nm -C`):

```c
// COPY steps (no reduce) — carry NEITHER SDMA_CCETYPE NOR reduction_type_t
enc_primitive::direct_recv_send(enc_half_chunk_index, bool×6)        @0x16f820
enc_primitive::direct_copy_send(enc_half_chunk_index, bool×4)        @0x16f710
enc_primitive::direct_recv(enc_half_chunk_index, bool×4)             @0x158540
enc_primitive::direct_copy(enc_half_chunk_index)                     @0x148470
enc_primitive::direct_pull(enc_half_chunk_index)
enc_primitive::send(enc_half_chunk_index, bool)                      @0x16c960
enc_primitive::post_recv(int)                                        @0x149570

// REDUCE steps (CCE reduce in the SDMA) — ALWAYS carry SDMA_CCETYPE + reduction_type_t
enc_primitive::recv_reduce_send(enc_half_chunk_index, SDMA_CCETYPE,
                                reduction_type_t, bool, bool)        @0x16ad70
enc_primitive::recv_reduce_copy(enc_half_chunk_index, SDMA_CCETYPE,
                                reduction_type_t, vector<encd_neigh>, bool)  @0x16b030
enc_primitive::recv_reduce_copy_send(…, SDMA_CCETYPE, reduction_type_t, bool, bool)  @0x16aed0
enc_primitive::__recv_reduce_write(…, SDMA_CCETYPE, reduction_type_t,
                                   vector<encd_neigh>, bool, bool)   @0x16a0a0
enc_primitive::direct_reduce_send_kangaring(enc_half_chunk_index, SDMA_CCETYPE)  @0x158120
```

The reduce family ALWAYS takes the op (`SDMA_CCETYPE`) + the topology
(`reduction_type_t`, [§3.3](#33-the-cce-reduce-descriptor)); the copy family takes
neither. The `enc_half_chunk_index` first argument is itself an enum
(`CHUNK_H0=0 / CHUNK_H1=1 / ENC_CHUNK_SPLIT_N=2`, DWARF DIE `<0x60c2b3>`) — a step
operates on the first or second half-chunk of the rank's buffer. `[HIGH/OBSERVED —
the `nm -C` signatures (the mangled `…20enc_half_chunk_index12SDMA_CCETYPE…`
confirms `SDMA_CCETYPE` is the 2nd arg and `reduction_type_t` the 3rd);
`__recv_reduce_write` CARRIED [cce-in-transfer.md](../dma/cce-in-transfer.md) §8.]`

### 3.2 The step → SB2SB `0xBF` leg → SDMA descriptor

Every step bottoms out in the host SB2SB encoder family (addresses re-confirmed by
`nm`):

```c
__encd_dma_common_sb2sb @0x23e1b0   // loops the active channels; per channel branches on reduce_mode:
  reduce_mode == 0 (COPY):
      encd_dma_copy_sb2sb        @0x23ebb0   // xor edx,edx → reduce_mode=0
        → __encd_dma_copy        @0x238c80
          → add_dma_packet                   // plain DDMA; SDMAOP CME=2; SDMA_CMETYPE COPY
  reduce_mode == 1 (REDUCE):
      encd_dma_reduce_copy_sb2sb @0x23ec20   // alloca per-channel op array, mov edx,0x1 → reduce_mode=1
        → __encd_dma_reduce_copy @0x23e070
          → add_dma_packet_cce   @0x2307d0    // the CCE compute descriptor; SDMAOP CCE=4
```

The two wrappers are byte-distinguished by their `reduce_mode` selector
(`xor edx,edx` for COPY at `@0x23ebbf`-area vs `mov edx,0x1` for REDUCE, plus the
reduce wrapper's per-channel op-array `alloca` and `r8d=0x8` positional default —
OBSERVED in
[s3d3-collective.md](../collectives/ops/s3d3-collective.md) §4.2). The **physical**
device instruction is the `0xBF` `S3D3_COLLECTIVE` word (64 B, one 3-D SRC
`TENSOR3D` + one 3-D DST `TENSOR3D`) — a real HW op on the POOL engine; the device
decodes it via `decode_sb2sb_collective` (`libnrtucode_extisa.so`, string
*"SB2SB_Collective: num_chans=%0d, tpb_idx=%u"*). The `0xBF` word itself carries
**no op field** — the reduce lives in the SDMA CCE descriptor the leg programs.
`num_active_channels` (`1..128`, `POOLING_NUM_CHANNELS`) = the channel count the
`__encd_dma_common_sb2sb` loop walks. `[encoder addresses HIGH/OBSERVED;
the `0xBF` word + the COPY/REDUCE encoder split CARRIED
[s3d3-collective.md](../collectives/ops/s3d3-collective.md) §4 + the
[CCE page](../dma/cce-in-transfer.md) §6.]`

### 3.3 The CCE reduce descriptor + `reduction_type_t`

`add_dma_packet_cce @0x2307d0` builds the 16-byte M2S compute descriptor: it reads
`N` neighbour source streams, applies the op (`SDMA_CCETYPE`) per element **while in
flight**, and writes the single reduced chunk down the S2M leg — the reduce is in
the SDMA datapath (the CCE block), with no Q7 FMA loop. The descriptor fields:

- **op** rides `SDMA_CCETYPE` (DWARF `<0x337bd>`): `ADD=0 / FMA=1 / MAX=2 / MIN=3 /
  EXT=4 / GCE=5`. Only `{ADD=0, MAX=2, MIN=3}` reach the HW descriptor (the ISA
  `CCE_OP` subset); `FMA=1` carries an extra scale operand; `EXT=4`/`GCE=5` are
  device-only fused/compression modes the `kbin_cce_op_to_sdma_cce_op` 4-entry map
  can never emit.
- **dtype** rides `SDMA_DTYPE` (verbatim, no re-encoding).
- **topology** rides `reduction_type_t` — the 3rd argument of `recv_reduce_*`,
  recovered from the `libnrt` DWARF at DIE `<0x60c99b>` (linkage
  `N13enc_primitive16reduction_type_tE`):

| value | enumerator | meaning |
|---|---|---|
| `0` | `RING_2R1W` | ring reduce step: READS 2 chunks (neighbour + own), WRITES 1 reduced chunk (the standard reduce-scatter step, `num_sources=2`) |
| `1` | `RING_2R2W` | the 2-read / 2-write double form (bidirectional / double-buffered) |
| `2` | `KANGARING_NR1W` | the kangaroo N-read / 1-write fan-in (multiple peers reduced into 1) |

So a reduce step is fully described by `(SDMA_CCETYPE op, reduction_type_t topology,
vector<encd_neigh> source peers)`. `add_dma_packet_cce` sets `num_sources` from the
peer vector, the op sub-selector from `SDMA_CCETYPE`, the dtype nibbles from
`SDMA_DTYPE`, and marks the **last** source's meta-ctrl `last` bit (bit 7) to flush
the accumulator. The meta-ctrl word's bit 25 (`0x2000000`) is the *"CCE meta-ctrl
valid"* flag; the three dtype nibbles (`in [11:8]`, `out [15:12]`, `accum [19:16]`)
implement the mixed-precision reduce (read low-precision, accumulate FP32, write
low-precision). `[the `reduction_type_t` enumerators HIGH/OBSERVED (DWARF
`const_value 0/1/2` at `<0x60c99c>`/`<0x60c9a2>`/`<0x60c9a8>`); the 2R1W/2R2W/NR1W
read-write reading INFERRED-HIGH from the names + the `num_sources` model; the CCE
descriptor bit layout CARRIED [cce-in-transfer.md](../dma/cce-in-transfer.md)
§2/§3/§4.]`

> **NOTE — `reduction_type_t` ≠ `SDMA_CCETYPE`.** They are distinct enums on
> distinct arguments of the same `recv_reduce_*` signature. `reduction_type_t`
> selects the ring read/write *rendezvous shape*; `SDMA_CCETYPE` selects the
> *add/max/min* op. A reimplementation that copies one into the other's descriptor
> slot mis-encodes the step.
> `[HIGH/OBSERVED — the two enums isolated in the DWARF; matches
> [collective-enums.md](../collectives/ops/collective-enums.md) §3.5 and
> [all-reduce.md](../collectives/ops/all-reduce.md) §4c.]`

### 3.4 Per-descriptor chunking (the size tiling)

The collective buffer is tiled per descriptor:
`encd_get_cce_reduce_packet_size @0x2396f0 = min(sdma_data_type_size[dtype]*N,
cc_cce_reduce_prio_pkt_size[priority_class]) & ~0x1F` — 32-byte-granular,
QoS-capped per priority class. `aws_hal_get_sdma_max_cce_elements @0x44be60` caps
the per-descriptor element count (`2048` on v3/v4, `1024` on v2). The `0xBF` word
itself caps at **256 elements** per S3D3 leg (`t3d_element_count <= 256`), so larger
buffers are tiled across multiple S3D3 legs AND across the `num_active_channels`
channels. The runtime log exposes the slicing: *"allreduce: … channel_n(%d) pipeline
slice_n(%d) … copy_slice_sz:0x%lx reduce_slice_sz:0x%lx"* — the all-reduce is split
into pipeline SLICES, each with a `copy_slice` (all-gather bytes) + a `reduce_slice`
(reduce-scatter bytes). `[the packet-size + element-cap functions CARRIED
[cce-in-transfer.md](../dma/cce-in-transfer.md) §7.1 + the 256-element cap CARRIED
[s3d3-collective.md](../collectives/ops/s3d3-collective.md) §3.4; the log string
OBSERVED.]`

> **GOTCHA — `fp32r` (`0xB`) has SDMA byte-size 0.** `sdma_data_type_size[fp32r]`
> reads zero, and `encd_get_cce_reduce_packet_size` asserts (line `0x1204`) when
> the size is 0 — so a CCE reduce that asks for `fp32r` *byte-sizing* traps.
> `fp32r` rides the **accumulation** nibble (the wide internal type), never the I/O
> byte-length nibble. The CCE-reducible set is `{BF16, FP16, FP32R, FP8_E3/E4/E5}`
> (the `cce_dtypes` seed table @`0x9b9f40`) — plain `FP32 (0xA)` and the integer
> dtypes are absent.
> `[CARRIED [cce-in-transfer.md](../dma/cce-in-transfer.md) §1.2/§7.1 +
> [all-reduce.md](../collectives/ops/all-reduce.md) §5.]`

---

## 4. SEMAPHORE / COMPLETION WIRING

The ring steps are ORDERED by the `EVT_SEM` two-semaphore protocol; the TOP_SP
`cc_op` program chains them with a counted barrier.

### 4.1 The per-leg two semaphores

Each SB2SB leg's `rdma_desc_gen` pushes TWO extra semaphore-increment BDs into the
ring:

- **LOCAL sem (source release):** `EVT_SEM.inc(local_sem)` on THIS core when the
  local DMA engine finishes → *"source memory can be released / overwritten"* (all
  16 DMA engines bump it).
- **REMOTE sem (data-ready):** `EVT_SEM.inc(remote_sem)` on the PEER, ROUTED by
  `routing_id` over `io_d2d`, when all bytes land → *"dst buffer ready to read"*.

The `EVT_SEM` aperture (the TOP_SP-hosted 256-semaphore unit, 4-B stride): base
`0x8280000000` + windows **`+0x1000` read / `+0x1400` set / `+0x1800` inc / `+0x1C00`
dec**. The encoder allocates the per-leg sema (`alloc_sema_value @0x231580`) and
programs the inc offset (`encd_arch_get_sp_sema_i_ofst @0x2558e0` → `+0x1800`).
`[the four windows + the `0x8280000000` base CARRIED
[top-sp-lowering.md](../collectives/ops/top-sp-lowering.md) §4 (byte-exact:
read `+0x1000`, set `+0x1400`, inc `+0x1800`, dec `+0x1C00`); the per-leg LOCAL/REMOTE
two-sema CARRIED; the encoder symbols OBSERVED.]`

> **CORRECTION (verification) — the EVT_SEM windows match the committed pages
> exactly.** This page's windows (`+0x1000`/`+0x1400`/`+0x1800`/`+0x1C00`) are
> **byte-identical** to [top-sp-lowering.md](../collectives/ops/top-sp-lowering.md)
> §4 and [all-reduce.md](../collectives/ops/all-reduce.md) §8 (`sp_base + 0x1800 +
> idx*4`). No divergence found. `[HIGH/OBSERVED — cross-checked.]`

### 4.2 The step chaining (the counted barrier)

The NEXT step's `rdma_desc_start` on the peer WAITS its recv-side semaphore `>=`
target before issuing, then atomically decrements it — the NCFW counted barrier.
Both halves are the device `EVT_SEM` TPB op `0x10A0`:

| host symbol | addr | device op | subop | role |
|---|---|---|---|---|
| `add_semaphore_inc` | `0x273860` | `0x10A0` | 21 | ARRIVE: write `SEMAPHORE_INC[i]` (`+0x1800`) |
| `add_semaphore_wait_ge_and_dec` | `0x273a20` | `0x10A0` | 20 | poll `SEMAPHORE_READ[i]` (`+0x1000`) until `>= target`, then write `SEMAPHORE_DEC[i]` (`+0x1C00`) |

So the order is: step *k*'s REMOTE sem inc (data landed at peer) → step *k+1*'s
`wait_ge_and_dec` gate → step *k+1* issues. This chains the *N−1* reduce-scatter
steps then the *N−1* all-gather steps of a ring. The `0xBF` word's own `events`
field (`wait_mode`/`wait_idx` vs `update_mode`/`update_idx` + `semaphore_value`) is
the per-instruction end of this — an SB2SB leg waits its inbound sem and updates the
peer's on completion. Underneath, each 16-B BD carries a 2-bit generation tag
busy-polled before slot reuse. `[the `add_semaphore_*` symbols HIGH/OBSERVED;
the device op `0x10A0` subop 20/21 + the read/inc/dec window targets CARRIED
[ring-kangaring.md](../collectives/ncfw/ring-kangaring.md) §6 +
[collective-end-to-end.md](../orientation/collective-end-to-end.md) §14; the
`events`-field chaining CARRIED [s3d3-collective.md](../collectives/ops/s3d3-collective.md) §6.]`

### 4.3 The TOP_SP `cc_op` program

The composed schedule is serialised into the TOP_SP CTRL SPAD (4 KiB → SRAM) `cc_op`
command table by `create_spad_ctrl_entry @0x232cd0`. The 8-byte entry:

```c
typedef struct {                 // spad_ctrl_entry  (size 8)
    struct { uint8_t cc_op:1;    //  byte0 bit0  — "active cc-op" flag (NRT asserts == 1)
             uint8_t :7; } header;
    struct {                     // spad_ctrl_cc_op_entry  (size 7, at +1)
        uint8_t algo_type:4;     //  byte1 [0:3]  — low nibble of enc_alg_type (§1.3)
        uint8_t algo_sub_type:3; //  byte1 [4:6]  — enc_alg_mesh_type (§1.3)
        uint8_t trigger_next:1;  //  byte1 [7]    — chain to next entry
        uint8_t reporter:1;      //  byte2 [0]    — this TOP_SP posts completion
        uint8_t ring_wait_complete:1;  // byte2 [1]
        uint8_t ring_send_complete:1;  // byte2 [2]
        uint8_t :5;
        uint8_t  __reserved;     //  byte3
        union {                  //  bytes 4..7
            uint32_t channel_list;                          // RING family: 32-channel bitmask
            struct { uint16_t sema_shift_offset, sema_mask; }; // MESH: indexes the event-tape window
        };
    } cc_op;
} spad_ctrl_entry;
```

The packer's word bits map 1:1 onto these bitfields
(`(trigger_next << 15) | (algo_sub_type << 12) | (algo_type << 8) | 1` for the
header word). The single `algo_type` switch binds a spad entry to an algorithm:
`RING / KANGARING / SINGLE_CYCLE_RING` → NCFW ring channel tape; `MESH / *_MESH` →
NCFW mesh event tape (with the `sema_shift_offset`/`sema_mask` overlay);
`HIER / *_RDH` → NCFW hierarchical intra+inter legs. The per-entry "mark" log:
*"CTRL mark -alg %d-subalg %d-trignext %d-chlist %d-reporter %d-sema_shift_offset
%u-sema_mask %u"*. The per-slot DMA steps are the `tp_inc_steps` list:
*"tp_inc_steps[%d] = m2s %d, s2m %d, repeat %d"* — the M2S(TX) + S2M(RX)
tail-pointer increment counts the TOP_SP issues per step.

So a **HIER** all-reduce becomes MULTIPLE chained `cc_op` entries (intra-RS ring leg,
inter all-reduce mesh/ring leg, intra-AG ring leg), each `algo_type`-tagged, chained
by `trigger_next`/`complete_prev`, built by `encd_populate_metaring_topsp_config
@0x240bf0` / `encd_populate_mesh_topsp_config @0x240330`. The TOP_SP NX core walks
them, per-step incrementing the `EVT_SEM` the DMA legs also bump. `[the `cc_op`
struct + packer + the config builders CARRIED
[top-sp-lowering.md](../collectives/ops/top-sp-lowering.md) §5; the symbols
`create_spad_ctrl_entry @0x232cd0`, `encd_populate_{metaring,mesh}_topsp_config`
re-confirmed by `nm`.]`

### 4.4 The leader / reporter completion

One TOP_SP per VNC is elected leader (`encd_get_topsp_is_leader @0x253420`) and posts
completion (`cc_op.reporter`); `enc_barrier` is gated by *"received clearance from
top_sp"*. The host `enc_proxy_task` workers drive the HOST half (net send/recv +
barriers) in parallel via the per-stream `barrier_done_semaphore_inc_addrs[4]`. So
completion is: per-leg `EVT_SEM` ([§4.1](#41-the-per-leg-two-semaphores)) → step
chaining ([§4.2](#42-the-step-chaining-the-counted-barrier)) → the leader TOP_SP's
reporter post → the host proxy barrier join. `[CARRIED
[top-sp-lowering.md](../collectives/ops/top-sp-lowering.md) §6b.]`

---

## 5. THE TWO WORKED LOWERINGS

### 5.1 AllReduce (RING) — the full lowering

**FRONTEND.** `nki.isa.all_reduce` → BIR `InstCollective(ctype = ALL_REDUCE)`. The
reduce op rides `ALU_OP` (`ADD=0x04 / MAX=0x08 / MIN=0x09`); dtype rides `DTYPE`
(`BF16=6 / FP16=7 / FP32R=0xB / FP8_E3..5=0xD..0xF`).

**ISEL.** `transformAllReduceOp` + `codegenTiledCCOp*` → a PSEUDO trigger:
`0xC7 PSEUDO_TRIGGER_ALL_REDUCE` (compact) / `0xC8 PSEUDO_TRIGGER_COLLECTIVE`
(`ctype = ALL_REDUCE`) / `0xD9 PSEUDO_TRIGGER_COLLECTIVE2` (SBUF operands). Upper
three bits `0b110` ⇒ PSEUDO ⇒ never decoded by HW; NRT rewrites at load.

**RUNTIME SELECT** ([§1](#1-algorithm-selection)). `__select_algorithms @0x14c620`
→ `enc_alg_type` per leg. Single-node intra-only all-reduce: typically `RING` (or
`SINGLE_CYCLE_RING` for latency, all-reduce-only). Multi-node: `HIER` (intra RS ring
→ inter all-reduce mesh/ring → intra AG ring), via `__compose_allreduce @0x1a34c0` +
`__build_allreduce_pgt_{intra_rdsc @0x1558f0, inter_allr @0x14ff80, intra_allg
@0x155260}`, all OBSERVED.

**RUNTIME COMPOSE** ([§3](#3-per-step-sdma-descriptor-generation)). For RING:
`enc_metaring_primitive::__compose_allreduce_channel @0x171600`. Its call census,
disassembled `0x171600 .. 0x1726f0`:

```text
REDUCE-SCATTER phase  (each step: SDMA_CCETYPE op + reduction_type_t RING_2R1W → add_dma_packet_cce)
   recv_reduce_send       ×2   @0x16ad70
   recv_reduce_copy       ×2   @0x16b030
   recv_reduce_copy_send  ×2   @0x16aed0
ALL-GATHER phase      (pure copy rotation; add_dma_packet, no CCE)
   direct_recv            ×2   @0x158540
   direct_recv_send       ×2   @0x16f820
   direct_copy_send       ×2   @0x16f710
   send                   ×2   @0x16c960
   post_recv              ×2   @0x149570
```

After *N−1* reduce steps each rank owns one fully-reduced chunk; the *N−1* copy
steps rotate the reduced chunks around the ring.

**DEVICE.** Each step is a `0xBF` SB2SB leg = `rdma_desc_gen(op8)` +
`rdma_desc_start(op9)`; the reduce-scatter legs carry the CCE reduce descriptor, the
all-gather legs a plain copy. Steps chained by the `EVT_SEM` two-semaphore counted
barrier ([§4](#4-semaphore--completion-wiring)). The TOP_SP NX core walks the `cc_op`
SPAD program (`algo_type = RING`) issuing the m2s/s2m tail increments. `[the channel
census HIGH/OBSERVED (objdump @0x171600); the chain HIGH/OBSERVED at each
named hop; the device leg CARRIED
[all-reduce.md](../collectives/ops/all-reduce.md) §6/§8.]`

### 5.2 AllGather (RING) — the full lowering

**FRONTEND.** BIR `InstCollective(ctype = ALL_GATHER = 0x3)` → the same `0xC8`/`0xD9`
PSEUDO carrier (`ctype` field = 0x3) — **no reduce op** (all-gather is copy-only; the
`ALU_OP` field is unused). `enc_op_type = ENC_ALLGATHER (0)`.

**RUNTIME SELECT.** `__select_algorithms` → `RING | KANGARING` (intra) or a mesh/hier
inter leg; `SINGLE_CYCLE_RING` is NOT legal (all-reduce-only,
[§1.4](#14-per-leg-legality-for-all_reduce)). Multi-node: `HIER` via
`__compose_allgather @0x1a0f10` + `__build_allgather_pgt_{intra_allg @0x14de30,
inter_allg @0x14ee00}` (OBSERVED).

**RUNTIME COMPOSE.** `enc_metaring_primitive::__compose_allgather_channel @0x16f940`.
Its call census, disassembled `0x16f940 .. 0x171600`:

```text
direct_recv_send  ×6   direct_copy_send  ×6   direct_recv  ×6
direct_copy       ×4   direct_pull       ×2   send         ×2   post_recv ×3
   ── ZERO recv_reduce_* calls (objdump `rg -c recv_reduce` over the range = 0) ──
```

i.e. PURE COPY ring rotation: each step receives a neighbour's chunk and forwards
its own, with NO element-wise reduce. After *N−1* steps every rank holds all *N*
chunks.

**DEVICE.** Each step is a `0xBF` SB2SB COPY leg (`encd_dma_copy_sb2sb` →
`add_dma_packet`, SDMAOP CME=2, no CCE). Steps chained by the same `EVT_SEM`
two-semaphore barrier; `cc_op` SPAD `algo_type = RING`. The runtime log confirms the
copy-only nature: *"allgather: comm(%p), alg(%d), dtype(%d), channel_n(%d) …
copy_slice_sz:0x%lx"* — `copy_slice_sz` ONLY, NO `reduce_slice_sz` (contrast §5.1's
allreduce log which carries BOTH). `[HIGH/OBSERVED — the channel census
(`recv_reduce` count = 0 over the range) + the log string.]`

### 5.3 The byte-exact contrast (the keystone)

The two RING composers' call census is the cleanest possible proof of the
reduce-vs-copy split:

| composer | reduce-scatter primitives | all-gather primitives |
|---|---|---|
| `__compose_allreduce_channel @0x171600` | `recv_reduce_send` ×2, `recv_reduce_copy_send` ×2, `recv_reduce_copy` ×2 \[SDMA_CCETYPE + reduction_type_t → `add_dma_packet_cce`, CCE\] | `direct_recv` ×2, `direct_recv_send` ×2, `direct_copy_send` ×2, `send` ×2, `post_recv` ×2 \[pure copy, `add_dma_packet`, NO CCE\] |
| `__compose_allgather_channel @0x16f940` | *(none — there is no reduce phase; all-gather is the ENTIRE op)* | `direct_recv_send` ×6, `direct_copy_send` ×6, `direct_recv` ×6, `direct_copy` ×4, `direct_pull` ×2, `send` ×2, `post_recv` ×3 \[ALL copy, NO `recv_reduce_*`\] |

⇒ **AllReduce = reduce-scatter(CCE) + all-gather(copy). AllGather = all-gather(copy)
only.** The `recv_reduce_*` vs `direct_*` split IS the reduce-vs-copy distinction,
and it is byte-exact in the channel composer. The runtime logs corroborate:
`"allreduce:"` carries BOTH `copy_slice_sz` AND `reduce_slice_sz`; `"allgather:"`
carries `copy_slice_sz` ONLY. `[HIGH/OBSERVED — the objdump call census of both
composers + the log strings.]`

### 5.4 The HIER / MESH / KANGARING variants

| form | composer (OBSERVED symbols) | step primitives |
|---|---|---|
| **HIER all-reduce** | `__compose_allreduce @0x1a34c0` + `__build_allreduce_pgt_intra_rdsc → inter_allr → intra_allg` | intra reduce-scatter (ring) → inter all-reduce (mesh/ring) → intra all-gather (ring); FIVE per-leg algos ([§1.4](#14-per-leg-legality-for-all_reduce)) |
| **HIER all-gather** | `__compose_allgather @0x1a0f10` + `__build_allgather_pgt_intra_allg → inter_allg` | intra all-gather → inter all-gather |
| **MESH all-reduce** | `__compose_allreduce_full_mesh @0x19ce00` (+ `_t0 @0x1595f0` / `_t1 @0x185890` two-step) + `__compose_allreduce_grouped_mesh @0x199ea0` + `__compose_allreduce_trn2 @0x190820` | reduce/copy/broadcast handshakes (`alg_full_mesh_reduce_*`); the mesh reduce leg is `encd_mesh_reduce_sb2sb @0x23b6a0 → add_dma_packet_cce` |
| **MESH all-gather** | `__compose_allgather_full_mesh @0x184550` / `_trn2 @0x18aeb0` / `_trn2_2dev @0x187f60` / `_trn2_generic @0x188f90` | copy-only (`encd_mesh_memcopy_sb2sb @0x239840`) |
| **KANGARING reduce-scatter** | `__compose_redsct_channel_kangaring @0x16b300` (the `__compose_allreduce_channel_kangaring @0x1726f0` outer) | `direct_reduce_send_kangaring @0x158120` + `KANGARING_NR1W` reduce |
| **KANGARING all-gather** | `__compose_allgather_channel_kangaring @0x1708c0` | copy |
| **SINGLE_CYCLE** | `__compose_single_cycle_allreduce_channel @0x16cdc0` | all-reduce-only, the latency-optimised single-pass ring |

> **NOTE — the byte-exact ring-vs-kangaring fork.** The plain-ring reduce-scatter
> composer `__compose_redsct_channel @0x16d800` emits `xor ecx,ecx` (= `RING_2R1W=0`)
> before every reduce call; the kangaring composer `__compose_redsct_channel_kangaring
> @0x16b300` emits `mov ecx,0x2` (= `KANGARING_NR1W=2`). So the *single instruction*
> `xor ecx,ecx` vs `mov ecx,0x2` before the `recv_reduce_*` call is the decisive
> ring-vs-kangaring signal — the same `reduction_type_t` enum
> ([§3.3](#33-the-cce-reduce-descriptor)) flowing into the descriptor. `[CARRIED
> [ring-kangaring.md](../collectives/ncfw/ring-kangaring.md) §5.3, re-anchored here
> as the COMPOSE-phase evidence for the per-step topology selector.]`

`[the composer symbols HIGH/OBSERVED at the cited addresses; the per-leg
decomposition CARRIED [all-reduce.md](../collectives/ops/all-reduce.md) §6.2 +
[ring-kangaring.md](../collectives/ncfw/ring-kangaring.md) /
[mesh-collective.md](../collectives/ncfw/mesh-collective.md) /
[hierarchical-collective.md](../collectives/ncfw/hierarchical-collective.md).]`

---

## 6. The end-to-end lowering map

| stage | artifact / function | output |
|---|---|---|
| FRONTEND (compiler) | `nki.isa.all_reduce`/`all_gather` → BIR `InstCollective` | `InstCollective(ctype)` + op(`ALU_OP`) + dtype |
| ISEL (SundaISel) | `transformAllReduce/AllGatherOp` → `codegenTiledCCOp*` | PSEUDO `0xC7`/`0xC8`/`0xD9` in NEFF (TOP_SP) |
| NRT SELECT (load) | `enc_hier_primitive::__select_algorithms @0x14c620` + `enc_can_post_*` (§1) | `enc_alg_type` per leg (RING/HIER/MESH/…) |
| NRT TOPOLOGY (load) | `topo_neuron findPath @0x5cc00` + `seng^2 @0x89fb0` + `isIntraMLA @0x5c430` (§2) | `enc_ring`/`enc_kangaring` + routing_ids (rid) |
| NRT COMPOSE (load) | `enc_*_primitive::__compose_*_channel` (§3/§5) | `enc_primitive` step sequence (reduce/copy) |
| NRT EMIT (load) | `{recv_reduce_*\|direct_*}` → `__encd_dma_common_sb2sb @0x23e1b0` → `encd_dma_{copy @0x23ebb0\|reduce_copy @0x23ec20}` → `add_dma_packet[_cce]` (§3) | per-channel SDMA descriptors + per-leg `EVT_SEM` |
| NRT SERIALIZE (load) | `create_spad_ctrl_entry @0x232cd0` (§4.3) | TOP_SP `cc_op` SPAD program (CTRL+SLOT) |
| HOST TRIGGER (run) | `encd_start_executable @0x2431c0` → `set_host_trigger` → `LOCAL_REG+0x15a0` (= `0x615a0`) = 1 | TOP_SP NX program starts |
| DEVICE EXECUTE (run) | TOP_SP walks `cc_op` → `tp_inc_steps` m2s/s2m tail increments → POOL/Q7 `decode_sb2sb_collective` → `rdma_desc_gen(op8)`/`start(op9)` | `0xBF` SB2SB legs + CCE reduce in SDMA |
| DEVICE COMPLETE (run) | `EVT_SEM` local_sem/remote_sem + `add_semaphore_wait_ge_and_dec @0x273a20` (§4) | step *k* → step *k+1* chained; leader posts |

`[every named function OBSERVED at the cited address (observed here or CARRIED); the
ISEL transform names CARRIED [sundaisel.md](sundaisel.md); the device-execute leg
CARRIED [collective-end-to-end.md](../orientation/collective-end-to-end.md) §10-13.
Sync insertion (the ordering of these legs against the rest of the engine schedule)
is covered on [opt-sync-insertion.md](opt-sync-insertion.md).]`

---

## 7. Reimplementer's checklist (byte-grounded)

- **Recognise** the pseudo trigger at load (`0xC7`/`0xC8`/`0xD9`+`0xDA`; upper 3
  opcode bits `0b110`). It carries `(op, dtype, src/dst, num_elements)` — no
  algorithm.
- **SELECT** an `enc_alg_type` per leg from `{RING, KANGARING}` (intra) and
  `{RING, INTER_RDH, SINGLE_CYCLE_RING, MESH}` (inter all-reduce) via the
  `enc_can_post_*` predicates; `SINGLE_CYCLE_RING` is all-reduce-only (the
  `op_type==1` guard); `KANGARING` is intra-only. Use the **global** `enc_alg_type`
  ordinals (`RING=0 / HIER=1 / MESH=2 / KANGARING=3 / SINGLE_CYCLE_RING=4 /
  INTRA_RDH=5 / INTER_RDH=7 / …`) for the `cc_op.algo_type` nibble — not the
  per-object metaring `.type`.
- **ROUTE** with `findPath` over the `NeuronEdge` graph; cross-die peer =
  `local_seng ^ 2`; 4 C2C ports per MLA. Materialise into `enc_ring` (24 B) /
  `enc_kangaring` (1060 B) + the `encd_neigh` peer selector per step.
- **COMPOSE** RING all-reduce as `recv_reduce_send`/`recv_reduce_copy_send`/
  `recv_reduce_copy` (reduce-scatter, `RING_2R1W`) + `direct_recv_send`/`direct_recv`/
  `send` (all-gather); all-gather as copy-only. HIER as
  `intra_rdsc → inter_allr → intra_allg`.
- **EMIT** per channel via `__encd_dma_common_sb2sb` → copy (`add_dma_packet`,
  SDMAOP CME=2) or reduce (`add_dma_packet_cce`, SDMAOP CCE=4); the reduce op is
  `SDMA_CCETYPE` (`{ADD0, MAX2, MIN3}` to HW), the ring shape is `reduction_type_t`
  — two **distinct** descriptor fields. Restrict the CCE reduce to
  `{BF16, FP16, FP32R, FP8}`; never plain `FP32` or integers.
- **SERIALIZE** into a `cc_op` SPAD table (byte0 `.cc_op=1`, `algo_type` nibble,
  `trigger_next`); chain a HIER all-reduce as multiple entries.
- **WIRE** completion with the two `EVT_SEM` semaphores (local source-release,
  remote data-ready) at base `0x8280000000` + windows
  `+0x1000`/`+0x1400`/`+0x1800`/`+0x1C00`; chain steps via `add_semaphore_inc`
  (subop 21) → `add_semaphore_wait_ge_and_dec` (subop 20).

---

## 8. Adversarial self-verification (the 5 strongest claims)

1. **`__compose_allreduce_channel @0x171600` = reduce-scatter (`recv_reduce_*`) +
   all-gather (`direct_*`); `__compose_allgather_channel @0x16f940` = copy only,
   ZERO `recv_reduce_*`.** *Challenge:* could the allgather composer call a reduce
   primitive I missed? *Test:* `objdump -d --start-address=0x16f940
   --stop-address=0x171600 | rg -c recv_reduce` = **0**; the full `call` census over
   the range is `direct_recv_send`×6, `direct_copy_send`×6, `direct_recv`×6,
   `direct_copy`×4, `direct_pull`×2, `send`×2, `post_recv`×3 — no reduce symbol. The
   allreduce range `0x171600..0x1726f0` shows `recv_reduce_send`×2,
   `recv_reduce_copy`×2, `recv_reduce_copy_send`×2 plus the copy phase. *Verdict:*
   HOLDS, OBSERVED.

2. **`reduction_type_t = {RING_2R1W=0, RING_2R2W=1, KANGARING_NR1W=2}` and it is the
   3rd argument of `recv_reduce_send`, with `SDMA_CCETYPE` the 2nd.** *Challenge:*
   off-by-one — is `reduction_type_t` actually the 2nd argument (as one sibling
   report claimed)? *Test:* the DWARF at DIE `<0x60c99b>` reads `const_value 0/1/2`
   for the three names; the demangled `nm -C` signature is
   `recv_reduce_send(enc_half_chunk_index, SDMA_CCETYPE, reduction_type_t, bool,
   bool)` (`@0x16ad70`) — `SDMA_CCETYPE` is the 2nd type, `reduction_type_t` the 3rd.
   *Verdict:* HOLDS — matches
   [collective-enums.md](../collectives/ops/collective-enums.md) §3.5's explicit
   "3rd argument" CORRECTION; the "2nd argument" reading is the off-by-one error.

3. **`enc_can_post_single_cycle_ring @0xfaa80` requires `op_type == 1 ==
   ENC_ALLREDUCE`.** *Challenge:* is `%ebp` really the `enc_op_type` argument, or a
   loop counter? *Test:* `objdump @0xfaa80` shows `mov %ecx,%ebp` @`0xfaa99` (the 4th
   arg, `enc_op_type`, arrives in `%ecx` → `%ebp`) then `cmp $0x1,%ebp ; jne fab50`
   @`0xfaadd`/`0xfaae0`; the demangled symbol's 4th parameter is `enc_op_type`, and
   `ENC_ALLREDUCE = 1` (DIE `<0x3bbd0>`). The strings `"Single Cycle ALLR"` confirm
   the function's purpose. *Verdict:* HOLDS, OBSERVED.

4. **`enc_alg_type` global ordinals: `KANGARING=3`, `SINGLE_CYCLE_RING=4`,
   `INTRA_RDH=5`, `INTER_RDH=7` (the two RDH split by `SINGLE_STEP_MESH=6`).**
   *Challenge:* could `INTRA_RDH`/`INTER_RDH` be adjacent (5,6)? *Test:* the DWARF
   enumeration at `<0x61ec6>` reads, in order, `SINGLE_CYCLE_RING=4` (`<0x61ee2>`),
   `INTRA_RDH=5` (`<0x61ee8>`), `SINGLE_STEP_MESH=6` (`<0x61eee>`), `INTER_RDH=7`
   (`<0x61ef4>`). So the pair is 5 and 7. *Verdict:* HOLDS — byte-for-byte with
   [collective-enums.md](../collectives/ops/collective-enums.md) §3.3.

5. **The cross-die peer rule is `local_seng ^ 2`, in `libnccom`.** *Challenge:* is
   the `xor` really against the engine index, or a flag toggle elsewhere? *Test:*
   `objdump -d --start-address=0x89fb0 | rg xor` yields `xor $0x2,%ebx` @`0x89fc9`
   inside `NeuronPlatformTRN2/TRN3::sengine_get_d2d_peer_nums` (both overrides
   folded to `0x89fb0` by ICF, confirmed by two `nm` entries at the same address).
   The `%ebx` register holds the local engine number derived from the function's
   `int` argument. *Verdict:* HOLDS, OBSERVED — the TRN2/TRN3 fold means the rule is
   gen-shared.

---

## CORRECTIONS / divergences vs the backing report and committed pages

- **No disagreement with the committed collectives pages.** Every enum value on
  this page — `enc_alg_type` (§1.3), `enc_alg_mesh_type` (§1.3),
  `reduction_type_t` (§3.3), `encd_neigh` (§2.4), `SDMA_CCETYPE` (§3.3), the
  `EVT_SEM` windows (§4.1), the `cc_op` layout (§4.3) — reconciles **byte-for-byte**
  with [collective-enums.md](../collectives/ops/collective-enums.md),
  [all-reduce.md](../collectives/ops/all-reduce.md),
  [top-sp-lowering.md](../collectives/ops/top-sp-lowering.md),
  [s3d3-collective.md](../collectives/ops/s3d3-collective.md), and
  [cce-in-transfer.md](../dma/cce-in-transfer.md).
- **CORRECTION (against a contiguous-RDH assumption):** `INTRA_RDH=5` / `INTER_RDH=7`
  are split by `SINGLE_STEP_MESH=6` ([§1.3](#13-the-enc_alg_type-output-enum)).
- **GOTCHA (metaring sub-enum vs global enum):** the per-object
  `enc_alg_metaring.type` (`KANGARING=1 / SINGLE_CYCLE_RING=2`) is a *different*
  numbering from the global `enc_alg_type` (`KANGARING=3 / SINGLE_CYCLE_RING=4`);
  only the global enum is the `cc_op.algo_type` nibble ([§1.1](#11-the-5-engines-live-in-one-enc_comm)).
- **TRN2/TRN3 `seng^2` are ICF-folded** to the same address `@0x89fb0`
  ([§2.2](#22-the-cross-die-peer-rule--the-c2c-ports)) — a `nm` reader sees two
  symbol names at one address; this is identity-by-folding, not two implementations.

---

## Cross-references

- The op-struct + device walk + reduce-op chain: [ALL_REDUCE (`0xC7`)](../collectives/ops/all-reduce.md)
- The enum source-of-truth (every value reconciled here): [Collective-Type + cc_op Enum Reference](../collectives/ops/collective-enums.md)
- The real HW SB2SB iDMA leg the steps lower to: [S3D3 Collective (SB2SB, `0xBF`)](../collectives/ops/s3d3-collective.md)
- The in-SDMA CCE reduce descriptor: [CCE In-Transfer Compute](../dma/cce-in-transfer.md)
- The TOP_SP `cc_op` SPAD program + host trigger: [TOP_SP Collective Lowering](../collectives/ops/top-sp-lowering.md)
- The ring/kangaring NCFW config + the `xor ecx`/`mov ecx,0x2` fork: [Ring + Kangaring](../collectives/ncfw/ring-kangaring.md)
- The mesh / hierarchical device firmware: [Mesh](../collectives/ncfw/mesh-collective.md), [Hierarchical](../collectives/ncfw/hierarchical-collective.md)
- The whole thread, compiler → device: [A Collective, End to End](../orientation/collective-end-to-end.md)
- Inter-engine sync insertion (ordering the legs): [Optimization + Inter-Engine Sync Insertion](opt-sync-insertion.md)
- The ISEL lowering of the pseudo trigger: [SundaISel](sundaisel.md)
