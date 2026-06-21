# Texture & Surface Pipeline

> *Recovered by binary analysis of `ptxas` and `nvdisasm` V13.1.115 (CUDA 13.1):
> every SASS opcode, modifier, and operand-layout fact below was produced by
> assembling PTX with `ptxas -arch=sm_{75,80,90}` and disassembling the resulting
> cubin with `nvdisasm -c`. Descriptor bit layouts are derived from the structure
> of the operands the instructions consume. Worked examples and exact disassembly
> appear throughout.*

The texture/surface path is the one part of the SASS ISA that still reaches a
**fixed-function unit** instead of an ALU. A texture instruction does not compute
a filtered sample itself; it packages coordinates and a **descriptor selector**
into a request, hands it to the TEX unit, and retires asynchronously when the
result lands. Everything `ptxas` does for `tex.*` / `suld.*` PTX collapses to two
questions: *how are the coordinates packed into registers*, and *how is the
descriptor named* (a constant-bank slot, or a register holding a bindless index).

This page recovers the full instruction family, the operand-packing rules, the
256-bit texture-header and 256-bit sampler descriptors the unit reads, the surface
path with its clamp/scope modifiers, and the Turing+ ray-tracing (TTU) path.

---

## 1. The instruction family

`nvdisasm` prints the following texture and surface opcodes (all confirmed by
round-tripping PTX through `ptxas`):

| SASS opcode | Generated from PTX | Role |
|-------------|--------------------|------|
| `TEX`       | `tex.*` (filtered, optional bias/level) | Sample a texture through a sampler |
| `TLD`       | `tex.*` integer fetch / `tex.level` integer | Fetch an unfiltered texel (texel-of-detail load) |
| `TLD4`      | `tld4.{r,g,b,a}.*` | Gather-4: one channel from a 2×2 footprint |
| `TXD`       | `tex.grad.*` | Sample with explicit screen-space derivatives |
| `TMML`      | mip-level query intrinsic | Compute the mip-map level-of-detail the unit *would* pick |
| `TXQ`       | `txq.*` | Query a field of the texture header |
| `SULD`      | `suld.b.*` | Surface load (typed `.D` / formatted `.P`) |
| `SUST`      | `sust.b.*` | Surface store |
| `SURED`     | `sured.b.*` | Surface reduction (no result returned) |
| `SUATOM`    | `suatom.b.*` | Surface atomic (result returned) |
| `SUQ` / `TXQ` | `suq.*` | Query a field of the surface descriptor (lowers to `TXQ` for dimensions) |

Two facts shape the encoding of the whole family:

- **`SUST`/`SULD`/`SURED`/`SUATOM` share the surface coordinate path.** `ptxas`
  picks `SURED` when the atomic returns no value and is not a compare-and-swap;
  otherwise it picks `SUATOM`.
- **Texture instructions are long-latency and decoupled.** The compiler marks
  every TEX-unit instruction as high-cost for scoreboarding; consumers of the
  result wait on a barrier-style scoreboard the instruction writes when it
  retires (the same mechanism `nvdisasm` shows as the `.reuse`/barrier control
  bits in the scheduling word — see *Instruction Encoding*).

---

## 2. Descriptor selection: bound vs. bindless

Every texture/surface instruction must name a **texture header** (the image
descriptor) and, for filtered sampling, a **sampler descriptor**. `ptxas` emits
one of three addressing forms, and `nvdisasm` distinguishes them in the operand
list:

### 2.1 Bound (constant-bank slot)

The descriptor index is an **immediate**, and the header table is reached through
a constant bank. The instruction carries:

- a **5-bit constant-bank selector**,
- a **texture/sampler pointer index** — 14 bits word-addressed (16 bits
  byte-addressed), so the index must be `< 0x10000` and 4-byte aligned.

This is the default for `.texref` / `.surfref` PTX variables: `ptxas` resolves the
symbol to a fixed slot.

```sass
TEX.SCR.LL    RZ, R2, R0, R2, 0x0, 0x5c, 2D, 0x1   // sm_90, bound: index 0x0, bank 0x5c
TXQ           RZ, R6, R6, TEX_HEADER_DIMENSION, 0x0, 0x5c, 0x1
SULD.D.BA.2D.128.STRONG.CTA.TRAP  R4, [R4], 0x0, 0x5b   // sm_75 bound surface
```

The two trailing immediates (`0x0`, `0x5c`/`0x5b`) are the descriptor index and
the constant-bank offset. `.BA` on the surface ops marks the bound-address form.

### 2.2 Bindless (descriptor index in a register)

The descriptor index lives in a **register** instead of an immediate. On
pre-Hopper this is a general register; from Hopper the descriptor selector is
taken from a **uniform register** (`UR`), reflecting that the handle is uniform
across the warp:

```sass
TEX.LL        RZ, R5, R0, R5, UR4, 0x0, 2D, 0x1      // sm_90 bindless: handle in UR4
SULD.D.BA.2D.128.STRONG.SM.TRAP  R4, [R4], UR4, 0x0  // sm_90 bindless surface
```

Bindless is what `tex.2d ... [%rd_handle, ...]` (a 64-bit handle in a register)
compiles to. The compiler validates that the bound form is only used when the
index is a uniform symbol with a legal aligned offset; otherwise it falls back to
the bindless (register) form.

### 2.3 Driver-managed partial-bindless

`ptxas` recognizes two API-specific bind variants (a DirectX partial-bindless and
an OpenGL partial-bindless mode) in which the bound-surface fast path is
explicitly *disabled* and the register form is forced. These exist so the runtime
can rebind descriptor tables without recompilation.

### 2.4 The three encoding skeletons

Internally each texture/surface opcode carries a union with exactly these
descriptor-selector shapes — directly visible in the operand counts above:

| Form | Selector fields | Disassembly tell |
|------|-----------------|------------------|
| Bound immediate | `bank5` + `index` | two trailing immediates |
| Bindless register | `Rc` / `UR` register | a register/`UR` before the immediates |
| Bound via header-table-base | `bank5` + 6-bit table base + 8-bit tid | thread-indexed table (rare) |

`SULD`/`SUST` additionally split into a **typed** path (`.D`) carrying an explicit
data-size field and a **formatted** path (`.P`) carrying an RGBA component mask;
see §6.

---

## 3. TEX / TLD / TLD4 / TXD: coordinate packing

The hard part of texture encoding is that the **number of source registers is not
fixed** — it depends on the texture dimensionality, whether the access is arrayed,
the LOD mode, offsets, depth-compare, and bindless-ness. `ptxas` computes a packed
register count and `nvdisasm` shows the dimensionality as a trailing token
(`1D`, `2D`, `3D`, `CUBE`, `ARRAY_2D`, …).

### 3.1 Dimensionality

The coordinate-parameter field selects one of eight shapes (the array variants are
the non-array shape with the array-layer bit set):

```
1D    2D    3D    CUBE
ARRAY_1D  ARRAY_2D  ARRAY_3D  ARRAY_CUBE
```

Base coordinate-register contribution: 1D→1, 2D→2, 3D/CUBE/ARRAY_CUBE→3. Array
forms add a layer-index register.

### 3.2 LOD mode

The LOD field selects how the level-of-detail is supplied. `nvdisasm` prints the
mode as a suffix on `TEX`/`TLD`:

| LOD field | Suffix | Meaning | Extra register |
|-----------|--------|---------|----------------|
| (none)    | —      | LOD from screen-space derivatives | — |
| LOD_ZERO  | `.LZ`  | force LOD 0 | — |
| LOD_BIAS_DISCRETE | `.LB` | per-pixel bias | +1 |
| LOD_ABSOLUTE_DISCRETE | `.LL` | absolute level | +1 |
| LOD_LC    | `.LC`  | LOD with per-pixel level-clamp | +1 |
| LOD_BIAS_DISCRETE_LC | `.LB.LC` | bias + clamp | +1 |
| LOD_LC_FORCE_DIVERGENT | — | clamp, divergent path | +1 |

Examples confirming the suffix mapping:

```sass
TLD.SCR.LZ    RZ, R0, R2, R0, 0x0, 0x5a, ARRAY_2D, 0x1   // integer fetch, LOD 0, 2D array
TEX.SCR.LL    RZ, R4, R0, R0, 0x0, 0x5c, 2D, 0x3         // absolute-level sample
```

### 3.3 Per-instruction extra registers

On top of the coordinate and LOD registers, the following add operands:

- **Offset (`aoffi`)** — a packed integer texel offset: +1 register (TEX/TLD/TXD).
- **TLD4 offset** — `tld4` supports a single AOFFI offset (+1) **or** a
  per-texel-programmable offset (`PTP`, +2).
- **Depth-compare (`dc`)** — shadow compare reference: +1 register (not on `TLD`).
- **Multisample (`ms`)** — `TLD` only: the sample index, +1 register.
- **Gradients** — `TXD` supplies dPdx/dPdy: 2 registers for 1D, 4 otherwise.
- **Bindless** — the register descriptor selector counts as one more source.

The compiler packs all of these into the `Ra`/`Rb` register pair (a contiguous
vector starting at `Ra`, overflowing into `Rb`). When source-register packing
(`.SCR`, seen above) is in effect, the count is rounded to the legal
{0,1,2,3,4}-register packing — one register goes to `Ra`, two split `Ra:Rb`, three
become `Ra(64b):Rb(32b)`, four become `Ra(64b):Rb(64b)`. More than four packed
source registers is rejected.

### 3.4 Destination packing and the write mask

A texture instruction can return any subset of {R,G,B,A}; `ptxas` carries a 4-bit
write mask and packs the result into one or two destination registers. With 32-bit
components: a full RGBA fetch returns 4×32b across two 64-bit destination
operands; three components return 96b; one or two return a single 32b/64b
destination. A half-precision-return mode (`f16rm`) halves the footprint — RGBA
becomes two 32-bit registers (two packed `f16x2`).

### 3.5 TLD4 component select

`tld4` gathers one channel from the four texels of a 2×2 footprint. The selected
channel is a 2-bit field that `nvdisasm` prints as `.R`/`.G`/`.B`/`.A`:

```sass
TLD4.SCR.R    RZ, R2, R0, R1, 0x0, 0x5c, 2D, 0x1
TLD4.SCR.B    RZ, R2, R0, R0, 0x0, 0x5a, 2D, 0x1
TLD4.SCR.A    RZ, R3, R0, R0, 0x0, 0x5a, 2D, 0x1
```

### 3.6 TXQ: header queries

`TXQ` reads a single field of the texture header and returns it. `nvdisasm` names
the queried field; for image dimensions it prints `TEX_HEADER_DIMENSION`:

```sass
TXQ   RZ, R6, R6, TEX_HEADER_DIMENSION, 0x0, 0x5c, 0x1
```

`TMML` is the companion query that, instead of reading a field, runs the unit's
LOD computation and returns the level the sampler *would* select for the given
coordinates.

---

## 4. The texture header (image descriptor)

A texture header is a **256-bit (8-DWORD) block** the TEX unit reads to learn the
pixel format, swizzle, base address, tiling layout, dimensions, and sampling
quality of an image. There are five header sub-formats — *blocklinear*,
*blocklinear+colorkey*, *1D-buffer*, *pitch*, and *pitch+colorkey* — selected by a
3-bit header-version field; the common fields sit at identical bit positions
across all five, so a decoder can read format/address/dimensions before it knows
which sub-format it has.

### 4.1 Blocklinear header bit layout

The recovered field map of the primary (blocklinear) sub-format. Bit ranges are
within the 256-bit block; `MW(hi:lo)` denotes a multi-word bit span.

| Bits | Field | Meaning |
|------|-------|---------|
| `6:0`   | `COMPONENTS` | Pixel format / channel-size code (see §4.2) |
| `9:7`   | `R_DATA_TYPE` | Numeric interpretation of R (snorm/unorm/sint/uint/float, ±force-fp16) |
| `12:10` | `G_DATA_TYPE` | …for G |
| `15:13` | `B_DATA_TYPE` | …for B |
| `18:16` | `A_DATA_TYPE` | …for A |
| `21:19` | `X_SOURCE` | Output X swizzle source |
| `24:22` | `Y_SOURCE` | Output Y swizzle source |
| `27:25` | `Z_SOURCE` | Output Z swizzle source |
| `30:28` | `W_SOURCE` | Output W swizzle source |
| `31`    | `PACK_COMPONENTS` | Component packing flag |
| `63:41` | `ADDRESS_BITS31TO9` | Base address [31:9] |
| `80:64` | `ADDRESS_BITS48TO32` | Base address [48:32] |
| `87:85` | `HEADER_VERSION` | Selects one of the five sub-formats |
| `98:96` | `GOBS_PER_BLOCK_WIDTH` | Tiling: GOBs per block (W) |
| `101:99`| `GOBS_PER_BLOCK_HEIGHT`| Tiling: GOBs per block (H) |
| `104:102`| `GOBS_PER_BLOCK_DEPTH`| Tiling: GOBs per block (D) |
| `113:112`| `LOD_ANISO_QUALITY*`  | Anisotropic filter quality |
| `114`   | `LOD_ISO_QUALITY` | Isotropic filter quality |
| `123`   | `DEPTH_TEXTURE` | Marks a depth/shadow texture |
| `127:124`| `MAX_MIP_LEVEL`  | Highest mip level present |
| `144:128`| `WIDTH_MINUS_ONE`| Texture width − 1 |
| `150`   | `S_R_G_B_CONVERSION` | sRGB→linear on fetch |
| `154:151`| `TEXTURE_TYPE`  | 1D/2D/3D/CUBE/array/buffer/no-mipmap (§4.3) |
| `159:157`| `BORDER_SIZE`   | Border behavior (incl. *sampler-border-color*) |
| `175:160`| `HEIGHT_MINUS_ONE`| Texture height − 1 |
| `189:176`| `DEPTH_MINUS_ONE`| Texture depth − 1 |
| `191`   | `NORMALIZED_COORDS` | Normalized vs. unnormalized addressing |
| `210:198`| `MIP_LOD_BIAS`  | Per-texture LOD bias |
| `221:219`| `MAX_ANISOTROPY`| Anisotropy cap |
| `235:232`| `MULTI_SAMPLE_COUNT`| MSAA mode (1×1 … 4×4, plus D3D variants) |
| `247:236`| `MIN_LOD_CLAMP` | Lower LOD clamp |

The base GPU virtual address of the image is split across two spans
(`ADDRESS_BITS31TO9` and `ADDRESS_BITS48TO32`) and is GOB-aligned (the low 9 bits
are implied zero), giving a 49-bit byte address.

### 4.2 Format codes (`COMPONENTS`)

The 7-bit format field enumerates the full set of sampleable formats. The recovered
families:

- **Uncompressed color:** `R32_G32_B32_A32`, `R16_G16_B16_A16`, `A8B8G8R8`,
  `A2B10G10R10`, `R16_G16`, `R32`, `B5G6R5`, `A4B4G4R4`, `A1B5G5R5`, `G8R8`,
  `R16`, `R8`, `G4R4`, `R1`, …
- **Packed/shared-exponent:** `E5B9G9R9_SHAREDEXP`, `BF10GF11RF11`.
- **Subsampled video:** `G8B8G8R8`, `B8G8R8G8`, `Y8_VIDEO`.
- **Block-compressed:** `DXT1`/`DXT23`/`DXT45`, `DXN2`, `BC6H_SF16`/`BC6H_UF16`,
  `BC7U`.
- **Mobile-compressed:** `ETC2_RGBA`, `EACX2`, and the full `ASTC_2D_*` grid
  (4×4 through 12×12).

`R/G/B/A_DATA_TYPE` then say how each channel's bits are interpreted (`NUM_SNORM`,
`NUM_UNORM`, `NUM_SINT`, `NUM_UINT`, `NUM_FLOAT`, plus the two `*_FORCE_FP16`
return modes that feed the `f16rm` half-return path of §3.4).

### 4.3 Texture type

| Code | Type |
|------|------|
| 0 | 1D |
| 1 | 2D |
| 2 | 3D |
| 3 | Cubemap |
| 4 | 1D array |
| 5 | 2D array |
| 6 | 1D buffer |
| 7 | 2D no-mipmap |
| 8 | Cubemap array |

The header `TEXTURE_TYPE` is what the instruction's dimensionality token (§3.1)
must agree with; a `2D` `TEX` against a `3D` header is a malformed program.

### 4.4 Swizzle

The four `*_SOURCE` fields remap header channels to output channels independently.
Each source can be a channel (`IN_R`/`IN_G`/`IN_B`/`IN_A`) or a constant
(`IN_ZERO`, `IN_ONE_INT`, `IN_ONE_FLOAT`). This is how a one-channel `R8` texture
can present as `(r,r,r,1)` or `(0,0,0,r)` to the shader without a separate
instruction.

---

## 5. The sampler descriptor

For *filtered* sampling, the TEX unit reads a second **256-bit (8-DWORD)** block —
the sampler descriptor — independently selected from the texture header (the
texture/sampler split is what makes "one texture, many samplers" cheap). The
recovered layout:

| DWORD.bits | Field | Values |
|------------|-------|--------|
| 0 `2:0`   | `ADDRESS_U` | wrap mode, U axis (below) |
| 0 `5:3`   | `ADDRESS_V` | wrap mode, V axis |
| 0 `8:6`   | `ADDRESS_P` | wrap mode, 3rd axis |
| 0 `9`     | `DEPTH_COMPARE` | enable shadow compare |
| 0 `12:10` | `DEPTH_COMPARE_FUNC` | `NEVER`/`LESS`/`EQUAL`/`LEQUAL`/`GREATER`/`NOTEQUAL`/`GEQUAL`/`ALWAYS` |
| 0 `13`    | `S_R_G_B_CONVERSION` | sRGB on this sampler |
| 0 `22:20` | `MAX_ANISOTROPY` | 1:1 … 16:1 |
| 1 `2:0`   | `MAG_FILTER` | magnification filter |
| 1 `5:4`   | `MIN_FILTER` | minification filter |
| 1 `7:6`   | `MIP_FILTER` | `MIP_NONE`/`MIP_POINT`/`MIP_LINEAR` |
| 1 `24:12` | `MIP_LOD_BIAS` | sampler LOD bias |
| 2 `11:0`  | `MIN_LOD_CLAMP` | lower LOD clamp |
| 2 `23:12` | `MAX_LOD_CLAMP` | upper LOD clamp |
| 2–3       | `S_R_G_B_BORDER_COLOR_{R,G,B}` | sRGB border color |
| 4–7       | `BORDER_COLOR_{R,G,B,A}` | full-precision border color (4×32b) |

### 5.1 Address (wrap) modes

Each of the three axes independently selects one of eight wrap behaviors:

| Code | Mode |
|------|------|
| 0 | `WRAP` (repeat) |
| 1 | `MIRROR` |
| 2 | `CLAMP_TO_EDGE` |
| 3 | `BORDER` |
| 4 | `CLAMP_OGL` |
| 5 | `MIRROR_ONCE_CLAMP_TO_EDGE` |
| 6 | `MIRROR_ONCE_BORDER` |
| 7 | `MIRROR_ONCE_CLAMP_OGL` |

The depth-compare function (8 modes), the magnification/minification/mip filters,
the anisotropy cap, the LOD bias and clamps, and the RGBA border color are all
sampler state — none of it is in the instruction; the instruction only names
*which* sampler. This is the structural reason texture sampling is one SASS
instruction regardless of filter complexity: all the work is table-driven by the
two 256-bit descriptors.

---

## 6. Surfaces: SULD / SUST / SURED / SUATOM / SUQ

Surfaces are the *unsampled*, *coordinate-addressed* read/write path — image
load/store with no filtering, no mip selection, and explicit out-of-bounds
behavior. They consume a texture header for layout but no sampler.

### 6.1 Dimensionality and coordinate registers

The surface coordinate count is fixed by the surface dimension field:

| Surface dim | Coordinate registers |
|-------------|----------------------|
| 1D, 1D-buffer | 1 (`S32`) |
| 1D-array, 2D | 2 (`S64`) |
| 2D-array, 3D | 3 (96-bit) |

### 6.2 Typed (`.D`) vs. formatted (`.P`)

- **`.D` (byte/typed)** carries a **data-size** field; the result/store register
  width follows the element size: 32b→1 reg, 64b→2 regs, 128b→4 regs.
- **`.P` (formatted)** carries an **RGBA component mask** (`R`, `RG`, `RGB`,
  `RGBA`, and the gap-masked variants `RA`, `GB`, …); the register width follows
  the mask (`RG`→64b, `RGBA`→128b).

### 6.3 Out-of-bounds: clamp / trap / zero

The surface clamp field decides what happens to an out-of-range coordinate, and
`nvdisasm` prints it as a modifier:

| Clamp field | Modifier | Behavior |
|-------------|----------|----------|
| `Z_IGN`     | `.IGN`   | Ignore: out-of-bounds reads return zero, writes are dropped |
| `NEAR`      | *(default, no suffix)* | Clamp the coordinate to the edge |
| `TRAP`      | `.TRAP`  | Raise a trap on out-of-bounds access |

The bindless surface form carries a slightly different clamp encoding that adds an
`SDCL` (sampler-descriptor-clamp) value alongside ignore/trap.

```sass
SULD.D.BA.1D.STRONG.CTA          R0, [R0], 0x0, 0x5a   // clamp (default)
SULD.D.BA.3D.STRONG.CTA.IGN      R2, [R8], 0x0, 0x5a   // zero / ignore
SUST.D.BA.1D.STRONG.CTA          [R1], R0, 0x0, 0x5a
SURED.D.BA.1D.MIN.STRONG.SYS.TRAP [R1], R2, 0x0, 0x5a  // trap, sys-scoped reduction
```

### 6.4 Scope and ordering

Surface memory operations are full members of the memory model: they carry a
**scope** (`.CTA` / `.SM` / `.GPU` / `.SYS`) and an **ordering semantic**
(`.STRONG` above; weak/constant forms exist) exactly like `LD`/`ST`/`ATOM`. The
`SURED`/`SUATOM` reduction/atomic op is a sub-field (`ADD`, `MIN`, `MAX`, `AND`,
`OR`, `XOR`, `EXCH`, …); compare-and-swap forces the `SUATOM` (value-returning)
form. The scope/semantic on a surface op is selected by the same rules as a global
atomic, so a system-scoped surface reduction shows `.SYS` while a load defaults to
the launching CTA's scope. See *Memory Hierarchy & Ordering* for the scope lattice
these modifiers index.

### 6.5 SUQ

`SUQ` queries a field of the surface — width, height, depth, channel layout. Because
the surface descriptor **is** a texture header, `ptxas` lowers a surface dimension
query to the very same `TXQ` opcode that `txq` uses, returning
`TEX_HEADER_DIMENSION`:

```sass
TXQ   RZ, R0, R0, TEX_HEADER_DIMENSION, 0x0, 0x5a, 0x1   // from suq.width on a bound surface
```

When the queried field is a compile-time constant of the bound descriptor, `ptxas`
folds it away entirely instead of emitting any query.

---

## 7. The TTU (ray-tracing) path

From Turing onward the SM gains a **Tree Traversal Unit (TTU)** — a second
fixed-function unit, alongside TEX, that walks a bounding-volume hierarchy (BVH)
and reports ray/box and ray/triangle intersections. It is reached by a small
family of operations: *open a traversal ticket*, *kick off the query*, *load the
result*, plus a cache-control and a macro-fusion helper.

### 7.1 How ptxas encodes it

The TTU operations are **fused into a macro** before final SASS encoding. At the
SASS level the traversal collapses to a `TTUMACRO` opcode (the fused
open/kickoff/load sequence) plus a `TTUCCTL` cache-control opcode. These are not
ordinary mnemonics a typical kernel emits; they are produced from the ray-tracing
intrinsics and appear as a macro-expansion in the instruction stream rather than
as a one-line `nvdisasm` mnemonic.

### 7.2 Scheduling

The TTU sits on its **own decoupled execution unit** with its **own virtual
queue**, distinct from both the ALU and the TEX path. Two scheduling facts the
compiler enforces:

- A traversal query is *very* long latency — budgeted at up to thousands of
  cycles, the largest single-instruction latency in the machine model — so the
  result-consuming instruction always waits on the TTU scoreboard.
- The traversal-open operation requires a **minimum wait of 5 cycles** before the
  next dependent issue on Turing, encoded directly into the scheduling word.

Because the TTU is decoupled, a kernel can overlap BVH traversal with independent
ALU and TEX work — the same decoupled-issue model the TEX unit uses, applied to
ray traversal.

---

## 8. Putting it together: what ptxas decides

For a single `tex.2d.v4.f32.f32 {...}, [t, {u,v}]` PTX instruction, `ptxas`:

1. Picks the **dimensionality token** (`2D`) from the access shape.
2. Selects the **LOD suffix** from the PTX form (`tex` → derivative LOD;
   `tex.level` → `.LL`; `tex.grad` → `TXD`; integer fetch → `TLD`).
3. Counts and **packs coordinate + LOD + offset + compare registers** into the
   `Ra:Rb` source vector, applying `.SCR` rounding to a legal 1–4 register
   packing.
4. Sizes the **destination** from the write mask and the half-return mode.
5. Chooses the **descriptor form** — a bound constant-bank slot (immediate index +
   bank) when the handle is a uniform symbol with a legal aligned offset, else the
   bindless register form (`UR` on Hopper).
6. Marks the instruction **long-latency / decoupled** and assigns a scoreboard so
   dependents wait for the TEX unit to retire.

Everything semantic — format decode, swizzle, filtering, wrap, anisotropy, border
color, sRGB, depth compare — is **table-driven** by the 256-bit texture header and
256-bit sampler descriptor the unit reads at runtime. The instruction is, in
effect, a typed pointer-plus-coordinates request into a fixed-function pipeline.
