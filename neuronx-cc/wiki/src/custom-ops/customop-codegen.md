# Penguin/BIR Custom-Op Codegen & `visitInstCustomOp`

> *All symbols, addresses, and Python line numbers on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310). The three Penguin codegen methods live in the UNSTRIPPED Cython extensions `neuronxcc/starfish/penguin/targets/codegen/BirCodeGenLoop.cpython-310-x86_64-linux-gnu.so` (class `BirCodeGenLoop`, the IMPL) and `neuronxcc/starfish/penguin/targets/generated/BirCodeGenLoopGen.cpython-310-x86_64-linux-gnu.so` (class `BirCodeGenLoopGen`, the GEN base). The single backend emitter `CoreV2GenImpl::visitInstCustomOp` lives in `libwalrus.so`. Addresses are pinned against the IDA decompile/disasm sidecars and `nm`. Evidence anchors: **D-Q09** (the three Penguin codegen methods, full IDA ctree decompile + per-method disasm) and **D-J14** (the `CoreV2GenImpl` emitter census). Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

A custom op is the compiler's escape hatch for an arbitrary user kernel running on the 8-core Xtensa GPSIMD CPU cluster. Three distinct phases turn a NKI/Penguin custom-op carrier into wire bytes, and this page documents the **middle phase** — the IR-to-IR lowering that sits between [6.5.8 `builtin_custom_op`](../nki/neuroncodegen-builtin-customop.md) (which **builds** the `penguin.ir.CustomOp` carrier) and [11.5 the wire layout](customop-wire-layout.md) (which **byte-encodes** the resulting BIR node). The middle phase is `BirCodeGenLoop`'s Penguin-level codegen: it allocates a `bir::InstCustomOp` via the `birpy` `Opcode.CustomOp` factory, copies seven scalar/shape fields off the Penguin carrier onto it, and binds each operand as an access pattern.

There are **three** codegen methods, not two — and the naming is a trap. `BirCodeGenLoopGen.codegenSundaCustomOp` (`_69`) is the GEN-base **allocator**: it is the single point where the BIR `InstCustomOp` is born (`bb.addInstruction(Opcode.CustomOp(build_debuginfo("I-%s" % inst.id)))`). The IMPL class overrides custom-op codegen with **two sibling entry points**, both of which delegate *up* to that allocator via `super()`: `codegenCustomOp` (`_215`) is the **generic / pre-Cayman** path (binds operands with the untyped `self.addSeqAccess`), and `codegenSundaCustomOp` (`_259`) is the **Sunda/Cayman** override (binds operands with the typed, directional `self.addAP(…, isOutput=…)` and opens with an arch-gate assertion). The dispatch chooses one of the two siblings by target uarch; it is **not** `codegenCustomOp` calling `codegenSundaCustomOp`.

The bar for this page: a reader understands (a) which of the three methods does what, (b) the exact field-copy order and operand-binding helper each path uses, (c) the arch gate that distinguishes the Sunda path, and (d) how the resulting BIR `InstCustomOp` reaches the single `CoreV2GenImpl::visitInstCustomOp` emitter that produces the `0x85`/`0x86` chain. Every method body, setter name, and helper register is pinned against the IDA decompile of these binaries.

| | |
|---|---|
| **GEN allocator** | `BirCodeGenLoopGen.codegenSundaCustomOp` (`_69`) @ `0x46d00`, `py309–312` |
| **IMPL generic** | `BirCodeGenLoop.codegenCustomOp` (`_215`) @ `0xc0840`, `py2601–2615` (7511 B) |
| **IMPL Sunda override** | `BirCodeGenLoop.codegenSundaCustomOp` (`_259`) @ `0x652d0`, `py3236` (12192 B) |
| **BIR node built** | `bir::InstCustomOp` via `birpy` module-global `Opcode.CustomOp(debuginfo)` |
| **Debuginfo format** | `"I-%s" % inst.id` (`__pyx_kp_u_I_s`, `.rodata`) |
| **Operand bind (generic)** | `self.addSeqAccess(inst_bir, operand)` — untyped sequence access |
| **Operand bind (Sunda)** | `self.addAP(inst_bir, operand, isOutput=<bool>)` — typed, directional |
| **Arch gate (Sunda)** | `neuron_external_assert("self.target < Cayman", hardware_target=self.target.arch)` |
| **Backend emitter** | `CoreV2GenImpl::visitInstCustomOp` @ `0x12613c0` → `0x85` header + `0x86` payloads |
| **Evidence** | D-Q09 (Penguin codegen) · D-J14 (CoreV2 emitter) |

> **NOTE — three "GPSIMD" name collisions, none of them this.** The custom op on this page dispatches a kernel onto the programmable **Xtensa CPU cluster** ([11.1](gpsimd-xtensa-layout.md)) — a different subsystem from the `InstGPSIMDSB2SB` (`0xBF`) Pool-alias SBUF↔SBUF mover ([1.13](../arch/gpsimd-engine.md)). "Sunda" here is the gen3 DVE uarch family name, and "Cayman" is the arch-level threshold the Sunda path asserts against — neither is a GPSIMD engine. SORT / TOPK and arbitrary user NKI kernels are the canonical occupants of this custom-op path ([11.2](bitonic-sort-topk.md)).

---

## 1. The three methods — allocator + two sibling IMPL entries

The MRO is `BirCodeGenLoop` (IMPL) → `BirCodeGenLoopGen` (GEN). `nm` confirms the GEN base exports **exactly one** `codegen*CustomOp` symbol — `codegenSundaCustomOp` (`_69`) — and **no** `codegenCustomOp`. The IMPL adds two: `codegenCustomOp` (`_215`) and an override `codegenSundaCustomOp` (`_259`). Both IMPL methods call `super().codegenSundaCustomOp` to reach the GEN allocator.

```text
   penguin.ir.CustomOp           ← built upstream by NeuronCodegen.builtin_custom_op (6.5.8)
        │  (.function_name, .lib_file_name, .ulib_to_{ucode,isa}_version,
        │   .is_builtin, .srcs_shapes, .dsts_shapes, .srcs, .dsts)
        ▼  dispatch by target uarch
   ┌─────────────────────────────┬──────────────────────────────────────────┐
   │ generic / pre-Cayman        │ Sunda / Cayman                            │
   │ codegenCustomOp (_215)      │ codegenSundaCustomOp (_259, IMPL override)│
   │   super()._69 alloc         │   neuron_external_assert(< Cayman, arch)  │
   │   7-field copy FIRST         │   super()._69 alloc                       │
   │   addSeqAccess per src/dst   │   addAP(…,isOutput) per src/dst FIRST      │
   │     (untyped)                │   7-field copy                            │
   └─────────────┬───────────────┴───────────────┬──────────────────────────┘
                 ▼                                ▼
        BirCodeGenLoopGen.codegenSundaCustomOp (_69) @0x46d00  ← THE ALLOCATOR
        bb.addInstruction(Opcode.CustomOp(build_debuginfo("I-%s" % inst.id)))
                 ▼
        bir::InstCustomOp  (the birpy Opcode.CustomOp node added to the BasicBlock)
                 ▼
        CoreV2GenImpl::visitInstCustomOp @0x12613c0  → 0x85 header + (1+N)×0x86 payload (11.5)
```

> **GOTCHA — the two IMPL methods are SIBLINGS, not a chain.** An earlier reading had `codegenCustomOp` (`_215`) call `codegenSundaCustomOp` (`_259`). The disasm shows `_215`'s `super()` getattr resolves the name **`codegenSundaCustomOp`** (`__pyx_n_s_codegenSundaCustomOp` @ `0xc0ab6`), and since the GEN base only defines `_69` under that name, the `super()` call lands on `_69` — the allocator — **not** on the IMPL `_259`. The dispatch (a target-method table, [5.x dispatch](../penguin/target-abstraction.md)) picks `_215` *or* `_259`; the chosen one then calls the *shared* GEN allocator. CONFIRMED (D-Q09 — disasm of both `super()` call sites).

---

## 2. The GEN allocator — `codegenSundaCustomOp` (`_69`)

This is the only place a BIR `InstCustomOp` is created. Both IMPL paths reach it via `super()`. The body is short and mechanical: format a debug label from `inst.id`, build a debuginfo object, then construct the `birpy` `Opcode.CustomOp` instruction and add it to the basic block.

```c
/* BirCodeGenLoopGen.codegenSundaCustomOp (_69) @0x46d00 — py309..312 (annotated) */
PyObject *codegenSundaCustomOp(self, inst, bb) {
    /* py310: id_str = str(inst.id) unless already a unicode object */
    Attr   = getattr(inst, "id");                       /* __pyx_n_s_id @L530           */
    id_str = PyObject_IsInstance(Attr, str) ? Attr : str(Attr);

    /* py311: fmt = "I-%s" % id_str  — the BIR-inst debug label */
    fmt    = PyUnicode_Format("I-%s", id_str);          /* __pyx_kp_u_I_s @L554 (.rodata)*/
    bd     = lookup("build_debuginfo");                 /* builtin/module global @L579/583 */
    dbg    = build_debuginfo(fmt, inst);                /* passes the penguin inst        */

    /* py312: inst_bir = bb.addInstruction(Opcode.CustomOp(dbg)) */
    add    = getattr(bb, "addInstruction");             /* __pyx_n_s_addInstruction @L709 */
    Op     = lookup("Opcode");                          /* module global @L835/839        */
    ctor   = getattr(Op, "CustomOp");                   /* __pyx_n_s_CustomOp @L727       */
    cust   = ctor(dbg);                                 /* birpy Opcode.CustomOp(dbg)     */
    return add(cust);                                   /* → the new bir::InstCustomOp    */
}
```

- `Opcode` is the module-global `neuronxcc.starfish.birpy.Opcodes` (the GEN `.rodata` carries both `"neuronxcc.starfish.birpy.Opcodes"` and the `.generated.…` spelling). `Opcode.CustomOp(dbg)` is the `birpy` factory for the BIR custom-op instruction.
- The debug label format is **`"I-%s"`** (verbatim `.rodata` `__pyx_kp_u_I_s`), **not** `"I%s"`. This is the per-instruction identity string the BIR carries; the actual ABI dispatch handle (`CustomOpFunctionId`) is allocated later, in the backend ([11.5 §6](customop-wire-layout.md)), not here.

> **NOTE — why a "Sunda"-named method is the generic allocator.** The GEN base method is *named* `codegenSundaCustomOp` because the code generator templates were emitted from the Sunda-arch description, but the body has no arch logic at all — it is a pure factory. The IMPL `_259` *overrides* it (same name) to add the arch gate and typed binding; the IMPL `_215` *delegates* to it under the inherited name via `super()`. CONFIRMED (D-Q09 — the GEN `_69` body has no `neuron_external_assert`, no `addAP`, no `target` read).

---

## 3. The generic path — `codegenCustomOp` (`_215`)

The pre-Cayman / generic lowering. Source `py2601–2615`. It (1) allocates via the GEN base, (2) copies the seven scalar/shape fields from the Penguin carrier onto the BIR inst, then (3) binds every operand with the **untyped** `self.addSeqAccess`. Note the order: **fields first, operands second.**

```c
/* BirCodeGenLoop.codegenCustomOp (_215) @0xc0840 — py2601..2615 (annotated) */
PyObject *codegenCustomOp(self, inst, bb) {
    /* py2601: inst_bir = super().codegenSundaCustomOp(inst, bb)  → GEN _69 alloc */
    proxy   = builtin_super(BirCodeGenLoop, self);      /* @0xc0a55                     */
    alloc   = getattr(proxy, "codegenSundaCustomOp");   /* __pyx_n_s_… @0xc0ab6 → _69   */
    inst_bir = alloc(inst, bb);                         /* the fresh bir::InstCustomOp  */

    /* py2604..2610: 1:1 field copy penguin.ir.CustomOp → BIR CustomOp  (order pinned) */
    inst_bir.setOpFunctionName        (inst.function_name);          /* L752  */
    inst_bir.setOpLibFile             (inst.lib_file_name);          /* L801  */
    inst_bir.set_ulib_to_ucode_version(inst.ulib_to_ucode_version);  /* L855  ← ucode   */
    inst_bir.set_ulib_to_isa_version  (inst.ulib_to_isa_version);    /* L895  ← then isa*/
    inst_bir.set_is_builtin           (inst.is_builtin);             /* L945  */
    inst_bir.setSrcsShape             (inst.srcs_shapes);            /* L985  */
    inst_bir.setDstsShape             (inst.dsts_shapes);            /* L1035 */

    /* py2612: for src in inst.srcs:  self.addSeqAccess(inst_bir, src)  */
    for (src in iter(inst.srcs))                         /* __pyx_n_s_srcs               */
        self.addSeqAccess(inst_bir, src);               /* __pyx_n_s_addSeqAccess @L1140*/

    /* py2614: for dst in inst.dsts:  self.addSeqAccess(inst_bir, dst)  */
    for (dst in iter(inst.dsts))                         /* __pyx_n_s_dsts               */
        self.addSeqAccess(inst_bir, dst);               /* __pyx_n_s_addSeqAccess @L1351*/

    return None;
}
```

Key facts, all CONFIRMED against the decompile:

- The setter **target** is always `inst_bir` (the GEN-alloc return); the **value** is always the matching attribute read off `inst` (the Penguin carrier). Each is `getattr(inst_bir, "setX")(getattr(inst, "x"))` — a clean 1:1 copy.
- **Field order**: `setOpFunctionName → setOpLibFile → set_ulib_to_ucode_version → set_ulib_to_isa_version → set_is_builtin → setSrcsShape → setDstsShape`. The ulib setters are **ucode-before-isa** (`py2606` @ L855 precedes `py2607` @ L895).
- The operand binder is `self.addSeqAccess`, **not** a method on `bb` or on `inst_bir`. The getattr base is `v130 = *__pyx_args` = `self` (CONFIRMED — `v130` is assigned `*__pyx_args` at the prologue). The call is `self.addSeqAccess(inst_bir, operand)` with empty kwargs.
- The src/dst iterators take a fast path for `PyTuple_Type` / `PyList_Type`, falling back to `PyObject_GetIter` — so `srcs`/`dsts` may be tuple, list, or any iterable.

> **GOTCHA — `addSeqAccess` is the *untyped* binder.** The generic path binds every operand the same way regardless of direction — there is no in/out tag. The directional split (which operand is an input vs the output) is reconstructed downstream from the operand position when the backend packs the access patterns. Only the Sunda path (§4) carries the in/out tag explicitly. CONFIRMED (D-Q09).

---

## 4. The Sunda/Cayman path — `codegenSundaCustomOp` (`_259`)

The IMPL override for the gen3 Sunda/Cayman uarch. Source `py3236`. It differs from the generic path in three ways: (1) it opens with an **arch-gate assertion**, (2) it binds operands with the **typed, directional** `self.addAP(…, isOutput=…)`, and (3) it binds operands **before** the field copy (the reverse of `_215`'s order). It calls the same GEN allocator via `super()`.

```c
/* BirCodeGenLoop.codegenSundaCustomOp (_259) @0x652d0 — py3236 (annotated) */
PyObject *codegenSundaCustomOp(self, inst, bb) {
    /* ---- ARCH GATE: require the target uarch is at least Cayman/Sunda ---- */
    t   = self.target;                                  /* __pyx_n_s_target @0x654f2     */
    cmp = RichCompare(t, Cayman, <);                    /* t < Cayman (cached cmp 1271)  */
    args = ("neuronxcc", "EBCG", 1, cmp,                /* n_u_neuronxcc / n_u_EBCG      */
            "self.target < Cayman",                     /* __pyx_kp_u_self_target_Cayman */
            self.dl);                                   /* __pyx_n_s_dl (device layer)   */
    kw  = {"hardware_target": self.target.arch};        /* n_s_hardware_target @0x6567d  */
    neuron_external_assert(*args, **kw);                /* @0x656d4 — the arch precondition */

    /* ---- ALLOCATE via the GEN base ---- */
    proxy    = builtin_super(BirCodeGenLoop, self);     /* @0x6573e                      */
    inst_bir = proxy.codegenSundaCustomOp(inst, bb);    /* → GEN _69 alloc @0x657a3      */

    /* ---- OPERAND BINDING: typed access-pattern, directional ---- */
    for (src in iter(inst.srcs))                         /* __pyx_n_s_srcs_2             */
        self.addAP(inst_bir, src, isOutput=False);      /* addAP @0x65930; isOutput=False*/
    for (dst in iter(inst.dsts))                         /* __pyx_n_s_dst loop           */
        self.addAP(inst_bir, dst, isOutput=True);       /* addAP @0x65a98; True @0x65b3c */

    /* ---- FIELD COPY: the same 7 fields, same order as _215 ---- */
    inst_bir.setOpFunctionName        (inst.function_name);          /* @0x65bb7 */
    inst_bir.setOpLibFile             (inst.lib_file_name);          /* @0x65c56 */
    inst_bir.set_ulib_to_ucode_version(inst.ulib_to_ucode_version);  /* @0x65cf9 */
    inst_bir.set_ulib_to_isa_version  (inst.ulib_to_isa_version);    /* @0x65d93 */
    inst_bir.set_is_builtin           (inst.is_builtin);             /* @0x65e36 */
    inst_bir.setSrcsShape             (inst.srcs_shapes);            /* @0x65ed0 */
    inst_bir.setDstsShape             (inst.dsts_shapes);            /* @0x65f73 */
    return inst_bir;
}
```

The arch gate, CONFIRMED token-by-token against the decompile/disasm:

- `neuron_external_assert` is a module global (`__pyx_n_s_neuron_external_assert`, cached `_pyx_dict_cached_value_1273`).
- The positional argument tuple is built (`PyTuple_New(6)`) with `[0]=u"neuronxcc"`, `[1]=u"EBCG"`, `[2]=int 1`, `[3]=<the t<Cayman compare result>`, `[4]=kp_u"self.target < Cayman"` (the assert message), `[5]=self.dl`.
- The keyword dict is `{"hardware_target": self.target.arch}` — built via `PyDict_New()` + `PyDict_SetItem` with key `__pyx_n_s_hardware_target` (@ `0x6567d`) and value `self.target.arch` (`__pyx_n_s_arch` @ L705).

The operand binding, CONFIRMED:

- The binder is `self.addAP` — getattr base is `v6 = *__pyx_args` = `self` (CONFIRMED in both loops; not `bb`, not `inst_bir`).
- Each call passes a positional `(inst_bir, operand)` plus a keyword `isOutput`. The **src** loop passes `isOutput=False`; the **dst** loop passes `isOutput=True` — the dst branch's `isOutput` value is the literal `_Py_TrueStruct_ptr` (@ `0x65b3c`), pinning the directional tag.

> **GOTCHA — `addAP`'s `isOutput` is what produces the wire's in/out arg tags.** The typed access-pattern binder tags each operand `[in]` (src, `isOutput=False`) or `[out]` (dst, `isOutput=True`). Downstream, the backend's per-payload encoder ([11.5 §3](customop-wire-layout.md)) emits the **output AP first** (payload 0, the single `isOutput=True` operand) then the `N` input APs — the directional split established here is exactly what the `0x86` payload ordering and the Xtensa SORT/TOPK leaf's `NEURON_ISA_TPB_CUSTOM_OP_ARG_TYPE_TENSOR` `[in]`/`[out]` arg tags consume. CONFIRMED (D-Q09 binding) / STRONG (the binding→payload edge, via the J14 emitter).

> **QUIRK — Cython dedup suffixes are not distinct fields.** The decompile shows `__pyx_n_s_srcs_2`, `srcs_shapes_2`, `dsts_shapes_2`, and a `dst` loop var. These are string-interning **dedup suffixes** for the same Python attributes (`.srcs`, `.srcs_shapes`, `.dsts_shapes`, `.dst`/`.dsts`) — `.rodata` carries a single canonical spelling of each. They are not separate fields. CONFIRMED (D-Q09).

> **NOTE — the `EBCG` token.** Positional `[1]` of the assert is the `.rodata` unicode `EBCG` — a codegen/build-config category label for the assertion (most plausibly an "External Backend Code Gen" / pass-id tag). The token is CONFIRMED in `.rodata`; its exact expansion is not byte-verifiable. SPECULATIVE (meaning) / CONFIRMED (token).

---

## 5. The seven fields — penguin.ir.CustomOp → BIR `InstCustomOp`

Both IMPL paths copy the same seven attributes. This is the inverse of [6.5.8 `builtin_custom_op`](../nki/neuroncodegen-builtin-customop.md), which *builds* those attributes onto the `penguin.ir.CustomOp` carrier; the codegen here *lowers* them onto the BIR node. The table maps each Penguin attribute to its BIR setter and the downstream wire field it feeds.

| Penguin `CustomOp` attr | BIR setter | wire ([11.5](customop-wire-layout.md)) | Conf |
|---|---|---|---|
| `.function_name` (op-name / ABI entry) | `setOpFunctionName` | header identity → `CustomOpFunctionId` (via registry) | CONFIRMED |
| `.lib_file_name` (embedded `.so`) | `setOpLibFile` | registry lib id (off-wire, NEFF metadata) | CONFIRMED |
| `.ulib_to_ucode_version` | `set_ulib_to_ucode_version` | ABI version handle | CONFIRMED |
| `.ulib_to_isa_version` | `set_ulib_to_isa_version` | ABI version handle | CONFIRMED |
| `.is_builtin` | `set_is_builtin` | builtin flag | CONFIRMED |
| `.srcs_shapes` | `setSrcsShape` | input shape metadata (pairs with each input AP) | CONFIRMED |
| `.dsts_shapes` | `setDstsShape` | output shape metadata (pairs with the output AP) | CONFIRMED |
| `.srcs` (operand list) | `addSeqAccess` (`_215`) / `addAP(…,isOutput=False)` (`_259`) | one `0x86` payload per input AP | CONFIRMED |
| `.dsts` (operand list) | `addSeqAccess` (`_215`) / `addAP(…,isOutput=True)` (`_259`) | the `0x86` output-AP payload | CONFIRMED |

> **GOTCHA — the op-name is copied, never resolved here.** `setOpFunctionName` stamps the `function_name` string onto the BIR inst verbatim; the binding of that name (and the `lib_file_name`) to the embedded Xtensa `.so` leaf is a **bir/runtime** concern (`ModuleArtifactInfo` / `allocateCustomOpFunctionId`, [11.5 §6](customop-wire-layout.md)). There is **no** name→`.so` resolution logic in `_215` or `_259` — the codegen is a pure field copy + operand bind. STRONG (D-Q09: no resolution code in either body; the `function_name`=op-name fact is from the upstream 6.5.8 emitter).

---

## 6. The single backend emitter — `CoreV2GenImpl::visitInstCustomOp`

Once the BIR `InstCustomOp` exists (built by `_69`, decorated by `_215`/`_259`), the backend lowers it to the 64-byte ISA wire. There is **exactly one** emitter — `CoreV2GenImpl::visitInstCustomOp` @ `0x12613c0` — and no `CoreV3`/`CoreV4` override; gen3/gen4 backends inherit it verbatim. It is the largest of the CoreV2 long-tail encoders (1207 decompiled lines) and emits the `0x85` header + per-operand `0x86` payload chain that [11.5](customop-wire-layout.md) byte-pins in full.

This page documents only how the codegen's output *enters* the emitter; the byte map is 11.5's subject. The points where this page and 11.5 must agree:

- The emitter reads `num_arguments = N` (the `InstCustomOp` argument list length) and writes the header **`num_payloads` as a `u16` at offset `+0x06`** (NOT `+0x0C`) via the bounded setter `sub_123CA60` (`"instr.num_payloads"`), with the value `N + 2` for `N ≥ 1` (header + output AP + `N` input APs) or `1` for `N == 0`. This page's operand-binding description (one binder call per src and per dst) is the upstream source of that `N` and the one-output invariant.
- The single output (the lone `dst` / `isOutput=True` operand) becomes **payload 0**; the `N` inputs (`src` / `isOutput=False`) become payloads `1..N`. The `_259` directional binding (§4) is what makes the output-first ordering meaningful.
- The legality the emitter enforces (`"Custom ops cannot have more than 1 output"`, SBUF/HBM operand location, no `TensorIndirect` AP, ≤255 unique functions) is downstream of the codegen — the codegen does not pre-check these; the backend `visitInstCustomOp` does.

```c
/* CoreV2GenImpl::visitInstCustomOp @0x12613c0 — how the BIR node enters the wire (sketch) */
void visitInstCustomOp(InstCustomOp &inst) {
    int N = arg_list_length(inst);               /* = num_arguments, set by the codegen binds */
    /* one 0x85 header: num_payloads(+0x06)=N?N+2:1, FunctionId(+0x0E), num_arguments(+0x0F)=N */
    /* one 0x86 payload per operand: OUTPUT AP first (the isOutput=True dst), then N input APs  */
    /* fwrite 0x40 per word; full byte map in 11.5                                              */
}
```

> **NOTE — BIR/NKI kernels are lowered to CustomOp before the backend.** There is no `CoreV2GenImpl::visitInstBIRKernel` / `visitInstNKIKernel` — those visitors exist only in the verifier/simulator. Whole BIR/NKI kernels (IT54/IT55) are **lowered to `InstCustomOp`** before backend codegen and emitted through this same `0x85`/`0x86` path. So this single emitter handles both explicit custom ops and lowered-away kernels. CONFIRMED (D-J14 — no Kernel emitter in any `CoreV*GenImpl`).

---

## 7. End-to-end chain

```text
  NeuronCodegen.builtin_custom_op (6.5.8, NKI ENTRY)
    │  op-name=function_name; srcs/dsts; lib_file; ulib↔{isa,ucode}; is_builtin; shapes
    ▼  builds  penguin.ir.CustomOp
  dispatch by target uarch (5.x target-method table)
    ├─ generic / pre-Cayman  → codegenCustomOp (_215) @0xc0840
    │     super()._69 alloc → Opcode.CustomOp(build_debuginfo("I-%s" % inst.id))
    │     → 7-field copy (ucode before isa) → self.addSeqAccess per src & per dst
    └─ Cayman / Sunda        → codegenSundaCustomOp (_259) @0x652d0  (IMPL override)
          neuron_external_assert("self.target < Cayman", hardware_target=self.target.arch)
          super()._69 alloc (same Opcode.CustomOp)
          → self.addAP(inst_bir, src, isOutput=False) / addAP(inst_bir, dst, isOutput=True)
          → same 7-field copy
    ▼  bir::InstCustomOp  (added to the BasicBlock by the GEN allocator _69)
  CoreV2GenImpl::visitInstCustomOp @0x12613c0  (the single backend emitter)
    ▼  0x85 CUSTOM_OP_HEADER (num_payloads@+0x06, FunctionId@+0x0E, num_arguments@+0x0F)
       + (1+N)×0x86 CUSTOM_OP_PAYLOAD (output AP first, then N input APs)   ← 11.5 byte map
  Xtensa CPU leaf: SORT / TOPK / user NKI kernel, ARG_TYPE_TENSOR [in]/[out] arg tags
  FunctionId → .so binding: bir/runtime ModuleArtifactInfo (off-wire NEFF metadata)
```

---

## 8. Adversarial self-verification

Five strongest claims, each re-checked against the binary:

1. **`_215` delegates to the GEN allocator `_69` via `super().codegenSundaCustomOp`, not to the IMPL `_259`.** Verified: the `_215` decompile at L611 reads `__pyx_n_s_codegenSundaCustomOp` off the `builtin_super` proxy; `nm` shows the GEN base exports only `_69` under that name and no `codegenCustomOp`, so the resolved target is `_69`. CONFIRMED.
2. **`_215` binds operands with `self.addSeqAccess`; `_259` with `self.addAP(…, isOutput=…)`.** Verified: `_215` decompile has `__pyx_n_s_addSeqAccess` at L1140 (srcs) and L1351 (dsts) and **no** `addAP`; `_259` has `__pyx_n_s_addAP` at L904/L1044 and **no** `addSeqAccess`. The getattr base is `*__pyx_args` (self) in both (`v130` in `_215`, `v6` in `_259`). CONFIRMED.
3. **The ulib version setters are ucode-before-isa.** Verified: in `_215`, `set_ulib_to_ucode_version` is at L855 and `set_ulib_to_isa_version` at L895 (ucode first); `_259` mirrors this (@ `0x65cf9` then `0x65d93`). CONFIRMED — corrects an earlier isa-first listing.
4. **`_259`'s arch gate is `neuron_external_assert` with message `"self.target < Cayman"` and kwarg `hardware_target=self.target.arch`.** Verified: `__pyx_n_s_neuron_external_assert` (L584/588), `__pyx_kp_u_self_target_Cayman` (L675), `__pyx_n_u_EBCG`/`__pyx_n_u_neuronxcc` in the arg tuple (L665–670), and `__pyx_n_s_hardware_target` @ `0x6567d` paired with `target.arch` via `PyDict_New`/`PyDict_SetItem`. CONFIRMED.
5. **The GEN allocator uses the debug format `"I-%s" % inst.id` and `Opcode.CustomOp`.** Verified: `_69` decompile reads `__pyx_n_s_id` (L530), `__pyx_kp_u_I_s` (L554, the `"I-%s"` literal), `build_debuginfo` (L579/583), `addInstruction` on `bb` (L709), and `Opcode` → `CustomOp` (L835/839 + L727). CONFIRMED — `"I-%s"`, not `"I%s"`.

Two findings worth flagging:

- **F1 — the "Sunda allocator" naming trap.** The GEN base method named `codegenSundaCustomOp` (`_69`) is the **generic** allocator with no arch logic; the IMPL `_215` (generic path) reaches it via the *inherited* name, while the IMPL `_259` (Sunda path) *overrides* it. So "Sunda" in the GEN symbol name is a template-origin artifact, not a runtime arch gate — the real arch gate is the `neuron_external_assert` only `_259` carries.
- **F2 — directionality is established in codegen, consumed at the wire.** The output-first `0x86` payload ordering and the Xtensa leaf's `[in]`/`[out]` arg tags both trace back to `_259`'s `addAP(…, isOutput=True/False)` split. The generic `_215` path binds untyped (`addSeqAccess`), so on pre-Cayman targets the in/out split is reconstructed from operand position at the backend, not carried explicitly through the IR.

## 9. Confidence ledger

- **CONFIRMED** (nm + full IDA ctree decompile + per-method disasm of `BirCodeGenLoop`/`BirCodeGenLoopGen`): all three symbols/addresses/py-lines; `_215`'s full body, exact setter order, `super().codegenSundaCustomOp`→`_69` delegation, and `addSeqAccess` per src/dst; `_259`'s arch gate (`neuron_external_assert("self.target < Cayman", hardware_target=arch)`), `super()._69` alloc, `addAP(…, isOutput=False/True)` directional binding, and 7-field copy; the GEN `_69` allocator (`"I-%s" % inst.id` → `bb.addInstruction(Opcode.CustomOp(build_debuginfo(...)))`); the seven-field penguin→BIR map; the single `CoreV2GenImpl::visitInstCustomOp` @ `0x12613c0` (D-J14) and the no-`CoreV3`/`CoreV4`-override fact.
- **STRONG**: the binding→payload edge (`addAP`/`addSeqAccess` → `0x86` payload, output-first) and the field-copy→`0x85`-header edge, both via the J14 emitter / 11.5 byte map; that `function_name` is the op-name/ABI entry (from the upstream 6.5.8 emitter); that the arch-gate predicate is exactly `target < Cayman` (the `.rodata` message is CONFIRMED; whether the assert fires on the predicate or its negation is a message-wording detail).
- **INFERRED / DEFER**: the dispatch that selects `_215` (generic) vs `_259` (Sunda) by target uarch (a target-method table, [5.x target abstraction](../penguin/target-abstraction.md), not byte-traced here); the `src`-branch `isOutput=False` being the cached `False` singleton (the `dst`-branch's explicit `_Py_TrueStruct_ptr` makes `False` for `src` the only consistent read); the exact `build_debuginfo` signature beyond `("I-%s" % id, inst)`.
- **SPECULATIVE**: the `EBCG` token expansion (CONFIRMED as a `.rodata` token; meaning unverifiable).

No fabricated symbols or offsets; every address is cited from the named binary's sidecar.

## Cross-References

- [6.5.8 NeuronCodegen `builtin_custom_op` Emitter](../nki/neuroncodegen-builtin-customop.md) — the NKI front door that **builds** the `penguin.ir.CustomOp` carrier these codegen methods lower; this page is its inverse.
- [11.5 CUSTOM_OP Wire Byte-Layout (0x85 / 0x86)](customop-wire-layout.md) — the byte map of the `CoreV2GenImpl::visitInstCustomOp` output; pins `num_payloads` @ `+0x06`, `num_payloads = N+2`, FunctionId @ `+0x0E`, num_arguments @ `+0x0F`.
- [2.22 Collective / GPSIMD / CustomOp Encoding](../isa/collective-customop-encoding.md) — the sibling ISA-encoding page; §10 first sketched the `0x85`/`0x86` pair.
- [11.1 The GPSIMD CPUs: 8-core Xtensa ELF Layout](gpsimd-xtensa-layout.md) — the Xtensa CPU cluster the lowered custom op dispatches a kernel onto.
- [11.2 The Bitonic SORT / TOPK Builtin Algorithm](bitonic-sort-topk.md) — SORT/TOPK, the canonical custom-op occupants (no dedicated opcode; ride `0x85`/`0x86`).
- [11.6 FindCustomOpData Staging & FunctionId Binding](findcustomopdata-staging.md) — how the header's FunctionId byte maps to `(libfile, function-name)` via `ModuleArtifactInfo`.
- [1.13 GPSIMD Engine — the Pool-Alias Cross-Core SB2SB Mover](../arch/gpsimd-engine.md) — the *other* "GPSIMD" (the `0xBF` Pool-alias mover); resolves the name collision.
