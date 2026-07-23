# MARIANA × PE image

The **MARIANA × PE** firmware image is the **NC-v4** (Mariana, v4) build of the **Processing-Element**
sequencer (`engine_idx = 0`, systolic matmul / TensorE) — the same `cayman/seq/` NX-class
SEQ-dispatch chassis as [CAYMAN × PE](./cayman-pe.md), recompiled for the v4 generation with **+4
handlers** and **+4 dispatch opcodes** linked in. This page is a **cross-generation diff page**: it
carves the 14 MARIANA PE image variants byte-exact from `libnrtucode_internal.so`, then diffs every
delta against the committed [CAYMAN × PE](./cayman-pe.md) baseline — the handler-set delta, the
opcode-space / dispatch / dtype / PROF deltas, the reset-vector shift, and the cross-gen size/sha256
deltas. The unchanged matmul core (the SBUF→array→PSUM dataflow, the 5 retained handlers, the IVP MAC
datapath) is **not re-derived** here; [CAYMAN × PE](./cayman-pe.md) is its byte-pinned ground truth.

**The headline fact this page proves at the byte level: MARIANA (v4) is where `PeManageSeed`
(`0x08`) and the MX matmul kernels (`LdweightsMX 0x09`, `MatmulMX 0x0A`) FIRST SHIP.**
[CAYMAN × PE §1.5](./cayman-pe.md) exhaustively grep-confirmed those tokens **absent** on every
CAYMAN PE variant (0 hits). This page shows them **present** on the MARIANA PE image — with the
`PeManageSeed(LOAD)` / `PeManageSeed(SAVE)` mode logs and the `micro-op : LdWeight` / `micro-op :
Matmul : is_load=%d` orchestration strings decoded out of the DEBUG DRAM. So the FW-66
`PeManageSeed` PSUM stochastic-rounding seed manager and the FP4/MX matmul path are a **v4-generation
PE feature, not an NC-v3 one** — the firmware-image boundary the [PE Matrix-Multiply
Path](../firmware/kernels/pe-matmul.md) per-gen table records.

Confidence/evidence tags follow the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. Every device fact is byte-pinned to a carve from
`libnrtucode_internal.so` (sha256 `b7c67e89…632fc329b`) and decoded with the shipped `ncore2gp`
`xtensa-elf-objdump`; both the CAYMAN baseline and the MARIANA images are carved for an
apples-to-apples diff (all 5 CAYMAN anchors + all 8 MARIANA carves reproduce exact). The micro-op
operand-struct semantics and the MX block model are CARRIED from the kernel pages cited inline.
The page default is `[HIGH/OBSERVED]`; claims that depart from it carry an explicit tag.

> **NOTE — the objects used.** Container:
> `…/custom_op/c10/lib/libnrtucode_internal.so` (sha256
> `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b`, ELF64 x86-64 DYN, **not
> stripped**). First R `LOAD` is the identity map (`off 0x0 == vaddr 0x0`, `filesz 0x9af194`), so each
> `<NAME>.data` accessor address is **simultaneously** the `.rodata` VA and the file offset of its
> blob — carve = `so[ptr : ptr+size]`. **IRAM file-offset == device IRAM VA** (reset vector at byte 0);
> **DRAM string-file-offset == device DRAM VA − `0x80000`**. Disassembler:
> `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump`
> (GNU Binutils 2.34.20200201, `XTENSA_CORE=ncore2gp`, ConfigName `Xm_ncore2gp`, uarch Cairo,
> Xtensa24, RI-2022.9, `TargetHWVersion=NX1.1.4`, `IsaMaxInstructionSize=32` FLIX/VLIW).

---

## 1. The headline diff (CAYMAN → MARIANA, one table)

This is the whole page in one table. Each row is byte-pinned in the sections that follow; the
left column is the committed [CAYMAN × PE](./cayman-pe.md) baseline, the right column is this image.

| property | CAYMAN PE (NC-v3 baseline) | MARIANA PE (NC-v4, **this image**) | Δ |
|---|---|---|---|
| packaging | flat IRAM/DRAM segments | flat IRAM/DRAM segments | same |
| reset vector | `06 76 00 00` → `j 0x1dc` | `06 7d 00 00` → `j 0x1f8` | **+0x1c** |
| 2nd vector | `86 77 00 00` → `j 0x1e8` → `halt 0` | `86 7e 00 00` → `j 0x204` → `halt 0` | **+0x1c** |
| boot stub body @`0xc` | `a0 71 69 80 …` | `a0 71 69 80 …` (byte-identical to MARIANA DVE) | only inner-`j` byte differs vs CAYMAN |
| dispatch model | RAW-opcode compare-chain (DEBUG) + table @`0x80814` | RAW-opcode compare-chain (DEBUG) + table @`0x80814` | same (**no** normalization, unlike DVE) |
| real opcode count | 25 | **29** | **+4** |
| new opcodes | — | `0x08`, `0x09`, `0x0A`, `0xe4` | **+4 / −0** |
| distinct DEBUG handlers | **24** | **28** | **+4** |
| matmul handlers | 5 (`Ldweights`/`Matmul`/`MatmulSparse`/`LdTags`/`PeRegWrite`) | 5 **+ 4 NEW** | **+4** |
| **`PeManageSeed` (`0x08`)** | **ABSENT** (0 grep hits) | **PRESENT** (`(LOAD)`/`(SAVE)` + micro-op logs) | **FIRST SHIP** |
| `LdweightsMX` (`0x09`) | ABSENT (0) | PRESENT (`SMX1_LW_STRUCT`) | **FIRST SHIP** |
| `MatmulMX` (`0x0A`) | ABSENT (0) | PRESENT (`SMX1D3_MM_STRUCT`, glued `XS:`) | **FIRST SHIP** |
| `ConvLutLoad` (`0xe4`) | ABSENT (0; POOL-only on CAYMAN) | PRESENT (migrated onto PE) | **+1 migration** |
| `S:`-`Dispatch opcode` log | DRAM `0x80868` (3 sites) | DRAM `0x80858` (3 sites) | relocated −0x10 |
| log helper | IRAM `0x154d4` | IRAM `0x14f54` | relocated |
| PROF_CAM | shared `8fd7e422` (47 records, **all 4 NX engines**) | **per-engine** `43475cec` (22 records, PE-only) | **de-shared + pruned 47→22** |
| PROF_TABLE | `ce761f81` (header `0x0201`, 150 nz words) | `d93b723e` (header **zeroed**, 59 nz words) | re-preallocated |
| PERF IRAM size | `0x159e0` | `0x12ce0` | **−0x2d00 (~−13%)** |
| DEBUG IRAM size | `0x19180` | `0x18c20` | −0x560 |
| DEBUG DRAM size | `0x6220` | `0x6400` | +0x1e0 (new strings) |
| source tree | `cayman/seq/src/…` | `cayman/seq/src/…` **+ `addr_bits.hpp`** | +1 file |
| gen self-name | `S: BEGIN on cayman` | `S: BEGIN on mariana` | gen tag |
| mariana-4062 errata log | (n/a) | **absent** (DVE-specific, not on PE) | — |
| engine model | SEQ ASCII-dispatch, `engine_idx=0` | SEQ ASCII-dispatch, `engine_idx=0` | **unchanged** |

**Verdict.** A CAYMAN↔MARIANA PE swap is a **full recompile + opcode-space growth (+4) + 4 handlers +
re-armed/pruned/de-shared profiler + reset-vector relocation** — NOT a model change. The "same SEQ
engine, different handler subset" hypothesis holds across the generation gap. No MARIANA PE image is
byte-identical to its CAYMAN counterpart (8/8 distinct sha256, §8).

---

## 2. The PeManageSeed / MX first-ship proof (the headline, byte-pinned)

This is the decisive section. [CAYMAN × PE §1.5](./cayman-pe.md) reported **0** grep hits for
`ManageSeed` / `MX` / `seed` / `ConvLut` across all three CAYMAN PE DRAM variants. The same grep
over the MARIANA DEBUG DRAMs (`strings -n3 | rg -c`):

| token | MARIANA PE DEBUG DRAM | CAYMAN PE DEBUG DRAM |
|---|---:|---:|
| `ManageSeed` | **4** | 0 |
| `MX` | **2** | 0 |
| `ConvLut` | **1** | 0 |
| `LdweightsMX` | **1** | 0 |
| `MatmulMX` | **1** | 0 |

The exact MARIANA PE handler-name strings, read out of the carve (`grep -aob`):

```text
S: PeManageSeed(LOAD)                          ; PE_SEED_MODE LOAD_SEED=1
S: PeManageSeed(SAVE)                          ; PE_SEED_MODE SAVE_SEED=2
S: PeManageSeed : micro-op : LdWeight          ; seed-mgmt orchestrates a LdWeight pass
S: PeManageSeed : micro-op : Matmul : is_load=%d
S: ConvLutLoad                                 ; @ DRAM 0x823c0 (file 0x23c0)
S: Ldweights      S: LdweightsMX               ; 0x81ad0 / 0x82470
TS: Matmul        XS: MatmulMX                 ; 0x81af0 / 0x82490  (note glue prefixes)
VS: MatmulSparse                               ; 0x824d0          (note glue prefix)
```

The CAYMAN PE DEBUG DRAM, same grep, yields **only** `S: Ldweights`, `S: Matmul`, `S: MatmulSparse`
— no seed, no MX, no ConvLut.

> **QUIRK — the `TS:`/`XS:`/`VS:` string-pool glue trap is present on PE.** The matmul-handler names
> are glued to the tail byte of the previous pooled string: `TS: Matmul`, `XS: MatmulMX`,
> `VS: MatmulSparse`. A naïve `^S:`-anchored diff would falsely report `Matmul`/`MatmulMX`/
> `MatmulSparse` as **removed**. The correct method (same as MARIANA ACT/DVE) strips the 0–3 glue
> bytes before `S: ` and normalizes to the bare `OpName`. The glue-stripped set-diff gives the clean
> **+4 / −0**: MARIANA ADDS `{ConvLutLoad, LdweightsMX, MatmulMX, PeManageSeed}`, removes nothing.
>

**Handler DRAM-string offsets** (MARIANA PE DEBUG DRAM, `grep -aob`):
the 5 retained CAYMAN handlers relocated `+~0xC0` for the new strings; the 4 new tokens slot into
the pool.

| handler | opcode | MARIANA DRAM str (file off / VA) | CAYMAN DRAM str (VA) | status |
|---|---|---|---|---|
| `Ldweights` | `0x01` | `0x1ad0` / `0x81ad0` | `0x81a60` | retained |
| `Matmul` | `0x02` | `0x1af0` / `0x81af0` | `0x81a80` | retained |
| `PeRegWrite` | `0x03` | `0x1b10` / `0x81b10` | `0x81aa0` | retained |
| `LdTags` | `0x06` | `0x24b0` / `0x824b0` | `0x82350` | retained |
| `MatmulSparse` | `0x07` | `0x24d0` / `0x824d0` | `0x82370` | retained |
| **`PeManageSeed`** | **`0x08`** | `0x0871` / `0x80871` (+ `0x823e0` copy) | — | **NEW** |
| **`LdweightsMX`** | **`0x09`** | `0x2470` / `0x82470` | — | **NEW** |
| **`MatmulMX`** | **`0x0A`** | `0x2490` / `0x82490` | — | **NEW** |
| **`ConvLutLoad`** | **`0xe4`** | `0x23c0` / `0x823c0` | — | **NEW** |

Two `PeManageSeed` string copies exist: an early dispatch-path copy @`0x80871` and the
handler-name-cluster copy @`0x823e0`.

> **NOTE — what `PeManageSeed` manages (CARRIED, not re-derived here).** `PeManageSeed` (`0x08`,
> ISA enum 165, struct `S2S1D2_PE_SEED_STRUCT`) is the **PSUM fp32→bf16 stochastic-rounding RNG seed
> save/restore** micro-op — *not* a per-cell PE-array PRNG. `PE_SEED_MODE` = `NONE=0` /
> `LOAD_SEED=1` / `SAVE_SEED=2`, matching the `(LOAD)`/`(SAVE)` mode logs decoded above. The
> firmware's `micro-op : LdWeight` / `micro-op : Matmul : is_load=%d` strings show the seed manager
> *issues* a wrapped LdWeight/Matmul pass to plumb seed state through the array. The per-op operand
> bytes and the SR datapath are documented in
> [pe-matmul.md §5/§11](../firmware/kernels/pe-matmul.md); this page establishes only the **image-level
> presence boundary** (v4-first). The corresponding fp32→bf16 BF16 `out_dtype` drain and the
> `flags`/`seed_mode` operand byte are also added to `S3_LW`/`S3D3_MM` at v4 (CARRIED from
> [pe-matmul.md §3](../firmware/kernels/pe-matmul.md)). `[HIGH/OBSERVED presence; semantics CARRIED]`

---

## 3. The +4 opcode delta (RAW compare-chain, both dispatch sites)

MARIANA PE keeps the CAYMAN dispatch *form* exactly: a **RAW-opcode** segmented compare-chain in
DEBUG (NO `addi a2,a2,-N` normalization — unlike DVE's normalized `addx4`), a 17-entry sub-table @
DRAM `0x80814`, the `S: Dispatch opcode=0x%x` log, and the clean PERF table @ DRAM `0x80218`. The
only delta is **+4 opcodes**. The DEBUG compare-chain was decoded instruction-exact at **both** NX-mode
dispatch sites (HW-Decode ~`0x2936`, Sunda ~`0x2e2b`); both agree.

```text
; MARIANA PE DEBUG IRAM, HW-Decode site ~0x2936 (ncore2gp objdump):
  293f:  662202   bnei  a2, 2, 0x2945        ; opcode 0x02 Matmul
  2948:  663202   bnei  a2, 3, 0x294e        ; opcode 0x03 PeRegWrite
  2951:  666202   bnei  a2, 6, 0x2957        ; opcode 0x06 LdTags
  295a:  667202   bnei  a2, 7, 0x2960        ; opcode 0x07 MatmulSparse
  2963:  668202   bnei  a2, 8, 0x2969        ; opcode 0x08 PeManageSeed   <-- NEW (v4)
  ; opcode 0x09 LdweightsMX routes via the following movi.n a3,9 arm        <-- NEW (v4)
  2977:  669202   bnei  a2, 10, 0x297d       ; opcode 0x0a MatmulMX        <-- NEW (v4)
  2980:  32a09f   movi  a3, 159              ; opcode 0x9f
  298c..2a64: movi a3, {160..171, 176..179, 181, 184, 189}   ; 0xa0..0xab, 0xb0..b3, b5, b8, bd
  2a70:  32a0e4   movi  a3, 228              ; opcode 0xe4 ConvLutLoad     <-- NEW (v4)
```

**Opcode rosters (instruction-exact):**

* **CAYMAN PE (25):** `0x1 2 3 6 7` · `0x9f a0 a1 a2 a3 a4 a5 a6 a7 a8 a9 aa ab` · `0xb0 b1 b2 b3 b5 b8 bd`
* **MARIANA PE (29):** CAYMAN's 25 **+ `{0x8, 0x9, 0xa, 0xe4}`** (+4, −0)

Each new opcode maps to a distinct trampoline in the contiguous `0x38xx` cluster (`0x8→0x38fd`,
`0x9→0x38e5`, `0xa→0x38ed`, `0xe4→0x38f5`) — real dispatch arms, not the Bad-Opcode default. The
byte-decoded per-opcode trampoline targets `{0x08→0x2bc7, 0x09→0x2baf, 0x0a→0x2bb7}` are CARRIED
from [pe-matmul.md §2.1](../firmware/kernels/pe-matmul.md). `[HIGH counts; per-opcode→name binding
CARRIED-MED — the 0x38xx trampolines partly FLIX-bundle in the linear sweep]`

> **GOTCHA — three independent signals agree.** The `+4 new dispatch opcodes`
> (`{0x8,0x9,0xa,0xe4}`) == the `+4 new handler names`
> (`{PeManageSeed, LdweightsMX, MatmulMX, ConvLutLoad}`) == the `+4` real-opcode count delta (29−25).
> The three counts coincide from independent evidence (the compare-chain arms, the glue-stripped string
> diff, the opcode census).

**Dispatch infra, gen-stable / relocated** (decoded):

* sub-table @ DRAM `0x80814`: **17** in-range IRAM trampolines in both gens (MARIANA pool starts
  `0x3d78`, CAYMAN `0x3c44` — relocated, same count).
* PERF clean table base @ DRAM `0x80218`: gen-stable (MARIANA `const16 a4,0x218`; the `addx4/l32i/jx`
  core decodes, byte-exact per-opcode PERF rows MED under the FLIX desync).
* `S: Dispatch opcode=0x%x` log @ DRAM `0x80858` (3 IRAM sites `0x292e/0x2e25/0x3651`), relocated −0x10
  from CAYMAN's `0x80868`; log helper at IRAM `0x14f54` (CAYMAN `0x154d4`).
* dual `S: NX in HW Decode mode` / `S: NX in Sunda mode` paths and the `ErrorHandler` arms
  (`Bad Opcode(0x%x)` / `Illegal Instruction` / `FP Error` / `Int Div Zero`) present, identical fault
  classes; source `…/cayman/seq/src/handlers/exception_handler.hpp`.

---

## 4. The NEW PeManageSeed + MX mechanisms (C pseudocode)

The unchanged matmul core — boot → fetch → the 5 retained handlers, the SBUF→array→PSUM dataflow, the
IVP widening-MAC datapath — is documented byte-exact in [CAYMAN × PE §4/§6/§9](./cayman-pe.md) and is
**not repeated** here. Below is **only** what v4 adds: the seed manager and the MX matmul path, as the
new arms of the same `switch`. Opcodes/strings are byte-pinned; the operand-struct and
PSUM-SR datapath fields are CARRIED from [pe-matmul.md](../firmware/kernels/pe-matmul.md).

```c
// ---------------------------------------------------------------------------
// MARIANA (v4) PE dispatch — the +4 NEW arms over the CAYMAN compute subset.
// Unchanged: boot jx a0 -> enter_run @ IRAM 0x90; the RAW-opcode compare-chain;
// the 5 retained matmul handlers (Ldweights/Matmul/MatmulSparse/LdTags/PeRegWrite)
// — see cayman-pe.md. The systolic array, PSUM banks, and SR-RNG are SILICON.
// ---------------------------------------------------------------------------

enum pe_seed_mode { PE_SEED_NONE = 0, PE_SEED_LOAD = 1, PE_SEED_SAVE = 2 };

void pe_dispatch_v4(instr_t *ins, uint8_t op) {           // raw op byte, no normalisation
    log("S: Dispatch opcode=0x%x", op);                   // DRAM 0x80858 (DEBUG only)
    switch (op) {
    // ----- CAYMAN compute subset (retained verbatim; see cayman-pe.md) -----
    case 0x01: h_ldweights(ins);     break;               // S: Ldweights    DRAM 0x81ad0
    case 0x02: h_matmul(ins);        break;               // TS: Matmul      DRAM 0x81af0
    case 0x03: h_pe_reg_write(ins);  break;               // S: PeRegWrite   DRAM 0x81b10
    case 0x06: h_ldtags(ins);        break;               // S: LdTags       DRAM 0x824b0
    case 0x07: h_matmul_sparse(ins); break;               // VS: MatmulSparse DRAM 0x824d0

    // ===================  THE 4 NEW v4 ARMS  ============================
    case 0x08: h_pe_manage_seed(ins); break;              // S: PeManageSeed  DRAM 0x80871/0x823e0
    case 0x09: h_ldweights_mx(ins);   break;              // S: LdweightsMX   DRAM 0x82470
    case 0x0A: h_matmul_mx(ins);      break;              // XS: MatmulMX     DRAM 0x82490
    case 0xe4: h_conv_lut_load(ins);  break;              // S: ConvLutLoad   DRAM 0x823c0
    // ====================================================================

    default:   seq_core_dispatch(ins, op);                // shared 18-handler SEQ core + EngineNop
                                                          // miss -> ErrorHandler "Bad Opcode(0x%x)"
    }
}

// ---- 0x08 PeManageSeed: PSUM fp32->bf16 stochastic-rounding seed save/restore ----
//      struct S2S1D2_PE_SEED_STRUCT (ISA enum 165, v4+). NOT a per-cell PE PRNG.
void h_pe_manage_seed(instr_t *ins) {                     // operand layout: pe-matmul.md §5
    struct s2s1d2_pe_seed *p = decode_seed(ins);
    switch (p->mode) {                                    // PE_SEED_MODE field
    case PE_SEED_SAVE:                                    // "S: PeManageSeed(SAVE)"
        // snapshot the array's SR-RNG state out to SBUF (S source banks) so a
        // later pass can restore deterministic rounding.
        pe_seed_save(p->dst /*S banks*/);
        break;
    case PE_SEED_LOAD:                                    // "S: PeManageSeed(LOAD)"
        // restore SR-RNG seeds from SBUF into the array; the manager then issues
        // a *wrapped* LdWeight/Matmul pass to plumb seeds through:
        //   "S: PeManageSeed : micro-op : LdWeight"
        //   "S: PeManageSeed : micro-op : Matmul : is_load=%d"
        pe_seed_load(p->src /*S banks*/);
        pe_issue_wrapped_pass(p, /*is_load=*/1);
        break;
    case PE_SEED_NONE:                                    // bypass — no seed plumbing
        break;
    }
}

// ---- 0x09/0x0A: the MX (microscaling) matmul path — separate v4 ops -------------
//      out-of-band scale_addr tensor + explicit E8M0 (SFP8_E8) shared-exponent scale.
//      (distinct from POOL's in-band proc_4bit_mx_8 block-of-8 dequant — see NOTE.)
void h_ldweights_mx(instr_t *ins) {                       // struct SMX1_LW_STRUCT (enum 166)
    struct smx1_lw *p = decode_mx_lw(ins);                // FP4_E2M1 weight codes + scale_addr
    pe_load_weights_mx(p->src_codes, p->scale_addr);      // load FP4 tile + per-block E8M0 scale
}
void h_matmul_mx(instr_t *ins) {                          // struct SMX1D3_MM_STRUCT (enum 167)
    struct smx1d3_mm *p = decode_mx_mm(ins);
    // stream moving acts through the MX-scaled stationary tile; the per-block
    // shared exponent rescales the partial products before the FP32 PSUM accum.
    pe_stream_matmul_mx(p);                               // dst = (scale * fp4_w).T @ moving -> PSUM
}

// ---- 0xe4: ConvLutLoad — load a conv lookup-table (migrated POOL->PE at v4) ------
void h_conv_lut_load(instr_t *ins) { pe_load_conv_lut(ins); }  // S: ConvLutLoad
```

> **NOTE — two different "microscaling" paths; do not conflate.** The PE `LdweightsMX 0x09` /
> `MatmulMX 0x0A` path uses an **out-of-band** `scale_addr` tensor and an explicit **E8M0**
> (`SFP8_E8`) shared-exponent scale (the OCP-MX matmul plumbing). The POOL-engine
> [`proc_4bit_mx_8` dequant](../firmware/kernels/mx-dequant.md) is a **different** mechanism — an
> **in-band** per-block scale (group-of-8) applied as a vector multiply on the dequant side. Both are
> "microscaling"; their block/scale plumbing differs. This page only establishes that the PE
> out-of-band MX matmul ops *first appear* at MARIANA. `[HIGH/OBSERVED ops present; the scale-model
> linkage CARRIED from mx-dequant.md §6 / pe-matmul.md]`

---

## 5. DTYPE delta (does v4's FP4/MX surface in PE strings?)

**No — and that is the expected negative.** `FP4` / `CPTC` / `MXTENSOR` / `SFP8` / `fp8_e` appear as
named strings in **0** of the 8 MARIANA PE blobs (grep over all blobs). The only named dtype
constants are `NEURON_ISA_TPB_DTYPE_{UINT32,INT32,FP32}` (the `move.cpp` / `alu_op.cpp` assertions) —
**byte-identical to CAYMAN**. The new dtype codes are numeric in the decode path, not named-string
asserts; same negative result MARIANA ACT/DVE showed.

The FP4/MX dtype expansion's PE-engine footprint is therefore **not** a dtype string but the **new
handler pair `LdweightsMX` + `MatmulMX`** (§2). This is the PE analogue of DVE's `QuantizeMx`
([MARIANA × DVE](./mariana-dve.md)): the matmul array gains an MX-format weight-load + matmul path
this generation, while the activation-side MX quantize lives on DVE. `[HIGH/OBSERVED handlers;
MX↔FP4/MX linkage INFERRED-HIGH from the name + the
[MX dequant scale model](../firmware/kernels/mx-dequant.md)]`

---

## 6. PROF re-armed, now ENGINE-SPECIFIC, and leaner

The single biggest *structural* PROF change is that **the generic-shared-CAM property did not survive
the generation**. On CAYMAN, PROF_CAM was byte-identical across all 4 NX engines (one shared
`8fd7e422`, [CAYMAN × PE §7](./cayman-pe.md)). On MARIANA, PROF_CAM is **per-engine** — carved and
sha-compared:

| PROF_CAM | sha256 | armed records |
|---|---|---:|
| CAYMAN (shared, all 4 NX) | `8fd7e422…` | 47 |
| MARIANA **PE** | **`43475cec…`** | **22** |
| MARIANA DVE | `ca588683…` | — |
| MARIANA ACT | `326bc0dd…` | — |

All four distinct (PE ≠ DVE ≠ ACT ≠ CAYMAN-shared) — proven by sha256, not inference. The MARIANA PE
CAM is **re-armed AND pruned to PE's own opcode mix** (16-byte `{opcode,mask,enable,rsvd}` records,
`enable==1`): `[HIGH/OBSERVED bytes; "arms these" reading INFERRED-HIGH from the record shape]`

* MARIANA PE arms `{0x0, 1, 2, 3, 6, 7, 9, 0xa, 0x9f, 0xa0–a3, a5, a7–ab, b1, b2, 0xe4}` — the PE
  matmul block + control core + the new opcodes' overlap (`0x9`, `0xa`, `0xe4`).
* It **drops** the other engines' opcodes the generic CAYMAN CAM carried (`0x21–0x68`, `a4`, `a6`,
  `b8`, `bb`, `bd`).
* **No 9-bit opcodes** — PE's opcode space stayed 8-bit (unlike DVE's `0x1e3`/mask `0x1ff`); PE added
  only 4 opcodes, all 8-bit.

**PROF_TABLE** (`0x2000`): MARIANA PE `d93b723e` ≠ CAYMAN PE `ce761f81` ≠ MARIANA DVE `d72b339f`. The
MARIANA PE header is **zeroed** (`00 00 00 00 …`, **59** nonzero words); CAYMAN carried header
`0x00000201` + `0x26000010` + the `` `acofglm`` descriptor (**150** nonzero words). Re-preallocated,
same pattern as DVE. Both tables keep their size (`0x400` / `0x2000`); the field schema is structure-
level only (MED). `[HIGH/OBSERVED bytes; schema MED]`

---

## 7. Carve provenance + flat-image geometry

The 14 getters (`nm libnrtucode_internal.so | rg 'MARIANA_NX_PE_.*_get$' | rg -v PLUS` → exactly 14)
match the catalog ([image-catalog-index.md](./image-catalog-index.md), MARIANA NX_PE rows 313–326)
exactly. 8 carry real bytes; 6 are zero-size SRAM/EXTRAM boundary cursors (`movq $0x0`; `lea` →
`MARIANA_NX_POOL_<MODE>_IRAM` — PE is the last NX engine before POOL, runs entirely from IRAM+DRAM, as
on CAYMAN). Engine ordering **ACT→DVE→PE→POOL** is proven by contiguity arithmetic: DVE PERF_DRAM ends
@`0x335ca0` == PE PERF_IRAM start; PE PERF_DRAM ends @`0x34b720` == POOL PERF_IRAM (= PE's PERF cursor).

**8 real carves, byte-exact** (sha256; all 8 also `cmp -s`-identical to the
matching `libnrtucode.a` member `.rodata`):

| IMAGE | FILE-OFF | SIZE | sha256 | CAYMAN counterpart | identical? |
|---|---|---:|---|---|---|
| `PE_PERF_IRAM` | `0x335ca0` | `0x12ce0` | `a077f110…` | `13ba3969…` | NO (recompile) |
| `PE_PERF_DRAM` | `0x348980` | `0x2da0` | `08cd432d…` | `1355773b…` | NO |
| `PE_TEST_IRAM` | `0x3a5ac0` | `0x12ca0` | `eba5f4dc…` | `4f9cbece…` | NO |
| `PE_TEST_DRAM` | `0x3b8760` | `0x30e0` | `4eb40842…` | `3561e4bd…` | NO |
| `PE_DEBUG_IRAM` | `0x42c520` | `0x18c20` | `6600e24a…` | `e1f1268c…` | NO |
| `PE_DEBUG_DRAM` | `0x445140` | `0x6400` | `4e00d0c3…` | `400910dd…` | NO |
| `PE_PROF_CAM` | `0x5a0c80` | `0x400` | `43475cec…` | `8fd7e422…` | NO (re-armed) |
| `PE_PROF_TABLE` | `0x5a1080` | `0x2000` | `d93b723e…` | `ce761f81…` | NO (re-prealloc) |

**Flat-image geometry.** None of the 8 carves is an ELF (head ∉ `\x7fELF`). The reset vector is
byte-identical across all 3 IRAM variants and **shifted +0x1c** from CAYMAN — the same shift MARIANA
ACT/DVE carry (`xxd` + `ncore2gp` objdump):

```text
MARIANA PE IRAM:  06 7d 00 00 00 00  86 7e 00 00 00 00     ; j 0x1f8 / j 0x204
CAYMAN  PE IRAM:  06 76 00 00 00 00  86 77 00 00 00 00     ; j 0x1dc / j 0x1e8   [+0x1c]

  0x000:  06 7d 00   j 0x1f8                         ; primary reset -> boot path
  0x006:  86 7e 00   j 0x204                         ; secondary -> halt trap
  0x00c:  a0 71 69 …                                  ; boot-stub body (== MARIANA DVE @0xc)
  0x1f8:  const16 a0,0 ; const16 a0,144(0x90) ; jx a0 ; -> C enter_run @ IRAM 0x90
  0x204:  halt 0
```

The boot-stub body at `0xc` is **byte-identical to MARIANA DVE** (`cmp 0xc..0x40` clean); vs CAYMAN PE
it differs only at the inner-`j` displacement byte — a full recompile with a relocated layout, the same
boot scheme shared across MARIANA NX engines, **NOT a binary patch**. The engine self-identity string
(`S: engine_base_addr=%llx tpb_base_addr=%llx -> … engine_idx=%u`) is present: the same flat image is
loaded on any engine slot; `engine_idx` (`=0` for PE) is derived at boot from `engine_base_addr` vs
`tpb_base_addr`. `[HIGH/OBSERVED string; runtime-compute INFERRED]`

The `ncore2gp` objdump decodes all three IRAMs to real Q7/NX windowed-ABI + FLIX-VLIW + IVP vector:
PE carries a full vector compute datapath (PERF 281 distinct IVP, vs CAYMAN PE PERF 307 —
same direction as DVE). The MAC-family detail is in [CAYMAN × PE §6](./cayman-pe.md).

---

## 8. Variant (DEBUG/PERF/TEST) + cross-gen size table

| GEN | VAR | IRAM sz | DRAM sz | `S:` strings | total strings | IVP-distinct |
|---|---|---:|---:|---:|---:|---:|
| CAYMAN | DEBUG | `0x19180` | `0x6220` | 148 | 247 | 179 (desync) |
| CAYMAN | PERF | `0x159e0` | `0x2a40` | 0 | 15 | 307 |
| CAYMAN | TEST | `0x152c0` | `0x2d80` | 0 | 58 | 316 |
| **MARIANA** | **DEBUG** | **`0x18c20`** | **`0x6400`** | **155** | 265 | 157 (desync) |
| **MARIANA** | **PERF** | **`0x12ce0`** | **`0x2da0`** | 0 | 15 | 281 |
| **MARIANA** | **TEST** | **`0x12ca0`** | **`0x30e0`** | 0 | 58 | 296 |

* Same DEBUG-vs-RELEASE model as CAYMAN: only DEBUG carries the `S:` runtime logs (MARIANA 155 vs
  CAYMAN 148, **+7** = the new handler logs + the `PeManageSeed` mode/micro-op + dispatch-path copies);
  PERF strips all `S:` (15 assertion source-paths survive); TEST keeps function/file symbols (58).
* **Cross-gen size:** MARIANA PE PERF/TEST IRAM are **smaller** despite +4 handlers (PERF −0x2d00
  ≈ −13%, TEST −0x2620 ≈ −11%); DEBUG IRAM slightly smaller (−0x560); **all** DRAM larger (DEBUG +0x1e0,
  PERF +0x360, TEST +0x360 = the new strings). PROF tables same size, different content (§6).
* **NO MARIANA PE image is byte-identical to its CAYMAN counterpart** (8/8 distinct sha256, §7): a full
  per-generation rebuild, not a patch.

> **NOTE — `addr_bits.hpp` is the one new source file; no `mariana-4062` errata on PE.** MARIANA PE
> DRAM adds exactly one source path vs CAYMAN PE: `addr_bits.hpp` (the same pool/tpb
> address-rerouting header MARIANA ACT/DVE gained — a gen-wide MARIANA change; verified 1 hit vs 0 on
> CAYMAN). `translate_cayman+.hpp` is on **both** gens (not new). Unlike MARIANA DVE, the PE image
> carries **no** `S: Applying patch for mariana-4062` errata string (0 hits) — that workaround is
> DVE-specific.

---

## 9. Engine-model classification (UNCHANGED) + the FW-66 boundary

MARIANA PE is the **same `cayman/seq/` SEQ-style ASCII-opcode dispatch NX engine** as CAYMAN PE — the
matmul SEQUENCER (`engine_idx = 0`, corpus CSR enum `PE=0`), distinguished by its weight-load/matmul
handler subset + the +4 new handlers + the +4 opcodes. It is **not** folded with any other engine
(the ACT→DVE fold is a MAVERICK-only change, irrelevant to PE) and **not** a POOL Q7
`kernel_info_table` engine. The full point-by-point property comparison is the §1 table; everything
not in the Δ column is byte-invariant across the generation.

**The FW-66 boundary (the cross-report fix).** [CAYMAN × PE](./cayman-pe.md) found `PeManageSeed`
**absent** on CAYMAN PE (exhaustively grep-verified) and flagged FW-66 territory. This page establishes
the firmware-image boundary: **`PeManageSeed` FIRST APPEARS on the MARIANA PE image** (a real handler,
the `(LOAD)`/`(SAVE)` mode + `micro-op` orchestration strings decoded HIGH, §2). The
[per-engine-depth](../uarch/per-engine-depth.md) PE row (`0x01..0x0A`, `0xe4`;
`S: Ldweights`/`S: Matmul`/`S: PeManageSeed`) describes this MARIANA roster. So the FW-66 PE
seed-management kernel and the MX matmul path are **MARIANA-generation PE handlers, not
CAYMAN-generation ones**:

* **CAYMAN PE** = `{Ldweights, Matmul, MatmulSparse, LdTags, PeRegWrite}`
* **MARIANA PE** adds `{PeManageSeed, LdweightsMX, MatmulMX, ConvLutLoad}`

The micro-op *semantics* (operand structs, the SR-RNG datapath) are CARRIED from
[pe-matmul.md](../firmware/kernels/pe-matmul.md); this page contributes only the **image-level
presence/absence boundary**. `[HIGH/OBSERVED handler presence; micro-op semantics CARRIED]`

---

## 10. Honesty ledger

**HIGH / OBSERVED:**

- Container sha256 `b7c67e89…632fc329b` (MATCH); 14 `MARIANA_NX_PE` getters (`nm`,
  14/14 accessor VAs exact); 8 real carves byte-exact (sha256 + `cmp -s` to `libnrtucode.a` member
  `.rodata`, 8/8); 6 zero-size SRAM/EXTRAM cursors → next-engine POOL IRAM. CAYMAN baseline carved,
  all 5 anchors MATCH (`e1f1268c`/`400910dd`/`13ba3969`/`8fd7e422`/`ce761f81`).
- Reset vector `06 7d 00 00` (`j 0x1f8`), **+0x1c** from CAYMAN's `06 76 00 00` (`j 0x1dc`), identical
  across DEBUG/PERF/TEST and to MARIANA DVE/ACT; boot-stub body @`0xc` byte-identical to MARIANA DVE;
  boot path `const16 a0,0x90; jx a0` → enter_run, 2nd vector → `halt 0` (decoded `ncore2gp`).
- **The headline:** MARIANA PE DEBUG DRAM = 4 `ManageSeed` + 2 `MX` + 1 `ConvLut` + 1 `LdweightsMX` +
  1 `MatmulMX` hits; the exact strings `S: PeManageSeed(LOAD)`/`(SAVE)`, `micro-op : LdWeight`,
  `micro-op : Matmul : is_load=%d`, `S: ConvLutLoad`, `S: LdweightsMX`, `XS: MatmulMX`. CAYMAN PE =
  **0** of each. Glue-trap (`TS: Matmul`/`XS: MatmulMX`/`VS: MatmulSparse`) documented; glue-stripped
  set-diff = +4 (`{ConvLutLoad, LdweightsMX, MatmulMX, PeManageSeed}`) / −0. Handler-name DRAM offsets
  for all 9.
- Dispatch: RAW-opcode compare-chain (no normalization), both DEBUG sites; `bnei a2,8`, `bnei a2,10`,
  `movi a3,228` arms decoded; CAYMAN 25 → MARIANA 29 real opcodes; +4 new `{0x8,0x9,0xa,0xe4}`, −0;
  17-entry sub-table @`0x80814`; PERF base @`0x80218`; log @`0x80858` (3 sites); log helper `0x14f54`.
- PROF_CAM 47→22 (re-armed, PE-only opcodes, 8-bit) + per-engine de-share (PE `43475cec` ≠ DVE
  `ca588683` ≠ ACT `326bc0dd` ≠ CAYMAN-shared `8fd7e422`, all carved + sha-compared); PROF_TABLE header
  zeroed, 59 vs 150 nonzero words.
- DTYPE: only `UINT32/INT32/FP32` named (== CAYMAN); `FP4/CPTC/MX/SFP8` absent as strings across all 8
  blobs. Size table reproduced; `addr_bits.hpp` new (1 vs 0); `translate_cayman` both gens; no
  `mariana-4062` on PE (0 hits); `BEGIN on mariana` vs `BEGIN on cayman`.

**MED / CARRIED:**

- Per-opcode→handler-name trampoline binding through the `0x38xx` cluster: the trampolines partly
  FLIX-bundle; counts coincide (HIGH), the byte-exact per-opcode→name pairing is CARRIED from
  [pe-matmul.md §2.1](../firmware/kernels/pe-matmul.md) (MED). The PERF per-opcode table rows desync
  under FLIX (base/form HIGH).
- `PeManageSeed` SR-RNG semantics, `PE_SEED_MODE` enum, `S2S1D2_PE_SEED_STRUCT`; the MX out-of-band
  `scale_addr`/E8M0 model and the `SMX1_LW`/`SMX1D3_MM` structs — CARRIED from
  [pe-matmul.md](../firmware/kernels/pe-matmul.md) / [mx-dequant.md](../firmware/kernels/mx-dequant.md).
- PROF_TABLE field schema — structure-level only.

**LOW / NOT CLAIMED:**

- Which silicon/runtime selects MARIANA vs MARIANA_PLUS, DEBUG vs PERF vs TEST (host driver +
  `NEURON_UCODE_FLAVOR`).
- The MARIANA_PLUS NX_PE byte-comparison — the catalog carries `MARIANA_PLUS_PE` getters; not carved
  here. See [MARIANA+ × PE image](./mariana-plus-pe.md).
- The exact per-handler operand-byte layout of the new PE handlers (decoded in
  [pe-matmul.md](../firmware/kernels/pe-matmul.md), not here).

---

## 11. Cross-references

- [CAYMAN × PE image](./cayman-pe.md) — **the NC-v3 diff baseline** this page diffs against (24
  handlers, reset `j 0x1dc`, shared PROF_CAM `8fd7e422`, the `PeManageSeed`/MX absence finding).
- [PE Matrix-Multiply Path](../firmware/kernels/pe-matmul.md) — the byte-exact per-gen operand structs,
  `PeManageSeed`/`PE_SEED_MODE`/`S2S1D2_PE_SEED_STRUCT` semantics, the `SMX1_LW`/`SMX1D3_MM` MX structs,
  the `arr_seq` CSR handshake, and the CAYMAN→MARIANA→MARIANA_PLUS→MAVERICK evolution.
- [MX (Microscaling) Dequant Compute Paths](../firmware/kernels/mx-dequant.md) — the POOL in-band
  `proc_4bit_mx_8` block-of-8 dequant vs the PE/DVE out-of-band `scale_addr`/E8M0 MX matmul (the §6
  reconciliation this page's NOTE cites).
- [MARIANA+ × PE image](./mariana-plus-pe.md) — the forward v4+ PE image (the next diff in the chain).
- [MARIANA × ACT](./mariana-act.md) / [MARIANA × DVE](./mariana-dve.md) — the sibling v4 image diffs
  (the +0x1c reset shift origin; DVE's `QuantizeMx` MX-quantize complement; the `mariana-4062` errata
  that is DVE-only).
- [Per-Engine Depth](../uarch/per-engine-depth.md) — the PE row (`engine_idx=0`, `0x01..0x0A`/`0xe4`
  roster) this image realizes.
- [Image Catalog Index](./image-catalog-index.md) — the full getter map (MARIANA NX_PE rows 313–326).
- [PROF_CAM / PROF_TABLE formats](./prof-cam-table-formats.md) — the HW-decode profiling CAM/table
  record layout (the per-engine de-share §6 documents).
- [Confidence & Walls Model](../reference/confidence-model.md) — the tag taxonomy.
