# __managed__ Variables

The `__managed__` attribute declares a variable in CUDA Unified Memory -- a memory region accessible from both host (CPU) and device (GPU) code, with the CUDA runtime handling page migration transparently. Unlike `__device__` variables (accessible only from device code without explicit `cudaMemcpy`), managed variables can be read and written by both the host and device using the same pointer. The hardware and driver cooperate to migrate pages on demand between CPU and GPU memory, so neither the programmer nor the compiler needs to issue explicit copies.

The constraint set on `__managed__` reflects two fundamental realities. First, unified memory is a **runtime feature**: the compiler cannot resolve managed addresses at compile time, so every host-side access must be gated behind a lazy initialization call that registers the variable with the CUDA runtime's unified memory subsystem. Second, unified memory requires **hardware support**: the Kepler architecture (compute capability 3.0) introduced the UVA (Unified Virtual Addressing) infrastructure that managed memory depends on. These two realities drive the entire implementation -- the attribute handler sets both a managed flag and a device flag (because managed memory is device-global memory with extra runtime semantics), the validation chain rejects memory spaces and qualifiers that conflict with runtime writability, and the code generator wraps every host-side access in a comma-operator expression that forces lazy initialization.

## Key Facts

| Property | Value |
|---|---|
| Attribute kind byte | `0x66` = `'f'` (102) |
| Handler function | `sub_40E0D0` (`apply_nv_managed_attr`, 47 lines, `attribute.c:10523`) |
| Entity node flags set | `entity+149` bit 0 (`__managed__`) AND `entity+148` bit 0 (`__device__`) |
| Detection bitmask | `(*(_WORD*)(entity + 148) & 0x101) == 0x101` |
| Minimum architecture | compute_30 (Kepler) -- `dword_126E4A8 >= 30` |
| Applies to | Variables only (entity kind 7) |
| Diagnostic codes | 3481, 3482, 3485, 3577 (attribute application); arch/config errors (declaration processing) |
| Managed RT boilerplate emitter | `sub_489000` (`process_file_scope_entities`, line 218) |
| Access wrapper emitters | `sub_4768F0` (`gen_name_ref`), `sub_484940` (`gen_variable_name`) |
| Managed access prefix string | `0x839570` (65 bytes) |
| Managed RT static block string | `0x83AAC8` (243 bytes) |
| Managed RT init function string | `0x83ABC0` (210 bytes) |

## Semantic Meaning

A `__managed__` variable occupies a single virtual address that is valid on both host and device. The CUDA runtime allocates the variable through `cudaMallocManaged` during module initialization and registers it so the driver can track page ownership. When a kernel accesses the variable, the GPU's page fault handler migrates the page from CPU memory (if needed). When host code accesses it after a kernel launch, the runtime ensures the GPU has finished writing and the page is migrated back to CPU-accessible memory.

This is fundamentally different from the other three memory spaces:

| Space | Accessibility | Migration | Lifetime |
|---|---|---|---|
| `__device__` | Device only (host needs `cudaMemcpy`) | Manual | Program lifetime |
| `__shared__` | Device only, per-thread-block | None (on-chip SRAM) | Block lifetime |
| `__constant__` | Device read-only (host writes via `cudaMemcpyToSymbol`) | Manual | Program lifetime |
| `__managed__` | Host and device, same pointer | Automatic (page faults) | Program lifetime |

Because managed memory is fundamentally device global memory with runtime-managed migration, the `__managed__` handler always sets the `__device__` bit alongside the `__managed__` bit. This is not redundant -- it ensures that all code paths that check for "device-accessible variable" (error 3483 scope checks, external linkage warning 3648, cross-space reference validation) treat managed variables correctly. A managed variable IS a device variable; it just happens to also be host-accessible through the runtime's page migration.

### Worked Example: Source to Emitted Wrapper

A single managed declaration triggers both the entity flag setup and a `comma-operator` access-wrapper rewrite for every host-side read or write:

```cpp
// User source
__managed__ int counter;
void host_inc() { counter++; }
```

After cudafe++ processes the file, the host stub in `.cudafe1.stub.c` looks roughly like:

```c
// Emitted host wrapper (abridged)
static int __nv_managed_init_done_counter;
static void __nv_init_managed_counter(void) {
    if (!__nv_managed_init_done_counter) {
        __cudaInitModule(&__nv_dummy);   // forces runtime registration
        __nv_managed_init_done_counter = 1;
    }
}
void host_inc() {
    (__nv_init_managed_counter(), counter)++;   // comma-op guards every access
}
```

The lazy-init guard is what the access wrapper emitters at `sub_4768F0` / `sub_484940` synthesize from the managed-access prefix string at `0x839570`. The same `__device__` flag set in step 1 of `apply_nv_managed_attr` is what lets `counter` survive the `__cudaRegisterVar` table walk later in registration.

### Why the Constraints Exist

Each validation check enforced by the handler exists for a specific hardware or semantic reason:

- **Variables only (kind 7)**: Unified memory is a storage concept. Functions do not reside in managed memory -- they have execution spaces, not memory spaces.

- **Cannot be `__shared__` or `__constant__`**: These are mutually exclusive memory spaces that occupy different physical hardware. `__shared__` is per-block on-chip SRAM with no concept of host accessibility. `__constant__` is a read-only cached region with no write path from device code. Managed memory is global DRAM with page migration. They cannot coexist.

- **Cannot be `thread_local`**: Thread-local storage uses thread-specific addressing (TLS segments) which is a host-side concept incompatible with CUDA's execution model. A managed variable must have a single global address visible to all threads on both host and device.

- **Cannot be a local variable or reference type**: Managed variables require runtime registration with the CUDA driver during module loading. Local variables are stack-allocated with lifetimes that cannot be tracked by the runtime. References cannot cross address spaces -- a reference to a managed variable on the host would hold a CPU virtual address that is meaningless on the device.

- **Requires compute_30+**: Unified Virtual Addressing (UVA), the hardware foundation for managed memory, was introduced with the Kepler architecture (compute capability 3.0). On earlier architectures, host and device have separate, non-overlapping virtual address spaces, making transparent page migration impossible.

- **Incompatible with `__grid_constant__`**: Grid-constant parameters are loaded into constant memory at kernel launch. A managed variable's value is determined by its current page state, which can change between kernel launches. The two semantics are contradictory.

## Attribute Application: apply_nv_managed_attr

### sub_40E0D0 -- Full Pseudocode

The `__managed__` attribute handler is the simplest of the four memory space handlers and demonstrates the complete validation template. Called from `apply_one_attribute` (`sub_413240`) when the attribute kind byte is `'f'` (102).

```c
// sub_40E0D0 -- apply_nv_managed_attr (attribute.c:10523)
// a1: attribute node pointer (attribute_node_t*)
// a2: entity node pointer (entity_t*)
// a3: entity kind (uint8_t)
// returns: entity node pointer (passthrough)

entity_t* apply_nv_managed_attr(attr_node_t* a1, entity_t* a2, uint8_t a3) {

    // ===== Gate: variables only =====
    // Entity kind 7 = variable. Any other kind (function=11, type=6, etc.)
    // is an internal error -- the dispatch table should never route
    // __managed__ to a non-variable entity.
    if (a3 != 7)
        internal_error("attribute.c", 10523, "apply_nv_managed_attr", 0, 0);

    // ===== Step 1: Set managed + device flags =====
    // Save current memory space byte for later checks.
    // Managed memory IS device global memory, so both flags must be set.
    uint8_t old_space = a2->byte_148;
    a2->byte_149 |= 0x01;         // set __managed__ flag
    a2->byte_148 = old_space | 1;  // set __device__ flag

    // ===== Step 2: Mutual exclusion -- shared + constant =====
    // The expression ((x & 2) != 0) + ((x & 4) != 0) == 2 is true
    // only when BOTH __shared__ (bit 1) and __constant__ (bit 2) are set.
    // This catches an impossible three-way conflict, NOT managed+shared
    // or managed+constant individually. The individual conflicts
    // (__managed__ + __shared__, __managed__ + __constant__) are caught
    // by the __grid_constant__ check or by subsequent declaration processing.
    if (((old_space & 2) != 0) + ((old_space & 4) != 0) == 2)
        emit_error(3481, a1->source_loc);  // "conflicting CUDA memory spaces"

    // ===== Step 3: Thread-local check =====
    // Byte +161 bit 7 (sign bit when read as signed char) indicates
    // thread_local storage duration. Managed variables must have
    // static storage duration with a single global address.
    if ((signed char)a2->byte_161 < 0)
        emit_error(3482, a1->source_loc);  // "CUDA memory space on thread_local"

    // ===== Step 4: Local variable / reference type check =====
    // Byte +81 bit 2 indicates the entity is declared in a local scope
    // (block scope, function parameter, or reference type).
    // Managed variables require file-scope lifetime for runtime registration.
    if (a2->byte_81 & 0x04)
        emit_error(3485, a1->source_loc);  // "CUDA memory space on local/ref"

    // ===== Step 5: __grid_constant__ conflict =====
    // Byte +164 bit 2 is the __grid_constant__ flag on the parameter entity.
    // If set, check whether this entity also has a conflicting memory space.
    // The 16-bit word read at +148 with mask 0x0102 catches:
    //   byte +148 bit 1 (0x02) = __shared__
    //   byte +149 bit 0 (0x01, as 0x100 in word) = __managed__
    // (Little-endian: word = byte_149 << 8 | byte_148)
    if ((a2->byte_164 & 0x04) && (*(uint16_t*)(a2 + 148) & 0x0102)) {

        // Build error message: select most restrictive space name
        uint8_t space = a2->byte_148;
        const char* name = "__constant__";
        if (!(space & 0x04)) {
            name = "__managed__";
            if (!(a2->byte_149 & 0x01)) {
                name = "__shared__";
                if (!(space & 0x02)) {
                    name = "__device__";
                    if (!(space & 0x01))
                        name = "";
                }
            }
        }
        emit_error_with_name(3577, a1->source_loc, name);
        // "memory space %s incompatible with __grid_constant__"
    }

    return a2;
}
```

### Entity Node Fields Modified

| Offset | Field | Bits Set | Meaning |
|---|---|---|---|
| `+148` | `memory_space` | bit 0 (`0x01`) | `__device__` -- variable lives in device global memory |
| `+149` | `extended_space` | bit 0 (`0x01`) | `__managed__` -- variable is in unified memory |

### Entity Node Fields Read (Validation)

| Offset | Field | Mask | Meaning |
|---|---|---|---|
| `+148` | `memory_space` | `0x02` | `__shared__` flag (mutual exclusion check) |
| `+148` | `memory_space` | `0x04` | `__constant__` flag (mutual exclusion check) |
| `+161` | `storage_flags` | bit 7 (sign) | `thread_local` storage duration |
| `+81` | `scope_flags` | `0x04` | Local scope / reference type indicator |
| `+164` | `cuda_flags` | `0x04` | `__grid_constant__` parameter flag |
| `+148:149` | `space_word` | `0x0102` | Combined `__shared__` OR `__managed__` (grid_constant conflict) |

### Comparison with apply_nv_device_attr (sub_40EB80)

The `__device__` handler's variable path (entity kind 7) is structurally identical to `apply_nv_managed_attr`, minus the `byte_149 |= 1` step. Both handlers:

1. Set `byte_148 |= 0x01` (device memory space)
2. Check error 3481 (shared + constant mutual exclusion)
3. Check error 3482 (thread_local)
4. Check error 3485 (local variable)
5. Check error 3577 (grid_constant conflict)

The only difference: `__managed__` additionally sets `byte_149 |= 0x01`. The `__device__` handler also has a function path (kind 11) for setting execution space bits -- `__managed__` has no function path because managed memory is a storage concept, not an execution concept.

## Architecture Gating

The compute_30 requirement for `__managed__` is enforced during declaration processing, not in the attribute handler itself. The attribute handler (`sub_40E0D0`) sets the bitfield flags unconditionally; the architecture check happens later when the declaration is fully processed.

Two diagnostic tags cover managed architecture gating:

| Tag | Message | Condition |
|---|---|---|
| `unsupported_arch_for_managed_capability` | `__managed__ variables require architecture compute_30 or higher` | `dword_126E4A8 < 30` |
| `unsupported_configuration_for_managed_capability` | `__managed__ variables are not yet supported for this configuration (compilation mode (32/64 bit) and/or target operating system)` | Configuration-specific flag check |

The architecture check uses the global `dword_126E4A8` which stores the SM version number from the `--gpu-architecture` flag. The value 30 corresponds to sm_30 (Kepler), the first architecture with Unified Virtual Addressing (UVA) support. The configuration check covers edge cases like 32-bit compilation mode or unsupported operating systems where the CUDA runtime's managed memory subsystem is unavailable.

## IL Emission Path

`__managed__` follows the **collapse-to-entity-bits + runtime-rewrite** pattern, distinct from any other CUDA attribute. There is no `kind 25` IL re-emission, no preserved attribute chain entry, and no launch-config struct field. Instead, after `apply_nv_managed_attr` returns, three independent downstream emitters react to the `(byte+148 | (byte+149 << 8)) & 0x101 == 0x101` detection mask:

1. **File-scope boilerplate emitter** (`sub_489000`, line 218) unconditionally writes the `__nv_init_managed_rt` lazy-init wrapper and `__nv_inited_managed_rt` guard into every `.int.c`, regardless of whether the TU declares any managed variables. The guard prevents zero-cost runs from registering anything.

2. **Variable-name emitter** (`sub_484940`) wraps every direct host-side reference in a comma-operator that calls `__nv_init_managed_rt()` before yielding the variable.

3. **Qualified-name emitter** (`sub_4768F0`) applies the same wrapping for qualified, templated, or member references when the entity kind is `7` (variable) and the call context is not nested.

The original `0x48` attribute IL node is consumed at application time and never reaches the IL output dispatcher. The entire post-application footprint is bytes `+148` bit 0 and `+149` bit 0; from the IL display walker's perspective, `__managed__` is invisible. The runtime/textual rewrite is the IL emission for this attribute.

## Managed Runtime Boilerplate

Every `.int.c` file emitted by cudafe++ contains a block of managed runtime initialization code, emitted unconditionally by `sub_489000` (`process_file_scope_entities`) at line 218. This block is emitted regardless of whether the translation unit contains any `__managed__` variables -- the static guard flag ensures zero overhead when no managed variables exist.

### Static Declarations

Four declarations are emitted as a single string literal from `0x83AAC8` (243 bytes):

```c
// Emitted verbatim by sub_489000, line 218
static char __nv_inited_managed_rt = 0;
static void **__nv_fatbinhandle_for_managed_rt;
static void __nv_save_fatbinhandle_for_managed_rt(void **in) {
    __nv_fatbinhandle_for_managed_rt = in;
}
static char __nv_init_managed_rt_with_module(void **);
```

Each symbol serves a specific role in the initialization chain:

| Symbol | Type | Role |
|---|---|---|
| `__nv_inited_managed_rt` | `static char` | Guard flag: 0 = uninitialized, nonzero = initialized |
| `__nv_fatbinhandle_for_managed_rt` | `static void**` | Cached fatbinary handle, populated during `__cudaRegisterFatBinary` |
| `__nv_save_fatbinhandle_for_managed_rt` | `static void(void**)` | Callback that stores the fatbin handle -- called at program startup |
| `__nv_init_managed_rt_with_module` | `static char(void**)` | Forward declaration -- defined later by `crt/host_runtime.h` |

The forward declaration of `__nv_init_managed_rt_with_module` is critical: this function is provided by the CUDA runtime headers and performs the actual `cudaRegisterManagedVariable` calls. By forward-declaring it here, the managed runtime boilerplate can reference it before the runtime header is `#include`d later in the `.int.c` file.

### Lazy Initialization Function

Emitted immediately after the static block (string at `0x83ABC0`, 210 bytes):

```c
// sub_489000, lines 221-224
// Conditional prefix:
if (dword_106BF6C)  // alternative host compiler mode
    emit("__attribute__((unused)) ");

// Function body:
static inline void __nv_init_managed_rt(void) {
    __nv_inited_managed_rt = (
        __nv_inited_managed_rt
            ? __nv_inited_managed_rt
            : __nv_init_managed_rt_with_module(
                  __nv_fatbinhandle_for_managed_rt)
    );
}
```

The ternary is a lazy-init idiom. On first call, `__nv_inited_managed_rt` is 0 (falsy), so the false branch executes `__nv_init_managed_rt_with_module`, which registers all managed variables in the translation unit and returns nonzero. The result is stored back into `__nv_inited_managed_rt`, so subsequent calls short-circuit through the true branch and return the existing nonzero value without re-initializing.

The `__attribute__((unused))` prefix is conditionally added when `dword_106BF6C` (alternative host compiler mode) is set. This suppresses `-Wunused-function` warnings on host compilers that may not see any call sites for this function if no managed variables exist in the translation unit.

### Runtime Registration Sequence

The full initialization flow spans the compilation and runtime startup pipeline:

```
Compile time (cudafe++ emits into .int.c):
  1. __nv_save_fatbinhandle_for_managed_rt() -- defined, stores fatbin handle
  2. __nv_init_managed_rt_with_module()      -- forward-declared only
  3. __nv_init_managed_rt()                  -- defined, lazy init wrapper
  4. #include "crt/host_runtime.h"           -- provides _with_module() definition

Program startup:
  5. __cudaRegisterFatBinary() calls __nv_save_fatbinhandle_for_managed_rt()
     to cache the fatbin handle for this translation unit

First managed variable access:
  6. Comma-operator wrapper calls __nv_init_managed_rt()
  7. Guard flag is 0, so __nv_init_managed_rt_with_module() executes
  8. __nv_init_managed_rt_with_module() calls cudaRegisterManagedVariable()
     for every __managed__ variable in the translation unit
  9. Guard flag set to nonzero, preventing re-initialization

Subsequent accesses:
  10. Comma-operator wrapper calls __nv_init_managed_rt()
  11. Guard flag is nonzero, ternary short-circuits, no runtime call
```

## Host Access Transformation: The Comma-Operator Pattern

When cudafe++ generates the `.int.c` host-side code and encounters a reference to a `__managed__` variable, it wraps the access in a comma-operator expression. This is the core mechanism that ensures the CUDA managed memory runtime is initialized before any managed variable is touched on the host.

### Detection

Two backend emitter functions detect managed variables using the same 16-bit bitmask test:

```c
// Used by both sub_4768F0 (gen_name_ref) and sub_484940 (gen_variable_name)
if ((*(_WORD*)(entity + 148) & 0x101) == 0x101)
```

In little-endian layout, the 16-bit word at offset 148 spans bytes `+148` (low) and `+149` (high). The mask `0x101` tests:
- Bit 0 of byte `+148` (`0x01`): `__device__` flag
- Bit 0 of byte `+149` (`0x100` in the word): `__managed__` flag

Both bits are always set together by `apply_nv_managed_attr`, so this test is equivalent to "is this a managed variable?"

### Transformed Output

For a managed variable named `managed_var`, the emitter produces:

```c
(*( (__nv_inited_managed_rt ? (void)0 : __nv_init_managed_rt()), (managed_var)))
```

The prefix string lives at `0x839570` (65 bytes):
```
"(*( (__nv_inited_managed_rt ? (void)0: __nv_init_managed_rt()), ("
```

After emitting the variable name, the suffix `)))` closes the expression.

### Why This Works: Anatomy of the Expression

Reading from inside out:

```
(*( (__nv_inited_managed_rt ? (void)0 : __nv_init_managed_rt()), (managed_var)))
     ^--- ternary: lazy init guard ----^                          ^--- value ---^
     ^--- comma operator: init side-effect, then yield value --------------------------^
^--- dereference: access the managed variable's storage ---------------------------------^
```

1. **Ternary** `__nv_inited_managed_rt ? (void)0 : __nv_init_managed_rt()` -- The guard flag is checked. If nonzero (already initialized), the expression evaluates to `(void)0`, which generates no code. If zero (first access), `__nv_init_managed_rt()` is called, which performs CUDA runtime registration and sets the guard flag to nonzero.

2. **Comma operator** `(init_expr, (managed_var))` -- The C comma operator evaluates its left operand for side effects only, discards the result, then evaluates and returns its right operand. This guarantees the initialization side-effect is sequenced before the variable access, per C/C++ sequencing rules (C11 6.5.17, C++17 [expr.comma]).

3. **Outer dereference** `*(...)` -- The outer `*` dereferences the result. After runtime registration, the managed variable's symbol resolves to the unified memory pointer that the CUDA runtime allocated via `cudaMallocManaged`. The dereference yields the actual variable value.

The entire expression is parenthesized to be safely usable in any expression context -- assignments, function arguments, member access, etc.

### Two Emitter Paths

The access transformation is applied by two separate functions, covering different name resolution contexts:

**sub_484940 (`gen_variable_name`, 52 lines)** -- handles direct variable name emission. Simpler structure: check the `0x101` bitmask, emit prefix, emit the name (handling three sub-cases: thread-local via `this`, anonymous via `sub_483A80`, or regular via `sub_472730`), emit suffix.

```c
// sub_484940 -- gen_variable_name (pseudocode)
void gen_variable_name(entity_t* a1) {
    bool needs_suffix = false;

    // Check: is this a __managed__ variable?
    if ((*(uint16_t*)(a1 + 148) & 0x101) == 0x101) {
        needs_suffix = true;
        emit("(*( (__nv_inited_managed_rt ? (void)0: __nv_init_managed_rt()), (");
    }

    // Emit variable name (three cases)
    if (a1->byte_163 & 0x80)
        emit("this");                       // thread-local proxy
    else if (a1->byte_165 & 0x04)
        emit_anonymous_name(a1);            // compiler-generated name
    else
        gen_expression_or_name(a1, 7);      // regular name emission

    if (needs_suffix)
        emit(")))");
}
```

**sub_4768F0 (`gen_name_ref`, 237 lines)** -- handles qualified name references with `::` scope resolution, template arguments, `__super::` qualifier, and member access. The managed wrapping applies an additional gate: `a3 == 7` (entity is a variable) AND `!v7` (the fourth parameter is zero, meaning no nested context that already handles initialization).

```c
// sub_4768F0 -- gen_name_ref, managed wrapping (lines 160-163, 231-236)
int gen_name_ref(context_t* ctx, entity_t* entity, uint8_t kind, int nested) {

    bool needs_suffix = false;

    if (!nested && kind == 7
        && (*(uint16_t*)(entity + 148) & 0x101) == 0x101) {
        needs_suffix = true;
        emit("(*( (__nv_inited_managed_rt ? (void)0: __nv_init_managed_rt()), (");
    }

    // ... 200+ lines of qualified name emission ...
    // handles ::, template<>, __super::, member access paths

    if (needs_suffix) {
        emit(")))");
        return 1;
    }
    // ...
}
```

### Host-Side Exemption in Cross-Space Checking

Managed variables receive a special exemption in the cross-space reference validation performed by `record_symbol_reference_full` (`sub_72A650`). When host code references a `__device__` variable, the checker would normally emit error 3548. But managed variables are specifically exempted:

```c
// Inlined in sub_72A650, cross-space variable reference check
if ((*(uint16_t*)(var_info + 148) & 0x0101) == 0x0101)
    return;  // managed variable -- host access is legal
```

This uses the same `0x0101` bitmask to detect managed variables. The exemption exists because managed variables are explicitly designed for host access -- that is their entire purpose. Without this exemption, every host-side `__managed__` variable access would trigger a spurious "reference to device variable from host code" error.

## Managed Variables and constexpr

The declaration processor `sub_4DEC90` (`variable_declaration`) imposes additional constraints when `__managed__` is combined with `constexpr`:

| Error | Condition | Description |
|---|---|---|
| 3568 | `__constant__` + constexpr | `__constant__` combined with constexpr (prevents runtime initialization) |
| 3566 | `__constant__` + constexpr + auto | `__constant__` constexpr with auto deduction |

These errors target `__constant__` specifically, but the validation cascade also generates the space name for managed variables when constructing error messages. The space name selection uses the same priority cascade as the attribute handler:

```c
// sub_4DEC90, line ~357 -- selecting display name for error messages
const char* space_name = "__constant__";
if (!(space & 0x04)) {
    space_name = "__managed__";
    if (!(*(uint8_t*)(entity + 149) & 0x01)) {
        space_name = "__host__ __device__" + 9;  // pointer trick: "__device__"
        if (space & 0x02)
            space_name = "__shared__";
    }
}
```

The string `"__device__"` is obtained by taking `"__host__ __device__"` and advancing by 9 bytes, skipping the `"__host__ "` prefix. This is a binary-level optimization where the compiler shares string storage between the combined form and the standalone `"__device__"` substring.

## Error 3648: External Linkage Warning

The post-definition check in `sub_4DC200` (`mark_defined_variable`) warns when a device-accessible variable has external linkage. This affects managed variables because they always have the `__device__` bit set:

```c
// sub_4DC200 -- mark_defined_variable
// Condition for warning 3648:
if ((entity->byte_148 & 3) == 1    // __device__ set AND __shared__ NOT set
    && !is_compiler_generated(entity)
    && (entity->byte_80 & 0x70) != 0x10)  // not anonymous
{
    warning(3648, entity->source_loc);
}
```

The bit test `(byte_148 & 3) == 1` checks that bit 0 (`__device__`) is set and bit 1 (`__shared__`) is NOT set. This catches:
- `__device__` variables (0x01): yes, `(0x01 & 3) == 1`
- `__managed__` variables (0x01 at +148, 0x01 at +149): yes, `(0x01 & 3) == 1`
- `__device__ __constant__` (0x05): yes, `(0x05 & 3) == 1`
- `__shared__` (0x02): no, `(0x02 & 3) == 2`
- `__constant__` alone (0x04): no, `(0x04 & 3) == 0`

Managed variables therefore trigger this warning if they have external linkage and are not compiler-generated.

## Diagnostic Summary

| Error | Phase | Condition | Message |
|---|---|---|---|
| 3481 | Attribute application | `__shared__` AND `__constant__` both set | Conflicting CUDA memory spaces |
| 3482 | Attribute application | `thread_local` storage duration | CUDA memory space on thread_local variable |
| 3485 | Attribute application | Local scope or reference type | CUDA memory space on local variable |
| 3577 | Attribute application | `__grid_constant__` + managed/shared | Memory space incompatible with `__grid_constant__` |
| 3648 | Post-definition | External linkage on device-accessible (non-shared) var | External linkage warning |
| (arch) | Declaration processing | `dword_126E4A8 < 30` | `__managed__` requires compute_30 or higher |
| (config) | Declaration processing | Unsupported OS/bitness | `__managed__` not supported for this configuration |

## Function Map

| Address | Name | Lines | Role |
|---|---|---|---|
| `sub_40E0D0` | `apply_nv_managed_attr` | 47 | Attribute handler -- sets flags, validates |
| `sub_40EB80` | `apply_nv_device_attr` | 100 | Device handler (variable path is structurally identical) |
| `sub_413240` | `apply_one_attribute` | 585 | Dispatch -- routes kind `'f'` to `sub_40E0D0` |
| `sub_489000` | `process_file_scope_entities` | 723 | Emits managed RT boilerplate into `.int.c` |
| `sub_4768F0` | `gen_name_ref` | 237 | Access wrapper -- qualified name path |
| `sub_484940` | `gen_variable_name` | 52 | Access wrapper -- direct name path |
| `sub_4DEC90` | `variable_declaration` | 1098 | Declaration processing, constexpr/VLA checks |
| `sub_4DC200` | `mark_defined_variable` | 26 | External linkage warning (error 3648) |
| `sub_72A650` | `record_symbol_reference_full` | ~400 | Cross-space check with managed exemption |
| `sub_6BC890` | `nv_validate_cuda_attributes` | 161 | Post-declaration cross-attribute validation |

## Cross-References

- [Memory Spaces](../cuda/memory-spaces.md) -- bitfield encoding at entity `+148`/`+149`, all four memory space handlers
- [Attribute System Overview](./overview.md) -- dispatch table, attribute kind enum, application pipeline
- [__grid_constant__](./grid-constant.md) -- error 3577 conflict with managed
- [Architecture Feature Gating](../cuda/arch-gating.md) -- compute_30 gate for `__managed__`
- [CUDA Runtime Boilerplate](../output/cuda-runtime.md) -- managed RT emission, lambda stubs, `__cudaPushCallConfiguration`
- [Cross-Space Validation](../cuda/cross-space-validation.md) -- managed exemption in host access checks
- [Entity Node Layout](../structs/entity-node.md) -- byte `+148`/`+149` field definitions
