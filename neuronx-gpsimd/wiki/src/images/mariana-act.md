# MARIANA × ACT image (cross-gen diff vs Cayman)

The **first cross-generation firmware-image diff** in this guide: the MARIANA
(NeuronCore v4 / NC-v4) Activation-engine image against the byte-true CAYMAN (NC-v3)
ACT baseline. Every size, sha256, opcode, reset byte, and string below is read
directly from `libnrtucode_internal.so` (sha256 `b7c67e89…632fc329b`) via its 14
`MARIANA_NX_ACT_*_get` accessors, carved out of identity-mapped `.rodata`, with the
shipped Cadence Vision-Q7 `ncore2gp` `xtensa-elf-objdump` decoding the flat blobs.
This page is the **template** the rest of Part 6 follows: it *diffs* against the
committed [CAYMAN × ACT baseline](./cayman-act.md) rather than re-deriving the engine.

> **The two headline facts up front.**
> 1. **MARIANA still ships a STANDALONE ACT image** — the "ACT folded into DVE?"
>    question is **NO** for MARIANA. The fold is a **MAVERICK-only** event. MARIANA is
>    the *last* generation with its own `NX_ACT` sequencer image, placed contiguously
>    *before* DVE in `.rodata`. [HIGH/OBSERVED]
> 2. **The reset vector shifted +0x1c** — CAYMAN `06 76 00 00` (`j 0x1dc`) becomes
>    MARIANA `06 7d 00 00` (`j 0x1f8`); the secondary vector moves `0x1e8`→`0x204`,
>    also **+0x1c**. Same boot *structure*, relocated layout: a full recompile, not a
>    binary patch. [HIGH/OBSERVED]
>
> Engine **model** is unchanged: MARIANA ACT is the *same* `cayman/seq/`-style
> NX-class SEQ ASCII-opcode dispatch engine, recompiled for v4 with **+3 handlers**, a
> **re-armed PROF_CAM**, and a **~14 % smaller IRAM**. The activation *function* is
> still a host-loaded LUT (no firmware relu/gelu/exp kernel).

Related pages: [CAYMAN × ACT (the baseline)](./cayman-act.md) ·
[MAVERICK × ACT — the DVE fold](./maverick-act.md) ·
[Firmware-Image Accessor Index](./image-catalog-index.md) ·
[Activate + PWL Application Mechanism](../firmware/kernels/activate-pwl.md) ·
[MARIANA+ delta](../generations/mariana-plus-delta.md) ·
[PROF_CAM / PROF_TABLE Blob Formats](./prof-cam-table-formats.md).

---

## 1. The delta table (CAYMAN baseline → MARIANA)

The whole page in one table. Unchanged properties are marked `(==)`; anchors for each
changed row are in the sections that follow. Carve geometry, packaging, dispatch
*model*, and the activation-LUT model are **invariant** — read the
[CAYMAN page](./cayman-act.md) for those; this table only documents the **delta**.

| PROPERTY | CAYMAN ACT (baseline) | MARIANA ACT (this page) | Δ |
|---|---|---|---|
| getters (`nm` / IDA sidecar) | 14 / 14 | 14 / 14 | `(==)` shape |
| real / empty images | 8 real + 6 zero-size cursors | 8 real + 6 zero-size cursors | `(==)` |
| packaging | flat IRAM/DRAM (not ELF) | flat IRAM/DRAM (not ELF) | `(==)` |
| **reset vector** | `06 76 00 00` → `j 0x1dc` | `06 7d 00 00` → `j 0x1f8` | **+0x1c** |
| **2nd vector** | `86 77 00 00` → `j 0x1e8` → `halt 0` | `86 7e 00 00` → `j 0x204` → `halt 0` | **+0x1c** |
| boot path | `const16 a0,0x90 ; jx a0` | `const16 a0,0x90 ; jx a0` | `(==)` form |
| shared boot stub `[12..15]` | `a0 71 69 80` | `a0 71 69 80` | `(==)` byte-identical |
| dispatch model | addx4 idx×4 + DRAM table | addx4 idx×4 + DRAM table | `(==)` SEQ |
| main DRAM table base | file `0x814` (178×4 B) | **moved off `0x814`** (now a string) | **reorg** |
| DEBUG compare-chain consts | `{145,167-171,176-179,181,184,189}` | same shared subset | `(==)` |
| **+ handlers** | — | **`Activate2`, `RandGetState`, `RandSetState`** | **+3** |
| − handlers | — | **none** (0 removed) | `(==)` retained |
| dtype string consts | `{UINT32,INT32,FP32}` | `{UINT32,INT32,FP32}` | `(==)` |
| FP4/CPTC as strings | absent | absent (grep-verified 0 hits) | `(==)` neg. |
| **PROF_CAM enabled** | 47 records | **25 records** (re-armed) | **47→25** |
| **PROF_TABLE header** | `0x0201`/`0x26000010`, 150 nz words | **zeroed**, 68 nz words | **differs** |
| engine self-name | `S: BEGIN on cayman` | `S: BEGIN on mariana` | rename |
| source tree | `…/cayman/seq/src/…` | `…/cayman/seq/src/…` | `(==)` dir name |
| IVP distinct (PERF/TEST) | 299 / 297 | 266 / 266 | smaller |
| **DEBUG/PERF/TEST IRAM** | `0x191e0 / 0x13dc0 / 0x13940` | `0x18ba0 / 0x11180 / 0x11240` | **−** (smaller) |
| DEBUG/PERF/TEST DRAM | `0x6260 / 0x2900 / 0x2c00` | `0x63c0 / 0x2ba0 / 0x2e60` | **+** (larger) |
| **DVE fold** | standalone | **standalone** (contiguous-before-DVE) | **NO fold** |

> **GOTCHA — read the getter STUBS via VA-aware `objdump`, not raw `dd`.** As on
> CAYMAN, `.rodata` is identity-mapped (`readelf -SW`: VA `0x46b0` == file `0x46b0`),
> so the *carve* `IMG-PTR`s below need no adjustment. But `.text` is **not** (VA
> `0x9b01a0` vs file `0x9af1a0`, a `0x2000` delta), so a naive `dd skip=<stub-VA>`
> reads the wrong stub bytes. The MARIANA PERF-IRAM getter at `.text` VA `0x9b3ca0`
> disassembles (VA-aware) to `lea …# 30b8a0 ; mov %rax,(%rdi) ; movq $0x11180,(%rsi)`
> — confirming ptr `0x30b8a0` / size `0x11180`. [HIGH/OBSERVED — CORRECTION carried
> from the CAYMAN page; re-verified this task]

---

## 2. The 14 MARIANA × ACT getters + carve provenance

`CLS=NX`, `ENG=ACT`. `nm | rg -c 'MARIANA_NX_ACT_[A-Z]+_[A-Z]+_get$'` → **14** (excluding
the `MARIANA_PLUS` set); the IDA `_names.json` sidecar independently lists **14**. 8
carry real bytes; 6 SRAM/EXTRAM getters emit `movq $0x0,(%rsi)` and alias the
contiguous-layout DVE cursor — ACT runs entirely from IRAM (code) + DRAM (data), as on
CAYMAN. [HIGH/OBSERVED]

| VARIANT | REGION | ACCESSOR (.text VA) | IMG-PTR (.rodata VA == file off) | SIZE | sha256 |
|---|---|---|---|---|---|
| DEBUG | IRAM | `0x9b41a0` | `0x3ea060` | `0x18ba0` | `f27afe6b…020fa282` |
| DEBUG | DRAM | `0x9b41c0` | `0x402c00` | `0x063c0` | `28b6bbe9…19055676` |
| DEBUG | SRAM/EXTRAM | `0x9b41e0`/`0x9b4200` | `0x408fc0` (DVE cursor) | `0x0` | EMPTY |
| PERF | IRAM | `0x9b3ca0` | `0x30b8a0` | `0x11180` | `78608c91…f371fd97` |
| PERF | DRAM | `0x9b3cc0` | `0x31ca20` | `0x02ba0` | `05a5b8d9…0a631d1d` |
| PERF | SRAM/EXTRAM | `0x9b3ce0`/`0x9b3d00` | `0x31f5c0` (DVE cursor) | `0x0` | EMPTY |
| TEST | IRAM | `0x9b3f20` | `0x37afe0` | `0x11240` | `c5c5adce…ecc4d8b4` |
| TEST | DRAM | `0x9b3f40` | `0x38c220` | `0x02e60` | `b3bcccc3…66e53252` |
| TEST | SRAM/EXTRAM | `0x9b3f60`/`0x9b3f80` | `0x38f080` (DVE cursor) | `0x0` | EMPTY |
| PROF | CAM | `0x9b4820` | `0x59c480` | `0x00400` | `326bc0dd…0d937e9a54` |
| PROF | TABLE | `0x9b4840` | `0x59c880` | `0x02000` | `8786dd11…1c7d233d` |

All 8 real carves (`dd bs=1 skip=<IMG-PTR> count=<SIZE>`) reproduce these sha256
exactly, and each is **byte-identical** to the matching `libnrtucode.a` member
`.rodata` (`ar x` → `objcopy --only-section=.rodata`): the static archive ships exactly
14 `MARIANA_NX_ACT` members (12 `img_*_contents.c.o` + 2
`hwdecode_*_PROF_{CAM,TABLE}_contents.c.o`), **8/8 IDENTICAL** to the in-`.so` getter
blobs. One firmware corpus, two packaging views. [HIGH/OBSERVED]

---

## 3. The +0x1c reset-vector shift (the cleanest relocation signal)

The reset region is **byte-identical across all 3 MARIANA IRAM variants**, and the
cross-gen delta is a clean **+0x1c on both boot targets** (`od -An -tx1 -N16`):

```
CAYMAN  IRAM:  06 76 00 00 | 00 00 | 86 77 00 00 | 00 00 | a0 71 69 80
MARIANA IRAM:  06 7d 00 00 | 00 00 | 86 7e 00 00 | 00 00 | a0 71 69 80
               └ j 0x1f8 ┘         └ j 0x204 ┘            └ shared stub ┘
```

Decoded with the shipped `ncore2gp` `xtensa-elf-objdump` (exit 0, empty stderr):

```text
0x000:  06 7d 00     j        0x1f8      ; primary reset vector → boot path  (CAYMAN: j 0x1dc)
0x006:  86 7e 00     j        0x204      ; secondary vector → halt trap      (CAYMAN: j 0x1e8)
0x1f8:  04 00 00     const16  a0, 0
0x1fb:  04 90 00     const16  a0, 144    ; a0 = 0x90  (C enter_run prologue)
0x1fe:  a0 00 00     jx       a0         ; jump into the C boot prologue
0x204:  00 52 00     halt     0          ; 2nd vector is a HALT trap
```

The Xtensa `J` opcode (`0x06`) carries an 18-bit signed displacement in the high bits;
the only changed byte is `byte[1]` (`0x76`→`0x7d`), and both decode to `target = 4 +
imm18` → CAYMAN `0x1dc`, MARIANA `0x1f8` — a **+0x1c (28-byte)** forward relocation of
the boot entry, the secondary vector tracking the same delta. The two IRAMs **diverge
at byte 2** (the `J` displacement) yet the boot-stub body at bytes `[12..15]` (`a0 71
69 80`) is byte-identical to CAYMAN — a recompile with a shifted IRAM layout, **not** a
binary patch. The boot *scheme* (`const16 a0,0x90 ; jx a0` → C `enter_run`; 2nd vector
`halt 0`) is unchanged. [HIGH/OBSERVED]

> **NOTE — what the +0x1c buys.** The shift is the downstream symptom of the +3
> handlers and the recompile, not a deliberate ABI move: ~0x1c more of boot/trampoline
> code is emitted ahead of the C prologue on v4, pushing `enter_run`'s landing pad
> forward. The runtime engine-identity string
> (`S: engine_base_addr=%llx tpb_base_addr=%llx -> … engine_idx=%u`) is present exactly
> as on CAYMAN, so the same flat image still self-locates its `engine_idx` at boot
> rather than baking it in. [string HIGH/OBSERVED; the relocation cause INFERRED-MED]

---

## 4. The handler delta (the cross-gen headline)

MARIANA ACT **retains every CAYMAN ACT handler and adds three**:

- **ADDED:** `Activate2` (a new activation-apply variant), `RandGetState`,
  `RandSetState` (RNG-state handlers that on CAYMAN were **POOL-only** — see the
  [CAYMAN ACT-vs-POOL roster](./cayman-act.md#5-the-activation-kernel-opcode-set)).
- **REMOVED:** none (0).

```c
// ---- the glue-stripped roster extractor (the ONLY correct diff) ----
// A naive `^S:` match FALSELY reports Cast/Copy/TensorScalar/ActivateQuantize/
// ActivationReadAccumulator as "removed": on MARIANA their log strings picked up a
// string-pool glued prefix with no intervening NUL ("VS: Cast", "TS: TensorScalar",
// "@S: ActivationReadAccumulator"). Strip the 0-3 glue chars before "S:" first.
//   strings -n3 DRAM
//     | rg -o '[A-Za-z@]{0,3}S: [A-Z][A-Za-z_0-9]+'
//     | sed -E 's/^[A-Za-z@]*S: //' | sort -u    // → bare OpName
```

> **GOTCHA — the string-pool prefix-glue diff-trap.** Running `^S:`-only on the two
> DEBUG DRAMs reports `ActivateQuantize, ActivationReadAccumulator, Cast, Copy,
> TensorScalar` as CAYMAN-only "removals" — **all false**. Each survives on MARIANA
> with a glued prefix: `VS: Cast`, `VS: Copy`, `TS: TensorScalar`, `TS:
> ActivateQuantize`, `@S: ActivationReadAccumulator`. The glue-stripped extractor
> yields the *true* delta: **+3, −0**. Any Part-6 cross-gen handler diff MUST
> glue-strip. [HIGH/OBSERVED]

Each new handler is a **real dispatch handler**, confirmed by its self-naming log-call
entry in DEBUG IRAM (the exact `const16 a10,8 ; const16 a10,<DRAM-off> ; call8
<log-helper>` pattern): [HIGH/OBSERVED]

| Handler | IRAM entry | log string (DRAM VA) | role |
|---|---|---|---|
| `Activate2` | `0xc0bd` (`const16 a10,0x24a0 ; call8 0x14eec`) | `S: Activate2` (`0x824a0`) | new activation-apply variant |
| `RandGetState` | `0x1f77` / `0x2d4c` / `0x69b3` / `0x6c2f` / `0xbeb8` | `S: RandGetState` (`0x82400`) | RNG-state read (POOL-only on CAYMAN) |
| `RandSetState` | `0xbe60` | `S: RandSetState` (`0x823d0`) | RNG-state write (POOL-only on CAYMAN) |

`Activate2`'s opcode is `0x25` (the ACT-native compare chain now tests `movi a3,33..37`
= `0x21`–`0x25`, vs CAYMAN's `0x21`–`0x24`; `0x25` is also newly armed in the PROF_CAM
— §6). The activation model is **unchanged**: no `relu`/`gelu`/`sigmoid`/`exp`/`tanh`
code strings exist in any MARIANA ACT image; the function is data loaded by
`ActivationTableLoad` and applied by `Activate`/`Activate2`. `Activate2` is a second
apply path (fused/alternate); its exact differentiation from `Activate` is not decoded.
[HIGH/OBSERVED absence; LUT model INFERRED-HIGH; `Activate2` semantics LOW/NOT CLAIMED]

Two new **non-handler** strings also appear — `transform_addr_for_pool_tpb_rerouting`
and the source header `addr_bits.hpp` (both absent on CAYMAN) — suggesting MARIANA
added POOL/TPB address-rerouting logic to the ACT engine, consistent with the RNG
handlers migrating in from POOL. [present HIGH/OBSERVED; "ACT gaining POOL capability"
INFERRED-MED]

---

## 5. Dispatch + dtype deltas

**Dispatch model: unchanged; the table moved.** MARIANA ACT uses the same SEQ
`addx4`-indexed jump-through-DRAM-table scheme (decoded `addx4 a4,a3,a4 ; l32i a4,a4,0
; jx a4` in PERF IRAM, plus a 16-entry `extui a3,a3,0,4` sub-table). The `S: Dispatch
opcode=0x%x` per-fetch log and the four `ErrorHandler` arms (`Bad Opcode` / `Illegal
Instruction` / `FP Error` / `Int Div Zero`) are present byte-for-byte, sourced from
`…/cayman/seq/src/handlers/exception_handler.hpp`. The DEBUG compare-chain uses the
**same shared dispatch-control constants** as CAYMAN (`movi a3,{145,167-171,176-179,
181,184,189}` decode-identically on both DEBUG IRAMs). [HIGH/OBSERVED]

> **CORRECTION — the main DRAM table is no longer at file `0x814`.** On CAYMAN, PERF
> DRAM file `0x814` holds the 178-entry 4-byte LE IRAM-target table (head `23 99 00 00`
> = `0x9923`, then the default trampoline `0x9be8`×3). On **MARIANA**, PERF DRAM file
> `0x814` holds an **assertion string** — `…2)) && "highest priority is full-register
> moves. TODO other dtypes"` — and the IRAM-pointer table runs have shifted (a
> word-scan finds big runs at MARIANA file `0x80`/`0x870` vs CAYMAN `0xc0`/`0x720`).
> The dispatch *model* is HIGH; the exact MARIANA main-table base + entry count is
> **MED** — the PERF IRAM dispatch desyncs under FLIX-VLIW bundling (the documented
> SX-FW-00 `{ const16 ; nop ; nop ; ivp_… }` bundle form). [HIGH model / MED base]

**Dtype delta: none visible at the firmware-string layer.** The only dtype constants in
any MARIANA ACT image are `NEURON_ISA_TPB_DTYPE_{UINT32,INT32,FP32}` — byte-for-byte
the same three CAYMAN carries, in the `move.cpp` assertion. MARIANA's new dtypes
(`FP4_EXP2`, `CPTC1..7`) appear **nowhere** as named strings in the ACT firmware
(`FP4|CPTC|MXTENSOR|SFP8|fp8_e` → **0 hits** across all 6 IRAM/DRAM carves). The new
dtype *codes* are numeric — handled by integer comparison in the `Cast`/activation-table
path, not by string-named asserts — so the dtype expansion is an ISA-header fact,
invisible as a new ACT-image artifact. [HIGH/OBSERVED-negative]

---

## 6. PROF_CAM + PROF_TABLE deltas (the HW-decode profiler was re-armed)

Both profiling blobs keep their **size** (CAM `0x400`, TABLE `0x2000`) but differ in
**content** (both `cmp`-differ at byte 1). The 16-byte `{opcode, mask, enable, rsvd}`
record format is unchanged — see the [CAYMAN page §6](./cayman-act.md#6-the-dram-image--prof-tables);
only the *armed opcode set* changed.

**PROF_CAM — 47 → 25 enabled records.** [HIGH/OBSERVED bytes; "arms these opcodes"
INFERRED-HIGH from the record shape]

```
CAYMAN (47):  0x00 01 02 03 06 07 21 22 23 24 41 42 43 44 45 46 47 48 49 4a 4b 4c
              4e 51 52 53 54 5e 67 68 a0 a1 a2 a3 a4 a5 a6 a7 a8 a9 aa ab b1 b2 b8 bb bd
MARIANA (25): 0x00 21 22 23 24 25 43 44 46 47 77 78 9f a0 a1 a2 a3 a5 a7 a8 a9 aa ab b1 b2
```

- **ADDED in MARIANA:** `0x25` (the new `Activate2` opcode), `0x77`, `0x78`, `0x9f`.
- **DROPPED:** most of the `0x41`–`0x5x` ASCII control block, the low `0x01/02/03/06/07`
  set, and `0xa4/0xa6/0xb8/0xbb/0xbd`.

The profiler is re-armed for the MARIANA opcode mix (fewer, different opcodes counted).

**PROF_TABLE — different preallocation.** CAYMAN ships header word `0x00000201` +
`0x26000010` + a descriptor blob (**150** nonzero words of 2048); MARIANA ships the
header **zeroed**, first nonzero at file `0x15`, **68** nonzero words. A
differently-preallocated profile-event/counter table the HW-decode profiler fills at
run time. Field schema not exhaustively decoded. [HIGH content-delta / MED schema]

---

## 7. Variant + cross-gen size table

| GEN | VAR | IRAM sz | DRAM sz | `S:` (uniq) | total strings | IVP-distinct |
|---|---|---|---|---|---|---|
| CAYMAN | DEBUG | `0x191e0` | `0x6260` | 150 | 249 | 178 (desync) |
| CAYMAN | PERF | `0x13dc0` | `0x2900` | 0 | 15 | 299 |
| CAYMAN | TEST | `0x13940` | `0x2c00` | 0 | 58 | 297 |
| MARIANA | DEBUG | `0x18ba0` | `0x63c0` | 152 | 253 | 157 (desync) |
| MARIANA | PERF | `0x11180` | `0x2ba0` | 0 | 15 | 266 |
| MARIANA | TEST | `0x11240` | `0x2e60` | 0 | 57 | 266 |

- **Same DEBUG-vs-RELEASE model as CAYMAN:** only DEBUG carries the `S:` runtime logs
  (MARIANA 152 vs CAYMAN 150; the +2 = the new `RandGet/SetState` logs, `Activate2`
  offset by glue-merges). PERF strips all `S:` (15 strings, all assertion source-paths);
  TEST keeps function/file symbols (57 strings). [HIGH/OBSERVED]
- **Cross-gen IRAM is SMALLER in every variant** (PERF `−0x2c40` ≈ 14 %, DEBUG `−0x640`,
  TEST `−0x2700`); **DRAM is slightly LARGER** (DEBUG `+0x160`, PERF `+0x2a0`, TEST
  `+0x260` — the +3 handlers' strings). A tighter/different compile, not a patch (the
  IRAMs diverge at byte 2). IVP distinct-mnemonic counts drop (266 vs 297-299); as on
  CAYMAN this is a **floor** — the larger DEBUG image desyncs more of the linear sweep
  across FLIX boundaries (157 vs PERF 266), not a real op-inventory loss. [HIGH/OBSERVED]
- The dispatch **mechanism is invariant** across all three MARIANA variants *and* vs
  CAYMAN (same reset-vector form, `S: Dispatch opcode` log, compare-chain constants,
  `ErrorHandler` arms, `cayman/seq/` codebase). A DEBUG↔RELEASE swap is pure
  observability; a CAYMAN↔MARIANA swap is recompile + 3 handlers + re-armed PROF +
  relocated layout — **not** a model change. [HIGH/OBSERVED]

> **NOTE — MARIANA ≠ MARIANA_PLUS at the ACT-image level.** A refinement worth
> carrying: MARIANA_PLUS ACT is **not** byte-identical to MARIANA ACT — DEBUG_DRAM
> differs (self-names `BEGIN on mariana_plus`, size `0x6560` vs `0x63c0`) and PERF_IRAM
> differs (size `0x14be0` vs `0x11180`). But the PROF_CAM **is** byte-identical
> (`326bc0dd`) and MARIANA_PLUS carries the same +3 handlers. So the v4/v4+ byte-identity
> holds for the EXTISA Q7 blobs and NCFW DRAM but **not** for the `NX_ACT` sequencer
> images — see [MARIANA+ delta](../generations/mariana-plus-delta.md). [HIGH/OBSERVED]

---

## 8. The DVE-fold question — resolved NO for MARIANA

The [MAVERICK SoC address map](../uarch/per-engine-depth.md) places ACT's control/bucket
tables (`ACT_CONTROL_TABLE`, `PWP_*`) *inside* the `TPB_DVE` node — "ACT folded into
DVE." This page tests that at the **MARIANA firmware-image layer**, and the answer is
**NO**:

- **MARIANA ships a separate, dedicated `NX_ACT` image** — the full 14-getter set
  (§2), identical in *shape* to CAYMAN's 14. `nm` lists exactly 14 `MARIANA_NX_ACT_*_get`
  + 14 `_get.data`. [HIGH/OBSERVED]
- **The layout adjacency is the fold-disproof:** the ACT PERF DRAM ends at file
  `0x31ca20 + 0x2ba0 = 0x31f5c0`, which is **exactly** where
  `MARIANA_NX_DVE_PERF_IRAM_get.data` begins. ACT is its own contiguous image block,
  placed immediately **before** DVE in `.rodata` — a separate engine image, not a
  DVE sub-table. [HIGH/OBSERVED]
- **Different SoC instances, different layers.** The MAVERICK finding is a *hardware
  address-map* phenomenon on the v5 SoC; this is the *firmware-image catalog* on the v4
  generation. It is **MAVERICK** — not MARIANA — that ships **no `NX_ACT` image at all**
  (its NX engines are DVE/PE/POOL/SP only). [HIGH/OBSERVED]

> **INTERPRETATION (INFERRED-MED).** The cross-gen trajectory is ACT-standalone on
> SUNDA/CAYMAN/MARIANA/MARIANA_PLUS (each with its own `NX_ACT` image), then **ACT
> folded into DVE on MAVERICK** (no `NX_ACT` image; ACT tables become DVE sub-regions).
> **MARIANA is the last generation with a standalone ACT engine image.** The
> +`RandGet/SetState` +`Activate2` +`pool_tpb_rerouting` additions here — ACT absorbing
> POOL-class capability — *may* be an early step toward that consolidation, but that
> causal reading is inferred, not proven. The fold itself is documented on
> [MAVERICK × ACT](./maverick-act.md).

---

## 9. Honesty ledger

**HIGH / OBSERVED (direct byte read or `ncore2gp` disassembly this task):**

- 14 `MARIANA_NX_ACT` getters (`nm` count 14, IDA `_names` sidecar 14); 8 real
  (ptr/size re-read via VA-aware `objdump`, the `0x2000` `.text` delta respected) + 6
  zero-size DVE-cursor aliases (all six `movq $0x0`).
- 8 carves reproduce their sha256 and are byte-identical (8/8) to the `libnrtucode.a`
  member `.rodata`. CAYMAN baseline re-carved + re-hashed (DEBUG_DRAM `f6c5136e`,
  PROF_CAM `8fd7e422`, PROF_TABLE `ce761f81`, DEBUG_IRAM `ffbb78fe` — all MATCH the
  committed CAYMAN page, so the diff is against the authentic baseline).
- Reset vector `06 7d 00 00` (`j 0x1f8`) byte-identical across DEBUG/PERF/TEST,
  **+0x1c** from CAYMAN's `06 76 00 00` (`j 0x1dc`); secondary `86 7e`→`j 0x204`
  (`halt 0`); boot path `const16 a0,0x90 ; jx a0`; shared stub `a0 71 69 80`; IRAMs
  diverge at byte 2.
- Handler diff (glue-stripped): +`Activate2`/`RandGetState`/`RandSetState`, −0; each
  confirmed a real handler by its IRAM log-call entry (`Activate2` @`0xc0bd` →
  `S: Activate2` @DRAM `0x824a0`). Glue-prefix artifact (`VS:`/`TS:`/`@S:`) documented.
- Dispatch model (addx4-indexed + DEBUG compare-chain), `S: Dispatch opcode` log,
  `ErrorHandler` arms, `cayman/seq/src/…` source tree, `BEGIN on mariana`,
  `engine_base_addr` identity string — all present. Main DRAM table moved off file
  `0x814` (now the `full-register moves` assertion string).
- DTYPE: only `{UINT32,INT32,FP32}` (== CAYMAN); FP4/CPTC absent (0 grep hits ×6 carves).
- PROF_CAM 47→25 enabled, armed-opcode set changed (+`0x25/0x77/0x78/0x9f`);
  PROF_TABLE header zeroed, 68 vs 150 nonzero words — both NOT byte-identical to CAYMAN.
- Cross-gen size/string/IVP table; IRAM smaller / DRAM +2-strings; 323 windowed-ABI
  `entry|retw` markers in PERF IRAM. Fold-disproof: ACT PERF DRAM end `0x31f5c0` ==
  DVE PERF IRAM start.

**MED / INFERRED:** exact MARIANA main DRAM dispatch-table base + entry count (table
moved off `0x814`, PERF IRAM FLIX-desync — model/log/compare-constants/arms are HIGH);
PROF_TABLE field schema (structure-level only); "+RNG/+rerouting is an early step toward
the MAVERICK fold"; "engine_idx computed at boot" (INFERRED from the OBSERVED string +
boot path).

**LOW / NOT CLAIMED:** the exact semantics of `Activate2` vs `Activate` (a second
activation-apply variant, not decoded); which silicon/runtime selects DEBUG/PERF/TEST
or MARIANA vs MARIANA_PLUS; activation FUNCTION coefficients (host-supplied LUT,
grep-confirmed absent from every image).
