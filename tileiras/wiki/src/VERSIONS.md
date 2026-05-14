# Versions and Fingerprints

This page records the version identifiers that matter for users and compatible implementations. It avoids private evidence anchors and focuses on the public compatibility contract: which CUDA release, LLVM lineage, dialect version family, and backend behavior this wiki describes.

## Version Table

| Component | Version or identity | Compatibility meaning |
| --- | --- | --- |
| CUDA toolkit | CUDA 13.1, toolkit banner `V13.1.80` | The documented driver, dialects, and target defaults describe the CUDA 13.1 tileiras binary. |
| LLVM base | Internal LLVM snapshot identifying as `LLVM21.0.0git` | LLVM IR, MLIR infrastructure, NVVM lowering, and NVPTX backend behavior should be read as LLVM-21-era behavior plus NVIDIA patches. |
| MLIR base | Co-tracked with the LLVM 21 snapshot | Operation, type, attribute, pass-manager, and bytecode infrastructure follow the corresponding MLIR generation. |
| Primary input dialect | `cuda_tile` TileIR bytecode | The accepted input is serialized MLIR bytecode carrying the public tile dialect. |
| Main target family | Blackwell-family targets, defaulting to `sm_100` | Many docs assume Hopper/Blackwell-era TMA, WGMMA, and tensor-memory features. |
| NVPTX backend | LLVM 21 NVPTX with NVIDIA-internal extensions | Backend pass and intrinsic behavior extends stock upstream LLVM. |
| libdevice | CUDA 13.1 libdevice bitcode | Device math calls are linked, reflected, inlined, and optimized before PTX emission. |

## LLVM and MLIR Lineage

The key compatibility fact is that tileiras uses an LLVM/MLIR stack aligned with LLVM 21 development. That affects:

- MLIR bytecode reader behavior,
- operation, type, attribute, and interface mechanics,
- pass-manager and rewrite-pattern infrastructure,
- LLVM bitcode writing,
- NVVM intrinsic naming and lowering,
- NVPTX instruction selection and PTX emission.

A compatible reimplementation does not need to reproduce every linked LLVM helper. It does need to match the observable LLVM/NVVM contracts: data layout, target attributes, intrinsic lowering, kernel ABI, libdevice handling, and PTX backend expectations.

## NVIDIA Extensions

The backend is not just stock upstream LLVM. It includes NVIDIA extensions for newer NVVM operations, Blackwell tensor-memory support, target-specific verifiers, NVVM reflection handling, parameter-space lowering, address-space specialization, and NVPTX machine-level cleanup.

The practical rule is:

```text
Treat generic LLVM behavior as LLVM-21-era behavior.
Treat NVVM, NVPTX, TileIR, and tensor-memory behavior as NVIDIA-extended behavior.
```

When a page documents `tcgen05`, TMA, WGMMA, cluster launch control, TileAS scheduling, or CUTLASS pipeline lowering, assume NVIDIA-specific semantics unless the page explicitly names an upstream MLIR or LLVM feature.

## Bytecode and Dialect Compatibility

The bytecode reader expects a TileIR-specific MLIR bytecode container. The public input dialect is `cuda_tile`; internal dialects such as `nv_tileaa`, `nv_tileas`, `cute`, `cute_nvgpu`, and `cutlass` are normally constructed by the pipeline or by frontend-specific producers.

Compatible tooling should preserve these boundaries:

- bytecode producers emit valid `cuda_tile` programs,
- dialect conversion lowers toward internal dialects in one direction,
- internal dialects are not treated as stable standalone file formats unless a page explicitly describes a textual debugging surface,
- target-specific dialects are verified against the selected compute capability.

## Content Hashing

The implementation uses BLAKE3-style content hashing for internal object identity and caching. For public compatibility, the important behavior is deterministic content-addressing: equivalent IR objects should receive stable identities within a compiler run, and hash-based caches must not change semantic behavior.

Do not depend on these hashes as a public ABI unless a page explicitly documents such an output. They are implementation support for interning, caching, and deduplication.

## Version-Sensitive Pages

Some pages are especially tied to CUDA 13.1 and the LLVM 21-era backend:

- [NVVM Dialect Overview](dialects/nvvm/overview.md)
- [NVPTX Backend Passes](nvptx-passes/overview.md)
- [Codegen Overview](codegen/overview.md)
- [libdevice Overview](libdevice/overview.md)
- [MLIR Bytecode Format](bytecode/mlir-bc-format.md)
- [Position in nvcc 13.1](boundaries/nvcc-13-1-position.md)

If a future CUDA release changes the bytecode schema, dialect roster, target defaults, or NVPTX intrinsic set, these pages should be reviewed first.
