# Custom-Op Full-Chain Reconciliation

> *Version pin: `neuronx_cc 2.24.5133.0+58f8de22` (cp310; cp311/cp312 carry the same backend `.text` at a fixed per-wheel VA delta). This page synthesizes the nine sibling pages of Part 11 into one end-to-end trace and resolves the points where two siblings phrased a shared byte differently. The producer/codegen side lives in `KernelBuilder.cpython-310…so` (UNSTRIPPED), `BirCodeGenLoop*.cpython-310…so`, and `neuronxcc/starfish/lib/libwalrus.so` + `libBIR.so`; the consumer side is the eight Tensilica Xtensa images `neuronxcc/data/custom_op/libbuiltincustomop_cpu{0..7}.stripped.so`. For `.text`/`.rodata` in the x86 backend objects, VMA == file offset; `.data` is `+0x400000`. Treat every address as version-pinned.*

## Abstract

A Neuron custom op is the compiler's escape hatch: arbitrary C++ that runs on the **GPSIMD custom-op CPU** — eight Tensilica Xtensa cores — instead of on the PE systolic array or the vector engine. Getting one op from a Python `nki.isa.builtin_custom_op` call down to a `*_compute` function executing on an Xtensa core crosses **six representations and four binaries**, and the op's *identity* changes form at every boundary: it is a human-readable string (`function_name`) from the NKI front door down to the BIR instruction, is **compressed to a single byte** (`CustomOpFunctionId`) on the ISA wire, and is **re-expanded to a string dispatch** on the CPU leaf. The nine sibling pages each own one stage of that descent; this page is the spine that threads them, naming the real symbol/offset at each stage and the exact bytes that cross each boundary.

The reference frame is a conventional lowering pipeline — front-end builder → IR-to-IR codegen → instruction-selection/encoding → runtime loader → leaf kernel — but with two structural surprises a reimplementer must internalize. First, the chain is a **confluence, not a line**: two independent producer formats carry a custom op (the live **beta3** Penguin `CustomOp` node and the dormant **beta2** klr `ExtendedInst` CBOR record), and they merge at exactly one node — `bir::InstCustomOp` (InstructionType 53) — after which a **single** backend emitter (`CoreV2GenImpl::visitInstCustomOp @0x12613c0`) produces the same wire for every arch and every producer. Second, the op never travels as a single self-describing word: it encodes to a **chain** of 64-byte bundles — one `CUSTOM_OP_HEADER` (`0x85`) carrying counts and the FunctionId, followed by `N+1` `CUSTOM_OP_PAYLOAD` words (`0x86`) carrying the output access pattern first, then `N` argument APs.

This is a synthesis page, so its job is **coherence**: where two siblings disagree on a byte, it adopts the byte-pinned reading and carries the correction in place. Two such reconciliations matter most — the `num_payloads` offset (`+0x0C`, settled by the encoder's bounded-setter store destination `lea [r13+0Ch]`) and the FunctionId width (a `uint32` in the registry namespace, truncated to a `uint8` on the wire) — and both are resolved in [§4](#4-the-reconciliations--where-two-siblings-disagree). A reader who finishes this page can trace any single custom op from NKI source to the Xtensa CPU leaf, naming every stage's symbol and the bytes that cross each boundary.

For reimplementation, the contract is:

- The **six-layer identity carrier table**: how `function_name`/`lib_file_name` thread from NKI → Penguin → BIR → wire-byte → loader → CPU-string-dispatch, and where the form changes.
- The **confluence rule**: beta3 ∥ beta2 → `bir::InstCustomOp` (IT53) → the *one* `CoreV2GenImpl` emitter; no per-arch and no per-producer wire divergence.
- The **wire chain arithmetic**: `num_payloads = N + 2`, the output-first payload ordering, and the byte map (`0x85`/`0x86`, num_payloads@`+0x0C`, FunctionId@`+0x0E`, num_arguments@`+0x0F`).
- The **resolution path** `function_name → FunctionId → .so → *_compute`, and the **builtin-vs-user** discriminator `(lib_file_name, is_builtin)`.

| | |
|---|---|
| **NKI front door** | `NeuronCodegen.builtin_custom_op` @ `0xb8890` (`KernelBuilder.py:3609`) → `penguin.ir.CustomOp` |
| **beta3 codegen** | `BirCodeGenLoop.codegenCustomOp` (`_215`) @ `0xc0840` ∥ `codegenSundaCustomOp` (`_259`) @ `0x652d0` → GEN alloc `_69` @ `0x46d00` |
| **beta2 carrier** | `klr::ExtendedInst` — 80-byte POD, CBOR tag 210, `Operator` kind 70 (`nki_klr_sim`; lowered libwalrus-side) |
| **Convergence node** | `bir::InstCustomOp` — InstructionType **53** (`libBIR`: ctor `0x1789b0`, `getIsBuiltinEvalIfNeeded` `0x176af0`) |
| **Single emitter** | `CoreV2GenImpl::visitInstCustomOp` @ `0x12613c0` (libwalrus) — the **only** `GenImpl` custom-op emitter |
| **Wire** | `0x85` HEADER + `(1+N)`× `0x86` PAYLOAD, 64-byte bundles; `num_payloads = N+2` |
| **Registry** | `bir::ModuleArtifactInfo::{addCustomOpLibFile,addCustomOpFunction,allocateCustomOpFunctionId}` |
| **CPU leaf** | `libbuiltincustomop_cpu{0..7}.stripped.so` — 8 Xtensa ELF32, base `0x84000000 + id·0x200000` |
| **Builtin set** | SORT/TOPK → `sort_{single,multi}core_compute`, `partial_{sort,merge}_multicore_compute` |

---

## 1. The Spine — six representations, one trace

### Purpose

The custom-op chain is a sequence of representations, each owned by a sibling page. This section is the at-a-glance map: one row per representation, naming what carries the op's identity, what tag/opcode (if any) it has, and what form the operands take. Read it top-to-bottom as the descent from Python to Xtensa.

### Algorithm

The spine, as the ASCII control-flow a reimplementer would build to:

```text
(a) NKI  nki.isa.builtin_custom_op                     [6.5.8]
      NeuronCodegen.builtin_custom_op @0xb8890
      identity = function_name str ("sort"/"topk"/user) + lib_file_name str
      operands = [s.tensor for s in srcs], [d.tensor for d in dsts]   (NKI view → IR tensor)
      tag = NONE (a Python emitter; builds an object)
        │  self.insert_raw(op)  @0x88be0
        ▼
(b) penguin.ir.CustomOp  ("Wrapper class for XLA Custom Call", CustomOp.py:40)
      identity = .function_name + .lib_file_name + .is_builtin
                 + .ulib_to_ucode_version + .ulib_to_isa_version + shapes
      operands = .srcs / .dsts tensor lists
      tag = NONE on the wire (a Python IR node — the Penguin analog of klr ExtendedInst)
        │  dispatch by target uarch (5.x target abstraction)
        ├──[beta3 generic, < Cayman]  codegenCustomOp (_215) @0xc0840  ──┐
        ├──[beta3 Sunda,   ≥ Cayman]  codegenSundaCustomOp (_259) @0x652d0 ─┤
        │       both → super()._69 GEN alloc → Opcode.CustomOp(debuginfo "I-%s"%id)
        │                                                                  │
        └──[beta2]  klr::ExtendedInst  [7.32]  ───────────────────────────┤
                 identity = raw opcode:u32 (NO op-name, NO lib_file)       │
                 CBOR tag 0xD9 0xD2 0x00 array(6); Operator kind 70        │
                 operands = data0/data1 = two std::list<u32> (descriptors) │
                 lowered libwalrus-side (nki_klr_sim REFUSES kind 70)      │
        ▼──────────────── all converge ───────────────────────────────────▼
(d) bir::InstCustomOp  (InstructionType 53)            [11.8]   ◄── CONFLUENCE
      identity = function_name + lib_file + is_builtin + srcs/dsts shapes
      operands = per-arg AccessPattern list (inst+0xA0..+0xA8) + output AP
      tag = IT53 (in-memory BIR node; bytes assigned in (e))
        │  CoreV2GenImpl::visitInstCustomOp @0x12613c0  — the ONLY emitter
        ▼
(e) WIRE  CUSTOM_OP_HEADER 0x85 + (1+N)× CUSTOM_OP_PAYLOAD 0x86   [11.5/11.6]
      identity = HEADER[0x0E] = CustomOpFunctionId (u8) — op-name now a 1-byte handle
      HEADER:  num_payloads u16 @+0x0C = N+2 ; FunctionId u8 @+0x0E ; num_arguments u8 @+0x0F
      PAYLOAD: present-flag 1 @+0x0F ; ADDR4 access pattern @+0x10..+0x3F (output first)
        │  runtime: FunctionId → ModuleArtifactInfo → (lib_file, function_name)
        ▼
(f) Xtensa CPU leaf  libbuiltincustomop_cpuN.so       [11.1/11.2/11.3/11.4]
      re-dispatch on function_name STRING → one of 4 *_compute fns
      args arrive as a typed isa_args TENSOR vector (get_types()[i] == _ARG_TYPE_TENSOR)
      tensors stage through DRAM window [0x80000, 0x90000); out = ONE [2,d0,d1] buffer
```

### Function Map

| Stage | Symbol / node | Address | Identity carrier | Confidence |
|---|---|---|---|---|
| (a) NKI emit | `NeuronCodegen.builtin_custom_op` | `0xb8890` | `function_name` + `lib_file_name` (str) | CERTAIN |
| (b) Penguin IR | `penguin.ir.CustomOp` | `CustomOp.py:40` | `.function_name` + `.is_builtin` + shapes | CERTAIN |
| (c1) beta3 generic | `BirCodeGenLoop.codegenCustomOp` (`_215`) | `0xc0840` | field copy `setOpFunctionName(...)` | CERTAIN |
| (c1) beta3 Sunda | `BirCodeGenLoop.codegenSundaCustomOp` (`_259`) | `0x652d0` | same fields + `addAP(isOutput)` | CERTAIN |
| (c1) GEN alloc | `BirCodeGenLoopGen.codegenSundaCustomOp` (`_69`) | `0x46d00` | `Opcode.CustomOp(debuginfo)` | CERTAIN |
| (c2) beta2 carrier | `klr::ExtendedInst` | (POD, `nki_klr_sim`) | raw `opcode:u32` (no name) | CERTAIN |
| (d) BIR node | `bir::InstCustomOp` ctor | `libBIR 0x1789b0` | function_name + is_builtin | CERTAIN |
| (e) emitter | `CoreV2GenImpl::visitInstCustomOp` | `libwalrus 0x12613c0` | FunctionId byte @ `+0x0E` | CERTAIN |
| (f) CPU leaf | `*_compute` family | Xtensa cpuN base + offset | function_name string redispatch | HIGH |

> **NOTE — the identity changes *kind* three times.** It is a **string** from (a) through (d); a **single byte** at (e); a **string again** at (f). The byte at (e) is not the string hashed — it is a dense index allocated by the registry ([§3](#3-the-resolution-path--function_name--functionid--so--compute)) and resolved back to the string by the runtime loader before the CPU sees it. A reimplementer who tries to carry the name on the wire, or to dispatch the CPU leaf on the byte, has mismodeled the seam.

---

## 2. The Confluence — beta3 ∥ beta2 → one node, one emitter

### Purpose

The single most important structural fact of the chain: two independent producer formats merge at `bir::InstCustomOp` and then share one emitter. A reimplementer who models two wire formats, or a per-arch emitter table, has overbuilt. This section states the two paths precisely and proves the merge.

### The two producers

**beta3 — Penguin `CustomOp` (the live production path).** The NKI front door builds a `penguin.ir.CustomOp` node ([6.5.8](../nki/neuroncodegen-builtin-customop.md)); `BirCodeGenLoop` lowers it to a `bir::InstCustomOp` ([11.8](customop-codegen.md)). The op-name is an **explicit string field** present at every level: `codegenCustomOp` copies it 1:1 via `setOpFunctionName(inst.function_name)` (and `setOpLibFile`), and it survives unchanged until the FunctionId compression at the wire. There are **three** codegen methods and the naming is a trap: the GEN-base `codegenSundaCustomOp` (`_69`) is the **allocator** (the single `bb.addInstruction(Opcode.CustomOp(...))` site); the two IMPL methods are **siblings**, not a chain — `codegenCustomOp` (`_215`, generic, binds operands with untyped `addSeqAccess`) and `codegenSundaCustomOp` (`_259`, Sunda/Cayman override, opens with an arch-gate assert and binds with the typed directional `addAP(..., isOutput=...)`). The target-uarch dispatch picks one of the two siblings; the chosen one calls the shared GEN allocator via `super()`.

**beta2 — klr `ExtendedInst` (the dormant serialized-KLR carrier).** The parallel path carries a custom op as a `klr::ExtendedInst` — an 80-byte POD reached through a `make_shared` inplace control block, serialized as a **CBOR** record under tag 210 (`0xD9 0xD2 0x00`, array of 6 fields) and appearing in the klr `Operator` variant as **kind 70** ([7.32](../bir/klr-extendedinst-cbor.md)). Critically, it carries **no op-name and no lib_file** — only a raw `opcode:u32` plus two `std::list<u32>` descriptor lists (`data0`/`data1`). The simulator `nki_klr_sim` deserializes and prints `ExtendedInst` but **flatly refuses to lower it**: its per-`Operator` codegen switch has no case for kind 70 and throws "Unsupported operator". The live lowering of `ExtendedInst` into a `bir::InstCustomOp` is therefore **libwalrus-side**, via the `InstNKIKLIRKernel` (IT56) klr-AST translate path (`TranslateNKIASTToBIR::copyConstFileToArtifact`, `checkVersionMatch`).

### Algorithm

```c
// The confluence, as the dispatch a reimplementer builds.
bir::InstCustomOp *lower_custom_op(producer):
    if producer.is_penguin_node:                  // beta3 (live)
        method = (target_uarch >= Cayman)
                   ? codegenSundaCustomOp_259      // typed addAP(isOutput), arch-gate
                   : codegenCustomOp_215;          // untyped addSeqAccess
        inst = super_codegenSundaCustomOp_69(producer, bb);   // GEN alloc @0x46d00
        method.copy_fields_and_bind_operands(inst, producer); // setOpFunctionName, ...
        return inst;                              // bir::InstCustomOp (IT53)
    else:                                          // beta2 (klr ExtendedInst, dormant)
        // nki_klr_sim has NO case 70 → throws. Live bridge is libwalrus-side:
        inst = translate_nki_klir_kernel(producer);  // InstNKIKLIRKernel (IT56)
        // op-name is NOT on ExtendedInst → must be re-attached at bir/runtime
        return inst;                              // also bir::InstCustomOp (IT53)

    // ── BOTH RETURN THE SAME NODE TYPE ──
    // From here ONE emitter, no per-arch / per-producer branch:
    //   CoreV2GenImpl::visitInstCustomOp @0x12613c0  →  identical 0x85/0x86 wire
```

### Considerations

The asymmetry between the two producers is the deliverable point. beta3 threads the op-name as an explicit BIR field, so the registry just *reads* it; beta2 carries only a numeric opcode, so on that path the op-name must be **re-attached at the bir/runtime `ModuleArtifactInfo` level** — there is nothing to copy from `ExtendedInst`. Both end at the same `CustomOpFunctionId@0x0E`, but beta3 carries `name → id` while beta2 carries `opcode → (bridge supplies name) → id`.

> **QUIRK — there is exactly ONE custom-op emitter, and a "Sunda" *codegen* does not mean a "Sunda" *wire*.** `nm -DC libwalrus.so | rg 'GenImpl::visitInstCustomOp'` returns **one** symbol — `CoreV2GenImpl`; there is no CoreV3/V4/Sunda/Cayman override (they inherit it). The Penguin-side `codegenSundaCustomOp` (`_259`) specialization is purely a *codegen-time* binding choice (typed `addAP` + the `target < Cayman` gate); it still produces a plain `bir::InstCustomOp` that the one CoreV2 emitter lowers. The only per-arch difference *downstream* is the latency model (`Trainium/Gen3/CoreV4Hwm::getLatency(InstCustomOp&)`), never the wire bytes. A reimplementer who builds a per-arch encoder table is wrong; the wire is arch-common **and** producer-common.

> **NOTE — the shipped NKI path is beta3.** `KernelBuilder.so` emits `penguin.ir.CustomOp`; the string "ExtendedInst" is **absent** from it. beta2 is the parallel klr-serialization carrier consumed libwalrus-side; whether any *shipped* op emits a klr `ExtendedInst` is open (it may be legacy/raw-escape). The two are parallel, not sequential.

---

## 3. The Resolution Path — `function_name` → FunctionId → `.so` → `*_compute`

### Purpose

The op-name string is registered, deduplicated, assigned a dense numeric handle, compressed to a wire byte, and then re-expanded by the runtime into a `.so` selection and a string redispatch on the CPU. This section walks that full id lifecycle so a reimplementer can reproduce the registry seam ([11.7](findcustomopdata-staging.md)) and the CPU dispatch ([11.2](bitonic-sort-topk.md)).

### Algorithm

```c
// The full id lifecycle.  Steps 1-3 and 5 are traced; STEP 4 is reconstructed.
FunctionId resolve_and_stamp(module, function_name, lib_file_name):
    // STEP 1 — REGISTER  (encoder prologue ~0x1261e00, libwalrus)
    for line in getline(lib_spec):                     // multi-line lib spec
        ModuleArtifactInfo::addCustomOpLibFile(         // libBIR T @0x39bb00
            libpath, fn_name, third_str, is_builtin, /*uchar*/cpu, /*uchar*/total);
        ModuleArtifactInfo::addCustomOpFunction(        // libBIR T @0x39cee0
            fn_name, lib_name, /*uint32*/id);

    // STEP 2 — ALLOCATE  (dense, deduplicated by fn-name)
    if unique_fn_count > 0xFE:                          // 254 cap → reportError
        fail("Number of unique custom op functions cannot exceed ");
    id = ModuleArtifactInfo::allocateCustomOpFunctionId();   // libBIR T @0x39a690

    // STEP 3 — STAMP into the wire  (truncate uint32 → uint8)
    byte fid = *(uint8*)(CustomOpLibInfo[lib] + 0x28);  // the stored u8 handle
    HEADER[0x0E] = fid;                                 // CustomOpFunctionId
    return fid;

// ── RUNTIME (NEFF link / GPSIMD loader) ──
// STEP 4 — id → .so   (ModuleArtifactInfo round-trips id → (lib_file, fn))
//   lib_file == libbuiltincustomop_cpu*  → load the 8 per-CPU images
//   base(cpu_id) = 0x84000000 + cpu_id·0x200000
// STEP 5 — .so → compute fn   (names read from the images; the dispatch reconstructed)
//   Xtensa op prologue parses isa_args, then dispatches ON function_name STRING:
char *select_compute(function_name, num_subtensors):
    if function_name == "sort":
        return (num_subtensors <= 1) ? "sort_singlecore_compute"
                                     : "sort_multicore_compute";   // ≤8-way + merge-tree
    if function_name == "topk":
        return "partial_sort_multicore_compute"   // per-core keep top-K
             + "partial_merge_multicore_compute"; // cross-core truncating merge
```

### Function Map

| Step | Symbol | Address | Returns / writes | Confidence |
|---|---|---|---|---|
| register lib | `ModuleArtifactInfo::addCustomOpLibFile(s,s,s,b,h,h)` | `libBIR 0x39bb00` | `unordered_map` entry | CERTAIN |
| register fn | `ModuleArtifactInfo::addCustomOpFunction(s,s,j)` | `libBIR 0x39cee0` | binds `fn → FunctionId` | CERTAIN |
| allocate id | `ModuleArtifactInfo::allocateCustomOpFunctionId()` | `libBIR 0x39a690` | dense `uint32`, deduped | CERTAIN |
| stamp byte | (inline in `visitInstCustomOp`) | `0x12613c0` body | `HEADER[0x0E] = u8` | CERTAIN |
| CPU dispatch | `sort_*` / `partial_*_multicore_compute` | Xtensa cpuN | the leaf compute | HIGH |

### Considerations

The numeric klr `opcode` (beta2) plays **no part** in selecting the `.so`: that selection is 100% the **string** `function_name` + `lib_file_name` via `ModuleArtifactInfo`; `nki_klr_sim` carries no op-name/lib map at all. The `.so` selection is driven by the registered library name; the `*_compute` selection is driven by the registered function name re-read on the CPU side.

> **GOTCHA — the kernel name and the `.so` are NOT on the wire.** The ISA stream is name-free. The `0x85` header carries only the numeric `CustomOpFunctionId` byte (`+0x0E`); the human-readable kernel/library name and the `libbuiltincustomop_cpu*` (or user `.so`) reference are registered separately into `ModuleArtifactInfo`, serialized into the NEFF metadata (`ucode_lib.json`), and resolved by the runtime loader against the FunctionId. A reimplementer who looks for the name in the instruction stream will not find it.

---

## 4. Three Header Subtleties

### Purpose

Three parts of the header layout are easy to get wrong from a casual read of the decompiler or the validator. This section pins each one and says what the wrong reading costs.

### A — `num_payloads` sits at `+0x0C`, in a contiguous `[0x0C..0x10]` count-band

`num_payloads` is a `u16` at byte offset `+0x0C`, inside the header's count/id block.

```c
// CoreV2GenImpl::visitInstCustomOp @0x12613c0 — the header stamps
setupHeader(&hdr, /*opcode=*/0x85);                   // hdr[0]=0x85, hdr[1]=0x10 → word 0x1085
customop_header_init(&hdr);                            // sub_122ED00 fills [0x04..0x0B] band
set_u16(&hdr[0x0C],"instr.num_payloads", N ? N+2 : 1);// sub_123CA60 — dest lea [r13+0Ch] @0x1262f75
hdr[0x0E] = function_id;                               // CustomOpFunctionId (u8)
set_u8 (&hdr[0xF], "instr.num_arguments", N);          // sub_1247BF0 — dest lea [r13+0Fh] @0x1262fbe
hdr[0x10] = 0;                                         // explicit zero; [0x11..0x3F] pxor-zero
```

The offset is pinned by the store destination in the encoder: `lea rdi,[r13+0Ch]` (machine bytes `49 8d 7d 0c`) @ `0x1262f75`, with the COLLECT-arm twin @ `0x1263201` stamping the same `[…+0Ch]`. `num_payloads`, `FunctionId` (`+0x0E`), and `num_arguments` (`+0x0F`) form one contiguous count-band running `[0x0C..0x10]`.

> **GOTCHA — the decompiler's `+ 6` is u16-pointer arithmetic, not a byte offset.** `visitInstCustomOp`'s decompiled call reads `sub_123CA60((_WORD*)hdr + 6, …)`, which looks like offset `+0x06`. `hdr` is typed `_WORD*`, so `+ 6` means `6 × 2 = +0x0C` bytes. The neighbouring `num_arguments` store uses a genuine `_BYTE*` `+ 15` → `lea [r13+0Fh]` → byte `+0x0F`: same setter family, different pointer type, which is exactly why only `num_payloads` is misleading.

> **NOTE — the count-band is clear of the header-init band.** `+0x0C` sits *past* the generic `[0x04..0x0B]` SyncInfo/header overlay that `customop_header_init` fills first. The count-band (`num_payloads` @ `+0x0C`, `FunctionId` @ `+0x0E`, `num_arguments` @ `+0x0F`, zero @ `+0x10`) does not overlap or overwrite that band — a reimplementer runs the shared header init, then stamps the count-band, with no ordering hazard.

### B — FunctionId is `uint32` in the registry, `uint8` on the wire

The same handle has two widths at two ends of one truncation. `allocateCustomOpFunctionId` returns a monotonic `uint32` and `addCustomOpFunction` takes a `uint32` id — the demangled signature `addCustomOpFunction(...)…EEES6_j` carries the trailing `j` for `unsigned int`. The wire slot, stored at `CustomOpLibInfo+0x28`, is a `uint8` spanning `0..254` with `0xFF` meaning unset.

```c
uint32 id   = allocateCustomOpFunctionId();   // registry namespace: dense uint32, 0,1,2,…
addCustomOpFunction(fn, lib, id);             // bound as uint32 (mangling …S6_j)
// … but the wire slot is one byte:
byte  fid   = *(uint8*)(CustomOpLibInfo[lib] + 0x28);  // truncated u8 (0xFF = unset)
HEADER[0x0E] = fid;                           // the 254 cap keeps it inside uint8 range
```

> **NOTE — the `254` cap is what keeps the truncation lossless.** The registry counts unique functions and `reportError`s past `0xFE`, so the dense `uint32` never exceeds the `uint8` wire slot. `0xFF` is reserved "unset". A reimplementer must enforce the cap *before* stamping, or the truncation silently aliases two ops to one FunctionId.

### C — the scratch tri-field: validated, never emitted

The header validator ([11.6](customop-wire-validators.md)) gates a `{byte[0x10], dword[0x18], byte[0x1C]}` scratch triple under an *all-zero-or-all-nonzero* rule. This is a wire slot for an SBUF scratch reservation — but **this build's encoder never populates it**: `visitInstCustomOp` writes `byte[0x10] = 0` (decompile line 827) and zero-fills `[0x11..0x3F]`, so the validator's all-zero branch is always taken. It is a validated-for-but-not-emitted slot. The SB scratchpad a custom op reserves is instead conveyed *structurally* as extra `MemoryLocation`s (tracked by `bir::isCCHavingSBScratchPad`), not through this header triple.

> **QUIRK — the scratch gate is all-zero-or-all-nonzero, not a range check.** A reimplementer validating the triple as a plain integer will wrongly accept a half-descriptor `{size, addr=0}`. The binary forbids exactly that: if *any* of the three subfields is nonzero, *all three* must be. Since this encoder emits all-zero, the non-zero branch is dead in this build — but a reimplemented encoder that wants to use the slot must honor the all-or-nothing rule.

---

## 5. The Wire Chain — `0x85` HEADER + `0x86` PAYLOADs

### Purpose

The custom op is the only TPB instruction that emits a **variable-length chain** of 64-byte words. This section gives the chain shape and the two byte maps so a reimplementer can encode a header/payload pair by hand. It is the synthesis of [11.5](customop-wire-layout.md) (byte layout) and [11.6](customop-wire-validators.md) (the integrity gates).

### Algorithm

```text
For a custom op with one output and N input args:

   CUSTOM_OP_HEADER   (0x85)   ── counts + FunctionId, NO access pattern
   CUSTOM_OP_PAYLOAD  (0x86)   ── OUTPUT access pattern        (payload 0)
   CUSTOM_OP_PAYLOAD  (0x86)   ── INPUT arg[0] access pattern  (payload 1)
   …
   CUSTOM_OP_PAYLOAD  (0x86)   ── INPUT arg[N-1] access pattern (payload N)

   total 64-byte words = 1 (header) + (N+1) (payloads) = N + 2
   HEADER num_payloads field  = N + 2   (counts the header itself AND the output)
```

```c
// The per-argument emit loop.   11.5 §1
emit_custom_op(inst, N):
    emit_header(0x85, num_payloads = (N ? N+2 : 1), fid, num_arguments = N);
    emit_payload(0x86, present=1, ADDR4_AP = getOutput_AP(inst, 0));   // sub_1210900
    for i in 0..N-1:
        emit_payload(0x86, present=1, ADDR4_AP = getArgument_AP(inst, i));
```

### Encoding

The header byte map (the live fields; everything else is reserved-zero or the scratch slot of [§4C](#c--the-scratch-tri-field-validated-never-emitted)):

| Field | Offset | Width | Value | Confidence |
|---|---|---|---|---|
| opcode | `+0x00` | 1 | `0x85` (133) → word `0x1085` | CERTAIN |
| `inst_word_len` | `+0x01` | 1 | `0x10` | CERTAIN |
| `num_payloads` | `+0x0C` | 2 | `N + 2` (`set_u16`, `sub_123CA60`, dest `lea [r13+0Ch]`) | CERTAIN |
| `CustomOpFunctionId` | `+0x0E` | 1 | `uint8` (0..254, 0xFF unset) | CERTAIN |
| `num_arguments` | `+0x0F` | 1 | `N` (`set_u8`, `sub_1247BF0`) | CERTAIN |
| reserved | `+0x10` | 1 | `0` (explicit) | CERTAIN |

The payload carries one operand: a present-flag `1` at `+0x0F` and an **ADDR4-rooted access-pattern descriptor** at `[0x10..0x3F]` (the 48-byte packing is the `sub_1210900` packer's job; its internals are deferred to the access-pattern page). The output is **positional** — it is always payload 0 — not flagged by a discriminator byte. The `_259` codegen's `addAP(isOutput=...)` directional bind does **not** survive as a wire byte; it survives positionally (output emitted first) plus via `setSrcsShape`/`setDstsShape` metadata.

> **GOTCHA — the `0x86` present-flag is a constant `1` on BOTH output and arg records.** `byte[0x0F]=1` reads like an "is-output" discriminator; it is a constant present-flag on every payload. Output-vs-arg is **positional** (output = first `0x86`). A decoder that keys output detection off `[0x0F]` will misclassify every record.

### Considerations

The validators ([11.6](customop-wire-validators.md)) re-check this wire in the ISA-check pass: opcode byte, the fixed `0x10` header-tag, the SyncInfo events, the reserved-zero discipline (the header reserves `[0x11..0x13] ∪ [0x1D..0x1F] ∪ [0x20..0x3F]`, masking *out* the scratch triple; the payload reserves only `[0x0C..0x0E]`), and the scratch tri-field gate. The arch gate is `birverifier::checkCustomOp @0xfdbf90` — **Sunda(20) ∨ Tonga(10) only**. The CoreV2 validation prologue also enforces the hardware rules: **≤1 output**, all args in SBUF or HBM, no `TensorIndirect` AP, and the `≤254` unique-fn cap — all confirmed as verbatim `.rodata` strings.

---

## 6. Builtin vs User — one ABI, two discriminators

### Purpose

SORT/TOPK are *builtin* custom ops; a user op rides the identical chain. The only difference is a `(lib_file_name, is_builtin)` pair. This section pins that distinction and contrasts the three unrelated top-k/sort paths a reimplementer must not conflate.

### The discriminator

The builtin set — SORT/TOPK — ships **inside** the toolchain as the prebuilt Xtensa images `libbuiltincustomop_cpu{0..7}.stripped.so`, reached via `builtin_custom_op` with `lib_file_name = libbuiltincustomop_cpu*` (the literal sits in `penguin.ir.CustomOp.so @0x15090`) and `is_builtin = True`. The `is_builtin` flag threads the whole stack: `penguin.ir.CustomOp.set_is_builtin` → `codegenCustomOp` copies it via `inst_bir.set_is_builtin(inst.is_builtin)` → the BIR inst reads it via `getIsBuiltinEvalIfNeeded` (`libBIR 0x176af0`).

```c
// The ONLY builtin-vs-user difference.
bool is_builtin = (lib_file_name starts_with "libbuiltincustomop_cpu");
// builtin:  lib_file_name = libbuiltincustomop_cpu*,  is_builtin = True
// user:     lib_file_name = a user-supplied Xtensa .so, is_builtin = False
// EVERYTHING else is identical: same penguin.ir.CustomOp → InstCustomOp →
//   same CoreV2 emitter → same 0x85/0x86 wire → same addCustomOpLibFile/Function
//   registration → same CoreV2 rules (≤1 output, SBUF/HBM args, no TensorIndirect,
//   ≤254 fns) → same extended_isa::sdk Xtensa ABI (export a *_compute fn, ≤8-way
//   cpu_id split, inline-shape rank≥1 tensors, dtype ∈ the 9 real-numeric
//   ScalarTypes, dram_addr ∈ [0x80000,0x90000), data_scratch overlay).
```

### Considerations

`is_builtin` most plausibly steers **where the loader finds the `.so`** — the bundled `data/custom_op/` directory for builtins versus the user-provided artifact for user ops — and possibly version-compat handling. The flag itself is traced through the whole stack; its exact runtime effect on `.so` location is [INFERRED].

> **NOTE — three unrelated "top-k" paths; do not conflate.** (1) The chain on *this* page: the CPU/GPSIMD bitonic SORT/TOPK in `libbuiltincustomop_cpu*`, reached by `builtin_custom_op` (full sort *or* partial top-K, `[2,d0,d1]` out). (2) The HLO `AwsNeuronTopK` → `legalize-topk` → `TopK_f32/f16/bf16` on the **DVE engine** (the framework-graph path) — disjoint, does **not** pass through `builtin_custom_op`. (3) The NKI device-side `rotational_topk`/`cascaded_max` on PE/DVE — a device kernel, **not** the Xtensa CPU lib. Only path (1) is the subject of Part 11.

---

## 7. The Macro-Kernel Lowerers — the inline-BIR sibling expansion

### Purpose

A second, parallel family of "custom" kernels lowers through the `walrus/inline_bir_kernel` library compiled into `nki_klr_sim`. These are **not** GPSIMD-CPU custom ops — they expand a macro kernel-inst node into constituent BIR ops on the standard TPB engines — but they share the carrier vocabulary, so a reimplementer must know they exist and where they diverge from the `InstCustomOp` chain.

### Algorithm

```c
// Per-class dispatch — NOT one global switch.
// Each backend <X>Kernel has its OWN lowerKernel() virtual (vtable slot).
// The macro-kernel inst node carries a class identity + a "model" name string;
// the class's lowerKernel() branches on that name to a concrete expander.
function lowerKernel(self, kernelInst):              // e.g. AttnFwdKernel::lowerKernel @0x5d72a0
    name = *(kernelInst.payload + 240);              // model name ptr; len at +248
    switch (name.len, name.bytes):
        case (20, "AttentionMMSoftmaxMM"):
            return lowerAttnFwdKernel(swap=1, cache=0);            // sub_5BB050
        case (37, "CausalAttentionMMSoftmaxMMWithoutSwap"):
            return (CP_degree <= 1) ? lowerAttnFwdKernel(0, 1)
                                    : lowerMPMDContextParallelAttnFwdKernel();  // sub_5E38A0
        // V2* names → FlashAttn; MLP *(a2+96) → CTE|TKG; Expert *(a2+133) → allExperts|allTokens
        default: return Status(2, "unknown model");
```

### Function Map

| Class::method | Address | Role | Confidence |
|---|---|---|---|
| `AttnFwdKernel::lowerKernel` | `0x5d72a0` | name-string dispatch → prefill attention | CERTAIN |
| `FlashAttnFwdKernel::lowerFlashAttnFwdKernel` | `0x620ef0` | V2 online-softmax attention | CERTAIN |
| `MLPKernel::lowerKernel` | `0x5ae930` | CTE (prefill) vs TKG (decode) split | CERTAIN |
| `ExpertMLPsTKGKernel::lowerKernel` | `0x5ab880` | all-experts vs all-tokens MoE | CERTAIN |
| `RouterTopKKernel::lowerKernel` | `0x6e51d0` | MoE gating top-k (args 3, out 12) | CERTAIN |
| `RMSNormQuantizationKernel::lowerRMSNormQuantKernel` | `0x6c57e0` | fused RMSNorm + per-token quant | CERTAIN |

### Considerations

These lowerers carry **verbatim arg/output contracts** as `.rodata` assert strings — e.g. `AttnFwd` inputs `nargs_wo_identity (+1 optional identity matrix)` and outputs `12 (cache)` / `10 (plain)`; `MLP-TKG` args `7|8 (+quantInputOffset)` out `10`; `RouterTopK` args `3` out `12`. The SwiGLU clamp is **config-driven, not baked**: `gate/up_clamp_{upper,lower}_limit` are `std::optional<float>` parsed from the kernel-inst JSON (default absent), applied as `min`/`max` `TensorScalarCustomDtype` ops *before* SiLU — `SwiGLU = act(clamp(gate)) * clamp(up)`. The decode (TKG) variants shard the hidden dim across LNC cores via `addLocalSendRecv` / `addStreamShuffleBroadcast` / `addStreamShuffleFlatten`; RoPE is fused only in the decode attention (`AttnTkg`, `_apply_rope`), which is exactly why it has `11` outputs with fused RoPE vs `10` without.

> **NOTE — these are NOT the GPSIMD-CPU custom ops of §1–§6.** They expand to ordinary BIR ops (matmul, activation, reduce, LNC collectives) on PE/Act/Pool/DVE engines — never to `InstCustomOp`, never to the `0x85`/`0x86` wire, never to an Xtensa `.so`. They share the "kernel-inst carrier with a model name" idea, but their target is the standard datapath. A reimplementer tracing a SORT/TOPK builtin must follow §1–§6; one tracing a fused attention/MLP macro kernel follows this section.

---

## 8. Evidence Anchors and Limits

The spine and its byte maps are pinned end-to-end; a handful of edges remain reconstructed. This table marks the residual-uncertainty boundary for a reimplementer.

| Claim | Grade | Basis / gap |
|---|---|---|
| beta3 spine (a)→(f); the §1 table; convergence at IT53; single CoreV2 emitter | CERTAIN | byte-verified symbols/offsets/strings |
| `num_payloads = N+2` @ `+0x0C`; FunctionId @ `+0x0E`; num_arguments @ `+0x0F` | CERTAIN | encoder bounded-setter stores (dest `lea [r13+0Ch]`/`[r13+0Fh]`) |
| op-name → FunctionId → wire-byte; 254 cap; uint32→uint8 truncation | CERTAIN | registry sigs + `+0x28` store |
| `(lib_file_name, is_builtin)` as the sole builtin/user discriminator | CERTAIN | flag traced through whole stack |
| 9-dtype roster + reject path; 8-image rebase `0x84000000 + id·0x200000` | CERTAIN | strings + `readelf`/`cmp` |
| beta2 `ExtendedInst` → `InstCustomOp` bridge is libwalrus-side | HIGH | `InstNKIKLIRKernel` translate symbols present; body not traced |
| runtime FunctionId → `.so` (STEP 4); isOutput → positional output | HIGH | `ModuleArtifactInfo` round-trip; positional reconcile |
| `is_builtin`'s exact loader effect on `.so` location | INFERRED | the flag is pinned; the runtime effect is deduced |
| `data0`/`data1` klr descriptor semantics; whether any shipped op emits beta2 | OPEN | round-tripped only; producer is upstream of `nki_klr_sim` |
| `SUNDA_APB_BASE` numeric; Xtensa instruction bodies (SIMD width, merge fan-in) | OPEN | no Xtensa disassembler; IDA decompiled = 0 — structure/string-only |

> **NOTE — the CPU-leaf interior is structure-only.** There is no Xtensa disassembler in this extraction (host binutils has no Xtensa backend; IDA recovered 0 decompiled bodies from the cpuN images). Every claim about *what the Xtensa code does* — the `cpu_id` derivation, the merge orchestration, the window-mapped DMA, the SIMD comparator network — is INFERRED from `.rodata`/`.data` strings, the byte-for-byte diff across the eight images, and assert templates, never from observed instructions. The *layout* — headers, sizes, entry `0x8400cd94`, the rebase law — is read directly; the *logic* is [INFERRED] and tagged as such on the leaf pages.

> **GOTCHA — the emitter body is missing from the partial decompile dump.** The `bir::InstCustomOp` class (ctor `@0x1789b0`, `getIsBuiltinEvalIfNeeded @0x176af0`, `createFromJson @0x179080`, `sameInst @0x17d870` in `libBIR`) and the registry signatures — `addCustomOpFunction(string,string,unsigned int)`, the `uint32` id of [§4B](#b--functionid-is-uint32-in-the-registry-uint8-on-the-wire), and the 6-arg `addCustomOpLibFile(…,bool,uchar,uchar)` — all resolve in the per-symbol sidecars. The emitter-internal facts do not: `CoreV2GenImpl::visitInstCustomOp @0x12613c0`, the `set_u16` store at byte `+0x0C` and `set_u8` store at byte `+0x0F`, and the `instr.num_payloads`/`instr.num_arguments` strings live only in the full `libwalrus.so` IDA database that [11.5](customop-wire-layout.md) and [11.8](customop-codegen.md) cite. Re-verifying from a text-sidecar-only slice will come up empty; open the `ida/` DB instead.

---

## Related Components

| Page | Relationship |
|---|---|
| [6.5.8 `builtin_custom_op`](../nki/neuroncodegen-builtin-customop.md) | stage (a): the NKI front door that builds `penguin.ir.CustomOp` |
| [7.32 klr `ExtendedInst`](../bir/klr-extendedinst-cbor.md) | stage (c2): the beta2 CBOR carrier that converges at IT53 |
| [11.8 Penguin/BIR codegen](customop-codegen.md) | stage (c1)→(d): the three codegen methods → `bir::InstCustomOp` |
| [11.5 Wire byte-layout](customop-wire-layout.md) | stage (e): the authoritative `0x85`/`0x86` byte map |
| [11.6 Wire validators](customop-wire-validators.md) | stage (e): the integrity gates + scratch tri-field |
| [11.7 FindCustomOpData staging](findcustomopdata-staging.md) | the FunctionId registry and `ucode_lib.json` packaging |
| [11.1 Xtensa ELF layout](gpsimd-xtensa-layout.md) | stage (f): the 8-image geometry and `cpu_id` derivation |
| [11.2 Bitonic SORT/TOPK](bitonic-sort-topk.md) | stage (f): the `*_compute` leaf algorithm |
| [11.3 CPU ABI `extended_isa::sdk`](customop-cpu-abi.md) | stage (f): the op-runtime ABI and DRAM window |
| [11.4 ATen/c10 surface](aten-c10-surface.md) | stage (f): the bundled tensor surface (no `Dispatcher`) |
| [11.9 Two-GPSIMD reconciliation](two-gpsimd-reconciliation.md) | the Pool-engine alias vs the Xtensa CPU name collision |

## Cross-References

- [`builtin_custom_op` (6.5.8)](../nki/neuroncodegen-builtin-customop.md) — where the chain begins; the `penguin.ir.CustomOp` build with no byte payload
- [klr `ExtendedInst` (7.32)](../bir/klr-extendedinst-cbor.md) — the parallel beta2 carrier; CBOR tag 210, kind 70, no op-name
- [Penguin/BIR Codegen (11.8)](customop-codegen.md) — the three codegen methods and the field-copy order into the BIR node
- [Wire Byte-Layout (11.5)](customop-wire-layout.md) — the byte-pinned `num_payloads@+0x0C` layout this page adopts
- [Wire Validators (11.6)](customop-wire-validators.md) — the reserved-zero masks and the scratch tri-field gate
- [FindCustomOpData Staging (11.7)](findcustomopdata-staging.md) — the `uint32` FunctionId allocator and the registry sink
- [Two-GPSIMD Reconciliation (11.9)](two-gpsimd-reconciliation.md) — disambiguates the Pool-engine `GPSIMD` from the Xtensa CPU `GPSIMD`
