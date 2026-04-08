# ROT13-Encoded Pass Names

NVIDIA uses ROT13 to obfuscate internal names throughout the nvlink v13.0.88 binary. Over 30,000 strings are ROT13-encoded, spanning SASS instruction mnemonics, compiler pass names, configuration knobs, ELF section names, and Mercury (SM100+ Blackwell) builtin instruction templates. The decoder function at `sub_1A40AC0` is SIMD-vectorized, processing 16 bytes at a time via SSE `_mm_load_si128` intrinsics. This page catalogs every identified category of ROT13-encoded content with decode tables.

## Overview

| Type | Encoded Count | Address Region | Key Prefix |
|---------:|--------------:|----------------|------------|
| SASS opcode mnemonics | ~320 per arch table | `0x1769B50` (SM70), `0x1782540` (SM100), `0x1848F70` (SM120) | uppercase: `VZNQ`, `SZHY` |
| Mercury builtins | 644 | `0x25168`--`0x26344` (string pool) | `ZREPHEL_` |
| Compiler pass names | 151 | `0x2443000`--`0x2445000` (phase table) | CamelCase: `BevYbbcHaebyyvat` |
| Knob/option names | ~1,287 | `0x23F0000`--`0x2460000` | CamelCase: `ErtNyybpFcvyyKOybpx2` |
| ELF section names | 13 meaningful | scattered | `.npp::s16`, `.fc::2gb4` |
| EIATTR names | 111 | scattered | `EIATTR_*`, `EICOMPAT_*` |
| Relocation type names | 186 | scattered | `R_CUDA_*`, `R_MERC_*` |

Total ROT13-encoded strings: approximately 30,349.

## ROT13 Decoder Function

**Address**: `sub_1A40AC0` (15,629 bytes, 449 decompiled lines)

The decoder implements a classic ROT13 substitution cipher with SIMD acceleration:

1. **Scalar preamble** -- processes unaligned head bytes one at a time: `A-M` maps to `N-Z` (+13), `N-Z` maps to `A-M` (-13), same for lowercase
2. **SIMD loop** -- loads 16 bytes via `_mm_load_si128`, applies vectorized ROT13 using packed byte comparisons and conditional adds/subtracts
3. **Scalar epilogue** -- handles remaining tail bytes after the last aligned 16-byte boundary

The input is copied to a fresh heap allocation (capacity rounded to next power of 2), decoded in-place, then returned. All SASS opcode mnemonic lookups flow through this function during table initialization.

## SASS Opcode Mnemonics

Three per-architecture opcode table constructors populate ROT13-encoded instruction mnemonic tables. Each entry is a `(name_ptr, name_length)` pair starting at offset +4184 within the table object.

| Constructor | Address | Architecture | Entries | Size |
|-------------|---------|-------------|--------:|-----:|
| `sm70_opcode_table_constructor` | `sub_1769B50` | SM70/SM75 (Volta/Turing) | ~130 | 24,230 bytes |
| `sm100_opcode_table_constructor` | `sub_1782540` | SM100 (Blackwell) | ~400 | 111,076 bytes |
| `sm120_opcode_table_constructor` | `sub_1848F70` | SM120 (RTX 50xx) | ~400+ | 89,621 bytes |
| `sass_opcode_table_initializer` | `sub_1A85E40` | Emission pass table | ~320 | 23,753 bytes |

### Core Arithmetic

| ROT13 | Decoded | Description |
|--------|---------|-------------|
| `VZNQ` | IMAD | Integer multiply-add |
| `VZNQ_JVQR` | IMAD_WIDE | Integer multiply-add wide |
| `VNQQ3` | IADD3 | 3-input integer add |
| `VNQQ32V` | IADD32I | Integer add with 32-bit immediate |
| `VZHY32V` | IMUL32I | Integer multiply with 32-bit immediate |
| `VZAZK` | IMNMX | Integer min/max |
| `VNOF` | IABS | Integer absolute value |
| `VFRGC` | ISETP | Integer set predicate |
| `SNQQ` | FADD | FP32 add |
| `SNQQ32V` | FADD32I | FP32 add with 32-bit immediate |
| `SZHY` | FMUL | FP32 multiply |
| `SZHY32V` | FMUL32I | FP32 multiply with 32-bit immediate |
| `SSZN` | FFMA | FP32 fused multiply-add |
| `SSZN32V` | FFMA32I | FP32 FMA with 32-bit immediate |
| `SZAZK` | FMNMX | FP32 min/max |
| `SZAZK3` | FMNMX3 | FP32 3-input min/max |
| `SFRGC` | FSETP | FP32 set predicate |
| `SFJMNQQ` | FSWZADD | FP32 swizzled add |
| `QFRGC` | DSETP | FP64 set predicate |

### FP16/BF16 (Tensor-path)

| ROT13 | Decoded | Description |
|--------|---------|-------------|
| `SNQQ2` | FADD2 | Packed FP16x2 add |
| `SZHY2` | FMUL2 | Packed FP16x2 multiply |
| `SSZN2` | FFMA2 | Packed FP16x2 FMA |
| `UNQQ2` | HADD2 | Packed FP16x2 add (half) |
| `UNQQ2_32V` | HADD2_32I | HADD2 with 32-bit immediate |
| `UZHY2` | HMUL2 | Packed FP16x2 multiply (half) |
| `UZHY2_32V` | HMUL2_32I | HMUL2 with 32-bit immediate |
| `USZN2` | HFMA2 | Packed FP16x2 FMA (half) |
| `USZN2_32V` | HFMA2_32I | HFMA2 with 32-bit immediate |
| `USZN2_ZZN` | HFMA2_MMA | HFMA2 for matrix multiply |
| `UZAZK2` | HMNMX2 | Packed FP16x2 min/max |
| `UFRGC2` | HSETP2 | Packed FP16x2 set predicate |
| `UFRG2` | HSET2 | Packed FP16x2 set |
| `SUNQQ` | FHADD | FP16 add (scalar half) |
| `SUNQQ2` | FHADD2 | Packed FP16x2 add (float-half) |
| `SUSZN` | FHFMA | FP16 FMA (scalar half) |
| `SUSZN2` | FHFMA2 | Packed FP16x2 FMA (float-half) |
| `SUZHY2` | FHMUL2 | Packed FP16x2 multiply (float-half) |
| `DNQQ4` | QADD4 | Packed int8x4/FP8x4 quad add |
| `DSZN4` | QFMA4 | Packed quad FMA |
| `DZHY4` | QMUL4 | Packed quad multiply |

### Uniform Register (SM75+)

| ROT13 | Decoded | Description |
|--------|---------|-------------|
| `HVNQQ3` | UIADD3 | Uniform integer 3-input add |
| `HVZNQ` | UIMAD | Uniform integer multiply-add |
| `HVZAZK` | UIMNMX | Uniform integer min/max |
| `HVNOF` | UIABS | Uniform integer absolute value |
| `HVFRGC` | UISETP | Uniform integer set predicate |
| `HSNQQ` | UFADD | Uniform FP32 add |
| `HSZHY` | UFMUL | Uniform FP32 multiply |
| `HSSZN` | UFFMA | Uniform FP32 FMA |
| `HSZAZK` | UFMNMX | Uniform FP32 min/max |
| `HSFRGC` | UFSETP | Uniform FP32 set predicate |
| `HSFRY` | UFSEL | Uniform FP32 select |
| `HSUNQQ` | UFHADD | Uniform FP16 add |
| `HSUSZN` | UFHFMA | Uniform FP16 FMA |
| `HSEAQ` | UFRND | Uniform FP round |
| `HS2SC` | UF2FP | Uniform float-to-FP convert |
| `HS2VC` | UF2IP | Uniform float-to-integer convert |
| `HV2SC` | UI2FP | Uniform integer-to-FP convert |
| `HV2VC` | UI2IP | Uniform integer-to-integer convert |
| `HYBC3` | ULOP3 | Uniform 3-input logic op |
| `HYBC32V` | ULOP32I | Uniform logic op with 32-bit immediate |
| `HCYBC3` | UPLOP3 | Uniform predicate 3-input logic op |
| `HCEZG` | UPRMT | Uniform byte permute |
| `HCFRGC` | UPSETP | Uniform predicate set predicate |
| `HFTKG` | USGXT | Uniform sign-extend |
| `HOZFX` | UBMSK | Uniform bit mask |
| `HOERI` | UBREV | Uniform bit reverse |
| `HC2HE` | UP2UR | Uniform predicate to uniform register |
| `HE2HC` | UR2UP | Uniform register to uniform predicate |
| `HFRGZNKERT` | USETMAXREG | Uniform set max registers |
| `HFRGFUZFM` | USETSHMSZ | Uniform set shared memory size |
| `PF2HE` | CS2UR | Control status to uniform register |

### Bitwise/Logic

| ROT13 | Decoded | Description |
|--------|---------|-------------|
| `YBC3` | LOP3 | 3-input logic operation (LUT-based) |
| `YBC32V` | LOP32I | Logic op with 32-bit immediate |
| `CYBC3` | PLOP3 | Predicate 3-input logic op |
| `OZFX` | BMSK | Bit mask generate |
| `FTKG` | SGXT | Sign-extend |
| `CEZG` | PRMT | Byte permute |
| `FUS` | SHF | Funnel shift |
| `YRN` | LEA | Load effective address |
| `C2E` | P2R | Predicate to register |
| `E2C` | R2P | Register to predicate |
| `OZBI_O` | BMOV_B | Barrier register move (barrier) |
| `OZBI_E` | BMOV_R | Barrier register move (register) |
| `PF2E_32` | CS2R_32 | Control/status to register 32-bit |
| `PF2E_64` | CS2R_64 | Control/status to register 64-bit |

### Memory Operations

| ROT13 | Decoded | Description |
|--------|---------|-------------|
| `ZBI` | MOV | Move (register) |
| `ZBI32V` | MOV32I | Move 32-bit immediate |
| `ZBI64VHE` | MOV64IUR | Move 64-bit immediate to uniform register |
| `FRY` | SEL | Select (conditional move) |
| `YQTFGF` | LDGSTS | Load global, store shared (async copy) |
| `YQTQRCONE` | LDGDEPBAR | Load global with dependency barrier |
| `YQTZP` | LDGMC | Load global multicast |
| `YQGENZ` | LDTRAM | Load texture RAM |
| `ZRZONE` | MEMBAR | Memory barrier |
| `ZRZFRG` | MEMSET | Memory set |
| `NGBZT` | ATOMG | Atomic (global) |
| `NGBZF` | ATOMS | Atomic (shared) |
| `FHNGBZ` | SUATOM | Surface atomic |
| `FHERQ` | SURED | Surface reduction |
| `FHDHREL` | SUQUERY | Surface query |
| `FPNGGRE` | SCATTER | Scatter store |
| `TNGURE` | GATHER | Gather load |
| `SBBGCEVAG` | FOOTPRINT | Texture footprint query |

### Control Flow

| ROT13 | Decoded | Description |
|--------|---------|-------------|
| `OEN_VZZ` | BRA_IMM | Branch (immediate offset) |
| `WZC_VZZ` | JMP_IMM | Jump (immediate) |
| `OERNX` | BREAK | Break from loop |
| `OFLAP` | BSYNC | Barrier sync (convergence) |
| `SRAPR_T` | FENCE_G | Fence (global) |
| `SRAPR_F` | FENCE_S | Fence (shared) |
| `SRAPR_G` | FENCE_T | Fence (texture) |
| `CERRKVG` | PREEXIT | Pre-exit annotation |
| `REEONE` | ERRBAR | Error barrier / NOP padding |
| `QRCONE` | DEPBAR | Dependency barrier |
| `LVRYQ` | YIELD | Yield execution |
| `ABC` | NOP | No operation |
| `IBGR` | VOTE | Warp vote |
| `ZNGPU` | MATCH | Warp match |
| `ERQHK` | REDUX | Warp reduction |
| `RYRPG` | ELECT | Warp elect (leader selection) |
| `JNECFLAP` | WARPSYNC | Warp synchronization |
| `ANABFYRRC` | NANOSLEEP | Nanosecond sleep |
| `ANABGENC` | NANOTRAP | Nano trap (debug) |
| `NEEVIRF` | ARRIVES | Arrive signal |

### Warp Synchronization (SM90+ Mercury)

| ROT13 | Decoded | Description |
|--------|---------|-------------|
| `JNECTEBHC` | WARPGROUP | Warpgroup operation |
| `JNECTEBHCFRG` | WARPGROUPSET | Warpgroup set |
| `RAQPBYYRPGVIR` | ENDCOLLECTIVE | End collective operation |
| `FLAPF` | SYNCS | Sync with scoreboard |
| `NPDOYX` | ACQBLK | Acquire block |
| `NPDOHYX` | ACQBULK | Acquire bulk |
| `NPDFUZVAVG` | ACQSHMINIT | Acquire shared memory init |
| `PPGY` | CCTL | Cache control |
| `PPGYY` | CCTLL | Cache control L1 |
| `PPGYG` | CCTLT | Cache control texture |
| `HPPGY` | UCCTL | Uniform cache control |

### Matrix Multiply (Tensor Core)

| ROT13 | Decoded | Description |
|--------|---------|-------------|
| `UZZN_16` | HMMA_16 | Half-precision MMA 16-wide |
| `UZZN_16816` | HMMA_16816 | HMMA 16x8x16 |
| `UZZN_1688` | HMMA_1688 | HMMA 16x8x8 |
| `UZZN_32` | HMMA_32 | HMMA 32-wide |
| `UZZN_FC_1688` | HMMA_SP_1688 | Sparse HMMA 16x8x8 |
| `VZZN_16816` | IMMA_16816 | Integer MMA 16x8x16 |
| `VZZN_16832` | IMMA_16832 | Integer MMA 16x8x32 |
| `VZZN_88` | IMMA_88 | Integer MMA 8x8 |
| `VZZN_FC_16832` | IMMA_SP_16832 | Sparse integer MMA |
| `VZZN_FC_88` | IMMA_SP_88 | Sparse integer MMA 8x8 |
| `JTZZN` | WGMMA | Warpgroup MMA (SM90+) |

### SM100+ Blackwell Matrix Ops

| ROT13 | Decoded | Description |
|--------|---------|-------------|
| `DZZN_16816` | QMMA_16816 | Quantized MMA 16x8x16 |
| `DZZN_16832` | QMMA_16832 | Quantized MMA 16x8x32 |
| `DZZN_FS_16832` | QMMA_SF_16832 | QMMA with scale factor |
| `DZZN_FS_FC_16864` | QMMA_SF_SP_16864 | QMMA with scale + sparsity |
| `DZZN_FC_12864` | QMMA_SP_12864 | Sparse QMMA 128x64 |
| `DZZN_FC_16832` | QMMA_SP_16832 | Sparse QMMA 16x8x32 |
| `OZZN_168128` | BMMA_168128 | Binary MMA 168x128 |
| `OZZN_168256` | BMMA_168256 | Binary MMA 168x256 |
| `OZZN_88128` | BMMA_88128 | Binary MMA 88x128 |
| `BZZN_16864` | OMMA_16864 | Output MMA 168x64 |
| `BZZN_FC_168128` | OMMA_SP_168128 | Sparse output MMA |
| `ZKDZZN` | MXQMMA | Mixed-precision quantized MMA |
| `ZKDZZN_FS_16832` | MXQMMA_SF_16832 | MXQMMA with scale factor |
| `OTZZN` | BGMMA | Blackwell group MMA |
| `OTZZN_TFO` | BGMMA_GSB | BGMMA with group scoreboard |
| `QTZZN` | DGMMA | Double-precision group MMA |
| `QTZZN_TFO` | DGMMA_GSB | DGMMA with group scoreboard |
| `VTZZN` | IGMMA | Integer group MMA |
| `VTZZN_TFO` | IGMMA_GSB | IGMMA with group scoreboard |
| `UTZZN` | HGMMA | Half-precision group MMA |
| `UTZZN_TFO` | HGMMA_GSB | HGMMA with group scoreboard |

### SM100+ Unified Tensor Core (UTC)

| ROT13 | Decoded | Description |
|--------|---------|-------------|
| `HGPONE_1PGN` | UTCBAR_1CTA | UTC barrier (1 CTA) |
| `HGPONE_2PGN` | UTCBAR_2CTA | UTC barrier (2 CTA) |
| `HGPPC_1PGN` | UTCCP_1CTA | UTC copy (1 CTA) |
| `HGPPC_2PGN` | UTCCP_2CTA | UTC copy (2 CTA) |
| `HGPZZN_1PGN` | UTCMMA_1CTA | UTC MMA (1 CTA) |
| `HGPZZN_2PGN` | UTCMMA_2CTA | UTC MMA (2 CTA) |
| `HGPFUVSG_1PGN` | UTCSHIFT_1CTA | UTC shift (1 CTA) |
| `HGPFUVSG_2PGN` | UTCSHIFT_2CTA | UTC shift (2 CTA) |
| `HGPNGBZFJF` | UTCATOMSWS | UTC atomic (SWS) |
| `HGPYQFJF` | UTCLDSWS | UTC load (SWS) |
| `HGPFGFJF` | UTCSTSWS | UTC store (SWS) |
| `GPTRA05` | TCGEN05 | Tensor core generation 5 |
| `HGZNPPGY` | UTMACCTL | UTC macro cache control |
| `HGZNY2PPGY` | UTMAL2CCTL | UTC MAL L2 cache control |
| `HGZNYQT` | UTMALDG | UTC MAL load global |
| `HGZNYFG` | UTMALST | UTC MAL store |
| `HGZNCS` | UTMAPF | UTC MAP (future) |
| `HGZNFGT` | UTMASTG | UTC MA store global |
| `HGZNERQT` | UTMAREDG | UTC MA reduction global |
| `HGZERQT` | UTMREDG | UTC M reduction global |

### Barrier/MBarrier

| ROT13 | Decoded | Description |
|--------|---------|-------------|
| `ZONEEVRE_NEEVIR` | MBARRIER_ARRIVE | MBarrier arrive |
| `ZONEEVRE_NEEVIR_QEBC` | MBARRIER_ARRIVE_DROP | MBarrier arrive + drop |
| `ZONEEVRE_PC_NFLAP_NEEVIR` | MBARRIER_CP_ASYNC_ARRIVE | MBarrier cp-async arrive |
| `ZONEEVRE_VAVG` | MBARRIER_INIT | MBarrier init |
| `ZONEEVRE_VAINY` | MBARRIER_INVAL | MBarrier invalidate |
| `ZONEEVRE_GEL_JNVG` | MBARRIER_TRY_WAIT | MBarrier try-wait |
| `ZONEEVRE_GEL_JNVG_CNEVGL` | MBARRIER_TRY_WAIT_PARITY | MBarrier try-wait with parity |
| `ZONEEVRE_GRFG_JNVG` | MBARRIER_TEST_WAIT | MBarrier test-wait |
| `ZONEEVRE_GRFG_JNVG_CNEVGL` | MBARRIER_TEST_WAIT_PARITY | MBarrier test-wait with parity |
| `ONE_VAQRKRQ` | BAR_INDEXED | Barrier (indexed) |

### Texture/Surface/Sampling

| ROT13 | Decoded | Description |
|--------|---------|-------------|
| `GRKGHER` | TEXTURE | Texture operation |
| `GRYCI` | TEYPL | Texture eyeply? |
| `CVKUF` | PIXHS | Pixel half-sample |
| `CVKYQ` | PIXLD | Pixel load |
| `INOFQVSS` | VABSDIFF | Vector absolute difference |
| `INOFQVSS4` | VABSDIFF4 | Vector absolute diff (4-wide) |
| `PERQHK` | CREDUX | Predicate reduction |
| `CZGEVT` | PMTRIG | Performance monitor trigger |
| `PFZGRFG` | CSMTEST | CSM test |

### Special/Miscellaneous

| ROT13 | Decoded | Description |
|--------|---------|-------------|
| `TRAZRGNQNGN` | GENMETADATA | Generate metadata |
| `TRGYZRZONFR` | GETLMEMBASE | Get local memory base |
| `FRGYZRZONFR` | SETLMEMBASE | Set local memory base |
| `FRGPGNVQ` | SETCTAID | Set CTA ID |
| `FRGZNKERT` | SETMAXREG | Set max registers |
| `FRGFZRZFVMR` | SETSMEMSIZE | Set shared memory size |
| `FCNEFVSL` | SPARSIFY | Sparsify operation |
| `FCZRGNQNGN` | SPMETADATA | Sparsity metadata |
| `QRPBZCERFF` | DECOMPRESS | Decompress |
| `EPCZBI` | RPCMOV | RPC move |
| `HGENPRRIRAG` | UTRACEEVENT | Trace event (GPU profiling) |
| `HIVEGPBHAG` | UVIRTCOUNT | Uniform virtual count |
| `HTRGARKGJBEXVQ` | UGETNEXTWORKID | Uniform get next work ID |

### Data Conversion

| ROT13 | Decoded | Description |
|--------|---------|-------------|
| `S2S_K` | F2F_X | Float-to-float convert (extended) |
| `S2V_K` | F2I_X | Float-to-integer convert (extended) |
| `V2S_K` | I2F_X | Integer-to-float convert (extended) |
| `SEAQ_K` | FRND_X | Float round (extended) |
| `E2HE_U` | R2UR_H | Register to uniform register (half) |

### TTU (Thread Tracing Unit / Graphics)

| ROT13 | Decoded | Description |
|--------|---------|-------------|
| `GGHPPGY` | TTUCCTL | TTU cache control |
| `GGHPYBFR` | TTUCLOSE | TTU close |
| `GGHTB` | TTUGO | TTU go |
| `GGHYQ` | TTULD | TTU load |
| `GGHYQ_PYBFR` | TTULD_CLOSE | TTU load + close |
| `GGHZNPEB` | TTUMACRO | TTU macro |
| `GGHZNPEBSHFR` | TTUMACROFUSE | TTU macro fuse |
| `GGHBCRA` | TTUOPEN | TTU open |
| `GGHFG` | TTUST | TTU store |

### UDP (Unified Data Path / SM100+)

| ROT13 | Decoded | Description |
|--------|---------|-------------|
| `HOYXPC` | UBLKCP | Unified block copy |
| `HOYXY2PPGY` | UBLKL2CCTL | Unified block L2 cache control |
| `HOYXCS` | UBLKPF | Unified block prefetch |
| `HOYXERQ` | UBLKRED | Unified block reduction |
| `HQYPONE` | UDLCBAR | UDL barrier |
| `HQYPPC` | UDLCCP | UDL copy |
| `HQYPUZZN` | UDLCHMMA | UDL HMMA |
| `HQYPVZZN` | UDLCIMMA | UDL IMMA |
| `HQYPDZZN` | UDLCQMMA | UDL QMMA |
| `HQCPOYXPC` | UDPCBLKCP | UDPC block copy |
| `HQCPOYXY2PPGY` | UDPCBLKL2CCTL | UDPC block L2 cache control |
| `HQCPOYXERQ` | UDPCBLKRED | UDPC block reduction |
| `HQCPGZNPPGY` | UDPCTMACCTL | UDPC TMA cache control |
| `HQCPGZNY2PPGY` | UDPCTMAL2CCTL | UDPC TMA L2 cache control |
| `HQCPGZNYQT` | UDPCTMALDG | UDPC TMA load global |
| `HQCPGZNERQT` | UDPCTMAREDG | UDPC TMA reduction global |
| `HQCPGZNFGT` | UDPCTMASTG | UDPC TMA store global |

## Mercury Passes (22 ROT13 Boolean Options)

All 22 Mercury-specific passes are registered in `ctor_007` at addresses `0x425A40`--`0x426080`. Each is a boolean enable/disable flag stored at a bit offset within the global options structure. The prefix `ZREPHEL` decodes to `MERCURY` -- the Blackwell (SM100+) codename.

| ROT13 Name | Decoded Name | Bit Offset | Reg. Address | Type |
|------------|-------------|-----------|-------------|----------|
| `ZrephelNffhzrCGKCbegnovyvgl` | MercuryAssumePTXPortability | `0x3D40` | `0x425A40` | assume |
| `ZrephelPbzcnpgrqNffhzrf` | MercuryCompactedAssumes | `0x3D50` | `0x425A90` | assume |
| `ZrephelPbafhzrNffhzrf` | MercuryConsumeAssumes | `0x3D60` | `0x425AE0` | assume |
| `ZrephelPbairegreFgngf` | MercuryConverterStats | `0x3D70` | `0x425B30` | diagnostics |
| `ZrephelQrcFgntrCersreAbaYvirvaCFO` | MercuryDepStagePreferNonLiveinPSB | `0x3D78` | `0x425B80` | scoreboard |
| `ZrephelQvfnoyrYrtnyvmngvbaBsGrkGbHEObhaq` | MercuryDisableLegalizationOfTexToURBound | `0x3D80` | `0x425BD0` | legalization |
| `ZrephelQhzcVafgfNfOvanel` | MercuryDumpInstsAsBinary | `0x3D90` | `0x425C20` | diagnostics |
| `ZrephelRapbqrQrpbqr` | MercuryEncodeDecode | `0x3DA0` | `0x425C70` | encoding |
| `ZrephelRapbqrArjJbexreSvyrf` | MercuryEncodeNewWorkerFiles | `0x3DB0` | `0x425CC0` | encoding |
| `ZrephelSbeprVFNPynff` | MercuryForceISAClass | `0x3DB8` | `0x425D10` | ISA/target |
| `ZrephelSbeprHaxabjaGptra05Ngge` | MercuryForceUnknownTcgen05Attr | `0x3DB9` | `0x425D60` | ISA/target |
| `ZrephelTraFnffHPbqr` | MercuryGenSassUCode | `0x3DC0` | `0x425DB0` | codegen |
| `ZrephelVafregNffhzrf` | MercuryInsertAssumes | `0x3DD0` | `0x425E00` | assume |
| `ZrephelVafregOnpxrqtrQrcone` | MercuryInsertBackedgeDepbar | `0x3DE0` | `0x425E50` | scoreboard |
| `ZrephelVafregKoybpxJnvg` | MercuryInsertXblockWait | `0x3DF0` | `0x425EA0` | scoreboard |
| `ZrephelVffhrQrynlJOFgnyyFrysYbbc` | MercuryIssueDelayWBStallSelfLoop | `0x3E00` | `0x425EF0` | scoreboard |
| `ZrephelZretrCebybthrOybpxf` | MercuryMergePrologueBlocks | `0x3E10` | `0x425F40` | codegen |
| `ZrephelCerfhzrKoybpxJnvgOrarsvpvny` | MercuryPresumeXblockWaitBeneficial | `0x3E18` | `0x425F90` | scoreboard |
| `ZrephelGrcvqNjnerFo` | MercuryTepidAwareSb | `0x3E20` | `0x425FE0` | scheduling |
| `ZrephelGenpxZhygvErnqfJneYngrapl` | MercuryTrackMultiReadsWarLatency | `0x3E30` | `0x426030` | scheduling |
| `ZrephelHfrNpgvirGuernqPbyyrpgvirVafgf` | MercuryUseActiveThreadCollectiveInsts | `0x3E40` | `0x426080` | ISA/target |
| `NqinaprqFOPebffOybpxZrephelNffhzr` | AdvancedSBCrossBlockMercuryAssume | `0x5B0` | `0x4129E0` | scoreboard |

Related global switches (also ROT13-encoded):

| ROT13 | Decoded | Reg. Address |
|--------|---------|-------------|
| `HfrZrepFrznagvpf` | UseMercSemantics | `0x424BE0` |
| `HfrZrepErfbheprf` | UseMercResources | `0x424B90` |
| `QhzcZrepBcPbhagf` | DumpMercOpCounts | `0x410F30` |

## Mercury Builtin Instructions (644 Templates)

The 644 `ZREPHEL_*` strings are SASS instruction templates for SM100+ (Blackwell) hardware intrinsic operations. Each template encodes a specific operand pattern (source register types, destination types, synchronization modes). Organized by operation family:

| Family | Template Count | Example Decoded |
|--------|---------------:|-----------------|
| mbarrier | 124 | `MERCURY_mbarrier_arrive_drop_shared_cluster_wcoopr` |
| barrier | 86 | `MERCURY_barrier_cta_red_popc_sync_unaligned` |
| warpgroup | 40 | `MERCURY_warpgroup_mma_sp_fp16_sync_srcs_r4_ur4_0` |
| atom | 36 | `MERCURY_atom_global_fp_acq_rel_dests_p_r` |
| fence | 32 | `MERCURY_fence_tensormap_generic_release_scope_cluster_cta_gpu_sys` |
| redux | 32 | `MERCURY_redux_f32_sync_unaligned_srcs_r_0` |
| addmin | 24 | `MERCURY_addmin` |
| max | 24 | `MERCURY_max_dests_p` |
| elect | 20 | `MERCURY_elect_sync_unaligned` |
| min | 20 | `MERCURY_min_dests_p` |
| max3 | 18 | `MERCURY_max3_fp` |
| match | 16 | `MERCURY_match_all_sync_unaligned` |
| vabsdiff4 | 14 | `MERCURY_vabsdiff4_srcs_ur_r_0` |
| mov | 14 | `MERCURY_mov_b32_dests_ur_srcs_sr_0` |
| createpolicy | 12 | `MERCURY_createpolicy_block` |
| mapa | 12 | `MERCURY_mapa_copy_generic_dests_r2` |
| vote | 12 | `MERCURY_vote_sync_unaligned_srcs_r_0` |
| addmax | 10 | `MERCURY_addmax` |
| cvt | 10 | `MERCURY_cvt_f16x8_u4x8` |
| cvta | 10 | `MERCURY_cvta_generic_shared_cluster_dests_ur2` |
| fma | 8 | `MERCURY_fma_f32x2` |
| red | 8 | `MERCURY_red_global_fp_release_policy` |
| shfl | 8 | `MERCURY_shfl_sync_unaligned` |
| st | 8 | `MERCURY_st_shared_cta_release` |
| cp | 6 | `MERCURY_cp_async_bulk` |
| ld | 6 | `MERCURY_ld_shared_cta_acquire` |
| min3 | 6 | `MERCURY_min3_int` |
| sad | 6 | `MERCURY_sad` |
| add | 4 | `MERCURY_add_in16x2_dests_r` |
| multimem | 4 | `MERCURY_multimem_red_release_fp` |
| predict | 4 | `MERCURY_predict_merge_1` |
| tcgen05 | 4 | `MERCURY_tcgen05_ld_16dp32bitx2_0` |
| griddepcontrol | 2 | `MERCURY_griddepcontrol` |
| selmov | 2 | `MERCURY_selmov` |

## Compiler Pass Names (Master Phase Table)

The master phase table at `0x2443000`--`0x2445000` contains 151 ROT13-encoded pass names defining the full compilation pipeline order. These are the names printed in `"After <PassName>"` diagnostic messages. Decoded and listed in pipeline execution order:

### Pre-optimization

| ROT13 | Decoded |
|--------|---------|
| `BevPurpxVavgvnyCebtenz` | OriCheckInitialProgram |
| `NccylAiBcgErpvcrf` | ApplyNvOptRecipes |
| `CebzbgrSC16` | PromoteFP16 |
| `NanylmrPbagebySybj` | AnalyzeControlFlow |
| `NqinaprqCunfrOrsberPbaiHaFhc` | AdvancedPhaseBeforeConvUnSup |
| `PbairegHafhccbegrqBcf` | ConvertUnsupportedOps |
| `FrgPbagebySybjBcYnfgVaOO` | SetControlFlowOpLastInBB |
| `NqinaprqCunfrNsgrePbaiHaFhc` | AdvancedPhaseAfterConvUnSup |
| `BevPerngrZnpebVafgf` | OriCreateMacroInsts |
| `ErcbegVavgvnyErcerfragngvba` | ReportInitialRepresentation |
| `RneylBevFvzcyrYvirQrnq` | EarlyOriSimpleLiveDead |
| `ErcynprHavsbezfJvguVzz` | ReplaceUniformsWithImm |

### Early Optimization

| ROT13 | Decoded |
|--------|---------|
| `BevFnavgvmr` | OriSanitize |
| `TrarenyBcgvzvmrRneyl` | GeneralOptimizeEarly |
| `QbFjvgpuBcgSvefg` | DoSwitchOptFirst |
| `BevOenapuBcg` | OriBranchOpt |
| `BevCresbezYvirQrnqSvefg` | OriPerformLiveDeadFirst |
| `BcgvzvmrOvaqyrffUrnqreYbnqf` | OptimizeBindlessHeaderLoads |
| `BevYbbcFvzcyvsvpngvba` | OriLoopSimplification |
| `BevFcyvgYvirEnatrf` | OriSplitLiveRanges |
| `CresbezCTB` | PerformPGO |
| `BevFgeratguErqhpr` | OriStrengthReduce |
| `BevYbbcHaebyyvat` | OriLoopUnrolling |
| `TrarengrZbiCuv` | GenerateMovPhi |
| `BevCvcryvavat` | OriPipelining |
| `FgntrNaqSrapr` | StageAndFence |
| `BevErzbirErqhaqnagOneevref` | OriRemoveRedundantBarriers |

### Mid Optimization

| ROT13 | Decoded |
|--------|---------|
| `NanylmrHavsbezfSbeFcrphyngvba` | AnalyzeUniformsForSpeculation |
| `FvaxErzng` | SinkRemat |
| `TrarenyBcgvzvmr` | GeneralOptimize |
| `QbFjvgpuBcgFrpbaq` | DoSwitchOptSecond |
| `BevYvarneErcynprzrag` | OriLinearReplacement |
| `PbzcnpgYbpnyZrzbel` | CompactLocalMemory |
| `BevCresbezYvirQrnqFrpbaq` | OriPerformLiveDeadSecond |
| `RkgenpgFunqrePbafgfSvefg` | ExtractShaderConstsFirst |
| `BevUbvfgVainevnagfRneyl` | OriHoistInvariantsEarly |
| `RzvgCFV` | EmitPSI |
| `TrarenyBcgvzvmrZvq` | GeneralOptimizeMid |
| `BcgvzvmrArfgrqPbaqOenapurf` | OptimizeNestedCondBranches |
| `PbairegIGTErnqJevgr` | ConvertVTGReadWrite |
| `QbIveghnyPGNRkcnafvba` | DoVirtualCTAExpansion |
| `ZnexNqqvgvbanyPbyqOybpxf` | MarkAdditionalColdBlocks |
| `RkcnaqZoneevre` | ExpandMbarrier |
| `SbejneqCebterff` | ForwardProgress |
| `BcgvzvmrHavsbezNgbzvp` | OptimizeUniformAtomic |
| `ZvqRkcnafvba` | MidExpansion |
| `TrarenyBcgvzvmrZvq2` | GeneralOptimizeMid2 |

### Late Optimization

| ROT13 | Decoded |
|--------|---------|
| `NqinaprqCunfrRneylRasbeprNetf` | AdvancedPhaseEarlyEnforceArgs |
| `RasbeprNethzragErfgevpgvbaf` | EnforceArgumentRestrictions |
| `TiaPfr` | GvnCse (?) |
| `BevErnffbpvngrNaqPbzzba` | OriReassociateAndCommon |
| `RkgenpgFunqrePbafgfSvany` | ExtractShaderConstsFinal |
| `BevErcynprRdhviZhygvQrsZbi` | OriReplaceEquivMultiDefMov |
| `BevCebcntngrInelvatSvefg` | OriPropagateVaryingFirst |
| `BevQbErzngRneyl` | OriDoRematEarly |
| `YngrRkcnafvba` | LateExpansion |
| `FcrphyngvirUbvfgPbzVafgf` | SpeculativeHoistComInsts |
| `ErzbirNFGGbQrsnhygInyhrf` | RemoveASTToDefaultValues |
| `TrarenyBcgvzvmrYngr` | GeneralOptimizeLate |
| `BevYbbcShfvba` | OriLoopFusion |
| `QbIGTZhygvIvrjRkcnafvba` | DoVTGMultiViewExpansion |
| `BevCresbezYvirQrnqGuveq` | OriPerformLiveDeadThird |
| `BevErzbirErqhaqnagZhygvQrsZbi` | OriRemoveRedundantMultiDefMov |
| `BevQbCerqvpngvba` | OriDoPredication |
| `YngrBevPbzzbavat` | LateOriCommoning |
| `TrarenyBcgvzvmrYngr2` | GeneralOptimizeLate2 |
| `BevUbvfgVainevnagfYngr` | OriHoistInvariantsLate |
| `QbXvyyZbirzrag` | DoKillMovement |
| `QbGrkZbirzrag` | DoTexMovement |
| `BevQbErzng` | OriDoRemat |
| `BevCebcntngrInelvatFrpbaq` | OriPropagateVaryingSecond |
| `BcgvzvmrFlapVafgehpgvbaf` | OptimizeSyncInstructions |

### Register Allocation & Scheduling

| ROT13 | Decoded |
|--------|---------|
| `PbairegNyyZbiCuvGbZbi` | ConvertAllMovPhiToMov |
| `PbairegGbHavsbezErt` | ConvertToUniformReg |
| `YngrNepuBcgvzvmrSvefg` | LateArchOptimizeFirst |
| `HcqngrNsgreBcgvzvmr` | UpdateAfterOptimize |
| `NqinaprqCunfrYngrPbaiHaFhc` | AdvancedPhaseLatConvUnSup |
| `BevUbvfgVainevnagfYngr2` | OriHoistInvariantsLate2 |
| `RkcnaqWzkPbzchgngvba` | ExpandJmxComputation |
| `YngrNepuBcgvzvmrFrpbaq` | LateArchOptimizeSecond |
| `NqinaprqCunfrOnpxCebcIErt` | AdvancedPhaseBackPropVReg |
| `BevOnpxPbclCebcntngr` | OriBackCopyPropagate |
| `BevCresbezYvirQrnqSbhegu` | OriPerformLiveDeadFourth |
| `BevCebcntngrTzzn` | OriPropagateGmma |
| `SvkhcTzznFrdhrapr` | FixupGmmaSequence |
| `BevUbvfgVainevnagfYngr3` | OriHoistInvariantsLate3 |
| `NqinaprqCunfrFrgErtNgge` | AdvancedPhaseSetRegAttr |
| `BevFrgErtvfgreNgge` | OriSetRegisterAttr |
| `BevPnypQrcraqnagGrk` | OriCalcDependantTex |
| `NqinaprqCunfrNsgreFrgErtNgge` | AdvancedPhaseAfterSetRegAttr |
| `SvanyVafcrpgvbaCnff` | FinalInspectionPass |
| `FrgNsgreYrtnyvmngvba` | SetAfterLegalization |
| `ErcbegOrsberFpurqhyvat` | ReportBeforeScheduling |
| `NqinaprqCunfrCerFpurq` | AdvancedPhasePreSched |
| `OnpxCebcntngrIRP2Q` | BackPropagateVEC2D |
| `BevQbFlapebavmngvba` | OriDoSyncronization |
| `NccylCbfgFlapebavmngvbaJnef` | ApplyPostSyncronizationWars |
| `NqinaprqCunfrNyybpErt` | AdvancedPhaseAllocReg |
| `ErcbegNsgreErtvfgreNyybpngvba` | ReportAfterRegisterAllocation |
| `Trg64oErtPbzcbaragf` | Get64bRegComponents |
| `NqinaprqCunfrCbfgRkcnafvba` | AdvancedPhasePostExpansion |
| `NccylCbfgErtNyybpJnef` | ApplyPostRegAllocWars |
| `NqinaprqCunfrCbfgFpurq` | AdvancedPhasePostSched |
| `BevErzbirAbcPbqr` | OriRemoveNopCode |
| `BcgvzvmrUbgPbyqVaYbbc` | OptimizeHotColdInLoop |
| `BcgvzvmrUbgPbyqSybj` | OptimizeHotColdFlow |

### Post-scheduling & Mercury Pipeline

| ROT13 | Decoded |
|--------|---------|
| `CbfgFpurqhyr` | PostSchedule |
| `NqinaprqCunfrCbfgSvkHc` | AdvancedPhasePostFixUp |
| `CynprOybpxfVaFbheprBeqre` | PlaceBlocksInSourceOrder |
| `CbfgSvkSbeZrepGnetrgf` | PostFixForMercTargets |
| `SvkHcGrkQrcOneNaqFlap` | FixUpTexDepBarAndSync |
| `NqinaprqFpberobneqfNaqBcrkrf` | AdvancedScoreboardsAndOpexes |
| `CebprffB0JnvgfNaqFOf` | ProcessO0WaitsAndSBs |
| `ZrepRapbqrNaqQrpbqr` | MercEncodeAndDecode |
| `ZrepRkcnaqVafgehpgvbaf` | MercExpandInstructions |
| `ZrepTrarengrJNEf1` | MercGenerateWARs1 |
| `ZrepTrarengrBcrk` | MercGenerateOpex |
| `ZrepTrarengrJNEf2` | MercGenerateWARs2 |
| `ZrepTrarengrFnffHPbqr` | MercGenerateSassUCode |

### Final Passes

| ROT13 | Decoded |
|--------|---------|
| `PbzchgrIPnyyErtHfr` | ComputeVCallRegUse |
| `PnypErtvfgreZnc` | CalcRegisterMap |
| `HcqngrNsgreCbfgErtNyybp` | UpdateAfterPostRegAlloc |
| `ErcbegSvanyZrzbelHfntr` | ReportFinalMemoryUsage |
| `NqinaprqCunfrBevCunfrRapbqvat` | AdvancedPhaseOriPhaseEncoding |
| `HcqngrNsgreSbezngPbqrYvfg` | UpdateAfterFormatCodeList |
| `QhzcAIhPbqrGrkg` | DumpNVuCodeText |
| `QhzcAIhPbqrUrk` | DumpNVuCodeHex |
| `QrohttreOernx` | DebuggerBreak |

## ROT13-Encoded ELF Section Names

Four families of ELF section names are stored ROT13-encoded in the binary. These annotate SASS-level metadata for memory ordering and data format constraints.

| ROT13 in Binary | Decoded Name | Description |
|----------------|-------------|---------|
| `.flap_erfgevpg::funerq::ernq::zzn::n` | `.sync_restrict::shared::read::mma::a` | Memory sync restriction for shared MMA reads |
| `.npp::s16` | `.acc::f16` | Accumulator section for FP16 data |
| `.fc::2gb4` | `.sp::2to4` | Sparsity annotation for 2:4 structured sparsity |
| `.eryrnfr::beqrerq` | `.release::ordered` | Memory ordering (release-ordered) |

Additionally, 22 Mercury debug section names are ROT13-encoded:

| ROT13 | Decoded |
|--------|---------|
| `.ai.zrep` | `.nv.merc` |
| `.ai.zrep.qroht_vasb` | `.nv.merc.debug_info` |
| `.ai.zrep.qroht_yvar` | `.nv.merc.debug_line` |
| `.ai.zrep.qroht_nooeri` | `.nv.merc.debug_abbrev` |
| `.ai.zrep.qroht_nenatrf` | `.nv.merc.debug_aranges` |
| `.ai.zrep.qroht_senzr` | `.nv.merc.debug_frame` |
| `.ai.zrep.qroht_ybp` | `.nv.merc.debug_loc` |
| `.ai.zrep.qroht_znpvasb` | `.nv.merc.debug_macinfo` |
| `.ai.zrep.qroht_choanzrf` | `.nv.merc.debug_pubnames` |
| `.ai.zrep.qroht_choglcrf` | `.nv.merc.debug_pubtypes` |
| `.ai.zrep.qroht_enatrf` | `.nv.merc.debug_ranges` |
| `.ai.zrep.qroht_fge` | `.nv.merc.debug_str` |
| `.ai.zrep.ai_qroht_cgk_gkg` | `.nv.merc.nv_debug_ptx_txt` |
| `.ai.zrep.ai_qroht_yvar_fnff` | `.nv.merc.nv_debug_line_sass` |
| `.ai.zrep.ai_qroht_vasb_ert_fnff` | `.nv.merc.nv_debug_info_reg_sass` |
| `.ai.zrep.ai_qroht_vasb_ert_glcr` | `.nv.merc.nv_debug_info_reg_type` |
| `.ai.zrep.flzgno_fuaqk` | `.nv.merc.symtab_shndx` |
| `.ai.zrep.eryn` | `.nv.merc.rela` |
| `.ai.zrep.ai.funerq.erfreirq.` | `.nv.merc.nv.shared.reserved.` |
| `.ragel_vzntr_urnqre_vaqvprf` | `.entry_image_header_indices` |
| `.ai.erfreirqFzrz` | `.nv.reservedSmem` |

## Selected Knob/Option Names

Over 1,287 ROT13-encoded configuration knob names control the compiler's behavior. Listed here organized by subsystem with selected highlights:

### Register Allocation Knobs

| ROT13 | Decoded |
|--------|---------|
| `ErtNyybpHfreFzrzOlgrfCrePGN` | RegAllocUserSmemBytesPerCTA |
| `ErtNyybpGuerfubyqSbeQvfpneqPbasyvpgf` | RegAllocThresholdForDiscardConflicts |
| `ErtNyybpFcvyyKOybpx2` | RegAllocSpillXBlock2 |
| `ranoyr_fzrz_fcvyyvat` | enable_smem_spilling |

### Scheduling Knobs

| ROT13 | Decoded |
|--------|---------|
| `FpurqFlapfCunfrpuxYngrapl` | SchedSyncsPhasechkLatency |
| `FpurqRfgvzngrqYbbcVgrengvbaf` | SchedEstimatedLoopIterations |
| `FpurqYQFYngrapl` | SchedLDSLatency |
| `FpurqYQTOngpuQrynlOvnf` | SchedLDGBatchDelayBias |
| `FpurqPebffOybpxVafgfGbFcrphyngr` | SchedCrossBlockInstsToSpeculate |
| `FpurqErfOhflZnpuvarBcpbqr` | SchedResBusyMachineOpcode |

### Code Sinking Knobs

| ROT13 | Decoded |
|--------|---------|
| `FvaxGrkErnqVafgEngvb` | SinkTexReadInstRatio |
| `FvaxGrkZnkErtGnetrgFpnyr` | SinkTexMaxRegTargetScale |
| `FvaxGrkVafgfGbVPnpurEngvb` | SinkTexInstsToICacheRatio |
| `FvaxErzngRanoyr` | SinkRematEnable |
| `FvaxErzngOhqtrg` | SinkRematBudget |
| `FvaxPbqrVagbFcyvgOybpx` | SinkCodeIntoSplitBlock |

### Loop Optimization Knobs

| ROT13 | Decoded |
|--------|---------|
| `HaebyyFznyyYbbcYvzvg` | UnrollSmallLoopLimit |
| `HaebyyZhygvOybpxYbbcf` | UnrollMultiBlockLoops |
| `HaebyyVafgYvzvg` | UnrollInstLimit |
| `HaebyyShyyVafgYvzvg` | UnrollFullInstLimit |
| `HaebyyHaxabjaVafgYvzvg` | UnrollUnknownInstLimit |
| `FgntrNaqSraprZnkYbbcf` | StageAndFenceMaxLoops |

### Texture/Speculation Knobs

| ROT13 | Decoded |
|--------|---------|
| `FcrphyngvirylUbvfgGrkZnkVafgf` | SpeculativelyHoistTexMaxInsts |
| `FcrphyngvirylUbvfgGrkZnkAhzGrkVafgfVaFbhepr` | SpeculativelyHoistTexMaxNumTexInstsInSource |
| `FcrphyngvirylUbvfgGrkZnkAhzGrkVafgfVaGnetrg` | SpeculativelyHoistTexMaxNumTexInstsInTarget |
| `FcrphyngvirylUbvfgGrkZnkAhzGrkVafgfVaOngpu` | SpeculativelyHoistTexMaxNumTexInstsInBatch |
| `GrkGbVafgEngvb` | TexToInstRatio |

### AdvancedSB (Scoreboard) Knobs

| ROT13 | Decoded |
|--------|---------|
| `NqinaprqFOPebffOybpx` | AdvancedSBCrossBlock |
| `NqinaprqFOPebffOybpxOhqtrg` | AdvancedSBCrossBlockBudget |
| `NqinaprqFOQrconeOnpxrqtr` | AdvancedSBDepbarBackedge |
| `NqinaprqFOQrconeQvfgnaprVaGvzr` | AdvancedSBDepbarDistanceInTime |
| `NqinaprqFOErfreirq1` | AdvancedSBReserved1 |
| `NqinaprqFOErfreirqUZZN` | AdvancedSBReservedHMMA |
| `NqinaprqFOFgnyyYvzvg` | AdvancedSBStallLimit |
| `NqinaprqFOHfrYbbcUrnqreUrhevfgvp` | AdvancedSBUseLoopHeaderHeuristic |

### Disable Flags (64 identified)

Selected flags that disable specific optimizations:

| ROT13 | Decoded |
|--------|---------|
| `QvfnoyrQrnqYbbcRyvzvangvba` | DisableDeadLoopElimination |
| `QvfnoyrQrnqFgberRyvzvangvba` | DisableDeadStoreElimination |
| `QvfnoyrRneylRkgenpgOPB` | DisableEarlyExtractBCO |
| `QvfnoyrReeoneNsgreZrzone` | DisableErrbarAfterMembar |
| `QvfnoyrUZZNErtNyybpJne` | DisableHMMARegAllocWar |
| `QvfnoyrSnfgirpRaunaprzrag` | DisableFastvecEnhancement |
| `QvfnoyrSbejneqCebterffJne1842954` | DisableForwardProgressWar1842954 |

## Cross-References

- [Mercury Overview](../mercury/overview.md) -- Mercury architecture and the ZREPHEL codename
- [Mercury Compiler Passes](../mercury/compiler-passes.md) -- detailed analysis of the 22 Mercury passes
- [Scheduling](../ptxas/scheduling.md) -- tepid scheduler and scoreboard management
- [Register Allocation](../ptxas/register-allocation.md) -- RegAlloc knobs and spilling
- [Peephole](../ptxas/peephole.md) -- Ori* pass family and optimization passes
- [SM100 Blackwell](../targets/sm100-blackwell.md) -- SM100+ architecture features
- [Pipeline Overview](../ptxas/overview.md) -- master phase table and compilation pipeline
