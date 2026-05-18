# Function, Call & Inline Asm Codegen

This page covers the four subsystems that together translate CUDA/C++ function definitions and call sites into LLVM IR: function prolog generation, call instruction emission, inline assembly compilation, and builtin lowering. The code lives in the `0x930000`--`0x960000` address range (Path A) with a parallel copy at `0x1270000`--`0x12D0000` (Path B).

| | |
|---|---|
| **EmitFunction** | `sub_946060` (Path A) -- creates entry BB, allocapt sentinel, dispatches to prolog |
| **GenerateFunctionProlog** | `sub_938240` (16 KB) -- parameter iteration, ABI dispatch, alloca emission |
| **EmitCallExpr** | `sub_93CB50` (1,293 lines) -- type resolution, ABI classification, call emission |
| **EmitInlineAsm** | `sub_1292420` (53 KB, 2,087 lines) -- 7-phase asm template-to-IR pipeline |
| **BuiltinLowering** | `sub_12B3FD0` (103 KB, 3,409 lines) -- mega-switch over ~250 builtin IDs |
| **EmitFunctionAttrs** | `sub_12735D0` / `sub_1273F90` -- grid_constant, preserve_n, custom ABI metadata |


## Function Prolog: Entry Block Setup

Every LLVM function produced by cicc starts with the same structural skeleton: an `entry` basic block containing a sentinel instruction, a cluster of `alloca` instructions for parameters and locals, and a `return` basic block for the unified exit path. The outer driver `EmitFunction` (`sub_946060`) builds this skeleton; the inner workhorse `GenerateFunctionProlog` (`sub_938240`) populates it with parameter handling code.

### EmitFunction -- The Outer Driver

`EmitFunction` executes a fixed 10-step initialization sequence before tail-calling into the prolog generator:

```
EmitFunction(IRGenState *S, FunctionDecl *Decl, Function *F,
             ParamList *Params, TypeInfoArray *TI, SourceLoc Loc, bool ByvalDemotion):

  1.  Resolve function type through typedef chain (kind==12 -> follow offset+160)
  2.  Call SetupFunctionMetadata(S, Decl)
  3.  Optionally set section name on F via Value::setSection
  4.  Create "entry" basic block:
        entryBB = BasicBlock::Create(S, "entry", F, nullptr)
  5.  Create the "allocapt" sentinel instruction:
        voidTy   = Type::getVoidTy(ctx)
        undef    = UndefValue::get(voidTy)
        allocapt = new BitCastInst(undef, voidTy)  // void-to-void no-op
        entryBB->getInstList().push_back(allocapt)
        allocapt->setName("allocapt")
        S->AllocaInsertPt = allocapt          // stored at IRGenState+456
  6.  Create "return" basic block:
        retBB = BasicBlock::Create(S, "return", nullptr, nullptr)
        S->ReturnBlock = retBB                // stored at IRGenState+200
  7.  Set up return value slot:
        if returnType is void:
            S->RetVal = nullptr
        elif ABI kind == 2 (sret) AND isAggregate(returnType):
            S->RetVal = F->arg_begin()        // reuse the sret pointer
        else:
            S->RetVal = CreateTmpAlloca(S, returnType, "retval")
  8.  Store alignment of return type at S+216
  9.  Initialize insertion state: S->CurrentBB = entryBB
 10.  Tail-call GenerateFunctionProlog(S, Decl, F, Params, TI, Loc, ByvalDemotion)
```

The `allocapt` sentinel is the critical mechanism. It is a dead `bitcast void undef to void` instruction that serves as an insertion anchor. When `CreateTmpAlloca` (at `sub_921D70`) is called with no explicit array size -- the common case -- it inserts the new `AllocaInst` **before** the `allocapt` marker rather than at the current builder insertion point. This ensures that all `alloca` instructions cluster at the top of the entry block regardless of where in the function body they were requested, which is a hard requirement for LLVM's `mem2reg` pass to promote them to SSA registers.

The sentinel is eventually dead-code-eliminated in a later pass since it produces no usable value.

### GenerateFunctionProlog -- Parameter Lowering

The prolog iterates four parallel data structures in lockstep:

| Cursor | Source | Stride | Termination |
|---|---|---|---|
| EDG parameter node | Linked list from `Decl` | `next` at offset +112 | `nullptr` |
| LLVM argument slot | `F->arg_begin()` | 40 bytes | `F->arg_end()` |
| Type info entry | From the ABI classifier | 40 bytes | (parallel with args) |
| Parameter index | 1-based counter | +1 | (parallel with params) |

A post-loop assertion validates that both cursors reached their end simultaneously: `"Argument mismatch in generation function prolog!"`.

#### Struct Return: The `agg.result` Convention

Before entering the parameter loop, a helper (`sub_938130`) checks whether the first argument's ABI kind equals 2 (sret). When true, the prolog names the first LLVM argument `"agg.result"` and advances the argument cursor by one slot (+40 bytes), so that subsequent parameter processing starts at the second argument. This mirrors the standard LLVM sret convention where the caller pre-allocates space for a returned struct and passes a pointer as a hidden first parameter.

#### ABI Variant Dispatch

For each parameter, the ABI variant field at `TypeInfo+12` selects one of four lowering paths:

**Variant 0/1 -- Indirect/Aggregate Pass.** The parameter arrives as a pointer to caller-allocated memory. If the type is an aggregate (struct/union/class/array -- type kinds 8--11 checked by `IsAggregateType` at `sub_91B770`), the prolog creates a local alloca named `<param>.addr`, stores the incoming argument into it, and registers the alloca in the declaration map via `EmitParamDecl`. If the type is a scalar, it goes directly to `EmitParamDecl` without an intermediate alloca.

**Variant 2 -- Direct Pass (most common).** The parameter is passed by value in a register or register pair. Two sub-paths exist:

- **Byval demotion path.** When the `ByvalDemotion` flag (parameter `a7`) is set and the parameter carries a `byval` attribute (TypeInfo+16 nonzero), the prolog consults a global name-set (`dword_4D04688`) to decide whether to create a `__val_param` temporary. If selected, it allocates a `"tmp"` alloca via `CreateTmpAlloca`, stores the argument into it, names the alloca `"__val_param" + param_name`, and falls through to `EmitParamDecl`. The `__val_param` prefix is NVIDIA-specific and marks parameters that have been demoted from byval to local copy for downstream optimization passes.

- **Normal path.** For non-byval scalars, calls `EmitParamDecl` directly. A guard validates that non-aggregate arguments are not marked indirect: `"Non-aggregate arguments passed indirectly are not supported!"`.

**Variant 3 -- Coercion.** The parameter's LLVM type does not match the source type and requires a coercion cast. For aggregates, a `"tmp"` alloca is created. For scalars, the declaration is looked up and wrapped with a bitcast. The result is forwarded to `EmitParamDecl`.

#### EmitParamDecl -- Registration

`EmitParamDecl` (`sub_9446C0`) performs the final steps for each parameter:

1. For scalar (non-aggregate, non-indirect) parameters: creates an alloca named `<param>.addr`, stores the incoming argument into it, and names the argument with the original parameter name.
2. Inserts the mapping `(EDG decl pointer -> LLVM Value*)` into a hash map with open-addressing/quadratic-probing collision resolution. A duplicate check guards against re-declaration: `"unexpected: declaration for variable already exists!"`.
3. If debug info is enabled (`dword_4D046B4`), emits debug metadata for the parameter via `sub_9433F0`.

### Naming Convention Table

| IR Value | Name Assigned |
|---|---|
| sret argument | `"agg.result"` |
| Unnamed parameter | `"temp_param"` |
| C++ this parameter | `"this"` (detected by bit 0 at EDG node offset +172) |
| Parameter alloca | `<param_name> + ".addr"` |
| Byval temp alloca | `"__val_param" + <param_name>` |
| Return value alloca | `"retval"` |
| Entry basic block | `"entry"` |
| Return basic block | `"return"` |
| Alloca sentinel | `"allocapt"` |

### CreateTmpAlloca Internals

`CreateTmpAlloca` (`sub_921D70`) computes alignment from the type size using `_BitScanReverse64` (effectively `log2(size)`), looks up or creates the pointer-to-type in the module's type system, then delegates to `CreateAllocaInst` (`sub_921B80`). The key detail: when no explicit array size is provided, the alloca is inserted at the `allocapt` marker position (`IRGenState+456+24`), not at the current builder insertion point.


## Call Codegen

Call emission (`sub_93CB50`) is a 1,293-line function that handles direct calls, indirect calls, builtins, special intrinsics, and `printf` interception. It receives the caller's codegen context, the EDG call expression node, and an optional pre-allocated destination for aggregate returns.

### Phase 1: Type Resolution

The callee operand is extracted from the call node's first operand slot (offset +72). The function resolves the callee's declaration via `sub_72B0F0`, then peels through the type chain -- stripping typedef aliases (kind 12) by following offset +160 -- until it reaches a pointer-to-function type (kind 6) wrapping a function type (kind 7). Fatal assertions guard both steps: `"Expected pointer to function!"` and `"unexpected: Callee does not have routine type!"`.

### Phase 2: Builtin Dispatch

For direct calls (opcode 20), the resolved callee declaration is checked for the builtin flag: `byte[199] & 2`. When set, the entire normal call path is bypassed. Control transfers to `sub_955A70` (or `sub_12B3FD0` on Path B), the builtin lowering mega-switch described in a later section. If the builtin returns an aggregate, the call codegen allocates an `"agg.tmp"` stack slot and emits a store of the result into it.

### Phase 3: Intrinsic Special Cases

If the callee is not a builtin but carries an intrinsic ID (`word[176] != 0`), a handful of intrinsic IDs receive special treatment:

| Intrinsic ID | Description |
|---|---|
| 10214 | Surface/texture primitive |
| 10219, 10227 | Warp-level primitives (detected via `(id - 10219) & 0xFFF7 == 0`) |
| 15752 | Special return convention intrinsic |

These dispatch to `sub_939370`, a dedicated handler that bypasses the normal ABI classification entirely.

### Phase 4: Argument Processing

Arguments are codegen'd by walking the argument linked list and calling `sub_921F50` on each expression. Results are collected into a dynamically-growing array (24 bytes per entry, managed by `sub_C8D5F0`).

When bit 1 of the call node's flags byte (offset +60) is set -- indicating variadic or reversed-evaluation convention -- arguments are first collected into a temporary linked list and then written into the array in reverse order. This preserves the C right-to-left evaluation order for variadic calls.

### Phase 5: ABI Classification

The ABI classifier (`sub_9378E0`) receives the return type, parameter types, and byval flags, and produces a calling-convention descriptor. Each parameter gets an ABI kind:

| ABI Kind | Meaning | Codegen Action |
|---|---|---|
| 0 | Direct (register) | Push value directly if scalar; alloca + store if byval aggregate |
| 1 | Indirect (pointer) | Push pointer directly (only valid for aggregates) |
| 2 | Indirect + byval | Push value directly (callee copies) |
| 3 | Coercion/expand | Multi-register split, handled by `sub_923000` |

For the return value, ABI kind 2 means sret: a hidden first parameter is prepended to the argument list, pointing to a caller-allocated `"tmp"` alloca.

### Phase 6: Callee Bitcast Folding

If the callee operand is a bitcast (byte[0] == 5), the optimizer walks back to the original function pointer and compares return types and parameter counts. If the signature matches exactly (pointer equality on type nodes, parameter-by-parameter comparison), the bitcast is folded out. This removes unnecessary `bitcast` wrappers that arise from C-style casts between compatible function pointer types.

### Phase 7: Pre-Call Hooks and printf Interception

Debug location metadata is emitted via `sub_92FD10`. Then a special case: if the call is direct (opcode 20) and the callee name is literally `"printf"`, control transfers to `sub_939F40` which performs GPU printf lowering -- converting the `printf` call into a `vprintf`-style call that writes formatted output through the GPU's printf buffer mechanism.

### Phase 8: preserve_n Operand Bundles

If the call node's `preserve_data` field (offset +64) is non-null, up to three operand bundles are attached to the call instruction:

```
preserve_data[0] >= 0  =>  "preserve_n_data"    = ConstantInt(value)
preserve_data[1] >= 0  =>  "preserve_n_control"  = ConstantInt(value)
preserve_data[2] >= 0  =>  "preserve_n_after"    = ConstantInt(value)
```

These NVPTX-specific operand bundles are register-pressure hints consumed by the instruction scheduler and register allocator. The value `-1` means "not specified" and suppresses the bundle.

### Phase 9: Call Emission and Attribute Attachment

The LLVM `CallInst` is created by `sub_921880`, which takes the callee, the argument array, return type, and the optional operand bundle. Calling-convention attributes (sret, byval, alignment) are collected by `sub_93AE30` and attached to the call. For indirect calls, the instruction is named `"call"` for readability; direct calls inherit the callee's name.

### Phase 10: Return Value Handling

| Return ABI Kind | Handling |
|---|---|
| 0 or 1 (direct scalar) | Return the `CallInst` result directly |
| 0 or 1 (direct aggregate) | Allocate `"agg.tmp"`, store the result, return the alloca |
| 2 (sret) | Return the sret pointer (aggregate) or load from it (scalar) |
| 3 (expanded/multi-register) | Call `sub_923000` to split across multiple extracts |

For indirect calls, `callalign` metadata is constructed by querying the alignment requirement of the return type and each argument type, wrapping them in an `MDTuple`, and attaching it to the call instruction. This metadata is consumed by the NVPTX backend to generate correct alignment annotations in PTX.

### Call Emission Pseudocode

```
EmitCallExpr(Result *Out, CodegenCtx *Ctx, CallNode *Call, u64 DestFlags, u32 Align):

  callee_decl = ResolveCallee(Call->operand[0])
  func_type   = PeelTypedefs(callee_decl->type)  // kind 6 -> kind 7

  // ---- Builtin fast path ----
  if Call->opcode == CALL_DIRECT  AND  callee_decl->flags[199] & 2:
      result = BuiltinLowering(Ctx, Call)
      if isAggregate(func_type->returnType):
          dest = DestFlags.ptr  OR  CreateTmpAlloca("agg.tmp")
          Store(result, dest, ComputeAlign(returnType))
          Out = {dest, INDIRECT, sizeof(returnType)}
      else:
          Out = result
      return

  // ---- Special intrinsics ----
  if callee_decl->intrinsicID in {10214, 10219, 10227, 15752}:
      return SpecialIntrinsicHandler(Out, Ctx, callee_decl->intrinsicID, Call)

  // ---- Normal call path ----
  callee_val = CodegenCallee(Ctx, Call->operand[0])
  args[]     = CodegenArguments(Ctx, Call->argList)
  if Call->flags & REVERSED_EVAL:
      Reverse(args)

  abi_desc   = ClassifyABI(func_type->returnType, paramTypes, byvalFlags)

  if abi_desc.returnIsSRet:
      sret_ptr = DestFlags.ptr  OR  CreateTmpAlloca("tmp")
      PrependArg(args, sret_ptr)

  for each (arg, abi_entry) in zip(args, abi_desc.params):
      if abi_entry.kind == DIRECT  AND  abi_entry.isByval:
          tmp = CreateAllocaForAggregate(arg)
          Store(arg, tmp)
          arg = tmp
      elif abi_entry.kind == INDIRECT:
          assert isAggregate(arg.type)

  callee_val = FoldCalleebitcast(callee_val, func_type)

  EmitDebugLoc(Ctx, Call->srcLoc)

  if Call->opcode == CALL_DIRECT  AND  callee_name == "printf":
      return PrintfExpansion(Ctx, abi_desc, args, Call->srcLoc)

  bundle = BuildPreserveNBundle(Call->preserveData)
  call_inst = EmitCall(func_type, callee_val, args, bundle)
  AttachCCAttrs(call_inst, abi_desc)

  Out = HandleReturnValue(call_inst, abi_desc, func_type->returnType)
```


## Inline Assembly Codegen

The inline asm handler (`sub_1292420`, 53 KB) translates a CUDA `__asm__()` statement into an LLVM `InlineAsm` call instruction through a strict 7-phase pipeline. A nearly-identical duplicate exists at `sub_932270` for the Path A codegen context -- same parsing logic, same constraint table, different diagnostic function pointers.

### Phase 1: Template String Parsing

The raw PTX template string from the EDG AST is scanned character-by-character into a fragment array. Each fragment (48 bytes) is either a literal text chunk (`kind=0`) or an operand substitution reference (`kind=1` with an operand index at offset +0x28).

The parser handles the CUDA-to-LLVM syntax translation:

| CUDA Syntax | LLVM IR Output | Parser Action |
|---|---|---|
| `$` (literal dollar) | `$$` | Escape doubling |
| `%%` | `%` | Literal percent |
| `%N` (operand ref) | Fragment kind=1, index=N | Multi-digit decimal parse |
| `%=` (unique ID) | `${:uid}` | LLVM unique-identifier modifier |
| `%[name]` | -- | Fatal: `"symbolic operand reference not supported!"` |
| `%cN` (modifier+operand) | Fragment kind=1, modifier=c, index=N | Alpha char + decimal parse |

For operands referencing string literal constants (the `C` constraint), the parser resolves the constant through the EDG value chain, validates the type is `array of char`, extracts each byte, escapes any `$` characters, strips the trailing NUL, and emits the entire string as a literal fragment.

### Phase 2: Template Reconstruction

The fragment array is serialized into the final LLVM inline-asm template string:

- Literal fragments: appended verbatim.
- Operand references without modifier: converted to `$N` (e.g., operand 3 becomes `$3`).
- Operand references with modifier: converted to `${N:c}` (e.g., operand 0 with modifier `h` becomes `${0:h}`).

This is where the CUDA `%N` convention is translated to LLVM's `$N` convention. Literal `%` characters in PTX (like `%tid.x`) pass through unchanged because they were never parsed as operand references.

### Phase 3: Constraint String Construction

The parser iterates the EDG operand linked list, building a comma-separated LLVM constraint string. Each EDG operand carries a constraint type-chain -- a linked list of tag bytes that map through a 61-byte global lookup table at `0x4B6DEC0` (`"@,&%#*?!Xg0123456789rhlCfmpoV><insFabcdSDRqQAtuxYyIJMKNLGHeZ~"`) to produce LLVM constraint letters.

**Output operands** (`flags & 2 != 0`):
- Pointer types: constraint prefix `"=*"` + letters (indirect output).
- Non-pointer types: constraint prefix `"="` + letters (direct output).
- Read-write operands (byte at +24 == 3): a tied input operand is generated with the output's index as the constraint, linking them as a two-address pair.

**Input operands:**
- Same tag-to-letter mapping.
- Tags 10--19 are prohibited: `"tied input/output operands not supported!"` (GCC-style matching-digit constraints are not implemented).
- Tag 23 (the `C` constraint on inputs) creates an `undef` value -- the constant's value was already inlined into the template string during Phase 1.

Special tag handling:

| Tag | Effect |
|---|---|
| 8, 9 | Sets `is_address` + `is_memory` flags; tag 9 also emits `"imr"` composite constraint |
| 0x14, 0x15, 0x16, 0x18, 0x26, 0x2A | Pointer-through types: follow type chain, set `is_address` |
| 0x19, 0x1B, 0x1C | Memory constraints |
| 23 | Remapped to tag 20 before table lookup |

### Phase 4: Clobber List

The EDG clobber linked list (at `asmInfo+144`) is iterated. Each clobber node has a tag byte selecting the clobber type:

- **Tag 1:** Memory clobber. Appends `",~{memory}"` to the constraint string.
- **Tag 58:** Named register clobber. Uses the name string from the node. Appends `",~{<name>}"`.
- **Other tags:** Looks up the register name from a global table (`off_4B6DCE0[tag]`). Appends `",~{<name>}"`.

### Phase 5: InlineAsm Object Creation

The LLVM function type for the asm is constructed based on the output count:
- Zero outputs: `void` return type.
- One output: scalar return type matching the output operand.
- Multiple outputs: anonymous struct return type.

The volatile/sideeffect flag is read from `asmInfo+128` (bit 2). A diagnostic (`0xE9F`) warns when outputs exist but the asm is not marked volatile, as this risks miscompilation.

The `InlineAsm` object is created via `InlineAsm::get(funcType, asmString, constraintString, hasSideEffects, isAlignStack=0, dialect=0)` and a `CallInst` is emitted to invoke it.

### Phase 6: Result Extraction

For single-output asm, the `CallInst` result is used directly. For multiple outputs, each result is extracted with `extractvalue` instructions:

- Results with type size <= 16 bytes: a compact `extractvalue` path.
- Results with type size > 16 bytes: a full instruction node (88 bytes) is allocated, the `extractvalue` is constructed with explicit index arrays, linked into the basic block's instruction list, and named `"asmresult"`.

Each extracted value is then stored into its output destination via `sub_12843D0`, which reads the output codegen-info records built during Phase 3.

### Phase 7: Cleanup

All temporary vectors and strings are freed: the fragment array (with per-element string cleanup), constraint strings, operand/type/destination vectors, and tied-operand tracking arrays.

### End-to-End Example

```
CUDA source:    __asm__("mov.u32 %0, %tid.x" : "=r"(result));

Phase 1 parse:  [literal("mov.u32 "), operand(idx=0), literal(", %tid.x")]
Phase 2 recon:  "mov.u32 $0, %tid.x"
Phase 3 constr: "=r"
Phase 4 clobber: ""
Phase 5 create: InlineAsm::get("mov.u32 $0, %tid.x", "=r", sideeffects=true)
                call i32 asm sideeffect "mov.u32 $0, %tid.x", "=r"()
Phase 6 extract: (single output -- use call result directly)
                 store i32 %asm_result, i32* %result.addr
```


## Builtin Lowering

The builtin lowering mega-switch (`sub_12B3FD0`, 103 KB) is one of the largest single functions in the binary. It handles ~250 builtin IDs across ~130 case labels, dispatching CUDA intrinsic functions like `__syncthreads()`, `__shfl_sync()`, and `__hmma_m16n16k16_mma_f16f16` into LLVM IR.

### Entry Logic

The function extracts the callee from the call expression, validates the builtin bit (`flags byte[199] & 2`), then looks up the builtin ID by name via `sub_12731E0`. If the ID is 0 (name not in the builtin table), execution falls through to the LLVM intrinsic fallback path at line 3154.

### Five Lowering Strategies

| Strategy | Usage (%) | Mechanism |
|---|---|---|
| **Sub-handler delegation** | 66% (~165 IDs) | Calls a specialized function for a family of builtins |
| **Intrinsic call emission** | 12% (~30 IDs) | 1:1 mapping to a single `llvm.nvvm.*` intrinsic via `sub_1285290` |
| **Inline IR generation** | 10% (~25 IDs) | Builds IR nodes directly (alloca, load, store, cast, insertvalue) |
| **Table-driven selection** | 10% (~25 IDs) | Selects intrinsic ID from a table keyed by operand type/size |
| **SM-gated conditional** | 2% (~5 IDs) | Different lowering depending on target SM version |

### Per-Category Dispatch

**Atomics and synchronization** (IDs 0xB5--0xCC, 181--204). Atomic operations delegate to `sub_12A7DA0`; fences and barriers to `sub_12AB550`. Cases 0xBA--0xBC map directly to LLVM intrinsic 6 (likely `llvm.nvvm.atomic.*`) with type-overloaded arguments. Case 0xCB is SM-gated: on SM <= 63 it emits an inline constant; on SM >= 70 it emits intrinsic 3769.

**Warp shuffle** (IDs 0x15F--0x166, 351--358). All eight variants delegate to `sub_12ABB90` parameterized by shuffle mode (0=idx, 1=up, 2=down, 3=butterfly) and sync flag (0=legacy, 1=`__shfl_sync_*`). The clamp flag distinguishes butterfly from other modes.

**Warp vote/ballot** (IDs 0x12E--0x135, 0x152--0x159, 0x18B--0x192). Three groups of 8 IDs each, all delegating to `sub_12B3540` with the builtin ID as a discriminator. This covers `__ballot_sync`, `__all_sync`, `__any_sync` across integer/float/predicate operand types.

**Surface and texture operations** (IDs 0xCF--0x113, 0x287--0x2A5, 207--275 + 647--677). The largest category at ~95 IDs (38%). Organized into pairs using two sub-handlers: `sub_12ADE80(ctx, intrinsic_base, surface_type, variant, args)` for individual load/store operations, and `sub_12AA9B0(ctx, surface_type, expr)` for combined operations. Surface types are encoded as integers (0=generic, 1=1D, 5=2D, 7=3D, 8=cubemap, 10=1D array, 11=2D array, 14=buffer). Intrinsic bases 3701/3702 are primary read/write; 3698/3699 are 2D-array variants.

The texture handler (case 0x287) is the most complex single case at ~230 lines. It walks the AST to extract the texture name string and return element type, constructs an intrinsic name as `"<texname>_<typename>"` using a type-name resolution switch (mapping integer subtypes 0--10 to strings like `"uchar"`, `"int"`, `"ulonglong"`), and emits the call. A global flag (`dword_4F06B98`) controls whether plain `char` maps to `uchar` or `schar`.

**Tensor core / WMMA** (IDs 0x16E--0x1D9, 0x2A6--0x2E8, 366--473 + 678--744). The second-largest category at ~85 IDs (34%). Three sub-handlers partition the work: `sub_12AC1A0` handles `wmma::mma_sync` with bias/scale flags `(has_bias, has_scale)` encoding four accumulator modes; `sub_12AC5F0` handles `store_matrix_sync`; `sub_12ACA80` handles `load_matrix_sync`. IDs group into triplets by matrix shape: m16n16k16, m32n8k16, m8n32k16, m16n16k8 (TF32), bf16, and fp8 (SM 89+) families.

**WGMMA** (IDs 0x2E9--0x302, 745--770). SM 90+ warpgroup MMA operations. Cases 0x2E9--0x2EE handle fence/commit/wait. Cases 0x2F1--0x2FC implement `__wgmma_mma_async` through a massive ~800-line handler that selects from a 144-entry intrinsic table spanning IDs 5304--5447. The table is indexed by a 5-dimensional grid: N-size (16/32/64/128), B-operand source (shared vs register), element type (s64 vs other), scale/negate flags, and case variant. Mode bits are packed into a single integer: `bit0=accumulate | bit1=transpose | bit2=negate-C | bit4=negate-A`.

**Memory copy** (IDs 0x199, 0x291--0x299, 409 + 657--665). Memcpy variants encode alignment directly in the builtin ID: ID 658 = align 2, ID 659 = align 4, ID 660 = align 8, ID 661 = align 16. The actual emission delegates to `sub_12897A0`. Memset operations (IDs 410, 663, 665) delegate to `sub_12A6DF0`.

**TMA bulk operations** (IDs 0x19B--0x1A0, 411--416). Cases 0x19B and 0x19C are the largest individual handlers (~300 and ~450 lines respectively) for SM 90+ tensor memory access bulk copy/scatter operations. They build operand vectors iteratively and select from intrinsic tables indexed by element count (IDs 4218--4223 for stores, 4244--4250 for loads).

### LLVM Intrinsic Fallback Path

When the builtin ID is 0, the default path (lines 3154--3407) looks up the LLVM intrinsic by name via `sub_15E2770`. If the intrinsic is type-overloaded, argument types are used to resolve the declaration. Each argument is lowered via `sub_128F980`, with type-mismatch bitcasts (opcode 47) and vector zexts (opcode 33) inserted as needed. Struct-return intrinsics are handled by iterating the return struct's fields with `extractvalue`.


## Function Attributes

CUDA function attributes are lowered through a three-stage pipeline: EDG frontend parsing, attribute emission during IR generation, and a final metadata-attachment pass.

### Stage 1: Frontend Parsing (sub_64F1A0)

The EDG parser scans the token stream for `preserve_n_data`, `preserve_n_control`, and `preserve_n_after` identifiers, parses each as an integer, and stores them in a 12-byte struct at offset +336 of the function declaration node:

```c
struct preserve_reg_info {
    int32_t preserve_n_data;     // +0, -1 = not specified
    int32_t preserve_n_control;  // +4, -1 = not specified
    int32_t preserve_n_after;    // +8, -1 = not specified
};
```

### Stage 2: Attribute Emission (sub_12735D0)

During IR generation, the attribute emitter checks declaration flags and writes attribute bundles:

- **Bit 0x20 at decl+198** (kernel function): emits `("kernel", 1)`. Then iterates the parameter array (40-byte entries); for each parameter with `byte[+33] != 0`, emits `("grid_constant", param_index)` where `param_index` is 1-based. This marks individual kernel parameters as grid-constant, enabling the backend to place them in constant memory.

- **Bit 0x04 at decl+199** (custom ABI): emits `("full_custom_abi", 0xFFFFFFFF)`.

- **Preserve-reg struct at decl+336**: for each of the three fields, if the value is >= 0, emits the corresponding attribute and then writes -1 back (consumed pattern) to prevent double-emission.

### Stage 3: Metadata Attachment (sub_1273F90)

The reader pass iterates all functions' attribute bundles and re-encodes them as LLVM named metadata:

**grid_constant.** Per-parameter type values are collected into a vector, then bundled under the MDString key `"grid_constant"` as an `MDTuple`. The downstream consumer `sub_CE8660` queries this metadata to determine aliasing/readonly semantics for kernel parameters.

**preserve_reg_abi.** The three preserve_n values are collected with their MDString keys (`"preserve_n_data"`, `"preserve_n_control"`) into a vector, then bundled under the composite key `"preserve_reg_abi"` as an `MDTuple`. The register allocator and prologue-epilogue inserter query this via `sub_314D260`.

**full_custom_abi.** Emitted as a simple `(MDString, MDNode(i32 0xFFFFFFFF))` pair. When a function has this attribute but NOT the full_custom_abi flag, the alternative `"numParams"` key records the explicit parameter count as a nested `MDTuple`.

### Final Metadata Layout

For a `__global__` kernel with grid_constant parameters and register preservation:

```llvm
!kernel_attrs = !{
  !MDString("kernel"), !MDNode(i32 1),
  !MDString("grid_constant"), !MDTuple(
    !MDNode(i32 <param1_type>), !MDNode(i32 <param2_type>), ...
  ),
  !MDString("preserve_reg_abi"), !MDTuple(
    !MDString("preserve_n_data"),    !MDNode(i32 N),
    !MDString("preserve_n_control"), !MDNode(i32 M),
    !MDString("preserve_n_after"),   !MDNode(i32 K)
  )
}
```

### Attribute Semantics

| Attribute | Meaning | Backend Effect |
|---|---|---|
| `grid_constant` | Kernel parameter is immutable across the grid | Place in constant memory; optimize loads |
| `preserve_n_data` | N data registers must be preserved across calls | Register allocator reserves R0--RN |
| `preserve_n_control` | N predicate registers to preserve | Prologue/epilogue saves predicates |
| `preserve_n_after` | N registers preserved after a call (callee-save count) | Adjusts spill/restore boundaries |
| `full_custom_abi` | Function bypasses standard CUDA calling convention | Parameter passing determined by explicit annotations |
| `numParams` | Explicit parameter count for non-full_custom_abi functions | Custom ABI parameter setup |


## Cross-Reference

| Address | Function | Role |
|---|---|---|
| `sub_946060` | EmitFunction | Creates entry BB, allocapt, return BB, dispatches to prolog |
| `sub_938240` | GenerateFunctionProlog | Iterates parameters, ABI dispatch, alloca emission |
| `sub_9446C0` | EmitParamDecl | Creates alloca+store, registers decl->Value mapping |
| `sub_921D70` | CreateTmpAlloca | Alloca creation with alignment, inserted at allocapt |
| `sub_921B80` | CreateAllocaInst | Low-level alloca IR emission |
| `sub_938130` | IsSRetReturn | Checks ABI kind == 2 |
| `sub_91B770` | IsAggregateType | Type kinds 8--11 (struct/union/class/array) |
| `sub_93CB50` | EmitCallExpr | Full call instruction emission (1,293 lines) |
| `sub_9378E0` | ClassifyABI | Return + parameter ABI classification |
| `sub_939F40` | PrintfExpansion | GPU vprintf lowering for printf calls |
| `sub_93AE30` | CollectCCAttrs | Builds sret/byval/align attribute list |
| `sub_955A70` / `sub_12B3FD0` | BuiltinLowering | Mega-switch over ~250 builtin IDs |
| `sub_1292420` / `sub_932270` | EmitInlineAsm | 7-phase asm template-to-IR pipeline |
| `sub_12735D0` | EmitFunctionAttrs | Writes attribute bundles during IR gen |
| `sub_1273F90` | ReadFunctionAttrs | Attaches LLVM named metadata from bundles |
| `sub_64F1A0` | ParsePreserveAttrs | EDG parser for preserve_n_* tokens |
