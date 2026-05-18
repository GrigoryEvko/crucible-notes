# Versions and Fingerprints

This page records the version identifiers that matter for users and compatible implementations. It avoids private evidence anchors and focuses on the public compatibility contract: which CUDA release, LLVM lineage, dialect version family, and backend behavior this wiki describes.

## Version Table

| Component | Version or identity | Compatibility meaning |
| --- | --- | --- |
| CUDA toolkit | CUDA 13.1, toolkit banner `V13.1.80`, build tag `local.local.36836380_` | The documented driver, dialects, and target defaults describe the CUDA 13.1 tileiras binary. The build tag identifies the exact NVIDIA-internal snapshot. |
| LLVM base | Host C++ link target: internal LLVM snapshot identifying as `LLVM21.0.0git` | LLVM IR, MLIR infrastructure, NVVM lowering, and NVPTX backend behavior should be read as LLVM-21-era behavior plus NVIDIA patches. |
| MLIR base | Co-tracked with the LLVM 21 snapshot | Operation, type, attribute, pass-manager, and bytecode infrastructure follow the corresponding MLIR generation. The bytecode reader's AttrTag numbering is wire-format-forked from upstream; see [MLIR Bytecode Format](bytecode/mlir-bc-format.md). |
| Embedded device-bitcode producer (recent) | `clang version 16.0.0` (NVIDIA internal), subtarget triple `nvptx64-nvidia-gpulibs` | The fp128 / `__nv_fp128` softfloat module embedded inside the binary was compiled by an NVIDIA-internal clang-16 toolchain. Different from the host LLVM-21 link target. |
| Embedded device-bitcode producer (legacy) | `clang version 7.1.0 git-630d6c22278`, subtarget triple `nvptx64-nvidia-gpulibs` | A second, older embedded LLVM IR blob (the `__nv_*128` integer family) carries this producer string. A compatible reimplementation must accept that the binary ships two embedded IR generations side by side. |
| Embedded soft-math providers | Berkeley SoftFloat (extF80 / f128M_* / softfloat_*), Sleef (`Sleef_*`, `Sleef_rempitabqp`, `qp_cuda_sleefq`) | fp128 arithmetic, fp128 transcendentals, and the `payne_hanek` argument reduction table are sourced from these third-party libraries linked into the embedded gpulibs IR, not from the main `__nv_*` libdevice math set. |
| Primary input dialect | `cuda_tile` TileIR bytecode | The accepted input is serialized MLIR bytecode carrying the public tile dialect. |
| Main target family | Blackwell-family targets, defaulting to `sm_100`; PTX produced through the LLVM-21 NVPTX AsmPrinter (header line `Based on LLVM 21.0.0git`) | Many docs assume Hopper/Blackwell-era TMA, WGMMA, and tensor-memory features. The user-target triple is `nvptx64-nvidia-cuda`; the gpulibs subtarget triple is `nvptx64-nvidia-gpulibs`. |
| NVPTX backend | LLVM 21 NVPTX with NVIDIA-internal extensions | Backend pass and intrinsic behavior extends stock upstream LLVM. |
| libdevice | CUDA 13.1 libdevice bitcode, exported as `_mlir_embedded_libdevice` | Device math calls are linked, reflected, inlined, and optimized before PTX emission. The bitcode is embedded as an MLIR-side resource, not loaded from disk. |
| Content hashing | BLAKE3-style construction (internal use only) | Used for IR object interning, deduplication, and caching. Not a public ABI. |

## LLVM and MLIR Lineage

The key compatibility fact is that tileiras uses an LLVM/MLIR stack aligned with LLVM 21 development. That affects:

- MLIR bytecode reader behavior,
- operation, type, attribute, and interface mechanics,
- pass-manager and rewrite-pattern infrastructure,
- LLVM bitcode writing,
- NVVM intrinsic naming and lowering,
- NVPTX instruction selection and PTX emission.

A compatible reimplementation does not need to reproduce every linked LLVM helper. It does need to match the observable LLVM/NVVM contracts: data layout, target attributes, intrinsic lowering, kernel ABI, libdevice handling, and PTX backend expectations.

The binary also embeds device-side LLVM IR that was produced by *older* clang generations (16.0.0 and 7.1.0) running against the `nvptx64-nvidia-gpulibs` subtarget. The host LLVM-21 framework consumes that prebuilt IR through the standard bitcode reader; a reimplementation only needs to honor the producer-string and subtarget-triple shapes, not rebuild the embedded IR from source.

## NVIDIA Extensions

The backend is not just stock upstream LLVM. It includes NVIDIA extensions for newer NVVM operations, Blackwell tensor-memory support, target-specific verifiers, NVVM reflection handling, parameter-space lowering, address-space specialization, and NVPTX machine-level cleanup.

The practical rule is:

```text
Treat generic LLVM behavior as LLVM-21-era behavior.
Treat NVVM, NVPTX, TileIR, and tensor-memory behavior as NVIDIA-extended behavior.
```

When a page documents `tcgen05`, TMA, WGMMA, cluster launch control, TileAS scheduling, or CUTLASS pipeline lowering, assume NVIDIA-specific semantics unless the page explicitly names an upstream MLIR or LLVM feature.

## External Dependency Surface

A reimplementation must account for every third-party or NVIDIA-internal component that crosses the binary's compatibility surface, not only the LLVM host link. The table below pins each one to a concrete integration point.

| Dependency | Where it crosses into tileiras | Anchor inside the wiki |
| --- | --- | --- |
| LLVM 21 host library | C++ link target. Provides IR types, pass manager, bitcode reader/writer, NVPTX backend, AsmPrinter. | [LLVM Fingerprint Table](reference/llvm-fingerprint-table.md) |
| MLIR (LLVM-21 generation) | C++ link target. Operation/type/attribute/interface mechanics, pass-manager, dialect registration, bytecode reader. | [MLIR Bytecode Format](bytecode/mlir-bc-format.md), [Dialect Asm-Printer Status](bytecode/asm-printer-status.md) |
| NVPTX backend extensions | Inside the LLVM host library, but with NVIDIA-internal passes, intrinsics, and Matcher tables. | [NVPTX Backend Passes](nvptx-passes/overview.md), [LLVM Fingerprint Table](reference/llvm-fingerprint-table.md) §6, §8 |
| Embedded libdevice bitcode | Linked at module construction via `_mlir_embedded_libdevice`. CUDA 13.1 generation. | [libdevice Overview](libdevice/overview.md), [NVPTX Bring-up and Target Init](codegen/nvptx-bring-up-and-target-init.md) |
| Embedded clang-16 device IR | Bitcode resource compiled by NVIDIA-internal clang 16.0.0 against `nvptx64-nvidia-gpulibs`. Carries the `__nv_fp128` softfloat family. | [Math Pass Pipeline and Crosswalk](libdevice/math-pass-pipeline-and-crosswalk.md) |
| Embedded clang-7.1 device IR | Bitcode resource compiled by NVIDIA-internal clang 7.1.0 (`git-630d6c22278`). Carries the `__nv_*128` integer family. | [Math Pass Pipeline and Crosswalk](libdevice/math-pass-pipeline-and-crosswalk.md) |
| Berkeley SoftFloat | Statically linked inside the embedded gpulibs IR. Drives fp128 arithmetic (`f128M_*`, `softfloat_*`). | [Math Pass Pipeline and Crosswalk](libdevice/math-pass-pipeline-and-crosswalk.md) |
| Sleef | Statically linked inside the embedded gpulibs IR. Drives fp128 transcendentals (`Sleef_*`, `qp_cuda_sleefq`, `Sleef_rempitabqp`). | [Math Pass Pipeline and Crosswalk](libdevice/math-pass-pipeline-and-crosswalk.md) |
| BLAKE3 content hashing | Internal interning, deduplication, and caching. Not a public ABI. | (no public surface) |
| Host C runtime | `libpthread`, `libdl`, GLIBC 2.3.4-baseline. Used for synchronization and dynamic loading; no CUDA driver linkage. | (no public surface) |

The integration points worth checking when a new CUDA release lands are concentrated in three places:

1. **Bytecode envelope and AttrTag numbering** — the wire-format fork from upstream MLIR.
2. **`_mlir_embedded_libdevice` and the gpulibs subtarget triples** — the device-side IR contract.
3. **NVPTX AsmPrinter header + MatcherTable** — the PTX emission contract.

## Bytecode and Dialect Compatibility

The bytecode reader expects a TileIR-specific MLIR bytecode container. The public input dialect is `cuda_tile`; internal dialects such as `nv_tileaa`, `nv_tileas`, `cute`, `cute_nvgpu`, and `cutlass` are normally constructed by the pipeline or by frontend-specific producers.

Compatible tooling should preserve these boundaries:

- bytecode producers emit valid `cuda_tile` programs,
- dialect conversion lowers toward internal dialects in one direction,
- internal dialects are not treated as stable standalone file formats unless a page explicitly describes a textual debugging surface,
- target-specific dialects are verified against the selected compute capability.

## Content Hashing

BLAKE3-style content hashing is used internally for IR object identity, deduplication, and caching. Equivalent IR objects receive stable identities within a compiler run, but the hashes are not a public ABI; treat them as implementation support.

## Version-Sensitive Pages

Some pages are especially tied to CUDA 13.1 and the LLVM 21-era backend:

- [NVVM Dialect Overview](dialects/nvvm/overview.md)
- [NVPTX Backend Passes](nvptx-passes/overview.md)
- [Codegen Overview](codegen/overview.md)
- [libdevice Overview](libdevice/overview.md)
- [MLIR Bytecode Format](bytecode/mlir-bc-format.md)
- [Position in nvcc 13.1](boundaries/nvcc-13-1-position.md)

If a future CUDA release changes the bytecode schema, dialect roster, target defaults, or NVPTX intrinsic set, these pages should be reviewed first.
