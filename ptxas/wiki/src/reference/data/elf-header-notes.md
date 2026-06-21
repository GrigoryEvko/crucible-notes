# Cubin ELF Header, Program Headers & NOTE Sections

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base 0x400000 (non-PIE).*

Byte-exact capture of the cubin ELF header, the 6 program headers a final cubin carries, the two NVIDIA NOTE sections, and the `.nv.compat` EICOMPAT attribute stream, obtained by emitting real cubins (`ptxas -arch=<sm> [-c]` on minimal PTX) and reading the raw header / phdr / note bytes.

## ELF identification & header

| Field | Final cubin | `-c` relocatable | Notes |
|---|---|---|---|
| `e_ident[EI_OSABI]` | `0x41` | `0x41` | earlier builds used the CUDA OSABI tag `0x33` |
| `e_ident[EI_ABIVERSION]` | `0x08` | `0x08` | cuobjdump prints `ABI=8`; earlier builds used `0x07` |
| `e_machine` | `190` (`0xBE`, EM_CUDA) | `190` | the ptxas host binary itself is `EM_X86_64=62` |
| `e_type` | `2` (ET_EXEC) | `1` (ET_REL) | final = assembled cubin; relocatable keeps `SHT_CUDA_*` types |
| `e_version` | `1` (EV_CURRENT) | `1` | |
| `e_entry` | `0` | `0` | device entry is resolved by the driver, not ELF `e_entry` |
| `e_phnum` | `6` | `0` | a relocatable object carries no program headers |

## e_flags per architecture

The real SM lives in bits 8–15, the virtual SM in bits 16–23. Full words captured from emitted cubins:

| Arch | e_flags | real SM byte | format nibble |
|---|---|---|---|
| sm_75 | `0x06004b04` | `0x4b` (75) | `0x04` |
| sm_80 | `0x06005004` | `0x50` (80) | `0x04` |
| sm_86 | `0x06005604` | `0x56` (86) | `0x04` |
| sm_89 | `0x06005904` | `0x59` (89) | `0x04` |
| sm_90 / sm_90a | `0x06005a04` | `0x5a` (90) | `0x04` |
| sm_100 / sm_100a | `0x06006402` | `0x64` (100) | `0x02` |
| sm_103 / sm_103a | `0x06006702` | `0x67` (103) | `0x02` |
| sm_120 / sm_120a | `0x06007802` | `0x78` (120) | `0x02` |
| sm_121 / sm_121a | `0x06007902` | `0x79` (121) | `0x02` |

`sm_90` and `sm_90a` produce identical `e_flags`; the `'a'`/`'f'` variant is carried in `.nv.compat` `EICOMPAT_ATTR_ISA_CLASS` and the `.note.nv.cuinfo` virtual-SM field, not in `e_flags`.

## Program headers (final sm_90 cubin)

A final cubin carries 6 program headers; a `-c` relocatable object carries none.

| PHDR | Type | Flags | Covers |
|---|---|---|---|
| PH0 | `PT_PHDR` | `R` (`0x4`) | program-header self-descriptor |
| PH1 | `PT_LOAD` | `R` (`0x4`) | read-only image group (`.nv.info` / `.symtab` / notes / read-only) |
| PH2 | `PT_LOAD` | `R` (`0x4`) | read-only driver constant banks (`.nv.constant3` / `.nv.constant4`) |
| PH3 | `PT_LOAD` | `R+X` (`0x5`) | executable SASS (`.text.<entry>`) |
| PH4 | `PT_LOAD` | `R+W` (`0x6`) | NOBITS group (`.nv.shared.<e>` + `.nv.global`); `memsz>0`, `filesz=0` |
| PH5 | `PT_LOAD` | `R` (`0x4`) | per-entry constant-bank-0 param area (`.nv.constant0.<entry>`) |

## NOTE sections

`sh_type = SHT_NOTE (0x07)`; owner string `"NVIDIA Corp\0"` (`namesz=12`); note version `u16 = 2`.

| Section | n_type | descsz | Description |
|---|---|---|---|
| `.note.nv.tkinfo` | `0x7d0` (2000) | 136 | toolkit info: `noteVer=2`, tool name `"ptxas"`, tool version `"Cuda compilation tools, release 13.0, V13.0.88"`, build branch, and the literal command-line args (e.g. `"-arch sm_90 "`). `sh_flags = 0x02000000` (OS-specific bit). |
| `.note.nv.cuinfo` | `0x3e8` (1000) | 8 | CUDA info: 8-byte desc = `u16 noteVer=2`, `u16 virtualSM` (`0x5a`=90 for sm_90), `u32 CUDA-API-version` (`0x82`=130). `sh_flags = 0x01000040` (OS bit + `SHF_INFO_LINK`). Raw: `02 00 | 5a 00 | 82 00 00 00`. |
| `.note.nv.cuver` | — | — | CUDA-version note name present in the binary string table; emitted in some configs. |

## .nv.compat EICOMPAT stream

`.nv.compat` (`SHT_CUDA_COMPAT_INFO = 0x70000086`) is an attribute stream whose wire format is identical to `.nv.info` — `[EIFMT(1B)][attr(1B)][value]`, 4-byte aligned. It is present in this build (absent in earlier ones) and carries the architecture-variant (`'a'`/`'f'`) and Mercury-finalizer capability info. A plain sm_90 kernel emits 7 attributes. The EICOMPAT names are not reachable through a name-pointer array in the binary; codes were recovered by pairing the on-wire `attr` byte against `cuobjdump`'s symbolic name for the same element.

| Code | Name | EIFMT | Observed value |
|---:|---|---|---|
| 0 | `EICOMPAT_ATTR_ERROR` | — | sentinel; never emitted |
| 1 | `EICOMPAT_ATTR_PAD` | — | alignment padding |
| 2 | `EICOMPAT_ATTR_ISA_CLASS` | BVAL | `0x01` — arch-variant selector (carries the `'a'`/`'f'` distinction) |
| 3 | `EICOMPAT_ATTR_INST_TENSORMAP_V1` | BVAL | `0x00` — tensormap v1 requirement level |
| 4 | `EICOMPAT_ATTR_INST_TCGEN05_MMA_DEPRECATED` | — | (string present; deprecated variant) |
| 5 | `EICOMPAT_ATTR_INST_TCGEN05_MMA` | BVAL | `0x05` — tcgen05 MMA requirement level (Blackwell tensor core) |
| 6 | `EICOMPAT_ATTR_ENABLE_OPPORTUNISTIC_FINALIZATION` | BVAL | `0x01` |
| 7 | `EICOMPAT_ATTR_MERCURY_ISA_MAJOR_MINOR_VERSION` | HVAL | `0x0101` (v1.1 on sm_90) |
| 8 | `EICOMPAT_ATTR_MERCURY_ISA_PATCH_VERSION` | — | (string present) |
| 9 | `EICOMPAT_ATTR_CUDA_ACCELERATOR_TARGET` | BVAL | `0x00` — accelerator target id; first element emitted |
| 11 | `EICOMPAT_ATTR_CAN_FASTPATH_FINALIZE` | SVAL | 8-byte capability descriptor |

Emission order for an sm_90 kernel (on-wire, not numeric): code 9, 2, 5, 7, 3, 6, 11. Raw bytes (off `0x63c`, size `0x24`):

```text
02 09 00 | 02 02 01 | 02 05 05 | 03 07 01 01 | 02 03 00 | 02 06 01 | 04 0b 08 00  00000000 00000000
```
