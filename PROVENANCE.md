# Binary Provenance

This document pins the exact NVIDIA toolchain binaries that the wikis in this
repository were reverse-engineered from. Every digest below was produced by
downloading the corresponding NVIDIA-published wheel and hashing the extracted
ELF in place; the wheel digests and inner paths are recorded so that any third
party can obtain **byte-identical** inputs and independently reproduce or audit
every result here.

All analyzed binaries are Linux `x86-64` `ELF64`. Digests are SHA-256.

## Binaries under analysis

| Binary    | Version  | Role                                             | SHA-256 |
|-----------|----------|--------------------------------------------------|---------|
| `ptxas`   | 13.0.88  | PTX → SASS assembler / optimizer                 | `daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2` |
| `cicc`    | 13.0.88  | NVVM IR → PTX device compiler (LLVM-based)       | `475a9486f1ccc9408323cc75ea2fa11599f08e9dee137bb7ac7150ce5208c425` |
| `cudafe++`| 13.0.88  | C++ front-end splitter (host/device separation)  | `82ba595cbd84843a8461e09c84ee83cfd47c645ad502fdb269f505b7416552b4` |
| `nvlink`  | 13.0.88  | Device-code (relocatable cubin) linker           | `b67c653b9b70bce76b55dded170618bea82f58bdac821bca7c6a7829a4d2b125` |
| `tileiras`| 13.1.80  | Tile-IR assembler (Blackwell tcgen05 path)       | `f0eb415767f403c96cbabf0817c3bcf70a50f88dfc8845fe36ebe21635fa6707` |
| `nvdisasm`| 13.1.115 | SASS disassembler / control-flow extractor       | `a81a7598c66b7c56660fd9fc4138ddc4dbd3eff54aeda7eb999c2445770a9582` |

## Source packages

NVIDIA publishes these binaries on its own Python package index,
`https://pypi.nvidia.com` (a PEP 503 simple index). Wheels are plain ZIP
archives; the executables are stored uncompressed-path under `nvidia/cu13/`.
The four CUDA 13.0 compiler executables come from two packages, because `cicc`
(the NVVM component) was split out of `nvidia-cuda-nvcc` in the CUDA 13 wheel
layout and now ships in `nvidia-nvvm`.

### nvidia-cuda-nvcc 13.0.88 — `ptxas`, `cudafe++`, `nvlink`

- URL: `https://pypi.nvidia.com/nvidia-cuda-nvcc/nvidia_cuda_nvcc-13.0.88-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl`
- Wheel SHA-256: `56fe502eb77625a12f25172caa3cdddb4e4c8ba2c8c17dba44b164761b380f03`
- Wheel size: 37,384,532 B

| Member                       | Size (B)   | SHA-256 |
|------------------------------|------------|---------|
| `nvidia/cu13/bin/ptxas`      | 37,741,528 | `daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2` |
| `nvidia/cu13/bin/cudafe++`   |  8,910,936 | `82ba595cbd84843a8461e09c84ee83cfd47c645ad502fdb269f505b7416552b4` |
| `nvidia/cu13/bin/nvlink`     | 38,140,392 | `b67c653b9b70bce76b55dded170618bea82f58bdac821bca7c6a7829a4d2b125` |

(The same wheel also carries `nvcc`, `fatbinary`, `bin2c`, `__nvcc_device_query`,
and the CRT link stubs; those are not subjects of this analysis.)

### nvidia-nvvm 13.0.88 — `cicc`

- URL: `https://pypi.nvidia.com/nvidia-nvvm/nvidia_nvvm-13.0.88-py3-none-manylinux2010_x86_64.manylinux_2_12_x86_64.whl`
- Wheel SHA-256: `c5f41ffeb6466944a026dfa5317d7d85355c119bbec279205d22f1869d1054e0`
- Wheel size: 61,601,415 B

| Member                                      | Size (B)   | SHA-256 |
|---------------------------------------------|------------|---------|
| `nvidia/cu13/nvvm/bin/cicc`                 | 76,506,792 | `475a9486f1ccc9408323cc75ea2fa11599f08e9dee137bb7ac7150ce5208c425` |
| `nvidia/cu13/nvvm/libdevice/libdevice.10.bc`|    464,132 | `91334d6e12748f6cb5bbf0a1cd965a56bcd93dc4f496d2e5c5f8c6e523094356` |

(`libdevice.10.bc` is the device math bitcode library `cicc` links against; listed
for completeness as it is part of the same compile step.)

### nvidia-cuda-tileiras 13.1.80 — `tileiras`

- URL: `https://pypi.nvidia.com/nvidia-cuda-tileiras/nvidia_cuda_tileiras-13.1.80-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl`
- Wheel SHA-256: `dbf5ab15e8ad70fad28ce3cc2398e71bda55ce90dae32fe92c09f07d84ce2e02`
- Wheel size: 35,796,685 B

| Member                      | Size (B)   | SHA-256 |
|-----------------------------|------------|---------|
| `nvidia/cu13/bin/tileiras`  | 91,451,032 | `f0eb415767f403c96cbabf0817c3bcf70a50f88dfc8845fe36ebe21635fa6707` |

### nvdisasm 13.1.115 — CUDA Toolkit archive

`nvdisasm` 13.1.115 is **not** published as a standalone wheel: the
`nvidia-cuda-nvdisasm` index begins at 13.2.51, so the 13.1.115 build is only
available inside the full toolkit. Obtain it from the **CUDA Toolkit 13.1
archive** (<https://developer.nvidia.com/cuda-toolkit-archive>), component
`cuda_nvdisasm`, member `bin/nvdisasm`. The digest above is the authoritative
identity check regardless of acquisition path.

## Reproducing the exact binary set

The three wheels resolve without authentication and without installing a CUDA
toolkit:

```bash
pip download --no-deps --index-url https://pypi.nvidia.com \
    nvidia-cuda-nvcc==13.0.88 \
    nvidia-nvvm==13.0.88 \
    nvidia-cuda-tileiras==13.1.80
```

Each wheel is a ZIP archive; extract the executables directly:

```bash
unzip -j nvidia_cuda_nvcc-13.0.88-*.whl   'nvidia/cu13/bin/ptxas'      'nvidia/cu13/bin/cudafe++' 'nvidia/cu13/bin/nvlink'
unzip -j nvidia_nvvm-13.0.88-*.whl        'nvidia/cu13/nvvm/bin/cicc'
unzip -j nvidia_cuda_tileiras-13.1.80-*.whl 'nvidia/cu13/bin/tileiras'
```

## Verifying byte-identity

Save the following as `SHA256SUMS` next to the extracted binaries and run
`sha256sum -c SHA256SUMS`:

```text
daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2  ptxas
475a9486f1ccc9408323cc75ea2fa11599f08e9dee137bb7ac7150ce5208c425  cicc
82ba595cbd84843a8461e09c84ee83cfd47c645ad502fdb269f505b7416552b4  cudafe++
b67c653b9b70bce76b55dded170618bea82f58bdac821bca7c6a7829a4d2b125  nvlink
f0eb415767f403c96cbabf0817c3bcf70a50f88dfc8845fe36ebe21635fa6707  tileiras
a81a7598c66b7c56660fd9fc4138ddc4dbd3eff54aeda7eb999c2445770a9582  nvdisasm
```

To verify a downloaded wheel before extracting:

```text
56fe502eb77625a12f25172caa3cdddb4e4c8ba2c8c17dba44b164761b380f03  nvidia_cuda_nvcc-13.0.88-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
c5f41ffeb6466944a026dfa5317d7d85355c119bbec279205d22f1869d1054e0  nvidia_nvvm-13.0.88-py3-none-manylinux2010_x86_64.manylinux_2_12_x86_64.whl
dbf5ab15e8ad70fad28ce3cc2398e71bda55ce90dae32fe92c09f07d84ce2e02  nvidia_cuda_tileiras-13.1.80-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
```

## Release mapping

The four C++/PTX-stage compiler executables are pinned at the CUDA 13.0 build
(component version 13.0.88). `tileiras` and `nvdisasm` are taken from the CUDA
13.1 release line; their differing patch levels (13.1.80 vs 13.1.115) reflect
independent per-component versioning within a single toolkit release and are
expected.

## Distribution policy and legal basis

This repository documents the observable structure and behavior of the listed
binaries through static analysis. It does not contain, redistribute, or derive
from NVIDIA source code, and it does not redistribute the binaries themselves.
The NVIDIA CUDA Toolkit EULA does not grant redistribution rights for these
compiler executables; accordingly, this project pins each input by SHA-256 and
links to NVIDIA's own distribution channels rather than re-hosting any file.

Reverse engineering for interoperability and for the production of independent
documentation is recognized under, among others, 17 U.S.C. §1201(f) (the DMCA
reverse-engineering exception), *Sega Enterprises Ltd. v. Accolade, Inc.*,
977 F.2d 1510 (9th Cir. 1992), *Sony Computer Entertainment, Inc. v. Connectix
Corp.*, 203 F.3d 596 (9th Cir. 2000), and EU Directive 2009/24/EC Article 6.
The digests above let any third party acquire byte-identical inputs and
independently reproduce or audit the analysis.

This section states the basis on which the project operates; it is not legal
advice.
