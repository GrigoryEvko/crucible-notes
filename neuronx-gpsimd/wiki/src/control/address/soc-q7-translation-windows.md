# The SoC ↔ Q7 Translation Windows

> **Scope.** This is the address-translation keystone for the GPSIMD engine: the
> complete, byte-grounded model of how a **32-bit Q7 NX-local** pointer maps into
> the **57-bit Cayman/Sunda SoC physical** space. Three views are reconciled here —
> the **device runtime software TLB** (`neuron_translate`), the **hardware
> window-register block** (the RTL `tpb_nx.h` CSRs), and the **host
> window-programming API** (`nxlib_window.h` + the `soc_window_manager` runtime).
> Everything below is derived from static analysis of the shipped customop static
> objects (Xtensa DWARF, disassembled with the shipped `ncore2gp` toolchain), the
> shipped RTL-generated CSR headers, and the shipped host `libnrtucode_internal.so`
> string/format table. Confidence is tagged `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`.

Related pages: the translate ABI [`../../abi/neuron-translate-windows.md`](../../abi/neuron-translate-windows.md),
the SEQ SoC window manager [`../../firmware/seq/soc-window-manager.md`](../../firmware/seq/soc-window-manager.md),
the Q7 ELF VADDR model [`../../abi/q7-elf-vaddr.md`](../../abi/q7-elf-vaddr.md),
the master SoC map [`soc-master-map.md`](soc-master-map.md),
the decode pipeline [`addr-decode.md`](addr-decode.md),
the unified memory map [`unified-soc-memory-map.md`](unified-soc-memory-map.md).

---

## 0. The one-paragraph model

The Q7 (Xtensa-NX `ncore2gp`) core dereferences a **32-bit NX-local** address space.
Anything in the **57-bit SoC physical** space (HBM, SBUF, hbm_scratch, EVT_SEM,
remote die/chip) is reached through a small set of **hardware window registers** in
the core's NX register block. On **Sunda** (NC-v2) that block is at NX
`0x00100000` and a window is a **`{lo, hi}` base pair** — eight 16-MB windows
(`MEM_WINDOW0..7`) plus two 64-MB windows, *not resizeable*. On **Cayman** (NC-v3)
and every generation after it, the block moved to a **TCAM** at relative offset
`0x2000`: each of **40** windows is a `{control, mask, match, replace}` entry that
matches an incoming address prefix and rewrites it. Two software layers ride on top:
the **device** `neuron_translate` — a 5-entry software TLB that caches `{SoC tag, NX
base, mask, &window-register}` and, on a miss, evicts a slot and **reprograms the
hardware window register**; and the **host** `nxlib_window` API + `soc_window_manager`
that stage/validate the same windows from the runtime side. The keystone
reconciliation (byte-exact): the device's three dynamic 16-MB slots program HW
registers `MEM_WINDOW3 / MEM_WINDOW5 / MEM_WINDOW6` (`0x100218 / 0x100228 /
0x100230`), leaving the other five 16-MB windows for the host runtime.

```
   Q7 NX 32-bit pointer  ──►  [ device TLB: neuron_translate ]  ──►  [ HW window reg ]  ──►  57-bit SoC phys
        0x07012345               5-entry sw cache, %3 round-robin       MEM_WINDOWn (Sunda)        0x2802012345
                                 hit = arith; miss = reprogram reg      TCAM window (Cayman)
                                          ▲
                                          │ staged/validated by
                                 [ host: nxlib_window + soc_window_manager ]
```

---

## 1. The target space — the 57-bit SoC physical address

`HIGH/CARRIED` — from [`addr-decode.md`](addr-decode.md) / the SoC decode headers.
A SoC address is 64-bit with bits `[57:0]` defined; the **LOCAL** (own-die) view:

| Bits | Field | Meaning |
| :--- | :--- | :--- |
| `[46:0]` | `LOCAL` | per-die intra-die byte address (128 TiB/die) |
| `[47]` | `DIE` | die selector (2 dies per Cayman package) |
| `[53:48]` | `CAYMAN_ID` | chip id in the 64-die mesh (2⁶ = 64) |
| `[54]` | `CAYMAN_ID_VALID` | route-by-chip-id enable |
| `[55]` | `RESERVED` | — |
| `[56]` | `PCIE_ATTR_RELAXED_ORDERING` | PCIe ordering attribute |
| `[57]` | `OK_TO_FAIL` | poison / fail-permitted attribute |

> **NOTE.** A translation window's **SoC tag** inherently carries the die/chip
> routing: a 16-MB Sunda window (mask `0xff…ff000000`) tags bits `[57:24]`, so the
> *same* NX window can be re-pointed at a remote die or chip simply by writing a tag
> with `DIE[47]` / `CAYMAN_ID[53:48]` set. This is the cross-die mechanism (§7).

---

## 2. The complete Q7 NX 32-bit address-space map

`HIGH/OBSERVED` for every NX base + route unless noted. Two structurally-distinct
parts: **(A)** fixed, directly-dereferenceable NX regions, and **(B)** the windowed
region where an NX slice is a movable view of a 57-bit SoC address.

| NX range | What it is | Reach / source |
| :--- | :--- | :--- |
| `[0x00000, 0x80000)` | IRAM / low NX (instruction image + early state). The SEQ IRAM loads at NX `0x0`. | direct NX deref; `FETCH_*` regs drive i-fetch |
| `[0x80000, 0x90000)` | **per-core dataram** (64 KiB): the Q7's own on-core DRAM, seen locally. | direct NX deref — the pointer *is* the address |
| `[0x90000, 0x100000)` | gap up to the NX register block | `INFERRED` structural |
| `[0x100000, 0x100840)` | **NX register block** (Sunda "Sequencer Registers"): `CONFIG`, `RUN_STATE`, `ENGINE_BASE_ADDR`, DMA rings, `FETCH_*`, `SEQUENCER_WINDOW`, **`MEM_WINDOW0..7`**, `TPB_WINDOW0..1`. *The window-config registers live here.* | memory-mapped CSRs; `aws_neuron_isa_tpb_nx_map.h` |
| `0x07000000` | dynamic 16-MB window #0 (NX base) | window → HW `MEM_WINDOW3` |
| `0x09000000` | dynamic 16-MB window #1 (NX base) | window → HW `MEM_WINDOW5` |
| `0x0a000000` | dynamic 16-MB window #2 (NX base) | window → HW `MEM_WINDOW6` |
| `0x80000000` | SBUF 64-MB **pinned** window (NX base) → SoC SBUF `[0x2000000000, 0x2004000000)` | tag = `ENGINE_BASE_ADDR − 41 MiB` |
| `0x84000000` | hbm_scratch 64-MB **pinned** window (NX base) → HBM-resident scratch heap | tag = `hbm_scratch & ~0x3FFFFFF` |

The NX bases are **byte-exact** from the `_init_translate_ctx` disassembly. The
`slli` immediates decode to:

```
slli a5,a5,24   with a5=7    →  7<<24  = 0x07000000   (dyn slot 0)
slli a4,a9,24   with a9=9    →  9<<24  = 0x09000000   (dyn slot 1)
slli a5,a10,25  with a10=5   →  5<<25  = 0x0a000000   (dyn slot 2)
slli a4,a15,31  with a15=-1  → -1<<31  = 0x80000000   (sbuf pin)
slli a7,a15,26  with a15=-31 → -31<<26 = 0x84000000   (hbm_scratch pin)
```

> **QUIRK — the proxy base `0x80000000` is NOT this window.** The hbm-scratch xmem
> allocator uses `0x80000000` as a *fake pool anchor* for offset bookkeeping (it
> subtracts the anchor back out and never dereferences through it). It coincides
> numerically with the SBUF window NX base but is a different thing. Do not conflate
> allocator bookkeeping with the real HW window. `HIGH/OBSERVED`.

---

## 3. The HW window-register block

### 3a. Sunda (NC-v2) — the `{lo, hi}` base-pair model

`HIGH/OBSERVED` — `aws_neuron_isa_tpb_nx_map.h` (header for NC-v2) and the
RTL `arch-headers/sunda/tpb_nx.h` agree byte-exact. The block is mapped at
`AWS_NEURON_NX_MEM_REG_BASE = 0x00100000`. Each window is **two consecutive 32-bit
registers** (`_LO` then `_HI`, 8-byte stride) forming a 64-bit SoC base; the RTL
field comment for each is simply `0:31 - addr` — **no sub-fields, no
valid/mask/match/replace bits at all**.

| Register | Offset | Abs NX addr | Role |
| :--- | :--- | :--- | :--- |
| `SEQUENCER_WINDOW_LO/HI` | `0x100/4` | `0x100100/104` | i-fetch sequencer window |
| `MEM_WINDOW0_LO/HI` | `0x200/4` | `0x100200/204` | 16-MB window 0 *(also the `init_dma_queue` sanity-check `WINDOW0_LO == SUNDA_APB_BASE 0xF0000000`)* |
| `MEM_WINDOW1_LO/HI` | `0x208/C` | `0x100208/20C` | 16-MB window 1 |
| `MEM_WINDOW2_LO/HI` | `0x210/4` | `0x100210/214` | 16-MB window 2 |
| `MEM_WINDOW3_LO/HI` | `0x218/C` | `0x100218/21C` | 16-MB window 3 **← device DYNAMIC slot 0** |
| `MEM_WINDOW4_LO/HI` | `0x220/4` | `0x100220/224` | 16-MB window 4 |
| `MEM_WINDOW5_LO/HI` | `0x228/C` | `0x100228/22C` | 16-MB window 5 **← device DYNAMIC slot 1** |
| `MEM_WINDOW6_LO/HI` | `0x230/4` | `0x100230/234` | 16-MB window 6 **← device DYNAMIC slot 2** |
| `MEM_WINDOW7_LO/HI` | `0x238/C` | `0x100238/23C` | 16-MB window 7 |
| `TPB_WINDOW0_LO/HI` | `0x300/4` | `0x100300/304` | separate "TPB window" pair |
| `TPB_WINDOW1_LO/HI` | `0x308/C` | `0x100308/30C` | separate "TPB window" pair |

Sunda window inventory = **8 `MEM_WINDOW` + 2 `TPB_WINDOW` + 1 `SEQUENCER_WINDOW` =
11 windows**, exactly the `NXLIB_MAX_WINDOWS_SUNDA_NX 11` the host header declares.

### 3b. Cayman (NC-v3) and after — the TCAM `{control, mask, match, replace}` model

`HIGH/OBSERVED` — `arch-headers/cayman/tpb_nx.h` (cross-validated against
`cayman/tpb_nx_consts.hpp`). **This is the generation shift.** The namespace prefix
changes from `…_NX_LOCAL_REG_…` to `CAYMAN_TPB_XT_LOCAL_REG_…` ("XT" = Xtensa local
regs), and the simple lo/hi pair is replaced by an **indexed TCAM array**:

```c
#define AWS_REG_CAYMAN_TPB_XT_LOCAL_REG_WINDOW_COUNT 40        // 40 windows
#define AWS_REG_CAYMAN_TPB_XT_LOCAL_REG_WINDOW_SIZE  0x1c      // 28 bytes = 7 × uint32 per window
#define AWS_REG_CAYMAN_TPB_XT_LOCAL_REG_WINDOW_CONTROL_OFFSET_START 0x2000
// per-window register N at:  0x2000 + index*0x1c + {0,4,8,0xc,0x10,0x14,0x18}
```

Each window is a **7-register, 28-byte struct**. The byte-exact field layout
(`VALUE_MASK` / `SHIFT` copied verbatim from the header):

| Sub-register | Rel. off | Field | `VALUE_MASK` | shift | Address bits it carries |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `WINDOW_CONTROL` | `+0x00` | bitfield (below) | — | — | — |
| `WINDOW_MASK_LO` | `+0x04` | `value` | `0xfff00000` | 20 | `mask_value[31:20]` |
| `WINDOW_MASK_HI` | `+0x08` | `value` | `0xff` | 0 | `mask_value[39:32]` |
| `WINDOW_MATCH_LO` | `+0x0c` | `value` | `0xfff00000` | 20 | `match_value[31:20]` |
| `WINDOW_MATCH_HI` | `+0x10` | `value` | `0xff` | 0 | `match_value[39:32]` |
| `WINDOW_REPLACE_LO` | `+0x14` | `value` | `0xfff00000` | 20 | `replace_value[31:20]` |
| `WINDOW_REPLACE_HI` | `+0x18` | `value` | `0x3ffffff` | 0 | `replace_value[63:32]` |

Reading the widths back into spans (byte-exact, **not assumed**):

- **`match`** = `{match_hi[7:0], match_lo[31:20]}` = a **20-bit field carrying
  address bits `[39:20]`**. The TCAM compares the incoming address prefix `[39:20]`.
- **`mask`** = `{mask_hi[7:0], mask_lo[31:20]}` = the **same 20-bit field over
  `[39:20]`** — the TCAM don't-care mask (a `1` bit = *compared*).
- **`replace`** = `{replace_hi[25:0]→[63:32], replace_lo[31:20]→[31:20]}` = the SoC
  output base, spanning **`[63:20]`** (44 bits populated). The minimum granule is
  **1 MiB** (bit 20) — matching the `nxlib` "1MB is the minimum window size" note.

`WINDOW_CONTROL` bitfield (`cayman/tpb_nx.h`, verbatim comments):

| Bits | Field | Mask | Meaning |
| :--- | :--- | :--- | :--- |
| `[0]` | `window_valid` | `0x1` | enable this window; clear before changing, set when state valid |
| `[1]` | `nx_dedicated` | `0x2` | only **NX**-initiated requests match this window |
| `[2]` | `q7_dedicated` | `0x4` | only **Q7**-initiated requests match this window |
| `[3]` | `single_q7_enable` | `0x8` | only the Q7 selected by `[6:4]` matches |
| `[6:4]` | `single_q7_select` | `0x70` | which Q7 instance |
| `[31:7]` | `reserved` | `0xffffff80` | — |

> **GOTCHA — match/mask widths are NOT 24/26 bits.** It is tempting to copy Sunda's
> 16-MB (`>>24`) / 64-MB (`>>26`) granule reasoning onto Cayman. The TCAM `match`/`mask`
> compare granule is **20 bits (1 MiB, bit 20)**, fully programmable per window — that
> is the whole point of "40 *resizeable* windows" vs Sunda's fixed 8×16 MB + 2×64 MB.
> The 24/26 numbers belong only to the Sunda software TLB's hard-coded masks (§4).

> **NOTE — the TCAM persists Cayman → v5.** `arch-headers/{mariana,mariana_plus,maverick}/tpb_nx.h`
> are byte-identical in structure (`WINDOW_COUNT 40`, `WINDOW_SIZE 0x1c`, base `0x2000`,
> `REPLACE_HI` mask `0x3ffffff`). The model change is purely **Sunda → Cayman**.
> The Maverick (NC-v5) *interior* match/replace behaviour beyond these widths is
> `INFERRED` — only the register widths are byte-grounded.

> **NOTE — these are the engine MEM-window TCAMs, not the fabric TCAMs.** The
> `URB`/`sfabric_urb` and `pcie_addr_swizzle` TCAMs elsewhere in the arch-headers are
> a *different* subsystem (interconnect routing / PCIe swizzle); the Q7/NX MEM-window
> TCAM lives **only** in `tpb_nx.h`. `HIGH/OBSERVED` (tree-wide sweep).

---

## 4. The device view — the runtime software TLB (`neuron_translate`)

`HIGH/OBSERVED` — re-disassembled byte-exact from `translation.o`
(`libneuroncustomop.a`) with the shipped `ncore2gp` `xtensa-elf-objdump`.
Four symbols:

| Mangled symbol | Demangled | Addr | Size |
| :--- | :--- | :--- | :--- |
| `_Z19_init_translate_ctxv` | `_init_translate_ctx()` | `0x000` | 269 B |
| `_Z20neuron_translate_ctxv` | `neuron_translate_ctx()` | `0x110` | 13 B |
| `_Z16neuron_translatePvy` | `neuron_translate(void*, unsigned long long)` | `0x120` | 323 B |
| `_Z31neuron_translate_mapping_lookupPvyPyS0_` | `neuron_translate_mapping_lookup(void*, ull, ull*, void**)` | `0x264` | 246 B |

### 4a. The context layout

```c
// _translation_ctx_t : 168 bytes
struct _map_record {            // 32 bytes
    uint64_t ptr;               // +0x00  SoC tag (the matched SoC base, full 64-bit)
    uint32_t window;            // +0x08  NX base of this window (0x07000000 / …)
    uint32_t _pad;              // +0x0c
    uint64_t mask;              // +0x10  64-bit AND mask
    volatile uint32_t *reg_loc; // +0x18  ABSOLUTE NX addr of MEM_WINDOWn_LO
    uint32_t _pad2;             // +0x1c
};
struct _translation_ctx_t {
    _map_record record[5];      // +0x00 .. +0x9F
    uint8_t     next_alloc;     // +0xA0  round-robin cursor (0..2)
};
```

The initial table (window/mask/`reg_loc` all pinned to the RTL `tpb_nx.h` offsets):

| idx | `ptr` (tag) | `window` | `mask` | `reg_loc` (abs NX) | granule | kind |
| :-- | :--- | :--- | :--- | :--- | :--- | :--- |
| 0 | runtime | `0x07000000` | `0xffffffffff000000` | `0x100218` (`MEM_WINDOW3_LO`) | 16 MB | dyn |
| 1 | runtime | `0x09000000` | `0xffffffffff000000` | `0x100228` (`MEM_WINDOW5_LO`) | 16 MB | dyn |
| 2 | runtime | `0x0a000000` | `0xffffffffff000000` | `0x100230` (`MEM_WINDOW6_LO`) | 16 MB | dyn |
| 3 | `ENGINE_BASE − 41 MiB` | `0x80000000` | `0xfffffffffc000000` | **unset** | 64 MB | pin (sbuf) |
| 4 | `hbm_scratch & ~0x3FFFFFF` | `0x84000000` | `0xfffffffffc000000` | **unset** | 64 MB | pin (scratch) |

> **CORRECTION 1 `HIGH/OBS` — `reg_loc` is the ABSOLUTE NX register address.** The
> init does `movi a11,16` then `const16 a11,0x218`. Xtensa `CONST16` shifts the
> existing register left 16 and ORs the imm16, so the stored value is
> `(0x10<<16)|0x218 = 0x00100218 = REG_BASE(0x00100000) + MEM_WINDOW3_LO(0x218)`.
> Likewise `0x100228 = MEM_WINDOW5_LO`, `0x100230 = MEM_WINDOW6_LO`.

> **CORRECTION 2 `HIGH/OBS` — the two pinned records have NO `reg_loc` store.**
> Enumerating every `s32i …,a2,<off>` in the init shows the stored ctx offsets are
> `{0,4,8,16,20,24,28, 32,36,40,48,52,56,60, 64,68,72,80,84,88,92,
> 96,100,104,112,116, 128,132,136,144,148, 160}`. The reg-location slots of rec3
> (`+120/+124`) and rec4 (`+152/+156`) are **absent** — left at allocation garbage.
> Benign: the `%3` round-robin only ever evicts slots `{0,1,2}`, so rec3/rec4
> `reg_loc` is never dereferenced. A latent fragility if a future change ever
> refilled a pinned slot.

> **CORRECTION 3 `HIGH/OBS` — the sbuf tag is computed from `ENGINE_BASE_ADDR`.**
> Init reads `l32i[0x10002c]` (`ENGINE_BASE_ADDR_HI`) and `l32i[0x100028]`
> (`ENGINE_BASE_ADDR_LO`), forms `tag = ENGINE_BASE + (-41<<20)` (i.e. engine base
> **minus 41 MiB**, with a high-word borrow via `saltu`), and caches it in the `.bss`
> global `_sbuf_window`. So the SBUF SoC base is a fixed displacement below the
> engine's own SoC base register. The runtime `ENGINE_BASE` value is itself runtime;
> only the registers + displacement are static.

### 4b. The translate algorithm

```c
// neuron_translate(ctx, soc_ptr)  — disasm @ 0x120
// SEARCH: 5-way unrolled linear scan (records 0..4)
for (i = 0; i < 5; i++)
    if ((soc_ptr & ctx->record[i].mask) == ctx->record[i].ptr)   // 64-bit compare
        goto HIT;                                                // i in {0..4}
goto MISS;                                                       // (i == 4 fell through)

HIT:    // @ 0x204 — pure arithmetic, NO eviction, NO HW write
    return ctx->record[i].window + (soc_ptr & ~ctx->record[i].mask);

MISS:   // @ 0x215 — evict the round-robin victim, REPROGRAM its MEM_WINDOWn reg
    victim = ctx->next_alloc;                       // 0..2
    rec    = &ctx->record[victim];
    rec->ptr    = soc_ptr & 0xff000000;             // and a4,a4,0xff000000  (lo prefix)
    rec->window = ctx->record[victim].window;       // unchanged (NX base is fixed)
    reg = rec->reg_loc;                             // = 0x100218 / 0x100228 / 0x100230
    memw;
    *(reg + 1) = (uint32_t)(soc_ptr >> 32);         // s32i a5,[reg+4]  = MEM_WINDOWn_HI
    *(reg + 0) = (uint32_t)(soc_ptr & 0xff000000);  // s32i a4,[reg+0]  = MEM_WINDOWn_LO
    memw;
    ctx->next_alloc = (victim + 1) % 3;             // 0xAAAAAAAB reciprocal: muluh→srli 1→addx2→sub
    return rec->window + (soc_ptr & 0x00FFFFFF);    // srli a2,(-1),8 = 0x00FFFFFF offset mask
```

The `%3` cursor uses the classic divide-by-3 reciprocal `0xAAAAAAAB`
(`muluh` → `srli …,1` → `addx2` (=`3×`) → `sub`), re-verified to map
`next∈{0..6}` → `{1,2,0,1,2,0,1}`. **The search scans all 5 records, but only the
3 dynamic slots are ever recycled** — the two 64-MB pins are HIT-only.

> **The keystone act:** programming the **software** TLB slot and the **hardware**
> window register is *one operation*. The miss path's two `s32i` to `reg_loc`
> write `{lo, hi}` into the NX `MEM_WINDOWn` register; after the second `memw`, the
> 16-MB NX window at `0x07/09/0a000000` physically resolves to the new SoC region.
> The `memw` fences order the register write against the subsequent NX dereference.

### 4c. The reverse probe

`neuron_translate_mapping_lookup(ctx, soc, *out_start, **out_end)` (`@ 0x264`) is
the **read-only** twin: same 5-way scan, but on a hit it writes
`[start, end] = prefix … (prefix | ~mask)` of the covering window and returns it;
on a full miss it returns `-1`. **No eviction, no HW reprogram.** It is the device
equivalent of the host `nxlib_soc_window_contains_target_address` (§5).

> **TRANSIENT-REFERENCE HAZARD.** The dynamic TLB is **depth-3**. Touching a 4th
> distinct 16-MB SoC region recycles a slot, invalidating any NX pointer still held
> into the evicted window. This is the same contract the host header states verbatim:
> *"Pointer will be valid until the next call to an `nxlib_` function."* `HIGH/OBS`.

---

## 5. The host view — staging and programming the windows

### 5a. `nxlib_window.h` — the public NX window API

`HIGH/OBSERVED` — header read in full. Window capacity per chip
(`NXLIB_MAX_WINDOWS`): **`SUNDA = 11`, `CAYMAN = 40`, `MARIANA(+) = 40`**, with the
verbatim comment:

> *"On Sunda, only 8 16MB and 2 64MB windows are available, and they are not
> resizeable. On Cayman+, 40 resizeable windows are available."*

This is the direct host-side corroboration of both HW models: the Sunda `11 = 8 + 2
+ 1` (the device uses 3 of the 8 16-MB + both 64-MB), and the Cayman `40` =
`WINDOW_COUNT 40`. Sizes: `NXLIB_WINDOW_SIZE_{16MB,32MB,64MB}`, default 16 MB.
`NXLIB_RESERVED_WINDOWS = 1` (the dynamic window). The API maps 1:1 onto the device
translate primitives:

| Host API | Device equivalent |
| :--- | :--- |
| `nxlib_allocate_soc_window(soc, **out)` | stage a **pinned** window |
| `nxlib_allocate_soc_window_with_size(soc, size, **out)` | sized pinned window |
| `nxlib_free_soc_window(window)` | release a pinned window |
| `nxlib_get_num_available_windows()` | remaining free windows |
| `nxlib_dynamic_translate(soc)` | `neuron_translate` dynamic path |
| `nxlib_soc_window_translate(window, soc)` | HIT on a pinned window (pure arith) |
| `nxlib_soc_window_contains_target_address(window, soc)` | `…_mapping_lookup` probe |
| `nxlib_soc_window_translate_or_dynamic(window, soc)` | pinned HIT else dynamic |

Result codes: `NXLIB_OK`, `NXLIB_WINDOW_ERROR_{INVALID, MAX_ALLOCATED, SIZE}`.
`nxlib_soc_window_t` is library-allocated (opaque) — the user cannot allocate it
directly.

### 5b. `libnrtucode_internal.so` — the `soc_window_manager` runtime

`HIGH/OBSERVED` (strings/format table). The runtime ships `soc_window_manager.hpp`,
`xt_window.hpp`, and the `program_window` / `update_window` / `get_window_addr` /
`push_unallocated_window` / `soc2xt_addr` functions. The log-format strings prove
the runtime models **both** window generations side by side:

```
R: program_window: num=%d, vld=%d, xt_addr=0x%llx, soc_addr=0x%llx, u_mask=0x%llx, l_mask=0x%llx
R: program_window: num=%d, mask=0x%llx, match=0x%llx, replace=0x%llx
R: update_window:  num=%d, xt_addr=0x%llx, soc_addr=0x%llx, u_mask=0x%llx, l_mask=0x%llx
```

The **first** `program_window` is the **Sunda** form — `{xt_addr (NX base),
soc_addr (SoC tag), u_mask:l_mask (the 64-bit mask split hi:lo)}` — field-for-field
the device `_map_record`. The **second** is the **Cayman TCAM** form —
`{mask, match, replace}` — field-for-field the `tpb_nx.h` TCAM window. (The `vld`
flag ↔ `WINDOW_CONTROL.window_valid`.) The `push_unallocated_window` is the host
free-list whose on-core fast path is the device round-robin.

> **CORRECTION vs SX-ADDR-17 `HIGH/OBS`.** The backing report documented only the
> Sunda `{xt_addr, soc_addr, u_mask, l_mask}` `program_window` form. This page adds
> the **second** format string — `program_window: num, mask, match, replace` — which
> is the host-side image of the Cayman TCAM window, and pins its field widths to
> `cayman/tpb_nx.h` (`mask`/`match` 20-bit over `[39:20]`, `replace` over `[63:20]`).
> The Sunda-only view in SX-ADDR-17 §5b is correct but incomplete.

### 5c. The host → device staging pattern (the PC-bounds twin)

`HIGH/OBS` for the PC-bounds path; `MED` that it is byte-identical plumbing. The
runtime's generic "write a 64-bit `{lo, hi}` SoC base/limit pair into a device
register-window field" pattern appears verbatim as the PC-bounds API
(`pc_bounds_soc_addr_lo` / `pc_bounds_soc_addr_hi` strings):
`nrtucode_core_enable_pc_bounds_check(core, lo, hi)` writes the pair through a vtable
WRITE callback into a device-access object field; the disable path writes the
degenerate inverted range. The window manager's `program_window` uses the same
`{lo, hi}` staging shape to write a window's `soc_addr` — the structural parallel.

---

## 6. The reconciliation — three views agree

`DYN` = dynamic 16-MB; `PIN` = pinned 64-MB; `DIR` = direct (no window).

| NX base | Kind | DEVICE (`translate` ctx) | HOST (`nxlib`/manager) | HARDWARE (reg + SoC region) |
| :--- | :-- | :--- | :--- | :--- |
| `0x07000000` | DYN | record 0; mask `0xff000000`; `reg=0x100218` | `nxlib_dynamic_translate`; `program_window num=?` | Sunda `MEM_WINDOW3` (`0x100218/21C`) / Cayman TCAM window |
| `0x09000000` | DYN | record 1; `reg=0x100228` | one of 3 dyn slots, `%3` | Sunda `MEM_WINDOW5` (`0x100228/22C`) |
| `0x0a000000` | DYN | record 2; `reg=0x100230` | dynamic-window path | Sunda `MEM_WINDOW6` (`0x100230/234`) |
| `0x80000000` | PIN | record 3; tag `ENGINE_BASE − 41 MiB`; mask `0xfc000000`; `reg` **unset** | pinned window; `program_window vld=1` | 64-MB window; SoC SBUF `[0x2000000000, 0x2004000000)` = STATE_BUF(32 M) + RESERVED10(pad) |
| `0x84000000` | PIN | record 4; tag `hbm_scratch & ~0x3FFFFFF`; `reg` **unset** | pinned window | 64-MB window; SoC = HBM-resident hbm_scratch heap |
| `[0x80000, 0x90000)` | DIR | (no TLB) | per-core SDMA aperture | per-core Q7 DRAM; local deref |
| `[0x100000, 0x100840)` | DIR | translate **writes** `MEM_WINDOWn` here | manager programs windows | NX register block (`tpb_nx.h`) |
| general HBM/PSUM/EVT_SEM/remote die | DYN | any other 16-MB region → 3-slot TLB | `nxlib_dynamic_translate` or a host-pinned spare | routed via `MEM_WINDOW3/5/6`; SoC = HBM / PSUM `0x2802000000` / EVT_SEM `0x2802700000` / remote `{DIE, CAYMAN_ID}` |

The explicit agreement points:

- **(a)** device `reg_loc 0x100218/228/230` == HW `MEM_WINDOW3/5/6_LO`
  (`tpb_nx.h`) == host `program_window` field set. `HIGH/OBS`
- **(b)** device "3 dyn 16 MB + 2 pin 64 MB" == `nxlib` "Sunda: 8×16 MB + 2×64 MB,
  not resizeable" (device uses 3 of the 8 + both 64-MB). `HIGH/OBS`
- **(c)** device transient-reference hazard == `nxlib` "valid until next `nxlib_`
  call". `HIGH/OBS`
- **(d)** device `mapping_lookup` == host `nxlib_soc_window_contains_target_address`.
  `HIGH/OBS`
- **(e)** device HIT `window + (ptr & ~mask)` == host `xt_addr + (soc & ~mask)`
  field model. `HIGH/OBS`
- **(f)** Sunda lo/hi `program_window` form **and** Cayman `mask/match/replace`
  form both exist in the host `.rodata` — the runtime models both HW generations.
  `HIGH/OBS`

> **MED — who owns windows `{0,1,2,4,7}`.** The device uses `MEM_WINDOW3/5/6`; the
> remaining five 16-MB windows + spare 64-MB capacity are available to the host /
> `nxlib` runtime (`window 0` is the APB/SUNDA control window the DMA-queue init
> sanity-checks). The exact per-window runtime owner is runtime; the contract is
> `nxlib`'s `NXLIB_RESERVED_WINDOWS`.

---

## 7. Cross-die / cross-chip addressing

`HIGH/OBS` die-bit; `MED` the translate-routes-the-barrier reading. An NX window does
**not** itself carry die/chip bits — those live in the 57-bit SoC tag the window
register is programmed with. To reach a remote die/chip from a Q7 core:

1. Compose the remote SoC address: set `LOCAL[46:0]` to the in-die offset, `DIE[47]`
   for the other die of the package, `CAYMAN_ID[53:48]` + `CAYMAN_ID_VALID[54]` for a
   remote chip in the 64-die mesh.
2. `neuron_translate(ctx, remote_soc_addr)`: the tag compare uses the **full 64-bit**
   mask, so a remote tag is a distinct TLB entry; a miss programs a dynamic window's
   `MEM_WINDOWn` with the remote `{lo, hi}` SoC base — *including its `DIE` /
   `CAYMAN_ID` high bits*. The 16-MB NX window then resolves to that remote block.
3. The `OK_TO_FAIL[57]` / `PCIE_ATTR_RELAXED_ORDERING[56]` attribute bits ride in the
   tag's high dword (programmed into `MEM_WINDOWn_HI`) for poison/ordering control.

On Cayman this is the same act through the TCAM: `replace[63:20]` carries the remote
`{DIE, CAYMAN_ID, LOCAL}` SoC base directly (replace spans bits up to `[63:32]`).

> **NOTE — EVT_SEM is not pinned.** EVT_SEM (SoC `0x2802700000`, 1 MiB) has **no
> dedicated pinned window** (the two pins are sbuf + hbm_scratch only). A Q7 op that
> must poke an EVT_SEM semaphore reaches it through a **dynamic** 16-MB window
> (EVT_SEM fits one slot); a cross-die barrier is just the local EVT_SEM offset with
> `DIE[47]` set. `HIGH/OBS` that EVT_SEM is absent from the pinned set; the
> dynamic-window route is `INFERRED`-strong from the absence + the granule fit.

---

## 8. The dynamic-TLB vs fixed-window bound

`HIGH/OBS` + `MED`. The device draws a clean line between **general HBM access** (the
dynamic TLB) and the **fixed regions** (the pins + direct NX regions):

| Region class | Route | Window kind / count |
| :--- | :--- | :--- |
| per-core dataram | direct NX deref `[0x80000,0x90000)` | none |
| NX register block / IRAM | direct NX deref (low NX / `0x100000`) | none |
| SBUF (32 MiB) | pinned 64-MB window @ NX `0x80000000` | 1 fixed (rec3) |
| hbm_scratch (HBM) | pinned 64-MB window @ NX `0x84000000` | 1 fixed (rec4) |
| general HBM tensor | dynamic 16-MB windows @ `0x07/09/0a000000` | 3 dyn, round-robin `%3` |
| HBM stack (≤4 MB) | one dynamic 16-MB window (fits whole) | 1 of the 3 dyn slots |
| PSUM (4 MiB) | dynamic 16-MB window (Q7 sw access) | 1 of the 3 dyn slots `MED` |
| EVT_SEM (1 MiB) | dynamic 16-MB window (§7) | 1 of the 3 dyn slots `MED` |

- **Fixed regions** are touched **without eviction**: dataram/IRAM/CSRs by direct
  deref; SBUF/hbm_scratch by a HIT on a pinned window (pure arithmetic). They never
  consume a dynamic slot and are always resident.
- **General access** (any other 16-MB SoC region, including remote die/chip) goes
  through the 3-deep dynamic TLB: a hit is arithmetic; a miss evicts the round-robin
  victim and reprograms its window register. **A working-set > 3 distinct 16-MB
  regions thrashes.**
- The host can add pinned windows (`nxlib_allocate_soc_window`) out of the spare HW
  windows, moving a hot region off the thrashing dynamic set. `MED`.

Corroborating geometry (`aws_neuron_isa_tpb_common.h`, byte-exact):
`STATE_BUF` 128 partitions × 256 KiB = **32 MiB** (`STATE_BUF_SZ 0x2000000`);
`PSUM` 128 part × 16 banks × 1024 B = **4 MiB** (`PSUM_BUF_SZ 0x400000`);
`partOffset[25]` = 0 SBUF / 1 PSUM; `TPB_PARTITION_ADDR_MASK 0x1fffffff`;
HBM stack capacity 16 GiB.

---

## 9. Worked example — one 32-bit Q7 address → 57-bit SoC physical

**Goal:** a Q7 op wants to read SoC PSUM `0x2802012345` (PSUM base `0x2802000000` +
offset `0x12345`). Cold cache, dynamic slot 0 (`window 0x07000000`) is the victim.

### Path A — Sunda software TLB (`neuron_translate`)

```
neuron_translate(ctx, 0x2802012345):
  SEARCH: no record matches (cold)  → MISS, victim = next_alloc = 0
  rec0.ptr  = 0x2802012345 & 0xff000000 = 0x02000000      // lo prefix install
  reg       = rec0.reg_loc = 0x100218 (MEM_WINDOW3_LO)
  memw
  *(0x10021C) = (0x2802012345 >> 32)        = 0x00000028   // MEM_WINDOW3_HI
  *(0x100218) = (0x2802012345 & 0xff000000) = 0x02000000   // MEM_WINDOW3_LO
  memw
  next_alloc = (0+1) % 3 = 1
  return rec0.window + (0x2802012345 & 0x00FFFFFF)
       = 0x07000000 + 0x012345
       = 0x07012345                                        // NX pointer handed back
```

The Q7 then dereferences NX `0x07012345`. The hardware `MEM_WINDOW3` (now holding
SoC base `0x2802000000`) resolves it to SoC **`0x2802012345`**. ✓

### Path B — Cayman hardware TCAM (one window, 16-MB span)

To express the same 16-MB window in the TCAM, the low 24 address bits are
don't-care, so the `[39:20]` care-mask keeps `[39:24]` and clears `[23:20]`:

```
WINDOW_CONTROL : window_valid=1, q7_dedicated=1
WINDOW_MATCH   : match[39:20] = (0x07000000 >> 20) = 0x070   → match_lo[31:20]=0x070, match_hi=0x00
WINDOW_MASK    : care[39:24]=1, [23:20]=0  → field 0xffff0   → mask_lo[31:20]=0xff0,  mask_hi=0xff
WINDOW_REPLACE : replace[63:20] = (0x2802000000 >> 20) = 0x28020  → replace_lo[31:20]=0x020, replace_hi[25:0]=0x28

access NX 0x07012345:
  field[39:20] = 0x070;  (field & care) = 0x070 == (match & care) = 0x070   → HIT
  out[63:24]   = replace[63:24]  (= 0x2802000000's high bits)
  out[23:0]    = NX[23:0] = 0x012345    (passthrough — don't-care bits)
  translated   = (0x2802000000 & ~0xFFFFFF) | (0x07012345 & 0xFFFFFF)
               = 0x2802012345                                              // ✓ identical to Path A
```

Both the software TLB and the hardware TCAM map NX `0x07012345` → SoC
**`0x2802012345`**. The software TLB caches what the hardware register/TCAM then
physically enforces; the host `program_window` (Sunda `{xt_addr,soc_addr,u_mask,
l_mask}` or Cayman `{mask,match,replace}`) is the runtime image of the same write.

---

## 10. Failure modes / guard asymmetry

`HIGH/OBS`.

- **Device has no bounds/null guard** in `neuron_translate`: the MISS path
  unconditionally evicts a slot and programs the window register for *any* 64-bit
  ptr; an out-of-range ptr yields an out-of-range window silently on-core.
- **Host is the guard:** `cayman_memory_bounds` (a `libnrtucode` `.rodata` table)
  validates SoC addresses; `nxlib_allocate_soc_window` returns
  `NXLIB_WINDOW_ERROR_{INVALID, MAX_ALLOCATED, SIZE}`; the PC-bounds path validates
  `lower < upper`.
- **Null ctx:** `neuron_translate` dereferences `ctx->record[0].mask` (`ctx+0x10`)
  with no null check → fault.
- **Working-set overflow:** a 4th distinct 16-MB region evicts a dynamic slot,
  invalidating held NX pointers (the transient-reference hazard).
- **Pinned-window `reg_loc` uninitialised** (CORRECTION 2): benign today because the
  `%3` cursor never selects slots 3/4 — but a code change that refilled a pinned slot
  would dereference garbage.
- **Startup ordering:** `init_dma_queue` asserts `MEM_WINDOW0_LO == SUNDA_APB_BASE
  0xF0000000` — the APB control window must be pre-programmed before the SDMA ring is
  stood up.

---

## 11. Confidence ledger

**HIGH / OBSERVED** (native Xtensa disasm re-verified + RTL/ISA header text + host
strings):

- `reg_loc 0x100218/228/230 = MEM_WINDOW3/5/6_LO` (`CONST16`-shift decode pinned to
  `nx_map.h` / `sunda/tpb_nx.h`); MISS path writes `*(reg)/*(reg+4) = lo/hi`.
- rec3/rec4 `reg_loc` NOT stored (store-offset map has no `+120/+152`); benign.
- rec3 sbuf tag = `ENGINE_BASE_ADDR (0x100028/0x10002c) − 41 MiB`, cached in
  `_sbuf_window`.
- The full window/mask/granule table (3 dyn 16 MB + 2 pin 64 MB; masks
  `0xff000000` / `0xfc000000`) — re-decoded byte-exact.
- The translate hit/miss arithmetic + `%3` round-robin (`0xAAAAAAAB`).
- **Sunda** `tpb_nx.h` / `nx_map.h`: 8 `MEM_WINDOW` lo/hi pairs (`0:31 - addr`, no
  sub-fields) + `TPB_WINDOW` + `SEQUENCER_WINDOW` at REG_BASE `0x100000`.
- **Cayman** `tpb_nx.h` TCAM: `WINDOW_COUNT 40`, `WINDOW_SIZE 0x1c`, base `0x2000`,
  per-window `{control, mask_lo/hi, match_lo/hi, replace_lo/hi}`; `mask`/`match`
  20-bit over `[39:20]`, `replace` over `[63:20]` (masks `0xfff00000` / `0xff` /
  `0x3ffffff`); `control` valid + nx/q7-dedicated + single_q7_select bits.
- `nxlib_window.h`: SUNDA 11 / CAYMAN 40 / MARIANA(+) 40 windows; the allocate /
  free / translate / contains / dynamic API; the transient-reference caveat —
  verbatim header text.
- Host `program_window` **both** forms (Sunda `{xt_addr,soc_addr,u_mask,l_mask}` and
  Cayman `{mask,match,replace}`); `update_window`; `push_unallocated_window`;
  `soc2xt_addr`; `pc_bounds_soc_addr_{lo,hi}`.
- `common.h` geometry: SBUF 128 part × 256 KiB = 32 MiB; PSUM 16 banks × 1024 B =
  4 MiB; `partOffset[25]` SBUF/PSUM select; `TPB_PARTITION_ADDR_MASK 0x1fffffff`;
  HBM stack 16 GiB.

**MED:**

- Host-reserves-windows-`{0,1,2,4,7}` (the `nxlib` reserved-window contract +
  `window0=APB` observed; exact runtime per-window owner is runtime).
- PSUM / SBUF-scratch / EVT_SEM ride dynamic windows for Q7 sw access (the pins are
  sbuf + hbm_scratch only — observed absence; the dynamic route inferred).
- The host window-manager vtable WRITE is byte-identical plumbing to the PC-bounds
  `{lo,hi}` staging (PC-bounds path observed; window-manager write not separately
  disassembled).

**LOW / NOTED:**

- The literal runtime `ENGINE_BASE_ADDR` / `hbm_scratch` / window-tag *values* are
  runtime-populated; only the registers, displacements, masks, and NX bases are
  static.
- The Maverick (NC-v5) TCAM *interior* behaviour beyond the register widths is
  `INFERRED` (widths byte-grounded; the v5 match/replace semantics carried from the
  Cayman model the headers share).

**INFERRED** (architectural, no contradicting evidence):

- The `[0x90000, 0x100000)` NX gap between dataram and the register block.
- "PSUM / SBUF-scratch / EVT_SEM each occupy one of the 3 dyn slots when touched."
