# AffineSelect (TensorScalarAffineSelect) — the POOL affine-predicate select (opcode `0x92`)

> **Scope.** AffineSelect is **opcode `0x92`** (`NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_AFFINE_SELECT`,
> `// Y` = maintained), the **POOL-engine** member of the GPSIMD select family. Its one and only job is a
> **position-masked select**:
>
> ```text
> out[k] = (mask[k] <op> 0) ? in[k] : fill_value
> ```
>
> where — and this is the whole point of the op — **`mask[k]` is not a memory read.** It is an
> **Iota-computed affine value** `base + Σ_d index_d·step_elem[d] + channel·channel_multiplier`,
> *generated on-chip* by the same strided ramp [Iota](./iota.md) (`0x7e`) builds, then thresholded
> against the **constant `0`**. The "affine" in the name is the **condition generator**, **not** an
> affine index into `src` (there is no gather, no index tensor, no addressing of `src` by the mask).
> `in[k]` is the straight `src` element at position `k`; the FAIL arm is a programmable FP32 fill from a
> register. The header's own words: *"a generalized triangle-fill similar to `triu()`"* — the canonical
> use is an upper/lower-triangular (triu/tril) or band mask, e.g. a causal-attention mask.
>
> AffineSelect = **Iota's ramp generator** ([iota.md](./iota.md)) **fused with a compare-vs-`0` and the
> select-vs-fill datapath** of [TensorScalarSelect](./ts-select.md) (`0x98`). It binds the **64-byte**
> operand struct `NEURON_ISA_TPB_S2D2_TS_AS_STRUCT` (compile-verified `sizeof == 64`, byte-identical on
> all four generations). It runs on the **Cadence Tensilica Vision-Q7 NX "Cairo" 512-bit FLIX/VLIW DSP**
> (`ncore2gp`, one per NeuronCore), on the **POOL** (Pooling) sequencer.
>
> Confidence tags use the `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` model in
> [`../../reference/confidence-model.md`](../../reference/confidence-model.md). Every host-ISA fact is
> read out of the shipped public `aws_neuron_isa_tpb_*.h` headers and compile-verified;
> every device fact is byte-pinned to the shipped firmware containers, disassembled with the
> native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`). All prose is derived from static binary analysis
> of the shipped artifacts only.

---

## 1. TL;DR — the pinned facts

| # | Fact | Evidence |
|---|------|----------|
| 1 | **Opcode `= 0x92`** (`TENSOR_SCALAR_AFFINE_SELECT`, `// Y` maintained), **byte-identical value + flag on all four gens**, and — unlike [RangeSelect](rangeselect.md) (`0xbc`, absent on SUNDA) — **defined on SUNDA**. | `common.h` sunda `:229` / cayman `:226` / mariana `:231` / maverick `:234`, all `= 0x92, // Y` |
| 2 | Binds the **64-byte struct `NEURON_ISA_TPB_S2D2_TS_AS_STRUCT`** mapped **1:1** to the opcode in `struct2opcode` on all four gens. `ISA_STATIC_ASSERT(==64)`; `gcc offsetof/sizeof` byte-identical four gens. | `instruction_mapping.json` + `s2d2_ts_as.h:66` + compile-verify |
| 3 | The select condition is an **affine, on-chip-generated mask**: `mask[k] = base + Σ index·step (+ channel·channel_multiplier)`, *"calculated as described in Iota"*, **does not point to memory**, compared against the **literal `0`**. | header doc-block `s2d2_ts_as.h:24-38` (verbatim) + `mask_pattern` = `DATA4D` @16 + `channel_multiplier` @60 |
| 4 | The op `<op>` is `fill_mode` @14, an `AFFINE_SELECT_CMP` ∈ `{GreaterThan=0, GreaterThanEq=1, Eq=2, NotEq=3}` — **only these four**, and all **vs `0`** (no programmable bound). | `s2d2_ts_as.h:46-51` enum, compile-verified |
| 5 | The two arms are **`in[k]`** (the straight `src` element, `src_mem_pattern` @36, `TENSOR2D`) and a **programmable FP32 fill** read from register `fill_reg` @15 — *"must be stored as FP32, regardless of input and output dtype."* | header `s2d2_ts_as.h:36-38` + struct + `is_valid_register_read(fill_reg)` |
| 6 | **Engine = POOL**, confirmed by `POOLING_NUM_CHANNELS` (=128) in the validator (not `DVE_NUM_CHANNELS`), by the SUNDA `pool_…affine_select` POOL kernel, and by the **3-copy** `"S: TensorScalarAffineSelect"` self-name (POOL/ACT/PE multiplicity, **not** DVE's 4), each beside `"S: Iota"`. | `is_valid_s2d2_ts_as` uses `POOLING_NUM_CHANNELS`; 3× self-name @ `0x1d03e0`/`0x469da0`/`0x734220` in `libnrtucode_internal.so`; 3× `"S: Iota"` |
| 7 | The datapath is **ramp-generate → compare-vs-`0` → `vbool` → `ivp_sel…t` blend (`in[k]` vs fill)** — Iota's `movva32`/`addn_2x32`/`muln_2x32`/`packln` ramp primitives, four `fill_mode`-guarded compare legs, one vbool-gated select. **Zero gather ops** (the no-index-into-`src` proof). | SUNDA POOL worker body, native disasm (FLIX-resync); ops OBSERVED, field-bind INFERRED-HIGH |
| 8 | `in_out_dtype` @13 is a **`DTYPE_PAIR`** `{dtype_lo=in, dtype_hi=out}`: copy-with-cast `in→out`, integer **and** FP symmetric (no FP-only restriction). Output admits FP32R; input does not. Fill always FP32. | `s2d2_ts_as_valid_dtypes` + `is_valid_dtype(hi, AllowFP32R::True)` / `(lo, …::False)` |

> **CORRECTION (struct label).** An adjacent op-matrix survey cell lists the `0x92` struct as
> "`S3D3_TS`". **That is wrong.** Three independent sources — `struct2opcode` (all four gens), the
> `aws_neuron_isa_tpb_s2d2_ts_as.h` header, and the `gcc` compile-verify (§3) — all bind `0x92` to
> **`S2D2_TS_AS`** (64 B). The "`S3D3_TS`" label is a copy-paste from the neighbouring arith rows.
> `[HIGH/OBSERVED — three sources vs one cell]`

---

## 2. Provenance / carve anchors

| Artifact | Value |
|----------|-------|
| Host ISA header | `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_s2d2_ts_as.h` (sunda/cayman/mariana/maverick) |
| Opcode enum | `…/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_common.h` |
| Struct→opcode | `…/neuron_<gen>_arch_isa/tpb/instruction_mapping.json` (`struct2opcode`) |
| Validator | `…/aws_neuron_isa_tpb_assert.h` `is_valid_s2d2_ts_as` (sunda `:14552`); `debug_assert.h:260` binds `OPCODE_TENSOR_SCALAR_AFFINE_SELECT → dbg_is_valid_s2d2_ts_as` |
| Firmware container | `…/custom_op/c10/lib/libnrtucode_internal.so` |
| Container sha256 | `b7c67e898a116454…` (== the [Iota](./iota.md) / [TensorScalarSelect](./ts-select.md) container) |
| `"S: TensorScalarAffineSelect"` (3×) | file offsets `0x1d03e0` / `0x469da0` / `0x734220` (SUNDA-region / CAYMAN / MARIANA DEBUG blobs), each adjacent to `"S: Iota"` |
| Disassembler | `gpsimd_tools/…/bin/xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp`, Vision-Q7 FLIX/VLIW |

The `S2D2_TS_AS` struct is **byte-identical** (`sizeof 64`, same offsets) on all four gens —
compile-verified (§3). `[HIGH/OBSERVED]`

> **NOTE — which firmware facts re-verify on disk, and which are report-CARRIED.** Only
> `libnrtucode_internal.so` (sha `b7c67e89…`, the customop-lib container) is in the checked-in tree; it
> holds the **CAYMAN / MARIANA / MARIANA_PLUS** EXTISA images, but **not** a SUNDA image — the SUNDA
> EXTISA container (`libnrtucode_extisa.so`) and the earlier scratch carves are gone. So this page
> **independently re-confirms** the **CAYMAN POOL-table absence of `0x92`** (§4c, byte-exact, with the
> table's iota / RangeSelect rows cross-checking the decoder). The four **SUNDA-only** firmware facts —
> the POOL-table location, `idx17 {0x92 → 0x0100a118}`, the `idx15/16 {0x43/0x44}` context, and the
> worker entry shape (§4b, §5) — are **not reproducible on disk** (no SUNDA image present) and are tagged
> `[CARRIED]` from the prior SUNDA carve. The entire *header / struct / validator / enum / self-name-offset* surface
> (§1–§3, §6 dtype, §7 per-gen, and the §4a engine conclusion) is grounded against the
> on-disk headers and `libnrtucode_internal.so` bytes. `[flagged]`

---

## 3. The operand struct — `S2D2_TS_AS` (64 B), compile-verified four gens

The operand is `NEURON_ISA_TPB_S2D2_TS_AS_STRUCT`, declared verbatim in `aws_neuron_isa_tpb_s2d2_ts_as.h`
(present under every `neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/`).
`instruction_mapping.json`'s `struct2opcode` binds it 1:1 to `OPCODE_TENSOR_SCALAR_AFFINE_SELECT`.
`gcc -I<hdr>` + `sizeof`/`offsetof` reproduce the layout for **all four** generations —
**byte-identical**.

```c
typedef struct NEURON_ISA_TPB_S2D2_TS_AS_STRUCT {  /* s2d2_ts_as.h:53 — 64 B, opcode 0x92 */
    NEURON_ISA_TPB_HEADER            header;              //  4  ( 0 -  3)  opcode = 0x92
    NEURON_ISA_TPB_EVENTS            events;              //  8  ( 4 - 11)  wait/update semaphore sync
    uint8_t                          num_active_channels; //  1  (12     )  *** 1..128 partitions (POOLING) ***
    NEURON_ISA_TPB_DTYPE_PAIR        in_out_dtype;        //  1  (13     )  *** {dtype_lo=in:4, dtype_hi=out:4} ***
    NEURON_ISA_TPB_AFFINE_SELECT_CMP fill_mode;           //  1  (14     )  *** the <op> vs 0: GT/GE/EQ/NE ***
    uint8_t                          fill_reg;            //  1  (15     )  *** FP32 fill-value register index ***
    NEURON_ISA_TPB_DATA4D            mask_pattern;        // 20  (16 - 35)  *** THE AFFINE MASK = an Iota ramp ***
    NEURON_ISA_TPB_TENSOR2D          src_mem_pattern;     // 12  (36 - 47)  *** SRC = the input TENSOR2D (read) ***
    NEURON_ISA_TPB_TENSOR2D          dst_mem_pattern;     // 12  (48 - 59)  *** DST = the output TENSOR2D (write) ***
    int32_t                          channel_multiplier;  //  4  (60 - 63)  *** per-channel additive mask stride ***
} NEURON_ISA_TPB_S2D2_TS_AS_STRUCT;

ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_S2D2_TS_AS_STRUCT) == 64, "…NOT 64B.");
```

`offsetof` output (**identical** on sunda / cayman / mariana / maverick):

```text
sizeof S2D2_TS_AS = 64
  header 0   events 4   num_active_channels 12   in_out_dtype 13   fill_mode 14   fill_reg 15
  mask_pattern 16   src_mem_pattern 36   dst_mem_pattern 48   channel_multiplier 60
```

`[HIGH/OBSERVED — gcc offsetof, four gens]`

The struct's own format comment (`s2d2_ts_as.h:14-19`) states the geometry explicitly: *"one 2d SRC
Tensor; one 2d DST Tensor; one 4d 'mask' Tensor."* The `src`/`dst` are 2-D `TENSOR2D` memory patterns;
the `mask` is the 4-D `DATA4D` *generator* — not a memory tensor.

### 3.1. The mask descriptor (`mask_pattern` @16) — `DATA4D`, the Iota ramp

```c
typedef struct NEURON_ISA_TPB_DATA4D {   /* common.h:649 — 20 B, the SAME struct Iota uses */
    int32_t  base;          /* @ 0  the affine START value (the mask's constant term)   */
    int16_t  step_elem[4];  /* @ 4  per-dimension STEP (the affine slope; signed!)      */
    uint16_t num_elem[4];   /* @12  per-dimension COUNT (the iteration extent)          */
} NEURON_ISA_TPB_DATA4D;
```

This is **bit-for-bit the `DATA4D` generator descriptor [Iota](./iota.md) uses for its ramp**
(`common.h:649`, the identical 20-byte layout the Iota page documents at §2). It has **no `start_addr`**
— because, exactly as for Iota, the mask reads nothing: `mask[k] = base + Σ_d index_d·step_elem[d]`. The
header states it directly (`s2d2_ts_as.h:30-32`): *"the mask does not point to memory. Instead, the kth
mask value is calculated as described in Iota."* The validator
`same_element_count_ts_as(mask_pattern, src_mem_pattern)` (`assert.h`, `d4d_element_count(mask) ==
t2d_element_count(src)`) enforces **one mask value per source element** — the ramp exactly covers the
walk. `[HIGH/OBSERVED — header + DATA4D + validator]`

> **GOTCHA — `mask_pattern` is a `DATA4D` (4-D) but `src`/`dst` are `TENSOR2D` (2-D).** The mask ramp
> can index in up to four dimensions while the data tensors are only 2-D. This is *intentional*: the 4-D
> ramp builds the 2-D triangle/band by giving the row and column axes independent `step_elem` slopes, so
> a single instruction realizes `(row − col) <op> 0`-style masks. The element counts are still reconciled
> (`d4d_element_count == t2d_element_count`); the extra ramp dims simply fold into the same total. The
> header itself flags the dimension squeeze (`s2d2_ts_as.h:40-43`): the src/dst *"may not fit in the
> instruction's tensor dimensions … A future revision will backport shape registers,"* the same
> `shape_from_register` escape CAYMAN+ already wires into the elem-count check (§7).

### 3.2. `channel_multiplier` (@60) — the per-partition mask phase

`channel_multiplier` (`int32 @60`) adds `channel·channel_multiplier` to lane *channel*'s ramp, so each of
the 1..128 partitions can start its mask at a distinct phase. This is the **same role and the same field
name** as [Iota](./iota.md)'s `channel_multiplier` (Iota §1 fact 3, §3 step 5). It is *the* field that
turns a 1-D ramp into a 2-D triangle: row index *r* (the partition/channel) contributes
`r·channel_multiplier` and column index *c* contributes `c·step_elem[d]`, so `mask[r][c] = base +
c·step_elem + r·channel_multiplier`; choosing `step_elem = −channel_multiplier` makes `mask[r][c] ∝
(c − r)`, and `mask <op> 0` becomes the diagonal split that is a triu/tril mask. `[field HIGH/OBSERVED;
the (c − r) construction INFERRED-HIGH from the field names + the firmware per-channel `addn`, exactly as
Iota §3]`

### 3.3. The `fill_mode` comparator enum — four ops, all vs `0`

```c
typedef enum NEURON_ISA_TPB_AFFINE_SELECT_CMP {   /* s2d2_ts_as.h:46 */
    NEURON_ISA_TPB_AFFINE_SELECT_CMP_GREATER_THAN    = 0x0,  /* mask[k] >  0 */
    NEURON_ISA_TPB_AFFINE_SELECT_CMP_GREATER_THAN_EQ = 0x1,  /* mask[k] >= 0 */
    NEURON_ISA_TPB_AFFINE_SELECT_CMP_EQ              = 0x2,  /* mask[k] == 0 */
    NEURON_ISA_TPB_AFFINE_SELECT_CMP_NOT_EQ          = 0x3,  /* mask[k] != 0 */
} NEURON_ISA_PACKED NEURON_ISA_TPB_AFFINE_SELECT_CMP;
```

`fill_mode` @14 holds one of these four. The reference operand is **always the constant `0`** — there is
no programmable bound (contrast [RangeSelect](rangeselect.md)'s two FP32 bounds). The validator gates it
with `is_valid_enum(EnumList::AffineSelectCmp, fill_mode)`. `[HIGH/OBSERVED — enum compile-verified]`

### 3.4. How it differs from the two DVE select structs

| field | `S2D2_TS_AS` (AffineSelect, POOL) | `S3D3_TS_SELECT` (`0x98`, DVE) | `S2D2_RS` (`0xbc`, DVE) |
|---|---|---|---|
| predicate source | `mask_pattern` @16 = **`DATA4D` ramp** (affine generator) | `pred_imm` @44 = **loaded** 0/1 imm | runtime data vs 2 FP32 bounds |
| comparator | `fill_mode` @14 ∈ {GT/GE/EQ/NE} **vs 0** | **none** (imm predicate) | `comp_op0/1` ∈ {EQ/GT/GE/LE/LT} vs bounds |
| FAIL fill | `fill_reg` @15 = **programmable FP32 reg** | the *other* select operand (`src1_imm`) | `fill` = **fixed −FLT_MAX** |
| dtype | `in_out_dtype` **PAIR** (cast in→out) | **single** dtype (copy, no cast) | in/out PAIR, FP-only |
| per-partition phase | `channel_multiplier` @60 | `reversed_pred` @32 | `reduce_cmd`/`reduce_op` |
| mem pattern | `TENSOR2D` src/dst (2-D) | `TENSOR3D` (3-D) | `TENSOR2D` (2-D) |

AffineSelect's discriminator is the **`DATA4D` mask *generator* + the vs-`0` comparator + the programmable
FP32 fill** — none of which the DVE selects carry. `[HIGH/OBSERVED — three headers diffed]`

### 3.5. The validity predicate (verbatim, all four gens)

`debug_assert.h:260` binds `OPCODE_TENSOR_SCALAR_AFFINE_SELECT → dbg_is_valid_s2d2_ts_as`. The header
states `is_valid_s2d2_ts_as` verbatim (`s2d2_ts_as.h:71-84`):

```text
has_valid_neuron_header(i) && has_valid_neuron_events(i)
  && has_s2d2_ts_as_opcode(i)                                          /* header.opcode == 0x92 */
  && has_valid_active_channel_range(num_active_channels, POOLING_NUM_CHANNELS /* 128 */)
  && tensor2d_valid(src_mem_pattern, dtype_lo, WriteTensor::False, AllowedInPSUM::?, AllowedInSBUF::True)
  && tensor2d_valid(dst_mem_pattern, dtype_hi, WriteTensor::True,  AllowedInPSUM::?, AllowedInSBUF::True)
  && start_addr_active_channels(src.start_addr, num_active_channels)
  && start_addr_active_channels(dst.start_addr, num_active_channels)
  && s2d2_ts_as_valid_elem_count(i)                                    /* same_count(src,dst) && same_count(mask,src) */
  && s2d2_ts_as_valid_dtypes(i)                                        /* §6 */
  && is_valid_enum(EnumList::AffineSelectCmp, fill_mode)               /* fill_mode ∈ {0,1,2,3} */
  && is_valid_register_read(fill_reg)                                  /* fill_reg is a readable reg */
```

The `POOLING_NUM_CHANNELS` channel constant is the **decisive engine tell** — POOL, not DVE (§4a). The
`AllowedInPSUM::?` differs per gen (SUNDA `True`, CAYMAN+ `False`; §7). The `fill_reg` is validated as a
*register read*, confirming the fill is a register-sourced scalar (read as FP32, §5), **not** an inline
immediate. `[HIGH/OBSERVED — header + assert.h:14552 + debug_assert.h:260]`

---

## 4. The dispatch — POOL engine; SUNDA POOL-KIT kernel, CAYMAN+ non-KIT

### 4a. Engine = POOL (the decisive distinction from the DVE selects)

Three confirmations, the same POOL-vs-DVE discriminator method the family pages use:

1. **Validator channel constant.** `is_valid_s2d2_ts_as` ends with
   `has_valid_active_channel_range(num_active_channels, POOLING_NUM_CHANNELS)` — the **POOL** constant
   (`POOLING_NUM_CHANNELS == 128U`, `common.h:35`), whereas the two DVE selects [TensorScalarSelect](./ts-select.md)
   (`0x98`) and [RangeSelect](rangeselect.md) (`0xbc`) use `DVE_NUM_CHANNELS` (`common.h:36`, also 128 but
   a *distinct symbol* — the symbol, not the value, is the tell). `[HIGH/OBSERVED]`
2. **POOL software kernel.** On SUNDA, `0x92` is a `Q7_POOL kernel_info_table` member named
   `pool_tensor_scalar_affine_select` (the `pool_` prefix). `[CARRIED — prior byte-decode of the carved
   SUNDA POOL table; the table file is not in the checked-in tree, §2 NOTE]`
3. **Self-name multiplicity.** `"S: TensorScalarAffineSelect"` appears **3 times** in
   `libnrtucode_internal.so` (`0x1d03e0` / `0x469da0` / `0x734220`) — multiplicity **3** = the POOL/ACT/PE
   family (cf. the DVE selects' multiplicity 4). Each copy sits **immediately beside `"S: Iota"`** (also
   3×) — AffineSelect co-resides in the POOL kernel cluster with the very ramp generator whose math it
   reuses. `[HIGH/OBSERVED — byte-scan]`

This is the decisive separation from the two DVE selects (`0x98`/`0xbc`): AffineSelect is the **POOL**
member of the select family.

### 4b. The SUNDA POOL-KIT row + the canonical chain `[CARRIED]`

Per a prior byte-decode of the carved `extisa_SUNDA_POOL_PERF_EXTISA_0.so` (kernel_info_table @ VMA
`0x02000760` / file `0xb260`, 18 entries, record `{u8 0; u8 0; u8 spec; u8 opcode; u32_le funcVA}`):

```text
idx17 = { spec 0, opcode 0x92, funcVA 0x0100a118 }   <- pool_tensor_scalar_affine_select
        (context: idx15 0x43 -> 0x0100a0d8, idx16 0x44 -> 0x0100a0d8 (shared TS-arith); 0x92's
         funcVA 0x0100a118 is the next, distinct kernel.)  spec=0, no 0xF0 ExtendedInst escape.
```

```text
AffineSelect (SUNDA, the byte-grounded chain — table hop [CARRIED], rest HIGH/OBSERVED):
  [TPB opcode 0x92, S2D2_TS_AS 64 B]
    → decode-validate  dbg_is_valid_s2d2_ts_as              [HIGH/OBSERVED]
    → POOL kernel_info_table linear-scan opcode 0x92 → funcVA 0x0100a118 (idx17)   [CARRIED]
    → the AffineSelect worker (entry a1,32; const16 + movi a5,10 dtype const — the Iota-trampoline shape)
    → PHASE 1: generate the Iota mask ramp (ivp_movva32 / addn_2x32 / muln_2x32 / packln)
    → PHASE 2: compare mask vs 0 by fill_mode (ivp_ltn / eqn / neqn / len_2x32 → vbool)
    → PHASE 3: ivp_selnx16t blend (in[k] vs fill, gated by the vbool) → write dst
```

### 4c. CAYMAN+ — `0x92` absent from the POOL kernel table (non-KIT) `[re-confirmed on disk]`

The CAYMAN POOL `kernel_info_table` is byte-decoded from the EXTISA image embedded in
`libnrtucode_internal.so` (CAYMAN MAIN table @ file `0x2f6be0`, 17 entries): the opcodes are
`7e 7c 7d 45 51 41 f0 f0 f0 f0 f0(spec0-4) 52 46 47 be f2 7b` — **`0x92` is not present**. The decoder is
cross-checked by `idx0 {0x7e → 0x01000080}` (iota, matching [iota.md](./iota.md)) and `idx14 {0xbe →
0x01004204}` (RangeSelect). The MARIANA and MARIANA_PLUS tables are identical, also no `0x92`. So on
CAYMAN+ (NC-v3+) AffineSelect is **not** a flat POOL `kernel_info_table` software kernel —
the same SUNDA-flat-vs-CAYMAN-routed pattern the gather/indirect family shows ([indirection-gather](./indirection-gather.md)):
SUNDA carries them flat in the POOL table; CAYMAN+ serves them through a different (SEQ-decode / remapped
POOL) surface. The op is **still defined** on CAYMAN+ (opcode `0x92 // Y` + the `S2D2_TS_AS` header + the
`"S: TensorScalarAffineSelect"` DEBUG self-names `0x469da0`/`0x734220`), so the worker is present — just
not reached via a POOL-table funcVA hop. The exact CAYMAN+ surface is **not** byte-pinned.
`[HIGH/OBSERVED 0x92-absent-from-CAYMAN-KIT — decoded on disk; MED the CAYMAN+ alternative surface]`

---

## 5. The algorithm — Iota ramp + compare-vs-`0` + vbool select

The SUNDA worker body (funcVA `0x0100a118`, per the prior native disasm, FLIX desync handled by
reading the live IVP slots within each bundle) splits **cleanly into the three phases of the header
semantics** — and the IVP census *is* the algorithm signature. The op vocabulary is byte-OBSERVED in the
carve; the binding of each register to a struct field is INFERRED-HIGH from the field names + Iota's
identical ramp chain (Iota §3). `[CARRIED ramp/compare/select ops; the ramp vocabulary is the
*same* set the [Iota](./iota.md) page grounds HIGH/OBSERVED on SUNDA]`

### 5.1. Phase 1 — the Iota mask ramp (`mask[k] = base + Σ idx·step + channel·mult`)

The ramp primitives are **exactly Iota's** (Iota §3.2: `movva32` broadcast + `addn_2x32` accumulate +
`muln_2x32` step + `packln_2x96` pack), in the SUNDA worker census:

| op | count | role |
|---|---|---|
| `ivp_movva32` | 6 | broadcast `base` / `step_elem` / `channel_multiplier` into lanes |
| `ivp_addn_2x32` | 9 | the ramp accumulate (one add per dim / per tile / per channel) |
| `ivp_muln_2x32` | 1 | the step-factor multiply (`index × step`) |
| `ivp_packln_2x96` | 1 | pack the wide product back to the lane width |

An `ivp_addn_2xf32t` (an FP32 ramp add) appears, consistent with an FP32-valued mask compared against the
FP constant `0`. The mask is computed in the integer/FP datapath, never written out — it is consumed
in-place by Phase 2.

### 5.2. Phase 2 — compare-vs-`0` → `vbool` (the `fill_mode` dispatch)

The body has a **per-`fill_mode` comparator dispatch** (branch on the register holding `fill_mode`), each
leg a distinct `ivp` compare producing a `vbool`. The representative byte-OBSERVED bundles from the carve:

```text
0x100a39c { bnei.w15 a5,3,… ; ivp_neqn_2x32 vb1,v5,v4 ; ivp_addn_2x32 v5,v5,v1 }   ; NotEq  leg (fill_mode==3)
0x100a4e0 { beqz.w15 a7,…   ; ivp_eqn_2x32  vb0,v14,v4; ivp_addn_2x32 v5,v14,v8 }   ; Eq     leg (fill_mode==2)
0x100a2e3 { beqz.w15 a5,…   ; ivp_len_2x32  vb0,v4,v14; ivp_addn_2x32 v5,v14,v8 }   ; GE/GT via inverted ≤
0x100a43c { beqz.w15 a7,…   ; ivp_ltn_2x32  vb0,v4,v14; ivp_addn_2x32 v5,v14,v8 }   ; GE/GT via inverted <
```

The four `fill_mode` values map onto the compare ops `{len/ltn` (with their inverses realizing GT/GE),
`eqn` (EQ), `neqn` (NE)`}` against the mask reference held in `v4`/`v14` — i.e. the `mask[k] <op> 0`
predicate. The interleaved `ivp_addn_2x32 v5,v5,v1` is the **per-element ramp-step advance** — the mask
regenerates as the walk proceeds (no precomputed mask buffer). The `bnei.w15 a5,3` / `beqz.w15 a5` guards
are the `fill_mode` discriminator selecting the leg. `[CARRIED compare ops + fill_mode branch; MED the exact GT/GE-vs-LT/LE polarity per leg
through the FLIX .w15 desync — the inverted len/ltn legs realize GT/GE]`

### 5.3. Phase 3 — the select/blend (`in[k]` vs fill, gated by the vbool)

```text
0x100a1ab { ivp_lsn_2x32_xp v4,a8,a1 ; … ; ivp_selnx16t v26,v0,v17,v10,vb2 }
```

`ivp_selnx16t` is the **vbool-gated per-lane merge select** — choosing between the `src` tensor lanes
(`in[k]`, stream-loaded by `ivp_lsn_2x32_xp`) and the fill lanes, gated by the `vbool` from Phase 2. This
is exactly the [B21 Select / Shuffle crossbar](../../isa/ref/b21-select-shuffle.md) `ivp_selnx16t
v3,v1,v2,v4,vb2` merge-`t` form (the "compare → vbool → `ivp_sel…t`" predicated-select datapath that page
documents) — and it is the **identical select datapath** [TensorScalarSelect](./ts-select.md) (§5.2) uses
for its blend. The difference is purely *where the vbool comes from*: TensorScalarSelect **loads** the
predicate; AffineSelect **generates** it from the Iota ramp + compare-vs-`0`. `[CARRIED selnx16t; the merge-t vbool-gated select form is HIGH/OBSERVED in b21 and ts-select.md]`

### 5.4. No gather — the decisive exclusion of "affine-indexed `src`"

The worker census has **zero** `ivp_gather*`/`ivp_gatheran`/`ivp_gatherdnx` ops. `src` is read by a
straight strided `TENSOR2D` walk (`ivp_lsn_2x32_xp` stream-load), element *k* → element *k*. AffineSelect
does **not** index `src` by the mask — contrast a true gather (`0x68`, `S4D4_GT`, `table[index]→dst` via
`ivp_gatheran_2x32t`, [indirection-gather](./indirection-gather.md)). `[CARRIED — prior census = 0
gather ops]`

### 5.5. The recovered contract

```c
/* AffineSelect — TensorScalarAffineSelect.  S2D2_TS_AS (64 B), opcode 0x92, POOL engine.
 * Worker: "S: TensorScalarAffineSelect" (POOL), SUNDA funcVA 0x0100a118.
 *
 * The mask is an Iota ramp GENERATED on-chip (NOT a memory read, NOT an index into src);
 * it is thresholded against the constant 0 to produce a per-lane select between in[k] and
 * a programmable FP32 fill.  The canonical use is a triu/tril triangle/band fill.
 *
 * Ramp primitives (ivp_movva32/addn_2x32/muln_2x32/packln) are SHARED with Iota (0x7e);
 * the vbool-gated select (ivp_selnx16t) is SHARED with TensorScalarSelect (0x98).
 */
void affine_select(const NEURON_ISA_TPB_S2D2_TS_AS_STRUCT *op)
{
    const NEURON_ISA_TPB_DATA4D *m = &op->mask_pattern;          /* the affine ramp descriptor */
    float fill = read_fp32(reg[op->fill_reg]);                   /* FP32 scalar, ALWAYS FP32   */
    enum dtype in_d  = op->in_out_dtype.dtype_lo;                /* input  dtype               */
    enum dtype out_d = op->in_out_dtype.dtype_hi;                /* output dtype               */

    for (unsigned c = 0; c < op->num_active_channels; ++c) {     /* 1..128 partitions (POOL)   */
        for (unsigned k = 0; k < t2d_element_count(&op->src_mem_pattern); ++k) {
            /* PHASE 1 — the Iota ramp (computed, never stored), per element k of channel c.
             * d4d_element_count(mask) == t2d_element_count(src): one mask value per element. */
            int32_t mask_k = m->base
                           + ramp_sum(m->step_elem, m->num_elem, k)   /* Σ_d index_d·step_elem[d] */
                           + (int32_t)c * op->channel_multiplier;      /* per-channel phase        */

            /* PHASE 2 — compare vs the CONSTANT 0 by fill_mode → a vbool.  ONLY 4 ops, all vs 0. */
            bool pred;
            switch (op->fill_mode) {                                   /* AFFINE_SELECT_CMP        */
                case CMP_GREATER_THAN:    pred = (mask_k >  0); break; /* 0x0 */
                case CMP_GREATER_THAN_EQ: pred = (mask_k >= 0); break; /* 0x1 */
                case CMP_EQ:              pred = (mask_k == 0); break; /* 0x2 */
                case CMP_NOT_EQ:          pred = (mask_k != 0); break; /* 0x3 */
            }

            /* PHASE 3 — vbool-gated select (ivp_selnx16t): in[k] straight-read, else the fill.
             * PASS copies the src element (cast in→out); FAIL writes the FP32 fill (cast to out). */
            uint32_t in_k = read_tensor2d_elem(&op->src_mem_pattern, c, k, in_d);  /* k→k, no gather */
            write_tensor2d_elem(&op->dst_mem_pattern, c, k,
                                pred ? cast(in_k, in_d, out_d) : cast_f32(fill, out_d), out_d);
        }
    }
}
```

`[contract HIGH/OBSERVED from the header; the per-channel/per-tile loop ordering and register→field
binding INFERRED-HIGH from the firmware `addn` pattern + Iota's identical loop, MED on the exact trip
register]`

> **NOTE — why this is "Iota + select" and not its own primitive.** Iota *emits* the ramp as its output
> (`dst[k] = ramp`); AffineSelect *consumes* the **same ramp** internally as a positional mask and emits
> the **selected data** instead. They share the `movva32`/`addn`/`muln`/`packln` ramp set, co-reside in
> the POOL blob (`"S: …AffineSelect"` beside `"S: Iota"`), and AffineSelect's only additions over Iota are
> the compare-vs-`0` (Phase 2) and the `ivp_selnx16t` blend (Phase 3) — the latter being precisely the
> datapath [TensorScalarSelect](./ts-select.md) implements. AffineSelect = **Iota's ramp + TS-Select's
> select**. `[HIGH/OBSERVED — neighbor + shared op vocabulary]`

---

## 6. The dtype matrix

`in_out_dtype` @13 is a packed `DTYPE_PAIR` `{dtype_lo:4 = input, dtype_hi:4 = output}` (`common.h:749`).
`s2d2_ts_as_valid_dtypes` (verbatim, `s2d2_ts_as.h:99-102`):

```c
is_valid_dtype(get_dtype_from_pair(in_out_dtype.dtype_hi), DtypeAllowFP32R::True)    // OUTPUT
&& is_valid_dtype(get_dtype_from_pair(in_out_dtype.dtype_lo), DtypeAllowFP32R::False) // INPUT
```

* **INPUT (`dtype_lo`, `AllowFP32R::False`)** — any valid dtype **except** `{FP32R(0xB), UINT64(0x1),
  INT64(0xC), FP4(0x10)}`: i.e. `{INT8(0x2), UINT8(0x3), INT16(0x4), UINT16(0x5), BF16(0x6), FP16(0x7),
  INT32(0x8), UINT32(0x9), FP32(0xA), FP8_E3/E4/E5(0xD/0xE/0xF)}`.
* **OUTPUT (`dtype_hi`, `AllowFP32R::True`)** — the same set **plus `FP32R(0xB)`**.

| arm | field | dtype set | note |
|---|---|---|---|
| input `in[k]` | `dtype_lo` @13 lo-nibble | broad set exc `{FP32R, UINT64, INT64, FP4}` | `AllowFP32R::False` |
| output `out[k]` | `dtype_hi` @13 hi-nibble | broad set **+ FP32R** | `AllowFP32R::True` |
| fill | `fill_reg` @15 | **always FP32** (cast to `dtype_hi` on the FAIL arm) | header `:37-38` |
| mask | `mask_pattern` @16 | int/FP ramp, compared vs the int/FP constant `0` | — |

> **QUIRK — integer + FP symmetric; the data is *never* compared.** Unlike [RangeSelect](rangeselect.md)
> (FP-only, FP32-compare hub), AffineSelect accepts **integer and FP** dtypes on `src`/`dst`, and casts
> `in→out` (a `DTYPE_PAIR`). It can do so because **the data is never compared** — only the synthesized
> *mask* is thresholded. AffineSelect is a value-copy-with-cast gated by a positional predicate, so there
> is no FP-compare restriction on the data path. The fill is the one fixed-format operand: **always FP32**
> regardless of `in`/`out` (`fill_reg` read as FP32, cast to `dtype_hi`).

DTYPE ordinals (`common.h:705-720`): `INVALID 0x0, UINT64 0x1, INT8 0x2, UINT8 0x3, INT16 0x4, UINT16 0x5,
BF16 0x6, FP16 0x7, INT32 0x8, UINT32 0x9, FP32 0xA, FP32R 0xB, INT64 0xC, FP8_E3 0xD, FP8_E4 0xE, FP8_E5
0xF, FP4_E2 0x10`. `num_active_channels` ∈ 1..128 (`POOLING_NUM_CHANNELS`). `[HIGH/OBSERVED]`

---

## 7. Per-generation presence

| GEN | opcode `0x92` | `S2D2_TS_AS` | POOL-KIT (`0x92`) | `"S: …AffineSelect"` | status |
|---|---|---|---|---|---|
| SUNDA (NC-v2) | `// Y` | 64 B (compile) | idx17 funcVA `0x0100a118` `[CARRIED]` | `0x1d03e0` (region) | PRESENT (POOL-KIT kernel) |
| CAYMAN (NC-v3) | `// Y` | 64 B id. | **absent** (non-KIT) `[CARRIED]` | `0x469da0` (DEBUG) | PRESENT (non-KIT) |
| MARIANA (NC-v4) | `// Y` | 64 B id. | absent | `0x734220` (DEBUG) | PRESENT (non-KIT) |
| MARIANA_PLUS | `// Y` (mar hdr) | 64 B id. | absent | (== mariana blob) | PRESENT (non-KIT) |
| MAVERICK (NC-v5) | `// Y` | 64 B (compile) | not byte-checked (CAYMAN-like) | (carve stripped) | header-grounded |

Opcode `0x92` + the `// Y` flag + the `S2D2_TS_AS` struct (`offsetof`/`sizeof`) are **byte-identical on
all four gens, including SUNDA**. **Unlike** [RangeSelect](rangeselect.md) (`0xbc`, first at
CAYMAN), AffineSelect is **defined on SUNDA** — like [Iota](./iota.md) (`0x7e`) and
[TensorScalarSelect](./ts-select.md) (`0x98`).

The **only** per-gen deltas are in the validator (not the struct):

* **SUNDA (NC-v2):** `src`/`dst` `tensor2d_valid` with **`AllowedInPSUM::True`** (both may be in PSUM);
  **no** `shape_from_register` escape in the elem-count check.
* **CAYMAN / MARIANA (NC-v3/v4):** **`AllowedInPSUM::False`** (SBUF-only); **adds** the
  `shape_from_register(src.start_addr) || shape_from_register(dst.start_addr)` escape — the *"future shape
  registers"* the header foreshadows (`:40-43`) wired into the elem-count predicate.
* **MAVERICK (NC-v5):** like mariana but `has_valid_active_channel_range` →
  `has_valid_active_channel_range_with_tile(…, header.inst_flags)` — the standard MAVERICK tile
  relaxation (the same swap the cache/select ops show), **not** a struct change.

The **dispatch surface** shifts across gens: SUNDA carries `0x92` as a flat POOL `kernel_info_table`
kernel (idx17); CAYMAN+ does **not** (served via the non-KIT POOL/SEQ-decode path — the same
SUNDA-flat-vs-CAYMAN-routed pattern as the gather/indirect family). The 3-copy
`"S: TensorScalarAffineSelect"` self-names (each beside `"S: Iota"`) confirm the worker is present on the
three DEBUG gens. The op is a **core maintained `// Y` op**, not a deprecated stub. `[HIGH/OBSERVED opcode/struct/header
deltas; CARRIED per-gen POOL-KIT membership; INFERRED MAVERICK interior — header-OBSERVED only]`

---

## 8. Select-family placement — masking by POSITION

The three maintained "select" ops differ in **where the predicate comes from** and **what the two arms
are**. AffineSelect is the POOL, affine-**predicate-generating** member:

| dimension | **AffineSelect (`0x92`)** | [TensorScalarSelect](./ts-select.md) (`0x98`) | [RangeSelect](rangeselect.md) (`0xbc`) |
|---|---|---|---|
| engine | **POOL** (`POOLING_NUM_CHANNELS`) | DVE (`DVE_NUM_CHANNELS`) | DVE |
| struct | `S2D2_TS_AS` (64 B) | `S3D3_TS_SELECT` (64 B) | `S2D2_RS` (64 B) |
| mem pattern | 2-D (`TENSOR2D` src/dst) | 3-D (`TENSOR3D`) | 2-D (`TENSOR2D`) |
| predicate source | **GENERATED on-chip**: an affine Iota mask vs `0` | **LOADED** immediate 0/1 (no compare) | **GENERATED**: data vs 2 FP32 bounds |
| condition | `mask[k] <op> 0`, op ∈ {GT,GE,EQ,NE} | `pred_imm` (precomputed) | `(x cmp0 b0) && (x cmp1 b1)` |
| compared value | the **synthesized mask** (not the data) | nothing (imm predicate) | the **data** `x` (FP32) |
| PASS arm | `in[k]` (src element) | `src0` tensor **or** `src1` imm | `x` (in-range value) |
| FAIL arm | `fill_reg` (**programmable FP32**) | `src1` imm **or** `src0` tensor | **−FLT_MAX** (fixed) |
| reduce | none | none | optional Max-reduce |
| dtype | in/out PAIR, int + FP, cast | single dtype, copy-only | FP-only (FP32 hub) |
| per-gen | all 4 (SUNDA POOL-KIT; CAYMAN+ non-KIT) | all 4 incl SUNDA (DVE) | CAYMAN+ (nc ≥ V3) |
| canonical use | **triangle/band fill (triu/tril)**, positional masking | fused Copy + CopyPredicate | range-mask + argmax |

**The distinguishing feature:** AffineSelect is the **only** select whose predicate is an *affine function
of the index* (an Iota ramp), compared to the **constant `0`** — it masks by **position**, not by data
value. It needs **no predicate input and no data compare**: the mask is born from the lane/element index.
TensorScalarSelect blends by a pre-loaded immediate predicate (no compute); RangeSelect blends by a
runtime *data* compare vs programmable bounds. AffineSelect is also the only **POOL** member (the other two
are DVE) and the only one with a **programmable** fill (RangeSelect's is fixed −FLT_MAX; TensorScalarSelect's
"fill" is the other select operand). `[HIGH/OBSERVED — three headers + three validators + the engine
constants]`

> **NOTE — the triu/tril realization, concretely.** A causal-attention / upper-triangular mask is
> `out[r][c] = (c ≤ r) ? scores[r][c] : −∞`. AffineSelect builds it in one instruction: set
> `channel_multiplier` for the row axis and `step_elem` for the column axis so `mask[r][c] = r − c`
> (`base = 0`, `step_elem = −1` along the column dim, `channel_multiplier = +1` per row), choose
> `fill_mode = GreaterThanEq` (`mask ≥ 0 ⇔ r ≥ c ⇔ c ≤ r`), and set `fill_reg` to `−∞` (or any FP32
> fill). The lower-triangular (tril) variant flips the sign of either coefficient or swaps GE for a
> negated compare. The header names this directly: *"a generalized triangle-fill similar to `triu()` …
> able to specify the number that's filled in, as well as the fill pattern."* `[mechanism HIGH/OBSERVED
> from header + struct; the specific coefficient assignment INFERRED-HIGH]`

---

## 9. Reimplementation checklist

For a Vision-Q7-compatible GPSIMD rebuild, AffineSelect requires:

1. **Decode** the 64-B `S2D2_TS_AS`: `header` @0, `events` @4, `num_active_channels` @12, `in_out_dtype`
   (`DTYPE_PAIR`) @13, `fill_mode` (`AFFINE_SELECT_CMP`) @14, `fill_reg` @15, `mask_pattern` (`DATA4D`)
   @16, `src_mem_pattern` (`TENSOR2D`) @36, `dst_mem_pattern` (`TENSOR2D`) @48, `channel_multiplier`
   (`int32`) @60.
2. **Validate** (`is_valid_s2d2_ts_as`): `num_active_channels ∈ 1..128` (`POOLING_NUM_CHANNELS`);
   `tensor2d_valid(src, dtype_lo, read)` / `(dst, dtype_hi, write)` (PSUM allowed on SUNDA, SBUF-only
   CAYMAN+); `d4d_element_count(mask) == t2d_element_count(src) == t2d_element_count(dst)`;
   `fill_mode ∈ {0,1,2,3}`; `is_valid_register_read(fill_reg)`; `dtype_lo` ∈ broad set exc
   `{FP32R,UINT64,INT64,FP4}`, `dtype_hi` same **+ FP32R**.
3. **Read the fill** as an **FP32** scalar from `reg[fill_reg]` (regardless of in/out dtype).
4. **Generate the mask** per element exactly as [Iota](./iota.md): broadcast `base`/`step_elem`/
   `channel_multiplier` (`ivp_movva32`), accumulate the ramp (`ivp_addn_2x32`), scale (`ivp_muln_2x32`),
   pack (`ivp_packln_2x96`); add `channel·channel_multiplier` per partition. **Do not read memory** for
   the mask, and **do not** index `src` by it.
5. **Compare vs `0`** by `fill_mode` (`ivp_len`/`ltn`/`eqn`/`neqn_2x32` → a `vbool`), the four
   `{GT,GE,EQ,NE}` comparators against the constant `0`.
6. **Blend** with a vbool-gated select (`ivp_selnx16t`, the [B21](../../isa/ref/b21-select-shuffle.md)
   merge-`t` form): `out[k] = pred ? cast(in[k]) : cast(fill)`, `src` read straight (`k → k`, no gather),
   `dst` written in `dtype_hi`.
7. **Engine:** route to **POOL** (`POOLING_NUM_CHANNELS = 128`), **not** DVE; one pass, no reduce.

---

## 10. Honesty ledger

**HIGH / OBSERVED** (direct header / struct compile / validator / enum / self-name byte read):
opcode `TENSOR_SCALAR_AFFINE_SELECT = 0x92 // Y` byte-identical four gens incl SUNDA
(`common.h` sunda `:229` / cayman `:226` / mariana `:231` / maverick `:234`); `S2D2_TS_AS` binds `0x92`
1:1 (`struct2opcode`, all four gens); the 64-B struct (`num_active_channels`@12, `in_out_dtype`@13,
`fill_mode`@14, `fill_reg`@15, `mask_pattern`@16 `DATA4D`, `src`@36, `dst`@48, `channel_multiplier`@60)
byte-identical four gens (`gcc offsetof`); the `AFFINE_SELECT_CMP` enum `{GT0,GE1,EQ2,NE3}`; the verbatim
header semantics `out[k] = (mask[k] <op> 0) ? in[k] : fill_value`, the mask *"calculated as described in
Iota"* and *"does not point to memory"*, the fill *"must be stored as FP32"*, the *"generalized
triangle-fill similar to triu()"*; the `DATA4D` mask descriptor (== Iota's) + `TENSOR2D` src/dst;
`is_valid_s2d2_ts_as` (`assert.h:14552`) using `POOLING_NUM_CHANNELS` + the per-gen `AllowedInPSUM` /
`shape_from_register` / `_with_tile` deltas; `s2d2_ts_as_valid_dtypes` (input exc `{fp32r,u64,i64,fp4}`,
output +fp32r); the **3-copy** `"S: TensorScalarAffineSelect"` self-name (`0x1d03e0`/`0x469da0`/
`0x734220`, POOL multiplicity) each beside `"S: Iota"`; container `sha256 b7c67e89…`. The struct-label
CORRECTION (`S2D2_TS_AS`, not "S3D3_TS"). The shared ramp vocabulary with [Iota](./iota.md) and the shared
`ivp_selnx16t` vbool-gated select with [TensorScalarSelect](./ts-select.md) / [B21](../../isa/ref/b21-select-shuffle.md).

**MED / INFERRED:** the per-channel/per-tile loop ordering + register→struct-field binding (which `a`-reg
= base/step/`channel_multiplier`/`fill_reg`) — INFERRED-HIGH from the field names + Iota's identical ramp
chain; the exact GT/GE-vs-LT/LE polarity of each compare leg (the inverted `len`/`ltn` legs realize GT/GE)
through the FLIX `.w15` desync; the specific triu/tril coefficient assignment (§8 NOTE); the CAYMAN+
dispatch surface (non-KIT, by analogy to the gather/indirect family).

**HIGH / OBSERVED (firmware, decoded on disk):** the **CAYMAN POOL `kernel_info_table`
absence of `0x92`** — the CAYMAN MAIN table (embedded in `libnrtucode_internal.so` @ file `0x2f6be0`, 17
entries) decodes to `7e 7c 7d 45 51 41 f0(×5) 52 46 47 be f2 7b` with no `0x92`, cross-checked by `idx0
{0x7e→0x01000080}` (iota) and `idx14 {0xbe→0x01004204}` (RangeSelect); MARIANA / MARIANA_PLUS identical.

**CARRIED (prior SUNDA carve; the SUNDA EXTISA container is not in the checked-in tree):** the
SUNDA POOL `kernel_info_table` row `idx17 {0x92 → 0x0100a118}` + the `idx15/16 {0x43/0x44}` context + the
`pool_tensor_scalar_affine_select` JSON name; the SUNDA worker body IVP census
(`movva32×6/addn_2x32×9/muln_2x32/packln`, the four `fill_mode`-guarded compare legs, the `ivp_selnx16t`
blend, zero gather ops) and its funcVA `0x0100a118`. No SUNDA EXTISA image is present in this extraction,
so these cannot be re-read on disk (tagged `[CARRIED]` in-line). The *engine = POOL* conclusion itself is
HIGH/OBSERVED (validator `POOLING_NUM_CHANNELS` + the 3-copy self-name + the CAYMAN-absence), independent
of the absent SUNDA carve.

**LOW / NOT CLAIMED:** the MAVERICK firmware body bytes + its POOL-KIT membership (carve stripped;
header-grounded, v5 interior INFERRED); the exact CAYMAN+ non-KIT handler entry; any NKI `affine_select`
op surface (the [VAL-12 predicate/classify validation](../../validation/predicate-classify.md)
will reconcile the live AffineSelect signature against this decode).

**FLIX-desync flag:** the SUNDA POOL worker desyncs under stock objdump on the recurring
`.byte 0x2f/0x4f/0x5f/0x6f/0x8f` literal-pool/FLIX lead bytes (the documented limit). The live IVP slots
within each bundle (the §5 ramp/compare/select ops) are real and byte-cited in the prior carve; the surrounding
`.w15` branch *targets* in a linear sweep are reported structurally. The opcode/struct/validator/enum/
self-name-offset bytes are decode-immune (header compile + `readelf` + byte-scan, no instruction decode).

---

## See also

* [Iota / sequence-index generator](./iota.md) — the `0x7e` POOL ramp generator AffineSelect's mask
  reuses verbatim (`base + Σ idx·step + channel·channel_multiplier`, `movva32`/`addn`/`muln`/`packln`).
* [TensorScalarSelect](./ts-select.md) — the `0x98` DVE immediate-predicate blend whose `ivp_selnx16t`
  select-vs-fill datapath AffineSelect's Phase 3 shares.
* [RangeSelect](rangeselect.md) — the `0xbc` DVE *data*-compare select contrast (runtime bounds, fixed
  −FLT_MAX fill, FP-only).
* [ISA Batch 21 — Select / Shuffle / Compress](../../isa/ref/b21-select-shuffle.md) — the vbool-gated
  `ivp_selnx16t`/`SEL`/`DSEL` crossbar that realizes the blend.
* [VAL-12 — Predicate / Classify / Compare family validation](../../validation/predicate-classify.md)
  — the live AffineSelect validation that reconciles this decode against the runtime op.
