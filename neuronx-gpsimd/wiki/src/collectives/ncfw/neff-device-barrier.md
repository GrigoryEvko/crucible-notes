# NEFF Device Barrier

This page documents the **NCFW NEFF *device* barrier** — the cross-engine /
cross-rank counted-semaphore fence the NeuronCore-management firmware runs
**on-device** to align all participating engines and ranks before and after each
collective phase. It is the **collective-execution** counterpart to the three
compiler **pseudo-op** barriers ([`PSEUDO_CORE_BARRIER 0xD8`](../ops/core-barrier.md)
/ [`PSEUDO_SYNC_BARRIER 0xD5`](../ops/sync-barrier.md) /
[`PSEUDO_DMABARRIER 0xC3`](../ops/dma-barrier.md)): those pseudo-ops are the
*consumer-side* opcodes a kernel emits; **this** struct is the concrete
semaphore-address + target-value table the firmware materialises them against at
runtime. It brackets the [ring/kangaring](./ring-kangaring.md) and
[mesh](./mesh-collective.md) data-movement phases.

The page owns four byte-grounded things:

1. the **`neff_barrier_config` parent** and its split into a **host barrier**
   ([`neff-host-barrier.md`](./neff-host-barrier.md), the host↔device handshake)
   and the **device barrier** (this page), gated by one `u8` flag;
2. the **device-barrier struct** — a 4-step counted-semaphore descriptor array
   plus a DMA-broadcast / network-proxy transport block, offsets `0x00..0x122`;
3. the **per-step arrive/wait protocol** — `barrier_sema[4]` waited up to
   `target_sema_val[4]`, fanned out over the APB-broadcast DMA channel; and
4. the **device leaf primitives** the firmware composes — the scalar-Xtensa-LX
   semaphore **WAIT-GE** and **fenced-SIGNAL** bodies, re-decoded from the carved
   IRAM blob.

The structural recovery is from the **host-side `ncfw_log_*` barrier decoders** in
`libncfw.so` — JSON pretty-printers that walk the firmware's DRAM-resident barrier
structs, so every offset / count / stride / scalar width below is read from the
exact byte (or `lea` immediate) the matching `snprintf()` consumes. The device-side
WAIT/SIGNAL leaves are from a **scalar-Xtensa-LX re-decode of the carved NCFW IRAM
blob**.

> **GOTCHA — this is the NCFW *management* core, a scalar Xtensa-LX, not the
> Vision-Q7 "Cairo" FLIX DSP.** Two Xtensa cores live in the GPSIMD estate. The
> user-kernel **datapath** is the Vision-Q7 NX (`ncore2gp`, 512-bit SIMD, real
> FLIX). The core *here* is its control sibling: a **scalar Xtensa-LX** whose
> firmware ships as four IRAM images embedded in `libncfw.so`. Decode its device
> WAIT/SIGNAL bodies under the **scalar-LX length rule** (`op0 e/f = 3-byte / else
> 2-byte`, resync at `retw.n`) via `xtensa-elf-objdump XTENSA_CORE=ncore2gp` —
> **never** the FLIX bundle decoder. See
> [The NCFW Scalar-LX Management Core](../../uarch/ncfw-lx-core.md).

**Provenance & confidence.** Every host-decoder fact below is read **this session**
from the shipped sibling runtime library
`libncfw.so` (sha256 `598920d743762c03…`, BuildID `a98f8e1ca2294582…`, SONAME
`libncfw.so.2.31.1.0.cf13a49f`, **615 640 B** — all four identity anchors
re-verified via `readelf -n`/`-d`, `stat`, `sha256sum`), with stock binutils
(`readelf -SW`/`nm -nS`/`objdump -d -M intel`) and the native Cadence
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) on the carved v3 IRAM blob.
`.text`/`.rodata` are VMA==file-offset here (`[14] .text @0x10c0`,
`[16] .rodata @0x65000` — both VMA==offset), so **no `.data` delta** applies to any
quoted address. Lawful interoperability reverse engineering (DMCA 17 U.S.C.
1201(f)); no vendor source snapshot consulted. Tags are
`HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` per the
[Confidence & Walls Model](../../reference/confidence-model.md): `OBSERVED` = a
byte/size/symbol/disasm read this pass; `CARRIED` = OBSERVED in a cited prior carve
and reused; `INFERRED` = reasoned over those. Callouts: **QUIRK** (counter-intuitive
but real), **GOTCHA** (a reimplementation trap), **NOTE** (orientation),
**CORRECTION** (overturns a prior reading).

> **THE v5 / MAVERICK WALL — read before any v5 claim.** `libncfw` ships **exactly
> four** NCFW generations (v2/v3/v4/v4+). There is **no MAVERICK (v5) NCFW image**:
> `libncfw_get_image @0x1179` compares `arch_id` against exactly `{0x05,0x0c,0x14,
> 0x1c}` with **no** `0x24` (36) leg, and `nm libncfw.so | rg -i 'maverick|v5'`
> returns zero symbols. The four embedded blobs are named for their codenames'
> source files (`sunda.c`/`cayman.c`/`mariana.c`/`mariana_plus.c`); there is no
> `maverick.c`. **MAVERICK = NC-v5 has NCFW ABSENT** — any statement about a v5
> device-barrier *interior* is structurally unverifiable here and is flagged
> **INFERRED/ABSENT**, never stated as fact.

---

## 1. The `arch_id` selector and the per-arch ×4 layout

Each barrier decoder appears **four times** — once per embedded NCFW image. The
`arch_id`→image binding is read directly from the `libncfw_get_image @0x1179`
dispatch (do **not** invert it):

| `arch_id` | codename | NC gen | image symbols (this pass) | barrier decoder copy |
| --------- | -------- | ------ | ------------------------- | -------------------- |
| **`0x05`** | SUNDA | NC-v2 | `v2_ncfw_dram_bin @0x66a60` / `v2_ncfw_iram_bin @0x6a140` | `device_barrier_config @0xfef5` |
| **`0x0c`** | CAYMAN | NC-v3 | `v3_ncfw_dram_bin @0x74a40` / `v3_ncfw_iram_bin @0x79860` | `device_barrier_config @0x28c9c` |
| **`0x14`** | MARIANA | NC-v4 | `v4_ncfw_dram_bin @0x7e440` / `v4_ncfw_iram_bin @0x83260` | `device_barrier_config @0x41a43` |
| **`0x1c`** | MARIANA_PLUS | NC-v4+ | `v4_plus_ncfw_dram_bin @0x87ea0` / `v4_plus_ncfw_iram_bin @0x8ccc0` | `device_barrier_config @0x5a7ea` |

`libncfw_get_image @0x1179` reads (**HIGH/OBSERVED** — `objdump -d 0x1199..0x12cd`):

```c
// arch_id in [rbp-0x4]; each leg returns (iram, dram) pointer+size tuple.
if (arch_id == 0x1c) return {v4_plus...};   // 1199: cmp [rbp-4],0x1c ; je 12a7
if (arch_id == 0x14) return {v4...};        // 11ad: cmp [rbp-4],0x14 ; je 1262
if (arch_id == 0x05) return {v2...};        // 11c1: cmp [rbp-4],0x05 ; je 11d2  -> v2_ncfw_dram_bin
if (arch_id == 0x0c) return {v3...};        // 11c7: cmp [rbp-4],0x0c ; je 121a  -> v3_ncfw_dram_bin
// no 0x24 leg  =>  v5/MAVERICK ABSENT
```

> **QUIRK — the four barrier decoders are byte-identical in *size*, proving a
> schema-wide layout.** From `nm -nS libncfw.so` this pass, every per-arch copy of
> every barrier decoder has the **same function size** (so the same struct walk):

| decoder | size | v2 | v3 | v4 | v4+ |
| ------- | ---- | -- | -- | -- | --- |
| `neff_host_barrier_config` | `0x7a9` | `0xd613` | `0x263ba` | `0x3f161` | `0x57f08` |
| `neff_device_barrier_semaphores` | `0x925` | `0xddbc` | `0x26b63` | `0x3f90a` | `0x586b1` |
| `neff_device_barrier_sema_values` | `0x621` | `0xe6e1` | `0x27488` | `0x4022f` | `0x58fd6` |
| `neff_device_barrier_step_config` | `0x8ce` | `0xed02` | `0x27aa9` | `0x40850` | `0x595f7` |
| `device_barrier_dma_sync_sema` | `0x925` | `0xf5d0` | `0x28377` | `0x4111e` | `0x59ec5` |
| `neff_device_barrier_config` | `0x1096` | `0xfef5` | `0x28c9c` | `0x41a43` | `0x5a7ea` |

The four `arch_id` values therefore share **one** device-barrier schema; offsets,
strides and the 4-step / 4-sema / 4-target counts below are schema-wide. **All
addresses below quote the v2/SUNDA copy.** [**HIGH/OBSERVED** — function-size deltas
from `nm -nS`, this pass.]

> **NOTE — `.data` caveat.** The `soc_addr` *integers* the firmware reads live in
> the embedded DRAM image (`v2_ncfw_dram_bin` …), populated at runtime — **not** in
> `libncfw.so`'s `.data`. This page recovers the struct **layout** (offsets / sizes
> / strides) byte-exactly from the host decoder code + `.rodata` keys, where
> VMA==file-offset; the concrete CSR addresses are firmware-runtime data.

---

## 2. The `neff_barrier_config` parent — host vs device split

The top-level decoder `ncfw_log_neff_barrier_config @0x10f8b` emits one JSON object
with **one scalar gate flag** and **two sub-objects** (**HIGH/OBSERVED** —
`objdump -d 0x10f8b`, `.rodata` keys read at the `lea` targets):

```c
// ncfw_log_neff_barrier_config @0x10f8b
struct neff_barrier_config {
    /* +0x00 .. */ neff_host_barrier_config   host_barrier;    // sub-object, key @0x6545a
    /* +0x00 .. */ neff_device_barrier_config device_barrier;  // sub-object, key @0x65467
    /* +0x124 */   uint8_t execute_device_barrier;             // GATE flag, key @0x65441
};
// 1121a: movzx eax,BYTE PTR [rax+0x124]   ; execute_device_barrier (u8)
//        ... printed via "%p"  (fmt @0x65328)  -> snprintf @0x11265
// 11313: mov rdx,[rbp-0x60]               ; cfg
// 1132b: call d613 <ncfw_log_neff_host_barrier_config>   key "host_barrier"  @0x6545a
// 11330: mov rdx,[rbp-0x60]               ; SAME cfg base
// 11348: call fef5 <ncfw_log_neff_device_barrier_config> key "device_barrier" @0x65467
```

> **GOTCHA — both sub-decoders receive the *same* parent base pointer, but each
> indexes its own sub-struct.** At `0x11313` and `0x11330` the host library loads
> the identical `cfg = [rbp-0x60]` into `rcx` for **both** sub-calls. The host
> sub-decoder reads `cfg+0x00`/`cfg+0x08`; the device sub-decoder reads
> `cfg+0x00..+0x122` of **its own passed base**. The library treats them as **two
> distinct DRAM regions, not an overlay** — when you rebuild the firmware-side
> struct, allocate the host pair (§3) and the device block (§4) separately; do not
> alias them. [**HIGH/OBSERVED** — `rcx=rbp-0x60` in both call sites.]

* The **`execute_device_barrier` u8 @+0x124** is the on/off gate: if clear, the
  firmware skips the device-barrier leg entirely and the collective relies on the
  host handshake alone. It is printed via `"%p"` (the decoder formats the raw byte
  value as a pointer-style hex literal — a cosmetic decoder choice; the field is one
  byte). [**HIGH/OBSERVED**; the gate *semantics* **INFERRED/MED** from the flag
  name + the two-leg split.]
* **`host_barrier`** (§3): the host↔device handshake CSR pair — owned by
  [`neff-host-barrier.md`](./neff-host-barrier.md).
* **`device_barrier`** (§4–§6): the on-device cross-engine/cross-rank counted
  barrier — this page.

---

## 3. Host barrier — the host↔device handshake (relationship)

The host-barrier sub-struct `ncfw_log_neff_host_barrier_config @0xd613` (size
`0x7a9`) is **16 bytes**: two semaphore `soc_addr` CSRs the host runtime drives.
Full layout is owned by [`neff-host-barrier.md`](./neff-host-barrier.md); the
relationship to the device barrier is recorded here.

| off | size | field | how read (v2 copy) |
| --- | ---- | ----- | ------------------ |
| `+0x00` | 8 | `barrier_start.addr.soc_addr` u64 | `ncfw_log_addr(cfg)` — `mov r8,rcx` @`0xd7b2` → `call 0x41c3` @`0xd7c4`; key `"barrier_start"` @`0x65371` |
| `+0x08` | 8 | `barrier_done.addr.soc_addr` u64 | `mov r12,QWORD PTR [rax+0x8]` @`0xda40`; key `"barrier_done"` @`0x6537f` |

[**HIGH/OBSERVED** — both loads + key strings read this pass.]

**Handshake relationship [INFERRED/MED].** `barrier_start` is the semaphore the host
**signals to release** the device into the collective; `barrier_done` is the
semaphore the device **signals on completion** (or the host **waits** on) once all
ranks have cleared the barrier. With the `execute_device_barrier` gate, a collective
executes as:

```
host: signal barrier_start  ─┐
                             ├─ [device_barrier: 4-step arrive/wait]   (§6)
                             │   ↓
                             │   ring (./ring-kangaring.md) / mesh (./mesh-collective.md) data phase(s)
                             │   ↓
                             ├─ [device_barrier: 4-step arrive/wait]   (§6)
host: wait  barrier_done   ◀─┘
```

The device barrier is the **cross-engine/cross-rank fence that brackets** the
ring/mesh phases; the host pair is the coarse-grained host↔device bracket around the
whole collective. [Bracketing **INFERRED/MED**, from the `execute_device_barrier`
gate + the `PSEUDO_*_BARRIER` placement around phases in the collective lowering.]
The host pair is **distinct** from the device-internal step semaphores in §5 — those
never surface to the host as addresses here.

---

## 4. The device-barrier struct — `neff_device_barrier_config @0xfef5`

The device-barrier struct spans offsets `0x000..0x122`, so it is **≥ 0x123 bytes**
(per-arch tail padding past `0x122` is not exposed by the decoder). Every field
below is read from the instruction that references it in `device_barrier_config`
(size `0x1096`). [**HIGH/OBSERVED** — load instructions quoted, v2 copy.]

| off | size | field | type | load instruction (v2) | fmt / child |
| --- | ---- | ----- | ---- | --------------------- | ----------- |
| `+0x000` | `0xD0` | `barrier_step_config[4]` | struct[4]×`0x34` | step base = cfg @`0x10e3f`; `call ed02` @`0x10e60` | §5 |
| `+0x0D0` | `0x20` | `dma_sync_sema[4]` | u64×4 | `lea rdx,[rax+0xd0]` @`0x1010d`; `call f5d0` @`0x1012e` | key `"dma_sync_sema"` @`0x653cf` |
| `+0x0F0` | `0x14` | `dma_apb_bcast` | struct (20 B) | `lea rdx,[rax+0xf0]` @`0x100e0`; `call 4899` @`0x10101` | §7; key `"dma_apb_bcast"` @`0x651e2` |
| `+0x104` | 8 | `start_network_proxy.addr.soc_addr` | u64 | `lea rcx,[rax+0x104]` @`0x1035d`; `call 41c3` @`0x10383` | `ncfw_log_addr`; key @`0x653f3` |
| `+0x10C` | 4 | `dma_sync_sema_value` | u32 | `mov ebx,DWORD PTR [rax+0x10c]` @`0x1024c` | `"%u"` @`0x6501e` |
| `+0x110` | 4 | `tdrbp_low` | u32 | `mov ebx,DWORD PTR [rax+0x110]` @`0x104a1` | `"%u"` |
| `+0x114` | 4 | `tdrbp_high` | u32 | `mov ebx,DWORD PTR [rax+0x114]` @`0x106c4` | `"%u"` |
| `+0x118` | 4 | `desc_count` | u32 | `mov ebx,DWORD PTR [rax+0x118]` @`0x108e7` | `"%u"` |
| `+0x120` | 2 | `dma_engines_bitmap` | u16 | `movzx eax,WORD PTR [rax+0x120]` @`0x10b0a` | `"%hu"` @`0x650ba` |
| `+0x122` | 1 | `queue_id` | u8 | `movzx eax,BYTE PTR [rax+0x122]` @`0x10d31` | `"%hhu"` @`0x650e3` |

```c
// neff_device_barrier_config @0xfef5  (>= 0x123 B; per-arch tail padding not exposed)
struct neff_device_barrier_config {
    barrier_step_config step[4];          // +0x000  4 × 0x34 = 0xD0   (§5)
    addr_t   dma_sync_sema[4];            // +0x0D0  4 × u64 (soc_addr) (§6 DMA-drain)
    dma_apb_bcast_t dma_apb_bcast;        // +0x0F0  20 B (m2s/s2m tail + mask, §7)
    addr_t   start_network_proxy;         // +0x104  u64 soc_addr (RDMA/inter-node leg)
    uint32_t dma_sync_sema_value;         // +0x10C  target dma_sync_sema must reach
    uint32_t tdrbp_low;                   // +0x110  transfer-descriptor ring base, lo32
    uint32_t tdrbp_high;                  // +0x114                                  hi32
    uint32_t desc_count;                  // +0x118  # DMA descriptors
    uint16_t dma_engines_bitmap;          // +0x120  participating DMA engines
    uint8_t  queue_id;                    // +0x122  DMA queue index
};
```

> **CONTIGUITY PROOF [HIGH/OBSERVED].** `step_config` consumes **no head scalar**: it
> stores the cfg base in `[rbp-0x80]` (`mov [rbp-0x80],rcx` @`0xed1c`) and reads its
> first element field at `[rax]` with `rax = [rbp-0x80]` (`movzx WORD PTR [rax]`
> @`0xf031`), then steps by `idx*0x34`. Four `0x34`-stride descriptors therefore
> occupy **exactly** `0x00..0xCF = 0xD0` bytes — precisely where `dma_sync_sema`
> begins (`cfg+0xD0`). `dma_sync_sema` = 4×u64 = `0x20` → ends at `0xF0`, where
> `dma_apb_bcast` begins. The whole low region is **gap-free**.

> **GOTCHA — there is *no* `barrier_id`, rank/participant *count*, `timeout`, or
> explicit *phase* field anywhere in the struct or in `libncfw.so`'s rodata.**
> `strings -t x libncfw.so | rg -i 'barrier_id|timeout|phase|participant|threshold|
> rank_mask|num_ranks'` returns **zero hits** this pass. Participant identity is
> carried **implicitly**: which *engines* → `dma_engines_bitmap` + `queue_id`; which
> *ranks/dies* → the high bits of the `barrier_sema` / `dma_sync_sema` /
> `start_network_proxy` `soc_addr` values (the Cayman die-select bitfields — see
> [`rdma-cross-die.md`](../../dma/rdma-cross-die.md)); how-many-*steps* → fixed at 4
> (compile-time, not a runtime count). A reimplementation must **not** look for a
> participant mask field — it does not exist at this boundary. [**HIGH/OBSERVED** for
> the absence; die-select via `soc_addr` high bits **INFERRED/MED**.]

**Transport-block field roles** (names **HIGH/OBSERVED**; transport semantics
**INFERRED/MED** from the identical tail-pointer roles in the ring channels):

* **`dma_apb_bcast` (§7)** — the APB-broadcast DMA channel that **physically
  delivers** each step's semaphore writes to peer dies: `m2s`/`s2m` tail-pointer CSRs
  plus an engine `mask`. One write reaches all masked peers at once.
* **`start_network_proxy`** — a single semaphore CSR that **kicks the
  network-proxy / RDMA leg** — the cross-**rank** (vs cross-engine) hook for
  inter-node barriers.
* **`dma_sync_sema[4]` + `dma_sync_sema_value`** — the **DMA-completion sync**: the
  barrier waits the DMA-sync semaphore up to `dma_sync_sema_value` to declare the
  transport drained (bridges RDMA/iDMA completion into the step counter).
* **`tdrbp_low`/`tdrbp_high` + `desc_count`** — the **transfer-descriptor ring**
  (64-bit base split into two u32 halves + count) the broadcast DMA pulls its
  descriptors from.

---

## 5. The step descriptor — `neff_device_barrier_step_config @0xed02`

The per-step micro-descriptor: an **array of 4, each 52 bytes (`0x34`)** (size
`0x8ce`). [**HIGH/OBSERVED**.]

* **Loop bound:** `cmp DWORD PTR [rbp-0x54],0x3 / jle` @`0xf4c1`/`0xf4c5` ⇒ idx
  `0..3` (**4 steps**).
* **Stride `0x34` proof** @`0xf331..0xf347` — the textbook multiply-by-52 shift
  chain:

```asm
f331: mov eax,[rbp-0x54]    ; idx
f334: movsxd rdx,eax        ; rdx = idx
f33a: add rax,rax           ; 2·idx
f33d: add rax,rdx           ; 3·idx
f340: shl rax,0x2           ; 12·idx
f344: add rax,rdx           ; 13·idx
f347: shl rax,0x2           ; 52·idx = 0x34·idx
```

| off | size | field | type | load (v2) | key |
| --- | ---- | ----- | ---- | --------- | --- |
| `+0x00` | 2 | `m2s_val` | u16 | `movzx eax,WORD PTR [rax]` @`0xf031` | `"m2s_val"` @`0x653ab` |
| `+0x02` | 2 | `s2m_val` | u16 | `movzx eax,WORD PTR [rax+0x2]` @`0xf23e` | `"s2m_val"` @`0x653b5` |
| `+0x04` | `0x20` | `barrier_sema[4]` | u64×4 | `lea rdx,[rax+0x4]` @`0xf355` → `call ddbc` @`0xf36d` | `"barrier_sema"` @`0x6538c` |
| `+0x24` | `0x10` | `target_sema_val[4]` | u32×4 | `lea rdx,[rax+0x24]` @`0xf396` → `call e6e1` @`0xf3ae` | `"target_sema_val"` @`0x653bf` |

`0x04 + 0x20 = 0x24`; `0x24 + 0x10 = 0x34` — the descriptor is exactly its own
stride, gap-free.

```c
// barrier_step_config @0xed02  (4 × 0x34 = 0xD0; base = cfg + idx*0x34)
struct barrier_step_config {
    uint16_t m2s_val;            // +0x00  per-step memory->semaphore counter / delta
    uint16_t s2m_val;            // +0x02  per-step semaphore->memory counter / delta
    addr_t   barrier_sema[4];    // +0x04  4 semaphore CSR soc_addrs for THIS step (§5.1)
    uint32_t target_sema_val[4]; // +0x24  4 target values barrier_sema[i] must REACH (§5.2)
};
```

`m2s_val` / `s2m_val` are the per-step **memory→semaphore** and **semaphore→memory**
counter values — the same `m2s`/`s2m` counter pair seen in the apb-bcast tail
pointers (§7). They are the per-step increment amounts / expected deltas for the two
transfer directions. [names/widths **HIGH/OBSERVED**; "increment / expected delta"
**INFERRED/MED** — see the OPEN list in §9.]

### 5.1 `barrier_sema[4]` — `neff_device_barrier_semaphores @0xddbc`

Emits `"barrier_sema": [ {"addr":{"soc_addr":"0x%016lX"}}, … ]` (size `0x925`).
[**HIGH/OBSERVED**.]

* **Loop bound:** `cmp DWORD PTR [rbp-0x5c],0x3 / jle` @`0xe5d6` ⇒ idx `0..3` (4
  entries).
* **Element:** `mov r12,QWORD PTR [rax+rdx*8]` @`0xe24d` ⇒ **stride 8, u64** each.

⇒ `barrier_sema[k][0..3]` are **four semaphore CSR physical addresses** for step `k`.

### 5.2 `target_sema_val[4]` — `neff_device_barrier_sema_values @0xe6e1`

Emits `"target_sema_val": [ u32, … ]` (size `0x621`). [**HIGH/OBSERVED**.]

* **Loop bound:** `cmp DWORD PTR [rbp-0x44],0x3 / jle` @`0xebf7` ⇒ idx `0..3` (4
  entries).
* **Element:** `mov ebx,DWORD PTR [rax+rdx*4]` @`0xe9f6` ⇒ **stride 4, u32** each.

⇒ for step `k`, `target_sema_val[k][i]` is the value `barrier_sema[k][i]` must
**reach** before the rank may proceed past step `k` — a **SEMAPHORE-WAIT-GE**
predicate.

> **QUIRK — each step waits on a 4×(semaphore, target) bundle, i.e. a fan-IN of up
> to 4 peers/engines per step.** The `4 sema × 4 target` pairing means alignment at
> step `k` is the **AND** of four independent GE-conditions, mirroring the kangaring
> fan-out of up to 3 peers + self. There is no single "master" semaphore; the whole
> barrier is the AND of 4 steps × ≤4 GE-conditions. Whether all 4 slots are always
> populated or sparse depends on rank count / topology and is set by the firmware,
> not visible here. [4×4 structure **HIGH/OBSERVED**; "fan-in of 4 peers"
> **INFERRED/MED**.]

---

## 6. Arrive / wait protocol (per step), and the DMA drain

Combining §4–§5 with the device leaf primitives (§8) and the host IOCTL / ucode-op
mapping. The **4-step + sema/target structure is HIGH/OBSERVED**; the per-step
arrive/wait *sequencing* is **INFERRED/MED**, deduced from the field roles and the
`add_semaphore_inc` / `add_semaphore_wait_ge_and_dec` op pair:

```c
// reconstructed firmware loop over the device_barrier struct
for (int k = 0; k < 4; ++k) {                  // 4 steps  (HIGH/OBSERVED)
    barrier_step_config *s = &cfg->step[k];

    // ARRIVE  — increment each peer's REMOTE barrier_sema[k][i] CSR.
    //   The write is FANNED OUT via dma_apb_bcast (m2s/s2m tail-ptr ring,
    //   engines selected by cfg->dma_engines_bitmap on cfg->queue_id); the
    //   soc_addr high bits select the peer die (rdma-cross-die.md).
    //   == device add_semaphore_inc (op 0x10A0 subop 21)
    //      / host NEURON_IOCTL_SEMAPHORE_INC (0x80084E29).
    for (int i = 0; i < 4; ++i)
        dma_apb_bcast_inc(&cfg->dma_apb_bcast, s->barrier_sema[i], s->m2s_val);

    // WAIT  — block until each LOCAL barrier_sema[k][i] >= target_sema_val[k][i].
    //   == add_semaphore_wait_ge_and_dec (op 0x10A0 subop 20)  (PollSem).
    //   On the device this is the scalar-LX WAIT-GE body @0x3498 (§8).
    for (int i = 0; i < 4; ++i)
        wait_ge(s->barrier_sema[i], s->target_sema_val[i]);   // memw; l32i.n; bgeu
}

// DMA-DRAIN  — block until the DMA-sync semaphore reaches its target
//   (transport completion; descriptors pulled from the TDR ring
//    tdrbp_high:tdrbp_low, desc_count of them).
for (int i = 0; i < 4; ++i)
    wait_ge(cfg->dma_sync_sema[i], cfg->dma_sync_sema_value);
```

> **GOTCHA — peer-to-peer addressing, centralized orchestration.** The addressing is
> **peer-to-peer per step** (each rank writes a remote semaphore CSR on every peer
> die and waits its own local count up to the target), but the loop is run by the
> single NCFW management core that owns this struct. There is **no master
> semaphore** — alignment is the AND of the per-step GE-conditions. A reimplementer
> driving this from a host runtime must place the *remote-write* and the *local-wait*
> on the **same** semaphore index `i` but distinct `soc_addr` die-select bits.
> [topology **INFERRED/MED**.]

**Relationship to the compiler pseudo-ops.** The collective lowering emits the
barrier **pseudo-ops** [`PSEUDO_CORE_BARRIER 0xD8`](../ops/core-barrier.md) /
[`PSEUDO_SYNC_BARRIER 0xD5`](../ops/sync-barrier.md) /
[`PSEUDO_DMABARRIER 0xC3`](../ops/dma-barrier.md) around each ring/mesh phase. Those
opcodes are the **consumer side**: the runtime/firmware replaces them with the
concrete semaphore wait/signal sequence parameterised by **this** `device_barrier`
struct. The `0xD8`/`0xD5`/`0xC3` trilogy operates on **compiler-visible** semaphores
within a VNC / within a core / on DMA queues respectively; **this NCFW device
barrier is the collective-execution counterpart** that spans **engines *and* ranks**
across the d2d fabric. [pseudo-op opcodes **CARRIED/HIGH** from the ops pages; the
binding "the pseudo-ops consume this struct" **INFERRED/MED**.] Completion can
surface back to the host through the `barrier_completed` flag in the NEFF context
([`ncfw-dram-ctx-log.md`](./ncfw-dram-ctx-log.md): `neff_ctx.barrier_completed`
u8 @`+0x5c`, `movzx eax,[rax+0x5c]` @`0x167ca`).

---

## 7. `dma_apb_bcast` — the broadcast-delivery channel (`@0x4899`, 20 B)

Shared decoder (also used by the ring channels). [**HIGH/OBSERVED**.]

| off | size | field | type | load (v2) | key |
| --- | ---- | ----- | ---- | --------- | --- |
| `+0x00` | 8 | `m2s_tail_ptr.addr.soc_addr` | u64 | `ncfw_log_addr(cfg)` @`0x4a4a` | `"m2s_tail_ptr"` @`0x65150` |
| `+0x08` | 8 | `s2m_tail_ptr.addr.soc_addr` | u64 | `lea rcx,[rax+0x8]` → `call 41c3` @`0x4a70` | `"s2m_tail_ptr"` @`0x6515d` |
| `+0x10` | 4 | `mask` | u32 | `mov ebx,DWORD PTR [rax+0x10]` @`0x4b70` | `"mask"` @`0x6516a` |

`m2s_tail_ptr` / `s2m_tail_ptr` are the **DMA-ring tail-pointer CSR addresses** the
firmware writes to **kick** an APB-broadcast DMA (one write reaches all masked peers
at once); `mask` is the broadcast target set. For the device barrier this is the
engine that **atomically fans each step's semaphore increments out to all
participating peer dies**. [fields **HIGH/OBSERVED**; broadcast role **INFERRED/MED**,
from the identical `m2s`/`s2m` tail-ptr semantics in the ring channels.]

---

## 8. Device leaf primitives — scalar-LX WAIT-GE and fenced SIGNAL

The host decoder recovers only the **layout**; the firmware *executes* the
arrive/wait using the scalar-Xtensa-LX semaphore primitives in the carved NCFW IRAM
blob. Re-decoded this pass with `xtensa-elf-objdump XTENSA_CORE=ncore2gp` on
**v3 CAYMAN IRAM** (carved from `v3_ncfw_iram_bin @0x79860`, 19 392 B / `0x4bc0`,
size confirmed against `v3_ncfw_iram_bin_size @0x7e420`).

**WAIT-GE @0x3498** — the `barrier_sema[k][i] >= target_sema_val[k][i]` spin of
§5.2 / §6 (**HIGH/OBSERVED** this pass; **CARRIED** from
[`mesh-collective.md`](./mesh-collective.md) /
[`main-dispatch-loop.md`](./main-dispatch-loop.md)):

```asm
; v3 CAYMAN IRAM @0x3498  (a10 = semaphore CSR ptr, a3 = target)
3498: c0 20 00   memw                 ; ordering fence before the CSR read
349b: 28 0a      l32i.n  a2, a10, 0   ; a2 = *barrier_sema  (load current count)
349d: 27 b3 f7   bgeu    a3, a2, 0x3498 ; spin while target a3 >= val a2 (release when val > target)
34a0: 1d f0      retw.n               ; windowed return once reached
```

This is one of a **family of ~10** such WAIT primitives (`{ge,le,eq,ne}` across two
register banks) in the helper bank `0x3100..0x36f0`; the device barrier composes the
**GE** member per `target_sema_val`.

**Fenced SIGNAL @0x31c7** — the CSR-write half (the on-device counterpart of the
ARRIVE increment) (**HIGH/OBSERVED** this pass; **CARRIED** from
[`mesh-collective.md`](./mesh-collective.md)):

```asm
; v3 CAYMAN IRAM @0x31c7  (a10 = semaphore CSR ptr, a4 = value)
31c7: c0 20 00   memw                 ; fence
31ca: 49 0a      s32i.n  a4, a10, 0   ; *barrier_sema = a4  (write the count)
31cc: c0 20 00   memw                 ; fence  (fenced CSR write)
```

> **GOTCHA — `ncore2gp` mis-groups the bytes *after* `0x31cc` as a Vision FLIX
> bundle.** Immediately past the second `memw`, the default `objdump` disassembler
> coalesces the next scalar-LX bytes into a fake 512-bit Vision bundle (`{ bbci.w15
> …; ivp_lvnx8u_i …; … }`). That is the known FLIX-mis-decode artifact for the
> **scalar-LX** management core — the real stream is scalar (the `1d f0` = `retw.n`
> is visible *inside* the garbled bundle). Resync at the next `retw.n` under the
> scalar-LX length rule; do **not** trust the bundle grouping here. [**HIGH** — the
> mis-decode is reproduced this pass; the debunk is established in
> [`main-dispatch-loop.md`](./main-dispatch-loop.md).]

**Hardware substrate.** Every semaphore `soc_addr` (the `barrier_sema`,
`dma_sync_sema`, `start_network_proxy`, host `barrier_start`/`done`, apb-bcast tail
ptrs) is a 64-bit pointer formatted `"0x%016lX"` by `ncfw_log_addr @0x41c3` (value
fmt `%s: "0x%016lX"` @`0x65127`, key `"soc_addr"` @`0x6511c`). These point into the
SoC master map's **semaphore / DMA-tail-pointer** CSR blocks — the same EVT_SEM
aperture documented in [`rdma-cross-die.md`](../../dma/rdma-cross-die.md):
`tpb_semaphores_read @0x1000` / `set @0x1400` / **`inc @0x1800`** (the atomic `+=`
target the ARRIVE writes hit) / `dec @0x1C00`. [`soc_addr` representation
**HIGH/OBSERVED**; CSR identity **CARRIED/MED** from the EVT_SEM page.]

> **NOTE — completion can ride the notification queue.** A semaphore-update write can
> raise a notification gated by the per-engine notific-control CSR; the device
> barrier's completion may therefore surface to the host as a notification-queue
> entry rather than a poll. The device-barrier struct itself carries **no** notific
> field — the linkage is via the shared semaphore-CSR-write → notific mechanism.
> [**MED/INFERRED**.]

---

## 9. Confidence ledger

**HIGH / OBSERVED** (read from disasm/bytes this pass):

* Full call tree (§2): parent passes the **same** `cfg=[rbp-0x60]` to host (`d613`)
  and device (`fef5`) sub-decoders; `execute_device_barrier` u8 @`+0x124` via `"%p"`.
* host_barrier = 2 `soc_addr` (`barrier_start@+0`, `barrier_done@+0x8`) — §3.
* device_barrier offsets `0x00..0x122`, every field width — §4 (load instructions
  quoted; `%u`/`%hu`/`%hhu` formats confirmed).
* step descriptor: **4 steps**, **`0x34`** stride (52·idx shift chain @`0xf331`),
  `m2s_val`/`s2m_val` u16, `barrier_sema[4]` u64 @`+0x4`, `target_sema_val[4]` u32
  @`+0x24` — §5.
* `barrier_sema` 4×u64 stride-8 (`[rax+rdx*8]` @`0xe24d`); `target_sema_val` 4×u32
  stride-4 (`[rax+rdx*4]` @`0xe9f6`) — §5.1/5.2.
* `dma_sync_sema` 4×u64 stride-8 @`cfg+0xd0`; `dma_apb_bcast` 20 B (m2s/s2m/mask)
  @`+0xf0`; `start_network_proxy` soc_addr @`+0x104` — §4/§7.
* 4-arch **byte-identical decoder sizes** ⇒ schema-wide layout — §1.
* **No** `barrier_id`/`timeout`/`phase`/`participant`-count strings in the binary — §4.
* `arch_id {0x05,0x0c,0x14,0x1c}` → v2/v3/v4/v4+ from `libncfw_get_image @0x1179`;
  **no** `0x24` (v5) leg — §1.
* `soc_addr = "0x%016lX"` semaphore/DMA-tail CSR pointers — §8.
* WAIT-GE @`0x3498` (`memw; l32i.n; bgeu …; retw.n`) and SIGNAL @`0x31c7`
  (`memw; s32i.n; memw`) byte-exact in carved v3 CAYMAN IRAM — §8.

**MED / INFERRED** (reasoned from field names + sibling carves, not executed):

* arrive/wait directionality and the per-step inc-remote / wait-local-GE sequencing
  (§6); the `add_semaphore_inc` / `add_semaphore_wait_ge_and_dec` mapping.
* `apb_bcast` = broadcast-delivery engine for barrier writes (§7).
* peer-die selection via `soc_addr` high bits (§4/§6).
* bracketing of ring/mesh phases via `execute_device_barrier` + the
  `PSEUDO_*_BARRIER` placement (§3/§6).
* notification-queue surfacing of barrier completion (§8).

**LOW / OPEN:**

* exact meaning of `m2s_val` vs `s2m_val` per step (counter vs expected-delta).
* whether all 4 `barrier_sema` slots per step are always populated or sparse
  (topology-dependent; set by firmware, not visible here).
* struct bytes past `+0x122` (per-arch tail padding) — not exposed by the decoder.

**ABSENT (the v5 wall):** the **MAVERICK / NC-v5 device-barrier interior** — no v5
NCFW image ships in `libncfw.so`; any v5 claim is **INFERRED/ABSENT**, never stated
as fact.

---

### See also

* [NEFF Host Barrier](./neff-host-barrier.md) — the host↔device handshake pair
  (`barrier_start`/`barrier_done`) and the step-config sequencing this barrier
  brackets.
* [`PSEUDO_CORE_BARRIER 0xD8`](../ops/core-barrier.md) /
  [`PSEUDO_SYNC_BARRIER 0xD5`](../ops/sync-barrier.md) /
  [`PSEUDO_DMABARRIER 0xC3`](../ops/dma-barrier.md) — the compiler pseudo-op barrier
  trilogy this device barrier is the collective-execution counterpart to.
* [RDMA Cross-Die SBUF→SBUF P2P](../../dma/rdma-cross-die.md) — the EVT_SEM
  increment substrate (`+0x1800`) and the `soc_addr` die-select bitfields.
* [NCFW Ring + Kangaring](./ring-kangaring.md) / [NCFW Mesh Collective](./mesh-collective.md)
  — the data-movement phases this barrier brackets.
* [NCFW pring Descriptors](./pring-descriptors.md) — the DMA descriptor-ring side of
  the transport block (`tdrbp`/`desc_count`).
* [NCFW DRAM Context Log](./ncfw-dram-ctx-log.md) — `neff_ctx.barrier_completed`
  (`+0x5c`); [NCFW IRAM Images](./ncfw-iram-images.md) — the four embedded blobs.
* [The NCFW Scalar-LX Management Core](../../uarch/ncfw-lx-core.md) — why these
  bodies decode under the scalar-LX rule, not FLIX.
