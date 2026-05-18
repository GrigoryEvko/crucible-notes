# Minor NVIDIA Passes

This page documents NVIDIA-custom passes that ride alongside the major optimization
machinery. Each pass below has its **PassInfo registration** at one address (the thin
`runRegistration()` thunk that calls `RegisterPass`) and its **algorithm body**
at another (the function pointer stored in the vtable). Earlier revisions of this
page listed only the registration thunks; the entries below trace each pass to its
actual `runOnFunction` / `runOnModule` / `runOnMachineFunction` body and distil the
recovered algorithm into C pseudocode.

## Passes with Dedicated Pages

| Pass | Page |
|---|---|
| NVVM IR Verifier | [nvvm-verify (Deep Dive)](./nvvm-verify-deep.md) |
| NVVM Intrinsic Lowering | [nvvm-intrinsic-lowering](./nvvm-intrinsic-lowering.md) |
| Dead Synchronization Elimination | [dead-sync-elimination](./dead-sync-elimination.md) |
| IV Demotion | [iv-demotion](./iv-demotion.md) |
| Struct/Aggregate Splitting | [struct-splitting](./struct-splitting.md) |
| Base Address Strength Reduction | [base-address-sr](./base-address-sr.md) |
| Common Base Elimination | [common-base-elim](./common-base-elim.md) |
| CSSA (Conventional SSA) | [cssa](./cssa.md) |
| FP128/I128 Emulation | [fp128-emulation](./fp128-emulation.md) |
| Memmove Unrolling | [memmove-unroll](./memmove-unroll.md) |

## Pass-to-Address Ledger

The table below is the authoritative map from a pass name to its algorithm
entry. The "Registration" column points at the thunk that calls `RegisterPass`;
that thunk allocates a 0x50-byte `PassInfo` whose `+72` slot holds a pointer to
a small **factory** that in turn allocates the pass instance (~160-272 bytes
depending on pass scope) and patches the LLVM `Pass` vtable. The factory's
vtable slot 19 is the algorithm entry shown in the "Algorithm" column.

| Pass name | Scope | Registration | Factory | Algorithm | Pseudo-name | Conf |
|---|---|---|---|---|---|---|
| `alloca-hoisting` | FunctionPass | `sub_21BC7D0` | `sub_21BC720` | `sub_21BC5B0` | `runOnFunction` | HIGH |
| `nvptx-image-optimizer` | FunctionPass | `sub_216E0A0`* | `sub_21BCE60` | `sub_21BD160` | `runOnFunction` | HIGH |
| `nvptx-assign-valid-global-names` | ModulePass | `sub_21BCD80` | `sub_21BC960` | `sub_21BCC50` | `runOnModule` | HIGH |
| `nvptx-replace-image-handles` | MachineFunctionPass | `sub_21DBEA0`† | (inline) | `sub_21DD1A0` | `runOnMachineFunction` | MED |
| `extra-machineinstr-printer` | MachineFunctionPass | `sub_21E9E80` | `sub_21E97F0` | (vtable-driven) | `runOnMachineFunction` | MED |
| `nvvm-intr-range` | FunctionPass | `sub_216F4B0` | `sub_216F590` | `sub_216F240` | `runOnFunction` | HIGH |
| `nvptx-proxyreg-erasure` | MachineFunctionPass | `sub_36F5B50` | `sub_36F5CC0` | (vtable slot, see below) | `runOnMachineFunction` | LOW |

> *The image-optimizer's `RegisterPass` thunk lives in the parent registration block
> (`sub_216E0A0`) since it shares the per-target init path with sibling passes; the
> factory `sub_21BCE60` is what installs the algorithm pointer.
>
> †`sub_21DBEA0` is a thirteen-byte `getPassName()` accessor returning
> `"NVPTX Replace Image Handles"`, not a `RegisterPass` thunk. The pass is constructed
> by the parent `TargetMachine` directly.

> ⚡ **QUIRK — getPassName is its own symbol**
> Several entries in earlier wiki revisions pointed at thirteen-byte leaf functions
> like `sub_21DBEA0` or `sub_21DA810` and called them "entry points". They are not
> entry points; each is a `virtual const char *Pass::getPassName() const { return
> "..."; }` stub whose body is a single `mov rax, str; ret`. In particular,
> `sub_21DA810` returns `"NVPTX optimize redundant cvta.to.local instruction"` — a
> *different* pass than `proxy-reg-erasure`, contrary to what the previous page
> claimed. Always trace the algorithm via the factory's vtable, not via the
> `getPassName()` accessor that happens to sit near the registration thunk.

---

## alloca-hoisting — Entry-Block Alloca Consolidation

| Field | Value |
|---|---|
| **Pass ID** | `alloca-hoisting` |
| **Registration** | `sub_21BC7D0` (218 B thunk) |
| **Algorithm** | `sub_21BC5B0` (177 B, 14 BBs) |
| **Scope** | FunctionPass |
| **Description string** | `"Hoisting alloca instructions in non-entry blocks to the entry block"` |
| **Pass-ID string** | `"NVPTX specific alloca hoisting"` (at `0x433c898`) |

PTX requires every stack allocation to dominate every use. After inlining or
loop transforms, allocas can survive in non-entry blocks; the verifier then
rejects the IR. This pass walks every basic block except the entry block, finds
`alloca` instructions (opcode 53 with type-tag 13), and moves each one to a
fixed insertion point in the entry block. The insertion point is computed once
at function entry by `sub_157EBA0` (the LLVM equivalent of
`BasicBlock::getFirstInsertionPt`).

```c
// runOnFunction(F) — sub_21BC5B0
bool runOnFunction(Function *F) {
    BasicBlock *entry      = F->blocks.head;       // *(F + 80)
    BasicBlock *bb         = entry->next;          //  skip entry
    Instruction *insertPt  = getFirstInsertionPt(entry);   // sub_157EBA0
    if (bb == &F->blocks.sentinel) return false;

    bool changed = false;
    do {
        for (Instruction *I = bb->insts.head; I != &bb->insts.sentinel; ) {
            Instruction *next = I->next;           // capture before reparent
            if (I->opcode == 53 /* Alloca */
                && I->type->tag == 13 /* sized type */) {
                moveBefore(I, insertPt);            // sub_15F22F0
                changed = true;
            }
            I = next;
        }
        bb = bb->next;
    } while (bb != &F->blocks.sentinel);
    return changed;
}
```

**IR shape before / after.** For a kernel that contains a conditional alloca
of `i64`:

```llvm
; before
entry:                                          ; entry block
  br label %hot
hot:
  %p = alloca i64, align 8
  store i64 %x, ptr %p

; after
entry:
  %p = alloca i64, align 8
  br label %hot
hot:
  store i64 %x, ptr %p
```

`sub_15F22F0` is a thirteen-byte leaf that performs the intrusive-list
unlink/relink and updates parent pointers in a single pass — there is no
dominator-tree recomputation and no def-use rewrite, because the alloca's
`%p` SSA name is unchanged.

Cross-refs: [Machine-Level Passes](../llvm/machine-passes.md),
[NVVM IR Generation](../pipeline/ir-generation.md).

---

## nvptx-image-optimizer — Texture / Surface Builtin Rewrite

| Field | Value |
|---|---|
| **Pass ID** | `nvptx-image-optimizer` |
| **Factory** | `sub_21BCE60` |
| **Algorithm** | `sub_21BD160` (814 B, 59 BBs) |
| **Helper** | `sub_21BCFC0` (deferred-erase queue) |
| **Scope** | FunctionPass |
| **Description string** | `"NVPTX Image Optimizer"` (at `0x433c958`) |

Replaces opaque image-handle calls with the surface/texture intrinsics that
the codegen can lower directly. The pass scans every instruction in the
function, dispatches on the NVVM intrinsic opcode, strips trivial
`addrspacecast` chains from the image-argument operand, then queries the
operand against four predicates: `__is_image_readonly`
(`sub_1C2E970`, refs `"rdoimage"`), `__is_image_writeonly` (`sub_1C2EAF0`,
refs `"wroimage"`), `__is_image_readwrite` (`sub_1C2EA30`, refs `"rdwrimage"`),
and `__is_sampler` (`sub_1C2E890`, refs `"sampler"`). When a predicate matches,
the original call is replaced with a specialised builtin and queued for
deletion.

```c
// runOnFunction(F) — sub_21BD160
bool runOnFunction(Function *F) {
    if (!isOptableTarget(F))             // sub_1636880
        return false;

    F->image_state.replace_count = 0;
    BBList *bb = F->blocks.head;
    if (bb == &F->blocks.sentinel) return false;

    do {
        for (Instruction *I = bb->insts.head;
             I != &bb->insts.sentinel;
             I = I->next) {
            if (I->opcode != 78 /* Call */) continue;
            Function  *callee   = I->callee;
            if (callee->flags & 0x1)   continue;       // user-defined
            uint32_t intr_id    = callee->intrinsic_id;
            Instruction *op0    = I->operand[0];

            // Strip opaque image-arg addrspacecast chain
            Value *img = op0;
            while (img->opcode == 86)  img = img->source;  // strip-cast loop

            switch (intr_id) {
            case 4054:  // image read intrinsic family
                if (isSampler(img))               // sub_1C2E890
                    new_call = makeReadSampled(op0); // sub_159C4F0
                else if (isReadOnly(img)         // sub_1C2EBB0
                      || isReadOnly(img))
                    new_call = makeRead(op0);     // sub_159C540
                else break;
                queueErase(F, I, new_call);       // sub_21BCFC0
                break;
            case 4055:  // image write intrinsic family
                if (isReadWrite(img) || isWriteOnly(img))
                    new_call = makeWrite(op0);    // sub_159C4F0
                else if (isReadOnly(img) || isSampler(img))
                    new_call = makeRead(op0);     // sub_159C540
                queueErase(F, I, new_call);
                break;
            case 4056:  // image probe / sampler-detect family
                /* mirrors case 4054 with predicate order swapped */
                break;
            }
        }
        bb = bb->next;
    } while (bb != &F->blocks.sentinel);

    // Bulk-delete originals queued in F->image_state.erase_list[]
    for (uint32_t i = 0; i < F->image_state.replace_count; ++i)
        eraseFromParent(F->image_state.erase_list[i]);     // sub_15F20C0
    return F->image_state.replace_count > 0;
}
```

> ⚡ **QUIRK — defer-then-bulk-erase to avoid iterator invalidation**
> The pass cannot erase the rewritten call inside the inner loop because the
> instruction list iterator would then dangle. `sub_21BCFC0` pushes the
> replaced instruction onto a SmallVector at `F->image_state.erase_list[]`
> (offset +160 from the pass state, with capacity tracking at +168/+172).
> The vector grows via the standard `SmallVector::grow` path (`sub_16CD150`,
> which produces the familiar `"SmallVector capacity overflow during
> allocation"` diagnostic on overflow). A single bulk-delete pass runs once
> the BB walk finishes — this is the same pattern used by LLVM's own
> `DeadInstructionElimination`.

The four intrinsic-id buckets (`4054`/`4055`/`4056` and the implicit fallthrough)
correspond to NVVM's `__nvvm_image_*` family. The numeric IDs change between
NVVM revisions; the values above are valid for cicc v13.0.

Cross-refs: [replace-image-handles](#replace-image-handles--surface--texture-handle-validation),
[Surface & Texture builtins](../builtins/surface-texture.md).

---

## nvptx-assign-valid-global-names — PTX Identifier Sanitisation

| Field | Value |
|---|---|
| **Pass ID** | `nvptx-assign-valid-global-names` |
| **Registration** | `sub_21BCD80` (218 B thunk) |
| **Factory** | `sub_21BC960` |
| **Algorithm** | `sub_21BCC50` (295 B, 17 BBs) |
| **Helper** | `sub_21BCA50` (507 B, name mangler) |
| **Scope** | ModulePass |
| **Description string** | `"Assign valid PTX names to globals"` (at `0x433c910`) |

PTX identifiers are restricted to `[A-Za-z_$][A-Za-z0-9_$]*`; C/C++ symbol
names emitted by EDG can contain `.`, `-`, and other characters that PTX
rejects. This pass walks every global variable *and* every alias in the
module, filters by linkage class (linkage tags 7-8), mangles the name into a
PTX-legal form, and re-installs the symbol via the module-rename helper.

```c
// runOnModule(M) — sub_21BCC50
bool runOnModule(Module *M) {
    SmallString sanitized;

    // Iterate globals (head at M->globals at +16, sentinel at +8)
    for (GlobalValue *g = M->globals.head;
         g != &M->globals.sentinel;
         g = g->next) {
        uint8_t linkage_tag = (g->linkage_byte & 0x0F) - 7;
        if (linkage_tag > 1) continue;            // only external/internal
        const char *raw = getValueName(g - 56);   // sub_1649960
        manglePtxIdentifier(&sanitized, raw);     // sub_21BCA50
        renameWithCmd(g - 56,                     // sub_164B780
                      cmd = { .ptr = &sanitized, .opcode = 260 /* setName */ });
        sanitized.dispose();
    }

    // Iterate aliases (head at M->aliases at +32, sentinel at +24)
    for (GlobalAlias *a = M->aliases.head;
         a != &M->aliases.sentinel;
         a = a->next) {
        // identical body
    }
    return true;
}

// manglePtxIdentifier(out, in) — sub_21BCA50
void manglePtxIdentifier(SmallString *out, const char *in, size_t n) {
    out->init();
    for (size_t i = 0; i < n; ++i) {
        char c = in[i];
        if ((uint8_t)(c - '-') <= 1) {            // c == '-' (45) || c == '.' (46)
            if (out->capacity_remaining() <= 2) {
                out->append("_$_", 3);            // expand: '-' / '.' -> "_$_"
            } else {
                out->buf[len + 0] = '_';
                out->buf[len + 1] = '$';          // 0x245F LE: '_', '$'
                out->buf[len + 2] = '_';
                out->len += 3;
            }
        } else {
            out->push_back(c);                    // verbatim
        }
    }
}
```

> ⚡ **QUIRK — "_$_" escape, not Itanium mangling**
> Both `-` and `.` are remapped to the literal three-byte sequence `"_$_"`.
> The constant `0x245F` (= `'$' << 8 | '_'`) appears in the decompilation as a
> 16-bit store — it is not a hash value, just the fast path that writes two
> ASCII bytes in one MOV when the SmallVector still has capacity. The third
> byte (`_`) is written separately. The escape is **not** reversible across
> distinct inputs: `foo-bar` and `foo.bar` both mangle to `foo_$_bar`.

The pass operates on the raw `Value::Name` slot (offset +56 from the
`GlobalValue` header) without consulting LLVM's `Mangler`, because PTX has no
ABI-level symbol decoration and the input is already a fully-qualified
post-EDG name.

Cross-refs: [Symbol Table](../structs/symbol-table.md), [PTX Emission](../pipeline/emission.md).

---

## nvptx-replace-image-handles — Surface / Texture Handle Validation

| Field | Value |
|---|---|
| **Pass ID** | `nvptx-replace-image-handles` |
| **Algorithm** | `sub_21DD1A0` (2716 B, 168 BBs) |
| **Scope** | MachineFunctionPass (pre-emission) |
| **Description string** | `"NVPTX Replace Image Handles"` (at `0x435dc94`) |

Runs after instruction selection. Replaces machine-level image/sampler handle
references with their PTX `.tex` / `.surf` operand forms, validating the
selected variant per opcode. The algorithm is unusually large (~169 BBs)
because it carries per-opcode validation tables for the four PTX image
instruction families.

The four diagnostic strings emitted on validation failure are the most
informative recovered evidence:

| Diagnostic | Trigger | NVVM family |
|---|---|---|
| `"Invalid image type in .tex"` (`0x435dccb` actually `.suld`)¹ | suld dim/element-type mismatch | surface load |
| `"Invalid image type in .suld"` | suld variant disagrees with image class | surface load |
| `"Invalid image type in .sust"` | sust variant disagrees with image class | surface store |
| `"Invalid image type in suq."` | suq query against non-surface handle | surface query |

¹ The diagnostic strings are addressed contiguously at `0x435dccb`...`0x435dd03`
in the order listed in `cicc_strings.json`; the first string is the `.suld`
slot despite its `.tex`-style wording.

The algorithm walks each `MachineInstr` in the function, peels off the
handle operand (which after ISel is typically an `INTRINSIC_W_CHAIN`
result tagged with the image's address space), validates the operand's
type encoding against the opcode's allowed-variant bitmap, and rewrites the
operand to the lowered PTX form. On mismatch it calls into the diagnostic
helper at the call site of the `"Invalid image type in ..."` string.

> ⚡ **QUIRK — 168 basic blocks for a one-instruction rewrite**
> The block count is dominated by the per-opcode validator: each of the
> ~40 surface/texture machine opcodes gets its own validation chain because
> PTX expresses the image's element type and dimensionality through the
> instruction mnemonic suffix rather than through operand types. Adding a
> new surface format therefore requires adding a new opcode and a new BB to
> this pass.

Cross-refs: [Surface & Texture builtins](../builtins/surface-texture.md),
[NVPTX Machine Opcodes](../reference/nvptx-opcodes.md),
[image-optimizer](#nvptx-image-optimizer--texture--surface-builtin-rewrite).

---

## extra-machineinstr-printer — Register Pressure Diagnostic

| Field | Value |
|---|---|
| **Pass ID** | `extra-machineinstr-printer` |
| **Registration** | `sub_21E9E80` (226 B thunk) |
| **Factory** | `sub_21E97F0` (615 B, 9 BBs) |
| **Co-registered** | `machine-rpa` via `sub_21EAA00` |
| **Scope** | MachineFunctionPass (diagnostic) |
| **Description string** | `"Machine Function Extra Printer"` (at `0x435f6e0`) |

A debug-only pass that prints per-MBB register pressure statistics. The
factory allocates a 0x110-byte pass instance with three pre-initialised
SmallVectors (one for each register class snapshot to be tracked) and wires
the pass into the machine pass pipeline alongside the register-pressure
analyzer it depends on.

```c
// PassInfo factory — sub_21E97F0
MachineFunctionPass *createExtraPrinter() {
    auto *P = (uint8_t *)operator new(0x110);
    P->vtable          = &ExtraMIPrinter_vtable; // &unk_49FB790
    P->machinePassKind = 3;                      // MachineFunctionPass tag
    initSentinels(P);

    // Three SmallVector<uint8_t, 8> snapshots
    for (int slot = 0; slot < 3; ++slot) {
        size_t offsets[3] = {160, 184, 208};
        uint8_t **storage = (uint8_t **)(P + offsets[slot]);
        *storage         = (uint8_t *)malloc(8);   // initial 8-byte inline
        if (!*storage) reportFatal("Allocation failed");
        (*storage)[0]    = 0;
        *(uint64_t *)(P + offsets[slot] + 8)  = 1;  // size = 1
        *(uint32_t *)(P + offsets[slot] + 16) = 8;  // capacity = 8
    }

    P->report_buf      = sub_16BA580();           // allocate diagnostic buffer
    P->vtable_alt      = &unk_4A03F50;
    return (MachineFunctionPass *)P;
}
```

The registration thunk first installs the `machine-rpa`
(`"Register pressure analysis on Machine IRs"`) dependency via
`sub_21EAA00` so that the printer can read its analysis result. Pressure
snapshots are taken at three program points per MBB (entry, mid, exit),
which is why the factory pre-allocates three SmallVectors.

> ⚡ **QUIRK — three SmallVectors with capacity 8 are mandatory**
> The factory will report `"Allocation failed"` and continue with a null
> pointer if any of the three 8-byte mallocs fails; the printer then
> dereferences null on the next access. This is a release-build pass — the
> failure mode is `SIGSEGV` rather than a graceful error. The 8-byte
> capacity is enough for typical small kernels; for larger functions the
> SmallVectors grow via the usual path.

Cross-refs: [LiveRangeCalc](../llvm/live-range-calc.md),
[Register Allocation](../llvm/register-allocation.md).

---

## nvvm-intr-range — Range Metadata for NVVM Intrinsics

| Field | Value |
|---|---|
| **Pass ID** | `nvvm-intr-range` |
| **Registration** | `sub_216F4B0` (218 B thunk) |
| **Factory** | `sub_216F590` (298 B) |
| **Algorithm** | `sub_216F240` (620 B, 40 BBs) |
| **Helper** | `sub_216F100` (`!range` attacher) |
| **Knob** | `nvvm-intr-range-sm` (registered at `ctor_359`, see `0x4329168`) |
| **Scope** | FunctionPass |
| **Description string** | `"Add !range metadata to NVVM intrinsics."` (at `0x4329130`) |

Attaches `!range` metadata to NVVM hardware-bounded intrinsics so that the
LLVM scalar optimizer (KnownBits, JumpThreading, DSE) can reason about the
return value. The exclusive upper bound is taken either from the
`__launch_bounds__` annotation (when present) or from the architectural
maximum gated by SM version. The pass operates as a simple opcode dispatch
over every call instruction in the function.

The recovered switch table maps 15 NVVM intrinsic opcodes (4286-4348, i.e.
`0x10BE`-`0x10FC`) to either a launch-bounds-driven exclusive bound or an
architectural bound:

| Opcode | Intrinsic | Bound source |
|---|---|---|
| `0x10BE` | `read.ntid.x` | `a1[42]` (function-level cached tid.x max) |
| `0x10BF` | `read.ntid.y` | `a1[43]` |
| `0x10C0` | `read.ntid.z` | `a1[44]` |
| `0x10E2` | `read.warpsize` | constant 32 (exclusive bound 32 → range 0..31) |
| `0x10E9` | `read.tid.x` | `a1[42] + 1` (launch-bounds biased) |
| `0x10EA` | `read.tid.y` | `a1[43] + 1` |
| `0x10EB` | `read.tid.z` | `a1[44] + 1` |
| `0x10EE`-`0x10F0` | `read.ctaid.{x,y,z}` | `a1[39..41] + 1` (grid dim) |
| `0x10F8`-`0x10FA` | `read.nctaid.{x,y,z}` | `a1[39..41]` (grid dim) |
| `0x10FC` | `read.laneid` | constant 32 (range 0..31) |

```c
// runOnFunction(F) — sub_216F240
bool runOnFunction(LaunchBoundsTable *LBT, Function *F) {
    BBList *bb = F->blocks.head;
    uint32_t changed = 0;

    for (; bb != &F->blocks.sentinel; bb = bb->next) {
        for (Instruction *I = bb->insts.head;
             I != &bb->insts.sentinel;
             I = I->next) {
            if (I->opcode != 78 /* Call */) continue;
            Function *callee = I->callee;
            if (callee->is_decl) continue;

            switch (callee->intrinsic_id) {
            case 0x10BE: changed |= attachRange(I, 0, LBT->ntid_x);          break;
            case 0x10BF: changed |= attachRange(I, 0, LBT->ntid_y);          break;
            case 0x10C0: changed |= attachRange(I, 0, LBT->ntid_z);          break;
            case 0x10E2: changed |= attachRange(I, 0, 32);                   break;
            case 0x10E9: changed |= attachRange(I, 1, LBT->ntid_x + 1);     break;
            case 0x10EA: changed |= attachRange(I, 1, LBT->ntid_y + 1);     break;
            case 0x10EB: changed |= attachRange(I, 1, LBT->ntid_z + 1);     break;
            case 0x10EE: changed |= attachRange(I, 1, LBT->nctaid_x + 1);   break;
            case 0x10EF: changed |= attachRange(I, 1, LBT->nctaid_y + 1);   break;
            case 0x10F0: changed |= attachRange(I, 1, LBT->nctaid_z + 1);   break;
            case 0x10F8: changed |= attachRange(I, 0, LBT->nctaid_x);       break;
            case 0x10F9: changed |= attachRange(I, 0, LBT->nctaid_y);       break;
            case 0x10FA: changed |= attachRange(I, 0, LBT->nctaid_z);       break;
            case 0x10FC: changed |= attachRange(I, 32, 33);                 break;
            }
        }
    }
    return changed;
}
```

The PassInfo factory `sub_216F590` initialises the per-function
`LaunchBoundsTable` from the `nvvm-intr-range-sm` knob. The SM gate is the
recovered comparison `dword_4FD2A20 < 0x1E` (= SM 30): for pre-SM 30 targets,
the upper bound is clamped to `0xFFFF`, whereas SM 30+ uses `0x7FFFFFFF`. The
default fallback dimensions `0x4000000400LL` decode to
`{ntid_x = 0x400 = 1024, ntid_y = 1, ...}`; the absolute max grid is encoded
in the 48-bit constant `0xFFFF0000FFFFLL`.

> ⚡ **QUIRK — launch-bounds drives a tighter range than the architectural max**
> When a kernel has `__launch_bounds__(N)` attached, the cached `ntid_x` field
> drops from `1024` to `N`. The `!range` metadata is then `[0, N)` for
> `read.tid.x`, allowing later passes to evaluate `tid < N` to constant-true and
> dead-code-eliminate the bounds check that user kernels often guard with. The
> bias `+1` in the launch-bounds-driven cases is because LLVM `!range` upper
> bounds are exclusive, while NVIDIA's internal bound is the inclusive maximum
> `tid`.

Cross-refs: [KnownBits & DemandedBits](../llvm/known-bits.md),
[Optimizer Pipeline](../pipeline/optimizer.md).

---

## nvptx-proxyreg-erasure — Post-ISel ProxyReg Elimination

| Field | Value |
|---|---|
| **Pass ID** | `nvptx-proxyreg-erasure` |
| **Registration** | `sub_36F5B50` (127 B thunk) |
| **Factory** | `sub_36F5CC0` (316 B) |
| **Scope** | MachineFunctionPass (post-RA) |
| **Description string** | `"NVPTX ProxyReg Erasure"` (at `0x451d111`) |
| **Long name** | `"NVPTX Proxy Register Instruction Erasure"` (at `0x451d0e8`) |

Removes `NVPTXISD::ProxyReg` machine instructions left by SelectionDAG when
it materialises certain calling-convention boundaries. A `ProxyReg` is a
single-source-single-destination pseudo-instruction that pins a virtual
register's allocation across an opaque boundary; after register allocation
its purpose is served and it can be replaced with a copy or deleted outright.

The factory allocates a 200-byte machine pass instance and patches the
LLVM `MachineFunctionPass` vtable at `0x4a3c198` (23 entries) — the unusual
vtable size relative to peer passes (~19 entries) reflects that this is a
post-RA pass and inherits the larger `MachineFunctionPass` interface.

```c
// PassInfo factory — sub_36F5CC0
MachineFunctionPass *createProxyRegErasure() {
    auto *P = (uint8_t *)operator new(0xC8);          // 200 bytes
    P->vtable             = &ProxyRegErasure_vtable;  // off_4A3C198
    P->id                 = 2;                        // pass-kind tag
    P->dep_table          = &unk_5041070;             // shared NVPTX deps
    initSmallVectorPair(P + 56,  P + 104);            // two SmallVectors
    initSmallVectorPair(P + 112, P + 160);
    *(float *)(P + 88)    = 1.0f;                     // 0x3F800000 — frequency threshold
    *(float *)(P + 144)   = 1.0f;
    registerListener(P, sub_BC2B00());                // observer hook
    return (MachineFunctionPass *)P;
}
```

The two `1.0f` constants (recovered as `1065353216` = `0x3F800000`) are
block-frequency thresholds — the pass biases its rewrite toward hot blocks
when deciding whether to erase or downgrade a `ProxyReg` to a `COPY`. The
two SmallVector pairs at offsets +56/+112 hold the worklist of `ProxyReg`
defs and the per-virtual-register live-range cache, respectively.

> ⚡ **QUIRK — three function pointers at +1.0f offsets**
> Two distinct floats both set to `1.0f` at offsets `+88` and `+144` flag a
> per-class threshold (predicate / general-purpose). LLVM upstream usually
> stores such constants in `cl::opt` knobs, but cicc bakes them into the pass
> instance — there is no recovered command-line knob to tune them, so they
> behave as compile-time constants from the user's perspective.

The actual erasure pass body lives behind the vtable's `runOnMachineFunction`
slot and was not fully decompiled in the current sweep; the visible structure
suggests an LLVM-standard `for-each-MBB / for-each-MI / erase-if(opcode ==
NVPTXISD::ProxyReg)` loop with a worklist drained after the main scan.

> ⚡ **QUIRK — confused with cvta-elimination in earlier wiki revisions**
> The thirteen-byte leaf at `sub_21DA810` returns
> `"NVPTX optimize redundant cvta.to.local instruction"` — that is the
> `getPassName()` accessor of a *different* pass (cvta.to.local
> redundancy elimination), whose algorithm body is `sub_21DA950` (1846 B,
> 54 BBs). Earlier wiki entries mapped `sub_21DA810` to `proxy-reg-erasure`;
> this was wrong. The cvta-redundancy pass is documented as part of the
> address-space-lowering pipeline; the proxy-reg pass lives in its own
> registration block at `sub_36F5B50`.

Cross-refs: [Machine-Level Passes](../llvm/machine-passes.md),
[Register Allocation](../llvm/register-allocation.md).

---

## Other Passes Documented Elsewhere

These NVPTX-backend passes ride alongside the seven above but have primary
documentation on other pages:

| Pass | Entry | Primary Page |
|---|---|---|
| `nvptx-peephole` | `sub_21DB090` | [NVVM Peephole](./nvvm-peephole.md) |
| `nvvm-pretreat` | `PretreatPass` (New PM slot 128) | [Optimizer Pipeline](../pipeline/optimizer.md) |
| NLO (Simplify Live Output) | `sub_1CE10B0`, `sub_1CDC1F0` | [Rematerialization](./rematerialization.md) |
| Prolog/Epilog | `sub_21DB5F0` | [Machine-Level Passes](../llvm/machine-passes.md), [PrologEpilogInserter](../llvm/prolog-epilog.md) |
| LDG Transform | `sub_21F2780` (`ldgxform`) | [Machine-Level Passes](../llvm/machine-passes.md), [Code Generation](../pipeline/codegen.md) |
| Machine Mem2Reg | `sub_21F9920` (`nvptx-mem2reg`) | [Machine-Level Passes](../llvm/machine-passes.md), [Code Generation](../pipeline/codegen.md) |
| GenericToNVVM | `sub_215DC20` | [PTX Emission](../pipeline/emission.md#generictonvvm--sub_215dc20) |
| cvta.to.local Redundancy Elim. | `sub_21DA950` (1846 B) | (unattached; pseudo-name `"NVPTX optimize redundant cvta.to.local instruction"`) |
