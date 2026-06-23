# TTU BVH node ("complet") byte format — the acceleration-structure contract

This recovers the fixed-function memory contract the RT-core Tree Traversal Unit
(TTU) validates and walks: the compressed-treelet ("complet") node, the
acceleration-structure (AS) memory layout, the triangle/primitive leaf, the
instance node, the ray descriptor, and the hit/result records. It is recovered
from analysis of the RT-core driver/runtime binaries (the BVH builder and the
reference-TTU validator path) together with the SASS `TTU*` instruction encoding
decoded by nvdisasm. It closes the three "OPAQUE" gaps flagged in `ttu_spec.md`
(the `ttuAddr` slot map, the ray/hit-record byte format, the BVH node format).

Everything here is a hardware-contract FACT: byte offsets, bit fields, and the
quantization arithmetic the silicon performs. All offsets are little-endian; a
field "at bit B, width W" lives in the `U32` word `B>>5`, bits `[ (B&31) .. (B&31)+W-1 ]`
(see the bitfield rule at the end). Confidence is stated per structure.

The whole format is built from four power-of-two-sized records, all naturally
aligned:

| record | size | alignment | role |
|--------|------|-----------|------|
| `TTUComplet` | 128 B | 128 B | interior/leaf BVH node (the "complet") |
| `TTUTriangleBlock` | 128 B | 128 B | 1–16 triangles, the triangle leaf payload |
| `TTUInstanceNode` | 64 B | 64 B | TLAS leaf: transform + pointer to a bottom AS |
| `TTUStackEntry` | 8 B | — | one hardware-traversal-stack slot |

Two address masks bound every pointer the format encodes: complet/leaf/instance
pointers are **49-bit** (`(1<<49)-1`); the bit-addressed item-range pointer is
**52-bit** (`(1<<52)-1`).

---

## 1. The complet (`TTUComplet`, 128 bytes) — the BVH node

A complet is a 32-byte header followed by twelve 8-byte child records:

```
TTUComplet, 128 B, 128-B aligned
+--------------------------------------------------------------+
| byte 0   : TTUCompletHeader            (32 B, 8 x U32)        |
| byte 32  : TTUCompletChild  child[0]   ( 8 B)                 |
| byte 40  : TTUCompletChild  child[1]   ( 8 B)                 |
|   ...    : ...                                                |
| byte 120 : TTUCompletChild  child[11]  ( 8 B)                 |
+--------------------------------------------------------------+
```

`CompletFormatMaxChildren = 12`. Child slot **11** doubles as the high bits of a
"long pointer" when `header.LPtr == 1` (see §1.3), leaving 11 usable AABB children
in that mode.

### 1.1 The header (`TTUCompletHeader`, 32 bytes)

Eight `U32` words; fields by *bit* offset from the start of the complet:

```
word0 (byte 0):   FORMAT[3:0]  LPTR[4]  RELPTR[5]  LEAFTYPE[7:6]  XSCL[15:8]  YSCL[23:16]  ZSCL[31:24]
word1 (byte 4):   XMIN  (FP32, bits 32..63)
word2 (byte 8):   YMIN  (FP32, bits 64..95)
word3 (byte 12):  ZMIN  (FP32, bits 96..127)
word4 (byte 16):  ITEMBASELO / LEAFPTRLO        (U32, bits 128..159)
word5 (byte 20):  ITEMBASEHI[15b] / LEAFPTRHI[17b]  + FORCERAYOPALWAYS[180] MULTIBOXCOUNT[183:181]
                  FIRSTTRIIDX[187:184] PARENTLEAFIDX[191:188]
word6 (byte 24):  PARENTOFS / PARENTPTRLO       (S32/U32, bits 192..223)
word7 (byte 28):  CHILDOFS  / CHILDPTRLO        (S32/U32, bits 224..255)
```

Field semantics:

- **FORMAT** (bit 0, 4b): node format. `0` = plain AABB node (the only value the
  base hardware accepts; `1`=motion, `2`=multi-box, `3`=motion+multi-box are
  feature-gated). A reserved FORMAT raises hardware error `0x30`.
- **LPTR** (bit 4): 0 = child/parent/leaf pointers are *relative* 32-bit offsets
  stored in the header; 1 = "long pointer" — the high 17 bits of the absolute
  child and parent addresses live in child slot 11 (§1.3).
- **RELPTR** (bit 5): for leaf/item pointers, 1 = the leaf pointer is relative to
  this complet's own address; 0 = absolute.
- **LEAFTYPE** (bits 6–7): what kind of leaf the *leaf children* of this complet
  point at — `0`=ItemRange (custom AABB prims), `1`=TriRange (triangles),
  `2`=InstanceNode (TLAS), `3`=reserved (DisplacedMicromesh on Ada). Reserved
  raises error `0x31`.
- **XSCL/YSCL/ZSCL** (bits 8/16/24, 8b each): per-axis quantization exponents
  (see the AABB decode below).
- **XMIN/YMIN/ZMIN** (bits 32/64/96, FP32 each): the complet's quantization origin
  — the low corner of the box the 8-bit child bounds are quantized against.
- **ITEMBASELO/LEAFPTRLO** (bit 128, 32b): low 32 bits of the leaf-array base
  pointer (TriRange/InstanceNode) or item-base pointer (ItemRange).
- **ITEMBASEHI** (bit 160, 15b) / **LEAFPTRHI** (bit 160, 17b): high bits of that
  pointer. (Width differs by leaf type: 17b leaf-ptr-hi for tri/instance; 15b
  item-base-hi.)
- **FORCERAYOPALWAYS** (bit 180): force the ray-op predicate to "always" for
  children of this complet.
- **MULTIBOXCOUNT** (bits 181–183, 3b): multi-box child grouping (feature).
- **FIRSTTRIIDX** (bits 184–187, 4b): starting triangle index for the *first*
  TriRange leaf of this complet — the local index into the target triangle block.
- **PARENTLEAFIDX** (bits 188–191, 4b): which child slot of the parent points back
  here; **`0xF` = root** (`ParentLeafIndexRoot`). The root complet is the only one
  allowed to have no parent.
- **PARENTOFS** (bit 192, S32): signed parent-complet offset (`<<7` = ×128 B,
  relative to this complet) when `LPTR==0`.
- **CHILDOFS** (bit 224, S32): signed child-complet base offset (`<<7` = ×128 B)
  when `LPTR==0`. **Must be non-zero** (a zero child offset means a complet points
  its own children at itself — hardware error `0x34`, also the historical
  "childOfs==0" build assert).

### 1.2 The child record (`TTUCompletChild`, 8 bytes)

Each child is two `U32` = eight bytes, byte-per-field:

```
byte 0: XLO   byte 1: XHI   byte 2: YLO   byte 3: YHI
byte 4: ZLO   byte 5: ZHI   byte 6: RVAL  byte 7: DATA
```

- **XLO/XHI/YLO/YHI/ZLO/ZHI** (8b each): the child AABB, quantized to 8 bits per
  axis-endpoint in the complet's local coordinate frame.
- **RVAL** (byte 6): the 8-bit visibility/ray-op value tested against the ray's
  instance mask and ray-op (child masks must be a subset of the parent's).
- **DATA** (byte 7): `TTUCompletChildData` — child type + per-type payload (§1.4).

An **empty child** is encoded `ZLO==0xFF && ZHI==0x00` (an inverted, impossible
box). `child[c].isEmpty()` tests exactly this; that is the canonical "this slot is
unused" sentinel.

When `header.LPtr==1`, child slot 11 is *not* an AABB child; its two words hold
`PARENTPTRHI` (bits 0–16) and `CHILDPTRHI` (bits 32–48), the high 17 bits of the
absolute parent/child addresses.

### 1.3 AABB decode (the quantization the hardware reverses)

The 8-bit child bounds expand to FP32 world bounds with a **pure power-of-two**
scale per axis. For axis x (y, z identical):

```
dx  = bitsToFloat(XSCL << 23) / 256.0          // = 2^(XSCL-127) / 256, i.e. an exponent-only scale
lo.x = XMIN + dx * XLO
hi.x = XMIN + dx * (XHI + 1)                    // note the +1 on the high endpoint
```

So `XSCL` is a biased FP32 exponent: the per-axis quantization step is
`2^(XSCL-127-8)`. `XSCL==0` collapses the axis (zero scale). The `+1` on the high
endpoint guarantees the dequantized box never under-covers the true child box —
critical for not missing hits. To make a child box safely cover a triangle, pick
`XMIN <= min over the axis`, an `XSCL` whose `dx` makes `256*dx` span the box, and
`XLO=0, XHI=255` (the full quantized range) as a conservative choice.

### 1.4 Child data byte (`TTUCompletChildData`, byte 7 of each child)

Bit 6 of the data byte is the **leaf bit** (`OFS_LEAF_BIT=6`): if any of bits
0–6 (`LEAF_MASK`, 7b) is set, the child is a leaf; if all zero it is an interior
complet child. The remaining low bits are interpreted per the complet's LEAFTYPE:

- **Interior (complet) child** — `TTUCompletChildData_Complet`: bit 7 = `INV`
  (invert the ray-op result for this child).
- **TriRange leaf** — `TTUCompletChildData_TriRange`: `LINES[2:0]` (number of extra
  128-B triangle-block cachelines spanned, see §3), `TRIIDX[6:3]` (the
  *exclusive* end triangle index for this child's range), `ALPHA[7]`.
- **ItemRange leaf** — `TTUCompletChildData_ItemRange`: `COUNT[5:0]`, `INV[7]`.
- **InstanceNode leaf** — `TTUCompletChildData_InstanceNode`: `SIZE[5:0]`
  (instance-node size in 64-B units), `INV[7]`.

The leaf address is resolved from `header.{LeafPtrLo,LeafPtrHi}` (sign-extended
and made relative if `RELPTR==1`), then advanced by summing earlier leaf children's
sizes — for triangles, `LINES<<7` bytes per earlier leaf; for instances,
`SIZE<<6`; for items, `COUNT` entries.

### 1.5 Child complet address derivation

For an interior child *c* (`LPtr==0`): the child-complet base is
`thisComplet + (CHILDOFS << 7)`, and child *c* lands at base + 128 B × (number of
earlier interior children). Equivalently the validator computes the child complet
index as `parentIdx + CHILDOFS + innerNodeCount`, then `addr = nodesBase + (idx<<7)`.
**Interior children of a complet are a contiguous run of 128-B complets**, in slot
order, starting `CHILDOFS` complets after the parent.

Confidence: **very high** — header/child layouts, the AABB formula, the leaf-bit
and per-type payloads, and the address arithmetic are all cross-checked between the
struct definitions, the dequantize routine, and the validator's traversal.

---

## 2. Acceleration-structure memory layout

The hardware traverses from a **complet pointer** handed to it via a stack-init
write (§6) — it does **not** read any AS-level header. The driver wraps the raw
arrays in a software bookkeeping header (`BvhHeader`) only so the runtime/SM knows
where each array starts; the TTU silicon only ever sees complet/triblock/instance
records and the pointers inside them.

```
AS buffer (one bottom-level AS)
+--------------------------------------------------------------+
| BvhHeader (software bookkeeping; offsets into this buffer)   |
|   .nodesOffset      -> complet array base   (128-B aligned)  |
|   .trianglesOffset  -> triangle-block base  (128-B aligned)  |
|   .instanceNodeOffset-> instance-node base  (64-B aligned)   |
|   .numNodes, .numTriBlocks, .size, .baseVA, ...              |
+--------------------------------------------------------------+
| complet[0]   = ROOT      (128 B)   <- traversal starts here  |
| complet[1]   ...                                             |
|   ... (numNodes complets, contiguous, 128-B stride)          |
+--------------------------------------------------------------+
| triBlock[0]  (128 B)                                         |
|   ... (numTriBlocks triangle blocks, 128-B stride)           |
+--------------------------------------------------------------+
| (instance nodes / item-range prim buffers, if any)          |
+--------------------------------------------------------------+
```

Key facts:

- **Root = complet index 0** at `nodesBase`. It carries `PARENTLEAFIDX == 0xF`.
- The **complet array** is `numNodes` contiguous 128-B records; child-complet
  pointers (§1.5) index within it.
- The **triangle-block array** is `numTriBlocks` contiguous 128-B records at
  `trianglesBase`; TriRange leaf pointers point into it.
- Required alignment: complet buffer 128-B, triangle-block buffer 128-B, instance
  nodes 64-B — the validator faults otherwise.
- `BvhHeader.baseVA` is the GPU virtual address the AS will live at; relative
  pointers inside complets are interpreted against the *runtime* address, so a
  self-consistent buffer can be built position-independently using `RELPTR`/relative
  `CHILDOFS`.

The SM hands the TTU `nodesBase` (= `baseVA + nodesOffset`) as the root-complet
pointer in a `StackInitComplet` request with `enterRoot=1` (§6).

Confidence: **high** — array bases/strides and "root = complet 0 with
PARENTLEAFIDX 0xF" are confirmed by the reference reader and the validator;
`BvhHeader` is a driver convenience, not a hardware-read structure (stated as such).

---

## 3. Triangle / primitive leaf (`TTUTriangleBlock`, 128 bytes)

A triangle block holds 1–16 triangles (or 1 AABB-equivalent), 128-B aligned.
Layout is mode-dependent; the **uncompressed mode** is the one to hand-build.

### 3.1 Block header (`TTUTriangleBlockHeader`, the last 32 bits, bit 992 = byte 124)

```
PX[4:0] PY[9:5] PZ[14:10] PID[19:15]   (compressed: delta bit-widths, store value-1)
FNC[19:17]   M[23:20] (triangle count - 1)   SHIFT[28:24]
ALPHA[28:26]   MODE[31:29]
```

- **MODE** (bits 29–31): `0`=Uncompressed, `1`=Compressed, `2`=Motion,
  `3`/`4`=Visibility (Ada), `5`=DisplacedMicromesh (Ada). Reserved → error `0x37`;
  note the base hardware *rejects uncompressed blocks at traversal* with error
  `0x38` on some configs — see §3.3.
- **M** (bits 20–23): triangle count minus 1 (so `M=0` ⇒ 1 triangle).
- **FNC** (bits 17–19) / **ALPHA** (bits 26–28): per-triangle force-no-cull and
  alpha bits (uncompressed mode keeps them here, one bit per triangle).

### 3.2 Uncompressed triangle layout (the hand-build target)

In uncompressed mode the vertices are stored **as plain FP32, packed nine floats
(36 bytes) per triangle, starting at byte 0**:

```
TTUTriangleBlock (uncompressed), triangle t at float index 9*t:
  byte  0..11 : v0.x, v0.y, v0.z   (triangle 0)
  byte 12..23 : v1.x, v1.y, v1.z
  byte 24..35 : v2.x, v2.y, v2.z
  byte 36..71 : triangle 1 (v0,v1,v2), if M>=1
  ...
  byte 120    : TIDBASE  (bit 960): userTriangleID of triangle 0 (U32)
                triangle t's TID at bit (960 - 32*t)
  byte 124    : header (MODE/M/FNC/ALPHA...)  (bit 992)
```

So a single uncompressed triangle is: write nine FP32 at bytes 0–35, the 32-bit
user triangle id at byte 120, and the header word at byte 124 with `MODE=0, M=0`
(and the alpha/fnc bits as desired). Vertex 0 *is* the implicit base vertex
(`VBASE`) at bytes 0–11; nothing else in the 128-B block needs to be set for one
triangle. (The reference writer does exactly `vPtr[0..8] = v0,v1,v2;
setBitfield(TIDBASE,32,id)` and leaves alpha/fnc in the header.)

### 3.3 TriRange semantics — how a complet child names triangles

A TriRange leaf child names a contiguous range of triangles inside the
triangle-block array. The complet's `FIRSTTRIIDX` gives the start local index; the
child's `TRIIDX` gives the *exclusive* end; `LINES` counts how many additional
128-B cachelines the range spans (a "line" = one 128-B triangle block). For a
single triangle that is the *only* leaf of the root: `FIRSTTRIIDX=0`,
child `TRIIDX=1` (range `[0,1)`), `LINES=0` (one block). The leaf base pointer
(`header.LeafPtr*`, relative if `RELPTR`) points at the first triangle block.

Confidence: **very high** for the uncompressed vertex layout (taken directly from
the block writer) and the header bit map; **high** for the exact TriRange
`triIdx/triEnd/lines` encoding for the single-triangle case (cross-checked against
the range-merge accessor). One caveat (§7): whether a given silicon revision will
*traverse* an uncompressed block, or requires the compressed format, is gated by
error `0x38` — verify against the target arch before relying on uncompressed at
traversal time (it is always valid as the builder's intermediate form).

---

## 4. Instance node (`TTUInstanceNode`, 64 bytes) — TLAS leaf

```
TTUInstanceNode, 64 B, 64-B aligned (16 x U32)
  bit   0 : USERINSTANCEIDLO   (32b)
  bit  32 : USERINSTANCEIDHI   (32b)   (== 0 reserved for top-level hits)
  bit  64 : ROOTCOMPLETPTR     (49b)   pointer to the bottom-AS root complet
  bit 113 : MASK               ( 8b)   per-instance visibility mask
  bit 121 : MASKVALID          ( 1b)
  bit 122 : HEADER             ( 6b)   TTUInstanceNodeHeader (flip-front-face,
                                       force-no-cull, relptr, alpha-opaque...)
  bit 128 : MATRIX             (12 x FP32)  object->world 3x4 transform
```

The header bits (`TTUInstanceNodeHeader`, the 6 bits at 122): `FORMAT[0]` (must be
0; reserved → error `0x35`), `FLIPFRONTFACE[1]`, `FORCENOCULL[2]`, `RELPTR[3]`
(root-complet pointer relative to the instance node), `FORCEALPHAOPAQUE[4]`,
`FORCEOPAQUEALPHA[5]`. The 3×4 `MATRIX` transforms the ray into the bottom AS's
object space; `ROOTCOMPLETPTR` is the bottom-AS root the TTU recurses into.

Confidence: **high** — layout and header bits taken from the struct; the
49-bit-pointer / 12-float-transform shape matches the TLAS recursion the validator
expects.

---

## 5. Ray descriptor and hit/result records (the TTU register interface)

These are the 128-bit records written/read through the TTU's internal address
space (`ttuAddr`, see §6). All are 16 bytes (`reg[4]`).

### 5.1 Ray inputs

**`TTUReqData_RayOrigin`** (slot `0x40`): `originX/Y/Z` FP32 at bits 0/32/64,
`tmin` FP32 at bit 96.

**`TTUReqData_RayDirection`** (slot `0x50`): `directionX/Y/Z` FP32 at bits
0/32/64, `tmax` FP32 at bit 96.

**`TTUReqData_RayFlags`** (slot `0x00`, 96 bits used): `type[2:0]` (0=Ray, 3=Box,
4..6=Beam, 7=instancing), `cullMode[5:4]` (0=none,1=cull-back,2=cull-front,
3=disable-edge-test), `frontFaceCW[6]`, `terminateOnHit[7]`, `stackLimit[10:8]`,
`rayOp[19:16]` (4b, see ray-op table), `traversalOrder[23:21]` (0=sort by tmin,
1=tree order, 2..7=axis sort), `instMask[31:24]`, then 2-bit per-leaf-class mode
fields at bits 32..62, and `rayOpParamA[79:64] / rayOpParamB[95:80]`.

Ray validity is enforced: `tmin>tmax` → error `0x1A`; NaN/negative t →
`0x19`; bad origin/dir → `0x1B`; reserved ray type → `0x10`.

### 5.2 Hit result (`TTUResData_Hit`, read from slot `0x300`)

A 16-byte union discriminated by a type field in the top bits (bit 124, 4b on
base hardware): `Triangle=0x0`, `NodeRef=0x8`, `TriRange=0x9`, `ItemRange=0xA`,
`InstanceNode=0xB`, `TerminateUnknown=0xC`, `Error=0xE`, `None=0xF`.

**`TTUResData_Hit_Triangle`** (the closest-hit payload):
```
bit   0 : userTriangleID (32b)
bit  32 : T  (31b non-negative FP32; full 32b signed if cullMode==DisableEdgeTest)
bit  63 : ALPHA (1b)
bit  64 : U  (31b barycentric)
bit  95 : BACKFACING (1b)
bit  96 : V  (31b barycentric)
```
So a triangle hit returns: the user triangle id, the ray parameter `t`, the
barycentric `(u,v)`, plus alpha/back-facing flags. (When `cullMode==DisableEdgeTest`
the t/u/v are full signed FP32 with no flag bits — used for barycentric
differentials.)

Other result subtypes: **NodeRef** (a complet ptr + child to resume traversal,
+ t at bit 64), **TriRange** / **ItemRange** / **InstanceNode** (pointer + t for
SM-side leaf processing), and **Error** (`PTR[48:0]`, `LINES[63:61]`,
`ERROR_CODE[101:96]` — the 6-bit error code, e.g. `0x30` bad complet format).

Triangle vertices for SM-side processing are also read back via the `TriFetch0..3`
result slots (`0x340..0x370`): v0/v1/v2 FP32 across the four 16-byte reads, plus
userTriangleID/alpha/predicate.

Confidence: **very high** — every offset is from the interface struct definitions
and matches the `ttuAddr` read-slot map.

---

## 6. Programming the TTU (the `ttuAddr` slot map + the kick sequence)

The TTU has an internal 16-bit-indexed state file. `TTUST.128` writes a 128-bit
record into a slot; `TTULD.128` reads one out. The slot indices (`TTUReqAddr`):

| slot (`ttuAddr`) | dir | record |
|------|-----|--------|
| `0x000` | W | `RayFlags` (primary) |
| `0x040` | W | `RayOrigin` (origin xyz + tmin) |
| `0x050` | W | `RayDirection` (dir xyz + tmax) |
| `0x080/0x0C0/0x0D0` | W | `RayFlags2/Origin2/Direction2` (second slot, dual/instancing) |
| `0x100/0x110/0x120` | W | `StackRestore01/23/45` |
| `0x200` | W | `StackInitComplet` (NodeRef + optional instance ptr) |
| `0x210` | W | `StackInitTriRange` |
| `0x220` | W | `StackInitTriRangeComplet` |
| `0x280` | W | `StackInitTriFetch` |
| `0x300` | R | `Hit` (the result record, §5.2) |
| `0x310` | R | `HitTrianglePtr` |
| `0x320` | R | `HitInstance` (user instance id + instance ptr) |
| `0x330/0x3B0` | R | `TransformedRay0/1` |
| `0x340..0x370` | R | `TriFetch0..3` (the three hit-triangle vertices) |
| `0x380/0x390/0x3A0` | R | `Stack01/23/45` (the traversal stack for spill/restore) |

The root is supplied through `StackInitComplet` (slot `0x200`), whose payload is a
`TTUNodeRef`: `CPLPTR[48:0]` = the root complet's 49-bit address, `CPLCHILD[59:56]`
= child to resume after, `ENTERROOT[60]` = 1 to start at the complet root.
To begin a fresh traversal at a bottom-AS root: `CPLPTR = nodesBase`, `ENTERROOT=1`.

**Instruction sequence** (the SASS kick):
1. `TTUST.128 ttuAddr[0x40], <origin.xy>, <origin.z|tmin>` etc. — fill RayFlags,
   RayOrigin, RayDirection.
2. `TTUST.128 ttuAddr[0x200], <nodeRef.lo>, <nodeRef.hi>` — the root NodeRef
   (`CPLPTR=nodesBase, ENTERROOT=1`).
3. `TTUOPEN` (or `.DUAL`) — open the traversal context (waits on the stores).
4. `TTUGO` — launch autonomous traversal; the TTU walks complets with a 6-deep
   hardware stack and runs ray-box / ray-triangle tests at leaves.
5. `TTULD.128 Pu, Rd2, Rd, ttuAddr[0x300]` — read the `Hit` record (+ hit predicate).
   `TTULD.CLOSE` reads the final result and tears the context down.

Confidence: **very high** — the slot map matches the request-address enum field
for field; the command sequence matches the decoded `TTU*` opcode semantics.

---

## 7. Hand-building a minimal 1-triangle bottom-level AS

Enough is recovered to assemble a buffer the TTU will traverse without hanging.
Minimal layout (position-independent, using a relative leaf pointer):

```
offset 0   : complet[0]  (ROOT, 128 B)
offset 128 : triBlock[0] (128 B, one uncompressed triangle)
total      : 256 B, 128-B aligned base
```

**Root complet[0]** (only the non-zero fields):
- `FORMAT = 0`, `LEAFTYPE = 1` (TriRange), `LPTR = 0`, `RELPTR = 1`.
- `PARENTLEAFIDX = 0xF` (root), `PARENTOFS = 0` (allowed because root).
- `XMIN/YMIN/ZMIN` = the triangle's AABB low corner; `XSCL/YSCL/ZSCL` chosen so
  `256 * (2^(SCL-127)/256)` spans each axis (i.e. `2^(SCL-127) >= axis extent`).
- `FIRSTTRIIDX = 0`.
- Leaf pointer `LEAFPTRLO/HI` (with `RELPTR=1`) = relative offset from the complet
  to `triBlock[0]` (here +128, sign-extended per the 17-bit-hi rule), so the leaf
  base resolves to the triangle block.
- `CHILDOFS` left unused (no interior children) — but note interior-child code
  requires `CHILDOFS != 0`; since there are no interior children, leave the slots
  marked as leaves so the interior path is never taken.
- `child[0]`: AABB `XLO=YLO=ZLO=0, XHI=YHI=ZHI=255` (full quantized box = the whole
  complet box, conservative cover), `RVAL = 0xFF` (visible to any mask), `DATA`
  with the leaf bit set (bit 6) and `TriRange{ TRIIDX=1, LINES=0, ALPHA=0 }`.
  `LINES` is a **block-index delta** (number of 128-B triangle blocks between the
  first and last block of the range), so for one triangle in one block `LINES=0`
  — the consumer reads it as "this leaf's range ends at `TRIIDX` within the single
  starting block." Do **not** hand-pick the raw DATA literal: the leaf bit (bit 6)
  and `TRIIDX` (bits 3–6) overlap in the union, so set it via the field setters
  (`init(leaf=true); triRange.setLines(0); triRange.setTriIdx(1); triRange.setAlpha(0)`).
- `child[1..11]`: empty (`ZLO=0xFF, ZHI=0x00`).

**triBlock[0]** (uncompressed, one triangle):
- Bytes 0–11: v0 xyz (FP32). Bytes 12–23: v1. Bytes 24–35: v2.
- Byte 120: userTriangleID (U32, e.g. 0).
- Byte 124 (header): `MODE=0` (uncompressed), `M=0` (1 triangle), `FNC/ALPHA` = 0.

**The kick:** write the ray (origin+tmin@0x40, dir+tmax@0x50, flags@0x00), write
`StackInitComplet@0x200` with `CPLPTR = &complet[0]`, `ENTERROOT=1`; then
`TTUOPEN → TTUGO → TTULD@0x300`. A hit returns `Hit_Triangle` with `t`, `(u,v)`,
and `userTriangleID`.

A raw-SASS `TTUGO` driving this buffer directly needs **no `BvhHeader`** — the
hardware only consumes the root complet pointer handed to it via `StackInitComplet`
and the complet/triblock records it reaches from there. The `BvhHeader` (§2) is
only required if the driver/runtime issues the `TTUGO` on your behalf and expects
its own bookkeeping prefix.

**Verdict: yes — the byte-level format is fully recovered and sufficient to
hand-build a 1-triangle BVH**, with two things to verify on the specific target
silicon before firing on hardware: (a) whether that arch traverses uncompressed
triangle blocks or rejects them with error `0x38` (if so, emit a compressed block —
the §3.1 header bit-widths and the delta packing are recovered but more fiddly);
and (b) the exact sign-extension of the relative leaf pointer for the `+128`
offset (the 17-bit-hi / `<<7` rules in the leaf-base resolver). Everything else —
header fields, child encoding, AABB quantization, the slot map, the ray/hit
records — is high-confidence and directly byte-addressable.

---

## Appendix — the bitfield packing rule

Every "field at bit B, width W" follows one little-endian rule. Viewing the record
as a `U32[]` (or `U8[]` for the 1-byte child-data fields):
```
word  = base[B >> 5]
field = (word >> (B & 31)) & ((1u << W) - 1)        // when (B&31)+W <= 32
```
Fields never straddle a 32-bit word in these structures (the layout is chosen so
each field fits in one word). FP32 fields (XMIN, vertices, t/u/v, transform) are
just the raw IEEE-754 bit pattern at the byte offset. Signed fields (PARENTOFS,
CHILDOFS, relative pointers) are sign-extended from their width before use.
