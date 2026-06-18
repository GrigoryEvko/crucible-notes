# The L2 Wire Validator — the Silicon Legality Cascade

## Abstract

This page documents the **L2 silicon-legality validator** in `libwalrus.so` — the gate that decides whether a *fully encoded 64-byte wire bundle* is legal for the target TPB silicon. It runs at codegen time, after each instruction is already bit-packed into its bundle, as the body of the deferred [`flushISAChecks`](codegen-driver.md) replay. Where the structural BIR verifier (L1) asks *"is this op on a legal engine for this arch?"* over the abstract IR, L2 asks *"does this exact packed word satisfy the silicon's dtype/quadrant/partition/reserved-bit format?"* over the wire bytes — a question L1 structurally cannot pose, because L1 never sees the packed word.

The validator is **not one function**. It is **three per-arch families** (`core_v2`/`core_v3`/`core_v4`), and the divergence is deep: the three entry points have **three different signatures** (different arity, not just a different roster), each arch has its own engine-opcode gate (`instruction_engine_check` on V2, `neuron_isa_check_opcode_on_engine` bitmaps on V3/V4) with its own `NeuronAssertion` codes, and each arch hosts a different set of `is_valid_<fmt>` format gates (59 / 64 / 81 symbols). The headline mechanism this page pins is the **dual-FP8 weight-load restriction** (`s3_lw_dual_fp8_restrictions`) — a forbidden-quadrant check that exists **only at L2, only in core_v4**, with no L1 or L0 analog. It is the canonical proof that L1∧L2 are *conjunctive, not redundant*: an instruction can pass every L1 rule and still fail L2's silicon word.

Every address, signature, and symbol count below was re-grounded against the readelf symbol table for the `cp310` `libwalrus.so` (`neuronx_cc 2.24.5133.0+58f8de22`); the bit-level gate bodies are from the decompiled function bodies. Confidence tags are per claim.

> **Two-VA-frame note.** Throughout, addresses are the **true ELF VA frame** (e.g. `is_valid_s3_lw` @ `0x14ac170`). The per-symbol decompiled sidecars in this corpus carry a *second* internal frame (e.g. `0x5fbd40` for the same function, where Hex-Rays emits a `thunk` wrapper). Both frames denote the same function; a sidecar reading `0x5fbd40` is **not** a wrong address. Symbol-table VAs (cited here) are the authoritative ones.

---

## Where L2 sits in the legality stack

There are three independent encodings of *"op X on engine E at arch A is legal"* in this wheel (plus a Python front-end mirror, L3). They are separately authored with **no call edge** between them; the single defended *runtime* contract is **L1 ∧ L2**, both resident in `libwalrus.so`.

| Layer | Symbol / entry | When it runs | What it checks | Key-space | Fail mode |
|---|---|---|---|---|---|
| **L0** libBIR | `bir::Instruction::isValidEngine` / `getValidEngines` (libBIR.so) | **never** (0 callers wheel-wide) | `(ArchLevel, EngineType)` ∈ static per-op map | bir `ArchLevel(10..50)` × `EngineType(0..7)` | `return false`, silent — **dormant data** |
| **L1** birverifier | `birverifier::Verifier::run` @ `0xf9f120` → `checkValidEngines` @ `0x103a200`, `checkArchLevel` @ `0xfd2540`, `checkMemType` @ `0xfd12f0` | **pre-codegen**, on the symbolic BIR | op on its assigned engine? arch-min/max? mem class? collective IO ratio? — coarse op-shape gate over *abstract IR fields* | bir `EngineType` (`Inst+0x90`), bir opcode (`Inst+0x88`), `ArchLevel` | `NeuronAssertion 400` (validEngines), `366/367` (arch), `310` (mem) |
| **L2** wire *(this page)* | `CoreV{2,3,4}GenImpl::runSingleISACheck` @ `0x1211cf0`/`0x134c060`/`0x1435010` → engine gate + `is_valid_neuron[_isa/_engine]_instruction` | **during codegen** (`RUN_ISA_CHECKS`), on the emitted 64-byte word, TBB-parallel via `flushISAChecks` | does the fully-encoded word satisfy the **silicon format**? dtype combos, AP quadrants, partition reach, FP8 dual-restriction, reserved bits, NaN/Inf, perf-mode | **wire** opcode byte (`INST_UNION[0]`), **wire** engine (`0..4` via `convert()`), core version `V2/V3/V4`, packed silicon fields | `NeuronAssertion` V4 `935/934/2083`, V3 `1084/1000`, V2 `1083/999/939` |
| **L3** Python | `NeuronVerifier` `check_isa_constraints` (pybind, front-end) | front-end fast-fail | mirrors L2's rule *set* for Sunda/Cayman — a faster-fail spec mirror | Penguin IR op | returns `False` / raises |

L2 is the **fine, post-encode wire gate**. It is the only layer that inspects the bit-packed bundle. [CERTAIN — L1/L2 symbols and addresses verified in the symbol table; L0 dormancy and L3 mirror per the verifier-strand cross-refs cited below.]

> **NOTE — the enum-space remap between layers.** L0/L1 key on **bir** `EngineType` (`Pool=1, Act=2, PE=3, DMA=4, DVE=5, SP=6`). L2 keys on **wire** `NEURON_ENGINE` (`PE=0, ACT=1, POOL=2, DVE=3, SP=4`). The bir→wire remap is `convert()` (an internal `sub_1433840`, not exported), applied at each `runSingleISACheck` call site: `1→2, 2→1, 3→0, 5→3, 6→4`. A legality answer literally passes through this remap between L1 and L2. [STRONG — remap table from the V4 `runSingleISACheck` body; the `sub_` is local, so no symbol-table cross-check, but the call site and remap are in the decompiled body.]

---

## H1 — the three entry points have three different signatures

This is the core mechanism by which the 64-way cascade *"differs per generation."* It is **not** merely a different validator roster: the **entry arity itself diverges**. The bir→wire context each arch needs is threaded in at the signature level.

| Arch | Entry validator | VA | Signature after `INST_UNION` | Roster (`is_valid_*` syms) |
|---|---|---|---|---|
| core_v2 (Trn1/inf2, Sunda) | `is_valid_neuron_instruction` | `0x1332540` | *(none)* — pure opcode-byte dispatch | **59** |
| core_v3 (Trn2, Cayman/gen3) | `is_valid_neuron_isa_instruction` | `0x141ed10` | `, NEURON_CORE_VERSION` — version-aware | **64** |
| core_v4 (Trn3, Mariana) | `is_valid_neuron_engine_instruction` | `0x1502120` | `, NEURON_CORE_VERSION, NEURON_ENGINE` — version + wire-engine aware | **81** |

The demangled signatures, verbatim from the symbol table:

```c
// core_v2 @ 0x1332540  — no version, no engine
neuronxcc::core_v2::is_valid_neuron_instruction(
    NEURON_ISA_TPB_INST_UNION);

// core_v3 @ 0x141ed10  — version threaded in
neuronxcc::core_v3::is_valid_neuron_isa_instruction(
    NEURON_ISA_TPB_INST_UNION, NEURON_ISA_TPB_NEURON_CORE_VERSION);

// core_v4 @ 0x1502120  — version AND wire-engine threaded in
neuronxcc::core_v4::is_valid_neuron_engine_instruction(
    NEURON_ISA_TPB_INST_UNION, NEURON_ISA_TPB_NEURON_CORE_VERSION,
    NEURON_ISA_TPB_NEURON_ENGINE);
```

All three share the **same cascade shape**. The `INST_UNION` arrives as 4 × `__m128i` (64 bytes); byte 0 (`INST_UNION[0]`, signed in Hex-Rays — e.g. `-87 = 0xA9`, `-29 = 0xE3`) is the **wire opcode**. For each opcode the validator runs, in order: (1) `is_valid_enum(4, engine)` to re-validate the wire engine; (2) `has_valid_neuron_events(4, engine, …)` for event/semaphore descriptors; (3) per-packed-field `is_valid_enum(typeId, byte)`; (4) reserved-bit / quadrant zeroing; (5) `is_valid_dtype[_64]`; (6) the per-**format** validator. The divergence is in **entry arity** and **format roster**, not in this shape. [CERTAIN — three entry symbols, three demangled signatures, and three roster counts all read directly from the readelf symbol table.]

> **VERIFIED — the roster counts are exact.** Counting unique `is_valid_*` `FUNC` symbols (including `is_valid_enum`/`is_valid_dtype*`, excluding the parallel `dbg_is_valid_*` debug-emitter family) in each arch namespace yields **`core_v2: 59`, `core_v3: 64`, `core_v4: 81`** — matching the roster claim to the symbol. The roster *grows by selective add*: v2→v3 adds the gen3 silicon (`dtype_64`, `s4d3_mm`, `conv_lut_load`, `dma_transpose`, `jpeg_decode`, `tensor_dequantize`, …) and drops v2-legacy formats (`custom_op_header`, `pseudo_mem_2d`, `s4d4_tsm`, …); v3→v4 adds the MX/FP4/sparsity family (`s3d3_mm`, `smx1d3_mm`, `smx1_lw`, `s3dmx1_quant`, `dtype_inc_fp4`, `matmul_seeding`, `pe_manage_seed`, `sparsity_compress`, `stream_shuffle/transpose`, `activate2`, `rand2`, …). MX, FP4, sparsity, seeding, and the dual-FP8 gate are **core_v4-only silicon**. [CERTAIN — symbol-table count; the per-arch ADD/DROP sets are a comm-diff of the three namespaces' rosters.]

---

## H2 — the engine-opcode gate diverges per arch (the bitmap LUT)

Before the format gate runs, `runSingleISACheck` asks a coarser question: *is this opcode legal on this engine, at this version?* The function that answers **also differs per arch**, with three different functions and three different assertion codes.

### core_v4 — `CoreV4GenImpl::runSingleISACheck` @ `0x1435010`

Signature `(bir::Instruction&, std::array<unsigned char,64>&)` — the 64-byte word is **already encoded** by the `visitInst` caller; `runSingleISACheck` only *validates* it. The body, as annotated pseudocode:

```c
// CoreV4GenImpl::runSingleISACheck(Instruction& I, array<uchar,64>& bundle)  @ 0x1435010
function runSingleISACheck_v4(I, bundle):
    assert(len(bundle) == 64, "Wrong instruction length")        // (a) length gate

    op  = *(uchar*)(I + 0x88)                                     // bir opcode
    eng = *(uchar*)(I + 0x90)                                     // bir engine
    if  op  not in {46, 49, 50} and eng not in {0, 7}:            // (b) DMA-engine fence
        assert(false, "CoreV4Gen - Instruction cannot be on the DMA engine by codegen")

    wireEng = convert(eng)                                        // sub_1433840: 1→2,2→1,3→0,5→3,6→4
    if not neuron_isa_check_opcode_on_engine(op, wireEng, /*V4=*/4, bundle):  // (c) the BITMAP gate
        NeuronAssertion(935)   // "::neuron_isa_check_opcode_on_engine(... convert(I.getEngine()),
                               //   ...V4, instrUn)"  [CoreV4GenImpl.cpp:202]

    if not is_valid_neuron_engine_instruction(bundle, /*V4=*/4, wireEng):     // (d) the FORMAT gate
        debug_invalid_neuron_engine_instruction(...)             // verbose per-field diagnostic
        NeuronAssertion(934)   // predicate "is_valid_neuron_instruction"

    if eng == SP:                                                // (e) SP 2nd path
        if not is_valid_neuron_engine_instruction(bundle, 4, /*wire SP=*/3):
            NeuronAssertion(2083)
```

### core_v3 / core_v2 — same skeleton, different gate + codes

```c
// CoreV3GenImpl::runSingleISACheck  @ 0x134c060
if not neuron_isa_check_opcode_on_engine(op, convert(eng), /*V3=*/3, bundle):
    NeuronAssertion(1084)
if not is_valid_neuron_isa_instruction(bundle, /*V3=*/3):
    NeuronAssertion(1000)

// CoreV2GenImpl::runSingleISACheck  @ 0x1211cf0  — NO opcode_on_engine on V2
if not instruction_engine_check(bundle, convert(eng)):           // structurally simpler engine gate
    NeuronAssertion(1083)
if not is_valid_neuron_instruction(bundle):
    NeuronAssertion(999)   // a 2nd path throws 939
```

| Arch | engine gate | VA | assert (engine fail) | assert (format fail) |
|---|---|---|---|---|
| core_v2 | `instruction_engine_check` | `0x13346e0` | **1083** | **999** (2nd path **939**) |
| core_v3 | `neuron_isa_check_opcode_on_engine` | `0x1421840` | **1084** | **1000** |
| core_v4 | `neuron_isa_check_opcode_on_engine` | `0x1504700` | **935** | **934** (SP 2nd path **2083**) |

core_v2 has **no** `opcode_on_engine` — its engine check is the structurally simpler `instruction_engine_check`. core_v3 and core_v4 share the `_bittest64` per-engine-bitmap form. [CERTAIN — `runSingleISACheck` addresses and the two engine-gate addresses verified in the symbol table; the assertion codes and the `convert()` remap are from the decompiled `runSingleISACheck` bodies (the V4 codes `935/934/2083` cross-confirmed by the verifier strand, the V2/V3 codes `1083/999/939` & `1084/1000` newly pinned here).]

### The bitmap LUT — `neuron_isa_check_opcode_on_engine` @ `0x1504700` (V4)

The gate's job is the wire analog of L1's `checkValidEngines`, keyed on wire op/engine. Per engine, it `_bittest64`s the opcode against a per-engine 64-bit constant — the LUT *is* a set of immediate bitmasks, one base per engine:

```c
// neuron_isa_check_opcode_on_engine(opcode a1, wireEngine a2, version a4, INST_UNION)  @ 0x1504700
//   ACT engine:  _bittest64(0x1000000000FE7F41, a1 - 88)       // a1 rebased to -88
//   sub-ranges:  _bittest64(0x66F000100001,    a1 + 68)
//                _bittest64(...,                a1 - 95)
//                _bittest64(...,                a1 - 114)
//   DVE op 66:   → sub_1446640 (special sub-decode)
//   returns 1 iff opcode legal on that engine at that version
```

Each engine's legal-opcode set is a single 64-bit immediate; the opcode is rebased (`a1 - 88` etc.) so it indexes bit `0..63` of the mask. [STRONG — the function and its VA are symbol-table-confirmed; the bitmap immediate `0x1000000000FE7F41`, the rebase offsets, and the DVE-66 sub-decode are from the decompiled `0x1504700` body. The corpus sidecar at this symbol is a `thunk` frame; the body constants are read at the true VA. The *full* per-engine legal-opcode expansion of every mask is not enumerated here — INFERRED only as "one mask per engine, `_bittest64`-indexed".]

---

## H3 — the dual-FP8 L2-only check (the L1≠L2 divergence headline)

The single most important fact about L2 is that it is **not redundant with L1**. The proof is `s3_lw_dual_fp8_restrictions` @ `0x14ac040` — a check that rejects forbidden FP8×FP8 dual-weight-load quadrant/alignment combinations. It exists **only in `core_v4`**, **only at L2**. There is no L1 FP8-combo rule and no L0 analog: an instruction can satisfy L1's engine vector and arch-min yet fail this gate.

> **VERIFIED — dual-FP8 is core_v4-only and L2-only.** The symbol `s3_lw_dual_fp8_restrictions` appears **exactly once** in the symbol table — at `0x14ac040`, in the `core_v4` namespace. There is no `core_v2`/`core_v3` instance and no L1 (`birverifier`) instance. [CERTAIN — single symbol-table hit, `core_v4` namespace.]

It is reached from the core_v4 load-weights format gate `is_valid_s3_lw` @ `0x14ac170` (the two functions are 0x130 bytes apart; the gate's VA `0x14ac170` and the restriction's VA `0x14ac040` are both symbol-table-confirmed). The load-weights gate first bounds the weight-tile column count by dtype width, then delegates the dual-mode legality to the restriction function:

```c
// is_valid_s3_lw (core_v4 load-weights gate)  @ 0x14ac170
//  • opcode must be 1 (LdWeight wire byte); engine via is_valid_enum(4, engine); events.
//  • reserved-bit zeroing: v86.i32[2] | … || (v86.i8[15] & 0xE0) != 0  → reject
//  • dtype-by-size column bound (two sides):
//        FP8 tag 15      → cols ≤ 0x80
//        tags {3,12}     → cols ≤ 0x40
//        tags {1,2,4,8}  → cols ≤ 0x20
//        else            → reject
//  • is_valid_dtype(dt,0) + LdW sub-mode enums is_valid_enum(41/40/77/37, …)
//  • AP geometry: s3_lw_check_tensor, s3_lw_m3d_valid_src_partition,
//                 indirect_quadrant_check_src3d, s3_lw_valid_num_active_cols
//  • ⭐ THEN the dual-FP8 gate:
        if not s3_lw_dual_fp8_restrictions(INST_UNION):  return 0;
```

The restriction body itself — the forbidden-quadrant logic that has **no L1 equivalent**:

```c
// s3_lw_dual_fp8_restrictions(INST_UNION)  @ 0x14ac040
function s3_lw_dual_fp8_restrictions(u):
    sel = HIBYTE(u.a11)                       // dual-mode selector
    if sel == 0:                              // not a dual load → always legal
        return 1
    if sel == 1:                              // dual FP8, mode 1
        require BYTE5(u.a12) == 15            //   FP8 dtype tag
        require BYTE2(u.a11) == 0             //   reserved bit
        require (u.a11 - 14) <= 1             //   opcode-subtype band
        if (BYTE3(u.a9) & 0x60) == 0x20:      //   forbidden FP8 quadrant
            return 0
        if (BYTE3(u.a9) & 0x70) != 0x40:
            return (u.a10 & 0xFFFF00000000000F) == 0x2000000000000  // dual-FP8 partition word
        return 1
    if sel == 4:                              // dual FP8, mode 4
        require (u.a11 - 14) <= 1
        require (BYTE3(u.a9) & 0x60) != 0x20
        require ((BYTE3(u.a9) & 0xA0) == 0x80) || addr_aligned_dtype(u.a9 & 0x1FFFFFFF, 5)
        // … PSUM-quadrant (0x40 / 0xA0) and partition-flag (WORD2/HIWORD(a10), a10 & 0x10000)
        //   constraints, all ending in BYTE2(u.a11) == 0 && BYTE5(u.a12) == 15
        return …
    return 0                                  // any other selector → illegal
```

The legality decision turns on the **packed bytes** (`HIBYTE(a11)`, `BYTE5(a12)`, `BYTE3(a9)` quadrant nibbles, the `a10` partition-flag word) — fields that exist only in the encoded wire bundle. L1, walking the abstract IR, never has them. This is why **L1∧L2 are conjunctive**: L1 catches op-on-engine and arch-min; L2 catches the FP8×FP8 quadrant combination that is invisible above the wire. [CERTAIN that the gate exists, is core_v4-only, and is reached from `is_valid_s3_lw`; the bit-level predicate (HIBYTE selectors, `0x60/0x70/0xA0` quadrant masks, the `0x2000000000000` partition word) is STRONG — from the decompiled body read; the corpus exposes a `thunk` sidecar at the `is_valid_s3_lw` symbol, so the field offsets are read at the true VA.]

---

## H4 — the `is_valid_<fmt>` predicate family (what the gates validate)

Past the engine gate, the entry validator falls through to a per-opcode `is_valid_<fmt>` branch. There are 59/64/81 such symbols per arch; collectively they validate **five classes of wire-byte fact**. The table below pins six representative gates spanning the matmul / batchnorm / load-weights / activation / tensor-tensor / MX-quant classes; all VAs are symbol-table-confirmed.

| Gate | VA | Opcode byte(s) | Silicon fact it validates |
|---|---|---|---|
| `core_v4::is_valid_s3d3_mm` | `0x14b6770` | subtype `==2` | gen-4 matmul: PE quadrant reach, 16-wide contraction, stationary FP-tag `10`, fixed scale word `0x20A`, `(in,out)` dtype-and-stride silicon word |
| `core_v4::is_valid_s4d2_bn` | `0x14d4e50` | `97/0x82/96/108/110` | 5-variant batchnorm: dtype-restricted to `{6,7,10}`, partition-product capped (`≤256` / `≤0x4000`), PSUM-quadrant aware (`0x40`/`0xA0`), exact aggregation stride per stage, NaN/Inf float-field reject |
| `core_v4::is_valid_s3_lw` | `0x14ac170` | `==1` | load-weights: dtype-by-size column bound, LdW sub-mode enums, → **dual-FP8 gate** (H3) |
| `core_v2::is_valid_activate` | `0x12d8e10` | `0x21` | activation (shared body): in-elem `==` out-elem, `is_valid_enum(14, ActivationFunctionType)`, bias/scale-tensor quadrant, `s3d3_ac_imm/scale_checks` |
| `core_v4::is_valid_s3s3d3_tt` | `0x14cbd30` | `0x51`, `{114,-103}` | tensor-tensor ALU: `is_valid_dtype_64` (the 64-bit dtype-COMBO table), `is_valid_enum(18/32)`, AluOp-class predicate, in/out element-product match |
| `core_v4::is_valid_s3dmx1_quant` | `0x14c78f0` | `0xE3` | MX-quantize: PSUM/SBUF quadrants, `mxmem1d_valid` E8M0 block-scale 1-D pattern, `is_valid_dtype_inc_fp4` |

The five classes, distilled from the six bodies:

1. **Packed OPCODE byte** (`INST_UNION[0]`, signed in decomp) — each gate hosts several opcode-subtype variants and rejects on byte mismatch.
2. **Reserved-bit zeroing** — e.g. `(field & 0xFFFFFFFFFF000000) != 0` reject (top 40 bits must be 0), `m128i_i32[3]` must be 0, top-nibble masks.
3. **Dtype-combo silicon gates** — `is_valid_dtype` / `is_valid_dtype_64` / `is_valid_dtype_inc_fp4`, plus restricted-dtype bands (`(dtype-6)>1 && dtype!=10` ⇒ only `{6,7,10}`; FP8 tag `15` → size bound), plus `is_valid_enum(typeId, field)` with `typeId` selecting the enum domain (see below).
4. **AP-quadrant nibbles** — PSUM/SBUF selectors `byte & 0x60 == 0x20`, `& 0x70 == 0x40`, `& 0xF0 == 0xA0` (forbidden/required quadrant combos) + `addr_aligned_dtype`.
5. **AP geometry** — `start_addr_active_channels` / `tensor{2,3,4}d_valid` / `mxmem1d_valid` + partition-product bounds (`≤256` / `≤0x4000` with quadrant exceptions) and NaN/Inf float-field rejects `(x & 0x7F800000) == 0x7F800000`, reserved-float `== 0.0`.

Passing the gate ⇒ the 64-byte word is silicon-legal for that format. [CERTAIN that the six gate symbols exist at the cited VAs; the five-class decomposition and the per-gate bit predicates are STRONG — from the decompiled bodies the backing strand read; not every one of the 59/64/81 gates was opened (the six are representative of the MM/BN/LW/ACT/TT/MX classes). The remaining gates — `smx1d3_mm`, `sparsity_compress`, `stream_transpose`, `conv_lut_load`, `tensor_dequantize`, … — are INFERRED to follow the same five-class shape.]

> **VERIFIED — the QuantizeMx 0xE3 emit↔validate tie.** `is_valid_s3dmx1_quant` (@ `0x14c78f0`, symbol-table-confirmed) gates on opcode byte `-29 = 0xE3 = 227` — **exactly** the wire byte the codegen emitter stamps for `QuantizeMx` (`visitInstQuantizeMx` stamps byte `0xE3` / 16-bit word `0x10E3`; see [PE Matmul Encoding — Dense / Sparse / MX & Quantize](../isa/pe-matmul-encoding.md)). The L3 emit byte and the L2 validator branch are the **same byte** — emit and validate agree on the wire by construction. [CERTAIN that the symbol and the matmul/MX encoding page exist; the `v14 == -29` branch is from the body read, STRONG.]

### The `is_valid_enum` typeId → domain map

Several gates call `is_valid_enum(typeId, byte)` to validate a packed enum field against its silicon domain, where `typeId` selects which domain array to range-check:

| typeId | domain | typeId | domain |
|---|---|---|---|
| 4 | engine (`NEURON_ENGINE`) | 40 / 41 / 77 | LdWeight sub-modes |
| 14 | ActivationFunctionType | 44 | dtype-tag |
| 18 | dtype | 45 | AccumCmd |
| 32 | perf / round mode | 37 | LdWeight sub-mode |

(See [ISA Numeric Enum-Ordinal Tables](../isa/isa-enum-ordinals.md) for the ordinal values these `typeId`s range over.) [STRONG — the typeId→domain assignments are identified by call context in the gate bodies, not by dumping each `is_valid_enum` backing array; a full per-typeId domain-array dump is a follow-up. typeId `34` is referenced by the activation gate but its domain is INFERRED.]

---

## How L2 is driven — the deferred batch (RUN_ISA_CHECKS)

L2 does not run inline during the encoder walk. The driver mechanism — the `CodeGenMode` tristate, the per-instruction enqueue, and the TBB-parallel `flushISAChecks` replay — is documented in full on [Codegen Driver and the CodeGenMode Mechanism](codegen-driver.md). The facts that matter for *this* page:

- **The production mode is `RUN_ISA_CHECKS = 1`.** The live `Codegen::codegen` emit pass constructs its `CoreVxGen` with `CodeGenMode = 1` (it drives only modes `0` and `1`; `GENERATE_ISACODE = 2` is used **only** by the verifier's separate `Generator`, never the production codegen pass). Mode 1 both `fwrite`s the bundle to the engine stream **and** enqueues it for L2 validation. [CERTAIN — per the codegen-driver page's `CORRECTION (CGD-01)`: the production driver emits only `mov r8d,1` and `xor r8d,r8d`, never `mov r8d,2`.]

- **Enqueue** — `Generator::enqueueISAChecks` @ `0x11f1fe0` snapshots each instruction's encoded 64-byte bundle(s) + `Instruction*` into a **152-byte `ISAMapping`** record, pushed onto the vector at `this+264`. At `> 0x173180` pending bytes (= **10,000 records** × 152) it auto-calls `flushISAChecks` to bound peak memory; otherwise it defers. [CERTAIN — VA symbol-table-confirmed; the `0x173180/152 = 10000` arithmetic is exact.]

- **Flush** — `Generator::flushISAChecks` @ `0x11efb00` runs the whole batch via `tbb` `start_for` over `blocked_range<size_t>(0, n)` across `2 × max_concurrency` threads. The per-range task body invokes, per item, **vtable slot 0** of the Generator — which is the per-arch `runSingleISACheck` (`CoreV2Gen` vtable slot 0 = `0x1211cf0`; V3/V4 override with `0x134c060`/`0x1435010`). Any thread's `NeuronAssertion` is captured into a single `std::exception_ptr` and **rethrown after the parallel join** — so the engine `.bin` streams are produced, but the codegen *pass* throws if any instruction is wire-illegal. [CERTAIN — VA symbol-table-confirmed; the slot-0 virtual dispatch and rethrow semantics per the codegen-driver page.]

- **Driver** — `bir::IRVisitor<CoreV4Gen>::visit(bir::Module&)` @ `0x11d7350` walks Functions→BasicBlocks→Instructions (each `visitInst` encodes + enqueues), then drains the residual batch with `flushISAChecks` at module end. [CERTAIN — VA symbol-table-confirmed.]

The legality-layer **dispatcher** (the L0/L1/L2 selection logic) is its own in-flight sibling page; this page covers only L2's body.

> **NET.** `RUN_ISA_CHECKS` = *"encode every instruction's 64-byte word exactly as for emission, but instead of only `fwrite`ing it, also queue it and validate the whole module's wire words in a TBB-parallel batch, rethrowing the first silicon-illegality as a `NeuronAssertion`."* It is the codegen-time silicon dress rehearsal, byte-identical to what is emitted.

---

## Where the layers diverge (and why it matters)

The L1∧L2 conjunction is real, not belt-and-braces. The concrete divergence points:

- **L1 vs L2 — passing L1 ⇏ passing L2.** The canonical example is **dual-FP8** (H3): L1 has no FP8-combo rule; `s3_lw_dual_fp8_restrictions` (core_v4 only) rejects forbidden FP8×FP8 quadrant/alignment combos that L1 cannot see. An instruction can satisfy L1's engine vector + arch-min and still fail L2's silicon word.

- **L2-only facts (no L1/L0 analog).** All the §H4 field-level gates: dtype-COMBO tables (`is_valid_dtype_64`), AP-quadrant nibbles (`0x20`/`0x40`/`0xA0`), partition-product caps (`≤256`/`≤0x4000`), MX scale patterns (`mxmem1d_valid`), FP4 dtype (`dtype_inc_fp4`), NaN/Inf float-field rejects, reserved-bit zeroing, and the per-engine opcode bitmaps. These are **structurally invisible** to L1 (which never sees the packed word) and to L0 (which only knows op×engine×arch).

- **Arch-space divergence.** L0/L1 key on bir `ArchLevel(10..50)`; L2 keys on core version `V2/V3/V4` — the three validator families with **different entry arity** (H1) and **different assertion codes** (H2). An op may have **no L2 validator at some arch** — e.g. MX-quant (`s3dmx1_quant`) exists only in core_v4's roster, so on V2/V3 its opcode matches no `is_valid_<fmt>` branch and the cascade falls through to the default-reject tail. The `ArchLevel→coreVersion` projection lives upstream of `runSingleISACheck` (in the `CoreV?Gen` selection); a drift there could let L1 pass an arch whose L2 version-validator rejects. [HIGH — the divergence is structural (per the roster diff and the arity/code divergence); it is not demonstrated here on a single concrete op, and the upstream projection is not traced on this page.]

**Invariant:** the single defended runtime legality contract in this wheel is **L1 (birverifier, pre-codegen, bir-keyed) ∧ L2 (`runSingleISACheck`, in-codegen, wire-keyed)**. L0 is dormant data; L3 is a front-end fast-fail. [CERTAIN — per the verifier-strand and the full L2 per-arch/per-format read.]

---

## Adversarial self-verification

The five strongest claims on this page, re-checked against the binary symbol table (not the backing report):

1. **Three divergent entry signatures (H1).** ✅ `is_valid_neuron_instruction` @ `0x1332540` (no ctx), `is_valid_neuron_isa_instruction` @ `0x141ed10` (`+CORE_VERSION`), `is_valid_neuron_engine_instruction` @ `0x1502120` (`+CORE_VERSION +NEURON_ENGINE`) — all three demangled signatures and VAs read directly from the readelf symbol table. **CERTAIN.**
2. **Roster counts 59/64/81 (H1/H3).** ✅ Counting unique `is_valid_*` `FUNC` symbols per arch namespace (excl. `dbg_`) gives exactly `59 / 64 / 81`. **CERTAIN.**
3. **Dual-FP8 is core_v4-only, L2-only (H3).** ✅ `s3_lw_dual_fp8_restrictions` appears once in the symbol table — `0x14ac040`, `core_v4` namespace; no V2/V3/birverifier instance. **CERTAIN.**
4. **Per-arch `runSingleISACheck` + per-arch engine gate (H2).** ✅ `runSingleISACheck` V2/V3/V4 @ `0x1211cf0`/`0x134c060`/`0x1435010`; `instruction_engine_check` @ `0x13346e0` (core_v2); `neuron_isa_check_opcode_on_engine` @ `0x1421840` (v3) / `0x1504700` (v4) — all symbol-table-confirmed. The assertion codes (`935/934/2083`, etc.) are body-read, **STRONG.**
5. **QuantizeMx `0xE3` emit↔validate tie (H4).** ✅ `is_valid_s3dmx1_quant` @ `0x14c78f0` symbol-table-confirmed; the `v14 == -29 = 0xE3` branch is body-read, **STRONG** — the matching emit byte is on the PE/MX encoding page.

**Honest re-verify ceiling.** I independently re-grounded every *symbol, VA, signature, and count* against the readelf symbol table. The *bit-level gate predicates* (the `0x60/0x70/0xA0` quadrant masks, the dtype bands, the `0x1000000000FE7F41` ACT bitmap, the dual-FP8 selector logic) are from the decompiled function bodies the backing strand read; the corpus exposes only `thunk` sidecars at several of these symbols (the two-VA-frame artifact), so the field offsets are at the true VA and were **not** independently re-derived byte-by-byte here. The `convert()` remap (`sub_1433840`) is an internal `sub_`, not exported — not symbol-table-cross-checkable. The full per-typeId `is_valid_enum` domain dump (G4 of the backing strand) and the full per-engine opcode-bitmap expansion remain follow-ups.

---

## Cross-References

- [Codegen Driver and the CodeGenMode Mechanism](codegen-driver.md) — the `RUN_ISA_CHECKS` mode, `enqueueISAChecks`/`flushISAChecks`, and the slot-0 virtual dispatch that *invokes* this validator.
- [The 64-Byte Instruction Bundle & Header Skeleton](../isa/instruction-bundle.md) — the wire word these gates validate.
- [PE Matmul Encoding — Dense / Sparse / MX & Quantize](../isa/pe-matmul-encoding.md) — the `QuantizeMx 0xE3` emit byte tied to `is_valid_s3dmx1_quant`.
- [ISA Numeric Enum-Ordinal Tables](../isa/isa-enum-ordinals.md) — the ordinals the `is_valid_enum(typeId, …)` calls range over.
- The L0/L1/L2 legality **dispatcher** is a separate in-flight backend-verifiers page (the layer-selection logic upstream of this validator's body).
