# printf-lowering

The printf lowering pass rewrites device-side `printf()` calls into CUDA's runtime `vprintf()` ABI. GPU hardware does not support C variadic function calls, so the compiler must pack all arguments into a stack buffer and emit a two-argument call to `vprintf(format_string, arg_buffer_ptr)`. CICC implements this transformation at two levels: a module-level IR pass and an AST-level lowering function.

| | |
|---|---|
| **Pass name** | `printf-lowering` |
| **Class** | `llvm::PrintfLoweringPass` |
| **Scope** | Module pass |
| **Registration** | New PM slot 130, `sub_2342890` |
| **Module-level entry** | `sub_1CB1E60` (31 KB) |
| **AST-level lowering** | `sub_12992B0` (24 KB) |
| **Enable knob** | `nvvm-lower-printf` (registered at `ctor_269`) |

## Two Lowering Stages

Printf lowering happens at two points in the compilation pipeline:

**Stage 1 -- AST-level** (`sub_12992B0`): During initial IR generation from the EDG frontend output, when the code generator encounters a direct call to `printf`, it intercepts the call and emits the `vprintf` rewrite inline. This is the earlier, more detailed pass that handles type promotion, buffer packing, and alloca management.

**Stage 2 -- Module-level** (`sub_1CB1E60`): A cleanup pass that runs during the LLVM optimization pipeline. It catches any remaining `printf` calls that survived the AST lowering (e.g., from linked bitcode modules or inlined functions) and applies the same transformation. This pass validates that the format string is a string literal: `"The first argument for printf must be a string literal!"`.

## AST-Level Lowering Algorithm (sub_12992B0)

The AST-level lowering is the more thoroughly analyzed implementation. It operates in six phases:

### Phase 1: Resolve the vprintf Symbol

The pass looks up or creates the `"vprintf"` function declaration in the module:

1. Build the `vprintf` parameter type list: `(i8*, i8*)`
2. Create the `FunctionType` via `sub_1644EA0`
3. Call `sub_1632190(Module*, "vprintf", 7, funcType)` -- this is `Module::getOrInsertFunction`

The literal string `"vprintf"` with length 7 is stored in a local variable.

### Phase 2: Set Up Argument List

- The format string (`**a3`) becomes the first argument
- The remaining varargs (`a3[1..]`) are collected into a dynamic argument array
- A 22-QWORD (176-byte) stack small-buffer optimization avoids heap allocation for typical printf calls with fewer than ~16 arguments

**Fast path**: if `argCount <= 1` (format string only, no varargs), the pass skips buffer creation entirely and emits `vprintf(fmt, undef)` using `sub_15A06D0` (UndefValue::get).

### Phase 3: Allocate Packed Argument Buffer

For the varargs case, a stack buffer named `"tmp"` is allocated:

- `sub_127FC40(context, type, "tmp", alignment=8, addrspace=0)` creates an alloca
- The alloca is cached at `a1[19]` and reused across multiple printf calls within the same function
- If a cached alloca exists, its size is reused (and potentially grown in Phase 5)

### Phase 4: Per-Argument Processing

For each vararg, the pass:

1. **Float promotion**: per C variadic calling convention, `float` arguments are promoted to `double` via an `fpext` instruction. Detected when `type_info[+12] == 2` and `type_info[+16] != 0`.

2. **Type size calculation**: a multi-level switch on the LLVM type tag computes the byte width:

   | Type tag | Size (bits) | Notes |
   |---|---|---|
   | 1 | 16 | half / i16 |
   | 2 | 32 | float / i32 |
   | 3, 9 | 64 | double / i64 |
   | 4 | 80 | x86_fp80 |
   | 5, 6 | 128 | fp128 / ppc_fp128 |
   | 7 | target-dependent | Pointer size from DataLayout |
   | 11 | custom | `dword >> 8` (arbitrary-width integer) |
   | 13 | aggregate | Struct size from DataLayout |
   | 14 | packed struct | Complex alignment calculation, up to 3 levels of nesting |

3. **Alignment and offset**: each argument is placed at the next naturally-aligned offset in the buffer. If `offset % argSize != 0`, the offset is rounded up.

4. **GEP creation**: a `GetElementPtr` named `"buf.indexed"` indexes into the packed buffer at the computed byte offset.

5. **Bitcast**: if the GEP result type differs from the argument type, a `bitcast` instruction named `"casted"` (opcode 47) is emitted.

6. **Store**: the argument value is stored into the buffer slot via a `StoreInst`.

### Phase 5: Alloca Resize

After processing all arguments, the pass checks whether the total packed size exceeds the current alloca size. If so, it patches the alloca's size operand in-place by manipulating the use-def chain directly -- unlinking the old size constant and linking a new one. This unusual technique avoids creating a second alloca while ensuring a single allocation dominates all printf pack sites.

### Phase 6: Emit vprintf Call

`sub_1285290` emits the final call: `vprintf(format_string, arg_buffer_ptr)`.

Cleanup frees any heap-allocated argument arrays (from the small-buffer overflow path).

## Module-Level Pass (sub_1CB1E60)

The module-level pass at `0x1CB1E60` (31 KB) performs a similar transformation but operates on already-lowered LLVM IR rather than AST nodes. Key recovered strings:

| String | Purpose |
|---|---|
| `"DataLayout must be available for lowering printf!"` | Guard: DataLayout required |
| `"vprintf"` | Target function name |
| `"The first argument for printf must be a string literal!"` | Format string validation |
| `"vprintfBuffer.local"` | Name of the packed argument buffer alloca |
| `"bufIndexed"` | Name of GEP instructions into the buffer |

The module-level pass uses `"vprintfBuffer.local"` as the alloca name (versus `"tmp"` in the AST-level lowering), and `"bufIndexed"` for the GEP instructions (versus `"buf.indexed"`). These naming differences confirm the two implementations are distinct codepaths.

## Implementation Details

**Small-buffer optimization**: the argument array uses a 22-QWORD (176-byte) stack buffer. Only if more than ~16 arguments overflow does it heap-allocate via the SmallVector grow path (`sub_16CD150`). This avoids `malloc` for typical printf calls.

**Alloca caching**: `a1[19]` in the IRGenState caches the `"tmp"` alloca across multiple printf calls within the same function. This reduces alloca instruction count in functions with many printf calls.

**Struct nesting limit**: the type-size calculation handles up to 3 levels of nested struct packing (three nested switch statements in the decompilation). Deeper nesting hits a `JUMPOUT` at `0x129A22F` -- likely an assertion for structs nested more than 3 levels in printf arguments.

**Pointer tag bits**: the basic block instruction list uses an intrusive doubly-linked list where the low 3 bits of next/prev pointers carry metadata tags (masked with `0xFFFFFFFFFFFFFFF8`). This is consistent with LLVM's `ilist` implementation using pointer-int pairs.

## Diagnostic Strings

Diagnostic strings recovered from `p2-B08-printf-lowering.txt` and `p1.7-04-sweep-0x1B00000-0x1CFFFFF.txt`.

| String | Source | Category | Trigger |
|--------|--------|----------|---------|
| `"DataLayout must be available for lowering printf!"` | `sub_1CB1E60` (module-level pass) | Assertion/Error | Module lacks DataLayout; fatal guard at module pass entry |
| `"The first argument for printf must be a string literal!"` | `sub_1CB1E60` (module-level pass) | Error | Format string argument is not a constant string; validation failure |
| `"vprintf"` | `sub_1632190` / `sub_12992B0` | Symbol | Target function name looked up or created in the module (literal string, length 7) |
| `"vprintfBuffer.local"` | `sub_1CB1E60` (module-level pass) | IR name | Name of the packed argument buffer alloca in the module-level pass |
| `"bufIndexed"` | `sub_1CB1E60` (module-level pass) | IR name | Name of GEP instructions into the argument buffer in the module-level pass |
| `"tmp"` | `sub_12992B0` (AST-level lowering) | IR name | Name of the packed argument buffer alloca in the AST-level lowering; cached at `a1[19]` |
| `"buf.indexed"` | `sub_12992B0` (AST-level lowering) | IR name | Name of GEP instructions into the argument buffer in the AST-level lowering |
| `"casted"` | `sub_12992B0` (AST-level lowering) | IR name | Name of bitcast instructions when GEP result type differs from argument type (opcode 47) |
| `"nvvm-lower-printf"` | `ctor_269` | Knob | Enable knob for the printf lowering pass |

The two lowering stages produce different IR names for the same conceptual entities (`"vprintfBuffer.local"` vs `"tmp"` for the alloca, `"bufIndexed"` vs `"buf.indexed"` for the GEPs), confirming they are distinct codepaths.

## IRGenState Layout

The codegen context object used by the AST-level lowering:

| Offset | Field | Purpose |
|---|---|---|
| `a1[4]` | `Module*` | The LLVM module |
| `a1[5]` | Return type | Function return type / type context |
| `a1[6]` | `DebugLoc` | Current debug location |
| `a1[7]` | `BasicBlock*` | Current insertion block |
| `a1[8]` | Iterator | Insertion point in BB's instruction list |
| `a1[9]` | AS context | Address space context for alloca type creation |
| `a1[19]` | `AllocaInst*` | Cached `"tmp"` alloca (reused across printf calls) |
