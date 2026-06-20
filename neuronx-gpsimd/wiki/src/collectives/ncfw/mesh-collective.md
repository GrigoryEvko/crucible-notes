# NCFW Mesh Collective

This page documents the **NCFW mesh-based collective algorithm** — the second of
the three NeuronCore-management-firmware collective frameworks (ring, mesh,
hierarchical). Where the [ring/kangaring](./ring-kangaring.md) algorithm runs **32
concurrent directed rings** with a per-channel credit scoreboard, **mesh** is a
**flat ordered event tape**: a fixed-stride array of 80-byte micro-program steps,
each *"wait on a semaphore to reach a value, then fire ≤2 broadcast-DMA legs and
≤3 direct-trigger semaphores at neighbour dies"*, walked by a single `u16` program
counter. The mesh's multi-dimensional reduce over the 64-die Cayman d2d fabric is
**flattened into this linear tape at NEFF build time**; the firmware just steps it.

The page owns four things, each byte-grounded:

1. the **mesh event-tape algorithm** — the 80-byte event struct, its fields, and
   the per-event WAIT → MOVE → SIGNAL → ADVANCE step;
2. the **per-arch ×4 event-count capacity table**, *correctly codename-labelled*
   (this is the **C1 inversion trap** — see the CORRECTION below);
3. the **event-tape stepping** on the device — the scalar Xtensa-LX
   semaphore-WAIT / fenced-SIGNAL / atomic-snapshot leaf primitives the tape
   composes; and
4. the **cross-die mesh traversal** — how each event's semaphore SoC-addresses
   route to neighbour dies through the `io_d2d` fabric.

The structural recovery is from the **host-side `ncfw_log_*` mesh decoders** in
`libncfw.so` (the JSON pretty-printers that walk the firmware's DRAM-resident
collective structs, so every offset/count below is read from the byte the matching
`snprintf()` loads); the device-side step machine is from a **scalar-Xtensa-LX
re-decode of the carved NCFW IRAM blobs**.

> **GOTCHA — this is the NCFW *management* core, a scalar Xtensa-LX, not the
> Vision-Q7 "Cairo" FLIX DSP.** Two different Xtensa cores live in the GPSIMD
> estate. The user-kernel **datapath** is the Vision-Q7 NX (`ncore2gp`, 512-bit
> SIMD, real FLIX), whose ucode ships separately. The core *here* is its quieter
> sibling: a **scalar Xtensa-LX control core** whose firmware ships as four IRAM
> images in `libncfw.so`. Decode its device loop bodies under the **scalar-LX
> length rule** (`op0 e/f = 3-byte / else 2-byte`, resync at `retw.n`) via
> `xtensa-elf-objdump XTENSA_CORE=ncore2gp` — **never** the FLIX bundle decoder.
> See [The NCFW Scalar-LX Management Core](../../uarch/ncfw-lx-core.md).

**Provenance & confidence.** Every fact below is read **this session** from the
shipped host library
`libncfw.so` (sha256 `598920d743762c03…`, BuildID `a98f8e1c…`, SONAME
`libncfw.so.2.31.1.0.cf13a49f`, 615 640 B — all four identity anchors re-verified)
with stock binutils (`readelf`/`nm`/`objdump -M intel`/`sha256sum`) and a native
Cadence `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) on the carved IRAM blob.
Lawful interoperability reverse engineering (DMCA 17 U.S.C. 1201(f)); no vendor
source snapshot consulted. Tags are `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` per
the [Confidence & Walls Model](../../reference/confidence-model.md): `OBSERVED` = a
byte/size/symbol/disasm read from the binary this pass; `CARRIED` = OBSERVED in a
cited prior carve and reused; `INFERRED` = reasoned over those. Callouts: **QUIRK**
(counter-intuitive but real), **GOTCHA** (a reimplementation trap), **NOTE**
(orientation), **CORRECTION** (overturns a prior reading).

> **THE v5 / MAVERICK WALL — read before any v5 claim.** `libncfw` ships **exactly
> four** NCFW generations (v2/v3/v4/v4+). There is **no MAVERICK (v5) NCFW image**
> in this binary — `nm libncfw.so | rg -i 'maverick|v5|tonga'` returns **zero
> symbols**, and the `arch_id` selectors compare exactly `{0x05,0x0c,0x14,0x1c}`
> with no `0x24` (36) leg. MAVERICK = NC-v5 has **NCFW ABSENT**; any statement
> about a v5 NCFW mesh *interior* is structurally unverifiable here and is flagged
> **INFERRED / ABSENT**, never stated as fact. `[HIGH/OBSERVED — negative.]`

---

## 1. The mesh in one paragraph

The NCFW collective config is **not** a true C union of `{ring, mesh,
hierarchical}` — it is a **struct with three adjacent sub-regions**, all emitted by
the same logger `ncfw_log_algo_configs`. Mesh occupies the middle region:

```
algo_configs struct (cfg ptr passed to ncfw_log_algo_configs):
  +0x0000  RING   configs   (32 channels × 149 B)   -> ncfw_log_algo_ring_configs
  +0x1280  MESH   configs   (N events  ×  80 B)     -> ncfw_log_algo_mesh_configs
  +0x1280+sizeof(mesh)  HIERARCHICAL configs        -> ncfw_log_algo_hierarchical_configs
```

`[HIGH/OBSERVED — lea rdx,[rax+0x1280] before the mesh sub-call @0x197cf (SUNDA) /
@0x32576 (CAYMAN), and lea rdx,[rax+0x2220] (SUNDA, @0x197f3) / [rax+0x3440]
(CAYMAN, @0x3259a) before the hierarchical sub-call.]`

Mesh is a **flat ordered sequence of fixed 80-byte events**. Each event is one
node in a dependency tape: a guarded fanout that waits on one inbound semaphore
reaching a count and grants up to three outbound credits to neighbour dies. The
runtime state is a **single `u16` cursor** (`event_index`), versus the ring's
512-byte 32-channel scoreboard — the mesh has no per-channel flow-control counters
because it is strictly sequential; the semaphore guards *inside* each event are the
flow control.

---

## 2. The decoder call tree (mesh path)

All `OBSERVED HIGH`, re-traced this session. The decoder symbols appear **4×** in
`libncfw` (one copy per arch); the SUNDA-copy addresses are shown.

```
ncfw_log_algo_configs   (struct: ring | mesh | hierarchical)
  └─ ncfw_log_algo_mesh_configs   @0x19365 (SUNDA), 0x2b7 B
     │   (thin wrapper: moves the per-arch event COUNT into r8d, then calls:)
     └─ ncfw_log_configs_algo_mesh_events   @0x8d82 (SUNDA), 0x1fb1 = 8113 B
            for (i = 0; i < count; i++):              // count = arg5 in r8, per-arch (§4)
              event = cfg + i*0x50                    // 80-byte stride (§3)
              ├─ for j in 0..1:  ncfw_log_dma_channel_apb_bcast @0x4899   (dma_apb_bcast_%d)
              ├─ for k in 0..2:  emit direct_trigger_sema_%d  (soc_addr u64)
              ├─ emit event_wait_sema  (soc_addr u64)
              ├─ emit wait_val (u32)
              └─ emit event_type / dma_trigger / direct_trigger / wait_event (4× u8)

ncfw_log_algo_ctx       (struct: ring_ctx | hier_ctx | mesh_ctx)
  └─ ncfw_log_algo_mesh_ctx   @0x1884c (SUNDA)   (single field: event_index u16)

ncfw_log_spad_ctrl_cc_op_entry   @0x1840
  (algo_type/sub_type selector + the ring/mesh UNION overlay at struct +0x3, §6)
```

The four decoder copies are **byte-size-identical** across arches:
`ncfw_log_configs_algo_mesh_events` = `0x1fb1` at all four addresses
(@0x8d82 / @0x21b29 / @0x3a8d0 / @0x53677), and `ncfw_log_algo_mesh_configs` =
`0x2b7` at all four (@0x19365 / @0x3210c / @0x4aeb3 / @0x63c5a). **Only relocated
string/data immediates and the event COUNT differ** — the mesh event *schema* is
uniform across all archs; only the *capacity* scales (§4). `[HIGH/OBSERVED —
nm -nS sizes this session.]`

---

## 3. The mesh event struct — 80 bytes (`0x50`), field-exact

`ncfw_log_configs_algo_mesh_events(buf, indent, enable_flag, cfg, count)`,
SUNDA copy @0x8d82. Per-event base = `cfg + i*0x50`. **The 80-byte stride is
byte-proven** by the index arithmetic at `0x8f06..0x8f13`:

```asm
8f06:  movsxd rdx,eax        ; rdx = i
8f0c:  shl    rax,0x2        ; 4·i
8f10:  add    rax,rdx        ; 4·i + i = 5·i
8f13:  shl    rax,0x4        ; (5·i) << 4 = 80·i   => stride = 0x50 = 80 B, EXACT
```

`[HIGH/OBSERVED.]` All field offsets read directly from the byte the matching
`snprintf` loads:

| off | size | field | decoded by / evidence |
|---|---|---|---|
| `+0x00` | `0x14` | `dma_apb_bcast_0` | `dma_apb_bcast` (20 B, §3.1); inner loop j=0..1 |
| `+0x14` | `0x14` | `dma_apb_bcast_1` | elem base = `event + j*20`; key `"dma_apb_bcast_%d"` @0x65246 |
| `+0x28` | `8` | `direct_trigger_sema_0` | `{addr:{soc_addr:u64}}` |
| `+0x30` | `8` | `direct_trigger_sema_1` | u64 SOC phys addr |
| `+0x38` | `8` | `direct_trigger_sema_2` | inner loop k=0..2; `r12 = [event + (k+4)*8 + 8]` @0x9454; key `"direct_trigger_sema_%d"` @0x65257 |
| `+0x40` | `8` | `event_wait_sema` | `{addr:{soc_addr:u64}}`; `mov r12,[event+0x40]` @0x9a17; key @0x6526e |
| `+0x48` | `4` | `wait_val` | u32; `mov ebx,[event+0x48]` @0x9e2b; fmt `"%u"` |
| `+0x4c` | `1` | `event_type` | u8; `movzx [event+0x4c]` @0xa06c; key @0x65289; = `enc_mesh_event_type` (§5) |
| `+0x4d` | `1` | `dma_trigger` | u8; `movzx [event+0x4d]` @0xa2b1; key @0x65296 |
| `+0x4e` | `1` | `direct_trigger` | u8; `movzx [event+0x4e]` @0xa4f6; key @0x652a4 |
| `+0x4f` | `1` | `wait_event` | u8; `movzx [event+0x4f]` @0xa73b; key @0x652b5 |

**Offset accounting:** `2·0x14 (0x28) + 3·8 (→0x40) + 8 (→0x48) + 4 (→0x4c) + 4·1
(→0x50) = 0x50 = 80 B`. **EXACT, no padding.** `[HIGH/OBSERVED.]` Every JSON key
string above is present in `.rodata` (verified by `rg -oa`).

The inner-loop bounds are byte-proven: `dma_apb_bcast` slots `cmp …,0x1 ; jle`
@0x90c0 (**2 slots**), `direct_trigger_sema` slots `cmp …,0x2 ; jle` @0x9722
(**3 slots**); the sema element address `movsxd rdx,edx ; add rdx,0x4 ; mov
r12,[rax+rdx*8+0x8]` @0x944d = `[event + 0x28 + 8·k]`. `[HIGH/OBSERVED.]`

As an annotated C reconstruction (real symbol/offset named):

```c
// mesh event descriptor — 80 bytes, stride proven 80·i @0x8f06..0x8f13
typedef struct ncfw_mesh_event {            // emitted by ncfw_log_configs_algo_mesh_events
    dma_apb_bcast_t dma_apb_bcast[2];       // +0x00, +0x14  (20 B each, §3.1)
    uint64_t        direct_trigger_sema[3]; // +0x28..+0x3f  SOC phys addrs (neighbour-die §7)
    uint64_t        event_wait_sema;        // +0x40         SOC phys addr (the inbound guard)
    uint32_t        wait_val;               // +0x48         counted-wait target
    uint8_t         event_type;             // +0x4c         enc_mesh_event_type (§5)
    uint8_t         dma_trigger;            // +0x4d         do the DMA legs this event?
    uint8_t         direct_trigger;         // +0x4e         do the sema fanout this event?
    uint8_t         wait_event;             // +0x4f         do the inbound wait this event?
} ncfw_mesh_event_t;                        // sizeof == 0x50
```

> **NOTE — field semantics.** The four offsets/counts are `OBSERVED HIGH`; the
> *roles* are `INFERRED MED` from name + width + the host PollSem/DmaTrigger
> lowering (§5 of the device cross-check). `event_wait_sema` + `wait_val` are the
> step's **inbound guard** (a counted wait-ge); `dma_apb_bcast_0/1` are up to two
> APB-broadcast DMA legs (e.g. one inbound reduce + one outbound forward, or two
> mesh dimensions); `direct_trigger_sema_0..2` are up to three neighbour-die
> semaphores this event SIGNALS after its DMA completes. The three trigger/wait
> u8s are enable/mode bits gating each phase.

### 3.1 `dma_apb_bcast` (20 B) — shared verbatim with the ring path

Decoded by `ncfw_log_dma_channel_apb_bcast` @0x4899 — the **identical** function the
ring channel uses; mesh reuses the ring's APB-broadcast DMA descriptor verbatim.

| off | size | field | evidence |
|---|---|---|---|
| `+0x00` | `8` | `m2s_tail_ptr` | `addr.soc_addr` u64 (memory→semaphore ring tail ptr) |
| `+0x08` | `8` | `s2m_tail_ptr` | `addr.soc_addr` u64 (semaphore→memory ring tail ptr) |
| `+0x10` | `4` | `mask` | u32; `mov ebx,[+0x10]` @0x4b70 |

`[HIGH/OBSERVED — key strings `m2s_tail_ptr`/`s2m_tail_ptr`/`mask` present; same
decoder as ring.]`

---

## 4. Per-arch ×4 mesh capacity — the event-count table

The `events[]` count is a per-arch immediate moved into `r8d` by
`ncfw_log_algo_mesh_configs` *before* calling the events decoder. Each row's
codename is bound by the **end-to-end `ctx_log` chain** (not a positional guess):
the per-codename `<codename>_ncfw_ctx_log` reaches exactly one
`ncfw_log_algo_configs`, which calls exactly one `ncfw_log_algo_mesh_configs`.

| arch_id | codename (NC-v#) | `mesh_configs` @ | count `mov r8d,IMM` (addr) | `events[]` count |
|---|---|---|---|---|
| `0x05` | **SUNDA** (v2) | `0x19365` | `mov r8d,0x32` @0x19502 | `0x32` = **50** |
| `0x0c` | **CAYMAN** (v3) | `0x3210c` | `mov r8d,0x6c` @0x322a9 | `0x6c` = **108** |
| `0x14` | **MARIANA** (v4) | `0x4aeb3` | `mov r8d,0x6c` @0x4b050 | `0x6c` = **108** |
| `0x1c` | **MARIANA_PLUS** (v4+) | `0x63c5a` | `mov r8d,0x6c` @0x63df7 | `0x6c` = **108** |

`[ALL OBSERVED HIGH — each immediate re-read this session via objdump.]` v5/MAVERICK
has **no row** — NCFW absent (the v5 wall).

> **CORRECTION — the C1 codename inversion. DO NOT REPEAT.** An earlier copy of
> this table printed the codename labels **INVERTED** — it bound `0x14 → CAYMAN`
> and `0x0c → MARIANA`. **That is WRONG.** The arch_id keys (`0x05/0x0c/0x14/0x1c`)
> and the counts (`50 / 108×3`) were always right; only the *codename text* on the
> `0x0c` / `0x14` rows was swapped. The **correct, binary-grounded** binding is:
>
> > **`arch_id 0x05 = SUNDA / 0x0c = CAYMAN / 0x14 = MARIANA / 0x1c = MARIANA_PLUS`**
>
> verified this session against the `libncfw_ctx_log` dispatch ladder @0x1309:
>
> ```asm
> 133c: cmp [arch_id],0x05 ; 1340: je 0x134a -> 135c: call sunda_ncfw_ctx_log        @0x1a12b
> 1342: cmp [arch_id],0x0c ; 1346: je 0x1363 -> 1375: call cayman_ncfw_ctx_log       @0x32ed2
> 1330: cmp [arch_id],0x14 ; 1334: je 0x137c -> 138e: call mariana_ncfw_ctx_log      @0x4bc79
> 1324: cmp [arch_id],0x1c ; 1328: je 0x1395 -> 13a7: call mariana_plus_ncfw_ctx_log @0x64a20
> default                                     -> 13ae: mov eax,0x16  (22 == EINVAL)
> ```
>
> Three independent in-binary anchors agree and **none was copied from a sibling
> report**: (1) the `ctx_log` dispatch above; (2) the `libncfw_get_image` @0x1179
> selector, where `cmp 0x05`→loads `v2_ncfw_*_bin`, the `0x0c` leg loads
> `v3_ncfw_*_bin` (@0x74a40), the `0x14` leg loads `v4_ncfw_*_bin` (@0x7e440); and
> (3) the source-file strings in address order `sunda.c (0x954f9) < cayman.c
> (0x95923) < mariana.c (0x9592c) < mariana_plus.c (0x95936)` — the same order as
> both the per-codename `ctx_log` symbols and the DRAM-blob symbols. `[HIGH/OBSERVED
> — triple-anchored this session.]` The binding is **`0x0c = CAYMAN/v3`,
> `0x14 = MARIANA/v4`**. Carry it; never re-introduce the
> swap. (coretype = arch_id + 1: SUNDA 6, CAYMAN 13, MARIANA 21, MARIANA_PLUS 29.)

**Cross-check on the region size** (the `ncfw_log_algo_configs` leas, correctly
labelled): SUNDA mesh region = `0x2220 − 0x1280 = 0xFA0` = 4000 = **50 × 80**;
CAYMAN/MARIANA/MARIANA_PLUS mesh region = `0x3440 − 0x1280 = 0x21C0` = 8640 =
**108 × 80**. Both confirm the 80-byte stride **and** the per-arch count
independently of the `mov r8d` immediate. `[HIGH/OBSERVED.]`

> **QUIRK — the 50-vs-108 split is the generation boundary.** SUNDA (v2) is the
> earlier, smaller-fabric generation: it caps the mesh at 50 events, the same split
> seen in the DRAM image sizes (SUNDA DRAM `0x36c0` vs the others `0x4e00`) and the
> `algo_configs` total (`0x2228` SUNDA vs `0x3448` the others). The three modern
> archs share an identical 108-event capacity. `[HIGH/OBSERVED.]`

---

## 5. The event-tape kinds, sub-types, and selector (cross-page enums)

The numeric `algo_type` that selects MESH is **not** recoverable from `libncfw`
alone — there is no value→name table and no gating branch; `libncfw` prints the raw
integers as `%hhu`/`%hu`. The runtime enums live in `libnrt.so` and are pinned,
byte-exact, in [`collective-enums.md`](../ops/collective-enums.md). Cite from there:

- **`enc_alg_type`** (the `algo_type` nibble, bits[0:3]) — `RING=0 HIER=1 MESH=2
  KANGARING=3 SINGLE_CYCLE_RING=4 INTRA_RDH=5 SINGLE_STEP_MESH=6 INTER_RDH=7
  TWO_STEP_POD_MESH=8 LATENCY_OPT_MESH=9 **BW_OPT_MESH=10** INVALID=11`. The **five
  mesh-family `algo_type` values** that all drive the event tape decoded here are
  `MESH=2, SINGLE_STEP_MESH=6, TWO_STEP_POD_MESH=8, LATENCY_OPT_MESH=9,
  BW_OPT_MESH=10`.
- **`enc_alg_mesh_type`** (the mesh SUB-type = `algo_sub_type`, bits[4:6]) —
  `FULL_MESH=0 GROUPED_MESH=1 MESH_TRN2=2 MESH_SWITCH=3 MESH_INVALID=4` (4 real
  sub-kinds 0..3).
- **`enc_mesh_event_type`** (the per-event `event_type` u8 at struct +0x4c) — the
  full **61-enumerator** body, phase-partitioned `COMMON 0..7 / **MESH 8..13** /
  A2A 14..22 / RDH 23..52`. The MESH event band (the kinds this tape's own band
  drives) is exactly `EVT_REDUCE_COPY=8, EVT_REDUCE_COPY_2=9, EVT_REDUCE_WRITE=10,
  EVT_INTER_GRP_BRDCST_2=11, EVT_LOCAL_AND_POD_GRP_BRDCST=12 (…_2=13)`; the COMMON
  band 0..7 (`EVT_SYNC=0 … EVT_REDUCE_LOCAL_HNDSHK=6 EVT_INTRA_GRP_BRDCST=7`) is
  shared with the other algorithms. The `EVT_FUNCTION_BARRIER_FIRST_COLL=4` /
  `_LAST_COLL=5` events tie to the NCFW function-barrier. The
  [`ENC_ALLTOALL_V=12`](../ops/collective-enums.md) op type drives the A2A band.

`[enum bodies HIGH/OBSERVED per collective-enums.md (libnrt DWARF); the binding
"struct +0x4c `event_type` == `enc_mesh_event_type`" is INFERRED MED — same name,
same 1-byte width, but `libncfw` prints it raw with no name table.]`

> **NOTE — `algo_type` = "how to route"; `SDMA_CCETYPE` = "what to compute".** The
> mesh `cc_op` word carries no reduce-op field. The add/max/min arithmetic rides
> the SDMA CCE descriptor (`SDMA_CCETYPE`), *separate* from the mesh routing
> selector. See [`collective-enums.md §7`](../ops/collective-enums.md).

---

## 6. Ring vs mesh selection — the `cc_op` `+0x3` union overlay

`ncfw_log_spad_ctrl_cc_op_entry` @0x1840 reads the firmware's per-step command
word. Byte 0 holds the selector nibbles: `algo_type = bits[0:3]` (`and 0xf` @0x1a43),
`algo_sub_type = bits[4:6]` (`shr al,0x4 ; and 0x7` @0x1c9a/0x1c9d), `trigger_next =
bit[7]`. `[HIGH/OBSERVED.]`

The `+0x3` word is a **true union**, dumped by the logger under *both*
interpretations (no gating branch — the firmware disambiguates by `algo_type`):

```c
// cc_op entry +0x3, selected by algo_type:
union {
    uint32_t channel_list;        // RING view: bitmask of the 32 ring channels
                                  //   mov ebx,[+0x3] @0x2a64; key "channel_list" @0x65092
    struct {                      // MESH view:
        uint16_t sema_shift_offset;  // +0x3   movzx WORD [+0x3] @0x2eb6; key @0x650a6
        uint16_t sema_mask;          // +0x5   movzx WORD [+0x5] @0x30da; key @0x650be
    };
};
```

`[HIGH/OBSERVED — both `movzx WORD PTR [rax+0x3]` and `[rax+0x5]` re-read this
session; same 4 bytes are either one ring channel bitmap or two mesh u16s.]`

> **NOTE — mesh derives neighbour sema slots arithmetically.** Where the ring
> carries an explicit `channel_list` bitmap, the mesh carries
> `sema_shift_offset` + `sema_mask`: a base semaphore index is shifted by
> `sema_shift_offset` and masked with `sema_mask` to compute this node's slot —
> the mesh's regular-grid stride analog of the ring's explicit channel list. The
> u16/u16 existence is `OBSERVED HIGH`; the "shift-base, mask → per-node slot" use
> is `INFERRED MED`. The `cc_op` word schema is canonical in
> [`collective-enums.md §7`](../ops/collective-enums.md).

---

## 7. Cross-die mesh traversal — the d2d neighbour fabric

Each event's semaphore targets (`event_wait_sema`, `direct_trigger_sema_0..2`) and
DMA tail-ptrs (`dma_apb_bcast` `m2s`/`s2m`) are **64-bit SOC physical addresses**.
The high bits select the **neighbour die** in the 64-die Cayman mesh. The SoC-address
fold is canonical in the committed cross-die page
[`rdma-cross-die.md`](../../dma/rdma-cross-die.md); the relevant layout:

```
[46:0]  LOCAL            per-die 128 TiB local byte address
[47]    DIE              1 of 2 dies per package
[53:48] CAYMAN_ID        6-bit chip id  -> 2^6 = 64 dies in the mesh
[54]    CAYMAN_ID_VALID  =1 -> route by chip id; =0 -> stay on chip
```

When an event targets a **neighbour** die, the `[53:48]` window is reinterpreted by
the d2d neighbour decoder (`cayman_addr_decode_neighbor`) as per-direction routing
flags: `[50] EXIT_SENG, [51] EXIT_DIE, [52] NEIGHBOR_ROUTE, [54] ID_VALID` (bit 54
is **shared** with `CAYMAN_ID_VALID`). The named destination block is `io_d2d` — the
die-to-die fabric. `[bit layout HIGH/OBSERVED — verbatim from the committed
rdma-cross-die page; cited, not re-derived here.]`

So a mesh step reduces across the d2d fabric **one neighbour at a time**:

```c
// One mesh event = one neighbour-hop of a multi-dimensional reduce.   [INFERRED MED]
//   data flow flattened into the events[] tape at NEFF build time.
void mesh_event_step(ncfw_mesh_event_t *e) {
    if (e->wait_event)                         // §3: the inbound guard
        wait_ge(e->event_wait_sema, e->wait_val);   // data arrived from prev neighbour axis
    if (e->dma_trigger)                        // reduce/forward the chunk over io_d2d
        for (int j = 0; j < 2; j++)            //   to the next neighbour (soc_addr sets
            fire_apb_bcast(&e->dma_apb_bcast[j]);    //   CAYMAN_ID_VALID + EXIT_DIE/NEIGHBOR_ROUTE)
    if (e->direct_trigger)                     // grant credits to <=3 neighbour-axis peers
        for (int k = 0; k < 3; k++)
            sema_inc(e->direct_trigger_sema[k]);     // unblock their dependent events
    /* event_index++  (mesh_ctx §8) */
}
```

The remote-sema increment rides the same `io_d2d` fabric: the cross-die page
documents `EVT_SEM.inc(remote_sem) +0x1800` routed over `io_d2d` — the offset the
device SIGNAL store targets when the semaphore CSR is a neighbour-die address.

> **GOTCHA — `[53:48]` is overloaded; never decode it both ways at once.** Bit 54
> (`CAYMAN_ID_VALID` / `ID_VALID`) is shared, and the `[53:48]` nibble means "chip
> id" *or* "neighbour route" depending on which decoder the firmware compiled
> against. A reimplementer must not treat `[53:48]` as both a 6-bit `CAYMAN_ID` and a
> packed `{EXIT_SENG, EXIT_DIE, NEIGHBOR_ROUTE, PEB}` simultaneously. See the
> [SoC-fold GOTCHA](../../dma/rdma-cross-die.md).

> **NOTE — the schedule lives in firmware, the structures are decodable.** The
> ≤3-trigger / ≤2-bcast caps mirror a low-radix multi-dim torus/mesh node (a handful
> of neighbour edges per node). Chaining events whose `direct_trigger_sema` targets
> address different axes yields the full multi-dimensional all-reduce
> (reduce-scatter then all-gather, neighbour-die hops in place of the ring's wrap).
> The d2d/`CAYMAN_ID` binding of the soc_addr bits is `HIGH`; the
> reduce-scatter/all-gather decomposition is `INFERRED MED` — the executing
> schedule runs in the LX firmware (the concrete soc_addr integers live in the
> runtime-populated firmware DRAM), only the consumed structures are decodable here.

---

## 8. The mesh runtime context — a single `u16` program counter

`ncfw_log_algo_mesh_ctx` @0x1884c reads **exactly one field** — no per-channel loop:

| off | size | field | fmt | meaning |
|---|---|---|---|---|
| `+0x00` | `2` | `event_index` | `%hu` | cursor: which event in `events[]` is current |

`[HIGH/OBSERVED — `movzx eax,WORD PTR [rax]` @0x18adb; key `"event_index"` @0x6562e.]`

This is the **entire mesh scoreboard**: one `u16` PC into the events tape. Its
placement in the `ncfw_log_algo_ctx` struct (@0x18cd2):

```
+0x000  ring_ctx          (32 × 16 = 512 B)   -> ncfw_log_algo_ring_ctx          (call @0x18e7c)
+0x200  hierarchical_ctx  (run_state u8)      -> ncfw_log_algo_hierarchical_ctx  (lea +0x200 @0x18e85)
+0x204  mesh_ctx          (event_index u16)   -> ncfw_log_algo_mesh_ctx          (lea +0x204 @0x18ea9)
```

`[HIGH/OBSERVED — all three leas re-read this session; the active one is chosen by
`algo_type`.]`

> **QUIRK — mesh has no per-channel flow control.** The ring runtime ctx is
> 32 channels × 16 B = 512 B of `recv_cnt`/`send_credit`/`m2s_val`/`s2m_val`/
> `run_state`. The mesh has a 2-byte cursor and *nothing else* — because it is
> strictly sequential and the semaphore guards inside each event ARE the flow
> control. `[event_index OBSERVED HIGH; the "tape PC" role INFERRED HIGH from its
> singularity beside the `events[]` array.]`

---

## 9. The device event-tape stepper (scalar Xtensa-LX)

The host decoders give the *structures* the firmware reads; the **device step
machine** that consumes them is in the carved NCFW IRAM blobs. Decoded under the
scalar-LX length rule via `xtensa-elf-objdump XTENSA_CORE=ncore2gp` on the **v3
CAYMAN** image (carved from `v3_ncfw_iram_bin` @0x79860, 19 392 B = `0x4bc0`; sha256
`d7bc8b81…`, re-verified == the carve identity). The mesh/ring/hier case bodies
**call a shared bank of leaf semaphore primitives**, and those primitives decode
cleanly even though the case-body interiors do not (§9.4).

### 9.1 The semaphore-WAIT spin-poll (the inbound guard)

The wait helpers are a `memw`-fenced spin-poll on the CSR pointed by `a10`. The
`event_wait_sema`/`wait_val` guard of §3 is realized as **WAIT-GE** @0x3498:

```asm
; xtensa-elf-objdump XTENSA_CORE=ncore2gp, v3 CAYMAN IRAM @0x3498
3498:  c0 20 00   memw                ; ordering barrier before the CSR read
349b:  28 0a      l32i.n  a2, a10, 0  ; a2 = *(sema CSR)   (a10 = CSR ptr)
349d:  27 b3 f7   bgeu    a3, a2, 0x3498 ; spin while target a3 >= val a2 (release when val>target)
34a0:  1d f0      retw.n              ; return once reached
```

`[HIGH/OBSERVED — bytes decoded this session, native objdump output.]` The full
family is present — **10 spin-poll wait primitives** across two register banks
(`a2/a3` and `a5/a4`): `wait-ne` (`bne`), `wait-lt/le` (`bltu`), `wait-ge` (`bgeu`),
each loading the CSR at `a10`, comparing the target, and spinning back to its own
`memw` until release. Count byte-stable **10/10/10** across CAYMAN/MARIANA/
MARIANA_PLUS; SUNDA (the structurally-different v2 monolith) does **not**
library-factor the primitives into an `a10`-pointer bank (4 inlined spin-polls on
base regs `a3/a4/a6`). `[HIGH/OBSERVED — per-gen census this session.]`

This is the device side of the host `add_semaphore_wait_ge_and_dec` op. The wait-ge
helper @0x3488 has an arg-decode prologue (an `entry a1,32` + one `op0=f` scalar op
`bf c2 08` @0x348b that the linear sweep mis-aligns on, §9.3) before the stream
re-converges at the verified `memw` @0x3498.

### 9.2 The semaphore-SIGNAL (the fanout) + the atomic snapshot

The `direct_trigger_sema` fanout of §3 is a `memw`-fenced CSR store. **Five genuine
fenced CSR-write SIGNAL primitives** exist (e.g. @0x31c7), distinct from six
frame-spill `s32i.n` (base register `a1`) by base-register classification:

```asm
31c7:  c0 20 00   memw                ; barrier
31ca:  49 0a      s32i.n  a4, a10, 0  ; *(sema CSR) = a4   (the SEMAPHORE-INC/SET store)
31cc:  c0 20 00   memw                ; next fenced store
```

`[HIGH/OBSERVED — the value written is the *register* a4 (= `1 << sema_shift_offset`
per the host overlay, §6), not a hardcoded 1.]`

A third primitive — the **atomic 64-bit CSR snapshot quad** (e.g. @0x3684, three
byte-identical copies) — reads two adjacent CSR words, each `memw`-fenced, and
spills both to the local frame:

```asm
3684:  memw ; 3687: l32i.n a2,a10,0 ; 3689: memw ; 368c: s32i.n a2,a1,12
368e:  memw ; 3691: l32i.n a3,a10,4 ; 3693: memw ; 3696: s32i.n a3,a1,8
```

`[HIGH/OBSERVED — the read-side of a read-compare/read-modify, e.g. snapshotting a
64-bit semaphore/counter pair before the wait compare.]`

> **NOTE — every CSR touch is `memw`-fenced.** Each WAIT read and each SIGNAL write
> is bracketed by `memw`; the per-gen barrier census is `memw 47/47/47` (v3/v4/v4+)
> vs `405` (the v2 monolith fences far more), `extw 1`, `waiti 15` 1 per image. This
> confirms the host-side "fenced by memw" claim at the instruction level.

### 9.3 The dispatch spine and the idle loop

The main loop reads its handler from a DRAM-resident table at `+0xB0`:

```asm
3bf8:  24 b0 00   const16 a2, 0xb0    ; table byte-base
3bfb:  20 23 a0   addx4   a2, a3, a2  ; a2 = index<<2 + 0xB0
3bfe:  58 02      l32i.n  a5, a2, 0   ; a5 = table[index]  (the case-label IRAM address)
```

The idle loop parks at `waiti 15` with a tight back-edge:

```asm
4b6c:  00 7f 00   waiti  15           ; park at max INTLEVEL
4b6f:  c6 fa ff   j      0x4b5e       ; back-edge
```

`[HIGH/OBSERVED — both byte-exact under the scalar rule and via native objdump.]`

### 9.4 What stays hard (honest)

> **GOTCHA — the case-body INTERIORS do not fully linearize, even under the correct
> scalar rule.** The `op0=e/f` bytes in the NCFW image are overwhelmingly
> *operand/immediate* bytes of scalar 2-/3-byte instructions, not instruction
> leaders. A linear sweep that treats every `op0=e/f` byte as a 3-byte instruction
> desyncs inside the dense ring (`0x3c..`) / hier-barrier (`0x3e..`) cluster
> interiors — the apparent `call0 0xc900`-style targets are **out-of-image** (the v3
> IRAM is only `0x4bc0` bytes) and are mis-synced operand bytes, not real calls. No
> LX TIE config ships, so the `op0=e/f` leader ops cannot be named. What **is**
> recovered: the dispatch spine, the staggered computed-goto entry structure, and
> the leaf WAIT/SIGNAL/snapshot primitives the bodies compose (§9.1–9.3). The
> per-step **schedule** (which sema, which target, in which order) lives in the
> e/f-dense interior + the runtime-populated firmware DRAM and stays `MED`. `[HIGH
> that the interiors stay hard — the irreducible limit; the primitives are HIGH.]`

---

## 10. Ring vs mesh vs hierarchical — structural diff

| | RING ([ring-kangaring](./ring-kangaring.md)) | **MESH** (this page) | HIER ([hierarchical](./hierarchical-collective.md)) |
|---|---|---|---|
| unit | channel | **event (tape step)** | (single handle) |
| config unit size | 149 B (`0x95`) | **80 B (`0x50`)** | 8 B region (`__stub`) |
| config count | 32 (loop `0x1f`) | **50 (SUNDA) / 108 (others)** | 1 (8-byte u64) |
| config region | `+0x0000` (`0x1280` wide) | **`+0x1280`** | `+0x2220` (SUNDA) / `+0x3440` |
| runtime ctx | 32 × 16 B (512 B) | **1 × u16 `event_index` (2 B)** | `run_state` u8 |
| runtime ctx offset | `algo_ctx+0x0` | **`algo_ctx+0x204`** | `algo_ctx+0x200` |
| topology field | `next_neigh`/`prev_neigh` | **`direct_trigger_sema_0..2` + `event_wait_sema`** (neighbour dies via CAYMAN_ID soc_addr) | (opaque handle) |
| flow control | `recv_cnt`/`send_credit`/`m2s`/`s2m` | **guarded event: wait≥val, then fanout INC (≤3 sema)** | (n/a at host) |
| sema addressing | explicit `channel_list` u32 (`+0x3` union, RING) | **`sema_shift_offset`+`sema_mask` u16/u16** (`+0x3` union, MESH) | (n/a) |
| DMA descriptor | `dma_apb_bcast` (20 B ×1) | **`dma_apb_bcast` (20 B ×2)** | (n/a) |
| `algo_type` value | `RING=0` | **`MESH=2 / 6 / 8 / 9 / 10`** | `HIER=1` |

> **NOTE — ring unit size.** The ring page reports the ring *config* channel at
> 149 B (`0x95`); the ring *runtime* channel record refines to 148 B (`0x94`). Both
> are ring facts; the mesh event (80 B) is unaffected. Cite the ring page for its
> own numbers — those facts are taken from the binary, not its (stub) prose.

---

## 11. Hard numbers (all OBSERVED, HIGH unless noted)

| quantity | value | evidence |
|---|---|---|
| mesh event struct size | `0x50` = 80 B | stride `80·i` @0x8f0c–0x8f13 |
| events/config (SUNDA/v2/0x05) | **50** (`0x32`) | `mov r8d` @0x19502 |
| events/config (CAYMAN/v3/0x0c) | **108** (`0x6c`) | `mov r8d` @0x322a9 |
| events/config (MARIANA/v4/0x14) | **108** (`0x6c`) | `mov r8d` @0x4b050 |
| events/config (MARIANA_PLUS/v4+/0x1c) | **108** (`0x6c`) | `mov r8d` @0x63df7 |
| `dma_apb_bcast` slots/event | 2 | `cmp …,0x1 ; jle` @0x90c0 |
| `direct_trigger_sema` slots/event | 3 | `cmp …,0x2 ; jle` @0x9722 |
| `event_wait_sema` / `wait_val` | 1 u64 @+0x40 / u32 @+0x48 | — |
| event opcode bytes | 4 u8 @+0x4c..+0x4f | `event_type`/`dma_trigger`/`direct_trigger`/`wait_event` |
| `dma_apb_bcast` struct size | `0x14` = 20 B (shared w/ ring) | @0x4899 |
| mesh runtime ctx size | 2 B (`event_index` u16, `%hu`) | @0x18adb |
| mesh region (SUNDA) | `0xFA0` = 50×80 | cfg+0x1280..+0x2220 |
| mesh region (CAYMAN/MARIANA/M_PLUS) | `0x21C0` = 108×80 | cfg+0x1280..+0x3440 |
| `cc_op` `+0x3` union (mesh) | `sema_shift_offset` u16 / `sema_mask` u16 | @0x2eb6 / @0x30da |
| mesh_events decoder size (all 4) | `0x1fb1` = 8113 B (byte-identical) | nm -nS |
| mesh_configs wrapper size (all 4) | `0x2b7` (byte-identical) | nm -nS |
| device WAIT spin-polls (v3/v4/v4+) | 10 / 10 / 10 | scalar-LX decode |
| device fenced SIGNAL stores (v3) | 5 | base-register split |
| CAYMAN mesh size | 64 dies (`CAYMAN_ID` 6 bits) × 2 dies/pkg | rdma-cross-die |
| arch_id → codename (CORRECT) | `0x05=SUNDA 0x0c=CAYMAN 0x14=MARIANA 0x1c=MARIANA_PLUS` | ctx_log @0x1309 |
| MAVERICK/v5 NCFW | **ABSENT** (no symbol, no selector leg) | the v5 wall |

---

## 12. Residual uncertainty

- The numeric `algo_type` value that selects MESH is **not in `libncfw`** (no
  value→name enum, no gating branch). The values come from
  [`collective-enums.md`](../ops/collective-enums.md): `MESH=2` + the `6/8/9/10`
  variants. `[absence in libncfw HIGH; values HIGH via collective-enums; the
  runtime's ring-vs-mesh heuristic is external — LOW.]`
- The per-event opcode-byte **semantics** (`event_type == enc_mesh_event_type`
  INFERRED MED from name+width; the three trigger/wait booleans name-inferred) and
  the wiring of the 2-bcast / 3-trigger slots to mesh dimensions — `MED`. The
  executing **schedule** runs in the LX firmware, not decodable here.
- `sema_shift_offset`/`sema_mask` are `OBSERVED` as u16@+0x3/+0x5 mesh-overlay
  fields; the "shift base, mask → per-node slot" use is `INFERRED MED`.
- The d2d/`CAYMAN_ID` binding of the event soc_addrs is structural (the committed
  rdma-cross-die fold) — `HIGH` that those bits route to neighbour dies, `MED` that
  each specific `direct_trigger_sema` is a specific mesh-axis neighbour (the
  concrete soc_addr integers live in the runtime-populated firmware DRAM).
- **v5/MAVERICK NCFW is FILE-ABSENT** — any v5 mesh interior claim would be
  `INFERRED/ABSENT`, never OBSERVED.

---

## Cross-references

- The sibling ring algorithm (32-channel scoreboard, shared `dma_apb_bcast` + the
  `cc_op` `+0x3` union): [Ring + Kangaring Collective](./ring-kangaring.md)
- The hierarchical leg (8-byte `__stub` handle, `run_state` u8, composes ring/mesh
  legs): [Hierarchical Collective](./hierarchical-collective.md)
- The cross-die d2d transport + the SoC-address fold (`CAYMAN_ID`/neighbour-route,
  `EVT_SEM.inc +0x1800` over `io_d2d`):
  [RDMA Cross-Die SBUF→SBUF P2P](../../dma/rdma-cross-die.md)
- The byte-exact enum bodies (`enc_alg_type`, `enc_alg_mesh_type`, the 61-event
  `enc_mesh_event_type`, the `cc_op` command word):
  [Collective-Type + cc_op Enum Reference](../ops/collective-enums.md)
- The scalar-LX ISA / decode / dispatch-spine evidence and the v5 wall:
  [The NCFW Scalar-LX Management Core](../../uarch/ncfw-lx-core.md),
  [NCFW IRAM Images + Host Selector](./ncfw-iram-images.md)
- The orchestration synthesis (LX-ISA naming, arch_id diff, the collective
  spine across all three algorithms):
  [NCFW LX-ISA / DMA-Naming / arch_id-Diff / Orchestration Synthesis](./lx-isa-naming-archid-synthesis.md)
