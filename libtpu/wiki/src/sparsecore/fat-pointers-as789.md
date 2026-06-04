# Fat Pointers (AS7/8/9)

> *Every address, offset, and value on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

This page documents the SparseCore (SC) pointer representation and the two pointer-numbering schemes that coexist in `libtpu.so`: the **AS7/AS8/AS9 "fat-pointer" reserve** declared in the TPU `DataLayout`, and the **SparseCore address-space ID table** the backend actually uses. The headline result is a negative one, and it is reimplementation-critical: **the 160/128/192-bit AS7/8/9 fat pointers are inherited AMDGPU boilerplate that no TPU or SparseCore op ever constructs.** They are carried verbatim in the TPU data-layout string because the TPU `TargetMachine` shares LLVM's AMDGPU buffer-fat-pointer ABI fragment, and the binary even ships AMDGPU's diagnostics for them — but across the entire SC MLIR-lowering, SC IR-emitter, and jellyfish LLO-emitter, the only address spaces ever passed to `LLVMPointerType::get` / `PointerType::get` are `0x00` and the `0xC9..0xE1` / `0x1F5` / `0x1F6` SparseCore band. The two number spaces are completely disjoint.

The mental model to discard is the one a reader coming from AMDGPU would bring: that a SparseCore address is a 160-bit struct packing `{descriptor, core/chip, offset}` into the bits of an AS7 fat pointer. It is not. A SparseCore pointer is **at most a 64-bit LLVM pointer carrying a 32-bit word offset as its in-register value** — the `0xCA`/`0xC9`/`0xCB`/`0xCC` address spaces are absent from the `DataLayout` `p`-list, so they take the default `p:64:64` representation, and the `addrspacecast` that re-tags between them lowers to a value-preserving `SDNode` of type `MVT::i32`. The routing that the AS7 reserve *looks* like it should carry — which core/chip, which tile — rides as **separate SSA operands** instead: the destination-id operand for cross-chip remote casts, and the `tpu_tileid` operand for the on-tile TEC casts.

The page is three units. First, **the AS7/8/9 fat-pointer reserve** — what the three formats are, their per-field bit layout as the AMDGPU ABI defines them, and the byte evidence that the TPU build inherits them dead. Second, **the SparseCore address-space ID table** — the 20 description-bearing IDs across three numeric bands, each ID's backing memory pool and on-tile/off-tile semantics, recovered from three mutually-inverse switch/table functions. Third, **the actual SparseCore pointer representation** — the 64-bit/32-bit-word-offset model, why the tile and core routing are operands not pointer bits, and how a pointer's address-space integer flows through the lowering. The conversion of an MLIR `MemorySpaceCast` into an `llvm.addrspacecast` lives on [addrspacecast ISel](addrspacecast-isel.md); the on-tile 2-operand tile-id cast lowering lives on [Tile-ID Cast](tile-id-cast.md); this page owns the *representation and the AS-id table*.

For reimplementation, the contract is:

- **Do not implement an AS7/8/9 constructor.** No SparseCore op materialises a 160/128/192-bit pointer. The reserve is dead in this build; a reimplementation that drives off the `DataLayout` `p7/p8/p9` entries will look for a constructor that does not exist.
- **The SparseCore pointer is a 64-bit LLVM ptr (default repr) carrying a 32-bit word-offset value.** Allocate your address-space numbers from `{0, 201..225, 501, 502}`, never from `{7, 8, 9}`. These two ranges must stay disjoint.
- **Reproduce the AS-id table exactly.** ID → human description → `MemorySpace` enum → pool name → on/off-tile, with the six reserved gaps (`206,207,209,210,221,222`), the two pool-less "Any" alias supersets (`211 SflagAny`, `225 SflagAnySynctile`), and the two live-but-undescribed IDs (`215 simem`, `220 mar`), as recovered from `AddressSpaceDescription` / `AddressSpaceToMemorySpace` / `MemorySpaceToAddressSpace`.
- **Carry tile and core routing as operands, not pointer bits.** The TEC and TAC tile-id casts are 2-operand `{base, tileId}` (both declare `NOperands<2>`); the SCS/TC/plain casts are 1-operand `{base}` (`OneOperand`). The discriminator is the sequencer's operand arity, not the pointer.

| | |
|---|---|
| **TPU DataLayout** | single instance @ `0x973de15`, emitted by `Triple::computeDataLayout` (`0x1DAF4380`) |
| **AS7/8/9 fragment** | `p7:160:256:256:32-p8:128:128:128:48-p9:192:256:256:32 … ni:7:8:9` (AMDGPU ABI, verbatim) |
| **AS7/8/9 constructor in TPU/SC code** | **none** — zero `…PointerType::get(7/8/9)` in any SC/jellyfish band |
| **SC address-space scheme** | `0x00` + `0xC9..0xE1` + `0x1F5`/`0x1F6` (disjoint from `{7,8,9}`) |
| **AS-id description switch** | `LlvmTpuDialect::AddressSpaceDescription` (`0x135462C0`) — base 201, span 24, + ID 0 / 501 / 502 arms |
| **ID → MemorySpace** | `AddressSpaceToMemorySpace` (`0x14B78800`); reverse `MemorySpaceToAddressSpace` (`0x14B78780`, table `0xAF36CE8`, mask `0x3FFF7F`) |
| **On-tile classifier** | `IsOffTileMemory` (`0x13D7AC00`) = `(ms & ~0x10) != 2` — on-tile only for MS 2 / 18 |
| **SC pointer in-register value** | `MVT::i32` 32-bit word offset (re-tag is value-preserving; `LowerADDRSPACECAST` @ `0x13B70592`) |
| **Tile/core routing** | separate SSA operands (`tpu_tileid` i32 / destination-id), never pointer bits |

---

## The AS7/8/9 Fat-Pointer Reserve

### Purpose

The TPU `TargetMachine` builds its `DataLayout` from the TPU arm of `llvm::Triple::computeDataLayout` (`0x1DAF4380`), which produces the single instance of the string at `0x973DE15`. That string is mostly conventional — a 64-bit default pointer, 32-bit `p2/p3/p5/p6`, 64-bit `p1/p4` — but it also carries three high pointer classes that are *not* TPU-native:

```text
e-m:e-p:64:64-p1:64:64-p2:32:32-p3:32:32-p4:64:64-p5:32:32-p6:32:32-
  p7:160:256:256:32-p8:128:128:128:48-p9:192:256:256:32-
  i64:64- … -n32:64-S32-A5-G1-ni:7:8:9
```

The `p7:160:256:256:32 / p8:128:128:128:48 / p9:192:256:256:32 / ni:7:8:9` fragment is byte-identical to LLVM's **AMDGPU** target `DataLayout`. In AMDGPU these three spaces are the *buffer fat pointer* family: AS7 is a buffer fat pointer (a 128-bit `V#` resource descriptor plus a 32-bit offset = 160 bits), AS8 is the bare buffer resource ("V#", 128 bits, 48-bit index — the "45-bit num_records" diagnostic), and AS9 is a buffer strided pointer (192 bits). The `ni:7:8:9` marker declares all three **non-integral**: they are not interconvertible with integers by `ptrtoint`/`inttoptr`, exactly because their bits are a structured descriptor, not a flat address.

The reason this fragment lives in the TPU layout at all is provenance: the TPU backend is built inside the same LLVM that ships AMDGPU, and the buffer-fat-pointer lowering machinery is compiled in. The binary carries the AMDGPU pass `AMDGPULowerBufferFatPointers` (`0x11999BA0`) and its diagnostics verbatim — `"global variables with a buffer fat pointer address space (7) are not supported"` and `"Use buffer resource pointers (address space 8) instead"` are both present in that pass's body. **None of this is reachable from SparseCore codegen.** It is dead reserve that a reimplementer can ignore — but must know about, so as not to mistake it for the SC pointer model.

### The Three Formats

The per-field layout below is the AMDGPU ABI definition for each space; the TPU build inherits it unchanged. The `DataLayout` quadruple is `size:abi-align:pref-align:index-width` in bits.

| AS | `p`-entry | Size | Index width | AMDGPU role | Field layout (AMDGPU ABI) |
|---|---|---|---|---|---|
| 7 | `p7:160:256:256:32` | 160 bit | 32 bit | buffer **fat pointer** | 128-bit buffer resource (`V#`) + 32-bit offset |
| 8 | `p8:128:128:128:48` | 128 bit | 48 bit | buffer **resource** ("V#") | 128-bit descriptor; 48-bit index ⇒ "45-bit num_records" |
| 9 | `p9:192:256:256:32` | 192 bit | 32 bit | buffer **strided pointer** | 128-bit `V#` + 32-bit index + 32-bit stride |

> **GOTCHA —** these three rows describe what the bits *would* mean **if** AS7/8/9 were ever constructed. In `libtpu` 0.0.40 they never are. The widths and field layout are the byte-identical AMDGPU ABI, and the binary ships the matching AMDGPU diagnostics; the TPU build treats them as inert reserve, with no TPU constructor anywhere (next section). Do not implement these as SparseCore pointer formats.

### The Negative Result — No TPU Constructor

The claim "no TPU op builds an AS7/8/9 pointer" is an instruction-grade negative. The way an MLIR/LLVM pointer type is materialised in this codebase is through `LLVMPointerType::get(ctx, AS)` (`0x1746EB40`) or `PointerType::get(ctx, AS)` (`0x1DA72640`), with the address-space literal in `esi`/`edx` immediately before the call. Sweeping every such call site across the three relevant code bands — the SC MLIR-lowering band (`0x13500000..0x135D0000`), the SC IR-emitter band (`0x13800000..0x13E00000`), and the jellyfish LLO-emitter band (`0x1D400000..0x1D800000`) — the address-space argument is **only ever** in `{0x00, 0xC9, 0xCA, 0xCB, 0xCC, 0xCD, 0xD3}`. There is **zero** occurrence of `7`, `8`, or `9`. The AMDGPU buffer-fat-pointer diagnostics and the `ni:7:8:9` marker resolve to `AMDGPULowerBufferFatPointers` and generic LLVM passes — none of them TPU-specific.

The only structured-pointer aggregate the SparseCore backend *does* build is the `CircularBufferDescriptor` — a 2-element LLVM struct `{CbReg, MemRefDescriptor}` assembled with `mlir::StructBuilder`, where the `CbReg` half is an `LLVMPPCFP128Type` singleton re-used as a 128-bit opaque CBREG handle. That is a first-class struct value backed by the SC CBREG register file, **not** a pointer in a fat address space, and it does not touch the AS7/8/9 reserve either. (The CBREG window itself is the `0x1F5`/`0x1F6` address-space band in the table below.)

> **NOTE — the reserve is not a TPU structured pointer.** It is tempting to read the AS7/8/9 reserve as a `{core/chip, space, offset}` packing for cross-core addressing. The bytes refute it: there is no constructor, the formats are dead AMDGPU boilerplate, and cross-core / cross-tile routing rides separate SSA operands — the destination-id operand for remote casts, the `tpu_tileid` operand for on-tile casts — never pointer bits.

---

## The SparseCore Address-Space ID Table

### Purpose

A SparseCore pointer carries a **numeric address-space ID**, and that ID — not any pointer-bit field — names which physical memory pool the pointer targets and whether it is reachable on-tile or requires a DMA/stream/sync to reach. The ID space is recovered three mutually-consistent ways: the `AddressSpaceDescription` switch maps ID → human string, `AddressSpaceToMemorySpace` maps ID → `MemorySpace` enum, and `MemorySpaceToAddressSpace` is the exact inverse. All three agree to the ID. The `MemorySpace` enum is the canonical SparseCore memory-pool enumeration (22 values, 1-based, with value 8 an unused gap); its pool names come from `stringifyMemorySpace`.

### Entry Point

```text
LlvmTpuDialect::AddressSpaceDescription(int)        (0x135462C0)  ── ID → human string (the authority below)
  ├─ base 201, jump-table span 24 (IDs 201..225)
  └─ explicit arms for ID 0, 501, 502; reserved IDs fall through to "Unknown"

AddressSpaceToMemorySpace(uint)                     (0x14B78800)  ── ID → MemorySpace enum  (IDs 201..224)
MemorySpaceToAddressSpace(MemorySpace)              (0x14B78780)  ── MemorySpace → ID  (reverse table 0xAF36CE8, mask 0x3FFF7F)
stringifyMemorySpace(MemorySpace)                   (0x14B78240)  ── MemorySpace → pool name
IsOffTileMemory(MemorySpace)                        (0x13D7AC00)  ── (ms & ~0x10) != 2  → on-tile gate
GetAnyTypeFromAddressSpace(int)                     (0x1357B400)  ── concrete ID → "Any" may-alias superset
```

### The Three Bands

The 20 description-bearing IDs fall into three numeric bands, recovered byte-for-byte from the `AddressSpaceDescription` switch:

- **ID 0** — the inherited base `Smem` (scalar memory, `MemorySpace 1`). This is the one SC ID that overlaps the conventional LLVM default-AS region; the switch's `default` arm returns `"Smem"` only for `0`.
- **IDs 201..225** — the SparseCore-specific band (base `0xC9`). Of the 25 slots, 17 name a backing pool, 2 (`211`, `225`) name an alias group with no pool, and 6 (`206`, `207`, `209`, `210`, `221`, `222`) are reserved gaps with neither a pool nor a description. Two more (`215`/`220`) carry a live `MemorySpace` (`simem`, `mar`) but no description string, so `AddressSpaceDescription` falls through on them.
- **IDs 501, 502** — the two circular-buffer spaces (`0x1F5`/`0x1F6`), in a separate high band, backing the CBREG windows.

The `AddressSpaceDescription` body is small enough to confirm exactly: for `ID > 500` it returns `"TileSpmem Circular Buffer"` (501) or `"Smem Circular Buffer"` (502); for `201..225` it switches to the names below; the description-less IDs (`206,207,209,210,215,220,221,222`) `return` the running result `"Unknown"`; and the `default` arm returns `"Smem"` for ID 0.

### Complete Table

`MS#` is the `MemorySpace` enum value (1-based; 0 = no canonical pool — an alias group). `tile?` is `IsOffTileMemory == false`, true only for MS 2 and MS 18.

| ID | hex | Description | MS# | Pool | tile? | Notes |
|---|---|---|---|---|---|---|
| 0 | `0x00` | Smem | 1 | `smem` | off | base TPU scalar memory |
| 201 | `0xC9` | TileSpmem | 2 | `tile_spmem` | **ON** | per-tile SC SRAM (KB) |
| 202 | `0xCA` | Spmem | 3 | `spmem` | off | chip-shared SC SRAM (MB) |
| 203 | `0xCB` | HBM | 4 | `hbm` | off | global (GB) embedding tables |
| 204 | `0xCC` | Sflag | 5 | `sflag` | off | sync-flag memory (also MS 22 `sflag_tc`) |
| 205 | `0xCD` | Vmem | 6 | `vmem` | off | TC vector memory (handoff) |
| 206 | `0xCE` | *(reserved)* | 0 | — | — | gap |
| 207 | `0xCF` | *(reserved)* | 0 | — | — | gap |
| 208 | `0xD0` | Dreg | 7 | `dreg` | off | data-register window |
| 209 | `0xD1` | *(reserved)* | 0 | — | — | gap |
| 210 | `0xD2` | *(reserved)* | 0 | — | — | gap |
| 211 | `0xD3` | SflagAny | 0 | — *(alias)* | off | sflag may-alias superset |
| 212 | `0xD4` | SmemAny | 9 | `smem_any` | off | smem may-alias superset |
| 213 | `0xD5` | HBMAny | 10 | `hbm_any` | off | hbm may-alias superset |
| 214 | `0xD6` | Timem | 11 | `timem` | off | per-tile instruction memory |
| 215 | `0xD7` | *(no desc → "Unknown")* | 12 | `simem` | off | SC instruction memory † |
| 216 | `0xD8` | IOVA | 13 | `iova` | off | I/O virtual address |
| 217 | `0xD9` | SflagTile | 14 | `sflag_tile` | off | per-tile sflag bank |
| 218 | `0xDA` | SpmemAny | 15 | `spmem_any` | off | spmem may-alias superset |
| 219 | `0xDB` | TileSmem | 16 | `smem_tile` | off | per-tile SMEM |
| 220 | `0xDC` | *(no desc → "Unknown")* | 17 | `mar` | off | memory-access-region † |
| 221 | `0xDD` | *(reserved)* | 0 | — | — | gap |
| 222 | `0xDE` | *(reserved)* | 0 | — | — | gap |
| 223 | `0xDF` | SflagScs | 20 | `sflag_scs` | off | per-SCS sflag bank |
| 224 | `0xE0` | SmemScs | 21 | `smem_scs` | off | per-SCS SMEM |
| 225 | `0xE1` | SflagAnySynctile | 0 | — *(alias)* | off | sflag-any-synctile (no pool) |
| 501 | `0x1F5` | TileSpmem Circular Buffer | 18 | `tile_spmem_cb` | **ON** | CBREG-windowed TILE_SPMEM |
| 502 | `0x1F6` | Smem Circular Buffer | 19 | `smem_cb` | off | CBREG-windowed SMEM |

> **NOTE —** † IDs 215 (`simem`) and 220 (`mar`) carry a valid `MemorySpace` (12 and 17) but `AddressSpaceDescription` has no arm for them and returns the fall-through `"Unknown"` — a description-switch gap in this build. Their pool names are the canonical ones from `stringifyMemorySpace`; the IDs themselves are live and map cleanly through `AddressSpaceToMemorySpace`. `MemorySpace 22` (`sflag_tc`) re-uses ID 204 alongside MS 5, so the reverse table `dword_AF36CE8` has the one AS (`0xCC`) shared by two `MemorySpace` values (entries 5 and 22).

### On-Tile vs Off-Tile

`IsOffTileMemory` (`0x13D7AC00`) is a one-liner: `return (a1 & 0xFFFFFFEF) != 2;`, i.e. `(ms & ~0x10) != 2`. Clearing bit 4 collapses MS 18 (`tile_spmem_cb`, the CBREG alias) onto MS 2 (`tile_spmem`), so the only two on-tile `MemorySpace`s are tile_spmem and its circular-buffer alias. Everything else — HBM, SPMEM, SMEM, SFLAG, VMEM, DREG, TIMEM, IOVA, MAR, and the per-SCS banks — is **off-tile** and requires a DMA, a stream, or a sync to reach. This single bit-test is the access-semantics gate the DMA and stream lowerings consult, and it is exactly why a TEC needs the tile-id cast: to convert its on-tile `TileSpmem` (201) pointer into an off-tile-addressable `Spmem` (202) pointer for execute-lane addressing.

### The "Any" May-Alias Supersets

Five IDs — `211 SflagAny`, `212 SmemAny`, `213 HBMAny`, `218 SpmemAny`, and the synthetic `225 SflagAnySynctile` — are the alias-analysis wildcards: the may-alias superset the SparseCore LLVM backend assigns to a pointer whose exact tile or core is statically unknown. Three of them (`212`/`213`/`218`) carry a dedicated `*_any` pool (`MemorySpace` 9/10/15, pools `smem_any`/`hbm_any`/`spmem_any`); the other two (`211 SflagAny` and `225 SflagAnySynctile`) carry **no** `MemorySpace` at all — `AddressSpaceToMemorySpace` returns 0 for them, so they are pure alias groupings with no physical pool. `GetAnyTypeFromAddressSpace` (`0x1357B400`) canonicalises a concrete ID to its wildcard:

```text
201 TileSpmem  → 218 SpmemAny        205 Vmem      → 205 Vmem (self; no wildcard)
202 Spmem      → 218 SpmemAny        219 TileSmem  → 212 SmemAny
203 HBM        → 213 HBMAny            0 Smem       → 212 SmemAny
204 Sflag      → 211 SflagAny
```

Calling `GetAnyTypeFromAddressSpace` on an already-wildcard space, or on a leaf space with no superset (Dreg, Timem, IOVA, SflagTile), `LOG(FATAL)`s — these are leaf or already-canonical spaces.

---

## The Actual SparseCore Pointer Representation

### Width and In-Register Value

None of the SC address-space IDs (`0xC9`/`0xCA`/`0xCB`/`0xCC`/…) appears in the `DataLayout` `p`-list, so each inherits the **default `p:64:64`** — a 64-bit pointer representation. But the *value* a SparseCore pointer carries is a 32-bit SparseCore **word offset**: in the SelectionDAG, `TPUTargetLowering::LowerADDRSPACECAST` (`0x13B70480`) emits a value-preserving custom `SDNode` (opcode `0xF3`, set at `0x13B70592`) with `EVT = MVT::i32` (`ecx = 0x7` at `0x13B70597`, where `0x7` is the `MVT::i32` enum value). The supporting diagnostics agree: `"No GEP instruction on HBM address space allowed. On TPU, HBM pointers are at least full 32-bit."` (`0x9FF4DD5`), and `SpmemAlignment` (`0x13DC5500`) computes a *word* alignment by dividing the per-SC byte count by 4. So a SparseCore pointer is at most a 64-bit LLVM ptr whose meaningful content is a 32-bit word offset — never a 160/128/192-bit struct.

> **QUIRK —** the re-tag is value-preserving. An `addrspacecast` between two SC spaces does not change the offset value; it only changes the address-space integer attached to the 64-bit pointer (and, for the tile-indexed TEC/TAC cases, attaches a tile-id operand). `LowerADDRSPACECAST` produces `SDNode 0xF3 / MVT::i32` carrying the same word offset in, the same word offset out. A reimplementer must not model the cast as an arithmetic transform of the address — it is a pure tag change plus an out-of-band routing operand.

### Per-AS Field Layout — The Routing Is Operands, Not Bits

Because the pointer is a flat 32-bit word offset with no structured fields, the routing information that a fat pointer would normally pack into its bits is instead carried as **separate SSA operands** on the casts that establish it. There are two routing dimensions, and each has its own operand:

```text
        SparseCore pointer  =  ⟨ 64-bit LLVM ptr (default repr) ; AS-id ∈ {0, 0xC9..0xE1, 0x1F5/0x1F6} ⟩
                                 value = 32-bit word offset (MVT::i32)

   routing is NOT in the pointer bits — it rides as paired operands on the cast:

     ┌─ tile-indexed (TEC / TAC, per-tile execute lanes) ─────────────────────────┐
     │  cast = tpu_addrspacecast_{spmem,smem,tec,…tec,tac}  →  2 operands         │
     │    operand 0 = base ptr (TileSpmem 0xC9 …)                                 │
     │    operand 1 = tpu_tileid  (i32 from the STILEID hardware register read)   │
     └────────────────────────────────────────────────────────────────────────────┘
     ┌─ singular-per-core (SCS / TC / plain) ─────────────────────────────────────┐
     │  cast = tpu_addrspacecast{,_scs,_tc,…scs}  →  1 operand                    │
     │    operand 0 = base ptr   (no tile id — these lanes are singular per core) │
     └────────────────────────────────────────────────────────────────────────────┘
     ┌─ cross-chip (remote) ─────────────────────────────────────────────────────┐
     │  the destination-id / remoteCoreId operand on the remote DMA / get_remote  │
     │  descriptor carries the {device, core, id} routing (not a pointer field)   │
     └────────────────────────────────────────────────────────────────────────────┘
```

The arity split is the per-AS "field layout" that actually exists: it is encoded in the cast op's operand count, byte-confirmed from the `…::create` constructor signatures (`S6_` in the mangled name = a second `Value` operand; its absence = one operand). `tpu_addrspacecast_tec::create` (`0x146D69C0`) has signature `(OpBuilder, Location, Type, Value, Value)` and emits two `addOperands` calls — `operand 0 = base ptr`, `operand 1 = tile id`, result `Type` added last. `tpu_addrspacecast_scs::create` (`0x146D5F80`) and the plain `tpu_addrspacecast::create` (`0x146D5EA0`) take a single `Value` and emit one `addOperands`. The tile-id source is `tpu_tileid::create` (`0x149883A0`) — signature `(OpBuilder, Location, Type)` with **zero** Value operands, because it reads the SparseCore scalar TID register (`STILEID.VRES` / `stileid.u32`) rather than consuming a value.

| Cast family | Constructor | Operands | Tile id? | Sequencer |
|---|---|---|---|---|
| `tpu_addrspacecast` (plain) | `0x146D5EA0` | 1 `{base}` | no | generic |
| `tpu_addrspacecast_scs` | `0x146D5F80` | 1 `{base}` | no | SCS |
| `tpu_addrspacecast_tc` | `0x146D68E0` | 1 `{base}` | no | TC |
| `tpu_addrspacecast_tac` | — (`NOperands<2>`) | 2 `{base, tileId}` | YES | TAC |
| `tpu_addrspacecast_smem` | `0x146D6500` | 2 `{base, tileId}` | YES | TEC |
| `tpu_addrspacecast_spmem` | `0x146D67E0` | 2 `{base, tileId}` | YES | TEC |
| `tpu_addrspacecast_tec` | `0x146D69C0` | 2 `{base, tileId}` | YES | TEC |
| `tpu_tileid` (the i32 source) | `0x149883A0` | 0 (register read) | — | — |

> **QUIRK —** the 2-operand-vs-1-operand split is determined entirely by the **sequencer**, not by the source/destination address space. Both tile-indexed lanes — TEC and TAC — declare `NOperands<2>` and carry the tile id; the singular-per-core lanes (SCS, TC, and the plain cast) declare `OneOperand` and do not. This is because the tile-indexed lanes address per-tile `TileSpmem` and therefore need a runtime tile-select; the singular sequencer lanes address their memory without a per-tile index. The full 16-cast roster, the from→to AS map, and the lowering body live on [addrspacecast ISel](addrspacecast-isel.md) and [Tile-ID Cast](tile-id-cast.md); this page documents only why the routing is an operand and not a pointer bit.

### Address-Space Integer Flow

The `MemorySpace` enum a SC op carries on its memref attributes is converted to an LLVM address-space integer by `MemorySpaceToAddressSpace` (`0x14B78780`): it validates `ms - 1 <= 0x15` against the bitmask `0x3FFF7F` (rejecting the MS-8 gap and out-of-range values with a `"Unsupported memory space"` `LOG(FATAL)`), then returns `dword_AF36CE8[ms - 1]` — a 22-entry table that is the exact inverse of `AddressSpaceToMemorySpace`. So the data path is: SC op `MemorySpace` attr → LLVM address-space ID (this table) → 64-bit LLVM pointer with that AS → default `p:64:64` repr → 32-bit word-offset value at the ISA. At no stage is a 160/128/192-bit fat pointer constructed, and at no stage does the address-space ID leave the `{0, 201..225, 501, 502}` band.

---

## Related Components

| Name | Relationship |
|---|---|
| `AddressSpaceDescription` (`0x135462C0`) | the authority for the AS-id → name table this page enumerates |
| `MemorySpaceToAddressSpace` (`0x14B78780`) | `MemorySpace` → AS-id forward map (the pointer's address-space integer) |
| `IsOffTileMemory` (`0x13D7AC00`) | the on-tile/off-tile gate that drives the tile-id-cast requirement |
| `tpu_tileid::create` (`0x149883A0`) | the i32 tile-id source fed as the TEC cast's 2nd operand |
| `AMDGPULowerBufferFatPointers` (`0x11999BA0`) | the AMDGPU pass that owns the (unused) AS7/8/9 reserve and its diagnostics |

## Cross-References

- [SparseCore Overview](overview.md) — the navigational entry for Part IX; engine names, per-gen presence, the data path the pointers sit on.
- [SparseCore Hardware Architecture](architecture.md) — the SPMEM ↔ TILE_SPMEM physical split and the four-tier memory model these address spaces name.
- [addrspacecast ISel](addrspacecast-isel.md) — the IR `MemorySpaceCast` → `llvm.addrspacecast` conversion and the full 16-cast from→to AS map.
- [Tile-ID Cast](tile-id-cast.md) — the on-tile 2-operand `{base, tileId}` cast lowering and how the tile id reaches the lowered TileSpmem load/store as a scalar register operand.
- [Stream Gather/Scatter](stream-gather-scatter.md) — the stream lowerings that dispatch on these address-space IDs to pick the embedding gather/scatter intrinsic.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore pointers & DMA — [back to index](../index.md)
