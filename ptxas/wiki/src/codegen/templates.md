# Newton-Raphson & Math Templates

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

NVIDIA GPUs lack hardware integer dividers and native FP64 arithmetic units on the SFU. When ptxas encounters PTX operations such as `div.s32`, `div.u64`, `rcp.f64`, `sqrt.f64`, or `rsqrt.f64`, it expands them into multi-instruction SASS sequences that synthesize the result from simpler hardware primitives. These expansions are the **math templates** -- pre-built instruction sequence generators that emit 20--100+ SASS instructions per PTX operation, using the MUFU (Multi-Function Unit) for initial approximations and Newton-Raphson iterations for refinement.

The template subsystem lives at `0x1700000`--`0x172A090` in the ptxas binary: 36 functions occupying ~180 KB. It is invoked during instruction selection by the master lowering dispatcher `sub_AED3C0` whenever the selected instruction requires multi-instruction expansion.

| | |
|---|---|
| **Address range** | `0x1700000`--`0x172A090` |
| **Function count** | 36 (4 top-level handlers + 4 coordinators + ~24 sub-expanders + 4 helpers) |
| **Binary size** | ~180 KB |
| **Master lowering dispatcher** | `sub_AED3C0` (28 KB, vtable-dispatched) |
| **Emission primitives** | `sub_9314F0` (standard), `sub_934630` (extended), `sub_935130` (branch), `sub_9352C0` (wide) |
| **Virtual register allocator** | `sub_91BF30` (535 bytes, allocates 160-byte register descriptors) |
| **Immediate encoder** | `sub_91D160` (318 bytes, encodes constant values into operand descriptors) |
| **Operand legalizer** | `sub_13A6A10` (called before each expansion to widen immediates / fix register classes) |
| **Template name strings** | `__ori_template_DDIV1`, `__ori_template_DDIV2`, `__ori_template_DDIV3` |

## Architecture

### Two-Level Hierarchy

Every math template follows the same structural pattern: a **top-level handler** performs lazy initialization and operand legalization, then delegates to a **coordinator** that allocates virtual registers and calls a sequence of **sub-expanders**, each of which emits a portion of the final SASS instruction sequence.

```
sub_AED3C0 (Master Lowering Dispatcher, 28 KB)
  |
  +-- sub_170E8B0 (DDIV handler)        -- FP64 division
  |     +-- sub_170E260 (coordinator)    -- 298 vregs, 6 sub-expanders
  |
  +-- sub_1718D60 (DRCP/DSQRT handler)  -- FP64 reciprocal / square root
  |     +-- sub_1718790 (coordinator)    -- 289 vregs, 7 sub-expanders (inc. MUFU.RCP)
  |
  +-- sub_17276C0 (DRSQRT handler)      -- FP64 reciprocal square root
  |     +-- sub_1720D60 (coordinator A) -- 247 vregs, 5 sub-expanders (MUFU.RSQ path)
  |     +-- sub_1727130 (coordinator B) -- 59 vregs, integer div/mod path
  |
  +-- sub_1704070 (Inline DDIV handler) -- FP64 division, register-pressure variants
        +-- sub_1702990 (>20K regs)     -- full unrolled, ~50 instructions
        +-- sub_1701F10 (>16K regs)     -- partially spilled
        +-- sub_1701860 (<=16K regs)    -- minimal-register variant
```

### Lazy Initialization

Each top-level handler uses a lazy-init pattern to avoid rebuilding the template for every invocation within a compilation unit:

```c
// sub_170E8B0 -- DDIV handler (simplified from decompilation)
void DDIV_Handler(template_state *a1, instruction *a2) {
    if (a1->template_id == -1) {              // first invocation
        a1->template_id = ctx->next_id++;     // allocate unique ID
        DDIV_Coordinator(a1, ...);            // build template once
    }
    ctx->insert_point = a2->position;
    LegalizeOperand(ctx, a2, 1, ...);         // sub_13A6A10

    if (a1->use_template_call) {
        // Template path: emit BRA-to-template via EmitExtended slot 168
        // (Slot 168 in sub_934630's internal opcode-to-emitter table is the
        // BRA-to-named-section variant; this is NOT index 168 in the 322-entry
        // ROT13 SASS name table, where index 168 = FOOTPRINT. The actual SASS
        // BRA is opcode 67.)
        EmitExtended(ctx, 168, 0x13, ...);    // sub_934630
    } else {
        // Inline path: emit individual FP ops directly
        EmitFP(ctx, 0x86, 0xC, a1->reg[0], ...);  // sub_92E800
        EmitFP(ctx, 0x85, 0xC, a1->reg[1], ...);
    }
}
```

The `a1->use_template_call` flag (at offset +8) controls whether the expansion is emitted as a callable template (with `BRA` to a named code section) or inlined directly at the call site. The template-call path produces three named code objects -- `__ori_template_DDIV1`, `__ori_template_DDIV2`, `__ori_template_DDIV3` -- that are shared across all DDIV call sites in the same function.

### Coordinator Pattern

All four coordinators share identical structure. They allocate virtual registers from a static descriptor table, call the shared helper `sub_1701140` to build the code object scaffolding, then invoke their sub-expanders in sequence:

```c
// sub_170E260 -- DDIV coordinator (simplified)
void DDIV_Coordinator(template_state *a1, ..., int template_id) {
    int *vreg_array = NULL;
    int count = 0;

    // Allocate 298 virtual registers from static table dword_23993E0
    for (int i = 0; i < 298; i++) {
        int reg_id = AllocVReg(ctx, dword_23993E0[2*i]);  // sub_91BF30
        int category = dword_23993E4[2*i];                  // 0=output, 1=temp
        if (category == 0)
            output_regs[out_count++] = reg_id;
        else if (category == 1)
            temp_regs[temp_count++] = reg_id;
        // Mark register as template-owned
        *(vreg_table[reg_id] + 48) |= 0x40;
    }

    // Build code object scaffolding
    BuildTemplateScaffold(ctx, template_id, &static_table, 3, ...);

    // Name the three code sections
    if (a1->use_template_call) {
        section[0]->name = intern("__ori_template_DDIV1");
        section[1]->name = intern("__ori_template_DDIV2");
        section[2]->name = intern("__ori_template_DDIV3");
    }

    // Allocate 240-byte scratch buffer (zeroed)
    void *scratch = arena_alloc(240);
    memset(scratch, 0, 232);

    // Call 6 sub-expanders in sequence
    DDIV_Part1(a1, template_id, scratch, vreg_array, ...);  // sub_1704180
    DDIV_Part2(a1, template_id, scratch, vreg_array, ...);  // sub_1705820
    DDIV_Part3(a1, template_id, scratch, vreg_array, ...);  // sub_17075A0
    DDIV_Part4(a1, template_id, scratch, vreg_array, ...);  // sub_1709130
    DDIV_Part5(a1, template_id, scratch, vreg_array, ...);  // sub_170AE80
    DDIV_Part6(a1, template_id, scratch, vreg_array, ...);  // sub_170CBD0

    // Emit convergence barriers (Ori-IR opcode 0x5D = 93; in the 322-entry
    // ROT13 SASS name table this is OUT_FINAL/BHG_SVANY, but in the Ori IR
    // opcode 93 is repurposed as a control-flow terminator/convergence
    // barrier marker -- see ir/cfg.md for the (opcode & 0xFFFFFFFD == 0x5D)
    // test that matches both 93 and 95).
    for (each section boundary in static_table) {
        EmitBarrier(ctx, 0x5D, pred_reg, ...);  // sub_92E1B0
    }
    // Mark scheduling barriers at section endpoints
    *(section[23]->flags + 280) |= 8;
    *(section[42]->flags + 280) |= 8;
}
```

The static descriptor tables (`dword_23993E0` for DDIV, `dword_2398940` for DRCP/DSQRT, `dword_2398000` for DRSQRT, `dword_23976E0` for integer div) encode the register type and category for each virtual register used by the template. The category field (second element of each pair) classifies registers as output (0) or temporary (1).

## FP64 Division (DDIV)

Double-precision division `a / b` has no single-instruction implementation on any NVIDIA GPU. ptxas synthesizes it using Newton-Raphson refinement of a single-precision reciprocal seed.

### Algorithm and SASS Pseudocode

The DDIV template produces three code sections. The pseudocode below is reconstructed from the emission calls in `sub_1704180` (Part 1), `sub_1705820` (Part 2), and `sub_17075A0` (Part 3); register names like `v[N]` refer to virtual register indices from `dword_23993E0`.

```
; === DDIV1 (sub_1704180): seed generation ===
    MOV.HI   v[7],  b_hi           ; extract high 32 bits of divisor
    MOV.LO   v[8],  b_lo           ; extract low 32 bits
    PRMT     v[9],  v[7], 0x7610   ; extract exponent+mantissa fields
    LOP3.LUT v[10], v[7], 0x7FF00000, 0  ; isolate exponent (bits 30:20)
    IADD     v[11], v[10], -bias   ; remove exponent bias
    MOV      v[12], v[7]           ; save original high word
    ISETP.NE p0, v[10], 0          ; check if exponent is zero (denormal)
    ISETP.NE p1, v[10], 0x7FF00000 ; check if exponent is all-ones (inf/NaN)
    FSETP    p2, |b|, +inf         ; classify: is b infinity?
    SHR      v[13], v[7], 20       ; shift exponent to low bits
  @!p0 BRA   DENORMAL_PATH         ; branch if denormal
  @p1  BRA   SPECIAL_PATH          ; branch if inf/NaN

; === DDIV2 (sub_1705820): Newton-Raphson core ===
    MOV      v[97], v[13]          ; copy biased exponent to FP32 scratch
    MUFU.RCP v[99], v[97]          ; ~23-bit FP32 reciprocal seed: x0 ~ 1/b
    MOV      v[100], v[99]         ; save seed for next step

    ; -- Iteration 1: x1 = x0 * (2 - b * x0), implemented as two DFMAs --
    DFMA     v[65:66], v[27], v[16], {-x0} ; e1_lo = b * x0 - 1 (residual)
    MOV.NEG  v[66], v[65], 0x80000000      ; negate: -e1 (flip sign bit)
    MOV      v[67], 0                       ; zero low word
    IADD3    v[40], v[67], v[66], v[65]    ; combine correction words
    ;   ... exponent adjustment (IADD3, MOV pairs for carry propagation) ...
    ISETP    p3, v[40], threshold_lo       ; check if correction overflows
    ISETP    p4, v[40], threshold_hi
  @p3 BRA   ITER1_FIXUP

    DFMA     v[68:69], v[65:66], v[27], v[16] ; x1 = x0 + x0*e1 (refined)

    ; -- Iteration 2: x2 = x1 * (2 - b * x1), same DFMA pattern --
    ISETP    p5, ..., ...
  @p5 BRA   ITER2_FIXUP
    DFMA     v[79:80], v[7], v[68:69], {2^54}  ; scale: x1 * b at extended prec

    ; -- Result assembly --
    DMUL     v[79], v[7], v[85], {2^54}    ; 0x8B: final a * refined_rcp
    MOV.HI   v[80], v[86], v[79]           ; split into register pair
    MOV.LO   v[81], v[87], v[79]
    ;   ... 4 more ISETP/BRA pairs for overflow/underflow guard ...
    DMUL     v[85], v[9], v[88], {2^54}    ; second-precision multiply

; === DDIV3 (sub_17075A0 + sub_1709130): rounding + exceptions ===
    MUFU.RCP v[rcp2], v[scaled_b]          ; 0x3C: re-seed for correction term
    POPC     v[t], v[rcp2]                 ; 0x93: population count for rounding
    IMAD     v[..], v[rcp2], ...           ; 0x6E: integer mantissa fixup (x6)
    DMUL     v[result], v[a], v[rcp_final] ; 0x8B: final quotient = a * (1/b)
    ;   ... PRMT/LOP3/IADD3: IEEE 754 rounding logic ...
    ;   ... ISETP/BRA: overflow->inf, underflow->zero, NaN propagation ...
    MOV.HI   dst_hi, v[result_hi]          ; write output pair
    MOV.LO   dst_lo, v[result_lo]
```

The DDIV template emits ~100--120 SASS instructions across 6 sub-expanders, using 298 virtual registers. The two `DFMA` instructions (opcode 0x122) in Part 2 are the mathematical core -- each implements one Newton-Raphson step that doubles precision from the ~23-bit MUFU.RCP seed to ~46 bits (iteration 1) then to the 52+3 guard bits needed for IEEE-correct rounding. The `DMUL` instructions (opcode 0x8B) with the constant `0x4350000000000000` (2^54) perform the final scaled multiply of `a * (1/b)` at extended precision.

### SASS Instruction Sequence (sub_1705820)

The DDIV Part 2 sub-expander (`sub_1705820`, 7,545 bytes, 1,057 lines decompiled) is the largest single sub-expander and emits the core Newton-Raphson loop. The instruction mix from decompilation:

| SASS Opcode | Internal ID | Count | Role |
|---|---|---|---|
| IMAD | 0xC9 | 10 | Integer multiply-add for mantissa manipulation |
| FSETP | 0x97 | 6 | Floating-point set-predicate for branch conditions |
| MOV | 0x82 | 13 | Register-to-register moves |
| MOV (FP) | 0x0A | 10 | FP register moves with type annotation |
| IADD3 | 0x110 | 5 | Three-operand integer add for exponent arithmetic |
| SHR | 0x19 | 1 | Shift right for exponent extraction |
| BRA | 0x5F | 5 | Conditional branches for special-case handling |
| MUFU | 0x3C | 1 | `MUFU.RCP` -- the initial reciprocal seed |
| DFMA | 0x122 | 2 | FP64 fused multiply-add (Newton-Raphson iteration) |
| FP64 op | 0x8B | 2 | FP64 arithmetic (multiply or add) |
| FP32 hi/lo | 0x86/0x85 | 4+4 | Move FP32 halves of FP64 register pair |
| **Total** | | **~63** | Per sub-expander (Part 2 of 6) |

The complete DDIV template across all 6 sub-expanders emits approximately 100--120 SASS instructions, using 298 virtual registers.

### Register Pressure Variants

The inline DDIV handler (`sub_1704070`) selects between three implementations based on the target architecture's register file size at `*(*(context+1584) + 372)`:

| Register limit | Handler | Strategy |
|---|---|---|
| > 20,479 | `sub_1702990` (5,846 bytes) | Full unrolled -- maximum ILP, 14+ dedicated scratch registers |
| > 16,383 | `sub_1701F10` | Partially spilled -- trades some registers for spill/fill |
| <= 16,383 | `sub_1701860` | Minimal-register -- reuses registers aggressively, more instructions |

This three-tier approach is a register-pressure/throughput tradeoff: kernels with high register demand (and thus low occupancy) use the minimal variant, while kernels with register headroom use the fully unrolled variant for better instruction-level parallelism.

## FP64 Reciprocal and Square Root (DRCP/DSQRT)

The DRCP/DSQRT handler (`sub_1718D60`) shares the same lazy-init and template-call architecture as DDIV. Its coordinator (`sub_1718790`) allocates 289 virtual registers from `dword_2398940` and calls 7 sub-expanders:

| Sub-expander | Address | Role |
|---|---|---|
| Part 1 | `sub_170ED40` | FP64 reciprocal: extract exponent, compute MUFU.RCP seed |
| Part 2 | `sub_1710280` | Newton-Raphson iteration 1 for reciprocal refinement |
| Part 3 | `sub_17120F0` | Newton-Raphson iteration 2 (second doubling of precision) |
| Part 4 | `sub_17139D0` | Rounding and normalization |
| Part 5 | `sub_1715910` | Square root path: compute `MUFU.RSQ` seed, refine |
| Part 6 | `sub_1717470` | Final multiply `x * rsqrt(x)` to get `sqrt(x)`, exception handling |
| (shared) | `sub_1701140` | Template scaffolding helper (called by all coordinators) |

Both paths share the same coordinator and register pool (289 vregs from `dword_2398940`). The coordinator selects the DRCP path (Parts 1--4) or DSQRT path (Parts 5--6, then shared Parts 2--4) based on the original PTX operation being lowered.

### DRCP/DSQRT SASS Pseudocode

**DRCP** (Parts 1--4) mirrors DDIV's N-R core but outputs the reciprocal directly. Part 1 (`sub_170ED40`) has identical seed extraction (PRMT 0x15, LOP3.LUT 0x14, ISETP 0xC9, SHR 0x19, BRA 0x5F for denormal/inf/NaN guards). Part 2 (`sub_1710280`) emits the same MUFU.RCP + two-DFMA iteration pattern:

```
    MUFU.RCP x0, float32(b)       ; 0x3C: ~23-bit seed
    POPC     t, x0                 ; 0x93: mantissa parity for rounding
    IMAD     t, x0, ...            ; 0x6E: integer mantissa fixup (x4)
    DMUL     e0, b, x0             ; 0x8B: residual e0 = b*x0 - 1
    DFMA     x1, x0, -e0, x0      ; 0x122: N-R iter 1 -> ~46 bits
    DFMA     x2, x1, -e1, x1      ; 0x122: N-R iter 2 -> 52+ bits
    DMUL     result, x2, scale     ; 0x8B: normalize exponent
```

**DSQRT** (Parts 5--6) computes `sqrt(a) = a * rsqrt(a)` using integer-mantissa Goldschmidt iteration instead of DFMA. The `DNEG` instruction (opcode 0xB4, unique to sqrt/rsqrt paths) negates the input for residual computation:

```
    DNEG     neg_a, a              ; 0xB4: -a for residual
    IMAD     t, a_mant, ...        ; 0x6E: extract mantissa as integer
    I2F/F2I  y0, t                 ; 0xD5/0xD6: round-trip normalization for seed
    IMAD     y0sq, y0, y0, 0       ; 0x6E: y0^2 in integer domain
    IMAD     ay2, y0sq, a_mant, 0  ; 0x6E: a * y0^2
    IMAD     y1, y0, corr, 0       ; 0x6E: y1 = y0*(3 - a*y0^2)/2
    ;   ... IMAD chain: sqrt = a * y1 in integer mantissa domain ...
```

DSQRT uses `IMAD` chains (~4-cycle latency) rather than `DFMA` (~30-cycle latency), trading more instructions for lower total latency when the scheduler keeps the integer ALU busy.

## FP64 Reciprocal Square Root (DRSQRT)

The DRSQRT handler (`sub_17276C0`) is the most complex top-level handler. It dispatches to one of two coordinators based on a hardware capability flag:

```c
// sub_17276C0 -- DRSQRT handler (simplified)
void DRSQRT_Handler(template_state *a1, instruction *a2) {
    int hw_flag = *(*(ctx + 1584) + 1037) & 1;

    if (a1->template_id == -1) {
        a1->template_id = ctx->next_id++;
        if (hw_flag)
            Coordinator_IntDiv(a1, ...);    // sub_1727130: 59 vregs
        else
            Coordinator_DRSQRT(a1, ...);    // sub_1720D60: 247 vregs
    }
    // ... operand legalization, template call or inline emission
}
```

The hardware flag at `*(config + 1037) & 1` likely distinguishes architectures with enhanced SFU precision (where fewer refinement iterations are needed) from older architectures requiring the full Newton-Raphson sequence.

**Coordinator A** (`sub_1720D60`): allocates 247 virtual registers from `dword_2398000` and calls 5 sub-expanders:

| Sub-expander | Address | Role |
|---|---|---|
| Part 1 | `sub_1719080` | Initial `MUFU.RSQ` seed, exponent extraction |
| Part 2 | `sub_171A260` | Newton-Raphson iteration 1 |
| Part 3 | `sub_171BB80` | Newton-Raphson iteration 2 |
| Part 4 | `sub_171D3A0` | Normalization and rounding |
| Part 5 | `sub_171EFD0` | Exception handling (NaN, infinity, negative, zero) |

### DRSQRT SASS Pseudocode

Reconstructed from `sub_1719080` (Part 1) and `sub_171A260` (Part 2). DRSQRT outputs `1/sqrt(a)` directly, using the same integer-mantissa Goldschmidt approach as DSQRT but without the final `a * rsqrt(a)` multiply.

```
; === Part 1 (sub_1719080): rsqrt seed via IMAD chain ===
    MOV      t0, a_hi              ; 0x82: copy FP64 high word
    DNEG     neg_a, a              ; 0xB4: -a for residual computation
    POPC     t1, t0                ; 0x93: mantissa parity
    IMAD     t2, t0, mask, 0       ; 0x6E: extract mantissa as integer (x3)
    IMAD     t3, t2, t2, 0         ; 0x6E: t3 = t2^2 (integer squaring for seed)
; === Part 2 (sub_171A260): N-R iteration 1 ===
    IMAD     r0, y0, y0, 0         ; 0x6E: y0^2
    IMAD     r1, r0, a_mant, 0     ; 0x6E: a * y0^2
    IADD     r2, r1, correction    ; 0x02: (3 - a*y0^2)
    ISETP    p0, r2, threshold     ; 0xC9: overflow guard
  @p0 BRA   DRSQRT_FIXUP1
    MOV      y1, r2                ; 0x82: refined estimate
    ;   ... SSY (0xBC) for convergence after special-case BRA ...
    ; Parts 3-4: second iteration y2 = y1*(3-a*y1^2)/2, then rounding
```

The DRSQRT template emits 65 instructions across Parts 1--2. `IMAD` (0x6E) chains dominate: the integer mantissa multiply approach avoids `DFMA` (~30-cycle latency) in favor of `IMAD` (~4-cycle). `DNEG` (0xB4) and `SSY` (0xBC) are unique to the rsqrt paths; `SSY` sets a convergence point for the special-case branches in Part 5.

**Coordinator B** (`sub_1727130`): allocates only 59 virtual registers from `dword_23976E0` and dispatches to the integer division sub-expanders (`sub_1724A20` for 32-bit, `sub_1728930` for 64-bit unsigned, `sub_1727AC0` for 64-bit signed). This path handles the integer division/modulo lowering via `sub_1729B50`.

## Integer Division Lowering

Integer division and modulo by variable (non-constant) values are expanded into multi-instruction SASS sequences during instruction selection. These sequences use the MUFU.RCP hardware approximation as a starting point, then correct the result with integer arithmetic.

### 32-bit Division -- `sub_1724A20`

**Size:** 28,138 bytes decompiled (957 lines), the largest function in the `0x1723000`--`0x17F8000` range.
**Called from:** `sub_1727130` (coordinator B).
**Virtual registers:** 59 (allocated by coordinator B from `dword_23976E0`).
**Temporary pool:** indices 90--126 of the parameter array, providing 37 dedicated scratch registers.

Algorithm for unsigned 32-bit `a / b`:

```
Step 1:  float_b = I2F(b)                    ; convert divisor to FP32
Step 2:  rcp     = MUFU.RCP(float_b)          ; ~23-bit reciprocal approximation
Step 3:  int_rcp = F2I(rcp)                   ; convert back to integer
Step 4:  q_est   = IMAD.HI(a, int_rcp, 0)    ; estimated quotient (high 32 bits of a*rcp)
Step 5:  r_est   = IMAD(q_est, -b, a)        ; estimated remainder = a - q*b
Step 6:  if (r_est >= b) q_est++; r_est -= b  ; correction iteration 1
Step 7:  if (r_est >= b) q_est++; r_est -= b  ; correction iteration 2
Step 8:  result  = q_est                       ; (or r_est for modulo)
```

The correction steps (6--7) are implemented with `ISETP` (opcode 0xC9) for comparison and `BRA` (opcode 0x5F) for conditional execution. In the worst case, two correction iterations suffice because the MUFU.RCP approximation is accurate to within 2 ULP of the true reciprocal.

Key constants allocated via `sub_91D160`:

| Constant | Purpose |
|---|---|
| 23 | Float exponent bias for mantissa extraction |
| 255 | Exponent mask (8-bit IEEE 754 exponent field) |
| 127 | IEEE 754 single-precision exponent bias |
| 254 | Double-bias for overflow guard (`2 * 127`) |
| 1, -1 | Correction increments for quotient adjustment |

The complete SASS instruction mix for the 32-bit division template:

| SASS Opcode | Internal ID | Count | Role |
|---|---|---|---|
| I2F | 0xD5 | 2 | Integer-to-float conversion |
| F2I | 0xD6 | 3 | Float-to-integer conversion |
| IMAD | 0x6E | 5 | Integer multiply-add (quotient estimation) |
| IMAD.WIDE | 0x6F | 3 | Wide multiply-add (64-bit intermediate) |
| IADD | 0x02 | ~3 | Integer add (correction) |
| MOV | 0x82 | 10 | Register moves |
| MOV (typed) | 0x0A | 6 | Typed register moves |
| ISETP | 0xC9 | 8 | Integer set-predicate (comparison) |
| FSETP | 0x97 | 3 | Float set-predicate |
| SHL/LEA | 0x24 | 2 | Shift-left / load effective address |
| BRA | 0x5F | 4 | Conditional branch (correction paths) |
| POPC/LOP | 0x93 | 1 | Population count / logic op |
| **Total** | | **~50** | |

### 64-bit Division

Two variants handle 64-bit operands, both called sequentially from `sub_1729B50` (signed first, then unsigned):

#### Unsigned 64-bit -- `sub_1728930`

**Size:** 16,545 bytes decompiled (634 lines). Emits 41 SASS instructions (including 4 branches).
**Temporary pool:** indices 29--58 of the parameter array, providing 30 dedicated scratch registers.

Algorithm for unsigned 64-bit `{a_hi, a_lo} / {b_hi, b_lo}`:

```
Phase 1 -- Normalization check (4 insns + 1 branch)
  MOV      t0 = a_hi                            ; 0x82: copy dividend high half
  MOV.T    t1 = t0, 0x7FFFFFFF                  ; 0x0A: mask sign for magnitude
  ISETP    p0, t1 >= b_hi                        ; 0xC9: divisor fits 32 bits?
  MOV      t2 = a_lo                             ; 0x82: copy dividend low half
  BRA      @p0 -> skip_to_Phase3                 ; 0x5F: shortcut if b_hi != 0

Phase 2 -- Divisor-fits-32-bit fast path (3 insns + 1 branch)
  MOV      t3 = t2                               ; 0x82: copy divisor_lo
  MOV      t4 = 0                                ; 0x82: zero-extend
  ISETP    p1, t3 == t4                          ; 0xC9: divisor-is-zero guard
  SHR      t5 = normalize(b_lo)                  ; 0x19: align MSB for FP seed
  BRA      @p1 -> skip_to_Phase4                 ; 0x5F

Phase 3 -- FP reciprocal seed (4 insns + 1 branch)
  MOV      t6 = 0x7FFFFFFF                       ; 0x82: INT32_MAX exponent guard
  MOV      t7 = 0x7F800000                       ; 0x82: +Inf overflow sentinel
  MUFU.RCP rcp = reciprocal(b_normalized)        ; 0x3C: ~23-bit HW approximation
  ISETP    p2, rcp >= +Inf                       ; 0xC9: overflow check
  MOV      t8 = rcp                              ; 0x82: save reciprocal seed
  BRA      @p2 -> overflow_path                  ; 0x5F

Phase 4 -- Refine and scale reciprocal (8 insns + 1 branch)
  MOV      t9  = 0x3F800000                      ; 0x82: 1.0f for FP correction
  IADD     rcp = rcp + 1_ULP                     ; 0x02: bump by 1 ULP
  MOV      t10 = 0x7F800000                      ; 0x82: +Inf for second guard
  ISETP    p3, rcp >= +Inf                       ; 0xC9: second overflow test
  SHR      scale = extract_exponent(rcp)         ; 0x19: integer quotient scale
  BRA      @p3 -> overflow_fixup                 ; 0x5F
  MOV      t12 = rcp_adjusted                    ; 0x82
  MOV      t13 = 0                               ; 0x82: carry init
  MOV      t14 = 0x5F800000                      ; 0x82: 2^64 as FP32

Phase 5 -- Double-width quotient estimation (10 insns)
  IMAD     q_seed = rcp * a_hi + rcp_scaled      ; 0x6E: initial q estimate
  MOV      t15 = q_seed                          ; 0x82
  MOV      t16 = 0x2F800000                      ; 0x82: 2^-32 (scale-down)
  MOV      t17 = 0x3F000000                      ; 0x82: 0.5f (rounding bias)
  PRMT     t18 = byte_permute(q_seed)            ; 0xC0: extract hi/lo halves
  IMAD.W   {h,l} = wide(t18, t16, ...)           ; 0x8B: q refinement (wide)
  IMAD.W   {h,l} = wide(t18, q_seed, ...)        ; 0x8B: second refinement
  LOP      carry = merge_carry(...)              ; 0x93: combine carry from wides
  IMAD     q_cor = correction(...)               ; 0x6E: fix-up multiply-add
  IMAD     r_est = a - q*b                       ; 0x6E: remainder estimate

Phase 6 -- Final assembly and output (4 insns + conditional tail)
  IMAD.W   {r_hi,r_lo} = wide_remainder_check    ; 0x8B: double-width r verify
  MOV      q_out = final_quotient                ; 0x82
  MOV      r_out = final_remainder               ; 0x82
  MOV/CALL output = select(div ? q : r)          ; 0x82 or 0xA8: mode-dependent
  RET                                            ; 0xBC: return from template
```

The `a1+8` byte flag at the template tail selects division vs. modulo: when set, the result is returned via a `CALL`-variant emitter (`sub_934630`, opcode 0xA8); when clear, a plain `MOV` copies the result.

Key constants allocated via `sub_91D160`:

| Hex literal | IEEE 754 value | Purpose |
|---|---|---|
| `0x7FFFFFFF` | INT32_MAX | Magnitude mask / exponent guard |
| `0x7F800000` | +Inf | Reciprocal overflow sentinel |
| `0x3F800000` | 1.0f | Unity for ULP correction |
| `0x5F800000` | 2^64 (1.845e19) | Scale factor for 64-bit range |
| `0x2F800000` | 2^-32 (2.328e-10) | Upper-half extraction scale |
| `0x3F000000` | 0.5f | Rounding bias |
| `0x00000000` | 0 | Zero initializer / carry seed |

Complete SASS instruction mix:

| SASS Opcode | Internal ID | Count | Role |
|---|---|---|---|
| MOV | 0x82 | 17 | Register copies and constant loads |
| MOV (typed) | 0x0A | 1 | Typed move with immediate mask |
| ISETP | 0xC9 | 4 | Integer predicate comparisons |
| BRA | 0x5F | 4 | Conditional branches (phase skips) |
| IMAD | 0x6E | 3 | Integer multiply-add |
| IMAD.WIDE | 0x8B | 3 | 64-bit wide multiply-add |
| SHR | 0x19 | 2 | Shift right (normalize, scale) |
| MUFU.RCP | 0x3C | 1 | Hardware reciprocal seed |
| IADD | 0x02 | 1 | ULP correction add |
| PRMT | 0xC0 | 1 | Byte permute (half extraction) |
| LOP | 0x93 | 1 | Logic op (carry merge) |
| RET | 0xBC | 1 | Template return |
| MOV/CALL | 0x82/0xA8 | 1 | Div-vs-mod output select |
| **Total** | | **41** | |

#### Signed 64-bit -- `sub_1727AC0`

**Size:** 13,776 bytes decompiled (538 lines). Emits 36 instructions (including 1 branch).

The signed variant does NOT call `sub_1728930` -- it inlines its own equivalent unsigned core wrapped with sign handling:

```
Phase A -- Sign extraction and absolute value (10 insns + 1 branch)
  MOV      a_hi = dividend_hi                   ; 0x82 x4: copy both halves
  MOV      a_lo = dividend_lo                   ;   of both operands
  MOV      b_hi = divisor_hi
  MOV      b_lo = divisor_lo
  MOV      guard = 0x727FFFFF                   ; 0x82: ~2^102 sign-safe limit
  IADD     abs_a = |dividend|                   ; 0x02: negate if MSB set
              ; uses const 0x0D000000 (~2^-101) as correction offset
  ISETP    p_neg, sign(a_hi XOR b_hi)           ; 0xC9: quotient sign = XOR
  MOV      copy for unsigned core               ; 0x82
  BRA      @p_neg -> fixup_path                 ; 0x5F

Phase B -- Inlined unsigned core (19 insns)
  ; Same MUFU.RCP -> IMAD -> IMAD.WIDE -> LOP -> IMAD correction
  ; sequence as sub_1728930 Phases 3-6, operating on |a| / |b|.
  ; Opcodes: I2F(0xC0), IMAD.WIDE(0x8B x2), IMAD(0x6E x2),
  ;          LOP(0x93), MOV(0x82 x8), ISETP/BRA for overflow.

Phase C -- Sign fixup and output (6 insns)
  MOV      q_lo = unsigned_q_lo                 ; 0x82: copy result pair
  MOV      q_hi = unsigned_q_hi                 ; 0x82
  MOV      zero = 0                             ; 0x82: negation identity
  MOV      copy                                 ; 0x82
  MOV/CALL output = negate_if(p_neg, q)         ; 0x82/0xA8: conditional negate
  RET                                           ; 0xBC
```

Together, `sub_1727AC0` (36) + `sub_1728930` (41) = 77 SASS instructions across both signed and unsigned paths (~80 including branch-target labels). Both allocate from the same 59-register pool managed by coordinator B.

### Division by Constant

Division by compile-time constant is handled separately during the `GeneralOptimize` bundle passes (not by these templates). The classic Granlund-Montgomery magic-number technique converts `x / C` to `MULHI(x, magic) >> shift`, producing 2--3 instructions instead of ~50. See [Strength Reduction](../passes/strength-reduction.md) for details.

## MUFU: The Hardware Approximation Engine

All math templates depend on the MUFU (Multi-Function Unit) instruction, which provides low-precision hardware approximations for transcendental and special functions. MUFU is a single SASS instruction (internal opcode 0x3C) with a sub-function selector:

| MUFU Sub-function | Operation | Precision | Latency (typical) |
|---|---|---|---|
| `MUFU.RCP` | 1/x (reciprocal) | ~23 bits (FP32 mantissa) | ~8 cycles |
| `MUFU.RSQ` | 1/sqrt(x) (reciprocal square root) | ~23 bits | ~8 cycles |
| `MUFU.RCP64H` | High-precision 1/x seed for FP64 | ~28 bits (sm_80+) | ~10 cycles |
| `MUFU.RSQ64H` | High-precision 1/sqrt(x) seed for FP64 | ~28 bits (sm_80+) | ~10 cycles |
| `MUFU.SIN` | sin(x) | ~23 bits | ~8 cycles |
| `MUFU.COS` | cos(x) | ~23 bits | ~8 cycles |
| `MUFU.EX2` | 2^x (base-2 exponential) | ~23 bits | ~8 cycles |
| `MUFU.LG2` | log2(x) (base-2 logarithm) | ~23 bits | ~8 cycles |
| `MUFU.SQRT` | sqrt(x) (sm_89+) | ~23 bits | ~8 cycles |

MUFU executes on the SFU (Special Function Unit), which is separate from the integer and floating-point ALU pipelines. On sm_80 (Ampere) and later, the SFU can execute one MUFU per cycle per SM partition. The key insight is that MUFU provides only FP32-precision seeds; achieving FP64 precision requires the Newton-Raphson refinement implemented by the math templates.

### Fast-Math vs. IEEE Precision

For FP32 operations, the PTX modifiers `.approx` and `.ftz` control whether ptxas uses MUFU directly or applies refinement:

- **`div.approx.f32`**: Emits a single `MUFU.RCP` followed by `FMUL`. No Newton-Raphson. Result has ~23-bit precision (not IEEE-correct rounding).
- **`div.full.f32`**: Emits `MUFU.RCP` + one Newton-Raphson iteration via FFMA. Result is IEEE-correct for all normal inputs.
- **`div.rn.f64`**: Emits the full DDIV template (~100+ instructions) with two Newton-Raphson iterations. Result is IEEE 754 round-to-nearest-even.

For transcendental functions (`sin`, `cos`, `exp`, `log`):
- **`sin.approx.f32`** / **`cos.approx.f32`**: Single `MUFU.SIN` / `MUFU.COS`. ~23-bit precision over a reduced range.
- **`sin.f32`** (full range): Range reduction to [-pi, pi] via polynomial argument reduction, then `MUFU.SIN` + polynomial correction. Emitted as a libdevice call or inline sequence depending on optimization level.
- **`ex2.approx.f32`**: Single `MUFU.EX2`.
- **`lg2.approx.f32`**: Single `MUFU.LG2`.

There are no FP64 versions of `MUFU.SIN/COS/EX2/LG2`. FP64 transcendentals are always implemented by linking against libdevice (the CUDA math library), which provides polynomial approximation sequences compiled from C source code. These are not handled by ptxas's internal templates but by the libdevice bitcode linked during `cicc` compilation, upstream of ptxas.

## Template Instantiation Infrastructure

### Emission Primitives

The sub-expanders construct SASS instructions using a family of emission functions:

| Function | Size | Signature | Role |
|---|---|---|---|
| `sub_9314F0` | 403 bytes | `(scratch, ctx, opcode, type, operand_count, operands, xmm, fp)` | Standard SASS instruction emission (2--5 operands) |
| `sub_934630` | 1,213 bytes | `(scratch, ctx, opcode, type, ?, ?, xmm, fp, operand_buf, count)` | Extended emission for control flow and >4 operands |
| `sub_935130` | 390 bytes | `(scratch, ctx, opcode, count, label_buf, label_count, ...)` | Branch emission with label resolution |
| `sub_9352C0` | (variant) | `(scratch, ctx, opcode, type, operands, count, ..., extra_buf, ...)` | Wide emission with extra operand buffer (used for MUFU) |
| `sub_92E800` | 70 bytes | `(scratch, ctx, opcode, type, reg_id, src_operand, xmm, fp)` | Simplified emission for single-source FP ops |
| `sub_92E720` | 51 bytes | `(scratch, ctx, opcode, type, dest_pair, src_operand, xmm, fp)` | Simplified emission wrapper for register pairs |
| `sub_92E1B0` | (variant) | `(scratch, ctx, opcode, pred_reg, xmm, fp)` | Predicated barrier/convergence emission |

### Operand Encoding

Each operand in the emission buffer is a 32-bit tagged value:

| Tag (bits 31:24) | Meaning |
|---|---|
| `0x90` | Destination register (bits 23:0 = register ID) |
| `0x10` | Source register |
| `0x20` | Immediate constant (from constant pool via `sub_91D160`) |
| `0x40` | External constant reference |
| `0x60` | Template call target / sentinel (used for BRA-to-template) |
| `0x80` | Negate modifier (OR'd onto source tag: `0x90` = negated source) |

The 64-bit modifier word `0x6000000500000000` appearing in many emission calls encodes instruction-level flags such as `.reuse` hints and type specifiers.

### Virtual Register Allocation

Each coordinator allocates its full set of virtual registers in a single loop before any instructions are emitted. The `sub_91BF30` allocator creates 160-byte register descriptors and returns a 24-bit register ID. Each register is marked with `flags |= 0x40` (bit 6) to indicate it is owned by a template rather than the main register allocation pass. This prevents the register allocator from coalescing or splitting template-internal registers.

The static descriptor tables encode register types as the first element of each `(type, category)` pair:

| Template | Table address | Register count | Data types |
|---|---|---|---|
| DDIV | `dword_23993E0` | 298 | FP64 pairs, FP32, integer, predicate |
| DRCP/DSQRT | `dword_2398940` | 289 | FP64 pairs, FP32, integer, predicate |
| DRSQRT | `dword_2398000` | 247 | FP64 pairs, FP32, integer, predicate |
| Integer div | `dword_23976E0` | 59 | Integer (32/64-bit), predicate |

The register counts explain why these templates dominate register pressure in FP64-heavy kernels: 298 virtual registers for a single DDIV expansion is enormous by GPU standards, where the entire physical register file is 65,536 32-bit registers shared across all active warps.

### Template Call vs. Inline

The `use_template_call` flag at `template_state + 8` selects between two emission strategies:

**Template-call path** (flag set):
- The coordinator builds three named code sections (`__ori_template_DDIV1/2/3`)
- Each call site emits a `BRA` (opcode 168 via `sub_934630`) to the template code
- The template code is shared across all call sites in the same function
- Convergence barriers (opcode 0x5D via `sub_92E1B0`) ensure correct re-convergence
- A `CALL`-like instruction (opcode 164) handles the return path

**Inline path** (flag clear):
- The sub-expander instructions are emitted directly at the call site
- Each call site gets its own copy of the full instruction sequence
- Uses direct `IADD3` (opcode 0x110) for control flow instead of `BRA`
- No named code sections, no convergence barriers
- A `JMP`/`BRA` (opcode 0x20 or 0x5F) replaces the template return

#### Decision Algorithm

The flag is computed once during lazy initialization of the 88-byte template state object (first encounter of a template-eligible instruction in the function). The decision is **not count-based** -- it does not depend on how many DDIV/DRCP/DSQRT operations appear. It is driven purely by compilation-context flags:

```c
// sub_AED3C0 template state init (identical in cases 48, 49, 138, 180, 222)
template_state = allocate(88);
*(uint64_t *)(template_state + 0) = ctx;           // back-pointer
*(int32_t *)(template_state + 12) = -1;            // template_id (uninitialized)
*(int32_t *)(template_state + 40) = -1;            // section_id_1
*(int32_t *)(template_state + 56) = -1;            // section_id_2
*(int32_t *)(template_state + 76) = -1;            // section_id_3

bool is_recompile = (*(int32_t *)(ctx + 896) == 5);
uint8_t use_call = previous_iteration_value;       // default 0 on first pass
if (is_recompile)
    use_call = (~*(uint8_t *)(ctx + 1412)) >> 7;   // inverted bit 7

*(uint8_t *)(template_state + 8) = use_call;       // use_template_call flag
*(uint8_t *)(template_state + 9) = (*(int32_t *)(ctx + 352) >= 0);  // opt_level_nonneg
```

The two inputs:

| Field | Offset | Meaning |
|---|---|---|
| `compilation_strategy` | `ctx + 896` | Internal strategy selector (values 0--5). Value 5 = recompilation trial: ptxas is re-lowering the function after a failed register allocation attempt. |
| `compilation_flags_byte` | `ctx + 1412` | Bit 7 (sign bit): when set, marks the function for forced-inline expansion (e.g. device-debug mode, or a prior pass determined inlining is cheaper). The expression `(~flags >> 7)` yields 1 when bit 7 is clear (template-call allowed), 0 when set (forced inline). |

The pre-scheduling pass `sub_7753F0` applies the same logic with an additional gate:

```c
// sub_7753F0 lines 538-541
bool enable_template_calls = false;
if ((*(uint8_t *)(ctx + 1420) & 1) == 0      // no forced-inline-all flag
    || (*(uint32_t *)(ctx + 900) - 1) > 1)   // pass_id not in {1, 2}
    enable_template_calls = (*(int32_t *)(ctx + 896) == 5);
```

**Summary**: template-call mode activates only when (a) the compilation strategy is 5 (recompilation after regalloc failure), (b) the function is not marked for forced-inline expansion (bit 7 of `ctx+1412` clear), and (c) the forced-inline-all flag at `ctx+1420` is unset or the current pass is not an early trial. On a normal first-pass compilation (strategy != 5), all math templates are inlined unconditionally. The call path appears only during recompilation, where code-size sharing across multiple call sites offsets the convergence-barrier overhead and the extra register pressure from the template-call ABI.

## SM-Specific Variants

### Register File Size Dispatch

The inline DDIV handler (`sub_1704070`) reads the register file capacity from `*(*(context + 1584) + 372)` and selects between three implementation tiers:

```c
int reg_file_size = *(*(ctx + 1584) + 372);  // physical register count
if (reg_file_size > 20479)
    DDIV_FullUnroll(a1, a2, ...);     // sub_1702990: max ILP
else if (reg_file_size > 16383)
    DDIV_PartialSpill(a1, a2, ...);   // sub_1701F10: balanced
else
    DDIV_MinimalRegs(a1, a2, ...);    // sub_1701860: min pressure
```

The thresholds (20,479 and 16,383) correspond to register file sizes across GPU generations:
- sm_50--sm_61 (Maxwell/Pascal): 65,536 registers per SM -> 20,479 threshold met at occupancy < 3 blocks
- sm_70--sm_89 (Volta through Ada): 65,536 registers -> same thresholds
- sm_100+ (Blackwell): 65,536 registers -> same, but wider warp execution changes the pressure calculus

### Hardware Capability Flag

The DRSQRT handler checks `*(*(context + 1584) + 1037) & 1` to select between coordinator A (full Newton-Raphson, 247 registers) and coordinator B (reduced sequence, 59 registers). This flag likely indicates the presence of `MUFU.RCP64H` / `MUFU.RSQ64H` on sm_80+ architectures, which provide higher-precision seeds (~28 bits vs. ~23 bits) and thus require fewer refinement iterations.

## SASS Opcode Reference

Internal opcode IDs used by the math templates, mapped to SASS mnemonics:

| Internal ID | SASS Mnemonic | Description |
|---|---|---|
| 0x02 | IADD | Integer add (3-operand) |
| 0x0A | MOV | FP register move (typed) |
| 0x19 | SHR | Shift right (exponent extraction) |
| 0x20 | BRA/JMP | Unconditional branch (inline return path) |
| 0x24 | SHL/LEA | Shift left / load effective address |
| 0x3C | MUFU | Multi-function unit (RCP, RSQ, SIN, COS, EX2, LG2) |
| 0x5D | BSYNC | Barrier synchronization / convergence barrier |
| 0x5F | BRA | Conditional branch |
| 0x6E | IMAD | Integer multiply-add |
| 0x6F | IMAD.WIDE | Wide integer multiply-add (64-bit result) |
| 0x82 | MOV | General register move |
| 0x85 | MOV.LO | Move low 32 bits of FP64 pair |
| 0x86 | MOV.HI | Move high 32 bits of FP64 pair |
| 0x8B | DFMA/DMUL | FP64 fused multiply-add or multiply |
| 0x93 | POPC | Population count / bitwise logic |
| 0x97 | FSETP | FP32 set-predicate (comparison) |
| 0xA8 | SEL | Conditional select |
| 0xC9 | ISETP | Integer set-predicate (comparison) |
| 0xD5 | I2F | Integer-to-float conversion |
| 0xD6 | F2I | Float-to-integer conversion |
| 0x110 | IADD3 | Three-operand integer add |
| 0x122 | DFMA | FP64 fused multiply-add (Newton-Raphson core) |

## Function Map

| Address | Size | Function | Role |
|---|---|---|---|
| `sub_AED3C0` | 28 KB | Master lowering dispatcher | Vtable-dispatched, calls all template handlers |
| `sub_170E8B0` | 1,166 bytes | DDIV top-level handler | Lazy init, operand legalization, template-call/inline dispatch |
| `sub_170E260` | 1,615 bytes | DDIV coordinator | 298 vregs, 6 sub-expanders, names `__ori_template_DDIV1/2/3` |
| `sub_1704180` | ~5 KB | DDIV sub-expander 1 | Initial reciprocal approximation |
| `sub_1705820` | 7,545 bytes | DDIV sub-expander 2 | Newton-Raphson core (MUFU.RCP + refinement) |
| `sub_17075A0` | ~6 KB | DDIV sub-expander 3 | Second refinement iteration |
| `sub_1709130` | ~6 KB | DDIV sub-expander 4 | Final multiply (a * 1/b) |
| `sub_170AE80` | ~6 KB | DDIV sub-expander 5 | Rounding and normalization |
| `sub_170CBD0` | ~5 KB | DDIV sub-expander 6 | Exception handling |
| `sub_1718D60` | 790 bytes | DRCP/DSQRT top-level handler | Lazy init, shares structure with DDIV |
| `sub_1718790` | 1,487 bytes | DRCP/DSQRT coordinator | 289 vregs, 7 sub-expanders |
| `sub_170ED40` | ~5 KB | DRCP/DSQRT sub-expander 1 | MUFU.RCP seed extraction |
| `sub_1710280` | ~5 KB | DRCP/DSQRT sub-expander 2 | Newton-Raphson iteration 1 |
| `sub_17120F0` | ~6 KB | DRCP/DSQRT sub-expander 3 | Newton-Raphson iteration 2 |
| `sub_17139D0` | ~6 KB | DRCP/DSQRT sub-expander 4 | Rounding/normalization |
| `sub_1715910` | ~6 KB | DRCP/DSQRT sub-expander 5 | DSQRT path (MUFU.RSQ) |
| `sub_1717470` | ~5 KB | DRCP/DSQRT sub-expander 6 | Final multiply, exceptions |
| `sub_17276C0` | 1,011 bytes | DRSQRT top-level handler | HW capability dispatch |
| `sub_1720D60` | 1,423 bytes | DRSQRT coordinator A | 247 vregs, 5 sub-expanders (full N-R) |
| `sub_1719080` | ~5 KB | DRSQRT sub-expander 1 | MUFU.RSQ seed |
| `sub_171A260` | ~6 KB | DRSQRT sub-expander 2 | Newton-Raphson iteration 1 |
| `sub_171BB80` | ~6 KB | DRSQRT sub-expander 3 | Newton-Raphson iteration 2 |
| `sub_171D3A0` | ~6 KB | DRSQRT sub-expander 4 | Normalization |
| `sub_171EFD0` | ~5 KB | DRSQRT sub-expander 5 | Exception handling |
| `sub_1727130` | 1,423 bytes | Integer div coordinator (B) | 59 vregs, dispatches to div templates |
| `sub_1724A20` | 28,138 bytes | 32-bit integer div/mod | Newton-Raphson via MUFU.RCP + IMAD correction |
| `sub_1728930` | 16,545 bytes | 64-bit unsigned div/mod | Double-width Newton-Raphson |
| `sub_1727AC0` | 13,776 bytes | 64-bit signed div/mod | Signed wrapper around unsigned |
| `sub_1729B50` | ~2 KB | 64-bit div dispatcher | Selects signed vs. unsigned handler |
| `sub_1704070` | 263 bytes | Inline DDIV dispatcher | Register-pressure based 3-tier selection |
| `sub_1702990` | 5,846 bytes | Inline DDIV (full unroll) | >20K register variant |
| `sub_1701F10` | ~4 KB | Inline DDIV (partial spill) | >16K register variant |
| `sub_1701860` | ~3 KB | Inline DDIV (minimal regs) | <=16K register variant |
| `sub_1701140` | 8,690 bytes | Template scaffolding helper | Code object construction, called by all coordinators |
| `sub_172A090` | 3,095 bytes | Conditional move emission | Scheduling barrier fixup |

## Cross-References

- [Code Generation Overview](overview.md) -- pipeline context showing templates as step 5 of 7
- [Strength Reduction](../passes/strength-reduction.md) -- division-by-constant optimization (Granlund-Montgomery), peephole patterns
- [Instruction Selection](isel.md) -- ISel mega-selector and pattern matchers that feed into template expansion
- [SASS Instruction Encoding](encoding.md) -- `sub_7B9B80` bitfield packer used by encoding tables
- [Peephole Optimization](peephole.md) -- post-template simplification of emitted sequences
- [Register Model](../ir/registers.md) -- virtual register allocation and the `0x40` template-ownership flag
- [Scheduling](../scheduling/overview.md) -- scheduling of template-emitted instruction sequences
