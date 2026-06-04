# MxuOpHoldIssues Stall Recurrence

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

When two MXU operations issue back-to-back on the same systolic array, the second cannot start until the sub-units it needs are free — a classic reservation-station structural hazard. libtpu prices this with a three-function model whose top is `MxuLatencyTable::GetLatencyBetween(A, B)`: it computes the cycles `B` must wait after `A` issues, as the **maximum**, over the MXU sub-units `B` holds, of how long `A` still occupies each one. This is the MXU-internal analog of a `SchedModel`'s `WriteRes`/`ReadAdvance` reservation, but at the granularity of named MXU resources (`kMatmulAccumulationPipe`, `kMsrAOverrunCheck0..3`, `kFusedLoadReserved`, `kLmrReserved`) rather than abstract proc-resources.

The recurrence is a max-reduction, not a serial sum. Two MXU ops that touch disjoint sub-units pipeline freely (stall 0); two that contend share at the worst-contended port. The model is built from three pieces: `MxuOpResourceReservations(A)` returns `A`'s per-resource hold-cycle vector (`array<int,N>`, N=19 VF / 11 GL); `MxuOpHoldIssues(B)` returns the *set* of sub-units `B` holds (a `flat_hash_set<MxuResource>`, the "issue footprint"); and `GetLatencyBetween` iterates `B`'s held set, reads `A`'s reservation for each, and keeps the max. This page reconstructs all three, with the byte-exact reduction loop and bounds checks for both generations.

Two gates determine *when* this matrix is consulted. The per-edge structural gate lives in `LatencyTableViperfish/Ghostlite::LatencyBetweenInternal`: the MXU reservation is priced precisely for an MXU-MXU op pair that is **not** a true data dependency — true RAW dependencies use the plain op latency; non-MXU pairs use the XLU conflict-penalty table. The second is a compilation-environment flag, `MxuLatencyBalancingUseSequenceDependencies`, that switches the MXU-sequence assigner (`AssignMxusForSequenceGroup`) between the latency-balanced path and a simpler ordering. Both are documented here.

For reimplementation, the contract is:

- The three-function split: `MxuOpResourceReservations` (hold-cycle vector), `MxuOpHoldIssues` (held-resource set), `GetLatencyBetween` (max-over-shared-resource).
- The closed-form stall recurrence — the same-MXU guard, the vlxmr→matmul availability seed (and the matmul→matres cost-map bypass), the max-reduction loop, and the per-gen resource-count bound (≤18 VF / ≤10 GL).
- The per-edge structural gate (`IsTrueDependencyBetween` + `LloOpcodeUsesMxu`) that decides whether the matrix fires.
- The `MxuLatencyBalancing` env-flag gate and the assigner path it selects.

| | |
|---|---|
| **The recurrence** | `MxuLatencyTable::GetLatencyBetween(A,B)` — VF `@0x1c8ae320`, GL `@0x1c8b71e0` |
| **A's hold-cycle vector** | `MxuOpResourceReservations(A)` — VF `@0x1c8ad080`, GL `@0x1c8b6300` (`array<int,N>`) |
| **B's issue footprint** | `MxuOpHoldIssues(B)` — VF `@0x1c8ad3a0`, GL `@0x1c8b6760` (`flat_hash_set<MxuResource>`) |
| **Resource bound** | `resource < kNumMxuResources` → VF ≤ 18 (19 res), GL ≤ 10 (11 res); else `LogMessageFatal` |
| **Reduction** | `stall = max(seed, A.reservation[k])` over `k ∈ B.held_set` (`cmovg`) |
| **Per-edge gate** | `LatencyTableViperfish::LatencyBetweenInternal` `@0x1c8a4ac0` (GL `@0x1c8b22e0`) |
| **True-dep test** | `IsTrueDependencyBetween` `@0x1c89fdc0` — `FindOperandIndexFull ≥ 0` or both opcode `0x14e` |
| **MXU-op test** | `LloOpcodeUsesMxu` `@0x10a433e0` — op ∈ `[0x8d,0xa5] ∪ [0xa8,0xab] ∪ [0x152,0x153]` |
| **Env-flag gate** | `MxuLatencyBalancingUseSequenceDependencies` `@0x1d6b9c80` — env `+0xbe8`, `(~v & 0x101)==0` |

---

## The Issue→Stall Recurrence

### Purpose

`GetLatencyBetween(A, B)` returns the structural stall the scheduler must insert between two MXU operations issuing on the same MXU. `A` is the earlier (producer) op, `B` the later op. The result is the number of cycles `B` waits before it can re-use the shared MXU sub-units `A` still holds. This is *not* `A`'s result latency (that is `GetLatency`, applied on a true-dependency edge); it is the structural hazard from sharing the systolic-array ports.

### Entry Point

```text
LatencyTableViperfish::LatencyBetweenInternal  (@0x1c8a4ac0)   ── scheduler edge-latency
  └─ [MXU-MXU non-true-dep edge]
       MxuLatencyTable::GetLatencyBetween(A, B)  (@0x1c8ae320)  ── the recurrence
         ├─ MxuOpResourceReservations(A)  (@0x1c8ad080)         ── A's array<int,N> hold vector
         └─ MxuOpHoldIssues(B)            (@0x1c8ad3a0)          ── B's held-resource set
```

### Algorithm

```c
function GetLatencyBetween(A, B):                  // VF @0x1c8ae320 / GL @0x1c8b71e0
    // same-MXU guard: unit_id from LloInstruction+0xb (bits 8-9 = mxu id, bit 0x400 = has-mxu)
    idA = (A[+0xb]&0x400) ? ((A[+0xb]>>8)&3 | 0x100000000) : 0
    idB = (B[+0xb]&0x400) ? ((B[+0xb]>>8)&3 | 0x100000000) : 0
    if !(idA.has_mxu && idB.has_mxu):
        if idA.has_mxu != idB.has_mxu: return 0     // one has no MXU → no structural conflict
    else if idA != idB:                             // distinct MXU instances → no conflict
        return 0

    seed = 0
    // availability seed: a matrix-load A (vlxmr/MSR, opcode 0xa9) feeding a matmul B
    if A.opcode == 0xa9 && (B.opcode - 0x9b) < 0xb:  seed = 1   // A=0xa9 (vlxmr), B in [0x9b,0xa5]
    else if A.opcode in [0x9b,0xa5] && B.opcode == 0x152:       // matmul A, matres B
        return MatmulDataFormat-keyed cost-map[A] (this+0x80)   // direct map lookup, not a seed
    // (every other pair: seed = 0, fall through to the max-reduction below)

    resA   = MxuOpResourceReservations(A)           // A's array<int,N> hold-cycle vector
    heldB  = MxuOpHoldIssues(B)                      // set of MxuResources B holds
    stall  = seed
    for k in heldB:                                  // iterate B's held-resource set
        CHECK(k < N)                                 // VF: k<=18 ; GL: k<=10 ; else LogMessageFatal
        if stall < resA[k]:                          // cmovg
            stall = resA[k]                          // MAX over shared resource
    return stall
```

The verified VF reduction loop (`@0x1c8ae4c9`): `movzx edi,[rdx]` (next held resource `k`), `cmp rdi,0x12 ; ja FATAL` (`k ≤ 18`), `mov eax,[rbp+rdi*4-0xa0]` (`A`'s reservation array at `rbp-0xa0`), `cmp r12d,eax ; cmovg eax,r12d` (`stall = max(stall, resA[k])`). The GL loop (`@0x1c8b73a0`) is identical with bound `cmp rdi,0xa` (`k ≤ 10`) and the array at `rbp-0x6c`. The CHECK string is `resource < to_underlying(MxuResource::kNumMxuResources)` (VF `mxu_latency_table_vf.cc:536`, GL `mxu_latency_table_gl.cc:431`).

> **QUIRK —** the reduction is a MAX, not a SUM. A naive implementation that adds the contended holds will over-predict the stall: independent MXU sub-units pipeline. The model serializes only on the single worst-contended sub-unit. This mirrors the higher-level `ResourceVector::MaxResourceCycles` plain-max group, but at MXU-internal sub-resource granularity.

### MxuOpResourceReservations — A's hold-cycle vector

`MxuOpResourceReservations(A) @0x1c8ad080` returns, for each `MxuResource`, how many cycles `A` occupies it (`array<int,19>` VF / `array<int,11>` GL). It is a per-opcode dispatch that builds a modifier key and copies the matching `array<int,N>` out of a per-class flat-hash map:

```c
function MxuOpResourceReservations(A):              // VF @0x1c8ad080
    switch on A.opcode:
        [0x8d,0x96] matpush → MatpushModifier key → find in map(this+0x00) → copy array
        [0x9b,0xa5] matmul  → MatmulModifier  key → find in map(this+0x20) → copy array
        0xa9 (vlxmr/MSR)    → key from .rodata     → find in map(this+0x40)
        0xaa (vlxmr)        → key from .rodata     → find in map(this+0x40)
        0x152 (matres)      → chunk-index key      → find in map(this+0x60)
    // map miss → ThrowStdOutOfRange / LogMessageFatal (a valid MXU op always hits)
```

The `MatmulModifier` key is built byte-exactly from the instruction fields: `{byte0 = matmul_data_format(), byte1 = (opcode==0xa5), byte2 = (done_with_gains_mode()!=2), byte3 = 0, word4 = matrix_staging_register()|0x100}`. The `MatpushModifier` key is `{byte0 = GainLatchModeToMatmulDataFormat(latch_mode()), byte1 = LatchModeIsTranspose(latch_mode()), byte2 = 0, byte3 = LatchInstructionToMsr(instr)}`. See [MXU Latency: GF](mxu-latency-gf.md) for the per-format reservation values these maps hold.

### MxuOpHoldIssues — B's issue footprint

`MxuOpHoldIssues(B) @0x1c8ad3a0` returns the *set* of MXU sub-units `B` locks at issue (a `flat_hash_set<MxuResource>`). Same per-opcode dispatch, with one extra wrinkle for matpush: it branches on `latch_index_in_sequence()` (0..3) to pick the per-sequence-step held set. The three opcode ranges and the held set each builds:

```c
function MxuOpHoldIssues(B):                         // VF @0x1c8ad3a0
    if (B.opcode - 0x9b) <= 0xA:                     // matmul [0x9b,0xa5]
        seed_set = { from .rodata @0xb43b344 }       // kFusedLoadReserved-class seed
        if done_with_gains_mode(B) != 2:
            insert MsrReservedForLgmr(matrix_staging_register(B))    // kLmrReserved (14)
        if B.opcode == 0xa5 || LloInstructionIsIntegerMatmul(B):
            insert kMatmulAccumulationPipe (16)
        else:
            insert kLmrReserved (14)
        return set
    if (B.opcode - 0x8d) > 9:                         // vlxmr / matres (not matpush, not matmul)
        switch B.opcode:
            case 0xa9: seed = .rodata @0xb43b347, 2   // vlxmr
            case 0xaa: seed = .rodata @0xb43b345, 2
            case 0x152: seed = .rodata @0xb43b349, 1  // matres
            default:   LogMessageFatal("Unsupported MXU op")  // :508
        return flat_hash_set(seed ...)
    // else: matpush [0x8d,0x96]
    seed_set = { from .rodata @0xb43b200, 2 }         // SOO scratch seed
    if !MxuSeqPredicate(latch_mode(B)):               // vtable +0x358 gate
        return seed_set
    switch latch_index_in_sequence(B):                // 0..3
        case 0: insert (4*(msr!=0)) | 2               // kMsr{A,B}OverrunCheck0  (:447)
        case 1: insert (4*(msr!=0)) | 3               // ...Check1                (:454)
        case 2: insert (4*(msr!=0)) + 4               // ...Check2                (:461)
        case 3: insert (4*(msr!=0)) + 5               // ...Check3                (:468)
    return seed_set
```

> **GOTCHA —** Mind which opcode band owns which seed. Matmul `[0x9b,0xa5]` (`(v6-155)<=0xA`) builds the `@0xb43b344` seed plus the `done_with_gains`/`kMatmulAccumulationPipe`/`kLmrReserved` logic; matpush `[0x8d,0x96]` is the fall-through `else` that seeds `@0xb43b200` and runs the `latch_index_in_sequence` `OverrunCheck` switch. The two are easy to transpose because each builds a held-set from a contiguous opcode range.

The held-set elements are the named `MxuResource` enumerators (`kMsrAOverrunCheck0..3` / `kMsrBOverrunCheck0..3`, the transpose bit selecting the A vs B bank via `(4*(msr!=0))`; `kFusedLoadReserved`, `kLmrReserved`, `kMatmulAccumulationPipe`), as confirmed by the CHECK strings in the constructor (`holds.insert(MxuResource::kMatmulAccumulationPipe).second` `:491`, `holds.insert(MxuResource::kLmrReserved).second` `:488`, `kMsrAOverrunCheck0..3` `:447/454/461/468`, `kFusedLoadReserved` `:483`, `mxu_latency_table_vf.cc`).

> **GOTCHA —** the matpush held set is *sequence-step dependent*. The same matpush opcode holds `kMsr*OverrunCheck0` on sequence step 0 but `...Check3` on step 3, and the transpose flag flips the resource ordinal by `+4` (A-bank vs B-bank, the `(4*(msr!=0))` term). A reimplementation that treats matpush as holding a fixed resource set will mis-price the back-to-back stall for the inner steps of a latch sequence.

---

## The Worked Recurrence

Two independent VF bf16 matmuls `A`, `B` on the same MXU, no true data dependency (e.g. two K-tiles accumulating to different MRB chunks; `B` does not consume `A`'s output):

```text
ISSUE (footprints):
  A: matmul fmt1 → MxuOpResourceReservations(A) = {res1:15, res15:8, res17:7, res16:14}
                   (MatmulIssue 15cy, AccA 8, AccC 7, AccB 14)
  B: matmul fmt1 → MxuOpHoldIssues(B) held set = {res1, res15, res16, res17}

STALL (recurrence): GetLatencyBetween(A, B):
  same MXU [yes]
  seed = 0   (matmul-matmul pair: A is matmul-class, not 0xa9, so no availability seed)
  for k in {res1, res15, res16, res17}:  stall = max(stall, resA[k])
     = max(15, 8, 14, 7) = 15
  ⇒ B waits 15 cycles after A issues before re-using the MatmulIssue slot.

If B is instead a bf16 matpush (fmt1, narrow):
  resA(A=matpush) = {res0:2, MSR-A:1, MSR-B:1}
  heldB(B=matpush) ⊇ {res0, MSR-A, MSR-B}
  stall = max(2, 1, 1) = 2  ⇒ back-to-back bf16 matpushes pipeline at 2-cycle issue.

For int8/x8 (GLM_*_S8 → fmt6):
  matpush array {res0:8, MSR-A:7, MSR-B:6} → back-to-back stall 8
  matmul grp4 {res15:32, res17:31, res16:38} → stall 32  (4× bf16 = the x8 4-plane sequence)
```

The retire half is separate: a downstream op that *truly* consumes the matmul result waits the full base TOTAL latency (bf16 ≈ 212, fp8 ≈ 204 cycles, from the per-gen Performance array), routed through the true-dependency edge, not the structural stall. So issue rate is gated by the reservation stall (2/8/15/32) while result availability is gated by `GetLatency`.

---

## The Per-Edge Structural Gate

### Purpose

`LatencyBetweenInternal` is the scheduler's edge-latency function (vtable slot `+0x18`). It decides, for an ordered pair `(A, B)`, which cost model prices the edge: true data dependency → plain op latency; MXU-MXU structural hazard → the reservation matrix; cross-lane-unit conflict → the XLU conflict-penalty table. The MXU reservation is consulted *precisely* for the MXU-MXU non-true-dependency case — exactly what a reservation matrix models.

### Algorithm

```c
function LatencyBetweenInternal(A, B):              // VF @0x1c8a4ac0 / GL @0x1c8b22e0
    if (A.opcode - 233) < 4: return 0               // 0xe9..0xec pseudo class
    // (pop-and-then-push-same-FIFO consistency CHECK elided)
    if IsPseudo(A) || IsPseudo(B): return LatencyBetweenPseudo(A, B)

    base = GetLatency(A)                            // A's intrinsic op latency
    if vtable[+0x20](A, B):                         // IsTrueDependencyBetween @0x1c89fdc0
        return base                                  // B genuinely consumes A's result

    if LloOpcodeUsesMxu(A.opcode) && LloOpcodeUsesMxu(B.opcode):
        return MxuLatencyTable::GetLatencyBetween(this.mxu_table /*[this+0x1d8]*/, A, B)

    // else: XLU / permute conflict path
    if HasSetPermutePatternReservation(A, B): ... GetXluPathReservation + XluConflictPenaltyBetween
    ... final clamps (min floor, SetIar→indexed-load) ...
    return max(stall, GetResourceLatency(A, B))
```

`IsTrueDependencyBetween(A, B) @0x1c89fdc0` is `FindOperandIndexFull(B, A) >= 0` (B lists A as an operand) OR both `A` and `B` have opcode `0x14e` (a self-pairing pseudo). `LloOpcodeUsesMxu(op) @0x10a433e0` is true for `op ∈ [0x8d, 0xa5] ∪ [0xa8, 0xab] ∪ [0x152, 0x153]` (encoded as `(op-141) < 0x19 || (op-168) < 4 || (op-338) < 2`). The MXU table itself is at `LatencyTable+0x1d8` (`*((_QWORD*)this+59)`). The XLU conflict table is at `LatencyTable+0x18` and prices the non-MXU permute/transpose-FIFO conflicts.

> **NOTE —** both generations are structurally identical. GL `LatencyBetweenInternal @0x1c8b22e0` tail-jumps GL `GetLatencyBetween @0x1c8b71e0`, reading the same `IsTrueDependencyBetween` (vtable `+0x20`) and the MXU table at `this+0x1d8`. The only per-gen difference is the resource-count bound inside the reduction (19 VF vs 11 GL).

---

## The MxuLatencyBalancing Env-Flag Gate

### Purpose

A second, orthogonal gate controls whether MXU-*sequence-group* assignment uses the reservation-derived inter-op stalls or a simpler ordering. It is a compilation-environment knob, not a per-edge test: the per-edge structural gate above always fires for MXU-MXU non-true-dep edges; this flag only changes how the sequence assigner composes those stalls into an MXU allocation.

### Algorithm

```c
function MxuLatencyBalancingUseSequenceDependencies(env):   // @0x1d6b9c80
    p = env[+0xbe8]                                 // AutoProto* (TpuCompilationEnvironment+0xbe8)
    if !p: p = &AutoProto_globals_                  // default @0x223c8968
    v = AutoOr<bool>::FromProtoOrDie(*p)            // @0xf795300
    return (~v & 0x101) == 0                        // true iff v.bit8 (present) AND v.bit0 (value)
```

The single caller is `AssignMxusForSequenceGroup @0x10f753c0`. When the flag is ON (`al != 0` at `@0x10f75410`) the function builds an MRB-address-keyed `linked_hash_map` and applies the latency-balanced MXU-sequence assignment using the `CycleTable` argument; when OFF it branches to the simpler ordering path.

> **NOTE —** the accessor and its bit encoding (`bit8` present, `bit0` value) are byte-exact, and the default `AutoProto*` is `AutoProto_globals_ @0x223c8968`. The embedded default proto bytes were not decoded, so whether sequence-dependency balancing is ON or OFF by default in v0.0.40 is **(LOW confidence)** — the gate mechanism is certain, the default value is not pinned.

---

## Function Map

| Function | Address | Role |
|---|---|---|
| `MxuLatencyTable::GetLatencyBetween` (VF) | `0x1c8ae320` | the max-over-shared-resource stall recurrence |
| `MxuLatencyTable::GetLatencyBetween` (GL) | `0x1c8b71e0` | same, bound res ≤ 10 |
| `MxuOpResourceReservations` (VF / GL) | `0x1c8ad080` / `0x1c8b6300` | A's `array<int,N>` hold-cycle vector |
| `MxuOpHoldIssues` (VF / GL) | `0x1c8ad3a0` / `0x1c8b6760` | B's `flat_hash_set<MxuResource>` issue footprint |
| `LatencyTableViperfish::LatencyBetweenInternal` | `0x1c8a4ac0` | per-edge gate (vtable +0x18) |
| `LatencyTableGhostlite::LatencyBetweenInternal` | `0x1c8b22e0` | GL per-edge gate |
| `IsTrueDependencyBetween` | `0x1c89fdc0` | `FindOperandIndexFull ≥ 0` ∨ both op `0x14e` |
| `LloOpcodeUsesMxu` | `0x10a433e0` | op ∈ `[0x8d,0xa5] ∪ [0xa8,0xab] ∪ [0x152,0x153]` |
| `MxuLatencyBalancingUseSequenceDependencies` | `0x1d6b9c80` | env `+0xbe8` AutoOr<bool> gate |
| `AssignMxusForSequenceGroup` | `0x10f753c0` | sole consumer of the env flag |
| `flat_hash_set<MxuResource>` held-set ctor | `0x1c8ae0a0` | builds B's footprint set |

---

## Considerations

- **Resource-count divergence.** VF carries 19 `MxuResource` values, GL/GF 11. The CHECK bound (`≤18` / `≤10`) is the only behavioral difference in the reduction; a reimplementation must size `A`'s reservation array per gen or the bound check fires `LogMessageFatal`.
- **Direction matters.** `GetLatencyBetween(A, B)` is not symmetric: `A` supplies the reservation vector (how long it holds each port) and `B` supplies the held set (which ports it needs). Swapping arguments prices a different edge.
- **The seed.** The pre-reduction seed is non-zero in exactly one case: a matrix-load `A` (vlxmr/MSR, opcode `0xa9`) feeding a matmul `B` (`B.opcode ∈ [0x9b,0xa5]`) seeds `stall = 1` (`v14` at `@0x1c8ae320`). A matmul `A` against a matres `B` (`0x152`) bypasses the reduction entirely and returns a `MatmulDataFormat`-keyed cost-map value (`this+0x80`). Every other pair — including matmul-to-matmul and matpush-to-matpush — seeds 0, so the stall is purely the max over the held set.
- **What is not pinned.** The literal contents of the `MxuOpHoldIssues` SOO-scratch seed (`@0xb43b200`) decode ambiguously at 4-byte stride; the operative held set comes from the modifier-map enumeration, and the seed's functional role (scratch) is confirmed but its literal bytes are **(LOW confidence)**. The default value of the `MxuLatencyBalancing` env flag is likewise unpinned (see above).

## Cross-References

- [MXU Latency Overview](mxu-latency-overview.md) — the `MxuResource` enum and the reservation-matrix concept this page consumes
- [MXU Latency: GF](mxu-latency-gf.md) — the `6acc60406` per-format matmul/matpush reservation values and the conv cost triple
- [MXU Latency: GL](mxu-latency-gl.md) — the Ghostlite reservation matrix (11-resource bound)
- [Performance Family Overview](performance-overview.md) — `GetLatency` (the retire-half base latency) and the per-gen Performance grid
- [MXU Slot](../isa/slot-mxu.md) — the physical MXU sub-units the held resources name
