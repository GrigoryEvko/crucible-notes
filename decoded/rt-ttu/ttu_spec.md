# TTU (RT core) — SASS-level interface

The Tree Traversal Unit is NVIDIA's ray-tracing core, exposed in SASS as the
`TTU*` instruction family. It is a decoupled co-processor with its own internal
address space: functional-unit class `VQ_TTU` (21), its own `ttu_pipe`, present
from sm_75 (Turing) onward. The instruction encodings are decoded from CUDA-13
nvdisasm; the acceleration-structure and ray/hit formats are decoded from the
RT-core runtime (`libnvidia-rtcore.so`).

## Instruction set

Opcode field is `{bit91, bits[11:0]}` (bit91 = 0 for all `TTU*`).

| op | opcode | operands | sync |
|----|--------|----------|------|
| `TTUOPEN`  | `0x3d0` | `@Pg /DUAL &REQ=sb` | waits a req-scoreboard, `MIN_WAIT` 6 |
| `TTUST`    | `0x3d1` | `@Pg TTU[U16], Rb, Rc &RD=sb` | releases a read-scoreboard |
| `TTULD`    | `0x3d2` | `@Pg Pu, Rd2, Rd, TTU[U16] &WR=sb` | arms a write-scoreboard |
| `TTUCLOSE` | `0x3d2` | `@Pg /CLOSE` | arms a write-scoreboard |
| `TTUGO`    | `0x3d3` | `@Pg` | branch-unit kick |
| `TTUCCTL`  | `0x3d5` | `@Pg /IVALLONLY &REQ=sb` | waits a req-scoreboard |
| `TTUMACROFUSE` | `0x9d4` | `@Pg UImm5:Sb &REQ=sb` | coupled-math |

`TTUCLOSE` is `TTULD` with the `/CLOSE` modifier (same opcode `0x3d2`).

All `TTU*` ops are decoupled (`VQ_TTU` pipe), driven through the branch unit's
DEPBAR mechanism. `TTUST` releases a read-scoreboard once it consumes its source
pair; `TTUOPEN`/`TTUCCTL` block on a req-scoreboard mask so they observe the prior
stores; `TTULD` arms a write-scoreboard the dependent consumer waits on.
`TTUMACROFUSE` is the one coupled-math variant, taking a 5-bit scoreboard immediate.

## Internal address space

`TTU:ttuAddr[UImm16]` addresses a 16-bit-indexed state file inside the TTU. The
ray, the BVH root handle, the traversal stack, and the hit record all live here.
`TTUST` writes a register pair `(Rb, Rc)` into `ttuAddr[i]`; `TTULD` reads a
128-bit value `(Rd2:Rd)` plus a hit/miss predicate `Pu` out of `ttuAddr[i]`.

| offset | contents |
|--------|----------|
| `0x000` | ray flags (cull mode, ray op, traversal order, instance mask) |
| `0x040` | ray origin (x, y, z) + `tmin` |
| `0x050` | ray direction (x, y, z) + `tmax` |
| `0x200` | stack-init complet: BVH root node-ref, enter-root flag `0x10000000` |
| `0x300` | hit record |
| `0x340`–`0x370` | triangle vertex fetch slots |
| `0x380`, `0x390` | traversal stack save |

## Programming protocol

1. `TTUST` the ray (origin, direction, `tmin`/`tmax`, flags) and the BVH root
   node-ref into their slots.
2. `TTUOPEN /DUAL` opens the traversal context, blocking on the stores via its
   req-scoreboard.
3. `TTUGO` kicks the autonomous traversal: the TTU walks the BVH with a hardware
   stack and runs ray-box / ray-triangle intersection at the leaves.
4. `TTULD Pu, Rd2, Rd, ttuAddr[0x300]` reads the 128-bit hit record and the
   hit/miss predicate, scoreboard-synchronized. `TTULD.CLOSE` reads the final
   result and tears the context down.
5. `TTUCCTL /IVALLONLY` invalidates the TTU cache when the BVH changes.

## Ray descriptor

16 bytes: origin `(x, y, z)` + `tmin`, then direction `(x, y, z)` + `tmax`, plus
the flag word (cull mode, ray op, traversal order, instance mask).

## Hit record

16 bytes, a tagged union. For a triangle hit: user triangle id, `t`, barycentric
`(u, v)`, and a front/back-facing bit. Example: `{triId=7, t=2.5, u=0.25, v=0.5,
facing=1}` packs to `[0x00000007, 0x40200000, 0xBE800000, 0x3F000000]`.

## Acceleration structure

The hardware reads no AS header. Traversal starts from a complet pointer supplied
through the stack-init slot, with the root at `complet[0]` (`PARENTLEAFIDX = 0xF`).
Complets and triangle blocks are contiguous 128-byte arrays; a child complet at
index `idx` is at `nodesBase + (idx << 7)`.

### Complet (BVH node) — 128 bytes, 128-byte aligned

A 32-byte header followed by 12 child records of 8 bytes. Builders populate up to
11 children on sm_86+ and 10 on Turing; the 12th slot holds long-pointer high
bits.

Header:

| field | offset | bits | meaning |
|-------|--------|------|---------|
| `format` | byte 0 | `[0:3]` | node format |
| `lptr` | byte 0 | `[4]` | long-pointer flag |
| `relptr` | byte 0 | `[5]` | relative-pointer flag |
| `leaftype` | byte 0 | `[6:7]` | leaf type |
| `xscl`/`yscl`/`zscl` | bytes 1/2/3 | 8 each | per-axis quantization exponent |
| `xmin`/`ymin`/`zmin` | bytes 4/8/12 | FP32 | AABB origin |
| `leafptr` | bytes 16–19 | | leaf block pointer (128-byte units) |
| `firsttriidx` | bits 184–187 | | first triangle index |
| `parentleafidx` | bits 188–191 | | parent leaf index; `0xF` = root |
| `parentofs` | bits 192+ | | parent complet pointer |
| `childofs` | bits 224+ | | child complet pointer (nonzero) |

Quantized child bounds dequantize with a power-of-two exponent scale:

```
dx  = bitcast_f32(xscl << 23) / 256        # = 2^(xscl - 127) / 256
lo  = xmin + dx * xlo
hi  = xmin + dx * (xhi + 1)
```

The high endpoint adds one quantum so a child box never under-covers its
contents. Worked values: a `[-1, 1]³` root gives `scl = 0x80`, `dx = 2.0`; a
`[-4/3, 4/3]³` root gives `scl = 0x81`, `dx = 4.0`; a `[0, 4/3]` +X-half child is
`xlo = 0x55`, `xhi = 0xAA`.

### Child record — 8 bytes

`xlo xhi ylo yhi zlo zhi rval data`: six 8-bit quantized bounds, a visibility byte
`rval`, and a `data` byte (leaf bit at bit 6 plus per-type payload). An empty
child is `zlo = 0xFF, zhi = 0x00`.

### Triangle block — 128 bytes

Holds 3–16 triangles. An uncompressed block stores vertices as plain FP32:
`v0` at bytes 0–11, `v1` at 12–23, `v2` at 24–35; the triangle id at byte 120; the
block header at byte 124 (`MODE = 0`, `M = count − 1`). The referencing child names
the range via `firsttriidx` plus the child's triangle-index end.

### Instance node — 64 bytes

A root-complet pointer plus a 3×4 row-major matrix holding the world→object
(inverse) transform.

## Function-evaluation encoding

A piecewise-linear `f(x)` with breakpoints `(x_i, y_i)` evaluates on the RT core
by building a triangle strip with vertices at the breakpoints in the `(x, h)`
plane and firing a vertical ray at `x_q` from `(x_q, +BIG, 0)` toward `(0, -1, 0)`.
The ray-triangle test returns the barycentric coordinates of the hit, and the
interpolated vertex height is `f(x_q)` — the linear interpolation between
breakpoints runs in the intersection unit. A batch of rays evaluates `f` at a
batch of points. `pwl_eval_probe.py` holds the CPU reference for the geometry.

A minimal traversable structure is a 256-byte buffer: one root complet plus one
uncompressed triangle block. The root pointer, ray, and `ttuAddr` slot values are
loaded as above; the hit's barycentrics reconstruct `f(x_q)` from `ttuAddr[0x300]`.

## Verified against driver output

A single-triangle acceleration structure built on the sm_89 GPU through the OptiX
runtime confirms the complet format. For a triangle spanning `[-1,1]³`, the root
complet (128 bytes) has `format=0x60`, `scl=(0x80,0x80,0x80)`, and
`xmin=ymin=zmin=-1.0` (FP32); `2^(0x80-127)=2.0` reproduces the extent. Byte 23 is
`0xF0` — `parentleafidx=0xF`, the root marker. Child 0 is the leaf with full-box
quantized bounds `xlo..zhi = 00..ff`; children 1–11 are empty with the
`zlo=0xFF, zhi=0x00` sentinel. The driver places a bookkeeping header ahead of the
complet array (off the traversal path — the traversable handle resolves to the
complet, not the header). The triangle is stored in the intersector's precomputed
form rather than a plain-FP32 block. `driver_bvh_1tri.bin` is this dumped
structure — a known-good BVH for raw-SASS traversal.
