# MLIR Infrastructure Overview

## Abstract

TileIR rides on top of a standard MLIR substrate that the whole compiler shares: 0x48-byte `Operation` headers, a two-level `StorageUniquer` that interns every Type and Attribute, an `InterfaceMap` keyed on TypeID sentinel addresses, four rewrite-pattern shapes A/B/C/D at 0x60 / 0x68 / 0x70 / 0x78 bytes, an 808-byte `AsyncValueImpl` that backs every `Pipe_` and `Mutex_` scheduling value, and a 208-byte `Diagnostic` body with a 4-slot inline argument buffer. There is one walker driver, one pattern application loop, one uniquer gateway, and one diagnostic engine for the entire toolchain.

The substrate is statically linked once and shared by `cuda_tile`, `nv_tileas`, `nv_tileaa`, `cute`, `cute_nvgpu`, `cutlass`, `nvvm`, `llvm`, and the standard builtin / func / arith / scf / vector / memref / cf / math / pdl dialects. This page is the router into the deep pages — each section names one topic and points at the page that covers it field-by-field.

## Reading Path

The pages below assume each other in roughly the following order. Read the operation layout first to understand what an MLIR node looks like in memory, then the storage uniquer to see how types and attributes interleave with operations, then the container fingerprints page to recognise every map and set in the binary.

| Topic | Owner page |
|---|---|
| Operation header, region traversal, walker driver | [Operation Layout](operation-layout.md) |
| Two-level uniquer, EMPTY/TOMBSTONE sentinels, fmix64 hash, context-impl rwlock | [Storage Uniquer and Context Impl](storage-uniquer-and-context-impl.md) |
| Pattern shapes A/B/C/D, FrozenRewritePatternSet, fingerprint hashmap | [Pattern Vtables and Shapes](pattern-vtables-and-shapes.md) |
| Interface vtables, concept tables, InterfaceMap probing | [Interface Vtables](interface-vtables.md) |
| TypeID idioms, .bss sentinel bands, Meyers-cached idiom | [TypeID Sentinels and Anchors](typeid-sentinels-and-anchors.md) |
| DenseMap, SwissTable, SmallVector fingerprints and resize policies | [Container Fingerprints](container-fingerprints.md) |
| `AsyncValueImpl` 808-byte body backing every `Pipe_` and `Mutex_` value | [AsyncValue and BLAKE3 Interning](asyncvalue-and-blake3-interning.md) |
| Diagnostic ABI, argument buffer, source-location formatting | [Diagnostic ABI and Helpers](diagnostic-abi-and-helpers.md) |

## Substrate Invariants

Three invariants tie the deep pages together. They are the assumptions every dialect and every pass relies on; violating any one of them is a substrate-level bug that surfaces far from the violation site.

**Uniqued payloads are immutable.** Types, attributes, locations, affine maps, and most dialect-specific values pass through the storage uniquer once and are referenced by pointer identity for the rest of the compiler's life. Mutating a uniqued payload after construction breaks every equality test, every keyed map, and every cache that depends on it — and the storage uniquer makes no copy.

**TypeID is by pointer-identity.** Every concrete type, attribute, interface, and pattern owns a `TypeID` whose address is its identity. Dispatch walkers compare against the address, not against the byte at the address, and the address must be stable for the lifetime of the `MLIRContext`. Two materialisations of the same TypeID — one through the Idiom-1 static sentinel and one through the Idiom-2 Meyers cache — live in different `.bss` bands, but a single TypeID never moves between them.

**Operation header offsets are part of the binary contract.** Reverse-engineering notes name byte offsets to identify behaviour because every walker, verifier, and canonicaliser in the binary reads the same offsets. A reimplementation that changes the header layout without updating every consumer breaks dispatch silently — the kindPtr at `*(qword*)(op + 48) + 16` is the canonical example.

## Cross-Cutting Threads

Three substrate threads thread through several deep pages and earn their own pointers here.

The 808-byte `AsyncValueImpl` body is what the scheduler's `Pipe_` and `Mutex_` constructors allocate; the scheduler-side companion is [Pipe and Mutex Value Layout](../scheduler/pipe-mutex-value-layout.md). The body itself lives in [AsyncValue and BLAKE3 Interning](asyncvalue-and-blake3-interning.md).

The SwissTable family — distinct from DenseMap by its fmix64 mixer and 16-byte control-byte groups — is exclusive to the scheduler in this binary. [Container Fingerprints](container-fingerprints.md) covers the layout; [Modulo Driver and OR-Chain](../scheduler/modulo-driver-or-chain.md) is the highest-traffic consumer.

The TypeID idioms back every dispatch in the binary. [TypeID Sentinels and Anchors](typeid-sentinels-and-anchors.md) covers both idioms; the `AnalysisManager` slot that caches the scheduler's `ScheduleAnalysis` is one example of the Meyers idiom in action.

## Cross-links

- [Operation Layout](operation-layout.md)
- [Storage Uniquer and Context Impl](storage-uniquer-and-context-impl.md)
- [Pattern Vtables and Shapes](pattern-vtables-and-shapes.md)
- [Interface Vtables](interface-vtables.md)
- [TypeID Sentinels and Anchors](typeid-sentinels-and-anchors.md)
- [Container Fingerprints](container-fingerprints.md)
- [AsyncValue and BLAKE3 Interning](asyncvalue-and-blake3-interning.md)
- [Diagnostic ABI and Helpers](diagnostic-abi-and-helpers.md)
