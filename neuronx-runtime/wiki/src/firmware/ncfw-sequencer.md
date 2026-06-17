# The NCFW Sequencer (Xtensa LX Disassembly)

> *All blob offsets, byte values, and decoded instructions on this page apply to the eight Xtensa firmware images embedded in `libncfw.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (host carrier md5 `e01ea384a76e59d511b4f005b7db98ac`, SONAME `libncfw.so.2.31.1.0.cf13a49f`, build-id `a98f8e1c…`; `.rodata` VMA == file offset per `readelf -S`, so the carve offsets below are host file offsets and the in-blob `+0xNNN` are image-relative). The deep focus is the **v4 / "mariana" IRAM** image (`sha256[0:16] = ed8eed34…`, 19,488 B). Per-arch deltas to v2/v3/v4_plus are called out in place. Other versions will differ.*
>
> *Evidence grade: **Confirmed (byte-anchored)** for the reset/boot/idle scaffold — every J target re-derived by independent off18 math, every idiom count re-scanned across all four IRAM blobs, every dispatch word read straight from the DRAM blob. The proprietary AWS sequencer **TIE is opaque** (no `.tie` shipped for this config); TIE-bearing bodies are marked. · Part X — Collectives Firmware (libncfw) · [back to index](../index.md)*

## Abstract

The NCFW ("Neuron Collective FirmWare") **sync core** is the on-device program that drives collective-communication primitives — ring/mesh/barrier DMA choreography — from inside each NeuronCore's TPB sequencer block. It runs on a **Tensilica Xtensa LX core** (XEA2, little-endian, windowed register file, Code-Density narrows) carrying a proprietary AWS sequencer TIE. `libncfw.so` is a pure host-side *provider*: `libncfw_get_image()` hands raw bytes — **no ELF header, no magic, no checksum** — to libnrt, which DMAs the IRAM half into TPB IRAM and the DRAM half into HBM, then releases the core from reset. Each of the four arch generations ships as a `{IRAM code, DRAM data}` pair, for **eight blobs** total. This is the *sequencer* Xtensa config, **not** the GPSIMD/Vision-Q7 vector core: same ISA family, different TIE, and unlike Q7 its `.tie` is not shipped — see [Tensilica Xtensa and Vision-Q7 Identification](../gpsimd/xtensa-vision-q7.md), which owns the lineage proof.

The control-flow model is best understood by analogy to any small RTOS-less bare-metal image: a packed exception/vector table at byte 0, a linear reset→boot→steady-state path, and an interrupt-driven idle loop. The Xtensa specifics are three. First, byte 0 is a **packed XEA2 vector table** whose reset slot is a 3-byte `j` whose target is computed by the off18 rule `PC + 4 + sign_ext18(imm)` — and that single target is the cleanest per-arch fingerprint (v2/v3 → `0x1dc`, v4/v4+ → `0x1f8`, a `+0x1c` shift that is exactly the size of the D-cache invalidate loop the v4 silicon adds). Second, the steady state is **not a polling loop** but an idle+exception dispatcher: the core sits in `waiti 15`, a Level-1 doorbell IRQ wakes it, and a four-entry DRAM table dispatches by `EXCCAUSE` with a hot fast-path for cause 4. Third, the entire host interface is a **single `simcall`** per image — provably one, by byte scan across all four IRAMs.

This page documents four artifacts a reimplementer must reproduce: (1) the **reset vector and its off18 arithmetic**, including the v4 `+0x1c` D-cache-loop shift; (2) the **boot sequence** — I/D-cache invalidate → `wsr.windowbase`/`rasid` → MMU/TLB program → `call0` stage 2 → the PRID core gate that splits core0 (main idle) from slaves (work-distribution loop); (3) the **idle dispatcher** — the `entry/extw/simcall/break/waiti/j` loop and the `EXCCAUSE==4` doorbell fast-path; and (4) the **L32E/S32E window spill/fill** vectors and the **per-arch handler-region delta** (v4 vs v4_plus differ *only* inside the CC-op handler bodies).

For reimplementation, the contract is:

- **The packed XEA2 vector table at byte 0** and the off18 target math, so the reset/window/exception entry points decode for any arch.
- **The boot path** as a linear sequence of recognizable Xtensa idioms (cache loops, `wsr.windowbase`, `wsr.rasid`, `call0` stage 2) plus the **PRID core gate** (`rsr SR 0xEB; beqz`) that forks primary vs slave.
- **The idle dispatcher** — the exact instruction sequence, the sole `simcall(a2=1)` host call, the `break 1,15` heartbeat / `break 1,1` trap, and the `beqi a3,4` doorbell fast-path through the **4-entry DRAM exception table**.
- **The per-arch delta model** — the `+0x1c` reset shift and added `dii` loop on v4/v4+, the `call8 dcache-wb` the v4 idle adds, and the fact that **v4 and v4_plus share byte-identical boot+idle** and diverge only in the handler region (`0x1190..0x4ab0`).

| | |
|---|---|
| **Core** | Tensilica **Xtensa LX**, XEA2, little-endian, windowed RF, Code-Density; **AWS sequencer TIE (opaque)** |
| **Image format** | Raw bytes — **no ELF, no magic, no checksum**; IRAM byte 0 = packed XEA2 vector table |
| **Blob count** | **8** = `{IRAM, DRAM}` × `{v2, v3, v4, v4_plus}` |
| **Focus image** | `v4_ncfw_iram_bin` — host file offset `0x83260`, 19,488 B (`0x4c20`), `sha256[0:16] = ed8eed34…` |
| **Reset vector** | `+0x0` `06 7d 00` → `j 0x1f8` (v4); v2/v3 → `0x1dc`; the `+0x1c` shift = the added `dii` D-cache loop |
| **Boot stage 2** | `+0x1c6` `05 e5 00` → `call0 0x1018` |
| **Core gate** | `+0x1118` `50 eb 03` `rsr SR 0xEB`; `beqz` → primary `call8 0x4b84` / slave `call8 0x4ad4` |
| **Idle loop** | `+0x4b84` `entry/extw/mov-shift/movi.n a2,1/simcall/break 1,15/waiti 15/j` (v4) |
| **Host call** | exactly **one** `simcall` (a2=1) per image — verified count `1/1/1/1` across the four IRAMs |
| **Exception table** | DRAM `u32[4]` = `{0x1399, 0x13b1, 0x13c5, 0x1399}` (v3/v4; slot0==slot3 catchall) |
| **Carve + decode** | `objdump`/`xxd`/`nm`/`readelf` + hand Xtensa-LX XEA2-LE decoder; TIE sites left symbolic |

---

## 1. Blob Map and Identity

### Purpose

Before any disassembly, a reimplementer needs to know which bytes are code, which are data, and how to tell the four arch generations apart. The eight blobs are nm `'r'` (read-only) symbols in `libncfw.so`'s `.rodata`; each is carved by its `*_bin` symbol and sized by the adjacent `*_bin_size` u64 word. There is no container — `libncfw_get_image()` returns a `{ptr, len}` into `.rodata` verbatim.

### The eight blobs

Carved this pass (`.rodata @0x65000`, VMA == file offset); sizes from the `*_bin_size` words; `sha256[0:16]` is the first 16 hex of the image digest.

| Blob (nm `'r'` sym) | Host off | Size (B) | `sha256[0:16]` | Role / entropy |
|---|---|---|---|---|
| `v2_ncfw_dram_bin` | `0x66a60` | 14016 | `ca01951124e505b6` | DRAM data (v2/sunda) — H≈0.13 |
| `v2_ncfw_iram_bin` | `0x6a140` | 43232 | `e379980b7ec3f2fe` | IRAM code (v2) — H≈6.81 (largest) |
| `v3_ncfw_dram_bin` | `0x74a40` | 19968 | `2418ab0f6350ce93` | DRAM data (v3/cayman) |
| `v3_ncfw_iram_bin` | `0x79860` | 19392 | `d7bc8b814b03c1f0` | IRAM code (v3) |
| `v4_ncfw_dram_bin` | `0x7e440` | 19968 | `1c3ac5f445865844` | DRAM data (v4/mariana) |
| `v4_ncfw_iram_bin` | `0x83260` | 19488 | `ed8eed3429da3834` | IRAM code (v4) — H≈6.45 **\*FOCUS\*** |
| `v4_plus_ncfw_dram_bin` | `0x87ea0` | 19968 | `1c3ac5f445865844` | DRAM data == v4 (byte-identical) |
| `v4_plus_ncfw_iram_bin` | `0x8ccc0` | 19488 | `abc4d4521dd857ab` | IRAM code (v4_plus) |

The arch dimension is read out of the provider switch (`libncfw_get_image` / `libncfw_ctx_log` keys `5/12/20/28`); the silicon codenames come from the `ctx_log` symbol strings:

| arch key | name | codename (INFERRED) | IRAM `sha256[0:16]` |
|---|---|---|---|
| 5 | v2 | sunda | `e379980b…` |
| 12 | v3 | cayman | `d7bc8b81…` |
| 20 | v4 | mariana | `ed8eed34…` |
| 28 | v4_plus | mariana_plus | `abc4d452…` |

> **NOTE —** the DRAM images are **sparse tables**, not code: ~1.05% nonzero for v2 (147 / 14016 bytes) and ~1.22% for v3/v4 (243 / 19968), with all payload in the first `~0x240` (v2) / `~0x3f4` (v3+) bytes and the remainder zero (H≈0.13–0.15 b/B vs IRAM's H≈6.4–6.8). The DRAM payload is the **exception-dispatch table** (§5) plus a SoC 40-bit physical-address grid; the IRAM is the executable image. `v4_dram == v4_plus_dram` byte-for-byte (`sha 1c3ac5f4`); `v4_iram != v4_plus_iram`. CONFIDENCE: **HIGH** (re-verified: sizes, sha256, entropy, and the `cmp`-proven dram identity all reproduce this pass).

> **GOTCHA —** the `v4_plus_iram_bin_size` u64 word's high four bytes overlap the SONAME tail in `.rodata`. Read the **low u32 only**: true length is `0x4c20 = 19488`, identical to v4. A reimplementer that reads the full u64 size word gets garbage in the high half. CONFIDENCE: **HIGH**.

### Considerations

The carve is the whole container model — there is no header to parse, no relocation to apply, no checksum to validate. The host side simply `memcpy`/DMAs `*_bin` of length `*_bin_size` into device IRAM (and the DRAM half into HBM) and releases reset. This is why the blobs are self-contained: IRAM byte 0 must be a valid packed XEA2 vector table because the silicon begins fetching there the instant reset deasserts. The consumer side (libnrt `encd_libncfw_init` / `encd_ncfw_init`, DRAM template patch, DMA-to-TPB-IRAM, reset-release) is owned by [Firmware Upload Path](upload-path.md) and not re-derived here.

---

## 2. The Reset Vector and Vector Table

### Purpose

IRAM byte 0 is a packed XEA2 vector table: a sequence of short branch slots and inline window/exception bodies the hardware indexes by exception kind. The reset slot is the only one a reimplementer must decode to bootstrap; the window and user-exception slots are decoded here because they prove the windowed-ABI model the rest of the image assumes.

### The off18 reset math

Xtensa `J` is a 3-byte (24-bit) instruction: `op0 = word & 0xf == 6`, the signed 18-bit offset is `imm = (word >> 6) & 0x3ffff` (sign-extended), and the target is `PC + 4 + imm`. Decoding slot A (`+0x0`) and slot B (`+0x6`) for all four arches reproduces the documented targets exactly:

```text
arch   raw @+0x0     slot A → target   slot B (@+0x6) → target
v2     06 76 00      j 0x1dc           j 0x1e8
v3     06 76 00      j 0x1dc           j 0x1e8        (byte-identical reset to v2)
v4     06 7d 00      j 0x1f8           j 0x204
v4+    06 7d 00      j 0x1f8           j 0x204        (byte-identical reset to v4)
```

> **QUIRK —** the entire per-arch reset delta is the **`+0x1c` shift**: `0x1f8 − 0x1dc = 0x1c`. That shift is not arbitrary — it is exactly the byte length of the **D-cache invalidate loop** (`dii … ; addmi ; bltu`) that the v4/v4_plus boot path inserts and the v2/v3 path omits (§3). The reset target moves forward by precisely the bytes the new loop occupies. A reimplementer porting the v2 image to v4 silicon must both *add* the `dii` loop and *re-resolve every absolute J target after it* by `+0x1c`. CONFIDENCE: **HIGH** (off18 math re-derived independently from raw bytes for all four images).

> **CORRECTION (W2-XT-NCFW) —** an early seed framing summarized the v2/v3 reset as `j 0x1dc / 0x1e8`, which reads as "v2=0x1dc, v3=0x1e8". The bytes show **both v2 and v3 use slot A → `0x1dc` and slot B → `0x1e8`** — the two arches have byte-identical reset vectors (`06 76 00` at `+0x0` for both). `0x1e8` is slot **B** of the same image, not v3's slot A. The arch split is v2/v3 (`0x1dc`) vs v4/v4+ (`0x1f8`).

### The packed table (v4, annotated)

```asm
; v4_ncfw_iram_bin — packed XEA2 vector table @+0x0  (LE 24-bit / 16-bit narrow; HIGH)
+0x0000  06 7d 00   j      0x1f8        ; reset / WindowOverflow slot A  (off18: 0+4+0x1f4)
+0x0006  86 7e 00   j      0x204        ; vector slot B
+0x000c  a0 71 69   <TIE-win>           ; WindowOverflow12 reg-window save/restore TIE block
+0x000f  80 2a 29   <TIE-win>           ;   (op0=0/op1=9/op2={6,2}; AWS sequencer window TIE; 5 insns)
+0x0012  a0 3a 29   <TIE-win>           ;   repeats verbatim @+0x5d
+0x0015  90 e1 69   <TIE-win>
+0x0018  80 08 69   <TIE-win>
+0x001b  46 6c 00   j      0x1d0        ; WindowUnderflow escape

; --- WindowUnderflow12 fill block: l32e at,as,imm  (imm = (regno-16)*4) ---
+0x0024  80 01 09   l32e   a8, a1,-64
+0x0027  90 01 09   l32e   a9, a1,-64
+0x002a  a0 11 09   l32e   a10,a1,-60
+0x002d  b0 41 09   l32e   a11,a1,-48
+0x0030  c0 51 09   l32e   a12,a1,-44
+0x0033  d0 61 09   l32e   a13,a1,-40
+0x0036  e0 71 09   l32e   a14,a1,-36
+0x0039  f0 01 09   l32e   a15,a1,-64

; --- WindowOverflow12 spill block: s32e at,as,imm ---
+0x003c  80 31 49   s32e   a8, a1,-52
+0x003f  90 91 49   s32e   a9, a1,-28
+0x0042  ...        s32e   a10..a15 (+ extra a12/a13/a14 through +0x5a)
+0x005d  a0 71 69   <TIE-win>           ; 2nd window-save TIE block (identical to @+0x0c)

; --- UserException / KernelException prologue, fixed @+0x6c ---
+0x006c  20 ee 03   rsr.excvaddr a2     ; SR 0xEE — read faulting address
+0x006f  29 71      s32i.n a2,a1,28     ; save excvaddr to frame
+0x0071  39 61      s32i.n a3,a1,24
+0x0073  46 55 00   j      0x1cc        ; -> exception body (joins vector tail @0x208)
```

> **NOTE —** the `l32e`/`s32e` immediate decode is `imm = (regno − 16) × 4` and reproduces every operand above. This proves the image uses the **standard Xtensa windowed ABI** — `a0`=return, `a1`=SP, `a2..a7` in/out window, `a8..a15` window-managed — which the idle loop and handlers rely on. The window-*save* itself is realized by the opaque TIE block at `+0x0c`/`+0x5d`, not by base-ISA `s32e`. CONFIDENCE: **HIGH** (spill/fill); **LOW** (the TIE save block — see §7).

---

## 3. The Boot Sequence

### Purpose

Between reset and steady state the core runs a single linear path: invalidate caches, initialize the windowed ABI, program the MMU/TLB, enter a second boot stage, then gate on core ID. Every step is a recognizable Xtensa idiom, which is what makes the boot path decodable even though the bodies it calls into are TIE-dense.

### The boot flow

```text
reset @0x0  ── j 0x1f8 ──►  reset-vector body @0x1f8  (AWS sequencer reset TIE, opaque)
   │
   ▼
I-cache invalidate loop @0xb8   ── iii a3,{0,64,128,192} ; addmi 256 ; bltu a3,a2,0xb8
   │
   ▼  isync @0xca
D-cache invalidate loop @0xd5   ── dii a3,{0,64,128,192} ; addmi 256 ; bltu a3,a2,0xd5
   │                                  ╰── v4 / v4_plus ONLY  (the +0x1c bytes)
   ▼
windowed-ABI init               ── wsr.<0x61> ; movi.n a2,1 ; wsr.windowbase a2 ; rsync
   │
   ▼
MMU / TLB program @0x119..0x1a8  ── descriptor walk: l32i.n pairs, xor/or hash, addx8 index,
   │                                  wdtlb/witlb via TIE  ; wsr.rasid a9 (SR 0x5A)
   ▼
call0 0x1018  ═══►  BOOT STAGE 2  (sequencer state init: PRID/CSR/semaphore/DMA — TIE-dense)
   │
   ▼
PRID CORE GATE @0x1118          ── rsr SR 0xEB a5 ; beqz a5,slave
   │                                  ├── primary  ─ call8 0x4b84 ─►  MAIN IDLE  (§4)
   │                                  └── slave    ─ call8 0x4ad4 ─►  SLAVE LOOP (§4)
```

### Cache invalidate — the `+0x1c` source

The two cache loops are the clearest target-delta in the image: v4/v4+ add the D-cache (`dii`) loop, and that loop *is* the `+0x1c` reset shift.

```asm
; v4_ncfw_iram_bin — early-boot cache invalidate  (HIGH)
+0x00b5  20 0c 03   rsr.<0x0c> a2       ; load cache-region limit (SR 0x0C config CSR) into a2
+0x00b8  f2 73 00   iii   a3, 0         ; I-cache invalidate: iii (op0=2,r=7,t=15), 4 lines
+0x00bb  f2 73 10   iii   a3, 64
+0x00be  f2 73 20   iii   a3, 128
+0x00c1  f2 73 30   iii   a3, 192
+0x00c4  32 d3 01   addmi a3, a3, 256   ; advance one 256B group
+0x00c7  27 33 ed   bltu  a3, a2, 0xb8  ; loop while a3 < region-limit
+0x00ca  00 20 00   isync               ; I-cache sync barrier
;            ── the following block is present on v4/v4_plus ONLY (the +0x1c bytes) ──
+0x00d5  72 73 00   dii   a3, 0         ; D-cache invalidate: dii (op0=2,r=7,t=7), 4 lines
+0x00d8  72 73 10   dii   a3, 64
+0x00db  72 73 20   dii   a3, 128
+0x00de  72 73 30   dii   a3, 192
+0x00e1  32 d3 01   addmi a3, a3, 256
+0x00e4  27 33 ed   bltu  a3, a2, 0xd5
;            ──────────────────────────────────────────────────────────────────────
+0x00ed  20 61 13   wsr.<0x61> a2       ; cache/region config CSR (SR 0x61; TIE-config, MED)
+0x00f0  0c 12      movi.n a2, 1
+0x00f5  20 48 13   wsr.windowbase a2   ; SR 0x48 — windowed-ABI WindowBase init
+0x00f8  10 20 00   rsync               ; register sync after windowbase
```

### MMU/TLB program and stage 2

```asm
; v4_ncfw_iram_bin — MMU/TLB + boot-stage-2 entry  (HIGH for structure; per-op TIE MED)
+0x0116  90 5a 13   wsr.rasid a9        ; SR 0x5A — Ring Address-Space ID for TLB
+0x0119..0x01a8     ; TLB-way program loop: l32i.n descriptor pairs (a12/a13 @0/4),
                    ; xor/or hashing, addx8 indexing, wdtlb/witlb realized via TIE
                    ; (op0=4/10/11 opaque). Walk base in a2, count in a3.
+0x01ad  20 61 13   wsr.<0x61> a2
+0x01c3  10 20 00   rsync
+0x01c6  05 e5 00   call0 0x1018        ; ═══► BOOT STAGE 2 ENTRY
+0x01cc  06 0e 00   j     0x208         ; (exception-body path joins the vector tail)
+0x01f8  04 00 00   <TIE>               ; reset-vector body = AWS sequencer reset TIE (opaque)

; BOOT STAGE 2 @0x1018 (HIGH for scaffold; body is TIE-dense)
+0x1018  02 a0 00   movi  a0, 0
+0x101b..           ; op0=4 <TIE> sequence: PRID / CSR / semaphore / DMA setup
+0x1048  30 e6 13   wsr.excvaddr a3     ; (a3=128) stage CSR
+0x104b  10 20 00   rsync
```

### The PRID core gate

The two-way fork that splits the primary core (which runs the main idle dispatcher) from the slave cores (which run the work-distribution loop):

```asm
; v4_ncfw_iram_bin — PRID core gate @0x1103..0x1124  (HIGH)
+0x1103  01 a8 0a   l32r  a0, <lit 0xfffc3ba4>   ; mask-ROM/region literal (negative L32R)
+0x110c  e5 a0 02   call8 0x3b1c                  ; per-core init helper
+0x1115  16 b4 00   beqz  a4, 0x1124
+0x1118  50 eb 03   rsr.<0xEB> a5                 ; ═══ READ CORE-ID / gate CSR (SR 0xEB) ═══
+0x111e  16 25 00   beqz  a5, 0x1124              ; if a5==0 -> take slave path
+0x1121  25 a6 03   call8 0x4b84                  ; ═══► MAIN IDLE (primary core)
+0x1124  e5 9a 03   call8 0x4ad4                  ; ═══► SLAVE work-distribution loop
```

> **QUIRK —** the gate reads a **custom CSR `0xEB`**, not the architectural `PRID` register, to decide primary-vs-slave. The two-way fork is byte-proven (`beqz` to the slave `call8`, fallthrough to the primary `call8`), but which `a5` value means "primary" is tied to the opaque `SR 0xEB` semantics. A reimplementer should treat `0xEB` as the core-ID/gate register and verify polarity against silicon, not assume `a5 != 0 ⇒ primary`. CONFIDENCE: **HIGH** (gate structure); **MED** (polarity).

### Function Map — boot

| Region | Role | Confidence |
|---|---|---|
| `+0x0` `j 0x1f8` | Reset vector (off18) | HIGH |
| `+0x1f8` `<TIE>` | Reset-vector body (sequencer reset/window-init) | LOW (opaque TIE) |
| `+0xb8` `iii`-loop | I-cache invalidate | HIGH |
| `+0xd5` `dii`-loop | D-cache invalidate (v4/v4+ only; the `+0x1c` bytes) | HIGH |
| `+0xf5` `wsr.windowbase` | Windowed-ABI init | HIGH |
| `+0x116` `wsr.rasid` | MMU ring-ASID set | HIGH |
| `+0x119..0x1a8` | TLB-way descriptor program loop | HIGH (shape) / MED (per-op TIE) |
| `+0x1c6` `call0 0x1018` | Boot stage 2 entry | HIGH |
| `+0x1018` | Boot stage 2 (sequencer state init) | HIGH (scaffold) / LOW (TIE body) |
| `+0x1118` `rsr SR 0xEB` | PRID core gate | HIGH (structure) / MED (polarity) |

---

## 4. The Idle Dispatcher and Slave Loop

### Purpose

The firmware's steady state is the main idle loop. It is the page's most-cited construct because it is fully base-ISA-decodable and it contains the *entire* host interface: one `simcall`, one heartbeat `break`, one `waiti`. A reimplementer who reproduces this loop has reproduced the sync core's observable behavior; the work itself happens in the TIE-dense exception handlers it dispatches to.

### The idle loop (v4)

```asm
; v4_ncfw_iram_bin — MAIN IDLE DISPATCHER @0x4b84  (HIGH)
+0x4b84  36 41 00   entry  a1, 32            ; windowed prologue (imm = imm12*8 = 4*8 = 32)
+0x4b87  25 03 00   call8  0x4bb8            ; one-shot D-cache writeback (v4/v4+ only; §below)
+0x4b8a  d0 20 00   extw                     ; external-bus write fence
LOOP (@0x4b8d):
+0x4b8d  20 32 20   mov    a3, a2            ; (or a3,a2,a2) register shift: a3 <- a2
+0x4b90  30 43 20   mov    a4, a3            ; a4 <- a3   (save across the call)
+0x4b93  0c 12      movi.n a2, 1             ; simcall code = 1
+0x4b95  00 51 00   simcall                  ; *** SOLE HOST-BUS CALL *** (a2 = syscall code)
+0x4b98  2d 04      mov.n  a2, a4            ; restore a2 from a4
+0x4b9a  f0 41 00   break  1, 15             ; heartbeat token to the debug bus
+0x4b9d  00 7f 00   waiti  15                ; sleep until any IRQ (level <= 15) = doorbell
+0x4ba0  46 fa ff   j      0x4b8d            ; loop
```

When the doorbell IRQ fires, the hardware vectors through the UserException prologue (`@0x6c`, §2), which dispatches through the DRAM exception table (§5). On return, the post-wake path classifies the cause:

```asm
; POST-WAKE EXCCAUSE FAST-PATH @0x4ba6  (HIGH)
+0x4ba6  00 e8 13   wsr.exccause a0          ; SR 0xE8 exccause
+0x4ba9  30 30 34   <TIE>                    ; extract cause -> a3
+0x4bac  26 43 02   beqi   a3, 4, 0x4bb2     ; *** EXCCAUSE==4 (Level1Interrupt/doorbell) ***
+0x4baf  10 41 00   break  1, 1              ; else: unexpected-cause debug trap
+0x4bb2  00 52 00   <op0=0>                  ; handler entry (cause==4 fast-path)
```

> **QUIRK —** there is **no `rfi`/`rfe` in any decoded path**. The model is Level-1-only: `waiti 15` sleeps, the doorbell raises a Level-1 interrupt, and dispatch is by software `EXCCAUSE` test rather than a hardware return-from-interrupt. The absence is consistent across the swept ranges but cannot be *proven* exhaustively — a linear sweep cannot follow control flow through the TIE/FLIX bundles. CONFIDENCE: **HIGH** (loop + fast-path); **MED** (the *absence* of rfi/rfe).

### The one-shot D-cache writeback (v4/v4+ only)

```asm
; v4_ncfw_iram_bin — D-CACHE WRITEBACK SUBROUTINE @0x4bb8  (HIGH)
+0x4bb8  36 41 00   entry  a1, 32
+0x4bbb  22 a0 40   movi   a2, 64            ; 64 loop iterations
+0x4bbe  32 a0 00   movi   a3, 0             ; base = 0
+0x4bc1  76 82 0e   loop   a2, 0x4bd3        ; hardware LOOP (a2 count) over 4 cache ops
+0x4bc4  82 73 04   <dcache-op> a3, 16       ; writeback op (op0=2,r=7,t=8; DHWBI? MED), 4 lines
+0x4bc7  82 73 44   <dcache-op> a3, 272
+0x4bca  82 73 84   <dcache-op> a3, 528
+0x4bcd  82 73 c4   <dcache-op> a3, 784
+0x4bd0  32 d3 01   addmi  a3, a3, 256       ; stride 256B
+0x4bd3  1d f0      retw.n                    ; windowed return
```

> **NOTE —** the writeback uses the architectural `LOOP` (zero-overhead loop, implying `LBEG/LEND/LCOUNT`) over 4 in-line cache ops × 64 iterations = 256 lines × 256B = a 64 KiB region flush. The exact mnemonic at `+0x4bc4` (`t=8` in the CACHE group) is `DHWBI` vs `DHWB`-ambiguous without the `.tie`; the *effect* (write-back of the working region before entering the idle loop) is clear from placement. CONFIDENCE: **HIGH** (structure); **MED** (mnemonic).

### The slave loop

```asm
; v4_ncfw_iram_bin — SLAVE LOOP @0x4ad4 (secondary cores)  (structure HIGH; logic TIE/MED)
+0x4ad4  36 41 00   entry  a1, 32
+0x4ad7..           ; <TIE> + l32i.n list walk (read 0/4 offsets), addx4 indexing,
                    ; bgeu bounds checks, call8 helpers (0x4bfc, ...), j back-edges
                    ; — a descriptor-list worker distributing CC-op sub-steps across
                    ;   non-primary cores.
```

> **QUIRK —** the v2/v3 idle loop uses the **2-byte `mov.n`** narrow for the register shift and has **no `call8 dcache-wb`** before the loop; v4/v4+ add the writeback `call8` and use the **3-byte `or aX,aY,aY`** form (`20 32 20` = `mov a3,a2`). The host idiom set (`simcall`/`waiti 15`/`break 1,15`/`break 1,1`/`extw`) is otherwise structurally identical across all four arches — see §5. The per-arch idle-loop offset differs (v2 `simcall@0xa87c`, v3 `@0x4b64`, v4/v4+ `@0x4b95`), but v4 and v4_plus are byte-identical here. CONFIDENCE: **HIGH** (offsets re-located by byte scan this pass).

### Function Map — idle/slave

| Region | Role | Confidence |
|---|---|---|
| `+0x4b84` | Main idle dispatcher (primary core) | HIGH |
| `+0x4b95` | The sole `simcall(a2=1)` host call | HIGH |
| `+0x4ba6` | Post-wake `EXCCAUSE==4` doorbell fast-path | HIGH |
| `+0x4bb8` | One-shot D-cache writeback (v4/v4+) | HIGH (shape) / MED (cache mnemonic) |
| `+0x4ad4` | Slave work-distribution loop | HIGH (shape) / MED (TIE body) |

---

## 5. Host Interface and Exception Dispatch

### Purpose

The host interface is small enough to enumerate exhaustively, and doing so is the strongest non-disassembly anchor on the page: every idiom that crosses the host/device boundary appears **exactly once** per image. The exception dispatch is the bridge from the idle loop's `waiti` to the CC-op handlers.

### The host idiom set — verified counts

Scanning each idiom's byte pattern across all four IRAM blobs returns a count of **1** for every one — the host interface is a single, uniform idle-loop construct, not a scattered API.

| Idiom | Bytes | Per-image count (v2/v3/v4/v4+) | Role |
|---|---|---|---|
| `simcall` | `00 51 00` | `1 / 1 / 1 / 1` | The sole host-bus call; `a2 = code = 1` |
| `waiti 15` | `00 7f 00` | `1 / 1 / 1 / 1` | Sleep until any IRQ (level ≤ 15) |
| `break 1, 15` | `f0 41 00` | `1 / 1 / 1 / 1` | Idle-loop heartbeat token to the debug bus |
| `break 1, 1` | `10 41 00` | `1 / 1 / 1 / 1` | Unexpected-`EXCCAUSE` debug trap (post-`waiti` else-arm) |
| `extw` | `d0 20 00` | `1 / 1 / 1 / 1` | External-bus write fence before entering the loop |

> **QUIRK —** the entire host interface is **one `simcall(a2=1)`**. The host side (simulator / JTAG / kernel sim-bus) services it; there is no second device→host primitive. The `break 1,15` is a *heartbeat token* (a debug-bus counter tick, not a fault), and `break 1,1` is the unexpected-cause trap. A reimplementer must not model `simcall` as a general syscall ABI — it is a single fixed call in a single fixed place, and `a2` is always `1`. CONFIDENCE: **HIGH** (counts re-scanned this pass, all `1`).

### The DRAM exception table

The DRAM blob's first `u32[4]` is the exception-dispatch table, indexed by exception slot; each entry is an IRAM handler-stub address. Read straight from the DRAM bytes (LE):

| arch | `u32[0]` | `u32[1]` | `u32[2]` | `u32[3]` | note |
|---|---|---|---|---|---|
| v2 | `0x1bb3` | `0x1bcf` | `0x1be3` | `0x1bb3` | slot0 == slot3 (catchall) |
| v3 | `0x1399` | `0x13b1` | `0x13c5` | `0x1399` | slot0 == slot3 |
| v4 | `0x1399` | `0x13b1` | `0x13c5` | `0x1399` | identical to v3 |
| v4_plus | `0x1399` | `0x13b1` | `0x13c5` | `0x1399` | identical to v3/v4 |

Each table entry points at a thin IRAM stub that `call8`s a deep handler and then restores the window:

```asm
; v4_ncfw_iram_bin — exception handler stubs (HIGH for shape; handler internals TIE/MED)
+0x1399  25 2e 00   call8 0x167c   ; slot0/3 (catchall) -> deep CC-op handler
+0x13b1  ..  ..  .. call8 ...       ; slot1 handler
+0x13c5  25 39 00   call8 0x1758   ; slot2 handler
;   each stub: call8 <deep handler> ; restore window/excvaddr.
;   deep handlers (0x167c, 0x1758, 0x3074, 0x2a818, ...) read the cc_op_entry / CC-context
;   schedule from DRAM and drive DMA + semaphore ops through TIE instructions.
```

> **GOTCHA —** the dispatch table lives in the **DRAM blob**, not the IRAM image, and slot0 == slot3 makes slot 3 a catchall alias of slot 0. A reimplementer wiring the device's exception vectors must populate this 4-entry table in HBM (not patch IRAM), and must respect that the doorbell (`EXCCAUSE==4`) is the only cause the idle loop's fast-path handles inline — every other cause falls to `break 1,1`. CONFIDENCE: **HIGH** (table bytes read directly; v2 differs from v3/v4 as shown).

### CSR / special-register usage

Recognized architectural/windowed SRs decode directly; the custom CSRs are named only by number (their meaning needs the `.tie`):

| SR | Name / role | Where | Confidence |
|---|---|---|---|
| `0x48` | `WindowBase` | boot windowed-ABI init (`wsr`) | HIGH |
| `0x49` | `WindowStart` | window spill/fill (`rsr`) | HIGH |
| `0x5A` | `RASID` (MMU ring ASID) | boot (`wsr`) | HIGH |
| `0xEE`/`0xE6` | `EXCVADDR` | exception prologue (`rsr`/`wsr`) | HIGH |
| `0xC0`/`0xE8` | `EXCCAUSE` | post-wake dispatch (`rsr`/`wsr`) | HIGH |
| LBEG/LEND/LCOUNT | hardware `LOOP` | dcache-wb subroutine | HIGH |
| `0x0C` | cache-region limit (`bltu` bound) | boot cache loops (`rsr`) | MED (custom) |
| `0x61` | cache/region or sequencer config | boot (`wsr`) | MED (custom) |
| `0xEB` | **CORE-ID / core-gate** | PRID gate (`rsr`) | MED (custom) |
| `0xE5`/`0xE7`/`0xEC`/`0xEF` | AWS sequencer custom (doorbell/semaphore/DMA-trigger class) | handlers | LOW (opaque) |

---

## 6. Per-Arch Delta Model

> **QUIRK —** the four images do **not** form a single parameterized template. The boot+idle *framework* is stable, but v2/v3 differ from v4/v4+ by the `+0x1c` D-cache shift and the idle-loop encoding, and **v4 vs v4_plus differ *only* inside the CC-op handler bodies**. A reimplementer should treat the boot/idle scaffold as one shared code base and the handler region as the per-generation revision surface.

| Dimension | v2 (sunda) | v3 (cayman) | v4 (mariana) | v4_plus (mariana_plus) |
|---|---|---|---|---|
| Reset slot A target | `0x1dc` | `0x1dc` | `0x1f8` | `0x1f8` |
| Reset slot B target | `0x1e8` | `0x1e8` | `0x204` | `0x204` |
| D-cache `dii` loop @0xd5 | — | — | present (the `+0x1c`) | present |
| Idle register-shift form | 2-byte `mov.n` | 2-byte `mov.n` | 3-byte `or aX,aY,aY` | 3-byte `or` |
| Idle `call8 dcache-wb` | — | — | present `@0x4b87` | present |
| Idle `simcall` offset | `0xa87c` | `0x4b64` | `0x4b95` | `0x4b95` |
| Exception table (DRAM) | `{1bb3,1bcf,1be3,1bb3}` | `{1399,13b1,13c5,1399}` | `{1399,13b1,13c5,1399}` | `{1399,13b1,13c5,1399}` |
| IRAM size | 43232 B | 19392 B | 19488 B | 19488 B |

### v4 vs v4_plus — the handler-only delta

A byte `cmp` of the two v4-class IRAM images locates the divergence precisely:

```text
v4_iram  vs  v4_plus_iram:
  0x0000 .. 0x1190   IDENTICAL   (reset + boot + vector table + early handlers)
  0x1190 .. 0x4ab0   DIFFER      (the CC-op handler bodies — ~40% of bytes)
  0x4ab0 .. end      IDENTICAL   (slave loop + idle dispatcher + dcache-wb)
  first differing byte: 0x1190    last differing byte: 0x4aac
```

> **NOTE —** because `0x0..0x1190` and `0x4ab0..end` are byte-identical, **v4_plus is a handler-logic revision on the v4 boot framework**, not a new image. The reset vector, cache loops, windowed-ABI init, MMU program, boot stage 2, PRID gate, slave loop, and the entire idle dispatcher are shared verbatim. Whatever changed in `mariana_plus` — a new CC-op algorithm, a barrier-step fix, an extra semaphore handshake — is confined to the `0x1190..0x4ab0` handler region. Characterizing that diff instruction-by-instruction is blocked on the TIE (§7). CONFIDENCE: **HIGH** (the identical/diff ranges are `cmp`-proven this pass).

---

## 7. The Opaque Sequencer TIE

### Purpose

Everything above decodes from the base Xtensa LX ISA plus Code-Density narrows. The remaining ~half of the IRAM — the window-save vectors, the boot-stage-2 body, the MMU per-op encodings, and all CC-op handler logic — is the **proprietary AWS sequencer TIE**, and it does not decode without the `.tie` config, which this firmware does **not** ship.

### What is opaque, and why

The sequencer TIE occupies three encoding regions a base-ISA decoder cannot resolve symbolically:

| TIE region | Encoding signature | Appears in | Confidence |
|---|---|---|---|
| MAC16 / sequencer custom ops | `op0 = 4` | boot stage 2, MMU program, CC-op handlers | LOW (opaque) |
| Window-save block | `op0=0 / op1=9 / op2 ∈ {2,6}` (5-insn) | vector table `+0x0c`, `+0x5d` | LOW (opaque) |
| FLIX-ish bundles | `op0 = 10 / 11` | `wdtlb`/`witlb`, deep handlers | LOW (opaque) |

> **QUIRK —** unlike the GPSIMD/Vision-Q7 core — whose Cadence `.tie` *is* shipped in `aws-neuronx-gpsimd-tools`, making every `ivp_*` op decode by name (see [Xtensa-Vision-Q7](../gpsimd/xtensa-vision-q7.md)) — the **NCFW sequencer TIE is not shipped anywhere in the runtime package**. There is no `.tie`, no `libtie`/`libisa` provider, no `xt_*.h` header for this config. The `op0=4` family here is the sequencer's MAC16/CSR/DMA-trigger space, **not** IVP vectors; this is a different, smaller TIE config with no vector register files. A reimplementer cannot symbolically decode these ops — they must be reverse-engineered *behaviorally*, by reading the operand registers and memory effects at each `<TIE>` site. This is the page's primary residual gap. CONFIDENCE: **LOW** (the symbolic decode is impossible; the *structural* placement of each TIE region is HIGH).

### The behavioral-model path

Where the TIE blocks the symbolic decode, the recoverable facts are the surrounding base-ISA frame: the GPR a TIE op reads (its operand window), the `l32i.n`/`s32i.n` around it (its memory footprint), and the CSR `rsr`/`wsr` it sits between. The deep handlers (`0x167c`, `0x1758`, `0x3074`, `0x2a818`) are visible as scaffolds — `entry`, list walks, `call8` chains, `j` back-edges — but their core work (ring/mesh/kangaring/barrier execution) is TIE. Binding each handler to a CC-op class requires cross-referencing the `cc_op_entry.algo_type` bitfield the handler reads from DRAM, which is owned by the [cc_op_entry On-Device Collective ISA](../collectives/cc-op-isa.md) and the libncfw serializer, not recoverable from the sequencer image alone.

> **CORRECTION — broken internal link repointed.** This sentence linked `[Collectives Scheduler](libncfw/collectives-scheduler.md)`, a target that does not exist (no `firmware/libncfw/` subdir). Repointed to the existing page that actually owns the `cc_op_entry.algo_type` bitfield — [The cc_op_entry On-Device Collective ISA](../collectives/cc-op-isa.md).

---

## Related Components

| Name | Relationship |
|---|---|
| `libncfw_get_image()` (host) | The blob provider — returns `{ptr, len}` into `.rodata`; no container |
| `encd_ncfw_init` / DRAM template patch (libnrt) | The consumer: DMAs IRAM→TPB-IRAM, DRAM→HBM, releases reset |
| `cc_op_entry` schema (DRAM) | The CC-op schedule the deep handlers consume; built host-side by libnrt/libnccom |
| GPSIMD/Vision-Q7 core | The *other* Xtensa config — IVP vector, `.tie` shipped; this is the sequencer config, no `.tie` |

## Cross-References

- [Tensilica Xtensa and Vision-Q7 Identification](../gpsimd/xtensa-vision-q7.md) — the canonical Xtensa LX lineage and toolchain proof; this page is the sequencer-specific boot/idle disasm, a *different* TIE config (its `.tie` is not shipped)
- [Embedded Payloads (8 Xtensa Blobs)](embedded-payloads.md) — the carve of the eight `*_bin` blobs from `libncfw.so` `.rodata`, sizes and arch-key map
- [Firmware Overview (libncfw)](overview.md) — where the sync core sits in the collectives firmware stack
- [Firmware Upload Path (DKMS → Device DRAM)](upload-path.md) — how the carved bytes reach device IRAM/HBM and reset is released
- [The Carrier Library (libncfw.so)](carrier-library.md) — the host-side provider and `ctx_log` serializer tree
- [back to index](../index.md)
