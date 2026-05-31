# NVPTX Register Classes

This page is the single authoritative reference for the nine NVPTX register classes used throughout cicc v13.0. Register class tables previously duplicated in [Register Allocation](../llvm/register-allocation.md), [Register Coalescing](../llvm/register-coalescing.md), [PTX Emission](../pipeline/emission.md), and [AsmPrinter](../infra/asmprinter.md) are consolidated here. When those pages reference register classes, they should cross-reference this page rather than maintaining inline copies.

| | |
|---|---|
| **Register encoding** | `sub_21583D0` (1.1 KB) |
| **PTX type suffix map** | `sub_2163730` (0.4 KB) |
| **PTX prefix map** | `sub_21638D0` (0.5 KB) |
| **Copy opcode dispatch** | `sub_2162350` (0.7 KB) |
| **Register info init (legacy)** | `sub_2163AB0` / `sub_2149CD0` |
| **Register info init (new PM)** | `sub_30590F0` / `sub_301F0C0` |
| **Register decl emission** | `sub_2158E80` (3.2 KB) |
| **Internal-only class vtable** | `off_4A026E0` |

## The Nine Register Classes

NVPTX defines nine register classes that participate in PTX code generation. Each class is identified at runtime by its vtable pointer, which `sub_2163730` and `sub_21638D0` use as a switch key to produce the PTX type suffix and register prefix respectively. The encoding function `sub_21583D0` maps each class to a 4-bit tag that occupies bits `[31:28]` of the 32-bit encoded register ID.

| Tag | Vtable | Class Name | PTX Type | Prefix | Encoded ID | Width | Description |
|---|---|---|---|---|---|---|---|
| 1 | `off_4A027A0` | Int1Regs | `.pred` | `%p` | `0x10000000` | 1 | Predicate (boolean) |
| 2 | `off_4A02720` | Int16Regs | `.b16` | `%rs` | `0x20000000` | 16 | Short integer |
| 3 | `off_4A025A0` | Int32Regs | `.b32` | `%r` | `0x30000000` | 32 | General-purpose integer |
| 4 | `off_4A024A0` | Int64Regs | `.b64` | `%rd` | `0x40000000` | 64 | Double-width integer |
| 5 | `off_4A02620` | Float32Regs | `.f32` | `%f` | `0x50000000` | 32 | Single-precision float |
| 6 | `off_4A02520` | Float64Regs | `.f64` | `%fd` | `0x60000000` | 64 | Double-precision float |
| 7 | `off_4A02760` | Int16HalfRegs | `.b16` | `%h` | `0x70000000` | 16 | Half-precision float (f16, bf16) |
| 8 | `off_4A026A0` | Int32HalfRegs | `.b32` | `%hh` | `0x80000000` | 32 | Packed pair (v2f16, v2bf16, v2i16, v4i8) |
| 9 | `off_4A02460` | Int128Regs | `.b128` | `%rq` | `0x90000000` | 128 | 128-bit wide (tensor core) |

### Naming Discrepancy

Two naming conventions exist in the codebase, depending on whether the name was recovered from the emission functions or from the register allocator context:

| Vtable | Emission name (sub_2163730/sub_21638D0) | RA-context name (sub_2162350) | Resolution |
|---|---|---|---|
| `off_4A02760` | Int16HalfRegs | Float16Regs | Same class. The emission functions use the TableGen-derived name `Int16HalfRegs`; the RA raw report uses the semantic alias `Float16Regs`. Both refer to `off_4A02760`. |
| `off_4A026A0` | Int32HalfRegs | Float16x2Regs | Same class. `Int32HalfRegs` is the TableGen name; `Float16x2Regs` is the semantic alias. Both refer to `off_4A026A0`. |
| `off_4A02460` | Int128Regs | SpecialRegs | **Different raw reports assigned different names to `off_4A02460`.** The emission report identifies it as `Int128Regs` (based on `.b128` type and `%rq` prefix). The earlier RA sweep report labeled it `SpecialRegs`. The emission-derived name `Int128Regs` is more accurate: `.b128` / `%rq` is used for 128-bit tensor-core values (i128 on SM 70+), not for special/environment registers. |

The tenth vtable `off_4A026E0` is present in the binary but returns `"!Special!"` from both `sub_2163730` and `sub_21638D0`. It is never assigned an encoded ID and never participates in register declaration emission. It is an internal-only sentinel class used within `NVPTXRegisterInfo` initialization (string `"ENVREG10"` at register info offset `+72`).

Throughout this wiki, the **emission-derived names** (Int16HalfRegs, Int32HalfRegs, Int128Regs) are canonical. Pages written before this consolidation may use the RA-context aliases.

## Register Encoding Scheme -- sub_21583D0

Every virtual register in the NVPTX backend is encoded as a 32-bit value that packs the register class and a per-class index into a single integer. The encoding function at `sub_21583D0` (1.1 KB) implements this:

```c
encoded_register = class_tag | (register_index & 0x0FFFFFFF)
```

The bit layout:

```text
 31  28 27                             0
+------+-------------------------------+
| class|       register index          |
| tag  |       (28 bits)               |
+------+-------------------------------+
```

- **Bits [31:28]** -- 4-bit class tag, values `0x1` through `0x9` as listed in the table above.
- **Bits [27:0]** -- 28-bit register index within that class, supporting up to 268 million registers per class.

The function operates in two modes:

1. **Physical register** (register_id >= 0): Returns the raw index directly (low 28 bits). Physical registers on NVPTX are a vestigial concept -- the target has no fixed register file -- but LLVM's infrastructure requires them for reserved registers like `%SP` and `%SPL`.

2. **Virtual register** (register_id < 0, i.e., bit 31 set in LLVM's internal convention): Looks up the register class from the `MachineRegisterInfo` register map, matches the class vtable against the nine known vtable addresses, and returns `class_encoded_id | (register_index & 0x0FFFFFFF)`.

If the vtable does not match any of the nine known classes, the function triggers a fatal error:

```text
"Bad register class"
```

This is a hard abort, not a recoverable diagnostic. It indicates that either a new register class was added without updating the encoding function, or memory corruption produced an invalid vtable pointer.

### Why Bits [31:28] and Not Bits [31:29]

LLVM's standard convention uses bit 31 (`0x80000000`) to distinguish physical from virtual registers internally. The NVPTX encoding reclaims this bit as part of the class tag because after encoding, the distinction between physical and virtual is no longer meaningful -- all registers in emitted PTX are virtual. Tag value `0x8` (Int32HalfRegs) has bit 31 set, which would collide with LLVM's virtual-register marker. This works because the encoding is applied only during emission, after register allocation is complete and the physical/virtual distinction is irrelevant.

## Complete Class Separation

The nine register classes are **completely disjoint**. There is no cross-class interference: an `Int32Regs` register (`%r`) never conflicts with a `Float32Regs` register (`%f`) even though both are 32 bits wide. This is a fundamental consequence of PTX's typed register model. In PTX, `.reg .b32 %r0` and `.reg .f32 %f0` are distinct storage locations from `ptxas`'s perspective. Two implications follow:

1. **No cross-class coalescing.** The register coalescer at `sub_34AF4A0` enforces a same-class check on every coalescing candidate. Cross-class copies (e.g., a bitcast from `i32` to `f32`) must survive as explicit `mov` instructions in the emitted PTX.

2. **Per-class pressure accounting.** The greedy register allocator at `sub_2F5A640` tracks register pressure per class independently. The `-maxreg` limit bounds total live registers across all classes combined, but interference within any single class never spills over to another.

This is unlike CPU targets (x86, AArch64) where integer and floating-point registers can alias through sub-register relationships, or where a single physical register appears in multiple register classes.

## Copy Opcodes -- sub_2162350

The function `sub_2162350` (0.7 KB, `"Copy one register into another with a different width"`) dispatches copy instruction emission based on the source and destination register classes. Each class has two opcodes: one for same-class copies (e.g., `mov.b32 %r1, %r0`) and one for cross-class copies (e.g., bitcasting between `Int32Regs` and `Float32Regs`):

| Class | Same-Class Opcode | Cross-Class Opcode | Notes |
|---|---|---|---|
| Int1Regs | 39424 | 39424 | No distinct cross-class path |
| Int16Regs | 39296 | 39296 | No distinct cross-class path |
| Int32Regs | 39552 | 10816 | Cross = `mov.b32` bitcast to float |
| Int64Regs | 39680 | 11008 | Cross = `mov.b64` bitcast to double |
| Float32Regs | 30656 | 10880 | Cross = `mov.b32` bitcast to integer |
| Float64Regs | 30784 | 11072 | Cross = `mov.b64` bitcast to integer |
| Int16HalfRegs | 30528 | 10688 | Cross = `mov.b16` half-to-short |
| Int32HalfRegs | 39552 | 39552 | Uses same opcode as Int32Regs same-class |
| Int128Regs | 39168 | 39168 | No distinct cross-class path |

Classes where both opcodes are identical (`Int1Regs`, `Int16Regs`, `Int32HalfRegs`, `Int128Regs`) have no meaningful cross-class copy path. For predicates (`Int1Regs`), this is because there is no other 1-bit type. For 128-bit registers, tensor-core values have no peer class to bitcast into. The `Int32HalfRegs` class shares its same-class opcode (39552) with `Int32Regs` because both emit `.b32` copies -- the packed v2f16 value is simply treated as a 32-bit bitpattern for copying.

The five classes with distinct cross-class opcodes (`Int32Regs`, `Int64Regs`, `Float32Regs`, `Float64Regs`, `Int16HalfRegs`) are exactly those that participate in bitcast operations between integer and floating-point interpretations of the same bit width.

## Register Declaration Emission -- sub_2158E80

During function body emission, `sub_2158E80` (3.2 KB) emits `.reg` declarations for every register class used by the function. The process:

1. **Iterate the register map** at `this+800` in the AsmPrinter state.
2. **Deduplicate classes** using a hash table at `this+808..832`.
3. **Track the maximum index** per class across all virtual registers.
4. **Emit one declaration per class** in the format:

```ptx
.reg .pred  %p<5>;       // 5 predicate registers (indices 0..4)
.reg .b16   %rs<12>;     // 12 short integer registers
.reg .b32   %r<47>;      // 47 general-purpose 32-bit
.reg .b64   %rd<8>;      // 8 double-width integer
.reg .f32   %f<20>;      // 20 single-precision float
.reg .f64   %fd<3>;      // 3 double-precision float
.reg .b16   %h<4>;       // 4 half-precision float
.reg .b32   %hh<2>;      // 2 packed-pair registers
.reg .b128  %rq<1>;      // 1 tensor-core 128-bit register
```

The count for each class is `max_register_index + 1`. The PTX declaration syntax `%prefix<N>` declares registers `%prefix0` through `%prefix(N-1)`.

Note that `Int16HalfRegs` and `Int16Regs` share the same PTX type suffix (`.b16`) but have different prefixes (`%h` vs `%rs`). Similarly, `Int32HalfRegs` and `Int32Regs` share `.b32` but use `%hh` vs `%r`. The PTX assembler `ptxas` treats these as completely separate register namespaces -- the prefix, not the type, determines the namespace.

Stack pointer registers (`%SP`, `%SPL`) are emitted before the class declarations when the function has a non-zero local frame. These use `.b64` in 64-bit mode or `.b32` in 32-bit mode.

## Per-Class Detail

### Int1Regs -- Predicates

| Property | Value |
|---|---|
| Vtable | `off_4A027A0` |
| PTX type | `.pred` |
| Prefix | `%p` |
| Tag | `0x1` |
| Width | 1 bit |
| Legal MVTs | `i1` |
| Same-class copy | 39424 |

Predicate registers hold boolean values used for conditional branches (`@%p1 bra target`), select instructions (`selp`), and set-predicate results (`setp`). They are the only 1-bit registers in PTX. There is no cross-class copy path because no other class holds 1-bit values. The coalescer excludes predicates from cross-class analysis entirely.

### Int16Regs -- Short Integers

| Property | Value |
|---|---|
| Vtable | `off_4A02720` |
| PTX type | `.b16` |
| Prefix | `%rs` |
| Tag | `0x2` |
| Width | 16 bits |
| Legal MVTs | `i16` |
| Same-class copy | 39296 |

Short integer registers hold 16-bit integer values. PTX `.param` space widens all scalars below 32 bits to `.b32`, so `%rs` registers appear primarily in computation, not in function signatures. The prefix `%rs` (register-short) distinguishes these from `%h` (Int16HalfRegs) even though both declare as `.b16`.

### Int32Regs -- General-Purpose 32-bit

| Property | Value |
|---|---|
| Vtable | `off_4A025A0` |
| PTX type | `.b32` |
| Prefix | `%r` |
| Tag | `0x3` |
| Width | 32 bits |
| Legal MVTs | `i32` |
| Same-class copy | 39552 |
| Cross-class copy | 10816 |

The workhorse register class. Holds 32-bit integers, addresses in 32-bit mode, loop indices, and general computation results. Cross-class copy opcode 10816 handles bitcast to `Float32Regs` (`%f`).

### Int64Regs -- Double-Width Integer

| Property | Value |
|---|---|
| Vtable | `off_4A024A0` |
| PTX type | `.b64` |
| Prefix | `%rd` |
| Tag | `0x4` |
| Width | 64 bits |
| Legal MVTs | `i64` |
| Same-class copy | 39680 |
| Cross-class copy | 11008 |

Holds 64-bit integers and device pointers in 64-bit mode (the common case). Cross-class copy opcode 11008 handles bitcast to `Float64Regs` (`%fd`).

### Float32Regs -- Single-Precision Float

| Property | Value |
|---|---|
| Vtable | `off_4A02620` |
| PTX type | `.f32` |
| Prefix | `%f` |
| Tag | `0x5` |
| Width | 32 bits |
| Legal MVTs | `f32` |
| Same-class copy | 30656 |
| Cross-class copy | 10880 |

Holds IEEE 754 single-precision floats. Note the `.f32` type suffix rather than `.b32` -- PTX distinguishes float from bitwise register types even at the same width. Cross-class copy opcode 10880 handles bitcast to `Int32Regs` (`%r`).

### Float64Regs -- Double-Precision Float

| Property | Value |
|---|---|
| Vtable | `off_4A02520` |
| PTX type | `.f64` |
| Prefix | `%fd` |
| Tag | `0x6` |
| Width | 64 bits |
| Legal MVTs | `f64` |
| Same-class copy | 30784 |
| Cross-class copy | 11072 |

Holds IEEE 754 double-precision floats. Cross-class copy opcode 11072 handles bitcast to `Int64Regs` (`%rd`).

### Int16HalfRegs -- Half-Precision Float

| Property | Value |
|---|---|
| Vtable | `off_4A02760` |
| PTX type | `.b16` |
| Prefix | `%h` |
| Tag | `0x7` |
| Width | 16 bits |
| Legal MVTs | `f16`, `bf16` |
| Same-class copy | 30528 |
| Cross-class copy | 10688 |

Despite the `Int16` in the TableGen-derived name, this class holds half-precision floating-point values (f16 and bf16). The `.b16` PTX type (bitwise 16-bit) is used rather than a hypothetical `.f16` because PTX's type system uses `.b16` for all 16-bit values that are not short integers. The `%h` prefix distinguishes these registers from `%rs` (Int16Regs). Cross-class copy opcode 10688 handles conversion to `Int16Regs`.

The semantic alias `Float16Regs` appears in some wiki pages and is equally valid.

### Int32HalfRegs -- Packed Half-Precision Pairs

| Property | Value |
|---|---|
| Vtable | `off_4A026A0` |
| PTX type | `.b32` |
| Prefix | `%hh` |
| Tag | `0x8` |
| Width | 32 bits |
| Legal MVTs | `v2f16`, `v2bf16`, `v2i16`, `v4i8` |
| Same-class copy | 39552 |
| Cross-class copy | 39552 |

This is the **only register class for vector types** on NVPTX. It holds exactly 32 bits of packed data: two f16 values, two bf16 values, two i16 values, or four i8 values. The `%hh` prefix distinguishes it from `%r` (Int32Regs). Both same-class and cross-class copy opcodes are 39552 (identical to `Int32Regs` same-class), because copies of packed values are simple 32-bit bitwise moves.

All vector types wider than 32 bits (`v4f32`, `v2f64`, `v8i32`, etc.) are illegal on NVPTX and must be split or scalarized during type legalization. See the [vector legalization](../llvm/selectiondag.md) documentation for the split/scalarize dispatch.

The semantic alias `Float16x2Regs` appears in some wiki pages.

### Int128Regs -- 128-bit Tensor Core Values

| Property | Value |
|---|---|
| Vtable | `off_4A02460` |
| PTX type | `.b128` |
| Prefix | `%rq` |
| Tag | `0x9` |
| Width | 128 bits |
| Legal MVTs | `i128` (SM 70+) |
| Same-class copy | 39168 |
| Cross-class copy | 39168 |

The widest register class, introduced for tensor core operations on Volta (SM 70) and later architectures. Holds 128-bit values used as operands and accumulators in `mma` and `wmma` instructions. The `%rq` prefix stands for "register quad" (4x32 bits). There is no cross-class copy path because no other class holds 128-bit values.

During register coalescing, 128-bit values are tracked as wide register pairs (two 64-bit halves). The coalescer at `sub_3497B40` handles paired-register decomposition: when coalescing the low half, the high half inherits corresponding constraints.

An earlier raw report (`p2c.5-01-register-alloc.txt`) labeled `off_4A02460` as `SpecialRegs`. This was an error in that report's identification. The vtable `off_4A02460` emits `.b128` / `%rq`, which is the 128-bit class for tensor core values, not a class for special/environment registers.

### The Internal-Only Class -- off_4A026E0

| Property | Value |
|---|---|
| Vtable | `off_4A026E0` |
| PTX type | `"!Special!"` |
| Prefix | `"!Special!"` |
| Encoded ID | None |

A tenth vtable address appears in the register info initialization path (`sub_2163AB0`). Both `sub_2163730` and `sub_21638D0` return the sentinel string `"!Special!"` for this vtable. It has no encoded ID, no PTX declaration, and never produces emitted registers. The string `"ENVREG10"` at register info offset `+72` (alongside `"Int1Regs"` at offset `+80`) suggests this class is associated with environment registers -- hardware-defined read-only registers like `%tid`, `%ctaid`, `%ntid`, etc. These are emitted by dedicated special-register emission functions (`sub_21E86B0`, `sub_21E9060`) rather than through the register class encoding path.

## Register Info Initialization

`NVPTXRegisterInfo` objects are created by two factory functions corresponding to the two pass manager generations:

| | Legacy PM | New PM |
|---|---|---|
| **Factory** | `sub_2149CD0` | `sub_301F0C0` |
| **Init** | `sub_2163AB0` | `sub_30590F0` |
| **Object size** | 224 bytes | 248 bytes |

Both call `sub_1F4A910` (`TargetRegisterInfo::InitMCRegisterInfo`) with the register descriptor table at `off_49D26D0` and register unit data at `unk_4327AF0`. Key fields in the initialized structure:

| Offset | Content |
|---|---|
| +44 | `NumRegs` (total register count) |
| +72 | `"ENVREG10"` (environment register class name) |
| +80 | `"Int1Regs"` (first register class name) |
| +96 | `numRegClasses` (initially 1, expanded during init) |

## Coalescing Constraints

The register coalescer imposes these constraints based on register class:

| Class | Coalesceable | Constraint Flag (offset +3, mask 0x10) |
|---|---|---|
| Int1Regs | Same class only | Set |
| Int16Regs | Same class only | Set |
| Int32Regs | Same class only | Set (type code 12) |
| Int64Regs | Same class only | Set (type code 13) |
| Float32Regs | Same class only | Set (type code 15) |
| Float64Regs | Same class only | Set |
| Int16HalfRegs | Same class only | Set |
| Int32HalfRegs | Same class only | Set |
| Int128Regs | Never coalesced | Cleared |

Int128Regs (the class at `off_4A02460`, previously mislabeled `SpecialRegs` in the coalescing page) has its constraint flag cleared, excluding it from the coalescing worklist entirely. This makes sense: tensor-core 128-bit values have specific register-pair relationships that the coalescer must not disturb.

Cross-class copies between `Int32Regs`/`Float32Regs` and between `Int64Regs`/`Float64Regs` are bitcasts that the coalescer never eliminates -- they must survive as explicit PTX `mov` instructions because the source and destination live in different register namespaces.

## Differences from Upstream LLVM NVPTX

The upstream LLVM NVPTX backend (as of LLVM 20.0.0) defines these register classes in `NVPTXRegisterInfo.td`:

- `Int1Regs`, `Int16Regs`, `Int32Regs`, `Int64Regs` -- identical.
- `Float16Regs`, `Float16x2Regs` -- upstream names for cicc's `Int16HalfRegs` / `Int32HalfRegs`. The rename reflects NVIDIA's preference for the TableGen-derived integer-typed names.
- `Float32Regs`, `Float64Regs` -- identical.
- `Int128Regs` -- present in upstream, matches cicc.
- No `SpecialRegs` class in upstream. Special registers are handled through dedicated physical registers, not a register class.
- No `off_4A026E0` internal-only class in upstream.

The encoding scheme (4-bit tag in `[31:28]`, 28-bit index in `[27:0]`) and the fatal `"Bad register class"` error path are NVIDIA additions not present in upstream LLVM's NVPTX backend, which relies on standard `MCRegisterInfo` encoding.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| Register class encoding (class tag OR index) | `sub_21583D0` | 1.1 KB | -- |
| Register class -> PTX type suffix (`.pred`, `.b32`, `.f32`, ...) | `sub_2163730` | 0.4 KB | -- |
| Register class -> PTX prefix (`%p`, `%r`, `%f`, ...) | `sub_21638D0` | 0.5 KB | -- |
| Copy opcode dispatch by register class | `sub_2162350` | 0.7 KB | -- |
| Stack frame + register declaration emission | `sub_2158E80` | 3.2 KB | -- |
| NVPTXRegisterInfo init (legacy PM) | `sub_2163AB0` | 0.3 KB | -- |
| NVPTXRegisterInfo factory (legacy PM) | `sub_2149CD0` | -- | -- |
| NVPTXRegisterInfo init (new PM) | `sub_30590F0` | -- | -- |
| NVPTXRegisterInfo factory (new PM) | `sub_301F0C0` | -- | -- |
| TargetRegisterInfo::InitMCRegisterInfo | `sub_1F4A910` | -- | -- |
| Special register emission (%tid, %ctaid, %ntid, %nctaid) | `sub_21E86B0` | -- | -- |
| Cluster register emission (SM 90+) | `sub_21E9060` | -- | -- |

## Cross-References

- [Register Allocation](../llvm/register-allocation.md) -- greedy RA that operates on these classes; pressure tracking and `-maxreg` constraint
- [Register Coalescing](../llvm/register-coalescing.md) -- same-class-only coalescing policy, copy opcode classification
- [PTX Emission](../pipeline/emission.md) -- function header orchestrator that calls the register declaration emitter
- [AsmPrinter](../infra/asmprinter.md) -- per-instruction emission that calls the encoding function
- [Type Legalization](../llvm/selectiondag.md) -- vector type legalization driven by the Int32HalfRegs-only vector model
- [NVPTX Target Infrastructure](../infra/nvptx-target.md) -- NVPTXTargetMachine that owns the register info objects
