# Codegen Helpers & the NKI-Kernel Provision Mechanisms

> *Version pin: `neuronx_cc 2.24.5133.0+58f8de22` (cp310; cp311/cp312 are address-twins). Three binaries carry this material. The shared codegen helpers and the lowering driver live in `neuronxcc/starfish/lib/libwalrus.so` (the Strand-I backend object — full dynamic symbol table). The three kernel-carrier node classes (`bir::InstBIRKernel` / `InstNKIKernel` / `InstNKIKLIRKernel`), their ctors, JSON serializers and enum tables live in `neuronxcc/starfish/lib/libBIR.so` (the defining library). The helper **bodies** were also read from `neuronxcc/.../bin/nki_klr_sim` (libwalrus statically linked; identical source, distinct addresses — `getActBiasTensor` = `sub_4F4BB0` there, `getIdentityMatrix` = `sub_506900`). For `.text`/`.rodata` in all three, virtual address equals file offset. Every claim is tagged CONFIRMED (read directly off these binaries) / STRONG (multi-evidence) / INFERRED / SPECULATIVE. Other wheels differ — treat every address as version-pinned.*

## Abstract

This page closes the KLR→BIR codegen sub-series. It covers two things that look unrelated but are both *tail-end* concerns of turning a traced NKI kernel into Backend IR.

First, three **shared codegen helpers** that `KlirToBirCodegen` calls while it walks the KLR AST: `getActBiasTensor` (a lazily-built, cached zero-valued bias tensor so every bias-less activation still has a bias operand), `getIdentityMatrix` (a per-dtype 128×128 SBUF identity, the stationary operand for transpose-via-matmul), and `getBankId` (codegen-time PSUM/SBUF bank-id resolution). These are utilities the per-op leaves ([7.21 KlirToBirCodegen](klir-codegen-dispatch.md)) reach for; they are not pipeline stages.

Second, the **kernel-provision mechanism** itself: how the `TranslateNKIASTToBIR` pass resolves an `InstNKIKLIRKernel(IT56)` placeholder into a real BIR function plus a call-site. There are **two** provision paths, selected by a single string field on the carrier node:

- **`lowerKernelInst` (Mechanism A)** — the carrier holds a *serialized KLR-AST binary*. The pass runs the full `codegenLncKernel` walk (the [7.21](klir-codegen-dispatch.md) machinery) to *build* the BIR body op-by-op, then tail-calls `lowerKLIRToNKI` to mint the call-site.
- **`lowerFromBirJson` (Mechanism B)** — the carrier holds an *already-lowered BIR-JSON blob*. No codegen runs; the [Two-Pass BIR-JSON Loader](json-loader.md) deserializes the body whole, and the call-site is minted inline.

Both converge on the identical downstream artifact — a separate NKI `bir::Function` (kind 2) plus an `InstNKIKernel(IT55)` call-site carrying the formal↔actual argument maps — differing only in *how the body arrives*. The `IT54`/`IT55`/`IT56` nodes are the KLR-AST kernel placeholders this lowering resolves; their three-way role is set on the emit side in [6.6.1 The Three-Sink Kernel-Node Model](../nki/three-sink-kernel-model.md).

For reimplementation, the contract is:

- **The three helpers** — what each materializes, the hard-wired float32 bias dtype, the 1.0f diagonal-fill identity, the codegen-time vs. accessor `getBankId` split, and the per-instance caches that make each helper fire at most once (or once per dtype).
- **The two provision mechanisms** — the `node.format == "bir"` discriminator, the disjoint callee sets that prove they are independent, and the field-by-field wiring each writes onto the `IT55` call.
- **The carrier → instructions resolution** — which path resolves which of `IT54`/`IT55`/`IT56`, and the `KernelSource` provenance tag (`Beta2` vs `Beta3`) each stamps.

| | |
|---|---|
| **Helper binary** | `libwalrus.so` (bodies also in `nki_klr_sim`) |
| **`getActBiasTensor`** | `@0xf16bc0` (lw) / `sub_4F4BB0 @0x4f4bb0` (sim) |
| **`getIdentityMatrix`** | `@0xf28910` (lw) / `sub_506900 @0x506900` (sim) |
| **`getBankId`** (codegen) | `KlirToBirCodegen::getBankId @0xf14ac0` |
| **Provision driver** | `TranslateNKIASTToBIR::run(bir::Module&) @0xf0dbc0` |
| **Mechanism A** | `lowerKernelInst @0xf0b610` → tail `lowerKLIRToNKI @0xf09c40` |
| **Mechanism B** | `lowerFromBirJson @0xf0a160` |
| **Discriminator** | `InstNKIKLIRKernel+0x110` (`"kernel_format"`) compared vs `"bir"` |
| **Carrier nodes** | `InstBIRKernel(54)` / `InstNKIKernel(55)` / `InstNKIKLIRKernel(56)` — ctors `0x409ee0/0x40a490/0x40a500` (libBIR) |
| **Provenance enum** | `bir::KernelSource{Beta1=0,Beta2=1,Beta3=2}` — `KernelSource2string @0x401c10` |

---

## Part 1 — The shared codegen helpers

The three helpers below are called from inside the KLR walk ([7.21](klir-codegen-dispatch.md)), not from the provision driver. They share a pattern: each is a *lazy materializer with a cache*, so a kernel that uses N bias-less activations builds **one** zero-bias tensor, a kernel that transposes M non-fp32 tensors of the same dtype builds **one** identity per dtype. The cache lives on the `KlirToBirCodegen` context object (`this`), so it is per-kernel.

### `getActBiasTensor` — the synthetic zero-bias

`InstActivation` (the BIR activation op) always has a bias operand. When the source NKI activation supplies no explicit bias, the codegen must still hand the builder *something*. `getActBiasTensor` is that something: a per-partition column of fp32 zeros.

It is called from **exactly one** site — `codegenNcActivate` (`sub_4F6B90` in the sim) — and that call sits on the cold branch taken only when the klr activation node carries no explicit bias (the explicit-bias case feeds the bias directly into `addActivation`'s `std::variant<float, bir::InstArg>` operand). [CONFIRMED — sole external xref to `sub_4F4BB0`]

```c
// getActBiasTensor(KlirToBirCodegen* this)  — sub_4F4BB0 @0x4f4bb0 (sim) / 0xf16bc0 (libwalrus)
// Builds ONE cached zero-bias SBUF tensor; all bias-less activations in the kernel share it.
bir::MemoryLocation* getActBiasTensor(KlirToBirCodegen* this) {
    if (this->zeroBiasPresent[+288] == 0) {                    // boost::optional engaged-byte
        suffix = uintToStr(this->ctx[+0xE0].idCounter[+240]++);    // monotonic per-codegen id
        name1  = "COMPILER-GENERATED-zero_bias_act_" + suffix;
        this->zeroBias[+296] =
            InstBuilder::addSBTensor(name1,
                                     /*nPartitions*/ this[+0x14C],   // arch partition count
                                     /*free dim   */ 1,
                                     /*Dtype      */ 0x10,           // = 16 = float32  (HARD-WIRED)
                                     /*flag*/ 0, /*uint*/ nullptr, /*bool*/ true);
        this->zeroBiasPresent[+288] = 1;
        // AP = (nPartitions, 1) unit pattern; size the memset cell from getDtype (== fp32, 4 B)
        InstBuilder::addMemset("COMPILER-GENERATED-memset_zero_bias_" + suffix2,
                               this->zeroBias, AP, /*value*/ 0);    // zero the tensor
    }
    return this->zeroBias[+296];
}
```

**Decisive fact — the bias dtype is hard-wired float32, independent of the activation data dtype.** The `addSBTensor` Dtype argument is the literal immediate `0x10` (BIR Dtype 16 = float32). This is the binary-level confirmation of the "bias/scale must be fp32" rule: the zero bias is *always* fp32, and the subsequent `getDtype` read only sizes the memset cell — it does not track the data dtype. [CONFIRMED — disasm `0x4f4c19: mov r8d, 10h` feeds `addSBTensor`'s Dtype slot, immediately preceding the `bir::InstBuilder::addSBTensor` call at `0x4f4c2f`; verified against the `nki_klr_sim` disasm this session]

The shape is `(nPartitions, 1)` — one fp32 zero per partition (a per-partition scalar bias column). The partition extent is `this+0x14C`, the arch partition count (INFERRED 128 for the Trainium PE array — STRONG, given the 128×128 identity below and the standard PE-array geometry). The cache is a `boost::optional<MemoryLocation*>` (engaged-flag `+288`, payload `+296`); three `boost::bad_optional_access` guards wrap every read. [CONFIRMED shape args; partition source STRONG]

### `getIdentityMatrix` — the per-dtype transpose stationary

For non-fp32 (or large) transposes, the codegen lowers `transpose` to a matmul against an identity matrix. `getIdentityMatrix(bir::Dtype dt)` materializes that identity — a 128×128 SBUF tensor — and **caches it per dtype, keyed by name**.

```c
// getIdentityMatrix(KlirToBirCodegen* this, bir::Dtype dt)  — sub_506900 @0x506900 / 0xf28910
bir::MemoryLocation* getIdentityMatrix(KlirToBirCodegen* this, bir::Dtype dt) {
    name = "COMPILER-GENERATED-id." + Dtype2string(dt);        // CACHE KEY = NAME
    Id = Function::getMemoryLocationByName(name);
    if (Id != nullptr) return Id;                              // per-dtype cache HIT

    Id = InstBuilder::addSBTensor(name);                       // fresh 128x128 SBUF tensor
    // boost::log perf warning:
    //   "Emitting dynamic Identity matrix generation code, this might affect performance."
    //   "You can bypass this by pass in an explicit identity matrix and use nc_matmul for transpose"
    AP = { 128, 128, step 1, 128 };                           // 128x128 2-D pattern (rank-word 0x400000002)
    InstBuilder::addMemset(name + "_memset", Id, AP, 0);       // (1) zero the whole tensor
    sel = addAffineSelect(this.ctx, name + "=IDAffineSelect(mask,fill=1)",
                          Id, Id, maskAP, fillAP, ...);        // (2) set the diagonal to 1.0f
    *(u32*)(sel + 0x194) = 0x3F800000;   // = 1.0f  ← DIAGONAL FILL value
    *(u32*)(sel + 0x190) = 19;           // affine-select / act-func kind discriminator
    push Id onto this->identityMatrices[+0x130];               // for late legalizeIdentityMatrices fixup
    return Id;
}
```

The realized operation is `out[p,e] = (p==e) ? 1.0f : 0` — `memset(0)` zeros the off-diagonal, the affine-select stamps `0x3F800000` (= 1.0f) onto the diagonal selected by a `(1,128)`-strided mask. [CONFIRMED — disasm `0x506d98: mov dword ptr [rbx+194h], 3F800000h`; the `"=IDAffineSelect(mask,fill=1)"` inst name, the `"Emitting dynamic Identity matrix generation code…"` and `"getIdentityMatrix"` LEAs at `0x506ac4`/`0x506b10`, and the `addMemset` call at `0x506c29` were all read off the `sub_506900` disasm this session. The exact `p==e` mask predicate is INFERRED from the `(1,128)` mask AP + the inst name; the mask AP was not unrolled.]

> **NOTE — the fill offset is `+0x194` (404), not `+0x180`.** The 1.0f write lands at the affine-select instruction's `+0x194` slot and the kind discriminator `19` at `+0x190`. These are the affine-select op's own fields, not the identity tensor's; the identity tensor itself is plain (memset + one selective fill). [CONFIRMED — `mov dword ptr [rbx+194h]`]

One identity per dtype is built at most once per function, then pushed onto the codegen's identity-matrix vector at `this+0x130` for the late `legalizeIdentityMatrices` address-fixup pass. Source locations: `klir_to_bir_codegen.cpp:1575` (memset) / `:1579` (affine-select). [CONFIRMED]

### `getBankId` — codegen-time bank resolution

`getBankId(shared_ptr<klr::TensorName>, uint, uint)` resolves a PSUM (or SBUF) **bank id** for a klr tensor operand at tensor-creation time; the two trailing uints are bank/partition hints from the klr AP. The resolved id is passed as the `long bankId` argument of `InstBuilder::addPSUMTensor(string const&, uint, uint, uint, long bankId, bir::Dtype)` — note `addSBTensor` has no bank argument; only PSUM tensors carry one. [STRONG — signature from the libwalrus roster; the body is a libwalrus-internal accessor with no string/assert hook in the sim, so it inlines without a decompilable stub]

> **GOTCHA — there are two `getBankId`s.** They are different functions with opposite roles:
>
> | Symbol | Address | Role |
> |---|---|---|
> | `KlirToBirCodegen::getBankId` | `0xf14ac0` | **assigns/derives** the bank id during codegen (this one) |
> | `bir::MemoryLocation::getBankId` | `0x3fda5a8` (libBIR; PLT thunk `0x44dcd0` in sim) | pure **accessor** returning the stored bank id at `MemoryLocation+0x210` (+528), set by `setBankId @0x32dc40` |
>
> The codegen one *writes* the placement; the accessor one *reads* it back. PSUM uses `+528` for the bank; SBUF uses `+536` for the base partition. `toString` renders `abs(bankId)` for `type == PSUM`. [CONFIRMED cross-ref]

The bank id `getBankId` assigns is only the **initial/hint** bank. The final physical bank is fixed much later: `LegalizeMatmulAccumulationGroups` groups matmuls by same bank, computes the bank span, and a single accumulate-group's zero-region caps at **8 banks** (a power-of-2 window `k ≤ 3`; a 16-bank window is uncoverable → the group is rejected with *"accumulation group is too large for SB"* and spilled to SBUF + column-tiled re-reduce). So `getBankId` provides the codegen-time placement; the PSUM coloring allocator and address-rotation pass fix the physical bank. [STRONG]

### Per-arch limits the helpers read

Two trivial accessors over the `KlirToBirCodegen` context carry the per-arch budget that the provision driver later reads (Part 2):

```c
uint getSbufMaxBytes(KlirToBirCodegen* this) {            // @0xf14420
    return *(u32*)(this + 0x118);                         // the SBUF byte budget
}
uint getPsumMaxBankId(KlirToBirCodegen* this) {           // @0xf14430
    uint total = *(u32*)(this + 0x158);                   // total PSUM accumulator count
    if (total == 0) return 0;
    return (total - 1) / *(u32*)(this + 0x154);           // / per-bank divisor → max legal bank index
}
```

[CONFIRMED — both disasm-read in the helper reports.] The **numeric** per-arch constants that fill `this+0x154`/`+0x158`/`+0x14C` come from the `libwalrus` `ArchModel` (`core_vN_bir.cpp`); the per-generation PSUM-bank-count and SBUF-byte values are documented from the geometry singletons in [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md). What is established structurally here: a 4 MiB on-chip PSUM window, ≥8 PSUM banks, an 8-bank max accumulate-group, and 128 partitions. [CONFIRMED structural bounds; numeric per-gen values are the ArchModel's, documented separately]

---

## Part 2 — The two kernel-provision mechanisms

`TranslateNKIASTToBIR::run` ([7.21](klir-codegen-dispatch.md) covers the dispatch; see also [Part 8 translate-nki](../walrus/) when published) walks every basic block of every function, finds each `bir::InstNKIKLIRKernel` (`InstructionType == 56`) placeholder, and calls `lowerKernelInst(node, BB)` on it. After the block scan it removes the placeholders (collect-then-remove, to avoid iterator invalidation). The node-find is an exact integer compare `*(u32*)(ins+0x50) == 56` (`0x38`) — only IT56 triggers lowering. [CONFIRMED — `run @0xf0dbc0`]

`lowerKernelInst @0xf0b610` is the **two-way split** on the carrier's format field.

> **GOTCHA — two unrelated `lowerKernelInst` symbols.** `nm -DC` shows two:
>
> | Symbol | Address | Operates on |
> |---|---|---|
> | `TranslateNKIASTToBIR::lowerKernelInst(InstNKIKLIRKernel&, BasicBlock&)` | `0xf0b610` | **IT56** carriers (this page) |
> | `InlineNKIKernel::lowerKernelInst(InstNKIKernel&, BasicBlock&, json&)` | `0xefddc0` | already-lowered **IT55** call-sites (a *later* pass) |
>
> They belong to different classes and different passes. Do not conflate them; this page's target is `0xf0b610`. [CONFIRMED via `nm -DC`; both decompiled bodies present in the corpus this session]

### The discriminator — one string field

The split is a single `std::string::compare` against `"bir"`:

```c
// lowerKernelInst @0xf0b610 — head (disasm 0xf0b642..0xf0b689)
fmt = std::string(node[+0x110] /*ptr*/, node[+0x118] /*len*/);   // the "kernel_format" field
if (fmt.compare("bir") == 0)                                       // "bir" = "backend.bir"+9
    return lowerFromBirJson(node);                                 // MECHANISM B; return its bool
// else fall through: MECHANISM A (KLR-compiled AST)
```

So the discriminator is the `"kernel_format"` `std::string` at **`InstNKIKLIRKernel + 0x110`** (the same slot [6.6.1](../nki/three-sink-kernel-model.md) and [7.21](klir-codegen-dispatch.md) name):

- `== "bir"` ⇒ **Mechanism B** (pre-compiled BIR-JSON; `lowerFromBirJson`; returns `bool`)
- `!= "bir"` ⇒ **Mechanism A** (KLR-compiled AST; `KlirToBirCodegen` codegen<Op> expansion; returns `1`)

[CONFIRMED — the `std::string::compare()` followed by the `lowerFromBirJson` call, the `addNKIFunction` / `codegenLncKernel` path, and the `getIsAllocated` gate were all read off the real `lowerKernelInst @0xf0b610` decompile this session.]

> **QUIRK — the callee sets are disjoint, which proves the mechanisms are independent.** `lowerFromBirJson` calls *neither* `klr::*` deserializers *nor* `addNKIFunction` *nor* any `KlirToBirCodegen` method *nor* `setMapping`; the KLR path calls *neither* nlohmann *nor* `createFromJson` *nor* `getNeuronCoreId` *nor* `createMappingFromNames`. The two callee sets are disjoint in exactly the places that matter — the cleanest possible confirmation of two genuinely independent provision mechanisms sharing only the convergence shape. [CONFIRMED — callgraph.json]

### Mechanism A — `lowerKernelInst` (serialized KLR-AST → codegen)

The carrier holds a serialized klr-binary file. The pass deserializes it, runs the *full* KLR walk to build the BIR body op-by-op, then tail-calls `lowerKLIRToNKI` to mint the call-site.

```c
// lowerKernelInst @0xf0b610, format != "bir"   (decompile + disasm anchors)
checkVersionMatch(node);                            // KLR version gate: sscanf "M.m.p" vs "1.0.0"
FILE* f = fopen(node[+0xF0], "r");                  // the serialized KLR file
if (!f) throw runtime_error("fail to locate KLIR artifact");
klr::KLRFile_des(&hdr, f);                          // header: version word[2] MUST == 12
if (hdr[2] != 12) throw runtime_error("Wrong KLR version");
klr::KLRMetaData_des(&meta, f);
klr::Contents_des(&contents, f);                    // payload (refcounted)
if (*(u32*)contents != 4) throw runtime_error("Wrong KLIR content type");  // tag 4 = "KLIR kernel"
fclose(f);

assert(node.getFunction().parentModule != nullptr); // FunctionHolder.h:25
bir::Function* F = Module::addNKIFunction();         // NEW empty function in the module
*(u32*)(F + 0xD0) = 2;                               // Function kind = NKI (2)
KlirToBirCodegen cg(F);                              // ctor binds codegen budgets to F
cg.name = std::string(node[+0xF0], len);
if (!node[+0x170]) {                                 // skip block when json-source flag set
    cg.setDeviceDump(node[+0x150]);                  // device-dump byte
    if (node.getDebugInfo()) cg.setParentTensorizerId(...);
}
cg.codegenLncKernel(contents+8);                     // <<< THE KLR WALK (7.21): builds F's BIR body
// ... debug-info side file, finalize F, prune empty blocks ...
assert(cg.getIsAllocated());                         // NeuronAssertion @cpp:1670 (kernel pre-allocated)

// OPERAND RE-BINDING — MANUAL, per-pair
FunctionArgumentMap inMap;
for (i, arg : node.INPUT_list) {
    if (!F.getMemoryLocationByName(arg.name)) throw runtime_error("<name> input cannot be found ");
    srcSet = node.getArgument<AccessPattern>(i).getLocation().getMemoryLocationSet();   // caller actual
    dstSet = F.getMemoryLocationByName(arg.name).getMemoryLocationSet();                // callee formal
    inMap.setMapping(dstSet, srcSet);                // callee <- caller
}
FunctionArgumentMap outMap;  /* mirror over node.OUTPUT_list; throws " output cannot be found " */

lowerKLIRToNKI(node, *F, cg.name, inMap, outMap, cg);   // <<< mint the IT55 call-site (below)
return 1;
```

The body production is *genuine per-op codegen*: `codegenLncKernel → codegenStmt → codegenOperator → codegen<Op>` (the ~141 hooks of [7.21](klir-codegen-dispatch.md)). The emitted BIR instructions are inserted into `F`'s basic blocks **inside** `codegenLncKernel` — *not* into the caller block. Operand re-binding is **manual**: `lowerKernelInst` walks the node's formal in/out lists, looks each formal name up in `F`, and pairs it with the caller's actual `MemoryLocationSet` via `setMapping` per pair. [CONFIRMED — decompile lines 1368–1539; the `" input cannot be found "` / `" output cannot be found "` throw strings]

The KLR file is pre-*allocated*: the `cpp:1670` assert `codegen.getIsAllocated()` guards that the NKI front-end already ran the allocator (SBUF/PSUM addresses assigned in the KLR). `TranslateNKIASTToBIR` therefore consumes *already-allocated* kernels. [CONFIRMED]

### `lowerKLIRToNKI` — the shared IT55 emitter (Mechanism A tail)

`lowerKLIRToNKI @0xf09c40` does **no KLR traversal**. By the time it runs, `codegenLncKernel` has already walked the whole AST and filled `F`. Its one job: mint **one** `InstNKIKernel(IT55)` call-site *before* the IT56 node, point it at `F`, copy the two argument maps onto it, clone the operands, and (if allocated) stamp the scratchpad budget.

```c
// lowerKLIRToNKI(node /*IT56*/, F /*NKI fn*/, name, inMap, outMap, cg) @0xf09c40
bb       = *(BasicBlock**)(node + 0x50);              // parent block backref
elemName = "inst__" + name;
bb.checkInsertionPointValid(node + 8);
InstNKIKernel* call = bb.insertElement<InstNKIKernel>(node + 8, elemName);  // IT55 minted before IT56

call[+0xF0]  = F;                                      // getFunc() target  (verifier: checkNKIKernelFunction)
SmallVector_assign(&call[+0xF8],  &inMap.vector);      // input  FunctionArgumentMap  (deep copy)
SmallVector_assign(&call[+0x120], &outMap.vector);     // output FunctionArgumentMap
call[+0x160] = 1;                                      // KernelSource = Beta2 (KLR carrier)

for (a : node[+0xA0..0xB0] input list)  if (isa<AccessPattern>(a)) a.cloneToArgument(call, true);
for (o : node[+0xC0..0xC8] output list) o.cloneToOutput(call, true);

if (!cg.getIsAllocated()) return call;                 // un-allocated → no budget
call[+0x148] = cg.getSbufMaxBytes();                   // sb_per_partition_size  (FROM THE CODEGEN)
call[+0x14C] = 128;                                    // sb_num_partitions
am = getArchModel(F.parentModule.getArch());
call[+0x158] = cg.getPsumMaxBankId() + 1;              // psum_num_partitions
call[+0x150] = swizzle(am partition descriptor);       // arch / psum partition geometry
return Instruction::createKernelScratchpadBuffers<InstNKIKernel>(call);   // tail-jump: reserve scratch
```

The scratchpad budget comes from the **codegen object** (`getSbufMaxBytes` / `getPsumMaxBankId` / `getIsAllocated`) — `lowerKLIRToNKI` never re-scans `F`'s allocs. This is the structural reason the codegen is passed `const&`. The arg-maps are **deep-copied into the call node** (the `SmallVector` of the `llvm::MapVector<MemoryLocationSet*, MemoryLocationSet*>` is bit-copied to `call+0xF8`/`+0x120`), so the IT55 call *permanently carries* the formal↔actual mapping — the data a later inliner needs to rebind operands. [CONFIRMED — disasm transcribed; offsets settled at `+0xF8`/`+0x120` (see correction note below)]

> **CORRECTION — `lowerKLIRToNKI` is called *directly*, not only via PLT.** An earlier reading held it "reached only through its PLT stub `0x5f60f0`." The callgraph shows exactly one caller, a *direct* call: `lowerKernelInst` (decompile line 1541). The PLT thunk `0x5f60f0` is just the cross-section trampoline. [CONFIRMED — callgraph + decompile]

### Mechanism B — `lowerFromBirJson` (pre-compiled BIR-JSON)

When `kernel_format == "bir"`, the body is *already* BIR; no codegen runs. The [Two-Pass BIR-JSON Loader](json-loader.md) deserializes it whole, and the call-site is minted inline (re-implementing `lowerKLIRToNKI`'s finalization rather than calling it).

```c
// lowerFromBirJson(node) @0xf0a160, format == "bir"
ifstream in(node[+0xF0]);
json j = nlohmann::parse(in);                          // nlohmann v3.11.3
Module* M = node.getFunction().parentModule;           // FunctionHolder.h:25 assert
fns = j["functions"];                                  // ARRAY: one entry per logical neuron core

// LNC-SHARDING GATE  (the one gating Mechanism B has and A lacks)
coreId   = M.getNeuronCoreId();                        // boost::optional<unsigned>
lncCount = size(fns);
if (!coreId.has_value && lncCount > 1)
    throw runtime_error("NeuronCoreId must be set when the LNC count is greater than 1");
if (coreId.has_value && coreId.value >= lncCount) {    // this core not represented
    log "The kernel is compiled for LNC count lower than the current core_id <id> skip lowering";
    return false;                                      // SKIP — node materialises NO function here
}
f_json = fns[coreId.value];                            // pick THIS core's function
name   = f_json.at("name");

bir::Function* F = bir::Function::createFromJson();    // PASS1: storages, blocks, insts, args
bir::Function::createFromJsonPass2();                  // PASS2: deps, queues, phi incoming
*(u32*)(F + 0xD0) = 2;                                 // NKI kind = 2
// RE-SCAN F.allocs() to detect SBUF/PSUM usage & is-allocated  (no codegen object exists)
for (ml : F.allocs()) {
    ty = *(u32*)(ml + 0xD8);                           // MemoryType: 16 = SB, 32 = PSUM
    if (ty == 16 && ml.allocated) { sbMax = max(sbMax, ml.size + ml.getAddress()); isAlloc = 1; }
    else if (ty == 32 && ml.allocated) isAlloc = 1;
}
InstNKIKernel* call = callerBB.insertElement<InstNKIKernel>(insertPt, "inst__"+name);  // IT55
call[+0xF0]  = F;
FunctionArgumentMap::createMappingFromNames(<input name-map>,  callerFn, F);   // BY NAME, in one shot
FunctionArgumentMap::createMappingFromNames(<output name-map>, callerFn, F);
call[+0x160] = 2;                                       // KernelSource = Beta3 (BIR-JSON carrier)
for (a : node[+0xA0..0xB0]) if (isa<AccessPattern>(a)) a.cloneToArgument(call, 1);
for (o : node[+0xC0..0xC8]) o.cloneToOutput(call, 1);
if (isAlloc) { call[+0x148]=sbMax; call[+0x14C]=128; ...; createKernelScratchpadBuffers(call); }
copyConstFileToArtifact(node, *F, kernelDir);          // localize TensorClass::Const(6) weight files
return true;
```

The two differences from Mechanism A that matter:

1. **Operand re-binding is by name in one shot.** `createMappingFromNames(map<string,string>, callerFn, F)` builds the whole `FunctionArgumentMap` from a name correspondence — the source `map<string,string>` being exactly the carrier's `func_args`/`func_outs` (`InstNKIKLIRKernel+0x178`/`+0x1A8`). Mechanism A builds the same `MapVector` manually, per pair. [CONFIRMED]
2. **The budget comes from a re-scan**, not a codegen. With no `KlirToBirCodegen` object to ask, `lowerFromBirJson` walks `F.allocs()` itself to find the SBUF high-water and PSUM usage. [CONFIRMED]

> **QUIRK — the LNC-sharding gate is unique to Mechanism B.** A BIR-JSON `"functions"` array holds *one function per logical neuron core*; the entry chosen is `fns[NeuronCoreId]`. If `coreId ≥ array length`, the kernel was "compiled for fewer LNC than the current core" and is **skipped** for this core (`return false`, the node simply materialises no function on this core — `run` still removes the placeholder). If `coreId` is unset but more than one function is present, it errors. Mechanism A has no such gate: a serialized KLR binary is already per-core. [CONFIRMED]

### The two mechanisms side by side

| | **Mechanism A** (`lowerKernelInst`, KLR) | **Mechanism B** (`lowerFromBirJson`, BIR-JSON) |
|---|---|---|
| Selected when | `node.format != "bir"` | `node.format == "bir"` |
| Carrier form | serialized klr-binary (`fopen`) | BIR-JSON file (nlohmann) |
| Version gate | `checkVersionMatch` "1.0.0"; hdr word 12, tag 4 | BIR-JSON `"version"` int in the loader |
| Body production | `KlirToBirCodegen` codegen<Op> (`codegenLncKernel`, ~141 hooks) | `Function::createFromJson` + `…Pass2` ([loader](json-loader.md)) |
| New function via | `Module::addNKIFunction()` | `Function::createFromJson()` |
| LNC-sharding gate | none (KLR is per-core) | `coreId` vs `len(j["functions"])`; skip if out of range |
| Operand re-bind | **manual** per-pair `setMapping` | **by name** `createMappingFromNames` ×2 |
| IT55 emitter | `lowerKLIRToNKI` (tail; copies maps to `call+0xF8`/`+0x120`) | inline (same `insertElement` + clone) |
| Budget source | codegen: `getSbufMaxBytes`/`getPsumMaxBankId`/`getIsAllocated` | re-scan `F.allocs()` MemoryType 16/32 + `getArchModel` |
| `KernelSource` tag (`+0x160`) | `1 = Beta2` | `2 = Beta3` |
| Returns | `1` (always) | `true` / `false` (LNC-skip) |

**Convergence (identical for both):** a new NKI `bir::Function` (kind 2) holding the full BIR body, plus a `bir::InstNKIKernel(IT55)` call-site (`call+0xF0 → F`) with the node's caller-side `AccessPattern` operands cloned and a scratchpad sized from the SBUF/PSUM budget. The IT56 placeholder is then removed by `run`. [CONFIRMED — both produce the same downstream artifact, differing only in *how* the body arrives.]

---

## Part 3 — The IT54 / IT55 / IT56 carrier resolution

The three kernel opcodes form a **lowering triple, not three peers**. All three derive from `bir::Instruction`; the IT ordinal sits at `Instruction+0x58`. The role split is set on the emit side — see [6.6.1 The Three-Sink Kernel-Node Model](../nki/three-sink-kernel-model.md) — and resolved on the lowering side here.

| Node | IT | ctor (libBIR) | Role | Resolved by |
|---|---|---|---|---|
| `InstNKIKLIRKernel` | 56 | `0x40a500` | frontend **carrier** (registry-traced) | `lowerKernelInst` (A or B) → consumed & removed |
| `InstNKIKernel` | 55 | `0x40a490` | post-lowering **call-site** (user `@nki.jit`) | minted by `lowerKLIRToNKI`/`lowerFromBirJson`; later inlined |
| `InstBIRKernel` | 54 | `0x409ee0` | **library** kernel (pre-compiled BIR) | `InlineBIRKernel` / `BIRKernelWrapper` — *not* this pass |

The IT literals are passed directly to the `Instruction` base ctor as the 4th argument; verified in the ctor bodies this session: `bir::Instruction::Instruction(a1, a2, a3, 54, …)` (InstBIRKernel), `…, 55, …` (InstNKIKernel), `…, 56, …` (InstNKIKLIRKernel). [CONFIRMED — read off the `0x409ee0`/`0x40a490`/`0x40a500` decompiled bodies]

### What each path resolves

**`IT56` is the input to this pass.** It is the *only* node the provision driver resolves: the NKI middle-end emits one `InstNKIKLIRKernel` per registry-traced kernel; `TranslateNKIASTToBIR::run` finds it (`*(u32*)(ins+0x50) == 56`), `lowerKernelInst` dispatches on its `kernel_format`, and `run` removes it after lowering. It is fully round-trippable on the BIR side (symmetric `readFieldsFromJson`/`toJson`), but it never survives this pass.

**`IT55` is the output of this pass — produced by *both* paths.** Mechanism A's `lowerKLIRToNKI` and Mechanism B's `lowerFromBirJson` each mint an `InstNKIKernel` call-site referencing the freshly built function. The only path-dependent difference visible on the produced node is the `KernelSource` tag at `+0x160`:

```c
// KernelSource2string @0x401c10 (libBIR):  0 -> "Beta1"   1 -> "Beta2"   2 -> "Beta3"
//                                          (other -> FATAL "Unknown KernelSource", brewer.py)
//   Mechanism A (lowerKLIRToNKI)    writes +0x160 = 1 = Beta2   (KLR-AST carrier origin)
//   Mechanism B (lowerFromBirJson)  writes +0x160 = 2 = Beta3   (already-BIR carrier origin)
```

The `+0x160` field is `bir::KernelSource`, settled by `InstNKIKernel::readFieldsFromJson` reading the key `"kernel_source_val"` via `bir::from_json(…, bir::KernelSource&)`. [CONFIRMED — `KernelSource2string @0x401c10` body contains `"Beta2"` and the `"Unknown KernelSource"` FATAL (brewer.py), read this session; the `Beta2`/`Beta3` ↔ KLR/BIR-JSON mapping is the byte-verified `+0x160 = 1` vs `= 2` write.]

> **GOTCHA — IT55's serializer is asymmetric (lossy).** `InstNKIKernel::toJson @0x43a4f0` emits `func`/`func_args`/`func_outs`, but `readFieldsFromJson @0x430410` reads **only** `sb_buf_shape`/`psum_buf_shape`/`kernel_source_val`/`address_rotation_scope_val` — it does *not* read back `func`/`func_args`/`func_outs`. A reloaded IT55 would have a null `getFunc()`. This is why IT55 is internal-only: it exists transiently between `TranslateNKIASTToBIR` (which mints it) and the inliner (which expands and removes it), never persisted-then-reloaded as the operative carrier. It is also why IT55/IT56 `sameInst` is `__assert_fail "Not Implemented"` (no CSE/dedup), whereas IT54 has a real structural `sameInst @0x2f66a0`. [CONFIRMED — ctor + serializer offsets `+0xF0`/`+0xF8`/`+0x120` are the libBIR-defining truth]

**`IT54` is *not* touched by this pass.** `InstBIRKernel` is a library kernel that arrives *already* as BIR (the `_private_kernels.<leaf>.so` body, spliced in by name). It is expanded by a different pass entirely — `InlineBIRKernel::run @0xd86510` via `BIRKernelWrapper::createInstance` — with no JSON round-trip and no KLR walk. It carries `kernel_name` + `srcs_shape`/`dsts_shape` + an opaque `kernel_attrs` json + the fusion options (`auto_cast`, `quant_kernel`, `norm_type`, `is_causal`, `lnc_size`, …), but **no version triplet** (the `ulib_to_ucode_version`/`ulib_to_isa_version` triplet belongs to `InstCustomOp(IT53)`, not here). It is listed in the table above only to complete the triple. [CONFIRMED]

### The carrier → instructions resolution, in one diagram

```text
NKI middle-end (BirCodeGenLoop, registry-traced kernels)
   │  emits ONE InstNKIKLIRKernel (IT56) per kernel into the BIR module
   ▼
IT56 { klir_binary@+0xF0, kernel_format@+0x110, func_args@+0x178/func_outs@+0x1A8 (map<str,str>),
       sb_buf_shape, psum_buf_shape, enable_device_dump, address_rotation_scope_val }
   │  TranslateNKIASTToBIR::run → lowerKernelInst dispatch on kernel_format@+0x110:
   │    FORM A (!="bir"): klr::*_des → KlirToBirCodegen::codegenLncKernel (KLR walk) → NKI Fn
   │                      → lowerKLIRToNKI mints IT55 (KernelSource = Beta2)
   │    FORM B (=="bir"): createFromJson(j["functions"][coreId]) → NKI Fn
   │                      → lowerFromBirJson mints IT55 (KernelSource = Beta3) + copyConstFileToArtifact
   │  (IT56 removed by run())
   ▼
IT55 { getFunc→NKI Fn @+0xF0, func_args@+0xF8 / func_outs@+0x120 (MapVector<MLSet*,MLSet*>),
       sb_buf_shape@+0x148, psum_buf_shape@+0x150, kernel_source_val@+0x160, rotation@+0x15C }
   │  (a LATER pass) InlineNKIKernel::run / sim buildInstCall: splice NKI Fn body into the caller,
   │  rebind operands via the two FunctionArgumentMaps, remove the IT55 + the NKI Fn.
   ▼
Inlined BIR body (the leaf opcodes) in the caller.

(Orthogonal: IT54 InstBIRKernel — a library kernel already in BIR, expanded by
 InlineBIRKernel/BIRKernelWrapper; no IT56/IT55 involvement.)
```

---

## Function & symbol map

| Symbol | Address | Binary | Role | Conf |
|---|---|---|---|---|
| `KlirToBirCodegen::getActBiasTensor` | `0xf16bc0` / `sub_4F4BB0 @0x4f4bb0` | libwalrus / sim | cached fp32 zero-bias builder | CONFIRMED |
| `KlirToBirCodegen::getIdentityMatrix` | `0xf28910` / `sub_506900 @0x506900` | libwalrus / sim | per-dtype 128×128 identity | CONFIRMED |
| `KlirToBirCodegen::getBankId` | `0xf14ac0` | libwalrus | codegen-time bank-id resolve | STRONG |
| `bir::MemoryLocation::getBankId` | `0x3fda5a8` | libBIR | bank-id accessor (`+528`) | CONFIRMED |
| `KlirToBirCodegen::getSbufMaxBytes` | `0xf14420` | libwalrus | `*(u32*)(this+0x118)` | CONFIRMED |
| `KlirToBirCodegen::getPsumMaxBankId` | `0xf14430` | libwalrus | `(total-1)/divisor` | CONFIRMED |
| `TranslateNKIASTToBIR::run` | `0xf0dbc0` | libwalrus | module walk; finds IT56; removes it | CONFIRMED |
| `TranslateNKIASTToBIR::lowerKernelInst` | `0xf0b610` | libwalrus | per-node A/B dispatch; Mechanism A | CONFIRMED |
| `TranslateNKIASTToBIR::lowerFromBirJson` | `0xf0a160` | libwalrus | Mechanism B (BIR-JSON) | CONFIRMED |
| `TranslateNKIASTToBIR::lowerKLIRToNKI` | `0xf09c40` | libwalrus | shared IT55 emitter (A tail) | CONFIRMED |
| `TranslateNKIASTToBIR::copyConstFileToArtifact` | `0xf08980` | libwalrus | localize `TensorClass::Const(6)` files | CONFIRMED |
| `backend::checkVersionMatch` | `0xf07570` | libwalrus | KLR version gate ("1.0.0") | CONFIRMED |
| `KlirToBirCodegen::codegenLncKernel` | `0xf34140` | libwalrus | the KLR-AST walk ([7.21](klir-codegen-dispatch.md)) | CONFIRMED |
| `InlineNKIKernel::lowerKernelInst` | `0xefddc0` | libwalrus | **different** pass — operates on IT55 | CONFIRMED |
| `bir::InstBIRKernel::InstBIRKernel` | `0x409ee0` | libBIR | IT54 ctor (literal `54`) | CONFIRMED |
| `bir::InstNKIKernel::InstNKIKernel` | `0x40a490` | libBIR | IT55 ctor (literal `55`) | CONFIRMED |
| `bir::InstNKIKLIRKernel::InstNKIKLIRKernel` | `0x40a500` | libBIR | IT56 ctor (literal `56`) | CONFIRMED |
| `bir::KernelSource2string` | `0x401c10` | libBIR | `{Beta1=0,Beta2=1,Beta3=2}` | CONFIRMED |

## Diagnostic strings

From `lowerKernelInst` / `run` (Mechanism A + driver):

- `"Started running Lower KLIR Kernel into NKI kernel instruction"`, `"Found InstKLIRKernel: <name>"`, `"Scan BKs time (s): <t>"`
- `"KLR file has been loaded from disk"`, `"KLR file header, version: <a>.<b>.<c>"`, `"KLR meta data : "`, `"Start to read content"`, `"KLR content type : "`, `"KLR Kernel : "`
- `"fail to locate KLIR artifact"`, `"Wrong KLR version"` (hdr word ≠ 12), `"Wrong KLIR content type"` (tag ≠ 4)
- `"<name> input cannot be found "` / `"<name> output cannot be found "` (arg-map resolution miss)
- `"codegen.getIsAllocated()"` (NeuronAssertion @`cpp:1670` — kernel must be pre-allocated)

From `lowerFromBirJson` (Mechanism B):

- `"BIR JSON file has been loaded from disk: <path>"`, `"Creating NKI function from BIR JSON: <name> and core id: <id>"`
- `"The kernel is compiled for LNC count lower than the current core_id <id> skip lowering"`
- `"NeuronCoreId must be set when the LNC count is greater than 1"`

From the helpers:

- `"COMPILER-GENERATED-zero_bias_act_<id>"` / `"COMPILER-GENERATED-memset_zero_bias_<id>"` (getActBiasTensor)
- `"COMPILER-GENERATED-id.<dtype>"`, `"=IDAffineSelect(mask,fill=1)"`, `"Emitting dynamic Identity matrix generation code, this might affect performance."`, `"You can bypass this by pass in an explicit identity matrix and use nc_matmul for transpose"` (getIdentityMatrix)
- `"Unknown KernelSource"` (KernelSource2string FATAL; brewer.py)

## Adversarial self-verification ceiling

The five strongest claims were re-checked against the binary this session:

- **Two provision mechanisms / disjoint callees** — CONFIRMED. The `lowerKernelInst @0xf0b610` decompile shows the `std::string::compare` → `lowerFromBirJson` split, the `addNKIFunction` + `codegenLncKernel` KLR body, and the `getIsAllocated` gate; the callgraph confirms the disjoint callee sets.
- **IT54/55/56 carrier resolution** — CONFIRMED at the ctor-literal level (`Instruction(…, 54/55/56, …)` read off `0x409ee0`/`0x40a490`/`0x40a500`). The `KernelSource Beta2`/`Beta3` provenance split is CONFIRMED (`KernelSource2string @0x401c10` body + the byte-verified `+0x160 = 1`/`= 2` writes).
- **The helpers' behavior** — CONFIRMED. `getActBiasTensor`'s float32 immediate (`mov r8d, 10h` into `addSBTensor`) and `getIdentityMatrix`'s diagonal fill (`mov dword ptr [rbx+194h], 3F800000h`) + the `IDAffineSelect`/perf-warning strings were read directly off the `nki_klr_sim` disasm.
- **`getBankId`** — STRONG, not CONFIRMED at body level. The codegen-side `getBankId @0xf14ac0` has no decompilable stub in the sim (it inlines / is a libwalrus-internal accessor with no string hook); its signature, role and `addPSUMTensor(…, long bankId, …)` consumption are STRONG from the roster + the accessor (`0x3fda5a8`) cross-ref. The accessor split itself is CONFIRMED.
- **The `TranslateNKIASTToBIR` driver** — CONFIRMED. The `run @0xf0dbc0` IT56-find compare, the per-node lowering, and the collect-then-remove epilogue are decompiled.

Residual gaps (honest ceiling): the **klr-binary wire format** (`KLRFile_des`/`Contents_des` byte layout beyond hdr word 12 / content tag 4) is named only, not transcribed — a dedicated KLR-format task owns it. The exact `ArchModel` field the `+0x150` pshufd decodes is STRONG by the verifier read order, not byte-named. The `p==e` identity mask predicate is INFERRED from the `(1,128)` mask AP + the inst name (the mask was not unrolled). The `getBankId` body is the single STRONG-not-CONFIRMED claim on this page.

## Cross-references

- [6.6.1 The Three-Sink Kernel-Node Model](../nki/three-sink-kernel-model.md) — the emit side: which `codegen<Name>` builds IT54 vs IT55 vs IT56, and why production macros land in the cheap name-inlined IT54 sink.
- [7.21 KlirToBirCodegen: dispatch core & routing table](klir-codegen-dispatch.md) — the KLR walk (`codegenLncKernel → codegenStmt → codegenOperator → codegen<Op>`) Mechanism A runs; the codegen context object the helpers cache onto.
- [The Two-Pass BIR-JSON Loader](json-loader.md) — `createFromJson` / `Pass2`, the deserializer Mechanism B consumes.
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the per-generation numeric PSUM-bank-count / SBUF-byte constants the helpers' per-arch accessors read.
- Part 8 translate-nki (`walrus/`, planned) — the pass-pipeline placement of `TranslateNKIASTToBIR` and the later `InlineNKIKernel` inliner that expands the IT55 this page produces.
