# The GPSIMD CPUs: 8-core Xtensa ELF Layout

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22. The images are `neuronxcc/data/custom_op/libbuiltincustomop_cpu{0..7}.stripped.so` — eight Tensilica Xtensa ELF executables, one per GPSIMD core. The cpu0 image is byte-identical across the cp310/cp311/cp312 wheels (md5 `2f8d136633e3f09e6674fd9e5f0aa65f`). Every virtual address on this page is the link base plus a fixed offset; the per-core link base is `0x84000000 + id·0x200000`. Other wheels differ — treat every address as version-pinned.*

## Abstract

The "GPSIMD CPUs" that run NKI builtin custom-ops are not a SIMD lane array and not part of the BIR ISA — they are **eight Tensilica Xtensa DSP cores**, each running its own copy of a stripped, statically-linked Xtensa ELF. The eight images ship under `data/custom_op/` as `libbuiltincustomop_cpu0..cpu7.stripped.so`. Each is 579,380 bytes, `ELF 32-bit LSB executable, Tensilica Xtensa, statically linked, stripped`, built with `XtensaTools-14.09 clang version 10.0.1`. This page reconstructs their **memory map**: the per-core 2 MiB aperture, the segment/section layout, the entry point, and the proof — by direct byte-diff — that the eight images are one program re-linked at eight bases with zero per-core baked constants.

> **NOTE — there are no recoverable Xtensa code bodies.** This page is grounded in **ELF structure, segment/section headers, a byte-for-byte diff between the eight per-core images, and `.rodata`/`.data` strings — not in decompiled logic.** No Xtensa disassembler was available in this extraction: host binutils has no Xtensa backend (`objdump -i | rg xtensa` → empty), and IDA's auto-analysis recovered only **2** trivial functions with **0** decompiled bodies (`total_functions=2, decompiled=0, flirt=null` in the cpu0 sidecar metadata). The only named entry IDA recovered is the ELF `start` at `0x8400cd94`. Every claim about *what the code does* (the cpu_id derivation, the merge orchestration, the window-mapped DMA) is therefore **INFERRED from strings and structure**, tagged accordingly, and never presented as observed instruction-level behaviour. What *is* CONFIRMED here is the layout: the headers, the sizes, the entry, and the diff.

This page is the structural foundation for Part 11. The **op-runtime ABI** that these images implement (arg parsing, the dtype map, the `data_scratch_map_t` contract) is documented on the [SDK-ABI page (11.3)](customop-cpu-abi.md). The **multicore SORT/TopK** algorithm these cores execute, and the merge-tree topology, are covered on [the bitonic SORT/TopK page (11.2)](bitonic-sort-topk.md); the two-GPSIMD name collision is settled in full on [the reconciliation page (11.9)](two-gpsimd-reconciliation.md). And critically — this GPSIMD (the Xtensa custom-op substrate) is **a different thing** from the on-chip GPSIMD *engine* documented at [1.13](../arch/gpsimd-engine.md); see the [two-GPSIMD warning](#two-gpsimd-the-pool-engine-alias-vs-the-xtensa-cpus) below before reading further.

For reimplementation, the contract is:

- The per-core aperture law `base(id) = 0x84000000 + id·0x200000` (2 MiB stride), and that the eight windows tile `[0x84000000, 0x85000000)` = 16 MiB exactly.
- The ELF segment and section layout — 2 `PT_LOAD` segments, ten allocated sections — and the constant entry offset `base + 0xcd94`.
- The **identical-modulo-rebase** property: the eight images are one program, re-linked, with every embedded absolute address bumped by `id·0x200000` and **zero** per-core baked constants — so a core cannot read its own id from a literal.
- The runtime cpu_id mechanism: a core learns its identity from the `MEM_WINDOW0_LO` user register, not from anything compiled into the image.

| | |
|---|---|
| **Images** | `neuronxcc/data/custom_op/libbuiltincustomop_cpu{0..7}.stripped.so` |
| **Arch / class** | Tensilica Xtensa, `ELF32` LSB, `EXEC`, statically linked, stripped, `Flags 0x300` |
| **Toolchain** | `XtensaTools-14.09 clang version 10.0.1` (`.comment`) |
| **File size** | 579,380 B each (identical, all 8) |
| **Link base** | `base(id) = 0x84000000 + id·0x200000` (2 MiB stride) |
| **Entry** | `start` @ `base + 0xcd94` (cpu0 `0x8400cd94` … cpu7 `0x84e0cd94`) |
| **Program headers** | 3 (`PT_NULL` + 2 `PT_LOAD`); first LOAD `RWE`, second `RW` (`.bss`) |
| **Section headers** | 12 (10 allocated + `.comment` + `.shstrtab`) |
| **Image span** | `base .. base+0xa03a0` = 656,288 B (≈641 KiB); fits in 2 MiB window |
| **Aperture tiling** | 8 × 2 MiB windows = `[0x84000000, 0x85000000)` = 16 MiB |
| **Byte-diff cpu0↔cpuN** | 12,706 differing bytes, all delta `+N·0x20` (rebase only) |
| **`.rodata` reloc words** | 1,834 differing 32-bit words, all delta `N·0x200000`, **0** non-rebase |
| **cpu_id source** | `READ_LOCAL_UREG64(MEM_WINDOW0_LO)`, runtime — no baked id |
| **Cross-wheel** | cp310/cp311/cp312 cpu0 byte-identical (same md5) |
| **Evidence floor** | `readelf -h/-l/-S`, `cmp -l`, Python 32-bit-word diff, `strings -a` |

---

## Two GPSIMD: the Pool-engine alias vs the Xtensa CPUs

> **GOTCHA — "GPSIMD" names two unrelated things in this codebase.** The neuronx_cc sources use the token *GPSIMD* in two distinct senses, and conflating them produces nonsense.
>
> - **The GPSIMD *engine*** ([1.13](../arch/gpsimd-engine.md)) is an **alias for the Pool engine** inside the BIR/HWM model — a TPB compute engine the scheduler emits ISA instructions for. It is on-chip, it is part of the instruction stream, and it has no separate ELF.
> - **The GPSIMD *CPUs*** (this page, Part 11) are the **eight Tensilica Xtensa cores** that run a software op-runtime — a stripped Xtensa ELF per core — and execute custom-op kernels (SORT, TopK) compiled to Xtensa, *not* to BIR ISA.
>
> They share the name because both are "general-purpose SIMD" surfaces, but they are different silicon blocks, different instruction sets, and different toolchains. Everything below is about the *Xtensa CPUs*. The full reconciliation of the two senses is the subject of [11.9](two-gpsimd-reconciliation.md).

The host-side x86 helper libraries shipped under the separate `aws-neuronx-gpsimd` package (`libnrtucode`, crypto helpers, etc.) are a **third, unrelated** component — host tooling, not the Xtensa op substrate. Do not fold them in either.

---

## The per-core aperture

### Purpose

The eight Xtensa cores do not share an address space for their code. Each core runs from a **private 2 MiB code/data window**, and the eight windows are packed contiguously into a 16 MiB region. The image for a given core is *linked* — non-PIC, absolutely — at that core's window base, so the core's own code, literal pool, and pointer tables all carry absolute addresses inside its window. This is why there are eight distinct `.so` files rather than one shared image: the Xtensa code is position-dependent, so "core 3" needs the program re-linked at core 3's base.

### The aperture law

The per-core base is a single affine function of the core index, CONFIRMED for all eight images by reading the ELF entry (`readelf -h`) and the `.text` section address (`readelf -S`):

```text
base(id) = 0x84000000 + id · 0x200000          stride = 0x200000 = 2 MiB

  id   .text base VA   entry VA (start)   md5
  --   -------------   ----------------   --------------------------------
   0   0x84000000      0x8400cd94         2f8d136633e3f09e6674fd9e5f0aa65f
   1   0x84200000      0x8420cd94         9bd70ccfd186a3ead75ab305bccce0d2
   2   0x84400000      0x8440cd94         83ebb40297c7bdc5fa3401e1c30cb88b
   3   0x84600000      0x8460cd94         c6366e4bb4bd81aa4f3359b13d62ee81
   4   0x84800000      0x8480cd94         85513c265eb6c8d13c9ddaebd0d30828
   5   0x84a00000      0x84a0cd94         2f4cee1f7818cd94257cce074bf41d77
   6   0x84c00000      0x84c0cd94         54941885cd1ad2a88b2c117644b9aa80
   7   0x84e00000      0x84e0cd94         dfef29964f14a7983d2a398f7c808944
```

Two facts fall straight out of this table:

- **Entry is a constant offset.** `entry(id) − base(id) = 0xcd94` for all eight. The ELF `start` (the Xtensa C-runtime entry) sits at the same file offset in every image; only the link base moves it. So the entry point carries no per-core information either — it is `base + 0xcd94`, full stop.
- **The eight windows tile 16 MiB.** The first window starts at `0x84000000`; the eighth starts at `0x84e00000` and (see the next section) every image spans only `0xa03a0` ≈ 641 KiB from its base. So window `id` occupies `[base(id), base(id)+0x200000)` and the union is `[0x84000000, 0x84000000 + 8·0x200000) = [0x84000000, 0x85000000)` = **16 MiB**, packed with no gaps.

> **NOTE — the 0x84… region is the CODE aperture, not the data DRAM window.** Do not confuse the 16 MiB code aperture at `0x84000000` with the **shared DRAM staging window** at `[0x80000, 0x90000)` that the op uses for tensor bytes (asserted at `data_transfer.cpp:160`, `dram_addr >= 0x80000 && dram_addr < 0x90000`). They are disjoint address spaces: `0x84xxxxxx` is the per-core private code/data the image *runs from*; `0x8xxxx` is a 64 KiB DRAM/HBM offset window the runtime maps in for cross-core data. The tensor I/O memory model (the DRAM window, `MEM_WINDOW0`, `SUNDA_APB_BASE`) belongs to the ABI page (11.3); only the code aperture is structural to this page.

### The headroom budget

Each image is far smaller than its window. The span and headroom (computed from the section table, below):

```text
image span  = .bss_end − .text_base = 0x840a03a0 − 0x84000000 = 0xa03a0 = 656,288 B  (≈641 KiB)
window      = 0x200000 = 2,097,152 B  (2 MiB)
headroom    = 0x200000 − 0xa03a0     = 0x15fc60 = 1,441,376 B  (≈1.37 MiB) per core
```

That ≈1.37 MiB of unused window per core is where the running image puts its **stack, heap, and DMA scratch** — none of which is in the file image (`.bss` is `NOBITS`; stack/heap are allocated at runtime). The aperture is sized to hold the static image plus a generous runtime working set with room to spare. [Span/headroom CONFIRMED by arithmetic on the section addresses; the stack/heap *use* of the headroom is INFERRED — no allocator code is disassembled.]

---

## ELF segment and section layout

### Program headers (the loader's view)

`readelf -l` reports **three** program headers — one `PT_NULL` and two `PT_LOAD` — identical in shape across all eight images, rebased only:

```text
  Type   Offset    VirtAddr     FileSiz   MemSiz    Flg   Sections
  ----   --------  -----------  --------  --------  ---   ---------------------------------
  NULL   0x000000  0x00000000   0x00000   0x00000         (empty header slot)
  LOAD   0x000094  0x84000000   0x8d448   0x8d448   RWE   .text .clib.rodata .clib.data
                                                          .eh_frame .ctors .dtors .rodata .data
  LOAD   0x000000  0x8408d480   0x00000   0x12f20   RW    .bss
```

Two load segments, mapped at the link base:

- **LOAD 1 — the on-disk image (`RWE`).** File offset `0x94` (right after the ELF + program headers), `FileSiz == MemSiz == 0x8d448`, permissions read/write/**execute**. It covers everything from `.text` through `.data`. The `RWE` (write *and* execute on one segment) is a Cadence/Xtensa bare-metal idiom, not a hardened OS mapping — there is no `GNU_STACK`, no `GNU_RELRO`, no dynamic segment.
- **LOAD 2 — `.bss` (`RW`).** `FileSiz 0` (occupies no file bytes), `MemSiz 0x12f20` (77,600 B of zero-init RAM), based at `0x8408d480`. The loader zero-fills this at the core's window.

> **QUIRK — these are statically-linked bare-metal Xtensa executables, not shared objects.** Despite the `.so` filename, `e_type` is `EXEC`, there is no `PT_DYNAMIC`, no `PT_INTERP`, no relocation segment, and no symbol table left (stripped). The `.so` suffix is a packaging convention; the file is a fixed-address standalone image. That is precisely *why* there must be eight of them: with no PIC and no runtime relocation, the only way to place the program at core `id`'s base is to re-link it there.

### Section headers (the link-time view)

`readelf -S` reports **twelve** sections; ten are allocated (`A` flag), plus `.comment` and `.shstrtab`. All offsets/addresses are for cpu0; for core `id`, add `id·0x200000` to the `Addr` column (the `Off` column is identical across cores).

```text
  Nr  Name           Type      Addr        Off      Size     Al   Flg   Role
  --  -------------  --------  ----------  -------  -------  ----  ---   ----------------------------------------
   1  .text          PROGBITS  0x84000000  0x00094  0x737d8   32   AX   Xtensa code (473,048 B) — entry+0xcd94
   2  .clib.rodata   PROGBITS  0x84073800  0x73894  0x02004   64   A    Cadence Xtensa C-lib rodata (8,196 B)
   3  .clib.data     PROGBITS  0x84075840  0x758d4  0x00458   64   WA   C-lib writable data (1,112 B)
   4  .eh_frame      PROGBITS  0x84075c98  0x75d2c  0x00004    4   A    4 B — empty CIE/terminator
   5  .ctors         PROGBITS  0x84075c9c  0x75d30  0x00020    4   A    8 words: sentinel + 6 ctors + NULL
   6  .dtors         PROGBITS  0x84075cbc  0x75d50  0x00008    4   A    2 words: sentinel + NULL (0 real dtors)
   7  .rodata        PROGBITS  0x84075d00  0x75d94  0x05a48   64   A    app rodata (23,112 B) — strings, ptr tables
   8  .data          PROGBITS  0x8407b780  0x7b814  0x11cc8   64   WA   app writable data (72,904 B)
   9  .bss           NOBITS    0x8408d480  0x8d4dc  0x12f20   64   WA   zero-init RAM (77,600 B) — counters, flags
  10  .comment       PROGBITS  0x00000000  0x8d4dc  0x00028    1   MS   "XtensaTools-14.09 clang version 10.0.1 "
  11  .shstrtab      STRTAB    0x00000000  0x8d504  0x0004f    1        section-name strings
```

The sections are 64-byte aligned, so there are small alignment gaps between them in the virtual address space (e.g. between `.text` end `0x840737d8` and `.clib.rodata` `0x84073800`). Those gaps are padding, not segments.

> **CORRECTION — there are 2 PT_LOAD segments, not a "9-segment table".** An earlier reconstruction (carried from the IDA `_segments.json` sidecar) listed nine "segments" including several 28–60-byte `LOAD(pad)` entries between the real sections. Those `LOAD(pad)` entries are **IDA's segmentation of the inter-section alignment gaps**, not ELF program headers. The canonical ELF view from `readelf -l` is **two** `PT_LOAD` program headers (the `RWE` image and the `RW` `.bss`); the gaps belong to no segment. The ten *sections* are real; the extra "segments" were a tooling artifact. This page uses the `readelf` ground truth.

### The constructor table

`.ctors` (`0x84075c9c`, 0x20 = 8 words) is a GNU-style `__CTOR_LIST__`: a head sentinel, the constructor pointers, and a NULL terminator. Reading the cpu0 bytes:

```text
  .ctors[0] = 0xffffffff      head sentinel (count marker)
  .ctors[1] = 0x8400db90  ┐
  .ctors[2] = 0x8400e380  │
  .ctors[3] = 0x8401b3c8  │   6 static constructors (run by `start` before main)
  .ctors[4] = 0x84037018  │
  .ctors[5] = 0x84037ecc  │
  .ctors[6] = 0x8404097c  ┘
  .ctors[7] = 0x00000000      NULL terminator

  .dtors = { 0xffffffff, 0x00000000 }   sentinel + NULL → zero real destructors
```

So the section is **8 words wide but holds 6 real constructors** (then a NULL). These six ctors run at image start — they register the in-library ATen/c10 statics and populate the op-name table — before control reaches the SDK main. Their *bodies* are Xtensa and not disassembled; the addresses and the count are CONFIRMED from the `.ctors` bytes, the binding to "ATen + op-table registration" is STRONG (from the `.rodata` strings those bodies reference, not from the code).

---

## Identical modulo rebase: the byte-diff proof

This is the central structural claim and it is provable directly from the bytes, with no Xtensa decoding. The eight images are **one program, re-linked at eight bases**. There are no per-core baked constants — not a cpu_id, not a per-core scale, nothing. The entire difference between any two images is the rebase of absolute addresses.

### Step 1 — the diff is uniform and small

`cmp -l` between cpu0 and any other image yields exactly **12,706** differing byte positions, and the per-byte delta is a single constant equal to `N·0x20`:

```text
  cmp cpu0 vs cpu1 : 12,706 differing bytes, every delta = +0x20   (1 · 0x20)
  cmp cpu0 vs cpu7 : 12,706 differing bytes, every delta = +0xE0   (7 · 0x20)
  → the SAME 12,706 byte positions differ in cpu1 and in cpu7 (verified set-equal)
```

The differing byte is the **bits-21..28 byte** of a 32-bit absolute address — the byte that changes when you add `N·0x200000` to a `0x84xxxxxx` pointer (`0x200000` is a `1` in bit 21, i.e. `+0x20` in the third byte). The first differing byte is at file offset `0x1a`, which lies inside the ELF header's `e_entry` field `[24,28)` — confirming the entry rebase from the bytes themselves:

```text
  e_entry  cpu0 = 0x8400cd94   →   cpu1 = 0x8420cd94   (Δ = 0x200000)
```

### Step 2 — at 32-bit-word granularity, every diff is exactly a rebase

Diffing `.rodata` (file offset `0x75d94`, size `0x5a48`) word-by-word as little-endian `uint32`:

```text
  .rodata differing 32-bit words (cpu0 vs cpu1) : 1834
  set of word deltas                            : { 0x200000 }     ← exactly one value
  NON-rebase words (delta ≠ 0x200000)           : 0
```

**1,834** pointer words differ, **every one** of them by exactly `0x200000`, and **zero** differ by anything else. Sample relocated pointers (cpu0 value → cpu1 value):

```text
  rodata+0x1c4 : 0x84075fdc → 0x84275fdc    (.rodata self-pointer — e.g. a string-table head)
  rodata+0x1c8 : 0x84039a2c → 0x84239a2c    (.text function pointer)
  rodata+0x1cc : 0x84039a38 → 0x84239a38    (.text function pointer)
  rodata+0x1d0 : 0x8405e648 → 0x8425e648    (.text function pointer)
```

Each differing word is an **absolute pointer** `P` with `value(cpuN) = value(cpu0) + N·0x200000`. They target `.text` (code) and `.rodata` (string/vtable/pointer-table) addresses — exactly what you expect from fully absolute, non-PIC Xtensa linking with no GOT. (The remaining diffs outside `.rodata` are `l32r` literal-pool entries in `.text` and pointer tables in `.data` — same rebase, same `+N·0x200000`.)

### Step 3 — the string *content* is byte-identical

To rule out any per-core textual constant, extract the printable strings from each allocated section and compare the *sets* across cores (filtering out the embedded address bytes that happen to be printable). The content is identical to the byte:

```text
  section        cpu0 strings   cpu7 strings   identical set?
  -------------  ------------   ------------   --------------
  .clib.rodata        16             16             yes
  .rodata            451            451             yes
  .data              717            717             yes
```

(The IDA sidecar reports `total_strings = 1427`, identical across all eight images — consistent with this.) Every assert string, every op-name, every type name is the same in all eight; only the absolute pointers that *reference* them move.

### What this proves about cpu_id

```c
// CONCLUSION (CONFIRMED by the diff): there is NO cpu_id literal in any image.
//
// If a core's id were baked in, two images would differ by that id somewhere —
// a word with delta ≠ N·0x200000, or a string that varies per core. Neither
// exists: the .rodata diff has ZERO non-rebase words, and the string sets are
// byte-identical. Therefore the cpu_id is NOT a compile-time constant.
//
// A core must learn its own id at RUNTIME. The image is wholly id-agnostic
// except for the link base it was relocated to.
```

The runtime mechanism is named in the binary, though its code is not disassembled. `data_transfer.cpp:240` asserts:

```text
  READ_LOCAL_UREG64(MEM_WINDOW0_LO) == SUNDA_APB_BASE
```

A core reads the 64-bit **local user register `MEM_WINDOW0_LO`** (the low base of hardware memory-window #0) and checks it against the compile-time `SUNDA_APB_BASE`. This UREG read is how a core derives/confirms its own memory window — and, combined with `data_transfer.cpp:171`'s `cpu_id < 8`, how the runtime indexes this core's slot among the eight. [The `MEM_WINDOW0_LO` / `cpu_id < 8` strings are CONFIRMED; the *derivation* — that the window register yields the id without a baked literal — is STRONG, forced by the byte-diff result above. The numeric `SUNDA_APB_BASE` value is an Xtensa immediate and is not recoverable from strings: SPECULATIVE.]

> **NOTE — "no baked cpu_id" is a hard, byte-level fact; "id comes from the window register" is the strong inference.** The diff *proves* the negative (no per-core constant exists). The positive mechanism (which register, how the loader seeds it) rests on the `MEM_WINDOW0_LO`/`cpu_id` strings plus the absence of any alternative, because the bodies are Xtensa and unavailable. Tag them apart when reusing this.

---

## How an invocation reaches a core (structure only)

The full dispatch and ABI live on 11.3/11.9; only the *structural* skeleton is in scope here, and only at the level the headers and strings support:

- **Library selection is external, by cpu_id.** There is one image per core. The compiler/runtime side stages `libbuiltincustomop_cpu{0..7}.stripped.so` keyed by cpu_id and loads cpu`id`'s image at `0x84000000 + id·0x200000`. The image itself contains no dispatcher that picks a core — it *is* core `id`'s program. [STRONG, from the per-core link bases + the BIR-side staging documented elsewhere; the in-image absence of a selector is consistent with the stripped 2-function IDA view but not independently proven from code.]
- **Entry is always `base + 0xcd94`.** The Xtensa `start` runs the six `.ctors`, then enters the SDK main. CONFIRMED entry; ctor *count/addresses* CONFIRMED; ctor *bodies* unavailable.
- **The four op entry-point names live in `.data`** as a contiguous NUL-terminated table — `sort_singlecore_compute`, `sort_multicore_compute`, `partial_sort_multicore_compute`, `partial_merge_multicore_compute` (all four CONFIRMED present in every image). They are the funcName keys the BIR side binds to FunctionIds, not separate libraries; every core carries all four. The op chosen inside a core is keyed by FunctionId through a plain function table, *not* a c10 dispatcher (the runtime Dispatcher/OperatorHandle symbols are absent; `noop_dispatch_fn` is the deliberate no-op). [Op names CONFIRMED from strings; the table-vs-dispatcher claim is STRONG, from the string surface, not from disassembly.]

The multicore SORT/TopK these names implement — the per-core bitonic sort, the 3-level pairwise merge tree, the `doSyncPhysicalCores` barrier over the shared DRAM window — is reconstructed from assert strings and cross-wave evidence (no Xtensa bodies) and is documented on 11.9. It is deliberately **not** re-derived here: this page stops at the ELF layout.

---

## Evidence and confidence

| Claim | Tag | Anchor |
|---|---|---|
| 8 Xtensa ELF32 images, 579,380 B each, `Flags 0x300`, stripped | CONFIRMED | `file`, `readelf -h` all 8 |
| `base(id) = 0x84000000 + id·0x200000`, stride 2 MiB | CONFIRMED | `readelf -h`/`-S` entry + `.text` addr, all 8 |
| entry = `base + 0xcd94` for all 8 | CONFIRMED | `readelf -h` e_entry, all 8 |
| 8 windows tile `[0x84000000, 0x85000000)` = 16 MiB | CONFIRMED | base table + 0xa03a0 span |
| image span `0xa03a0` (≈641 KiB), headroom `0x15fc60` (≈1.37 MiB) | CONFIRMED | `.bss` end − `.text` base |
| 2 `PT_LOAD` (RWE image + RW `.bss`); no DYNAMIC/INTERP/RELRO | CONFIRMED | `readelf -l` |
| 10 allocated sections (`.text`…`.bss`) + `.comment` + `.shstrtab` | CONFIRMED | `readelf -S` |
| toolchain `XtensaTools-14.09 clang 10.0.1` | CONFIRMED | `.comment` |
| `.ctors` = sentinel + 6 ctors + NULL; `.dtors` = 0 real | CONFIRMED | `.ctors`/`.dtors` bytes |
| identical-modulo-rebase: 12,706 byte diffs, all `+N·0x20` | CONFIRMED | `cmp -l` cpu0↔cpu1/cpu7 |
| `.rodata`: 1,834 word diffs, all `0x200000`, **0** non-rebase | CONFIRMED | Python uint32 diff |
| string *content* byte-identical per section across cores | CONFIRMED | per-section printable-string set compare |
| cp310/cp311/cp312 cpu0 byte-identical | CONFIRMED | md5 across wheels |
| **no cpu_id baked into any image** | CONFIRMED | the zero-non-rebase diff + identical strings |
| cpu_id read at runtime from `MEM_WINDOW0_LO` | STRONG | `data_transfer.cpp:240`/`:171` strings; bodies absent |
| 6 ctors register ATen + op-name table | STRONG | ctor addrs + `.rodata` strings; bodies absent |
| op selected by FunctionId via plain table, no dispatcher | STRONG | string surface (Dispatcher symbols absent) |
| `SUNDA_APB_BASE` numeric value | SPECULATIVE | Xtensa immediate, not in strings |
| **Xtensa instruction-level behaviour (any)** | NOT RECOVERABLE | no Xtensa disassembler; IDA `decompiled=0` |

**Hard limit (restated):** the `cpu{0..7}.so` Xtensa code is not disassembled (`total_functions=2`, `decompiled=0`, `flirt=null`). Everything on this page is ELF structure, the cross-image byte-diff, and `.rodata`/`.data` strings. No claim here rests on a recovered Xtensa instruction sequence, and none is fabricated to look like one.

*Backing analysis: D-AC06 (per-CPU link layout + multicore merge topology), D-Q04 (the bitonic SORT/TopK builtin), D-Q05 (the `extended_isa::sdk` custom-op CPU ABI). Cross-refs: [1.13 GPSIMD engine = Pool-engine alias](../arch/gpsimd-engine.md); [11.3 SDK custom-op ABI](customop-cpu-abi.md); [11.2 the bitonic SORT/TopK builtin + merge tree](bitonic-sort-topk.md); [11.9 two-GPSIMD reconciliation](two-gpsimd-reconciliation.md).*
