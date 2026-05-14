# LowerStructArgs: Bare-Pointer ABI Translation

## Abstract

LowerStructArgs rewrites by-value struct parameters into parameter-space pointers. Every aggregate load of the original SSA argument becomes a scalar `LDPARAM` at the field's computed offset, and the use graph gets rewired so downstream instructions consume the loaded scalars instead of the original struct value. The pass lands late enough that LLVM-level struct shape is still visible but early enough that instruction selection sees only pointer-and-scalar traffic.

NVPTX cannot pass an aggregate object directly through register classes the way the IR-level ABI pretends it can. Every by-value struct parameter has to be materialized as a pointer into parameter space, loaded piecewise, and address-space-cast wherever the original value flowed into a generic-pointer or global-pointer consumer.

## Rewrite Shape

The pass operates at the LLVM-IR / SelectionDAG MachineIR boundary. For an arbitrary by-value struct parameter `%s`, the shape it consumes and the shape it produces are:

```text
input  : define ptx_kernel void @k(%S byval(%S) %s) {
           %x = getelementptr %S, ptr %s, i32 0, i32 1
           %v = load i32, ptr %x
           ...
         }

output : define ptx_kernel void @k(ptr addrspace(101) %s.param) {
           %v = call i32 @llvm.nvvm.ldparam.i32(ptr addrspace(101) %s.param, i64 4)
           %v.gen = call i32 @llvm.nvvm.cvt.generic.to.as(i32 %v, i32 ...)
           ...
         }
```

The `byval` aggregate parameter becomes a parameter-space (`addrspace(101)`) pointer; every load that read a struct field is replaced by `LDPARAM` (MI opcode `101`) reading from the parameter pointer at the field's offset, followed by `CVT_GENERIC_TO_AS` (opcode `80`) when the loaded scalar still flowed into a typed pointer consumer.

## Algorithm

The pass body is a function-local rewriter. It seeds a work list from every by-value struct argument of the current function, drains the work list depth-first, and emits replacement MIs at each use site. Each work item carries the original SSA value, its computed replacement, and the specific use edge that needs rewiring — not just the user instruction. GEP chains feeding several downstream loads share a user but not a use, and each use needs an independent rewrite to keep SSA def-use chains consistent for the passes downstream.

```c
typedef struct WorkItem {
    Value *defining;     // original SSA value being rewritten
    Value *replacement;  // new value: loaded scalar, parameter-space pointer, or cast
    Use   *use_edge;     // the specific use site to rewire
} WorkItem;

LogicalResult lower_struct_args(Function *fn) {
    if (!opt_byval_enabled) return success();  // shared flag, see below

    WorkList<WorkItem> wl = seed_from_byval_args(fn);

    while (!wl.empty()) {
        WorkItem item = wl.pop();
        Instruction *user = cast<Instruction>(item.use_edge->getUser());

        switch (user->getOpcode()) {
            case GEP:    rewrite_gep(user, item);     push_uses(wl, user); break;
            case Load:   rewrite_load(user, item);                          break;
            case Store:  rewrite_store(user, item);                         break;
            case Call:   rewrite_call_arg(user, item);                      break;
            default:     emit_diagnostic(user);                             return failure();
        }
    }
    return success();
}
```

GEPs are the only opcode that re-seeds the work list: a GEP of the by-value struct produces a new pointer whose own uses must be rewritten, so the walker descends into them. Loads, stores, and calls terminate the rewrite — the materializer emits the `LDPARAM` + `CVT_GENERIC_TO_AS` pair (or, for calls and stores, the appropriate address-cast variant), and the original instruction is either replaced or has its operand swapped to the loaded scalar.

Unknown opcodes bail with a diagnostic rather than silently leaving half-rewritten def-use chains for later passes to trip over.

## Materializer

The materializer is the single entry point for emitting replacement MIs. Given a work item, it computes the offset of the requested scalar inside the original struct (using the LLVM `DataLayout` for the active target), emits an `LDPARAM` reading from the rewritten parameter pointer at that offset, then emits a `CVT_GENERIC_TO_AS` to coerce the loaded value back to the original SSA type:

```c
Value *materialize_field_load(IRBuilder *b, Value *param_ptr,
                              StructType *struct_ty, unsigned field_idx) {
    uint64_t off = layout.struct_field_offset(struct_ty, field_idx);
    Type *field_ty = struct_ty->getElementType(field_idx);

    Value *ld = b->createCall(intrinsic_ldparam(field_ty),
                              {param_ptr, b->getInt64(off)});
    Value *cv = b->createCall(intrinsic_cvt_generic_to_as(field_ty),
                              {ld, b->getInt32(/*target_as=*/0)});
    return cv;
}
```

Order matters: the cast consumes the load's result, and the load consumes the parameter pointer rather than the original aggregate pointer, so the rewrite naturally severs the use graph from the original by-value argument.

## MI Opcodes

Four machine-instruction opcodes participate in the rewrite. The materializer picks among them based on the original use's address space and what the consumer expects.

| MI opcode | Mnemonic | When emitted |
|---|---|---|
| 49 | `CVT_PARAM_TO_GENERIC` | Cast a `.param` pointer to a generic pointer for a downstream generic-space use. |
| 50 | `CVT_PARAM_TO_GLOBAL` | Cast a `.param` pointer directly to a global-space pointer. |
| 80 | `CVT_GENERIC_TO_AS` | Coerce a loaded scalar back to the original SSA pointer type. |
| 101 | `LDPARAM` | Load a scalar from `.param` space at a computed offset from the parameter pointer. |

Opcode `101` always precedes opcode `80` in the materialized sequence: read the scalar out of parameter space first, then cast it to whatever pointer flavor the original SSA value carried. Opcodes `49` and `50` fire only on the address-cast path, where the original by-value struct's address itself flowed into a generic or global consumer rather than being loaded through. The cast-only path is documented separately below.

## Worked Example: Field-Level Rewrite

Take the struct

```text
%S = type {f64, i8, [4 x i32]}
```

On the standard NVPTX target the `DataLayout` places `f64` at offset 0, `i8` at offset 8, three padding bytes at offsets 9–11, and `[4 x i32]` at offset 12. Total struct size is 28 bytes, alignment 8.

Input function:

```text
define ptx_kernel void @k(%S byval(%S) align 8 %s) {
entry:
  %p_f = getelementptr %S, ptr %s, i32 0, i32 0
  %f   = load double, ptr %p_f
  %p_b = getelementptr %S, ptr %s, i32 0, i32 1
  %b   = load i8,     ptr %p_b
  %p_a = getelementptr %S, ptr %s, i32 0, i32 2, i32 3
  %a3  = load i32,    ptr %p_a
  ...
}
```

The rewriter seeds a work list with the byval argument `%s` and walks each user GEP. For each GEP it computes the field offset from the `DataLayout`, then for each downstream `load` of that pointer it emits the `LDPARAM` / `CVT_GENERIC_TO_AS` pair.

Output function:

```text
define ptx_kernel void @k(ptr addrspace(101) align 8 %s.param) {
entry:
  %f   = call double @llvm.nvvm.ldparam.f64(ptr addrspace(101) %s.param, i64 0)
  %b   = call i8     @llvm.nvvm.ldparam.i8 (ptr addrspace(101) %s.param, i64 8)
  %a3  = call i32    @llvm.nvvm.ldparam.i32(ptr addrspace(101) %s.param, i64 24)
  ...
}
```

The `i8` at offset 8 still keeps the same 8-byte offset; the three padding bytes that preserved `[4 x i32]` alignment are never named because nothing in the original IR referenced them. Field 2, element 3 of `[4 x i32]` lands at offset `12 + 3·4 = 24`. The struct's natural alignment (8) survives onto the `.param` pointer so the loads can use the wide `LDPARAM` variants without per-field alignment fix-ups.

## Worked Example: Cast-Only Fast Path

When no field of the byval struct is ever loaded — only the struct's address flows out, typically into a callee that expects a generic or global pointer — the materializer skips field-level rewriting entirely. A single `addrspacecast` from parameter space to the consumer's expected space replaces the byval indirection.

Input function: the byval address flows directly into a generic-pointer callee.

```text
declare void @consume(ptr %p)

define ptx_kernel void @k(%S byval(%S) align 8 %s) {
entry:
  call void @consume(ptr %s)
  ret void
}
```

The walker visits the single call-site use of `%s` and notes that the consumer takes a generic (`addrspace(0)`) pointer. Rather than materializing a scalar load chain, the materializer emits a `CVT_PARAM_TO_GENERIC` (opcode `49`) at the call site and rewires the operand:

```text
define ptx_kernel void @k(ptr addrspace(101) align 8 %s.param) {
entry:
  %s.gen = call ptr @llvm.nvvm.cvt.param.to.generic(ptr addrspace(101) %s.param)
  call void @consume(ptr %s.gen)
  ret void
}
```

If `@consume` had taken a `ptr addrspace(1)` argument instead, the materializer would emit `CVT_PARAM_TO_GLOBAL` (opcode `50`) — the parametric-to-global cast — and feed that to the call. Either way the entire body of `lower_struct_args` collapses to a single address-space cast: no GEP rewriting, no per-field loads, no padding arithmetic. This is the cheapest shape the pass can produce and the one the rewriter actively prefers when the use graph permits.

## Nested Aggregates

Nested aggregates use the same materializer with one extra step. A GEP of the form `getelementptr %Outer, ptr %s, i32 0, i32 i, i32 j, ...` is folded to a single byte offset by composing per-level `DataLayout::getElementOffset` queries from outermost to innermost:

```c
uint64_t composite_offset(StructType *outer, ArrayRef<unsigned> path) {
    uint64_t off = 0;
    Type *t = outer;
    for (unsigned idx : path) {
        if (auto *st = dyn_cast<StructType>(t)) {
            off += layout.struct_field_offset(st, idx);
            t    = st->getElementType(idx);
        } else if (auto *at = dyn_cast<ArrayType>(t)) {
            off += idx * layout.size_of(at->getElementType());
            t    = at->getElementType();
        }
    }
    return off;
}
```

The recursion is purely on the type, never on the runtime SSA values: every level of nesting collapses to a single integer offset added to the base of the parameter-space pointer. Per-field alignment is whatever the `DataLayout` says for the leaf type, since the original byval struct's alignment is at least the maximum field alignment by construction.

## Shared Enable Flag

The pass is gated by a single boolean (`opt-byval` in `cl::opt` terms) that `MemorySpaceOpt` consults at the same offset in the same `.bss` slot. Both passes have to see the same value, and the reason is concrete:

- When the flag is `1`, this pass rewrites byval struct arguments to parameter-space pointers plus scalar `LDPARAM` loads. `MemorySpaceOpt` then seeds its address-space lattice on those parameter-space pointers, folds the resulting `CVT_PARAM_TO_GENERIC` / `CVT_PARAM_TO_GLOBAL` casts, and lets the verifier see a clean parameter-space-aware ABI.
- When the flag is `0`, this pass returns immediately and the byval calling convention is preserved verbatim. `MemorySpaceOpt` then has to treat byval arguments as generic and refrain from folding the casts.

A mismatched configuration — this pass disabled but `MemorySpaceOpt` still seeding `AS_PARAM` — produces parameter-space pointers `MemorySpaceOpt` cannot classify because the rewrite never ran. The `NVVMIRVerifier` rejects the function later with a "pointer-to-local-or-generic launch argument" diagnostic, and the failure surface is far from the actual misconfiguration. Both passes therefore read the same byte and a reimplementation must keep them in lockstep.

## Cross-References

[Memory-Space Optimization and Restrict](memory-space-opt-and-process-restrict.md) consumes the parameter-space pointers and `CVT_PARAM_TO_*` casts this pass emits. [NVVM IR Verifier](nvvm-ir-verifier.md) accumulates parameter-space byte counts against the per-SM ceiling using the byval-aware parameter list this pass leaves behind. [Modulo Scheduler and Rau-Style Placement](../scheduler/modulo-scheduler-and-rau.md) is the eventual consumer of the `LDPARAM` MIs in TileAS loops.
