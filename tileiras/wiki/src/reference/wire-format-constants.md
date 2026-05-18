# Wire-Format Constants

A reimplementation of `tileiras` that aims for byte-for-byte parity with a shipped binary
must reproduce a small set of magic numbers, tag namespaces, opcode tables, and obfuscation
ciphers exactly. Every constant in this page is fingerprintable from a stripped 88 MB
`tileiras` ELF and verified against the cross-referencing dispatchers documented in
[MLIR Bytecode Format](../bytecode/mlir-bc-format.md),
[LLVM Fingerprint Table](llvm-fingerprint-table.md), and
[ISelDAG and MatcherTable](../codegen/iseldag-and-matchertable.md). The constants are
*not* configuration. Changing any one of them produces an artifact that fails to
interoperate with the shipped reader or fails to bind against the AsmWriter's
post-decryption string pool.

The page is organized strictly by layer of the wire format, walking from the outermost
envelope down to the innermost emitter. Each section lists the constants the layer
defines, the exact byte offsets and lengths where they live in the binary, and the
authoritative cross-reference for the dispatch site that reads them. Where a constant
is interesting on its own — a typo preserved across builds, an unused mid-slot in a
bit-mask, a numbering divergence from upstream — the rationale is captured inline
rather than buried in a footnote.

## Layer 1 — TileIR Bytecode Envelope

The bytecode container's framing prefix is the single most reproduced constant in
this binary. Stock LLVM 18-21 MLIR-bytecode files share the first three bytes; the
private TileIR dialect tag occupies bytes 3-7 and the trailing terminator byte at
offset 7 separates TileIR from upstream MLIR at the magic-byte level.

| Offset | Byte | Symbolic name | Meaning |
|---:|---|---|---|
| 0 | `0x06` | `MAGIC_LEN_HI` | MLIR-bytecode framing prefix (shared with upstream) |
| 1 | `0x03` | `MAGIC_LEN_LO` | MLIR-bytecode framing prefix (shared with upstream) |
| 2 | `0x80` | `MAGIC_FLAGS` | MLIR-bytecode framing prefix (shared with upstream) |
| 3 | `0x54` | dialect byte 1 | `'T'` |
| 4 | `0x69` | dialect byte 2 | `'i'` |
| 5 | `0x6C` | dialect byte 3 | `'l'` |
| 6 | `0x65` | dialect byte 4 | `'e'` |
| 7 | `0x00` | tileiras terminator | Upstream writes `'\n'` (start of `"\nMLIR"`) here |

The literal lives at rodata `0x45EBF08` and is compared byte-for-byte by `sub_5838A0`
against the input buffer; mismatch surfaces a three-fragment diagnostic
(`"invalid magic number at position "` / `", got "` / `" expected "`).

The version block follows immediately after the magic and is a sequence of three
unsigned-LEB128 VarInts: `major`, `minor`, optional `patch`. The accepted range
table at rodata `0x45EBF10` is verbatim:

```c
static const TileVersion supported_versions[] = {
    /*min:*/ { .major = 13, .minor = 1, .patch = 0          }, // inclusive
    /*max:*/ { .major = 13, .minor = 1, .patch = UINT32_MAX }, // inclusive (only 13.1.x)
};
```

Any major or minor other than `13.1` is rejected; the patch field is read for forward
compatibility but never gated on.

The section ID space is dense in `[0x00, 0x06]` and the `0x00` slot doubles as the
end-of-bytecode marker:

| ID | Section | Required | Reference width |
|---:|---|:---:|---|
| `0x00` | EndOfBytecode | required (last) | none |
| `0x01` | String | required | `u32` offsets |
| `0x02` | Func | required | sequential |
| `0x03` | Debug | optional | `u32` and `u64` offsets |
| `0x04` | Constant | optional | `u64` offsets |
| `0x05` | Type | optional | `u32` offsets |
| `0x06` | Global | optional | sequential |

Section header padding is `0xCF`. The on-disk section order is the producer's
choice, but the walker order is fixed: STRING → TYPE → CONSTANT → IR → optional
RESOURCE/DEBUG. See [MLIR Bytecode Format](../bytecode/mlir-bc-format.md#section-walker-algorithm)
for the dependency-ordered dispatch.

> ⚡ **QUIRK — terminator byte 7 is the file-format split**
> A bytecode container with the first seven bytes identical to upstream MLIR and byte
> 7 set to `0x00` is TileIR; a container with byte 7 set to `'\n'` (`0x0A`) and bytes
> 8-11 spelling `"MLIR"` is upstream MLIR. The two file formats share enough framing
> that a magic-number sniff that only checks bytes 0-2 will mis-classify both as
> "some MLIR bytecode dialect." A reimplementation that wants to refuse upstream MLIR
> inputs early must compare all eight bytes — anything less lets stock MLIR
> bytecode bind to the TileIR header parser and produce mangled tag-table errors
> several sections in.

## Layer 2 — TypeTag Namespace (`sub_59C710`)

The Type section's per-record tag is a one-byte slot at offset 0 of the payload,
followed by a tag-specific operand list. The dense numbering `0..18` is independent
of upstream MLIR's `BytecodeTypeOpcodes.td`:

| Tag | Type | Operands (VarInt count) |
|---:|---|---:|
| `0..4` | `i1`, `i8`, `i16`, `i32`, `i64` | 0 |
| `5..11` | `f16`, `bf16`, `f32`, `tf32`, `f64`, `f8E4M3FN`, `f8E5M2` | 0 |
| `12` | Pointer (element type) | 1 |
| `13` | Tile (element + i64 shape) | 2 + dim_count |
| `14` | TensorView (element + shape + strides) | 3 + dim_count + stride_count |
| `15` | PartitionView (element + shape + dim-map + mode byte) | 4 + dim_count + map_count |
| `16` | Function (input list + result list) | 2 + input_count + result_count |
| `17` | Token | 0 |
| `18` | `f8E8M0FNU` (extension) | 0 |

The trailing `f8E8M0FNU` extension is an element type — like tags `5..11` it carries
no payload of its own. Tag 18 is reachable only as a leaf inside a tile-family
aggregate (`TileType`, `TensorViewType`, `PartitionViewType`), so the operand-zero
contract holds whether the tag is decoded standalone or through one of the
aggregate-type arms.

## Layer 3 — AttrTag Numbering (`sub_59F100`)

The most consequential single constant table in the file. The shipped tileiras
AttrTag numbering is **wire-format-breaking** versus upstream MLIR's
`mlir/Bytecode/BytecodeEnums.h::AttributeTag`. Both tables are reproduced side by
side so the divergence is unambiguous:

| AttrTag | Upstream MLIR | Tileiras `sub_59F100` |
|---:|---|---|
| 0 | (reserved / sentinel) | (default-arm; emits `"unsupported AttributeTag"`) |
| 1 | `IntegerAttr` | `StringAttr` |
| 2 | `FloatAttr` | `FloatAttr` |
| 3 | `BoolAttr` | `TypeAttr` |
| 4 | `TypeAttr` | `DenseElementsAttr` (int/float) |
| 5 | `StringAttr` | `DenseElementsAttr` (string) |
| 6 | `ArrayAttr` | `DivByAttr` |
| 7 | `DenseElements` | `DenseI64ArrayAttr` (variant A) |
| 8 | `DivByAttr` | `DenseI64ArrayAttr` (variant B) |
| 9 | `SameElementsAttr` | `SameElementsAttr` |
| 10 | `Dictionary` | `BoundedAttr` (variant 0) |
| 11 | `OptimizationHints` | `BoundedAttr` (variant 1) |
| 12 | `BoundedAttr` | `BoundedAttr` (variant 2) |
| 13 | (no upstream slot) | `AssumePredicateAttr` |

Only tag `2` (`FloatAttr`) matches upstream by coincidence. Every other tag in the
`1..13` range disagrees: tag `1` is `StringAttr` here versus upstream `IntegerAttr`;
tag 4 lands on `DenseElementsAttr` instead of `TypeAttr`; tag 5 lands on
`DenseElementsAttr<string>` instead of `StringAttr`; tag 6 lands on `DivByAttr`
instead of `ArrayAttr`. Going the other direction, an `AssumePredicateAttr` emitted
by tileiras at tag 13 has no destination in upstream's table at all. Any external
tool that needs to round-trip MLIR bytecode through both implementations must
freeze the tileiras numbering above; the upstream header is reserved for future
stock cuda_tile builds.

The parallel DebugTag namespace at `sub_589B90` is private to the Debug section and
uses a dense `[0..6]` range. Tag `0` is the failure sentinel; tags `1-6` cover
`DICompileUnit`, `DIFile`, `DILexicalBlock`, `DILoc`, `DISubprogram`,
`CallSite` respectively. No upstream LLVM debug-info tag table participates in
this dispatcher.

## Layer 4 — cuda_tile Opcode Space (`sub_5B13D0`)

The 110-row `cuda_tile` opcode table is dense in `[0..109]` with two reserved
holes the dispatcher leaves on the default arm:

| Range | Status |
|---|---|
| `0..24` | Assigned (`absf` through `exp2`) |
| `25..36` | Reserved hole — emits `"unknown or unimplemented opcode: "` |
| `37..51` | Assigned (`exti` through `int_to_ptr`) |
| `52..57` | Reserved hole — emits `"unknown or unimplemented opcode: "` |
| `58..109` | Assigned (`iota` through `yield`) |

Opcode `0x6E` (`atan2` in the public 13.2 namespace) is **absent** from this binary.
The dispatcher has no case for it and embeds no `cuda_tile.atan2` mnemonic string;
encoding the op lands on the default arm. This places the binary at a 13.1-vintage
opcode-table snapshot.

The full per-opcode mnemonic / handler-address table lives in
[MLIR Bytecode Format — Operation Opcode Dispatch](../bytecode/mlir-bc-format.md#operation-opcode-dispatch).

The location-index slot is signed zig-zag LEB128: the value `0x7F` after zig-zag
decode is `-1`, which the dispatcher resolves to `UnknownLoc` (typical of a
`--lineinfo`-less compile).

## Layer 5 — NVPTX MatcherTable Pools (XOR-3 Cipher)

The NVPTX AsmWriter ships two `.data` mnemonic pools obfuscated by a walking
XOR cipher. Byte `i` is XORed with `(3 * i) mod 256`, decoded once at startup,
and cached behind a pointer at `qword_5B4F4D0`.

```c
void xor3_decode(uint8_t *begin, uint8_t *end) {
    uint8_t key = 0;
    for (uint8_t *p = begin; p != end; ++p) {
        *p ^= key;
        key = (uint8_t)(key + 3);
    }
}
```

The two pools and their decoder entry points are:

| Pool | Range | Length | Decoder | Cached pointer |
|---|---|---:|---|---|
| Opcode mnemonic | `0x5A4C080 .. 0x5A656F0` | ~105 KB | `sub_1BD1810` | `qword_5B4F4D0` |
| Physical-register-name | `0x5A4BE20 .. 0x5A4C06A` | 586 B | `sub_1BD1830` | (post-decode cached) |

The cipher is not a security boundary. Its only effect is to prevent a naive
`strings(1)` sweep from surfacing every PTX mnemonic. A reimplementation that
does not need binary-for-binary `.data` parity can store the same strings plainly.

The shape of the decoded pool matches LLVM 21's `NVPTXGenAsmWriter` output; the
pattern-name strings paired with each `OPC_*` row of the MatcherTable
(`"setmaxregister"`, `"cp.async.bulk.tensor.group.shared.cluster"`,
`"wgmma.mma_async.sync.aligned"`, `"wgmma.fence.sync.aligned"`,
`"tcgen05.mma.sync"`, `"tcgen05.mma.ws.sync"`, `"mma.block_scaled.sync.aligned"`,
`"mma.sp.sync.aligned.m8n8k16"`) sit unencrypted in `.rodata` since they are
TableGen pattern records rather than printer-side mnemonic literals.

## Layer 6 — NVPTX ProxyReg Whitelist

The post-ISel `NVPTXProxyRegErasure` peephole uses a **contiguous opcode range**
rather than a named whitelist. The TableGen-side consolidation that landed
in LLVM 21 trunk just before the 21.0 cut replaced the older per-type
`ProxyRegInst<*>` template with a four-way emit that produces adjacent
indices:

| MI opcode | Type class | TableGen name |
|---:|---|---|
| 3156 | i16 | `ProxyRegI16` |
| 3157 | i32 | `ProxyRegI32` |
| 3158 | i64 | `ProxyRegI64` |
| 3159 | f32 / f64 | `ProxyRegF` |

The check at `0x1AE5086` is `sub eax, 0xC54 ; cmp eax, 3` — a contiguous range
test that costs two x86 instructions. Stock LLVM 18 used a 5-6-element named
whitelist, so the contiguous numbering is itself a fingerprint for the LLVM 21
NVPTX backend. Reimplementations cannot pick arbitrary opcode numbers for the
typed ProxyReg family without breaking the peephole's hot-path test.

## Layer 7 — FTZ-Path Constants in `SelectIntrinsic_W_Chain` Case `0x66`

The per-call FTZ override in case `0x66` of `sub_1A854E0` carries two MI opcode
literals and one SDNode flag bit that must reproduce exactly:

| Constant | Value | Meaning |
|---|---|---|
| FTZ-path FMA opcode | `0x65` | `FMA_FTZ`; emitted when probe selects FTZ |
| Non-FTZ-path wrapper opcode | `0xF7` | `FMA_NON_FTZ`; emitted when probe selects IEEE |
| FTZ-authorization flag bit | `0x40` | `NoFPExcept` reinterpreted as per-node FTZ-authorize signal |
| Inner FMAD opcode | `0x63` | Set with `NoFPExcept` (`0x200`) on the FTZ four-instruction chain |
| `INST_WRAPPER` opcode (non-FTZ) | `0xD2` | Holds chain through `ADDRESSOF` wrap |
| `CopyToReg` opcode | `0x11` | Standard LLVM SDNode opcode |
| MUL_ADD_f32 / MUL_ADD_f64 | 207 / 208 | MVT-keyed select after the wrapper chain |

> ⚡ **QUIRK — `NoFPExcept` flag bit `0x40` repurposed as FTZ-authorization**
> Upstream LLVM treats SDNode flag bit `0x40` (`NoFPExcept`) as a pure FP-exception
> -safety advisory: it tells later passes that no FP exception can be raised. In
> case `0x66` of `sub_1A854E0`, tileiras reads the same bit *before* the
> `"unsafe-fp-math"` function attribute and treats it as a per-node "authorize FTZ
> substitution" signal. A combine that legitimately sets `NoFPExcept` on a single
> FMA in an otherwise IEEE-denormal function therefore silently switches that one
> FMA to `fma.rn.ftz.f32` (opcode `0x65`) instead of the `FMA_NON_FTZ` wrapper
> (`0xF7`). A reimplementation that imports upstream flag semantics will produce
> different PTX for the same SDAG.

## Layer 8 — cvt_packfloat Validator Constants (`sub_1A84900`)

The four-gate `cvt_packfloat` validator carries five subtarget-level constants:

| Constant | Value | Gate |
|---|---|---|
| SM major floor | `0x384` (sm_90) | Gate 1 |
| PTX version floor | `0x4D` (PTX 7.7) | Gate 1 |
| sm_100a SM major | `0xA0` | Gates 2 and 3 (UE8M0x2, fp6x2/fp4x2) |
| sm_100f SM minor | `0xF` | Gate 4 (family-conditional) |
| `tmem` feature byte | offset `80` in subtarget feature array at `unk_5BEBD51` | tcgen05 128-bit atomic guard at `sub_1A80A40` |

> ⚡ **QUIRK — `atleast` typo and mismatched PTX number in gate-one diagnostic**
> Gate one's diagnostic string is `"cvt_packfloat intrinsic needs atleast SM90 and PTX >= 78"`:
> the missing space in `atleast` is preserved byte-for-byte, and the message
> advertises `PTX >= 78` even though the actual compare is against `0x4D`
> (PTX 7.7, not 7.8). The discrepancy stems from an internal NVIDIA test-suite
> log scraper that keys on the verbatim string. A reimplementer who "fixes"
> either the spelling or the number desyncs that scraper without changing
> behavior.

## Layer 9 — LLVM 21 NVPTX Data-Layout Stamp

Every NVPTX module emitted by tileiras carries one verbatim data-layout
string, unconditionally stamped before bitcode serialization:

```text
e-p:64:64:64-p3:32:32:32-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-
i128:128:128-f32:32:32-f64:64:64-v16:16:16-v32:32:32-v64:64:64-
v128:128:128-n16:32:64
```

Length: 154 bytes (`0x9A`). Rodata location: `0x4D079D0`. Sole xref:
`sub_1A4E5C0` at `0x1A4E5D1`. Address space 3 (`p3:32:32:32`) marks
NVPTX shared memory as 32-bit-pointer. The string is byte-identical to
stock LLVM 21 NVPTX64, and is one of the ten independent fingerprints
that pin the LLVM base version in [LLVM Fingerprint Table](llvm-fingerprint-table.md#1-nvptx-data-layout-stamp).

## Layer 10 — LLVM Bitcode Producer Strings

Two `.rodata` strings stamp the LLVM base version into every emitted module:

| Slot | Rodata address | Length | Verbatim string |
|---|---|---:|---|
| `IDENTIFICATION_CODE_STRING` | `0x4F882C4` | 13 B | `LLVM21.0.0git` |
| NVPTX AsmPrinter `emitHeader` line 3 | (inside `sub_1A56540`) | varies | `Based on LLVM 21.0.0git` |
| libNVVM module name (when libNVVM path is taken) | (compile-time literal) | 10 B | `mlir-input` |

The producer string is emitted as the bitcode-writer's `IDENTIFICATION` subblock
record at `sub_3935490` (the `EnterSubblock(IDENTIFICATION, 5)` site). The
AsmPrinter header comment block is written at every PTX-emit invocation; the
third line of four is the verbatim `Based on LLVM 21.0.0git` literal, not a
runtime-formatted template.

## Cross-Layer Constant Index

For a reimplementation walking the wire format top-down, the constants converge
on a small handful of source-of-truth dispatchers. The index below maps each
constant back to the page that documents its dispatch site at reimplementation depth.

| Layer | Constant class | Authority page |
|---|---|---|
| 1 | Magic bytes, version range, section IDs | [MLIR Bytecode Format](../bytecode/mlir-bc-format.md#header-parser-sub_5838a0) |
| 2 | TypeTag `0..18` | [MLIR Bytecode Format — Type Tag Dispatch](../bytecode/mlir-bc-format.md#type-tag-dispatch) |
| 3 | AttrTag `0..13`, DebugTag `0..6` | [MLIR Bytecode Format — Self-Contained Attribute Dispatch](../bytecode/mlir-bc-format.md#self-contained-attribute-dispatch) |
| 4 | cuda_tile opcodes `0..109`, reserved holes | [MLIR Bytecode Format — Operation Opcode Dispatch](../bytecode/mlir-bc-format.md#operation-opcode-dispatch) |
| 5 | XOR-3 cipher, pool ranges | [ISelDAG and MatcherTable — AsmWriter String Tables](../codegen/iseldag-and-matchertable.md#asmwriter-string-tables) |
| 6 | ProxyReg whitelist `[3156, 3159]` | [LLVM Fingerprint Table — Fingerprint 8](llvm-fingerprint-table.md#8-nvptxproxyregerasure-4-opcode-contiguous-whitelist) |
| 7 | FMA opcodes `0x65` / `0xF7`, flag bit `0x40` | [ISelDAG and MatcherTable — NVIDIA-Specific ISel Patches](../codegen/iseldag-and-matchertable.md#nvidia-specific-isel-patches) |
| 8 | cvt_packfloat SM/PTX floors, `tmem` feature byte | [ISelDAG and MatcherTable — NVIDIA-Specific ISel Patches](../codegen/iseldag-and-matchertable.md#nvidia-specific-isel-patches) |
| 9 | NVPTX64 data-layout string | [LLVM Fingerprint Table — Fingerprint 1](llvm-fingerprint-table.md#1-nvptx-data-layout-stamp) |
| 10 | `LLVM21.0.0git`, `Based on LLVM 21.0.0git`, `mlir-input` | [LLVM Fingerprint Table — Fingerprints 2, 3](llvm-fingerprint-table.md#2-llvm21-0-0git-bitcode-producer-string) |

## Reimplementation Contract

Three rules summarize the constraint these constants impose on a clean-room
reimplementation:

1. **Magic, AttrTag numbering, and cuda_tile opcode table** are wire-format
   invariants. A reimplementation that picks any other byte for offset 7,
   any other tag-to-attribute-kind mapping in `sub_59F100`'s switch, or any
   other opcode-to-mnemonic assignment in `sub_5B13D0`'s switch produces
   bytecode that the shipped reader either rejects or silently mis-decodes.
2. **NVPTX MatcherTable pool ranges, ProxyReg numbering, and FMA opcode
   numbers** are emitter invariants. A reimplementation that ships different
   bytes here still produces *valid* PTX, but the binary-for-binary
   `.data` and MIR cross-checks NVIDIA's internal regression suite runs
   against tileiras output will fail.
3. **All diagnostic strings — including the `atleast` typo, the `PTX >= 78`
   off-by-one, the `FileLineColLoc` debug-attr naming inheritance — are
   contract surface.** Test-suite log scrapers key on verbatim spelling.
   "Fixing" any of them is a behavioral change as far as downstream tools
   are concerned, even though the fix is locally correct.

The shared property across all three rules is that no constant in this page is
configuration. Each is either a header-stamped invariant frozen at build time, a
table TableGen emitted into the binary at LLVM 21 cut-time, or a literal NVIDIA
chose for hand-rolled validator code. A reimplementation that wants compatibility
must freeze every one of them.
