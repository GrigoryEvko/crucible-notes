# SASS Instruction Encoding (128-bit)

> *Recovered from the decoded per-architecture SASS instruction tables in
> nvdisasm V13.1.115 (CUDA 13.1), validated by a parser that processes all 13
> tables cleanly (291,325 bit-field directives, 99.996% exact reconstruction).*

Every post-Volta SASS instruction is a **128-bit word**, even though the decoder
header reports `WORD_SIZE 64` (a generic header artifact present in every table).
The true width is confirmed three ways: the highest referenced bit is 124, the
alternate dot-mask representation is exactly 128 characters, and the relocator
`R_CUDA_INSTRUCTION128` exists. The instruction word splits into a low opcode +
guard region, a middle operand region, and a high scheduling-control word.

## Word layout

```text
 bit 127 .. 125 | 124 ........ 105 | 104 | 103 102 | 101 .. 92 | 91 | 90 ........ 16 | 15 | 14 .. 12 | 11 .. 0
 +-------------+-----------------+-----+---------+-----------+----+--------------+----+---------+--------+
 |  reserved   | CONTROL / SCHED | gap | pm_pred | reserved  |OPC | OPERAND REGION|Pg_ |  Pg     | OPCODE |
 |  (unused)   |     WORD        |     | (perf)  | (unused)  |hi  | regs/imm/mod  |not | guard   | low 12 |
 +-------------+-----------------+-----+---------+-----------+----+--------------+----+---------+--------+
```

- **Opcode** — a 13-bit field split `{bit 91} ++ {bits 11:0}`: the low 12 bits are
  the primary opcode, bit 91 is the single high extension bit. Literals are 10–13
  bits, zero-extended. *Exception:* on **SM75 (Turing) only**, 80 legacy
  control/barrier classes (`BAR`, `BRA`, `BRX`, `BSSY`, `BPT`, `B2R`, `AL2P`) use a
  pure 12-bit opcode with no bit-91 extension.
- **Guard predicate** — `Pg`@`14:12` (3-bit, `P0..PT`) with `Pg_not`@`15` (inversion).
  Present on **every** instruction; the same slot carries `UPg` for uniform-datapath classes.
- **Operand region** — bits `16:91`: register ports, immediates, constant-bank
  addresses, and operand modifiers (below).
- **Control / scheduling word** — bits `102:124`, architecturally stable Turing→Blackwell (below).
- **Never-encoded bits** — `92:101`, `104`, and `125:127` are unused by any field
  across the entire ISA.

## The scheduling-control word (bits 102–124)

| Field | Bits | Width | Role |
|---|---|---:|---|
| `opex` | `124:122, 109:105` | 8 (split) | packs `batch_t` + `usched_info` (stall/yield) + per-operand `.reuse` flags, LUT-compressed via `TABLES_opex_N` |
| `req_bit_set` | `121:116` | 6 | scoreboard **wait mask** — which of 6 dependency barriers to wait on |
| `src_rel_sb` | `115:113` | 3 | read/release scoreboard (variable-latency source barrier; `7` = none) |
| `dst_wr_sb` | `112:110` | 3 | write scoreboard (destination barrier; `7` = none) |
| `pm_pred` | `103:102` | 2 | perf-monitor predicate — **first introduced at SM90** (absent on SM75/SM80) |

`opex`/`req_bit_set`/`src_rel_sb`/`dst_wr_sb` are stable from Turing; only `pm_pred`
is a Hopper-era addition. The dynamic meaning of these fields — coupled vs
decoupled issue, scoreboard signalling, the wait mask — is the
[Execution Model](execution-model.md); this page covers only their bit placement.

> **`.reuse` is not a bit.** Operand-reuse flags live *inside* `opex`, table-compressed
> via `TABLES_opex_N(batch_t, usched_info, reuse_a, reuse_b, reuse_c)`. The variant
> index `N` encodes reuse arity (`opex_4` = 3-source reuse for `IADD3`/`IMAD`/`CLMAD`;
> `opex_2` = 1 reuse; `opex_0/1` = none). Recovering reuse requires inverting the LUT.

## Register file

| File | Range | Encoding |
|---|---|---|
| **GPR** | `R0..R254` + `RZ`(255) | 8-bit, byte-aligned ports (`R254` reserved → `OOR_REG_ERROR`) |
| **Uniform** | `UR0..UR62` + `URZ`(63) | 6-bit, in the **low 6 bits of a GPR byte-slot** (top 2 bits select form) |
| **Predicate** | `P0..P6` + `PT`(7) | 3-bit |
| **Uniform predicate** | `UP0..UP6` + `UPT`(7) | 3-bit |
| **Convergence barrier** | `B0..B63` | 4-bit `barReg` (in `BSSY`/`BMOV`) |
| **Special (S2R)** | `SR0..SR255` (named: `SR_LANEID`, `SR_CLOCK`, `SR_TID`, …) | 8-bit `SRa`@`79:72` |

## Operand ports (SM90 reference)

| Field | Bits | W | Role |
|---|---|---:|---|
| `Rd` | `23:16` | 8 | dest GPR |
| `Ra` | `31:24` | 8 | srcA GPR |
| `Rb` | `39:32` | 8 | srcB GPR |
| `Rc` | `71:64` | 8 | srcC GPR |
| `URd` | `21:16` | 6 | uniform dest (low 6b of `Rd` byte) |
| `Ra_URb` | `37:32` | 6 | srcB as GPR-or-UR (low 6b of `Rb` byte) |
| `Ra_URc` | `69:64` | 6 | srcC as GPR-or-UR (low 6b of `Rc` byte) |
| `Pu` | `83:81` | 3 | dest predicate 1 |
| `Pnz` | `89:87` | 3 | source-predicate slot (carries `Pp`) |
| `cop` | `86:84` | 3 | dest-predicate 2 slot (carries `Pv`) |
| `UPq`/`UPq_not` | `79:77`/`80` | 3/1 | uniform predicate + inversion |
| `SRa` | `79:72` | 8 | special-register index |
| `barReg` | `19:16` | 4 | convergence-barrier descriptor |

The `Ra_URb`/`Ra_URc` ports are a **single physical field that decodes as GPR or
uniform reg** depending on instruction form: register form uses all 8 bits as a GPR;
uniform/const form uses the low 6 as a UR.

> **Decode by bit-range + source, never by slot name.** A `BITS_…_<name>` field's role
> is set by its RHS source operand, not its incidental name: the slot literally named
> `cop`@`86:84` carries the second dest-predicate `Pv` in `ISETP`; `Pnz`@`89:87`
> carries source predicate `Pp`; `input_reg_sz_32_dist`@`90` carries `Pp@not` in `BSSY`.

## Immediates and constant-bank addressing

- **Immediate shapes** (dominant): `Sb_offset`@`53:40` (14-bit const/mem offset, 426
  uses), `Ra_offset`@`63:32` (32-bit, 209 uses) and @`63:40` (24-bit mem offset).
  Split (non-contiguous) immediates exist: `sImm`@`{81:34, 23:16}` (56-bit),
  `uimm8`@`{76:72, 66:64}`.
- **Constant bank** `c[bank][offset]`: `Sb_bank`@`58:54` (5-bit, banks 0–31) +
  `Sb_offset`@`53:40` (14-bit), generated by `ConstBankAddress2` and patched by the
  `R_CUDA_CONST_FIELD*` relocators.
- **PC-relative** branch targets use `SCALE 4` (16-byte instruction alignment) — e.g.
  `BSSY`'s `Sa`@`63:34`.

## Operand modifiers

1-bit flags near each operand's port; the opcode disambiguates **shared** bits:

| Modifier | Bits |
|---|---|
| `@negate` (−x) | 72 (`Ra`), 63 (`Sb`/`Rb`/`URb`/`Rc`), 75 (`Rc`), 84 |
| `@absolute` (\|x\|) | 73 (`Ra`), 62 (`Sb`/`Sc`/`Rc`), 83, 74 |
| `@invert` (~x, int logic) | 75, 72, 63, 30 |
| `@not` (!P, predicate) | 15 (`Pg`), 90 (`Pp`), 71 (`Pr`), 80/27 (`UPq`) |

Bits 63/72/75 are shared between negate / invert / absolute — the opcode selects the meaning.

## Relocators (R_CUDA_*)

A **116-entry** `R_CUDA_*` relocator table, **byte-identical across all 13 arches** —
a stable ELF ABI contract (`ELF_ABI 0x33`, version 7), since relocations are a linker
property, not an arch-encoding one. Families:

- **Absolute** — `R_CUDA_32`/`64` (bit 0), `ABS{16,24,32}_<off>` at 20/23/26/32, `ABS47_34`, plus split `ABS55_16_34` = `{16,8}+{34,47}` and `ABS56_16_34` = `{16,8}+{34,48}`.
- **Constant-bank** — `CONST_FIELD<w>_<off>`: width-19 → class `ConstBankAddress2`, width-21/22 → `ConstBankAddress0` (the two const-bank addressing modes). `CONST_FIELD19_28` = `{28,18}+{26,1}` (split).
- **Descriptor** — `QUERY_DESC21_37`; **function descriptor** `FUNC_DESC*` (tag `fdesc`, LO/HI/byte-lane).
- **Texture/sampler/surface** — `TEX_HEADER_INDEX`, `SAMP_HEADER_INDEX`, `TEX_SLOT`, `SAMP_SLOT`, `SURF_SLOT`, `TEX_BINDLESSOFF13_*`.
- **PC-relative** — `PCREL_IMM24_{23,26}` (24-bit branch displacement).
- **Byte-lane** — `R_CUDA_8_{0..56}` / `G8_*` / `UNIFIED_8_*` (one byte lane each, written at bit 0).
- **Misc** — `UNIFIED*` (tag `unified`), `G32`/`G64`, `YIELD_OPCODE9_0`, `INSTRUCTION64`/`INSTRUCTION128`.

## Encoding oddities (decoder pitfalls)

1. **Destination predicates encode complemented.** Source/guard predicates encode straight (`P0→0 … PT→7`), but dest-predicate ports use the `DestPred` table (`P0→7, P1→6, … PT→0`). Apply the `7−N` complement only to dest-predicate fields.
2. **Header mislabeling.** Every table reports `ARCHITECTURE "Volta"`, `PROCESSOR_ID Volta`, `WORD_SIZE 64` regardless of true arch — a generic decoder-header artifact. True width is 128 bits; true arch is the per-class content.
3. **SM75 dual opcode form** — 80 legacy control/barrier ops use a pure 12-bit opcode (no bit-91 extension).
4. **Zero-width fields** — `BITS_0_Sb=Sb` is a real feature: an operand named in the syntax that contributes 0 encoding bits (implicit/aliased operand).
5. **Bit-name reuse** — the same physical bit serves different roles across families (bit 90 = `input_reg_sz_32_dist` in `IMAD` but `Pp@not` in `BSSY`; bit 63 = negate/invert/immediate by class). No global bit→role map exists.

## Per-arch counts (parser-validated)

| arch | classes | opcode entries | distinct `BITS_*` fields |
|---|---:|---:|---:|
| SM75 | 898 | 1854 | 99 |
| SM80 | 962 | 1942 | 109 |
| SM86/87/88 | 999 | 2018 | 108 |
| SM89 | 1049 | 2118 | 112 |
| SM90/90a | 1168 | 2366 | 115 |
| SM100 | 975 | 2018 | 121 |
| SM103 | 971 | 2010 | 123 |
| SM110 | 901 | 1870 | 118 |
| SM120/121 | 1012 | 2088 | 112 |

## Cross-References

- [Execution Model](execution-model.md) — the dynamic meaning of the control word.
- [Architecture Evolution](architecture-evolution.md) — what the variable opcodes are.
- [Legality & Capabilities](legality-and-capabilities.md) — the constraint rules on these fields.
- [ELF/Cubin Output — Relocations](../output/relocations.md) — the ptxas side of `R_CUDA_*`.
