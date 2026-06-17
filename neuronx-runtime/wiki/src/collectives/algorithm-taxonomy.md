# Algorithm Taxonomy (Ring / Mesh / Hier / Kangaring / RDH)

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present. The composer source TU is `/opt/workspace/KaenaRuntime/enc/enc.cc`; the device-resident driver is `tdrv/encd.c`. `.text`/`.rodata` VMA == file offset, so every `0x…` is an analysis VMA. Other versions will differ.*
> *Evidence grade: **Confirmed (enum- and jump-table-anchored)** — the 11-entry dispatch is decoded from `enc_get_algorithm_name @0xfef30` (jump table `@0x857a00`); the three algorithm enums (`enc_alg_type`, `enc_pattern_t`, `metaring_type`) are verbatim DWARF; the tree-free claim is proven three independent ways (§5). · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

A NeuronCore does not pick a collective algorithm at runtime the way stock NCCL does. There is no cost model, no roofline search, no tuning database in libnrt. Instead the algorithm for each collective op is a **closed enumeration** — `enc_alg_type`, eleven valid values 0..10 — and selection is a two-pass walk of one feasibility gate (`enc_cc_algorithm_allowed @0x108d30`) followed by a fixed-priority cascade. This page is the catalog of that enumeration: every algorithm, its enum value, the gate that admits it, and the sibling page that owns its composer. It is the *taxonomy* — the others are the *implementations*.

The reference frame is the standard NCCL algorithm family, minus one member. Where upstream NCCL offers Ring, Tree (single- and double-binary), and CollNet, libnrt offers **Ring, Mesh, Hierarchical, Kangaring, and RDH** — and **no Tree**. The ring family is the textbook NCCL ring (an all-reduce is `(nranks-1)` reduce-scatter steps then `(nranks-1)` all-gather steps); the mesh family is a fully-peered `N·(N-1)` exchange the ring cannot express; the hierarchical family is a *fixed* two-level intra/inter decomposition built from ring/mesh/RDH stages; and RDH (Reduction-Distribution-Hub) is a hub-centric reduce/distribute pattern. The absence of any tree composer is not an oversight — it is structural, proven from the enum, the jump table, and the dispatcher's call graph (§5). Tree-shaped reductions are substituted by the two-level hierarchical path, which is the closest analogue but is *not* a logarithmic binary tree.

The taxonomy lives on three axes that a reimplementer must never conflate. The **host** axis is `enc_alg_type` (11 valid (0..10) + INVALID=11 sentinel, picks the composer). The **device** axis is `enc_pattern_t` (`RING=0`/`MESH=1`/`INVALID=2`, the 4-bit `cc_op_entry.algo_type` the firmware reads). The **ring-topology** axis is `metaring_type` (`RING`/`KANGARING`/`SINGLE_CYCLE_RING`/`RDH`/`INVALID`, derived from `enc_alg_type`). This page is organized around the first; it pins each `enc_alg_type` to the second and third, names the gate, and links the composer.

For reimplementation, the contract is:

- **The closed 11-value enum** — `enc_alg_type` 0..10 with sentinel 11 (`INVALID`); no twelfth value exists and no future-reserved tree slot. The producer (`neuronx-cc`) bakes one value per op into the NEFF; libnrt only *reads* it (`enc_get_operation_algorithm @0xfeee0`, op struct `+0x14`).
- **The selection mechanism** — *not* a search. `enc_cc_algorithm_allowed @0x108d30` is a per-`(comm_type, alg_type)` boolean gate; `enc_post_operation` runs a battery of `enc_can_post_*` queries and resolves one winner by a fixed precedence (§2.1).
- **The three-axis collapse** — the composer maps the 11 valid (0..10) host algorithms onto the binary device pattern; the metaring sub-axis is the ring-family topology label. None of the three carries a tree member.

| | |
|---|---|
| **Algorithm roster fn** | `enc_get_algorithm_name @0xfef30` — 11-case jump table `@0x857a00`, default `0xfefd0` = `"Invalid"` |
| **Per-op alg read** | `enc_get_operation_algorithm @0xfeee0` — op struct `+0x14`; OOB → sentinel `0xb` (11) |
| **Feasibility gate** | `enc_cc_algorithm_allowed @0x108d30` — per-`(enc_comm_type, enc_alg_type)` boolean; 2nd jump table `@0x857a2c` |
| **Ring-topology map** | `enc_get_metaring_type @0xfc860` — alg → `metaring_type` (0→RING, 3→KANGARING, 4→SINGLE_CYCLE, 7→RDH) |
| **Host alg axis** | `enc_alg_type` — 11 valid (`RING=0 … BW_OPT_MESH=10`), `INVALID=11` |
| **Device pattern axis** | `enc_pattern_t` — `RING=0`, `MESH=1`, `INVALID=2` (the `cc_op_entry.algo_type` field) |
| **Metaring sub-axis** | `metaring_type` — `RING=0`, `KANGARING=1`, `SINGLE_CYCLE_RING=2`, `RDH=3`, `INVALID_METARING=4` |
| **Tree composer** | **absent** — no symbol, no enumerator, no call edge (§5) |
| **Alg initializers** | `alg_ring_init @0xfddd0`, `alg_kangaring_init @0xfe480`, `alg_inter_rdh_init @0xfe1f0`, `alg_mesh_init @0x135990` |

---

## 1. The Three Algorithm Axes

The single most important fact about the collective stack is that "algorithm" means three different things at three layers, with three different enums. A reimplementer who carries one enum through all three layers will overflow the 4-bit device field and mis-select the composer.

### 1.1 `enc_alg_type` — the host composer axis (DWARF DIE `<61eb4>`)

This is *the* algorithm selector: a closed 11-value enum (sentinel 12th), decl `enc/*.h:115`, `byte_size 4`. It picks which composer builds the op's schedule. Two identical DWARF copies exist (`<61eb4>` and `<1ac472>`).

| `enc_alg_type` | Value | Name string (`enc_get_algorithm_name`) |
|---|---|---|
| `ENC_ALG_RING` | 0 | `Ring` (`0x840d11`) |
| `ENC_ALG_HIER` | 1 | `Hier` (`0x840d16`) |
| `ENC_ALG_MESH` | 2 | `Mesh` (`0x840d26`) |
| `ENC_ALG_KANGARING` | 3 | `Kangaring` (`0x840d2b`) |
| `ENC_ALG_SINGLE_CYCLE_RING` | 4 | *(→ `Invalid`; no dedicated string)* |
| `ENC_ALG_INTRA_RDH` | 5 | `RDH` (`0x84278d`) |
| `ENC_ALG_SINGLE_STEP_MESH` | 6 | `Single Step Mesh` (`0x840d35`) |
| `ENC_ALG_INTER_RDH` | 7 | `RDH` (`0x84278d`, shared with intra) |
| `ENC_ALG_TWO_STEP_POD_MESH` | 8 | `UltraServer Mesh` (`0x840d46`) |
| `ENC_ALG_LATENCY_OPT_MESH` | 9 | `Mesh` (`0x840d26`, shared) |
| `ENC_ALG_BW_OPT_MESH` | 10 | `Bw Optimal Mesh` (`0x840d1b`) |
| `ENC_ALG_INVALID` | 11 | `Invalid` (`0x840d57`); sentinel for unset op->alg |

> **NOTE —** the name table has fewer distinct labels than the enum has values: `SINGLE_CYCLE_RING (4)` falls through to the `"Invalid"` default string (it is a ring variant, not an invalid alg), `LATENCY_OPT_MESH (9)` reuses `"Mesh"`, and `INTRA_RDH (5)` / `INTER_RDH (7)` both print `"RDH"`. The name string is for logging only; never use it as an identity key. This matches the roster in [`collectives/overview.md` §3.1](overview.md) and [`cc-op-isa.md` §4](cc-op-isa.md) — three pages, same enum.

### 1.2 `enc_pattern_t` — the device pattern axis (DWARF DIE `<125733>`/`<125734>`)

This is the **only** algorithm enum that reaches the wire descriptor: the 4-bit `cc_op_entry.algo_type`. It is binary — ring vs mesh — plus an invalid sentinel.

| `enc_pattern_t` | Value | Union view in `cc_op_entry` |
|---|---|---|
| `ENC_PATTERN_RING` | 0 | ring `channel_list` bitmap |
| `ENC_PATTERN_MESH` | 1 | mesh `{sema_shift_offset, sema_mask}` |
| `ENC_PATTERN_INVALID` | 2 | — |

> **CORRECTION (ALG-1) —** an early seed note recorded `enc_pattern_t` as `{RING=1, MESH=2}`. The DWARF in this binary (DIE `<125733>`, typedef `<125758>`, decl `enc/*.h:213/217`) is unambiguous: **`ENC_PATTERN_RING = 0`, `ENC_PATTERN_MESH = 1`, `ENC_PATTERN_INVALID = 2`**. This is the value scheme the already-shipped [`cc-op-isa.md`](cc-op-isa.md) pins (its own `CORRECTION (CCOP-2)` overturned the same `{RING=1, MESH=2}` guess from the IDA-recovered enum) and that [`channel-descriptor.md`](channel-descriptor.md) uses to select the ring vs mesh config region. All four sources — this page's DWARF, both shipped pages' enum, and the `cc_op_entry` packer's two store branches — agree on `RING=0 / MESH=1 / INVALID=2`. The DWARF is authoritative; the seed is discarded. The composer is where the 11 valid (0..10) host `enc_alg_type` values collapse onto this binary device pattern.

### 1.3 `metaring_type` — the ring-topology sub-axis (DWARF DIE `<125a2d>`)

A derived label for the ring-family topology, computed from `enc_alg_type` by `enc_get_metaring_type @0xfc860` (`cmp $0x4/$0x3/$0x7` disasm). It is the `type` field of `enc_alg_metaring` (`+37512`), consumed by `init_metaring_algorithm` and the `enc_metaring_primitive` ctor. Not a `cc_op_entry` field.

| `metaring_type` | Value | Source `enc_alg_type` (`enc_get_metaring_type`) |
|---|---|---|
| `RING` | 0 | `ENC_ALG_RING` (0) |
| `KANGARING` | 1 | `ENC_ALG_KANGARING` (3) |
| `SINGLE_CYCLE_RING` | 2 | `ENC_ALG_SINGLE_CYCLE_RING` (4) |
| `RDH` | 3 | `ENC_ALG_INTER_RDH` (7) |
| `INVALID_METARING` | 4 | (anything else → assert/error) |

---

## 2. The Algorithm Taxonomy

The eleven algorithms partition into four families by composer. The table below is the spine of the page: each algorithm, its enum value, the gate condition that admits it (a short `c` predicate, decoded from `enc_cc_algorithm_allowed @0x108d30`), the device pattern it collapses to, and the sibling page that owns its composer. Gate conditions are abbreviated from the per-`enc_alg_type` switch arms; the full battery is §2.1.

| Algorithm | `enc_alg_type` | Family / device pattern | Selection gate (abbreviated) | Owning sub-page | Confidence |
|---|---|---|---|---|---|
| `ENC_ALG_RING` | 0 | metaring → RING | always allowed (fallthrough default) | [Ring Scheduling](ring-scheduling.md) | HIGH |
| `ENC_ALG_HIER` | 1 | hierarchical (decomposes → RING + MESH) | `inter` comm, `local_rank_n>1`, `node_n*local_rank_n==rank_n`, `src_target_pairs_id==-1`, `dbg_hierarchical_cc!=2` | [Hierarchical & RDH](hierarchical-rdh.md) | HIGH |
| `ENC_ALG_MESH` | 2 | mesh → MESH | `enc_enable_mesh_alg`: `nccl_mesh_supported`, `!multi_stream`, per-family + `vcore_size` + `local_rank_n` switch + same-chip-group check | [Mesh Composer](mesh-composer.md) | HIGH |
| `ENC_ALG_KANGARING` | 3 | metaring → KANGARING / RING | `!(multi_stream \|\| inter \|\| dbg==2)`; gated on `vcore_size`+`rank_n` (vcs==2 & rank_n>4, or vcs==1 & rank_n>8) | [Ring Scheduling](ring-scheduling.md) | HIGH |
| `ENC_ALG_SINGLE_CYCLE_RING` | 4 | metaring → SINGLE_CYCLE / RING | `inter` only, `dbg!=2`, `node_n>1`, `rank_n==node_n` | [Ring Scheduling](ring-scheduling.md) | HIGH |
| `ENC_ALG_INTRA_RDH` | 5 | RDH (mesh-dependent) → MESH | `intra` only, `!multi_stream`, `dbg_rdh_cc_mode!=0`; depends on mesh being allowed | [Hierarchical & RDH](hierarchical-rdh.md) | HIGH |
| `ENC_ALG_SINGLE_STEP_MESH` | 6 | mesh → MESH | `enc_enable_mesh_alg` (single-step variant); `is_single_step_mesh` subtype | [Mesh Composer](mesh-composer.md) | HIGH |
| `ENC_ALG_INTER_RDH` | 7 | metaring → RDH (mesh-dependent) | `inter` only, `local_rank_n==1`, `rank_n==node_n`, `rank_n` pow-2 `>7`, `!enable_pod` | [Hierarchical & RDH](hierarchical-rdh.md) | HIGH |
| `ENC_ALG_TWO_STEP_POD_MESH` | 8 | mesh → MESH | `enc_enable_mesh_alg` (two-step-pod variant); pod proxy builders | [Mesh Composer](mesh-composer.md) | HIGH |
| `ENC_ALG_LATENCY_OPT_MESH` | 9 | switch-mesh → MESH | `enc_enable_mesh_alg`; NeuronSwitch-v1 single-hop latency-opt path | [Switch Broadcast/Barrier](switch-broadcast-barrier.md) | MED |
| `ENC_ALG_BW_OPT_MESH` | 10 | switch-mesh → MESH | `enc_enable_mesh_alg`; NeuronSwitch-v1 single-hop bandwidth-opt path | [Switch Broadcast/Barrier](switch-broadcast-barrier.md) | MED |
| `ENC_ALG_INVALID` | 11 | — → INVALID | sentinel; returned when `op->alg` unset, never selected | — | HIGH |

> **GOTCHA —** the gate is a *legality predicate*, not a chooser. `enc_cc_algorithm_allowed` answers "is alg X feasible for this `(comm, replica-group, topology)`?"; it does **not** rank. Multiple algorithms are simultaneously *allowed* for a given op, and the winner is resolved by the precedence cascade in §2.1. A reimplementer who treats the first `true` gate as the selection will mis-rank whenever two families are both feasible (the common case on a single-node Trn2 pod, where MESH, KANGARING, and RING are all allowed and MESH must win).

### 2.1 Selection: the feasibility battery and the precedence cascade

Selection is two passes over the *same* gate. At communicator-init (`enc_init_comm @0x135d60`) the gate fixes which *family objects* are built per comm (INTRA/INTER); at op-post (`enc_post_operation @0x11f790`) it re-resolves a concrete `enc_alg_type` per op. The post-time resolution runs a battery of `enc_can_post_*` queries — every gate runs, unconditionally — then a fixed if-else cascade picks one winner. This precedence is decoded in [`overview.md` §2.1](overview.md); reproduced here as the taxonomy's ranking:

```c
// enc_post_operation @0x11f790 — alg precedence (decompile lines 762-791)
// every enc_can_post_* has already run; this is a priority resolver, NOT short-circuit
if can_hierarchical && v86:          alg = ENC_ALG_HIER;               // 1  (v86 = no flat-ring family + no ring drv_alg)
else if can_intra_rdh:               alg = ENC_ALG_INTRA_RDH;          // 2
else if can_mesh:                    alg = ENC_ALG_MESH;               // 3
else if can_kangaring:               alg = ENC_ALG_KANGARING;          // 4
else if can_single_cycle_ring:       alg = ENC_ALG_SINGLE_CYCLE_RING;  // 5
else if can_inter_rdh:               alg = ENC_ALG_INTER_RDH;          // 6
else:                                alg = ENC_ALG_RING;               // 7  (fallthrough)
// ALLTOALL / ALLTOALL_V hard-assert alg == ENC_ALG_MESH (enc.cc:0xE4F)
```

> **QUIRK —** `ENC_ALG_RING (0)` is both the lowest-priority winner and the universal fallback: the cascade's `else` arm and the only `enc_alg_type` whose gate is unconditionally `true`. So a ring algorithm is *always* legal and is what every op falls back to when no richer family is feasible — which is why the metaring composer is the one piece of the stack that can never be elided. Hierarchical wins *only* when no flat-ring family (kangaring / mesh / single-cycle) is feasible and there is no ring `drv_alg` already built (the `v86` extra condition).

### 2.2 The producer baked the choice; the runtime only reads it

`enc_get_operation_algorithm @0xfeee0` reads the op's algorithm from a per-operation field (`enc_operation +0x14`, DWARF decl `enc.cc:463`) that the Neuron compiler (`neuronx-cc`) baked into the NEFF. When that field is out of range or unset, the function clamps to the sentinel `0xb` (11, `INVALID`). There is **no runtime cost model, no algorithm search, no tuning database** in libnrt — the selection logic above is a *legality* check on a producer-supplied choice, not a discovery of the best algorithm. (The dead cost model lives in the sibling `libnccom.so`; see [`nccom/tuning.md`](../nccom/tuning.md).)

Because the producer enum closes at 10 with no tree value, and libnrt clamps anything `>10` to the invalid sentinel, no NEFF can ever request a tree algorithm and no runtime composer exists to honor one (§5).

---

## 3. The Four Composer Families

Each family is a distinct schedule object and composer entry point. This section is the one-paragraph orientation per family; the byte-level composer logic is the linked sub-page's job.

### 3.1 Ring family (RING / KANGARING / SINGLE_CYCLE_RING / INTER_RDH)

Schedule object `enc_alg_metaring` (37544 B; `ring_ranks[32]` @+2824, `kangaring_ranks[32]` @+3592, `type` @+37512). The four ring-family algorithms share one composer, `enc_metaring_primitive::compose_operation @0x178170`, and one device-init dispatcher, `init_metaring_algorithm @0xfe570`, which branches **only** to `alg_ring_init @0xfddd0` (RING / SINGLE_CYCLE), `alg_kangaring_init @0xfe480` (KANGARING), and `alg_inter_rdh_init @0xfe1f0` (RDH) — three call targets, verified at `fe7b7/fe789/fe72b`, no fourth branch. The all-reduce is the canonical NCCL ring: `(nranks-1)` reduce-scatter steps (`send` → `recv_reduce_send` → `recv_reduce_copy_send`) then `(nranks-1)` all-gather steps (`direct_recv_send` → `direct_copy_send` → `direct_recv`), half-chunk pipelined. KANGARING is Neuron's multi-rail ring variant (per-VNC `logical_path[256]`). Full math: [Ring Scheduling](ring-scheduling.md).

### 3.2 Mesh family (MESH / SINGLE_STEP_MESH / TWO_STEP_POD_MESH / LATENCY_OPT_MESH / BW_OPT_MESH)

Schedule object `enc_alg_mesh` (61568 B; `mesh_subtype[20]` @+1136). Entry `alg_mesh_init @0x135990` → `alg_mesh_build_subtypes @0x133cd0`, which dispatches on silicon family to one of three flavors: `alg_mesh_build_full_mesh @0x125fb0` (`ENC_ALG_FULL_MESH`, canonical `N·(N-1)`), `alg_mesh_initializer_pd` (Trn2/Trn3 per-device, ~110 methods including the single-step and two-step-pod-mesh proxy builders), and `alg_mesh_initializer_switch` (NeuronSwitch-v1, the latency-opt and bandwidth-opt single-hop paths that back algorithms 9 and 10). A fourth, `ENC_ALG_GROUPED_MESH`, is the inf2/family-4 path (`rank_n % 6 == 0`; the grouping is `alg_mesh_build_subtypes`' own arithmetic — **no union-find**; the `disjoint_set`/`connected_components` partitioner serves only `enc_parse_src_target_pairs`, see [topology-partition CORRECTION TOPO-1](topology-partition.md)). Mesh is the *only* family that can serve `ALLTOALL` (the ring path rejects it: `"alltoall cannot be supported without Mesh algorithm"`). Full composer: [Mesh Composer](mesh-composer.md).

### 3.3 Hierarchical family (HIER)

State object `enc_alg_hier` (386368 B; `intra` @0, `inter` @+136736, `pipeline.stage[3]` @+273472), embedded at `enc_comm->hier`. Composer `enc_hier_primitive::compose_operation @0x1a8d20` decomposes one global collective into a **fixed two-level** schedule: for all-reduce, intra reduce-scatter → inter all-reduce → intra all-gather (three stages, `__compose_allreduce @0x1a34c0`). Each stage is composed from the *same* lower-level primitives — `__select_algorithms @0x14c620` picks one of `{INTRA_RDH=5, MESH=2, INTER_RDH=7, KANGARING=3, SINGLE_CYCLE_RING=4}` per stage slot, never a value the enum lacks. Cross-stage partials live in a shared 128 MiB (Trn2/Trn3) / 32 MiB intermediate buffer (`comm.hier.devmem_res`). This is the closest analogue to a tree, and §5 proves it is not one. Full composer: [Hierarchical & RDH](hierarchical-rdh.md).

### 3.4 RDH family (INTRA_RDH / INTER_RDH)

RDH — Reduction-Distribution-Hub — is split across the metaring composer (`INTER_RDH` via `alg_inter_rdh_init @0xfe1f0` and `__compose_inter_rdh @0x15ae70`) and the mesh composer (`INTRA_RDH`, which depends on mesh being allowed: `"Detected usage of RDH while mesh is not allowed. RDH construction will be skipped because it depends on Mesh."`). The per-device DMA schedule is built by `rdh_scheduler::schedule_{1,2,4,16}_dev_*` (`0x1df630..0x1eedd0`). RDH reduces to a hub and distributes from it — a star/hub pattern, not a binary tree (the hub-shape question is the one OPEN item, §6). Full composer: [Hierarchical & RDH](hierarchical-rdh.md).

---

## 4. The Algorithm Initializers

The entire `alg_*_init` family is four functions — no tree initializer exists. These are the device-resident setup entry points the family composers call; the dispatcher `init_metaring_algorithm @0xfe570` is the proof of closure (three branches only).

| Initializer | Address | Algorithm(s) | Schedule object | Confidence |
|---|---|---|---|---|
| `alg_ring_init` | `0xfddd0` | RING, SINGLE_CYCLE_RING | `enc_alg_metaring` | HIGH |
| `alg_kangaring_init` | `0xfe480` | KANGARING | `enc_alg_metaring` (`kangaring_ranks`) | HIGH |
| `alg_inter_rdh_init` | `0xfe1f0` | INTER_RDH | `enc_alg_metaring` (RDH) | HIGH |
| `alg_mesh_init` | `0x135990` | MESH family (all five) | `enc_alg_mesh` | HIGH |
| `init_metaring_algorithm` | `0xfe570` | dispatcher → ring/kangaring/rdh only | — | HIGH |
| *(tree initializer)* | **absent** | — | — | HIGH |

> **NOTE —** there is no `alg_hier_init`: the hierarchical family does not get its own metaring initializer because it *reuses* the intra/inter ring/kangaring/rdh/mesh objects of its two levels. `encd_alg_init_hier @0x24f250` is its device-resident setup, but it calls the same per-level `encd_alg_metaring` init with `metaring_type` ∈ {RING, KANGARING, RDH}. The hierarchy is a composition of the four initializers above, not a fifth one.

---

## 5. There Is No Tree Composer

The single most consequential structural fact: **libnrt's collective layer is tree-free**. There is no single- or double-binary-tree reduce/broadcast composer, no tree-node struct, no tree enumerator, and no tree-shaped up/down phase loop. This is not dead-retained code (as the sibling `libnccom.so` retains dead `ncclGetBtree`/`ncclGetDtree`) — in libnrt the tree builders do **not exist at all**. Proven three independent ways.

> **CORRECTION (ALG-2) — there is NO "Tree" composer in libnrt (proven, not assumed).** An NCCL-shaped mental model expects a Tree algorithm alongside Ring. libnrt has none. The proof is structural and exhaustive:
>
> 1. **Closed jump table.** `enc_get_algorithm_name @0xfef30` is `cmp $0xa,%edi; ja default` → 11 cases via table `@0x857a00`. Every slot maps to `{Ring, Hier, Mesh, Kangaring, Invalid, RDH, Single Step Mesh, RDH, UltraServer Mesh, Mesh, Bw Optimal Mesh}`. No slot points to a "Tree" string; no "Tree" string exists to point to.
> 2. **Exhaustive DWARF enums.** `enc_alg_type {0..10}`, `enc_pattern_t {0..2}`, `metaring_type {0..4}` — a whole-DWARF enumerator sweep for "tree" (excluding `btree`/`_Rb_tree`) returns zero collective hits.
> 3. **Dispatcher coverage.** `init_metaring_algorithm @0xfe570` branches only to `alg_ring_init`/`alg_kangaring_init`/`alg_inter_rdh_init`; `nccl_init_info @0x12c540` calls only `nccl_setup_alg_ring_info` + `nccl_setup_alg_kangaring_info`. No call edge reaches a tree builder, because no tree builder symbol exists in the 25,623-entry symtab.

### 5.1 Where the tree code is, and where the tree work goes

The tree/topology construction (where NCCL's dead tree code lives) is entirely on the **libnccom** side. libnrt holds only thin `nccl*` `dlsym` thunks into libnccom's `neuron*` entrypoints; the `graph/trees.cc` translation unit is not part of libnrt, so `ncclGetBtree`/`ncclGetDtree`/`ncclTopoPreset`/`ncclBuildRings` are all absent from this binary. libnrt receives a *built* communicator and composes its own ring/mesh/rdh/hier DMA schedule on top — it never sees an NCCL tree. (The dead builders on the libnccom side: [`nccom/algorithm-tree.md`](../nccom/algorithm-tree.md).)

So where do the logarithmic-depth reductions that NCCL's double-binary tree would handle go? They are **substituted**, not implemented:

- **Ring / SINGLE_CYCLE_RING** metaring pipelines for the latency-tolerant case.
- **KANGARING** (Neuron's multi-rail ring) for higher bandwidth.
- **RDH** (reduce-to-hub then distribute) for the hub-centric case.
- **Two-level HIER** (intra reduce-scatter → inter → intra all-gather) as the closest tree analogue.

> **QUIRK —** the hierarchical path *looks* like a tree but is not one. `enc_hier_primitive` is a **fixed two-level** intra/inter decomposition whose stage algorithms (`__select_algorithms @0x14c620`) are drawn only from `{2,3,4,5,7}` — never a value the 11-enum lacks, and never recursing. A binary tree has logarithmic depth `O(log nranks)`; the hierarchy has exactly two levels (three with the optional ring-only pipeline stages). A reimplementer porting an NCCL tree all-reduce must map it onto the two-level HIER decomposition, not onto a recursive tree of ranks. The structural witness is `enc_alg_metaring`'s rank storage: `ring_ranks[32]` and `kangaring_ranks[32]` are *linear orderings*, with no `tree_ranks` / `tree.up` / `tree.down` / `treeToParent` fields anywhere.

### 5.2 The non-collective "tree" tokens

Every "tree" token elsewhere in libnrt belongs to vendored data structures, none with an edge into `enc_*`/`alg_*`/`rdh_*`/`nccl_*` collective code: absl Cord B-trees (`CordRepBtree`), absl/protobuf flat-hash-map tree-bucket promotion (`is_tree`/`TableEntryToTree`/`ConvertToTree`), protobuf `TextFormat::ParseInfoTree`, `std::_Rb_tree` (the internals of `std::map`/`std::set`), miniz inflate Huffman `init_tree`, and Rust libstd `"create whole tree"`. A reimplementer grepping for "tree" will find these and must discard all of them.

---

## 6. Verification Notes

> The taxonomy, the three enums, the tree-free claim, and the gate were cross-checked against IDA artifacts of `libnrt.so` 2.31.24.0 and reconciled against three already-shipped sibling pages:
> - **`enc_get_algorithm_name @0xfef30`**: 11-case jump table `@0x857a00`, `cmp $0xa; ja 0xfefd0` default, name strings at `.rodata 0x840d11..` confirmed. Identical roster in [`overview.md` §3.1](overview.md) and [`cc-op-isa.md` §4](cc-op-isa.md).
> - **Enums** `enc_alg_type` (DWARF `<61eb4>`, 11 valid + sentinel), `enc_pattern_t` (`<125733>`, RING=0/MESH=1/INVALID=2), `metaring_type` (`<125a2d>`, RING=0..INVALID=4) are verbatim DWARF, cross-confirmed against the shipped pages' `enums.json` values. `CORRECTION (ALG-1)` resolves the `{RING=1,MESH=2}` seed in favor of the DWARF, consistent with `cc-op-isa.md`'s own `CCOP-2`.
> - **`enc_cc_algorithm_allowed @0x108d30`**: the per-`(comm_type, alg_type)` gate (first gate = bit `(alg + 11*comm)` of `config.cc_allowed_alg_types`, then per-alg switch arms) is decoded in `P1-L-ENC-ALG-14`; the abbreviated gate column in §2 is from that switch. **[MED]** the LATENCY_OPT/BW_OPT_MESH gates (algs 9/10) are inferred from the `enc_enable_mesh_alg` per-family branch + the NeuronSwitch-v1 single-hop builders, not from a dedicated switch arm.
> - **Precedence cascade** (HIER > INTRA_RDH > MESH > KANGARING > SINGLE_CYCLE_RING > INTER_RDH > RING) is read from the `enc_post_operation` decompile (lines 762-791) with the `"alg == ENC_ALG_MESH"` all-to-all assert at `enc.cc:0xE4F`; matches [`overview.md` §2.1](overview.md).
> - **Tree-free proof**: jump-table decode, three exhaustive DWARF enums, dispatcher call-target verification (`fe7b7/fe789/fe72b`), and a 25,623-entry symtab sweep (`readelf` `.symtab`; an earlier draft's "25,112" was a count slip) — all in `P2-W2-ALG-TREE`. **HIGH.**
>
> **[OPEN]** Whether RDH internally forms a tree-shaped reduction *within* a hub (`rdh_scheduler::schedule_*_dev`) — named "hub", behaves as reduce-to-hub/distribute; its per-device DMA schedule not fully traced. Likely star/hub, not binary tree. Owned by [Hierarchical & RDH](hierarchical-rdh.md).
> **[OPEN]** Whether `neuronx-cc` could introduce a new `enc_alg_type` value in a future build; in *this* binary the enum is closed at 10 with no reserved tree slot, and libnrt clamps `>10` to sentinel `0xb`.

---

## Related Components

| Name | Relationship |
|---|---|
| `enc_get_algorithm_name` (`@0xfef30`) | The 11-entry roster jump table — the taxonomy's spine |
| `enc_get_operation_algorithm` (`@0xfeee0`) | Reads the NEFF-baked per-op `enc_alg_type` (`+0x14`) |
| `enc_cc_algorithm_allowed` (`@0x108d30`) | The per-`(comm, alg)` feasibility gate behind §2's selection column |
| `enc_get_metaring_type` (`@0xfc860`) | Maps `enc_alg_type` → `metaring_type` ring-topology label |
| `init_metaring_algorithm` (`@0xfe570`) | The three-target dispatcher proving ring-family closure (no tree branch) |
| `enc_post_operation` (`@0x11f790`) | Runs the `enc_can_post_*` battery + precedence cascade (§2.1) |

## Cross-References

- [Overview: the Collective-Compute Architecture](overview.md) — the two-layer substrate and the end-to-end dispatch flow this taxonomy feeds
- [Mesh Composer (alg_mesh_initializer)](mesh-composer.md) — `enc_alg_mesh`, the 20 subtypes, full/grouped/pd/switch builders (algs 2/6/8)
- [Ring Scheduling Math](ring-scheduling.md) — `enc_alg_metaring`, the ring/kangaring step loop, chunk sizing (algs 0/3/4)
- [Hierarchical and RDH Composition](hierarchical-rdh.md) — the fixed two-level intra/inter composer and the RDH hub schedule (algs 1/5/7)
- [Switch-Platform Broadcast and Barrier Tables](switch-broadcast-barrier.md) — NeuronSwitch-v1 latency-opt / bandwidth-opt mesh (algs 9/10)
- [enc Primitives (Send/Recv Leaves)](enc-primitives.md) — the per-step leaves every family composer drives
- [Topology Partitioning (Union-Find)](topology-partition.md) — the grouped-mesh disjoint-set grouping
- [The cc_op_entry On-Device ISA](cc-op-isa.md) — the device `enc_pattern_t` (`algo_type`) this taxonomy collapses onto
- [The 148-Byte Ring Channel Descriptor](channel-descriptor.md) — the ring config the `cc_op_entry`'s `channel_list` selects; KANGARING populates its peer-sema block
- [Tree / CollNet (Dead Code)](../nccom/algorithm-tree.md) — where the absent tree builders live (libnccom, dead) — the other half of the tree-free proof
- [Tuning Model (the Dead Cost Model)](../nccom/tuning.md) — the absent runtime cost model; selection here is legality, not search
- [back to index](../index.md)
