# Printf Lowering and the vprintf ABI

## Abstract

VprintfLowering rewrites every CUDA-side `printf(...)` call into the device-runtime intrinsic `vprintf(fmt, buffer)`. The format string stays a constant-address-space pointer, the variadic tail packs into a contiguous per-thread local buffer, and the high-level call becomes a direct call to the runtime symbol. The pass is a flat scan: visit each `call printf(...)`, dispatch on a single op-tag byte attached to the call's argument-packing block, and emit the lowered form for that tag. No inter-procedural analysis, no varargs reasoning beyond what the tag already encodes.

## Input and Output Shape

The pass consumes one IR opcode and emits one runtime call plus optional packing
ops. The shape of the rewrite, for the varargs tag, is:

```text
input  : %r = call @printf(%fmt, %a, %b, %c, ...)            ; fmt in addrspace(4)
output : %buf = alloca %vprintfBuffer.local : [N x i8]
         store %a, %buf+off_a
         store %b, %buf+off_b
         store %c, %buf+off_c
         %r   = call @vprintf(%fmt, %buf)                    ; i32 result
```

For the bare-format tag the `alloca` and stores are absent and the buffer
argument is `nullptr`. For the pre-packed tag the caller has already produced
`%buf` and the pass forwards it verbatim.

## Rewriter Entry Point

The rewriter `sub_2863FE0` walks every `call printf(...)` in the current function. For each call it reads the op-tag byte at offset 0 of the call's argument-packing block and dispatches on the value. Three tag bytes pass; anything else triggers a hard diagnostic.

| Tag | Form | Meaning |
|---|---|---|
| 40 | varargs | Standard `printf(fmt, a, b, c, ...)`. Pack the args into a local buffer. |
| 34 | bare format | `printf(fmt)` with no variadic args. Skip packing; pass `nullptr` as buffer. |
| 85 | pre-packed buffer | Caller already packed args into a buffer; forward it. Used by CUB / Thrust internals. |

Any other tag emits `"unsupported printf form (op-tag = N)"`, with the decimal tag value substituted for `N`. The string is emitted verbatim with no localization.

## Buffer Allocation

Tag 40 emits a single `alloca` in the function's entry block sized to the sum of the packed-arg sizes. The allocation is named `%vprintfBuffer.local`, and that name is the canonical fingerprint for vprintf-lowered functions across every CUDA version — stable, deterministic, and untouched by later NVPTX passes. Tag 34 skips the allocation entirely and feeds `nullptr` as the buffer argument. Tag 85 forwards the caller-supplied pointer and allocates nothing.

```c
LogicalResult lowerPrintf(CallInst *call) {
    uint8_t tag = call->getArgPackingBlock()[0];
    switch (tag) {
        case 40: {
            Value *buf = allocaPackingBuffer(call);   // %vprintfBuffer.local = alloca [N x i8]
            emitVprintf(call->getArg(0), buf);
            return success();
        }
        case 34:
            emitVprintf(call->getArg(0), /*buf=*/nullptr);
            return success();
        case 85:
            emitVprintf(call->getArg(0), call->getArg(1) /*pre-packed*/);
            return success();
        default:
            return emit("unsupported printf form (op-tag = " + std::to_string(tag) + ")");
    }
}
```

Buffer size `N` is the sum of the slot sizes for every variadic operand, in order, once each operand has been legalized to its ABI-stored type.

## Runtime Symbol

The runtime intrinsic is `vprintf(fmt: i8*, buf: i8*) -> i32`, declared at rodata offset `0x4D02AE5`. The original `call printf(...)` becomes a direct `call @vprintf(fmt, buf)`, and every use of the printf result is replaced with the vprintf result. The declaration is materialized lazily the first time the rewriter needs it within a translation unit.

## Format String Address Space

The `fmt` argument must be a constant-AS pointer. The rewriter probes `getPointerAddressSpace(fmt) == 4` and rejects any other address space with the diagnostic `"printf format string must be a constant address space pointer"`. This rules out format strings synthesized into generic, global, shared, or local memory and forces the front-end to materialize the literal in constant memory before lowering reaches it.

## Inline Operand Layout

Each operand in the call's argument-packing block carries its own metadata. The rewriter reads two fields per arg slot:

- `a3[7] & 0x80` is the indirect-operand flag. Set means the slot value is `*ptr` rather than a literal — the rewriter materializes a load before packing. Clear means the slot value is used directly.
- The variadic args list uses a fixed 32-byte stride per arg. The rewriter advances by exactly 32 bytes when iterating, regardless of the underlying operand size; oversized operands occupy a single stride entry whose payload is read out of the auxiliary table indexed from the slot header.

Packing walks the args in source order, legalizes each one, and writes it into `%vprintfBuffer.local` at the next slot offset. Slot offsets follow the device ABI's natural alignment; the final buffer size N is the offset after the last write.

## Reimplementation Notes

The pass is intentionally narrow. It assumes the front-end has already attached a valid op-tag and a valid arg-packing block; it does not re-derive operand types from the printf prototype, and it does not try to recover packing from malformed calls. The three accepted tags partition the entire device-printf call space the front-end can emit, and the diagnostic for unknown tags is the only failure path that reaches user output.

Reimplementations should preserve the `%vprintfBuffer.local` name verbatim, the `addrspace(4)` format-string check, the 32-byte variadic stride, and the bit-7 indirect-operand flag. Each is observable downstream: the buffer name in IR dumps, the AS check in the diagnostic stream, and the stride and flag in the layout of any tool that re-reads the argument-packing block.

