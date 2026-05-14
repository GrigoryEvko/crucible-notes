# TILEIR_CALLBACKS ABI

## Abstract

Tileiras emits a callback ABI that lets a runtime shim discover and patch TileIR launch hooks by symbol name. The
ABI is not debug logging — it is a compile-time-inserted module table, a pre-load trampoline, a per-function
callback table, and an argument-change callback the runtime can invoke when kernel launch arguments or TMA
descriptors change.

The public symbol set is:

| Symbol | Role |
| --- | --- |
| `__CUDA_TILEIR_CALLBACKS` | Module-level ABI entry vector. |
| `__CUDA_TILEIR_ON_PRE_LOAD` | Compiler-emitted function returning the module callback vector. |
| `__CUDA_TILEIR_CALLBACKS_ON_PRE_LOAD` | Weak runtime-patched pre-load callback slot. |
| `__CUDA_TILEIR_FUNC_CALLBACKS` | Per-function callback table, fixed 64 bytes. |
| `__CUDA_TILEIR_FUNC_ON_ARGUMENTS_CHANGE` | Per-function hook called when launch arguments are updated. |

## Global linkage tuples

Every `__CUDA_TILEIR_*` global is created with an `(isConstant, linkage)` tuple at definition time. The driver
loader inspects those tuples to decide which symbols it may claim and which slots are mandatory. Linkage codes are
LLVM's: `0` denotes `External` (strong), `7` denotes `Weak` / `WeakODR` (optional override).

| Global | Size | `(isConstant, linkage)` | Notes |
| --- | ---: | --- | --- |
| `__CUDA_TILEIR_CALLBACKS` | 9 × i64 = 72 B | `(1, 0)` — const, External | Top-level callback table. |
| `__CUDA_TILEIR_FUNC_CALLBACKS` | 8 × i64 = 64 B (fixed) | `(1, 0)` — const, External | Per-function callback table. Fixed 64 B, one global per kernel. |
| `__CUDA_TILEIR_CALLBACKS_ON_PRE_LOAD` | 7 × i64 = 56 B | `(0, 7)` — mutable, Weak/WeakODR | Optional pre-load hook table. |

The 64-byte size of `__CUDA_TILEIR_FUNC_CALLBACKS` is fixed at the type level. The pass emits one such global per
kernel function in the module, and the struct shape inside each global is constant. An older note that called
the layout variable-size with one slot per emitted func-callback is wrong; the struct shape is pinned and
populated by exactly eight `insertvalue` operations at indices 0..7.

## Callback vector ABI

The module-level `__CUDA_TILEIR_CALLBACKS` global is the entry vector — a constant external object with nine
64-bit slots, total size 72 bytes. The current ABI revision is the integer `0x40`, decimal `64`. The
binary stores this value as the immediate `0x40`; the two forms are numerically identical and both are correct
references to the same revision.

| Slot | u64 | Semantic |
| ---: | --- | --- |
| 0 | `__cuda_tileir_init` fn pointer | Called on dlopen. |
| 1 | `__cuda_tileir_fini` fn pointer | Called on dlclose. |
| 2 | `__cuda_tileir_compile_begin` fn pointer | |
| 3 | `__cuda_tileir_compile_end` fn pointer | |
| 4 | `ABI_REVISION = 0x40` | Verified at load. |
| 5 | reserved (zero) | |
| 6 | MUL multiplier A | NOT a flag — see correction below. |
| 7 | MUL multiplier B | NOT a flag — see correction below. |
| 8 | sentinel | Always zero; marks end of table. |

```c
typedef struct TileirCallbackVector {
    uint64_t init_fn;
    uint64_t fini_fn;
    uint64_t compile_begin_fn;
    uint64_t compile_end_fn;
    uint64_t abi_revision;      /* = 0x40 */
    uint64_t reserved_zero;
    uint64_t mul_multiplier_a;
    uint64_t mul_multiplier_b;
    uint64_t sentinel_zero;
} TileirCallbackVector;
```

## Slots 6 and 7 are MUL multipliers, not flags

Earlier wiki revisions treated slots 6 and 7 as OR-able bit flags. They are not. The body emitter at
`sub_870430` multiplies a runtime counter by the slot value and writes the product back into the lowered IR. The
multiplication routes through `sub_868170`, the `llvm.mul` helper. In the generated IR the operation appears as

```llvm
%v = mul i64 %counter, <slot value>
```

with `<slot value>` taken verbatim from slot 6 or slot 7. Treating these slots as OR-combined flag bits would
silently miscompile every program that sets more than one — `a | b` and `a * b` agree only on single-bit values.
Reimplementations must preserve the multiplicative semantics.

## Body emission

Three sub-emitters cooperate to produce the callback objects:

- `sub_8689C0` (4 293 B) emits the per-revision constant data block — the nine `i64` slots for revision `0x40`,
  including the addresses of `__CUDA_TILEIR_FUNC_CALLBACKS` and `__CUDA_TILEIR_ON_PRE_LOAD`.
- `sub_86DAD0` (~22 KB) emits the full callback dispatch trampoline including the type converter and the
  `nvvm.kernel` attribute lift. The related `cute.kernel` -> `nvvm.kernel` rename is performed by the downstream
  D08 pattern `CuteKernelToNvvmRewrite` at `sub_1698C20`; it is not part of the callback ABI itself.
- `sub_870430` is the body emitter that wires the MUL multipliers from slots 6 and 7 into the dispatch path via
  `sub_868170` (the `llvm.mul` helper).

## ON_PRE_LOAD trampoline

At each TileIR launch site, the compiler can emit a pre-load callback call. The runtime-patched symbol is weak and
null-guarded, so binaries remain executable even when no runtime shim has installed a callback.

```c
void maybe_call_on_pre_load(void *arg_desc, int64_t sm_num, void *tma_arena) {
    TileirOnPreLoadSlot *slot = &__CUDA_TILEIR_CALLBACKS_ON_PRE_LOAD;
    if (slot == NULL || slot->fn == NULL)
        return;

    slot->fn(arg_desc, sm_num, tma_arena);
}
```

The argument descriptor is an 11-slot, 88-byte block. The callback receives the descriptor pointer, the
sign-extended SM number, and a pointer to the TMA descriptor arena.

## Callback signatures

| Callback | C signature | Calling convention | Who emits | Who calls |
| --- | --- | --- | --- | --- |
| `__CUDA_TILEIR_ON_PRE_LOAD` | `TileirCallbackVector (*)(void)` | cdecl, no args | compiler | driver at module load |
| `(*__CUDA_TILEIR_CALLBACKS_ON_PRE_LOAD.fn)` | `void (*)(void *arg_desc, int64_t sm_num, void *tma_arena)` | cdecl, 3 args | runtime slot | compiled launch sites |
| `__CUDA_TILEIR_FUNC_ON_ARGUMENTS_CHANGE` | `int32_t (*)(void *cookie, void *arg_buf, void *tma_arena, <kernel args...>)` | cdecl, 3+N args | compiler | runtime on argument-buffer change |

`__CUDA_TILEIR_FUNC_ON_ARGUMENTS_CHANGE` returns `i32`, not `void`. Its first three arguments are pointers — a
runtime cookie, an argument-buffer pointer, a TMA-arena pointer. The user kernel arguments follow that prefix in
their lowered kernel ABI order.

## TMA descriptor shape

Each TMA argument occupies 64 bytes — eight 64-bit words. That shape matches the SM90+ tensor-map descriptor used
by `cp.async.bulk.tensor`. Debug printing splits the descriptor into two four-word rows for readability, but the
storage object is one contiguous 64-byte descriptor.

```c
typedef struct TileirTmaDescriptor {
    uint64_t word[8];
} TileirTmaDescriptor;

typedef struct TileirTmaArena {
    uint64_t header[2];
    TileirTmaDescriptor descriptor[8];
} TileirTmaArena;
```

Device-side descriptor storage must be 64-byte aligned. The compiler relies on the runtime to provide a correctly
aligned arena. Non-SM90 targets take the zero path and do not require a populated TMA arena.

## Version and backwards-compatibility handling

Revision tracking lives in slot 4 of the module vector. The compiler writes revision `0x40` (= 64 decimal)
unconditionally. The runtime checks both the sentinel zero in slot 8 and the revision in slot 4 before installing
hooks. No versioned alias symbols like `__CUDA_TILEIR_CALLBACKS_v1` exist, so every compatibility check has to go
through the vector contents.

The weak runtime-patched pre-load slot and the null guard form the primary backwards-compatibility mechanism.
Older runtimes can leave the slot unresolved or null, and the compiled launch site simply skips the callback. The
per-function callback table is `(1, 0)` const+External and fixed at 64 bytes; absent specialized hooks, the
argument-change hook is the default callback target.

