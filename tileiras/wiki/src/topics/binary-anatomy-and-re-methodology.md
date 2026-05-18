# Binary Anatomy and Reverse-Engineering Methodology

## Abstract

Tileiras ships as a single stripped 88 MB x86-64 ELF binary inside the CUDA 13.1 toolkit. The rest of this wiki describes what is inside that binary; this page describes the binary itself. It records the file's identity, the section and segment layout a disassembler will show, the tools the wiki authors used to extract information, and a recipe a reader can follow to verify any individual claim in the wiki against the bytes on disk. The page exists so that a reimplementer who does not trust the wiki can quickly close the gap by opening the binary directly.

## Binary Identity

| Property | Value |
| --- | --- |
| File | `tileiras` |
| Toolkit path | `cuda-13.1/bin/tileiras` |
| Approximate size | ~88 MB |
| Format | ELF64, x86-64, dynamically linked executable |
| Stripped | Yes; `.symtab` removed, only dynamic symbols retained |
| Compiler | clang 21 (verified via the `LLVM21.0.0git` producer string) |
| Toolkit banner | `Cuda compilation tools, release 13.1, V13.1.80` |
| Linkage | LLVM, MLIR, libstdc++ statically linked; libc and libpthread dynamic |
| Default output | Host relocatable named `elf.o` |

The producer string is the strongest single anchor: it appears verbatim in `.rodata`, it is referenced from the bitcode-writer body, and the same version string also surfaces in every emitted PTX header. The detailed argument for "this is LLVM 21" is the ten-fingerprint analysis collected in the [LLVM Fingerprint Table](../reference/llvm-fingerprint-table.md).

## Section and Segment Layout

| Section | Purpose | Approximate footprint |
| --- | --- | --- |
| `.text` | The entire compiler: driver, bytecode reader, dialect logic, scheduler, codegen, asm printer, plus statically linked LLVM and MLIR | tens of MB |
| `.rodata` | Mnemonic pools, diagnostic strings, pattern descriptors, the XOR-3 NVPTX printer pool, bitcode tag table, `cl::opt` help text, typeinfo, embedded libdevice bitcode | ~10 MB |
| `.data` | `cl::opt` mutable storage, dialect and pass registration tables, the XOR-3 mnemonic walking-cipher working copy, LLVM global initialisers | a few MB |
| `.data.rel.ro` | Vtables and typeinfo nodes for polymorphic classes, `AbstractOperation` singletons, conversion-target descriptors | small |
| `.bss` | `StorageUniquer` hash tables, `TypeID` Meyers-cache slots, dialect singletons, operation-name registry, per-thread caches, `LLVMContext` state | ~1 MB |
| `.got`, `.plt` | Dynamic-link tables for libc and libpthread | small |
| `.eh_frame`, `.eh_frame_hdr` | C++ exception unwind information | small |

The deeper subsystem-by-subsystem breakdown of what lives inside each segment is the subject of the [Program Layout](../binary-layout.md) page.

## Tools the Wiki Was Produced With

The wiki was authored with an iterative reverse-engineering workflow on a single workstation. The dominant tools were:

- **IDA Pro 9 (or compatible)** — primary disassembler and decompiler. Provides the `sub_ADDR` auto-naming the wiki uses internally as an evidence trail.
- **`readelf` / `objdump`** — section and segment structure, dynamic-symbol table, relocations.
- **`strings(1)`** — extracting `.rodata` and `.data` strings to build a diagnostic and mnemonic catalog.
- **`xxd` / `hexdump`** — byte-level layout reading for vtable shapes, walking-cipher pools, and packed bitfields.
- **`mlir-translate` from the upstream LLVM tree** — cross-checking bytecode wire-format claims against an independent implementation.
- **The OSS preview source tree (`cuda-tile`)** — used as a sanity check for tablegen-derived structures and dialect rosters.

The wiki was produced in multiple passes. An initial sweep extracted every printable string and clustered them by subsystem; a second pass identified function bodies by call-graph traversal from string-anchored entry points; a third pass cross-validated the recovered structures against the OSS preview where it overlapped. Each pass narrowed the evidence base; only claims that survived all three pass forms made it into the wiki, with confidence tags reflecting how many independent forms of evidence agreed.

## Verifying a Wiki Claim Against the Binary

The wiki is structured so that any individual claim can be checked against the binary in a small constant amount of time.

For a **diagnostic string the wiki cites verbatim**, run `strings tileiras | grep "the cited fragment"`. Every backticked string in the wiki is byte-identical to an entry in the binary's string table; if the binary has it, the wiki claim is verified at the byte level. The discipline behind that rule is documented in the [String Evidence and Confidence Policy](../reference/string-evidence-and-confidence-policy.md).

For a **`sub_ADDR` the wiki cites in an evidence table**, open the binary in IDA, navigate to that address, and compare the body to the wiki description. The auto-named address is not a stable interface — it is an evidence trail — but it is reproducible across identical loads of the same binary in IDA.

For a **vtable layout the wiki describes**, find the `AbstractOperation` singleton or the class-instance allocation site referenced in the page, follow the vtable pointer at offset zero, and dump the function-pointer array. The 4-slot and 8-slot pattern vtable shapes documented in the wiki are observable as contiguous 0x60-byte and 0x68-byte arrays in `.data.rel.ro`.

For a **bit-field layout the wiki gives**, find a use site of the field — usually a verifier diagnostic that prints the field name — and read the immediate operand of the bit-extract instruction. As a worked example: the Tcgen05 MMA kind bitfield. Locate the verifier diagnostic that mentions `cta_group`; trace back to the `and` or `bextr` that extracts the field from the encoded attribute word; confirm that bits 0-1 are the cta_group selector.

## Where the Wiki's Anchors Come From

Four kinds of binary-content evidence dominate the wiki.

The **string catalog** is the primary anchor. Every backticked string is byte-identical to a `.rodata` entry. Diagnostic strings, op mnemonics, pass names, and the producer string itself are all directly quotable. This is the kind of evidence with the highest signal-to-noise ratio and the lowest risk of misidentification.

**Vtable shapes** are the second anchor. Polymorphic classes — patterns, passes, dialects, the conversion target, the diagnostic engine — show up as contiguous arrays of function pointers in `.data.rel.ro`. The slot count and ordering of those arrays is a stable structural fingerprint even when the function bodies themselves are inlined or shared.

**Mnemonic pools** are the third anchor. The XOR-3 walking cipher used by the NVPTX asm printer is observable as a `pthread_once`-guarded decode function in `.text` plus a contiguous block of XOR-3-encoded bytes in `.data`. The encrypted form keeps the readable PTX vocabulary out of `strings` output; the decode site reveals the full pool to anyone who reads the binary statically.

**Bytecode tag tables** are the fourth anchor. The 110-case `OpTag` dispatcher in the bytecode reader compiles to a contiguous jump table whose row count and case-label values are visible in the disassembly. That table fixes the wire-format claims independently of any string.

## Binary Distinction from Upstream LLVM and MLIR

The binary is mostly LLVM and MLIR plus NVIDIA-private additions on top. Specifically:

- Stock LLVM 21, verified by the ten independent fingerprints in the [LLVM Fingerprint Table](../reference/llvm-fingerprint-table.md).
- Stock MLIR with the post-2024 / LLVM 21 layout (Operation header is 0x48 bytes, AsyncValueImpl is 808 bytes).
- The NVPTX backend with private peephole passes, an enlarged `MatcherTable`, and a contiguous typed-ProxyReg whitelist that lands in LLVM 21 itself.
- The TileAS pass family, which is NVIDIA-private and has no upstream counterpart.
- The `cute`, `cute_nvgpu`, and `cutlass` MLIR dialects, which are mostly ports of NVIDIA's open-source CUTLASS to MLIR.
- The `cuda_tile` dialect, which is NVIDIA-private; a partial OSS preview is available under the `cuda-tile` tree and is discussed on the [OSS Comparison Overview](../oss/overview.md) page.

The combination is roughly 60% upstream LLVM/MLIR by code size and 40% NVIDIA-private; the wiki focuses on the NVIDIA-private portion because that is where reimplementation effort is concentrated.

## Limits of This Wiki

The binary is stripped. Function references in the wiki's evidence tables use IDA's auto-naming convention (`sub_ABCDEF`), which is reproducible but is not a real symbol. Anyone reproducing the analysis with a different disassembler will see different labels for the same addresses.

Inline-only functions have no separate compiled body and cannot be located by address. Macro- and TableGen-generated code may have many addresses for the same logical entity, because each instantiation is its own compiled body. Some claims rest on structural evidence — vtable shape, basic-block count, allocation footprint — rather than on a verbatim string; those claims carry MED rather than HIGH confidence. The discipline is documented in the [String Evidence and Confidence Policy](../reference/string-evidence-and-confidence-policy.md).

Finally, the wiki documents the binary as-shipped in CUDA 13.1. A reader who needs to confirm a claim against a later toolkit should reverify against that release before relying on it.

## Reimplementation Viability

The wiki is dense enough that a reimplementer can reproduce the great majority of tileiras's behavior from the wiki alone, with bit-level correctness for diagnostic strings, op rosters, attribute encodings, and bitfield layouts. The remaining behavior — corner cases not exercised by static analysis — would require running tileiras on test inputs and observing the output.

The wiki is not a substitute for binary access; it is an accelerator. Instead of starting from "what does this 88 MB binary do," a reader starts from "I know the pattern-applicator uses a 4-slot vtable; let me find the singleton." That shortcut is what makes a stripped binary tractable to a small reimplementation team.

## Cross-References

The structural layout of each segment is described in detail on the [Program Layout](../binary-layout.md) page. The editorial methodology that governs how evidence becomes wiki prose is documented on the [Methodology](../methodology.md) page. The confidence-tag discipline applied to every claim is the [String Evidence and Confidence Policy](../reference/string-evidence-and-confidence-policy.md). The ten-anchor argument for the LLVM 21 base is the [LLVM Fingerprint Table](../reference/llvm-fingerprint-table.md). The boundary between NVIDIA-private and upstream-derived code is mapped on the [OSS Comparison Overview](../oss/overview.md) page. The deliberate decisions visible in the binary — static linkage, XOR-3 mnemonic obfuscation, the stripped-by-design distribution — are framed as design choices on the [Architecture Evolution and Design Decisions](architecture-evolution-and-design-decisions.md) page.
