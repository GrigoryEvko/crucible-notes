# TPUMCImm / SyImm32 Operand

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped — full C++ symbols). All addresses are virtual addresses; `.text` / `.rodata` / `.lrodata` are mapped 1:1 (VA == file offset). Other wheel versions differ.*

## Abstract

A constant that an LLO instruction needs — a branch/call target, a sync-flag id, a tile-overlay sflag, a scalar literal — is carried through the LLVM-MC layer as a **`TPUMCImmExpr`**, the TPU back end's target-specific `MCExpr` (`MCExpr::Kind == 5`, the `Target` kind). It is the analog of LLVM's `MCSymbolRefExpr` plus a variant: it wraps a sub-expression (usually an `MCConstantExpr`) and tags it with two orthogonal discriminators — a **`TPUMCImmKind`** that names *what relocation/variant family* the value belongs to, and an 8-bit **encoding-id** that names *which operand-encoding class* (`OpEnc::OpEncodings`) the value is to be packed as. `SyImm32` is the widest scalar member of that family: encoding-id `0x2c`, the full 32-bit scalar immediate, the fallback when the compact zero-/one-/shift-extended forms do not fit.

This page is the canonical reference for the `TPUMCImmExpr` object and the `TPUMCImmKind` enum. It covers the 56-byte object layout written by `TPUMCImmExpr::create`, the enum's six values recovered byte-exactly from the printer (`VK_TPU_none = 0` through `embed = 6`), the `OpEnc::OpEncodings` encoding-id family (`SyImm32 = 0x2c` and its `+4`-cadence neighbours), and how a `SyImm32` reaches actual bundle bits through the `ResourceSolver` packer. The slot *positions* a `SyImm32` finally lands at — the per-gen 16/20-bit immediate ladders — live on [Immediate Slot](slot-immediate.md); this page owns the MC-expr carrier, and links there for the landing.

The page closes with the **overlay PatchOverlay** mechanism: the per-overlay pass that rewrites in-bundle address operands after the program is segmented, recomputing each operand to its overlay-relative word offset and writing it back. PatchOverlay is the post-encode fixup that consumes the same address-operand machinery `SyImm32` rides on, which is why it sits beside the immediate operand here.

For reimplementation, the contract is:

- The **`TPUMCImmExpr` is a 56-byte (`0x38`) `MCExpr` subclass**: `MCExpr::Kind = 5` at `+0x08`, the `TPUMCImmKind` at `+0x18` (`getImmKind()`), the sub-expression pointer at `+0x20`, the 8-bit *imm-base* `h1` at `+0x28` (`getImmBase()`), the 8-bit *encoding-id* `h2` at `+0x29`, the `TPUMCImmType` at `+0x2c`, and the `MCContext*` at `+0x30`.
- The **`TPUMCImmKind` enum is `{0 VK_TPU_none, 1 zext, 2 oneext, 3 shl12, 4 shl16, 5 i32, 6 embed}`** — recovered from the printer's per-kind string suffix, not inferred. `VK_TPU_none = 0` is the relocation-free plain immediate a branch/call target must use.
- The **encoding-id (`+0x29`) is an `OpEnc::OpEncodings` value** from a `getFirst<Class>Encoding` constant function: scalar `ZeroExt 0x20 / OneExt 0x24 / Shl 0x28 / Imm32 0x2c` (a `+4` cadence), vector `VyImm32 0x1a`. `SyImm32 = 0x2c`.
- A **`SyImm32` value is packed by the `ResourceSolver`**: `canAddImmInternal` reads the operand's `OperandType`, the `ImmediateCompatibilityTable` maps it to an `OpEnc` class, `getPackedImm` / `getFullImmediate` allocate the bundle immediate slot(s) and write the encoding-id `0x2c`; a value wider than one slot splits lo/hi across two slots.
- **PatchOverlay rewrites address operands post-segmentation**: per overlay it computes `ConvertOffsetByteToWord(...)`, asserts the operand word is DMA-aligned, and writes the overlay-relative word back into the bundle operand, gated by `!trampolines_patched_` and `encoded_word_offset < 0`.

| | |
|---|---|
| **MC-expr class** | `llvm::TPUMCImmExpr` (`MCExpr::Kind == 5`, `getSubKind() == 1`); vtable `off_21934360` |
| **Object size** | `0x38` = 56 B, `MCContext` `BumpPtrAllocator` (`create` @ `0x13c784a0`) |
| **Kind field** | `+0x18` `int` (`getImmKind()`); `VK_TPU_none == 0` |
| **Encoding-id field** | `+0x29` `uchar` (`OpEnc::OpEncodings`); `SyImm32 == 0x2c` |
| **`SyImm32` encoder** | `getFirstSyImm32Encoding` @ `0x13c63a00` (`return 0x2c`) |
| **Packer entry** | `ResourceSolver::canAddImmInternal` @ `0x13bebce0` → `getPackedImm` @ `0x13bec4e0` / `getFullImmediate` @ `0x13be79a0` |
| **`OperandType` table** | `ImmediateCompatibilityTable` @ `0xaed36d0` (17 × 12 B, `{key=OperandType−13, mask, OpEnc class}`); key `4` (`OperandType 17`) → OpEnc class `5`, mask `0x0f` |
| **Overlay fixup** | `Overlay::PatchOverlay` @ `0x1406a940` (+ per-patch lambda `$_2` @ `0x1406c3e0`) |
| **Slot positions** | see [Immediate Slot](slot-immediate.md) (16-bit pre-V5, 20-bit V5+) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The TPUMCImmExpr Object

`TPUMCImmExpr::create(TPUMCImmKind, MCExpr*, uchar h1, uchar h2, MCContext&)` (`0x13c784a0`) bump-allocates a 56-byte object in the `MCContext` arena and stamps every field as a literal store. The disassembly fixes the layout exactly:

```c
// llvm::TPUMCImmExpr::create(kind a1, MCExpr* a2, uchar h1 a3, uchar h2 a4, MCContext& a5)  @ 0x13c784a0
//   result = MCContext::Allocate(56, align 8)
*(_QWORD *)(result + 0x00) = &off_21934360;   // vtable (TPUMCImmExpr)
*(_QWORD *)(result + 0x08) = 5;               // MCExpr::Kind = 5 (Target / custom)
*(_QWORD *)(result + 0x10) = 0;               // (MCExpr base spare)
*(_DWORD *)(result + 0x18) = a1;              // TPUMCImmKind            <- getImmKind()
*(_QWORD *)(result + 0x20) = a2;              // MCExpr* sub-expr (the value)
*(_BYTE  *)(result + 0x28) = a3;              // h1  imm-base           <- getImmBase()
*(_BYTE  *)(result + 0x29) = a4;              // h2  encoding-id (OpEnc::OpEncodings)
*(_DWORD *)(result + 0x2c) = 0;               // TPUMCImmType (kind-only overload writes 0)
*(_QWORD *)(result + 0x30) = a5;              // MCContext*
```

The fields are independent. The `TPUMCImmKind` at `+0x18` is the *variant*; the encoding-id at `+0x29` is the *operand-encoding class*; the `h1`/imm-base at `+0x28` is a per-operand zero check (a call immediate must have `getImmBase() == 0`). Three accessors read the object:

| Accessor | Address | Reads | Result | Confidence |
|---|---|---|---|---|
| `getSubKind` | `0x13c78da0` | (constant) | `1` — the `MCExpr::Kind==5` sub-discriminator that marks this a `TPUMCImmExpr` | CONFIRMED |
| `getImmKind` | (inlined) | `+0x18` | the `TPUMCImmKind` | CONFIRMED |
| `getImmBase` | (inlined) | `+0x28` | the `h1` byte (`== 0` for branch/call) | CONFIRMED |
| `getBitWidth` | `0x13c78660` | `+0x2c` | `(TPUMCImmType < 2) ? 32 : 16` | CONFIRMED |

`getBitWidth` decompiles to `16 * (this->type < 2) + 16` — a `TPUMCImmType` below `2` is a 32-bit immediate, otherwise 16-bit. The five-argument overload (`0x13c78580`) is field-identical except it writes the caller's `TPUMCImmType` into `+0x2c` instead of `0`. The emitter recovers a `TPUMCImmExpr` from an `MCInst` operand via `GetTPUMCImmExpr` (`0x13a65900`), which checks `*(MCExpr) == 5 && getSubKind() == 1` and otherwise returns *"Could not cast MCExpr to TPUMCImmExpr."* (`isa_emitter_base.cc:49`); the wrapped value is read by `GetValueFromSubExpr` (`0x13a658e0`) as `*(sub-expr + 0x10)` (the `MCConstantExpr` value word).

> **NOTE —** the sibling target-MCExprs share the `+0x18` kind / `+0x28` h1 / `+0x29` h2 layout: `TPUMCFuncExpr::create` (`0x13c77ce0`, also carries a `TPUMCCoreKind`), `TPUMCLinkageExpr::create` (`0x13c78dc0`, 48 B), `TPUMCFuncSizeExpr::create` (`0x13c780a0`, adds a `long` size at `+0x30`), `TPUMCSectionSizeExpr::create` (`0x13c79140`). A reimplementation can model them as one base with a per-subclass tail.

---

## The TPUMCImmKind Enum

`TPUMCImmKind` is an LLVM `VariantKind`-style enum (the "`VK_TPU_*`" family) stored as the `int` at `+0x18`. Its six values are recovered **byte-exactly from `TPUMCImmExpr::printImpl` (`0x13c787a0`)**, whose `switch (this->kind)` prints a human name suffix `" (<name> imm encoding <id>)"` after the value — so the case label *is* the kind value and the string *is* the name. The kind suffix is printed only when `kind != 0`, which is the direct proof that `0` is the plain, suffix-free `VK_TPU_none`:

```c
// TPUMCImmExpr::printImpl  @ 0x13c787a0  (the kind-name switch, decompiled)
switch (*((_DWORD *)this + 6)) {          // this+6*4 = +0x18 = the TPUMCImmKind
  case 1: print("zext imm");   break;     // SyZeroExt   (encoding 0x20)
  case 2: print("oneext imm"); break;     // SyOneExt    (encoding 0x24)
  case 3: print("shl12 imm");  break;     // Sy shift-by-12
  case 4: print("shl16 imm");  break;     // Sy shift-by-16
  case 5: print("i32 imm");    break;     // SyImm32     (encoding 0x2c)   <- this page
  case 6: print("embed");      break;     // embedded / resource-allocated
}
// ... then print(" encoding "), then print(this->h2 /*+0x29 encoding-id*/)
```

| `TPUMCImmKind` | name (printer) | encoding-id (`+0x29`) | producing `getFirst…Encoding` | role | Confidence |
|---|---|---|---|---|---|
| 0 | `VK_TPU_none` | (none; plain `MCConstantExpr`) | — | plain immediate (branch/call offset) | CONFIRMED |
| 1 | `zext` | `0x20` | `getFirstSyZeroExtEncoding` | zero-extended scalar imm | CONFIRMED |
| 2 | `oneext` | `0x24` | `getFirstSyOneExtEncoding` | one-extended scalar imm | CONFIRMED |
| 3 | `shl12` | `0x28` | `getFirstSyShlEncoding` | shift-left scalar imm (×2¹²) | CONFIRMED |
| 4 | `shl16` | — | (shifted variant) | shift-left scalar imm (×2¹⁶) | HIGH |
| 5 | `i32` (SyImm32) | `0x2c` | `getFirstSyImm32Encoding` | FULL 32-bit scalar imm | CONFIRMED |
| 6 | `embed` | (`getSyEncodings`) | resource-allocated | embedded / general scalar enc | HIGH |

The kind is the **relocation / variant discriminator**; the encoding-id at `+0x29` is the **operand-field selector**. They co-vary for the integer-immediate family (kind 5 ⇒ encoding `0x2c`), but they are distinct fields with distinct consumers: the printer reads both, the emitter reads the kind to gate (below), and the packer reads the encoding-id to choose a slot.

> **CORRECTION (H4-IMM-1) —** an earlier reading inferred `shl` was a single kind and that kinds 4 was `VyImm32`. The printer is unambiguous: kind 3 is `shl12 imm` and kind 4 is `shl16 imm` — two scalar shift variants, not a vector kind. `i32 imm` (the `SyImm32` this page documents) is kind 5, and kind 6 is `embed`. The `Vy*` vector encodings have their own encoding-ids (e.g. `VyImm32 = 0x1a`) but are not the same axis as this scalar-`TPUMCImmKind` enum.

### VK_TPU_none Is Mandatory for Branch/Call

A branch or call target must be a *plain* immediate — `VK_TPU_none`. The SparseCore `EmitCallOp<…CallAbsolute>` template (e.g. GLC SCS @ `0x13a5d4c0`) recovers the `TPUMCImmExpr` from operand 0 and gates it:

```c
// EmitCallOp<…, …_CallAbsolute>  @ 0x13a5d4c0  (decompiled, exact)
if (*(_BYTE *)operand0_expr != 5)                       // not an MCExpr Target kind
    RetCheckFail("slot_inst.getOperand(0).isExpr()");   // isa_emitter_base.h:1359
GetTPUMCImmExpr(&imm, operand0_expr+8);                 // cast (kind==5 && getSubKind()==1)
if (*((_DWORD *)imm + 6))                                // imm->kind  (+0x18) != 0
    RetCheckFail("call_imm_expr->getImmKind() == "
                 "llvm::TPUMCImmKind::VK_TPU_none");     // isa_emitter_base.h:1362
if (*((_BYTE *)imm + 40))                                // imm->h1    (+0x28) != 0
    RetCheckFail("call_imm_expr->getImmBase() == 0");    // isa_emitter_base.h:1363
```

The `*((_DWORD *)imm + 6)` read is `imm + 0x18` — the `getImmKind()` field — and the RetCheck demands it be zero. So a call/branch target carries no relocation variant; the encoding-tagged kinds (`zext`/`oneext`/`shl*`/`i32`/`embed`) are built by the immediate-packing and overlay paths, never by the branch emitter. The same assert appears at eight `EmitCallOp` / `EmitBranchOp` instantiations across the VFC/GLC/GFC SparseCore bundle types.

---

## The OpEnc::OpEncodings Encoding-Id Family

The encoding-id stored at `+0x29` is an `OpEnc::OpEncodings` value returned by a `getFirst<Class>Encoding(bool secondOperand)` function. The scalar (`Sy`) classes are single-instruction constant returns at a **`+4` cadence from `0x20`**, byte-verified:

```c
char getFirstSyZeroExtEncoding(bool) { return 0x20; }   // 0x13c63940
char getFirstSyOneExtEncoding (bool) { return 0x24; }   // 0x13c63980
char getFirstSyShlEncoding    (bool) { return 0x28; }   // 0x13c639c0
char getFirstSyImm32Encoding  (bool) { return 0x2c; }   // 0x13c63a00  <- SyImm32
```

| class | fn @ addr | value | note | Confidence |
|---|---|---|---|---|
| SyZeroExt | `0x13c63940` | `0x20` | scalar zero-extend | CONFIRMED |
| SyOneExt | `0x13c63980` | `0x24` | scalar one-extend | CONFIRMED |
| SyShl | `0x13c639c0` | `0x28` | scalar shift-left | CONFIRMED |
| **SyImm32** | `0x13c63a00` | `0x2c` | **full 32-bit scalar imm** | CONFIRMED |
| VyImm32 | `0x13c639e0` | `0x1a − arg` | full 32-bit vector imm | CONFIRMED |
| VyZeroExt / VyOneExt / VyShl | `0x13c63920` / `0x13c63960` / `0x13c639a0` | `0x08 / 0x0e / 0x14 − arg` | vector variants | CONFIRMED |

`SyImm32 = 0x2c` is the widest of the four scalar integer-immediate classes — the full 32-bit scalar immediate the packer falls through to when `ZeroExt` (`0x20`), `OneExt` (`0x24`), and `Shl` (`0x28`) cannot represent the value compactly. The byte `0x2c` is stored verbatim into `TPUMCImmExpr+0x29` and re-emitted by the packer as the slot's encoding-id.

---

## How a SyImm32 Reaches Bundle Bits — the ResourceSolver Walk

A `TPUMCImmExpr` is the *carrier*; the actual placement into a bundle immediate slot is a runtime allocation done by the `ResourceSolver` at the `MachineInstr` layer. The chain has three steps, all byte-anchored.

```c
// ResourceSolver::canAddImmInternal  @ 0x13bebce0
opnd_type = MCInstrDesc.operand[opno].optype_byte;       // TPUOp::OperandType (record byte +3)
rec       = getOperandTypeRecord(opnd_type);             // 0x13c63b80: key = opnd_type-13, binary search
//          getSpecialOpEncoding(MCInstrDesc&, opno)     // 0x13c63a80
opclass   = rec.openc_class;                             // ImmediateCompatibilityTable col 3 (OpEnc class)
// then dispatch to getPackedImm (auto-select) or getFullImmediate (class forced)
```

1. **`canAddImmInternal` (`0x13bebce0`)** reads the operand's `TPUOp::OperandType` from the per-opcode `MCInstrDesc` operand record (the operand-type byte) and maps it through the **`ImmediateCompatibilityTable`** (`0xaed36d0`, 17 entries × 12 B, `{key u32, compat_mask u32, OpEnc class u32}`) via `getOperandTypeRecord` (`0x13c63b80`). `getOperandTypeRecord` first subtracts 13 from the `OperandType`, then binary-searches on that key, so the table's `key` column is `OperandType − 13`. The scalar-immediate row is key `4` (i.e. raw `OperandType 17`): `→ OpEnc class 5` (the scalar integer-immediate chooser) with compat mask `0x0f`.

   > **CORRECTION (H4-IMM-3) —** an earlier reading reported this as "`OperandType 4 → OpEnc class 5`". The `4` is the table *key*, and `getOperandTypeRecord`/`getSpecialOpEncoding` both compute `key = OperandType − 13` before the search (byte-anchored: `v1 = (uchar)(a1 - 13)` at `0x13c63b80`). The raw `OperandType` for the scalar-imm row is therefore `17`, not `4`. The `0x0f` mask is the row's compatibility bitmask; mapping its four set bits onto the `ZeroExt`/`OneExt`/`Shl`/`Imm32` classes is plausible but UNVERIFIED (the bits are not the encoding-ids `0x20/0x24/0x28/0x2c`).
2. **`getPackedImm` (`0x13bec4e0`)** auto-selects the most compact encoding: a jump table on the `OpEnc` class routes class `5` (scalar) / `4` (vector) to the integer chooser, which tests how many high bits of the value are non-zero (the per-encoding width is a subtarget vtable slot) and picks `ZeroExt`/`OneExt`/`Shl`, falling through to **`Imm32 = 0x2c` for a value that needs all 32 bits**. The chosen id is written to `ImmediateEncoding+0x5`.
3. **`getFullImmediate` (`0x13be79a0`)** is the forced-class path: it asserts the class is in `{4,5,6}`, forces `VyImm32 0x1a` (class 4) or **`SyImm32 0x2c`** (otherwise), then allocates the bundle immediate slot(s) from the per-program slot pool, splitting a value wider than one slot into a low and a high half across two slots.

The slot *positions* a `SyImm32` lands at — the 20-bit V5+ ladder (`TC 430/410/…`, `SCS 67/47/27/7`) and the 16-bit pre-V5 pool — are documented in full on [Immediate Slot](slot-immediate.md#how-a-value-is-chosen-and-placed--the-resourcesolver-walk), which owns the `ResourceSolver` pool model. The handoff is: **`OperandType 17` (table key `4`) → `OpEnc` class 5 → encoding-id `0x2c` → a free immediate slot → the per-gen `<gen>ImmediatesEncoder::Encode` bit position.**

> **GOTCHA —** the SparseCore tile-overlay routine (`overlayer::OverlayProgram` @ `0x1395bba0`) *bypasses* the `ResourceSolver` and hand-builds a `SyImm32`: it `movslq`s the tile-overlay sflag, `MCConstantExpr::create`s it, calls `getFirstSyImm32Encoding()` (= `0x2c`), and `TPUMCImmExpr::create(kind=5, expr, h1=0, h2=0x2c, ctx)`, then appends it as an `MCInst` operand-kind-5 in `SLOT_S1`. A reimplementation must support both routes to the same `0x2c` encoding-id: the auto-allocated MachineInstr path and the directly-constructed MCInst path. See [Overlay PatchOverlay](#overlay-patchoverlay--post-encode-address-fixup) below.

---

## evaluateAsRelocatableImpl — the Relocation Path

`TPUMCImmExpr::evaluateAsRelocatableImpl` (`0x13c78d20`) is the `MCExpr` override that the assembler calls to fold the expression to an `MCValue`. It delegates to the sub-expression's `evaluateAsRelocatable` and, on success, stamps the `MCValue`'s `RefKind`:

```c
// TPUMCImmExpr::evaluateAsRelocatableImpl(this a1, MCValue& a2, MCAssembler* a3)  @ 0x13c78d20
bool ok = MCExpr::evaluateAsRelocatable(*(MCExpr**)(this + 0x20), &out, asm);  // fold the sub-expr
if (ok)
    *(_DWORD *)(out + 0x18) = *(unsigned __int8 *)(this + 8);   // MCValue.RefKind <- this->Kind byte
return ok;
```

The folded value comes from the sub-expression at `+0x20`; the `MCValue` `RefKind` (`out+0x18`) is set from the byte at `this+8`. This is a `TPUMCImmExpr`, so `this+8` is the `MCExpr::Kind` field (constant `5`, the `Target` kind), which is what the assembler's relocation logic keys on to recognise a TPU target expression during fixup resolution.

> **CORRECTION (H4-IMM-2) —** an earlier note claimed `evaluateAsRelocatableImpl` copies the `TPUMCImmKind` (`+0x18`) into the `MCValue` `RefKind`. The decompile reads `this + 8` (the `MCExpr::Kind` byte, `== 5`), **not** `this + 0x18` (the `TPUMCImmKind`). The relocation `RefKind` therefore carries the *expression-class* tag (`5`), not the immediate-variant kind; the variant kind drives encoding selection and printing, not relocation. The distinction matters for a reimplementation that wires the fixup table off the `MCValue` `RefKind`.

---

## Overlay PatchOverlay — Post-Encode Address Fixup

When a tensor program is segmented into HBM-resident overlays, address operands that target a bundle now living in a *different* overlay segment must be rewritten to the segment-relative form. `Overlay::PatchOverlay` (`0x1406a940`) is the per-overlay fixup pass; the per-patch-site work is its `$_2` lambda (`0x1406c3e0`). It consumes the same in-bundle address-operand machinery a `SyImm32` rides on, applied after encoding, which is why it sits here.

### Guards and Setup

```c
// Overlay::PatchOverlay(BuildContext& ctx, LloAddress addr, uchar mem_space)  @ 0x1406a940
if (ctx[+136] == 1)                                          // !trampolines_patched_
    RetCheckFail("!trampolines_patched_");                   // overlay.cc:4635
if (ctx[+264] >= 0)                                          // encoding_info().encoded_word_offset
    RetCheckFail("encoding_info().encoded_word_offset < 0"); // overlay.cc:4649 (must be unset)
if (addr < 0) LogFatal("has_offset()");                      // llo_address.h:56
//   compute the overlay's own encoded word offset and stash it:
ctx[+264] = address_util::ConvertOffsetByteToWord(mem_space,
              program[overlay].entry_byte(+248) + addr, target);   // -> encoded_word_offset
```

The pass refuses to run twice (`!trampolines_patched_`) and refuses to re-encode an already-encoded overlay (`encoded_word_offset < 0` must hold on entry). It then computes this overlay's word offset via `address_util::ConvertOffsetByteToWord` and stores it at `ctx+264` (`= 0x108`, the `encoded_word_offset`). Program extents are read through `GetIsaProgramUtil` / `IsaProgram::IsaProgramsCase` and `BundleCountInProgram`; the pass patches **only** TENSOR `IsaProgram`s (`callee_overlay->kind() == Kind::kHloFunction` is the asserted alternative for the function-overlay path).

### The Patch Sites

PatchOverlay scans the program's bundles and, per address operand, classifies it by a `switch` on the patch kind (the operand's `[+48]` byte) and inserts it into per-overlay `absl::flat_hash_set<long>` patch-site sets (the SIMD `crc32` / `vpcmpeqb` hash-set inserts dominating the body). The kinds:

| patch kind | covers | RetCheck on miss | Confidence |
|---|---|---|---|
| 0 / 3 | direct in-overlay bundle address | (operand present) overlay.cc:4709 | CONFIRMED |
| 1 / 2 | cross-overlay target (`target_overlay_number`) | `t.target_overlay_number.has_value()` (4694) | CONFIRMED |
| 4 | non-targeting size patch | `!t.target_overlay_number.has_value()` (4674) | CONFIRMED |
| 5 | HLO-function overlay (`kind() == kHloFunction`, 4681) | `kind() == Kind::kHloFunction` | CONFIRMED |

### The Per-Site Rewrite

The `$_2` lambda performs the actual operand write for one `PatchData`:

```c
// Overlay::PatchOverlay(...)::$_2::operator()(PatchData& patch)  @ 0x1406c3e0
if (patch.overlay_number >= ctx.overlays.size())             // overlay.cc:4740
    RetCheckFail("patch.overlay_number < context.overlays.size()");
// kind 0 (kAddress): recompute the overlay-relative word offset
word = address_util::ConvertOffsetByteToWord(mem_space,
          program[overlay].entry(+248) + patch.offset, target);
unit = target.SharedMemoryToImemDmaUnitWords();              // vtable[+360] = vtable[0x168]
if (word % unit != 0)                                        // overlay.cc:4765
    LogFatal("value % target_.SharedMemoryToImemDmaUnitWords() == 0");
bundle_setter(patch.operand_index, word / unit);             // [setter +264] writes the operand
// kind 1 (kSize): assert pack.GetEntryOverlay() == patch.overlay_number, then patch size
```

For an address patch (`kind 0`), the lambda recomputes `ConvertOffsetByteToWord`, asserts the result is a whole multiple of `SharedMemoryToImemDmaUnitWords()` (the DMA granule, a `Target` vtable slot at `+360` = `0x168`), divides to express the offset in DMA units, and writes it into the bundle operand through the setter at vtable `+264` (`0x108`). For a size patch (`kind 1 / kSize`) it asserts `pack.GetEntryOverlay() == patch.overlay_number` (else *"Attempt to patch the size of packed non-entry HLO function"*, overlay.cc:4757) before writing. After all bundles are patched, `PatchOverlay` sets `ctx[+136] = 1` (`trampolines_patched_`) and, if a continuation delay slot is needed, runs the `IsaEmitterFactory::Create` continuation-tail path (the *"No place for the delay slot"* guard at overlay.cc:4794).

> **NOTE —** PatchOverlay only *rewrites* address operands; it does not change their encoding-id. A patched address operand that was a `SyImm32` (`0x2c`) stays a `SyImm32`; only its value is recomputed to the overlay-relative DMA-unit word offset. The overlay-fetch DMA descriptor that consumes these segments — and its overlay-reserved sflag, itself a `SyImm32` immediate — is documented on [Immediate Slot §EncodeOverlaysForDma](slot-immediate.md#encodeoverlaysfordmas-use-of-an-immediate-slot).

---

## Cross-References

- [Immediate Slot](slot-immediate.md) — the per-gen 16/20-bit immediate-slot ladders and the `ResourceSolver` pool walk that a `SyImm32` (`encoding-id 0x2c`) is finally placed into.
- [MC-Emitter](mc-emitter.md) — `getBinaryCodeForInstr`; how operand values reach the 239-bit MC record, and why V5+ branch/call bits come from the proto-bundle path, not the MC record.
- [239-Bit Record Format](record-format.md) — the MC `APInt` record and the `insertBits` model the immediate operand feeds.
- [InstBits Master DB](instbits-master-db.md) — the `TPUDescs` / operand-type descriptors `canAddImmInternal` reads to find an operand's `OperandType`.
- [Bundle Model](bundle-model-overview.md) — the VLIW issue-word contract the patched address operands and immediate slots live in.
