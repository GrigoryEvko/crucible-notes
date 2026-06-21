# Occupancy, Resources & Work Distribution

> *Recovered from the ptxas v13.0.88 (CUDA 13.0) register-allocation and
> launch-bounds code paths, cross-validated against the decoded per-architecture
> SASS tables in nvdisasm V13.1.115 (CUDA 13.1) and against SASS that ptxas
> actually emits under `--maxrregcount` / `__launch_bounds__`. Facts and models
> only.*

Occupancy is the single number that ties ptxas's whole back end together: how many
warps the hardware will keep resident on one SM while a kernel runs. It is *not* a
free runtime property — ptxas computes a **register-pressure target** at compile
time and shapes register allocation, rematerialization, and scheduling around it,
because the register count it picks is exactly what bounds how many warps and CTAs
fit. This page recovers the resource model the compiler uses, the occupancy
arithmetic, how `__launch_bounds__` / `maxntid` / `minnctapersm` / `-maxrregcount`
steer it, and how the grid's CTAs reach the SMs.

The headline result: **the resource accounting is a fixed, integer-exact model**,
stable in its arithmetic from Turing (SM75) through Hopper (SM90). Only the
per-architecture *constants* (warps/SM, shared-memory capacity, max CTAs/SM) move;
the formulas do not.

## The SM as a resource pool

Every occupancy decision is a `min` across a small set of hard resource limiters.
The physical substrate is stable across Volta→Hopper:

| Resource | Value | Notes |
|---|---|--:|
| Register file per SM | **65,536 × 32-bit registers** (256 KB) | 256 KB / 4 bytes per register |
| Register file per CTA limit | **65,536 regs** (32,768 on SM72/SM82 cache-steered variants) | a *separate*, sometimes tighter cap |
| Sub-partitions per SM | **4** | each owns a register-file slice + warp scheduler |
| Register-allocation granularity (per thread) | **8 registers** | per-thread register counts round up to a multiple of 8 |
| Register-allocation unit (warp-wide) | **256 registers** | = 8 regs × 32 threads; the SM allocates registers a warp at a time |
| Max registers per thread | **255** (`RZ` = R255); **253** user-visible (2 reserved) | `RZ` and the PC-pair reservation are not user-allocable |
| Warp size | **32 threads** | invariant |
| Warps per SM | **64** (SM70/80/87) · **48** (SM86) · **32** (SM75/turing) | the warp-slot limit |
| Max CTAs per SM | **32** (SM70/80/87) · **16** (SM75/86) | the CTA-slot limit |
| Shared memory per SM | **96 KB** (SM70) · **64 KB** (SM75) · **164 KB** (SM80/87) · **100 KB** (SM86) | configurable L1/smem split |
| Shared memory per CTA | **48 KB** static (more via dynamic opt-in) | a per-CTA cap below the per-SM total |

Two things in this table are easy to miss and matter for the arithmetic below:

- **There are two register limits, not one.** A per-SM register file (65,536) *and*
  a per-CTA register-file cap. On parts where CTA-steering / caching is engaged the
  per-CTA cap can be half the per-SM file (32,768), and it can bind *before* the
  per-SM file does. ptxas applies both.
- **Registers are allocated a warp at a time, rounded to 8 per thread.** The unit
  the hardware hands out is 256 registers (one warp's worth at 8-register
  granularity). This is why occupancy moves in discrete *cliffs* rather than
  smoothly: dropping from 33 to 32 registers/thread can double the resident warp
  count, while 32→31 does nothing until the next multiple of 8.

## Occupancy arithmetic — warps from registers

The compiler models the register file as **4 sub-partitions**, each holding
`65536 / 4 / 32 = 512` *warp-wide* registers (a warp-wide register is 32 threads ×
one 32-bit scalar). Per sub-partition, the number of resident warps a kernel can
field at `R` registers/thread is `floor(512 / roundUp8(R))`; times the 4
sub-partitions:

```
warpsResident = 4 * floor( 512 / roundUpToMultipleOf8(R) )      // capped at maxWarpsPerSM
occupancy     = warpsResident / maxWarpsPerSM
```

ptxas evaluates exactly this when it reports a kernel's static occupancy. Worked
examples on a 64-warp SM (SM70/80):

| Regs/thread `R` | `roundUp8(R)` | warps/sub-part `floor(512/·)` | resident warps `×4` | occupancy |
|--:|--:|--:|--:|--:|
| 16 | 16 | 32 | 128 → **64** (capped) | 100 % |
| 24 | 24 | 21 | 84 → **64** (capped) | 100 % |
| 32 | 32 | 16 | **64** | 100 % |
| 33 | 40 | 12 | **48** | 75 % |
| 40 | 40 | 12 | **48** | 75 % |
| 48 | 48 | 10 | **40** | 62.5 % |
| 64 | 64 | 8 | **32** | 50 % |
| 96 | 96 | 5 | **20** | 31 % |
| 128 | 128 | 4 | **16** | 25 % |
| 255 | 256 | 2 | **8** | 12.5 % |

The cliff at 32→33 registers (100 %→75 %) is the textbook example: it is the first
multiple-of-8 boundary that drops a warp out of every sub-partition. This is also
the inverse of the **max-registers-for-a-warp-target** computation ptxas uses to
turn a desired warp count back into a register ceiling:

```
maxRegForWarps(W)  = roundDown8( 512 * 2 / W ) - reserved      // registers/thread for W warps/sub-pair
warpsForRegs(R)    = ( 512 / roundUp8(R + reserved) ) * 2      // warps/sub-pair for R registers
```

(The `512 * 2` and the `/2` factors come from the compiler modelling occupancy in
units of *two* hardware warps; the result is identical to the per-sub-partition form
above.) These two functions are each other's inverse across the 8-register grid, and
the scheduler steps `R` along that grid when it searches register targets.

## The full limiter `min` — registers, warps, CTAs, shared memory

Occupancy in CTAs is the **minimum across four independent caps**, computed by
ptxas's static performance model exactly as the hardware would resolve them at
launch:

```
warpsPerCTA       = ceil(threadsPerCTA / 32)

regLimitedCTAs    = warpsResident(R) / warpsPerCTA            // register file
warpSlotCTAs      = maxWarpsPerSM   / warpsPerCTA             // 64/48/32 warp slots
ctaSlotCTAs       = maxCTAsPerSM                              // 32 or 16 CTA slots
smemLimitedCTAs   = smemPerSM / smemPerCTA                    // shared memory

residentCTAs      = min( regLimitedCTAs, warpSlotCTAs, ctaSlotCTAs, smemLimitedCTAs )
residentWarps     = residentCTAs * warpsPerCTA
```

Whichever term is smallest is the **binding limiter**. A kernel can be
register-bound, warp-slot-bound, CTA-slot-bound, or shared-memory-bound, and the
compiler's job in register allocation is to keep the *register* term from being the
one that loses. Note that `warpSlotCTAs` and `ctaSlotCTAs` are why a kernel with
tiny CTAs (e.g. 32 threads = 1 warp) tops out at the CTA-slot count (16 or 32) and
never reaches 64 warps regardless of register usage — 32 one-warp CTAs is the wall.

These four caps are not a software convention. The hardware enforces the matching
set at CTA-launch time as a fixed battery of launch-validation checks — among them a
**register-count** check, a **total-threads** check, a **shared-memory-size** check,
and a **per-CTA register-consumption** check that validates the per-CTA register cap
with warps rounded up to the 4 sub-partitions. ptxas's register-target arithmetic is
built to land a kernel on the legal side of each of those checks; a kernel that
overran them would be rejected by the SM, not merely slow.

## Launch bounds → register ceiling

`__launch_bounds__(maxThreadsPerBlock, minBlocksPerSM)` lowers to the PTX directives
`.maxntid` (and/or `.reqntid`) and `.minnctapersm`, plus the command-line
`-maxrregcount` and the per-function `.maxnreg` / `.minnreg`. These do not directly
set a register count; they set a *target occupancy* from which ptxas **derives** a
register ceiling.

### The derivation

Given a thread-count-per-CTA `T` (from `maxntid`) and a desired blocks-per-SM `B`
(from `minnctapersm`), ptxas computes the register ceiling that *guarantees* `B`
CTAs of `T` threads coexist:

```
maxWarpsPerCTA          = ceil(T / 32)
maxWarpsPerSM           = maxWarpsPerCTA * B
maxWarpsPerSubPartition = roundUp(maxWarpsPerSM, 4) / 4            // 4 sub-partitions
maxWarpRegsPerSubPart   = 65536 / (4 * 32)                        // = 512 warp-wide regs

maxRReg = roundDown8( maxWarpRegsPerSubPart / maxWarpsPerSubPartition )
```

If the part enforces a tighter **per-CTA** register file, a second ceiling is taken
and the smaller wins (warps-per-CTA first rounded up to the 4 sub-partitions, to
match the hardware's per-CTA register-consumption check):

```
maxWarpsPerCTA'   = roundUp(maxWarpsPerCTA, 4)
ctaRegCeiling     = roundDown8( regFilePerCTA / (4 * maxWarpsPerCTA' * 32) )
maxRReg           = min( maxRReg, ctaRegCeiling )
```

Worked example, SM80, `__launch_bounds__(256, 4)`:

- `maxWarpsPerCTA = 256/32 = 8`
- `maxWarpsPerSM  = 8 * 4 = 32`
- `maxWarpsPerSubPartition = 32 / 4 = 8`
- `maxWarpRegsPerSubPartition = 512`
- `maxRReg = roundDown8(512 / 8) = roundDown8(64) = 64`

So `__launch_bounds__(256, 4)` instructs ptxas to keep register usage at or below
**64 registers/thread**, which is precisely the count that lets four 256-thread CTAs
(32 warps) live on one SM. Asking for `(256, 8)` would halve it to 32, and `(1024,
2)` (32 warps, same as `(256, 8)`'s warp count but in two CTAs) likewise yields 32.

### `maxntid` alone — the multi-target table

When `.maxntid` is given **without** a valid `.minnctapersm`, ptxas cannot pick a
single occupancy point, so it builds a **register-target table** indexed by
blocks-per-SM: entry *i* is the largest register count that still permits *(i+1)*
CTAs of the given size to be resident. The allocator then chooses the entry that
best balances register pressure against occupancy for the actual code. The
decision is summarized by the directive-validity table ptxas applies:

| `.maxntid` | `.minnctapersm` | derived `maxrreg`? | action |
|---|---|---|---|
| absent | absent | no | use the global `-maxrregcount` / arch max |
| absent | present | no | use the global ceiling (minnctapersm needs maxntid) |
| **valid** | absent | no | **build the per-CTA-count register-target table**, leave the hard ceiling open |
| **valid** | **valid** | **yes** | **use the single computed register ceiling** |
| invalid | (any) | no | fall back to the global ceiling |

`-maxrregcount N` (and PTX `.maxnreg N`) take priority over the launch-bounds
derivation: they set the hard ceiling directly and the occupancy that follows is
whatever that ceiling produces. They also conflict with `minnctapersm`/`maxntid` —
ptxas ignores the launch-bounds occupancy request when an explicit register cap is
present, because the two are competing specifications of the same quantity.
Specified counts are clamped to `[archMin=32, archMax=255]` per thread; a request
below the architectural minimum is bumped up with a diagnostic, above the maximum
clamped down.

## The register-pressure target the allocator chases

Occupancy feeds back into register allocation through a **register-pressure
target** — the register count ptxas tries to keep the kernel under. It is *not* the
optimization-effort budget (a separate compile-time throttle that drops to `-O0`
after a fixed number of references); it is a concrete per-thread register ceiling
derived from the desired occupancy step.

The allocator's natural register demand is its **fat point** — the maximum live
register count over the program, i.e. the register width the code wants if
unconstrained. ptxas compares the fat point against the occupancy cliffs and, when
the fat point sits just above a cliff, drives the allocator (and rematerialization)
to claw the count back down to the next occupancy step:

- The target table is seeded at each **occupancy-increase point** in the range
  `[reducedTarget, fatPoint)` — every 8-register boundary that buys another resident
  warp — plus **half-occupancy** intermediate targets between them, capped at a
  fixed trial count.
- Rematerialization (recomputing a value instead of keeping it live) is throttled by
  a cost model that **backs off when the kernel is resource-bound or
  latency-bound**: there is no point spilling/rematerializing to chase a register
  target if doing so cannot actually move occupancy up a step, or if the kernel is
  already memory-latency-limited rather than occupancy-limited.
- A user `-maxrregcount` / `.maxnreg`, when present, short-circuits all of this: the
  single user ceiling *is* the target.

The connection to the allocator's internals — the fat-point coloring, the
register-file banking, the spill machinery — is on
[Register Allocation](../regalloc/overview.md). The point here is that **occupancy is
an input to register allocation, not an output**: ptxas decides how many warps it
wants resident, converts that to a register ceiling on the 8-register grid, and then
allocates to fit.

## Work distribution — grid to CTAs to SMs

The compiler's resource accounting answers "how many CTAs fit on one SM"; the
**compute work distributor** answers "which SM does each CTA go to". A launched grid
is a 3-D array of CTAs; the distributor streams them onto SMs as slots free up.

The distributor is governed by a fixed warp cap and a CTA-slot pool:

- The work distributor carries a **max-warps-per-SM** register whose reset value is
  **64** (`0x40`), **byte-identical from Volta through Hopper**. This is the
  hardware ceiling the compiler's `maxWarpsPerSM` mirrors — the distributor will not
  oversubscribe an SM past 64 warps even if registers and shared memory would allow
  more. On parts that expose 48 or 32 warps the effective cap is lower, but the
  distributor's architectural maximum is 64.
- The compute scheduler maintains a pool of **CTA slots** (the 32-or-16 `maxCTAsPerSM`
  limit) and per-SM free-slot tracking; CTAs are handed to SMs with free slots until
  the grid is drained. A separate batch limiter bounds in-flight work.
- CTAs are assigned **greedily as resources free**, not statically pre-partitioned:
  an SM that finishes a CTA early pulls the next available CTA from the grid, so load
  naturally balances across SMs of differing progress. There is no guaranteed
  CTA-to-SM mapping and no architected execution order between CTAs — only the
  resource caps and the launch-validation checks constrain residency.
- On parts that engage **CTA-steering** (binding all warps of a CTA to one of the
  two SM partitions, e.g. for cache locality), the distributor restricts a CTA's
  warps to a single partition; this is exactly the case where ptxas applies the
  tighter per-CTA register ceiling and rounds warps-per-CTA up to the 4
  sub-partitions, matching the hardware's CTA-steering register/warp launch check.

The flow end to end:

```
grid (CTAs)
   │  compute work distributor: stream CTAs onto SMs with free slots
   ▼
SM  ─ holds up to min(regLimit, warpSlots=64/48/32, ctaSlots=32/16, smemLimit) CTAs
   │  warps bound to one of 4 sub-partitions (register-file slice + scheduler)
   ▼
warp ─ single-issue per sub-partition, scoreboard-scheduled
        (see Execution Model for the per-warp issue rule)
```

## Per-architecture resource constants

The arithmetic above is invariant; these constants are what each generation feeds
into it. (Blackwell parts are outside the range of the decoded constant tables and
are omitted here rather than guessed.)

| Arch | Warps/SM | CTAs/SM | Reg file/SM | Reg file/CTA | Reg gran. | Max regs/thr | smem/SM |
|---|--:|--:|--:|--:|--:|--:|--:|
| SM70 (Volta) | 64 | 32 | 65,536 | 65,536 | 8 | 255 | 96 KB |
| SM72 (Volta) | 32 | 16 | 65,536 | 32,768¹ | 8 | 255 | — |
| SM75 (Turing) | 32 | 16 | 65,536 | 65,536 | 8 | 255 | 64 KB |
| SM80 (Ampere) | 64 | 32 | 65,536 | 65,536 | 8 | 255 | 164 KB |
| SM82 (Ampere) | 64 | 32 | 65,536 | 32,768¹ | 8 | 255 | — |
| SM86 (Ampere) | 48 | 16 | 65,536 | 65,536 | 8 | 255 | 100 KB |
| SM87 (Ampere) | 64 | 32 | 65,536 | 65,536 | 8 | 255 | 164 KB |

¹ The per-CTA register-file cap halves on the cache-steered variants; it can bind
before the per-SM file does and is enforced by the hardware per-CTA
register-consumption check.

Invariant across all rows: **4 sub-partitions**, **32 threads/warp**, **8-register
per-thread granularity** (256-register warp-wide unit), **253 user-visible
registers** (255 minus the 2 reserved), and a work-distributor warp cap of 64.

The two splits that move occupancy between generations are (1) the **warp/CTA-slot
counts** — SM86 dropping to 48 warps / 16 CTAs is the notable Ampere outlier — and
(2) the **shared-memory capacity**, which sets the smem-limited CTA term. The
register arithmetic itself never changes.

## Validation against ptxas-emitted SASS

The model reproduces what ptxas reports and emits. Compiling a register-heavy kernel
with and without `__launch_bounds__` / `-maxrregcount` and reading the emitted cubin
shows:

- The `EIATTR_REGCOUNT` and `EIATTR_MAX_THREADS` / `EIATTR_REQNTID` attributes ptxas
  writes match the launch-bounds derivation: `__launch_bounds__(256,4)` on SM80
  produces a kernel whose register count is held at or below 64; `(256,8)` at or
  below 32. Tightening `minnctapersm` lowers the register ceiling by exactly the
  occupancy-step factor.
- `-maxrregcount N` pins the register count at the rounded-up `N` regardless of the
  launch-bounds request, and the static occupancy ptxas reports follows the
  `4*floor(512/roundUp8(N))` curve in the table above.
- Register counts in emitted SASS are always a multiple of 8 minus the reserved
  pair, never an arbitrary value — the 8-register granularity is visible directly in
  `R`-register high-water marks across a sweep of kernels.

## Cross-References

- [Register Allocation](../regalloc/overview.md) — the fat-point allocator, the
  register-file banking, and how the register-pressure target shapes coloring and
  spilling.
- [Execution Model](execution-model.md) — the per-warp single-issue rule and the
  scoreboard model that runs *within* a resident warp.
- [Architecture Evolution](architecture-evolution.md) — the per-generation ISA and
  capability diffs that accompany these resource-constant changes.
- [Legality & Capabilities](legality-and-capabilities.md) — the register-alignment
  and out-of-range constraints the allocator must also satisfy.
