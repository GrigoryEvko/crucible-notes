# Tensor-Core Pipeline — MMA, Fragments & wgmma

> *Recovered from the decoded per-architecture SASS instruction tables in
> nvdisasm V13.1.115 (CUDA 13.1), cross-validated against the ptxas v13.0.88
> scheduling tables, and **round-trip-validated against real ptxas output** — every
> mnemonic and shape name below was reproduced by assembling PTX `mma`/`wmma`/`wgmma`
> with ptxas and disassembling with nvdisasm. Facts and models only; the verbatim
> NVIDIA tables are not redistributed.*

Every NVIDIA GPU since Volta carries a dedicated **matrix unit** alongside the FMA,
ALU, FP16 and FP64 datapaths. ptxas emits its work as a small family of SASS opcodes
— `HMMA`, `IMMA`, `BMMA`, `DMMA`, `QMMA`, `OMMA` (warp-level), and
`HGMMA`/`IGMMA`/`QGMMA`/`BGMMA` (Hopper warpgroup-level) — plus the matrix-move
helpers `LDSM`, `STSM` and `MOVM`.
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
holds a slice of the A, B, C and D tiles in its own registers. ptxas selects a
warp-level opcode by the operand element type, and the opcode a given element type
reaches depends on the target — several types have no native class on some
architectures and are lowered onto a wider one:

| Element type | SASS opcode | Accumulate | Native on |
|---|---|---|---|
| F16, BF16, TF32 | `HMMA` | F16 or F32 | all (sm_75+) |
| INT8 | `IMMA` | INT32 | all (sm_75+) |
| INT4 | `IMMA` (`S4`/`U4`) | INT32 | sm_75–sm_89 |
| 1-bit (binary) | `BMMA` | INT32 (popcount) | sm_75–sm_90 |
| FP64 | `DMMA` | FP64 | sm_80+ |
| FP8 (E4M3 / E5M2) | `QMMA` | F32 | sm_89, sm_120/121 |
| FP4/FP6 block-scaled | `OMMA`/`QMMA` (`.SF`) | F32 | sm_120/121 |

Where a type is not native, ptxas emulates it rather than rejecting the PTX:

| PTX | Target | Lowering |
|---|---|---|
| `mma…s4` | sm_90+ | `LOP3`/`SHF` sign-extend to INT8, then `IMMA…S8.S8` |
| `mma…b1.xor.popc` | sm_90 | `LOP3` XOR, then `BMMA.88128.AND.POPC` (only `AND` is native) |
| `mma…b1` | sm_100+ | `MOVM.U4TO8` + `LOP3`/`SHF` unpack, then `IMMA.16832.U8.U8` |
| `mma…e4m3`/`e5m2` | sm_90, sm_100/103/110 | `F2FP.F16.E4M3.UNPACK_B` upconvert, then `HMMA.16816.F32` |

So binary MMA has no native class anywhere on Blackwell, and per-warp FP8 is native
only on Ada and consumer Blackwell — Hopper and datacenter Blackwell reach FP8
through `wgmma` and `tcgen05` respectively, and emulate the `mma.sync` form.

### 2.1 Shape naming

SASS names a shape by a compact `MNK` token that is **not** the PTX shape verbatim —
it is the on-wire tile the hardware executes, with the K dimension expressed per
32-bit operand lane. The validated mapping (ptxas → nvdisasm) is:

| PTX shape (type) | SASS mnemonic | Target scope |
|---|---|---|
| `mma.m16n8k16.f16` | `HMMA.16816.F32` | sm_80+ |
| `mma.m16n8k8.f16` | `HMMA.1688.F32` | all |
| `mma.m16n8k16.bf16` | `HMMA.16816.F32.BF16` | sm_80+ |
| `mma.m16n8k8.tf32` | `HMMA.1688.F32.TF32` | sm_80+ |
| `mma.m8n8k4.f16` (Volta form) | `HMMA.884.F32.F32.STEP{0..3}` | sm_75 |
| `mma.m8n8k16.s8` | `IMMA.8816.S8.S8` | sm_75–sm_90 |
| `mma.m8n8k16.s8` | `IMMA.16816.S8.S8` | sm_100+ (8-row shapes retired) |
| `mma.m8n8k32.s4` | `IMMA.8832.S4.S4` | sm_75–sm_89 |
| `mma.m16n8k32.s8` | `IMMA.16832.S8.S8` | sm_80+ |
| `mma.m8n8k128.b1.xor.popc` | `BMMA.88128.XOR.POPC` | sm_75–sm_89 |
| `mma.m8n8k4.f64` | `DMMA.884` | sm_80, sm_89 |
| `mma.m8n8k4.f64` | `DMMA.8x8x4` | sm_90+ |
| `mma.m16n8k32.e4m3` | `QMMA.16832.F32.E4M3.E4M3` | sm_89, sm_120/121 |
| `mma.kind::mxf8f6f4.block_scale.m16n8k32.e4m3` | `QMMA.SF.16832.F32.E4M3.E4M3.E8` | sm_120/121 |
| `mma.kind::mxf4.block_scale.m16n8k64.e2m1` | `OMMA.SF.16864.F32.E2M1.E2M1.E8` | sm_120/121 |

Rows without a target scope of "all" fall back to the emulation sequences in §2 on
targets outside their scope. The `MNK` token itself is also not stable across
generations: Hopper renames the FP64 shape from the concatenated `884` to the
`x`-separated `8x8x4`, and the warpgroup family uses the `x`-separated form
throughout (§6).

The full SASS shape vocabularies the disassembler will print:

- **HMMA** — `884`, `1688`, `16816`, `1684`, `16832`. The dst suffix is `.F32` or
  `.F16`; alt-format suffixes `.E8M7` (TF32-style) and `.E8M10` distinguish the
  reduced-mantissa inputs; `.BF16` / `.TF32` appear for the corresponding inputs.
- **IMMA** — `8816`, `8832`, `8864`, `16816`, `16832`, `16864`, `168128`. The two
  type suffixes are the A and B operand formats, each one of `U8`, `S8`, `U4`, `S4`
  (`U16`/`S16` are also encodable). Operands carry explicit `.ROW`/`.COL` modifiers.
  The three 8-row shapes (`8816`, `8832`, `8864`) are **absent from the Blackwell
  tables** — sm_100 onward maps those PTX shapes onto the 16-row unit.
- **BMMA** — `88128`, `168128`, `168256`. The logical-op suffix is the source op
  followed by the accumulate op, which is always `POPC` (population count) — binary
  MMA computes `popc(A op B)`. `XOR` is native on sm_75–sm_89; `AND` requires sm_80
  and is the only native source op on sm_90. **No `BMMA` class exists on any
  Blackwell target.**
- **DMMA** — one shape, printed `884` on sm_80/sm_89 and `8x8x4` on sm_90 onward,
  with a rounding-mode field (`ROUND`/`FLOOR`/`CEILING`/`TRUNC`).
- **QMMA** — the FP8 family, native on Ada `sm_89` and consumer Blackwell
  `sm_120`/`sm_121`; the type suffixes name the A and B FP8 formats (`E4M3`,
  `E5M2`). On `sm_120`/`sm_121` the family extends to FP6 (`E3M2`, `E2M3`) and FP4
  (`E2M1`) and gains the block-scaled `.SF` forms.
- **OMMA** — consumer-Blackwell-only, block-scaled FP4 at `16864` (and `168128`
  sparse). Every `OMMA` class in the tables carries a scale factor; there is no
  unscaled form.

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

Sparse variants exist for `HMMA` and `IMMA` on every target, for the Hopper
`HGMMA`/`IGMMA`/`QGMMA` warpgroup families, and on consumer Blackwell for `QMMA` and
`OMMA` (including their block-scaled `.SF.SP` forms). The standalone
metadata-preparation opcodes are `SPMETADATA` / `GENMETADATA`.

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

The warpgroup-MMA shapes are `m64nNkK` — the M dimension is **fixed at 64** (the
warpgroup's row count), N ranges over `{8,16,24,32,…,256}` in steps of 8 (32 values),
and K is one of `{8, 16, 32, 64, 256}` by element width (K=64 for sub-byte integer,
K=256 for the binary `BGMMA` forms). The warpgroup family prints its shape token in
**`x`-separated** form, not the concatenated form the warp-level opcodes use:
`HGMMA.64x16x16.F32` (m64n16k16), `HGMMA.64x8x16.F32`, `HGMMA.64x32x16.F32`. The
tables carry 146 distinct `64xNxK` tokens.

### 6.1 The shared-memory matrix descriptor

The decisive new mechanism is that A and B may live **in shared memory** instead of in
registers. When they do, the operand is **not** a register fragment vector — it
collapses to a **single 64-bit descriptor** held in a **uniform register** (`URa` or
`URb`). The SASS form is:

```
HGMMA.size.dstfmt{.srcfmt}  Rd, {-}Ra, URb{.negB}{.tnspB}, Rc, {!}UPp{, gsb}
```

`URb` carries the 64-bit shared-memory matrix descriptor (the disassembler prints
it as a bare uniform register; the Blackwell tcgen05 form below prints it
explicitly as `gdesc[URb]`). Two per-operand modifiers ride on it:

- **`.tnspB`** (and `.tnspA`) — the transpose attribute of the shared-memory matrix
  (column- vs row-major reading of the SMEM tile).
- **`.negB`** — input negation.

ptxas treats the descriptor as an opaque value — it is materialised into the
uniform register by preceding integer code (the user builds it; the consuming
`HGMMA` reads `gdesc[URb]` whole) — but the descriptor itself is a fixed
hardware contract whose 64-bit bit-layout is well-defined:

| Bit range | Field | Encoding |
|---|---|---|
| `[13:0]`  | SMEM matrix start address | byte address `>> 4` (16-byte units; low 4 bits dropped) |
| `[29:16]` | leading-dimension byte offset (LBO) | byte offset `>> 4` |
| `[45:32]` | stride-dimension byte offset (SBO) | byte offset `>> 4` |
| `[51:49]` | matrix base offset | raw 3-bit value |
| `[63:62]` | swizzle / layout mode | enum below |

Bits `[15:14]`, `[31:30]`, `[47:46]`, `[48]`, `[55:52]` and `[61:56]` are unused.
The three address/offset fields are all stored **right-shifted by 4** — they index
in 16-byte units, which is why a `wgmma` SMEM tile must be 16-byte aligned. The
swizzle field selects the bank-conflict-avoiding SMEM layout the hardware walks as
it reads the tile, and its enum is **descending** in byte size:

| `[63:62]` | Swizzle mode |
|---|---|
| `0` | none (interleaved / linear) |
| `1` | 128-byte swizzle |
| `2` | 64-byte swizzle |
| `3` | 32-byte swizzle |

(The descending 1=128B / 2=64B / 3=32B ordering is the genuine encoding — not the
naive ascending guess.) The compiler's only *semantic* interaction with the
descriptor is the `.tnspA`/`.tnspB` transpose flag it folds into the instruction;
the base/LBO/SBO/base-offset/swizzle fields are produced by the descriptor-build
code and interpreted by the tensor core.

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

## 8. Datacenter Blackwell (`sm_100`/`103`/`110`): tcgen05 — TMEM, the instruction descriptor, and 2-CTA issue

Hopper's warpgroup `*GMMA` family is **removed** across all of Blackwell, and on the
datacenter targets it is replaced by **tcgen05**. Consumer Blackwell has neither
family (§8.3). Assembling `tcgen05.*` PTX for `sm_100a` and disassembling pins the
opcodes and — decisively — the operand-class structure the disassembler prints:

| PTX | SASS (`sm_100a`) | op byte |
|---|---|---|
| `tcgen05.mma.kind::f16` / `.tf32` | `UTCHMMA gdesc[URa], gdesc[URb], tmem[URd], tmem[URc], idesc[URi], URp, UPp` | `0xea` |
| `tcgen05.mma.kind::i8` | `UTCIMMA gdesc[…], gdesc[…], tmem[…], tmem[…], idesc[…], …` | `0xea` |
| `tcgen05.mma.cta_group::2` | `UTCHMMA.2CTA …` | `0xea` |
| `tcgen05.ld.16x64b` | `LDTM.16dp64bit Rd, tmem[UR]` | `0xee` |
| `tcgen05.ld.16x128b` | `LDTM.16dp128bit Rd, tmem[UR]` | `0xee` |
| `tcgen05.st` | `STTM tmem[UR], Rs` | `0xed` |
| `tcgen05.alloc` | `UTCATOMSWS.FIND_AND_SET.ALIGN UP, UR, UR` | `0xe3` |
| `tcgen05.dealloc` | `UTCATOMSWS.AND UR, UR` | `0xe3` |
| `tcgen05.commit.mbarrier` | `UTCBAR [UR], UR` | `0xe9` |

The `UTCHMMA` operand list is the whole model in one line: the A and B operands are
**`gdesc[UR]`** (the same 64-bit SMEM matrix descriptor as Hopper §6.1, held in a
uniform register), the accumulator and the second source are **`tmem[UR]`** (a
32-bit tensor-memory address in a uniform register), and a fifth uniform register
holds **`idesc[UR]`** — the *instruction descriptor* that carries the shape,
formats, transpose and negate that Hopper spread across the mnemonic. The `.2CTA`
suffix is the two-CTA paired-issue variant (two CTAs cooperatively feeding one
tensor core). `LDTM.16dp64bit` / `.16dp128bit` name the read geometry as
**16 data-paths × {64,128}-bit columns**.

### 8.1 Tensor memory (TMEM) and its 32-bit address

TMEM is a dedicated on-chip matrix store, addressed by the 32-bit value in the
`tmem[UR]` operand. The address splits into a **lane (row)** and a **column** half:

| Bit range | Field | Meaningful width |
|---|---|---|
| `[31:16]` | lane index (row / data-partition) | low 7 bits (128 lanes) |
| `[15:0]`  | column index | low 9 bits (512 columns) |

The geometry is fixed at **128 lanes × 512 columns × 4 bytes = 256 KB per SM**.
`tcgen05.alloc` (→ `UTCATOMSWS.FIND_AND_SET`) reserves a **column range** — all 128
lanes of each allocated column, never a lane sub-range — in units of **32 columns**,
with `nCols ∈ {32, 64, 128, 256, 512}` (a power of two). `tcgen05.dealloc` clears
the same allocation bitset via `UTCATOMSWS.AND`. The allocator returning a column
base into shared memory is why `tcgen05.alloc` takes a `.shared` destination.

### 8.2 The 32-bit instruction descriptor (`idesc`)

The dense `idesc` (`.kind::tf32`/`.f16`/`.f8f6f4`/`.i8`) packs the MMA into a single
32-bit word:

| Bit range | Field | Meaning |
|---|---|---|
| `[1:0]`   | sparse selector | 2:4 metadata id (sparse only) |
| `[2]`     | sparse | 0 = dense, 1 = sparse |
| `[3]`     | saturate | integer-result clamp |
| `[5:4]`   | D (accumulator) format | F16=0, F32=1, S32=2 |
| `[9:7]`   | A format | per-kind dtype code |
| `[12:10]` | B format | per-kind dtype code |
| `[13]`    | negate A | input negation |
| `[14]`    | negate B | input negation |
| `[15]`    | A major | 0 = K-major, 1 = MN-major (transpose A) |
| `[16]`    | B major | transpose B |
| `[22:17]` | N dimension | encodes `N >> 3` (N ∈ 8…256) |
| `[28:24]` | M dimension | encodes `M >> 4` (M = 64/128/256) |
| `[31:30]` | max shift | B-reuse window: 0/8/16/32 |

Per-kind A/B dtype codes: TF32 → `2`; F16 → {F16=0, BF16=1}; F8F6F4 →
{E4M3=0, E5M2=1, E2M3=3, E3M2=4, E2M1=5}; I8 → {U8=0, S8=1}. The
**block-scaled** variant (`.kind::mxf8f6f4`/`.mxf4`) reuses `[2:0]` and the
transpose/negate/N fields but repurposes `[5:4]` as the B scale-factor id, `[23]`
as the scale-factor type (UE4M3=0 / UE8M0=1), and `[30:29]` as the A scale-factor
id; in the block-scaled layout the M dimension moves to bits `[28:27]` encoded as
`M >> 7`. `D = A·B` vs `D = A·B + C` is *not* an idesc bit — it is a separate PTX
predicate operand (the `UPp` in the disassembled `UTCHMMA`).

### 8.3 The datacenter/consumer split

tcgen05 + TMEM + 2-CTA paired issue exist **only on the datacenter Blackwell
targets** (`sm_100`/`103`/`110`). Consumer Blackwell (`sm_120a`/`sm_121a`) **rejects
every `tcgen05.*` instruction** — ptxas reports `Instruction 'tcgen05.mma' not
supported on .target 'sm_120a'`, and the `kind::` qualifier on a warp-level `mma`
draws `Feature '.kind::f16' not supported on .target 'sm_120a'`. The decoded tables
agree: `utc*`, `ldtm` and `sttm` classes are present on sm_100/103/110 and number
**zero** on sm_120/121.

Consumer Blackwell instead reaches FP8/FP6/FP4 through the **per-warp** `QMMA` and
`OMMA` classes, which is where its block-scaled MX support lives:

| PTX | SASS (`sm_120a`/`sm_121a`) |
|---|---|
| `mma…kind::mxf8f6f4.block_scale.scale_vec::1X.m16n8k32…{e4m3,e5m2,e3m2,e2m3,e2m1}…ue8m0` | `QMMA.SF.16832.F32.<fmt>.<fmt>.E8` |
| `mma…kind::mxf4.block_scale.scale_vec::2X.m16n8k64…e2m1…ue8m0` | `OMMA.SF.16864.F32.E2M1.E2M1.E8` |
| `mma…kind::mxf4nvf4.block_scale.scale_vec::4X.m16n8k64…e2m1…ue4m3` | `OMMA.SF.16864.F32.E2M1.E2M1.UE4M3.4X` |
| `mma.sp::ordered_metadata…kind::mxf8f6f4.block_scale…m16n8k64` | `QMMA.SF.SP.16864.F32.E4M3.E4M3.E8` |
| `mma.sp::ordered_metadata…kind::mxf4.block_scale…m16n8k128` | `OMMA.SF.SP.168128.F32.E2M1.E2M1.E8` |

Every one of these is **rejected on sm_89, sm_90a, sm_100a, sm_103a and sm_110a**
with `Instruction 'mma with block scale' not supported` — the block-scaled
`mma.sync` form is exclusive to consumer Blackwell, exactly complementing the
datacenter line's block-scaled `tcgen05` path. Consumer Blackwell also **drops
`BMMA` entirely** (§2). This is the third matrix-unit redesign in four generations,
and the first that splits the ISA between datacenter and consumer parts.

The opcode bytes, operand classes, and the consumer rejection were taken directly
from CUDA 13.1 ptxas + nvdisasm/cuobjdump on `sm_100a`/`sm_120a` probes; the idesc
and TMEM bit-ranges are the public hardware descriptor contracts these instructions
consume. The full op taxonomy and the datacenter/consumer split are catalogued in
[Architecture Evolution](architecture-evolution.md#a-tensor-cores-bifurcate-at-blackwell).

---

## 9. Summary — the matrix-unit model at a glance

- **Six warp-level opcodes** (`HMMA`/`IMMA`/`BMMA`/`DMMA`/`QMMA`/`OMMA`) selected by
  element type; the shape token is the on-wire `MNK` tile, not the PTX shape. Which
  opcode a type reaches is target-dependent — INT4, binary and (on Hopper/datacenter
  Blackwell) FP8 are emulated onto wider units rather than rejected.
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
  tensor memory — and at Blackwell the ISA **splits**: tcgen05/TMEM on
  sm_100/103/110, block-scaled per-warp `QMMA`/`OMMA` on sm_120/121, with neither
  target accepting the other's family.
