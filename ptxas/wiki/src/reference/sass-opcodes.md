# SASS Opcode Catalog

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

Five entries in the table share a SASS mnemonic with an earlier index. These are **not** errors in the table -- they are distinct IR opcodes that happen to produce the same assembly mnemonic but with different binary encodings, operand widths, or functional-unit routing. The duplicates fall into two categories:

**Category A -- SM-generation re-introduction.** The same operation is re-implemented for a newer GPU generation with a different SASS major opcode and encoding path, typically because the tensor core or ALU microarchitecture changed:

| Later Index | Earlier Index | Mnemonic | Why re-introduced |
|-------------|---------------|----------|--------------------|
| 215 (sm_90) | 180 (sm_82) | DMMA | Hopper warpgroup-aware TC path (enc. cat. 515 vs 434) |
| 220 (sm_90) | 14 (sm_70) | FMNMX | Hopper adds 5-entry operand sub-mode table (enc. cat. 534 vs 510) |

**Category B -- Operand-width extension.** Blackwell Ultra (sm_104) adds 64-bit operand variants of existing integer ALU instructions. The SASS printer appends a `.64` suffix at render time; the IR name table stores the same base mnemonic for both widths:

| Later Index | Earlier Index | Mnemonic | What the later index adds |
|-------------|---------------|----------|---------------------------|
| 284 (sm_104) | 37 (sm_70) | IMNMX | 32-bit form, new encoding path |
| 285 (sm_104) | 37 (sm_70) | IMNMX | 64-bit form (`IMNMX.64`, `.64.UI`, `.64.LO`) |
| 288 (sm_104) | 7 (sm_70) | ISETP | 64-bit comparison (`ISETP.64`, `.64.UI`, `.64.LO`) |

Binary evidence: in the constructor `sub_7A5D10`, indices 284 and 285 store identical `"VZAZK"` string pointers at adjacent 16-byte slots (`v2+8728` and `v2+8744`). The SASS printer (`sub_7CB560`) maps them to `IMNMX` vs `IMNMX.64` based on operand metadata.

## Base ISA -- sm_70 (Volta) and Later (Indices 0--135)

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

### Memory -- Load/Store

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

The 0x508 bytes (1288 bytes) at `unk_21C0E00` are **not** additional opcode names. They are a 322-element `int32` array mapping each opcode index to an **encoding category** number -- a level of indirection between opcode indices and binary encoding format descriptors.

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

After matching an input mnemonic string against the ROT13 name table (with inline decoding at lines 264-273), the function reads `encoding_category_map[opcode_index]` and uses the result as a hash key -- combined with a 24-bit architecture discriminator via FNV-1a -- to look up the encoding format descriptor in the hash table at `InstructionInfo+10672`.

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

**256-bit format (format code 0x8):** IMAD.WIDE variants with 16 constant-bank operand slots. Extremely rare -- only 2 encoder functions use this format.

The 64-bit short-form encoders cover 27 opcode classes across 174 encoder functions total. The 128-bit encoders cover the remaining ~75+ opcode classes across 912+ encoder functions.

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

## Related Pages

- [Instructions & Opcodes](../ir/instructions.md) -- Ori IR instruction layout, opcode encoding, full ROT13 table
- [SASS Encoding](../codegen/encoding.md) -- Instruction encoding pipeline, format groups, encoder templates
- [Instruction Selection](../codegen/isel.md) -- Pattern matching from IR to SASS
- [SM Architecture Map](../targets/index.md) -- SM version numbering and feature sets
- [Scheduling](../scheduling/overview.md) -- How opcodes are assigned to functional units

## Key Functions

| Address | Size | Role | Confidence |
|---------|------|------|------------|
| `sub_7A5D10` | -- | `InstructionInfo` constructor; initializes the 322-entry ROT13 opcode name table at object offset +0x1058 and the 322-entry encoding category identity map at +0x2478 (vtable `off_233ADC0`) | 0.92 |
| `sub_BE7390` | -- | Parallel `InstructionInfo` constructor; initializes an identical 322-entry name table | 0.90 |
| `sub_7CB560` | -- | SASS printer; maps duplicate opcode indices (e.g., 284 vs 285) to distinct mnemonic strings (`IMNMX` vs `IMNMX.64`) based on operand metadata | 0.85 |
| `sub_6575D0` | 49KB | Register-class-to-opcode dispatch; handles DMMA (index 215) shared dispatch with CVTA at cases 0xD6/0xD7 | 0.85 |
| `sub_7482B0` | -- | Encoding path for ISETP (index 288, sm_104); handles case 0x120 for 64-bit integer set-predicate | 0.80 |
| `sub_8380A0` | -- | Encoding path for ISETP (index 288, sm_104); second handler for case 0x120 | 0.80 |
