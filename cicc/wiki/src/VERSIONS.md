# Version Tracking

This page documents the exact version identifiers embedded in the cicc v13.0 binary and the version relationships between its components. Every version listed here was recovered from string constants, constructor initializations, or binary header fields in the stripped ELF binary. This is the single source of truth for version-related questions across the wiki.

## Version Summary

| Component | Version | Evidence |
|-----------|---------|----------|
| cicc binary | v13.0 | Build string `cuda_13.0.r13.0/compiler.36424714_0` |
| CUDA Toolkit | 12.8 | Toolkit release that ships cicc v13.0 |
| LLVM base (internal) | 20.0.0 | `ctor_036` at `0x48CC90` falls back to `"20.0.0"`; string `"llvm-mc (based on LLVM 20.0.0)"` at `sub_E7A190` |
| Bitcode producer (emitted) | `"LLVM7.0.1"` | `ctor_154` at `0x4CE640` writes `"7.0.1"` to producer global |
| EDG frontend | 6.6 | String `"Based on Edison Design Group C/C++ Front End, version 6.6"` |
| NVVM IR version (user code) | 3.2 | Metadata gate at `sub_157E370`: `major == 3, minor <= 2` |
| NVVM IR version (libdevice) | 2.0 | `!nvvmir.version = !{i32 2, i32 0}` -- always-compatible sentinel |
| NVVM container format | 1.x | Header field `version_major = 1`, `version_minor <= 0x41` |
| NVVM debug info version | 3.2 | Container header `nvvm_debug_major = 3`, `nvvm_debug_minor <= 2` |
| Embedded libdevice | `libdevice.10.bc` | 455,876 bytes, 352 functions, triple `nvptx64-nvidia-gpulibs` |
| GCC emulation (EDG) | 8.1 | `DEFAULT_GNU_VERSION = 80100` |
| Clang emulation (EDG) | 9.1 | `DEFAULT_CLANG_VERSION = 90100` |
| jemalloc | 5.3.x | ~400 statically linked functions at `0x12FC000` |
| Default PTX ISA (sm_90) | 8.5 | `.version 8.5` computed from `PTXVersion / 10`, `PTXVersion % 10` |
| Default SM target | sm_75 | Hardcoded `strcpy("compute_75")` in `sub_900130` and `sub_125FB30` |

## LLVM Version: The Dual Identity

CICC has two LLVM version identities. Internally, it is an LLVM 20.0.0 fork -- all modern instruction opcodes, metadata formats, type encodings, and pass infrastructure from LLVM 20 are present. Externally, the bitcode it emits identifies itself as `"LLVM7.0.1"` in the producer field.

The reason is historical: NVVM IR 2.0 was defined against LLVM 7.0.1. The entire NVVM toolchain ecosystem (libNVVM, nvcc's device pipeline, nvdisasm, third-party NVVM IR consumers) standardized on `"LLVM7.0.1"` as the format identifier. Changing the producer string would require a coordinated update across the entire CUDA toolkit and all downstream consumers.

**Binary evidence**:

- `ctor_036` at `0x48CC90`: reads `LLVM_OVERRIDE_PRODUCER` environment variable, falls back to `"20.0.0"` (the true version).
- `ctor_154` at `0x4CE640`: reads `LLVM_OVERRIDE_PRODUCER`, falls back to `"7.0.1"` (the compatibility marker). This is the constructor that runs for the bitcode writer path.
- `sub_E7A190`: contains the string `"llvm-mc (based on LLVM 20.0.0)"`.
- `sub_1538EC0` (writeModule): emits `"LLVM" + "7.0.1"` = `"LLVM7.0.1"` as the IDENTIFICATION_BLOCK producer.

Both constructors accept the `LLVM_OVERRIDE_PRODUCER` environment variable to override the default. Setting it changes the embedded producer string in output bitcode.

See [Bitcode Reader/Writer](./infra/bitcode-io.md) for the full dual-producer mechanism.

## EDG 6.6 Frontend

The EDG (Edison Design Group) frontend is a licensed commercial C/C++ frontend. Version 6.6 occupies 3.2 MB of code at `0x5D0000`--`0x8F0000`. The version string is embedded literally as `"Based on Edison Design Group C/C++ Front End, version 6.6"` and is accessible via the `--version` flag.

EDG 6.6 in cicc is configured to emulate GCC 8.1 (`DEFAULT_GNU_VERSION = 80100`) and Clang 9.1 (`DEFAULT_CLANG_VERSION = 90100`). It supports C++23 as the newest C++ standard and C23 as the newest C standard, with C++17 as the default mode.

See [EDG 6.6 Frontend](./pipeline/edg.md) for the full frontend documentation.

## NVVM IR Version

The NVVM IR version is a metadata tuple `(major, minor)` embedded in every NVVM bitcode module via the `!nvvmir.version` named metadata node. CICC v13.0 has two distinct version contexts:

**User code**: the IR generation phase (`sub_9151E0`) emits `!nvvmir.version` with the current version tuple. The version checker at `sub_157E370` enforces `major == 3` and `minor <= 2`, making **3.2** the current maximum accepted version. Modules with `major != 3` or `minor > 2` are rejected with `"Broken module found, compilation aborted!"`.

**Libdevice**: the embedded `libdevice.10.bc` carries `!nvvmir.version = !{i32 2, i32 0}`. The version `(2, 0)` is hard-coded in the version checker (`sub_12BDA30`) as an **always-compatible sentinel** -- it passes the check regardless of the current NVVM IR version. This ensures the embedded math library is compatible with any user module.

**Container format**: the [NVVM container](./structs/nvvm-container.md) binary header stores version fields separately at offsets 0x06--0x07 (`nvvm_ir_major`, `nvvm_ir_minor`). These track the container-level IR spec version and may differ from the bitcode-level metadata tuple.

**Bypass**: setting `NVVM_IR_VER_CHK=0` in the environment disables version validation entirely, allowing any version tuple to pass.

See [Bitcode Reader/Writer](./infra/bitcode-io.md) for the version gate implementation and [NVVM Container](./structs/nvvm-container.md) for the container-level version fields.

## Embedded Libdevice

The embedded libdevice is `libdevice.10.bc`, a 455,876-byte LLVM bitcode library containing 352 GPU-optimized math functions. Two identical copies are statically embedded in the binary:

| Copy | Address | Pipeline |
|------|---------|----------|
| A | `unk_3EA0080` | LibNVVM mode (Path A) |
| B | `unk_420FD80` | Standalone mode (Path B) |

Key properties:
- **Target triple**: `nvptx64-nvidia-gpulibs`
- **Function count**: 352 (all `alwaysinline nounwind`)
- **NVVM IR version**: `(2, 0)` -- always-compatible sentinel
- **Producer**: `"clang version 3.8.0 (tags/RELEASE_380/final)"` -- the Clang version that originally compiled libdevice (not indicative of cicc's own compiler version)
- **NVVMReflect calls**: uses `__nvvm_reflect("__CUDA_FTZ")`, `__nvvm_reflect("__CUDA_ARCH")`, and `__nvvm_reflect("__CUDA_PREC_SQRT")` for runtime specialization

The `libdevice.10.bc` naming convention carries forward from the CUDA 5.0 era. The `10` in the filename originally indicated "compute capability 1.0 and above" (i.e., universal), not a version number.

See [Libdevice Linking](./infra/libdevice-linking.md) for the linking algorithm, version validation, and NVVMReflect interaction.

## NVVM Container Format Version

The NVVM container binary envelope uses its own versioning scheme, independent of the NVVM IR version:

| Field | Offset | Value | Meaning |
|-------|--------|-------|---------|
| `version_major` | 0x04 | 1 | Container format major |
| `version_minor` | 0x05 | <= 0x41 | Container format minor |
| `nvvm_ir_major` | 0x06 | 2 | NVVM IR spec major (container-level) |
| `nvvm_ir_minor` | 0x07 | <= 0x62 | NVVM IR spec minor (container-level) |
| `nvvm_debug_major` | 0x08 | 3 | Debug info format major |
| `nvvm_debug_minor` | 0x09 | <= 2 | Debug info format minor |
| `llvm_major` | 0x0A | encoded | LLVM version (combined: `major * 100 + minor` = 2000) |
| `llvm_minor` | 0x0B | encoded | |

The container's LLVM version encoding stores the combined value `20 * 100 + 0 = 2000`, confirming the internal LLVM 20.0.0 base.

See [NVVM Container](./structs/nvvm-container.md) for the full binary format specification.

## Version Cross-Reference Matrix

How versions flow through the pipeline:

```text
                    EDG 6.6 Frontend
                         |
                         v
                    NVVM IR Generation
                    (emits nvvmir.version = {3, 2})
                         |
                    +----+----+
                    |         |
              libdevice    user IR
           (version 2,0) (version 3,2)
                    |         |
                    +----+----+
                         |
                    NVVM IR Version Check
                    (gate: major==3, minor<=2)
                    (sentinel: 2,0 always passes)
                         |
                    LLVM 20.0.0 Optimizer
                         |
                    Bitcode Writer
                    (producer: "LLVM7.0.1")
                         |
                    NVVM Container Serializer
                    (container version 1.x, LLVM encoded as 2000)
                         |
                         v
                    .ptx / .optixir output
```

## Future Updates

This wiki documents cicc v13.0 from CUDA 12.8. When a new CUDA toolkit release ships a newer cicc binary, the following version fields are the most likely to change:

- **LLVM base version**: NVIDIA periodically rebases on newer LLVM releases. A jump from 20.0.0 to a later version would change the internal string, the container LLVM encoding, and potentially add new passes, opcodes, and metadata formats.
- **EDG version**: EDG releases track independently of LLVM. A bump from 6.6 to a later version would affect C++ standard support, keyword handling, and the frontend error catalog.
- **NVVM IR version minor**: the minor field (currently 2 in the `major == 3` series) may increment to accommodate new metadata kinds or intrinsic conventions without breaking the major version.
- **PTX ISA version**: new SM targets require new PTX versions. sm_100 Blackwell already uses a higher PTX version than sm_90 Hopper.
- **SM target range**: new GPU architectures add new SM numbers. The sm_75--sm_121 range in v13.0 will expand in future releases.

The **bitcode producer string** (`"LLVM7.0.1"`) is unlikely to change in the near term -- doing so would break backward compatibility with the entire NVVM IR ecosystem. The **libdevice version sentinel** `(2, 0)` is similarly stable because the version checker special-cases it.

To update this wiki for a new cicc version:
1. Extract the build string (search for `cuda_XX.Y.rXX.Y/compiler.`).
2. Check `ctor_036` for the LLVM version fallback string.
3. Check the EDG version string at `sub_617BD0`.
4. Check the NVVM IR version gate constants at the version checker function.
5. Measure the embedded libdevice size and function count.
6. Verify the NVVM container header version fields.

## Cross-References

- [Bitcode Reader/Writer](./infra/bitcode-io.md) -- producer string mechanism, version gate implementation
- [NVVM Container](./structs/nvvm-container.md) -- container binary format with version header fields
- [Libdevice Linking](./infra/libdevice-linking.md) -- embedded math library, version sentinel
- [EDG 6.6 Frontend](./pipeline/edg.md) -- frontend version, GCC/Clang emulation modes
- [Binary Layout](./binary-layout.md) -- build ID string, ELF properties
- [Entry Point & CLI](./pipeline/entry.md) -- dual-path dispatch, version string arguments
- [Environment Variables](./config/env-vars.md) -- `LLVM_OVERRIDE_PRODUCER`, `NVVM_IR_VER_CHK`
- [Debug Info Verification](./infra/debug-verify.md) -- debug version field (3.2)
