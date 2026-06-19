# DVE State Read-Back (`DveReadIndices` `0xe9` / `DveReadAccumulator` `0x9b`)

> **Scope.** This page decodes the **two DVE-state read-back ops** that *drain* the hidden
> per-lane pipeline state the rest of the DVE compute family leaves in flops:
> **`DveReadIndices` (opcode `0xe9`)** and **`DveReadAccumulator` (opcode `0x9b`)**. They are
> the read side of two write-side producer clusters already documented on this wiki: the
> **search/select cluster** ([`search-cluster.md`](search-cluster.md) — `FindIndex8` `0x6e` /
> `MatchReplace8` `0x6f` / `Max8` `0x6c`, keyed by `MatchValueLoad` `0x6d`) leaves the 8
> per-lane **argmax/match index** flops that `DveReadIndices` spills; the **cache-reduce
> family** ([`ts-cache-reduce.md`](ts-cache-reduce.md) `0x9a` /
> [`ts-cache-cumulative.md`](ts-cache-cumulative.md) `0xe6` / `SelectReduce` `0xea`) leaves the
> per-lane **fp32 running accumulator** that `DveReadAccumulator` spills.
>
> The page pins: the byte-exact opcode enums and per-gen presence; the `struct2opcode`
> bindings (`D1_RD` shared with the ACT read-accumulator; `D4_MR` shared with the
> memset-family writers); the **compile-verified** operand structs (offsets + `sizeof == 64`);
> the read-back **semantics** — exactly *which* internal flop state each op drains and the
> output format it writes; the DVE-native dispatch (no `Q7_POOL` kernel hop); the MAVERICK
> dispatch-table → `call8` → worker trampoline; and the **MAVERICK ACT→DVE fold** under which
> the ACT engine's read-accumulator is re-expressed as the DVE-native `DveReadAccumulator`.
>
> **The carried correction this page owns.** The optional **`negated`** flag is a
> **`DveReadAccumulator`** field, **not** an `ActivationReadAccumulator` field — the ACT
> read-accumulator (`0x24`) validator *forbids* negate (`negated == 0`); the DVE
> read-accumulator *permits* it. §5 documents `negated` here as a DVE field and §8 distinguishes
> the two same-format read-accumulators so they are never conflated.
>
> Confidence tags use the `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` model defined in
> [`../../reference/confidence-model.md`](../../reference/confidence-model.md). Per the standing
> rule: **v2–v4 (sunda/cayman/mariana) are byte-grounded; v5 (MAVERICK) interiors are flagged
> INFERRED** — the v5 enum/struct surface is OBSERVED, its worker-body compute is not.

---

## 1. TL;DR — the pinned facts

| # | Fact | Evidence | Tag |
|---|------|----------|-----|
| 1 | `DveReadAccumulator` = **opcode `0x9b`**, `// Y` maintained, present in **all four** gens (`sunda:237 cayman:234 mariana:239 maverick:242`); binds the **64-B** `NEURON_ISA_TPB_D1_RD_STRUCT`. | `common.h` enum (4 gens); `instruction_mapping.json` `struct2opcode` | HIGH/OBSERVED |
| 2 | `DveReadIndices` = **opcode `0xe9`**, `// Y` maintained, present **CAYMAN..MAVERICK** (`cayman:299 mariana:309 maverick:315`) — **ABSENT from SUNDA** (no enum entry; not in sunda `d4_mr.h`); binds the **64-B** `NEURON_ISA_TPB_D4_MR_STRUCT`. | `common.h` (4 gens); sunda `d4_mr.h` absence | HIGH/OBSERVED |
| 3 | **`DveReadAccumulator` drains the per-lane fp32 REDUCE accumulator** — one scalar per DVE lane, the running value left by `CacheReduce`/`CacheCumulative`/`SelectReduce` via their `accumulator_cmd` — to a **1-element** 1-D dst (`dst_element_count == 1`), optionally **negated**, fp output incl **FP8**. | `d1_rd.h:37–49` verbatim + validator | HIGH/OBSERVED; "is the reduce accumulator, not wvec" MED/INFERRED |
| 4 | **`DveReadIndices` drains the 8 per-lane INDEX flops** the search cluster leaves — the header says verbatim *"8 indices per lane stored after MATCH_REPLACE8 instruction"* — to an **8-element** 4-D dst (`dst_element_count == 8`), **UINT16/UINT32 only** (the same index dtype as `FindIndex8`). | `d4_mr.h:49–54` verbatim + validator | HIGH/OBSERVED |
| 5 | **The `negated` flag belongs to `DveReadAccumulator`, NOT `ActivationReadAccumulator`.** Both ride `D1_RD`; the validators diverge — ACT (`0x24`) requires `d1_rd_has_zero_negated_field` (`negated == 0`); DVE (`0x9b`) uses `d1_rd_has_valid_negated_field` (`negated ∈ {0,1}`). | `d1_rd.h:87/101` (the two validators) | HIGH/OBSERVED |
| 6 | **Both are DVE-engine, hardware-native** — neither is a `Q7_POOL` software kernel. Proven by (i) absence from the carved SUNDA(18)/CAYMAN(17) `kernel_info_table` and (ii) the 4-copy DVE `"S: DveRead*"` self-name multiplicity (vs the 3-copy ACT class). | carved POOL tables; `rg -ao` over `libnrtucode_internal.so` | HIGH/OBSERVED |
| 7 | **MAVERICK ACT→DVE fold.** On v5 the standalone ACT read-accumulator handler image is gone; `DveReadAccumulator` is the renamed DVE-native survivor (`ActivationReadAccumulator` has **no** MAVERICK-band self-name copy). | per-band self-name carve; `activate-pwl.md §1/§6` | HIGH/OBSERVED region; v5 interior INFERRED |
| 8 | Both structs are **64 B, byte-identical** across all four gens (`gcc` compile-verify, `ISA_STATIC_ASSERT == 64`); `D1_RD` shared 1:1 with `ActivationReadAccumulator`, `D4_MR` shared with `Memset`/`Rng`/`RegStore`. | header `ISA_STATIC_ASSERT` + `instruction_mapping.json` | HIGH/OBSERVED |

---

## 2. Provenance / carve anchors

All facts derive from static analysis of the shipped **host customop-lib ISA C headers**, the
shipped per-gen **`instruction_mapping.json`**, the shipped **`libnrtucode_internal.so`** SEQ-ASCII
rodata + carved firmware blobs read via the **Cadence ncore2gp** `xtensa-elf-readelf`/`objdump`
(`XTENSA_CORE=ncore2gp`, Vision-Q7 FLIX/VLIW), the **FLIX decoder**, and `gcc` struct
compile-verify. No vendor source was consulted. Lawful interoperability RE (DMCA 17 U.S.C.
§1201(f)).

| Artifact | Path / value |
|----------|--------------|
| ISA headers (4 gens) | `…/custom_op/c10/include/neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/aws_neuron_isa_tpb_{common,d1_rd,d4_mr}.h` |
| Struct→opcode map | `…/tpb/instruction_mapping.json` (`struct2opcode`), per gen |
| SEQ-token / self-name container | `…/custom_op/c10/lib/libnrtucode_internal.so` (the `"S: <Name>"` DVE self-names) |
| `xtensa-elf-readelf`/`objdump` | `gpsimd_tools/tools/XtensaTools/bin/`, `XTENSA_CORE=ncore2gp` |
| Carved POOL images | `extisa_{SUNDA,CAYMAN}_POOL_PERF_EXTISA_0.so` — `kernel_info_table` SUNDA `@file 0xb260/0x90`, CAYMAN `@0x7400/0x88` |
| MAVERICK DVE carve | `NX_DVE_DEBUG_DRAM` dispatch table `@flat off 0x820`; `NX_DVE_DEBUG_IRAM` trampolines `0x2eff`/`0x2f1f`; `NX_DVE_PROF_CAM` |
| Backing report | **GX-OP-06** (`raw/GX-OP-06_dve_read_state.txt`) — anchored to FW-46 (DVE blob roster), FW-62 (`SelectReduce`/`CopyPredicatedReduce` accumulator producer), GX-OP-01 + FW-44 (the index producer cluster), FW-54/55 (the cache accumulator producers), GX-MAV-03 (the ACT→DVE fold + MAVERICK dispatch table), ISS-06 (`wvec`-vs-DVE-accumulator) |

> **NOTE (where FLIX desync does and does not bite).** Every **primary** fact below — the opcode
> enums, the `D1_RD`/`D4_MR` struct layouts (compile-verified), the validator pseudocode
> (verbatim header text), the `instruction_mapping.json` bindings, the `kernel_info_table` hex
> dumps (read as **data** via `readelf`, not the code-stream decoder), and the
> `libnrtucode_internal.so` self-name offsets — depends on **no FLIX decode at all**. Only the
> MAVERICK dispatch *confirmation* (§7) crosses the FLIX decoder, and only on the clean
> control-spine bundles (`call8` + `entry`), not desynced literal-pool interiors. The standing
> ncore2gp `op0=e/f` FLIX-vs-LX artifact does not touch the struct/enum/semantics facts.
> [HIGH — no code-stream desync on the primary facts]

---

## 3. The opcodes — byte-exact, all four gens

From the `NEURON_ISA_TPB_OPCODE` enum in `aws_neuron_isa_tpb_common.h`. The enum is
`__attribute__((__packed__))`, so the opcode is a single byte at offset 0 of the 4-byte
`NEURON_ISA_TPB_HEADER`. The trailing `// Y` column means *"Tested/Maintained/Not deprecated?"*;
deprecated ops carry `// n`. Both read-back ops are `// Y`:

```c
NEURON_ISA_TPB_OPCODE_DVE_READ_ACCUMULATOR        = 0x9b,    // Y   <- drains the per-lane fp32 reduce accumulator
NEURON_ISA_TPB_OPCODE_DVE_READ_INDICES            = 0xe9,    // Y   <- drains the 8 per-lane search index flops
NEURON_ISA_TPB_OPCODE_ACTIVATION_READ_ACCUMULATOR = 0x24,    // Y   <- the ACT sibling of 0x9b (same D1_RD; §8)
```

**Enum neighborhood of `0x9b`** (sunda — its accumulator producer sits one byte before it):

```c
0x98 TENSOR_SCALAR_SELECT          // Y
0x99 CAST_PREDICATED               // Y
0x9a TENSOR_SCALAR_CACHE_REDUCE    // Y   <- a PRODUCER of the DVE accumulator (ts-cache-reduce.md)
0x9b DVE_READ_ACCUMULATOR          // Y   <- THIS op (drains the accumulator)
0x9c TENSOR_REDUCE_RANGE_CHECK     // n   (deprecated)
0x9d SCALAR_TENSOR_TENSOR_ARITH    // Y
```

`0x9b` is encoded **immediately after** its cache-reduce producer `0x9a` — the drain sits beside
the source. [HIGH/OBSERVED]

**Enum neighborhood of `0xe9`** (cayman — the index/gather/select-reduce cluster):

```c
0xe5 TENSOR_TENSOR_SCAN_ARITH       // Y
0xe6 TENSOR_SCALAR_CACHE_CUMULATIVE // Y   <- a PRODUCER of the DVE accumulator (ts-cache-cumulative.md)
0xe7 INDIRECT_COPY                  // Y   <- the index CONSUMER (gather)
0xe8 COPY_PREDICATED_SCALAR         // Y
0xe9 DVE_READ_INDICES               // Y   <- THIS op (drains the index flops)
0xea SELECT_REDUCE                  // Y   <- CopyPredicatedReduce — an accumulator producer (FW-62)
```

`0xe9` sits between the cache/scan accumulator producers and `SelectReduce`, **beside
`INDIRECT_COPY`** (`0xe7`) — the gather that ultimately consumes the indices `DveReadIndices`
spills. [HIGH/OBSERVED]

### 3.1 The per-gen existence split (the one asymmetry)

| gen | NC-ver | `0x9b` DveReadAccumulator | `0xe9` DveReadIndices |
|-----|--------|---------------------------|------------------------|
| sunda | NC-v2 | `// Y` (L237), `D1_RD` map | **ABSENT** (no enum; not in sunda `d4_mr.h`) |
| cayman | NC-v3 | `// Y` (L234), `D1_RD` map | `// Y` (L299), `D4_MR` map |
| mariana | NC-v4 | `// Y` (L239), `D1_RD` map | `// Y` (L309), `D4_MR` map |
| maverick | NC-v5 | `// Y` (L242), `D1_RD` map | `// Y` (L315), `D4_MR` map |

`DveReadAccumulator` (`0x9b`) is **ISA-defined from SUNDA (NC-v2)**, already sharing `D1_RD` with
the ACT read-accumulator. `DveReadIndices` (`0xe9`) **first appears at CAYMAN (NC-v3)** — a
direct `rg` of the SUNDA `common.h` returns no `DVE_READ_INDICES`, and the SUNDA `d4_mr.h`
validator block does not list `dve_read_indices`. This mirrors the index producer cluster: the
`MatchReplace8`/`FindIndex8` search cluster reaches its mature, read-back-requiring form at CAYMAN.
[HIGH/OBSERVED — verified across all four `common.h` + the sunda `d4_mr.h`]

> **CORRECTION (per-gen, vs a naive "DVE read-backs are a MAVERICK feature" reading).** Both
> read-backs predate MAVERICK. `0x9b` is present **from SUNDA**; `0xe9` **from CAYMAN**. What is
> MAVERICK-specific is *not* opcode-space growth but the **ACT→DVE fold** (§9): on v5 the
> standalone ACT read-accumulator handler is gone and `DveReadAccumulator` is its renamed
> DVE-native survivor. The opcode byte `0x9b` has been valid since NC-v2. [HIGH/OBSERVED]

### 3.2 The `struct2opcode` bindings (`instruction_mapping.json`)

```jsonc
// D1_RD — the "read one accumulator value per lane to a 1-element 1-D dst" struct, shared 1:1
"NEURON_ISA_TPB_D1_RD_STRUCT": [ ACTIVATION_READ_ACCUMULATOR,   // 0x24 (ACT engine; activate-pwl.md)
                                 DVE_READ_ACCUMULATOR ],         // 0x9b (THIS — DVE engine)

// D4_MR — the "one 4-D DST" memset-family struct, shared with the memory writers
"NEURON_ISA_TPB_D4_MR_STRUCT": [ MEMSET, REG_STORE, RNG,
                                 DVE_READ_INDICES ]              // 0xe9 (THIS — cayman..maverick)
```

`DveReadAccumulator` rides `D1_RD`, decode-compatible with the ACT read-accumulator — the only
structural difference is the **opcode predicate** plus the DVE-side **`negated`** and **FP8**
extensions (§5/§8). `DveReadIndices` rides `D4_MR`, the **destination-only** memset-family
descriptor: it has **no source operand** because the source is the hidden DVE index state — it is
structurally a *"drain the index flops to a dst tensor"* writer, which is why it borrows the
`Memset`/`Rng`/`RegStore` struct. The SUNDA `D4_MR` list omits `DVE_READ_INDICES` (the struct still
ships there for `Memset`/`Rng`/`RegStore`). [HIGH/OBSERVED — `jq` over all 4 per-gen maps]

---

## 4. The operand structs — offset-exact, `gcc` compile-verified

Both structs are 64 B with `ISA_STATIC_ASSERT(sizeof == 64)`; offsets are **byte-identical**
across sunda/cayman/mariana/maverick (compile-verified). Offsets read verbatim from the headers'
trailing `// size (range)` comments.

### 4.1 `DveReadAccumulator` (`0x9b`) → `NEURON_ISA_TPB_D1_RD_STRUCT` (64 B)

Header line (`d1_rd.h:14–18`): *"Neuron 'D1_RD' Format … one 1-D DST Tensor. Use for:
ActivationReadAccumulator, DveReadAccumulator."*

| off | size | field | type | `DveReadAccumulator` role |
|----:|-----:|-------|------|---------------------------|
| 0 | 4 | `header` | `HEADER` | `opcode = 0x9b` |
| 4 | 8 | `events` | `EVENTS` | wait/update semaphore sync |
| 12 | 16 | `reserved0[16]` | `u8` | **must be 0** (`d1_rd_reserved_zero`) |
| 28 | 4 | `dst_element_count` | `u32` | **must == 1** (`read_accumulator_dst_size`) |
| 32 | 1 | `dtype` | `DTYPE` | output dtype — fp datapath incl **FP8** (§6) |
| 33 | 1 | **`negated`** | `u8` | **0 or 1 — OPTIONAL NEGATE** of the drained value (DVE-only; see CORRECTION §8) |
| 34 | 1 | `num_active_channels` | `u8` | DVE lane count `1..128` (`POOLING_NUM_CHANNELS`) |
| 35 | 9 | `reserved1[9]` | `u8` | **must be 0** |
| 44 | 8 | `dst_mem_pattern` | `TENSOR1D` | the 1-element 1-D output (`num_elem[0] == 1`) |
| 52 | 12 | `reserved2[12]` | `u8` | **must be 0** |

`compile-verify (4 gens)`: `sizeof == 64`; `header=0 events=4 reserved0=12 dst_element_count=28
dtype=32 negated=33 num_active_channels=34 reserved1=35 dst_mem_pattern=44 reserved2=52`.
[HIGH/OBSERVED]

### 4.2 `DveReadIndices` (`0xe9`) → `NEURON_ISA_TPB_D4_MR_STRUCT` (64 B)

Header line (`d4_mr.h`): *"one 4-D DST Tensor. Use for: Memset, Rng, RegStore"* plus the
`DVE_READ_INDICES` note (`d4_mr.h:49`): *"DveReadIndices 8 indices per lane stored after
MATCH_REPLACE8 instruction."*

| off | size | field | type | `DveReadIndices` role |
|----:|-----:|-------|------|-----------------------|
| 0 | 4 | `header` | `HEADER` | `opcode = 0xe9` |
| 4 | 8 | `events` | `EVENTS` | wait/update semaphore sync |
| 12 | 16 | `reserved0[16]` | `u8` | **must be 0** (`d4_mr_reserved_zero`) |
| 28 | 4 | `dst_element_count` | `u32` | **must == 8** (`dve_read_indices_elem_count_8`) |
| 32 | 1 | `dtype` | `DTYPE` | output dtype — **UINT16 or UINT32 only** (§6) |
| 33 | 1 | `reserved1[1]` | `u8` | **must be 0** (here, the slot `D1_RD` uses for `negated` — there is **no negate** on an integer index read) |
| 34 | 1 | `num_active_channels` | `u8` | DVE lane count `1..128` (`DVE_NUM_CHANNELS`) |
| 35 | 1 | `reserved2[1]` | `u8` | **must be 0** |
| 36 | 1 | `serialization_mode` | `REG_SERIAL_MODE` | **must == Serial** (`d4_mr_zero_serialization`) |
| 37 | 3 | `reserved3[3]` | `u8` | **must be 0** |
| 40 | 4 | `set_value` | `u32` | **must == 0** (memset-family field, unused for read) |
| 44 | 20 | `dst_mem_pattern` | `TENSOR4D` | the 8-element output (`dst_element_count == 8`) |

`compile-verify (4 gens)`: `sizeof == 64`; `header=0 events=4 reserved0=12 dst_element_count=28
dtype=32 reserved1=33 num_active_channels=34 reserved2=35 serialization_mode=36 reserved3=37
set_value=40 dst_mem_pattern=44`. (SUNDA compiles the struct for `Memset`/`Rng`/`RegStore`, but
`0xe9` is not a legal opcode there.) [HIGH/OBSERVED]

> **GOTCHA (offset 33 is `negated` on `D1_RD`, but `reserved1` on `D4_MR`).** The two read-back
> structs put **different** meanings at byte 33: on `D1_RD` (the accumulator read) it is the live
> `negated` flag; on `D4_MR` (the index read) it is a **forced-zero reserved** byte. A
> reimplementer porting the negate concept across the two ops will write a non-zero byte 33 on a
> `DveReadIndices` and **fail validation** (`d4_mr_reserved_zero`). Negate is meaningful only for
> the floating-point accumulator value; an integer index has no sign to flip. [HIGH/OBSERVED]

---

## 5. Semantics — `DveReadAccumulator` (`0x9b`): drain the reduce accumulator

Header purpose (`d1_rd.h:37–49`, verbatim): *"Read the fp32 value of the accumulator in each lane
to a 1-D dst tensor with a single element … Output can optionally be negated by setting negate to
1 … the legal values for the output dtype are: FP16, BFLOAT16, FP32, FP32R, FP8."*

So `DveReadAccumulator`:

- **Reads the per-lane fp32 ACCUMULATOR** — one scalar value per active DVE channel — and spills
  it to a **1-element 1-D dst** (`dst_element_count == 1`, `num_elem[0] == 1`). This is the
  **drain** of the per-lane running accumulator the cache-reduce / select-reduce family leaves in
  DVE flops (the producer chain is §7). [HIGH/OBSERVED]
- **Optionally negates** the read-out value via the `negated` field (off 33, `0` or `1`). When
  `1`, the spilled value is sign-flipped — useful when the accumulator holds a running `min`/`sum`
  the downstream consumer wants negated (e.g. converting a tracked `−max` back to `+max`). This is
  the **only** compute facet beyond a bare read. [HIGH/OBSERVED]
- **Narrows to the output dtype** on read: the accumulator is held fp32 internally and converted
  to `{FP8e3, FP8e4, FP8e5, FP16, BF16, FP32, FP32R}` (§6). [HIGH/OBSERVED]

```c
// is_valid_dve_read_accumulator (d1_rd.h:90–102) gates entry:
//   header/events valid; opcode == 0x9b; dve_read_accumulator_type_check (fp set incl FP8);
//   active channels <= POOLING_NUM_CHANNELS(128); tensor1d write valid (PSUM+SBUF);
//   read_accumulator_dst_size (dst_element_count == 1); d1_rd_has_valid_negated_field (negated in {0,1}).
//
// DVE per-lane state (one running fp32 accumulator per active channel), left by the
// CacheReduce/CacheCumulative/SelectReduce accumulator_cmd family (search §7):
//   float dve_reduce_accumulator[DVE_NUM_CHANNELS];   // persists across instructions until re-seeded
void dve_read_accumulator(const NEURON_ISA_TPB_D1_RD_STRUCT *r) {   // opcode 0x9b
    for (int ch = 0; ch < r->num_active_channels; ch++) {           // one fp32 per active lane
        float acc = dve_reduce_accumulator[ch];                     // the drained per-lane reduce accumulator
        if (r->negated) acc = -acc;                                 // OPTIONAL negate — DVE-only field (off 33)
        // dst_element_count == 1; single-element 1-D write, narrowed to the output dtype:
        store_1d(r->dst_mem_pattern, ch, from_fp32(acc, r->dtype)); // {FP8/FP16/BF16/FP32/FP32R}
    }
    // NO source operand; NO Q7_POOL funcVA hop — the "source" is the hidden DVE accumulator flop.
}
```

> **NOTE (which accumulator — the reduce accumulator, not the `wvec` MAC file).** The value read
> is a **single fp32 per lane** to a 1-element dst. This is the DVE per-lane **reduce**
> accumulator (one running fp32 per partition), **not** the wide PE/matmul MAC accumulator. ISS-06
> pins the MAC `wvec` file at **4 registers × 1536-bit** (paired with the `b32_pr` MAC predicate);
> a 1536-bit `wvec` is not a single fp32, and the `D1_RD` geometry is exactly 1 fp32/lane — so
> `DveReadAccumulator` drains the DVE reduce accumulator, a **separate** flop from `wvec`. [the
> 1-fp32/lane read HIGH/OBSERVED; "separate from `wvec`, it is the DVE reduce accumulator"
> MED/INFERRED — the header says *"the accumulator in each lane"*, the geometry is 1 value/lane,
> and the producers (§7) are the DVE reduce family that maintains exactly such a per-lane running
> accumulator via `accumulator_cmd`]

---

## 6. Semantics — `DveReadIndices` (`0xe9`): drain the search index flops

Header purpose (`d4_mr.h:49–54`, verbatim): *"DveReadIndices 8 indices per lane stored after
MATCH_REPLACE8 instruction. Following access pattern restrictions are required:
dst_element_count == 8. The legal values for the output dtype are: UINT16, UINT32."*

So `DveReadIndices`:

- **Reads back the 8 per-lane INDEX flops** the search cluster leaves in DVE state and writes them
  to an **8-element dst** tensor (`dst_element_count == 8`, `dve_read_indices_elem_count_8`).
  [HIGH/OBSERVED]
- **Output dtype is UINT16 or UINT32 only** (`is_valid_dtype_dve_read_indices`) — the **same**
  index dtype set as `FindIndex8`'s `out_dtype` (`has_find_index8_dst_type`,
  [`search-cluster.md §5.2/§6.3`](search-cluster.md)). The indices are flat positions; `FindIndex8`
  uses the `>= 32768` (u16) / `0xFFFFFFFF` (u32) **no-match sentinel**, which `DveReadIndices` reads
  back verbatim. [dtype gate HIGH/OBSERVED; sentinel carry-through INFERRED-HIGH from the shared
  u16/u32 index encoding]
- **Validates `num_active_channels` against `DVE_NUM_CHANNELS` (128)** — *not*
  `POOLING_NUM_CHANNELS` — the one validator clause that explicitly names the DVE channel constant,
  reinforcing the DVE-engine assignment. [HIGH/OBSERVED]
- **`set_value == 0`, `serialization_mode == Serial`, all reserved zero** — it borrows the
  memset-family `D4_MR` struct purely as a destination-tensor descriptor; the "source" is the
  hidden DVE index state. [HIGH/OBSERVED]

```c
// is_valid_dve_read_indices (d4_mr.h:157–171) gates entry:
//   header/events valid; opcode == 0xe9; reserved-zero; d4_mr_zero_serialization (Serial);
//   is_valid_dtype_dve_read_indices (UINT16 || UINT32); channels <= DVE_NUM_CHANNELS(128);
//   tensor4d write valid; dve_read_indices_elem_count_8 (dst_element_count == 8); set_value == 0.
//
// DVE per-lane index state: the 8 first-match positions the search cluster records in flops
// (search-cluster.md §6.3/§6.4 — FindIndex8 0x6e fills them, MatchReplace8 0x6f leaves them):
//   uint32_t dve_index_flops[DVE_NUM_CHANNELS][8];   // u16/u32-range positions; >=32768/0xFFFFFFFF = no match
void dve_read_indices(const NEURON_ISA_TPB_D4_MR_STRUCT *r) {       // opcode 0xe9
    for (int ch = 0; ch < r->num_active_channels; ch++) {
        for (int n = 0; n < 8; n++) {                              // exactly 8 indices per lane
            uint32_t idx = dve_index_flops[ch][n];                 // the drained search index flop
            store_t4d(r->dst_mem_pattern, ch, n, narrow(idx, r->dtype));  // UINT16 or UINT32
        }
    }
    // NO source operand; the source is the hidden DVE index flop set.
}
```

> **NOTE (the producer is `MATCH_REPLACE8`, not `FindIndex8` — and why).** The header names
> `MATCH_REPLACE8` (`0x6f`) as the producer, not `FindIndex8` (`0x6e`). Per
> [`search-cluster.md §6.3`](search-cluster.md), **`FindIndex8` writes its 8 indices directly to
> its own dst tensor** — so it needs no read-back op. **`MatchReplace8`**, by contrast, writes a
> 1:1 *replaced stream* to its dst and records the 8 first-match **positions in the per-lane index
> flops as a side-effect** — it does **not** spill them to a tensor. `DveReadIndices` is the op
> that drains those side-effect flops after a `MatchReplace8` pass, which is exactly why the
> header says *"8 indices per lane stored AFTER MATCH_REPLACE8"*. [per-op dst behavior
> HIGH/OBSERVED from both headers; "MatchReplace8 records index state as a side-effect"
> MED/INFERRED — `MatchReplace8` shares `FindIndex8`'s exact first-match core, so it computes the
> same positions]

### 6.1 Dtype matrix

Dtype enum codes (`common.h` `NEURON_ISA_TPB_DTYPE`): `BFLOAT16=0x6`, `FP16=0x7`, `UINT16=0x5`,
`UINT32=0x9`, `FP32=0xA`, `FP32R=0xB`, `FP8_EXP3=0xD`, `FP8_EXP4=0xE`, `FP8_EXP5=0xF`.

| op | output dtype set | internal |
|----|------------------|----------|
| `DveReadAccumulator` (`0x9b`) | `dve_read_accumulator_type_check = is_valid_fp_dtype_datapath(dtype, FP32R::True)` = `{FP8e3, FP8e4, FP8e5, FP16, BF16, FP32, FP32R}` | accumulator held fp32, narrowed on read |
| `ActivationReadAccumulator` (`0x24`) | `{BFLOAT16, FP16, FP32, FP32R}` — **NO FP8** (the ACT twin; §8) | (narrower) |
| `DveReadIndices` (`0xe9`) | `is_valid_dtype_dve_read_indices = {UINT16, UINT32}` only | indices are integer positions |

The read-accumulator output is **floating-point** (and the DVE variant uniquely admits **FP8**);
the read-indices output is strictly the **integer index dtype** (`u16`/`u32`) shared with
`FindIndex8`. No integer dtype is legal on the accumulator read; no float dtype is legal on the
index read. Element counts: `DveReadAccumulator` writes **1** fp/lane; `DveReadIndices` writes **8**
indices/lane. [HIGH/OBSERVED — both type-check bodies + the dtype enum]

---

## 7. The producer → state → read-back chains

The DVE pipeline keeps **two** kinds of hidden per-lane state; each read-back op drains one.

### 7.1 The index state → `DveReadIndices` (`0xe9`)

```text
PRODUCERS (the beam-search search cluster — search-cluster.md, all DVE-native):
  MatchValueLoad (0x6d)  loads 8 match keys mv[0..7] into DVE flops (the prologue, reversed map)
  FindIndex8     (0x6e)  scans src, records the first-match position per mv[n] in the 8 index
                         flops AND writes them to its OWN dst tensor (so it needs no read-back)
  MatchReplace8  (0x6f)  scans src, replaces first-matches with an immediate to its dst STREAM,
                         and leaves the 8 first-match positions in the SAME 8 index flops as a
                         SIDE-EFFECT (no tensor spill) -> this is the state DveReadIndices drains
STATE: 8 per-lane index flops (u16/u32-range positions; >=32768 / 0xFFFFFFFF = unmatched key)
READ-BACK: DveReadIndices (0xe9) spills the 8 flops to an 8-element u16/u32 dst
CHAIN: ... MatchValueLoad -> MatchReplace8 -> DveReadIndices (read the positions the replace
       pass found) -> INDIRECT_COPY (0xe7) / GATHER fetch the winning beam states by those indices
```

[chain HIGH from the header naming the producer + the shared index encoding; the exact flop-sharing
between `FindIndex8` and `MatchReplace8` MED/INFERRED]

### 7.2 The reduce accumulator → `DveReadAccumulator` (`0x9b`)

```text
PRODUCERS (the DVE accumulator-carrying family; each holds a per-lane running fp32 accumulator
via an accumulator_cmd, all DVE-native):
  TensorScalarCacheReduce     (0x9a, S3D3_TS)  acc = acc op1 (in[i] op0 scalar0), COLLAPSE; emits
                                               the FINAL acc; accumulator_cmd in {ZeroAccumulate=3,
                                               Accumulate=2, LoadAccumulate=4}   (ts-cache-reduce.md)
  TensorScalarCacheCumulative (0xe6, S3D3_TS)  same recurrence but emits the RUNNING acc at every
                                               step (inclusive scan)             (ts-cache-cumulative.md)
  SelectReduce/CopyPredicatedReduce (0xea, S2S2D2_STT)  masked reduce-MAX into a PSUM accumulator;
                                               accumulator_cmd in {Idle, Accumulate, ZeroAccumulate} (FW-62)
accumulator_cmd seeds/continues the carry (NEURON_ISA_TPB_ACCUM_CMD: IDLE=0, ZERO=1, ACCUMULATE=2,
  ZERO_ACCUMULATE=3, LOAD_ACCUMULATE=4). The Accumulate path CHAINS the accumulator across
  instructions/tiles, leaving the final running value in the per-lane accumulator flop.
STATE: 1 per-lane fp32 running accumulator (held fp32; NOT the wvec MAC file — §5 NOTE)
READ-BACK: DveReadAccumulator (0x9b) spills that 1 fp32/lane to a 1-element dst (optionally negated)
SIBLING: ActivationReadAccumulator (0x24) drains the equivalent ACT-engine accumulator; at MAVERICK
         the ACT read-accumulator image is gone and DveReadAccumulator is the renamed survivor (§9)
```

The cache-reduce / cache-cumulative producer pages name the state explicitly: both carry a
**persistent per-channel accumulator register** under `accumulator_cmd` —
[`ts-cache-reduce.md §4c`](ts-cache-reduce.md) and [`ts-cache-cumulative.md §4c`](ts-cache-cumulative.md)
describe it as *"a persistent register the engine carries across invocations: seed once
(`ZeroAccumulate` to identity, or `LoadAccumulate` from `imm1`), then chain more tiles with
`Accumulate`."* `DveReadAccumulator` is the op that reads that register out after the chain.
[chain HIGH from FW-62 naming `DveReadAccumulator` as the readout of the `SelectReduce`/CPR
accumulator + the shared `accumulator_cmd` family; the exact accumulator-flop identity MED/INFERRED]

> **GOTCHA (the accumulator outlives the producing instruction).** Like the `Accumulate(2)`
> command the cache-reduce twins use, the accumulator `DveReadAccumulator` reads is **state that
> persists across SEQ instructions** — it was seeded by a `ZeroAccumulate`/`LoadAccumulate` and
> extended by `Accumulate` on prior tiles. A reimplementer must preserve the per-lane accumulator
> register between the producer ops and the `0x9b` read; issuing `DveReadAccumulator` without a
> prior accumulator-seeding producer reads stale lanes. [INFERRED from the `accumulator_cmd`
> cross-tile carry semantics; MED]

---

## 8. CORRECTION — the `negated` flag, and `DveReadAccumulator` vs `ActivationReadAccumulator`

Both read-accumulators ride the **same `D1_RD` format**; they are decode-compatible by design (the
header says the format is kept similar to minimise decode changes). They diverge in **exactly**
three places, and the `negated` ownership is the one most easily mis-assigned.

```c
// fn is_valid_activation_read_accumulator(i) -> bool {        // 0x24, ACT engine
//    ... && has_activation_read_accumulator_opcode(i)
//        && activation_read_accumulator_type_check(i)         // {BFLOAT16, FP16, FP32, FP32R} — NO FP8
//        && read_accumulator_dst_size(i)                      // dst_element_count == 1
//        && d1_rd_has_zero_negated_field(i)  }                // *** negated MUST be 0 ***
//
// fn is_valid_dve_read_accumulator(i) -> bool {               // 0x9b, DVE engine
//    ... && has_dve_read_accumulator_opcode(i)
//        && dve_read_accumulator_type_check(i)                // is_valid_fp_dtype_datapath(.,True) — ADDS FP8
//        && read_accumulator_dst_size(i)                      // dst_element_count == 1
//        && d1_rd_has_valid_negated_field(i)  }               // *** negated in {0,1} — DVE USES it ***
```

The three differences:

| facet | `ActivationReadAccumulator` (`0x24`, ACT) | `DveReadAccumulator` (`0x9b`, DVE) |
|-------|-------------------------------------------|-------------------------------------|
| opcode predicate | `has_activation_read_accumulator_opcode` | `has_dve_read_accumulator_opcode` |
| `negated` (off 33) | **forbidden** — `d1_rd_has_zero_negated_field` (`negated == 0`) | **permitted** — `d1_rd_has_valid_negated_field` (`negated ∈ {0,1}`) |
| output dtype | `{BFLOAT16, FP16, FP32, FP32R}` — no FP8 | `{FP8e3/e4/e5, FP16, BF16, FP32, FP32R}` — **adds FP8** |

Everything else — the 1-element dst, the `POOLING_NUM_CHANNELS` channel range, the `TENSOR1D`
write to PSUM+SBUF, the reserved-zero fields — is **identical**.

> **CORRECTION (carried from [`activate-pwl.md §6`](activate-pwl.md), confirmed here).** The
> optional **`negated`** flag belongs to **`DveReadAccumulator` (`0x9b`)**, **not**
> `ActivationReadAccumulator` (`0x24`). The ACT read-accumulator validator
> (`d1_rd_has_zero_negated_field`) *requires* `negated == 0`; the DVE read-accumulator
> (`d1_rd_has_valid_negated_field`) is the op that actually *uses* it. What it negates: the
> drained per-lane accumulator value, on read. Any decode that attributes negate to the ACT op
> mis-reads the format. [HIGH/OBSERVED — the two validators, verbatim, `d1_rd.h:87/101`]

> **NOTE (the two read-accumulators drain DIFFERENT accumulators).** `ActivationReadAccumulator`
> (`0x24`) drains the **ACT engine's** per-lane fp32 reduction accumulator (built across an
> `Activate`/`ActivateQuantize` sequence under the ACT `accumulator_cmd` — see
> [`activate-pwl.md §6`](activate-pwl.md)). `DveReadAccumulator` (`0x9b`) drains the **DVE
> engine's** per-lane fp32 reduce accumulator (built by `CacheReduce`/`CacheCumulative`/
> `SelectReduce`, §7.2). Same 64-byte format, **two physically distinct accumulators on two
> engines**. Do not conflate the two same-named-shape read-accumulators. [HIGH/OBSERVED]

---

## 9. Dispatch — DVE-native (no `Q7_POOL` hop), and the MAVERICK fold

### 9.1 Both are DVE-engine, not `Q7_POOL` software kernels

The standing FW-64 discriminator applies on two axes: **(i)** presence in the `Q7_POOL`
`kernel_info_table` = POOL software kernel, absence = engine-native; **(ii)** the `"S: <Name>"`
self-name multiplicity in `libnrtucode_internal.so` selects the engine class (16 = iTPB SEQ,
**4 = DVE**, 3 = ACT/PE/POOL).

**(i) POOL `kernel_info_table` absence** — decoded via the shipped ncore2gp `xtensa-elf-readelf` on
the carved POOL ELFs:

```text
SUNDA  kernel_info_table @file 0xb260/0x90 = 18 entries (8 B records, opcode @ byte+3):
       e7 74 67 68 46 47 b8 bb 7e 41 7c 7d 49 7a 79 43 44 92   => 0x9b ABSENT ; 0xe9 ABSENT
CAYMAN kernel_info_table @file 0x7400/0x88 = 17 entries:
       7e 7c 7d 45 51 41 f0(x5) 52 46 47 be f2 7b               => 0x9b ABSENT ; 0xe9 ABSENT
```

Neither `0x9b` nor `0xe9` is a POOL kernel in either carved table. [HIGH/OBSERVED]

**(ii) DVE self-name multiplicity = 4** for both read-backs (`rg -ao` over
`libnrtucode_internal.so`, re-grounded this task):

```text
S: DveReadAccumulator   4   @ 0x18dbe0 0x427ec0 0x6efbe0 0x8aff90
S: DveReadIndices       4   @ 0x18e1c0 0x428500 0x6f0220 0x8b0600
S: ActivationReadAccumulator  3   @ 0x16aed3 0x404713 0x6cb093   (the ACT class — and note: NO MAVERICK-band copy, §9.3)
```

The 4 copies = the CAYMAN / MARIANA / MARIANA_PLUS / MAVERICK DVE blobs. **Co-residence** in the
CAYMAN DVE blob places both read-backs inside the same blob as every producer they drain:

```text
0x18d26c S: TensorScalarCacheReduce       <- accumulator producer (0x9a)
0x18d2df S: TensorScalarCacheCumulative   <- accumulator producer (0xe6)
0x18db50 S: MatchReplace                  <- index producer (0x6f)
0x18dbe0 S: DveReadAccumulator            <- THIS (0x9b)
0x18dbf7 S: FindIndex8                    <- index producer (0x6e)
0x18e16a S: CopyPredicatedReduce          <- accumulator producer (0xea)
0x18e1c0 S: DveReadIndices                <- THIS (0xe9)
0x18e1d3 S: RangeSelect                   (FW-46 anchor — confirms the 0x18e1c0 offset)
```

The two read-back tags sit **inside the same DVE blob** as every accumulator/index producer.
[HIGH/OBSERVED] **Verdict:** both `0x9b` and `0xe9` are DVE-engine, hardware-native — neither is a
`Q7_POOL` software kernel.

### 9.2 The MAVERICK dispatch table → `call8` → worker (FLIX-confirmed)

The MAVERICK DVE HW-decode dispatch table sits at device-VA `0x80820` (flat off `0x820` in
`NX_DVE_DEBUG_DRAM`; 187 LE words; opcode normalisation `addi −0x30`; default/stub `0x2f90`). Both
read-backs land on **real** handlers (≠ default):

```text
opcode 0x9b -> table[107] = 0x2eff   (REAL handler)        ; (producers also real: 0x9a->0x2eef, 0xe6->0x2ef7,
opcode 0xe9 -> table[185] = 0x2f1f   (REAL handler)        ;  0xea->0x2ea7, 0x6e->0x2def, 0x6f->0x2e37)
```

(For contrast, `0x24 ActivationReadAccumulator` is **below** the `0x30` table floor → routes via
the sub-`0x41` ACT compare-chain, consistent with it being the ACT-engine variant.)

FLIX-decode of the two trampolines (clean control-spine bundles, not desynced literal-pool):

```asm
0x2eff (DveReadAccumulator): a5 15 ff  -> call8 (target 0x2058) ; worker @0x2058: 36 61 00 -> entry a1,a1,48
0x2f1f (DveReadIndices):     a5 19 ff  -> call8 (target 0x20b8) ; worker @0x20b8: 36 61 00 -> entry a1,a1,48
```

The two workers differ at `+8` (const16 literal `ccd0` vs `c8f0`) → **distinct handlers**, not a
shared stub. The MAVERICK dispatch chain `opcode → table[op−0x30] → call8 → worker entry` is
FLIX-confirmed for both. [table decode + `call8` resolution + worker `entry`-prologue FLIX-decode
HIGH/OBSERVED] Neither op is in the MAVERICK DVE `PROF_CAM` (53 armed opcodes; both ABSENT) —
PROF-arming is profiling instrumentation, orthogonal to dispatch/ISA presence; the read-backs are
simply not profiled. [HIGH/OBSERVED]

> **SUNDA note.** `0xe9` does not exist on SUNDA (no enum, not in `d4_mr.h`) → no `DveReadIndices`
> dispatch there. `0x9b DveReadAccumulator` exists (enum + `D1_RD` map) but the SUNDA DVE ships as
> a RELEASE blob (self-name strings stripped, per FW-62) — so its device self-name copy is absent
> while the opcode + validator are present. [opcode/validator HIGH/OBSERVED; SUNDA device-string
> absence = RELEASE-strip MED/INFERRED]

### 9.3 The MAVERICK ACT→DVE fold

On MAVERICK (NC-v5) the standalone ACT engine image is gone — the activation PWL SRAM physically
migrated into the `TPB_DVE` block, and the read-accumulator is **re-expressed as the DVE-native
`DveReadAccumulator` (`0x9b`)** (see [`activate-pwl.md §1/§6`](activate-pwl.md)). The byte evidence:
`ActivationReadAccumulator`'s self-name copies exist only in the CAYMAN / MARIANA / MARIANA_PLUS
bands (`0x16aed3` / `0x404713` / `0x6cb093`) with **no MAVERICK-band copy**, whereas
`DveReadAccumulator` *is* present in the MAVERICK DVE band — it is the **renamed DVE-native
survivor** of the fold. [the self-name band pattern HIGH/OBSERVED; the v5 worker-body interior
INFERRED — per the standing rule the v5 region is OBSERVED at the enum/struct/self-name surface but
its compute interior is not byte-decoded here]

> **NOTE (a v5-only equivalence, header-grounded).** The fold means: on v2–v4 the ACT
> read-accumulator (`0x24`) and the DVE read-accumulator (`0x9b`) are **two distinct ops on two
> engines** (§8 NOTE); on v5 there is no standalone ACT read-accumulator handler, and the
> `DveReadAccumulator` `0x9b` op carries the read-accumulator role for the merged ACT+DVE block.
> The v5 *opcode/struct surface* is OBSERVED; the claim that the v5 `0x9b` worker subsumes the ACT
> accumulator semantics is **INFERRED** from the self-name band absence + the physical SRAM
> migration documented on `activate-pwl.md`. [v5 INFERRED — header-OBSERVED only]

---

## 10. Cross-links

- **[`search-cluster.md`](search-cluster.md)** (#705) — the index PRODUCER cluster.
  `FindIndex8` (`0x6e`) fills the 8 per-lane index flops (and writes its own dst), `MatchReplace8`
  (`0x6f`) leaves them as a side-effect, `Max8` (`0x6c`) / `MatchValueLoad` (`0x6d`) frame the
  beam-search round. `DveReadIndices` (this page) is the read side of that write side; the
  search-cluster page already forward-references this read-back as *"the read-side counterpart to
  the `mv[]`-state write side."*
- **[`ts-cache-reduce.md`](ts-cache-reduce.md)** (#715) — `TensorScalarCacheReduce` (`0x9a`), the
  COLLAPSE accumulator producer: leaves the **final** per-lane fp32 accumulator in the DVE flop
  that `DveReadAccumulator` drains.
- **[`ts-cache-cumulative.md`](ts-cache-cumulative.md)** (#714) — `TensorScalarCacheCumulative`
  (`0xe6`), the SCAN accumulator producer: leaves the **running** per-lane fp32 accumulator (and
  the cross-tile `Accumulate` carry) that `DveReadAccumulator` drains.
- **[`activate-pwl.md`](activate-pwl.md)** (#736) — `ActivationReadAccumulator` (`0x24`), the ACT
  sibling that shares the `D1_RD` format; the source of the carried `negated`-flag CORRECTION (§8)
  and the MAVERICK ACT→DVE fold (§9.3).
- **VAL-14 (Regfile-Bridge / Accumulator-Readout + Divergence Catalog)** — the validation family
  that tracks the accumulator-readout bridge ops differentially; `DveReadAccumulator` is the
  DVE-side accumulator-readout member of that catalog. (Planned page —
  `validation/regfile-bridge-divergence.md`.)
- **The full-ISS `read_state_value` path** — the ISS-side oracle that materialises a read-state
  drain (the introspection / read_state_value synthesis path). `DveReadAccumulator`/
  `DveReadIndices` are the ISA-level expression of that read-state primitive on the DVE engine.
  (Planned page — `iss/iss-oracle-synthesis.md`.)

---

## 11. Confidence / provenance summary

**HIGH/OBSERVED**

- opcodes `0x9b` (all 4 gens) / `0xe9` (cayman..maverick, **not** sunda), `// Y` maintained,
  byte-exact enum lines
- struct bindings `D1_RD`(`0x9b`, shared with ACT `0x24`) / `D4_MR`(`0xe9`, shared with
  Memset/Rng/RegStore) from all 4 per-gen `instruction_mapping.json`
- both structs 64 B, every offset compile-verified byte-identical across 4 gens (`negated`@33 on
  `D1_RD`; `reserved1`@33 on `D4_MR`; `dst_element_count`@28 on both)
- DVE-engine, not POOL: `0x9b`/`0xe9` absent from carved SUNDA(18)/CAYMAN(17) `kernel_info_table`;
  `"S: DveRead*"` = 4 copies each (DVE class), co-resident with the producers + `RangeSelect`
- `DveReadAccumulator` semantics: read 1 per-lane fp32 accumulator → 1-element dst, optional
  negate, fp output incl FP8 (verbatim `d1_rd.h`)
- `DveReadIndices` semantics: read 8 per-lane indices → 8-element dst, UINT16/UINT32 only,
  `DVE_NUM_CHANNELS` gate, *"stored after MATCH_REPLACE8"* (verbatim `d4_mr.h`)
- the `negated` flag is a `DveReadAccumulator` field (`d1_rd_has_valid_negated_field`), forbidden
  on `ActivationReadAccumulator` (`d1_rd_has_zero_negated_field`)
- the `DveReadAccumulator` (`0x9b`, DVE) vs `ActivationReadAccumulator` (`0x24`, ACT) distinction:
  same `D1_RD` format, different opcode predicate / negate gate / FP8 admission, **two distinct
  per-engine accumulators**
- MAVERICK dispatch: table `@0x80820` `0x9b→0x2eff` / `0xe9→0x2f1f` (real handlers); trampolines
  FLIX-decode `call8` → worker `entry a1,a1,48`
- ACT→DVE fold: `ActivationReadAccumulator` self-name 0 copies in the MAVERICK band;
  `DveReadAccumulator` present in the MAVERICK band (the renamed survivor)
- per-gen: `0x9b` SUNDA..MAVERICK; `0xe9` CAYMAN..MAVERICK

**MED/INFERRED**

- the accumulator `DveReadAccumulator` reads is the DVE per-lane **reduce** accumulator, SEPARATE
  from the wide `wvec` MAC file (header says *"the accumulator in each lane"*; geometry = 1
  fp/lane; producers = the DVE reduce family) — not the 4×1536-bit MAC `wvec`
- `MatchReplace8` records the index positions in the same flops `FindIndex8` uses as a side-effect
  (the `d4_mr.h` note names `MATCH_REPLACE8`; the flop-sharing inferred from the shared match core)
- the read-back-indices → `INDIRECT_COPY`/GATHER consumer wiring (structural)
- the SUNDA `DveReadAccumulator` device-string absence = RELEASE-build strip (per FW-62)
- the MAVERICK FLAT-NX per-row opcode↔funcVA binding (no `.rela`) + the v5 worker-body interiors
  past the `entry` prologue (the standing v5-INFERRED limit)

**CARRIED**

- the `negated`-belongs-to-`DveReadAccumulator` CORRECTION, established by
  [`activate-pwl.md §6`](activate-pwl.md) (#736) and re-confirmed here against the two `D1_RD`
  validators
- the MAVERICK ACT→DVE fold mechanism (GX-MAV-03 / `activate-pwl.md`); the ISA-definition layer is
  OBSERVED here, the v5 worker interior carried/inferred
