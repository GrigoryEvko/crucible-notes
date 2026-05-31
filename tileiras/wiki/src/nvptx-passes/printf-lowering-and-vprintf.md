# Printf Lowering and the vprintf ABI

## Abstract

VprintfLowering rewrites every CUDA-side `printf(...)` call into the device-runtime intrinsic `vprintf(fmt, buffer)`. The format string stays a constant-address-space pointer, the variadic tail packs into a contiguous per-thread local buffer, and the high-level call becomes a direct call to the runtime symbol. The pass is a flat scan: visit each `call printf(...)`, dispatch on a single op-tag byte attached to the call's argument-packing block, and emit the lowered form for that tag. No inter-procedural analysis, no varargs reasoning beyond what the tag already encodes.

## Input and Output Shape

The pass consumes one IR opcode and emits one runtime call plus optional packing ops. The shape of the rewrite, for the varargs tag, is:

```text
input  : %r = call @printf(%fmt, %a, %b, %c, ...)            ; fmt in addrspace(4)
output : %buf = alloca %vprintfBuffer.local : [N x i8]
         store %a, %buf+off_a
         store %b, %buf+off_b
         store %c, %buf+off_c
         %r   = call @vprintf(%fmt, %buf)                    ; i32 result
```

For the bare-format tag the `alloca` and stores are absent and the buffer argument is `nullptr`. For the pre-packed tag the caller has already produced `%buf` and the pass forwards it verbatim.

## Rewriter Dispatch

The rewriter walks every `call printf(...)` in the current function. For each call it reads the op-tag byte at offset 0 of the call's argument-packing block and dispatches on the value. Three tag bytes pass; anything else triggers a hard diagnostic.

| Tag | Form | Meaning |
|---|---|---|
| 40 | varargs | Standard `printf(fmt, a, b, c, ...)`. Pack the args into a local buffer. |
| 34 | bare format | `printf(fmt)` with no variadic args. Skip packing; pass `nullptr` as buffer. |
| 85 | pre-packed buffer | Caller already packed args into a buffer; forward it. Used by CUB / Thrust internals. |

Any other tag emits `"unsupported printf form (op-tag = N)"`, with the decimal tag value substituted for `N`. The string is emitted verbatim with no localization.

## Buffer Allocation

Tag 40 emits a single `alloca` in the function's entry block sized to the sum of the packed-arg sizes. The allocation is named `%vprintfBuffer.local`, and that name is the canonical fingerprint for vprintf-lowered functions across every CUDA version — stable, deterministic, and untouched by later NVPTX passes. Tag 34 skips the allocation entirely and feeds `nullptr` as the buffer argument. Tag 85 forwards the caller-supplied pointer and allocates nothing.

```c
LogicalResult lower_printf(CallInst *call) {
    uint8_t tag = call->arg_packing_block()[0];
    switch (tag) {
        case 40: {
            Value *buf = alloca_packing_buffer(call);   // %vprintfBuffer.local = alloca [N x i8]
            pack_args_into(buf, call->args_from(1));
            emit_vprintf(call->getArg(0), buf);
            return success();
        }
        case 34:
            emit_vprintf(call->getArg(0), /*buf=*/nullptr);
            return success();
        case 85:
            emit_vprintf(call->getArg(0), call->getArg(1) /*pre-packed*/);
            return success();
        default:
            return emit_diagnostic("unsupported printf form (op-tag = "
                                   + std::to_string(tag) + ")");
    }
}
```

Buffer size `N` is the sum of the slot sizes for every variadic operand, in order, once each operand has been legalized to its ABI-stored type.

## Runtime Symbol

The runtime intrinsic is `vprintf(fmt: i8*, buf: i8*) -> i32`. The original `call printf(...)` becomes a direct `call @vprintf(fmt, buf)`, and every use of the printf result is replaced with the vprintf result. The declaration is materialized lazily the first time the rewriter needs it within a translation unit.

## Format String Address Space

The `fmt` argument must be a constant-AS pointer. The rewriter probes `getPointerAddressSpace(fmt) == 4` and rejects any other address space with the diagnostic `"printf format string must be a constant address space pointer"`. This rules out format strings synthesized into generic, global, shared, or local memory and forces the front-end to materialize the literal in constant memory before lowering reaches it.

## Slot Layout

Each operand in the argument-packing block contributes one 32-byte slot. The rewriter advances by exactly 32 bytes when iterating, regardless of the underlying operand size; oversized operands (anything that does not fit into a single slot's payload, including structs passed by pointer) occupy a single stride entry whose payload word holds a pointer to the larger value. Each slot header carries two fields the rewriter reads:

- The indirect-operand flag is bit 7 of the slot's tag byte. When set, the slot's payload is a pointer to the actual value rather than the value itself, and the rewriter materializes a load before packing. When clear, the slot's payload is used directly.
- The size of the actual operand drives how many bytes inside the slot are populated. The remainder is unspecified padding that `vprintf` ignores at the receiving end because the format string already encodes which operand sizes to expect.

Packing walks the args in source order, legalizes each one (`float` is promoted to `double` per the C variadic ABI, smaller integer types are widened to `int`), and writes it into `%vprintfBuffer.local` at the next slot offset. The final buffer size `N` is the offset after the last write.

## Worked Example: `printf("x=%d y=%f", i, f)`

Take the canonical mixed-type case:

```c
int   i = 7;
float f = 3.5f;
printf("x=%d y=%f", i, f);
```

The front-end emits this as a `call printf(...)` with two variadic operands. The frontend has also placed the literal `"x=%d y=%f"` into a constant-AS-4 string global and attached a tag-40 packing block to the call.

After lowering, the buffer carries two slots, 32 bytes each, for a total `N = 64`:

| Slot | Offset | Tag byte | Payload | Notes |
|---:|---:|---|---|---|
| 0 | 0  | 0x00 | `i32 7`  written at byte 0, zero-padded to 32 bytes  | `%d` consumes one slot. Bit 7 of tag clear: payload is the literal value. |
| 1 | 32 | 0x00 | `f64 3.5` written at byte 32, zero-padded to 32 bytes | `%f` promotes the `float` to `double` per the C variadic ABI; the 8-byte payload sits at the slot base. |

If the call had passed a `struct Pt { int x, y, z, w, u; }` through `%p` instead of `f`, the slot tag's bit 7 would be set and the 8-byte payload would be a pointer to the struct rather than the struct contents. The 32-byte stride absorbs the size mismatch: every operand consumes exactly one slot regardless of width, and the indirect-pointer escape hatch handles anything that does not fit.

Output IR:

```llvm
@.str = private addrspace(4) constant [10 x i8] c"x=%d y=%f\00"

define ptx_kernel void @k(i32 %i, float %f) {
entry:
  %vprintfBuffer.local = alloca [64 x i8]
  %slot0  = getelementptr [64 x i8], ptr %vprintfBuffer.local, i64 0, i64 0
  store i32 %i,                ptr %slot0
  %slot1  = getelementptr [64 x i8], ptr %vprintfBuffer.local, i64 0, i64 32
  %f.d    = fpext float %f to double
  store double %f.d,           ptr %slot1
  %fmt    = getelementptr [10 x i8], ptr addrspace(4) @.str, i64 0, i64 0
  %r      = call i32 @vprintf(ptr addrspace(4) %fmt, ptr %vprintfBuffer.local)
  ret void
}
```

The buffer name `%vprintfBuffer.local` is preserved verbatim across CUDA versions, and downstream tooling that parses `--print-after` dumps anchors on that name to find the lowered call.

## Cross-References

[MemorySpaceOpt](memory-space-opt-and-process-restrict.md#memoryspaceopt) classifies the `%vprintfBuffer.local` alloca as local space and propagates that tag onto every slot pointer. The [Parameter-Space Sizer](nvvm-ir-verifier.md#parameter-space-sizer) does not size this buffer against the per-SM ceiling — `vprintf` does not take its arguments through `.param`.