# DVE Search/Select Cluster (Max8 / MatchValueLoad / FindIndex8 / MatchReplace8)

> **Scope.** This page decodes the **four contiguous opcodes `0x6c`–`0x6f`** — the
> DVE-native **search/select cluster** of the NeuronCore GPSIMD ISA: **`Max8` (`0x6c`)**,
> **`MatchValueLoad` (`0x6d`)**, **`FindIndex8` (`0x6e`)**, and **`MatchReplace8`
> (`0x6f`)**. Together they are the **beam-search building blocks**: a top‑8 score
> reducer, an 8‑key state loader, an 8‑key first-match index emitter, and an 8‑key
> first-match replacer. The defining structural fact, proved three ways below, is that
> **none of these four is a `Q7_POOL` software kernel** — they have **no `kernel_info_table`
> entry, no `funcVA`, no nrtucode kernel body**. The work happens in the **DVE datapath**;
> the firmware only *arms* and *emits* them. This page decodes each opcode
> instruction-exact (operand struct, validator, datapath, dtype matrix), names the
> `ivp_*` / `xdref` value primitive each compare/reduce/emit maps to, and reconciles the
> per-gen PROF-arming claim against the ISA-header definition layer.
>
> Cross-links: the index consumers in [`../../isa/ref/b19-scatter-gather.md`](../../isa/ref/b19-scatter-gather.md)
> (the SuperGather batch — what eats the 8 emitted indices); the DVE channel-state
> read-back ops in [`dve-read-state.md`](./dve-read-state.md) (planned); the *unbounded*
> predicate-scan sibling [`nonzero-with-count.md`](./nonzero-with-count.md) (planned); and
> the live value-core differential **VAL‑13**.
>
> Confidence tags use the `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` model defined in
> [`../../reference/confidence-model.md`](../../reference/confidence-model.md).

---

## 1. TL;DR — the pinned facts

| # | Fact | Evidence | Tag |
|---|------|----------|-----|
| 1 | The cluster is a **contiguous 4‑opcode block `0x6c..0x6f`**: `MAX8=0x6c`, `MATCH_VALUE_LOAD=0x6d`, `FIND_INDEX8=0x6e`, `MATCH_REPLACE8=0x6f`; all carry the `// Y` (maintained) flag in all four gens. | `aws_neuron_isa_tpb_common.h` sunda `:197–200`, maverick `:208–211` | HIGH/OBSERVED |
| 2 | All four are **DVE-engine ops, not POOL kernels.** They map to the DVE structs `S2_BN`/`S4D2_BN`/`S4D4_MR`, NOT the POOL struct `S4D4_PL`. | `instruction_mapping.json` `struct2opcode` | HIGH/OBSERVED |
| 3 | **`MatchValueLoad`** loads 8 keys into per-channel DVE state `mv[0..7]` in **REVERSED** order: `src[k] -> mv[7-k]`. The struct is `S2_BN` (one 2D src, no dst). | `aws_neuron_isa_tpb_s2_bn.h:50–61` | HIGH/OBSERVED |
| 4 | **`FindIndex8`** scans the src stream and, per key, emits the **flat index of its first exact-equality match** — exactly **8 indices** out, `uint16`/`uint32` only, `>= 32768` sentinel if a key never matches. | `s4d2_bn.h:171–235`, `has_find_index8_dst_type:469` | HIGH/OBSERVED |
| 5 | **`Max8`** is a **top‑8 reduction**: it keeps the **8 largest values** seen across `≤16384` src elements and writes them smallest→largest. Output is **8 VALUES** (any compute dtype incl. FP32R), not indices. | `s4d2_bn.h:157–167`, `is_valid_max8:336` | HIGH/OBSERVED |
| 6 | **`MatchReplace8`** runs the **same first-match core** as FindIndex8 but emits a **1:1 stream**: each first-match element becomes the instruction's **fp32 `immediate`** (`@off 40`); everything else passes through (in→fp32→out). | `s4d4_mr.h:23–43`, struct `:84–94` | HIGH/OBSERVED |
| 7 | **No software body.** `0x6c/0x6d/0x6e/0x6f` are absent from the carved SUNDA(18-entry) and CAYMAN(17-entry) `kernel_info_table`s. The DVE datapath does the work; firmware only emits/arms. | SX‑FW‑18 table dump; struct map | HIGH/OBSERVED |
| 8 | **`Max8` has NO in-corpus emitter.** `MatchValueLoad`/`MatchReplace`/`FindIndex8` each carry a 4-copy `"S: <Name>\n"` SEQ dispatch token; `"S: Max8"` has **0 copies** anywhere in the nrtucode libs. | `rg -ao` over `libnrtucode_internal.so` (this task) | HIGH/OBSERVED |
| 9 | **CORRECTION.** These are NOT "first appearing at MARIANA". The SUNDA and CAYMAN headers ALREADY define all four (`// Y` + struct map). ISA-defined from **SUNDA (NC‑v2)**; the MAVERICK event is a DVE **PROF re-arm** (runtime dispatch enable), a different layer. | per-gen `common.h` + `instruction_mapping.json` | HIGH/OBSERVED |

---

## 2. Provenance / carve anchors

All facts derive from static analysis of the shipped **host customop-lib ISA C headers**, the
shipped **`instruction_mapping.json`**, the shipped **`libnrtucode*` SEQ-ASCII rodata**, the
shipped **Cadence ncore2gp value-core configuration DLLs** (`libfiss-base.so`), and carved
device-firmware **`kernel_info_table` data** read via the Cadence `xtensa-elf-readelf`
(`XTENSA_CORE=ncore2gp`). No vendor source was consulted. Lawful interoperability RE
(DMCA 17 U.S.C. §1201(f)).

| Artifact | Path / value |
|----------|--------------|
| ISA headers (4 gens) | `…/custom_op/c10/include/neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/aws_neuron_isa_tpb_{common,s2_bn,s4d2_bn,s4d4_mr}.h` |
| Struct→opcode map | `…/tpb/instruction_mapping.json` (`struct2opcode`), identical membership in all 4 gens |
| SEQ-token container | `…/custom_op/c10/lib/libnrtucode_internal.so` (the `"S: <Name>\n"` dispatch tokens) |
| Value cores (ncore2gp) | `…/gpsimd_tools/tools/ncore2gp/config/libfiss-base.so` (the `opcode__ivp_*` / `module__xdref_*` ISS primitives) |
| `xtensa-elf-readelf` | `gpsimd_tools/tools/XtensaTools/bin/`, `XTENSA_CORE=ncore2gp` |
| Carved POOL images | `extisa_{SUNDA,CAYMAN}_POOL_PERF_EXTISA_0.so` — `kernel_info_table` SUNDA `@file 0xb260/0x90`, CAYMAN `@0x7400/0x88` |
| Backing reports | SX‑FW‑44 (FindIndex8) + GX‑OP‑01 (Max8/MVL/MatchReplace8); IMG‑27 (per-gen PROF); FW‑18 (kernel_info format) |

> **NOTE (no FLIX desync surface).** This decode never crosses the Q7 Vision FLIX bundle
> decoder. The operand structs come from C headers + `gcc` compile-verify; the
> `kernel_info_table` evidence is a **hex data dump** (read via `readelf`, not the
> instruction-stream decoder); the value primitives are read from the **host x86**
> `libfiss-base.so` symbol table via `nm`. The ncore2gp `op0=e/f` FLIX-vs-LX mis-decode
> artifact does **not** apply — none of these four opcodes has a Q7 kernel body to
> disassemble, *because they are DVE hardware*. [HIGH — no code-stream desync touched]

---

## 3. The opcode block — byte-exact, all four gens

From the `NEURON_ISA_TPB_OPCODE` enum in `aws_neuron_isa_tpb_common.h`. The enum is
`__attribute__((__packed__))` (`common.h:8` / `:299`), so the opcode is a **single byte**
occupying offset 0 of the 4-byte `NEURON_ISA_TPB_HEADER` (`opcode, inst_word_len,
debug_cmd, debug_hint` — `common.h:405–410`). The trailing `// Y` column is defined at
`common.h:154` as *"Tested/Maintained/Not deprecated?"*; deprecated opcodes carry `// n`
(e.g. the old `Rand` `0x76` is `// n`). All four cluster members are `// Y`:

```c
NEURON_ISA_TPB_OPCODE_MAX8              = 0x6c,    // Y   <- top-8 value reducer
NEURON_ISA_TPB_OPCODE_MATCH_VALUE_LOAD  = 0x6d,    // Y   <- load 8 keys mv[0..7]
NEURON_ISA_TPB_OPCODE_FIND_INDEX8       = 0x6e,    // Y   <- 8-key first-match INDEX emit
NEURON_ISA_TPB_OPCODE_MATCH_REPLACE8    = 0x6f,    // Y   <- 8-key first-match REPLACE
```

| gen | NC‑ver | `0x6c` MAX8 | `0x6d` MVL | `0x6e` FI8 | `0x6f` MR8 | struct map | verdict |
|-----|--------|-------------|------------|------------|------------|-----------|---------|
| sunda | NC‑v2 | `// Y :197` | `// Y :198` | `// Y :199` | `// Y :200` | all 4 mapped | PRESENT |
| cayman | NC‑v3 | `// Y :200` | `// Y :201` | `// Y :202` | `// Y :203` | all 4 mapped | PRESENT |
| mariana | NC‑v4 | `// Y :205` | `// Y :206` | `// Y :207` | `// Y :208` | all 4 mapped | PRESENT |
| maverick | NC‑v5 | `// Y :208` | `// Y :209` | `// Y :210` | `// Y :211` | all 4 mapped | PRESENT |

> **CORRECTION (premise refuted at the ISA layer).** IMG‑27 frames `0x6c/0x6d/0x6e/0x6f`
> as *"PRE-EXISTING MARIANA"* opcodes and implies first-appearance at MARIANA. The shipped
> **SUNDA (NC‑v2)** and **CAYMAN (NC‑v3)** headers ALREADY define all four with `// Y` and
> map them to their DVE structs. The opcode bytes are **ISA-present from SUNDA**. What
> changes at MARIANA/MAVERICK is the **DVE PROF dispatch arming** (a runtime
> dispatch-enable table, §10), not the ISA opcode space. ISA-DEFINED (all 4 gens,
> OBSERVED) and DVE-PROF-DISPATCHABLE (the runtime arming IMG‑27 tracked) are two
> different layers; both statements are true at their own layer. **Do not write
> "FindIndex8/Max8 first appears at MARIANA."** [HIGH/OBSERVED for the ISA layer]

> **MAVERICK interior caveat.** Per the standing rule, v2–v4 (sunda/cayman/mariana) are
> byte-grounded; the **MAVERICK (v5)** header is OBSERVED at the enum/struct surface but
> its DVE PROF-table interior is INFERRED from the IMG‑27 carve, not independently
> byte-decoded here.

---

## 4. Struct → opcode map (the DVE-vs-POOL discriminator)

The per-gen `instruction_mapping.json` `struct2opcode` block places each opcode under its
**operand-struct group**. The cluster lives entirely under the **DVE BatchNorm / ImmLd
family of structs**, NOT under the POOL struct — the single cleanest proof that these are
DVE ops:

```jsonc
"NEURON_ISA_TPB_S2_BN_STRUCT":   [ BATCH_NORM_PARAM_LOAD, MATCH_VALUE_LOAD,
                                   TENSOR_SCALAR_IMM_LD_ARITH, TENSOR_SCALAR_IMM_LD_BITVEC ],
"NEURON_ISA_TPB_S4D2_BN_STRUCT": [ BATCH_NORM_STATS2, TRANSPOSE_BATCH_NORM_STATS2,
                                   BATCH_NORM_STATS, BATCH_NORM_AGGREGATE,
                                   MAX8, FIND_INDEX8 ],
"NEURON_ISA_TPB_S4D4_MR_STRUCT": [ MATCH_REPLACE8 ],          // sole occupant
// ── the POOL codec lives in a DIFFERENT struct ───────────────────────────────
"NEURON_ISA_TPB_S4D4_PL_STRUCT": [ POOL, MAX_POOL_SELECT ],   // <- Q7_POOL kernels
```

So: `MatchValueLoad` shares its struct (`S2_BN`) with the DVE BatchNorm-param and
ImmLd loaders; `Max8`+`FindIndex8` share `S4D2_BN` with DVE BatchNorm-stats; `MatchReplace8`
gets its own `S4D4_MR`. The actual `Q7_POOL` codec (`POOL`/`MAX_POOL_SELECT`) is in
`S4D4_PL`, where the cluster is conspicuously **not**. [HIGH/OBSERVED]

The shared sub-structs (all `gcc` compile-verified, `_Static_assert(sizeof==64)`):

| sub-struct | size | layout |
|---|---|---|
| `NEURON_ISA_TPB_HEADER` | 4 | `opcode:u8, inst_word_len:u8, debug_cmd:u8, debug_hint:u8` |
| `NEURON_ISA_TPB_EVENTS` | 8 | `wait_mode, wait_idx, update_mode, update_idx, semaphore_value:u32` |
| `NEURON_ISA_TPB_TENSOR4D` | 20 | `start_addr:Addr4(4) + step_elem[4]:i16(8) + num_elem[4]:u16(8)` |
| `NEURON_ISA_TPB_TENSOR2D` | 12 | `start_addr:Addr4(4) + step_elem[2]:i16(4) + num_elem[2]:u16(4)` |
| `NEURON_ISA_TPB_DTYPE` | 1 | packed enum (§9) |
| `NEURON_ISA_TPB_IMM_VAL_INST_FIELD` | 4 | union (`imm_ptr` / `imm_reg` / `imm_arith_fp32`) |

---

## 5. The four operand structs — offset-exact

### 5.1 `MatchValueLoad` (0x6d) → `NEURON_ISA_TPB_S2_BN_STRUCT` (64 B)

The **key-loader**. "One 2D SRC, no DST" (`s2_bn.h:15–16`). Shared with
`BatchNormParamLoad` + `TensorScalarImmLd[Arith/Bitvec]` — the FW‑57 ImmLd loader analog.

| off | size | field | MatchValueLoad role |
|----:|-----:|-------|---------------------|
| 0 | 4 | `header` | opcode `0x6d` in `header.opcode` |
| 4 | 8 | `events` | event/sync triggers |
| 12 | 4 | `reserved0[4]` | must be zero |
| 16 | 12 | `src_mem_pattern` (T2d) | the **8 KEYS** to load (num_elem product == 8) |
| 28 | 4 | `imm1` (ImmVal) | **unused — must be 0.0** |
| 32 | 1 | `dtype` | src dtype (converted to fp32 in DVE state) |
| 33 | 1 | `reserved1[1]` | must be zero |
| 34 | 1 | `num_active_channels` | DVE lane count `[1,128]` |
| 35 | 9 | `reserved2[9]` | must be zero |
| 44 | 4 | `imm0_ptr` (ImmVal) | **unused — must be 0** |
| 48 | 1 | `imm0_src` (ImmSrc) | **unused — must be 0** |
| 49 | 15 | `reserved3[15]` | must be zero |

> **GOTCHA (key count is shape-encoded, not a count field).** "8 keys" is asserted as a
> num_elem **product**, accepting any of the 2D shapes `(1,8) | (2,4) | (4,2) | (8,1)`
> (`has_match_value_load_src_element_cnt`, `s2_bn.h:195–204`). There is no dedicated
> count byte — the validator multiplies `num_elem[0]*num_elem[1]` and demands `== 8`.

### 5.2 `Max8` (0x6c) and `FindIndex8` (0x6e) → `NEURON_ISA_TPB_S4D2_BN_STRUCT` (64 B)

Both share one struct. "One 4D SRC, one 2D DST" (`s4d2_bn.h:14–25`). The dst is always an
**8-element** 2D tensor; the difference between the two ops is entirely in the **datapath**
and the **out_dtype validator**, not the bytes.

| off | size | field | Max8 / FindIndex8 role |
|----:|-----:|-------|------------------------|
| 0 | 4 | `header` | opcode `0x6c` (Max8) / `0x6e` (FindIndex8) |
| 4 | 8 | `events` | event/sync triggers |
| 12 | 20 | `src_mem_pattern` (T4d) | **SEARCH INPUT** stream (`≥8`, `≤16384` elems) |
| 32 | 1 | `in_dtype` | input dtype (converted to fp32 internally) |
| 33 | 1 | `out_dtype` | Max8: VALUE dtype; FindIndex8: **INDEX dtype** (u16/u32) |
| 34 | 1 | `num_active_channels` | DVE lane count `[1,128]` |
| 35 | 5 | `reserved0[5]` | must be zero |
| 40 | 4 | `even_count` (f32) | **MUST be 0.0** (`s4d2_bn_zero_counts`) |
| 44 | 4 | `odd_count` (f32) | **MUST be 0.0** (`s4d2_bn_zero_counts`) |
| 48 | 12 | `dst_mem_pattern` (T2d) | **OUTPUT 8 elems** (Max8=values, FindIndex8=indices) |
| 60 | 4 | `reserved1[4]` | must be zero |

> **QUIRK (the `even_count`/`odd_count` fields are inert ballast here).** Offsets 40–47
> are the `BatchNormStats2` even/odd-count immediates (`s4d2_bn.h:55–61`). Because
> `Max8`/`FindIndex8` reuse the *same struct* as BatchNorm, the fields physically exist
> but are forced to **`0.0`** by `s4d2_bn_zero_counts` (`s4d2_bn.h:424–427`). A
> reimplementation must zero them or the validator rejects the instruction. This is pure
> struct-sharing residue — the search ops have no even/odd concept.

### 5.3 `MatchReplace8` (0x6f) → `NEURON_ISA_TPB_S4D4_MR_STRUCT` (64 B)

The **replace** variant. "One 4D SRC, one 4D DST" + an embedded fp32 immediate
(`s4d4_mr.h:78–94`). **Sole occupant** of its struct. Note: **no reserved tail** — the
second `Tensor4d` runs to byte 63.

| off | size | field | MatchReplace8 role |
|----:|-----:|-------|--------------------|
| 0 | 4 | `header` | opcode `0x6f` |
| 4 | 8 | `events` | event/sync triggers |
| 12 | 20 | `src_mem_pattern` (T4d) | **SEARCH INPUT** stream (`≤16384` elems) |
| 32 | 1 | `in_dtype` | input dtype (converted to fp32 internally) |
| 33 | 1 | `out_dtype` | output dtype |
| 34 | 1 | `num_active_channels` | DVE lane count `[1,128]` |
| 35 | 5 | `reserved0[5]` | must be zero |
| 40 | 4 | `immediate` (f32) | the **REPLACEMENT VALUE** written on first match |
| 44 | 20 | `dst_mem_pattern` (T4d) | **OUTPUT** stream (== src elem count, 1:1) |

> **NOTE (the same byte slot, three meanings).** Offset 40 is `even_count` in `S4D2_BN`
> (Max8/FindIndex8, forced 0), `immediate` in `S4D4_MR` (MatchReplace8, the live
> replacement), and `imm1` in `S2_BN` (MatchValueLoad, forced 0). Same physical slot,
> opcode-dependent semantics — a classic Neuron-ISA struct-overlap trap. The struct group
> (chosen by `header.opcode` via `struct2opcode`) decides which reading is live.

---

## 6. The four datapaths — instruction-exact

These are **DVE-hardware datapaths**. There is **no Q7 `ivp_*`/TIE op named
find/index/match** in the shipped ISA roster — so the algorithms below are the **silicon
behavior the ISA headers prescribe**, expressed as annotated C, with the **value-core
primitive** that realizes each compare/reduce/select named from `libfiss-base.so`. (The
`ivp_*` primitives are how the *software* siblings — e.g. NonzeroWithCount — realize the
same idiom; the DVE engine implements the equivalent compare/select logic in hardware.)

### 6.1 `MatchValueLoad` (0x6d) — the stateful prologue

The setup op. It deposits 8 fp32 keys into per-channel DVE state, in **reversed** order,
to be consumed by a *later* `FindIndex8` or `MatchReplace8`. The reversed map is verbatim
from the header (`s2_bn.h:56–61`): `first src element → mv[7]`, `last → mv[0]`.

```c
// is_valid_match_value_load (s2_bn.h:161–172) gates entry.
// DVE per-channel state (one mv[8] per active channel):
//   float dve_mv[NUM_CHANNELS][8];     // persists across instructions
//   bool  dve_mv_matched[NUM_CHANNELS][8];  // armed clean by this op

void dve_match_value_load(const S2_BN *I) {              // opcode 0x6d
    for (int ch = 0; ch < I->num_active_channels; ch++) {
        // src has EXACTLY 8 elements (shape product == 8, §5.1)
        for (int k = 0; k < 8; k++) {
            float key = to_fp32(load(I->src_mem_pattern, ch, k), I->dtype);
            dve_mv[ch][7 - k] = key;          // REVERSED: src[k] -> mv[7-k]
            dve_mv_matched[ch][7 - k] = false; // arm the slot
        }
    }
    // NO dst write; NO Q7_POOL funcVA hop. Pure state deposit.
}
```

> **GOTCHA (the reversal is load-bearing for beam search).** Because `Max8` writes its 8
> values **smallest→largest** (§6.2) and `MatchValueLoad` reverses on load
> (`src[k]→mv[7-k]`), feeding `Max8`'s output straight into `MatchValueLoad` makes
> **`mv[0]` = the LARGEST score**. The two reversals compose so the lowest key index holds
> the top score — consistent with the lowest-`n`-first arbitration in the consumers (§6.3).
> [HIGH/OBSERVED for both reversals; the *composition* INFERRED — the headers do not
> spell out the `Max8→MatchValueLoad` handoff as a fixed pipeline]

### 6.2 `Max8` (0x6c) — the top‑8 value reducer

A streaming top‑k=8 reduction in the fp32 domain. Output is **8 VALUES**, written
**smallest→largest** (`s4d2_bn.h:157–167`).

```c
// is_valid_max8 (s4d2_bn.h:336–352): src in [8,16384], dst==8,
//   even_count==odd_count==0, in_dtype valid (no FP32R),
//   out_dtype valid (FP32R ALLOWED — it's a value, not an index).

void dve_max8(const S4D2_BN *I) {                        // opcode 0x6c
    int N = elem_count_t4d(I->src_mem_pattern);          // 8..16384
    for (int ch = 0; ch < I->num_active_channels; ch++) {
        float top[8];                                    // running top-8, ascending
        for (int j = 0; j < 8; j++) top[j] = -INFINITY;
        for (int i = 0; i < N; i++) {
            float e = to_fp32(load_xyzw(I->src_mem_pattern, ch, i), I->in_dtype);
            // insert e if it beats the smallest survivor, then re-sort the 8 slots.
            // The fp32 max compare is the engine's maxNum primitive:
            //   module__xdref_rmaxnum_n_2x32f / opcode__ivp_maxn_2xf32  (NaN-quieting).
            if (e > top[0]) {                            // top[0] == current minimum
                top[0] = e;
                insertion_reorder_ascending(top, 8);     // 8-wide, O(1) amortized
            }
        }
        // write 8 values smallest->largest, converted to out_dtype:
        for (int j = 0; j < 8; j++)
            store(I->dst_mem_pattern, ch, j, from_fp32(top[j], I->out_dtype));
    }
}
```

The reduction primitive is byte-pinned in `libfiss-base.so`: the **reduction-max**
`module__xdref_rmaxnum_n_2x32f_1_32f_512f` (`nm @0x1b2710`) and the per-lane fp32 max
`opcode__ivp_maxn_2xf32__stage_5` (`@0x585020`) / IEEE-`maxNum`-quieting
`opcode__ivp_maxnumn_2xf32__stage_5` (`@0x589040`); the fp16 path is
`opcode__ivp_maxnumnxf16__stage_5` (`@0x223910`). The `maxnum`/`rmaxnum` naming pins the
NaN-quieting semantics (IEEE `maxNum`: NaN operand → the non-NaN operand wins).
[HIGH/OBSERVED for the primitive names; the top‑8 *micro-architecture* (sort network vs
heap) is MED/INFERRED — not byte-decoded]

> **QUIRK (Max8 is ISA-resident but un-emittable in-corpus).** `Max8` is fully
> ISA-defined, struct-mapped, and PROF-arm-listed at MAVERICK — yet it has **zero**
> `"S: Max8"` SEQ dispatch tokens in `libnrtucode_internal.so` (vs **4 each** for the
> other three; §8). The DVE engine can decode and execute `Max8`, but **no in-corpus
> nrtucode emitter produces it**. It is "ISA-present everywhere, in-corpus-emittable
> nowhere" — the single biggest divergence among the four members. The WHY (out-of-corpus
> front-end vs reserved/staged) is INFERRED. [absence HIGH/OBSERVED]

### 6.3 `FindIndex8` (0x6e) — the predicate-scan + index-emit

The flagship. It consumes the `mv[0..7]` keys from a prior `MatchValueLoad` and emits, per
key, the **flat index of its first exact-equality match**. Output is exactly **8 indices**.

```c
// is_valid_find_index8 (s4d2_bn.h:354–371): src in [8,16384], dst==8,
//   even_count==odd_count==0, out_dtype in {UINT16,UINT32} ONLY.

void dve_find_index8(const S4D2_BN *I) {                 // opcode 0x6e
    int N = elem_count_t4d(I->src_mem_pattern);
    for (int ch = 0; ch < I->num_active_channels; ch++) {
        uint32_t idx[8];
        for (int n = 0; n < 8; n++) idx[n] = NOT_FOUND;  // >=32768 sentinel
        for (int i = 0; i < N; i++) {                    // XYZW-unrolled, 0-based
            float e = to_fp32(load_xyzw(I->src_mem_pattern, ch, i), I->in_dtype);
            for (int n = 0; n < 8; n++) {                // ASCENDING: mv[0] -> mv[7]
                if (dve_mv_matched[ch][n]) continue;     // key already consumed
                // ORDERED fp equality (NaN never matches):
                //   opcode__ivp_oeqn_2xf32 (@0x580f20) -> vbool predicate.
                if (e == dve_mv[ch][n]) {
                    idx[n] = (uint32_t)i;                 // FIRST occurrence wins
                    dve_mv_matched[ch][n] = true;
                    break;                                // element CONSUMED; not tried vs higher n
                }
            }
        }
        // write exactly 8 indices, narrowed to out_dtype (u16 or u32):
        for (int n = 0; n < 8; n++) {
            uint32_t v = (idx[n] == NOT_FOUND)
                       ? (I->out_dtype == UINT32 ? 0xFFFFFFFFu : 0xFFFFu)
                       : idx[n];
            store(I->dst_mem_pattern, ch, n, narrow(v, I->out_dtype));
        }
    }
}
```

Four semantics are nailed by the header text and must be reproduced exactly:

1. **Exact equality, not threshold/range** — "If the input value *exactly* matches the
   `mv[n]`" (`s4d2_bn.h:183`). All compares are in fp32 after input conversion.
2. **First-match (FIRST occurrence wins)** — once `mv[n]` matches an element, "`mv[n]`
   will be marked 'matched' and will not match any later input" (`:190–191`).
3. **Lowest-`n`-first arbitration for duplicate keys** — an element is consumed by the
   lowest still-unmatched key it equals; if the 8 keys contain duplicates, "the first
   unmatched `mv[n]` (with the smallest `n`) will be matched first and the rest ignored"
   (`:207–213`).
4. **Not-found sentinel `>= 32768`** — concretely (Neuron-uarch) `0xFFFFFFFF` for u32,
   `0xFFFF` for u16 (`:214–222`). "Should not normally happen in beam-search."

The compare core is the ordered-fp-equal primitive `opcode__ivp_oeqn_2xf32__stage_5`
(`nm @0x580f20`, fp32) / `opcode__ivp_oeqnxf16__stage_5` (`@0x8018f0`, fp16) — the same
`oeq → vbool` predicate the software predicate-scan siblings use. The "emit index on
first-set predicate" step is the engine's select/move family
(`opcode__ivp_seln_2x32__stage_5 @0x4cd1c0`, `opcode__ivp_selnx16__stage_5 @0x264980`).
[primitive names HIGH/OBSERVED; the exact DVE lane-parallel scan MED/INFERRED]

> **GOTCHA (`out_dtype` is hard-restricted to indices).** Unlike `Max8` (which shares the
> struct), `FindIndex8` calls `has_find_index8_dst_type` (`s4d2_bn.h:469–472`) requiring
> `out_dtype ∈ {UINT16, UINT32}`. The two validators *diverge exactly here*: `Max8` uses
> only `is_valid_dtype(out, FP32R::True)` and emits values; `FindIndex8` ANDs the
> index-type restriction. A u16 dst can only address `< 65536` positions, but the
> `≤16384` src cap (and the `≥32768` sentinel reservation) keep real indices below the
> sentinel band. [HIGH/OBSERVED]

### 6.4 `MatchReplace8` (0x6f) — the first-match replacer

Same first-match core as FindIndex8, but instead of emitting indices it emits a **1:1
transformed stream**: each first-match element becomes the instruction's fp32 `immediate`;
all other elements pass through (in→fp32→out).

```c
// is_valid_match_replace8 (s4d4_mr.h:105–120): src<=16384, src==dst count,
//   channels in [1,POOLING_NUM_CHANNELS=128], 7-dtype list (§9).

void dve_match_replace8(const S4D4_MR *I) {              // opcode 0x6f
    int N = elem_count_t4d(I->src_mem_pattern);          // == dst count (1:1)
    for (int ch = 0; ch < I->num_active_channels; ch++) {
        for (int i = 0; i < N; i++) {
            float e = to_fp32(load_xyzw(I->src_mem_pattern, ch, i), I->in_dtype);
            float out = e;                               // default: pass-through
            for (int n = 0; n < 8; n++) {                // ASCENDING mv[0]->mv[7]
                if (dve_mv_matched[ch][n]) continue;
                if (e == dve_mv[ch][n]) {                // ivp_oeqn_2xf32 -> vbool
                    out = I->immediate;                  // SCALAR fp32 immediate @off 40
                    dve_mv_matched[ch][n] = true;
                    break;                               // consumed; not tried vs higher n
                }
            }
            store(I->dst_mem_pattern, ch, i, from_fp32(out, I->out_dtype));
        }
    }
}
```

> **NOTE (one immediate for all 8 keys — not a replacement vector).** The replacement is a
> **single** scalar fp32 `immediate` in the instruction (`s4d4_mr.h:31–32`), applied to
> whichever element first matches **any** `mv[n]`. All 8 first-matches get the **same**
> replacement. It is NOT a per-key replacement vector. In beam search this is the
> "blank out the already-selected top‑8" step: set `immediate = -INFINITY` so the *next*
> `Max8` pass finds the following 8 without re-picking the winners — extending top‑8 to
> top‑16/24/… (use-note `s4d4_mr.h:44–48`). [replace mechanics HIGH/OBSERVED; the exact
> beam-extension wiring MED/INFERRED]

---

## 7. The dispatch surface — why there is no software body

FW‑18/FW‑64 established the GPSIMD front-end has **two** dispatch surfaces in
`libnrtucode_internal.so`:

- **(A) DVE / sequencer SEQ-ASCII tags** — byte pattern `"S: <Name>\n"`. The DVE
  decoder matches `header.opcode` against the struct-format group and runs the **DVE
  datapath**. No kernel body.
- **(B) `Q7_POOL` software kernels** — a `kernel_info_table` keyed by opcode, each entry
  carrying a `funcVA` to a templated nrtucode kernel body (the `callx8`/`funcVA` hop).

The search cluster is **entirely surface (A)**. Three independent confirmations:

**(1) SEQ-token multiplicity (measured this task, `rg -ao "S: <name>" libnrtucode_internal.so | wc -l`):**

| token | copies | class |
|-------|-------:|-------|
| `S: MatchValueLoad` | **4** | DVE compute (0x6d), one per gen |
| `S: MatchReplace` | **4** | DVE compute (0x6f) — tag is `"MatchReplace"`, **no `8` suffix** |
| `S: FindIndex8` | **4** | DVE compute (0x6e), one per gen |
| `S: Max8` | **0** | ABSENT — no in-corpus emitter (0x6c) |
| `S: TensorLoad` / `S: TensorStore` | 16 / 16 | iTPB sequencer (reference) |
| `S: Pool` | 10 | Q7_POOL codec (reference) |

The 4‑copy DVE-class signature for MVL/MatchReplace/FindIndex8 vs the 0 for Max8 was
verified directly. [HIGH/OBSERVED]

**(2) Struct membership** — the cluster maps to `S2_BN`/`S4D2_BN`/`S4D4_MR` (DVE family),
not `S4D4_PL` (POOL) (§4). [HIGH/OBSERVED]

**(3) Carved `kernel_info_table` absence** — reading the carved SUNDA and CAYMAN POOL
images with the shipped `xtensa-elf-readelf`:

```text
SUNDA  kernel_info_table @file 0xb260, 0x90 B = 18 entries (8B records, opcode @rec+3)
       decoded 18 opcodes: 41 43 44 46 47 49 67 68 74 79 7a 7c 7d 7e 92 b8 bb e7
CAYMAN kernel_info_table @file 0x7400, 0x88 B = 17 entries
       decoded:           41 45 46 47 51 52 7b 7c 7d 7e be f0 f2 (+dups)
       => 0x6c / 0x6d / 0x6e / 0x6f ABSENT from BOTH tables.
```

None of `0x6c/0x6d/0x6e/0x6f` is a `Q7_POOL` software kernel in any read image. The
search/select cluster therefore has **no funcVA, no kernel body** — the work is in the DVE
datapath. This is the cleanest contrast with the POOL-codec kernels (NonzeroWithCount,
Sort, SparsityCompress) which *do* carry a `kernel_info_table` funcVA.
[HIGH/OBSERVED — `0x6c..0x6f` absent in both carved tables]

> **GOTCHA (`MatchReplace` vs `MatchReplace8`).** The SEQ dispatch token is literally
> `"S: MatchReplace\n"` (no `8`), even though the ISA mnemonic is `MATCH_REPLACE8` and the
> validator is `is_valid_match_replace8`. A name-based reimplementation that searches for
> `"MatchReplace8"` in the rodata will miss the dispatch token. The opcode byte `0x6f` is
> the stable join key, not the string. [HIGH/OBSERVED]

---

## 8. Per-gen PROF arming (the IMG‑27 layer)

IMG‑27's `"+10 PROF arm"` carve lists `0x6c/0x6d/0x6e/0x6f` among DVE opcodes
**newly PROF-armed at MAVERICK** as part of the ACT→DVE *fold* — a change to the DVE
**PROF dispatch table** (a runtime dispatch-enable), **not opcode-space growth**. Reconciled
against the ISA-header layer decoded here:

- **CONFIRMED:** the opcode bytes are stable across all four gens; the engine is DVE; the
  MAVERICK event is a DVE PROF **re-arm** (runtime), not a new opcode. [HIGH/OBSERVED for
  bytes + engine; PROF carve CARRIED from IMG‑27]
- **CORRECTED:** the "pre-existing MARIANA / first appears at MARIANA" sub-claim is
  inaccurate at the ISA layer — SUNDA and CAYMAN headers already define all four
  (`// Y` + struct map). ISA-DEFINED from SUNDA; only the DVE PROF arming changes at
  MARIANA/MAVERICK. The two are different layers (§3 CORRECTION). [HIGH/OBSERVED]

> **NOTE (what "PROF arming" means for a reimplementation).** Because there is no software
> kernel body, "arming" an op like `FindIndex8` for a generation is not loading a
> kernel — it is **enabling the DVE PROF-table entry** that routes `header.opcode==0x6e`
> into the DVE search datapath. A from-scratch DVE that already decodes the four opcodes
> needs no per-gen kernel; the per-gen variation is purely the dispatch-enable table the
> firmware programs. The ISA contract (structs, validators, semantics) is **identical**
> across all four gens (`gcc` compile-verify: byte-identical 64 B structs). [HIGH for the
> struct identity; PROF-table mechanism CARRIED/INFERRED from IMG‑27]

---

## 9. Dtype matrix

Dtype enum codes from `aws_neuron_isa_tpb_common.h:705–720` (the `NEURON_ISA_TPB_DTYPE`
packed enum):

| code | dtype | | code | dtype |
|---|---|---|---|---|
| `0x0` | INVALID | | `0x8` | INT32 |
| `0x1` | UINT64 | | `0x9` | UINT32 |
| `0x2` | INT8 | | `0xA` | FP32 |
| `0x3` | UINT8 | | `0xB` | FP32R |
| `0x4` | INT16 | | `0xC` | INT64 |
| `0x5` | UINT16 | | `0xD` | FP8_EXP3 |
| `0x6` | BFLOAT16 | | `0xE` | FP8_EXP4 |
| `0x7` | FP16 | | `0xF` | FP8_EXP5 |

`is_valid_dtype(d, FP32R::False)` = `d ∉ {INVALID, FP32R, UINT64, INT64}`
(`dtype_invalid_check && dtype_fp32r_illegal_check && dtype_uint64_illegal_check &&
dtype_int64_illegal_check`, `common.h:1437–1442`). `is_valid_dtype(d, FP32R::True)` = the
same set with FP32R re-permitted (output-only).

| opcode | `in_dtype` | `out_dtype` |
|--------|------------|-------------|
| `MATCH_VALUE_LOAD` (0x6d) | any `is_valid_dtype(False)` — the **general** set (NOT the bn-only `{fp32,fp16,bf16}` restriction that gates `BatchNormParamLoad`) | n/a (no dst; keys held fp32 in state) |
| `MAX8` (0x6c) | any `is_valid_dtype(False)`, → fp32 internally | any `is_valid_dtype(True)` — **values**; **FP32R allowed** |
| `FIND_INDEX8` (0x6e) | any `is_valid_dtype(False)`, → fp32 internally | **`UINT16` or `UINT32` only** (`has_find_index8_dst_type`) |
| `MATCH_REPLACE8` (0x6f) | explicit 7-dtype list `\|` `is_valid_dtype(False)`: `FP16 / BFLOAT16 / UINT8 / UINT16 / UINT32 / FP32 / INT32` | same 7-dtype list (**FP32R rejected** by the AND'd `match_replace_dtypes` list, even though `is_valid_dtype(out,True)` alone would permit it) |

Element-count and zero-field requirements:

| opcode | src elem count | dst elem count | forced-zero fields |
|--------|----------------|----------------|--------------------|
| `MATCH_VALUE_LOAD` | **== 8** (shape product) | — | `imm0`, `imm1`, `imm0_src`, all reserved |
| `MAX8` | `≥ 8` and `≤ 16384` | **== 8** (values) | `even_count==odd_count==0`, reserved |
| `FIND_INDEX8` | `≥ 8` and `≤ 16384` | **== 8** (indices) | `even_count==odd_count==0`, reserved |
| `MATCH_REPLACE8` | `≤ 16384` | **== src count** (1:1) | `reserved0[5]==0` (`immediate` is live) |

> **CORRECTION (vs GX‑OP‑01 §4c wording).** GX‑OP‑01 lists the MatchReplace8 `in_dtype`
> 7-list as `FP16/BF16/UINT8/UINT16/UINT32/FP32/INT32`. The header `match_replace_dtypes`
> body (`s4d4_mr.h:134–149`) confirms `UINT32` **is** in both the in and out list — so the
> 7-dtype set is the same for input and output. The SX‑FW‑44 dtype-contrast line (its §9)
> dropped `UINT32` from the in-list shorthand; the authoritative set is the 7-list above
> for **both** directions. [HIGH/OBSERVED — read from the validator body]

---

## 10. The complete beam-search pipeline

The four contiguous opcodes form one cooperating DVE-native search/select family, all
sharing the **"8" width** (8 keys / 8 outputs) and the **DVE engine** with **no `Q7_POOL`
kernel**. The two stateful consumers (`FindIndex8`, `MatchReplace8`) read the `mv[0..7]`
keys deposited by `MatchValueLoad`; `Max8` is the top‑8 producer.

| stage | opcode | struct | role in a beam-search round |
|-------|--------|--------|-----------------------------|
| **P** | `MAX8` (0x6c) | `S4D2_BN` | reduce the score stream → top‑8 **values** (smallest→largest) — the beam's 8 best scores |
| **L** | `MATCH_VALUE_LOAD` (0x6d) | `S2_BN` | load those 8 values as keys `mv[0..7]` (reversed `src[k]→mv[7-k]`, so `mv[0]`=largest) |
| **I** | `FIND_INDEX8` (0x6e) | `S4D2_BN` | scan the original score stream; per key emit the **index** of its first exact match → 8 indices (u16/u32) |
| **R** | `MATCH_REPLACE8` (0x6f) | `S4D4_MR` | re-scan; replace each first-match with the fp32 `immediate` (e.g. `-inf`), pass the rest through — blanks out the chosen top‑8 so the next `MAX8` finds the following 8 |

```text
  1. MAX8(scores)              -> top8_values                  [producer]
  2. MatchValueLoad(top8)      -> DVE mv[0..7]                 [stage; reversed map]
  3. FindIndex8(scores)        -> top8_indices  (u16/u32)      [recover positions]
       └─ indices feed a downstream GATHER / INDIRECT_COPY (see §11) to fetch the
          winning beam states.
  4. MatchReplace8(scores, imm=-inf) -> scores'                [k-extension]
       └─ GOTO 1 on scores' for the next 8 (top-16, top-24, …)
```

The ISA headers name the use directly: `FindIndex8` for *"beam-search top‑8 index
recovery"* (`s4d2_bn.h:203–217`); both `FindIndex8` and `MatchReplace8` carry *"extend
beam-search to more than 8 values"* and *"bubble-sort"* todo-notes (`s4d2_bn.h:224–228`,
`s4d4_mr.h:44–48`). The shared match core (exact-equality, first-match,
lowest-`n`-arbitration) is identical between `FindIndex8` and `MatchReplace8`; they differ
only in the **output** (8 indices vs a 1:1 replaced stream). `Max8` is the reduction
sibling on the same struct as `FindIndex8`; `MatchValueLoad` is the loader sibling,
structurally the FW‑57 ImmLd loader. [per-op semantics HIGH/OBSERVED; family being a
beam-search cluster HIGH (named in spec); the exact `Max8→MatchValueLoad` and
`Index→gather` wiring MED/INFERRED — the headers imply but do not fix it as a pipeline]

---

## 11. Cross-links & the wider idiom

- **[`../../isa/ref/b19-scatter-gather.md`](../../isa/ref/b19-scatter-gather.md)** — the
  SuperGather ISA batch. `FindIndex8`'s 8 emitted indices are exactly the index vector a
  subsequent `GATHER` (`S4D4_GT`) / `INDIRECT_COPY` (`S4D4_IC`) consumes to fetch the
  winning beam states. [consumer link INFERRED — structural]
- **[`dve-read-state.md`](./dve-read-state.md)** (planned) — `DveReadAccumulator`/
  `DveReadIndices` read back DVE per-channel state; the SEQ table shows
  `DveReadAccumulator` adjacent to `FindIndex8` in the dispatch order, the read-side
  counterpart to the `mv[]`-state write side here.
- **[`nonzero-with-count.md`](./nonzero-with-count.md)** (planned) — the **unbounded,
  predicate(!=0), all-match** analog. `NonzeroWithCount` (`0xf2`, struct
  `S3D3_NONZERO_WITH_COUNT`) emits a **variable** index-list **plus a count**; it **is** a
  `Q7_POOL` software kernel (it has a `kernel_info_table` funcVA — `0xf2` *is* in the
  CAYMAN table above). `FindIndex8` is the **bounded-k=8, keyed (equality-to-given-values),
  first-match** analog with a **fixed** 8-wide output and **no count slot**. Same
  `compare → vbool predicate → emit-index` idiom, opposite output discipline.
- **VAL‑13** — the live value-core differential. The compare/reduce/select primitives this
  page names (`ivp_oeqn_2xf32`, `ivp_maxn_2xf32`/`xdref_rmaxnum_n_2x32f`,
  `ivp_seln_2x32`) are members of the 864-function `libfiss-base.so` value roster VAL‑13
  tracks; a live drive of these ISS modules requires the ncore2gp engine-state context
  (a bare `ctypes` call returns no result without it), so this page anchors the value
  semantics to the **symbol naming + ISA header text**, not a standalone live result.
  [primitive presence HIGH/OBSERVED via `nm`; standalone live-drive LOW/not-established]

---

## 12. Confidence / provenance summary

**HIGH/OBSERVED**

- opcodes `0x6c/0x6d/0x6e/0x6f`, `// Y` maintained, all 4 gen `common.h` enums (byte-pinned)
- struct map `S2_BN`(0x6d) / `S4D2_BN`(0x6c,0x6e) / `S4D4_MR`(0x6f) vs POOL `S4D4_PL` —
  `struct2opcode`, all 4 gens
- all four structs 64 B, every offset (`gcc` compile-verify; `immediate`@40 in S4D4_MR,
  `even_count`/`odd_count`@40/44 forced 0 in S4D2_BN, reversed key map in S2_BN)
- semantics: Max8 = top‑8 values smallest→largest; MatchValueLoad = 8 keys reversed
  `src[k]→mv[7-k]`; FindIndex8 = 8-key first-match index emit, u16/u32, `≥32768` sentinel;
  MatchReplace8 = first-match → fp32 immediate, else pass-through, 1:1
- DVE-engine, **no software body**: cluster absent from carved SUNDA(18)/CAYMAN(17)
  `kernel_info_table`; struct group is DVE family not POOL
- SEQ-token multiplicity: MVL/MatchReplace/FindIndex8 = **4** each; **Max8 = 0** (verified
  `rg -ao` this task) — Max8 ISA-present everywhere, in-corpus-emittable nowhere
- ISA-present + maintained in all 4 gens SUNDA(NC‑v2)..MAVERICK(NC‑v5)

**MED/INFERRED**

- exact DVE lane-parallel micro-architecture of the scan / top‑8 reduction (single-pass
  O(N) inferred)
- `Max8 → MatchValueLoad` as the beam-search producer→key handoff (cluster co-design +
  shared "8" width + reversal composition; not spelled out as a fixed pipeline)
- `MatchReplace8 imm=-inf` as the >8 beam k-extension blank-out (use-notes name the
  extension; exact wiring inferred)
- `FindIndex8` indices → `S4D4_GT GATHER` / `S4D4_IC INDIRECT_COPY` consumer (structural)
- WHY `Max8` has no in-corpus emitter (out-of-corpus front-end vs reserved/staged)

**CARRIED**

- the MAVERICK DVE PROF re-arm mechanism (IMG‑27 carve); the ISA-definition layer is
  OBSERVED here, the PROF-table interior is carried/inferred

**CORRECTION OF PREMISE (HIGH/OBSERVED)**

- IMG‑27's "pre-existing MARIANA / first appears at MARIANA" framing for the cluster is
  refuted at the ISA layer: all four are ISA-defined + maintained from **SUNDA**; the
  MARIANA/MAVERICK event is DVE **PROF arming** (runtime), a different layer.
- MatchReplace8's `in_dtype` 7-list includes `UINT32` (per the validator body), matching
  its `out_dtype` list — the SX‑FW‑44 §9 shorthand that dropped `UINT32` from the in-list
  is corrected here.
