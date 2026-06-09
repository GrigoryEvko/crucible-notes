# Memory Spaces

Every CUDA variable that resides in GPU memory belongs to one of four **memory spaces**: `__device__` (global memory), `__shared__` (per-block scratchpad), `__constant__` (read-only broadcast memory), or `__managed__` (unified memory). cudafe++ encodes memory space as a two-byte bitfield at offsets `+148` and `+149` of the variable entity node. These two bytes are the variable-side analog of the execution space byte at `+182` used for functions — the two systems are complementary but independent.

The memory space bitfield passes through three processing stages. First, attribute handlers in `attribute.c` set the appropriate bits and enforce mutual exclusion constraints (no `__shared__` + `__constant__`, no `thread_local`, no `grid_constant` conflict). Second, declaration processing in `decls.c` applies additional validation: VLA restrictions for `__shared__`, constexpr and external-linkage restrictions for `__constant__`/`__device__`, and structured binding constraints for all spaces. Third, symbol reference recording in `symbol_ref.c` checks whether host code illegally accesses device-side variables at reference time.

Memory spaces apply exclusively to variables (entity kind 7). `__shared__` and `__constant__` have no function-side meaning — only `__device__` (kind `'W'`, 87) doubles as a function execution space attribute.

## Key Facts

| Property | Value |
|---|---|
| Memory space offset | Entity node byte `+148` (3-bit bitfield) |
| Extended space offset | Entity node byte `+149` (1 bit for `__managed__`) |
| `__device__` handler | `sub_40EB80` (`apply_nv_device_attr`, 100 lines, `attribute.c`) |
| `__managed__` handler | `sub_40E0D0` (`apply_nv_managed_attr`, 47 lines, `attribute.c:10523`) |
| `__shared__` handler | Kind `'Z'` (90), not individually decompiled; sets `+148 \|= 0x02` |
| `__constant__` handler | Kind `'['` (91), not individually decompiled; sets `+148 \|= 0x04` |
| Declaration processor | `sub_4DEC90` (`variable_declaration`, 1098 lines, `decls.c`) |
| Variable declaration | `sub_4CA6C0` (`decl_variable`, 1090 lines, `decls.c:7730`) |
| Variable fixup | `sub_4CC150` (`cuda_variable_fixup`, 120 lines, `decls.c`) |
| Defined-variable check | `sub_4DC200` (`mark_defined_variable`, 26 lines, `decls.c`) |
| Cross-space reference checker | `sub_72A650` / `sub_72B510` (`record_symbol_reference_full`, `symbol_ref.c`) |
| Device-var-in-host checker | `sub_6BCF10` (`nv_check_device_variable_in_host`, `nv_transforms.c`) |
| Post-validation | `sub_6BC890` (`nv_validate_cuda_attributes`, 161 lines, `nv_transforms.c`) |
| Attribute kind codes | `'W'`=87 (`__device__`), `'Z'`=90 (`__shared__`), `'['`=91 (`__constant__`), `'f'`=102 (`__managed__`) |

## The Memory Space Bitfield (Entity +148 / +149)

### Byte +148: Primary Memory Space

```text
Byte at entity+148:

  bit 0  (0x01)   __device__       Variable in device global memory
  bit 1  (0x02)   __shared__       Variable in per-block shared memory
  bit 2  (0x04)   __constant__     Variable in constant memory
  bit 3  (0x08)   type_member      Set when variable inherits space from type context
  bit 4  (0x10)   device_at_file   __device__ at file scope (no enclosing function)
  bit 7  (0x80)   weak_odr         Set by apply_nv_weak_odr_attr (sub_40AD80)
```

Bits 3, 4, and 7 are set by `decl_variable` (`sub_4CA6C0`) during declaration processing, not by the attribute handlers. Bit 3 is set via `*(_BYTE *)(v33 + 148) |= 8u` when the variable inherits its memory space from a type context (such as a static member of a class with a device annotation). Bit 4 is set via `*(_BYTE *)(v43 + 148) = v73 | 0x10` when a `__device__` variable is declared at file scope (`dword_126C5D8 == -1`, meaning no enclosing function).

### Byte +149: Extended Memory Space

```text
Byte at entity+149:

  bit 0  (0x01)   __managed__    Unified memory (host + device accessible)
  bits 1-7        (reserved)
```

### Word-Level Access

Some validation code reads bytes `+148` and `+149` together as a 16-bit word. The `__grid_constant__` conflict check in `apply_nv_managed_attr` tests:

```c
// sub_40E0D0, line 26 (apply_nv_managed_attr)
if ( (a2[164] & 4) != 0 && (*((_WORD *)a2 + 74) & 0x102) != 0 )
```

Here `(_WORD *)(a2 + 148)` (offset 74 in 16-bit units) is tested against `0x0102`. In little-endian layout, `0x0102` means byte `+148` bit 1 (`__shared__`) OR byte `+149` bit 0 (`__managed__`). This catches the case where a `__grid_constant__` parameter also carries `__shared__` or `__managed__`.

### Mutual Exclusion

In valid CUDA programs, at most one of `__device__`, `__shared__`, and `__constant__` should be set. However, `__managed__` always implies `__device__` — the handler sets both `+149` bit 0 and `+148` bit 0. The validation logic permits `__device__ + __managed__` but rejects combinations like `__shared__ + __constant__`.

The mutual exclusion check appears identically in both `apply_nv_managed_attr` and `apply_nv_device_attr`:

```c
// From sub_40EB80 (apply_nv_device_attr), variable path:
v9 = *(_BYTE *)(a2 + 148) | 1;     // set __device__ bit
*(_BYTE *)(a2 + 148) = v9;
if ( ((v9 & 2) != 0) + ((v9 & 4) != 0) == 2 )
    sub_4F81B0(3481, a1 + 56);      // error: conflicting spaces
```

The expression `((v9 & 2) != 0) + ((v9 & 4) != 0) == 2` is true only when **both** `__shared__` (bit 1) and `__constant__` (bit 2) are set simultaneously. This means:
- `__device__ + __shared__` is **allowed** (the bits coexist)
- `__device__ + __constant__` is **allowed**
- `__shared__ + __constant__` triggers **error 3481**

## Attribute Handlers

### apply_nv_managed_attr — sub_40E0D0

The `__managed__` handler is the simplest and most thoroughly documented. It demonstrates the full validation pattern that all memory space handlers share.

**Entry point**: Called from `apply_one_attribute` (`sub_413240`) when attribute kind is `'f'` (102).

**Decompiled logic** (47 lines, `attribute.c:10523`):

```c
// sub_40E0D0 -- apply_nv_managed_attr
// a1: attribute node, a2: entity node, a3: entity kind

// Gate: only applies to variables
if ( a3 != 7 )
    internal_error("attribute.c", 10523, "apply_nv_managed_attr");

// Step 1: Set managed flag AND device flag
v3 = a2[148];           // save old memory space byte
a2[149] |= 1;           // set __managed__ bit
a2[148] = v3 | 1;       // set __device__ bit (managed implies device)

// Step 2: Mutual exclusion check
if ( ((v3 & 2) != 0) + ((v3 & 4) != 0) == 2 )
    error(3481, ...);    // __shared__ + __constant__ conflict

// Step 3: Thread-local check
if ( (char)a2[161] < 0 )
    error(3482, ...);    // __managed__ on thread_local

// Step 4: Local variable check
if ( (a2[81] & 4) != 0 )
    error(3485, ...);    // __managed__ on local variable

// Step 5: __grid_constant__ conflict
if ( (a2[164] & 4) != 0 && (*(WORD*)(a2 + 148) & 0x102) != 0 )
{
    // Determine which space string to display
    v4 = a2[148];
    v5 = "__constant__";
    if ( (v4 & 4) == 0 ) {
        v5 = "__managed__";
        if ( (a2[149] & 1) == 0 ) {
            v5 = "__shared__";
            if ( (v4 & 2) == 0 ) {
                v5 = "__device__";
                if ( (v4 & 1) == 0 )
                    v5 = "";
            }
        }
    }
    error(3577, ..., v5);   // incompatible with __grid_constant__
}
```

The space-name selection cascade (`__constant__` > `__managed__` > `__shared__` > `__device__` > empty) is used in error messages to show which memory space conflicts with `__grid_constant__`. The cascade tests bits in priority order, matching the most "restrictive" space first.

### apply_nv_device_attr — sub_40EB80

The `__device__` handler is dual-purpose: it handles **both** variables (`a3 == 7`) and functions (`a3 == 11`).

**Entry point**: Called from `apply_one_attribute` when attribute kind is `'W'` (87).

**Variable path** (entity kind 7):

```c
// sub_40EB80, variable branch
*(_BYTE *)(a2 + 148) |= 1;          // set __device__ bit

// Validation (identical to __managed__):
// 1. Error 3481 if __shared__ + __constant__ both set
// 2. Error 3482 if thread_local (byte +161 bit 7)
// 3. Error 3485 if local variable (byte +81 bit 2)
// 4. Error 3577 if __grid_constant__ conflict
```

**Function path** (entity kind 11):

```c
// sub_40EB80, function branch
// Check: not an implicitly-deleted function
if ( (*(_QWORD *)(a2 + 184) & 0x800001000000LL) != 0x800000000000LL
     || (*(_BYTE *)(a2 + 176) & 2) != 0 )
{
    // Conflict with __global__
    if ( !dword_106BFF0 && (*(_BYTE *)(a2 + 182) & 0x40) != 0 )
        error(3481, ...);

    *(_BYTE *)(a2 + 182) |= 0x23;    // set device execution space

    // Local function with __global__ conflict
    if ( (*(_BYTE *)(a2 + 81) & 4) != 0 && (*(_BYTE *)(a2 + 182) & 0x40) != 0 )
        error(3688, ...);

    // __device__ on main()
    if ( a2 == qword_126EB70 && (*(_BYTE *)(a2 + 182) & 0x20) != 0 )
        warning(3538, ...);
}
else
{
    // Implicitly-deleted function: just warn
    v14 = get_entity_display_name(a2);
    error(3469, ..., "__device__", v14);
}

// Check function parameters for missing default initializers
// (error 3669 for parameters without defaults in device context)
```

The function path is documented in [Execution Spaces](./execution-spaces.md) — here we focus on the variable path.

### __shared__ and __constant__ Handlers

The `__shared__` and `__constant__` attribute handlers are dispatched through `apply_one_attribute` (`sub_413240`) when attribute kind codes `'Z'` (90) and `'['` (91) are encountered. Their variable-path logic mirrors `__device__` and `__managed__`:

| Step | `__shared__` ('Z') | `__constant__` ('[') |
|---|---|---|
| Set memory space bit | `byte +148 \|= 0x02` | `byte +148 \|= 0x04` |
| Mutual exclusion (3481) | Check `__constant__` bit (bit 2) | Check `__shared__` bit (bit 1) |
| Thread-local check (3482) | Yes | Yes |
| Local variable check (3485) | Yes | Yes |
| `__grid_constant__` conflict (3577) | Yes | Yes |

The `__shared__` and `__constant__` keywords apply **only** to variables (kind 7). Unlike `__device__`, they do not have a function-path branch — there is no `__shared__` or `__constant__` function execution space.

## Variable Declaration Processing

### sub_4DEC90 — variable_declaration

The top-level declaration processor (`decls.c`) performs additional CUDA-specific validation after attribute handlers have set the memory space bits. This function is 1098 lines and handles both normal variable declarations and static data member definitions.

**CUDA-specific checks** in `variable_declaration`:

| Error | Condition | Description |
|---|---|---|
| 149 | Memory space attribute at illegal scope | CUDA storage class at namespace scope (specific scenarios) |
| 892 | `auto` with `__constant__` | `auto`-typed `__constant__` variable |
| 893 | `auto` with CUDA attribute | `auto`-typed variable with other CUDA memory space |
| 3510 | `__shared__` with VLA | `__shared__` variable with variable-length array type |
| 3566 | `__constant__` + constexpr + auto | `__constant__` constexpr with auto deduction |
| 3567 | CUDA variable with VLA | CUDA memory-space variable with VLA type |
| 3568 | `__constant__` + constexpr | `__constant__` combined with constexpr |
| 3578 | CUDA attribute in discarded branch | CUDA attribute on variable in constexpr-if discarded branch |
| 3579 | CUDA attribute + structured binding | CUDA attribute at namespace scope with structured binding |
| 3580 | CUDA attribute on VLA | CUDA attribute on variable-length array |

**Memory space string selection** (used in error messages):

```c
// sub_4DEC90, line ~357: selecting display name for the memory space
v50 = "__constant__";
if ( (v49 & 4) == 0 ) {
    v50 = "__managed__";
    if ( (*(_BYTE *)(v15 + 149) & 1) == 0 ) {
        v50 = "__host__ __device__" + 9;   // pointer arithmetic: = "__device__"
        if ( (v49 & 2) != 0 )
            v50 = "__shared__";
    }
}
```

The string `"__device__"` is produced by taking the string `"__host__ __device__"` and advancing by 9 bytes, skipping past `"__host__ "`. This is a binary-level optimization — the compiler shares string storage between the combined `"__host__ __device__"` literal and the standalone `"__device__"` reference.

### sub_4CA6C0 — decl_variable

The core variable declaration function (1090 lines, `decls.c:7730`) handles CUDA memory space propagation during symbol table entry creation. Key behaviors:

**Storage class mapping**: When declaration state byte at offset `+269` equals 5, it indicates a CUDA memory space storage class. The function performs a scope walk to determine the correct namespace scope for the variable. If a prior declaration exists at the same scope (`dword_126C5DC == dword_126C5B4`), the CUDA storage class is reset to allow redeclaration.

**Scope walk**: Traverses the scope chain (784-byte scope entries at `qword_126C5E8`, indexed by `dword_126C5E4`) upward through class scopes (scope_kind 4) and template scopes (bit `0x20` at scope entry `+9`), until reaching a non-class, non-template scope. This determines whether the variable is at namespace scope, class scope, or block scope.

**Error 3483 — memory space in non-device function**: When a variable with a device memory space bit (`+148` bit 0 set) is declared inside a function body, and the enclosing routine is NOT device-only (`+182 & 0x30 != 0x20`), the function emits error 3483 with the storage kind and space name:

```c
// From sub_4CA6C0, ~line 886-910
if (!at_namespace_scope) {
    char space = entity->byte_148;
    if (storage_class != 1 && (space & 0x01)) {
        routine_descriptor = qword_126C5D0;
        if (routine_descriptor) {
            entity_ptr = *(routine_descriptor + 32);
            if (entity_ptr && (entity_ptr[182] & 0x30) != 0x20) {
                const char *name = get_space_name(entity);  // priority cascade
                const char *kind = (storage_class == 2) ? "a static" : "an automatic";
                error(3483, source_loc, kind, name);
            }
        }
    }
}
```

**File-scope device flag**: When a `__device__` variable is at file scope (`dword_126C5D8 == -1`), the function sets bit 4 of `+148`:

```c
if ((entity->byte_148 & 0x01) && dword_126C5D8 == -1)
    entity->byte_148 |= 0x10;   // bit 4: device_at_file_scope
```

**Redeclaration checking**: When a variable is redeclared, the function compares memory space encoding at offset `+136` (the attribute byte) between the existing and new entity. Error 1306 is emitted for mismatched CUDA memory spaces.

**Memory space propagation**: Calls `sub_4C4750` (`set_variable_attributes`) for final attribute propagation, and `sub_4CA480` (`check_variable_redeclaration`) for prior-declaration compatibility.

### sub_4DC200 — mark_defined_variable

Post-declaration validation for device-memory variables with external linkage (26 lines):

```c
// sub_4DC200 -- mark_defined_variable (decompiled)
void mark_defined_variable(entity_t *a1, int a2) {
    if (a1[164] & 0x10) {   // already marked as defined
        if (!dword_106BFD0                    // cross-space checking not overridden
            && (a1[148] & 3) == 1             // __device__ set, __shared__ NOT set
            && !is_compiler_generated(a1)     // not compiler-generated
            && (a1[80] & 0x70) != 0x10)       // not anonymous
        {
            warning(3648, a1 + 64);           // external linkage warning
        }
    } else if (!a2 && (*(byte*)(*(qword*)a1 + 81) & 2)) {
        error(1655, ...);   // tentative definition of constexpr
    } else {
        // Same 3648 check on first definition
        if (!dword_106BFD0 && (a1[148] & 3) == 1 && ...)
            warning(3648, a1 + 64);
        a1[164] |= 0x10;   // mark as defined
    }
}
```

The condition `(a1[148] & 3) == 1` tests that bit 0 (`__device__`) is set AND bit 1 (`__shared__`) is NOT set. This catches `__device__` variables (including `__device__ __constant__` and `__device__ __managed__`, since those have bit 0 set) but excludes `__shared__` variables (which have bit 1 set). The check is NOT about `__constant__` alone — a pure `__constant__` variable (only bit 2 set, value 0x04) would yield `(0x04 & 3) == 0`, failing the test. The p1.06 report's characterization of error 3648 as "__constant__ with external linkage" is misleading; the actual condition is "device-accessible (non-shared) variable with external linkage."

### sub_4CC150 — cuda_variable_fixup

Called from `variable_declaration` after CUDA constexpr-if detection. This function:

- Manipulates variable entity fields at offset `+148` (memory space) and `+162` (visibility flags)
- Adjusts scope chains using the 784-byte scope entry array
- Creates new type entries for CUDA-specific variable rewriting

## Bit Assignment Resolution

Two sweep reports provided conflicting bit assignments for byte `+148`:

| Source | bit 0 | bit 1 | bit 2 |
|---|---|---|---|
| p1.01 (attribute.c handlers) | `__device__` | `__shared__` | `__constant__` |
| p1.06 (decls.c) | `__constant__` | `__shared__` | `__managed__` |

The decompiled code resolves this definitively in favor of the p1.01 assignment. Two independent functions confirm it:

1. `sub_40E0D0` (`apply_nv_managed_attr`) sets `a2[149] |= 1` (managed at `+149`) and `a2[148] = v3 | 1` (device at `+148` bit 0). The subsequent conflict check tests `(v3 & 2)` for `__shared__` and `(v3 & 4)` for `__constant__`.

2. `sub_40EB80` (`apply_nv_device_attr`) sets `*(_BYTE *)(a2 + 148) | 1` (device at `+148` bit 0), then uses the identical conflict test `((v9 & 2) != 0) + ((v9 & 4) != 0) == 2`.

The canonical encoding is:

```text
Byte +148:  bit 0 = __device__,  bit 1 = __shared__,  bit 2 = __constant__
Byte +149:  bit 0 = __managed__
```

The p1.06 report's alternative encoding is an analysis error, caused by `mark_defined_variable` (`sub_4DC200`) testing `+148 & 3 == 1` in the context of error 3648. That test checks for `__device__` set (bit 0) without `__shared__` (bit 1) — not for `__constant__` at bit 0. The error was then characterized as "__constant__ with external linkage" based on the error message text rather than the actual bit test.

## Validation Constraints

### __managed__ Constraints

`__managed__` has the strictest requirements among memory space annotations. All five checks occur in `apply_nv_managed_attr` (`sub_40E0D0`):

| Constraint | Binary test | Error | Description |
|---|---|---|---|
| Variables only | `a3 != 7` | internal_error | `__managed__` can only apply to variables, not functions or types |
| No shared+constant | `((old & 2) != 0) + ((old & 4) != 0) == 2` | 3481 | Both `__shared__` and `__constant__` already set |
| Not thread-local | `(signed char)byte+161 < 0` | 3482 | Bit 7 of `+161` = thread_local storage |
| Not reference/local | `byte+81 & 4` | 3485 | Bit 2 of `+81` = reference type or local variable |
| Not grid_constant | `byte+164 & 4` and word `+148 & 0x0102` | 3577 | `__grid_constant__` parameter with managed or shared space |

The `__managed__` keyword requires compute capability >= 3.0. This is verified at compilation time via version threshold comparisons (`qword_126EF90 > 0x78B3`, where `0x78B3` = 30899 in the CUDA version encoding scheme). The specific error code for architecture-too-low is not captured in the decompiled attribute handler.

### __shared__ Constraints

`__shared__` variables have restrictions enforced across multiple functions:

| Constraint | Where | Error | Description |
|---|---|---|---|
| No VLA type | `sub_4DEC90` | 3510 | `__shared__` variable cannot have variable-length array type |
| No VLA (general) | `sub_4DEC90` | 3580 | CUDA memory-space attribute on variable-length array |
| Not thread-local | Attribute handler | 3482 | `__shared__` on `thread_local` variable |
| Not local (non-block) | Attribute handler | 3485 | Cannot appear on local variables outside device function scope |
| No grid_constant | Attribute handler | 3577 | Incompatible with `__grid_constant__` parameter |

### __constant__ Constraints

`__constant__` carries additional restrictions related to constexpr and type:

| Constraint | Where | Error | Description |
|---|---|---|---|
| No constexpr | `sub_4DEC90` | 3568 | `__constant__` combined with constexpr (when managed+device bits also set) |
| No constexpr+auto | `sub_4DEC90` | 3566 | Constexpr with const-qualified type |
| No VLA type | `sub_4DEC90` | 3567 | CUDA memory-space variable with VLA type |
| Not thread-local | Attribute handler | 3482 | `__constant__` on `thread_local` variable |
| Not local | Attribute handler | 3485 | Cannot appear on local variables |
| No grid_constant | Attribute handler | 3577 | Incompatible with `__grid_constant__` parameter |

Note: Error 3648 (external linkage warning) is emitted by `sub_4DC200` but the condition tests `(byte+148 & 3) == 1`, which checks for `__device__` set without `__shared__` — not specifically `__constant__`. The check applies to any device-accessible non-shared variable, including `__device__`, `__device__ __constant__`, and `__device__ __managed__`.

## Cross-Space Variable Access Checking

When host code references a device-side variable, the symbol reference recorder emits diagnostics. This checking occurs in `record_symbol_reference_full` (`sub_72A650` / `sub_72B510`, `symbol_ref.c`) and is gated by global flags `dword_106BFD0` and `dword_106BFCC`.

### Gate Logic

```text
1. Is cross-space checking enabled?
   → dword_106BFD0 != 0 OR dword_106BFCC != 0

2. Is the referenced entity a variable (kind == 7)?
   → Yes: proceed to nv_check_device_var_ref_in_host
   → No (kind 10/11/20 -- function): check nv_check_host_var_ref_in_device

3. Get current routine from scope stack (dword_126C5D8)
4. Check routine execution space at +182 (0x30 mask):
   → 0x00 or 0x10 (host): emit device-var-in-host errors
   → 0x20 (device): emit host-var-in-device errors
```

### Device Variable Referenced from Host Code

The `nv_check_device_var_ref_in_host` path (assert string at `symbol_ref.c:2347`) checks memory space bits and produces specific errors based on which space the variable occupies:

| Error | Condition | Description |
|---|---|---|
| 3548 | Variable has `__shared__` or `__constant__` (byte+148 bits 1-2) | Reference to `__shared__` / `__constant__` variable from host code |
| 3549 | Variable has `__constant__` and reference is in initializer context (ref_kind bit 4) | Initializer referencing device memory variable from host |
| 3550 | Variable has `__shared__` and reference is a write (ref_kind bit 1) | Write to `__shared__` variable from host code |
| 3486 | Via `sub_6BCF10` — complex linkage check (`+176 & 0x200000000002000`, `+166 == 5`, `+168 in [1,4]`) | Illegal device variable reference from host (operator function context) |

### Host Variable Referenced from Device Code

The `nv_check_host_var_ref_in_device` path (assert string at `symbol_ref.c:2390`) handles the reverse direction:

| Error | Condition | Description |
|---|---|---|
| 3623 | Device-only function referenced outside device context | Use of `__device__`-only function outside the bodies of device functions |

The error 3623 has two context strings:
- `"outside the bodies of device functions"` — general case
- `"from a constexpr or consteval __device__ function"` — constexpr context

### Relaxation: dword_106BF40

When `dword_106BF40` is set (corresponding to `--expt-relaxed-constexpr`), and the current routine at `+182` has the device annotation pattern (`& 0x30 == 0x20`) with `+177` bit 1 set (explicit `__device__`), cross-space variable access checks are suppressed. This allows constexpr device functions to reference host variables during constant evaluation.

## Host Reference Arrays

When the backend emits host-side code, variables marked with `__device__`, `__shared__`, or `__constant__` are registered in ELF section arrays so the CUDA runtime can discover them at load time. The emission function `sub_6BCF80` (`nv_emit_host_reference_array`) writes entries into six separate sections:

| Section | Array Name | Memory Space | Linkage |
|---|---|---|---|
| `.nvHRDE` | `hostRefDeviceArrayExternalLinkage` | `__device__` | External |
| `.nvHRDI` | `hostRefDeviceArrayInternalLinkage` | `__device__` | Internal |
| `.nvHRCE` | `hostRefConstantArrayExternalLinkage` | `__constant__` | External |
| `.nvHRCI` | `hostRefConstantArrayInternalLinkage` | `__constant__` | Internal |
| `.nvHRKE` | `hostRefKernelArrayExternalLinkage` | `__global__` (kernel) | External |
| `.nvHRKI` | `hostRefKernelArrayInternalLinkage` | `__global__` (kernel) | Internal |

Each array entry contains the mangled name of the device symbol as a byte array:

```c
extern "C" {
    extern __attribute__((section(".nvHRDE")))
    __attribute__((weak))
    const unsigned char hostRefDeviceArrayExternalLinkage[] = {
        /* mangled name bytes */ 0x0
    };
}
```

Six global lists (at addresses `unk_1286780` through `unk_12868C0`) accumulate symbols during compilation, one per section type. Note that `__shared__` variables do NOT get host reference arrays — they have no host-visible address.

## Redeclaration Compatibility

When a variable is redeclared, `decl_variable` (`sub_4CA6C0`) compares the memory space bits between the prior declaration and the new one. Error 1306 is emitted for mismatched CUDA memory spaces:

```text
Error 1306: CUDA memory space mismatch on redeclaration
```

The comparison tests byte `+148` of both the existing entity and the new declaration's computed attributes. The CUDA memory space acts as an implicit storage class — storage class value 5 in the declaration state (offset 269) indicates a CUDA-specific storage class that requires special scope-walking behavior.

## String Table Usage

The memory space keywords appear in the binary's string table and are referenced by error message formatting code:

| String | Usage |
|---|---|
| `"__constant__"` | Error messages for `__constant__` constraints, space name display |
| `"__managed__"` | Error messages for `__managed__` constraints |
| `"__device__"` | Obtained via `"__host__ __device__" + 9` (pointer arithmetic), or direct literal |
| `"__shared__"` | Error messages for `__shared__` constraints |
| `"__host__ __device__"` | Combined string; `+9` yields `"__device__"` |

The pointer-arithmetic trick for `"__device__"` appears in both `sub_4DEC90` (`variable_declaration`) and error message formatting throughout the attribute handlers. It saves binary space by reusing the combined `"__host__ __device__"` string constant.

## Error Code Summary

### Attribute Application Errors

| Error | Severity | Description |
|---|---|---|
| 3481 | Error | Conflicting CUDA memory spaces (`__shared__` + `__constant__` simultaneously) |
| 3482 | Error | CUDA memory space attribute on `thread_local` variable |
| 3485 | Error | CUDA memory space attribute on local variable |
| 3577 | Error | Memory space incompatible with `__grid_constant__` parameter |

### Declaration Processing Errors

| Error | Severity | Description |
|---|---|---|
| 149 | Error | Illegal CUDA storage class at namespace scope |
| 892 | Error | `auto` type with `__constant__` variable |
| 893 | Error | `auto` type with CUDA memory space variable |
| 1306 | Error | CUDA memory space mismatch on redeclaration |
| 3483 | Error | Memory space qualifier on automatic/static variable in non-device function |
| 3510 | Error | `__shared__` variable with variable-length array |
| 3566 | Error | `__constant__` with constexpr and auto deduction |
| 3567 | Error | CUDA variable with VLA type |
| 3568 | Error | `__constant__` combined with constexpr |
| 3578 | Error | CUDA attribute in constexpr-if discarded branch |
| 3579 | Error | CUDA attribute at namespace scope with structured binding |
| 3580 | Error | CUDA attribute on variable-length array |
| 3648 | Warning | Device-accessible (non-shared) variable with external linkage |

### Cross-Space Reference Errors

| Error | Severity | Description |
|---|---|---|
| 3486 | Error | Illegal device variable reference from host (operator function context) |
| 3548 | Error | Reference to `__shared__` / `__constant__` variable from host code |
| 3549 | Error | Initializer referencing device memory variable from host |
| 3550 | Error | Write to `__shared__` variable from host code |
| 3623 | Error | Use of `__device__`-only function outside device context |

## Global State Variables

| Variable | Type | Description |
|---|---|---|
| `dword_126EFA8` | int | CUDA mode flag (nonzero when compiling CUDA) |
| `dword_126EFB4` | int | CUDA dialect (2 = CUDA C++) |
| `dword_126EFAC` | int | Extended CUDA features flag |
| `dword_126EFA4` | int | CUDA version-check control |
| `qword_126EF98` | int64 | CUDA version threshold (hex: `0x9E97` = 40599, `0x9D6C`, etc.) |
| `qword_126EF90` | int64 | CUDA version threshold (hex: `0x78B3` = 30899 for compute_30) |
| `dword_106BFD0` | int | Enable cross-space reference checking (primary) |
| `dword_106BFCC` | int | Enable cross-space reference checking (secondary) |
| `dword_106BF40` | int | Allow `__device__` function refs in host (`--expt-relaxed-constexpr`) |
| `dword_106BFF0` | int | Relaxed execution space mode (permits otherwise-illegal combos) |
| `qword_126EB70` | ptr | Entity pointer for `main()` (prevents `__device__` on main) |
| `qword_126C5E8` | ptr | Scope stack base pointer (784-byte entries) |
| `dword_126C5E4` | int | Current scope stack top index |
| `dword_126C5D8` | int | Current function scope index (-1 if none) |

## Function Map

| Address | Identity | Size | Source |
|---|---|---|---|
| `sub_40AD80` | `apply_nv_weak_odr_attr` | 0.2 KB | `attribute.c:10497` |
| `sub_40E0D0` | `apply_nv_managed_attr` | 0.4 KB | `attribute.c:10523` |
| `sub_40E1F0` | `apply_nv_global_attr` (variant 1) | 0.9 KB | `attribute.c` |
| `sub_40E7F0` | `apply_nv_global_attr` (variant 2) | 0.9 KB | `attribute.c` |
| `sub_40EB80` | `apply_nv_device_attr` | 1.0 KB | `attribute.c` |
| `sub_4108E0` | `apply_nv_host_attr` | 0.3 KB | `attribute.c` |
| `sub_413240` | `apply_one_attribute` (dispatch) | 5.9 KB | `attribute.c` |
| `sub_413ED0` | `apply_attributes_to_entity` | 4.9 KB | `attribute.c` |
| `sub_40A310` | `attribute_display_name` | 0.6 KB | `attribute.c:1307` |
| `sub_4CA6C0` | `decl_variable` | 11 KB | `decls.c:7730` |
| `sub_4CC150` | `cuda_variable_fixup` | 1.2 KB | `decls.c:20654` |
| `sub_4DC200` | `mark_defined_variable` | 0.3 KB | `decls.c` |
| `sub_4DEC90` | `variable_declaration` | 11 KB | `decls.c:12956` |
| `sub_6BC890` | `nv_validate_cuda_attributes` | 1.6 KB | `nv_transforms.c` |
| `sub_6BCF10` | `nv_check_device_variable_in_host` | 0.2 KB | `nv_transforms.c` |
| `sub_6BCF80` | `nv_emit_host_reference_array` | 0.8 KB | `nv_transforms.c` |
| `sub_72A650` | `record_symbol_reference_full` (6-arg) | 6.6 KB | `symbol_ref.c` |
| `sub_72B510` | `record_symbol_reference_full` (4-arg) | 7.3 KB | `symbol_ref.c` |

## See Also

- [Execution Spaces](./execution-spaces.md) — function-level `__host__` / `__device__` / `__global__` encoding at entity `+182`
- [Cross-Space Call Validation](./cross-space-validation.md) — full cross-space call checking algorithm
- [Entity Node Layout](../structs/entity-node.md) — complete entity node offset map
- [\_\_managed\_\_ Variables](../attributes/managed-variables.md) — `__managed__` attribute system details
- [\_\_grid\_constant\_\_](../attributes/grid-constant.md) — `__grid_constant__` parameter attribute
- [Host Reference Arrays](../output/host-reference-arrays.md) — runtime device symbol discovery
