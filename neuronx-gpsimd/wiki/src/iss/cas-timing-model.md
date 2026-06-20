# libcas-core — The Cycle/Pipeline Timing Model

> **Scope.** This is the **timing half** of the cas/fiss reference oracle. `libcas-core.so` decodes a FLIX bundle, schedules every operand read and write on a 32-deep pipeline-stage ring, runs a latency-aware RAW-hazard scoreboard with a forwarding window, enforces the structural writeback-port and multi-slot-dispatch interlocks, and steps one cycle per `dll_cycle_advance` tick. It computes **zero element values** — those are produced by `libfiss-base` and the host TIE-port callbacks ([`fiss-datapath-oracle.md`](./fiss-datapath-oracle.md), referenced by title where the file is a stub). A reimplementer diffs their **cycle counts and stall decisions** against this page; they diff their **element values** against the fiss page. The two are orthogonal and were read from the same binary.
>
> **Binary.** `libcas-core.so` — 45,878,080 bytes, ELF64 x86-64, **not stripped**, GCC 4.9.4. Identity `dll_get_version → 0x1381f`, `dll_get_data_size → 0x4a09f0` (4,851,184 B per-instance state). Modeled core = **Cayman / Vision-Q7 IVP VLIW** (FLIX) on Xtensa. All addresses below are file offsets into that image; `.text`/`.rodata` are VMA==fileoffset, `.data.rel.ro`/`.data` carry a **+0x200000** VMA→fileoffset delta (subtract before `xxd`). Sibling surface/ABI facts: [`cas-core-surface.md`](./cas-core-surface.md).

---

## 0. Executive finding — what the timing model *is*

`libcas-core` is a **cycle-accurate interlock simulator with no datapath math**. The decisive evidence is negative and total: the entire 45 MB image contains **not one packed/SIMD arithmetic instruction** — a full-disassembly sweep for `paddw/psubw/pmullw/pmaxsw/pminsw/paddd/paddb/vpadd*` returns **0**. `[HIGH, OBSERVED]` cas therefore cannot and does not compute lane values. It computes exactly five things:

1. per-bundle **instruction decode** (op0-nibble length table + 47-record slot dispatch);
2. the **per-operand read/write schedule** on a 32-deep pipeline-stage ring;
3. a **latency-aware RAW-hazard scoreboard** with a bypass/forwarding window;
4. **structural hazards** (a 6-lane writeback buffer + multi-slot-dispatch interlocks);
5. the **one-tick cycle advance** that commits pending writes and shifts the ring.

Element values are delegated to the host via **119** `nx_*_interface` TIE-port callbacks (`nm -D | rg -c 'nx_.*_interface' == 119`, `[HIGH, OBSERVED]`); the host datapath fires at the execute stage (stage 10).

The split, proven from both sides:

| `libcas-core` (this page) | `libfiss-base` (fiss value oracle) |
|---|---|
| cycles / hazards / decode / issue | decode + architectural element **values** |
| 0 packed-arith; 0 value math | real per-lane add/sub/min/max/softfloat |
| 119 `nx_*` host-math callbacks | 0 host-math callbacks (self-contained) |
| 32-deep stage ring + scoreboard | stateload→regload→opcode→writeback |

> **NOTE.** Read this page alongside the microarch view of the same machine — [`../uarch/pipeline-timing.md`](../uarch/pipeline-timing.md) ("Pipeline Timing Model") frames these stages as a 7-stage scalar / 10-15 vector pipe; [`../uarch/co-issue-matrix.md`](../uarch/co-issue-matrix.md) ("FLIX Co-Issue Matrix") gives the bundle widths; [`../uarch/regfile-ports.md`](../uarch/regfile-ports.md) ("Register-File Port Model + Bypass Network") gives the read/write-port fan-in. This page is the **ISS-internal** restatement: the exact symbols, offsets, and `mov $LAT,%esi` immediates the simulator actually executes. The capstone that fuses cas timing + fiss values is [`iss-oracle-synthesis.md`](./iss-oracle-synthesis.md).

---

## 1. The pipeline stage model — a 32-deep stage RING

The pipeline is a **circular ring of 32 stage-records**. Every timing routine indexes it as `(base_head + distance) & 0x1f`, stride 4 bytes per scoreboard column. This `& 0x1f` masking appears **identically** in `dll_cycle_advance`, `my_vec_stall`, `my_AR_stall`, and every per-register `_cycle` routine. `[HIGH, OBSERVED]`

**Stage-ring control offsets in the per-instance state struct (arg0 / `%rdi`):**

| Offset | Meaning | Evidence |
|---|---|---|
| `+0x484c` | current pipeline-stage index / ring base (read `& 0x1f` by every scoreboard accessor) | `mov 0x484c(%rdi),%r9d` head of `my_vec_stall` `[HIGH, OBSERVED]` |
| `+0x4844` | per-stage dirty/def bitmask (semantic accessors OR in `0x2000`) | `[HIGH]` |
| `+0x24ec` | **cycle-advance ring head** (`& 0x1f`; decremented 1 per tick) | `mov 0x24ec(%rdi),%eax` in `dll_cycle_advance` `[HIGH, OBSERVED]` |
| `+0x24e4` | cycle-advance enable/guard (tested at `dll_cycle_advance` entry) | `mov 0x24e4(%rdi),%r9d; test %r9d,%r9d` `[HIGH, OBSERVED]` |
| `+0x5a8` | control/flags byte: bit0 event-trace, bit1/2 tie-stall-eval mode, bit3 "cycle advanced" | `orb $0x8,0x5a8(%rdi)` / `andb $0xf7,0x5a8(%rdi)` `[HIGH, OBSERVED]` |
| `+0x8f8` | **scoreboard / regfile commit array** (writes land here `N` stages after issue) | `add $0x8f8,%rax; mov %edx,0x4(%rdi,%rax,4)` in `dll_cycle_advance` `[HIGH, OBSERVED]` |

**Logical depth per slot = 16 stages (stage0..stage15).** There are **157,775** per-(format,slot,mnemonic) `..._inst_stage<0..15>` functions (`nm | rg -c '_inst_stage[0-9]+' == 157775`, `[HIGH, OBSERVED]`). stage0/1 are no-op latches (`repz ret` wrappers); the operand-field nibble decode runs in an early stage; the **execute / host-value callback fires at stage 10** (`esi=$0xa`). The 32-entry ring is the *physical* in-flight buffer; the 16 logical stages map into it. The ring is 2× the logical depth so producer and consumer waves never alias within the interlock window (windows ≤ ~14, §4). `[MED, INFERRED]`

### Per-register pipeline latches — the 81 `my_<SR>_cycle` shift registers

There are **81** `my_*_cycle` routines (`nm | rg -c 'my_.*_cycle' == 81`, `[HIGH, OBSERVED]`): `my_CCOMPARE0_cycle` `@0x176f710`, `my_EPC_cycle` `@0x1766240`, `my_EXCCAUSE_cycle` `@0x1768c60`, `my_IVP_FS0_cycle` `@0x1773050`, and so on across the timer/compare, FP-status, exception, and breakpoint special registers. Each is a **stage-indexed shift register**: keyed on a stage argument (`cmp $0x5/$0x4,%ebp`), it copies a special-register value from stage `N+1` down to stage `N`, so the architectural SR state stays phase-coherent with the timing ring. This is how `CCOMPARE0/1/2`, `IVP_FS0..7`, `EPC`/`EXCCAUSE`/`EXCVADDR` advance exactly one stage per cycle. `[HIGH, OBSERVED]`

### Pipeline squash — `dll_kill_stage`

`dll_kill_stage` `@0x17ae3e0` takes `(from_stage %esi, to_stage %edx)` and **memsets the per-stage records** over `[+0xbf8.., +0xc01..]`, clearing in-flight bits — a flush of a stage range on misprediction / exception / replay. The two `memset` calls are explicit: `lea 0xbf8(%rdi,%r13,1),%rdi; call memset` and `lea 0xc01(%r12,%r13,1),%rdi; call memset`. `[HIGH, OBSERVED]`

---

## 2. The decode / instruction-length model — cycles begin at fetch width

`dll_instruction_get_length` `@0x17b57a0` is three instructions: `lea CSWTCH.4667(%rip),%rax; movsbl (%rax,%rsi,1),%eax; ret`. It indexes a **256-entry signed-byte table @0x17d0640** by the low opcode byte. The table (`xxd -s 0x17d0640`, `.rodata` so VMA==fileoffset) decodes by op0 nibble: `[HIGH, OBSERVED]`

```
017d0640: 03 03 03 03 03 03 03 03   nibble 0x0..0x7 -> 0x03 (3 B : x24 / base 24-bit density)
017d0648: 02 02 02 02 02 02 ...     nibble 0x8..0xD -> 0x02 (2 B : x16a/x16b 16-bit density)
          ... 10 08 ...             nibble 0xE -> 0x10 (16 B : wide FLIX bundle F0..F11)
                                     nibble 0xF -> 0x08 normally, 0x10 on a wide byte3-selector,
                                                   or 0xFF (-1 = ILLEGAL) for two reserved rows
```

`dll_instruction_speculate_length` `@0x17b57b0` uses a parallel 16×int32 table `@0x17d0600` (`xxd`: eight `03`, six `02`, two `00` → `{0..7:3, 8..D:2, E/F:0}`) — the pre-byte3 length for early PC advance / branch-target sizing. `[HIGH, OBSERVED]`

Wall-clock cycle accounting starts from the per-bundle **fetch width**; one FLIX bundle issues per cycle, subject to the issue/stall checks of §3-4. `[MED]`

---

## 3. The FLIX co-issue + structural-hazard model

### Issue grid

A FLIX bundle co-issues up to **4** parallel ops/cycle (wide formats F0/F1/F2/F4/F6/F7) or **5** (F3, F11 — dual/triple-ALU); narrow N0/N1/N2 issue 2-3. The 14 formats span 46 FLIX slots over the units `{LdSt, LdStALU, Ld, Mul, ALU, none(NOP)}`. cas models this as **parallel per-slot dispatch**. Peak issue width = 5; ≤2 memory ops/bundle, ≤1 store/bundle, ≤1 `S2_Mul` lane (see [`../uarch/co-issue-matrix.md`](../uarch/co-issue-matrix.md)). `[HIGH]`

### Three parallel 47-record dispatch tables

The top-level cas issue model is three sibling tables, each **47 records × 32 bytes**, in `.data.rel.ro` (subtract `0x200000` for `xxd`): `[HIGH, OBSERVED]`

| Symbol | VMA | File offset (−0x200000) |
|---|---|---|
| `slot_semantic_functions` | `0x227ecc0` | `0x207ecc0` |
| `slot_issue_functions` | `0x227f2c0` | `0x207f2c0` |
| `slot_stall_functions` | `0x227f8c0` | `0x207f8c0` |

Record layout, decoded by `xxd` of record 0 @file `0x207f2c0`:

```c
struct slot_dispatch_record {           // 32 bytes (NOT 188x8; stride is 32 B)
    const char *format_name;   // +0x00  e.g. "x24","F0"   (rec0 -> 0x17cfcfc "x24")
    const char *slot_name;     // +0x08  e.g. "F0_S3_ALU"  (rec0 -> 0x17cfd00 "Inst")
    uint64_t    slot_position; // +0x10  sequential slot index within the format
    void      **dispatch_array;// +0x18  -> per-(format,slot) {mnemonic, fn} array
};
```

> **GOTCHA.** `dll_get_*_functions` (the ABI accessors, [`cas-core-surface.md`](./cas-core-surface.md)) report these as "1504 B / 188 ptr slots". That is the *legacy 8-byte-stride view*; the true record stride is **32 B / 47 records**. `1504 / 8 == 188` and `1504 / 32 == 47` — same bytes, different lens. Reimplement against **47 records × 32 B**.

### Per-slot dispatch arrays and their census

Each `dispatch_array` is one `{const char *mnemonic, void *fn}` 16-byte record per mnemonic that can issue in that `(format,slot)`. Representative record counts (array size / 16): `[HIGH, OBSERVED]`

| Slot class | Examples (issue/stall arrays) |
|---|---|
| busiest ALU | `F0_S3_ALU` 568, `F1_S3_ALU` 560, `F7_S3_ALU` 552 (semantic arrays larger: `F0_S3_ALU` 848) |
| Mul | `F1_S2_Mul` 452, `F2_S2_Mul` 536, `N1_S2_Mul` 384 |
| Ld | `F0_S1_Ld` 264, `F4_S0_Ld` 196 |
| LdSt | `F0_S0_LdSt` 352, `F7_S0_LdSt` 352 |
| NOP "none" | `N0_S1_None` 2-4 records (narrow-bundle filler, no real issue unit) |

Image-wide totals: **2,149** `*_inst_*_issue` and **1,651** `*_inst_*_stall` functions (`nm | rg -c '_inst_.*_issue' == 2149`, `rg -c '_inst_.*_stall' == 1651`). `[HIGH, OBSERVED]`

> **CORRECTION.** The microarch pages ([`../uarch/regfile-ports.md`](../uarch/regfile-ports.md), [`../uarch/pipeline-timing.md`](../uarch/pipeline-timing.md)) cite "**1746/1748** stall functions". That is a **superset scope**, not a conflict: `nm | rg -c ' [tT] .*_stall' == 1748` counts *every* stall symbol, which is the **1,651 per-mnemonic `*_inst_*_stall` thunks plus the shared `my_*_stall` predicates** (`nm | rg -c 'my_.*_stall' == 93`: the 6 `my_WB_*` lanes, `my_<rf>_stall` per-file interlocks, `my_MS_*`, `my_CPENABLE_stall`, `my_InOCDMode_stall`, etc., minus a handful of non-`t` duplicates). The figure to reimplement is **1,651 per-op stall thunks** (each tail-chaining into the shared predicates).

### Issue function = "admit op, POST its def reservations"

`F0_F0_S3_ALU_36_inst_MOVI_issue` `@0x14b2bd0` shows the canonical body verbatim: `[HIGH, OBSERVED]`

```c
// F0_F0_S3_ALU_36_inst_MOVI_issue @0x14b2bd0  (annotated from disasm)
void MOVI_issue(state *s, slotword *w) {
    reserve_hook_base = s->fn_158c0;                 // mov 0x158c0(%rdi),%rbp
    dst = (w->raw << 0x18) >> 0x1c;                  // shl $0x18 ; shr $0x1c  = 4-bit dst nibble
    addr = opnd_sem_AR_addr(s->reg_14eb0, dst);      // call opnd_sem_AR_addr ; rdi = 0x14eb0(rbx)
    esi  = 0x1;                                       // mov $0x1,%esi   <-- AR def-post LATENCY
    reserve = s->fn_158c0->slot_158c8;               // mov 0x158c8(%rdi),%r12
    reserve(s->reg_14eb0, addr, /*lat=*/esi);        // call *%r12  -> POSTS the scoreboard reservation
}
```

Three offsets carry the split, all directly observed in this one function: **`+0x158c0`** = the issue-time reserve-hook *base*, **`+0x158c8`** = the reserve *method pointer* loaded from it and called, **`+0x14eb0`** = the regfile-STATE base (the **timing** side). The issue routine *writes* the reservation; the stall routine *reads* it.

> **QUIRK.** The reserve hook is reached as `mov 0x158c0(%rdi),%rbp; … ; mov 0x158c8(%rbp-derived),%r12; call *%r12` — i.e. `+0x158c0` is the table, `+0x158c8` the slot. (The host **value** callback uses a *different* base, `+0x15200` — §7. `0x158xx` = cas timing, `0x152xx` = host values. Do not confuse them.)

### Structural hazards (the stall chain)

Every `*_inst_*_stall` tail-chains into the shared predicates: `[HIGH, OBSERVED]`

- **Writeback-port conflict — `my_WB_{S,P,C,N,T,M}_stall`** (six write-buffer lanes; `nm` confirms `my_WB_S_stall` `@0x17782f0`, `_P_` `@0x1778690`, `_C_` `@0x1778a30`, `_N_` `@0x1778dd0`, `_T_` `@0x1779170`, `_M_` `@0x1779510`). `my_WB_S_stall` walks a 6-deep writeback queue: per-stage busy flags at `+0xde4..+0xde9` (`cmpb $0x0,…`), latency counters at `+0xdeb..+0xdf0` (`movzbl`), each compared `cnt - {0,2,3,4,5,6} <= esi -> STALL`. This models the drain of a 6-stage write-buffer: a producer cannot issue if its writeback stage/port is occupied.
- **Multi-slot dispatch — `my_MS_DISPST_stall` `@0x1779c50` / `my_MS_DE_stall` `@0x177be30`** — gating co-issue when the bundle decode/dispatch resource is busy. `[HIGH presence / MED exact rule]`
- **Coprocessor enable — `my_CPENABLE_stall` `@0x178e420`** (`esi=3`) — a vector op stalls/faults at stage 3 if its coprocessor is disabled. `[HIGH, OBSERVED]`
- **Debug halt — `my_InOCDMode_stall` `@0x177c1d0`** (the OCD halt interlock). `[HIGH]`

Each predicate returns **1 ⇒ the op cannot issue this cycle** (re-presented next cycle); 0 ⇒ clear. The issue routine runs only after *all* stall predicates clear.

#### The fp-width 4-port 2:1 structural mismatch

The fp-width converts (fp32↔fp16) occupy **four** structural writeback ports for a 2:1 register-width mismatch — an fp32 result is twice the fp16 register footprint, so it claims multiple writeback slots. `IVP_CVTF16F32_issue` `@0x14b47b0` opens with **four** `mov $0xe,%esi` (the structural horizon arg `$0xe`=14) feeding the WB-port hooks before any operand resolves, then posts the result vec at `mov $0xd` (LAT 13) and the sources at `mov $0xa` (LAT 10). The unsigned `ufloat` variant uses an *extra* structural port versus the signed (see [`cas-convert-pack-fp.md`](./cas-convert-pack-fp.md) §10). `[HIGH, OBSERVED]`

---

## 4. The RAW-dependency scoreboard + bypass/forwarding network

This is the genuine cycle-accuracy core. For each register file there is a `my_<rf>_stall` **consumer interlock** predicate plus reservation records on the 32-deep ring. The six exist as: `my_vec_stall` `@0x17994d0`, `my_AR_stall` `@0x1795bb0`, `my_vbool_stall` `@0x17a2df0`, `my_wvec_stall` `@0x17a7ac0`, `my_valign_stall` `@0x17a6b80`, `my_gvr_stall` `@0x17a9970`. `[HIGH, OBSERVED]`

### Mechanism (decoded from `my_vec_stall` head)

```c
// my_vec_stall @0x17994d0  (annotated; the canonical latency-aware RAW interlock)
// Returns 1 => STALL this cycle (RAW hazard not yet forwardable), 0 => clear.
int my_vec_stall(state *s, int src_reg, int need_lat /*esi*/) {
    src_reg = (uint16_t)src_reg;               // movzwl %dx,%edx
    int ring_base = s->stage_idx_484c;         // mov 0x484c(%rdi),%r9d
    for (int dist = 1; dist <= 14; dist++) {   // r8/r10/r11 = 1 ; loop bound 0xe per histogram
        col = &ring[(dist + ring_base) & 0x1f]; // & 0x1f, stride 4
        // multi-port reservation lanes (>=4): reg#, two valid bits, ready-cycle each
        for (each lane L in {A,B,C,D}) {
            int rsv_reg   = col->reg_no[L];    // +0x5954 / +0x6cd4 / +0x8054 / +0x93d4
            if (!col->valid[L]) continue;      // +0x5a54/0x5b54, +0x6dd4/0x6ed4, ...
            if (rsv_reg != src_reg) continue;  // cmp 0x6cd4(%rcx),%edx
            int ready = col->ready_cycle[L];   // +0x5ad4 / +0x6e54 / +0x81d4 / +0x9554
            int avail = ready - dist;          // sub %r8d,%r14d   (producer distance down the pipe)
            if (need_lat > avail)              // cmp %esi,%r14d
                return 1;                      // RAW hazard: result NOT yet forwardable -> STALL
        }
    }
    return 0;                                  // producer is either retired or already forwardable
}
```

A RAW hazard stalls **only if** the producer's result will not have propagated far enough down the pipe to be **forwarded** by the time the consumer needs it (`need_lat > ready - dist`). This is a **latency-aware bypass/forwarding network**, not a naive "any pending def ⇒ stall". The reservation triple `+0x9554(reg#)/+0x94d4(valid)/+0x93d4(value)` matches the surface page's slot layout exactly. `[HIGH, OBSERVED — sub %r8d,%r14d ; cmp %esi,%r14d over each lane.]`

### The consumer-need argument (`esi`) IS the per-operand latency

The latency is **not table-driven**. It is a literal `mov $LAT,%esi` emitted right after each operand-address resolve and just before the shared scoreboard call. Worked, directly observed, from `IVP_BMAXNX16_issue` `@0x14b52c0`:

```
call opnd_sem_vbool_addr ; mov $0xb,%esi    ; vbool predicate input -> LAT 11
call opnd_sem_vec_addr   ; mov $0xa,%esi    ; vec source           -> LAT 10
call opnd_sem_vec_addr   ; mov $0xa,%esi    ; vec source           -> LAT 10
call opnd_sem_vec_addr   ; mov $0xb,%esi    ; vec dest             -> LAT 11
```

A histogram of `mov $imm,%esi` immediates, keyed by the preceding `opnd_sem_<rf>_addr` resolver, over the full disassembly gives the canonical per-register-file latency table. `[HIGH, OBSERVED]`

#### Per-register-file latency table

| regfile | LAT distribution (count) | dominant USE-LAT | interpretation |
|---|---|---|---|
| AR (scalar windowed) | 1:1430, 2:1, 3:356, 4:855, 5:127, 6:21, 12:41 | **4** (use) | scalar ALU result ~stage 4; 1=def-post, 3=set_def |
| vec (512-bit) | 3:768, 10:3431, 11:225, 12:341, 13:277 | **10** | vector op result @stage 10 (matches execute@10) |
| vbool (predicate) | 3:36, 10:869, 11:141, 12:21 | **10** | predicate result @stage 10 |
| wvec (1536-bit MAC) | 3:88, 10:50, 12:288 | **12** | MAC accumulator retires later (deeper Mul pipe) |
| valign (align/shuf) | 3:75, 9:270, 10:134 | **9** | align result ~stage 9 (one earlier than load) |
| gvr (SuperGather) | 3:8, 9:8, 10:34 | **10** | gather staging ~9-10 |
| b32_pr (b32 pred) | 3:9, 10:114, 12:57 | **10** | b32 predicate @stage 10 |
| BR/BR4/BR8/BR16 | mostly 4 | **4** | Xtensa boolean ~stage 4 |

**Reading the role split:** LAT **1** = the issue-time def-post (producer posts "ready in N"); LAT **3** = the set_def / write-mask / CPENABLE-gate role; LAT **4** (scalar) / **9-13** (vector) = the USE read-latency a consumer requires — how many stages downstream the result is forwardable. Signed/unsigned/saturating variants share the **same** LAT (they differ only by host value-callback, never by cas timing). `[HIGH numbers / MED 1/3/4/10 role labeling]`

#### Canonical op latencies — harmonized with the committed op pages

Every value below is the immediate the simulator actually loads into `%esi`, cross-checked against the sibling op pages (which read the same binary):

| Op class | cas USE/DEF LAT | Source page / symbol | Tag |
|---|---|---|---|
| scalar ALU (AR dst) | result ~4; MOVI def 1 | `MOVI_issue` `0x158c8` reserve | HIGH OBSERVED |
| vector load-use (vec dst) | **10** | [`cas-load-store.md`](./cas-load-store.md) | HIGH CARRIED |
| base AR (load/store address) | **1** | [`cas-load-store.md`](./cas-load-store.md) | HIGH CARRIED |
| store (writeback egress) | **stage 11** | [`cas-load-store.md`](./cas-load-store.md) | HIGH CARRIED |
| vector ALU (ADD/SUB/MIN/MAX/SEL) | **10** read / 11 def | `IVP_BMAXNX16` (vec `$0xa`, def `$0xb`) | HIGH OBSERVED |
| integer MAC (wvec accumulator) | **12** | [`cas-mac-fmac.md`](./cas-mac-fmac.md) | HIGH CARRIED |
| FP FMA (fp16 + fp32) | **10** | [`cas-mac-fmac.md`](./cas-mac-fmac.md) | HIGH CARRIED |
| valign (align/funnel) | **9** | [`cas-valign-shuffle-reduce.md`](./cas-valign-shuffle-reduce.md) | HIGH CARRIED |
| crossbar control read (SEL/SHFL `sr`) | **12** (data 10) | [`cas-valign-shuffle-reduce.md`](./cas-valign-shuffle-reduce.md) | HIGH CARRIED |
| reduce result lane / fold source | **10** result / **12** fold-read | [`cas-valign-shuffle-reduce.md`](./cas-valign-shuffle-reduce.md) | HIGH CARRIED |
| compare result-def (fp / int) | **10** (fp) / **11** (int) | [`cas-predicate-boolean.md`](./cas-predicate-boolean.md) | HIGH CARRIED |
| int→fp & fp-width convert | **13** | [`cas-convert-pack-fp.md`](./cas-convert-pack-fp.md) | HIGH CARRIED |
| fp→int convert (`trunc`) | **12** | [`cas-convert-pack-fp.md`](./cas-convert-pack-fp.md) | HIGH CARRIED |
| predicate (vbool / b32_pr) | **10** | [`cas-predicate-boolean.md`](./cas-predicate-boolean.md) | HIGH CARRIED |

> **CORRECTION (reduce result LAT).** [`../uarch/pipeline-timing.md`](../uarch/pipeline-timing.md) classes `IVP_RADDNX16` as a "stage-12 result" op, while [`cas-valign-shuffle-reduce.md`](./cas-valign-shuffle-reduce.md) posts the reduce **result lane at LAT 10** and the **fold source at LAT 12**. These are not in conflict once you separate operands: the *posted forwardable result lane* is `mov $0xa` (10); the *fold-tree read depth* that gates back-to-back reduces is `mov $0xc` (12). The microarch page's "12" is the **conservative result-availability** (the op's deepest write), the ISS immediate is **per-operand**. A reimplementer must model **both**: post the result at 10, but gate a dependent reduce that reads the fold network at 12. `[HIGH numbers / MED reconciliation]`

> **NOTE (compare def split).** Do not collapse compare to a single latency: fp compare posts its `vbool` result at LAT **10** (`OLEN` → `mov $0xa`), int compare at LAT **11** (`LTNX16` → `mov $0xb`); both read `vec` sources at 10. `[HIGH, CARRIED]`

> **NOTE (valign is the one 9-stage class).** `valign` is the only file with a LAT-9 result (histogram `9:270`), confirmed by `LALIGN_IP` → `mov $0x9`. Store-align (`SAPOS_FP`) drains one stage later at LAT 10. Do not fold valign into the generic 10/11/12/13 vector model. `[HIGH, CARRIED]`

### Per-file interlock-scan window (the bypass horizon)

The max stage-distance each stall predicate scans (`cmp $0xN,%r8d/%ecx` loop bound) correlates with the latency — a producer beyond its window has already retired into the regfile, so no interlock is needed: `[HIGH, OBSERVED bounds / MED correlation]`

| File | Scan window | File | Scan window |
|---|---|---|---|
| vec | 13-14 | wvec (MAC) | 13 (`r8 = 1..0xd`) |
| vbool | 12-13 | b32_pr | 13 |
| valign | 10-11 | gvr | 11 |
| AR (scalar) | up to 13, bypass cutoffs at 5-6 | | |

> **GOTCHA — the structural-horizon arg `$0xe`/`$0xf` is NOT a latency.** Issue functions for FP/convert/predicate ops open with several `mov $0xe,%esi` (14) or `mov $0xf,%esi` (15) feeding the **WB-port / bypass-horizon** hooks (`+0x15648`/`+0x15650` on the predicate page; the four `$0xe` in `CVTF16F32`). These are *structural* occupancy arguments, not RAW use-latencies. Only the immediates that follow an `opnd_sem_<rf>_addr` resolve are real per-operand latencies. Misreading the horizon arg as a latency inflates every FP op by 4-5 stages.

---

## 5. The cycle counter / clock advancement — `dll_cycle_advance`

`dll_cycle_advance` `@0x17aa3c0` (≈16 KB, `0x401d` B) is the **one-tick pipeline stepper**, signature `(state *s /*rdi*/, int dir /*esi*/)`. Decoded head + body: `[HIGH, OBSERVED]`

```c
// dll_cycle_advance @0x17aa3c0   (annotated from disasm)
void dll_cycle_advance(state *s, int dir) {
    int enabled = s->cycle_adv_guard_24e4;        // mov 0x24e4(%rdi),%r9d
    s->flags_5a8 |= 0x8;                           // orb $0x8,0x5a8(%rdi)  -- set "cycle advanced" bit3
    if (!enabled) return;                          // test %r9d,%r9d

    int head = s->ring_head_24ec;                  // mov 0x24ec(%rdi),%eax
    // --- 1. COMMIT pending writes: look-ahead (head + 12), 4 write-back lanes ---
    for (int lane = 0; lane < 4; lane++) {
        // commit-flags +0x25f4/+0x28f4/+0x2bf4/+0x2ef4 ; reg# +0x2574/.. ; value +0x24f4/..
        if (commit_flag[lane]) {                   // mov 0x25f4(%rbp),%r8d ; test
            int reg = reg_no[lane];                // movslq 0x2574(%rbp),%rax
            int val = value[lane];                 // mov 0x24f4(%rbp),%edx
            s->scoreboard_8f8[reg].val = val;      // add $0x8f8,%rax ; mov %edx,0x4(%rdi,%rax,4)
            if (s->flags_5a8 & 0x1) trace_commit(); // bit0 = event-trace hook
        }
    }
    // --- 2. DECREMENT ring head: exactly ONE stage of advance per tick ---
    s->ring_head_24ec = (head - 1) & 0x1f;         // dec & 0x1f
    // --- 3. SHIFT THE RING: copy each stage's reservation columns stage+1 -> stage ---
    for (each stage) {
        stage.col_25f4 = (stage+1).col_25f4;       // mov 0x25f4(%rdx),%r8d ; mov %r8d,0x25f4(%rax)
        stage.col_28f4 = (stage+1).col_28f4;       // ... 0x2674, 0x2774, 0x2574, ...
        stage.col_2bf4 = (stage+1).col_2bf4;       //  all in-flight reservations move 1 stage
        ...                                         //  closer to retirement
    }
    // --- 4. dir (esi) selects forward-commit vs ROLLBACK ---
    // cmp r12d(dir) {==0 / <0 / >0}  => forward vs reverse-direction (checkpoint rollback)
}
```

`dll_reset_cycle_advanced` `@0x17aa3b0` is two instructions — `andb $0xf7,0x5a8(%rdi); ret` — clearing bit3. `[HIGH, OBSERVED]`

The four committed write-back lanes (`+0x25f4/+0x28f4/+0x2bf4/+0x2ef4`, each landing its value into the `+0x8f8` scoreboard/regfile array) correspond to the up-to-4 parallel writes a wide FLIX bundle retires per cycle. The `dir` argument's three-way split (`==0 / <0 / >0`) means the timing model is **reversible / checkpointable** — the harness can step backward, consistent with a debugger-driven cycle ISS. `[HIGH presence / MED rollback rule]`

> **QUIRK — no wall-clock frequency constant exists in the binary.** There is no MHz/GHz/period/picosecond/cycle-time string or numeric constant anywhere in the image (strings + symbol sweep == 0). cas counts **cycles abstractly**; the cycle-time→nanoseconds conversion (clock frequency) is a host/ncore2gp concern, not modeled here. The only per-cycle numeric state is the architectural cycle-counting SRs (`CCOMPARE0/1/2` and their `_cycle` shift routines, §1). `[HIGH, OBSERVED — absence is evidence]`

---

## 6. Memory-access timing

cas models memory **timing** (port occupancy + load-use latency) and delegates the **data** to the host via the **52** memory `nx_*_interface` ports — `Scalar/VectorMemAccess`, `MemDataIs{8..512}Bits_{0,1}`, `VAddr{Base,Index,Offset,Filter,Res}_{0,1}`, `Load_{0,1}`/`Store_0`/`StoreAcc`, `MMUDataIn/Out`, `ScatterData_0`. The `_0/_1` suffix = **two parallel vector memory pipes** (a 2-lane LSU; `XCHAL_NUM_LOADSTORE_UNITS = 2`). `[HIGH from imports / co-issue-matrix]`

What cas times: `[HIGH, OBSERVED via the LdSt/Ld-slot stall chains]`

- **Load-use latency.** A load posts its AR/vec dst with use-LAT **4** (scalar) / **10** (vector) into the scoreboard; a dependent consumer stalls per §4 until the loaded value would be forwardable. The vector memory data-in port returns at stage 9, the vec-reg result posts at stage 10 (next-bundle forwarding, 0-cycle dep-lat — [`../uarch/co-issue-matrix.md`](../uarch/co-issue-matrix.md)).
- **Store / write-buffer occupancy.** The 6-lane `my_WB_*_stall` queue (§3) drains over up to 6 writeback stages; a store retires at the writeback stage (**stage 11**) and a following store stalls when its WB lane/stage is busy — the structural store-port hazard.
- **Load/store fault ports.** `nx_LoadStore{CrossMemAcc,Disable,InvalidTCMAcc,UnsupportedAtomicOp,WrongIramAcc}` gate illegal-access exceptions, surfaced through the `dll_exception_table` `LoadStore*Exception_exc` handlers.

> **GOTCHA — no per-region (SBUF vs DataRAM/TCM vs HBM) latency table in cas.** The address-region distinction and any per-region access-latency differentiation are **not** numeric tables inside `libcas-core`. The region decode + variable memory latency live host-side: the `nx_*MemAccess` ports return when data is ready, and cas models a **fixed load-use LAT of 4/10** plus the WB-port structural occupancy — *not* per-region HBM-vs-SBUF cycle counts. A reimplementer who needs region-aware DRAM latency must add it at the host model, not expect it from cas. `[MED, INFERRED — no per-region latency constant found; cross-check the control/address pages for the region map, which is a host/firmware artifact.]`

---

## 7. The cas-TIMING vs fiss-VALUE split — the per-op handoff

For one vector op the two libraries cooperate as: `[HIGH]`

```
cas:  decode (length table @0x17d0640)
   -> issue admit (stall chain clears: WB_*, MS_*, CPENABLE, OCD)
   -> POST def reservation into the scoreboard at use-LAT (vec=10) via 0x158c0/0x158c8 reserve hook
   -> advance ring one stage per dll_cycle_advance tick
   -> at EXECUTE (stage 10) call the HOST VALUE port:
        mov 0x15200(%rbx),%rdi ; mov $0xa,%esi ; call *0xe7098(%rbx)
   -> dll_cycle_advance COMMITs the host-produced result into the +0x8f8 array N stages later
host/fiss: the nx_*_interface callback computes the actual element values
        (fiss's per-lane add/sub/min/max/softfloat primitives)
```

The cas scoreboard **never inspects the value**; it only schedules the read/write slots by reg# and latency. Signed / unsigned / saturating differ **only** in which host callback fires (distinct decode bit / distinct opcode), **never** in cas timing.

State-struct vtable slots that carry the split (ref-count over the full disassembly): `[HIGH, OBSERVED]`

| Slot | Refs | Role |
|---|---|---|
| `+0x15200` | 10,807 | host **VALUE** callback base (the datapath handoff) |
| `+0x14eb0` / `+0x14eb8` | 8,854 / 855 | internal regfile-STATE bases (the **timing** def/use/stall hooks) |
| `+0x158c0` / `+0x158c8` | — | issue-time def/reserve hook base + method (observed in `MOVI_issue`) |

⇒ clean separation: **`0x152xx` = host values, `0x14exx`/`0x158xx` = cas timing.** A reimplementer who wants timing-only can stub every `0x152xx` callback to a no-op and still get bit-exact cycle counts; a reimplementer who wants values-only (fiss) can ignore the entire `0x14exx`/`0x158xx` scoreboard.

---

## 8. THE WALL — the MODULE_SCHEDULE reservation matrices are EMPTY

> **CORRECTION / WALL (read this before you try to port a classic reservation-table ISS).**
>
> A textbook cycle ISS carries **per-op reservation matrices** — a `MODULE_SCHEDULE`-style 2-D table `[op_class][pipeline_stage]` of resource demands that the scoreboard reads to decide structural conflicts. **`libcas-core` carries no such populated table.** Direct evidence:
>
> - `nm libcas-core.so | rg -ci 'MODULE_SCHEDULE'` → **0** (no symbol).
> - `strings -a libcas-core.so | rg -i 'MODULE_SCHEDULE'` → **0** (no string).
> - `nm | rg -i 'reservation|ReservationTable|sched_class'` → **0** (no reservation-table symbol of any name).
>
> What replaces it is **per-op-function-encoded latency**: the latency a consumer needs is a `mov $LAT,%esi` immediate *baked into each of the 2,149 `*_inst_*_issue` thunks*, and the structural demand is the *chain of `my_WB_*` / `my_MS_*` predicate calls* in each of the 1,651 `*_inst_*_stall` thunks. There is no central table to read; the schedule is **distributed across ~3,800 hand-emitted issue/stall functions**, one per (format, slot, mnemonic).
>
> **What this means for a reimplementer.** (1) You cannot extract a compact reservation matrix and drive a generic scoreboard — the canonical source of truth is the **immediate operand inside each issue thunk**, recovered exactly as §4 does (resolver → `mov $imm,%esi` → reserve call). (2) The latency table in §4 *is* the reconstructed matrix — it is what you would have to build to replace the distributed encoding, but it is **derived**, not present. (3) Structural conflicts are decided by *running the stall predicate chain*, not by table lookup; a faithful port must replay the `my_WB_S_stall` 6-lane drain and the `my_MS_*` interlocks, not approximate them with a port-count integer. `[HIGH, OBSERVED — the absence is verified three ways; the §4 table is the INFERRED reconstruction.]`

---

## 9. Summary table — the timing model at a glance

| Aspect | Model | Tag |
|---|---|---|
| Pipeline | 16 logical stages/slot mapped onto a 32-deep RING (`& 0x1f`); ring head `+0x24ec` advances 1/tick; stage idx `+0x484c` | HIGH |
| Issue width | FLIX bundle, up to 4 (wide) / 5 (F3,F11) / 2-3 (narrow) parallel ops/cycle; units LdSt/Ld/Mul/ALU | HIGH |
| Decode/length | op0-nibble table `CSWTCH.4667`@0x17d0640 → {3,2,16,8/16,illegal} B | HIGH |
| Dispatch | 47-record (32 B) `slot_{issue,stall,semantic}_functions` → per-(fmt,slot) {mnemonic,fn} arrays (2149 issue / 1651 stall) | HIGH |
| RAW hazards | latency-aware bypass scoreboard; per-op use-LAT (`esi`) vs (producer_ready − stage_distance) over a per-file window | HIGH |
| Latency table | AR=4, vec=10(/11/12/13), vbool=10, wvec(MAC)=12, valign=9, gvr=9-10, b32_pr=10, FP-convert=13, fp→int=12; def-post=1, set_def=3 | HIGH numbers |
| Structural hazards | 6-lane writeback buffer (WB_S/P/C/N/T/M), multi-slot dispatch (MS_DISPST/MS_DE), CPENABLE@3, OCD; fp-width 4-port 2:1 | HIGH |
| Cycle advance | `dll_cycle_advance` — commit 4 WB lanes → `+0x8f8` regfile, dec ring head, shift ring; `dir` arg supports rollback | HIGH |
| Memory timing | fixed load-use LAT (4/10) + WB-port occupancy; per-region (SBUF/HBM) latency is HOST-side, not a cas constant | MED |
| Clock frequency | NOT in binary (cycles abstract; ns conversion host-side) | HIGH |
| Reservation matrix | **EMPTY** — no MODULE_SCHEDULE; latencies are per-op-function-encoded, not table-driven (§8) | HIGH |
| Value math | ZERO (0 packed-arith in 45 MB); all values via 119 `nx_*` host callbacks | HIGH |

---

## 10. Open items / uncertainty

- **`[MED]`** Exact role mapping of `esi` values **1 vs 3** (def-post vs set_def vs kill_def) — labeled by position in the issue/stall body; the 4/10/12 USE latencies are HIGH, the 1/3 reservation-role labels are MED.
- **`[MED]`** The full per-mnemonic latency spread (which specific shuffle/reduce/CVT ops carry vec LAT 11/12/13 vs 10) — the histogram proves the distribution; a per-mnemonic enumeration was sampled (`RADDNX16/SELNX16/SHFLNX16/MOVAV16` all = 10 result), not exhaustively walked.
- **`[MED]`** The `dll_cycle_advance` **rollback** (`dir<0`) path semantics — presence is HIGH; the exact reverse-commit rule is MED (not fully traced through 16 KB).
- **`[MED]`** Per-memory-region (SBUF vs DataRAM vs HBM) latency — cas appears to model a uniform load-use LAT + structural WB occupancy; variable region latency is inferred to be host-resolved (no per-region constant found). Needs a host-model cross-check to confirm cas does not pass a region-dependent LAT.
- **`[HIGH]`** §1-5 and §7-8 are direct binary evidence (disasm + bytes); the §4 latency table is the reconstructed (INFERRED) form of the distributed, table-less schedule.

The full cross-engine fusion of this timing model with the fiss value oracle — the single re-runnable reference that emits *both* the cycle count and the architectural result for any bundle — is assembled in [`iss-oracle-synthesis.md`](./iss-oracle-synthesis.md). The reference-vs-production timing diff (whether the shipped ncore2gp uses these exact latencies or a tuned production set) is treated in the Part-15 validation material by title.
