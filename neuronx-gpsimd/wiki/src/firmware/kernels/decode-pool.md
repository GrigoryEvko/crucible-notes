# `decode_pool` — the "Pool" Kernel (role disambiguation)

> **NOTE — there are TWO things called "pool" in this firmware, and conflating them is
> the single most common reimplementation error here.** This page is about the **second**
> one.
>
> 1. **The POOL *dispatch loop*** — the POOL engine's top-level per-instruction
>    fetch/decode/dispatch FSM (`e_entry = 0x01005610`, the linear scan of the 17-entry
>    `kernel_info_table`). That is **not** this page; it is fully owned by
>    [POOL Engine Main Dispatch Loop](../pool/pool-dispatch.md).
> 2. **The `decode_pool` *kernel* (this page)** — an actual windowed avg/max **pooling
>    compute kernel**, funcVA **`0x01000b90`** (entry trampoline) → body **`0x01000bc0`**
>    (`.xt.prop._Z11decode_poolb` = `decode_pool(bool)`). It is `kernel_info_table`
>    **idx 3** (opcode `0x45 'E'`, spec 0). It is a **leaf kernel that the dispatch loop
>    *calls*** on a Pool opcode — a decode *stage inside one kernel*, **not** the engine
>    dispatcher and **not** its alias.
>
> "POOL engine" is the Vision-Q7 compute core that runs *all* pool-core kernels; "pool
> operation" is the *specific* windowed avg/max kernel that lends the engine its name.
> `decode_pool` is the namesake **operation**, not the dispatcher. The two functions live
> `~0x4a50` bytes apart (`0x01000bc0` vs `0x01005610`), carry different `.xt.prop`
> coverage, and self-name differently in the DEBUG trace (`"P%i: Pool : …"` vs
> `"P%i: … dispatch …"`).

This page anchors the `decode_pool` kernel's **dispatch role and table linkage** — *where*
it sits in the `kernel_info_table`, *how* opcode `0x45` reaches it, *what* its entry
trampoline does, and *how* the `0xF0` extended band re-uses its entry. The avg/max
**math** (the windowed reduction, the `1/N` scale, the fp32 accumulator) is the companion
[avg_pool / max_pool](avg-max-pool.md) page (*planned*); this page documents only the
**inner pool_func switch as a dispatch fact** (one table row → one kernel → two inline
arms), not the SIMD body in detail.

The POOL core runs on the Vision-Q7 NX `ncore2gp` ("Cairo") datapath core
(`XCHAL_HAVE_VISION = 1`, `XCHAL_VISION_TYPE = 7` — the FLIX/VLIW layer;
`XCHAL_HAVE_FLIX3 = 0` is **not** "scalar"). All disassembly below is the native Cadence
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`); the scalar-LX decode rule decodes the
*different* NCFW management core and is wrong here.

> **NOTE — the exact object decoded.** The carve source is the static archive
> `libnrtucode.a`. The canonical image
> is archive member `img_CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_contents.c.o`; its `.rodata`
> payload (file off `0x60`, size `0xA260`) **is** the embedded Vision-Q7 device SO
> (`== internal_CAYMAN_0.so`):
>
> | property | value |
> |---|---|
> | object | `CAYMAN_Q7_POOL_PERF_EXTISA_0` device SO (`extisa_CAYMAN_POOL_PERF_EXTISA_0.so`) |
> | size | **41,568 B** (`0xA260`) |
> | sha256 | **`910d41c3ededce67cd00ec7041a5e66c3c39536d2e9b16fe21ea019db4b55527`** |
> | ELF | ELFCLASS32 LE, `e_machine = 94` (Tensilica Xtensa), `ET_EXEC` |
> | entry | `0x01005610` (the **dispatch loop**, not this kernel) |
> | `.text` map | VMA `0x01000000` at file off `0x100` (`file_off = VMA − 0x01000000 + 0x100`) |
>
> The `'P%i:'`-prefixed `decode_pool` log strings (`"Pool : num_chans"`,
> `"Running max_pool …"`, `"Running avg_pool …"`, `"Error, invalid pooling function."`)
> are ASCII baked into the **DEBUG** DRAM image
> (`img_CAYMAN_Q7_POOL_DEBUG_DRAM_contents.c.o` `.rodata`, 89,344 B,
> sha256 `226f4254…f6f0128e`); this PERF image strips them, so its kernel name comes from
> the `.xt.prop._Z11decode_poolb` section. The `'P%i:'` strings are used purely as name
> anchors.

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte/string read from the shipped image; `INFERRED` = reasoned
over OBSERVED facts (often across a FLIX/literal-pool desync); `CARRIED` = consolidated
from a cited cross-page anchor at its original confidence. Crossed with `HIGH`/`MED`/`LOW`.
Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a reimplementation trap),
**CORRECTION** (overturns a naive reading), **NOTE** (orientation).

---

## 0. The two "pools" in one diagram

```
                       ┌─────────────────────────────────────────────────────────┐
   POOL DISPATCH LOOP  │  e_entry 0x01005610  (entry a1,32)                       │
   (NOT this page —    │  key = (opcode<<24)|(spec<<16)                           │
    pool-dispatch.md)  │  LINEAR SCAN of kernel_info_table @0x02000380 (17 rows)  │
                       └──────────────────────────┬──────────────────────────────┘
                                                  │ match (0x45, 0)  → idx 3
                                                  │ load funcVA @ idx3+4 = 0x01000b90
                                                  │ callx8 0x01000b90
                                                  ▼
                       ┌─────────────────────────────────────────────────────────┐
   decode_pool KERNEL  │  ENTRY TRAMPOLINE  0x01000b90  (THIS page)               │
   (this page)         │    entry a1,32 ; const16 a2,0x02000458 (.bss state) ;    │
                       │    (FLIX setup) ; const16 a2,0x01000bc0 ; callx8 a2 ;    │
                       │    movi.n a2,0 ; retw.n                                  │
                       └──────────────────────────┬──────────────────────────────┘
                                                  │ callx8 0x01000bc0
                                                  ▼
                       ┌─────────────────────────────────────────────────────────┐
   IMPL BODY           │  decode_pool(bool)  0x01000bc0  (.xt.prop _Z11decode_poolb)│
                       │    decode S4D4_PL operand (64 B) ; read pool_func@+36     │
                       │    switch (pool_func) {                                   │
                       │      MAX_POOL(1) → "Running max_pool …"  windowed max     │
                       │      AVG_POOL(2) → "Running avg_pool …"  Σ × pool_scale   │
                       │      default     → "Error, invalid pooling function."     │
                       │    }                                                      │
                       └─────────────────────────────────────────────────────────┘

   ALSO reached: 0xF0 EXTENDED spec 3 (@0x01003a60) and spec 4 (@0x010037a8) both
   `const16 a0,0x01000b90` → re-enter decode_pool's entry trampoline (Rand/Cptc band),
   each with its OWN .bss state slot (0x470 / 0x46c).
```

One-line verdict: `decode_pool` is a **leaf pooling kernel** at table idx 3; the *only*
"dispatch" inside it is (a) the loop's `callx8` into it, and (b) its own inner `pool_func`
switch. It is structurally a *kernel*, not a *dispatcher*. `[HIGH/OBSERVED]`

---

## 1. Key facts

All rows `[HIGH/OBSERVED]` except the last.

| Fact | Value | Anchor |
|---|---|---|
| Entry trampoline | `0x01000b90` (file off `0xc90`) | xxd; `kernel_info_table` idx3 funcVA |
| Impl body | `0x01000bc0` (file off `0xcc0`) | `.xt.prop._Z11decode_poolb` FUNC-START |
| Mangled / demangled | `_Z11decode_poolb` → `decode_pool(bool)` | section [17] name + `c++filt` |
| `.xt.prop` section | [17] file off `0x85bc`, size `0x108` (22 records) | `readelf -SW` |
| Body extent | `0x01000bc0 .. 0x01000d9a` (`~0x1da` B) | max prop end-VA |
| `.bss` state slot | `0x02000458` (loaded by trampoline) | trampoline `const16` bytes |
| Table index | idx **3** of 17 | linear-scan order; reloc census |
| Table row bytes | `00 00 00 45 90 0b 00 01` @ file off `0x7418` | xxd |
| Opcode / spec | `0x45` ('E', 69) / `0` | row `+3` / `+2` |
| Packed key | `0x45000000` = `(0x45<<24)\|(0<<16)` | native-LE u32 of row `[0..4]` |
| funcVA reloc | `R_XTENSA_RELATIVE` @ `0x0200039c` (= base+3·8+4) | `readelf -rW` |
| Trampoline shape | `entry a1,32 ; const16 state ; FLIX ; const16 impl ; callx8 ; movi a2,0 ; retw.n` | byte-exact |
| Inner classifier | `pool_func` @ S4D4_PL `+36`, enum `{NONE=0,MAX=1,AVG=2}` | shipped ISA header |
| 0xF0 re-use | specs 3,4 `const16 a0,0x01000b90` → re-enter trampoline | route bytes `04 90 0b` |
| Per-gen | idx3/trampoline/body/`.xt.prop` invariant across CAYMAN/MARIANA/MARIANA_PLUS | byte compare §6 |
| MAVERICK / v5 | EXTISA_0 not in this archive | absence `[CARRIED/INFERRED]` |

---

## 2. Why this is **not** the dispatch loop (the disambiguation, byte-grounded)

The dispatch loop and `decode_pool` are **separate functions** with separate roles. The
dispatch loop's distinguishing arithmetic — the `kernel_info_table` base/end getters and
the `(end−base)>>3` count — lives at a **different VMA** from `decode_pool`:

```
base getter @0x010055f8 :  36 41 00  entry a1,32
                           24 00 02  const16 a2,0x200
                           24 80 03  const16 a2,0x380   ; → kernel_info_table BASE = 0x02000380
                           1d f0     retw.n
end  getter @0x01005607 :  24 00 02  const16 a2,0x200
                           24 08 04  const16 a2,0x408   ; → kernel_info_table END  = 0x02000408
                           1d f0     retw.n
count       @0x01005641 :  44 00 02 34 00 02 44 80 03 34 08 04   ; a4=0x380(BASE), a3=0x408(END)
                           40 33 c0  sub  a3,a3,a4       ; a3 = 0x88
                           30 33 41  srli a3,a3,3        ; a3 = 0x88>>3 = 17  (>>3 ⇒ 8-byte stride)
e_entry     @0x01005610 :  36 41 00  entry a1,32         ; the dispatch prologue
```

`decode_pool` (`0x01000bc0`) contains **none** of this — no base/end getters, no
`(end−base)>>3`, no table scan. It is a kernel **called by** that loop, `~0x4a50` bytes
away. Two distinct functions, two distinct `.xt.prop` coverages, two distinct DEBUG string
clusters (§7). It is emphatically **not** an alias of the dispatcher.
`[HIGH/OBSERVED]` — full loop treatment: [pool-dispatch.md](../pool/pool-dispatch.md).

> **GOTCHA — the name "decode_pool" is *not* "the pool decoder/dispatcher".** The `decode_`
> prefix marks the *per-kernel operand-decode-and-execute worker* for the Pool op, in the
> same naming family as `decode_tensor_tensor_arith` (idx 4),
> `decode_extended_inst_tensor_tensor_arith` (`0xF0`/spec 2), and
> `decode_tensor_dequantize` (idx 16). It is a **kernel-internal decode stage**, not the
> engine-level instruction decoder. The engine-level decoder is the dispatch loop at
> `0x01005610`. A reimplementer who reads "decode_pool" as "the function that decodes which
> pool kernel to run" has the layering exactly inverted.

---

## 3. The kernel_info_table linkage — opcode `0x45` → idx 3 → `0x01000b90`

`decode_pool` is reached purely through the `kernel_info_table` (its full binary layout is
owned by [kernel-info-table.md](../pool/kernel-info-table.md); only the one `0x45/idx3` row
is documented here). The 8-byte record format is
`{ u8 0; u8 0; u8 spec(+2); u8 opcode(+3); u32_le funcVA(+4) }`, and the dispatcher builds
`key = (opcode<<24) | (spec<<16)` — the native-LE u32 view of the first four bytes — then
linear-scans for a match.

### 3.1 The idx-3 row, byte-exact

Raw row at file off `0x7418` (= table base file off `0x7400` + `3·8`):

```
00007418:  00 00 00 45   90 0b 00 01
           └─ BE key ─┘  └ LE funcVA ┘
```

- Key bytes `00 00 00 45` → `opcode = 0x45` (`+3`), `spec = 0x00` (`+2`).
  As a native-LE u32: `0x45000000 = (0x45<<24) | (0<<16)`. ✓
- funcVA bytes `90 0b 00 01` → LE u32 **`0x01000b90`** = the `decode_pool` entry
  trampoline.

`0x45` is ASCII `'E'`; the SUNDA cross-image opcode map registers `0x45` as the Pool
operation, and CAYMAN reuses that numbering. `[HIGH/OBSERVED]`

### 3.2 The funcVA is the **only** relocated field — and it pins idx 3

`.rela.got` carries exactly **17** `R_XTENSA_RELATIVE` (type `0x05`) relocations, one per
record at `r_offset = table_base + 8·i + 4` for `i = 0..16`. The relocation for **idx 3**
sits at `0x02000380 + 3·8 + 4 = 0x0200039c`:

```
0200039c  00000005 R_XTENSA_RELATIVE  0     ← idx 3 funcVA slot (decode_pool)
```

This independently fixes the 8-byte stride, `funcVA @ +4`, the key being a literal
(non-relocated) compare value, and idx 3's position. The key bytes are **not** relocated.

> **GOTCHA — `decode_pool` is found by a *keyed linear scan*, not a direct index.** The
> table is in **registration order** (`7e 7c 7d 45 51 41 f0×5 52 46 47 be f2 7b`), not
> ascending — `decode_pool`'s `0x45` is the *fourth* entry, not the 0x45th. A
> reimplementation that direct-indexes by opcode reads garbage; scan linearly comparing the
> packed `(opcode, spec)` key (`O(17)`). The `index 3` here is the *scan position*, not a
> computed offset.

---

## 4. The entry trampoline `0x01000b90` — byte-exact

The table funcVA is the **trampoline** (`0x01000b90`); the named worker is the **body**
(`0x01000bc0`). The trampoline is the standard POOL per-opcode kernel entry shape:
materialise this kernel's `.bss` state-object pointer, then `callx8` the impl. Raw bytes
(`xxd`, file off `0xc90`):

```
01000b90:  36 41 00 24 00 02 24 58 04 8f 32 90 0b 24 00 00
01000ba0:  00 21 40 28 02 c0 22 11 2f 00 c5 89 91 00 40 41
01000bb0:  24 c0 0b e0 02 00 0c 02 1d f0 00 00 00 00 00 00
```

Byte-0-aligned decode (the slice starts on `0xb90`, so byte 0 is the `entry` insn — no
phase shift):

```
 1000b90:  36 41 00     entry   a1, 32          ; windowed-ABI prologue, 32-byte frame
 1000b93:  24 00 02     const16 a2, 0x200       ; a2 high half
 1000b96:  24 58 04     const16 a2, 0x458       ; a2 = 0x02000458  ← decode_pool .bss STATE SLOT
 1000b99:  8f …         <FLIX/F-format bundle; 0x8f selector — DESYNC span; setup the linear
                          sweep mis-renders, NOT trusted as the listed scalar mnemonics>
 1000bb0:  24 c0 0b     const16 a2, 0xbc0       ; a2 = 0x01000bc0  ← decode_pool IMPL BODY
 1000bb3:  e0 02 00     callx8  a2              ; call decode_pool(bool) @0x01000bc0
 1000bb6:  0c 02        movi.n  a2, 0           ; return 0
 1000bb8:  1d f0        retw.n
```

So the trampoline does exactly four things: open a 32-byte windowed frame; load the
per-kernel `.bss` state pointer `0x02000458`; (across a FLIX setup span) materialise the
impl VA `0x01000bc0`; `callx8` it; return 0. `[HIGH/OBSERVED]` for the prologue, the two
`const16` loads, and the `callx8`/`retw.n`; the `0xb99..0xbb0` FLIX interior is desynced
and reported only structurally.

The `.bss` membership of the state slot is confirmed by section geometry:
`.globstruct` is `0x02000408..0x02000450`; `.bss` is `0x02000450..0x0200048c` (NOBITS,
zero-init). `0x02000458` falls **inside `.bss`** — a zero-initialised per-kernel
state/scratch pointer, written at runtime, not part of the dispatch table.

> **CORRECTION — the trampoline is `entry a1,32 … callx8 0x01000bc0`, NOT
> `entry a1,64 … callx8 0x01006ee8`.** An earlier pass disassembled this trampoline as
> `entry a1,64 ; const16 a8,…/0x5c ; callx8 a8 ; const16 a8,…/0x6ee8 ; callx8 0x01006ee8 ;
> retw.n`. That is an **objdump phase-shift artifact**: allowing the linear sweep to decode
> the bytes *preceding* `0xb90` lands it mid-instruction (a non-byte-0-aligned start prints
> the bogus `36 81 00 → entry a1,64`). The byte-exact truth — raw bytes `36 41 00 …`,
> byte-0-aligned decode — is `entry a1,32`, state slot `0x02000458`, and the work call is
> `callx8 a2` with `a2 = 0x01000bc0` (**not** `0x01006ee8`). The body VMA `0x01000bc0` was
> always correct; only the trampoline disasm was the desync artifact.

> **NOTE — the trampoline lives *outside* the `decode_pool` `.xt.prop` coverage.** The
> `.xt.prop._Z11decode_poolb` records (§5) cover the **body** `0x01000bc0..0x01000d9a`;
> the trampoline at `0x01000b90` is its own 0x30-byte stub *before* the body, with no
> `.xt.prop` of its own. The table funcVA (`0x01000b90`) and the named worker
> (`0x01000bc0`) are deliberately split: the table points at the trampoline, the trampoline
> tail-calls the named body.

---

## 5. The body `0x01000bc0` — identity, extent, and the `.xt.prop` map

The body is the named worker. Its identity is fixed by the `.xt.prop._Z11decode_poolb`
section, whose **first 12-byte record is the function-start descriptor**:

```
file off 0x85bc:  c0 0b 00 01   00 00 00 00   04 28 00 00
                  └ VA 0x1000bc0┘ └ size 0  ┘  └ flags 0x2804 ┘   ← FUNC-START
```

`c++filt _Z11decode_poolb → decode_pool(bool)`. The `0x2804` flag word marks a
function-start record; the body VMA is `0x01000bc0`. The first body bytes are
`36 41 00 8f` = `entry a1,32` immediately followed by a `0x8f` FLIX selector — the body is
hand-scheduled FLIX VLIW. `[HIGH/OBSERVED]`

### 5.1 The property record set — code/data spans (why the body desyncs)

Parsing all **22** records (`{u32 VA, u32 size, u32 flags}`, 264 B) gives the body's
full code/data span map and its extent `0x01000bc0 .. 0x01000d9a` (`~0x1da` B):

| rec | VA | size | flags | kind |
|----:|:---|:-----|:------|:-----|
| 0 | `0x01000bc0` | `0x0000` | `0x2804` | FUNC-START |
| 1 | `0x01000bc0` | `0x0062` | `0x0082` | code span |
| 2 | `0x01000c22` | `0x0002` | `0x0008` | **DATA** (literal) |
| 3 | `0x01000c24` | `0x0020` | `0x00a2` | code span |
| 4 | `0x01000c44` | `0x0033` | `0x00a2` | code span |
| 5 | `0x01000c77` | `0x0001` | `0x0008` | **DATA** |
| … | … | … | … | (alternating code `0x82/0xa2/0x92` / DATA `0x08`) |
| 20 | `0x01000d98` | `0x0002` | `0x0092` | code span |
| 21 | `0x01000d9a` | `0x0000` | `0x0008` | terminator |

Eight `0x08` (DATA) spans are interleaved into the body — literal pools embedded in
`.text`. These are exactly what break `xtensa-elf-objdump`'s linear sweep: the sweep
mis-renders the literal words as FLIX bundles (the documented Vision-Q7 desync). Decoding
this body with the native objdump prints raw FLIX words and `.byte` for `~25%` of the span;
those mis-decoded words and any out-of-`.text` call targets are **not** reported as real.
`[HIGH/OBSERVED span map; body partly DESYNC]`

> **NOTE — the inner SIMD pooling math is OUT OF SCOPE here and reported only structurally.**
> The body's `ivp_*` Vision-Q7 vector primitives (the windowed max / sum / scale datapath)
> are the subject of [avg_pool / max_pool](avg-max-pool.md) (*planned*). This page treats
> the body only as a **dispatch target**: `0x01000bc0 = decode_pool(bool)`, one kernel,
> reached by one table row. The `ivp_*` op *set* is INFERRED from the dtype/vector model;
> the in-body slot order is not byte-pinned. `[in-body math LOW/INFERRED on this page]`

---

## 6. The inner `pool_func` switch — avg/max are NOT separate dispatch targets

The task's standing question — *"do avg_pool and max_pool dispatch through `decode_pool`?"*
— resolves to: **they ARE `decode_pool`.** There is **no** separate `avg_pool` kernel and
**no** separate `max_pool` kernel; neither carries its own `.xt.prop` section (only
`_Z11decode_poolb` does). avg vs max is a **1-byte parameter** read *inside* the body, below
the dispatch layer.

The parameter is grounded in the shipped ISA header
`neuron_sunda_arch_isa/tpb/aws_neuron_isa_tpb_s4d4_pl.h`, which defines the 64-byte Pool
operand `NEURON_ISA_TPB_S4D4_PL_STRUCT` and the selector enum:

```c
typedef enum NEURON_ISA_TPB_POOL_TYPE {
    NEURON_ISA_TPB_POOL_TYPE_NONE     = 0,
    NEURON_ISA_TPB_POOL_TYPE_MAX_POOL = 1,
    NEURON_ISA_TPB_POOL_TYPE_AVG_POOL = 2,
} NEURON_ISA_PACKED NEURON_ISA_TPB_POOL_TYPE;

struct NEURON_ISA_TPB_S4D4_PL_STRUCT {   /* 64 B (ISA_STATIC_ASSERT == 64) */
    /* … src/dst mem access patterns … */
    NEURON_ISA_TPB_DTYPE      in_dtype;             /* +32 */
    NEURON_ISA_TPB_DTYPE      out_dtype;            /* +33 */
    uint8_t                   num_active_channels;  /* +34 */
    NEURON_ISA_TPB_POOL_TYPE  pool_func;            /* +36  ← the avg/max selector */
    NEURON_ISA_TPB_TENSOR_SUBDIM pool_dim;          /* +37 */
    float                     pool_scale;           /* +40..43  = 1/(R·S) for avg-pool */
};
```

The DEBUG trace confirms the switch arms (all byte-present in `dbg_dram.bin`, with
file offsets):

```
0x1f7b  "P%i: Pool : num_chans = %0d"                                   ← decode_pool announces itself
0x1f98  "P%i: Error, invalid pooling function."                         ← the default arm (pool_func ∉ {MAX,AVG})
0x1fbf  "P%i: Running max_pool with period = %0d and num = %0d."        ← MAX_POOL(1)
0x1ff7  "P%i: Done running max_pool."
0x2014  "P%i: Running avg_pool with period = %0d and num = %0d."        ← AVG_POOL(2)
0x204c  "P%i: Done running avg_pool."
```

As annotated C, naming the real symbols:

```c
/* decode_pool(bool) @ 0x01000bc0 — inner classification (structure HIGH/OBSERVED from the
 * S4D4_PL header + DEBUG strings; the in-body FLIX arms are reported structurally).        */
void decode_pool(bool per_instr_decode_flag)                /* `b` arg, FW-14/45 convention */
{
    NEURON_ISA_TPB_S4D4_PL_STRUCT *op = current_pool_operand();   /* 64 B operand decode    */
    log("P%%i: Pool : num_chans = %%0d", op->num_active_channels); /* str @0x1f7b            */

    switch (op->pool_func) {                                 /* enum @ +36, POOL_TYPE        */
    case NEURON_ISA_TPB_POOL_TYPE_MAX_POOL:                  /* == 1                          */
        log("P%%i: Running max_pool with period = %%0d and num = %%0d.", period, num);  /* 0x1fbf */
        /* fp32 windowed MAX over the pool window (ivp_max* vector reduce — avg-max-pool.md) */
        log("P%%i: Done running max_pool.");                 /* 0x1ff7                        */
        break;
    case NEURON_ISA_TPB_POOL_TYPE_AVG_POOL:                  /* == 2                          */
        log("P%%i: Running avg_pool with period = %%0d and num = %%0d.", period, num);  /* 0x2014 */
        /* fp32 windowed SUM, then × pool_scale (= 1/(R·S)); fp32 accumulator (avg-max-pool.md)*/
        log("P%%i: Done running avg_pool.");                 /* 0x204c                        */
        break;
    default: /* NONE(0) or out-of-range */
        log("P%%i: Error, invalid pooling function.");       /* 0x1f98 — decode_pool's OWN error */
        break;
    }
}
```

This is a **second, inner** classification distinct from the dispatcher's opcode
classification, at a different layer:

| layer | classifies on | bound by | error path |
|---|---|---|---|
| dispatch loop (`0x01005610`) | the **opcode** (`0x45` vs `0xf0` vs …) | `count = (end−base)>>3 = 17`; no sentinel | `"P%i: UNKNOWN OPCODE=0x%x"` (scan exhausted) |
| `decode_pool` (`0x01000bc0`) | the **`pool_func`** byte (`1`/`2`/other) | S4D4_PL `is_valid_*` (pool_func ∈ {MAX,AVG}, dtype, subdim, active-channel range) | `"P%i: Error, invalid pooling function."` |

By the time `decode_pool` runs, the opcode is already validated as `0x45` (or `0xf0` via
§7); `decode_pool`'s own validity check is purely on the operand fields. The avg/max choice
is keyed by `pool_func @ +36` — **not by opcode and not by spec**: one table row, one
kernel, two inline arms. `[HIGH/OBSERVED switch; FLIX arms structural]`

> **QUIRK — `pool_scale` removes division from the avg-pool datapath.** The shipped header
> states `pool_scale` holds `1/(R·S)` precomputed by the host (0.25 for 2×2, 0.1111… for
> 3×3), so the engine *scales-then-accumulates* (or sums-then-multiplies, the ISA permits
> either, with fp32 internal accumulation). The avg-pool arm therefore does no integer
> division — it is a windowed SUM followed by one fp32 multiply. The full reduction is the
> [avg_pool / max_pool](avg-max-pool.md) page. `[HIGH/OBSERVED from the ISA header]`

---

## 7. The base-vs-extended (`0xF0`) branch — `decode_pool` is reached on BOTH paths

There is **no special-case `0xF0` branch** in the dispatch loop (see
[pool-ext-0xf0.md](../pool/pool-ext-0xf0.md)): the five `0xf0`+spec rows *are* the
sub-dispatch table, and the single linear scan matches `(0xf0, spec)` to exactly one row.
`decode_pool`'s **entry trampoline `0x01000b90` is a shared decode entry**, reached along
two distinct table paths.

**PATH 1 — BASE opcode `0x45` (the direct Pool path):** idx 3 funcVA `0x01000b90` →
trampoline → `decode_pool@0x01000bc0`, state slot `0x02000458`. This is the windowed
avg/max Pool instruction. `[HIGH/OBSERVED]`

**PATH 2 — `0xF0` EXTENDED spec-3 and spec-4 (the Rand/Cptc band re-using `decode_pool`):**
both `0xF0` extended trampolines materialise `const16 a0, 0xb90` (bytes `04 90 0b`),
routing into the **same** `decode_pool` entry `0x01000b90`, but with their **own** `.bss`
state slots, byte-exact:

```
spec 4  @0x010037a8 (file off 0x38a8):
   3661 00   entry   a1, 48          ; LARGER 48-byte frame (the only one of the five 0xF0 rows)
   4f 00 6f 79 08 00 c2 42           ; shared FLIX/literal preamble
   44 6c 04  const16 a4, 0x46c       ; per-kernel state slot 0x0200046c (.bss)
   …(FLIX body)…
   04 90 0b  const16 a0, 0xb90       ; a0 = 0x01000b90 = decode_pool ENTRY trampoline

spec 3  @0x01003a60 (file off 0x3b60):
   3641 00   entry   a1, 32
   4f 00 6f 79 08 00 c2 42           ; identical preamble to spec-4
   44 70 04  const16 a4, 0x470       ; per-kernel state slot 0x02000470 (.bss)
   …(FLIX body)…
   04 90 0b  const16 a0, 0xb90       ; a0 = 0x01000b90 = decode_pool ENTRY trampoline
```

The two share the preamble bytes `4f 00 6f 79 08 00 c2 42`, differing only in the entry
frame size (48 vs 32), the state-slot immediate (`0x46c` vs `0x470`), and one selector
byte. `[HIGH route bytes / MED Rand/Cptc variant name]`

The `0xF0` specs **0/1/2 do NOT** route through `decode_pool`:

| `0xF0` spec | funcVA | routes to | reaches decode_pool? |
|:--:|:---|:---|:--:|
| 0 | `0x01003370` | `ExtendedInstEngineNop` (`entry; movi a2,0; retw.n` no-op) | no |
| 1 | `0x01003380` | `pool_extended_inst_copy()` (EXACT `.xt.prop`) | no |
| 2 | `0x01003484` | `decode_extended_inst_tensor_tensor_arith()` (`callx8 0x010034b0`) | no |
| **3** | `0x01003a60` | Rand/Cptc band, state `0x470` | **yes — `const16 a0,0xb90`** |
| **4** | `0x010037a8` | Rand/Cptc band, state `0x46c` | **yes — `const16 a0,0xb90`** |

So the base-vs-extended relationship is: **BASE `0x45` → `decode_pool` directly; EXTENDED
`0xF0`/spec{3,4} → `decode_pool` via a thin per-spec trampoline with a private `.bss`
state slot.** `decode_pool` is a *shared operand-decode machine* that three table rows
(idx 3, idx 9, idx 10) re-enter. `[HIGH structure]`

> **GOTCHA — `const16 a0, 0xb90` is a route *setup*, not a call.** Specs 3/4 load
> `0x01000b90` into `a0` (not `a2`/`a8`), then continue inside their FLIX body. Whether
> they `callx0 a0` / fall through into `decode_pool`'s machinery, and which named
> Rand*/Cptc variant the selector byte picks once inside, is in the FLIX-desynced interior
> and **not byte-recovered**. The recovered facts — entry frame (48 vs 32), state slot
> (`0x46c` vs `0x470`), shared `decode_pool@0xb90` route — are OBSERVED; the exact branch
> and the variant name are INFERRED. The full `0xF0` treatment, including the
> RandGetState/RandSetState/Cptc enum alignment, is
> [pool-ext-0xf0.md](../pool/pool-ext-0xf0.md). `[HIGH route / MED variant]`

> **QUIRK — `decode_pool` has no `0xF0` EXTENDED variant of *its own*.** Asked "does
> `decode_pool` have a `0xF0` extended variant, and how does it select?" — the answer is
> *no, and the relationship is inverted*: `decode_pool` is **base-only** (opcode `0x45`,
> spec 0) as a *table row*; it does not register a `0xF0` row. Instead, two of the `0xF0`
> *Rand/Cptc* rows **borrow `decode_pool`'s entry** as a shared decode subroutine. The
> selection that lands a `0xF0` instruction on spec 3 vs 4 is the same packed-key linear
> scan that handles every other opcode — the spec byte is part of the key, nothing special.

---

## 8. Per-generation invariance

The `decode_pool` dispatch path was byte-compared across the three EXTISA_0 generations
present in `libnrtucode.a` (CAYMAN, MARIANA, MARIANA_PLUS). All carved cleanly to 41,568 B.

```
cayman0.so        sha256 910d41c3…   (distinct)
mariana0.so       sha256 9f2ce049…   ┐ MARIANA and MARIANA_PLUS are BYTE-IDENTICAL
mariana_plus0.so  sha256 9f2ce049…   ┘ (same sha256) — one image shared by both gen names
```

**Invariant across all three** (byte-for-byte):

| anchor | value (identical CAYMAN / MARIANA / MARIANA_PLUS) |
|---|---|
| idx-3 table row (`0x7418`) | `00 00 00 45 90 0b 00 01` (opcode `0x45`, spec 0, funcVA `0x01000b90`) |
| trampoline first 10 B (`0xc90`) | `36 41 00 24 00 02 24 58 04 8f` (entry a1,32; state `0x458`; FLIX-start) |
| body first 4 B (`0xcc0`) | `36 41 00 8f` (entry a1,32; FLIX selector) |
| `.xt.prop._Z11decode_poolb` | section [17], file off `0x85bc`, size `0x108` (22 records) |

So the **dispatch/role invariants are stable**: the `(opcode 0x45 → idx3 → 0x01000b90 →
decode_pool@0xbc0)` path, the entry frame, the state slot `0x458`, the `.xt.prop`
section/offset. `[HIGH/OBSERVED]`

> **CORRECTION — the *code* is NOT byte-identical across CAYMAN and MARIANA; only the
> *dispatch geometry* is.** The FLIX bundle *interiors* differ per build. At trampoline
> off `0xc99` (`0xb90 + 9`): CAYMAN reads `8f 32 90 0b 24 00 00 00 21 40`, while
> MARIANA/MARIANA_PLUS read `8f 32 62 03 20 40 00 00 60 14`. And idx 4 (`0x51`) carries a
> build-delta funcVA: CAYMAN `0x0100105c` (`5c 10`) vs MARIANA `0x01001068` (`68 10`).
> So the entry VMA (`0x01000b90`), impl VMA (`0x01000bc0`), state slot (`0x458`), entry
> frame (`a1,32`), table row, and `.xt.prop` section/offset are invariant; the per-build
> FLIX micro-schedule is **not**. "Identical `.xt.prop`" means identical *metadata at the
> same offset*, not identical *code bytes*.

> **NOTE — MAVERICK / v5 is header-only here; its `decode_pool` interior is INFERRED.** The
> EXTISA_0 POOL image for MAVERICK is **not present** in this `libnrtucode.a`, so the
> MAVERICK `decode_pool` body/trampoline is not byte-verified. The
> dispatch arrangement (`0x45 → idx3 → decode_pool`, 8-byte keyed table, packed-key linear
> scan) is **CARRIED** as the expected MAVERICK arrangement from the CAYMAN/MARIANA
> invariance, but its interiors are **INFERRED**, not OBSERVED. Flag every v5 `decode_pool`
> claim accordingly. Earlier-generation SUNDA re-tables the pool path entirely (a
> different `kernel_info_table` VMA, no `0x45` row, no `_Z11decode_poolb` section — the
> Pool op is handled differently there); `decode_pool@(0x45→0xb90→0xbc0)` is the
> CAYMAN/MARIANA/MARIANA_PLUS arrangement. `[CARRIED/INFERRED for v5; SUNDA divergence
> CARRIED]`

---

## 9. Reimplementation checklist

To wire `decode_pool` into a Vision-Q7-compatible POOL engine:

1. **Register one table row** in the `kernel_info_table`: `(opcode = 0x45, spec = 0) →
   funcVA = decode_pool_trampoline`. Pack the key as `(0x45<<24)|(0<<16) = 0x45000000`; the
   dispatch loop's linear scan resolves it (§3). **Do not** add a `0xF0` row for
   `decode_pool` — it is base-only.
2. **Entry trampoline**: `entry a1,32` ; load this kernel's `.bss` state pointer
   (`0x02000458`) ; `callx8` the impl ; `movi.n a2,0` ; `retw.n` (§4).
3. **Impl `decode_pool(bool)`**: decode the 64-byte `S4D4_PL` operand, then `switch` on
   `pool_func @ +36` (`MAX_POOL=1` / `AVG_POOL=2` / else → `"invalid pooling function"`)
   (§6). avg/max are **inline arms**, not separate kernels and not separate table rows.
4. **avg-pool** multiplies by the precomputed `pool_scale` (`= 1/(R·S)`) — no division;
   accumulate in fp32. **max-pool** windowed-reduces with the vector max. (Datapath:
   [avg-max-pool.md](avg-max-pool.md).)
5. **`0xF0` re-use**: if you implement the Rand/Cptc extended band, route its spec-3 and
   spec-4 trampolines through the **same** `decode_pool` entry (`const16 a0, 0xb90`), each
   with its **own** `.bss` state slot (`0x470` / `0x46c`), entry frames `a1,32` / `a1,48`
   respectively (§7).
6. **Two error layers**: the dispatcher emits `"UNKNOWN OPCODE=0x%x"` on a scan miss;
   `decode_pool` emits `"Error, invalid pooling function."` on a bad `pool_func`. Keep them
   distinct (§6).

---

## 10. Adversarial self-verification

Five strongest claims, each re-challenged against the re-carved CAYMAN image
(carve sha256 `910d41c3…b4b55527`, 41,568 B):

| # | Claim | Challenge | Verdict |
|---|---|---|---|
| 1 | **Body `0x01000bc0` = `decode_pool(bool)`** | read `.xt.prop._Z11decode_poolb` first record + `c++filt` | **HOLDS.** Section [17] @ off `0x85bc`; first record `c0 0b 00 01` = VMA `0x01000bc0`, flags `0x2804` (FUNC-START); `c++filt → decode_pool(bool)`. OBSERVED. |
| 2 | **Trampoline `0x01000b90`: `entry a1,32`; state `0x458`; `callx8 0x01000bc0`** | byte-0-aligned xxd of `0xc90` | **HOLDS.** `36 41 00 / 24 00 02 24 58 04 / … / 24 c0 0b e0 02 00 / 0c 02 1d f0`. The "`a1,64`/`callx8 0x6ee8`" reading is a phase-shift artifact (CORRECTION §4). OBSERVED. |
| 3 | **idx-3 row = `00 00 00 45 90 0b 00 01`, reloc @ `0x0200039c`** | xxd off `0x7418`; `readelf -rW` | **HOLDS.** Row bytes exact; key `0x45000000`; funcVA `0x01000b90`; `R_XTENSA_RELATIVE` @ `0x0200039c` (= base+3·8+4); **17** relocs total. OBSERVED. |
| 4 | **`0xF0` spec-3/4 re-enter `decode_pool@0xb90`; specs 0/1/2 do not** | xxd `0x38a8` / `0x3b60`; disasm specs 0–2 | **HOLDS.** spec-4 `36 61 00 … 44 6c 04 … 04 90 0b`; spec-3 `36 41 00 … 44 70 04 … 04 90 0b` (both `const16 a0,0xb90`); spec-0 = no-op, spec-1 = copy start, spec-2 = `callx8 0x34b0` — none route to `0xb90`. OBSERVED. |
| 5 | **Per-gen: idx3/trampoline/body/`.xt.prop` invariant CAYMAN↔MARIANA↔MARIANA_PLUS** | xxd same offsets in all three carves | **HOLDS.** idx3 row, trampoline first 10 B, body first 4 B, `.xt.prop` off/size all identical; FLIX interior `0xc99` and idx4 funcVA differ (build delta). MARIANA == MARIANA_PLUS (same sha). MAVERICK absent → v5 INFERRED. OBSERVED. |

> **CORRECTIONS folded in during self-verify.** (a) The trampoline `entry a1,64 / callx8
> 0x01006ee8` reading is a non-byte-0-aligned objdump artifact; the byte-exact truth is
> `entry a1,32 / callx8 0x01000bc0` (§4). (b) "Identical across generations" applies to the
> *dispatch geometry and `.xt.prop` metadata*, **not** the FLIX code bytes, which differ
> per build (§8). (c) The DEBUG strings are slightly fuller than earlier shorthand:
> `"P%i: Running max_pool with period = %0d and num = %0d."` (with spaces around `=`), at
> `dbg_dram.bin` off `0x1fbf`.

---

## 11. Honesty ledger

**HIGH / OBSERVED (direct byte / section / reloc / string read):**

- `decode_pool` ROLE: it is the Pool **kernel** (idx 3, opcode `0x45`), a leaf called by
  the dispatch loop (`0x01005610`), **not** the loop and **not** its alias — `~0x4a50`
  bytes apart, separate `.xt.prop` coverage, separate DEBUG string clusters.
- Trampoline `0x01000b90` byte-exact (`entry a1,32`; state `0x02000458`; `const16 0x01000bc0`;
  `callx8`; `movi a2,0`; `retw.n`); body `0x01000bc0` = `.xt.prop._Z11decode_poolb` =
  `decode_pool(bool)`; body extent `..0x01000d9a`; 22-record prop set with 8 DATA spans.
- Table linkage: idx-3 row `00 00 00 45 90 0b 00 01`; key `(opcode<<24)|(spec<<16) =
  0x45000000`; funcVA `R_XTENSA_RELATIVE` @ `0x0200039c`; 17 relocs total.
- Base-vs-`0xF0`: `decode_pool` entry reached directly for `0x45` AND via `0xF0` spec-3
  (`@0x3a60`, state `0x470`) / spec-4 (`@0x37a8`, state `0x46c`), both `const16 a0,0xb90`;
  specs 0/1/2 do not route to it.
- Inner `pool_func` switch grounded in the shipped ISA header (`POOL_TYPE {NONE=0,MAX=1,
  AVG=2}`, `pool_func @ +36`, `pool_scale = 1/(R·S) @ +40`); DEBUG arms `max_pool`/
  `avg_pool`/`"invalid pooling function."` — distinct from the dispatcher's `"UNKNOWN
  OPCODE=0x%x"`.
- Per-gen: CAYMAN/MARIANA/MARIANA_PLUS share idx3/trampoline/body/`.xt.prop`; FLIX interior
  + idx4 funcVA differ; MARIANA == MARIANA_PLUS (same sha).

**MED / INFERRED:**

- The in-body FLIX dispatch arms (the `pool_func` runners, period/num loops, the
  max/sum/scale `ivp_*` ops) — FLIX-desynced (`~25%` `.byte`); structure from the prop
  span map + DEBUG strings + the `POOL_TYPE`/`S4D4_PL` header, not byte-pinned.
- The `0xF0` spec-3/4 → {Rand/Cptc} variant **name** (route bytes to `decode_pool` are
  HIGH; the variant name is MED, per the `0xF0` enum-order analysis).
- The `decode_pool(bool)` `bool` arg role (per-instruction decode flag by FW-14/45
  convention — **not** the avg/max selector, which is `pool_func @ +36`).

**LOW / CARRIED-INFERRED:**

- The exact in-body `ivp_*` accumulate sequence (op set INFERRED; slot order not recovered)
  — owned by [avg-max-pool.md](avg-max-pool.md).
- MAVERICK / v5 `decode_pool` interior (EXTISA_0 not in this archive; dispatch arrangement
  CARRIED from CAYMAN/MARIANA, interiors INFERRED).

**DESYNC FLAGS RAISED:** trampoline `0xb99..0xbb0` FLIX interior (only the two `const16`
loads + `callx8` trusted); body `0x01000bc0..0x01000d9a` (8 DATA spans, `~25%` `.byte`);
the `0xF0` spec-3/4 branch past `const16 a0,0xb90`.

---

## See also

- [POOL Engine Main Dispatch Loop](../pool/pool-dispatch.md) — the engine's per-instruction
  dispatch FSM (`0x01005610`) that **calls** `decode_pool`; the function this page
  disambiguates `decode_pool` *from*.
- [`kernel_info_table` Binary Layout](../pool/kernel-info-table.md) — the full 8-byte
  record format and 17-row table; this page documents only the `0x45/idx3` row.
- [POOL Extended-Opcode (`0xF0`) Dispatch](../pool/pool-ext-0xf0.md) — the `0xF0` band whose
  spec-3/4 rows re-enter `decode_pool`'s entry trampoline.
- [avg_pool / max_pool](avg-max-pool.md) *(planned)* — the windowed avg/max **math** inside
  `decode_pool`'s body (the `ivp_*` reduction, the `1/N` scale, the fp32 accumulator).
- [The Opcode Catalog Ledger](opcode-catalog-ledger.md) — the cross-engine opcode inventory
  the `kernel_info_table` map feeds.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the
  OBSERVED/INFERRED/CARRIED × HIGH/MED/LOW tagging used throughout.
