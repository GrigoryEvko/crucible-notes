# The 148-Byte Ring Channel Descriptor

> *Populator addresses apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, **not** stripped, DWARF type info recovered, source paths in `.rodata`; `.text` VMA == file offset, every `0x23…`/`0x24…`/`0x25…` is an analysis VMA). Source TU is `/opt/workspace/KaenaRuntime/tdrv/encd.c` (+ `encd/archs/cayman.c`). Dumper addresses apply to `libncfw.so` from the same package (md5 `e01ea384…`, build-id `a98f8e1ca2294582835310c3a1092e0a5e500db5`, SONAME `libncfw.so.2.31.1.0`; symtab present, no DWARF; `.rodata` field-name band `0x650xx` is VMA == file offset).*
> *Evidence grade: **Confirmed (byte-anchored, triple-sourced)** — the wire layout is authored by libnrt's `encd` populator and reflected by libncfw's JSON serializer, and IDA recovered the **typed `algorithm_ring_channel_configs_t` struct (size 148)** on the populator binary; all three agree field-for-field (§7). Other versions will differ. · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

When a NeuronCore runs a ring or kangaring collective, each on-device top-SP (the Xtensa sync core that drives the ring) needs a static, per-channel description of its place in the ring: who its forward and reverse neighbors are, which semaphores it counts on, where its APB-broadcast DMA tail pointers live. That description is the **148-byte `algorithm_ring_channel_configs_t`** descriptor. A fixed array of 32 of them — `ring_channels[32]`, 4736 bytes — is the head of the device-side **`encd_algorithm_configs_t`** (20736 bytes: `ring_channels[32]` @0 + `mesh_events[200]` @4736), uploaded to each top-SP as the **`algo`** config view of the ncfw CC-context. This is the static topology table the firmware reads to walk the ring; it is the ring-algorithm sibling of the per-op [`cc_op_entry`](cc-op-isa.md) scheduler descriptor that rides the same CC-context, and it is what the `cc_op_entry`'s `channel_list` bitmap selects into.

The descriptor is best understood by analogy to a router's per-port forwarding entry: a `{next-hop, prev-hop, four counters, peer block, broadcast tail-pointers}` record, one per channel, indexed by a small channel id. The two `algorithm_ring_neighbor_t` sub-records (forward / reverse) each carry up to three network-index SOC addresses and a *fold factor* that says how many of them are live; the four `semaphore_t` slots are each a single 64-bit top-SP semaphore-register SOC byte address; the 32-byte `kring_peer_semas` block holds the kangaring peer semaphores; and the 20-byte `dma_apb_bcast` tail carries the broadcast DMA tail pointers plus a qbundle/engine mask. The populator (`prep_metaring_topsp_config @0x2331d0`, libnrt) writes every field, resolving semaphore IDs to SOC byte addresses and OR-ing routing/location bits for cross-device neighbors; the dumper (`ncfw_log_algo_ring_channels_configs @0x6020`, libncfw) reflects every field to JSON for context telemetry, read-only.

This page documents three artifacts a reimplementer must reproduce: (1) the **148-byte field layout** of `algorithm_ring_channel_configs_t` and its four nested record types (`algorithm_ring_neighbor_t`, `algorithm_ring_kring_peer_semas_t`, `dma_channel_apb_bcast_t`, `semaphore_t`); (2) the **enclosing `encd_algorithm_configs_t`** — the `ring_channels[32]` array at offset 0, stride 148, and its relationship to the `mesh_events[200]` tail; and (3) the **16-byte runtime channel row**, the *live* per-channel state in top-SP IRAM that the same dumper reflects under a separate JSON key — distinct from the static 148-byte config row.

For reimplementation, the contract is:

- **The 148-byte record** — `next_neigh @0`, `prev_neigh @28`, four 8-byte semas (`recv`/`send`/`post`/`dma_compl`) @56/64/72/80, `kring_peer_semas @88` (32 B), `dma_apb_bcast @120` (20 B), then a 8-byte scalar tail `{channel_id, spad_slot_idx, fold_n, kangaring_is_primary, kangaring_num_peers} @140..144` + `reserved[3] @145`.
- **The array head** — `algorithm_ring_channel_configs_t ring_channels[32]` (4736 B) at offset 0 of `encd_algorithm_configs_t`; the 200-row 80-byte `mesh_events` array follows at +4736 (owned by a sibling page).
- **The semaphore SOC-address model** — each `semaphore_t` is one `uint64 addr.soc_addr`, computed `sp_base + sp_sema_r_ofst(0, sp_idx, sem_id)`, with cross-device neighbor addresses additionally carrying routing/location bits.
- **The 16-byte runtime row** — the separate live-state view (`recv_cnt`/`send_credit`/`repeat_cnt`/`m2s_val`/`s2m_val`/`slot_idx`/`run_state`) the firmware mutates and the dumper reflects.

| | |
|---|---|
| **Descriptor** | `algorithm_ring_channel_configs_t` — **148 bytes** (IDA-typed, libnrt DWARF) |
| **Array** | `ring_channels[32]` — 4736 B (32 × 148), head of `encd_algorithm_configs_t` |
| **Enclosing struct** | `encd_algorithm_configs_t` — 20736 B = `ring_channels[32]@0` + `mesh_events[200]@4736` |
| **Config view** | the `algo` view of each top-SP's ncfw CC-context (`sp_engine->spe_config.algo`) |
| **Populator** | `prep_metaring_topsp_config @0x2331d0` (libnrt), driven by `encd_populate_metaring_topsp_config @0x240bf0` |
| **Dumper** | `ncfw_log_algo_ring_channels_configs @0x6020` (libncfw), reached via `ncfw_log_algo_ring_configs @0x8544` |
| **Stride proof** | dumper loop `148LL * i + a4`, `i = 0..31`; populator `&leader + 148*idx`; IDA array size 4736 |
| **Nested types** | `algorithm_ring_neighbor_t` (28 B) · `algorithm_ring_kring_peer_semas_t` (32 B) · `dma_channel_apb_bcast_t` (20 B) · `semaphore_t` (8 B) |
| **Runtime row** | 16-byte live state, `ncfw_log_algo_ring_ctx @0x16a00`, stride 16 (separate view) |
| **Version lock** | libnrt `dlopen`s libncfw, pins `libncfw_get_version() == 2` (`encd_libncfw_init @0x251cc0`) |
| **Byte-agreement** | populator writes == dumper reads == IDA struct, field-for-field (§7) |

---

## 1. The Descriptor Model

### Purpose

`algorithm_ring_channel_configs_t` is the static, per-channel topology record the on-device top-SP firmware reads to run a ring (or kangaring) collective. It is *static*: written once at NEFF load by the host runtime, never mutated by the host afterward, and idempotent (the populate path is gated by a `channel_config_init` flag, §6). It is *per-channel*: a top-SP can drive up to 32 ring channels, so the record is replicated 32× into `ring_channels[32]`. And it is the **config** half of a two-view-per-channel split: this 148-byte record is the immutable description; a separate **16-byte runtime row** (§5) holds the counters the firmware advances as the ring turns.

A "channel" here is one ring lane on one top-SP. The descriptor tells the firmware everything it needs to participate in that lane: the two ring neighbors (forward `next_neigh`, reverse `prev_neigh`), the four semaphores it waits/posts on (recv counter, send credit, slot-ready post, DMA completion), the kangaring peer semaphores (for the kangaring topology, where a channel has up to three peers instead of two linear neighbors), and the APB-broadcast DMA tail pointers used to fan a reduction across DMA engines.

### Entry Point

Two code lines bracket the descriptor: libnrt populates it, libncfw reflects it.

```text
encd_populate_metaring_topsp_config (0x240bf0)          ── idempotent driver (gate: channel_config_init)
  └─ prep_metaring_topsp_config (0x2331d0)              ── WRITES all 32 × 148B rows
       ├─ encd_arch_get_sp_base_addr / _sp_sema_r_ofst  ── resolve sema id → SOC byte addr
       ├─ set_addr_routing_bits (0x231340)              ── OR routing/location bits (cross-device neighbors)
       ├─ metaring_copy_neighbor_config (inlined)       ── load host neighbor → next_neigh / prev_neigh
       └─ encd_arch_get_apb_bcast_{m2s,s2m}_offsets / _mask  ── dma_apb_bcast tail + qbundle mask
  (prereq: encd_alg_ring_init_channel 0x24d3d0 / encd_alg_kangaring_init_channel 0x24e050
   set neighbors, fold_n, sp_slot_idx, alloc DMA engines, init sema ids = -1/255 "unset")
```

```text
ncfw_log_algo_configs (0x1961c)                          ── CC-context "configs" walk (libncfw)
  └─ ncfw_log_algo_ring_configs (0x8544)                 ── "channels":[] loop, 148*i + base, i=0..31
       └─ ncfw_log_algo_ring_channels_configs (0x6020)   ── READS every field → JSON
            ├─ ncfw_log_algorithm_ring_neighbor (0x37f1) ── ×2 (next_neigh @+0, prev_neigh @+28)
            ├─ ncfw_log_algo_ring_kring_peer_semas (0x4d64)
            ├─ ncfw_log_dma_channel_apb_bcast (0x4899)
            └─ ncfw_log_addr (0x41c3)                     ── "soc_addr":"0x%016lX"
```

> **NOTE —** the dumper is the cleanest available *reflection* of the firmware's read pattern, but it is not the consumer. The on-device top-SP (Xtensa LX IRAM) owns the actual ring walk and the semantics of `run_state`/`slot_idx`; libncfw only mirrors the bytes for host-side context dumps. The descriptor's true executor is firmware, out of scope here (§8). libncfw is `dlopen`'d by libnrt and version-locked to `get_version() == 2` (`encd_libncfw_init @0x251cc0`), so the two libraries share one descriptor contract.

### The config view

The 148-byte record lives at `sp_engine->spe_config.algo.ring_channels[idx]`. The populator computes its base as `&sp_engine->leader + 148 * channel_abs_index` — `leader` sits **176 bytes before** `ring_channels[0]`, so the named-struct offset of any field `F` equals the populator's `leader`-relative byte offset minus 176. This `-176` rebase is the Rosetta stone that maps the populator's raw `v8[...]` accesses onto the IDA-typed field offsets (§7); a reimplementer measuring from `ring_channels[idx]` directly never sees it.

---

## 2. The 148-Byte Field Layout

### Encoding

The descriptor is a flat little-endian struct, no bit-fields. Every offset below is confirmed three ways: the libncfw dumper's `*(…)(a3 + N)` read, the libnrt populator's `&leader + 148*idx + (N+176)` write, and the IDA-recovered `algorithm_ring_channel_configs_t` member at offset `N`. Confidence **HIGH** on every row except the trailing `reserved[3]` (the struct asserts it; neither code line touches it).

| offset | size | field | type | meaning | Confidence |
|---|---|---|---|---|---|
| 0 | 28 | `next_neigh` | `algorithm_ring_neighbor_t` | forward ring neighbor (network-index SOC addrs + fold + type) (§2a) | HIGH |
| 28 | 28 | `prev_neigh` | `algorithm_ring_neighbor_t` | reverse ring neighbor (§2a) | HIGH |
| 56 | 8 | `recv_sema` | `semaphore_t` | RECV-counter semaphore SOC addr (`recv_sema_ids[0]`) | HIGH |
| 64 | 8 | `send_sema` | `semaphore_t` | SEND-credit semaphore SOC addr (`send_sema_id`) | HIGH |
| 72 | 8 | `post_sema` | `semaphore_t` | POST (slot-ready) semaphore SOC addr (`post_sema_ids[0]`) | HIGH |
| 80 | 8 | `dma_compl_sema` | `semaphore_t` | DMA-completion semaphore SOC addr (`compl_sema_id`) | HIGH |
| 88 | 32 | `kring_peer_semas` | `algorithm_ring_kring_peer_semas_t` | kangaring peer sema block: `mine` + `peers[3]` (§2c) | HIGH |
| 120 | 20 | `dma_apb_bcast` | `dma_channel_apb_bcast_t` | APB-broadcast DMA tail ptrs + qbundle/engine mask (§2d) | HIGH |
| 140 | 1 | `channel_id` | `uint8_t` | channel index 0..31 (`channels[i].id`) | HIGH |
| 141 | 1 | `spad_slot_idx` | `uint8_t` | scratchpad slot index (`< ENCD_SPAD_SLOT_MAX_COUNT`) | HIGH |
| 142 | 1 | `fold_n` | `uint8_t` | fold factor (2D-ring DMA fan; `channels[i].fold_n`) | HIGH |
| 143 | 1 | `kangaring_is_primary` | `uint8_t` | kangaring primary flag (KANGARING only, else 0) | HIGH |
| 144 | 1 | `kangaring_num_peers` | `uint8_t` | kangaring peer count ∈ {1, 3} (else 0) | HIGH |
| 145 | 3 | `reserved` | `uint8_t[3]` | padding to 148 | MED |

The dumper reads exactly these offsets — `next_neigh @a3+0`, `prev_neigh @a3+28`, `recv_sema @a3+56`, `send_sema @a3+64`, `post_sema @a3+72`, `dma_compl_sema @a3+80`, `kring_peer_semas @a3+88`, `dma_apb_bcast @a3+120`, then the five scalar tail bytes `@a3+141..144` (and `channel_id` from the loop index, §2e). The populator writes them through the `-176` rebase: `*((_QWORD*)v8 + 29..32) = recv/send/post/compl sema`, `*((_QWORD*)v8 + 37..38) = apb m2s/s2m`, `*((_DWORD*)v8 + 78) = apb mask`, `v8[316..320] = {channel_id, spad_slot_idx, fold_n, kangaring_is_primary, kangaring_num_peers}` — every one of which, minus 176, lands on the IDA offset above.

> **QUIRK —** the four channel semaphores are *not* 8-byte handles with structure — each `semaphore_t` is a single `uint64 addr.soc_addr`, the raw top-SP semaphore-register **SOC byte address** (§3). The 8-byte width is the address, not a `{id, generation}` pair. A reimplementer who reserves bytes for a handle header will overrun the descriptor.

### Algorithm — the scalar-tail pack

The five tail bytes are the channel's identity and topology flags. The populator writes them as a contiguous run after the neighbor/sema/apb body:

```c
// prep_metaring_topsp_config @0x2331d0 (libnrt) — the scalar tail (v8 = &leader + 148*idx)
function pack_channel_tail(v8, ch, host_chan):              // ch = ring_channels[idx]
    v8[316] = host_chan->id                                 // channel_id  (off 140)
    v8[317] = host_chan->sp_slot_idx                        // spad_slot_idx (off 141)  asserted < ENCD_SPAD_SLOT_MAX_COUNT
    v8[318] = host_chan->fold_n                             // fold_n      (off 142)
    if topology == KANGARING:                               // else clear both as one u16:
        v8[319] = kangaring_nbrs[0].is_primary              // kangaring_is_primary (off 143)
        v8[320] = 2 * (ring_nbrs[0].prev.rid != 2) + 1      // kangaring_num_peers ∈ {1,3} (off 144)
    else:
        *(u16 *)&ch.kangaring_is_primary = 0                // zero off 143/144 together
```

> **GOTCHA —** `kangaring_num_peers` is `2 * (prev.rid != 2) + 1`, so it is **1 or 3, never 2**. The value-2 encoding is unreachable: a kangaring channel either has one peer (when `prev.rid == 2`, the pod-node sentinel) or three. A reimplementer treating `num_peers` as a free 0..N count, or writing 2, will mis-drive the peer-sema block (§2c), which is populated only for `num_peers == 3`. For any non-kangaring channel both `kangaring_is_primary` and `kangaring_num_peers` are zeroed as a single 16-bit store.

### (2a) `algorithm_ring_neighbor_t` (28 bytes)

Both `next_neigh` and `prev_neigh` use this layout. It is an array of up to three network-index SOC addresses, a fold count that says how many are live, and a one-byte neighbor type.

| offset | size | field | type | meaning | Confidence |
|---|---|---|---|---|---|
| 0 | 24 | `net_idx_addrs` | `uint64_t[3]` | network-index SOC addrs; **valid count = `fold_n`** | HIGH |
| 24 | 1 | `fold_n` | `uint8_t` | active-entry count (dumper loops `i < fold_n`) | HIGH |
| 25 | 1 | `type` | `uint8_t` | neighbor type — 0 local/none, 1 P2P direct, 2 P2P_POD | MED |
| 26 | 2 | `__reserved` | `uint8_t[2]` | padding to 28 | MED |

The dumper loops `i < *(u8)(neigh + 24)` and reads `*(u64)(neigh + 8*i)` into the `net_idx_addrs` JSON array, so the 24-byte array's *capacity* is 3 and its *live count* is `fold_n`. The `type` value→name mapping is inferred from the populator's branch logic, where `type = port < 0 ? (port == -2 ? 2 : 0) : (port == -2) + 1` and the assert `type != NEIGHBOR_TYPE_P2P_POD || fold_n == 1` gates value 2.

> **CORRECTION (CHAN-1) —** an earlier survey of the populator read a `prev_neigh.recv_sema` write and inferred the neighbor record had an interior `recv_sema` member. The IDA-recovered `algorithm_ring_neighbor_t` has **no** semaphore member — it is `{net_idx_addrs[3], fold_n, type, __reserved[2]}`, 28 bytes, full stop. The decompiler aliased the write expression onto the channel-level `recv_sema @+56` (the outer descriptor field). The neighbor record carries addresses and a fold/type pair only; the four semaphores live in the outer 148-byte record, not inside a neighbor.

### (2c) `algorithm_ring_kring_peer_semas_t` (32 bytes)

The kangaring peer-semaphore block: the channel's own peer sema plus up to three peer semas.

| offset | size | field | type | meaning | Confidence |
|---|---|---|---|---|---|
| 0 | 8 | `mine` | `semaphore_t` | my kangaring peer semaphore SOC addr (`peer_sema_id`) | HIGH |
| 8 | 24 | `peers` | `semaphore_t[3]` | peer semaphore SOC addrs (`peer_id` 0..2) | HIGH |

Populated only for `type == KANGARING`. When `kangaring_num_peers == 3`, `peers[1]` and `peers[2]` are filled (via the rmtv/rmtv2/peer-local resolution paths, each routed through `set_addr_routing_bits`); otherwise only `mine` + `peers[0]` carry addresses and the rest are zero. The dumper reads `mine` from `*peer_block` and each `peers[i]` from `peer_block[i+1]`.

### (2d) `dma_channel_apb_bcast_t` (20 bytes)

The APB-broadcast DMA tail pointers and the qbundle/engine broadcast mask. APB-broadcast is how a reduction fans one source across multiple DMA queue bundles.

| offset | size | field | type | meaning | Confidence |
|---|---|---|---|---|---|
| 0 | 8 | `m2s_tail_ptr` | `addr_t` | APB-bcast mem→SP tail pointer (SOC addr) | HIGH |
| 8 | 8 | `s2m_tail_ptr` | `addr_t` | APB-bcast SP→mem tail pointer (SOC addr) | HIGH |
| 16 | 4 | `mask` | `uint32_t` | qbundle / DMA-engine broadcast mask | HIGH |

The populator derives all three from the channel's used queue-bundles and allocated DMA mask: `m2s_tail_ptr = encd_arch_get_apb_bcast_m2s_offsets(dev, used_qbids)`, `s2m_tail_ptr = …_s2m_offsets(…)`, `mask = …_mask(dev, used_qbids, allocated_dma_mask)`. `used_qbids[8]` and `allocated_dma_mask[8]` are gathered from each `channels[].drv_queues[].qbundle_id` / `dma_engine_id`, with `qbundle_id < ENCD_MAX_DMA_QUEUE_BUNDLES (= 8)` asserted. The 20-byte record sits at `+120..+139`; `+140` (`channel_id`) follows with no inter-struct padding.

### (2e) `channel_id` — struct field vs print source

`channel_id` is a **real struct field at offset 140** (`v8[316] = channels[i].id` in the populator). The dumper, however, prints its value from the **loop index `a4`**, not from `+140`:

```c
// ncfw_log_algo_ring_channels_configs @0x6020 (libncfw) — channel_id from loop arg
snprintf(buf, "%d", a4);                                    // a4 = the i passed by the outer loop
//   under key "channel_id" — NOT a read of *(u8)(a3 + 140)
```

The two are value-identical (the populator writes `id == idx`), so the JSON is correct, but a reimplementer reading the dumper alone would miss the `+140` field. It is confirmed present by the populator write and the IDA struct.

---

## 3. Semaphore SOC-Address Layout

### Purpose

Every `semaphore_t` in the descriptor — the four channel semas (@56/64/72/80), `mine` and `peers[3]` in the kring block, and the neighbor `net_idx_addrs` — is a single `uint64 addr.soc_addr`: a top-SP semaphore-register **SOC byte address**. The descriptor never stores a semaphore *id*; the populator resolves the id to an address at populate time, so the firmware reads a ready-to-use address. This section is the address formula a reimplementer must reproduce.

### Encoding

The address is `sp_base + per-tile-sema-offset`:

```c
// soc_addr = encd_arch_get_sp_base_addr() + encd_arch_get_sp_sema_r_ofst(0, sp_idx, sem_id)
// cayman_get_sp_sema_r_ofst @0x25c130, bar_ofst = 0 path (encd/archs/cayman.c):
function sp_sema_r_ofst(bar_ofst=0, sp_idx, sem_id):
    assert(sp_idx < tdrv_arch_get_num_topsp())            // cayman.c:0x1B5
    tile = sp_idx & 7                                       // low 3 bits select the tile
    ofst  = tile << 30                                      // tile → address bits 30..32
    ofst += (sp_idx > 7) << 47                              // tiles >= 8 add bit 47
    ofst += 4 * sem_id                                      // 4-byte sema slots
    ofst += 4096                                            // sema region base within tile
    return ofst
```

The four channel semas draw their ids from the per-channel id arrays: `recv_sema ← recv_sema_ids[0]`, `send_sema ← send_sema_id`, `post_sema ← post_sema_ids[0]`, `dma_compl_sema ← compl_sema_id`, and the kring `mine ← peer_sema_id`. All ids are initialized to `-1`/`255` ("unset") in `encd_alg_ring_init_channel` before assignment, so an unwritten sema is detectable.

> **NOTE —** the dumper prints the resolved address verbatim as `"soc_addr":"0x%016lX"`; it does **not** re-derive the id. The id→address resolution is a populate-time, host-side computation — the firmware sees only the final SOC byte address. Reimplementing the descriptor without reimplementing this formula yields semaphore addresses the top-SP cannot dereference.

### Cross-device neighbor routing

A neighbor that lives on a *different* device gets routing/location bits OR-ed into its semaphore address by `set_addr_routing_bits @0x231340`:

- **switch-v1 family** → `encd_arch_get_switch_v1_address(dev, addr, d_port, d_rid, d_pod_node_id)` — the remote device's port / ring-id / pod-node id are encoded into the high address bits.
- **otherwise** → `set_loc_bit_arch(addr, mla_location = get_mla_location(d_port, link_idx), !dma_flush)`.

So a neighbor's `recv_sema` address is not a bare local address — its high bits carry the remote device's topology coordinates. The exact bit layout of the routing/location encoding is owned by an arch CSR-map page (§8); this page records only that the neighbor sema addresses pass through it.

---

## 4. The Enclosing `encd_algorithm_configs_t`

The 148-byte channel descriptor is the head element of a larger device-side config block. The block has exactly two members, both fixed arrays, and is the `algo` config view uploaded to each top-SP.

| offset | size | field | type | meaning | Confidence |
|---|---|---|---|---|---|
| 0 | 4736 | `ring_channels` | `algorithm_ring_channel_configs_t[32]` | the 32 ring-channel descriptors (this page) | HIGH |
| 4736 | 16000 | `mesh_events` | `algorithm_mesh_event_configs_t[200]` | 200 mesh-event descriptors, 80 B each (sibling page) | HIGH |
| | **20736** | | | **total `sizeof(encd_algorithm_configs_t)`** | HIGH |

The `ring_channels[32]` stride of 148 is proven three independent ways: the dumper's loop arithmetic `148LL * i + a4` with `i = 0..31` (`ncfw_log_algo_ring_configs @0x8544`), the populator's `&sp_engine->leader + 148 * channel_abs_index`, and the IDA array size `4736 = 32 × 148`. The dumper's last-element comma elision — `if (a4 == 31) "}\n" else "},\n"` — independently pins the count at exactly 32.

The `mesh_events[200]` tail at +4736 is the **mesh**-topology analog: each 80-byte `algorithm_mesh_event_configs_t` carries `dma_apb_bcast[2]` (40 B), `direct_trigger_sema[3]` (24 B), `event_wait_sema` (8 B), `wait_val` (u32), and four trailing `uint8` flags (`event_type`, `dma_trigger`, `direct_trigger`, `wait_event`). It is the mesh-pattern counterpart of `ring_channels` and is documented on its own page; this page owns only the ring half.

> **QUIRK —** the two arrays are *both* fixed-size and *both* present in every config block, regardless of which algorithm a given collective uses — a ring collective still ships the 16000-byte `mesh_events` region (zeroed), and a mesh collective still ships the 4736-byte `ring_channels` region. The block is a union-of-personalities by *coexistence*, not by overlay: ring and mesh descriptors occupy disjoint byte ranges, and the on-device pattern selector (the [`cc_op_entry`](cc-op-isa.md)'s `algo_type`, `ENC_PATTERN_RING = 0` / `ENC_PATTERN_MESH = 1`) chooses which region the firmware reads.

---

## 5. The 16-Byte Runtime Channel Row

### Purpose

The 148-byte record is the *static* description; the firmware also keeps a *live* per-channel state row that it advances as the ring turns. This 16-byte row is **not** part of `algorithm_ring_channel_configs_t` and is **not** in the libnrt type table — it lives in top-SP IRAM, and the only host visibility into it is the libncfw context dumper, which reflects it under a separate JSON key (`ncfw_log_algo_ring_ctx @0x16a00`). It is the running counters: how many recvs have arrived, how much send credit remains, the repeat count, the two APB tail values, and the channel's slot/run state.

### Encoding

32 rows, stride 16. The dumper reads each field at `16*i + base + off`:

| offset | size | field | type | meaning | Confidence |
|---|---|---|---|---|---|
| 0 | 2 | `recv_cnt` | `uint16` | recv-counter live value | HIGH |
| 2 | 2 | `send_credit` | `uint16` | send-credit live value | HIGH |
| 4 | 2 | `repeat_cnt` | `uint16` | repeat counter | HIGH |
| 6 | 2 | `m2s_val` | `uint16` | APB mem→SP tail live value | HIGH |
| 8 | 2 | `s2m_val` | `uint16` | APB SP→mem tail live value | HIGH |
| 10 | 4 | *(gap)* | — | not dumped; purpose unknown | LOW |
| 14 | 1 | `slot_idx` | `uint8` | active scratchpad slot index | HIGH |
| 15 | 1 | `run_state` | `uint8` | channel run-state | MED |

As with the config row, `channel_id` is printed from the loop index, not a stored field, and the last row (`i == 31`) omits its trailing comma — again pinning 32 rows. The 4-byte gap at `+10..+13` is never read by the dumper; its content is firmware-private.

> **GOTCHA —** the config row (148 B) and the runtime row (16 B) are **two different per-channel structures with two different strides**, both reflected by the same library under different JSON keys (`configs → … → channels` for the 148-byte row; `algo → ring → channels` for the 16-byte row). Conflating them — e.g. assuming the runtime counters live inside the 148-byte descriptor — is the central trap of this descriptor family. The static record carries addresses and topology; the runtime row carries the mutable counters the static record's semaphores feed.

---

## 6. Populate vs Consume

### Populate — libnrt `encd` (writer)

The populator authors all 32 rows and is the sole writer of the device struct. It is idempotent: the driver gates it on a per-metaring flag so a second call is a no-op.

```c
// encd_populate_metaring_topsp_config @0x240bf0 — idempotent gate
function populate(metaring):
    if metaring->channel_config_init: return NRT_SUCCESS    // already done
    if prep_metaring_topsp_config(metaring) == NRT_SUCCESS:
        metaring->channel_config_init = 1                   // latch
    return status
```

`prep_metaring_topsp_config @0x2331d0` is the row writer. For each `channel_abs_index` (0..31, asserted ≤ 32 per top-SP), it computes the row base `&leader + 148*idx`, then writes the scalar tail (§2), resolves the four channel semas through `sp_sema_r_ofst + sp_base` (§3), fills the kangaring peer block when `type == KANGARING`, copies the host neighbor state into `next_neigh`/`prev_neigh` (`metaring_copy_neighbor_config`, with the `type != NEIGHBOR_TYPE_P2P_POD || fold_n == 1` assert from `encd.c:0x245A`), and derives the `dma_apb_bcast` tail + mask from the channel's used queue-bundles. The prerequisite channel state — neighbors, `fold_n`, `sp_slot_idx`, DMA-engine allocation, sema ids initialized to `-1` — is set earlier by `encd_alg_ring_init_channel @0x24d3d0` (or `encd_alg_kangaring_init_channel @0x24e050`).

### Consume — libncfw (read-only serializer)

The dumper walks the CC-context and serializes the `algo` config view to JSON for telemetry. It is strictly read-only — libncfw never writes the descriptor.

```text
"channels": [
  { "next_neigh": { fold_n, type, net_idx_addrs:[…] },
    "prev_neigh": { … },
    "recv_sema": { addr:{ soc_addr } }, "send_sema": {…}, "post_sema": {…}, "dma_compl_sema": {…},
    "kring_peer_semas": { mine:{ addr:{ soc_addr } }, peers:[ { peer_id, addr:{soc_addr} } × 3 ] },
    "dma_apb_bcast": { m2s_tail_ptr:{soc_addr}, s2m_tail_ptr:{soc_addr}, mask },
    channel_id, spad_slot_idx, fold_n, kangaring_is_primary, kangaring_num_peers },
  … × 32 ]
```

The field-name keys are `.rodata` string literals in libncfw, repeated **four times** (one band per arch); the first band sits at `~0x650xx`. The verbatim keys and their byte offsets: `net_idx_addrs @0x650ef`, `soc_addr @0x6511c`, `m2s_tail_ptr @0x65150`, `s2m_tail_ptr @0x6515d`, `peer_id @0x65181`, `recv_sema @0x651a4`, `send_sema @0x651ae`, `post_sema @0x651b8`, `dma_compl_sema @0x651c2`, `kring_peer_semas @0x651d1`, `dma_apb_bcast @0x651e2`, `channel_id @0x651f0`, `spad_slot_idx @0x651fd`, `kangaring_is_primary @0x6520d`, `kangaring_num_peers @0x65224`, `channels @0x6523a`. Addresses print as `"0x%016lX"`.

> **NOTE —** the direction is one-way: `encd` (libnrt) **writes** the device struct, libncfw (dlopen'd by libnrt, `get_version() == 2`) **dumps** it. The four per-arch string bands and the four arch dumper clones mirror the [serializer-families](../firmware/serializer-families.md) pattern — the per-arch context dumpers are byte-identical in decode and differ only in which arch's CC-context they are reached from.

---

## 7. Populator ↔ Dumper ↔ Struct Agreement

The descriptor is authored by libnrt, reflected by libncfw, and typed by IDA, and the three agree field-for-field. The populator measures from `&leader` (176 bytes before `ring_channels[idx]`); the dumper measures from `ring_channels[idx]` (= `a3`); the IDA struct offsets are relative to `ring_channels[idx]`. Subtract 176 from the populator offset and all three coincide.

| field | dumper read (`a3 +`) | populator write (`&leader +`, −176) | IDA offset | agree |
|---|---|---|---|---|
| `next_neigh` | +0 | neighbor copy → +0 | 0 | **yes** |
| `prev_neigh` | +28 | neighbor copy → +28 | 28 | **yes** |
| `recv_sema` | +56 | `*((u64*)v8 + 29)` → +56 | 56 | **yes** |
| `send_sema` | +64 | `*((u64*)v8 + 30)` → +64 | 64 | **yes** |
| `post_sema` | +72 | `*((u64*)v8 + 31)` → +72 | 72 | **yes** |
| `dma_compl_sema` | +80 | `*((u64*)v8 + 32)` → +80 | 80 | **yes** |
| `kring_peer_semas` | +88 | `.kring_peer_semas.mine…` → +88 | 88 | **yes** |
| `dma_apb_bcast` (m2s) | +120 | `*((u64*)v8 + 37)` → +120 | 120 | **yes** |
| `dma_apb_bcast` (s2m) | +128 | `*((u64*)v8 + 38)` → +128 | 128 | **yes** |
| `dma_apb_bcast` (mask) | +136 | `*((u32*)v8 + 78)` → +136 | 136 | **yes** |
| `channel_id` | (loop arg) | `v8[316]` → +140 | 140 | **yes** |
| `spad_slot_idx` | +141 | `v8[317]` → +141 | 141 | **yes** |
| `fold_n` | +142 | `v8[318]` → +142 | 142 | **yes** |
| `kangaring_is_primary` | +143 | `v8[319]` → +143 | 143 | **yes** |
| `kangaring_num_peers` | +144 | `v8[320]` → +144 | 144 | **yes** |

> **Verification —** the three sources are mutually independent: the dumper is in a *different binary* (libncfw) from the populator (libnrt), and the IDA type comes from libnrt's DWARF, not from either code path's arithmetic. They were recovered separately and converge on the identical 148-byte layout, including the 32-byte kring block, the 20-byte apb tail, and the five-byte scalar run. The only field not read by the dumper is `reserved[3] @145` (the struct asserts it; both code lines skip it — MED), and the only field the dumper sources differently is `channel_id` (loop arg vs `+140`, value-identical — §2e).

---

## 8. Considerations

- **On-device firmware semantics.** The top-SP Xtensa LX IRAM owns the actual ring walk: how it dereferences `next_neigh`/`prev_neigh`, when it waits on `recv_sema` vs posts `post_sema`, how `fold_n` fans the 2D-ring DMA, and the value space of the runtime row's `run_state` / `slot_idx`. The two host binaries populate and reflect the descriptor; they do not execute it. The executor is out of scope — see [The NCFW Sequencer](../firmware/ncfw-sequencer.md). *(layout: HIGH; firmware behavior: LOW/UNKNOWN.)*
- **The neighbor `type` enum.** The `0 = local`, `1 = P2P`, `2 = P2P_POD` mapping is inferred from the populator's `port < 0 ? (port == -2 ? 2 : 0) : (port == -2) + 1` branch and the `type != NEIGHBOR_TYPE_P2P_POD || fold_n == 1` assert, not from a recovered named enum. The named `NEIGHBOR_TYPE_*` values and their tie to the `-2` (`NEC_POD_MLA_DEV`) sentinel belong to a ring-topology page. *(MED.)*
- **The semaphore routing-bit encoding.** `set_addr_routing_bits` / `set_loc_bit_arch` / `encd_arch_get_switch_v1_address` encode the remote device's port / ring-id / pod-node / mla-location into a neighbor sema address's high bits. The exact bit positions are the primary reimplementation artifact for cross-device collectives and are owned by an arch CSR / SOC-address page, not decoded here. *(HIGH that they are OR-ed in; the bit layout is out-of-cell.)*
- **`spad_slot_idx` bounds.** `spad_slot_idx` is asserted `< ENCD_SPAD_SLOT_MAX_COUNT`; the scratchpad-slot map (`encd_arch_get_ncfw_ctx_basic_block_{ctrl,slot}_spad_offset`) that gives each slot its SPAD offsets is a separate scratchpad-layout concern.
- **APB-broadcast mask derivation.** `encd_arch_get_apb_bcast_{m2s,s2m}_offsets` / `_mask` derive the tail pointers and mask from `used_qbids` + `allocated_dma_mask` per-arch (cayman / mariana / mariana_plus). The per-arch derivation is a DMA-APB-broadcast concern; this page records only that the descriptor's `dma_apb_bcast` field is its output.
- **Per-arch leaf identity.** The cayman / mariana / mariana_plus dumper leaves and sema formulas are clone bodies; per-arch byte-identity is assumed from the ×4 string repetition and the shared `encd_arch_*` dispatch, not byte-diffed for every arch. *(MED.)*

---

## Related Components

| Name | Relationship |
|---|---|
| `prep_metaring_topsp_config` (`@0x2331d0`, libnrt) | The writer of all 32 × 148-byte rows; resolves semas and neighbor routing |
| `encd_populate_metaring_topsp_config` (`@0x240bf0`, libnrt) | The idempotent driver (gate `channel_config_init`) |
| `ncfw_log_algo_ring_channels_configs` (`@0x6020`, libncfw) | The per-channel JSON serializer (read-only reflection) |
| `ncfw_log_algo_ring_ctx` (`@0x16a00`, libncfw) | The 16-byte runtime-row serializer (separate live-state view) |
| `cayman_get_sp_sema_r_ofst` (`@0x25c130`, libnrt) | The sema-id → SOC-byte-address offset formula |
| `set_addr_routing_bits` (`@0x231340`, libnrt) | OR-s remote-device routing/location bits into neighbor sema addresses |
| `encd_libncfw_init` (`@0x251cc0`, libnrt) | The `dlopen` bridge that version-locks populator and dumper (`get_version == 2`) |

## Cross-References

- [The cc_op_entry On-Device ISA](cc-op-isa.md) — the per-op scheduler descriptor on the same CC-context; its `channel_list` bitmap selects these channels, and its `algo_type` (`ENC_PATTERN_RING = 0` / `ENC_PATTERN_MESH = 1`) chooses ring vs mesh
- [Serializer Families (per-arch CC-context dumpers)](../firmware/serializer-families.md) — the four arch clones of the JSON serializer that reach this channel dumper, and the ×4 `.rodata` key bands
- [encd: Device-Side Descriptor Emitter](encd-overview.md) — the `encd` populate path (`prep_metaring_topsp_config`) and the host metaring graph that feeds it
- [Algorithm Taxonomy (Ring / Mesh / Hier / Kangaring / RDH)](algorithm-taxonomy.md) — the host `enc_alg_type` composer axis; KANGARING is what populates the peer-sema block
- [The NCFW Sequencer (Xtensa LX Disassembly)](../firmware/ncfw-sequencer.md) — the on-device top-SP executor that reads this descriptor and owns the runtime-row semantics
- [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md) — the DMA descriptor wire format whose tail pointers the `dma_apb_bcast` field references
- [back to index](../index.md)
