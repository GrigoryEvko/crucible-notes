# Memory Hierarchy & Ordering Model

> *Recovered from the decoded per-architecture SASS instruction tables in
> nvdisasm V13.1.115 (CUDA 13.1), cross-validated against real ptxas v13.x SASS
> emitted for SM75 (Turing) → SM90a (Hopper). Every numeric limit below was
> confirmed by assembling probe PTX and reading the resulting machine code or the
> exact ptxas diagnostic. Blackwell (SM100+) is decoder-only here and is called out
> where the model is known to extend.*

The instruction tables and the SASS that ptxas emits expose the GPU's memory system
as four orthogonal mechanisms a code generator must model: a **constant-bank file**
read by `LDC`/`ULDC`, a **shared/L1 unified scratchpad** addressed by `LDS`/`STS`, a
**two-level cache** controlled by `CCTL` and per-access modifiers, and a
**scope-graded ordering model** built from `MEMBAR`/`FENCE` and the strong/weak
qualifiers on every load, store, and atomic. This page is the hardware-facing
companion to the [Execution Model](execution-model.md) (which covers the
register-scoreboard and the orthogonal `MEM_SCBD` memory-pipe scoreboard) and the
[Instruction Encoding](instruction-encoding.md) (which covers the bit layout of the
`c[bank][offset]` operand).

## Constant banks

Constant memory is a file of up to **32 banks**, `c[0x0]` … `c[0x1f]`, selected by
the **5-bit bank field** at `58:54` of the instruction word (see
[Instruction Encoding](instruction-encoding.md#immediates-and-constant-bank-addressing)).
The in-bank offset is the **14-bit field at `53:40`**, interpreted word-addressed:
14 bits × 4 bytes = a 16-bit byte range, so **each bank spans 64 KB (`0x10000`)**.
ptxas enforces exactly this: a kernel whose `__constant__` data exceeds `0x10000`
bytes is rejected with `File uses too much global constant data (… 0x10000 max)`.

Allocation inside a bank is granular to **16 bytes** (a 128-bit element): a 4-byte
scalar occupies a 4-byte-aligned slot, an 8-byte value an 8-byte-aligned slot, and
the bank cursor for compiler-injected constants advances in 16-byte units.

### Bank map (compute)

| Bank | ELF section | Role |
|---|---|---|
| `c[0x0]` | `.nv.constant0` | **Driver/compiler interface (DCI) + kernel parameters** — see layout below |
| `c[0x1]`, `c[0x2]` | — | Reserved by the driver; not directly bindable by the program |
| `c[0x3]` | `.nv.constant3` | **User `__constant__` memory** |
| higher banks | `.nv.constant<N>` | Bindless descriptor / texture-header banks, allocated on demand |

`c[0x0]` is the single most important bank: it carries the launch geometry, the
stack/window pointers, the global-memory descriptor, and the kernel parameter
block. Its byte layout is fixed and was read directly out of emitted SASS:

| Offset in `c[0x0]` | Contents |
|---|---|
| `0x0` / `0x4` / `0x8` | `blockDim.x` / `.y` / `.z` (CTA dimensions) |
| `0xc` / `0x10` / `0x14` | `gridDim.x` / `.y` / `.z` (grid dimensions) |
| `0x18` … | shared-window / local-window base addresses |
| `0x28` | initial **stack pointer** (loaded as the very first `LDC R1, c[0x0][0x28]`) |
| `0x30` … | grid-id, launch metadata |
| descriptor slot | 64-bit **global-memory descriptor** — `c[0x0][0x118]` on SM80–89, `c[0x0][0x208]` on SM90, `c[0x0][0x358]` on Blackwell |
| param base | **kernel parameters** — `c[0x0][0x160]` on SM75–89, `c[0x0][0x210]` on SM90, `c[0x0][0x360]` on Blackwell |

SM75 has no descriptor slot: the descriptor-addressed (`desc[UR][Ra.64]`) memory
forms first appear at SM80, and a Turing `LDG`/`STG` takes a bare 64-bit pointer.
The Blackwell offsets are identical on SM100 and SM120.

`blockIdx` and `threadIdx` are **not** in the bank — they come from the `S2R`/`CS2R`
special-register reads (`SR_CTAID`, `SR_TID`). Everything that is uniform across the
CTA (dimensions, pointers, parameters) lives in `c[0x0]` and is read with the
uniform-datapath `ULDC` whenever the value feeds a uniform register.

### Parameter-block size

The kernel parameter region inside `c[0x0]` is capped by ptxas:

| Architecture | Max parameter bytes | ptxas limit |
|---|---|---|
| SM75–89 (Turing → Ada) | 4 352 | `0x1100` |
| SM90 (Hopper) | 32 764 | `0x7ffc` |

Hopper's ~32 KB parameter region is what makes large grid-constant parameter
structures viable without a separate global spill; on Turing→Ada the 4 KB ceiling
forces large argument blobs into global memory.

### Addressing modes and bindless

A `c[bank][offset]` reference resolves through one of five addressing modes the
code generator attaches to each uniform symbol:

| Mode | Meaning |
|---|---|
| `IA` | **Immediate/absolute** — direct `c[bank][imm]`, the common case |
| `IL` | **Immediate-literal** in the unified constant space |
| `IS` | **Indexed** — bank/offset taken from a register |
| `ISL` | Indexed literal |
| `IB` | **Bindless** — the bank "header" is supplied in a register, no fixed bank bit |

For every non-bindless reference the code generator OR-bits `(1 << bank)` into a
per-program **constant-bank reference mask**. That mask is emitted in the program
header and tells the hardware which banks the kernel touches — the input to the
hardware's **constant-bank prefetch/binding**. Bindless references (`IB`) carry
their bank in a register and are deliberately excluded from the mask.

### Hardware constant prefetch

Two banks are auto-prefetched by the front-end constant pipeline. The compute work
distributor exposes two prefetch slots whose bank indices default to **bank 5 and
bank 6** (a 3-bit selector, so any of the low 8 banks can be chosen). This
two-slot prefetch is an **Ampere-onward** addition; Volta and Turing have no such
selector. Independently, only the **driver constant bank** and the **compiler's
own optimizer constant bank** are treated as guaranteed-resident — those are the
only banks ptxas will speculatively (unconditionally) load from, because a fault on
an unbound bank must be impossible. The per-SM constant cache that backs these
reads holds **8 lines on Volta** and grows to **16 lines on Hopper**.

## Shared memory

Shared memory is the on-chip scratchpad addressed by `LDS`/`STS` (and the uniform
forms). It is physically **32 banks wide, 4 bytes per bank** — the L1/shared data
RAM decodes a **5-bit bank index** from the address, i.e. 32 word-granular banks.
Successive 4-byte words map to successive banks; two threads of a warp that hit the
same bank with different addresses **conflict** and serialize, which is the entire
reason `STS.128`/`LDS.128` wide forms exist — one wide transaction touches four
consecutive banks per thread and amortizes the bank arbitration.

### Static vs dynamic shared memory

A kernel declares a fixed `.shared` block (static) and may additionally bind an
`extern .shared[]` array (dynamic) sized at launch. Both resolve to the same
`STS`/`LDS` against the per-CTA shared window; the dynamic array simply starts above
the static block. ptxas enforces a **static** ceiling per entry function:

| Architecture | Static `.shared` ceiling | ptxas limit |
|---|---|---|
| SM75–89 (Turing → Ada) | 48 KB | `0xc000` |
| SM90 (Hopper) | 227 KB | `0x38c00` |

The static ceiling is *not* the physical capacity — the extra capacity is reached
only through the runtime opt-in carveout (below), which the compiler cannot
statically allocate into.

### The L1/shared carveout

Shared memory and the L1 data cache share one physical SRAM, split at launch into a
small set of discrete carveout points:

| Generation | Selectable shared sizes (KB) | Default |
|---|---|---|
| Volta, Turing | 0, 8, 16, 32, 64, 96 | 64 |
| Ampere, Hopper | 0, 8, 16, 32, 64, 96, 100, 132, 164 | 64 |

The remainder of the SRAM is L1. Hopper's physical shared capacity reaches 228 KB
(227 KB usable, matching the `0x38c00` static ceiling); the table above lists the
hardware-selectable carveout split points the front-end accepts. Requesting a size
the hardware does not support raises a shared-memory-size class error
(`ILLEGAL_SHARED_MEMORY_SIZE…`, `ILLEGAL_SHARED_LOCAL_OVERLAP`).

**The default policy is a switch, not a slider.** Measured on sm_120 (100 KB max
shared per SM), a dependent-load chase shows the L1 knee move as the carveout
changes — and *any* nonzero dynamic shared-memory request collapses L1 identically,
whether the kernel asks for 8 KB or 96 KB:

| shared requested | 16 KB set | 24 KB set | 48 KB set | 96 KB set |
|---|--:|--:|--:|--:|
| none | 62 | 72 | 101 | 245 |
| 8 KB, default policy | 62 | **258** | 353 | 353 |
| 8 KB, carveout hint 0 (prefer L1) | 62 | 72 | 101 | **159** |

With no shared memory requested, L1 stays resident past 64 KB; asking for 8 KB
under the default policy drops the effective L1 to roughly 16 KB and sends a 24 KB
working set to L2 (72 → 258 cycles). Setting
`cudaFuncAttributePreferredSharedMemoryCarveout` to 0 restores the L1-preferring
split while still granting the shared allocation, which is the best configuration
measured here (159 cycles at a 96 KB footprint against 353 under the default). A
kernel that needs only a little shared memory should set the hint explicitly
rather than accept the driver's default.

### Shared-memory atomics

Atomics that target shared memory use the dedicated `ATOMS` opcode (distinct from
the global `ATOMG`/`REDG`): `atom.shared.add` lowers to `ATOMS.ADD`,
`atom.shared.cas` to `ATOMS.CAS`, and `red.shared.add` to `ATOMS.ADD RZ` (the same
`ATOMS` instruction with the result tied to `RZ`). There is no separate `REDS`
opcode and no scope field — shared atomics resolve in the per-CTA shared-memory
unit and do not participate in the global cache-coherence scopes below.

## Cache hierarchy

Since Volta the per-SM **L1 data cache is unified with shared memory** (the carveout
above). Beyond it sits the device-wide **L2**. The L2 line is **128 bytes = four
32-byte sectors**: the tag sector mask is a 4-bit field (`S0`–`S3`, all-enable
`0xf`), and the LSU/coalescer issues memory transactions in 32-byte sector
granularity, which is why naturally-aligned 32/64/128-bit accesses are the efficient
shapes.

### Cache-control: `CCTL` / `CCTLL` / `CCTLT`

Three explicit cache-control opcodes act on cache lines directly: `CCTL` (global),
`CCTLL` (local), and `CCTLT` (texture). Their sub-operation field enumerates:

| Sub-op | Meaning |
|---|---|
| `PF1` | Prefetch line into L1 |
| `PF1_5` | Prefetch into the L1.5 / mid-level cache |
| `PF2` | Prefetch line into L2 |
| `WB` | Write back one dirty line |
| `IV` | Invalidate one line |
| `IVALL` | Invalidate all lines (privileged variant `IVALLP`) |
| `RS` | Reset line (clear dirty without write-back) |
| `RSLB` | Reset line, look-aside buffer |
| `WBALL` | Write back all dirty lines (privileged `WBALLP`) |
| `PML2` / `DML2` / `RML2` | L2 prefetch / demote / discard-in-L2 (Ampere-onward) |

A `cacheType` selector narrows the target cache: **D** (data/L1), **U** (uniform),
**C** (constant), **I** (instruction), texture, shared, or the ray-tracing TTU.

`CCTLT` (texture) is narrower: `IVTH` invalidates one texture header, `IVALL`
invalidates all.

These are exactly what ptxas emits for the corresponding PTX: `prefetch.global.L1`
→ `CCTL.E.PF1`, `prefetch.global.L2` → `CCTL.E.PF2`, and `discard.global.L2` →
`CCTL.E.RML2`.

### Per-access modifiers

Pre-Volta architectures encoded the classic cache modifiers as a single load/store
field — `.ca` (cache all, L1+L2), `.cg` (cache global, L2 only / bypass L1), `.cs`
(cache streaming), `.cv` (cache volatile / don't cache) on loads, and `.wb`
(write-back), `.cg`, `.cs`, `.wt` (write-through) on stores. **Volta and later
decompose this into three orthogonal fields**: an *eviction priority* (`cop`), a
*strength* (`sem`), and a *scope* (`sco`). The legacy PTX modifier is no longer a
field of its own — it is *projected* onto those three. Assembling each modifier as a
one-variable probe for `sm_90a` and reading the emitted `LDG`/`STG` makes the
projection exact:

| PTX modifier | SASS load form | SASS store form | projected fields |
|---|---|---|---|
| `.ca` / `.wb` | `LDG.E.STRONG.SM` | `STG.E.STRONG.SM` | `cop=EN`, `sem=STRONG`, `sco=CTA` (printed `.SM`) |
| `.cg` | `LDG.E.STRONG.GPU` | `STG.E.STRONG.GPU` | `cop=EN`, `sem=STRONG`, `sco=GPU` |
| `.cs` | `LDG.E.EF` | `STG.E.EF` | `cop=EF`, `sem=WEAK`, no scope |
| `.lu` | `LDG.E.LU` | — | `cop=LU`, `sem=WEAK` |
| `.cv` / `.wt` | `LDG.E.STRONG.SYS` | `STG.E.STRONG.SYS` | `cop=EN`, `sem=STRONG`, `sco=SYS` |
| (default, weak) | `LDG.E` | `STG.E` | all fields at their bare value |

So the cache *all*/*global*/*volatile* distinction is carried by the **scope** field
(CTA / GPU / SYS strength), *streaming* is carried by the **eviction** field
(`cop=EF`, evict-first), and *last-use* is `cop=LU`. There is no separate "cache
class" bit on Volta-onward `LDG`/`STG` — the three fields below are the entire model.

### Eviction priority `cop` (Volta+)

On Volta and later, a load's residency hint is a 3-bit **eviction-priority** field
(`cop`) with six values, observable as the suffix on `LDG`/`STG`:

| Suffix | `cop` | Meaning |
|---|---|---|
| `.EF` | 0 | Evict first |
| `.EN` | 1 | Evict normal (default — printed bare) |
| `.EL` | 2 | Evict last |
| `.LU` | 3 | Last use (invalidate after read) |
| `.EU` | 4 | Evict-unchanged |
| `.NA` | 5 | No-allocate |

These are emitted directly — `ld.global.L1::evict_first` → `LDG.E.EF`,
`L1::evict_last` → `LDG.E.EL`, `L1::evict_unchanged` → `LDG.E.EU`,
`L1::no_allocate` → `LDG.E.NA`, `ld.global.lu` → `LDG.E.LU` (all reproduced from
isolated probes). An adjacent field controls **L2 sector promotion**
(`NOSP` / 64 B / 128 B / 256 B) — the width of the L2 fill triggered by a single
access. The `.E` decoration that prefixes every global form is a separate 1-bit
**extended-address** flag (a 64-bit pointer register pair); `LDG`/`STG`/`ATOMG`
always carry it, while `LDS`/`STS`/`LDC` (32-bit window addresses) never do.

## Memory ordering

Every ordered memory access carries two more fields: a 2-bit **strength** (`sem`)
and a 3-bit **scope** (`sco`). The strength is one of:

| `sem` | Value | Meaning |
|---|---|---|
| `CONSTANT` | 0 | compile-time-constant / read-only memory (no ordering) |
| `WEAK` | 1 | no inter-thread ordering (the bare `LDG.E` default) |
| `STRONG` | 2 | ordered with respect to scope (acquire on load, release on store) |
| `MMIO` | 3 | uncached, strongly ordered device access |

ptxas renders a strong ordered access as `LDG.E.STRONG.<scope>` /
`STG.E.STRONG.<scope>`, with the scope drawn from the graded set below. (An access
that is both `.EF` and weak prints just `LDG.E.EF`; the STRONG marker appears only
when the access is genuinely ordered.) Two behaviours were pinned by probe:

- **`.relaxed.<scope>` and `.acquire`/`.release`/`.acq_rel.<scope>` produce the
  *same* printed and encoded form** — e.g. `ld.global.relaxed.gpu` and
  `ld.global.acquire.gpu` both emit `LDG.E.STRONG.GPU` with identical bits. A
  single strong access at a given scope already provides the acquire/release
  half-fence on this hardware; the ordering distinction is folded into the
  scope+strength pair, not a separate acquire/release bit.
- **`mmio` retargets the addressing path**: `ld.global.mmio.relaxed.sys` emits
  `LDG.E.MMIO.SYS` over a *bare* `[Rn.64]` operand, bypassing the
  `desc[UR][Rn.64]` memory-descriptor addressing every cached form uses.

### Scope ladder

Scope is the nesting of thread sets across which an ordered access or fence is
observed, in increasing breadth:

```
CTA  <  SM  <  GPU  <  VC  <  SYS
```

- **`CTA`** — the thread block.
- **`SM`** — the streaming multiprocessor (relevant for clusters / co-resident CTAs).
- **`GPU`** — all CTAs on the device.
- **`VC`** — the video-/coherence domain (a peer-coherence tier above GPU).
- **`SYS`** — system scope, including the host and peers across the bus.

The default `sco` value differs by class, recovered from probes: a **load/store**
with no explicit scope is `WEAK` with no scope (bare `LDG.E`), but an **atomic or
reduction** with no explicit scope defaults to **`STRONG.GPU`**. The verified
atomic/reduction forms are `atom.global.add` → `ATOMG.E.ADD.STRONG.GPU`,
`atom.global.min.s32` → `ATOMG.E.MIN.S32.STRONG.GPU`, `atom.global.exch` →
`ATOMG.E.EXCH.STRONG.GPU`, and `red.global.add` → `REDG.E.ADD.STRONG.GPU`.
`atom.global.cas` is special: it lowers to `ATOMG.E.CAS.STRONG.GPU` but on a
**distinct opcode** (a separate primary byte) over bare `[Rn]` addressing —
compare-and-swap is its own SASS instruction, not a value of the atomic-op field.
The integer atomic-op field enumerates `ADD=0, MIN=1, MAX=2, INC=3, DEC=4, AND=5,
OR=6, XOR=7, EXCH=8`. Acquire/release atomics fold their ordering into the same
`STRONG.<scope>` pair (`atom.global.add.acq_rel.gpu` → `ATOMG.E.ADD.STRONG.GPU`).

A **shared-memory** atomic uses the scope-free `ATOMS` opcode
(`atom.shared.add` → `ATOMS.ADD`, `atom.shared.cas` → `ATOMS.CAS`), and a
**shared reduction** lowers to `ATOMS.ADD RZ` — the same `ATOMS` opcode with the
result register tied off to `RZ`, *not* a distinct `REDS` opcode.

### Fences: `MEMBAR` and `FENCE`

The fence opcode carries a 2-bit **semantics** field `membar_sem`
(`SC`=0 sequential-consistency, `ALL`=1 ordering, `NONE`=2, `MMIO`=3) and its own
3-bit scope field `membar_sco` with a numbering *distinct* from the load/store
`sco` (`CTA`=0, `SM`=1, `GPU`=2, `SYS`=3, `VC`=5). The exact lowerings, each
disassembled from a probe, are:

| PTX | SASS | Effect |
|---|---|---|
| `membar.cta` | `MEMBAR.SC.CTA` | sequential-consistency fence, CTA scope |
| `membar.gl` | `MEMBAR.SC.GPU` | SC fence, GPU scope |
| `membar.sys` | `MEMBAR.SC.SYS` | SC fence, system scope |
| `fence.sc.gpu` | `MEMBAR.SC.GPU` | SC fence at GPU scope |
| `fence.acq_rel.cta` | `MEMBAR.ALL.CTA` | acquire-release (ordering) fence at CTA scope |
| `fence.acq_rel.gpu` | `MEMBAR.ALL.GPU` | acq-rel fence at GPU scope |
| `fence.acq_rel.cluster` | `MEMBAR.ALL.GPU` + `ERRBAR; CGAERRBAR` | cluster fence: GPU membar plus the cluster error-pipe drain |
| `fence.proxy.tensormap` | `UTMACCTL.IV` | invalidate the TMA tensor-map proxy cache |

Note the consequence of the two `membar_sem` rules: a **`membar.*`** is always an
`SC` fence, but a **`fence.acq_rel.*`** is an `ALL` fence — so `membar.cta` and
`fence.acq_rel.cta` are *not* the same instruction (`MEMBAR.SC.CTA` vs
`MEMBAR.ALL.CTA`). `MEMBAR.SC` (sequential-consistency) is strictly stronger than
`MEMBAR.ALL` (plain ordering). The code generator may **demote** a fence whose effect is already
covered (e.g. a GPU-scope fence narrowed to CTA scope when no wider sharing exists,
or elided entirely when a subsequent fence subsumes it) — fence minimization is part
of codegen. A `MEMBAR.SYS` may additionally require an off-deck drain, and on the
widest scopes a fence pairs with `ERRBAR`/`CGAERRBAR` (the error-barrier that drains
the memory error pipe at scope ≥ `VC`).

The async-copy path uses a different completion mechanism. `cp.async.ca` lowers to
`LDGSTS.E.<width>` (the bytes flow through L1 into shared), `cp.async.cg` to
`LDGSTS.E.BYPASS.<width>` (bypassing L1, matching `.cg` semantics); the group
arms an async-copy scoreboard via `LDGDEPBAR` and is waited on with `DEPBAR.LE`.
This async ordering is orthogonal to the `MEMBAR` scopes and is sequenced by the
memory-pipe scoreboard documented in the
[Execution Model](execution-model.md#scoreboard--barrier-model).

## The LSU / coalescer as modeled

The load/store unit is what the code generator targets with these forms, and three
properties shape the SASS it produces:

1. **Sector granularity.** All global traffic is in 32-byte sectors; the L2 line is
   four sectors (128 B). Aligned, contiguous accesses fill whole sectors and waste
   no bandwidth.
2. **Wide forms.** `LDG.E.128` / `STG.E.128`, `LDS.64`/`LDS.128`, `STS.128` pack a
   16-byte transaction per thread — fewer, wider memory instructions reduce
   instruction count and bank/sector arbitration. The vectorizer emits these
   whenever the address alignment and value layout permit.
3. **Uniform datapath.** Values uniform across the warp are read with `ULDC` /
   `UR`-register forms and addressed once per warp rather than per thread; the entire
   `c[0x0]` interface block is consumed this way.

## Architecture summary

| Property | Turing (SM75) | Ampere (SM80/86) | Ada (SM89) | Hopper (SM90) |
|---|---|---|---:|---|
| Constant bank size | 64 KB | 64 KB | 64 KB | 64 KB |
| `c[0x0]` param base | `0x160` | `0x160` | `0x160` | `0x210` |
| Max kernel-param bytes | 4 352 | 4 352 | 4 352 | 32 764 |
| Static `.shared` ceiling | 48 KB | 48 KB | 48 KB | 227 KB |
| Shared carveout max | 96 KB | 164 KB | 164 KB | 228 KB |
| HW constant-prefetch slots | — | 2 (banks 5,6) | 2 | 2 |
| L2 line / sector | 128 B / 32 B | 128 B / 32 B | 128 B / 32 B | 128 B / 32 B |
| Scope ladder | CTA·SM·GPU·VC·SYS | same | same | same |

The decoded encoding (bank width, scope field, eviction field, CCTL sub-ops) is
identical across the post-Volta tables; what differs is the **capacity** ptxas will
allocate into (`0x1100` → `0x7ffc` parameter bytes, 48 KB → 227 KB static shared,
96 KB → 228 KB carveout) and the **constant-prefetch** machinery added at Ampere.
The ordering model — strong/weak × the five-level scope ladder, `MEMBAR.SC`/`.ALL`,
`CCTL`, the six eviction priorities — has been stable since Volta and, on the
decoder evidence, extends unchanged into Blackwell.

## Cross-References

- [Instruction Encoding](instruction-encoding.md) — the bit layout of the
  `c[bank][offset]` operand and the `R_CUDA_CONST_FIELD*` relocators.
- [Execution Model](execution-model.md) — the register scoreboards and the
  orthogonal `MEM_SCBD` memory-pipe scoreboard that sequences `MEMBAR`/`LDGSTS`.
- [Legality & Capabilities](legality-and-capabilities.md) — the address-range and
  alignment constraints on memory operands.
