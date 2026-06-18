# The Eight Register Files

Every per-slot operand in a decoded FLIX bundle — every `vt`, `wvt`, `vbr`, `art`, `bs` — names
a *register file* and an *index within it*. The [FLIX decoder](../../reference/flix-decoding.md)
hands the rest of the toolchain a `(regfile, index)` pair per operand and walks away; this page
is where that pair is given meaning. It is the authoritative architectural-state reference for
the Cairo (`ncore2gp`) config: the geometry (width × depth), the role, the read/write semantics,
and the move/bridge instructions that copy data *across* files, for each of the **eight** register
files a reimplementer must model. Get this roster wrong and operand typing is wrong for the entire
ISA reference; the [glossary's 8-regfile convention](../../glossary.md#register-files--isa-metadata)
and the [index](../../index.md) both standardize on the count established here.

The whole roster is one frozen configuration — it does **not** vary across the five GPSIMD
generations (SUNDA…MAVERICK). It is read **directly out of the shipped `libisa-core.so`**: a
single `regfiles[]` table of eight 56-byte records at VMA `0x74a800`, plus a `regfile_views[]`
table of four 32-byte records at `0x74a780`. The accessor `num_regfiles` (@ `0x3b5c20`) returns
`8`; `num_regfile_views` (@ `0x3b5d50`) returns `4`. Both bodies are a single `mov imm,%eax; ret`
and were re-disassembled in-checkout. `[HIGH/OBSERVED]` throughout except where flagged.

> **GOTCHA — there are 8 *files*, not 12, even though the TIE DB enumerates 12 regfile-class
> entries.** The fourth field in the count is `regfile_views`: four narrowed sub-views of the
> single `BR` boolean file (`BR2`/`BR4`/`BR8`/`BR16`). A *view* is a different ctype window onto
> an existing file's storage, **not** an independent file with its own ports. `8 files + 4 views
> = 12 entries`; the canonical hardware register-file count is **8**. The reconciliation is
> worked in full in [§7](#7-the-8-vs-12-reconciliation--files-vs-views).

---

## 1. Key facts

| Fact | Value | Source |
|---|---|---|
| Config / uarch | `Xm_ncore2gp` / *Cairo*, `Xtensa24`/XEA3, `NX1.1.4` | `ncore2gp-params` |
| `num_regfiles` | **8** (`0x08`) | `num_regfiles` @ `0x3b5c20` → `mov $0x8,%eax; ret` |
| `num_regfile_views` | **4** (`0x04`) | `num_regfile_views` @ `0x3b5d50` → `mov $0x4,%eax; ret` |
| `regfiles[]` | VMA `0x74a800`, stride **56** (`0x38`), 8 entries | `.data.rel.ro` |
| `regfile_views[]` | VMA `0x74a780`, stride **32** (`0x20`), 4 entries | `.data.rel.ro` |
| coprocessors | **1**: `{name="Vision", number=1}` | `num_coprocs` @ `0x3b6dc0` → `mov $0x1` |
| ctypes | **64** TIE C-types | `num_ctypes` @ `0x3b67d0` → `mov $0x40` |
| `.data.rel.ro` VMA↔file delta | **`0x200000`** (VMA `0x67bb00` → file `0x47bb00`) | `readelf -SW` |
| `.rodata` delta | **0** (VMA == file offset) — string pointers resolve direct | `readelf -SW` |
| callee-saved, all 8 files | `num_callee_saved == 0` (every file caller-saved) | `regfiles[i]+0x20` |

The two tables are reached by twelve trivial accessor thunks (`regfile_name` @ `0x3b5c30`,
`regfile_shortname` @ `0x3b5c50`, `regfile_num_bits` @ `0x3b5c90`, … `regfile_coproc` @
`0x3b5d30`), each of which is index arithmetic plus a load at a fixed struct offset. The index
math in `regfile_name` is `lea (,rdi,8); shl $6,rdi; sub rax,rdi` — i.e. `rdi*64 − rdi*8 =
rdi*56`, the proof of the 56-byte stride. A reimplementation does not need the thunks; it needs
the two tables and the struct layout below.

> **NOTE — counts are re-grounded against the binary, not a decompile grep.** Every count on this
> page reproduces with `nm libisa-core.so | rg -c …` or a `readelf -x`/`objdump -d` against the
> named VMA. The `.data.rel.ro` delta is `0x200000` *for this binary*; `.rodata` is VMA == file
> offset, so the string pointers stored in the table (absolute link-time VMAs into `.rodata`)
> resolve with no adjustment.

---

## 2. The `regfile` / `regfile_view` struct layout

Recovered from the accessor offset arithmetic (each thunk loads from a fixed byte offset) and
confirmed byte-exact against the raw `.data.rel.ro` bytes.

```c
struct regfile {              // 56 bytes, stride verified rdi*56
    char   *name;             // 0x00  canonical id        ("AR", "vec", "wvec", …)
    char   *shortname;        // 0x08  TIE `base` token     ("a","v","wv",…) — used in operands
    char   *package;          // 0x10  defining TIE package ("xt_xtensa" | "xt_booleans" | "xt_ivp32")
    int32_t num_bits;         // 0x18  register WIDTH in bits
    int32_t num_entries;      // 0x1c  register COUNT (depth)
    int32_t num_callee_saved; // 0x20  ABI: 0 for all eight (caller-saved)
    uint32_t flags;           // 0x24  0x05 for seven files; 0x0d for gvr (extra bit 0x08)
    char   *ctype;            // 0x28  TIE C value-type the registers map to
    char   *coproc;           // 0x30  ""(core) for AR/BR; "Vision" for the six SIMD files
};

struct regfile_view {         // 32 bytes, stride rdi*32
    char   *name;             // 0x00  view id    ("BR2","BR4","BR8","BR16")
    char   *parent;           // 0x08  owning file ("BR" — all four)
    int32_t num_bits;         // 0x10  view width (2/4/8/16)
    /* 0x14 pad */
    char   *ctype;            // 0x18  view ctype (_TIE_xtbool2 / 4 / 8 / 16)
};
```

There is **no** debug-number (`dbnum`) field in either struct — the only integer fields are
`num_bits`, `num_entries`, `num_callee_saved`, `flags` (exhaustively confirmed: no
`regfile_dbnum` symbol exists in `nm`). Per-register target/debug numbers live in the TIE
`tie.h` `SA_LIST` instead (`v0..v31` = `0x1000..0x101F`, etc.), not in the libisa tables.

---

## 3. The eight register files — the authoritative roster

Read byte-exact from the 448 raw bytes at file offset `0x54a800` (= VMA `0x74a800` − the
`0x200000` `.data.rel.ro` delta) and resolved with the struct above. Reproduce with:
`readelf -x .data.rel.ro libisa-core.so` and parse 8 records of 56 bytes.

| idx | name | short | package | width (bits) | depth | total bits | flags | ctype | coproc | role |
|---|---|---|---|---:|---:|---:|---|---|---|---|
| 0 | **`AR`** | `a` | `xt_xtensa` | 32 | 64 | 2048 | `0x05` | `_TIE_uint32` | *(core)* | windowed scalar address/general registers |
| 1 | **`BR`** | `b` | `xt_booleans` | 1 | 16 | 16 | `0x05` | `_TIE_xtbool` | *(core)* | scalar boolean/branch predicate registers |
| 2 | **`vec`** | `v` | `xt_ivp32` | 512 | 32 | 16384 | `0x05` | `_TIE_xt_ivp32_xb_vec2Nx8` | `Vision` | the 512-bit SIMD vector data file (the hot compute file) |
| 3 | **`vbool`** | `vb` | `xt_ivp32` | 64 | 16 | 1024 | `0x05` | `_TIE_xt_ivp32_vbool2N` | `Vision` | per-lane vector predicate (mask) registers |
| 4 | **`valign`** | `u` | `xt_ivp32` | 512 | 4 | 2048 | `0x05` | `_TIE_xt_ivp32_valign` | `Vision` | alignment registers priming unaligned vector loads |
| 5 | **`wvec`** | `wv` | `xt_ivp32` | 1536 | 4 | 6144 | `0x05` | `_TIE_xt_ivp32_xb_wvecspill` | `Vision` | wide MAC accumulators (3×512-bit headroom) |
| 6 | **`b32_pr`** | `pr` | `xt_ivp32` | 64 | 16 | 1024 | `0x05` | `_TIE_xt_ivp32_xb_int64pr` | `Vision` | packed-boolean / 32-lane predicate file |
| 7 | **`gvr`** | `gr` | `xt_ivp32` | 512 | 8 | 4096 | `0x0d` | `_TIE_xt_ivp32_xb_gsr` | `Vision` | global / vector-pipe state registers (VFPU CSR) |

Raw witness (selected entries, `readelf -x .data.rel.ro`, little-endian):

```
0x74a800  ..  20000000 40000000  AR     : num_bits 0x20=32,   count 0x40=64
0x74a850  01000000 10000000  ..    BR     : num_bits 1,        count 0x10=16
0x74a888  ..  00020000 20000000  vec    : num_bits 0x200=512, count 0x20=32
0x74a8f0  ..  00060000 04000000  wvec   : num_bits 0x600=1536,count 4
0x74a9a0  00020000 08000000 00000000 0d000000   gvr : 512 / count 8 / cs 0 / flags 0x0d
0x74a9d0  00000000 …                       (zero — confirms exactly 8 entries)
```

The zero fill immediately after entry 7 corroborates the `num_regfiles == 8` accessor: there is
no ninth record.

Two structural facts split the roster:

- **Two core files** (`AR`, `BR`) carry `coproc = ""` (a non-NULL pointer at `0x3c4ec4` to the
  empty string — *not* NULL) and live in the base-Xtensa packages `xt_xtensa` / `xt_booleans`.
  They are the scalar Xtensa state.
- **Six SIMD files** (`vec`, `vbool`, `valign`, `wvec`, `b32_pr`, `gvr`) carry `coproc =
  "Vision"` and all live in the single Vision-Q7 IVP SIMD package `xt_ivp32`. There is exactly
  one coprocessor in the config (`num_coprocs == 1`, `{name="Vision", number=1}`).

> **QUIRK — `gvr`'s flags are `0x0d`, every other file is `0x05`.** Seven files set flag bits
> `{0,2}` (`0x05`); `gvr` additionally sets bit `3` (`0x08`). The corpus carries no flag-name
> table, so the bit's meaning is inferred: combined with `gvr`'s `gsr` ctype ("global state
> register") and its zero schedule-operand footprint ([§5](#5-readwrite-semantics-per-file)),
> bit `0x08` marks `gvr` as the **global/state** file rather than a data file. `[MED/INFERRED]`
> for the bit's *meaning*; `[HIGH/OBSERVED]` for the byte `0x0d` itself.

### Per-file detail

**`AR` (idx 0) — windowed scalar address/general, 64 × 32-bit.** The base Xtensa register file.
Physically 64 registers organized as **8 windows of 8** (the LX7 windowed ABI: `entry`/`callx8`/
`retw.n`); an instruction sees the eight `a0..a7` of the current window, and a window rotate
spills/fills via `SPILLW`. `XCHAL_NUM_AREGS = 64` in `core.h` agrees with the binary count. ctype
`_TIE_uint32`. Sources AGU base addresses, ALU operands, loop counters; sinks load data, ALU
results, and — at deep vector stage — the scalar results of `IVP_SQZN`-class ops.

**`BR` (idx 1) — scalar boolean/branch predicate, 16 × 1-bit.** `XCHAL_HAVE_BOOLEANS = 1`. The
sixteen single-bit `b0..b15` drive conditional branches and boolean ALU ops. Its
[four narrowed views](#7-the-8-vs-12-reconciliation--files-vs-views) (`BR2`/`BR4`/`BR8`/`BR16`)
expose 2/4/8/16-bit boolean tuples over the same 16-bit storage.

**`vec` (idx 2) — the 512-bit SIMD vector file, 32 × 512-bit.** The hot compute file: 32 vector
registers, each 512 bits = 32 lanes × 16-bit (the default `vec2Nx8` ctype; many other ctype
*views* of the same 512-bit storage exist — `vecNx16`, `vec2Nx8`, `vecN_2x32`, etc. — selected
per-op, see [§6](#6-cross-file-bridge--move-instructions)). Every architectural vector source
(`vt`/`vr`/`vs`/`vp`/`vq`) is read here.

**`vbool` (idx 3) — per-lane vector predicate, 16 × 64-bit.** Sixteen mask registers; each bit
gates one SIMD lane of a predicated vector op (the `_T`-suffix throttle/select family). ctype
`_TIE_xt_ivp32_vbool2N`, 64-bit storage (one mask bit per 8-bit sub-lane of a 512-bit vector).

**`valign` (idx 4) — alignment registers, 4 × 512-bit.** The four `u0..u3` hold the rotate-merge
alignment state an unaligned vector load primes before a streaming read (`ivp_lavnx8*_xp` prime,
`ivp_sanx8*_ip` flush). They also serve as iterative-divide scratch (the 32 `DIVN_*_4STEP` step
ops). ctype `_TIE_xt_ivp32_valign`.

**`wvec` (idx 5) — wide MAC accumulators, 4 × 1536-bit.** The deep-learning inner-loop critical
state: four 1536-bit accumulators (`wv0..wv3`), each holding **3 × 512-bit** of headroom so a
widening quad-MAC can accumulate `int8×int8 → int24/int48`-class products without overflow across
a long reduction. ctype `_TIE_xt_ivp32_xb_wvecspill`. Only four entries exist, so at most four
independent MAC accumulation chains can be in flight at once
([§5](#5-readwrite-semantics-per-file)).

**`b32_pr` (idx 6) — packed predicate / 32-lane select-mask, 16 × 64-bit.** Sixteen `pr0..pr15`,
ctype `_TIE_xt_ivp32_xb_int64pr` ("`int64pr`", 64-bit storage). The "`b32`" in the name denotes
the 32-lane packed-predicate organization; it is cosmetic — there is no 32-bit width discrepancy
(the storage is 64-bit). A cold-path file: it carries zero architectural operands in any per-op
schedule ([§5](#5-readwrite-semantics-per-file)).

**`gvr` (idx 7) — global / vector-pipe state, 8 × 512-bit.** Eight `gr0..gr7`, ctype
`_TIE_xt_ivp32_xb_gsr` ("global state register"). In the per-op schedules this file carries **no
register operand**: it is the Vision-FPU control/status file (`FSR`/`FCR` — round mode, the five
IEEE exception flags + enables), accessed via `RUR.FSR`/`WUR.FSR`/`RUR.FCR`/`WUR.FCR` rather than
as a data source/sink.

> **CORRECTION — `gvr` is real; do not treat it as a stub even though `tie.h` omits it.** The
> shipped `tie.h` `SA_LIST` (the context-save-area list) enumerates the other five SIMD files
> exactly (32 `v` + 16 `vb` + 4 `u` + 16 `pr` + 4 `wv` = 72 CP1 registers, matching the binary's
> SIMD counts), but lists **no** `gr`/`gvr`/`gsr` base, and `core.h` has no `GVR`/`GSR` macro. The
> binary is authoritative and ships full instruction support for it — `proto_TIE_xt_ivp32_xb_gsr_
> loadi` / `_storei` / `_move` and their operand tables all resolve (21 `gsr` symbols). `gvr` is a
> genuine eighth file; its absence from `tie.h`'s SA_LIST is consistent with a file that is not
> part of the ABI register-save context (it is a CSR/state file, not call-clobbered data).
> `[HIGH/OBSERVED]` for the file; `[LOW]` only for *why* `tie.h` omits it.

---

## 4. The naming spread — one roster, several report labels

Different backing analyses labelled the same eight files slightly differently. The **`name` and
`shortname` columns in the binary table are canonical**; the descriptive role-labels below are
synonyms a reader may encounter. None of them are conflicting files — they are prose for the same
record.

| canonical (`name`/`short`) | role-label synonyms seen in analysis | binding fact |
|---|---|---|
| `gvr` / `gr` | "global state registers", "general vector registers", "gr vector-pipe state file (VFPU CSR)" | `gsr` ctype + `RUR/WUR.FSR/FCR` access ⇒ control/status, not data |
| `b32_pr` / `pr` | "predicate/pack registers", "32-way packed-register / select-mask file", "packed boolean" | `int64pr` ctype, 16×64-bit |
| `valign` / `u` | "alignment registers", "load/store alignment", "divide scratch" | unaligned-L/S rotate-merge + `DIVN` step scratch |
| `wvec` / `wv` | "wide MAC accumulators", "wide accumulator", "quad-MAC widen target" | `wvecspill` ctype, 4×1536-bit |

Where a role-label is ambiguous, default to the *binding fact* column.

---

## 5. Read/write semantics per file

Every file's read/write behavior is fixed by the per-opcode schedule tables (one schedule per
opcode; each operand carries an architectural name + the pipeline stage it is read/written in).
The *port model* — exact per-cycle read/write port counts, the distinct-registers-per-bundle
limits, the forwarding-path matrix, and the structural-hazard list — is its own deep page
([Register-File Port Model + Bypass Network](../../uarch/regfile-ports.md)); this section gives
the high-level access model a reimplementer needs to type operands correctly.

| file | reads per op (max) | writes per op (max) | read stage | write stage(s) | access character |
|---|---:|---:|---|---|---|
| `vec` | 4 | 1 | `@10` (single unified port) | `@10`/`@11`/`@12`/`@13` (staggered by latency class) | general data source/sink |
| `wvec` | 1 (RMW) | 1 (RMW) | `@12` | `@12` (same-stage read-modify-write) | accumulator only — never read without write |
| `valign` | 1 | 1 | `@10` | `@12` | unaligned-L/S + divide-step scratch |
| `vbool` | 2 | 1 | `@10` | `@10`/`@11` | predicate mask source/sink |
| `gvr` | 0 | 0 | `RUR` | `WUR` | CSR (`FSR`/`FCR`) — no data operand |
| `b32_pr` | 0 | 0 | — | — | no schedule operand (cold path) |
| `AR` | 2 | 1 | `@1`/`@3`/`@4`/`@5` | `@1`/`@3`/`@4`/`@5`/`@6`/`@12` | windowed scalar; per-issue-slot single |
| `BR` | 2 | 1 | `@3`/`@4` | `@4` | scalar boolean E-stage |

Three semantics matter for operand typing and must be modelled exactly:

- **`vec` reads are a single unified stage-10 port** — *all* architectural vector source reads
  land at stage 10, with no exceptions. Writes are *staggered* across stages 10–13 by latency
  class (load @10, 1-cycle ALU @11, 2-cycle MAC/lookup @12, 3-cycle FMA/convert @13). This
  read-heavy / write-light asymmetry is the central anti-port-pressure mechanism (a co-issued
  bundle reads its whole source fan-in the same cycle but writes its results across four).

- **`wvec` is a same-stage read-modify-write accumulator.** An accumulating MAC reads its four
  multiply sources from `vec` @10, reads the running accumulator (`wvt`) @12, multiplies+adds, and
  writes the new accumulator back @12. The (use_stage, def_stage) pair is `(12, 12)` for *all*
  accumulating ops, with zero exceptions: the accumulator is never read without also being written
  in the same op (it is a true accumulate register, not a general source). Write@12 → next read@12
  is the same stage, so the accumulator forward is a single-cycle bypass and a MAC chain issues
  **every cycle** (initiation interval 1) with a **2-cycle result latency** (inputs@10 →
  result@12) — the "2-cycle MAC recurrence". With only four `wvec` entries, at most four
  independent accumulation chains can be live; a fifth aliases an in-use accumulator (a
  software-scheduling constraint, not a hardware port limit).

- **`gvr` and `b32_pr` carry zero architectural register operands.** `gvr` is the VFPU
  control/status file, touched only by `RUR/WUR.FSR/FCR` (the only `RUR/WUR` ops besides
  `THREADPTR`); `b32_pr` appears in no per-op schedule at all (a cold path the compiler rarely
  emits in this config). Model both as present state that the *common* compute path does not
  read or write per-cycle.

> **GOTCHA — the `wvec` accumulator reads at stage 12, not stage 10.** It is tempting to model the
> accumulator as a normal `vec`-class read @10. It is not: it reads and writes at stage 12 in the
> same op. Modelling it @10 breaks the II=1 / 2-cycle-latency recurrence and makes the accumulator
> spuriously appear as a stage-10 vec read. The `(12,12)` self-RMW is the correct model.

---

## 6. Cross-file bridge / move instructions

Data crosses between files only through explicit bridge instructions; the FLIX operand encoder
tags each operand with its direction (`0x6f` = ASCII `'o'` for *out*, `0x69` = `'i'` for *in*, in
the high byte of the per-operand flag word). The bridge families below are all present as
resolvable `proto_TIE_*` operand-table symbols in `libisa-core.so`.

### 6.1 Per-file `loadi` / `storei` / `move`

Every file (and every ctype-view of `vec`) ships a uniform `_loadi` (memory → file), `_storei`
(file → memory), and `_move` (intra-file copy) triplet:
`proto_TIE_xt_ivp32_xb_<ctype>_{loadi,storei,move}_*`. The scalar files have the base-Xtensa
equivalents (`l32i`/`s32i`/`mov` on `AR`). For `wvec`, the move is `ivp_movww` (`wvecspill_move`):
an accumulator-to-accumulator copy used for spill/restore.

### 6.2 `vec` ↔ `wvec` — pack (write into the accumulator) and unpack (read it back out)

The quad-MAC widen *writes* its product into the wide `wvec` accumulator; reading the accumulated
result back into a normal `vec` register is the **pack/unpack readout**. The semantic field stems
in the binary are `ivp_sem_wvec_pack_{vr,vt,wvr}` (the write side) and
`ivp_sem_unpack_wvec_mov_{vr,vs,wvr,wvt}` (the readout side, riding the `s2_mul` slot). The user
opcode is `IVP_PACKVRNX48` and its two-half variants `IVP_PACKVRNX48_0` / `_1`:

```c
// IVP_PACKVRNX48 — pack the wide (Nx48) accumulator content into a normal vec register.
//   operands (binary):  a = vecNx16  (OUT, regfile vec)
//                        b = vecNx48  (IN,  the wvec accumulator viewed as Nx48)
//                        c = int32    (IN,  an AR scalar: rounding / shift amount)
// Reads the 1536-bit accumulator, applies the AR-supplied shift/round, narrows each
// lane to 16 bits, and writes a 512-bit vec result. The _0 / _1 variants emit the
// two 32-bit halves (a = vecN_2x32v, c = uint32) for a 2-step full-width readout.
void ivp_packvrnx48(vecNx16 *vec_out, const wvec_acc *acc /*Nx48*/, uint32_t ar_shift) {
    for (int lane = 0; lane < N; lane++) {                 // N = lanes (32 for 16-bit)
        int48_t wide = acc->lane[lane];                    // read from wvec @ stage 12
        int48_t shifted = round_shift_right(wide, ar_shift);
        vec_out->lane[lane] = saturate_to_16(shifted);     // write to vec @ stage 12
    }
}
```

The matching *unpack/move* (`ivp_sem_unpack_wvec_mov_*`) reads the accumulator and routes its
lanes back onto the `vec` read port without the narrowing — the raw accumulator readout.

> **NOTE — `wvec` is never a general source; the only way to consume an accumulated result is a
> pack/unpack readout.** Because the schedule never lists `wvt` as a use-only operand
> ([§5](#5-readwrite-semantics-per-file)), a reduction that has finished accumulating *must* emit
> a pack/unpack to move the answer into `vec` before any downstream op (store, further compute)
> can see it.

### 6.3 `AR` (scalar) → `vec` — splat / replicate

A scalar broadcast into all vector lanes is the **replicate** family `ivp_sem_vec_rep_*`. The
variants select the source: `rep_i32` / `rep_i64` / `rep_i8` (an immediate or `AR` scalar),
`rep_vr` / `rep_vs` / `rep_vt` (a single `vec` lane), `rep_prr` / `rep_prt` (a `b32_pr` lane), and
`rep_vbr` (a `vbool` lane). The user opcode `IVP_REPNX16` replicates a value across all 32
16-bit lanes:

```c
// IVP_REPNX16 — broadcast a scalar/lane value to every lane of a vec register.
//   operands (binary):  a = vecNx16 (OUT, regfile vec)
//                        b = vecNx16 (IN)   c = (selector)
// The rep_i32 form sources the broadcast value from an AR scalar; the rep_vr form
// sources lane 0 of a vec register. Either way the value is written to all N lanes.
void ivp_repnx16(vecNx16 *vec_out, int16_t value) {
    for (int lane = 0; lane < N; lane++)
        vec_out->lane[lane] = value;                       // AR/imm -> all vec lanes
}
```

### 6.4 `vec` → `AR` (scalar) — squeeze

The reverse bridge — extracting a scalar from a vector — is the **squeeze** family
`IVP_SQZN` / `IVP_UNSQZN` (and `_2` variants). `IVP_SQZN`'s operands are `a = vecNx16` (vec out),
`b = int32` (an **`AR` scalar** out — e.g. a population/index count), `c = vboolN` (a `vbool`
predicate selecting the active lanes). This is the path that produces a scalar `AR` result *at the
deep vector stage 12*: the scalar pipe reads `AR` @4, so a scalar op dependent on a squeeze result
must wait the full deep-pipe-to-early-scalar gap (~8 cycles) — there is no fast forward across the
pipe boundary. Software schedules independent work into the gap.

### 6.5 `vbool` ↔ `vec`

Predicate masks bridge into the vector pipe via `ivp_sem_vec_mov_vbr` (move a `vbool` mask onto the
`vec` data path) and are produced by comparison/classify ops that write `vbool` directly. The
predicated-op family (`ivp_selnx16` / `ivp_dselnx16` / the `_T`-throttle ops) reads a `vbool` mask
@10 alongside its `vec` sources to gate per-lane writeback.

> **GOTCHA — the `IVP_MOVNX16_FROM<ctype>` family is *not* a cross-file move.** Symbols like
> `IVP_MOVNX16_FROM32`, `IVP_MOVNX16_FROM2NX8`, `IVP_MOVNX16_FROMN_4X64` look like file bridges but
> are **re-typings of the same `vec` storage** — `vecN_2x32`, `vec2Nx8`, `vecN_4x64` are all
> different ctype *views* of one 512-bit `vec` register (`ivp_movvv`, a vec→vec move). They change
> how the bits are interpreted (lane count × element width), not which file holds them. Only
> `pack`/`unpack` (`vec↔wvec`), `rep` (`AR/lane→vec`), `sqz` (`vec→AR`), and `vec_mov_vbr`
> (`vbool→vec`) actually cross file boundaries.

---

## 7. The 8-vs-12 reconciliation — files vs. views

The TIE database, queried for "regfile-class entries", returns **twelve**. The hardware register
count is **eight**. The four-entry difference is *exactly* the `regfile_views` table: narrowed
sub-views of the single `BR` boolean file, not independent files.

| `regfile_views[]` entry | parent file | width (bits) | ctype |
|---|---|---:|---|
| `BR2` | `BR` | 2 | `_TIE_xtbool2` |
| `BR4` | `BR` | 4 | `_TIE_xtbool4` |
| `BR8` | `BR` | 8 | `_TIE_xtbool8` |
| `BR16` | `BR` | 16 | `_TIE_xtbool16` |

All four name `BR` as `parent`. A view exposes a multi-bit boolean tuple (2/4/8/16 bits) over the
same sixteen 1-bit `BR` registers — it has its own ctype but **no storage, no count, and no
ports of its own**; it is a typed window onto `BR`. There are no views over any SIMD file (the
`regfile_views[]` table is exactly these four). Hence:

```
8 register files            (regfiles[],      num_regfiles      == 8)
+ 4 BR sub-views            (regfile_views[], num_regfile_views == 4)
= 12 TIE regfile-class entries
canonical hardware register-file count = 8
```

A reimplementer models **eight** files; the four `BR` views are alternate ctypes the operand
typer applies to `BR`, not extra state.

> **CORRECTION — count the files, not the entries.** Any analysis or upstream TIE dump that reports
> "12 register files" has folded the four `BR` views into the file count. The shipped accessor
> `num_regfiles` is the arbiter, and it returns **8**. The glossary, index, and this page all
> standardize on 8.

---

## 8. Cross-references

- [The FLIX Bundle-Decoding Methodology](../../reference/flix-decoding.md) — the decoder that
  produces the `(regfile, index)` operand pairs this page types; slot operands name `AR`/`vec`/
  `vbool`/`wvec`/`gvr`/… by the `shortname` column here.
- [Register-File Port Model + Bypass Network](../../uarch/regfile-ports.md) — the deep port model:
  per-cycle read/write port counts, distinct-registers-per-bundle limits, the full write-stage →
  read-stage forwarding matrix, the `wvec` 2-cycle recurrence, and the no-forward hazard list.
  ([§5](#5-readwrite-semantics-per-file) is the high-level summary; this is the cycle-accurate
  model.)
- [ctype / coproc / funcUnit Tables](ctype-coproc-funcunit.md) — the 64 TIE C-types each register
  maps to, the single `Vision` coproc, and the reverse `ctype → regfile` map.
- [The TIE Database & Four Independent ISA Sources](tie-database.md) — where `tie.h`'s `SA_LIST`
  (the `dbnum` source, and the table that omits `gvr`) lives.
- [Core Identity & Configuration](identity-config.md) — the `ncore2gp`/Cairo config the roster
  belongs to (`XCHAL_NUM_AREGS = 64`, `XCHAL_HAVE_BOOLEANS = 1`).
- [Master Glossary → Register files & ISA metadata](../../glossary.md#register-files--isa-metadata)
  — the one-line definitions and canonical anchors for every file named here.

---

*Provenance: the `regfiles[]` (`0x74a800`) and `regfile_views[]` (`0x74a780`) tables, the
`num_regfiles`/`num_regfile_views` accessors, and the bridge-op `proto_TIE_*` symbols are
`[HIGH/OBSERVED]` — re-disassembled and `readelf -x`-dumped in-checkout from the shipped
`libisa-core.so`. The read/write stages in [§5](#5-readwrite-semantics-per-file) are `[HIGH/
OBSERVED]` from the per-opcode schedule tables (see the port-model page). The `gvr` flag-bit
`0x08` meaning is `[MED/INFERRED]`; the reason `tie.h` omits `gvr` is `[LOW]`. All facts read as
derived from shipped-artifact static analysis (lawful interoperability RE).*
