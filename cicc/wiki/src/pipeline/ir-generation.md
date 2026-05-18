# NVVM IR Generation

Between the EDG 6.6 frontend and the LLVM optimizer sits a layer that has no upstream LLVM equivalent: the NVVM IR generation subsystem. Its job is to translate the EDG intermediate language (IL) tree -- a C-level AST produced by EDG's source-to-source backend -- into LLVM IR suitable for the NVPTX target. This is cicc's equivalent of Clang's `CodeGen` library (`lib/CodeGen/CGExpr.cpp`, `CGStmt.cpp`, `CGDecl.cpp`, etc.), but it operates on EDG's proprietary IL node format rather than a Clang AST. Understanding this layer is essential because it determines every structural property of the LLVM IR that the optimizer and backend will see: address space annotations on pointers, alloca placement conventions, kernel metadata encoding, and the specific IR patterns used for CUDA-specific constructs like `threadIdx.x` or `__shared__` memory.

The EDG frontend does not produce LLVM IR directly. Its backend mode (`BACK_END_IS_C_GEN_BE = 1`) emits transformed C code into `.int.c`, `.device.c`, and `.stub.c` files. A second compilation pass then parses these files back through EDG to produce an IL tree -- a typed, linked representation of every declaration, statement, and expression in the translation unit. The IR generation layer walks this IL tree recursively, creating LLVM `BasicBlock`s, `Instruction`s, and `GlobalVariable`s via a hand-rolled IR builder that directly manipulates LLVM's in-memory data structures. The result is a complete LLVM `Module` containing one function per device-side function definition, with kernel entry points annotated via `nvvm.annotations` metadata.

## Dual-Path Architecture

One of the most distinctive features of cicc's IR generation is that **two complete copies exist within the binary**. This mirrors the [dual-path design](./entry.md) observed throughout cicc: Path A (LibNVVM API mode, `0x90xxxx`) and Path B (standalone mode, `0x126xxxx`).

| Component | Path A (LibNVVM) | Path B (Standalone) |
|---|---|---|
| **Expression codegen** | `0x91xxxx`--`0x94xxxx` | `0x127xxxx`--`0x12Bxxxx` |
| **Expression master dispatch** | `sub_91DF90` | `sub_128D0F0` |
| **Statement dispatch** | `sub_9363D0` | (parallel at similar offset) |
| **Function entry block setup** | `sub_946060` | (parallel) |
| **Function prolog / parameter lowering** | `sub_938240` | (parallel) |
| **Builtin lowering mega-switch** | `sub_90AEE0` (33 KB native) | `sub_12B3FD0` (22 KB native) |
| **Bitfield load/store** | `sub_923780` / `sub_925930` | `sub_1282050` / `sub_1284570` |
| **Special variable codegen** | `sub_920430` / `sub_922290` | `sub_127F7A0` / `sub_1285550` |
| **Inline asm codegen** | `sub_932270` | `sub_1292420` |
| **Global variable codegen** | `sub_916430` | (parallel) |
| **Type translation** | `sub_91AED0` | (parallel) |
| **Kernel metadata emitter** | `sub_93AE30` | (parallel) |

These are not shared-library variations or template instantiations across different types. They are structurally identical copies of the same algorithms with the same string constants (e.g., `"allocapt"`, `"agg.result"`, `"entry"`, `"return"`, `".addr"`) and the same error messages (e.g., `"unsupported expression!"`, `"Argument mismatch in generation function prolog!"`). The two copies use different calling conventions for their codegen context objects -- Path A passes codegen state through a flat struct with LLVM API vtable pointers, while Path B uses a pointer-to-pointer indirection scheme -- but the algorithmic logic and IR output are byte-for-byte identical.

The remainder of this page uses Path B addresses (the `0x12xxxxx` range) as the primary reference because they correspond to the standalone compilation path that `nvcc` invokes, and because the B-series analysis reports provide the most detailed coverage of this path. Every function described here has a direct counterpart in Path A at the corresponding `0x9xxxxx` address.

## Address Map

| Address Range | Subsystem | Key Functions |
|---|---|---|
| `0x126A000`--`0x126BFFF` | Volatile detection, alignment queries | `sub_126A420` (volatile-address predicate) |
| `0x1273000`--`0x1275FFF` | Function attribute emission | `sub_12735D0` (attribute-bundle emitter), `sub_1273F90` (attribute-bundle reader) |
| `0x127A000`--`0x127CFFF` | Type translation helpers | `sub_127A030` (EDG-to-LLVM type lookup), `sub_127B390` (SM-version query), `sub_127B420` (address-of predicate), `sub_127B550` (fatal diagnostic) |
| `0x127D000`--`0x127FFFF` | Constants, alloca creation, bool emission | `sub_127D8B0` (constant-expression emitter), `sub_127FC40` (alloca creator), `sub_127FEC0` (bool-conversion helper) |
| `0x1280000`--`0x1285FFF` | Bitfield access, member loads, inline asm | `sub_1282050` (bitfield store), `sub_1284570` (bitfield load), `sub_1285290` (asm-call emitter) |
| `0x1286000`--`0x128FFFF` | L-value codegen, binary ops, expression dispatch | `sub_1286D80` (address-of L-value), `sub_1287ED0` (lvalue-load wrapper — see [caller graph](irgen-expressions.md#caller-graph-how-sub_1287ed0-emitlvalueload-is-reached)), `sub_128A450` (cast/conversion), `sub_128D0F0` (expression master dispatch), `sub_128F9F0` (binary arithmetic and bitwise), `sub_128F580` (compare predicates) |
| `0x1290000`--`0x129AFFF` | Control flow helpers, inline asm, printf lowering | `sub_1290AF0` (insertion-point setter), `sub_1292420` (inline-asm emitter), `sub_12992B0` (printf-to-vprintf lowering) |
| `0x129B000`--`0x12AFFFF` | Builtin helpers, atomic ops, surface/texture ops | `sub_12A4D50` (basic-block creator), `sub_12A7DA0` (atomic ops), `sub_12ADE80` (surface/texture) |
| `0x12B0000`--`0x12BFFFF` | Builtin mega-switch | `sub_12B3FD0` (builtin lowering, 103KB, 770 IDs) |

## The IRGenState Object

Every codegen function receives a context object -- called `IRGenState` or `CodeGenState` in this wiki -- that carries all mutable state for the current function being compiled. Two distinct layouts exist depending on whether the context is accessed through the Path A flat struct or the Path B double-indirection pattern. Both layouts carry the same logical fields; the difference is structural.

### Path B Layout (pointer-to-pointer pattern)

In Path B, the primary codegen context `a1` is a `CodeGenState**` -- a pointer to a pointer. The outer pointer dereferences to a struct containing the core IR builder state, and sibling pointers at `a1[1]`, `a1[2]`, etc., reach related context objects:

| Access | Offset | Field | Purpose |
|---|---|---|---|
| `*a1` | +0 | IRBuilder state | Current function, insert point, module |
| `a1[1]` | +8 | Insertion context | `[0]` = debug location, `[1]` = current BB, `[2]` = insertion sentinel |
| `a1[2]` | +16 | LLVM context/module | Module handle, LLVMContext |
| `a1[4]` | +32 | Module pointer | LLVM Module* |
| `a1[5]` | +40 | Type context | Type table for `GetLLVMType`, `getIntNTy` |
| `a1[6]` | +48 | Debug location | Current `DebugLoc` to attach to new instructions |
| `a1[7]` | +56 | Current BasicBlock | BB for instruction insertion |
| `a1[8]` | +64 | Insertion point | Iterator into BB's instruction list |
| `a1[9]` | +72 | Address space context | For alloca type creation |
| `a1[19]` | +152 | Cached printf alloca | Reused `"tmp"` alloca for vprintf buffer packing |

### Path A Layout (flat struct, offsets from `a1`)

| Offset | Field | Purpose |
|---|---|---|
| +32 | Module pointer | LLVM Module* |
| +40 | IR builder | Current builder state |
| +48, +56 | Operand pair array | Base and count for metadata pairs |
| +96 | Current BasicBlock | Active BB |
| +104 | Insertion point | Iterator |
| +128 | Instruction creation vtable | Virtual dispatch for instruction emission |
| +136 | Emitter context | Vtable at `[0]`, dispatch at `vtable[2]` |
| +192 | Current Function | LLVM `Function*` being populated |
| +200 | Return BB | The `"return"` basic block |
| +208 | Return value alloca | `"retval"` alloca or sret pointer |
| +240 | Has-cleanups flag | Nonzero when C++ destructors are pending |
| +344 | Module (kernel metadata) | Used by `sub_93AE30` |
| +360/376 | In-kernel flag | Bit 0 set when compiling a `__global__` function |
| +424 | Cleanup stack | Stack of pending destructor frames (24 bytes each) |
| +456 | Allocapt marker | The `"allocapt"` sentinel instruction |

The `"allocapt"` marker deserves special attention. When the function entry-block setup routine (`sub_946060`) creates the entry block, it inserts a dummy `bitcast void to void` instruction named `"allocapt"` as a sentinel. All subsequent alloca instructions created by `CreateTmpAlloca` (`sub_921D70` / `sub_127FC40`) are inserted **before** this sentinel, ensuring that every alloca ends up clustered at the top of the entry block. This is a hard requirement for LLVM's `mem2reg` pass to promote stack slots to SSA registers. The allocapt marker is removed by a later cleanup pass.

## EDG IL Node Layout

Every codegen function traverses EDG IL nodes -- linked structures that represent declarations, statements, and expressions from the parsed CUDA source. The node layout is consistent across all codegen paths:

**Expression node** (passed as `a2` to the expression master dispatch):

| Offset | Field | Description |
|---|---|---|
| +0 | Type pointer | EDG type node (dereference for type info) |
| +18 | Qualifier word | 16-bit: bits 0--14 = qualifier ID, bit 15 = negation |
| +24 | Kind byte | Top-level expression category (1=operation, 2=literal, 3=member, 0x11=call, 0x14=decl-ref) |
| +25 | Flags byte | Bit 2 = assignment context (write-only) |
| +36 | Source location | Passed to debug info attachment |
| +56 | Sub-opcode / data | For kind=1: operator sub-opcode; for kind=2: literal data |
| +72 | Child/operand | Pointer to first child expression |

**Type node** (accessed via expression's type pointer):

| Offset | Field | Description |
|---|---|---|
| +8 | Type classification byte | 1--6 = float types, 11 = integer, 15 = pointer, 16 = vector |
| +128 | Byte size | Element count for arrays, byte size for scalars |
| +136 | Element size | Size in bits for non-typedef types |
| +140 | Type tag | 1=void, 8--11=aggregate (struct/union/class/array), 12=typedef alias, 16=__int128 |
| +144 | Flags | Bit 2 = is_bitfield, bit 3 = signed |
| +160 | Inner type / next | Followed when tag==12 (typedef stripping) |
| +176 | Element count | For array types |

The typedef-stripping idiom appears throughout every codegen function (15+ occurrences in the expression master dispatch alone):

```c
for (t = *expr_type; *(BYTE*)(t + 140) == 12; t = *(QWORD*)(t + 160));
```

This walks through chains of typedef aliases (kind 12) until it reaches the canonical type.

## Function Emission Pipeline

When cicc processes a device-side function, IR generation proceeds through a fixed sequence of stages. The entry point is the function entry-block setup routine (`sub_946060`), which sets up the function skeleton and then calls the parameter-prolog emitter (`sub_938240`) to emit parameter handling, followed by recursive statement emission.

**Stage 1: Function skeleton** (`sub_946060`).
Creates the LLVM `Function*` object, resolves the function type through the EDG typedef chain, and optionally sets a section name. Then creates two basic blocks: `"entry"` (the function entry point) and `"return"` (the single return block -- all return paths branch here). Inserts the `"allocapt"` sentinel into the entry block. For non-void functions, creates a `"retval"` alloca to hold the return value; for sret functions (returning aggregates), uses the first argument directly.

**Stage 2: Function prolog** (`sub_938240`).
Iterates the EDG parameter linked list (next pointer at offset +112, stride 40 bytes per LLVM argument slot) in lockstep with the LLVM function's argument list. For each parameter:
- If the first parameter has ABI kind 2 (sret), names it `"agg.result"` and advances.
- Unnamed parameters get the name `"temp_param"`; the implicit `this` parameter (flags bit 0 at offset +172) gets `"this"`.
- Creates an alloca named `<param_name>.addr` via `CreateTmpAlloca`.
- Emits a `store` of the incoming SSA argument into the alloca.
- Registers the EDG declaration -> LLVM Value mapping in a hash table (open addressing, quadratic probing) for later lookup during expression codegen.
- Optionally emits `"__val_param"` temporaries for byval aggregate parameters.

**Stage 3: Body emission** (recursive statement and expression dispatch).
Walks the IL tree for the function body, dispatching through the statement codegen switch and the expression codegen switch (detailed below).

**Stage 4: Kernel metadata** (`sub_93AE30`).
For `__global__` functions, emits `nvvm.annotations` metadata: kernel flag, `__launch_bounds__` parameters (`nvvm.maxntid`, `nvvm.reqntid`, `nvvm.minctasm`, `nvvm.maxnreg`), cluster dimensions (`nvvm.cluster_dim`, `nvvm.blocksareclusters`), and per-parameter metadata (alignment, grid_constant, hidden-parameter flags).

**Stage 5: Function attributes** (`sub_12735D0`).
Emits function-level metadata for CUDA-specific attributes: `grid_constant` (per-parameter), `preserve_n_data` / `preserve_n_control` / `preserve_n_after` (register preservation hints), and `full_custom_abi` (custom calling convention flag). These are later read back by `sub_1273F90` and re-encoded as LLVM named metadata with `MDString` keys.

## CUDA Semantic Mapping

The central task of this layer is mapping CUDA-specific semantics to LLVM IR constructs. The following table summarizes every CUDA concept and its IR representation:

| CUDA Concept | LLVM IR Representation | Codegen Function |
|---|---|---|
| `threadIdx.x` | `call i32 @llvm.nvvm.read.ptx.sreg.tid.x()` | `sub_1286E40` (special-var member access) |
| `blockIdx.y` | `call i32 @llvm.nvvm.read.ptx.sreg.ctaid.y()` | same, category 2, component 1 |
| `blockDim.z` | `call i32 @llvm.nvvm.read.ptx.sreg.ntid.z()` | same, category 1, component 2 |
| `gridDim.x` | `call i32 @llvm.nvvm.read.ptx.sreg.nctaid.x()` | same, category 3, component 0 |
| `warpSize` | `call i32 @llvm.nvvm.read.ptx.sreg.warpsize()` | `sub_1285550` (special-var direct access) |
| `__shared__` variable | `@var = addrspace(3) global ...` | `sub_916430` (address space = 3) |
| `__constant__` variable | `@var = addrspace(4) global ...` | same (address space = 4) |
| `__device__` variable | `@var = addrspace(1) global ...` | same (address space = 1) |
| `__global__` function | `define void @kern() #0` + `!{ptr @kern, !"kernel", i32 1}` in `nvvm.annotations` | `sub_93AE30` |
| `__launch_bounds__(N, M)` | `!{!"nvvm.maxntid", !"N,1,1"}` + `!{!"nvvm.minctasm", !"M"}` | same |
| `__cluster_dims__(x,y,z)` | `!{!"nvvm.cluster_dim", !"x,y,z"}` + `!{!"nvvm.blocksareclusters"}` | same |
| `__syncthreads()` | Builtin ID dispatch -> `llvm.nvvm.barrier0` | `sub_12B3FD0` (cases 0xB5--0xCC) |
| `atomicAdd(ptr, val)` | Builtin dispatch -> `atomicrmw add` or `llvm.nvvm.atomic.*` | same (cases 0xBA--0xCC) |
| `printf(fmt, ...)` | Rewritten to `vprintf(fmt, packed_buf)` | `sub_12992B0` (printf-to-vprintf lowering) |
| `__asm__("ptx" : ...)` | `call void asm sideeffect "ptx", "=r,..."(...)` | `sub_1292420` (inline-asm emitter) |
| Texture/surface ops | `call @llvm.nvvm.tex.*` / `@llvm.nvvm.suld.*` | `sub_12ADE80`, `sub_12AA9B0` |
| `__nv_float2int_rz` | `call i32 @__nv_float2int_rz(float %v)` | `sub_128A450` (cast/conversion, NVIDIA intrinsic path) |

The special variable recognition pipeline (`sub_127F7A0`) checks five preconditions before treating a variable as a hardware register read: (1) the in-kernel flag at IRGenState+376 must be set, (2) the symbol must not be `extern`, (3) it must not be template-dependent, (4) its element count must be 1, and (5) its name must be non-null. The intrinsic IDs are stored in a static 5x3 table (`unk_427F760`): 5 categories (threadIdx, blockDim, blockIdx, gridDim, warpSize) times 3 components (x, y, z), with warpSize using only the first slot.

## Common IR Emission Patterns

### Alloca-at-entry

Every local variable and parameter copy uses the same pattern:

```
sub_127FC40(ctx, type, name, alignment, addrspace)
  -> sub_921B80(ctx, type, name, arraySize=0)
     -> insert AllocaInst BEFORE the allocapt sentinel
     -> set alignment bits
     -> return alloca pointer
```

The critical detail: when `arraySize == 0` (the common case), the alloca is inserted at `IRGenState+456+24` -- the position just before the allocapt marker. This ensures all allocas land at the top of the entry block regardless of where in the function body they are created.

### Instruction insertion and debug location

After creating any instruction, the same 15-line pattern inserts it into the current basic block and attaches debug metadata:

```
bb = ctx[1][1];              // current BB
sentinel = ctx[1][2];        // insertion sentinel
sub_157E9D0(bb + 40, inst);  // update BB instruction list
// doubly-linked list pointer surgery with 3-bit tag in low bits
sub_164B780(inst, &name);    // set instruction name (e.g., "arraydecay")
debugLoc = *ctx_debug;
if (debugLoc) {
    sub_1623A60(&loc, debugLoc, 2);  // clone debug location
    *(inst + 48) = loc;              // attach at instruction offset +48
    sub_1623210(&loc, loc, inst+48); // register in debug info list
}
```

The low 3 bits of list pointers carry tag/flags (alignment guarantees those bits are zero for valid pointers). Offset +24 is `prev`, +32 is `parent block`, +48 is `debug location` on each instruction node.

### Constant vs instruction dispatch

Throughout expression codegen, a consistent threshold check determines whether to constant-fold or create an IR instruction:

```c
if (*(BYTE*)(value + 16) > 0x10u)
    // Real IR instruction -> emit IR-level operation
    result = sub_15FDBD0(opcode, value, destTy, &out, 0);  // CastInst
else
    // Constant value -> constant-fold
    result = sub_15A46C0(opcode, value, destTy, 0);         // ConstantExpr
```

The byte at `value+16` encodes the LLVM Value subclass kind. Values <= 0x10 are constants (`ConstantInt`, `ConstantFP`, `ConstantPointerNull`); values > 0x10 are `Instruction` subclasses. This avoids creating unnecessary instructions when both operands are compile-time constants.

### Short-circuit boolean evaluation

Logical AND (`&&`) and OR (`||`) use the same short-circuit pattern with PHI merge:

```llvm
; Logical AND (a && b):
  %lhs = icmp ne i32 %a, 0
  br i1 %lhs, label %land.rhs, label %land.end
land.rhs:
  %rhs = icmp ne i32 %b, 0
  br label %land.end
land.end:
  %0 = phi i1 [ false, %entry ], [ %rhs, %land.rhs ]
  %land.ext = zext i1 %0 to i32
```

Logical OR inverts the branch sense: TRUE goes to the end block (result is `true`), FALSE falls through to evaluate the RHS. Both share the same ZExt epilogue code via a merged tail at LABEL_162, selecting the name `"land.ext"` or `"lor.ext"` through a variable.

### Printf lowering

Device-side `printf` cannot use C varargs. The compiler rewrites it to CUDA's `vprintf(fmt, packed_buffer)` ABI:

1. Look up or create `@vprintf` in the module via `Module::getOrInsertFunction`.
2. Allocate a stack buffer (`"tmp"` alloca, cached at IRGenState+152 for reuse across multiple printf calls in the same function).
3. For each vararg: compute its byte size, round offset to natural alignment, GEP into the buffer (`"buf.indexed"`), bitcast if needed (`"casted"`), and store.
4. Promote `float` arguments to `double` per C variadic convention (fpext).
5. If the total packed size exceeds the current alloca size, patch the alloca's size operand in-place by manipulating the use-def chain.
6. Emit `call i32 @vprintf(ptr %fmt, ptr %buf)`.

The alloca in-place resize (step 5) is unusual -- most LLVM passes would create a new alloca. NVIDIA's motivation is to maintain a single alloca that dominates all printf pack sites within a function.

## Type Translation System

The EDG-to-LLVM type translation (`sub_91AED0` and its callees) is a worklist-driven fixed-point computation that runs before per-function codegen. It translates every EDG type node into an LLVM type, handling:

- **Primitive types**: Direct mapping (EDG `int` -> LLVM `i32`, EDG `float` -> LLVM `float`).
- **Pointer types**: Carry qualifier words at node+18 that encode CUDA address spaces (qualifier 1 = global/addrspace 1, qualifier 32 = shared/addrspace 3, qualifier 33 = constant/addrspace 4).
- **Struct/union/class types**: Recursive member-by-member translation with reference counting to handle shared sub-types and diamond inheritance.
- **Typedef chains**: Stripped by the standard `for (t = type; tag == 12; t = *(t+160))` idiom.
- **Template specializations**: Two-pass approach -- syntactic substitution (`sub_908040`) followed by semantic matching (`sub_910920`), gated by optimization flags.
- **Mutually recursive types**: Handled by the fixed-point iteration `do { changed = process_all(); } while (changed)`.

All hash tables in the type system use the standard DenseMap infrastructure with NVVM-layer sentinels (-8 / -16). See [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the common implementation.

## Global Variable Codegen

Device-side globals (`__device__`, `__constant__`, `__shared__`, `__managed__`) are emitted by `sub_916430` (determineAddressSpaceAndCreate) which reads EDG IL node attributes at offsets +0x88 (storage class), +0x9C, +0xAE, and +0xB0 to determine the NVPTX address space:

| EDG Attribute | NVPTX Address Space | PTX Qualifier |
|---|---|---|
| `__device__` | 1 (global) | `.global` |
| `__constant__` | 4 (constant) | `.const` |
| `__shared__` | 3 (shared) | `.shared` |
| Generic (default) | 0 (generic) | (none) |

After creating the `GlobalVariable`, `sub_915400` (finalizeGlobals) orchestrates module-level metadata emission: `nvvmir.version` (IR version metadata), `nvvm.annotations` (kernel and parameter annotations), `llvm.used` (prevents dead-global elimination), Debug Info Version module flag (value 3), and optionally `llvm.ident`.

## Naming Conventions

The IR generation layer produces named IR values that match Clang's naming conventions almost exactly, confirming that NVVM's codegen was closely modeled on Clang's IRGen:

| IR Name | Context | Source |
|---|---|---|
| `"entry"` | Function entry basic block | `sub_946060` |
| `"return"` | Return basic block | `sub_946060` |
| `"allocapt"` | Sentinel instruction for alloca grouping | `sub_946060` |
| `"retval"` | Return value alloca | `sub_946060` |
| `"agg.result"` | Sret argument | `sub_938240` |
| `<name>.addr` | Parameter alloca | `sub_938240` / `sub_9446C0` |
| `"temp_param"` | Unnamed parameter | `sub_938240` |
| `"this"` | Implicit C++ `this` parameter | `sub_938240` |
| `"__val_param"<name>` | Byval parameter copy | `sub_938240` |
| `"arraydecay"` | Array-to-pointer decay GEP | `sub_128D0F0` (opcode 0x15) |
| `"lnot"` / `"lnot.ext"` | Logical NOT + ZExt | `sub_128D0F0` (opcode 0x1D) |
| `"land.rhs"` / `"land.end"` / `"land.ext"` | Logical AND blocks + result | `sub_128D0F0` (opcode 0x57) |
| `"lor.rhs"` / `"lor.end"` / `"lor.ext"` | Logical OR blocks + result | `sub_128D0F0` (opcode 0x58) |
| `"cond.true"` / `"cond.false"` / `"cond.end"` | Ternary operator blocks | `sub_128D0F0` (opcode 0x67) |
| `"tobool"` / `"conv"` | Cast results | `sub_128A450` (cast/conversion) |
| `"sub.ptr.lhs.cast"` / `"sub.ptr.rhs.cast"` / `"sub.ptr.sub"` / `"sub.ptr.div"` | Pointer subtraction | `sub_128D0F0` (opcode 0x34) |
| `"if.then"` / `"if.else"` / `"if.end"` | If statement blocks | `sub_937020` |
| `"while.cond"` / `"while.body"` / `"while.end"` | While loop blocks | `sub_937180` |
| `"for.cond"` / `"for.body"` / `"for.inc"` / `"for.end"` | For loop blocks | `sub_936D30` |
| `"do.body"` / `"do.cond"` / `"do.end"` | Do-while loop blocks | `sub_936B50` |
| `"bf.*"` | Bitfield access temporaries (30+ variants) | `sub_1282050` / `sub_1284570` |
| `"predef_tmp_comp"` | Special register read result | `sub_1286E40` |
| `"buf.indexed"` / `"casted"` | Printf buffer GEP and cast | `sub_12992B0` |
| `"asmresult"` | Inline asm extractvalue result | `sub_1292420` |

## Sub-Page Navigation

The IR generation subsystem is documented in detail across four sub-pages, each covering a major functional area:

- **[Expression & Constant Codegen](./irgen-expressions.md)** -- The expression master dispatch (`sub_128D0F0`), its 40-operator inner switch, compile-time constant emission (`sub_127D8B0`), and the cast/conversion codegen (`sub_128A450`). Covers every C/C++ expression type from array decay to pointer subtraction to logical short-circuit.

- **[Statement & Control Flow Codegen](./irgen-statements.md)** -- The statement dispatcher (`sub_9363D0`), basic block creation for if/while/do-while/for/switch, cleanup scope management for C++ destructors, label and goto handling, and `#pragma unroll` metadata attachment.

- **[Function, Call & Inline Asm Codegen](./irgen-functions.md)** -- Function skeleton creation (`sub_946060`), the parameter prolog (`sub_938240`), call instruction emission with ABI classification (`sub_93CB50`), inline asm template parsing and constraint construction (`sub_1292420`), printf-to-vprintf lowering (`sub_12992B0`), and the 770-entry builtin dispatch table (`sub_12B3FD0`).

- **[Type Translation, Globals & Special Vars](./irgen-types.md)** -- The fixed-point type translation system (`sub_91AED0`), address space mapping for CUDA memory qualifiers, global variable creation (`sub_916430`), kernel metadata emission (`sub_93AE30`), function attribute handling (`sub_12735D0`), and special variable codegen for threadIdx/blockIdx/blockDim/gridDim/warpSize.
