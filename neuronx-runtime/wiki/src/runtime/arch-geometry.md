# Per-Arch Device Layer: Geometry and Address Map

> *All addresses, offsets, symbol names, and per-arch return constants on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF; all four `PT_LOAD` segments are identity-mapped in this build, so `.text`, `.rodata`, **and `.data` are VMA == file offset** — every table below was read at its VMA directly. Provenance strings `/opt/workspace/KaenaRuntime/tdrv/encd/archs/{sunda,cayman,mariana}.c` and `.../common.h` root every leaf. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — every constant is the immediate of the leaf's `mov $N,%eax;ret` read by `objdump`, every routing table is `struct`-decoded from `.rodata`/`.data` at its VMA, the `soc_struct_t` slot offsets are from DWARF `structures.json`, and the arch dispatch is read from the `arch_init` decompile. · Part IV — TDRV Runtime, DEEP · [back to index](../index.md)*

## Abstract

The Neuron *encoder* (`encd`) device layer carries a second, larger per-silicon vtable than the 488-byte `tdrv_arch_ops` that [tdrv-arch-ops](tdrv-arch-ops.md) documents: the **744-byte `soc_struct_t`** (DWARF ordinal 9305, `structures.json` size `0x2E8`), the collectives/routing arch interface filled once by `mariana_arch_init` / `cayman_arch_init` / `sunda_arch_init` (`@0x25a930` / `0x25df70` / `0x25fd40`). Where `tdrv_arch_ops` holds the *device-driver* geometry (`num_tpb`, `num_seng`, BAR offsets), `soc_struct_t` holds the **fabric geometry** — the constants the AllReduce/AllGather/ReduceScatter routing math derives from: how many SBUF ports a NeuronCore has, how big a valid SBUF partition is, which DMA-queue index a queue-set maps to, which DMA engines bridge two reduction half-domains (the inter-RDH map), how many folds a p2p/mesh collective may take, and how many devices the switch fabric spans. This page is the byte-verified **geometry-dimension matrix** for that band, across the three arches the runtime knows: **sunda** (arch 2 · V2 · Trn1+Inf2), **cayman** (arch 3 · V3 · Trn2), **mariana** (arch 4 · V4 · Trn3).

Mechanically the band is a wall of single-instruction leaves — `is_address_in_sbuf` and `get_inter_rdh_dma_id` aside, almost every accessor compiles to `mov $N,%eax; ret` or a one-line table index. The redundancy is structural: `sunda.c`, `cayman.c`, and `mariana.c` each define an identically-named set of `<arch>_get_<property>` leaves, so the *code* triplicates while only the *constants* carry information. That is the organizing principle of this page — state the leaf shape once, then tabulate the values by functional property × arch. The leaves have zero direct callers in the static call graph; they are reached only through the `soc_struct_t` function-pointer slot the matching `*_arch_init` installs, which is why a tracer never sees them fire and the constants must be read from the leaf bodies. The dispatch is `arch_init` (`@0x2563f0`), which reads `al_hal_tpb_get_arch_type()` and tail-jumps `2→sunda_arch_init`, `3→cayman_arch_init`, `4→mariana_arch_init` — the same arch enum the 488-byte table uses.

The per-arch story has three shapes. Most properties are **arch-invariant** (the queue-index table, `num_sb_ports`, `num_sbuf_partitions` are byte-identical across all three). A second class is a **clean two-vs-one split**: sunda is the small chip (2 sequencer engines/MLA, the `{0,1}` inter-RDH map, the lower SBUF-partition size), while cayman and mariana share the big shape (4 SENG/MLA, the `{4,5}` inter-RDH map, byte-identical 8-window SBUF base tables). A third class is genuinely **per-generation** — the SBUF partition *size* steps `0x30000 → 0x38000 → 0x40000` across all three, and the two fabric-extent leaves (`get_max_devs`, `get_mesh_max_folds`) are *constant* on sunda/cayman but **runtime-conditional on mariana**, doubling when `nrt_is_neuron_switch_v1_family()` holds. Those divergences, and the address-in-SBUF / inter-RDH / p2p-fold routing tables behind them, are the matrix in §2 and the callouts in §4.

For reimplementation, the contract is:

- **The `soc_struct_t` geometry band** — the leaf installed into each geometry slot (`get_num_sb_ports @+424`, `get_valid_sbuf_partition_size @+560`, `get_num_sbuf_partitions @+576`, `get_inter_rdh_fold_n @+544`, `get_inter_rdh_dma_id @+552`, `get_rel_queue_idx @+632`, `get_seng/die_idx_from_tpb_idx @+648/+640`, `get_max_devs @+680`, `get_mesh_max_folds @+736`, …). Reproduce the *byte offsets* — every consumer reaches the slot by hard-coded displacement off the installed `soc_struct_t`.
- **The per-property constant matrix** — for each geometry property, the exact value each `<arch>_get_<property>` leaf returns (symbol + address + decoded constant), and which properties are arch-invariant, sunda-vs-big, or per-generation.
- **The routing tables** — `<arch>_inter_rdh_dma_map`, `<arch>_queue_idx`, `<arch>_sbuf_base_addresses`, and `<arch>_p2p_port_info_tbl`, decoded at their VMAs, with the index arithmetic each leaf applies (window stride `0x2000000`; p2p-table stride 72; queue-idx linear scan).
- **The family-conditional extent leaves** — `get_max_devs` and `get_mesh_max_folds` on mariana are *not* constants; they branch on `nrt_is_neuron_switch_v1_family()` (`@0x5cb320`) and return `v1 ? {32,16} : {16,8}`. A reimplementer who bakes them as constants mis-sizes the Trn3 switch fabric by 2×.

| | |
|---|---|
| **Geometry vtable** | `soc_struct_t` (DWARF ordinal 9305) — **744 B / ~93 8-byte slots**; distinct from the 488-B `tdrv_arch_ops` |
| **Installers** | `mariana_arch_init @0x25a930` · `cayman_arch_init @0x25df70` · `sunda_arch_init @0x25fd40` |
| **Dispatch** | `arch_init @0x2563f0` → `al_hal_tpb_get_arch_type()` → `2→sunda / 3→cayman / 4→mariana` (tail-jmp) |
| **Arch enum** | `SUNDA=2` (V2 / Trn1+Inf2) · `CAYMAN=3` (V3 / Trn2) · `MARIANA=4` (V4 / Trn3) |
| **Source TUs** | `tdrv/encd/archs/{sunda,cayman,mariana}.c` + `tdrv/encd/archs/common.h` (assert-file literals) |
| **Family predicate** | `nrt_is_neuron_switch_v1_family @0x5cb320` — gates `get_max_devs` / `get_mesh_max_folds` on **mariana only** |
| **SBUF window stride** | `0x2000000` (32 MiB), matching `V2_SBUF_SIZE` ([arch/hw-geometry](../arch/hw-geometry.md)) |
| **Routing tables** | `<arch>_inter_rdh_dma_map` · `<arch>_queue_idx` · `<arch>_sbuf_base_addresses` · `<arch>_p2p_port_info_tbl` |

> **CORRECTION (ARCH-GEOM-01) —** an early cell survey ([P1-L-TDRV-ARCH-02]) labeled mariana "Trn2-class / neuron-switch family." That is wrong: the shipped arch enum is `SUNDA=2`, `CAYMAN=3` (Trn2), `MARIANA=4` (Trn3), confirmed both by the `arch_init` dispatch (`cmp $0x4 → mariana_arch_init`, `@0x256402`) and by the sibling pages ([tdrv-arch-ops](tdrv-arch-ops.md), [arch/hw-geometry](../arch/hw-geometry.md)). This page uses **sunda=2 / cayman=3 / mariana=4** throughout. The "neuron-switch family" predicate (`nrt_is_neuron_switch_v1_family`) is an orthogonal runtime gate inside the *mariana* leaves, not an arch label.

---

## 1. The Geometry Band and Its Registration

### Purpose

`soc_struct_t` is the encoder layer's per-silicon collectives/routing interface — a 744-byte struct of ~93 function-pointer slots, zero-initialized and then filled by exactly one of the three `*_arch_init` bodies. It is a **sibling** of the 488-byte `tdrv_arch_ops`, not the same object: `tdrv_arch_ops` ([tdrv-arch-ops](tdrv-arch-ops.md)) is the device-driver vtable installed by `tdrv_arch_register_*`, holding `num_tpb`/`num_seng`/BAR-offset leaves; `soc_struct_t` is the *fabric* vtable installed by `*_arch_init`, holding the SBUF-geometry, queue-index, inter-RDH, p2p/mesh-fold, and switch-extent leaves this page owns. The two are reached through different globals and filled by different registrars; a reimplementer must keep them distinct.

The grouping axis is **geometry property**, not arch. Each of `sunda.c`/`cayman.c`/`mariana.c` defines the same `get_<property>` leaf, so the band is best read as one row per property crossed with three arch columns (§2). The leaves are pure: an input index or engine ordinal in, a constant or a table-indexed value out, an `__assert_fail` on out-of-range. None has an in-cell caller — they fire only through the installed slot.

### Entry Point

```text
arch_init (0x2563f0)                                ── tdrv/encd/archs dispatch
  └─ al_hal_tpb_get_arch_type (0x44bca0)            ── 2=SUNDA / 3=CAYMAN / 4=MARIANA
  └─ switch (tail-jmp):
       case 2 → sunda_arch_init   (0x25fd40)        ── installs sunda_* geometry leaves
       case 3 → cayman_arch_init  (0x25df70)        ── installs cayman_* geometry leaves
       case 4 → mariana_arch_init (0x25a930, 1333 B)── installs mariana_*/arch_common_* leaves
       default→ nlog_write(error); return 1
  (every geometry leaf is then reached ONLY via the installed soc_struct_t slot —
   zero direct callers in the call graph)
```

### Algorithm

The registrar `memset`s the 744-byte struct to zero and writes each slot by plain (SIMD-paired) store. The compiler fuses adjacent slot writes into 16-byte `movdqa` pairs (`_mm_unpacklo_epi64` of a `.data.rel.ro` reloc half and an immediate leaf-address half), so the raw decompile shows ~80 statements for ~93 slots; expanded to one store per geometry slot:

```c
function mariana_arch_init():                             // 0x25a930, mariana.c:2555
    S = &soc_struct                                       // 744-byte object, memset 0 first
    // --- SBUF / partition geometry ---
    S.get_num_sb_ports               = mariana_get_num_sb_ports             // +424  → 16
    S.get_valid_sbuf_partition_size  = mariana_get_valid_sbuf_partition_size// +560  → 0x40000
    S.get_num_sbuf_partitions        = mariana_get_num_sbuf_partitions      // +576  → 0x80 (uint8)
    S.is_address_in_sbuf             = mariana_is_address_in_sbuf           // +392  (8-window walk)
    // --- TPB ↔ SENG ↔ die decomposition ---
    S.get_seng_idx_from_tpb_idx      = mariana_get_seng_idx_from_tpb_idx    // +648  → idx>>1
    S.get_die_idx_from_tpb_idx       = mariana_get_die_idx_from_tpb_idx     // +640  → idx>>2
    S.get_num_sengines_per_mla       = mariana_get_num_sengines_per_mla     // +584  → 4
    // --- DMA queue-set / inter-RDH map ---
    S.get_queue_bundle_id_for_qset   = mariana_get_queue_bundle_id_for_qset // +336  → 4*(qb>3)
    S.get_rel_queue_idx              = mariana_get_rel_queue_idx            // +632  (queue_idx scan)
    S.get_inter_rdh_fold_n           = mariana_get_inter_rdh_fold_n         // +544  → 2
    S.get_inter_rdh_dma_id           = mariana_get_inter_rdh_dma_id         // +552  (rdh_dma_map[idx])
    // --- p2p / mesh / switch fabric extent ---
    S.get_p2p_port_fold_n            = mariana_get_p2p_port_fold_n          // +16   (p2p_tbl[port].fold_n)
    S.get_max_links_per_pod_port     = mariana_get_max_links_per_pod_port   // +368  → 2
    S.get_max_devs                   = mariana_get_max_devs                 // +680  → v1?32:16  ← QUIRK
    S.get_mesh_max_folds             = mariana_get_mesh_max_folds           // +736  → v1?16:8   ← QUIRK
    // ... ~75 further routing/address slots (p2p_port, mesh_dma_engine, barrier_sem, …)
    return NRT_SUCCESS
```

The single geometry accessor below is the canonical shape of an indexed leaf — `get_inter_rdh_dma_id`, an `.rodata` table read with a bound and an out-of-range `-1`:

```c
// mariana_get_inter_rdh_dma_id @0x257190 — mariana.c — bounded table index
function get_inter_rdh_dma_id(idx):                      // soc_struct +552
    if idx > 2:                                          // cmp $0x2,%edi
        return -1                                        // mov $0xffffffff,%eax (OOB sentinel)
    return mariana_inter_rdh_dma_map[idx]                // lea 0x9bf608; mov (rax,rdi,4) → {4,5,0}
```

### Function Map

The geometry leaves and their `soc_struct_t` slots. Addresses are the **mariana** leaves; the cayman/sunda siblings install their own `<arch>_` leaf into the *same* slot offset (§2 gives all three values per property). Slot offsets are DWARF `structures.json`.

| Slot (+off) | Member | mariana leaf · addr | Returns / form | Confidence |
|---|---|---|---|---|
| +16 | `get_p2p_port_fold_n` | `mariana_get_p2p_port_fold_n @0x2578a0` | `p2p_port_info_tbl[port].fold_n` (stride 72) | CERTAIN |
| +336 | `get_queue_bundle_id_for_qset` | `mariana_get_queue_bundle_id_for_qset @0x2570c0` | `4 * (qb_id > 3)` → {0,4} | CERTAIN |
| +368 | `get_max_links_per_pod_port` | `mariana_get_max_links_per_pod_port @0x257100` | const `2` | CERTAIN |
| +392 | `is_address_in_sbuf` | `mariana_is_address_in_sbuf @0x257110` | 8-window `[base,base+0x2000000)` walk | CERTAIN |
| +424 | `get_num_sb_ports` | `mariana_get_num_sb_ports @0x257150` | const `16` | CERTAIN |
| +544 | `get_inter_rdh_fold_n` | `mariana_get_inter_rdh_fold_n @0x257180` | const `2` | CERTAIN |
| +552 | `get_inter_rdh_dma_id` | `mariana_get_inter_rdh_dma_id @0x257190` | `inter_rdh_dma_map[idx]`, `-1` if idx>2 | CERTAIN |
| +560 | `get_valid_sbuf_partition_size` | `mariana_get_valid_sbuf_partition_size @0x257160` | const `0x40000` | CERTAIN |
| +576 | `get_num_sbuf_partitions` | `mariana_get_num_sbuf_partitions @0x257170` | const `0x80` (slot is `uint8_t`) | CERTAIN |
| +584 | `get_num_sengines_per_mla` | `mariana_get_num_sengines_per_mla @0x2570a0` | const `4` | CERTAIN |
| +632 | `get_rel_queue_idx` | `mariana_get_rel_queue_idx @0x2570d0` | `queue_idx` linear scan → rel idx | CERTAIN |
| +640 | `get_die_idx_from_tpb_idx` | `mariana_get_die_idx_from_tpb_idx @0x2571c0` | `idx >> 2` | CERTAIN |
| +648 | `get_seng_idx_from_tpb_idx` | `mariana_get_seng_idx_from_tpb_idx @0x2571b0` | `idx >> 1` | CERTAIN |
| +680 | `get_max_devs` | `mariana_get_max_devs @0x257990` | `v1_family ? 32 : 16` | CERTAIN |
| +736 | `get_mesh_max_folds` | `mariana_get_mesh_max_folds @0x257970` | `v1_family ? 16 : 8` | CERTAIN |

> **NOTE —** the `get_num_sbuf_partitions` leaf body is `mov $0xffffff80,%eax; ret` on all three arches — the immediate *looks* like the signed `-128`. It is not: the `soc_struct_t` slot is typed `uint8_t (*)(...)` (DWARF), so the caller reads only the low byte `0x80` = **128 partitions**. A reimplementer who stores the leaf's return in an `int` and uses it as a signed count will get `-128`; the install-time truncation to `uint8` is what makes it 128. (Clarifies [P1-L-TDRV-ARCH-02], which reported "128" — correct value, reconciled here against the raw immediate via the `uint8` cast.)

---

## 2. The Geometry Dimension Matrix

### Purpose

This is the page's core artifact: one row per geometry property, three arch columns, each cell the **decoded constant** the matching `<arch>_get_<property>` leaf returns — never a raw byte dump. Every runtime value was read from the leaf's `mov $N,%eax` immediate via `objdump`; every table value was `struct`-decoded at its `.rodata`/`.data` VMA. The "source getter" column pins the symbol+address of the leaf that returns the cell, so each cell is verifiable against the binary independently of its neighbors.

### The matrix

| Property | sunda (2 · V2) | cayman (3 · V3) | mariana (4 · V4) | Source getter (symbol · addr per arch) | Conf |
|---|---|---|---|---|---|
| **SB ports / NC** (`get_num_sb_ports`) | **16** | **16** | **16** | `<arch>_get_num_sb_ports` · sun `0x25e740` / cay `0x25b020` / mar `0x257150` | CERTAIN |
| **Valid SBUF partition size** (`get_valid_sbuf_partition_size`) | **`0x30000`** (196608) | **`0x38000`** (229376) | **`0x40000`** (262144) | `<arch>_get_valid_sbuf_partition_size` · sun `0x25e750` / cay `0x25af60` / mar `0x257160` | CERTAIN |
| **SBUF partitions / NC** (`get_num_sbuf_partitions`) | **128** (`0x80`/u8) | **128** | **128** | `<arch>_get_num_sbuf_partitions` · sun `0x25e760` / cay `0x25af80` / mar `0x257170` | CERTAIN |
| **SBUF window stride** (`is_address_in_sbuf`) | `0x2000000` | `0x2000000` | `0x2000000` | `<arch>_is_address_in_sbuf` · cay `0x25afe0` / mar `0x257110`; 8 windows | CERTAIN |
| **Sequencer engines / MLA** (`get_num_sengines_per_mla`) | **2** | **4** | **4** | `<arch>_get_num_sengines_per_mla` · sun `0x25e660` / cay `0x25af90` / mar `0x2570a0` | CERTAIN |
| **SENG index from TPB idx** | `idx>>1` | `idx>>1` | `idx>>1` | `<arch>_get_seng_idx_from_tpb_idx` · cay `0x25b060` / mar `0x2571b0` | CERTAIN |
| **Die index from TPB idx** | `idx>>2` | `idx>>2` | `idx>>2` | `<arch>_get_die_idx_from_tpb_idx` · cay `0x25b070` / mar `0x2571c0` | CERTAIN |
| **Queue-set → bundle bank** (`get_queue_bundle_id_for_qset`) | `4·(qb>3)` | `4·(qb>3)` | `4·(qb>3)` | `<arch>_get_queue_bundle_id_for_qset` · cay `0x25af50` / mar `0x2570c0` | CERTAIN |
| **Queue-index table** (`get_rel_queue_idx`) | `{0xb,0xc,0xd,0xf}→{0,1,2,3}` | (same) | (same) | `<arch>_queue_idx` · sun `@0xc08f60` / cay `@0xc08f40` / mar `@0xc08f20` | CERTAIN |
| **Inter-RDH fold count** (`get_inter_rdh_fold_n`) | (sunda band) | **2** | **2** | `<arch>_get_inter_rdh_fold_n` · cay `0x25b030` / mar `0x257180` | CERTAIN |
| **Inter-RDH DMA map** (`get_inter_rdh_dma_id`) | **`{0,1}`** | **`{4,5}`** | **`{4,5}`** | `<arch>_inter_rdh_dma_map` · sun `@0x9d2aa0` / cay `@0x9c9e80` / mar `@0x9bf608` | CERTAIN |
| **Max links / pod port** (`get_max_links_per_pod_port`) | (non-const, asserts) | **1** | **2** | `<arch>_get_max_links_per_pod_port` · sun `0x25ea80` / cay `0x25afa0` / mar `0x257100` | HIGH |
| **Max devices / fabric** (`get_max_devs`) | **16** | **16** | **`v1?32:16`** | `<arch>_get_max_devs` · sun `0x25e800` / cay `0x25b0c0` / mar `0x257990` | CERTAIN |
| **Mesh max folds** (`get_mesh_max_folds`) | **8** | **8** | **`v1?16:8`** | `<arch>_get_mesh_max_folds` · sun `0x25f350` / cay `0x25b0d0` / mar `0x257970` | CERTAIN |
| **SBUF base table** (`is_address_in_sbuf` windows) | (sunda band) | 8×u64, `0x2000000000`-based + mesh hi-bit | byte-identical to cayman | `<arch>_sbuf_base_addresses` · cay `@0x9ca0c0` / mar `@0x9bf820` | CERTAIN |
| **SP (TopSP) base address** (`get_sp_base_addr`) | `0xFFFD0000000` | `0x8280000000` | (mariana band) | `<arch>_get_sp_base_addr` · sun `0x25e610` / cay `0x25aee0` | CERTAIN |
| **RMTV HBM-channel parity** (`get_rmtv_hbm_idx`) | `hbm^1` | `hbm^1` | (mariana band) | `<arch>_get_rmtv_hbm_idx` · sun `0x25e670` / cay `0x25af30` | CERTAIN |

> **NOTE —** "sunda band" / "mariana band" in a cell means that leaf is owned by a sibling `.text` band not swept for this page, not that the property is absent — every arch fills every slot. The values that *are* decoded here triangulate the property's per-arch behavior; where a cell is blank, the §2 row for the property still resolves it on the other two arches.

### Reading the matrix

The matrix collapses to three behaviors a reimplementer must internalize:

- **Arch-invariant (build once).** `num_sb_ports` (16), `num_sbuf_partitions` (128), the SBUF window stride (`0x2000000`), the TPB-index decomposition (`>>1` seng, `>>2` die), the queue-set bundle-bank rule (`4·(qb>3)`), and the queue-index table (`{0xb,0xc,0xd,0xf}→{0,1,2,3}`, byte-identical at `0xc08f20`/`0xc08f40`/`0xc08f60`) are the **same** on all three. A reimplementer codes one body and installs it three times.
- **Sunda-vs-big (one two-way split).** `num_sengines_per_mla` is **2** on sunda, **4** on cayman/mariana — the same 2-vs-8-core shape [hw-geometry](../arch/hw-geometry.md) draws from `num_seng`. The inter-RDH DMA map follows it: sunda routes the two reduction half-domains through DMA engines `{0,1}`, while cayman and mariana use `{4,5}` (the higher engine bank that exists only on the 4-SENG chips). Both reduce to "sunda is the small fabric."
- **Per-generation (key the value on arch).** The valid SBUF partition size steps **`0x30000 → 0x38000 → 0x40000`** across sunda → cayman → mariana — three *distinct* values, no shared table. `get_max_links_per_pod_port` is **1** on cayman but **2** on mariana. And the two fabric-extent leaves are constants on sunda/cayman yet runtime-conditional on mariana (next callout). A reimplementer who assumes "Trn2 == Trn3" here mis-sizes the partition window and the pod-port link budget.

> **GOTCHA —** the SBUF base tables `cayman_sbuf_base_addresses @0x9ca0c0` and `mariana_sbuf_base_addresses @0x9bf820` are **byte-identical** — both `{0x2000000000, 0x3000000000, 0x6000000000, 0x7000000000, 0x802000000000, 0x803000000000, 0x806000000000, 0x807000000000}`, the eight 32-MiB SBUF tile windows with the `0x800000000000` mesh high-bit on tiles 4–7. An earlier survey ([P1-L-TDRV-ARCH-02]) reported the mariana table as `0x20000000`-based (a truncated 32-bit read); the real entries are the 40-bit tile bases that match cayman and the [arch-csr-offsets](arch-csr-offsets.md) TPB-base table. A reimplementer can share one SBUF base table across Trn2/Trn3 — but **not** with sunda, whose single-mesh layout has no `0x80…` high half.

---

## 3. The Routing Tables

### Purpose

Four `.rodata`/`.data` tables back the indexed geometry leaves. Each is small and fully decoded below; the leaf that reads it applies a fixed index arithmetic (a linear scan, a `[idx]`, or a stride-72 record index). These are the data half of the fabric geometry — the constants the routing leaves return are *table entries*, not immediates.

### Decoded tables

```text
<arch>_queue_idx          .data  — 4 × {u32 queue_id, u32 rel_queue_idx}; get_rel_queue_idx scans for a
                                   queue_id match and writes rel_queue_idx (NRT_SUCCESS) else NRT_INVALID.
  mariana @0xc08f20  = {0x0b→0, 0x0c→1, 0x0d→2, 0x0f→3}
  cayman  @0xc08f40  = {0x0b→0, 0x0c→1, 0x0d→2, 0x0f→3}   (byte-identical)
  sunda   @0xc08f60  = {0x0b→0, 0x0c→1, 0x0d→2, 0x0f→3}   (byte-identical)

<arch>_inter_rdh_dma_map  .rodata — u32[]; get_inter_rdh_dma_id returns map[idx] for idx≤2 else -1.
  mariana @0x9bf608  = {4, 5, 0, …}     ← reduction half-domains bridged by DMA eng 4 & 5
  cayman  @0x9c9e80  = {4, 5, 0, …}     (byte-identical to mariana)
  sunda   @0x9d2aa0  = {0, 1, 0, …}     ← small fabric: DMA eng 0 & 1

<arch>_sbuf_base_addresses .rodata — 8 × u64; is_address_in_sbuf tests dev_addr ∈ [base, base+0x2000000)
                                     for each of the 8 windows (stride 8, end-sentinel at base+0x40).
  mariana @0x9bf820  = {0x2000000000, 0x3000000000, 0x6000000000, 0x7000000000,
                        0x802000000000, 0x803000000000, 0x806000000000, 0x807000000000}
  cayman  @0x9ca0c0  = (byte-identical to mariana)

<arch>_p2p_port_info_tbl  .rodata — record stride 72: {int fold_n@+0, int link_n@+4, mla_location_t links[16]@+8}.
                                    get_p2p_port_fold_n returns tbl[port].fold_n for port ≤ TDRV_HW_PORT_INVALID(12).
  mariana @0x9c6780  (lea (rdi,rdi,8) then ×8 = 72-byte stride; ≥13 entries)
  sunda   @0x9d2ac0  (13 × 72 B; entry0 fold_n=3 link_n=3 …)
```

### Considerations

> **NOTE —** `is_address_in_sbuf` is the one non-constant SBUF leaf: it is an 8-iteration loop (`lea base; lea base+0x40 sentinel`) testing `dev_addr ∈ [base, base+0x2000000)` per window and returning `1` on the first hit (`@0x257110` for mariana). The window size `0x2000000` (32 MiB) is the same `V2_SBUF_SIZE` [hw-geometry](../arch/hw-geometry.md) pins for the per-NeuronCore state buffer — consistent across arches even though the *partition* size inside a window (`get_valid_sbuf_partition_size`) steps `0x30000/0x38000/0x40000`. A reimplementer keys the partition size on arch but the window size on the shared `V2_SBUF_SIZE` constant.

> **QUIRK —** the queue-index table is `{queue_id 0xb,0xc,0xd,0xf}` — note **`0x0e` is skipped**. `get_rel_queue_idx` is a linear scan, not an array index, precisely because the four physical queue ids are sparse (11, 12, 13, 15) and map to dense relative indices 0–3. A reimplementer who builds a direct `rel = queue_id - 0xb` lookup gets the wrong answer for `0x0f` (would yield 4, not 3). The scan, and the sparse table, are what make the gap at 14 work.

---

## 4. Per-Generation Geometry Divergences

The reimplementer-trapping divergences, gathered. Two are clean values (partition size, pod-port links); the dangerous one is the family-conditional fabric extent on mariana.

### The family-conditional extent leaves (mariana only)

On sunda and cayman, `get_max_devs` and `get_mesh_max_folds` are constants (`16`/`8`). On **mariana** they are *runtime-conditional* — they call `nrt_is_neuron_switch_v1_family()` (`@0x5cb320`) and double the extent when it holds:

```c
// mariana_get_max_devs @0x257990 — mariana.c — family-conditional fabric width
function get_max_devs():
    v1 = nrt_is_neuron_switch_v1_family()                // 0x5cb320
    // cmp $1,%al; sbb %eax,%eax  →  v1 ? 0 : 0xFFFFFFFF
    return ((v1 ? 0 : ~0u) & 0xFFFFFFF0) + 0x20          // and 0xfffffff0; add 0x20
    //   v1 family   → 0      & ..f0 + 0x20 = 0x20 = 32 devices
    //   non-v1      → ~0     & ..f0 + 0x20 = 0x10 = 16 devices

// mariana_get_mesh_max_folds @0x257970 — same idiom, and-mask 0xfffffff8, +0x10
function get_mesh_max_folds():
    v1 = nrt_is_neuron_switch_v1_family()
    return ((v1 ? 0 : ~0u) & 0xFFFFFFF8) + 0x10          // v1 → 16 folds ; non-v1 → 8 folds
```

> **QUIRK —** `get_max_devs` and `get_mesh_max_folds` are the **only** geometry leaves on this page that are not pure constants/table-reads. They are constant on sunda/cayman but a runtime branch on mariana, so the Trn3 switch fabric is `32 devices / 16 folds` in the v1-switch family and `16 / 8` otherwise. A reimplementer who copies the cayman *constant* leaf onto mariana (the obvious "they share the big shape" assumption from §2) will hard-cap the mariana mesh at 16 devices / 8 folds and silently truncate every collective that spans the larger v1 fabric. The `sbb`/`and`/`add` idiom is a branchless `v1 ? 2x : x` — decode it as a 2× toggle, not a magic constant.

### The SBUF partition-size step and pod-port link budget

```text
get_valid_sbuf_partition_size        get_max_links_per_pod_port
──────────────────────────────       ─────────────────────────────
  sunda   0x30000  (196608)            sunda   (non-const, asserts)
  cayman  0x38000  (229376)            cayman  1
  mariana 0x40000  (262144)            mariana 2
```

> **GOTCHA —** the SBUF partition size is the cleanest three-way per-generation axis on the page — three distinct constants, no shared table, no derivable relationship (`0x30000`, `0x38000`, `0x40000` are uniformly `+0x8000` apart in *this* build, but that is not a documented invariant — read each from its own leaf). Key it on arch. The same caution applies to `get_max_links_per_pod_port`: cayman allows **1** link per pod port, mariana **2** — a pod-port DMA scheduler that assumes the cayman value on mariana will under-provision the inter-pod links by half. Both are single-immediate leaves, trivially per-arch; the trap is assuming Trn2 and Trn3 agree because they agree on `num_seng`.

### The inter-RDH DMA-engine split

> **NOTE —** the inter-RDH map is where the "sunda small / cayman+mariana big" split shows in the *routing data*, not just the counts. The inter-RDH (Reduction-Domain-Handler) map names the DMA engines that bridge the two halves of a reduction domain: sunda uses engines `{0,1}` (`@0x9d2aa0`), cayman and mariana use `{4,5}` (`@0x9c9e80` / `@0x9bf608`, byte-identical). The higher engine bank exists on the 4-SENG chips; sunda's 2-SENG fabric has only the low bank. A reimplementer building the collectives engine table must select the map by arch — the `get_inter_rdh_dma_id` leaf hard-references the per-arch `.rodata` symbol, so the engine pair is not a function of `num_seng` at runtime but a baked-in table the installer wires.

## Related Components

| Name | Relationship |
|---|---|
| `mariana_get_mesh_dma_engine_id_from_tbl` (`0x259e60`) | the largest collectives consumer of this band — indexes ~30 routing tables keyed by the `get_seng/die_idx`, `get_rel_queue_idx`, and `get_inter_rdh_dma_id` outputs documented here |
| `mariana_get_p2p_port` / `cayman_get_p2p_port` (`0x259ae0` / `0x25d1c0`) | top-level p2p port resolver; reads `get_p2p_port_fold_n` (slot +16) and the `p2p_port_info_tbl` decoded in §3 |
| `nrt_is_neuron_switch_v1_family` (`0x5cb320`) | the runtime predicate that flips `get_max_devs` / `get_mesh_max_folds` to their 2× value on mariana (§4) |
| `arch_init` (`0x2563f0`) | the dispatch that picks `{sunda,cayman,mariana}_arch_init` by `al_hal_tpb_get_arch_type()`, installing this band's leaves into `soc_struct_t` |

## Cross-References

- [Per-Generation Hardware Geometry](../arch/hw-geometry.md) — the `tdrv_arch_ops` device-geometry constants (`num_tpb` 2/8/8, `num_seng` 2/4/4, HBM/SBUF sizes); this page's `num_sengines_per_mla` and SBUF window stride must agree with its `num_seng` and `V2_SBUF_SIZE` (they do)
- [TDRV: arch-ops Dispatch and Sync-Event Accessors](tdrv-arch-ops.md) — the **distinct** 488-byte `tdrv_arch_ops` vtable and its `tdrv_arch_register_*` installers; reconcile: that table is the device-driver geometry, this page's `soc_struct_t` (744 B, `*_arch_init`) is the fabric geometry
- [Per-Arch Device Layer: CSR and Register-Offset Accessors](arch-csr-offsets.md) — the per-arch CSR/APB base+offset *values* (the SBUF tile bases `0x2000000000…` indexed by the `sbuf_base_addresses` table here, the `0x800000000000` mesh high-bit)
- [Per-Arch Device Layer: STPB Engine-Init, DGE and Pooling](arch-stpb.md) — the STPB engine-init leaves that consume the partition/port geometry (SBUF→AXI partition mapping, DGE carveout) returned by this band
