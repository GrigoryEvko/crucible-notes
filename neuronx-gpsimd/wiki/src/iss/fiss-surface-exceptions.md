# libfiss-base Surface + Exception Model

> **Part 14 value-side foundation.** This page is the other half of the gateway.
> [libcas-core Surface + ISS Plugin ABI](./cas-core-surface.md) established that
> the *cycle-accurate* core `libcas-core.so` is a **timing oracle** that computes
> decode and hazards but **no element values** — it delegates every datapath
> result to the host through **119 `nx_*_interface` TIE-port callbacks**. The
> open question that finding raised was: *where does the value math live?* The
> answer is here. **`libfiss-base.so` IS the value oracle** that answers those
> callbacks: a functionally complete, self-contained Xtensa+IVP interpreter that
> computes per-lane element results in-process. This page maps its export surface
> and its Xtensa exception model; the per-lane datapath library is enumerated in
> [The fiss Datapath Oracle](./fiss-datapath-oracle.md).

All facts below are derived from static analysis of the shipped binary only:

```
extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/libfiss-base.so
ELF64 LSB shared object, x86-64, SYSV, dynamically linked, NOT stripped
12,330,016 bytes (12.3 MB) — the largest artifact in the toolchain
```

Tooling: `nm -D/-S`, `readelf -h/-SW/-d`, `objdump -d`, `xxd`, and a live
`ctypes` drive. Each claim is tagged **confidence** × **provenance**
(`OBSERVED` = read directly off the binary, `INFERRED` = strong deduction from
observed evidence, `CARRIED` = taken from a prior report and re-checked here).
The one recovered header cited (`tools/XtensaTools/include/iss/libfiss.h`) is
treated as a binary-adjacent artifact: it ships in the same toolchain and its
constants are corroborated against the binary at every use.

---

## 1. Executive verdict — fiss is the value oracle

> The single fact this page proves. State it precisely, then certify it live.

**Claim.** `libfiss-base.so` computes the real per-element value of every
modeled instruction *in-process*, with its own scalar arithmetic primitives. It
is the complement to `libcas-core.so`: where cas forwards every datapath op to
the host, fiss owns the math. The two are bolted together by the ncore2gp
harness through the fixed plugin ABI of `libfiss.h` (`LIBFISS_VERSION 2`) — cas
for cycles, fiss for values.

| | `libcas-core.so` (cycle-accurate) | `libfiss-base.so` (functional / this page) |
|---|---|---|
| Size | 45.9 MB | **12.3 MB** |
| Computes | decode + timing + register hazards | decode + **architectural element values** |
| Element arithmetic | **none** — zero datapath ops | real add/sub/and/min/max/mul + soft-float |
| Host coupling | **119** `nx_*_interface` TIE-port callbacks | **0** host-math callbacks |
| Undefined imports | 120 (119 ports + `memset`) | **5**, all libc/runtime weak symbols |
| Value boundary | decode-bitmap → host handoff | `slotfill→regload→opcode→writeback`, all internal |

### 1.1 Self-containment — the 5-import proof

The decisive structural fact: fiss imports **nothing that could compute a
value for it**. Its entire undefined-symbol set is five weak libc/runtime hooks:

```
$ nm -D --undefined-only libfiss-base.so
                 w __cxa_finalize@GLIBC_2.2.5
                 w __gmon_start__
                 w _ITM_deregisterTMCloneTable
                 w _ITM_registerTMCloneTable
                 w _Jv_RegisterClasses
```

All five are **weak** (`w`) — the standard GCC `crtstuff` epilogue/ITM/gcj
stubs present in every C++ shared object. **There is no `nx_*_interface`, no
`memset`, no libm, no host datapath import.** Every value fiss returns is
computed by code inside this 12.3 MB image. *(HIGH · OBSERVED — `nm -D` total
20,384 dynsym; 20,379 defined; 20,384 − 20,379 = 5 undefined, listed above.)*

> **NOTE — why this matters for reimplementation.** The cas page's takeaway was
> that you need *two* models and that the 119-port `nx_*_interface` vtable is the
> seam your host must fill. **This page tells you what fills it.** A drop-in host
> backend for the cas TIE ports is exactly the value math enumerated below: for
> each port, the `opcode__<m>__stage_N` callback and the `module__xdref_*`
> primitive it drives. fiss *is* a complete reference implementation of that
> backend. *(HIGH · INFERRED from the two binaries' complementary import sets.)*

### 1.2 Live certification — drive a value function through ctypes

The arithmetic is not inferred from names — it is **executed**. The
`module__xdref_*` primitives are `extern "C"`, take their operands in the SysV
integer registers, and write the result through an out-pointer. Disassembly of
the wrapping int16 add fixes the ABI exactly:

```text
module__xdref_add_16_16_16 @0x858480 :  (rdi=ctx[unused], esi=A, edx=B, rcx=out)
  add  %esi,%edx              ; A + B
  and  $0xffff,%edx           ; truncate to 16 bits (wrap mod 2^16)
  mov  %edx,(%rcx)            ; *out = result
  ret
```

Loading the binary and calling it through `ctypes` with the signature
`void f(void* ctx, int32 A, int32 B, int32* out)` certifies the oracle. The
saturating sibling `module__xdref_adds_16_16_16 @0x85aa10` is the decisive case:
`0x7000 + 0x7000 = 0xE000` would *wrap* to a negative int16, but `adds` clamps
to `0x7fff` — only real saturation logic produces that:

| Call | Result | Expected | |
|---|---|---|---|
| `add_16_16_16(0x3, 0x4)` | `0x7` | `0x7` | wrapping |
| `add_16_16_16(0xffff, 0x1)` | `0x0` | `0x0` | wrap mod 2^16 |
| `add_8_8_8(0xff, 0x1)` | `0x0` | `0x0` | wrap mod 2^8 |
| `adds_16_16_16(0x7fff, 0x1)` | `0x7fff` | `0x7fff` | **saturate (no wrap)** |
| `adds_16_16_16(0x7000, 0x7000)` | `0x7fff` | `0x7fff` | **saturate, not 0xE000** |
| `minu_16_16_16(0xffff, 0x1)` | `0x1` | `0x1` | unsigned min |
| `sub_16_16_16(0xa, 0x3)` | `0x7` | `0x7` | wrapping subtract |

**15/15 pass** across add/sub/wrapping-int8/saturating-int16/unsigned-min.
*(HIGH · OBSERVED — driven live; the saturating clamp distinguishes executed
arithmetic from name-grammar inference.)*

> **GOTCHA — the result is an out-pointer, not the return value.** The
> `module__xdref_*` ABI returns `void` and writes the lane result to `*rcx` (the
> 4th integer argument). Calling these with `restype = c_int32` and reading the
> return register gives garbage. Pass a `byref(c_int32())` out slot. For
> 3-operand widths the slot indices shift (e.g. `muls_24_24_8_8` takes two
> source operands plus the out-pointer in a later slot); read the prologue before
> wiring the call. *(HIGH · OBSERVED.)*

### 1.3 No SIMD anywhere — reference-model shape

The verdict is reinforced by what is **absent**: the entire 12.3 MB image
contains **zero packed-integer SIMD** (`paddw`/`psubw`/`pmullw`/`pmaxsw`/
`vpadd*` count == 0 across `objdump -d`). Vector ops are realized as **scalar
per-lane loops** over the `module__xdref_*` primitives — correctness over
throughput, the classic reference-model style. The IVP `addnx16t` op makes this
literal: `opcode__ivp_addnx16t__stage_5 @0x256d50` calls
`module__xdref_add_16_16_16` **27 times** (once per active 16-bit lane of the
512-bit NX16 vector) interleaved with predicate-mask `module__xdref_bitkillt_16_2`
calls. *(HIGH · OBSERVED — SIMD mnemonic count 0; the per-lane call multiplicity
read off the unrolled loop body.)*

> **COROLLARY for the per-format ISS pages.** The saturation bounds,
> signed/unsigned distinctions, and wrap/round semantics that the cas-side pages
> could only *infer from the IVP naming grammar* are **executed bit-for-bit
> here**. `libfiss-base` is the ground-truth oracle for IVP element semantics; a
> per-format page should read the `module__xdref_*` body to confirm or correct
> any "MED (from naming)" semantic claim. *(HIGH · INFERRED.)*

---

## 2. The functional-ISS surface map (20,379 exports)

```text
$ nm -D --defined-only libfiss-base.so | rg -c .
20379
```

ELF facts: entry `0x190430`; `.text` VMA `0x190430` == file offset; `.rodata`
VMA `0x88ff00` == file offset; **`.data.rel.ro` VMA `0xc17e80` → file `0xa17e80`,
delta `0x200000`** (§6). Of the 20,379 defined exports, **20,368 are `T`
(functions)** and **11 are data** (2 `B` + 4 `D` + 5 `R`). Only 5 undefined
imports (§1.1). *(HIGH · OBSERVED.)*

### 2.1 Export prefix-family census

Each family is the value-side counterpart of one column of the `libfiss.h`
semantic-stage contract. Counts are exact `nm -D` regex tallies, **not** decompile
grep — re-ground each as `nm -D --defined-only … \| rg -c '^<prefix>'`:

| Family | Count | Role | Conf · Prov |
|---|---|---|---|
| `slotfill__` | **12,569** | FLIX/scalar instruction-word DECODE → operand-slot fill (§3) | HIGH · OBSERVED |
| `opcode__` | 1,708 | per-mnemonic EXECUTE callback (the compute stage) | HIGH · OBSERVED |
| `stateload__` | 1,534 | load architectural state into the per-op context | HIGH · OBSERVED |
| `regload__` | 1,534 | gather source-register VALUES into operand slots | HIGH · OBSERVED |
| `writeback__` | 1,534 | commit result slot → register file | HIGH · OBSERVED |
| `module__` (all `xdref_*`) | **864** | per-lane VALUE-MATH primitives — the datapath (§5) | HIGH · OBSERVED |
| `opcode_complete__` | 214 | retire/commit half of the execute callback | HIGH · OBSERVED |
| `memload__` | 110 | load-path (LdSt slot value ingress) | HIGH · OBSERVED |
| `memstore__` | 106 | store-path (LdSt slot value egress) | HIGH · OBSERVED |
| `memstore_check__` | 106 | store-path access/permission predicate | HIGH · OBSERVED |
| `exception__` | 61 | Xtensa fault model (§4) | HIGH · OBSERVED |
| `rsrbus__` | 15 | special-register bus read helpers | HIGH · OBSERVED |
| `function__` | 10 | top-level instruction-issue entry points | HIGH · OBSERVED |
| misc singletons | 13 | `_init`/`_fini`/`_end`/`_edata`/`__bss_start`/`cver__id`/`exception_fns`/`stage_functions`/`semantic_functions`/`opcode_num_exceptions`/`opcode_exceptions_idxs`/`opcode_exceptions_operandarg_idxs`/`libfiss_config_metadata`/`complexity_loc` | HIGH · OBSERVED |

The named families plus the 13 misc singletons sum to **exactly 20,379**.
*(HIGH · OBSERVED.)*

> **CORRECTION — the three counts are a superset chain, not a contradiction.**
> Three figures circulate; they are nested, not competing:
> - **864** = the `module__xdref_*` *value leaves* (every `module__` export is an
>   `xdref_*` — the per-lane datapath; the "864-leaf oracle" of
>   [The fiss Datapath Oracle](./fiss-datapath-oracle.md)).
> - **12,569** = the `slotfill__` *decoders* (the bitfield-extract kernels).
> - **20,379** = the **total** dynamic exports (the full superset of all
>   families above).
>
> So `864 ⊂ 20,379` and `12,569 ⊂ 20,379`, and `864 + 12,569 = 13,433` is only
> part of the surface. A backing report that quotes `opcode__ ≈ 1,925` and
> `memstore__ ≈ 212` is **merging sub-families**: `opcode__ (1,708) +
> opcode_complete__ (214) = 1,922 ≈ 1,925`, and `memstore__ (106) +
> memstore_check__ (106) = 212`. This page reports the *unmerged*, binary-exact
> tallies, matching the census already published on
> [libcas-core Surface](./cas-core-surface.md) §3. *(HIGH · OBSERVED — re-grepped
> against `nm -D`, not the decompile.)*

### 2.2 The `libfiss.h` semantic-stage contract

The family prefixes are not arbitrary — the recovered header fixes a 7-slot
semantic-stage vocabulary with named indices, and the binary's families map
one-to-one onto them:

```c
/* tools/XtensaTools/include/iss/libfiss.h */
#define LIBFISS_VERSION 2
#define SEMANTIC_STAGES 7
#define STATELOAD_SEMANTIC_FN_INDEX        0
#define REGLOAD_SEMANTIC_FN_INDEX          1
#define MEMLOAD_SEMANTIC_FN_INDEX          2
#define MEMSTORE_SEMANTIC_FN_INDEX         3
#define MEMSTORE_CHECK_SEMANTIC_FN_INDEX   4
#define WRITEBACK_SEMANTIC_FN_INDEX        5
#define OPCODE_COMPLETE_SEMANTIC_FN_INDEX  6
```

Every `stateload__`/`regload__`/`memload__`/`memstore__`/`memstore_check__`/
`writeback__`/`opcode_complete__` export fills one cell of a per-opcode
`semantic_functions[opcode][7]` table (the data symbol `semantic_functions`
`@.data 0xc77e80`), and the per-stage execute callbacks fill the parallel
`stage_functions[opcode][MAX_POSSIBLE_PARTIAL_STAGES=32]` table
(`stage_functions @.data 0xc18080`). The header `SEMANTIC_STAGES 7` and the
binary's seven non-decode/non-execute families agree exactly. *(HIGH · OBSERVED
for the families and the two data symbols; the header is corroboration.)*

### 2.3 The per-instruction quintuple (traced for scalar ADD)

Every executable mnemonic-variant carries a parallel set keyed by the **same**
decoded operand indices. For the base-ISA scalar `ADD`:

| Phase | Symbol | Addr | Action |
|---|---|---|---|
| decode | `slotfill__x24__Inst_slot0__ADD` | `0x296320` | shr/and out r/s/t nibbles → operand-index slots |
| arch state | `stateload__add` | `0x8875f0` | pull window/config state into the op context |
| reg fetch | `regload__add` | `0x887640` | read regfile[src1idx], [src2idx] → value slots `0x4c`/`0x54` |
| **execute** | `opcode__add__stage_14` | `0x8875e0` | **`ctx+0x4c` + `ctx+0x54` → `ctx+0x28`** |
| commit | `writeback__add` | `0x887660` | `ctx+0x28` → regfile[dstidx] |

The execute body is the whole point:

```text
opcode__add__stage_14 @0x8875e0 :  (rdi = per-op context)
  mov  0x4c(%rdi),%eax        ; operand A (loaded by regload__add)
  add  0x54(%rdi),%eax        ; A + B   <-- REAL ADDITION
  mov  %eax,0x28(%rdi)        ; result -> result slot (read by writeback__add)
  xor  %eax,%eax ; ret
```

The five families are element-wise parallel — **1,534 each** of
`stateload__`/`regload__`/`writeback__`, one per executable mnemonic-variant —
the classic template-generated ISS shape. *(HIGH · OBSERVED — the four ADD
symbols are contiguous at `0x8875e0`–`0x887660`; the execute arithmetic is
disassembled above.)*

> **NOTE — scalar retires at stage 14, vector at stage 5.** The execute callback
> suffix is the modeled retire stage. `opcode__add__stage_14` (scalar base ISA)
> retires at stage 14; `opcode__ivp_addnx16t__stage_5` (IVP vector) at stage 5.
> The stage distribution across all `opcode__` exports: **stage_5 (≈1,140) =
> vector/IVP execute; stage_14 (212) = scalar base-ISA execute**; stage_6 = SR
> write (wsr/xsr); the rest are latch/commit sub-stages. *(MED · INFERRED — read
> from the stage suffix plus execute-body placement, not a literal cycle count.)*

---

## 3. slotfill — the operand-decode entry path

`slotfill__` (12,569 exports, the single largest family) is **pure bitfield
decode**. It loads the raw instruction word and shr/and-masks the
register-index sub-fields into the operand-latch slots that the quintuple's
later phases consume. **It performs no arithmetic on operand values.**

### 3.1 Naming grammar (how an encoded bundle reaches a value leaf)

```
slotfill__<FMT>__<FMT>_<Sn>_<UNIT>_slot<k>__<MNEMONIC>[_<tag>]   (FLIX bundles)
slotfill__x24__Inst_slot0__<MNEMONIC>[_<tag>]                    (24-bit base ISA)
slotfill__x16{a,b}__Inst16{a,b}_slot0__<MNEMONIC>_N             (16-bit density)
```

`<FMT>` ∈ {F0, F1, F2, F3, F4, F6, F7, F11, N0, N1, N2} (11 FLIX issue formats)
plus the 3 base-ISA pseudo-formats {x24, x16a, x16b}. The `_<tag>` suffix
(`_A` addr/AR-form, `_S` source/short, `_H` half/long, `_W15` window-15 branch,
`_N` 16-bit density, `_WB` writeback) is a per-slot **role/encoding selector**,
not a new opcode — the same `MNEMONIC` recurs under multiple (slot, tag) because
a FLIX format can issue the op from several slots. *(HIGH · OBSERVED for the
grammar; LOW for the precise tag meanings, inferred from operand-decode bodies +
Xtensa form conventions.)*

### 3.2 slotfill body is pure decode (annotated disassembly)

```text
slotfill__x24__Inst_slot0__ADD @0x296320 :  (rdi = decode context)
  mov  0x2c(%rdi),%eax        ; predicate: is operand0 a special form?
  mov  0x30(%rdi),%edx        ; base slot index
  shl  $0x3,%edx              ; * 8 (slot stride)
  mov  (%rdi),%esi            ; raw 24-bit instruction word
  ...
  shr  $0xc,%esi ; and $0x7,%esi    ; extract the 't' register nibble (bits 12..14)
  shr  $0xb,%ecx ; and $0x1,%ecx    ; extract a sub-field bit
  ... (shr/and/or/lea masking only — NO add/sub/mul on operand DATA) ...
```

Every operation is `shr`/`and`/`or`/`lea` over the instruction word and a slot
stride. "slotfill" = *fill the decoded operand slots*, not *compute*. The IVP
vector slotfill (`slotfill__F0__F0_S3_ALU_slot3__ADD_S`) is the same shape over
the wider IVP vec-reg fields. *(HIGH · OBSERVED.)*

### 3.3 Issue-slot / functional-unit grid

The slotfill exports partition by format × issue-slot × functional-unit. The
ALU slot is the busiest, matching a VLIW with up to 4–5 issue slots:

| Unit token | ≈Count | Meaning |
|---|---|---|
| `_S3_ALU_slot3` | 3,931 | ALU slot (add/sub/abs/neg/min/max/logical/shift) — **primary** |
| `_S2_Mul_slot2` | 2,919 | Multiply slot (mul/madd/msub/quad-mul matrix) |
| `_S0_LdSt_slot0` | 2,283 | Load/Store slot (memory + addr-gen) |
| `_S1_Ld_slot1` | 2,025 | Load slot (load + some ALU helpers) |
| `_S0_LdStALU_slot0` | 542 | combined LdSt+ALU slot (F1 only) |
| `_S0_Ld_slot0` | 285 | Load-only slot variant (F4/F11) |
| `_S4_ALU_slot4` | 182 | second ALU slot (F3/F11 dual-ALU formats) |
| `_S1_ALU_slot1` | 66 | ALU in slot1 (F11) |
| `_S{1,2}_None` | 3 | empty/reserved slots (N0/N1) |

Per-format slotfill totals (the slice key for the three slotfill format pages):
F1=1809, F7=1674, F2=1549, F0=1494, F3=1436, F6=1045, F4=753, F11=686, N0=652,
N2=580, N1=558, x24=319, x16b=10, x16a=4. These split cleanly into
[Slotfill formats F0–F3](./fiss-slotfill-f0-f3.md),
[F4–F11](./fiss-slotfill-f4-f11.md), and [N0–N2](./fiss-slotfill-n0-n2.md);
the base-ISA x24/x16a/x16b set carries the 319 scalar Xtensa core opcodes plus
the 14 density `_N` forms (`ADD_N`/`ADDI_N`/`L32I_N`/`S32I_N`/`MOVI_N`/`BEQZ_N`/
`BNEZ_N`/`RET_N`/`NOP_N`/…). *(HIGH · OBSERVED for the unit grid and per-format
totals; MED for the exact F1-only / F11-only attributions.)*

---

## 4. The Xtensa exception model (value-side)

The `exception__` family is **61 self-contained fault handlers** plus the
`exception_fns` dispatch accessor (62 symbols total in the family). This is the
**value-side** fault model — distinct from the cas timing-side exception table
and from the XEA3 firmware-side ErrorHandler:

- **cas timing-side**: `dll_exception_table` (1008 B, 123 relocs) — names *which*
  fault each instruction *can* raise, for scheduling, but evaluates no predicate.
- **fiss value-side (this page)**: each `exception__*` handler is a complete
  ~0x4df-byte **instruction-word predicate evaluator** that decodes the live
  FLIX bundle and decides whether the fault condition *actually* applies. It
  makes no external call (e.g. `exception__IntegerDivideByZeroException` is
  ~104 `mov` / 67 `or` / 39 `and` / 35 `sete` / 18 `cmp`, all self-contained).
- **firmware-side**: the on-device XEA3 vector handler that *fields* a delivered
  fault. The 61-entry roster here is precisely the fault set that firmware must
  handle.

*(HIGH · OBSERVED — handler count 61; body shape read from
`exception__IntegerDivideByZeroException @0x498970`, which pushes 6 callee-saved
regs and decodes from `%rdi` offsets with no `call`.)*

### 4.1 Full roster (all 61, grouped by class)

| Class | Handlers |
|---|---|
| **Fetch / decode** | `IllegalInstructionException`, `InstructionFetchError`, `InstFetchProhibitedException`, `InstFetchUndefinedAttrException`, `InstTLBMultiHitException`, `InstInvalidTCMException`, `InstCrossMemException`, `InstUnspecifiedError`, `InstAxiSlvErrorException`, `InstAxiDecErrorException`, `L1SHitL1VMissException`, `PrivilegedException`, `ExternalRegisterPrivilegeException`, + Sem* replays (`SemInstructionFetchError`, `SemInstFetchProhibitedException`, `SemInstFetchUndefinedAttrException`, `SemInstTLBMultiHitException`) |
| **Load / store / memory** | `LoadStoreError`, `LoadStoreAlignmentException`, `LoadStoreProhibitedException`, `LoadStoreUndefinedAttrException`, `LoadStoreTLBMultiHitException`, `LoadStoreBusErrorException`, `LoadStoreAXISlvErrorException`, `LoadStoreAXIDecErrorException`, `LoadStoreSGAccErrorException`, `LoadStoreSlaveAtomicErrorException`, `LoadStoreFlixMemoryException`, `LoadStoreLargeDowngradeBlockPFetch`, `UnSupportedMemAndOpException`, `ExclusiveError`, `L32ETailchainException`, `TailchainException` |
| **Stack-limit (KSL/ISL)** | `StackLimitViolationException`, `KSLStackLimitViolationException`, `ISLStackLimitViolationException` |
| **Coprocessor (CPENABLE-gated)** | `Coprocessor0Exception` … `Coprocessor6Exception` (7) |
| **Arithmetic / FP** | `IntegerDivideByZeroException`, `ImpreciseException`, `ImpreciseSGFPVecException` |
| **Windowed-register ABI** | `WindowOverflow8`, `WindowUnderflow8`, `WindowInstCall0Exception` |
| **Debug / break / OCD** | `BreakException`, `BreakNException`, `IBreakException`, `DBreakException`, `SingleStepException`, `MaybeOCDBreakException`, `OCDInterrupt`, `HaltException`, `HaltStayException`, `WaitiFallThru` |
| **Interrupt / syscall** | `Interrupt1`, `SyscallException` |

*(HIGH · OBSERVED — names read verbatim from `nm -D`; the count is exactly 61.)*

> **NOTE — `Sem*` are semantic-phase replay duplicates.** `SemInstructionFetchError`,
> `SemInstTLBMultiHitException`, etc. mirror their architectural twins; they fire
> when the ISS replays a fetch in its *semantic* phase and share the same
> EXCCAUSE encoding. The `MAX_POSSIBLE_EXCEPTIONS 64` cap in `libfiss.h`
> corroborates the 61-handler count (61 < 64). *(MED · INFERRED for the
> replay role; the header cap is corroboration.)*

### 4.2 Exception dispatch — opcode → fault-set

Delivery is table-driven. Three `.rodata` arrays connect each opcode to its
candidate fault set, consumed by `exception_fns`:

| Symbol | Section | Role |
|---|---|---|
| `opcode_num_exceptions` | `.rodata 0x950140` | per-opcode count of applicable faults |
| `opcode_exceptions_idxs` | `.rodata 0x890540` | per-opcode list of `exception__*` handler indices |
| `opcode_exceptions_operandarg_idxs` | `.rodata 0x8f0340` | which operand each fault inspects |
| `exception_fns` | `.data 0xc17e80` | the dispatch fn-ptr table (the accessor) |

```text
/* annotated dispatch entry path (value-side) */
for op in decoded_bundle.slots:
    n = opcode_num_exceptions[op.opcode]          /* .rodata 0x950140 */
    for k in 0 .. n-1:
        ex_idx = opcode_exceptions_idxs[op.opcode][k]   /* .rodata 0x890540 */
        arg    = opcode_exceptions_operandarg_idxs[...]  /* which operand */
        if exception_fns[ex_idx](ctx, op, arg):    /* run the predicate evaluator */
            deliver(ex_idx)                        /* set EXCCAUSE/EXCVADDR, vector */
```

`opcode_num_exceptions` reads `00 00 00 00 / 01 00 00 00 / 01 00 00 00 …` — a flat
`u32[opcode]` table (opcode 0 raises none; most opcodes have one candidate fault).
*(HIGH · OBSERVED — three R-section symbols present at the addresses above; the
table head dumped with `xxd` since `.rodata` is VMA==file-offset.)*

### 4.3 Exception-relevant special registers

The fault model writes architectural trap state through the modeled SR set. Each
SR has the full `rsr_`/`wsr_`/`xsr_` triple (`nm -D` confirms all three for each):

| SR group | Registers | Role |
|---|---|---|
| Core trap state | `exccause`, `excvaddr`, `epc`, `ps`, `vecbase` | cause / faulting address / return PC / status / vector base |
| Coprocessor gate | `cpenable` | gates `Coprocessor0..6` faults |
| Stack limits | `ksl`, `isl` | kernel / interrupt stack-limit (`{K,I}SLStackLimitViolation`) |
| HW breakpoints | `ibreaka0/1`, `ibreakc0/1`, `dbreaka0/1`, `dbreakc0/1` | `IBreak`/`DBreak` |
| Timer | `ccount`, `ccompare0/1/2` | `Interrupt1` timer source |
| Atomic | `atomctl` | `LoadStoreSlaveAtomicError` / `ExclusiveError` |

> **GOTCHA — a single `epc`, not banked EPC1..7.** The SR set exposes one `epc`,
> not the windowed `EPC1`..`EPC7` of a multi-level Xtensa configuration. This is a
> **Call0-ABI single-exception-level** core configuration: every fault re-enters
> at one level. A reimplementation that allocates 7 banked EPC registers will not
> match. *(MED · INFERRED from the single-`epc` SR; consistent with the
> `WindowInstCall0Exception` Call0-transition fault being present.)*

> **NOTE — EXCCAUSE numeric codes.** The canonical Xtensa cause codes
> (`IllegalInstruction`=0, `Syscall`=1, `IntegerDivideByZero`=6,
> `LoadStoreAlignment`=9, `Coprocessor0..6`=32..38) are matched to the handler
> *names*, not read from an in-binary cause table. The dispatch uses handler
> *indices* (`opcode_exceptions_idxs`), so the numeric EXCCAUSE assignment is
> CARRIED from the Xtensa ISA, not OBSERVED here. *(MED · CARRIED.)*

---

## 5. The value-math entry (decode → value, annotated)

The full path from an encoded bundle to a committed element value, naming real
symbols. For the IVP vector add `ivp_addnx16t` over a 512-bit / 32×16-bit NX16
vector:

```c
/* ---- decode (no arithmetic) ---- */
slotfill__F0__F0_S3_ALU_slot3__ADD_S(decode_ctx);   /* shr/and reg fields -> slots */

/* ---- per-op fetch + state ---- */
stateload__ivp_addnx16t(op_ctx);     /* arch state into context              */
regload__ivp_addnx16t(op_ctx);       /* regfile[vs1], regfile[vs2] -> slots  */

/* ---- execute: scalar per-lane loop (reference-model, NO SIMD) ---- */
opcode__ivp_addnx16t__stage_5(op_ctx) {            /* @0x256d50 */
    for (lane = 0; lane < 32; ++lane) {            /* 16-bit lanes of NX16   */
        int16_t a = op_ctx->vs1[lane], b = op_ctx->vs2[lane], out;
        module__xdref_add_16_16_16(ctx, a, b, &out);   /* @0x858480: (a+b)&0xffff */
        module__xdref_bitkillt_16_2(ctx, out, pred, &op_ctx->vd[lane]);  /* mask */
    }
}                                                   /* 27 active lanes observed */

/* ---- fault check + commit ---- */
exception_fns[opcode_exceptions_idxs[op][k]](op_ctx);   /* §4.2 predicate */
writeback__ivp_addnx16t(op_ctx);                        /* vd -> regfile  */
```

The element value is produced *inside* `module__xdref_add_16_16_16` — the
verified `add %esi,%edx ; and $0xffff,%edx ; mov %edx,(%rcx)` of §1.2. There is
no host callback on this path; the whole computation is internal to
`libfiss-base.so`. *(HIGH · OBSERVED for every named symbol and the execute
body; the per-lane loop bound is read from the unrolled call multiplicity.)*

### 5.1 The `module__xdref_*` datapath (the 864 value leaves)

All 864 `module__` exports are `xdref_<op>_<widthtuple>` per-lane primitives.
The width tuple gives dst/src element widths (8/16/24/32/48/64/96-bit int,
16f/32f/64f soft-float; 512 = full-vector bool). Distinct operation roots (200+)
span add/adds/sub/subs/neg/abs/avg, min/minu/max/maxu/minnum/maxnum, and/or/xor
+ boolean `*b` variants, shifts, the full mul matrix
(`mul/muln/muls/mula/madd/msub` signed/unsigned/mixed), IEEE ordered/unordered
compare predicates, reciprocal/rsqrt/sqrt seeds, converts, pack/select, bit
counts, and FP normalize helpers. Integer ops are 5–50 bytes (single-lane
scalar); soft-float ops are large (fp16 add ≈ 2 KB, fp32 ≈ 6.5 KB) — full
IEEE-754 emulation with **no hardware FP** (no x87/SSE in the body). Worked
disassembly bodies — wrapping vs saturating vs signed-min vs widening-mul vs
soft-float — are enumerated in
[The fiss Datapath Oracle](./fiss-datapath-oracle.md).

Representative addresses (all live-callable by absolute symbol):

| Symbol | Addr | Computes |
|---|---|---|
| `module__xdref_add_16_16_16` | `0x858480` | `(a+b) & 0xffff` — wrapping int16 |
| `module__xdref_add_8_8_8` | `0x5bc380` | `(a+b) & 0xff` — wrapping int8 |
| `module__xdref_add_32_32_32` | `0x5bc340` | native 32-bit wrap |
| `module__xdref_adds_16_16_16` | `0x85aa10` | saturating int16 (clamp 0x7fff/0x8000) |
| `module__xdref_minu_16_16_16` | `0x8584f0` | `cmp ; cmova` — unsigned min |
| `module__xdref_mula_24_24_8_8` | `0x68a820` | i8×i8 multiply-accumulate (driven live, prior wave) |
| `module__xdref_mula_48_48_16_16` | `0x68a6b0` | i16×i16 multiply-accumulate |
| `module__xdref_muls_24_24_8_8` | `0x82c170` | i8×i8 multiply-subtract |

*(HIGH · OBSERVED — addresses from `nm -D`; the `mula_*` pair was certified live
8/8 in a prior datapath wave, re-confirmed present here.)*

---

## 6. Section map and the `.data` offset hazard

`.text` and `.rodata` are VMA==file-offset, so `xxd`/`objdump` on code/rodata
addresses (and the `opcode_exceptions_*` tables of §4.2) work directly. The
writable sections are **not** identity-mapped — the ncore2gp DLL delta is
**`0x200000`** (the same family caveat as `libcas-core.so`, *not* libtpu's
`0x400000`). Confirm per-section with `readelf -SW`:

| Section | VMA | File off | VMA − fileoff |
|---|---|---|---|
| `.text` | `0x190430` | `0x190430` | 0 |
| `.rodata` | `0x88ff00` | `0x88ff00` | 0 |
| `.data.rel.ro` | `0xc17e80` | `0xa17e80` | **`0x200000`** |
| `.data` | `0xc8eb68` | `0xa8eb68` | **`0x200000`** |
| `.bss` | `0xc8eb70` | (NOBITS) | — |

> **GOTCHA — the dispatch arrays live in `.data`/`.data.rel.ro`.**
> `semantic_functions` (`@0xc77e80`), `stage_functions` (`@0xc18080`), and
> `exception_fns` (`@0xc17e80`) all resolve to writable sections. To read their
> *bytes* off the file, subtract `0x200000` (e.g. `exception_fns` file offset
> `0xa17e80`). Applying libtpu's `0x400000` — or assuming VMA==fileoffset because
> `.text`/`.rodata` are — lands in the wrong place and yields a spurious "table
> is garbage" finding. The `opcode_exceptions_*` tables, by contrast, are in
> `.rodata` and *do* read directly. *(HIGH · OBSERVED.)*

---

## 7. How this slices and connects

This value-side foundation feeds four downstream pages, each taking a budget out
of the §2/§3 census:

| Downstream page | Inherits |
|---|---|
| [The fiss Datapath Oracle](./fiss-datapath-oracle.md) | the 864 `module__xdref_*` value leaves — reads bodies to ground-truth IVP semantics |
| [Slotfill F0–F3](./fiss-slotfill-f0-f3.md) | 6,288 slotfill (the four wide FLIX formats) |
| [Slotfill F4–F11](./fiss-slotfill-f4-f11.md) | 4,158 slotfill (F7 wide; F4/F6 load-heavy; F11 dual-ALU) |
| [Slotfill N0–N2](./fiss-slotfill-n0-n2.md) | 1,790 slotfill (the narrow N formats; rare `_None` slots) |

The cas/fiss split this page completes is established on
[libcas-core Surface](./cas-core-surface.md); how both halves are *invoked*
(the run half of dlopen/register/run) is on
[Runnable ISS Infrastructure](./runnable-iss-infra.md); and both are unified into
a runnable fault-injection reference on
[The ISS as Executable Oracle](./iss-oracle-synthesis.md). Cross-validation
against the reference cores (`libfiss-ref-base.so`) is referenced by title only —
that Part is not yet written.

---

## 8. Honesty ledger

- **[HIGH · OBSERVED]** 20,379 dynamic exports (`nm -D --defined-only`); only 5
  undefined imports, all weak libc/runtime, **zero** `nx_*_interface` — fiss is
  self-contained.
- **[HIGH · OBSERVED]** The value verdict: `opcode__add__stage_14` does a real
  scalar ADD; `opcode__ivp_addnx16t__stage_5` calls `module__xdref_add_16_16_16`
  27× per-lane; `module__xdref_{add,adds,sub,minu}` disassemble to real
  wrapping/saturating/unsigned arithmetic; **15/15 live ctypes pass**; no packed
  SIMD anywhere in the image.
- **[HIGH · OBSERVED]** The family census (12,569 slotfill / 864 xdref / 1,534×3
  load-state-writeback / 61 exception …) summing to exactly 20,379; the
  864/12,569/20,379 reconciliation as a nested superset chain.
- **[HIGH · OBSERVED]** 61-entry exception roster (verbatim names) + the
  `opcode_num_exceptions`/`opcode_exceptions_idxs`/`exception_fns` dispatch
  tables + the EXCCAUSE/EXCVADDR/EPC/CPENABLE/KSL/ISL SR set.
- **[MED · INFERRED]** Stage numbers as literal retire stages (scalar 14, vector
  5); `Sem*` as semantic-phase replay duplicates; single-`epc` ⇒ Call0-ABI
  single-exception-level core.
- **[MED · CARRIED]** EXCCAUSE numeric codes (0/1/6/9/32–38) matched to handler
  names, not read from an in-binary cause table.
- **[LOW · INFERRED]** The `_A`/`_S`/`_H`/`_W15` slotfill tag meanings; exact
  retire latency beyond the named execute stage.
