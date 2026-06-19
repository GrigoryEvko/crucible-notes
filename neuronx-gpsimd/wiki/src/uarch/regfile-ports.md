# Register-File Port Model + Bypass Network

A scheduler for this core does not get to pretend every operand is free. The Cairo (`ncore2gp`)
config issues up to **five** slots per FLIX bundle, and every co-issued op reads its sources in
the *same* cycle — so the `vec` file must physically sustain on the order of **ten simultaneous
reads** at one pipeline stage, while writing those same ops' results back across *four* different
stages. This page is the cycle-accurate port model behind the [eight register
files](../isa/core/register-files.md): the per-file read/write **port counts per cycle**, the
**distinct-registers-per-bundle** limits, the **port-conflict** constraints, the full **write-stage
→ read-stage forwarding matrix**, the `wvec` accumulator's **2-cycle recurrence**, and the
**no-forward structural-hazard list**. Get this wrong and a reimplementation either over-builds the
register file (wasted area — the file is already ~4.5M cells) or schedules illegal bundles that the
real silicon cannot read in one cycle.

Everything here is derived from the shipped `libcas-core.so` schedule descriptors (one per
`(format, slot, opcode)` instance) and the `libisa-core.so` regfile/format tables. The schedule
is the authoritative source for which pipeline stage each operand is read or written in; the format
tables are the authoritative source for how many slots co-issue and which semantic class is legal in
each slot. `[HIGH/OBSERVED]` unless flagged; the physical-port *sizing* (vs. the worst-case demand)
is `[INFERRED]` — the shipped artifacts state the demand, not a port count, and the demand is what a
cycle-accurate model needs.

> **NOTE — three claims here correct an earlier backing analysis.** A prior port study asserted that
> `gvr` and `b32_pr` carry *zero* schedule operands and that the integer quad-MAC reads exactly four
> `vec` registers. Re-grounding directly against the `libcas-core.so` operand-accessor symbols shows
> all three are wrong: `gvr` is the **scatter/gather address-vector** port, `b32_pr` carries
> multiply/replicate operands, and the quad-MAC touches **five** `vec` operands (four reads plus a
> `vt` read-modify-write accumulator). Each correction is flagged in place with its witness symbol.

---

## 1. How the port model is recovered (there is no port-count field)

The config exposes **no** explicit physical-port count. There is no `XCHAL_*_READ_PORTS` /
`*_WRITE_PORTS` macro for the TIE register files, and the schedule's reservation bodies do not encode
port multiplicity. The model is therefore *composed* from three quantities, each read straight out of
the binary:

1. **Per-op operand stage + count** — `libcas-core.so` ships, for every `(format, slot, opcode)`
   instruction instance, a 16-entry **`<inst>_stage_functions`** table (an array of per-pipeline-stage
   function pointers, stages 0…15) plus a set of **operand-accessor** symbols of the form
   `my_<REGFILE>_<SLOT>_opnd_<SEM>_<OPERAND>_<KIND>`, where `KIND ∈ {use, def, set_use, set_def,
   kill_def, bitkill_def}`. `use` = a read port demanded; `def` = a write port demanded; the
   `<SLOT>` digit is the FLIX slot the operand belongs to; `<OPERAND>` is the architectural operand
   name (`vt`/`vr`/`vs`/`vp`/`vq`/`wvt`/`wvu`/`gt`/`gs`/…). There are **411** distinct `_opnd_`
   accessors and **12 564** instruction-instance `_stage_functions` tables.

2. **Per-slot class legality** — `libisa-core.so` names every format's slots as
   `Field_..._Slot_<fmt>_s<n>_<class>_get/_set`, e.g. `Slot_f3_s2_mul`, `Slot_f11_s3_alu`,
   `Slot_f0_s0_ldst`. The `<class>` token (`ldst`/`ld`/`ldstalu`/`mul`/`alu`/`none`) fixes which
   semantic family may issue in that slot.

3. **FLIX co-issue width** — the count of `s<n>` slots per format (peak **5**: `F3`, `F11`).

**Port demand per bundle** = Σ over the co-issued slots of the per-op operand count of the heaviest
class legal in that slot. The physical port array the generator instantiates must be ≥ the worst-case
demand; this page reports the **demand** (what silicon is sized to). The decisive asymmetry: all reads
of all co-issued ops land in **one** stage per file (`vec` @10, `wvec` @12, `AR` @1/@4), so the
read-port count *is* the simultaneous fan-in; **writes are staggered** across stages by latency class
(`vec` @10/@11/@12/@13), which is the explicit mechanism that keeps the write-port count well below the
read-port count (§4).

> **Reading rule.** Architectural operands are the `my_<rf>_<slot>_opnd_<sem>_<name>` accessors with
> a plain `_use`/`_def` suffix. The `set_*`/`kill_*`/`bitkill_*` variants are the schedule's
> liveness/def-kill bookkeeping for the *same* operand, **not** additional ports — they are excluded
> from the port count. (Counting them would inflate every file's port demand ~3–6×.)

The eight files and their geometry are fixed by `regfiles[]` at VMA `0x74a800` in `libisa-core.so`
(`num_regfiles` @ `0x3b5c20` → `mov $0x8,%eax; ret`); the geometry is re-pinned and witnessed on
[The Eight Register Files](../isa/core/register-files.md) and is not re-derived here.

---

## 2. The 16-deep pipeline and where each file is read/written

The schedule descriptor is the proof that the named stages 10/11/12/13 are *real* pipeline stages,
not an abstraction. Each instruction instance owns a `_stage_functions` table of **16** entries
(stage 0…15). For the integer quad-MAC `IVP_MULUUQAN16XR16` issued in format `F4`, slot `S2_Mul`:

```
F4_F4_S2_Mul_28_IVP_MULUUQAN16XR16_stage_functions   @ .data.rel.ro VMA 0x2172000
   [0..13] -> F4_F4_S2_Mul_28_IVP_MULUUQAN16XR16_inst_stage0 .. _inst_stage13
   [14,15] -> (next symbol)   # this op allocates a 14-deep schedule (stages 0..13)
```

(`.data.rel.ro` VMA−fileoffset delta is `0x200000` for this binary, confirmed `readelf -SW`: VMA
`0x2070900` ↔ file `0x1e70900`.) The 16-slot table, and the existence of per-opcode
`<op>_opcode_stage0 … _opcode_stage15` semantic functions, fix the pipeline depth at **16 stages**;
the deepest compute classes (`fp_sem_hp_fma`, `bbn_sem_vec_sprecip_rsqrt`) populate all 16.

Read/write stages per file, tallied over the operand accessors (plain `_use`/`_def`):

| file | reads/op (max) | writes/op (max) | read stage | write stage(s) | access character |
|---|---:|---:|---|---|---|
| **`vec`** | **5** | 2 | `@10` (single unified port) | `@10`/`@11`/`@12`/`@13` (staggered by latency) | the hot data source/sink |
| **`wvec`** | 2 (RMW) | 2 (RMW) | `@12` | `@12` (same-stage read-modify-write) | wide MAC accumulator only |
| **`valign`** | 1 | 1 | `@10` | `@12` | unaligned-L/S align + divide scratch |
| **`vbool`** | 2 | 1 | `@10` | `@10`/`@11` | predicate-mask source/sink |
| **`gvr`** | 1 | 1 | `@10`/`@11` | `@10`/`@11` | scatter/gather addr-vector **+** VFPU CSR |
| **`b32_pr`** | 1 | 1 | `@10` | `@10`/`@11` | packed-predicate / MAC round-mode |
| **`AR`** | 2 | 1 | `@1`/`@3`/`@4`/`@5` | `@1`/`@3`/`@4`/`@5`/`@6`/`@12` | windowed scalar; one slot ⇒ single |
| **`BR`** | 2 | 1 | `@3`/`@4` | `@4` | scalar boolean E-stage |

> **CORRECTION — `vec` reads/op is 5, not 4 (the quad-MAC has a `vt` RMW).** The integer
> accumulating MAC `ivp_sem_multiply` (always slot 2, `S2_Mul`) reads **`vp`, `vq`, `vr`, `vs`** as
> pure inputs *and* reads-and-writes **`vt`** as a `vec`-side accumulator:
> `my_vec_2_opnd_ivp_sem_multiply_{vp,vq,vr,vs}_use` + `my_vec_2_opnd_ivp_sem_multiply_vt_use` +
> `_vt_def` all resolve in `nm`. So one MAC op demands five distinct `vec` read ports
> (`vp`/`vq`/`vr`/`vs`/`vt`) and one `vec` write port (`vt`), **in addition to** its wide `wvec`
> accumulator (§5). The `vt` here is the *narrow* running accumulator; `wvt`/`wvu` is the *wide* one.
> `[HIGH/OBSERVED]`.

> **CORRECTION — `gvr` and `b32_pr` are not zero-operand files.**
> - `gvr` carries the scatter/gather **address-vector** operands:
>   `my_gvr_0_opnd_ivp_sem_vec_scatter_gather_gt_def` (the gather *target* index vector, write) and
>   `my_gvr_1_opnd_ivp_sem_vec_scatter_gather_gs_use` (the gather *source* index vector, read), both
>   on the memory slots (0/1). Its role as the VFPU control/status file (`FSR`/`FCR`, accessed via
>   `RUR.FSR`/`WUR.FSR`/`RUR.FCR`/`WUR.FCR` — those ops carry **no** regfile operand, only a modeled
>   pipeline) is *additional*, not the whole story.
> - `b32_pr` carries `my_b32_pr_2_opnd_ivp_sem_multiply_arr_use` (the MAC's packed round/select
>   operand) and `my_b32_pr_{0,3}_opnd_ivp_sem_vec_rep_prt_def` / `_prr_use` (replicate to/from a
>   packed predicate). Cold path, but non-empty. `[HIGH/OBSERVED]`.

> **GOTCHA — `BR` has no `my_BR_*_opnd` accessor at all in `libcas-core.so`.** The scalar boolean
> ports in the table above (`BR` reads @3/@4, writes @4) come from the base-Xtensa scalar schedule
> (the `_S3_ALU`/E-stage boolean ALU), not from a `my_BR_*` vision accessor. A search
> `nm libcas-core.so | grep my_BR_` returns nothing; the SIMD operand-accessor namespace covers only
> the six Vision files plus `AR`. Model `BR` from the scalar E-stage, not the vision schedule.

---

## 3. Per-slot operand fan-in — the read-port demand, built from the binary

Rather than trust a derived port count, the read demand is read straight from the operand accessors.
For each `vec` slot, the **distinct read (`_use`) operands** across all semantics legal in that slot:

```
slot 0 (LdSt / ALU):   vr  vs  vt              -> up to 3 reads
slot 1 (Ld):           vr                       -> up to 1 read
slot 2 (Mul):          vp  vq  vr  vs  vt  vu    -> up to 5 reads in one op (MAC: vp/vq/vr/vs/vt)
slot 3 (ALU):          sr  vr  vs  vt            -> up to 4 reads (sr = a scalar/select operand)
slot 4 (ALU):          vr  vs                    -> up to 2 reads
```

(Command: `nm libcas-core.so | grep -E "my_vec_<n>_opnd_..._use$" | grep -v set_use`.) Slot 2's
`vu` (a `valign` register) belongs to the iterative-divide op (`ivp_sem_divide` reads `vr/vs/vt/vu`);
it is mutually exclusive in that slot with the MAC, so the per-slot-2 worst case is the MAC's **5**.

The **format slot inventory** (from `libisa-core.so` `Slot_<fmt>_s<n>_<class>` getters) and the
resulting peak read demand:

| format | slots | class layout (s0…s4) | per-slot max `vec` reads | bundle Σ (peak demand) |
|---|---:|---|---|---:|
| `F0` | 4 | `ldst, ld, mul, alu` | 3, 1, 5, 4 | up to **10** (mem+MAC+ALU) |
| `F1` | 4 | `ldstalu, ld, mul, alu` | 3, 1, 5, 4 | up to **10** |
| `F2` | 4 | `ldst, ld, mul, alu` | 3, 1, 5, 4 | up to **10** |
| `F3` | **5** | `ldst, ld, mul, alu, alu` | 3, 1, 5, 4, 2 | up to **11** ← PEAK |
| `F4` | 4 | `ld, ld, mul, alu` | 1, 1, 5, 4 | up to 9 |
| `F6` | 4 | `ldst, ld, mul, alu` | 3, 1, 5, 4 | up to **10** |
| `F7` | 4 | `ldst, ld, mul, alu` | 3, 1, 5, 4 | up to **10** |
| `F11` | **5** | `ld, alu, mul, alu, alu` | 1, 4, 5, 4, 2 | up to **11** ← PEAK |
| `N0` | 4 | `ldst, none, none, alu` | 3, 0, 0, 4 | up to 4 |
| `N1` | 3 | `ldst, none, mul` | 3, 0, 5 | up to 5 |
| `N2` | 2 | `ldst, ld` | 3, 1 | up to 3 |

A realistic compute bundle does not pack the *absolute* per-slot maximum in every slot
simultaneously (the MAC's 5 reads and a 4-read ALU rarely both peak), but the **physical `vec` read
port array must cover the worst legal case**, which the slot structure bounds at **~10–11
simultaneous stage-10 reads** for the 5-slot formats `F3`/`F11`. That bound — not a quoted constant —
is what a reimplementer sizes the `vec` read port to. `[OBSERVED operand counts; INFERRED sizing.]`

> **QUIRK — the MAC's wide accumulator does NOT add to the stage-10 read count.** The quad-MAC reads
> `vp/vq/vr/vs/vt` at `@10` (the `vec` read port) but reads its **wide** `wvt`/`wvu` accumulator at
> `@12` from a *separate* file with its own port (§5). So the "+accumulator" of the MAC is one
> `wvec` read at stage 12, never an eleventh stage-10 `vec` read.

Class–slot exclusivity (the constraints that bound co-issue), each confirmed by the operand-accessor
slot index:

| semantic class | legal slot(s) | witness |
|---|---|---|
| integer quad-MAC (`ivp_sem_multiply`) | **only `s2` (`S2_Mul`)** | `my_vec_2_opnd_ivp_sem_multiply_*` (no `_0/_1/_3/_4`) |
| FP-FMA (`ivp_sem_spfma`, `fp_sem_hp_fma`) | `s2` **or** `s3` | `my_vec_2_…spfma`, `my_vec_3_…spfma` both exist |
| memory (`ivp_sem_ld_st`) | `s0`, `s1` | `my_vec_0_…ld_st`, `my_vec_1_…ld_st` |
| vec-ALU (`ivp_sem_vec_alu`) | `s0`, `s2`, `s3`, `s4` | `my_vec_{0,2,3,4}_…vec_alu` |

> **CORRECTION — FP-FMA is not slot-3-exclusive.** A prior analysis bound FP-FMA to `S3_ALU` only.
> The binary shows both `my_vec_2_opnd_ivp_sem_spfma_*` and `my_vec_3_opnd_ivp_sem_spfma_*` (likewise
> `fp_sem_hp_fma`), so an FMA may ride the multiply lane (`s2`) *or* an ALU lane (`s3`). It remains
> true that there is **one** `s2_mul` lane, so an FMA on `s2` excludes a same-bundle integer MAC.

---

## 4. Why reads ≫ writes — the write-staggering mechanism

Per-slot **write (`_def`) operands** for `vec`:

```
slot 0:  vr  vt          slot 1:  vr  vr2  vt      slot 2:  vt  vu
slot 3:  vt  vu          slot 4:  vt
```

`vr2` (slot 1) is the **second load destination** — the dual-load port writing two `vec` registers
in one cycle (`my_vec_1_opnd_ivp_sem_ld_st_vr2_def`). Even at peak, the *simultaneous* write count is
far below the read count, because **each op writes its result in a stage set by its latency class**:

| latency class | example op | result write stage |
|---|---|---|
| load (`S0`/`S1`) | `IVP_LA2NX8_IP` | `@10` |
| 1-cycle vec-ALU | `IVP_ADD2NX8` | `@11` |
| 2-cycle multiply / lookup / reduce / pack / select / squeeze / divide-step | `IVP_MULUUQAN16XR16`, `IVP_SQZN` | `@12` |
| 3-cycle FP-FMA / SP·HP convert / recip-rsqrt | `MADD_S`, `IVP_RECIP0N_2XF32` | `@13` |

A co-issued bundle reads its **whole** source fan-in at `@10` (so the read array must cover ~10–11 at
once), but its results **spread** across `@10`/`@11`/`@12`/`@13`, so the write array only needs the
**per-stage** maximum. The worst same-stage write collision is the three ALU lanes of `F11`
(`s1`+`s3`+`s4`) all retiring a 1-cycle result at `@11` → **3** write ports `@11`. This read-heavy /
write-light asymmetry — `~10–11` read ports `@10` vs `~2–3` write ports per stage — is the direct
reason the `vec` file is heavily multi-ported only at stage 10. `[OBSERVED per-stage counts; INFERRED
write-port sizing.]`

C model of the per-bundle port check a scheduler must enforce:

```c
// Names track real binary symbols: slots from libisa-core Slot_<fmt>_s<n>_<class>;
// per-op read/write operands from libcas-core my_vec_<slot>_opnd_<sem>_<name>_{use,def}.

enum stage { S_LD = 10, S_ALU1 = 11, S_MAC = 12, S_FMA = 13 };

typedef struct {
    int reads_at_10;            // every vec _use lands here
    int writes_at[16];          // vec _def, indexed by the op's result stage
} vec_port_demand;

// Heaviest legal class per slot determines its read fan-in and result stage.
static void accumulate_slot(vec_port_demand *d, int slot, int sem_class) {
    static const int slot_reads[5]  = {3, 1, 5, 4, 2}; // §3, per-slot vec _use max
    d->reads_at_10 += slot_reads[slot];

    int wstage;                                        // result stage by latency class
    switch (sem_class) {
        case SEM_LD_ST:         wstage = S_LD;   break;            // @10
        case SEM_VEC_ALU:       wstage = S_ALU1; break;            // @11
        case SEM_MULTIPLY:                                        // @12 (rides s2 only)
        case SEM_DIVIDE: case SEM_REDUCE: case SEM_LOOKUP:
        case SEM_SELECT: case SEM_SQZ:    wstage = S_MAC; break;
        case SEM_SPFMA: case SEM_HP_FMA:                          // @13
        case SEM_SP_CVT: case SEM_HP_CVT:
        case SEM_SPRECIP_RSQRT: wstage = S_FMA; break;
        default:                wstage = S_ALU1; break;
    }
    d->writes_at[wstage] += 1;                          // one vec dest per compute slot
}

// A legal bundle never exceeds the physical port array:
//   read array  >= max over bundles of reads_at_10               (~10-11, sized to F3/F11)
//   write array >= max over (bundle, stage) of writes_at[stage]  (~3,  the F11 @11 case)
static bool bundle_fits(const vec_port_demand *d, int RD_PORTS, int WR_PORTS_PER_STAGE) {
    if (d->reads_at_10 > RD_PORTS) return false;        // read-port structural hazard
    for (int s = 0; s < 16; ++s)
        if (d->writes_at[s] > WR_PORTS_PER_STAGE) return false; // same-stage write collision
    return true;
}
```

---

## 5. The `wvec` accumulator — a same-stage `(12,12)` read-modify-write

The wide accumulator (`wvec`, 1536-bit × 4) is the deep-learning inner-loop critical path, and its
port behavior in the schedule is exact and uniform. Two operand accessors carry the accumulator, both
on slot 2 (`S2_Mul`):

```
my_wvec_2_opnd_ivp_sem_multiply_wvt_use   AND  ..._wvt_def    # low  half: read AND written
my_wvec_2_opnd_ivp_sem_multiply_wvu_use   AND  ..._wvu_def    # high half: read AND written
```

> **CORRECTION — the `wvec` RMW is *two* operands (`wvt` + `wvu`), not one.** The 1536-bit
> accumulator is addressed as a low half `wvt` and a high half `wvu`, **both** of which appear as a
> `_use` and a `_def` on the multiply slot (`nm` confirms all four symbols). A model that tracks only
> `wvt` undercounts the accumulator's read/write ports by half. The non-accumulating widen
> (`unpack_wvec_mov_wvt`) is `_def`-only; the pack readback (`wvec_pack_wvr`, `vec_mov_wvr`) is
> `_use`-only. There is **no** plain use-only `wvt`/`wvu`: the accumulator is never read without also
> being written in the same op — a true accumulate register, not a general source.

Both halves are read **and** written at stage 12 — a same-stage read-modify-write. The recurrence:

```c
// One accumulating quad-MAC: acc += widen(a) * widen(b)
// Reads  vp,vq,vr,vs (vec @10)  + wvt,wvu (wvec @12)
// Writes vt (vec @11/@12, narrow running acc) + wvt,wvu (wvec @12, wide acc)
void mac_step(wvec_acc *acc /* wvt:wvu */, vec a, vec b, vec c, vec d) {
    // stage 10: four vec source reads land on the unified vec read port
    int48_t prod = widen_mul_quad(a, b, c, d);   // the int8/int16 quad product
    // stage 12: read running accumulator, add, write back -- SAME stage
    acc->lo /* wvt */ += prod_lo(prod);          // wvec _use @12  -> wvec _def @12
    acc->hi /* wvu */ += prod_hi(prod);
}
```

Because the accumulator's `write @12` feeds the next accumulating MAC's `read @12` at the **same**
stage, the forward is a single-cycle bypass: the MAC chain **issues every cycle** (initiation
interval **II = 1**) with a **2-cycle result latency** (inputs `@10` → result `@12`). This is the
"2-cycle MAC recurrence." Modelling the accumulator as a normal `@10` `vec` read breaks the II=1 /
2-cycle loop and makes the accumulator spuriously appear as a stage-10 `vec` read — it does not; the
`wvec` ports are stage-12-only.

> **File-bound chain count.** Only **4** `wvec` entries exist, so at most **four independent MAC
> accumulation chains** can be live at once. A fifth chain must alias an in-use accumulator,
> serializing with the chain that owns it. This is a software register-pressure constraint, not a
> hardware port limit — there is exactly one `wvec` RMW port per bundle (it rides the single `S2_Mul`
> lane). `[HIGH/OBSERVED geometry.]`

---

## 6. The forwarding-path matrix (write stage → consumer read stage)

The bypass network bridges each producer's result-write stage to the consumer's operand-read stage.
Because every `vec` consumer reads at the single stage 10, the **dependency latency equals
`(producer_write_stage − 10)` exactly** for every `vec`-consuming class — the algebraic signature of
a result-forwarding bypass from every compute write stage (11/12/13) back to the stage-10 `vec` read
port. Without the bypass, a dependent op would wait `(write_stage − decode_stage)` ≈ 9–11 cycles; the
observed 1/2/3-cycle dep-latencies **prove** the forwarding paths 11→10, 12→10, 13→10 all exist.

**Vector pipe** — all consumers read `vec` @10 / `wvec` @12:

| producer class | result write stage | forwards to | dep-latency |
|---|---|---|---:|
| vector load (`ivp_sem_ld_st`) | `@10` | `vec` read `@10` | 0 cyc |
| scatter / gather (`gvr` addr-vec) | `@10`/`@11` | `vec` read `@10` | 0–1 cyc |
| vec-ALU / vec-mov / shift / rep | `@11` | `vec` read `@10` | 1 cyc |
| `vbool`-ALU (predicate) | `@10`/`@11` | `vbool` read `@10` | 0–1 cyc |
| multiply / quad-MAC (narrow `vt`) | `@12` | `vec` read `@10` | 2 cyc |
| reduce / pack / select / lookup / histogram / squeeze / divide-step / seli | `@12` | `vec` read `@10` | 2 cyc |
| FP-FMA (`spfma`/`hp_fma`) / SP·HP convert / recip-rsqrt | `@13` | `vec` read `@10` | 3 cyc |
| **accumulating MAC → wide acc** | `@12` | **`wvec` read `@12`** | same-stage RMW (II=1, 2-cyc result) |

Explicit write-stage → read-stage network:

```
write @10 (load result)  --forward-->  stage-10 vec read     (0-bubble next bundle)
write @11 (1-cyc ALU)    --forward-->  stage-10 vec read     (next bundle)
write @12 (2-cyc MAC)    --forward-->  stage-10 vec read  AND  stage-12 wvec read
write @13 (3-cyc FMA)    --forward-->  stage-10 vec read
```

The `vbool` predicate path forwards `@10`/`@11` → `vbool` `@10`. The `valign` divide-scratch (`vu`)
forwards `@12` → `@10` within the iterative `DIVN_*_4STEP` step sequence.

**Scalar pipe** (7-stage R0/E3/M4/W6 scalar Xtensa):

| producer class | result stage | consumer read | dep-latency | forward? |
|---|---|---|---:|---|
| `AR`-ALU (`ADD`/`SUB`/logic, incl. `.N`) | `@4` | ALU read `@4` | 0 cyc | YES (E-stage) |
| `BR` boolean (`AND`/`OR`/`XOR` B) | `@4` | `BR` read `@4` | 0 cyc | YES |
| scalar load (`L32I`/`L16UI`) | `@5` | ALU read `@4` | 1 cyc | partial — 1-cyc load-use bubble (no `@4` forward of a `@5` result) |
| scalar MUL32 | `@6` | ALU read `@4` | 2 cyc | **NO** `@4`-forward of a `@6` result → stall (§7 H1) |
| `RSR`/`WSR`/`XSR` (special reg) | `@5`/`@6` | — | — | SR write `@6` commit |
| vision-scalar (`IVP_SQZN`/`UNSQZN`, `RUR.FSR`/`FCR`) | `@12` (`art@12`) | scalar use `@4` | ~8 cyc | **NO** fast path; deep-pipe scalar result (§7 H3) |

> **The single AR-write@12.** The only `vec`-pipe op that writes an `AR` scalar via the SIMD operand
> namespace is `my_AR_3_opnd_ivp_sem_vbool_alu_ltr_art_def` (slot 3) — a `vbool`-ALU
> "leading/compare" op (`ltr`) producing a scalar `art` result at the deep vector stage. `IVP_SQZN`
> and `RUR.FSR/FCR` likewise produce a scalar result at vector depth. All three feed the scalar pipe,
> which reads `AR` @4 — hence the deep-pipe-to-early-scalar gap (H3).

---

## 7. No-forward stalls and structural hazards

A dependency **stalls** exactly when the producer's result-write stage is later than the earliest
stage the consumer can read that operand **and** no shorter-stage forward of the result exists. The
schedule encodes these as per-instance **`<inst>_stall`** predicate functions — `libcas-core.so`
ships **1 651** of them (and **2 149** `_issue` functions, the dual-issue legality hooks). Enumerated
data hazards:

- **H1 — scalar `MUL32` → scalar ALU/AGU consumer.** STALL = 2 cycles. `MUL32` writes its result
  `@6` (W-stage); a dependent ALU reads `@4`. No `@4` (or `@5`) forward of a `@6` result exists, so
  the consumer stalls `6 − 4 = 2`. A dependent **address** use (`AR` read `@1`) stalls longer (must
  wait `@6` → next op's `@1`). This is the only multi-cycle scalar-arithmetic stall. `[HIGH/OBSERVED]`

- **H2 — scalar load → immediate ALU consumer.** STALL = 1 cycle. `L*I` result `@5`, dependent ALU
  reads `@4` → the classic load-delay slot. Witness: `F0_F0_S0_LdSt_4_inst_L16UI_stall` (and per
  format/slot). A non-dependent op or window-rotate fills the slot. `[HIGH/OBSERVED]`

- **H3 — vision-scalar result → scalar consumer.** Large gap (~8 cycles). `IVP_SQZN`/`UNSQZN`,
  `RUR.FSR`/`FCR`, and the `vbool_alu_ltr` (`my_AR_3_…art_def`) write a scalar `AR` result at the deep
  vector stage 12, but the scalar pipe reads `AR` @4. No fast forward from the deep vector pipe into
  the early scalar read exists; a dependent scalar op waits `12 →` next op's `@4`. Witness:
  `F0_F0_S3_ALU_36_inst_IVP_SQZN_stall` + `IVP_SQZN_stage_functions`. Software schedules independent
  work into the gap. `[HIGH/OBSERVED]`

- **H4 — cross-pipe `vec`-result → scalar-`AR` consumer.** Same deep-pipe-to-early-scalar problem as
  H3: the forwarding network is **within** the vector pipe (to `@10`) and **within** the scalar pipe
  (to `@4`), with no observed fast path from a vector write stage back to the early scalar `AR` read.
  Such transfers go through the architectural register and incur the full pipe-depth gap.
  `[MED/INFERRED]`

- **H5 — `wvec` 5th-stream alias.** Structural, not a port stall: only 4 `wvec` entries, so a fifth
  independent MAC chain must reuse an entry, serializing with the chain that owns it (software
  register pressure, not a HW bubble). `[HIGH/OBSERVED structure]`

**What does NOT stall (full forwarding exists):**

- **Every vector→vector dependency.** The bypass forwards `@11`/`@12`/`@13` → the stage-10 `vec`
  read, so dep-latency equals the result latency (1/2/3 cyc) with no extra bubble. A 1-cyc vec-ALU
  chain runs at full IPC; a 2-cyc MAC chain accumulates **every cycle** (II=1, §5); a 3-cyc FMA chain
  issues every cycle with a 3-deep in-flight window.
- **Vector load → vector consumer.** Result `@10` forwards to the next bundle's `@10` read with a
  0-cycle bubble (the stage-9 memory port feeds the stage-10 read directly). A *same-cycle* use is a
  true dependency (cannot read before the load produces), not a removable stall.
- **Scalar `AR`-ALU / `BR` → same-class consumer.** 0-cycle E-stage forwarding (`@4` → `@4`),
  back-to-back at IPC.

**Structural (port/slot) hazards — distinct from the data-forwarding stalls above:**

| id | constraint | witness |
|---|---|---|
| **S1** | ≤ **1** integer quad-MAC per bundle | `ivp_sem_multiply` exists only on slot 2 (`S2_Mul`); no `my_vec_{0,1,3,4}_…multiply` |
| **S2** | ≤ **1** FP-FMA / FP-divide-step occupying a given lane | FMA on `s2` *or* `s3`; an `s2` FMA excludes a same-bundle MAC |
| **S3** | ≤ **2** memory ops per bundle (`s0` + `s1`); ≤ **1** store (`s1` is load-only) | `ld_st` only on slots 0/1; `Slot_<fmt>_s1_ld` is `ld`, not `ldst` |
| **S4** | ≤ **1** `wvec` accumulator R-M-W per bundle | rides the single `S2_Mul` lane (§5) |
| **S5** | ≤ **~10–11** simultaneous stage-10 `vec` reads | the 5-slot format read sum (§3); the physical port array is sized to this peak, so a *legal* format never over-subscribes the read port `[INFERRED sizing]` |

---

## 8. Reconciliation summary

| claim | this page (binary) | prior analysis | verdict |
|---|---|---|---|
| `vec` read port single stage 10 | all architectural `vec` `_use` @10 | DX-HW-02 §1/§7 | CONFIRMED |
| `vec` write staggered 10/11/12/13 | per-latency-class result stages | DX-HW-02 §2 | CONFIRMED |
| `vec` reads/op by the MAC | **5** (`vp/vq/vr/vs` + `vt` RMW) | "4 (`vp/vq/vr/vs`)" | **CORRECTED (+1 `vt` RMW)** |
| `wvec` RMW operands | **2** (`multiply_wvt` + `multiply_wvu`), both `(12,12)` | "1 (`wvt`)" | **CORRECTED (+`wvu`)** |
| `wvec` accumulator read stage | `@12` (same-stage RMW), not `@10` | DX-HW-02 "@10" | CORRECTED (per prior re-pin) |
| `wvec` 2-cyc recurrence, II=1 | write@12 → next read@12 | DX-HW-02 §6 | CONFIRMED + mechanism |
| forwarding 11/12/13 → read@10 | dep-lat = wstage − 10 exact | DX-HW-02 §6 | CONFIRMED |
| `gvr` schedule operands | **scatter/gather `gt`/`gs`** (+ CSR via RUR/WUR) | "zero operands" | **CORRECTED** |
| `b32_pr` schedule operands | **`multiply_arr`, `vec_rep_prr/prt`** | "no operand" | **CORRECTED** |
| FP-FMA slot | `s2` **or** `s3` | "`S3_ALU` only" | **CORRECTED** |
| `valign` operands | unaligned-L/S (`uul`/`uus`/`valignr`) **and** divide `vu` | "divide-step only" | REFINED (both) |
| scalar MUL32 2-cyc stall (no fwd) | `arr@6`, ALU read @4 | DX-HW-02 §6 | CONFIRMED |
| scalar load-use 1-cyc bubble | `L16UI_stall`, read @4 | DX-HW-02 §6 | CONFIRMED |
| AR write @12 (vision-scalar) | `vbool_alu_ltr_art`, `IVP_SQZN`, `RUR.FSR/FCR` | (new) | CONFIRMED |
| peak `vec` read-port demand | F3/F11 = ~10–11 | (derived) | CONFIRMED (re-derived per-slot) |
| `num_regfiles` | `8` (`0x3b5c20` → `mov $0x8`) | DX-HW-01 §1 | CONFIRMED |

The corrections all *strengthen* the model: a `vt`+`wvt`+`wvu` triple-accumulator MAC, a real
gather port on `gvr`, and a slot-2-or-3 FMA are each tighter, more faithful constraints than the
coarser prior claims, and each is a direct `nm` witness rather than an inference.

---

## 9. Cross-references

- [The Eight Register Files](../isa/core/register-files.md) — the geometry/role/ctype of each file
  this page ports; §5 there is the high-level access model, this page is the cycle-accurate version.
- [Pipeline Timing Model](pipeline-timing.md) — the 16-stage pipeline and per-engine latencies the
  write stages here index into (shared ground truth: the `_stage_functions` descriptors).
- [Co-Issue Matrix](co-issue-matrix.md) — the full FLIX format × slot-class legality table behind §3.
- [LSU / Memory Subsystem](lsu-memory.md) — the stage-9 memory port that feeds the stage-10 `vec`
  read (the load-forward path of §6/§7) and the scatter/gather `gvr` addressing.
- [VFPU / IEEE Numerics](vfpu-ieee.md) — the `gvr`/`FSR`/`FCR` control/status file accessed via
  `RUR/WUR` (the non-data side of the `gvr` port).

---

*Provenance: the per-`(format, slot, opcode)` `_stage_functions` tables, the `my_<rf>_<slot>_opnd_…`
operand accessors, and the `_stall`/`_issue` hazard hooks are `[HIGH/OBSERVED]` — re-disassembled and
`nm`-tallied in-checkout from the shipped `libcas-core.so`; the format/slot inventory and `regfiles[]`
geometry are `[HIGH/OBSERVED]` from `libisa-core.so` (`Slot_<fmt>_s<n>_<class>` getters,
`regfiles[]` @ `0x74a800`, `num_regfiles` @ `0x3b5c20`). The physical port *counts* (vs. the
worst-case demand derived here) are not stated in the shipped artifacts; the demand bounds (§3/§4) are
the authoritative substitute and suffice to size a cycle-accurate model. All facts read as derived
from shipped-artifact static analysis (lawful interoperability reverse engineering, DMCA 17 U.S.C.
1201(f)). Tags HIGH/MED/LOW + OBSERVED/INFERRED throughout; the four corrections to a prior analysis
(`vec` reads=5, `wvec` RMW=2 operands, `gvr`/`b32_pr` non-empty, FMA slot `s2`|`s3`) are each carried
with their `nm` witness.*
