# ROT13-Encoded Names (Mercury Obfuscation)

NVIDIA uses ROT13 as a lightweight obfuscation layer for a specific subset of internal identifiers in nvlink v13.0.88. The encoding is concentrated on Mercury-related content (SM100+ Blackwell codename `MERCURY` -> `ZREPHEL`), SASS instruction mnemonics held inside per-architecture opcode tables, configuration knob/option names, and a small set of ELF section-name suffixes used to annotate SASS-level metadata. The decoder function at `sub_1A40AC0` is SIMD-vectorized, processing 16 bytes at a time via SSE `_mm_load_si128` intrinsics, and is invoked at table-initialization time to convert an encoded string-pool entry into its plaintext form in a fresh heap allocation. This page catalogs every confirmed category of ROT13-encoded content with spot-verified decode tables keyed by string-pool address.

> **Correction notice (P050c-3 / P083).** A prior version of this page claimed the 151 compiler pass names in the master phase table at `0x2443000`--`0x2445000` were ROT13-encoded, using `BevYbbcHaebyyvat` -> `OriLoopUnrolling` as an example. Direct verification against `nvlink_strings.json` shows that **these pass names are stored as plaintext, not ROT13**. The token `BevYbbcHaebyyvat` does not exist anywhere in the binary; only the plaintext `OriLoopUnrolling` is present, and it lives at `0x24434b0` — inside the exact address range that had been labelled "ROT13 phase table". The pipeline order and pass list itself remain accurate, but the encoding-format claim was wrong and has been removed. See the [Compiler Pass Names (Plaintext, Not ROT13)](#compiler-pass-names-plaintext-not-rot13) section below for the corrected treatment.

## Purpose of ROT13 in the nvlink Binary

ROT13 is not a cryptographic cipher — it is a self-inverse Caesar shift that any reader can undo by hand. Its role here is to prevent casual `strings(1)` dumps from trivially revealing Mercury internals: grepping a raw nvlink binary for `MERCURY` or `WGMMA` returns nothing, but grepping for `ZREPHEL` or `JTZZN` reveals the encoded form. The obfuscation is applied to exactly those identifiers NVIDIA wanted to keep out of trivial keyword searches — most prominently Mercury (Blackwell) builtin templates, Mercury compiler-pass option names, SASS opcode mnemonics inside packed per-arch tables, and knob/option strings. Truly public-facing identifiers (R_CUDA relocation names, EIATTR constants, elfLink error messages, compiler pass diagnostic names in `"After <PassName>"` output) are stored as plaintext because they appear in linker error output, nvdisasm disassembly headers, or documented tool interfaces.

## Overview

| Category | Encoded? | Count | Address Region | Prefix/Example |
|----------|----------|------:|----------------|----------------|
| Mercury builtin templates (`ZREPHEL_*`) | ROT13 | 644 | scattered in string pool | `ZREPHEL_` (= `MERCURY_`) |
| Mercury compiler passes (via `ctor_007`) | ROT13 | 22 | `0x23F2CB0`+ (string-pool), registered at `0x425A40`--`0x426080` | `Zrephel...` (= `Mercury...`) |
| SASS opcode mnemonics (qualified forms) | ROT13 | ~320+ | per-arch opcode tables | `VZNQ.JVQR` (= `IMAD.WIDE`), `JTZZN` (= `WGMMA`) |
| Configuration knob/option names | ROT13 | ~1,287 | `0x23F0000`--`0x2460000` | `ErtNyybpFcvyyKOybpx2` (= `RegAllocSpillXBlock2`) |
| ELF section-name annotations | ROT13 | 4 confirmed meaningful | `0x2272970`--`0x22729bd` | `.npp::s16` (= `.acc::f16`), `.fc::2gb4` (= `.sp::2to4`) |
| Compiler pass names (master phase table) | **plaintext** | 151 | `0x24433f7`--`0x2443dc1` | `OriLoopUnrolling`, `GeneralOptimizeEarly`, `MercEncodeAndDecode` |
| EIATTR / EICOMPAT constants | plaintext | 111 | scattered | `EIATTR_*`, `EICOMPAT_*` |
| R_CUDA / R_MERCURY relocation names | plaintext | 186 | scattered | `R_CUDA_*`, `R_MERCURY_*` |
| elfLink error messages | plaintext | 14 | `0x1D489E0` lookup table | `"elfLink: unexpected error"` etc. |

The total ROT13-encoded count is approximately 30,000+ entries, dominated by the 644 `ZREPHEL_*` templates, ~1,287 knob names, and the SASS opcode tables. Bulk counts for individual subsets are verified in the Confidence Assessment section at the end of this page; only the 644 `ZREPHEL_*` figure and the 22 Mercury passes are exhaustively counted — other subsets are plausible estimates.

## ROT13 Decoder Function

**Address**: `sub_1A40AC0` (1,859 bytes)

The decoder implements a classic ROT13 substitution cipher with SIMD acceleration:

1. **Scalar preamble** — processes unaligned head bytes one at a time: `A-M` maps to `N-Z` (+13), `N-Z` maps to `A-M` (-13), same for lowercase
2. **SIMD loop** — loads 16 bytes via `_mm_load_si128`, applies vectorized ROT13 using packed byte comparisons and conditional adds/subtracts
3. **Scalar epilogue** — handles remaining tail bytes after the last aligned 16-byte boundary

The input is copied to a fresh heap allocation (capacity rounded to next power of 2), decoded in-place, then returned. All SASS opcode mnemonic lookups flow through this function during table initialization.

## SASS Opcode Mnemonics

Three per-architecture opcode table constructors populate ROT13-encoded instruction mnemonic tables. Each entry is a `(name_ptr, name_length)` pair starting at offset +4184 within the table object.

| Constructor | Address | Architecture | Entries | Size |
|-------------|---------|-------------|--------:|-----:|
| `sm70_opcode_table_constructor` | `sub_1769B50` | SM70/SM75 (Volta/Turing) | ~130 | 7,639 bytes |
| `sm100_opcode_table_constructor` | `sub_1782540` | SM100 (Blackwell) | ~400 | 24,243 bytes |
| `sm120_opcode_table_constructor` | `sub_1848F70` | SM120 (RTX 50xx) | ~400+ | 21,368 bytes |
| `sass_opcode_table_initializer` | `sub_1A85E40` | Emission pass table | ~320 | 7,548 bytes |

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

All 22 Mercury-specific passes are registered in `ctor_007` at addresses `0x425A40`--`0x426080`. Each is a boolean enable/disable flag stored at a bit offset within the global options structure. The prefix `ZREPHEL` decodes to `MERCURY` — the Blackwell (SM100+) codename.

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

## Compiler Pass Names (Plaintext, Not ROT13)

The master phase table at `0x2443000`--`0x2445000` contains **151 plaintext pass name strings** defining the full compilation pipeline order, cross-referenced from an adjacent pointer table starting around `0x2443ff0`. These are the same names emitted in `"After <PassName>"` diagnostic output. They are stored **as plaintext, without any ROT13 encoding** — a prior version of this page incorrectly listed them as ROT13.

**Evidence of plaintext storage** (spot-checks from `nvlink_strings.json`, all type=0 = ASCII string pool entry):

| String-pool addr | Plaintext value | Pointer-table xref |
|------------------|-----------------|--------------------|
| `0x24433f7` | `OriSanitize` | `0x2443fe8` |
| `0x2443403` | `GeneralOptimizeEarly` | `0x2443ff0` |
| `0x2443418` | `DoSwitchOptFirst` | `0x2444000` |
| `0x2443429` | `OriBranchOpt` | `0x2444008` |
| `0x2443436` | `OriPerformLiveDeadFirst` | `0x2444010` |
| `0x244344e` | `OptimizeBindlessHeaderLoads` | `0x2444018` |
| `0x244346a` | `OriLoopSimplification` | — |
| `0x2443480` | `OriSplitLiveRanges` | — |
| `0x24434b0` | `OriLoopUnrolling` | `0x2444048` |
| `0x24434c1` | `GenerateMovPhi` | `0x2444050` |
| `0x24439ba` | `OriPerformLiveDeadFourth` | — |
| `0x2443c07` | `PostSchedule` | — |
| `0x2443c2b` | `PlaceBlocksInSourceOrder` | — |
| `0x2443ca2` | `MercEncodeAndDecode` | — |
| `0x2443d02` | `MercGenerateSassUCode` | — |
| `0x2443dc1` | `DebuggerBreak` | — |

The string-pool region for these pass names spans `0x24433f7` (`OriSanitize`, first entry) to `0x2443dc1` (`DebuggerBreak`, last entry). The previous wiki version listed `BevYbbcHaebyyvat` as the ROT13 form of `OriLoopUnrolling`; direct `grep` against `nvlink_strings.json` returns zero matches for that encoded token — it does not exist anywhere in the binary. Only the plaintext `OriLoopUnrolling` (decoder-output form) is present, and it sits at `0x24434b0` — inside the exact address range the old "ROT13 phase table" claim named. The root cause of the original error was noticing the address range and assuming the uniform CamelCase naming scheme implied ROT13, without verifying that the stored bytes were actually encoded.

**Why plaintext and not ROT13?** These names are emitted verbatim into `"After <PassName>"` diagnostic messages and `--verbose`/timing output. Obfuscating them would require either a decode step on every diagnostic print (wasteful) or a dedicated plaintext mirror table (redundant). Mercury-specific *options* (the 22 `ctor_007`-registered booleans with the `Zrephel...` prefix) are ROT13-encoded because their *names* are only consulted at option-parsing time, but the master phase-table names are consulted on every timing dump and diagnostic emission.

The 151 plaintext pass names in pipeline order (grouped by phase) are catalogued in [ptxas/peephole.md](../ptxas/peephole.md) (for the `Ori*` family) and [ptxas/overview.md](../ptxas/overview.md) (for the full master phase table). Only the encoding classification is corrected here; the pipeline order, pass count, and pass-name list themselves remain accurate in those pages.

## ROT13-Encoded ELF Section Name Annotations

Four ELF section-name suffix annotations are stored ROT13-encoded in the binary as isolated string-pool entries. These annotate SASS-level metadata for memory ordering and data format constraints. All four are verbatim confirmed in `nvlink_strings.json`:

| Addr | ROT13 in Binary | Decoded Name | Description |
|------|-----------------|--------------|-------------|
| `0x2272970` | `.flap_erfgevpg::funerq::ernq::zzn::n` | `.sync_restrict::shared::read::mma::a` | Memory sync restriction for shared MMA reads |
| `0x2272995` | `.npp::s16` | `.acc::f16` | Accumulator section for FP16 data |
| `0x22729ad` | `.fc::2gb4` | `.sp::2to4` | Sparsity annotation for 2:4 structured sparsity |
| `0x22729bd` | `.eryrnfr::beqrerq` | `.release::ordered` | Memory ordering (release-ordered) |

### Mercury debug section name prefix (`.nv.merc` -> `.ai.zrep`)

A related but partially runtime-assembled family of 22 Mercury debug section names uses the ROT13 prefix `.ai.zrep` (= `.nv.merc`) plus a DWARF section suffix. Only one representative fragment — `.ai.erfreirqFzrz.bssfrg` at `0x1f245d7` (= `.nv.reservedSmem.offset`) — exists as an isolated string-pool entry. The 22 individual debug section names (`.nv.merc.debug_info`, `.nv.merc.debug_line`, etc.) are **not** present as standalone strings in the string pool; they are built at runtime via concatenation of the ROT13 prefix with a DWARF suffix and ROT13-decoded on demand.

The mapping for reference (prefix is stored and decoded at runtime):

| Decoded runtime name | Composition (prefix + suffix) |
|----------------------|-------------------------------|
| `.nv.merc` | `.ai.zrep` (ROT13 prefix, stored) |
| `.nv.merc.debug_info` | `.ai.zrep` + `.qroht_vasb` |
| `.nv.merc.debug_line` | `.ai.zrep` + `.qroht_yvar` |
| `.nv.merc.debug_abbrev` | `.ai.zrep` + `.qroht_nooeri` |
| `.nv.merc.debug_aranges` | `.ai.zrep` + `.qroht_nenatrf` |
| `.nv.merc.debug_frame` | `.ai.zrep` + `.qroht_senzr` |
| `.nv.merc.debug_loc` | `.ai.zrep` + `.qroht_ybp` |
| `.nv.merc.debug_macinfo` | `.ai.zrep` + `.qroht_znpvasb` |
| `.nv.merc.debug_pubnames` | `.ai.zrep` + `.qroht_choanzrf` |
| `.nv.merc.debug_pubtypes` | `.ai.zrep` + `.qroht_choglcrf` |
| `.nv.merc.debug_ranges` | `.ai.zrep` + `.qroht_enatrf` |
| `.nv.merc.debug_str` | `.ai.zrep` + `.qroht_fge` |
| `.nv.merc.nv_debug_ptx_txt` | `.ai.zrep` + `.ai_qroht_cgk_gkg` |
| `.nv.merc.nv_debug_line_sass` | `.ai.zrep` + `.ai_qroht_yvar_fnff` |
| `.nv.merc.nv_debug_info_reg_sass` | `.ai.zrep` + `.ai_qroht_vasb_ert_fnff` |
| `.nv.merc.nv_debug_info_reg_type` | `.ai.zrep` + `.ai_qroht_vasb_ert_glcr` |
| `.nv.merc.symtab_shndx` | `.ai.zrep` + `.flzgno_fuaqk` |
| `.nv.merc.rela` | `.ai.zrep` + `.eryn` |
| `.nv.merc.nv.shared.reserved.` | `.ai.zrep` + `.ai.funerq.erfreirq.` |
| `.nv.reservedSmem` | `.ai.erfreirqFzrz` (fragment confirmed at `0x1f245d7`) |
| `.entry_image_header_indices` | `.ragel_vzntr_urnqre_vaqvprf` (runtime-assembled) |

Confidence for this runtime-assembled family is LOW — the ROT13 mapping is mechanically correct (trivially inverse-checkable), but the individual encoded forms are not verifiable via string-pool dumps alone and require decompiler confirmation of the concatenation site.

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

- [Mercury Overview](../mercury/overview.md) — Mercury architecture and the ZREPHEL codename
- [Mercury Compiler Passes](../mercury/compiler-passes.md) — detailed analysis of the 22 Mercury passes
- [Scheduling](../ptxas/scheduling.md) — tepid scheduler and scoreboard management
- [Register Allocation](../ptxas/register-allocation.md) — RegAlloc knobs and spilling
- [Peephole](../ptxas/peephole.md) — Ori* pass family and optimization passes
- [SM100 Blackwell](../targets/sm100-blackwell.md) — SM100+ architecture features
- [Pipeline Overview](../ptxas/overview.md) — master phase table and compilation pipeline

## Confidence Assessment

ROT13 is a self-inverse substitution cipher (A<->N, B<->O, ..., M<->Z). Mapping correctness for any given (encoded, decoded) pair is mechanically checkable, so HIGH confidence requires both: (1) the encoded form found verbatim in `nvlink_strings.json`, and (2) the listed plaintext being the correct ROT13 inverse. This page was revised under P083 to remove an incorrect claim that the 151 compiler pass names at `0x2443000`--`0x2445000` were ROT13-encoded; they are stored as plaintext.

**Spot-check verification (14 entries from `nvlink_strings.json`):**

| ROT13 Encoded | Decoded | Addr in strings JSON | ROT13 Correct? | Confidence |
|---|---|---|---|---|
| `JTZZN` | `WGMMA` | `0x1f24874` (line 59353) | yes | HIGH |
| `ZrephelTraFnffHPbqr` | `MercuryGenSassUCode` | `0x23f2cb0` (line 240957) | yes | HIGH |
| `ZrephelNffhzrCGKCbegnovyvgl` | `MercuryAssumePTXPortability` | `0x23f2e80` (line 241221) | yes | HIGH |
| `ZrephelRapbqrQrpbqr` | `MercuryEncodeDecode` | line 241053 | yes | HIGH |
| `ErtNyybpFcvyyKOybpx2` | `RegAllocSpillXBlock2` | `0x23f5c30` (line 248301) | yes | HIGH |
| `QvfnoyrQrnqYbbcRyvzvangvba` | `DisableDeadLoopElimination` | `0x23fa950` (line 260073) | yes | HIGH |
| `NqinaprqFOPebffOybpx` | `AdvancedSBCrossBlock` | line 264597 | yes | HIGH |
| `HGPONE_1PGN` | `UTCBAR_1CTA` | line 265867 | yes | HIGH |
| `VZNQ_JVQR` (qualified) | `IMAD_WIDE` | line 269277 | yes | HIGH |
| `.flap_erfgevpg::funerq::ernq::zzn::n` | `.sync_restrict::shared::read::mma::a` | `0x2272970` (line 232924) | yes | HIGH |
| `.npp::s16` | `.acc::f16` | `0x2272995` (line 232956) | yes | HIGH |
| `.fc::2gb4` | `.sp::2to4` | `0x22729ad` (line 232988) | yes | HIGH |
| `.eryrnfr::beqrerq` | `.release::ordered` | `0x22729bd` (line 233052) | yes | HIGH |
| `.ai.erfreirqFzrz.bssfrg` | `.nv.reservedSmem.offset` | `0x1f245d7` (line 58921) | yes | HIGH |

All 14 ROT13 spot-checks verified at HIGH confidence. The former `BevYbbcHaebyyvat` / `OriLoopUnrolling` entry has been removed from the spot-check table; the token does not exist in the binary (see the Compiler Pass Names section for the plaintext treatment at `0x24434b0`).

**Plaintext verification (strings previously claimed to be ROT13):**

| Plaintext String | Addr in strings JSON | Claim in old wiki | Reality |
|---|---|---|---|
| `OriSanitize` | `0x24433f7` | ROT13 `BevFnavgvmr` | Plaintext only; encoded form absent |
| `GeneralOptimizeEarly` | `0x2443403` | ROT13 `TrarenyBcgvzvmrRneyl` | Plaintext only; encoded form absent |
| `DoSwitchOptFirst` | `0x2443418` | ROT13 `QbFjvgpuBcgSvefg` | Plaintext only; encoded form absent |
| `OriBranchOpt` | `0x2443429` | ROT13 `BevOenapuBcg` | Plaintext only; encoded form absent |
| `OriPerformLiveDeadFirst` | `0x2443436` | ROT13 `BevCresbezYvirQrnqSvefg` | Plaintext only; encoded form absent |
| `OptimizeBindlessHeaderLoads` | `0x244344e` | ROT13 `BcgvzvmrOvaqyrffUrnqreYbnqf` | Plaintext only; encoded form absent |
| `OriLoopUnrolling` | `0x24434b0` | ROT13 `BevYbbcHaebyyvat` | Plaintext only; encoded form absent |
| `OriPerformLiveDeadFourth` | `0x24439ba` | ROT13 `BevCresbezYvirQrnqSbhegu` | Plaintext only; encoded form absent |
| `PostSchedule` | `0x2443c07` | ROT13 `CbfgFpurqhyr` | Plaintext only; encoded form absent |
| `PlaceBlocksInSourceOrder` | `0x2443c2b` | ROT13 `CynprOybpxfVaFbheprBeqre` | Plaintext only; encoded form absent |
| `MercEncodeAndDecode` | `0x2443ca2` | ROT13 `ZrepRapbqrNaqQrpbqr` | Plaintext only; encoded form absent |
| `MercGenerateSassUCode` | `0x2443d02` | ROT13 `ZrepTrarengrFnffHPbqr` | Plaintext only; encoded form absent |
| `DebuggerBreak` | `0x2443dc1` | ROT13 `QrohttreOernx` | Plaintext only; encoded form absent |

Every spot-checked pass name inside `0x24433f7`--`0x2443dc1` is present in `nvlink_strings.json` as plaintext, and none of the previously-claimed ROT13 forms exist anywhere in the binary. The encoding-format claim for this region is definitively refuted.

**Bulk verification:**

| Aspect | Confidence | Basis |
|---|---|---|
| ROT13 cipher mapping (A<->N etc.) | HIGH | Self-inverse alphabet substitution; trivially checkable for any pair; the prefixes `ZREPHEL` -> `MERCURY` and `Zrephel` -> `Mercury` confirmed mechanically on every spot-checked entry |
| ROT13 decoder function (`sub_1A40AC0`) | MEDIUM | Function exists at address `0x1a40ac0` (decompiled file present at `decompiled/sub_1A40AC0_0x1a40ac0.c`); the "SIMD-vectorized `_mm_load_si128` 16-byte chunks" claim is plausible given function size but not re-verified in this pass |
| Mercury builtin templates count (644 `ZREPHEL_*`) | HIGH | `grep -c '"ZREPHEL_' nvlink_strings.json` returns exactly 644; matches the wiki claim |
| Mercury pass names (22 entries via `ctor_007`) | HIGH | All 22 use the ROT13 prefix `Zrephel` (= `Mercury`); spot-checked entries (`ZrephelNffhzrCGKCbegnovyvgl`, `ZrephelRapbqrQrpbqr`, `ZrephelTraFnffHPbqr`) all present in strings; `ctor_007` registration address range `0x425A40`--`0x426080` matches bit-offset progression |
| Knob/option names (~1,287 ROT13) | HIGH | Spot-checked knobs (`ErtNyybpFcvyyKOybpx2`, `NqinaprqFOPebffOybpx`, `QvfnoyrQrnqYbbcRyvzvangvba`) all confirmed at stated addresses; aggregate count of ~1,287 not exhaustively recounted |
| SASS opcode mnemonics (qualified forms) | HIGH | Qualified forms `VZNQ.JVQR`, `VZNQ.UV`, `HVZNQ`, `SSZN2`, `SSZN32V`, `JTZZN` all present in strings |
| SASS opcode mnemonics (bare forms) | MEDIUM | Bare `VZNQ` and `SSZN` are NOT present as isolated string-pool entries — only suffix-qualified variants appear as standalone strings. Bare base mnemonics live inside per-arch packed opcode tables (`sub_1769B50` SM70, `sub_1782540` SM100, `sub_1848F70` SM120). ROT13 mapping itself is trivially correct |
| Compiler pass names at `0x2443000`--`0x2445000` | HIGH (plaintext) | All 151 pass names are stored as plaintext, confirmed by 13 spot-check addresses spanning `0x24433f7` (`OriSanitize`) to `0x2443dc1` (`DebuggerBreak`). The prior wiki claim that this region held ROT13 strings is refuted; the correction is made inline above. Pipeline order and pass-name list remain accurate |
| ELF section-name annotations (4 meaningful) | HIGH | `.flap_erfgevpg::funerq::ernq::zzn::n` (`0x2272970`), `.npp::s16` (`0x2272995`), `.fc::2gb4` (`0x22729ad`), `.eryrnfr::beqrerq` (`0x22729bd`) all confirmed in strings at consecutive addresses; ROT13 decodes verified |
| Mercury debug section names (22 `.ai.zrep.*` entries) | LOW | None of the 22 listed `.ai.zrep.qroht_*` ROT13 forms appear as isolated strings in the binary string pool. Only one `.ai.*` ROT13 fragment (`.ai.erfreirqFzrz.bssfrg` = `.nv.reservedSmem.offset`) is present at `0x1f245d7`. The 22 debug section names are constructed at runtime via concatenation (`.ai.zrep` prefix + DWARF section suffix, both ROT13), decoded after concatenation. ROT13 mapping itself is correct |
| Total ROT13-encoded string count (~30,000+) | LOW | Aggregate figure is plausible but not verified by direct counting; only specific subsets (644 `ZREPHEL_*`, 22 Mercury passes, ~20 spot-checked knobs/opcodes/sections) were exactly counted |
| Architecture / opcode counts per arch (SM70 ~130, SM100 ~400, SM120 ~400+) | MEDIUM | Constructor function sizes match the wiki listing; per-arch entry counts not individually re-verified in this pass |

### Revision history

- **P050c-3** — added initial Confidence Assessment; detected and flagged that compiler pass names at `0x2443000`--`0x2445000` are plaintext, not ROT13.
- **P083** — full page rewrite to remove the incorrect "ROT13 compiler phase table" section and replace it with an explicit plaintext catalogue with spot-verified addresses. Purpose section added. Overview table restructured to distinguish ROT13 from plaintext categories. Mercury debug section names relabelled as "runtime-assembled from ROT13 prefix + suffix" to reflect that the 22 individual forms are not present as isolated strings.

