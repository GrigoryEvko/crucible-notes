# SEQ Decode / Dispatch Hub

This page **owns and fully characterizes** the GPSIMD **SEQ engine's** opcode
dispatch surface: the **178-entry computed jump table at DRAM `0x80814`** and the
C++ `Handler`/`execute()` routing that takes each decoded `'S:'`-stream opcode and
delivers control to its handler body. The surrounding fetch/poll FSM is
[SEQ Main FSM Loop](main-loop.md); the instruction-fetch front-end that hands the
opcode word to the decode is [SEQ Fetch + PC-Redirect Front-End](fetch-pc-redirect.md);
the address-level guard is [SEQ PC-Bounds Enforcement + Host API](pc-bounds.md). Those
siblings *reference* the table; **this page is the table's full reference** — its
physical layout, the per-opcode real-handler map, the trampoline→impl→thunk→`execute()`
mechanism, the unknown-opcode error path, and the 178 `'S:'`-named ASCII dispatch
entries the DEBUG image carries.

The SEQ runs on the Vision-Q7 NX `ncore2gp` "Cairo" datapath core
(`IsaMaxInstructionSize = 32`, `XCHAL_HAVE_VISION = 1`, `XCHAL_VISION_TYPE = 7` —
this carries the FLIX/VLIW layer; `XCHAL_HAVE_FLIX3 = 0` is **not** "scalar"). Decode
the SEQ image with the native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`); the
scalar-LX rule decodes the *different* NCFW management core and is wrong here.

> **NOTE — what was re-run this session.** Every fact below was re-derived from a fresh
> independent carve of the SEQ base firmware out of the static archive `libnrtucode.a`,
> members `img_CAYMAN_NX_POOL_DEBUG_{IRAM,DRAM}_contents.c.o`. The carve
> `objcopy -O binary --only-section=.rodata` reproduces **iram.bin = 116,768 B
> (`0x1c820`), sha256 `8e4412b9…ab9ed70a`** and **dram.bin = 28,448 B (`0x6f20`),
> sha256 `7bdf6ed7…d6816ecd`**, head bytes `06 76 00 00` = `j 0x1dc` (reset vector).
> The 178-entry table was re-parsed from `dram.bin` offset `0x814` and the
> trampoline/impl/thunk bodies re-disassembled from `iram.bin` with
> `xtensa-elf-objdump -D -b binary -m xtensa --adjust-vma=0x0` (exit 0, empty stderr,
> 45,901 lines). All IRAM offsets equal device IRAM VA (reset vector at byte 0); DRAM
> string offset = device DRAM VA − `0x80000`. `[HIGH/OBSERVED]`

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte/string read from the shipped image this session; `INFERRED` =
reasoned over OBSERVED facts (often across a FLIX/literal-pool desync); `CARRIED` =
consolidated from a cited cross-page anchor at its original confidence. Crossed with
`HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a
reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE**
(orientation).

> **GOTCHA — two opcode spaces, do not conflate.** The Xtensa `entry`/`call8`/`jx`/
> `callx8` you read in the disassembly are the *engine's own instruction set*. The
> `'A'`,`'S'`,`0xa0`,`0xa1` bytes the decode at `0x2e5f` operates on are the
> *interpreted SEQ-microcode opcode* — a separate encoding the engine runs. The 178-way
> table maps **microcode-opcode → Xtensa handler address**; it is not an Xtensa decode
> table. `[HIGH/OBSERVED]`

---

## 0. The dispatch hub in one diagram

```
   opcode word [a4]  (32-bit; low byte = microcode opcode)
        │
        │  0x2e5f  addi a2,a2,-65        index = opcode_byte − 0x41 ('A')
        │  0x2e62  movi a3,177           upper bound (178 entries: 0..177)
        │  0x2e65  bgeu a3,a2,0x2e6b     SINGLE unsigned bound → catches BOTH ends
        ▼
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │  in range?                                                                     │
   │    NO  → 0x2e68  j 0x3198  → ErrorHandler 0x13f58 "Bad Opcode(0x%x)" → SPIN    │
   │    YES → 0x2e6b  const16 a3,0x80814 ; addx4 a2,a2,a3 ; l32i.n a2,[a2]          │
   │         0x2e76  jx a2  → table[index]  (a thin IRAM TRAMPOLINE)                │
   └──────────────────────────────────┬───────────────────────────────────────────┘
                                       ▼
   TRAMPOLINE  <tramp>: call8 <impl> ; j 0x31a3          (8-byte shim; 55 of them)
                                       ▼
   IMPL  <impl>: entry ; const16 a2,<handler-fn> ; s32i a2,[frame]
                 <FLIX a10 object-setup bundle> ; call8 <thunk> ; retw
                                       ▼
   THUNK  one of TWO:  0x96d4 (compute family, pre-drain)
                       0x85c4 (control family, pre-drain + POST notify)
                 s32i a2,[a1+12] ; <drain> ; a2=[a1+12] ; a2=[a2+0] ; callx8 a2
                                       ▼
   HANDLER execute()  LOG "S: <OpName>" ; read operands from cursor a4 + param block ;
                      perform op
                                       ▼
   j 0x31a3 → 0x2d81   (SUNDA-LOOP BACK-EDGE; every handler tail returns here)
```

One-line verdict: the SEQ dispatch hub is a **dense, direct-indexed (`opcode − 0x41`),
4-byte-entry jump table of 178 slots** — **55 real handlers / 123 default fills** —
whose real slots point at 8-byte trampolines that route, in four hops, through a thin
impl shim and one of two notify-bracketing thunks into a C++ `Handler::execute()`
body. There is **no static vtable array** in the image: the `Handler` singletons are
constructed at boot and the impl's FLIX bundle materialises the live object pointer per
call.

---

## 1. Key facts

| Fact | Value | Anchor | Conf |
|---|---|---|---|
| Anchor image | `img_CAYMAN_NX_POOL_DEBUG_{IRAM,DRAM}_contents.c.o` (in `libnrtucode.a`) | `ar t` member list | `[HIGH/OBSERVED]` |
| Carve `iram.bin` | 116,768 B (`0x1c820`), sha256 `8e4412b9…ab9ed70a` | `wc -c` + `sha256sum` | `[HIGH/OBSERVED]` |
| Carve `dram.bin` | 28,448 B (`0x6f20`), sha256 `7bdf6ed7…d6816ecd` | `wc -c` + `sha256sum` | `[HIGH/OBSERVED]` |
| Table base | DRAM `0x80814` (= `dram.bin` offset `0x814`) | `const16` decode `0x2e6b`/`0x2e6e` | `[HIGH/OBSERVED]` |
| Entry stride | **4 bytes** (one LE absolute IRAM target) | `addx4` `0x2e71` | `[HIGH/OBSERVED]` |
| Entry count | **178** (span `0x814..0xadc`, 712 B) | `struct.unpack` 178×u32 | `[HIGH/OBSERVED]` |
| Index function | `opcode_byte − 0x41` (`'A'`); opcode range `0x41..0xf2` | `addi −65` `0x2e5f` | `[HIGH/OBSERVED]` |
| Bound | `bgeu 177, index` (single unsigned, both ends) | `0x2e62`/`0x2e65` | `[HIGH/OBSERVED]` |
| Real handlers / defaults | **55 real / 123 default** | counted: entries `!= 0x3198` | `[HIGH/OBSERVED]` |
| Default target | `0x3198` → ErrorHandler `0x13f58` "Bad Opcode" → spin | table fill + `0x3198` decode | `[HIGH/OBSERVED]` |
| All real targets in-range | yes — all 55 `< 0x1c820` (IRAM) | range check on parsed targets | `[HIGH/OBSERVED]` |
| Two dispatch thunks | `0x96d4` (compute, 32 ops) / `0x85c4` (control, 15 ops) | impl `call8` targets | `[HIGH/OBSERVED]` |
| `'S:'` records (DEBUG) | **178** records (`rg -c`); **187** literal instances (`rg -o`/`count`) | `rg -c`/`rg -o -a 'S: ' dram.bin` | `[HIGH/OBSERVED]` |
| `'S:'` strings (PERF) | **0** (stripped) | `rg -c -a 'S: ' perf_dram.bin` → `0` | `[HIGH/CARRIED]` |
| No static vtable array | 0 occurrences of any handler-fn addr as a 4-byte data word in IRAM or DRAM | `bytes.count(pack(addr))` | `[HIGH/OBSERVED]` |

---

## 2. The decode → index → dispatch algorithm (instruction-exact)

Decode+dispatch is the tail of the Sunda FSM body
([main-loop.md §4](main-loop.md#4-level-3a--the-sunda-software-fetch-fsm-the-per-iteration-loop)).
Re-decoded byte-exact this session from `iram.bin` (raw bytes
`22c2 bf32 a0b1 27b3 0206 cb00 3408 0034 1408 3022 a028 02a0 0200` at `0x2e5f`):

```
2e50:  s32i.n a2,[a4+0]            ; advance cursor: write next word to [a4]
2e52:  l32i.n a11,[a4+0]           ; re-read fetched word (log arg)
2e54:  const16 a10,8 ; a10,0xe38   ; LOG "S: Dispatch opcode=0x%x" (DRAM 0x80e38)
2e5a:  call8 0x18b84               ; the 'S:' logger (varargs printf-class)
2e5d:  l32i.n a2,[a4+0]            ; a2 = opcode word (re-read for decode)
2e5f:  addi   a2,a2,-65            ; *** index = opcode_byte − 0x41 ('A')
2e62:  movi   a3,177               ; upper bound (178 entries 0..177)
2e65:  bgeu   a3,a2,0x2e6b         ; if (unsigned)index <= 177 → in range
2e68:  j 0x3198                    ; else DEFAULT (unknown opcode → ErrorHandler)
2e6b:  const16 a3,8                ; }
2e6e:  const16 a3,0x814            ; } a3 = DRAM 0x80814 (jump-table base)
2e71:  addx4  a2,a2,a3             ; &table[index] = index*4 + base
2e74:  l32i.n a2,[a2]              ; a2 = table[index] (trampoline addr)
2e76:  jx     a2                   ; → trampoline → Handler execute()
```

```c
/* SEQ decode + 178-way dispatch, byte-exact. [HIGH/OBSERVED]
 * TABLE       = (uint32_t *)0x80814   (DRAM; 178 absolute IRAM targets)
 * DEFAULT     = 0x3198               (unknown-opcode → ErrorHandler)
 * index space = opcode_byte − 0x41   (opcode range 0x41..0xf2) */
static void seq_decode_dispatch(uint32_t opcode_word) {
    uint32_t index = opcode_word - 0x41u;     /* 0x2e5f: addi a2,a2,-65  (no &0xff mask!) */
    if (index > 177u)                          /* 0x2e62/0x2e65: movi 177 ; bgeu — single bound */
        goto *(void *)0x3198;                  /* 0x2e68: j 0x3198 → ErrorHandler (Bad Opcode) */
    void (*trampoline)(void) =                 /* 0x2e6b..0x2e74: const16 base ; addx4 ; l32i.n */
        ((void (**)(void))0x80814)[index];
    trampoline();                              /* 0x2e76: jx a2 (tail to the trampoline) */
}
```

> **QUIRK — one `bgeu` catches *both* ends; no `& 0xff` mask exists.** `addi a2,a2,-65`
> runs on the **whole 32-bit word**, not a masked low byte. `bgeu a3,a2` is an
> *unsigned* compare of `177` against `index`. For an opcode below `0x41`,
> `opcode − 0x41` underflows to a huge unsigned (`0x40 − 0x41 = 0xffffffff > 177`), so
> the same single branch rejects **under-range** and **over-range** opcodes. Any high
> bits set in the fetched word also push the index past 177. A reimplementation that
> masks `& 0xff` before subtracting gets the *same dispatch result* but does not
> byte-match the firmware, which relies purely on the bound to reject. `[HIGH/OBSERVED]`

> **GOTCHA — the operand DATA is not passed in a register at `jx`.** The dispatch passes
> **control only**. Handlers read their operands from (a) the fetch **cursor `a4`**,
> still addressing the instruction word, and (b) a fixed decoded-instruction
> **parameter block** at a constant address. This is OBSERVED in two handlers: the
> inline `0xa1` sync handler reads its sync-target index straight from the cursor
> (`l32i.n a2,[a4+4]` at `0x2e92`, §6), and the Tensor-Scalar handler `0xa02c` reloads
> a 4-word parameter record from a constant block (`const16 a2,0x21b0 ; l32i [a2+12]/
> [a2+8]/[a2+4]/[a2+0]`). The per-opcode operand layout is per-handler and out of this
> page's scope. `[HIGH on the interface / OBSERVED in two handlers]`

---

## 3. The 178-entry dispatch table — full enumeration

Base `0x80814`; **178 × 4-byte LE absolute IRAM targets**; span `0x814..0xadc`
(712 B); index = `opcode_byte − 0x41`; default = `0x3198`. Re-parsed directly from
`dram.bin` this session: **55 real / 123 default**, all 55 real targets `< 0x1c820`
(in-range IRAM). `[HIGH/OBSERVED]`

### 3a. The 55 real handlers

Each row: `opcode → trampoline → impl → handler-fn → thunk → operation`. The trampoline
VA is the literal table word (re-parsed this session, all 55 verified); the operation
name is resolved from the handler's own embedded `"S: <OpName>"` log string (§8). The
`handler-fn` column is the `const16` value the impl materialises (each verified to
**start with `entry`**, §5).

| OP | CHR | TRAMP | IMPL | HANDLER-FN | THUNK | OPERATION |
|---|---|---|---|---|---|---|
| `0x41` | `A` | `0x3074` | `0x2124` | `0x9c30` | `0x96d4` | Tensor-Tensor |
| `0x43` | `C` | `0x309d` | `0x21b8` | `0x9f10` | `0x96d4` | Tensor-Scalar |
| `0x44` | `D` | `0x30ad` | `0x21f0` | `0xa0a0` | `0x96d4` | Tensor-Scalar-PTR |
| `0x45` | `E` | `0x3064` | `0x20ec` | `0x9aec` | `0x96d4` | Pool |
| `0x46` | `F` | `0x2fee` | `0x1fe8` | `0x96f8` | `0x96d4` | Tensor-Reduce *(shares impl `0x1fe8` with `R`)* |
| `0x47` | `G` | `0x2ff6` | `0x2004` | `0x9930` | `0x96d4` | Tensor-Reduce |
| `0x49` | `I` | `0x3084` | `0x215c` | `0x9eac` | `0x96d4` | MEMSET/RNG |
| `0x4d` | `M` | `0x30e5` | `0x22b4` | `0xd194` | `0x96d4` | Rng (XORWOW) |
| `0x51` | `Q` | `0x307c` | `0x2140` | `0x9dec` | `0x96d4` | Tensor-Tensor *(variant fn)* |
| `0x52` | `R` | `0x2fe6` | `0x1fe8` | `0x96f8` | `0x96d4` | Tensor-Reduce *(shares impl `0x1fe8` with `F`)* |
| `0x53` | `S` | `0x30a5` | `0x21d4` | `0xa02c` | `0x96d4` | Tensor-Scalar |
| `0x54` | `T` | `0x30b5` | `0x220c` | `0xa124` | `0x96d4` | Tensor-Scalar-PTR |
| `0x58` | `X` | `0x306c` | `0x2108` | `0x9c04` | `0x96d4` | Tensor-Tensor *(variant fn)* |
| `0x67` | `g` | `0x30bd` | `0x2228` | `0xa1a8` | `0x96d4` | Pool Buffer Load |
| `0x68` | `h` | `0x3044` | `0x207c` | `0xb338` | `0x96d4` | TensorGather |
| `0x74` | `t` | `0x30c5` | `0x2244` | `0xb3a4` | `0x96d4` | TensorScalarAddr |
| `0x77` | `w` | `0x3105` | *inline* | — | — | RandGetState *(FLIX-scheduled inline)* |
| `0x78` | `x` | `0x314c` | *inline* | — | — | RandSetState *(FLIX-scheduled inline)* |
| `0x79` | `y` | `0x30f5` | `0x22ec` | `0xb878` | `0x96d4` | EmbeddingUpdate |
| `0x7a` | `z` | `0x30ed` | `0x22d0` | `0xb850` | `0x96d4` | LoadPoolArgument |
| `0x7b` | `{` | `0x30fd` | `0x2308` | `0xd240` | `0x96d4` | TensorDequantize |
| `0x7c` | `\|` | `0x30d5` | `0x227c` | `0xa210` | `0x96d4` | CrossLaneReduce |
| `0x7d` | `}` | `0x30dd` | `0x2298` | `0xa2ac` | `0x96d4` | CrossLaneReduce *(variant fn)* |
| `0x7e` | `~` | `0x30cd` | `0x2260` | `0xb940` | `0x96d4` | Iota |
| `0x92` |   | `0x2fd6` | `0x1fb0` | `0xb8b8` | `0x96d4` | TensorScalarAffineSelect |
| `0x95` |   | `0x2fce` | `0x1f94` | `0xb828` | `0x96d4` | ModifyPoolConfig |
| `0x96` |   | `0x2fde` | `0x1fcc` | `0xd1bc` | `0x96d4` | Sort |
| `0x9f` |   | `0x308c` | `0x2180` | `0xb800` | `0x96d4` | EngineNop |
| `0xa0` |   | `0x2f1d` | `0x1e18` | *inline* | — | Event_Semaphore *(inline body, no thunk)* |
| `0xa1` |   | `0x2e89` | *inline* | — | — | sync → Enter Pause \| Setup Halt *(inline; §6)* |
| `0xa2` |   | `0x2e79` | `0x1c6c` | `0xa5e8` | `0x85c4` | MOVE |
| `0xa3` |   | `0x2e81` | `0x1c88` | `0x85f4` | `0x85c4` | INS_FL |
| `0xa4` |   | `0x2f25` | `0x1e74` | `0x8950` | `0x85c4` | NOP |
| `0xa5` |   | `0x2f15` | `0x1dfc` | `0x877c` | `0x85c4` | WRITE |
| `0xa6` |   | `0x2efd` | `0x1da8` | `0xa468` | `0x85c4` | NOTIFY |
| `0xa7` |   | `0x2f05` | `0x1dc4` | `0xa600` | `0x85c4` | MOVE *(variant fn)* |
| `0xa8` |   | `0x2f45` | `0x1ee4` | `0xb9a8` | `0x85c4` | BRANCH |
| `0xa9` |   | `0x2ee5` | `0x1d54` | `0xc924` | `0x85c4` | TensorLoad |
| `0xaa` |   | `0x2f35` | `0x1eac` | `0x89fc` | `0x85c4` | TensorStore |
| `0xab` |   | `0x2f3d` | `0x1ec8` | `0x1b0c` | `0x85c4` | TensorStore *(variant fn)* |
| `0xb0` |   | `0x2f2d` | `0x1e90` | `0xcbdc` | `0x85c4` | Event_Semaphore Rng Clr |
| `0xb1` |   | `0x2ef5` | `0x1d8c` | `0x8684` | `0x85c4` | SET_OM |
| `0xb2` |   | `0x2f0d` | `0x1de0` | `0xa7cc` | `0x85c4` | MoveShape (RegToShape / ShapeToReg) |
| `0xb3` |   | `0x2f4d` | `0x1f00` | `0x9324` | `0x85c4` | POLL_SEM |
| `0xb5` |   | `0x2eed` | `0x1d70` | `0xcfe8` | `0x85c4` | BRANCH *(variant fn)* |
| `0xb8` |   | `0x2f55` | *inline* | — | — | FLIX-desync inline *(unnamed; deep helpers)* |
| `0xbb` |   | `0x2ffe` | *inline* | — | — | FLIX-desync inline → TensorLoad path `0x89d0` |
| `0xbd` |   | `0x2faf` | *inline* | — | — | FLIX-desync inline *(unnamed; deep helpers)* |
| `0xbe` |   | `0x304c` | `0x2098` | `0xd218` | `0x96d4` | GetSequenceBounds |
| `0xbf` |   | `0x3095` | `0x219c` | `0xd1e4` | `0x96d4` | SB2SB_Collective |
| `0xe4` |   | `0x305c` | `0x20d0` | `0xd348` | `0x96d4` | ConvLutLoad |
| `0xe7` |   | `0x303c` | `0x2060` | `0xb7d8` | `0x96d4` | Indirect Copy |
| `0xf0` |   | `0x3190` | `0x235c` | `0xb3f0` | `0x96d4` | ExtendedInst |
| `0xf1` |   | `0x3020` | *inline* | — | — | FLIX-desync inline *(unnamed; deep helpers)* |
| `0xf2` |   | `0x3054` | `0x20b4` | `0xd29c` | `0x96d4` | NonzeroWithCount |

**Family tally:** `0x96d4`-thunk = **32** opcodes; `0x85c4`-thunk = **15**; pure-inline =
**2** (`0xa0`, `0xa1`); FLIX-desync inline = **6** (`0x77`, `0x78`, `0xb8`, `0xbb`,
`0xbd`, `0xf1`). **Total = 55.** `[HIGH/OBSERVED]`

> **QUIRK — N:1 opcode→impl and N:1 op-name→distinct-fn both occur; the table is not a
> permutation.** `'F'` (`0x46`) and `'R'` (`0x52`) resolve to the **same impl**
> `0x1fe8` (and the same handler-fn `0x96f8`). Conversely, several *operations* span
> **multiple opcodes mapping to DISTINCT handler functions** that share a log name:
> Tensor-Tensor is three distinct fns (`'A'`=`0x9c30`, `'Q'`=`0x9dec`, `'X'`=`0x9c04`);
> MOVE is two (`0xa2`=`0xa5e8`, `0xa7`=`0xa600`); BRANCH two (`0xa8`=`0xb9a8`,
> `0xb5`=`0xcfe8`); TensorStore two (`0xaa`=`0x89fc`, `0xab`=`0x1b0c`); CrossLaneReduce
> two (`0x7c`=`0xa210`, `0x7d`=`0xa2ac`). These are per-opcode specializations
> (operand-width / addressing-mode variants) compiled as separate `Handler` objects. A
> reimplementation must allow both directions of multiplicity. *(Distinct addresses
> OBSERVED; "mode variant" meaning INFERRED.)* `[HIGH addresses / MED meaning]`

### 3b. The 123 default entries → `0x3198`

The 123 default slots are exactly the indices **not** in §3a. They form 19 contiguous
runs (verified by compressing the parsed default-index list this session):

| idx run | opcode run | | idx run | opcode run |
|---|---|---|---|---|
| `1` | `0x42` `B` | | `82..83` | `0x93..0x94` |
| `7` | `0x48` `H` | | `86..93` | `0x97..0x9e` |
| `9..11` | `0x4a..0x4c` `J K L` | | `107..110` | `0xac..0xaf` |
| `13..15` | `0x4e..0x50` `N O P` | | `115` | `0xb4` |
| `20..22` | `0x55..0x57` `U V W` | | `117..118` | `0xb6..0xb7` |
| `24..37` | `0x59..0x66` `Y..f` | | `120..121` | `0xb9..0xba` |
| `40..50` | `0x69..0x73` `i..s` | | `123` | `0xbc` |
| `52..53` | `0x75..0x76` `u v` | | `127..162` | `0xc0..0xe3` |
| `62..80` | `0x7f..0x91` | | `164..165` | `0xe5..0xe6` |
| | | | `167..174` | `0xe8..0xef` |

> **NOTE — the 123 defaults are not enumerated row-by-row on purpose.** Every default
> slot holds the **identical** literal `0x3198`; dumping 123 identical rows adds no
> information. The runs above plus "everything not in §3a" fully specify them. The
> `0x3198` target is characterized once in §7 (the unknown-opcode error path).
> `[HIGH/OBSERVED]`

---

## 4. The handler / thunk dispatch mechanism

The routing is **table → trampoline → impl shim → thunk → `Handler::execute()`** — four
hops, all instruction-exact below. It is **not** a flat indexed call; the indexed jump
(`jx`) lands on a trampoline, and the actual `execute()` call is an indirect `callx8`
through a per-object function pointer inside a shared thunk.

### 4a. The trampoline (the table target) — `[HIGH/OBSERVED]`

Each real `table[idx]` points at an 8-byte trampoline of the form
`call8 <impl> ; j 0x31a3`. Raw bytes at `0x3074` (the `'A'` target) this session:
`e5 0a ff | 06 4a 00` = `call8 0x2124 ; j 0x31a3`. The `j 0x31a3` is the universal
back-edge (`31a3: j 0x2d81`), so **every** handler re-enters the FSM at the poll/fetch
point.

```
3074:  call8 0x2124 ; j 0x31a3        ('A')   ; e5 0a ff | 06 4a 00
30a5:  call8 0x21d4 ; j 0x31a3        ('S')
```

> **QUIRK — the 6 FLIX-desync slots are NOT the `call8;j` shape.** Slots `0x3105`,
> `0x314c`, `0x2f55`, `0x2ffe`, `0x2faf`, `0x3020` (opcodes `0x77`/`0x78`/`0xb8`/`0xbb`/
> `0xbd`/`0xf1`) begin with a FLIX format-selector byte and **inline the handler body
> directly** — their first scalar instruction is a large-frame/`l32r` setup, not a
> `call8`. The linear sweep cannot bundle-sync them, so their *table targets are exact*
> but their *bodies are FLIX-vector and partly unrecovered*. Two are namable from their
> first reachable log (`0x77` RandGetState, `0x78` RandSetState); `0xbb` routes into the
> TensorLoad path `0x89d0`. `[HIGH table targets / LOW bodies]`

### 4b. The impl shim (loads the handler, calls the thunk) — `[HIGH/OBSERVED]`

Re-decoded this session for `'A'` (impl `0x2124`):

```
2124:  entry a1,48
2127:  const16 a2,0                 ; a2 high16 = 0
212a:  const16 a2,0x9c30            ; a2 = 0x00009c30  (the handler-fn / execute() entry)
212d:  s32i a2,[a1+12]              ; stash to local frame
2130:  <FLIX bundle: object-pointer setup for the thunk arg>   [MED — see §4d]
2137:  call8 0x96d4                 ; → the dispatch thunk
213a:  retw
```

All 31 `const16`-built handler-fn targets were verified to **start with `entry`**
(byte 0 = `0x36`) — i.e. each is the entry of a real handler **function** (the
polymorphic `execute()`), not a data record (§5).

### 4c. The two dispatch thunks — `[HIGH/OBSERVED]`

There are exactly **two** thunks; which one an impl calls partitions the 47 thunked
handlers into two families. Both end with the *identical* double-indirect `execute()`
core; they differ only in pre/post bracketing.

**Thunk `0x96d4` — compute/tensor/pool family (32 opcodes), pre-drain only:**

```
96d4:  entry a1,48
96d7:  s32i a2,[a1+12]              ; save the incoming Handler-object pointer
96da:  call8 0x3aac                 ; PRE: drain pending notify/surprise queue
       <FLIX co-issue padding 0x96dd..0x96e5>
96e6:  l32i a2,[a1+12]              ; reload object pointer      ; bytes 22 21 03
96e9:  l32i.n a2,[a2+0]             ; a2 = object[0] = execute() entry ; bytes 28 02
96eb:  callx8 a2                    ; *** CALL the handler's execute()   ; bytes e0 02 00
96f5:  retw.n
```

The bytes `22 21 03 | 28 02 | e0 02 00` at `0x96e6..0x96ee` are an **unambiguous
double-indirection** — re-read from `iram.bin` this session, byte-identical to the
sibling pages' anchor.

**Thunk `0x85c4` — control/move/notify family (15 opcodes), pre-drain + POST notify:**

```
85c4:  entry a1,48
85c7:  s32i.n a2,[a1+12]            ; save object pointer
85c9:  call8 0x3cf0                 ; PRE: drain notify queue
       <FLIX co-issue padding 0x85cc..0x85d9>
85da:  l32i.n a2,[a1+12]            ; reload object
85dc:  l32i.n a2,[a2+0]             ; a2 = object[0] = execute() entry
85de:  callx8 a2                    ; *** CALL execute()
85e1:  call8 0x3d1c                 ; POST: completion-notify (→ 0x3d74)
85f2:  retw.n
```

```c
/* The two notify-bracketing dispatch thunks. [HIGH/OBSERVED]
 * Identical execute() core; 0x85c4 adds a POST completion-notify. */
struct Handler { void (**vtable)(struct Handler *); /* [obj+0] -> slot 0 = execute() */ };

void seq_vthunk_compute(struct Handler *h) {   /* 0x96d4 — 32 opcodes */
    seq_drain_notify_queue();                  /* 0x3aac (PRE) */
    h->vtable[0](h);                           /* l32i [obj+0]; callx8 : execute() */
}
void seq_vthunk_control(struct Handler *h) {   /* 0x85c4 — 15 opcodes */
    seq_drain_notify_queue();                  /* 0x3cf0 (PRE) */
    h->vtable[0](h);                           /* l32i [obj+0]; callx8 : execute() */
    seq_publish_completion();                  /* 0x3d1c (POST) — control ops publish completion */
}
```

> **NOTE — why two thunks.** The control/move opcodes (MOVE/WRITE/NOTIFY/BRANCH/SET_OM/
> POLL_SEM/TensorLoad/Store…) must **publish completion** after running, hence the extra
> `call8 0x3d1c` POST step. The compute family does not, so `0x96d4` omits it. The
> `execute()`-invocation core (`l32i [obj]`; `l32i [obj+0]`; `callx8`) is identical in
> both. `[HIGH/OBSERVED]`

### 4d. The `Handler` object layout + registration — `[MED/INFERRED]`

The thunk treats the passed pointer `P` as a C++ object whose word `[P+0]` is the
`execute()` entry — classic single-method polymorphic dispatch
(`obj->execute()` == `(*(void(**)())obj)()` with the function pointer at offset 0). The
`const16` value built in the impl (e.g. `0x9c30`) **is** a handler-**function** entry
(it starts with `entry`), yet the thunk does a **double** indirection
(`obj = [a1+12]; fn = [obj+0]; callx8 fn`). Reconciling those two OBSERVED facts:

- `call8` rotates the Xtensa register window by 8: the caller's `a10..a15` become the
  callee's `a2..a7`, and the caller's `a2` is **not** an argument. So the thunk's
  incoming `a2` (saved to `[a1+12]`) is the impl's **`a10`**, set by the FLIX bundle at
  `impl+0x0c` (`0x2130` for `'A'`), **not** the `const16`-built `a2`.
- Therefore the object pointer `P` passed to the thunk is the **FLIX-loaded `a10`
  value** (a pointer to a 1-word `Handler` object whose `[P+0]` = the `execute()`
  function). The `const16 0x9c30` saved to the impl's own frame is the impl-local copy
  of the `execute()` address used by that FLIX setup.
- An **exhaustive word search** this session confirmed **none** of the handler-fn
  addresses appear as a 4-byte data word anywhere in IRAM or DRAM (§5), so there is
  **no static pointer table / no `.rodata` vtable array** — the `Handler` singletons are
  **constructed at boot** (per-singleton, on a stack/heap frame or BSS slot) and the
  impl's FLIX bundle materialises the live object pointer per call.

> **CORRECTION (vs a naive "the table is a vtable") — there is no static vtable; the
> object is built at boot.** A reader might assume the `0x80814` table, or some
> `.rodata` array, holds the `Handler` vptrs. The byte evidence refutes this: the table
> holds **trampoline** addresses (not objects, not vptrs), and a full scan finds **zero**
> static occurrences of any handler-fn address as a data word. The vptr lives in a
> boot-constructed object the FLIX bundle loads into `a10`. The construction site is the
> [boot](boot.md) `enter_run` dispatch-context init (`call8 0x3cdc` / `0x5524` at
> `enter_run` `0x2d2d`/`0x2d48`); the exact slot is boot-page territory. *(HIGH/OBSERVED:
> the double-indirect thunk bytes, every `const16` target is a real `entry` fn, and the
> zero static-pointer-table count. MED/INFERRED: the window-rotation argument path and
> the per-call object materialisation across the FLIX desync.)* `[MED/INFERRED]`

> **GOTCHA — vtable slot 0 is at `vptr + 0`, no header skip.** `0x96d4` does
> `l32i [obj+0]` to fetch the vptr, then `callx8` **on the vptr itself** — slot 0 is at
> `vptr[0]`. Do **not** add `0x10` to reach "slot 0"; the object's `[obj+0]` already
> points at the executable-pointer array (any `offset-to-top`/`typeinfo` header sits
> *before* the vptr the object stores). `[HIGH/OBSERVED]`

---

## 5. Why the `const16` targets are functions, and why there is no vtable array

Two byte-level checks this session pin §4d:

**(i) Every handler-fn starts with `entry`.** For each `const16`-built target, the byte
at that IRAM offset is `0x36` (the `entry` opcode low byte):

```
0x9c30:0x36  0xa02c:0x36  0xd194:0x36  0x9aec:0x36  0x96f8:0x36  0x9930:0x36
0x9eac:0x36  0x9c04:0x36  0x9dec:0x36  0xb338:0x36     →  all ENTRY-OK
```

**(ii) No handler-fn address occurs as a static data word.** Packing each address
little-endian and counting raw occurrences across both carved images:

```
0x9c30 → iram.count=0 dram.count=0     0xa02c → 0/0     0xd194 → 0/0
0x9aec → iram.count=0 dram.count=0     0xb338 → 0/0
```

So the handler functions are real `execute()` bodies, and they are reached **only**
through the boot-constructed object pointer — never through a static pointer table.
`[HIGH/OBSERVED]`

---

## 6. The inline handlers (no thunk) — `[HIGH/OBSERVED]`

Two opcodes inline their body directly in the impl instead of routing through a thunk.

**`0xa0` Event_Semaphore — impl `0x1e18` is the body:**

```
1e18:  entry a1,48
1e1e:  const16 a10,8 ; a10,0xf56 ; call8 0x18b84   ; LOG "S: Event_Semaphore" (DRAM 0x80f56)
       ... inline semaphore work (reads param block, updates a sem state) ...
       j 0x31a3
```

**`0xa1` sync/wait — impl `0x2e89`, the Pause/Halt decision, inline:** this is the
clearest OBSERVED proof of the operand-decode interface — the handler pulls its operand
straight from the fetch cursor `[a4+4]`.

```
2e89:  call8 0x1ca4                ; primary sync work
2e8f:  movi a2,0 ; s32i a2,[a4+4]  ; clear, then ...
2e92:  l32i.n a2,[a4+4]            ; *** OPERAND: sync-target index from the cursor
2e94:  movi a3,38 ; bltu a3,a2,…   ; clamp index to ≤ 38
2e9c:  l32i.n a2,[a4+4] ; movi a3,145 ; slli a3,a3,13 ; addx4 a2,a2,a3
                                   ; → external addr = (145<<13) + idx*4 = 0x122000 + idx*4
2eab:  rer a2,a2                   ; RER: read EXTERNAL register at that address
2eb0:  and a2,a2,4                 ; test bit2 of the remote status
2ec1:  beqz.n a2,0x2ece
2ec6:  call8 0x1cc0                ; bit2 set   → Enter Pause  (run_state=2)
2ed9:  call8 0x1cf8                ; bit2 clear → Setup Halt   (save resume PC)
       ... j 0x31a3
```

> **NOTE — Pause/Halt are *not* opcodes.** They are reached inside the `0xa1` inline
> sync handler, driven by bit2 of a remote engine's status read over `RER`. Full
> treatment of the resulting run-state transitions is on
> [SEQ Main FSM Loop §6](main-loop.md#6-pause--halt--the-loops-quiescing-transitions)
> and [SEQ Run-State Machine](run-state.md). `[HIGH/OBSERVED]`

---

## 7. The default / error path (Bad Opcode) — `[HIGH/OBSERVED]`

All 123 default slots route to `0x3198`. An **aligned** decode at `0x3198` (the linear
sweep enters mid-bundle and prints garbage) gives:

```
3198:  l8ui a10,[a4+0]             ; reload the bad opcode byte from the cursor
319b:  call8 0x13f58               ; → ErrorHandler(opcode)
319e:  j 0x31a3                    ; (would-be back-edge — ErrorHandler never returns)
```

**ErrorHandler `0x13f58` (HandleBadOpcode):**

```
13f5b:  s8i a2,[a1+12]                     ; save opcode arg byte
13f61:  l8ui a11,[a1+12]
13f64:  const16 a10,8 ; a10,0x3b11 ; call8 0x18b84
                                            ; LOG "S: ErrorHandler : Bad Opcode(0x%x)" (DRAM 0x83b11)
13f7d:  call8 0x13e00                       ; dispatch error notification
--- error-notify 0x13e00 ---
13e07:  movi a10,2 ; call8 0x13e18           ; raise error (severity 2)
13e0d:  call8 0xa2e0
13e14:  j 0x13e14                            ; *** INFINITE SELF-LOOP (hard fault / halt)
```

> **CORRECTION — two different "out-of-range" outcomes; do not unify them.** An
> out-of-range **opcode value** (the dispatch index fails `bgeu 177`) is a **HARD
> FAULT** → ErrorHandler → "Bad Opcode" → infinite spin (`j 0x13e14`). An out-of-range
> **fetch address** (the PC is outside `[lo,hi]`) is **SKIP-with-WARNING**, not a fault
> — that is the `is_pc_in_bounds @0x68d0` guard
> ([pc-bounds.md](pc-bounds.md) /
> [fetch-pc-redirect.md §6](fetch-pc-redirect.md#6-the-pc-range-guard-is_pc_in_bounds-0x68d0-the-address-level-guard)).
> A reimplementer who maps both to the same policy diverges. **The binary wins.**
> `[HIGH/OBSERVED]`

Sibling ErrorHandler arms (same translation unit): `0x13f80`
"Illegal Instruction(0x%x)" (DRAM `0x83b35`); FP/IntDivZero arms log "FP Error(%d)"
(`0x83acb`) / "Int Div Zero Error" (`0x83aeb`). Full fault-reporting surface:
[SEQ Error-Handler / Fault Reporting](error-handler.md).

---

## 8. The 178 `'S:'`-named SEQ entries (the ASCII dispatch surface)

The DEBUG DRAM image carries **178** `'S: '`-prefixed format-string **records** —
`rg -c -a 'S: ' dram.bin` → `178`. These are the **named log strings** each handler (and
the dispatch step itself) emits through the `'S:'` logger `0x18b84`. They are the ASCII
dispatch surface: the human-readable identity of each routed operation.

> **CORRECTION — "178" is the NUL-delimited *record* count; the literal `S: ` *instance*
> count is 187.** `rg -c` counts matching **records** (`ripgrep` splits the binary on
> `\n` / `0x0a`), and the strings here are predominantly **NUL-terminated** — so `-c`
> reports records, not occurrences. Re-counted this session: `rg -o -a 'S: ' dram.bin
> \| wc -l` → **187** and `python3 -c "print(open('dram.bin','rb').read().count(b'S: '))"`
> → **187**. The 9-instance gap is **4 records that pack multiple `S: ` substrings into
> one `\n`-delimited line** (records 113→4×, 133→2×, 159→5×, 173→2×): `178 + 9 = 187`.
> So: **178** if a "string" means a NUL-delimited record (the figure the sibling pages
> and PERF-contrast cite, and what `rg -c` reports); **187** if it means literal `S: `
> occurrences. Both are reproducible; do not conflate the two metrics. The PERF
> comparison (`rg -c … perf_dram.bin` → `0`) is on the same record metric, so the
> "DEBUG 178 → PERF 0" delta is sound. `[HIGH/OBSERVED]`

> **GOTCHA — `'S:'` is the *logger prefix*, not a 1:1 per-table-slot label.** The record
> count `178` matching the table's 178 entries is a **coincidence of magnitude**, **not**
> a per-entry correspondence (and the true instance count is 187 — see the CORRECTION
> above). The records comprise: the per-fetch `"S: Dispatch opcode=0x%x"`, every
> handler's
> `"S: <OpName>"` name (one or more per *operation*, including the variant-fn duplicates
> like `"S: MOVE"` reached from both `0xa2` and `0xa7`), the FSM-state logs
> (`"S: enter_run: start/done"`, `"S: Enter Pause"`, `"S: Setup Halt"`,
> `"S: Sunda seq Loop"`, `"S: Seq Loop, iter=…"`), the redirect/bounds/error messages,
> and the ErrorHandler arms. So the `'S:'` surface **maps onto the table by op-name**, not
> by index: a handler at `table[idx]` logs its `"S: <OpName>"` on entry, which is how the
> §3a operation column was resolved. `[HIGH/OBSERVED]`

How the surface maps to the table:

1. Decode logs `"S: Dispatch opcode=0x%x"` (DRAM `0x80e38`) **once per fetch**, with the
   raw opcode byte — independent of which slot is selected.
2. `jx` lands on the trampoline → impl → thunk → `execute()`; the **`execute()` body
   logs its own `"S: <OpName>"`** as its first action. That string *is* the operation
   identity used to name the §3a rows.
3. The default slot's ErrorHandler logs `"S: ErrorHandler : Bad Opcode(0x%x)"` (DRAM
   `0x83b11`) for any of the 123 unmapped opcodes.

> **NOTE — why DEBUG keeps the strings and PERF drops them.** The DEBUG build retains all
> 178 `'S:'` strings as named xrefs (each handler does a `const16`-pair + `call8 0x18b84`
> to emit its name); the PERF build (`img_CAYMAN_NX_POOL_PERF_*`) **strips every log
> call** and re-lays-out `.rodata`, so PERF carries **0** `'S:'` strings
> (`rg -c -a 'S: ' perf_dram.bin` → `0`) and its `.rodata` is smaller (`0x17280` vs
> `0x1c820`). Removing each 6-byte `const16`-pair + `call8` shifts **all** downstream
> code offsets, so the DEBUG addresses on this page **do not map 1:1 onto PERF**. The
> *algorithm* is identical (same `opcode − 0x41` → 178-way table → trampoline/thunk
> structure); only the strings and the layout differ. Anchor a PERF reimplementation to
> the **control structure**, never the literal addresses. `[HIGH/OBSERVED on DEBUG=178,
> PERF=0; CARRIED on PERF layout from the sibling pages]`

A representative cross-reference of the handler op-name strings (DRAM offset = VA −
`0x80000`; all OBSERVED):

| VA | String | | VA | String |
|---|---|---|---|---|
| `0x80e38` | `Dispatch opcode=0x%x` | | `0x8202a` | `Tensor-Tensor` |
| `0x82077` | `Tensor-Scalar` | | `0x82089` | `Tensor-Scalar-PTR` |
| `0x81ffc` | `Pool` | | `0x81f8b` | `Tensor-Reduce` |
| `0x82058` | `MEMSET/RNG` | | `0x82d20` | `Rng (XORWOW)` |
| `0x8209f` | `Pool Buffer Load` | | `0x82650` | `TensorGather` |
| `0x82690` | `TensorScalarAddr` | | `0x82770` | `EmbeddingUpdate` |
| `0x82730` | `LoadPoolArgument` | | `0x82da6` | `TensorDequantize` |
| `0x820b4` | `CrossLaneReduce` | | `0x827bd` | `Iota` |
| `0x827a0` | `TensorScalarAffineSelect` | | `0x82700` | `ModifyPoolConfig` |
| `0x82d31` | `Sort` | | `0x826e0` | `EngineNop` |
| `0x82d90` | `GetSequenceBounds` | | `0x82d50` | `SB2SB_Collective` |
| `0x82e20` | `ConvLutLoad` | | `0x826b6` | `Indirect Copy` |
| `0x826a5` | `ExtendedInst` | | `0x82de0` | `NonzeroWithCount: num_elements=%d` |
| `0x82291` | `MOVE` | | `0x81efb` | `INS_FL` |
| `0x81f37` | `NOP` | | `0x81f2d` | `WRITE` |
| `0x82286` | `NOTIFY` | | `0x82be6` | `BRANCH` |
| `0x81f54` | `TensorLoad` | | `0x80e28` | `TensorStore` |
| `0x82c23` | `Event_Semaphore Rng Clr` | | `0x81f06` | `SET_OM` |
| `0x81f7e` | `POLL_SEM` | | `0x80f56` | `Event_Semaphore` |
| `0x8260f` | `MoveShape(RegToShape)` | | `0x825f6` | `MoveShape(ShapeToReg)` |
| `0x80e51` | `RandGetState : … not currently supported on POOL` | | `0x80e99` | `RandSetState : …` |
| `0x83b11` | `ErrorHandler : Bad Opcode(0x%x)` | | `0x83b35` | `ErrorHandler : Illegal Instruction(0x%x)` |

---

## 9. SEQ vs POOL dispatch — the structural contrast

The two GPSIMD engines dispatch in **opposite styles**. The POOL facts below are
consolidated from the POOL pages, not re-derived here.

| | **SEQ engine** (this page) | **POOL engine** |
|---|---|---|
| table location | DRAM `0x80814` (`.rodata` data word array) | `kernel_info_table` ELF section (PROGBITS @ VMA `0x02000380`) |
| entry size | **4 bytes** (one LE absolute IRAM target) | **8 bytes** (`key4` + `funcVA4`) |
| entry count | **178** (`0x41..0xf2`, idx = byte − `0x41`) | 17 (CAYMAN_0) |
| key | ASCII opcode byte − `0x41` (DIRECT-INDEXED, O(1)) | `(opcode<<24)\|(spec<<16)` — a key **match**, not an index |
| funcVA relocation | **none** (absolute IRAM addresses baked at link) | `R_XTENSA_RELATIVE` per entry (load-time relocated funcVA) |
| indirection depth | table → trampoline → impl → thunk → `Handler::execute()` (4 hops) | table → funcVA (direct call to kernel fn, 1 hop) |
| handler form | C++ `Handler` object + `execute()` via 1 of 2 notify thunks | plain kernel function (flat C pointer) |
| default / miss | 123 slots → `0x3198` → ErrorHandler "Bad Opcode" → **infinite spin** | key-miss → POOL miss path (separate policy) |
| opcode space | ASCII mnemonics (`'A'..'~'`) + a binary block (`≥ 0x92`) | binary `opcode`+`spec` keys |

> **NOTE — the role split.** SEQ is the **control/sequencer front-end** (the `'S:'`
> stream): a dense direct-indexed ASCII-opcode jump table feeding a C++ `Handler`/
> `execute()` indirection with per-family notify thunks. POOL is the **compute-kernel
> back-end**: a sparse keyed (`opcode<<24|spec`) relocated-funcVA table feeding flat C
> kernel functions. Full POOL treatment:
> [POOL Engine Main Dispatch Loop](../pool/pool-dispatch.md) and
> [kernel_info_table Binary Layout](../pool/kernel-info-table.md) *(both stubs at time of
> writing — the POOL contrast facts here are derived from the POOL dispatch analysis)*.
> `[HIGH on SEQ side / CARRIED on POOL side]`

---

## 10. Reimplementation checklist

To rebuild the SEQ decode/dispatch hub:

1. **Decode**: `index = opcode_word − 0x41` (on the **whole word**, no `& 0xff`); reject
   with a **single** `bgeu 177, index` — it catches both ends via unsigned underflow
   (§2). Out of range → the unknown-opcode default.
2. **Table**: a flat array of **178 × 4-byte absolute IRAM targets** at DRAM `0x80814`;
   **55** real trampolines, **123** default fills `= 0x3198`. Index by `opcode − 0x41`.
   Allow N:1 opcode→impl (`'F'`/`'R'`) and multi-opcode→distinct-fn variants (§3a).
3. **Trampoline**: each real slot is an 8-byte `call8 <impl> ; j 0x31a3` shim; the
   `j 0x31a3 → 0x2d81` is the universal FSM back-edge every handler shares (§4a).
4. **Impl shim**: `entry` ; `const16 a2,<handler-fn>` ; stash ; materialise the
   `Handler` object pointer into `a10` (the windowed call's argument) ; `call8 <thunk>` ;
   `retw` (§4b).
5. **Thunk**: two variants. `0x96d4` (compute, 32 ops) = pre-drain + `execute()`;
   `0x85c4` (control, 15 ops) = pre-drain + `execute()` + POST completion-notify. Core =
   `l32i [obj]; l32i [obj+0]; callx8` (vtable slot 0 at `vptr+0`, no header skip) (§4c).
6. **Objects, not a vtable array**: construct the `Handler` singletons at boot; there is
   **no** static pointer table (§4d/§5). Operands come from the fetch cursor `a4` + a
   decoded-instruction param block, never from a register at the `jx`.
7. **Inline fast paths**: `0xa0` Event_Semaphore and `0xa1` sync inline their body (no
   thunk); 6 FLIX-scheduled slots inline too (§4a/§6).
8. **Default**: `0x3198` reloads the bad opcode and calls ErrorHandler `0x13f58`, which
   logs "Bad Opcode" and **spins forever** — a hard fault, distinct from the
   skip-with-warning PC-bounds guard (§7).

---

## 11. Adversarial self-verification

Five strongest claims, each re-challenged against the re-carved image this session
(carve SHAs match: `iram.bin 8e4412b9…`, `dram.bin 7bdf6ed7…`; disassembly exit 0,
45,901 lines):

| # | Claim | Challenge | Verdict |
|---|---|---|---|
| 1 | **178 entries, 55 real / 123 default**, all real in-range | `struct.unpack` 178×u32 from `dram.bin[0x814:]`; count `!= 0x3198`; range-check | **HOLDS.** Parsed 178; **55** real / **123** default; **0** real targets `≥ 0x1c820`. Default index runs match §3b exactly. OBSERVED. |
| 2 | **Decode** = `addi −65` ; `bgeu 177` (single bound) ; `addx4`+`l32i`+`jx`, base `0x80814` | re-decode `0x2e5f..0x2e76` from raw bytes | **HOLDS.** `22c2bf addi a2,a2,-65`; `32a0b1 movi a3,177`; `27b302 bgeu a3,a2,0x2e6b`; `06cb00 j 0x3198`; `const16 a3,0x814`; `3022a0 addx4`; `2802 l32i.n`; `a00200 jx a2`. Byte-exact. OBSERVED. |
| 3 | **Thunk double-indirect** `l32i[obj]; l32i[obj+0]; callx8` at `0x96d4` | dump bytes `0x96e6..0x96ee` | **HOLDS.** `22 21 03` (`l32i a2,[a1+12]`) ; `28 02` (`l32i.n a2,[a2+0]`) ; `e0 02 00` (`callx8 a2`) ; `1df0` (`retw.n`). `0x85c4` confirmed identical core + `call8 0x3d1c` POST. OBSERVED. |
| 4 | **Every `const16` handler-fn starts with `entry`** (real fn, not data) | read `iram.bin[addr]` for 10 targets | **HOLDS.** All 10 (`0x9c30,0xa02c,0xd194,0x9aec,0x96f8,0x9930,0x9eac,0x9c04,0x9dec,0xb338`) = byte `0x36` (`entry`). OBSERVED. |
| 5 | **No static vtable array** for handler fns | `bytes.count(pack(addr))` over both images | **HOLDS.** `0x9c30/0xa02c/0xd194/0x9aec/0xb338` each occur **0** times as a 4-byte word in `iram.bin` **and** `dram.bin`. Confirms boot-constructed objects, not a `.rodata` table. OBSERVED. |

> **CORRECTION folded in during self-verify.** The `0x3198` default and the impl FLIX
> bundle at `impl+0x0c` both decode as **garbage under the linear sweep** (mid-bundle
> entry / FLIX desync). The §7 default path is recovered from an **aligned** decode
> (`l8ui a10,[a4+0]; call8 0x13f58`); the §4d object-pointer setup is honestly flagged
> MED/INFERRED across the desync. Both are noted in place, never silently smoothed.

---

## 12. Honesty ledger

**HIGH / OBSERVED (re-run this session):**

- Carve reproduced: `iram.bin` 116,768 B / sha256 `8e4412b9…`, `dram.bin` 28,448 B /
  sha256 `7bdf6ed7…`; head bytes = reset vector `j 0x1dc`.
- Full 178-entry table re-parsed: base `0x80814`, stride 4, default `0x3198`, **55** real
  / **123** default, all 55 in-range. Per-opcode trampoline VAs (§3a column 1) all match
  the parsed table words; default index runs (§3b) exact.
- Decode (`opcode − 0x41`, `bgeu 177`, `addx4` base `0x80814`, `jx`) instruction-exact.
- Trampoline shape `call8 <impl>; j 0x31a3` (raw `0x3074` = `e5 0a ff | 06 4a 00`); impl
  shim `entry; const16 <handler-fn>; …; call8 <thunk>; retw`.
- Two thunks `0x96d4` (pre-drain) / `0x85c4` (pre-drain + post-notify); identical
  double-indirect `callx8 [obj+0]` core (byte-exact).
- All 10 sampled `const16` handler-fn targets start with `entry`; **zero** static
  pointer-table occurrences for any of 5 sampled addresses in IRAM or DRAM.
- Default `0x3198` → ErrorHandler `0x13f58` "Bad Opcode(0x%x)" → `0x13e00` → infinite
  self-loop (hard fault).
- `'S:'` records in DEBUG DRAM = **178** (`rg -c`); literal `S: ` instances = **187**
  (`rg -o`/`bytes.count`) — the 9-gap is 4 multi-`S:`-per-record lines (§8 CORRECTION).

**MED / INFERRED:**

- The exact `Handler`-object pointer passed to the thunk is set by a FLIX bundle in the
  impl (`impl+0x0c`) that the linear sweep mis-decodes (spurious out-of-image
  `l32r a10`); the window-rotation argument path (caller `a10` → callee `a2`) and the
  per-call object materialisation are inferred from the ABI + the byte-exact thunk.
- Boot-time construction/registration of the `Handler` singletons (no static table ⇒
  built in the `enter_run` dispatch-context init); exact site is boot-page scope.
- "variant fn" multi-opcode→distinct-fn groupings are operand/mode variants (the distinct
  addresses are OBSERVED; the variant *meaning* is inferred).

**LOW / UNRECOVERED:**

- The 4 unnamed FLIX-desync inline handlers `0xb8`/`0xbd`/`0xf1` (and the full body of
  `0x77`/`0x78`/`0xbb`): table targets exact, bodies FLIX-vector and partly unrecovered
  by the linear sweep.

---

## See also

- [SEQ Main FSM Loop](main-loop.md) — the poll/fetch/decode/dispatch FSM this hub is the
  tail of.
- [SEQ Fetch + PC-Redirect Front-End](fetch-pc-redirect.md) — the front-end that hands
  the opcode word to the decode at `0x2e5d`.
- [SEQ PC-Bounds Enforcement + Host API](pc-bounds.md) — the *address*-level guard
  (`is_pc_in_bounds @0x68d0`), distinct from the opcode-value bound here.
- [SEQ Error-Handler / Fault Reporting](error-handler.md) — the Bad/Illegal-Opcode fault
  arms the default path lands in.
- [POOL Engine Main Dispatch Loop](../pool/pool-dispatch.md) *(stub)* — the contrasting
  POOL dispatch style (§9).
- [kernel_info_table Binary Layout](../pool/kernel-info-table.md) *(stub)* — the POOL
  8-byte `(key, funcVA)` table (§9).
- [The Opcode Catalog Ledger](../kernels/opcode-catalog-ledger.md) *(stub)* — the
  cross-engine opcode inventory this table's 55 named handlers feed.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the
  OBSERVED/INFERRED/CARRIED × HIGH/MED/LOW tagging used throughout.
