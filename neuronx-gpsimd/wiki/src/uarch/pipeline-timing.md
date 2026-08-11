# Pipeline Timing Model

> **Scope.** The cycle-level latency / read-stage / write-stage model of the Vision-Q7 NX
> "Cairo" core (config `ncore2gp`, `uarchName="Cairo"`, `TargetHWVersion="NX1.1.4"`). Every
> latency number on this page is read directly out of the shipped **ISS** (instruction-set
> simulator) `libcas-core.so`, whose per-opcode `…_issue` functions *literally encode* the
> read/write pipeline stage of each architectural operand as an immediate argument to a
> def/use callback. This is the strongest possible ground truth short of the RTL: it is the
> exact stage table the reference simulator schedules against.
>
> **Companion pages.** The FLIX format/slot bitfield decode and the structural co-issue
> ceiling live in [FLIX Co-Issue Matrix](./co-issue-matrix.md); the multi-ported register-file
> read/write-port pressure analysis and the bypass-network reconstruction live in
> [Register-File Port Model + Bypass Network](./regfile-ports.md). This page owns the **stage
> model**, the **per-class latency/forwarding table**, and the **structural-hazard substrate**
> (operand-availability scoreboard + the 2 LSU mem-ports + the FLIX slot count). The unified
> resolution of the reservation-model framing across the Part-4 pages is the
> [Microarchitecture Synthesis §2.4](microarch-synthesis.md) capstone.
> The per-instruction operand semantics that these latencies attach to are catalogued in the
> ISA reference, e.g. [B02 vec-alu/fp](../isa/ref/b02-vec-alu-fp.md),
> [B04 mac-integer](../isa/ref/b04-mac-integer.md), [B13 sp-cvt](../isa/ref/b13-sp-cvt.md).

All claims tagged `[CONF × PROV]`: confidence `HIGH/MED/LOW`, provenance `OBSERVED` (read out
of the binary/config here), `INFERRED` (derived from observed stage gaps), `CARRIED` (from a
prior report, re-verified). Binary paths are relative to
`extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/`.

---

## 1. How the ISS encodes pipeline stages (the extraction primitive)

`libcas-core.so` is shipped **unstripped with a full `.symtab`** (179 079 symbols;
`readelf -SW` shows `[19] .symtab`). Its scheduling model is not a data table — it is
*compiled code*. For every legal `(FLIX-format, slot, opcode)` triple there is one function:

```
<FORMAT>_<format>_<slot>_<width>_inst_<OPCODE>_issue        // 2149 of them
<FORMAT>_<format>_<slot>_<width>_inst_<OPCODE>_stall        // 1746 of them
```

e.g. `F11_F11_S1_ALU_16_inst_ADD_issue`, `F0_F0_S2_Mul_28_inst_IVP_MULQA2N8QXR8_issue`.
The two dispatch tables are returned by `dll_get_issue_functions` (`@0x17aa340`) and
`dll_get_stall_functions` (`@0x17aa2e0`, which `lea`s `slot_stall_functions @0x227f8c0`).

**The `_issue` body is a flat list of operand stamps.** Disassembling
`F11_F11_S1_ALU_16_inst_ADD_issue` (`@0x6eabe0`) shows the canonical shape:

```asm
6eabec: mov  0x15738(%rdi),%r12        ; r12 <- def/use callback ptr (slot in ISS core obj)
6eabf3: shl  $0x14,%eax ; shr $0x1c    ; extract operand-field bits from the instr word
6eabfb: call opnd_sem_AR_addr@plt      ; -> architectural AR register index of this operand
6eac09: mov  $0x4,%esi                 ; <-- STAGE = 4
6eac0e: call *%r12                     ; register (operand, stage=4) with the scheduler
…                                       ; repeat for each operand
6eac5f: mov  $0x4,%esi ; jmp *%rax      ; last operand, tail-called
```

Decode rule, used everywhere below:

| element | meaning |
| --- | --- |
| `call opnd_sem_<RF>_addr@plt` | resolves an architectural operand of regfile `<RF>` (AR, vec, wvec, vbool, valign, gvr, BR, b32_pr) from a bitfield of the instruction word |
| the `mov $0xN,%esi` that **immediately follows** | `N` (hex) is the **pipeline stage** at which that operand is read (USE) or written (DEF) |
| the callback slot (`0x15738`, `0x153b0`, …) it is handed to | selects DEF vs USE and the regfile (initialised from the `my_<RF>_<reg>_set_def` / `_set_use` family, e.g. `my_AR_0_arr_set_def @0x1796140`, `my_AR_0_ars_set_use @0x1795fd0`) |

By convention the **first** operand stamped is the result (DEF); the rest are inputs (USE).
**Producer→consumer dependency latency = (producer result-DEF stage) − (consumer operand-USE
stage).** `[HIGH × OBSERVED]`

> **NOTE — this *is* the schedule table.** Earlier reconnaissance described a 45 MB
> "decoded TIE-XML" carrying `<INSTR_SCHEDULE>`/`<USEDEF_LIST>` tags. Those XML tags are **not**
> present as strings in any shipped config DLL (`rg -a 'INSTR_SCHEDULE'` over
> `libcas-core.so`, `libtie-core.so`, `libisa-core.so` → 0 hits). The schedule was *compiled
> into* the `…_issue` functions above. Everything on this page is recovered from that compiled
> form, which is authoritative for the simulator's timing behaviour.

---

## 2. Pipeline stage structure — two coupled pipes

The core runs **two pipelines that share fetch/issue but differ in depth**. The scalar pipe's
stage map is given *by name* in the config (`core.xparm` `<core>` element):

```
pipeAStage=1  pipeBStage=3  pipeEStage=4  pipeMStage=5  pipeWStage=6  pipeDStage=9
ifetchLatency=3   instructionWidth=0x100 (256b)   L0IBufferSize=0x80 (128B)
maxInstructionSize=0x20 (32B)   instfetchsize=0x10 (16B)   loadStoreUnitsCount=2
```

and again in `default-params` / `ncore2gp-params`:

```
ISSPipeBStage = 3   ISSPipeEStage = 4   ISSPipeMStage = 5   ISSPipeWStage = 6
InstFetchWidth = 256   HasVectorPipe = 1   IsaUseMul16 = 1   MUL32ImplementsMul16 = 1
```

`[HIGH × OBSERVED]` — `core.xparm`, `default-params`, `ncore2gp-params`.

> **CORRECTION — two stage-numbering conventions, +1 apart.** A prior report quoted the **TIE
> root** convention `rstage=0 / estage=3 / mstage=4 / wstage=6`. The **ISS stamps disassembled
> on this page use the config convention `A1 / B3 / E4 / M5 / W6` (plus a deep-pipe `D9`).** The
> two agree on W (=6) but the ISS execute/memory stages sit one higher: ISS-E4 ≡ TIE-E3,
> ISS-M5 ≡ TIE-M4. **All numeric stages below are in the ISS `A1/B3/E4/M5/W6/D9` convention** —
> that is the only convention the latency stamps actually use, so it is the one a reimplementer
> must reproduce. To translate any stamp to the TIE convention, subtract 1 from E/M (W and the
> stage-10+ vector ports are unchanged). This is an in-place correction, not a silent edit.

### 2.1 Scalar / address pipe — 7 stages, `A1 / B3 / E4 / M5 / W6`

| stage | name | what settles | evidence (ISS stamp) |
| --- | --- | --- | --- |
| 1 | A (address) | AR base read for L/S address-gen; MOVI immediate DEF; post-increment AR write | `L16UI` AR-base USE @1; `MOVI` DEF @1; `IVP_LV2NX8_IP` two AR @1 |
| 3 | B (decode / branch) | branch condition read + resolve; vision-op issue marker | `BEQZ_W15` AR USE @3; `BALL_W15` two AR @3 |
| 4 | E (execute) | simple-ALU compute + AR write; BR write | `ADD`/`SLL`/`EXTUI`/`MAX` arr·ars·art @4 |
| 5 | M (memory) | scalar load data return; store data egress | `L16UI` art DEF @5; `S16I` data-source USE @5 |
| 6 | W (writeback) | MUL32/MUL16 result; SR writes; sync barriers | `MUL16S` arr DEF @6 |
| 9 | D (done / deep) | retire point that the deep vector pipe's tail aligns to | `pipeDStage=9` (config) |

### 2.2 Vector / VFPU pipe — deep, operand read port @10, results @11–13

The Vision SIMD (`xt_ivp32`, `coprocessorCount=7`, `vectorPipe=1`) issues into a much longer
datapath that is **not** a continuation of the 7-stage scalar pipe. Its architectural operand
**read port is stage 10** and its results land at 11/12/13 by latency class; stage-14 carries
IEEE status flags and stage-15 the deferred imprecise exception.

| stage | role | ISS evidence |
| --- | --- | --- |
| 3 | `CPENABLE` coprocessor-enable gate (a USE on the vision op) | `my_CPENABLE_stall @0x178e420`, called with stage 3 in every vision `_stall` |
| 10 (`0xa`) | **canonical vector read port** — all `vec`/`vbool`/`gvr`/`valign` source operands | universal: `opnd_sem_vec_addr` → `mov $0xa,%esi` in every vision `_issue` |
| 11 (`0xb`) | 1-cycle class result: vec-ALU, vec-mov, vec-shift, vbool-ALU, compare, FP min/max | `IVP_ANDB` vbool DEF @11; `IVP_LTNX16` vbool DEF @11; `IVP_MINN_2XF32T` DEF @11 |
| 12 (`0xc`) | 2-cycle class result: multiply/MAC, reduce, pack/unpack, lookup, select, divide-step, `TRUNC` | `IVP_MULQA2N8QXR8` wvec DEF @12; `IVP_RADDNX16` DEF @12; `IVP_TRUNCN_2XF32T` DEF @12 |
| 13 (`0xd`) | 3-cycle class result: FMA, SP/HP convert, recip/rsqrt seed | `IVP_MULAN_2XF32T` DEF @13; `IVP_FLOATN_2X32T` DEF @13; `IVP_MULANXF16T` DEF @13 |
| 14 (`0xe`) | IEEE FSR flag DEFs (Invalid/DivZero/Overflow/Underflow/Inexact) | every FP `_issue` opens with 4–5× `mov $0xe,%esi` |
| 15 | deferred / imprecise vector exception post (`impreciseExceptions=1`) | `core.xparm` `impreciseExceptions="1"`; status, not data |

**Quantitative anchor — every operand stamp in the ISS.** A full sweep of all 2149 `_issue`
functions (6081 operand callbacks, 0 unresolved) gives the read/write-stage histogram per
regfile, confirming the read-port/result-stage structure above to the unit:

```
vec    (2815): {10:1972, 11:225, 12:341, 13:277}   ; read port 10; writes 11/12/13 by class
wvec   ( 241): {10:25,   12:216}                    ; accumulator read 10 / write 12 (RMW @12)
vbool  ( 634): {10:472,  11:141, 12:21}             ; predicate read 10; bool result 11
AR     (1920): {1:994,   3:179,  4:581, 5:105, 6:20, 12:41}  ; A1 addr, B3 branch, E4 ALU,
                                                              ;   M5 load, W6 mul, 12 = vec→scalar
valign (305): {9:209, 10:96}    b32_pr (119): {10:62, 12:57}    gvr (34): {10:34}
```

There are **372 distinct opcodes**, each compiled into 1–38 `_issue` copies (mean ≈ 5.8) — one
per legal `(format, slot)` — across **60 distinct format/slot prefixes**. The *timing* of an
opcode is identical across its copies; only the FLIX slot eligibility differs.

> **KEY MICROARCHITECTURE FACT `[HIGH × INFERRED]`.** A vision op has a long *issue-to-result*
> depth (issue ≈ stage 3 → result 11–13, i.e. ~8–10 absolute stages) but a **short
> producer→consumer dependency latency of 1–3 cycles**, because two dependent vision ops both
> read at stage 10 and the bypass network forwards the 11/12/13 result back to the next op's
> stage-10 read. Without that forward the dependency would be `result_stage − decode_stage ≈
> 8–10` cycles. The 1/2/3-cycle numbers below are therefore *forwarded* dependency latencies, and
> they equal exactly `result_stage − 10` (see §5).

---

## 3. Per-class latency / forwarding table

Each row is `result@` = the result-DEF stage observed in the ISS, and `dep-lat` = the
back-to-back dependent-issue latency in cycles = `result@ − read-port`. Vector read port is 10;
scalar read ports are 4 (E inputs) / 1 (address) / 3 (branch). Every entry was disassembled
from at least one `…_issue` function (the named exemplar). `[HIGH × OBSERVED]`

### 3.1 Vector / VFPU classes (read port @10)

| class | exemplar opcode (`_issue` addr) | inputs @ | result @ | dep-lat |
| --- | --- | --- | --- | --- |
| vec integer ALU (add/sub/logic/shift/move/min/max) | `IVP_MINN_2XF32T` `@0x14b6380` | vec 10 | **11** | 1 |
| vbool ALU / compare | `IVP_ANDB` `@0x71d9a0`, `IVP_LTNX16` `@0x11f4db0` | vbool/vec 10 | **11** | 1 |
| SIMD multiply (vec→vec) | `IVP_MULPN16XR16` `@0xde22b0` | vec 10 | **12** | 2 |
| quad-MAC into wide accumulator | `IVP_MULQA2N8QXR8` `@0xde1ac0` | vec 10, **wvec acc 12** | **wvec 12** | 2 |
| horizontal reduce | `IVP_RADDNX16` `@0x14b2f40` | vec 10 | **12** | 2 |
| pack / unpack / wvec-mov | `IVP_PACKL2NX24` (`_stall @0x73f7d0`) | wvec/vec 10 | **12** | 2 |
| table lookup (HP/SP) | `IVP_GATHERD*` family | vec 10 | **12** | 2 |
| vector select / mux | `IVP_SELNX16` `@0x14b2fb0` | vec 10 | **12** | 2 |
| `TRUNC`/round-to-integral (stays fp32) | `IVP_TRUNCN_2XF32T` `@0x14b5a90` | vec 10 | **12** | 2 |
| divide **step** op (one of N) | `IVP_DIVN_2X32X16S_4STEP` `@0x11f4ca0` | vec 10 | **12** | 2 |
| FP FMA (SP) | `IVP_MULAN_2XF32T` `@0x14b6020` | vbool 10, vec 10 ×3 | **13** | 3 |
| FP FMA (HP) | `IVP_MULANXF16T` `@0x14b6e90` | vec 10 | **13** | 3 |
| SP/HP convert (int↔float, widen) | `IVP_FLOATN_2X32T` `@0x14b59e0` | vec 10 | **13** | 3 |
| FP divide / NEXP seed | `IVP_DIVNN_2XF32T` `@0x14b6250`, `IVP_NEXP01N_2XF32T` `@0x14b5b50` | vec 10 | **13 / 12** | 3 / 2 |
| recip / rsqrt seed | `bbn_sem_vec_sprecip_rsqrt` family | vec 10 | **13** | 3 |
| scatter / gather | `IVP_GATHERDNX16` `@0x71e6e0`, `IVP_SCATTERINCNX16` `@0x1189870` | gvr/vec 10, AR 1 | **11** (gather) | 0–1 |
| vector load (result) | `IVP_LV2NX8_I` `@0x164d4a0` | AR base 1 | **vec 10** | 0 (next bundle) |
| vector store (commit) | `IVP_SCATTERINCNX16` store-source | vec 10, AR 1, decode 3 | data egress ~11 | — |

> **QUIRK — `TRUNC` is one cycle faster than every other convert.** `IVP_TRUNCN_2XF32T`,
> `IVP_NEXP01N_2XF32T`, `IVP_MKSADJN_2XF32T` write their result at **stage 12**, while
> `IVP_FLOATN_2X32T` and the int↔float/widen conversions write at **stage 13**. This split is
> real and independently confirmed by [B13 sp-cvt](../isa/ref/b13-sp-cvt.md) (writeback stage
> "**13** (FLOAT/round/widen) · **12** (TRUNC)"). Model the convert unit as 3-cycle with a
> 2-cycle `TRUNC` fast path, not a uniform 3-cycle block.

> **GOTCHA — the wide accumulator port is at stage 12, not 10.** For the accumulating quad-MAC
> `IVP_MULQA2N8QXR8`, the two `vec` source operands read at stage 10 but **both `wvec`
> accumulator operands (read-modify-write) are stamped at stage 12** (`opnd_sem_wvec_addr →
> mov $0xc`). The accumulator is therefore on a *self-recurrence at the result stage*: the MAC
> reads the previous accumulator value at 12 and writes the new one at 12. This is what makes the
> chained-MAC initiation interval 1 with a 2-cycle accumulator forward (12→12), the deep-learning
> inner-loop critical path. The `wvec` file has only 4 entries (`wvec 1536b × 4`), so MAC chains
> are file-bound to 4 live accumulators. See [Register-File Port Model](./regfile-ports.md).

> **NOTE — divide is an N-step macro.** There is no single-issue divide-result. A full SIMD
> divide is a chain of step ops (`IVP_DIVN_2X32X16S_4STEP0`, `…_4STEP`, ×4), each a 2-cycle
> @12 op reading the previous step's partial @10. A complete 32-bit quotient = 4 chained 2-cycle
> steps ⇒ ~8-cycle dependent latency, fully pipelined (issue every cycle if independent).

### 3.2 Scalar classes

| class | exemplar (`_issue` addr) | inputs @ | result @ | dep-lat |
| --- | --- | --- | --- | --- |
| ADD/SUB/ADDX/AND/OR/XOR/EXTUI/SLL/MAX | `ADD` `@0x6eabe0`, `MAX` `@0x1188f80` | AR 4 (`MAX` reads @5/4) | **4** (E) | 0 |
| MOVI (immediate) | `MOVI` `@0x6eacd0` | — | **1** (A) | — |
| MUL16S / MUL32 (`MUL32ImplementsMul16`) | `MUL16S` `@0x1188ef0` | AR 4 | **6** (W) | 2 |
| scalar load `L8/16/32 I`, `L32R` | `L16UI` `@0x1188cb0` | AR base 1 | **5** (M) | 1 |
| scalar store `S8/16/32 I` | `S16I` `@0x1188de0` | AR base 1, data 5 | commit 5 | — |
| branch `B*`, `B*.W15` | `BEQZ_W15` `@0x1189070`, `BALL_W15` `@0x11890b0` | AR 3 | resolve **3** (B) | — |

`Div32=1` (config) provides a scalar 32-bit divide; `writeBufferEntries=8` buffers stores.
`Mulh=1` enables the high-half multiply.

---

## 4. Functional-unit model (by unit, not opcode)

Because every result lands at a *fixed* stage with no observed recurrence other than the
accumulator self-forward, each unit is **fully pipelined, initiation interval = 1** as far as
the shipped tables show. `[HIGH × OBSERVED latency; MED × INFERRED II=1 — §6]`

```c
// One canonical reservation/forwarding sketch per latency class. RESULT_STAGE and READ_PORT
// are the ISS stamps disassembled above; the reservation substrate is the populated scoreboard +
// mem-port of §6 — there is no per-COMPUTE-unit single-issue counter in the shipped ISS, so a
// compute unit's II is bounded by the FLIX slot count (§6) + operand availability (the per-port
// stall cycle count stays [MED]). Symbols named are real ISS callbacks.

enum { READ_PORT_VEC = 10, READ_PORT_AR_E = 4, READ_PORT_AR_A = 1, READ_PORT_BR = 3 };

// --- VEC-ALU: 1-cycle, result @11 (IVP_*NX16 add/sub/logic/shift, vbool, compare, FP min/max)
//   issue fn pattern: opnd_sem_vec_addr -> mov $0xa (read@10);  result: ... -> mov $0xb (def@11)
static inline int vec_alu_result_stage(void)   { return 11; }   // dep-lat = 11 - 10 = 1

// --- VEC-MUL / quad-MAC: 2-cycle, result @12; accumulator self-forward 12->12
//   IVP_MULQA2N8QXR8_issue: opnd_sem_vec_addr x2 @10; opnd_sem_wvec_addr x2 @12 (acc RMW)
static inline int mac_result_stage(void)        { return 12; }   // dep-lat 2; acc-chain II=1
static inline int mac_acc_port(void)            { return 12; }   // wvec acc read==write stage

// --- VFPU FMA / convert: 3-cycle, result @13; TRUNC fast path @12; FSR flags @14
//   IVP_MULAN_2XF32T_issue: 5x mov $0xe (flag def@14); vec @10; result @13
static inline int fma_result_stage(void)        { return 13; }
static inline int trunc_result_stage(void)      { return 12; }   // QUIRK §3.1
static inline int fsr_flag_stage(void)          { return 14; }   // my_*Flag def@14

// --- DIVIDE: iterative; each IVP_DIVN_*_4STEP is a 2-cycle @12 op; full = 4 chained steps
static inline int divide_step_stage(void)       { return 12; }
static inline int divide_steps_for_2x32(void)   { return 4; }    // _4STEP0.._4STEP3

// --- LOAD/STORE (vector): load result @10 (AR base @1); store data @10, egress ~11
//   The ONLY structurally-reserved port in the whole ISS: load/store _stall fns call the
//   memory interface nx_Load_0_interface / nx_Load_1_interface / nx_Store_0_interface (2 LSUs,
//   loadStoreUnitsCount=2). Compute _stall fns never call any interface => compute is unported.
static inline int vec_load_result_stage(void)   { return 10; }   // IVP_LV2NX8_I def@10
static inline bool mem_port_busy(iss_t *s, int port /*0,1=Ld; 0=St*/);  // nx_*_interface

// --- SCALAR: ALU E@4 (0-cyc fwd), MUL32 W@6 (2-cyc), load M@5 (1-cyc), branch B@3, MOVI A@1
static inline int scalar_alu_stage(void)        { return 4; }
static inline int scalar_mul_stage(void)        { return 6; }
static inline int scalar_load_stage(void)       { return 5; }
static inline int scalar_branch_stage(void)     { return 3; }
```

The throughput ceiling is therefore **not** per-unit; it is the **FLIX slot count per bundle**
(peak issue width = 5 ops/bundle in formats F3/F11), bounded further by operand availability.
The detailed format/slot bitfield model is [FLIX Co-Issue Matrix](./co-issue-matrix.md).

---

## 5. Bypass / forwarding network (reconstructed from stage gaps)

There is no explicit "bypass" table; the network is *inferred* from the equality
`dep-lat == result_stage − read_port` holding exactly for every class. `[HIGH × OBSERVED gaps,
INFERRED network]`

* **Scalar ALU→ALU:** result @4, dependent read @4 ⇒ **0-cycle** full E-stage forward (ALU runs
  at IPC). `ADD`/`SLL` both stamp E@4.
* **Scalar MUL32→use:** result @6 (W), a dependent ALU reads @4 (E) ⇒ **2-cycle bubble** (no
  W→E forward of a multiply result). `MUL16S` arr@6.
* **Scalar load→use:** result @5 (M), dependent ALU reads @4 (E) ⇒ **1-cycle load-use bubble**.
* **Vector (dominant network):** read port @10; results @11/12/13. The forward bridges every
  compute write stage back to the next op's stage-10 read:
  * vec-ALU 11 → read 10 ⇒ 1-cycle
  * MAC/lookup/reduce/select/TRUNC 12 → read 10 ⇒ 2-cycle
  * FMA/convert 13 → read 10 ⇒ 3-cycle
* **Accumulator chain:** `wvec` read **and** write at 12 ⇒ a 12→12 self-forward sustains a
  back-to-back MAC chain at II=1 (2-cycle result latency, the inner-loop critical path).
* **FSR/status:** IEEE flag DEFs at 14 consumed at 14 (flag-accumulate is internal to stage 14).
* **CPENABLE gate @3:** a USE on (almost) every vision op — a vision op issued with `CPENABLE`
  clear traps. This is a control gate (`my_CPENABLE_stall`), not a data bypass.

---

## 6. The structural-hazard substrate — scoreboard + mem-port + slot-count

A reimplementer needs a *structural-hazard* model to know how many ops of a class may issue per
cycle below the FLIX slot ceiling. The realizable bound **is** recoverable: it is
`operand-availability (RAW/WAW scoreboard) + the 2 LSU mem-ports + the FLIX slot count + the
class-exclusive lane partition`. The per-`(format,slot,opcode,stage)` bodies that encode it ship
**fully populated** in `libcas-core.so` (`_issue` = 2149, `_stall` = 1746, `_stage<N>` ≈ 160 k,
stages 0–15). What stays `[MED]` is only the reduction of
those populated bodies to an **exact per-port single-issue stall cycle count** — a task, not a
wall. `[HIGH × OBSERVED bodies; MED × exact cycle counts]`

> **CORRECTION (divergence #1, resolved at the capstone) — the reservation model is PRESENT, not
> an empty wall.** An earlier framing of this section called the model "the honest empty-reservation
> wall / not recoverable." That over-read one artifact: the empty `MODULE_SCHEDULE` reservation
> bodies belong to the *TIE encoding DB* (`libisa-core.so`), which indeed carries no schedule — but
> the *cycle model* `libcas-core.so` ships the reservation as the populated `_issue`/`_stage<N>`
> function bodies above. The honest residual is the **exact stall cycle count** (MED), not the
> model's existence. This is unified across the Part-4 pages in
> [Microarchitecture Synthesis §2.4 + §8 row 1](microarch-synthesis.md) and
> [co-issue-matrix §4](./co-issue-matrix.md); this page adopts that resolution.

Two observations describe *what* the populated bodies model (a per-register scoreboard + one
memory-only functional-unit port), and *what* they do not (a per-compute-unit issue counter):

1. **The `…_stall` functions implement a per-register DEF/USE *scoreboard* — never a
   functional-unit reservation station.** Each of the 1746 `…_stall` functions calls
   `opnd_sem_<RF>_addr` followed by a per-regfile availability callback that does a plain
   `cmp <stage>, scoreboard_busy_stage[reg]` against the producer's recorded write-completion
   cycle, plus `my_CPENABLE_stall @0x178e420` (@stage 3). The stall callback family is the
   **per-state availability** set — `my_AR_stall @0x1795bb0`, `my_vec_stall @0x17994d0`,
   `my_wvec_stall @0x17a7ac0` (probed @12), `my_vbool_stall @0x17a2df0`,
   `my_valign_stall @0x17a6b80`, `my_gvr_stall @0x17a9970`, `my_b32_pr_stall @0x17a9290`,
   `my_CPENABLE_stall`, plus write-buffer interlocks `my_WB_{N,S,P,C,M,T}_stall`,
   `my_REV8AR_stall`, `my_InOCDMode_stall`, `my_MS_DISPST_stall`, and the External-Register-
   Interface RAW family `my_ERI_RAW_INTERLOCK_*` (93 `my_*_stall` symbols total). A resource-
   keyword scan (`reservation|port_busy|fu_busy|occup|resource|unit`) over **all three** config
   DLLs (`libcas-core.so`, `libisa-core.so`, `libtie-core.so`) returns **0 hits**. The ISS thus
   models RAW/WAW scoreboard hazards + the coprocessor gate + write-buffer/OCD interlocks, and
   nothing about how many multipliers physically exist.

   **The one genuine structural port — and it is memory-only.** The single resource-occupancy
   construct in the entire ISS is the memory interface: `nx_Load_0_interface`,
   `nx_Load_1_interface`, `nx_Store_0_interface` (imported `U` symbols → runtime memory-subsystem
   back-pressure). These are called **only by load/store `_stall` functions** — every arithmetic
   `_stall` (ALU/MAC/FMA/convert/reduce) has **no** interface call. So the *only* port that can
   structurally stall co-issue is the 2-load + 1-store memory port set (matching
   `loadStoreUnitsCount=2`); the compute units have no modeled port at all.

   ```c
   // F0_F0_S1_Ld_16_inst_IVP_PACKL2NX24_stall @0x73f7d0 — verbatim shape:
   bool packl_stall(iss_t *s, instr_word_t *w) {
       int acc = opnd_sem_wvec_addr(field(w));
       if (s->wvec_available(acc, /*stage=*/10)) return true;   // RAW on accumulator @10
       return my_CPENABLE_stall(s, /*stage=*/3);                // coprocessor-enable gate @3
   }   // no functional-unit / issue-port counter anywhere
   ```

2. **The `MODULE_SCHEDULE` reservation bodies of the TIE-DB ship empty — but that is the
   *encoding DB*, not the cycle model.** Prior census found all 1994 `MODULE_SCHEDULE` elements
   self-closing (0 non-empty bodies) in the `libisa-core.so` TIE encoding DB, each a `(line,file)`
   pointer into a `coretie2_internalmodules.tie` source that is **absent** from this extraction;
   and the `SLOT_OPCODES` eligibility lists are **permissive supersets** (Tensilica lists nearly
   every op as slot-legal). This says nothing about the cycle model: the per-stage reservation
   bodies in `libcas-core.so` (the `_issue`/`_stall`/`_stage<N>` functions of §6 intro) **are**
   populated — they are where the scoreboard + the one memory-only port live ([co-issue-matrix §4],
   [microarch-synthesis §2.4](microarch-synthesis.md)). The empty `MODULE_SCHEDULE` is the
   *TIE-DB* table; the populated `_issue`/`_stage<N>` bodies are the reservation.

**Consequence.** The structural bounds that *are* sound:

* issue width ≤ the format's slot count — **HARD**, from the FLIX `FORMAT` bitfield (F3/F11 ⇒ 5;
  F0/F1/F2/F4/F6/F7 ⇒ 4; N0–N2 ⇒ 2; density `Inst*` ⇒ 1). The 40 ISS `(format,slot)` prefixes
  enumerate exactly: `{F0,F2,F6,F7}=S0_LdSt·S1_Ld·S2_Mul·S3_ALU`, `F1=S0_LdStALU·…`,
  `F3=+S4_ALU`, `F4=S0_Ld·S1_Ld·S2_Mul·S3_ALU`, `F11=S0_Ld·S1_ALU·S2_Mul·S3_ALU·S4_ALU`,
  `N0=S0_LdSt·S3_ALU`, `N1=S0_LdSt·S2_Mul`, `N2=S0_LdSt·S1_Ld`.
* the slot **name** `S2_Mul` is a generator convention, **not** a unique multiplier port:
  multiply/MAC-class `…_issue` functions also exist for `S3_ALU` (and `S0_LdStALU`) slots
  (`nm | rg 'inst_IVP_MUL.*S3_ALU'` is non-empty), so a multiply may issue in more than one slot.
* the **only** unit whose co-issue *is* structurally throttled below the slot count is the
  **memory port** — 2 load + 1 store (`nx_Load_0/1_interface`, `nx_Store_0_interface`,
  `loadStoreUnitsCount=2`). Compute units (ALU/MAC/FMA/convert) have **no** modeled port and are
  bounded only by slots + operand availability.

The exact **per-port single-issue cycle count below the slot count is `[MED]`, not a wall** —
e.g. "exactly one multiply per bundle" vs "up to two/three" is not reduced to a byte-readable
literal *here*, because the question requires unfolding the populated `_stage<N>` bodies rather
than reading a single field, and the `_stall` code consults a per-register scoreboard, not a
per-compute-unit counter. The realizable structural bounds that *are* sound and authoritative for
issue legality (the FLIX slot count, the 2 mem-ports + ≤1 store, the single `S2_Mul` lane) stand;
a cycle-accurate model of *this config* treats each compute unit as fully pipelined (II=1) bounded
by FLIX slot count + operand availability, with the finer single-port cycle count flagged `[MED]`
(recoverable in principle from the populated bodies — a task, not an empty wall).

> **CORRECTION — divergent multiply-per-format counts.** One prior pass claimed a specific
> per-format multiply co-issue ceiling (`F4/F6=3, F0/F1/F3=2, F11/F7/N0=1`). Re-deriving it here
> from the ISS `…_issue` symbol set (mapping opcodes to the multiply/MAC/FMA/divide class by
> name and counting mul-capable slots per format) instead yields `{F0,F1,F2,F3,F7}=2
> (S2_Mul+S3_ALU), {F4,F6,F11,N1}=1 (S2_Mul only)`. **The two methods disagree** because both read
> *permissive* eligibility, not a true single-issue port limit — the `[MED]` residual above. Do not
> trust either number as a hard ceiling; trust only the slot-count bound (the unified
> `1×S2 + 1×S3` MAC-class ceiling of [co-issue-matrix §3.2](./co-issue-matrix.md) /
> [microarch-synthesis §2.2](microarch-synthesis.md)). Recorded as the open cross-page divergence
> resolved at the capstone.

---

## 7. Worked dependency examples

```text
; MAC chain (inner loop), all in one issue stream:
;   IVP_MULQA2N8QXR8  v_acc, v0, v1   ; reads v0,v1 @10, reads+writes wvec acc @12
;   IVP_MULQA2N8QXR8  v_acc, v2, v3   ; depends on v_acc -> acc forward 12->12 -> II = 1 cycle
; Sustained: one quad-MAC per cycle, 2-cycle result latency, bound to 4 live wvec accumulators.

; FMA -> consumer:
;   IVP_MULAN_2XF32T  v_r, va, vb, vc ; result @13
;   IVP_ADDN_2XF32T   v_s, v_r, vd    ; reads v_r @10 -> 13->10 forward -> 3-cycle dep latency

; vector load -> use:
;   IVP_LV2NX8_I  v_x, a_p, 0         ; result @10
;   IVP_ADD2NX8   v_y, v_x, v_z       ; reads v_x @10 -> same-stage: 0-cycle in a *later* bundle,
;                                       true 1-op dependency if same bundle (must be next bundle)

; scalar load-use bubble:
;   L32I  a1, a2, 0                   ; result @5 (M)
;   ADD   a3, a1, a4                  ; reads a1 @4 (E) -> 5 > 4 -> 1-cycle load-use stall
```

---

## 8. Cross-check summary

| claim | this page (ISS ground truth) | prior / sibling | verdict |
| --- | --- | --- | --- |
| scalar pipe `A1/B3/E4/M5/W6/D9` | `core.xparm` `<core>`, `default-params` | TIE `r0/e3/m4/w6` | **RECONCILED** (+1 on E/M; W,vec-ports equal) |
| vec read port @10 | universal `opnd_sem_vec_addr → 0xa` | prior report | CONSISTENT |
| vec-ALU 1-cyc @11 | `IVP_MINN`/`ANDB`/`LTNX16` @11 | [B02](../isa/ref/b02-vec-alu-fp.md) | CONSISTENT |
| MAC/multiply 2-cyc @12 | `IVP_MULQA2N8QXR8` @12 | [B04](../isa/ref/b04-mac-integer.md) | CONSISTENT |
| FMA 3-cyc @13 | `IVP_MULAN_2XF32T`/`MULANXF16T` @13 | prior report (FMA 3-cyc) | CONSISTENT |
| convert @13, `TRUNC` @12 | `FLOATN` @13, `TRUNCN` @12 | [B13](../isa/ref/b13-sp-cvt.md) L66/492 | CONSISTENT |
| MUL32 2-cyc @6 (W) | `MUL16S` arr@6 | prior report | CONSISTENT |
| load-use @5 / @10 | `L16UI` @5, `IVP_LV2NX8_I` @10 | prior report (ld/st) | CONSISTENT |
| FSR flags @14 | 4–5× `mov $0xe` per FP op | prior report (@14) | CONSISTENT |
| branch resolve @3 (B) | `BEQZ_W15` AR @3 | prior report | CONSISTENT |
| reservation model | `_stall` (1746) = per-register scoreboard + 1 mem-only port; `_issue` (2149) / `_stage<N>` (~160 k) bodies PRESENT | MODULE_SCHEDULE (TIE-DB) empty | **PRESENT** (scoreboard + mem-port + slot-count; exact cycle counts MED) |
| multiply-per-format ceiling | slot-count bound `1×S2 + 1×S3`; finer cycle count MED, methods diverge | prior `3/2/1` | **RESOLVED** at [synthesis §2.2](microarch-synthesis.md) (§6) |

---

## 9. Provenance index

| fact | symbol / file | addr / value |
| --- | --- | --- |
| issue dispatch table | `dll_get_issue_functions` | `@0x17aa340` |
| stall dispatch table | `dll_get_stall_functions → slot_stall_functions` | `@0x17aa2e0 → 0x227f8c0` |
| def/use operand resolvers | `opnd_sem_{AR,vec,wvec,vbool,gvr,valign,BR}_addr` | `@0x17a9f20…` |
| availability stall callbacks | `my_{AR,vec,wvec,vbool,valign,gvr,b32_pr,CPENABLE}_stall` | `@0x1795bb0…` |
| scalar stage map | `core.xparm` `<core …>` | `pipeA1/B3/E4/M5/W6/D9`, `ifetchLatency=3` |
| ISS pipe params | `default-params`, `ncore2gp-params` | `ISSPipe{B3,E4,M5,W6}`, `InstFetchWidth=256` |
| bundle / fetch width | `core.xparm` | `instructionWidth=0x100`, `L0IBufferSize=0x80`, `maxInstructionSize=0x20` |
| core identity | `core.xparm` | `uarchName="Cairo"`, `NX1.1.4`, `loadStoreUnitsCount=2`, `coprocessorCount=7` |
| `.data` VMA offset | `readelf -SW libcas-core.so` | `.data.rel.ro` VMA `0x2070900` − file `0x1e70900` = **0x200000** (`.text`/`.rodata` are 1:1) |

> All addresses are in `libcas-core.so` `.text`/`.rodata` where VMA == file offset; only
> `.data`/`.data.rel.ro` carry the +0x200000 VMA−fileoffset delta (irrelevant to the `.text`
> `_issue`/`_stall` disassembly above, but required before any `xxd`/`objdump` on a
> `.data`-resident struct in this DLL).
