# TTU (RT-core) BVH format — cross-validated layout + concrete byte test-vectors

This pins the **OPAQUE** data contract that `ttu_spec.md` left as a gap: the BVH
node ("complet") byte layout, the triangle/instance leaf formats, the ray + hit
byte formats, and the TTU internal address-space slot map. Everything here is
**recovered from analysis of the RT runtime and hardware-test binaries plus the
BVH builder and nvdisasm SASS** — byte layouts and test vectors are bare interface
facts (not protectable expression).

The format is cross-validated across **three independent producers**, which agree
field-for-field:

1. **RT-core hardware diag test** — constructs acceleration structures with
   *explicit byte writes* and *known geometry*, then fires real TTU queries and
   checks the hit bytes. This is the **ground-truth oracle**: a known triangle →
   known node bytes → known hit, with the full encoder/decoder in the open.
2. **OptiX wide-BVH builder** — the canonical bit-field spec headers (`OFS_*`/`SZ_*`
   constants + accessor bodies + `sizeof` compile-time asserts).
3. **DXR → rtcore acceleration-structure builder** — a second, independent producer;
   translates `D3D12_RAYTRACING_*` build inputs/instance descriptors into the same
   complet/triangle-block/instance-node bytes, and adds the AS buffer header.

All three are public API names (OptiX, DXR/DirectX Raytracing); the layouts below
are the hardware contract, not anyone's source.

---

## 0. Geometry of the format (one paragraph)

A BVH is one contiguous device buffer: an **AS header** (`BvhHeader`, 128-byte
aligned), then a stream of **128-byte complets** (compressed wide-BVH internal
nodes, ≤11 children each), with **128-byte triangle blocks** and **64-byte instance
nodes** as leaves. Every structural pointer is in **128-byte units** (`<<7`). The
TTU walks this with a hardware stack; a ray and its hit live in the TTU's own
**internal 16-bit-indexed address space** (the `ttuAddr` slots), filled by `TTUST`
and read by `TTULD` — *not* in registers or global memory.

---

## 1. Complet — compressed BVH node (128 bytes, 128-byte aligned)

`32-byte header + 11 child records × 8 bytes = 120 used; bytes 120–127 carry the
long-pointer high bits`. The spec headers reserve a 12th child slot
(`CompletFormatMaxChildren = 12`); the builders use **11** usable boxes on Ampere+
(`s_MaxChildren = 11`), **10** on Turing, because slot 11 (the 12th) is reused to
hold the 17-bit high pointer bits when long-pointer mode is on (`lptr = 1`). With
MotionBlur format the effective fanout halves (5 on Ampere+) because each child
consumes two adjacent slots (begin/end box).

### 1a. Header (bytes 0–31)

| byte | bit | size | field | meaning |
|-----:|----:|-----:|-------|---------|
| 0 | 0 | 4 | `format` | child format: 0=Standard, 1=MotionBlur, 2=MultiBox, 3=MotionBlurMultiBox |
| 0 | 4 | 1 | `lptr` | long-pointer mode (slot 11 holds high ptr bits, not a box) |
| 0 | 5 | 1 | `relPtr` | leaf pointer is relative (offset) vs absolute |
| 0 | 6 | 2 | `leafType` | 0=ItemRange, 1=TriRange, 2=InstanceNode, 3=DisplacedMicromesh |
| 1 | 8 | 8 | `xscl` | X scale exponent |
| 2 | 16 | 8 | `yscl` | Y scale exponent |
| 3 | 24 | 8 | `zscl` | Z scale exponent |
| 4 | 32 | 32 | `xmin` | box origin / anchor X (FP32) |
| 8 | 64 | 32 | `ymin` | box origin / anchor Y |
| 12 | 96 | 32 | `zmin` | box origin / anchor Z |
| 16 | 128 | 32 | `itemBaseLo` / `leafPtrLo` | low bits of leaf/item base pointer (128B-aligned → bits[6:0]=0) |
| 20 | 160 | 15/17 | `itemBaseHi` / `leafPtrHi` | high bits of leaf/item base pointer |
| 20 | 165 | 1 | `shearEnable` | misc bit 17: sheared-AABB complet (Ada) |
| 20 | 161/157/161 | 4 ea | `shearSelect/coeff0/coeff1` | misc bits [16:13]/[12:9]/[8:5] when sheared |
| 23 | 184 | 4 | `firstTriIdx` | misc bits [27:24]: first triangle index in leaf range |
| 23 | 188 | 4 | `parentLeafIdx` | misc bits [31:28]: this complet's index in its parent (**0xF = root**) |
| 24 | 192 | 32 | `parentOfs` / `parentPtrLo` | parent ptr: signed rel ofs (bits[38:7], `<<7`) when lptr=0, else abs low 32b |
| 28 | 224 | 32 | `childOfs` / `childPtrLo` | first-child ptr: signed rel ofs `<<7` (128B) when lptr=0, else abs low 32b |
| 120 | 960 | 17 | `parentPtrHi` | (u64@120 bits[16:0]) high abs parent ptr (lptr=1) |
| 120 | 992 | 17 | `childPtrHi` | (u64@120 bits[48:32]) high abs child ptr (lptr=1) |

The 32-bit "misc" word at byte 20 multiplexes leaf-ptr-hi / shear / firstTriIdx /
parentLeafIdx; the encoder masks each field in independently.

When `leafType = 3` (DisplacedMicromesh), the **LSB of each `xmin/ymin/zmin`** is
cleared — they become FP31 (se8m22), and the freed LSBs carry a 3-bit
`shearExpOffset`.

### 1b. Child record (8 bytes), 11 records at bytes 32–119

| byte | size | field | meaning |
|-----:|-----:|-------|---------|
| 0 | 8 | `xlo` | quantized child AABB lower X (8-bit, 256 buckets/axis) |
| 1 | 8 | `xhi` | quantized child AABB upper X |
| 2 | 8 | `ylo` | quantized lower Y |
| 3 | 8 | `yhi` | quantized upper Y |
| 4 | 8 | `zlo` | quantized lower Z |
| 5 | 8 | `zhi` | quantized upper Z |
| 6 | 8 | `rval` | per-child ray-op / range value |
| 7 | 8 | `data` | leaf-descriptor byte (union by `leafType`) |

**`data` byte decode** (offset 7):
- TriRange leaf: bits[2:0] = `lines` (cache lines), bits[6:3] = `triIdx`, bit7 = `alpha`.
- ItemRange leaf: bits[5:0] = item count, bit7 = invert.
- InstanceNode leaf: bits[5:0] = size (5b + 1b force-return-to-SM under motion), bit7 = invert.
- Common: bit6 = leaf bit.

**Empty-child sentinel** (verified — both the builder's `set_child_invalid` and the
spec's `setEmpty` write this): `xlo=0xFF xhi=0x00 ylo=0xFF yhi=0x00 zlo=0xFF zhi=0x00`
(every axis has lo > hi → a degenerate/empty box the traversal skips). All 11 child
slots are pre-initialized to this before the real children are written.

### 1c. AABB quantization / dequantization (decode math — verified two ways)

The 8-bit `scl` is the **biased FP32 exponent** of the per-axis box length. The
decode step `dx` is a power of two:

```
dx  = bitcast_f32(xscl << 23)              # mantissa 0 → exact power of two
lo  = xmin + dx * xlo / 256                # 8-bit lower face
hi  = xmin + dx * (xhi + 1) / 256          # 8-bit upper face, +1 (conservative)
```

Encoding rounds the exponent **up** when the length's mantissa is non-zero, so
`dx ≥ (max−min)` always — the quantized box always **encloses** the real box (a
hardware traversal that over-includes is correct; one that under-includes drops
hits). `lo` rounds down, `hi` rounds up. The OptiX spec's accessor uses the
identical formula (`d = float(exp<<23)/256; box.lo = pos + lox*d`), and the
builder's alternate uncompressed node format reuses the same float-origin +
exponent-byte-scale + 8-bit-lo/hi scheme — three-way agreement on the decode.

---

## 2. Concrete byte test-vectors (the gold — known geometry → known bytes)

These are reproduced from the diag oracle's explicit construction and verified by
recomputation.

### Test vector A — scale exponent from a known root AABB

The BVH test uses root AABB `[-4/3, -4/3, -4/3, +4/3, +4/3, +4/3]` (axis length
= 8/3 ≈ 2.6667):

```
len = 2.6667  →  bitcast = 0x402AAAAB  →  raw exponent = 128, mantissa ≠ 0
xscl = 128 + 1 = 0x81 (129)            # round up because mantissa nonzero
dx   = 2^(129−127) = 4.0               # ≥ 2.6667  ✓ (encloses)
```

The sanity-check bbox `[-1,-1,-1, 1,1,1]` (length = 2.0 **exactly**) gives a clean
power of two with no round-up:

```
len = 2.0  →  bitcast = 0x40000000  →  exp = 128, mantissa = 0
scl = 0x80 (128)   →   dx = 2.0 exactly
```

So a real on-device complet built around the root box has header bytes
`[1]=[2]=[3] = 0x81`, and `xmin/ymin/zmin = -1.3333333 (0xBFAAAAAB)`.

### Test vector B — child quantization (the +X half of the root)

A child whose X spans `[0, +4/3]` inside the root (origin xmin = −4/3, dx = 4.0):

```
xlo = floor(256 * (0    − (−4/3)) / 4)       = 85  = 0x55
xhi = ceil (256 * (4/3  − (−4/3)) / 4 − 1)   = 170 = 0xAA
```

Decoding those bytes back: X = `[−0.00521, 1.33854]`, which conservatively encloses
the target `[0, 1.333]`. So the on-device child record for this box starts
`xlo=0x55 xhi=0xAA …`.

### Test vector C — Triangle hit record (16 bytes = 4× u32)

A synthetic triangle hit `triId=7, t=2.5, u=0.25, v=0.5, alpha=0, facing=1` packs
to the 128-bit result (LE u32 words):

```
data = [ 0x00000007, 0x40200000, 0xBE800000, 0x3F000000 ]
         triId        t|alpha     u|facing    v|<tri-tag>
```

Decode (verified): `data[3]>>31 == 0` → Triangle; `triId = data[0] = 7`;
`t = bitcast(data[1] & 0x7FFFFFFF) = 2.5`; `u = bitcast(data[2] & 0x7FFFFFFF) = 0.25`;
`v = bitcast(data[3] & 0x7FFFFFFF) = 0.5`; `alpha = data[1]>>31 = 0`;
`facing = data[2]>>31 = 1`.

Other result tags (top nibble of `data[3]` when bit31=1):
`0b1111` → miss (`data[3]=0xF0000000`), `0b1001` (bit27=0) → TriRange,
`0b1001` (bit27=1) → DisplacedSubTri, `0b1110` → Error (`errorCode = data[3]&0x3F`).

---

## 3. Ray request + hit record byte formats

The ray is three 16-byte registers; the hit is one 16-byte register, all in the TTU
internal address space.

**Ray flags** (16B): `type`(3b@0), `cullMode`(2b@4), `frontFaceCW`(1b@6),
`terminateOnHit`(1b@7), `stackLimit`(3b@8), `storeT`(1b@13), `rayOp`(4b@16),
`traversalOrder`(3b@21), `instanceMask`(8b@24), per-node pass/fail mode pairs
(2b each @32+), `rayOpParamA/B`(16b@64/@80), `timestamp`(32b@96, motion).

**Ray origin** (16B): `ori.x/y/z` FP32 @0/4/8, **`tmin`** FP32 @12.
**Ray direction** (16B): `dir.x/y/z` FP32 @0/4/8, **`tmax`** FP32 @12.

**Hit record** (16B, `data[0..3]`): type-tagged union — see §1b / Test vector C.
TriRange hit: `triRangePtr = (data[1]&0x1FFFF)<<32 | data[0]`,
`triIdx=(data[1]>>21)&0xF`, `triEnd=(data[1]>>25)&0xF`, `lines=(data[1]>>29)&0x7`,
`t = bitcast(data[2])`.

---

## 4. TTU internal address-space slot map (`ttuAddr[U16]`)

This is the exact gap `ttu_spec.md` flagged as OPAQUE #1 ("which 16-bit index holds
ray.origin.x vs the BVH root vs tmin vs the hit result"). The diag kernel's
`TTUST`/`TTULD` indices are explicit, so the slot map is now recovered:

| `ttuAddr` | what is stored | written/read by |
|-----------|----------------|-----------------|
| `0x000` | ray flags (u32) + ray-op params (u32) + timestamp (u32) | `TTUST` |
| `0x040` | ray ori.x, ori.y \| ori.z, tmin | `TTUST` |
| `0x050` | ray dir.x, dir.y \| dir.z, tmax | `TTUST` |
| `0x100` / `0x110` | traversal-stack restore (preemption resume) | `TTUST` |
| `0x200` | complet stack init: `cplPtrLo` \| `(0x10000000 \| cplPtrHi)` = "enter from complet root" | `TTUST` |
| `0x210` | tri-range stack init: `triPtrLo` \| `(numLines<<29 \| triPtrHi)` | `TTUST` |
| `0x300` | 128-bit **hit result** | `TTULD` |
| `0x380` / `0x390` | traversal-stack save (preemption); `0x390` read with `TTULD.CLOSE` | `TTULD` |

The programming sequence is therefore: `TTUOPEN` → `TTUST` the three ray regs and
the stack-init reg → `TTUGO` → `TTULD [0x300]` (hit) → `TTULD.CLOSE [0x390]` (stack +
teardown). The `0x10000000` bit OR'd into the high pointer word is the **enter-from-
root** flag for the traversal stack.

---

## 5. Triangle block (leaf, 128 bytes, 3–16 triangles)

| byte | bit | size | field | meaning |
|-----:|----:|-----:|-------|---------|
| 0 | 0 | 32 | `vBaseX` | first-vertex anchor X (compressed) / tri0.v0.x (uncompressed) |
| 4 | 32 | 32 | `vBaseY` | anchor Y |
| 8 | 64 | 32 | `vBaseZ` | anchor Z |
| 12 | 96 | 864 | `dataBits` | bit-packed vertex-pos deltas / vertex-IDs (4b each) / alpha+fnc / tri-ID deltas |
| 120 | 960 | 32 | `tidBase` | base triangle ID; per-tri IDs are PID-bit deltas |
| 124 | 992 | 32 | `header` | triangle-block header |

**Header** (u32 @ bit 992): `PX`(5b@0), `PY`(5b@5), `PZ`(5b@10), `PID`(5b@15),
`M`(4b@20, tri count − 1), `shift`(5b@24), `mode`(3b@29: 0=Uncompr, 1=Compr,
2=Motion, 3=ComprVM, 4=MotionVM, 5=Micromesh). PX/PY/PZ/PID store *bit-width − 1*.

**Uncompressed mode** (3 tris/line, full FP32 vertices): tri *i* (i=0,1,2) has its
9 vertex floats at line byte `36·i`, its `triId` at word `30−i` (bytes 120/116/112),
its force-no-cull bit at `word31` bit `17+i`, and `numTris−1` at `word31` bits[21:20].

**Compressed mode**: vertex 0 is `vBase`; other vertices store PX/PY/PZ-bit deltas
folded into the high bits of `vBase` with the low `shift` bits truncated (lossy);
vertices are de-duplicated and referenced by 4-bit IDs (≤16 distinct), so vertex/
edge sharing across a triangle group compresses well.

---

## 6. Instance node — TLAS leaf (64 bytes, 64-byte aligned)

| byte | bit | size | field | meaning |
|-----:|----:|-----:|-------|---------|
| 0 | 0 | 32 | `userInstIdLo` | user instance ID low + packed SBT offset/flags |
| 4 | 32 | 32 | `userInstIdHi` | user instance ID high (encoded) |
| 8 | 64 | 49 | `rootCompletPtr` | pointer to child BLAS root complet |
| 14 | 113 | 8 | `mask` | instance visibility mask |
| 15 | 121 | 1 | `maskValid` | mask field valid |
| 15 | 122 | 6 | `header` | flags: flipFrontFace / forceNoCull / relPtr / forceAlphaOpaque / forceOpaqueAlpha |
| 16 | 128 | 384 | `matrix` | 3×4 affine, **12× FP32, row-major** `m[row*4+col]`; stores the **world→object inverse** transform |

DXR mapping (the second producer's contract): `InstanceMask → mask` + `maskValid`;
`InstanceID → userInstIdHi`; `InstanceContributionToHitGroupIndex/SBT → userInstIdLo`;
`Transform` (3×4) → its **inverse** stored in `matrix`; `AccelerationStructure` (BLAS
VA) → `rootCompletPtr`. Instance flags map: `CULL_FLIP_WINDING→flipFrontFace`,
`CULL_DISABLE→forceNoCull`, `FORCE_OPAQUE→forceAlphaOpaque`,
`FORCE_NON_OPAQUE→forceOpaqueAlpha`.

---

## 7. Displaced micromesh leaf (SM_89, 128B mesh + 128B disp block)

Base-tri 3× FP32 vertices, 3× FP16 displacement directions per axis, FP32
`dispScale`, `subSize`=2 (32×32 = 1024 micro-tris), 42-bit 128B-aligned displacement-
block pointer (`meshAddr + 128`), `baseResolution` value 5 (4⁵ = 1024 utris). A hit
returns `DisplacedSubTri` (`subTriIdx` 6b + 128b-aligned tri ptr + t).

---

## 8. Key constants

| constant | value | note |
|----------|-------|------|
| complet / triblock size | **128 B** | `TTU_CACHE_LINE_BYTES`, 128B alignment everywhere |
| complet header | 32 B | format / scl / origin / pointers |
| child record | 8 B | xlo xhi ylo yhi zlo zhi rval data |
| usable children | **11** (SM_86+) / 10 (Turing) | MotionBlur halves it (5 / —) |
| spec child slots | 12 | slot 11 reused for long-ptr high bits |
| tris / compressed block | ≤16 | 4-bit vertex IDs → ≤16 distinct verts |
| tris / uncompressed line | 3 | full FP32 vertices |
| max tri blocks / range | 7 | `child.data.lines` is 3 bits |
| instance node | 64 B | 64B aligned; 3×4 row-major matrix; world→object inverse |
| pointer unit | 128 B (`<<7`) | all structural offsets |
| root marker | `parentLeafIdx = 0xF` | |
| enter-root flag | `0x10000000` | OR'd into stack-init high ptr word |
| supported SM | 75 / 86 / 87 / 89 | Turing / GA10x / Orin / Ada; VM+shear+micromesh = SM_89 |

---

## 9. Cross-validation summary

| structure | diag oracle | OptiX spec | DXR→rtcore | verdict |
|-----------|:-----------:|:----------:|:----------:|---------|
| complet 128B / 32B hdr / 8B child | ✓ explicit bytes | ✓ `sizeof` assert | ✓ producer | **agree** |
| 11/12 children, slot-11 long-ptr | ✓ (11 used) | ✓ (12 slots) | ✓ | **agree** (reconciled) |
| 8-bit quant, `dx=f32(scl<<23)/256` | ✓ encode+decode | ✓ accessor | ✓ | **agree** (identical math) |
| empty-child `zlo=0xFF zhi=0x00` | ✓ | ✓ `setEmpty` | — | **agree** |
| triangle block 128B, hdr @ bit 992 | ✓ serializer | ✓ spec | ✓ | **agree** |
| instance node 64B, 3×4 row-major | — | ✓ | ✓ + D3D12 map | **agree** |
| hit record 16B tagged union | ✓ decoder | ✓ | ✓ | **agree** |
| ray 16B×3, tmin@12 tmax@12 | ✓ kernel | ✓ | ✓ | **agree** |
| `ttuAddr` slot map | ✓ kernel asm | — | — | **diag-only (definitive)** |

**No disagreements.** The only nuance is the **11 vs 12** children, which is not a
conflict: the format reserves 12 child slots, but the 12th (index 11) is reused for
the long-pointer high bits, so 11 (Ampere+) / 10 (Turing) usable bounding boxes.

**What the diag oracle uniquely pins** (and the other two cannot): the `ttuAddr`
slot map (§4) and the literal test vectors (§2) — a known triangle, the exact bytes
its node gets, and the exact bytes its hit returns. That closes both OPAQUE pieces
(`ttu_spec.md` gaps #1 and #2) and grounds #3 (the node format) with reproducible
numbers.

**Confidence: HIGH** on all structures (each is either built byte-by-byte in the
oracle or backed by `sizeof` asserts in the spec, with the decode math verified by
recomputation), **medium-high** only on the AS-header (`BvhHeader`) per-field byte
offsets, which are member-ordered host-struct fields rather than bit-declared.
