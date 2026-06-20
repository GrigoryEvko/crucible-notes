# cas/fiss SuperGather Semantics

> **What this page is.** The SuperGather is the Vision-Q7 IVP **indirection
> engine**: a two-phase, gather-register-staged vector gather (plus a single-phase
> scatter). This page documents how the ncore2gp ISS *models* it across **both**
> plugins — the **DECODE + TIMING** half in `libcas-core.so` and the **element
> VALUE** half in `libfiss-base.so` — and cross-checks that model against the
> device firmware's two-phase hardware gather. It is reimplementation-grade: every
> register count, latency, operand field, and value-staging offset is read off the
> binary by absolute address, tagged **confidence × provenance**.

The keystone for the whole Part is [libcas-core Surface + ISS Plugin
ABI](./cas-core-surface.md): the cycle-accurate simulator core
(`libcas-core.so` — BASE `libcas-Xtensa.so`, `VERS_1.1`, GCC 4.9.4, modeled core
= Cayman / Vision-Q7 IVP VLIW) computes **decode and timing but never an element
value**. It reaches into the host through **119 `nx_*_interface` TIE-port
callbacks**; the value math lives in the sibling `libfiss-base.so` (the 864-leaf
`module__` / 12,569 `slotfill__` value oracle). The cas plugin ABI is a flat C
`dll_*` accessor vocabulary — *not* a `getInterface` / `register_client` factory.
The SuperGather is one slice of that surface: **a gather-register file (`gvr`,
8 entries), a 5-/6-port `nx_GS*` host group, and a `vec_scatter_gather` semantic
class.** This page is that slice, decoded end to end.

```
CAS  = extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/libcas-core.so
       45,878,080 B · ELF64 x86-64 · NOT stripped
       sha256 7f1d86da52891b3c65533d394ace4902b101536fedb31dff7ed976dc40b1041a
FISS = extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/libfiss-base.so
       12,330,016 B · ELF64 x86-64 · NOT stripped · self-contained value oracle
       sha256 260b110cd59c76b090cbdeb4d5d90f5245be34792618c023ab963ce108d3cc94
```

Tooling: `nm`, `nm -D`, `objdump -d`, `readelf -SW` by absolute path. `.text` and
`.rodata` are VMA==file-offset; writable sections carry the ncore2gp **`0x200000`**
delta (confirm per-section with `readelf -SW` — see the keystone §6). Every address
below is a VMA into `.text` unless noted. Tags: **HIGH/MED/LOW** ×
**OBSERVED** (read off disasm/bytes) / **INFERRED** (derived) / **CARRIED**
(from a prior report, re-checked here).

---

## 1. The shape in one diagram — post → gr → drain

The SuperGather is **the same instruction pair** the device firmware issues, and
the cas/fiss ISS models it operand-for-operand. The canonical pair (from the
firmware indirection engine, [The Indirection Engine](../firmware/kernels/indirection-gather.md)):

```
  ivp_gatheran_2x32t  gr0, a8(base), v31(Voff), vb0(vbMask)   ; PHASE 1 "post"
  ivp_gatherdnx16     v3,  gr0                                ; PHASE 2 "drain"
```

The two-phase model, with the real cas symbols on each edge:

```
                 PHASE 1 "post"  (FLIX slot S0, unit LdSt)
                 IVP_GATHERAN* / MOVGATHERD
  base (AR) ──┐
  Voff (vec, 16x32b) ─┐    F0_F0_S0_LdSt_4_inst_IVP_GATHERANX8UT_issue @0x118aa90
  vbMask (vbool) ─┐   │      stages {base, 16 per-lane offsets, predicate}
                  ▼   ▼                       into ───────────────►  ┌─────────────┐
              addr[lane] = base + Voff[lane]                         │  gr0..gr7   │
              gated by per-lane predicate                            │  GVR file   │
                                                                     │ 8 x 512-bit │
                 PHASE 2 "drain" (FLIX slot S1, unit Ld)             └──────┬──────┘
                 IVP_GATHERD*                                               │
              F0_F0_S1_Ld_16_inst_IVP_GATHERDNX16_issue @0x71e6e0          │
              drains 16 gathered lanes  gr0 ──────────────────────────────►  v3 (vec)
```

`cas` owns the boxes' **latency and the gr→drain bypass interlock**; `fiss` owns
the **values flowing through the edges** (the `base + Voff[lane]` arithmetic, the
predicate mask, the drained datum). The seam between them is the `nx_GS*` host
port group at the EXECUTE stage. *(HIGH · OBSERVED — both issue symbols and their
addresses read off `nm`/`objdump`; firmware pairing per the firmware page and
[ISA Batch 19](../isa/ref/b19-scatter-gather.md).)*

> **NOTE — "SuperGather".** The mnemonic family is `ivp_sem_vec_scatter_gather`
> (the semantic class name baked into the `my_gvr_0_opnd_ivp_sem_vec_scatter_gather_*`
> accessor symbols). 24 distinct mnemonics ship; the firmware names them
> "indirection engine"; the ISA-side reference calls the batch "SuperGather". All
> three are the same indexed memory engine.

---

## 2. The gather register file (`gvr`) — gr0..gr7, 8 × 512-bit

The two-phase staging buffer is a dedicated architectural regfile, `gvr` ("global
vector register" / gather-scatter register), ctype `_TIE_xt_ivp32_xb_gsr`. Three
independent binary facts fix its geometry.

**(a) 8 entries — `opnd_sem_gvr_addr @0x17aa2b0`:**

```text
opnd_sem_gvr_addr @0x17aa2b0 :
  89 f0          mov   %esi,%eax       ; eax = operand field
  83 e0 07       and   $0x7,%eax       ; & 0x7  ->  index 0..7
  c3             ret
```

The operand field is masked `& 0x7`, i.e. **exactly 8 gather registers,
gr0..gr7**. The mask *is* the regfile size (the keystone's §4 convention).
*(HIGH · OBSERVED — three bytes, `83 e0 07`.)*

**(b) 512-bit stride — `my_gvr_commit_data @0x17a9ed0`:**

```text
my_gvr_commit_data @0x17a9ed0 :
  48 63 f6                movslq %esi,%rsi          ; rsi = gr index
  48 c1 e6 06             shl    $0x6,%rsi          ; x 0x40 = 64-byte stride
  48 8d 84 37 94 36 01 00 lea    0x13694(%rdi,%rsi,1),%rax  ; &state.gr[index]
  c3                      ret
```

The per-register stride is `1 << 6 = 0x40 = 64 bytes = **512 bits**`. The gather
register holds 16 lanes × 32 bit = 512 bit — one lane per gathered element.
*(HIGH · OBSERVED — `48 c1 e6 06` is the `shl $0x6`.)*

**(c) 64-byte copy — `my_gvr_set_stage_value @0x17a9a20`** copies the full
gather-register value with eight 8-byte moves (0x00..0x38), i.e. 64 bytes,
byte-exact. *(HIGH · OBSERVED.)*

> **GOTCHA — `gvr` is in the ISS but not the public `tie.h`.** The 8-entry
> SuperGather file is confirmed by `opnd_sem_gvr_addr` (`& 0x7`), the `gvr`
> regfile-name string in `.rodata`, and the `my_gvr_*` accessor bodies — even
> though the public TIE header omits it. The keystone left its *width* "LOW"
> pending a state-table read; **this page resolves it to 512-bit** from the
> `my_gvr_commit_data` `shl $0x6` stride and the `my_gvr_set_stage_value`
> 64-byte copy. *(HIGH · OBSERVED — width now upgraded from LOW to HIGH.)*

### 2.1 The `my_gvr_*` accessor surface (15, not 10)

The `gvr` scoreboard is exposed through **15 distinct `my_gvr_*` accessors**
(`nm libcas-core.so \| rg ' my_gvr_'`), partitioning by role:

| Role group | Accessors | Phase |
|---|---|---|
| gather-TARGET def (`gt`) | `my_gvr_0_..._gt_def`, `gt_set_def`, `gt_kill_def`, `gt_bitkill_def` | PHASE-1 post writes gr |
| gather-SOURCE use (`gs`) | `my_gvr_1_..._gs_use`, `gs_set_use` | PHASE-2 drain reads gr |
| address oracles | `my_gvr_def_addr`, `my_gvr_use_addr` | scoreboard ring lookups |
| staged-value / commit | `my_gvr_stage_value`, `my_gvr_set_stage_value`, `my_gvr_commit_value`, `my_gvr_set_commit_value`, `my_gvr_commit_data` | deferred-commit path |
| interlock | `my_gvr_stall` | gr->drain RAW bypass |

> **CORRECTION — "10 `my_gvr_*`" is the def/use-role subset, not the full
> surface.** SX-ISS-01 / SX-ISS-05 counted **10** by listing only the def/use
> semantic rows plus `commit_data`/`def_addr`/`use_addr`. A literal
> `nm libcas-core.so \| rg ' my_gvr_'` returns **15** distinct names — the extra 5
> are the `stage_value`/`commit_value`/`set_stage_value`/`set_commit_value` helpers
> of the deferred-commit path (the gr is filled at post, committed at drain). The
> roles are the same; the surface is 15. *(HIGH · OBSERVED — `nm` count.)*

The crucial **`gt`/`gs` split**: `gt` (gather-TARGET) is the gr **def** posted by
PHASE 1; `gs` (gather-SOURCE) is the gr **use** consumed by PHASE 2. This is the
RAW dependency the ISS scoreboards to keep the two phases ordered.

---

## 3. PHASE 1 "post" — operand decode (`libcas-core`)

Decoded byte-exact from `F0_F0_S0_LdSt_4_inst_IVP_GATHERANX8UT_issue @0x118aa90`
(the predicated unsigned-8 gather-address; identical operand grammar to the
firmware's `ivp_gatheran_2x32t`). Each operand is resolved by an
`opnd_sem_<rf>_addr` PLT call, then **posted to the scoreboard** through an
indirect def hook `call *%r12` with the latency in `%esi`:

```text
IVP_GATHERANX8UT_issue @0x118aa90 :         (rdi=state, rbp=instr-word ptr, r12=def-hook)
  118aa97:  mov  (%rsi),%esi
  118aaa6:  call opnd_sem_AR_addr@plt        ; OPERAND 1 base  -> AR regfile
  118aab4:  mov  $0x1,%esi                   ;   LAT 1  (address def-post)
  118aab9:  call *%r12                        ;   post AR def
  118aacf:  mov  %eax,%esi
  118aad1:  call opnd_sem_gvr_addr@plt       ; OPERAND 2 gr    -> gr0..gr7  *** GATHER REG ***
  118aadf:  mov  $0xa,%esi                   ;   LAT 10 (gr ready @ stage 10)
  118aae4:  call *%r12                        ;   post gvr (gt) def
  118aaf4:  mov  %eax,%esi
  118ab04:  call opnd_sem_vbool_addr@plt     ; OPERAND 3 vbMask -> vbool predicate  *** PER-LANE MASK ***
  118ab12:  mov  $0xa,%esi                   ;   LAT 10
  118ab17:  call *%r12
  118ab2d:  call opnd_sem_vec_addr@plt       ; OPERAND 4 Voff  -> vec reg  *** 16x32-bit OFFSETS ***
  118ab42:  mov  $0xa,%esi                   ;   LAT 10
```

So the post grammar is **`{base:AR, gr:gvr, vbMask:vbool, Voff:vec}`** — exactly
the firmware's `gatheran_2x32t gr0, a8(base), v31(Voff), vb0(vbMask)`,
operand-for-operand. *(HIGH · OBSERVED — the four `opnd_sem_*@plt` calls and the
four `mov $LAT,%esi` immediates are read directly off the disassembly at the
addresses shown.)*

- The **non-predicated** `GATHERANX8U` is the same minus the `vbool` operand:
  `AR(LAT 1) + gvr(LAT 0xa) + vec(LAT 0xa)`.
- `MOVGATHERD` (the gr-fill move) is `gvr(LAT 0xa) + vec(LAT 0xa) + b32_pr(LAT 0xc)`.

> **NOTE — why `gr` and `vbMask`/`Voff` all post at LAT 10.** The post is a single
> issue; all three vector-class operands become available at the EXECUTE stage
> (stage 10), where the host value handoff fires (§6). Only the scalar `AR` base
> posts early (LAT 1, an address def). The 12 logical stage functions per
> `(format, slot, mnemonic)` run stage0..stage11; **EXECUTE is stage 10**
> (`esi=$0xa`). *(HIGH · OBSERVED.)*

---

## 4. PHASE 2 "drain" — operand decode (`libcas-core`)

Decoded from `F0_F0_S1_Ld_16_inst_IVP_GATHERDNX16_issue @0x71e6e0`:

| Operand | Field decode | Regfile | Hook | LAT |
|---|---|---|---|---|
| 1 — gr src | `instr & 0x7` -> `opnd_sem_gvr_addr` | gr0..gr7 | gvr (gs) use | `0xa` (10) |
| 2 — v dst | `(instr<<0x13)>>0x1b` -> `opnd_sem_vec_addr` | vec | host-value | `0xb` (11) |

The drain reads gr (gather-SOURCE) and writes the destination vector. Its
destination retires at **LAT 11 — one cycle deeper than the post's LAT 10** — the
drained value materializes one stage after the gr is ready. The pair issues across
all FLIX formats (F0..F7) **plus the narrow `N2` format**
(`N2_N2_S1_Ld_16_inst_IVP_GATHERDNX16_issue`), so the drain is encodable in a
narrow bundle as well. *(HIGH · OBSERVED — issue symbol present in F0..F7 and N2.)*

> **QUIRK — drain is a *light* op.** Where the post (§3) and scatter (§8) carry a
> stack of structural hazards, the drain's only dependencies are the gr it reads
> and the coprocessor-enable. It is, in effect, "pull the already-staged 16 lanes
> out of the register." See the stall chain in §7. *(HIGH · OBSERVED.)*

### 4.1 FLIX slot map (where each phase issues)

| Group | Mnemonics | FLIX slot | Unit |
|---|---|---|---|
| PHASE-1 gather-address | all `GATHERAN*`, `MOVGATHERD` | S0 | LdSt (F1: LdStALU) |
| PHASE-2 gather-drain | `GATHERDNX16`, `GATHERDNX8S`, `GATHERD2NX8_H/_L` | S1 | Ld |
| Scatter (single-phase) | all `SCATTER*` | S0 | LdSt / LdStALU |

The two phases issue in **different FLIX slots on different LSU units** — post on
S0/LdSt, drain on S1/Ld — so a post and an unrelated drain can co-issue in one
bundle, but the *dependent* drain stalls on its gr (§7). *(HIGH · OBSERVED — the
slot/unit are encoded in every issue symbol's `S0_LdSt` / `S1_Ld` prefix.)*

---

## 5. The value semantics (`libfiss-base`) — `addr[lane] = base + Voff[lane]`

`libfiss-base.so` is the self-contained, in-process VALUE oracle: its only
undefined imports are libc/runtime (`memcpy`/`memset`, `__gmon_start__`, `_ITM_*`,
`_Jv_*`) — **zero `nx_*_interface` host callbacks**
(`nm -D libfiss-base.so \| rg ' U ' \| rg -c 'nx_.*_interface'` -> 0). Every
gather/scatter mnemonic has a `regload__` + `opcode__<m>__stage_10` triple; all 24
mnemonics present, identical to cas. *(HIGH · OBSERVED.)*

### 5.1 PHASE-1 value — `regload__ivp_gatheran_2x32t @0x2c20e0`

The operand READ. Byte-exact, this is the literal `addr[lane] = base + Voff[lane]`
machinery (per-lane offsets loaded into the staging area):

```text
regload__ivp_gatheran_2x32t @0x2c20e0 :        (rdi = op state, rsi = regfile vector)
  2c20e0:  mov 0x8(%rsi),%rcx              ; rcx = &AR regfile
  2c20e4:  mov 0xac(%rdi),%edx             ; edx = base AR index
  2c20ea:  mov (%rcx),%rax
  2c20ed:  mov (%rax,%rdx,4),%r8d          ; r8d = base ADDRESS  -> 0xb0(rdi)
  2c20f1:  mov 0xd0(%rdi),%eax             ; eax = Voff vec index
  2c20f7:  mov 0x10(%rcx),%rdx             ; rdx = &vec regfile
  2c20fb:  shl $0x4,%eax                   ; x 16  (16 lanes per vec reg)
  2c20fe:  mov %eax,%esi
  2c2100:  mov (%rdx,%rsi,4),%esi          ; lane 0 offset
  2c2103:  mov %esi,0xd4(%rdi)             ;   -> staging 0xd4
  2c2109:  lea 0x1(%rax),%esi
  2c210c:  mov (%rdx,%rsi,4),%esi          ; lane 1 offset
  2c210f:  mov %esi,0xd8(%rdi)             ;   -> staging 0xd8
  ...                                      ; lanes 2..15 (lea 0x2..0xf, +4 each)
                                           ;   16 x 4 = 64 B -> 0xd4..0x110(rdi)
  ; then vbMask: reg index 0x114(rdi), vbool regfile [0x18(rsi)] -> 0x118/0x11c(rdi)
```

The `shl $0x4` (×16) on the vec index is the **per-lane fan-out**: the offset
register is read as **16 consecutive 32-bit lane values** into `0xd4..0x110`
(`16 × 4 = 64` bytes). These are the **16 per-lane 32-bit offsets**; combined with
the base address in `0xb0`, the per-lane gather address is `addr[lane] = base +
Voff[lane]`. *(HIGH · OBSERVED — `shl $0x4` and the 16 unrolled `lea n(%rax)` /
`mov ->0xd4+4n(%rdi)` stores are byte-exact.)*

### 5.2 PHASE-1 value — `opcode__ivp_gatheran_2x32t__stage_10 @0x2c1c20`

The stage-10 EXECUTE for the post (size `0x43a`):

1. Copies the 16 Voff lanes to the gr-staging area `0x124..0x160(rdi)`.
2. **Expands the 16-bit vbMask into 16 per-lane masks** via **sixteen** calls to
   `module__xdref_bitkillf_32_4 @0x82d010`, one per mask bit:

   ```text
   module__xdref_bitkillf_32_4 @0x82d010 :    (esi = lane predicate bit, rdx = out)
     c1 e6 1f   shl $0x1f,%esi      ; bit0 -> MSB
     c1 fe 1f   sar $0x1f,%esi      ; arithmetic-shift-replicate  -> 0x0 or 0xFFFFFFFF
     89 32      mov %esi,(%rdx)
     c3         ret
   ```

   Each predicate bit becomes `0x00000000` (lane **KILLED** / masked / OOB) or
   `0xFFFFFFFF` (lane **ACTIVE**). This is the **per-lane in-bounds/active gate**
   that selects which lanes are gathered. *(HIGH · OBSERVED — `shl 0x1f ; sar 0x1f`
   is the canonical sign-replicate predicate expansion.)*
3. Tags the gr state word (`0x91 @0x164`) and stages the per-lane mask
   `0x168..0x18c`.

> The non-predicated `GATHERANX8U @0x2bf650` sets all 16 lane masks to `0xFFFFFFFF`
> (all lanes active) and zeroes the gr drain buffer `0x6c..0xa4` (pre-drain).
> *(HIGH · OBSERVED.)*

### 5.3 PHASE-2 value — the drain

- `regload__ivp_gatherdnx16 @0x2c2560`: gr src index `0x6c(rdi)`, `<<4`, gr regfile
  `[0x38(rsi)]` -> loads 16 gathered lanes into `0x70..0xac(rdi)`.
- `opcode__ivp_gatherdnx16__stage_10 @0x2c23f0` (size `0x14e`): unpacks the 16
  staged gathered lanes from `0x70..0xac` into the destination-vector writeback
  slots `0x2c..0x68(rdi)`, sets the per-lane writeback validity masks to
  `0xFFFFFFFF` (`0xb4..0xf0`), tags state `0x42 @0xb0`, writes the predicate at
  `0xf4`.

So PHASE-2 **drains the 16 gathered elements from gr into the destination vector
register**. Sub-width drains exist: `GATHERD2NX8_H/_L @0x2c3120/0x2c2c80` (double-
rate 8-bit, high/low halves), `GATHERDNX8S @0x2c2700` (nx8 signed),
`MOVGATHERD @0x2c3780` (gr->gr move). *(HIGH · OBSERVED.)*

### 5.4 Per-lane load / miss / bounds — where the memory read happens

`fiss` has **no host memory callback** (self-contained). The per-lane LOAD of
`table[addr[lane]]` is performed by the simulator's own memory model fed by the
gr-staged addresses; the gr holds `{base, per-lane offset, per-lane mask}`, and the
gathered datum per lane is produced for the drain.

> **GOTCHA — the masked lane keeps its slot, it is not re-read.** A
> predicate-0 lane (`bitkillf -> 0x0`) is **not gathered**; its drain slot retains
> the pre-zeroed / miss value. This is the ISS realisation of the firmware's
> "out-of-bounds lanes (mask bit 0) are not gathered." The **bound->predicate
> compare** (the firmware's `ivp_ltun`/`leun_2x32`) is computed **upstream** — the
> caller passes the already-computed `vbMask`; the gather op itself only *consumes*
> the predicate. **The mask expansion (`bitkillf` bytes) is OBSERVED; the binding
> that the un-gathered lane keeps the zeroed slot AS the gather index-miss fill is
> INFERRED** from the firmware's immediate-write / SKIP model. *(HIGH mask /
> MED · INFERRED miss-value binding.)*

---

## 6. The cas-TIMING / fiss-VALUE split — where the seam is

This is the whole point of two plugins. The same op is decoded twice with
disjoint responsibilities:

| Concern | `libcas-core.so` | `libfiss-base.so` |
|---|---|---|
| operand fields (which AR/gvr/vbool/vec) | **decodes** (§3, §4) | re-reads via `regload__` (§5) |
| gr scoreboard (gt def / gs use) | **owns** (`my_gvr_*`, §2.1) | — |
| per-op latency, FLIX co-issue, hazards | **owns** (§7) | — |
| `addr[lane] = base + Voff[lane]` | — | **computes** (§5.1) |
| per-lane predicate mask | declares the vbool operand | **expands** (`bitkillf`, §5.2) |
| the gathered datum / drain unpack | — | **computes** (§5.3) |
| EXECUTE handoff | `call *0x494a48` at stage10 (§6.1) | the in-process implementation of that call |

The cas core hands the actual address + data emission to the host at the EXECUTE
stage; `fiss` is the in-process implementation of that same value math when
`libfiss-base.so` serves as the value oracle.

### 6.1 The host handoff at stage 10

```text
GATHERAN_2X32T_inst_stage10 @0x11d7270 :
  - zeroes a 0x494900.. scratch
  - sets trace cursor: 0x4a09dc = slot, 0x4a09e0 = 0xa (stage10)
  - call *0x494a48(%rbx)      ; esi=$0xa  <- the host VALUE handoff
```

The shared helper `ivp_sem_vec_scatter_gather_opcode_stage10 @0x64f440` (a large
instruction-word field decoder) drives the per-lane GS port writes. *(HIGH ·
OBSERVED.)*

### 6.2 The SuperGather `nx_GS*` host ports

The host ports the gather/scatter path writes (`nm -D libcas-core.so \| rg ' U ' \|
rg 'nx_GS\|nx_ScatterData'`):

```
U nx_GSControl_0_interface     U nx_GSEnable_0_interface     U nx_GSVAddrOffset_0_interface
U nx_GSControl_1_interface     U nx_GSEnable_1_interface     U nx_ScatterData_0_interface
```

By **port wiring at the stages**: stage 1 (control setup) drives
`nx_GSControl_0`; stage 10 (EXECUTE) drives `nx_GSEnable_0` + `nx_GSVAddrOffset_0`
+ `nx_ScatterData_0`.

> **CORRECTION — "5 SuperGather ports" vs the binary's 6.** The keystone
> [cas-core-surface](./cas-core-surface.md) §3.1 buckets a **"SuperGather control"
> group of 5** (`nx_GSControl_{0,1}`, `nx_GSEnable_{0,1}`, `nx_GSVAddrOffset_0`)
> and files `nx_ScatterData_0` under the 52 *memory* ports. A literal
> `nm -D \| rg 'nx_GS\|nx_ScatterData'` returns **6** symbols, because
> `nx_ScatterData_0` is functionally part of the same indirection engine (it
> carries the per-lane *scatter store data*). So: **5 if you count only the
> `nx_GS*`-prefixed control/address ports; 6 if you include `nx_ScatterData_0`
> with the engine it serves.** Both framings are consistent with the binary; this
> page uses **6 = the full SuperGather port set** (the 5 `nx_GS*` plus
> `nx_ScatterData_0`). The total `nx_*_interface` import count is **119** either
> way (re-verified). *(HIGH · OBSERVED — the 6-symbol `nm -D` set is byte-exact;
> the 5-vs-6 difference is solely which bucket `nx_ScatterData_0` is filed under.)*

The dual `_0`/`_1` on `nx_GSControl`/`nx_GSEnable` mirrors the **2-lane LSU**
(`nx_Load_0`/`_1`, `nx_VAddr*_0`/`_1`) — two parallel vector memory pipes. Note
`nx_GSVAddrOffset` and `nx_ScatterData` are **`_0`-only** (single-pipe for the
address-offset and scatter-data carriers). *(HIGH · OBSERVED counts; MED ·
INFERRED two-pipe interpretation, per the keystone.)*

---

## 7. Timing — latencies, the gr interlock, stall chains (`libcas-core`)

### 7.1 Per-op latency (the `esi` posted to the def/use hook)

Read literally from the `mov $LAT,%esi` immediately before each operand's
def/use hook:

| Op | Operand | LAT | Role |
|---|---|---|---|
| PHASE-1 `gatheran*` | base / AR | `1` | address def-post |
| | gr (gvr) | `0xa` (10) | gather register ready @ stage 10 |
| | vbMask (vbool) | `0xa` (10) | predicate |
| | Voff (vec) | `0xa` (10) | per-lane offsets |
| PHASE-2 `gatherdnx16` | gr src (use) | `0x9` (9) in stall / `0xa` in issue | drain reads gr |
| | v dst | `0xb` (11) | drained value retires 1 later |
| `SCATTERNX8U` | AR | `1`; vbool/offsets/data all `0xa` | single-phase store |
| `MOVGATHERD` | gvr `0xa`, vec `0xa`, b32_pr | `0xc` (12) | gr-fill move |

Signed/unsigned/8/16-bit/predicated variants share the **same latency** — they
differ only by the host VALUE callback (the `fiss` primitive), never by cas
timing. *(HIGH · OBSERVED numbers; MED the 1-vs-10-vs-11 role labels.)*

### 7.2 The gr RAW interlock — `my_gvr_stall @0x17a9970`

Structurally identical to `my_vec_stall` but on the `gvr` lane:

```text
my_gvr_stall @0x17a9970 :
  edx = this op's gr reg#
  r9  = ring base (state+0x1389c)
  for ecx = 1..0xa  (10-stage gvr bypass window) over ring slot (ecx+r9)&0x1f, stride 4:
      valid  := 0x14aa4 / 0x14ba4 (ring)
      reg#   := cmp 0x149a4
      ready  := 0x14b24
      avail  := ready - ecx                     ; stage distance
      if (consumer_need esi vs avail) fails AND guard 0x148a4 set -> return 1  (STALL)
```

The drain (phase 2) **stalls until the gr posted by phase 1 has propagated far
enough to be forwardable** — a latency-aware bypass interlock between the two
phases over the 32-deep ring, window 10. *(HIGH · OBSERVED.)*

### 7.3 The stall chains (RAW + structural)

```text
PHASE-1 GATHERANX8UT_stall @0x11b3030 :
  AR base RAW (esi=1) ; vbMask RAW (esi=0xa) ; Voff RAW (esi=0xa) ;
  my_CPENABLE_stall(esi=3) ; my_REV8AR_stall ; my_WB_P_stall ; my_WB_N_stall
       (two of the 6 writeback ports — structural)

PHASE-2 GATHERDNX16_stall @0x73fb10 :
  gr (gvr) RAW (hook gvr, esi=0x9) ; my_CPENABLE_stall(esi=3)
       (minimal — the drain only waits on the gr + CP)

SCATTERNX8UT_stall @0x7f2320 :
  AR(1) ; vbMask(0xa) ; Voff(0xa) ; data(0xa) RAW ;
  my_CPENABLE_stall(3) ; my_REV8AR_stall ; my_WB_P/WB_N ; my_InOCDMode_stall ; my_MS_DISPST_stall
       (multi-slot dispatch)
```

The drain stall body, byte-exact, confirms gr use-LAT 9:

```text
73fb19:  mov  (%rsi),%esi
73fb25:  call opnd_sem_gvr_addr@plt
73fb33:  mov  $0x9,%esi              ; <- gr use-latency 9
73fb38:  call *%rbp                   ; gr RAW stall hook
73fb43:  mov  $0x3,%esi
73fb4b:  call my_CPENABLE_stall@plt  ; coprocessor-enable
```

Gather-post + scatter model the writeback-PORT structural hazard
(`WB_P`/`WB_N`), coprocessor-enable, and (scatter) multi-slot dispatch; the
phase-2 drain is light. *(HIGH · OBSERVED — all callees and `esi` values read off
disasm.)*

### 7.4 Faults and debug

| Symbol | Addr | Meaning |
|---|---|---|
| `LoadStoreSGAccErrorException_exc` | `0x1783190` | SuperGather access error |
| `ImpreciseSGFPVecException_exc` | `0x177cad0` | imprecise SG FP-vector exception |
| `my_DBREAKC_SG0_*` / `_SG1_*` | — | two SuperGather data-breakpoint registers (full def/use/commit/stall role set) |

The gather path has its own access-error + imprecise-FP fault model and two
dedicated data-breakpoint registers. *(HIGH · OBSERVED.)*

---

## 8. The scatter path — single-phase, no gr

Scatter is **single-phase**: the op posts the AR base, the per-lane vec offsets,
the per-lane vec data, and (for `*T` forms) the `vbMask` predicate **directly to
the store port** — there is **no gr def/use in any scatter issue body**. Verified:
`F1_F1_S0_LdStALU_4_inst_IVP_SCATTERNX8UT_issue @0x7abc70` calls
`opnd_sem_AR_addr` + `opnd_sem_vbool_addr` (×2) + `opnd_sem_vec_addr` (×3) — and
**never `opnd_sem_gvr_addr`**. *(HIGH · OBSERVED — the operand-resolver call set
contains no gvr.)*

| Scatter form | Behavior |
|---|---|
| `SCATTERNX8U`/`NX16`/`N_2X32` (+`T`) | `data[lane] -> base+Voff[lane]`, predicate-gated for `*T` |
| `SCATTERINCNX16`/`T` | **scatter-INCREMENT** (scatter-ADD): `table[base+Voff[lane]] += value` — the histogram / reduce-add form |
| `SCATTER2NX8_H/_L` (+`T`) | double-rate 8-bit scatter, high/low halves |
| `SCATTERW` | scatter-word — the **only** op with a separate completion hook |

- **Scatter value packing**: `opcode__ivp_scatternx8u__stage_10 @0x2c3c70` makes
  **32** calls to `module__xdref_replo8_16_16 @0x82d020` (`movzbl ; shl $8 ; or` —
  pack the low-8 scatter datum into 32 lanes). *(HIGH · OBSERVED.)*
- **Scatter-add**: `opcode__ivp_scatterincnx16__stage_10 @0x2c8a50` has **no
  sub-calls** — an inline per-lane increment (the scatter-reduce / histogram form).
  It ties to the firmware's DGE-compute ADD / embedding-update ADD scatter-reduce
  model. *(HIGH · OBSERVED that the body is a self-contained inc; MED · INFERRED
  that the ISS inc == the firmware scatter-add reduce — name + body inference.)*
- **`SCATTERW`**: `opcode_complete__ivp_scatterw @0x2c8a40` is the **only**
  gather/scatter op with a separate `opcode_complete__` completion hook (all other
  completion hooks belong to scalar base-ISA ops). `scatterw` is the one
  completion-deferred / multi-cycle scatter. *(HIGH · OBSERVED — single occurrence
  among the ~214 `opcode_complete__` symbols.)*

Scatter ADDRESS emission to the host goes via `nx_GSVAddrOffset_0` +
`nx_ScatterData_0` under the `nx_GSEnable` gate; structural hazards add
`my_InOCDMode_stall` + `my_MS_DISPST_stall` (multi-slot dispatch) on top of the
gather-post set. *(HIGH · OBSERVED.)*

---

## 9. Cross-check against the device hardware gather

The cas/fiss model is not invented — it **reconciles operand-for-operand** with
the device firmware's two-phase hardware gather (the indirection engine), and with
the ISA reference batch:

| Hardware / firmware fact | cas/fiss ISS evidence (this page) | Tag |
|---|---|---|
| `ivp_gatheran_2x32t gr0,base,Voff,vbMask` (post) | `IVP_GATHERANX8UT_issue`: `AR(base)+gvr(gr)+vbool(vbMask)+vec(Voff)`; `regload` reads exactly these 4 | HIGH · OBSERVED |
| `gr0` = gather register / FIFO | `gvr` file gr0..gr7, 8x512-bit, gt def / gs use | HIGH · OBSERVED |
| `ivp_gatherdnx16 v3, gr0` (drain nx16) | `IVP_GATHERDNX16_issue` (Ld S1): gvr src -> vec dst; fiss drains 16 lanes | HIGH · OBSERVED |
| index dtype u8/16/32; lanes `*_2x32` | mnemonics span `8U`/`NX16`/`2X32`; offsets are 16x32-bit | HIGH · OBSERVED |
| predicate `vb` gates the gather (OOB lanes not gathered) | `vbMask` -> 16x `bitkillf_32_4` -> per-lane `0x0`/`0xFFFFFFFF`; masked lane keeps slot | HIGH mask / MED miss-binding |
| bounds via `ivp_ltun`/`leun_2x32` | compare is **upstream** (caller supplies `vbMask`); gather op consumes it | HIGH · INFERRED locus |
| scatter / scatter-add (DGE ADD, embedding ADD) | `SCATTERINCNX16(T)` = scatter-add; `SCATTER*` single-phase | HIGH struct / MED reduce-binding |
| HW post->drain structure | `cas` gt/gs gvr ring (def @ post, use @ drain) | HIGH · OBSERVED |

The bit-precise per-opcode reference compute for this group is in
[Formal Semantics I — Gather](../isa/semantics/group-semantics-i.md); the indexed
memory engine's ISA encoding is in [ISA Batch 19](../isa/ref/b19-scatter-gather.md);
the device-side indirection kernels are in
[The Indirection Engine](../firmware/kernels/indirection-gather.md). The
cross-validation against the *reference* ISS cores (`libcas-ref-core.so` /
`libfiss-ref-base.so`, report VAL-06) is a later Part and is named here in prose
only. *(HIGH · OBSERVED structural identity.)*

---

## 10. Reimplementation checklist

To rebuild the SuperGather engine compatibly, you need **both halves**:

1. **A `gvr` file** of 8 entries × 512 bit (16 × 32-bit lanes). Index = operand
   field `& 0x7`; per-register stride 64 B.
2. **A two-phase scoreboard** with a `gt` (def-at-post) / `gs` (use-at-drain)
   split over a 32-deep ring. The post writes gr at LAT 10; the drain reads at
   use-LAT 9 and retires at LAT 11; the `gr` RAW interlock window is 10 stages.
3. **A post stage** that stages `{base = AR, 16×32-bit Voff = vec, vbMask = vbool}`
   into the gr and expands the 16-bit predicate to 16 lane masks
   (`0x0`/`0xFFFFFFFF`).
4. **A per-lane value model**: `addr[lane] = base + Voff[lane]`; gather
   `table[addr[lane]]` for active lanes; masked lanes keep their (zeroed/miss)
   slot.
5. **A drain stage** (Ld slot S1) that unpacks 16 staged lanes into a destination
   vector, plus the sub-width drains (`NX8S`, `2NX8_H/_L`).
6. **A single-phase scatter** (no gr): plain store, `SCATTERINC` (scatter-add),
   `SCATTER2NX8` (double-rate 8-bit), and `SCATTERW` (the one completion-deferred
   op).
7. **The host seam**: at EXECUTE (stage 10) emit through the SuperGather ports
   `nx_GSControl_{0,1}` (stage 1), `nx_GSEnable_{0,1}` + `nx_GSVAddrOffset_0` +
   `nx_ScatterData_0` (stage 10). Two LSU pipes (`_0`/`_1`).
8. **Faults**: a SuperGather access-error (`LoadStoreSGAccErrorException`) and an
   imprecise-FP-vector exception (`ImpreciseSGFPVecException`); two SG data
   breakpoints.

---

## 11. Honesty ledger

**HIGH · OBSERVED** — gr0..gr7 (`opnd_sem_gvr_addr` `& 0x7`); 512-bit stride
(`my_gvr_commit_data` `shl $0x6`, 64-byte copy); post issue `@0x118aa90` operand
grammar `AR(1)+gvr(0xa)+vbool(0xa)+vec(0xa)`; drain issue `@0x71e6e0` `gvr(0xa)->vec(0xb)`,
drain use-LAT 9; the 24 distinct mnemonics; fiss `regload__ivp_gatheran_2x32t`
`shl $0x4` ×16-lane offset load; `bitkillf_32_4` predicate expansion
(`shl 0x1f ; sar 0x1f`); scatter is single-phase (no gvr operand);
`replo8_16_16` ×32 scatter pack; `SCATTERW` the only `opcode_complete`; SG faults
+ DBREAKC_SG breakpoints; **6** `nx_GS*`/`nx_ScatterData_0` ports; 119 total
`nx_*_interface`; fiss has 0 host callbacks; both binaries sha256-verified, NOT
stripped.

**MED · INFERRED** — the un-gathered (predicate-0) lane retaining the zeroed slot
*as* the gather index-miss fill (zero-init OBSERVED; the miss-fill binding
INFERRED); the bound->predicate compare being upstream of the gather op; `SCATTERINC`
== the firmware scatter-ADD reduce (inline inc OBSERVED, the reduce binding
INFERRED); the 1-vs-10-vs-11 latency role *labels* (numbers HIGH).

**LOW / NOT CLAIMED** — the exact instruction-word bit layout decoded by
`ivp_sem_vec_scatter_gather_opcode_stage10` (structurally characterised, not
field-by-field); whether `scatterw`'s `opcode_complete` models a specific cycle
count (presence OBSERVED; the cycle value not traced).

**Corrections issued on this page:** (1) the full SuperGather port set is **6** by
`nm` presence, not 5 — the difference is whether `nx_ScatterData_0` is filed with
the engine or with the 52 memory ports; (2) **15** distinct `my_gvr_*` accessors,
not 10 — the prior count was the def/use-role subset; (3) `gvr` width upgraded
from the keystone's LOW to **HIGH/OBSERVED 512-bit** via the `shl $0x6` stride.
