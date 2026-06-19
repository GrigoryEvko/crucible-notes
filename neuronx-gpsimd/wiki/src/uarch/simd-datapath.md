# The SIMD Compute-Datapath

> **Scope.** This page is the **compute-datapath microarchitecture** of the Vision-Q7 *Cairo*
> (`ncore2gp`, `XCHAL_VISION_TYPE=7`) core — the *structure behind the ISA*. The [ISA reference
> batches](../isa/ref/b04-mac-integer.md) tell you what each opcode computes; the [pipeline-timing
> model](pipeline-timing.md) tells you when each operand is read and written; the
> [register-file port model](regfile-ports.md) tells you how many ports each file sustains. This page
> ties those into the **functional clusters** that hang off the deep vector pipe: the 32-lane vector
> ALU (8/16/32-bit lane partitioning), the integer **quad-MAC array** feeding the 1536-bit `wvec`
> accumulator, the SP/HP **2×FMA VFPU**, the **SuperGather** address-generation engine, and the
> `valign`/permute **lane crossbar** — with the compute-datapath block diagram, the per-unit
> lane/width/throughput table, and the datapath↔regfile connectivity map.

## Epistemic guard — read this first

**The RTL is not in the corpus.** There is no netlist, no Verilog, no gate-level schematic for the
Vision-Q7 compute datapath in any shipped artifact. Every *datapath-structure* claim on this page
(how many physical multipliers, carry-save vs ripple, crossbar topology, partial-product layout) is
either read out of a **named hardware-block model function** in the unstripped ISS or **inferred** by
composing the observable layers. Five layers are independently OBSERVED:

| layer | what it gives | where |
|---|---|---|
| **L1 — ISA roster + operand ctypes/widths** | lane tokens, accumulator widths, the 4-tap operand shape | `libisa-core.so` `Opcode_*` thunks; `xt_ivp32.h` intrinsic CTYPEs |
| **L2 — per-opcode pipeline-stage model** | read port @10, result @11/12/13, the `wvec` (12,12) RMW | `libcas-core.so` `*_issue` operand stamps ([pipeline-timing](pipeline-timing.md)) |
| **L3 — FLIX co-issue / slot-class binding** | which cluster rides which slot, the class-exclusivity bounds | `libisa-core.so` `Slot_<fmt>_s<n>_<class>` getters ([regfile-ports §3](regfile-ports.md)) |
| **L4 — regfile read/write port-stage model** | the unified @10 read, staggered writes, the gather `gt`/`gs` ports | `libcas-core.so` `my_<rf>_<slot>_opnd_*` accessors ([regfile-ports](regfile-ports.md)) |
| **L5 — the executable value model** | the i8→24 / i16→48 accumulate, the 4-tap dot-product, single-rounded FMA | `libfiss-base.so` `module__xdref_*` leaves, **driven live** this pass |

> **The big upgrade over a prior backing analysis: the multiplier datapath is NOT merely inferred —
> it is NAMED.** A prior report (DX-HW-08) tagged "no native multiplier; partial-product / carry-save
> trees" as `[INFERRED]` because the only value bodies it had were the host-`imul` *simulation* in the
> x86 ISS. This pass disassembles `libcas-core.so` and finds the **device hardware-block models
> themselves** as named functions — Booth encoders, partial-product generators, 3:2/4:2/5:2 carry-save
> compressors, the multiply-slice pipeline — and confirms they contain **zero** host `imul`/`mul`
> instructions (they build the product bit-by-bit). So "Booth + partial-product + carry-save Wallace
> tree, no native full-width multiplier" is `[HIGH/OBSERVED]` *structure* here, not inference; only the
> exact tree wiring (which compressor feeds which) remains `[INFERRED]`.

Tags per claim: `[HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED]`. `OBSERVED` = a symbol / byte / stage /
**executed** value read from a shipped binary or shipped public header this pass; `INFERRED` =
reasoned over OBSERVED; `CARRIED` = re-used at a cited sibling page's confidence. All prose reads as
derived from shipped-binary + shipped-public-header + device-disassembler static analysis (lawful
interoperability reverse engineering, DMCA 17 U.S.C. 1201(f)). Binary paths under
`extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/` (gitignored — reach with `fd --no-ignore` or an
absolute path). `.text`/`.rodata` are VMA==file-offset; `.data`/`.data.rel.ro` carry the
per-binary delta **`0x200000`** (confirm per-section with `readelf -SW` before any `xxd`/`objdump` on
a `.data`-resident struct).

---

## 0. Headline — the compute datapath in one paragraph

`[HIGH/OBSERVED widths+stages; INFERRED top-level wiring]` The compute heart is a **512-bit-wide,
32-physical-lane SIMD datapath** (`XCHAL_VISION_SIMD16 = 32`, `core-isa.h` L207 — 32 lanes × 16 bit =
512 bit), run as a **deep pipe** hanging off the shared 7-stage scalar front-end: every architectural
vector source is read at **stage 10** and results land at **11 / 12 / 13** by latency class
([pipeline-timing §2.2](pipeline-timing.md)). The 512 bits are statically **partitioned** into
`64×8b` / `32×16b` / `16×32b` element lanes by the opcode's lane token (`2NX8` / `NX16` / `N_2X32`).
**Five functional clusters** sit on this one datapath, each bound to FLIX slot classes:

1. **VEC-ALU** (1-cyc, result @11) — the 32-lane integer/logic/shift/compare ALU, on the
   `S0`/`S2`/`S3`/`S4` ALU-capable slots.
2. **QUAD-MAC** (2-cyc, result @12) — the integer multiply / 4-tap quad-MAC array, **exclusively** the
   `S2_Mul` slot; widens products into the **1536-bit `wvec`** accumulator (i8→24 / i16→48 / i32→96
   per lane). **No native full-width multiplier** — products are Booth-encoded partial products
   reduced by a carry-save (Wallace) compressor tree (§3, named modules).
3. **VFPU** (3-cyc, result @13) — the SP (fp32, 16-lane) + HP (fp16, 32-lane) **2×FMA** unit, riding
   the `S2`/`S3` ALU/Mul slots; a fully-multiplexed FMA tree doing MUL/ADD/SUB/MADD/MSUB +
   Newton-step divide/recip, controlled by the `gvr` FSR/FCR CSR.
4. **SuperGather** (mem slots, @1+@10) — the indexed-address-generation engine in the `S0`/`S1`
   load/store slots; computes per-lane byte addresses `base + offset·elem_sz` into the `gvr`
   staging file, two-phase A(address)/D(drain).
5. **PERMUTE crossbar** (2-cyc, result @12) — the `2N→N` / `N→N` lane-select/shuffle/deal/compress
   network (`SEL`/`SHFL`/`DSEL`/`DCMPRS`).

All five read their vector sources through **one unified stage-10 vec read port** and write back
through a **staggered** write port (10/11/12/13 by latency class). The integer MAC accumulator
(`wvec`) is a **separate same-stage `(12,12)` read-modify-write** file enabling an `II=1` MAC chain.

`[HIGH/OBSERVED — device round-trip this pass]` The slot binding is confirmed on silicon-targeted
encodings via the device assembler/disassembler (`xtensa-elf-as`/`-objdump`, GNU binutils
2.34.20200201 / "Xtensa Tools 14.09", `XTENSA_CORE=ncore2gp`):

```
{ IVP_MULNX16 wv0, v1, v2 ; IVP_ADDNX16 v3, v1, v2 }
   -> 16-byte F-bundle  000919034046034090004ab08504452f
      ivp_mulnx16 writes the wvec accumulator (S2_Mul); ivp_addnx16 co-issues in S3_ALU.

IVP_MULUUQAN16XR16 wv0, v1, v2, v3, v4, pr0
   -> 16-byte F-bundle  026c8331440a0046505c09908d04455f
      the 4-tap quad-MAC reads wvec acc (inout) + FOUR vec taps + a b32_pr radix in ONE issue.
```

---

## 1. The compute-datapath block diagram

`[HIGH/OBSERVED widths+stages; INFERRED box-to-box wiring]` Vertical axis = pipeline stage; horizontal
= the five clusters. Stages 0–3 are the shared scalar front-end (fetch/decode/issue + AGU base read +
the `CPENABLE` coprocessor-enable gate @3); 8–9 are early vector flops (per-lane mask / gather-offset /
divide-early read); **stage 10** is *the* vec read port; 11/12/13 are the latency-class result writes;
14/15 are the FSR sticky-flag accumulate and the deferred imprecise-exception post (`impreciseExceptions=1`),
not data.

```
  SCALAR FRONT-END (shared, 7-stage: A1 / B3 / E4 / M5 / W6)
  +-----------------------------------------------------------------------------------+
  | s0 fetch | s1 AGU base (ars@1 -> VAddrBase) | s2/3 vision decode/issue + CPENABLE  |
  |          |                                  | coprocessor-enable gate (USE @3)     |
  +----------+---------------------------------------------------------------+--------+
             |  FLIX bundle (up to 5 slots: S0  S1  S2_Mul  S3_ALU  S4_ALU)
             v
  =========================  THE 512-bit DEEP VECTOR PIPE  ==========================
             |
   s8-9   +--+------------------+  (per-lane byte-mask / gather-offset / divide-early read)
          |                     |
   s10  +=+=====================+================================================+
   THE  |        UNIFIED 512-bit vec READ PORT  (all vec _use land @10)         |
   vec  |  feeds ALL clusters; vbool@10, valign@10(divide), GSVAddrOffset@10    |
   read +=+========+============+==============+================+===============+
        |          |            |              |                |
   (S0/S3/S4)   (S2_Mul)     (S2|S3)        (S0/S1 mem)     (S3 + pinned S0/S2/S4)
        |          |            |              |                |
   +----+----+ +---+--------+ +-+----------+ +-+------------+ +-+--------------+
   | VEC-ALU | | QUAD-MAC   | |   VFPU      | | SuperGather | |  PERMUTE        |
   | 32-lane | | array      | | SP/HP 2xFMA | | addr-gen    | |  crossbar       |
   | 8/16/32 | | Booth + PP | | mulpp25x25  | | engine      | | SEL/SHFL/DSEL/  |
   | add sub | | + CSA      | | (fp32 sig)  | | base+off*sz | | DCMPRS          |
   | logic   | | (3:2/4:2/  | | mulpp12x12  | | -> gvr(gsr) | | 2N->N / N->N    |
   | shift   | |  5:2 comp) | | (fp16 sig)  | | +GSEnable   | | lane mux        |
   | cmp     | | -> wvec    | | AddMul      | | +GSVAddrOff | | + DCMPRS left-  |
   |         | | acc RMW    | | 23x23x11    | | +ScatterDat | |   pack          |
   +----+----+ +-----+------+ +-----+-------+ +------+------+ +-------+---------+
        | @11       | @12         | @13          | @10/11(D)        | @12
   s11  v 1-cyc     |             |              | (load drain)     |
   s12  ----------> v 2-cyc <- wvec acc (12,12) RMW, II=1 chain      v 2-cyc
   s13  -----------------------> v 3-cyc
        |           |            |              |                   |
        +-----------+------------+--------------+-------------------+
                                 |  STAGGERED 512-bit vec WRITE PORT
                                 v  { @10 load | @11 ALU | @12 MAC/perm/divstep | @13 FMA/cvt }
   s14   FSR sticky-flag accumulate (Invalid/DivZero/Overflow/Underflow/Inexact @14)
   s15   deferred / imprecise vector fp exception post (status, not data)

  SIDE FILES feeding the clusters (geometry walked from the regfile descriptor table, §7):
    wvec   1536b x4  -- read+write @12 (RMW)  -- QUAD-MAC accumulator (3x512b headroom)
    vbool    64b x16 -- read @10, write @10/11 -- per-lane predicate (.T merge, compare sink, DCMPRS)
    gvr     512b x8  -- SuperGather staging "gsr" (gt def / gs use)  AND  VFPU FSR/FCR CSR (RUR/WUR)
    b32_pr   64b x16 -- the quad-MAC RADIX scalar (xb_int64pr; byte-split taps) + vec-rep packed pred
    valign  512b x4  -- unaligned-L/S byte-rotate AGU scratch + iterative-divide step scratch
    AR       32b x64 -- scalar base/index (ars@1 -> VAddrBase) + vision-scalar results (SQZN @12)
```

`[HIGH/OBSERVED]` the stage numbers and the named hardware-block modules are read from the ISS;
`[INFERRED][HIGH]` the box-to-box wiring is the unique topology consistent with: a single @10 read
port feeding all clusters, the per-class result stages, the `wvec` `(12,12)` self-RMW, and the
live-driven value bodies.

---

## 2. The 32-lane vector ALU — 8/16/32-bit lane partitioning

### 2.1 Physical lane grid

`[HIGH/OBSERVED]` `XCHAL_VISION_SIMD16 = 32` (`core-isa.h` L207): the datapath is **32 lanes of
16-bit = 512 bits**. The lane width is **not a mode register** — it is the opcode's lane *token*,
enumerated across the ISA roster (`Opcode_ivp_*<token>*` mnemonics in `libisa-core.so`):

| token | partition | distinct mnemonics carrying it | what it is |
|---|---|---:|---|
| `2NX8` | **64 × 8-bit** | 164 | the 512-bit reg viewed as 64 byte-lanes |
| `NX16` | **32 × 16-bit** | 169 | the canonical / native lane = the 32 physical lanes |
| `N_2X32` | **16 × 32-bit** | 101 | 32-bit elements (two 16-lanes welded per element) |
| `N_2XF32` | 16 × fp32 | 100 | the SP VFPU lane (§4) |
| `NXF16` | 32 × fp16 | 100 | the HP VFPU lane (§4) |

(Counts: `nm libisa-core.so | rg -o 'Opcode_ivp_[a-z_0-9]*<tok>[a-z_0-9]*_args' | sort -u | wc -l`.)
`[HIGH/OBSERVED]`

`[INFERRED][HIGH — supported by L1 lane tokens + L5 per-width value bodies]` The 8/32-bit modes are
**carved from the same `32×16b` physical array**: 8-bit splits each 16-bit lane into two byte
sub-lanes (carry-chain break at the byte boundary); 32-bit fuses adjacent 16-bit lane pairs
(carry-chain joined). This is the standard **segmented-adder** discipline — one 512-bit adder with
programmable carry-kill at the 8/16/32 boundaries, the lane token selecting the carry-break mask. The
value model corroborates per-width bodies: `libfiss-base.so` ships distinct `module__xdref_add_*` /
`*_min_*` / `*_max_*` leaves in `_8` / `_16` / `_32` width variants, each with its own saturation
constants (the saturation is a *decode-time width+sat variant*, not a global mode).

### 2.2 Ops, latency, slot binding

`[HIGH/OBSERVED]` The `ivp_sem_vec_alu` family (plus `vec_mov`, `vec_shift`, `vbool_alu`) is
**1-cycle**: inputs read @10, result written @11 ([pipeline-timing §3.1](pipeline-timing.md), exemplar
`IVP_MINN_2XF32T` @11, `IVP_ANDB`/`IVP_LTNX16` vbool @11). The ALU is the **`S0`/`S2`/`S3`/`S4`**
ALU-capable slots — `my_vec_{0,2,3,4}_opnd_ivp_sem_vec_alu_*` all resolve in `nm libcas-core.so`
([regfile-ports §3](regfile-ports.md)). Peak vec-ALU co-issue is therefore **2–3** ALU lanes (`F3`:
`S3`+`S4`; `F11`: `S1_ALU`+`S3`+`S4`). Device confirm: the `IVP_ADDNX16 v3,v1,v2` half of the bundle
above lands in `S3_ALU`.

### 2.3 Throughput

`[HIGH/OBSERVED latency; INFERRED II=1]` Fully pipelined, `II=1` — the ISS `MODULE_SCHEDULE` reservation
bodies ship empty and the `_stall` callbacks model only RAW/WAW scoreboard hazards, never a
functional-unit reservation ([pipeline-timing §6](pipeline-timing.md), the "empty-reservation wall").
A 1-cyc vec-ALU chain runs at full IPC because the result @11 forwards to the next bundle's read @10
(dep-latency = `11 − 10 = 1`).

---

## 3. The quad-MAC array + 1536-bit `wvec` accumulator

This is the deep-learning inner-loop critical path, and it is the strongest-evidenced cluster on the
page: the **hardware-block model functions are named in the binary** and the **value semantics drive
live**.

### 3.1 No native multiplier — Booth + partial-product + carry-save Wallace tree `[HIGH/OBSERVED]`

There is **no single full-width device multiplier**. `libcas-core.so` (unstripped, 179 079 symbols)
models the multiply datapath as a chain of named hardware-block functions, each a bit-level model
that contains **zero** x86 `imul`/`mul` instructions (verified by disassembly — they are pure
`shr`/`and`/`xor`/`or`/`neg` bit-models):

| stage of the multiply datapath | named module(s) (`libcas-core.so`, `nm`-verbatim) | width(s) |
|---|---|---|
| **Booth radix-4 encode** | `module_ivp_booth_enc_8_stage0` `@0x6e5150`, `module_ivp_booth_enc_16_stage0` `@0x6e4770`; TIE `tie_function_tie_func_booth{12,21,25,32}` | 8, 16; 12/21/25/32 |
| **partial-product generation** | `module_ivp_su_xtmulpp_8x8_stage0` `@0x6e51f0`, `module_ivp_sign_unsign_xtmulpp_16_8_stage0` `@0x6e4810`; TIE `mulpp{12x12,21x18,25x25,32x32}` | 8×8, 16×8; 12..32 |
| **carry-save compressors (Wallace tree)** | 11× `module_ivp_comp_{3,4,5}_2_arr_{18,19,24,26,34,35,40}_stage0`; `module_ivp_sem_csa_l0_slice_stage0` `@0xa8d140`; TIE `ivp_sem_csa_8_16_32_l{0,1,2}_f` | 3:2/4:2/5:2 cells at 18..40 bit |
| **multiply slice pipeline** | `module_ivp_sem_mul_slice_stage{0,1,2,3}` (4-deep), `mul_mux_even/odd_slice`, `mul_2_1_mux` | per-lane |
| **mult-add / reduce** | `module_ivp_mult_add_16_16_48_stage0` `@0x6e4c80`, `module_ivp_sem_reduce_stage1_stage0` `@0xa8d670` | 16×16→48 |

> **GOTCHA — the x86 ISS `xdref` value leaf uses one host `imul`; the device modules do not.** The
> *value-checking* leaf `module__xdref_mul_24_8_8` in `libfiss-base.so` is literally
> `movsbl; movsbl; imul; and $0xffffff; ret` — a numeric **simulation** of the product, fast and
> exact. The *structural* model in `libcas-core.so` (`mul_slice`/`booth_enc`/`csa`/`comp_*`) is the
> bit-level device model and has **no** `imul`. Do not read the `xdref` `imul` as evidence of a
> hardware array multiplier — it is the simulator's shortcut for getting the right bits. The 11
> `comp_X_2_arr_N` compressor cells (3:2, 4:2, 5:2 at widths 18/19/24/26/34/35/40) are the definitive
> carry-save Wallace-tree signature. `[HIGH/OBSERVED]`

`[INFERRED][HIGH — constrained by the named modules above]` The *device* is therefore a Booth-encoded
partial-product PE array per lane feeding a carry-save (3:2/4:2/5:2) compressor tree into the
accumulator lane — the only structure consistent with the named module set and a width-parameterised
PP accumulate. The exact which-compressor-feeds-which wiring is the only remaining inference.

### 3.2 The `wvec` accumulator lane layout (i8→24 / i16→48 / i32→96) `[HIGH/OBSERVED]`

The accumulator widths are read straight out of the `xdref` symbol names and the firmware CTYPEs, and
confirmed by the regfile descriptor table (`wvec` = **1536 bit × 4**, §7):

| source | product | acc lane | acc words | firmware CTYPE | `xdref` width-signature | headroom |
|---|---|---|---:|---|---|---|
| i8·i8 | 16b raw | **24-bit** | 1 word | `xb_vec2Nx24` | `module__xdref_mul_24_8_8` | +8b cross-MAC |
| i16·i16 | 32b raw | **48-bit** | 2 words | `xb_vecNx48` | `module__xdref_mul_48_16_16` | +16b cross-MAC |
| i32·i32 | 64b raw | **96-bit** | 3 words | `xb_vecN_2x64w` | `module__xdref_*_96_*_512` | +32b cross-MAC |

The **1536-bit `wvec` width = 3 × the 512-bit `vec` width** = exactly the 3-word/i32-lane (96-bit)
maximum span, and the same 1536 bits re-partitioned by source width:

```
16 lanes × 96b  =  32 lanes × 48b  =  64 lanes × 24b  =  1536 bit   (arithmetic check: all == 1536)
```

So `wvec` is the reduce/MAC summation **headroom** over the raw product, re-partitioned by source
width — never a wider physical file. `[HIGH/OBSERVED — regfile table walk + arithmetic + CTYPEs]`

### 3.3 The three accumulate modes, driven live `[HIGH/OBSERVED]`

The accumulate direction is a **decode-selected datapath path**, not a runtime mode: OVERWRITE
(`mul`/`muln`/`mulp`/`mulq`: `wvt = product`, no prior-acc read), RMW-ADD (`mula`: `wvt += product`),
RMW-SUB (`muls`: `wvt -= product`). Driven live in-process via `ctypes` this pass against the
`libfiss-base.so` leaves (these integer leaves take no context pointer, so they call cleanly):

```
module__xdref_mul_24_8_8(-1,-1)    = 0x000001   ; movsbl both, imul, AND 0xffffff
module__xdref_mul_24_8_8(127,127)  = 0x003f01
module__xdref_mul_24_8_8(-128,-128)= 0x004000
module__xdref_mul_24_8_8(100,-50)  = 0xffec78   ; sign-extended, two's-complement in 24 bit

; i8 MAC accumulate chain (mula_24_24_8_8), mod-2^24 wrap, NO saturate:
  acc=0      += 100*100 =  10000 -> 0x002710  (OK)
  acc=10000  += 120*120 =  14400 -> 0x005f50  (OK)
  acc=24400  += -50*80  =  -4000 -> 0x004fb0  (OK)
  acc=20400  += 127*-128=-16256 -> 0x001030  (OK)   ; 4/4 bit-exact
```

The accumulate **wraps mod 2^(lanewidth)** — there is no saturation on the accumulate (matching
[B04 §1](../isa/ref/b04-mac-integer.md)). Saturating/rounding narrowing is a *separate* opcode
(the `packvr` family); plain `PACKL` is a truncating low-half extract. `[HIGH/OBSERVED — executed live]`

### 3.4 The 4-tap quad-MAC (`QUAD_MAC_TYPE = 1`) `[HIGH/OBSERVED]`

`XCHAL_VISION_QUAD_MAC_TYPE = 1` (`core-isa.h` L209). The quad-MAC reads, in **one issue**: the
running `wvec` accumulator (inout) + **FOUR** vec operands (the 4 taps) + a `b32_pr` **radix** scalar.
The firmware intrinsics (`xt_ivp32.h`) name the operand shape verbatim:

```c
/* xt_ivp32.h L1212-1214 — the 4-tap n16 forms (XR16/XR8 = radix-16/8): */
void   IVP_MUL4TAN16XR16(xb_vecNx48 a /*inout*/, xb_vecNx16 b, xb_vecNx16 c, xb_int64pr d);
xb_vecNx48 IVP_MUL4TN16XR16 (xb_vecNx16 b, xb_vecNx16 c, xb_int64pr d);
/* xt_ivp32.h L1478 — the unsigned-unsigned 4-vec-tap quad accumulate: */
void   IVP_MULUUQAN16XR16(xb_vecNx48 a /*inout*/,
                          xb_vecNx16U b, xb_vecNx16U c, xb_vecNx16U d, xb_vecNx16U e,
                          xb_int64pr f);  /* a += quad-dot(b,c,d,e ; radix f) */
```

The radix (`b32_pr`, 64-bit `xb_int64pr`) is **split into bytes**; each tap product
(`tap_i · radix_byte_i`) is summed into ONE accumulator lane. The per-lane value leaf
`module__xdref_mul4t2n8xr8_24_8_8_8_8_64` `@0x817060` disassembles to **four `mul_24_8_8` calls then a
4-term add**, and drives live to the exact 4-tap dot-product:

```
; module__xdref_mul4t2n8xr8_24_8_8_8_8_64 disassembly (the per-lane 4-tap):
  mov (%r9),%edx; shr $0x18        ; radix byte 3
  call module__xdref_mul_24_8_8    ; tapA * radix_b3
  movzbl 0x2(%rbp); call ...       ; tapB * radix_b2
  movzbl %dh;       call ...       ; tapC * radix_b1
  movzbl 0x0(%rbp); call ...       ; tapD * radix_b0
  mov 0xc(%rsp),%eax; add 0x8; add 0x4; add (%rsp)   ; SUM the 4 partial products
  and $0xffffff; mov %eax,(%rdx)   ; -> one 24-bit acc lane

; live drive: taps=[10,20,30,40], radix bytes b0..b3=[3,5,7,11]
;   result = 0x000208 = 10*11 + 20*7 + 30*5 + 40*3 = 110+140+150+120 = 520 = 0x208  (OK)
```

So **`wvt[lane] = (Σ_{i=0..3} tap_i · radix_byte_i) mod 2^acc_w`** — a fused 4-tap micro-dot-product
per issue, the deep-learning inner-loop MAC. The `mul4t*` symbol names carry the widths literally:
`mul4t2n8xr8_1536_512_512_64` = `1536`(wvec) / `512`(vec) / `512`(vec) / `64`(b32_pr radix).
Device round-trip (above): `IVP_MULUUQAN16XR16 wv0,v1,v2,v3,v4,pr0` → one 16-byte F-bundle, `S2_Mul`.
`[HIGH/OBSERVED — disasm + live + intrinsic + device]`

`[INFERRED][HIGH]` the HW is a 4-lane PP-PE bank per output lane (the 4 taps) feeding a carry-save
reduction into the `wvec` lane — the only topology consistent with the 4-vec+radix read fan-in, the
byte-split radix, and the single-issue 2-cyc result.

```c
// ---------------------------------------------------------------------------
// QUAD-MAC partial-product accumulation — annotated reconstruction.
// Symbols named are real: module_ivp_booth_enc_16, module_ivp_su_xtmulpp_8x8,
// module_ivp_comp_4_2_arr_24, module_ivp_sem_csa_l0_slice, module_ivp_sem_reduce_stage1,
// module__xdref_mul4t2n8xr8_24_8_8_8_8_64 (the value oracle), all in libcas-core/libfiss-base.
// Per-lane datapath for one i8 4-tap quad-MAC into a 24-bit wvec lane.
// ---------------------------------------------------------------------------

typedef struct { uint8_t s[8]; uint8_t c[8]; } csa_pair;   // sum/carry vectors, carry-save form

// One lane: acc += dot4(tap[0..3], radix_byte[0..3])  (the IVP_MUL*Q*XR8 inner op)
static uint32_t quad_mac_lane_i8(uint32_t acc_in,           // wvec lane, 24-bit (read @12)
                                 int8_t tap0, int8_t tap1,  // four vec taps (read @10)
                                 int8_t tap1b, int8_t tap1c,
                                 uint64_t radix)            // b32_pr, byte-split (read @10)
{
    // 1) split the 64-bit radix into 4 byte multipliers (shr $0x18 / movzbl in the disasm).
    uint8_t r3 = (radix >> 24) & 0xff, r2 = (radix >> 8) & 0xff;   // pairing per disasm
    uint8_t r1 = (radix >> 8) & 0xff,  r0 = radix & 0xff;          // (illustrative byte map)

    // 2) per tap: Booth-encode the radix byte, generate partial products, NO host multiply.
    //    Each pp_i is a set of Booth-selected, shifted partial products (module_ivp_su_xtmulpp_8x8).
    pp_t pp0 = booth_pp_i8(tap0, r3);     // module_ivp_booth_enc_8 -> module_ivp_su_xtmulpp_8x8
    pp_t pp1 = booth_pp_i8(tap1, r2);
    pp_t pp2 = booth_pp_i8(tap1b, r1);
    pp_t pp3 = booth_pp_i8(tap1c, r0);

    // 3) carry-save reduce the FOUR taps' partial products into ONE redundant sum.
    //    A 4:2 compressor cell (module_ivp_comp_4_2_arr_24) folds 4 PP rows -> 2 (sum,carry).
    csa_pair red = comp_4_2_arr_24(pp0, pp1, pp2, pp3);   // 4 PP -> {sum,carry}, no ripple yet

    // 4) the wvec accumulator self-RMW (stage 12): add prior acc as a 5th term, then resolve.
    //    csa_l0 folds {sum,carry,acc} -> {sum',carry'} (3:2), reduce resolves the final ripple.
    red = csa_l0_slice(red.s, red.c, acc_in);             // module_ivp_sem_csa_l0_slice (3:2)
    uint32_t lane = reduce_resolve(red);                  // module_ivp_sem_reduce_stage1

    return lane & 0xffffff;                                // 24-bit wvec lane wrap (mod 2^24)
}
// Value-equivalent oracle (no host imul on the DEVICE path, but the ISS xdref uses one):
//   module__xdref_mul4t2n8xr8_24_8_8_8_8_64 == sum_i tap_i*radix_byte_i, AND 0xffffff (driven live).
```

### 3.5 The `wvec` 2-cycle recurrence (the MAC chain) `[HIGH/OBSERVED]`

The accumulator (`wvt`/`wvu`) is **both a USE @12 and a DEF @12** — a same-stage read-modify-write —
for every accumulating op ([pipeline-timing §3.1 GOTCHA](pipeline-timing.md);
[regfile-ports §5](regfile-ports.md)). `write@12 → next-MAC read@12` is the same stage, so the
accumulator forwards in one cycle: the MAC chain **issues every cycle (`II=1`)** with a **2-cycle
result latency** (taps@10 → acc@12). Because only **4** `wvec` entries exist, at most **4 independent
MAC accumulation chains** are live at once — a software register-pressure bound, not a HW port stall.

> **CORRECTION — the `wvec` RMW is *two* operands (`wvt` + `wvu`), not one.** A prior backing analysis
> modeled the accumulator as a single `wvt`. The 1536-bit accumulator is addressed as a low half
> `wvt` and a high half `wvu`, and `nm libcas-core.so` resolves **all four**
> `my_wvec_2_opnd_ivp_sem_multiply_{wvt,wvu}_{use,def}` — both halves are read **and** written at
> stage 12. A model that tracks only `wvt` undercounts the accumulator's read/write ports by half.
> This page adopts the binary-witnessed two-operand RMW (matching [regfile-ports §5](regfile-ports.md)).
> `[HIGH/OBSERVED]`

### 3.6 Slot binding `[HIGH/OBSERVED]`

The integer multiply / quad-MAC class is carried **exclusively** by `S2_Mul`: `nm` resolves only
`my_vec_2_opnd_ivp_sem_multiply_{vp,vq,vr,vs,vt}_use` — **no** `my_vec_{0,1,3,4}_opnd_ivp_sem_multiply_*`.
So at most **one** integer quad-MAC issues per bundle, and it demands **five** `vec` read ports
(`vp`/`vq`/`vr`/`vs` inputs + `vt` a *narrow* `vec`-side accumulator RMW) at @10 **plus** the wide
`wvt`/`wvu` `wvec` RMW at @12 ([regfile-ports §2 CORRECTION](regfile-ports.md)). The `b32_pr` radix
is `my_b32_pr_2_opnd_ivp_sem_multiply_arr_use` — the packed round/select operand on the same slot.

---

## 4. The SP/HP 2×FMA VFPU

### 4.1 Config `[HIGH/OBSERVED]`

From `core-isa.h` (L211-215): `XCHAL_HAVE_VISION_SP_VFPU = 1`, `SP_VFPU_2XFMAC = 1`,
`HP_VFPU = 1`, `HP_VFPU_2XFMAC = 1`, `DP_VFPU = 0`. So: **single-precision (fp32, `N_2XF32` = 16
lanes)** and **half-precision (fp16, `NXF16` = 32 lanes)** vector FMA, both with the **`2×FMAC`**
double-throughput option; **no vector double precision**.

### 4.2 One multiplexed FMA tree, named in the binary `[HIGH/OBSERVED]`

`libcas-core.so` models the VFPU FMA as a named, multiplexed tree per precision, with the **significand
partial-product cells named explicitly**:

| FMA datapath block | named function(s) (`libcas-core.so`/`libfiss-base.so`) |
|---|---|
| **fp32 significand FMA core** | `tie_function_AddMul_23x23x11` (24-bit significand `25×25`, +11b add), fed by `tie_function_AddMul_enc` (Booth) + `tie_function_AddMul_booth`; PP cell `tie_function_tie_func_mulpp25x25` |
| **fp32 FMA slice pipeline** | `module_pdx_sem_spfma_slice_stage{0,1,2,3}`, `module_fp_spfma_round_stage0`, `spfma_concat_64` / `take_even` / `take_odd` |
| **fp16 significand FMA core** | `tie_function_sem_fp_hp_fma_stage{0,1,2,3}` (+ `_simd_stage0..3`, `_fp_hp_round`); PP cell `tie_function_tie_func_mulpp12x12` (11-bit significand `12×12`) |
| **recip / rsqrt seed (Newton)** | `module_recip_rsqrt_qli_s{0,1,2}` quadratic-interpolation seeds + `CONST_TBL_fp_{recip,rsqrt}_qli_lut{1,2}_{A,gx}` coefficient LUTs |
| **fp divide / quo-rem** | `module_ivp_sem_divide_f_quo_rem_32_stage{0,1}`, `quo_rem_{1,2}_32`, `pack_f` |
| **fp value leaves (live)** | `module__xdref_madd_1_1_1_1_32f_32f_32f_32f_2`, `..._16f_..._2`, `maddn_{32f,16f}` |

So the significand multiply is a **partial-product grid** (`mulpp25x25` for fp32's 24-bit significand,
`mulpp12x12` for fp16's 11-bit significand) — the same Booth/PP discipline as the integer array, not a
native multiplier. The product is kept **exact** into the aligned add (`AddMul_23x23x11`), so the FMA
is **single-rounded** (round once on `a·b + c`, not `round(round(a·b) + c)`) — proven by live execution
on the sibling [B17 §4](../isa/ref/b17-spfma.md) page (8/8 divergent single-vs-double-rounding cases
matched single-round). `[HIGH/OBSERVED structure + CARRIED single-round certificate]`

```c
// ---------------------------------------------------------------------------
// VFPU 2xFMA — fp32 single-rounded fused multiply-add reconstruction.
// Symbols named are real: tie_function_AddMul_enc / _booth / _23x23x11,
// tie_function_tie_func_mulpp25x25 (significand PP), module_pdx_sem_spfma_slice_stage0..3,
// module_fp_spfma_round_stage0 (the round-once stage), module__xdref_madd_*_32f (value oracle).
// One fp32 lane; the op-class is selected by the opcode CONST (MUL/ADD/SUB/MADD/MSUB/DIVN).
// ---------------------------------------------------------------------------

enum fma_op { OP_MUL, OP_ADD, OP_SUB, OP_MADD, OP_MSUB, OP_MADDN, OP_DIVN };

static uint32_t vfpu_fma_lane_fp32(enum fma_op op,
                                   uint32_t a, uint32_t b, uint32_t c,  // fp32 bit patterns
                                   unsigned RM /* FCR RoundMode, from gvr CSR */)
{
    fp32_t A = unpack(a), B = unpack(b), C = unpack(c);

    // 1) sign path: MSUB/MSUBN negate the PRODUCT leg (the negate_axb / negadd toggle).
    bool neg_prod = (op == OP_MSUB || op == OP_MADD /*c-a*b polarity*/ ? false : false);

    // 2) significand multiply: a 25x25 partial-product grid of the 24-bit significands,
    //    Booth-encoded (AddMul_enc/_booth -> mulpp25x25). Kept EXACT (sig[26:0]), NOT rounded here.
    wide_sig product = mulpp25x25(A.sig, B.sig);          // tie_function_tie_func_mulpp25x25
    int      pexp    = A.exp + B.exp - BIAS;

    // 3) align the addend to the product exponent and fused-add (single wide accumulator).
    wide_sig aligned_c = align_to(C.sig, C.exp, pexp);    // module_pdx_sem_spfma_slice_stage1
    wide_sig fused;
    switch (op) {
        case OP_MUL:  fused = product;                          break;  // c absent
        case OP_ADD:  fused = aligned_c; product = 0;           break;  // a*b absent (add_only)
        case OP_MADD: fused = add_exact(product, aligned_c);    break;  // round once on a*b + c
        case OP_MSUB: fused = sub_exact(aligned_c, product);    break;  // c - a*b (negate_axb)
        case OP_DIVN: fused = newton_step(A, B, C);             break;  // recip-seed refine chain
        default:      fused = product;                          break;
    }

    // 4) normalize + ROUND ONCE under RM (the single rounding of the whole a*b+-c).
    return pack_round(fused, pexp, RM);                   // module_fp_spfma_round_stage0
}
// Value oracle: module__xdref_madd_1_1_1_1_32f_32f_32f_32f_2 (a fp32 single-rounded FMA leaf;
// it dereferences an ISS state-context pointer, so it is driven via the ISS, not a bare ctypes call).
```

### 4.3 Latency / throughput / slot `[HIGH/OBSERVED lat+slot; INFERRED 2x micro-binding]`

3-cycle (inputs @10, result @13; `IVP_MULAN_2XF32T` / `IVP_MULANXF16T` @13). The `2×FMAC` config bit
**doubles per-cycle FMA throughput** while latency stays 3 cyc; the exact dual-issue micro-binding is
not separately encoded in the schedule (one op = one result/op), so "2 FMA/cycle/lane" is
`[INFERRED]` from the config bit. (The roster has 100 fp32 + 100 fp16 FMA-class `Opcode_*_args`, each
50 base + 50 predicated `…t`.)

> **NOTE — there is no `MADD`/`MSUB` opcode spelling; the FMA roots are `mulan`/`mulsn`.** The
> Tensilica VFPU expresses the fused multiply-add via `muln` (mul), `addn`/`subn` (add/sub),
> **`mulan`** (`a·b + c`, the MADD root) and **`mulann`** (normalized maddn), **`mulsn`** (the MSUB
> root) / **`mulsnn`** (msubn), and `mulsonen` (`a·(1−b)`), each in fp32 (`_2xf32`) and fp16 (`xf16`)
> with a predicated twin (`nm libisa-core.so | rg '_args$'`: `Opcode_ivp_mulan_2xf32_args`,
> `Opcode_ivp_mulsn_2xf32_args`, `Opcode_ivp_mulanxf16_args`, …). The `MADD.S`/`MADD.H` *assembly
> aliases* the device assembler accepts are spellings of these roots, not distinct opcodes. The
> `OP_MADD`/`OP_MSUB` enum names in the §4.2 pseudocode are the *op-class* the selector `CONST`
> lights, not mnemonics. `[HIGH/OBSERVED]` The FP-FMA / FP-divide-step class rides **`S2`-Mul OR `S3`-ALU** —
see the CORRECTION — and the VFPU control/status (RoundMode + the 5 IEEE flags/enables) lives in the
`gvr` **FSR/FCR** CSR, accessed via `RUR.FSR`/`WUR.FSR`/`RUR.FCR`/`WUR.FCR` (no data-register operand;
the flag DEFs land @14). Device confirm: `MADD.S v4,v5,v6` co-issues with a MAC.

> **CORRECTION — FP-FMA is NOT slot-3-exclusive; it rides `S2_Mul` OR `S3_ALU`.** A prior backing
> analysis (DX-HW-08 §4.5) bound the VFPU to the `S3_ALU` lane, "distinct from the integer-MAC
> `S2_Mul`." The binary disproves this: `nm libcas-core.so` resolves **both**
> `my_vec_2_opnd_ivp_sem_spfma_{vr,vs,vt}_use` *and* `my_vec_3_opnd_ivp_sem_spfma_*` (likewise
> `fp_sem_hp_fma` on `vec_2` and `vec_3`). So an FMA may ride the multiply lane (`s2`) *or* an ALU
> lane (`s3`). It remains true that there is **one** `s2_mul` lane, so an FMA placed on `s2` **excludes
> a same-bundle integer quad-MAC** — the two contend for that single lane. The co-issue win is still
> real: an FMA on `s3` co-issues with an integer MAC on `s2`. This page adopts the binary-witnessed
> `s2|s3` binding (matching [regfile-ports §3 CORRECTION](regfile-ports.md) and
> [pipeline-timing §6](pipeline-timing.md)). `[HIGH/OBSERVED]`

---

## 5. The SuperGather address-generation engine

### 5.1 Config + engine role `[HIGH/OBSERVED]`

`XCHAL_HAVE_SUPERGATHER = 1` (`core-isa.h` L100). SuperGather is a **memory-subsystem engine** wired
to the load/store slots (`S0` for address-issue + scatter; `S1` for drain), **not** an ALU — every
member is an `ivp_sem_vec_scatter_gather` op. Its defining property is the **per-lane indirect
address `addr[k] = base + offset[k]·elem_sz`** computed inside the core and handed, with a per-lane
validity predicate, to a memory port the rest of the pipeline cannot see ([B19](../isa/ref/b19-scatter-gather.md)).
The roster is **24 mnemonics** (6 gather-address `GATHERA*`, 4 gather-drain `GATHERD*`, 1 `MOVGATHERD`,
10 `SCATTER*`, 2 `SCATTERINC*`, 1 `SCATTERW`); `nm libisa-core.so | rg ...gather|scatter..._args`
returns 23 `_args`-tabled mnemonics (`SCATTERW` is the no-data drain barrier). `[HIGH/OBSERVED]`

### 5.2 The `gvr` ("gsr") staging file `[HIGH/OBSERVED]`

`gvr` (512b × 8, regfile descriptor idx 7, ctype `xb_gsr`) is the SuperGather staging register file.
It is the **only** file flagged **`0x0d`** in the descriptor table (every other file is `0x05`) — the
structural marker of a file written by one instruction (the A-phase) and read by a *different*
instruction (the D-phase) across a latency window, never directly named as an ALU operand
([B19 §1.1 QUIRK](../isa/ref/b19-scatter-gather.md)). It holds the in-flight per-lane addresses and
the collected element bytes (8 entries `gr0..gr7`).

> **CORRECTION — `gvr` carries scatter/gather data operands; it is not a "RUR-only / no-operand"
> file.** A prior backing analysis (DX-HW-08 §7) modeled `gvr` as carrying *zero* schedule operands
> (accessed only via `RUR`/`WUR`). The binary disproves this: `nm libcas-core.so` resolves
> `my_gvr_0_opnd_ivp_sem_vec_scatter_gather_gt_def` (the gather **target** index vector, written by
> the A-phase on slot 0) and `my_gvr_1_opnd_ivp_sem_vec_scatter_gather_gs_use` (the gather **source**
> index vector, read by the D-phase on slot 1). The `RUR/WUR` FSR/FCR CSR role is *additional*, not
> the whole story — `gvr` is genuinely dual-role (gather-staging data path **and** VFPU CSR), but the
> data-path operands are real. This page adopts the binary-witnessed `gt`/`gs` operands (matching
> [regfile-ports §2 CORRECTION](regfile-ports.md)). `[HIGH/OBSERVED]`

### 5.3 The two-phase gather `[HIGH/OBSERVED]`

`IVP_GATHERNX16(base, off) = IVP_GATHERDNX16( IVP_GATHERANX16(base, off) )` ([B19](../isa/ref/b19-scatter-gather.md)):

- **A-phase** (`GATHERA*`, → `gvr`, `S0`): per lane `k` posts `addr_k = base + offset_k·elem_sz`
  (`elem_sz`: `NX16`=2, `NX8U`=1, `N_2X32`=4) + validity `k` (`GSEnable`, AND `vbool` for the `T`
  form) into the `gvr`; base/control resolve a cycle early (@1 in the scalar AGU), the lane payload
  (offset vector, enable mask) @10 (the canonical vec read port).
- **D-phase** (`GATHERD*`, `gvr` → `vec`, `S1`): muxes the collected data out of `gvr` into a `vec`
  register, width/sign-mux only (`NX16` raw / `NX8S` sign-ext / `2NX8_L`/`_H` halves); result @11
  (one extra drain cycle).
- **SCATTER** (`vec` → mem, `S0`): symmetric one-phase indexed store, `ScatterData` @10 + per-lane
  `GSEnable` kill. `SCATTERINC` is an in-memory RMW `+1` (the histogram primitive,
  `XCHAL_HAVE_VISION_HISTOGRAM = 1`). `SCATTERW` is a standalone drain/wait barrier.

### 5.4 Address-gen datapath `[INFERRED][HIGH]`

The A-phase is a 32/64-lane parallel AGU: a per-lane `(base + offset·elem_sz)` scaled-index adder
bank feeding the `gvr`, with `elem_sz` the scale and `GSEnable` the per-lane valid. This is the only
structure consistent with the @1 base + @10 offset-vector + per-lane address-descriptor write to
`gvr`. The memory-subsystem occupancy under DataRAM bank conflicts is **not** in the ISS schedule
(conflict-free assumed) — the only structurally-modeled memory port is the `loadStoreUnitsCount = 2`
back-pressure (`nx_Load_0/1_interface`, `nx_Store_0_interface`,
[pipeline-timing §6](pipeline-timing.md)); the real gather throughput under bank contention is a
deferred memory-arbitration slice. `[INFERRED + scope note]`

---

## 6. The `valign` / permute lane crossbar

### 6.1 The permute network `[HIGH/OBSERVED ops+timing; MED exact LUT]`

The cross-lane select/shuffle/deal/compress family (`SEL`/`SHFL`/`DSEL`/`DCMPRS` + the slot-pinned
`SELi`/`SHFLi` variants) is a **multiplexed lane crossbar**: result lane `k` is sourced from an
arbitrary input lane chosen by a control (vector `sr`, `vbool`, or immediate). `nm libisa-core.so`
gives **28** distinct `_args`-tabled mnemonics in this family. 2-cycle (inputs @10, result @12 — one
stage deeper than vec-ALU's 1-cyc, reflecting the crossbar depth;
[pipeline-timing §3.1](pipeline-timing.md)):

| op | semantics | network |
|---|---|---|
| `SEL` | `vt[k] = (vr++vs)[ sr[k] ]` | `2N→N` full crossbar (both source vectors) |
| `SHFL` | `vt[k] = vr[ sr[k] ]` | `N→N` 1-src intra-register permute |
| `DSEL` | `{vt,vu} = deal(vr,vs,sr)` | dual-output butterfly (de-interleave / zip) |
| `DCMPRS` | `vt = compress(vr, vbr)` | predicate-driven left-pack (stream compact) |
| `.T` | per-lane `vbool`-predicated merge | kill → keep dst |
| `.I` | immediate-pattern control (`saimm7`/`imm4`/`imm2`/coded `selimm`) | LUT-driven pattern |

`DSEL`/`DCMPRS` are witnessed on `S3_ALU` (`Opcode_ivp_dcmprs2nx8_Slot_f0_s3_alu_encode`,
`Opcode_ivp_dsel2nx8i_Slot_{f0,f4,f6,n0}_s3_alu_encode`). The slot-pinned `SELi`/`SHFLi` `_S0/_S2/_S4`
variants exist so the bundler can place a byte-permute in the `LdSt`/`Mul`/`ALU2` slot when `S3` is
busy (same compute, alternate host slot). `[HIGH/OBSERVED operand sigs]`

`[INFERRED][HIGH]` the HW is a full `2N`-input lane mux (a banked crossbar) + a population-count /
prefix-sum pack network for `DCMPRS` — the only topology covering a `2N→N` arbitrary lane select + a
contiguous left-pack. The exact immediate→pattern LUT and the `DCMPRS` tail-fill are the network's
internal tables (`[MED]`, suppressed semantic body).

### 6.2 `valign` (the unaligned-L/S rotate-merge crossbar) `[HIGH/OBSERVED]`

`valign` (512b × 4, ctype `u`) serves **two** uses ([regfile-ports §2](regfile-ports.md)):

1. **Unaligned vector load/store rotate-merge** — the alignment register holds the boundary bytes so
   a `vec` L/S that straddles a 512-bit boundary is reassembled by a **byte-rotate** crossbar in the
   `S0`/`S1` mem-slot AGU (an AGU use, not an architectural `vu` operand in the schedule).
2. **Iterative-divide scratch** — the only architectural `valign` operands in the schedule are the
   `DIVN_*_4STEP*` step ops (**14** distinct 4-step mnemonics, `nm` count); read @10, write @12. A
   full SIMD divide is a chain of these 2-cyc @12 steps (e.g. `_4STEP0`…`_4STEP3` → ~8-cyc dependent
   latency, fully pipelined).

`[INFERRED][MED]` the byte-rotate alignment crossbar (byte-granular rotate at the mem port) and the
lane-select permute crossbar (lane-granular arbitrary select at the ALU) are **distinct** networks.

---

## 7. Datapath ↔ regfile connectivity map

`[HIGH/OBSERVED]` The 8 register files (geometry walked directly from the `libisa-core.so` descriptor
table this pass — file offset `0x54a800`, 56-byte stride, 8 entries) and which clusters they feed,
with the port-STAGE each connection operates at ([regfile-ports §2/§6](regfile-ports.md)):

| idx | file | geom | reads (USE) | writes (DEF) | feeds / fed-by |
|---:|---|---|---|---|---|
| 0 | `AR` | 32b×64 | @1/@3/@4/@5 | @1/@3/@4/@5/@6/@12 | scalar base/index (`ars@1` → `VAddrBase`, AGU + SuperGather) + vision-scalar results (`IVP_SQZN`/`UNSQZN` @12, `RUR.FSR/FCR`). Windowed 8×8. |
| 1 | `BR` | 1b×16 | @3/@4 | @4 | scalar boolean E-stage; **not** on the vector compute path (no `my_BR_*` vision accessor). |
| 2 | **`vec`** | **512b×32** | **@10 (ALL clusters, one unified port)** | **@10/11/12/13 (staggered by class)** | the hot compute file: VEC-ALU `a`/`b`, QUAD-MAC `vp`/`vq`/`vr`/`vs`+`vt` (5 reads), VFPU `vr`/`vs`/`vt`, SuperGather offset/drain/scatter-data, PERMUTE src/dst. |
| 3 | `vbool` | 64b×16 | @10 | @10/11 | per-lane predicate: `.T` merge (all clusters), `DCMPRS`, compare-result sink, gather `GSEnable` AND. |
| 4 | `valign` | 512b×4 | @10 (divide) | @12 (divide) | unaligned-L/S byte-rotate AGU scratch (mem port) + iterative-divide step scratch. |
| 5 | **`wvec`** | **1536b×4** | **@12 (RMW)** | **@12 (RMW)** | **QUAD-MAC accumulator ONLY** (i8→24 / i16→48 / i32→96). Same-stage `(12,12)` RMW (`wvt`+`wvu`), `II=1` chain, file-bound to 4 live chains. |
| 6 | `b32_pr` | 64b×16 | @10 | @10/11 | the quad-MAC **radix** (`my_b32_pr_2_opnd_ivp_sem_multiply_arr_use`; `xb_int64pr`, byte-split = taps) + `vec_rep` packed predicate (`prr`/`prt`). |
| 7 | **`gvr`** | **512b×8** (flags `0x0d`) | @10/11 (gather `gs`) + `RUR` | @10/11 (gather `gt`) + `WUR` | **dual role**: SuperGather staging "gsr" (`gt` def / `gs` use, the data path) **and** VFPU `FSR`/`FCR` CSR (RoundMode + 5 IEEE flags/enables, the `RUR/WUR` path). |

### Key connectivity facts `[HIGH/OBSERVED]`

- **One unified stage-10 `vec` read port feeds all five clusters.** A peak `F3`/`F11` 5-slot bundle
  demands **~10–11 simultaneous @10 `vec` reads** (MAC 5 + ALU 4 + ALU 2 + mem 1, the per-slot read
  sum `{3,1,5,4,2}`; [regfile-ports §3](regfile-ports.md)). The physical port array is sized to this
  peak. The MAC's wide `wvec` accumulator is a **separate @12 file** — it does **not** add to the @10
  read count.
- **`vec` writes are STAGGERED across 10/11/12/13 by latency class** — the explicit anti-port-pressure
  mechanism that keeps the same-stage write count (~2–3) far below the read count (~10). The worst
  same-stage write collision is `F11`'s three ALU lanes (`s1`+`s3`+`s4`) all retiring a 1-cyc result
  @11 → 3 write ports @11.
- **The `wvec` accumulator is the ONLY same-stage `(12,12)` RMW file**; read/written exclusively by the
  QUAD-MAC (`S2_Mul`), never as a general `vec` source.
- **The forwarding network bridges every compute write stage back to the @10 read** (dep-latency =
  `wstage − 10` exactly: ALU 1, MAC/perm/div-step 2, FMA/cvt 3) + the `wvec` @12→@12 self-forward.
  There is **no** fast path from a vector write stage to the early scalar `AR` read — cross-pipe
  vec→scalar transfers eat the full pipe-depth gap ([regfile-ports §6/§7](regfile-ports.md)).

---

## 8. Per-unit lane / width / throughput table

`[HIGH/OBSERVED unless noted]`

| unit | slot (class) | lanes (8b / 16b / 32b) | result stage | dep-lat | issue/bundle | accum / width | src files | dest file |
|---|---|---|:---:|:---:|---|---|---|---|
| **VEC-ALU** | `S0`/`S2`/`S3`/`S4` ALU | 64 / 32 / 16 | @11 | 1 | 2 (F3) · 3 (F11) | — | `vec`, `vbool` | `vec` (+`vbool`) |
| **QUAD-MAC** (4-tap; Booth+PP+CSA, no native MUL) | **`S2_Mul`** only | 64×8 / 32×16 / 16×32 taps | @12 | 2 | **1** | `wvec` 24/48/96 acc (1536b total) | `vec`×5 (vp/vq/vr/vs+vt), `b32_pr` radix | `wvec` (RMW) + `vec` (narrow `vt`) |
| **VFPU SP** (2×FMA) | `S2`\|`S3` | — / — / 16×fp32 | @13 | 3 | 1 (2/cyc via 2×) | fp32, RM round | `vec`, `gvr` (FSR/FCR) | `vec` |
| **VFPU HP** (2×FMA) | `S2`\|`S3` | — / 32×fp16 / — | @13 | 3 | 1 (2/cyc via 2×) | fp16, RM round | `vec`, `gvr` | `vec` |
| **SuperGather** (addr-gen) | `S0`/`S1` mem | 64 / 32 / 16 per-lane addrs | @10 (A) · @11 (D) | 0–1 | ≤2 mem (S0+S1) | per-lane addr (no acc) | `vec` (off+data), `AR`, `gvr` | `gvr` / `vec` / mem |
| **PERMUTE** (crossbar) | `S3` (+pinned `S0`/`S2`/`S4`) | 64 / 32 / 16 lane-select | @12 | 2 | 1–2 | lane-granular | `vec`, `vbool` | `vec` (+`vu`) |
| **`valign`** (align / div-step) | `S0`/`S1` mem AGU | byte-rotate (mem) + 32×16 div lanes | @12 (div) | 2 | — | 512b (mem + divide) | `vec`, `valign` | `vec` |

**Notes (all `[HIGH/OBSERVED]` unless tagged):**

- **`DEP-LAT`** = producer-write-stage − stage-10-read = back-to-back dependent-issue latency; all
  units fully pipelined (`II=1`) per the empty `MODULE_SCHEDULE` reservation bodies
  ([pipeline-timing §6](pipeline-timing.md)). The MAC chain accumulates at `II=1` via the `wvec`
  `(12,12)` self-RMW.
- **QUAD-MAC "1/bundle"** is hard (only `S2_Mul` hosts integer multiply). An **FMA on `S3`** co-issues
  with a MAC on `S2`; an **FMA on `S2`** *excludes* a same-bundle MAC (they contend for the one
  `S2_Mul` lane). `[HIGH/OBSERVED — §4.3 CORRECTION]`
- **"2/cyc via 2×"** = the `SP/HP_VFPU_2XFMAC` config bit doubles per-cycle FMA throughput
  (`[HIGH config OBSERVED; the dual-issue micro-binding INFERRED, §4.3]`).
- **PEAK COMPUTE/cycle** (a well-scheduled `F3`/`F11` bundle): 1 quad-MAC (4 i16 taps → `wvec`, `S2`) +
  1 FP-FMA (16 fp32 or 32 fp16, ×2 via 2×FMAC, `S3`) + 2–3 vec-ALU lanes + 2 mem/gather lanes.
  `[HIGH counts; INFERRED peak composition]`

---

## 9. Self-verify — the five strongest datapath claims

| # | claim | OBSERVED / INFERRED | verdict + witness |
|---:|---|---|---|
| 1 | **No native multiplier; products from Booth + partial-product + carry-save Wallace tree** | **OBSERVED** (structure named) | UPGRADED from a prior `[INFERRED]`. `module_ivp_booth_enc_{8,16}`, `module_ivp_su_xtmulpp_8x8`, 11× `module_ivp_comp_{3,4,5}_2_arr_*`, `module_ivp_sem_csa_l0_slice`, `mulpp{12x12,25x25,32x32}` — disassembled, **zero** host `imul` in the device modules. Exact tree wiring stays INFERRED. |
| 2 | **`wvec` i8→24 / i16→48 / i32→96, 1536b = same bits re-partitioned** | **OBSERVED** | Regfile descriptor table walk (`wvec` 1536b×4); `xdref` widths `mul_24_8_8`/`mul_48_16_16`; arithmetic `16·96 = 32·48 = 64·24 = 1536`; live `mula_24_24_8_8` (mod-2^24, 4/4 exact). |
| 3 | **4-tap quad-MAC: `wvt[lane] = Σ tap_i·radix_byte_i`** | **OBSERVED** | Disassembled `mul4t2n8xr8_24_8_8_8_8_64` (4×`mul_24_8_8` + sum); live (520 = 0x208); intrinsic `IVP_MULUUQAN16XR16(xb_vecNx48 a/*inout*/,…U b,c,d,e, xb_int64pr f)`; device round-trip `026c…`. |
| 4 | **vec read @10, MAC `(12,12)` RMW, FMA @13, `II=1` MAC chain** | **OBSERVED** | [pipeline-timing histograms](pipeline-timing.md) (`vec` @10 read; `wvec` @12 RMW); [regfile-ports §5](regfile-ports.md). |
| 5 | **FP-FMA rides `S2_Mul` OR `S3_ALU` (not `S3`-exclusive)** | **OBSERVED** | `my_vec_2_opnd_ivp_sem_spfma_*` AND `my_vec_3_opnd_ivp_sem_spfma_*` both resolve; same for `fp_sem_hp_fma`. CORRECTS a prior `S3`-exclusive claim (§4.3). |

**Divergences reconciled (in-place CORRECTIONs above):** (a) FP-FMA slot `s2|s3`, not `S3`-exclusive
(§4.3); (b) `gvr` carries `gt`/`gs` scatter-gather operands, not "RUR-only / no-operand" (§5.2); (c) the
`wvec` RMW is two operands `wvt`+`wvu`, not one (§3.5). All three adopt the binary-witnessed facts of the
committed [regfile-ports](regfile-ports.md) / [pipeline-timing](pipeline-timing.md) pages — the numbers on
this page are identical to those siblings. The integer-MAC count frame follows the corrected
[B04](../isa/ref/b04-mac-integer.md)/[B05](../isa/ref/b05-mac-mixed.md) partition (integer `ivp_mul*` =
**188** = 65 signed + 123 mixed; **24** FP-typed MAC forms reclaimed to [B17](../isa/ref/b17-spfma.md)/
[B18](../isa/ref/b18-hp-fma.md)), not the older 71/141/212 framing.

---

## 10. Cross-references

- [Register-File Port Model + Bypass Network](regfile-ports.md) — the per-file read/write port counts,
  the `vec` 5-read MAC, the `wvec` `(12,12)` RMW, the `s2|s3` FMA binding, and the forwarding matrix
  this page's connectivity map (§7) ports.
- [Microarchitecture Synthesis](microarch-synthesis.md) — the consolidating capstone that places this
  datapath (the Booth/PP/CSA multiply tree, the 1536-bit `wvec`, the VFPU FMA, SuperGather, permute)
  in the one cycle-approximate model, and where the `188 = 65 + 123` integer-MAC / `24` FP-reclaimed
  frame and the `s2|s3` FMA binding are unified (synthesis §2.2/§3.3, §8 rows 4, 5).
- [Pipeline Timing Model](pipeline-timing.md) — the 16-stage pipeline, the @10 read port, the
  per-class result stages (@11/12/13), and the empty-reservation wall (`II=1`) behind §2.3/§3.5/§8.
- [FLIX Co-Issue Matrix](co-issue-matrix.md) — the format × slot-class legality behind the slot
  bindings (§2.2/§3.6/§4.3).
- [The Eight Register Files](../isa/core/register-files.md) — the geometry/role/ctype of each file
  in §7's table.
- [B04 Integer MAC](../isa/ref/b04-mac-integer.md) / [B05 Mixed-Sign MAC](../isa/ref/b05-mac-mixed.md) —
  the per-opcode quad-MAC roster the §3 array executes.
- [B17 fp32 FMA](../isa/ref/b17-spfma.md) / [B18 fp16 FMA](../isa/ref/b18-hp-fma.md) — the per-opcode
  VFPU roster and the single-rounding certificate behind §4.
- [B19 SuperGather Scatter/Gather](../isa/ref/b19-scatter-gather.md) — the 24-mnemonic engine roster,
  the two-phase A/D model, and the `gvr` staging-file geometry behind §5.
- [VFPU / IEEE Numerics](vfpu-ieee.md) — the `gvr`/FSR/FCR CSR and the IEEE soft-float core behind §4.

---

*Provenance: `core-isa.h` `XCHAL_VISION_*` config bits (`SIMD16=32`, `QUAD_MAC_TYPE=1`,
`SP/HP_VFPU_2XFMAC=1`, `SUPERGATHER=1`); the `libcas-core.so` named hardware-block models
(`module_ivp_booth_enc_*`, `module_ivp_su_xtmulpp_*`, `module_ivp_comp_{3,4,5}_2_arr_*`,
`module_ivp_sem_csa_*`, `module_pdx_sem_spfma_slice_*`, `tie_function_AddMul_23x23x11`) and operand
accessors (`my_<rf>_<slot>_opnd_*`); the `libisa-core.so` opcode roster + regfile descriptor table
(walked this pass: 8 files, 56-byte stride, `wvec` 1536b×4 / `vec` 512b×32 / `gvr` 512b×8 flags
`0x0d`); the `libfiss-base.so` `module__xdref_*` value leaves **driven live** in-process via `ctypes`
(`mul_24_8_8`, `mula_24_24_8_8`, `mul4t2n8xr8_24_8_8_8_8_64` — bit-exact); the `xt_ivp32.h` intrinsic
CTYPEs (`xb_vec2Nx24`/`xb_vecNx48`/`xb_vecN_2x64w`/`xb_int64pr`/`xb_gsr`); and the device-native
`xtensa-elf-as`/`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) round-trip oracle. THE RTL IS NOT IN THE
CORPUS — all block-to-block wiring is INFERRED from OBSERVED ports / stages / widths / values / named
hardware-block modules, tagged per claim. Lawful interoperability reverse engineering (DMCA 17 U.S.C.
1201(f)); no vendor source tree referenced — every fact reads as derived from shipped-binary +
shipped-public-header + device-disassembler analysis alone.*
