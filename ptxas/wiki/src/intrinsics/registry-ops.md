# Registry-Only & Special-Op Lowering

> *Lowering observed by feeding PTX to ptxas v13.1 (CUDA 13.1.115) at the lowest `-arch` that accepts each form (sm_89 for the classic ops, sm_90a / sm_100 for the cluster ops) and disassembling with nvdisasm. Other versions will differ.*

This page documents the SASS lowering of the PTX instructions that the
[MercConverter dispatch table](../codegen/isel.md) routes to a handler but which
are otherwise undocumented: the predicate/select ops, the FP class test, the
bit-manipulation primitives, the address-space queries, the cache/scheduler
hints, the stack/frame ops, and the cluster intrinsics. For each: the SASS
op(s) emitted (or *pseudo / no-op / barrier-only / driver-handled*), a one-line
semantics, and the SM floor.

## Predicate, select, and class-test ops

| PTX | SASS | Semantics | Floor |
|---|---|---|---|
| `set.<cmp>.f32.s` | single native `FSET.BF.<cmp>` (float dest = `1.0f`/`0.0`) | compare a,b, write float result; `.BF` = boolean-float result mode | sm_50+ |
| `set.<cmp>.u32.s` | `ISETP.<cmp>` + `SEL Rd, RZ, 0xffffffff, !P` (int dest = all-ones / 0) | compare a,b, write integer mask result | sm_50+ |
| `set.<cmp>.<boolop>` | one fused `ISETP.<cmp>.{AND,OR,XOR}` (predicate `q` folds into the third predicate input) + `SEL` | compare then combine with predicate `q` | sm_50+ |
| `slct.d.s32` | `ISETP.GE …, RZ` + predicated `SEL`/load | `d = (c ≥ 0) ? a : b` (select on **sign of c**) | sm_50+ |
| `slct.d.f32` | `ISETP.GE …, RZ` + `SEL` | same, f32 selector compared `≥ 0` | sm_50+ |
| `selp.b32` | `SEL Rd, Ra, Rb, P` | predicate-driven select (consumes an existing predicate) | sm_50+ |
| `cnot.b32` | `ISETP.EQ.U32 …, RZ` + `SEL` + `IMAD.MOV R, RZ, RZ, -R` | C-logical not: `d = (a == 0) ? 1 : 0` | sm_50+ |
| `testp.<class>.f32` | `LOP3.LUT …, 0x7fffffff` (clear sign) + 1–3 `ISETP` against IEEE bit patterns + `SEL`/predicate | FP class test {finite, infinite, number, notanumber, normal, subnormal} → predicate | sm_50+ |

`set` is the data-register sibling of `setp`: it runs the same `*SETP` compare
and then expands the predicate to a full-width data value with `SEL`. The
boolean-secondary form (`set.lt.and`) does **not** emit a second op — the
predicate operand folds into the `ISETP`'s third predicate input
(`ISETP.LT.AND P, PT, a, b, q`). `testp` is a small open-coded sequence (mask off
the sign bit, then compare the magnitude against `+INF` / smallest-normal /
zero), never a single op.

Example — `cnot.b32`:

```
ISETP.EQ.U32.AND P0, PT, Ra, RZ, PT     ; a == 0 ?
SEL  R0, RZ, 0xffffffff, !P0
IMAD.MOV R, RZ, RZ, -R0                  ; -> 0/1
```

Example — `testp.finite.f32`:

```
LOP3.LUT R, Ra, 0x7fffffff, RZ, 0xc0, !PT     ; |x| bits
ISETP.GE.U32.AND P0, PT, R, 0x7f800000, PT     ; |x| >= +INF ?
SEL R, RZ, 0x1, P0                              ; finite = NOT(>=INF)
```

## Bit-manipulation primitives

| PTX | SASS | Semantics | Floor |
|---|---|---|---|
| `bmsk.{clamp,wrap}.b32` | `BMSK Rd, Ra, Rb` | mask of `b` ones starting at bit `a` | sm_75+ (native; rejected below) |
| `szext.{clamp,wrap}.s32` | `SGXT Rd, Ra, Rb` | sign/zero-extend the low field of `a` from bit `b` | sm_75+ (native; rejected below) |
| `fns.b32` | multi-op: `BREV` + `POPC`/`FLO` + `ISETP`/`SHF`/`PLOP3` search | find the n-th set bit (from a base, with direction) | sm_75+ accepts; open-coded |

`bmsk` and `szext` are 1:1 native ops on every architecture ptxas still targets;
there is no software fallback (the bare op is required). `fns` accepts but
expands into a leading-one / population-count search sequence rather than a
single op:

```
BREV  R4, Ra
... PLOP3 / ISETP base+direction handling ...
IADD3 R5, -R5, 0x1f, RZ        ; convert FLO index to bit position
SHF.L.U32 R7, 0x1, R5, RZ      ; produce the result mask/index
```

## Address-space & type queries

| PTX | SASS | Semantics | Floor |
|---|---|---|---|
| `isspacep.{global,shared,local,const}` | `QSPC.E.{G,…} P, RZ, [Ra]` + `UISETP`/`PLOP3`/`SEL` window-range compose | test whether a generic pointer is in a state space → predicate | sm_75+ (native `QSPC`) |
| `isspacep.shared::cluster` | `QSPC.E.D P0, RZ, [Ra]` + `SEL` | test pointer is in the distributed (cluster) shared window | sm_90+ |
| `istypep.{texref,samplerref,surfref}` | `LOP3.LUT …, <typemask>` + `ISETP.NE` + `SEL` | test an opaque handle's type → predicate (bit-test on the driver handle layout) | sm_50+ |

`isspacep` queries the hardware state-space window with `QSPC` (query-space) and
then composes the window membership with uniform-predicate logic; `istypep` is a
pure bit-test against the handle-descriptor type field — no query op.

## Cache & scheduler hints

| PTX | SASS | Semantics | Floor |
|---|---|---|---|
| `prefetch.global.L2` | `CCTL.E.PF2 [Ra]` | prefetch line into L2 | sm_50+ |
| `prefetch.global.L1` | `CCTL.E.PF1 [Ra]` | prefetch line into L1 | sm_50+ |
| `prefetchu.L1` | `CCTL.E.PF1 [Ra]` | prefetch into the uniform/constant path | sm_50+ |
| `applypriority.global.L2::evict_normal` | `CCTL.E.DML2 [Ra]` | demote a cache line to a lower L2 priority | sm_80+ |
| `discard.global.L2` | `CCTL.E.RML2 [Ra]` | remove (discard) a cache line from L2 | sm_80+ |
| `griddepcontrol.launch_dependents` | **no SASS emitted** (pure scheduler hint, consumed by the launch path) | release dependent grids early | sm_90+ |
| `griddepcontrol.wait` | `ACQBULK` | wait until prerequisite grids have signalled | sm_90+ |

The prefetch / priority / discard family are all `CCTL` (cache-control) sub-modes
selected by a suffix (`PF1`/`PF2`/`DML2`/`RML2`). `griddepcontrol.launch_dependents`
is the notable *no-op* — it produces **zero** SASS instructions; it is a hint to
the programmatic-dependent-launch machinery, not a datapath op.

## Stack / frame ops

These manipulate the stack pointer **R1** directly; there is no dedicated SASS
opcode for any of them.

| PTX | SASS | Semantics | Floor |
|---|---|---|---|
| `stacksave.u32 d` | `MOV Rd, R1` | read the current stack pointer | sm_50+ |
| `alloca.u32 ptr, size, align` | `IMAD.IADD R1, R1, 1, -Rsize` + `LOP3.LUT R1, R1, 0xfffffff0, RZ, 0xc0` (align-down) + `MOV Rptr, R1` | grow the per-thread stack frame, return new pointer | sm_50+ |
| `stackrestore.u32 d` | `MOV R1, Rd` | restore the stack pointer to a saved value | sm_50+ |

`alloca` subtracts the (aligned) size from R1 and returns the new R1 as the
frame pointer; the `align` immediate becomes the `LOP3` mask
(`0xfffffff0` for 16-byte alignment). The returned pointer is a `.local`
window offset.

## Cluster intrinsics (sm_90+)

| PTX | SASS | Semantics | Floor |
|---|---|---|---|
| `mapa.shared::cluster.u64 d, a, rank` | `UPRMT` (insert target CTA-id into the address bits) | map a shared address to peer CTA `rank`'s window | sm_90+ |
| `getctarank.shared::cluster.u32 d, a` | `SHF.L.U64.HI` (extract the CTA-rank field of a cluster address) | rank of the CTA owning a cluster shared address | sm_90+ |
| `clusterlaunchcontrol.try_cancel … [mbar], [out]` | `ELECT` + `UGETNEXTWORKID.SELFCAST [out], [mbar]` (warp-leader-gated) | attempt to cancel/claim the next cluster work item, posting to an mbarrier | sm_100+ |
| `st.bulk.weak [addr], size, val` | `UMEMSETS.64 [UR], URZ, UR<size/8>` | bulk-initialize a shared region to `val` (uniform memset) | sm_100+ |

`mapa` rewrites the CTA-id portion of a distributed-shared address via a uniform
byte-permute (`UPRMT`); `getctarank` reads that same field back out with a funnel
shift. `clusterlaunchcontrol.try_cancel` elects a warp leader and runs the
cluster work-distribution op `UGETNEXTWORKID`. `st.bulk` is the uniform shared
memset (`UMEMSETS`), the same SASS family as MercConverter's `MEMSET` opcode.

## Trap, breakpoint, sleep, perf-event

| PTX | SASS | Semantics | Floor |
|---|---|---|---|
| `trap` | `BPT.TRAP 0x1` | abort the kernel / enter the trap handler | sm_50+ |
| `brkpt` | `BPT.TRAP 0x1` | debugger breakpoint (suspend the warp) | sm_50+ |
| `nanosleep.u32 t` | `NANOSLEEP Rt` | suspend the warp for ~`t` nanoseconds | sm_70+ (native; no pre-Volta path) |
| `pmevent N` | `PMTRIG 0x<1<<N>` | fire performance-monitor trigger N (index → one-hot mask) | sm_50+ |
| `pmevent.mask M` | `PMTRIG 0x<M>` | fire the set of triggers in mask M | sm_50+ |

`trap` and `brkpt` lower to the **same** `BPT.TRAP` opcode (distinguished only by
an internal mode subfield, not visible in the disassembly). `pmevent` converts
its single index into a one-hot trigger mask (`1 << N`); `pmevent.mask` passes the
mask through unchanged to `PMTRIG`.

## Cross-References

- [Instruction Selection](../codegen/isel.md) — MercConverter dispatch IDs for `BMSK`(4)/`SGXT`(5)/`PRMT`(24)/`QSPC`(106)/`CCTL`(108)/`BPT`(75)/`NANOSLEEP`(81)/`MEMSET`(276) and the `word_22B4B60` encoding-slot table
- [PTX-to-Ori Lowering](../pipeline/ptx-to-ori.md) — `set`/`slct`/`cnot` reduce to `ISETP`/`SEL`; the `LOP3` truth-table conventions
- [Convert (`cvt`) Lowering](cvt-lowering.md) and [SIMD Video & Dot-Product Lowering](simd-video-dp.md) — the other two undocumented families
- [OCG Intrinsic System](ocg.md) — the SM100+ OCG builtin path (cluster barriers, TMA, fences)
