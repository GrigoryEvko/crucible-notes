# Sim BN / RNG / Search / Collective / DMA Kernels

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; the cp311/cp312 wheels share the `.text` logic but drift the VAs). Every kernel lives in `neuronxcc/starfish/lib/libBIRSimulator.so` (md5 `f3acdcba9176056cb50daac01389dd13`, 36 MB — the FUNCTIONAL reference interpreter, distinct from `libBIR.so` md5 `12bb979f…`). For `.text`/`.rodata` the virtual address equals the file offset (`readelf -S`: `.text [12]` Addr `0xf1ad0` == Off `0xf1ad0`; `.rodata [14]` Addr `0x58f000` == Off `0x58f000`), so `xxd`/`objdump` at the VA reads the datum directly; `.data` carries a +0x1000 delta and is not used here. Treat every address as version-pinned.*

## Abstract

The BIR simulator's 110-case visitor ([7.34](sim-dispatch-state.md)) routes five distinct opcode families to the kernels documented here, and each family asks a separate question of the interpreter's numeric model. The **BatchNorm** family (IT 34–38) is a tiled two-pass Welford / Chan parallel-variance pipeline that produces *population* variance in fp32. The **RNG** family (IT 58–60, 97–100) is not one generator but *three independent generation regimes* — a 3-way `LFSR / PCG32 / Philox` switch (gen-1), a cuRAND `XORWOW` engine (gen-2), and a host-side `MT19937-64` seed source — plus the gen-1/gen-2 state get/set machinery. The **DVE search** family (IT 88–91) is the silicon "top-8" primitive set: an 8-slot descending insertion sort, a one-to-one argmax index resolver, and a sentinel strike-out, fused as the TopK workhorse `MaxIndexAndMatchReplace`. The **collective** family (IT 48–50) is — surprisingly — a *full N-rank registry* model, not a single-rank stub with zeroed peers, with one decisive quirk: in the multi-worker harness `AllReduce` degenerates to a local `×N` approximation. The **DMA** family (IT 19/22/23/32, 67–72, 107) is a single access-pattern-driven element copy `read-src-AP → MemoryObject → write-dst-AP`, with descriptor lists modeled as re-dispatched sub-instructions rather than a hardware descriptor engine.

This page recovers the per-op numeric/state model for each family. It builds on — and does **not** re-derive — two siblings: [7.34 Dispatch & State](sim-dispatch-state.md) (the visitor, the `Memory`/`SyncState`/`RegState` objects these kernels mutate, the `NeuronCoresManager`) and [7.35 Core Arithmetic](sim-core-arithmetic.md) (the shared `cast_copy_or_accumulate` RMW core, the per-dtype widen/narrow, the RNE rounding). The collective reducer's libm `fmaxf`/`fminf` fold is contrasted against — not duplicated from — the scatter-RMW `minss`/`maxss` core documented there. For the *codegen* side of these ops — how KLR lowers `Rng→Memset(Random)` and how the `codegenEngine {2,4,5,3,1,6}` table picks `Pool`/`XORWOW` — see [7.29 Codegen of DVE/RNG/Control](codegen-dve-rng-control.md).

| Family | Opcodes | Kernels | Numeric model |
|---|---|---|---|
| **BatchNorm** | 34–38 | `visitInstBNStats … BNBackprop2` | Welford + Chan parallel-variance, fp32, population var |
| **RNG** | 58–60, 97–100 | `visitInstRand / Rand2 / *RandState` | 3-way LFSR/PCG32/Philox (gen-1), XORWOW (gen-2), MT19937-64 seed |
| **DVE search** | 88–91, 104 | `visitInstMax / MaxIndex / MatchReplace` | 8-slot insertion sort, one-to-one argmax, sentinel strike-out |
| **Collective** | 48–50 | `visitInstCollectiveCompute / Send / Recv` | full N-rank registry fold; `AllReduce` `×N` in multi-worker |
| **DMA** | 19/22/23/32, 67–72, 107 | `visitInstDMACopy …` | AP→AP `memcpy`; descriptor list = re-dispatch |

A shared invariant ties all five families together and a reimplementer must honor it: **every input `MemoryObject` is widened to fp32 (`cast_to(dt=16)`) before any arithmetic.** BN moments, collective reductions, DVE value comparisons, and DMA-CCE folds are *all* single-precision regardless of the stored dtype (bf16/fp16/fp8 inputs are widened first; there is no fp64 path anywhere). The simulator therefore provides a faithful fp32-accumulating reference but cannot expose fp32-rounding divergences from a hypothetical fp64 golden. [CONFIRMED — cast call in every family body; see the per-family sections.]

---

## 1. BatchNorm family (IT 34–38) — Welford + Chan parallel variance

The five BN kernels implement a tiled, two-pass batch-norm statistics + backward pipeline matching the Trainium BN macro. The forward path is `BNStats → BNStatsAggregate`; the backward path is `BNGradients` (reduce) feeding `BNBackprop` / `BNBackprop2` (per-element apply).

| IT | Name | body | size | thunk |
|---:|---|---|---:|---|
| 34 | BNStats | `0x1df7b0` | 1280 B | `0xee5a0` |
| 35 | BNStatsAggregate | `0x1dfcb0` | 832 B | `0xec9c0` |
| 36 | BNGradients | `0x1dfff0` | 1392 B | `0xebcc0` |
| 37 | BNBackprop | `0x1e0560` | 1360 B | `0xed1b0` |
| 38 | BNBackprop2 | `0x1e0ab0` | 1542 B | `0xef5d0` |

The statistics travel as packed fixed-stride fp32 records in SBUF. The representation is the key to all five:

- **BNStats output** = 6 elt/partition = **two Welford triples** `[n_A, mean_A, M2_A, n_B, mean_B, M2_B]`. Lane A is the even-indexed free elements, lane B the odd-indexed (2-way interleaved Welford). `M2` is *raw* Σ(x−mean)² — not yet divided by n.
- **BNStatsAggregate input** = K·6 elt/partition (K Welford triple-pairs to merge); output = 2 elt/partition `(mean, variance)`, where variance is the **population** variance Σdev²/N.
- **BNGradients output** = 3 elt/partition = three reduction sums (the gradient coefficients).

The count-step constant `1.0f` lives once at rodata `0x5f02c4` (`= 0x3f800000`), reused by `BNStats` (Welford count++) and `BNStatsAggregate`. [CONFIRMED — `xxd -s 0x5f02c4 -l 4` = `00 00 80 3f`.] The `BNStats` prologue anchors the VA: `xxd -s 0x1df7b0` = `41 57 31 d2 31 c9 41 56 …` (`push r15; xor edx; xor ecx; push r14; …`). [CONFIRMED this pass.]

### 1.1 BNStats (IT 34) — 2-way interleaved Welford

The kernel reads input AP0, casts to fp32, optionally transposes (`apply_transpose`, a `MaybeAffine<bool>` at `Inst+0xF0` with variant tag at `Inst+0x110` — non-constant throws `__throw_bad_variant_access`), then runs a per-partition 2-lane online Welford and writes 6 fp32 stats per partition. The output counts are *seeded to the lane sizes*, not zero:

```c
// per partition row, after cast_to(fp32) and optional 32x32 tile transpose
out[0] = n_A = (N + 1) >> 1;   // ceil(N/2)  even-indexed lane
out[3] = n_B =  N      >> 1;   // floor(N/2) odd-indexed lane
out[1] = out[2] = out[4] = out[5] = 0.0f;     // mean_A, M2_A, mean_B, M2_B
float n = 0.0f;                                // SHARED running pair count
for (i = 0; i < N; ++i) {                      // alternate lane A / B per element
    int L = i & 1;                             // 0 -> A (out[0..2]), 1 -> B (out[3..5])
    if (L == 0) n += 1.0f;                     // 1.0f @ 0x5f02c4 ; increments once PER PAIR
    float x = in[i];
    float r        = 1.0f / n;                 // divss
    float oldMean  = out[3*L + 1];
    float newMean  = (1.0f - r)*oldMean + r*x; // = oldMean + (x-oldMean)/n  (canonical Welford)
    out[3*L + 1]   = newMean;
    out[3*L + 2]  += (x - newMean) * (x - oldMean);  // M2 recurrence (raw Sigma dev^2)
}
```

> **QUIRK — the two lanes share one running count `n`.** The shared `n` (xmm5) increments once per *pair* (when lane B is processed), so when lane A processes its k-th element `n` equals what lane B sees for its k-th — `n ≈ pair_index`. The per-lane *output* counts `ceil/floor(N/2)` are what `BNStatsAggregate` uses as final merge weights; the running `n` is only the Welford weight. [CONFIRMED — disasm `0x1df9b7`–`0x1dfa06`, count `addss 1.0f` @ `0x1df947`/`0x1df9ac`.] The transpose path is the same 32×32 tile-swap idiom as `StreamTranspose` (out-index `(p&0x1F | e&~0x1F) + N*((p&~0x1F) | e&0x1F)`), letting BNStats reduce over the partition axis instead of the free axis; `N==1` is a plain copy. [transpose math CONFIRMED; non-multiple-of-32 edges unfuzzed.]

### 1.2 BNStatsAggregate (IT 35) — Chan parallel-variance merge

`BNStatsAggregate` merges K Welford triples per partition into a single `(mean, variance)` pair. The input may be the 2 lanes from one `BNStats` and/or the partial stats of several tiles concatenated along the free axis. The input guard is `NumElementsPerPartition % 6 == 0` (line `0x1259`); the loop strides by 3 (one triple) and runs `NEPP/3` times, merging all triples sequentially into one accumulator. The output guard is `ElementsPerPartition == 2`.

```c
// per output partition; running accumulator starts empty
float n_acc = 0.0f, mean_acc = 0.0f, var_acc = 0.0f;  // var_acc holds NORMALIZED var
for (each input triple t = (n_t, mean_t, M2_t)) {     // disasm 0x1dfe40-0x1dfeb8
    float N     = n_t + n_acc;
    float r     = 1.0f / N;
    float delta = mean_acc - mean_t;
    mean_acc = (mean_acc*n_acc + n_t*mean_t) * r;       // weighted mean
    var_acc  = (var_acc*n_acc + M2_t) * r               // un-normalize, add raw M2, re-normalize
             + n_t*n_acc*delta*delta * r*r;             // Chan cross term (extra *r)
    n_acc   += n_t;
}
out[0] = mean_acc;
out[1] = var_acc;     // POPULATION variance  Sigma(x-mean)^2 / N_total  (biased / ML-style)
```

> **GOTCHA — Stats and Aggregate are a matched raw/normalized pair.** `BNStats` emits *raw* `M2` (Σdev²); `BNStatsAggregate`'s running accumulator stores a *normalized* variance (Σdev²/N). Each merge step therefore un-normalizes the running variance (`·n_acc`), adds the raw `M2_t`, and re-normalizes (`·r`); the Chan cross term carries the extra `·r` (`·r²` total) for the same reason. The final `out[1]` is the **population** variance (÷N, not N−1) — there is **no epsilon and no rsqrt here**: `invstd = 1/√(var+eps)` is a precomputed *input* to `BNGradients` (the `Varfactor` operand), materialized by a separate Reciprocal/Activation kernel. [CONFIRMED.]

### 1.3 BNGradients (IT 36) — backward reduction

Reduces 4 inputs (AP0 `Ofmap x`, AP1 `OfmapGrad dy`, AP2 `Mean` 1 elt, AP3 `Varfactor vf` 1 elt) into 3 per-partition gradient coefficients:

```c
out[0] = out[1] = out[2] = 0.0f;
for (i = 0; i < NEPP; ++i) {            // decompiled l.137-142
    float t = (x[i] - m) * dy[i] * vf;
    out[2] += t;                        // Sigma (x-m)*dy*vf      <- dgamma reduction
    out[1] += t * vf;                   // Sigma (x-m)*dy*vf^2    <- 2nd-order term for dx apply
    out[0] += dy[i];                    // Sigma dy               <- dbeta reduction
}
```

The three emitted sums are exactly the per-channel coefficients the backprop apply kernels consume (`vf` is the inverse-stddev scale). These are plain fp32 Σ — no Welford, no Kahan; the `(x−m)` centering reduces cancellation but the sums are naive adds. [CONFIRMED — asserts `0x1297/0x129A/0x129C/0x12C4/0x12C5`.]

### 1.4 BNBackprop (IT 37) and BNBackprop2 (IT 38) — the dx apply

Both compute the per-element input gradient `dx`; the **numerics are byte-identical**, and the *only* difference is how the three coefficients are packed:

```c
// BOTH variants, per element i (disasm 0x1e08ad-0x1e08d6):
dx[i] = ( dy[i]*total_elements  -  ( (x[i] - m)*Bc + A ) ) * scale;
// total_elements = *(float*)(Inst+0xF0)  (JSON "total_elements", float scalar = N)
```

This is the standard fused BN backward `dx = (γ·invstd/N)·(N·dy − Σdy − (x−μ)·invstd²·Σ(x−μ)dy)`, regrouped so `{scale = γ·invstd/N, A = Σdy, Bc = invstd²·Σ(x−μ)dy}`. [arithmetic CONFIRMED; the γ/invstd factoring of the 3 opaque coefficients is HIGH/INFERRED — the simulator only sees fp32 coefficients, the precise fold is set by the walrus codegen.] The variant difference:

| | BNBackprop (37) | BNBackprop2 (38) |
|---|---|---|
| input AP count | 4 | 5 |
| coefficient tensor | one `Gradients` = 3 elt `[scale, A, Bc]` | two: `GradientsA`=1 `[scale]`, `GradientsBc`=2 `[A, Bc]` |
| Mean operand | AP3 | AP4 |
| sim assert | `ABC AP == 3 elt/part` | `A AP==1 AND Bc AP==2 elt/part` |
| dx formula | identical | identical |

So v2 is the *split-tensor* form — it separates the scalar `scale` from the per-channel `(A, Bc)` pair so codegen can produce them on independent engines. It is **not** a with/without-affine split and not a two-pass split; it is purely an operand-layout variant of the same single-pass dx apply, invisible to the numerics. [same math CONFIRMED; the "split for separate engines" rationale HIGH/INFERRED.]

---

## 2. RNG family (IT 58–60, 97–100) — three generation regimes

The simulator implements **two independent RNG generations plus a host-side MT19937-64 seed engine** — *not* the single XORWOW that an earlier survey guessed. The generations are partitioned by opcode and by an `EngineType` discriminator.

| Function | Addr | Role |
|---|---|---|
| `visitInstRand(InstRand&)` | `0x1d6290` | **GEN-1**: LFSR / PCG32 / Philox 3-way switch |
| `visitInstRand2(InstRand2&)` | `0x1d2fa0` | **GEN-2**: XORWOW → `[min,max)` |
| `generateXorwowRandom(Xorwow&)` | `0x1aa180` | XORWOW step → uint32 |
| `xorwowStep(Xorwow&, float, float)` | `0x1aa1c0` | uint32 → float `[min,max)` |
| `visitInstGetRandState` | `0x1d2ad0` | IT58 = `doCopy` snapshot |
| `visitInstSetRandState` | `0x1f8490` | IT59: restore by `EngineType`; seeds MT |
| `visitInstRandGetState` | `0x1d3740` | IT99: snapshot gen-1 / XORWOW |
| `visitInstRandSetState` | `0x1d4360` | IT98: restore gen-1 / XORWOW |
| `checkRandomSeedStateSize` | `0x1b26d0` | asserts 6 or 24 u32/part |
| `std::mersenne_twister_engine<u64,64,312,…>` | `0x237990` | MT19937-64 seed source |
| `InstRng (IT 100)` | base `0x1aa750` | sim-UNSUPPORTED (skip-and-note) |

The InstVisitor object holds all three states: the MT19937-64 312-qword state at `+0x3E0` (`mti` at `+0xDA0`), a `std::vector<XorwowState>` for gen-1 6-u32 state at `+0xDB8`, and a `std::vector<array<XorwowState,4>>` for gen-2 24-u32 state at `+0xDD0`; init flags at `this+3496` (gen-1) / `this+3497` (XORWOW).

### 2.1 GEN-1 — `visitInstRand` (IT 60): 3-way PRNG switch

`visitInstRand` reads `RandomAlgorithmKind = *(uint32*)(InstRand + 240)` and dispatches:

```c
uint32_t algorithm = *(uint32_t*)(inst + 0xF0);   // mov esi,[rdi+0F0h] @0x1d680a
if      (algorithm == 1) /* PCG32  */ goto pcg32;       // block 0x1d71b4
else if (algorithm == 2) /* PHILOX */ goto philox;      // block 0x1d6ac5
else if (algorithm == 0) /* LFSR   */ goto lfsr;        // block 0x1d6cdf
else throw runtime_error("Supported PRNGs are LFSR/PCG32/PHILOX_1: ");  // @0x5aaa40
```

Each per-partition state is 6×uint32 (24 bytes) in `MemoryType RNGSTATE (0x40)`. **PCG32** is the canonical PCG-XSH-RR 64/32 with the default constants — both verified as `movabs` immediates in the disasm:

```c
// PCG-XSH-RR 64/32 (constants CONFIRMED at 0x1d71b4 / 0x1d71be)
uint64_t oldstate = state;
state = oldstate*0x5851F42D4C957F2DULL + 0x14057B7EF767814FULL;   // MUL, INC
uint32_t xorshifted = (uint32_t)(((oldstate >> 18) ^ oldstate) >> 27);
uint32_t rot        = (uint32_t)(oldstate >> 59);
output = (xorshifted >> rot) | (xorshifted << ((-rot) & 31));     // rotr32, `ror eax,cl`
```

[CONFIRMED — `objdump` `0x1d71b4`: `movabs $0x5851f42d4c957f2d,%r14`; `0x1d71be`: `movabs $0x14057b7ef767814f,%r15`; shifts 18/59/27 + `ror eax,cl` @ `0x1d7203`–`0x1d7215`.]

**Philox** is Philox-4x32-10 with the canonical Random123/cuRAND constants `M0=0xD2511F53`, `M1=0xCD9E8D57`, Weyl `W0=0x9E3779B9` (golden ratio), `W1=0xBB67AE85` (√5); 10 rounds are confirmed by the unrolled `lea` key-bumps for every `r×W0`/`r×W1` (r=1..9) and 40 `imul` (4/round). **LFSR** is a 32-bit Fibonacci LFSR with taps `{32,22,2,1}` read from rodata `0x5F2700` (poly `x³² + x²² + x² + x + 1`):

```c
// LFSR taps {32,22,2,1} @ 0x5F2700 (xxd = 20 00 00 00 16 00 00 00 02 00 00 00 01 00 00 00)
uint32_t b = (state>>31) ^ (state>>21) ^ (state>>1) ^ (state>>0);  // tap-1 bit index
state = (state << 1) | (b & 1);
output = state;
```

[CONFIRMED — taps `xxd`-verified; step disasm `0x1d6d82`.]

### 2.2 GEN-2 — `visitInstRand2` (IT 97): cuRAND XORWOW

Gen-2 uses cuRAND XORWOW. Per partition there are **4 `XorwowState` lanes** = 24 u32 = 96 bytes; default seed `{1,1,1,1,1,1}` (xmm `{1,1,1,1}` at `0x5F26F0`, the 5th/6th words set via `0x100000001` stores). The core step and the `[min,max)` conversion:

```c
uint32_t generateXorwowRandom(uint32_t s[6]) {     // @0x1aa180
    uint32_t t = s[0] ^ (s[0] >> 2);
    s[0]=s[1]; s[1]=s[2]; s[2]=s[3]; s[3]=s[4];
    s[4] = (s[4] ^ (s[4] << 4)) ^ (t ^ (t << 1));  // shift_a=2, shift_b=1, shift_c=4
    s[5] += 362437u;                               // Weyl counter, 0x587C5
    return s[5] + s[4];
}
float xorwowStep(uint32_t s[6], float lo, float hi) {       // @0x1aa1c0
    uint32_t r = generateXorwowRandom(s);
    float u = __builtin_bit_cast(float, (r & 0x7FFFFF) | 0x3F800000) - 1.0f;  // [0.0,1.0)
    return lo + (hi - lo) * u;                              // 1.0f @ 0x5F02C4
}
```

[CONFIRMED — Weyl `lea 0x587c5(%rdi),%edx` @ `0x1aa1a7` (`362437`); mantissa mask `and 0x7FFFFF` @ `0x1aa1e1`, exponent `or 0x3F800000` @ `0x1aa1e6`.] `Rand2`'s operands are `getMin` (operand[0] `ImmediateValue`, default `0.0`) and `getMax` (operand[1], default `1.0`); dst dtype is float32. The `is2x1p/2x2p/4x2p` AbleFlags choose which of the 4 lanes drives each output column — this selects which lane's state advances, not the PRNG itself.

> **QUIRK — `visitInstRand` produces RAW bits only.** The kernel reads *only* `Instruction+240` (algorithm). It does **not** read `+288` (distribution) or `+296` (params) or `+248` (rand_num_steps) — verified by exhaustive field-offset grep of the body. So `Uniform / Normal / Binomial` distribution transforms are **not implemented** in `libBIRSimulator`; the gen-1 kernel models the hardware RNG *bit-stream*, leaving distribution shaping to the upstream lowering. There is **no Box-Muller**, no inverse-CDF/erfinv, no binomial loop anywhere in the 6 RNG kernels. The *only* `[0,1)→affine` conversion is `Rand2`'s `xorwowStep` (a float32 `Uniform[min,max)`). [CONFIRMED — only `+0xF0` algorithm, operand lists, and IT field are read.]

### 2.3 RNGSTATE format and the MT19937-64 seed engine

The state tensor dtype is `uint32` (Dtype 14). `checkRandomSeedStateSize` @ `0x1b26d0` asserts the per-partition element count is `== 6` (gen-1) or `== 24` (gen-2 = 4 lanes × 6). The discriminator for which generation a get/set touches is `Instruction+0x90` — the `EngineType` field: `== 1 → gen-1` (`this+0xDB8`), `== 5 → gen-2 XORWOW` (`this+0xDD0`). On gen-1 restore, an all-zero `word[0..4]` forces `word[4]=1` (avoids a dead state); on gen-2 restore the 24-byte sub-blocks are transposed in DWORD order and each lane gets the same zero-guard.

`SetRandState` (IT 59) @ `0x1f8490` is the seed path. For `EngineType 5` (scalar seed) it seeds the embedded MT19937-64 via the standard 64-bit init recurrence:

```c
mt[0] = seed; uint64_t st = seed;       // @0x1f8490:99-110
for (int i = 1; i < 312; ++i) {
    mt[i] = 0x5851F42D4C957F2D * (st ^ (st >> 62)) + i;   // MT19937-64 init multiplier
    st = mt[i];
}
mti = 312;                              // NN = 312
```

For `EngineType 1` the seed arrives either as a tensor/AP (`doCopy` direct state copy) or as an 8-u32 `ImmediateArray` — the wire seed for gen-1 is **8 u32** (`word0 = (partition_idx<<24) | (imm[0] & 0xFFFFFF)`, `words[1..7]=imm[1..7]`), while the running state is the 6-u32 representation. `InstRng` (IT 100) routes to the base `visitInstruction` — sim-UNSUPPORTED, records the opcode name with no FATAL and executes no PRNG. [CONFIRMED.]

> **Cross-ref — the codegen-side discriminator.** The PCG/Philox `RandomAlgorithmKind` enum is *dead code* for KLR-originated randomness: per [7.29](codegen-dve-rng-control.md), `KlirToBirCodegen` lowers `klr::Rng → InstMemset(Random)` and `klr::Rand2 → InstRand2(XORWOW)`, never emitting `InstRand(60)` or `InstRng(100)`. The `codegenEngine {2,4,5,3,1,6}` table stamps the `EngineType` at `+0x90` that the get/set kernels above read back (`Pool=1`/`DVE=5`). A reimplementer wiring the gen-1 `InstRand` switch into the NKI path is implementing a code path KLR never reaches; `InstRand`/`InstRng` arrive only via direct BIR-JSON / HLO.

---

## 3. DVE search family (IT 88–91, 104) — the top-8 primitive set

The Max-8 family is the DVE "running top-8" hardware primitive set — the iterative-TopK / argmax / MoE-routing core. The simulator models four of the five opcodes; `NonzeroWithCount` is a sim-unsupported leaf.

| IT | Name | kernel | thunk | modeled? |
|---:|---|---|---|---|
| 88 | Max | `0x1f2690` | `0xede10` | YES |
| 89 | MaxIndex (FindIndex8) | `0x1f2b50` | `0xeb950` | YES |
| 90 | MatchReplace | `0x1f37c0` → helper | `0xebe20` | YES |
| 91 | MaxIndexAndMatchReplace | `0x1f37d0` → helper + idx | `0xebcd0` | YES |
| 104 | NonzeroWithCount | base `0x1aa750` | — | **NO** (skip-and-note) |

All four share a unifying execution model: resolve in/out APs via `getInOutPhysicalAP`, `Memory::read` (vtable `+0x18`) each input, `cast_to(fp32)` it (value comparison is *always* IEEE-754 single precision — bf16/fp16 widened first), build the output `MemoryObject` (dtype 16 fp32 for value outputs, dtype 14 uint32 for index outputs), loop per partition row, and `Memory::write` (vtable `+0x48`). The hard contract is **8 outputs per partition** (the silicon DVE lane width), with `8 ≤ data ≤ 16384` input elements per row. JSON-record-wise (per the wire schema), IT 88/89/104 are header-only; IT 90/91 carry exactly one key `imm_value` (the replacement sentinel) at `Inst+0xF0`. [CONFIRMED — `movss 0xf0(%rax),%xmm1` @ `0x1f347f`.]

### 3.1 Max (IT 88) — descending insertion sort into 8 slots

```c
// per partition row p: 8-slot buffer kept fully sorted descending
float*   slot  = &maxVals[p*8];       // 8 fp32 output slots
uint8_t  valid[8] = {0};              // v41 -- slot-initialized bitmask
for (i = 0; i < numDataPerPartition; ++i) {
    float carry = data[p_base + i];
    for (k = 0; k < 8; ++k) {
        if (!valid[k]) { slot[k] = carry; valid[k] = 1; break; }  // first empty slot
        if (carry > slot[k]) {        // STRICT '>' : comiss/jbe @0x1f28a7/0x1f28aa
            float demoted = slot[k];
            slot[k] = carry;          // new max into slot
            carry   = demoted;        // demoted value cascades down (movaps xmm0,xmm1)
        }
    }
}
// result: maxVals[0] >= maxVals[1] >= ... >= maxVals[7]
```

> **GOTCHA — strict `>` gives stable, earliest-index tie-break + sticky NaN.** A new value *equal* to an existing slot does **not** displace it, so the earliest (lowest-index) data element wins the slot — decisive for argmax determinism. `comiss` is unordered on NaN → `jbe` is taken (no swap): a NaN carry never displaces a slot, and a NaN in a slot is never displaced by a non-NaN (since `non-NaN > NaN` is false). **NaNs are sticky** in whichever slot first received them — an IEEE quirk inherited from the strict-`>` model. [CONFIRMED — disasm `0x1f28a7`/`0x1f28b0`.]

### 3.2 MaxIndex (IT 89) — one-to-one argmax index resolver

For each of the 8 `maxVals` (operand0, typically the `InstMax` output) find the data index of its first occurrence, with one-to-one assignment when `maxVals` contain duplicates:

```c
for (k = 0; k < 8; ++k) outIdx[k] = 0xFFFFFFFF;   // uint32 "not found" sentinel, re-written until matched
uint8_t matched[8] = {0};                          // v72 bitmask
for (i = 0; i < numDataPerPartition; ++i) {        // ascending data index
    float d = data[p_base + i];
    for (k = 0; k < 8; ++k) {
        if (matched[k]) continue;
        if (d == maxVals[k]) {        // ucomiss + jp(NaN-skip) + jnz(neq-skip) -> strict IEEE ==
            outIdx[k] = i; matched[k] = 1; break;  // one slot per data element
        }
    }
}
```

A `maxVal` that never appears in the data (or is NaN) leaves index `= -1 (0xFFFFFFFF)`. Equal `maxVals` are assigned to *successive first occurrences* (slot0 gets the earliest, slot1 the next), consistent with Max's stable tie-break. [CONFIRMED — `0x1f2e73`, output dtype `mov edx,0Eh`=uint32 @ `0x1f2c48`.]

### 3.3 MatchReplace (IT 90) and the fused MaxIndexAndMatchReplace (IT 91)

Both ride the shared helper `doInstMatchReplaceWithOptionalMaxIndex` @ `0x1f3250`; IT 90 passes `writeIndices=false`, IT 91 passes `true` plus an extra uint32 index output write. The op finds each `maxVal` in the data and strikes it out with the `imm_value` sentinel:

```c
float sentinel = *(float*)(inst + 0xF0);          // "imm_value" -- for TopK this is -inf
for (each partition row p) {
    uint8_t matched[n] = {0};                      // memset 0 @0x1f3509
    for (i = 0; i < numDataPerPartition; ++i) {
        float d = data[p_base + i];
        out[p_base + i] = d;                        // default: copy data through
        for (k = 0; k < n; ++k) {
            if (matched[k]) continue;
            if (d == maxVals[k]) {                   // ucomiss + setnp + cmovnz -> strict IEEE ==
                matched[k] = 1;
                out[p_base + i] = sentinel;          // REPLACE matched element
                if (writeIndices) idxOut[k] = i;     // IT91 only
                break;
            }
        }
    }
}
```

So IT 91 produces, in one pass, **both** the struck-out data (matched top-8 → `imm_value`) **and** the uint32 indices of those 8 matches. This is exactly the iterative-TopK round: extract where the current top-8 live and simultaneously remove them so the next `Max-8` pass yields ranks 9–16. The HLO `AwsNeuronTopK` lowers (via `LegalizeTopK`) into the NKI `router_topk`/`topk_reduce` path which emits `nisa.{max8, nc_find_index8, nc_match_replace8, nc_match_replace_indices8}` → BIR IT 88–91 → these kernels. Duplicate handling is one-to-one across all of 89/90/91, so a row with repeated maxima yields distinct indices and the iteration cannot livelock on a repeated value. [CONFIRMED — helper `0x1f3250`, sentinel replace `movss [r10],xmm1` @ `0x1f3642`, idx write @ `0x1f3638`.]

`NonzeroWithCount` (IT 104) has **no** simulator kernel — it dispatches to the base `visitInstruction` (a skip-and-note leaf shared with `InlineASMBytes(93)` and `Rng(100)`). Its intended "count + compact nonzero indices → `[idx…, -1…, count]`" semantics (pad `-1`) exist only in the NKI `find_nonzero_indices_with_count` kernel, not in the simulator; the only IT 104 functions present are `Hwm::getLatency*` timing models. [CONFIRMED.]

---

## 4. Collective family (IT 48–50) — a full N-rank registry model

The three top-level collective opcodes route from the 110-case switch to three kernels: `visitInstCollectiveCompute` @ `0x1ed9b0` (IT 48), `visitInstCollectiveSend` @ `0x1eda20` (IT 49), `visitInstCollectiveRecv` @ `0x1edba0` (IT 50).

`visitInstCollectiveCompute` is itself a **second-level switch on `CollectiveKind`** at `InstCollectiveCompute+0xF8`, tail-calling a per-kind handler — so the sim has a *separate numeric model per kind*, not one generic collective:

```c
switch (*(int*)(inst + 0xF8)) {        // cmpl $0xa,0xf8(%rsi); ja default; jmp *rax @0x1ed9b0
    case 0:  doCollectiveSendRecv;             break;   // SendRecv          @0x1e4ad0
    case 1:  doCollectiveSendRecvCCE;          break;   // SendRecvCCE       @0x1e59e0
    case 2:  doCollectiveAllReduce;            break;   // AllReduce         @0x1e6ff0
    case 3:  doCollectiveReduceScatter;        break;   // ReduceScatter     @0x1e94f0
    case 4:  doCollectiveAllGather;            break;   // AllGather         @0x1e8580
    case 5:  doCollectiveAllToAll;             break;   // AllToAll          @0x1e2b60
    case 6:  doCollectiveAllToAllV;            break;   // AllToAllV         @0x1e3f70
    case 7: case 9: doCollectivePermute;       break;   // Permute / Implicit @0x1eaaf0
    case 0xA: doCollectivePermuteReduceImplicit; break; // (kind 10)         @0x1ebd10
    default:  doCollectiveCompute;             break;   // PermuteReduce(8) + unknown -> identity @0x1ecd80
}
```

[CONFIRMED — `objdump` `0x1ed9b0`: `cmpl $0xa,0xf8(%rsi)` / `ja` / `mov 0xf8(%rsi),%eax` / `jmp *%rax`.]

> **QUIRK — `PermuteReduce` (kind 8) has no reduce model.** Kinds 7 (Permute) and 9 (PermuteImplicit) share the `doCollectivePermute` arm; kind 8 falls into the `default` arm and is modeled as a plain **identity copy** (`in.numElems == out.numElems` assert, copy-through). Only `PermuteReduceImplicit` (10) has a real reduce. So in this build `PermuteReduce(8)` is a no-reduce stub — possibly because the verifier forbids kind 8 in sim inputs, but the sim itself would silently identity-copy it. [CONFIRMED from the case table.]

### 4.1 The rank / topology model

> **HEADLINE — the sim is a full N-rank model with two regimes, NOT a single-rank stub.** A single `birsim::NeuronCoresManager` (the "LNC manager", held at `InstVisitor+0x140`) keeps a per-core registry of every participating core's collective instruction and its input `MemoryObject`. Each reducing/gathering kernel literally fetches every rank's real input buffer (`getCcOpInMemObjForCore`) and folds/concatenates them — peers are not stubbed with zeros.

The two regimes are selected by `NeuronCoresManager::isMultiWorkerSimTest()` @ `0x272ae0` (`return numCoresPerLNC <= 1 && numWorkers > 1`):

- **(A) Single-process multi-core** (`numCoresPerLNC > 1`): all cores live in one process; the kernel walks the real per-core `MemoryObject`s → **bit-exact** collective.
- **(B) Multi-worker sim test** (`numCoresPerLNC == 1 && numWorkers > 1`): each worker process owns one rank; the `AllReduce`-average degenerates to a **local approximation** (`res = local_input × numReplicas`).

The rank arithmetic is `rank-of-core = core / numCoresPerLNC` (`getRankIdforCore` @ `0x272380`); `numReplicas = replica_groups[0].size()`; participation is gated by `isInReplicaGroup(I, core)` @ `0x1aa460` (reads the `I+0x100/+0x108` flattened u32 rank list; empty list → all ranks participate). The shared scaffolding every reducing kind uses:

```c
curCore   = *((u64*)this + 28);                    // InstVisitor+0xE0
mgr       = *((NeuronCoresManager**)this + 40);    // InstVisitor+0x140
ccIdx     = mgr->getCollectiveComputeInstIdx(curCore, I);  // per-core op slot
numReplicas = (replica_groups[0].end - .begin) >> 2;
res = MemoryObject(out_AP.getShape());             // fp32 accumulator
for (core = 0; core < numReplicas; ++core) {
    mob = mgr->getCcOpInMemObjForCore(core, ccIdx, apIdx, /*isInput=*/1);  // @0x272aa0
    ins.push_back(mob.cast_to(/*fp32*/16));        // every rank widened to fp32
    assert(ins.back().num_elements() == res.num_elements());
}
// <kind-specific fold/route> ; write res -> out_AP via vtable+72
```

`getCollectiveComputeInstIdx` @ `0x273e50` returns the single-worker sentinel `0x7FFFFFFF` when `numWorkers <= 1`; otherwise it maintains a per-core `DenseMap<Instruction*, idx>` so each core's k-th collective op lines up across cores. Multi-LNC (`numReplicas != numCoresPerLNC` or `replica_groups.size() > 1`) is explicitly **unsupported** for `ReduceScatter`/`SendRecv`/`SendRecvCCE` (`"multiple LNCs aren't yet supported"` / `"Multiple LNCs are not supported yet"`); single-LNC intra-core collective is the fully-modeled case.

### 4.2 The reducer — `doReduction` @ `0x1b3400`

`AllReduce`, `ReduceScatter`, `SendRecvCCE`, and `PermuteReduceImplicit` all funnel their per-rank fp32 inputs into one reducer. It reads `op = AluOpType` at `inst+0x180` and folds element-wise:

```c
op = *(int*)(I + 0x180);              // cmpl $0x4,0x180(%rbx) @0x1b3444
if (op == 4 /*add*/ && scale.has_value()) {           // AVERAGE / SCALE branch
    res[p][i] = ins[0][p][i] * (float)scale;          // scales ONLY the first input
} else {                                               // FOLD branch
    res = ins[0];                                      // copy 1st input
    for (k = 1; k < ins.size(); ++k)
      for (p, i) switch (op) {                          // op re-read each element
        case 4: res[p][i] += ins[k][p][i];             break;        // SUM
        case 8: res[p][i]  = fmaxf(ins[k][p][i], res[p][i]); break;  // libm fmaxf
        case 9: res[p][i]  = fminf(ins[k][p][i], res[p][i]); break;  // libm fminf
        default: NeuronAssertion(false);  // inst_visitor.cpp:5365 "Unsupported OP type"
      }
}
```

[CONFIRMED — `op@[rbx+180h]` @ `0x1b3444`; `MemoryObject` stride `0x70` recovered via the `×0x6DB6DB…B7` (÷112) loop bound.] Only `add(4)/max(8)/min(9)` are accepted — exactly the HLO `AwsNeuron<Coll><RedOp>` `RedOp∈{Add,Max,Min}` producer set; mult/average/bitwise abort.

> **GOTCHA — the multi-worker `AllReduce` is a local `×N` approximation.** In regime (B) the `AllReduce` kernel sets an "average" flag and threads `scale = numReplicas` into `doReduction`, which takes the SCALE branch: `res = ins[0] × numReplicas`. Since each worker only holds its own rank's buffer, this is a *local approximation* of "sum of N equal replicas", **not** a true cross-worker sum. [CONFIRMED — branch `0x1e7295`, optional push `0x1e72e7`–`0x1e732e`.] Note also that the collective `min`/`max` uses **libm `fmaxf`/`fminf`** (NaN-quieting `minNum`/`maxNum`-style) — distinct from the scatter-RMW `minss`/`maxss` SSE core in [7.35](sim-core-arithmetic.md); the two have different NaN semantics, and the collective path is the only one that executes min/max for a collective.

### 4.3 Per-kind routing and the rank-id opcodes

The non-reducing kinds are pure data routing. `AllGather` (kind 4) `memcpy`s each rank's slice into its output offset (output is N× the per-rank input; `cc_dim@+0x27C != 1` picks per-partition vs flat concatenation). `AllToAll` (kind 5) splits each rank's buffer into N chunks and `memcpy`s peer `p`'s chunk-for-this-rank locally; `AllToAllV` (kind 6) is the variable-chunk version. `Permute` (7/9) routes rank `r`'s output ← the source rank named by `src_target_pairs` (kind 7) or `replica_groups` (kind 9). `SendRecv` (kind 0) is a point-to-point routed copy: this rank's output ← the peer's input (peer resolved by a `QuasiAffineExpr` over the `+0x100` pair). `SendRecvCCE` (kind 1) gathers the recv-from ranks and folds via `doReduction` (the on-chip CCE recv+reduce). `CollectiveSend`/`Recv` (IT 49/50) are separate point-to-point primitives carrying only `peer_id`, routed through the same registry. [HIGH — routing handlers traced structurally; the IT 49/50 primitive bodies enumerated at the registry call-site, not line-by-line.]

The rank-id opcodes assign ranks: `GetGlobalRankId` (IT 11) @ `0x1ba670` writes the precomputed `InstVisitor+0xDE8` (ctor-initialized to 0; a driver sets non-zero) into the output register. `GetCurProcessingRankID` (IT 66) @ `0x1bb1f0` maps `(iter_id, channel_id, replica_group, ArchLevel)` → physical TP rank via a *global permutation table* `qword_22969A0` (`unordered_map<ArchLevel, unordered_map<u32 groupSize, vector<vector<int>>>>`, indexed `table[archLevel][groupSize][iter_id][col]`), modeling the TP rank-remapping the runtime applies. [HIGH — `sub_1BA6A0` index-formula traced; the table *populator* not traced.]

---

## 5. DMA family (IT 19/22/23/32, 67–72, 107) — AP-driven element copy

> **The universal DMA primitive is `read-source-AP → MemoryObject → write-dest-AP`.** There is no separate descriptor-engine state machine: the descriptor list is just a flat sequence of `DMADescriptor` instructions re-fed through the *same* opcode dispatch.

The executor is the `birsim::Memory*` at `InstVisitor+0x138`. The `Memory` vtable (`_ZTVN6birsim6MemoryE` @ `0x2276370`, slots vptr-relative) exposes `read` (`+0x18`) `→ 0x17a420`, `readIndirect` (`+0x38`/gather) `→ 0x16daa0`, `write` (`+0x48`) `→ 0x179fd0`, `writeIndirect` (`+0x60`/scatter+RMW) `→ 0x16db80`. The multi-dim `[W,Z,Y,X]` walk delegates to `MemoryObject::runAP` @ `0x298390`:

```c
// runAP: 1. linearizedElementIntervals (libBIR import) flattens [W,Z,Y,X] APPair list
//           -> contiguous byte intervals [start..end]
//        2. addr = base + (off % partStride) + partStep*(off / partStride)
//        3. normal element: memcpy(dst, src, chunkLen)                 // bulk contiguous
//        4. ACCUMULATE element (src opcode 95 or 7..9 AND dst flag@+72):
//           cast_copy_or_accumulate(seen?, dst+i, src+i, dtype)        // PSUM-accumulate RMW
```

The accumulate path is the PSUM-reuse of the shared `cast_copy_or_accumulate` core from [7.35](sim-core-arithmetic.md). `runAP` asserts `AP.dtype == MemObj.dtype` and bounds-checks the offset. A DMA with differing src/dst dtypes casts the source via `Memory::writeLocal`'s `cast_and_reshape` before `writeAP`, and a uninit-NaN guard fires on write (`canInstReadFromUninitMem(&I) || !hasNaN()`). Remote (cross-core) DMA is a property of the AP's `MemoryLocation::isRemote` → routed to `NeuronCoresManager` — collective DMA and auto-sharded cross-core copies use the *same* read/write primitive.

> **The sim models SW-DGE vs HW-DGE by AP manifestation, not the `DGEType` enum.** No DMA kernel reads `InstDMA+0xF8` (DGEType) — that field is consumed by the verifier/codegen. Instead: **SW-DGE** = an explicit descriptor list (`DMATrigger → DMABlock → descriptors`) that the sim executes by iterating and re-dispatching; **HW-DGE** = a dynamic-offset AP detected by `isVectorDynamicOffsetDMA` / `isScalarDynamicOffsetDMA`, for which the sim *software-materializes* the per-element address vector (`getIndirectAddrsVector` @ `0x1db900`) and feeds it to `readIndirect`/`writeIndirect`. [CONFIRMED — no `getDGEType`/`+0xF8` read in any DMA body.]

`visitInstDMACopy` (IT 32) @ `0x20b7b0` is the mode multiplexer, reading `CopyMode` at `inst+0x128`:

```c
int mode = *(int*)(inst + 0x128);     // mov 0x128(%rsi),%eax @0x20b7c7
switch (mode) {
    case 1: doDMATranspose(inst);  return;   // axis perm over WZYX labels (@0x1dd3b0)
    case 3: doDMAReplicate(inst);  return;   // broadcast 1 src across dst partitions (@0x20b430)
    case 2: doDMACCE(inst);        return;   // N-input reduce add/min/max -> write (@0x1da0c0)
    case 0:                                  // Copy
        if (!isVectorDynamicOffsetDMA(inst)) { doCopy(inst); return; }   // plain AP->AP
        /* else: VECTOR-INDIRECT (HW-DGE scatter) — getIndirectAddrsVector +
           readIndirect/writeIndirect, with isDstReduceDGE selecting the
           accumulate-on-write reduce op = AluOpType2MemoryReduction(inst+0x178) */
}
```

[CONFIRMED — `mov 0x128(%rsi),%eax` @ `0x20b7c7`.] `doDMACCE` is the collective-compute-engine in-flight reduce: it casts N source APs to fp32, then folds `op∈{add=4, max=8, min=9}` (via `fminf`/`fmaxf`) across them into a zeroed fp32 accumulator. The plain-copy core `doCopy` @ `0x1d2660` (`src = getInOutPhysicalAP(0,0)`, `dst = getInOutPhysicalAP(0,1)`, `read → write`) is shared by **all** plain copy ops: `Load(19)`, `Save(22)`, `TensorCopy(23)`, `GenericCopy`, `AbstractCopy(3)`, and `DMACopy` mode 0 — every plain copy is an AP→AP element copy through the universal primitive.

The SW-DGE descriptor machinery: `DMATrigger` (IT 67) @ `0x2124a0` is a *cursor over its block list* — it reads a per-trigger fire counter (`InstVisitor+3680` `DenseMap`), fires the `id`-th `DMABlock`, and advances round-robin. `DMABlock` (IT 107) @ `0x212420` iterates its descriptor list and re-feeds each `DMADescriptor` (IT 68–72) through the main dispatcher — so descriptor execution reuses the same kernels as inline DMA. The TREE2 descriptor visitors (`DMADescriptorCopy/CCE/Transpose/Replicate`, IT 69–72) thunk to the same `do*` bodies as the IT 32 mode-switch. Finally `TensorLoad`/`TensorSave` are *not* bulk DMA: they move a single scalar (or a memory pointer) between memory and a register, used for dynamic offsets and loop bounds. [CONFIRMED — full kernel→behavior map traced.]

---

## 6. Function map

| Family | Kernel | Addr | Confidence |
|---|---|---|---|
| BN | `visitInstBNStats` (34) | `0x1df7b0` | CONFIRMED |
| BN | `visitInstBNStatsAggregate` (35) | `0x1dfcb0` | CONFIRMED |
| BN | `visitInstBNGradients` (36) | `0x1dfff0` | CONFIRMED |
| BN | `visitInstBNBackprop` (37) | `0x1e0560` | CONFIRMED |
| BN | `visitInstBNBackprop2` (38) | `0x1e0ab0` | CONFIRMED |
| RNG | `visitInstRand` (60) | `0x1d6290` | CONFIRMED |
| RNG | `visitInstRand2` (97) | `0x1d2fa0` | CONFIRMED |
| RNG | `generateXorwowRandom` | `0x1aa180` | CONFIRMED |
| RNG | `xorwowStep` | `0x1aa1c0` | CONFIRMED |
| RNG | `visitInstSetRandState` (59) | `0x1f8490` | CONFIRMED |
| RNG | `visitInstRandSetState` (98) | `0x1d4360` | CONFIRMED |
| RNG | MT19937-64 seed engine | `0x237990` | CONFIRMED |
| Search | `visitInstMax` (88) | `0x1f2690` | CONFIRMED |
| Search | `visitInstMaxIndex` (89) | `0x1f2b50` | CONFIRMED |
| Search | `doInstMatchReplaceWithOptionalMaxIndex` | `0x1f3250` | CONFIRMED |
| Search | `visitInstMatchReplace` (90) | `0x1f37c0` | CONFIRMED |
| Search | `visitInstMaxIndexAndMatchReplace` (91) | `0x1f37d0` | CONFIRMED |
| Collective | `visitInstCollectiveCompute` (48) | `0x1ed9b0` | CONFIRMED |
| Collective | `doCollectiveAllReduce` | `0x1e6ff0` | CONFIRMED |
| Collective | `doReduction` | `0x1b3400` | CONFIRMED |
| Collective | `isInReplicaGroup` | `0x1aa460` | CONFIRMED |
| Collective | `NeuronCoresManager::getRankIdforCore` | `0x272380` | CONFIRMED |
| Collective | `NeuronCoresManager::getCcOpInMemObjForCore` | `0x272aa0` | CONFIRMED |
| DMA | `visitInstDMACopy` (32) | `0x20b7b0` | CONFIRMED |
| DMA | `doCopy` | `0x1d2660` | CONFIRMED |
| DMA | `doDMACCE` | `0x1da0c0` | CONFIRMED |
| DMA | `MemoryObject::runAP` | `0x298390` | CONFIRMED |
| DMA | `getIndirectAddrsVector` | `0x1db900` | CONFIRMED |
| DMA | `visitInstDMATrigger` (67) | `0x2124a0` | CONFIRMED |
| DMA | `visitInstDMABlock` (107) | `0x212420` | CONFIRMED |

## 7. Adversarial verification ceiling

Five strongest claims were re-checked directly against the cp310 binary (md5 `f3acdcba9176056cb50daac01389dd13`) this pass and hold byte-exact: (1) `BNStats` prologue `41 57 31 d2 31 c9 41 56` @ `0x1df7b0` and the `1.0f` count-step `00 00 80 3f` @ `0x5f02c4`; (2) PCG32 `movabs $0x5851f42d4c957f2d,%r14` / `$0x14057b7ef767814f,%r15` @ `0x1d71b4`; (3) XORWOW Weyl `lea 0x587c5(%rdi),%edx` @ `0x1aa1a7` (`362437`) and the LFSR taps `{32,22,2,1}` @ `0x5F2700`; (4) the collective kind switch `cmpl $0xa,0xf8(%rsi); ja; jmp *%rax` @ `0x1ed9b0` and `doReduction`'s `cmpl $0x4,0x180(%rbx)` @ `0x1b3444`; (5) the DMA mode read `mov 0x128(%rsi),%eax` @ `0x20b7c7` and the MatchReplace sentinel `movss 0xf0(%rax),%xmm1` @ `0x1f347f`.

The numeric models, op-sets, dispatch offsets, and PRNG constants are **CONFIRMED**. The residual inferences (tagged inline) are: the BN backprop coefficient → textbook `{γ, invstd, Σdy, Σ(x−μ)dy}` mapping (HIGH/INFERRED — the sim sees opaque fp32 coefficients; the fold is set by the walrus codegen); the gen-1 6-u32 word→`Philox key/counter` assignment (MED — decompiler-fused); the multi-worker (B) cross-worker buffer-exchange transport (the local `×N` arithmetic is CONFIRMED, but *how* a separate worker's `MemoryObject` becomes visible is unpinned — `isMultiWorkerSimTest` suggests a test harness); and the `qword_22969A0` TP-rank table populator (HIGH on the index formula, untraced on init).

## 8. Cross-references

- [7.34 Sim Dispatch & State](sim-dispatch-state.md) — the 110-case visitor, `Memory`/`SyncState`/`RegState`, and the `NeuronCoresManager` these kernels mutate.
- [7.35 Sim Core Arithmetic](sim-core-arithmetic.md) — the shared `cast_copy_or_accumulate` RMW core (the DMA-accumulate path), per-dtype widen/narrow, and the `minss`/`maxss` scatter-RMW contrasted with the collective `fmaxf`/`fminf`.
- [7.29 Codegen of DVE/RNG/Control](codegen-dve-rng-control.md) — how KLR lowers `Rng→Memset(Random)` and `Rand2→XORWOW`, and the `codegenEngine {2,4,5,3,1,6}` table that stamps the `EngineType` the RNG get/set kernels read.
- [dtype tables](dtype-tables.md) — Dtype 14 (uint32) / 16 (float32) and the byte-stride vector.
