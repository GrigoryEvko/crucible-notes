# PTX Instruction Table

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

Complete catalog of PTX instructions recognized by ptxas v13.0.88 (CUDA 12.8 / PTX ISA 8.7). All entries are verified against the binary: instruction names come from the Flex lexer's 552 token rules, type signatures from the instruction table builder's 1,141 descriptor registrations (`sub_46E000` calling `sub_46BED0`), and formatter names from the 580 PTX text generation functions dispatched by `sub_5D4190`. Internal-only instructions (prefixed with `_`) are included where they appear in the binary but are marked accordingly.

| | |
|---|---|
| **Instruction table builder** | `sub_46E000` (93 KB, 1,141 calls to `sub_46BED0`) |
| **Instruction lookup** | `sub_46C690` (entry) / `sub_46C6E0` (6.4 KB matcher) |
| **PTX text formatter dispatch** | `sub_5D4190` (12.9 KB, 81 string + 473-entry hash) |
| **Formatter functions** | `0x4DA340`--`0x5A8E40` (580 functions) |
| **Semantic validators** | `0x460000`--`0x4D5000` (~20 validator functions) |
| **Operand type encoding** | Single-char codes: `F`=float, `H`=half, `I`=int, `B`=bits, `N`=imm, `P`=pred, `E`=bf16, `Q`=fp8, `R`=fp4 |

## Organization

Instructions are grouped by functional category following NVIDIA's PTX ISA documentation structure. Each table entry lists:

- **Mnemonic**: the PTX instruction name as recognized by the lexer
- **Type suffixes**: legal type qualifiers (from instruction table builder encoding strings)
- **Operands**: operand pattern (`d`=dest, `a`/`b`/`c`=source, `p`=predicate, `[a]`=memory)
- **SM req**: minimum SM architecture (from `sub_489390` version gates in validators)
- **PTX req**: minimum PTX ISA version (from `sub_489050` version gates)
- **Description**: brief functional description

Type abbreviations in the suffix column: `s`=signed int, `u`=unsigned int, `f`=float, `b`=bits, `pred`=predicate. Widths: 8/16/32/64/128. Packed: `f16x2`, `bf16x2`.

---

## Integer Arithmetic

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `add` | `.s16 .s32 .s64 .u16 .u32 .u64` | `d, a, b` | all | 1.0 | Integer addition |
| `sub` | `.s16 .s32 .s64 .u16 .u32 .u64` | `d, a, b` | all | 1.0 | Integer subtraction |
| `mul.lo` | `.s16 .s32 .s64 .u16 .u32 .u64` | `d, a, b` | all | 1.0 | Multiply, low half of result |
| `mul.hi` | `.s16 .s32 .s64 .u16 .u32 .u64` | `d, a, b` | all | 1.0 | Multiply, high half of result |
| `mul.wide` | `.s16 .s32 .u16 .u32` | `d, a, b` | all | 1.0 | Widening multiply (16->32 or 32->64) |
| `mul24.lo` | `.s32 .u32` | `d, a, b` | all | 1.0 | 24-bit multiply, low half (deprecated sm_20+) |
| `mul24.hi` | `.s32 .u32` | `d, a, b` | all | 1.0 | 24-bit multiply, high half (deprecated sm_20+) |
| `mad.lo` | `.s16 .s32 .s64 .u16 .u32 .u64` | `d, a, b, c` | all | 1.0 | Multiply-add, low half |
| `mad.hi` | `.s16 .s32 .s64 .u16 .u32 .u64` | `d, a, b, c` | all | 1.0 | Multiply-add, high half |
| `mad.wide` | `.s16 .s32 .u16 .u32` | `d, a, b, c` | all | 1.0 | Widening multiply-add |
| `mad24.lo` | `.s32 .u32` | `d, a, b, c` | all | 1.0 | 24-bit multiply-add, low (deprecated) |
| `mad24.hi` | `.s32 .u32` | `d, a, b, c` | all | 1.0 | 24-bit multiply-add, high (deprecated) |
| `mad.cc` | `.s32 .u32 .s64 .u64` | `d, a, b, c` | all | 1.0 | Multiply-add with carry-out |
| `madc.lo` | `.s32 .u32 .s64 .u64` | `d, a, b, c` | all | 1.0 | Multiply-add with carry-in, low |
| `madc.hi` | `.s32 .u32 .s64 .u64` | `d, a, b, c` | all | 1.0 | Multiply-add with carry-in, high |
| `mad.fused.hi` | `.s32 .u32` | `d, a, b, c` | all | 1.0 | Fused multiply-add, high half |
| `madc.fused.hi` | `.s32 .u32` | `d, a, b, c` | all | 1.0 | Fused multiply-add with carry, high |
| `div` | `.s16 .s32 .s64 .u16 .u32 .u64` | `d, a, b` | all | 1.0 | Integer division |
| `rem` | `.s16 .s32 .s64 .u16 .u32 .u64` | `d, a, b` | all | 1.0 | Integer remainder |
| `abs` | `.s16 .s32 .s64` | `d, a` | all | 1.0 | Absolute value |
| `neg` | `.s16 .s32 .s64` | `d, a` | all | 1.0 | Negate |
| `min` | `.s16 .s32 .s64 .u16 .u32 .u64` | `d, a, b` | all | 1.0 | Minimum |
| `max` | `.s16 .s32 .s64 .u16 .u32 .u64` | `d, a, b` | all | 1.0 | Maximum |
| `popc` | `.b32 .b64` | `d, a` | 20+ | 2.0 | Population count (count set bits) |
| `clz` | `.b32 .b64` | `d, a` | 20+ | 2.0 | Count leading zeros |
| `bfind` | `.s32 .s64 .u32 .u64` | `d, a` | 20+ | 2.0 | Find most significant set bit |
| `brev` | `.b32 .b64` | `d, a` | 20+ | 2.0 | Bit reverse |
| `bfe` | `.s32 .s64 .u32 .u64` | `d, a, b, c` | 20+ | 2.0 | Bit field extract |
| `bfi` | `.b32 .b64` | `d, f, a, b, c` | 20+ | 2.0 | Bit field insert |
| `dp4a` | `.s32.s32 .s32.u32 .u32.s32 .u32.u32` | `d, a, b, c` | 61+ | 5.0 | 4-element dot product accumulate |
| `dp2a.lo` | `.s32.s32 .s32.u32 .u32.s32 .u32.u32` | `d, a, b, c` | 61+ | 5.0 | 2-element dot product accumulate, low |
| `dp2a.hi` | `.s32.s32 .s32.u32 .u32.s32 .u32.u32` | `d, a, b, c` | 61+ | 5.0 | 2-element dot product accumulate, high |
| `sad` | `.s16 .s32 .u16 .u32` | `d, a, b, c` | all | 1.0 | Sum of absolute differences |

## Floating-Point Arithmetic

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `add` | `.f32 .f64` | `d, a, b` | all | 1.0 | FP addition (`.rn .rz .rm .rp` rounding) |
| `sub` | `.f32 .f64` | `d, a, b` | all | 1.0 | FP subtraction |
| `mul` | `.f32 .f64` | `d, a, b` | all | 1.0 | FP multiplication |
| `fma` | `.f32 .f64` | `d, a, b, c` | 20+ | 2.0 | Fused multiply-add |
| `mad` | `.f32 .f64` | `d, a, b, c` | all | 1.0 | Multiply-add (non-fused on sm_20+) |
| `mad.rnd.f32` | `.f32` | `d, a, b, c` | all | 1.0 | Multiply-add with explicit rounding |
| `div` | `.f32 .f64` | `d, a, b` | all | 1.0 | FP division (`.approx .full .rn .rz .rm .rp`) |
| `div.full` | `.f32` | `d, a, b` | all | 1.0 | Full-range division (specialized formatter) |
| `div.rnd.f32` | `.f32` | `d, a, b` | all | 1.0 | Division with explicit rounding |
| `div.rn.f64` | `.f64` | `d, a, b` | all | 1.0 | Double-precision division, round-nearest |
| `abs` | `.f32 .f64` | `d, a` | all | 1.0 | FP absolute value |
| `neg` | `.f32 .f64` | `d, a` | all | 1.0 | FP negate |
| `min` | `.f32 .f64` | `d, a, b` | all | 1.0 | FP minimum |
| `max` | `.f32 .f64` | `d, a, b` | all | 1.0 | FP maximum |
| `rcp` | `.f32 .f64` | `d, a` | all | 1.0 | Reciprocal (`.approx .rn .rz .rm .rp`) |
| `rcp.approx.f64` | `.f64` | `d, a` | all | 1.0 | Approximate double reciprocal |
| `rcp.rnd.f32` | `.f32` | `d, a` | all | 1.0 | Reciprocal with explicit rounding |
| `rcp.rn.f64` | `.f64` | `d, a` | all | 1.0 | Double reciprocal, round-nearest |
| `sqrt` | `.f32 .f64` | `d, a` | all | 1.0 | Square root (`.approx .rn .rz .rm .rp`) |
| `rsqrt` | `.f32 .f64` | `d, a` | all | 1.0 | Reciprocal square root (`.approx`) |
| `sin` | `.f32` | `d, a` | all | 1.0 | Sine (approximate) |
| `cos` | `.f32` | `d, a` | all | 1.0 | Cosine (approximate) |
| `lg2` | `.f32` | `d, a` | all | 1.0 | Log base 2 (approximate) |
| `ex2` | `.f32` | `d, a` | all | 1.0 | Exp base 2 (approximate) |
| `tanh` | `.f32` | `d, a` | 75+ | 6.5 | Hyperbolic tangent (approximate) |
| `testp` | `.f32 .f64` | `p, a` | 20+ | 2.0 | Test FP property (`.finite .infinite .number .notanumber .normal .subnormal`) |
| `copysign` | `.f32 .f64` | `d, a, b` | 20+ | 2.0 | Copy sign from `b` to `a` |
| `fma.f32` | `.f32` | `d, a, b, c` | 20+ | 2.0 | FP32 fused multiply-add (rounding modes) |
| `fma.f64` | `.f64` | `d, a, b, c` | 20+ | 2.0 | FP64 fused multiply-add |

## Half-Precision and BFloat16 Arithmetic

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `add` | `.f16 .f16x2 .bf16 .bf16x2` | `d, a, b` | 53+ | 4.2 | Half-precision addition |
| `sub` | `.f16 .f16x2 .bf16 .bf16x2` | `d, a, b` | 53+ | 4.2 | Half-precision subtraction |
| `mul` | `.f16 .f16x2 .bf16 .bf16x2` | `d, a, b` | 53+ | 4.2 | Half-precision multiplication |
| `fma` | `.f16 .f16x2 .bf16 .bf16x2` | `d, a, b, c` | 53+ | 4.2 | Half-precision fused multiply-add |
| `neg` | `.f16 .f16x2 .bf16 .bf16x2` | `d, a` | 53+ | 4.2 | Half-precision negate |
| `abs` | `.f16 .f16x2 .bf16 .bf16x2` | `d, a` | 53+ | 4.2 | Half-precision absolute value |
| `min` | `.f16 .f16x2 .bf16 .bf16x2` | `d, a, b` | 80+ | 7.0 | Half-precision minimum |
| `max` | `.f16 .f16x2 .bf16 .bf16x2` | `d, a, b` | 80+ | 7.0 | Half-precision maximum |
| `min.ftz.NaN` | `.f16 .f16x2 .bf16 .bf16x2` | `d, a, b` | 80+ | 7.0 | Min with NaN propagation |
| `max.ftz.NaN` | `.f16 .f16x2 .bf16 .bf16x2` | `d, a, b` | 80+ | 7.0 | Max with NaN propagation |
| `ex2.approx` | `.f16 .f16x2` | `d, a` | 75+ | 6.5 | Half-precision exp2 |
| `tanh.approx` | `.f16 .f16x2` | `d, a` | 75+ | 6.5 | Half-precision tanh |
| `fma.rn.relu` | `.f16 .bf16` | `d, a, b, c` | 80+ | 7.0 | Fused multiply-add with ReLU |

## Comparison and Selection

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `setp` | `.s16 .s32 .s64 .u16 .u32 .u64 .f32 .f64 .f16 .bf16 .b16 .b32 .b64` | `p[\|q], a, b` | all | 1.0 | Set predicate on comparison |
| `selp` | `.s16 .s32 .s64 .u16 .u32 .u64 .f32 .f64 .b16 .b32 .b64` | `d, a, b, p` | all | 1.0 | Select on predicate |
| `slct` | `.s32 .u32 .f32 .s64 .u64 .f64` | `d, a, b, c` | all | 1.0 | Select on comparison |
| `set` | `.s32 .u32 .f32 .s64 .u64 .f64` | `d, a, b` | all | 1.0 | Compare and set |

Comparison operators for `setp`/`set`: `.eq .ne .lt .le .gt .ge .lo .ls .hi .hs .equ .neu .ltu .leu .gtu .geu .num .nan`

## Logic and Shift

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `and` | `.b16 .b32 .b64 .pred` | `d, a, b` | all | 1.0 | Bitwise AND |
| `or` | `.b16 .b32 .b64 .pred` | `d, a, b` | all | 1.0 | Bitwise OR |
| `xor` | `.b16 .b32 .b64 .pred` | `d, a, b` | all | 1.0 | Bitwise XOR |
| `not` | `.b16 .b32 .b64 .pred` | `d, a` | all | 1.0 | Bitwise NOT |
| `cnot` | `.b16 .b32 .b64` | `d, a` | all | 1.0 | C-style logical NOT |
| `lop3` | `.b32` | `d, a, b, c, immLut` | 50+ | 4.0 | 3-input logic operation (LUT-encoded) |
| `shl` | `.b16 .b32 .b64` | `d, a, b` | all | 1.0 | Shift left |
| `shr` | `.s16 .s32 .s64 .u16 .u32 .u64` | `d, a, b` | all | 1.0 | Shift right (arithmetic for `.s`, logical for `.u`) |
| `shf.l` | `.b32` | `d, a, b, c` | 32+ | 3.2 | Funnel shift left |
| `shf.r` | `.b32` | `d, a, b, c` | 32+ | 3.2 | Funnel shift right (`.clamp .wrap`) |
| `prmt` | `.b32` | `d, a, b, c` | 20+ | 2.0 | Byte permute (`.f4e .b4e .rc8 .ecl .ecr .rc16`) |

## Data Movement

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `mov` | `.b16 .b32 .b64 .b128 .u16 .u32 .u64 .s16 .s32 .s64 .f16 .f32 .f64 .pred` | `d, a` | all | 1.0 | Move register-to-register |
| `shfl` | `.b32` | `d\|p, a, b, c` | 30+ | 3.0 | Warp shuffle (`.up .down .bfly .idx`) |
| `shfl.sync` | `.b32` | `d\|p, a, b, c, membermask` | 70+ | 6.0 | Warp shuffle with sync |
| `vote` | `.pred` | `d, {p}, a` | 20+ | 2.0 | Warp vote (`.all .any .uni .ballot`) |
| `vote.sync` | `.pred .b32` | `d, {p}, a, membermask` | 70+ | 6.0 | Warp vote with sync |
| `match` | `.b32 .b64` | `d, a` | 70+ | 6.0 | Warp match (`.any .all`) |
| `match.sync` | `.b32 .b64` | `d, a, membermask` | 70+ | 6.0 | Warp match with sync |
| `redux` | `.s32 .u32` | `d, a` | 80+ | 7.0 | Warp reduction (`.add .min .max .and .or .xor`) |
| `redux.sync` | `.s32 .u32` | `d, a, membermask` | 80+ | 7.0 | Warp reduction with sync |
| `activemask` | `.b32` | `d` | 70+ | 6.2 | Get active thread mask |
| `elect` | `.pred` | `p` | 90+ | 8.0 | Elect one leader thread |
| `elect.one` | — | `d, {p}` | 90+ | 8.0 | Elect one thread, return success |

## Load, Store, and Memory

### Global, Local, Shared, Const

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `ld` | `.b8 .b16 .b32 .b64 .b128 .u8 .u16 .u32 .u64 .s8 .s16 .s32 .s64 .f16 .f32 .f64` | `d, [a]` | all | 1.0 | Load from memory (`.global .shared .local .const .param`) |
| `ld.nc` | `.b32 .b64 .b128 .f32 .f64` | `d, [a]` | 35+ | 3.2 | Non-coherent load (read-only cache) |
| `ld.param` | `.b8 .b16 .b32 .b64 .b128` | `d, [a]` | all | 1.0 | Load from kernel parameter space |
| `st` | `.b8 .b16 .b32 .b64 .b128 .u8 .u16 .u32 .u64 .s8 .s16 .s32 .s64 .f16 .f32 .f64` | `[a], b` | all | 1.0 | Store to memory |
| `ldu` | `.b32 .b64 .b128 .f32 .f64` | `d, [a]` | 20+ | 2.0 | Load via uniform cache (deprecated) |
| `prefetch` | `.L1 .L2` | `[a]` | 20+ | 2.0 | Prefetch to cache level |
| `prefetchu` | `.L1` | `[a]` | 20+ | 2.0 | Prefetch uniform |
| `isspacep` | `.global .shared .local .const` | `p, a` | 20+ | 2.0 | Test address space |
| `cvta` | — | `d, a` | 20+ | 2.0 | Convert address space (generic <-> specific) |
| `cvta.to` | `.global .shared .local .const` | `d, a` | 20+ | 2.0 | Convert to specific state space |

Cache qualifiers for `ld`/`st`: `.ca .cg .cs .cv .lu .wb .wt`  
Eviction policy (PTX 7.4+): `.L2::evict_first .L2::evict_last .L2::evict_normal .L2::cache_hint`

### Async Copy

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `cp.async` | `.ca .cg` | `[dst], [src], size` | 80+ | 7.0 | Async copy (4/8/16 bytes, global->shared) |
| `cp.async.commit_group` | — | — | 80+ | 7.0 | Commit outstanding async copies |
| `cp.async.wait_group` | — | `N` | 80+ | 7.0 | Wait for async copy group completion |
| `cp.async.wait_all` | — | — | 80+ | 7.0 | Wait for all async copies |
| `cp.async.mbarrier.arrive` | — | `[mbar]` | 80+ | 7.0 | Arrive at mbarrier on async copy completion |
| `cp.async.bulk` | — | `[dst], [src], size` | 90+ | 8.0 | Bulk async copy (TMA) |
| `cp.async.bulk.tensor` | — | `[dst], [src], dims...` | 90+ | 8.0 | Tensor async copy (TMA, 1-5D tiles) |
| `cp.async.bulk.prefetch` | — | `[src], size` | 90+ | 8.0 | Prefetch via TMA |
| `cp.async.bulk.prefetch.tensor` | — | `[src], dims...` | 90+ | 8.0 | Tensor prefetch via TMA |
| `cp.async.bulk.commit_group` | — | — | 90+ | 8.0 | Commit bulk async group |
| `cp.async.bulk.wait_group` | — | `N` | 90+ | 8.0 | Wait for bulk group completion |
| `cp.reduce.async.bulk` | `.add .min .max .and .or .xor .inc .dec` | `[dst], [src], size` | 90+ | 8.0 | Bulk async copy with reduction |
| `cp.reduce.async.bulk.tensor` | `.add .min .max .and .or .xor .inc .dec` | `[dst], [src], dims...` | 90+ | 8.0 | Tensor async copy with reduction |
| `st.async` | `.b32 .b64 .b128` | `[a], b` | 90+ | 8.1 | Async store |
| `st.bulk` | — | `[a], b, size` | 90+ | 8.0 | Bulk store |
| `red.async` | `.add .min .max .and .or .xor .inc .dec` | `[a], b` | 90+ | 8.1 | Async reduction |

### Multimem

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `multimem.ld_reduce` | `.f16 .bf16 .f32 .u32 .s32 .u64` | `d, [a]` | 90+ | 8.1 | Multicast memory load with reduction |
| `multimem.st` | `.f16 .bf16 .f32 .u32 .s32 .u64` | `[a], b` | 90+ | 8.1 | Multicast memory store |
| `multimem.red` | `.f16 .bf16 .f32 .u32 .s32 .u64` | `[a], b` | 90+ | 8.1 | Multicast memory reduction |

### Cache Control

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `discard` | `.global .L2` | `[a], size` | 80+ | 7.4 | Discard data (hint: no writeback) |
| `applypriority` | `.global .L2` | `[a], size, prio` | 80+ | 7.4 | Set cache eviction priority |
| `createpolicy.cvt` | `.L2` | `d, imm` | 80+ | 7.4 | Create cache policy from immediate |
| `createpolicy.fractional` | `.L2` | `d, fraction` | 80+ | 7.4 | Create fractional cache policy |
| `createpolicy.range` | `.L2` | `d, lo, hi, hit, miss` | 80+ | 7.4 | Create cache policy for address range |

## Conversion

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `cvt` | all int/float combinations | `d, a` | all | 1.0 | Type conversion (rounding: `.rn .rz .rm .rp .rna`) |
| `cvt.pack` | `.b16.b32 .b32.b32` | `d, a, b` | 80+ | 7.1 | Pack two values into one register |

**cvt type combinations** (from instruction table encoding strings):

The instruction table builder registers extensive type-pair combinations for `cvt`. Representative signatures:

| Source | Destination | Notes |
|---|---|---|
| `F[16\|32\|64]` | `F[16\|32\|64]` | Float-to-float, rounding modes apply |
| `F[16\|32\|64]` | `I[8\|16\|32\|64]` | Float-to-integer, rounding + saturation |
| `I[8\|16\|32\|64]` | `F[16\|32\|64]` | Integer-to-float, rounding modes |
| `I[8\|16\|32\|64]` | `I[8\|16\|32\|64]` | Integer-to-integer, sign extend / truncate |
| `E16` | `F[16\|64]` / `I[8\|16\|32\|64]` | bf16 source conversions (sm_80+) |
| `F[16\|64]` / `I[8\|16\|32\|64]` | `E16` | bf16 destination conversions (sm_80+) |
| `H32` (tf32) | various | TensorFloat-32 conversions (sm_80+) |
| `Q16` (fp8 e5m2) | `F32` / `H32` / `E32` | FP8 conversions (sm_89+) |
| `R8` (fp8 e4m3) | `F32` / `H32` / `E32` | FP8 e4m3 conversions (sm_89+) |
| `R16` (fp4) | `F32` | FP4 conversions (sm_100+, PTX 8.6) |
| `Q32` (fp8 e5m2) | `F32` | Extended FP8 conversions (sm_100+) |

Modifier `.ftz` (flush-to-zero), `.sat` (saturation), `.relu` (ReLU clamp to 0) are recognized. The `.rna` rounding mode (round-to-nearest-away, PTX 8.3+) is registered for `cvt.tf32.f32`.

### `szext` — Sign/Zero Extend

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `szext` | `.b32` | `d, a, pos` | 90+ | 8.0 | Sign- or zero-extend at bit position |

## Texture and Surface

### Texture

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `tex` | `.1d .2d .3d .a1d .a2d .cube .acube` | `d, [tex, sampler, coord]` | all | 1.0 | Texture fetch with sampler |
| `tex.base` | `.1d .2d .3d` | `d, [tex, coord]` | 60+ | 5.0 | Texture fetch base level |
| `tex.level` | `.1d .2d .3d .a1d .a2d .cube .acube` | `d, [tex, sampler, coord, lod]` | all | 1.0 | Texture fetch at explicit LOD |
| `tex.grad` | `.1d .2d .3d .a1d .a2d .cube .acube` | `d, [tex, sampler, coord, dPdx, dPdy]` | all | 1.0 | Texture fetch with explicit gradients |
| `tld4` | `.2d .a2d` | `d, [tex, sampler, coord]` | 20+ | 2.0 | Texture gather (4 texels) |
| `txq` | `.width .height .depth .channel_data_type .channel_order .normalized_coords .filter_mode .addr_mode_0 .addr_mode_1 .addr_mode_2 .samp_pos .num_mip_levels .num_samples` | `d, [tex]` | 20+ | 2.0 | Texture query |

Return types for texture ops: `.v4.s32 .v4.u32 .v4.f32` (4-component return).

### Surface

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `suld.b` | `.1d .2d .3d .a1d .a2d` | `d, [surf, coord]` | 20+ | 2.0 | Surface load (bindless) |
| `sust.b` | `.1d .2d .3d .a1d .a2d` | `[surf, coord], a` | 20+ | 2.0 | Surface store (bindless) |
| `sust.p` | `.1d .2d .3d .a1d .a2d` | `[surf, coord], a` | 20+ | 2.0 | Surface store (packed format) |
| `sured.b` | `.1d .2d .3d` | `d, [surf, coord], a` | 20+ | 2.0 | Surface reduction (`.add .min .max .and .or`) |
| `suq` | `.width .height .depth .channel_data_type .channel_order .array_size` | `d, [surf]` | 20+ | 2.0 | Surface query |

Surface clamp modes: `.trap .clamp .zero`

### Tensormap

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `tensormap.replace` | `.tile .im2col` | `d, [tmap], field, value` | 90+ | 8.0 | Replace tensormap field at runtime |
| `tensormap.cp_fenceproxy` | — | `[tmap]` | 90+ | 8.0 | Tensormap copy fence proxy |

## Control Flow

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `bra` | — | `target` | all | 1.0 | Branch (unconditional or predicated) |
| `bra.uni` | — | `target` | all | 1.0 | Uniform branch (all threads take same direction) |
| `brx.idx` | — | `a, [targets]` | 70+ | 6.0 | Indexed branch (jump table) |
| `call` | — | `(ret), func, (params)` | 20+ | 2.0 | Function call (with ABI) |
| `ret` | — | — | 20+ | 2.0 | Return from function |
| `exit` | — | — | all | 1.0 | Exit kernel / terminate thread |
| `trap` | — | — | all | 1.0 | Trigger error |
| `brkpt` | — | — | 11+ | 1.0 | Breakpoint (debugger halt) |
| `pmevent` | — | `imm` | 20+ | 2.0 | Performance monitor event |
| `pmevent.mask` | — | `imm` | 20+ | 3.0 | Performance monitor event with mask |
| `nanosleep` | — | `t` | 70+ | 6.3 | Sleep for `t` nanoseconds |

## Synchronization and Barriers

### Legacy Barrier

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `bar.sync` | — | `a{, b}` | all | 1.0 | Barrier synchronize (CTA-level) |
| `bar.arrive` | — | `a, b` | all | 1.0 | Barrier arrive (non-blocking) |
| `bar.red` | `.and .or .popc` | `d, a, {b}, p` | all | 1.0 | Barrier with reduction |
| `bar.warp` | `.sync` | `membermask` | 70+ | 6.0 | Warp-level barrier |

### Named Barrier (PTX 6.0+)

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `barrier` | — | `a{, b}` | 70+ | 6.0 | Named barrier synchronize |
| `barrier.arrive` | — | `a, b` | 70+ | 6.0 | Named barrier arrive |
| `barrier.red` | `.and .or .popc` | `d, a, {b}, p` | 70+ | 6.0 | Named barrier with reduction |

### CTA-Cluster Barrier (PTX 7.8+)

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `bar.cta` | — | — | 90+ | 7.8 | CTA-level barrier sync |
| `bar.cta.arrive` | — | — | 90+ | 7.8 | CTA-level barrier arrive |
| `bar.cta.red` | `.and .or .popc` | `d, p` | 90+ | 7.8 | CTA barrier with reduction |
| `barrier.cta` | — | — | 90+ | 7.8 | CTA named barrier sync |
| `barrier.cta.arrive` | — | — | 90+ | 7.8 | CTA named barrier arrive |
| `barrier.cta.red` | `.and .or .popc` | `d, p` | 90+ | 7.8 | CTA named barrier with reduction |
| `barrier.cluster.arrive` | — | — | 90+ | 7.8 | Cluster-level barrier arrive |
| `barrier.cluster.wait` | — | — | 90+ | 7.8 | Cluster-level barrier wait |

### Asynchronous Barriers (mbarrier)

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `mbarrier.init` | `.shared.b64` | `[mbar], count` | 80+ | 7.0 | Initialize mbarrier with expected count |
| `mbarrier.inval` | `.shared.b64` | `[mbar]` | 80+ | 7.0 | Invalidate mbarrier |
| `mbarrier.arrive` | `.shared.b64` | `state, [mbar]` | 80+ | 7.0 | Arrive at mbarrier |
| `mbarrier.arrive_drop` | `.shared.b64` | `state, [mbar]` | 80+ | 7.0 | Arrive and drop expected count |
| `mbarrier.test_wait` | `.shared.b64` | `p, [mbar], state` | 80+ | 7.0 | Test if mbarrier phase complete |
| `mbarrier.test_wait.parity` | `.shared.b64` | `p, [mbar], parity` | 80+ | 7.1 | Test mbarrier parity |
| `mbarrier.try_wait` | `.shared.b64` | `p, [mbar], state` | 80+ | 7.8 | Try-wait on mbarrier (with timeout) |
| `mbarrier.try_wait.parity` | `.shared.b64` | `p, [mbar], parity` | 80+ | 7.8 | Try-wait on mbarrier parity |
| `mbarrier.pending_count` | `.b64` | `d, state` | 80+ | 7.0 | Get pending arrival count |
| `mbarrier.complete_tx` | `.shared.b64` | `[mbar], count` | 90+ | 8.0 | Complete transaction at mbarrier |
| `mbarrier.expect_tx` | `.shared.b64` | `[mbar], count` | 90+ | 8.0 | Set expected transaction count |
| `mbarrier.tx` | — | `[mbar], count` | 90+ | 8.0 | Transaction mbarrier arrive |
| `mbarrier.arrive.expect_tx` | `.shared.b64` | `state, [mbar], count` | 90+ | 8.0 | Arrive with expected tx count |
| `mbarrier.arrive_drop.expect_tx` | `.shared.b64` | `state, [mbar], count` | 90+ | 8.0 | Arrive-drop with expected tx count |

### Memory Fence

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `membar` | `.cta .gl .sys` | — | all | 1.0 | Memory barrier (scope) |
| `membar.proxy` | `.alias` | — | 75+ | 6.4 | Proxy memory barrier (alias scope) |
| `fence.proxy` | `.alias .async .async.global .async.shared::cta` | — | 70+ | 6.0 | Fence proxy (alias/async) |
| `fence.proxy.tensormap` | — | `[addr]` | 90+ | 8.0 | Fence tensormap proxy |

### Grid Dependency Control

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `griddepcontrol` | `.launch_dependents .wait` | — | 90+ | 7.8 | Grid dependency control |

## Atomic and Reduction

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `atom` | `.s32 .u32 .u64 .f32 .f64 .b32 .b64 .f16x2 .bf16x2` | `d, [a], b` | all | 1.1 | Atomic RMW (`.add .min .max .and .or .xor .exch .cas .inc .dec`) |
| `atom.global` | (same as atom) | `d, [a], b` | all | 1.1 | Atomic on global memory |
| `atom.shared` | (same as atom) | `d, [a], b` | all | 1.1 | Atomic on shared memory |
| `red` | `.s32 .u32 .u64 .f32 .f64 .b32 .b64 .f16x2 .bf16x2` | `[a], b` | all | 1.1 | Reduction (no return value) |
| `red.global` | (same as red) | `[a], b` | all | 1.1 | Reduction on global memory |

Atom/red scope modifiers (PTX 6.0+): `.cta .gpu .sys .cluster`

## Matrix (MMA / Tensor Core)

### WMMA (Warp Matrix Multiply-Accumulate, PTX 6.0+)

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `wmma.load.a` | `.sync .aligned` | `d, [ptr], stride` | 70+ | 6.0 | Load matrix A fragment |
| `wmma.load.b` | `.sync .aligned` | `d, [ptr], stride` | 70+ | 6.0 | Load matrix B fragment |
| `wmma.load.c` | `.sync .aligned` | `d, [ptr], stride` | 70+ | 6.0 | Load accumulator C fragment |
| `wmma.store.d` | `.sync .aligned` | `[ptr], d, stride` | 70+ | 6.0 | Store result D fragment |
| `wmma.mma` | `.sync .aligned` | `d, a, b, c` | 70+ | 6.0 | Matrix multiply-accumulate |

WMMA shapes (from validator `sub_4BFED0`): `.m16n16k16 .m32n8k16 .m8n32k16 .m16n16k8` etc.  
WMMA type combinations (from string table): `F16F16F16F16`, `F32F16F16F32`, `F32F32` (TF32), `I32I8I8I32`, `I32I4I4I32`, `I32B1B1I32`, `F64F64F64F64`

### MMA (PTX 6.5+)

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `mma` | `.sync .aligned` | `d, a, b, c` | 75+ | 6.5 | Matrix multiply-accumulate (Turing+) |

MMA type combinations (verified from instruction table builder strings):

| D type | A type | B type | C type | SM | Notes |
|---|---|---|---|---|---|
| `F16` | `F16` | `F16` | `F16` | 75+ | Native FP16 |
| `F32` | `F16` | `F16` | `F32` | 75+ | Mixed-precision |
| `F32` | `F32` | `F32` | — | 80+ | TF32 Tensor Core |
| `F32` | `E16` | `E16` | `F32` | 80+ | BFloat16 |
| `F32` | `T32` | `T32` | `F32` | 80+ | TF32 path (string: `F32T32T32F32`) |
| `I32` | `I8` | `I8` | `I32` | 75+ | INT8 |
| `I32` | `I4` | `I4` | `I32` | 75+ | INT4 |
| `I32` | `B1` | `B1` | `I32` | 75+ | Binary (1-bit) |
| `F64` | `F64` | `F64` | `F64` | 80+ | Double-precision |
| `F16` | `Q8` | `Q8` | `F16` | 89+ | FP8 (e5m2) |
| `F32` | `Q8` | `Q8` | `F32` | 89+ | FP8 mixed |
| `F16` | `R4` | `Q8` | `F16` | 100+ | FP4 x FP8 |
| `F32` | `R4` | `Q8` | `F32` | 100+ | FP4 x FP8 mixed |
| `F32` | `R4` | `R4` | `F32` | 100+ | FP4 x FP4 |
| `F32` | `R4` | `R4` | `F32.Q8` | 100+ | FP4 with scale (string: `F32R4R4F32Q8`) |
| `F32` | `Q8` | `Q8` | `F32.Q8` | 100+ | FP8 with block scale |

MMA shapes: `.m16n8k16 .m16n8k32 .m16n8k64 .m16n8k128 .m16n8k256`  
Sparse MMA modifiers: `.sp` with metadata selector and sparsity pattern

### WGMMA (Warp-Group MMA, PTX 7.8+)

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `wgmma.mma_async` | `.aligned` | `d, a_desc, b_desc` | 90+ | 7.8 | Warp-group async matrix multiply |
| `wgmma.fence` | `.aligned` | — | 90+ | 7.8 | WGMMA fence (ordering) |
| `wgmma.commit_group` | `.aligned` | — | 90+ | 7.8 | Commit WGMMA group |
| `wgmma.wait_group` | `.aligned` | `N` | 90+ | 7.8 | Wait for WGMMA group completion |

WGMMA operand encoding strings (from instruction table, selection):
`hUUhP`, `fUUfP`, `hUhhP`, `fUhfP`, `hhUhP`, `fhUfP` (H=half dest, F=float dest, U=desc operand, P=pred)  
With accumulator: `hUUhdC`, `fUUfdC`, `hUhhdC`, `fUhfdC`  
With scale: `hUUhdCP`, `fUUfdCP` (P=pred control for scale)

### TCGen05 (5th Generation Tensor Core, sm_100+)

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `tcgen05.mma` | — | `d, a_desc, b_desc` | 100+ | 8.6 | 5th-gen tensor core MMA |
| `tcgen05.mma.ws` | — | `d, a_desc, b_desc` | 100+ | 8.6 | 5th-gen MMA with warpgroup scale |
| `tcgen05.ld` | — | `d, [desc]` | 100+ | 8.6 | TC load from descriptor |
| `tcgen05.ld.red` | — | `d, [desc], src` | 100+ | 8.6 | TC load with reduction |
| `tcgen05.st` | — | `[desc], src` | 100+ | 8.6 | TC store to descriptor |
| `tcgen05.cp` | — | `[desc], [src]` | 100+ | 8.6 | TC copy |
| `tcgen05.commit` | — | `[mbar]` | 100+ | 8.6 | TC commit |
| `tcgen05.shift` | — | `[desc]` | 100+ | 8.6 | TC shift accumulator |
| `tcgen05.alloc` | — | `d, nCols` | 100+ | 8.6 | Allocate TC columns |
| `tcgen05.dealloc` | — | `nCols` | 100+ | 8.6 | Deallocate TC columns |
| `tcgen05.relinquish_alloc_permit` | — | — | 100+ | 8.6 | Relinquish TC allocation permit |
| `tcgen05.fence` | — | — | 100+ | 8.6 | TC fence |
| `tcgen05.wait` | — | — | 100+ | 8.6 | TC wait |

TCGen05 MMA operand encodings (from instruction table):
`MUUuP`, `MUUMuP`, `MMUuP`, `MMUMuP` (M=matrix, U=desc, u=uniform, P=pred)  
With accumulator descriptors: `MUUudP`, `MUUuPC`, `MMUudP`, `MMUuPC`, `MUUMudP`, `MMUMudPC`  
With metadata: `MUUuMMP`, `MUUMuMMP`, `MMUuMMP`, `MMUMuMMP` (sparse)

#### TCGen05 Guardrails (internal)

These are internal debug/verification instructions, not user-facing PTX:

| Mnemonic | SM | Description |
|---|---|---|
| `_tcgen05.guardrails.is_phase_valid` | 100+ | Validate TC phase |
| `_tcgen05.guardrails.are_columns_allocated` | 100+ | Check column allocation |
| `_tcgen05.guardrails.is_current_warp_valid_owner` | 100+ | Check warp ownership |
| `_tcgen05.guardrails.in_physical_bounds` | 100+ | Check physical bounds |
| `_tcgen05.guardrails.allocation_granularity` | 100+ | Validate allocation granularity |
| `_tcgen05.guardrails.datapath_alignment` | 100+ | Validate datapath alignment |
| `_tcgen05.guardrails.sp_consistency_across_idesc_mod` | 100+ | Sparse consistency check |
| `_tcgen05.guardrails.check_sparse_usage` | 100+ | Validate sparse usage |

### Matrix Utility Instructions

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `ldmatrix` | `.sync .aligned .m8n8 .num` | `d, [ptr]` | 75+ | 6.5 | Load matrix from shared memory |
| `stmatrix` | `.sync .aligned .m8n8 .num` | `[ptr], a` | 90+ | 7.8 | Store matrix to shared memory |
| `movmatrix` | `.aligned` | `d, a` | 80+ | 7.1 | Move/transform matrix fragment |

## SIMD Video Instructions

These 8/16-bit SIMD instructions operate on packed sub-word elements within 32-bit registers.

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `vadd` | `.s32.s32 .s32.u32 .u32.s32 .u32.u32` | `d, a.asel, b.bsel, c` | 20+ | 2.0 | SIMD add with secondary op |
| `vsub` | (same) | `d, a.asel, b.bsel, c` | 20+ | 2.0 | SIMD subtract |
| `vmad` | (same) | `d, a.asel, b.bsel, c` | 20+ | 2.0 | SIMD multiply-add |
| `vmin` | (same) | `d, a.asel, b.bsel, c` | 20+ | 2.0 | SIMD minimum |
| `vmax` | (same) | `d, a.asel, b.bsel, c` | 20+ | 2.0 | SIMD maximum |
| `vabsdiff` | (same) | `d, a.asel, b.bsel, c` | 20+ | 2.0 | SIMD absolute difference |
| `vset` | (same) | `d, a.asel, b.bsel, c` | 20+ | 2.0 | SIMD set on compare |
| `vshl` | `.u32.u32` | `d, a.asel, b.bsel, c` | 20+ | 2.0 | SIMD shift left |
| `vshr` | `.s32.u32 .u32.u32` | `d, a.asel, b.bsel, c` | 20+ | 2.0 | SIMD shift right |
| `vadd2` | (packed-pairs) | `d, a, b, c` | 30+ | 3.0 | Dual 16-bit SIMD add |
| `vsub2` | (packed-pairs) | `d, a, b, c` | 30+ | 3.0 | Dual 16-bit SIMD subtract |
| `vmin2` | (packed-pairs) | `d, a, b, c` | 30+ | 3.0 | Dual 16-bit SIMD minimum |
| `vmax2` | (packed-pairs) | `d, a, b, c` | 30+ | 3.0 | Dual 16-bit SIMD maximum |
| `vabsdiff2` | (packed-pairs) | `d, a, b, c` | 30+ | 3.0 | Dual 16-bit absolute difference |
| `vset2` | (packed-pairs) | `d, a, b, c` | 30+ | 3.0 | Dual 16-bit set on compare |
| `vavrg2` | (packed-pairs) | `d, a, b, c` | 30+ | 3.0 | Dual 16-bit average |
| `vadd4` | (packed-quads) | `d, a, b, c` | 30+ | 3.0 | Quad 8-bit SIMD add |
| `vsub4` | (packed-quads) | `d, a, b, c` | 30+ | 3.0 | Quad 8-bit SIMD subtract |
| `vmin4` | (packed-quads) | `d, a, b, c` | 30+ | 3.0 | Quad 8-bit SIMD minimum |
| `vmax4` | (packed-quads) | `d, a, b, c` | 30+ | 3.0 | Quad 8-bit SIMD maximum |
| `vabsdiff4` | (packed-quads) | `d, a, b, c` | 30+ | 3.0 | Quad 8-bit absolute difference |
| `vset4` | (packed-quads) | `d, a, b, c` | 30+ | 3.0 | Quad 8-bit set on compare |
| `vavrg4` | (packed-quads) | `d, a, b, c` | 30+ | 3.0 | Quad 8-bit average |

Element selectors: `.b0 .b1 .b2 .b3` (byte), `.h0 .h1` (half-word)  
Secondary ops: `.add .min .max`  
Saturation: `.sat`

## Miscellaneous

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `getctarank` | `.shared .global` | `d, a` | 90+ | 7.8 | Get CTA rank in cluster from address |
| `istypep` | `.texref .samplerref .surfref` | `p, a` | all | 1.0 | Test if variable is a type |
| `preexit` | — | — | all | 1.0 | Pre-exit notification |
| `stacksave` | — | `d` | 20+ | 2.0 | Save stack pointer |
| `stackrestore` | — | `a` | 20+ | 2.0 | Restore stack pointer |
| `alloca` | — | `d, size` | 20+ | 2.0 | Dynamic stack allocation |
| `clusterlaunchcontrol.try_cancel.async` | — | `[mbar], d` | 100+ | 8.7 | Cluster launch cancel (async) |
| `clusterlaunchcontrol.query_cancel` | — | `d, [mbar]` | 100+ | 8.7 | Query cluster launch cancel status |

### Register and Shared Memory Control

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `setmaxnreg.inc` | — | `N` | 90+ | 7.8 | Increase max register count |
| `setmaxnreg.dec` | — | `N` | 90+ | 7.8 | Decrease max register count |
| `setmaxreg.alloc` | — | `N` | 100+ | 8.6 | Allocate registers to max |
| `setmaxreg.dealloc` | — | `N` | 100+ | 8.6 | Deallocate from max registers |
| `setmaxreg.try_alloc` | — | `d, N` | 100+ | 8.6 | Try-allocate registers |
| `setsmemsize` | — | `N` | 90+ | 7.8 | Set dynamic shared memory size |
| `setsmemsize.flush` | — | `N` | 100+ | 8.6 | Set shared memory size with flush |
| `getnextworkid` | — | `d` | 90+ | 8.0 | Get next dynamic work unit ID |

### Warp-Group Management

| Mnemonic | Type suffixes | Operands | SM | PTX | Description |
|---|---|---|---|---|---|
| `_warpgroup.arrive` | — | — | 90+ | 7.8 | Warpgroup arrive (internal) |
| `_warpgroup.wait` | — | `N` | 90+ | 7.8 | Warpgroup wait |
| `_warpgroup.commit_batch` | — | — | 90+ | 7.8 | Warpgroup commit batch |

## Internal Instructions

These underscore-prefixed instructions are not part of the public PTX ISA. They are generated internally by ptxas during lowering, stub synthesis, or as pre-codegen IR representations. All are registered in the instruction table builder `sub_46E000` and appear in `--dumpir` output, but users never write them directly.

### Internal Memory

| Mnemonic | Type suffixes | Operands | String addr | Handler / Formatter | Description |
|---|---|---|---|---|---|
| `_ldldu` | (varies) | `d, [a]` | `0x1d080ee` | formatter `sub_4DD860` | Unified load-uniform; combines ld+ldu semantics for uniform-cache-path loads |
| `_ldsm` | `.b8 .b16 .s8.s4 .u8.u4 .s4.s2 .u4.u2` | `d, [M]` | `0x1d076c2` | handlers `sub_46B0C0`--`sub_46B160`, validator `sub_4AEB60` | Load shared matrix; loads matrix tiles from shared memory into registers for MMA. Opcode ID 28 |
| `_movm` | `.b16 .s8.s4 .u8.u4 .s4.s2 .u4.u2` | `d, a` | `0x1d076da` | handlers `sub_46B1B0`--`sub_46B260` | Move matrix; register-to-register matrix data movement with optional format conversion. Opcode ID 29 |

### Internal Cache Control

| Mnemonic | Type suffixes | Operands | String addr | Table builder xref | Description |
|---|---|---|---|---|---|
| `_createpolicy.fractional` | `.L2` | `d, fraction` | `0x1d0813a` | `0x47752f` | Internal form of `createpolicy.fractional`; creates fractional L2 cache eviction policy |
| `_createpolicy.range` | `.L2` | `d, lo, hi, hit, miss` | `0x1d08158` | `0x477579` | Internal form of `createpolicy.range`; creates L2 policy for address range |

### Internal Surface

| Mnemonic | Type suffixes | Operands | String addr | Table builder xrefs | Description |
|---|---|---|---|---|---|
| `_sulea.b` | (varies) | `d, [surf, coord]` | `0x1d088bc` | `0x4815cd`, `0x48166b` | Surface load effective address, bindless; computes address for `suld.b` without performing the load |
| `_sulea.p` | (varies) | `d, [surf, coord]` | `0x1d088c5` | `0x48161c`, `0x4816ba` | Surface load effective address, packed; computes address for `sust.p`-mode surface access |

### Internal FP / Guard

| Mnemonic | Type suffixes | Operands | String addr | Table builder xref | Description |
|---|---|---|---|---|---|
| `_checkfp.divide` | (varies) | `d, a, b` | `0x1d088d2` | `0x481709` | FP division guard; inserted during lowering to validate divisor (handles division-by-zero, denormals) before SASS `div` emission |

### Internal Control Flow / ABI

| Mnemonic | Type suffixes | Operands | String addr | Table builder xref | Description |
|---|---|---|---|---|---|
| `_gen_proto` | — | (opaque) | `0x1d08903` | `0x48189a` | Generate function prototype; synthesizes call prototypes for indirect / device-runtime calls during ABI resolution |
| `_jcall` | — | `target` | `0x1d0890e` | `0x4818df` | Internal jump-call; used inside auto-generated unified-function-stub (UFT) wrappers synthesized by `sub_451680` (`.func .attribute(.unified_func_stub) __cuda_uf_stub_%s() { _jcall %s; }`) |

### Internal Warp

| Mnemonic | Type suffixes | Operands | String addr | Table builder xref | Description |
|---|---|---|---|---|---|
| `_match` | (varies) | `d, a` | `0x1d08a24` | `0x483404` | Internal match; pre-sync lowered form of warp `match` instruction, distinct from the public `match.sync` |

### Internal MMA / Tensor Core

| Mnemonic | Type suffixes | Operands | String addr | Handlers | Description |
|---|---|---|---|---|---|
| `_mma.warpgroup` | 135 type combos (F16, BF16, TF32, FP8, INT8) | `d, a, b, c` | `0x1d072e3` | 135 handlers `sub_4668A0`--`sub_469FD0` | Warp-group MMA; pre-codegen form of WGMMA. Each handler registers one (src, dst, acc) type triple via `sub_465030`. Lowers to `MERCURY_warpgroup_mma_*` SASS opcodes (sm_90+) |
| `_zzn.z8a8x4` | (sub-byte int) | `d, a, b, c` | `0x1cfdc03` | data table at `0x1cfe678` | ROT13-obfuscated `_mma.m8n8k4`; sub-byte integer MMA with tile shape m8n8k4 for INT4/INT2 and bit-level XOR MMA (sm_75+) |

**Handler address summary** for internal instructions:

| Range | Contents |
|---|---|
| `sub_46B0C0`--`sub_46B260` | `_ldsm` (3) + `_movm` (3) type-variant handlers |
| `sub_4668A0`--`sub_469FD0` | `_mma.warpgroup` 135 type-variant handlers |
| `sub_4AEB60` | `_ldsm` validator (3.7 KB) — handles `.s8.s4`/`.u8.u4` format rules |
| `sub_451680` | `_jcall` UFT stub generator |
| `sub_4DD860` | `_ldldu` PTX text formatter |

## Instruction Table Builder Internals

### Registration Mechanism

The 93 KB function `sub_46E000` runs once during parser initialization (`sub_451730`). Each of its 1,141 calls to `sub_46BED0` has the form:

```c
sub_46BED0(table, operand_encoding, opcode_id, opcode_name, 
           type_flags, sm_requirement, xmm_data, extra);
```

Where:
- `operand_encoding` is a compact string like `"F32F32"`, `"I32I8I8I32"`, `"MUUuP"`
- `opcode_id` is the internal opcode integer (mapped 1:1 to Ori IR opcodes from `ctor_003`)
- `type_flags` is a bitfield encoding which `.sNN`/`.uNN`/`.fNN`/`.bNN` qualifiers are legal
- `sm_requirement` gates the instruction to architectures >= this SM version

The operand encoding characters are:

| Char | ID | Meaning | Type bits registered |
|---|---|---|---|
| `F` | 1 | Float operand | `.f16 .f32 .f64` |
| `H` | 2 | Half-precision | `.f16 .f16x2` |
| `N` | 3 | Immediate / numeric | (no type suffix) |
| `I` | 4 | Integer operand | `.s8`--`.s64 .u8`--`.u64` |
| `B` | 5 | Bitwise operand | `.b8`--`.b128` |
| `P` | 6 | Predicate | `.pred` |
| `O` | 7 | Optional operand | (no type suffix) |
| `E` | 8 | Extended type | `.bf16 .e4m3 .e5m2` |
| `Q` | 10 | FP8 type | `.e5m2 .e4m3` (fp8) |
| `R` | 11 | FP4/narrow type | `.e2m1` (fp4) |
| `M` | — | Matrix descriptor | Tensor core descriptor |
| `U` | — | Uniform descriptor | TMA/TC uniform register |
| `C` | — | Carry/accumulator | MMA accumulator control |

When a letter is followed by a digit, that digit constrains the bit-width: `F32` means only `.f32`, `I16` means only `.s16`/`.u16`. The function `sub_1CB0850` registers each valid width into a bitset.

### Descriptor Layout

Each registered instruction creates a 368-byte descriptor node via `sub_424070`. Key fields:

| Offset | Field | Description |
|---|---|---|
| +0 | opcode_id | Internal opcode identifier |
| +8 | type_flags | Bitfield of legal type qualifiers |
| +12 | xmm_data | 128-bit SIMD data (rounding, modifiers) |
| +28 | extra_flags | Architecture / behavior flags |
| +36 | operand_count | Number of operands |
| +40+ | operand_slots | Per-operand type bitsets (4 slots max, each 8 bytes) |
| +232 | name_length | Length of the opcode name string |

The two hash tables at offsets 2472 and 2480 in the instruction table provide dual-path lookup — the first table is the primary lookup; if the opcode is not found, the second table is checked. This two-table scheme separates core instructions from extended/variant instructions.

### Instruction Count by Category

Based on the 1,141 registration calls and the 431 decoded Ori IR opcodes (from `ctor_003`):

| Category | Approximate descriptor count |
|---|---|
| Integer arithmetic (add/sub/mul/mad/div/rem/...) | ~120 |
| Float arithmetic (fadd/fmul/fma/div/rcp/sqrt/...) | ~100 |
| Half/BF16 arithmetic | ~60 |
| Comparison and selection (setp/selp/set/slct) | ~50 |
| Logic and shift (and/or/xor/shl/shr/lop3/shf) | ~40 |
| Conversion (cvt + 100+ type combinations) | ~180 |
| Load/store/atomic (ld/st/atom/red + variants) | ~150 |
| Texture/surface (tex/suld/sust/sured/txq) | ~80 |
| Control flow (bra/call/ret/exit/bar/barrier) | ~50 |
| MMA/WMMA/WGMMA/TCGen05 | ~160 |
| SIMD video (vadd/vsub/vmin/vmax/...) | ~60 |
| Miscellaneous (prmt/bfe/bfi/shfl/vote/...) | ~91 |
| **Total** | **1,141** |

## PTX 8.x New Instructions

Instructions introduced in PTX ISA 8.0--8.7 (CUDA 12.0--12.8), verified from SM architecture gates and PTX version checks in the ptxas v13.0.88 binary:

| PTX | Instruction | SM | Description |
|---|---|---|---|
| 8.0 | `cp.async.bulk` | 90+ | TMA bulk async copy |
| 8.0 | `cp.async.bulk.tensor` | 90+ | TMA tensor async copy |
| 8.0 | `cp.reduce.async.bulk` | 90+ | Bulk async copy with reduction |
| 8.0 | `cp.reduce.async.bulk.tensor` | 90+ | Tensor async copy with reduction |
| 8.0 | `cp.async.bulk.prefetch` | 90+ | TMA prefetch |
| 8.0 | `cp.async.bulk.prefetch.tensor` | 90+ | TMA tensor prefetch |
| 8.0 | `cp.async.bulk.commit_group` | 90+ | Commit TMA group |
| 8.0 | `cp.async.bulk.wait_group` | 90+ | Wait for TMA group |
| 8.0 | `tensormap.replace` | 90+ | Runtime tensormap modification |
| 8.0 | `elect` / `elect.one` | 90+ | Elect leader thread |
| 8.0 | `mbarrier.complete_tx` | 90+ | Transaction mbarrier completion |
| 8.0 | `mbarrier.expect_tx` | 90+ | Set expected transaction count |
| 8.0 | `st.bulk` | 90+ | Bulk store |
| 8.0 | `szext` | 90+ | Sign/zero extend at bit position |
| 8.0 | `getnextworkid` | 90+ | Dynamic work unit |
| 8.1 | `st.async` | 90+ | Asynchronous store |
| 8.1 | `red.async` | 90+ | Asynchronous reduction |
| 8.1 | `multimem.ld_reduce` | 90+ | Multicast load-reduce |
| 8.1 | `multimem.st` | 90+ | Multicast store |
| 8.1 | `multimem.red` | 90+ | Multicast reduction |
| 8.6 | `tcgen05.mma` | 100+ | 5th-gen tensor core MMA |
| 8.6 | `tcgen05.mma.ws` | 100+ | 5th-gen MMA with warp scale |
| 8.6 | `tcgen05.ld` / `tcgen05.st` | 100+ | TC load/store |
| 8.6 | `tcgen05.ld.red` | 100+ | TC load with reduction |
| 8.6 | `tcgen05.cp` | 100+ | TC copy |
| 8.6 | `tcgen05.commit` | 100+ | TC commit |
| 8.6 | `tcgen05.shift` | 100+ | TC shift accumulator |
| 8.6 | `tcgen05.alloc` / `tcgen05.dealloc` | 100+ | TC column allocation |
| 8.6 | `tcgen05.relinquish_alloc_permit` | 100+ | Relinquish TC permit |
| 8.6 | `tcgen05.fence` / `tcgen05.wait` | 100+ | TC fence/wait |
| 8.6 | `setmaxreg.alloc` / `.dealloc` / `.try_alloc` | 100+ | Register management |
| 8.6 | `setsmemsize.flush` | 100+ | Shared memory with flush |
| 8.7 | `clusterlaunchcontrol.try_cancel.async` | 100+ | Cluster launch cancel |
| 8.7 | `clusterlaunchcontrol.query_cancel` | 100+ | Query cancel status |

## Cross-References

- [PTX Parser (Flex + Bison)](../pipeline/ptx-parser.md) — scanner, parser, instruction table builder details
- [PTX Directive Handling](../pipeline/ptx-directives.md) — `.version`, `.target`, `.entry`, etc.
- [PTX-to-Ori Lowering](../pipeline/ptx-to-ori.md) — how PTX instructions become Ori IR
- [Ori IR Instructions](../ir/instructions.md) — the 431 Ori IR opcodes derived from PTX
- [Intrinsic Table](../intrinsics/index.md) — 608 built-in helper functions
- [SM Architecture Map](../targets/index.md) — architecture version requirements
- [SASS Opcode Catalog](./sass-opcodes.md) — SASS equivalents of PTX instructions

## Key Functions

| Address | Size | Role | Confidence |
|---------|------|------|------------|
| `sub_46E000` | 93KB | Instruction table builder; runs once during parser init, makes 1,141 calls to `sub_46BED0` to register all PTX instruction descriptors | 0.95 |
| `sub_46BED0` | — | Instruction descriptor registrar; called 1,141 times with operand encoding, opcode ID, type flags, SM requirement | 0.95 |
| `sub_46C690` | — | Instruction lookup entry point; dispatches to `sub_46C6E0` for name-to-descriptor matching | 0.90 |
| `sub_46C6E0` | 6.4KB | Instruction name matcher; resolves PTX mnemonic strings to instruction table descriptors | 0.90 |
| `sub_5D4190` | 12.9KB | PTX text formatter dispatch; 81-string + 473-entry hash table routing to per-instruction formatters | 0.90 |
| `sub_489390` | — | SM version gate; validates minimum SM architecture for each instruction | 0.85 |
| `sub_489050` | — | PTX ISA version gate; validates minimum PTX version for each instruction | 0.85 |
| `sub_4BFED0` | — | WMMA shape validator; checks legal WMMA shape combinations (`.m16n16k16`, `.m32n8k16`, etc.) | 0.85 |
| `sub_451680` | — | UFT stub generator; synthesizes `_jcall` wrappers for unified-function-stub entries | 0.90 |
| `sub_451730` | — | Parser initialization; calls `sub_46E000` to build the instruction table | 0.85 |
| `sub_4DD860` | — | `_ldldu` PTX text formatter; handles internal unified load-uniform instruction | 0.85 |
| `sub_4AEB60` | 3.7KB | `_ldsm` validator; validates `.s8.s4`/`.u8.u4` format rules for shared matrix loads | 0.85 |
| `sub_465030` | — | MMA type triple registrar; called by 135 `_mma.warpgroup` handlers to register (src, dst, acc) type combinations | 0.85 |
| `sub_424070` | — | Instruction descriptor allocator; creates 368-byte descriptor nodes for each registered instruction | 0.85 |
| `sub_1CB0850` | — | Width bitset registrar; registers valid bit-widths (e.g., `F32` -> `.f32` only) into per-operand bitsets | 0.80 |
