# Activation Encoding

> *All addresses, offsets, and table bytes on this page are read from `neuronx_cc` build `2.24.5133.0+58f8de22`, cp310 wheel — `libwalrus.so` (the backend ELF that owns the byte-emitters). See [Build & Version Provenance](../reference/versions.md). Other builds will shift addresses; the field offsets are wire-format and gen-stable.*

## Abstract

The Activation engine is Trainium's scalar/elementwise unit (`EngineType 2`, external name "Activation"/"Scalar"). Where the [PE array](../arch/pe-engine.md) reduces into PSUM, the Activation engine does the pointwise leg — an affine pre-scale, a transcendental from a loaded LUT set, an optional bias add, and an optional channel accumulation — one pass per element. This page is the **wire encoding** of the four instructions that drive that datapath: `Activation` (opcode `0x21`), `Activation2` (`0x25`, the gen-4 two-dimensional form), `LoadActFuncSet` (`0x23`, the LUT-set selector), and `ActivationReadAccumulator` (`0x24`, the accumulator drain). The engine itself, its single-set LUT residency, and the `act_tbl_sel` → LUT-load mechanism are owned by [Activation Engine](../arch/activation-engine.md); here we encode bits.

Every Activation-family instruction is a slot in the 64-byte [instruction bundle](instruction-bundle.md). The encoder is a `CoreVnGenImpl::visit*` method that runs one of three arms keyed on the codegen-mode word `*((int*)this+156)`: `1 = GENERATE_ISACODE` (writes a fresh 64-byte bundle and `fwrite`s `0x40` bytes to the engine's `Bin` file), `2 = RUN_ISA_CHECKS` (writes the same fields into stack scratch and runs the ISA assertions), `0 = COLLECT_OPCODES` (records the opcode integer into a per-instruction `set<unsigned>` and emits nothing). The GENERATE and RUN arms write **identical** field offsets; every offset below is taken from the clean `*(bundle+N) = …` stores in the GENERATE arm. `setupHeader` (vtable slot +72) stamps the opcode byte at `+0x00` and the common header.

The legacy `0x21` form encodes into `NEURON_ISA_TPB_S3D3_AC_STRUCT` (validator field-names carry the `d3_ac_` prefix); the gen-4 `0x25` form encodes into `s2d2_ac_struct` over `MEM_PATTERN2D` descriptors; `0x24` encodes into `D1_RD_STRUCT` (`d1_rd_struct`) over a one-dimensional `TENSOR1D`. The three share one helper family — a `DTYPE` remap table, an `ActivationFunctionType` remap table, an `EngineAccumulationType` remap, and the per-operand mem-pattern packer — each with a CoreV2 and a CoreV4 twin.

For reimplementation, the contract is:

- The **opcode bytes** and which `visit*` method owns each (and that `0x21` and `0x24` share one encoder, selected by output count).
- The **`s3d3_ac` / `s2d2_ac` / `d1_rd` field-to-offset map** — every `dtype`, `imm`, `accum`, `func` byte and its BIR-member source.
- The **`DTYPE` / `ActivationFunctionType` / `EngineAccumulationType` / `AluOp`** remap tables, byte-for-byte from `.rodata`.
- The **second-immediate (`imm1`) path** and the byte that gates it.
- The **`act2_quadrant_restrictions_errata_3071`** constraint that the `0x25` validator enforces.

| | |
|---|---|
| **Engine** | `EngineType 2` — "Activation" / "Scalar" |
| **`0x21` encoder** | `CoreV2GenImpl::visitInstActivation` @ `0x12596f0` (dual: also emits `0x24`) |
| **`0x25` encoder** | `CoreV4GenImpl::visitInstActivation` @ `0x143bbb0` (gen-4 only; defers to `0x12596f0` when not in act2 mode) |
| **`0x23` encoder** | `CoreV2GenImpl::visitInstLoadActFuncSet` @ `0x1250b30` (gen-invariant) |
| **`0x24` encoder** | `CoreV2GenImpl::visitInstActivationReadAccumulator` @ `0x1234f70` (+ twin `visitInstReadActivationAccumulator` @ `0x125b830`) |
| **Bundle size** | 64 bytes (`std::array<unsigned char,64>`), zero-filled by `emplace_back` |
| **Codegen-mode word** | `*((int*)this+156)` — `1`=GENERATE, `2`=RUN_ISA, `0`=COLLECT |
| **Opcode integers** | `0x21`=33, `0x23`=35, `0x24`=36, `0x25`=37 |

> **NOTE —** "s3d3" vs "d3_ac": the wire struct type is `NEURON_ISA_TPB_S3D3_AC_STRUCT` (recovered from `setupSyncUpdate<…NEURON_ISA_TPB_S3D3_AC_STRUCT>` in `libwalrus` `.rodata`). The ISA **validator** field-names use the `d3_ac_` prefix (`d3_ac_element_count`, `d3_ac_bias_check`, …). Both names are binary-derived; this page uses `s3d3_ac` for the struct and `d3_ac_` for the named checks, matching the binary.

---

## Activation — opcode `0x21`

### Purpose

The legacy single-output activation: read one input access-pattern, optionally pre-scale by `imm0` and bias by an immediate pointer, apply the activation function selected by the `func` byte, optionally accumulate channel-wise, write one output. The encoder is `CoreV2GenImpl::visitInstActivation` @ `0x12596f0` — a **dual** encoder: the same `bir::InstActivation` visit produces wire opcode `0x21` ("Activate", one output) **or** `0x24` ("ActivationReadAccumulator", two outputs) depending on the output-list element count.

### Entry Point

```text
CoreV4GenImpl::visitInstActivation (0x143bbb0)   ── gen-4 dispatch
  ├─ *(a2+296)==0  → tail-call CoreV2GenImpl::visitInstActivation (0x12596f0)   ── legacy 0x21 path
  └─ else          → emit s2d2_ac_struct opcode 0x25                            ── §Activation2
CoreV2GenImpl::visitInstActivation (0x12596f0)   ── 0x21 / 0x24 encoder
  selector = output-list element count:
    count==1 → opcode 0x21 (this section)
    count==2 → opcode 0x24 fused read (§ReadAccumulator)
    else     → reportError "Activation instruction can only have 1 or 2 outputs"
```

### Algorithm

The output-count selector, the four-argument contract, and the GENERATE-arm stores, as annotated pseudocode against `0x12596f0`:

```c
function CoreV2_visitInstActivation(this, inst):              // 0x12596f0
    n_out = count(inst.outputs[a2+192 .. ])                   // list head a2+192
    if n_out != 1 and n_out != 2:
        reportError("Activation instruction can only have 1 or 2 outputs")  // @+1336
    if *((int*)this+156) == 0:                                // COLLECT_OPCODES
        record_opcode(33); return
    // ---- GENERATE arm (mode==1); bundle = 64B array `v19` ----
    setupHeader(this, bundle, &33)                            // vtbl+72 → bundle[0x00]=0x21
    n_args = count(inst.args)
    if n_args != 4:
        reportError("Activation instruction can only have 4 arguments")      // @+1339

    bundle[0x23] = ACTFUNC_map(inst[a2+0x120])                // FUNC byte, sub_1203820
    acc = inst[a2+0x124]                                      // EngineAccumulationType
    bundle[0x0E] = (acc==0) ? 3 : ACCUM_map(acc)             // accumulator_cmd, sub_12038D0

    in_ap = getArgument<AccessPattern>(inst, 0)
    bundle[0x20] = DTYPE_map(in_ap.dtype) & 0x0F              // in_dtype = low nibble
    imm = getImmediatePointer(inst)                           // sub_123DF20 → args[1]
    switch imm.kind[imm+0x18]:                                // Argument kind tag
      case 1 (LocationSB/PSUM):
        assert(imm.isLocationSB() || imm.isLocationPSUM())   // "ISA immediate pointer ..."
        bundle[0x0F] = 0                                      // imm0_src = mem-ptr
        bundle[0x28] = slot32_resolver(this+76, imm)         // SB/PSUM slot id (dword)
      case 3 (RegisterAccessPattern):
        bundle[0x0F] = 2                                      // imm0_src = register
        bundle[0x28] = Register::getRegId(imm.reg[+232])
      else if dyn_cast<ImmediateValue>(imm):
        bundle[0x0F] = 1                                      // imm0_src = inline float
        *(float*)&bundle[0x28] = imm.getValue<float>()
      else: reportError("contains wrong argument type")

    bundle[0x20] |= (DTYPE_map(imm.dtype) << 4) & 0xF0       // imm_dtype = high nibble
    pack_mem_pattern(getImm0(inst), &bundle[0x0D])           // imm0 AP byte, sub_12051E0

    func_byte = bundle[0x23]                                  // <-- gate reads the FUNC byte
    if (uint8)(func_byte + 0x80) <= 1:                       // func == -128/-127 (sat forms)
        bundle[0x0C] = 1; *(dword)&bundle[0x2C] = 0          // imm1 disabled / unit pattern
    else if func_byte == 4:
        a3 = getArgument(inst, 3)
        pack_mem_pattern(a3, &bundle[0x0C])                  // imm1 AP byte + value@+0x2C

    out_ap = getOutput<AccessPattern>(inst, 0)
    bundle[0x21] = DTYPE_map(out_ap.dtype)                    // out_dtype
    bundle[0x22] = in_ap.firstAPPair.num                      // num_active_channels
    assignAccess<TENSOR3D>(...) x2                            // in then out s3d3 mem-pattern
    fwrite(bundle, 1, 0x40, findBin(this, inst))
```

> **GOTCHA — the second-immediate gate reads the *func* code, not the accumulator.** At `0x12596f0` (line 563) the gate reads `*(bundle+0x23)`, which is the byte written moments earlier as the activation-function code (line 461, `sub_1203820`). The accumulator command lives at `+0x0E` and plays no part. So the `imm1` second-operand path is selected by `func == 4` or by the `±0x80` saturate forms — matching the engine's affine/dual-immediate function variants.

### Field Map — `s3d3_ac_struct` (bundle `v19`)

All rows are clean `*(bundle+N) = …` stores in the GENERATE arm of `0x12596f0`. Offsets are hex byte positions in the 64-byte bundle.

| Offset | Width | Field (ISA / `d3_ac_`) | Source / encoder | Confidence |
|---|---|---|---|---|
| `0x00` | 1 | `opcode` = `0x21` | `setupHeader(this,bundle,&33)`, vtbl+72 | CERTAIN |
| `0x0C` | 1 | `imm1` mem-pattern ctrl (s3d3 AP byte) | `sub_12051E0(arg3,&bundle[0x0C])` — only when `func==4`; else `=1` | CERTAIN |
| `0x0D` | 1 | `imm0` mem-pattern ctrl (s3d3 AP byte) | `sub_12051E0(getImm0,&bundle[0x0D])` | CERTAIN |
| `0x0E` | 1 | `accumulator_cmd` (`EngineAccumulationType` wire) | `sub_12038D0(inst[+0x124])`; default `3` when BIR field `==0` | CERTAIN |
| `0x0F` | 1 | `imm0_src` (`0`=mem-ptr, `1`=inline float, `2`=register) | trinary on immediate-pointer kind | CERTAIN |
| `0x20` | 1 | `in_dtype` (bits `[3:0]`) ∣ `imm_dtype` (bits `[7:4]`) | `sub_120E650(in.dtype)` ∣ `16·sub_120E650(imm.dtype)` | CERTAIN |
| `0x21` | 1 | `out_dtype` | `sub_120E650(output0.dtype@+48)` | CERTAIN |
| `0x22` | 1 | `num_active_channels` (`d3_ac_channel_check`) | `in_ap[+0x50]→[+8]`, guarded by `in_ap[+0x58]!=0` | CERTAIN |
| `0x23` | 1 | `activation_func` code | `sub_1203820(inst[+0x120])` | CERTAIN |
| `0x28` | 4 | `imm0` value / reg-id / SB-PSUM slot (union, by `imm0_src`) | slot resolver / `getValue<float>` / `getRegId` | CERTAIN |
| `0x2C` | 4 | `imm1` value (only if `func==4`; else `0`) | `sub_12051E0` imm-resolver on `arg3` | CERTAIN |

Bytes `+0x01..+0x0B`, `+0x10..+0x1F`, `+0x24..+0x27`, `+0x30..+0x3F` are never touched by the encoder; they stay zero from the `emplace_back` zero-fill and are checked by the `d3_ac_reserved_zero` validator.

### Argument & Immediate Contract

The instruction takes exactly four arguments (`reportError "Activation instruction can only have 4 arguments"` @+1339):

- `args[0]` — input `AccessPattern`.
- `args[1]` — immediate pointer (bias/scale ptr in SB/PSUM, or an inline `ImmediateValue`, or a `RegisterAccessPattern`). Decoded trinary; sets `imm0_src` (`+0x0F`) and `imm0` (`+0x28`).
- `args[2]` — `imm0` scalar (its s3d3 AP byte packed to `+0x0D`).
- `args[3]` — `imm1` scalar, **only consumed when the func gate fires** (`func==4`), packed to `+0x0C`/`+0x2C`.
- `output[0]` — result `AccessPattern`.

The immediate-pointer kind tag (`imm[+0x18].i32` in the decompile) drives `imm0_src`:

| Kind tag | `imm0_src` (`+0x0F`) | `imm0` (`+0x28`) | Guard string | Confidence |
|---|---|---|---|---|
| `1` (SB/PSUM ptr) | `0` | SB/PSUM slot dword (`this+76` resolver, slot+32) | "ISA immediate pointer should only be SB/PSUM accesses" | CERTAIN |
| `3` (Register AP) | `2` | `Register::getRegId(reg@+232)` | "offset register cannot be applied to register immediate in ISA" | CERTAIN |
| `ImmediateValue` | `1` | `getValue<float>()` (written as a 4-byte float) | — | CERTAIN |
| other | — | reportError "contains wrong argument type" | — | CERTAIN |

> **GOTCHA —** the `0x21` encoder and the `0x24` accumulator-drain encoder are the *same* C++ method (`0x12596f0`). A reimplementation that maps one BIR `InstActivation` to exactly one wire instruction is wrong: when the instruction carries **two** outputs, the encoder additionally records opcode `0x24` (the fused HW-accumulator drain) — and on `arch ≥ core_v5` (`*((int*)this+150) > 49`) that two-output form is rejected with "ActivationReadAccumulator not supported in core_v5" (`CoreV2GenImpl.cpp:1426`).

---

## Activation2 — opcode `0x25`

### Purpose

The gen-4 (CoreV4) two-dimensional scalar form: `s2d2_ac_struct` over `MEM_PATTERN2D` source/destination descriptors, with a richer datapath — a pre-ALU op (`op0`), a post-ALU op (`op1`), a reduce op, a per-argument dtype byte, and a saturate/reverse control byte. `CoreV4GenImpl::visitInstActivation` @ `0x143bbb0` is gen-4-only; if the instruction is *not* in act2 mode (`*(a2+296)==0`) it tail-calls the legacy `0x12596f0` encoder.

### Algorithm

```c
function CoreV4_visitInstActivation(this, inst):              // 0x143bbb0
    if inst[a2+0x148] (variant tag) set: throw bad_variant
    if inst[a2+296] == 0:
        return CoreV2_visitInstActivation(this, inst)         // legacy 0x21 path
    if mode == COLLECT: record_opcode(37); return
    // ---- GENERATE arm; bundle = 64B array `v25` ----
    if vtbl+72 == CoreV4GenImpl::setupHeader:
        *(uint16*)&bundle[0x00] = 0x1025                      // opcode 0x25 | header 0x10
    else: setupHeader(this, bundle, &v57)

    in_ap = getArgument<AccessPattern>(inst, 0)
    assignAccess<MEM_PATTERN2D>(in_ap)                        // src descriptor, low band
    bundle[0x22] = in_ap.firstAPPair.num                      // num_active_channels (in)
    bundle[0x20] = DTYPE4_map(in_ap.dtype)                    // in_dtype, sub_14347C0

    a1 = getArgument(inst,1); pack_mp2d(a1, &bundle[0x19]); bundle[0x1C] = DTYPE4_map(a1.dtype)
    a2_= getArgument(inst,2); pack_mp2d(a2_,&bundle[0x1B]); bundle[0x1C] = DTYPE4_map(a2_.dtype)
    a3 = getArgument(inst,3); pack_mp2d(a3, &bundle[0x18]); bundle[0x1C] = DTYPE4_map(a3.dtype)
    //  ^ imm_dtype byte @0x1C is written three times; arg3 wins.

    out_ap = getOutput<AccessPattern>(inst, 0)
    assignAccess<MEM_PATTERN2D>(out_ap)                       // dst descriptor, +0x30 band
    bundle[0x21] = DTYPE4_map(out_ap.dtype)                   // out_dtype
    bundle[0x22] = out_ap.firstAPPair.num                     // num_active_channels (out wins)

    bundle[0x1D] = ALUOP_map(inst[+0x150])                    // op0,  sub_142E030
    bundle[0x1E] = ALUOP_map(inst[+0x154])                    // op1
    bundle[0x23] = ACTFUNC4_map(inst[+0x120])                 // activation_func, sub_142E2C0
    bundle[0x1F] = ALUOP_map(inst[+0x1A8])                    // reduce_op
    bundle[0x1A] = passthrough(ACCUM4_map(inst[+0x124]))      // accumulator_cmd, sub_142DF40→sub_1434C50

    // ---- saturate / reverse pack @0x3C ----
    if inst[+0x158] (reverse0):                               // a2+344
        bundle[0x3C] = (inst[+0x180]==0) ? 1 : 3              // saturate arm
    else:
        bundle[0x3C] = 2 * inst[+0x180]                       // normal arm (2*reverse1)
```

> **GOTCHA — the gen-4 header word is `0x1025`, and the decompile shows it in decimal.** `*(uint16*)v25 = 4133` at `0x143bbb0` line 258 is the little-endian word `0x1025`: low byte `0x25` = opcode 37, high byte `0x10` = the header length. Reading the decimal `4133` as hex gives the wrong word.

### Field Map — `s2d2_ac_struct` (bundle `v25`)

All rows are clean `bundle[N] = …` byte stores in the GENERATE arm of `0x143bbb0` (`v25` is a `_BYTE*` into the 64-byte `emplace_back` array).

| Offset | Width | Field (`s2d2_ac_struct`) | Source / encoder | Confidence |
|---|---|---|---|---|
| `0x00` | 2 | `opcode` word `0x1025` (opcode `0x25` + header `0x10`) | `setupHeader` / `*(uint16*)v25=4133` | CERTAIN |
| `0x18` | 1 | `imm1` / arg3 AP byte (`s2d2_ac_ts` op) | `sub_142E370(arg3,&v25[0x18])` | CERTAIN |
| `0x19` | 1 | `imm0` / arg1 AP byte | `sub_142E370(arg1,&v25[0x19])` | CERTAIN |
| `0x1A` | 1 | `accumulator_cmd` / `reduce_cmd` | `sub_1434C50(sub_142DF40(inst[+0x124]))` | CERTAIN |
| `0x1B` | 1 | arg2 AP byte (op variant) | `sub_142E370(arg2,&v25[0x1B])` | CERTAIN |
| `0x1C` | 1 | `imm_dtype` (per-arg, **last-wins** = arg3) | `sub_14347C0(arg{1,2,3}.dtype)` ×3 | CERTAIN |
| `0x1D` | 1 | `op0` (pre ALU op) | `sub_142E030(inst[+0x150])` | CERTAIN |
| `0x1E` | 1 | `op1` (post ALU op) | `sub_142E030(inst[+0x154])` | CERTAIN |
| `0x1F` | 1 | `reduce_op` | `sub_142E030(inst[+0x1A8])` | CERTAIN |
| `0x20` | 1 | `in_dtype` (arg0) | `sub_14347C0(arg0.dtype@+48)` | CERTAIN |
| `0x21` | 1 | `out_dtype` | `sub_14347C0(output0.dtype@+48)` | CERTAIN |
| `0x22` | 1 | `num_active_channels` (in then out; **out wins**) | `AP.firstAPPair.num` (`AP[+0x50]→[+8]`) | CERTAIN |
| `0x23` | 1 | `activation_func` | `sub_142E2C0(inst[+0x120])` | CERTAIN |
| `0x3C` | 1 | saturate / reverse control | `reverse0`(`+0x158`)/`reverse1`(`+0x180`) — see below | CERTAIN |

In/out `MEM_PATTERN2D` descriptors are placed by two `assignAccess<NEURON_ISA_TPB_MEM_PATTERN2D>` calls (in band low, out band at `+0x30`).

The `+0x3C` saturate/reverse byte packs two BIR booleans:

```c
reverse0 = inst[+0x158];  reverse1 = inst[+0x180];          // a2+344, a2+384
if (inst[+0x178] /*a2+376*/ || inst[+0x1A0] /*a2+416*/): take_act2_arm  // bad-variant escapes
if (reverse0): bundle[0x3C] = (reverse1 == 0) ? 1 : 3;      // saturate set-select
else:          bundle[0x3C] = 2 * reverse1;                 // normal {0,2}
```

### High-Level Roster

The `s2d2_ac_struct` field roster, recovered from the `activate2_info` pybind module (`__pyx_n_u_*`): `in_dtype`, `out_dtype`, `imm_dtype`, `num_active_channels`, `activation_func` (`ActivationFunc`), `op0`/`op1` (`AluOp`), `reduce_op`/`reduce_cmd` (`ReduceCmd`), `reverse0`/`reverse1`/`reverse_operands`, `imm0`/`imm0_src`, `imm1`/`imm1_src`, `relu_param`/`relu_param_src` (`ActivationImm`), `src`/`dst` (with `src_mem_pattern`/`dst_mem_pattern` `MemPattern2d`), `has_valid_reduce_op`, `has_valid_s2d2_ac_ts_ops`, and `act2_quadrant_restrictions_errata_3071`. The `has_valid_s2d2_ac_ts_ops` flag gates the three tensor-scalar AP bytes at `+0x18`/`+0x19`/`+0x1B`. (All confirmed present in `activate2_info.cpython-310-x86_64-linux-gnu.so`.)

> **QUIRK — the quadrant errata.** The gen-4 Activate2 validator enforces `act2_quadrant_restrictions_errata_3071`, a hardware-erratum gate that couples the *source operand's memory quadrant* to the legal placement of its **pointer immediates** (bias/scale pointers). Verbatim from `activate2_info` `.rodata`:
>
> > *"When src is in SBUF, pointer immediates must be in same SBUF quadrant; when src is in PSUM, all immediates must be instruction immediates or from same SBUF quadrant."*
>
> So: if `src ∈ SBUF`, every pointer immediate (`imm0`/`imm1` when `imm*_src` = mem-ptr) must reside in the **same SBUF quadrant** as `src`. If `src ∈ PSUM`, pointer immediates are illegal unless they are inline instruction immediates (`imm*_src` = imm) **or** all drawn from a single SBUF quadrant. The checker is `birverifier::checkActivate2QuadrantRestrictions` (@ `0x100e3b0`), which reports per-operand `src_quadrant` / `imm0_quadrant` / `imm1_quadrant` / `relu_param_quadrant`. A reimplementer placing bias pointers in SBUF must align their quadrant to `src` or the bundle is rejected. (The legacy `0x21` path enforces the analogous SB/PSUM-only rule for its immediate pointer via the `d3_ac_bias_check` / `d3_ac_scale_check` validators — see §Activation.)

---

## LoadActFuncSet — opcode `0x23`

### Purpose

Selects which LUT function-set is resident on the engine. It carries exactly one operand-specific field: a zero-based integer **set index** (`act_func_set_id` in BIR-JSON, `act_tbl_sel` in the silicon encoder). There is **no LUT source address** — the table images are engine/ROM-resident and the index merely selects which one becomes active. This is the wire face of the engine's single-set residency; the LUT-load mechanism, set-cover, and PWP polynomial model are owned by [Activation Engine](../arch/activation-engine.md) and [Part 10 — PWP](../activation/).

### Algorithm

```c
function CoreV2_visitInstLoadActFuncSet(this, inst):          // 0x1250b30
    if mode == COLLECT: record_opcode(35); return
    bundle = emplace_back zero-filled 64B array
    setupHeader(this, bundle, &35)                            // bundle[0x00] = 0x23
    setupCommon(bundle, inst); setupOutputs(bundle, inst)     // sub_122D280 / sub_122CFC0
    if inst[a2+272] != 0: throw bad_variant                   // variant guard
    idx = inst[a2+0xF0]                                       // act_func_set_id (dword)
    range_checked_set(&bundle[0x23], "instr.act_tbl_sel", idx, inst)   // sub_124E710
    fwrite(bundle, 1, 0x40, findBin(this, inst))
```

### Field Map

| Offset | Width | Field | Source / encoder | Confidence |
|---|---|---|---|---|
| `0x00` | 1 | `opcode` = `0x23` | `setupHeader(this,bundle,&35)` | CERTAIN |
| `0x23` | 1 | `act_tbl_sel` (= `act_func_set_id` index) | `sub_124E710(&bundle[0x23], "instr.act_tbl_sel", inst[+0xF0])`, range-checked int→byte | CERTAIN |

> **NOTE — `+0x23` is one silicon byte with two roles.** `LoadActFuncSet` writes a table-**set index** to `+0x23`; the subsequent `Activation`/`Activation2` reads an activation-function **code** at the same `+0x23`. They are the two roles of the engine's act-func ROM-addressing knob: the load installs which set is resident, the activate selects which function within it. The `act_func_set_id` source `inst+0xF0` and the bundle byte `+0x23` exactly match the [Activation Engine](../arch/activation-engine.md) page's `inst+0xF0 → +0x23` framing. The range-check validators in `.rodata` are `valid_act_tbl_sel` / `zero_act_tbl_sel`.

The producer that *creates* `bir::InstLoadActFuncSet` nodes (not the byte encoder) is the PWP-lowering pass `LowerPWPImpl::generateInstLoadActFuncSet`, which resolves the set name to its array order via the `AllActSetName2ActInfo` map and stamps `act_func_set_id`; a separate scheduling pseudo-opcode `0xC6` "PseudoLoadActFuncSet" exists for pass-time bookkeeping. (See [Activation Engine](../arch/activation-engine.md).)

---

## ActivationReadAccumulator — opcode `0x24`

### Purpose

Drains the engine's hardware accumulator into a one-dimensional `TENSOR1D` output — `d1_rd_struct`. Reached three ways: the standalone `visitInstActivationReadAccumulator` @ `0x1234f70`, its byte-identical twin `visitInstReadActivationAccumulator` @ `0x125b830`, and the fused `count==2` path inside `CoreV2GenImpl::visitInstActivation` (§Activation), which emits a `0x24` back-to-back with the `0x21` Activate so the accumulator drain rides on the activation.

### Algorithm

```c
function CoreV2_visitInstActivationReadAccumulator(this, inst):   // 0x1234f70
    if mode == COLLECT: record_opcode(36); return
    bundle = emplace_back zero-filled 64B array
    setupHeader(this, bundle, &36)                            // bundle[0x00] = 0x24
    setupCommon(bundle, inst)                                 // sub_12270D0 / sub_1232720
    out_ap = getOutput<AccessPattern>(inst, 0)
    assignAccess<TENSOR1D>(out_ap)
    bundle[0x20] = DTYPE_map(out_ap.dtype@+48)                // dtype, sub_120E650
    bundle[0x22] = out_ap.firstAPPair.num                     // num_active_channels
    bundle[0x21] = 0                                          // d1_rd_reserved_zero / has_zero_negated
    *(dword)&bundle[0x1C] = out_ap.getNumElementsPerPartition()   // dst_element_count
    fwrite(bundle, 1, 0x40, findBin(this, inst))
```

### Field Map — `d1_rd_struct` (bundle `v6`)

| Offset | Width | Field (`d1_rd_struct`) | Source / encoder | Confidence |
|---|---|---|---|---|
| `0x00` | 1 | `opcode` = `0x24` | `setupHeader(this,bundle,&36)` | CERTAIN |
| `0x1C` | 4 | `dst_element_count` (`read_accumulator_dst_size`) | `getNumElementsPerPartition()` (RUN arm range-checks via `sub_1250E50`) | CERTAIN |
| `0x20` | 1 | `dtype` (`read_accumulator_type_check`) | `sub_120E650(output0.dtype@+48)` | CERTAIN |
| `0x21` | 1 | `negated` / `reserved_zero` (`read_accumulator_has_zero_negated`) | `= 0` (the twin @`0x125b830` relies on zero-fill) | CERTAIN |
| `0x22` | 1 | `num_active_channels` | `out_ap[+0x50]→[+8]` (same accessor as `0x21`/`0x25`) | CERTAIN |

The RUN-ISA arm asserts the one-dimensionality of the drain: "ACTIVATION_READ_ACCUMULATOR doesn't support TensorIndirect AP since ISA only has one dimensional AP", "ScalarEng read accum instr dst element count must be 1", and "ScalarEng read accum dst_mem_pattern.num_elem[0] must be 1" (the twin uses "Scalar Engine read accumulator dst_element_count must be 1" / "… dst_mem_pattern.num_elem[0] must be 1"). The only encode-time delta between the twins is that `0x1234f70` explicitly stores `bundle[0x21]=0` while `0x125b830` relies on zero-fill.

---

## Shared Field-Mapper Tables

Each BIR enum is remapped to a wire byte through a small `.rodata` table or a `switch`. The tables come in a CoreV2 / CoreV4 pair; the bytes below are read directly from `libwalrus.so` `.rodata` (VA == file offset; `.rodata` base `0x1c72000`).

### DTYPE map

`in_dtype` / `out_dtype` / `imm_dtype` all go through these. Index domain is `[0, 0x13]`; out-of-range raises `NeuronAssertion "false"` at `CoreV2GenImpl.cpp:470`.

| Index | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **V2** (`byte_1DF5760`, `sub_120E650`) | 3 | 2 | 5 | 13 | 14 | 14 | 3 | 15 | 14 | 15 | 5 | 4 | 6 | 7 | 9 | 8 | 10 | 11 | 1 | 12 |
| **V4** (`byte_1DFBAD0`, `sub_14347C0`) | 3 | 2 | **16** | 13 | 14 | 14 | 3 | 15 | 14 | 15 | 5 | 4 | 6 | 7 | 9 | 8 | 10 | 11 | 1 | 12 |

The only delta is index `2` (V4 = 16 vs V2 = 5) — gen-4 relabelled that dtype. Both tables' bytes are read from `.rodata`.

### ActivationFunc map

`sub_1203820` (V2, `byte_1DF57A0`) and `sub_142E2C0` (V4, `byte_1DFBAF0`) are **byte-identical** 30-entry tables; domain `[0, 0x1D]`, out-of-range → "Invalid enum variant for enum ActivationFunctionType".

| Index | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 | 21 | 22 | 23 | 24 | 25 | 26 | 27 | 28 | 29 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Wire** | 1 | 30 | 2 | 3 | 4 | 5 | 6 | 7 | 9 | 8 | 29 | 10 | 21 | 19 | 28 | 31 | 23 | 24 | 25 | 26 | 38 | 32 | 22 | 128 | 33 | 129 | 34 | 36 | 37 | 35 |

Both tables are read from `.rodata` and are identical. Note the wire values `128`/`129` (signed `-128`/`-127`) at indices 23/25 — these are the saturate-form func codes the `0x21` second-immediate gate keys on (`(uint8)(func+0x80) <= 1`).

### EngineAccumulationType map

`accumulator_cmd` (`+0x0E` for `0x21`, `+0x1A` for `0x25`) is a `switch`, not a table — `sub_12038D0` (V2) and `sub_142DF40` (V4) are structurally identical. The BIR enum order is `[Idle, ZeroAccumulate, AddAccumulate, LoadAccumulate]` plus a forbidden `Accumulate`.

| BIR `EngineAccumulationType` | Wire value | Confidence |
|---|---|---|
| `0` (Idle) | `0` | CERTAIN |
| `1` (ZeroAccumulate) | `1` | CERTAIN |
| `3` (LoadAccumulate) | `3` | CERTAIN |
| `4` | `2` | CERTAIN |
| `5` | `4` | CERTAIN |
| `2` (Accumulate) / other | reportError "Invalid enum variant for enum EngineAccumulationType" (unreachable) | CERTAIN |

At the `0x21` call site, a BIR value of `0` (Idle) is substituted with wire `3` before the map (the GENERATE arm's `acc==0 ? 3 : …`). The `exponential_info` pybind documents the imm1 rule this couples to: under `LoadAccumulate`, `imm1` may be any valid immediate source; under `ZeroAccumulate`/`Idle`/`Accumulate`, `imm1_src` must be `InstructionImmediate` and `imm1` must be `0`.

### AluOp map (gen-4 only)

`op0` / `op1` / `reduce_op` go through `sub_142E030`: identity for `0..18`, then a remap into the gen-4 ALU opcode space. (The legacy `0x21` band has no separate ALU-op bytes; its op is folded into the func/accum bytes.) `sub_1434C50` is an identity passthrough used to forward the accum byte unchanged.

| In | 19 | 20 | 21 | 22 | 23 | 26 | 27 | 28 | 29 | 30 | 31 | 32 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Out** | 24 | 19 | 20 | 22 | 21 | 26 | 27 | 29 | 25 | 32 | 33 | error (`-56`) |

The notable swaps are `19↔24`, `23↔21`, `28↔29`, `29↔25`, all read from the decompiled `switch`.

---

## Join — Offsets ↔ Validator Names ↔ BIR Members

`s3d3_ac` (`0x21`) validator field-names recovered from `libwalrus` `.rodata` (`d3_ac_element_count`, `d3_ac_channel_check`, `d3_ac_bias_check`, `d3_ac_imm_check`, `d3_ac_scale_check`, `d3_ac_reserved_zero`) and the pybind modules:

| Validator | Joins to |
|---|---|
| `d3_ac_element_count` / `d3_ac_channel_check` | `num_active_channels` (`+0x22`) |
| `d3_ac_bias_check` | immediate-pointer SB/PSUM legality (`imm0_src` `+0x0F` / `imm0` `+0x28`) |
| `d3_ac_imm_check` | imm operand kind + `imm_dtype` (`+0x0F` / `+0x20` high nibble) |
| `d3_ac_scale_check` | scale immediate (`imm1` `+0x2C`, when func gate fires) |
| `d3_ac_reserved_zero` | unwritten pad bytes (zero-fill) |

`d1_rd` (`0x24`): `dst_element_count` (`+0x1C`), `read_accumulator_type_check` (`+0x20`), `read_accumulator_has_zero_negated` / `d1_rd_reserved_zero` (`+0x21`).

BIR `bir::InstActivation` member offsets (the source dwords the encoders read):

| BIR offset | Member | Read by |
|---|---|---|
| `+0x120` | `activation_func` (`ActivationFunctionType`) | `0x21` (`inst+72`), `0x25` |
| `+0x124` | `accumulator_cmd` (`EngineAccumulationType`) | `0x21` (`inst+73`), `0x25` |
| `+0x148` | act2 variant tag | `0x25` dispatch |
| `+0x150` / `+0x154` | `op0` / `op1` (`AluOp`) | `0x25` |
| `+0x158` / `+0x180` | `reverse0` / `reverse1` | `0x25` `+0x3C` |
| `+0x1A8` | `reduce_op` (`AluOp`) | `0x25` |
| `+0xF0` | `act_func_set_id` (`InstLoadActFuncSet`) | `0x23` (`inst+60`) |

---

## How to Byte-Encode an Activation

A worked `0x21` encode, from BIR fields to the 64-byte bundle:

```text
bundle = {64 × 0x00}
bundle[0x00] = 0x21                              # setupHeader
bundle[0x23] = ACTFUNC_V2[ inst.func@+0x120 ]    # e.g. gelu enum 12 → 21
bundle[0x0E] = (acc==0) ? 3 : ACCUM_V2[ inst.acc@+0x124 ]
bundle[0x20] = DTYPE_V2[in.dtype] & 0x0F  |  (DTYPE_V2[imm.dtype] << 4)
bundle[0x21] = DTYPE_V2[out.dtype]
bundle[0x22] = in.firstAPPair.num
# immediate pointer (args[1]):
#   SB/PSUM  → bundle[0x0F]=0; bundle[0x28]=slot_dword
#   register → bundle[0x0F]=2; bundle[0x28]=regid
#   inline   → bundle[0x0F]=1; *(float*)&bundle[0x28]=value
bundle[0x0D] = pack_s3d3_ap(args[2])             # imm0 AP byte
if func==4: bundle[0x0C]=pack_s3d3_ap(args[3]); *(u32)&bundle[0x2C]=imm1
else:       bundle[0x0C]=1;                     *(u32)&bundle[0x2C]=0
# in/out TENSOR3D mem-patterns filled by assignAccess; pad bytes stay 0
fwrite(bundle, 1, 0x40, engine_bin)
```

For `0x25`, substitute the V4 tables, place `MEM_PATTERN2D` descriptors, write `op0`/`op1`/`reduce_op` at `0x1D`/`0x1E`/`0x1F`, the per-arg `imm_dtype` (arg3 wins) at `0x1C`, and the saturate/reverse byte at `0x3C`; stamp the opcode word `0x1025`.

---

## Related Components

| Name | Relationship |
|---|---|
| `CoreV2GenImpl::visitInstActivation` @ `0x12596f0` | `0x21` + `0x24` dual encoder |
| `CoreV4GenImpl::visitInstActivation` @ `0x143bbb0` | `0x25` encoder; defers to V2 |
| `CoreV2GenImpl::visitInstLoadActFuncSet` @ `0x1250b30` | `0x23` set-index encoder |
| `birverifier::checkActivate2QuadrantRestrictions` @ `0x100e3b0` | errata-3071 quadrant validator |
| `sub_120E650` / `sub_14347C0` | DTYPE remap (V2 / V4) |
| `sub_1203820` / `sub_142E2C0` | ActivationFunc remap (V2 / V4) |
| `sub_12038D0` / `sub_142DF40` | EngineAccumulationType remap (V2 / V4) |
| `sub_142E030` | AluOp remap (gen-4 `op0`/`op1`/`reduce_op`) |

## Cross-References

- [Activation Engine — Datapath and the LUT-Load Mechanism](../arch/activation-engine.md) — the engine, single-set LUT residency, and the `act_tbl_sel` → `inst+0xF0`/`+0x23` framing this page encodes.
- [The 64-Byte Instruction Bundle & Header Skeleton](instruction-bundle.md) — the Family-A bundle and `setupHeader` slot +72 that stamps `+0x00`.
- [MEM_PATTERN2D / 3D — the DST/PSUM Role](mempattern-2d-3d.md) — the `MEM_PATTERN2D` source/destination descriptors the `0x25` form uses.
- [TENSOR1D / 2D / 3D Descriptors](tensor-descriptors.md) — the `TENSOR3D` / `TENSOR1D` access patterns assigned by the `0x21` / `0x24` encoders.
- [Part 10 — the PWP model](../activation/) *(planned)* — the polynomial LUT-set images that `act_tbl_sel` selects.
- [Part 7 — BIR codegen](../bir/) *(planned)* — the `bir::InstActivation` (I04) node whose members feed these encoders.
- [Build & Version Provenance](../reference/versions.md) — the pinned `2.24.5133.0+58f8de22` build.
