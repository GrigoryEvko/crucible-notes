# NVPTX Target Lowering - Calls and Arguments

## Abstract

The NVPTX `SelectionDAG` target-lowering layer is the bridge between
ordinary LLVM function semantics and the PTX `.param` ABI. It converts LLVM
IR calls, formal arguments, return values, custom loads, and atomic
operations into NVPTX-specific DAG nodes.

The contract is param-space discipline. Call arguments and returns never
travel as ordinary memory traffic. Each one gets a generated param symbol,
breaks into ABI-legal value parts, threads through explicit NVPTX call
envelope nodes (`DeclareRetParam`, `ParamCallStart`, `ParamCallEnd`), and
reassembles after the call. Kernel by-value grid-constant arguments take a
fast path that preserves their param-address-space identity through
legalization. Custom DAG opcodes for vector memory, vector atomics, and
scalar floating remainders share one dispatcher so unhandled cases fall
back cleanly to LLVM's generic legalizer.

## Responsibilities

This lowering family does four jobs:

| Area | Responsibility |
|---|---|
| Formal arguments | Convert incoming LLVM function parameters into `LOAD_PARAM`, `MoveParam`, or proxy nodes. |
| Calls | Build the `DeclareRetParam`, `ParamCallStart`, argument materialization, callee target, result extraction, and `ParamCallEnd` sequence. |
| Custom operations | Handle target-marked custom opcodes such as vector loads, vector atomics, and NVPTX-specific splats. |
| Atomics | Lower scalar and vector atomic-RMW families into NVPTX atomic DAG nodes with explicit chain bundling. |

Everything downstream assumes this layer has already made ABI details explicit. Botch param naming, byval handling, or chain construction and the emitted PTX still prints — but it will not match tileiras behavior.

## NVPTXTargetLowering Vtable Bank

The `NVPTXTargetLowering` instance carries a 21-slot LLVM `TargetLowering` vtable in `.data.rel.ro`. Most slots inherit from the abstract base class. The NVPTX backend overrides eight, four of which carry the codegen-shaping methods this page documents. The vtable bank sits at a fixed `.data.rel.ro` address that this report references as `&vt_NVPTXTargetLowering`; the exact offset shifts across builds, but slot order is stable because LLVM publishes a versioned `TargetLowering` ABI.

| Slot | Method | Identity in this build | Role |
|---:|---|---|---|
| 0  | typeinfo helper | RTTI pointer | Standard Itanium-ABI `_ZTI...` slot. |
| 1  | dtor (delete) | inherited | Virtual destructor, deletes through the base pointer. |
| 2  | dtor (no delete) | inherited | Virtual destructor variant that leaves storage alone. |
| 3  | `LowerOperation` | `sub_1A7C310` via shim `sub_1A7FB60` | 79-case DAG dispatch for `BUILD_VECTOR` remap, vector LOAD, scalar floating-remainder fallback, and atomic families. |
| 4  | `LowerFormalArguments` | `sub_1A77460` | Walks the IR argument list, builds `_param_<N>` symbols, and emits `LOAD_PARAM`, `MoveParam`, or `ProxyReg` per part. |
| 5  | `LowerCall` | `sub_1A72EF0` | Builds the `DeclareRetParam`, `ParamCallStart`, argument materialization, callee target, result extraction, and `ParamCallEnd` envelope. |
| 6  | `LowerReturn` | inherited hook stub | Lowers `ret` into `RET_FLAG`-class nodes; this build leaves the slot pointing at the LLVM default because return-value marshaling already happened upstream through `StoreRetval` custom nodes. See the [Lowering Returns](#lowering-returns) section below. |
| 7  | `ReplaceNodeResults` | `sub_1A7C310` (shared body) | Post-legalisation hook for v8 / v16 splits; reuses the `LowerOperation` body with a different return path. |
| 8  | `getTargetNodeName` | NVPTX numeric-opcode table | Translates `NVPTXISD::` opcodes (such as `0x1FD` `ParamCallStart`, `0x1FE` `ParamCallEnd`, `0x317` `DeclareRetParam`) into display names for `-debug` dumps. |
| 9  | `useSoftFloat` | constant `return false` | NVPTX always lowers floating point through hardware DAG nodes; no soft-float runtime. |
| 10-20 | inherited from `TargetLowering` base | base class methods | Type-promotion hooks, register class hooks, shift-amount type, atomic legality, and other defaults the NVPTX backend does not override. |

The four overrides this page details are slots 3, 4, 5, and 7. Slot 5 dominates the bank's complexity at `~16.6 KB` of code; slot 3 is `~14.0 KB`; slot 4 is `~6.4 KB`. Remaining slots are thin policy returns or short dispatch wrappers. The dispatch shim `sub_1A7FB60` deserves a separate note: it is a public-facing `LowerOperation` trampoline that the LegalizeOp driver enters and tail-calls into `sub_1A7C310`, keeping the vtable slot pointer stable across rebuilds even when the body shifts.

The numeric-opcode table backing slot 8 carries the names this page references repeatedly: `ParamCallStart` at `0x1FD`, `ParamCallEnd` at `0x1FE`, `ParamCallStartScalar` at `0x1FF`, `PrintCallVector` at `0x200`, `CallNonRegPrototype` at `0x201`, `CallNonReg` at `0x202`, `CallSeqBegin` at `0x203`, `CallArg` at `0x204`. Declare nodes `DeclareRetParam` (`0x13D`) and `DeclareScalarParam` (`0x13E`) sit just above the call range. Ship the same opcode numbers and `-debug` parity comes for free; downstream diagnostic tooling reads the names through this slot.

## Param Symbol Naming

Tileiras names generated param symbols deterministically:

```c
StringRef make_param_symbol(unsigned arg_index, bool is_vararg) {
    if (is_vararg)
        return "_vararg";
    return format("_param_%u", arg_index);
}
```

The same namer is used by formal-argument lowering and call lowering, so caller and callee agree on declarations such as:

```text
.param .align A .b8 <function>_param_<N>[SIZE]
```

This is a behavioral contract, not just a printing convention. Later param loads, stores, and call-sequence nodes refer to these names.

## Lowering Formal Arguments

Formal-argument lowering walks the LLVM function's argument list, computes the legal register parts for each argument type, creates the param symbol, and emits one of two shapes:

- Non-kernel by-value arguments are represented with `MoveParam` or proxy nodes because their value must be copied through ordinary param space.
- Kernel grid-constant by-value arguments can load directly from the param address space, preserving the special kernel argument representation.

```c
void lower_formal_arguments(Function *fn, MachineFunctionInfo *mfi, DAG *dag) {
    for (unsigned i = 0; i < fn->arg_count; ++i) {
        Argument *arg = fn->args[i];
        ValueTypeParts parts = compute_value_parts(arg->type, fn->calling_conv);
        StringRef name = intern_param_name(mfi, make_param_symbol(i, false));

        if (arg->has_byval && !is_kernel(fn)) {
            SDValue moved = emit_move_param(dag, name, parts, arg->type);
            bind_argument_value(arg, moved);
            continue;
        }

        if (is_kernel_grid_constant_byval(arg, fn)) {
            SDValue loaded = emit_load_param_addrspace_101(dag, name, parts);
            mark_preserve_param_address_space(loaded);
            bind_argument_value(arg, loaded);
            continue;
        }

        SDValue loaded = emit_load_param(dag, name, parts);
        bind_argument_value(arg, loaded);
    }
}
```

The address-space preservation flag is essential for grid-constant byval arguments. Drop it and later DAG combines promote the value back to a generic pointer, erasing the fact that the source was kernel param memory.

## Lowering Calls

Call lowering builds an explicit NVPTX call envelope. The path starts by reserving a per-call unique ID, then folds that ID into generated param symbols so multiple calls in the same function cannot collide.

The call lowering path has six logical stages:

1. Create the entry chain and return-param declaration.
2. Classify outgoing arguments.
3. Resolve the callee target.
4. Materialize and store each outgoing argument.
5. Extract return values from call-result nodes.
6. Close the call sequence.

```c
SDValue lower_call(CallLoweringInfo *cli, DAG *dag, MachineFunctionInfo *mfi) {
    unsigned call_id = ++mfi->next_call_id;
    Chain chain = dag_entry_token(dag);

    ReturnParam ret = declare_return_param(dag, cli->result_types, call_id);
    chain = emit_param_call_start(dag, chain, call_id);

    CalleeTarget target = resolve_callee(cli);
    for (unsigned i = 0; i < cli->out_count; ++i) {
        OutArg *arg = &cli->outs[i];
        ParamSymbol sym = make_call_param_symbol(cli, call_id, i);

        if (is_kernel_grid_constant_byval_outarg(cli, arg)) {
            chain = emit_direct_grid_constant_param(dag, chain, arg, sym);
        } else if (arg->is_byval) {
            chain = emit_byval_param_copy(dag, chain, arg, sym);
        } else {
            chain = emit_scalar_or_vector_param_store(dag, chain, arg, sym);
        }
    }

    chain = emit_callee_target(dag, chain, target);
    SDValue result = collect_call_results(dag, chain, ret, cli->result_types);
    emit_param_call_end(dag, chain, call_id);
    return result;
}
```

## Byval and Grid-Constant State Machine

The byval path is governed by four facts:

| Fact | Meaning |
|---|---|
| `K` | The caller/callee context is a kernel entry. |
| `B` | The argument has `byval` semantics. |
| `G` | The argument carries the grid-constant annotation. |
| `D` | The call resolves to a direct `Function`. |

The decision table is:

| Condition | Lowering action |
|---|---|
| `K && B && G` | Load directly from param address space. This is the fast path for kernel grid constants. |
| `K && B && !G` | Materialize through the ordinary lowered-args path. |
| `!K && B` | Use a proxy or move-param sequence for device-function byval. |
| `!B && D` | Emit direct callee prototype and direct call target nodes. |
| `!B && !D` | Build a synthetic callable wrapper and mark it as an NVPTX libcall callee. |

The synthetic wrapper case adds the function attribute:

```text
nvptx-libcall-callee = "true"
```

The marker is NVIDIA-specific and lets later passes recognize indirect-call wrappers without re-deriving their origin from the DAG.

```c
CalleeTarget resolve_callee(CallLoweringInfo *cli) {
    if (cli->called_function != NULL)
        return direct_callee(cli->called_function);

    if (is_global_address(cli->callee_value))
        return external_symbol_callee(cli->callee_value);

    Function *wrapper = build_libcall_wrapper(cli->callee_value);
    add_function_attribute(wrapper, "nvptx-libcall-callee", "true");
    return callable_wrapper(wrapper);
}
```

## Value Part Scheduling

Aggregate and vector arguments are broken into legal machine value types before they are stored into param space. The helper logic is equivalent to:

```c
void store_outgoing_argument(DAG *dag,
                             Chain *chain,
                             OutArg *arg,
                             ParamSymbol sym) {
    ValueTypeParts parts = compute_value_parts(arg->type, arg->calling_conv);
    PartSchedule sched = schedule_value_parts(parts);

    for (unsigned i = 0; i < sched.count; ++i) {
        SDValue piece = extract_argument_piece(dag, arg->value, sched.part[i]);
        *chain = emit_store_param(dag, *chain, sym, sched.part[i], piece);
    }
}
```

Part scheduling is why the lowering path must know ABI size and alignment. By the time PTX sees a source-level aggregate, it is no longer a single call operand.

## Lowering Returns

Vtable slot 6 (`LowerReturn`) points at the inherited base-class stub in this build because the
NVPTX return ABI does not need a custom DAG shape: every return value has already been routed
through `StoreRetval`-class custom nodes (numeric `0xDA` and friends, dispatched from the load/store
vector selector). The base hook merely emits a `RET_FLAG` chain node that closes the function.

```c
SDValue lower_return(ReturnLoweringInfo *rli, DAG *dag) {
    Chain chain = rli->chain;
    for (unsigned i = 0; i < rli->ret_count; ++i) {
        RetVal *ret = &rli->rets[i];
        ValueTypeParts parts = compute_value_parts(ret->type, rli->calling_conv);
        for (unsigned j = 0; j < parts.count; ++j) {
            SDValue piece = extract_return_piece(dag, ret->value, parts.part[j]);
            chain = emit_store_retval(dag, chain, parts.part[j], piece);
        }
    }
    return emit_ret_flag(dag, chain);
}
```

Reimplementations that override this slot must keep `StoreRetval` materialization upstream of the chain close. Pushing return-value materialization into `LowerReturn` itself collapses the chain into a single node and breaks the value-part scheduling the rest of the lowering layer relies on.

## Custom Operation Lowering

The custom-operation dispatcher takes target-specific cases and lets LLVM's generic legalizer take everything else. The relevant classes:

| Operation class | Lowering behavior |
|---|---|
| Vector load/store | Rewrite into NVPTX vector load/store or splat DAG nodes when the target supports the shape. |
| `INSERT_VECTOR_ELT`, `EXTRACT_VECTOR_ELT`, `BUILD_VECTOR`, `SCALAR_TO_VECTOR` | Rebuild as NVPTX splat or lane-extract nodes. |
| Scalar floating remainder fallback | Materialize through param-load and element rebuild nodes. |
| Scalar atomics | Lower into NVPTX atomic nodes and chain bundles. |
| Vector atomics | Require a sufficiently new SM target; otherwise emit a fatal unsupported-architecture diagnostic. |

The dispatcher returns "not handled" for gaps on purpose: that preserves LLVM's ordinary legalization behavior for non-NVIDIA cases.

```c
bool lower_operation(SDNode *node, DAG *dag, SmallVector<SDValue> *results) {
    switch (node->opcode) {
    case ISD_LOAD:
        return lower_vector_or_param_load(node, dag, results);

    case ISD_INSERT_VECTOR_ELT:
    case ISD_EXTRACT_VECTOR_ELT:
    case ISD_BUILD_VECTOR:
    case ISD_SCALAR_TO_VECTOR:
        return lower_vector_lane_op(node, dag, results);

    case ISD_ATOMIC_LOAD_ADD:
    case ISD_ATOMIC_LOAD_AND:
    case ISD_ATOMIC_LOAD_OR:
    case ISD_ATOMIC_LOAD_XOR:
    case ISD_ATOMIC_CMP_SWAP:
        return lower_atomic(node, dag, results);

    default:
        return false;
    }
}
```

## Atomic-RMW Lowering

Atomic lowering is split by operation family. CAS-like and load-only operations emit an atomic compare/swap skeleton. Arithmetic RMW operations emit one per-part arithmetic atomic and bundle the result chain.

```c
bool lower_atomic(SDNode *node, DAG *dag, SmallVector<SDValue> *results) {
    AtomicKind kind = classify_atomic(node->opcode);

    if (kind.is_vector && !subtarget_supports_vector_atomics(dag->subtarget))
        fatal("vector atomics not supported on this architecture!");

    ValueTypeParts parts = compute_value_parts(node->value_type, node->calling_conv);
    Chain chain = node->chain;

    for (unsigned i = 0; i < parts.count; ++i) {
        AtomicPart part = extract_atomic_part(node, parts.part[i]);

        if (kind.uses_compare_exchange) {
            chain = emit_atomic_compare_and_swap(dag, chain, part, kind.signedness);
        } else {
            chain = emit_atomic_rmw(dag, chain, part, kind.opcode, kind.signedness);
        }
    }

    results->push_back(bundle_atomic_chain(dag, chain));
    return true;
}
```

Signedness does not change the overall DAG shape. It threads into final instruction selection so the backend picks signed or unsigned PTX mnemonics — `atom.global.min.s32` versus `atom.global.min.u32`.

## Reimplementation Notes

The high-risk compatibility points are:

- Use deterministic param symbol names for both formal and call lowering.
- Preserve kernel grid-constant byval values in param address space.
- Mark synthetic indirect-call wrappers with `nvptx-libcall-callee = "true"`.
- Break aggregates and vectors into ABI-legal value parts before storing into param space.
- Keep call sequences explicitly chained: start, stores, callee target, result extraction, end.
- Return "not handled" from custom lowering for opcode gaps so generic legalization still runs.
- Gate vector atomics on the target architecture and produce a hard diagnostic when unsupported.
