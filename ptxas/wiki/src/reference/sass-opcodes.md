# SASS Opcode Catalog

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

Complete reference table of all SASS opcode mnemonics known to ptxas v13.0.88. Extracted from the ROT13-encoded opcode name table in the `InstructionInfo` constructor (`sub_7A5D10`, vtable `off_233ADC0`). The table stores exactly 322 named entries (indices 0--321) at object offset +0x1058, with each entry occupying 16 bytes (8-byte string pointer + 8-byte length). A parallel constructor `sub_BE7390` initializes an identical table. Immediately after the name table, a 322-element identity-mapped index array (0x508 bytes of 4-byte integers 0..321) is bulk-copied from `unk_21C0E00` to object offset +0x2478; this is a separate data structure (encoding category map), not additional opcode names.

All SASS mnemonic strings in the ptxas binary are ROT13-obfuscated. The cleartext names shown here are the result of applying ROT13 decoding to the stored strings.

## Table Organization

Opcodes are partitioned by SM generation through explicit boundary markers embedded in the table:

| Index | Marker | Range |
|-------|--------|-------|
| 0--135 | Base ISA | sm_70 (Volta) and all later architectures |
| 136 | `SM70_LAST` | End of sm_70 range |
| 137--171 | sm_73+ | Volta extensions (uniform registers, tensor shapes) |
| 171 | `SM73_LAST` | End of sm_73 range |
| 172--193 | sm_82+ | Ampere additions (MMA shapes, gather, REDUX) |
| 193 | `SM82_LAST` | End of sm_82 range |
| 194--199 | sm_86+ | Ampere+ additions (conversion packed, SUQUERY) |
| 199 | `SM86_LAST` | End of sm_86 range |
| 200--205 | sm_89+ | Ada Lovelace additions (QMMA shapes) |
| 205 | `SM89_LAST` | End of sm_89 range |
| 206--252 | sm_90+ | Hopper additions (GMMA, CGA barriers, fences, TMA) |
| 252 | `SM90_LAST` | End of sm_90 range |
| 253--280 | sm_100+ | Blackwell datacenter additions (UTC, QFMA4, MEMSET) |
| 280 | `SM100_LAST` | End of sm_100 range |
| 281--320 | sm_104+ | Blackwell Ultra additions (uniform FP, new conversions) |
| 320 | `SM104_LAST` | End of sm_104 range |
| 321 | `LAST` | Sentinel (end of table) |

Each SM generation only adds opcodes; no base opcodes are removed. The Ori IR uses the 12-bit index into this table as the base opcode field (instruction offset +72, lower 12 bits). Bits 12--13 of the opcode word encode sub-operation modifiers (`.HI`, `.WIDE`, etc.) and are stripped by the `0xFFFFCFFF` mask to recover the base index.

## Encoding Format Summary

SASS instructions use three widths, selected per opcode during encoding:

| Format Code | Width | Usage |
|-------------|-------|-------|
| 0x1 | 64-bit | Simple moves, branches, barriers, NOPs, short-form ALU |
| 0x2 | 128-bit | Most ALU, load/store, texture, tensor core, atomics |
| 0x8 | 256-bit | IMAD.WIDE variants with 16 constant-bank operand slots |

The 3-level opcode hierarchy within the encoded instruction word is: major (9 bits, at bits [8:16]) / minor (8 bits, at bits [17:24]) / sub-opcode (7 bits, at bits [25:31]). See the [encoding page](../codegen/encoding.md) for full details.

## Duplicate Mnemonic Entries

Five entries in the table share a SASS mnemonic with an earlier index. These are **not** errors in the table — they are distinct IR opcodes that happen to produce the same assembly mnemonic but with different binary encodings, operand widths, or functional-unit routing. The duplicates fall into two categories:

**Category A — SM-generation re-introduction.** The same operation is re-implemented for a newer GPU generation with a different SASS major opcode and encoding path, typically because the tensor core or ALU microarchitecture changed:

| Later Index | Earlier Index | Mnemonic | Why re-introduced |
|-------------|---------------|----------|--------------------|
| 215 (sm_90) | 180 (sm_82) | DMMA | Hopper warpgroup-aware TC path (enc. cat. 515 vs 434) |
| 220 (sm_90) | 14 (sm_70) | FMNMX | Hopper adds 5-entry operand sub-mode table (enc. cat. 534 vs 510) |

**Category B — Operand-width extension.** Blackwell Ultra (sm_104) adds 64-bit operand variants of existing integer ALU instructions. The SASS printer appends a `.64` suffix at render time; the IR name table stores the same base mnemonic for both widths:

| Later Index | Earlier Index | Mnemonic | What the later index adds |
|-------------|---------------|----------|---------------------------|
| 284 (sm_104) | 37 (sm_70) | IMNMX | 32-bit form, new encoding path |
| 285 (sm_104) | 37 (sm_70) | IMNMX | 64-bit form (`IMNMX.64`, `.64.UI`, `.64.LO`) |
| 288 (sm_104) | 7 (sm_70) | ISETP | 64-bit comparison (`ISETP.64`, `.64.UI`, `.64.LO`) |

Binary evidence: in the constructor `sub_7A5D10`, indices 284 and 285 store identical `"VZAZK"` string pointers at adjacent 16-byte slots (`v2+8728` and `v2+8744`). The SASS printer (`sub_7CB560`) maps them to `IMNMX` vs `IMNMX.64` based on operand metadata.

## Base ISA — sm_70 (Volta) and Later (Indices 0--135)

These opcodes are available on all SM architectures supported by ptxas v13.0.

### Integer Arithmetic

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 1 | `VZNQ` | **IMAD** | Integer multiply-add (32-bit) |
| 2 | `VZNQ_JVQR` | **IMAD_WIDE** | Integer multiply-add, 32x32->64 result |
| 3 | `VNQQ3` | **IADD3** | Three-input integer add with carry |
| 4 | `OZFX` | **BMSK** | Generate bitmask from position and width |
| 5 | `FTKG` | **SGXT** | Sign-extend from specified bit position |
| 6 | `YBC3` | **LOP3** | Three-input logic operation (arbitrary LUT) |
| 7 | `VFRGC` | **ISETP** | Integer compare and set predicate (32-bit; re-introduced at index 288 for sm_104 with 64-bit support) |
| 8 | `VNOF` | **IABS** | Integer absolute value |
| 9 | `YRN` | **LEA** | Load effective address (shift-add) |
| 10 | `FUS` | **SHF** | Funnel shift (concatenate two regs, shift) |
| 33 | `VQC` | **IDP** | Integer dot product (4-element) |
| 34 | `VQR` | **IDE** | Integer dot expand |
| 37 | `VZAZK` | **IMNMX** | Integer min/max (32-bit only; re-introduced at indices 284--285 for sm_104 with 32/64-bit split) |
| 38 | `CBCP` | **POPC** | Population count (count set bits) |
| 39 | `SYB` | **FLO** | Find leading one (bit scan) |
| 53 | `OERI` | **BREV** | Bit reverse |

### FP32 Arithmetic

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 11 | `SSZN` | **FFMA** | FP32 fused multiply-add |
| 12 | `SNQQ` | **FADD** | FP32 add |
| 13 | `SZHY` | **FMUL** | FP32 multiply |
| 14 | `SZAZK` | **FMNMX** | FP32 min/max (base encoding cat. 510; re-introduced at index 220 for sm_90 with extended operand modes) |
| 15 | `SFJMNQQ` | **FSWZADD** | FP32 swizzle add (cross-lane partial reduction) |
| 16 | `SFRG` | **FSET** | FP32 compare and set result register |
| 17 | `SFRY` | **FSEL** | FP32 select (conditional move) |
| 18 | `SFRGC` | **FSETP** | FP32 compare and set predicate |
| 40 | `SPUX` | **FCHK** | FP check for NaN/Inf/denorm |
| 42 | `ZHSH` | **MUFU** | Multi-function unit: RCP, RSQ, SIN, COS, EX2, LG2, RCP64H, RSQ64H |

### FP64 Arithmetic

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 122 | `QSZN` | **DFMA** | FP64 fused multiply-add |
| 123 | `QNQQ` | **DADD** | FP64 add |
| 124 | `QZHY` | **DMUL** | FP64 multiply |
| 125 | `QFRGC` | **DSETP** | FP64 compare and set predicate |

### FP16 Packed Arithmetic

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 126 | `UNQQ2` | **HADD2** | Packed FP16x2 add |
| 127 | `UNQQ2_S32` | **HADD2_F32** | Packed FP16x2 add with FP32 accumulator |
| 128 | `USZN2` | **HFMA2** | Packed FP16x2 fused multiply-add |
| 129 | `UZHY2` | **HMUL2** | Packed FP16x2 multiply |
| 130 | `UFRG2` | **HSET2** | Packed FP16x2 compare and set |
| 131 | `UFRGC2` | **HSETP2** | Packed FP16x2 compare and set predicate |

### Type Conversion

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 35 | `V2V` | **I2I** | Integer to integer conversion (width/sign change) |
| 36 | `V2VC` | **I2IP** | Integer to integer, packed variant |
| 43 | `S2S` | **F2F** | Float to float conversion (precision change) |
| 44 | `S2S_K` | **F2F_X** | Float to float, extended (with carry chain) |
| 45 | `S2V` | **F2I** | Float to integer |
| 46 | `S2V_K` | **F2I_X** | Float to integer, extended |
| 47 | `V2S` | **I2F** | Integer to float |
| 48 | `V2S_K` | **I2F_X** | Integer to float, extended |
| 49 | `SEAQ` | **FRND** | FP round to integer (within FP format) |
| 50 | `SEAQ_K` | **FRND_X** | FP round, extended |

### Data Movement

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 19 | `ZBI` | **MOV** | Move register to register |
| 20 | `FRY` | **SEL** | Predicated select (ternary conditional) |
| 21 | `C2E` | **P2R** | Pack predicate registers into GPR |
| 22 | `E2C` | **R2P** | Unpack GPR bits into predicate registers |
| 24 | `CEZG` | **PRMT** | Byte-level permute (4-byte shuffle) |
| 41 | `VCN` | **IPA** | Interpolate pixel attribute (fragment shader) |
| 57 | `F2E` | **S2R** | Read special register to GPR |
| 27 | `PF2E_32` | **CS2R_32** | Control/status register to GPR (32-bit) |
| 28 | `PF2E_64` | **CS2R_64** | Control/status register to GPR (64-bit) |

### Predicate Operations

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 23 | `CYBC3` | **PLOP3** | Three-input predicate logic (arbitrary LUT) |
| 26 | `IBGR` | **VOTE** | Warp-wide vote (ballot/any/all/unanimity) |
| 31 | `INOFQVSS` | **VABSDIFF** | Vector absolute difference |
| 32 | `INOFQVSS4` | **VABSDIFF4** | Vector absolute difference, 4-way |

### Memory — Load/Store

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 89 | `YQP` | **LDC** | Load from constant memory bank `c[bank][offset]` |
| 90 | `NYQ` | **ALD** | Attribute load (vertex/fragment attributes) |
| 91 | `NFG` | **AST** | Attribute store |
| 94 | `YQF` | **LDS** | Load from shared memory |
| 95 | `FGF` | **STS** | Store to shared memory |
| 96 | `YQT` | **LDG** | Load from global memory |
| 97 | `FGT` | **STG** | Store to global memory |
| 98 | `YQY` | **LDL** | Load from local memory (per-thread stack) |
| 99 | `FGY` | **STL** | Store to local memory |
| 100 | `YQ` | **LD** | Load, generic address space |
| 101 | `FG` | **ST** | Store, generic address space |

### Atomic and Reduction

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 102 | `NGBZ` | **ATOM** | Atomic operation (generic address space) |
| 103 | `NGBZT` | **ATOMG** | Atomic operation (global memory) |
| 104 | `ERQ` | **RED** | Reduction (global memory, fire-and-forget) |
| 105 | `NGBZF` | **ATOMS** | Atomic operation (shared memory) |

### Cache and Memory Control

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 106 | `DFCP` | **QSPC** | Query address space type |
| 107 | `PPGY_AB_FO` | **CCTL_NO_SB** | Cache control, no scoreboard wait |
| 108 | `PPGY` | **CCTL** | Cache control (invalidate/writeback/etc.) |
| 109 | `PPGYY` | **CCTLL** | Cache control, L2 level |
| 110 | `PPGYG` | **CCTLT** | Cache control, texture cache |
| 111 | `ZRZONE` | **MEMBAR** | Memory barrier (fence) |

### Texture Operations

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 83 | `GRK` | **TEX** | Texture fetch (filtered sample) |
| 84 | `GYQ` | **TLD** | Texture load (unfiltered, integer coords) |
| 85 | `GYQ4` | **TLD4** | Texture gather (fetch 4 texels for bilinear) |
| 86 | `GZZY` | **TMML** | Query texture mip-map level |
| 87 | `GKQ` | **TXD** | Texture fetch with explicit derivatives |
| 88 | `GKD` | **TXQ** | Texture query (dimensions, levels, format) |

### Surface Operations

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 112 | `FHYQ` | **SULD** | Surface load |
| 113 | `FHFG` | **SUST** | Surface store |
| 114 | `FHNGBZ` | **SUATOM** | Surface atomic |
| 115 | `FHERQ` | **SURED** | Surface reduction |

### Graphics Pipeline

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 51 | `NY2C` | **AL2P** | Attribute location to patch offset |
| 52 | `NY2C_VAQRKRQ` | **AL2P_INDEXED** | Attribute to patch, indexed variant |
| 92 | `BHG` | **OUT** | Tessellation output emit |
| 93 | `BHG_SVANY` | **OUT_FINAL** | Tessellation output emit (final, cut primitive) |
| 116 | `CVKYQ` | **PIXLD** | Pixel information load (coverage, sample mask) |
| 117 | `VFOREQ` | **ISBERD** | Indexed set buffer for read (bindless) |
| 118 | `VFORJE` | **ISBEWR** | Indexed set buffer for write (bindless) |

### Control Flow

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 67 | `OEN` | **BRA** | Branch (relative) |
| 68 | `OEK` | **BRX** | Branch indirect (register target) |
| 69 | `WZC` | **JMP** | Jump (absolute) |
| 70 | `WZK` | **JMX** | Jump indirect |
| 71 | `PNYY` | **CALL** | Function call |
| 72 | `ERG` | **RET** | Return from function |
| 73 | `OFFL` | **BSSY** | Push convergence point onto branch sync stack |
| 74 | `OERNX` | **BREAK** | Break out of convergence region |
| 77 | `RKVG` | **EXIT** | Thread exit |
| 76 | `XVYY` | **KILL** | Kill thread (discard fragment) |
| 75 | `OCG` | **BPT** | Breakpoint trap (debugger) |
| 78 | `EGG` | **RTT** | Return to trap handler |
| 79 | `OFLAP` | **BSYNC** | Branch sync (pop convergence stack, reconverge) |

### Synchronization and Warp

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 54 | `OZBI_O` | **BMOV_B** | Barrier move (barrier register, B variant) |
| 55 | `OZBI_E` | **BMOV_R** | Barrier move (barrier register, R variant) |
| 56 | `OZBI` | **BMOV** | Barrier move |
| 58 | `O2E` | **B2R** | Barrier register to GPR |
| 59 | `E2O` | **R2B** | GPR to barrier register |
| 61 | `ONE` | **BAR** | Named barrier synchronization |
| 62 | `ONE_VAQRKRQ` | **BAR_INDEXED** | Barrier, indexed variant |
| 66 | `QRCONE` | **DEPBAR** | Dependency barrier (wait for scoreboard) |
| 80 | `ZNGPU` | **MATCH** | Warp match (find lanes with same value) |
| 119 | `FUSY` | **SHFL** | Warp shuffle (cross-lane data exchange) |
| 120 | `JNECFLAP` | **WARPSYNC** | Warp-wide synchronization barrier |
| 81 | `ANABFYRRC` | **NANOSLEEP** | Thread sleep for specified nanoseconds |
| 82 | `ANABGENC` | **NANOTRAP** | Nano trap (lightweight trap) |

### System and Miscellaneous

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 0 | `REEONE` | **ERRBAR** | Error barrier (internal pseudo-instruction) |
| 25 | `ABC` | **NOP** | No-operation |
| 29 | `CZGEVT` | **PMTRIG** | Performance monitor trigger |
| 30 | `PFZGRFG` | **CSMTEST** | CSM (compute shader model) test |
| 60 | `YRCP` | **LEPC** | Load effective PC (get current instruction address) |
| 63 | `FRGPGNVQ` | **SETCTAID** | Set CTA (thread block) ID |
| 64 | `FRGYZRZONFR` | **SETLMEMBASE** | Set local memory base address |
| 65 | `TRGYZRZONFR` | **GETLMEMBASE** | Get local memory base address |
| 121 | `LVRYQ` | **YIELD** | Yield execution (internal, scheduler hint) |
| 135 | `VAGEVAFVP` | **INTRINSIC** | Compiler intrinsic (pseudo-opcode, lowered before encoding) |

### Tensor Core (Base)

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 132 | `UZZN_16` | **HMMA_16** | FP16 matrix multiply-accumulate, 16-wide |
| 133 | `UZZN_32` | **HMMA_32** | FP16 matrix multiply-accumulate, 32-wide |
| 134 | `VZZN` | **IMMA** | Integer matrix multiply-accumulate |

## sm_73 Extensions (Indices 137--171)

Volta+ additions. Primarily introduces uniform register variants and additional tensor core shapes.

### Uniform Register Operations

Uniform registers (UR0--UR63) hold values shared across the warp, enabling scalar execution of warp-uniform computations.

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 138 | `HOERI` | **UBREV** | Uniform bit reverse |
| 139 | `HOZFX` | **UBMSK** | Uniform bitmask |
| 140 | `HPYRN` | **UCLEA** | Uniform clear address |
| 141 | `HVFRGC` | **UISETP** | Uniform integer set-predicate |
| 142 | `HYQP` | **ULDC** | Uniform load constant |
| 143 | `HYRN` | **ULEA** | Uniform load effective address |
| 144 | `HC2HE` | **UP2UR** | Uniform predicate to uniform register |
| 145 | `HYBC3` | **ULOP3** | Uniform three-input logic |
| 146 | `HCYBC3` | **UPLOP3** | Uniform predicate three-input logic |
| 147 | `HFRY` | **USEL** | Uniform select |
| 148 | `HFTKG` | **USGXT** | Uniform sign-extend |
| 149 | `HSYB` | **UFLO** | Uniform find leading one |
| 150 | `HVNQQ3` | **UIADD3** | Uniform three-input integer add |
| 151 | `HVZNQ` | **UIMAD** | Uniform integer multiply-add |
| 152 | `HZBI` | **UMOV** | Uniform move |
| 153 | `HCEZG` | **UPRMT** | Uniform byte permute |
| 154 | `IBGRH` | **VOTEU** | Uniform vote |
| 155 | `HCBCP` | **UPOPC** | Uniform population count |
| 156 | `HFUS` | **USHF** | Uniform funnel shift |

### Additional sm_73 Operations

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 157 | `FPNGGRE` | **SCATTER** | Scatter write |
| 158 | `S2SC` | **F2FP** | Float to float, packed conversion |
| 159 | `UZZN_1688` | **HMMA_1688** | FP16 MMA, 16x8x8 shape |
| 160 | `UZZN_16816` | **HMMA_16816** | FP16 MMA, 16x8x16 shape |
| 161 | `OZZN` | **BMMA** | Binary (1-bit) matrix multiply-accumulate |
| 162 | `GGHPPGY` | **TTUCCTL** | Tensor texture unit cache control |
| 163 | `GGHZNPEB` | **TTUMACRO** | Tensor texture unit macro |
| 164 | `E2HE` | **R2UR** | GPR to uniform register |
| 165 | `ZBIZ` | **MOVM** | Move with mask |
| 166 | `YQFZ` | **LDSM** | Load from shared memory to matrix register |
| 167 | `YQGENZ` | **LDTRAM** | Load from TRAM (transposed shared memory) |
| 168 | `SBBGCEVAG` | **FOOTPRINT** | Texture footprint query |
| 169 | `F2HE` | **S2UR** | Special register to uniform register |
| 170 | `OEKH` | **BRXU** | Branch indirect, uniform target |

## sm_82 Extensions (Indices 172--193)

Ampere additions. New MMA shapes, gather/scatter metadata, and reduction variants.

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 173 | `TNGURE` | **GATHER** | Gather (multi-address load) |
| 174 | `TRAZRGNQNGN` | **GENMETADATA** | Generate metadata (for sparse MMA) |
| 175 | `FCZRGNQNGN` | **SPMETADATA** | Sparse metadata |
| 176 | `OZZN_88128` | **BMMA_88128** | Binary MMA, 8x8x128 shape |
| 177 | `OZZN_168128` | **BMMA_168128** | Binary MMA, 16x8x128 shape |
| 178 | `OZZN_168256` | **BMMA_168256** | Binary MMA, 16x8x256 shape |
| 179 | `PYZNQ` | **CLMAD** | Carry-less multiply-add (GF(2) arithmetic) |
| 180 | `QZZN` | **DMMA** | FP64 matrix multiply-accumulate (Ampere; encoding category 434; re-introduced at index 215 for Hopper with different TC path) |
| 181 | `UZZN_FC_1688` | **HMMA_SP_1688** | FP16 sparse MMA, 16x8x8 |
| 182 | `USZN2_ZZN` | **HFMA2_MMA** | FP16 FMA2, MMA variant |
| 183 | `UZAZK2` | **HMNMX2** | Packed FP16x2 min/max |
| 184 | `VZZN_88` | **IMMA_88** | Integer MMA, 8x8 shape |
| 185 | `VZZN_FC_88` | **IMMA_SP_88** | Integer sparse MMA, 8x8 |
| 186 | `VZZN_16816` | **IMMA_16816** | Integer MMA, 16x8x16 |
| 187 | `VZZN_16832` | **IMMA_16832** | Integer MMA, 16x8x32 |
| 188 | `VZZN_FC_16832` | **IMMA_SP_16832** | Integer sparse MMA, 16x8x32 |
| 189 | `NEEVIRF` | **ARRIVES** | Async barrier arrive signal |
| 190 | `YQTQRCONE` | **LDGDEPBAR** | Load-global dependency barrier |
| 191 | `YQTFGF` | **LDGSTS** | Load-global, store-to-shared (async copy) |
| 192 | `ERQHK` | **REDUX** | Warp-wide reduction (uniform result) |

## sm_86 Extensions (Indices 194--199)

Ampere+ (GA106/GA107) additions.

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 195 | `S2VC` | **F2IP** | Float to integer, packed |
| 196 | `HS2SC` | **UF2FP** | Uniform float to float, packed |
| 197 | `V2SC` | **I2FP** | Integer to float, packed |
| 198 | `FHDHREL` | **SUQUERY** | Surface query (dimensions, format) |

## sm_89 Extensions (Indices 200--205)

Ada Lovelace additions. Quarter-precision MMA shapes for FP8/INT4.

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 201 | `DZZN_16816` | **QMMA_16816** | Quarter-precision MMA, 16x8x16 (FP8) |
| 202 | `DZZN_16832` | **QMMA_16832** | Quarter-precision MMA, 16x8x32 |
| 203 | `DZZN_FC_16832` | **QMMA_SP_16832** | Quarter-precision sparse MMA, 16x8x32 |
| 204 | `DZZN_FC_12864` | **QMMA_SP_12864** | Quarter-precision sparse MMA, 128x64 |

## sm_90 Extensions (Indices 206--252)

Hopper additions. Major expansion: CGA (Cooperative Grid Array) barriers, fences, GMMA (Group MMA), TMA (Tensor Memory Accelerator), and collective operations.

### CGA Barriers and Synchronization

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 207 | `NPDOYX` | **ACQBLK** | Acquire block (CTA resource acquisition) |
| 208 | `PTNONE_NEI` | **CGABAR_ARV** | CGA barrier arrive |
| 209 | `PTNONE_TRG` | **CGABAR_GET** | CGA barrier get (query state) |
| 210 | `PTNONE_FRG` | **CGABAR_SET** | CGA barrier set |
| 211 | `PTNONE_JNVG` | **CGABAR_WAIT** | CGA barrier wait |
| 212 | `PTNREEONE` | **CGAERRBAR** | CGA error barrier |

### Collective and Election

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 213 | `PERNGRCBYVPL` | **CREATEPOLICY** | Create scheduling/cache policy |
| 214 | `PIGN` | **CVTA** | Convert address space (generic to specific) |
| 215 | `QZZN` | **DMMA** | FP64 matrix multiply-accumulate (Hopper re-introduction; encoding category 515 vs 434 for index 180; uses warpgroup-aware tensor core path, shared dispatch with CVTA at case 0xD6/0xD7 in `sub_6575D0`) |
| 216 | `RYRPG` | **ELECT** | Elect a leader lane in warp |
| 217 | `RAQPBYYRPGVIR` | **ENDCOLLECTIVE** | End collective operation scope |

### Fences

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 218 | `SRAPR_T` | **FENCE_G** | Fence, global scope |
| 219 | `SRAPR_F` | **FENCE_S** | Fence, shared/CTA scope |
| 220 | `SZAZK` | **FMNMX** | FP32 min/max (Hopper re-introduction; encoding category 534 vs 510 for index 14; adds 5-entry operand sub-mode table via `dword_2026FC0` for extended rounding/precision modes not in base encoding) |

### GMMA (Group Matrix Multiply-Accumulate)

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 221 | `TZZN` | **GMMA** | Group (warpgroup) matrix multiply-accumulate |

### Memory Extensions

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 222 | `YQPH` | **LDCU** | Load constant, uniform (warp-coherent constant load) |
| 223 | `YRCP` | **LEPC** | Load effective PC (sm_90 variant) |
| 224 | `ZNCN` | **MAPA** | Map address (for TMA address translation) |
| 225 | `CERRKVG` | **PREEXIT** | Pre-exit (cleanup before thread exit) |
| 226 | `E2HE_U` | **R2UR_H** | Register to uniform register, high half |
| 227 | `ERQNF` | **REDAS** | Reduction, async (fire-and-forget with arrive) |

### Configuration

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 228 | `FRGZNKERT` | **SETMAXREG** | Set maximum register count for dynamic partitioning |
| 229 | `FRGFZRZFVMR` | **SETSMEMSIZE** | Set shared memory size dynamically |
| 230 | `FGNF` | **STAS** | Store async (to shared, with barrier) |
| 231 | `FGFZ` | **STSM** | Store to shared memory, matrix layout |

### Synchronization Extensions

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 232 | `FLAPF_ONFVP` | **SYNCS_BASIC** | Sync scope, basic |
| 233 | `FLAPF_YQ_HAVSZ` | **SYNCS_LD_UNIFM** | Sync scope with uniform load |

### Uniform Block Operations

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 234 | `HOYXPC` | **UBLKCP** | Uniform block copy |
| 235 | `HOYXERQ` | **UBLKRED** | Uniform block reduction |
| 236 | `HOYXCS` | **UBLKPF** | Uniform block prefetch |
| 237 | `HPIGN` | **UCVTA** | Uniform convert address space |
| 238 | `HYRCP` | **ULEPC** | Uniform load effective PC |
| 239 | `HZNCN` | **UMAPA** | Uniform map address |

### TMA (Tensor Memory Accelerator) Operations

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 240 | `HGZNPPGY` | **UTMACCTL** | TMA cache control |
| 241 | `HGZNPZQSYHFU` | **UTMACMDFLUSH** | TMA command flush |
| 242 | `HGZNYQT` | **UTMALDG** | TMA load global |
| 243 | `HGZNCS` | **UTMAPF** | TMA prefetch |
| 244 | `HGZERQT` | **UTMREDG** | TMA reduction global |
| 245 | `HGZNYFG` | **UTMALST** | TMA load/store |

### Vector Min/Max Extensions

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 246 | `IUZAZK` | **VHMNMX** | Vector half min/max (FP16x2) |
| 247 | `IVNQQ` | **VIADD** | Vector integer add |
| 248 | `IVNQQZAZK` | **VIADDMNMX** | Vector integer add with min/max |
| 249 | `IVZAZK` | **VIMNMX** | Vector integer min/max |
| 250 | `IVZAZK3` | **VIMNMX3** | Vector integer three-input min/max |
| 251 | `JNECTEBHC` | **WARPGROUP** | Warpgroup collective operation |

## sm_100 Extensions (Indices 253--280)

Blackwell datacenter additions. UTC (Unified Tensor Core) operations, quad-precision FP, FP32x2 packed operations, and tensor core swizzle load/store.

### Packed FP32 and Reduction

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 254 | `PERQHK` | **CREDUX** | CTA-scope reduction (cross-warp) |
| 255 | `SNQQ2` | **FADD2** | Packed FP32x2 add |
| 256 | `SSZN2` | **FFMA2** | Packed FP32x2 fused multiply-add |
| 257 | `SZAZK3` | **FMNMX3** | FP32 three-input min/max |
| 258 | `SZHY2` | **FMUL2** | Packed FP32x2 multiply |

### Tensor Memory

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 259 | `YQGZ` | **LDTM** | Load via tensor memory (5th-gen tensor core) |
| 260 | `HTRGARKGJBEXVQ` | **UGETNEXTWORKID** | Uniform get next work ID (dynamic scheduling) |

### UTC (Unified Tensor Core) Operations

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 261 | `HGPONE_1PGN` | **UTCBAR_1CTA** | UTC barrier, 1 CTA scope |
| 262 | `HGPONE_2PGN` | **UTCBAR_2CTA** | UTC barrier, 2 CTA scope |
| 263 | `HGPPC_1PGN` | **UTCCP_1CTA** | UTC copy, 1 CTA scope |
| 264 | `HGPPC_2PGN` | **UTCCP_2CTA** | UTC copy, 2 CTA scope |
| 265 | `HGPZZN_1PGN` | **UTCMMA_1CTA** | UTC MMA, 1 CTA scope |
| 266 | `HGPZZN_2PGN` | **UTCMMA_2CTA** | UTC MMA, 2 CTA scope |
| 267 | `HGPFUVSG_1PGN` | **UTCSHIFT_1CTA** | UTC shift, 1 CTA scope |
| 268 | `HGPFUVSG_2PGN` | **UTCSHIFT_2CTA** | UTC shift, 2 CTA scope |

### Tensor Core Swizzle

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 269 | `IVEGPBHAG` | **VIRTCOUNT** | Virtual thread count query |
| 270 | `GPNGBZFJF` | **TCATOMSWS** | Tensor core atomic with swizzle |
| 271 | `GPYQFJF` | **TCLDSWS** | Tensor core load with swizzle |
| 272 | `GPFGFJF` | **TCSTSWS** | Tensor core store with swizzle |

### Quad-Precision FP

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 273 | `DSZN4` | **QFMA4** | Quad-element FP fused multiply-add |
| 274 | `DNQQ4` | **QADD4** | Quad-element FP add |
| 275 | `DZHY4` | **QMUL4** | Quad-element FP multiply |

### Additional sm_100

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 276 | `ZRZFRG` | **MEMSET** | Memory set (block fill) |
| 277 | `NPDFUZVAVG` | **ACQSHMINIT** | Acquire shared memory and initialize |
| 278 | `FGGZ` | **STTM** | Store via tensor memory |
| 279 | `SRAPR_G` | **FENCE_T** | Fence, tensor scope |

## sm_104 Extensions (Indices 281--320)

Blackwell Ultra additions. Uniform FP operations, additional integer widths, conversion variants, MMA shape extensions, and MKQ sparse variants.

### Integer Extensions

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 282 | `VNQQ` | **IADD** | Integer add (two-input, distinct from IADD3) |
| 283 | `HIVNQQ` | **UVIADD** | Uniform vector integer add |
| 284 | `VZAZK` | **IMNMX** | Integer min/max, 32-bit operands (sm_104 re-introduction; new Blackwell Ultra encoding path distinct from base index 37) |
| 285 | `VZAZK` | **IMNMX** | Integer min/max, 64-bit operands (SASS prints as `IMNMX.64`; consecutive with 284 to form the 32/64-bit pair; `.64.UI` and `.64.LO` sub-modifiers select unsigned/low-half comparison modes) |
| 286 | `HVZAZK` | **UIMNMX** | Uniform integer min/max |
| 287 | `HIVZAZK` | **UVIMNMX** | Uniform vector integer min/max |
| 288 | `VFRGC` | **ISETP** | Integer set-predicate (sm_104 re-introduction; supports 64-bit operand comparison as `ISETP.64` with `.64.UI`/`.64.LO` sub-modifiers; new encoding path, case 0x120 in `sub_7482B0` and `sub_8380A0`) |
| 289 | `HVFRGC` | **UISETP** | Uniform integer set-predicate (sm_104 re-introduction of index 141; pairs with ISETP index 288 for 64-bit uniform comparison) |

### Data Movement Extensions

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 290 | `ZBI` | **MOV** | Move (sm_104 variant) |
| 291 | `HZBI` | **UMOV** | Uniform move (sm_104 variant) |
| 292 | `FRY` | **SEL** | Select (sm_104 variant) |
| 293 | `HFRY` | **USEL** | Uniform select (sm_104 variant) |

### Uniform FP Operations

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 294 | `HSNQQ` | **UFADD** | Uniform FP add |
| 295 | `HSFRY` | **UFSEL** | Uniform FP select |
| 296 | `HSSZN` | **UFFMA** | Uniform FP fused multiply-add |
| 297 | `HSZHY` | **UFMUL** | Uniform FP multiply |
| 298 | `HSFRG` | **UFSET** | Uniform FP compare and set |
| 299 | `HSFRGC` | **UFSETP** | Uniform FP compare and set predicate |

### Uniform Conversion

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 300 | `HV2V` | **UI2I** | Uniform integer to integer conversion |
| 301 | `HV2VC` | **UI2IP** | Uniform integer to integer, packed |
| 302 | `HS2S` | **UF2F** | Uniform float to float |
| 303 | `HSEAQ` | **UFRND** | Uniform FP round |
| 304 | `HS2V` | **UF2I** | Uniform float to integer |
| 305 | `HS2VC` | **UF2IP** | Uniform float to integer, packed |
| 306 | `HV2S` | **UI2F** | Uniform integer to float |
| 307 | `HV2SC` | **UI2FP** | Uniform integer to float, packed |
| 308 | `HVNOF` | **UIABS** | Uniform integer absolute value |
| 309 | `PF2HE` | **CS2UR** | Control/status register to uniform register |
| 310 | `HS2SC` | **UF2FP** | Uniform float to float, packed (sm_104 variant) |

### MMA Extensions

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 311 | `ZKDZZN_FS_16832` | **MXQMMA_SF_16832** | Mixed-quantized structured-sparse MMA, 16x8x32 |
| 312 | `BZZN_16864` | **OMMA_16864** | Operand MMA, 16x8x64 shape |
| 313 | `BZZN_FC_168128` | **OMMA_SP_168128** | Operand sparse MMA, 16x8x128 |
| 314 | `DZZN_16816` | **QMMA_16816** | Quarter-precision MMA (sm_104 variant) |
| 315 | `DZZN_16832` | **QMMA_16832** | Quarter-precision MMA (sm_104 variant) |
| 316 | `DZZN_FC_16832` | **QMMA_SP_16832** | Quarter-precision sparse MMA (sm_104 variant) |
| 317 | `DZZN_FC_12864` | **QMMA_SP_12864** | Quarter-precision sparse MMA (sm_104 variant) |
| 318 | `DZZN_FS_16832` | **QMMA_SF_16832** | Quarter-precision structured sparse MMA |
| 319 | `DZZN_FS_FC_16864` | **QMMA_SF_SP_16864** | Quarter-precision structured+unstructured sparse MMA |

### Boundary Markers

| Idx | ROT13 | Mnemonic | Description |
|-----|-------|----------|-------------|
| 136 | `FZ70_YNFG` | SM70_LAST | End of sm_70 base ISA |
| 137 | `FZ73_SVEFG` | SM73_FIRST | Start of sm_73 extensions |
| 171 | `FZ73_YNFG` | SM73_LAST | End of sm_73 |
| 172 | `FZ82_SVEFG` | SM82_FIRST | Start of sm_82 extensions |
| 193 | `FZ82_YNFG` | SM82_LAST | End of sm_82 |
| 194 | `FZ86_SVEFG` | SM86_FIRST | Start of sm_86 extensions |
| 199 | `FZ86_YNFG` | SM86_LAST | End of sm_86 |
| 200 | `FZ89_SVEFG` | SM89_FIRST | Start of sm_89 extensions |
| 205 | `FZ89_YNFG` | SM89_LAST | End of sm_89 |
| 206 | `FZ90_SVEFG` | SM90_FIRST | Start of sm_90 extensions |
| 252 | `FZ90_YNFG` | SM90_LAST | End of sm_90 |
| 253 | `FZ100_SVEFG` | SM100_FIRST | Start of sm_100 extensions |
| 280 | `FZ100_YNFG` | SM100_LAST | End of sm_100 |
| 281 | `FZ104_SVEFG` | SM104_FIRST | Start of sm_104 extensions |
| 320 | `FZ104_YNFG` | SM104_LAST | End of sm_104 |
| 321 | `YNFG` | LAST | End-of-table sentinel |

## Encoding Category Map at `unk_21C0E00`

The 0x508 bytes (1288 bytes) at `unk_21C0E00` are **not** additional opcode names. They are a 322-element `int32` array mapping each opcode index to an **encoding category** number — a level of indirection between opcode indices and binary encoding format descriptors.

### Binary Evidence

1. RSI is loaded with `0x21C0E00` (at `0x7A5D9F: mov $0x21c0e00, %esi`)
2. RDI is set to `obj+0x2478` (at `0x7A5D82: lea 0x2478(%rbx), %rdi`)
3. RCX is set to 161 (at `0x7A5D22: mov $0xa1, %r13d`; `0x7A5D69: mov %r13, %rcx`)
4. The `rep movsq` at `0x7A791D` copies 161 quadwords = 1288 bytes = 322 x 4 bytes

The destination offset +0x2478 (decimal 9336) is immediately after the 322-entry name table (+4184 through +9328). Three arch-specific constructors each populate this array from a different static source table:

| Constructor | Source Table | Map Content |
|---|---|---|
| `sub_7A5D10` (base) | `unk_21C0E00` | Identity: `map[i] = i` for all i in 0..321 |
| `sub_7C5410` | `unk_21C3600` | Arch-remapped (selected entries differ) |
| `sub_BE7390` | `unk_22B2320` | Arch-remapped (selected entries differ) |

### Reader: `sub_1377C60` (SASS Mnemonic Lookup)

The SASS mnemonic lookup function at `sub_1377C60` reads this map at line 292:

```c
v84 = *(_DWORD *)(a1 + 4 * v18 + 9336);  // encoding_category_map[opcode_index]
```

After matching an input mnemonic string against the ROT13 name table (with inline decoding at lines 264-273), the function reads `encoding_category_map[opcode_index]` and uses the result as a hash key — combined with a 24-bit architecture discriminator via FNV-1a — to look up the encoding format descriptor in the hash table at `InstructionInfo+10672`.

This is why duplicate mnemonics (e.g. DMMA at indices 180 and 215, or FMNMX at indices 14 and 220) can have different encoding categories (434 vs 515, 510 vs 534): the category map provides the indirection needed to select different binary encoders for the same mnemonic across architectures. The opcode name table has exactly 322 entries and no more.

## Opcode Category Summary

| Category | Base ISA | sm_73+ | sm_82+ | sm_86+ | sm_89+ | sm_90+ | sm_100+ | sm_104+ | Total |
|----------|----------|--------|--------|--------|--------|--------|---------|---------|-------|
| Integer ALU | 16 | 10 | 1 | 0 | 0 | 2 | 0 | 5 | 34 |
| FP32 | 10 | 0 | 0 | 0 | 0 | 1 | 4 | 0 | 15 |
| FP64 | 4 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 5 |
| FP16 | 6 | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 8 |
| Conversion | 10 | 1 | 0 | 3 | 0 | 0 | 0 | 10 | 24 |
| Data Movement | 9 | 5 | 0 | 0 | 0 | 2 | 0 | 5 | 21 |
| Predicate/Vote | 4 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 6 |
| Load/Store | 11 | 3 | 2 | 0 | 0 | 5 | 2 | 0 | 23 |
| Atomic/Reduce | 4 | 0 | 1 | 0 | 0 | 1 | 0 | 0 | 6 |
| Cache/Fence | 6 | 1 | 0 | 1 | 0 | 2 | 1 | 0 | 11 |
| Texture | 6 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 8 |
| Surface | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 4 |
| Control Flow | 13 | 1 | 0 | 0 | 0 | 1 | 0 | 0 | 15 |
| Sync/Warp | 10 | 0 | 0 | 0 | 0 | 4 | 0 | 0 | 14 |
| Tensor Core | 3 | 3 | 10 | 0 | 4 | 1 | 9 | 9 | 39 |
| TMA | 0 | 0 | 0 | 0 | 0 | 6 | 0 | 0 | 6 |
| Uniform Block | 0 | 0 | 0 | 0 | 0 | 3 | 1 | 6 | 10 |
| CGA/Collective | 0 | 0 | 0 | 0 | 0 | 5 | 0 | 0 | 5 |
| Graphics | 7 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 8 |
| System/Misc | 7 | 0 | 1 | 0 | 0 | 4 | 2 | 0 | 14 |
| Boundaries | 2 | 2 | 2 | 2 | 2 | 2 | 2 | 2 | 16 |

## Encoding Format Correlation

From the encoding page analysis, the approximate distribution of 64-bit vs 128-bit formats for the base ISA:

**64-bit format (format code 0x1):** NOP, BRA, BRX, JMP, JMX, CALL, RET, EXIT, BREAK, BSSY, BSYNC, BPT, KILL, RTT, BAR, DEPBAR, WARPSYNC, BMOV, B2R, R2B, S2R, CS2R, MOV (short form), YIELD, ERRBAR, NANOSLEEP, NANOTRAP, SHFL. These are primarily control-flow, barriers, and simple data movement instructions that need fewer operand bits.

**128-bit format (format code 0x2):** All ALU operations (IMAD, IADD3, FFMA, FADD, FMUL, LOP3, ISETP, FSETP, etc.), all memory operations (LDG, STG, LDS, STS, LDL, STL, LD, ST, LDC), all atomics (ATOM, ATOMG, ATOMS, RED), all texture operations (TEX, TLD, TLD4, TMML, TXD, TXQ), all surface operations, tensor core operations (HMMA, IMMA, BMMA, GMMA, etc.), conversion instructions, and most uniform register operations.

**256-bit format (format code 0x8):** IMAD.WIDE variants with 16 constant-bank operand slots. Extremely rare — only 2 encoder functions use this format.

The 64-bit short-form encoders cover 27 opcode classes across 174 encoder functions total. The 128-bit encoders cover the remaining ~75+ opcode classes across 912+ encoder functions.

## SM100 Encoding Variant Counts

Per-opcode variant counts for the SM100 (Blackwell datacenter) SASS encoder, extracted from the 683 concrete encoding handler functions at `0xED1520`--`0xFA5F10`. Each function encodes one (opcode, operand-form) pair — e.g., `FFMA reg,reg,reg` vs `FFMA reg,reg,imm` vs `FFMA reg,reg,pred`. The "Enc ID" column is the numeric value written to `*(WORD*)(a2+12)` by each handler, which maps to the SASS binary major opcode through the encoding dispatch megafunctions. The "SASS Mnemonic" column gives the canonical name from the 322-entry ROT13 opcode name table in `InstructionInfo`. Where two encoder IDs map to the same mnemonic (e.g. IADD3 IDs 0+1, LOP3 IDs 4+10), both are listed; the "Combined" column gives the merged count for that instruction.

Source: sweep report `p1.14-sweep-0xED1000-0xFA6000.txt`, ptxas v13.0.88.

### Integer ALU

| Enc ID | Variants | SASS Mnemonic | Combined | Formats |
|--------|----------|---------------|----------|---------|
| 0 | 8 | IADD3 | 13 (IDs 0+1) | 23F1DF8, 23F1F08 |
| 1 | 5 | IADD3 | | 23F1DF8, 23F1F08 |
| 15 | 19 | IMAD | 19 | 23F1DF8, 23F2018 |
| 40 | 23 | IMAD (wide) | 23 | 23F1DF8, 23F21B0 |
| 42 | 34 | IMAD (extended) | 34 | 23F1DF8, 23F21B0 |
| 4 | 4 | LOP3 | 12 (IDs 4+10) | 23F2018 |
| 10 | 8 | LOP3 | | 23F2018 |
| 34 | 33 | ISETP | 33 | 23F1DF8, 23F29A8 |
| 30 | 2 | IMNMX | 2 | 23F1D70 |
| 43 | 13 | FLO | 13 | 23F1D70, 23F1DF8 |
| 44 | 4 | IABS | 4 | 23F1F08, 23F1F90 |
| 47 | 5 | POPC | 5 | 23F1F08, 23F1F90 |
| 49 | 2 | BREV | 2 | 23F1DF8 |
| 21 | 5 | SHF | 5 | 23F1DF8, 23F1F08 |
| 84 | 6 | SHF | 6 | 23F1F08, 23F1F90 |
| **Subtotal** | | | **171** | |

### FP32 ALU

| Enc ID | Variants | SASS Mnemonic | Combined | Formats |
|--------|----------|---------------|----------|---------|
| 13 | 30 | FFMA | 30 | 23F2018..23F2EF8 |
| 14 | 11 | FADD | 11 | 23F1F90, 23F2E70 |
| 22 | 18 | FMUL | 18 | 23F1DF8..23F2678 |
| 31 | 2 | FMNMX | 2 | 23F1D70 |
| 35 | 30 | FSETP | 30 | many formats |
| 33 | 2 | FSET/CSET | 2 | 23F2238 |
| 38 | 2 | FSWZADD | 2 | 23F2128 |
| 103 | 9 | extended FMA | 9 | 23F1DF8..23F2678 |
| **Subtotal** | | | **104** | |

### FP64 ALU

| Enc ID | Variants | SASS Mnemonic | Combined | Formats |
|--------|----------|---------------|----------|---------|
| 59 | 6 | DFMA | 6 | 23F2678, 23F2EF8 |
| 91 | 2 | DADD | 2 | 23F1DF8 |
| 57 | 5 | DMUL | 5 | 23F1F08 |
| 65 | 6 | DSETP | 6 | 23F2678, 23F2EF8 |
| **Subtotal** | | | **19** | |

### FP16 / Half-Precision

| Enc ID | Variants | SASS Mnemonic | Combined | Formats |
|--------|----------|---------------|----------|---------|
| 23 | 18 | HFMA2/HMUL2 | 18 | 23F1DF8..23F2678 |
| 37 | 34 | HSETP2/DSETP | 34 | 23F1DF8, 23F21B0 |
| **Subtotal** | | | **52** | |

### Data Movement

| Enc ID | Variants | SASS Mnemonic | Combined | Formats |
|--------|----------|---------------|----------|---------|
| 18 | 78 | MOV | 78 | many formats |
| 32 | 28 | SEL | 28 | 23F1D70, 23F1DF8 |
| 71 | 45 | P2R/R2P | 45 | many formats |
| 19 | 3 | PRMT | 3 | 23F1C60, 23F1D70 |
| 20 | 3 | LEA | 3 | 23F1DF8, 23F1F08 |
| 6 | 5 | S2R | 5 | 23F1F08, 23F1F90 |
| 7 | 2 | CS2R | 2 | 23F2018 |
| **Subtotal** | | | **164** | |

### Memory

| Enc ID | Variants | SASS Mnemonic | Combined | Formats |
|--------|----------|---------------|----------|---------|
| 27 | 24 | LDG/STG | 24 | 23F1F08, 23F29A8 |
| 77 | 18 | LDS/STS | 18 | 23F29A8 |
| 94 | 16 | LDL/STL | 16 | 23F29A8 |
| 74 | 6 | ST | 6 | 23F1DF8, 23F1F08 |
| 50 | 5 | ATOM/ATOMG | 5 | 23F1DF8, 23F1F08 |
| 81 | 6 | RED | 6 | 23F1F08, 23F1F90 |
| 100 | 3 | SULD | 3 | 23F1DF8, 23F1F08 |
| **Subtotal** | | | **78** | |

### Tensor Core

| Enc ID | Variants | SASS Mnemonic | Combined | Formats |
|--------|----------|---------------|----------|---------|
| 78 | 35 | HMMA/IMMA | 35 | 23F1DF8, 23F29A8 |
| 90 | 5 | BMMA/QMMA | 5 | 23F2678 |
| **Subtotal** | | | **40** | |

### Texture

| Enc ID | Variants | SASS Mnemonic | Combined | Formats |
|--------|----------|---------------|----------|---------|
| 5 | 1 | TLD | 1 | 23F1F08 |
| 8 | 2 | TEX | 2 | 23F1DF8, 23F1F90 |
| 9 | 1 | TLD4 | 1 | 23F1F08 |
| 88 | 2 | TEX (variant) | 2 | 23F1F08 |
| **Subtotal** | | | **6** | |

### Predicate / Warp

| Enc ID | Variants | SASS Mnemonic | Combined | Formats |
|--------|----------|---------------|----------|---------|
| 79 | 7 | PLOP3 | 7 | 23F1F08..23F2018 |
| 82 | 6 | VOTE | 6 | 23F1F08, 23F1F90 |
| 48 | 7 | SHFL | 7 | 23F1D70, 23F1DF8 |
| **Subtotal** | | | **20** | |

### Control Flow / Sync

| Enc ID | Variants | SASS Mnemonic | Combined | Formats |
|--------|----------|---------------|----------|---------|
| 17 | 1 | BRA | 1 | 23F1F08 |
| 73 | 10 | BAR | 10 | 23F1F08, 23F2238 |
| 92 | 1 | DEPBAR | 1 | 23F1F08 |
| 98 | 1 | MEMBAR | 1 | 23F1F08 |
| 11 | 14 | MUFU | 14 | 23F1F08, 23F1F90 |
| 45 | 1 | NOP | 1 | 23F1D70 |
| 46 | 1 | YIELD/EXIT | 1 | 23F2238 |
| **Subtotal** | | | **29** | |

### Totals

| Category | Encoder Functions | Distinct Opcodes |
|----------|-------------------|------------------|
| Integer ALU | 171 | 15 (across 10 mnemonics) |
| FP32 ALU | 104 | 8 |
| FP64 ALU | 19 | 4 |
| FP16 | 52 | 2 |
| Data Movement | 164 | 7 |
| Memory | 78 | 7 |
| Tensor Core | 40 | 2 |
| Texture | 6 | 4 |
| Predicate/Warp | 20 | 3 |
| Control/Sync | 29 | 7 |
| **Total** | **683** | **59** |

The top 5 instructions by variant count — MOV (78), P2R/R2P (45), HMMA/IMMA (35), IMAD extended (34), HSETP2/DSETP (34) — account for 226 of 683 encoders (33%). MOV alone accounts for 11.4% of all encoder functions because every possible source type (GPR, uniform reg, immediate, constant bank, predicate, special reg) and every destination type requires a separate encoder with a distinct operand signature and bitfield extraction sequence.

The 21 encoding format descriptors (xmmword groups) cluster into three tiers by usage: heavy (165+141+101 = 407 functions across 3 formats), medium (87+47+36 = 170 across 3 formats), and light (106 functions across 15 formats). The heavy-tier formats (23F1F08, 23F1DF8, 23F29A8) are the simple/compact, primary ALU, and memory/load-store formats respectively — these three alone cover 60% of all SM100 encoders.

## Internal Index vs. Numeric Opcode

The index in this table (the position within the ROT13 name array) is the value stored in the Ori IR instruction's opcode field at offset +72 (lower 12 bits). However, this index is distinct from the encoded SASS major opcode in the binary instruction word. The mapping between IR opcode index and SASS binary major opcode is performed by the encoding dispatch tables (the "six megafunctions" at `0x10C0B20`--`0x10E32E0`, which switch on up to 370 opcode category values from 0x0 through 0x171). A single IR opcode index may map to multiple SASS major opcodes depending on operand types and modifier bits, and vice versa.

Known IR-index-to-numeric correlations (confirmed from switch statements across multiple independent functions):

| IR Index | Numeric (encoding switch) | Mnemonic |
|----------|--------------------------|----------|
| 1 | 0x59 | IMAD |
| 3 | 0x29 | IADD3 |
| 25 | (64-bit, no major) | NOP |
| 52 | (pseudo) | BB boundary |
| 77 | (64-bit, no major) | EXIT |
| 91 | 0x1E | ATOM |
| 95 | (64-bit, no major) | EXIT/RET |
| 96 | 0x38 | LDG |
| 221 | 0xDF | GMMA |

## Extended Mnemonic Table (`sub_896D50`)

A second, much larger mnemonic table is constructed by `sub_896D50` (21KB, vtable `off_21DA9F8`). This "extended" table serves a different purpose from the primary 322-entry table: it is used during **SASS disassembly input parsing** (string-to-index lookup), whereas the primary table is used during **encoding** (index-to-string). The two tables share the same base class (`sub_A2B110`) but have different vtables and different object layouts.

### Table Dimensions

| Property | Primary (`sub_7A5D10`) | Extended (`sub_896D50`) |
|----------|------------------------|-------------------------|
| Entry count | 322 (indices 0--321) | 773 (indices 0--772) |
| Effective mnemonics | 306 (excl. 16 boundary markers) | 772 (excl. NONE sentinel) |
| Entry size | 16 bytes (8B ptr + 8B len) | 16 bytes (8B ptr + 8B len) |
| Object offset | +0x1058 (+4184) | +0x2C60 (+11360) |
| Ordering | By IR opcode index | Alphabetical by ROT13 name |
| Encoding category map | 322 x int32 at +0x2478 | 772 x int32 at +0x5CB0 (+23728), from `unk_21D92E0` |
| Vtable | `off_233ADC0` | `off_21DA9F8` |

### Why 772 Entries?

The extended table is 2.4x larger because it expands each base mnemonic into its modifier-qualified SASS forms. For example, the primary table stores one `IMAD` entry (index 1), but the extended table stores seven:

| Extended entry | ROT13 | Description |
|----------------|-------|-------------|
| IMAD | `VZNQ` | Base form |
| IMAD.HI | `VZNQ.UV` | High-half variant |
| IMAD.WIDE | `VZNQ.JVQR` | 32x32->64 |
| IMAD.WIDE.READ.AB | `VZNQ.JVQR.ERNQ.NO` | Paired read, A+B |
| IMAD.WIDE.READ.CH | `VZNQ.JVQR.ERNQ.PU` | Paired read, C high |
| IMAD.WIDE.READ.CL | `VZNQ.JVQR.ERNQ.PY` | Paired read, C low |
| IMAD.WIDE.WRITE.DH | `VZNQ.JVQR.JEVGR.QU` | Paired write, D high |
| IMAD.WIDE.WRITE.DL | `VZNQ.JVQR.JEVGR.QY` | Paired write, D low |

### Entry Composition

The 771 populated entries (from the decompiled string assignments at `a1+11360` through `a1+23712`) break down as:

| Category | Count | Examples |
|----------|-------|---------|
| SASS base mnemonics (also in primary table) | 244 | IMAD, FADD, LDG, BRA, MOV, ... |
| SASS dot-modified variants | 125 | FENCE.G, ISETP.64, BAR.SYNC.DEFER\_BLOCKING, HMMA.SP.16832.F16.* |
| SASS new base names (not in primary) | 81 | BGMMA, RPCMOV, SYNCS, MOV32I, SHL, SHR, LOP, BITEXTRACT |
| Mercury internal descriptors | 321 | MERCURY\_addmin\_srcs\_r\_ur\_0, MERCURY\_mbarrier\_try\_wait\_... |
| **Total SASS** | **450** | |
| **Total (SASS + Mercury)** | **771** | |

Of the 450 SASS entries, 7 carry annotation text in parentheses: `F2F (not F64)`, `F2I (not *64)`, `FRND (not F64)`, `I2F (not F64)`, `NANOSLEEP (with Rb)`, `NANOTRAP (with Rb)`, `WARPSYNC (with Rb)`. These annotations indicate operand-type restrictions or register-variant qualifiers used by the SASS parser to disambiguate instruction forms.

### 32-Bit Immediate Forms

These mnemonics represent SASS instructions with a 32-bit immediate operand packed directly into the instruction word. They do not appear as separate entries in the primary IR opcode table because the immediate form is selected during encoding based on operand type, not during IR construction:

| ROT13 | Mnemonic | Description |
|-------|----------|-------------|
| `SNQQ32V` | **FADD32I** | FP32 add with 32-bit immediate |
| `SSZN32V` | **FFMA32I** | FP32 FMA with 32-bit immediate |
| `SZHY32V` | **FMUL32I** | FP32 multiply with 32-bit immediate |
| `UNQQ2_32V` | **HADD2_32I** | FP16x2 add with 32-bit immediate |
| `USZN2_32V` | **HFMA2_32I** | FP16x2 FMA with 32-bit immediate |
| `UZHY2_32V` | **HMUL2_32I** | FP16x2 multiply with 32-bit immediate |
| `VNQQ32V` | **IADD32I** | Integer add with 32-bit immediate |
| `VNQQ2` | **IADD2** | Two-input integer add (32I related) |
| `VZHY32V` | **IMUL32I** | Integer multiply with 32-bit immediate |
| `VZHY32V.JVQR` | **IMUL32I.WIDE** | Integer multiply-wide with 32-bit immediate |
| `VFPNQQ32V` | **ISCADD32I** | Integer scaled-add with 32-bit immediate |
| `YBC32V` | **LOP32I** | Logic operation with 32-bit immediate |
| `ZBI32V` | **MOV32I** | Move 32-bit immediate to register |
| `ZBI64VHE` | **MOV64IUR** | Move 64-bit immediate to uniform register |
| `HYBC32V` | **ULOP32I** | Uniform logic with 32-bit immediate |

### Mercury Pseudo-Instructions (321 Entries)

The single largest category. These are **not** real SASS instructions — they are internal pseudo-instructions representing Mercury IR operations that need mnemonic-string identity for diagnostic and dump output. They follow a rigid naming convention:

```text
MERCURY_{operation}_{srcs|dests}_{regclass}_{variant_index}
```

Register class codes in the mnemonic:
- `r` = GPR (R0--R255)
- `ur` = Uniform register (UR0--UR63)
- `p` = Predicate register (P0--P6)
- `simm` = Signed immediate
- `uimm` = Unsigned immediate
- `r2` / `ur2` = Register pair

Representative entries (decoded from ROT13):

| ROT13 | Cleartext | Operation |
|-------|-----------|-----------|
| `ZREPHEL__vage` | MERCURY\_\_intr | Generic intrinsic placeholder |
| `ZREPHEL_nqqzva_fepf_e_he_0` | MERCURY\_addmin\_srcs\_r\_ur\_0 | Fused add-min, GPR + uniform |
| `ZREPHEL_nqqznk_fepf_he_e_0` | MERCURY\_addmax\_srcs\_ur\_r\_0 | Fused add-max, uniform + GPR |
| `ZREPHEL_ngbz_pnf_vag_npd_ery_...` | MERCURY\_atom\_cas\_int\_acq\_rel\_... | Atomic CAS with acquire-release |
| `ZREPHEL_flapf_neevir_n1g0_n0g1_...` | MERCURY\_syncs\_arrive\_a1t0\_a0t1\_... | Sync arrive with token spec |

### New Base Mnemonics

Mnemonics that appear in the extended table but have no base-name match in the primary 322-entry table at all. Some are legacy forms (pre-Volta mnemonics preserved for disassembly compatibility), others are specialized operations:

| ROT13 | Mnemonic | Category |
|-------|----------|----------|
| `NPDOHYX` | **ACQBULK** | CGA bulk resource acquire |
| `OVGRKGENPG` | **BITEXTRACT** | Bitfield extract |
| `QRPBZCERFF` | **DECOMPRESS** | Data decompression |
| `VQC4N` | **IDP4A** | Integer dot-product accumulate (4-element) |
| `VZHY` | **IMUL** | Integer multiply (non-fused, legacy) |
| `VFPNQQ` | **ISCADD** | Integer scaled-add (legacy LEA form) |
| `YQTZP` | **LDGMC** | Load global with memory consistency |
| `YQG` | **LDT** | Load from texture memory |
| `YBC` | **LOP** | Two-input logic operation (legacy) |
| `CFRGC` | **PSETP** | Predicate set-predicate |
| `ERQT` | **REDG** | Reduction, global (explicit address space) |
| `FUY` | **SHL** | Shift left (legacy, replaced by SHF) |
| `FUE` | **SHR** | Shift right (legacy, replaced by SHF) |
| `FCNEFVSL` | **SPARSIFY** | Convert dense to sparse format |
| `FGG` | **STT** | Store to texture memory |
| `GNGBZT` | **TATOMG** | Texture atomic, global scope |
| `IVFRG` | **VISET** | Vector integer set |
| `JNECTEBHCFRG` | **WARPGROUPSET** | Configure warpgroup parameters |

### Modifier Suffix Patterns

Five distinct modifier suffix patterns are used in the extended table's dot-separated SASS mnemonics:

**Pattern 1 — Sub-operation mode.** The suffix selects a functional sub-operation within a single hardware instruction. `CCTL` has the most variants (7):

| Extended Mnemonic | Sub-operation |
|-------------------|---------------|
| `CCTL.C` | Clean |
| `CCTL.C.LDC` | Clean via constant cache |
| `CCTL.C.LDC.IVALL` | Clean constant cache, invalidate all |
| `CCTL.E.LDC` | Evict via constant cache |
| `CCTL.I` | Invalidate |
| `CCTL.LDCU` | Load constant, uniform path |
| `CCTL.QFAULT` | Query fault status |

Also: `SYNCS.ARRIVE.A1T0.A0T1`, `SYNCS.CAS.EXCH`, `SYNCS.CCTL`, `SYNCS.FLUSH`, `SYNCS.LD.NON_UNIFORM`, `SYNCS.LD.UNIFORM`, `SYNCS.PHASECHK` (8 variants); and `BPT.DRAIN`, `BPT.PAUSE`.

**Pattern 2 — Operand width.** The `.64` suffix (with optional `.HI`/`.LO` half-selectors) indicates 64-bit operand mode. Added for sm_104 (Blackwell Ultra):

| Extended Mnemonic | Base Opcode |
|-------------------|-------------|
| `ISETP.64`, `ISETP.64.HI`, `ISETP.64.LO` | `ISETP` (idx 288) |
| `IMNMX.64`, `IMNMX.64.HI`, `IMNMX.64.LO` | `IMNMX` (idx 285) |
| `IADD.64`, `IADD.64.HI`, `IADD.64.LO` | `IADD` (idx 282) |
| `IADD2.64`, `IADD2.64.HI`, `IADD2.64.LO` | `IADD2` |
| `MOV.64`, `MOV.64.HI`, `MOV.64.LO` | `MOV` (idx 290) |
| `SEL.64`, `SEL.64.HI`, `SEL.64.LO` | `SEL` (idx 292) |
| `UMOV.64`, `USEL.64`, `UIADD3.64`, `UIMNMX.64`, `UISETP.64` | Uniform 64-bit variants |

**Pattern 3 — Data access direction.** `IMAD.WIDE` has 5 sub-variants controlling which 32-bit half of the 64-bit accumulator is read or written. These correspond to the 256-bit instruction format (format code 0x8) with 16 constant-bank operand slots:

| Extended Mnemonic | Meaning |
|-------------------|---------|
| `IMAD.WIDE` | Default wide multiply-add |
| `IMAD.WIDE.READ.AB` | Read both A and B input halves |
| `IMAD.WIDE.READ.CL` / `.CH` | Read accumulator low / high half |
| `IMAD.WIDE.WRITE.DL` / `.DH` | Write result low / high half |
| `IMAD.HI` | High-half result only |

**Pattern 4 — Scope qualifier.** Fences, barriers, UTC operations, and synchronization carry scope suffixes:

| Extended Mnemonic | Scope |
|-------------------|-------|
| `FENCE.G` | Global (GPU-wide) |
| `FENCE.S` | Shared/CTA |
| `FENCE.T` | Tensor (sm_100+) |
| `UTCBAR.1CTA`, `UTCBAR.2CTA` | 1-CTA / 2-CTA scope |
| `UTCBAR.1CTA.FLUSH` | 1-CTA with flush |
| `BAR.SYNC.DEFER_BLOCKING` | Deferred blocking sync |
| `USETMAXREG.RELEASE` | Release variant |
| `USETSHMSZ.FLUSH` | Flush variant |

**Pattern 5 — Shape and type descriptor.** Tensor core operations carry shape geometry and data type. Brace-delimited alternation syntax indicates a single encoder handling multiple shapes:

| Extended Mnemonic | Meaning |
|-------------------|---------|
| `HMMA.F32.{16816.F16\|16816.E8M7\|1688.E8M10}` | FP16 MMA with FP32 accum, multiple shapes |
| `HMMA.SP.16832.F16.*` | Sparse FP16 MMA, 16x8x32 |
| `IMMA.{8816.*\|8832.*}` | Integer MMA, 8x8x16 or 8x8x32 |
| `IMMA.SP.{16832.*\|16864.*4.*4}` | Sparse integer MMA |
| `QMMA.SF.SP` | Structured + unstructured sparse |
| `MUFU.EX2.LOW_ACC.{F16x2, BF16x2}` | Low-accuracy EX2 for half types |

### Top Opcodes by Dot-Variant Count

| Base Opcode | Variants | Category |
|-------------|----------|----------|
| HMMA | 8 | Tensor core shape + sparse + FP type |
| SYNCS | 8 | Scope-aware synchronization modes |
| CCTL | 7 | Cache control sub-operations |
| IMAD | 7 | .HI, .WIDE, .WIDE.READ.*, .WIDE.WRITE.* |
| IMMA | 6 | Tensor core shape + sparse |
| QMMA | 6 | Shape + structured/unstructured sparse |
| USYNCS | 6 | Uniform sync scope modes |
| MUFU | 5 | .EX2, .RCP, .RSQ, .EX2 with half-precision |
| IADD | 4 | .64, .64.HI, .64.LO, .XOR |
| WARPGROUP | 3 | .ARRIVE, .DEPBAR, .WAIT |
| RPCMOV | 3 | .32, .32.READ, .64 |
| UTCBAR | 3 | .1CTA, .1CTA.FLUSH, .2CTA |

### Complete New SASS Mnemonics by Category

The following 206 SASS mnemonics appear **only** in the extended table — they have no corresponding entry in the base 322-entry name table. Many represent modifier-suffixed forms of base opcodes; others are entirely new operations.

**GMMA type-specialized (8):** `BGMMA`, `BGMMA_GSB`, `HGMMA`, `HGMMA_GSB`, `IGMMA`, `IGMMA_GSB`, `QGMMA`, `QGMMA_GSB`

**UTC type-specialized (20):** `UTCHMMA.1CTA`, `UTCHMMA.2CTA`, `UTCIMMA.1CTA`, `UTCIMMA.2CTA`, `UTCMXQMMA.1CTA`, `UTCMXQMMA.2CTA`, `UTCOMMA.1CTA`, `UTCOMMA.2CTA`, `UTCQMMA.1CTA`, `UTCQMMA.2CTA`, `UTCBAR.1CTA.FLUSH`, `UTCATOMSWS`, `UTCLDSWS`, `UTCSTSWS`, `UTCBAR.1CTA`, `UTCBAR.2CTA`, `UTCCP.1CTA`, `UTCCP.2CTA`, `UTCSHIFT.1CTA`, `UTCSHIFT.2CTA`

**DLC/DPC operations (13):** `UDLCBAR`, `UDLCCP`, `UDLCHMMA`, `UDLCIMMA`, `UDLCQMMA`, `UDPCBLKCP`, `UDPCBLKL2CCTL`, `UDPCBLKRED`, `UDPCTMACCTL`, `UDPCTMAL2CCTL`, `UDPCTMALDG`, `UDPCTMAREDG`, `UDPCTMASTG`

**Synchronization (17):** `SYNCS.ARRIVE.A1T0.A0T1`, `SYNCS.ARRIVE.A1TR.ART0.A0TR.A0TX`, `SYNCS.CAS.EXCH`, `SYNCS.CCTL`, `SYNCS.FLUSH`, `SYNCS.LD.NON_UNIFORM`, `SYNCS.LD.UNIFORM`, `SYNCS.PHASECHK`, `SYNCSU.ARRIVE.A1T0`, `SYNCSU.ARRIVE.MULTICAST.A1T0`, `WARPGROUP.ARRIVE`, `WARPGROUP.DEPBAR`, `WARPGROUP.WAIT`, `WARPGROUPSET`, `BAR.SYNC.DEFER_BLOCKING`, `BPT.DRAIN`, `BPT.PAUSE`

**Uniform sync (6):** `USYNCS.ARRIVE`, `USYNCS.ARRIVE.MULTICAST`, `USYNCS.CAS.EXCH`, `USYNCS.CCTL`, `USYNCS.LD`, `USYNCS.PHASECHK`

**Integer 64-bit variants (18):** `IADD.64`, `IADD.64.HI`, `IADD.64.LO`, `IADD.XOR`, `IADD2`, `IADD2.64`, `IADD2.64.HI`, `IADD2.64.LO`, `IMNMX.64`, `IMNMX.64.HI`, `IMNMX.64.LO`, `ISETP.64`, `ISETP.64.HI`, `ISETP.64.LO`, `MOV.64`, `MOV.64.HI`, `MOV.64.LO`, `SEL.64`, `SEL.64.HI`, `SEL.64.LO`

**Uniform scalar extended (27):** `UIADD3.64`, `UIMNMX.64`, `UISETP.64`, `UMOV.64`, `USEL.64`, `ULOP`, `ULOP32I`, `UMEMSETS.64`, `UPSETP`, `UR2UP`, `USHL`, `USHR`, `UCCTL`, `UBLKL2CCTL`, `UCGABAR_ARV`, `UCGABAR_GET`, `UCGABAR_SET`, `UCGABAR_WAIT`, `USETMAXREG`, `USETMAXREG.RELEASE`, `USETSHMSZ`, `USETSHMSZ.FLUSH`, `UREDGR`, `UREGPRERELEASE`, `USTGR`, `UTRACEEVENT`, `UVIRTCOUNT`

**IMAD/IMUL variants (8):** `IMAD.HI`, `IMAD.WIDE.READ.AB`, `IMAD.WIDE.READ.CH`, `IMAD.WIDE.READ.CL`, `IMAD.WIDE.WRITE.DH`, `IMAD.WIDE.WRITE.DL`, `IMUL.WIDE`, `IMUL32I.WIDE`

**Tensor core shapes (28):** `HMMA.16816.F16.*`, `HMMA.1688.F16.*`, `HMMA.F32.{...}` (4 entries), `HMMA.SP.{...}` (4 entries), `IMMA.{...}` (3 entries), `IMMA.SP.{...}` (3 entries), `DMMA.1684`, `DMMA.1688`, `DMMA.16816`, `BMMA.88128`, `BMMA.168128`, `BMMA.168256`, `QMMA.16816`, `QMMA.16832`, `QMMA.SF`, `QMMA.SF.SP`, `QMMA.SP.16832`, `QMMA.SP.16864`, `OMMA.SP`

**FP extensions (16):** `FADD32I`, `FFMA32I`, `FMUL32I`, `FHADD`, `FHADD2`, `FHFMA`, `FHFMA2`, `FHMUL2`, `UFHADD`, `UFHFMA`, `UFMNMX`, `MUFU.EX2`, `MUFU.RCP`, `MUFU.RSQ`, `MUFU.EX2.{F16x2, BF16x2}`, `MUFU.EX2.LOW_ACC.{F16x2, BF16x2}`

**Cache control (7):** `CCTL.C`, `CCTL.C.LDC`, `CCTL.C.LDC.IVALL`, `CCTL.E.LDC`, `CCTL.I`, `CCTL.LDCU`, `CCTL.QFAULT`

**Texture extensions (8):** `TATOMG`, `TTUCLOSE`, `TTUGO`, `TTULD`, `TTULD_CLOSE`, `TTUMACROFUSE`, `TTUOPEN`, `TTUST`

**Fence/scope (3):** `FENCE.G`, `FENCE.S`, `FENCE.T`

**Data movement (7):** `MOV32I`, `MOV64IUR`, `RPCMOV`, `RPCMOV.32`, `RPCMOV.32.READ`, `RPCMOV.64`, `CS2R` (base without size), `DECOMPRESS`

**Memory (4):** `LDGMC`, `LDT`, `STT`, `REDG`

**Other new (13):** `ACQBULK`, `BRA_IMM`, `JMP_IMM`, `JMXU`, `NONE`, `PSETP`, `HADD2_32I`, `HFMA2_32I`, `HMUL2_32I`, `IADD32I`, `IMUL`, `LOP`, `LOP32I`

### Parallel Constructor Regions

The ROT13 string data for the extended table exists in two identical regions:

| Region | Address Range | SASS Entries | MERCURY Entries |
|--------|--------------|--------------|-----------------|
| 1 | `0x2039000`--`0x203A500` | 139 unique | 32 |
| 2 | `0x21CA000`--`0x21CB100` | 139 unique | 40 |

Region 2 has 8 additional MERCURY entries not in region 1, all for sm_100/sm_104 cluster barrier and atomic operations: `MERCURY_barrier_cluster_arrive_sync_unaligned_*` (4), `MERCURY_atom_shared_cta_popc_inc_*` (3), `MERCURY_atom_shared_cta_int_acq_rel_*` (1). This indicates at least two InstructionInfo variant objects for different target architectures, where the newer variant gains additional Mercury instruction templates.

### Hash Table for O(1) Lookup

After populating the flat sorted array, `sub_896D50` constructs a hash table for O(1) mnemonic lookup during SASS parsing. The hash table is allocated as a 488-byte header object with three backing arrays:

| Array | Slot size | Slots | Total bytes | Purpose |
|-------|-----------|-------|-------------|---------|
| 1 | 64 bytes | 772 | 49,408 | Open-addressing hash (key prefix + metadata) |
| 2 | 36 bytes | 772 | 27,792 | Auxiliary data per mnemonic |
| 3 | 16 bytes | 35 | 560 | Overflow / collision chain |

Array 1 slots are initialized to 0xFF (empty sentinel). The hash function used for lookup is the same FNV-1a variant used by `sub_1377C60` for the primary table.

### Object Tail Configuration

After building the tables and hash structure, the constructor:
1. Queries ~14 knobs via `context+1664` (knobs 1, 2, 5, 11, 14, 18, 22, 25, 28, 273, 774, 775, 803, 983, 998) to conditionally register feature-gated instruction families at `context+1728`
2. Stores knob 803's value at `obj+108`
3. Sets the vtable to `off_21DA9F8` (line 2438 in decompiled source)
4. Writes feature bitmask `0x48018BA65` at `obj+26856`
5. Stores the hash table pointer at `obj+26832` and the arena pointer at `obj+26840`

## Related Pages

- [Instructions & Opcodes](../ir/instructions.md) — Ori IR instruction layout, opcode encoding, full ROT13 table
- [SASS Encoding](../codegen/encoding.md) — Instruction encoding pipeline, format groups, encoder templates
- [Instruction Selection](../codegen/isel.md) — Pattern matching from IR to SASS
- [SM Architecture Map](../targets/index.md) — SM version numbering and feature sets
- [Scheduling](../scheduling/overview.md) — How opcodes are assigned to functional units

## Key Functions

| Address | Size | Role | Confidence |
|---------|------|------|------------|
| `sub_7A5D10` | — | `InstructionInfo` constructor; initializes the 322-entry ROT13 opcode name table at object offset +0x1058 and the 322-entry encoding category identity map at +0x2478 (vtable `off_233ADC0`) | 0.92 |
| `sub_BE7390` | — | Parallel `InstructionInfo` constructor; initializes an identical 322-entry name table | 0.90 |
| `sub_7CB560` | — | SASS printer; maps duplicate opcode indices (e.g., 284 vs 285) to distinct mnemonic strings (`IMNMX` vs `IMNMX.64`) based on operand metadata | 0.85 |
| `sub_6575D0` | 49KB | Register-class-to-opcode dispatch; handles DMMA (index 215) shared dispatch with CVTA at cases 0xD6/0xD7 | 0.85 |
| `sub_7482B0` | — | Encoding path for ISETP (index 288, sm_104); handles case 0x120 for 64-bit integer set-predicate | 0.80 |
| `sub_8380A0` | — | Encoding path for ISETP (index 288, sm_104); second handler for case 0x120 | 0.80 |
| `sub_896D50` | 21KB | Extended mnemonic table constructor; builds the 772-entry alphabetically-sorted SASS mnemonic lookup table at object offset +11360, with parallel 772-entry encoding category map from `unk_21D92E0`, plus 3-array hash table for O(1) string lookup during disassembly parsing (vtable `off_21DA9F8`) | 0.90 |
| `sub_A2B110` | — | Base class constructor shared by both primary (`sub_7A5D10`) and extended (`sub_896D50`) mnemonic table objects | 0.85 |
