# FP8 and dtype Encoding

> *All addresses, offsets, struct ordinals, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce` (runtime-lib **2.31.24.0-0b044f4ce**; `libnrt.so.2.31.24.0`, ELF64, **not stripped**, DWARF present, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). `.text` VMA equals file offset for the cited ranges. Enum values are read from DWARF (`enums.json`), struct offsets from DWARF (`structures.json`), and the encode/validate bodies from the disassembly. Other versions will differ.*

## Abstract

The TPB ISA carries **three 8-bit floating-point sub-formats** in the dtype field of a [64-byte instruction record](instruction-record.md): `FP8_EXP3`, `FP8_EXP4`, and `FP8_EXP5` — equivalently the E3M4 (3 exponent bits), E4M3 (4 exponent bits), and E5M2 (5 exponent bits) members of the OCP-style FP8 family. The dtype field is *not* a width or a format descriptor; it is a small enum (`NEURON_ISA_TPB_DTYPE`, `uint8`) whose value selects one of 16 base types — and the FP8 sub-formats occupy values `13/14/15`, deliberately the **top of the 4-bit base-type space**. The exponent/mantissa split is implied entirely by that enum value: the instruction word never carries an explicit exponent-width or bias field. A reimplementer who treats the dtype byte as a "format code" rather than an index into a fixed 16-entry table will mis-decode every FP8 operand.

The critical, easily-missed fact is that **two unrelated dtype encodings coexist in this one binary**. The KBIN container and the public NRT API use a *sequential* numbering where `FP8_E3=1, FP8_E4=2, FP8_E5=3` (`kbin_dtype` / `nrt_dtype`); the ISA instruction word uses a *completely different* numbering where the same three formats are `FP8_EXP3=13, FP8_EXP4=14, FP8_EXP5=15` (`NEURON_ISA_TPB_DTYPE`). `FP32R` is `6` in the KBIN map but `11` in the ISA map; `UINT64` is `11` in KBIN but `1` in the ISA. The kbin→ISA remap is therefore a real table, not an identity. This page owns the **ISA-word** encoding (the bits the sequencer actually fetches); the KBIN-level enum and its semantics live on [The dtype System](../neff/dtype-system.md).

FP8 is **arch-gated** at two distinct layers. First, *enablement* of OCP FP8 conversion in the hardware data path is programmed at model-load time by a single encoder, `insert_set_fp8_conv_config` (`@0x27d5b0`), which emits CSR-write records into a model preamble — and which `__assert_fail`s unless `tdrv_arch_instr_block_supports_fp8_conv_config()` returns true (a per-arch capability bit). Second, the FP4 extension (`FP4_EXP2=16`) and the 7 compressed `CPTC` types (`25..31`) are accepted only by a separate, newer-silicon validator variant (`dbg_is_valid_dtype_inc_fp4`, present only in the v4/MARIANA band) — the base `NEURON_ISA_TPB_DTYPE` enum-membership test is a flat `value <= 0xF`, which structurally rejects FP4 and CPTC. This page documents the FP8 format table, the dtype-field bit encoding, the encoder that gates FP8 enablement, and the validator gates that police dtype legality.

For reimplementation, the contract is:

- **The FP8 format table** — three sub-formats (E3M4 / E4M3 / E5M2), their exponent/mantissa split, bias, and the fact that the special-value handling (saturation vs. non-saturation, and the per-format `emax`) is *runtime-configurable*, not fixed by the format.
- **The ISA dtype-field encoding** — `NEURON_ISA_TPB_DTYPE` (`uint8`) is a 16-value table; FP8 = `{13,14,15}`, FP4 = `16`, CPTC = `25..31`; membership for the base path is `value <= 15`.
- **The two-encoding split** — the ISA-word numbering is *not* the KBIN/NRT numbering; a kbin→ISA dtype remap is mandatory at NEFF build time.
- **The FP8 enablement encoder** — `insert_set_fp8_conv_config` reads a 16-byte `kbin_fp8_conv_cfg_t` and emits per-engine CSR writes plus `0x40`-byte dummy-read records into the preamble, gated by a per-arch capability bit.
- **The dtype legality gates** — `INVALID=0`, `UINT64=1` (gated by `allow_u64`), `FP32R=11` (gated by `allow_fp32r`), `INT64=12` (gated by `allow_i64`) are conditionally illegal; everything else `<=15` is a valid base dtype.

| | |
|---|---|
| **ISA dtype enum** | `NEURON_ISA_TPB_DTYPE` (DWARF, `uint8`) — 16 base values + FP4(16) + CPTC(25..31) |
| **FP8 sub-formats** | `FP8_EXP3=13` (E3M4), `FP8_EXP4=14` (E4M3), `FP8_EXP5=15` (E5M2) |
| **KBIN/NRT FP8 values** | `FP8_E3=1`, `FP8_E4=2`, `FP8_E5=3` (`kbin_dtype` / `nrt_dtype`) — **different numbering** |
| **Base enum-membership** | `is_valid_enum(DTYPE, v)` = `v <= 0xF` (`@0x27b650`, case `DTYPE`) |
| **Silent dtype gate** | `is_valid_dtype` `@0x27b910` (46 B) — INVALID/UINT64/INT64/FP32R gates + membership |
| **Debug dtype gate** | `dbg_is_valid_dtype` `@0x27f940` (2233 B, 153 callers) — same logic + named asserts |
| **FP4-aware variant** | `dbg_is_valid_dtype_inc_fp4` `@0x3b3b10` (v4/MARIANA band only) — adds `allow_fp4` |
| **FP8 enablement encoder** | `insert_set_fp8_conv_config` `@0x27d5b0` (1138 B) — per-engine CSR writes |
| **FP8 arch gate** | `tdrv_arch_instr_block_supports_fp8_conv_config` `@0x30a290` — per-arch capability bit |
| **FP8 config struct** | `kbin_fp8_conv_cfg_t` (16 B): `fp8_range_extension`, `emax_fp8_e4m3`, `emax_fp8_e5m2`, `ocp_sat`, `sis`, `sns` |
| **Source TU** | `tdrv/instruction_block_sunda.c:0x284` (encoder); `aws_neuron_isa_tpb_assert.h` (validators, generated) |

---

## 1. The FP8 Format Table

### Purpose

The dtype field selects a numeric format, and for the three FP8 values the format is an 8-bit float with a fixed exponent/mantissa split. A reimplementer encoding or decoding an FP8 tensor must know the bit budget for each sub-format and — the part the ISA does *not* hard-wire — that the dynamic range (`emax`) and the overflow behavior (saturation) are programmed separately, per engine, by the FP8 enablement encoder ([§3](#3-the-fp8-enablement-encoder)). The format names in the ISA enum are `EXP3/EXP4/EXP5`, naming the **exponent width**, while the public-facing names (`E3/E4/E5`) name the same widths.

### Encoding

The three FP8 sub-formats partition the 8 bits as 1 sign bit + E exponent bits + M mantissa bits, `E+M=7`. The bias follows the IEEE-style `2^(E-1) - 1` convention. The "special-value handling" column is the part that diverges from IEEE and from naive FP8: the saturation mode (`ocp_sat`) and the per-format maximum exponent (`emax_fp8_e4m3`, `emax_fp8_e5m2` in `kbin_fp8_conv_cfg_t`) are *configuration inputs*, not properties baked into the format — the same `FP8_EXP4` operand behaves as OCP-saturating or non-saturating depending on what the preamble programmed.

| Format (ISA name) | Public name | Sign | Exp bits | Mantissa bits | Bias | Special-value handling | Confidence |
|---|---|---|---|---|---|---|---|
| `FP8_EXP3` (`=13`) | E3M4 | 1 | 3 | 4 | 3 | narrowest range, finest precision of the three; saturation per `ocp_sat` | HIGH |
| `FP8_EXP4` (`=14`) | E4M3 | 1 | 4 | 3 | 7 | OCP-style: no Inf; NaN = one bit-pattern; range set by `emax_fp8_e4m3` + `ocp_sat` | HIGH |
| `FP8_EXP5` (`=15`) | E5M2 | 1 | 5 | 2 | 15 | IEEE-like: Inf/NaN representable; widest range, coarsest precision | HIGH |

> **QUIRK —** the ISA enum names FP8 by **exponent width** (`EXP3/4/5`), and the value ordering is ascending-exponent: value `13+k` has `3+k` exponent bits, so `FP8_EXP3=13` is the widest-mantissa format (E3M4) and `FP8_EXP5=15` is the widest-exponent format (E5M2). A reimplementer must not confuse the public `E4` (which means E4M3) with the ISA `EXP4` (also E4M3) versus the *KBIN* `FP8_E4=2` — three names, two numbering systems, one format.

> **GOTCHA —** the per-format `emax` and the `ocp_sat`/`fp8_range_extension` knobs mean two records with identical `FP8_EXP4` dtype bytes can decode to *different* numeric values if they ran under different preambles. The dtype byte fixes the bit layout (E4M3); it does **not** fix the dynamic range. The range is established once, at model load, by [`insert_set_fp8_conv_config`](#3-the-fp8-enablement-encoder), and applies to every FP8 op on that engine until reprogrammed. There is no per-instruction range override.

---

## 2. The dtype-Field Encoding

### Purpose

Every record that carries a numeric operand has one or more dtype bytes (e.g. `in_dtype`/`out_dtype` in the tensor-scalar and DMA arms documented on the [record format](instruction-record.md) page). Those bytes are `NEURON_ISA_TPB_DTYPE` enum values. Pinning the full value→format map, and the membership rule the validator enforces, lets a reimplementer both emit and check any dtype field.

### Encoding

The 16 base values plus the FP4 and CPTC extensions, decoded from the DWARF `NEURON_ISA_TPB_DTYPE` enum. The "width" column is the operand byte-width the type implies (used by `type_size_check` elsewhere); the "Confidence" column flags the extension rows, which only the newer-arch validator accepts.

| Value | `NEURON_ISA_TPB_DTYPE` | Format | Width | Confidence |
|---|---|---|---|---|
| `0` | `INVALID` | — (rejected by every gate) | — | HIGH |
| `1` | `UINT64` | unsigned 64-bit int | 8 | HIGH |
| `2` | `INT8` | signed 8-bit int | 1 | HIGH |
| `3` | `UINT8` | unsigned 8-bit int | 1 | HIGH |
| `4` | `INT16` | signed 16-bit int | 2 | HIGH |
| `5` | `UINT16` | unsigned 16-bit int | 2 | HIGH |
| `6` | `BFLOAT16` | bf16 (E8M7) | 2 | HIGH |
| `7` | `FP16` | IEEE half (E5M10) | 2 | HIGH |
| `8` | `INT32` | signed 32-bit int | 4 | HIGH |
| `9` | `UINT32` | unsigned 32-bit int | 4 | HIGH |
| `10` | `FP32` | IEEE single (E8M23) | 4 | HIGH |
| `11` | `FP32R` | FP32 "round"/reduced variant | 4 | HIGH |
| `12` | `INT64` | signed 64-bit int | 8 | HIGH |
| `13` | `FP8_EXP3` | **FP8 E3M4** | 1 | HIGH |
| `14` | `FP8_EXP4` | **FP8 E4M3** | 1 | HIGH |
| `15` | `FP8_EXP5` | **FP8 E5M2** | 1 | HIGH |
| `16` | `FP4_EXP2` | FP4 E2M1 (extension) | — | MEDIUM (v4/MARIANA validator only) |
| `25..31` | `CPTC1..CPTC7` | compressed tensor codecs | — | MEDIUM (out of base `<=15` range) |

The base validator's enum-membership test (`is_valid_enum` case `DTYPE`, `@0x27b650`) is the single line that defines "is this byte a legal dtype":

```c
// is_valid_enum @0x27b650, case NEURON_ISA_TPB_ENUM_LIST_DTYPE:
case DTYPE:
    return value <= 0xF;     // ONLY 0..15 are base-legal; FP4(16)/CPTC(25..31) excluded here
```

> **QUIRK —** the base dtype membership is a bare `value <= 0xF`, so `FP4_EXP2=16` and `CPTC1..7=25..31` are **not** accepted on the base path even though they are defined enumerators. They are gated into a *different* enum list / validator variant (`dbg_is_valid_dtype_inc_fp4`, the `_inc_fp4` clone that takes an `allow_fp4` parameter and exists only in the v4/MARIANA `0x3b3xxx` band). A reimplementer keying dtype legality off "is it a named enumerator" will wrongly accept FP4 on V2/V3 silicon; the real rule is "`<=15` on the base path, FP4/CPTC only when the arch-specific extended validator is selected." This mirrors the three-clone validator templating documented on [Per-Arch Validators](validators-per-arch.md).

> **CORRECTION (ISA-FP8-1) —** the FP8 dtype values are **`13/14/15`** in the instruction word (`NEURON_ISA_TPB_DTYPE`), not `1/2/3`. The `1/2/3` numbering (`FP8_E3/E4/E5`) belongs to `kbin_dtype` / `nrt_dtype` — the KBIN container and public API. An early reading that "the dtype field encodes FP8 as 1/2/3" conflated the two enums; in the ISA word, `1`=`UINT64` and `2`=`INT8`. The NEFF builder must remap kbin→ISA, not pass the kbin byte through.

### The two-encoding remap

The same three physical formats carry different enum values across the stack. A reimplementer building the kbin→ISA lowering reproduces this table (decoded from `kbin_dtype` and `NEURON_ISA_TPB_DTYPE`):

| Format | `kbin_dtype` / `nrt_dtype` | `NEURON_ISA_TPB_DTYPE` (ISA word) | `SDMA_DTYPE` |
|---|---|---|---|
| FP8 E3M4 | `FP8_E3 = 1` | `FP8_EXP3 = 13` | `FP8_E3 = 13` |
| FP8 E4M3 | `FP8_E4 = 2` | `FP8_EXP4 = 14` | `FP8_E4 = 14` |
| FP8 E5M2 | `FP8_E5 = 3` | `FP8_EXP5 = 15` | `FP8_E5 = 15` |
| FP16 | `FP16 = 4` | `7` | `7` |
| FP32 | `FP32 = 5` | `10` | `10` |
| FP32R | `FP32R = 6` | `FP32R = 11` | `FP32R = 11` |
| UINT64 | `UINT64 = 11` | `UINT64 = 1` | `UINT64 = 1` |

> **NOTE —** `SDMA_DTYPE` (the DMA-engine dtype enum) shares the *ISA* numbering, not the kbin one: `FP8_E3/E4/E5 = 13/14/15`, `UINT64=1`, `FP32R/RESERVED=11`. So the DMA path and the compute path agree on the wire encoding, and only the KBIN/NRT container layer uses the sequential `1/2/3` form. The remap is a single table lookup at NEFF build time; the *values* are identical across SUNDA/CAYMAN/MARIANA — only the *legality* of FP4/CPTC varies by arch.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `NEURON_ISA_TPB_DTYPE` | DWARF enum (`uint8`) | the ISA-word dtype value space (16 base + FP4 + CPTC) | CERTAIN |
| `kbin_dtype` / `nrt_dtype` | DWARF enum | KBIN/NRT sequential dtype numbering (FP8=1/2/3) | CERTAIN |
| `SDMA_DTYPE` | DWARF enum | DMA-engine dtype; shares ISA numbering | CERTAIN |
| `is_valid_enum` (case `DTYPE`) | `0x27b650` | base membership: `value <= 0xF` | HIGH |

---

## 3. The FP8 Enablement Encoder

### Purpose

The dtype byte tells an instruction *which* FP8 format to use, but the hardware data-conversion path must be *programmed* to honor it: the per-engine FP8 control CSRs (range extension, OCP saturation, the DVE sequencer `emax`, and the per-DMA OCP/non-OCP conversion registers) carry the dynamic-range and saturation policy. `insert_set_fp8_conv_config` (`@0x27d5b0`) is the one encoder that emits those CSR writes — as ordinary `ctrl_wr` (CSR-write) instruction records appended to a model **preamble** buffer, so the writes execute before any inference op. It is the bridge between the `kbin_fp8_conv_cfg_t` policy and the device registers, and it is where FP8 enablement is arch-gated.

### Entry Point

```text
ib_create_one_block (0x2f7e50) / ib_add_inference_wait_v2 (0x2f6e20)
  └─ insert_set_fp8_conv_config (0x27d5b0, 1138 B)
       ├─ tdrv_arch_instr_block_supports_fp8_conv_config (0x30a290)  ── ASSERT arch gate
       ├─ aws_hal_get_eng_fp8_cfg_offset / _dve_sequencer_emax_cfg_offset / _sdma_*_cfg_offset
       ├─ aws_hal_mariana_tpb_fp8_control_reg_value (0x4681a0)        ── pack control CSR
       ├─ aws_hal_mariana_tpb_fp8_dve_emax_reg_value (0x468230)       ── pack DVE emax CSR
       ├─ aws_hal_mariana_sdma_get_fp8_ocp_control_reg_value_for_config (0x465080)
       ├─ aws_hal_mariana_sdma_get_non_ocp_control_reg_value_for_config (0x4650b0)
       ├─ add_wr32 (0x2733d0)   ── emit one ctrl_wr (opcode 0xA5) record
       └─ buf_append (0x265e80) ── grow the preamble buffer
```

### Algorithm

```c
// Models insert_set_fp8_conv_config @0x27d5b0 (1138 B).
// config = kbin_fp8_conv_cfg_t* (16 B); eng = kbin_engine_t* (eng_type 0..4); preamble = buf_t*.
// Emits ctrl_wr (CSR-write) records into the preamble; arch-gated.
NRT_STATUS insert_set_fp8_conv_config(config, pcore, eng, preamble):
    // (0) ARCH GATE — abort the load if this silicon has no FP8 conv path
    if !tdrv_arch_instr_block_supports_fp8_conv_config():            // 0x30a290: per-arch cap bit
        __assert_fail("...", "instruction_block_sunda.c", 0x284)     // hard fail at build time

    // (1) engines past the 4 compute engines need no TPB programming
    if eng->eng_type > 3:                                            // >3 == SP(4)/beyond
        log("Engine %s does not require TPB programming for OCP enablement")
        return NRT_SUCCESS

    apb     = aws_get_apb_base(pcore)
    tpb_csr = aws_get_tpb_csr_apb_offset(pcore, 0) + apb             // CSR window base

    // (2) ALWAYS: the per-engine FP8 control register (range-ext, ocp_sat, sis, sns)
    eng_off  = aws_hal_get_eng_fp8_cfg_offset(eng->eng_type)         // 0x44c590
    ctrl_val = aws_hal_mariana_tpb_fp8_control_reg_value(            // 0x4681a0
                   eng->eng_type,
                   config->fp8_range_extension, config->ocp_sat,
                   config->sis, config->sns)
    off = add_wr32(instr, 0, tpb_csr + eng_off, ctrl_val, NULL)      // emit ctrl_wr (0xA5)

    // (3) DVE engine (eng_type == 3): also program the sequencer emax register,
    //     carrying the per-format max-exponent for E4M3 and E5M2
    if eng->eng_type == AL_HAL_TPB_ENG_DVE /* 3 */:
        emax_off = aws_hal_get_dve_sequencer_emax_cfg_offset()       // 0x44c6e0
        emax_val = aws_hal_mariana_tpb_fp8_dve_emax_reg_value(       // 0x468230
                       config->emax_fp8_e4m3, config->emax_fp8_e5m2)
        off = add_wr32(instr, off, tpb_csr + emax_off, emax_val, NULL)

    // (4) PE engine (eng_type == 0): program EVERY DMA engine's data-conversion regs,
    //     4 registers each (OCP control x2 selectors, non-OCP control x2 selectors)
    if eng->eng_type == AL_HAL_TPB_ENG_PE /* 0 */:
        dma_base = aws_get_apb_dma_base(pcore)
        cce_user = aws_hal_get_sdma_cce_user_offset() + dma_base
        for dma in 0 .. tdrv_arch_get_num_dma_per_tpb():             // 0x308f50: per-arch DMA count
            misc = get_dma_eng_sdma_misc_relbase_v2(pcore, dma) + cce_user
            // OCP conversion control, selector 1 then 0
            for sel in {1, 0}:
                v = aws_hal_mariana_sdma_get_fp8_ocp_control_reg_value_for_config(
                        config->fp8_range_extension, config->ocp_sat, sel)
                off = add_wr32(instr, off, misc + aws_hal_get_sdma_data_conv_fp8_ocp_cfg_offset(sel), v, NULL)
            // non-OCP conversion control, selector 1 then 0
            for sel in {1, 0}:
                v = aws_hal_mariana_sdma_get_non_ocp_control_reg_value_for_config(
                        config->sis, config->sns, sel)
                off = add_wr32(instr, off, misc + aws_hal_get_sdma_data_conv_non_ocp_cfg_offset(sel), v, NULL)

    // (5) commit the accumulated CSR-write records to the preamble
    if buf_append(preamble, instr, off) != 0:                       // 0x265e80
        log("Failed to add TPB FP8 programming to model preamble")

    // (6) append a 0x40-byte "dummy read" record per programmed CSR window so the
    //     write is forced to retire before inference begins (read-back fence).
    //     The DVE path emits a second dummy read for the emax register.
    emit_fp8_dummy_read(preamble, tpb_csr + eng_off)                // data[0..1]=0x10AA, data[12]=9
    if eng->eng_type == 3:
        emit_fp8_dummy_read(preamble, tpb_csr + aws_hal_get_dve_sequencer_emax_cfg_offset())
    return NRT_SUCCESS
```

### Encoding — the emitted records

`add_wr32` (`@0x2733d0`) builds a 64-byte `ctrl_wr` record: its low opcode word is `4261 = 0x10A5`, i.e. `opcode = 0xA5` (the CSR-write opcode the validator's `is_valid_ctrl_wr` polices) and `inst_word_len = 0x10 = 16`; `BYTE12 = 4` sets the `wr_size` to a 4-byte write; the 8-byte `csr` address and 32-bit `value` fill the operand slots. The dummy-read record built inline in the encoder uses low word `4266 = 0x10AA`, i.e. `opcode = 0xAA`, `inst_word_len = 16`, with `data[12] = 9` and a register-form address marker byte `0x80` — a read of the just-written CSR window that fences the write.

| Emitted record | Opcode byte | `inst_word_len` | Distinguishing field | Role |
|---|---|---|---|---|
| CSR write (`add_wr32`) | `0xA5` (from `0x10A5`) | `16` | `wr_size = 4` (`BYTE12`) | program one FP8 control/emax/conv register |
| FP8 dummy read | `0xAA` (from `0x10AA`) | `16` | `data[12]=9`, addr marker `0x80` | read-back fence after the writes |

> **QUIRK —** the encoder's HAL value-packers are named `aws_hal_mariana_*` (`tpb_fp8_control_reg_value`, `sdma_get_fp8_ocp_control_reg_value_for_config`, …) yet the assert pins the *source* to `instruction_block_sunda.c`. The `mariana_` prefix names the **HAL origin of the FP8 register layout**, not the target architecture — it is the shared FP8/OCP register-encoding HAL that every arch's encoder calls. A reimplementer should not read this as a misplaced MARIANA-only path; the same value-packers serve the SUNDA (V2: Trn1/Inf2) encoder. (MED confidence on the precise SUNDA-vs-MARIANA attribution of the *register bit layout*; the call structure is HIGH.)

> **GOTCHA —** the per-engine fan-out is asymmetric and easy to get wrong: only the **DVE** engine (`eng_type==3`) programs the `emax` register, and only the **PE** engine (`eng_type==0`) loops over *all* DMA engines writing 4 SDMA conversion registers apiece. The ACT(1)/POOL(2) engines emit only the single control register. A reimplementer that programs `emax` or the SDMA conv regs for every engine will write registers that do not exist for that engine and corrupt the preamble.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `insert_set_fp8_conv_config` | `0x27d5b0` (1138 B) | emit per-engine FP8 enablement CSR writes into preamble | HIGH |
| `tdrv_arch_instr_block_supports_fp8_conv_config` | `0x30a290` | per-arch FP8 capability gate (asserts) | HIGH |
| `add_wr32` | `0x2733d0` | build one `ctrl_wr` (opcode `0xA5`) record | HIGH |
| `aws_hal_mariana_tpb_fp8_control_reg_value` | `0x4681a0` | pack the per-engine FP8 control CSR value | HIGH |
| `aws_hal_mariana_tpb_fp8_dve_emax_reg_value` | `0x468230` | pack the DVE-sequencer `emax` CSR value (E4M3 + E5M2) | HIGH |
| `aws_hal_mariana_sdma_get_fp8_ocp_control_reg_value_for_config` | `0x465080` | pack per-DMA OCP conversion control | HIGH |
| `aws_hal_mariana_sdma_get_non_ocp_control_reg_value_for_config` | `0x4650b0` | pack per-DMA non-OCP conversion control | HIGH |
| `tdrv_arch_get_num_dma_per_tpb` | `0x308f50` | per-arch DMA-engine count for the PE loop | HIGH |

### The `kbin_fp8_conv_cfg_t` policy struct

The 16-byte config the encoder reads (DWARF `kbin_fp8_conv_cfg_t`, layout confirmed):

| Field | Offset | Type | Feeds | Meaning |
|---|---|---|---|---|
| `fp8_range_extension` | `+0` | `bool` | tpb control + SDMA OCP | widen the FP8 exponent range beyond the format default |
| `emax_fp8_e4m3` | `+4` | `uint32` | DVE emax CSR | max exponent for E4M3 operands (range cap) |
| `emax_fp8_e5m2` | `+8` | `uint32` | DVE emax CSR | max exponent for E5M2 operands (range cap) |
| `ocp_sat` | `+12` | `bool` | tpb control + SDMA OCP | OCP saturation mode (clamp vs. wrap on overflow) |
| `sis` | `+13` | `bool` | tpb control + SDMA non-OCP | non-OCP conversion flag (signed-input-scale) |
| `sns` | `+14` | `bool` | tpb control + SDMA non-OCP | non-OCP conversion flag (signed-no-scale) |

> **NOTE —** `emax_fp8_e4m3` and `emax_fp8_e5m2` are the only two per-format range parameters, and they are consumed exclusively by the DVE-engine path. E3M4 (`FP8_EXP3`) has no dedicated `emax` field in this struct — its range follows the global `fp8_range_extension`/`ocp_sat` policy. The `sis`/`sns` field names are inferred from their position in the non-OCP packer call (`sdma_get_non_ocp_control_reg_value_for_config(sis, sns, sel)`); their exact bit semantics in the conversion CSR are not pinned (MED confidence on the names, HIGH on the wiring and the struct offsets).

---

## 4. dtype Legality Gates

### Purpose

Before a record with a dtype field is staged, the [validator layer](validator-architecture.md) checks that the dtype is legal for that opcode and arch. The check is more than "is it a defined enum value": several dtypes are *conditionally* illegal — gated by `allow_fp32r`, `allow_u64`, `allow_i64` flags the per-opcode validator passes in. Pinning these gates lets a reimplementer reproduce exactly which dtype bytes a given op accepts.

### Algorithm

The silent gate is a single 46-byte leaf; the debug twin runs the identical logic and names the failing stage. Both reduce to four conditional rejections plus the `<=15` membership test.

```c
// Models is_valid_dtype @0x27b910 (46 B, the fast-path gate).
bool is_valid_dtype(NEURON_ISA_TPB_DTYPE dtype, bool allow_fp32r):
    return dtype > UINT64           /* >1: rejects INVALID(0) AND UINT64(1) together */
        && dtype != INT64           /* 12: gated off on the base gate               */
        && (allow_fp32r || dtype != FP32R)   /* 11: FP32R only with flag             */
        && is_valid_enum(DTYPE, dtype);       /* 0x27b650: value <= 0xF              */

// Models dbg_is_valid_dtype @0x27f940 (2233 B): SAME logic, named asserts (153 callers).
// Stage order pinned by the four .rodata error fragments it emits.
NEURON_ISA_TPB_DEBUG_BOOL dbg_is_valid_dtype(dtype, allow_fp32r):
    r = new_dbg_bool(dtype != INVALID)                    // stage 1
    if dtype == INVALID:  fill("'dtype_invalid_check'")
    if dtype == FP32R /*11*/ && !allow_fp32r:             // stage 2
        fill("'dtype_fp32r_illegal_check'"); r.result = false
    if dtype == UINT64 /*1*/  /* && !allow_u64 */:        // stage 3
        fill("'dtype_uint64_illegal_check'"); r.result = false
    if dtype == INT64 /*12*/  /* && !allow_i64 */:        // stage 4
        fill("'dtype_int64_illegal_check'"); r.result = false
    merge(r, dbg_is_valid_enum(DTYPE, dtype))             // stage 5: value <= 0xF
    return r
```

The flag-parameterized variants extend the same shape: `dbg_is_valid_dtype_64` (`@0x328030`) adds explicit `allow_u64`/`allow_i64` arguments (so UINT64/INT64 become *conditionally* legal rather than always-rejected), and `dbg_is_valid_dtype_inc_fp4` (`@0x3b3b10`, v4/MARIANA band only) adds an `allow_fp4` argument and selects the extended enum list that admits `FP4_EXP2=16`.

> **GOTCHA —** the FP8 dtypes (`13/14/15`) are **never** gated by these flags — there is no `allow_fp8`. Once the arch supports FP8 at all (the encoder's capability gate), every op that accepts a float dtype accepts all three FP8 sub-formats. The conditionally-illegal set is exactly `{INVALID, UINT64, FP32R, INT64}` (plus FP4/CPTC on the extended path). A reimplementer must not invent an FP8 gate at this layer; FP8 enablement is the encoder's job ([§3](#3-the-fp8-enablement-encoder)), and FP8 *legality* per op is unconditional once the format is in the `<=15` table.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `is_valid_dtype` | `0x27b910` | 46 B | silent dtype gate (INVALID/UINT64/INT64/FP32R + membership) | HIGH |
| `dbg_is_valid_dtype` | `0x27f940` | 2233 B | debug twin; 153 callers; 4 named asserts | HIGH |
| `dbg_is_valid_dtype_64` | `0x328030` | 1256 B | adds `allow_u64`/`allow_i64` gates | HIGH |
| `dbg_is_valid_dtype_inc_fp4` | `0x3b3b10` | — | adds `allow_fp4`; v4/MARIANA band; admits FP4 | MEDIUM |
| `is_valid_enum` (case `DTYPE`) | `0x27b650` | — | membership oracle: `value <= 0xF` | HIGH |

> **CORRECTION (ISA-FP8-2) —** the silent `is_valid_dtype` expresses the INVALID-and-UINT64 rejection as a single `dtype > UINT64` (`>1`) test, *not* as two separate comparisons. Because `UINT64=1` sits directly above `INVALID=0` in the ISA enum, one `>1` comparison rejects both, then INT64 and FP32R are gated individually. A reimplementer who writes `dtype != INVALID` alone will wrongly accept `UINT64` on the base (no-`allow_u64`) gate. The debug twin splits the same logic into separately-named `dtype_invalid_check` and `dtype_uint64_illegal_check` stages for diagnostics, but the silent gate folds them.

---

## Related Components

| Name | Relationship |
|---|---|
| `NEURON_ISA_TPB_DTYPE` | the ISA-word dtype enum this page decodes (FP8 = 13/14/15) |
| `kbin_dtype` / `nrt_dtype` / `SDMA_DTYPE` | the parallel dtype numberings the NEFF builder remaps to/from |
| `insert_set_fp8_conv_config` | the FP8-enablement encoder that programs per-engine conversion CSRs |
| `kbin_fp8_conv_cfg_t` | the 16-byte FP8 policy struct the encoder reads |
| `is_valid_dtype` / `dbg_is_valid_dtype` | the silent/debug dtype legality gates |

## Cross-References

- [The dtype System](../neff/dtype-system.md) — the higher-level `kbin_dtype`/`nrt_dtype` enum, its width/semantics, and the kbin→ISA remap this page's encoding consumes
- [The 64-Byte Instruction Record Format](instruction-record.md) — the records whose `in_dtype`/`out_dtype` fields carry the `NEURON_ISA_TPB_DTYPE` values, and the `ctrl_wr` (opcode `0xA5`) record `add_wr32` emits
- [ISA Validator Architecture and Entry Tree](validator-architecture.md) — the silent/debug validator forest the dtype gates live in, `is_valid_enum`, and the `NEURON_ISA_TPB_DEBUG_BOOL` ABI
- [Per-Arch Instruction Validators (SUNDA / CAYMAN / MARIANA Deltas)](validators-per-arch.md) — the three validator clones and the arch band where the FP4-aware `_inc_fp4` dtype variant appears
