# NCFW LX-ISA / DMA-Naming / arch_id-Diff / Orchestration Synthesis

This is the **capstone of the NCFW lane** of Part 10. It synthesizes the thirteen
NCFW sibling pages ([`ncfw-iram-images`](ncfw-iram-images.md) …
[`ring-protocol-config-command`](ring-protocol-config-command.md)) and the fourteen
[`collectives/ops`](../ops/architecture-synthesis.md) pages into four reimplementation-grade
deliverables, each its own top-level section:

1. **[The NCFW management-core Xtensa ISA](#1-the-ncfw-management-core-xtensa-isa)** — core type, register
   model, calling convention, the *definitive* no-native-disassembler / FLIX-mis-decode
   explanation (demonstrated live with the shipped `xtensa-elf-objdump`), and the decodable-subset rule.
2. **[The DMA-engine naming taxonomy + NC-EVENT register table](#2-the-dma-engine-naming-taxonomy--nc-event-register-table)** —
   the engine-id → base model, the logical-DMA → (named-engine, doorbell, completion-event) triple, the engine table.
3. **[arch_id / coretype reconciliation + the 4-image-vs-5-gen map + the per-gen diff + capability table](#3-arch_id--coretype-reconciliation-the-4-image-vs-5-gen-map-the-per-gen-diff)** —
   the v5/MAVERICK file-absent wall.
4. **[The NCFW collective-orchestration architecture, end-to-end](#4-the-ncfw-collective-orchestration-architecture-end-to-end)** —
   host command → TOP_SP lowering → NCFW dispatch → algorithm → DMA post/wait → completion, as one annotated flow.

Everything below is derived from static/binary analysis of the shipped redistributable
host libraries and the device firmware images they embed. Every claim is anchored to an
address / offset / symbol / enum / opcode / string and tagged
**[CONF × PROVENANCE]** where CONF ∈ {HIGH, MED, LOW} and PROVENANCE ∈ {OBSERVED
(bytes/disasm read directly), INFERRED (architectural reading over observed facts),
CARRIED (from a cited sibling at its stated confidence)}. v2–v4 are byte-grounded;
**every v5/MAVERICK claim is INFERRED or ABSENT, never OBSERVED.**

**Target binaries** (all three identity anchors re-verified):

| binary | role | sha256 / BuildID | size |
|--------|------|------------------|------|
| `libncfw.so` | host: NCFW image-blob accessor + `ctx_log` JSON pretty-printer; embeds the 4 device IRAM+DRAM images as raw `.rodata` blobs | `598920d7…` / BuildID `a98f8e1c…` | 615640 B |
| `libnrt.so.2.31.24.0` | host RunTime — the authoritative builder/driver (carries DWARF) | BuildID `8bb57aba…` | 122956336 B |
| `libnrtucode_internal.so` | GPSIMD ucode internal — plain C fn-ptr tables (zero `_ZTV`) | — | — |

> **NOTE — verification gotchas applied throughout.** `.text`/`.rodata` are
> VMA == file-offset for both `libncfw.so` and `libnrt.so` (`readelf -SW`: `libnrt`
> `.rodata` VMA `0x7cf000` == fileoff `0x7cf000`); the ncore2gp config DLLs (a *different*
> binary set) carry a `0x200000` `.data` delta, not relevant here. `libncfw.so` /
> `libnrtucode_internal.so` use plain **C fn-ptr tables** (slot N = symbol + 8·N), NOT
> C++ `_ZTV+0x10` — confirmed `nm libnrtucode_internal.so | rg -c _ZTV` → **0**. Counts
> are re-grounded to `nm <lib> | rg -c`.

---

## 1. The NCFW management-core Xtensa ISA

### 1.1 Core type — scalar Xtensa-LX, NOT FLIX

The NCFW management core is a **scalar Xtensa-LX control core**: base ISA + windowed
registers (XEA2) + 16-bit code density + zero-overhead loops + a small option set, with
**no Vision/SIMD datapath and no FLIX/VLIW layer**. It is a *different* core from the
GPSIMD Vision-Q7 NX datapath core (`ncore2gp`/Cairo) on which the customop kernels run;
the two share only the windowed+density **base** ISA. **[HIGH × OBSERVED]** — see the
dedicated uarch page [`uarch/ncfw-lx-core.md`](../../uarch/ncfw-lx-core.md) for the full
register/SR census; this section gives the *decode* consequence for the collective lane.

The register model is a windowed AR file (logical `a0`–`a15` over a larger physical file,
`call8`/`call12` rotation). The dispositive evidence is the canonical
`WindowOverflow8`/`WindowUnderflow8` spill/fill handler at **IRAM `0x24`**, which is
**byte-identical across all four images** (`80 01 09 90 01 09 a0 11 09 b0 41 09` …) — an
`l32e`/`s32e` (windowed-exception load/store) sequence that can only exist on a windowed
AR core. The calling convention is the standard windowed XEA2 ABI: `entry a1,N` prologues
(frame sizes {32,48,64,80,96}), `retw.n` epilogues, `call8`/`call0` calls, `l32r` literal
loads, `a2`–`a7` → `a10`–`a15` rotation. The SR set is the standard LX registry
(`WINDOWBASE`/`PS`/`VECBASE`/`EXCCAUSE`/`EXCVADDR`/`PRID`/`MEMCTL`/`MS`/`ISB`/`ISL`/`MPUENB`
/`PREFCTL`/`LBEG`/`IBREAKC0`); **no TIE/coprocessor SRs**. The idle point is a single
`waiti 15` per image (v3 at IRAM **`0x4b6c`**, decoded cleanly with the shipped objdump:
`00 7f 00 → waiti 15`). **[HIGH × OBSERVED]**

A per-generation **boot marker byte at IRAM `0x1022`** distinguishes the images even
within a shared prologue: SUNDA `0x08`, CAYMAN `0x09`, MARIANA `0x0a`, MARIANA_PLUS
`0x0a`. (It is at `0x1022`, **not** `0x1021`.) **[HIGH × OBSERVED]**

### 1.2 The decode limit — the definitive FLIX-mis-decode demonstration

The single most-repeated NCFW artifact is the "~26-28% FLIX" figure. It is a **decoder
artifact, not a property of the core.** The root cause: the shipped corpus contains
**exactly one** Xtensa configuration — `ncore2gp` (the Vision-Q7 NX datapath core: one
`core-isa.h`, one `*-params`, zero `.tie`/`.flix`, zero second xtensa-core dir). The
native `xtensa-elf-objdump` registers `ncore2gp` as its only core, and that config's
FLIX format-length table assigns **op0 = `0xe` → 16-byte (5-slot Vision) bundle** and
**op0 = `0xf` → 8-byte bundle**, with Vision SIMD `ivp_*` slots. On the scalar-LX NCFW
bytes, every `op0 = e/f` byte (which is just the tail/operand of an ordinary scalar
2-/3-byte instruction) is greedily consumed as a wide Vision bundle, mis-decoded, then
re-synced via `.byte`. **[HIGH × OBSERVED]**

This is demonstrated *live* at the v3 dispatch prologue. Decoding `v3_iram.bin` with
`XTENSA_CORE=ncore2gp xtensa-elf-objdump -D -b binary -m xtensa` produces, at IRAM
`0x3bd9`, a **single "instruction" 16 bytes wide**:

```text
3bd9: 9e650800a004577a80fec20a4061a427
      { bbsi.w15 a5,25,0xc96; ivp_lsn_4x64_i v0,a8,0; uneq.s vb14,v2,v0;
        ivp_float16nx16t v24,v13,6,vb7; ivp_babssubu2nx8 vb0,v1,v7,v26 }
```

A 5-slot 512-bit SIMD float-blend/absolute-difference bundle, **inside the dispatch
prologue of a DMA/collective control core** — architecturally impossible. The bytes are
scalar; the bundle is fabricated by the wrong config. This is the entire mechanism of the
"FLIX mis-decode." **[HIGH × OBSERVED]**

> **GOTCHA — do not report any `ivp_*` / `{ … }` content from an NCFW image.** Any
> `{ … }` bundle the `ncore2gp` objdump emits over NCFW bytes is spurious. The two prior
> "NCFW is genuine FLIX" lineages (radare2's stock-xtensa 8-byte FLIX heuristic; the
> `ncore2gp` 16-byte one) are *both* decoder artifacts of applying a FLIX width to scalar
> bytes — neither is evidence of an NCFW FLIX layer (the core has no datapath to feed one).

### 1.3 The decodable subset — the LX length rule

The base/windowed/density ISA is **config-independent** and decodes correctly under any
Xtensa config; only the Vision-FLIX layer is spurious. The practical rule for reading NCFW
code on the device side:

> **The LX decode rule.** Step instructions at **op0 nibble `e`/`f` ⇒ 3 bytes**, else 2
> bytes; **resync at `retw.n`** (byte pattern `1d f0`). Treating `op0=e/f` as wide bundles
> over-consumes bytes and loses sync; the 3-byte scalar rule resyncs to the genuine
> `retw.n` function-ends decisively better (v3/v4/v4+ ≈ 67–75% landed vs 49–61% for either
> FLIX width). **[HIGH × CARRIED from [`uarch/ncfw-lx-core.md`](../../uarch/ncfw-lx-core.md)]**

The reliable subset (~70–79% of bytes for v3/v4/v4+, ~70% for the denser v2) is the
**dispatch spine**: function boundaries (`entry`/`retw.n`), the call graph
(`call8`/`call0`), the `j`/branch control flow, `l32r`/`l32i.n`/`s32i.n` data flow,
`const16`/`extui`/`addx4`, the SR boot sequence, the `waiti 15` idle, and — critically —
the dispatch read itself (§4.3). The per-algorithm **case bodies** and the exact on-core
**step schedule** are NOT instruction-decodable; they are recovered host-DWARF-primary
(from `libnrt`/`libncfw`) and structurally (the DRAM tables + the decodable spine). This
is the universal MED/LOW boundary restated in every NCFW page.

### 1.4 Per-generation ISA stability

The ISA family does **not** change across SUNDA → CAYMAN → MARIANA → MARIANA_PLUS: the
`WindowOverflow8` handler at `0x24` is byte-identical (§1.1); the SR set is identical; the
scalar 2-/3-byte length profile is identical; each image has exactly one `waiti 15`. The
images differ in *code shape*, not ISA: v2/SUNDA is a ~2.2× larger monolith (43 KB vs ~19
KB) but shares its first **121 (`0x79`) IRAM bytes** with v3/CAYMAN (the reset+window
prologue family). The reset-`j` magic groups the gens `{v2,v3}→j 0x1dc` (`06 76`) vs
`{v4,v4+}→j 0x1f8` (`06 7d`) — confirmed byte-exact in the IRAM heads. **[HIGH × OBSERVED]**

---

## 2. The DMA-engine naming taxonomy + NC-EVENT register table

### 2.1 How NCFW names DMA — by engine-id + bitmap, not by class

The NCFW management firmware names DMA **purely numerically**, never by SoC class. Keyword
census of `libncfw.so` (`strings | rg -cw`): **`SDMA` = 0, `DDMA` = 0, `CDMA` = 0,
`UDMA` = 0**. The entire DMA vocabulary it carries is index/bitmap/role based — the
`ctx_log` JSON keys (each appearing **×4**, one byte-identical copy per arch):
`dma_engines_bitmap`, `queue_id`, `m2s_tail_ptr`/`s2m_tail_ptr`, `tdrbp_low`/`tdrbp_high`,
`dma_apb_bcast`, `dma_sync_sema`, `dma_compl_sema`, `dma_trigger`, `barrier_sema`.
**[HIGH × OBSERVED]**

The engine namespace is a **flat engine-id** `0..(16·tpb − 1)`, at most 32 per TPB, with
16 queues each. The host RunTime carries the constants:
`{CAYMAN,MARIANA,SUNDA}_NUM_DMA_ENGINES_PER_TPB = 16`, `MAX_NUM_DMA_ENGINES_PER_TPB = 32`,
`GET_SUNDA_ENG_IDX_FROM_TPB_ENG(tpb,eng) = 16·tpb + eng`, and the bitmap test
`(1 << dma_engine_id) & valid_dma_engines_bitmap`. The per-gen getter
`tdrv_arch_get_num_dma_per_tpb_cayman@0x30ba10` returns `mov eax,0x10` (16).
**[HIGH × OBSERVED]**

> **NOTE — reconciliation with the DDMA/CDMA/UDMA SoC taxonomy.** On the four NCFW gens
> (all pre-MAVERICK), the SoC name is **SDMA**, the engine IP is **UDMA**
> (`al_mla_udma_*` objects ×3 gens; `libnrt` keyword `UDMA = 5`, `DDMA = CDMA = 0`,
> `SDMA = 1`), and the RTL dir is `sdma`. The MAVERICK rename SDMA → {DDMA (Data) / CDMA
> (Compute)} is a *later* SoC-level change above this gen set; UDMA is the invariant engine
> core. NCFW's class-agnostic engine-id/bitmap naming **survives the rename** because it has
> no class field. **[engine-id/bitmap facts HIGH × OBSERVED; "survives the rename"
> INFERRED-STRONG — the four gens predate MAVERICK]**

### 2.2 The NC-EVENT register table — half (a): the EVT_SEM op-window file

The on-chip semaphores are the **EVT_SEM** unit: 256 events (1-bit) + 256 semaphores
(32-bit counters) per instance, exposed as **four op-aliased windows over the same 256
physical counters**. The host RunTime computes each window's SoC address with a dedicated
getter; the four offsets are byte-exact in the disassembly (`lea rax,[rax+rbx*4+OFF]`,
`rbx*4` = `sema_idx·4` stride):

| op | window | offset | getter (cayman) @addr | access |
|----|--------|--------|------------------------|--------|
| READ  | `SEMAPHORE_READ` | `+0x1000` | `cayman_get_sp_sema_r_ofst@0x25c130` | RO — WAIT-GE poll |
| SET   | `SEMAPHORE_SET`  | `+0x1400` | `cayman_get_sp_sema_s_ofst@0x25c360` | WO — overwrite/init |
| INC   | `SEMAPHORE_INC`  | `+0x1800` | `cayman_get_sp_sema_i_ofst@0x25c2e0` | WO — atomic +=, arrive/signal |
| DEC   | `SEMAPHORE_DEC`  | `+0x1C00` | `cayman_get_sp_sema_d_ofst@0x25c260` | WO — atomic −=, consume |

The base arithmetic (from `_i_ofst@0x25c2e0`): `shl rdx,0x1e` (bit 30 = TOP_SP-within-die)
+ `shl rax,0x2f` (bit 47 = die) — i.e. the TOP_SP EVT_SEM copy is keyed on
`(topsp_idx & 7)` and the die bit. SUNDA uses the same window offsets but a `shl edx,0x16`
(bit 22) base shift (the smaller fabric). **[HIGH × OBSERVED]** — these are byte-identical
to the [`spad-ccop-tsync`](spad-ccop-tsync.md) EVT_SEM windows and the
[`ops/sync-barrier`](../ops/sync-barrier.md) substrate.

### 2.3 The NC-EVENT register table — half (b): the named-event index table

The `<arch>_sync_events` struct (`sunda@0x9de8a0`, `cayman@0x9def80`, `mariana@0x9df660`;
all 20 B, **byte-identical**: `[0,1,2,3,35,3,0,3,4,5,5,6,7,8,9,10,13,14,18,3]`) assigns
each named NeuronCore event a small index into the 256-event EVT_SEM array. Each
`tdrv_sync_get_<name>` getter reads one byte at a fixed struct offset:

| name | idx | name | idx |
|------|-----|------|-----|
| `INFERENCE_START` | 0 | `ACT_TABLES_LOAD` | 6 |
| `CORE_BARRIER` | 1 | `DVE_TABLES_LOAD` | 7 |
| `SYNC_BARRIER` | 2 | `HW_EXEC_QUEUE_REQUEST_LOAD` | 8 |
| `PREAMBLE_PINNED_QUEUES_START` | 3 | `COLLECTIVES_CTX_LOAD` | 9 |
| `IOQ_CODE_SWITCH` | 4 | `COLLECTIVE_TOPSP_ACK_FIRST` | 10 |
| `RANGE_CHECK_START` | 5 | `COLLECTIVE_TOPSP_ACK_LAST` | 13 |

`NUM_RESERVED_SEMAPHORES = 3`. **[HIGH × OBSERVED — 20 struct bytes + getter offsets]**

### 2.4 The firmware's own base table — DRAM+0x10 soc_addr

The NCFW DRAM header carries the firmware-side base table at **DRAM+0x10** (20 × u64 on
v3/v4, byte-identical; 4 entries on v2/SUNDA). Carved byte-exact from `v3_dram.bin`:

| idx | off | soc_addr (u64 LE) | decode |
|-----|-----|-------------------|--------|
| 0–3 | `+0x10`–`+0x28` | `…02700000` (die0) | `TPB_0..3_EVT_SEM` bases |
| 4–7 | `+0x30`–`+0x48` | `…02700000` (die1 = `\| bit47`) | `TPB_4..7_EVT_SEM` bases |
| 8–15 | `+0x50`–`+0x88` | `0x0…02/03/04/05000000` | TPB-internal control sub-blocks (die-banked) |
| 16–17 | `+0x90`/`+0x98` | `0x8006800000` / `0x8006900000` | SDMA `D2H` / `H2D` host-transfer (die0) |
| 18–19 | `+0xa0`/`+0xa8` | `0x808006800000` / `…900000` | SDMA `D2H` / `H2D` (die1) |

Entries [0..7] reconcile byte-exact to the per-TPB EVT_SEM bases; [16..19] to the SDMA
host-transfer engines; the die bit is **bit 47** (`0x800000000000`). v2/SUNDA has only
4 entries with older-fabric `0x0fff_xxxx` addressing
(`0x…fffc2700000`, `0x…fffc6700000`, `0x…fff0600000`, `0x…fff0d00000`). v3 == v4 over
`0x10..0xb0` (Python `==` True). **[HIGH × OBSERVED]**

### 2.5 The engine table + the logical-DMA → (engine, doorbell, completion) triple

A logical DMA op is resolved to an engine-id via static topology tables in `libnrt.so`,
then driven through its UDMA per-queue CSR block. The **engine table** (per-queue CSR
offsets, `queue_base = (queue_id+1) << 12`, verified via the per-gen getters):

| CSR | offset | getter (cayman) | role |
|-----|--------|-----------------|------|
| `base_ptr_lo` | `+0x1028` | `…get_m2s_queue_base_ptr_lo_offset_cayman@0x473ab0` | ring base PA [31:0] |
| `base_ptr_hi` | `+0x102c` | `…base_ptr_hi_offset_cayman` | ring base PA [63:32] |
| `len` | `+0x1030` | — | ring length (`desc_count` seeds) |
| `head_ptr` | `+0x1034` | — | head pointer (engine-advanced) |
| **`tail_ptr_inc`** | **`+0x1038`** | `…tail_ptr_inc_offset_cayman@0x473bf0` | **DOORBELL** — write N = arm N descs |
| `sw_ctrl` | `+0x10b0` | — | `rst_tail_ptr` re-arm |
| `data_tail_ptr_inc` | `+0x10e0` | `…data_tail_ptr_inc_offset_cayman@0x473c30` | independent data tail (cayman+) |

The **triple**, materialized as the device-barrier `ctx_log` fields:

| component | NCFW config field(s) | → hardware target |
|-----------|----------------------|-------------------|
| **named-engine** | `dma_engines_bitmap` (32-bit mask) + `queue_id` | engine-ids (§2.1) → UDMA queue 0..15 |
| **doorbell-reg** | `tdrbp_low`/`tdrbp_high` + `desc_count`; `m2s_tail_ptr`/`s2m_tail_ptr`; `dma_apb_bcast` | ring base → `+0x1028/2c/30`; tail-inc `+0x1038`; APB-broadcast fanout |
| **completion-event** | `dma_sync_sema` / `dma_sync_sema_value`; `barrier_sema[4]` / `target_sema_val[4]` | an EVT_SEM `SEMAPHORE_INC +0x1800` slot, polled WAIT-GE `+0x1000` |

So one logical DMA = `{engine-id-in-bitmap + queue_id, +0x1038 tail-pointer doorbell on its
ring, +0x1800 SEMAPHORE_INC completion}`. **[wiring HIGH × OBSERVED; the per-op runtime
integers (engine-id, queue, ring PA, sema index) are LX/DRAM-runtime-resident — MED]**

> **CORRECTION — vs the historical "engines A/B/C/D = EVENT-file offsets 0x28/0x38/0x68/0x78"
> reading.** Those four hex values are the **DRAM table-entry byte positions** within the
> §2.4 soc_addr array (entry 3 @ `+0x28`, entry 4 @ `+0x38`, …), not intra-EVENT-file
> sub-offsets and not four engine apertures. Entries [0..7] are the **eight per-TPB EVT_SEM
> unit bases** (4 TPBs/die × 2 dies), the die split is bit 47. There is no separate
> firmware "engine event 10/14/26/30" allocation — that was the same table-offset mis-read.
> **[CORRECTION HIGH × OBSERVED]**

---

## 3. arch_id / coretype reconciliation, the 4-image-vs-5-gen map, the per-gen diff

### 3.1 The arch_id selector — triple-anchored, do not invert

Both NCFW host selectors key on **`arch_id`** (the `coretype − 1` space `{5,12,20,28}`),
not on coretype and not on a private NCFW version number. `libncfw_get_image@0x1179`
re-disassembled (byte-exact):

```text
1199: cmp DWORD PTR [rbp-0x4],0x1c ; je 12a7  -> v4_plus  (dram 0x87ea0 / iram 0x8ccc0)
11a7: ja  12ec  (default return 2)            ; ANY arch_id > 0x1c -> default
11ad: cmp DWORD PTR [rbp-0x4],0x14 ; je 1262  -> v4       (dram 0x7e440 / iram 0x83260)
11c1: cmp DWORD PTR [rbp-0x4],0x05 ; je 11d2  -> v2       (dram 0x66a60 / iram 0x6a140)
11c7: cmp DWORD PTR [rbp-0x4],0x0c ; je 121a  -> v3       (dram 0x74a40 / iram 0x79860)
      else: jmp 12ec (default return 2)
```

`libncfw_ctx_log@0x1309` uses the same immediates `{0x1c,0x14,0x05,0x0c}` → the four
`<codename>_ncfw_ctx_log` calls, default `EINVAL` (0x16). The authoritative,
**triple-anchored binding** (`get_image` lea chain + the four `.c` strings + the per-codename
`ctx_log` symbols):

| arch_id | codename / NC-gen | coretype | image (IRAM @ / DRAM @) |
|---------|-------------------|----------|--------------------------|
| `0x05` | **SUNDA / NC-v2** | 6 | `0x6a140` / `0x66a60` |
| `0x0c` | **CAYMAN / NC-v3** | 13 | `0x79860` / `0x74a40` |
| `0x14` | **MARIANA / NC-v4** | 21 | `0x83260` / `0x7e440` |
| `0x1c` | **MARIANA_PLUS / NC-v4+** | 29 | `0x8ccc0` / `0x87ea0` |
| `0x24*` | **MAVERICK / NC-v5** | 37 | — **FILE-ABSENT** |

`arch_id = coretype − 1`, +8 generation stride. **[HIGH × OBSERVED for the four shipped
gens; MAVERICK `arch_id 0x24` INFERRED from `coretype−1` — there is no NCFW v5 image to
confirm it.]**

### 3.2 The central v5 wall — MAVERICK NCFW is file-absent

There is **no MAVERICK NCFW image of either kind**, and MAVERICK does **not** reuse v4+.
Four independent observations:

1. The `get_image` ladder compares only `{0x05,0x0c,0x14,0x1c}`; `cmp $0x1c; ja 12ec`
   sends any `arch_id > 0x1c` (incl. `0x24` = 36) straight to `return 2` — **no `cmp`
   against `0x24`, no leg aliasing MAVERICK onto v4+.**
2. The rodata blob region is **contiguous and closed**: `v4_plus_ncfw_iram_bin_size` ends
   at `0x918e4`, and the very next symbol (`nm -nS`) is `__GNU_EH_FRAME_HDR` — no room for
   a 5th image.
3. Zero `v5`/`maverick` symbol (`nm | rg -ci 'v5|maverick'` → 0).
4. Exactly **four** codename `.c` strings: `sunda.c`, `cayman.c`, `mariana.c`,
   `mariana_plus.c`.

So the NCFW image→gen mapping is a **total function on the four shipped gens, undefined
(`EINVAL`) for MAVERICK**. The 4-image-vs-5-gen gap is the MAVERICK omission, not a shared
image. **[HIGH × OBSERVED for the absence; matches the
[firmware image catalog](../../images/firmware-image-catalog.md) wall.]**

### 3.3 The image-sharing structure (the literal 4-vs-5 answer)

| gen | IRAM (sha16) | DRAM (sha16) | sharing |
|-----|--------------|--------------|---------|
| SUNDA v2 | `e379980b…` (43 KB) | `ca019511…` (`0x36c0`) | both unique |
| CAYMAN v3 | `d7bc8b81…` (19 KB) | `2418ab0f…` (`0x4e00`) | both unique |
| MARIANA v4 | `ed8eed34…` (19 KB) | **`1c3ac5f4…`** | IRAM unique; **DRAM shared w/ v4+** |
| MARIANA_PLUS v4+ | `abc4d452…` (19 KB) | **`1c3ac5f4…`** | IRAM unique; **DRAM shared w/ v4** |

All four IRAM SHAs are distinct; **v4 == v4+ DRAM byte-identical** (`1c3ac5f4`) is the only
image share. **[HIGH × OBSERVED — all 8 SHAs re-hashed from the carved blobs this pass.]**

### 3.4 The per-generation NCFW functional diff — two families, not five

**SUNDA (v2) — MONOLITHIC.** ~2.2× larger IRAM (43 KB), 23 functions, **no DRAM+0xB0
dispatch table** (the `+0xB0` region holds descriptor *data* — `{0,0,0,0, 0xffffff,0,
0x4000000,0x1000000, 0x100200,1,0,0}` — not IRAM code addresses); smaller DRAM (`0x36c0`),
**only 4 soc_addr CSRs** (vs 20), 11 descriptor records (vs 16); a more-inlined main loop
@`0x5f60`. Reduced collective: 50-event mesh tape; and (ISA-side) no SB2SB `0xBF` on-engine
collective. **[HIGH × OBSERVED]**

**CAYMAN (v3) + MARIANA (v4) + MARIANA_PLUS (v4+) — TABLE-BASED.** The DRAM+0xB0 12-entry
IRAM-code-address jump table; 108-event per-die mesh tape; 20 per-die soc_addr CSRs; 16
descriptor records. SB2SB `0xBF` + the pseudo-op family present. Within this family:

- **CAYMAN → MARIANA** is a pure **+0x18 code relocation**: the v3→v4 DRAM diff is
  **exactly 13 bytes, all in `0xB0..0x12c`** (the `+0xB0` table + one IRAM ptr); the
  soc_addr + descriptor tables are byte-identical. Main loop `0x3bb0 → 0x3bc8`. Every
  `+0xB0` entry shifts +0x18 (idx3 +0x2c, idx9 +0x14 outliers).
- **MARIANA → MARIANA_PLUS** is a **code-only IRAM recompile on a byte-identical DRAM**:
  first IRAM divergence at byte **`0x1190` (4496)**, **70.2% byte-identical** — a
  handler-rewrite of v4 with the same dispatch spine, (A) vector, `+0xB0` table, mesh
  count, and reset vector. v4+'s one genuine functional add (the DGE reshape fast-path)
  lives in the **NX sequencer + Q7 POOL** (EXTISA), **not** the NCFW management core. At
  the NCFW level v4→v4+ is a recompile. **[HIGH × OBSERVED]**

> **NOTE — orthogonal groupings.** The reset-`j` prologue groups `{v2,v3}` (j `0x1dc`) vs
> `{v4,v4+}` (j `0x1f8`); the *dispatch* architecture groups `{v2}` (monolithic) vs
> `{v3,v4,v4+}` (table). SUNDA is the prologue-twin of CAYMAN but the odd-one-out on
> dispatch.

### 3.5 The per-generation capability table

| axis | SUNDA v2 | CAYMAN v3 | MARIANA v4 | MARIANA_PLUS v4+ | MAVERICK v5 |
|------|----------|-----------|------------|------------------|-------------|
| arch_id (selector key) | `0x05` | `0x0c` | `0x14` | `0x1c` | `0x24*` INFERRED |
| coretype (=arch_id+1) | 6 | 13 | 21 | 29 | 37 INFERRED |
| NCFW IRAM size | `0xa8e0` (43K) | `0x4bc0` (19K) | `0x4c20` | `0x4c20` | **ABSENT** |
| NCFW IRAM sha16 | `e379980b` | `d7bc8b81` | `ed8eed34` | `abc4d452` | — |
| NCFW DRAM sha16 | `ca019511` | `2418ab0f` | `1c3ac5f4` === | `1c3ac5f4` (v4==v4+) | — |
| reset `j` target | `0x1dc` | `0x1dc` | `0x1f8` | `0x1f8` | — |
| dispatch arch | MONOLITHIC (no D+0xB0) | table @D+0xB0 (12) | table (+0x18 reloc) | table (== v4) | — |
| #functions | 23 | 57 | 61 | 55 | — |
| main-loop addr | `0x5f60` | `0x3bb0` | `0x3bc8` | `0x3bc8` | — |
| (A) cmd vector @D+0 | `{1bb3,1bcf,1be3,1bb3}` | `{1399,13b1,13c5,1399}` | `==v3` | `==v3` | — |
| soc_addr CSRs | 4 (`0x0fff…`) | 20 (per-die) | 20 (per-die) | 20 (per-die) | — |
| descriptor records (40B) | 11 | 16 | 16 | 16 | — |
| mesh tape (events ×80B) | 50 | 108 | 108 | 108 | — |
| SB2SB `0xBF` (ISA) | ABSENT | PRESENT | PRESENT | PRESENT | re-model (no NCFW) |
| DGE reshape fast-path | n/a | n/a | n/a | PRESENT (NX/Q7, not NCFW) | — |
| collective tier | REDUCED | FULL | FULL == CAYMAN | FULL == MARIANA | — |

The cc_op record schema, the `neff_configs`/barrier/`basic_block` sub-struct shapes, and
the EVT_SEM geometry are **schema-wide byte-identical across all four shipped gens** (one
all-gen `libnrt`); only data *sizes* scale (the 50-vs-108 mesh). **[HIGH × OBSERVED unless
tagged; the MAVERICK column is INFERRED/ABSENT throughout]** — consistent with the
[master capability matrix](../../generations/master-capability-matrix.md).

---

## 4. The NCFW collective-orchestration architecture, end-to-end

This ties the host command down to completion as one annotated flow, naming real symbols.
The full single-mechanism detail lives on the sibling pages
([`main-dispatch-loop`](main-dispatch-loop.md), [`ring-kangaring`](ring-kangaring.md),
[`mesh-collective`](mesh-collective.md), [`hierarchical-collective`](hierarchical-collective.md),
[`dma-reprogram-apb-bcast`](dma-reprogram-apb-bcast.md), [`neff-device-barrier`](neff-device-barrier.md),
[`neff-host-barrier`](neff-host-barrier.md), [`pring-descriptors`](pring-descriptors.md));
this section is the cross-cutting end-to-end synthesis.

### 4.1 The NCFW ↔ TOP_SP relationship

**TOP_SP** is the on-device collective **sequencer engine** (engine #5 per NeuronCore, an
embedded Xtensa-NX core): the hardware executor, kicked once by the host_trigger LOCAL_REG
write, that walks the cc_op program and issues per-step DMA-tail increments + EVT_SEM ops.
**NCFW** is the per-NeuronCore collective-firmware **management core** (the scalar-LX core
of §1) whose firmware *is* the per-TOP_SP collective program. The `libncfw` `ctx_log` dump
roots the entire NCFW runtime context at the JSON key **`ncfw_ctx_top_sp`** (present ×4) —
i.e. the NCFW context *is* the per-TOP_SP context. The relationship: **the TOP_SP NX core
is the per-NeuronCore executor of the NCFW firmware's collective program.** The rooting and
spad-load strings are OBSERVED; the byte-level "TOP_SP walks the LX/NX cc_op table" reading
crosses the un-disassemblable core, so it is **MED × INFERRED-STRONG**. See
[`top-sp-lowering`](../ops/top-sp-lowering.md) for the host-side lowering.

### 4.2 The end-to-end spine

```text
[host nrt_cc_*]  --neff_configs block (qTOPSPinit DMA) + host_trigger 0x615a0-->
[cc_op program]  --algo_type nibble per cc_op record-->
[LX core: (A)-vector fetch @D+0x00 -> DRAM+0xB0 dispatch -> run_state step]-->
[algorithm: ring / kangaring / mesh / hier  (co-resident, algo_type-selected)]-->
[DMA orch: ALLOC bitmap -> REPROGRAM ring CSRs -> APB-bcast 0x80000 aperture]-->
[pring tail doorbell +0x1038 -> SB2SB 0xBF: rdma_desc_gen(op8) + rdma_desc_start(op9)]-->
[TX/RX M2S/S2M tail; local_sem (src release) + peer recv_sema (dst ready)]-->
[EVT_SEM: WAIT-GE +0x1000 / INC +0x1800;  NEFF 4-step barrier brackets phase]-->
[host: ack EVT + COLLECTIVE_TOPSP_ACK sema + TOPSP_CC_STATUS NQ -> NRT_STATUS].
```

### 4.3 The dispatch core — the DRAM+0xB0 single-table reading

The on-core state machine is: park in `waiti 15` → wake on a notification → **FETCH** via
the DRAM+0x00 (A) command vector (3 trampolines + 1 default, 4th == 1st:
v3/v4/v4+ `{0x1399,0x13b1,0x13c5,0x1399}`, v2 `{0x1bb3,0x1bcf,0x1be3,0x1bb3}`) → **DISPATCH**
on the cc_op `algo_type` nibble through the DRAM+0xB0 jump table → **RUN** one step →
**SIGNAL** (EVT_SEM CSR write, `memw`-fenced) → DEFAULT/ERROR (idx 3 → `0x48xx`).

The dispatch instruction itself decodes cleanly (config-independent) at **v3 IRAM
`0x3bf8`**, re-verified this pass with the shipped objdump:

```text
3bf8:  24 b0 00   const16 a2, 0xB0       ; table base = DRAM+0xB0
3bfb:  20 23 a0   addx4   a2, a3, a2     ; a2 = 0xB0 + (algo_type a3) << 2  -- ONE ×4 index
3bfe:  58 02      l32i.n  a5, a2, 0      ; a5 = *(DRAM+0xB0 + algo_type*4)  -- the handler
```

The table is **one 12-entry, ×4-indexed `algo_type` jump table**, carved byte-exact:

```text
v3:  {3c20, 3c1d, 3c16, 48c4, 3c0f, 3c08, 3c08, 3c08, 3e84, 3e1e, 3e5e, 3e68}
v4:  v3 + 0x18  (idx3 +0x2c, idx9 +0x14 outliers)   v4+ == v4 (byte-identical)
```

idx 3 (`0x48c4`/`0x48f0`) is the error/default outlier → the `0x48e0` region. The index
`a3` is bounded **0..11** (a single `addx4` scale into a physically-contiguous 12-entry
table; idx 3 is the unknown-command guard). **[HIGH × OBSERVED]**

> **NOTE — the contested DRAM+0xB0 split (resolved here).** The committed
> [`main-dispatch-loop`](main-dispatch-loop.md), [`ncfw-iram-images`](ncfw-iram-images.md),
> [`ring-kangaring`](ring-kangaring.md), and [`mesh-collective`](mesh-collective.md) all
> read DRAM+0xB0 as a **single 12-entry `algo_type` table**. The committed
> [`ncfw-dram-ctx-log`](ncfw-dram-ctx-log.md) §2.3 instead splits it into an **8-slot
> table @+0xb0 plus a separate 4-entry secondary table @+0xd0**. This page adopts the
> **single-table reading**, and the deciding evidence is the dispatch instruction at v3
> `0x3bf8`: a single `const16 a2,0xB0` (the literal `24 b0 00` occurs **exactly once** in
> the v3 image — there is **no second `const16 0xD0`**), followed by **one** `addx4`
> (×4 scale) and **one** `l32i.n`. A genuine 8+4 split would require a second base literal
> `const16 0xD0` (`24 d0 00`, count **0** in the v3 image) and a second indexed load; the
> binary has neither. The `+0xd0` boundary is a layout coincidence — entries 8..11 of the
> *same* table land at `0xB0 + 8·4 = 0xD0`. **Reconciled:**
> [`ncfw-dram-ctx-log`](ncfw-dram-ctx-log.md) §2.3 now reads the single 12-entry table
> too (its earlier 8+4 split overturned in place against this dispatch read); the
> dispatch instruction is the decider.
> **[CORRECTION HIGH × OBSERVED — addx4/l32i dispatch read at v3 0x3bf8]**

The case *bodies* the handlers run are FLIX-undecodable (§1.2); the leg-cluster grouping
(`0x3c..` ring/mesh, `0x3e..` hier/barrier, `0x48xx` default) is OBSERVED from the table,
the per-step logic is MED.

### 4.4 The algorithm layer (co-resident, algo_type-selected)

The collective config is one struct with three adjacent co-resident sub-regions — RING @+0,
MESH @+0x1280 (50 SUNDA / 108 others × 80 B), HIERARCHICAL @+0x2220(SUNDA)/+0x3440 (an 8-byte
`__stub` handle). The runtime `algo_ctx` coexists: `ring_ctx@+0`, `hier_ctx@+0x200`
(`run_state` u8), `mesh_ctx@+0x204` (`event_index` u16). The `algo_type` nibble selects
which is live; the cc_op packer is `create_spad_ctrl_entry@0x232cd0` =
`(trigger_next<<15) | (sub_type<<12) | (algo_type<<8) | 1` (the `mov eax,0x1; shl eax,0x8`
and `mov edi,0x3; shl edi,0xc` terms are byte-visible in the disassembly). The
`ncfw_log_spad_ctrl_cc_op_entry` decoder extracts `algo_type` via `and 0xf @0x1a43`,
`algo_sub_type` via `shr 4; and 7 @0x1c9a`, `trigger_next` via `shr 7 @0x1efa`. The
`reduction_type_t` (the 3rd arg of `recv_reduce_*`) names the ring read/write *pattern*:
`{RING_2R1W=0, RING_2R2W=1, KANGARING_NR1W=2}`; `enc_alg_type` runs `RING0..BW_OPT_MESH10/
INVALID11`. **[HIGH × OBSERVED for the packer/extraction/enums; the `algo_type ≡
enc_alg_type` numeric identity is MED — no byte-level host→device write traced]** — full
algorithm detail on [`ring-kangaring`](ring-kangaring.md), [`mesh-collective`](mesh-collective.md),
[`hierarchical-collective`](hierarchical-collective.md), and
[`collective-enums`](../ops/collective-enums.md).

The algorithm shapes, condensed:
- **RING all-reduce** = reduce-scatter (N−1 `recv_reduce_*` CCE-fold steps) + all-gather
  (N−1 copy steps), flow-controlled by `recv_cnt` + `send_credit` (ring_ctx).
- **KANGARING** = NR1W fanout: a SEND signals up to 3 `kring_peers[]` and waits its own
  `mine` sema (`reduction_type_t = KANGARING_NR1W`).
- **MESH all-reduce** = a flat event tape (each event: wait `event_wait_sema ≥ wait_val` →
  fire ≤2 `dma_apb_bcast` → INC ≤3 `direct_trigger_sema` at neighbour dies, routed by the
  `CAYMAN_ID`/`DIE` soc_addr bits).
- **HIERARCHICAL** = intra-RS ring → inter-AR mesh/ring → intra-AG ring, sequenced by the
  single `hier run_state` byte, reusing the co-resident ring+mesh descriptors. The reduce
  ALU op rides the SDMA CCE descriptor (`SDMA_CCETYPE {ADD0,FMA1,MAX2,MIN3,EXT4,GCE5}`),
  not the cc_op word.

### 4.5 The DMA orchestration (per step)

Each step is a four-stage DMA orchestration:

1. **ALLOC** — build the runtime `dma_engines_bitmap` from the static
   `cayman_dma_alloc_bitmap@0x9d20a0` (8 u64; low32 = engine apertures `{0x0b,0x0c,0x0d,0x0f}`,
   high32 = queue mask `0xffff` die0 / `0xffff0000` die1), `|=`-ing in-use engine bits,
   validity-gated by `valid_dma_engines_bitmap`.
2. **REPROGRAM** — re-arm the standing pring in place via six per-queue ring-CSR
   descriptors (`base_ptr_lo +0x1028`, `_hi +0x102c`, `len +0x1030`, plus the `sw_ctrl`
   re-arm), per (engine, queue). Base/len cannot be broadcast (distinct per engine).
3. **BCAST-TRIGGER** — **one** APB-broadcast write to the BCAST_UDMA aperture
   `cayman_bcast_region_table[engine&7]@0x9def40` (each = SDMA channel base + `0x80000`;
   verified `[0]=0x1002080000`, `[0]−0x80000 = 0x1002000000` = SDMA base), value
   `(dma_engines_bitmap << 16) | 0x100` — the fabric fans the doorbell to the masked group.
4. **COMPLETE** — REMOTE: the SEND's INC_SEMA descriptor bumps the receiver's `recv_sema`
   (`SEMAPHORE_INC +0x1800`), polled WAIT-GE (`+0x1000`) + DEC; LOCAL: the
   `DMA_COMPLETION_MARKER 0xabcdef01` busy-poll (no interrupt). See
   [`dma-reprogram-apb-bcast`](dma-reprogram-apb-bcast.md) and
   [`cust3-doorbell-thunks`](cust3-doorbell-thunks.md). **[HIGH × OBSERVED + the four spot
   checks above]**

### 4.6 The sync model + completion

The per-step **peer handshake** sequences steps within a phase (the 2-semaphore
producer/consumer pair + a credit: SEND → INC peer `recv_sema`; RECV → INC `send_sema` =
return credit). The phase **barrier** brackets each ring/mesh phase: the NEFF 4-step counted
device barrier (`barrier_step_sizes {1,4,4,1}` default / `{1,2,2,1}` switch-family; the "4"
= `NEFF_DEVICE_BARRIER_MAX_LEADERS`), with the host barrier merged in host-CC mode (device
done sema == host barrier sema, id 6) and the TOP_SP `timestamp_inc` tick (tsync) keeping
the dies aligned. See [`neff-device-barrier`](neff-device-barrier.md),
[`neff-host-barrier`](neff-host-barrier.md), [`spad-ccop-tsync`](spad-ccop-tsync.md).

The data plane the NCFW core *drives but does not execute*: each ring step = one SB2SB
collective leg (opcode `0xBF`, `aws_neuron_isa_tpb_s3d3_collective.h`, on the POOL/Q7 iDMA)
= `rdma_desc_gen` (op 8: build the SDMA CME BD ring + local/remote sema descriptors) +
`rdma_desc_start` (op 9: M2S/S2M tail-pointer doorbell with left_pop/right_push), moving
SBUF → remote-SBUF and folding on reducing legs via the SDMA CCE descriptor. Completion
surfaces to the host as the per-TOPSP ack EVT, the `COLLECTIVE_TOPSP_ACK` sema, and the
`NOTIFICATION_TYPE_TOPSP_CC_STATUS` NQ (type 9) → NRT_STATUS — confirmed by the
"`[nec_dev %u] TOP_SP #%d started`" and "`total cc op report cnt`" strings. See
[`ops/architecture-synthesis`](../ops/architecture-synthesis.md) for the data-plane detail.
**[HIGH × OBSERVED for the symbols/strings; the per-leg on-core schedule is MED — §1.2 boundary]**

### 4.7 The collective lifecycle (mapped through the stack)

The host state machine (`nrt_cc_*` symbols re-verified): **CREATE**
(`nrt_cc_global_comm_init@0x7fd90` builds the topology) → **PREPARE**
(`nrt_cc_prepare@0x7f610`: `__select_algorithms` assigns one `enc_alg_type` per leg,
`encd_set_exec_prings@0x23f9b0` binds the persistent prings, the cc_op program is *built*
here via `create_spad_ctrl_entry`) → **EXEC/SCHEDULE** (`nrt_cc_schedule@0x7fc60`: CONFIGURE
`encd_ncfw_configure_device_init@0x230c70` + START `encd_start_executable@0x2431c0` +
`set_host_trigger 0x615a0`; the LX core then runs §4.3–§4.6 with the barrier bracketing each
phase) → **DESTROY** (`encd_free_exec_prings`, `set_stop_signal +0x15c0`). The five control
doorbells: init `+0x1540`, tsync `+0x1560`, host_trigger `+0x15a0` (= abs `0x615a0`; sunda
`0x60848`), stop `+0x15c0`, bbswitch `0x615e0` — `mov eax,0x615a0 @0x477990`,
`mov eax,0x615e0 @0x4779b0`, `mov eax,0x60848 @0x479120` all byte-verified. See
[`ring-protocol-config-command`](ring-protocol-config-command.md). **[HIGH × OBSERVED host;
MED device]**

---

## 5. The five byte-verified strongest claims

| # | claim | byte-anchor |
|---|-------|-------------|
| 1 | **NCFW is scalar-LX; "FLIX" is a mis-decode** | native `xtensa-elf-objdump` (ncore2gp) emits an impossible 5-slot 512-bit Vision bundle at v3 IRAM `0x3bd9` inside the dispatch prologue; `WindowOverflow8` handler byte-identical @ IRAM `0x24` ×4 gens; `waiti 15` @ v3 `0x4b6c` |
| 2 | **arch_id map + v5 file-absent** | `libncfw_get_image@0x1179` ladder `cmp {0x1c,0x14,0x05,0x0c}`, `ja` default for `>0x1c`; rodata closed @ `0x918e4` (`__GNU_EH_FRAME_HDR`); 0 maverick/v5 symbols; 4 codename `.c` strings |
| 3 | **DRAM+0xB0 = ONE 12-entry table** | `const16 a2,0xB0; addx4 a2,a3,a2; l32i.n a5,a2,0` @ v3 IRAM `0x3bf8`; `24 b0 00` occurs **exactly once** in v3; table `{3c20…3e68}`, idx3=`0x48c4` outlier, v4=v3+0x18 |
| 4 | **DMA naming triple** | sema getters READ `+0x1000`/SET `+0x1400`/INC `+0x1800`/DEC `+0x1c00`; `tail_ptr_inc +0x1038@0x473bf0`; `bcast_region_table[0]=0x1002080000` (base+`0x80000`); `libncfw` SDMA/DDMA/CDMA/UDMA = 0 |
| 5 | **4-image-vs-5-gen map** | 8 blob symbols (`nm -nS`); v4==v4+ DRAM sha `1c3ac5f4`; v4 vs v4+ IRAM diverge @ `0x1190`, 70.2% identical; soc_addr v3==v4 |

---

## 6. Open items (the standing boundaries)

- **The NCFW LX case-body decode (the central gap).** No NCFW LX-core config ships (only
  `ncore2gp`/Vision-Q7); the dispatch spine is HIGH/OBSERVED, the per-algorithm case bodies
  and the exact on-core step schedule are NOT instruction-decodable (the "which peers in
  which step", the per-(world-size,topology) leg schedule, the kangaring hop schedule). **MED/LOW.**
- **`algo_type ≡ enc_alg_type` numeric identity** is MED — the 4-bit nibble + shared
  vocabulary converge, but no byte-level host→device write was traced (it crosses the LX core).
- **The concrete runtime integers** (per-step `dma_engines_bitmap`, ring base/size HBM
  addresses, queue ids, EVT_SEM indices, sema target values) are runtime-populated in the LX
  firmware DRAM working area (the ~99%-zero `.bss` tail), not in any shipped static file. **LOW.**
- **The CUST3 TIE doorbell op-word bit semantics** and the `al_udma_desc`/SDMA-CCE on-wire
  byte layout are `.tie`/HW-register territory; the op word (`0x0d0ca0`) and target aperture
  are proven, the internal bit-fields are not. **LOW.**
- **MAVERICK v5 source-absence (the central v5 wall).** No NCFW management-core image; its
  collective sync/d2d re-model is an ISA/EXTISA/SoC fact with no NCFW artifact to carry it.
  The NCFW orchestration architecture above is **defined only for
  SUNDA/CAYMAN/MARIANA/MARIANA_PLUS**; MAVERICK `arch_id 0x24*` is INFERRED. **[HIGH absence
  / INFERRED arch_id]**
