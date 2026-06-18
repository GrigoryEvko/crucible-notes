# BIR Simulator: Activation & the PWP Numeric Model

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). Two binaries are in play. `libpwp_sim.so` (md5 `9bd71c8e75e2e002073b52d03c8b9fab`, BuildID-sha1 `c0b9dc4a217f3626d34ef08b5830619064ae7c55`) is the numeric model — **not** stripped, full DWARF + demangled symbols, 535 functions, all decompiled (the "failures" are PLT/GOT stubs); for its `.text`/`.rodata`, virtual address equals file offset (string `@0x17058` == fileoff `0x17058`). `libBIRSimulator.so` (md5 `f3acdcba9176056cb50daac01389dd13`) is the interpreter that delegates to it; the same `.data` `+0x1000` VMA→file-offset delta documented in [`sim-dispatch-state`](sim-dispatch-state.md) applies there. Every `libpwp_sim` address below is CERTAIN by symbol, not inference. Other wheels differ; treat each address as version-pinned.*

## Abstract

The activation engine is the one BIR opcode family whose *numerics* the simulator does not compute inline. When `birsim::InstVisitor` reaches a generic `InstActivation` (`IT 4`), it converts the activation enum to a **name string**, then hands the per-element math to a second shared object — `libpwp_sim.so`, the `PWPSim::Simulator` class. That class is a self-contained reference model for all ~30 activation functions, and it carries *two* numeric paths in every kernel: an **exact libm closed form**, and a **piecewise-polynomial LUT evaluator** (`evaluate_generic`) that reproduces the silicon activation engine's degree-3 segment approximation bit-for-bit. Which path runs is a single runtime predicate — `usePwp && can_use_pwp_table` — so bit-exactness against hardware is opt-in, gated only on whether a profile directory was supplied.

This page documents the simulator's side of activation: how the visitor marshals the operands and dispatches by name; how `PWPSim::Simulator` loads the per-function profile JSONs into an `AFTable`; and — the high-value core — how **one** generic evaluator, `evaluate_generic` `@0x99c0`, maps an arbitrary fp32 input to a polynomial segment using the input's own IEEE-754 exponent and mantissa bits, then Horner-evaluates a degree-3 Taylor expansion about that segment's breakpoint. The segment-selection math is the interesting part: it is a **two-level, floating-point-aware table** — outer index the biased exponent (one region per octave), inner index the top *N* mantissa bits — wrapped in a four-way saturation clamp and an even/odd symmetry fold. This is the simulator's model of the hardware LUT, not the hardware itself; the silicon datapath (`LoadActFuncSet`, the LUT-set residency budget) is in [`activation-engine`](../arch/activation-engine.md), and the shipped profile schema is in the Part-10 activation pages (planned). This page does not re-derive those.

For a reimplementer, the contract is:

- The visitor's operand contract: `out[i] = ActFn(in[i]·scale + bias ; alpha)`, the float32-only assert on scale/bias/alpha, and the scalar-vs-bulk dispatch flag.
- The name-based dispatch: `lookupSimFunction` binds a `sim_<fn>` member pointer plus the matching `AFTable&` into a `std::function`, keyed on the activation name string.
- The `AFTable` layout (184 B) and its three sub-structs, and the `[key,"int"]` bit-exact load mechanism that reads raw IEEE-754 patterns from the profile JSON.
- `evaluate_generic`: the special-value short-circuits, the symmetry fold, the four-way saturation clamp, the two-level bucket search (`find_pwp_nonsat_section`), the mixed-precision degree-3 polynomial, and the symmetry reconstruction.
- The `usePwp` gate in every `sim_<fn>` and the exact closed-form fallbacks (the reference formulas the LUT approximates).

| | |
|---|---|
| **Numeric model** | `libpwp_sim.so`, class `PWPSim::Simulator` (96 B), 535 funcs, full decompiled C |
| **Visitor entry** | `birsim::InstVisitor::visitInstActivation` `@0x2012d0` (libBIRSimulator, 506-line body) |
| **Name→math entry** | `PWPSim::Simulator::simulate` (buffer) `@0xccf0`; (scalar) `@0xcb70` |
| **Name dispatch** | `PWPSim::Simulator::lookupSimFunction` `@0xc3d0` (31-arm string compare) |
| **PWP evaluator** | `PWPSim::Simulator::evaluate_generic` `@0x99c0` (thunk `@0x8ae0`) |
| **Segment search** | `find_pwp_nonsat_section` `@0x9340` (exp×mantissa two-level index) |
| **LUT loader** | `PWPSim::Simulator::initialize_pwptable` `@0xb040` → `parse_nonsat`/`parse_sat`/`parse_xd` |
| **Per-func table** | `PWPSim::AFTable`, 184 B; segment = `PWPSim::exponent_sect`, 20 B (`x,d0,d1,d2,d3`) |
| **PWP gate** | `usePwp && PWPSim::Simulator::can_use_pwp_table` (else exact libm) |
| **Source marker** | `/tmp/packages/KaenaPWP/pwp_sim/activation_pwp_simulation.cpp` (`.rodata @0x17058`) |

---

## 1. The visitor side — `visitInstActivation` marshals, then delegates

The generic activation kernel is `birsim::InstVisitor::visitInstActivation(this, bir::InstActivation*)` `@0x2012d0` (a 506-line body; the symbol also carries a second per-symbol sidecar frame `@0xecb30` — the two-VA-frame artifact, same body). It does no activation math itself: it resolves the function by name, reads three scalar operands, then loops `PWPSim::Simulator::simulate` over the input tensor. The handler runs *before* any PWP work — its job is the operand contract and the engine-accumulator fold.

The sequence, with decompiled line refs:

```c
// birsim::InstVisitor::visitInstActivation @0x2012d0
void visitInstActivation(InstVisitor *this, bir::InstActivation *inst) {
    if (*(u32*)(inst + 0x148) != 0)            // L125: variant tag at +328
        __throw_bad_variant_access();          //       (operand is a tensor-or-imm variant)

    if (*(u8*)(inst + 0x128)) {                // L128: is_activate2 byte at +296
        // fused "activate2" macro: activation + a second TensorScalar/Reduction.
        return simulateActivate2(this, ...);   //  → @0x1fc360, tail-call and RETURN
    }

    AP in = getInOutPhysicalAP(inst, 0, /*isOutput=*/0);   // L134: the INPUT tensor AP

    // *** FUNCTION SELECTION IS BY NAME, NOT BY ENUM INT ***
    std::string name = bir::ActivationFunctionType2string(*(u32*)(inst + 0x120)); // L148
    //   the +0x120 ActivationFunctionType enum → "Sigmoid"/"Gelu"/"Exp"/... ; this
    //   string is the key later handed to PWPSim::Simulator::simulate.

    uint count = NumElementsPerPartitionConsideringIndirection(in);  // L149

    float scale = operand(inst, 1);   // L303   } each operand is either a fp32 MEMORY
    float bias  = operand(inst, 2);   // L304   } tensor (variant tag 1) or an IMMEDIATE
    float alpha = operand(inst, 3);   // L305   } (tag 6). For an immediate, the dtype
    // L314-343: for each of {scale,bias,alpha}:                        is asserted:
    //   tag==1 ⇒ cast the memory operand to an fp32 vector;
    //   tag==6 ⇒ ASSERT imm_value.getType() == Dtype::float32           (L330)
    //            "activation function only support float32 bias/scale/alpha"
    //            (inst_visitor.cpp:0x82B) — then read the imm float at arg+0x48.

    if (*(u8*)(this + 205)) {          // L345: the scalar-vs-bulk dispatch flag
        for (i in 0..count-1)          // L353: per-element scalar calls
            simulate(&pwp, name, &out[i], &in[i], scale, bias, alpha, 1, usePwp);
    } else {                           // L363-402 / L440-485: TBB parallel-for task
        // bulk-call simulate over `count` elements via task vtables
        // off_2267B20 / off_2267AF0 (two simulate flavours)
        simulate(&pwp, name, out, in, scale, bias, alpha, count, usePwp);
    }

    doEngineAccumReduce(this, outAP, result, -1);  // L500: apply the InstActivation
    //   acc/reduce_op (EngineAccumulationType) into the per-engine accumulator AND
    //   write the activation output. (the accumulate side is shared with Exponential.)
}
```

Three facts a reimplementer must mirror. **(1)** Dispatch is by *name string*, not by the enum integer — `ActivationFunctionType2string` is the indirection, and the same string set is the `lookupSimFunction` compare-key roster (§2.3) and the profile FuncMap (§3.1). **(2)** scale/bias/alpha are float32-only when immediate; the assert at L330 is the gate. **(3)** The per-element contract is `out[i] = ActFn(in[i]·scale + bias ; alpha)` — the affine pre-scale is applied *before* the activation, and `alpha` passes through unchanged (used only by Prelu and the symmetry reconstruction; ignored by exp/sigmoid/…). **[HIGH]** — every line ref above is from the `0x2012d0` decompiled body; L330 string, L148 `ActivationFunctionType2string` call, L353/L485 `simulate` calls, and L500 `doEngineAccumReduce(...,-1)` are all confirmed present.

> **QUIRK — the dedicated opcodes never touch the PWP model.** Five activation-engine opcodes are hardwired in the interpreter and bypass `libpwp_sim` entirely: `InstReciprocal` (`IT 21`, `@0x1d9e60`) is inline exact `1.0f/x`; `InstExponential` (`IT 103`, `@0x1fe920`) is the fused softmax primitive `expf(in[i] − rowmax)` followed by an `EngineAccumulationType` reduce (the running SUM for the softmax denominator); `InstReadActivationAccumulator` (`IT 5`, `@0x1d2ae0`) / `InstActivationReadAccumulator` (`IT 101`, thunk → `@0x1d2ae0`) drain the per-engine accumulator with no math; `InstLoadActFuncSet` (`IT 6`, `@0x1b2070`) is a no-op validity gate (asserts `ActFuncSetId > -1` — it does **not** model LUT-set residency). So `InstReciprocal` and `Activation(func=Reciprocal)` are **not** numerically interchangeable in the simulator: the dedicated op is true `1/x`; the generic op can engage the polynomial LUT. The HW-side choice between the two encodings is a codegen decision, invisible here. The `LoadActFuncSet` no-op is the simulator's deliberate divergence from the silicon LUT-set budget documented in [`activation-engine`](../arch/activation-engine.md).

---

## 2. `PWPSim::Simulator` — state, lifecycle, and name dispatch

### 2.1 Class layout

`PWPSim::Simulator` is 96 bytes (IDA `structures.json`, **[CERTAIN]**). It is embedded inside the `birsim::InstVisitor` machine-state object (see [`sim-dispatch-state`](sim-dispatch-state.md) for the embedding offset); the visitor passes its address into `simulate`.

```c
struct PWPSim::Simulator {            // size 96
    bool        can_use_pwp_table;    // +0x00  set iff a profile file opened OK
    std::string pwp_path;             // +0x08  the profile DIRECTORY (32 B SSO string)
    std::unordered_map<std::string,   // +0x28  name → per-function table
                       PWPSim::AFTable> tables;
};
```

`can_use_pwp_table` is the sole switch between the PWP path and the exact path. It is the *only* state that gates approximation, and it is set once at construction.

### 2.2 Lifecycle — ctor, the profile probe, and the FuncMap load

The constructor `Simulator(std::string const& pwp_path)` `@0xced0` runs three steps:

1. **Probe.** `can_use_pwp_table = use_pwp_table()` `@0xac20` — returns `false` if `pwp_path` is empty, else constructs an `std::ifstream`, opens the path, and returns `is_open()` (then closes). It does **not** read the file; it is a pure "does a profile exist and open" probe. This resolves where `can_use_pwp_table` comes from: it is exactly `ifstream(pwp_path).is_open()`, with `pwp_path` the ctor's only argument — no CLI flag, no env var. **[CERTAIN]**
2. **FuncMap.** If the probe succeeded, the ctor lazily builds a function-local `static` `unordered_map<std::string,std::string>` named `FuncMap` (json_filename → activation_name), `__cxa_guard`-protected. **[CERTAIN]** — decoded from the ctor's strcpy literals and the SIMD constants `@0x17D10..0x17E20` (§3.1).
3. **Load.** It iterates `FuncMap`; for each `{filename, actname}` it builds `path = pwp_path + filename` and calls `initialize_pwptable(path, actname)`, parsing that JSON into `tables[actname]` (§3).

So **`pwp_path` is a directory**, and each function's profile is a separate JSON file inside it. The loaded files are the same per-function profiles the wheel ships (`exp_400p.json`, `reciprocal_400p.json`, `sqrt_65536p.json`, …) — the simulator's "PWP table" *is* the shipped profile directory, addressed by the FuncMap filename table.

### 2.3 `simulate` and `lookupSimFunction` — name to bound kernel

`simulate` has a **buffer** overload `@0xccf0` (the visitor's entry) and a **scalar** overload `@0xcb70` (single pre-scaled value, no scale/bias). The buffer overload:

```c
// PWPSim::Simulator::simulate(name, out, in, scale, bias, alpha, count, usePwp) @0xccf0
//  SysV regs: rdi=this, rsi=name, rdx=out, rcx=in, xmm0=scale, xmm1=bias,
//             xmm2=alpha, r8d=count, r9b=usePwp
auto fn = lookupSimFunction(this, name);        // std::function<float(Sim*,AFTable&,
                                                //                     float,float,bool)>
for (i in 0..count-1) {
    float scaled = xmm1 * in[i] + xmm0;         // *** see swap quirk below ***
    out[i] = fn(&AFTable, scaled, alpha, usePwp);
}
```

> **QUIRK — the operands named "scale" and "bias" are passed swapped relative to their roles.** Disassembly of `@0xccf0` spills `xmm0→var_D0`, `xmm1→var_CC`, `xmm2→var_C8` (`0xcd15`/`0xcd43`/`0xcd49`), then the per-element compute at `0xce36 mulss xmm0,[rbx]` / `0xce3f addss xmm0,var_D0` loads `var_CC` (= xmm1) as the **multiplier** and adds `var_D0` (= xmm0). In SysV float-arg order, with the declared signature `(…, scale, bias, …)`, xmm0=scale and xmm1=bias — so the loop literally computes `bias·in[i] + scale`. The visitor caller (§1) marshals `xmm0=scale, xmm1=bias` consistently, so the *end-to-end* affine is correct, but a reimplementation that follows the parameter names rather than the register assignment will swap multiplier and addend. Follow the registers: `var_CC` (xmm1, second float arg) = multiplier, `var_D0` (xmm0, first float arg) = addend. **[CERTAIN — disasm-anchored `0xce36`/`0xce3f`.]**

`lookupSimFunction` `@0xc3d0` is a 31-arm `std::string::compare` chain. For the matched name it fetches `&tables[name]` (`operator[]` on `this+0x28`) and composes a `std::function`: the bound `sim_<fn>` member pointer plus the `AFTable&`, with the standard `_M_invoke@0xe2d0` / `_M_manager@0xe290` trampoline. The chain order is `Identity, Square, Relu, Lrelu, Prelu, Sigmoid, Tanh, Exp, Softplus, Sqrt, Rsqrt, Ln, Ln_prime, Erf, Sin, Arctan, Sign, Gelu, Mish, Gelu_apprx_tanh, Gelu_apprx_sigmoid, Derivative_Gelu, Derivative_Erf, Copy, Abs, Reciprocal, Abs_reciprocal_sqrt, MemsetZero, Silu, Derivative_silu`, then a trailing `else` for `Derivative_Gelu_apprx_sigmoid`. An unmatched name throws `std::runtime_error("Unsupported activation function Simulator::simulation: " + name)` (cold `@0x8ffe`, string `.rodata @0x17218`). The bound `std::function` signature `float(PWPSim::Simulator*, PWPSim::AFTable&, float ele, float alpha, bool usePwp)` is confirmed in `structures.json` and fixes the `sim_<fn>` call shape. **[CERTAIN]**

---

## 3. The per-function table — `AFTable` and the LUT loader

### 3.1 The static FuncMap (json filename → activation name)

32 entries, decoded from the ctor literals + SIMD constants `@0x17D10..0x17E20` **[CERTAIN]**. The filenames are the shipped profile names; the activation names are the `lookupSimFunction` compare-keys. The full roster is in the Part-10 activation pages (planned); the simulator-relevant subset and its three structural notes:

| json file (profile) | activation name | note |
|---|---|---|
| `exp_400p.json` | `Exp` | the LUT `Exp`, distinct from the dedicated `InstExponential` |
| `reciprocal_400p.json` | `Reciprocal` | odd-symmetry profile |
| `sqrt_65536p.json` | `Sqrt` | highest-resolution table |
| `reciprocal_sqrt_40000p.json` | `Rsqrt` | |
| `ln_40p.json` | `Ln` | single-pass; the multipass `ln_4p_0mp/1mp` are **not** loaded |
| `leaky_relu_1p.json` | `Lrelu` | 1-point table (hardwired-class) |
| `identity_1p.json` / `copy_1p.json` / `relu_1p.json` / `sign_1p.json` / `abs_1p.json` | `Identity`/`Copy`/`Relu`/`Sign`/`Abs` | the universal 1-point residents |
| … (≈30 total) | … | |

Notes: **(1)** the sim picks ONE concrete resolution per function (e.g. `exp_400p`, not the 777-bucket variant). **(2)** there is **no** FuncMap entry for `Prelu` (it is pure alpha-slope, no PWP path) or `Is_finite`. **(3)** `Ln` loads the single-pass `ln_40p.json`; the simulator does not model the multipass LUT iteration, a known sim-vs-silicon divergence for `ln`. **[CERTAIN]**

### 3.2 `AFTable` and sub-structs

All offsets are byte-exact in `structures.json` **[CERTAIN]**. `AFTable` is 184 bytes:

```c
struct PWPSim::AFTable {              // size 184
    bool  symmetry_en;                // +0    even/odd reuse enabled
    bool  symmetry_invert_sign_opt;   // +1    odd function: f(-x) = -f(x)
    bool  symmetry_opt_use_neg_region;// +2    evaluate -|x| in the neg table
    float symmetry_point;             // +4    reflection offset added after reconstruct
    float zero_result;                // +8    f(0)
    float nan_result;                 // +12
    float pinf_result;                // +16   f(+inf)
    float ninf_result;                // +20   f(-inf)
    AFNonSatTy      pos_region;       // +24   std::vector<pwp_nonsat_region>  (24 B)
    AFNonSatTy      neg_region;       // +48   std::vector<pwp_nonsat_region>  (24 B)
    pwp_sat_region  sat_point_pos_high;// +72  (28 B)  the 4-way saturation clamp
    pwp_sat_region  sat_point_pos_low; // +100
    pwp_sat_region  sat_point_neg_high;// +128
    pwp_sat_region  sat_point_neg_low; // +156
};

struct PWPSim::pwp_nonsat_region {    // size 40   — one per IEEE-754 octave
    int exponent;                     // +0    = biased exponent = json "exponent" + 127
    int num_sections;                 // +4    = 2^extract_size buckets this octave spans
    int extract_size;                 // +8    # of top mantissa bits used as inner index
    std::vector<exponent_sect> sections; // +16
};

struct PWPSim::pwp_sat_region {       // size 28
    int sat_point;                    // +0    biased-exponent threshold (TRUE int)
    int mantissa_point;               // +4    mantissa tie-break threshold (TRUE int)
    exponent_sect section;            // +8    the clamp region's own poly
};

struct PWPSim::exponent_sect {        // size 20   — one polynomial bucket
    float x;                          // +0    segment breakpoint (Taylor expansion center)
    float d0, d1, d2, d3;             // +4..+16  degree-3 coefficients
};
```

The two-level shape lives in `pwp_nonsat_region`: one region per *octave* (exponent value), each holding `2^extract_size` polynomial buckets subdivided by the top `extract_size` mantissa bits. `pwp_sat_region.sat_point`/`mantissa_point` are **true integers** (compared against the input's decomposed exponent/mantissa); `x` and `d0..d3` are stored as **raw IEEE-754 bit patterns**.

### 3.3 The bit-exact load mechanism

`initialize_pwptable(path, actname)` `@0xb040` slurps the file, `json_tokener_parse`s it (assert `jobj && "Failed to parse pwp file"`, `.rodata @0x171b8`), then fills `tables[actname]`. The decisive mechanism: every "float" field in the profile is stored as a 6-tuple `{float, int, hexstring, sign, exponent, mantissa}`, and the loader reads the `.int` member — the int32 reinterpretation of the float's bits — and `LODWORD`-stores it. So `symmetry_point`, `zero_result`, `nan_result`, `pinf_result`, `ninf_result`, and every `x`/`d0..d3` are exact bit-level copies, not re-parsed decimals.

- `parse_nonsat` `@0xae90`: for each entry of `pos_exponents`/`neg_exponents`, `idx = exponent + 127`; the region vector is pre-sized to the exponent range, `region[idx]` is written, `sections` resized to `num_sections`, and each section parsed by `parse_xd`.
- `parse_xd` `@0xa550`: reads `x,d0,d1,d2,d3` each via the nested path `[<key>,"int"]` then `LODWORD`-stores — the bit-exact coefficient load.
- `parse_sat` `@0xab50`: reads `sat_point`/`mantissa_point` as **true** `int64`s (not bit patterns) and `parse_xd`s the clamp section.

The four saturation sub-keys are assembled from the constants `@0x17CC0` (`"saturation_points"`) and `@0x17CD0..0x17D00` (`sat_point_{pos,neg}_{high,low}`). **[CERTAIN]** for `parse_xd`/`parse_sat`/`find_*`; **[HIGH]** for the `initialize_pwptable` field-order (read from the 602-line body).

---

## 4. `evaluate_generic` — the segment-selection and polynomial core

This is the page's high-value section: **one** evaluator serves every PWP activation. There is no per-function evaluator — the function-specific behavior is entirely in the `AFTable` data (symmetry flags, special-value scalars, the segment buckets, the four clamp points). `evaluate_generic(AFTable& T, float v, float alpha)` `@0x99c0` (thunk `@0x8ae0`) takes the *already-scaled* input `v = in·scale + bias` and returns `f(v)`. The full body, verified against the decompiled C and disassembly:

```c
// PWPSim::Simulator::evaluate_generic(AFTable& T, float v, float alpha) @0x99c0
float evaluate_generic(Simulator *this, AFTable *T, float v, float alpha) {

    // (1) SPECIAL VALUES — the only explicit non-finite branches
    if (v == 0.0f) return T->zero_result;             // f(0)
    float absv = v & 0x7FFFFFFF;                       // _mm_and_ps, mask @0x17C90
    if (absv > 3.4028235e38f)                          // |v| > FLT_MAX ⇒ v is ±inf
        return (v <= 0.0f) ? T->ninf_result : T->pinf_result;
    //  NaN falls THROUGH: |NaN| compares false to FLT_MAX and != 0, so a NaN input
    //  reaches the bucket path; nan_result is loaded but not returned by a dedicated
    //  NaN branch (see Adversarial verification G2).

    // (2) SYMMETRY FOLD — even/odd functions reuse one half of the table
    float sel;
    if (T->symmetry_en) {
        sel = absv;                                    // evaluate on the magnitude
        if (T->symmetry_opt_use_neg_region)
            sel = absv ^ 0x80000000;                   // = -|v|  (mask @0x17CA0)
    } else {
        sel = v;                                       // asymmetric: signed input
    }

    // (3) DECOMPOSE sel's IEEE-754 bits
    int expo = bits(sel) >> 23;                        // biased exponent (sign in bit 31..)
    int mant = bits(sel) & 0x7FFFFF;                   // 23-bit mantissa

    // (4) SECTION SELECT — 4-way saturation clamp, THEN in-range bucket
    exponent_sect *sect;
    if (bits(sel) >= 0) {                              // sign bit clear → positive side
        if ( T->sat_point_pos_high.sat_point <  expo ||
            (T->sat_point_pos_high.sat_point == expo &&
             T->sat_point_pos_high.mantissa_point <= mant) )
            sect = &T->sat_point_pos_high.section;     // upper saturation
        else if ( T->sat_point_pos_low.sat_point >  expo ||
                 (T->sat_point_pos_low.sat_point == expo &&
                  T->sat_point_pos_low.mantissa_point >= mant) )
            sect = &T->sat_point_pos_low.section;      // lower saturation
        else
            sect = find_pwp_nonsat_section(T->pos_region, sel);  // in-range LUT
    } else {                                           // negative side; (u8)expo drops sign
        if ( (u8)expo >  T->sat_point_neg_high.sat_point ||
            ((u8)expo == T->sat_point_neg_high.sat_point &&
             T->sat_point_neg_high.mantissa_point <= mant) )
            sect = &T->sat_point_neg_high.section;
        else if ( (u8)expo <  T->sat_point_neg_low.sat_point ||
                 ((u8)expo == T->sat_point_neg_low.sat_point &&
                  T->sat_point_neg_low.mantissa_point >= mant) )
            sect = &T->sat_point_neg_low.section;
        else
            sect = find_pwp_nonsat_section(T->neg_region, sel);
    }

    // (5) DEGREE-3 POLYNOMIAL about the segment breakpoint x  (MIXED PRECISION)
    float t = sel - sect->x;                           // local offset from breakpoint
    double r = (double)((float)(t * sect->d1) + sect->d0)  // d0 + d1*t   in FLOAT
             + (double)(t * t * sect->d2);                 // + d2*t^2    in DOUBLE
    r = pow((double)t, 3.0) * (double)sect->d3 + r;        // + d3*t^3 via libm pow
    float result = (float)r;
    //  ⇒  f(sel) ≈ d0 + d1·t + d2·t² + d3·t³ ,  t = sel − x_section   (Taylor about x)

    // (6) SYMMETRY RECONSTRUCTION — only when sel was the reflected copy of a neg input
    if (v != absv && T->symmetry_en) {                 // original v was NEGATIVE
        if (T->symmetry_invert_sign_opt) result = -result;   // odd: f(-x) = -f(x)
        return result + T->symmetry_point;                   // + reflection offset
    }
    return result;
}
```

### 4.1 The segment-selection math (the two-level table)

Step (4) routes to `find_pwp_nonsat_section` `@0x9340` for in-range inputs. This is where the floating-point-aware addressing happens:

```c
// find_pwp_nonsat_section(region_vec, sel) @0x9340  (.isra)
exponent_sect *find_pwp_nonsat_section(AFNonSatTy *region_base, float sel) {
    // OUTER index: biased exponent → one pwp_nonsat_region per IEEE-754 octave
    pwp_nonsat_region *R = region_base + 40 * (u8)(bits(sel) >> 23);   // 40 = sizeof region

    // INNER index: top `extract_size` mantissa bits → uniform bucket within the octave
    int sectid = (bits(sel) & 0x7FFFFF) >> (23 - R->extract_size);

    int num_sections = (R->sections._M_finish - R->sections._M_start) / 20;  // /sizeof sect
    if (sectid >= num_sections)
        __assert_fail("sectid<exp.sections.size() && \"Unhandled section in simulation\"",
                      "…/activation_pwp_simulation.cpp", 0xAF, …);
    return R->sections._M_start + 20 * sectid;          // &R->sections[sectid]
}
```

The LUT is **two-level**: the outer index is the biased exponent (`bits(sel) >> 23`, masked to a `u8`), selecting one `pwp_nonsat_region` per octave (the region vector is indexed by `exp + 127`); the inner index is the top `extract_size` mantissa bits, selecting one of `2^extract_size` uniform buckets *within* that octave (`num_sections == 2^extract_size` for in-range octaves). Each bucket holds one degree-3 Taylor expansion `{x, d0, d1, d2, d3}`. The consequence is precisely the right shape for fp32: because each octave gets its own bucket array, the buckets are **exponentially denser near zero and exponentially wider away from it** — matching the relative (ULP) precision of floating-point and the activation engine's hardware LUT addressing. A profile's total physical bucket count (`lut_size` in the shipped JSON; e.g. `exp` = 371 positive + 406 negative = 777) is the sum over octaves of these per-octave bucket arrays. **[CERTAIN — `find_pwp_nonsat_section` body + disasm; the `lut_size`=Σ-buckets relation cross-referenced from the shipped profile schema, see Part-10 (planned).]**

### 4.2 The polynomial evaluation — mixed precision is decisive

Step (5) is a degree-3 Taylor expansion about the bucket breakpoint `x`, but the *precision* is not uniform, and a bit-exact reimplementation must reproduce it exactly. The disassembly (`@0x99c0`) shows:

- `0x9afb subss xmm0,[rax]` — `t = sel − x` (x at `section+0`), in **float**.
- `0x9b2b mulss xmm0,[rax+8]` then `0x9b38 addss xmm0,[rax+4]` — the `d0 + d1·t` term stays in **float** (d1 at +8, d0 at +4).
- `0x9b17 cvtss2sd [rax+0Ch]` and `0x9b1c cvtss2sd [rax+10h]` — d2 (+0xC) and d3 (+0x10) are promoted to **double**; the `d2·t²` and `d3·t³` terms accumulate in double.
- `0x9b5b call _pow` — the cubic term uses libm `pow(t, 3.0)`, **not** `t*t*t`.

So a faithful reimplementation must evaluate `d0 + d1·t` in float, accumulate `d2·t²` and `d3·t³` in double, and compute `t³` with `pow(t,3.0)` — the rounding differs from a uniform-precision Horner form. **[CERTAIN — disasm-anchored.]**

### 4.3 Symmetry families and special values

The symmetry flags collapse a function's table to one half. `symmetry_en=false` (exp, sigmoid, tanh, sqrt, softplus, …) means `sel = v` and no reconstruction. `symmetry_en=true` with `symmetry_invert_sign_opt=true` is an **odd** function: for `v < 0`, evaluate the polynomial on `|v|`, negate, and add `symmetry_point`. The shipped `reciprocal_400p` profile is exactly this — `symmetry_en=true`, `invert_sign_opt=true`, `opt_use_neg_region=false`, `symmetry_point=0`, `zero_result≈3.39e38` (the `1/0` saturation) — so `1/(−x) = −(1/x)` is reconstructed by step (6). **[CERTAIN — cross-checked against the shipped profile.]**

Special values resolve to the `AFTable` scalar fields: `v==0 → zero_result`; `v=+inf → pinf_result`; `v=−inf → ninf_result`; an input whose biased exponent crosses a `sat_point_*_high` threshold lands in that clamp section (e.g. `exp`'s `sat_point_pos_high` carries `d0=+inf` with `d1=d2=d3=0`, so the normal poly eval returns `+inf` — that *is* the overflow clamp). `nan_result` is loaded into the table but is **not** returned by an explicit NaN branch (see §6 G2).

---

## 5. The `sim_<fn>` roster — the PWP gate and the exact fallbacks

Every activation kernel `sim_<fn>(Simulator*, AFTable*, float ele, float alpha, bool usePwp)` has the same two-path shape:

```c
float sim_<fn>(Simulator *this, AFTable *T, float ele, float alpha, bool usePwp) {
    if (usePwp && this->can_use_pwp_table)
        return evaluate_generic(this, T, ele, alpha);
    else
        return <exact closed form>;     // the reference formula the LUT approximates
}
```

The exact fallback is the reference math the PWP LUT is approximating — useful both as a spec for the LUT and as the default (no-profile) numerics. Verbatim closed forms (constants read from the decompiled bodies; addresses **[CERTAIN]** by symbol):

| Activation | addr | usePwp | Exact fallback |
|---|---|---|---|
| `Identity`(0) | `0x9c70` | opt | `x` |
| `Square`(1) | `0x9c90` | yes | `x*x` |
| `Relu`(2) | `0x9cb0` | yes | `fmaxf(x, 0)` |
| `Lrelu`(3) | `0x9cd0` | yes | `x<=0 ? x*0.01 : x` (slope **0.01 hardcoded**, alpha unused) |
| `Prelu`(4) | `0x9320` | **NO** | `x<=0 ? x*alpha : x` (**no PWP branch at all**) |
| `Sigmoid`(5) | `0x9d10` | yes | `1.0/(exp(-x)+1.0)` |
| `Tanh`(6) | `0x9d60` | yes | `tanh(x)` |
| `Exp`(7) | `0x9d90` | yes | `exp(x)` |
| `Softplus`(8) | `0x9dc0` | yes | `log1p(exp(-fabs(x))) + (x<=0 ? 0 : x)` |
| `Sqrt`(9) | `0x9e40` | yes | `x<0 ? sqrtf(x) : fsqrt(x)` |
| `Rsqrt`(10) | `0x9e70` | yes | `1.0/sqrt(x)` |
| `Ln`(11) | `0x9ed0` | yes | `log(x)` |
| `Ln_prime` | `0x9f00` | yes | `1.0/x` |
| `Erf`(12) | `0xa040` | yes | `erf(x)` |
| `Sin`(13) | `0x9fe0` | yes | `sin(x)` |
| `Arctan`(14) | `0xa010` | yes | `atan(x)` |
| `Sign`(15) | `0xa070` | yes | `(x>0) - (x<0)` (branchless) |
| `Gelu`(16) | `0x9f30` | yes | `0.5*x*(erf(x/1.414213562373095)+1)` |
| `Mish`(17) | `0x9f90` | yes | `x*tanh(sim_softplus(x))` |
| `Gelu_apprx_tanh`(18) | `0xa100` | yes | `0.5*x*(tanh((0.044715*x³+x)*0.7978845608028654)+1)` |
| `Gelu_apprx_sig`(19) | `0xa0b0` | yes | `x*sim_sigmoid(1.702*x)` |
| `Deriv_Gelu_aps`(20) | `0xa480` | yes | `u=1.702x: (u+e^u+1)*e^u/(e^u+1)²` |
| `Derivative_Gelu`(21) | `0xa180` | yes | `0.5*(erf(x*0.7071067811865476)+1) + x*0.3989423*exp(-x²/2)` |
| `Derivative_Erf`(22) | `0xa260` | yes | `sim_exp(-x²) * 1.1283792` (= 2/√π·e^{-x²}) |
| `Copy`(23) | `0xa500` | opt | `x` |
| `Abs`(24) | `0xa2b0` | yes | `|x|` (blendv sign-clear) |
| `Reciprocal`(25) | `0xa520` | yes | `1.0/x` |
| `Abs_recip_sqrt`(26) | `0xa2f0` | yes | `1.0/sqrt(fabs(x))` |
| `Silu`(27) | `0xa380` | yes | `x/(exp(-x)+1.0)` |
| `Derivative_silu`(28) | `0xa3e0` | yes | `(x*e^x+1)*e^x/(e^x+1)²` |
| `memset_zero` | `0xa360` | yes | `0.0` |

Math constants for bit-exact reimplementation: `√2 = 1.414213562373095`, `1/√2 = 0.7071067811865476`, `√(2/π) = 0.7978845608028654`, `1/√(2π) = 0.3989423`, `2/√π = 1.1283792`, tanh-gelu cubic coeff `0.044715`, sigmoid-gelu scale `1.702`. `Prelu` is the only kernel with no `usePwp` branch. `Mish`/`Derivative_Gelu` *call* `sim_softplus`/`sim_erf`/`sim_exp` with the inner exact value, so the composite stays exact even when `usePwp` is set. **[CERTAIN — `sim_sigmoid`/`sim_gelu`/`sim_prelu` bodies read in full; the gate pattern is identical across the roster.]**

---

## 6. Adversarial verification — the five strongest claims

Each claim below was re-checked against the binary; the verification ceiling is stated honestly.

1. **`evaluate_generic` is one generic evaluator (no per-function evaluator).** **[CONFIRMED]** — the `0x99c0` body reads only `AFTable` data (symmetry flags, sat points, the bucket sections, the four result scalars); the function-specific behavior is entirely in the loaded table, and every `sim_<fn>` calls the *same* `evaluate_generic` (verified in `sim_sigmoid@0x9d10`, `sim_gelu@0x9f30`). No counter-evidence of a per-function PWP path.
2. **Two-level segment selection: outer = biased exponent, inner = top `extract_size` mantissa bits.** **[CONFIRMED]** — `find_pwp_nonsat_section@0x9340`: `region_base + 40*(u8)(bits>>23)` (outer), `(mant & 0x7FFFFF) >> (23 - extract_size)` (inner), `20*sectid` element stride, `num_sections = (finish-start)/20`. Byte-exact in both decompiled C and disasm.
3. **The polynomial is mixed-precision degree-3 with a libm `pow` cubic.** **[CONFIRMED]** — disasm `@0x99c0`: `subss`(t), `mulss`/`addss` for `d0+d1·t` (float), `cvtss2sd` of d2/d3 then `mulsd` (double), `call _pow` for `t³`. A `t*t*t` reimplementation would diverge in rounding.
4. **`simulate` passes scale/bias swapped vs. the parameter names.** **[CONFIRMED]** — disasm `@0xccf0`: `xmm0→var_D0`, `xmm1→var_CC`; per-element `mulss xmm0,[rbx]` uses `var_CC` (xmm1, the "bias" arg) as the multiplier, `addss var_D0` adds `var_D0` (xmm0, the "scale" arg). The register assignment is the truth, not the names.
5. **`AFTable` is 184 B with the documented sub-struct offsets.** **[CONFIRMED]** — `structures.json`: `AFTable`=184 (symmetry flags @0/1/2, scalars @4..20, `pos_region`@24 / `neg_region`@48, four `pwp_sat_region`@72/100/128/156); `pwp_nonsat_region`=40 (exponent@0, num_sections@4, extract_size@8, sections@16); `pwp_sat_region`=28; `exponent_sect`=20.

**Verification ceiling / open items.**
- **G1 — generator-only profile fields.** The shipped JSONs carry `imm_bias`, `exponent_offset`, `max_diff`, `fma_const0/1`, `lower_bound`/`upper_bound`, `use_multipass`, and various `*_id`s that **neither** `initialize_pwptable` nor `evaluate_generic` reads. They are KaenaPWP LUT-generator / HW metadata; the simulator consumes only the already-baked `{x,d0..d3}` buckets, four sat points, and the special-value scalars. **[INFERRED — absence-of-read, verified across the two loader bodies; not proven exhaustively over every JSON key.]**
- **G2 — NaN handling.** `evaluate_generic` has explicit branches only for `v==0` and `|v|>FLT_MAX`. A NaN input falls through to bit-decompose → bucket-select → poly, so `nan_result` is loaded but not returned by a dedicated NaN branch. Whether a NaN ever lands in a section whose poly reproduces `nan_result` is data-dependent; **not traced** with a NaN input. **[INFERRED.]**
- **G3 — negative-side `sat_point` semantics.** The positive side compares `int sat_point` vs `int expo` (≤255, always ≥0); the negative side uses `(u8)expo`. The bodies are explicit (§4 step 4), but the JSON author's intent for neg-region `sat_point` keying is **not** proven against a negative-region trace. **[INFERRED.]**
- **G4 — `ln` multipass divergence.** The simulator loads single-pass `ln_40p.json`; silicon `ln` is multipass. The single-pass bucket eval is a real sim-vs-silicon divergence for `ln`, **not** chased to a numeric delta. **[INFERRED.]**
- **G5 — `InstVisitor+0x205` provenance.** The scalar-vs-bulk dispatch flag (§1) and the TBB task vtables `off_2267B20`/`off_2267AF0` are read at call-site granularity; the source that *sets* `+0x205` is **not** traced. **[Not traced.]**

---

## 7. Function map

| Symbol | addr | identity | conf |
|---|---|---|---|
| `birsim::InstVisitor::visitInstActivation` | `0x2012d0` | generic act → name → `PWPSim::simulate` | HIGH |
| `birsim::InstVisitor::simulateActivate2` | `0x1fc360` | fused activation + TensorScalar/Reduction | MED |
| `birsim::InstVisitor::visitInstExponential` | `0x1fe920` | exact `expf(x−rowmax)` + reduce (softmax) | HIGH |
| `birsim::InstVisitor::visitInstReciprocal` | `0x1d9e60` | exact inline `1.0f/x` | CERTAIN |
| `birsim::InstVisitor::visitInstReadActivationAccumulator` | `0x1d2ae0` | drain per-engine accumulator | HIGH |
| `birsim::InstVisitor::visitInstActivationReadAccumulator` | `0x1d2c90` | thunk → `0x1d2ae0` | HIGH |
| `birsim::InstVisitor::visitInstLoadActFuncSet` | `0x1b2070` | no-op validity gate (`ActFuncSetId>-1`) | HIGH |
| `birsim::InstVisitor::doEngineAccumReduce` | `0x1f9900` | `EngineAccumulationType` fold + write | HIGH |
| `PWPSim::Simulator::Simulator(string&)` | `0xced0` | ctor: probe + FuncMap load | CERTAIN |
| `PWPSim::Simulator::use_pwp_table` | `0xac20` | `ifstream(pwp_path).is_open()` gate | CERTAIN |
| `PWPSim::Simulator::initialize_pwptable` | `0xb040` | profile JSON → `tables[name]` `AFTable` | HIGH |
| `parse_nonsat` (.part.0) | `0xae90` | `pos/neg_exponents` → `region[exp+127]` | CERTAIN |
| `parse_sat` | `0xab50` | `saturation_points{sat_point,…}` → clamp | CERTAIN |
| `parse_xd` | `0xa550` | section `{x,d0..d3}["int"]` → bit pattern | CERTAIN |
| `PWPSim::Simulator::simulate` (buffer) | `0xccf0` | per-elem `xmm1·in+xmm0`, fn dispatch | CERTAIN |
| `PWPSim::Simulator::simulate` (scalar) | `0xcb70` | single pre-scaled value | CERTAIN |
| `PWPSim::Simulator::lookupSimFunction` | `0xc3d0` | name → (`sim_<fn>`, `&tables[name]`) | CERTAIN |
| `PWPSim::Simulator::evaluate_generic` | `0x99c0` | PWP poly eval (§4); thunk `0x8ae0` | CERTAIN |
| `find_pwp_nonsat_section` (.isra) | `0x9340` | exp×mantissa two-level bucket search | CERTAIN |
| `sim_<fn>` roster (32) | `0x9320..0xa520` | per-act exact-or-PWP kernels (§5) | CERTAIN |
| `PWPSim::DEBUG` (bss flag) | `0x1c720` | gates the three `dump()` diagnostics | CERTAIN |

**Diagnostic strings** (`libpwp_sim.so` `.rodata`, VA==fileoff): `0x17058` `/tmp/packages/KaenaPWP/pwp_sim/activation_pwp_simulation.cpp`; `0x17098` `sectid<exp.sections.size() && "Unhandled section in simulation"` (assert `@0xAF`); `0x17140` `jobj && "Expected valid json object"` (assert `@0x66`/`@0x86`); `0x171b8` `jobj && "Failed to parse pwp file"` (assert `@0xB8`); `0x17218` `Unsupported activation function Simulator::simulation: `; sign/negate masks `0x17C90` (`0x7FFFFFFF`) / `0x17CA0` (`0x80000000`); `0x17CC0` `saturation_points`.

---

## 8. Cross-references

- [`sim-dispatch-state`](sim-dispatch-state.md) — the `birsim::InstVisitor` opcode→kernel dispatch and the whole-machine state model (where `PWPSim::Simulator` is embedded).
- [`sim-core-arithmetic`](sim-core-arithmetic.md) — the shared cast / accumulate / RNE / `MemoryReductionOp` primitives that back `doEngineAccumReduce`.
- [`activation-engine`](../arch/activation-engine.md) — the silicon activation datapath, `LoadActFuncSet`, and the HW LUT-set residency budget the simulator deliberately does not model.
- Part-10 activation pages (planned, `activation/`) — the shipped PWP profile schema (`pwp_jsons/*.json`, `lut_size`/`extract_size`/`num_sections`, the per-function resolutions) that this evaluator consumes.
