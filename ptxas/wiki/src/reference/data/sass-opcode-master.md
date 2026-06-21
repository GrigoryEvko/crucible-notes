# SASS Opcode Master Table (322 canonical entries)

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base `0x400000` (non-PIE).*

The canonical per-opcode table that drives SASS encoding. Each row is one Ori IR base opcode
(index 0–321 into the `InstructionInfo` name table). Columns:

- **id** — the 12-bit base opcode (instruction offset +72, masked `0xFFFFCFFF`).
- **mnemonic** — ROT13-decoded SASS name.
- **sm_gen** — the generation that introduced the opcode (boundary markers `SMxx_LAST` delimit the
  ranges; opcodes are only added, never removed).
- **slot** — `encoding_slot`: the index into the per-SM encoder dispatch (`0` = default slot).
- **status** — how the opcode reaches an encoder:
  - `default-slot` — uses encoding slot 0 (the shared default encoder path).
  - `isel-slot` — has a dedicated ISel encoder slot (`encoding_slot > 0`, `< 222`).
  - `no-encoding-entry` — no entry in `opcode_to_encoding`; ≥ 222 opcodes fall through to the
    per-SM handler dispatch instead of a static slot.
  - `sentinel-unencoded` — `encoding_slot == 355`, the not-encodable sentinel (boundary/abstract
    opcodes and arch-gated forms reached only through a later generation's dispatch).
- **pf7x / pf10x** — `pipeline_flags` for the sm_7x and sm_10x scheduling families (blank = no
  flag entry for that family).

`encoding_category` (a parallel 322-entry array copied from `unk_21C0E00`) is an **identity map**
(`category[i] == i`); it carries no payload and is omitted here. `in_opcode_to_encoding` is `yes`
for every row whose status is not `no-encoding-entry`.

| id | mnemonic | sm_gen | slot | status | pf7x | pf10x |
|---|---|---|---|---|---|---|

| 0 | `ERRBAR` | sm_70 | 0 | default-slot | 1 | 1 |
| 1 | `IMAD` | sm_70 | 0 | default-slot | 1 | 1 |
| 2 | `IMAD_WIDE` | sm_70 | 0 | default-slot | 3 | 3 |
| 3 | `IADD3` | sm_70 | 0 | default-slot | 3 | 3 |
| 4 | `BMSK` | sm_70 | 0 | default-slot | 1 | 1 |
| 5 | `SGXT` | sm_70 | 160 | isel-slot | 1 | 1 |
| 6 | `LOP3` | sm_70 | 241 | isel-slot |  |  |
| 7 | `ISETP` | sm_70 | 148 | isel-slot |  |  |
| 8 | `IABS` | sm_70 | 97 | isel-slot | 1 | 1 |
| 9 | `LEA` | sm_70 | 0 | default-slot | 1 | 1 |
| 10 | `SHF` | sm_70 | 93 | isel-slot | 1 |  |
| 11 | `FFMA` | sm_70 | 94 | isel-slot | 1 | 1 |
| 12 | `FADD` | sm_70 | 0 | default-slot |  | 1 |
| 13 | `FMUL` | sm_70 | 95 | isel-slot | 1 | 1 |
| 14 | `FMNMX` | sm_70 | 0 | default-slot | 1 | 1 |
| 15 | `FSWZADD` | sm_70 | 0 | default-slot | 3 | 3 |
| 16 | `FSET` | sm_70 | 0 | default-slot | 0 | 0 |
| 17 | `FSEL` | sm_70 | 0 | default-slot | 1 | 1 |
| 18 | `FSETP` | sm_70 | 0 | default-slot | 1 | 1 |
| 19 | `MOV` | sm_70 | 0 | default-slot | 1 | 1 |
| 20 | `SEL` | sm_70 | 29 | isel-slot | 1 | 1 |
| 21 | `P2R` | sm_70 | 0 | default-slot | 1 |  |
| 22 | `R2P` | sm_70 | 37 | isel-slot | 1 | 1 |
| 23 | `PLOP3` | sm_70 | 0 | default-slot | 1 | 1 |
| 24 | `PRMT` | sm_70 | 188 | isel-slot | 1 | 1 |
| 25 | `NOP` | sm_70 | 190 | isel-slot | 3 | 3 |
| 26 | `VOTE` | sm_70 | 0 | default-slot | 3 |  |
| 27 | `CS2R_32` | sm_70 | 0 | default-slot | 3 |  |
| 28 | `CS2R_64` | sm_70 | 0 | default-slot | 1 | 1 |
| 29 | `PMTRIG` | sm_70 | 32 | isel-slot | 3 | 3 |
| 30 | `CSMTEST` | sm_70 | 271 | isel-slot | 1 | 1 |
| 31 | `VABSDIFF` | sm_70 | 159 | isel-slot | 1 |  |
| 32 | `VABSDIFF4` | sm_70 | 72 | isel-slot | 3 | 3 |
| 33 | `IDP` | sm_70 | 0 | default-slot | 2 | 2 |
| 34 | `IDE` | sm_70 | 55 | isel-slot | 3 | 1 |
| 35 | `I2I` | sm_70 | 42 | isel-slot | 3 |  |
| 36 | `I2IP` | sm_70 | 53 | isel-slot | 1 | 1 |
| 37 | `IMNMX` | sm_70 | 0 | default-slot | 1 |  |
| 38 | `POPC` | sm_70 | 0 | default-slot | 1 | 1 |
| 39 | `FLO` | sm_70 | 0 | default-slot | 4 | 4 |
| 40 | `FCHK` | sm_70 | 355 | sentinel-unencoded |  |  |
| 41 | `IPA` | sm_70 | 0 | default-slot |  |  |
| 42 | `MUFU` | sm_70 | 0 | default-slot |  |  |
| 43 | `F2F` | sm_70 | 0 | default-slot |  |  |
| 44 | `F2F_X` | sm_70 | 0 | default-slot |  |  |
| 45 | `F2I` | sm_70 | 0 | default-slot |  |  |
| 46 | `F2I_X` | sm_70 | 0 | default-slot |  |  |
| 47 | `I2F` | sm_70 | 224 | isel-slot |  |  |
| 48 | `I2F_X` | sm_70 | 235 | isel-slot |  |  |
| 49 | `FRND` | sm_70 | 355 | sentinel-unencoded |  |  |
| 50 | `FRND_X` | sm_70 | 150 | isel-slot |  |  |
| 51 | `AL2P` | sm_70 | 87 | isel-slot |  |  |
| 52 | `AL2P_INDEXED` | sm_70 | 0 | default-slot |  |  |
| 53 | `BREV` | sm_70 | 0 | default-slot |  |  |
| 54 | `BMOV_B` | sm_70 | 0 | default-slot |  |  |
| 55 | `BMOV_R` | sm_70 | 16 | isel-slot |  |  |
| 56 | `BMOV` | sm_70 | 0 | default-slot |  |  |
| 57 | `S2R` | sm_70 | 98 | isel-slot |  |  |
| 58 | `B2R` | sm_70 | 153 | isel-slot |  |  |
| 59 | `R2B` | sm_70 | 183 | isel-slot |  |  |
| 60 | `LEPC` | sm_70 | 288 | isel-slot |  |  |
| 61 | `BAR` | sm_70 | 18 | isel-slot |  |  |
| 62 | `BAR_INDEXED` | sm_70 | 302 | isel-slot |  |  |
| 63 | `SETCTAID` | sm_70 | 303 | isel-slot |  |  |
| 64 | `SETLMEMBASE` | sm_70 | 0 | default-slot |  |  |
| 65 | `GETLMEMBASE` | sm_70 | 248 | isel-slot |  |  |
| 66 | `DEPBAR` | sm_70 | 0 | default-slot |  |  |
| 67 | `BRA` | sm_70 | 0 | default-slot |  |  |
| 68 | `BRX` | sm_70 | 283 | isel-slot |  |  |
| 69 | `JMP` | sm_70 | 164 | isel-slot |  |  |
| 70 | `JMX` | sm_70 | 168 | isel-slot |  |  |
| 71 | `CALL` | sm_70 | 130 | isel-slot |  |  |
| 72 | `RET` | sm_70 | 0 | default-slot |  |  |
| 73 | `BSSY` | sm_70 | 0 | default-slot |  |  |
| 74 | `BREAK` | sm_70 | 12 | isel-slot |  |  |
| 75 | `BPT` | sm_70 | 13 | isel-slot |  |  |
| 76 | `KILL` | sm_70 | 0 | default-slot |  |  |
| 77 | `EXIT` | sm_70 | 172 | isel-slot |  |  |
| 78 | `RTT` | sm_70 | 30 | isel-slot |  |  |
| 79 | `BSYNC` | sm_70 | 0 | default-slot |  |  |
| 80 | `MATCH` | sm_70 | 0 | default-slot |  |  |
| 81 | `NANOSLEEP` | sm_70 | 0 | default-slot |  |  |
| 82 | `NANOTRAP` | sm_70 | 0 | default-slot |  |  |
| 83 | `TEX` | sm_70 | 0 | default-slot |  |  |
| 84 | `TLD` | sm_70 | 86 | isel-slot |  |  |
| 85 | `TLD4` | sm_70 | 88 | isel-slot |  |  |
| 86 | `TMML` | sm_70 | 89 | isel-slot |  |  |
| 87 | `TXD` | sm_70 | 0 | default-slot |  |  |
| 88 | `TXQ` | sm_70 | 187 | isel-slot |  |  |
| 89 | `LDC` | sm_70 | 0 | default-slot |  |  |
| 90 | `ALD` | sm_70 | 0 | default-slot |  |  |
| 91 | `AST` | sm_70 | 0 | default-slot |  |  |
| 92 | `OUT` | sm_70 | 0 | default-slot |  |  |
| 93 | `OUT_FINAL` | sm_70 | 0 | default-slot |  |  |
| 94 | `LDS` | sm_70 | 0 | default-slot |  |  |
| 95 | `STS` | sm_70 | 0 | default-slot |  |  |
| 96 | `LDG` | sm_70 | 0 | default-slot |  |  |
| 97 | `STG` | sm_70 | 0 | default-slot |  |  |
| 98 | `LDL` | sm_70 | 0 | default-slot |  |  |
| 99 | `STL` | sm_70 | 1 | isel-slot |  |  |
| 100 | `LD` | sm_70 | 25 | isel-slot |  |  |
| 101 | `ST` | sm_70 | 33 | isel-slot |  |  |
| 102 | `ATOM` | sm_70 | 38 | isel-slot |  |  |
| 103 | `ATOMG` | sm_70 | 0 | default-slot |  |  |
| 104 | `RED` | sm_70 | 44 | isel-slot |  |  |
| 105 | `ATOMS` | sm_70 | 45 | isel-slot |  |  |
| 106 | `QSPC` | sm_70 | 59 | isel-slot |  |  |
| 107 | `CCTL_NO_SB` | sm_70 | 0 | default-slot |  |  |
| 108 | `CCTL` | sm_70 | 60 | isel-slot |  |  |
| 109 | `CCTLL` | sm_70 | 62 | isel-slot |  |  |
| 110 | `CCTLT` | sm_70 | 68 | isel-slot |  |  |
| 111 | `MEMBAR` | sm_70 | 71 | isel-slot |  |  |
| 112 | `SULD` | sm_70 | 78 | isel-slot |  |  |
| 113 | `SUST` | sm_70 | 79 | isel-slot |  |  |
| 114 | `SUATOM` | sm_70 | 106 | isel-slot |  |  |
| 115 | `SURED` | sm_70 | 0 | default-slot |  |  |
| 116 | `PIXLD` | sm_70 | 0 | default-slot |  |  |
| 117 | `ISBERD` | sm_70 | 0 | default-slot |  |  |
| 118 | `ISBEWR` | sm_70 | 147 | isel-slot |  |  |
| 119 | `SHFL` | sm_70 | 149 | isel-slot |  |  |
| 120 | `WARPSYNC` | sm_70 | 0 | default-slot |  |  |
| 121 | `YIELD` | sm_70 | 0 | default-slot |  |  |
| 122 | `DFMA` | sm_70 | 179 | isel-slot |  |  |
| 123 | `DADD` | sm_70 | 180 | isel-slot |  |  |
| 124 | `DMUL` | sm_70 | 192 | isel-slot |  |  |
| 125 | `DSETP` | sm_70 | 191 | isel-slot |  |  |
| 126 | `HADD2` | sm_70 | 199 | isel-slot |  |  |
| 127 | `HADD2_F32` | sm_70 | 215 | isel-slot |  |  |
| 128 | `HFMA2` | sm_70 | 0 | default-slot |  |  |
| 129 | `HMUL2` | sm_70 | 221 | isel-slot |  |  |
| 130 | `HSET2` | sm_70 | 225 | isel-slot |  |  |
| 131 | `HSETP2` | sm_70 | 2 | isel-slot |  |  |
| 132 | `HMMA_16` | sm_70 | 10 | isel-slot |  |  |
| 133 | `HMMA_32` | sm_70 | 48 | isel-slot |  |  |
| 134 | `IMMA` | sm_70 | 0 | default-slot |  |  |
| 135 | `INTRINSIC` | sm_70 | 0 | default-slot |  |  |
| 136 | `SM70_LAST` | sm_70 | 0 | default-slot |  |  |
| 137 | `SM73_FIRST` | sm_73 | 0 | default-slot |  |  |
| 138 | `UBREV` | sm_73 | 0 | default-slot |  |  |
| 139 | `UBMSK` | sm_73 | 0 | default-slot |  |  |
| 140 | `UCLEA` | sm_73 | 0 | default-slot |  |  |
| 141 | `UISETP` | sm_73 | 120 | isel-slot |  |  |
| 142 | `ULDC` | sm_73 | 126 | isel-slot |  |  |
| 143 | `ULEA` | sm_73 | 129 | isel-slot |  |  |
| 144 | `UP2UR` | sm_73 | 139 | isel-slot |  |  |
| 145 | `ULOP3` | sm_73 | 143 | isel-slot |  |  |
| 146 | `UPLOP3` | sm_73 | 151 | isel-slot |  |  |
| 147 | `USEL` | sm_73 | 163 | isel-slot |  |  |
| 148 | `USGXT` | sm_73 | 0 | default-slot |  |  |
| 149 | `UFLO` | sm_73 | 200 | isel-slot |  |  |
| 150 | `UIADD3` | sm_73 | 201 | isel-slot |  |  |
| 151 | `UIMAD` | sm_73 | 206 | isel-slot |  |  |
| 152 | `UMOV` | sm_73 | 207 | isel-slot |  |  |
| 153 | `UPRMT` | sm_73 | 208 | isel-slot |  |  |
| 154 | `VOTEU` | sm_73 | 213 | isel-slot |  |  |
| 155 | `UPOPC` | sm_73 | 0 | default-slot |  |  |
| 156 | `USHF` | sm_73 | 214 | isel-slot |  |  |
| 157 | `SCATTER` | sm_73 | 0 | default-slot |  |  |
| 158 | `F2FP` | sm_73 | 216 | isel-slot |  |  |
| 159 | `HMMA_1688` | sm_73 | 217 | isel-slot |  |  |
| 160 | `HMMA_16816` | sm_73 | 219 | isel-slot |  |  |
| 161 | `BMMA` | sm_73 | 227 | isel-slot |  |  |
| 162 | `TTUCCTL` | sm_73 | 229 | isel-slot |  |  |
| 163 | `TTUMACRO` | sm_73 | 290 | isel-slot |  |  |
| 164 | `R2UR` | sm_73 | 7 | isel-slot |  |  |
| 165 | `MOVM` | sm_73 | 0 | default-slot |  |  |
| 166 | `LDSM` | sm_73 | 0 | default-slot |  |  |
| 167 | `LDTRAM` | sm_73 | 0 | default-slot |  |  |
| 168 | `FOOTPRINT` | sm_73 | 36 | isel-slot |  |  |
| 169 | `S2UR` | sm_73 | 355 | sentinel-unencoded |  |  |
| 170 | `BRXU` | sm_73 | 0 | default-slot |  |  |
| 171 | `SM73_LAST` | sm_73 | 0 | default-slot |  |  |
| 172 | `SM82_FIRST` | sm_82 | 110 | isel-slot |  |  |
| 173 | `GATHER` | sm_82 | 115 | isel-slot |  |  |
| 174 | `GENMETADATA` | sm_82 | 114 | isel-slot |  |  |
| 175 | `SPMETADATA` | sm_82 | 117 | isel-slot |  |  |
| 176 | `BMMA_88128` | sm_82 | 196 | isel-slot |  |  |
| 177 | `BMMA_168128` | sm_82 | 254 | isel-slot |  |  |
| 178 | `BMMA_168256` | sm_82 | 255 | isel-slot |  |  |
| 179 | `CLMAD` | sm_82 | 256 | isel-slot |  |  |
| 180 | `DMMA` | sm_82 | 257 | isel-slot |  |  |
| 181 | `HMMA_SP_1688` | sm_82 | 258 | isel-slot |  |  |
| 182 | `HFMA2_MMA` | sm_82 | 259 | isel-slot |  |  |
| 183 | `HMNMX2` | sm_82 | 260 | isel-slot |  |  |
| 184 | `IMMA_88` | sm_82 | 261 | isel-slot |  |  |
| 185 | `IMMA_SP_88` | sm_82 | 0 | default-slot |  |  |
| 186 | `IMMA_16816` | sm_82 | 0 | default-slot |  |  |
| 187 | `IMMA_16832` | sm_82 | 262 | isel-slot |  |  |
| 188 | `IMMA_SP_16832` | sm_82 | 243 | isel-slot |  |  |
| 189 | `ARRIVES` | sm_82 | 0 | default-slot |  |  |
| 190 | `LDGDEPBAR` | sm_82 | 0 | default-slot |  |  |
| 191 | `LDGSTS` | sm_82 | 0 | default-slot |  |  |
| 192 | `REDUX` | sm_82 | 0 | default-slot |  |  |
| 193 | `SM82_LAST` | sm_82 | 0 | default-slot |  |  |
| 194 | `SM86_FIRST` | sm_86 | 0 | default-slot |  |  |
| 195 | `F2IP` | sm_86 | 0 | default-slot |  |  |
| 196 | `UF2FP` | sm_86 | 0 | default-slot |  |  |
| 197 | `I2FP` | sm_86 | 0 | default-slot |  |  |
| 198 | `SUQUERY` | sm_86 | 0 | default-slot |  |  |
| 199 | `SM86_LAST` | sm_86 | 0 | default-slot |  |  |
| 200 | `SM89_FIRST` | sm_89 | 0 | default-slot |  |  |
| 201 | `QMMA_16816` | sm_89 | 0 | default-slot |  |  |
| 202 | `QMMA_16832` | sm_89 | 96 | isel-slot |  |  |
| 203 | `QMMA_SP_16832` | sm_89 | 0 | default-slot |  |  |
| 204 | `QMMA_SP_12864` | sm_89 | 169 | isel-slot |  |  |
| 205 | `SM89_LAST` | sm_89 | 178 | isel-slot |  |  |
| 206 | `SM90_FIRST` | sm_90 | 197 | isel-slot |  |  |
| 207 | `ACQBLK` | sm_90 | 239 | isel-slot |  |  |
| 208 | `CGABAR_ARV` | sm_90 | 154 | isel-slot |  |  |
| 209 | `CGABAR_GET` | sm_90 | 0 | default-slot |  |  |
| 210 | `CGABAR_SET` | sm_90 | 195 | isel-slot |  |  |
| 211 | `CGABAR_WAIT` | sm_90 | 175 | isel-slot |  |  |
| 212 | `CGAERRBAR` | sm_90 | 355 | sentinel-unencoded |  |  |
| 213 | `CREATEPOLICY` | sm_90 | 355 | sentinel-unencoded |  |  |
| 214 | `CVTA` | sm_90 | 355 | sentinel-unencoded |  |  |
| 215 | `DMMA` | sm_90 | 355 | sentinel-unencoded |  |  |
| 216 | `ELECT` | sm_90 | 355 | sentinel-unencoded |  |  |
| 217 | `ENDCOLLECTIVE` | sm_90 | 309 | isel-slot |  |  |
| 218 | `FENCE_G` | sm_90 | 170 | isel-slot |  |  |
| 219 | `FENCE_S` | sm_90 | 355 | sentinel-unencoded |  |  |
| 220 | `FMNMX` | sm_90 | 355 | sentinel-unencoded |  |  |
| 221 | `GMMA` | sm_90 | 0 | default-slot |  |  |
| 222 | `LDCU` | sm_90 | — | no-encoding-entry |  |  |
| 223 | `LEPC` | sm_90 | — | no-encoding-entry |  |  |
| 224 | `MAPA` | sm_90 | — | no-encoding-entry |  |  |
| 225 | `PREEXIT` | sm_90 | — | no-encoding-entry |  |  |
| 226 | `R2UR_H` | sm_90 | — | no-encoding-entry |  |  |
| 227 | `REDAS` | sm_90 | — | no-encoding-entry |  |  |
| 228 | `SETMAXREG` | sm_90 | — | no-encoding-entry |  |  |
| 229 | `SETSMEMSIZE` | sm_90 | — | no-encoding-entry |  |  |
| 230 | `STAS` | sm_90 | — | no-encoding-entry |  |  |
| 231 | `STSM` | sm_90 | — | no-encoding-entry |  |  |
| 232 | `SYNCS_BASIC` | sm_90 | — | no-encoding-entry |  |  |
| 233 | `SYNCS_LD_UNIFM` | sm_90 | — | no-encoding-entry |  |  |
| 234 | `UBLKCP` | sm_90 | — | no-encoding-entry |  |  |
| 235 | `UBLKRED` | sm_90 | — | no-encoding-entry |  |  |
| 236 | `UBLKPF` | sm_90 | — | no-encoding-entry |  |  |
| 237 | `UCVTA` | sm_90 | — | no-encoding-entry |  |  |
| 238 | `ULEPC` | sm_90 | — | no-encoding-entry |  |  |
| 239 | `UMAPA` | sm_90 | — | no-encoding-entry |  |  |
| 240 | `UTMACCTL` | sm_90 | — | no-encoding-entry |  |  |
| 241 | `UTMACMDFLUSH` | sm_90 | — | no-encoding-entry |  |  |
| 242 | `UTMALDG` | sm_90 | — | no-encoding-entry |  |  |
| 243 | `UTMAPF` | sm_90 | — | no-encoding-entry |  |  |
| 244 | `UTMREDG` | sm_90 | — | no-encoding-entry |  |  |
| 245 | `UTMALST` | sm_90 | — | no-encoding-entry |  |  |
| 246 | `VHMNMX` | sm_90 | — | no-encoding-entry |  |  |
| 247 | `VIADD` | sm_90 | — | no-encoding-entry |  |  |
| 248 | `VIADDMNMX` | sm_90 | — | no-encoding-entry |  |  |
| 249 | `VIMNMX` | sm_90 | — | no-encoding-entry |  |  |
| 250 | `VIMNMX3` | sm_90 | — | no-encoding-entry |  |  |
| 251 | `WARPGROUP` | sm_90 | — | no-encoding-entry |  |  |
| 252 | `SM90_LAST` | sm_90 | — | no-encoding-entry |  |  |
| 253 | `SM100_FIRST` | sm_100 | — | no-encoding-entry |  |  |
| 254 | `CREDUX` | sm_100 | — | no-encoding-entry |  |  |
| 255 | `FADD2` | sm_100 | — | no-encoding-entry |  |  |
| 256 | `FFMA2` | sm_100 | — | no-encoding-entry |  |  |
| 257 | `FMNMX3` | sm_100 | — | no-encoding-entry |  |  |
| 258 | `FMUL2` | sm_100 | — | no-encoding-entry |  |  |
| 259 | `LDTM` | sm_100 | — | no-encoding-entry |  |  |
| 260 | `UGETNEXTWORKID` | sm_100 | — | no-encoding-entry |  |  |
| 261 | `UTCBAR_1CTA` | sm_100 | — | no-encoding-entry |  |  |
| 262 | `UTCBAR_2CTA` | sm_100 | — | no-encoding-entry |  |  |
| 263 | `UTCCP_1CTA` | sm_100 | — | no-encoding-entry |  |  |
| 264 | `UTCCP_2CTA` | sm_100 | — | no-encoding-entry |  |  |
| 265 | `UTCMMA_1CTA` | sm_100 | — | no-encoding-entry |  |  |
| 266 | `UTCMMA_2CTA` | sm_100 | — | no-encoding-entry |  |  |
| 267 | `UTCSHIFT_1CTA` | sm_100 | — | no-encoding-entry |  |  |
| 268 | `UTCSHIFT_2CTA` | sm_100 | — | no-encoding-entry |  |  |
| 269 | `VIRTCOUNT` | sm_100 | — | no-encoding-entry |  |  |
| 270 | `TCATOMSWS` | sm_100 | — | no-encoding-entry |  |  |
| 271 | `TCLDSWS` | sm_100 | — | no-encoding-entry |  |  |
| 272 | `TCSTSWS` | sm_100 | — | no-encoding-entry |  |  |
| 273 | `QFMA4` | sm_100 | — | no-encoding-entry |  |  |
| 274 | `QADD4` | sm_100 | — | no-encoding-entry |  |  |
| 275 | `QMUL4` | sm_100 | — | no-encoding-entry |  |  |
| 276 | `MEMSET` | sm_100 | — | no-encoding-entry |  |  |
| 277 | `ACQSHMINIT` | sm_100 | — | no-encoding-entry |  |  |
| 278 | `STTM` | sm_100 | — | no-encoding-entry |  |  |
| 279 | `FENCE_T` | sm_100 | — | no-encoding-entry |  |  |
| 280 | `SM100_LAST` | sm_100 | — | no-encoding-entry |  |  |
| 281 | `SM104_FIRST` | sm_104 | — | no-encoding-entry |  |  |
| 282 | `IADD` | sm_104 | — | no-encoding-entry |  |  |
| 283 | `UVIADD` | sm_104 | — | no-encoding-entry |  |  |
| 284 | `IMNMX` | sm_104 | — | no-encoding-entry |  |  |
| 285 | `IMNMX` | sm_104 | — | no-encoding-entry |  |  |
| 286 | `UIMNMX` | sm_104 | — | no-encoding-entry |  |  |
| 287 | `UVIMNMX` | sm_104 | — | no-encoding-entry |  |  |
| 288 | `ISETP` | sm_104 | — | no-encoding-entry |  |  |
| 289 | `UISETP` | sm_104 | — | no-encoding-entry |  |  |
| 290 | `MOV` | sm_104 | — | no-encoding-entry |  |  |
| 291 | `UMOV` | sm_104 | — | no-encoding-entry |  |  |
| 292 | `SEL` | sm_104 | — | no-encoding-entry |  |  |
| 293 | `USEL` | sm_104 | — | no-encoding-entry |  |  |
| 294 | `UFADD` | sm_104 | — | no-encoding-entry |  |  |
| 295 | `UFSEL` | sm_104 | — | no-encoding-entry |  |  |
| 296 | `UFFMA` | sm_104 | — | no-encoding-entry |  |  |
| 297 | `UFMUL` | sm_104 | — | no-encoding-entry |  |  |
| 298 | `UFSET` | sm_104 | — | no-encoding-entry |  |  |
| 299 | `UFSETP` | sm_104 | — | no-encoding-entry |  |  |
| 300 | `UI2I` | sm_104 | — | no-encoding-entry |  |  |
| 301 | `UI2IP` | sm_104 | — | no-encoding-entry |  |  |
| 302 | `UF2F` | sm_104 | — | no-encoding-entry |  |  |
| 303 | `UFRND` | sm_104 | — | no-encoding-entry |  |  |
| 304 | `UF2I` | sm_104 | — | no-encoding-entry |  |  |
| 305 | `UF2IP` | sm_104 | — | no-encoding-entry |  |  |
| 306 | `UI2F` | sm_104 | — | no-encoding-entry |  |  |
| 307 | `UI2FP` | sm_104 | — | no-encoding-entry |  |  |
| 308 | `UIABS` | sm_104 | — | no-encoding-entry |  |  |
| 309 | `CS2UR` | sm_104 | — | no-encoding-entry |  |  |
| 310 | `UF2FP` | sm_104 | — | no-encoding-entry |  |  |
| 311 | `MXQMMA_SF_16832` | sm_104 | — | no-encoding-entry |  |  |
| 312 | `OMMA_16864` | sm_104 | — | no-encoding-entry |  |  |
| 313 | `OMMA_SP_168128` | sm_104 | — | no-encoding-entry |  |  |
| 314 | `QMMA_16816` | sm_104 | — | no-encoding-entry |  |  |
| 315 | `QMMA_16832` | sm_104 | — | no-encoding-entry |  |  |
| 316 | `QMMA_SP_16832` | sm_104 | — | no-encoding-entry |  |  |
| 317 | `QMMA_SP_12864` | sm_104 | — | no-encoding-entry |  |  |
| 318 | `QMMA_SF_16832` | sm_104 | — | no-encoding-entry |  |  |
| 319 | `QMMA_SF_SP_16864` | sm_104 | — | no-encoding-entry |  |  |
| 320 | `SM104_LAST` | sm_104 | — | no-encoding-entry |  |  |
| 321 | `LAST` | sentinel | — | no-encoding-entry |  |  |
