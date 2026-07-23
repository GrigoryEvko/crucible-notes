# cas/fiss Load/Store (LSU) Semantics

This page documents the **vector & scalar load/store family** — the LSU — as the
cycle-accurate ISS models it across its two cooperating plugins. It is one node of
Part 14, *the ISS as executable oracle*: the place you go when the ISA reference
tells you a `lsnx16.i` mnemonic *exists* and you need to know, to the bit, **what
address it forms, what bytes it touches, how it extracts an element, and what the
base register holds afterward** — answers the ISS computes mechanically and a
reimplementation must match.

The LSU is where the universal **cas/fiss split** is at its sharpest. The
cycle-accurate timing core in `libcas-core.so` (45,878,080 B, ELF64 x86-64, **not
stripped**) models the Cayman / Vision-Q7 IVP VLIW pipeline: it does **decode +
timing** — it resolves which architectural registers an op reads and writes, posts
each at its latency, and threads the op through the LSU's structural hazards — but
it computes **no datum**. The value math lives in `libfiss-base.so` (12,330,016 B,
self-contained): 864 `module__xdref_*` value functions, reached from cas through the
119 `nx_*_interface` TIE-port callbacks. For a load, the "value" is the loaded vector
itself, produced by the fiss `memload` stage (the host memory read) and the
`wideldshift` element-extract funnel; for a store, it is the inverse `widestshift`
funnel that splices a lane back into a 64-byte line. **This page traces exactly
where cas hands the memory access off to the host model and where fiss provides the
funnel transform.**

The surface-level anatomy of that split — the plugin ABI, the `_issue`/`_stall`
naming, the 119 ports — is [libcas-core Surface + ISS Plugin ABI](cas-core-surface.md).
The ISA-reference roster and opcodes this page realizes are
[B06 — Loads](../isa/ref/b06-loads.md) and [B07 — Stores](../isa/ref/b07-stores.md).
The microarchitectural LSU model (pipes, ports, regions) is
[LSU + Memory Model](../uarch/lsu-memory.md), and the formal load/store semantics are
[Group I Semantics](../isa/semantics/group-semantics-i.md). The **unaligned** wrapper
built on top of this aligned primitive — VALIGN — is in the shuffle/valign page
[cas valign / shuffle / reduce](cas-valign-shuffle-reduce.md); the **indexed**
(per-lane scatter/gather) counterpart is [cas SuperGather](cas-supergather.md).

> **Confidence tags.** `[OBSERVED]` byte-exact from a shipped symbol / address /
> instruction; `[INFERRED]` reasoned over observed control/data flow;
> `[CARRIED]` carried from a cited sibling page. `HIGH/MED/LOW` =
> confidence. Every cas/fiss symbol and address below is from `nm`/`objdump -d` of
> the two shipped `.so` files by absolute path. `.text`/`.rodata` have **VMA ==
> file offset** (confirmed `readelf -SW`: `.text` @ `0x572fa0`); `.data` carries a
> `0x200000` delta (`.data` VMA `0x2280ed8`, file offset `0x2080ed8`) — irrelevant
> here, every routine cited is in `.text`.

> **GUARD.** The modeled core is **Cayman / Vision-Q7** (CoreV3-class IVP). The LSU
> slots, latencies and ports below are that one ISS configuration; cross-gen deltas
> are not in scope. "GPSIMD" = the POOL-engine Vision-Q7 cores. Provenance is pure
> static RE of the two shipped binaries (lawful interoperability, DMCA 1201(f)); no
> external source tree is consulted or referenced.

---

## 1. The family — what counts as a load/store, and where it issues

### 1.1 Two naming tiers, one operand grammar
The plain (non-indexed) memory-access family splits into two IVP / Tensilica naming
tiers, recovered by an `nm` sweep of `libcas-core.so` (35 load/store roots):

| tier | roots (each × `{_I,_IP,_X,_XP}`) | nature |
| --- | --- | --- |
| **LV / SV** | `LV2NX8`, `LVNX8S`, `LVNX8U`, `LVN_2X16S`, `LVN_2X16U` (+ `SV*`, + `_T` pred. stores) | variable / element-addressed vector ld/st (8-bit roots) |
| **LS / SS** | `LSNX16`, `LSNX8S`, `LS2NX8`, `LSN_2X16S`, `LSN_2X32`, `LSN_4X64`, `LSN_8X128`, `LSN_16X256` | aligned-stride vector ld/st (16-bit + wide patterned) |
| **LSR** | `LSR2NX8`, `LSRNX16`, `LSRN_2X32`, `LSRN_4X64` | stream-**rotate** aligned load (rotating-buffer streaming) |

> **GOTCHA.** The 16-bit plain vector load is **`LSNX16` / `SSNX16`** — it lives in
> the *aligned* `LS/SS` tier. There is **no `LVNX16` / `SVNX16`**; the 16-bit width
> is not a variable-element form. Reach for `LS*` when you want the contiguous
> 16-bit vector load, `LV*` only for the 8-bit / `n_2x16` element forms.

Two prefixed families look like loads but are **not plain data loads**: `LB*`
(bit-load) and the compare-loads `LE*/LT*/LTU*/LTR*` compute a `vbool` boundary
predicate from a vector (the gather `vbMask` producers `ivp_ltun/leun_2x32`, consumed
by [SuperGather](cas-supergather.md)) — they carry an `L` prefix but produce a
predicate, not a memory datum. `[HIGH/OBSERVED roster; MED for the semantic binding via the gather page]`

### 1.2 Slot placement — `S0_LdSt` and `S1_Ld`, every FLIX format
Every load issues in the two LSU slots of every wide FLIX format `F0..F7,F11` and the
narrow `N0/N1/N2`. The `LSNX16_I` issue symbol is present in (sampled from `nm`):

```text
F0_S1_Ld   F0_S0_LdSt   F1_S0_LdStALU   F1_S1_Ld   F2_S1_Ld
F3_S0_LdSt F3_S1_Ld     F4_S0_Ld        F4_S1_Ld   F6_S0_LdSt
F6_S1_Ld   F7_S0_LdSt   F7_S1_Ld        N2_S0_LdSt N2_S1_Ld
```

So the LSU exposes two slots: **`S0_LdSt`** = address-generation + the store path,
**`S1_Ld`** = the load drain (`F1` folds them into the combined `S0_LdStALU`). A
bundle can co-issue one `S0` op and one `S1` op — **two memory ops per cycle**,
modeled by the two host pipes `nx_Load_0` / `nx_Load_1`. These are exactly the two
slots [valign](cas-valign-shuffle-reduce.md) uses for `lalign(S0+S1)`/`salign(S0)`
and [SuperGather](cas-supergather.md) uses for `gather(S0)`/`drain(S1)`. The plain
aligned load **is** the LSU primitive those two build on.
### 1.3 The scalar base-ISA load/store
Distinct from the vector LSU is the Xtensa scalar core ld/st, present in both
binaries: `L32I/L32I_N/L32R/L32AI/L32E` (32-bit word), `L16UI/L16SI/L16U/L16S`
(16-bit zero/sign-extending), `L8UI/L8` (8-bit); `S32I/S32I_N/S32RI/S32E/S32NB/S32STK`,
`S16I`, `S8I`. These have **hard natural alignment** (§3.4) — a different rule from
the element-tolerant vector LSU.

---

## 2. The four addressing modes — `_i / _x / _ip / _xp` (cas DECODE)

The universal `{_i,_x,_ip,_xp}` suffix grid is decoded byte-exact from the four
`F0_F0_S1_Ld_16_inst_IVP_LSNX16_*_issue` routines. Their addresses are the canonical
anchors:

| suffix | symbol | addr | meaning |
| --- | --- | --- | --- |
| `_i` | `..._LSNX16_I_issue` | `0x71ddc0` | `addr = base_AR + IMM` (IMM is an instruction-word field) |
| `_x` | `..._LSNX16_X_issue` | `0x71deb0` | `addr = base_AR + index_AR` |
| `_ip` | `..._LSNX16_IP_issue` | `0x71de20` | `addr = base_AR`; then `base_AR := base_AR + IMM` |
| `_xp` | `..._LSNX16_XP_issue` | `0x71df40` | `addr = base_AR`; then `base_AR := base_AR + index_AR` |

The operand fields are identical across the grid — what changes is **how many AR
operands are decoded and whether the base AR is defined a second time**.

### 2.1 `_i` immediate-offset (`0x71ddc0`)
The issue routine decodes exactly two operands:

```c
// F0_F0_S1_Ld_16_inst_IVP_LSNX16_I_issue @ 0x71ddc0
// rsi -> instruction word; rdi -> ISS op context
uint32_t iw = *(uint32_t*)instr_word;          // mov (%rsi),%esi

// OPERAND 1 — base address register: low 4 bits of the word
opnd_sem_AR_addr(ctx, iw & 0xf);               // and $0xf,%esi ; call AR_addr@plt
ctx->AR_def_hook(/*esi=*/ 1);                  // hook 0x14eb0, LAT = 1  (address ready immediately)

// OPERAND 2 — destination vector: bits [8:4] (a 5-bit vec field)
uint32_t vec = (iw << 0x13) >> 0x1b;           // shl $0x13 ; shr $0x1b  => bits[8:4]
opnd_sem_vec_addr(ctx, vec);                   // call vec_addr@plt
ctx->vec_host_hook(/*esi=*/ 0xa);              // hook 0x15200, LAT = 10 (vector load-use)
```

The **immediate offset is not a register operand** — it is a field in the
instruction word, consumed later in the fiss execute stage (§5). cas posts the base
AR at LAT 1 and the destination vector at LAT 10.
### 2.2 `_x` register-indexed (`0x71deb0`)
`_x` decodes a **third** operand — a second AR holding the index — between the base
and the vector:

```c
// ..._LSNX16_X_issue @ 0x71deb0
opnd_sem_AR_addr(ctx, iw & 0xf);                       // OPERAND 1: base AR, LAT 1
ctx->AR_def_hook(1);

uint32_t index = (eax << 0x18) >> 0x1c;                // OPERAND 2: index AR = bits[11:8] (4-bit)
opnd_sem_AR_addr(ctx, index);                          //   shl $0x18 ; shr $0x1c
ctx->AR_def_hook(1);                                   //   LAT 1

uint32_t vec = (iw << 0x13) >> 0x1b;                   // OPERAND 3: dst vec = bits[8:4]
opnd_sem_vec_addr(ctx, vec);
ctx->vec_host_hook(0xa);                               // LAT 10
```

`addr = base + index_AR`. The index is a full architectural AR, read at LAT 1.
### 2.3 `_ip` immediate post-increment (`0x71de20`) — **the post-update rule**
`_ip` is the keystone of the post-increment family. The cas issue decodes the base
AR **twice** — once as the *read* base, once as the *write-back* def of the same
register number:

```c
// ..._LSNX16_IP_issue @ 0x71de20
opnd_sem_AR_addr(ctx, iw & 0xf);     // OPERAND 1: base AR (the READ base)   -- and $0xf
ctx->AR_def_hook(1);                 //   LAT 1

opnd_sem_AR_addr(ctx, iw & 0xf);     // OPERAND 2: SAME AR (the post-UPDATE) -- and $0xf  (again!)
ctx->AR_def_hook(1);                 //   LAT 1 -- the base-register auto-update def

uint32_t vec = (iw << 0x13) >> 0x1b; // OPERAND 3: dst vec = bits[8:4]
opnd_sem_vec_addr(ctx, vec);
ctx->vec_host_hook(0xa);             // LAT 10
```

The decisive evidence is the **doubled `and $0xf`**: `_i` masks the base register
number once (one AR operand); `_ip` masks the identical field a second time and posts
it as a fresh AR def. That second AR def **is** the post-increment write-back. The
address used by the access is the *current* base; the base register is then
re-defined to the incremented value.
> **QUIRK.** Post-increment costs an extra architectural register **write** but
> updates *the same register it read* — so a chain of `_ip` loads walking a buffer
> never frees the base AR for another use; the scoreboard sees a fresh def of that
> AR at LAT 1 every iteration. A reimplementation that models the base as
> read-only on `_ip` will diverge: the ISS posts a second `AR_def`.

### 2.4 `_xp` register post-increment (`0x71df40`)
`_xp` is `_ip`'s shape with the increment sourced from the **index register** instead
of an immediate: base AR (read) + index AR + base-AR write-back (`base := base + index`)
+ dst vec. The per-format encoding template that repacks the `_x → _xp` delta — a
fixed positional bit-shift per FLIX format — and the roster-wide post-update marker
(`opcodes[].flags` bit 11, set on all `ivp_*_ip/_xp` plus `lddr32.p`/`sddr32.p`) are
carried from the ISA-reference encoding page. `[HIGH for the 2-AR cas decode OBSERVED here; CARRIED for the flags/template]`

### 2.5 LV (variable) vs LS (aligned) — **identical cas decode**
`N1_N1_S0_LdSt_4_inst_IVP_LV2NX8_I_issue` decodes base AR (`& 0xf`, LAT 1) + dst vec
(bits[8:4], LAT 10) — byte-for-byte the same operand grammar as `LSNX16_I`. **LV and
LS differ only in the fiss value** (element width and alignment-fault behavior, §3–4),
never in the cas operand decode or timing.
### 2.6 The store decode — one shared LSU semantic
Stores carry **no separate `_issue` symbol**. The admit/decode lives in a thin
per-mnemonic wrapper `<fmt>_<slot>_IVP_<m>_inst_stage1` that sets a per-mnemonic
select bit, calls the **one shared** LSU decode for that (format, slot), then clears
the bit:

```text
F0_F0_S0_LdSt_4_IVP_SSNX16_I_inst_stage1 @ 0x119db40:
  orb  $0x10,0x6f4(%rdi)                                    ; set the per-mnemonic select bit
  call F0_F0_S0_LdSt_4_ivp_sem_ld_st_semantic_stage1        ; @ 0x1198ff0 (the ONE shared decode)
  andb $0xef,0x6f4(%rbx)                                    ; clear the select bit
  ...                                                        ; (repeated as a select-bit ladder)
```

`ivp_sem_ld_st_semantic_stage1` (`@0x1198ff0` for `F0_S0_LdSt`) is a large flag-bit
dispatcher: a ladder of `testb/testl` over the per-mnemonic select region
(`0x6dc..0xbab` of the op context) choosing which load/store variant decode runs. The
execute handoff is the shared `ivp_sem_ld_st_semantic_stage10.isra`
(`@0x11b54a0`) → the host VALUE callback (vec hook `0x15200`) + the per-instance port
pointers. `[HIGH/OBSERVED for the wrapper + shared-semantic structure; LOW for the exhaustive per-mnemonic flag map (a select-bit ladder, not enumerated field-by-field)]`

---

## 3. Alignment — the natural-element model (why VALIGN exists)

### 3.1 The vector alignment mask scales with element width
Each fiss `memload__ivp_<m>_i` fetches a **64-byte line** at `addr & MASK_W`, where the
mask clears the in-line bits below the element granularity. Read byte-exact from the
`and $0xffffff??,%esi` immediately before the host read (all reads `mov $0x40,%edx`):

| width | element | memload mask | bits cleared | extract funnel |
| --- | --- | --- | --- | --- |
| `nx8` (8b) | 64 × 8-bit | `0xffffffdf` | bit 5 | `wideldshift_256_512_6` |
| `nx16` (16b) | 32 × 16-bit | `0xffffffc1` | bits 1..5 | `wideldshift_16_512_6` |
| `2x32` (32b) | 16 × 32-bit | `0xffffffc3` | bits 2..5 | `wideldshift_32_512_6` |
| `4x64` (64b) | 8 × 64-bit | `0xffffffc7` | bits 3..5 | `wideldshift_64_512_6` |
| `8x128` (128b) | 4 × 128-bit | `0xffffffcf` | bits 4..5 | `wideldshift_128_512_6` |
| `16x256` (256b) | 2 × 256-bit | `0xffffffdf` | bit 5 | `wideldshift_256_512_6` |

This was verified across all six `memload__ivp_lsn_*_i` / `lvnx8u_i` bodies. The mask
forces a **natural-element-aligned 64-byte line read**: a vector load whose address is
element-aligned (e.g. a 16-bit load at any even byte) reads cleanly **without a fault**,
and the residual low bits feed the element-extract funnel.

> **NOTE.** The mask "scales" because the funnel only needs to extract one element
> from within the fetched line; the line is always 64 bytes regardless of width. The
> wider the element, the fewer low address bits the funnel needs, so the mask clears
> fewer of them. The `nx8`/`16x256` masks coincide (`0xdf`, bit 5) — both use the
> `_256` funnel, which is why the table has two `0xdf` rows.

### 3.2 The within-line extract — the funnel shift
`opcode__ivp_lsnx16_i__stage_14` (`@0x390fb0`) computes `edx = (base+offset) & 0x3f`
(the 6-bit byte offset within the 64-byte line) and calls
`module__xdref_wideldshift_16_512_6` (`@0x857410`). Its body, read in full:

```c
// module__xdref_wideldshift_16_512_6 @ 0x857410  (line: 16 dwords; addr_lo = addr & 0x3f)
uint32_t shamt = addr_lo << 3;          // shl $0x3       -> bit shift amount
uint32_t widx  = (shamt & 0x1f0) >> 5;  // and $0x1f0 ; shr $0x5  -> 32-bit word index
if (shamt & 0x10) {                     // and $0x10,%edx -> cross-word case?
    // cross-word funnel: join two adjacent 32-bit words
    lo = line[widx]   >> cl;            // shr %cl,%esi   (cl = shamt within word)
    hi = line[widx+1] << cl;            // shl %cl,%edi
    elem = (lo | hi);
} else {
    elem = line[widx] >> cl;
}
return elem & 0xffff;                   // and $0xffff,%eax -> mask to 16 bits
```

The `_6` token is the 6-bit `addr_lo`. This **same** `wideldshift_W_512_6` family is
what [valign](cas-valign-shuffle-reduce.md) uses behind the unaligned access — the
plain load applies it to the in-line pick over *one* 64-byte line; valign applies it
*across two* lines for full byte misalignment.
### 3.3 The byte-phase bit
`opcode__ivp_lsnx16_i__stage_5` (`@0x390f70`) derives a phase/parity bit from the
instruction word (`shr $1 ; xor $1 ; and $1`) and the memload gates the host read on
`addr & 0x1` ORed with a disable mask (`sete` → a `je` that skips the host read on
disable / odd phase). This is the same byte-phase tracking valign's `lalign` carries,
so the plain aligned word composes cleanly into the align-register two-phase
streaming.
### 3.4 The scalar load **faults** on misalignment
The scalar core is hard-natural-aligned. `memload__l32i` (`@0x88a3a0`):

```c
// memload__l32i @ 0x88a3a0
flag_misaligned = ((addr & 0x3) != 0);   // test $0x3,%sil ; setne %al
if (flag_misaligned) fault |= 0x4;       // or $0x4,%ecx   -> LoadStoreAlignmentException
host_read(addr, /*len=*/ 4);             // mov $0x4,%edx ; call *0x18(%r10)
```

`memload__l16ui` tests `$0x1` (2-byte align); `memload__l8ui` does no alignment test.
The fault surfaces through cas `LoadStoreAlignmentException` (§6.4). So: **scalar ld/st
= hard natural alignment; vector LSU = element-alignment-tolerant via the funnel; full
byte misalignment of a vector access is the VALIGN regime**.
---

## 4. Access width — uniform 64-byte line, per-op element granularity

Every vector LSU access is a **64-byte (512-bit) host transfer** (`mov $0x40,%edx`
before every `memload` host read). The 512-bit vector register is the natural unit
(32 × int16, 64 × int8, 16 × int32). The per-op **element** granularity is selected
purely by the funnel suffix `W`:
```text
LSNX16     -> wideldshift_16_512_6     (16-bit elements, 32 lanes)
LSN_2X32   -> wideldshift_32_512_6     (32-bit,          16 lanes)
LSN_4X64   -> wideldshift_64_512_6     (64-bit,           8 lanes)
LSN_8X128  -> wideldshift_128_512_6    (128-bit,          4 lanes)
LSN_16X256 -> wideldshift_256_512_6    (256-bit,          2 lanes)
LVNX8U     -> wideldshift_256_512_6    (the 2nx8 64×8-bit access)
```

The host width ports confirm the supported set: `nx_MemDataIs{8,16,32,64,128,256,512}Bits`
— eight access widths, each `_0/_1` dual-pipe. `nx_MemDataIs512Bits` is the full-vector
access; the narrower ports serve the sub-vector / scalar accesses.
> **NOTE.** There is **no 1536-bit memory transfer**. The wide MAC accumulator (`wvec`,
> 1536-bit, 4-entry) is filled *from vector registers* by the register op
> `ivp_sem_wvec_pack` (in `S1_Ld`), not by a memory load — the memory side stays
> 64-byte-wide. (See [cas MAC / FMAC](cas-mac-fmac.md).) `[CARRIED]`

---

## 5. The fiss VALUE — the pipeline, address calc, byte layout

A vector load is a **6-stage fiss pipeline**. The `lsnx16_i` chain, byte-exact:

| stage | symbol | addr | what it computes |
| --- | --- | --- | --- |
| `stateload` | `stateload__ivp_lsnx16_i` | `0x3910f0` | instr word + SRs → per-op context (`0x74..0x88`, `0xe0`) |
| `regload` | `regload__ivp_lsnx16_i` | `0x391160` | AR regfile `[idx 0x6c]` → base value → ctx `0x70` |
| `stage_5` | `opcode__ivp_lsnx16_i__stage_5` | `0x390f70` | `addr = base(0x70) + IMM(0x90)` → `0xd8`; phase bit → `0xdc/0xf8` |
| `memload` | `memload__ivp_lsnx16_i` | `0x391180` | read `addr & MASK_W` for `$0x40` B via host port `call *0x18(%r10)` → staging `0x98..0xd4` |
| `stage_14` | `opcode__ivp_lsnx16_i__stage_14` | `0x390fb0` | `(addr & 0x3f)` → `wideldshift_16_512_6` → extracted element |
| `writeback` | `writeback__ivp_lsnx16_i` | `0x391290` | result lanes → vec regfile (`reg# 0x28 << 4`, 16 lanes), guarded by fault flag |

The memload stage is **where cas's decode hands the access to the host memory model**:
`mov 0x40(%r10),%rdi ; mov $0x40,%edx ; call *0x18(%r10)` is the indirect call through
the load port into the host's 64-byte line fetch. fiss does not own the bytes — it
asks the host for them, then applies the funnel.
### 5.1 The `_x` / `_ip` address-compute deltas
The four addressing modes differ only in which context slot supplies the offset and
whether an AR write-back fires:

```c
// _i  : opcode__ivp_lsnx16_i__stage_5  @ 0x390f70
addr = ctx->base /*0x70*/ + ctx->imm    /*0x90*/;   // add 0x90

// _x  : opcode__ivp_lsnx16_x__stage_5  @ 0x391770
addr = ctx->base /*0x70*/ + ctx->index  /*0x94*/;   // add 0x94 (register, not immediate)

// _ip : opcode__ivp_lsnx16_ip__stage_14 @ 0x3913a0  (the post-increment update)
ebp        = ctx->base  /*0x70*/;        // access uses the CURRENT base
r12d       = ctx->incr  /*0x94*/;
new_base   = ebp + r12d;                 // add %r12d,%ebp  -> base := base + increment
shamt      = ctx->addr & 0x3f;           // and $0x3f -- extract offset UNCHANGED
```

The funnel/extract is identical across modes — only the offset **slot** (`0x90`
immediate vs `0x94` register) and the presence of the base-update differ.
### 5.2 The post-increment write-back — vec **and** AR
This is where the second AR def of §2.3 becomes a real regfile write. The `_ip`
write-back emits **two** writes; the `_i` write-back emits **one**:

```c
// writeback__ivp_lsnx16_i  @ 0x391290   -- VECTOR ONLY
write_vec_regfile(ctx->vec_regnum /*0x28*/ << 4, result_lanes);   // 16 lanes, no AR write

// writeback__ivp_lsnx16_ip @ 0x391690   -- VECTOR + AR
write_vec_regfile(ctx->vec_regnum /*0x28*/ << 4, result_lanes);   // 16 lanes
write_ar_regfile (ctx->ar_regnum  /*0x6c*/,      ctx->new_base /*0x74*/);  // the post-update AR
```

The `_i` write-back contains no `0x6c`/`0x74` reference — the extra AR write is the
sole structural difference. This matches the cas issue's doubled `and $0xf` (§2.3).

### 5.3 The store — the inverse funnel
Stores execute at **stage 11** (the write-back stage). `opcode__ivp_ssnx16_i__stage_11`
(`@0x395330`): `addr = base(0x70) + offset(0x90)`; then `(addr & 0x3f)` + the source
datum feed `module__xdref_widestshift_512_16_6` (`@0x857a00`) — the **inverse** of the
load funnel: it splices the 16-bit element *into* the 512-bit line at bit position
`(addr & 0x3f) << 3`. The same phase bit is carried. The host store fires through the
LSU store port. (Store stage 11 matches valign's `salign` drain at stage 11.)
The predicated store `SVNX8UT` packs each lane via `module__xdref_trunc_8_16` (byte
truncate, 32×) and guards each lane by the `vbool` predicate — the same per-lane
masking as the gather/select `_t` forms.
### 5.4 Byte layout / extension

The 64-byte line is staged as 16 consecutive 32-bit words (`0x98..0xd4`, ascending);
the funnel indexes `word = (shamt & 0x1f0) >> 5` then a cross-word join. Lane `k` of
an int16 vector sits at byte `2k` of the line — little-endian, word-ordered,
element-`k`-at-byte-`(k·elemW)`. The word-ordered staging is **OBSERVED**; that the
*silicon* fabric is little-endian is **INFERRED** (the ISS mirrors host x86 LE).

Sign/zero extension on narrow scalar loads is a **mnemonic split**: the unsigned forms
(`LVNX8U`/`L8UI`/`L16UI`) zero-extend, the signed forms (`LVNX8S`/`L16SI`) sign-extend.
The `opcode__*__stage_14` bodies (`L16UI`/`L16SI` @ `0x889df0`/`0x88a080`) just move
the host-fetched value into the AR result slot `0x28`; the width/sign decision is
carried by the upstream host-width fetch (`nx_MemDataIs16Bits` + the U/S decode bit).
`[HIGH for the U/S mnemonic split; MED for the exact extension locus being the host fetch]`

---

## 6. The cas TIMING — latency, occupancy, faults

### 6.1 Per-operand latency
Read from the `mov $LAT,%esi` immediately before each def/use hook:

| operand | LAT | meaning |
| --- | --- | --- |
| base AR (address) | **1** | address ready immediately (def-post) |
| index AR (`_x`/`_xp`) | **1** | |
| AR write-back (`_ip`/`_xp`) | **1** | the post-update def |
| dst/src vector | **10** | the vector load-use latency |
| scalar `L32I` dst | **4** | the AR load-use latency `[CARRIED from the timing page]` |

A load posts its destination vector at **LAT 10** — a dependent vector op stalls on
the latency-aware bypass scoreboard until the loaded vector is forwardable. This is
the vector load-use interlock; the scalar AR load-use is 4. `[HIGH/OBSERVED for the vec/AR/index LATs; CARRIED for the scalar-4 cross-check]`

### 6.2 The stall chain
`F0_F0_S0_LdSt_4_inst_IVP_LSNX16_I_stall` (`@0x11b21f0`) calls, in order:

```text
nx_Load_0_interface     ; the host LOAD PORT — pipe-0 occupancy
opnd_sem_AR_addr        ; base-AR RAW hazard
my_CPENABLE_stall       ; coprocessor / vector-unit enable
my_REV8AR_stall
my_WB_P_stall  /  my_WB_N_stall  /  my_WB_S_stall   ; writeback-port hazards
my_InOCDMode_stall      ; debug-halt interlock
my_MS_DISPST_stall      ; multi-slot dispatch
```

This is the same structural-hazard set the gather post and the generic LSU model use.
The two LSU pipes are `nx_Load_0` / `nx_Load_1`. A store has no architectural result;
it occupies the writeback buffer (`WB_{S,P,N}`) and retires at fiss stage 11, so a
following store stalls on the WB port.
### 6.3 Address-space reach — the host ports
The LSU's host-facing surface (from `nm -D libcas-core.so`):

* **Pipes/ports:** `nx_Load_{0,1}`, `nx_Store_0`, `nx_StoreAcc` (store-accumulate);
  the addressing-mode decomposition `nx_VAddr{Base,Index,Offset,Filter,Res,BaseAlternate,BaseUseAlternate}`
  (`Base+Index+Offset` = the `_i`/`_x` modes; `BaseAlternate` = a second base; `Filter`
  = predicate); the width signal `nx_MemDataIs{8..512}Bits`.
* **Fault ports:** `nx_LoadStore{CrossMemAcc, InvalidTCMAcc, WrongIramAcc, UnsupportedAtomicOp}`.

### 6.4 The exception set
The cas exception table carries the full LSU exception roster:

```text
LoadStoreAlignmentException   LoadStoreProhibitedException     LoadStoreFlixMemoryException
LoadStoreAXIDecErrorException LoadStoreAXISlvErrorException    LoadStoreBusErrorException
LoadStoreTLBMultiHitException LoadStoreSlaveAtomicErrorException
LoadStoreUndefinedAttrException  LoadStoreSGAccErrorException
```

The AXI / TLB / bus exceptions show the access is **fabric + TLB-translated**: the LSU
touches the SoC memory fabric. Reconciled with the ABI memory view — a 64-bit SoC/HBM
address is window-mapped to a 32-bit NX-local address; `dataram` (TCM, fast on-core
local) and IRAM are the other regions. The `InvalidTCMAcc` / `WrongIramAcc` /
`CrossMemAcc` faults gate the three regions (HBM-fabric / dataram-TCM / IRAM). cas
models a **uniform** load-use LAT + WB-port occupancy — **not** a per-region
HBM-vs-TCM latency; the region-dependent latency is host-resolved (the `nx_*MemAccess`
ports return when ready). `[HIGH for the port/fault evidence; MED for the per-region binding — INFERRED from the fault names + the ABI map, not a per-region latency constant]`

---

## 7. The aligned primitive vs the unaligned wrapper

This page is the **aligned LSU primitive**; [valign](cas-valign-shuffle-reduce.md) is
the unaligned wrapper layered on top. They share the unit:

* **Same funnel** — both use `module__xdref_wideldshift_W_512_6`. The plain load picks
  one element within *one* line (`addr & 0x3f`); valign joins *two* lines for full byte
  misalignment.
* **Same slots** — both issue in `S0_LdSt` / `S1_Ld`.
* **Same phase bit** — the plain load carries the byte-phase bit (§3.3) so it feeds the
  align register's two-phase streaming.
* **Same timing** — the plain load's dst vector posts at LAT 10; valign's align-register
  result posts one stage earlier (LAT 9).

The layering is clean: **aligned LS/SS = a 64-byte line access tolerant of natural
element alignment; VALIGN = the front-end that assembles a fully byte-misaligned vector
from two such aligned loads + the funnel + the align register.**
The **indexed** counterpart is orthogonal: this page is the non-indexed form (one base
AR ± a scalar offset/index → one contiguous 64-byte line); [SuperGather](cas-supergather.md)
is the indexed form (16 per-lane 32-bit offsets in a vector register, scattering to
per-lane addresses). The `LSR*` stream-rotate forms are the rotating-buffer streaming
variant of the aligned load. `[HIGH/OBSERVED roster; MED for the rotate-buffer model — inferred from name + slot]`

---

## 8. The validation hook (VAL, Part 15)

Because the LSU is a clean stimulus/response oracle — feed it `(base, offset, line
bytes)`, read back `(loaded vector, updated AR)` — it is the ideal target for the
differential **VAL** harness that Part 15 will define: drive the same `lsnx16.{i,x,ip,xp}`
encoding through the fiss pipeline and a candidate reimplementation, and assert
byte-equality of the extracted element, the alignment-mask line address, and (for
`_ip`/`_xp`) the post-update AR. The funnel masks of §3.1 and the LAT table of §6.1
are the two highest-value invariants to pin first.

---

## 9. Honesty / uncertainty ledger

**HIGH / OBSERVED** (disassembled):

* The 35-root `LV/SV/LS/SS/LSR` roster + the LSU slot map (`S0_LdSt`/`S1_Ld`); the
  scalar `L32I/L16/L8/S32/S16/S8` base-ISA ld/st.
* The four addressing modes `_i/_x/_ip/_xp` byte-exact from `LSNX16_*_issue`
  (`0x71ddc0`/`de20`/`deb0`/`df40`): base AR (`& 0xf`, LAT 1) + dst vec (bits[8:4],
  LAT 10); `_x` index AR (bits[11:8]); `_ip`/`_xp` the doubled-`and $0xf` second AR def.
* LV-vs-LS identical cas decode.
* The 64-byte (`$0x40`) line; the per-width funnel `wideldshift_W_512_6`
  `{16,32,64,128,256}`; the per-width mask `{c1,c3,c7,cf,df}` (all six verified).
* The fiss pipeline stateload/regload/stage5/memload/stage14/writeback;
  `addr = base + offset(0x90/0x94)`; the `wideldshift` extract; the `widestshift_512_16_6`
  store funnel; store @ stage 11; the `_ip` `add increment` + dual (vec + AR) write-back.
* The scalar `L32I` `test $0x3` fault vs the vector funnel tolerance.
* cas timing: AR LAT 1, vec LAT 10; the stall chain (`nx_Load_0` + CPENABLE + REV8AR +
  WB_P/N/S + InOCDMode + MS_DISPST). The 119 `nx_*_interface` callbacks and 864
  `module__xdref_*` value functions (both counts verified exact).
* The host ports + fault ports + the 10-member `LoadStore*Exception` set.

**MED / INFERRED:**

* The per-region (HBM/dataram-TCM/IRAM) binding of the fault ports — the names are
  OBSERVED, the region→fault binding is INFERRED, the per-region latency is host-resolved.
* Device endianness (LE) — the word-ordered staging is OBSERVED, the silicon LE inferred.
* The narrow-load sign/zero extension locus (host fetch vs opcode body) — the U/S split
  is HIGH, the exact locus MED.
* The `LSR*` rotate-buffer semantics; the `LE/LT/LTU` compare-load → gather `vbMask`
  binding.

**LOW:**

* The full per-mnemonic flag-bit map decoded by the shared `ld_st_semantic_stage1`
  dispatcher — structurally a select-bit ladder, not enumerated field-by-field.

> **CORRECTION.** An early cross-reference put the count of LSU instruction-stage
> functions at "30,789". That figure is a *filtered* LSU subset and is **not directly
> reproducible** from the binary as stated — the total `_inst_stage[0-9]+` symbol count
> in `libcas-core.so` is **157,775** (all units, every format × slot × stage), and the
> LSU portion is a fraction of that obtained by an additional mnemonic filter. This
> page therefore anchors on the two counts that *are* exactly reproducible — **119**
> `nx_*_interface` callbacks and **864** `module__xdref_*` value functions — and quotes
> the 35-root LSU roster rather than a single derived stage-function total.
