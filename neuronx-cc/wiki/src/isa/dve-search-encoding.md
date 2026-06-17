# DVE Search & Datamove Encoding — Max8 / FindIndex8 / MatchReplace / Nonzero

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 are byte-identical for the C++ core logic). The encoders, validators, and wire-struct field-name tables live in `libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset; build-id `92b4d331a42d7e80bb839e03218d2b9b0c23c346`, stripped); the BIR instruction classes and `EngineType` enum live in `libBIR.so` (build-id `a9b1ea38c47e579178b179fd445aa8edd593f206`). Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

This page is the byte-for-byte field map of the **DVE search / match / compaction primitives** — the top-k, argmax, match-and-strike, and nonzero-compaction ops the Vector engine ([1.11 DVE engine](../arch/dve-engine.md)) emits onto the wire. They are the silicon back-ends of the NKI top-k / index / select intrinsics ([6.7.12](../nki/topk-primitives.md)): **Max8** (running top-8), **FindIndex8** (argmax indices), **MatchReplace8** / **MaxIndexAndMatchReplace** (strike the matched values to a sentinel, optionally with index out), and **NonzeroWithCount** (sparse/MoE compaction). Two helper micro-ops — **MatchValueLoad** and **LoadMaskSelect** — have validators and wire structs but no standalone visitor; they are *co-issued* as the first half of a larger op, exactly the idiom the Gather family uses ([2.12 pool/reduce](pool-reduce-encoding.md), `pool_buffer_load`+`reg_load`).

Every bundle is a `std::array<unsigned char, 64>`: emplaced into a `SmallVector`, `pxor`+`movups`-zeroed in full (so any byte the encoder does not write is a hard `0x00`), header-stamped by `setupHeader` (vtable slot 9), field-filled, ISA-checked by `runISACheck` (`@0x12095A0`), and `fwrite(buf, 1, 0x40, findBin(I))`-ed. The control band is **Family-C** ([2.1 the bundle](instruction-bundle.md)): `+0x20` in-dtype, `+0x21` out-dtype, `+0x22` lane (= `numElementsPerPartition` of src), source access pattern at `+0x0C` / `+0x10`, dest at `+0x2C` / `+0x30` — identical to the BatchNorm family ([2.11](batchnorm-encoding.md)), because Max8 *shares* its wire struct (`NEURON_ISA_TPB_S4D2_BN_STRUCT`) with BatchNormStats.

The bar for this page: a reader can **byte-encode any DVE search/match instruction by hand**, knowing for each control byte its offset, width, semantic, value source, the recovered `s_<op>_*` / `d4_mr_*` / `d2_bn_*` struct-field name, and the disassembly store-site that pins it. Every field row carries a confidence tag (CONFIRMED = exact store/cmp disassembled byte-exact; STRONG = validator/LUT/helper xref cross-checked; INFERRED = zero-init implied, no direct store; SPECULATIVE). No field name is fabricated — every name on this page is a literal string recovered from the `libwalrus.so` `.rodata` predicate-name tables.

## At a glance

| Opcode | BIR inst (IT#) | Generator (libwalrus) | Engine | Wire struct | Operand band(s) |
|---|---|---|---|---|---|
| `0x6C` Max8 | `InstMax` (IT88) | `CoreV2GenImpl::visitInstMax` `@0x1273120` | DVE (5) | `…_S4D2_BN_STRUCT` (`s_max8_*` / `d2_bn_*`) | data src `TENSOR4D` @`+0x0C`, maxvals dst `MEM_PATTERN2D` @`+0x30` |
| `0x6D` MatchValueLoad | *(co-issued helper)* | — *(emitted inside MaxIndex / MatchReplace)* | DVE (5) | `D4_MR` (`s_match_value_load_opcode`) | maxvals src pattern @`+0x10` (or `+0x10` 2-D) |
| `0x6E` FindIndex8 | `InstMaxIndex` (IT89) | `CoreV2GenImpl::visitInstMaxIndex` `@0x1254650` | DVE (5) | `…_S4D2_BN_STRUCT` (`s_find_index8_*`) | data src `TENSOR4D` @`+0x0C`, idx dst `TENSOR2D` @`+0x30` |
| `0x6F` MatchReplace8 | `InstMatchReplace` (IT90) | `CoreV2GenImpl::generateInstMatchReplaceWithOptionalMaxIndex(…, false)` `@0x1244ab0` | DVE (5) | `…_D4_MR_STRUCT` (`s_match_replace8_*` / `d4_mr_*`) | data src `TENSOR4D` @`+0x0C`, sentinel @`+0x28`, replaced dst `TENSOR4D` @`+0x2C` |
| `0x6F`+idx MaxIndexAndMatchReplace | `InstMaxIndexAndMatchReplace` (IT91) | `…WithOptionalMaxIndex(…, true)` `@0x1244ab0` + `CoreV3GenImpl::visitInstMaxIndexAndMatchReplace` `@0x135b670` | DVE (5) | `D4_MR` + gen3 index bundle | as IT90 + index output |
| `0x69` LoadMaskSelect | *(co-issued helper)* | — *(emitted inside `visitInstStreamTranspose` `@0x1266df0`)* | DVE (5) datamove | `s_load_mask_select_opcode` | 32-byte lane-select mask @`+0x20..+0x3F` |
| `0xF2` NonzeroWithCount | `InstNonzeroWithCount` (IT104) | `CoreV3GenImpl::visitInstNonzeroWithCount` `@0x1355a30` **(gen3+ only)** | Pool | `…_S3D3_NONZERO_WITH_COUNT_STRUCT` (`s_nonzero_with_count_*`) | data src `TENSOR3D` @`+0x10`, idxOff/padVal i32 @`+0x20`/`+0x28`, out `TENSOR3D` @`+0x2C` |

**Header skeleton (all opcodes), from `setupHeader` (vtable slot 9):**

```
 byte +0x00  opcode        (low byte of the opcode word; 0x6C/0x6D/0x6E/0x6F/0x69/0xF2)
 byte +0x01  inst_word_len = 0x10  (16 dwords = 64 bytes)
 byte +0x02..+0x03  reserved = 0x0000
```

The header is stamped by a `call *0x48(%rax)` to `setupHeader`, which copies the opcode byte from a seed the encoder writes onto the stack (e.g. Max8 `movb $0x6c,-0x6e2(%rbp)` `@0x12732aa`). CONFIRMED — the same vtable-slot-9 idiom as [2.10](pe-matmul-encoding.md).

> **GOTCHA — `0x6D` (MatchValueLoad) is *not* "MaxIndex".** The argmax/index BIR op is `bir::InstMaxIndex` (IT89), whose CoreV2 encoder `visitInstMaxIndex` emits opcode `0x6E` (FindIndex8) and *co-issues* a `0x6D` (MatchValueLoad) helper bundle ahead of it. There is no standalone "MaxIndex" wire opcode; "MaxIndex" is the BIR/IT-89 name, **FindIndex8 (`0x6E`) is its wire op**, and `0x6D` MatchValueLoad is a separate locate-pass micro-op. Pinned by `dbg_is_valid_find_index8` `cmp $0x6e` (`@0x1306793`), `dbg_is_valid_match_value_load` `cmp $0x6d` (`@0x12bbdbe`).

---

## The bundle lifecycle (shared by all six)

Every generator runs the same `CodeGenMode` (`*(this+0x270)`) cycle, identical to the PE / Pool families:

1. **Emplace + zero.** `SmallVectorImpl<std::array<u8,64>>::emplace_back`, then `pxor`+`movups` zero-stores blanket all 64 bytes. *Consequence: every reserved/unwritten byte reads `0x00`.*
2. **Header.** `setupHeader` (vtable slot 9) → `bundle[0]=opcode`, `bundle[1]=0x10`, `bundle[2:4]=0`.
3. **Access patterns.** `assignAccess<TENSOR4D>`/`<TENSOR2D>`/`<TENSOR3D>` into the source band (`+0x0C` 4-D, `+0x10` 2-/3-D) and dest band (`+0x2C` 4-D, `+0x30` 2-D).
4. **Control band.** the per-opcode field fills mapped below (`+0x20`/`+0x21`/`+0x22` Family-C dtype/lane, plus op-specific imms/masks).
5. **ISA check + emit.** `runISACheck` (`sub_12095A0` `@0x12095A0`) appends the bundle to the engine stream and runs the in-place validity check; then `fwrite(buf,1,0x40,findBin(I))` and a census `++map<opcode>` (`this+0x1e0`).

`CodeGenMode` arms: **0 = COLLECT_OPCODES** (push the opcode int into the per-inst `_Rb_tree` set, emit nothing); **1 = GENERATE_ISACODE** (fill + `fwrite`); **2 = RUN_ISA_CHECKS** (build the *byte-identical* bundle on a stack frame, feed the validator, no `fwrite`). The field map below is the GENERATE path; modes 1 and 2 share it.

**Shared helpers.** The dtype LUT `sub_120E650` (`byte_1df5760` table, V2/V3) maps a BIR `Dtype` (read from `AP+0x30`) to a `NEURON_ISA_TPB_DTYPE` wire tag; CoreV3's NonzeroWithCount uses its own `sub_1348870`. The lane byte is `numElementsPerPartition(src)` low byte. Validators are the weak `neuronxcc::core_v{2,3,4}::dbg_is_valid_<op>` family; each pins its opcode by a `cmp`.

> **NOTE — Max8 ≡ BatchNormStats wire struct.** Max8 emits the `NEURON_ISA_TPB_S4D2_BN_STRUCT` (alias `s4d2_bn`) — the 4-D-src / 2-D-dst struct shared with BatchNormStats. Its dst-pattern fields therefore carry `d2_bn_*` names (`d2_bn_ge8_src_element_cnt`, `d2_bn_zero_count`, `d2_bn_reserved_z`) alongside the `s_max8_*` opcode/dst fields. This is why the dst num_elem hard-asserts to 8 and the high dims read the BatchNorm reserved pattern (`+0x36=0` / `+0x3a=1`).

---

## Max8 — `InstMax` (IT88, opcode `0x6C`)

The DVE "running top-8" reduction: one src row in, **8 fp32 maxvals out** (descending). The "8" is the silicon DVE lane width — it is **not a bundle field**; it falls out of the dst access-pattern `num_elem`, which the encoder hard-asserts `== 8`. Wire struct `NEURON_ISA_TPB_S4D2_BN_STRUCT`. Generator `CoreV2GenImpl::visitInstMax @0x1273120` (bundle base in `%r14`).

**Operands:** `getArgument<AccessPattern>(0)` = DATA src; `getOutput<PhysicalAccessPattern>(0)` = MAXVALS dst.

**Pre-emit legality asserts** (CONFIRMED):
- src must NOT be a TensorIndirect AP, else `reportError "TODO: support MAX8 with TensorIndirect AP once ISA supports it"`.
- dst `numElementsPerPartition == 8` — `cmp $0x8,%eax;jne` (`@0x12733a9`).
- src `numElementsPerPartition` in `[8 .. 16384]` — `cmpl $0x7,…;jbe(err)` (`@0x12733b2`) and `cmpl $0x4000,…;ja(err)` (`@0x12733bf`).
- src AP rank ≤ 5, maxvals AP dims ≤ 3.

| Off | W | Field | `s4d2_bn` name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x6C` | `s_max8_opcode` | setupHeader (seed `movb $0x6c` `@0x12732aa`); COLLECT seed `movl $0x6c,-0x150(%rbp)` `@0x1273221` | hdr | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | — | hdr constant | hdr | CONFIRMED |
| `+0x02` | 2 | reserved = `0x0000` | (header pad) | hdr | hdr | CONFIRMED |
| `+0x04` | .. | semaphore wait/update | (s4d2_bn events) | `setupSyncWait`/`Update` | — | STRONG |
| `+0x0C` | ~14 | DATA src 4-D mem-pattern | (src `TENSOR4D`) | `assignAccess<TENSOR4D>` `@0x12754f7` | call site | CONFIRMED |
| `+0x20` | 1 | in-dtype (wire tag) | `in_dtype` | `sub_120E650(src.Dtype@+0x30)` | `0x12754d0` | CONFIRMED |
| `+0x21` | 1 | out-dtype (wire tag) | `out_dtype` | `sub_120E650(dst.Dtype@+0x30)` | `0x12754ed` | CONFIRMED |
| `+0x22` | 1 | lane = `numElementsPerPartition(src)` low byte | `d2_bn_ge8_src_element_cnt` | src lane width | `0x12754f1` | CONFIRMED |
| `+0x30` | 2 | dst `start_addr.addr_immediate` | `s_max8_dst` start_addr | setter (`0x1d6cb40`) | `0x12755d0` | CONFIRMED |
| `+0x34` | 2 | dst `step_elem[0]` = −(dst step) | dst_mem_pattern.step_elem[0] | setter (`0x1d6caf0`); negated (descending) | `0x1275539` | CONFIRMED |
| `+0x36` | 2 | dst `step_elem[1]` = `0x0000` | (dst step dim1) | `mov %cx,0x36(%r14)` | `0x127556c` | CONFIRMED |
| `+0x38` | 2 | dst `num_elem[0]` = `8` | `s_max8_dst` num_elem[0] | setter (`0x1d6cb18`); the 8 maxval slots | `0x127555e` | CONFIRMED |
| `+0x3A` | 2 | dst `num_elem[1]` = `0x0001` | (dst num dim1) | `mov %si,0x3a(%r14)` | `0x1275571` | CONFIRMED |
| others | — | reserved-zero | `d2_bn_zero_count` / `d2_bn_reserved_z` | pxor zero-init; asserted `==0` | — | INFERRED |

**Emit:** `runISACheck` `@0x12755e4`; `findBin` `@0x12755ef`; `fwrite(bundle,1,0x40,bin)` `@0x1275604`; census `++map<0x6C>` `@0x127562d`.

---

## FindIndex8 + MatchValueLoad — `InstMaxIndex` (IT89), the co-issued argmax pair

`bir::InstMaxIndex` (the argmax / IT89) does **not** lower to a single bundle. Its CoreV2 encoder `visitInstMaxIndex @0x1254650` emits **two** 64-byte bundles, in order:

1. **MatchValueLoad** (opcode `0x6D`) — locate the 8 maxvals in the data row;
2. **FindIndex8** (opcode `0x6E`) — emit the 8 argmax indices (uint32).

This is why `0x6D` MatchValueLoad has a validator + struct but no standalone `visitInst` encoder: it is only ever produced as the first half of MaxIndex (and of MatchReplace, below). COLLECT inserts `0x6D` *then* `0x6E` into the per-inst opcode set — `movl $0x6d,-0x150(%rbp)` `@0x12546d3` then `movl $0x6e,…` `@0x1254732` — pinning both the pair and its order. CONFIRMED.

**3 BIR operands:** op0 = MAXVALS, op1 = DATA, op2 = INDICES (out). Distribution: bundle #1 (MatchValueLoad) uses the MAXVALS pattern as its src; bundle #2 (FindIndex8) uses DATA (4-D src) + INDICES (2-D dst). Same TensorIndirect guard as Max8 (`reportError "TODO: support MATCH_VALUE_LOAD with TensorIndirect AP once ISA supports it"`).

### Bundle #1 — MatchValueLoad (`0x6D`), base `%r13`

| Off | W | Field | Name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x6D` | `s_match_value_load_opcode` | setupHeader (seed `movb $0x6d,-0x994(%rbp)` `@0x1254bba`) | hdr | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | — | hdr constant | hdr | CONFIRMED |
| `+0x10` | 2 | src `start_addr.addr_immediate` | src_mem_pattern.start_addr | setter (`0x1d6a330`) | `0x1255ffd` | CONFIRMED |
| `+0x14` | 2 | src `step_elem[0]` = −(maxvals step) | src_mem_pattern.step_elem[0] | setter (`0x1d6a4d8`) | `0x1255f55` | CONFIRMED |
| `+0x16` | 2 | src `step_elem[1]` = `0x0000` | (src step dim1) | `mov %cx,0x16(%r13)` | `0x1255f8e` | CONFIRMED |
| `+0x18` | 2 | src `num_elem[0]` = `8` | src_mem_pattern.num_elem[0] | setter (`0x1d6a360`); the 8 values to locate | `0x1255f7a` | CONFIRMED |
| `+0x1A` | 2 | src `num_elem[1]` = `0x0001` | (src num dim1) | `mov %si,0x1a(%r13)` | `0x1255f89` | CONFIRMED |
| `+0x20` | 1 | in-dtype | `in_dtype` | `sub_120E650(maxvals.Dtype@+0x30)` | `0x1255f14` | CONFIRMED |
| `+0x22` | 1 | lane = `numElementsPerPartition` | lane / element_cnt | src lane width | `0x1255f10` | CONFIRMED |

> **NOTE — no `+0x21` out-dtype on MatchValueLoad.** It is a single-pattern *load* micro-op (one src band, no dst), so `+0x21` is never written and reads `0`. CONFIRMED.

**Emit:** `runISACheck` `@0x1256010`; `fwrite` `@0x1256030`; census `++map<0x6D>`.

### Bundle #2 — FindIndex8 (`0x6E`), base `%r13`

| Off | W | Field | `s4d2_bn` name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x6E` | `s_find_index8_opcode` | setupHeader | hdr | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | — | hdr constant | hdr | CONFIRMED |
| `+0x0C` | ~14 | DATA src 4-D mem-pattern | (src `TENSOR4D`) | `assignAccess<TENSOR4D>` `@0x1257db6` | call site | CONFIRMED |
| `+0x20` | 1 | in-dtype | `in_dtype` | `sub_120E650(DATA.Dtype@+0x30)` | `0x1257d8f` | CONFIRMED |
| `+0x21` | 1 | out-dtype (uint32 tag) | `out_dtype` | `sub_120E650(IDX.Dtype@+0x30)` | `0x1257dac` | CONFIRMED |
| `+0x22` | 1 | lane = `numElementsPerPartition` | `s_find_index8_dst_element_cnt` | src lane width | `0x1257db0` | CONFIRMED |
| `+0x30` | ~10 | INDICES dst 2-D mem-pattern | (dst `TENSOR2D`; 8 uint32 slots) | `assignAccess<TENSOR2D>` `@0x1257dc7` | call site | CONFIRMED |

**Emit:** `runISACheck` `@0x1257dda`; `fwrite` `@0x1257dfe`; census `++map<0x6E>`.

The index dst is a flat `[partition, 8]` `TENSOR2D` block of uint32, mirroring Max8's 8-slot maxvals dst; the DATA src is a full `TENSOR4D`. The `-1`=`0xFFFFFFFF` index preset some kernels expect is a **simulator initialization**, not a bundle field (see the NOTE below). Both bundles share the same `findBin`/engine stream (co-issued, DVE=5).

---

## MatchReplace8 / MaxIndexAndMatchReplace — `InstMatchReplace` (IT90) / `InstMaxIndexAndMatchReplace` (IT91)

Both BIR visitors tail-call the **same** generator with a bool selector, the ISA-layer shadow of the codegen-side IT90/IT91 collapse:

- `CoreV2GenImpl::visitInstMatchReplace @0x1247020` is a **2-instruction thunk**: `xor %edx,%edx ; jmp generateInstMatchReplaceWithOptionalMaxIndex(inst, false)` → IT90 (no index out). CONFIRMED — disassembled exactly as `31 d2` / `e9 …` at `0x1247020`.
- The IT91 path forwards with `bool=true`. CoreV3 has a dedicated `visitInstMaxIndexAndMatchReplace @0x135b670` that FIRST does `mov $0x1,%edx` (`@0x135b684`) then `call generate…` (`@0x135b6b4`) — emitting the `{MatchValueLoad 0x6D, MatchReplace8 0x6F}`+index pair exactly as IT90 — THEN continues with CoreV3-specific argmax-index bundle work.

The real encoder is `generateInstMatchReplaceWithOptionalMaxIndex(InstMatchReplace&, bool) @0x1244ab0`; the bool rides `%edx`, saved to `-0x5d8(%rbp)` and tested `cmpb $0x0,-0x5d8(%rbp)` (`@0x1246a25`). COLLECT inserts `0x6D` then `0x6F`. The op co-issues:

1. **MatchValueLoad** (`0x6D`) — locate the 8 maxvals (2-D src pattern);
2. **MatchReplace8** (`0x6F`) — strike the matches to the **sentinel**; carries the replace immediate at `+0x28` and (if `withMaxIndex`) the argmax index output.

**Operands:** `getArgument(1)` = DATA, `getOutput(0)` = REPLACED output; `getArgument(0)` (maxvals) drives the `0x6D` locate bundle. Same `8 ≤ n ≤ 16384` / rank guards as Max8.

### Bundle #1 — MatchValueLoad (`0x6D`), base `%r14`

| Off | W | Field | Name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x6D` | `s_match_value_load_opcode` | setupHeader | hdr | CONFIRMED |
| `+0x10` | ~10 | maxvals 2-D src mem-pattern | (src `TENSOR2D`) | `assignAccess<TENSOR2D>` `@0x1246114` | call site | CONFIRMED |
| `+0x20` | 1 | in-dtype | `in_dtype` | `sub_120E650(maxvals.Dtype)` | `0x1246109` | CONFIRMED |
| `+0x22` | 1 | lane = `numElementsPerPartition` | lane / element_cnt | src lane width | `0x12460f9` | CONFIRMED |

**Emit:** `runISACheck` `@0x1246127`; `fwrite` `@0x1246147`; census `++map<0x6D>` `@0x124617b`.

### Bundle #2 — MatchReplace8 (`0x6F`), base `%r15`, wire struct `NEURON_ISA_TPB_D4_MR_STRUCT`

| Off | W | Field | `d4_mr` name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x6F` | `s_match_replace8_opcode` | setupHeader | hdr | CONFIRMED |
| `+0x0C` | ~14 | DATA src 4-D mem-pattern | (src `TENSOR4D`) | `assignAccess<TENSOR4D>` `@0x1246a91` | call site | CONFIRMED |
| `+0x20` | 1 | in-dtype | `in_dtype` / `match_replace_dtype` | `sub_120E650(DATA.Dtype)` | `0x1246a49` | CONFIRMED |
| `+0x21` | 1 | out-dtype | `out_dtype` | `sub_120E650(REPLACED.out Dtype)` | `0x1246a67` | CONFIRMED |
| `+0x22` | 1 | lane = `numElementsPerPartition` | lane / `d4_mr_le16k_src_*` | src lane width | `0x1246a72` | CONFIRMED |
| `+0x28` | 4 (fp32) | **replace SENTINEL** | `instr.immediate` (replace value) | setter (`0x1244420`) reads `*(float*)(inst+0xF0)` — the codegen-written `imm_value` (`= −inf` in TopK) | `lea 0x28(%r15),%rdi` `@0x1246a5c` | CONFIRMED |
| `+0x2C` | ~14 | REPLACED dst 4-D mem-pattern | (dst `TENSOR4D`; matches struck out) | `assignAccess<TENSOR4D>` `@0x1246aa2` | call site | CONFIRMED |
| others | — | reserved-zero / guard | `d4_mr_reserved_zero` / `d4_mr_same_src_dt` | pxor zero-init | — | INFERRED/STRONG |

**Emit:** `runISACheck` `@0x1246ab5`; `fwrite` `@0x1246ad5`; census `++map<0x6F>` `@0x1246af2`.

**With-max-index (IT91):** when `bool=true`, a setup at `0x1246c9f` binds the extra index output before joining the *same* field-store block at `0x1246a39` — so the `+0x28` sentinel and the `+0x0C`/`+0x2C` AP band are emitted **byte-identically** to IT90; the index output rides as an additional bound output. CoreV3's `visitInstMaxIndexAndMatchReplace` then adds the gen3 index bundle. The sentinel-at-`+0x28` = the `imm_value@inst+0xF0` is shared between IT90 and IT91 (the BIR `sameInst{90,91}` float-compares the imm at `+0xF0`). NKI surface: `nc_match_replace8` (IT90) / `nc_match_replace_indices8` (IT91).

> **NOTE — the `−1` sentinel convention.** Two distinct `−1`-typed sentinels live in this family, and only one is a *bundle field*: (1) **MatchReplace's replace value** at bundle `+0x28` is a real fp32 immediate (`= −inf`/min-value in a TopK fold), sourced from `inst+0xF0` — CONFIRMED as a wire field. (2) The **FindIndex8 / NonzeroWithCount `−1` index/pad value** for "no match" / "empty slot" is the integer `−1` (`0xFFFFFFFF`). For NonzeroWithCount it *is* a wire field — `paddingVal` at `+0x28` (see below). For FindIndex8 the `-1` index preset is a **simulator init only**, not a FindIndex8 bundle byte. Do not lay a `-1` into the FindIndex8 dst pattern; do lay it into the MatchReplace `+0x28` (as the encoded fp32) and the NonzeroWithCount `paddingVal` `+0x28` (as the int32 `−1`).

---

## LoadMaskSelect — opcode `0x69`, the mask-driven select-load datamove

LoadMaskSelect (`0x69`) has **no** `visitInstLoadMaskSelect` — it is one of the bundles that **StreamTranspose** (opcode `0x6B`) decomposes into. `CoreV2GenImpl::visitInstStreamTranspose @0x1266df0` emits a `0x69` LoadMaskSelect bundle (COLLECT seed `movl $0x69,-0x160(%rbp)` `@0x1267459`; GENERATE setupHeader `movb $0x69` `@0x12680d7`) as part of the transpose datamove — the same co-issued-datamove idiom as the Gather family. Validator `core_v2::dbg_is_valid_load_mask_select @0x1286290` (`cmp $0x69` `@0x12864e9`).

**Key structure — a 32-byte lane-select mask at `+0x20..+0x3F` (NOT a dtype band).** Unlike the Max/Match family, LoadMaskSelect's control band is a 32-byte per-lane bit-mask built in-line with SSE from a single scalar width (`%ebx`, the transpose select count):

- scalar `%ebx` → `movd %ebx,%xmm0; punpcklqdq` broadcast (`@0x12680f4`);
- 16 lane-index constant vectors (`.rodata 0x1dd66d0..0x1dd67c0`) are `pcmpgtq`-compared against the broadcast width, `pandn`/`packusdw`/`packuswb`-folded to bytes, then `paddb`×5 packs the per-lane select bits;
- first 16-byte half → `movups %xmm1,0x20(%r8)` (`@0x126828b`); second half → `movups %xmm0,0x30(%r8)` (`@0x126838a`).

The validator reads bundle bytes one-by-one across `+0x20..+0x3F` (`movzbl 0x332N(%rsp)` `@0x12862d9..0x1286399`), confirming the whole region is the select mask.

| Off | W | Field | Name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x69` | `s_load_mask_select_opcode` | setupHeader | hdr | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | — | hdr constant | hdr | CONFIRMED |
| `+0x04` | .. | sem / sync region | events | `setupSync` helper (`0x1231240`) | — | STRONG |
| `+0x20..+0x2F` | 16 | select-mask low half | `load_mask_select` mask[0] | `movups %xmm1,0x20(%r8)` (per-lane bits from width `%ebx`) | `0x126828b` | CONFIRMED |
| `+0x30..+0x3F` | 16 | select-mask high half | `load_mask_select` mask[1] | `movups %xmm0,0x30(%r8)` | `0x126838a` | CONFIRMED |
| `+0x08..+0x1F` | — | load source descriptor / sync | — | zero where unused | — | INFERRED |

**Emit:** `runISACheck` `sub_12095A0` `@0x1268396`; `fwrite` `@0x12683be`.

**Semantics (STRONG):** the 32-byte mask is the per-lane enable bitmap (up to 256 lane bits) for which lanes/partitions the streaming transpose load writes; the SSE builder turns the transpose width into the contiguous enable run. It is a datamove op — no in/out dtype, no AP step/num pattern in `+0x20..+0x3F` (those bytes ARE the mask). The `0x69` ISA micro-op is distinct from any BIR `InstLoadMaskSelect` surface name; here it is purely the bundle StreamTranspose lowers to (no `klr::LoadMaskSelect` node exists).

---

## NonzeroWithCount — `InstNonzeroWithCount` (IT104, opcode `0xF2`), gen3+ only

The sparse / MoE-routing **compaction** op. Wire struct `NEURON_ISA_TPB_S3D3_NONZERO_WITH_COUNT_STRUCT`. Generator `CoreV3GenImpl::visitInstNonzeroWithCount @0x1355a30` (bundle base `%r13`); validator `core_v3::dbg_is_valid_nonzero_with_count @0x13d8d80` (`cmp $0xf2` `@0x13d8f05`, plus a `NEURON_ISA_TPB_NEURON_CORE_VERSION` second arg).

> **GEN3+ ONLY.** There is **no** CoreV2 encoder *or* validator for `0xF2`. The first encoder/validator appear at core_v3 and the validator takes a `CORE_VERSION` discriminant (the body does `cmpb $0x2,…` on a version byte). In this binary the op is gen3-resident; gen2 silicon has no native `0xF2` bundle (it borrows the gen3 emitter). Whether gen2 silicon *originates* the op is an open arch question, not decided by the binary.

**Operands:** `getArgument<AccessPattern>(0)` = INPUT DATA; `getArgument<ImmediateValue>(0)` = `indexOffset` (int32); `getArgument<ImmediateValue>(1)` = `paddingVal` (int32, `= −1`); `getOutput<AccessPattern>(0)` = the compaction output `[idx…, −1…, count]`. The imm fetch uses a per-dtype jump table (`jpt @0x1df9720` for imm0, `@0x1df9770` for imm1) extracting the int from `ImmediateValue+0x48`.

| Off | W | Field | `S3D3` name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0xF2` | `s_nonzero_with_count_opcode` | setupHeader (seed `movb $0xf2` `@0x1355b87`); COLLECT `movl $0xf2,-0xb0(%rbp)` `@0x1355b18` | hdr | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | — | hdr constant | hdr | CONFIRMED |
| `+0x04` | .. | sem wait/update region | events | `setupSync<S3D3_…>` | — | STRONG |
| `+0x0C` | 1 | src AP last-pair dim byte | (src 3-D pattern hdr) | `mov %al,0xc(%r13)` | `0x1355bd8` | CONFIRMED |
| `+0x0D` | 1 | in-dtype | `s_valid_nonzero_with_count_dtype` (in) | CoreV3 LUT `sub_1348870(input.Dtype)` | `0x1355be9` | CONFIRMED |
| `+0x0E` | 1 | out-dtype | out_dtype | CoreV3 LUT `sub_1348870(output.Dtype)` | `0x1355c88` | CONFIRMED |
| `+0x0F` | 1 | reserved = `0` | `d3_nonzero_with_count_reserved_z` | `movb $0,0xf(%r13)` | `0x1355bf2` | CONFIRMED |
| `+0x10` | ~10 | INPUT 3-D data mem-pattern | (src `TENSOR3D`) | `assignAccess<TENSOR3D>` `@0x1355bc0` | call site | CONFIRMED |
| `+0x20` | 4 (i32) | `indexOffset` | indexOffset | `ImmediateValue(0).getValue<int>` (base added to each emitted idx) | `0x1355c1b` | CONFIRMED |
| `+0x24` | 1 | reserved = `0` | reserved_z | `movb $0,0x24(%r13)` | `0x1355c2c` | CONFIRMED |
| `+0x28` | 4 (i32) | `paddingVal` = `−1` | paddingVal (PADDING_VALUE) | `ImmediateValue(1).getValue<int>` | `0x1355c53` | CONFIRMED |
| `+0x2C` | ~10 | OUTPUT 3-D compaction pattern | (dst `TENSOR3D`; `container_nonzero_cardinality`) | `assignAccess<TENSOR3D>` `@0x1355c74` | call site | CONFIRMED |

**Emit:** `runISACheck` `@0x1355c90`; `findBin`; `fwrite` `@0x1355cb0`; census `++map<0xF2>` `@0x1355ccd`.

**Semantics:** output `[idx1, idx2, …, −1, −1, …, count]` int32 — nonzero-element indices packed front (each `+ indexOffset`), tail padded with `paddingVal` (`−1`), total nonzero count last. The slot-count metadata (`container_nonzero_cardinality` / `count_element_co*`) rides the output 3-D pattern dims; `s_valid_nonzero_with_count_dtype` is the dtype-pair legality predicate. **Engine: Pool** (not DVE). The op is lowered to real CoreV3/V4 hardware bundles even though the BIR simulator stubs it.

> **CORRECTION — opcode roster.** Earlier drafts that listed "MaxIndex / FindIndex8 = `0x6D` / `0x6E`" are wrong: `0x6D` is **MatchValueLoad** (a separate co-issued micro-op) and FindIndex8 is `0x6E`. The correct ordinals, re-pinned by the `cmp` in each `dbg_is_valid_*` validator, are: Max8 `0x6C`, MatchValueLoad `0x6D`, FindIndex8 `0x6E`, MatchReplace8 `0x6F`, LoadMaskSelect `0x69`, NonzeroWithCount `0xF2`.

---

## Cross-arch stability

The **Max family** (`0x6C` / `0x6D` / `0x6E` / `0x6F`) has **no** separate CoreV3/CoreV4 `visitInst` encoder — gen3/gen4 reuse the CoreV2 bodies (`visitInstMax @0x1273120`, `visitInstMaxIndex @0x1254650`, the shared `generateInstMatchReplaceWithOptionalMaxIndex @0x1244ab0`). Only the dtype LUT differs by arch (`sub_120E650`/`byte_1df5760` vs the core_v4 table); the bundle offset layout (`+0x20` in / `+0x21` out / `+0x22` lane / mem-patterns) is arch-stable. Only the **gen3+ ops** have dedicated CoreV3 bodies: `visitInstNonzeroWithCount @0x1355a30` (`0xF2`) and `visitInstMaxIndexAndMatchReplace @0x135b670` (IT91). Validators exist for all three arches (core_v2/v3/v4 `dbg_is_valid_*`), each pinning its opcode by `cmp` over the same field offsets; only the NonzeroWithCount validator carries the `CORE_VERSION` arch-gate arg.

## Confidence ledger

**CONFIRMED** (direct store/cmp/seed disassembled byte-exact this task):
- Opcodes pinned by validator `cmp`: Max8 `0x6C` (`@0x13047fb`), MatchValueLoad `0x6D` (`@0x12bbdbe`), FindIndex8 `0x6E` (`@0x1306793`), MatchReplace8 `0x6F` (`@0x1316ca2`), LoadMaskSelect `0x69` (`@0x12864e9`), NonzeroWithCount `0xF2` (`@0x13d8f05`).
- Encoder/thunk addresses: `visitInstMax @0x1273120`, `visitInstMaxIndex @0x1254650`, `visitInstMatchReplace @0x1247020` (`xor %edx,%edx; jmp 0x61eff0`), `generateInstMatchReplaceWithOptionalMaxIndex @0x1244ab0`, `visitInstNonzeroWithCount @0x1355a30`, `visitInstMaxIndexAndMatchReplace @0x135b670` (`mov $0x1,%edx; call`), `visitInstStreamTranspose @0x1266df0` — all from `nm -DC`.
- Max8 field stores `+0x20/+0x21/+0x22` (`@0x12754d0/ed/f1`), `+0x36/+0x3a`, dst `cmp $0x8` (`@0x12733a9`), src bounds `cmpl $0x7`/`$0x4000` (`@0x12733b2/bf`); COLLECT `movl $0x6c` (`@0x1273221`).
- MaxIndex co-issue order `movl $0x6d` (`@0x12546d3`) then `movl $0x6e` (`@0x1254732`); FindIndex8 `+0x20/+0x21/+0x22` (`@0x1257d8f/ac/b0`).
- MatchReplace8 `+0x20/+0x21/+0x22` (`@0x1246a49/67/72`), sentinel `lea 0x28(%r15)` (`@0x1246a5c`).
- LoadMaskSelect seed `movl $0x69` (`@0x1267459`); mask `movups %xmm1,0x20(%r8)` (`@0x126828b`) / `movups %xmm0,0x30(%r8)` (`@0x126838a`).
- NonzeroWithCount `+0xc/+0xd/+0xe/+0xf` (`@0x1355bd8/be9/c88/bf2`), `+0x20` idxOff (`@0x1355c1b`), `+0x24` reserved (`@0x1355c2c`), `+0x28` padVal (`@0x1355c53`).

**STRONG** (validator / rodata-name xref): wire-struct field names `s_max8_*`, `s_find_index8_dst_element_cnt`, `s_match_value_load_opcode`, `s_match_replace8_opcode`, `s_load_mask_select_opcode`, `s_nonzero_with_count_opcode`, `d2_bn_ge8_src_element_cnt`, `d2_bn_zero_count`, `d4_mr_reserved_zero`, `d4_mr_le16k_src_*`, `d4_mr_same_src_dt`, `match_replace_dtype`, `d3_nonzero_with_count_reserved_z`, `container_nonzero_cardinality`, `count_element_co*`, `s_valid_nonzero_with_count_dtype` — all recovered as literal strings from `libwalrus.so` `.rodata`; the struct tags `NEURON_ISA_TPB_S4D2_BN_STRUCT` / `…_D4_MR_STRUCT` / `S4D4_MR` / `…_S3D3_NONZERO_WITH_COUNT_STRUCT` likewise. The sem/sync `+0x04` regions and `assignAccess` interiors are N-strand common code.

**INFERRED** (zero-init only): reserved-zero pad bytes (`d2_bn_reserved_z`, `d4_mr_*` guards, NonzeroWithCount `+0x24`); the FindIndex8 `−1` index preset (simulator init, not a bundle field); LoadMaskSelect `+0x08..+0x1F` load-source descriptor.

**OPEN / cross-ref:** the exact bit-width / packing of `container_nonzero_cardinality` + `count_element_co*` within the output `TENSOR3D` descriptor (they ride the dst AP dims — [2.4 TENSOR4D / MEM_PATTERN4D](tensor4d-mempattern4d.md)); the full StreamTranspose decomposition (which other bundles co-issue with the `0x69`) — see [2.17 DVE datamove encoding](dve-datamove-encoding.md) *(planned)*; CoreV4 Max-family bodies (reuse CoreV2; dtype-LUT delta only).

## Cross-References

- [1.11 — The DVE Engine](../arch/dve-engine.md) — the table-driven engine model (`EngineType::DVE` = 5, `→ "Vector"`); this page is its bit-level companion for the search/match opcodes.
- [2.1 — The 64-Byte Instruction Bundle & Header Skeleton](instruction-bundle.md) — the Family-C bundle shape and the `+0x20..+0x2F` control-band convention shared by all five Max-family encoders.
- [2.11 — BatchNorm-Family Encoding](batchnorm-encoding.md) — the `NEURON_ISA_TPB_S4D2_BN_STRUCT` Max8 *shares*; the `d2_bn_*` field names originate there.
- [2.10 — PE Matmul Encoding](pe-matmul-encoding.md) — the sibling encoding page; same `setupHeader` / `assignAccess` / `runISACheck` / `fwrite 0x40` lifecycle.
- [2.12 — Pool / TensorReduce / Reciprocal / Iota Encoding](pool-reduce-encoding.md) — the co-issued-micro-op idiom (`Gather → {pool_buffer_load, reg_load}`) that MatchValueLoad / LoadMaskSelect follow.
- [2.17 — DVE Datamove Encoding](dve-datamove-encoding.md) *(planned)* — the StreamTranspose sibling that hosts the `0x69` LoadMaskSelect sub-bundle.
- Part 7 (`../bir/`) — codegen of the same ops (the max-index / match-replace producers that write the `imm_value@inst+0xF0` sentinel this encoder reads into bundle `+0x28`).
- Part 6 (`../nki/`) — the NKI top-k / index / select primitives (`nc_match_replace8` / `nc_match_replace_indices8`) these bundles back.
