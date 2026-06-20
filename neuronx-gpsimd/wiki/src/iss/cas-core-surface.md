# libcas-core Surface + ISS Plugin ABI

> **Part 14 gateway.** This page frames the whole `iss/` chapter. It establishes
> the single fact every later ISS page rests on: the *cycle-accurate* simulator
> core (`libcas-core.so`) is a **timing and hazard oracle**, not a value oracle.
> It schedules, stalls, and forwards operands but never computes a single element
> result. The arithmetic lives in a sibling plugin, `libfiss-base.so`, documented
> in [The fiss Datapath Oracle](./fiss-datapath-oracle.md). The two are bolted
> together by the harness through a fixed plugin ABI defined in `libfiss.h`
> (`LIBFISS_VERSION 2`) and exercised here.

All facts below are derived from static analysis of the shipped binary only:

```
extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/libcas-core.so
ELF64 LSB shared object, x86-64, SYSV, dynamically linked, NOT stripped
45,878,080 bytes (45.9 MB)
```

Tooling: `nm -D`, `readelf -d/-V/-r/-S`, `objdump -d/-s`. Each claim is tagged
**confidence** × **provenance** (`OBSERVED` = read directly off the binary,
`INFERRED` = strong deduction from observed evidence, `CARRIED` = taken from a
prior report and re-checked here). The one recovered header cited
(`tools/XtensaTools/include/iss/libfiss.h`) is treated as a binary-adjacent
artifact: it ships in the same toolchain and its constants are corroborated
against the binary at every use.

---

## 1. Identity and versioning

| Property | Value | Evidence | Conf · Prov |
|---|---|---|---|
| Version-def BASE | `libcas-Xtensa.so` | `.gnu.version_d` index 1, `Flags: BASE` | HIGH · OBSERVED |
| Symbol version | `VERS_1.1` | `.gnu.version_d` index 2; every export binds `@@VERS_1.1` | HIGH · OBSERVED |
| Version anchor | `VERS_1.1` `@0x0` type `A` | `nm -D` shows one absolute symbol | HIGH · OBSERVED |
| ABI/build stamp | `dll_get_version()` → `0x1381f` (79903) | `mov $0x1381f,%eax ; ret` `@0x1776580` | HIGH · OBSERVED |
| Per-instance state size | `dll_get_data_size()` → `0x4a09f0` (4,851,184 B) | `mov $0x4a09f0,%eax ; ret` `@0x1776570` | HIGH · OBSERVED |
| Compiler | GCC (GNU) 4.9.4 | `.comment` string dump | HIGH · OBSERVED |
| SONAME / NEEDED | *none* | `readelf -d` has no `SONAME`/`NEEDED` entry | HIGH · OBSERVED |
| Modeled core | Cayman / Vision-Q7 IVP VLIW | regfile-name table + vector ISA op families (§4) | MED · INFERRED |

> **NOTE — naming.** `cas` = *cycle-accurate simulator*. The BASE string
> `libcas-Xtensa.so` is the generic Tensilica/Cadence ISS DLL identity; the
> *shipped* file is renamed `libcas-core.so` but keeps the BASE version-def. The
> file is a Tensilica TIE cycle-accurate ISS plugin specialized to the GPSIMD
> Cayman / Vision-Q7 vector core. (CARRIED from SX-ISS-01; the BASE string and
> the vector regfile names are OBSERVED.)

> **GOTCHA — "4.85 MB".** `dll_get_data_size` returns **4,851,184 bytes**. That
> is 4.85 *decimal* MB but only **4.628 MiB**. The harness `malloc`s exactly this
> many bytes per simulated core instance and passes the pointer as `rdi` (arg0)
> to every accessor; `dll_initialize` then `memset`s precisely `0x4a09f0` bytes
> (verified below). Do not size the instance buffer in MiB.

### Symbol census (`nm -D`)

| Class | Count | Evidence | Conf · Prov |
|---|---|---|---|
| Total dynamic symbols | 1232 | `nm -D` | HIGH · OBSERVED |
| Defined exports | 1112 | 1111 `T` + 1 `A` (`VERS_1.1`) | HIGH · OBSERVED |
| Undefined imports | 120 | `nm -D \| rg -c ' U '` | HIGH · OBSERVED |
| ↳ libc imports | 1 | `memset` — the *only* standard-library import | HIGH · OBSERVED |
| ↳ TIE-port callbacks | **119** | `rg -c 'nx_.*_interface'` over the `U` set | HIGH · OBSERVED |

The 1111 `T` functions partition cleanly: **24 `dll_*`** (plugin ABI, §2) +
**16 `opnd_sem_*`** (operand-address resolvers / regfile sizers) + **1071 `my_*`**
(TIE scoreboard accessors, §4). `24 + 16 + 1071 = 1111` — exact.

---

## 2. The plugin ABI (the `dll_*` accessor vocabulary)

> **CORRECTION — there is no `getInterface` and no `CycleCore::register_client`.**
> A full-symbol-table scan (`nm libcas-core.so`, 179,079 symbols) finds **neither
> symbol, nor `CycleCore`, nor any `*register_client*`** anywhere in the binary.
> The plugin ABI of this ISS family is **not** a C++ factory/registry; it is a
> flat C *accessor vocabulary* of 24 `extern "C"` `dll_*` entry points plus a
> harness-installed function-pointer vtable. The contract is fixed by the
> recovered header `libfiss.h` (`#define LIBFISS_VERSION 2`). Earlier framing that
> assumed a `getInterface`/`register_client` shape is wrong for this binary.
> *(HIGH · OBSERVED — absence verified against the unstripped symbol table.)*

### 2.1 dlopen model

The ncore2gp ISS driver (the "harness") `dlopen`s `libcas-core.so`, resolves the
24 `dll_*` accessors by name (all `@@VERS_1.1`), and drives the core entirely
through them. The core is *otherwise self-contained*: a single translation unit
with 472 PLT relocations but only 120 external dynamic imports — the sole runtime
coupling is `memset` plus the 119 `nx_*_interface` host callbacks (§3).
*(HIGH · OBSERVED — `PLTRELSZ 11328` / 24 = 472; `nm -D` import count = 120.)*

### 2.2 The 24 accessors

All accessors take the state pointer in `rdi` (arg0). Table-returning accessors
are a literal `lea <table>(%rip),%rax ; ret`.

| Accessor | Addr | Returns / action | Conf · Prov |
|---|---|---|---|
| `dll_get_version` | `0x1776580` | imm `0x1381f` | HIGH · OBSERVED |
| `dll_get_data_size` | `0x1776570` | imm `0x4a09f0` | HIGH · OBSERVED |
| `dll_initialize` | `0x17b5c90` | `memset(state,0,0x4a09f0)` then register state (§2.3) | HIGH · OBSERVED |
| `dll_init_tieport_outputs` | `0x17b5c80` | reset TIE output ports | MED · INFERRED |
| `dll_reset_states` | `0x17b5810` | reset architectural state | MED · INFERRED |
| `dll_get_stage_functions` | `0x17aa360` | `&slot_semantic_functions` (1504 B = 188 ptr slots) | HIGH · OBSERVED |
| `dll_get_issue_functions` | `0x17aa340` | `&slot_issue_functions` (188 slots) | HIGH · OBSERVED |
| `dll_get_stall_functions` | `0x17aa2e0` | `&slot_stall_functions` (188 slots) | HIGH · OBSERVED |
| `dll_get_shared_functions` | `0x17aa3a0` | `&dll_shared_functions` (.bss, runtime-set) | HIGH · OBSERVED |
| `dll_get_exception_table` | `0x17aa2d0` | `&dll_exception_table` (1008 B, 123 relocs) | HIGH · OBSERVED |
| `dll_get_state_table` | `0x17aa370` | `&dll_state_table` (4592 B; regfile-offset oracle) | HIGH · OBSERVED |
| `dll_get_regfile_table` | `0x17aa380` | `&dll_regfile_table` (504 B = 63 slots, 48 relocs) | HIGH · OBSERVED |
| `dll_get_ext_regfile_table` | `0x17aa390` | all-zero 32 B → **no aux regfiles** | HIGH · OBSERVED |
| `dll_get_queue_info` | `0x17b57d0` | `&dll_queue_info` (.data) | HIGH · OBSERVED |
| `dll_get_lookup_info` | `0x17b57e0` | `&dll_lookup_info` (.data) | HIGH · OBSERVED |
| `dll_cycle_advance` | `0x17aa3c0` | one pipeline tick (§2.4) | HIGH · OBSERVED |
| `dll_reset_cycle_advanced` | `0x17aa3b0` | `andb $0xf7,0x5a8(rdi)` — clears flags bit3 | HIGH · OBSERVED |
| `dll_set_tie_stall_eval` | `0x17aa2f0` | set/clear flags bits 1..2 at `0x5a8(rdi)` | HIGH · OBSERVED |
| `dll_enable_events` | `0x17b57f0` | toggles flags bit0 at `0x5a8(rdi)` | HIGH · OBSERVED |
| `dll_wait_for_tie_io` | `0x17aa350` | `xor eax,eax ; ret` — no-op stub (always 0) | HIGH · OBSERVED |
| `dll_kill_stage` | `0x17ae3e0` | squashes one pipeline stage | MED · INFERRED |
| `dll_export_state_stall` | `0x1795ba0` | stall hook for state export | MED · INFERRED |
| `dll_instruction_get_length` | `0x17b57a0` | `CSWTCH.4667[opcode_byte]` → byte length (256-entry) | HIGH · OBSERVED |
| `dll_instruction_speculate_length` | `0x17b57b0` | speculative length decode | MED · INFERRED |

> **QUIRK — `dll_wait_for_tie_io` is a stub.** It is literally `xor eax,eax ; ret`.
> The cycle-accurate core never blocks on TIE I/O: every TIE-port read is assumed
> resolvable in the same tick because the *value* comes from the host vtable, not
> from a modeled bus. This is a direct structural consequence of the no-values
> design — there is no datapath inside `libcas-core` that could stall on a result.
> *(HIGH · OBSERVED.)*

### Key state-struct offsets (decoded from accessor bodies)

| Offset | Meaning | Evidence | Conf · Prov |
|---|---|---|---|
| `state+0x0` | harness handle (arg1) | `mov %rbp,(%rbx)` in `dll_initialize` | HIGH · OBSERVED |
| `state+0x8..` | installed shared-helper vtable (copied from arg2) | `rep movsq` in `dll_initialize` | HIGH · OBSERVED |
| `state+0x5a8` | control/flags byte: bit0 event-trace, bits1–2 stall-eval mode, bit3 cycle-advanced | `dll_enable_events` / `dll_set_tie_stall_eval` / `dll_cycle_advance` / `dll_reset_cycle_advanced` | HIGH · OBSERVED |
| `state+0x484c` | current pipeline-stage index (masked `& 0x1f`) | read by every semantic accessor | MED · CARRIED |
| `state+0x4844` | per-stage def/dirty bitmask (`OR` bit `0x2000`) | semantic-accessor bodies | MED · CARRIED |
| `state+0x24ec` | cycle-advance ring head (`& 0x1f`, decremented per tick) | `dll_cycle_advance` body | MED · CARRIED |
| slot+`0x9554` / `0x94d4` / `0x93d4` | def-reservation: reg#, valid=1, value | per-stage reservation records | MED · CARRIED |

> **NOTE — the 32-deep stage ring.** `state+0x484c` and `state+0x24ec` are both
> masked `& 0x1f`, i.e. a 32-entry ring. This matches `libfiss.h`'s
> `#define MAX_POSSIBLE_PARTIAL_STAGES 32` exactly — the header bound and the
> binary's mask agree, which is why we trust the header as a binary-adjacent
> spec. *(HIGH · OBSERVED for the mask; the header is corroboration.)*

### 2.3 `dll_initialize` — the registration loop (annotated disassembly)

The most important function on this page. Its prologue proves both the state size
and the *registration* shape of the ABI (no `register_client` factory; instead the
core calls *back* into a harness-supplied vtable to publish its state slots).

```text
dll_initialize @0x17b5c90 :  (rdi=state, rsi=harness_handle, rdx=init_ctx)
  push r12 ; push rbp ; push rbx
  mov  rdx, r12              ; r12 = init_ctx (arg2)
  mov  rsi, rbp              ; rbp = harness_handle (arg1)
  mov  rdi, rbx              ; rbx = state (arg0)
  mov  $0x4a09f0, edx
  call memset@plt            ; <-- zero exactly 4,851,184 bytes of instance state
  mov  rbp, (rbx)            ; state+0x00 = harness_handle
  mov  (r12), rax
  mov  rax, 0x8(rbx)         ; state+0x08 = init_ctx->vtable_base
  lea  0x14eb0(rbx), rdx     ; rdx = &state->reg_slot[0]
  ...
  rep movsq                  ; copy the harness shared-helper vtable into state+0x10..
  mov  0x598(r12), rax
  mov  rax, 0x5a0(rbx)       ; mirror init_ctx control word
  ; --- registration loop (unrolled) ---
  mov  rbp, rdi
  lea  "REV8AR"(rip), rsi    ; .rodata 0x17cdb53 : a state-register NAME string
  call *0x10(rbx)            ; <-- INDIRECT call through the installed vtable:
                             ;     harness->register(handle, "REV8AR", &slot)
  lea  0x14eb8(rbx), rdx
  mov  rbp, rdi
  lea  <name>(rip), rsi      ; next register name (.rodata 0x17cdd0f ...)
  call *0x28(rbx)            ; another vtable slot (different register class)
  ... (repeated per architectural state register) ...
```

The semantics: the harness installs *its* function-pointer table into
`state+0x10..` (via `rep movsq`), then the core walks every architectural state
register and calls back through that table (`call *0x10(rbx)`, `call *0x28(rbx)`,
…), passing the harness handle, a name string from `.rodata`, and the address of
the matching state slot. **This is the "register" half of the dlopen/register/run
lifecycle** — implemented as core-calls-harness, not harness-calls-factory.
*(HIGH · OBSERVED — `memset 0x4a09f0`, `rep movsq`, and the `call *N(rbx)` chain
are all in the disassembly; the registered name `REV8AR` reads off `.rodata`.)*

### 2.4 `dll_cycle_advance` — the one-tick pipeline stepper

```text
dll_cycle_advance @0x17aa3c0 :  (rdi=state, esi=dir)
  - OR  flags bit3 (cycle-advanced) at state+0x5a8
  - iterate up to 4 write-back lanes (offset 0x24.. block, stride 0x300),
    committing pending register writes through the +0x8f8 scoreboard array
    for any stage whose count field is nonzero
  - decrement ring head state+0x24ec (& 0x1f)
  - branch on dir(esi): dir==0 / dir<0 / dir>0 select
    forward-commit vs. rollback paths     (~5 KB of code)
```

This is the single function that turns wall time into pipeline progress: it
commits the scoreboard's pending writes and rotates the 32-deep stage ring. It
moves *scoreboard tokens*, never element data — the values were already produced
host-side. The timing detail is the subject of
[The cas Timing Model](./cas-timing-model.md). *(MED · CARRIED; the bit3 OR and
ring-head decrement are OBSERVED.)*

---

## 3. The division of labor — cas does timing, fiss does values

> This is the finding the entire Part rests on. State it precisely, then prove it.

**Claim.** `libcas-core.so` (the cycle-accurate ISS) **does not compute any
element value**. It only declares, per instruction, which architectural registers
and TIE ports are *read* and *written* and when, then schedules/forwards/stalls
on that read/write set. Every actual value — the result of a multiply, an FMA, a
gather, a convert — is produced **host-side**, requested through one of the 119
`nx_*_interface` TIE-port callbacks, and computed in the sibling plugin
`libfiss-base.so`.

**Proof 1 — cas exports zero value functions.** The value-producing functions in
this ISS family are named by the seven semantic stages of `libfiss.h`
(`stateload__`, `regload__`, `memload__`, `memstore__`, `memstore_check__`,
`writeback__`, `opcode_complete__`) plus `slotfill__` (the datapath slot-fill
kernels) and `opcode__`. **`libcas-core.so` exports none of them** — every grep
for those prefixes over its `nm -D` exports returns 0. Its 1071 `my_*` exports are
*pure scoreboard accessors*: 1068 of them end in a hazard-role suffix
(`{def, def_commit, use, use_commit, set_def, kill_def, stall, bitkill_def}`):

| Role suffix | `my_*` count | | Role suffix | `my_*` count |
|---|---|---|---|---|
| `_def` | 496 | | `_set_def` | 152 |
| `_use` | 317 | | `_kill_def` | 152 |
| `_stall` | 93 | | `_def_commit` | 81 |
| `_bitkill_def` | 40 | | `_use_commit` | 81 |

Total role-suffixed accessors = **1068** of 1071. The remaining 3 are the
SuperGather deferred-commit hooks (`my_gvr_commit_data`, `my_gvr_def_addr`,
`my_gvr_use_addr`). None of these names a datapath operation; they name *which
registers an op touches and in which pipeline role*. *(HIGH · OBSERVED.)*

**Proof 2 — fiss-base exports exactly the value functions.** The sibling
`libfiss-base.so` (12,330,016 B) carries the matching set, keyed by the same seven
`libfiss.h` stage prefixes plus `slotfill__`:

| fiss-base prefix | export count | | fiss-base prefix | export count |
|---|---|---|---|---|
| `slotfill__` | 12,569 | | `opcode__` | 1,708 |
| `stateload__` | 1,534 | | `regload__` | 1,534 |
| `writeback__` | 1,534 | | `module__` | 864 |
| `opcode_complete__` | 214 | | `memload__` | 110 |
| `memstore__` | 106 | | `memstore_check__` | 106 |

These are the per-stage value kernels — the side documented in
[The fiss Datapath Oracle](./fiss-datapath-oracle.md) (the "864-leaf oracle" =
the `module__` set above). *(HIGH · OBSERVED.)*

**Proof 3 — the 119 ports are the value boundary.** cas-core's only non-libc
imports are 119 `nx_*_interface` callbacks (§3.1). These are *host TIE ports*:
the cas-core reads/writes element data only by calling them. Re-verified count:

```
$ nm -D libcas-core.so | rg ' U ' | rg -c 'nx_.*_interface'
119
$ nm -D libcas-core.so | rg ' U ' | rg -v 'nx_.*_interface'
                 U memset
```

**119**, no more, no less; the only other import is `memset`. *(HIGH · OBSERVED.)*

> **The reimplementation takeaway.** To rebuild a Vision-Q7-compatible engine you
> need *two* models, not one: a **timing model** (scoreboard read/write sets +
> the 32-deep stage ring + `dll_cycle_advance` commit logic — this binary) and a
> **value model** (the `slotfill__`/stage kernels — `libfiss-base.so`). The
> 119-port `nx_*_interface` vtable is the seam between them, and it is the seam
> your host must fill. The capstone [The ISS as Executable Oracle](./iss-oracle-synthesis.md)
> wires both together into a runnable reference.

### 3.1 The 119 `nx_*_interface` host ports

Grouped by regex bucket (groups overlap; counts are not a strict partition).
*(HIGH · OBSERVED for the count; MED · INFERRED for the functional grouping.)*

| Group | ≈Count | Representative ports |
|---|---|---|
| Memory / load-store / vector-mem | 52 | `nx_{Scalar,Vector}MemAccess`, `nx_MemDataIs{8..512}Bits_{0,1}`, `nx_Load_{0,1}` / `nx_Store_0` / `nx_StoreAcc`, `nx_VAddr{Base,Index,Offset,Filter,Res}_{0,1}`, `nx_MMUDataIn/Out`, `nx_ScatterData_0`, `nx_LoadStore{CrossMemAcc,Disable,InvalidTCMAcc,UnsupportedAtomicOp,WrongIramAcc}` |
| Interrupt / exception / NMI | 25 | `nx_Intr*` (priority/fairness/primary/secondary/NMI request+commit+sample), `nx_Exception{Cause,PC,VAddr,Vector}`, `nx_SYSCALLCause`, `nx_HaltImmediate`, `nx_DisableInterrupt` |
| SR/ER register-bus | 15 | `nx_{SR,ER}{Read,Write,Addr}`, `nx_{R,W}{SR,ER}Bus`, `nx_RSRTraceBus`, `nx_WSRTraceBus`, `nx_SRWriteLBEG/LEND`, `nx_RER/WERAddr`, `nx_RER/WERBus` |
| Branch / PC / predictor / loop | 11 | `nx_Branch{ImmOffset,PredictorHint,PredictorReadData,PredictorWriteData,Taken,Target}`, `nx_{PC_R1,PC_R2,NextPC}`, `nx_LCOUNT`, `nx_SpillWOverflow` |
| SuperGather control | 5 | `nx_GSControl_{0,1}`, `nx_GSEnable_{0,1}`, `nx_GSVAddrOffset_0` |
| OCD / debug | 3 | `nx_OCDEnabled`, `nx_NextOCDEnabled`, `nx_BreakNum` |
| Misc (excl. monitor, instr-mem, …) | rem. | `nx_EXCLRES`/`_next`, `nx_InstructionMemDataIn`, `nx_NMILock`, `nx_MEMCTLOut`, `nx_S32dis*` |

> **NOTE — dual `_0`/`_1` LSU.** Many memory ports come in `_0`/`_1` pairs
> (`nx_Load_0`/`nx_Load_1`, `nx_VAddrBase_0`/`_1`, `nx_GSControl_0`/`_1`). This is
> two parallel vector memory pipes — a **2-lane LSU** — and it propagates into the
> load/store and gather/scatter ISS pages. *(MED · INFERRED from the suffix pattern.)*

---

## 4. Operand resolvers, regfile sizes, and the modeled core

The 16 `opnd_sem_<rf>_addr` resolvers mask the instruction operand field to a
register index; **the mask is the regfile size**. This is the cleanest way to read
the modeled core's register topology straight off the binary. *(HIGH · OBSERVED.)*

| Resolver | Mask | Regfile size | Regfile role |
|---|---|---|---|
| `opnd_sem_vec_addr` | `& 0x1f` | 32 | vector registers |
| `opnd_sem_vbool_addr` | `& 0x0f` | 16 | boolean/predicate lanes |
| `opnd_sem_b32_pr_addr` | `& 0x0f` | 16 | b32 predicate file |
| `opnd_sem_BR_addr` | `& 0x0f` | 16 | Xtensa `BR` boolean file |
| `opnd_sem_wvec_addr` | `& 0x03` | 4 | wide accumulators (claimed 1536-bit) |
| `opnd_sem_valign_addr` | `& 0x03` | 4 | alignment registers |
| `opnd_sem_gvr_addr` | `& 0x07` | 8 | SuperGather global-state file |

Plus the Xtensa **windowed** AR family (`opnd_sem_AR_addr` / `_AR_0_addr` /
`_AR_8_addr` / `_AR_entry_addr` / `_AR_stk_addr`) and `BR2/BR4/BR8/BR16` boolean
sub-field resolvers. The regfile-name table at `.rodata 0x17cdcf2` literally
spells `gvr.wvec.vbool.valign.b32_pr` (and `vec`, `AR` nearby), corroborating each
file. See [Register Files](../isa/core/register-files.md) for the ISA-side view.

> **QUIRK — `gvr` is present here but absent from the public `tie.h`.** The
> 8-entry SuperGather global-state file is confirmed three independent ways:
> (1) the `gvr` regfile-name string at `.rodata 0x17cdcf2`; (2) `opnd_sem_gvr_addr`
> masks `& 0x7` → 8 entries; (3) 10 `my_gvr_*` accessors bound to
> `vec_scatter_gather` semantics with a deferred-commit path
> (`my_gvr_commit_data`). The ISS exposes `gvr` even though the public TIE header
> does not. Its *width* (claimed 512-bit) is **not** provable from this surface —
> read it from `dll_state_table` strides in the gather/scatter page.
> *(HIGH · OBSERVED for presence and 8-entry count; LOW for width.)*

### Op-family fingerprint (why the modeled core is Cayman / Vision-Q7)

The `my_vec_*` / `my_wvec_*` semantic accessors carry the operation taxonomy of
the modeled VLIW vector ISA. The headline shapes (each a slice key in the export
names, all OBSERVED via `nm -D` regex buckets):

- **Integer/ALU datapath** — `vec_alu` 38, `vec_specialized_seli` 24,
  `vec_shift` 20, `vec_rep` 20, `vec_select` 12, `vec_reduce` 6, `vec_mov` 4,
  `sqz` 4.
- **MAC / wide accumulator** — `multiply` 14, `spfma` 20, `wvec_pack` 6,
  `unpack_wvec_mov` 4 (targets the 4-entry `wvec` file).
- **Memory** — `ld_st` 10 (+ `ld_st_vr2` variants), `vec_scatter_gather` 8.
- **Floating-point** — the *only* `fp_sem` operation is **`hp_fma`**
  (half-precision FMA): 24 functions. HP FMA is the FP path.
- **Transcendental seed** — the *only* `bbn_sem` op is **`vec_sprecip_rsqrt`**
  (reciprocal / reciprocal-sqrt): 8 functions.

This op set — IVP vector ALU + HP-FMA FP + SuperGather + windowed AR — is the
GPSIMD Cayman / Vision-Q7 IVP vector core. The per-instruction value semantics for
each family are owned by the later ISS pages and by the
[The fiss Datapath Oracle](./fiss-datapath-oracle.md); this gateway only fixes the
*surface and the slicing*.

---

## 5. Decode and fault tables (surface only)

| Table | Size | Contents | Conf · Prov |
|---|---|---|---|
| `dll_exception_table` | 1008 B, 123 relocs | 61 distinct `*_exc` handlers: `Window{Under,Over}flow`, `{Load,Inst}Store*`, `Coprocessor0..6`, `IntegerDivideByZero`, OCD/Break/DBreak/IBreak, and SuperGather faults `LoadStoreSGAccErrorException_exc`, `ImpreciseSGFPVecException_exc`, `ExclusiveError_exc`, `{ISL,KSL}StackLimitViolationException_exc` | HIGH · OBSERVED |
| `CSWTCH.4667` | 256 entries (signed byte) | `dll_instruction_get_length` indexes it by the low opcode byte → per-opcode byte length | HIGH · OBSERVED |
| `dll_regfile_table` | 504 B = 63 slots (48 relocs) | per-regfile descriptor table | HIGH · OBSERVED |
| `dll_state_table` | 4592 B | the authoritative per-register state-offset map; each later ISS page slices its regfile's rows from here | HIGH · OBSERVED |

`libfiss.h` caps exceptions at `MAX_POSSIBLE_EXCEPTIONS 64`, consistent with the
123-reloc / 61-handler table observed here (handlers < 64). *(Header bound is
corroboration; the 61 handlers are OBSERVED.)*

---

## 6. Section map and the `.data` offset hazard

`.text` and `.rodata` are VMA==file-offset, so `xxd`/`objdump` on code/rodata
addresses works directly. The writable sections are **not** identity-mapped — the
ncore2gp DLL delta is **`0x200000`** (not libtpu's `0x400000`). Confirm per-section
with `readelf -SW`:

| Section | VMA | File off | VMA − fileoff |
|---|---|---|---|
| `.text` | `0x0572fa0` | `0x0572fa0` | 0 |
| `.rodata` | `0x17ba840` | `0x17ba840` | 0 |
| `.data.rel.ro` | `0x2070900` | `0x1e70900` | **`0x200000`** |
| `.data` | `0x2280ed8` | `0x2080ed8` | **`0x200000`** |
| `.got.plt` | `0x2280000` | `0x2080000` | **`0x200000`** |

> **GOTCHA — the dispatch tables live in `.data.rel.ro`.** `slot_semantic_functions`
> (`@0x227ecc0`, returned by `dll_get_stage_functions`) and its issue/stall peers
> resolve to `.data.rel.ro`. To read their *bytes* off the file, subtract
> `0x200000` (file offset `0x207ecc0`). Applying the libtpu `0x400000` delta — or
> assuming VMA==fileoffset because `.text`/`.rodata` are — lands you in the wrong
> place and yields a spurious "table is garbage" finding. *(HIGH · OBSERVED.)*

---

## 7. Slice plan (what each later ISS page inherits)

This gateway owns the cross-cutting surface; the per-class pages take a function
budget out of the §4 taxonomy. Every slice reuses the `opnd_sem_<rf>_addr`
resolvers (regfile sizing), the 188-slot semantic/issue/stall dispatch tables, the
8-role scoreboard contract, and `dll_state_table` as the regfile-offset oracle.

| Downstream slice | Takes | Regfiles |
|---|---|---|
| Arithmetic (ALU/shift/select/move/reduce/rep) | `vec_alu` 38, `vec_shift` 20, `vec_rep` 20, `vec_select` 12, `vec_mov` 4, `vec_reduce` 6, `vec_specialized_seli` 24, `sqz` 4 + `vbool` rows | vec(32), vbool(16), b32_pr(16) |
| MAC / multiply | `multiply` 14, `spfma` 20+10, `wvec_pack` 6, `unpack_wvec_mov` 4, all `my_wvec_*` 23 | wvec(4), vec(32) |
| Load/store | `ld_st` 10 + `ld_st_vr2`, vbool/valign `ld_st` rows + the 52 memory `nx_*` ports | — |
| Gather/scatter (SuperGather) | `vec_scatter_gather` 8 (+2 vbool) + all `my_gvr_*` 10 + `nx_GS*` / `nx_ScatterData_0` | gvr(8) |
| Predicate / boolean | all `my_vbool_*` 78 + all `my_b32_pr_*` 11 + `my_BR_*` 11 + the boolean resolvers | vbool(16), b32_pr(16), BR(16) |
| Convert / FP | all `fp_sem` (`hp_fma` 24) + all `bbn_sem` (`vec_sprecip_rsqrt` 8) + `divide` 16 + the IVP_FS0..7 FPU state | — |
| Valign / shuffle | all `my_valign_*` 20 + `opnd_sem_valign_addr` (`uul/uus/valignr` roles) | valign(4) |

How these slices are *invoked* (the run half of dlopen/register/run) is on
[Runnable ISS Infrastructure](./runnable-iss-infra.md); the timing under each is
on [The cas Timing Model](./cas-timing-model.md); the value semantics are on
[The fiss Datapath Oracle](./fiss-datapath-oracle.md); and both halves are unified
in [The ISS as Executable Oracle](./iss-oracle-synthesis.md). The cross-validation
against the reference cores (`libcas-ref-core.so` / `libfiss-ref-base.so`) and the
appendix port catalog are referenced by title only — those Parts are not yet
written.

---

## 8. Open items / uncertainty

- **[LOW]** Exact `gvr` (512-bit?) and `wvec` (1536-bit?) widths — 8-entry and
  4-entry counts are OBSERVED; the widths must be read from `dll_state_table`
  strides by the MAC and gather/scatter pages.
- **[MED]** `dll_state_table` (4592 B) record format (16 B → 287 records, or
  mixed) — confirmed to exist and to be the regfile-offset oracle; layout decode
  deferred to the page that walks it.
- **[MED]** The 188-slot semantic/issue/stall row struct (name-ptr + fn-ptrs);
  138/188 populated by `R_X86_64_RELATIVE` relocs. Per-row field offsets are a
  follow-up for whichever page walks slots programmatically.
- **[HIGH]** Everything in §1–§5 (identity, the 24-accessor ABI, the no-values
  finding, regfile sizes, fault tables) is direct binary evidence.
