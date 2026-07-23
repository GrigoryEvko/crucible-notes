# `kernel_info_table` Binary Layout

This page is the **canonical reference** for the GPSIMD POOL engine's
**`kernel_info_table`** — the opcode→kernel-function dispatch table baked as its own
ELF section into every GPSIMD Vision-Q7 device image. The [POOL main dispatch
loop](pool-dispatch.md) reads this table on every decoded POOL/extended instruction:
it builds a packed `(opcode, spec)` key, linear-scans the table for a matching row,
and `callx8`s the row's `funcVA` to reach that opcode's kernel. The
[`0xF0` extended-opcode sub-dispatch](pool-ext-0xf0.md) is **five rows of this same
table**, one per spec byte. The entire kernels cluster
([opcode catalog](../kernels/opcode-catalog-ledger.md) and each per-kernel page)
cross-links here for the table's byte layout and per-opcode `funcVA`s.

The deliverable here is exhaustive: the **8-byte entry format** decoded byte-by-byte,
the **full CAYMAN table dump** (every one of the 17 rows, with resolved kernel name),
the **lookup method**, the **entry count proven three independent ways**, and **every
`funcVA` validated against the live instruction stream** and resolved to a named
function.

The POOL core runs on the Vision-Q7 NX `ncore2gp` ("Cairo") datapath core
(`XCHAL_HAVE_VISION = 1`, `XCHAL_VISION_TYPE = 7` — the FLIX/VLIW layer). All
disassembly below is the native Cadence `xtensa-elf-objdump` with
`XTENSA_CORE=ncore2gp`; the scalar-LX decode rule applies to the *different* NCFW
management core and is wrong here.

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte/string read from the shipped image; `INFERRED` =
reasoned over OBSERVED facts (often across a FLIX/literal-pool desync); `CARRIED` =
consolidated from a cited cross-page anchor at its original confidence. Crossed with
`HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a
reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE**
(orientation).

> **NOTE — which image holds the table.** The canonical `kernel_info_table` is in the
> **CAYMAN `EXTISA_0` PERF image**, the embedded Vision-Q7 device SO that also carries
> the POOL dispatcher and all kernel trampolines. It is carved from the host loader
> `libnrtucode_internal.so` (x86-64) as the data behind the getter
> `CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get` (host symbol
> **`CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get.data`** @ host `.rodata` vaddr `0x2ef7e0`,
> size **41,568 B** returned by the getter). The identical bytes also appear as the
> `.rodata` payload of archive member `img_CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_contents.c.o`
> in `libnrtucode.a` (the carve path the [POOL dispatch](pool-dispatch.md) /
> [`0xF0`](pool-ext-0xf0.md) pages used). Both routes yield the same object:
>
> | property | value |
> |---|---|
> | object | `CAYMAN_Q7_POOL_PERF_EXTISA_0` device SO (`== internal_CAYMAN_0.so`) |
> | size | **41,568 B** (`0xA260`) |
> | sha256 | **`910d41c3ededce67cd00ec7041a5e66c3c39536d2e9b16fe21ea019db4b55527`** |
> | ELF | ELFCLASS32, little-endian, `e_machine = 94` (Tensilica Xtensa), `ET_EXEC` |
> | entry | `0x01005610` (the dispatch/main routine that consumes the table) |
> | `.text` map | VMA `0x01000000` at file off `0x100` (`file_off = VMA − 0x01000000 + 0x100`) |
>
> This is a **different image** from the `CAYMAN_NX_POOL` DEBUG firmware the SEQ pages
> used (iram `8e4412b9…` / dram `7bdf6ed7…`): that DEBUG build carries the `'P%i:'`
> runtime log strings; **this PERF image strips them**, so kernel names below come from
> the image's own `.xt.prop.<mangled>` section names, not from runtime strings.

---

## 0. The table in one diagram

```
 kernel_info_table  —  ELF section [7], VMA 0x02000380, file off 0x7400, size 0x88 (136 B), Al 8
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │  17 fixed 8-byte records, no header, no in-band terminator                     │
 │                                                                                │
 │   record i  (8 bytes):                                                         │
 │     +0x00 [0x00] [0x00] [spec] [opcode]   ← key bytes; LE u32 = opcode<<24|spec<<16│
 │     +0x04 [        funcVA (u32 LE)        ]  ← R_XTENSA_RELATIVE, load-relocated │
 │                                                                                │
 │   delimited by: section size  AND  base-getter(0x380)/end-getter(0x408) pair   │
 │   end == .globstruct base (section [8], VMA 0x02000408)                         │
 └──────────────────────────────────────────────────────────────────────────────┘

 lookup (in POOL main dispatch @ 0x01005610):
   key = opcode<<24 | spec<<16
   base = 0x02000380 ; end = 0x02000408 ; count = (end − base) >> 3 = 17
   for (i = 0; i < 17; ++i)            // LINEAR SCAN, 8-byte stride, NOT sorted
     if (table[i].key == key) { funcVA = table[i].funcVA; callx8 funcVA; }   // 1 hop
   (miss → POOL "UNKNOWN OPCODE" log path)
```

---

## 1. Entry format — byte-exact

The table is a flat array of fixed **8-byte** records. There is no header and no
embedded count. Each record is:

| offset | size | field | encoding | notes |
|:-------|:-----|:------|:---------|:------|
| `+0x00` | 1 | `0x00` | — | always zero (top byte of the BE key) |
| `+0x01` | 1 | `0x00` | — | always zero |
| `+0x02` | 1 | `spec` | byte | sub-opcode / spec selector (`0` for most opcodes) |
| `+0x03` | 1 | `opcode` | byte | POOL / extended-instruction opcode |
| `+0x04` | 4 | `funcVA` | u32 **little-endian** | kernel entry VMA in `.text` (`0x010xxxxx`); `R_XTENSA_RELATIVE`, load-relocated |

The **key endianness** is the subtle part, and it reconciles the two framings used
across the POOL cluster:

- Read as bytes, the first four are stored **big-endian**: `opcode` sits in the
  **lowest** address (`+0x03`), `spec` at `+0x02`, `+0x00`/`+0x01 = 0`.
- Read the *same four bytes* as a **native little-endian `u32`** (Xtensa is LE), the
  value is exactly **`(opcode<<24) | (spec<<16)`** — the key formula pinned by
  [pool-dispatch](pool-dispatch.md) and [pool-ext-0xf0](pool-ext-0xf0.md).

> **GOTCHA — "BE key" and "`opcode<<24|spec<<16`" are the same four bytes.** A
> reimplementer comparing keys must build the comparand the *same way the firmware
> does*: as a native-LE `u32` equal to `opcode<<24 | spec<<16`. The byte-exact
> invariant is `opcode @ entry+3, spec @ entry+2`; the `<<24/<<16` form is the LE `u32`
> view of those bytes. Do not byte-swap one side only.

### 1.1 Worked byte example (row 0)

Raw 8 bytes of record 0 at file offset `0x7400`:

```
00 00 00 7e   80 00 00 01
└─ BE key ─┘  └ LE funcVA ┘
```

- BE key bytes `00 00 00 7e` → `opcode = 0x7e` (`+3`), `spec = 0x00` (`+2`).
  As native-LE `u32`: `0x7e000000 = (0x7e<<24) | (0<<16)`. ✔
- funcVA bytes `80 00 00 01` → LE `u32` `0x01000080` → the `iota` kernel entry
  trampoline.

`python3 struct.unpack` of all 17 records confirms the reconstruction
`(opcode<<24)|(spec<<16) == little_u32(entry[0:4])` holds for every row. `[HIGH/OBSERVED]`

### 1.2 Verified raw table bytes (file off `0x7400`, `0x88` bytes)

```
7400: 0000007e 80000001   0000007c f8030001   0000007d 10040001   00000045 900b0001
7420: 00000051 5c100001   00000041 1c0f0001   000000f0 70330001   000001f0 80330001
7440: 000002f0 84340001   000004f0 a8370001   000003f0 603a0001   00000052 403b0001
7460: 00000046 c0400001   00000047 60410001   000000be 04420001   000000f2 4c480001
7480: 0000007b c44d0001
```

Read each 8 bytes as `[BE key u32][LE funcVA u32]`. (xxd shows these byte-for-byte; the
grouping above pairs them for readability.)

### 1.3 The only relocated field

`funcVA` (`entry+4`) is the single load-relocated field. `.rela.got` contains exactly
**17** `R_XTENSA_RELATIVE` (type `0x05`) relocations whose `r_offset = table_base + 8*i + 4`
for `i = 0..16` — one per record, each pointing at the `funcVA` slot, **stride 8, addend
0**:

```
02000384 R_XTENSA_RELATIVE 0   ← record 0 funcVA slot (base+0x04)
0200038c R_XTENSA_RELATIVE 0   ← record 1            (base+0x0c)
...
02000404 R_XTENSA_RELATIVE 0   ← record 16           (base+0x84)
```

This independently proves four things at once: 8-byte stride, `funcVA` at `+4`, the key
is **not** relocated (so it is a literal compared value, not a pointer), and there are
**17** records.

---

## 2. Section geometry

`readelf -S` on the carved CAYMAN EXTISA_0 image places the table as its own section,
immediately followed by `.globstruct`:

```
[ 7] kernel_info_table  PROGBITS  Addr 0x02000380  Off 0x007400  Size 0x000088  ES 0  Flg WA  Al 8
[ 8] .globstruct        PROGBITS  Addr 0x02000408  Off 0x007488  Size 0x000048  ES 0  Flg WA  Al 8
```

The table occupies VMA `0x02000380 .. 0x02000408` (exclusive). `0x88 = 136 = 17 × 8`.
It is writable (`WA`) because the `funcVA` slots are relocated at load. The table's
**end VMA is exactly the `.globstruct` base** — the firmware uses that boundary as the
scan terminator.

> **QUIRK — no sentinel, no terminator.** Record 16 (the 17th) ends precisely at the
> section boundary `0x02000408`. The four bytes immediately after are `.globstruct` data
> (`0x6099cb34 …`), **not** a `0x00000000` or `0xffffffff` terminator. The table length
> comes *only* from the section size and the base/end getter pair (§4). A reimplementer
> must not scan for a null/`-1` row — there isn't one.

---

## 3. Lookup method — linear scan `[HIGH key-order / MED loop body]`

The key column, in table order, is:

```
7e 7c 7d 45 51 41   f0 f0 f0 f0 f0   52 46 47 be f2 7b
```

This is **not ascending** by opcode, nor by the full BE/LE key. Therefore the table is
**not sorted** and **cannot be binary-searched**; matching is a **linear scan** over
`[base, end)` comparing the packed key. The order is *registration order*: the five
`0xf0` rows are grouped and differ only by their spec byte (`0,1,2,4,3`), which is how
the multiplexed `0xf0` opcode is sub-selected (see [pool-ext-0xf0](pool-ext-0xf0.md)).

The dispatch/main routine (entered at `0x01005610`) loads `base = 0x02000380` and
`end = 0x02000408` via two tiny getters, derives `count = (end − base) >> 3 = 17`
(§4), and walks 8-byte records comparing the packed `(spec, opcode)` key. Annotated as
C, naming the real symbols:

```c
/* POOL main dispatch @ 0x01005610 (decode → key → scan → call) */
uint32_t key = (opcode << 24) | (spec << 16);          /* native-LE u32 of [0,0,spec,opcode] */

const KernelInfo *base = kernel_info_table_base();      /* getter @ 0x010055f8 → 0x02000380 */
const KernelInfo *end  = kernel_info_table_end();       /* getter @ 0x01005607 → 0x02000408 */
size_t count = ((const char *)end - (const char *)base) >> 3;   /* = 0x88 >> 3 = 17 */

for (size_t i = 0; i < count; ++i) {                    /* LINEAR SCAN, 8-byte stride */
    if (base[i].key == key) {                           /* whole 4-byte packed key compared */
        void (*kernel)() = (void (*)())base[i].funcVA;  /* funcVA @ entry+4 (load-relocated) */
        callx8(kernel);                                 /* single indirect call — 1 hop */
        return;
    }
}
/* miss → POOL "UNKNOWN OPCODE=0x%x" log path */
```

> **GOTCHA — the byte-exact compare loop is FLIX-desynced.** The per-record compare
> sits inside hand-written FLIX/VLIW-scheduled code with an interleaved literal-pool
> word; the native `objdump` linear sweep desyncs across literal/selector boundaries (a
> known POOL-firmware limitation). So the **base/end/count arithmetic and the absence of
> any sort are `HIGH`**, but the byte-exact per-record compare encoding is reported
> `MED`. The structural conclusion — *keyed linear scan, `callx8` on hit* — is solid.

On a hit, the matched record's `funcVA` is the absolute VMA of the per-opcode kernel
**entry trampoline**, reached via a single register-indirect call (`callx8`). The 17
`funcVA`s are distinct and follow trampoline-layout order
(`0x080, 0x3f8, 0x410, 0xb90, …, 0x4dc4`), **independent of the key order** — confirming
this is a *value table*, not an index-as-offset table.

---

## 4. Entry count — 17, three independent ways

| # | method | computation | result |
|:--|:-------|:------------|:------:|
| 1 | section size ÷ stride | `readelf -S`: size `0x88` ÷ 8 | **17** |
| 2 | relocation census | `readelf -r`: `R_XTENSA_RELATIVE` at `base + 4 + 8*i` | **17** |
| 3 | firmware-computed count | dispatch arithmetic `(end − base) >> 3` (below) | **17** |

Method 3 is read out of the dispatch routine's own code. Two getters supply the bounds,
and the main routine subtracts and shifts:

```
0x010055f8:  entry a1,32 ; const16 a2,0x0200 ; const16 a2,0x0380 ; retw.n   ; base → 0x02000380
0x01005607:  const16 a2,0x0200 ; const16 a2,0x0408 ; retw.n                 ; end  → 0x02000408 (==.globstruct base)
0x0100564a:  const16 a3,0x0408                  ; end loaded inline in the dispatcher
0x0100564d:  sub     a3, a3, a4                 ; a3 = end − base = 0x88
0x01005650:  srli    a3, a3, 3                  ; a3 = 0x88 >> 3 = 17
```

The base/end constants are decoded directly from the getter byte streams
(`24 00 02`/`24 80 03` build `0x02000380`; `24 00 02`/`24 08 04` build `0x02000408`);
the `34 08 04`/`40 33 c0`/`30 33 41` sequence is `const16 a3,0x408 ; sub a3,a3,a4 ;
srli a3,a3,3`. The `>> 3` (== `÷8`) independently re-confirms the 8-byte stride. All
three methods agree on **17**. The table is delimited by these getters and by the
section size — **never** by an in-band sentinel (§2).

---

## 5. `funcVA` validation — every entry lands on a real prologue

Each `funcVA` was checked against the raw `.text` byte stream (and spot-checked through
the native `xtensa-elf-objdump`). All 17 begin with the Xtensa windowed-ABI `entry`
prologue:

| prologue bytes | mnemonic | count |
|:---------------|:---------|:-----:|
| `36 41 00` | `entry a1, 32` | 16 / 17 |
| `36 61 00` | `entry a1, 48` | 1 / 17 (record 9, `0x010037a8`, larger frame) |

No `funcVA` points into the middle of an instruction; every one is a clean function
entry. (Record-by-record first bytes: `0x01000080 = 36 41 00`, …, `0x010037a8 = 36 61
00`, …, `0x01004dc4 = 36 41 00` — all verified by reading `.text` at each VMA.)

Three `funcVA`s point **directly** at a named function start (see §6 EXACT); the rest
point at a thin per-opcode entry trampoline that builds a `.bss` state-struct pointer
and then `const16`/`callx8`s into a named `decode_*`/`*_impl` worker.

> **NOTE — kernel names come from `.xt.prop`, not runtime strings.** This PERF image
> carries **no** `'P%i:'`/`'P%d:'` runtime log strings (those live in the DEBUG DRAM
> image). Every name below is recovered from the image's own
> `.xt.prop.<mangled>` property-record section names, whose first record word is the
> function start VMA; demangling with `c++filt` gives the C++ signature. Verified starts
> include: `pool_cross_lane_reduce_arith@0x010003f8`,
> `pool_cross_lane_reduce_bitvec@0x01000410`, `decode_pool(bool)@0x01000bc0`,
> `decode_tensor_tensor_arith@0x01000f60`, `pool_extended_inst_copy@0x01003380`,
> `decode_extended_inst_tensor_tensor_arith@0x010034b0`,
> `get_sequence_bounds_impl@0x01004284`,
> `nonzero_with_count_impl<int>@0x01004b80` / `<float>@0x01004940`,
> `decode_tensor_dequantize@0x01004df0`,
> `iota_impl<true>@0x01000100` / `<false>@0x010002c0`.

---

## 6. Full CAYMAN `kernel_info_table` dump

Every one of the 17 records, in table (registration) order. `funcVA` is the table
value; **routes to** names the worker the trampoline reaches; **basis** says how the
name was anchored.

| idx | opcode | spec | funcVA | routes to (resolved kernel) | basis | conf |
|----:|:------:|:----:|:-------|:----------------------------|:------|:-----|
| 0 | `0x7e` (126) | 0 | `0x01000080` | `iota` entry trampoline → `iota_impl<true>@0x01000100` / `iota_impl<false>@0x010002c0` | `.xt.prop` impl in band | `[HIGH op / MED route]` |
| 1 | `0x7c` (124) | 0 | `0x010003f8` | **`pool_cross_lane_reduce_arith()`** | `.xt.prop` EXACT (funcVA == start) | `[HIGH]` |
| 2 | `0x7d` (125) | 0 | `0x01000410` | **`pool_cross_lane_reduce_bitvec()`** | `.xt.prop` EXACT | `[HIGH]` |
| 3 | `0x45` (69) | 0 | `0x01000b90` | `decode_pool(bool)` — trampoline `const16 a2,0x100;const16 a2,0xbc0; callx8` → `0x01000bc0` | route decoded to `.xt.prop` start | `[HIGH]` |
| 4 | `0x51` (81) | 0 | `0x0100105c` | `decode_tensor_tensor_arith(uint)` entry (worker `@0x01000f60`) | named worker in band | `[MED]` |
| 5 | `0x41` (65) | 0 | `0x01000f1c` | `tensor_tensor_arith` op trampoline — **computed/indirect**: loads `*(0x0200045c)`, masks low byte, `slli ×16`, `callx8 a2` (runtime vtable, no fixed target) | indirect call confirmed | `[HIGH op / HIGH that it is indirect]` |
| 6 | `0xf0` (240) | 0 | `0x01003370` | **`ExtendedInstEngineNop`** (`entry a1,32; movi.n a2,0; retw.n` — empty) | full disasm | `[HIGH]` |
| 7 | `0xf0` (240) | 1 | `0x01003380` | **`pool_extended_inst_copy()`** | `.xt.prop` EXACT | `[HIGH]` |
| 8 | `0xf0` (240) | 2 | `0x01003484` | `decode_extended_inst_tensor_tensor_arith(bool,uint)` — `const16 a2,0x100;const16 a2,0x34b0; callx8` → `0x010034b0` | route decoded to `.xt.prop` start | `[HIGH]` |
| 9 | `0xf0` (240) | 4 | `0x010037a8` | extended spec 4 (`entry a1,48`; `.bss` state `0x0200046c`; routes via `decode_pool@0x01000b90`) | state ptr + route band | `[MED]` |
| 10 | `0xf0` (240) | 3 | `0x01003a60` | extended spec 3 (`.bss` state `0x02000470`; routes via `decode_pool@0x01000b90`) | state ptr + route band | `[MED]` |
| 11 | `0x52` (82) | 0 | `0x01003b40` | op `0x52` dispatch (loads `.data`/state table; no `.xt.prop` EXACT) | trampoline only | `[LOW]` |
| 12 | `0x46` (70) | 0 | `0x010040c0` | `pool_copy` op trampoline (SUNDA op 70) | op number + trampoline | `[HIGH op / MED route]` |
| 13 | `0x47` (71) | 0 | `0x01004160` | `pool_cast` op trampoline (SUNDA op 71) | op number + trampoline | `[HIGH op / MED route]` |
| 14 | `0xbe` (190) | 0 | `0x01004204` | `get_sequence_bounds_impl` — `const16 a2,0x4284; callx8` (encoding `24 84 42` @ `0x01004278`) → `0x01004284` | route decoded to `.xt.prop` start | `[HIGH]` |
| 15 | `0xf2` (242) | 0 | `0x0100484c` | `nonzero_with_count_impl<int>` — `const16 a2,0x4b80; callx8` (`24 80 4b` @ `0x0100491c`) → `0x01004b80`; alt branch `const16 a2,0x4940` (`24 40 49` @ `0x01004913`) → `<float>@0x01004940`; state ptr `0x0200047c` | route decoded to `.xt.prop` start | `[HIGH int / MED float branch]` |
| 16 | `0x7b` (123) | 0 | `0x01004dc4` | `decode_tensor_dequantize(bool)` — `const16 a2,0x100;const16 a2,0x4df0; callx8` → `0x01004df0` | route decoded to `.xt.prop` start | `[HIGH]` |

Naming bases:

- **EXACT** — `funcVA == .xt.prop.<mangled>` function start (records 1, 2, 7). `[HIGH]`
- **route** — trampoline disassembled; its `const16`/`callx8` (or `call0`/`j`) target
  lands on a named `.xt.prop` function start (records 3, 8, 14, 15, 16) or its body
  (records 0, 9, 10). `[HIGH for the five clean a2-routes; MED for body/branch routes]`
- **SUNDA op** — the opcode *number* matches the SUNDA opcode→pool-fn map
  (op 65/70/71/124/125/126 = `tensor_tensor_arith`/`copy`/`cast`/`clr_arith`/
  `clr_bitvec`/`iota`); CAYMAN reuses the same opcode numbering. The *number* is
  corroborating evidence; the *name* on this page is grounded in the image's own
  `.xt.prop` symbols. `[HIGH for the op number / CARRIED]`

> **CORRECTION — five routes (records 3, 8, 14, 15, 16) resolve to EXACT `.xt.prop`
> starts.** A naive `const16`-only byte scan misses the route target because the
> trampolines first build a `.bss` state-struct pointer with
> `const16 a2,0x200 ; const16 a2,0x<state_lo>`, *then* build the code target with a
> single `const16 a2,0x<route_lo>` (implicit high half `0x0100`) immediately before
> `callx8 a2`. Decoding that code-low `const16` (byte-exact, verified by direct search
> for the `24 <lo> <hi>` encoding inside each trampoline's body range) gives:
>
> | rec | opcode | code-low `const16` (bytes @ VMA) | route target | named function |
> |----:|:------:|:---------------------------------|:-------------|:---------------|
> | 3 | `0x45` | `24 c0 0b` @ `0x01000bb0` | `0x01000bc0` | `decode_pool(bool)` |
> | 8 | `0xf0/2` | `24 b0 34` @ `0x010034a4` | `0x010034b0` | `decode_extended_inst_tensor_tensor_arith` |
> | 14 | `0xbe` | `24 84 42` @ `0x01004278` | `0x01004284` | `get_sequence_bounds_impl` |
> | 15 | `0xf2` | `24 80 4b` @ `0x0100491c` | `0x01004b80` | `nonzero_with_count_impl<int>` |
> | 16 | `0x7b` | `24 f0 4d` @ `0x01004de4` | `0x01004df0` | `decode_tensor_dequantize` |
>
> Each lands on an EXACT `.xt.prop` function start, so these five are `HIGH` route.
> Records **14** (`0xbe`) and **15** (`0xf2`) were previously `LOW`/`MED` with a wrong
> worker attribution — `0xbe` was mis-tagged "no SUNDA entry / unresolved", and `0xf2`
> was mis-attributed to `get_sequence_bounds`/`tensor_dequantize`. The binary shows
> `0xbe → get_sequence_bounds_impl` and `0xf2 → nonzero_with_count_impl<int>` (with a
> `<float>@0x01004940` alt branch). Corrected here.

> **NOTE — trampoline `.bss` state pointers.** The state-struct pointers loaded by the
> trampolines (`0x02000458` record 3, `0x0200045c` record 5, `0x02000468` record 8,
> `0x0200046c` record 9, `0x02000470` record 10, `0x0200047c` record 15, `0x02000480`
> record 16) fall **past `.globstruct`** (which ends at `0x02000450`) into the image's
> `.bss` (VMA `0x02000450`, size `0x3c`). They are zero-initialized per-opcode scratch
> blocks, one per kernel — not part of the dispatch table itself.

---

## 7. Entries not cleanly resolved — flagged honestly

- **op `0x51` (record 4), op `0x52` (record 11), op `0x46` (record 12), op `0x47`
  (record 13)** could **not** be cleanly route-resolved. Their trampoline bodies are
  dominated by `op0 = 0xf` 16-byte FLIX wide bundles that the available toolchain
  cannot keep in sync (every candidate `j`/`call` target lands out of range — a clear
  desync artifact). Records 11/12 share a byte-identical prologue and 13 is a close
  sibling, suggesting a shared `copy`/`cast` dispatch family, but the actual transfer is
  not recoverable statically. The **opcode** is `HIGH`; the **worker name** is `LOW`
  (record 4 names `decode_tensor_tensor_arith` from the in-band `.xt.prop`, but its
  route is not byte-confirmed). Treat these worker names as provisional until reached
  through a live trace.
- **op `0x41` (record 5)** is a genuinely **indirect / runtime-computed** call: the
  trampoline loads a function pointer from `*(0x0200045c)`, masks the low byte,
  `slli ×16`, and `callx8 a2`. There is **no fixed code `const16`** before the call, so
  there is no single static named target — the dispatch is a runtime vtable index. That
  it is indirect is `HIGH`; the concrete worker depends on runtime state.
- **op `0xf0` specs 0/3/4 (records 6, 9, 10):** the `0xf0` opcode is multiplexed by the
  spec byte. Spec 1 (`pool_extended_inst_copy`) and spec 2
  (`decode_extended_inst_tensor_tensor_arith`) resolve `HIGH`; spec 0 is an empty NOP
  (resolved `HIGH` by full disasm); specs 3/4 route through the shared
  `decode_pool`/Rand-Cptc paths and are `MED`. See
  [pool-ext-0xf0](pool-ext-0xf0.md) for the full per-spec handler disassembly.
- **No `funcVA` lands in a FLIX-desync span** in a way that breaks prologue detection —
  all 17 are valid `entry` prologues. The desync only affects the *interior* of the
  hand-written dispatch loop and some trampoline bodies (limiting route confidence on
  the `MED` rows), never the table-entry targets themselves.

---

## 8. Cross-image stability `[HIGH/OBSERVED → CARRIED]`

- **MARIANA EXTISA_0** (`internal_MARIANA_0.so`, carved the same way, 41,568 B) carries
  a `kernel_info_table` at the **same** VMA `0x02000380`, **same** size `0x88`, and the
  **same 17 `(opcode, spec)` sequence** as CAYMAN — byte-for-byte identical key column.
  Only the `funcVA`s differ, by the small entry-offset delta between the two builds. The
  format and opcode set are **stable across the CAYMAN / MARIANA / MARIANA_PLUS
  generation** (all three ship the main EXTISA_0 image with the same dispatch table).
  `[HIGH/OBSERVED for CAYMAN; CARRIED for the MARIANA byte-identity claim]`
- The smaller per-engine POOL **sub-images** each embed their *own* smaller
  `kernel_info_table` covering only the kernels that sub-image implements (e.g.
  `EXTISA_1` ~1 entry, `EXTISA_2` ~2 entries, `EXTISA_3` ~9 entries, at per-image VMAs).
  The VMA/size are per-image; the **8-byte entry format is identical**. The forward
  Part 6 [cross-generation kernel-info matrix](../images/cross-gen-kernel-info-matrix.md)
  will enumerate these side by side.

---

## 9. Reproduction

```sh
# 1. carve the canonical CAYMAN EXTISA_0 image from the x86-64 host loader
HOST=.../libnrtucode_internal.so
#    blob = data behind getter CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get
#    symbol CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get.data @ .rodata vaddr 0x2ef7e0, size 41568
dd if="$HOST" of=EXTISA_0.so bs=1 skip=$((0x2ef7e0)) count=41568
sha256sum EXTISA_0.so      # 910d41c3ededce67…b4b55527

# 2. section geometry + the table itself
readelf -SW EXTISA_0.so | grep -E 'kernel_info_table|globstruct'   # [7] @0x02000380 0x88
xxd -s 0x7400 -l 0x88 EXTISA_0.so                                  # 17 × 8-byte records

# 3. count three ways
python3 -c 'import struct;d=open("EXTISA_0.so","rb").read()[0x7400:0x7400+0x88];print(len(d)//8)'  # 17
readelf -rW EXTISA_0.so | grep R_XTENSA_RELATIVE | grep -c 0200038  # relocs in table range
#    dispatch arithmetic: (0x02000408 - 0x02000380) >> 3 = 17

# 4. native FLIX disassembly of a funcVA (force the start address)
NESTED=.../gpsimd_tools_tgz
export XTENSA_SYSTEM="$NESTED/tools/XtensaTools/config" XTENSA_CORE=ncore2gp
"$NESTED/tools/XtensaTools/bin/xtensa-elf-objdump" -d \
    --start-address=0x01000b90 --stop-address=0x01000bc0 EXTISA_0.so
```

---

## See also

- [POOL main dispatch loop](pool-dispatch.md) — how the table is scanned and the
  `callx8`-on-hit happens (#677).
- [POOL extended-opcode (`0xF0`) dispatch](pool-ext-0xf0.md) — the five `0xf0` spec
  rows of this table, per-spec handler disassembly, and the Cptc codec family (#678).
- [Cross-generation kernel-info matrix](../images/cross-gen-kernel-info-matrix.md) —
  *(forward link; Part 6, not yet authored)* — the per-image `kernel_info_table`s
  across CAYMAN/MARIANA/MAVERICK and the sub-images, side by side.
- [The Opcode Catalog Ledger](../kernels/opcode-catalog-ledger.md) — the master
  opcode→kernel ledger this table feeds — every per-kernel page resolves its opcode's
  `funcVA` through this table.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the normative
  definition of the `[HIGH/OBSERVED]`-style tags used throughout.
