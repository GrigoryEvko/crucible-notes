# CAYMAN × DVE image

This page decodes the **CAYMAN × DVE** firmware image — the baseline **Data/Vector
Engine** program for the NC-v3 (CAYMAN) NeuronCore, across all of its shipped variants
and regions (**DEBUG / PERF / TEST** × **IRAM / DRAM** plus **PROF CAM / TABLE**). It is
the reference image the [MARIANA](mariana-dve.md), MARIANA+, and [MAVERICK](maverick-dve.md)
DVE diffs are written against, so every byte-pinned anchor here (carve offsets, shas, the
dispatch loop, the 53-name handler roster, the DEBUG-vs-PERF string delta) is the baseline
those forward pages reference.

The headline verdict, proved instruction-exact below: **DVE is the same `cayman/seq/`
NX-class SEQ-style ASCII-opcode dispatch engine as ACT / POOL / PE / SP** — same flat
IRAM/DRAM packaging, same `j 0x1dc` reset vector, same DRAM jump table at `0x80814` — but
**compiled with the data/vector handler subset**: the largest of the five engines (53
distinct handlers, 28 of them DVE-only) and the only one whose opcode space is bound to
**170 entries** (`movi a3,169`) rather than the SEQ/ACT 178. DVE is the **control/sequencer
front-end** that decodes the `S:` instruction stream and routes to the data/vector handlers
(batch-norm, predicated copy/cast, match/find/select, scan/transpose/shuffle, dropout),
which run vector math on the NX core's IVP datapath.

Confidence and evidence tags follow the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. Every device fact is byte-pinned to a carve re-derived this
session from `libnrtucode_internal.so` and disassembled with the native `ncore2gp`
`xtensa-elf-objdump`. The findings below were all reproduced against the binary in-task.

> **NOTE — provenance.** Every fact derives **solely** from static analysis of the shipped
> binaries with stock binutils (`nm`/`objdump`/`readelf`/`ar`/`objcopy`/`dd`/`xxd`/
> `strings`) and the Cadence Xtensa toolchain (`xtensa-elf-objdump`,
> `XTENSA_CORE=ncore2gp`, GNU Binutils 2.34.20200201 / Xtensa Tools 14.09) that ships
> inside the gpsimd-tools package. All naming derives from the DEBUG build's own embedded
> `S:` format strings and the firmware's baked-in source-path assertion strings. Lawful
> interoperability reverse engineering (DMCA 17 U.S.C. 1201(f)).

---

## 1. The image container and the 14 DVE getters

**Container.** The firmware images live as `.rodata` blobs inside the host customop
shared object:

```
…/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/
    custom_op/c10/lib/libnrtucode_internal.so
sha256 b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b   (10,276,288 B)
```

ELF64 x86-64 DYN, **not stripped**. The first `R` `LOAD` segment is **identity-mapped**
(`readelf -lW`: `LOAD off 0x000000 vaddr 0x0 filesz 0x9af194`), so every `<NAME>.data`
`.rodata` VA is **simultaneously the file offset** of its blob — the carve rule is a plain
`dd`. All 14 DVE blob VAs (`0x6f600`..`0x3050a0`) fall inside this `R` `LOAD`.
`[HIGH/OBSERVED — `readelf -lW` confirmed identity map; LOAD R filesz `0x9af194`]`

**The getters.** Each image is exposed by a 4-instruction `(ptr, size)` accessor stub in
`.text`:

```asm
lea  <blob>(%rip),%rax ;  mov %rax,(%rdi) ;  movq $<size>,(%rsi) ;  ret
```

`nm` resolves all 14 `CAYMAN_NX_DVE_*_get` accessors and their `.data` blob symbols
(`CLS=NX, ENG=DVE`). Of the 14, **8 carry real bytes** and **6 are zero-size boundary
cursors**:

| VARIANT | REGION | `*_get.data` VA (== file off) | SIZE | STATUS |
|---|---|---:|---:|---|
| DEBUG | IRAM | `0x16f660` | `0x1bcc0` | REAL (code) |
| DEBUG | DRAM | `0x18b320` | `0x06d60` | REAL (data + `S:` logs) |
| DEBUG | SRAM | `0x192080` | `0x0` | EMPTY (boundary cursor) |
| DEBUG | EXTRAM | `0x192080` | `0x0` | EMPTY (boundary cursor) |
| PERF | IRAM | `0x06f600` | `0x15c20` | REAL (code) |
| PERF | DRAM | `0x085220` | `0x02fc0` | REAL (data, no `S:`) |
| PERF | SRAM | `0x0881e0` | `0x0` | EMPTY (boundary cursor) |
| PERF | EXTRAM | `0x0881e0` | `0x0` | EMPTY (boundary cursor) |
| TEST | IRAM | `0x0ebde0` | `0x15840` | REAL (code) |
| TEST | DRAM | `0x101620` | `0x032c0` | REAL (data, no `S:`) |
| TEST | SRAM | `0x1048e0` | `0x0` | EMPTY (boundary cursor) |
| TEST | EXTRAM | `0x1048e0` | `0x0` | EMPTY (boundary cursor) |
| PROF | CAM | `0x304ca0` | `0x00400` | REAL (HW-decode CAM) |
| PROF | TABLE | `0x3050a0` | `0x02000` | REAL (profile table) |

`[HIGH/OBSERVED — all 14 `nm`-resolved; VAs/sizes re-read this pass; the getter-body
`movq` sizes agree 14/14]`

> **NOTE — the 6 SRAM/EXTRAM getters are boundary cursors, not images.** All six return
> `(ptr, 0)`: their `movq` is `$0x0`, and their `.data` symbol **aliases the start of the
> next engine's IRAM blob** (the contiguous-layout cursor — `nm` resolves the PERF cursor
> `0x881e0`, DEBUG `0x192080`, TEST `0x1048e0` onto the CAYMAN **NX_PE** IRAM symbol).
> **DVE runs entirely out of IRAM (code) + DRAM (data); SRAM/EXTRAM are unused on CAYMAN.**
> A reimplementer must not carve at these cursors — they belong to PE. `[HIGH/OBSERVED]`

> **GOTCHA — CAYMAN has no RELEASE flavor; the release axis is DEBUG-vs-PERF.** Unlike the
> SUNDA generation (which ships a `*_RELEASE_*` DVE variant), CAYMAN's three DVE flavors
> are **DEBUG / PERF / TEST**. **PERF is the production/release flavor** (the default when
> no `NEURON_UCODE_FLAVOR` override is set). Throughout this page "DEBUG-vs-RELEASE" means
> **DEBUG-vs-PERF**. `[HIGH/OBSERVED — the getter set has no `RELEASE` member; PERF-as-default
> CARRIED from the image catalog]`

---

## 2. Carve + 3-source byte-identity reconciliation

Carving the 8 real blobs with the identity-map rule (`dd if=$SO bs=1 skip=<VA> count=<SIZE>`)
reproduces these shas this session — **8/8 exact**:

| IMAGE | file off | size | sha256 (first 16) |
|---|---:|---:|---|
| `DVE_DEBUG_IRAM` | `0x16f660` | `0x1bcc0` | `259769ff1b47b3b3` |
| `DVE_DEBUG_DRAM` | `0x18b320` | `0x06d60` | `c106642d38386cb7` |
| `DVE_PERF_IRAM` | `0x06f600` | `0x15c20` | `9fa066f40f3cafc5` |
| `DVE_PERF_DRAM` | `0x085220` | `0x02fc0` | `eb980f98138d6010` |
| `DVE_TEST_IRAM` | `0x0ebde0` | `0x15840` | `3e83b0f4bcc8e964` |
| `DVE_TEST_DRAM` | `0x101620` | `0x032c0` | `5efcb3eb31907683` |
| `DVE_PROF_CAM` | `0x304ca0` | `0x00400` | `8fd7e422bd07881a` |
| `DVE_PROF_TABLE` | `0x3050a0` | `0x02000` | `ce761f81d075658e` |

`[HIGH/OBSERVED — all 8 shas re-computed this pass from the carve]`

**3-source reconciliation.** The shipped static archive
`…/custom_op/c10/lib/libnrtucode.a` carries exactly **14 `CAYMAN_NX_DVE` members** (12
`img_CAYMAN_NX_DVE_<MODE>_<SEG>_contents.c.o` + 2
`hwdecode_CAYMAN_NX_DVE_PROF_{CAM,TABLE}_contents.c.o`). For each real image, `objcopy -O
binary --only-section=.rodata <member.o>` followed by `sha256sum` matches the carved blob
**8/8 IDENTICAL** — the `internal.so` getter blob **is** the `libnrtucode.a` member
`.rodata`:

| `libnrtucode.a` member | image | sha (16) match |
|---|---|---|
| `img_CAYMAN_NX_DVE_DEBUG_IRAM_contents.c.o` | DEBUG_IRAM | `259769ff…` ✓ |
| `img_CAYMAN_NX_DVE_DEBUG_DRAM_contents.c.o` | DEBUG_DRAM | `c106642d…` ✓ |
| `img_CAYMAN_NX_DVE_PERF_IRAM_contents.c.o` | PERF_IRAM | `9fa066f4…` ✓ |
| `img_CAYMAN_NX_DVE_PERF_DRAM_contents.c.o` | PERF_DRAM | `eb980f98…` ✓ |
| `img_CAYMAN_NX_DVE_TEST_IRAM_contents.c.o` | TEST_IRAM | `3e83b0f4…` ✓ |
| `img_CAYMAN_NX_DVE_TEST_DRAM_contents.c.o` | TEST_DRAM | `5efcb3eb…` ✓ |
| `hwdecode_CAYMAN_NX_DVE_PROF_CAM_contents.c.o` | PROF_CAM | `8fd7e422…` ✓ |
| `hwdecode_CAYMAN_NX_DVE_PROF_TABLE_contents.c.o` | PROF_TABLE | `ce761f81…` ✓ |

The blob and its accessor are two views of the same compiled object. `[HIGH/OBSERVED — `ar
t`/`ar x` + `objcopy --only-section=.rodata` + `sha256`, 8/8 this pass; `ar t` shows clean
unmangled member names]`

> **NOTE — none of the 8 carves is an ELF.** Head bytes are `06 76 …` (IRAM), `34 cb 99
> 60 …` (DRAM), `01 00 …` (PROF_CAM), `01 02 …` (PROF_TABLE) — never `\x7fELF`. These are
> **flat device-memory segments** (the device-side payload of the `img_*`/`hwdecode_*`
> members), *not* EM_XTENSA ELFs. (Contrast the Q7 POOL EXTISA blobs of the
> [extisa inventory](extisa-inventory.md), which *are* EM_XTENSA ELFs.) For DVE, the
> "ELF confirmation" is the **flat-image geometry** confirmation: reset vector at byte 0,
> DRAM tables at `0x814`/`0xabc`, and the `ncore2gp` disassembler decoding the IRAM to
> real windowed-ABI + FLIX-VLIW code. `[HIGH/OBSERVED — head-byte check on all 8]`

---

## 3. Flat geometry, reset vector, and the engine-identity boot

**Reset vector (first 12 bytes — byte-identical across DEBUG / PERF / TEST):**

```
00000000:  06 76 00 00  00 00  86 77 00 00  00 00
   0x000:  06 76 00     j 0x1dc       ; primary reset vector -> boot path
   0x006:  86 77 00     j 0x1e8       ; secondary vector -> halt trap
```

Decoded boot path (DVE DEBUG IRAM, `ncore2gp` objdump):

```asm
1dc:  const16 a0, 0          ┐ a0 = C enter_run prologue VA (0x90)
1de:  const16 a0, 0x90       ┘
1e1:  jx      a0             ; jump to the enter_run prologue
…
1e8:  halt    0             ; 2nd vector = HALT trap
```

This `j 0x1dc` (`06 76 00 00`) is **byte-identical to the SEQ engine reset vector and to
ACT / PE / POOL / SP** — all five engines' first 12 bytes are `0676 0000 0000 8677 0000
0000`. The boot bodies *past* byte 6 are engine-specific (different literal pools /
`enter_run` targets). `[HIGH/OBSERVED — reset vector read on all 3 DVE variants + verified
identical across all 5 engines]`

> **NOTE — the same flat binary loads on any engine slot; `engine_idx` is computed at
> boot.** The DRAM carries the format string
> `S: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u`.
> The engine derives its own identity (`engine_idx = 3` for DVE) at runtime from
> `engine_base_addr` vs `tpb_base_addr`; the firmware is **not** hardcoded to a slot. The
> log string is OBSERVED; the runtime-compute is INFERRED-HIGH from the string + the boot
> path. `[string HIGH/OBSERVED; runtime compute INFERRED-HIGH]`

**Disassembly proof.** `xtensa-elf-objdump -D -b binary -m xtensa --adjust-vma=0x0` decodes
the DEBUG IRAM to **44,989 lines, exit 0, empty stderr** — real Q7/NX windowed-ABI
(`entry a1,N` ×615, `retw` ×829) plus dense Cadence Vision IVP TIE vector ops. The DVE NX
core carries a **full vector compute datapath** — the data/vector math runs on it. Distinct
IVP mnemonics per IRAM: **PERF 317 / TEST 319** (cleaner schedules); DEBUG's linear sweep
recovers fewer (167 distinct) because the FLIX/literal-pool desync loses more bundle sync.
`[HIGH/OBSERVED — disasm exit-0; entry/retw + IVP counts re-derived this pass]`

> **NOTE — the IVP vocabulary names the exact data/vector datapath primitives.** A scan of
> the PERF/TEST IRAM confirms the op families a reimplementer needs: **uint→float casts**
> (`ivp_ufloatn_2x32t`, `ivp_ufloat16nx16t` — the `uint32 → fp32 [0,1)` step Dropout's RNG
> rides on), **ordered-float compares** (`ivp_oltnxf16t`, `ivp_oltn_2xf32t`, `…oeq…`,
> `…ole…` — the predicate writers behind the predicated ops), **select / dual-select**
> (`ivp_seln_2x32t`, `ivp_dselnx16t`, `ivp_sel2nx8t` — the masked input-vs-other lane
> select), plus abs/add/avg integer-vector ops across `nx16`/`2nx8`/`n_2x32` width classes.
> The compare→select pair is exactly the predicated-op datapath. `[HIGH/OBSERVED — the IVP
> *vocabulary* in the PERF/TEST histogram; per-op operand binding is per the committed
> kernel pages]`

---

## 4. The DVE dispatch loop

DVE uses the SEQ dispatch model: per-fetch opcode-byte normalization (`opcode − 0x41`) into
a **direct-indexed DRAM jump table** at `0x80814`. Decoded instruction-exact this pass at
**two parallel sites** — one per NX mode:

```c
/* The DVE dispatch core (decoded byte-exact from DEBUG IRAM 0x2f5a..0x2f7c, HW-Decode
 * site; the Sunda-mode site at 0x3739..0x3750 is identical bar the table base).
 * Key = the ASCII opcode byte; index = byte - 'A'(0x41); table = 170 4-byte IRAM targets. */
static void dve_dispatch(uint32_t opcode_word /* [a1+8] */)
{
    dbg_log("S: Dispatch opcode=0x%x", opcode_word);  /* const16 a10,0xde0 ; call8 0x18010 */

    int idx = opcode_word - 0x41;                     /* addi a2,a2,-65   (normalize 'A'->0) */
    if ((unsigned)idx > 169)                          /* movi a3,169 ; bgeu a3,a2 -> bound 170 */
        goto bad_opcode;                              /* j 0x3207  (HW-decode ErrorHandler arm) */

    uint32_t target = dispatch_table[idx];            /* base = const16 a3,0x80814 ; addx4 ; l32i.n */
    goto *target;                                     /* jx a2  -> trampoline -> handler          */

bad_opcode:
    error_handler_bad_opcode(opcode_word);            /* "S: ErrorHandler : Bad Opcode(0x%x)" */
}
```

Byte-exact at **SITE A** (HW-Decode, `0x2f5a`): `const16 a10,8 ; const16 a10,0xde0 ; call8
0x18010` (log) ; `l32i.n a2,[a1+8]` ; `addi a2,a2,-65` ; `movi a3,169` ; `bgeu a3,a2,0x2f71`
; `j 0x3207` (default) ; `const16 a3,8 ; const16 a3,0x814` ; `addx4 a2,a2,a3` ; `l32i.n
a2,[a2]` ; `jx a2`. **SITE B** (Sunda, `0x3739`) is identical except `const16 a3,0xabc`
(the **second** table) and `j 0x39e6` (the Sunda default arm). `[HIGH/OBSERVED — both sites
disassembled instruction-exact]`

The decisive distinctions from the SEQ/ACT baseline:

| property | DVE (this image) | SEQ / POOL / ACT |
|---|---|---|
| table base | DRAM `0x80814` (+ second `0x80abc`) | DRAM `0x80814` |
| entry stride | 4 B (direct IRAM target) | 4 B |
| key | `opcode_byte − 0x41` | `opcode_byte − 0x41` |
| **bound** | **170** (`movi a3,169`) | **178** (`movi a3,177`) |
| lookup | direct-indexed `jx` (O(1)) | direct-indexed `jx` |
| miss policy | `ErrorHandler : Bad Opcode` | `ErrorHandler : Bad Opcode` |

`[HIGH/OBSERVED]`

> **QUIRK — DVE's opcode space is 170 (`movi a3,169`), not the SEQ/ACT 178.** A grep of the
> DEBUG IRAM finds **zero** `movi a3,177` and **two** `movi a3,169` (the two dispatch
> sites). DVE's valid opcode range is therefore `0x41..0xea` (170 indices), distinct from
> the 178-entry SEQ/ACT table. A reimplementer's dispatch bound check must use **169**.
> `[HIGH/OBSERVED — grep-verified: 0× `movi a3,177`, 2× `movi a3,169`]`

> **NOTE — the dual-mode dispatch is a generic SEQ feature, not DVE-specific.** The DRAM
> names both modes — `S: NX in HW Decode mode` and `S: NX in Sunda mode: HW decode
> disabled` — and both modes ship in **all five** engines. DVE carries two parallel
> 170-entry tables (`0x80814` for HW-Decode, `0x80abc` for Sunda) so the per-opcode
> trampoline differs by mode. `[HIGH/OBSERVED]`

**Error / fault path (same as SEQ, all variants).** The DRAM carries the ErrorHandler
arms `S: ErrorHandler : Bad Opcode(0x%x)`, `… Illegal Instruction(0x%x)`, `… FP
Error(%d)`, `… Int Div Zero Error`, plus `S: Assertion failure! %s(%s:%u)`. The assertion
source paths name the engine tree: `…/cayman/seq/src/handlers/exception_handler.hpp`.
`[HIGH/OBSERVED]`

---

## 5. The DRAM image + dispatch-table geometry

DRAM segment layout (device VA `0x80000`; **DRAM file offset = VA − 0x80000**):

```
DEBUG DRAM (0x6d60):
  0x000..0x814  header             (header word 0x6099cb34)
  0x814         dispatch table1    (170 × 4 B IRAM targets — HW-Decode mode)
  0xabc         dispatch table2    (170 × 4 B IRAM targets — Sunda mode)
  0xd64..        'S:' log/format-string pool  (from ~0xde0) + assertion source-paths
```

Both tables decoded this pass (170 LE words each):

| table | base | default arm | real entries | default entries |
|---|---|---|---:|---:|
| table1 (HW-Decode) | `0x80814` | `0x3207` | **75** | 95 |
| table2 (Sunda) | `0x80abc` | `0x39e6` | **75** | 95 |

The two tables are **parallel**: each populated opcode's table1 / table2 trampolines are
offset by a constant **`+0x7df`** (opcode `'A'`/idx 0 → `0x31e6` in table1, `0x39c5` in
table2; `0x39c5 − 0x31e6 = 0x7df`). `[HIGH/OBSERVED — both tables read + the +0x7df constant
re-derived this pass]`

> **NOTE — DVE carries NO resident LUT/coefficient table in the firmware image.** Unlike
> ACT (which ships an activation/coefficient LUT), DVE's DRAM holds only the header, the
> two dispatch tables, and the string pool. The data DVE consumes — batch-norm params
> (γ/β/running stats), match keys, indices — is **loaded at runtime** by the
> `BatchNormalizeParamLoad` / `MatchValueLoad` / `DveReadIndices` opcodes (host/DMA-supplied),
> not baked into DRAM. `[HIGH geometry / INFERRED-HIGH the "no resident LUT" reading from
> the handler set + the DRAM-content scan]`

> **GOTCHA — the tiny PERF DRAM overlaps its table tail with the string pool.** PERF DRAM
> is only `0x2fc0`; because PERF strips the `S:` pool, the assertion source-path strings
> start earlier and overlap the table tail (words past idx ~55 read as ASCII
> `/opt/workspace/…`). The dispatch **model** (base `0x80814`, 170-bound, opcode-`0x41`
> normalization, dual table) is HIGH from DEBUG's two clean 170-entry tables; the *byte-exact
> full PERF per-opcode rows past op `0xa0` are MED* (table/string overlap). `[MED for the
> exhaustive PERF per-opcode list]`

---

## 6. The DVE handler roster — 53 distinct, 28 DVE-only

**Method** (the same `S:`-handler set-diff used across the engine pages): extract every
single-token `S: <OpName>` handler-entry log from each engine's DEBUG DRAM (regex `^S:
[A-Za-z][\w/-]*$`), then set-diff against ACT / PE / POOL / SP. Per-engine distinct
handler-name counts: **DVE 53 \| POOL 41 \| ACT 26 \| PE 24 \| SP 18** — **DVE carries the
largest set**. `[HIGH/OBSERVED — 53 re-counted this pass]`

Each DVE-specific handler was verified to be a **real** handler: its `S:` string DRAM
offset is loaded by a `const16 a10,<off>` in the IRAM and a real `entry`-prologue handler
logs it. Spot-checked instruction-exact this pass:

```asm
; DveReadAccumulator handler @ IRAM 0xcb28 ; logs S: at DRAM 0x828c0 (file 0x28c0)
cb28:  entry   a1, 48
cb31:  const16 a10, 8
cb34:  const16 a10, 0x28c0        ; bytes a4c028 -> DRAM VA 0x828c0  "S: DveReadAccumulator"
cb37:  call8   0x18010            ; the log helper
```

Other verified `const16 ↔ DRAM-offset` xrefs: `BatchNormalize` `const16 a10,0x2261`
@`0xb4fc`; `Dropout` `0x1ee7` @`0x96c5`; `Stream-Transpose` `0x1ed2` @`0x95f5`; the
per-fetch `S: Dispatch opcode` `0xde0` @`0x2f5d`. `[HIGH/OBSERVED]`

### 6a. Shared SEQ control/move core (identical across all 5 engines, 18 handlers)

```
AluOp  BRANCH  BranchPrefetchHint  Event_Semaphore  EXT_BREAK  Halt
INS_BREAK  INS_FL  MOVE  NOP  NOTIFY  POLL_SEM  Redirect  SET_OM
STRONG_ORDER  TensorLoad  TensorStore  WRITE
```

This is the SEQ control/move/notify family — byte-for-handler-name identical in
ACT/PE/POOL/SP/DVE. `[HIGH/OBSERVED]`

### 6b. Shared basic-compute core (DVE shares with POOL)

```
EngineNop   MEMSET/RNG   Pool   Tensor-Reduce   Tensor-Scalar   Tensor-Scalar-PTR   Tensor-Tensor
```

DVE includes the basic tensor-compute primitives POOL also has. `[HIGH/OBSERVED]`

### 6c. The 28 DVE-only handlers (in DVE, absent from ACT/PE/POOL/SP)

| group | handlers | one-line role | committed kernel page |
|---|---|---|---|
| **Batch-norm statistics (6)** | `BatchNormalize`, `BatchNormalizeBackProp`, `BatchNormalizeGradAccum`, `BatchNormalizeGradAccum2`, `BatchNormalizeParamLoad`, `BatchNormalizeParamLoad2` | apply / backward / grad-accumulate / param-load batch-norm | [forward](../firmware/kernels/batchnorm-forward.md), [backprop](../firmware/kernels/batchnorm-backprop.md), [gradaccum](../firmware/kernels/batchnorm-gradaccum.md), [paramload](../firmware/kernels/batchnorm-paramload.md) |
| **Predicated copy/cast (4)** | `CastPredicated`, `CopyPredicated`, `CopyPredicatedReduce`, `CopyPredicatedScalar` | masked dtype-cast / masked copy / masked copy+reduce / masked copy w/ scalar | [castpredicated](../firmware/kernels/castpredicated.md), [copypredicatedreduce](../firmware/kernels/copypredicatedreduce.md), [copypredicatedscalar](../firmware/kernels/copypredicatedscalar.md) |
| **DVE-native (3)** | `DveReadAccumulator`, `DveReadIndices`, `Dropout` | read the `DveAcc` accumulator / read DVE index state / stochastic dropout | [dve-read-state](../firmware/kernels/dve-read-state.md), [dropout](../firmware/kernels/dropout.md) |
| **Match/find/select (5)** | `FindIndex8`, `MatchReplace`, `MatchValueLoad`, `RangeSelect`, `TensorScalarSelect` | 8-key first-match index / masked match-and-replace / 8-key state load / range-select / tensor-scalar select | [search-cluster](../firmware/kernels/search-cluster.md) |
| **Scan/transpose/shuffle (4)** | `TensorTensorScan`, `Tensor-Cumulative/Copy/Cast/Stream-Shuffle`, `Stream-Transpose`, `Scalar-Tensor-Tensor` | tensor-tensor prefix scan / fused cumulative-copy-cast-shuffle / streaming transpose / scalar-tensor-tensor fused | [tensorcumulative](../firmware/kernels/tensorcumulative.md), [stream-transpose](../firmware/kernels/stream-transpose.md) |
| **Cached / immediate tensor-scalar (6)** | `TensorScalarCacheCumulative`, `TensorScalarCacheReduce`, `TensorScalarImmLdArith`, `TensorScalarImmLdBitvec`, `TensorScalarPtrMultiArith`, `TensorScalarPtrMultiBitvec` | cached cumulative/reduce + immediate-load and pointer-multi arith/bitvec variants | [ts-cache-cumulative](../firmware/kernels/ts-cache-cumulative.md) |

`[HIGH/OBSERVED — all 28 present in the DEBUG `S:` roster; absent from the other four DEBUG
DRAMs]`

> **NOTE — `MatchReplace` (DVE self-name) vs `MatchReplace8` (opcode `0x6f`).** The DVE
> `S:` roster self-names the masked match-and-replace handler **`MatchReplace`**; the
> committed [search-cluster](../firmware/kernels/search-cluster.md) page (which decodes the
> opcode-layer struct) calls the `0x6f` op **`MatchReplace8`**. They are the same op — the
> `R:` runtime log `R: program_window: num=%d, mask=0x%llx, match=0x%llx, replace=0x%llx`
> is emitted by this handler, confirming the masked match→replace semantics. `[HIGH/OBSERVED
> — both strings read this pass]`

> **NOTE — `Max8` (`0x6c`) has no `S:` self-name in the DVE roster.** The
> [search-cluster](../firmware/kernels/search-cluster.md) page documents a four-op cluster
> `Max8`/`MatchValueLoad`/`FindIndex8`/`MatchReplace8` (`0x6c..0x6f`); three of the four
> appear in the DVE `S:` roster (`MatchValueLoad`, `FindIndex8`, `MatchReplace`) but
> **`Max8` does not** self-name. This is consistent with the search-cluster finding that
> these ops are *armed/emitted* by the firmware while the reduce/compare work runs in the
> DVE datapath — `Max8` dispatches without an independent `S:` log site. `[HIGH/OBSERVED —
> `Max8` byte-absent from the `^S:` roster]`

**DVE-vs-ACT direct diff** (same regex on both DEBUG DRAMs):

- **ACT-only**: `Activate`, `ActivateQuantize`, `ActivationReadAccumulator`,
  `ActivationTableLoad`, `Cast`, `Copy`, `TensorScalar`.
- **DVE-only**: the 28 above (plus DVE's `Pool`/`MEMSET-RNG`/`Tensor-*` that ACT lacks but
  POOL has).
- **Shared**: the 18-handler SEQ control/move core.

ACT and DVE are the same SEQ engine with **disjoint compute subsets**: ACT does LUT-driven
activation; DVE does the **data-dependent / statistical vector ops** — batch-norm
statistics, predicated copy/cast, index gather/find/match/replace/select, cumulative scans,
streaming transpose/shuffle, dropout — i.e. the operations that depend on **data values**
(indices, masks, running statistics). `[HIGH/OBSERVED handler names; the functional
characterization INFERRED-HIGH from the set]`

> **NOTE — DVE = Data/Vector Engine, `engine_idx = 3`.** The DVE-prefixed handlers
> (`DveReadAccumulator` reads the `DveAcc` accumulator register; `DveReadIndices`) and the
> `S: DVE perf mode support = %d` string name the engine directly; the corpus CSR enum
> (`PE=0 ACT=1 POOL=2 DVE=3 TPB_SP=4`) and the `dve_perf_cntr_ctrl@0xCC0` /
> `tpb_dve_in_conv_ctrl` CSRs corroborate. The "Data/Vector Engine" expansion is CARRIED
> from the corpus CSR map (not a string in the image), cross-confirmed by the `Dve*`
> handlers. `[handler names HIGH/OBSERVED; the expansion CARRIED from the CSR map]`

---

## 7. PROF CAM / TABLE — generic, shared across all four NX engines

DVE ships two HW-decode profiling tables; both are **byte-identical across ACT / DVE / PE /
POOL** (re-verified 4/4 this pass), so they are a **generic shipped resource**, not a
per-engine opcode list.

**PROF_CAM** (`0x400` = 1 KiB, sha `8fd7e422…`) — the HW-decode profiling CAM:
16-byte fixed-stride records, 64 slots, **47 populated** (`enable == 1`). Record =

```c
struct prof_cam_record {           /* 16 bytes */
    uint32_t opcode_id;            /* the profiled opcode (0x01, 0x06, 0x02, 0x07, 0x03, 0xa1, …) */
    uint32_t mask;                 /* = 0xff */
    uint32_t enable;               /* = 1 for the 47 populated slots */
    uint32_t reserved;             /* = 0 */
};
```

First records (re-read this pass): `0x01 0x06 0x02 0x07 0x03 0xa1 0xa4 0xa7 …`, all
`mask=0xff enable=1`. `[HIGH/OBSERVED — 47 populated re-counted; record layout decoded]`

**PROF_TABLE** (`0x2000` = 8 KiB, sha `ce761f81…`) — the profile counter/event descriptor
table: header word **`0x00000201`** (`01 02 00 00`), then **`0x26000010`**, then a small
ASCII descriptor blob and zeros. Of the 8 KiB only **329 bytes are non-zero**, all within
the first ~5.9 KiB; the rest is preallocated zero padding the HW-decode profiler fills at
run time. **Also byte-identical 4/4.** Exact counter/event schema not decoded.
`[HIGH provenance + header words + nonzero extent re-read this pass / MED schema]`

> **CORRECTION — the 47-record CAM is the GENERIC NX CAM, not a DVE (or ACT) opcode list.**
> Because DVE/ACT/PE/POOL ship the *same* `PROF_CAM` byte-for-byte (`8fd7e422…`), the 47
> profiled opcodes are **not** engine-specific. This corrects an earlier reading (in the
> ACT image survey) of "the ACT engine's 47 profiled opcodes" — that 47-opcode CAM is the
> **same generic CAM every NX engine carries**. SP ships **no** PROF tables at all. See
> [prof-cam-table-formats](prof-cam-table-formats.md). `[HIGH/OBSERVED — sha 4/4 identical]`

---

## 8. DEBUG vs PERF vs TEST — the variant delta

| VARIANT | IRAM size | DRAM size | `S:` strings | total strings | IRAM IVP-distinct |
|---|---:|---:|---:|---:|---:|
| **DEBUG** | `0x1bcc0` | `0x6d60` | **182** | 285 | 167 (FLIX-desync, lower) |
| **PERF** | `0x15c20` | `0x2fc0` | **0** | 16 | 317 |
| **TEST** | `0x15840` | `0x32c0` | **0** | 61 | 319 |

`[HIGH/OBSERVED — sizes from the getters; string counts + IVP-distinct re-derived this pass]`

- **DEBUG** is the largest and the **only** build carrying the 182 `S:` runtime log strings
  (vs ACT's 150 — DVE's richer handler set yields more logs). It is the RE substrate:
  every handler self-names via `S: <OpName>`.
- **PERF** (the production/release flavor) **strips all `S:` logs**: DRAM shrinks to
  `0x2fc0`, only **16** strings survive — all assertion source-paths
  (`…/cayman/seq/src/uarch.hpp`, `…/handlers/exception_handler.hpp`,
  `…/src/decode/{alu_op,move,branch}.cpp`, `…/handlers/signal_handler.cpp`) plus the
  `ok_to_evict` out-of-bounds WARNING and `Assertion failure!`. IRAM shrinks too; the log
  call-sites are gone; IVP-distinct rises to 317 (PERF schedules/inlines more vector
  compute). `[HIGH/OBSERVED — all 16 PERF strings enumerated this pass]`
- **TEST** sits between: 0 `S:` logs but 61 strings — keeps function-name / file symbols
  for assert context (a symbol/assert build).

> **NOTE — a DEBUG→RELEASE(PERF) swap is a pure observability change.** The dispatch
> mechanism is **invariant** across all three: same reset vector (`06 76 00 00`), same DRAM
> table at `0x814` (+ second at `0xabc`), same 170-bound, same ErrorHandler arms, same
> `cayman/seq/` codebase. The variant axis changes *what is logged*, not *what is dispatched*.
> `[HIGH/OBSERVED]`

---

## 9. Cross-engine code-sharing

| ENG | PERF_IRAM sha | PERF_IRAM size | PROF_CAM | PROF_TABLE |
|---|---|---:|---|---|
| ACT | `5ef2a351` | `0x13dc0` | `8fd7e422` | `ce761f81` |
| **DVE** | `9fa066f4` | `0x15c20` | `8fd7e422` | `ce761f81` |
| PE | `13ba3969` | `0x159e0` | `8fd7e422` | `ce761f81` |
| POOL | `9049bf8c` | `0x17280` | `8fd7e422` | `ce761f81` |
| SP | `5a6f6eaa` | `0x182c0` | — | — |

`[HIGH/OBSERVED — PERF_IRAM shas/sizes + PROF 4/4 identity re-verified this pass]`

- **Code/data (IRAM/DRAM): DVE shares NO bytes with ACT/POOL/PE/SP.** Each engine's
  `PERF_IRAM` has a distinct sha (and a distinct size). Each engine is a
  **separately-compiled** build of the same `cayman/seq/` source with its own handler subset
  linked in. The sharing is at the **source/structure** level (identical reset vector,
  dispatch model, control/move core, source tree), **not** at the linked-byte level.
- **Profiling tables: SHARED byte-for-byte across all 4 NX engines** (§7). SP ships no PROF
  tables.
- **Reset vector**: byte-identical (`06 76 00 00` / `86 77 00 00`) across all 5 engines; the
  boot bodies past byte 6 are engine-specific.

`[HIGH/OBSERVED]`

---

## 10. Engine-model classification

DVE is a **SEQ-style ASCII-opcode dispatch engine** — the same `cayman/seq/` firmware as
ACT/POOL/PE/SP, compiled with the data/vector handler subset. The "same SEQ engine,
different handler subset" hypothesis is **CONFIRMED** for DVE:

| property | DVE | SEQ / POOL | ACT |
|---|---|---|---|
| packaging | flat IRAM/DRAM | flat IRAM/DRAM | flat IRAM/DRAM |
| reset vector | `j 0x1dc` (`06 76 00 00`) | `j 0x1dc` | `j 0x1dc` |
| dispatch base | DRAM `0x80814` (+`0x80abc`) | DRAM `0x80814` | DRAM `0x80814` |
| entry size | 4 B | 4 B | 4 B |
| **bound** | **170** (`movi a3,169`) | **178** (`movi a3,177`) | 178 (PERF table) |
| key | opcode byte − `0x41` | byte − `0x41` | byte − `0x41` |
| lookup | direct-indexed `jx` (O(1)) | direct-indexed `jx` | direct-indexed `jx` |
| handler form | C++ Handler + `S:` log | C++ Handler + `S:` log | C++ Handler + `S:` log |
| miss policy | `ErrorHandler "Bad Opcode"` | `ErrorHandler "Bad Opcode"` | `ErrorHandler "Bad Opcode"` |
| source tree | `cayman/seq/src/…` | `cayman/seq/src/…` | `cayman/seq/src/…` |
| distinct handlers | **53** (28 DVE-only) | POOL 41 | 26 (7 ACT-only) |
| compute subset | batch-norm / predicated / match-find-select / scan / transpose / dropout / `Dve*` | pool / reduce / gather / … | Activate / Quantize / TableLoad / ReadAccum / Cast |

`[HIGH/OBSERVED]`

> **NOTE — what DVE is NOT.** DVE is **not** a POOL `kernel_info_table` Q7-EXTISA engine
> (the Q7 POOL EXTISA ELF is a different, separate compute back-end — see
> [extisa inventory](extisa-inventory.md)), and it is **not** a standalone vector core.
> DVE is the **control/sequencer front-end** for the Data/Vector engine: it decodes the
> `S:` instruction stream and routes to the data/vector handlers, which run vector math
> (317 IVP ops in PERF) on the single NX core's datapath. The hardware map: `TPB_0_DVE` at
> SoC base `0x2802B00000` (`0x500000` span), a **single Xtensa-NX core** (no Q7 sub-array —
> only POOL has the 8-core Q7 array), `engine_idx = 3`. `[HIGH — `j 0x1dc`/dispatch/handler
> facts OBSERVED this pass; the hardware base/span CARRIED from the corpus address survey +
> the CSR enum]`

> **NOTE — forward to MAVERICK.** On MAVERICK (NC-v5) the **ACT engine is folded into DVE**
> (header-OBSERVED: DVE absorbs ACT's activation handlers). The CAYMAN DVE roster here is
> the pre-fold baseline; see [maverick-dve.md](maverick-dve.md) for the absorbed-ACT diff
> and [mariana-dve.md](mariana-dve.md) for the NC-v4 step. `[CARRIED from the MAVERICK
> header observation]`

---

## 11. Honesty ledger

**HIGH / OBSERVED** (direct disasm or byte read this pass):

- 14 `CAYMAN_NX_DVE` getters (`nm` + getter-body `movq` sizes 14/14 exact); 8 real + 6
  zero-size boundary cursors (all six `movq $0x0`; cursors alias the next-engine PE IRAM).
- 8 real carves byte-identical (sha256) to the named `libnrtucode.a` member `.rodata`, 8/8.
- All carves FLAT (no ELF magic); reset vector `06 76 00 00` (`j 0x1dc`) identical across
  DEBUG/PERF/TEST and across all 5 engines; boot `const16 a0,0x90 ; jx`; 2nd vector
  `j 0x1e8 → halt 0`.
- Dispatch: base DRAM `0x80814` (+ second `0x80abc`), 170-bound (`movi a3,169`, two sites),
  opcode-`0x41` normalization, `S: Dispatch opcode=0x%x` log at DRAM `0xde0`, dual
  HW-Decode/Sunda tables (75 real / 95 default each, parallel `+0x7df`).
- 182 `S:` strings in DEBUG DRAM; 53 distinct handler names; the 28 DVE-specific handlers
  named from their own logs and each verified via the IRAM `const16 ↔ DRAM-offset` xref
  (`DveReadAccumulator@0xcb28`, `BatchNormalize@0xb4fc`, `Dropout@0x96c5`,
  `Stream-Transpose@0x95f5`).
- ErrorHandler arms present; source tree `cayman/seq/src/…` in all 3 variants.
- PROF_CAM 16-byte `{opcode, mask=0xff, enable=1, rsvd}` × 47; PROF_CAM/PROF_TABLE
  byte-identical across ACT/DVE/PE/POOL (`8fd7e422` / `ce761f81`, 4/4); PROF_TABLE header
  words `0x201`/`0x26000010`, 329 nonzero bytes.
- PERF_IRAM distinct per engine (5 distinct shas/sizes); DVE shares no IRAM/DRAM bytes.
- DEBUG/PERF/TEST size + string-count diff; `ncore2gp` objdump decodes all 3 IRAM to real
  windowed-ABI + FLIX-VLIW (615 `entry` / 829 `retw` in DEBUG; 317/319 IVP ops PERF/TEST);
  exit 0, empty stderr; IVP families uint→float / ordered-compare / select all present.
- Engine-identity string `engine_base_addr…→engine_idx` present.

**MED / INFERRED:**

- Exhaustive per-opcode PERF dispatch table past op `~0xa0` (PERF DRAM table/string-pool
  overlap); the base/bound/normalization/dual-table are HIGH from DEBUG's two clean tables.
- PROF_TABLE field schema (header `0x201` / `0x26000010` + descriptor): structure-level only.
- DVE functional characterization ("data-dependent / statistical vector ops"):
  INFERRED-HIGH from the handler set; the handler **names** are OBSERVED.

**CARRIED (cross-report, not re-derived here):**

- "Data/Vector Engine" expansion + `engine_idx = 3` (corpus CSR enum); the `TPB_0_DVE`
  hardware base `0x2802B00000` / `0x500000` span; PERF-as-default-flavor; the MAVERICK
  ACT→DVE fold (header-OBSERVED).

**LOW / NOT CLAIMED:**

- Which silicon part / runtime selects DEBUG vs PERF vs TEST (host driver +
  `NEURON_UCODE_FLAVOR`).
- The SUNDA `NX_DVE_RELEASE` variant (out of CAYMAN scope).
- The exact per-opcode operand layout of each DVE handler (per-handler scope — see the
  committed kernel pages of §6c).

---

## Cross-references

- **[Image Catalog Index](image-catalog-index.md)** — the 266/386-getter image catalog;
  the CAYMAN × DVE rows live here.
- **[Firmware Image Catalog](firmware-image-catalog.md)** — the per-image getter inventory.
- **[PROF CAM/TABLE Formats](prof-cam-table-formats.md)** — the generic 47-record CAM and
  8 KiB profile table shared across the NX engines (§7).
- **[Cross-Generation Kernel-Info Matrix](cross-gen-kernel-info-matrix.md)** — the
  per-generation handler-presence matrix.
- **[MARIANA × DVE image](mariana-dve.md)** / **[MAVERICK × DVE image](maverick-dve.md)** —
  the NC-v4 step and the NC-v5 ACT→DVE fold, both diffed against this baseline.
- **The committed DVE kernel pages** decoded opcode-exact: batch-norm
  ([forward](../firmware/kernels/batchnorm-forward.md), [backprop](../firmware/kernels/batchnorm-backprop.md),
  [gradaccum](../firmware/kernels/batchnorm-gradaccum.md), [paramload](../firmware/kernels/batchnorm-paramload.md)),
  [dropout](../firmware/kernels/dropout.md),
  [search-cluster](../firmware/kernels/search-cluster.md),
  predicated ([castpredicated](../firmware/kernels/castpredicated.md),
  [copypredicatedreduce](../firmware/kernels/copypredicatedreduce.md),
  [copypredicatedscalar](../firmware/kernels/copypredicatedscalar.md)),
  [stream-transpose](../firmware/kernels/stream-transpose.md),
  [tensorcumulative](../firmware/kernels/tensorcumulative.md),
  [ts-cache-cumulative](../firmware/kernels/ts-cache-cumulative.md),
  [dve-read-state](../firmware/kernels/dve-read-state.md).
- **[The Confidence & Walls Model](../reference/confidence-model.md)** — the
  `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` tags and the FLIX-desync ceiling this page applies.
