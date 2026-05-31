# Execution Spaces

Every CUDA function lives in one or more **execution spaces** that govern where the function can run (host CPU, device GPU, or both) and what it can call. cudafe++ encodes execution space as a single-byte bitfield at offset `+182` of the entity (routine) node. This byte is the most frequently tested field in CUDA-specific code paths -- it drives attribute application, redeclaration compatibility, virtual override checking, call-graph validation, IL marking, and code generation selection. Understanding this byte is prerequisite to understanding nearly every CUDA-specific subsystem in cudafe++.

The three CUDA execution-space keywords (`__host__`, `__device__`, `__global__`) are parsed as EDG attributes with internal kind codes `'V'` (86), `'W'` (87), and `'X'` (88) respectively. The attribute dispatch table in `apply_one_attribute` (`sub_413240`) routes each kind to a dedicated handler that validates constraints and sets the bitfield. Functions without any explicit annotation default to `__host__`.

## Key Facts

| Property | Value |
|---|---|
| Source file | `attribute.c` (handlers), `class_decl.c` (redecl/override), `nv_transforms.h` (inline predicates) |
| Bitfield location | Entity node byte at offset `+182` |
| `__global__` handler | `sub_40E1F0` / `sub_40E7F0` (`apply_nv_global_attr`, two variants) |
| `__device__` handler | `sub_40EB80` (`apply_nv_device_attr`) |
| `__host__` handler | `sub_4108E0` (`apply_nv_host_attr`) |
| Virtual override checker | `sub_432280` (`record_virtual_function_override`) |
| Execution space mask table | `dword_E7C760[]` (indexed by space enum) |
| Mask lookup | `sub_6BCF60` (`nv_check_execution_space_mask`) |
| Annotation helper | `sub_41A1F0` (validates HD annotations on types) |
| Relaxed mode flag | `dword_106BFF0` (permits otherwise-illegal space combinations) |
| main() entity pointer | `qword_126EB70` (compared during attribute application) |

## The Execution Space Bitfield (Entity + 182)

Byte offset `+182` within a routine entity node encodes the execution space as a bitfield. Individual bits carry distinct meanings:

```text
Byte at entity+182:

  bit 0  (0x01)   device_capable     Function can execute on device
  bit 1  (0x02)   device_explicit    __device__ was explicitly written
  bit 2  (0x04)   host_capable       Function can execute on host
  bit 3  (0x08)   (reserved)
  bit 4  (0x10)   host_explicit      __host__ was explicitly written
  bit 5  (0x20)   device_annotation  Secondary device flag (used in HD detection)
  bit 6  (0x40)   global_kernel      Function is a __global__ kernel
  bit 7  (0x80)   hd_combined        Combined __host__ __device__ flag
```

### Combined Patterns

The attribute handlers do not set individual bits -- they OR entire patterns into the byte. Each CUDA keyword produces a characteristic bitmask:

| Keyword | OR mask | Resulting byte | Bit breakdown |
|---|---|---|---|
| `__global__` | `0x61` | `0xE1` | device_capable + device_annotation + global_kernel + bit 7 (always set) |
| `__device__` | `0x23` | `0x23` | device_capable + device_explicit + device_annotation |
| `__host__` | `0x15` | `0x15` | device_capable + host_capable + host_explicit |
| `__host__ __device__` | `0x23 \| 0x15` | `0x37` | device_capable + device_explicit + host_capable + host_explicit + device_annotation |
| (no annotation) | none | `0x00` | Implicit `__host__` -- bits remain zero |

The `0x80` bit is set unconditionally by the `__global__` handler. After the `|= 0x61` operation (which sets bit 6), the handler reads the byte back and checks `(byte & 0x40) != 0`. Since bit 6 was just set, this is always true, so `|= 0x80` always executes. Despite the field name `hd_combined` in some tooling, the bit functions as a "has __global__ annotation" marker in practice.

### Why device_capable (bit 0) Appears in __host__

The `__host__` mask `0x15` includes bit 0 (`device_capable`). This is not an error. Bit 0 acts as a "has execution space annotation" marker rather than a strict "runs on device" flag. The actual device-only vs host-only distinction is determined by the two-bit extraction at bits 4-5 (the `0x30` mask), described below.

## Execution Space Classification (0x30 Mask)

The critical two-bit extraction `byte & 0x30` classifies a routine into one of four categories:

```text
(byte & 0x30):
  0x00  ->  no explicit annotation (implicit __host__)
  0x10  ->  __host__ only
  0x20  ->  __device__ only
  0x30  ->  __host__ __device__
```

This extraction is the basis of `nv_is_device_only_routine`, an inline predicate defined in `nv_transforms.h` (line 367). The full check from the decompiled binary is:

```c
// nv_is_device_only_routine (inlined from nv_transforms.h:367)
// entity_sym: the symbol table entry for the routine
// entity_sym+88 -> associated routine entity

__int64 entity = *(entity_sym + 88);
if (!entity)
    internal_error("nv_transforms.h", 367, "nv_is_device_only_routine");

char byte = *(char*)(entity + 182);
bool is_device_only = ((byte & 0x30) == 0x20) && ((byte & 0x60) == 0x20);
```

The double-check `(byte & 0x60) == 0x20` ensures the function is device-only and NOT a `__global__` kernel (which would have bit 6 set, making `byte & 0x60 == 0x60`). This predicate is used in:

- `check_void_return_okay` (`sub_719D20`): suppress missing-return warnings for device-only functions
- `record_virtual_function_override` (`sub_432280`): drive virtual override execution space propagation
- Cross-space call validation: determine whether a call crosses execution space boundaries
- IL keep-in-il marking: identify device-reachable code

### The 0x60 Mask (Kernel vs Device)

A secondary extraction `byte & 0x60` distinguishes kernels from plain device functions:

```text
(byte & 0x60):
  0x00  ->  no device annotation
  0x20  ->  __device__ only (not a kernel)
  0x40  ->  __global__ only (should not occur in isolation)
  0x60  ->  __global__ (which implies __device__)
```

### nv_is_device_only_routine Truth Table

The predicate is inlined from `nv_transforms.h:367` and appears in multiple call sites. Its internal_error guard string `"nv_is_device_only_routine"` appears in `sub_432280` at the source path `EDG_6.6/src/nv_transforms.h`. The complete truth table for all execution space combinations:

| Execution space | byte+182 | byte & 0x30 | byte & 0x60 | Result |
|---|---|---|---|---|
| (none, implicit `__host__`) | `0x00` | `0x00` | `0x00` | false |
| `__host__` | `0x15` | `0x10` | `0x00` | false |
| `__device__` | `0x23` | `0x20` | `0x20` | **true** |
| `__host__ __device__` | `0x37` | `0x30` | `0x20` | false |
| `__global__` | `0xE1` | `0x20` | `0x60` | false |

The `__global__` case is the key distinction: `byte & 0x30` yields `0x20` (same as `__device__`), but `byte & 0x60` yields `0x60` (not `0x20`), so the predicate correctly rejects kernels.

```c
// Full pseudocode for nv_is_device_only_routine
// Inlined at every call site; not a standalone function in the binary.
//
// Input: sym -- a symbol table entry (not the entity itself)
// Output: true if the routine is __device__ only (not __host__, not __global__)
bool nv_is_device_only_routine(symbol *sym) {
    entity *e = sym->entity;            // sym + 88
    if (!e)
        internal_error("nv_transforms.h", 367, "nv_is_device_only_routine");

    char byte = e->byte_182;
    // First check:  bits 4-5 == 0x20 -> has __device__, no __host__
    // Second check: bits 5-6 == 0x20 -> has __device__, no __global__
    return ((byte & 0x30) == 0x20) && ((byte & 0x60) == 0x20);
}
```

### Complete Redeclaration Matrix

The matrix below documents every possible pair of (existing annotation, newly-applied annotation) and the result. Each cell is derived from the three attribute handler functions. "Relaxed" means the outcome changes when `dword_106BFF0` is set.

| Existing \ Applying | `__host__` | `__device__` | `__global__` |
|---|---|---|---|
| **(none)** `0x00` | `0x15` -- OK | `0x23` -- OK | `0xE1` -- OK |
| **`__host__`** `0x15` | `0x15` -- idempotent | `0x37` -- OK (HD) | error 3481 (always: handler checks `byte & 0x10` unconditionally) |
| **`__device__`** `0x23` | `0x37` -- OK (HD) | `0x23` -- idempotent | error 3481 (relaxed: OK) |
| **`__global__`** `0xE1` | error 3481 (always) | error 3481 (relaxed: OK) | `0xE1` -- idempotent |
| **`__host__ __device__`** `0x37` | `0x37` -- idempotent | `0x37` -- idempotent | error 3481 (always: `byte & 0x10` fires) |

The `__global__` column always errors when the existing annotation includes `__host__` (bit 4 = `0x10`), because the `__global__` handler's condition `(v5 & 0x10) != 0` is not guarded by the relaxed-mode flag. The `__device__` column errors on existing `__global__` only when relaxed mode is off, because the `__device__` handler guards its check with `!dword_106BFF0`.

Note that `__global__`'s byte value is `0xE1` (not `0x61`) because the `0x80` bit is always set after `__global__` is applied, as documented above.

## Attribute Application Functions

### apply_nv_global_attr (sub_40E1F0 / sub_40E7F0)

Two nearly identical entry points exist. Both apply `__global__` to a function entity. The variant at `sub_40E7F0` uses a do-while loop for parameter iteration instead of a for loop, but the validation logic is identical. Both variants may exist because EDG generates different code paths for attribute-on-declaration vs attribute-on-definition.

The function performs extensive validation before setting the bitmask:

```c
// Pseudocode for apply_nv_global_attr (sub_40E1F0)
int64_t apply_nv_global_attr(attr_node *a1, entity *a2, char target_kind) {
    if (target_kind != 11)      // only applies to functions
        return a2;

    // Check constexpr lambda with wrong linkage
    if ((a2->qword_184 & 0x800001000000) == 0x800000000000) {
        char *name = get_entity_name(a2, 0);
        error(3469, a1->source_loc, "__global__", name);
        return a2;
    }

    // Static member check
    if ((signed char)a2->byte_176 < 0 && !(a2->byte_81 & 0x04))
        warning(3507, a1->source_loc, "__global__");

    // operator() check
    if (a2->byte_166 == 5)
        error(3644, a1->source_loc);

    // Return type must be void (skip cv-qualifiers)
    type *ret = a2->return_type;    // +144
    while (ret->kind == 12)         // 12 = cv-qualifier wrapper
        ret = ret->next;            // +144
    if (ret->prototype->exception_spec)     // +152 -> +56
        error(3647, a1->source_loc);        // auto/decltype(auto) return

    // Execution space conflict check (single condition with ||)
    char es = a2->byte_182;
    if ((!dword_106BFF0 && (es & 0x60) == 0x20) || (es & 0x10) != 0)
        error(3481, a1->source_loc);
        // Left branch: already __device__ (not relaxed mode) -> conflict
        // Right branch: already __host__ explicit (unconditional) -> conflict

    // Return type must be void (non-constexpr path)
    if (!(a2->byte_179 & 0x10)) {      // not constexpr
        if (a2->byte_191 & 0x01)       // lambda
            error(3506, a1->source_loc);
        else if (!is_void_return(a2))
            error(3505, a1->source_loc);
    }

    // Variadic check
    // ... skip to prototype, check bit 0 of proto+16
    if (proto_flags & 0x01)
        error(3503, a1->source_loc);

    // >>> SET THE BITMASK <<<
    a2->byte_182 |= 0x61;    // bits 0,5,6: device_capable + device_annotation + global_kernel

    // Local function check
    if (a2->byte_81 & 0x04)
        error(3688, a1->source_loc);

    // main() check
    if (a2 == qword_126EB70 && (a2->byte_182 & 0x20))
        error(3538, a1->source_loc);

    // Always set bit 7 after __global__: the check reads the byte AFTER |= 0x61,
    // so bit 6 is always set, making this unconditional.
    if (a2->byte_182 & 0x40)
        a2->byte_182 |= 0x80;

    // Parameter default-init check (device-side warning)
    // ... iterate parameters, warn 3669 if missing defaults
    return a2;
}
```

### apply_nv_device_attr (sub_40EB80)

Handles both variables (`target_kind == 7`) and functions (`target_kind == 11`). For variables, it sets the memory space bitfield at `+148` (bit 0 = `__device__`). For functions, it sets the execution space.

```c
// Variable path (target_kind == 7):
a2->byte_148 |= 0x01;              // __device__ memory space
if (((a2->byte_148 & 0x02) != 0) + ((a2->byte_148 & 0x04) != 0) == 2)
    error(3481, ...);               // both __shared__ (bit 1) AND __constant__ (bit 2) set
if ((signed char)a2->byte_161 < 0)
    error(3482, ...);               // thread_local
if (a2->byte_81 & 0x04)
    error(3485, ...);               // local variable

// Function path (target_kind == 11):
// Same constexpr-lambda check as __global__
if (!dword_106BFF0 && (a2->byte_182 & 0x40))
    error(3481, ...);               // already __global__, now __device__
a2->byte_182 |= 0x23;              // device_capable + device_explicit + device_annotation
if ((a2->byte_81 & 0x04) && (a2->byte_182 & 0x40))
    error(3688, ...);               // local function with __global__
if (a2 == qword_126EB70 && (a2->byte_182 & 0x20))
    error(3538, ...);               // __device__ on main()
```

### apply_nv_host_attr (sub_4108E0)

The simplest of the three. Only applies to functions (target_kind 11). Fewer validation checks than `__global__` or `__device__`.

```c
// Function path (target_kind == 11):
// Same constexpr-lambda check
if (a2->byte_182 & 0x40)
    error(3481, ...);           // already __global__, now __host__
a2->byte_182 |= 0x15;          // device_capable + host_capable + host_explicit
if ((a2->byte_81 & 0x04) && (a2->byte_182 & 0x40))
    error(3688, ...);           // local function
if (a2 == qword_126EB70 && (a2->byte_182 & 0x20))
    error(3538, ...);           // __host__ on main()
```

## Default Execution Space

Functions without any explicit annotation have `byte +182 == 0x00`. This is treated as implicit `__host__`:

- The `0x30` mask yields `0x00`, which the cross-space validator treats identically to `0x10` (explicit `__host__`)
- The function is compiled for the host side only
- It is excluded from device IL during the keep-in-il pass

In JIT compilation mode (`--default-device`), the default flips to `__device__`. This changes which functions are kept in device IL without requiring explicit annotations.

## Execution Space Conflict Detection

The attribute handlers enforce a mutual-exclusion matrix. When a second execution space attribute is applied to a function that already has one, the handler checks for conflicts using error 3481:

| Already set | Applying | Result |
|---|---|---|
| (none) | `__host__` | `0x15` -- accepted |
| (none) | `__device__` | `0x23` -- accepted |
| (none) | `__global__` | `0xE1` -- accepted |
| `__host__` (0x15) | `__device__` | `0x37` -- accepted (HD) |
| `__device__` (0x23) | `__host__` | `0x37` -- accepted (HD) |
| `__host__` (0x15) | `__global__` | error 3481 (always -- `byte & 0x10` is unconditional) |
| `__device__` (0x23) | `__global__` | error 3481 (unless `dword_106BFF0`) |
| `__global__` (0xE1) | `__host__` | error 3481 (always) |
| `__global__` (0xE1) | `__device__` | error 3481 (unless `dword_106BFF0`) |
| `__host__` (0x15) | `__host__` | idempotent OR, no error |
| `__device__` (0x23) | `__device__` | idempotent OR, no error |
| `__global__` (0xE1) | `__global__` | idempotent OR, no error |

The relaxed mode flag `dword_106BFF0` suppresses certain conflicts. When set, combinations that would normally produce error 3481 are silently accepted. This flag corresponds to `--expt-relaxed-constexpr` or similar permissive compilation modes. Note that the relaxed flag does NOT affect the `__host__` -> `__global__` or `__global__` -> `__host__` paths -- these always error because the `__global__` handler checks `byte & 0x10` unconditionally, and the `__host__` handler checks `byte & 0x40` unconditionally.

## Virtual Function Override Checking (sub_432280)

When a derived class overrides a virtual function, cudafe++ must verify execution space compatibility. This check is embedded in `record_virtual_function_override` (`sub_432280`, 437 lines, from `class_decl.c`).

### nv_is_device_only_routine Inline Check

The function first tests whether the overriding function has the `__device__` flag at `+177` bit 4 (`0x10`). If so, and the overridden function does NOT have this flag, execution space propagation occurs:

```c
// Propagation logic (simplified from sub_432280, lines 70-94)
if (overriding->byte_177 & 0x10) {     // overriding is __device__
    if (!(overridden->byte_177 & 0x10)) {   // overridden is NOT __device__
        char es = overridden->byte_182;
        if ((es & 0x30) != 0x20) {          // overridden is not device-only
            overriding->byte_182 |= 0x10;   // propagate __host__ flag
        }
        if (es & 0x20) {                    // overridden has device_annotation
            overriding->byte_182 |= 0x20;   // propagate device_annotation
        }
    }
}
```

### Six Virtual Override Mismatch Errors (3542-3547)

When the overriding function is NOT `__device__`, the checker looks up execution space attributes using `sub_5CEE70` (attribute kind 87 = `__device__`, kind 86 = `__host__`). Based on which attributes are found on the overriding function and the execution space of the overridden function, one of six errors is emitted:

| Error | Overriding has | Overridden space (`byte & 0x30`) | Meaning |
|---|---|---|---|
| 3542 | `__device__` only | `0x00` or `0x10` (host/implicit) | Device override of host virtual |
| 3543 | `__device__` + `__host__` | `0x00` (no annotation) | HD override of implicit-host virtual |
| 3544 | `__device__` + `__host__` | `0x20` (device-only) | HD override of device-only virtual |
| 3545 | no `__device__` | `0x20` (device-only) | Host override of device-only virtual |
| 3546 | no `__device__` | `0x30` (HD) | Host override of HD virtual |
| 3547 | `__device__` only | `0x30` (HD), relaxed mode | Device override of HD virtual (relaxed) |

The errors are emitted via `sub_4F4F10` with severity 8 (hard error). The `dword_106BFF0` relaxed mode flag modulates certain paths: in relaxed mode, some combinations that would otherwise error are accepted or downgraded.

### Decision Logic

```c
// Pseudocode for override mismatch detection (sub_432280, lines 95-188)
char es = overridden->byte_182;
char mask_30 = es & 0x30;
bool has_host_bit = (es & 0x20) != 0;    // device_annotation
bool is_hd = (mask_30 == 0x30);

bool has_device_attr = has_attribute(overriding, 87 /*__device__*/);
bool has_host_attr   = has_attribute(overriding, 86 /*__host__*/);

if (has_device_attr) {
    if (has_host_attr) {
        // Overriding is __host__ __device__
        if (has_host_bit)
            error = 3544;   // HD overrides device-only
        else if (mask_30 != 0x20)
            error = 3543;   // HD overrides implicit-host
    } else {
        // Overriding is __device__ only
        if (!has_host_bit)
            error = 3542;   // device overrides host
        if (is_hd && relaxed_mode)
            error = 3547;   // device overrides HD (relaxed)
    }
} else {
    // Overriding has no __device__
    if (mask_30 == 0x20)
        error = 3545;       // host overrides device-only
    else if (mask_30 == 0x30)
        error = 3546;       // host overrides HD
}
```

## __global__ Function Constraints

The `__global__` handler enforces the strictest constraints of any execution space. A kernel function must satisfy all of the following:

| Constraint | Check | Error |
|---|---|---|
| Must be a function (not variable/type) | `target_kind == 11` | silently ignored if not |
| Not a constexpr lambda with wrong linkage | `(qword_184 & 0x800001000000) != 0x800000000000` | 3469 |
| Not a static member function | `(signed char)byte_176 >= 0 \|\| (byte_81 & 0x04)` | 3507 |
| Not `operator()` | `byte_166 != 5` | 3644 |
| Return type not `auto`/`decltype(auto)` | no exception spec at proto+56 | 3647 |
| No conflicting execution space | see conflict matrix above | 3481 |
| Return type is `void` (non-constexpr) | `is_void_return(a2)` | 3505 / 3506 |
| Not variadic | `!(proto_flags & 0x01)` | 3503 |
| Not a local function | `!(byte_81 & 0x04)` | 3688 |
| Not `main()` | `a2 != qword_126EB70` | 3538 |
| Parameters have default init (device-side) | walk parameter list | 3669 (warning) |

## Execution Space Annotation Helper (sub_41A1F0)

This function validates that type arguments used in `__host__ __device__` or `__device__` template contexts are well-formed. It traverses the type chain (following cv-qualifier wrappers where `kind == 12`), emitting diagnostics:

- **Error 3597**: Type nesting depth exceeds 7 levels
- **Error 3598**: Type is not device-callable (fails `sub_550E50` check)
- **Error 3599**: Type lacks appropriate constructor/destructor for device context

The first argument selects the annotation string: when `a3 == 0`, the string is `"__host__ __device__"`; when `a3 != 0`, it is `"__device__"`.

## Attribute Dispatch (apply_one_attribute)

The central dispatcher `sub_413240` (`apply_one_attribute`, 585 lines) routes attribute kinds to their handlers via a switch statement:

| Kind byte | Decimal | Attribute | Handler |
|---|---|---|---|
| `'V'` | 86 | `__host__` | `sub_4108E0` |
| `'W'` | 87 | `__device__` | `sub_40EB80` |
| `'X'` | 88 | `__global__` | `sub_40E1F0` or `sub_40E7F0` |

Attribute display names are resolved by `sub_40A310` (`attribute_display_name`), which maps the kind byte back to the human-readable CUDA keyword string for use in diagnostic messages.

## Execution Space Mask Table (dword_E7C760)

A lookup table at `dword_E7C760` stores precomputed bitmasks indexed by execution space enum value. The function `sub_6BCF60` (`nv_check_execution_space_mask`) performs `return a1 & dword_E7C760[a2]`, allowing fast bitwise checks of whether a given entity's execution space matches a target space category. This table is used throughout cross-space validation and IL marking.

## Diagnostics Reference

| Error | Severity | Meaning |
|---|---|---|
| 3469 | error | Execution space attribute on constexpr lambda with wrong linkage |
| 3481 | error | Conflicting execution spaces |
| 3482 | error | `__device__` variable with `thread_local` storage |
| 3485 | error | `__device__` attribute on local variable |
| 3503 | error | `__global__` function cannot be variadic |
| 3505 | error | `__global__` return type must be `void` (non-constexpr path) |
| 3506 | error | `__global__` return type must be `void` (constexpr/lambda path) |
| 3507 | warning | `__global__` on static member function |
| 3538 | error | Execution space attribute on `main()` |
| 3577 | error | `__device__` variable with `constexpr` and conflicting memory space |
| 3542 | error | Virtual override: `__device__` overrides host |
| 3543 | error | Virtual override: `__host__ __device__` overrides implicit-host |
| 3544 | error | Virtual override: `__host__ __device__` overrides device-only |
| 3545 | error | Virtual override: host overrides device-only |
| 3546 | error | Virtual override: host overrides `__host__ __device__` |
| 3547 | error | Virtual override: `__device__` overrides HD (relaxed mode) |
| 3597 | error | Type nesting too deep for execution space annotation |
| 3598 | error | Type not callable in target execution space |
| 3599 | error | Type lacks device-compatible constructor/destructor |
| 3644 | error | `__global__` on `operator()` |
| 3647 | error | `__global__` return type cannot be `auto`/`decltype(auto)` |
| 3669 | warning | `__global__` parameter without default initializer (device-side) |
| 3688 | error | Execution space attribute on local function |

## Function Map

| Address | Identity | Lines | Source |
|---|---|---|---|
| `sub_40A310` | `attribute_display_name` | 83 | attribute.c |
| `sub_40E1F0` | `apply_nv_global_attr` (variant 1) | 89 | attribute.c |
| `sub_40E7F0` | `apply_nv_global_attr` (variant 2) | 86 | attribute.c |
| `sub_40EB80` | `apply_nv_device_attr` | 100 | attribute.c |
| `sub_4108E0` | `apply_nv_host_attr` | 31 | attribute.c |
| `sub_413240` | `apply_one_attribute` (dispatch) | 585 | attribute.c |
| `sub_41A1F0` | execution space annotation helper | 82 | class_decl.c |
| `sub_432280` | `record_virtual_function_override` | 437 | class_decl.c |
| `sub_6BCF60` | `nv_check_execution_space_mask` | 7 | nv_transforms.c |
| `sub_719D20` | `check_void_return_okay` | 271 | statements.c |

## Cross-References

- [Memory Spaces](../cuda/memory-spaces.md) -- variable-side `__device__`/`__shared__`/`__constant__` at entity+148
- [Cross-Space Validation](../cuda/cross-space-validation.md) -- call-graph enforcement of execution space rules
- [Device/Host Separation](../cuda/device-host-separation.md) -- IL marking driven by execution space
- [Kernel Stubs](../cuda/kernel-stubs.md) -- host-side stub generation for `__global__` functions
- [Entity Node Layout](../structs/entity-node.md) -- full byte map of the entity structure
- [Virtual Override Matrix](../reference/virtual-override-matrix.md) -- detailed 6-error mismatch table
- [JIT Mode](../cuda/jit-mode.md) -- `--default-device` flag that changes implicit execution space
