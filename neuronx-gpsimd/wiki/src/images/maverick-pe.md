# MAVERICK × PE image (cross-gen diff vs MARIANA_PLUS PE)

This page diffs the **MAVERICK (v5) Processing-Element firmware image** — the
systolic-matmul / TensorE sequencer (`engine_idx = 0`, the same `cayman/seq/`
SEQ-dispatch chassis as [CAYMAN × PE](./cayman-pe.md) /
[MARIANA × PE](./mariana-pe.md)) — against the committed, byte-true
[MARIANA_PLUS × PE baseline](./mariana-plus-pe.md). Every getter address, size,
sha256, reset byte, PROF record, opcode, and struct field below is read directly
from `libnrtucode_internal.so` (sha256 `b7c67e89…632fc329b`) via its **10**
`MAVERICK_NX_PE_*_get` accessors, carved from identity-mapped `.rodata`, decoded
with the shipped Cadence Vision-Q7 `ncore2gp` `xtensa-elf-objdump`, and
cross-read against the shipped clean `neuron_maverick_arch_isa` C headers.

> **CRITICAL WALL — MAVERICK (v5) is HEADER-OBSERVED only.** The container header,
> the carved blobs, the **PROF-CAM arming**, and the **reset bytes** are all
> directly **OBSERVED** (decoded with stock binutils + `ncore2gp`).
> But MAVERICK PE ships **NO DEBUG image**, so the named-handler roster carries **no
> carved `S:` strings to anchor a handler body** — the roster is **INFERRED** from
> the PROF-CAM arming + the shipped OPCODE enum + the dispatch structure + the
> present MAC datapath. Every v5-**interior** claim (handler bodies, per-row
> dispatch binding, `arch_id`) is flagged **INFERRED**. The image-level deltas
> below are **OBSERVED**.

> **THE HEADLINE — MAVERICK × PE is a GENUINE NEW v5 GENERATION of the
> systolic-matmul sequencer, NOT a recompile of MARIANA_PLUS PE.** Five image-level
> facts prove it, each byte-OBSERVED:
>
> 1. **A NEW v5 reset geometry: `j 0x1d8` (−0x20 vs MARIANA's `j 0x1f8`),
>    enter_run @`0x94`.** This is a v5 **structural** change — DISTINCT from the v4
>    `+0x1c` shift (CAYMAN→MARIANA, stable through MARIANA_PLUS at `j 0x1f8`,
>    enter_run @`0x90`). It is the EXACT geometry the MAVERICK matrix carries on
>    [DVE](./maverick-dve.md). [HIGH/OBSERVED]
> 2. **NO PE DEBUG image** — 10 getters (vs 14), 0 `MAVERICK_NX_PE_DEBUG_*`; the
>    named SEQ handler strings are amputated **firmware-wide** on MAVERICK PE
>    (0 `S:` strings in any of the 6 carves). [HIGH/OBSERVED]
> 3. **A RE-AUTHORED PROF** — CAM `85d857a7` and TABLE `e94d413a`, **both distinct**
>    from MARIANA_PLUS's `43475cec`/`d93b723e` (which were MARIANA-verbatim), **−3
>    armed** (18 vs 21). This is the v5 per-engine re-authoring — the OPPOSITE of
>    MARIANA_PLUS's byte-identical PROF reuse. [HIGH/OBSERVED]
> 4. **The DGE fast-path DROPPED region-wide** — 0 of the 4 `dge_decode_fast`
>    family strings (all NEW-ADDED on MARIANA_PLUS PE). [HIGH/OBSERVED]
> 5. **~64% smaller, internal-twin-EXCLUSIVE, an independent build** — total
>    `0x1e780` vs `0x56960` (`−0x381e0`); **0** `.a` members; **7.6%** positional
>    block-similarity vs MARIANA_PLUS PE (byte-1 divergence). [HIGH/OBSERVED]

> **THE MATMUL / MX VERDICT — matmul RETAINED + MX REORGANIZED.** The core matmul
> `LDWEIGHTS 0x01` / `MATMUL 0x02` AND the MX pair `LDWEIGHTS_MX 0x09` /
> `MATMUL_MX 0x0A` are **all PROF-armed** on the MAVERICK PE CAM and all marked
> `// Y` (maintained) in the shipped maverick OPCODE enum; the full widening-MAC
> datapath (205 `ivp_mul*`) is present in the PE IRAM. **The v5 MX evolution is a
> STRUCT/ACCESS-PATTERN REORGANIZATION, not new dispatch handlers:** the separate
> `MatmulMX`/`LdweightsMX` instructions are **folded into** the unified
> `Matmul`/`Ldweights` via the **MXTensorV2** access pattern — the shipped
> maverick `S3D3_MM`/`S3_LW` structs gain `TILE_SIZE`/`TILE_SEL`/`MX_PERF_MODE`,
> the legacy `SMX1D3_MM` (`MatmulMX`) struct header is stamped *"DEPRECATED on
> Neuron: Use the regular Matmul instruction with MXTensorV2 access patterns
> instead"*, and the dtype enum adds `FP8_EXP2(0x11)`/`INT4(0x12)`/`SFP8_E8..E5
> (0x13..0x16)` over `FP4_EXP2(0x10)`. `PeManageSeed (0x08)` is RETAINED at the ISA
> level. [HIGH/OBSERVED enum+struct+PROF; handler bodies INFERRED]

This is the **opposite** delta-class from the sibling
[MARIANA_PLUS × PE](./mariana-plus-pe.md): that page proved MARIANA_PLUS = MARIANA
*recompiled + a DGE fast-path* (byte-identical reset, byte-identical opcode
compare-chain, byte-identical PROF). MAVERICK PE shares **none** of those — it is a
ground-up v5 build, consistent with the MAVERICK ACT→DVE fold the
[MAVERICK × ACT](./maverick-act.md) / [MAVERICK × DVE](./maverick-dve.md) pages
establish as a real structural gen-step.

Confidence/evidence tags follow the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. PROF-CAM arming + reset bytes are **OBSERVED**;
handler bodies + per-row dispatch binding + `arch_id 36` are **INFERRED**.

Related pages: [MARIANA_PLUS × PE (the diff base)](./mariana-plus-pe.md) ·
[MARIANA × PE](./mariana-pe.md) · [MAVERICK × DVE](./maverick-dve.md) ·
[MAVERICK × ACT](./maverick-act.md) ·
[PE Matrix-Multiply Path](../firmware/kernels/pe-matmul.md) ·
[MX (Microscaling) Dequant](../firmware/kernels/mx-dequant.md) ·
[MAVERICK generation profile](../generations/maverick-profile.md) ·
[Firmware-Image Accessor Index](./image-catalog-index.md).

> **NOTE — the objects used.** Container:
> `…/custom_op/c10/lib/libnrtucode_internal.so` (sha256
> `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b`, ELF64 x86-64
> DYN, **not stripped**; MATCH). First R `LOAD` is the
> identity map (`off 0x0 == vaddr 0x0`), so each `<NAME>.data` accessor address is
> simultaneously the `.rodata` VA and the file offset of its blob — carve =
> `so[ptr : ptr+size]`. **IRAM file-offset == device IRAM VA** (reset vector at byte
> 0); **DRAM string-file-offset == device DRAM VA − `0x80000`**. Disassembler:
> `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump`
> (GNU Binutils 2.34.20200201 Xtensa Tools 14.09, `XTENSA_CORE=ncore2gp`, ConfigName
> `Xm_ncore2gp`, uarch Cairo).

---

## 1. The delta table (MARIANA_PLUS PE baseline → MAVERICK PE)

The whole page in one table. `(==)` marks invariant rows; the **bold** rows are the
v5 gen-step changes. Read the [MARIANA_PLUS × PE page](./mariana-plus-pe.md) for the
diff base; this table documents the cross-gen delta, leading with the
genuine-new-gen headline + the new v5 reset geometry + the matmul-retained /
MX-reorganized verdict.

| PROPERTY | MARIANA_PLUS PE (baseline) | MAVERICK PE (this page) | Δ |
|---|---|---|---|
| getters (`nm`) | 14 (PERF/TEST/DEBUG × {IRAM,DRAM} + PROF) | **10 (PERF/TEST × {IRAM,DRAM} + PROF)** | **`−4` — DEBUG dropped** |
| real / empty images | 8 real + 6 zero cursors | **6 real + 4 zero cursors** | **`−2` real (no DEBUG)** |
| `.a` byte-reconcile | 14 `.a` members (8/8 reconciled) | **0 `.a` members** | **internal-twin-EXCLUSIVE** |
| packaging | flat IRAM/DRAM (not ELF) | flat IRAM/DRAM (not ELF) | `(==)` |
| **reset vector** | `06 7d 00 00` → **`j 0x1f8`** | `06 75 00 00` → **`j 0x1d8`** | **`−0x20` NEW v5 geometry** |
| **2nd vector** | `86 7e 00 00` → `j 0x204` → `halt 0` | `86 76 00 00` → `j 0x1e4` → `halt 0` | **`−0x20`** |
| **enter_run target** | `const16 a0,144` → **@`0x90`** | `const16 a0,148` → **@`0x94`** | **`+4`** |
| boot dispatch | `extui;addx4;l32i;jx` @`0x217` | same form @`0x1f7` | `−0x20` (same model) |
| boot-stub body @`0xc` | `a0 71 69 80 …` | **byte-identical** @`0xc..0x1c` | `(==)` shell; image diverges @`0x1c` |
| DRAM `.globstruct` magic | `0x6099cb34` | `0x6099cb34` | `(==)` unchanged since CAYMAN |
| dispatch model | RAW-opcode compare-chain + table | **addx4-indexed jump-thru-DRAM-table** | model shift (see §3) |
| compare-chain `0x2934..0x2a78` | byte-identical to MARIANA | **N/A — independent build** | `byte-1` divergence |
| **HANDLER ROSTER** | 9 PE (5 CAYMAN + 4 MARIANA) | **RETAINED (all `// Y` in enum)** | **`+0/−0` roster** |
| `PeManageSeed 0x08` | PRESENT (DEBUG strings) | **RETAINED (enum `// Y` + `seed_mode:2`)** | retained (no DEBUG string) |
| `LdweightsMX 0x09`/`MatmulMX 0x0A` | PRESENT (separate instrs) | **RETAINED + DEPRECATED (folded into Matmul)** | **MX REORGANIZED** |
| `ConvLutLoad 0xe4` | PRESENT | **RETAINED (`// Y`, PROF-armed)** | `(==)` |
| **PROF_CAM** | `43475cec` (22 enabled / 21 armed) | **`85d857a7` (19 / 18 armed)** | **RE-AUTHORED, `−3` armed** |
| **PROF_TABLE** | `d93b723e` | **`e94d413a`** (first diff @byte 1543) | **RE-AUTHORED** |
| de-armed on v5 | — | `{0x03 PE_REG_WRITE, 0x06 LDTAGS, 0x07 MATMUL_SPARSE}` | **`−3`** (handlers still `// Y`) |
| matmul-armed | `0x01/0x02` + MX `0x09/0x0A` | **`0x01/0x02` + MX `0x09/0x0A` RETAINED-armed** | `(==)` matmul kept |
| MX dtypes | `FP4_EXP2 0x10` | **`+FP8_EXP2 0x11/INT4 0x12/SFP8_E8..E5 0x13..16`** | **+6 micro-formats** |
| MX struct fields | `transpose/perf_opt/row_grp/col_grp/psum_zero` | **`+TILE_SIZE/TILE_SEL/MX_PERF_MODE`** | **struct rework** |
| **DGE fast-path** | PRESENT (4 strings, NEW at v4+) | **DROPPED (0/4 in PE carves)** | **`−4` removed** |
| dtype string surface | `{UINT32,INT32,FP32}` only | `{UINT32,INT32,FP32}` only | `(==)` numeric MX |
| `mariana-4062` errata | absent (DVE-only) | absent; no `maverick-NNNN` | `(==)` |
| **PERF_IRAM size** | `0x172c0` | **`0xbd60`** | **`−0xb560` SHRANK** |
| total PE footprint | `0x56960` (8 imgs) | **`0x1e780` (6 imgs)** | **`−0x381e0` ~64% smaller** |
| build relation | recompile of MARIANA (`0x212` shared prefix) | **independent v5 (7.6% block-sim, byte-1)** | **NOT a recompile** |
| engine_idx | `0` (PE) | `0` (PE) | `(==)` |

**Verdict.** A MARIANA_PLUS↔MAVERICK PE swap is **NOT** a recompile — it is a
**genuine new v5 generation**: a new `j 0x1d8` reset, no DEBUG image, a re-authored
PROF (`−3` armed), the DGE fast-path dropped, ~64% smaller, independent build, 0
`.a` members. The **matmul roster is RETAINED** (5 CAYMAN core + 4 MARIANA
additions, all `// Y`; `0x01/0x02` + MX `0x09/0x0A` PROF-armed; the widening-MAC
datapath present) and the **MX path is REORGANIZED** (`MatmulMX`/`LdweightsMX`
folded into `Matmul`/`Ldweights` via `MXTensorV2` + `TILE_SIZE`/`MX_PERF_MODE`;
legacy MX opcodes/structs retained-and-deprecated; `+FP8/INT4/SFP8` dtypes). The v5
gen-step the MAVERICK matrix carries on DVE is **confirmed for the matmul engine**.
`[HIGH/OBSERVED deltas; handler bodies + arch_id INFERRED]`

---

## 2. The 10 MAVERICK × PE getters + carve provenance

`CLS=NX`, `ENG=PE`, `engine_idx=0`. `nm libnrtucode_internal.so | rg -c
'MAVERICK_NX_PE_.*_get$'` → **10** (vs MARIANA_PLUS PE's **14**), and **`rg -c
'MAVERICK_NX_PE_DEBUG'` → 0**: there is **NO** `MAVERICK_NX_PE_DEBUG_*` getter at
all. MAVERICK PE ships PERF/TEST × {IRAM,DRAM} (4) + PROF{CAM,TABLE} (2) = **6 real
images**; the DEBUG image — the only place the named SEQ handler strings live — is
**dropped** on the v5 PE (only DVE, the MAVERICK NX family head, keeps a DEBUG image
on v5). Each real getter is the canonical stub `lea <blob>(%rip),%rax ; movq
$<size>,(%rsi) ; ret` (e.g. `MAVERICK_NX_PE_PERF_IRAM_get @0x9b5720: lea
-0x1021e7(%rip),%rax # 8b3540 ; movq $0xbd60,(%rsi) ; ret`).

| VARIANT | REGION | ACCESSOR (.text VA) | IMG-PTR (.rodata VA == file off) | SIZE | sha256 | status |
|---|---|---|---|---|---|---|
| PERF | IRAM | `0x9b5720` | `0x8b3540` | `0xbd60` | `dd8b9695…` | REAL (SEQ code) |
| PERF | DRAM | `0x9b5740` | `0x8bf2a0` | `0x2040` | `3bb5f20e…` | REAL (SEQ data) |
| PERF | SRAM | `0x9b5760` | `0x8c12e0` | `0x0` | — | EMPTY → POOL PERF_IRAM |
| PERF | EXTRAM | `0x9b5780` | `0x8c12e0` | `0x0` | — | EMPTY (cursor) |
| TEST | IRAM | `0x9b5820` | `0x8d07e0` | `0xc320` | `07a24fba…` | REAL (SEQ code) |
| TEST | DRAM | `0x9b5840` | `0x8dcb00` | `0x22c0` | `ffa7b824…` | REAL (SEQ data) |
| TEST | SRAM | `0x9b5860` | `0x8dedc0` | `0x0` | — | EMPTY → POOL TEST_IRAM |
| TEST | EXTRAM | `0x9b5880` | `0x8dedc0` | `0x0` | — | EMPTY (cursor) |
| **PROF** | **CAM** | `0x9b5ce0` | `0x9a66a0` | `0x400` | **`85d857a7…`** | **REAL (re-authored)** |
| **PROF** | **TABLE** | `0x9b5d00` | `0x9a6aa0` | `0x2000` | **`e94d413a…`** | **REAL (re-authored)** |

All 6 real carves (`internal.so[IMG-PTR : IMG-PTR+SIZE]`) reproduce these sha256
exactly. The `(img-ptr,size)` pairs MATCH
[image-catalog-index.md](./image-catalog-index.md) rows 747–756 byte-for-byte, and
the catalog's `MAVERICK_NX_PE_DEBUG_*` row (count `0`) corroborates the no-DEBUG
finding.

> **GOTCHA — single-source carve, NO `.a` byte-reconcile.** `libnrtucode.a` carries
> **0** MAVERICK members (`ar t … | rg -ic maverick` → 0; the archive tops at
> MARIANA_PLUS). Unlike the MARIANA_PLUS PE page's 8/8 `.so`↔`.a` `.rodata`
> reconciliation, **no `.a` member exists to cross-validate a MAVERICK PE carve** —
> MAVERICK is `internal.so`-EXCLUSIVE (the same internal-twin pattern the
> [MAVERICK × DVE](./maverick-dve.md) / [MAVERICK × ACT](./maverick-act.md) pages
> found). Cross-validation here is by the `(img-ptr,size)` getter parse + the
> contiguity arithmetic + the `ncore2gp` decode. The `.a` topping at MARIANA_PLUS is
> itself a v5 gen-step signature. `[HIGH/OBSERVED]`

> **NOTE — PE is SECOND in the MAVERICK NX block (DVE → PE → POOL).** With ACT
> amputated (folded into DVE per the [MAVERICK × ACT](./maverick-act.md) /
> [MAVERICK × DVE](./maverick-dve.md) pages), DVE is the family head and PE follows
> it. Contiguity (PERF slot, `.data` sort): DVE_DEBUG_DRAM end
> `0x8ad5c0+0x5f80 = 0x8b3540` == PE_PERF_IRAM; PE_PERF_IRAM end `0x8b3540+0xbd60 =
> 0x8bf2a0` == PE_PERF_DRAM; PE_PERF_DRAM end `0x8bf2a0+0x2040 = 0x8c12e0` ==
> POOL_PERF_IRAM (PE→POOL adjacency). The 4 PE zero-size cursors all resolve
> (`objdump` `lea`) to `MAVERICK_NX_POOL_{PERF,TEST}_IRAM.data` — PE runs entirely
> from IRAM+DRAM, **standalone** (not folded). `[HIGH/OBSERVED]`

---

## 3. The reset/boot vector — the NEW v5 `−0x20` geometry (`j 0x1d8`, enter_run @`0x94`)

This is the v5 **structural** divergence, and it is **NOT** the v4 `+0x1c` shift.
The MAVERICK PE PERF IRAM head is **byte-distinct from MARIANA_PLUS at byte 1**
(`xxd -l16` — PERF/TEST head identically):

```
MARIANA_PLUS PE IRAM:  06 7d 00 00 | 00 00 | 86 7e 00 00 | 00 00 | a0 71 69 80
MAVERICK     PE IRAM:  06 75 00 00 | 00 00 | 86 76 00 00 | 00 00 | a0 71 69 80
                       └ j 0x1f8→1d8┘       └ j 0x204→1e4┘        └ shared NX stub ┘
                            (−0x20)              (−0x20)
```

The `j` immediate byte-1 is `0x7d` on MARIANA(_PLUS) and **`0x75` on MAVERICK** — a
genuine `−0x20` relocation of the boot entry, **distinct from** the v4 model
(CAYMAN's `0x76` → MARIANA's `0x7d`, the stable `+0x1c`). Decoded
instruction-exact with the shipped `ncore2gp` `xtensa-elf-objdump` off the
MAVERICK PE PERF IRAM carve:

```text
0x000:  06 75 00     j        0x1d8      ; primary reset vector → boot trampoline   (−0x20 vs MARIANA 0x1f8)
0x006:  86 76 00     j        0x1e4      ; secondary vector → halt trap             (−0x20 vs MARIANA 0x204)
0x1d8:  04 00 00     const16  a0, 0
0x1db:  04 94 00     const16  a0, 148    ; a0 = 0x94  (C enter_run prologue)        (+4 vs MARIANA 0x90)
0x1de:  a0 00 00     jx       a0         ; jump into the C boot prologue
0x1e4:  00 52 00     halt     0          ; 2nd vector is a HALT trap
0x1f7:  30 30 34     extui    a3, a3, 0, 4
0x1fa:  40 43 a0     addx4    a4, a3, a4 ; SEQ boot dispatch — addx4-indexed
0x1fd:  42 24 00     l32i     a4, a4, 0  ; jump-through-DRAM-table
0x205:  a0 04 00     jx       a4
```

So the PE v5 reset shift is **primary `−0x20`** (`0x1f8`→`0x1d8`), **secondary
`−0x20`** (`0x204`→`0x1e4`), **enter_run `+4`** (`0x90`→`0x94`), boot dispatch
`−0x20` (`0x217`→`0x1f7`). This is the **exact** v5 geometry the MAVERICK matrix
carries on [DVE](./maverick-dve.md).

> **NOTE — `−0x20` is the NX-compute class shift, not gen-wide.** The `−0x20` /
> `j 0x1d8` geometry is shared by the **NX-compute** engines (PE, DVE, NX_POOL — head
> `06 75`); the **SRAM-resident** cores differ: SP is `−0x14` (Top-Sync stub, head
> `06 78` → `j 0x1e4`) and Q7_POOL is `−0x1c` (head `06 78` → `j 0x1e4`). What **all**
> v5 engines share is **`enter_run @0x94`** (the +4 v5 invariant). See the unified
> per-engine reset table in
> [maverick-profile §8](../generations/maverick-profile.md). `[HIGH/OBSERVED]` The boot-stub *body* at `0xc..0x1c` (`a0 71 69
80 …`) is byte-identical to MARIANA_PLUS (the canonical NX boot shell is reused
verbatim), but the image diverges from `0x1c` onward — an independent v5 build that
reuses the NX boot-stub bytes. The decode of `enter_run @0x94` lands on a real C
prologue (`extui a0,a0,0,3 ; l32i.n a6,a8,40 ; beqz a2,0xa0 ; callx0 a2`), not a
stub. The DRAM head is `34 cb 99 60` = `0x6099cb34`, the `.globstruct`
dispatcher-state magic, unchanged since CAYMAN. `[HIGH/OBSERVED — all bytes decoded
with `ncore2gp`]`

> **CORRECTION — the v4 reset model does NOT extend to v5.** A reader carrying the
> v4 `+0x1c` reset model ([MARIANA × PE §3](./mariana-pe.md), stable through
> MARIANA_PLUS PE) would predict `j 0x1f8` on MAVERICK. That is **wrong**: MAVERICK
> introduces a *new* `−0x20` geometry (`j 0x1d8`, enter_run @`0x94`). The v4 model
> was a forward-relocation of CAYMAN's entry; the v5 model is a *backward*
> relocation off MARIANA's — a different scheme, the same one DVE carries. This is
> the primary structural signature that MAVERICK is a real gen-step. `[HIGH/OBSERVED]`

DISASSEMBLY PROOF (shipped `ncore2gp`): the MAVERICK PE PERF IRAM decodes a
full Q7/NX windowed-ABI code body — **106 `entry` / 151 `retw` / 339 `call8` / 464
`call0` / 1085 `const16` / 1057 `l32r`** (FLIX bundle slots counted) —
a genuine separately-compiled `cayman/seq` sequencer, **smaller** than
MARIANA_PLUS's PE (consistent with `PERF_IRAM 0xbd60` vs `0x172c0`). Not a stub.
`[HIGH/OBSERVED]`

---

## 4. The PROF CAM — re-authored, `−3` armed, matmul/MX RETAINED-armed

The clean opcode-arming evidence. The PROF CAM is a `0x400` table of 16-byte
records `{opcode(u32 LE)@0, mask(u32)@4, enable(u32)@8, rsvd@12}`. Decoded
byte-for-byte; the `op=0/mask=0/en=1` record is the sentinel
("armed" excludes it):

| PROF_CAM | sha256(8) | enabled records | armed (non-sentinel) |
|---|---|---|---|
| MARIANA_PLUS PE | `43475cec` | 22 | 21 (== MARIANA verbatim) |
| **MAVERICK PE** | **`85d857a7`** | **19** | **18 (RE-AUTHORED)** |

The first divergence between the two CAMs is at byte `208` (record 13). The
armed-opcode delta (names from the shipped `neuron_maverick_arch_isa` OPCODE enum):

```text
MAVERICK PE armed (18 + sentinel 0x0), all mask 0xff:
  0x01 LDWEIGHTS      <- core matmul: load stationary weight tile   (RETAINED)
  0x02 MATMUL         <- core matmul: stream moving + MAC → PSUM     (RETAINED)
  0x09 LDWEIGHTS_MX   <- MX matmul: microscaled weight load          (RETAINED)
  0x0A MATMUL_MX      <- MX matmul: microscaled matmul               (RETAINED)
  0x9f ENGINE_NOP, 0xa0 EVENT_SEMAPHORE, 0xa1 HALT, 0xa2 DRAIN,
  0xa3 INSTRUCTION_FLUSH, 0xa5 WRITE, 0xa7 MOVE, 0xa8 ALU_OP,
  0xa9 COMPARE_BRANCH, 0xaa TENSOR_LOAD, 0xab TENSOR_STORE,
  0xb1 SET_ORDERING_MODE, 0xb2 MOVE_SHAPE, 0xe4 CONV_LUT_LOAD

ADDED vs MARIANA_PLUS PE: (none)
REMOVED vs MARIANA_PLUS PE (−3):
  0x03 PE_REG_WRITE     <- de-armed from PROF (handler still `// Y` in enum)
  0x06 LDTAGS           <- de-armed from PROF (handler still `// Y` in enum)
  0x07 MATMUL_SPARSE    <- de-armed from PROF (handler still `// Y` in enum)
```

**The decisive matmul fact: `0x01`/`0x02` AND the MX pair `0x09`/`0x0A` are ALL
armed on the MAVERICK PE CAM** — the core systolic matmul and the microscaled MX
matmul both stay perf-profiled on v5. The `−3` de-arming removes `PE_REG_WRITE`,
`LDTAGS`, and `MATMUL_SPARSE` from hardware profiling (they remain valid handlers,
marked `// Y` in the enum — see §5); `PE_MANAGE_SEED (0x08)` is not PROF-armed on
*either* gen (it is a seed-management op, not a profiled matmul) — consistent.
`[HIGH/OBSERVED arming; the "de-arm not remove" reading INFERRED-HIGH from the enum
`// Y` markers]`

The PROF TABLE is also re-authored: `e94d413a` (first diff vs MARIANA_PLUS at byte
`1543`) vs MARIANA_PLUS's `d93b723e`. **Both PROF halves distinct** — the v5
per-engine PROF re-authoring, the OPPOSITE of MARIANA_PLUS's byte-identical
per-engine reuse ([MARIANA_PLUS × PE §7](./mariana-plus-pe.md)). `[HIGH/OBSERVED]`

> **GOTCHA — the dispatch is FLIX-desynced with NO DEBUG image to fall back on.**
> The MAVERICK PE PERF IRAM dispatch is an `addx4 a2,a2,a7 ; l32i.n a2,a2,0 ; jx a2`
> jump-through-DRAM-table (table base const16 `0x1c8` → PERF DRAM `0x1c8`, ~116
> in-range IRAM-target entries), the same indexed model as DVE. The per-opcode →
> handler-body trampolines are bundle-interleaved by the linear sweep (the
> documented FLIX-desync). On MARIANA_PLUS PE the per-row binding was read from the
> DEBUG image — which **MAVERICK lacks**. So the dispatch *structure* (addx4-indexed
> table, globstruct magic, boot dispatch @`0x1f7`) is OBSERVED; the per-opcode row
> binding is the only un-byte-resolved item. `[HIGH dispatch model; LOW per-row
> binding]`

---

## 5. The handler roster — RETAINED (string-amputated; done at ISA/PROF/datapath level)

> **WALL — the handler roster is INFERRED, not carved.** MAVERICK PE has **0 `S:`
> strings in any of its 6 images** (the named SEQ handler strings — `PeManageSeed`,
> `S: Matmul`, `S: Ldweights`, `MatmulMX`, `LdweightsMX`, `MatmulSparse`,
> `ConvLutLoad` — are **0** across all carves; they live below the
> MAVERICK region, in earlier-generation DEBUG DRAMs). MAVERICK ships no PE DEBUG
> image, so the roster **cannot be diffed by string**. It is established at the
> **OPCODE-enum + PROF-arming + datapath** level. The roster membership is HIGH (the
> enum `// Y` markers + PROF arming are direct); the per-handler-**body** presence is
> INFERRED-HIGH (no DEBUG string anchors a body, but the opcode `// Y` + PROF arming
> + present MAC datapath are direct evidence). `[roster HIGH/OBSERVED; bodies
> INFERRED-HIGH]`

The 9 PE-specific opcodes are all marked `// Y` (maintained / not-deprecated) in
the shipped `neuron_maverick_arch_isa/tpb/aws_neuron_isa_tpb_common.h` OPCODE enum,
byte-for-name identical to the mariana enum (read field-by-field):

| handler | opcode | enum line / marker | origin | v5 status |
|---|---|---|---|---|
| `LDWEIGHTS` | `0x01` | `:158  // Y` | CAYMAN core | RETAINED, PROF-armed |
| `MATMUL` | `0x02` | `:159  // Y` | CAYMAN core | RETAINED, PROF-armed |
| `PE_REG_WRITE` | `0x03` | `:160  // Y` | CAYMAN core | RETAINED, **PROF de-armed** |
| `LDTAGS` | `0x06` | `:163  // Y` | CAYMAN core | RETAINED, **PROF de-armed** |
| `MATMUL_SPARSE` | `0x07` | `:164  // Y` | CAYMAN core | RETAINED, **PROF de-armed** |
| `PE_MANAGE_SEED` | `0x08` | `:165  // Y` | MARIANA | RETAINED (not PROF-armed either gen) |
| `LDWEIGHTS_MX` | `0x09` | `:166  // Y` | MARIANA | RETAINED + folded (§6) |
| `MATMUL_MX` | `0x0A` | `:167  // Y` | MARIANA | RETAINED + folded (§6) |
| `CONV_LUT_LOAD` | `0xe4` | `:310  // Y` | MARIANA | RETAINED, PROF-armed |

So the **5 CAYMAN matmul core + 4 MARIANA additions** roster that
[MARIANA_PLUS × PE §5](./mariana-plus-pe.md) found is **RETAINED on MAVERICK** — NET
`+0/−0` at the matmul-handler-roster level. The v5 enum-space growth (`159 → 165`,
+6 names — `ACTIVATE_MULTIPASS`/`COMPACT_CONTROL_INST 0xb6`/`DMA_IMMEDIATE 0xba`/
`DMA_MEMCPY2 0xb9`/`TENSOR_SCALAR_INT_WIDE`/`TENSOR_TENSOR_INT_WIDE`) is in the
control/DMA/vector cluster, **not** the PE matmul block — no new PE-matmul opcode, no
INT-wide matmul variant on the PE engine. `[HIGH/OBSERVED — both enums read]`

The **matmul datapath** (the widening-MAC inner product) is **present** in the
MAVERICK PE PERF IRAM (FLIX-desynced linear sweep; the `ivp_mul*` mnemonics live
almost entirely inside FLIX VLIW bundles, so counts are sound only when
bundle-internal slots are scanned, and are indicative not exhaustive):

| mnemonic | MAVERICK | MARIANA_PLUS PE |
|---|---|---|
| `ivp_mul4t2n8xr8` | 56 | 98 |
| `ivp_mul4ta2n8xr8` | 9 | 29 |
| `ivp_mul4tan16xr16` | 5 | 8 |
| `ivp_mul4tan16xr8` | 16 | 22 |
| `ivp_mulpan16xr16` | 8 | 14 |
| `ivp_mulpn16xr16` | 8 | 4 |
| `ivp_mulus*` (mixed) | 44 | 55 |
| `ivp_mulsu*` (mixed) | 3 | 19 |
| **`ivp_mul*` total** | **205** | 388 |

The 4-term MAC (`mul4t*`/`mul4ta*`), the 2×-FMAC pair MAC (`mulpa*`/`mulp*`), and
the signed/unsigned-mixed MACs (`mulus`/`mulsu`) are all present (33 distinct
`ivp_mul*` variants) — the `Matmul` / `MatmulMX` handler bodies are genuinely
implemented. Lower counts = the smaller independent v5 build. `[HIGH/OBSERVED
presence; counts MED under FLIX-desync]`

---

## 6. The MX-dtype expansion — `MatmulMX` folded into `Matmul` (the v5 MX reorganization)

The central v5 question, answered from the shipped struct headers + the dtype enum +
the PROF — the PE **does** carry the v5 MX/FP8/INT4/SFP8 matmul, realized as a
**struct/access-pattern reorganization** (MX folded into the regular matmul), **not**
as new dispatch handlers.

**(A) The MX fold.** The shipped maverick per-instruction struct headers state it
verbatim:

```c
// aws_neuron_isa_tpb_s3d3_mm.h:18-20  (MATMUL operand struct):
//   "On Neuron, when the source uses MXTensorV2 access pattern, the instruction
//    operates in MX mode with restricted flags and MX-specific dtype/validation
//    rules. This replaces the separate MatmulMX instruction from previous chips."
//
// aws_neuron_isa_tpb_s3_lw.h:18-19  (LDWEIGHTS operand struct):
//   "...This replaces the separate LdWeightMX instruction from previous chips."
//
// aws_neuron_isa_tpb_smx1d3_mm.h:15  (the LEGACY MatmulMX struct):
//   "DEPRECATED on Neuron: Use the regular Matmul instruction with MXTensorV2
//    access patterns instead."
```

So on v5 the MX matmul is **unified** into the regular `Matmul`/`Ldweights` via the
`MXTensorV2` access pattern; the legacy `SMX1D3_MM`/`SMX1_LW` structs + opcodes
`0x09`/`0x0A` are **RETAINED** (still `// Y`, still PROF-armed §4) for back-compat,
but **deprecated**. `[HIGH/OBSERVED — shipped headers]`

**(B) The new MX struct fields.** The maverick `S3D3_MM` (MATMUL) struct now carries
(cross-ref the committed
[pe-matmul.md](../firmware/kernels/pe-matmul.md) `S3_LW_STRUCT` layout):

```c
// MAVERICK S3D3_MM (MATMUL) — the NEW MX fields (replacing mariana's
// transpose_mode/perf_opt/reserved_quant/row_grp/col_grp/psum_zero_region):
    NEURON_ISA_TPB_DTYPE          in_dtype;     // @32
    NEURON_ISA_TPB_PE_FP32MODE    fp32_mode;    // @33
    NEURON_ISA_TPB_DTYPE          out_dtype;    // @34
    NEURON_ISA_TPB_TILE_SIZE      tile_size;    // @37   NEW: row r64/r128, col c128/c256
    NEURON_ISA_TPB_TILE_SEL       tile_sel;     // @38   NEW
    NEURON_ISA_TPB_MATMUL_FLAGS   flags;        // @39   (carries seed_mode:2)
    NEURON_ISA_TPB_MX_PERF_MODE   perf_mode;    // @40   NEW: the microscaling pump
// S3_LW gains tile_size/tile_sel/perf_mode likewise.
```

with validation `valid_pe_tile_select` / `valid_pe_row_tile_size` /
`valid_pe_col_tile_size`, and the `MX_PERF_MODE` enum (read field-by-field):

```c
// aws_neuron_isa_tpb_common.h:1396-1404
typedef enum NEURON_ISA_TPB_MX_PERF_MODE {
  NONE = 0x0, QUAD_ROW = 0x1, QUAD_ROW_INTERLEAVE = 0x2, QUAD_ROW_TILED = 0x3,
  OCT_ROW = 0x4, OCT_ROW_INTERLEAVE = 0x5, OCT_ROW_TILED = 0x6,  // microscaling matmul perf modes
} NEURON_ISA_TPB_MX_PERF_MODE;
```

`[HIGH/OBSERVED — struct + enum read field-by-field]`

**(C) The new MX dtypes.** The maverick `NEURON_ISA_TPB_DTYPE` enum adds, over
mariana's `FP4_EXP2 0x10` (the mariana enum carries *only* `0x10` in this range;
the other six are MAVERICK-NEW):

```c
// aws_neuron_isa_tpb_common.h:866,874-879
  0x10 FP4_EXP2   // OCP FP4_E2M1          (mariana+)
  0x11 FP8_EXP2   // FP8_E2M5              <- NEW maverick
  0x12 INT4                                <- NEW maverick
  0x13 SFP8_E8    // scale-only, FP8_S0E8M0 <- NEW maverick
  0x14 SFP8_E7    // FP8_S0E7M1            <- NEW maverick
  0x15 SFP8_E6    // FP8_S0E6M2            <- NEW maverick
  0x16 SFP8_E5    // FP8_S0E5M3            <- NEW maverick
```

These ride the matmul `in_dtype`/`out_dtype` fields (`S3D3_MM.in_dtype/out_dtype`,
`S3_LW.in_dtype`), entering the matmul input space via the unified MX-mode `Matmul`.
The scale-only `SFP8_E8` (E8M0) is the MX shared-exponent format the POOL dequant
kernel consumes ([mx-dequant.md](../firmware/kernels/mx-dequant.md) §"the standalone
`proc_4bit_mx_8`"). `[HIGH/OBSERVED — enums read]`

**(D) The NX-sequencer dtype surface stays NUMERIC.** Across all 6 MAVERICK PE
carves: `FP8`/`INT4`/`SFP8`/`MXTENSOR`/`TILE_SIZE`/`FP4`/`CPTC`/`QuantizeMx`/
`proc_4bit` = **0** named-string hits (these are enum tokens in the C
headers, never data strings in the image). The only dtype strings are the
`move.cpp:41` assertion's `NEURON_ISA_TPB_DTYPE_{UINT32,INT32,FP32}` (byte-identical
to MARIANA/MARIANA_PLUS PE; source path
`/opt/workspace/NeuronUcode/src/decode/move.cpp:41`). The v5 MX dtype superset is
realized in the **struct/opcode decode path** (the `TILE_SIZE`/`MX_PERF_MODE` fields
+ the `FP8`/`INT4`/`SFP8` ordinals) + the Q7 POOL dequant kernel — **not** as
NX-sequencer dtype strings. The PE adds the MX *matmul* (the compute), not MX dtype
*strings* — exactly the DVE/MARIANA_PLUS-PE pattern. `[HIGH/OBSERVED-negative]`

```c
// The v5 MX matmul, in one line (INFERRED-HIGH from the headers + the present
// MAC datapath — see pe-matmul.md / mx-dequant.md):
//
//   if (mxtensorv2_pattern(matmul.src_mem_pattern)) {          // MXTensorV2 access pattern
//       // MX mode: restricted flags, MX dtype rules — REPLACES the old MatmulMX op
//       widening_mac(in_dtype ∈ {FP8_EXP2, INT4, SFP8_E8..E5, FP4_EXP2},
//                    tile_size, tile_sel, perf_mode);          // QUAD_ROW / OCT_ROW pump
//   } else {
//       widening_mac(/* normal bf16/fp16/tf32/fp32 matmul */); // the present widening-MAC datapath
//   }
```

> **NOTE — `PeManageSeed` (0x08) is RETAINED at the ISA level.** The enum marks
> `PE_MANAGE_SEED 0x08 // Y`; the mechanism is intact: `NEURON_ISA_TPB_PE_SEED_MODE
> {NONE=0, LOAD_SEED=1, SAVE_SEED=2}` (lines 1288-1292) with a `seed_mode : 2`
> bitfield in **both** `MATMUL_FLAGS` (line 1382) and `LD_WEIGHT_FLAGS` (line 1391)
> — the LOAD/SAVE seed-mode gating on the matmul datapath (issue a `LdWeight`
> micro-op to push the seed via the weight port + a `Matmul` micro-op with `is_load`
> toggling LOAD vs SAVE). The matmul datapath the seed rides (§5) is present.
> **IMAGE-LEVEL caveat:** the `S: PeManageSeed(SAVE/LOAD)` handler *string* cannot be
> confirmed on the MAVERICK PE image — there is no PE DEBUG image (the string lived
> only in MARIANA_PLUS PE DEBUG DRAM); region-wide MAVERICK count = 0. So
> PeManageSeed-RETAINED is **HIGH/OBSERVED** at the enum + `seed_mode`-flag +
> datapath level; the handler-body presence is **INFERRED-HIGH**; the handler-STRING
> presence is **N/A** (no DEBUG image). The PE_SEED stochastic-rounding RNG semantics
> are CARRIED from [pe-matmul.md](../firmware/kernels/pe-matmul.md), not re-derived.
> `[enum+flags HIGH/OBSERVED; body INFERRED-HIGH; semantics CARRIED]`

---

## 7. The DGE fast-path DROPPED + the size shrink (the v5-vs-v4+ reversal)

The MARIANA_PLUS PE page proved the *one* v4→v4+ functional change was a **NEW** DGE
reshape fast-path (`dge_decode_fast.cpp` + 3 helpers). On MAVERICK that fast-path is
**dropped** — 0 hits across the 6 PE carves:

| DGE string | MAVERICK PE (6 carves) | MARIANA_PLUS PE (DEBUG DRAM) |
|---|---|---|
| `dge_decode_fast` | **0** | present |
| `dge_reshape_memcopy_transpose_fast` | **0** | present |
| `dge_backend_rtl` | **0** | present |
| `wait_for_credit` | **0** | present |

The v4+ SEQ-side DGE fast-path that MARIANA_PLUS PE *added* is **GONE** from the
MAVERICK PE images — re-architected to the HW DMA path (the same drop the
[MAVERICK × DVE](./maverick-dve.md) page found). `[HIGH/OBSERVED counts;
"re-architected to HW DMA" INFERRED-HIGH]`

> **GOTCHA — "dropped" means absent from the PE region, not the whole binary.** The
> 4 `dge_decode_fast`-family strings still exist *elsewhere* in `internal.so` (e.g.
> `dge_decode_fast` first occurs near file `0x640bbb`, in an earlier-generation
> region) — they are simply **0 in the MAVERICK PE byte ranges** (the 6 carves) and
> 0 in the MAVERICK region `[0x871300:EOF]`. Phrase the finding as "absent from the
> MAVERICK PE image," not "absent from the binary." The drop is real for the v5 PE
> firmware; the strings survive as residue of the older builds. `[HIGH/OBSERVED]`

**Cross-gen size — MAVERICK PE is dramatically smaller** (the DEBUG-drop +
DGE-drop + independent-build shrink, the INVERSE of the MARIANA_PLUS PE *growth*):

| IMAGE | MAVERICK | MARIANA_PLUS | Δ (MAV−MPLUS) |
|---|---|---|---|
| PERF_IRAM | `0xbd60` | `0x172c0` | `−0xb560` |
| PERF_DRAM | `0x2040` | `0x2ec0` | `−0xe80` |
| TEST_IRAM | `0xc320` | `0x16de0` | `−0xaac0` |
| TEST_DRAM | `0x22c0` | `0x32a0` | `−0xfe0` |
| DEBUG_IRAM | *(absent)* | `0x19e00` | **DROPPED** |
| DEBUG_DRAM | *(absent)* | `0x6560` | **DROPPED** |
| PROF_CAM | `0x400` | `0x400` | `+0x0` (re-authored) |
| PROF_TABLE | `0x2000` | `0x2000` | `+0x0` (re-authored) |
| **total** | **`0x1e780` (6 imgs)** | **`0x56960` (8 imgs)** | **`−0x381e0` (~64.8%)** |

**Build independence.** MAVERICK PE PERF IRAM diverges from MARIANA_PLUS PE PERF
IRAM at **byte 1** (the reset immediate `75` vs `7d`) with **7.6% positional 16-byte
block similarity (231/3030)** — a fully independent v5 build, **NOT** a relocated
recompile. Contrast [MARIANA_PLUS × PE §3](./mariana-plus-pe.md): MARIANA_PLUS-vs-
MARIANA PE shared a `0x212`-byte prefix + a byte-identical opcode compare-chain (a
recompile-relocation). `[HIGH/OBSERVED]`

No `mariana-4062` errata (DVE-only, never on PE); no `maverick-NNNN` errata in any
PE image. The source tree stays `cayman/seq/src/…` (the `move.cpp:41` path). The ISA
enum fixes `PE = 0`; the `.globstruct` magic `0x6099cb34` and the addx4-indexed
dispatch confirm the same SEQ model. `[HIGH/OBSERVED]`

---

## 8. Adversarial self-verification

The five strongest claims, challenged against the binary:

1. **The NEW v5 reset (`j 0x1d8`, `−0x20`, enter_run @`0x94`).** *Challenge:* maybe
   it is the v4 `+0x1c` shift mis-read, or a single-variant fluke. *Evidence:* the
   head byte-1 is `0x75` (MAVERICK) vs `0x7d` (MARIANA_PLUS) — `ncore2gp` decodes
   `j 0x1d8` / `j 0x1e4`, the trampoline at `0x1d8` is `const16 a0,148 ; jx a0` →
   `0x94` (148 = 0x94), and `enter_run @0x94` lands on a real C prologue; PERF and
   TEST IRAM head identically; the boot dispatch sits at `0x1f7`
   (`extui;addx4;l32i;jx`), a uniform `−0x20` off MARIANA's `0x217`. The shift is
   `−0x20`/`−0x20`/`+4`, the same geometry as DVE — NOT `+0x1c`. **HOLDS.**
2. **NO PE DEBUG image / handler strings amputated.** *Challenge:* maybe a DEBUG
   getter exists under a different name, or the strings live in PERF/TEST.
   *Evidence:* `nm | rg -c 'MAVERICK_NX_PE_.*_get$'` = 10; `rg -c
   'MAVERICK_NX_PE_DEBUG'` = 0; `strings` over all 6 carves: `PeManageSeed`/`S:
   Matmul`/`S: Ldweights`/`MatmulMX`/`LdweightsMX`/`MatmulSparse`/`ConvLutLoad` all
   **0**, no genuine `S:`-mnemonic strings in any PE byte range (the one incidental
   `53 3A` pair in TEST_IRAM has no trailing text); the catalog
   `MAVERICK_NX_PE_DEBUG_*` row = `0`. **HOLDS** (and the roster is correctly flagged
   INFERRED).
3. **The DGE fast-path DROPPED.** *Challenge:* the 0-count could be a no-DEBUG
   artifact (the strings only ever lived in DEBUG), or the strings could be gone
   binary-wide. *Evidence:* all 4 `dge_decode_fast`-family strings are **0** across
   the 6 carves AND 0 in the MAVERICK region `[0x871300:EOF]` — but they still exist
   in earlier-generation regions of the same `.so` (so the drop is image-scoped, not
   a measurement artifact; see §7 GOTCHA). On MARIANA_PLUS PE they were present in
   DEBUG DRAM. They are *source/helper* names, not handler strings — their absence
   from the PE image is the dropped code, corroborated by the matched DVE drop.
   **HOLDS** (phrasing scoped to the PE image).
4. **Matmul RETAINED + MX REORGANIZED.** *Challenge:* maybe the MX opcodes are gone
   and "folded" is wishful. *Evidence:* the PROF CAM arms `0x01/0x02` AND `0x09/0x0A`
   (decoded byte-for-byte, 18 armed, mask 0xff); the enum marks `LDWEIGHTS_MX 0x09 //
   Y` / `MATMUL_MX 0x0A // Y` (RETAINED); the `S3D3_MM`/`S3_LW` headers say MXTensorV2
   "replaces the separate MatmulMX/LdWeightMX"; the `SMX1D3_MM` header is stamped
   "DEPRECATED on Neuron: Use the regular Matmul…"; the structs gain
   `TILE_SIZE`/`TILE_SEL`/`MX_PERF_MODE`; the dtype enum adds `FP8_EXP2 0x11`/`INT4
   0x12`/`SFP8_E8..E5 0x13..16` (mariana has only `0x10`); 205 `ivp_mul*` confirm the
   MAC datapath. RETAINED-and-folded, not removed. **HOLDS.**
5. **Independent v5 build (not a recompile).** *Challenge:* same SEQ chassis could
   mean a recompile with relocated literals. *Evidence:* first byte divergence at
   index **1**; positional 16-byte block similarity **231/3030 = 7.6%**; total
   footprint `0x1e780` vs `0x56960` (`−0x381e0`); 0 `.a` members. A recompile would
   share a long prefix (as MARIANA_PLUS-vs-MARIANA did at `0x212`) — this shares only
   the 16-byte NX boot-stub body and diverges immediately. **HOLDS.**

**Honesty ledger.** *HIGH/OBSERVED:* container sha256
`b7c67e89…` (MATCH); 10 PE getters (0 DEBUG); 6 carves sha-reproduced; 0 `.a`
members; contiguity DVE→PE→POOL; reset `j 0x1d8`/`j 0x1e4`/enter_run @`0x94`
(`ncore2gp`, `−0x20`/`+4`); windowed-ABI census 106/151/339/464/1085/1057; 205
`ivp_mul*`; PROF_CAM `85d857a7` 18 armed (`0x01/0x02` + MX `0x09/0x0A` armed, `0x03/
0x06/0x07` de-armed), first diff @byte 208; PROF_TABLE `e94d413a` first diff @byte
1543; OPCODE enum 9 PE handlers `// Y`; `S3D3_MM`/`S3_LW`/`SMX1D3_MM` headers
verbatim; dtype `0x11/0x12/0x13-0x16` MAVERICK-new (mariana has only `0x10`);
`MX_PERF_MODE` + `PE_SEED_MODE` + `seed_mode:2` (lines 1382/1391); 0 DGE-fast / 0 MX
/ 0 `S:` strings in the PE carves; `move.cpp:41` `{UINT32,INT32,FP32}`; size
`0x1e780` vs `0x56960` (~64.8%); block-sim 231/3030 (byte-1).
*MED/INFERRED:* `arch_id 36` (coretype = arch_id+1 across the 4 lower gens; no NCFW
v5 image — never binary-observed); the per-handler-BODY presence (no DEBUG string to
anchor; the opcode `// Y` + PROF arming + present MAC datapath are direct); "DGE
re-architected to HW DMA"; the `ivp_mul*` census counts (FLIX-desync); coretype 37
(upstream).
*CARRIED:* `PeManageSeed` SR-RNG semantics + `PE_SEED_MODE` micro-op gating (from
[pe-matmul.md](../firmware/kernels/pe-matmul.md)); the `MX↔FP4/MX`/E8M0 POOL-dequant
linkage (from [mx-dequant.md](../firmware/kernels/mx-dequant.md)).
*LOW/NOT CLAIMED:* the per-opcode → handler trampoline ROW binding on the v5 PE PERF
dispatch (FLIX-desync, no DEBUG fallback); the exact `TILE_SIZE`/`TILE_SEL`/
`MX_PERF_MODE` field semantics beyond the enum names; whether the runtime ever loads
MAVERICK.

---

## 9. Cross-references

- [MARIANA_PLUS × PE image](./mariana-plus-pe.md) — **the v4+ diff base** (the
  9-handler roster, reset `j 0x1f8`/enter_run @`0x90`, PROF `43475cec`/`d93b723e`
  22-enabled, the DGE fast-path this page finds DROPPED, the `0x212` recompile-prefix
  this page contrasts).
- [MARIANA × PE image](./mariana-pe.md) — the v4 baseline where `PeManageSeed`/MX/
  `ConvLut` first ship and the `+0x1c` reset model lives (the model this page shows
  does NOT extend to v5).
- [MAVERICK × DVE](./maverick-dve.md) / [MAVERICK × ACT](./maverick-act.md) — the v5
  sibling images: the identical `j 0x1d8` reset, the ACT→DVE fold (the structural
  gen-step), the per-engine PROF re-authoring, the DGE-drop, internal-twin
  exclusivity.
- [PE Matrix-Multiply Path](../firmware/kernels/pe-matmul.md) — the byte-exact
  per-gen operand structs, the MAVERICK `S3_LW_STRUCT`/`S3D3_MM` layout with
  `TILE_SIZE`/`MX_PERF_MODE`, the `MatmulMX → unified Matmul` fold, and the
  `PeManageSeed`/`PE_SEED_MODE` semantics.
- [MX (Microscaling) Dequant Compute Paths](../firmware/kernels/mx-dequant.md) — the
  POOL `proc_4bit_mx_8` E8M0 (`SFP8_E8`) block-scale dequant the v5 MX matmul feeds.
- [MAVERICK generation profile](../generations/maverick-profile.md) — the v5 model
  (coretype 37 / `arch_id 36*` INFERRED / NC-v5 / the MX dtype uplift).
- [Image Catalog Index](./image-catalog-index.md) — the full getter map (MAVERICK
  NX_PE rows 747–756, the `MAVERICK_NX_PE_DEBUG_*` = 0 row).
- [Confidence & Walls Model](../reference/confidence-model.md) — the tag taxonomy.
