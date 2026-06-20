# NCFW Main Dispatch Loop

This page reconstructs the **NCFW management-core main dispatch loop** — the
entry/reset path, the per-iteration command poll, the `algo_type`→handler
dispatch table, the per-iteration idle→fetch→dispatch→run→signal state machine,
the completion/error legs, and the per-generation loop differences. It is the
*control-flow* companion to the [NCFW IRAM Images](ncfw-iram-images.md) (where the
bytes are) and the [NCFW DRAM Images + `ctx_log`](ncfw-dram-ctx-log.md) (the data
tables this loop reads). The loop and dispatch table are reproduced below as
annotated C pseudocode naming the **real device IRAM symbols/offsets**.

> **GOTCHA — this core is a scalar Xtensa-LX, NOT the Vision-Q7 "Cairo" FLIX DSP.**
> The loop documented here runs on the NCFW **management** core, a scalar Xtensa-LX
> control processor — *not* the 512-bit Vision-Q7 NX datapath (`ncore2gp`) that
> runs custom-op kernels. The "~26–28% FLIX" every earlier NCFW pass measured is a
> **mis-decode artifact** of pointing the only-shipped Vision-Q7 disassembler at
> scalar-LX bytes — there is **no real FLIX layer in NCFW**, and this page proves it
> on the bytes ([§1.3](#13-the-lx-not-flix-debunk-re-grounded-on-the-loop-bytes)).
> The full ISA evidence is the authoritative
> [scalar-LX core page](../../uarch/ncfw-lx-core.md). Do not budget a FLIX issue
> port for this core.

**Provenance & confidence.** Every fact is read **this session** from the four raw
NCFW IRAM/DRAM images carved out of the shipped host library `libncfw.so` (sha256
`598920d7…`, `.rodata` blobs, VMA==file-offset), decoded with the co-shipped
Cadence `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, the only registered core,
used as a base/windowed/density decoder) and a from-scratch scalar-LX length walk
(op0 `e/f` ⇒ 3-byte resync, the [scalar-LX rule](../../uarch/ncfw-lx-core.md)).
Lawful interoperability reverse engineering (DMCA 17 U.S.C. 1201(f)); no vendor
source snapshot consulted. Tags are `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` per
the [Confidence & Walls Model](../../reference/confidence-model.md): `OBSERVED` = a
byte/disasm read from the binary this pass; `CARRIED` = OBSERVED in a cited prior
carve and reused; `INFERRED` = reasoned over those. Callouts: **QUIRK**
(counter-intuitive but real), **GOTCHA** (a reimplementation trap), **NOTE**
(orientation), **CORRECTION** (overturns a prior reading).

> **THE v5 / MAVERICK WALL.** `libncfw` ships **exactly four** NCFW images
> (v2/v3/v4/v4_plus). **There is no MAVERICK (v5) NCFW image in this binary** — the
> `get_image` selector ladder stops at `arch_id 0x1c` and the blob region closes
> into `__GNU_EH_FRAME_HDR` @ `0x918e4` ([IRAM-images §5](ncfw-iram-images.md#5-the-v5--maverick-wall-no-ncfw-image-ships)).
> Every statement on this page is grounded on the **v2/v3/v4/v4+** bytes. Any claim
> about a **v5 NCFW loop interior is INFERRED/ABSENT, never observed** — the file
> does not contain one. `[WALL]`

The codename↔generation binding (per the committed
[Codename ↔ Generation Map](../../generations/codename-generation-map.md)) used
throughout: **SUNDA = NC-v2** (`arch_id 0x05`), **CAYMAN = NC-v3** (`0x0c`),
**MARIANA = NC-v4** (`0x14`), **MARIANA_PLUS = NC-v4+** (`0x1c`),
**MAVERICK = NC-v5** (ABSENT). `arch_id = coretype − 1`. The reference generation
for byte anchors is **CAYMAN (v3)**, the full table-dispatch reference.

---

## 0. The loop in one diagram

```text
  RESET (IRAM[0] = j reset_body) ──► boot (SP/VECBASE/window setup) ──► C entry
        │                                                                  │
        ▼                                                                  ▼
   ┌─────────────────────────── MAIN LOOP (v3 @0x3bb0, the LARGEST func) ──────────────┐
   │                                                                                    │
   │  IDLE  (v3 @0x4b58):  extw ; … ; waiti 15 ; j 0x4b5e   ◄── park at max INTLEVEL    │
   │     │  (one waiti / one extw per image)                                            │
   │     ▼  notification/semaphore interrupt wakes the core                             │
   │  FETCH:  read next command record → (A) command-type vector @DRAM+0x00             │
   │     │      {0x1399, 0x13b1, 0x13c5, 0x1399}  (3 trampolines + slot3==slot0 default)│
   │     ▼                                                                              │
   │  DISPATCH:  extui a9,a2,8,7   (7-bit cmd field)                                    │
   │             const16 a2,0xB0 ; addx4 a2,a3,a2 ; l32i.n a5,a2,0                      │
   │             handler = *(DRAM_base + 0xB0 + algo_index*4)   ◄── 12-entry table      │
   │     │                                                                              │
   │     ├─► algo case body (staggered computed-goto in the 0x3c.. / 0x3e.. clusters)   │
   │     │      RUN one step: WAIT-spin-poll → reduce/route DMA → SIGNAL next peer      │
   │     └─► idx 3 → 0x48c4 OUTLIER → 0x48e0 error/default region                       │
   │                                                                                    │
   │  COMPLETE/SIGNAL:  memw-fenced CSR store (s32i.n a4,[a10]) → barrier_completed     │
   │     └──────────────────────────────────── back to IDLE ───────────────────────────┤
   └────────────────────────────────────────────────────────────────────────────────────┘
```

Every box is byte-grounded below. `[OBSERVED HIGH]`

---

## 1. The decode method — and the LX-not-FLIX debunk

### 1.1 Which disassembler, and why the spine is recoverable

The only Xtensa core registered anywhere in the corpus is **`ncore2gp`** (the
Vision-Q7 NX datapath core); `xtensa-elf-objdump -i` lists exactly that one and
errors *"no Xtensa core registered as the default"* for any other (re-verified this
pass). There is **no NCFW LX disassembler config** — no `core-isa.h`, `-params`,
`.tie`, or `.flix` for the management core (the exhaustive inventory is on the
[scalar-LX core page](../../uarch/ncfw-lx-core.md)). So the loop is decoded two
ways, cross-checked: (a) `ncore2gp` as a **base/windowed/density** decoder — the
reset `j`, the window-overflow handler, `entry`/`call8`/`call0`/`l32r`/`waiti`/
`memw`/`const16`/`addx4`/`extui`/branches all decode config-independently; and
(b) a from-scratch scalar-LX length walk (op0 `e/f` ⇒ 3-byte resync, resync at
`retw.n` = `1d f0`).

The **dispatch spine** — function boundaries, the jump table, the call graph, the
idle loop, the field extractions — is recoverable with **HIGH** confidence. The
instruction-by-instruction **case bodies** are not (§4). `[OBSERVED HIGH]`

### 1.2 The decode anchors — byte-exact this pass

Read directly from the carved v3 (CAYMAN) IRAM `[OBSERVED HIGH]`:

| IRAM off | bytes | decode | role |
|---|---|---|---|
| `0x0` | `06 76 00` | `j 0x1dc` | reset vector |
| `0x6` | `86 77 00` | `j 0x1e8` | window/exc vector |
| `0x24` | `80 01 09` | `l32e a8,a1,…` | WindowOverflow8 spill handler |
| `0x3bb0` | `36 a1 00` | `entry a1,80` | **MAIN LOOP** prologue |
| `0x3bb5` | `20 98 64` | `extui a9,a2,8,7` | extract the 7-bit cmd field from `a2` |
| `0x3bf8` | `24 b0 00` | `const16 a2,0xb0` | **dispatch-table base** (occurs ONCE in v3) |
| `0x3bfb` | `20 23 a0` | `addx4 a2,a3,a2` | `a2 = (index a3)<<2 + 0xB0` |
| `0x3bfe` | `58 02` | `l32i.n a5,a2,0` | `a5 = table[index]` (the case-label IRAM addr) |
| `0x4b58` | `36 41 00` | `entry a1,32` | **IDLE** prologue |
| `0x4b5b` | `d0 20 00` | `extw` | ordering barrier (the image's one `extw`) |
| `0x4b6c` | `00 7f 00` | `waiti 15` | park at max INTLEVEL (the image's one `waiti`) |
| `0x4b6f` | `c6 fa ff` | `j 0x4b5e` | idle back-edge |
| `0x1399` | `25 2e 00` | `call8 0x167c` | (A)[0] command-type trampoline |
| `0x13b1` | `25 62 00` | `call8 0x19d4` | (A)[1] trampoline |
| `0x13c5` | `25 39 00` | `call8 0x1758` | (A)[2] trampoline |

> **QUIRK — `const16 a2,0xB0` (`24 b0 00`) appears EXACTLY ONCE in the v3 image,
> and ZERO times in v2.** Counted byte-exact this pass: v3 = 1, v2/SUNDA = 0. That
> single literal load — the byte offset `0xB0` of the DRAM dispatch table — is the
> **fingerprint** of the entire table-based dispatch. SUNDA has no such instruction
> because it has no `+0xB0` jump table (§5/§7.2). `[OBSERVED HIGH]`

### 1.3 The LX-not-FLIX debunk, re-grounded on the loop bytes

Pointing the native `ncore2gp` disassembler at the **real v3 IRAM** decodes the
base op `rsr.excvaddr a2` @ `0x6c` correctly (`20 ee 03`) but then spews
**impossible-for-a-control-core** Vision-DSP opcodes — verbatim this pass at `0x76`:

```text
  6c:  20 ee 03   rsr.excvaddr  a2        ◄── correct base op
  76:  00 00 06   ivp_scatterw             ◄── a Cairo VECTOR-SCATTER on a DMA control core
  7e:  00         .byte 00                  ◄── resync noise after the bogus bundle
  7f:  00         .byte 00
```

`ivp_scatterw` is a 512-bit Vision SIMD scatter; it **cannot** exist on a scalar
collective-orchestration core. It is the wrong-config artifact: the `ncore2gp` FLIX
length table greedily eats `op0=e/f` operand bytes as 16-/8-byte Vision bundles.
That is the entire mechanism of the spurious "~26–28% FLIX". Under the **scalar-LX
rule** the same `op0=e/f` bytes are ordinary operand/immediate bytes of scalar
2-/3-byte instructions, and the dispatch read decodes **cleanly** under that same
tool:

```text
  3bf8:  24 b0 00   const16  a2, 176   (=0xB0)
  3bfb:  20 23 a0   addx4    a2, a3, a2
  3bfe:  58 02      l32i.n   a5, a2, 0
```

The `retw.n` resync ratios confirm the scalar rule wins on these very images
(matching the committed
[scalar-LX core page](../../uarch/ncfw-lx-core.md#4-why-the-core-is-scalar-xtensa-lx--decoded-against-the-blob)):
**v3 90/133, v4 101/134, v4+ 100/134** under scalar `(e3,f3)` vs 66/74/69 under the
Vision `(e16,f8)` rule. `[OBSERVED HIGH]`

> **CORRECTION — there is no NCFW FLIX layer to recover, in EITHER width.** Two
> earlier report lineages each invented a FLIX over scalar bytes (radare2's 8-byte
> guess, `ncore2gp`'s 16-byte Vision bundle). Both are decoder artifacts: the
> cleanly-decoded NCFW instruction set is **exclusively 2-byte and 3-byte** (no
> genuine wide bundles), the SR set is the standard LX registry (no TIE coprocessor
> SRs), and the scalar resync beats both FLIX widths. NCFW is a scalar LX control
> core; do not reimplement a FLIX issue port for it. `[OBSERVED HIGH]`

---

## 2. Entry / reset path

### 2.1 The reset & exception vectors

The carved blob **is** the IRAM-resident image: the Xtensa XEA2 vector table sits
at offset `0x0`, code follows, and there is **no load-address header** (the device
IRAM base is set by the upload path, not embedded). The first two `j` (opcode
`0x06`) are the reset and window/exception vectors `[OBSERVED HIGH]`:

| gen | head bytes | reset `j` @ `0x0` | secondary `j` @ `0x6` |
|---|---|---|---|
| v2 SUNDA / v3 CAYMAN | `06 76 00 … 86 77 00` | `j 0x1dc` | `j 0x1e8` |
| v4 MARIANA / v4+ MARIANA_PLUS | `06 7d 00 … 86 7e 00` | `j 0x1f8` | `j 0x204` |

The `j` immediates were re-derived this pass (`op0=6`, 18-bit signed offset in bits
`[23:6]`, `target = PC+4+off`). The `0x1dc`→`0x1f8` shift in v4/v4+ is the +28-byte
(`+0x1c`) early-boot relocation (a D-cache-invalidate loop inserted ahead of every
inline handler — [IRAM-images §1.1](ncfw-iram-images.md#11-the-raw-image-head--reset-vectors-at-the-iram-base)).
The `WindowOverflow8` handler is at `0x24` (`l32e a8..a15` / `s32e a8..a15`),
**byte-identical across all four gens** — proving the windowed XEA2 ABI.
`[OBSERVED HIGH]`

### 2.2 Reset body → C entry (the honest limit)

The reset body (`0x1dc` v2/v3 / `0x1f8` v4/v4+) is a short boot sequence —
register/SP/`VECBASE` setup, then a jump into the C entry. The **exact** SP/vector
values and the reset→main hand-off **address are NOT decodable**: the reset window
(`0x1b8..0x208`) is `op0=e/f`-dense and the LX core has ops no shipped config names.
The presence of `l32r`-shaped reset-time loads and a literal block at `0x1f0..0x208`
is OBSERVED; the precise values are not. So the hand-off is pinned **structurally**
(via the (A) vector and the function map, §3–4), not by instruction decode.
`[bytes OBSERVED HIGH; reset-body decode MED — op0=e/f-corrupted]`

---

## 3. Function map + the (A) command-type vector

### 3.1 The function skeleton (from `entry` prologues)

Sweeping each IRAM for valid windowed prologues (`entry a1,N`, frame in
`{32,48,64,80,96}`) partitions it into functions. The **largest function is the
main loop**; the **one `waiti 15` is the idle point** `[OBSERVED HIGH]`:

| image | #funcs | MAIN LOOP (largest) | size | IDLE (`waiti 15`) |
|---|---|---|---|---|
| v2 SUNDA | 23 | `@0x5f60` | `0x1760` | `@0xa884` |
| v3 CAYMAN | 57 | `@0x3bb0` | `0xd28` | `@0x4b6c` |
| v4 MARIANA | 61 | `@0x3bc8` | `0xd3c` | `@0x4b9d` |
| v4+ MARIANA_PLUS | 55 | `@0x3bc8` | `0xd3c` | `@0x4b9d` |

The v4 main loop (`@0x3bc8`) is the v3 loop (`@0x3bb0`) relocated by **+0x18**
(size `0xd3c` = `0xd28`+0x18) — a code-insertion ahead of the `0x3c20` cluster (§7).

### 3.2 The poll source — the (A) command-type vector @DRAM+0x00

The DRAM image header @`+0x00` holds a 4-entry IRAM-code-address vector — the
per-iteration **command/event-type** entry points (read byte-exact from the carved
DRAM this pass) `[OBSERVED HIGH]`:

| gen | (A)[0] | (A)[1] | (A)[2] | (A)[3] |
|---|---|---|---|---|
| v2 SUNDA | `0x1bb3` | `0x1bcf` | `0x1be3` | `0x1bb3` (==[0]) |
| v3/v4/v4+ | `0x1399` | `0x13b1` | `0x13c5` | `0x1399` (==[0]) |

All three **distinct** entries are **trampolines** that `call8` a handler and all
live inside one host function (`0x132c..0x14bc` in v3). The 4th slot equals the 1st
— the canonical *unknown-command → default* guard. `[OBSERVED HIGH]`

```c
// (A) command-type vector @ DRAM+0x00 — the per-iteration dispatch on COMMAND TYPE.
// v3 trampolines (byte-exact call8s):
void (*A_vector[4])(void) = {
    /*[0]*/ trampoline_1399,   // 1399: call8 0x167c  -> handler @0x167c (entry a1,48)
    /*[1]*/ trampoline_13b1,   // 13b1: call8 0x19d4  -> handler @0x19d4 (entry a1,48)
    /*[2]*/ trampoline_13c5,   // 13c5: call8 0x1758  -> handler @0x1758 (entry a1,96)
    /*[3]*/ trampoline_1399,   // == [0]  : DEFAULT / unknown-command fallback
};
```

> **NOTE — the actual mailbox CSR is computed at runtime, not encoded statically.**
> The notification/semaphore source the loop drains is **not** a literal CSR in the
> static image. The per-die `soc_addr` semaphore/CSR base table lives at
> **DRAM+0x10** and the 40-byte per-entry descriptor table at **DRAM+0x98** (v2,
> 11 recs) / **DRAM+0x188** (v3/v4, 16 recs) — see
> [NCFW DRAM Images](ncfw-dram-ctx-log.md). The firmware computes the exact
> doorbell/semaphore CSR from `base + per-entry offset` inside the LX core. So the
> poll source is a **semaphore/notification-CSR set** pinned structurally, not by a
> decoded CSR-read instruction. `[base table + descriptors OBSERVED HIGH; "this is
> the poll source the loop drains" INFERRED MED]`

The cleanly-decoding handler `0x167c` runs end-to-end to its `retw.n` @`0x16a8`
with two in-range `call8` targets (`0x381c`, `0x3840`) — a small command handler.
The larger handlers `0x19d4`/`0x1758` start clean (`entry`; `const16`; `call8` to
in-range `0x36ec`/`0x3784`) then desync on `op0=e/f` operand bytes in their interior
(the §4 limit). `[trampolines + 0x167c OBSERVED HIGH; larger interiors MED]`

---

## 4. The `algo_type` → handler dispatch table (DRAM+0xB0)

### 4.1 The dispatch read in the main loop

Inside the largest function (v3 `@0x3bb0`) the dispatch index sequence is, byte-for
byte `[OBSERVED HIGH]`:

```c
// MAIN LOOP dispatch read — v3 @0x3bb0 (CAYMAN), byte-confirmed under the scalar-LX rule.
//   The handler table is *device DRAM+0xB0*; the index is the algo/command nibble.
uint32_t cmd = a2;                              // 3bb0: entry a1,80   (the command word)
uint32_t idx = (cmd >> 8) & 0x7f;               // 3bb5: extui a9,a2,8,7  (7-bit cmd field)
//      … (the algo_type 4-bit nibble, §4.3, is the dispatch SELECTOR within this field)
uint32_t *T  = (uint32_t *)(DRAM_base + 0xB0);  // 3bf8: const16 a2,0xB0  (table base; ONE per image)
uint32_t  handler = T[idx];                     // 3bfb: addx4 a2,a3,a2 ; 3bfe: l32i.n a5,a2,0
//                                              //   a2 = (idx<<2)+0xB0 ; a5 = *(DRAM+0xB0+idx*4)
goto *handler;                                  // computed-goto into a 0x3c.. / 0x3e.. / 0x48xx case label
```

`a5` is loaded with `T[idx]`, an **IRAM code address that falls inside the main-loop
function** — i.e. the table holds **case labels / computed-goto targets**, not
addresses of separate functions. `[OBSERVED HIGH]`

### 4.2 The 12-entry table @DRAM+0xB0 (carved byte-exact)

Read directly from the carved v3/v4 DRAM this pass `[OBSERVED HIGH]`:

| idx | v3 (CAYMAN) | v4/v4+ (MARIANA/_PLUS) | Δ | note |
|---|---|---|---|---|
| 0 | `0x3c20` | `0x3c38` | +0x18 | ring/mesh case body |
| 1 | `0x3c1d` | `0x3c35` | +0x18 | case body |
| 2 | `0x3c16` | `0x3c2e` | +0x18 | case body |
| **3** | **`0x48c4`** | **`0x48f0`** | +0x2c | **OUTLIER → 0x48e0 error/default region** |
| 4 | `0x3c0f` | `0x3c27` | +0x18 | case body |
| 5 | `0x3c08` | `0x3c20` | +0x18 | ┐ |
| 6 | `0x3c08` | `0x3c20` | +0x18 | ├ three identical entries ⇒ same handler |
| 7 | `0x3c08` | `0x3c20` | +0x18 | ┘ (shared common/no-op leg) |
| 8 | `0x3e84` | `0x3e9c` | +0x18 | second case cluster (`0x3e..` region) |
| 9 | `0x3e1e` | `0x3e32` | +0x14 | " |
| 10 | `0x3e5e` | `0x3e76` | +0x18 | " |
| 11 | `0x3e68` | `0x3e80` | +0x18 | " |

All v4 targets verified inside `0x3bc8..0x4900`. The v3→v4 shift is the same +0x18
code-relocation throughout (idx 9 is +0x14, idx 3 +0x2c) — a pure code-insertion,
not a topology change (§7). `[OBSERVED HIGH]`

> **QUIRK — the case labels are a STAGGERED computed-goto, not separate functions.**
> Walking the ring/mesh cluster under the scalar-LX rule, the labels are entry
> points into **one packed body** at fixed +7/+7/+7/+3-byte strides (verified
> byte-exact this pass): `idx5/6/7 → 0x3c08 ─+7→ idx4 0x3c0f ─+7→ idx2 0x3c16
> ─+7→ idx1 0x3c1d ─+3→ idx0 0x3c20`. A higher dispatch index enters **earlier** in
> the chain and executes **more** steps — a Duff's-device / staggered-fallthrough
> dispatch (the three identical `0x3c08` entries for idx5/6/7 are exactly this
> shared entry). Each 7-byte stride is `[op0=e/f leader, 3B] + [arith op, 3B] +
> tail`; the arithmetic (`sub`/`mul16u`, the reduce/index math) decodes cleanly, the
> leading `op0=e/f` op stays un-nameable. `[stride structure OBSERVED HIGH; per-step
> e/f-op semantics MED/UNKNOWN]`

### 4.3 The selector: the `algo_type` nibble

The dispatch SELECTOR is the collective **`algo_type`** field — the **low nibble of
`enc_alg_type`** (`RING=0 … BW_OPT_MESH=10`, `INVALID=11`), carried in byte 0 of the
firmware `cc_op` command word
([collective-enums §3.3 / §7](../ops/collective-enums.md#33-enc_alg_type--the-algorithm-selector-die-0x61eb3)).
The host `__select_algorithms` assigns one `enc_alg_type` per leg; the device-side
realization is this 12-entry table (plus barrier/default legs). The case bodies
(`0x3c..` / `0x3e..` clusters) are the device entry points for the algorithms whose
config+runtime structs the sibling handler pages decode host-side:

| `algo_type` / leg | device case label (v3) | handler page |
|---|---|---|
| ring / kangaring (`RING=0`, `KANGARING=3`, `SINGLE_CYCLE_RING=4`) | `0x3c..` cluster | [Ring / Kangaring](ring-kangaring.md) |
| mesh (`MESH=2`, `*_MESH=6/8/9/10`) | `0x3c..`→`0x3e..` (event-tape stepping) | [Mesh Collective](mesh-collective.md) |
| hierarchical (`HIER=1`, `INTRA/INTER_RDH=5/7`) | `0x3e..` cluster | [Hierarchical](hierarchical-collective.md) |
| barrier (device/host) | a case leg + `0x3e..` | [neff-device-barrier](neff-device-barrier.md) / [neff-host-barrier](neff-host-barrier.md) |
| default / error | idx 3 → `0x48c4` → `0x48e0` region | [§6.4](#64-error--default-leg) |

> **NOTE — `algo_type` = "how to move/route", the reduce op is elsewhere.** The
> `cc_op` command word carries **no** reduce-op field; `algo_type` only selects the
> routing algorithm. The arithmetic (`ADD`/`MAX`/`MIN`) rides the SDMA CCE
> descriptor (`SDMA_CCETYPE`), not the dispatch. The `cc_op` schema is on
> [Ring-protocol config command](ring-protocol-config-command.md) and
> [SPAD cc_op / tsync](spad-ccop-tsync.md). `[OBSERVED-absence HIGH; the
> algo_type ≡ enc_alg_type low-nibble identity is MED — width(4b) + shared
> vocabulary; the host→device write crosses into the un-disassemblable LX core]`

> **GOTCHA — device IRAM addresses ≠ host decoder addresses.** The handler-page
> addresses (e.g. host `ncfw_log_algo_*` printers) are **x86-64** decoder addresses
> in `libncfw.so`, NOT device IRAM addresses. The DEVICE handler entry points are
> the small IRAM addresses in the DRAM (A)/(C) tables above. The two address spaces
> are linked by **role** (ring/mesh/hier/barrier), not by numeric equality.
> `[OBSERVED HIGH]`

### 4.4 The case-body interiors stay hard (the honest limit)

Walking the ring cluster (idx0 entry `0x3c20`) and the hier cluster (idx9 entry
`0x3e1e`) under the scalar-LX rule, both desync within the first ~16 bytes on
`op0=e/f` operand bytes (`fe 5f 05` @`0x3c20`, `5e 2a 95` @`0x3c29`, …); the
`call0/call8` targets the walker then emits point **out of image**
(`0xc900`, `0x452c4`, `0xfffd3920` — all > the `0x4bc0` v3 IRAM size), i.e.
mis-synced operand bytes that happen to form a call opcode. So the case-body
**control flow** is not recoverable instruction-by-instruction. What **is**
recoverable is (a) the staggered computed-goto entry structure (§4.2), and (b) the
leaf primitives the bodies call (§5). `[OBSERVED HIGH that the interiors stay hard]`

---

## 5. Per-iteration state machine (idle → fetch → dispatch → run → signal)

### 5.1 IDLE — `waiti 15` at max INTLEVEL

The idle state is a tiny function (v3 `@0x4b58`, `entry a1,32`), fully decoded
`[OBSERVED HIGH]`:

```c
// IDLE — v3 @0x4b58. The core PARKS here until a notification/semaphore interrupt.
void ncfw_idle(void) {                  // 4b58: entry a1,32
    for (;;) {                          // 4b5e: loop top (the j back-edge target)
        extw();                         // 4b5b: extw      (the image's ONE ordering barrier)
        /* … minimal state setup … */
        waiti(15);                      // 4b6c: waiti 15  (park at max INTLEVEL; ONE per image)
        /* back-edge */                 // 4b6f: j 0x4b5e
    }
}
```

`extw` (one per image) and `waiti 15` (one per image) were counted byte-exact this
pass. The idle function also carries `simcall 0` / `break 1,15` host-debug/trap
hooks adjacent to the `waiti`. `[OBSERVED HIGH]`

### 5.2 FETCH → DISPATCH → RUN

Each iteration the loop (a) reads the next command/event descriptor (the (A) vector,
§3.2; poll source §3.2 NOTE), (b) extracts the algo/command index (the `extui` at
the top of the main loop) and indexes the DRAM+0xB0 table (§4.1) to jump into the
case body, and (c) **runs one step** of the selected algorithm's state machine,
advancing its runtime scoreboard:

- **ring**: per-channel `run_state` u8 (32×16B scoreboard @`algo_ctx+0`);
  `recv_cnt`/`send_credit`/`m2s_val`/`s2m_val` updated per step
  ([Ring / Kangaring](ring-kangaring.md)).
- **hierarchical**: `run_state` u8 (@`algo_ctx+0x200`)
  ([Hierarchical](hierarchical-collective.md)).
- **mesh**: `event_index` u16 cursor (@`algo_ctx+0x204`) walking the **50-event**
  (SUNDA) / **108-event** (CAYMAN/MARIANA/MARIANA_PLUS) tape; each event carries
  `event_type`/`dma_trigger`/`direct_trigger`/`wait_event` u8
  ([Mesh Collective](mesh-collective.md)).

The one step composes **three re-decoded leaf primitives** from the helper bank at
`0x3100..0x36f0` (a dense library of **41 windowed functions** — `entry`/`retw.n`
sweep this pass: 41 prologues / 32 `retw.n`):

```c
// One collective STEP, expressed in the §5.2 leaf primitives (helper bank @0x3100..0x36f0).
// (a) WAIT — spin-poll until the inbound semaphore reaches the step target.
//     v3 @0x3498 (one of TEN such primitives, the full {ge,le,eq,ne} family in 2 reg banks):
do {
    memw();                         // 3498: c0 20 00  ordering barrier before the CSR read
    a2 = *(volatile u32 *)a10;      // 349b: 28 0a     a2 = *(sema CSR)  (a10 = CSR pointer)
} while (a3 >= a2);                 // 349d: 27 b3 f7  bgeu a3,a2,0x3498 (spin until val > target)
return;                             // 34a0: 1d f0     retw.n

// (b) reduce / route DMA  — the case-body arithmetic + the un-nameable op0=e/f ops (§4.4).

// (c) SIGNAL — fenced CSR store to the next peer's recv_sema.
//     v3 @0x3113 (one of FIVE genuine fenced CSR-write signals):
memw();                             // 3113: c0 20 00  barrier
*(volatile u32 *)a10 = a4;          //       s32i.n a4,[a10]  (the SEMAPHORE-SET / -INC store)
```

> **QUIRK — every CSR access in the wait/signal path is `memw`-fenced.** The
> wait/signal substrate, left MED by earlier passes ("runs in the FLIX-corrupted
> bodies, not directly decodable"), **lifts to HIGH** under the scalar-LX rule: the
> helper bank decodes cleanly. The census is byte-stable across the table-dispatch
> family — `memw` **47/47/47** (v3/v4/v4+) — and `memw` **405** in the v2 monolith
> (denser, un-factored codegen). Each `memw` brackets exactly one CSR read (wait) or
> one CSR write (signal). A third primitive — an **atomic 64-bit CSR snapshot quad**
> (`memw; l32i [a10+0]; memw; s32i [a1+12]; memw; l32i [a10+4]; memw; s32i [a1+8]`)
> at `0x3684`/`0x36ac`/`0x36d4` — reads two adjacent CSR words memw-fenced into the
> frame. The host's "2R1W" ring step maps onto the two wait register banks (a2/a3,
> a5/a4) + one fenced signal store. `[primitives OBSERVED HIGH; per-step SCHEDULE
> (which sema/target/order) stays MED — it lives in the e/f-dense body interior + the
> runtime-populated DRAM target values]`

### 5.3 COMPLETE → SIGNAL

Completion is reported by the §5.2(c) fenced CSR store to the completion semaphores
(ring `post_sema`/`dma_compl_sema`; barrier `barrier_sema`/`dma_sync_sema`), whose
base addresses are the **DRAM+0x10** per-die table, each store `memw`-fenced. The
host-visible done flag is `neff_ctx.barrier_completed` (u8 @`neff_ctx+0x5c`) /
`host_barrier.barrier_done` ([neff-host-barrier](neff-host-barrier.md)). The value
written (in `a4`) is **not always 1** — the mesh `cc_op` overlay increments by
`1 << sema_shift_offset`, consistent with the device store writing the `a4` value
rather than a hardcoded constant. `[done-flag fields + CSR bases OBSERVED HIGH; the
device write path OBSERVED HIGH at the primitive level; the per-step schedule
INFERRED MED]`

### 6.4 Error / default leg

Dispatch index **3** (`0x48c4` v3 / `0x48f0` v4) is the **OUTLIER** target — it
jumps to the `0x48e0` region (a separate small function `@0x48e0`, `entry a1,32`,
size `0x1a4`), distinct from the `0x3c..` algorithm cluster. Combined with the (A)
vector's repeated default slot (§3.2), this is the **default / error / unknown-
command** leg. Verified byte-exact this pass: idx3 = `0x48c4` is the only table
entry outside the `0x3c..`/`0x3e..` clusters. `[OBSERVED structure HIGH; "error
reporting" naming INFERRED MED]`

---

## 7. Per-generation loop differences

### 7.1 v3 (CAYMAN) → v4 (MARIANA) → v4+ (MARIANA_PLUS): same architecture, relocated

- **Same dispatch design**: DRAM+0xB0 12-entry table → case labels inside the
  largest function. `[OBSERVED HIGH]`
- **v4 main loop `@0x3bc8` = v3's `@0x3bb0` relocated +0x18** (size `0xd3c` vs
  `0xd28`). Every DRAM+0xB0 entry shifts +0x18 (idx9 +0x14, idx3 +0x2c). The DRAM
  delta is **exactly 13 bytes**, all within offsets `0xB0..0x12c` (verified this
  pass: `ndiff=13`, range `0xb0..0x12c`) — the `soc_addr` CSR table and the 40-byte
  descriptor table are byte-identical CAYMAN↔MARIANA. A pure code-layout delta.
  `[OBSERVED HIGH]`
- **v4 vs v4+ IRAM first diverge @ byte `0x1190` (4496)** (verified this pass) but
  the main-loop address (`0x3bc8`), the DRAM+0xB0 table, and the (A) vector are
  **identical**, and the **DRAM is byte-identical** (verified `v4_dram == v4p_dram`
  this pass). MARIANA_PLUS is a code-only recompile on the same dispatch spine; its
  one functional add (the DGE reshape fast-path) lives in the NX-sequencer / Q7
  firmware, **not** the NCFW management core. `[OBSERVED HIGH]`
- **(A) vector identical** for v3/v4/v4+: `{0x1399, 0x13b1, 0x13c5, 0x1399}`.

### 7.2 v2 (SUNDA): structurally different dispatch

- **NO DRAM+0xB0 12-entry jump table.** v2's `+0xB0` region holds descriptor **DATA**
  (`0x0,0x0,0x0,0x0, 0xffffff,0x0, 0x4000000,0x1000000, 0x100200,0x1,0x0,0x0` —
  carved byte-exact this pass), **not** IRAM code addresses; and there is **no
  `const16 0xB0` index instruction anywhere in v2** (the `24 b0 00` one-shot pattern
  that defines the v3/v4 dispatch read is **absent** — counted 0). `[OBSERVED HIGH]`
- **Larger, more monolithic body**: 23 functions (vs 57), largest `@0x5f60`
  (`0x1760` B, ~1.8× the v3 main loop) on a ~43 KB IRAM (vs ~19 KB). SUNDA does not
  factor the wait/signal primitives into the a10-pointer helper bank either —
  searching v2 finds **4 inlined** spin-polls (base regs a3/a4/a6), present but not
  library-factored. `[OBSERVED HIGH]`
- **(A) vector shape is COMMON**: `{0x1bb3, 0x1bcf, 0x1be3, 0x1bb3}` — 3 command-type
  trampolines + 1 default, exactly the v3 shape; only the IRAM addresses differ. So
  the per-iteration **command** dispatch shape (3 handlers + default) is common to
  all four gens; only the **algorithm** dispatch realization differs (v2 monolithic;
  v3/v4/v4+ table-at-DRAM+0xB0). `[OBSERVED HIGH for the (A) shape; "v2 inlines the
  algo dispatch" INFERRED MED — v2 case bodies are e/f-corrupted too]`
- **v2 idle**: `waiti 15` @`0xa884` (one occurrence), same idle-loop pattern.

### 7.3 Summary table

| gen | reset→body | (A) cmd vector @D+0 | algo dispatch | #funcs | main loop | idle |
|---|---|---|---|---|---|---|
| v2 SUNDA | `j 0x1dc` | `{1bb3,1bcf,1be3,1bb3}` | **monolithic (NO D+0xB0)** | 23 | `0x5f60` | `0xa884` |
| v3 CAYMAN | `j 0x1dc` | `{1399,13b1,13c5,1399}` | 12-entry tbl @D+0xB0 | 57 | `0x3bb0` | `0x4b6c` |
| v4 MARIANA | `j 0x1f8` | `{1399,13b1,13c5,1399}` | 12-entry tbl @D+0xB0 (+0x18) | 61 | `0x3bc8` | `0x4b9d` |
| v4+ MARIANA_PLUS | `j 0x1f8` | `{1399,13b1,13c5,1399}` | 12-entry tbl @D+0xB0 (== v4) | 55 | `0x3bc8` | `0x4b9d` |

`[ALL OBSERVED HIGH]`

> **NOTE — two orthogonal groupings cross-cut the four gens.** The **reset-vector**
> magic groups them `{v2,v3}→0x1dc` vs `{v4,v4+}→0x1f8` (and v2/v3 share their first
> 121 IRAM bytes), but the **dispatch architecture** groups them `{v2}` vs
> `{v3,v4,v4+}`. SUNDA is the prologue-twin of CAYMAN yet the odd-one-out on
> dispatch. The deeper arch_id ↔ codename ↔ silicon reconciliation is consolidated
> in the [LX-ISA / arch_id synthesis](lx-isa-naming-archid-synthesis.md).
> `[OBSERVED HIGH]`

---

## 8. Reimplementer's checklist (the dispatch-loop obligations)

To rebuild a Vision-Q7-compatible GPSIMD engine, the **management-core dispatch**
layer obligates you to (the image-asset obligations are on
[NCFW IRAM Images §6](ncfw-iram-images.md#6-reimplementers-checklist-the-carve--selector-obligations);
the ISA/ABI obligations on the [scalar-LX core page](../../uarch/ncfw-lx-core.md)):

1. **Run the loop on a scalar Xtensa-LX control core**, windowed XEA2 ABI, **no FLIX
   issue port** — the case bodies are scalar 2-/3-byte ops, not wide bundles. `[HIGH]`
2. **Park the idle state at max INTLEVEL** with `extw; waiti 15; j back` (one `waiti`
   / one `extw` per image), waking on a notification/semaphore interrupt. `[HIGH]`
3. **Fetch on a command-type vector @DRAM+0x00** of 3 handler trampolines + 1 default
   slot (`[3]==[0]`). `[HIGH]`
4. **Dispatch the algorithm via a 12-entry jump table @DRAM+0xB0** indexed by the
   `algo_type` nibble (`handler = *(DRAM+0xB0 + algo_index*4)`), the targets being
   **case labels inside the main-loop function** (a staggered computed-goto, not
   separate functions), with index 3 reserved for the **error/default** leg. `[HIGH]`
5. **Compose each step from three `memw`-fenced leaf primitives** — WAIT-spin-poll
   (`memw; load CSR; cond-branch-back`), SIGNAL (`memw; store CSR`), atomic CSR
   snapshot — in an a10-pointer helper bank; the reduce/route arithmetic and the
   per-step semaphore schedule sit between them. `[primitives HIGH; schedule MED]`
6. **Compute the mailbox/doorbell CSR at runtime** from the DRAM+0x10 per-die base
   table + per-entry descriptor offsets (DRAM+0x98/+0x188) — do **not** hardcode CSR
   integers in the static image. `[MED — bases OBSERVED, integers runtime]`
7. **Stop at your top generation** — there is no MAVERICK NCFW image; the selector
   returns `2`/EINVAL for `arch_id > 0x1c`. `[HIGH]`

---

## 9. Cross-references

- [NCFW IRAM Images + Host Selector](ncfw-iram-images.md) *(Part 10)* — where the
  four IRAM/DRAM blobs are, the `get_image` selector, the v5 wall, the carve bounds.
- [NCFW DRAM Images + `ctx_log` Decoder](ncfw-dram-ctx-log.md) *(Part 10)* — the
  DRAM data tables (the `soc_addr` integers, the (A) vector, the `algo_type` table)
  this loop reads, and the host codename log decoders.
- [The NCFW Scalar-LX Management Core](../../uarch/ncfw-lx-core.md) *(Part 6)* — the
  authoritative ISA/decode evidence: the windowed XEA2 ABI, the standard-LX SR set,
  the FLIX-mis-decode debunk, the `retw.n` resync ratios.
- [LX-ISA / arch_id / Orchestration Synthesis](lx-isa-naming-archid-synthesis.md)
  *(Part 10)* — the consolidated arch_id ↔ codename ↔ silicon reconciliation.
- [Collective-Type + `cc_op` Enum Reference](../ops/collective-enums.md) *(Part 5)* —
  `enc_alg_type` (`RING0..BW_OPT_MESH10/INVALID11`), the `cc_op` command word, the
  `algo_type` nibble that indexes the DRAM+0xB0 table.
- The dispatched algorithm handlers: [Ring / Kangaring](ring-kangaring.md),
  [Mesh Collective](mesh-collective.md), [Hierarchical](hierarchical-collective.md).
- [Ring-protocol config command](ring-protocol-config-command.md) /
  [SPAD cc_op / tsync](spad-ccop-tsync.md) *(Part 10)* — the host-side `cc_op`
  command/config schema the loop's dispatch index comes from.
- [neff device barrier](neff-device-barrier.md) / [neff host barrier](neff-host-barrier.md)
  *(Part 10)* — the barrier completion/done-flag legs.
- [Codename ↔ Generation Map](../../generations/codename-generation-map.md) *(Part 6)*
  / [MAVERICK (NC-v5) Profile](../../generations/maverick-profile.md) — the
  generation join and the v5 wall.

---

## 10. Verification ledger (grounded this session)

| # | claim | how grounded **this session** | verdict |
|---|---|---|---|
| 1 | Dispatch read = `const16 a2,0xB0 (24 b0 00) ; addx4 a2,a3,a2 (20 23 a0) ; l32i.n a5,a2,0 (58 02)` @v3 `0x3bf8`; `const16 0xB0` occurs **once** in v3, **zero** in v2 | byte read at `0x3bf8`; `v3.count(24b000)=1`, `v2.count=0`; native `ncore2gp` objdump decodes it cleanly as `const16 a2,176; addx4; l32i.n` | **HIGH/OBSERVED** |
| 2 | 12-entry table @DRAM+0xB0: v3 `{3c20,3c1d,3c16,48c4,3c0f,3c08,3c08,3c08,3e84,3e1e,3e5e,3e68}`; v4 = +0x18; v2 = **data** (`0xffffff/0x4000000/…`); idx3 `0x48c4` is the OUTLIER | `<I` unpack of carved v3/v4/v2 DRAM @`0xB0`; staggered strides `[7,7,7,3]` for `0x3c08→0x3c20`; idx5==6==7==`0x3c08` | **HIGH/OBSERVED** |
| 3 | LX-not-FLIX: `ncore2gp` emits the impossible `ivp_scatterw` @v3 `0x76` right after the correct `rsr.excvaddr a2` @`0x6c`; scalar `(e3,f3)` resync 90/101/100 vs Vision `(e16,f8)` 66/74/69 | live `xtensa-elf-objdump XTENSA_CORE=ncore2gp` run on the carved v3 IRAM emits `ivp_scatterw`+`.byte`; resync ratios match [scalar-LX page](../../uarch/ncfw-lx-core.md) | **HIGH/OBSERVED** |
| 4 | (A) command vector @DRAM+0x00: v3/v4 `{1399,13b1,13c5,1399}` (`[3]==[0]`), v2 `{1bb3,1bcf,1be3,1bb3}`; trampolines `25 2e 00`/`25 62 00`/`25 39 00` (`call8 0x167c/0x19d4/0x1758`) | `<I` unpack of carved DRAM; byte read of the three v3 trampolines @`0x1399/0x13b1/0x13c5` | **HIGH/OBSERVED** |
| 5 | Per-gen diffs: v3→v4 DRAM = **exactly 13 bytes** in `0xB0..0x12c`; v4↔v4+ IRAM first diverge **byte 0x1190**; v4 DRAM == v4+ DRAM byte-identical; idle `waiti 15 (00 7f 00)` + `extw (d0 20 00)` one each; helper bank `0x3100..0x36f0` = 41 `entry`/32 `retw.n`; `memw` 47/47/47/405 | byte-diff carved DRAM (`ndiff=13`); byte-diff carved v4/v4+ IRAM (first=`0x1190`); v4_dram==v4p_dram True; count `007f00`/`d02000`=1 each; entry/retw + memw census | **HIGH/OBSERVED** |
