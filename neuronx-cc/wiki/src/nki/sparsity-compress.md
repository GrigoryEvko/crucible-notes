# SparsityCompress — Datapath & Compressed Format

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp311/cp312 identical). The compress/tag validators and the `LoadTagsSparse` emitter live in `libwalrus.so` (`.text`/`.rodata` VA == file offset); the tag-decode and consumer scatter live in `libBIRSimulator.so` (`birsim::`); the format spec is the docstring of `neuronxcc/apis/sparsity.cpython-310-…so`. Every address came from `nm -DC` (these binaries carry **no** static `.symtab` — see [§Grounding](#grounding-and-the-stripped-isa-so)). Other wheels differ; treat every address as version-pinned.*

## Abstract

Trainium's structured-sparsity path turns a dense moving operand into a **(compressed-values, tag)** pair so the PE array can multiply only the kept elements. Two PE-front-end micro-ops own the *encode* side — `SparsityCompress` (opcode **0xE0 = 224**) emits both the value buffer and the tag stream; `SparsityCompressTag` (opcode **0xE1 = 225**) emits the tag stream alone. The PE array then consumes the tags through a third op, `LoadTagsSparse` (opcode **6**), emitted by the codegen function `generateLoadTagsSparse`, ahead of the sparse matmul. This page documents the *datapath* and the *compressed format*: the opcode identity and engine binding, the `uint16` four-nibble tag word and its flipped column order, the structural 4:1 ratio and the row-group LUT, and the consumer contract the reference simulator enforces.

The single fact a reimplementer must internalise is that the mask is **structural, not magnitude**. Unlike NVIDIA's 2:4 path — where the runtime (ASP / cuSPARSELt) prunes by weight magnitude — `SparsityCompress` performs *no* selection of its own. There is no top-k, argsort, threshold, or absolute-value logic anywhere in the encoder (verified: zero such strings in `sparsity.so`). The op is a pure **gather + index-pack** keyed by an externally-supplied structured mask; "which elements are kept" is decided upstream (by the training-time sparsification), and the compiler simply trusts the mask's nonzero pattern. The hardware never re-derives it.

The format is fixed: a dense `(K, N)` ubyte source compresses to a `(K, N//4)` value tensor plus a `(K, N//4)` `uint16` tag tensor. Each tag word packs **four `uint4` nibbles**, one per lane of a group of four; each nibble is the dense column index (0..15) the kept value of that lane maps to. The decode is `dense_position(lane) = (tag_word >> (4*lane)) & 0xF`. The producer pre-flips the column order so hardware lane order matches dense columns. The 128 PE rows are organised into compression groups with base offsets `{0, 32, 64, 96}`, selected at codegen time by a four-entry row-group LUT.

For reimplementation, the contract is:

- **The opcode identity & engine binding** — `0xE0`/`0xE1` are PE-array (Matmult) engine micro-ops, gated to core_v4 only, grouped with `PeManageSeed`/`Ldweights-MX`/`Activate2`, *not* the Pool/Act vector engine.
- **The `uint16` four-nibble tag word** — `[3:0]`=lane0 … `[15:12]`=lane3, decoded by `(tag>>(4*lane))&0xF`, with the producer-side flipped column order (col 127 first) the ISA requires.
- **The structural 4:1 invariant** — `4 * out_elems == in_elems` along the compressed axis, enforced by both validators; the group of four is the "1-of-4" structured pattern.
- **The row-group LUT** — `translateToRowGroup` maps `(tilePos, tileSize)` → a row-group code `{1,2,4,8}/{3,12}/15`, which the emitter maps to a tag-nibble shift `{0,4,8,12}`.
- **The consumer contract** — `generateLoadTagsSparse` emits a **64-byte** `LoadTags` (opcode 6) binary with `num_active_cols = 4 * NumElementsPerPartition`; the simulator asserts `num_active_rows % 4 == 0` and exactly `== 128`.

| | |
|---|---|
| **Encode opcodes** | `SparsityCompress` = `0xE0` = 224; `SparsityCompressTag` = `0xE1` = 225 |
| **Compress validator** | `core_v4::is_valid_sparsity_compress` @ `libwalrus 0x14b8c00` |
| **Tag validator** | `core_v4::is_valid_sparsity_compress_tag` @ `0x149bb80`; `…_tag_subdim_count` @ `0x149ba60` |
| **Engine** | PE array (Matmult / `EngineType::PE` = 3), core_v4 only (`a1 == 4` gate) |
| **Row-group LUT** | `translateToRowGroup` member @ `0x1346100`, free fn @ `0x1089e70`; LUT `{1,2,4,8}` @ `.rodata 0x1DD33F0` |
| **Consumer emitter** | `CoreV3GenImpl::generateLoadTagsSparse` @ `0x135e960` → 64-byte `LoadTags` (opcode 6) |
| **Sim consumer** | `birsim::InstVisitor::visitInstMatmultSparse` @ `libBIRSimulator 0x1f0a10` |
| **Tag decode** | `…visitInstMatmultSparse(…)::{lambda(short,int)#1}` = `sub_1A1D70` @ `0x1a1d70` |
| **Format spec** | `neuronxcc.apis.sparsity.to_compressed_sparse` docstring (`(K,N)`→`(K,N//4)` uint16) |
| **Mask** | **structural** — caller-supplied; no magnitude/top-k logic in the encoder |

This is the *format/datapath* face of the sparse path. The **producer** that emits these instructions (the Penguin `MatMulSparseOp` lowering and `InstMatmultSparse`) is *6.8.8 — Sparse matmul lowering* *(planned)*. The bit-level **encoding** of all nine PE bundles (dense / sparse / MX / quantize) is [PE Matmul Encoding](../isa/pe-matmul-encoding.md) (2.10); the **PE engine** functional model is [PE Engine](../arch/pe-engine.md) (1.08). Read this page for *what the bytes mean*; read those for *the array that consumes them*.

---

## 1. Opcode Identity & Engine Binding

### Purpose

Establish that `0xE0`/`0xE1` are real PE-array micro-ops, what gates them, and where they sit in the validation dispatch — because the natural assumption (that "compress" is a vector-engine op) is wrong, and getting the engine binding wrong derails the whole datapath.

### Entry Point

```text
core_v4::is_valid_neuron_engine_instruction  @0x1502120   (per-instruction validator chain)
  └─ linear "try each opcode validator" within the PE-array (Matmult) engine branch:
       … is_valid_pe_manage_seed         (PeManageSeed)
       --> is_valid_sparsity_compress    (0xE0)   @0x14b8c00
       --> is_valid_sparsity_compress_tag(0xE1)   @0x149bb80
       is_valid_activate2                (Activate2)
       is_valid_smx1_lw                  (Ldweights-MX)
```

The sparsity validators are adjacent to `PeManageSeed`, `Activate2`, and `Ldweights-MX` in the dispatch chain — all **PE-array (Matmult) engine** micro-ops. They are **not** in the Pool/Act vector branch. This places `SparsityCompress`/`Tag` as PE-front-end ops that pre-process the moving (activation) operand into a `(values, tag)` pair the array consumes.

> **CORRECTION — "compress" is a PE-array op, not a DVE/vector op.** The op name (and the historical D-series notes) suggest a DVE datamove. The validation dispatch proves otherwise: the validators sit between `is_valid_pe_manage_seed` and `is_valid_activate2`/`is_valid_smx1_lw`, inside the Matmult-engine branch. The simulator reinforces this — there is *no* `birsim::visitInstSparsityCompress`; the only sim datapath that touches the compressed format is the matmul consumer (`visitInstMatmultSparse`, [§5](#5-consumer-contract)).

### Algorithm — the opcode gate

Recovered from `is_valid_sparsity_compress` `@0x14b8c00`:

```c
// is_valid_sparsity_compress(INST_UNION, CORE_VERSION) @ libwalrus 0x14b8c00
char is_valid_sparsity_compress(/* a1 = core_version */ ..., char a7 /* opcode byte */, ...) {
    if (a1 != 4) return 0;                       // core_v4 ONLY — op absent on v2/v3-enum cores
    if (!is_valid_enum(4, opcode, ...)) return 0;
    char has_events = has_valid_neuron_events(4, opcode, ...);
    if (opcode != -32) return 0;                 // -32 == 0xE0 == 224  -> SparsityCompress  [CONFIRMED]
    if (!has_events) return 0;
    /* … tensor + dtype + invariant checks (§2, §3) … */
}
```

`SparsityCompressTag` is identical in shape but gates on `opcode != -31` (`-31 == 0xE1 == 225`), verified at the top of `is_valid_sparsity_compress_tag` `@0x149bb80`.

The opcode-name table (`core_v4::enum_variant_string_opcode` `@0x143fd80`) `strncpy`s case 224 → `"SparsityCompress"` and case 225 → `"SparsityCompressTag"` (mnemonic strings interned in `libwalrus.so` `.rodata`; both confirmed present by `strings`). Case 6 → `"Ldtags"` is the consumer opcode of [§5](#5-consumer-contract).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `core_v4::is_valid_sparsity_compress` | `0x14b8c00` | 0xE0 validator (values + tag) | CERTAIN |
| `core_v4::is_valid_sparsity_compress_tag` | `0x149bb80` | 0xE1 validator (tag only) | CERTAIN |
| `core_v4::is_valid_sparsity_compress_tag_subdim_count` | `0x149ba60` | tag-path 4:1 + encoding-mode select | CERTAIN |
| `core_v4::is_valid_neuron_engine_instruction` | `0x1502120` | PE-engine validator chain (host) | HIGH |
| `core_v4::enum_variant_string_opcode` | `0x143fd80` | opcode → mnemonic (`strncpy`) | HIGH |

> **NOTE — namespace split.** The *validators* are `neuronxcc::core_v4::…` (weak `W` symbols). The *codegen* functions ([§4](#4-row-group-translation), [§5](#5-consumer-contract)) are `CoreV3GenImpl::…` members. Both are correct — "CoreV3GenImpl" is the gen-3 backend codegen class, while "core_v4" is the ISA-validation namespace for NeuronCore-v3 ("Cayman", arch 30). Do not conflate the two; the gen labels are off by one between the two subsystems.

---

## 2. Tensor Layout & Structural Invariants

### Purpose

Pin down the `INST_UNION` field layout, the dtype pairing, and — most importantly — the `4 * out == in` element-count invariant that *is* the 4:1 structured ratio.

### INST_UNION field map (SparsityCompress, 0xE0)

`INST_UNION` is passed by value as 16 dwords (64 B). Offsets recovered from the register loads at the top of `is_valid_sparsity_compress` `@0x14b8c00`:

| Field | Offset | Meaning | Confidence |
|---|---|---|---|
| `opcode` | `+0x00` | `0xE0`; feeds `is_valid_enum` + the `!= -32` gate | CERTAIN |
| `dst_dtype` | `+0x0C` | OUT (compressed values) dtype; `is_valid_dtype(.,0)` = write | HIGH |
| `num_active_channels` | `+0x0D` | `start_addr_active_channels(out_addr, .)` | HIGH |
| dst tensor3d | `+0x10..` | `tensor3d_valid(dst, *, dst_dtype, 0,1,1)` | HIGH |
| dst dims `[d0,d1,d2]` | `+0x1A/+0x1C/+0x1E` | OUT tile extents | HIGH |
| `in_dtype` | `+0x20` | MOVING (dense) input dtype; `is_valid_dtype(.,1)` | HIGH |
| `tag_dtype` | `+0x21` | TAG stream dtype; `is_valid_dtype(.,1)` | HIGH |
| `in_active_channels` | `+0x22` | `start_addr_active_channels(in/tag_addr, .)` | HIGH |
| `subdim_count` | `+0x24` | in `{8,9}` or `{32,33}` (group / row-subdim size) | HIGH |
| `compress_dim_sel` | `+0x25` | which axis is compressed; primary path `{2,3,4}` | HIGH |
| src tensor3d | `+0x30..` | `tensor3d_valid(src, *, in_dtype, 1,1,1)` | HIGH |
| src dims `[d0,d1,d2]` | `+0x3A/+0x3C/+0x3E` | IN tile extents | HIGH |

`SparsityCompressTag` (0xE1) uses the same skeleton but validates its three operands with **`tensor2d_valid`** (2-D) instead of `tensor3d_valid`, and reads its dtype/dim fields at slightly shifted offsets ([§3](#3-tag-only-path-0xe1)).

### Algorithm — dtype pairing and the 4:1 invariant

```c
// is_valid_sparsity_compress @0x14b8c00 — tail half (after the opcode gate)

// (a) three operands validated; dst is a WRITE (mode 0), src/tag are reads (mode 1):
if (!start_addr_active_channels(dst, nch) || !start_addr_active_channels(src, nch))   return 0;
if (!tensor3d_valid(dst, ..., dst_dtype, 0,1,1)) return 0;     // OUT compressed values
if (!tensor3d_valid(src, ..., in_dtype,  1,1,1)) return 0;     // IN dense moving operand
if (!is_valid_dtype(dst_dtype, 0, ...)) return 0;             // write (FP32R allowed)
if (!is_valid_dtype(in_dtype,  1, ...)) return 0;
if (!is_valid_dtype(tag_dtype, 1, ...)) return 0;

// (b) DTYPE PAIRING  [CONFIRMED @0x14b8dee]:
//   in_dtype in {13,14,15} pairs with tag_dtype == 5   (group A)
//   in_dtype in {6,7}      pairs with tag_dtype == 9   (group B)
if ( ((u8)(in_dtype - 13) > 2 || tag_dtype != 5)
  && ((u8)(in_dtype - 6 ) > 1 || tag_dtype != 9) ) return 0;

// (c) STRUCTURAL 4:1 ELEMENT-COUNT INVARIANT  [CONFIRMED @0x14b8e14]:
//   cmov-mask the OUT product by which axis `compress_dim_sel` folds, then require
//   4 * (full OUT element count)  ==  (masked OUT product) * (IN element count).
int d0 = out_d0, d1 = out_d1, d2 = out_d2;       // (mapped to v36/v37/v38 in decomp)
if ((u8)(sel - 2) >= 4) d0 = 1;                  // axis not selected -> fold to 1
if ((u8)(sel - 3) >= 3) d1 = 1;
if ((u8)(sel - 4) >= 2) d2 = 1;
if (4 * out_d0 * out_d1 * out_d2 != d0 * d1 * d2 * in_d0 * in_d1 * in_d2) return 0;  // the *4*
if ((u32)(d0 * d1 * d2) <= 3) return 0;          // keep at least one full group of 4

// (d) reserved/zero bitfields + subdim range  [CONFIRMED @0x14b8eb1]:
if (HIWORD(reserved) || tail_qword | (mask_qword & 0xFFFF0000FF000000)
    || (u8)(sel - 2) > 2                          // compress_dim_sel in {2,3,4}
    || ((u8)(subdim_count - 8) > 1 && (u8)(subdim_count - 32) > 1)) return 0;  // {8,9}|{32,33}

// (e) SBUF/quant predicate + final accept  [CONFIRMED @0x14b8f02]:
if (quant_flag == 0 ||
    (quant_flag == 1
     && (((nch - 64) & 0xBF) == 0 || (nch & 0xBF) == 0x20)      // active-channel quant alignment
     && ((out_d0 * out_d1 * out_d2) & 0x1F) == 0))              // OUT element count % 32 == 0
    return (u8)(-nch) >> 7;                       // sign/high-bit accept
return 0;
```

The `*4*` literal on line (c) is the structured group size. The compressed DST holds exactly **one quarter** of the dense SRC elements along the compressed axis — a 1-of-4 ("2:4"-class) structured ratio. The `> 3` guard (line `if ((d0*d1*d2) <= 3)`) forces at least one complete group to survive.

> **GOTCHA — the `4*` is the *only* place the ratio is hard-coded.** The compress op does not carry a ratio field of its own; the ratio is the invariant `4 * out == in`. The producer's *runtime* compression ratio (e.g. 16:4) is consumed upstream into `tileSize → translateToRowGroup` ([§4](#4-row-group-translation)) and never appears as a standalone bundle byte. On the wire, the density that survives is the row-group code at `+0x2C` of the consumer bundle.

> **NOTE — dtype enum ids.** The codes `5/9` (tag) and `13,14,15` / `6,7` (input) are `core_v4` `NEURON_ISA_TPB_DTYPE` ordinals for the low-bitwidth quantized classes (the uint4 / fp8 / int8 family used by structured sparsity). Their exact names are resolved in the dtype-enum reference, not here — but the pairing structure is exact: a 1-byte integer tag code (5) for the `{13,14,15}` input class, a 2-byte code (9) for the `{6,7}` class. The matmul consumer always reads the tag as a 16-bit `short` regardless.

---

## 3. Tag-Only Path (0xE1)

### Purpose

`SparsityCompressTag` generates **just** the metadata stream — for the case where the values are compressed separately (or are implicit). It is structurally a sibling of 0xE0 with three differences a reimplementer must respect.

### Algorithm

```c
// is_valid_sparsity_compress_tag @0x149bb80
if (a1 != 4) return 0;                            // core_v4 only
/* … is_valid_enum / has_valid_neuron_events … */
if (opcode != -31) return 0;                      // -31 == 0xE1 == 225  [CONFIRMED]

// (1) 2-D operand validation (vs tensor3d for 0xE0):
if (!tensor2d_valid(tag_dst, ..., 0,1,1)) return 0;    // tag (write)
if (!tensor2d_valid(op1,     ..., 1,1,1)) return 0;
if (!tensor2d_valid(op2,     ..., 1,0,1)) return 0;

// (2) subdim_count gate: when the subdim flag is set, require sel==5 & flag==1;
//     otherwise the subdim must be in {3,5,9}:
//     0x228 = 0b10_0010_1000  -> bits 3,5,9 set.
result = (subdim_flag) ? (sel == 5 && flag == 1)
                       : (subdim <= 9 && ((0x228 >> subdim) & 1));   // {3,5,9}  [CONFIRMED]

if (!is_valid_sparsity_compress_tag_subdim_count(...)) return 0;     // §3 tail, below

// (3) reserved zeros + same-quadrant address windows:
addr_a = a & 0x1FFFFFFF;  addr_b = b & 0x1FFFFFFF;
if ((u32)(addr_a - 0x2000000) <= 0x3FFFFF && (addr_b - 0x2000000) <= 0x3FFFFF) /* PE tag-buffer */;
return addresses_in_same_sbuf_quadrant(addr_a, addr_b, ...);        // all 3 in one quadrant
```

`is_valid_sparsity_compress_tag_subdim_count` `@0x149ba60` re-checks the 4:1 invariant for the *tag* path and selects a tag-encoding mode by a dtype-class byte (`enc`):

```c
// is_valid_sparsity_compress_tag_subdim_count @0x149ba60
// primary: 4 * tag_d0*tag_d1 == sel-masked product, AND masked*tag_d1 > 3.
if (subdim_flag == 0) {                            // mode A
    if ((enc & 0x70) == 0x40 || (enc & 0xF0) == 0xA0) accept;   // "nibble-packed tag" dtype class
    else accept if (tag_hiword * tag_word2 == value_d0 * value_d1);   // 1x: tags == values
} else {                                           // mode B
    if ((enc & 0x70) == 0x40 || (enc & 0xF0) == 0xA0) accept;
    else accept if (4 * tag_hiword * tag_word2 == value_d0 * value_d1);  // 4x packed tags
}
```

The `(enc & 0x70) == 0x40` / `(enc & 0xF0) == 0xA0` classes mark the **nibble-packed `uint16` tag dtype** (4 tags per word). Otherwise the tags are a plain index tensor with element count either equal (`1x`) or 4× (`4x`) the value-tensor element count.

> **QUIRK — the tag stream is quadrant-confined.** All three 0xE1 operands must lie in the same SBUF quadrant and inside the PE tag-buffer windows (`addr & 0x1FFFFFFF` in `[0x2000000, 0x23FFFFF]`, with sub-windows at `0x2000000`/`0x2200000`/`0x2300000`/`0x2400000`). This is the hardware constraint that the tag stream must be co-resident with the values it indexes — the PE array reads them together.

---

## 4. Row-Group Translation

### Purpose

Map a PE tile position to the row-group code that selects which of the four base offsets `{0,32,64,96}` (and hence which tag nibble) a row group consumes. This is the bridge between the L:R compression pattern and the on-wire bundle byte.

### Algorithm

```c
// neuronxcc::backend::translateToRowGroup(u8 tilePos, u8 tileSize)
//   free fn @0x1089e70 ; CoreV3GenImpl member @0x1346100 (thunks at 0x6043e0 / 0x61a060)
static const u8 LUT[4] = { 1, 2, 4, 8 };           // .rodata @0x1DD33F0, 4-byte stride
                                                    // verified bytes: 01000000 02000000 04000000 08000000
u8 translateToRowGroup(u8 tilePos, u8 tileSize) {
    if (tileSize <= 32) return LUT[tilePos >> 5];   // [0,32)->1 [32,64)->2 [64,96)->4 [96,128)->8
    if (tileSize <= 64) return (u8[]){3,12}[tilePos >> 6]; // [0,64)->3  [64,128)->12  (const 0xC00000003)
    return 15;                                      // tileSize > 64 -> all rows (dense / 128-wide)
}
```

The codes mean: `{1,2,4,8}` = the four 32-strided quadrants; `{3,12}` = two 64-wide halves; `15` = all 128 rows (no sub-grouping). These codes are then mapped to a **tag-nibble shift** by the consumer emitter ([§5](#5-consumer-contract)).

> **NOTE — the `{1,2,4,8}` codes are not the kept-row count.** They are a positional encoding of *which* 32-row quadrant the tile starts in (`tilePos >> 5` indexes the LUT). The number of rows kept per group is the L of the L:R pattern, set upstream by the framework's structured mask, not by this LUT.

### The L:R compression-group structure

The format spec (the `to_compressed_sparse` docstring) is authoritative:

```text
Organizes 128 PE rows into (128 // R) groups based on an L:R sparsity pattern, where L
is the number of rows per compression group. The compression groups do not consist of
consecutive rows and follow a pattern of [0, 32, 64, 96]. For instance, with a 16:4
pattern (4x compression ratio), the row indices in the first group are
  [0, 128, 256, 384, 32, 160, 288, 416, 64, 192, 320, 448, 96, 224, 352, 480].
```

So rows within a group are strided by 128 (the 128×128 window), then by 32 (the quadrant) — interleaving the four `{0,32,64,96}` base offsets. The row-group LUT code identifies the starting quadrant; the `{0,32,64,96}` offsets are the four sub-quadrant bases.

---

## 5. Consumer Contract

### Purpose

The compress op produces the `(values, tag)` pair, but the *only* place the compressed format is interpreted is the sparse matmul. Two functions own the consumer side: `generateLoadTagsSparse` (codegen — emits the 64-byte `LoadTags` bundle) and `visitInstMatmultSparse` (the reference simulator — decodes tags and scatters values back to dense). This section is the reimplementation contract for both.

### Entry Point

```text
CoreV3GenImpl::visitInstMatmultSparse  (walrus orchestrator, see PE-matmul-encoding 2.10)
  ├─ generateLoadTagsSparse(InstMatmultSparse&, dl, hi)  @0x135e960  -> 64-byte LoadTags (opcode 6)
  └─ generateMatMulSparse(…)                                          -> sparse matmul bundle (opcode 7)

birsim::InstVisitor::visitInstMatmultSparse(bir::InstMatmultSparse&)  @0x1f0a10  (reference model)
  └─ {lambda(short,int)#1} = sub_1A1D70 @0x1a1d70  (tag-nibble decode)
```

### Algorithm — emitting LoadTags (codegen)

```c
// CoreV3GenImpl::generateLoadTagsSparse @0x135e960 — GENERATE_ISACODE path
// Struct base r13/v8; offsets verified in disasm @0x135eaf9..0x135ebf9.
void generateLoadTagsSparse(InstMatmultSparse &inst, bool dl, bool hi) {
    u8 *b = bundle;                                 // 64-byte instruction binary
    set_header(b, /*opcode field*/ 6);              // = "Ldtags"/LoadTags (opcode-name table case 6)
    *(u32*)(b + 0x10) = 4 * col_descriptor;         // vtbl-derived col count
    *(u32*)(b + 0x14) = 1;
    *(u16*)(b + 0x18) = 0;
    *(u16*)(b + 0x1A) = NumElementsPerPartition;
    *(u32*)(b + 0x1C) = 0x10001;                    // = 65537 ; two packed u16 {1,1}
    b[0x20]           = tag_dtype_code;             // sub_1348870(tagAP.field48)
    // row-group CODE -> tag-nibble SHIFT (which lane 0..3 within the group of 4):
    u8 pos  = getLegalTilePosition(inst, "instr.col_grp");
    u8 code = translateToRowGroup(this, pos, max(tileSize, colgrp));   // §4
    b[0x2C] = code;                                 // on-wire density / row-group code
    u8 shift;
    switch (code) {
        case 1: case 3: case 15: shift = 0;  break; // single group / dense
        case 2:                  shift = 4;  break;
        case 4: case 12:         shift = 8;  break;
        case 8:                  shift = 12; break;
        default: llvm_unreachable("Unhandled row group val");   // CoreV3GenImpl.cpp
    }
    b[0x21] = shift;                                // selects which 4-bit nibble of the uint16 tag
    attach_metadata(b, "instr.num_active_cols", 4 * NumElementsPerPartition);  // sub_1351230
    fwrite(b, 1, 0x40, Bin);                        // 64-byte bundle
}
```

The `+0x21` shift `{0,4,8,12}` is the key wire artifact: it picks which of the four nibbles (`lane 0/1/2/3`) of each `uint16` tag word *this* row group reads, matching the `{0,32,64,96}` PE-row base offsets ([§4](#4-row-group-translation)). `num_active_cols = 4 * NumElementsPerPartition` because each compressed column expands into a group of four dense columns selected by the tag.

### LoadTags bundle field map

| Field | Offset | Value / source | Confidence |
|---|---|---|---|
| opcode | `+0x00` | `6` (`Ldtags`/LoadTags) | CERTAIN |
| col count | `+0x10` | `4 * col_descriptor` | HIGH |
| const | `+0x14` | `1` | HIGH |
| zero | `+0x18` | `0` | HIGH |
| `NumElementsPerPartition` | `+0x1A` | per-partition tag element count | HIGH |
| packed `{1,1}` | `+0x1C` | `0x10001` | HIGH |
| `tag_dtype_code` | `+0x20` | `sub_1348870(tagAP.field48)` | HIGH |
| `row_group_shift` | `+0x21` | `{0,4,8,12}` from row-group code | CONFIRMED (values) / HIGH (offset) |
| col-group size | `+0x26` | `tagAP.field80->field8` | MEDIUM |
| `row_group` code | `+0x2C` | `translateToRowGroup(pos, max(size,colgrp))` | CERTAIN |

### Algorithm — tag decode + scatter (reference simulator)

```c
// birsim::…visitInstMatmultSparse(…)::{lambda(short,int)#1} == sub_1A1D70 @0x1a1d70
int tag_decode(short tag_word, int lane /* 0..3 */) {
    short v = tag_word;
    for (int k = 0; k < lane; ++k) v >>= 4;         // shift 4 bits per lane
    return v & 0xF;                                  // low nibble = dense column index
}
//  ==>  dense_position(lane) = (tag_word >> (4 * lane)) & 0xF      [CONFIRMED]

// birsim::InstVisitor::visitInstMatmultSparse @0x1f0a10 — consumer asserts + scatter
void visitInstMatmultSparse(bir::InstMatmultSparse &inst) {
    int numActiveRows = …;
    assert((numActiveRows & 3) == 0  && "num_active_rows should be divisible by 4");  // @+0x18E2
    assert( numActiveRows == 128     && "Only the 128-active-rows case is supported!");// @+0x18E8

    // arg0 = weights (cast_to float, 128 rows); arg1 = compressed values (cast_to float);
    // arg2 = uint16 tag stream (integer_cast_to(…,10,…), read as short*).
    short *tag    = (short*) arg2_data;             // the uint16 tag words
    int    numCols = …;                             // = *(arg1.field + 24)
    for (each output partition / element position elemPos) {
        int lane = elemPos & 3;                     // v116 = v97 & 3
        for (int j = 0; j < 4; ++j) {               // group of 4
            short tw  = tag[(elemPos >> 2) + j * getNumElementsPerPartition(tagAP)];
            int   idx = tag_decode(tw, lane);        // (tw >> (4*lane)) & 0xF
            int   row = idx / numCols;               // v98 = idx / numCols
            int   col = idx % numCols;               // v99 = idx % numCols
            scatter(compressed_value, dense[row][col]);   // accumulate into dense position
        }
    }
}
```

### The consumer contract, stated

```text
InstMatmultSparse takes 3 PhysicalAccessPattern args:
  arg0 = weights (stationary, 128 rows)        ; cast_to(…,16,…) = float
  arg1 = compressed moving values (M x Compressed_K) ; cast_to(…,16,…) = float
  arg2 = uint16 tag stream (M x Compressed_K)  ; integer_cast_to(…,10,…) = short*
                                                 packed 4 nibbles/word, flipped column order
Sim asserts (@0x1f0a10):
  num_active_rows % 4 == 0   (group of 4)
  num_active_rows == 128     (only the 128-active-rows case is modelled)
Reconstruction:
  dense_col(lane) = (tag_word >> (4*lane)) & 0xF
  out += moving[col] * weight[dense_col]   over each group of 4
```

> **GOTCHA — the simulator never *executes* the compress op.** There is no `birsim::visitInstSparsityCompress`. The `(values, tag)` pair is produced host-side (the `sparsity.py` golden compressor), and the only simulated datapath is this consumer, which re-expands the tags back to dense. The walrus side only *validates* (0xE0/0xE1) and *codegens* (`LoadTags`/`MatMulSparse`) the instruction binaries — it does not interpret the format either. So the format's ground truth is: the `sparsity.py` docstring (the bit packing), the validators (the legality envelope), and this consumer (the decode).

---

## 6. The Compressed Format — Reimplementation Reference

### The `uint16` four-nibble tag word

```text
Dense source   : (K, N) ubyte (fp8 / int8 moving operand), K = compression axis.
Compressed val : (K, N//4)  — keeps 1 of every 4 along N (ratio R=4 primary).
Tag tensor     : (K, N//4)  uint16; each word = 4 x uint4 dense-column indices.

tag_word bit layout (post-flip, hardware lane order):
   [ 3: 0] = dense col index of lane0   (= packed LAST, hardware col 4k)
   [ 7: 4] = lane1                       (col 4k+1)
   [11: 8] = lane2                       (col 4k+2)
   [15:12] = lane3                       (col 4k+3)   <- packed FIRST (col 127..)
Decode: dense_col(lane) = (tag_word >> (4*lane)) & 0xF      (sub_1A1D70)
```

### The flipped column order (ISA requirement)

The `to_compressed_sparse` docstring is verbatim authoritative:

```text
the ISA requires the uint4 tags to be in "flipped ordering": within each 128x128 window,
the tags are packed such that the first uint4 tag is placed in column 127, followed by
columns 126, 125, and 124. For instance, in a tag tensor of shape [128,256], the tags are
packed as (127, 126, 125, 124), ..., (3, 2, 1, 0), (255, 254, 253, 252), ..., and so on.
```

So within each 128-column window, the four nibbles of a `uint16` map to columns in **descending** order: the first packed nibble goes to column 127, the next to 126, and so on. The producer (`sparsity.py`) pre-flips so that when the `sub_1A1D70` decode reads nibbles back in lane order (lane0, lane1, …), the lane sequence lines up with the dense column sequence the hardware expects. A reimplementer who packs in ascending column order will produce a tag stream the array reads transposed.

> **CORRECTION — "first nibble" is column 127, not column 0.** It is tempting to read `[3:0]` (lane0) as "first" and assign it column 0. The docstring forbids this: the *first packed* `uint4` is column 127. The bit position (lane index) and the column index run in *opposite* directions within a window. The decode lambda reads `(tw >> (4*lane)) & 0xF` in increasing lane order, and the producer's pre-flip is what makes increasing lane ↔ the descending column packing land on the correct dense column.

### Format summary table

| Aspect | Value | Source |
|---|---|---|
| Dense source | `(K, N)` ubyte, K = compression axis | `to_compressed_sparse` docstring |
| Compressed values | `(K, N//4)` (1 of 4 kept) | docstring + `4*out==in` invariant |
| Tag tensor | `(K, N//4)` `uint16`, 4 × `uint4` nibbles | docstring |
| Nibble decode | `(tag >> (4*lane)) & 0xF` | `sub_1A1D70` @ `0x1a1d70` |
| Column order | flipped (col 127 first) within 128-col window | docstring |
| Row groups | base offsets `{0,32,64,96}`, strided 128 then 32 | docstring |
| Row-group code | `{1,2,4,8}/{3,12}/15` → nibble shift `{0,4,8,12}` | `translateToRowGroup` + `generateLoadTagsSparse` |
| ISA gates | core_v4; `(in{13,14,15},tag 5)`\|`(in{6,7},tag 9)`; `4*out==in`; `out%32==0`; subdim `{8,9}/{32,33}`; sel `{2,3,4}`; tag-op subdim `{3,5,9}` | validators @ `0x14b8c00`/`0x149bb80`/`0x149ba60` |
| Mask | **structural** (caller-supplied, no magnitude prune) | `sparsity.so` (no top-k/abs strings) |

---

## 7. Mask Derivation — Structural, Not Magnitude

### The claim

`SparsityCompress` does **not** derive its own mask. The "which elements are kept" decision is made entirely outside these binaries (at training time), and the compiler trusts it.

### The evidence

```text
1. NO selection logic in sparsity.so:  topk / top_k / argsort / threshold / magnitude / abs
   all return ZERO hits in `strings sparsity.so`. The only numpy-class symbols present are
   nonzero / where / random / permute.
2. to_compressed_sparse(mask, weights) asserts:
      "non zero elements in mask and weights not matching got <…>"
   -> nnz(mask) must equal nnz(weights). The op gathers the kept (nonzero) elements per
      compression group into the value tensor and records each kept element's original
      column as a uint4 index, packing 4/word into the uint16 tag.
3. get_rand_mask() is ONLY a test helper ("Provide random mask that is aligned with the
   compression groups") that produces a random structured mask already aligned to the
   {0,32,64,96} groups — it is not part of the production datapath.
4. Error strings: "Invalid compression ratio detected: <R>", "<K> must be divisible by
   ratio (<R>)", "only 2D tensors are supported got shape <…>". The mask shape is M x K,
   K = compression dim (docstring: "Assumes compression dim = 1").
```

So `SparsityCompress` (0xE0) is a pure **structural gather + index-pack** keyed by the externally-provided structured mask — the gather selects the mask's nonzeros, then encodes each kept element's original column as a `uint4` nibble.

> **QUIRK — this is the inverse of NVIDIA's 2:4.** On NVIDIA, the runtime (ASP / cuSPARSELt) prunes by weight magnitude and *produces* the 2:4 structure. Here the structure is an *input*: the training pipeline already produced a structured-sparse weight per the L:R pattern, and the compiler's job is only to gather + index-pack it for the array. The hardware makes no magnitude decision — it executes exactly the mask it is handed. Two implications for a reimplementer: (a) you must supply a *pre-structured* mask aligned to `{0,32,64,96}`; (b) `nnz(mask) == nnz(weights)` is a hard precondition, not a hint.

---

## Grounding and the stripped ISA .so

> **CORRECTION — `neuron_isa.…so` is stripped, and carries none of this.** A prior note suggested the sparsity opcodes/constants could be read directly from `neuron_isa.cpython-310-…so`. They cannot. That binary is **stripped** (`file` reports `stripped`; `readelf -S` shows no `.symtab` and no `.debug_*`; only `.dynsym` with 305 symbols — opcode *typeinfo* like `NEURON_ISA_TPB_OPCODE` but no sparsity members). `strings` on it yields **no** `SparsityCompress`/`compress`/`Ldtags` hits. Every sparsity opcode string, validator, and codegen symbol on this page lives in **`libwalrus.so`** (and the consumer in `libBIRSimulator.so`). Both also lack a static `.symtab` — `nm -C` returns "no symbols", so every cited address was recovered via `nm -DC` (dynamic) plus the IDA decompile sidecars. The mnemonic strings `"SparsityCompress"`, `"SparsityCompressTag"`, `"Ldtags"`, `"LDTAGS"`, and the symbol `generateLoadTagsSparse` are all confirmed present in `libwalrus.so`'s `.rodata`/`.dynsym`.

### Confidence summary

| Claim | Confidence | Anchor |
|---|---|---|
| Opcodes 0xE0/0xE1 + names; PE-engine grouping | CONFIRMED | validator `!= -32`/`!= -31` gates; dispatch chain order |
| `4 * out == in` structural invariant | CONFIRMED | `0x14b8e14` cmov-masked product compare |
| Tag = `uint16` packing 4 × `uint4`; flipped col order | CONFIRMED | `to_compressed_sparse` docstring (verbatim) |
| Nibble decode `(tag>>(4*lane))&0xF` | CONFIRMED | `sub_1A1D70` @ `0x1a1d70` |
| `translateToRowGroup` LUT `{1,2,4,8}`/`{3,12}`/`15` | CONFIRMED | `.rodata 0x1DD33F0` bytes `01 02 04 08`; fn `0x1089e70` |
| Row-group → shift `{0,4,8,12}` | CONFIRMED | `generateLoadTagsSparse` switch @ `0x135e960` |
| LoadTags 64-byte bundle, opcode 6 | CONFIRMED | `fwrite(…,0x40,…)`; case 6 = `Ldtags` |
| Consumer asserts (`%4==0`, `==128`) | CONFIRMED | `0x1f0a10` @ `+0x18E2`/`+0x18E8` |
| Mask is structural, no magnitude prune | CONFIRMED | zero top-k/argsort/abs strings in `sparsity.so` |
| `INST_UNION` field offsets (Sec 2/3) | HIGH | register loads at validator heads |
| dtype enum ids 5/9/13..15/6,7 → named types | INFERRED | ordinals confirmed; type names in dtype-enum ref |
| `compress_dim_sel` 2/3/4 → named axis | INFERRED | reconstructed from cmov masking; no naming string |
| in-HW micro-sequencing of the compress op | SPECULATIVE | op never executed in the simulator — only validated/codegen'd/consumed |

---

## Cross-References

- [PE Matmul Encoding — Dense / Sparse / MX & Quantize](../isa/pe-matmul-encoding.md) (2.10) — the bit-level encoding of all nine PE bundles; the **producer** `generateLoadTagsSparse` / `generateMatMulSparse` / `visitInstMatmultSparse` orchestrator on the walrus side.
- [PE Engine — the Systolic Matmul Array](../arch/pe-engine.md) (1.08) — the 128×128 systolic array, the two-phase load-stationary / matmul protocol, and where sparse diverges from dense.
- *Sparse matmul lowering* (6.8.8, `nki/` *(planned)*) — the **producer**: the Penguin `MatMulSparseOp` lowering and `InstMatmultSparse` that emit these instructions and the `arg0` stationary-weights side.
- [SBUF / PSUM Geometry](../arch/sbuf-psum-geometry.md) — the SBUF quadrant model that the tag-buffer address windows (`addresses_in_same_sbuf_quadrant`) constrain.
- [Front: Worked Example — a matmul end-to-end](../front/worked-example-matmul.md) — the dense matmul this sparse path specialises.
