# TTU slot map + ray/hit register formats + trace op-sequence (RT-intrinsic lowering)

Recovered from binary analysis of the CUDA-13 RT toolchain: the RT-intrinsic→SASS
compiler lowering path, our ptxas decompile (the TTU opcode emitters are confirmed at
the disassembly level), `nvdisasm` instruction-class decode, and assembled
RT-traversal SASS. This file closes the two pieces that the prior `ttu_spec.md` listed
as OPAQUE: the **`ttuAddr[U16]` slot map** and the **ray/hit byte formats**, and it
pins the exact **trace op-sequence** the compiler emits.

Companion: `ttu_slot_map_lowering.tsv` (machine-readable slot table). The opcode/encoding
layer is in `ttu_spec.md` / `ttu_opcodes.tsv` (not duplicated here).

## How the slot index is carried (encoding, empirically validated)

`TTUST` and `TTULD` both take a `TTU:ttuAddr[UImm16]` operand. The 16-bit index lands in
SASS instruction **bits[55:40]** (the same byte position the load/store offset field
occupies on these classes). It is a **byte offset into a 16-byte-granular state file**,
required 16-byte aligned (low nibble = 0); the compiler validates `offset == offset &
0xFFF0`. Each store writes one 16-byte slot, each load reads one 16-byte slot.

This bit placement was validated empirically. Hand-encoding the seven TTU classes per
the recovered field layout and feeding the raw 128-bit words to `nvdisasm` (CUDA 13.1,
V13.1.115) makes it decode them by opclass — `ttust_`, `ttugo_`, `ttuld__no_close`,
`ttuld__close`, `ttucctl_`, `ttuopen_` — with no decode error once the control word is
valid. Encoding `ttuAddr` values 0x000 / 0x040 / 0x050 / 0x300 places them at bits[55:40]
exactly (byte5/byte6 of the low 64-bit word), e.g. index 0x300 → low word `…0300…73d2`.
Our ptxas decompile independently OR-s the opcodes into the instruction word
(`*word |= 0x3D0` TTUOPEN, `0x3D1` TTUST, `0x3D2` TTULD, `0x3D3` TTUGO, `0x3D5` TTUCCTL,
`0x9D4` TTUMACROFUSE), each visible as an `or qword ptr [rax], <opcode>` in two
per-architecture encoder clusters.

The state file spans `[0x000, 0x600)` (1536 B). The split is hard:

- **Writable input window** `[0x000, 0x300)` — TTUST only (ray + traversal-stack init).
- **Readable output window** `[0x300, 0x3F0)` — TTULD only (hit + fetched geometry + stack).
- `0x3F0` is the *Invalid* sentinel address used by ops that carry no explicit slot
  (OPEN/GO and the no-address forms of CLOSE).

## 1. The `ttuAddr[U16]` slot map

The index→meaning map (`W`=written by TTUST, `R`=read by TTULD; each row = 16 bytes):

| Offset | Dir | Contents |
|--------|-----|----------|
| `0x000` | W | **RayFlags** (primary slot) — type/cullMode/flags/stackLimit/rayOp/traversalOrder/instMask + RTT modes |
| `0x040` | W | **RayOrigin** (primary) — origin.x, origin.y, origin.z, **tmin** |
| `0x050` | W | **RayDirection** (primary) — dir.x, dir.y, dir.z, **tmax** |
| `0x080` | W | RayFlags2 (bottom/dual ray slot — hardware instancing) |
| `0x0C0` | W | RayOrigin2 (bottom slot; tmin field ignored) |
| `0x0D0` | W | RayDirection2 (bottom slot; tmax field ignored) |
| `0x100`/`0x110`/`0x120` | W | StackRestore primary, entry pairs 0-1 / 2-3 / 4-5 |
| `0x180`/`0x190`/`0x1A0` | W | StackRestore secondary, entry pairs 0-1 / 2-3 / 4-5 |
| `0x200` | W | **StackInitComplet — the BVH root pointer** (traversal entry point) |
| `0x210` | W | StackInitTriRange — triangle range to intersect (+ instance ptr) |
| `0x220` | W | StackInitTriRangeComplet — tri range + node ref |
| `0x280` | W | StackInitTriFetch — tri range for vertex fetch |
| `0x300` | R | **Hit** — the result record (also the TTUCLOSE default slot) |
| `0x310` | R | HitTrianglePtr — TTUTriRange of the hit triangle |
| `0x320` | R | HitInstance — user instance id (lo/hi) + 49-bit instance pointer |
| `0x330` | R | TransformedRay0 — object-space origin.xyz + dir.x |
| `0x340`-`0x370` | R | TriFetch 0-3 — fetched triangle verts v0/v1/v2 + triId/alpha/error |
| `0x380`/`0x390`/`0x3A0` | R | return Stack primary, entry pairs 0-1 / 2-3 / 4-5 |
| `0x3B0` | R | TransformedRay1 — object-space dir.y, dir.z |
| `0x3C0`/`0x3D0`/`0x3E0` | R | return Stack secondary, entry pairs 0-1 / 2-3 / 4-5 |
| `0x3F0` | — | Invalid sentinel (no-address commands) |
| `0x600` | — | AddressSpaceSize (state-file size bound) |

This resolves the prior spec's OPAQUE item (1): the slot map is a fixed byte-offset
layout, and the `768 = 0x300` we deduced from the TTUCLOSE encoding default is the
**Hit result** slot. The address space is symmetric: an input field at offset *X* in
`[0,0x300)` has its corresponding read-back output starting at `0x300`.

## 2. Ray descriptor + hit-record byte formats

Every request/result slot is `U32 reg[4]` (128 bits = one register pair `(Rb-pair,
Rc-pair)` for a store, `(Rd2:Rd)` for a load). Bit offsets below are little-endian within
the 4×U32; floats are raw IEEE-754 FP32.

### Ray descriptor (TTUST consumes these)

**RayOrigin (slot `0x040`)** and **RayDirection (slot `0x050`)** are the geometric ray —
note that **tmin rides in the origin slot and tmax rides in the direction slot**:

```
RayOrigin     reg[4]:  ORIGIN_X[31:0]  ORIGIN_Y[63:32]  ORIGIN_Z[95:64]  TMIN[127:96]
RayDirection  reg[4]:  DIR_X[31:0]     DIR_Y[63:32]     DIR_Z[95:64]     TMAX[127:96]
```

**RayFlags (slot `0x000`)** packs traversal control into the first 96 bits:

```
TYPE[2:0]           ray type (0=ray, 3=box, 4..6=beam, 7=HW-instancing dual ray)
CULLMODE[5:4]       0=none 1=cull-back 2=cull-front 3=disable-edge-test
FRONTFACECW[6]      front-face winding
TERMINATEONHIT[7]   any-hit early terminate
STACKLIMIT[10:8]    # stack entries SM expects back
INSTANCENODEPOINTER[12], STORE_T[13], REPORTONENTER[14], NOPOPONRETURN[15]
RAYOP[19:16]        ray-op selector (alpha/anyhit predicate, see TTURayOp)
TRAVERSALORDER[23:21]
INSTMASK[31:24]     8-bit ray instance mask
MODE_*[55:32]       eight 2-bit RTT child/hit modes (Opaque/Alpha/Complet/TriRange/
                    ItemRange/InstanceNode, each x {Pass,Fail})
RAYOPPARAM_A[79:64], RAYOPPARAM_B[95:80]
```

**StackInitComplet (slot `0x200`)** carries the BVH root: a 64-bit node-ref (a 49-bit
complet pointer plus an enter-root flag) in `reg[0:1]`, and an optional 49-bit instance
pointer in `reg[2:3]`. This is the single store that hands the traversal its starting
node.

### Hit / result record (TTULD produces these)

**Hit (slot `0x300`)** is a tagged union. The 4-bit **type lives at bit[127:124]** (top
nibble of `reg[3]`). Decode: if `reg[3]>>31 == 0` the hit is a **Triangle**; otherwise
`reg[3]>>28` selects NodeRef=0x8, TriRange=0x9, ItemRange=0xA, InstanceNode=0xB,
Error=0xE, **None/miss=0xF**.

Triangle hit (the common closest-hit / any-hit payload):

```
USERTRIANGLEID = reg[0][31:0]                     primitive id
T   = reg[1] & 0x7FFFFFFF   (FP32 hit distance)   ALPHA      = reg[1]>>31
U   = reg[2] & 0x7FFFFFFF   (FP32 barycentric u)  BACKFACING = reg[2]>>31
V   = reg[3] & 0x7FFFFFFF   (FP32 barycentric v)
```

(When `cullMode == DisableEdgeTest`, T/U/V are full signed FP32 and the ALPHA/BACKFACING
sign bits are part of the value — used for barycentric differentials.) Other hit subtypes
put a 32-bit FP32 `T` at bit[95:64] alongside a node-ref / tri-range / 49-bit item-range
start / 49-bit instance pointer.

The **predicate `Pu`** that `TTULD` returns is the hit/miss (and per-lane validity)
signal — a `None` (0xF) type means the ray missed, so the SM branches on `Pu` without
even decoding the record.

**HitInstance (slot `0x320`)** gives the instance result: `USERINSTANCEIDLO[31:0]`,
`USERINSTANCEIDHI[63:32]`, `INSTANCEPTR[112:64]` (49-bit). **return Stack (slots
`0x380…`, `0x3C0…`)** is the traversal continuation the SM saves/restores; `reg[1]==0`
on the first stack slot means traversal is fully terminated (no more hits possible).

## 3. The exact emission sequence (operand wiring + scoreboard handshake)

The compiler emits a fixed block per trace. Assembled SASS for a single-ray closest-hit
(slot indices match the map above; `?WAITn` = MIN_WAIT, `&wr` = armed write-scoreboard):

```
TTUOPEN                       ?WAIT5          ; allocate a single-slot ticket
TTUMACROFUSE 0xa              ?WAIT4          ; fused wait-for-ticket (5-bit Sb imm)
TTUST ttu[0x0],   RZ,  R6     ?WAIT1          ; RayFlags
TTUST ttu[0x40],  R52, R30    ?WAIT1          ; RayOrigin    (origin.xyz + tmin)
TTUST ttu[0x50],  R12, R66    ?WAIT1          ; RayDirection (dir.xyz + tmax)
@P1  TTUST ttu[0x100], R8,  R38 ?WAIT1        ; (warm-start) StackRestore primary 0/1
@P1  TTUST ttu[0x110], R44, R42 ?WAIT1        ; StackRestore primary 2/3
@!P1 TTUST ttu[0x200], RZ,  R38 ?WAIT1        ; StackInitComplet = BVH root pointer
TTUGO                         ?WAIT1          ; kick autonomous traversal (no operands)
TTULD R8,  R6,  ttu[0x300]    &wr=0x0 ?WAIT1  ; read Hit result (Rd2:Rd = R8:R6)
TTULD R40, R38, ttu[0x380]    &wr=0x0 ?WAIT1  ; read return-Stack primary 0/1
TTULD.CLOSE R44, R42, ttu[0x390] &wr=0x0 ?WAIT1  ; final stack read + tear down ticket
```

Dual-ray (hardware instancing) is the same shape with the bottom-slot ray
(`TTUOPEN.DUAL`, `TTUMACROFUSE 0xe`, extra `TTUST ttu[0x80]` RayFlags2, secondary stack
restores at `0x180/0x190`, and result reads from `0x300/0x320/0x380/0x3C0` with
`TTULD.CLOSE ttu[0x3d0]`).

Key facts the sequence establishes:

- **OPEN precedes the stores.** TTUOPEN allocates the ticket first (it blocks
  `MIN_WAIT 6`/`?WAIT5` on its req-scoreboard); the stores then fill the opened context.
  TTUMACROFUSE immediately after OPEN is the fused "wait until ticket allocated" step
  (its 5-bit immediate is the pending-instruction count: `0xa` single, `0xe` dual).
- **One TTUST per 16-byte slot**, operands `(Rb-pair, Rc-pair)`; `RZ` is used for the
  upper half of slots whose high 64 bits are zero (e.g. RayFlags' instance ptr).
- **TTUGO takes no operands** — it operates purely on the opened context and kicks the
  autonomous BVH walk.
- **TTULD arms a write-scoreboard** (`&wr`) that the SM consumer waits on; the load
  returns `(Rd2:Rd)` + predicate `Pu`. `TTULD.CLOSE` is the final read that also tears
  the ticket down; its encoding hard-defaults `ttuAddr` to `0x300` (the Hit slot) when no
  explicit address is given — which is exactly the `768` default seen in the close class.
- Reads of distinct result slots are independent TTULDs in the same post-GO block; the
  block is bounded by OPEN…(GO)…(LD.CLOSE) and the toolchain enforces that structure.

## 4. How the RT intrinsics map through to this sequence

The high-level trace builtin lowers in four stages, each confirmed in the toolchain:

1. **NVVM RT intrinsics** `ttuopen / ttust / ttugo / ttuld` — the IR-level primitives.
2. **Direct-to-IR lowering** turns each into `IOP_TTU{OPEN,ST,GO,LD}`. The `ttuAddr`
   index is taken from the intrinsic's constant-int argument and emitted as the immediate
   offset (`OffsetScalarArg`); TTUOPEN's argument selects SINGLE vs DUAL slots; TTULD's
   argument selects CLOSE. Loads/stores are fixed `v2i64` (the 128-bit register pair).
3. **PTX instruction layer** defines `_ttuopen / _ttugo / _ttust (B64, vectorizable) /
   _ttuld (B64, result, TTU)` and a grammar rule that validates the OPEN→…→`_ttuld.close`
   block.
4. **SASS encoding** assigns the `0x3d0…0x3d3 / 0x3d5 / 0x9d4` opcodes (the values our
   ptxas decompile OR-s into the word) and lays the 16-bit `ttuAddr` into bits[55:40];
   the TTUCLOSE class hard-defaults that field to `768`.

The store helpers wire the ray fields to slots one-for-one: a ray builtin maps
`origin/tmin → TTUST ttu[0x40]`, `direction/tmax → TTUST ttu[0x50]`, flags →
`ttu[0x00]`, BVH root → `ttu[0x200]`; the closest-hit read maps to
`TTULD … ttu[0x300]` (+ `ttu[0x320]` for instance id), and the per-lane hit/miss is the
returned predicate `Pu`. Type, cull mode, instance mask, any-hit/terminate, and the
RTT child/hit modes are all packed by the lowering into the RayFlags slot.

## Feasibility verdict update

With this slot map and ray/hit format recovered, two of the three OPAQUE items from
`ttu_spec.md` are now closed:

- **(1) slot map — CLOSED.** Fixed byte-offset layout above; inputs `[0,0x300)`, outputs
  `[0x300,0x3F0)`.
- **(2) ray / hit byte format — CLOSED.** Full field layouts for RayFlags / RayOrigin
  (+tmin) / RayDirection (+tmax) / StackInitComplet (root) and the Hit-result union
  (t, u, v, primId, instance, type/miss).
- **(3) BVH "complet" node format — still the hard part.** The traversal hardware fetches
  and *validates* the compressed node format from BVH memory; a malformed node faults the
  TTU rather than degrading.

**Hand-assembling a raw-SASS trace over a driver-built BVH is now feasible end-to-end**,
combined with the sibling agents' BVH-format work: emit the exact
`TTUOPEN → TTUMACROFUSE → TTUST ttu[0x00]/[0x40]/[0x50] → TTUST ttu[0x200](root) →
TTUGO → TTULD ttu[0x300] (.CLOSE)` block (the encoding is fully recovered and
nvdisasm-validated), pack the ray into the formats above, and point `ttu[0x200]` at a
root complet from a driver-produced acceleration structure. The only remaining external
dependency is a known-good BVH whose root-complet layout matches what the traversal
hardware expects — which is the BVH-format track, not the instruction/slot track this
file covers. The data path is no longer blocked on the slot map or the ray/hit format.
