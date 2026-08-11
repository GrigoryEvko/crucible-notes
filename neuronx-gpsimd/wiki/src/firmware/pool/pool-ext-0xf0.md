# POOL Extended-Opcode (`0xF0`) Dispatch

This page **owns and fully characterizes** the GPSIMD **POOL engine's** handling of
the **extended instruction**, base opcode `0xF0`. It is the *second level* of the
POOL dispatch: the top-level [`kernel_info_table`](kernel-info-table.md) scan in the
[POOL main dispatch loop](pool-dispatch.md) matches base opcode `0xF0`, then
sub-selects on the **spec byte**. The deliverable here is the **spec → handler
sub-dispatch table** — which, as the binary proves, is **not a separate `switch`
statement** but **five rows of the same `kernel_info_table`**, one per spec value
(`0,1,2,4,3`) — plus the `cptc_decode_impl<1..6>` codec-kernel template family and the
selector that picks among its six specializations.

The POOL core runs on the Vision-Q7 NX `ncore2gp` "Cairo" datapath core
(`XCHAL_HAVE_VISION = 1`, `XCHAL_VISION_TYPE = 7` — the FLIX/VLIW layer;
`XCHAL_HAVE_FLIX3 = 0` is **not** "scalar"). All disassembly below is the native
Cadence `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`); the scalar-LX rule decodes the
*different* NCFW management core and is wrong here. POOL-core DEBUG logs carry the
`'P%i:'` / `'P%d:'` prefix (vs the SEQ engine's `'S:'`), the cleanest discriminator
between the two firmwares.

> **NOTE — the objects used.** The carve source is the static archive
> `libnrtucode.a`. The **PERF** dispatcher + tables + five `0xF0` trampolines are in
> member `img_CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_contents.c.o`; the embedded device SO is
> the section-`.rodata` payload (symbol `CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get.data`,
> 41,568 B). Carved, it is a **32-bit Tensilica-Xtensa ELF**,
> **sha256 `910d41c3…b4b55527`**, `e_entry 0x01005610`, `.text` VMA `0x01000000` at
> file off `0x100` (`file_off = VMA − 0x01000000 + 0x100`). The Cptc family
> (`cptc_decode_impl<1..6>`, the `0xE4` dispatcher, the `0xF0`/spec-7 path) is in the
> sub-image `img_CAYMAN_Q7_POOL_PERF_EXTISA_3_SO_contents.c.o`, embedded SO
> **sha256 `052ac31c…`** (26,996 B, `e_entry 0x01003c74`). The `ExtendedInst*` /
> `cptc_decode` log strings are baked into the **DEBUG** build's DRAM image
> (`img_CAYMAN_Q7_POOL_DEBUG_DRAM_contents.c.o`, `.rodata` payload), used here purely
> as name/order anchors — the PERF build strips them. Cross-image stability checked
> against `img_MARIANA_Q7_POOL_PERF_EXTISA_0_SO_contents.c.o` (carved SO, 41,568 B).

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte/string read from the shipped image; `INFERRED` =
reasoned over OBSERVED facts (often across a FLIX/literal-pool desync); `CARRIED` =
consolidated from a cited cross-page anchor at its original confidence. Crossed with
`HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a
reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE**
(orientation).

> **GOTCHA — two opcode spaces, do not conflate.** The Xtensa `entry`/`const16`/
> `callx8`/`retw.n` you read in the disassembly are the *engine's own instruction
> set*. The `0xF0` base opcode and its **spec** sub-byte are fields of the
> *interpreted POOL-microcode instruction word* — a separate encoding the engine
> decodes. The `kernel_info_table` maps **(opcode, spec) → Xtensa handler address**;
> it is not an Xtensa decode table.

---

## 0. The extended dispatch in one diagram

```
 POOL microcode instr word  (base opcode = 0xF0, plus a "spec" sub-byte)
        │
        │  main dispatcher @ 0x01005610  (entry a1,32; FLIX-scheduled scan)
        │  builds packed key = opcode<<24 | spec<<16
        ▼
   ┌─────────────────────────────────────────────────────────────────────────┐
   │  LINEAR SCAN of kernel_info_table (17 rows × 8 B, VMA 0x02000380)         │
   │  match the packed (opcode,spec) key  →  load funcVA at row+4  →  callx8   │
   └─────────────────────────────────────────────────────────────────────────┘
        │   (a 0xF0 instr matches EXACTLY ONE of the five 0xF0 rows by its spec)
        ▼
   spec 0 → 0x01003370  ExtendedInstEngineNop           (no-op; entry; movi a2,0; retw.n)
   spec 1 → 0x01003380  pool_extended_inst_copy()        (== .xt.prop start, EXACT)
   spec 2 → 0x01003484  → callx8 0x010034b0              decode_extended_inst_tensor_tensor_arith(bool,uint)
   spec 4 → 0x010037a8  state 0x0200046c → decode_pool@0x01000b90   (Rand/Cptc band)
   spec 3 → 0x01003a60  state 0x02000470 → decode_pool@0x01000b90   (Rand/Cptc band)

   miss (0xF0 + unregistered spec) → "UNKNOWN EXTENDED OPCODE=%d"   (decimal: the spec)
   miss (no opcode match at all)   → "UNKNOWN OPCODE=0x%x"          (hex: the opcode)
```

> **CORRECTION — there is no in-loop `0xF0` special case.** A naive reading expects a
> branch like `if (opcode == 0xF0) dispatch_extended_inst(spec)`. The binary has **no
> such branch**. The "two-level" dispatch is realized **entirely by the
> `kernel_info_table`**: opcode `0xF0` is **registered five times** with five distinct
> spec bytes. One linear scan over the packed `(opcode, spec)` key lands a `(0xF0,
> spec)` instruction on exactly one of the five rows. "`dispatch_extended_inst`" is the
> DEBUG/source *family name* of these handlers (it prints `"P%i: dispatch_extended_inst(%d)
> : num_chans = %0d"`, `%d` = the spec), **not** a single switch routine — the five
> handlers do not converge to one address.

---

## 1. The packed dispatch key and how the spec is read

The `kernel_info_table` is a dedicated section in the carved SO (not buried in
`.data`):

```
readelf -SW cayman0.so
  [ 7] kernel_info_table PROGBITS  02000380  007400  000088  WA   (17 rows × 8 B)
```

Each row is **8 bytes**: a 4-byte little-endian **key** followed by a 4-byte little-
endian **funcVA**. The key encodes the opcode and spec exactly as the SEQ-vs-POOL
contrast pins it (`opcode<<24 | spec<<16`; see
[SEQ Decode / Dispatch Hub](../seq/dispatch-hub.md) for the contrasting SEQ scheme).
Because the table is little-endian, the on-disk key bytes read
`[mid_lo][mid_hi][spec][opcode]`; for the extended rows `mid = 0`, so they appear as
`00 00 <spec> f0`:

```
xxd -s 0x7430 -l 0x30 cayman0.so       (rows 6..11, around the 0xF0 band)
00007430: 0000 00f0 7033 0001 0000 01f0 8033 0001   ← spec 0 → 0x01003370 ; spec 1 → 0x01003380
00007440: 0000 02f0 8434 0001 0000 04f0 a837 0001   ← spec 2 → 0x01003484 ; spec 4 → 0x010037a8
00007450: 0000 03f0 603a 0001 0000 0052 403b 0001   ← spec 3 → 0x01003a60 ; (next row: 0x52)
```

The **spec byte is the third byte of the key** (key bits `[23:16]`). The main
dispatcher composes `opcode<<24 | spec<<16` from the decoded microcode word, then the
**same scan** that distinguishes any two opcodes also distinguishes the five `0xF0`
rows by their spec — no extra logic. This is why the "sub-opcode read beyond base
`0xF0`" is simply "the spec byte at key offset `+0x02`, compared as part of the one
packed key."

> **NOTE — extraction is in the FLIX body, not byte-recovered.** The exact bit-field
> by which the front-end peels the **spec** out of the 32-bit microcode word, and the
> exact `callx8 funcVA` bundle in the scan loop, live inside the main dispatcher's
> FLIX-scheduled body (`entry a1,32` at `0x01005610`, then VLIW bundles such as
> `{ s16i …; nop; nop; ivp_dselnx16t … }`) that objdump's linear sweep cannot fully
> resequence. The **key format**, the **table geometry**, and the **five resolved
> funcVAs** are OBSERVED-HIGH; the precise extraction shift is INFERRED from the key
> layout. `[HIGH key+rows / MED extraction shift]`

### The complete 17-row table (re-decoded byte-exact)

Re-parsing all 136 table bytes (`opcode = key>>24`, `spec = (key>>16)&0xFF`):

| idx | opcode | spec | funcVA | role |
|----:|:------:|:----:|:-------|:-----|
| 0 | `0x7e` | 0 | `0x01000080` | (non-extended) |
| 1 | `0x7c` | 0 | `0x010003f8` | (non-extended) |
| 2 | `0x7d` | 0 | `0x01000410` | (non-extended) |
| 3 | `0x45` | 0 | `0x01000b90` | `decode_pool` entry trampoline |
| 4 | `0x51` | 0 | `0x0100105c` | (non-extended) |
| 5 | `0x41` | 0 | `0x01000f1c` | (non-extended) |
| **6** | **`0xf0`** | **0** | **`0x01003370`** | **ExtendedInstEngineNop** |
| **7** | **`0xf0`** | **1** | **`0x01003380`** | **ExtendedInstCopy = `pool_extended_inst_copy()`** |
| **8** | **`0xf0`** | **2** | **`0x01003484`** | **ExtendedInstTensorTensorArith** |
| **9** | **`0xf0`** | **4** | **`0x010037a8`** | **Rand/Cptc band (state `0x0200046c`)** |
| **10** | **`0xf0`** | **3** | **`0x01003a60`** | **Rand/Cptc band (state `0x02000470`)** |
| 11 | `0x52` | 0 | `0x01003b40` | (non-extended) |
| 12 | `0x46` | 0 | `0x010040c0` | (non-extended) |
| 13 | `0x47` | 0 | `0x01004160` | (non-extended) |
| 14 | `0xbe` | 0 | `0x01004204` | (non-extended) |
| 15 | `0xf2` | 0 | `0x0100484c` | (non-extended) |
| 16 | `0x7b` | 0 | `0x01004dc4` | (non-extended) |

> **COUNT — table census.** `python3` parse of `kernel_info_table` (file off `0x7400`,
> `0x88` B): **17 total rows**, of which **5 carry opcode `0xF0`** (specs
> `{0,1,2,4,3}`). Metric: 8-byte rows with `(key>>24)&0xFF == 0xF0`.

> **QUIRK — registration order is `0,1,2,4,3`, not sorted.** The five `0xF0` rows are
> stored in *registration* order (`0,1,2,4,3`), so **spec 4 precedes spec 3** in the
> table. A linear key-scan is order-independent, so this is harmless to dispatch — but
> a reimplementation that assumes the table is sorted by `(opcode,spec)` and binary-
> searches it would mis-locate spec 3/4. Scan linearly, or sort first.

---

## 2. The five `0xF0` spec handlers — per-handler disassembly

`file_off = VMA − 0x01000000 + 0x100`. Each funcVA was disassembled freshly
re-anchored (`xtensa-elf-objdump -D -b binary -m xtensa --adjust-vma=<VA>`) to avoid
carry-over `retw.n` / 2-byte FLIX desync. The trampolines are FLIX-scheduled with an
interleaved literal-pool word, so the byte-exact *interior* desyncs; the prologue, the
`const16` state-pointer load, and the `const16`+`callx8` route target are recoverable
and reported. Desync spans are flagged and **never invented**.

### spec 0 — `0x01003370` — `ExtendedInstEngineNop`

```
 1003370:  36 41 00     entry   a1, 32
 1003373:  0c 02        movi.n  a2, 0
 1003375:  1d f0        retw.n
```

A complete, clean, **three-instruction no-op that returns 0**. No state pointer, no
`callx8`, no channel work. This is the exact signature of the DEBUG variant that
logs only `"ExtendedInstEngineNop : processing complete"` — the **only** variant with
no `"Decode : …"` line and no `num_*` work line (§4).

### spec 1 — `0x01003380` — `pool_extended_inst_copy()` (EXACT)

```
 1003380:  36 41 00     entry   a1, 32     (then FLIX/literal body — DESYNC)
            …
 100338b:  { bbsi.w15 …; ivp_labvdcmprs2nx8_xp …; ivp_dmulq2n8dxr8 …; ivp_dselnx16t … }  ← real FLIX copy body
```

The funcVA `0x01003380` **is** the function start of
`.xt.prop._Z23pool_extended_inst_copyv` — its first prop record reads
`80 33 00 01` = VMA `0x01003380`. So the spec-1 row is **not** a trampoline *to* the
copy kernel; the row's funcVA **is** `pool_extended_inst_copy()` itself
(`c++filt _Z23pool_extended_inst_copyv → pool_extended_inst_copy()`). The recovered
`ivp_*` Vision-Q7 SIMD bundle past the prologue confirms a real copy datapath, not a
stub. DEBUG anchor: `"Decode : ExtendedInstCopy"` / `"ExtendedInstCopy :
num_tensor_elements = %d"`. The per-kernel `.bss` slot it touches is `0x02000468`
(§4), recoverable only from the band map (its `const16` lives in the desynced body).
`[HIGH funcVA / MED exact slot const16]`

### spec 2 — `0x01003484` — `ExtendedInstTensorTensorArith`

```
 1003484:  36 41 00     entry   a1, 32
 1003487:  24 00 02     const16 a2, 0x200
 100348a:  24 68 04     const16 a2, 0x468     ; a2 = 0x02000468  (per-kernel state ptr)
            …(FLIX body — partial DESYNC)…
 10034a4:  24 b0 34     const16 a2, 0x34b0    ; a2 = 0x010034b0  (impl VA)
 10034a7:  e0 02 00     callx8  a2            ; call decode_extended_inst_tensor_tensor_arith()
 10034aa:  0c 02        movi.n  a2, 0
 10034ac:  1d f0        retw.n
```

> **NOTE — the `const16` pair idiom.** Xtensa `const16 aR, imm16` computes
> `aR = (aR << 16) | imm16`. So `const16 a2,0x200 ; const16 a2,0x468` materializes the
> 32-bit literal `0x02000468` in two halves (verified arithmetic); `const16 a2,0x34b0`
> is similarly preceded by a `0x0100`-band high half to form `0x010034b0`. Where
> **both** halves of a pair are clean, the literal is HIGH; where only one `const16`
> survived the FLIX desync, the *band* (`.bss 0x0200046x` / `.text 0x01003xxx`) is HIGH
> but the exact low half is MED.

The `callx8` target `0x010034b0` is the function start of
`.xt.prop._Z40decode_extended_inst_tensor_tensor_arithbj` — first prop record
`b0 34 00 01` = VMA `0x010034b0`, with a verified clean `entry a1,32` prologue there.
`c++filt → decode_extended_inst_tensor_tensor_arith(bool, unsigned int)`. So spec-2
is a one-`callx8` route into the 64-bit-aware tensor-tensor arith decoder; DEBUG anchor
`"ExtendedInstTensorTensorArith : num_tensor_elements = %d : alu_op = 0x%x"`. The
underlying ALU and 64-bit paths are characterized in
[ext-tensor-tensor-arith](../kernels/ext-tensor-tensor-arith.md) and
[tensor-tensor-64bit](../kernels/tensor-tensor-64bit.md). `[HIGH route]`

### spec 4 — `0x010037a8` — Rand/Cptc band (via `decode_pool`) `[MED]`

```
 10037a8:  36 61 00     entry   a1, 48       (LARGER 48-byte frame — the only one of the five)
 10037ab:  4f 00 6f …                        <literal-pool / FLIX preamble; DESYNC marker>
 10037b3:  44 6c 04     const16 a4, 0x46c     ; per-kernel state slot 0x0200046c (.bss)
            …(FLIX body — partial DESYNC)…
 10037d6:  04 90 0b     const16 a0, 0xb90     ; a0 = 0x01000b90 = decode_pool ENTRY trampoline
```

Routes through `decode_pool@0x01000b90` (the same entry trampoline opcode `0x45`
uses — table idx 3), with its **own** per-kernel state slot `0x0200046c`. The
48-byte `entry` frame (vs the 32-byte frame of the other four) is its only structural
distinction besides the state slot. This is the **Rand/Cptc band** (§5 / §6).
`[MED]`

### spec 3 — `0x01003a60` — Rand/Cptc band (via `decode_pool`) `[MED]`

```
 1003a60:  36 41 00     entry   a1, 32
 1003a63:  4f 00 6f …                        <identical FLIX preamble to spec-4; DESYNC marker>
 1003a6b:  44 70 04     const16 a4, 0x470     ; per-kernel state slot 0x02000470 (.bss)
            …(FLIX body — partial DESYNC)…
 1003a8e:  04 90 0b     const16 a0, 0xb90     ; a0 = 0x01000b90 = decode_pool ENTRY trampoline
```

Byte-structurally near-identical to spec-4 (same `4f 00 6f 79 08 00 c2 42` preamble,
same `decode_pool@0xb90` route), differing only in the state-pointer immediate
(`0x470` vs spec-4's `0x46c`) and one selector byte. `[MED]`

> **GOTCHA — `const16 a0, 0xb90` is a route *setup*, not a call.** Specs 3/4 load
> `0x01000b90` into `a0` (not `a2`/`a8`), then continue in their FLIX body. Whether
> they `callx0 a0` / fall through into `decode_pool`'s machinery, and what selector
> byte distinguishes the named Rand* / Cptc variant once inside, is **inside the
> FLIX-desynced interior and not byte-recovered**. The recovered facts — `entry`
> frame size (48 vs 32), state slot (`0x46c` vs `0x470`), shared `decode_pool` route —
> are OBSERVED; the exact branch is INFERRED. `[MED]`

---

## 3. The uniform trampoline shape — confirmed at `decode_pool@0x01000b90`

Every per-opcode/per-spec handler in this image follows the **same shape**, recovered
*cleanly* at the `decode_pool` entry trampoline (which the spec-3/4 paths route into):

```
 1000b90:  36 41 00     entry   a1, 32
 1000b93:  24 00 02     const16 a2, 0x200
 1000b96:  24 58 04     const16 a2, 0x458     ; a2 = 0x02000458  (decode_pool state slot)
            …(a few FLIX ops; mostly clean here)…
 1000bb0:  24 c0 0b     const16 a2, 0xbc0     ; a2 = 0x01000bc0  decode_pool(bool) IMPL
 1000bb3:  e0 02 00     callx8  a2            ; call decode_pool@0x01000bc0
 1000bb6:  0c 02        movi.n  a2, 0
 1000bb8:  1d f0        retw.n
```

So the trampoline pattern is:

```
entry ; load this kernel's .bss STATE-POINTER slot (0x0200045x/046x/047x) ;
(decode/setup) ; const16 IMPL_VA ; callx8 IMPL_VA ; movi a2,0 ; retw.n
```

`decode_pool`'s impl `0x01000bc0` is the function start of
`.xt.prop._Z11decode_poolb` (first record `c0 0b 00 01`,
`c++filt → decode_pool(bool)`). Spec-2's clean `const16+callx8 → 0x010034b0` and the
spec-3/4 `const16 a0,0xb90` route are the same shape; spec-0 (EngineNop) is the
degenerate case (no state, no `callx8`); spec-1 (Copy) is the case where the funcVA
**is** the impl. `[HIGH shape / MED spec-3/4 exact branch]`

---

## 4. Per-kernel state slots — the `.bss` descriptor band

The `const16` immediates the trampolines load (`0x458`, `0x468`, `0x46c`, `0x470`, …)
are **not** inside `.globstruct`. Section geometry:

```
readelf -SW cayman0.so
  [ 7] kernel_info_table  Addr 0x02000380  Size 0x88   (dispatch table)
  [ 8] .globstruct        Addr 0x02000408  Size 0x48   → ends 0x02000450
  [ 9] .bss               Addr 0x02000450  Size 0x3c   (NOBITS, zero-init)
```

So `0x02000458 … 0x0200047c` are zero-initialised `.bss` slots — one per-kernel
runtime **state/scratch pointer**, written at runtime, read by each kernel's
trampoline:

| `.bss` slot | owner |
|:------------|:------|
| `0x02000458` | `decode_pool` state slot |
| `0x0200045c` | `tensor_tensor_arith` state slot (CARRIED) |
| `0x02000468` | `ExtendedInstCopy` (spec 1) / `decode_extended_inst_tensor_tensor_arith` (spec 2) |
| `0x0200046c` | spec-4 (Rand/Cptc band) state slot |
| `0x02000470` | spec-3 (Rand/Cptc band) state slot |
| `0x0200047c` | `get_sequence_bounds` / dequantize state slot (CARRIED) |

> **NOTE — distinct slots prove distinct kernels.** Each spec handler owning its own
> `.bss` state slot **confirms the five `0xF0` handlers are independent kernels**, not
> one shared switch sharing one descriptor. (`.globstruct` itself, at `0x02000408`,
> magic `0x6099cb34` — LE bytes `34 cb 99 60` — with four `0x00001000` size fields,
> four `0xffffff00` masks, and `0xffffffff` terminators, is the *shared* dispatcher
> state block, distinct from these per-kernel slots.) `[HIGH band / OBSERVED]`

---

## 5. The `cptc_decode_impl<1..6>` template family

> **CORRECTION — the `1..6` is a dtype selector, not the POOL spec.** A tempting
> reading maps the POOL spec byte directly onto the six `cptc_decode_impl<N>`
> specializations. The binary refutes this on two counts. (a) The family **does not
> exist in `cayman0.so`'s `.xt.prop` set at all** — it lives in the `cayman3.so`
> sub-image. (b) The template parameter `<N>` is an **`(unsigned char)` dtype/format
> selector** chosen *inside* the Cptc handler, proven by the demangled signature and by
> the `"unsupported in_dtype/out_dtype for cptc_decode : 0x%x"` error strings.
> `spec ≠ cptc template arg`; do **not** map spec `0/3/4` one-to-one onto `impl<1..6>`.

The six specializations differ **only** in the leading `(unsigned char)` template arg
(`c++filt` of `cayman3.so`'s `.xt.prop._Z16cptc_decode_implILh1EEvj25_TIE_xt_ivp32_xb_vec2Nx8US0_ttthb`):

```cpp
void cptc_decode_impl<(unsigned char)1>(
    unsigned int,
    _TIE_xt_ivp32_xb_vec2Nx8U,        // operand vector A (2N×8-bit)
    _TIE_xt_ivp32_xb_vec2Nx8U,        // operand vector B (2N×8-bit)
    unsigned short, unsigned short, unsigned short,   // 3× u16 geometry/length
    unsigned char,                    // dynamic dtype/mode arg
    bool);
// … <2> … <3> … <4> … <5> … <6>  — identical signature, template arg 2..6
```

The two `2N×8U` vector operands + three `u16` + `u8` + `bool` are a **codec decode
kernel** signature; the `u8` template arg is a *static* dtype/mode specialization. The
six function starts (`.xt.prop` first records in `cayman3.so`):

| spec'n | funcVA |
|:-------|:-------|
| `cptc_decode_impl<1>` | `0x010024b4` |
| `cptc_decode_impl<2>` | `0x01002934` |
| `cptc_decode_impl<3>` | `0x01002bf8` |
| `cptc_decode_impl<4>` | `0x010030cc` |
| `cptc_decode_impl<5>` | `0x010032c8` |
| `cptc_decode_impl<6>` | `0x01003794` |

### What selects among the six

The `cayman3.so` `kernel_info_table` (9 rows @ VMA `0x020008c8`, re-decoded byte-exact):

| idx | opcode | spec | funcVA | role |
|----:|:------:|:----:|:-------|:-----|
| 0 | `0x7e` | 0 | `0x01000080` | — |
| 1 | `0x7c` | 0 | `0x010003f8` | — |
| 2 | `0x7d` | 0 | `0x01000410` | — |
| 3 | `0x45` | 0 | `0x01000b90` | `decode_pool` |
| 4 | `0xbe` | 0 | `0x01000dac` | — |
| 5 | `0xf2` | 0 | `0x010013f4` | — |
| 6 | `0x7b` | 0 | `0x01001964` | — |
| **7** | **`0xe4`** | **0** | **`0x01002258`** | **Cptc decode DISPATCHER** |
| **8** | **`0xf0`** | **7** | **`0x01003b64`** | **Cptc extended-instr entry (spec 7)** |

The `0xE4` handler `@0x01002258` sits **immediately before** the
`cptc_decode_impl<1..6>` block (`0x24b4 … 0x3794`) and has a clean `entry a1,32`
prologue. It is the **CPTC decode dispatcher**: it reads its descriptor, validates
`in_dtype`/`out_dtype` (the `"unsupported in_dtype/out_dtype for cptc_decode"` arms),
then **calls the matching `cptc_decode_impl<N>`** indexed by dtype. The `0xF0`/spec-7
handler `@0x01003b64` is the *extended-instruction* entry into the same Cptc path. So
the **selector** is a two-stage funnel:

1. **handler selection** — the spec byte (or the `0xE4` opcode) picks the **Cptc
   handler** via the `kernel_info_table` row;
2. **specialization selection** — `in_dtype`/`out_dtype` then index `impl<1..6>`
   **inside** that handler.

> **NOTE — exact index arithmetic not byte-recovered.** The precise
> `in_dtype/out_dtype → N` map (e.g. a lookup table vs a computed index) is inside the
> `0xE4`/`0xF0`-spec-7 FLIX-desynced bodies and is reported structurally as
> "dtype-indexed selection over `impl<1..6>`", not byte-exact. Full codec detail is in
> [cptc-codec](../kernels/cptc-codec.md).
> `[HIGH family location & dtype-driven selection / MED exact index arithmetic]`

---

## 6. Reconciliation — the five `0xF0` rows vs the recovered handlers

Re-derived from the table bytes (§1) and the per-handler disassembly (§2):

| spec | funcVA | recovered handler | evidence | conf |
|:----:|:-------|:------------------|:---------|:-----|
| 0 | `0x01003370` | `ExtendedInstEngineNop` | clean no-op `entry;movi a2,0;retw.n` == `"processing complete"` | HIGH |
| 1 | `0x01003380` | `ExtendedInstCopy` = `pool_extended_inst_copy()` | funcVA == `.xt.prop` start (`80 33 00 01`); DEBUG `"ExtendedInstCopy"` | HIGH |
| 2 | `0x01003484` | `ExtendedInstTensorTensorArith` | `const16+callx8 → 0x010034b0` == `.xt.prop decode_extended_inst_tensor_tensor_arith` start (`b0 34 00 01`) | HIGH |
| 4 | `0x010037a8` | Rand/Cptc band | `entry a1,48`; state `0x46c`; route `const16 a0,0xb90` | MED |
| 3 | `0x01003a60` | Rand/Cptc band | `entry a1,32`; state `0x470`; route `const16 a0,0xb90` | MED |

### Why specs 3/4 cannot be pinned 1:1 to a named variant (honest limitation)

The DEBUG build's `ExtendedInst*` string table lists **six** variants in source/enum
declaration order:

```
strings -t x dbg_dram.bin | rg ExtendedInst
  0x1908  ExtendedInstEngineNop : processing complete         ← enum 0
  0x193a  Decode : ExtendedInstCopy                           ← enum 1
  0x198c  Decode : ExtendedInstTensorTensorArith              ← enum 2
  0x1a3e  Decode : ExtendedInstRandGetState                   ← enum 3
  0x1aa0  Decode : ExtendedInstRandSetState                   ← enum 4
  0x1b02  Decode : ExtendedInstCptcDecode : num_active_chans  ← enum 5
```

Three table specs remain after pinning spec-0=EngineNop, spec-1=Copy, spec-2=TTArith:
specs **3 and 4**, plus the implicit absentee. The spec-3/4 PERF trampolines carry
**no name string** (PERF strips them), both route through `decode_pool@0xb90` (so the
named impl is reached through `decode_pool`'s internal switch, which is FLIX-desynced
and not byte-recovered), and the Cptc family proper lives in `cayman3.so`. So the
spec-3/4 → `{Rand*/Cptc}` pairing is **MED**.

> **QUIRK — the suggestive enum-ordinal alignment.** If the table spec value equalled
> the enum ordinal, then spec-0=EngineNop, spec-1=Copy, spec-2=TTArith all hold, and
> the remaining alignment would give **spec 3 → `RandGetState`** and **spec 4 →
> `RandSetState`**, with `CptcDecode` (ordinal 5) *not* a `cayman0.so` `0xF0` row —
> consistent with Cptc living in `cayman3.so` (opcode `0xE4` + `0xF0` spec-7). Because
> the first three specs align positionally and the spec-3/4 trampolines route through
> `decode_pool` with distinct state slots, **spec3=RandGetState / spec4=RandSetState is
> the most probable pairing** — but the table stores specs in registration order
> `0,1,2,4,3` (not a clean ordinal), and the trampolines carry no name pin, so this is
> reported as PROBABLE at MED, **not asserted HIGH**. `[MED/INFERRED]`

---

## 7. Cross-image stability of the spec routing

`mariana0.so` carries the **identical** `0xF0` spec sequence at the same table VMA
(`0x02000380`); its **key column is byte-for-byte identical** to CAYMAN's (verified:
all 17 keys equal), with funcVAs differing only by a small per-build entry-offset
delta:

| idx | row | CAYMAN funcVA | MARIANA funcVA | Δ |
|----:|:----|:--------------|:---------------|:--|
| 6 | `0xf0`/0 | `0x01003370` | `0x01003390` | +0x20 |
| 7 | `0xf0`/1 | `0x01003380` | `0x010033a0` | +0x20 |
| 8 | `0xf0`/2 | `0x01003484` | `0x010034a4` | +0x20 |
| 9 | `0xf0`/4 | `0x010037a8` | `0x010037d8` | +0x30 |
| 10 | `0xf0`/3 | `0x01003a60` | `0x01003a90` | +0x30 |

This proves the spec→handler routing (the five `0xF0` rows in registration order
`0,1,2,4,3`) is **stable across the CAYMAN / MARIANA / MARIANA_PLUS generation** that
ships the EXTISA_0 POOL image.

---

## 8. Error / miss paths

Two miss paths, both DEBUG-string-anchored (PERF strips the strings; the table-scan
behavior is identical):

| condition | string (file off in `dbg_dram.bin`) | format |
|:----------|:-------------------------------------|:-------|
| top-level scan exhausts (no `(opcode,spec)` match) | `"P%i: UNKNOWN OPCODE=0x%x"` (`0xea3`) | **hex** opcode |
| `0xF0` with an unregistered/unimplemented spec | `"P%i: UNKNOWN EXTENDED OPCODE=%d"` (`0xdba`) | **decimal** spec |

> **NOTE — the format difference is itself evidence.** The extended miss path prints
> `=%d` (decimal — a small spec/sub-code integer), while the top-level miss prints
> `=0x%x` (hex — a full opcode). That the extended path formats a *decimal* sub-code is
> corroborating evidence that the spec byte is the extended-dispatch discriminator.

The DEBUG family header `"P%i: dispatch_extended_inst(%d) : num_chans = %0d"` (`0xd87`)
and the module string `"dispatch.hpp"` (`0xde4`) name the handler family and its source
module; the `%d` it logs is the spec/sub-code, the `num_chans` is the per-`get_cpu_id()`
channel partition the resolved variant works over (DEBUG-only telemetry).

---

## 9. Reimplementation checklist

To rebuild the POOL `0xF0` extended dispatch:

1. **Do not special-case `0xF0`.** Register the five rows in your `kernel_info_table`:
   pack `key = opcode<<24 | spec<<16` and store `(key, funcVA)` for `(0xF0,0)`,
   `(0xF0,1)`, `(0xF0,2)`, `(0xF0,4)`, `(0xF0,3)`. One linear key-scan resolves them.
2. **spec 0** → a no-op returning 0 (`EngineNop`).
3. **spec 1** → the copy kernel directly (`pool_extended_inst_copy()`), per-kernel
   state slot in `.bss`.
4. **spec 2** → `decode_extended_inst_tensor_tensor_arith(bool, unsigned)`; build the
   `0x0200_0468` state ptr, then one `callx8` to the impl.
5. **spec 3 / spec 4** → Rand/Cptc band; each loads its own `.bss` state slot
   (`0x470` / `0x46c`) and routes through the shared `decode_pool` machinery.
6. **Cptc** is reached via a *separate* image (opcode `0xE4` or `0xF0`-spec-7); the
   `cptc_decode_impl<N>` (`N` ∈ 1..6) specialization is selected by **dtype**, not by
   spec.
7. Miss handling: print decimal spec for an unknown extended spec, hex opcode for a
   top-level miss.

---

## See also

- [POOL Engine Main Dispatch Loop](pool-dispatch.md) — the
  top-level linear `kernel_info_table` scan this page's five rows sit beneath.
- [kernel_info_table Binary Layout](kernel-info-table.md) — the physical
  8-byte `(key, funcVA)` row format and the full 17-row table.
- [Cptc Codec](../kernels/cptc-codec.md) — the `cptc_decode_impl<1..6>` codec
  internals and the `in_dtype/out_dtype → impl<N>` selection.
- [Extended Tensor-Tensor Arith](../kernels/ext-tensor-tensor-arith.md) — the
  spec-2 `decode_extended_inst_tensor_tensor_arith` body.
- [Tensor-Tensor 64-bit](../kernels/tensor-tensor-64bit.md) — the 64-bit ALU
  paths the extended tensor-tensor arith dispatches into.
- [SEQ Decode / Dispatch Hub](../seq/dispatch-hub.md) — the contrasting SEQ
  computed-jump dispatch (the `opcode<<24 | spec<<16` key contrast originates there).
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the
  OBSERVED/INFERRED/CARRIED × HIGH/MED/LOW tagging used throughout.
