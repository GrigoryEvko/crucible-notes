# Tensor-Core Pipeline — MMA, Fragments & wgmma

> *Recovered from the decoded per-architecture SASS instruction tables in
> nvdisasm V13.1.115 (CUDA 13.1), cross-validated against the ptxas v13.0.88
> scheduling tables, and **round-trip-validated against real ptxas output** — every
> mnemonic and shape name below was reproduced by assembling PTX `mma`/`wmma`/`wgmma`
> with ptxas and disassembling with nvdisasm. Facts and models only; the verbatim
> NVIDIA tables are not redistributed.*

Every NVIDIA GPU since Volta carries a dedicated **matrix unit** alongside the FMA,
ALU, FP16 and FP64 datapaths. ptxas emits its work as a small family of SASS opcodes
— `HMMA`, `IMMA`, `BMMA`, `DMMA`, `QMMA` (warp-level), and `HGMMA`/`IGMMA`/`BGMMA`
(Hopper warpgroup-level) — plus the matrix-move helpers `LDSM`, `STSM` and `MOVM`.
This page is the machine-level model of that unit: how a PTX `mma`/`wmma`/`wgmma`
lowers to SASS, how the 32 lanes of a warp (or 128 of a warpgroup) partition the
operand tiles into per-thread register fragments, how the shared-memory **matrix
descriptor** feeds the Hopper warpgroup unit, and how the asynchronous accumulator
is committed and waited on.

The companion compiler-side material is the
[GMMA/WGMMA Pipeline](../passes/gmma-pipeline.md) pass (the two ptxas phases that
fix up the warpgroup sequence) and the
[Latency Model](../scheduling/latency-model.md) (the per-shape result latencies). The
binary encoding of these opcodes in the 128-bit instruction word is in
[Instruction Encoding](instruction-encoding.md).

---

## 1. The matrix unit in the SM

The matrix unit is one of the SM's coupled math pipes, sitting beside the scalar
FMA/ALU/FP16/FP64 lanes. The hardware microarchitecture state recovered from the
SM-block registers distinguishes the matrix op *types* explicitly: the per-SM
micro-scheduler carries dedicated **MMA wait fields** (`MMA_S_WAIT`, `MMA_M_WAIT`,
`MMA_L_WAIT` — short/medium/long pending classes) and per-op-type pipeline-jitter
controls for the full taxonomy:

> `HMMA16`, `HMMA32`, `HMMAE8M10`, `IMMA`, `IMMA8832`, `BMMA`, `DMMA`

— the FP16-accumulate, FP32-accumulate, TF32-mantissa half, the integer unit and its
8×8×32 fast path, the binary unit, and the FP64 tensor unit. On Volta the matrix unit
exists (**8 tensor cores per SM**) but is not exposed as a separate power/wakeup
domain; **Ampere is the first generation where the `IMMA` pipe is its own wakeup
domain** in the SM, reflecting the redesign of the operand path that the Ampere shapes
(`16816`, `16832`) require. The TPC partitioning is constant across every generation
from Volta through Hopper: **2 SMs per TPC**.

The matrix pipe is **coupled** (fixed-latency, scoreboard-free for same-pipe
forwarding) for the warp-level opcodes and **decoupled** (async, scoreboard-tracked)
for the Hopper warpgroup opcodes. ptxas models four distinct matrix resources with
their own per-shape issue (resource-busy) rates and a redirected functional-unit
class for each:

| Resource class | What it covers | Lane structure (Ampere model) |
|---|---|---|
| `HMMA` (1688 / 16816) | FP16/BF16/TF32 multiply, F16 or F32 accumulate | 4 lanes, 1 issue/lane |
| `IMMA` (16816 / 16832) | INT8/INT4 multiply, INT32 accumulate | 4 lanes, 1 issue/lane |
| `DMMA` (884) | FP64 multiply-accumulate | 2 lanes, 1 issue/lane, 64-bit |
| `fp16_mma` | the legacy Volta `884` FP16 path | 3 lanes, 1 issue/lane |

Two architectural sharing facts fall straight out of the resource model:

1. **`IMMA` shares the issue rate of the matching `HMMA`.** The 1× collect group
   (`mma_1x_collect`) holds `HMMA.1688` and `IMMA.16816` together; the 2× collect
   group (`mma_2x_collect`) holds `HMMA.16816` and `IMMA.16832`. Integer and
   half-precision matrix throughput are therefore identical per element-pair.
2. **`DMMA` shares the matrix dispatch unit with `HMMA`** — FP64 tensor work issues
   through the same dispatch port, just at the FP64 resource-busy rate.

The matrix unit also has its own **virtual issue queue** (a redirected queue distinct
from the FMA/ALU queues), so `HMMA` instructions queue and retire independently of
scalar math in the warp's issue model. The same-pipe forwarding rule applies: an
`HMMA` whose accumulator feeds the next `HMMA` pays **0** hazard cycles, not the full
result latency — back-to-back accumulation is free, which is exactly what a matrix
loop wants. (See [Latency Model](../scheduling/latency-model.md#result-forwarding-bypass).)

---

## 2. Warp-level MMA: shapes, types and lowering

A warp-level PTX `mma.sync`/`wmma` is issued cooperatively by all 32 lanes; each lane
holds a slice of the A, B, C and D tiles in its own registers. ptxas selects one of
five warp-level opcodes by the operand element type:

| Element type | SASS opcode | Accumulate |
|---|---|---|
| F16, BF16, TF32 | `HMMA` | F16 or F32 |
| INT8, INT4 | `IMMA` | INT32 |
| 1-bit (binary) | `BMMA` | INT32 (popcount) |
| FP64 | `DMMA` | FP64 |
| FP8 (E4M3 / E5M2) | `QMMA` | F32 |

### 2.1 Shape naming

SASS names a shape by a compact `MNK` token that is **not** the PTX shape verbatim —
it is the on-wire tile the hardware executes, with the K dimension expressed per
32-bit operand lane. The validated mapping (ptxas → nvdisasm) is:

| PTX shape (type) | SASS mnemonic |
|---|---|
| `mma.m16n8k16.f16` | `HMMA.16816.F32` |
| `mma.m16n8k8.f16` | `HMMA.1688.F32` |
| `mma.m16n8k16.bf16` | `HMMA.16816.F32.BF16` |
| `mma.m16n8k8.tf32` | `HMMA.1688.F32.TF32` |
| `mma.m8n8k4.f16` (Volta form) | `HMMA.884.F32.F32.STEP{0..3}` |
| `mma.m8n8k16.s8` | `IMMA.8816.S8.S8` |
| `mma.m8n8k32.s4` | `IMMA.8832.S4.S4` |
| `mma.m16n8k32.s8` | `IMMA.16832.S8.S8` |
| `mma.m8n8k128.b1.xor.popc` | `BMMA.88128.XOR.POPC` |
| `mma.m8n8k4.f64` | `DMMA.884` |
| `mma.m16n8k32.e4m3` | `QMMA.16832.F32.E4M3.E4M3` |

The full SASS shape vocabularies the disassembler will print:

- **HMMA** — `884`, `1688`, `16816`, `1684`, `16832`. The dst suffix is `.F32` or
  `.F16`; alt-format suffixes `.E8M7` (TF32-style) and `.E8M10` distinguish the
  reduced-mantissa inputs; `.BF16` / `.TF32` appear for the corresponding inputs.
- **IMMA** — `8816`, `8832`, `8864`, `16816`, `16832`, `16864`, `168128`. The two
  type suffixes are the A and B operand formats, each one of `U8`, `S8`, `U4`, `S4`
  (`U16`/`S16` are also encodable). Operands carry explicit `.ROW`/`.COL` modifiers.
- **BMMA** — `88128`, `168128`, `168256`. The logical-op suffix is the source op
  (`XOR`, `AND`) followed by the accumulate op, which is always `POPC` (population
  count) — binary MMA computes `popc(A op B)`.
- **DMMA** — `884` only, with a rounding-mode field (`ROUND`/`FLOOR`/`CEILING`/`TRUNC`).
- **QMMA** — the FP8 family (Ada `sm_89` onward); the type suffixes name the A and B
  FP8 formats (`E4M3`, `E5M2`).

### 2.2 Operand modifiers

Every warp-level MMA carries:

- **`.ROW` / `.COL`** on the A and B operands — the row-/column-major reading of each
  input fragment. (`IMMA` prints both explicitly; `HMMA.884` prints A and B layout in
  the mnemonic.)
- **`.SATFINITE` / `.SAT`** — saturating result clamp (HMMA float / IMMA integer).
- **`.NegA` / `.NegB`** — input negation, used by the compiler to fold sign flips.
- **sparsity** — `.SP` plus a metadata register and a 2-bit selector immediate
  (see §5).

---

## 3. Per-thread register fragments

The defining property of a warp-level MMA is that the A/B/C/D tiles are **distributed
across the 32 lanes' registers** — no single lane holds a whole matrix. ptxas knows
the exact register-vector length of each fragment per shape, and lays the D, A, B, C
vectors out contiguously in the instruction's register window. The fragment vector
lengths (in 32-bit registers per lane) recovered from the lowering tables are:

### IMMA fragment vector lengths (per lane)

The operand window is laid out `D, A, B, C` with the following start offsets and
implied lengths (dense, 8-bit operands):

| SASS shape | D start | A start | B start | C start | end | ⇒ A,B,C,D lengths |
|---|---|---|---|---|---|---|
| `8816`  | 0 | 2 | 3 | 4  | 6  | A=1, B=1, C=2, D=2 |
| `16816` | 0 | 4 | 6 | 7  | 11 | A=2, B=1, C=4, D=4 |
| `16832` | 0 | 4 | 8 | 10 | 14 | A=4, B=2, C=4, D=4 |

For 4-bit (`U4`/`S4`) operands the same offsets apply one shape down (`8832`, `16832`,
`16864`). Sparse variants pack the A operand to half width and shift B/C accordingly
(e.g. dense `16832` A,B = 4,2 → sparse `16832` A,B = 4,2 with C shifted to start 8).

### HMMA fragment starts (per lane)

The `884` Volta form lays A/B/C at operand offsets `{4,6,8}` (non-step) or `{2,4,6}`
(per step); the SuperHMMA shapes (`1688`, `16816`, `16832`) carry their own per-shape,
per-alt-format, per-sparsity start tables. The accumulator (C) and result (D) of the
common Ampere `m16n8k16` F32 shape occupy **4 registers per lane** each; A is 4, B is 2
— matching the 32-lane partition of a 16×16 A tile and 16×8 B tile into FP16 pairs.

### BMMA fragment starts (per lane)

| SASS shape | D | A | B | C | end |
|---|---|---|---|---|---|
| `88128`  | 0 | 2 | 3 | 4  | 6  |
| `168128` | 0 | 4 | 6 | 7  | 11 |
| `168256` | 0 | 4 | 8 | 10 | 14 |

These tables are what let ptxas allocate a single contiguous register range for an
MMA and address each fragment by a fixed offset — and what a reimplementation needs
to reproduce the exact register packing nvcc/ptxas emit.

---

## 4. The Volta `884` STEP decomposition

The original Volta/Turing FP16 form, `mma.m8n8k4`, is the one warp-level MMA that does
**not** map to a single SASS instruction. ptxas expands it into a **macro** of 2 or 4
`HMMA.884` sub-instructions, one per accumulator pair, distinguished by a `.STEP0`…
`.STEP3` suffix:

```
HMMA.884.F32.F16.STEP0  R8,  R16.reuse.ROW, R14.reuse.COL, R4.reuse
HMMA.884.F32.F16.STEP1  R10, R16.reuse.ROW, R14.reuse.COL, R4
HMMA.884.F32.F16.STEP2  R4,  R16.reuse.ROW, R14.reuse.COL, R6.reuse
HMMA.884.F32.F16.STEP3  R6,  R16.ROW,       R14.COL,       R6
```

All steps share the **same** A (`R16.ROW`) and B (`R14.COL`) operands — the `.reuse`
flag keeps them resident in the operand-reuse cache across the macro so the register
file is read once. Each step writes a different accumulator pair (`R8`, `R10`, `R4`,
`R6`). The step count depends on the accumulate width:

- **F32 destination** → 4 steps (`STEP0..STEP3`).
- **F16 destination** → 2 steps (`STEP0..STEP1`).

ptxas treats the whole run as a unit for scheduling. Its **scoreboard optimisation for
HMMA** combines the `&req` (operand-ready) scoreboards onto `STEP0` and the `&wr`
(result) scoreboard onto the last step, so the four issues run with no intra-macro
stall — the macro behaves like one long-latency tensor op. The optimisation is
disabled only when an intervening non-HMMA instruction within the macro would write a
scoreboard a later step depends on.

The newer **SuperHMMA** shapes (`1688`/`16816`/`16832`) are single instructions — they
are *not* expanded into steps, which is the visible signature of the Turing→Ampere
matrix-unit redesign: a wider unit that retires a whole `m16n8k16` in one issue. (This
is the same redesign reflected in the hardware exposing the `IMMA` wakeup domain on
Ampere.)

---

## 5. Sparsity (`.SP`)

Structured (2:4) sparsity adds a **metadata operand** and a **selector immediate** to
the MMA. The sparse form keeps the dense accumulator and B tile but compresses the A
tile to its non-zero half, so the A fragment vector is half-width (§3). Ground truth:

```
mma.sp.m16n8k32.f16   →   HMMA.SP.16832.F32 R12, R8, R12, RZ, R6, 0x0
```

The trailing `R6` is the **sparsity metadata register** (the 2:4 index map) and `0x0`
is the metadata-selector immediate (`spId`). ptxas appends the metadata as a single
`u32` scalar operand just before the instruction's shape/info word; its operand index
is fixed at "second from last". Two metadata layouts are encodable, selected by the
`spformat` field:

| `spformat` | Meaning |
|---|---|
| `TID`       | metadata addressed by lane (thread) id |
| `REGOFFSET` | metadata addressed by register offset |

Sparse variants exist for `HMMA`, `IMMA` and the Hopper `HGMMA`/`IGMMA`. The
standalone metadata-preparation opcodes are `SPMETADATA` / `GENMETADATA`.

> *Note:* ptxas advises using the PTX `.sp::ordered_metadata` modifier rather than the
> bare `.sp` form, warning that the latter "is expected to have substantially reduced
> performance on some future architectures" — the SASS encoding is the same `*.SP.*`
> opcode either way; the difference is the metadata-ordering contract.

---

## 6. Hopper warpgroup MMA (`HGMMA` / wgmma)

Hopper redesigns the matrix unit a second time. A `wgmma.mma_async` is issued by a
**warpgroup** — 4 consecutive warps, **128 threads** — cooperatively, and it is
**asynchronous**: the instruction dispatches work to the tensor core and returns
immediately, with the accumulator filling in the background. ptxas selects an
`HGMMA` / `IGMMA` / `BGMMA` opcode by element type (the half/int/binary warpgroup
families), each with the same async structure.

The warpgroup-MMA shapes are `m64nNk{8,16,32}` — the M dimension is **fixed at 64**
(the warpgroup's row count), N ranges over `{8,16,24,32,…,256}` in steps of 8, and K
is 8/16/32 by element width. Decoded shape codes the disassembler prints include
`641616` (m64n16k16), `64816`, `643216`, etc.

### 6.1 The shared-memory matrix descriptor

The decisive new mechanism is that A and B may live **in shared memory** instead of in
registers. When they do, the operand is **not** a register fragment vector — it
collapses to a **single 64-bit descriptor** held in a **uniform register** (`URa` or
`URb`). The SASS form is:

```
HGMMA.size.dstfmt{.srcfmt}  Rd, {-}Ra, URb{.negB}{.tnspB}, Rc, {!}UPp{, gsb}
```

`URb` carries the opaque 64-bit shared-memory matrix descriptor. Two per-operand
modifiers ride on it:

- **`.tnspB`** (and `.tnspA`) — the transpose attribute of the shared-memory matrix
  (column- vs row-major reading of the SMEM tile).
- **`.negB`** — input negation.

The descriptor itself is a hardware-defined 64-bit word encoding the operand's
shared-memory **base address**, **leading-dimension** and **stride** byte offsets, and
the **swizzle mode** of the SMEM layout. ptxas treats it as an opaque value: it is
materialised into the uniform register by preceding code (typically from a
`wgmma.descriptor`-style construction) and consumed whole. The compiler's only
interaction with its internals is the transpose flag; the base/leading-dim/stride/
swizzle bit-fields are produced and interpreted by the hardware, so they are not
reconstructed here beyond the four fields the ISA exposes.

ptxas picks one of three operand-placement encodings depending on which sources are in
uniform registers:

| Source placement | SASS form |
|---|---|
| A and B both SMEM (uniform reg) | `HGMMA … URa, Rc` |
| A in registers, B in SMEM | `HGMMA … Ra, URb, Rc` |
| A in SMEM, B in registers | `HGMMA … URa, Rb, Rc` |

The destination/result register count follows the same fragment table as the warp-MMA
families: for a given `(64, N, K, srcfmt, dstfmt)` the accumulator occupies **N/2**
registers per lane (e.g. `m64n128k16` F32 → 64 accumulator registers per lane in the
warpgroup partition), the register-sourced A is 4 registers, and a SMEM-sourced
operand is the single descriptor slot.

### 6.2 The asynchronous accumulator: arrive / wait / depbar

Because the accumulator fills asynchronously, ordering against any consumer of the
result is enforced by a small set of warpgroup-barrier instructions. ptxas lowers the
PTX async-group protocol to three SASS forms:

| PTX | SASS | Role |
|---|---|---|
| `wgmma.commit_group` / `wgmma.fence` region close | `WARPGROUP.ARRIVE` | marks the warpgroup's async work committed |
| `wgmma.wait_group N` | `WARPGROUP.DEPBAR #N, gsb` | wait until **≤ N** GMMA groups remain pending |
| (full barrier) | `WARPGROUP.WAIT` | wait for all warpgroup async work |

`WARPGROUP.DEPBAR` carries a 4-bit pending-group count (`gsbcnt`) and a GMMA-scoreboard
selector (`gsb`); it is the commit-group/wait-group analogue — the warp stalls until
the number of in-flight GMMA groups drops to the requested threshold. Each `HGMMA`
carries its own `gsb` scoreboard field that the DEPBAR waits on. There is also a
`WARPGROUPSET` form (uniform-register-sourced) used to configure the warpgroup state.

ptxas treats `HGMMA` and `WARPGROUP` as instructions that **may not change the number
of active warp threads** — the full 128-thread warpgroup must participate — and the
enclosing `WARPGROUP` acts as a memory-ordering fence so the async SMEM reads of the
GMMAs are never reordered illegally. The two ptxas phases that enforce the sequencing
(accumulator-register liveness, fence/commit/wait insertion, and serialisation when the
constraints are violated) are documented in the
[GMMA/WGMMA Pipeline](../passes/gmma-pipeline.md) pass.

---

## 7. Matrix-move helpers: `LDSM`, `STSM`, `MOVM`

Feeding the matrix unit from shared memory and transposing tiles uses three dedicated
opcodes, all validated against ground truth:

| PTX | SASS | Meaning |
|---|---|---|
| `ldmatrix.m8n8.x4.shared.b16` | `LDSM.16.M88.4` | load 4 × 8×8 16-bit matrices from SMEM into the warp's fragment registers |
| `stmatrix.m8n8.x1.shared.b16` (Hopper) | `STSM.16.M88` | store an 8×8 16-bit matrix from registers to SMEM |
| `movmatrix.m8n8.trans.b16` | `MOVM.16.MT88` | in-register transpose of an 8×8 16-bit matrix |

`LDSM` (the "read-matrix" opcode) encodes an element size (`16`, or sub-byte packing
forms `U4TO8`/`S4TO8`/`U2TO4`/`S2TO4`), a matrix shape (`.M88` / `.MT88`-transposed /
`.M816` / `.M832`) and a matrix count (`1`/`2`/`4`). `MOVM` shares the size/shape
vocabulary for the in-register transpose path. These are how a kernel stages an A/B
tile through SMEM and hands the warp its per-lane fragments without an explicit
shuffle.

---

## 8. Blackwell (`sm_100`+): tcgen05 — binary-RE-only

Hopper's warpgroup `*GMMA` family is **removed** at Blackwell and replaced by
**tcgen05**: the `UTC*MMA` opcodes (`UTCHMMA`, `UTCIMMA`, `UTCQMMA`, `UTCOMMA`) plus a
dedicated **tensor memory (TMEM)** addressed by `LDTM`/`STTM`/`LDT`/`STT`. The decoded
nvdisasm tables show tcgen05 MMAs are **async-issued** with **1-CTA and 2-CTA**
variants (two CTAs cooperatively feeding one tensor core) and operand sources that are
either a tensor-memory operand (`_tmem`) or a global descriptor (`_gdesc`) — a
generalisation of the Hopper SMEM-descriptor idea to a dedicated on-chip tensor
memory. tcgen05 + TMEM + 2-CTA paired issue exist **only on the datacenter/Jetson
Blackwell parts** (`sm_100`/`103`/`110`); consumer Blackwell (`sm_120`) lacks TMEM and
does FP4/FP6 tensor through the legacy per-warp `OMMA` path instead. This is the third
matrix-unit redesign in four generations.

These Blackwell details are recovered purely from the CUDA 13.1 nvdisasm instruction
tables and the ptxas legality gates; the full op taxonomy and the datacenter/consumer
split are catalogued in [Architecture Evolution](architecture-evolution.md#a-tensor-cores-bifurcate-at-blackwell).

---

## 9. Summary — the matrix-unit model at a glance

- **Five warp-level opcodes** (`HMMA`/`IMMA`/`BMMA`/`DMMA`/`QMMA`) selected by element
  type; the shape token is the on-wire `MNK` tile, not the PTX shape.
- **Fragments are per-lane register vectors** with fixed per-shape lengths — the 32
  lanes partition each A/B/C/D tile; ptxas addresses each fragment by a constant offset
  in a contiguous register window.
- **The Volta `884` form expands** into a 2- or 4-step `HMMA.884.STEP*` macro with
  shared `.reuse` A/B operands and combined scoreboards; every later shape is a single
  instruction.
- **Hopper warpgroup MMA** is 128-thread, asynchronous, and sources A/B from shared
  memory through a **64-bit uniform-register matrix descriptor** (base / leading-dim /
  stride / swizzle, opaque to the compiler, with a transpose flag); the async
  accumulator is committed and waited via `WARPGROUP.ARRIVE`/`.WAIT`/`.DEPBAR` and the
  per-instruction `gsb` scoreboard.
- **Sparsity** adds a metadata register + selector and halves the A fragment.
- **Latencies** (result latency charged to a dependent consumer, from the ptxas
  scheduling tables): `HMMA m16n8k8` = 14 (Volta) → 16 (Ampere), `HMMA m16n8k16` =
  22 → 24, `DMMA` = 18, warpgroup `GMMA` = 20; an `HMMA`→`HMMA` accumulator chain
  forwards in **0** cycles. Full table in
  [Latency Model](../scheduling/latency-model.md#functional-unit-base-latencies).
- **The unit was redesigned three times**: Volta `884` steps → Ampere wide SuperHMMA +
  `IMMA` wakeup domain → Hopper async warpgroup + SMEM descriptor → Blackwell tcgen05 +
  tensor memory.
