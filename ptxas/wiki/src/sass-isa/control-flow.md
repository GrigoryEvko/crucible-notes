# SM Control Flow, Divergence & Convergence

> *Recovered from CUDA 13.1 ptxas V13.1.115 SASS output and the decoded
> per-architecture nvdisasm instruction tables (Turing SM75 through Blackwell
> SM120). The convergence-barrier model below was reconstructed from emitted SASS
> across `BSSY`/`BSYNC`/`BREAK`/`BMOV`/`BRA`/`CALL`/`RET` instructions, their
> operand fields, and the scheduler control-word dependency classes. Every register
> field, modifier, and lowering rule cited here is observable in `nvdisasm -c`
> output and the 128-bit encoding tables.*

Since Volta (SM70) the SM abandoned the pre-Volta **SIMT reconvergence stack** (the
`SSY`/`.S`/`PBK`/`BRK`/`PCNT`/`CONT` machine) in favour of **independent thread
scheduling**: every lane carries its own program counter and the warp's
reconvergence points are named explicitly by a small file of **convergence-barrier
registers** `B0…B15`. Turing, Ampere, Ada and Hopper inherit this model unchanged;
Blackwell extends only the instruction *spelling* (a `.RECONVERGENT` modifier, below)
while keeping the same barrier file and the same lowering. This page is the
hardware's control-flow and convergence contract; the compiler-side hazard model
(RAW/WAR/WAW + the control dependency) is summarized at the end and lives in full in
the [Dependency & Hazard Model](../scheduling/dependency-model.md).

## 1. The active mask and independent thread scheduling

A warp is 32 lanes. At any cycle the hardware holds, per warp, an **active mask**
(`MACTIVE`, 32 bits) selecting which lanes the next instruction executes on. An
instruction's **effective mask** is `MACTIVE & guard_predicate`: a guarded
instruction `@P0 FADD …` runs only on lanes that are both active and pass the guard.
Three more 32-bit warp-level masks complete the lane-state model:

| Mask | Meaning |
|---|---|
| `MACTIVE` | lanes currently executing (the active/exec mask) |
| `MEXITED` | lanes that have executed `EXIT` |
| `MKILL` | lanes killed by `KILL` (e.g. discarded fragments) |
| `MATEXIT` | lanes pending the at-exit handler |

Pre-Volta SIMT kept reconvergence implicit on a per-warp stack: a divergent branch
pushed the fall-through PC and the active mask, and execution serialized the two
sides before the hardware automatically popped the mask at the reconvergence PC. The
weakness was that **all** lanes shared one PC, so a thread holding a lock could never
yield to a thread spinning on it within the same warp — a structural deadlock for
fine-grained synchronization.

Under independent thread scheduling each lane has a private PC. There is no implicit
mask stack; reconvergence happens **only** where the program asks for it, via a
convergence barrier. This is why divergent code on SM70+ may interleave the two sides
of a branch at warp-instruction granularity, and why correct intra-warp
communication requires the explicit `.sync` family (`SHFL.SYNC`, `VOTE.SYNC`,
`__syncwarp`). The lane PCs and a small per-lane reconvergence-state file are CBU
state the compiler can read back through `BMOV` (the eight `THREAD_STATE` slots in
§4).

## 2. Convergence barriers: `BSSY` / `BSYNC` / `BREAK`

The reconvergence machine is three instructions over a barrier register `B`:

| SASS | role | operands |
|---|---|---|
| `BSSY  Ba, #relReconv` | **set** a convergence barrier | barrier `Ba`, PC-relative reconvergence target |
| `BSYNC Ba` | **wait / reconverge** on a barrier | barrier `Ba` |
| `BREAK Ba` | **drop** issuing lanes from a barrier | barrier `Ba` |

A convergence barrier register physically holds **a participating-lane membership
mask plus the reconvergence PC**. `BSSY Ba, #relReconv` records, for the current set
of active lanes, that they will reconverge at `PC + relReconv`, and arms barrier
`Ba`. Lanes then diverge freely (one or both sides of a branch run). `BSYNC Ba`
blocks each arriving lane until **every** lane still a member of `Ba` has reached a
`BSYNC` on it; at that point the hardware re-forms the recorded mask, clears the
barrier, and continues all of them together at the reconvergence PC. A lane that must
leave the region early — a `break`, an early `return`, an exit — issues `BREAK Ba` to
remove itself from membership so the others' `BSYNC` does not wait on it forever.

The canonical divergent-`if`/call pattern, reproduced exactly from `nvdisasm -c` of a
kernel with a non-uniform `call` (SM90):

```
BSSY  B0, `(.L_x_0) ;     // arm barrier B0, reconverge at .L_x_0
@!P0  BRA  `(.L_x_1) ;     // lanes failing P0 branch away
CALL.REL.NOINC `($k$helper) ;
.L_x_0:
BSYNC B0 ;                 // all lanes reconverge here
```

`BSSY` carries, besides the 4-bit barrier selector, a signed PC-relative immediate to
the reconvergence point (encoded in 4-byte units — the assembler scales the byte
offset down by 4). `BSYNC` carries only the barrier selector. `BSYNC` always permits
the warp to **yield** while waiting (it sets its yield bit unconditionally), so a warp
stalled at a reconvergence point cannot starve sibling warps on the same
sub-partition — a prerequisite for the forward-progress guarantee in §5.

`BSSY`, `BSYNC`, `BREAK` may all be guarded by a predicate (`@P`, `@!P`); a predicated
`BREAK` is exactly how the compiler emits "this conditional edge leaves the loop, so
release these barriers on the lanes that take it." Where a region nests inside another
(a `break` out of an inner loop that is itself inside an outer loop), the compiler
emits one `BREAK` per enclosing barrier, from the innermost up to — but not including
— the barrier that owns the destination.

### The barrier register file (`B0…B15`)

The 4-bit barrier-operand field on `BSSY`/`BSYNC`/`BREAK`/`BMOV` selects one of
**16 compiler-visible barriers, `B0…B15`**. The all-ones value (`15` in the field, i.e.
no barrier) is reserved to mean *no barrier operand present* on instructions where the
barrier is optional, so the practically allocatable set the compiler draws from is
`B0…B14` with `B15` as the sentinel. A warp can query how many barriers it was actually
granted by reading the `BARRIERALLOC` special register (`S2R`, SR index 62), the
barrier-file analogue of the `REGALLOC` (SR 61) GPR-budget query. The barrier file is
a hard architectural resource: deeply nested or irregular divergence can exhaust it,
at which point the compiler **spills** a barrier to a general register (§4).

The barrier register is what makes reconvergence a *named, first-class* resource
rather than an implicit stack discipline. Two independent divergent regions can be in
flight on different barriers; a region can be entered, left via `BREAK`, and its
barrier reused — none of which the old single-stack model could express.

### Blackwell: `.RECONVERGENT`

On Blackwell (SM100, SM120) the same two set/wait instructions acquire an explicit
`.RECONVERGENT` modifier in their disassembly while keeping the identical `B0`
operand and the identical lowering. From `nvdisasm -c` of the same divergent-call
kernel built for SM100:

```
BSSY.RECONVERGENT  B0, `(.L_x_0) ;
CALL.REL.NOINC `($k$helper) ;
.L_x_0:
BSYNC.RECONVERGENT B0 ;
```

The modifier is absent on Hopper and earlier (`BSSY B0` / `BSYNC B0`), present on
SM100 and SM120 — a binary-RE-only observation, as Blackwell post-dates the
convergence-barrier model documented for Turing→Hopper here. It does not change the
barrier file, the membership-mask semantics, or the reconvergence-PC operand; it makes
the reconvergence intent of the barrier explicit in the encoding.

## 3. Branches, calls, returns, exit

### Conditional and uniform branches — `BRA`

`BRA` is the direct, PC-relative branch. Its encoding carries, beyond the 48-bit
relative target, three control fields that tie it to the convergence machine:

- a **guard predicate** `@P`/`@!P` (a normal divergent conditional branch), and a
  separate **predicate-accumulator** (`pa`) pair used to fold a chain of predicate
  tests into the branch decision;
- a 2-bit **branch-condition** selector with four values:

  | `BRA.cond` | meaning |
  |---|---|
  | (none) | ordinary branch; divergence resolved by the guard predicate |
  | `.U` | **uniform** — all active lanes provably agree, so no divergence is created |
  | `.DIV` | the branch is the **divergence point** of a region |
  | `.CONV` | the branch is a **reconvergence point** |

  The `.U` form is the uniform-control-flow fast path: the hardware need not track per-lane
  divergence because the compiler has proven the condition warp-uniform. The `.DIV`/`.CONV`
  forms are emitted with no guard predicate (the predicate field is `PT`); they annotate the
  CFG edge as the divergence or reconvergence boundary that pairs with a `BSSY`/`BSYNC`.

- a 2-bit **depth** field that adjusts the warp's **API call-depth** counter (see
  below); for a plain `BRA` it is `NONE`.

### Indexed and computed branches — `BRX` / `JMP` / `JMX`

`BRX` is a register-indexed relative branch: it adds a per-lane GPR to a base
immediate, the classic lowering for a `switch` jump table or a computed `goto`. Its
uniform sibling `BRXU` reads a **uniform register** instead of a per-lane GPR, so the
target is warp-uniform and no divergence is produced. `JMP` is the absolute
(immediate or constant-bank) jump; `JMX`/`JMXU` are the absolute computed forms
(per-lane GPR vs uniform register). `BRX`/`JMX` engage both the branch unit and the
address-generation datapath because the target must be computed; an unresolved
indirect target (a branch through a table the assembler cannot bound) is a hard error
— the scheduler cannot build the control-flow graph without the candidate target set.

A genuinely **divergent** indexed branch (lanes taking different table entries) is
itself a divergence point and is bracketed by a `BSSY`/`BSYNC` pair on a barrier just
like a divergent `BRA`.

### `CALL` / `RET` and the API call-depth counter

`CALL` comes in absolute/relative × direct-immediate / constant-bank /
uniform-register / GPR-target variants; `RET` in a direct and a uniform-register
form. Each carries a 1-bit **depth** modifier that the disassembler renders as a
suffix:

- `CALL.…INC` / `CALL.…NOINC` — increment, or do not increment, the **API call-depth**
  counter;
- `RET.…DEC` / `RET.…NODEC` — decrement, or do not decrement, the counter.

The API call-depth counter is a per-warp CBU register (`API_CALL_DEPTH`, §4) — **not**
a hardware return-address stack; the return address is an ordinary value the compiler
materializes (with `LEA`/`MOV` of a return-address relocation) and feeds to the
callee, restoring it at the `RET` site. The depth counter exists so the CBU can track
divergent call nesting for reconvergence and at-exit bookkeeping. In the SM90 sample
above the call site is `CALL.REL.NOINC` and the function returns with
`RET.REL.NODEC R4` — the `NOINC`/`NODEC` pairing the compiler selects when the call is
not managing API depth, and the divergent-exit form that leaves the counter untouched
so the outer reconvergence still accounts for the diverged lanes.

A call that may return divergently (the callee can `RET` while its lanes are diverged,
or the call target varies per lane) is wrapped in its own `BSSY`/`BSYNC` so the caller
reconverges after the call. The compiler detects the divergent-exit and varying-target
cases statically and synchronizes the call site and the function epilogue.

### `EXIT`

`EXIT` retires the issuing lanes (sets their `MEXITED` bit). It is the lowering of a
warp/thread terminating; a function that ends a thread emits `EXIT` rather than `RET`.
`EXIT` has modes for keeping a reference count and for the preempted case. A lane that
exits while others in its barrier regions are still converging must `BREAK` out of
those barriers first, which the compiler inserts on the exit path.

## 4. Saving and restoring convergence state — `BMOV`

`BMOV` moves data between general registers and the **convergence/branch-unit (CBU)
state**, addressed by a 6-bit state selector. The state space is:

| Selector | State |
|---|---|
| `0…15` | barrier registers `B0…B15` |
| `16…23` | eight per-lane `THREAD_STATE` reconvergence-state slots |
| `24` `MEXITED` / `25` `MKILL` / `26` `MACTIVE` / `27` `MATEXIT` | the four 32-bit warp masks |
| `28` `OPT_STACK` | the divergence-optimization stack |
| `29` `API_CALL_DEPTH` | the call-depth counter |
| `30/31` `ATEXIT_PC_LO/HI` | the 64-bit at-exit handler PC |

`BMOV` therefore has three jobs:

1. **Spill / refill a barrier.** When the 16-entry barrier file is exhausted, the
   compiler reads a live barrier into a GPR with `BMOV.32.CLEAR Rd, Bx` (which also
   clears the barrier so the physical slot is free) and later restores it with
   `BMOV.32 Bx, Rd`. A real example from SM75 disassembly, where the function-entry
   sequence clears `B0` before re-arming it:

   ```
   BMOV.32.CLEAR RZ, B0 ;    // read+clear B0 (to RZ — discard, just clear it)
   BSSY  B0, `(.L_x_0) ;
   ```

2. **Save / restore the active mask, with per-quad granularity.** `BMOV.32.PQUAD`
   moves `MACTIVE` a quad (4 lanes) at a time, used when reconstructing partial active
   masks. Barrier-to-barrier copies (`BMOV.32.CLEAR Bd, Ba`) move a barrier's mask
   directly.

3. **Set the at-exit handler PC.** `BMOV.64 ATEXIT_PC, #imm/Rb/c[…]` writes the
   64-bit PC the warp jumps to at `EXIT` when an at-exit handler is registered (e.g.
   geometry-shader output finalization); `BMOV.64 ATEXIT_PC, 0` clears it at program
   entry.

`BMOV` is the only way to make convergence state — barriers, masks, lane PCs, the
call-depth counter, the at-exit PC — observable or restorable, which is exactly why it
exists: barrier spilling and divergent-call boundaries need to move that state through
the GPR file. `BMOV` does **not** consume the warp scheduler's slot the way the
flow-control instructions do; it is treated by the scheduler as a CBU read/write with
a normal scoreboard.

## 5. Forward progress — `YIELD`, `WARPSYNC`, `NANOSLEEP`

Independent thread scheduling makes intra-warp spin loops correct only if a spinning
warp can step aside for a sibling warp making progress. Four mechanisms provide the
guarantee:

- **`YIELD`** relinquishes the warp's issue slot after the current instruction so the
  sub-partition picks another ready warp. The scheduler **inserts** a `YIELD` wherever
  forward progress can otherwise stall: a block (or, preferentially, a loop header or
  back-edge) that contains an atomic with a result, a `volatile`/strong/acquire/mmio
  load, or an intra-warp communication op (`SHFL`, `VOTE`, `MATCH`, `REDUX`) — exactly
  the operations whose result may depend on another warp's or lane's progress. `YIELD`
  is also the per-instruction scheduler-yield hint encoded in the control word (the
  `usched_info` field), distinct from the standalone `YIELD` instruction; the
  static stall-scheduler uses it to interleave warps even in straight-line code.

- **`BSYNC`'s yield bit** (always set) makes every reconvergence point a yield point,
  so a warp waiting for its lanes to converge does not hold the sub-partition.

- **`WARPSYNC`** reconverges (and gates further execution on) a programmer-named subset
  of the warp's lanes — the lowering of `__syncwarp(mask)`. Its 32-bit thread-mask
  operand comes from a GPR, a uniform register, an immediate, or a constant. It is
  orthogonal to the `B0…B15` barriers: `WARPSYNC` synchronizes an arbitrary explicit
  mask, while the barriers synchronize the compiler's structural reconvergence points.

- **`NANOSLEEP`** backs a spinning lane off for a (optionally randomized) duration; the
  per-thread form implicitly yields, so the scheduler omits a redundant `YIELD` in a
  block that already nanosleeps. `NANOTRAP` is the trapping variant. When the
  forward-progress guarantee is disabled, the inserted `NANOSLEEP`s are dropped.

All of these — together with `BPT` (breakpoint/trap, modes pause/trap/int/drain) and
`RTT` (return-from-trap) — are issued by the same unit as the branches and barriers.

## 6. Warp collectives: the convergent datapath

`WARPSYNC` reconverges a named lane subset; the instructions in this section are
the ones that then *compute across* that converged subset. They are the SASS
lowerings of the PTX `.sync` collective family, and every one of them takes the
warp's **active mask** as an implicit operand: the result of an active lane is
defined only over the set of lanes that are simultaneously active (the PTX
"membermask" must equal the arriving set). The functional model below was
recovered by compiling each PTX form, confirming the emitted op with `nvdisasm`,
and differential-testing a single 32-thread warp on hardware with deliberately
*divergent* active masks (lanes deactivated by predication) — reading every
lane's result back and matching it bit-for-bit.

### `VOTE` / `VOTEU` — warp ballot and reductions of a predicate

| PTX | SASS | result |
|---|---|---|
| `vote.sync.ballot.b32 Rd,P,mask` | `VOTE.ANY Rd, PT, P` | 32-bit mask, bit `l`=1 iff lane `l` active **and** `P` true; inactive lanes contribute 0 |
| `vote.sync.all.pred Pd,P,mask` | `VOTE.ALL Pd, P` | 1 iff `P` true on **every** active lane |
| `vote.sync.any.pred Pd,P,mask` | `VOTE.ANY Pd, P` | 1 iff `P` true on **some** active lane |
| `vote.sync.uni.pred Pd,P,mask` | `VOTE.ALL`/`.ANY` pair | 1 iff `P` is **uniform** across active lanes |
| `activemask.b32 Rd` | `VOTE.ANY Rd, PT` | the active mask itself (predicate forced `PT`) |

`VOTEU` is the uniform-datapath form (`VOTEU.ANY UR, UPT, …`): when ptxas can
prove the predicate uniform it computes the ballot once on the uniform unit
instead of per lane. `ACTIVEMASK` is just a ballot with the predicate pinned
true, so its result equals the current active mask exactly — the property the
differential harness asserts directly.

### `SHFL` — lane-to-lane copy with segment clamp

`SHFL.{IDX,UP,DOWN,BFLY} Pd, Rd, Ra, Rb, Rc` reads `Ra` from a computed source
lane and also returns a predicate `Pd`. The `c` operand packs **two** fields,
`c = (segmask << 8) | clamp`, with `segmask = c[12:8]` and `clamp = c[4:0]`. For
a sub-warp width `W` the front end emits `segmask = 32 - W` and `clamp = W - 1`.
The hardware derives, per lane:

```
minLane = laneid & segmask                 # segment base (bits of laneid kept)
maxLane = minLane | (clamp & ~segmask)      # segment top
```

so full-warp width (`segmask = 0`) makes the whole warp one segment
(`minLane = 0`, `maxLane = clamp = 0x1f`). The source lane and in-range
predicate per mode are:

| mode | source lane | in range (`Pd`) when |
|---|---|---|
| `IDX`  | `minLane \| (b & ~segmask)` | `src ≤ maxLane` |
| `UP`   | `laneid − b`               | `src ≥ minLane` |
| `DOWN` | `laneid + b`               | `src ≤ maxLane` |
| `BFLY` | `laneid ^ b`               | `minLane ≤ src ≤ maxLane` |

`Pd` is a purely **geometric** test: it does not depend on whether the source
lane is active. When the source lane is out of range the destination keeps the
issuing lane's own value; when the source lane is in range but **inactive**, the
result value is **undefined** (hardware writes nothing — the destination keeps
its prior contents) even though `Pd` is 1. Reliable `SHFL` data therefore
requires the source lane to be in the converged set, which is why the `.sync`
form ties the shuffle to a membermask.

### `MATCH` — find lanes holding the same value

| PTX | SASS | result |
|---|---|---|
| `match.any.sync.b32 Rd,R,mask` | `MATCH.ANY Rd, R` | per-lane mask of the active lanes whose `R` equals this lane's `R` |
| `match.all.sync.b32 Rd\|Pd,R,mask` | `MATCH.ALL Rd, Pd, R` | if all active lanes share one value: `Rd = active`, `Pd = 1`; else `Rd = 0`, `Pd = 0` |

`MATCH.ANY` is the building block for converting a warp's data-dependent
divergence into a set of "labels" — e.g. a conflict-free atomic aggregation.

### `REDUX` — single-instruction warp reduction (uniform result)

`redux.sync.{add,min,max,and,or,xor}.{u32,s32,b32} Rd,R,mask` lowers to
`REDUX.{SUM,MIN,MAX,AND,OR,XOR}[.S32] UR, R` — note the **uniform** destination:
the reduction over all active lanes is computed once and broadcast, so every
active lane sees the identical scalar. `min`/`max` honour the signedness of the
PTX type (`.s32` vs `.u32`); the bitwise ops are width-32. `REDUX` collapses the
classic `log₂(warp)`-step `SHFL.BFLY` reduction tree into one hardware op.

### `ELECT` — pick a single leader lane

`elect.sync Rd|Pd, mask` (`ELECT`, Hopper+) returns `Pd = 1` on exactly the
**lowest** active lane and 0 elsewhere, and `Rd` = that leader's lane id. It is
the canonical single-writer primitive ("one lane does the store"). On parts
without `ELECT` the same semantics are the predicate `laneid == FFS(activemask)`,
which is how the pre-Hopper lowering expresses it.

### Convergence requirement, restated

All of the above are **convergent** ops: their datapath crosses lanes, so the
participating lanes must be converged at the instruction. The compiler guarantees
this by pairing the collective with the membermask sync (`WARPSYNC`, or the
`.sync` semantics folded into the op) and by the control dependency (§7) that
forbids hoisting a collective across a divergent branch or an active-mask
mutation. A collective issued on a partially-diverged warp reads its inputs only
from the arriving lanes — which is exactly why the differential model masks every
result by the active set.

## 7. The branch unit (CBU) and the control hazard

### One unit, one queue

Every control-flow and convergence instruction routes through a single per-warp
**branch / convergence-barrier unit (CBU)** and its virtual issue queue:
`BRA`, `BRX`, `JMP`, `JMX`, `CALL`, `RET`, `EXIT`, `BSSY`, `BSYNC`, `BREAK`, `BMOV`,
`KILL`, `RTT`, `NANOSLEEP`, `NANOTRAP`, `WARPSYNC` and `YIELD` all carry the CBU
resource bit. They are **decoupled** (variable-latency, scoreboard-tracked) ops with a
fixed unit latency of **6 cycles**. Because they share one resource and one queue,
the hardware serializes them with respect to each other regardless of their register
operands — exactly what a control-state machine needs, since the order in which
barriers are armed, masks are mutated, and branches are taken must be the program
order.

There is no architected branch *delay slot* on these machines — the decoded
machine descriptions carry a branch-delay value of zero, so the instruction
immediately after a taken branch is not unconditionally executed. What the scheduler
must instead respect is the **control-dependency latency**: the minimum spacing
between a branch (or a barrier/mask mutation) and a later control instruction, carried
as a latency on the control-dependency edge rather than as a delay slot. Latencies
that span a branch, call, or return are resolved by a cross-edge pass that decides
whether the producer or the consumer side is responsible for the gap and inserts
stall/`NOP` spacing in the right block.

### The control dependency — the 4th hazard kind

The scheduler classifies every dependency edge into one of **four** kinds:

| Kind | Hazard |
|---|---|
| true / flow | RAW (read-after-write) |
| output | WAW (write-after-write) |
| anti | WAR (write-after-read) |
| **control / order** | **control-flow, condition-code, branch-state ordering** |

The first three are the register data hazards. The **fourth, the control dependency,
is the control hazard**: it serializes mutations of state that is not a renameable
register operand — the convergence-barrier file, the active mask, the API call-depth
counter, condition codes, and the position of an instruction relative to a branch,
label, or divergent exit. The scheduler adds a control-dependency edge to pin a
control-flow instruction behind the block label, to keep an instruction from being
hoisted across a divergent exit, to order anything that can change the active mask
(`KILL`, `WARPSYNC`) against the surrounding flow, and to enforce volatile/ordered
memory sequencing. Modeling it as a distinct kind — rather than overloading a register
dependency — is what lets the scheduler freely reorder *data*-independent arithmetic
while still forbidding any reordering that would corrupt the divergence/reconvergence
state.

Concretely, the control state behaves like a scoreboarded resource with its own
latency column: arming a barrier, mutating the active mask, and taking a branch are
each ordered against later control instructions through control-dependency edges, so
the CBU sees them in program order even as the data pipe runs ahead.

## 8. Summary — the lowering at a glance

| Source intent | SASS | barrier / field |
|---|---|---|
| enter a divergent region (reconverge after) | `BSSY Ba, #relReconv` | sets `Ba`, records reconverge PC + active mask |
| reconverge | `BSYNC Ba` | waits on `Ba`, yields while waiting, re-forms mask |
| leave a region early (break / early-exit lane) | `@P BREAK Ba` | drops issuing lanes from `Ba` |
| divergent conditional | `@P BRA` / `BRA.DIV` | guard predicate; `.DIV` marks divergence point |
| uniform conditional | `BRA.U` / `BRA.CONV` | proven warp-uniform; no divergence / reconverge point |
| switch / computed branch | `BRX` / `JMX` (`BRXU`/`JMXU` uniform) | per-lane GPR vs uniform register index |
| call (return addr in register) | `CALL.…{INC,NOINC}` | API-depth ± via depth field |
| return | `RET.…{DEC,NODEC}` | API-depth ∓ via depth field |
| thread/warp terminate | `EXIT` | sets `MEXITED` |
| spill / restore convergence state | `BMOV.{32,64}{.CLEAR,.PQUAD}` | B-reg ↔ GPR, mask, `ATEXIT_PC`, depth |
| spin-loop forward progress | `YIELD` / `NANOSLEEP` / `WARPSYNC` | relinquish slot / sleep / mask-sync |
| warp ballot / predicate vote | `VOTE.{ANY,ALL}` / `VOTEU.…` | ballot mask, all/any/uni over active lanes |
| warp ballot of "true" (active set) | `VOTE.ANY Rd, PT` | the active mask (== `activemask`) |
| lane-to-lane copy | `SHFL.{IDX,UP,DOWN,BFLY}` | segment clamp `c=(segmask<<8)\|clamp`; geometric in-range `Pd` |
| equal-value lane grouping | `MATCH.{ANY,ALL}` | per-lane match mask (+ `Pd` for `.ALL`) |
| single-instruction warp reduction | `REDUX.{SUM,MIN,MAX,AND,OR,XOR}` | uniform-register result broadcast to all active lanes |
| pick one leader lane | `ELECT` | `Pd`=1 on lowest active lane (Hopper+) |

The whole machine is named, explicit, and compiler-scheduled: 16 convergence
barriers replace the implicit reconvergence stack, per-lane PCs make divergence
free-running, `BSSY`/`BSYNC`/`BREAK` mark every reconvergence point by hand, `BMOV`
spills the barrier file and saves the masks, and a dedicated control dependency keeps
the branch unit's view of all of it in program order. Blackwell adds only the
`.RECONVERGENT` spelling on the set/wait pair; the model is otherwise stable from
Turing through Hopper.
