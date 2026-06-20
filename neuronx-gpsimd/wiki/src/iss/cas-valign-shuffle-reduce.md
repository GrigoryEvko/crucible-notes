# cas/fiss VALIGN / Shuffle-Select / Reduce

This page documents the **lane-manipulation op family** — the three datapath legs that
move data *across* lanes rather than computing a value *within* a lane — as the
cycle-accurate ISS models them across its two cooperating plugins:

1. **VALIGN** — the unaligned-vector-access *funnel*: a prime/flush state machine over a
   4-entry alignment register that assembles a fully byte-misaligned 512-bit vector from
   two aligned 64-byte line loads (`LALIGN`/`LA*` load side, `SALIGN`/`SAPOS`/`SA*` store
   side).
2. **Shuffle / Select** — the lane-permute *crossbar*: a control-word-driven per-output-lane
   mux (`SHFL` single-source, `SEL` two-source, `DSEL` dual-output, `DCMPRS` predicate
   compress) plus the slot-pinned immediate forms.
3. **Reduce** — the cross-lane *fold/reduce-tree*: `radd`/`rmax`/`rmin` collapse 32 lanes to
   a scalar, the `rb*` forms add an argmax/argmin `vbool`, and `randb`/`rorb` fold a
   predicate to a single bit.

It is one node of [Part 14 — the ISS as Executable Oracle](./cas-core-surface.md), and it
threads the same keystone that governs every page in this Part:

> **Keystone — cas decodes, fiss values.** The cycle-accurate ISS `libcas-core.so`
> (`BASE = libcas-Xtensa.so`, `VERS_1.1`, GCC 4.9.4, modeled core = Cayman / Vision-Q7 IVP
> VLIW) computes **DECODE + TIMING + REGISTER-HAZARD** — it resolves which architectural
> registers an op reads and writes and posts each at its latency — but it computes **NO
> element value**. For this family that means: cas models the **valign register state
> machine** (which align-register entry is read and re-defined, at what latency), the
> **crossbar / reduce-tree read depth** (the control-vector and fold-source resolve one
> stage deeper than the data), and the issue scoreboard. The **permuted / reduced datum**
> — the actual funnel shift, the per-lane gather, the fold result — is delegated to the
> host through the 119 `nx_*_interface` TIE-port callbacks and produced by
> `libfiss-base.so` (the `module__xdref_*` value leaves). So this page covers the **cas
> half** (the state machine + the schedule) and the **fiss half** (the funnel, the
> crossbar, the fold) side by side, naming the real symbols of each.

Confidence tags follow [the Confidence & Walls model](../reference/confidence-model.md):
`[HIGH/OBSERVED]` = read-from-byte / proven by disassembly, `[MED/INFERRED]` = reasoned
over OBSERVED, `[…/CARRIED]` = re-used at a sibling page's confidence. All cas offsets are
into `extracted/.../ncore2gp/config/libcas-core.so` (45,878,080 B; ELF64 x86-64, **not
stripped**); all fiss offsets into the sibling `libfiss-base.so` (12,330,016 B, **not
stripped**).

> **GOTCHA — VMA vs file offset is not uniform.** `.text` (`0x572fa0`) and `.rodata`
> (`0x17ba840`) are **VMA == file offset** (`readelf -SW`); the writable sections are
> **not**: `.data.rel.ro` (VMA `0x2070900` → file `0x1e70900`) and `.data` (VMA
> `0x2280ed8` → file `0x2080ed8`) both carry a **`0x200000` delta** — **not** libtpu's
> `0x400000`. Every cas/fiss routine cited below is in `.text`, so the disassembly
> addresses are raw VMA; only the decode-record `xxd` of §2 subtracts `0x200000`.
> `[HIGH/OBSERVED]`

The aligned-load primitive VALIGN is built on is [cas/fiss Load/Store](./cas-load-store.md);
the per-instruction ISA roster is [B08 reduce](../isa/ref/b08-reduce.md),
[B12 shift](../isa/ref/b12-shift.md) and [B21 select/shuffle](../isa/ref/b21-select-shuffle.md);
the formal semantics are [Group II Semantics](../isa/semantics/group-semantics-ii.md); and
the firmware consumers are [CrossLaneReduce](../firmware/kernels/cross-lane-reduce.md) and
[StreamTranspose](../firmware/kernels/stream-transpose.md).

---

## 1. The family — three legs, three issue units

The three legs issue in **three distinct units**, which is the cleanest structural test
separating them. Read from the `*_issue` symbol prefixes (`nm libcas-core.so`):
`[HIGH/OBSERVED]`

| leg | issue unit | representative mnemonics | what it does |
|---|---|---|---|
| **VALIGN** | `S0_LdSt` / `S1_Ld` (the **LSU** slots) | `LALIGN` `LA*` `LAV*` · `SALIGN` `SAPOS` `SA*` `SAV*` | unaligned vector access via a funnel + align register |
| **Shuffle / Select** | `S3_ALU` (the **lane-permute** slot) | `SHFLNX16` `SELNX16` `DSELNX16` `DCMPRS2NX8` (+ `_S0/_S2/_S4` pinned) | per-output-lane crossbar mux |
| **Reduce (arith)** | `S3_ALU` (the **fold** slot) | `RADDNX16` `RMAXNX16` `RMINNX16` `RBMINNX16` (+ fp `RxNUM`) | cross-lane fold to scalar (+ argmax `vbool`) |
| **Reduce (bool/tail)** | `S1_Ld` (the **load pipe**) | `RANDBN` `RORBN` · `LTRN` | fold `vbool` to one bit; emit tail mask |

> **QUIRK — the boolean/tail reduces run in the LOAD pipe, not the ALU.** The arithmetic
> reduces `radd/rmax/rmin/rbmax/rbmin` issue in `S3_ALU` (`RADDNX16` = 9 placements, all
> `S3_ALU`, one per FLIX format — OBSERVED). But the *boolean* fold `randb/rorb` and the
> *tail-predicate* reduce `ltr` issue in **`S1_Ld`** — the load slot (`LTRN` placements:
> `F0_S1_Ld … F7_S1_Ld`, OBSERVED; the `randb/rorb` slot is CARRIED from
> [B08 §1](../isa/ref/b08-reduce.md), which is HIGH/OBSERVED on the `s1_ld` band). A
> reimplementation that puts every "reduce" in the ALU will mis-schedule the bool fold
> against the wrong structural hazard. `[HIGH/OBSERVED for ltr-S1 + radd-S3; CARRIED for randb/rorb-S1]`

Every op of all three legs shares the IVP vector-coprocessor gate (`STATE_IN = CPENABLE`,
`EXC = Coprocessor1Exception` if cp1 disabled) — the same gate the whole `ivp_` axis
carries; it is not re-listed per op. `[CARRIED — B21 §2.1]`

---

## 2. VALIGN — the prime / flush funnel state machine

### 2.1 The four-mnemonic streaming cycle `[HIGH/OBSERVED]`

A misaligned 512-bit vector access cannot be served by a single 64-byte line read (the
plain LSU primitive of [load/store §3](./cas-load-store.md) fetches *one* line, tolerant of
*element* alignment but not *byte* misalignment). VALIGN assembles it from **two** aligned
line reads, funnel-shifted by the low address bits, with the **4-entry `valign` register**
(`opnd_sem_valign_addr` masks `& 0x3` → 4 entries — [core surface §4](./cas-core-surface.md))
carrying the *previous load's tail* across to the next access. The streaming cycle, read
from the mnemonic roster (`nm`) and corroborated by [Group II §3.1](../isa/semantics/group-semantics-ii.md):

```text
IVP_ZALIGN     : u = 0                                  ; INITIALISE  the align register
IVP_LALIGN_I/IP: u <- one fresh aligned 64-byte word ; advance byte phase   ; PRIME / ACCUMULATE
IVP_LA<w>_IP/XP: out = funnel(u_prev, fresh_word, muxsel) ; u := fresh_word ; PRODUCE  (load side)
IVP_MALIGN     : out = funnel(u_phase0, phase1)           ; PRODUCE  (explicit)
IVP_SALIGN_I/IP / IVP_SAPOS_FP : store-side counterpart (mask + funnel-in)  ; STORE / FLUSH
```

The `ZALIGN`/`MALIGN` init/produce mnemonics and the `LALIGN`/`SALIGN`/`SAPOS` prime/flush
mnemonics are all present as `_issue` symbols in `libcas-core.so` (OBSERVED). The "two
aligned loads composed by the funnel" memory shape is the standard Tensilica valign idiom
(`[MED/INFERRED]`); the **funnel + align-register state machine is OBSERVED** (§2.2).

> **The leg names decode the streaming role.**
> `LALIGN` = *load-align prime* (fill the align register). `LA<w>` (e.g. `LANX8U`,
> `LAN_2X16S`, `LA2NX8`) = *aligning load* (funnel through the primed register). `LAV<w>` =
> the *variable-element* aligning load. `SALIGN`/`SAPOS` = *store-align prime / position*.
> `SA<w>`/`SAV<w>` = *aligning store*. `LA_PP`/`LA_PPXU` = the *priming-pair* variant.
> `[HIGH/OBSERVED roster; MED for the per-name role split]`

### 2.2 The prime — `LALIGN_IP` issue, byte-exact `[HIGH/OBSERVED]`

`F0_F0_S1_Ld_16_inst_IVP_LALIGN_IP_issue` @ `0x71dc20` is the keystone. It decodes the base
AR twice (read + post-update, the `_ip` doubled-`and $0xf` of [load/store §2.3](./cas-load-store.md)),
then the **`valign` register both as a use and as a def** — that read-then-redefine is the
funnel-carry. Verbatim (operand-scoreboard calls + the `mov $LAT,%esi` before each hook):

```asm
; F0_F0_S1_Ld_16_inst_IVP_LALIGN_IP_issue @ 0x71dc20  (rdi=ctx, rsi=instr word)
71dc27:  mov    (%rsi),%esi                ; iw = *instr_word
71dc33:  and    $0xf,%esi                  ; OPERAND 1: base AR = iw & 0xf   (the READ base)
71dc36:  call   opnd_sem_AR_addr@plt
71dc44:  mov    $0x1,%esi                  ;   LAT 1   (address ready immediately)
71dc49:  call   *%r12                      ;   AR def hook
71dc59:  and    $0xf,%esi                  ; OPERAND 2: SAME AR = iw & 0xf   (the POST-UPDATE)
71dc5c:  call   opnd_sem_AR_addr@plt
71dc6a:  mov    $0x1,%esi                  ;   LAT 1   (base auto-increment def)
71dc6f:  call   *%r12
71dc85:  call   opnd_sem_valign_addr@plt   ; OPERAND 3: the valign reg as INPUT  (carried phase)
71dc9a:  mov    $0x9,%esi                  ;   LAT 9   <-- the align-register USE
71dccb:  call   opnd_sem_valign_addr@plt   ; OPERAND 4: the valign reg as OUTPUT (new phase)
71dcd9:  mov    $0x9,%esi                  ;   LAT 9   <-- the align-register DEF
71dcde:  call   *%r12
71dcee:  and    $0x3,%esi                  ;   valign index masked & 0x3 -> 4-entry file
71dcf1:  call   opnd_sem_valign_addr@plt   ;   (the def's index resolve)
71dd06:  mov    $0x9,%esi                  ;   LAT 9
```

Three facts fall straight out of this:

* The **align register is read AND written by the same op** (two `opnd_sem_valign_addr`
  calls, one use at `0x71dc85`, one def at `0x71dccb`/`0x71dcf1`) — this *is* the funnel
  carry: the op consumes the previous-load tail (`u_prev`) and writes back the fresh tail
  (`u := fresh_word`). `[HIGH/OBSERVED]`
* The valign index is masked **`& 0x3`** → the **4-entry** `valign` regfile, confirming
  [core surface §4](./cas-core-surface.md)'s `opnd_sem_valign_addr & 0x03`. `[HIGH/OBSERVED]`
* The align-register def/use posts at **LAT 9** — **one stage earlier** than a plain load's
  LAT-10 destination vector ([load/store §6.1](./cas-load-store.md)). The align register is
  ready a cycle before a normal load-use because it is a register-internal carry, not a
  memory drain. `[HIGH/OBSERVED]`

### 2.3 The funnel-produce — `LA2NX8_IP` reads-and-rewrites the primed register `[HIGH/OBSERVED]`

The aligning load funnels the carried tail with the fresh aligned word into the destination
vector while re-priming the align register. The byte-tier funnel-load that carries the
byte-exact analyzable `_issue` body is **`F0_F0_S0_LdSt_4_inst_IVP_LA2NX8_IP_issue` @
`0x118a360`** — a **five-operand** prime→funnel in a single instruction:

```asm
; F0_F0_S0_LdSt_4_inst_IVP_LA2NX8_IP_issue @ 0x118a360  (the funnel step)
;  OP1  base AR   (use,  & 0xf)            LAT 1   -- the load address
;  OP2  incr AR   (use,  & 0xf)            LAT 1   -- the post-increment
;  OP3  valign    (USE,  opnd_sem_valign)  LAT 9   -- read the carried tail (u_prev)
;  OP4  valign    (DEF,  opnd_sem_valign)  LAT 9   -- write the new tail   (u := fresh)
;  OP5  dst vec   (DEF,  opnd_sem_vec)     LAT 10  -- the assembled byte-misaligned vector
```

So one funnel-load **reads the align register, rewrites it, and produces the destination
vector at LAT 10** (like a plain load) — the align-register carry runs at LAT 9, one stage
ahead. The `_IP`/`_XP`-only mode set (there is **no plain `LA<w>_I`**, roster-verified) is
itself the tell: aligning loads are *streaming* — every access auto-advances the base
pointer, so the align register tracks a moving window.

> **CORRECTION — the funnel-load is `LA2NX8_*`, not `LANX8U_*` at the `_issue` layer.** The
> mnemonics `LANX8U`/`LANX8S`/`LAN_2X16S/U` are present as decode-record strings (the
> element-typed aligning loads), but they carry **no distinct `*_issue` symbol** — only the
> `2NX8`-grid forms (`LA2NX8_IP` @ `0x118a360`, `LAV2NX8_XP`, `LAT2NX8_XP`) have a
> byte-analyzable issue body; the narrower element forms share it via a select bit (the same
> shared-LSU-semantic pattern stores use, [load/store §2.6](./cas-load-store.md)). Anchor any
> funnel-load timing claim on `LA2NX8_IP`. `[HIGH/OBSERVED — nm: zero `LANX8U.*issue`]`

The `valignr` operand role makes the read-only nature of the *consumer* explicit:

> **QUIRK — `valignr` is a read-only role (`use`, no `def`).** The `my_valign_*` scoreboard
> census splits into three operand roles: `uul` (the *producer* role — full hazard set
> `{def, kill_def, set_def, set_use, use}`, present in **both** LSU pipes `_0`/`_1`), `uus`
> (the store-side role, single pipe), and **`valignr`** — which carries **only `set_use` and
> `use`, no `def`** (`my_valign_{0,1}_opnd_ivp_sem_ld_st_valignr_{set_use,use}`). So the
> align register, once primed by a `LALIGN`/`uul` def, is *consumed* by the aligning load
> through the `valignr` use without being redefined in that role — the prime owns the def,
> the produce owns the read. `[HIGH/OBSERVED — nm census]`

> **CORRECTION — the `my_valign_*` count is 24, not 20.** [Core surface §7](./cas-core-surface.md)
> budgeted "all `my_valign_* 20`" to this slice. The exact `nm` census is **24**: **19
> operand-role accessors** (10 `uul` across both pipes + 5 `uus` + 4 `valignr`) **+ 5
> internal helpers** (`my_valign_commit_value`, `my_valign_set_commit_value`,
> `my_valign_stage_value`, `my_valign_set_stage_value`, `my_valign_stall` — the lowercase
> `t`-bound deferred-commit + stall hooks). The "20" omits the staging helpers and rounds
> the role set; the reproducible figure is **24 total / 19 operand-role**. `[HIGH/OBSERVED]`

### 2.4 The store flush — `SAPOS_FP` `[HIGH/OBSERVED]`

The store side mirrors the load: `SALIGN`/`SAPOS` prime a store-align register; `SA<w>`/`SAV<w>`
funnel each source vector *into* the partially-filled 64-byte line; `SAPOS_FP` ("store-align
position, **F**lush **P**artial") drains the residual tail line at the end of a misaligned
store stream. `F0_F0_S0_LdSt_4_inst_IVP_SAPOS_FP_issue` @ `0x118a210` decodes **three**
operands — base AR (`& 0xf`, LAT 1), then the store-align register as a **use** (read the
buffered partial) **and** as a **def** (write the flushed/reset align state back), **both at
LAT 10**. So the store-align register runs at **LAT 10** (the store-execute stage), one stage
later than the load prime's LAT 9 — the store funnel drains at the writeback latency. This is
the store counterpart of [Group II §3.1](../isa/semantics/group-semantics-ii.md)'s
`IVP_SALIGN_I` (`xdsem_st_shifter_512 + mask_sav`). `[HIGH/OBSERVED — the 3-operand decode, the use+def of the store-align reg, and the LAT 10 read this pass]`

### 2.5 The fiss VALUE — the same funnel as the plain load `[HIGH/OBSERVED]`

There is **no `module__xdref_valign_*` leaf**. The value side reuses the *plain load's*
funnel `module__xdref_wideldshift_W_512_6` ([load/store §3.2](./cas-load-store.md)) — the
exact same family — but applies it **across two adjacent 64-byte lines** instead of within
one:

```c
/* The valign funnel VALUE: word-granular shift across the {carried ++ fresh} 1024-bit pool.
 * The plain load picks one element WITHIN one 64-byte line (addr & 0x3f);
 * valign joins TWO lines and the funnel selects a 512-bit window by word offset. */
v512 valign_funnel(v512 carried /*u_prev, the align reg*/, v512 fresh /*new aligned word*/,
                   uint6 muxsel /*low byte/word bits of the unaligned addr*/) {
    /* module__xdref_wideldshift_128_512_6 advances its window by 4 words per (amt += 16):
     *   amt= 0 -> out = src[0..3];  amt=16 -> src[4..7];  amt=48 -> src[12..15]  */
    return funnel_select(concat(carried, fresh), muxsel);   /* WORD-granular (32-bit) shift */
}
```

* The funnel is **word-granular** (32-bit word boundaries within the 512-bit register), not
  bit-granular: [Group II §3.4](../isa/semantics/group-semantics-ii.md) drives
  `wideldshift_128_512_6` live and observes the 128-bit window advancing exactly 4 words per
  `amt += 16`. `[HIGH/OBSERVED — live-driven there]`
* The `_6` token is the 6-bit `addr_lo` (`addr & 0x3f`), the byte phase within the line —
  the **same phase bit** the plain aligned load carries ([load/store §3.3](./cas-load-store.md)),
  which is *why* the plain load composes cleanly into valign's two-phase streaming. `[HIGH/OBSERVED]`
* Store side: `module__xdref_widestshift_512_W_6` is the inverse — it splices the source
  datum *into* the line at bit `(addr & 0x3f) << 3`, with `mask_sav` gating the live bytes.
  `[HIGH/OBSERVED]`

> **NOTE — VALIGN is the front-end, the LSU is the unit.** The complete reimplementation
> recipe: `LALIGN` primes the align register from one aligned 64-byte line (LAT 9 def);
> each `LA<w>` reads that carried tail (`valignr` use) + the next aligned line, funnels them
> with `wideldshift_W_512_6` by the low address bits, writes the byte-misaligned 512-bit
> vector (LAT 10), and re-primes the align register with the new tail. Same slots
> (`S0_LdSt`/`S1_Ld`), same funnel, same phase bit as the aligned load — only the *two-line*
> composition and the align-register carry are new. `[HIGH/OBSERVED]`

---

## 3. Shuffle / Select — the lane-permute crossbar

### 3.1 The crossbar wall — per-output-lane index `[HIGH/OBSERVED]`

The unifying property of this leg is the **per-output-lane index vector**: a *different*
source lane may be chosen for *every* output lane, in one issue. That is the structural test
separating it from the single-scalar-indexed `rep`/`extr` ([B16](../isa/ref/b16-vec-rep.md),
which routes *one* lane) and from the memory-indexed gather/scatter ([SuperGather](./cas-supergather.md),
which computes a per-lane *address*). The crossbar routes lanes *inside* the register file
with no memory touch. The deeper RTL mux is the **`xdsem_tiesel_5_32`** 5-bit 32-way lane
mux + the **`xdsem_bitkill`** per-lane predicate kill — [Group II](../isa/semantics/group-semantics-ii.md)'s
domain; this page documents the cas decode/timing and the fiss value shape. `[HIGH/CARRIED — B21 §0]`

### 3.2 The control word — how many sources reach the mux `[HIGH/OBSERVED]`

The four compute shapes differ purely in **how wide the control index is** and **how many
result vectors are written**, read byte-exact from the operand decode of each `*_issue`:

| shape | mnemonics | operands | control reach | result lanes |
|---|---|---|---|---|
| **SHFL** | `SHFLNX16` `SHFL2NX8` `SHFLN_2X32` | 3: `vt`(o) `vr`(i) `sr`(i) | `vt[k] = vr[ctrl[k]]` — **1 source** (`log2(N)`-bit index) | N |
| **SEL** | `SELNX16` `SEL2NX8` `SELN_2X32` | 4: `vt`(o) `vs`(i) `vr`(i) `sr`(i) | `vt[k] = {vr ++ vs}[ctrl[k]]` — **2 sources** (1 bit wider) | N |
| **DSEL** | `DSELNX16` `DSELN_2X32` | 5: `vu`(o) `vt`(o) `vs`(i) `vr`(i) `sr`(i) | two permutations of `{vr++vs}` | **2N** (dual output) |
| **DCMPRS** | `DCMPRS2NX8` | 3: `vt`(o) `vr`(i) `vbr`(i) | predicate-driven byte EXPAND/compress | N |
| **SHFL/SEL `_S0/_S2/_S4`** | `SHFL2NX8I_S0…` `SEL2NX8I_S0…` | slot-pinned immediate (`isel`/`ishfl`) | same byte-permute, locked to LdSt/Mul/ALU2 slot for co-issue | N |

The decisive cas-side evidence is the **vec-operand count and the control latency**.
`F0_F0_S3_ALU_36_inst_IVP_SELNX16_issue` @ `0x14b2fb0`, verbatim:

```asm
; SELNX16 issue @ 0x14b2fb0  -- FOUR opnd_sem_vec_addr, the 4th deeper
14b2fcb:  call opnd_sem_vec_addr@plt ; OPERAND vt (output)
14b2fd9:  mov  $0xa,%esi             ;   LAT 10
14b2ffb:  call opnd_sem_vec_addr@plt ; OPERAND vs (source 2)
14b3009:  mov  $0xa,%esi             ;   LAT 10
14b302e:  call opnd_sem_vec_addr@plt ; OPERAND vr (source 1)
14b303c:  mov  $0xa,%esi             ;   LAT 10
14b3057:  call opnd_sem_vec_addr@plt ; OPERAND sr (CONTROL selector vector)
14b306c:  mov  $0xc,%esi             ;   LAT 12   <-- one stage DEEPER than the data
```

`SHFLNX16` @ `0x14b3080` is the same shape with **one fewer source** (`vt`,`vr` @ LAT 10,
then `sr` @ LAT 12 — three operands, not four), and `DSELNX16` @ `0x14b4940` has **five**
vec operands (the dual `vu`/`vt` outputs both at LAT 10). The control-vector `sr` resolving
at **LAT 12** while the data/result resolve at **LAT 10** is the crossbar's signature: the
extra two-stage read depth is the lane mux selecting its sources. `[HIGH/OBSERVED — every
LAT byte read this pass; operand counts cross-checked against B21 §2.2 descriptor table]`

> **GOTCHA — `SEL`'s index is one bit wider than `SHFL`'s.** `SHFLNX16` selects among **32**
> lanes of one source (5-bit index); `SELNX16` selects among **64** lanes of `{vr ++ vs}`
> (6-bit index). A reimplementation that sizes the control field by lane count alone will
> truncate `SEL`'s top index bit and silently fold the second source out. The `xdsem_tiesel_5_32`
> mux name encodes the 5-bit SHFL width; SEL widens it by the concat bit. `[HIGH/OBSERVED operand count; CARRIED for the mux width]`

### 3.3 The fiss VALUE — the per-lane gather `[HIGH/OBSERVED]`

The value leaves spell the operand arity directly (`512` = a 512-bit vector argument):

```text
module__xdref_shfl_nx16_512_512_512          ; 3 args: vr(512) + ctrl(512) -> vt(512)
module__xdref_sel_nx16_512_512_512_512       ; 4 args: vr + vs + ctrl -> vt
module__xdref_dsel_nx16_512_512_512_512_32   ; dual-out: vr + vs + ctrl -> {vu, vt}, +32 imm
module__xdref_sels_nx16_16_512_32            ; scalar-broadcast select (the rep-datapath cousin)
```

```c
/* fiss SHFL value: per-output-lane gather by the control word.
 * Each output lane k reads source lane = ctrl[k] (lane-granular, whole element). */
v512 shfl_nx16(v512 vr, v512 ctrl) {
    v512 vt;
    for (int k = 0; k < 32; k++) {           /* 32 int16 lanes */
        uint idx = lane16(ctrl, k) & 0x1f;   /* 5-bit lane index (xdsem_tiesel_5_32) */
        lane16(vt, k) = lane16(vr, idx);     /* copy whole 16-bit element */
    }
    return vt;
}
/* SEL is the same with a 6-bit index over concat(vr, vs):
 *   idx = lane16(ctrl,k) & 0x3f;  src = (idx < 32) ? vr[idx] : vs[idx-32];   */
```

The permute acts at **lane granularity** — an output lane copies a whole input *element*,
not a sub-element byte (B21 §1.1). The scalar-broadcast `sels*` forms (`sels_nx16_16_512_32`)
spell like this family but ride the `vec_rep` single-scalar-index datapath ([B16](../isa/ref/b16-vec-rep.md));
they are cited here only as adjacency. `[HIGH/OBSERVED — leaf signatures; the per-lane loop is the INFERRED reference shape behind them, MED]`

### 3.4 DSEL and DCMPRS — the butterfly and the compactor `[HIGH/OBSERVED]`

* **DSEL** writes **two** result vectors (`vu`, `vt`) from the same source pair in one issue
  — two `'o'`-marked operand descriptors in the encoding table (B21 §2.2), the cas issue's
  five vec operands (§3.2). It is the de-interleave / butterfly stage that
  [StreamTranspose](../firmware/kernels/stream-transpose.md) (POOL opcode `0x6b`, the 32×32
  datapath transpose) builds its lane-permute from — `op@36` pinned to `Bypass`, pure data
  movement. `[HIGH/OBSERVED]`
* **DCMPRS2NX8** is the predicate-driven stream-compaction primitive: `out[k] = vbr[k] ?
  src[popcount(vbr[0..k])] : src[63]` — it packs the predicate-true bytes to the front. Its
  cas issue (`@0x14b7bb0`) reads `vbr`/`vr` at LAT 10 and drains through the pack network at
  LAT 12, pulling a `b32_pr` packed predicate. `[HIGH/CARRIED — B21 §6]`

### 3.5 The predicated `.T` forms — RMW with lane kill `[HIGH/OBSERVED]`

`SELNX16T` / `SHFLNX16T` / `DSELNX16T` add a `vbr` `vbool` operand and mark the destination
`'m'` (inout): killed lanes **keep the destination's prior value** (a read-modify-write). The
kill is the same per-lane `xdsem_bitkill` predicate guard the `_t` arithmetic ops use
([cas arith §2](./cas-arith-sem.md)'s trailing-`T` predication). `[HIGH/CARRIED — B21 §4.3]`

---

## 4. Reduce — the cross-lane fold tree

### 4.1 The fold collapses the lane axis `[HIGH/OBSERVED]`

Reduce is the only `ivp_` vector family that **collapses the 32-lane `vec` register to a
scalar/narrowed result** instead of operating lane-wise (B08 §0). The five sub-families:

| sub-family | mnemonics | dest | semantics |
|---|---|---|---|
| **horizontal sum** | `RADD{2NX8,NX16,N_2X32}` · `RADDU*` · `RADDS*` (+ `_T`) | scalar `vec` lane (widened) | `Σ lane` |
| **horizontal min/max** | `RMAX/RMIN{…}` · `RMAXU/RMINU*` · fp `RMAXNUM/RMINNUM*` (+ `_T`) | scalar lane (no widen) | `max/min` over lanes |
| **reduce-and-flag** | `RBMAX/RBMIN{…}` · fp `RBMAXNUM/RBMINNUM*` (+ `_T`) | scalar lane **+ `vbool`** | extremum + **argmax/argmin** mask |
| **tail-predicate** | `LTR{N,2N,SN,…}` (`S1_Ld` slot) | `vbool` | `vb[l] = (l < n)` |
| **boolean fold** | `RANDB{N,2N,N_2}` (AND) · `RORB{…}` (OR) (`S1_Ld`) | 1 bit | all-true / any-true |

> **The three widening rules — read from the fiss leaf signatures.**
> `radd_nx16_32_512` (sum) → **32-bit** output (the `_32_` middle token): the accumulator
> **widens** `nx16 → 32b` to hold the 32-lane sum without overflow. `rmax_nx16_16_512`
> (max) → **16-bit** output: min/max **do not widen** (the extremum fits in the element
> width). `radds_nx16_16_512` (saturating sum) → **16-bit**, **clamped** to int16. These
> three signatures are the byte witness for the widening table. `[HIGH/OBSERVED — leaf names]`

> **CORRECTION — there is no reduce-subtract and no reduce-XOR opcode.** The arithmetic
> reduces are **add / max / min only**. The decoder group spelled `op_sub` in the reduce
> block is the `rb`-prefix *bool-bounded* (argmax/argmin) selector, **not** an arithmetic
> subtract; `rxorb` is a member of neither fold block (only `randb`/`rorb` exist). Do not
> emit a phantom reduce-subtract / reduce-XOR. `[HIGH/OBSERVED — roster; CARRIED from Group II §3.2]`

### 4.2 The cas TIMING — result LAT 10, fold-source LAT 12 `[HIGH/OBSERVED]`

`F0_F0_S3_ALU_36_inst_IVP_RADDNX16_issue` @ `0x14b2f40` decodes exactly two vec operands —
the **result lane** and the **full-vector fold source** — at the same staggered latency as
the crossbar:

```asm
; RADDNX16 issue @ 0x14b2f40
14b2f5e:  and  $0x18,%edx              ; result-lane index masking
14b2f65:  call opnd_sem_vec_addr@plt   ; OPERAND result (the reduced scalar lane)
14b2f73:  mov  $0xa,%esi               ;   LAT 10
14b2f8e:  call opnd_sem_vec_addr@plt   ; OPERAND source (the 512-bit vec being folded)
14b2fa3:  mov  $0xc,%esi               ;   LAT 12   <-- the fold-tree read depth
```

The result posts at **LAT 10** (the S3-ALU vector-execute stage, forwardable like any ALU
op), while the **fold source resolves at LAT 12** — the same two-stage extra read depth the
crossbar shows, here because the fold tree must read all 32 lanes before it can produce the
scalar. So reduce and select share the S3-ALU `(data@10, deep-read@12)` timing shape.
`[HIGH/OBSERVED]`

The reduce decode reuses the per-`(format,slot,mnemonic)` decode-bitmap dispatch of
[cas arith §3.1](./cas-arith-sem.md): each reduce-op's `stage10` wrapper sets a **distinct
opcode-selector bit** in the `0x734..0x73a` region before the **one shared**
`ivp_sem_vec_reduce_semantic_stage10` (@`0x1525e00`) runs — `RADDNX16` lights `$0x20,0x734`,
`RMINNX16` `$0x80,0x736`, `RMAXNX16` `$0x40,0x735`, `RBMINNX16` `$0x04,0x73a` — then clears
it on return. The shared body funnels to the host VALUE callback via the per-instruction
vtable (`mov $0xa,%esi ; call *0x222ea8(%rbx)`), confirming **execute fires at stage 10**,
exactly like the lane-wise ALU. So the op identity is carried by one decode bit; the fold
*value* is host-side. `[HIGH/OBSERVED]`

### 4.3 The fiss VALUE — device tree vs oracle flat fold `[HIGH/OBSERVED]`

This is the sharpest cas/fiss split on the page. The **device realises a balanced log-step
reduce tree** (for timing — what the cas latency models), while the **fiss `xdref_r*` oracle
realises a FLAT, fully-unrolled left-to-right fold** (for value). They are
**value-equivalent by the associativity license**. Disassembling `module__xdref_radd_nx16_32_512`
@ `0x858690` proves the oracle shape is flat, not a tree:

```asm
; module__xdref_radd_nx16_32_512 @ 0x858690  -- FULLY UNROLLED, one movzwl per lane
85869d:  add    $0xffffffffffffff80,%rsp     ; 0x80-byte stack frame for the 32 staged lanes
8586a1:  movzwl 0x3e(%rsi),%esi              ; lane 31  (zero-extend i16 -> wider accum)
85869d…:  ...                               ; (each lane staged via  lea 0xNN(%rsp),%rdx)
8586af:  movzwl 0x3c(%r12),%esi              ; lane 30   <- source offset steps by 2 (one i16)
8586c2:  movzwl 0x3a(%r12),%esi              ; lane 29
8586d5:  movzwl 0x38(%r12),%esi              ; lane 28
   ...                                       ; ... descending to lane 0 (offset 0x00) ...
```

The body reads each of the 32 int16 lanes with `movzwl 0xNN(%r12),%esi` at descending source
offsets stepping by 2 bytes (one int16 lane each) — a **flat unrolled accumulate**, not a
log-depth butterfly. The `movzwl` (zero-extend) into the wider accumulator is the byte
witness for the `nx16 → 32b` widening. `[HIGH/OBSERVED]`

```c
/* fiss reduce VALUE (the oracle's flat fold) -- value-equivalent to the device tree */
int32_t radd_nx16_32_512(v512 v) {       /* radds_* would clamp to int16 instead */
    int32_t acc = (int16_t)lane16(v, 0);
    for (int i = 1; i < 32; i++)
        acc += (int16_t)lane16(v, i);    /* WIDENING accumulate; flat, associative */
    return acc;                          /* lands in the result lane; rb* also writes vbt=argmax */
}
/* The DEVICE realises the same sum as a balanced tree:
 *   ADD     : ivp_sem_csa_8_16_32_l0/l1/l2 (3:2 carry-save) -> reduce_stage1
 *   MAX/MIN : ivp_sem_minmax_prep/stage0   -> reduce_stage1
 *   fp NaN  : ivp_sem_rminmaxnum_{f16,f32}_unpack -> _2_to_1_select / _4_to_1_select */
```

> **The reimplementation takeaway for reduce.** Model the **timing** as a log-step tree
> (the device's carry-save / min-max tree — that is what the LAT-12 fold-source read depth
> measures), but compute the **value** with any associative fold order — the shipped oracle
> uses the simplest one (flat left-to-right) precisely because the fold is associative. The
> only value subtleties are the *widening* (`radd` widens, `rmax`/`rmin` don't, `radds`
> saturates) and the fp *NaN-suppression* (`RMAXNUM`/`RMINNUM` implement IEEE `maxNum`/`minNum`).
> `[HIGH/OBSERVED + CARRIED from Group II §3.2]`

### 4.4 The block / butterfly reduce `RB*` and the bool fold `[HIGH/OBSERVED]`

* **`RBMINNX16`** (`@0x14b5220`) and the `rb*` family fold the same way **plus** emit a
  `vbool` marking the argmin/argmax lane(s). Its cas issue decodes **three** operands: a
  **`vbool` def** (the flag/argmin mask, `opnd_sem_vbool_addr`) at LAT 12, the single vector
  source at LAT 10, and the dst lane at LAT 12 — the extra `vbool` output is the only
  structural difference from plain `RMINNX16`. The fiss leaf carries the matching predicate
  output: `rbmin_nx16_64_512_512` (block size `64`, in `512`, out `512` + the `_64_` flag
  width), built from the scalar **`module__xdref_rbmin_16`** tree node @ `0x859450` — a
  **min-of-two with argmin-index tracking**: it biases each value's sign bit so an unsigned
  `cmp` orders signed int16 correctly, keeps the smaller `{value, lane-index}` pair, and on a
  **tie picks the lower index** (the deterministic argmin tie-break). `[HIGH/OBSERVED — the 3-operand issue + the rbmin_16 tie-break body]`
* **`RANDBN` / `RORBN`** fold a 64-bit `vbool` to a **single bit** (`randbn_64_64` =
  AND/all-true, `rorbn_64_64` = OR/any-true), in the **`S1_Ld`** load slot (§1 QUIRK).
  These have no distinct `*_issue` symbol of their own — like stores ([load/store §2.6](./cas-load-store.md)),
  they ride a select-bit under the shared LSU semantic; the *value* is the `randbn`/`rorbn`
  leaf. `[HIGH/OBSERVED for the leaf + load-slot; MED for the select-bit dispatch]`

> **NOTE — scan is software, there is no reduce-scan opcode.** The prefix scan
> ([Group II §3.3](../isa/semantics/group-semantics-ii.md)) is built from a closed lane
> **rotate** (`op_ROT`: `ROTRN`/`ROTRI` — [B12 shift](../isa/ref/b12-shift.md)) + a per-step
> associative **combine** (Add/Max/Mult from the ALU/MAC slices), Hillis-Steele style
> (stride `1,2,4,…`). The rotate is **element-granular** (lane index), distinct from VALIGN's
> **word-granular** funnel. Reduce collapses, scan accumulates, valign streams — three
> different lane motions. `[HIGH/CARRIED — Group II §3.3]`

---

## 5. The firmware consumers

The three legs surface in firmware as named POOL/DVE kernels — the cross-check that the
ISS roster is the real one:

* **[CrossLaneReduce](../firmware/kernels/cross-lane-reduce.md)** — POOL opcodes `0x7c`
  (`CrossLaneReduceArith`) / `0x7d` (`CrossLaneReduceBitvec`) share one 64-byte
  `NEURON_ISA_TPB_S4D4_CR_STRUCT` and one 6-entry `NEURON_ISA_TPB_REDUCE_OP` enum; both tail
  into `cross_lane_reduce_impl(bool)` which issues the `ivp_r*` ops of §4 and pins their
  value/widening/saturation by driving the `module__xdref_r*` oracle leaves. The `_arith`
  worker uses `radd/rmax/rmin`; the `_bitvec` worker uses `randb/rorb`. `[HIGH/CARRIED]`
* **[StreamTranspose](../firmware/kernels/stream-transpose.md)** — DVE opcode `0x6b`, a 32×32
  lane-permute transpose realised by the **`sel`/`dsel` butterfly** of §3 (the `op@36` ALU-op
  field pinned to `Bypass`, pure data movement, no HBM round-trip). The distinct *descriptor*
  transpose is the DGE crossbar; this is the *datapath* one. `[HIGH/CARRIED]`
* **VALIGN** is the streaming front-end every misaligned vector kernel uses; it has no single
  named firmware opcode (it is the `LA*`/`SA*` instruction stream the compiler emits around
  unaligned tensor tiles). `[MED/INFERRED]`

---

## 6. The validation hook (VAL-05, Part 15)

All three legs are clean stimulus/response oracles, ideal for the differential **VAL-05**
harness Part 15 will define:

* **VALIGN** — feed `(align-reg carry, fresh line, unaligned addr)`, assert the funnelled
  512-bit vector and the re-primed align-register tail match a candidate. The LAT-9 align
  def and the word-granular `wideldshift_W_512_6` window are the two highest-value invariants.
* **Crossbar** — feed `(vr, vs, control word)`, assert the per-lane gather byte-for-byte;
  pin the SEL 6-bit vs SHFL 5-bit index width and the LAT-12 control read.
* **Reduce** — feed a 512-bit vector, assert the scalar fold (and the `rb*` argmax `vbool`);
  pin the three widening rules (`radd`→32b, `rmax`→16b, `radds`→sat16) and the
  associativity-equivalence between the device tree and the fiss flat fold.

Each is driven through the fiss `module__xdref_*` leaf and a candidate reimplementation; the
ISS itself supplies the cycle-accurate timing oracle to check the LAT-9/10/12 schedule.

---

## 7. Honesty / uncertainty ledger

**`[HIGH/OBSERVED]` (disassembled / read-from-byte this pass):**

* **VALIGN prime state machine** — `LALIGN_IP` @ `0x71dc20`: base AR ×2 (`& 0xf`, LAT 1) +
  the `valign` register read **and** written (`opnd_sem_valign_addr & 0x3` → 4-entry file)
  at **LAT 9**; the `valignr` operand role is `use`-only (no `def`); the `my_valign_*`
  census is **24 total / 19 operand-role (10 uul / 5 uus / 4 valignr) + 5 internal**.
* **Crossbar** — `SELNX16` @ `0x14b2fb0` (4 vec: 3 data @ LAT 10 + control `sr` @ LAT 12);
  `SHFLNX16` @ `0x14b3080` (one fewer source); `DSELNX16` @ `0x14b4940` (dual output, 5 vec);
  the SEL-6-bit vs SHFL-5-bit index width.
* **Reduce** — `RADDNX16` @ `0x14b2f40` (result @ LAT 10, fold source @ LAT 12); `radd`/`rmax`/`rmin`
  in `S3_ALU` (9 placements); `ltr` in `S1_Ld`; the three widening signatures
  (`radd_nx16_32_512` / `rmax_nx16_16_512` / `radds_nx16_16_512`).
* **fiss flat fold** — `module__xdref_radd_nx16_32_512` @ `0x858690` is a fully-unrolled,
  per-lane `movzwl` accumulate (NOT a butterfly); the widening zero-extend.
* **fiss funnel reuse** — valign has **no** `xdref_valign_*` leaf; it reuses
  `wideldshift_W_512_6` / `widestshift_512_W_6` across two lines.
* The roster: `IVP_ZALIGN`/`MALIGN`/`LALIGN`/`SALIGN`/`SAPOS` present; the sel/shfl/dsel/dcmprs
  and radd/rmax/rmin/rb*/randb/rorb mnemonic sets.

**`[MED/INFERRED]`:**

* The two-aligned-load *memory composition* of a misaligned access (standard Tensilica
  idiom; the funnel + align-register state are OBSERVED, the memory pairing INFERRED).
* The per-lane reference *loop* shape behind the `shfl`/`sel` leaves (the leaf signature is
  OBSERVED; the loop is the reference reconstruction).
* The `randb`/`rorb` select-bit dispatch under the shared LSU semantic (no distinct issue
  symbol observed); the `SAPOS_FP` per-routine flush detail.

**`[…/CARRIED]` (re-grounded from a HIGH sibling):**

* The `randb`/`rorb` `S1_Ld` slot (B08 §1, HIGH there; corroborated by `LTRN`-S1 OBSERVED
  here); the `xdsem_tiesel_5_32` / `xdsem_bitkill` RTL mux (Group II); the device reduce-tree
  family (`csa_8_16_32`, `minmax_prep`) and the no-reduce-subtract/XOR correction (Group II §3.2);
  the `.T` RMW kill (B21 §4.3); the CrossLaneReduce / StreamTranspose firmware bindings.

> **The single most important takeaway.** All three legs share the cas/fiss split, but each
> needs a *different* model. **VALIGN** is a stateful register machine (the align register is
> read-and-rewritten every access, LAT 9, word-granular funnel) — model the state, not just
> the value. **Crossbar** and **Reduce** share one S3-ALU timing shape — **data/result at
> LAT 10, the control vector / fold source one stage deeper at LAT 12** (the extra read depth
> *is* the lane mux / fold tree). And for reduce, **model the timing as a log-step tree but
> compute the value with any associative order** — the shipped oracle proves the value is a
> flat fold. Do not look for a permuted or reduced datum inside `libcas-core`; it is
> physically not there — get it from the `module__xdref_*` leaves. `[HIGH/OBSERVED]`
