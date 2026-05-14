# LowerStructArgs: Bare-Pointer ABI Translation

## Abstract

LowerStructArgs rewrites by-value struct parameters into pointer-to-`.param` parameters. Every aggregate load of the original SSA argument becomes a sequence of scalar `.param` loads, and the use-graph gets rewired so downstream instructions consume the loaded scalars instead of the original struct value. The transform lands late enough that struct shape is still visible but early enough that instruction selection sees only pointer-and-scalar traffic.

NVPTX cannot pass an aggregate object directly through register classes the way the IR-level ABI pretends it can. Every by-value struct parameter must be materialized as a pointer into parameter space, loaded piecewise, and address-space-cast wherever the original value flowed into a generic-pointer consumer.

## Rewrite Shape

The pass operates at the LLVM-IR / SelectionDAG MachineIR boundary. The IR
shape it consumes and the shape it produces are:

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

The `byval` aggregate parameter becomes a parameter-space pointer; every load
that read a struct field is replaced by `LDPARAM` (MI opcode 101) reading from
the parameter pointer at the field's offset, followed by `CVT_GENERIC_TO_AS`
(opcode 80) when the loaded scalar still flowed into a typed pointer consumer.

## Pass Layout

The pass body at `sub_2842580` (12.9 KB, 627 basic blocks) seeds a work-list from every by-value struct argument of the current function, then drains the work-list depth-first, rewriting each use as it is visited. `sub_2841420` materialises the replacement load and address-space cast. When materialization needs to convert a `.param` pointer into a global-space pointer — the case where the by-value struct's address itself flows to a callee — it calls `sub_28402E0` to emit the opcode-50 cast directly.

The typeinfo slot carries the captured string `"LowerStructArgsPass]"`; the trailing `]` is the libc++ / libstdc++ `__PRETTY_FUNCTION__` closing bracket convention and confirms the symbol came from a compiler-generated pretty-function literal rather than a hand-written tag. The master enable flag exposes itself under the `cl::opt` name `"opt-byval"`. A legacy global at `byte_5B6CAC0`, shared with MemorySpaceOpt, gates the whole pass: when zero, `sub_2842580` returns immediately and the by-value struct calling convention is preserved verbatim for builds that still need it.

## MI Opcodes

Four machine-instruction opcodes participate in the rewrite. The materializer picks among them based on the original use's address space and what the consumer expects.

| MI Opcode | Mnemonic | When emitted |
|---|---|---|
| 49 | `CVT_PARAM_TO_GENERIC` | Cast a `.param` pointer to a generic pointer for downstream generic-space uses. |
| 50 | `CVT_PARAM_TO_GLOBAL` | Cast a `.param` pointer to a global-space pointer; emitted via `sub_28402E0`. |
| 80 | `CVT_GENERIC_TO_AS` | Generic-cast follow-up that pins the loaded scalar back to the original SSA type. |
| 101 | `LDPARAM` | Load a scalar from `.param` space at a computed offset from the parameter pointer. |

Opcode 101 always precedes opcode 80 in the materialized sequence: read the scalar out of parameter space first, then cast it to whatever pointer flavor the original SSA value carried. Opcodes 49 and 50 fire only on the address-cast path, where the original by-value struct's address itself flowed into a generic or global consumer rather than being loaded through.

## Work-List Triple

A 24-byte triple tracks each rewrite. Each entry binds the original SSA value to its replacement and to the specific use edge that needs rewriting; deduplication runs at insertion time, so a single defining value seen through several edges produces several triples but only one replacement scalar.

```c
typedef struct WorkItem {
    /*+0x00*/ Value     *defining;     // original SSA value to rewrite
    /*+0x08*/ Value     *replacement;  // new value (loaded scalar or cast pointer)
    /*+0x10*/ Use       *use_edge;     // the specific use site to be rewired
} WorkItem;
```

Carrying the use edge rather than just the user instruction matters: GEP chains feeding multiple downstream loads share a user but not a use, and each use needs an independent rewrite to avoid double-counting and to preserve the SSA def-use invariants that later passes depend on.

## Use-Graph State Machine

`sub_2842580` is a switch over user-instruction opcodes. Each case rewrites the user in place, then pushes any secondary uses produced by that rewrite back onto the work-list. Unknown opcodes bail with a diagnostic — the rewrite is total over its supported instruction set, rather than silently leaving half-rewritten def-use chains for later passes to trip over.

```c
LogicalResult lowerStructArgs(Function *F) {
    WorkList<WorkItem> wl = seedFromByValArgs(F);
    while (!wl.empty()) {
        WorkItem item = wl.pop();
        Instruction *user = cast<Instruction>(item.use_edge->getUser());
        switch (user->getOpcode()) {
            case GEP:    rewriteGep(user, item);     pushUses(wl, user); break;
            case Load:   rewriteLoad(user, item);                         break;
            case Store:  rewriteStore(user, item);                        break;
            case Call:   rewriteCallArg(user, item);                      break;
            default:     bailWithDiagnostic(user);                        return failure();
        }
    }
    return success();
}
```

GEPs are the only opcode that re-seeds the work-list: a GEP of the by-value struct produces a new pointer whose own uses must be rewritten, so the walker descends into them. Loads, stores, and calls terminate the rewrite — the materializer emits the `LDPARAM`+`CVT_GENERIC_TO_AS` pair (or, for calls and stores, the appropriate address-cast variant), and the original instruction is either replaced or has its operand swapped to the loaded scalar.

## Materializer

`sub_2841420` is the single entry point for emitting replacement MIs. Given a work-item, it computes the offset of the requested scalar inside the original struct, emits a `LDPARAM` (opcode 101) reading from the rewritten parameter pointer at that offset, then emits a `CVT_GENERIC_TO_AS` (opcode 80) to coerce the loaded value back to the original SSA type. Order matters: the cast consumes the load's result, and the load consumes the parameter pointer rather than the original aggregate pointer, so the rewrite naturally severs the use-graph from the original by-value argument.

When the rewrite path involves the struct's address rather than its contents, the materializer skips the load and goes straight to an address-space cast. Opcode 49 covers the generic-pointer case; opcode 50 is delegated to `sub_28402E0`, which constructs the global-space cast inline and inserts it at the rewrite site. Both casts produce a new SSA value that feeds back into the work-list as the replacement for the next use down the chain.

## Reimplementation Invariants

- Seed the work-list from every by-value struct argument of the function, one entry per use edge.
- Carry the use edge in each work-item; never key the rewrite on the user instruction alone.
- Re-seed the work-list from GEP results; terminate on Load, Store, and Call without re-seeding.
- Emit `LDPARAM` (opcode 101) before `CVT_GENERIC_TO_AS` (opcode 80) on every scalar materialization.
- Route generic-pointer address casts through opcode 49 and global-pointer address casts through `sub_28402E0`
  (opcode 50).
- Bail with a diagnostic on unrecognized user opcodes rather than leaving the use-graph partially rewritten.
- Respect `byte_5B6CAC0`: when zero, the pass is a no-op and by-value struct passing is preserved.
- Honor the `"opt-byval"` cl::opt as the master enable flag.

## Cross-References

[Modulo Scheduler and Rau-Style Placement](../scheduler/modulo-scheduler-and-rau.md) documents the scheduler that
consumes the rewritten parameter loads. MemorySpaceOpt shares the `byte_5B6CAC0` global with this pass and runs after
LowerStructArgs has reduced every by-value struct to scalar `.param` traffic.
