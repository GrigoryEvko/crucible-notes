# Sim Core Arithmetic: cast / accumulate / RNE

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 share this binary). The arithmetic core lives in `neuronxcc/starfish/lib/libBIRSimulator.so`. For `.text`/`.rodata` the virtual address equals the file offset (`readelf -S`: `.text [12]` Addr `0xf1ad0` == Off `0xf1ad0`; `.rodata [14]` Addr `0x58f000` == Off `0x58f000`) — `xxd`/`objdump` at the VA reads the datum directly. `.data` carries a +0x1000 delta and is not used here. Other wheels differ; treat every address as version-pinned.*

## Abstract

When the BIR simulator executes a scatter, an indirect copy, or an embedding-bag accumulate, every output element passes through one 1055-byte kernel: `birsim::cast_copy_or_accumulate` @ `0x28ff00`. It is the simulator's numerical-fidelity floor — the function that decides, byte for byte, what `dst = reduce(dst, src)` *means* for each of the 20 BIR dtypes. This page recovers that kernel: its two-level dispatch (a copy fast-path plus a per-dtype reduce jump table), the exactly **8 dtypes** that support reduction, the **WRAP-vs-IEEE** split between integer and float accumulation, the hard **round-to-nearest-even** every float narrow performs, and one decisive correction — the `MemoryReductionOp` enum's **MIN/MAX numbering was previously documented reversed**, and the binary proves it three independent ways.

The kernel does *not* re-implement dtype conversion; the per-element widen/narrow it calls (fp8/fp16/bf16 ↔ fp32) is the same family of decoders/encoders the whole-tensor cast engine `bir::CastToNewDType` uses. This page documents the *reduction* layer — the dispatch, the ALU cores, the integer wrap, and the float rounding it applies *around* those conversions — and cross-references [9.3 Cast to New Dtype](../numerics/cast-to-new-dtype.md) for the conversion LUTs themselves, which it does not re-tabulate. The simulator state model the kernel reads and mutates is in [7.34 Sim Dispatch & State](sim-dispatch-state.md).

For a reimplementer the contract is:

- The `cast_copy_or_accumulate(op, dst, src, dt)` dispatch: a `memcpy` copy path keyed by `COPY_SIZE_LUT[dt]`, and an `op != 0` path keyed by a 17-entry relative jump table `REDUCE_JT[dt-3]`.
- The **8** reducing dtypes — `{E3M4, E4M3, E5M2}` (fp8) ∪ `{bf16, fp16}` ∪ `{int32, float32, int64}` — and the 12 that throw `cast_accumulate: unsupported dtype`.
- **WRAP vs IEEE**: integer add is 2's-complement wrap with no saturation; float reduce is IEEE through an SSE ALU core (`addss`/`minss`/`maxss`).
- The `MemoryReductionOp` numbering `{copy=0, add=1, MIN=2, MAX=3}` and the converter (`AluOpType2MemoryReduction`) that produces only `copy` and `add`, throwing on min/max.

| | |
|---|---|
| **Apply kernel** | `birsim::cast_copy_or_accumulate(MemoryReductionOp, uint8* dst, uint8* src, bir::Dtype)` @ `0x28ff00` (1055 B) |
| **AluOp → ReductionOp** | `birsim::AluOpType2MemoryReduction(bir::Instruction const*, bir::AluOpType)` @ `0x1b0ed0` (2112 B) |
| **fp32 ALU core** | `sub_28f700` (`op1`=`addss`, `op2`=`minss`, `op3`=`maxss`) |
| **Copy size table** | `COPY_SIZE_LUT` @ `0x5f7760` (20 × u64 stride) |
| **Reduce jump table** | `REDUCE_JT` @ `0x5f7694` (17 × int32, table-relative) |
| **Unsupported throw** | string `0x5c9608` `"cast_accumulate: unsupported dtype "`; cold block `0x110548` |

## Dispatch: two paths off the `op` argument

The kernel's first argument is `birsim::MemoryReductionOp` — a **compact 4-value** enum `{copy=0, add=1, MIN=2, MAX=3}`, *not* the wide `bir::AluOpType` and *not* the PSUM `EngineAccumulationType`. The very first branch splits copy from everything else (`objdump`, `0x28ff00`):

```c
// birsim::cast_copy_or_accumulate(op /*edi*/, dst /*rsi->r12*/, src /*rdx->rsi*/, dt /*ecx*/)
// 0x28ff06  mov %rsi,%r12        // dst -> r12
// 0x28ff09  mov %rdx,%rsi        // src -> rsi
if (op == 0) {                          // 0x28ff11 test %edi,%edi ; 0x28ff13 jne 0x28ff40
    if ((unsigned)dt > 0x13)            // 0x28ff15 cmp $0x13,%ecx ; ja
        throw_unknown_dtype();          //   "Unknown dtype" (Dtype.h:50, sub_5725C0)
    size_t n = COPY_SIZE_LUT[dt];       // 0x28ff2a mov (%rax,%rcx,8),%rdx ; rax=0x5f7760
    return memcpy(dst, src, n);         // 0x28ff39 tail-jmp memcpy@plt
}
// op != 0  (add / MIN / MAX)
int idx = (int)dt - 3;                  // 0x28ff40 lea -0x3(%rcx),%eax
if ((unsigned)idx > 0x10)               // 0x28ff43 cmp $0x10,%eax ; 0x28ff46 ja 0x110548
    throw_unsupported_dtype(dt);        //   "cast_accumulate: unsupported dtype <name>"
void* jpt = &REDUCE_JT;                 // 0x28ff4c lea 0x5f7694(%rip),%rdx
goto *(jpt + (int32_t)REDUCE_JT[idx]);  // 0x28ff55 movslq (%rdx,%rax,4),%rax ; add %rdx,%rax ; jmp
```

Two structural facts fall out. **Copy supports all 20 dtypes** — it is a `memcpy` of `COPY_SIZE_LUT[dt]` bytes, gated only by `dt <= 0x13`. The reduce path is far narrower: it first rebases `dt-3` (the smallest reducible dtype, `E3M4`, is dtype 3), bounds-checks against 16, then performs a table-relative computed jump. [CONFIRMED — disasm `0x28ff00`–`0x28ff5c`, both `lea`-relative table bases read directly off the instruction stream.]

`COPY_SIZE_LUT` @ `0x5f7760` is the dtype byte-stride table (`xxd` verified):

```
005f7760: {1,1,2,1, 1,1,1,1, 4,4,2,2, 2,2,4,4, 4,4,8,8}   // u64 each, dtypes 0..19
```

This is the same stride vector the BIR dtype tables carry ([dtype-tables](dtype-tables.md)); copy emits exactly one element of the dtype's width. [CONFIRMED — `xxd -s 0x5f7760 -l 160`.]

## The 8 reducing dtypes

`REDUCE_JT` @ `0x5f7694` is 17 `int32` entries; the jump target is `0x5f7694 + entry`. Nine entries equal `0xffb18eb4`, which resolves to `0x110548` — the cold throw block. The remaining eight route to a per-dtype handler. [CONFIRMED — `xxd -s 0x5f7694 -l 68`; throw entries decode to `0x110548` by inspection regardless of any base ambiguity, and the dispatch head reads `jpt = 0x5f7694` directly.]

| idx | dtype | name | target | handler | reduce? |
|----|----|----|----|----|----|
| 0 | 3 | E3M4 (fp8) | `0x290190` | fp8 stub (decode `0x2a3f80`, encode `0x2a36e0`) | ✅ |
| 1 | 4 | E4M3 (fp8) | `0x290140` | fp8 stub (decode `0x2a4050`, encode `0x2a38d0`) | ✅ |
| 2 | 5 | e4m3fn | `0x110548` | THROW | ✗ |
| 3 | 6 | e8m0fnu | `0x110548` | THROW | ✗ |
| 4 | 7 | E5M2 (fp8) | `0x2900f0` | fp8 stub (decode `0x2a41e0`, encode `0x2a3cf0`) | ✅ |
| 5 | 8 | fp8_e4m3_x4 | `0x110548` | THROW | ✗ |
| 6 | 9 | fp8_e5m2_x4 | `0x110548` | THROW | ✗ |
| 7 | 10 | uint16 | `0x110548` | THROW | ✗ |
| 8 | 11 | int16 | `0x110548` | THROW | ✗ |
| 9 | 12 | bf16 | `0x2900a0` | bf16 stub (decode `0x2a42d0`, encode `0x2a42e0`) | ✅ |
| 10 | 13 | fp16 | `0x28ffd0` | fp16 INLINE | ✅ |
| 11 | 14 | uint32 | `0x110548` | THROW | ✗ |
| 12 | 15 | int32 | `0x28ffb0` | int32 inline (`pminsd`/`pmaxsd`/add) | ✅ |
| 13 | 16 | float32 | `0x28ff90` | tail-jmp fp32 ALU `sub_28f700` | ✅ |
| 14 | 17 | float32r | `0x110548` | THROW | ✗ |
| 15 | 18 | uint64 | `0x110548` | THROW | ✗ |
| 16 | 19 | int64 | `0x28ff60` | int64 inline (`cmovg`/`cmovl`/add) | ✅ |

**Exactly 8 dtypes reduce**: `{3 E3M4, 4 E4M3, 7 E5M2}` ∪ `{12 bf16, 13 fp16}` ∪ `{15 int32, 16 float32, 19 int64}`. The other 12 hit `0x110548`, which allocates a `std::runtime_error` carrying `"cast_accumulate: unsupported dtype "` (string @ `0x5c9608`) concatenated with `bir::Dtype2string(dt)`. [CONFIRMED — REDUCE_JT byte decode (9 throw entries / 8 live handlers); throw string `xxd -s 0x5c9608`: `cast_accumulate: unsupported dty pe`.]

> **GOTCHA — `float32r` (dtype 17) cannot be accumulated in the simulator.** `REDUCE_JT[14] = 0x110548` → throw. `float32r` is the PE array's reduced-precision FP32 accumulation format, yet a scatter-add targeting a `float32r` buffer aborts with `unsupported dtype`. Copy of `float32r` works (it has a `COPY_SIZE_LUT` entry of 4); only the ALU path rejects it. The same applies to every integer narrower than 32 bits (`int8/16`, all `uint`), the OCP `e4m3fn`/`e8m0fnu`, and the x4-packed fp8/fp4 dtypes. [CONFIRMED.]

## WRAP vs IEEE: the integer/float split

This is the first headline behavior. **Integers accumulate by 2's-complement wrap; floats accumulate by IEEE through an SSE core.** There is no saturating add anywhere in the reduce path.

### int32 — dtype 15, inline @ `0x28ffb0`

```c
switch (op) {
    case 1: *(int32_t*)dst += *(int32_t*)src;            break; // 0x28ffc9 add %eax,(%r12) -> WRAP
    case 2: *dst = _mm_min_epi32(*dst, *src);            break; // 0x29026a pminsd  (signed MIN)
    case 3: *dst = _mm_max_epi32(*dst, *src);            break; // 0x2901ea pmaxsd  (signed MAX)
}
```

The add is a plain `add DWORD[r12],eax` — overflow wraps mod 2³², no clamp. `pminsd`/`pmaxsd` are signed. [CONFIRMED — `objdump 0x28ffb0`: `cmp $0x2,%edi; je 0x290260` (pminsd), `cmp $0x3,%edi; je 0x2901e0` (pmaxsd), `cmp $0x1,%edi; add %eax,(%r12)`.]

### int64 — dtype 19, inline @ `0x28ff60`

```c
switch (op) {
    case 1: *(int64_t*)dst += *(int64_t*)src;                 break; // 0x290203 add %rax,(%r12) -> WRAP
    case 2: { int64_t a=*dst; if (a > *src) a=*src; *dst=a; } break; // 0x29024a cmovg  -> MIN
    case 3: { int64_t a=*dst; if (a < *src) a=*src; *dst=a; } break; // 0x29021a cmovl  -> MAX
}
```

Signed 64-bit compare via `cmov`; add wraps mod 2⁶⁴. [CONFIRMED — `objdump 0x290200`–`0x290252`.]

### float32 — dtype 16, tail-jmp @ `0x28ff90` → `sub_28f700`

The fp32 case forwards into a shared four-way ALU core (`op0` is a diagnostic/ostream path, unused on the reduce path):

```c
// sub_28f700(op /*edi*/, acc /*rsi*/, x /*rdx*/)
switch (op) {
    case 1: *acc = addss(*acc, *x);  break; // 0x28f718 movss(%rsi); addss(%rdx); movss->(%rsi)
    case 2: *acc = minss(*x, *acc);  break; // 0x28f904 movss(%rdx); minss(%rsi); movss->(%rsi)
    case 3: *acc = maxss(*x, *acc);  break; // 0x28f731 movss(%rdx); maxss(%rsi); movss->(%rsi)
}
```

[CONFIRMED — `objdump 0x28f700`: `cmp $0x2,%edi; je 0x28f900`; `jg 0x28f728` then `cmp $0x3` → `maxss`; `cmp $0x1` → `addss`.]

> **GOTCHA — SSE `minss`/`maxss` are not IEEE `minNum`/`maxNum`.** On a NaN operand or an exact tie, x86 `minss`/`maxss` return the **second** source operand. Here both encode `movss (x); {min,max}ss (acc); movss → (acc)` — i.e. the second operand is `acc` (= `dst`). So `reduce(dst, NaN)` keeps `dst`, and a NaN already in `dst` is *replaced* by `src` only if `src` is not NaN. This differs from a `minNum`/`maxNum` implementation that always propagates the non-NaN value symmetrically. [CONFIRMED — operand order read off the `movss` sources at `0x28f72d`/`0x28f900`.]

| dtype | add overflow | NaN under min/max |
|----|----|----|
| int32 (15) | 2's-comp **WRAP** | n/a |
| int64 (19) | 2's-comp **WRAP** | n/a |
| float32 (16) | IEEE (Inf via SSE) | `minss`/`maxss` → 2nd op (= `dst`) |
| bf16 (12) | no clamp; round-carry → Inf | encode → `0x7fc0` canonical qNaN |
| fp16 (13) | → fp16 Inf `0x7c00` (IEEE) | encode → `0x7e00` canonical qNaN |
| fp8 3/4/7 | config=0 → Inf | → canonical NaN |

## Float narrow: hard round-to-nearest-even

Every reducing float dtype narrower than fp32 (fp8, bf16, fp16) computes its reduction in fp32 and re-narrows with a **hard round-to-nearest-even**. There is no rounding-mode field — RNE is wired into each encoder. fp32 itself rounds via the SSE/MXCSR default (also RNE).

**fp8 (dtypes 3/4/7), stubs @ `0x290190`/`0x290140`/`0x2900f0`** — a four-step decode→ALU→encode:

```c
// E3M4 stub @0x290190 (E4M3/E5M2 identical shape)
fp8_decode(&tmpA, src, 1);          // 0x2901a2 call 0x2a3f80  widen src fp8 -> fp32
fp8_decode(&tmpB, dst, 1);          //          call decode    widen dst fp8 -> fp32
sub_28f700(op, &tmpB, &tmpA);       // 0x2901bf call 0x28f700  tmpB = ALU(op, tmpB, tmpA)
fp8_encode(dst, &tmpB, 1, /*cfg=*/0);// 0x2901c4 xor %ecx,%ecx ; call 0x2a36e0  narrow RNE
```

The encoder's RNE adds a `1<<(MANT_SHIFT-1)` round-bias plus guard/round/sticky/odd correction before the mantissa shift. The decode/encode byte-tables (exponent rebias, mantissa shift, denormal hidden-bit, Inf/NaN codes) belong to the shared conversion layer and are tabulated in [9.3 Cast to New Dtype](../numerics/cast-to-new-dtype.md); this page does not duplicate them.

> **GOTCHA — fp8 reduce hard-codes `FP8Config = 0`.** Each fp8 stub clears the config argument (`xor %ecx,%ecx` @ `0x2901c4`/`0x290174`/`0x290124`) before the encode, so `fp8_sis` (saturate) and `fp8_sns` (NaN-suppress) are both off. **Overflow therefore emits Inf, not max-finite**, and NaN emits canonical NaN. This diverges from the compiler/runtime default `0x0001` (saturate) that the whole-tensor `CastToNewDType` and the MX-quantize path honor — a *simulator-fidelity gap* where the sim is more pessimistic than the device. [CONFIRMED that the stub passes config 0 — disasm; that the device saturates is INFERRED from NEFF `fp8_sis=1` metadata, not from a HW model in these binaries.]

**bf16 (dtype 12), encode `cast_fp32_to_bf16` @ `0x2a42e0`** — bf16 is the high 16 bits of fp32 (shared 8-bit exponent, bias 127), so decode (`0x2a42d0`) is a lossless `<<16`. Encode is true conditional RNE:

```c
if (isnan_fp32(v)) return 0x7fc0;               // 0x2a4313 ucomiss;jp -> canonical qNaN, sign dropped
if (exp == 0xff)  return (sign<<15) | 0x7f80;   // 0x2a43db or $0x7f80 -> Inf
m = mant | 0x800000;                            // 0x2a4350 or $0x800000  restore hidden bit
guard   = m & 0x8000;                           // 0x2a4369 and $0x8000   MSB of 16 discarded bits
lsbKept = m & 0x10000;                          // 0x2a4359 and $0x10000  surviving bf16 LSB (tie input)
sticky  = m & 0x7fff;                           // 0x2a4363 and $0x7fff
if (guard && (lsbKept || sticky)) m += 0x10000; // round up (carry can bump exp)
m >>= 16; // recombine sign|exp|mant7
```

No saturation — bf16 and fp32 share the exponent range, so overflow occurs only when a round-up carries out of `exp==0xfe` into bf16 Inf `0x7f80`. [CONFIRMED — `objdump 0x2a42e0`–`0x2a4400`: the `0x8000`/`0x10000`/`0x7fff` masks and the `0x7fc0`/`0x7f80` codes are byte-present.]

**fp16 (dtype 13), inline @ `0x28ffd0`** — the only narrow float with no helper; both decode and encode are open-coded.

```c
// DECODE (fp16 -> fp32), inline @0x28ffd0
e  = (x << 13) & 0x0F800000;                    // exp field repositioned to bits 23..27
em = (x << 13) & 0x0FFFE000;                    // exp+mant field
if      (e == 0x0F800000) f = em + 0x70000000;  // 0x28ffe6 cmp $0xf800000 -> Inf/NaN rebase
else if (e != 0)          f = em + 0x38000000;  // 0x28fffa lea 0x38000000 -> NORMAL rebase ((127-15)<<23)
else                      f = (em + 0x38800000) - 2.0f^-14; // SUBNORMAL renorm via SSE
f |= (x << 16) & 0x80000000;                    // 0x290003 and $0x80000000  sign
// ENCODE (fp32 -> fp16), inline, hard RNE:
//   in-range knee mag<=0x477fffff; normal-round when mag>0x387fffff:
//     out = (mag + ((mag>>13)&1) - 0x37FFF001) >> 13   // RNE bias folded into the -0x37FFF001 const
//   else subnormal: out = addss(fp32, 0.5)             // 0.5 = 0x3f000000 @ rodata 0x5f3260
//   else overflow: out = (mag < 0x7F800001) ? 0x7C00 : 0x7E00  // finite/Inf -> Inf, NaN -> qNaN
```

fp16 overflow produces fp16 **Inf `0x7c00`** (IEEE-correct, not a max-finite clamp); NaN → `0x7e00`. The fp32 round constant is `0x37FFF001` (= `-exp_rebias 0x38000000 + RNE_bias 0xfff + 1`). [CONFIRMED for the decode rebase (`cmp $0xf800000`, `lea 0x38000000`, sign mask `0x80000000`) and the `0.5`/`2^-14` rodata @ `0x5f3260` = `0x3f000000 0x38800000`; the encode knee constants `0x477fffff`/`0x37FFF001`/`0x7F800001` are STRONG — documented and consistent with the decode-side immediates, materialized as SSE float comparisons rather than `cmp` immediates.]

> Note on a prior transcription: the fp16 RNE constant is `0x37FFF001` (939520001), not `0x37FF0001`; the binary `lea` folds the round bias into this single immediate. [CONFIRMED.]

## The MIN/MAX reversal correction

This is the page's strongest finding. The compact `birsim::MemoryReductionOp` enum was previously documented as `{copy=0, add=1, MAX=2, MIN=3}`. **The binary proves the byte-exact truth is `{copy=0, add=1, MIN=2, MAX=3}`** — op code 2 is MIN, op code 3 is MAX. Three independent reduce handlers agree, leaving no ambiguity:

| `op` | int32 (`0x28ffb0`) | int64 (`0x28ff60`) | fp32 (`0x28f700`) | meaning |
|----|----|----|----|----|
| 1 | `add %eax,(%r12)` | `add %rax,(%r12)` | `addss` | **add** |
| 2 | `je 0x290260` → `pminsd` | `je 0x290240` → `cmovg` (keep-smaller) | `je 0x28f900` → `minss` | **MIN** |
| 3 | `je 0x2901e0` → `pmaxsd` | `je 0x290210` → `cmovl` (keep-larger) | `maxss` | **MAX** |

[CONFIRMED — all three handlers disassembled above; `cmp $0x2,%edi` always routes to the MIN instruction (`pminsd`/`cmovg`/`minss`) and `cmp $0x3,%edi` always to the MAX instruction. The int64 `cmovg %rdx,%rax` after `mov (%r12),%rax` keeps `rax`(dst) when `dst <= src` — i.e. selects the smaller — confirming op 2 = MIN.]

The practical consequence is muted by a second finding: in this build **the memory-RMW min/max kernels are unreachable**. Both branches exist (the SSE/`cmov` instructions are present), but no producer of the `op` argument ever passes 2 or 3.

## AluOpType2MemoryReduction: the converter

`birsim::AluOpType2MemoryReduction` @ `0x1b0ed0` maps the wide `bir::AluOpType` (the cce-reduce field at instruction `+0x178`) into the compact `MemoryReductionOp`. It accepts **only two** values cleanly:

```c
// AluOpType2MemoryReduction(Instruction const* /*rdi, unused for value*/, AluOpType a2 /*esi*/)
int slot = a2;                          // 0x1b0ee6 mov %esi,0x1c(%rsp)   (seed return = a2)
if (a2 == 0) return slot;               // 0x1b0eea test %esi,%esi; je 0x1b1258 -> 0 == copy
if (a2 == 4) { slot = 1; return slot; } // 0x1b0ef2 cmp $0x4,%esi; je 0x1b1250; mov $1 -> 1 == add
// else (any a2 not in {0,4}, INCLUDING max=8 / min=9):
//   build NeuronAssertion(false) at inst_visitor.cpp:363 and std::throw_with_nested -> THROWS
```

| `bir::AluOpType` | int | → `MemoryReductionOp` | proof |
|----|----|----|----|
| bypass | 0 | copy = 0 | `test %esi,%esi; je 0x1b1258`; return slot = a2 = 0 |
| add | 4 | add = 1 | `cmp $0x4,%esi; je 0x1b1250`; `mov $0x1`; return 1 |
| max | 8 | **THROWS** | falls into NeuronAssertion (`inst_visitor.cpp:363`) |
| min | 9 | **THROWS** | same |
| (all others) | … | **THROWS** | same |

[CONFIRMED — `objdump 0x1b0ed0`: the two `je` branches to `0x1b1258`/`0x1b1250`, the `movl $0x1,0x1c(%rsp)` at `0x1b1250`, and the shared `mov 0x1c(%rsp),%eax; ... ret` epilogue at `0x1b1258`–`0x1b126d`. The else-path loads the assertion source-path string @ `0x5a57d8` (`.../Simulator/inst_visitor.cpp:363`).]

> **GOTCHA — min/max do not map; they throw.** A previous pass inferred `min(9)/max(8) → MemoryReductionOp 2/3` by fall-through. The converter does no such thing: only `bypass→copy` and `add→add` return; every other `AluOpType` (min and max included) hits `NeuronAssertion(false)` and throws. So the converter *never* produces a 2 or a 3, which is why the kernel's MIN/MAX branches are dead through this path. [CONFIRMED.]

### Reachability of `op`

`cast_copy_or_accumulate` has exactly two callers, and both yield `op ∈ {0,1}`:

- `Memory::doCopyIndirect` @ `0x17a930` — per-element loop calls `cast_copy_or_accumulate` @ `0x17bcb0` with `op` threaded from `writeIndirect`. That `op` originates from `AluOpType2MemoryReduction`, so it is 0 or 1 only.
- `MemoryObject::runAP` @ `0x298390` — calls @ `0x29883f` with a **boolean** `op` (`*v34 != 0`): 0 = copy, 1 = add. Never 2/3.

`AluOpType2MemoryReduction` itself has three callers (`visitInstIndirectSaveAccumulate` `0x1e2640`, `visitInstGenericIndirectSave` `0x1f3b30`, `visitInstDMACopy` `0x20b7b0`), all forwarding the cce-op field and all therefore restricted to `{bypass, add}`. The BIR verifier (`birverifier::checkCCEAluOpType`) admits a wider per-opcode set including min/max, so a min/max cce-reduce is *verifier-legal but simulator-unsupported* in this build. [CONFIRMED for the call sites; the verifier-vs-sim gap is STRONG — the verifier permits combinations the converter rejects.]

## The collective reducer is a separate engine

A *third* reducer exists but does not use `MemoryReductionOp` or `cast_copy_or_accumulate` at all: `doReduction` @ `0x1b3400`, the collective (AllReduce / ReduceScatter) path. It reads `bir::AluOpType` directly at instruction `+0x180` and dispatches on fp32:

```c
// doReduction @0x1b3400, op = *(int*)(inst + 0x180)
switch (op) {
    case 4: *acc = addss(*x, *acc); break; // 0x1b3b60  add
    case 8: *acc = maxss(*x, *acc); break; // 0x1b3af0  movss(x); maxss(acc) -> MAX
    case 9: *acc = minss(*x, *acc); break; // 0x1b3b70  movss(x); minss(acc) -> MIN
    default: NeuronAssertion(false);       // inst_visitor.cpp:5365
}
```

This is the **only** path that executes a reducing min/max in this build. [CONFIRMED — `objdump 0x1b35ca`: `cmp $0x8` / `cmp $0x9` / `cmp $0x4` dispatch on `inst+0x180` (`cmpl $0x4,0x180(%rbx)`); handlers at `0x1b3af0`/`0x1b3b70` are `maxss (%rdx),%xmm0` / `minss (%rdx),%xmm0`.]

> **CORRECTION to a prior note.** The collective min/max were earlier described as libm `fmaxf`/`fminf` (IEEE quieting). The binary disagrees: the handlers at `0x1b3af0`/`0x1b3b70` are inline SSE `maxss`/`minss` — the *same* 2nd-operand-on-NaN family as `cast_copy_or_accumulate`'s fp32 core, **not** libm. With `movss (x); maxss (acc)`, a NaN operand or tie returns `acc`. There is no `call fmaxf@plt`/`call fminf@plt` in the dispatch region. [CONFIRMED — `objdump 0x1b3af0`/`0x1b3b70` show `maxss (%rdx),%xmm0` / `minss (%rdx),%xmm0` with no PLT call.]

## Function map

| Function | Addr | Role | Conf |
|----|----|----|----|
| `birsim::cast_copy_or_accumulate` | `0x28ff00` | per-dtype RMW apply `(op,dst,src,dt)` | CONFIRMED |
| `birsim::AluOpType2MemoryReduction` | `0x1b0ed0` | `AluOp → MemoryReductionOp` (only `{copy,add}`) | CONFIRMED |
| `sub_28f700` (fp32 ALU core) | `0x28f700` | `op1`=`addss`, `op2`=`minss`, `op3`=`maxss` | CONFIRMED |
| fp8 reduce stubs E3M4/E4M3/E5M2 | `0x290190`/`0x290140`/`0x2900f0` | decode→ALU→encode, config=0 | CONFIRMED |
| bf16 stub | `0x2900a0` | decode `0x2a42d0` (`<<16`) / encode `0x2a42e0` (RNE) | CONFIRMED |
| fp16 inline | `0x28ffd0` | open-coded decode + RNE encode | CONFIRMED |
| int32 inline | `0x28ffb0` | `pminsd`/`pmaxsd`/wrap-add | CONFIRMED |
| int64 inline | `0x28ff60` | `cmovg`(MIN)/`cmovl`(MAX)/wrap-add | CONFIRMED |
| `REDUCE_JT` | `0x5f7694` | 17×int32 table-rel; 9 → throw `0x110548` | CONFIRMED |
| `COPY_SIZE_LUT` | `0x5f7760` | 20×u64 dtype stride | CONFIRMED |
| throw string | `0x5c9608` | `"cast_accumulate: unsupported dtype "` | CONFIRMED |
| `Memory::doCopyIndirect` | `0x17a930` | per-elem caller (call @ `0x17bcb0`) | CONFIRMED |
| `MemoryObject::runAP` | `0x298390` | bool-op caller (call @ `0x29883f`) | CONFIRMED |
| `doReduction` (collective) | `0x1b3400` | AluOp-direct `addss`/`maxss`/`minss` on fp32 | CONFIRMED |

## Reimplementation checklist

- Implement `op==0` as `memcpy(dst, src, stride[dt])` for **all** dtypes; gate the reduce path on the 8-dtype set and throw `unsupported dtype` for the other 12.
- Integers: **wrap** on add (no saturating add); signed `min`/`max`.
- Floats: reduce in fp32, narrow back with **hard RNE**; use SSE `min`/`max` semantics (2nd-operand-on-NaN) if bit-matching the simulator, *not* `minNum`/`maxNum`.
- For fp8 narrow in the reduce path, force config 0 (Inf on overflow, canonical NaN) to match the sim — knowing this diverges from the saturating device default.
- The compact op enum is `{copy=0, add=1, MIN=2, MAX=3}`; the only producer maps `{bypass→copy, add→add}` and throws on everything else.

## Cross-references

- [9.3 Cast to New Dtype](../numerics/cast-to-new-dtype.md) — the shared fp8/fp16/bf16 ↔ fp32 conversion LUTs this kernel calls (not duplicated here).
- [9.10 Negative Results](../numerics/negative-results.md) — the unsupported-dtype and device-vs-sim fp8-saturate gaps as standalone negative findings.
- [7.34 Sim Dispatch & State](sim-dispatch-state.md) — the simulator state model this arithmetic reads and mutates.
- [BIR dtype tables](dtype-tables.md) — the 20-entry `bir::Dtype` enum and the byte-stride vector that is `COPY_SIZE_LUT`.
- [BIR AluOp modes](aluop-modes.md) — the wide `bir::AluOpType` the converter narrows from.
