# crucible-notes

Reverse engineering reference for NVIDIA's CUDA compiler toolchain, Google's TPU compiler stack, and AWS's Neuron (Trainium/Inferentia) compiler and runtime stack.

> **AI-GENERATED REVERSE-ENGINEERING NOTES — AUTHOR'S PERSONAL REFERENCE ONLY.**
> Everything here is a best-guess reconstruction from static binary analysis, **not a reliable source.**

**[Documentation](https://gh.evko.io/crucible-notes/)**

## Components

| Component | Description | Docs | Status |
|---|---|---|---|
| **cicc** | CUDA C→PTX compiler (60 MB, LLVM 20.0.0 + EDG 6.6) | **[wiki](https://gh.evko.io/crucible-notes/cicc/)** | 267 pages |
| **tileiras** | Cuda Tile IR optimizing assembler (88 MB, MLIR bytecode → TileAS → PTX/SASS) | **[wiki](https://gh.evko.io/crucible-notes/tileiras/)** | 143 pages |
| **ptxas** | PTX→SASS assembler (37.7 MB, proprietary, 159-phase pipeline) | **[wiki](https://gh.evko.io/crucible-notes/ptxas/)** | 73 pages |
| **nvlink** | GPU device linker (37 MB, 95% embedded ptxas) | **[wiki](https://gh.evko.io/crucible-notes/nvlink/)** | 92 pages |
| **nvcc** | CUDA compilation driver | — | Decompiled |
| **nvptxcompiler** | PTX JIT compilation library (86 MB static lib, 392 objects) | — | Decompiled |
| **cudafe++** | CUDA C++ frontend (8.5 MB, EDG 6.6, 6,483 functions) | **[wiki](https://gh.evko.io/crucible-notes/cudafe++/)** | 69 pages |
| **libtpu** | Google TPU PJRT plugin (745 MB, 6 silicon gens, LLO VLIW ISA + cost model) | **[wiki](https://gh.evko.io/crucible-notes/libtpu/)** | 424 pages |
| **neuronx-cc** | AWS Neuron compiler (Trainium/Inferentia) — hlo-opt/Penguin/NKI front-end, libBIR + libwalrus backend, NEFF packaging | **[wiki](https://gh.evko.io/crucible-notes/neuronx-cc/)** | 355 pages |
| **neuronx-runtime** | AWS Neuron runtime — aws-neuronx-dkms kernel driver, libnrt.so runtime, libncfw.so firmware + Xtensa payloads | **[wiki](https://gh.evko.io/crucible-notes/neuronx-runtime/)** | 182 pages |
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

<details>
<summary>Documentation conventions</summary>

Wiki pages follow a shared house style — see [WIKI_STYLE_GUIDE.md](WIKI_STYLE_GUIDE.md).

</details>

## License

MIT
