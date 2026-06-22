# TTU (RT core) — SASS-level interface spec

The Tree Traversal Unit is NVIDIA's ray-tracing core, exposed in SASS as the
`TTU*` instruction family. It is a **decoupled co-processor with its own internal
address space**: functional-unit class `VQ_TTU` (=21), its own `ttu_pipe`, present
from sm_75 (Turing) onward. This spec is decoded from the CUDA-13 nvdisasm SM89
instruction-class tables and the 128-bit SASS encoding; opcodes cross-checked
against the decoded `nvdisasm-sass-isa/sass_isa_SM89.txt` class set.

Everything below the line "SOLID" is recovered from binary analysis. Everything
under "OPAQUE" is the fixed-function hardware contract that the toolchain never
exposes — stated honestly as a gap.

## SOLID — the instruction interface (fully recovered)

Seven opcodes, `{bit91, bits[11:0]}` opcode field (all have bit91=0):

| op | opcode | operands | sync |
|----|--------|----------|------|
| `TTUOPEN`  | `0x3d0` | `@Pg /DUAL &REQ=sb` | waits req-scoreboard, MIN_WAIT 6 |
| `TTUST`    | `0x3d1` | `@Pg TTU[U16], Rb, Rc &RD=sb` | releases read-sb |
| `TTULD`    | `0x3d2` | `@Pg Pu, Rd2, Rd, TTU[U16] &WR=sb` | arms write-sb |
| `TTUCLOSE` | `0x3d2` | `@Pg /CLOSE` (TTULD alternate) | arms write-sb |
| `TTUGO`    | `0x3d3` | `@Pg` (no operands) | branch-unit kick |
| `TTUCCTL`  | `0x3d5` | `@Pg /IVALLONLY &REQ=sb` | waits req-scoreboard |
| `TTUMACROFUSE` | `0x9d4` | `@Pg UImm5:Sb &REQ=sb` | coupled-math |

**Internal address space.** The `TTU:ttuAddr[UImm16]` operand kind addresses a
16-bit-indexed state file *inside* the TTU — the ray, the traversal stack, and the
hit results all live here, not in the register file or global memory. `TTUST`
writes a register pair `(Rb, Rc)` into `ttuAddr[i]`; `TTULD` reads a 128-bit value
`(Rd2:Rd)` plus a predicate `Pu` out of `ttuAddr[i]`.

**Programming protocol** (the move sequence):
1. `TTUST … ttuAddr[i], Rb, Rc` — fill the TTU state: ray origin (x,y,z), ray
   direction (x,y,z), `tmin`/`tmax`, and the BVH root handle, each into its slot.
2. `TTUOPEN /DUAL` — open the traversal context (waits on the stores via its
   req-scoreboard; MIN_WAIT 6).
3. `TTUGO` — kick the autonomous traversal. The TTU walks the BVH with a hardware
   stack and runs ray-box / ray-triangle intersection at the leaves; no register
   operands — it operates entirely on the opened context.
4. `TTULD Pu, Rd2, Rd, ttuAddr[r]` — read a 128-bit hit record (and a hit/miss
   predicate `Pu`), scoreboard-synchronized (the consumer waits the armed write-sb).
   `TTULD.CLOSE` reads the final result and tears the context down.
5. `TTUCCTL /IVALLONLY` — invalidate the TTU's cache when the BVH changes.

**Sync model.** All `TTU*` ops are decoupled (`VQ_TTU` pipe), driven through the
branch unit's DEPBAR mechanism (`INST_TYPE_DECOUPLED_BRU_DEPBAR_*`). `TTUST`
releases a read-scoreboard once it has consumed its source pair; `TTUOPEN`/`TTUCCTL`
block on a req-scoreboard mask (so they observe the prior stores); `TTULD` arms a
write-scoreboard the dependent consumer waits on. `TTUMACROFUSE` is the one
coupled-math variant (a fused step taking a 5-bit scoreboard immediate).

## OPAQUE — the hardware contract (the gap, stated honestly)

The instruction *interface* is complete, but three pieces of the *data* contract
are fixed-function and are not present in any open header, register manual, or
toolchain table (an exhaustive firmware/register scan finds no RT/BVH/ray/TTU
definitions — NVIDIA never shipped RT-core internals in the open):

1. **The `ttuAddr` slot map** — which 16-bit index holds ray.origin.x vs the BVH
   root vs `tmin` vs the hit result. The instruction takes an index; the index→field
   assignment is the hardware's.
2. **The ray / hit-record byte format** — field order, packing, the 128-bit hit
   record's layout (primitive id / barycentrics / t / instance).
3. **The BVH node format** — NVIDIA's compressed treelet ("complet") format with
   quantized child bounds and internal pointers. This is the single hardest
   structure on the GPU; the hardware *validates* it during traversal, so a
   malformed node faults or hangs the TTU rather than degrading gracefully.

## Creative-compute encoding — the PWL function evaluator (design)

To evaluate a piecewise-linear `f(x)` (breakpoints `(x_i, y_i)`) on the RT core:
build a triangle strip whose vertices are the breakpoints in the `(x, h)` plane
(`vertex_i = (x_i, y_i, 0)`); fire a vertical ray from `(x_q, +BIG, 0)` in
direction `(0,-1,0)`. The hardware ray-triangle test returns the barycentric
coordinates of the hit, and the interpolated vertex height *is* `f(x_q)` — the
linear interpolation between breakpoints is done in fixed-function silicon. A
batch of rays = `f` evaluated at a batch of points, with HW interpolation free.
The hit's `t` (or the reconstructed `y`) is read back via `TTULD`. (See
`pwl_eval_probe.py` for the CPU reference that validates the geometry.)

## Feasibility verdict — raw-SASS PWL on sm_89

**The instruction path is buildable; the data path is blocked.** We can emit a
correct `TTUST → TTUOPEN → TTUGO → TTULD` sequence in a cubin (the encoding is
fully recovered above). What we cannot do *blind* is (a) build a BVH the hardware
will traverse, or (b) load the ray into the correct `ttuAddr` slots — both depend
on the OPAQUE contract. Firing `TTUGO` against a guessed BVH would hang the TTU
(it validates node structure), with no information gained, so we deliberately do
not attempt it.

**Minimal experiment to close it** (the next step, not done here):
1. Build a trivial acceleration structure with the public ray-tracing API
   (`optixAccelBuild` over one triangle) and **dump the resulting device buffer** —
   reverse the node layout from a known-good BVH the driver produced.
2. Capture the SASS of a minimal closest-hit trace and read the `ttuAddr` indices
   its `TTUST`/`TTULD` use → recover the slot map and hit-record layout.
3. With (1)+(2), hand-assemble the `TTUST/OPEN/GO/LD` sequence over the
   driver-built BVH and launch via the raw-SASS path on the sm_89 GPU.

That converts the two opaque pieces from "unknown" to "capture-and-reverse a
known-good instance" — the standard way to crack a fixed-function contract.
