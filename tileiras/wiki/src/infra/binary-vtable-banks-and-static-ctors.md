# Binary Vtable Banks + Static Ctors

## Abstract

The `tileiras` ELF uses conventional C++ static registration heavily. MLIR pass models, rewrite patterns, dialect descriptors, op interfaces, NVPTX register classes, and target-lowering hooks all publish through vtables or static constructors before normal compilation starts. This page documents the runtime shape of that registration layer without treating raw table addresses as part of the public API.

## PassConcept vtable shape

Every MLIR new-PM `PassModel<PassT>` instantiation uses the same `PassConcept<PassT>` shape: Itanium ABI prefix, destructor pair, `run` wrapper, name printer, `isRequired` hook, and tail destructor. The uniform shape is what lets MLIR store arbitrary pass models behind one concept pointer while still dispatching to the typed pass implementation.

| Slot | Role |
|---|---|
| 0 | typeinfo pointer, null in this no-RTTI build |
| 1 | typeinfo extension word, null in this no-RTTI build |
| 2 | deleting destructor |
| 3 | non-virtual destructor body |
| 4 | `run(IRUnitT&, AM&)` wrapper |
| 5 | `name()` printer trampoline |
| 6 | `isRequired()` |
| 7 | tail `~PassModel()` |

The `run` wrapper adjusts from the erased `PassConcept` base to the typed `PassT` object, calls the real pass body, and returns the model pointer. For a reimplementation only the ownership-and-dispatch contract matters: a pass instance must retain enough typed state for its `run`, `name`, and `isRequired` hooks to agree.

## RewritePattern tables

Rewrite patterns split into two shapes: conversion patterns and plain rewrite patterns. Conversion patterns share a generic rewrite driver that delegates to the typed `matchAndRewrite`; plain patterns have a smaller table and often use the default no-op `match` hook. Pattern identity is carried by the op name, benefit, context pointer, and typed rewrite hook — not by a binary table address.

## Dialect vtables

Every MLIR `Dialect` subclass uses the same dialect ABI: destructor pair, canonicalization-pattern hook, constant materializer, attribute parser/printer, type parser/printer, and region/op attribute verifiers.

| Dialect | Distinctive behavior |
|---|---|
| `nv_tileaa` | Installs the inliner interface used by the alias-analysis layer. |
| `cutlass` | Disables textual attribute/type parsing for unsupported forms. |
| `cute_nvgpu` | Provides the non-trivial textual type printer for GPU atom types. |
| `cute` | Relies heavily on generic ODS assembly behavior. |
| `cutlass.seq_bar` family | Dense op-model family for sequence-barrier operations. |

Dialect construction is registration-heavy: the constructor installs namespace, TypeID, attribute/type parsers, op interfaces, and dialect interfaces as one coherent unit. A reimplementation should reproduce the observable dialect behavior and parser/printer hooks, not the original table layout.

## NVPTX register-class descriptors

The NVPTX backend ships declared-pool `TargetRegisterClass` descriptors for PTX register pools: `%p`, `%rs`, special registers, `%r`, `%f`, `%rd`, and `%rq`. These descriptors are pure data: MC register class pointer, subclass masks, allocatable bit, superclass list, and related metadata. The asm printer reads them to choose declarations such as `.reg .b<width> %<prefix><N>;`.

## NVPTXTargetLowering

`tileiras` carries the normal LLVM `SelectionDAG` target-lowering surface for NVPTX. Key hooks: `LowerFormalArguments`, `LowerCall`, `LowerOperation`, and the load/vector helper path. Slot-by-slot behavior is covered in `codegen/nvptx-target-lowering-call-and-args.md`; this page only records that the target-lowering surface is a conventional LLVM virtual interface.

## Static Constructors

The binary has hundreds of static constructors. Each body falls into one of three useful categories:

- `cl::opt<>` registrars that publish command-line options, help text, defaults, and flags.
- `TypeID::get<T>()` static-local initializers guarded by the Itanium ABI guard protocol.
- Dispatch-table initializers that install vtables, op interfaces, or dialect interface records.

For reimplementation, constructor order matters only where later code expects a registry to be populated before first use. The durable behavior is the registry side effect, not the original constructor body address.

## Per-Dialect Ctor Chain

A `.ctors` table at `0x591CE78..0x591E2F0` lists all 653 ctor bodies — a `void(*)()[]` array of function pointers walked by `_start` before `main` runs. Each entry is a `__cxa_atexit`-registered ctor. The order is established at link time and matches the order of declarations across the source units; the dependency-ordered listing matches the actual link order.

Six of those ctors initialise the six in-binary dialects, and the order over those six is not incidental. Each later dialect ctor reads types, attributes, or interface records that a previous ctor registered, so swapping the order would observe partially-populated registries during construction. The chain is strict, single-threaded, and runs to completion before any user code touches the dialect registry.

| Order | Dialect | Ctor | Notes |
|---|---|---|---|
| 1 | nv_tileaa | `sub_1545E80` | Lowest dialect; registers Type / Attribute / OperationName slabs first |
| 2 | nv_tileas | `sub_147EC90` | Depends on nv_tileaa for token types |
| 3 | (intermediate) | `sub_153EC20` | Registers shared interfaces (FunctionOpInterface, SymbolTable, LoopLikeOpInterface) |
| 4 | CutlassDialect | `sub_17640C0` | Depends on the shared interfaces |
| 5 | CuteNvgpuDialect | `sub_17D1190` | Depends on CutlassDialect for pipeline ops |
| 6 | CuteDialect | `sub_1928370` | Highest dialect; depends on all the above |

The `cuda_tile` dialect registers separately — it is the public input dialect and has its own ctor chain via the dialect-target registry, registered through `RegisteredDialect` at `sub_6B3ED0`. It does not appear in the six-step chain above because the chain only covers in-binary dialects whose ctors emit registration calls into the global `MLIRContext` table; `cuda_tile` is published into the target registry instead.

## `__cxa_atexit` and the XOR-3 Pool Exception

Most of the 653 ctors register a corresponding `__cxa_atexit` dtor for ordered teardown — but the XOR-3-encrypted `.data` pools (mnemonic and register-name pools — see [Data Section Decryption](data-section-decryption.md) for the cipher and decoders, and `codegen/asm-printer-monster-and-windows.md` for the AsmWriter consumer) do not register dtors. The pools are zeroed at static-init, decoded at first use via `pthread_once`, and never re-encoded. The omission is deliberate: pools sit memory-mapped read-only after the first use, so re-encrypting them on shutdown is pointless and a no-op dtor would only add wasted entries to the exit chain.

The init-order over the six dialects also lines up with which dialects use `pthread_once` guards versus eager static-init. Only the shared-interfaces step at order 3 is gated by a one-shot guard — the interfaces it publishes are queried lazily on first use, so the ctor stages a `pthread_once_t` slot rather than running registration immediately. The other five dialects run their entire registration at static-init and need no one-shot guard.

| Ctor | sub_ADDR | Dialect | pthread_once slot |
|---|---|---|---|
| ctor_001 | `sub_1545E80` | nv_tileaa | `dword_5B6A640` (not used; nv_tileaa has eager init) |
| ctor_002 | `sub_147EC90` | nv_tileas | (eager) |
| ctor_003 | `sub_153EC20` | shared interfaces | `dword_5B37670` (FunctionOpInterface guard) |
| ctor_004 | `sub_17640C0` | CutlassDialect | (eager) |
| ctor_005 | `sub_17D1190` | CuteNvgpuDialect | (eager) |
| ctor_006 | `sub_1928370` | CuteDialect | (eager) |

## Referenced Ctor Bodies at `0x46xxxxx`

Of the 653 total ctor pointers, only 49 are referenced from elsewhere in the binary — the rest are template instantiations of upstream LLVM/MLIR static-init that fire once and never get named again. The 49 referenced ctors split into a small handful of roles: the six dialect ctors above, twelve `cl::opt` registrations, eight pass registrations, eleven `TypeID`-singleton initialisers, four `raw_ostream` sinks, three fingerprint-singleton initialisers, and five miscellaneous singletons. A reimplementation only needs to reproduce those 49 effects in a clean way; the unreferenced 604 are link-order noise from upstream with no observable post-init behavior in tileiras.

## Reimplementation Notes

```text
startup():
    register_command_line_options()
    register_type_ids()
    register_dialects()
    register_passes()
    register_rewrite_patterns()
    register_nvptx_target_lowering()
```

The registration graph should be explicit in a clean implementation. Avoid making address-contiguous table placement part of the design; it is an artifact of the original linker layout, not a semantic requirement.
