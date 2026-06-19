# MARIANA_PLUS × PE image (cross-gen diff vs MARIANA)

This page closes the **MARIANA_PLUS (v4+) NX engine matrix** by diffing the
MARIANA_PLUS Processing-Element firmware image — the systolic-matmul / TensorE
sequencer (`engine_idx = 0`, the same `cayman/seq/` SEQ-dispatch chassis as
[CAYMAN × PE](./cayman-pe.md)) — against the committed, byte-true
[MARIANA × PE baseline](./mariana-pe.md). Every size, sha256, opcode, reset byte,
and string below is read directly from `libnrtucode_internal.so` (sha256
`b7c67e89…632fc329b`) via its 14 `MARIANA_PLUS_NX_PE_*_get` accessors, carved from
identity-mapped `.rodata`, with the shipped Cadence Vision-Q7 `ncore2gp`
`xtensa-elf-objdump` decoding the flat blobs. This is a **DIFF page**: the
unchanged matmul core (the SBUF→array→PSUM dataflow, the IVP widening-MAC datapath,
the 9-handler roster's per-op operand semantics) is **not re-derived** — the
[MARIANA × PE](./mariana-pe.md) / [CAYMAN × PE](./cayman-pe.md) baselines are its
byte-pinned ground truth.

> **Three headlines, up front — this page proves all three at the byte level.**
>
> 1. **MARIANA_PLUS PE is BYTE-FOR-NAME IDENTICAL to MARIANA PE at the
>    handler/opcode/dtype/PROF layer.** The handler roster is `69 == 69` glue-stripped
>    (`+0/−0`); the opcode-space compare-chain is **byte-identical at byte-identical
>    addresses** (`0x2934..0x2a78` `cmp`-clean); the PROF tables are byte-for-byte
>    identical; the dtype surface is unchanged. The v4+-vs-v4 delta is a **RECOMPILE
>    + ONE functional addition — a DGE reshape fast-path** — with **no new dispatch
>    handler, no new opcode, no new dtype, no new silicon**.
>    `MARIANA_PLUS PE = MARIANA PE recompiled + the DGE fast-path`. [HIGH/OBSERVED]
>
> 2. **`PeManageSeed` (`0x08`) + the MX matmul pair (`LdweightsMX 0x09` /
>    `MatmulMX 0x0A`) + `ConvLutLoad 0xe4` are RETAINED on MARIANA_PLUS PE,
>    byte-identical to MARIANA.** These four FIRST SHIPPED in MARIANA (v4) — CAYMAN PE
>    has **0** hits ([CAYMAN × PE §1.5](./cayman-pe.md)). They persist verbatim into
>    v4+: same strings, same DRAM offsets, same opcode bindings. [HIGH/OBSERVED]
>
> 3. **The IMG-10-vs-FW-66 nuance is resolved definitively: `PeManageSeed` first
>    ships at v4 (MARIANA), NOT v4+.** The [MARIANA × PE](./mariana-pe.md)
>    image-boundary attribution is **correct**. Any read that places `PeManageSeed`
>    first at MARIANA_PLUS is a **carve-coverage artifact** — see the
>    [§6 CORRECTION](#6-the-definitive-pemanageseed-v4-vs-v4-resolution). [HIGH/OBSERVED]

This is the *expected* result. MARIANA_PLUS shares the MARIANA ISA — there is no
`neuron_mariana_plus_arch_isa` dir; the four ISA dirs are
`cayman`/`mariana`/`maverick`/`sunda`, and MARIANA_PLUS carries only its own
`arch-headers/mariana_plus/` **register-map** dir. So the ISA / struct / dtype /
handler surface **is** MARIANA's. "v4+" is a register-map refresh + a recompile + a
DGE optimization, not a model or ISA change — the same null functional delta the
sibling [MARIANA_PLUS × ACT](./mariana-plus-act.md) and
[MARIANA_PLUS × DVE](./mariana-plus-dve.md) pages established. [HIGH/OBSERVED — the
ISA-dir listing is the shipped artifact]

Confidence/evidence tags follow the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. Every device fact is byte-pinned to a carve from
`libnrtucode_internal.so` and decoded with the shipped `ncore2gp`
`xtensa-elf-objdump`; both the MARIANA PE baseline and the MARIANA_PLUS images were
**re-carved and re-hashed this session** for an apples-to-apples diff (all 8 MARIANA
anchors and all 8 MARIANA_PLUS carves reproduced exact; the dispatch chain, PROF
identity, reset vector, and handler/seed strings re-verified with stock binutils +
`ncore2gp`).

Related pages: [MARIANA × PE (the diff base)](./mariana-pe.md) ·
[MARIANA_PLUS × ACT](./mariana-plus-act.md) ·
[MARIANA_PLUS × DVE](./mariana-plus-dve.md) ·
[MARIANA+ generation delta](../generations/mariana-plus-delta.md) ·
[PE Matrix-Multiply Path](../firmware/kernels/pe-matmul.md) ·
[MX (Microscaling) Dequant](../firmware/kernels/mx-dequant.md) ·
[DGE Reshape Engine](../firmware/dge/dge-reshape.md) ·
[Firmware-Image Accessor Index](./image-catalog-index.md) ·
[Per-Engine Depth](../uarch/per-engine-depth.md).

> **NOTE — the objects used.** Container:
> `…/custom_op/c10/lib/libnrtucode_internal.so` (sha256
> `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b`, ELF64 x86-64
> DYN, **not stripped**). First R `LOAD` is the identity map (`off 0x0 == vaddr 0x0`),
> so each `<NAME>.data` accessor address is simultaneously the `.rodata` VA and the
> file offset of its blob — carve = `so[ptr : ptr+size]`. **IRAM file-offset ==
> device IRAM VA** (reset vector at byte 0); **DRAM string-file-offset == device DRAM
> VA − `0x80000`** (the DRAM image loads at device VA `0x80000`). Disassembler:
> `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump`
> (GNU Binutils 2.34.20200201 Xtensa Tools 14.09, `XTENSA_CORE=ncore2gp`, ConfigName
> `Xm_ncore2gp`, uarch Cairo, Xtensa24, RI-2022.9, `TargetHWVersion=NX1.1.4`,
> `IsaMaxInstructionSize=32` FLIX/VLIW). All carve sha256, the reset vector, the
> dispatch chain, the PROF tables, and the seed/handler strings were reproduced this
> session (exit 0, empty stderr). `[HIGH/OBSERVED]`

---

## 1. The delta table (MARIANA PE baseline → MARIANA_PLUS PE)

The whole page in one table. `(==)` marks invariant rows; the **bold** rows are the
*only* real changes. Read the [MARIANA × PE page](./mariana-pe.md) for the engine
*model* — this table documents the cross-gen delta, leading with the null functional
delta + the PeManageSeed-retained finding + the IMG-10/FW-66 resolution.

| PROPERTY | MARIANA PE (baseline) | MARIANA_PLUS PE (this page) | Δ |
|---|---|---|---|
| getters (`nm` / IDA sidecar) | 14 / 14 | 14 / 14 | `(==)` shape |
| real / empty images | 8 real + 6 zero-size cursors | 8 real + 6 zero-size cursors | `(==)` |
| packaging | flat IRAM/DRAM (not ELF) | flat IRAM/DRAM (not ELF) | `(==)` |
| reset vector | `06 7d 00 00` → `j 0x1f8` | `06 7d 00 00` → `j 0x1f8` | **`(==)` SAME +0x1c, no further shift** |
| 2nd vector | `86 7e 00 00` → `j 0x204` → `halt 0` | `86 7e 00 00` → `j 0x204` → `halt 0` | `(==)` byte-identical |
| boot stub @`0xc` / path | `a0 71 69 80` → `const16 a0,0x90 ; jx a0` → `enter_run@0x90` | same | `(==)` byte-identical |
| DRAM `.globstruct` magic | `0x6099cb34` + init block | `0x6099cb34` + init block | `(==)` byte-identical |
| dispatch model | RAW-opcode compare-chain (no `addi` norm) + 17-tbl @`0x814` | RAW-opcode compare-chain + 17-tbl @`0x814` | `(==)` SEQ |
| compare-chain `0x2934..0x2a78` | `l32i.n a2,[a1,16]; bnei/movi a3,N;bne` | **byte-identical AT identical addrs** | **`(==)` `cmp`-clean** |
| real opcode count | **29** | **29** | **`+0/−0`** |
| opcodes | CAYMAN's 25 + `{0x8,0x9,0xa,0xe4}` | **identical 29** | **`(==)` retained** |
| **HANDLER ROSTER** | 9 PE (5 CAYMAN matmul + 4 MARIANA) | **byte-for-name IDENTICAL** | **`+0/−0`** |
| **`PeManageSeed` (`0x08`)** | PRESENT (4 strings @`0x871/888/23e0/2407`) | **PRESENT (SAME 4, SAME offsets)** | **`(==)` retained** |
| `LdweightsMX 0x09` / `MatmulMX 0x0A` | PRESENT (`SMX1_LW`/`SMX1D3_MM`) | **PRESENT (`(==)`)** | `(==)` retained |
| `ConvLutLoad 0xe4` | PRESENT (migrated onto PE at v4) | **PRESENT (`(==)`)** | `(==)` retained |
| handler-name pool `0x23c0..0x26c0` | (the 9-handler cluster) | **byte-identical** | `(==)` |
| dtype string consts | `{UINT32,INT32,FP32}` | `{UINT32,INT32,FP32}` | `(==)` |
| FP4/CPTC/MX/SFP8 as strings | absent | absent (0 hits ×8 carves) | `(==)` neg. |
| **PROF_CAM** | `43475cec` (22 armed records) | **`43475cec`** | **byte-IDENTICAL** |
| **PROF_TABLE** | `d93b723e` (header zeroed) | **`d93b723e`** | **byte-IDENTICAL** |
| source tree | `…/cayman/seq/src/…` + `addr_bits.hpp` | same | `(==)` dir name |
| errata | no `mariana-4062` (DVE-only) | no errata, no `mariana_plus-NNNN` | `(==)` |
| engine self-name | `S: BEGIN on mariana` | `S: BEGIN on mariana_plus` | **rename** |
| **NEW vs prior gen** | `+4 handlers/+4 opcodes` (vs CAYMAN) | **`+DGE fast-path code`** (vs MARIANA) | **new code** |
| **IRAM size (PERF)** | `0x12ce0` | `0x172c0` | **`+0x45e0` GREW** |
| DRAM size (PERF) | `0x2da0` | `0x2ec0` | `+0x120` larger |
| POOL fold | standalone (contiguous-before-POOL) | standalone (contiguous-before-POOL) | `(==)` NO fold |
| engine model | SEQ ASCII-dispatch, `engine_idx=0` | SEQ ASCII-dispatch, `engine_idx=0` | `(==)` |

**Verdict.** A MARIANA↔MARIANA_PLUS PE swap is a **full recompile + a DGE reshape
fast-path + a register-map refresh** — with **NOTHING added or removed** at the
dispatch / handler / opcode / dtype / PROF surface (PROF byte-identical, the dispatch
compare-chain even byte-identical, the handler-name pool byte-identical). 6/8 images
differ (IRAM/DRAM recompiled); 2/8 (PROF) byte-identical. It is **not** a model
change, **not** an ISA change. The IMG-13/14 v4+ model holds for the matmul engine
exactly as it did for ACT and DVE. `[HIGH/OBSERVED]`

> **CORRECTION carried from [MARIANA × PE §10](./mariana-pe.md).** The MARIANA page's
> LOW-ledger note ("the MARIANA_PLUS NX_PE byte-comparison… not carved here") is
> discharged by this page: the MARIANA_PLUS PE images **are** carved (8/8), and the
> result is the null functional delta above. The blobs differ from MARIANA's because
> they were recompiled and a DGE fast-path was inserted; the *engine* did not change.
> `[HIGH/OBSERVED]`

---

## 2. The 14 MARIANA_PLUS × PE getters + carve provenance

`CLS=NX`, `ENG=PE`, `engine_idx=0`. `nm libnrtucode_internal.so | rg -c
'MARIANA_PLUS_NX_PE_.*_get$'` → **14**; the IDA `_names.json` sidecar independently
lists 14, matching [image-catalog-index.md](./image-catalog-index.md) MARIANA_PLUS
NX_PE rows 413–426 exactly. 8 carry real bytes; 6 SRAM/EXTRAM getters emit
`movq $0x0,(%rsi)` and alias the contiguous-layout POOL cursor — PE runs entirely
from IRAM (code) + DRAM (data), exactly as MARIANA / CAYMAN PE. Each real getter
disassembles to `lea <blob>(%rip),%rax ; movq $<size>,(%rsi) ; ret` (e.g.
`MARIANA_PLUS_NX_PE_PERF_IRAM_get @0x9b4a20: lea -0x3ddbc7(%rip),%rax  # 5d6e60 ;
movq $0x172c0,(%rsi) ; ret`). `[HIGH/OBSERVED]`

| VARIANT | REGION | ACCESSOR (.text VA) | IMG-PTR (.rodata VA == file off) | SIZE | sha256 | identical to MARIANA? |
|---|---|---|---|---|---|---|
| PERF | IRAM | `0x9b4a20` | `0x5d6e60` | `0x172c0` | `c4e2115d…` | NO (recompile+DGE) |
| PERF | DRAM | `0x9b4a40` | `0x5ee120` | `0x2ec0` | `9a4abf20…` | NO |
| TEST | IRAM | `0x9b4ca0` | `0x65ce60` | `0x16de0` | `e08c8d41…` | NO |
| TEST | DRAM | `0x9b4cc0` | `0x673c40` | `0x32a0` | `3b48f7e2…` | NO |
| DEBUG | IRAM | `0x9b4f20` | `0x6f43a0` | `0x19e00` | `6f02f447…` | NO |
| DEBUG | DRAM | `0x9b4f40` | `0x70e1a0` | `0x6560` | `45ee6ef5…` | NO |
| **PROF** | **CAM** | `0x9b5520` | `0x86cb00` | `0x400` | **`43475cec…`** | **YES (byte-identical)** |
| **PROF** | **TABLE** | `0x9b5540` | `0x86cf00` | `0x2000` | **`d93b723e…`** | **YES (byte-identical)** |

All 8 real carves (`internal.so[IMG-PTR : IMG-PTR+SIZE]`) reproduce these sha256
exactly this session, and each is **byte-identical** (sha256 + `cmp -s` clean) to the
matching `libnrtucode.a` member `.rodata` (`ar x` → `objcopy --only-section=.rodata`):
the archive ships exactly 14 `MARIANA_PLUS_NX_PE` members (12 `img_*` + 2
`hwdecode_*_PROF_{CAM,TABLE}`), **8/8 reconciled** — not a spot-check (DEBUG_DRAM,
PROF_CAM, PERF_IRAM re-checked `cmp`-clean this session). One firmware corpus, two
packaging views. `[HIGH/OBSERVED]`

> **NOTE — PE is THIRD of the five NX engines, contiguous-before-POOL.** The layout
> is variant-major, engine-minor (`ACT → DVE → PE → POOL → SP`), the same order as
> MARIANA. Contiguity arithmetic is 3/3 MATCH (`PERF` slot, this session): DVE
> `PERF_DRAM` end `0x5d3ba0 + 0x32c0 = 0x5d6e60` == PE `PERF_IRAM` start; PE
> `PERF_IRAM` end `0x5d6e60 + 0x172c0 = 0x5ee120` == PE `PERF_DRAM` start; PE
> `PERF_DRAM` end `0x5ee120 + 0x2ec0 = 0x5f0fe0` == POOL `PERF_IRAM` start. PE's 6
> zero-size SRAM/EXTRAM cursors all resolve (`objdump` `lea`) to
> `MARIANA_PLUS_NX_POOL_*_IRAM.data` (PERF `0x5f0fe0`, TEST `0x676ee0`, DEBUG
> `0x714700`). PE is a **standalone** image, **not folded** — the ACT→DVE fold is
> MAVERICK-only and irrelevant here. `[HIGH/OBSERVED]`

---

## 3. The reset/boot vector — SAME +0x1c MARIANA shift, NO further shift

The reset region is **byte-identical across all 3 MARIANA_PLUS IRAM variants AND
byte-identical to MARIANA PE** (`xxd -l16`, this session — PERF/DEBUG/TEST all head
the same):

```
MARIANA      PE IRAM:  06 7d 00 00 | 00 00 | 86 7e 00 00 | 00 00 | a0 71 69 80
MARIANA_PLUS PE IRAM:  06 7d 00 00 | 00 00 | 86 7e 00 00 | 00 00 | a0 71 69 80
                       └ j 0x1f8 ┘         └ j 0x204 ┘            └ shared stub ┘
```

The `j` opcode byte-1 is `0x7d` on **both** generations (the MARIANA `+0x1c` forward
shift from CAYMAN's `0x76`), so MARIANA_PLUS inherits MARIANA's relocated boot entry
with **no further shift**. Decoded instruction-exact with the shipped `ncore2gp`
`xtensa-elf-objdump` (exit 0) off the MARIANA_PLUS PERF IRAM carve this session:

```text
0x000:  06 7d 00     j        0x1f8      ; primary reset vector → boot path
0x006:  86 7e 00     j        0x204      ; secondary vector → halt trap
0x1f8:  04 00 00     const16  a0, 0
0x1fb:  04 90 00     const16  a0, 144    ; a0 = 0x90  (C enter_run prologue)
0x1fe:  a0 00 00     jx       a0         ; jump into the C boot prologue
0x204:  00 52 00     halt     0          ; 2nd vector is a HALT trap
```

The DRAM head is `34 cb 99 60` = header word `0x6099cb34`, the `.globstruct`
dispatcher-state magic shared with every flat NX DRAM, **byte-identical** to MARIANA
across all 3 variants, with the same `@0x18:0x38` init block (`4× 0x00001000 + 4×
0x00ffffff`). The boot-stub body at `0xc` (`a0 71 69 80 …`) is byte-identical to
MARIANA PE. The reset + boot stub + DRAM init are a recompile of the *same* boot
scheme, not a re-relocation. `[HIGH/OBSERVED]`

> **GOTCHA — the recompile signature is a literal constant, not a vector move.** The
> MARIANA_PLUS-vs-MARIANA PERF IRAM **shared prefix is `0x212` bytes** (this session:
> `carve[:0x212]` `cmp`-clean, first divergence at `0x212`) — the reset, boot
> trampoline, *and the entire opcode compare-chain* are byte-identical, and the first
> divergence is a relocated literal-address constant. DEBUG/TEST IRAM diverge later
> (the report's `@0xa2` is the first byte of a relocated literal in those builds).
> That is the recompile-relocation point: the boot/dispatch *scheme* is shared
> verbatim and the code only diverges where a relocated literal appears. A binary
> patch would have left the literals alone; a recompile shifts them. `[HIGH/OBSERVED]`

DISASSEMBLY PROOF (shipped `ncore2gp`, exit 0): the MARIANA_PLUS PE PERF IRAM decodes
a full Q7/NX windowed-ABI code body (98 `entry` / 191 `retw.n` / 555 `call8`; DEBUG
525/756/1617) — a genuine separately-compiled `cayman/seq` sequencer, **larger** than
MARIANA's PE (PERF 131/187), consistent with the bigger `PERF_IRAM` (`0x172c0` vs
`0x12ce0`). Not a stub. The vector datapath is partly bundle-interleaved by the
linear sweep (the documented FLIX-desync); the windowed-ABI control spine + the
dispatch chain decode cleanly. `[HIGH/OBSERVED]`

---

## 4. The dispatch — opcode space STABLE at 29 (byte-identical chain, NO growth)

MARIANA_PLUS PE uses the **same RAW-opcode segmented compare-chain** as MARIANA PE
([MARIANA × PE §3](./mariana-pe.md)) — NOT the normalized `addx4`/`addi a2,a2,-65`
ASCII style of DVE/ACT. The decision code did not even move: the entire chain at
DEBUG IRAM `0x2934..0x2a78` is **byte-identical** between the two generations (`cmp`
clean this session — stronger than the ACT instance, where the DRAM table was `+0x20`
relocated). Decoded instruction-exact off the MARIANA_PLUS DEBUG IRAM carve with the
native `ncore2gp` `xtensa-elf-objdump`: `[HIGH/OBSERVED]`

```text
; MARIANA_PLUS PE DEBUG IRAM, HW-Decode dispatch site 0x2934 (ncore2gp, this session):
  2934:  2841     l32i.n  a2, a1, 16        ; read RAW opcode word (no normalisation)
  2936:  661202   bnei    a2, 1,  0x293c    ; 0x01 Ldweights
  293f:  662202   bnei    a2, 2,  0x2945    ; 0x02 Matmul        (TS: glue)
  2948:  663202   bnei    a2, 3,  0x294e    ; 0x03 PeRegWrite
  2951:  666202   bnei    a2, 6,  0x2957    ; 0x06 LdTags
  295a:  667202   bnei    a2, 7,  0x2960    ; 0x07 MatmulSparse   (VS: glue)
  2963:  668202   bnei    a2, 8,  0x2969    ; 0x08 PeManageSeed   <-- v4 op, RETAINED
  296c:  0c93     movi.n  a3, 9             ; 0x09 LdweightsMX    <-- v4 op, RETAINED
  296e:  379202   bne     a2, a3, 0x2974
  2977:  669202   bnei    a2, 10, 0x297d    ; 0x0a MatmulMX       <-- v4 op, RETAINED
  2980:  32a09f   movi    a3, 159           ; 0x9f … (control/move core, 0xa0..ab, b0..bd)
  …
  2a70:  32a0e4   movi    a3, 228           ; 0xe4 ConvLutLoad    <-- v4 op, RETAINED
  2a73:  379202   bne     a2, a3, 0x2a79
```

**Opcode roster (instruction-exact, MARIANA_PLUS PE):** `[HIGH/OBSERVED]`

```text
{0x1,2,3,6,7,8,9,0xa} | {0x9f,0xa0..0xab} | {0xb0,b1,b2,b3,b5,b8,bd} | {0xe4}  = 29 ops
```

This is **MARIANA PE's exact 29** (CAYMAN's 25 + `{0x8,0x9,0xa,0xe4}`). `ADDED=0,
REMOVED=0`. The four v4 opcodes are RETAINED on v4+; the opcode space is byte-stable
across the v4→v4+ gap. The `S: Dispatch opcode=0x%x` log is at DRAM file `0x858` on
**both** gens; the dual `S: NX in HW Decode mode` / `S: NX in Sunda mode` strings and
the `ErrorHandler` arms (`Bad Opcode(0x%x)` / `Illegal Instruction` / `FP Error` /
`Int Div Zero`; source `…/cayman/seq/src/handlers/exception_handler.hpp`) are
byte-for-name present — identical fault classes and fetch machinery. `[HIGH/OBSERVED]`

> **GOTCHA — the 17-entry sub-table `@DRAM 0x814` relocates `+0x1c`, not `+0x20`.**
> The DEBUG sub-table has **17** in-range IRAM trampolines on both gens (gen-stable
> count, NO opcode-space growth); the targets are uniformly relocated `MARIANA =
> MARIANA_PLUS + 0x1c` (idx0 `0x3d5c → 0x3d78`, idx16 `0x8ba8 → 0x8bc4`). The PE
> recompile-shift is `+0x1c` — a per-engine layout detail (ACT's DRAM table shifted
> `+0x20`), **not** a functional difference; the opcode chain itself did not move at
> all (§3). The PERF clean dispatch-table base `@DRAM 0x218` is FLIX-bundled in the
> linear sweep, so the byte-exact PERF table base is MED; the `addx4/jx` form is the
> gen-stable model. `[HIGH chain/table count; MED PERF row binding]`

---

## 5. The handler roster — byte-for-name IDENTICAL (`+0/−0`), PeManageSeed/MX RETAINED

The decisive section. Proven two independent ways against both DEBUG DRAMs this
session; both methods agree: **nothing added, nothing removed.** `[HIGH/OBSERVED]`

```c
// METHOD 1 (glue-stripped normalized): extract every "<glue>S: <OpName>", strip the
//   0-3 string-pool glue chars (TS:/XS:/VS:/@S:), bare OpName, sort -u.
//   →  MARIANA_PLUS 69 tokens  ==  MARIANA 69 tokens   (ADDED=0, REMOVED=0)
//
// METHOD 2 (strict end-anchored single-token): the clean dispatch roster.
//   →  MARIANA_PLUS 27  ==  MARIANA 27                 (diff = EMPTY)
```

The **9 PE-specific handlers** — the 5 CAYMAN matmul core + the 4 MARIANA additions —
are ALL RETAINED on MARIANA_PLUS, with **identical counts** in both gens (byte-verified
this session via `strings | rg -c`):

| handler | opcode | MARIANA_PLUS / MARIANA count | DRAM str (file off) | origin |
|---|---|---|---|---|
| `Ldweights` | `0x01` | 2 / 2 | `0x1ad0` | CAYMAN core |
| `Matmul` | `0x02` | (`TS:` glued) | `0x1af0` | CAYMAN core |
| `PeRegWrite` | `0x03` | 1 / 1 | `0x1b10` | CAYMAN core |
| `LdTags` | `0x06` | 1 / 1 | `0x24b0` | CAYMAN core |
| `MatmulSparse` | `0x07` | 1 / 1 | `0x24d0` (`VS:` glued) | CAYMAN core |
| **`PeManageSeed`** | **`0x08`** | **4 / 4** | `0x871/888` + `0x23e0/2407` | **MARIANA, retained** |
| **`LdweightsMX`** | **`0x09`** | **1 / 1** | `0x2470` | **MARIANA, retained** |
| **`MatmulMX`** | **`0x0A`** | **1 / 1** | `0x2490` (`XS:` glued) | **MARIANA, retained** |
| **`ConvLutLoad`** | **`0xe4`** | **1 / 1** | `0x23c0` | **MARIANA, retained** |

The handler-name string pool (DRAM `0x23c0..0x26c0`) is **byte-identical** between the
two generations. `[HIGH/OBSERVED]`

> **GOTCHA — the string-pool prefix-glue diff-trap recurs identically.** A naive `^S:`
> match FALSELY reports `Matmul`/`MatmulMX`/`MatmulSparse` as "removed": their DRAM log
> strings picked up a string-pool glued prefix with no intervening NUL — `TS: Matmul`
> @`0x1aef`, `XS: MatmulMX` @`0x248f`, `VS: MatmulSparse` @`0x24cf` (all three present
> on MARIANA_PLUS, confirmed this session). **Both** generations exhibit the *same*
> glue, so the glue-stripped diff is robustly clean. Any Part-6 cross-gen handler diff
> MUST glue-strip. `[HIGH/OBSERVED]`

**The four PeManageSeed strings, read out of the MARIANA_PLUS PE DEBUG DRAM
(`strings -t x`, this session) — at offsets byte-identical to MARIANA:**

```text
0x871   S: PeManageSeed(SAVE)                          ; PE_SEED_MODE SAVE_SEED=2
0x888   S: PeManageSeed(LOAD)                          ; PE_SEED_MODE LOAD_SEED=1
0x23e0  S: PeManageSeed : micro-op : LdWeight          ; seed-mgmt orchestrates a LdWeight pass
0x2407  S: PeManageSeed : micro-op : Matmul : is_load=%d
```

The *only* difference between the MARIANA_PLUS and MARIANA PE DEBUG DRAMs in this
cluster is the self-name immediately after the SAVE/LOAD pair: `0x89f
S: BEGIN on mariana_plus` vs MARIANA's `S: BEGIN on mariana`. The four seed strings,
their offsets, and the surrounding handler pool are byte-for-byte the same. The matmul
MAC datapath (the IVP widening MAC: `ivp_mul4t2n8xr8`, `ivp_mul4ta2n8xr8`,
`ivp_mulpan16xr16`, the signed/unsigned-mixed `ivp_mulus*/mulsu*pan16xr16`,
`ivp_packvr*`) is present in both gens' PERF IRAM (higher MPLUS counts —
`mul4t2n8xr8` 98 vs 64 — from the recompile + DGE growth). `[HIGH/OBSERVED]`

> **NOTE — what `PeManageSeed`/MX do (CARRIED, not re-derived here).** `PeManageSeed`
> (`0x08`, struct `S2S1D2_PE_SEED_STRUCT`) is the **PSUM fp32→bf16 stochastic-rounding
> RNG seed save/restore** micro-op — *not* a per-cell PE-array PRNG. `PE_SEED_MODE` =
> `NONE=0` / `LOAD_SEED=1` / `SAVE_SEED=2`, matching the `(LOAD)`/`(SAVE)` mode logs;
> the seed manager issues a *wrapped* `LdWeight`/`Matmul` pass (`is_load=%d`) to plumb
> seed state through the array. The MX path (`LdweightsMX 0x09` `SMX1_LW_STRUCT` /
> `MatmulMX 0x0A` `SMX1D3_MM_STRUCT`) is the **out-of-band** `scale_addr` + E8M0
> (`SFP8_E8`) shared-exponent matmul plumbing — distinct from POOL's **in-band**
> `proc_4bit_mx_8` block-of-8 dequant ([mx-dequant.md](../firmware/kernels/mx-dequant.md)).
> This page establishes only the image-level **presence (retained at v4+)**; the
> operand structs + SR datapath are documented in
> [pe-matmul.md](../firmware/kernels/pe-matmul.md). `[presence HIGH/OBSERVED; semantics CARRIED]`

---

## 6. The definitive PeManageSeed v4-vs-v4+ resolution

**THE QUESTION.** [MARIANA × PE](./mariana-pe.md) attributed `PeManageSeed`
first-appearance to MARIANA PE (v4) — finding it **absent** on CAYMAN (0 grep hits)
and **present** on MARIANA. A separate firmware decode (FW-66) attributed
`PeManageSeed` to "MARIANA_PLUS / NeuronCore-v4 **only**", carving the v4-class image
that self-names `BEGIN on mariana_plus` from a different binary
(`libnrtucode_extisa.so`). The two reads *appear* to disagree on whether the first
appearance is at v4 (MARIANA) or v4+ (MARIANA_PLUS).

**THE EVIDENCE (this session, decisive):** `[HIGH/OBSERVED]`

- In `libnrtucode_internal.so` — the **MARIANA** PE DEBUG DRAM (self-name
  `S: BEGIN on mariana` @`0x89f`) carries all 4 `PeManageSeed` strings at offsets
  `0x871` (SAVE) / `0x888` (LOAD) / `0x23e0` (micro-op LdWeight) / `0x2407` (micro-op
  Matmul:is_load).
- The **MARIANA_PLUS** PE DEBUG DRAM (self-name `S: BEGIN on mariana_plus` @`0x89f`)
  carries the **SAME 4** strings at the **SAME offsets** — the handler-name pool is
  byte-identical (the `xxd`/`strings` diff above shows the *only* change in that
  cluster is the self-name token).
- CAYMAN PE = **0** hits for `ManageSeed`/`MX`/`ConvLut`
  ([CAYMAN × PE §1.5](./cayman-pe.md), re-confirmed).

> **CORRECTION — `PeManageSeed` FIRST SHIPS at v4 (MARIANA), and is RETAINED at v4+
> (MARIANA_PLUS). The [MARIANA × PE](./mariana-pe.md) attribution is CORRECT.** The
> "MARIANA_PLUS / v4-only" label is a **carve-coverage artifact**: FW-66 worked from a
> *different* binary (`libnrtucode_extisa.so`) and carved only the **CAYMAN** and
> **MARIANA_PLUS** PE images — it never carved the **MARIANA (non-plus)** PE image that
> sits between them, so it could not distinguish "first at MARIANA" from "first at
> MARIANA_PLUS". Because the v4-class image it *did* carve self-named `mariana_plus`,
> it read the feature as MARIANA_PLUS-specific. The cross-check settles it: in
> `extisa.so` the MARIANA non-plus PE image **also** carries `PeManageSeed` (at
> `0x45fa31`, adjacent to `BEGIN on mariana` @`0x45fa5f`), while no CAYMAN PE image
> carries it. The boundary is **CAYMAN(no) → MARIANA(first) → MARIANA_PLUS(retained)**,
> confirmed byte-exact in BOTH `internal.so` and `extisa.so`. FW-66's micro-op
> *semantics* (RNG seed save/restore via LdWeight+Matmul micro-ops) stand unchanged;
> only its per-gen first-appearance *label* is corrected. Note "NeuronCore-v4" at the
> *architecture* level covers BOTH the MARIANA and MARIANA_PLUS firmware labels
> (MARIANA_PLUS shares the mariana ISA / coretype 29); FW-66's collapse of
> "NeuronCore-v4" onto "the MARIANA_PLUS firmware image only" is the point corrected
> here. `[HIGH/OBSERVED — the per-gen presence is byte-exact from the firmware
> self-naming]`

---

## 7. dtype + PROF deltas — minimal dtype, PROF byte-IDENTICAL

**dtype: unchanged, minimal.** The only dtype constants in any MARIANA_PLUS PE image
are `NEURON_ISA_TPB_DTYPE_{UINT32,INT32,FP32}` in the byte-identical `move.cpp:41`
assertion (*"highest priority is full-register moves. TODO other dtypes"*) + the
`alu_op.cpp:231` "not supported dtype" — the same three MARIANA (and CAYMAN) carry.
`FP4`/`CPTC`/`MXTENSOR`/`SFP8`/`proc_4bit`/`QuantizeMx` → **0 hits across all 8
MARIANA_PLUS PE carves** (this session). The FP4/MX matmul-path footprint is **not** a
dtype string but the retained `LdweightsMX`/`MatmulMX` handler pair (§5) — numeric in
the decode path, never a named-string assert. `[HIGH/OBSERVED-negative]`

**PROF: byte-for-byte IDENTICAL to MARIANA.** Both profiling blobs are `cmp -s` clean
against the MARIANA PE tables (this session):

- **PROF_CAM** `43475cec…` (`0x400`) — **22 armed 16-byte records** `{opcode(u32)@0,
  mask=0xff@4, enable=1@8, rsvd}` (record layout confirmed this session:
  `a100 0000 ff00 0000 0100 0000`), opcode set `{0x0,1,2,3,6,7,9,0x9f,0xa,0xa0,a1,a2,
  a3,a5,a7,a8,a9,aa,ab,b1,b2,0xe4}` — the PE matmul block (`0xa0..ab`), the control
  core (`0x1/2/3/6/7`), and the new-opcode overlap (`0x9/0xa/0x9f/0xe4`). 8-bit
  opcodes only (no 9-bit) — the SAME PE-specific arming as MARIANA PE.
- **PROF_TABLE** `d93b723e…` (`0x2000`) — identical preallocation (the MARIANA PE
  header is zeroed; 59 nonzero words).

This is the gen-wide per-engine PROF reuse: MARIANA_PLUS **reuses MARIANA's per-engine
PROF_CAM/TABLE verbatim**, matching the same byte-identity on
[ACT](./mariana-plus-act.md) (`326bc0dd`) and [DVE](./mariana-plus-dve.md)
(`ca588683`). A strong v4/v4+ kinship signal, and confirmation that the recompile did
not re-arm anything. `[HIGH/OBSERVED]`

---

## 8. The ONE functional delta — a DGE reshape fast-path (the v4+ feature on PE)

The only substantive image-level change distinguishing MARIANA_PLUS PE from MARIANA PE
is **a new DGE (Descriptor-Generation Engine) reshape fast-path**, evidenced by 4 new
source/helper strings present on MARIANA_PLUS PE and **absent on MARIANA PE**
(this session, `MARIANA_PLUS=1` / `MARIANA=0` each):

| String (MARIANA_PLUS-only) | kind |
|---|---|
| `dge_decode_fast.cpp` | new source file |
| `dge_reshape_memcopy_transpose_fast` | new DGE reshape helper |
| `tensor_reshape_transpose_sb2sb` | new SB→SB transpose reshape kind |
| `wait_for_credit` | new DMA credit-flow primitive |

These are **source/helper names, NOT dispatch handler names** — the handler set is
unchanged (§5). They *refine* the existing DGE/Reshape subsystem present on **both**
generations (shared `dge_backend_rtl`, `dge_reshape`, `analyze_tensor_reshape`,
`dge_ctx_num`, `addr_bits.hpp`, `transform_addr_for_pool_tpb_rerouting`). The full
DEBUG-DRAM string-set delta is **exactly the ACT/DVE shape** (`+5 / −2`): only-MPLUS =
`{those 4 + "BEGIN on mariana_plus"}`; only-MARIANA = `{"BEGIN on mariana",
"S: push REGWRITE to DMA[%d]"}` (the latter likely folded into the fast path). This is
the same v4+ delta IMG-13/14 found on ACT and DVE — the DGE fast-path is a **SEQ-infra
feature, gen-wide on the NX engines** (now confirmed on ACT, DVE, AND PE, even though
PE is the matmul engine). `[HIGH/OBSERVED strings; "throughput optimization" reading
INFERRED-HIGH]`

```c
// The v4+ change, in one line of pseudocode (INFERRED-HIGH from the names +
// the shared DGE context — see firmware/dge/dge-reshape.md):
//
//   if (dge_decode_fast(req) && is_sb2sb_transpose(req)) {
//       tensor_reshape_transpose_sb2sb(req);   // NEW: fast reshape/memcopy-transpose
//       wait_for_credit(dma_ring);             // NEW: explicit DMA credit-wait
//   } else {
//       analyze_tensor_reshape(req);           // the SHARED slow path (both gens)
//   }
```

There is **no `mariana_plus` errata string**; the `mariana-4062` errata is
DVE-specific and absent on PE in both gens (0 hits this session). The source tree
stays `cayman/seq/src/…` on MARIANA_PLUS; `addr_bits.hpp`/`translate_cayman+.hpp`/
`transform_addr_for_pool_tpb_rerouting` are on both. `[HIGH/OBSERVED]`

---

## 9. The recompile evidence — size table + directional growth

| IMAGE | MARIANA sz / sha | MARIANA_PLUS sz / sha | ΔSize | identical? |
|---|---|---|---|---|
| PERF_IRAM | `0x12ce0` / `a077f110` | `0x172c0` / `c4e2115d` | `+0x45e0` | NO (recompile+DGE) |
| PERF_DRAM | `0x2da0` / `08cd432d` | `0x2ec0` / `9a4abf20` | `+0x120` | NO |
| TEST_IRAM | `0x12ca0` / `eba5f4dc` | `0x16de0` / `e08c8d41` | `+0x4140` | NO |
| TEST_DRAM | `0x30e0` / `4eb40842` | `0x32a0` / `3b48f7e2` | `+0x1c0` | NO |
| DEBUG_IRAM | `0x18c20` / `6600e24a` | `0x19e00` / `6f02f447` | `+0x11e0` | NO |
| DEBUG_DRAM | `0x6400` / `4e00d0c3` | `0x6560` / `45ee6ef5` | `+0x160` | NO |
| **PROF_CAM** | `0x400` / `43475cec` | `0x400` / `43475cec` | `+0x0` | **YES** |
| **PROF_TABLE** | `0x2000` / `d93b723e` | `0x2000` / `d93b723e` | `+0x0` | **YES** |

**6/8 distinct (full recompile); 2/8 (PROF) byte-identical.** The directional
signature is telling: **IRAM GREW in every variant** (PERF `+0x45e0`, TEST `+0x4140`,
DEBUG `+0x11e0`) — the **opposite** direction from the CAYMAN→MARIANA *shrink*
([MARIANA × PE §8](./mariana-pe.md)). The growth is the new DGE fast-path code (§8) —
exactly the v4→v4+ reversal IMG-13/14 found for ACT and DVE. Full recompile with
relocated layout + inserted code (first IRAM divergence at a literal @`0x212`), **not**
a patch. `[HIGH/OBSERVED]`

> **NOTE — `engine_idx` is runtime-computed (= 0, PE).** The shipped ISA enum
> `NEURON_ISA_TPB_NEURON_ENGINE { PE=0, ACT=1, POOL=2, DVE=3, TPB_SP=4, TOP_SP=5 }`
> (`neuron_cayman_arch_isa/tpb/aws_neuron_isa_tpb_common.h:139-146`, re-read this
> session) fixes PE = 0. MARIANA_PLUS PE carries the runtime-identity string
> `S: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u`
> exactly as MARIANA — the same flat image is loaded at the PE base and self-locates
> `engine_idx` at boot, which is why all NX engines share the identical reset + boot
> trampoline. MARIANA_PLUS shares the mariana ISA (no `neuron_mariana_plus_arch_isa`
> dir; its own `arch-headers/mariana_plus/` register-map dir only). `[HIGH/OBSERVED
> string + enum; runtime-compute INFERRED]`

---

## 10. Adversarial self-verification

Five strongest claims, re-challenged against the binary this session:

1. **Handler roster byte-for-name identical (`+0/−0`); PeManageSeed/MX retained.**
   *Challenge:* a glue artifact could mask a real add/drop, or the v4 handlers could
   have been dropped at v4+. *Re-verify:* glue-stripped `69 == 69`, strict
   end-anchored `27 == 27`, set-diff EMPTY both directions; all 9 PE handlers present
   in both gens with identical counts (`ManageSeed` 4/4, `LdweightsMX`/`MatmulMX`/
   `ConvLut`/`MatmulSparse`/`PeRegWrite`/`LdTags` 1/1, `Ldweights` 2/2); the
   handler-name pool `0x23c0..0x26c0` byte-identical. **HOLDS.**
2. **PeManageSeed RETAINED at v4+, byte-identical to MARIANA.** *Challenge:* the
   strings could be at shifted offsets, or be a stray copy. *Re-verify:* all 4 at
   `0x871/0x888/0x23e0/0x2407` on BOTH MARIANA_PLUS and MARIANA DEBUG DRAM; the only
   delta in that cluster is `BEGIN on mariana_plus` (`0x89f`) vs `BEGIN on mariana`;
   CAYMAN PE = 0 hits. **HOLDS.**
3. **The IMG-10/FW-66 resolution: first ships at v4 (MARIANA).** *Challenge:* maybe
   the MARIANA image really lacks PeManageSeed and FW-66 is right. *Re-verify:* the
   self-name proves the gen — the MARIANA (`BEGIN on mariana`) PE image carries all 4
   seed strings; FW-66's "v4+-only" came from carving only CAYMAN + MARIANA_PLUS (a
   coverage gap), and the `extisa.so` cross-check finds PeManageSeed adjacent to
   `BEGIN on mariana` @`0x45fa5f`. Boundary = CAYMAN(no)→MARIANA(first)→MPLUS(retained).
   **HOLDS.**
4. **SAME +0x1c reset, no further shift.** *Challenge:* a second relocation could hide
   in a later variant. *Re-verify:* all 3 MARIANA_PLUS IRAM variants head
   `06 7d 00 00 … 86 7e` — byte-identical to MARIANA; `ncore2gp` decodes
   `j 0x1f8`/`j 0x204`; PERF-IRAM shared prefix `0x212`, first divergence a relocated
   literal. **HOLDS.**
5. **PROF byte-identical to MARIANA.** *Challenge:* same size could mask different
   content. *Re-verify:* `cmp -s` clean for both PROF_CAM (`43475cec`) and PROF_TABLE
   (`d93b723e`); the 22 armed records re-decoded match the MARIANA PE set (16-byte
   `{op,mask=0xff,enable=1}` records). **HOLDS.**

**Honesty ledger.** *HIGH/OBSERVED (reproduced this session):* container sha256
`b7c67e89…` (MATCH); 14 getters (`nm` + IDA sidecar = 14); 8 carves sha-reproduced +
`cmp`-reconciled to `libnrtucode.a` `.rodata`; MARIANA baseline 8/8 re-hashed and
MATCH; contiguity 3/3 (PE third, ACT→DVE→PE→POOL); reset/boot/DRAM-magic byte-identical
to MARIANA; dispatch chain `0x2934..0x2a78` byte-identical (`ncore2gp` decode, 29 ops);
17-tbl `@0x814` `+0x1c` reloc, log `@0x858`; handler `69==69`/`27==27` `+0/−0`, 9 PE
handlers retained; PeManageSeed 4 strings same offsets, `BEGIN on mariana_plus`;
PROF_CAM/TABLE `cmp`-clean (22 armed records); 4 DGE strings present-vs-absent;
`{UINT32,INT32,FP32}` only, FP4/MX 0 hits ×8; IRAM grew; no `mariana-4062`/no
`mariana_plus` errata; ISA enum PE=0.
*MED/CARRIED:* PERF per-opcode dispatch-row binding (FLIX-desync, the documented
limit); "DGE fast-path = throughput optimization" (string/symbol level, INFERRED-HIGH);
the `MX↔FP4/MX` dtype linkage (INFERRED-HIGH from name + [mx-dequant.md](../firmware/kernels/mx-dequant.md));
`PeManageSeed` SR-RNG semantics + `PE_SEED_MODE`/`S2S1D2_PE_SEED_STRUCT`/`SMX1_LW`/
`SMX1D3_MM` (CARRIED from [pe-matmul.md](../firmware/kernels/pe-matmul.md)).
*LOW/NOT CLAIMED:* which silicon/runtime selects MARIANA vs MARIANA_PLUS, DEBUG vs
PERF vs TEST; the exact `PeManageSeed` handler-body instruction order (FLIX-desync);
the numeric struct offsets of `S3_LW`/`S3D3_MM`.

---

## 11. Cross-references

- [MARIANA × PE image](./mariana-pe.md) — **the v4 diff base** this page diffs against
  (the 9-handler roster, the `PeManageSeed`/MX first-ship proof, reset `j 0x1f8`,
  PROF `43475cec`/`d93b723e`, the FW-66 boundary flag this page resolves).
- [CAYMAN × PE image](./cayman-pe.md) — the v3 baseline where `PeManageSeed`/MX/ConvLut
  are **absent** (0 hits) — the lower bound of the first-ship boundary.
- [MARIANA_PLUS × ACT](./mariana-plus-act.md) / [MARIANA_PLUS × DVE](./mariana-plus-dve.md)
  — the sibling v4+ image diffs (the same null functional delta + the DGE fast-path +
  PROF byte-identity; the `+0x20` ACT vs `+0x1c` PE sub-table reloc difference).
- [PE Matrix-Multiply Path](../firmware/kernels/pe-matmul.md) — the byte-exact per-gen
  operand structs, `PeManageSeed`/`PE_SEED_MODE`/`S2S1D2_PE_SEED_STRUCT` semantics, the
  `SMX1_LW`/`SMX1D3_MM` MX structs, and the CAYMAN→MARIANA→MARIANA_PLUS→MAVERICK
  evolution.
- [MX (Microscaling) Dequant Compute Paths](../firmware/kernels/mx-dequant.md) — the
  POOL in-band `proc_4bit_mx_8` vs the PE out-of-band `scale_addr`/E8M0 MX matmul.
- [MARIANA+ generation delta](../generations/mariana-plus-delta.md) — the v4+ model
  (register-map refresh + recompile + DGE fast-path; shared mariana ISA).
- [DGE Reshape Engine](../firmware/dge/dge-reshape.md) — the `dge_decode_fast` /
  `tensor_reshape_transpose_sb2sb` / `wait_for_credit` fast-path machinery.
- [Per-Engine Depth](../uarch/per-engine-depth.md) — the PE row (`engine_idx=0`,
  `0x01..0x0A`/`0xe4` roster) this image realizes.
- [Image Catalog Index](./image-catalog-index.md) — the full getter map (MARIANA_PLUS
  NX_PE rows 413–426).
- [Confidence & Walls Model](../reference/confidence-model.md) — the tag taxonomy.
