# Version Tracking

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

This page documents the exact ptxas binary under analysis and the version-related
metadata recovered from the stripped ELF.

## Binary Under Analysis

| Field | Value |
|-------|-------|
| Tool | `ptxas` (PTX optimizing assembler) |
| Version | **13.0.88** |
| Build tag | `cuda_13.0.r13.0/compiler.36424714_0` |
| Build date | Wed Aug 20 01:55:12 PM PDT 2025 |
| Source path | `/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/common/utils/generic/impl/generic_knobs_impl.h` |
| ELF size | 37,741,528 bytes (37.7 MB) |
| Architecture | x86-64 (AMD64) |
| Linking | Dynamically linked, stripped |
| Functions | ~40,000 (estimated from IDA/Ghidra DB) |

## Embedded Version Strings

`sub_432A00` (0x432A00, CLI option registration) contains the self-identification
strings that ptxas prints for `--version` / `--list-version`:

| String | Location |
|--------|----------|
| `"Ptx optimizing assembler"` | Product name |
| `"NVIDIA (R)"` | Vendor |
| Copyright 2005-2025 | Date range |
| `"ptxocg.0.0"` | OCG backend version tag |

The `"ptxocg.0.0"` tag also appears in `sub_43A400` (compilation setup) and at
address `0x1CE74AB` in the `.rodata` section, identifying the backend optimizer
component embedded inside ptxas.

## Default Target Architecture

`sub_6784B0` returns **sm_75** (Turing) as the default compilation target when no
`--gpu-name` flag is supplied.  This is consistent with the CUDA 13.0 toolkit
defaulting to a Turing-class GPU.

The full set of architecture strings referenced in the front-end validators
(addresses 0x460000-0x4D5000) includes:

```text
sm_20  sm_30  sm_35  sm_50  sm_60  sm_75  sm_80  sm_86  sm_89  sm_90
```

with `sm_%d` format-string patterns covering all supported SM codes.

## Output ELF Format

Cubins emitted by ptxas use the ELF standard with:

| Field | Value |
|-------|-------|
| `e_machine` | `EM_CUDA` (0xBE = 190) |
| ELF class | ELFCLASS32 or ELFCLASS64 (per target) |
| Custom section type | `SHT_CUDA_INFO` = 0x70000064 |
| Magic (code object header) | `0x16375564E` (`"dUWc"` + version nibble) |

The SM-version-to-code-object mapping lives in the ELF emitter at
`sub_1C9F280`. Example encodings recovered from `sub_A3D000` range:

| `field[93]` | Target | Version encoding |
|-------------|--------|------------------|
| 12288 | sm_30 | 0x70007 |
| 20481 | sm_50 | 0xC000C |

## Build System Metadata

The source path leaked through `__FILE__` macros in the knobs infrastructure
reveals the NVIDIA internal build tree layout:

```text
/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/
  drivers/common/utils/generic/impl/generic_knobs_impl.h
```

Key observations:
- **`/dvs/p4/`** -- Perforce depot root on the DVS (Driver Verification System) build farm.
- **`sw/rel/gpgpu/toolkit/r13.0/`** -- Release branch for CUDA toolkit 13.0.
- **`compiler/drivers/common/`** -- Shared compiler driver code (used by both ptxas and cicc).
- **`generic_knobs_impl.h`** -- The knob system implementation header; the `__FILE__` macro at lines 395-1090 of this file is embedded in ptxas error metadata.

## Evidence Index

| Claim | Source |
|-------|--------|
| Version 13.0.88, 37.7 MB | Headers of all 30 sweep reports (e.g. `p1.23`, `p1.28`) |
| `sub_432A00` strings | `p1.01` lines 514-521 |
| `sub_6784B0` default sm_75 | User-provided; corroborated by sm_75 prevalence across all validators |
| Source path | `p1.05` lines 14-16, `p1.04a` line 628 |
| `ptxocg.0.0` | `p1.01` line 553, `p1.05` line 1256 |
| ELF emitter / EM_CUDA | `p1.30` lines 46-69 |
| SM version encoding table | `p1.08b` lines 217-237 |

## Key Functions

| Address | Size | Role | Confidence |
|---------|------|------|------------|
| `sub_432A00` | -- | CLI option registration; contains `--version` / `--list-version` self-identification strings (`"Ptx optimizing assembler"`, `"NVIDIA (R)"`, `"ptxocg.0.0"`) | 0.92 |
| `sub_43A400` | -- | Compilation setup; references the `"ptxocg.0.0"` backend version tag | 0.85 |
| `sub_6784B0` | -- | Default target architecture selector; returns **sm_75** (Turing) when no `--gpu-name` flag is supplied | 0.90 |
| `sub_1C9F280` | -- | ELF emitter; SM-version-to-code-object mapping for cubin output | 0.85 |
| `sub_A3D000` | -- | SM version encoding table; example encodings (12288 = sm_30, 20481 = sm_50) | 0.80 |
