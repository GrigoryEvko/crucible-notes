# Cross-Space Call Validation

CUDA's execution model partitions code into host (CPU) and device (GPU) worlds. A function in one execution space cannot directly call a function in the other — a `__host__` function cannot call a `__device__` function, and vice versa. cudafe++ enforces these rules at two points during compilation: at explicit call sites in expressions (`expr.c`) and at symbol reference recording time (`symbol_ref.c`). Together these checks cover both direct function calls and indirect references — variable accesses, implicit constructor/destructor invocations, and template-instantiated calls. The validation produces 12 distinct calling error messages (6 normal + 6 constexpr-with-suggestion variants), plus 4 variable access errors and 1 device-only function reference error.

## Key Facts

| Property | Value |
|---|---|
| Source files | `expr.c` (call site checks), `symbol_ref.c` (reference-time checks), `class_decl.c` (type hierarchy walk), `nv_transforms.c` (helpers) |
| Call-site checker | `sub_505720` (`check_cross_execution_space_call`, 4.0 KB) |
| Template variant | `sub_505B40` (`check_cross_space_call_in_template`, 2.7 KB) |
| Reference checker | `sub_72A650` (`record_symbol_reference_full`, 6-arg, 659 lines) |
| Reference checker (short) | `sub_72B510` (`record_symbol_reference_full`, 4-arg, 732 lines) |
| Type hierarchy walker | `sub_41A1F0` (annotation helper, walks nested types for HD violations) |
| Type hierarchy entry | `sub_41A3E0` (validates lambda/class HD annotation, calls `sub_41A1F0`) |
| Space name helper | `sub_6BC6B0` (`get_entity_display_name`, 49 lines) |
| Trivial-device-copyable | `sub_6BC680` (`is_device_or_extended_device_lambda`, 16 lines) |
| Device ref expression walker | `sub_6BE330` (`nv_scan_expression_for_device_refs`, 89 lines) |
| Diagnostic emission | `sub_4F7450` (multi-arg diagnostic), `sub_4F8090` (type+entity diagnostic) |
| Calling errors | 3462, 3463, 3464, 3465, 3508 |
| Variable access errors | 3548, 3549, 3550, 3486 |
| Device-only function ref | 3623 |
| Type annotation errors | 3593, 3594, 3597, 3598, 3599, 3615, 3635, 3691 |
| Cross-space enable flag | `dword_106BFD0` (primary), `dword_106BFCC` (secondary) |
| Device ref relaxation | `dword_106BF40` (allow `__device__` function refs in host) |
| Relaxed constexpr flag | `dword_126EFB0` (also referenced as CLI flag 104) |

## Execution Space Recall

The execution space is encoded at byte offset `+182` of the entity (routine) node. The two-bit extraction `byte & 0x30` classifies the routine:

| `byte & 0x30` | Space | Meaning |
|---|---|---|
| `0x00` | (none) | Implicit `__host__` |
| `0x10` | `__host__` | Explicit host-only |
| `0x20` | `__device__` | Device-only |
| `0x30` | `__host__ __device__` | Both spaces |

The `0x60` mask distinguishes `__global__` kernels: `(byte & 0x60) == 0x20` means plain `__device__`, while `byte & 0x40` set means `__global__`.

Additional flags at byte `+177` encode secondary space information:

| Bit | Mask | Meaning |
|---|---|---|
| 0 | `0x01` | `__host__` annotation present |
| 1 | `0x02` | `__device__` annotation present |
| 2 | `0x04` | constexpr device |
| 4 | `0x10` | implicitly HD / `__forceinline__` relaxation |

The `+177 & 0x10` bit is the critical bypass: when set, the function is treated as implicitly `__host__ __device__` and exempt from cross-space checks. This covers constexpr functions (which are implicitly HD since CUDA 7.5) and `__forceinline__` functions (which the compiler may allow to be instantiated in either space).

## The Implicitly-HD Bypass

Before any cross-space error is emitted, both the caller and callee are tested for the implicitly-HD condition. The exact binary test is:

```c
// Implicitly-HD check (appears in both sub_505720 and sub_505B40)
// entity: pointer to routine entity node

bool is_implicitly_hd(int64_t entity) {
    // Check 1: bit 0x10 at +177 (constexpr/forceinline HD)
    if ((*(uint8_t*)(entity + 177) & 0x10) != 0)
        return true;

    // Check 2: deleted function with specific annotation combo
    // +184 is an 8-byte extended flags field
    // 0x800000000000 = deleted bit, 0x1000000 = explicit annotation
    // If deleted but NOT explicitly annotated, AND byte+176 bit 1 is clear:
    if ((*(uint64_t*)(entity + 184) & 0x800001000000LL) == 0x800000000000LL
        && (*(uint8_t*)(entity + 176) & 2) == 0)
        return true;

    return false;
}
```

This means:

1. **constexpr functions** — the `+177 & 0x10` bit is set during attribute processing, making them callable from both host and device code without explicit annotation.
2. **`__forceinline__` functions** — same bit, allowing cross-space inlining.
3. **Implicitly-deleted functions** — defaulted special members (constructors, destructors, assignment operators) that are deleted due to non-copyable members. These get a pass because they will never actually be called.

If either the caller or the callee is implicitly HD, the cross-space check returns immediately without error.

### Worked Example: Source to Diagnostic

A direct host-from-device call against a non-bypassed callee triggers the canonical 3462 path:

```cuda
__host__   int   make_value();           // host-only
__device__ int   square(int x) {
    return make_value() * make_value();  // host call from device context
}
```

Expected diagnostic from `sub_505720`:

```text
error: calling a __host__ function ("make_value") from a __device__ function ("square") is not allowed
       (#3462)
```

The check unfolds as: `square`'s entity byte `+182 & 0x30 == 0x20` (device), `make_value`'s is `0x10` (host); neither carries the `+177 & 0x10` implicit-HD bypass, neither matches the deleted-defaulted special case, so the gate fails through and `sub_4F8090` emits 3462 with both entity names. Marking `make_value` `__host__ __device__` would set its `+182` to `0x30` and make the same call legal — the constexpr/relaxed-constexpr flag at `dword_126EFB0` reaches the same result by promoting `square` through the `+177 & 0x10` bit instead.

## Call-Site Validation: sub_505720

`check_cross_execution_space_call` is called during expression scanning in `scan_expr_full` whenever a function call expression is processed. It takes three parameters:

```c
// sub_505720 -- check_cross_execution_space_call
// a1: entity pointer of the callee function (may be NULL)
// a2: bool -- if true, this is a "must be callable" context (__global__ launch)
// a3: source location pointer for diagnostics
// returns: char (nonzero if diagnostic was emitted)
char check_cross_execution_space_call(int64_t callee, bool must_callable, uint64_t *src_loc);
```

### Algorithm

The function follows a multi-stage gate structure. At each gate, an early return can skip the check entirely:

**Gate 1 — Class scope suppression.** If we are inside a class definition scope (`dword_126C5C8 != -1`) and the current scope has device-scope flags set (`scope_entry[6] & 0x06`), AND we are inside a type node context (`dword_106B670 != -1`, `type_entry[5] & 0x08`), the check is suppressed. This allows member function declarations inside device classes to reference host functions without error — the actual check happens when the member is instantiated/defined.

**Gate 2 — Diagnostic suppression scope.** If the current scope entry has diagnostic-suppression bit 1 of byte `+14` set (`scope_entry[14] & 0x02`), checks are suppressed. This covers SFINAE contexts and `decltype` evaluation.

**Gate 3 — Concept/requires context.** If the current context pointer (`qword_106B970`) is non-null and byte `+17` has bit 1 set (strict-mode or concept context), checks are suppressed.

**Gate 4 — No enclosing function.** If `dword_126C5D8 == -1` (no enclosing function scope), the caller space defaults to host-only (`v7=0, v8=1`) — meaning we are at file scope, which is implicitly host.

**Gate 5 — Extract caller space.** The enclosing function entity is retrieved from the scope stack at `qword_126C5E8 + 784 * dword_126C5D8 + 224`. Its execution space is extracted:
- `v7 = (caller[182] & 0x60) == 0x20` — caller is host-only
- `v8 = (caller[182] & 0x30) != 0x20` — caller is NOT device-only
- `v5 = (caller[-8] & 0x10) != 0` — caller has secondary device mark (the `-8` offset reads a flags byte 8 bytes before the entity, in the preceding allocation header)

**Gate 6 — Caller implicitly HD.** The caller is tested for implicitly-HD status. If true, return immediately.

**Gate 7 — Callee implicitly HD.** The callee (parameter `a1`) is tested for implicitly-HD status. If true, return immediately.

**Gate 8 — No caller entity or secondary device.** If no caller entity exists or the secondary device flag is set, skip to the `__global__` check.

### Error Decision Logic

After passing all gates, the function computes which error to emit based on caller/callee space combination:

```c
// Pseudocode for the error decision tree

bool callee_is_not_device = (callee[182] & 0x30) != 0x20;   // v3
bool callee_is_host_only  = (callee[182] & 0x60) == 0x20;   // v4
bool callee_is_global     = (callee[182] & 0x40) != 0;       // v11 in some paths
bool caller_is_host_only  = (caller[182] & 0x60) == 0x20;    // v7
bool caller_not_device    = (caller[182] & 0x30) != 0x20;    // v8
bool has_forceinline      = (caller[181] & 0x20) != 0;

if (caller_is_host_only && caller_not_device) {
    // Caller is __host__ __device__ (both flags set)
    if (has_forceinline || callee_is_not_device || !callee_is_host_only)
        goto global_check;

    // HD caller calling host-only callee
    if (!is_device_or_extended_lambda(callee)) {
        char *caller_name = get_entity_display_name(caller, 0);
        char *callee_name = get_entity_display_name(callee, 1);
        int errcode = 3462 + ((callee[177] & 0x02) != 0);  // 3462 or 3463
        emit_diagnostic(errcode, src_loc, callee_name, caller_name);
    }
} else if (caller_not_device) {
    // Caller is host-only, callee is device-only
    if (has_forceinline || callee_is_not_device || !callee_is_host_only)
        goto global_check;

    // Check relaxed-constexpr bypass
    if ((callee[177] & 0x02) != 0 && dword_106BF40) {
        // Callee has __device__ annotation AND relaxation flag is set
        if (must_callable && !callee_is_global)
            goto global_check;  // suppress for __global__ must-call context
        // else suppress entirely
    }

    // Check constexpr-device bypass
    if ((callee[177] & 0x04) != 0)
        goto global_check;  // constexpr device functions get a pass

    // Host caller calling device-only callee
    char *caller_name = get_entity_display_name(caller, 0);
    char *callee_name = get_entity_display_name(callee, 1);
    int errcode = 3465 - ((callee[177] & 0x02) == 0);  // 3464 or 3465
    emit_diagnostic(errcode, src_loc, callee_name, caller_name);
}

global_check:
if (must_callable && !callee_is_global) {
    // must_callable is true but callee is not __global__
    // (this path is for __global__ launch checks)
    // no error here -- fall through
} else if (!must_callable && callee_is_global) {
    // __global__ function called from wrong context
    if (callee_is_host_only) {
        // __global__ called from host-only -- "cannot be called from host"
        emit_diagnostic(3508, src_loc, "host", "cannot");
    } else if (!callee_is_host_only) {
        // __global__ called from __device__ context
        emit_diagnostic(3508, src_loc, "__device__", "cannot");
    }
} else if (must_callable || !callee_is_global) {
    return;  // no __global__ issue
} else {
    emit_diagnostic(3508, src_loc, "__global__", "must");
}
```

### Error 3462 vs 3463 (Device-from-Host Direction)

The distinction between errors 3462 and 3463 is the `+177 & 0x02` bit on the callee — whether it has an explicit `__device__` annotation:

- **3462**: `__device__` function called from `__host__` context. The callee has no explicit `__device__` annotation (it was implicitly device-only).
- **3463**: Same violation, but the callee has explicit `__device__` annotation. The error message includes an additional note about the `__host__ __device__` context.

The computation: `3462 + ((callee[177] & 0x02) != 0)` yields 3462 when the bit is clear, 3463 when set.

### Error 3464 vs 3465 (Host-from-Device Direction)

Similarly for the reverse direction:

- **3464**: `__host__` function called from `__device__` context, callee has explicit `__device__` annotation (bit clear in the subtraction).
- **3465**: Same violation, callee does NOT have explicit `__device__` annotation.

The computation: `3465 - ((callee[177] & 0x02) == 0)` yields 3464 when the bit is clear, 3465 when set.

### Error 3508 (__global__ Misuse)

Error 3508 is a parameterized error with two string arguments: the context string and the verb. The combinations are:

| Context | Verb | Meaning |
|---|---|---|
| `"host"` | `"cannot"` | `__global__` function cannot be called from `__host__` code directly (must use `<<<>>>`) |
| `"__device__"` | `"cannot"` | `__global__` function cannot be called from `__device__` code |
| `"__host__ __device__" + 9` = `"__device__"` | `"cannot"` | Same, from HD context with device focus |
| `"__global__"` | `"must"` | A `__global__` function must be called with `<<<>>>` syntax |

## Template Variant: sub_505B40

`check_cross_space_call_in_template` performs the same validation but is called during template instantiation rather than initial expression scanning. It has two key differences:

1. **Guard on `dword_126C5C4 == -1`**: only runs when no nested class scope is active. If `dword_126C5C4 != -1`, the entire function is skipped — template instantiation inside nested class definitions defers cross-space checks.

2. **Additional scope guards**: checks `scope_entry[4] != 12` (not a namespace scope) and `qword_106B970 + 17 & 0x40 == 0` (not in a concept context). These prevent false positives during dependent name resolution.

3. **No return value**: returns `void` instead of `char`. It only emits diagnostics; it does not report whether a diagnostic was emitted.

4. **Error code selection**: uses `3463 - ((callee[177] & 0x02) == 0)` for the HD-caller case (yielding 3462 or 3463), and `3465 - ((callee[177] & 0x02) == 0)` for the host-caller case (yielding 3464 or 3465). The `__global__` error always uses `"must"` verb.

5. **No `must_callable` parameter**: the template variant does not handle the `must`/`cannot` distinction for `__global__`. It always emits `3508` with `"__global__"` and `"must"` if the callee is `__global__`.

## Complete Calling Error Matrix

The following matrix shows which errors fire for each caller/callee space combination:

| Caller \ Callee | `__host__` | `__device__` | `__host__ __device__` | `__global__` |
|---|---|---|---|---|
| **`__host__`** (explicit) | OK | **3464** or **3465** | OK | **3508** (`"must"`) |
| **`__device__`** | **3462** or **3463** | OK | OK | **3508** (`"cannot"`) |
| **`__host__ __device__`** | OK | **3462** or **3463** | OK | **3508** |
| **(no annotation)** = host | OK | **3464** or **3465** | OK | **3508** (`"must"`) |
| **`__global__`** | OK | OK | OK | **3508** (`"cannot"`) |

Entries marked "OK" pass the cross-space check without error. The specific error (3462 vs 3463, 3464 vs 3465) depends on whether the callee has the `+177 & 0x02` bit (explicit `__device__` annotation).

### Bypass Conditions (No Error Despite Mismatch)

Even when the matrix says an error should fire, the following conditions suppress it:

1. **Caller or callee is implicitly HD** (`+177 & 0x10`): constexpr functions, `__forceinline__` functions, implicitly-deleted special members.
2. **Caller has `__forceinline__` relaxation** (`+181 & 0x20`): the caller has a `__forceinline__` attribute that relaxes cross-space restrictions.
3. **Callee is a device lambda that passes trivial-device-copyable check** (`sub_6BC680` returns true): extended lambda optimization.
4. **Callee has constexpr-device flag** (`+177 & 0x04`): constexpr functions marked for device use.
5. **`dword_106BF40` is set and callee has explicit `__device__`** (`+177 & 0x02`): the `--expt-relaxed-constexpr` or similar flag allows device function references from host code.
6. **Current scope has diagnostic suppression** (`scope_entry[14] & 0x02`): SFINAE context.
7. **Concept/requires context** (`qword_106B970 + 17 & 0x40`).

## The 12 Calling Error Messages

cudafe++ emits 6 base error messages for cross-space call violations. Each has a variant that adds a `--expt-relaxed-constexpr` suggestion when the callee is a constexpr function, yielding 12 total messages:

| Error | Direction | Context | Suggestion? |
|---|---|---|---|
| 3462 | device called from host | Callee lacks explicit `__device__` | No |
| 3463 | device called from HD | Callee has explicit `__device__` (HD context note) | No |
| 3464 | host called from device | Callee has explicit `__device__` (bit clear in subtraction) | No |
| 3465 | host called from device | Callee lacks explicit `__device__` | No |
| 3508 | `__global__` context error | Parameterized: `"must"` / `"cannot"` + space string | No |
| 3462+constexpr | device called from host | constexpr callee | Yes: `--expt-relaxed-constexpr` |
| 3463+constexpr | device called from HD | constexpr callee | Yes |
| 3464+constexpr | host called from device | constexpr callee | Yes |
| 3465+constexpr | host called from device | constexpr callee | Yes |
| 3508+constexpr | `__global__` context | constexpr callee | Yes |

The constexpr suggestion variants are selected by the relaxed-constexpr flag state. When `dword_106BF40` (the `--expt-relaxed-constexpr` relaxation flag) is NOT set and the callee has constexpr annotations, the error message includes a note suggesting the flag to resolve the issue.

## Variable Access Validation: symbol_ref.c

The `record_symbol_reference_full` functions (`sub_72A650` / `sub_72B510`) enforce cross-space rules at the symbol reference level. This is a different check point than the call-site checker — it catches variable accesses and implicit function references that are not explicit function calls.

### Reference Kind Bitmask (Parameter a1)

The first parameter encodes the kind of reference being made:

| Bit | Mask | Meaning |
|---|---|---|
| 0 | `0x01` | Address reference (`&var`) |
| 1 | `0x02` | Write reference (assignment target) |
| 2 | `0x04` | Non-modifying reference (read) |
| 3 | `0x08` | Direct use |
| 4 | `0x10` | Initializer |
| 5 | `0x20` | Potential modification |
| 6 | `0x40` | Move reference |
| 10 | `0x400` | Template argument |
| 13 | `0x2000` | ODR-use |
| 15 | `0x8000` | Negative offset |

### Device Variable in Host Code (Errors 3548-3550)

When the referenced symbol is a variable (kind == 7), `record_symbol_reference_full` checks whether a `__shared__`, `__constant__`, or `__managed__` variable is accessed from host code:

```c
// nv_check_device_var_ref_in_host (inlined in sub_72A650, lines ~330-374)
// var_info: *(entity + 88) -- pointer to variable descriptor

uint64_t caller = 0;
if (dword_126C5D8 != -1)  // enclosing function scope exists
    caller = *(qword_126C5E8 + 784 * dword_126C5D8 + 224);

uint8_t mem_flags = *(var_info + 148);     // memory space flags
const char *space_name;
if (mem_flags & 0x02)
    space_name = "__shared__";
else if (mem_flags & 0x04)
    space_name = "__constant__";
else
    space_name = "";  // no specific space (managed or other)

// Exemption: managed variables with bit 0x100 set are OK
if ((*(uint16_t*)(var_info + 148) & 0x0101) == 0x0101)
    return;  // managed + exemption flag

// Only check if: has device memory annotation, there is a caller,
// caller is NOT device-only, caller is not implicitly-HD
if ((ref_kind & 0x12040) == 0       // not a transparent reference
    && (mem_flags & 0x07) != 0       // has device memory annotation
    && caller != 0
    && (*(caller + 182) & 0x30) != 0x20   // caller NOT device-only
    && (*(caller + 177) & 0x10) == 0      // caller NOT implicitly HD
    && !is_implicitly_hd(caller))          // extended implicit-HD check
{
    if (ref_kind & 0x08)  // direct use
        emit_diag(3548, src_loc, space_name, entity);  // "reference to __shared__"

    if (ref_kind & 0x10)  // initializer
        emit_diag(3549, src_loc, space_name, entity);  // "initializer for __constant__"

    if ((mem_flags & 0x02) && (ref_kind & 0x20))  // __shared__ + write
        emit_diag(3550, src_loc, space_name, entity);  // "write to __shared__"
}
```

| Error | Condition | Message |
|---|---|---|
| 3548 | Direct use of `__shared__`/`__constant__` variable from host | Reference to device memory variable from host code |
| 3549 | Initializer referencing `__shared__`/`__constant__` from host | Cannot initialize from host |
| 3550 | Write to `__shared__` variable from host | Cannot write to shared memory from host |

### Device-Only Function Reference (Error 3623)

For function-type symbols (kind 10 or 11, or concept kind 20), the check validates that `__device__`-only functions are not referenced from host code:

```c
// nv_check_device_function_ref_in_host (inlined in sub_72A650, lines ~382-454)
// entity: the function being referenced
// entity + 88 -> routine info (for kind 10/11)
// entity + 88 -> +192 for concepts (kind 20)

int64_t routine_info = ...;  // resolve through type chain
if (routine_info == 0)
    return;

// Only check if: has device annotation, is device-only,
// has no implicit-HD flags
if ((*(routine_info + 191) & 0x01) == 0     // not a coroutine exemption
    || (*(routine_info + 182) & 0x30) != 0x20  // not device-only
    || (*(routine_info + 177) & 0x15) != 0)    // has HD/host/constexpr flags
    return;

// Check if already exempted by extended flags
if (is_implicitly_hd(routine_info))
    return;

// Determine caller context
int64_t caller_routine = 0;
if (dword_126C5D8 != -1) {
    caller_routine = *(qword_126C5E8 + 784 * dword_126C5D8 + 224);
} else if (dword_126C5B8) {
    // Walk scope stack to find enclosing try block
    int scope_idx = dword_126C5E4;
    while (scope_idx != -1) {
        int64_t entry = qword_126C5E8 + 784 * scope_idx;
        if (*(int32_t*)(entry + 408) != -1)  // has try block
            break;
        scope_idx = *(int32_t*)(entry + 560);  // parent scope
    }
    if (scope_idx == -1) return;
    caller_routine = *(entry + 224);
}

if (caller_routine == 0) goto emit_outside;
if (is_implicitly_hd(caller_routine)) return;

if ((*(caller_routine + 182) & 0x30) == 0x20) {
    // Caller is __device__-only
    if ((*(caller_routine + 177) & 0x05) == 0)
        return;  // no constexpr/consteval markers
    context = "from a constexpr or consteval __device__ function";
} else {
    context = "outside the bodies of device functions";
}

emit_outside:
const char *name = *(routine_info + 8);  // function name
if (!name) name = "";
emit_diagnostic(3623, src_loc, name, context);
```

Error 3623 has two context strings:
- `"outside the bodies of device functions"` — the reference is from file scope or host code
- `"from a constexpr or consteval __device__ function"` — the reference is from a constexpr/consteval device function that cannot actually call the target

### The dword_106BFD0 / dword_106BFCC Gate

Both `record_symbol_reference_full` variants gate the cross-space device-reference scan (`sub_6BE330`) with:

```c
if (dword_106BFD0 || dword_106BFCC) {
    // Cross-space reference checking is enabled
    if (!qword_126C5D0                                    // no current routine descriptor
        || *(qword_126C5D0 + 32) == 0                    // no routine entity
        || (*(*(qword_126C5D0 + 32) + 182) & 0x30) != 0x20  // not device-only
        || (dword_106BF40 && (*(*(qword_126C5D0 + 32) + 177) & 0x02) != 0))
    {
        // Call sub_6BE330 to walk expression tree for device references
        nv_scan_expression_for_device_refs(entity);
    }
}
```

The scan is skipped when the current routine IS `__device__`-only — device code referencing other device symbols is always valid. The `dword_106BF40` check further relaxes: if the flag is set AND the routine has explicit `__device__` annotation (`+177 & 0x02`), the scan is also skipped.

## Type Hierarchy Walk: sub_41A1F0 / sub_41A3E0

The type hierarchy walkers handle a different class of violation: when a `__host__ __device__` or `__device__` annotation is applied to a class or lambda whose member types contain HD-incompatible nested types. These functions live in `class_decl.c` and are called during class completion.

### sub_41A3E0 (Entry Point)

This function validates a complete type annotation context. It receives a lambda/class info structure and checks multiple conditions:

```c
// sub_41A3E0 -- validate_type_hd_annotation
// a1: type annotation context structure
//   +8:  entity pointer
//   +32: flags byte (bit 0 = has_host, bit 3 = has_conflict, bit 4 = has_device,
//                     bit 5 = has_virtual)
//   +36: source location
// a2: 0 = __host__ __device__, nonzero = __device__ only
// a3: enable additional nested check (for OptiX path)

char *space_name = (a2 == 0) ? "__host__ __device__" : "__device__";

// Error 3615: duplicate HD annotation conflict
if (a2 == 0 && (flags & 0x01))
    emit_diag(3615, src_loc);

// Error 3593: conflict between __host__ and __device__ on type
if (flags & 0x08) {
    if (entity && entity[163] < 0) {  // entity has device-negative flag
        if ((flags & 0x18) != 0x18)
            goto check_members;
        emit_diag(3635, src_loc);  // both __host__ and __device__ + conflict
    } else {
        emit_diag(3593, src_loc, space_name);
    }
}

// Error 3594: virtual function in __device__ context
if (flags & 0x20 || ...)
    emit_diag(3594, src_loc, space_name);

// Recurse into member types
walk_type_for_hd_violations(type_entry, src_loc, a2);  // sub_41A1F0

// Error 3691: nested OptiX check
if (a3 && (flags & 0x10))
    emit_diag(3691, src_loc, space_name);
```

### sub_41A1F0 (Recursive Type Walker)

This function walks the type hierarchy to find nested violations. It uses `sub_7A8370` (is-array-type check) and `sub_7A9310` (get-array-element-type) to traverse through arrays, and walks through `cv`-qualified type wrappers (kind == 12) by following the `+144` pointer chain.

```c
// sub_41A1F0 -- walk_type_for_hd_violations (recursive)
// a1: type node pointer
// a2: source location pointer
// a3: 0 = HD mode, nonzero = device-only mode

char *space_name = (a3) ? "__device__" : "__host__ __device__";

if (!is_valid_type(a1) || a1 == 0) {
    // Base case: no type to check, or check passed at top level
    goto label_20;
}

int depth = 0;
int64_t current = a1;
do {
    if (!is_array_type(current)) {  // sub_7A8370
        // Not an array -- check this type for violations
        if (depth > 7)
            emit_diag(3597, src_loc, space_name, a1);  // nesting depth exceeded

        // Walk through cv-qualified wrappers
        while (*(current + 132) == 12)  // cv-qual kind
            current = *(current + 144);  // underlying type

        // Guard: skip if in nested class scope
        if (dword_126C5C4 != -1)
            return;
        if ((scope_entry[6] & 0x06) != 0)
            return;
        if (scope_entry[4] == 12)  // namespace scope
            goto walk_callback;

        // Error 3598: type not valid in device context
        if (!check_type_valid_for_space(30, current, 0))  // sub_550E50
            emit_diag(3598, src_loc, space_name, current);

        // Error 3599: type has problematic member
        int64_t display = get_type_display_name(current);  // sub_5BD540
        if (!check_member_compat(60, display, current))  // sub_510860
            emit_diag(3599, src_loc, space_name, current);

        goto label_20;
    }
    ++depth;
    current = get_array_element_type(current);  // sub_7A9310
} while (current != 0);

label_20:
// Final phase: walk_tree with callback sub_41B420
if (dword_126C5C4 != -1) return;
if ((scope_entry[6] & 0x06) != 0) return;
if (scope_entry[4] == 12) return;

// Save/restore diagnostic state
saved_state = qword_126EDE8;
qword_126EDE8 = *src_loc;
dword_E7FE78 = 0;
walk_tree(a1, sub_41B420, 792);  // sub_7B0B60 with callback
qword_126EDE8 = saved_state;
```

The callback `sub_41B420` is used in the tree walk to check each nested type member. This is the same callback used for OptiX extended lambda body validation, applied to validate that all types referenced within the annotated scope are compatible with the target execution space.

### Type Annotation Errors

| Error | Condition | Message |
|---|---|---|
| 3593 | Conflict between `__host__` and `__device__` on extended lambda/type | Cannot apply both annotations |
| 3594 | Virtual function in `__device__` or HD context | Virtual dispatch not supported on device |
| 3597 | Type nesting depth exceeds 7 levels in HD validation | Type hierarchy too deep for device |
| 3598 | Nested type not valid in device context | Type `X` cannot be used in `__device__` code |
| 3599 | Nested type member incompatible with device execution | Member of type `X` is not device-compatible |
| 3615 | Duplicate `__host__ __device__` annotation | Already annotated as HD |
| 3635 | Both `__host__` and `__device__` annotations with negative device flag | Conflicting explicit annotations |
| 3691 | Nested OptiX annotation conflict | OptiX extended lambda nested check failure |

## Global State Variables

| Global | Type | Purpose |
|---|---|---|
| `qword_126C5E8` | `int64_t` | Scope stack base pointer (array of 784-byte entries) |
| `dword_126C5E4` | `int32_t` | Current scope stack top index |
| `dword_126C5D8` | `int32_t` | Current function scope index (-1 if none) |
| `dword_126C5C8` | `int32_t` | Class scope index (-1 if none) |
| `dword_126C5C4` | `int32_t` | Nested class scope (-1 if none) |
| `dword_126C5B8` | `int32_t` | Is-member-of-template flag |
| `qword_126C5D0` | `int64_t` | Current routine descriptor pointer |
| `qword_106B970` | `int64_t` | Current compilation context |
| `dword_106BFD0` | `int32_t` | Enable cross-space reference checking (primary) |
| `dword_106BFCC` | `int32_t` | Enable cross-space reference checking (secondary) |
| `dword_106BF40` | `int32_t` | Allow `__device__` function references in host |
| `dword_106B670` | `int32_t` | Current type node context index (-1 if none) |
| `qword_106B678` | `int64_t` | Type node table base pointer |
| `dword_E7FE78` | `int32_t` | Diagnostic state flag (cleared during type walks) |
| `qword_126EDE8` | `int64_t` | Saved diagnostic source position |

## Function Map

| Address | Size | Identity | Source |
|---|---|---|---|
| `sub_41A1F0` | ~0.5 KB | `walk_type_for_hd_violations` | `class_decl.c` |
| `sub_41A3E0` | ~0.5 KB | `validate_type_hd_annotation` | `class_decl.c` |
| `sub_41B420` | (callback) | Type walk callback for device compat | `class_decl.c` |
| `sub_4F7450` | ~0.3 KB | `emit_diag_multi_arg` (cross-space diagnostics) | `expr.c` |
| `sub_505720` | 4.0 KB | `check_cross_execution_space_call` | `expr.c` |
| `sub_505AA0` | 0.8 KB | `get_execution_space_string` | `expr.c` |
| `sub_505B40` | 2.7 KB | `check_cross_space_call_in_template` | `expr.c` |
| `sub_6BC680` | 0.1 KB | `is_device_or_extended_device_lambda` | `nv_transforms.c` |
| `sub_6BC6B0` | 0.5 KB | `get_entity_display_name` | `nv_transforms.c` |
| `sub_6BE330` | 0.9 KB | `nv_scan_expression_for_device_refs` | `nv_transforms.c` |
| `sub_72A650` | 6.6 KB | `record_symbol_reference_full` (6-arg) | `symbol_ref.c` |
| `sub_72B510` | 7.3 KB | `record_symbol_reference_full` (4-arg) | `symbol_ref.c` |

## Cross-References

- [Execution Spaces](execution-spaces.md) — the `+182` byte encoding and attribute handlers
- [Device/Host Separation](device-host-separation.md) — how validated code is split into device and host IL
- [Kernel Stubs](kernel-stubs.md) — `__global__` function wrapper generation
- [Entity Node](../structs/entity-node.md) — byte offsets `+176`, `+177`, `+182`, `+184`
- [Diagnostics Overview](../diagnostics/overview.md) — error emission pipeline
- [Lambda Overview](../lambda/overview.md) — extended lambda HD annotation validation
