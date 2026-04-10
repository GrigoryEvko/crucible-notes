# Device/Host Separation

A single `.cu` file contains both host and device code intermixed. Conventional wisdom assumes cudafe++ splits them with two compilation passes -- one for host, one for device. That assumption is wrong. cudafe++ uses a **single-pass, tag-and-filter architecture**: the EDG frontend builds one unified IL tree from the entire translation unit, every entity gets execution-space bits written into its node, and then two separate output paths filter the tagged IL -- one path emits the `.int.c` host file, the other emits the device IL for cicc. There is no re-parse, no second invocation of the frontend.

This page documents the global variables that control the split, the IL-marking walk that selects device-reachable entries, the host-output filtering logic that suppresses device-only entities, and the output files produced.

## Key Facts

| Property | Value |
|---|---|
| Architecture | Single-pass: parse once, tag with execution-space bits, filter at output time |
| Language mode flag | `dword_126EFB4` -- language mode (`1` = C, `2` = C++) |
| Host compiler identity | `dword_126EFA4` -- clang mode; `dword_126EFA8` -- gcc mode |
| Device stub mode | `dword_1065850` -- toggled per-entity in `sub_47BFD0` (`gen_routine_decl`) |
| Device-only filter | `sub_46B3F0` -- returns 0 for device-only entities when generating host output |
| Keep-in-IL entry point | `sub_610420` (`mark_to_keep_in_il`), 892 lines |
| Keep-in-IL worker | `sub_6115E0` (`walk_tree_and_set_keep_in_il`), 4649 lines |
| Prune callback | `sub_617310` (`prune_keep_in_il_walk`), 127 lines |
| Host output entry point | `sub_489000` (`process_file_scope_entities`) |
| Host sequence dispatcher | `sub_47ECC0` (`gen_template` / top-level source sequence processor), 1917 lines |
| Routine declaration | `sub_47BFD0` (`gen_routine_decl`), 1831 lines |
| Host output file | `<input>.int.c` (transformed C++ for host compiler) |
| Device output file | Named via `--gen_device_file_name` CLI flag (binary IL for cicc) |
| Module ID file | Named via `--module_id_file_name` CLI flag |
| Stub file | Named via `--stub_file_name` CLI flag |

## Why Single-Pass Matters

Old NVIDIA documentation and third-party descriptions sometimes describe a "two-pass" compilation model where `cudafe++` runs once to extract device code and once to extract host code. This is not what the binary does. The evidence:

1. **One frontend invocation.** `sub_489000` (`process_file_scope_entities`) is called once. It walks the source sequence list (`qword_1065748`) a single time, dispatching each entity through `sub_47ECC0`.

2. **No re-parse.** The EDG frontend builds the IL tree in memory once. The keep-in-IL walk (`sub_610420`) runs during `fe_wrapup` pass 3, marking device-reachable entries with bit 7 of the prefix byte. The host backend then emits `.int.c` from the same IL tree, filtering based on execution-space bits.

3. **`dword_126EFB4` is a language mode, not a pass counter.** Its value `2` means "C++ mode," not "second pass." It never changes between device and host output phases.

4. **The device IL is a byte-level binary dump** of marked entries, not the output of a separate code-generation pass. The host output is a text-mode C++ file produced by the `gen_*` family of functions.

The practical implication: every CUDA entity exists once in memory with its execution-space tag at `entity+182`. The tag drives all downstream decisions -- what goes into device IL, what appears in host `.int.c`, what gets wrapped in `#if 0`, and what gets a kernel stub.

## Control Globals

### dword_126EFB4 -- Language Mode

| Value | Meaning |
|---|---|
| `0` | Unset / not initialized |
| `1` | C mode |
| `2` | C++ mode |

Set during CLI processing (`sub_45C200`, case 228/240/246/251/252 for C++ standard versions). In CUDA compilation this is always `2` because `.cu` files are compiled as C++. The keep-in-IL logic at `sub_610420` checks `dword_126EFB4 == 2` to decide whether to run the secondary routine-definition marking pass (`sub_6175F0`).

### dword_126EFA4 -- Clang Mode / Device Code Mode

This global has different semantics depending on context. In CLI processing (case 187), it records whether clang host compiler mode is active. In the template instantiation system (`p1.18` sweep), it acts as a device-code mode flag (`1` = device code path, `0` = host stubs). The dual use reflects the fact that cudafe++ reuses the same global for different phases.

### dword_126EFA8 -- GCC Mode / GPU Compilation Mode

Set when gcc host compiler mode is active. In template-related code paths, a nonzero value indicates GPU compilation mode is enabled.

### dword_1065850 -- Device Stub Mode Toggle

This global flag controls how `__global__` kernel bodies are emitted. It is toggled inside `gen_routine_decl` (`sub_47BFD0`). The toggle mechanism is a self-inverting flip that causes `gen_routine_decl` to process each `__global__` kernel TWICE. Because the toggle fires at the TOP of the function (before body emission), the first call (0->1) emits the static stub definition, and the recursive call (1->0) emits the forwarding body.

#### Toggle Pseudocode (from sub_47BFD0, decompiled line 551-553)

```c
// v3 = entity pointer, v8 = is_friend flag
uint64_t flags = *(uint64_t*)(entity + 176);    // 8-byte flags field

// Bitmask 0x40000002000000 combines the __global__ attribute bit (0x40000000000000)
// and a definition/linkage flag (0x2000000) from the entity's flags field at +176.
if ((flags & 0x40000002000000) == 0x40000002000000 && !is_friend)
    dword_1065850 = (dword_1065850 == 0);   // flip: 0->1 or 1->0
```

This toggle fires at the TOP of `gen_routine_decl`, before either stub variant is emitted. Because the function calls itself recursively at the end (decompiled line 1821: `return sub_47BFD0(v152, a2)`), the toggle fires again on re-entry, resetting the flag.

#### Body Emission Decision (decompiled line 1421-1432)

The actual stub body selection happens later in the function, based on the CURRENT value of `dword_1065850` (which has already been toggled):

```c
if ((entity->byte_182 & 0x40) != 0) {       // has __global__ annotation
    char has_body = entity->byte_179 & 0x02;  // has a definition

    if (dword_1065850) {
        // First call (toggle 0->1): emit static stub with cudaLaunchKernel placeholder
        if (!is_specialization && has_body) {
            emit("{ ::cudaLaunchKernel(0, 0, 0, 0, 0, 0);}");
        }
    } else if (has_body) {
        // Recursive call (toggle 1->0): emit forwarding stub
        emit("{");
        emit_scope_qualifier(entity);
        emit("__wrapper__device_stub_");
        emit(entity->name);
        emit_template_args_if_needed(entity);
        emit_parameter_forwarding(entity);
        emit(");return;}");
    }
    // Both invocations: wrap original body in #if 0 / #endif
}
```

#### Self-Recursion (decompiled line 1817-1821)

After the first call emits the static stub, the function checks whether `dword_1065850` is nonzero (the toggle set it to 1). If so, it restores the source sequence pointer and calls itself:

```c
if (dword_1065850) {
    qword_1065748 = saved_source_sequence;
    return sub_47BFD0(context, a2);   // recursive self-call
}
```

The recursive invocation toggles `dword_1065850` back to 0, emits the forwarding body, and returns without further recursion (since `dword_1065850 == 0` at the self-recursion check).

The flag is also set in `sub_47ECC0` when processing template instantiation directives (source sequence kind 54): if the entity has `byte_182 & 0x40` (device/global annotation) and CUDA language mode is active, `dword_1065850` is set to `1` before emitting the instantiation directive.

### dword_126EBA8 -- Language Standard Mode

Value `1` indicates C language standard mode. The device-only filtering function `sub_46B3F0` references this to determine whether EBA (EDG binary archive) mode applies.

## Host-Output Filtering: sub_46B3F0

This compact function (39 lines decompiled) is the gatekeeper that determines whether an entity should be emitted in the host `.int.c` output. It is called from `sub_47ECC0` at the point where the host backend decides whether to emit a type/variable declaration or wrap it in `#if 0`.

### Decompiled Logic

```c
// sub_46B3F0 -- returns 0 to suppress (device-only), nonzero to emit
uint64_t sub_46B3F0(entry *a1, entry *a2) {
    char kind = a1->byte_132;

    // Classes, structs, unions (kind 9-11): always check device-only
    if ((unsigned char)(kind - 9) <= 2)
        goto check_device_flag;

    // Enums (kind 2): check if scoped enum is device-only
    if (kind == 2) {
        if ((a1->byte_145 & 0x08) == 0)  // not an enum definition
            return 1;                      // emit it
        goto check_device_flag;
    }

    // Typedefs (kind 12): check underlying type kind
    if (kind == 12) {
        char underlying = a1->byte_160;
        if (underlying > 10)
            return 0;
        // Magic bitmask: 0x71D = 0b11100011101
        // Bits set for kinds 0,2,3,4,8,9,10 -> emit
        return (0x71DULL >> underlying) & 1;
    }

    return 1;  // everything else: emit

check_device_flag:
    int is_device;
    if (a2)
        is_device = a2->byte_49 & 1;
    else
        is_device = a1->byte_135 >> 7;

    if (!is_device)
        return 0;   // not device-related, suppress? (inverted logic)

    // Device entity: check if it should still be emitted
    return dword_126EBA8           // C mode -> emit anyway
        || (kind - 9) > 2         // not a class/struct/union -> emit
        || *(a1->ptr_152 + 89) != 1;  // scope check
}
```

The function uses a bitmask trick (`0x71D >> underlying_kind`) to quickly determine which typedef underlying types pass the filter. The bit pattern `0b11100011101` selects kinds 0 (void/basic), 2 (enum), 3 (parameter), 4 (pointer), 8 (field), 9 (class), and 10 (struct).

### Where It Is Called

In `sub_47ECC0` (the master source-sequence dispatcher), when processing type declarations (kind 6):

```c
case 6:  // type_decl
    sub_4864F0(recursion_level, &continuation, kind_byte);
    if (!recursion_level && !sub_46B3F0(type_entry, scope_entry)) {
        // Entity is device-only in host context
        // Wrap in #if 0 / #endif
    }
```

This is the mechanism that makes device-only classes, structs, and enums invisible to the host compiler. They still exist in the IL tree (and participate in the keep-in-IL walk for device output), but their text representation is suppressed in `.int.c`.

## Device-Only Suppression in Host Output

When `sub_46B3F0` returns 0 for an entity, or when the execution-space check in `gen_routine_decl` identifies a device-only function, the host backend wraps the declaration in preprocessor guards:

```c
#if 0
__device__ void device_only_function() {
    // ... original body ...
}
#endif
```

This pattern appears in three locations:

1. **Type declarations** -- `sub_47ECC0` wraps device-only types via `sub_46B3F0` check.

2. **Routine declarations** -- `sub_47BFD0` checks `entity->byte_81 & 0x04` (has device scope) combined with execution-space bits at `entity+182`. When a function is device-only and the current output track is host, the function body is suppressed.

3. **Lambda bodies** -- `sub_47B890` (`gen_lambda`) wraps device lambda bodies in `#if 0` / `#endif` and emits `__nv_dl_wrapper_t` wrapper types instead.

### The nv_is_device_only_routine Check

The inline predicate from `nv_transforms.h:367` is the canonical way to test if a routine lives exclusively in device space:

```c
bool nv_is_device_only_routine(entity *e) {
    char byte = e->byte_182;
    return ((byte & 0x30) == 0x20)    // device annotation, no host
        && ((byte & 0x60) == 0x20);   // device, not __global__
}
```

The double-mask check distinguishes three cases:
- `(byte & 0x30) == 0x20`: has `__device__` but not `__host__` (bits 4-5)
- `(byte & 0x60) == 0x20`: has `__device__` but not `__global__` (bits 5-6)

A `__global__` function fails the second test because bit 6 is set (`byte & 0x60 == 0x60`). This matters because `__global__` functions ARE emitted in host output -- as stubs that call `__wrapper__device_stub_<name>`.

## The Keep-in-IL Walk (Device Code Selection)

The keep-in-IL mechanism runs during `fe_wrapup` pass 3 and selects which IL entries belong to the device output. The full details are documented in the [Keep-in-IL](../il/keep-in-il.md) page; this section covers the aspects relevant to device/host separation.

### Call Chain

```
sub_610420 (mark_to_keep_in_il)
  |
  +-- installs pre_walk_check = sub_617310 (prune_keep_in_il_walk)
  +-- walks file-scope IL via sub_6115E0 (walk_tree_and_set_keep_in_il)
  |     |
  |     +-- for each child entry:
  |           *(child - 8) |= 0x80    // set bit 7 = keep_in_il
  |           recurse into child
  |
  +-- if dword_126EFB4 == 2 (C++ mode):
  |     sub_6175F0 (walk_scope_and_mark_routine_definitions)
  |
  +-- iterates 45+ global entry-kind linked lists
  +-- processes using-declarations (fixed-point loop)
```

### The Keep Bit

Every IL entry has an 8-byte prefix. Bit 7 (0x80) of the byte at `entry_ptr - 8` is the keep-in-IL flag:

```
Byte at (entry_ptr - 8):
  bit 0  (0x01)  is_file_scope
  bit 1  (0x02)  is_in_secondary_il
  bit 2  (0x04)  current_il_region
  bits 3-6       reserved
  bit 7  (0x80)  keep_in_il          <<<< THE DEVICE CODE MARKER
```

The sign bit doubles as the flag, enabling a fast test: `*(signed char*)(entry - 8) < 0` means "keep." The recursive worker `sub_6115E0` sets this bit on every reachable sub-entry by ORing `0x80` into the prefix byte and recursing.

### Transitive Closure

The walk implements a transitive closure: if a `__device__` function references a type, that type gets marked, which transitively marks its member types, base classes, template parameters, and any routines they reference. The prune callback (`sub_617310`) prevents infinite loops by returning 1 (skip) when an entry already has bit 7 set.

Additional "keep definition" flags exist for deeper marking:

| Entity | Field | Bit | Effect |
|---|---|---|---|
| Type (class/struct) | `entry + 162` | bit 7 (0x80) | Retain full class body, not just forward decl |
| Routine | `entry + 187` | bit 2 (0x04) | Retain function body |

### Seed Entries

The walk starts from entities already tagged with execution-space bits. These seeds include:
- Functions with `__device__` or `__global__` at `entity+182`
- Variables with `__shared__`, `__constant__`, or `__managed__` memory space attributes
- Extended device/host-device lambdas

Everything reachable from a seed gets the keep bit. Everything without the keep bit is eliminated from the device IL by the elimination pass (`sub_5CCBF0`).

## __host__ __device__ Functions

Functions annotated with both `__host__` and `__device__` have bits 4 and 5 set in `entity+182`, producing `(byte & 0x30) == 0x30`. These functions participate in BOTH output paths:

1. **Host output (.int.c):** The function passes the `nv_is_device_only_routine` check (it returns false because bit 4 is set alongside bit 5). The function body is emitted normally -- no `#if 0` wrapping, no stub substitution.

2. **Device IL:** The keep-in-IL walk marks the function and all its dependencies because it has device-capable bits set. The full function body is retained in the device IL.

This dual inclusion is why `__host__ __device__` functions must be valid C++ in both execution contexts. They are compiled once by EDG, then the same IL is consumed by both the host compiler (via `.int.c` text) and cicc (via binary IL).

### Template Instantiation Interaction

When `sub_47ECC0` processes a template instantiation directive (source sequence kind 54) for a `__host__ __device__` template, it does NOT set `dword_1065850`. The stub mode toggle only activates for entities with `byte_182 & 0x40` (the `__global__` kernel bit). Host-device functions get their bodies emitted directly in both tracks.

## Output Files

cudafe++ produces up to four output files from a single compilation:

### 1. Host C++ File (.int.c)

Generated by `sub_489000` (`process_file_scope_entities`). The filename is derived from the input: `<input>.int.c`, or stdout if the output name is `"-"`.

Contents:
- Pragma boilerplate (`#pragma GCC diagnostic ignored ...`)
- Managed runtime initialization (`__nv_init_managed_rt`, `__nv_fatbinhandle_for_managed_rt`)
- Lambda macro definitions (`__nv_is_extended_device_lambda_closure_type`, etc.)
- `#include "crt/host_runtime.h"` (injected when first CUDA-tagged type is encountered)
- All host-visible declarations with device-only entities wrapped in `#if 0`
- Kernel functions replaced with forwarding stubs to `__wrapper__device_stub_<name>`
- Registration tables (`sub_6BCF80` called 6 times for device/host x managed/constant combinations)
- Anonymous namespace macro (`_NV_ANON_NAMESPACE`)
- Original source re-inclusion (`#include "<original_file>"`)

### 2. Device IL File

Named via `--gen_device_file_name` CLI flag (flag index 85). Contains the binary IL for all entries that passed the keep-in-IL walk. This file is consumed by cicc (the CUDA IL compiler).

### 3. Module ID File

Named via `--module_id_file_name` CLI flag (flag index 87). Contains the CRC32-based unique identifier for this compilation unit, computed by `make_module_id` (`sub_5B5500`). Used to prevent ODR violations across separate compilation units in RDC mode.

### 4. Stub File

Named via `--stub_file_name` CLI flag (flag index 86). Contains the `__wrapper__device_stub_<name>` function definitions that bridge host-side kernel launch calls to the CUDA runtime.

## Kernel Stub Generation

For `__global__` kernel functions, the host output replaces the original body with two stub forms. The toggle `dword_1065850` flips 0->1 at the top of `gen_routine_decl`, so the static definition is emitted first, followed by the forwarding body from the recursive call:

```c
// Output 1 (dword_1065850 == 1 after toggle, emitted first):
static void __wrapper__device_stub_kernel_name(params) {
    ::cudaLaunchKernel(0, 0, 0, 0, 0, 0);
}
#if 0
<original body>
#endif

// Output 2 (dword_1065850 == 0 after toggle, emitted by recursive call):
void kernel_name(params) {
    <scope>::__wrapper__device_stub_kernel_name(params);
    return;
}
#if 0
<original body>
#endif
```

The static stub provides the definition of `__wrapper__device_stub_` that the forwarding body calls. The `cudaLaunchKernel(0, 0, 0, 0, 0, 0)` placeholder creates a linker dependency on the CUDA runtime without performing an actual kernel launch.

For template kernels, the forwarding stub includes explicit template arguments: `__wrapper__device_stub_kernel_name<T1, T2, ...>(params)`. For full details see [Kernel Stubs](./kernel-stubs.md).

## Architectural Diagram

```
                        .cu source
                            |
                     EDG Frontend (parse once)
                            |
                     Unified IL Tree
                    (all entities tagged
                     at entity+182)
                            |
              +-------------+-------------+
              |                           |
        fe_wrapup pass 3           Backend (sub_489000)
     mark_to_keep_in_il            walks source sequence
      (sub_610420)                       |
              |                    sub_47ECC0 per entity
        set bit 7 on                     |
        device-reachable          +------+------+
        entries                   |             |
              |              sub_46B3F0    sub_47BFD0
        Device IL output    returns 0?    __global__?
        (binary, for cicc)       |             |
                            #if 0/endif   stub body
                            wrap it       replacement
                                  |             |
                                  +------+------+
                                         |
                                   .int.c output
                                 (text C++ for host
                                  compiler)
```

## Function Map

| Address | Name | Lines | Role |
|---|---|---|---|
| `sub_489000` | `process_file_scope_entities` | 723 | Backend entry point, `.int.c` emission |
| `sub_47ECC0` | `gen_template` (source sequence dispatcher) | 1917 | Dispatches each entity; calls `sub_46B3F0` for type filtering |
| `sub_47BFD0` | `gen_routine_decl` | 1831 | Routine declaration/definition; toggles `dword_1065850` |
| `sub_46B3F0` | device-only type filter | 39 | Returns 0 for device-only entities in host output |
| `sub_610420` | `mark_to_keep_in_il` | 892 | Top-level device IL marking entry point |
| `sub_6115E0` | `walk_tree_and_set_keep_in_il` | 4649 | Recursive worker that sets bit 7 on reachable entries |
| `sub_617310` | `prune_keep_in_il_walk` | 127 | Pre-walk callback; skips already-marked entries |
| `sub_6175F0` | `walk_scope_and_mark_routine_definitions` | 634 | Additional pass for C++ routine definitions |
| `sub_47B890` | `gen_lambda` | 336 | Lambda wrapper generation; `#if 0` for device lambda bodies |
| `sub_4864F0` | `gen_type_decl` | 751 | Type declaration emission; host runtime injection |
| `sub_5CCBF0` | `eliminate_unneeded_il_entries` | 345 | Elimination pass (removes entries without keep bit) |

## Cross-References

- [Execution Spaces](./execution-spaces.md) -- byte `+182` bitfield encoding for `__host__`/`__device__`/`__global__`; the `nv_is_device_only_routine` predicate that drives host-output filtering
- [Kernel Stubs](./kernel-stubs.md) -- detailed stub generation logic: forwarding body (pass 1) and static cudaLaunchKernel body (pass 2)
- [Keep-in-IL](../il/keep-in-il.md) -- full documentation of the device code marking walk, the keep bit at `entry_ptr - 8`, and the transitive closure algorithm
- [Memory Spaces](./memory-spaces.md) -- variable-side `__device__`/`__shared__`/`__constant__` at entity+148; these are the seed entries for the keep-in-IL walk
- [.int.c File Format](../output/int-c-format.md) -- structure of the generated host translation file
- [Entity Node Layout](../structs/entity-node.md) -- full byte map of the entity structure including offset +176 (flags field) and +182 (execution space byte)
