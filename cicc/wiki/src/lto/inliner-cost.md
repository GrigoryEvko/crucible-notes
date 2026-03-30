# Inliner Cost Model

CICC v13.0 contains four parallel inliner cost models -- an architecturally unusual design that reflects both the historical evolution of NVIDIA's compiler and the fundamental differences between GPU and CPU inlining economics. The NVIDIA custom inliner at `0x1864060` (75 KB, 2135 decompiled lines) uses a 20,000-unit budget that is 89x the upstream LLVM default of 225. Roughly 60% of the custom inliner's code computes type-size comparisons for argument coercion cost, because on GPU the dominant cost of a function call is not instruction count but `.param` address-space marshaling. Alongside the custom model, CICC also links the standard LLVM `InlineCostAnalysis` at `0x30DC7E0` (51 KB), a New Pass Manager CGSCC inliner at `0x2613930` (69 KB) with ML-based advisory support, and an NVPTX target-specific cost modifier at `0x38576C0` (58 KB) that injects a +2000 bonus for GPU intrinsics.

| | |
|---|---|
| **Model A: NVIDIA custom** | `sub_1864060` (`0x1864060`, 75 KB, CGSCC) |
| **Model B: LLVM standard** | `sub_30DC7E0` (`0x30DC7E0`, 51 KB, `InlineCostAnalysis`) |
| **Model C: New PM CGSCC** | `sub_2613930` (`0x2613930`, 69 KB, recursive SCC) |
| **Model D: NVPTX target** | `sub_38576C0` (`0x38576C0`, 58 KB, opcode-based) |
| **Knob constructor** | `ctor_186_0` (`0x4DBEC0`, 14 KB) |
| **LLVM knob constructor** | `ctor_625_0` / `ctor_715_0` (`0x58FAD0`, 27 KB) |

## Why Four Inliner Models

The four models are not truly interchangeable alternatives -- they serve overlapping but distinct roles in the compilation pipeline:

**Model A** is the original NVIDIA inliner, predating the LLVM 14+ New Pass Manager. It operates on NVIDIA's internal NVVM IR node format (not LLVM IR), walks the callee body with bespoke type-size arithmetic, and is the only model that understands `.param`-space argument coercion costs. It runs inside the legacy CGSCC inliner framework via `sub_186CA00` (`Inliner::inlineCallsImpl`). When CICC runs in its default optimization pipeline, this is the model that makes the bulk of inlining decisions.

**Model B** is upstream LLVM's `InlineCostAnalysis::analyzeCall`, compiled into CICC essentially unmodified. It uses LLVM's instruction-counting cost model with a 225-unit default threshold, the `inline-threshold`, `inlinedefault-threshold`, and PGO deferral knobs. It exists because CICC links the full LLVM codebase and certain LLVM passes (e.g., the always-inliner, sample-profile inliner) call into `getInlineCost` / `analyzeCall` directly.

**Model C** is the New Pass Manager's CGSCC inliner at `0x2613930`. It handles recursive SCC splitting, carries the `function-inline-cost-multiplier` knob for penalizing recursive functions, and can delegate decisions to an `InlineAdvisor` (`sub_2609820`, 57 KB). The advisor supports three modes registered in the pipeline parser: `default`, `development` (training), and `release` (inference). The ML model inference path lives at `sub_29B2CD0` / `sub_29B4290`. CICC registers the pipeline string `"inliner-ml-advisor-release"` for the release mode (parser slot 49).

**Model D** is an NVPTX target-specific cost modifier at `0x38576C0` that adjusts inline costs based on opcode analysis. Its primary contribution is a +2000 cost bonus for functions containing opcode tag 9 instructions (see [Opcode Tag 9 Bonus](#nvptx-opcode-tag-9-bonus-2000) below). This runs as a layer on top of whichever primary cost model is active, modifying the accumulated cost at `offset+72` and comparing against the threshold at `offset+76`.

The historical layering is: NVIDIA built Model A first for their custom NVVM IR, then LLVM matured its own inliner (Model B), then the New PM arrived with ML advisory (Model C), and NVPTX target hooks added GPU-specific adjustments (Model D). Rather than consolidating, NVIDIA kept all four because each handles a different phase or code path in the pipeline.

## The `.param` Address Space Problem

Understanding the NVIDIA inliner requires understanding why GPU function calls are so expensive compared to CPU calls. On x86, a function call requires pushing arguments to registers/stack, a `CALL` instruction, and a `RET`. The overhead is typically 5-20 cycles.

On NVIDIA GPUs, there is no hardware call stack for registers. The PTX calling convention works through the `.param` address space:

1. **Caller declares `.param` variables** via `DeclareParam` (opcode 505) or `DeclareScalarParam` (opcode 506) for each argument.
2. **Caller stores argument values** into `.param` space via `st.param` instructions (opcodes 571-573 for StoreV1/V2/V4).
3. **Caller emits the `call` instruction** referencing the `.param` declarations.
4. **Callee loads arguments** from `.param` space via `ld.param` instructions.
5. **Return values** come back through `.param` space via `ld.param` (opcodes 515-516, 568-570 for LoadRetParam / LoadV1/V2/V4).
6. **Byval arguments** (structs passed by value) copy the entire struct to `.param` space field by field.

Each function call therefore generates O(n) `st.param` + O(n) `ld.param` instructions where n is the number of arguments, plus register save/restore if the callee needs more registers than are available (spills go to local memory, which is device DRAM -- hundreds of cycles). Additionally, call boundaries destroy instruction scheduling freedom, prevent cross-boundary register allocation, and create branch divergence hazards at the call/return sites.

This is why NVIDIA's default inline budget of 20,000 is not as aggressive as it sounds: inlining a function with 50 instructions but 8 struct arguments might save hundreds of cycles of `.param` marshaling overhead.

## Model A: NVIDIA Custom Inliner

### Knob Inventory

All knobs are registered in `ctor_186_0` at `0x4DBEC0`:

| Knob | Type | Default | Purpose |
|------|------|---------|---------|
| `inline-budget` | int | 20,000 | Per-caller inlining cost budget |
| `inline-total-budget` | int | (none) | Global total budget across all callers in the module |
| `inline-adj-budget1` | int | (none) | Secondary per-caller budget, dynamically adjusted |
| `nv-inline-all` | bool | off | Force inline every function call unconditionally |
| `profuseinline` | bool | off | Verbose inlining diagnostics (NVIDIA profuse framework) |
| `inline-switchctrl` | int | (none) | Switch-statement inlining heuristic tuning |
| `inline-numswitchfunc` | int | (none) | Penalty based on number of switch stmts in callee |
| `inline-maxswitchcases` | int | (none) | Maximum switch cases before cost penalty applies |
| `disable-inlined-alloca-merging` | bool | off | Disable post-inline alloca merging |

CLI surface mapping:

| User Flag | Routed To |
|-----------|-----------|
| `-aggressive-inline` | `-inline-budget=40000` (2x default) |
| `-disable-inlining` | `-disable-inlining` |
| `-inline-budget=N` | Sets per-caller budget directly |
| `-inline-info` | Diagnostic flag for inline decisions |

### Entry and Early Bail-Outs

The entry point `sub_1864060` takes four arguments: `a1` = function/callsite node, `a2` = context, `a3` = callback, `a4` = data pointer. The function performs a series of eligibility checks before any cost computation:

**Intrinsic name check.** Calls `sub_1649960(a1)` to retrieve the function name. If the name starts with the 4-byte magic `0x6D6C6C6C` (an LLVM intrinsic prefix) followed by `'.'`, returns 0 immediately. LLVM intrinsics are never inlined through this path.

**Pre-analysis walk.** Initializes a 32-byte inline-analysis state struct via `sub_1ACF5D0`, then calls `sub_1ACF600` which delegates to `sub_1ACF0B0`. This walks the callee body to collect basic metrics (instruction count, call count, basic block count). If the pre-analysis returns nonzero, the function is not analyzable.

**Linkage check.** Reads the byte at `a1+32`. The low nibble encodes linkage class: values 7 (`linkonce_odr`) and 8 (`weak_odr`) are eligible for inlining. Bits [7:6] encode visibility: `0x2` = hidden (OK), `0x1` = protected (bail). The function also requires byte at `a1+16` == 3 (function definition, not declaration), bit 0 of byte at `a1+80` == 0 (no `noinline` attribute), and `sub_15E4F60(a1)` returning false (no `optnone`).

```
function shouldInline(callsite):
    name = getName(callsite.callee)
    if name starts with LLVM_INTRINSIC_PREFIX:
        return NEVER_INLINE

    state = initAnalysisState()
    if preAnalyze(callsite.callee, state) != 0:
        return NEVER_INLINE

    linkage = callsite.callee.linkage
    if linkage not in {linkonce_odr, weak_odr}:
        return NEVER_INLINE
    if callsite.callee.isDeclaration:
        return NEVER_INLINE
    if callsite.callee.hasNoinline:
        return NEVER_INLINE
    if callsite.callee.hasOptnone:
        return NEVER_INLINE

    // ... proceed to cost computation
```

### Callee Body Scan

After eligibility checks pass, the inliner walks the callee's operand/argument list (linked list at `a1+8`). Each argument node is classified by its type tag at byte offset +16 via `sub_1648700`:

| Tag Range | Meaning | Action |
|-----------|---------|--------|
| <= `0x17` | Basic types or call-like | If tag == 5 (phi): recurse into operands, check all > `0x17`; otherwise bail |
| `0x36` (54) | Load-like instruction | Collect into loads vector |
| `0x37` (55) | Store-like instruction | Collect into stores vector |
| `0x47` (71, `'G'`) | Aggregate/GEP | Enter sub-operand scan |

The loads and stores are accumulated into two `SmallVector`s (`v357`, `v360`) with initial inline capacity of 4 elements each. These vectors are the input to the argument coercion cost check.

### Load-Store Combinatorial Bail-Out

Before proceeding to the expensive type-size computation, the function checks:

```
if (num_loads * num_stores > 100):
    return BAIL_OUT  // Too expensive argument copy pattern
```

This prevents inlining functions where argument materialization would create a quadratic load-store explosion. Consider a function taking 4 struct-by-value arguments, each with 30 fields: that is 120 loads times 120 stores = 14,400 combinations, far above the 100 threshold. Without this guard, the type-size computation engine below would take unreasonable time.

### Type-Size Computation Engine

The bulk of `sub_1864060` -- lines 1140 through 2100, approximately 60% of the function -- is a type-size computation engine. This is the single most distinctive feature of the NVIDIA inliner: where LLVM counts instructions, NVIDIA computes byte-level argument coercion costs.

The engine walks NVVM IR type nodes and computes byte sizes for each argument at both the callsite (actual argument) and the callee (formal parameter). The type tag dispatch is repeated 8+ times across different contexts:

| Type Tag | Type | Size Computation |
|----------|------|------------------|
| `0x01` | `half` | 16 bits |
| `0x02` | `float` | 32 bits |
| `0x03` | `double` | 64 bits |
| `0x04` | `fp80` | 80 bits |
| `0x05` | `fp128` | 128 bits |
| `0x06` | `ppc_fp128` | 128 bits |
| `0x07` | pointer | `sub_15A9520(module, 0)` for target pointer size |
| `0x08` | array | `element_type_size * count` (recursive) |
| `0x09` | `x86_mmx` | 64 bits |
| `0x0A` | vector | `element_type_size * count` (recursive) |
| `0x0B` | integer | `(dword >> 8)` bits |
| `0x0C` | function | Recurse (unusual, but handled) |
| `0x0D` | struct | `sub_15A9930` for layout size |
| `0x0E` | packed struct | Manual: `8 * count * align * ceil` |
| `0x0F` | named type | `sub_15A9520(module, type_id)` |
| `0x10` | opaque/token | `element_type_size * count` |

The byte-size formula applied uniformly is:

```
byte_size = (multiplier * bit_width + 7) >> 3
```

The core comparison at the heart of the cost model:

```
if callee_arg_size > callee_formal_size:
    // Argument is being widened at the call boundary
    // This costs extra st.param + ld.param instructions
    // Proceed to next comparison level (accumulate cost)
else:
    // Sizes match or shrink -- this argument pair is OK
```

Arguments are processed in groups of 4 (loop unrolled at line 2098: `v142 += 4`, `--v306` where `v306 = num_stores * 8 >> 5`, i.e., groups of 4 store arguments). Remainder arguments (1-3 after the groups-of-4 loop) are handled by the type compatibility check function `sub_185CCC0` which calls `sub_15CCEE0` for type matching.

### Struct Layout Walk

The helper `sub_185B2A0` (3 KB) performs a stack-based DFS walk of struct type trees to count fields. It handles pointer types (tag 15), struct types (tag 13/14), and array types (tag 16). The walk has a hard depth limit of 20 levels, preventing runaway recursion on deeply nested struct definitions.

### Argument Coercion Check

The helper `sub_185D7C0` (9 KB) classifies each callee operand and determines whether argument coercion is needed at the inline callsite. For each operand in the callee's argument linked list at `a1+8`, it:

1. Reads the instruction tag via `sub_1648700`.
2. Computes the formal parameter type size.
3. Computes the actual argument type size at the callsite.
4. If sizes differ, flags this argument as requiring coercion (extra cost).
5. If the argument is a struct, invokes the struct layout walk to count individual field copies.

### Callsite Transformation

When the callee qualifies for "alias inline" (replacing a call with direct body substitution), the function:

1. Allocates a new 88-byte IR node via `sub_1648A60(88, 1)`.
2. Builds a function reference node via `sub_15F8BC0`.
3. Builds a call replacement node via `sub_15F9660`.
4. Walks callee operands to collect phi nodes into a worklist.
5. For each phi: copies via `sub_1596970`, updates operands via `sub_15F2120`, replaces references via `sub_1648780`.
6. Deletes original phis via `sub_159D850`.
7. Performs final callsite replacement via `sub_164D160` + `sub_15E55B0`.

### Switch Statement Heuristics

Three dedicated knobs control inlining of switch-heavy functions. On GPU, large switch statements are particularly costly because:

- **Branch divergence:** Each thread in a warp may take a different case, serializing execution.
- **No branch prediction hardware:** Every divergent branch pays full penalty.
- **Control flow reconvergence:** The hardware must synchronize threads after the switch, wasting cycles.

The `inline-switchctrl` knob tunes the general heuristic sensitivity. `inline-numswitchfunc` penalizes functions containing many switch statements. `inline-maxswitchcases` sets a case-count ceiling beyond which a switch-heavy callee is considered too expensive to inline regardless of other factors.

### nv-inline-all: Force-All Mode

The `nv-inline-all` knob bypasses cost analysis entirely and forces inlining of every call. This is used for specific compilation modes where the call graph must be completely flattened:

- **OptiX ray tracing:** The hardware intersection pipeline requires a single monolithic function. All user-defined intersection, closest-hit, any-hit, and miss programs must be inlined into a single continuation function.
- **Aggressive LTO:** When doing whole-program optimization with small modules, flattening removes all call overhead.

### Two-Budget System

NVIDIA uses a two-level budget to control inlining granularity:

- `inline-budget` (default 20,000): Per-caller limit. Caps how much code can be inlined into a single function, preventing any one function from becoming unreasonably large.
- `inline-total-budget`: Module-wide limit. Caps the total amount of inlining across all callers in the compilation unit.
- `inline-adj-budget1`: A secondary per-caller limit that may be dynamically adjusted based on context -- for example, kernel entry points (`__global__` functions) may receive a higher adjusted budget because they are the outermost scope and benefit most from aggressive inlining.

The threshold adjustment helper at `sub_1868880` (12 KB) modifies thresholds based on calling context through pure arithmetic on cost/threshold values (no string evidence, entirely numeric).

### Alloca Merging

The `disable-inlined-alloca-merging` knob controls post-inline stack allocation merging. On GPU, "stack" means local memory, which is device DRAM (hundreds of cycles latency). Merging allocas from inlined callees with the caller's allocations reduces total local memory consumption. Lower local memory usage directly improves occupancy (more concurrent thread blocks per SM). The default is to **enable** merging.

## Model B: LLVM Standard InlineCostAnalysis

The standard LLVM `InlineCostAnalysis::analyzeCall` at `0x30DC7E0` (51 KB) is compiled into CICC from upstream LLVM sources. Its knobs are registered in `ctor_625_0` / `ctor_715_0` at `0x58FAD0` (27 KB of option registration, an unusually large constructor due to the 40+ individual cost parameter registrations).

Key upstream LLVM knobs present in CICC:

| Knob | Default | Purpose |
|------|---------|---------|
| `inline-threshold` | 225 | Base inlining threshold |
| `inlinedefault-threshold` | 225 | Default when no hint/profile |
| `inlinehint-threshold` | 325 | Threshold for `__attribute__((always_inline))` hint |
| `inline-cold-callsite-threshold` | 45 | Threshold for cold callsites |
| `inlinecold-threshold` | 45 | Threshold for functions with cold attribute |
| `hot-callsite-threshold` | 3000 | Threshold for hot callsites (PGO) |
| `locally-hot-callsite-threshold` | 525 | Threshold for locally hot callsites |
| `inline-instr-cost` | 5 | Cost per instruction |
| `inline-call-penalty` | 25 | Penalty per callsite in callee |
| `inline-memaccess-cost` | 0 | Cost per load/store |
| `inline-savings-multiplier` | 8 | Multiplier for cycle savings |
| `inline-savings-profitable-multiplier` | 4 | Multiplier for profitability check |
| `inline-size-allowance` | 100 | Max callee size inlined without savings proof |
| `inline-cost-full` | false | Compute full cost even when over threshold |
| `inline-enable-cost-benefit-analysis` | false | Enable cost-benefit analysis |
| `inline-deferral` | (PGO) | Defer inlining in cold paths |
| `inline-remark-attribute` | (off) | Emit inline remarks |

The LLVM model fundamentally counts instructions (at `inline-instr-cost` = 5 units each) and subtracts savings from constant propagation, dead code elimination after argument specialization, and simplified control flow. This instruction-counting approach is appropriate for CPUs where call overhead is small and code size is the primary concern. It is inadequate for GPUs where argument marshaling dominates.

## Model C: New PM CGSCC Inliner

The New Pass Manager inliner at `0x2613930` (69 KB) handles recursive SCC processing and integrates with LLVM's `InlineAdvisor` framework. Its key differentiation is the `function-inline-cost-multiplier` knob that penalizes recursive function inlining -- a scenario the NVIDIA custom inliner (Model A) does not handle.

The `InlineAdvisor` at `sub_2609820` (57 KB) supports three modes:

| Mode | Pipeline String | Behavior |
|------|----------------|----------|
| `default` | `"inline-advisor"` | Heuristic-based (uses Model B cost analysis) |
| `development` | (training path) | Feature extraction for ML model training |
| `release` | `"inliner-ml-advisor-release"` | ML model inference via `sub_29B2CD0` / `sub_29B4290` |

The ML inference path extracts features from the callsite and callee (instruction count, call depth, loop nesting, etc.) and feeds them through a model to produce an inline/no-inline decision. This is standard upstream LLVM ML inlining infrastructure compiled into CICC; there is no evidence of NVIDIA-custom ML model weights, though NVIDIA could supply custom weights via the `enable-ml-inliner` knob (registered as an enum: `{default, development, release}`).

## NVPTX Opcode Tag 9 Bonus (+2000)

Model D at `sub_38576C0` modifies inline costs based on NVPTX-specific opcode analysis. The key logic:

```
for each instruction in callee:
    tag = getOpcodeTag(instruction)
    if ((tag >> 4) & 0x3FF) == 9:
        inline_cost += 2000
    // ... accumulate other per-instruction costs
```

The state layout of the cost analyzer object:

| Offset | Field | Purpose |
|--------|-------|---------|
| +72 | Accumulated cost | Running sum of per-instruction costs |
| +76 | Threshold | Budget for this callsite |
| +120 | Per-instruction cost (lo) | Cost array element (low) |
| +128 | Per-instruction cost (hi) | Cost array element (high) |

The +2000 bonus for tag 9 opcodes encourages inlining of functions containing specific GPU operations -- likely tensor core instructions, warp-level intrinsics, or other operations that benefit significantly from being visible to the register allocator and instruction scheduler within the caller's scope. The bonus is large enough (equivalent to inlining ~400 regular LLVM instructions at cost 5 each) to override most size-based objections.

## NVIDIA vs. LLVM: Complete Comparison

| Feature | NVIDIA (Model A) | LLVM (Model B) |
|---------|-------------------|----------------|
| Default threshold | 20,000 | 225 |
| Aggressive threshold | 40,000 | Varies by `-O` level |
| Primary cost metric | Argument type-size coercion | Instruction count |
| Cost per instruction | N/A (not instruction-based) | 5 units |
| Struct handling | Deep field-by-field walk (depth limit 20) | Aggregate flat cost |
| GPU opcode bonus | +2000 for tag 9 | N/A |
| Load x store bail-out | > 100 combinations | N/A |
| Switch heuristics | 3 dedicated knobs | 1 (`case-cluster-penalty`) |
| Budget system | Per-caller + module total + adjusted | Per-callsite only |
| Diagnostic knob | `profuseinline` | `inline-remark-attribute` |
| Force-all mode | `nv-inline-all` | `inline-all-viable-calls` (hidden) |
| ML-based advisor | No (separate path via Model C) | Yes (`InlineAdvisor`) |
| Recursive cost multiplier | No | `function-inline-cost-multiplier` |
| Alloca merging control | `disable-inlined-alloca-merging` | N/A |
| Call penalty | Implicit (`.param` marshaling cost) | 25 units per callsite |
| PGO integration | No evidence | `inline-deferral`, `hot-callsite-threshold` |

## Decision Flowchart

The complete inlining decision flow through Model A:

```
                     CallSite arrives at sub_186CA00
                              |
                   sub_186B510: check remarks
                              |
                   sub_1864060: shouldInline
                              |
                     +--------+--------+
                     |                 |
              Name is LLVM       Name is user
              intrinsic?         function
                     |                 |
                NEVER INLINE     Init analysis state
                                 sub_1ACF5D0
                                      |
                                 Pre-analyze callee
                                 sub_1ACF600
                                      |
                              +-------+-------+
                              |               |
                         Returns 0       Returns != 0
                         (analyzable)    (cannot analyze)
                              |               |
                     Check linkage      NEVER INLINE
                     (7=linkonce_odr
                      8=weak_odr)
                              |
                  +-----------+-----------+
                  |                       |
            Eligible                Not eligible
                  |                  (wrong linkage,
             Check noinline,         declaration,
             optnone attrs           protected vis)
                  |                       |
            +-----+-----+          NEVER INLINE
            |           |
         Has attr    No attr
            |           |
       NEVER INLINE  Walk callee body
                     collect loads/stores
                              |
                     loads * stores > 100?
                        +-----+-----+
                        |           |
                       Yes         No
                        |           |
                   BAIL OUT    Type-size computation
                               (60% of function)
                                    |
                              Compute per-argument
                              coercion cost
                                    |
                              Total cost < inline-budget?
                                 +-----+-----+
                                 |           |
                                Yes         No
                                 |           |
                              INLINE     DO NOT INLINE
                              Transform callsite
                              sub_1648A60 / sub_15F8BC0
```

## Call Graph

```
sub_186CA00  Inliner::inlineCallsImpl (CGSCC SCC walk)
  +-> sub_186B510  Inline decision with remarks
      +-> sub_1864060  shouldInline / cost computation (THIS)
          +-> sub_1ACF5D0  Inline analysis state init
          +-> sub_1ACF600  Pre-analysis callee walk
          |   +-> sub_1ACF0B0  Metric collection
          +-> sub_185FD30  Argument materialization cost (5 KB)
          +-> sub_185E850  Post-inline cleanup assessment (9 KB)
          +-> sub_185B2A0  Struct layout walk, depth limit 20 (3 KB)
          +-> sub_185D7C0  Argument matching / coercion (9 KB)
          +-> sub_185B9F0  Recursive operand simplification (5 KB)
          +-> sub_185CCC0  Type compatibility check (4 KB)
          +-> sub_18612A0  GlobalOpt integration (65 KB, conditional)
  +-> sub_1868880  Inline threshold adjustment (12 KB)
  +-> sub_1866840  Post-inline callsite update (42 KB)
```

## Why 89x the LLVM Budget

The 20,000 vs. 225 ratio sounds extreme, but the economics are different:

**CPU call overhead** is approximately 5-20 cycles (push/pop registers, branch prediction handles the rest). A function with 50 instructions that is not inlined costs perhaps 60-70 cycles total. Inlining saves ~15 cycles. The savings must justify the I-cache pressure increase.

**GPU call overhead** includes: (1) declaring `.param` variables for every argument, (2) `st.param` for each argument value, (3) `ld.param` in the callee for each argument, (4) register save/restore to local memory (device DRAM, 200-800 cycle latency) if the callee's register demand exceeds what is available, (5) loss of instruction scheduling across the call boundary, (6) branch divergence at call/return. For a function with 8 arguments, the `.param` overhead alone is 16+ memory operations. With register spilling, a single function call can cost 1000+ cycles.

Furthermore, GPU functions tend to be small (typically 10-100 instructions for device helper functions). The NVIDIA cost model does not count instructions at all -- it counts the argument marshaling cost. A function with 200 instructions but 2 scalar arguments is cheap to call; a function with 10 instructions but 8 struct arguments is expensive. The 20,000 budget reflects this: it is not 89x more aggressive in inlining large functions; it is calibrated for a cost model where the per-argument coercion cost dominates rather than instruction count.

With `-aggressive-inline` (budget 40,000, i.e., 178x the LLVM default), NVIDIA targets workloads like OptiX where complete flattening is desired but `nv-inline-all` is too blunt (it ignores all cost analysis).

## What Upstream LLVM Gets Wrong for GPU

Upstream LLVM's inliner cost model was built for x86/AArch64 where function call overhead is small and code size is the primary inlining constraint. On GPU, every assumption is wrong:

- **Upstream assumes a 225-instruction budget is sufficient.** The default `inline-threshold` of 225 reflects CPU economics where a function call costs 5-20 cycles (register push/pop + branch). On GPU, a single function call with 8 struct arguments generates 16+ `.param`-space memory operations, potential register spills to device DRAM (200-800 cycle latency), loss of cross-boundary scheduling, and branch divergence hazards. NVIDIA's 20,000-unit budget (89x upstream) is calibrated for this reality, not because GPU code is more aggressive about inlining large functions.
- **Upstream counts instructions as the primary cost metric.** LLVM prices each instruction at 5 units and subtracts savings from constant propagation and dead code elimination. NVIDIA's custom inliner (Model A) does not count instructions at all -- 60% of its 75KB body computes byte-level argument type-size coercion costs, because on GPU the dominant cost of a function call is `.param` address-space marshaling, not instruction count.
- **Upstream has no concept of `.param`-space argument passing cost.** CPU calling conventions pass arguments in registers (nearly free) or via L1-cached stack (3-5 cycles). On GPU, every argument requires explicit `DeclareParam` + `st.param` (caller) + `ld.param` (callee) sequences. A function with 10 instructions but 8 struct arguments is more expensive to call than one with 200 instructions and 2 scalar arguments. Upstream's model gets this exactly backwards.
- **Upstream uses a single per-callsite budget.** NVIDIA uses a three-level system: per-caller budget (`inline-budget`), module-wide total budget (`inline-total-budget`), and a dynamically adjusted secondary budget (`inline-adj-budget1`) that can give kernel entry points higher limits. This multi-level approach prevents any single caller from bloating while still allowing aggressive inlining where it matters most.
- **Upstream has no GPU intrinsic awareness.** NVIDIA's Model D applies a +2000 cost bonus for functions containing opcode tag 9 instructions (likely tensor core or warp-level intrinsics), because these operations benefit enormously from being visible to the register allocator and scheduler within the caller's scope. Upstream LLVM has no mechanism to express "this function contains operations that are disproportionately valuable to inline."

## Key Addresses

| Address | Size | Function |
|---------|------|----------|
| `0x1864060` | 75 KB | `shouldInline` / inline cost computation |
| `0x186CA00` | 61 KB | `Inliner::inlineCallsImpl` (CGSCC core) |
| `0x186B510` | 20 KB | Inline decision with remarks |
| `0x1866840` | 42 KB | Post-inline callsite update |
| `0x1868880` | 12 KB | Inline threshold adjustment |
| `0x185FD30` | 5 KB | Argument materialization |
| `0x185E850` | 9 KB | Post-inline cleanup |
| `0x185B2A0` | 3 KB | Struct layout walk (depth 20) |
| `0x185D7C0` | 9 KB | Argument coercion check |
| `0x185B9F0` | 5 KB | Recursive operand simplification |
| `0x185CCC0` | 4 KB | Type compatibility check |
| `0x18612A0` | 65 KB | GlobalOpt integration |
| `0x1ACF5D0` | -- | Inline analysis state init |
| `0x1ACF600` | -- | Pre-analysis callee walk |
| `0x30DC7E0` | 51 KB | `InlineCostAnalysis::analyzeCall` (LLVM) |
| `0x2613930` | 69 KB | New PM CGSCC inliner |
| `0x2609820` | 57 KB | Inline advisor / ML inliner |
| `0x38576C0` | 58 KB | NVPTX target-specific cost modifier |
| `0x4DBEC0` | 14 KB | NVIDIA inliner knob registration |
| `0x58FAD0` | 27 KB | LLVM InlineCost option registration |
