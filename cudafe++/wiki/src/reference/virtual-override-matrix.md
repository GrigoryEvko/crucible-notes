# Virtual Override Execution Space Matrix

When a derived class overrides a base class virtual function in CUDA, the execution spaces of both functions must be compatible. A `__device__` virtual cannot be overridden by a `__host__` function, a `__host__` virtual cannot be overridden by a `__device__` function, and so on. cudafe++ enforces these rules inside `record_virtual_function_override` (`sub_432280`, 437 lines, `class_decl.c`), which runs each time the EDG front-end registers a virtual override during class body scanning. The function performs three tasks: (1) propagate the base class's execution space obligations onto the derived function, (2) detect illegal mismatches and emit one of six dedicated error messages (3542--3547), and (3) fall through to standard EDG override recording (covariant returns, `[[nodiscard]]`, `override`/`final`, requires-clause checks).

This page documents the override checking logic at reimplementation-grade depth: reconstructed pseudocode from the decompiled binary, a complete compatibility matrix, the six error messages with their diagnostic tags, and the relaxed-mode flag that softens certain checks.

## Key Facts

| Property | Value |
|---|---|
| Binary function | `sub_432280` (`record_virtual_function_override`, 437 lines) |
| Source file | `class_decl.c` |
| Parameters | `a1`=derivation\_info, `a2`=overriding\_sym, `a3`=overridden\_sym, `a4`=base\_class\_info, `a5`=covariant\_return\_adjustment |
| Entity field read | `byte +182` (execution space bitfield) on both overridden and overriding entities |
| Classification mask | `byte & 0x30` -- two-bit extraction: `0x00`=implicit host, `0x10`=explicit host, `0x20`=device, `0x30`=HD |
| Propagation bits | `0x10` (host\_explicit), `0x20` (device\_annotation) |
| Attribute lookup | `sub_5CEE70` with kind 87 (`__device__`) and 86 (`__host__`) |
| Error emission | `sub_4F4F10` with severity 8 (hard error) |
| Relaxed mode flag | `dword_106BFF0` (`relaxed_attribute_mode`) |
| Implicitly-HD test | `byte +177 & 0x10` on entity -- constexpr / `__forceinline__` bypass |
| Override-involved mark | `byte +176 \|= 0x02` on overriding entity |
| Assertion guard | `nv_is_device_only_routine` from `nv_transforms.h:367` |

## Why Virtual Functions Need Execution Space Checks

Standard C++ imposes no concept of execution space on virtual functions. CUDA introduces three execution spaces (`__host__`, `__device__`, `__host__ __device__`) and one launch-only space (`__global__`). When a virtual function in a base class is declared with one execution space, every override in every derived class must be callable in the same space. If the base declares a `__device__` virtual, calling it through a base pointer on the GPU must dispatch to the derived override -- which is only possible if the override is also `__device__` (or `__host__ __device__`).

`__global__` functions cannot be virtual at all (error 3505/3506 prevents this at the attribute application stage), so the override matrix only covers three spaces: `__host__`, `__device__`, and `__host__ __device__`. An unannotated function counts as implicit `__host__`.

## Function Entry: Mark and Resolve Entities

The function begins by resolving the actual entity nodes from the symbol table entries:

```c
// sub_432280 entry (lines 60-69 of decompiled output)
//
// a2 = overriding_sym (symbol table entry for the derived-class function)
// a3 = overridden_sym (symbol table entry for the base-class function)
//
// v10 = entity of overridden function:  *(overridden_sym + 88)
// v11 = entity of overriding function:  *(*(overriding_sym) + 88)
//
// The entity node at offset +88 is the "associated routine entity" --
// the actual function representation containing execution space bits.

int64_t overridden_entity = *(int64_t*)(overridden_sym + 88);   // v10
int64_t overriding_entity = *(int64_t*)(*(int64_t*)overriding_sym + 88);  // v11

// Mark the overriding entity as "involved in an override"
*(uint8_t*)(overriding_entity + 176) |= 0x02;
```

The `+176 |= 0x02` flag marks the derived function as "override-involved." This flag is consumed downstream by the exception specification resolver and other class completion logic.

## Phase 1: Implicitly-HD Fast Path and Execution Space Propagation

The first branch tests `byte +177 & 0x10` on the **overriding** entity. This bit indicates the function is implicitly `__host__ __device__` -- set for constexpr functions (implicitly HD since CUDA 7.5) and `__forceinline__` functions. When this bit is set, the override is exempt from mismatch checking, but execution space **propagation** still occurs.

```c
// Phase 1: implicitly-HD check and propagation (lines 70-94)
void check_and_propagate(int64_t overriding_entity, int64_t overridden_entity) {

    if (overriding_entity->byte_177 & 0x10) {
        // Overriding function is implicitly HD (constexpr / __forceinline__)
        //
        // Skip mismatch errors entirely -- an implicitly-HD function is
        // compatible with any base execution space.  But we must still
        // propagate the base's space obligations onto the derived entity
        // so that downstream passes (IL marking, code generation) know
        // what to emit.

        if (!(overridden_entity->byte_177 & 0x10)) {
            // Overridden function is NOT implicitly HD -- it has an explicit
            // execution space.  We need to propagate that space.
            //
            // Guard: skip propagation for constexpr lambdas with internal
            // linkage but no override flag (a degenerate case).
            if ((overridden_entity->qword_184 & 0x800001000000) == 0x800000000000
                && !(overridden_entity->byte_176 & 0x02)) {
                // Degenerate case -- skip propagation
                goto done_nvidia_checks;
            }

            uint8_t base_es = overridden_entity->byte_182;

            // Propagate __host__ obligation:
            // If the base is NOT device-only (i.e., base is host, HD, or
            // unannotated), the derived function inherits the host obligation.
            if ((base_es & 0x30) != 0x20) {
                overriding_entity->byte_182 |= 0x10;   // set host_explicit
            }

            // Propagate __device__ obligation:
            // If the base has the device_annotation bit set, the derived
            // function inherits the device obligation.
            if (base_es & 0x20) {
                overriding_entity->byte_182 |= 0x20;   // set device_annotation
            }
        }

        goto done_nvidia_checks;
    }

    // ... Phase 2 continues below
}
```

### Why Propagation Matters

Propagation ensures that a derived class inherits its base class's execution space obligations even when the derived function is implicitly HD. Consider:

```cuda
struct Base {
    __device__ virtual void f();        // byte_182 & 0x30 == 0x20
};

struct Derived : Base {
    constexpr void f() override;        // byte_177 & 0x10 set (implicitly HD)
};
```

Without propagation, `Derived::f` would have `byte_182 == 0x00` (no explicit annotation). The device-side IL pass would skip it, and a virtual call `base_ptr->f()` on the GPU would dispatch to a function never compiled for the device. Propagation sets `byte_182 |= 0x20` (device\_annotation), ensuring the function is included in device IL.

The propagation follows strict rules:

| Base `byte_182 & 0x30` | Propagated to overriding entity |
|---|---|
| `0x00` (implicit host) | `\|= 0x10` (host\_explicit) |
| `0x10` (explicit host) | `\|= 0x10` (host\_explicit) |
| `0x20` (device) | `\|= 0x20` (device\_annotation) |
| `0x30` (HD) | `\|= 0x10` then `\|= 0x20` (both) |

## Phase 2: Explicit Annotation Mismatch Detection

When the overriding function is NOT implicitly HD (`byte_177 & 0x10 == 0`), the checker must verify that the derived function's explicit execution space matches the base. It does this by querying the attribute lists on the overriding symbol for `__device__` (kind 87) and `__host__` (kind 86) attributes using `sub_5CEE70`.

The overriding symbol has two attribute list pointers: offset `+184` (primary attributes) and offset `+200` (secondary/redeclaration attributes). Both are checked for each attribute kind.

### Reconstructed Pseudocode

```c
// Phase 2: explicit annotation mismatch detection (lines 96-188)
//
// At this point, overriding_entity->byte_177 & 0x10 == 0 (not implicitly HD).
// We must determine what execution space annotations the overriding function
// has, and compare against the overridden function's execution space.

void check_override_mismatch(
    int64_t overriding_sym,       // a2
    int64_t overriding_entity,    // v11
    int64_t overridden_entity,    // v10
    int64_t overridden_sym_list,  // v6 = a2+48 (location info for diagnostics)
    int64_t overridden_sym_arg,   // v8 = a3 (for diagnostics)
    int64_t base_sym              // v9 = *a2 (for diagnostics)
) {
    // -- Assertion: overridden entity must exist --
    if (!overridden_entity) {
        internal_error("nv_transforms.h", 367, "nv_is_device_only_routine");
    }

    // -- Extract overridden execution space --
    uint8_t base_es    = overridden_entity->byte_182;
    uint8_t mask_30    = base_es & 0x30;     // 0x00/0x10/0x20/0x30
    bool    base_no_device_annotation = (base_es & 0x20) == 0;  // v56
    bool    base_is_hd = (mask_30 == 0x30);  // v58
    uint8_t base_device_bit = base_es & 0x20;  // v55

    // -- Check overriding function for __device__ attribute (kind 87) --
    bool has_device_attr = find_attribute(87, overriding_sym->attr_list_184)
                        || find_attribute(87, overriding_sym->attr_list_200);

    if (has_device_attr) {
        // Overriding function has __device__.
        // Now check if it also has __host__ (kind 86) -- making it HD.

        bool has_host_attr = find_attribute(86, overriding_sym->attr_list_184)
                          || find_attribute(86, overriding_sym->attr_list_200);

        if (has_host_attr) {
            // --- Overriding is __host__ __device__ ---
            if (base_device_bit) {
                // Base has device_annotation (bit 5 set).
                // If base is device-only (mask_30 == 0x20), error 3544.
                if (mask_30 == 0x20) {
                    emit_error(8, 3544, location, overridden, base);
                }
                // If base is HD (mask_30 == 0x30), it's legal -- no error.
                // If base has device_bit but mask_30 != 0x20 and != 0x30,
                // that can't happen (bit 5 set implies mask_30 is 0x20 or 0x30).
            } else {
                // Base has no device_annotation -- base is host or implicit host.
                emit_error(8, 3543, location, overridden, base);
            }
        } else {
            // --- Overriding is __device__ only ---
            // Fall through to LABEL_83 logic.
            goto device_only_check;
        }
    } else {
        // Overriding function has NO __device__ attribute.
        // It's either explicit __host__ or implicit host (no annotation).

        if (dword_106BFF0) {
            // Relaxed mode: check if overriding has explicit __host__.
            bool has_host_attr = find_attribute(86, overriding_sym->attr_list_184)
                              || find_attribute(86, overriding_sym->attr_list_200);

            if (!has_host_attr) {
                // No explicit __host__ either -- implicit host.
                // In relaxed mode, an implicit-host override is treated like
                // a device-only override for certain base configurations.
                // Jump into the device-only path with modified conditions.
                goto device_only_check_relaxed;
            }
            // Explicit __host__ in relaxed mode: fall through to normal checks.
        }

        // --- Overriding is __host__ (explicit or implicit) ---
        if (mask_30 == 0x20) {
            // Base is __device__ only
            emit_error(8, 3545, location, overridden, base);
        } else if (mask_30 == 0x30) {
            // Base is __host__ __device__
            emit_error(8, 3546, location, overridden, base);
        }
        // else: base is host/implicit-host, same space -- no error.
        goto done_nvidia_checks;
    }

device_only_check:
    // Overriding is __device__ only (has __device__ but no __host__).
    // v39 = base_no_device_annotation (v56), v40 = 1 (always set entering here).
    {
        bool should_error = base_no_device_annotation;  // v39
        bool relaxed_extra = true;                      // v40

device_only_check_relaxed:
        // (relaxed mode entry: v39 = 0, a1 = v56 = base_no_device_annotation)

        if (dword_106BFF0) {
            // Relaxed mode: the error fires unconditionally when
            // base has no device annotation (base is host/implicit-host).
            // In strict mode, same condition applies.
            should_error = base_no_device_annotation;
            relaxed_extra = true;   // always true in relaxed
        }

        if (should_error) {
            // Base is host-only (no device_annotation) and override is device-only.
            emit_error(8, 3542, location, overridden, base);
        } else if (base_is_hd && relaxed_extra) {
            // Base is HD, override is device-only.
            // v40 (relaxed_extra) is always 1 from Entry A, so this
            // fires in both strict and relaxed modes for D-overrides-HD.
            emit_error(8, 3547, location, overridden, base);
        }
        // else: base is device-only too -- compatible, no error.
    }

done_nvidia_checks:
    // Continue to standard EDG override recording...
}
```

### Decision Tree (Simplified)

```
overriding byte_177 & 0x10?
  YES (implicitly HD) --> propagate, skip mismatch check
  NO  --> extract base_es = overridden byte_182
          has __device__ attr on overriding?
            YES --> also has __host__ attr?
              YES (override=HD):
                base has device_annotation?
                  YES and mask_30==0x20 --> ERROR 3544
                  NO                    --> ERROR 3543
              NO (override=D-only):
                base has NO device_annotation? --> ERROR 3542
                base is HD?                    --> ERROR 3547
            NO (override=H or implicit-H):
              base mask_30==0x20 --> ERROR 3545
              base mask_30==0x30 --> ERROR 3546
              otherwise         --> legal (same space)
```

## The Six Error Messages

Each mismatch produces one of six errors. All are emitted at severity 8 (hard error) and are individually suppressible by their diagnostic tag via `--diag_suppress` or `#pragma nv_diag_suppress`.

| Internal | Display | Diagnostic Tag | Message Template |
|---|---|---|---|
| 3542 | 20085 | `vfunc_incompat_exec_h_d` | `execution space mismatch: overridden entity (%n1) is a __host__ function, but overriding entity (%n2) is a __device__ function` |
| 3543 | 20086 | `vfunc_incompat_exec_h_hd` | `execution space mismatch: overridden entity (%n1) is a __host__ function, but overriding entity (%n2) is a __host__ __device__ function` |
| 3544 | 20087 | `vfunc_incompat_exec_d_hd` | `execution space mismatch: overridden entity (%n1) is a __device__ function, but overriding entity (%n2) is a __host__ __device__ function` |
| 3545 | 20088 | `vfunc_incompat_exec_d_h` | `execution space mismatch: overridden entity (%n1) is a __device__ function, but overriding entity (%n2) is a __host__ function` |
| 3546 | 20089 | `vfunc_incompat_exec_hd_h` | `execution space mismatch: overridden entity (%n1) is a __host__ __device__ function, but overriding entity (%n2) is a __host__ function` |
| 3547 | 20090 | `vfunc_incompat_exec_hd_d` | `execution space mismatch: overridden entity (%n1) is a __host__ __device__ function, but overriding entity (%n2) is a __device__ function` |

The display number is computed as `internal + 16543` (the standard CUDA error renumbering from `construct_text_message`). The tag naming convention is `vfunc_incompat_exec_{overridden}_{overriding}`.

The `%n1` and `%n2` fill-ins resolve to the entity display names of the base and derived functions respectively, including their full qualified names and parameter types.

### Suppression Example

```bash
# Suppress by tag (preferred)
nvcc --diag_suppress=vfunc_incompat_exec_h_d file.cu

# Suppress by display number
nvcc --diag_suppress=20085 file.cu

# Suppress in source
#pragma nv_diag_suppress vfunc_incompat_exec_h_d
```

## Complete Compatibility Matrix

This table shows every combination of base (overridden) and derived (overriding) execution space. "Implicit H" means the function has no execution space annotation (`byte_182 & 0x30 == 0x00`). Since implicit host and explicit `__host__` are treated identically for override purposes (both lack the device\_annotation bit and have `mask_30 != 0x20`), they share the same row/column behavior.

`__global__` is excluded because `__global__` functions cannot be virtual -- the attribute handler rejects `__global__` on virtual functions before override checking ever runs.

The matrix is the same in both strict mode (`dword_106BFF0 == 0`) and relaxed mode (`dword_106BFF0 == 1`). The relaxed flag changes the *code path* used to reach the error decision but produces the same result for all input combinations.

| | **Derived: H / implicit H** | **Derived: D** | **Derived: HD** | **Derived: implicitly HD** |
|---|---|---|---|---|
| **Base: H / implicit H** | legal | **error 3542** | **error 3543** | legal + propagate `\|= 0x10` |
| **Base: D** | **error 3545** | legal | **error 3544** | legal + propagate `\|= 0x20` |
| **Base: HD** | **error 3546** | **error 3547** | legal | legal + propagate `\|= 0x10`, `\|= 0x20` |

Reading the matrix: each row is the base class virtual function's space; each column is the derived class override's space. "Legal" means no error is emitted and the override is recorded normally. "Legal + propagate" means the override is accepted AND the base's execution space bits are OR'd into the derived entity's `byte_182`.

The diagonal (same space in base and derived) is always legal. The last column (implicitly HD) is always legal because an implicitly HD function is compatible with every execution space -- the mismatch check is skipped entirely and only propagation runs.

### Why Both Modes Produce the Same Matrix

Tracing the LABEL\_83 code path with the two entry points reveals that `dword_106BFF0` does NOT gate error 3547. In the critical device-only-override path (Entry A), `v40` is set to 1 before reaching LABEL\_83 regardless of the relaxed flag. The flag only changes the assignment to `a1` and `v40` via conditional moves (cmovz/cmovnz in the disassembly), but the net effect is identical for all input combinations:

```
LABEL_83 internals (decompiled, annotated):
  a2 = 3542;                          // tentative error
  if (!dword_106BFF0) a1 = v39;       // strict: a1 = v39
  if (dword_106BFF0) v40 = 1;         // relaxed: force v40 = 1
  // BUT v40 was already 1 from Entry A (line 134)
  if (a1) emit_error(3542);           // base has no device_annotation
  else if (v58 && v40) emit_error(3547);  // base is HD
  else skip;                          // base is D-only (compatible)
```

Entry A sets `v39 = v56`, `v40 = 1`, `a1 = v56`. In strict mode, `a1` is overwritten to `v39` (same value). In relaxed mode, `a1` stays `v56` (same value). Either way, `a1 = v56 = (base has no device annotation)`. The `v40 = 1` from Entry A is preserved. The result is identical.

The relaxed flag introduces a second entry point (Entry B) for overriding functions with no explicit annotation. In relaxed mode, such functions are routed through LABEL\_83 with `v39 = 0` and `a1 = v56`, producing the same device-only check logic. In strict mode, the same functions take the direct H/implicit-H path and produce errors 3545/3546 for device/HD bases. Both paths reach the same conclusions.

### Relaxed Mode: The Unannotated Override Path

When `dword_106BFF0 == 1` and the overriding function has no `__device__` attribute, the checker takes an additional step before falling through to the H/implicit-H path. It queries the overriding symbol for explicit `__host__` (kind 86). If `__host__` IS found, the function is confirmed as explicit host and errors 3545/3546 apply normally. If `__host__` is NOT found (truly unannotated), the function is reclassified through the device-only check path (LABEL\_83). This reclassification does not change the error outcome -- an unannotated function overriding a host base still sees no error (both are host-space), and an unannotated function overriding a device or HD base still produces the appropriate error.

## Propagation Details

When the overriding function is implicitly HD (`byte_177 & 0x10`), execution space is propagated from the base to the derived entity by OR-ing bits into `byte_182`:

```c
// Propagation (direct from decompiled sub_432280, lines 77-91)
uint8_t base_es = overridden_entity->byte_182;

// If base is NOT device-only, derived inherits host obligation
if ((base_es & 0x30) != 0x20) {
    overriding_entity->byte_182 |= 0x10;   // host_explicit bit
    base_es = overridden_entity->byte_182;  // re-read (compiler artifact)
}

// If base has device_annotation, derived inherits device obligation
if (base_es & 0x20) {
    overriding_entity->byte_182 |= 0x20;   // device_annotation bit
}
```

The re-read of `overridden_entity->byte_182` after setting `0x10` on the **overriding** entity is a compiler artifact (the decompiler shows it reading back from `v10+182` into `v22`, but `v10` is the overridden entity, so the value hasn't changed). The OR operations are on the overriding entity only.

### Propagation Matrix

| Base space (`byte_182 & 0x30`) | Bits OR'd into overriding `byte_182` | Net effect on overriding entity |
|---|---|---|
| `0x00` (implicit H) | `\|= 0x10` | Becomes explicit host (`0x10`) |
| `0x10` (explicit H) | `\|= 0x10` | Becomes explicit host (`0x10`) |
| `0x20` (D only) | `\|= 0x20` | Becomes device-annotated (`0x20`) |
| `0x30` (HD) | `\|= 0x10`, then `\|= 0x20` | Becomes HD (`0x30`) |

After propagation, the overriding entity's `byte_182` accurately reflects the execution space obligations inherited from its base class. Downstream passes (device/host separation, IL marking, code generation) use this byte to determine whether the function needs device-side compilation, host-side compilation, or both.

## Relaxed Mode (dword\_106BFF0)

The global flag `dword_106BFF0` (`relaxed_attribute_mode`, default `1` per CLI defaults) controls permissive handling of execution space annotations across the compiler. Its primary effects are on attribute application (allowing `__device__` + `__global__` coexistence) and cross-space call validation. For virtual override checking, its effect is narrower:

1. **Unannotated override reclassification.** In relaxed mode, when the overriding function has neither `__device__` nor `__host__` attributes explicitly, the checker additionally queries the overriding symbol for `__host__` (kind 86). If `__host__` is NOT found, the checker treats the unannotated function as potentially device-compatible and routes through the device-only check path (LABEL\_83). This can produce error 3542 (D overrides H) for an implicit-host function, which would otherwise only see errors 3545/3546.

2. **No error suppression for overrides.** Unlike attribute application where relaxed mode suppresses error 3481, relaxed mode does NOT suppress any of the six override errors. All six fire at severity 8 in both modes. The flag `dword_106BFF0` modulates the *code path taken* to reach the error decision, not the severity or suppression of the error itself.

## Additional Override Checks (Non-CUDA)

After the CUDA execution space checks, `sub_432280` continues with standard EDG override validation:

| Error | Condition | Meaning |
|---|---|---|
| 1788 | Base has `[[nodiscard]]`, derived does not | Missing `[[nodiscard]]` on override |
| 1789 | Derived has `[[nodiscard]]`, base does not | Extraneous `[[nodiscard]]` on override |
| 1850 | Overriding a `final` virtual function | Override of `final` function |
| 2935 | Derived has requires-clause, base does not | Requires-clause mismatch |
| 2936 | Base has requires-clause, derived does not | Requires-clause mismatch |

These are standard C++ checks unrelated to CUDA execution spaces.

## Example: Override Interactions

```cuda
// Example 1: Legal same-space override
struct Base {
    __device__ virtual void f();
};
struct Derived : Base {
    __device__ void f() override;     // Legal: D overrides D
};

// Example 2: Error 3542 -- D overrides H
struct Base2 {
    virtual void f();                 // Implicit __host__
};
struct Derived2 : Base2 {
    __device__ void f() override;     // ERROR 3542 (20085)
};
// error #20085-D: execution space mismatch: overridden entity (Base2::f)
//   is a __host__ function, but overriding entity (Derived2::f)
//   is a __device__ function

// Example 3: Error 3546 -- H overrides HD
struct Base3 {
    __host__ __device__ virtual void f();
};
struct Derived3 : Base3 {
    void f() override;                // ERROR 3546 (20089)
};
// error #20089-D: execution space mismatch: overridden entity (Base3::f)
//   is a __host__ __device__ function, but overriding entity (Derived3::f)
//   is a __host__ function

// Example 4: Legal constexpr override with propagation
struct Base4 {
    __device__ virtual int g();
};
struct Derived4 : Base4 {
    constexpr int g() override;       // Legal: implicitly HD, propagates |= 0x20
};
// Derived4::g now has byte_182 |= 0x20 (device_annotation)
// and is included in device IL compilation.

// Example 5: Error 3547 -- D overrides HD
struct Base5 {
    __host__ __device__ virtual void h();
};
struct Derived5 : Base5 {
    __device__ void h() override;     // ERROR 3547 (20090)
};
```

## Function Map

| Address | Identity | Lines | Source |
|---|---|---|---|
| `sub_432280` | `record_virtual_function_override` | 437 | `class_decl.c` |
| `sub_5CEE70` | `find_attribute` (attribute list lookup by kind) | ~30 | `attribute.c` |
| `sub_4F4F10` | `emit_diag_with_entity_pair` (severity, error, loc, base, derived) | ~100 | `error.c` |
| `sub_4F2930` | `internal_error` (assertion failure) | ~20 | `error.c` |
| `sub_41A6E0` | `dump_override_entry` (debug trace helper) | ~40 | `class_decl.c` |
| `sub_41D010` | `add_to_override_list` | ~20 | `class_decl.c` |
| `sub_5E20D0` | `allocate_override_entry` (40-byte node) | ~15 | `mem.c` |
| `sub_432130` | `resolve_indeterminate_exception_specification` | ~60 | `class_decl.c` |

## Override Entry Structure

Each recorded override is stored as a 40-byte linked list node:

```
Override entry (40 bytes):
  +0x00 (0):   next pointer
  +0x08 (8):   base_class_symbol (entity in base class vtable)
  +0x10 (16):  derived_class_entity (overriding function entity)
  +0x18 (24):  flags (0 initially, set during processing)
  +0x20 (32):  covariant_return_adjustment (pointer or NULL)
```

The override list is managed via:
- `qword_E7FE98`: list head (most recent entry)
- `qword_E7FEA0`: free list head (recycled 40-byte entries)
- `qword_E7FE90`: allocation counter

When debug tracing is enabled (`dword_126EFCC > 3`), the function prints `"newly created: "`, `"existing entry: "`, `"after modification: "`, and `"removing: "` to stderr via `fwrite`, followed by calls to `sub_41A6E0` to dump the entry contents.

## Cross-References

- [Execution Spaces](../cuda/execution-spaces.md) -- bitfield layout at entity `+182`, attribute application handlers, conflict matrix
- [Cross-Space Call Validation](../cuda/cross-space-validation.md) -- call-graph enforcement, the implicitly-HD bypass
- [CUDA Error Catalog](../diagnostics/cuda-errors.md) -- error numbering scheme, diagnostic tag suppression system
- [Global Variables](../reference/global-variables.md) -- `dword_106BFF0` and other flags
- [Entity Node Layout](../structs/entity-node.md) -- full byte map of the entity structure including `+176`, `+177`, `+182`
- [\_\_global\_\_ Function Constraints](../attributes/global-function.md) -- why `__global__` functions cannot be virtual
