# MAVERICK × POOL image (dual-core)

This page diffs the **MAVERICK (v5) × POOL** firmware image against the committed,
byte-true [MARIANA_PLUS × POOL baseline](./mariana-plus-pool.md). POOL is the **only**
one of the NX engines that ships **two distinct firmware cores** (`engine_idx = 2`):
an NX-class SEQ sequencer (`NX_POOL`, `'S:'` dialect) and a per-pool-core
`kernel_info_table` compute engine (`Q7_POOL`, `'P%i:'` dialect), bridged by the `0xF0`
`ExtendedInst` escape. This is a **DIFF page** — it does **not** re-derive the dual-core
model, the SEQ dispatch hub, the `kernel_info_table` back-end, or the `0xF0`
reconciliation; those are owned by the [MARIANA baseline](./mariana-pool.md) and the
[MARIANA_PLUS diff](./mariana-plus-pool.md). Read those for the *model*; this page records
**only what changed** across v4+ → v5, for **both** cores.

> **CRITICAL WALL — MAVERICK (v5) is HEADER-OBSERVED.** Unlike the byte-grounded
> v2–v4(+) engines, the MAVERICK generation is characterized primarily from its
> **carved image headers**, the **embedded `kernel_info_table` bytes**, the **reset/boot
> bytes**, and the **shipped clean C ISA enums** — the *deep code interiors* (the SEQ
> opcode table rows, the per-instruction kernel bodies) sit in FLIX-desynced spans or
> stripped `ET_DYN` SOs and are **INFERRED**, flagged inline. The carved
> `kernel_info_table`s, the reset bytes, and the PROF blobs **ARE OBSERVED** (parsed /
> `cmp`-clean this session). `MAVERICK_Q7_CC_TOP` is **FILE-ABSENT**; `arch_id 36` is
> **INFERRED**. [WALL]

Every size, sha256, opcode, reset byte and `kernel_info_table` key below reads directly
from `libnrtucode_internal.so` (sha256 `b7c67e89…632fc329b`) via its 10
`MAVERICK_NX_POOL_*_get` + 20 `MAVERICK_Q7_POOL_*_get` accessors, carved from
identity-mapped `.rodata`, with the shipped Cadence Vision-Q7 `ncore2gp`
`xtensa-elf-objdump` decoding the flat blobs and `xtensa-elf-readelf` the EXTISA ELFs.

> **THE HEADLINE, UP FRONT — the counterintuitive v5 finding.**
> **The v5 MX/dtype "expansion" does NOT land on POOL.** All four MAVERICK Q7 EXTISA
> `kernel_info_table`s (17/1/2/9 entries) carry the **IDENTICAL `(opcode,spec)` KEY SET**
> as MARIANA_PLUS POOL — **byte-for-key, ZERO rows added, ZERO removed** (`ADDED = []`,
> `REMOVED = []`, parsed byte-exact this session). The dequant op `0x7b`
> (→ `proc_4bit_mx_8`) and the whole codec family route **exactly** as on v4; the MX
> dequant + **both** RNG algos (TIE + LFSR) are **RETAINED byte-for-name**. The genuine
> v5 MX-expansion opcodes — `QUANTIZE_MX 0xe3`, `MATMUL_MX 0x0A`, `LDWEIGHTS_MX 0x09` —
> are **DVE/PE opcodes, NOT POOL** (proven absent from every POOL KIT; present in the
> shipped maverick enum, owned by [DVE](./maverick-dve.md)/[PE](./maverick-pe.md)). What
> *did* change is a **structural re-build**: a **new `j 0x1d8` NX_POOL reset** and a
> **new `j 0x1e4` Q7 shift** — and the Q7 core, which **never shifted on any prior gen**,
> moves on v5. [KIT key-set / MX-opcode / reset: HIGH/OBSERVED]

Unlike MARIANA_PLUS (a recompile that *added* the DGE fast-path), MAVERICK POOL is a
**genuine v5 generation step** that *removes* surface: NX DEBUG dropped, the **entire
DGE/reshape subsystem dropped firmware-wide** (POOL was the v4+ fast-path's home engine),
Q7 DKL dropped, Q7 runs from **SRAM not IRAM**, the EXTISA kernels re-built as **stripped
`ET_DYN`**, and **every POOL image is SMALLER** than MARIANA_PLUS — an independent v5 build
(4.5–7.0 % block-similarity). This is consistent with the sibling v5 engine pages
([ACT folded into DVE](./maverick-act.md), [DVE](./maverick-dve.md),
[PE](./maverick-pe.md)): real gen-step, new reset, DGE fast-path dropped. [HIGH/OBSERVED;
HW-DMA re-architecture reading INFERRED]

Confidence/evidence tags follow the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**.

Related pages: [MARIANA_PLUS × POOL (the diff base)](./mariana-plus-pool.md) ·
[MAVERICK × DVE (ACT-fold, QuantizeMx target)](./maverick-dve.md) ·
[MAVERICK × PE (MATMUL_MX/LDWEIGHTS_MX)](./maverick-pe.md) ·
[MAVERICK × ACT (the v5 anchor)](./maverick-act.md) ·
[Cross-Gen kernel_info_table matrix](./cross-gen-kernel-info-matrix.md) ·
[MX Dequant (op 0x7b)](../firmware/kernels/mx-dequant.md) ·
[kernel_info_table Layout](../firmware/pool/kernel-info-table.md) ·
[Firmware-Image Accessor Index](./image-catalog-index.md).

---

## 1. The cross-gen delta table (MARIANA_PLUS → MAVERICK, per core)

The whole page in one table. `(==)` marks invariant rows; the **bold** rows are the real
v5 changes. The table **leads with the no-MX-expansion-on-POOL headline and the new reset
geometry** — the two findings that distinguish v5 POOL from a recompile.

| AXIS | `NX_POOL` (SEQ) | `Q7_POOL` (compute) | Δ |
|---|---|---|---|
| **KIT KEY SET** | (n/a — SEQ has no KIT) | **17/1/2/9 entries, `(opcode,spec)` byte-for-key == MARIANA_PLUS; `+0/−0`** | **`(==)` NO MX EXPANSION** |
| **reset vector** | **`06 75 00 00` → `j 0x1d8`** (was `06 7d`→`j 0x1f8`) | **`06 78 00 00` → `j 0x1e4`** (was `06 7f`→`j 0x200`) | **NEW v5 SHIFT both cores** |
| **secondary reset** | **`86 76 00` → `j 0x1e4`** (halt) | **`86 79 00` → `j 0x1f0`** | **NEW** |
| **boot path** | `const16 a0,0x94 ; jx a0` → `enter_run@0x94` (was `@0x90`) | new v5 build (26.3 % byte-id to v4+) | **`enter_run` `0x90`→`0x94`** |
| getters (`nm` / IDA sidecar) | 10 / 10 (no DEBUG) | 20 / 20 (no DKL) | **REDUCED** (was 14 / 32) |
| variant set | PERF / TEST / PROF — **DEBUG DROPPED** | PERF / TEST / DEBUG — **DKL DROPPED** | **both shed variants** |
| code residence | IRAM (flat) | **SRAM** (IRAM size 0) | **Q7 moved to SRAM** |
| `dge_*` / reshape subsystem | **DROPPED firmware-wide** (0 of 9 strings) | rdma desc-gen logging **DROPPED** (25 strings) | **DGE fast-path home → GONE** |
| `0xF0` bridge | slot not byte-resolved (FLIX) | five `0xf0+spec{0,1,2,4,3}` rows byte-for-key | **`(==)` intact (Q7-proven)** |
| dtype strings (NX) | `{UINT32,INT32,FP32}` only | MX dequant (`proc_4bit_mx_8`/`cptc_decode`) RETAINED | `(==)` |
| MX dequant op `0x7b` | (numeric in SEQ) | **RETAINED**, idx16 funcVA `0x50ec` (recompiled body) | `(==)` routing |
| RNG body | (no RNG kernel) | `XorwowRng(TIE)` + `Lfsr` + `rand_algo` — **RETAINED byte-for-name** | `(==)` carried |
| EXTISA SO form | (n/a) | **stripped `ET_DYN`** (was named `ET_EXEC`) | **rebuilt** |
| PROF | **`0951b326`/`534f2239` — byte-IDENTICAL, disarmed** | (no PROF) | **`(==)` REUSED VERBATIM** |
| `.a` reconcile | 0 members | 0 members | **internal-twin-EXCLUSIVE** |
| IMAGE size | **SHRANK** (PERF `−0xac80`, TEST `−0xa140`) | **SHRANK** (code `−0x2060`, EXTISA `−0x22b0`) | **every image smaller** |

**The diff is the OPPOSITE direction from v4+.** Where MARIANA_PLUS *grew* the NX IRAM
(the DGE fast-path add) and kept the Q7 release path byte-identical, MAVERICK *shrinks*
both cores, drops the DGE subsystem that POOL anchored, and re-builds the Q7 release path
fresh. Yet the **dual-core model**, the **`0xF0` bridge**, the **`kernel_info_table` key
set**, the **MX dequant**, the **TIE+LFSR RNG**, and the **disarmed PROF** are all
invariant. A MARIANA_PLUS ↔ MAVERICK POOL swap is **a full v5 re-build with the routing
contract preserved**, not an MX/dtype expansion. [HIGH/OBSERVED for the deltas]

---

## 2. THE HEADLINE — NO MX/dtype expansion on POOL (the counterintuitive v5 finding)

The task's key v5 question: *does `Q7_POOL` expand the MX/dtype surface on v5?* The
intuition says yes — v5 ships a real MX dtype superset, and POOL is where the MX **dequant**
machinery (`proc_4bit_mx_8`) lives. The answer is **NO at the routing level**, proven
byte-for-key.

### 2.1 All four Q7 EXTISA `kernel_info_table`s — IDENTICAL key set

Each EXTISA SO embeds a `kernel_info_table` of 8-byte records
`{ u8 0; u8 0; u8 spec(+2); u8 opcode(+3); u32_le funcVA(+4) }` (the
[FW-18 / kernel-info-table format](../firmware/pool/kernel-info-table.md)). `readelf -S`
locates each table; parsed byte-exact this session. The **17-entry EXTISA_0** table (the
full POOL routing table) reads: [HIGH/OBSERVED]

| idx | opcode | spec | funcVA (rel, ET_DYN) | routing (== MARIANA_PLUS names) |
|---|---|---|---|---|
| 0 | `0x7e` | 0 | `0x0000007c` | `pool_iota` |
| 1 | `0x7c` | 0 | `0x00000384` | `pool_cross_lane_reduce_arith` |
| 2 | `0x7d` | 0 | `0x0000039c` | `pool_cross_lane_reduce_bitvec` |
| 3 | `0x45` | 0 | `0x00000be8` | `decode_pool` [Pool] |
| 4 | `0x51` | 0 | `0x00001384` | (tensor primitive) |
| 5 | `0x41` | 0 | `0x000010c0` | `decode_tensor_tensor_arith` |
| **6** | **`0xf0`** | **0** | `0x00003824` | `ExtendedInst` spec0 |
| **7** | **`0xf0`** | **1** | `0x00003840` | `ExtendedInstCopy` |
| **8** | **`0xf0`** | **2** | `0x0000393c` | `decode_extended_inst_tensor_tensor_arith` |
| **9** | **`0xf0`** | **4** | `0x00003c5c` | ExtendedInst spec4 → Rand band |
| **10** | **`0xf0`** | **3** | `0x00003e58` | ExtendedInst spec3 → Rand band |
| 11 | `0x52` | 0 | `0x00004060` | (tensor primitive) |
| 12 | `0x46` | 0 | `0x000041c0` | `pool_copy` |
| 13 | `0x47` | 0 | `0x00004260` | (tensor primitive) |
| 14 | `0xbe` | 0 | `0x00004770` | (tensor primitive) |
| 15 | `0xf2` | 0 | `0x00004bfc` | `get_sequence_bounds` / `NonzeroWithCount` |
| 16 | `0x7b` | 0 | `0x000050ec` | `decode_tensor_dequantize` [TensorDequantize] → `proc_4bit_mx_8` |

The other three tables: **EXTISA_1** = 1 entry (`0x7e`); **EXTISA_2** = 2 (`0x7c`, `0x7d`);
**EXTISA_3** = 9 (`0x7e`/`0x7c`/`0x7d`/`0x45`/`0xbe`/`0xf2`/`0x7b`/`0xe4`/`0xf0+spec7` — the
cptc/MX codec family). `op 0xe4 = CONV_LUT_LOAD` per the shipped maverick enum (present on
both gens, **not** a dequant). [HIGH/OBSERVED]

> **RESULT — the key-set diff (parsed both gens this session).** For **every** one of the
> four tables, the `(opcode,spec)` key set diffed against the MARIANA_PLUS EXTISA SO is
> `ADDED = []`, `REMOVED = []` → **byte-for-key IDENTICAL**. The union of **all** POOL KIT
> opcodes is `{0x41, 0x45, 0x46, 0x47, 0x51, 0x52, 0x7b, 0x7c, 0x7d, 0x7e, 0xbe, 0xe4,
> 0xf0, 0xf2}` — and **no** v5 MX opcode is in it. **ZERO new `opcode→funcVA` rows; ZERO
> removed.** [HIGH/OBSERVED]

### 2.2 The v5 MX-expansion opcodes are DVE/PE, NOT POOL

The shipped clean ISA header `neuron_maverick_arch_isa/tpb/aws_neuron_isa_tpb_common.h`
defines the v5 MX opcodes — read directly this session: [HIGH/OBSERVED]

```c
NEURON_ISA_TPB_OPCODE_LDWEIGHTS_MX      = 0x09,   // PE  — MX weight load
NEURON_ISA_TPB_OPCODE_MATMUL_MX         = 0x0A,   // PE  — MX matmul feeder
NEURON_ISA_TPB_OPCODE_TENSOR_DEQUANTIZE = 0x7b,   // POOL — the ONLY POOL MX surface
NEURON_ISA_TPB_OPCODE_QUANTIZE_MX       = 0xe3,   // DVE — QuantizeMx (the IMG-19 target)
NEURON_ISA_TPB_OPCODE_CONV_LUT_LOAD     = 0xe4,   // POOL KIT idx (EXTISA_3) — not a dequant
```

`LDWEIGHTS_MX 0x09`, `MATMUL_MX 0x0A` and `QUANTIZE_MX 0xe3` are **provably absent from all
four POOL KITs** (checked this session — `False` for each). They are owned by
[PE](./maverick-pe.md) (`0x09`/`0x0A`) and [DVE](./maverick-dve.md) (`0xe3`). POOL's only MX
surface is the **pre-existing** `TENSOR_DEQUANTIZE 0x7b` — the **dequant** direction
(in-band block-of-8, present since NC-v3), a *different, older* mechanism than the v5
`QuantizeMx`/`MATMUL_MX` **forward** path with its out-of-band `MXTENSOR_V2` / `SFP8_E8`
(E8M0) scale surface. So the v5 MX expansion is real — but it lands on **DVE/PE**, not as
new POOL `kernel_info_table` rows. [HIGH/OBSERVED enum + KIT]

> **CORRECTION — refining the [DVE page](./maverick-dve.md)'s "QuantizeMx migrates to the
> Q7 POOL MX path".** That migration statement is about the *MX machinery's home engine*,
> not about POOL gaining the `QuantizeMx` *opcode*. `QUANTIZE_MX 0xe3` is the **DVE
> forward-direction** op; the POOL Q7 carries the **DEQUANT-direction** MX (`op 0x7b →
> proc_4bit_mx_8`), which is pre-existing. The two are distinct opcodes on distinct
> engines. The v5 forward MX op set adds **no** POOL KIT rows. [HIGH/OBSERVED]

### 2.3 The funcVAs differ — same routing, independently recompiled kernels

The MAVERICK EXTISA SOs are **stripped `ET_DYN`** (`.text` rebased to VMA `0x0`, no
`.xt.prop`, no symtab — `readelf -h` Type `DYN`), vs MARIANA_PLUS **`ET_EXEC`** (`.text`
@`0x01000000`, named `.xt.prop` sections). The `MPLUS − MAVERICK` funcVA deltas are **NOT a
constant rebase** — they range `0xfffad4 .. 0x1000074` (clustered around `0x1000000` but
per-function variable), i.e. the kernel functions were **re-ordered/re-sized**: **same
routing key set, independently recompiled kernel bodies**. The dequant `op 0x7b` lands at
rel funcVA `0x50ec` (MAVERICK) vs `0x01004e04` (MARIANA_PLUS) — byte-pinned present, routing
to a recompiled `decode_tensor_dequantize`. Every EXTISA SO is **smaller**
(`0_SO −0x22b0`, `1_SO −0x2f8`, `2_SO −0x280`, `3_SO −0x13d4`). [HIGH/OBSERVED readelf +
funcVA parse; the per-instruction kernel bodies are stripped and **INFERRED**]

---

## 3. The new v5 reset geometry — BOTH cores shift (the second headline)

The byte-level signature that `NX_POOL` and `Q7_POOL` are **two separate cores**, and the
v5 finding that **both** cores' resets move — including the Q7 core, which had been
**unchanged on every prior gen** (CAYMAN/MARIANA/MARIANA_PLUS Q7 all reset `j 0x200`). All
reset bytes read with `xxd -l16` and decoded with `ncore2gp` `xtensa-elf-objdump` (exit 0)
this session. [HIGH/OBSERVED]

### 3.1 `NX_POOL` — new `j 0x1d8` (the −0x20 v5 shift)

```text
MAVERICK     NX IRAM:  06 75 00 00 | 00 00 | 86 76 00 00 | 00 00 | a0 71 69 80
MARIANA_PLUS NX IRAM:  06 7d 00 00 | 00 00 | 86 7e 00 00 | 00 00 | a0 71 69 80
                       └ j 0x1d8 ┘         └ j 0x1e4 ┘            └ shared boot stub ┘
```

`ncore2gp` decode of the MAVERICK head and trampoline:

```text
0x000:  06 75 00   j 0x1d8     ; primary reset  -> boot  (was j 0x1f8 on v4+)
0x006:  86 76 00   j 0x1e4     ; secondary      -> halt  (was j 0x204)
0x1d8:  const16 a0, 0 ; const16 a0, 148 (0x94) ; jx a0   -> enter_run @0x94  (was @0x90)
0x1e4:  halt 0
0x1eb:  const16 a4, 0x1330     ; SEQ sub-table base
```

**The v5 shift: primary `0x1f8 → 0x1d8` (−0x20), secondary `0x204 → 0x1e4` (−0x20),
`enter_run 0x90 → 0x94` (+4)** — the **same v5 geometry** the
[DVE](./maverick-dve.md)/[ACT](./maverick-act.md) pages found, **not** the MARIANA `+0x1c`
forward shift. The first MAVERICK-vs-MARIANA_PLUS NX divergence is at **byte 0x1** (`75` vs
`7d`) — the reset immediate, the recompile/re-build signature. The DRAM head word is still
`0x6099cb34` (the `.globstruct` dispatcher-state magic, `xxd` `34 cb 99 60`), byte-identical
cross-gen. [HIGH/OBSERVED]

### 3.2 `Q7_POOL` — new `j 0x1e4` (the −0x1c v5 shift; the Q7 core MOVES)

The Q7 compute code lives in the **SRAM** segment on MAVERICK (IRAM size 0 — §4). Its reset
head, `xxd`/`ncore2gp`:

```text
head: 06 78 00 00 00 00 86 79 00 00 00 00 a0 71 69 80
0x000:  06 78 00   j 0x1e4     ; primary reset -> boot   (was j 0x200 on EVERY prior gen)
0x006:  86 79 00   j 0x1f0     ; secondary     -> halt   (was j 0x20c)
```

**The v5 Q7 shift: `0x200 → 0x1e4` (−0x1c), `0x20c → 0x1f0` (−0x1c).** The Q7 boot stub is a
**new v5 build** (26.3 % byte-identity to the MARIANA_PLUS Q7 over the first `0x220`,
diverging at byte 0x1).

> **CORRECTION — "the Q7 core never shifts on any gen" no longer holds.** The
> [MARIANA_PLUS § 4B baseline](./mariana-plus-pool.md) established that the Q7 reset was
> byte-identical across CAYMAN/MARIANA/MARIANA_PLUS (the `+0x1c` shift was **NX-only**).
> MAVERICK **re-shifts the Q7 core too** (`−0x1c`). v5 is the first generation to move the
> Q7 reset. [HIGH/OBSERVED]

### 3.3 The structural re-build evidence

The reset shift is one face of a fully independent v5 build, not a relocation:

| evidence | MARIANA_PLUS | MAVERICK | reading |
|---|---|---|---|
| NX_POOL PERF_IRAM size | `0x17bc0` | `0xcf40` (**−0xac80**) | smaller, not a recompile-grow |
| NX_POOL PERF_IRAM sha[:16] | `582c246b…` | `150437ba…` | independent build |
| first NX divergence | — | **byte 0x1** (reset immediate) | diverges at the very first byte |
| NX block-similarity vs v4+ | — | **7.0 %** (231/3316 16-B blocks) | independent v5 build |
| Q7 code block-similarity | — | **4.5 %** (232/5192) | independent v5 build |
| EXTISA SO form | `ET_EXEC` named | **stripped `ET_DYN`** | re-built kernels |

Both cores are independent v5 builds. The "Q7 release path byte-identical cross-gen"
property that held v4 ↔ v4+ ([MARIANA_PLUS § 8](./mariana-plus-pool.md)) **does not carry to
v5** — a striking contrast. [HIGH/OBSERVED]

---

## 4. The dual-core inventory + carve (30 getters, REDUCED shape)

`nm libnrtucode_internal.so | rg -c 'MAVERICK_(NX|Q7)_POOL_.*_get$'` = **30**: **10
`NX_POOL`** + **20 `Q7_POOL`** (20 real + 10 zero-size boundary cursors). The shape vs
MARIANA_PLUS POOL (14 NX + 32 Q7 = 46) is **reduced**: NX_POOL **drops DEBUG** (no DEBUG
getters → no `'S:'` handler surface); Q7_POOL **drops DYNAMIC_KERNEL_LOAD (DKL)** entirely
(12 DKL getters gone). Each getter is the canonical 4-instruction `(img-ptr, size)` stub
(`lea <blob>(%rip),%rax ; mov %rax,(%rdi) ; movq $<size>,(%rsi) ; ret`); all 30
`(img-ptr, size)` pairs parse instruction-exact and match the
[image-catalog index](./image-catalog-index.md) rows 509–546. [HIGH/OBSERVED]

> **GOTCHA — `MAVERICK` is internal-twin-EXCLUSIVE.** `ar t libnrtucode.a` carries **0
> MAVERICK members** (only CAYMAN/MARIANA/MARIANA_PLUS/SUNDA `img_` + `hwdecode_` PROF
> members). Unlike the MARIANA_PLUS POOL carve (12/12 `.so == .a` reconciled), there is
> **no `.a` byte-reconcile** for MAVERICK — the carve is single-source by necessity.
> Cross-validation is by the `(img-ptr, size)` getter parse + the sha256 manifest + the
> gen-boundary contiguity arithmetic (§4.2). [HIGH/OBSERVED]

### 4.1 The carved images (sha256 verified this session)

**`NX_POOL` (CLS = NX) — 10 getters (SEQ sequencer; NO DEBUG):**

| VARIANT | REGION | IMG-PTR (.rodata = file off) | SIZE | sha256[:16] / STATUS |
|---|---|---|---|---|
| PERF | IRAM | `0x8c12e0` | `0xcf40` | `150437bae67c75db` (SEQ code; reset `j 0x1d8`) |
| PERF | DRAM | `0x8ce220` | `0x25c0` | `1efa8f0aca15933d` (SEQ data; magic `0x6099cb34`) |
| TEST | IRAM | `0x8dedc0` | `0xd560` | `768239aa98e67b93` (SEQ code; symbols) |
| TEST | DRAM | `0x8ec320` | `0x27c0` | `a3ce05f819e688e2` (SEQ data + symbols) |
| **PROF** | **CAM** | `0x9a8aa0` | `0x400` | `0951b326f4a40ccd` (**disarmed; == MARIANA_PLUS**) |
| **PROF** | **TABLE** | `0x9a8ea0` | `0x2000` | `534f2239b9e76d1c` (**== MARIANA_PLUS**) |

(The 4 SRAM/EXTRAM rows are zero-size boundary cursors. **No DEBUG IRAM/DRAM** — the `'S:'`
handler surface lives in DEBUG, which POOL drops on v5.)

**`Q7_POOL` (CLS = Q7) — 20 getters (compute core; runs from SRAM; NO DKL):**

| VARIANT | REGION | IMG-PTR | SIZE | sha256[:16] / STATUS |
|---|---|---|---|---|
| PERF | IRAM | `0x912320` | `0x0` | EMPTY (cursor) — **Q7 IRAM is size 0** |
| PERF | DRAM | `0x912320` | `0x13000` | `360bd0aaaa51935d` (Q7 data) |
| **PERF** | **SRAM** | `0x925320` | `0x14480` | `bb5b3af2169d59ed` (**Q7 COMPUTE CODE**) |
| TEST | DRAM | `0x9397a0` | `0x13300` | `7f923c67117960ec` (Q7 data) |
| TEST | SRAM | `0x94caa0` | `0x15dc0` | `15824326ddb35b06` (Q7 compute code) |
| DEBUG | DRAM | `0x962860` | `0x15480` | `a34dc9245ac6d0cb` (data + `'P%i:'` logs) |
| DEBUG | SRAM | `0x977ce0` | `0x1d100` | `6bcbbdd3895cb6dd` (Q7 compute code) |
| PERF_EXTISA_0 | SO | `0x994de0` | `0x7fb0` | `a92c8ba0e9dfb2d8` (`ET_DYN`; **17-entry KIT**) |
| PERF_EXTISA_1 | SO | `0x99cdb0` | `0xc64` | `f6a232efc3c1c9c8` (1-entry KIT) |
| PERF_EXTISA_2 | SO | `0x99da40` | `0x1280` | `a86e957daf2a658f` (2-entry KIT) |
| PERF_EXTISA_3 | SO | `0x99ece0` | `0x55a0` | `e6a63e8c72164ae6` (9-entry; cptc/MX) |
| PERF_EXTISA_{0–3} | JSON | (4 blobs) | `0x20` | `ba7458eb…` (dummy; all 4 identical) |

> **GOTCHA — the MAVERICK Q7 SRAM anomaly.** Q7 PERF/TEST/DEBUG **IRAM are size 0**; the
> real Q7 compute code lives in the **SRAM** segment (`0x14480` / `0x15dc0` / `0x1d100`),
> with the reset vector at byte 0 of the SRAM blob. MAVERICK runs `Q7_POOL` **out of SRAM,
> not IRAM** — a v5-specific residence change (the [image-catalog § 3](./image-catalog-index.md)
> MAVERICK anomaly). Carve the Q7 code from the **SRAM** getter, not IRAM. [HIGH/OBSERVED]

All 15 spot-carved blobs hashed byte-for-byte against the report manifest this session. The
EXTISA `ET_DYN` form + the SRAM residence + the missing DEBUG/DKL are the v5 build's
fingerprints. [HIGH/OBSERVED]

### 4.2 Engine order — POOL is 3rd of 4 NX engines (ACT amputated)

The MAVERICK NX block is laid out **variant-major** (all PERF, then TEST). The PERF run is
`DVE @0x871300 → PE @0x8b3540 → POOL @0x8c12e0 → SP @0x8eeae0`; with **ACT amputated**
([the v5 ACT-fold](./maverick-act.md)) the v5 NX roster is **DVE → PE → POOL → SP**, and
POOL sits 3rd, keeping `engine_idx = 2`. Contiguity is byte-exact (computed this session):
`POOL PERF_IRAM 0x8c12e0 + 0xcf40 = 0x8ce220 == POOL PERF_DRAM`; `+0x25c0 = 0x8d07e0 ==` the
next cursor; `POOL TEST_DRAM 0x8ec320 + 0x27c0 = 0x8eeae0 == SP PERF`. [HIGH/OBSERVED]

---

## 5. The NX_POOL diff — DEBUG dropped, DGE subsystem GONE firmware-wide

The NX_POOL diff is the **opposite direction** from MARIANA_PLUS: where v4+ *added* the DGE
fast-path, v5 *removes* the entire DGE subsystem.

### 5.1 DEBUG dropped → no `'S:'` handler surface

MAVERICK NX_POOL ships **PERF/TEST/PROF only** — no DEBUG. Consequently the NX_POOL carves
carry **0 `'S:'` handler-dispatch strings** (re-grepped this session over all NX carves
concatenated). The named-handler roster lives in the DEBUG build, which POOL lacks on v5, so
the [MARIANA_PLUS § 5 handler-name diff](./mariana-plus-pool.md) (run on DEBUG DRAMs) is
**not directly reproducible**; the handler comparison resolves to the TEST-build
compiled-symbol surface. The dispatch **model** is unchanged — the `0x6099cb34` DRAM magic +
the `addx4`-indexed jump-through-DRAM-table dispatch are present. [HIGH/OBSERVED for the
absence; opcode-table rows **INFERRED** — see §5.3]

### 5.2 The DGE/reshape subsystem — DROPPED firmware-wide (POOL was its home)

POOL was the v4+ DGE fast-path's **home engine** (the SW-DGE backend runs on the Q7/POOL
cores; [MARIANA_PLUS § 2](./mariana-plus-pool.md) found all four fast-path strings on the
MARIANA_PLUS NX_POOL). On MAVERICK NX_POOL, **region-wide** (all NX carves concatenated,
re-grepped this session): [HIGH/OBSERVED]

| String | MARIANA_PLUS NX_POOL | MAVERICK NX_POOL | kind |
|---|:---:|:---:|---|
| `dge_decode_fast` | 1 (TEST_DRAM) | **0** | v4+ fast-path TU |
| `dge_reshape_memcopy_transpose_fast` | 1 | **0** | fused memcopy+transpose |
| `tensor_reshape_transpose_sb2sb` | 1 | **0** | SB→SB transpose kind |
| `wait_for_credit` | 1 | **0** | DMA credit wait |
| `dge_backend_rtl` | (MPLUS) | **0** | DGE RTL backend |
| `analyze_tensor_reshape` | (MPLUS) | **0** | the generic reshape analyzer |
| `dge_reshape` / `Setting up DGE` | (both gens) | **0** | pre-existing DGE machinery |
| `push REGWRITE` | (MARIANA-side) | **0** | the REGWRITE emit |

The **entire** DGE/reshape subsystem — the v4+ fast-path **and** the pre-existing DGE
machinery — is **gone** on MAVERICK NX_POOL (not even the "on both gens" `dge_reshape` /
`Setting up DGE` survive). The Q7 side correspondingly drops the rdma descriptor-gen logging
(§6.3). This is the v5 DGE re-architecture (to a HW DMA path) realized on the engine that
anchored the v4+ fast-path. [HIGH/OBSERVED for the string counts; the "re-architected to HW
DMA" reading INFERRED]

### 5.3 NX_POOL opcode space — NOT byte-resolved (honest FLIX gap)

The MAVERICK NX_POOL dispatch normalization (`addi a2,a2,-65`) + bound (`const16`-loaded,
not `movi a3,177`) are **FLIX-bundle-desynced** by the linear sweep (the documented
disassembler limit — the flat IRAM blobs carry no `.xt.prop` FLIX property table). No clean
177-entry DRAM jump table resolves at a fixed offset. The NX_POOL **opcode-space count** is
therefore **NOT byte-claimed** here — the same gap the [DVE page](./maverick-dve.md) flagged
for the per-row binding. The **model** (DRAM magic + `addx4` dispatch) is OBSERVED; the
exact entry count and per-opcode rows are **INFERRED**. The `op 0xf0` SEQ dispatch slot is
likewise unresolved, but the Q7 KIT confirms the bridge target set is intact (§7). [model
HIGH/OBSERVED; opcode count + table rows MED/INFERRED]

### 5.4 NX_POOL dtype — numeric only

NX_POOL carries only `NEURON_ISA_TPB_DTYPE_{UINT32, INT32, FP32}` (×2 each, the
`move.cpp` assertion) and **0** `FP8`/`INT4`/`SFP8`/`MXTENSOR`/`CPTC`/`QuantizeMx`/
`proc_4bit` strings (re-grepped this session). The new v5 dtype codes
(`FP8_EXP2 0x10` / `INT4 0x12` / `SFP8_E8 0x13` = E8M0) are **numeric** in the sequencer
decode path, not named NX strings — the same negative as
[DVE](./maverick-dve.md)/[PE](./maverick-pe.md)/[ACT](./maverick-act.md). [HIGH/OBSERVED]

---

## 6. The Q7_POOL compute — MX dequant + RNG RETAINED; rdma DROPPED

### 6.1 The MX dequant machinery — RETAINED byte-for-name

The MAVERICK Q7_POOL DEBUG DRAM carries the firmware's own `'P%i:'` dequant self-naming
strings (re-grepped this session): `proc_4bit_mx_8` (×1), `proc_4bit_non_mx` (×1),
`proc_6bit_non_mx` (×1), `TensorDequantize` (×2), `cptc_decode` (×2),
`Unimplemented dequant format` (×1) — the **identical** set to MARIANA_PLUS Q7 DEBUG DRAM.
The dequant-side MX (`op 0x7b → proc_4bit_mx_8`, grp8 block-of-8, 8:5, in-band scale; the
[MX Dequant model](../firmware/kernels/mx-dequant.md)) is **RETAINED unchanged in
routing/naming** on v5. The `op 0x7b` byte-pins via KIT idx16 (funcVA `0x50ec`, §2). [HIGH/
OBSERVED for the strings + KIT; the per-instruction kernel body is stripped `ET_DYN` and
INFERRED]

### 6.2 The RNG body — UNCHANGED (TIE + LFSR retained)

The Q7_POOL DEBUG DRAM `'P%i:'` RNG token set (re-grepped this session): `Xorwow` (×4),
`XorwowRng` (×1), `LfsrSetSeeds` (×1), `LfsrGetSeeds` (×1), `rand_algo` (×2),
`RandGetState` (×3), `RandSetState` (×3), `Xorwow(SW)` (×**0**) — the **same** TIE+LFSR
dual-algo set MARIANA (v4) introduced and MARIANA_PLUS retained. The `Xorwow(SW) → TIE+LFSR`
boundary was the **v4 arrival**; MAVERICK (v5) **retains** it byte-for-name. The RNG is
**not** a v5 POOL delta. (The `(TIE)` suffix in the firmware string is the HW-TIE path; the
HW-vs-SW distinction is not byte-recoverable from the string layer.) See
[RNG — LFSR + `rand_algo` Dispatch](../firmware/kernels/rng-lfsr-dispatch.md). [HIGH/OBSERVED]

### 6.3 The Q7-side v5 delta — rdma descriptor-gen logging DROPPED

The MAVERICK-vs-MARIANA_PLUS Q7 DEBUG DRAM string set-diff (this session): **28 strings
dropped** on MAVERICK (none added beyond binary noise), of which **25 are the
`rdma_desc_gen` / `rdma_desc_start`** (remote-DMA descriptor generation) subsystem +
`remote_copy.cpp` / `xt_addrs` / `shuffled_sbuf_swizzle`. **Zero** dequant/MX/proc/RNG/cptc
strings dropped. So the Q7-side v5 change is the **SW remote-DMA descriptor-gen logging
being gone** — the Q7 leg of the firmware-wide DGE/DMA re-architecture — while the **compute
kernels (dequant/MX/RNG/pooling) are retained**. [HIGH/OBSERVED counts; the "HW-DMA
re-architecture" reading INFERRED]

### 6.4 Q7 is a genuine compute core

The Q7 PERF_SRAM blob decodes (`ncore2gp`, exit 0) to a real windowed-ABI sequencer:
**154 `entry` / 174 `retw` / 340 `call8`** prologues this session — the vector IVP datapath
partly FLIX-desynced by the linear sweep, but unmistakably a real compute core, not a stub.
[HIGH/OBSERVED for the counts; the desynced vector spans INFERRED]

---

## 7. The 0xF0 ExtendedInst bridge — INTACT (Q7-proven)

The `0xF0` two-level escape `(opcode<<24)|(spec<<16)` registers on the Q7 side, unchanged.
**Q7 side (`EXTISA_0`):** the five `0xf0 + spec` rows (specs `0, 1, 2, 4, 3`; KIT idx 6–10)
are **byte-identical-key** to MARIANA_PLUS — `spec 4`/`spec 3` route to the Rand band (the
RNG dispatch). The two-level escape is structurally invariant; the spec byte sub-selects
exactly one of the five rows. **SEQ side (`NX_POOL`):** with DEBUG dropped, the
`S: ExtendedInst` string is absent, and the `op 0xf0` NX dispatch slot is **not byte-resolved**
(FLIX desync) — but the Q7 KIT confirms the bridge **target set** is intact. POOL remains the
**only** engine with **both** the SEQ `0xf0` bridge **and** a Q7 compute core. See
[POOL Extended-Opcode (0xF0) Dispatch](../firmware/pool/pool-ext-0xf0.md). [Q7 side
HIGH/OBSERVED; SEQ slot MED/INFERRED gap]

---

## 8. PROF — REUSED VERBATIM (the POOL-vs-DVE contrast)

Both NX_POOL profiling blobs are `cmp -s` clean against the MARIANA_PLUS POOL tables this
session: [HIGH/OBSERVED]

- **PROF_CAM** `0951b326f4a40ccd` (`0x400`) — **byte-identical** to MARIANA_PLUS (and
  MARIANA) POOL. The blob carries a **single** nonzero 4-byte word (at offset `0x8`) in
  `0x400` bytes — the **disarmed** CAM (a stray `opcode 0 / mask 0` sentinel record, **0**
  effective armed records). POOL's per-engine HW-decode profiler is disarmed on **all** of
  v4/v4+/v5. [byte-identity + nonzero-word count HIGH/OBSERVED; the record-shape reading
  CARRIED from the [MARIANA_PLUS baseline](./mariana-plus-pool.md), same byte-identical blob]
- **PROF_TABLE** `534f2239b9e76d1c` (`0x2000`) — **byte-identical** to MARIANA_PLUS.

> **NOTE — the KEY POOL-vs-DVE contrast.** MAVERICK **DVE** PROF was **re-authored** (CAM
> `dbff2b84`, 53 armed, +10/−3 — [DVE page](./maverick-dve.md)). MAVERICK **POOL** PROF is
> **reused verbatim**. The resolution: the [ACT page](./maverick-act.md)'s "PROF
> re-authored per-engine on v5" generalization holds for the **armed** engines (DVE); the
> **disarmed** POOL PROF has nothing to re-author and carries forward byte-identical. PROF
> re-authoring is **opcode-arming-driven**, not a blanket per-engine v5 rebuild. [HIGH/
> OBSERVED — sha256 match]

---

## 9. The cross-gen size/sha table + build independence

**Every MAVERICK POOL image is SMALLER than MARIANA_PLUS** (the **inverse** of the v4 → v4+
growth — the DGE/rdma drop + the independent v5 rebuild shrink). Of 20 real images, **3 are
byte-identical** (NX PROF_CAM, NX PROF_TABLE, EXTISA JSON-dummy), **17 differ**, all smaller:
[HIGH/OBSERVED]

| IMAGE | MPLUS sz / sha | MAV sz / sha | ΔSize | identical? |
|---|---|---|---|---|
| NX PERF_IRAM | `0x17bc0` / `582c246b` | `0xcf40` / `150437ba` | **`−0xac80`** | NO (v5 re-build) |
| NX PERF_DRAM | `0x32a0` / `fbe207ed` | `0x25c0` / `1efa8f0a` | `−0xce0` | NO |
| NX TEST_IRAM | `0x176a0` / `bf84aacb` | `0xd560` / `768239aa` | **`−0xa140`** | NO |
| NX TEST_DRAM | `0x3620` / `9c605daf` | `0x27c0` / `a3ce05f8` | `−0xe60` | NO |
| **NX PROF_CAM** | `0x400` / `0951b326` | `0x400` / `0951b326` | `+0x0` | **YES** (verbatim) |
| **NX PROF_TABLE** | `0x2000` / `534f2239` | `0x2000` / `534f2239` | `+0x0` | **YES** (verbatim) |
| Q7 code PERF (SRAM) | `0x164e0` / `0c761dba` | `0x14480` / `bb5b3af2` | `−0x2060` | NO (was IRAM) |
| Q7 PERF_DRAM | `0x13180` / `c448c5ff` | `0x13000` / `360bd0aa` | `−0x180` | NO |
| Q7 code TEST (SRAM) | `0x17e80` / `e8a32b3f` | `0x15dc0` / `15824326` | `−0x20c0` | NO |
| Q7 code DEBUG (SRAM) | `0x1ef00` / `1c9c15bc` | `0x1d100` / `6bcbbdd3` | `−0x1e00` | NO |
| Q7 DEBUG_DRAM | `0x15d80` / `295fae9c` | `0x15480` / `a34dc924` | `−0x900` | NO |
| Q7 EXTISA_0_SO | `0xa260` / `9f2ce049` | `0x7fb0` / `a92c8ba0` | **`−0x22b0`** | NO (stripped DYN) |
| Q7 EXTISA_3_SO | `0x6974` / `8477ff26` | `0x55a0` / `e6a63e8c` | `−0x13d4` | NO |

**Build independence:** NX_POOL PERF_IRAM = **7.0 %** 16-B positional block-similarity vs
MARIANA_PLUS (231/3316); Q7 PERF code = **4.5 %** (232/5192). Both fully independent v5
builds (diverge at byte 0x1 = the reset immediate). The "Q7 byte-identical cross-gen"
property of v4/v4+ **does not carry to v5**. [HIGH/OBSERVED]

---

## 10. engine_idx = 2 + the dual-core model (CONFIRMED)

The shipped clean ISA header `neuron_maverick_arch_isa/tpb/aws_neuron_isa_tpb_common.h`
fixes the engine enum (read directly this session): [HIGH/OBSERVED]

```c
typedef enum NEURON_ISA_TPB_NEURON_ENGINE {
    NEURON_ISA_TPB_NEURON_ENGINE_PE     = 0,
    NEURON_ISA_TPB_NEURON_ENGINE_ACT    = 1,   /* logical; image-absent (ACT-fold) */
    NEURON_ISA_TPB_NEURON_ENGINE_POOL   = 2,
    NEURON_ISA_TPB_NEURON_ENGINE_DVE    = 3,
    NEURON_ISA_TPB_NEURON_ENGINE_TPB_SP = 4,
    NEURON_ISA_TPB_NEURON_ENGINE_TOP_SP = 5,
} NEURON_ISA_PACKED NEURON_ISA_TPB_NEURON_ENGINE;
```

`POOL = engine_idx 2` confirmed (the enum unchanged; ACT=1 still **logical** but
image-absent per the [ACT-fold](./maverick-act.md)). The dual-core model is **intact** on
MAVERICK: `NX_POOL` (SEQ, `'S:'`-class, `0x6099cb34` DRAM magic + `addx4`-indexed dispatch) +
`Q7_POOL` (`kernel_info_table` compute, `'P%i:'`-class) bridged by the `0xF0` escape. The Q7
DEBUG DRAM carries `P%i: Starting pooling engine: %i` / `Stopping pooling engine` +
`P%i: engine_base_addr=%llx … engine_idx=%u` — `engine_idx` still boot-computed. POOL is the
**only** engine of the v5 four-NX roster (DVE/PE/POOL/SP) that ships two cores.
`NEURON_CORE_VERSION` tops at V5; `coretype 37` / `arch_id 36` (INFERRED — no NCFW v5 image).
[HIGH/OBSERVED enum + strings; `arch_id 36` MED/INFERRED]

---

## 11. Adversarial self-verification

Five strongest claims, re-challenged against the binary this session:

1. **The KIT key-set identity / NO new rows (the headline).** *Challenge:* could a v5 MX op
   have been added as a new `kernel_info_table` row, or a routing silently re-pointed?
   *Re-verify:* all four EXTISA KITs parsed byte-exact (17/1/2/9) — the `(opcode,spec)` key
   set diffed vs MARIANA_PLUS is `ADDED = []`, `REMOVED = []` for **every** table; the union
   of all POOL KIT opcodes is `{0x41,0x45,0x46,0x47,0x51,0x52,0x7b,0x7c,0x7d,0x7e,0xbe,0xe4,
   0xf0,0xf2}`; `op 0x7b` dequant present at idx16 (funcVA `0x50ec`). **HOLDS.** [HIGH/
   OBSERVED]
2. **The v5 MX opcodes are NOT on POOL.** *Challenge:* are `0x09`/`0x0A`/`0xe3` hiding in a
   table I didn't parse? *Re-verify:* the shipped maverick enum defines `LDWEIGHTS_MX 0x09`,
   `MATMUL_MX 0x0A`, `QUANTIZE_MX 0xe3`; each checked against the union of **all four** POOL
   KITs → `present? False` for each. They are PE (`0x09`/`0x0A`) / DVE (`0xe3`) opcodes.
   **HOLDS.** [HIGH/OBSERVED]
3. **The new `j 0x1d8` / `j 0x1e4` reset geometry.** *Challenge:* could a later variant hide
   a different shift, or is this the v4 `+0x1c`? *Re-verify:* NX head `06 75 00` → `j 0x1d8`
   / `86 76 00` → `j 0x1e4`, trampoline `const16 a0,0x94 ; jx a0` → `enter_run @0x94` (the
   −0x20 v5 shift, == DVE, NOT the v4 +0x1c); Q7 SRAM head `06 78 00` → `j 0x1e4` (the −0x1c
   Q7 shift — the Q7 core moves for the first time); both decoded `ncore2gp` exit 0; first NX
   divergence at byte 0x1. **HOLDS.** [HIGH/OBSERVED]
4. **The RNG / dequant retention.** *Challenge:* did v5 swap the RNG algo or drop the MX
   dequant? *Re-verify:* Q7 DEBUG DRAM `proc_4bit_mx_8`/`proc_6bit_non_mx`/`cptc_decode`/
   `TensorDequantize` present (== MARIANA_PLUS); `XorwowRng`/`LfsrSetSeeds`/`rand_algo`/
   `Rand{Get,Set}State` present, `Xorwow(SW)` = 0 — byte-for-name unchanged; `op 0x7b` KIT
   row present. **HOLDS.** [HIGH/OBSERVED]
5. **The structural re-build (independent v5 build).** *Challenge:* is this a recompile of
   MARIANA_PLUS, not a new generation? *Re-verify:* every image smaller (NX `−0xac80`, Q7
   `−0x2060`, EXTISA `−0x22b0`); NX block-similarity 7.0 %, Q7 4.5 %; EXTISA `ET_DYN`
   stripped vs `ET_EXEC`; DGE subsystem 0 region-wide; DEBUG/DKL dropped; Q7 in SRAM; 0 `.a`
   members. **HOLDS** (an independent v5 build, not a recompile). [HIGH/OBSERVED]

---

## 12. Honesty ledger

**HIGH / OBSERVED (reproduced this session):** container sha `b7c67e89…632fc329b`; 30
getters (10 NX + 20 Q7), every `(img-ptr,size)` instruction-exact, == catalog 509–546; 0
MAVERICK `.a` members. 15 carve hashes match the manifest (NX PERF_IRAM `150437ba`, Q7 code
SRAM `bb5b3af2`, EXTISA_0 `a92c8ba0`). NX reset `06 75 00` → `j 0x1d8` / `j 0x1e4` →
`enter_run @0x94` (base `0x1330`); Q7 SRAM reset `06 78 00` → `j 0x1e4` / `j 0x1f0`; both
`ncore2gp` exit 0; DRAM magic `0x6099cb34`. **All four EXTISA KITs parsed byte-exact
(17/1/2/9), key set byte-for-key == MARIANA_PLUS (`ADDED=[]`/`REMOVED=[]` each); `op 0x7b`
idx16 funcVA `0x50ec`; v5 MX `0x09`/`0x0A`/`0xe3` absent from all POOL KITs.** EXTISA stripped
`ET_DYN` (no `.xt.prop`/symtab) vs MPLUS `ET_EXEC`; funcVA deltas variable
(`0xfffad4..0x1000074`, recompiled not relocated). NX DGE subsystem 0 of 9 strings region-wide;
0 `S:` strings; NX dtype UINT32/INT32/FP32 only. Q7 DEBUG DRAM dequant + RNG strings present
(== MPLUS); 25 rdma desc-gen strings dropped; engine enum POOL=2; `Starting pooling engine`
present; Q7 SRAM 154 entry/174 retw/340 call8. PROF CAM `0951b326` + TABLE `534f2239`
byte-identical to MARIANA_PLUS (`cmp -s` clean; CAM 1 nonzero word). Contiguity DVE→PE→POOL→SP
byte-exact. Build independence 7.0 % / 4.5 %; every image smaller.

**MED / INFERRED (the v5 WALL):** the NX_POOL opcode-space **count** and per-opcode table
rows (FLIX-desynced; only the dispatch model is OBSERVED); the `op 0xf0` SEQ dispatch slot
(FLIX desync; the Q7 KIT confirms the bridge target set); the MAVERICK kernel bodies
(stripped `ET_DYN` EXTISA — `op 0x7b` presence pinned by the KIT only, the proc body not
recoverable); the "DGE/rdma re-architected to a HW DMA path" reading (string-presence diff
only); the PROF disarmed record-shape (CARRIED from the byte-identical baseline blob);
`arch_id 36` (no NCFW v5 image); the Q7 `enter_run` exact target (the `j 0x1e4` + −0x1c shift
are HIGH; the precise enter VA MED).

**LOW / NOT CLAIMED:** which silicon/runtime selects MAVERICK vs MARIANA_PLUS (the front lib
has 0 MAVERICK refs — shipping vs pre-release out of scope); the exact MAVERICK Q7
dequant/RNG kernel bodies; the exact rdma re-architecture mechanics.

---

## 13. Cross-references

- [MARIANA_PLUS × POOL](./mariana-plus-pool.md) — **the diff baseline** (the dual-core
  model, the SEQ hub, the KIT format, the `0xF0` reconciliation, the v4 RNG arrival, the DGE
  fast-path POOL anchored).
- [MAVERICK × DVE](./maverick-dve.md) — the ACT-fold + `QUANTIZE_MX 0xe3` (the MX expansion's
  DVE home; the re-authored PROF contrast).
- [MAVERICK × PE](./maverick-pe.md) — `MATMUL_MX 0x0A` / `LDWEIGHTS_MX 0x09` (the MX
  expansion's PE home; the DGE-dropped pattern).
- [MAVERICK × ACT](./maverick-act.md) — the v5 anchor (real gen-step, new reset, ACT folded,
  DGE dropped).
- [Cross-Gen kernel_info_table matrix](./cross-gen-kernel-info-matrix.md) — the per-gen KIT
  key-set comparison (POOL's `+0/−0` row).
- [MX Dequant (op 0x7b)](../firmware/kernels/mx-dequant.md) — the retained `proc_4bit_mx_8`
  in-band block-of-8 dequant.
- [kernel_info_table Layout](../firmware/pool/kernel-info-table.md) — the 8-byte record /
  packed-key format.
- [RNG — LFSR + `rand_algo` Dispatch](../firmware/kernels/rng-lfsr-dispatch.md) — the
  retained TIE+LFSR RNG body.
- [POOL Extended-Opcode (0xF0) Dispatch](../firmware/pool/pool-ext-0xf0.md) — the spec
  sub-dispatch.
- [PROF CAM/TABLE Formats](./prof-cam-table-formats.md) — the byte-identical disarmed
  profiler.
- [Image Catalog Index](./image-catalog-index.md) — the getter map (rows 509–546).
