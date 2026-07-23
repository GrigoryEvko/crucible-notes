# The NCFW Scalar-LX Management Core

This page characterizes the **second, distinct Xtensa core** in the GPSIMD stack. The
custom-op kernel core — the one [Boot / Reset Sequence](boot-reset.md) and
[The SIMD Compute-Datapath](simd-datapath.md) describe — is the Vision-Q7 NX "Cairo"
512-bit-SIMD **datapath** core (`ncore2gp`). The core here is its quieter sibling: the
**NCFW** (NeuronCore collective-firmware) **management core**, a *scalar* Xtensa-LX control
processor with **no Vision/SIMD datapath, no MAC16, no coprocessor/TIE, and — decisively —
no FLIX/VLIW layer**. It exists to orchestrate the collective / RDMA control path that the
GPSIMD SB2SB collective hop rides on: it parks in a `waiti 15` idle loop, wakes on a
semaphore notification, dispatches a `cc_op` collective program through a small jump table,
and drives the per-step DMA-reprogram + semaphore rendezvous — while the *data* moves on the
SDMA/POOL data plane it sequences but never executes.

The page's reason to exist is a **correction**. Every earlier reading that pointed the
*only shipped* disassembler config (`ncore2gp` = Vision-Q7) at the NCFW firmware measured a
"**~26–28% FLIX**" content and one sibling (P-3-225) even pinned a "genuine 8-byte FLIX
bundle, op0=e→3 slots / op0=f→2 slots, four-way proof." **That FLIX is a decode artifact.**
This page debunks it *empirically* — by crafting a blob of two leader bytes and
watching the `ncore2gp` objdump greedily eat them as 16-/8-byte Vision SIMD bundles — and
gives the correct **scalar-LX length rule** (`op0 ∈ {e,f}` ⇒ treat as a 3-byte resync width,
**not** a FLIX bundle marker; resync at `retw.n`). The honest wall is then stated plainly:
**there is no shipped NCFW LX disassembler config anywhere in the corpus**, so the native
`xtensa-elf-objdump` *will* mis-decode every NCFW-interior instruction stream, and every
NCFW-interior instruction-mix percentage is a decode artifact, not a real FLIX layer.

Confidence tags follow [the Confidence & Walls Model](../reference/confidence-model.md):
`OBSERVED` = a byte / string / config field / disassembler output read from a shipped
artifact present here; `CARRIED` = OBSERVED in prior analysis and reused
at its original confidence (one inheritance step from the binary — flagged because the NCFW
firmware images themselves are **not in this checkout**); `INFERRED` = reasoned over those.
Crossed with `HIGH` / `MED` / `LOW`. Callouts: **QUIRK** (counter-intuitive but real),
**GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE**
(orientation).

> **GOTCHA — provenance split, read this first.** The NCFW *device firmware* (`libncfw.so`
> and the four IRAM/DRAM images it carries) is **not present in this corpus checkout**. What
> *is* present and **OBSERVED** here: (a) the `ncore2gp` Xtensa config
> headers and the whole co-shipped `XtensaTools`/`ncore2gp` tree, (b) the native
> `xtensa-elf-objdump` and its FLIX mis-decode reproduced on a crafted blob, (c) the
> config-inventory negative. Every *firmware-byte* fact (the `0x24` handler bytes, the
> dispatch anchors, the helper-bank decodes, the IRAM/DRAM SHAs and sizes, the host
> `get_image` selector) is **CARRIED** from sibling carve work that *did* have
> the binary in its checkout. The distinction is tagged on every claim below.

---

## 0. The two cores in one diagram

```
  GPSIMD per-NeuronCore Xtensa estate — TWO distinct cores, ONE shared base ISA:

  ┌─────────────────────────── ncore2gp — Vision-Q7 NX "Cairo" ───────────────────────────┐
  │  THE USER DATAPATH CORE (custom-op kernels run here)                                    │
  │  XCHAL_HAVE_VISION=1  VISION_TYPE=7(Q7)  VISION_SIMD16=32 (512-bit)                      │
  │  XCHAL_MAX_INSTRUCTION_SIZE=32  INST_FETCH_WIDTH=32  ← 256-bit FLIX bundles, ivp_* SIMD  │
  │  windowed + density + loops + L32R + MUL16/32  (FLIX3=0 flag, but VISION carries FLIX)   │
  │  >>> THE ONLY Xtensa config that ships. THE SOURCE OF THE MIS-DECODE. <<<                │
  └─────────────────────────────────────────────────────────────────────────────────────────┘
                       │  shares the windowed+density BASE ISA  (so base ops
                       │  decode under either config — only the FLIX layer differs)
                       ▼
  ┌─────────────────────────────── NCFW — scalar Xtensa-LX ─────────────────────────────────┐
  │  THE COLLECTIVE-FIRMWARE MANAGEMENT CORE (characterized on THIS page)                     │
  │  base 24-bit core ISA + windowed AR (XEA2) + 16-bit density + zero-overhead loops + L32R  │
  │  NO Vision/SIMD · NO MAC16 · NO coprocessor/TIE · NO FLIX/VLIW                             │
  │  ships as 4 raw IRAM+DRAM images in libncfw.so .rodata, get_image(arch_id)-selected       │
  │  role: park (waiti 15) → wake on sema → dispatch cc_op → drive DMA + sema rendezvous       │
  │  >>> NO config ships for THIS core anywhere → ncore2gp mis-decodes its op0=e/f bytes <<<   │
  └─────────────────────────────────────────────────────────────────────────────────────────┘
```

One-line verdict: a small **scalar** control core whose body is **2-byte and 3-byte
instructions only**, sharing the windowed base ISA with the Q7 datapath core — which is
*exactly why* base ops decode under either config and only the **nonexistent** NCFW FLIX
layer mis-decodes into bogus 512-bit vector ops.

---

## 1. Core identity — which binary carries NCFW, and the scalar-LX config evidence

### 1.1 The NCFW firmware lives as four raw Xtensa images in `libncfw.so .rodata`

The host x86-64 library `libncfw.so` (SONAME `libncfw.so.2.31.1.0.cf13a49f`) embeds the
device NCFW firmware as **raw, non-ELF** Xtensa IRAM(code)+DRAM(data) memory images in
`.rodata` — one IRAM+DRAM pair per generation, plus a `u32 *_size` word per blob. There is
no ELF header and no embedded load-address header: the host DMAs each image to the device
IRAM/DRAM base, and the base is implied by the selector contract, not carried in the blob.
The carved inventory `[HIGH/CARRIED]`:

| gen | codename | kind | file-off in `.so` | size (B / hex) | IRAM sha256 (carved) |
|---|---|---|---|---|---|
| v2 | SUNDA | IRAM | `0x06a140` | 43232 / `0xa8e0` | `e379980b7ec3f2fe…` *(2.2× larger)* |
| v2 | SUNDA | DRAM | `0x066a60` | 14016 / `0x36c0` | `ca01951124e505b6…` |
| v3 | CAYMAN | IRAM | `0x079860` | 19392 / `0x4bc0` | `d7bc8b814b03c1f0…` *(reference SoC)* |
| v3 | CAYMAN | DRAM | `0x074a40` | 19968 / `0x4e00` | `2418ab0f6350ce93…` |
| v4 | MARIANA | IRAM | `0x083260` | 19488 / `0x4c20` | `ed8eed3429da3834…` |
| v4 | MARIANA | DRAM | `0x07e440` | 19968 / `0x4e00` | `1c3ac5f445865844…` |
| v4+ | MARIANA_PLUS | IRAM | `0x08ccc0` | 19488 / `0x4c20` | `abc4d4521dd857ab…` |
| v4+ | MARIANA_PLUS | DRAM | `0x087ea0` | 19968 / `0x4e00` | `1c3ac5f445865844…` *(byte-identical to MARIANA DRAM)* |

Two structural facts fall straight out of the table:
**MARIANA and MARIANA_PLUS share a byte-identical DRAM** (same sha256 `1c3ac5f4…`) and a
*code-only* IRAM delta (the v4→v4+ images diverge at byte 4497) — i.e. M+ is a feature-flag
refresh, not new silicon. And **SUNDA's IRAM is ~2.2× larger** than the others — it is the
older, structurally-different monolith (§4.1). The IRAM heads are live `j` reset/window
vectors: v2/v3 lead with `06 76 …` / `86 77 …`; v4/v4+ with `06 7d …` / `86 7e …`.

> **NOTE — codename ↔ arch_id binding.** `arch_id = coretype − 1`, with a +8 generation
> stride: `0x05`=v2 SUNDA (coretype 6), `0x0c`=v3 CAYMAN (13), `0x14`=v4 MARIANA (21),
> `0x1c`=v4+ MARIANA_PLUS (29), `0x24`*=v5 MAVERICK (37, *arch_id INFERRED). This is the
> same axis [the IRAM-image page](../collectives/ncfw/ncfw-iram-images.md) keys on.
> `[HIGH/CARRIED]`

### 1.2 The host selector `libncfw_get_image(arch_id)` — and the MAVERICK-absent proof

The image is chosen by `libncfw_get_image` (`.text 0x1179`, fully disassembled in the carve)
via a `cmpl` ladder on `arch_id` `[HIGH/CARRIED]`:

```c
// out = struct { void *iram; u64 iram_size; void *dram; u64 dram_size; }
int libncfw_get_image(uint32_t arch_id, struct img *out) {
    if (!out) return 22;                              // EINVAL
    if (arch_id == 0x1c) { *out = mariana_plus; return 0; }   // v4+
    if (arch_id >  0x1c) return 2;                    // <- ANY > 0x1c (incl. 0x24 MAVERICK) → unsupported
    if (arch_id == 0x14) { *out = mariana;      return 0; }   // v4
    if (arch_id >  0x14) return 2;
    if (arch_id == 0x05) { *out = sunda;        return 0; }   // v2
    else if (arch_id == 0x0c) { *out = cayman;  return 0; }   // v3
    else return 2;                                    // unknown arch_id
}
```

> **CORRECTION — MAVERICK ships no NCFW image.** The `arch_id > 0x1c → return 2` leg is the
> definitive proof: `arch_id 0x24` (= 36 = MAVERICK v5) falls *straight* to the unsupported
> path; there is **no `cmp` against `0x24`** anywhere in the selector. Corroborated by the
> `ctx_log` symbol census — exactly **four** codename symbols
> (`{sunda,cayman,mariana,mariana_plus}_ncfw_ctx_log`) and **zero** `maverick`/`v5` — and by
> `libnrtucode.so` likewise omitting coretype-37. The NCFW management core, like GPSIMD
> itself, tops out at MARIANA_PLUS. `[HIGH/CARRIED]`

### 1.3 The only shipped Xtensa config is `ncore2gp` — the Vision-Q7 datapath core, **not** NCFW

This is the root cause of every prior FLIX mis-read `[HIGH/OBSERVED]`. The
co-shipped Cadence `XtensaTools` registers exactly **one** Xtensa core, and its `core-isa.h`
describes the *Vision-Q7 datapath* core, not a scalar control core. Read from
`extracted/…/tools/ncore2gp/xtensa-elf/arch/include/xtensa/config/core-isa.h`:

| Knob | Value | `core-isa.h` line | What it means |
|---|---|---|---|
| `XCHAL_HAVE_VISION` | **1** | `:206` | Vision SIMD datapath present |
| `XCHAL_VISION_TYPE` | **7** (Q7) | `:208` | Vision **Q7** |
| `XCHAL_VISION_SIMD16` | **32** | `:207` | 512-bit SIMD lane width |
| `XCHAL_MAX_INSTRUCTION_SIZE` | **32** | `:53` | up to 256-bit FLIX bundles |
| `XCHAL_INST_FETCH_WIDTH` | **32** | `:228` | 256-bit instruction fetch |
| `XCHAL_HAVE_FLIX3` | **0** | `:202` | (the *3-way* FLIX flag is off; Vision carries its own FLIX) |
| `XCHAL_HAVE_MAC16` | **0** | `:95` | no MAC16 |
| `XCHAL_HAVE_MUL16` | **1** | `:63` | `MUL16S/U` present |
| `XCHAL_HAVE_MUL32` | **1** | `:64` | `MULL` present |
| `XCHAL_HAVE_WINDOWED` | **1** | `:50` | windowed AR file |
| `XCHAL_NUM_AREGS` | **64** | `:51` | 64 physical AR (16-reg logical window) |
| `XCHAL_HAVE_DENSITY` | **1** | `:55` | 16-bit code density |
| `XCHAL_HAVE_LOOPS` | **1** | `:56` | zero-overhead loops |
| `XCHAL_HAVE_L32R` | **1** | `:67` | `L32R` literal loads |
| `XCHAL_HAVE_CONST16` | **1** | `:69` | `CONST16` |
| `XCHAL_HW_VERSION_NAME` | **"NX1.1.4"** | `:260` | the GPSIMD HW rev |

The `ivp_*` SIMD opcodes on vector regs `v0..v31` this config defines are **impossible on a
scalar control core**. This config is the source of the mis-decode: its FLIX format-length
table makes `op0=0xe` a 16-byte (5-slot Vision) bundle and `op0=0xf` an 8-byte bundle.

**The config-inventory negative** (`--no-ignore` over the whole tools tree) `[HIGH/OBSERVED]`:

| Inventory item | Count in corpus | How verified |
|---|---|---|
| `core-isa.h` files | **1** (the `ncore2gp` one above) | `fd -i core-isa.h extracted/` ⇒ exactly one path |
| registered Xtensa cores | **1** (`ncore2gp`) | `xtensa-elf-objdump` with no `XTENSA_CORE` ⇒ *"no Xtensa core registered as the default"*; only `ncore2gp` resolves |
| `.flix` files | **0** | `fd -e flix extracted/` ⇒ empty |
| `.tie` files | **5** (all generic SDK) | `TIE/lib/TIE/library.tie` + xocl Tests `imap_v{p1,p6,q7,q8}.tie` — none is a registered NCFW core |

The objdump self-reports *"BFD … (GNU Binutils) 2.34.20200201 Xtensa Tools 14.09"*. So there
is **genuinely no NCFW (LX management) core configuration anywhere in the corpus** — no
params, no `core-isa.h`, no `.tie`/`.flix`/`libisa` for it. The five `.tie` files are generic
XtensaTools SDK library/test artifacts (the four `imap_v*.tie` are Vision-OpenCL examples);
`objdump` still lists only `ncore2gp` regardless of them. *This is the irreducible cause of
the decode limit (§5).*

> **GOTCHA — `fd -e tie extracted/` lies.** `fd -e tie extracted/` returns *nothing* because
> `fd` reads `extracted/` as a **pattern**, not a path; the correct form is
> `fd -e tie . extracted/` (pattern `.`, then the search root), which finds all five. A naive
> "zero `.tie` files!" reading is an `fd` arg-order trap, not a corpus fact. The genuine
> negatives are *zero `.flix`* and *one `core-isa.h`*. (See §9 divergence note 1.)

### 1.4 The FLIX mis-decode, demonstrated empirically (the debunk)

Rather than only assert the artifact, a blob was **crafted and disassembled**:
a clean `entry a1,32` (bytes `36 41 00`), an `op0=0x0e` leader byte, an
`op0=0x0f` leader byte, then `retw.n` (`1d f0`), with zero padding so the FLIX decoder can
consume whole bundles. Run with `XTENSA_CORE=ncore2gp xtensa-elf-objdump -D -b binary
-m xtensa` — **verbatim output**:

```
00000000 <.data>:
   0:  364100                            entry a1, 32
   3:  0e000000000000000000000000000000  { bbci.w15 a0,0,0x7; depbits a0,a0,0,1;
                                           ivp_mul4t2n8xr8 wv0,v0,v0,pr0;
                                           ivp_srsnx16 v0,v0,v0;
                                           ivp_sel2nx8i_s4 v0,v0,v0,0 }   <- 16 BYTES (5-slot Vision)
  13:  0f00000000000000                  { const16 a0,0; nop; nop;
                                           ivp_dselnx16t v0,v0,v0,v0,v0,vb0 } <- 8 BYTES
  1b:  1df0                              retw.n
```

The `op0=0xe` byte is **greedily consumed as a 16-byte 5-slot Vision SIMD bundle** — a
512-bit vector multiply right after a function prologue, useless to a DMA/collective control
core — and `op0=0xf` as an 8-byte bundle. **This is the entire mechanism of the "~26–28%
FLIX."** `[HIGH/OBSERVED]`

Apply the **scalar-LX length rule** to the *same bytes* (a small length walker encoding
`op0 0..7 ⇒ 3B core`, `op0 8..d ⇒ 2B density`, `op0 e/f ⇒ 3B
resync`): the `e` leader is consumed in **3 bytes**, the stream re-converges across the
padding, and the `1d f0` lands at `0x1b` as a real **2-byte density `retw.n`** boundary — the
exact resync behaviour the prior carves measured.

> **CORRECTION — "genuine 8-byte FLIX, op0=e→3 slots / op0=f→2 slots" (P-3-225) is REFUTED.**
> P-3-225 read the NCFW image under a *radare2* 8-byte stepping (`op0=e`/`op0=f` both 8 B),
> declared a "FLIX width = 8 bytes (64-bit), two formats, four-way proof," and even relabeled
> the scalar SRs `MEMCTL/MS/ISL/ISB/MPUENB` as "TIE control regs." All three are wrong: the
> `op0=e/f` bytes are **scalar operand/immediate bytes** of 2-/3-byte instructions, not
> bundle leaders (the byte *after* an `e/f` is dominantly a scalar `05` CALLN op0 or the `1d`
> of `1d f0 retw.n` — the fingerprint of a *mis-sync*, not a format selector); the `8-byte`
> read is a desync that *coincidentally* re-lands on some boundaries; and the SRs are all
> standard Xtensa-LX registry entries (§3.3). The scalar `(e3,f3)` rule **wins** the global
> `retw.n` resync decisively over both the `ncore2gp` (`e16,f8`) and the radare2 (`e8,f8`)
> readings (§1.5). `[HIGH — empirical demo + CARRIED]`

### 1.5 Why this is a *real* scalar-LX core — the four-way proof

Four independent lines establish the core *positively* as scalar-LX (each byte-decoded over
the carved blobs; the `retw.n` resync table reproduced *byte-for-byte* by an independent
decoder):

**(a) Width profile.** The only cleanly-decoded instructions are *exclusively* 2-byte and
3-byte (v3: 2B 32% / 3B 68%) — the textbook Xtensa-LX profile (24-bit core ops `op0 0..7` +
16-bit density `op0 8..d`). A genuine FLIX core's body is dominated by wide bundles; NCFW's is
not. The only "8/16-byte instructions" are the `ncore2gp` Vision mis-decodes.

**(b) Boundary resync.** Sweep each IRAM from offset 0, count how many genuine `retw.n`
(`1d f0`) land as an instruction start under three width rules. The scalar `(e3,f3)` rule wins
for v3/v4/v4+ `[HIGH/CARRIED — reproduced byte-for-byte]`:

| gen | scalar (e3,f3) | `ncore2gp` (e16,f8) | radare2 (e8,f8) |
|---|---|---|---|
| v3 | **90/133 (67.7%)** | 66/133 (49.6%) | 76/133 (57.1%) |
| v4 | **101/134 (75.4%)** | 74/134 (55.2%) | 81/134 (60.4%) |
| v4+ | **100/134 (74.6%)** | 69/134 (51.5%) | 83/134 (61.9%) |
| v2 | 26/37 (70.3%) | 27/37 (73.0%) | 32/37 (86.5%) ⚠ |

> **QUIRK — the v2/SUNDA anomaly.** v2 scores higher under the radare2 `(e8,f8)` rule than
> under the scalar rule — but only 37 anchors exist in the 2.2×-larger monolith, so it is
> small-sample noisy. v2 shares the *identical* reset prologue, `0x24` window handler, and SR
> set as v3, so it is the **same core**; the v2 number is a counting artifact of its
> monolithic codegen, not a different ISA. `[HIGH/CARRIED]`

**(c) `e/f`-leader follow-byte.** The `op0=e/f` "leader" bytes are dominantly *followed* by
scalar call/return opcode bytes (`05` = the CALLN `op0`; `1d` = the low byte of `1d f0
retw.n`). In a genuine Vision FLIX bundle, byte0–1 carry the format/slot-select bits and
would **not** be dominated by scalar call/retw opcodes — this is the fingerprint of a scalar
mis-sync (the `e/f` byte is an operand/tail byte).

**(d) Special-register set.** Every decoded `wsr`/`rsr` names a **standard Xtensa-LX SR**
only — no TIE/coprocessor SRs (full table §3.3).

**Conclusion:** NCFW is a scalar Xtensa-LX control core. There is no NCFW FLIX format because
there is no NCFW datapath to feed one.

---

## 2. The windowed-overflow handler at IRAM `0x24`

### 2.1 `WindowOverflow8` — byte-identical across all four generations

IRAM is laid out vectors-first: off `0x0` = `j reset_body`, off `0x6` = `j window/exc
vector`, and off `0x24` = the canonical **WindowOverflow8 / WindowUnderflow8** spill/fill
handler. Decoded by the scalar length rule (`op0 0..7` = 3-byte core ops; the windowed
`l32e`/`s32e` are `op0=0` RRR forms) `[HIGH/CARRIED]`:

```
0x24: l32e a8 ,a1,-64      0x3c: s32e a8 ,a1,-52
0x27: l32e a9 ,a1,-64      0x3f: s32e a9 ,a1,-28
0x2a: l32e a10,a1,-60      0x42: s32e a10,a1,-48
0x2d: l32e a11,a1,-48      ...   (a8..a15 spill/fill sequence) ...
```

`l32e`/`s32e` are the **windowed-exception** load/store: on a window-overflow exception they
spill the rotated register window to its backing store (and `l32e` fills it back on
underflow). Their presence is dispositive — **NCFW has a windowed AR file** (`a0`–`a15`
logical window over a 64-entry physical AR file, `XCHAL_NUM_AREGS=64`) with hardware
window-overflow/underflow exceptions. The handler bytes at `0x24` are **byte-identical** across
SUNDA / CAYMAN / MARIANA / MARIANA_PLUS — the single hardest proof that all four images are
the *same* scalar-LX core, and the per-generation ISA-stability ground truth.

Annotated reconstruction of the spill half (windowed-exception store of the live window):

```c
// WindowOverflow8 handler @ IRAM 0x24 — entered on a window-overflow exception.
// a1 here is the *interrupted* frame's stack pointer; the window has already rotated.
// s32e aN, a1, off  ==  store aN to the caller's save area at (a1 + off), exception form.
void WindowOverflow8(void) {            // hardware vector, NOT a windowed call (no `entry`)
    // spill the eight caller-saved window regs a8..a15 to the backing store below a1:
    s32e(a8,  a1, -16);   //  the standard Xtensa-LX 8-register overflow layout
    s32e(a9,  a1, -12);   //  (exact displacements per the decoded bytes above)
    s32e(a10, a1, -32);
    s32e(a11, a1, -28);
    s32e(a12, a1, -24);
    s32e(a13, a1, -20);
    s32e(a14, a1, -48);
    s32e(a15, a1, -44);
    rfwo();               // return-from-window-overflow: retry the faulting access
}
```

> **NOTE — what "byte-identical across four gens" buys a reimplementer.** It pins (i) the
> register-window discipline (8-register overflow granularity ⇒ `call8` is the dominant call
> form, §3.1), and (ii) that the *exception/vector* layer is frozen across SUNDA→MARIANA_PLUS
> — only the *collective* code bodies differ per gen. The handler is your fixed point: if
> your reimplementation's `0x24` bytes diverge across gens, your window model is wrong.

### 2.2 Why the scalar rule is the correct decoder here — and its honest limit

The window handler, the reset/exc vectors, the `entry`/`retw.n`/`call8`/`call0`/`l32r`
skeleton, the dispatch read, the idle loop, and the leaf helper bank all decode **cleanly**
under the scalar rule (§3, §4). But the `e/f = 3-byte` rule is a **statistical boundary
heuristic, not an exact per-instruction length**: the `op0=e/f` bytes are overwhelmingly
operand/immediate bytes of scalar 2-/3-byte instructions, not leaders.

> **GOTCHA — `op0=e` is not uniformly 3 bytes at the instruction level.** Between the verified
> `entry a1,80` @`0x3bb0` (3 B → `0x3bb3`) and the verified
> `extui a9,a2,8,7` @`0x3bb5` sit *exactly two* bytes (`5e 10`) — so the op at `0x3bb3`
> (`op0=0xe`) is consumed in **2 bytes** on that path. The `e/f=3B` rule **wins** the *global*
> `retw.n` resync because 3 is the modal scalar width, but for any single dense body it will
> still lock onto operand bytes as false leaders. Treat `op0=e/f` as "do not anchor here"
> (operand), not as a fixed 3-byte op. The exact `e/f` leader ops **cannot be named** without
> NCFW's own TIE config (none ships). `[HIGH/CARRIED]`

---

## 3. Calling convention + memory model

### 3.1 The windowed ABI (XEA2)

NCFW uses the standard Xtensa **windowed** ABI `[HIGH/CARRIED]`:

- **Prologue** `entry a1,N` rotates `WINDOWBASE` and reserves an `N`-byte frame on `a1` (SP).
  Frame sizes observed (v3 histogram): `N ∈ {32×40, 48×10, 64×3, 80×2, 96×2}`.
- **Args** in `a2`–`a7`; on `call8` the callee sees them as `a10`–`a15` (the standard windowed
  +8 rotation). `call0` is the windowless variant (no rotation). Census (v3 / v4):

  | form | v3 | v4 | role |
  |---|---|---|---|
  | `entry` | 64 | 71 | windowed prologue |
  | `retw.n` | 102 | 105 | windowed return |
  | `call8` | 290 | 314 | windowed call (+8 rotation) — the dominant form |
  | `call0` | 202 | 149 | windowless call |
  | `callx8` | 15 | — | indirect windowed call |
  | `l32e` | 8 | — | window-overflow spill |
  | `s32e` | 11 | — | window-overflow fill |

- **Epilogue** `retw.n` (`1d f0`) un-rotates the window and returns. Window-overflow/underflow
  exceptions spill/fill the physical AR file via the `0x24` handler (§2).

This is the **same windowed ABI shape** the Q7 datapath core uses (`entry`/`retw.n`/
`xthal_window_spill`) — the two cores *share* the windowed base ISA, which is precisely why
base ops decode under either config and only the nonexistent NCFW FLIX layer mis-decodes. The
difference is the datapath: NCFW is scalar control; Q7 is Vision. See
[The XEA2/windowed ABI on the Q7 side](boot-reset.md) for the datapath core's `entry`/window
priming.

### 3.2 Memory model — L32R literal pools, `memw`-fenced CSR access, MPU + stack limit

- **L32R literal pools.** 436–503 `L32R` literal loads per gen pull 32-bit constants from a
  pool. Some literals point off-image (`0xfffeXXXX`) = an off-image mask-ROM region the
  static IRAM *references but does not contain*; the concrete `soc_addr` integers the thunks
  load live there and in the runtime-populated DRAM, not in the static image.
  `[HIGH/CARRIED count; LOW off-image integers]`

  Annotated L32R literal-pool addressing (PC-relative load of a 32-bit constant):

  ```c
  // L32R aT, label   ==   aT = *(uint32_t*)((PC & ~3) + (signed_imm16 << 2));
  //   the imm16 is NEGATIVE-only: the literal pool sits BEFORE the referencing code,
  //   so L32R reaches BACKWARD into the pool. CONST16 is the forward/immediate alt
  //   (e.g. the dispatch-table base load `const16 a2,0xB0` @0x3bf8, §4.1).
  uint32_t l32r(uint32_t pc, int16_t imm16) {     // imm16 < 0 always
      uint32_t lit_addr = (pc & ~3u) + ((int32_t)imm16 << 2);
      return *(volatile uint32_t *)lit_addr;       // a `soc_addr` CSR pointer, or 0xfffeXXXX off-image
  }
  ```

- **SP-relative frames** via `l32i.n`/`s32i.n` (density) and `l32i`/`s32i` (24-bit).
- **Fenced CSR access.** *Every* CSR read in a wait and *every* CSR write in a signal is
  bracketed by `memw` (the ordering barrier) — confirmed at the instruction level in the
  helper bank (§4.2). The image's **one** `extw` (full barrier) is in the idle loop; **one**
  `waiti 15` per image. Per-gen barrier census: `memw` 47/47/47 (v3/v4/v4+) vs **405** in v2
  (the monolith fences far more); `extw` 1 and `waiti15` 1 in *every* image.

### 3.3 The special-register set — standard Xtensa-LX only (no TIE)

Every decoded `wsr`/`rsr` names a standard Xtensa-LX SR `[HIGH/CARRIED]`:

| SR# | name | role |
|---|---|---|
| `0x00` | `LBEG` | zero-overhead-loop begin (`wsr.lbeg` @v3 `0x2623`) |
| `0x28` | `PREFCTL` | prefetch control |
| `0x48` | `WINDOWBASE` | window rotation base (`wsr.wb` in boot) |
| `0x5a` | `MPUENB` | MPU region enable (a memory-protection unit is present) |
| `0x5f` | `ERACCESS` | (v4 only @`0x4813`) |
| `0x61` | `MEMCTL` | memory/cache control *(P-3-225 wrongly called this "TIE 0x61")* |
| `0xc0` | `IBREAKC0` | instruction-breakpoint control (v4 @`0x3b40`) |
| `0xe5` | `MS` | memory sequence/mode *(P-3-225 wrongly "TIE 0xe5")* |
| `0xe6` | `PS` | processor state (arms windowed ABI / INTLEVEL) |
| `0xe7` | `VECBASE` | relocatable vector base |
| `0xe8` | `EXCCAUSE` | exception cause |
| `0xeb` | `PRID` | processor ID (`rsr` @`0x10ce`/`0x1118` — die/rank self-id) |
| `0xec` | `ISB` | interrupt-stack base *(P-3-225 wrongly "TIE 0xec")* |
| `0xee` | `EXCVADDR` | exception virtual address (`rsr` at exc entry `0x6c`) |
| `0xf8` | `ISL` | instruction/interrupt **stack-limit** (HW stack-overflow protection) *(P-3-225 wrongly "TIE")* |

> **CORRECTION — these are not TIE/coprocessor SRs.** P-3-225 read `MEMCTL/MS/ISL/ISB/MPUENB`
> as "TIE coprocessor control state," which would imply a coprocessor/datapath. They are all
> standard Xtensa SR-registry numbers (and `ncore2gp` itself names them as such). The
> `MPUENB` (MPU) + `ISL` (stack limit) + `VECBASE` (relocatable vectors) + up-to-7 INTLEVEL
> profile is that of a **small privileged management/control core**, not a DSP datapath —
> reinforcing the scalar-LX verdict. `[HIGH/CARRIED; role char. MED/INFERRED]`

### 3.4 ISA family / extensions

**Confirmed HIGH** (config-independent decodes): base 24-bit core ISA (`j`/branch/`call*`/
`l32i`/`s32i`/`movi`/`extui`/`const16`/`addx2`/`addx4`/…); Code Density (`l32i.n`/`s32i.n`/
`mov.n`/`movi.n`/`retw.n`/`add.n`/`addi.n`); Windowed Registers; Zero-Overhead Loops (`loop`/
`loopnez`/`loopgtz` with `LBEG`/`LEND`/`LCOUNT`, in-range `LEND` targets); `L32R`/`CONST16`.

**Present under the `ncore2gp` proxy** (`[MED]` — that config is only a *proxy* for NCFW's
true option vector): `MUL16` (`mul16s/u`), `MUL32` (`mull`), booleans (`andb`/`orb`/`xorb`),
`SEXT`, `CLAMPS`, `MIN`/`MAX`, `NSAU`, `depbits`. **Absent:** `MAC16` (`ncore2gp` itself has
`MAC16=0`). The narrower option vector cannot be *proven* in NCFW's silicon without NCFW's own
params — UNKNOWN at the config level. `[CARRIED]`

---

## 4. The collective-control role — how NCFW drives the SDMA/RDMA path

This section ties NCFW into the collective lane, stating **only** what the recoverable spine +
helper bank support and flagging every step-level binding that crosses the un-disassemblable
case-body interior.

### 4.1 The dispatch spine (the recoverable control skeleton)

The control skeleton decodes cleanly under the scalar rule, byte-confirmed against the carved
v3 image `[HIGH/CARRIED — anchors byte-exact]`:

- **IDLE.** A tiny function whose body is `entry; extw; …; waiti 15; j <back>` — park at max
  INTLEVEL, wake on a notification. `waiti 15` occurs **exactly once** per image (v3 @`0x4b6c`,
  bytes `00 7f 00`; back-edge `c6 fa ff` `j 0x4b5e`).
- **Command/event vector (A).** The `DRAM+0x00` vector is four IRAM code addresses — three
  distinct **trampolines** (each `call8` into a command handler) + a fourth slot `== [0]` (the
  "unknown command → default" guard). v3 `{0x1399, 0x13b1, 0x13c5, 0x1399}`, byte-identical
  v3/v4/v4+ (`1399: 25 2e 00` `call8 0x167c`, etc.); v2 `{0x1bb3, 0x1bcf, 0x1be3, 0x1bb3}`.
- **Algo dispatch.** Inside the *largest* function (v3 @`0x3bb0`, `0xd28` B) the loop reads a
  12-entry IRAM-code-address jump table at `DRAM+0xB0`, indexed by a 4-bit `algo_type` nibble:

  ```
  3bb0: 36 a1 00   entry   a1, 80          ; main loop prologue
  3bf8: 24 b0 00   const16 a2, 0xB0        ; a2 = 0xB0 = byte offset of the DRAM+0xB0 table
                                           ;          (this const16 0xB0 occurs ONCE in the image)
  3bfb: 20 23 a0   addx4   a2, a3, a2      ; a2 = (algo_type a3)<<2 + 0xB0
  3bfe: 58 02      l32i.n  a5, a2, 0       ; a5 = table[algo_type]  (the case-label IRAM address)
  ```

  ```c
  // The dispatch read, reconstructed:
  uint32_t handler = *(uint32_t *)(DRAM_base + 0xB0 + (algo_type & 0xF) * 4);
  goto *handler;   // a computed goto into a case label inside the main function
  ```

  The 12 targets are case labels *inside* the main function — a **staggered computed-goto**
  (entries at +7/+7/+7/+3 strides; three command codes share one entry, a Duff's-device
  fallthrough): `idx5/6/7 → 0x3c08 —+7→ idx4 0x3c0f —+7→ idx2 0x3c16 —+7→ idx1 0x3c1d —+3→
  idx0 0x3c20`. Each 7-byte stride is one scalar step `[<op0=e/f op, leader> + <arith op, 3B>]`
  — e.g. `0x3c0b: sub a8,a8,a0`, `0x3c12: mul16u a10,a8,a0` (the reduce/index math decodes
  cleanly; the leading `e/f` op does not). The nibble selects RING, MESH, or HIERARCHICAL.

> **QUIRK — v2/SUNDA has no dispatch table.** SUNDA is *structurally different*: **no
> `DRAM+0xB0` table**, a monolithic loop (23 funcs vs v3's 57) — the older 2.2×-larger body.
> Its (A) vector still exists (`{0x1bb3,…}`) but the algo dispatch is inlined, not
> table-driven. `[HIGH/CARRIED]`

### 4.2 The leaf primitives the algorithms compose (the rendezvous substrate)

The region `0x3100..0x36f0` is a **bank of ~41 tiny windowed helper functions** (41
`entry a1,N` prologues, 32 `retw.n`) that the case bodies *call*, and it decodes **cleanly**
under the scalar-LX rule. It is the semaphore-primitive library — the device side of the
collective's `PollSem`/`DmaTrigger` lowering `[HIGH/CARRIED]`:

**WAIT (spin-poll)** — `memw; l32i.n aV,[a10+0]; <cond-branch back to the memw>`. Load the CSR
at the pointer in `a10`, compare to the target, spin until released. Byte-exact (v3):

```
3498: c0 20 00   memw                 ; ordering barrier before the CSR read
349b: 28 0a      l32i.n a2,a10,0      ; a2 = *(sema CSR)      (a10 = CSR pointer)
349d: 27 b3 f7   bgeu   a3,a2,0x3498  ; if (target a3 >= a2) spin back to the memw
34a0: 1d f0      retw.n               ; fall through when val a2 exceeds target a3
```

**Ten** such primitives, the full family `{wait-ne, wait-lt/le, wait-ge}` in two register
banks (`a2/a3` and `a5/a4`), byte-stable **10/10/10** across CAYMAN/MARIANA/MARIANA_PLUS. The
comparison op (`op0=7` reg-reg branch) gives the exact variant: `bne`@`0x343c` (wait-ne),
`bltu`@`0x347c` (wait-lt), `bgeu`@`0x3498` (wait-ge), … This is the device side of the host
`add_semaphore_wait_ge_and_dec` / `_wait_eq` / `_wait_ge` op set.

**SIGNAL (fenced CSR store)** — `memw; s32i.n a4,[a10+0]`. Write the value in `a4` to the
semaphore CSR. **Five** genuine fenced CSR-write signals (v3 @`0x3113`/`0x31c7`/`0x3203`/
`0x323f`/`0x3669`), separated from six frame-spill `s32i` by base-register classification
(`a1` = stack spill; any other = CSR/descriptor write). This is the device side of
`add_semaphore_inc`/`_set` and the host `nec_inc_semaphore`.

> **CORRECTION — SIGNAL count is 5, not 11.** An earlier partial counted "11 SIGNAL
> primitives (`memw; s32i*`)." Classifying every `memw`-fenced `s32i.n` by its **base
> register** shows only **5** target a CSR (`[a!=a1]`); the other **6** target the stack frame
> (`[a1]`) and are the spill halves of the §atomic-snapshot quads, *not* signals.
> `[HIGH/CARRIED]`

**ATOMIC SNAPSHOT** — three byte-identical quads (v3 @`0x3684`/`0x36ac`/`0x36d4`):
`memw; l32i.n a2,[a10+0]; memw; s32i.n a2,[a1+12]; memw; l32i.n a3,[a10+4]; memw; s32i.n
a3,[a1+8]` — a fully-fenced read of two adjacent CSR words (a 64-bit semaphore/counter pair)
into the local frame. `[HIGH/CARRIED; "64-bit pair" INFERRED-STRONG]`

### 4.3 How NCFW drives the data plane (the role, without over-claiming)

Per the collective lane, the host runtime never posts a wire opcode: it **builds** the `cc_op`
collective program (the load-time `SELECT → COMPOSE → EMIT` rewrite), DMAs it onto the core,
and rings a doorbell. The NCFW core then, per algorithm step `[CARRIED; per-step schedule MED]`:

1. **Allocates** DMA engines from the static `dma_alloc_bitmap` into a runtime
   `dma_engines_bitmap`;
2. **Reprograms** each engine's standing `pring` ring CSRs in place (base/hi/len);
3. **Triggers** them with one APB-broadcast write to the `BCAST_UDMA` aperture (the fabric
   fans it to the masked engine group) + the per-step tail-pointer write;
4. **Rendezvouses** via the §4.2 WAIT-GE poll / SIGNAL-inc on the `EVT_SEM` channel
   semaphores, bracketed by the NEFF counted device barrier.

The **data** itself moves on the path NCFW *sequences but does not execute*: each ring step
lowers to one SB2SB collective leg — opcode `0xBF`, the POOL/Q7 iDMA `rdma_desc_gen` (builds
the SDMA BD ring + local/remote sema descriptors) + `rdma_desc_start` (the M2S/S2M
tail-pointer doorbell), moving SBUF→remote-SBUF bytes and folding on reducing legs via the
SDMA CCE descriptor. **NCFW is the control/orchestration core for the SB2SB collective hop;
the Q7 POOL engine and the SDMA/CCE blocks are the data plane.** The host all-reduce
(reduce-scatter + all-gather ring 2R1W, kangaring NR1W, hier intra-ring/inter-mesh) is
executed on-device by composing exactly the three §4.2 leaf primitives around the
(un-decodable) reduce/index/DMA-reprogram arithmetic.

### 4.4 The per-generation orchestration split

| gen | collective orchestration | NCFW firmware shape |
|---|---|---|
| SUNDA (v2) | cross-die RDMA legs (`gen`/`start` + P2P sendrecv) + pseudo-ops, but **not** the on-chip `0xBF` SB2SB reduce-copy leg | structurally reduced **monolith**, no `DRAM+0xB0` table |
| CAYMAN (v3) | full reference: pseudo-ops + `0xBF` SB2SB + 12-entry dispatch + 108-event mesh | ring/mesh/hier firmware drives per-step peer-select + counted barrier |
| MARIANA (v4) ≡ MARIANA_PLUS (v4+) | **collective-identical** to CAYMAN; M+ adds 0 new collective ISA bytes | feature-flag refresh; own NCFW image (code-only delta on byte-identical DRAM) |
| MAVERICK (v5) | `0xBF` + `gen`/`start` unchanged, but d2d transport re-IPs to native UCIe (vs CAYMAN's PCIe-derived `io_d2d`); sync fabric widens | **no NCFW image ships** — the collective is driven differently |

Tier: `SUNDA ⊂ {CAYMAN ≡ MARIANA ≡ MARIANA_PLUS} ⊂ MAVERICK` (transport/sync re-model).
`[HIGH/CARRIED]`

### 4.5 The NCFW ↔ TOP_SP relationship — stated carefully

> **NOTE — do not over-claim physical co-residence.** The per-NeuronCore collective program is
> rooted in `libncfw` at the key `ncfw_ctx_top_sp` (+ a `run_state` byte) — an OBSERVED/SPOT
> rooting fact. The reading that the NCFW LX firmware *physically executes on* the TOP_SP NX
> sequencer (i.e. NCFW **is** the per-TOP_SP program) is **INFERRED-STRONG/MED**, not
> byte-provable: it crosses the un-disassemblable LX case bodies. What *is* solid: NCFW is a
> separate scalar-LX core **image**, one per generation, `get_image(arch_id)`-selected, whose
> per-TOP_SP context carries the `cc_op` program. Whether that core is physically the TOP_SP
> NX core in a separate firmware mode, or an adjacent dedicated management core, **cannot be
> settled from the shipped artifacts**. `[INFERRED/MED]`

---

## 5. The honest decode-limit statement

**What the scalar-LX rule recovers** (HIGH/CARRIED in the sources; ~70–79% of bytes for
v3/v4/v4+):

- the reset/window/exc vectors (`j` targets); the **WindowOverflow8 handler at `0x24`** (§2);
  the windowed ABI skeleton (`entry`/`retw.n`/`call8`/`call0`); `l32r`/`l32i.n`/`s32i.n`;
  `const16`/`extui`/`addx4`; the SR boot sequence; `waiti`/`extw`/`memw`;
- the **dispatch spine**: the (A) command vector, the `DRAM+0xB0` 12-entry `algo_type` table +
  the `const16 0xB0; addx4; l32i.n` read, the staggered computed-goto entries, the idle
  `waiti 15` loop (§4.1);
- the **leaf primitive helper bank**: the 10 `memw`-fenced WAIT spin-polls, the 5 fenced
  SIGNAL stores, the 3 atomic CSR snapshots (§4.2) — byte-stable across
  CAYMAN/MARIANA/MARIANA_PLUS.

**What needs a real config** (the irreducible residual):

- the `op0=e/f`-dense **case-body interiors** (the ring `0x3c..` / hier-barrier `0x3e..`
  clusters) do not linearize even under the correct scalar rule: the `e/f` bytes are scalar
  operand bytes; a linear sweep locks onto them as false leaders and emits spurious
  out-of-image call targets (e.g. `call0 0xc900` when v3 IRAM is only `0x4bc0` bytes — flagged
  and refused by the walker). The body instruction stream is **irreducibly partial**;
- the **per-step semaphore schedule** (which §4.2 wait helper, on which CSR, for which target,
  in which order, per ring/mesh/hier step) lives in that interior + in the runtime-populated
  firmware DRAM — not decodable;
- the exact `op0=e/f` **leader instructions cannot be named** — they require NCFW's own
  Tensilica configuration (params / `core-isa.h` / `.tie`), and **none of it ships anywhere in
  the corpus** (§1.3). The `e/f = 3-byte` rule is a *statistical* boundary heuristic (modal
  scalar width), not an exact per-instruction length.

> **CORRECTION — there is no hidden FLIX layer to recover.** NCFW is **fully** scalar-decodable
> as to its *identity, register/memory model, calling convention, dispatch spine, and
> rendezvous primitives.* The only genuine limit is the **missing NCFW disassembler config**,
> which leaves the per-step case-body *schedule* MED and the `e/f` leader ops *un-nameable*. Any
> "X% FLIX" figure measured by pointing `ncore2gp` at NCFW is a **decode artifact**, not a real
> VLIW layer. Do not budget a FLIX issue port for this core. `[HIGH — §1.4 empirical + CARRIED]`

---

## 6. Genesis check — no TONGA (V1) NCFW image

TONGA (V1, the pre-unified Trn1/Inf1-era part) has no coretype/arch_id/runtime presence in the
unified stack and no GPSIMD Vision-Q7 POOL engine — GPSIMD is a v2+ feature. Consistently,
`libncfw` ships **no TONGA NCFW image**: the `get_image` ladder (§1.2) compares only
`{0x05, 0x0c, 0x14, 0x1c}`. The NCFW collective-firmware management core, like GPSIMD itself,
is a **v2+ (SUNDA-onward)** construct. `[HIGH/CARRIED]`

---

## 7. Reimplementer's checklist

To rebuild a Vision-Q7-compatible GPSIMD engine, the NCFW core obligates you to:

1. **Provision a second, scalar Xtensa-LX core** (windowed AR, XEA2, base + density + ZOL +
   `L32R`/`CONST16`; **no** Vision/SIMD, **no** MAC16, **no** TIE, **no** FLIX). Do **not**
   reuse the Q7 datapath config for it. `[HIGH]`
2. **Install the `WindowOverflow8`/`Underflow8` handler at IRAM `0x24`** (`l32e`/`s32e`
   `a8..a15`), byte-frozen across generations. The window granularity is 8 (so `call8`
   dominates). `[HIGH]`
3. **Boot it privileged**: program `VECBASE`, `MPUENB`, `ISL` (stack limit), `PS` (windowed +
   INTLEVEL ≤ 7), then park in `extw; waiti 15; j back`. `[HIGH]`
4. **Implement the dispatch spine**: an (A) command vector of `call8` trampolines + a
   `DRAM+0xB0` 12-entry `algo_type` jump table read by `const16 0xB0; addx4; l32i.n` into a
   staggered computed-goto (or a monolithic inlined loop for the SUNDA-class part). `[HIGH]`
5. **Provide the semaphore-primitive bank**: 10 `memw`-fenced WAIT spin-polls (`{ne,lt,ge}` ×
   two register banks), 5 fenced SIGNAL stores (`memw; s32i.n a4,[a10]`), 3 atomic 64-bit CSR
   snapshots. *Every* CSR access is `memw`-fenced. `[HIGH]`
6. **Wire the data-plane handoff**: alloc DMA engines → reprogram `pring` CSRs → APB-broadcast
   trigger to `BCAST_UDMA` → rendezvous on `EVT_SEM`; the bytes move on the `0xBF` SB2SB leg
   (`rdma_desc_gen`/`_start`) you sequence but do not execute. `[MED — the per-step schedule]`

---

## 8. Cross-references

- [NCFW IRAM Images + Host Selector](../collectives/ncfw/ncfw-iram-images.md) *(Part 10)* — the
  four carved images, offsets/sizes/SHAs, and the `get_image` selector in full.
- [NCFW DRAM Images + `ctx_log` Decoder](../collectives/ncfw/ncfw-dram-ctx-log.md) *(Part 10)* —
  the runtime-populated DRAM the `soc_addr` integers live in.
- [NCFW Main Dispatch Loop](../collectives/ncfw/main-dispatch-loop.md) *(Part 10)* — the (A)
  vector, the `DRAM+0xB0` table, and the idle loop at full depth.
- [Ring + Kangaring](../collectives/ncfw/ring-kangaring.md),
  [Mesh](../collectives/ncfw/mesh-collective.md), and
  [Hierarchical](../collectives/ncfw/hierarchical-collective.md) collectives *(Part 10)* — the
  algorithms the `algo_type` nibble selects.
- [NCFW DMA Reprogram + APB Broadcast + Alloc Bitmap](../collectives/ncfw/dma-reprogram-apb-bcast.md)
  and [pring Descriptors](../collectives/ncfw/pring-descriptors.md) *(Part 10)* — the data-plane
  handoff (§4.3).
- [NCFW LX-ISA / arch_id-Diff / Orchestration Synthesis](../collectives/ncfw/lx-isa-naming-archid-synthesis.md)
  *(Part 10)* — the consolidated ISA/naming/per-gen synthesis.
- [Boot / Reset Sequence + Startup Config](boot-reset.md) — the Q7 *datapath* core's reset
  spine and the shared windowed ABI (the contrast core).
- [The SIMD Compute-Datapath](simd-datapath.md) and [The FLIX Co-Issue Matrix](co-issue-matrix.md)
  — the *real* FLIX/VLIW layer (Q7), the one NCFW does **not** have.
- [The SEQ firmware subsystem](../firmware/seq/boot.md) *(Part 5)* — the per-engine sequencer
  firmware the collective program coordinates with.

---

## 9. Verification ledger

| # | claim | how grounded | verdict |
|---|---|---|---|
| 1 | NCFW is a **scalar Xtensa-LX** core, distinct from the Q7 datapath core | `ncore2gp core-isa.h` is unambiguously the *Vision-Q7* config (`VISION=1`/`TYPE=7`/`SIMD16=32`/`MAC16=0`, lines `:206/:208/:207/:95`); the config-negative ⇒ no NCFW config ships; the firmware body is 2/3-byte only | **HIGH/OBSERVED** (config) + **HIGH/CARRIED** (body width) |
| 2 | The "~26–28% FLIX" is a **decode artifact**, not a VLIW layer | crafted `op0=e/f` blob → `ncore2gp` objdump emits a 16-byte 5-slot `ivp_*` Vision bundle + an 8-byte bundle; scalar rule reconverges to `retw.n` on the same bytes | **HIGH/OBSERVED** (empirical demo) |
| 3 | `WindowOverflow8` handler at IRAM `0x24`, byte-identical across 4 gens | `l32e`/`s32e a8..a15` decoded by the scalar length rule; byte-identical SUNDA/CAYMAN/MARIANA/MARIANA_PLUS | **HIGH/CARRIED** (libncfw not in this checkout) |
| 4 | Windowed XEA2 ABI (`entry`/`retw.n`/`call8`/`call0`, frames `{32..96}`, +8 rotation) + standard-LX SR set, no TIE | census `call8` 290/314, `call0` 202/149, `entry` 64/71, `retw.n` 102/105; SR table all standard registry | **HIGH/CARRIED** |
| 5 | Collective-control role: dispatch spine + sema primitives + `0xBF` SB2SB handoff; NCFW sequences, does not move data | dispatch read `const16 0xB0; addx4; l32i.n` byte-exact; 10 WAIT / 5 SIGNAL / 3 snapshot helpers; SB2SB `0xBF` data plane | **HIGH/CARRIED**; TOP_SP co-residence **MED/INFERRED** |
| 6 | Config-inventory negative: 1 `core-isa.h`, 0 `.flix`, only `ncore2gp` registers | `fd`/`objdump` over `extracted/` | **HIGH/OBSERVED** |
| 7 | MAVERICK (v5) ships no NCFW image | `get_image` ladder has no `cmp 0x24`; 4 `ctx_log` codename symbols, zero v5 | **HIGH/CARRIED** |

### Corrections and divergences recorded on this page

1. **`.tie` inventory — checkout-local divergence.** A prior count cited "**5** `.tie` files
   (generic SDK)"; an initial `fd -e tie extracted/` returned **zero** — but that was an `fd`
   arg-order trap (`extracted/` parsed as a *pattern*). The correct `fd -e tie . extracted/`
   confirms the **5** files (`library.tie` + the 4 `imap_v*.tie`), matching that count. The
   genuine config-negatives are *one* `core-isa.h` and *zero* `.flix` (§1.3 GOTCHA).
2. **IRAM/DRAM file offsets.** A prior trailing-comment paraphrase mixed IRAM/DRAM
   offsets (`0x6a120/0x74a20/0x79840`); §1.1 here uses the **precise carve table**
   (`0x06a140` SUNDA IRAM, `0x079860` CAYMAN IRAM, …) as primary.
3. **SIGNAL primitive count: 11 → 5.** An earlier partial over-counted by treating
   frame-spill `s32i.n a*,[a1]` as signals; base-register classification yields **5** genuine
   CSR-write signals (§4.2 CORRECTION).
4. **P-3-225 "genuine 8-byte FLIX" + "TIE control SRs" — REFUTED.** The `op0=e/f` bytes are
   scalar operands (not bundle leaders); the SRs `MEMCTL/MS/ISL/ISB/MPUENB` are standard
   Xtensa-LX registry entries, not TIE (§1.4, §3.3 CORRECTIONs).
5. **`op0=e` is not a fixed 3-byte width.** The `e/f=3B` rule is a *statistical* `retw.n`
   resync heuristic (a verified 2-byte `op0=e` span exists @`0x3bb3`), not an exact length
   (§2.2 GOTCHA).
