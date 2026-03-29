# nvopen-tools

Reverse engineering reference for NVIDIA's CUDA compiler toolchain.

**[Documentation](https://grigoryevko.github.io/nvopen-tools/)**

## Components

| Component | Description | Docs | Status |
|---|---|---|---|
| **cicc** | CUDA C→PTX compiler (60 MB, LLVM 20.0.0 + EDG 6.6) | **[wiki](https://grigoryevko.github.io/nvopen-tools/cicc/)** | 49 pages |
| **ptxas** | PTX→SASS assembler | — | Decompiled |
| **nvcc** | CUDA compilation driver | — | Decompiled |
| **nvlink** | GPU device linker | — | Decompiled |
| **nvptxcompiler** | PTX JIT compilation library | — | Decompiled |
| **cudafe++** | CUDA C++ frontend preprocessor | — | Decompiled |
| **fatbin** | Fat binary manipulation tools | [readme](fatbin/README.md) | Released |

All analysis is from static reverse engineering of stripped x86-64 ELF binaries using IDA Pro 9.x. No source code or any other restricted or copyrighted material was used — all findings derive solely from analysis of compiled binaries.

## Legal

The CUDA Toolkit is freely distributed by NVIDIA at [developer.nvidia.com](https://developer.nvidia.com/cuda-downloads) without NDA or access restrictions. Reverse engineering of publicly distributed software for research, education, and interoperability is protected by the Digital Millennium Copyright Act (17 U.S.C. § 1201(f)) and established court precedent (*Sega v. Accolade*, 977 F.2d 1510; *Sony v. Connectix*, 203 F.3d 596) in the United States, and by EU Directive 2009/24/EC (Articles 5–6) in the European Union. No proprietary source code, trade secrets, or confidential materials were used.

## Fat Binary Tools

Toolkit for manipulating `.nv_fatbin` sections found in CUDA libraries. Supports SM 75–121, ZSTD compression (levels 1–22), ELF/PTX/LTOIR entries, variable-length headers (64/80/112 bytes).

```bash
cd fatbin && make

# Extract .nv_fatbin from a shared library
objcopy --dump-section .nv_fatbin=output.fatbin libcublasLt.so

# Analyze
./fatbin_dump output.fatbin --list-elf

# Extract all entries with metadata
./fatbin_unpack output.fatbin /tmp/extracted

# Repack with maximum compression
./fatbin_repack /tmp/extracted repacked.bin 22
```

See [fatbin/README.md](fatbin/README.md) for full documentation and [fatbin/FORMAT_SPECIFICATION.md](fatbin/FORMAT_SPECIFICATION.md) for the binary format spec.

## License

MIT
