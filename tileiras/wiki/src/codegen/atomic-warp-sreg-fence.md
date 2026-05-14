# Atomic, Warp, Sreg, Fence Emission

## Abstract

Four PTX synchronization and communication families share the NVPTX
backend's final printer: atomic read-modify-write and reductions, warp-level
collectives, special-register readers, and the fence/mbarrier/proxy-fence
family. They enter code generation through different IR layers and selector
dispatch arms, then converge on the same emitter.

The contract is modifier construction in a fixed order. Atomics and fences
carry memory ordering and scope. Warp collectives carry a small kind enum
that picks a PTX template. Special-register readers map a typed NVVM op to
a registered PTX special register and route through a fast path for thread
and CTA coordinates. Mbarriers and proxy fences carry operation-specific
operands but reuse the same scope vocabulary, so one ordering/scope packing
function services every family.

## Atomic and Reduction Family

Atomic RMW lowering builds a modifier record. The printer emits modifiers
in a fixed order: cluster-tail, scope, ordering, operation, cache hint, type.
Reductions reuse the same scope and ordering vocabulary but support a
smaller ordering set at the PTX level.

| Family / op | Orderings | Scope set | Type set |
|---|---|---|---|
| `red.cta` / `red.gpu` / `red.sys` / `red.cluster` | relaxed default, release | cta/gpu/sys/cluster | b32/b64/u32/u64/s32/s64/f32/f64/f16/f16x2/bf16/bf16x2 |
| `atom.cas{.b16,.b32,.b64,.b128}` | relaxed/acquire/release/acq_rel/seq_cst | cta/gpu/sys/cluster/cta::cluster | b16, b32, b64, b128 |
| `atom.exch.{b32,b64}` | relaxed/acquire/release/acq_rel/seq_cst | cta/gpu/sys/cluster/cta::cluster | b32, b64 |
| `atom.add` | relaxed/acquire/release/acq_rel/seq_cst | cta/gpu/sys/cluster/cta::cluster | u32, u64, f32, f64, f16/bf16 packed forms |
| `atom.and.{b32,b64}` | relaxed/acquire/release/acq_rel/seq_cst | cta/gpu/sys/cluster | b32, b64 |
| `atom.or.{b32,b64}` | relaxed/acquire/release/acq_rel/seq_cst | cta/gpu/sys/cluster | b32, b64 |
| `atom.xor.{b32,b64}` | relaxed/acquire/release/acq_rel/seq_cst | cta/gpu/sys/cluster | b32, b64 |
| `atom.min.{s32,s64,u32,u64}` | relaxed/acquire/release/acq_rel/seq_cst | cta/gpu/sys/cluster | s32, s64, u32, u64 |
| `atom.max.{s32,s64,u32,u64}` | relaxed/acquire/release/acq_rel/seq_cst | cta/gpu/sys/cluster | s32, s64, u32, u64 |

Some `red.gpu.global.add.*` forms emit as inline PTX templates rather than
through the generic modifier printer. Invalid combinations get specific
diagnostics: `unsupported ordering for nvvm.atomic.rmw`,
`Invalid memory model ordering for nvvm.red`,
`Invalid reduction op for nvvm.red`, `Invalid reduction type for nvvm.red`.

The printer concatenates tokens in a fixed order so a reimplementation can read tokens off a modifier word without re-sorting. The order, from left to right, is: opcode stem, memory ordering, scope, operation suffix, address-space suffix, optional cache hint, type suffix, then the operand list. Each token comes from a small enum table; an absent enum value (default order or implicit scope) emits nothing rather than a placeholder dash.

For an atomic add on shared memory with relaxed memory order and CTA scope, the printer reads `op = ADD`, `order = RELAXED`, `scope = CTA`, `addrspace = SHARED`, `type = U32` from the operand record and emits:

```
atom.relaxed.cta.add.u32.shared %r0, [%r1], %r2;
```

The token order is `atom` (stem) → `.relaxed` (order) → `.cta` (scope) → `.add` (operation) → `.u32` (type) → `.shared` (address space) → operands. A few token slots accept compound forms: scope can be `.cta::cluster` when the cluster-tail bit is set, the cache-hint slot expands to `.L2::cache_hint` and adds a cache-policy operand, and the type slot can take packed widths like `.f16x2` or `.bf16x2`.

Reductions reuse the same order without a return register:

```
red.gpu.add.f32.global [%rd0], %f1;
```

Atomic compare-and-swap doubles the operand count but keeps the same token order:

```
atom.acquire.gpu.cas.b64.global %rd0, [%rd1], %rd2, %rd3;
```

Lowering rejects illegal order/scope pairs before the printer fires, so the token-emission step never has to recover from an invalid modifier word. The invariant a reimplementation must preserve: every order/scope/space combination that lowering accepts is also accepted by `ptxas` on the current target. The verifier in [ISelDAG and MatcherTable — Subtarget Feature Model](iseldag-and-matchertable.md#subtarget-feature-model) shares this contract through the same subtarget feature bitmap.

## Warp-Level Collectives

Four MLIR NVVM ops model warp-level collectives: `nvvm.redux.sync`,
`nvvm.shfl.sync`, `nvvm.vote.sync`, `nvvm.match.sync`. Each carries a
compact kind enum that selects the PTX template.

| NVVM op | Kind enum | PTX template family | Verifier / constraint |
|---|---|---|---|
| `nvvm.redux.sync` | add, umin, umax, min, max, and, or, xor, fmin, fmax, fminabsnan, fmaxabsnan | `redux.sync.*` on 32-bit values | must run uniformly over the entire subgroup |
| `nvvm.shfl.sync` | bfly, up, down, idx | `shfl.sync.{bfly,up,down,idx}.b32` | optional validity predicate result |
| `nvvm.vote.sync` | any, all, uni, ballot | `vote.sync.{any,all,uni}.pred` or `vote.sync.ballot.b32` | ballot returns i32; others return i1 |
| `nvvm.match.sync` | any, all | `match.sync.{any,all}.{b32,b64}` | any returns i32; all returns `{i32, i1}` |

`redux.sync` is feature-gated. Integer reductions require the redux-capable
path; floating redux appears only on newer targets. `bar.warp.sync` belongs
to the same warp-level family and emits `bar.warp.sync mask`.

The selector dispatches each warp collective by intrinsic-ID plus operand types. The intrinsic ID picks the family (`redux`, `shfl`, `vote`, `match`); the kind enum on the SDNode picks the operation within the family; and the operand element type picks the PTX type suffix. Four representative emissions:

```
redux.sync.add.s32     %r0, %r1, 0xFFFFFFFF;     // signed-int reduction over the full warp
shfl.sync.bfly.b32     %r0|%p0, %r1, 0x10, 0x1F, 0xFFFFFFFF;
vote.sync.ballot.b32   %r0, %p1, 0xFFFFFFFF;     // ballot returns i32
match.sync.any.b32     %r0, %r1, 0xFFFFFFFF;     // match.any returns i32
```

The `vote.sync.{any, all, uni}` variants return a `pred` rather than `b32`; `match.sync.all.b32` returns the pair `{i32, i1}` and the printer emits the i1 destination as the second operand slot. The last 32-bit operand on each form is the membership mask the issuing thread passes in. `redux.sync` is feature-gated and requires the subtarget bitmap's `has_redux` bit; floating `redux` adds `has_redux_float`. The verifier rejects non-uniform `redux.sync` callers before the selector fires, so the emitter can treat the subgroup as uniform without re-checking.

The `bar.warp.sync mask;` instruction belongs to the same family and emits a warp-level barrier. Selection routes it through the same dispatcher arm as `vote.sync`, with `bar.warp.sync` as the stem and the mask as the single operand.

## Special-Register Readers

Tileiras registers the `nvvm.read.ptx.sreg.*` family for PTX
special-register reads. A compact fast path prints the base thread and CTA
coordinate registers; the rest go through the ordinary instruction printer.

| Sreg family | PTX name(s) | Intrinsic | Width |
|---|---|---|---|
| Thread index | `%tid.x` / `%tid.y` / `%tid.z` | `nvvm.read.ptx.sreg.tid.{x,y,z}` | u32 |
| Thread-block dim | `%ntid.x` / `%ntid.y` / `%ntid.z` | `nvvm.read.ptx.sreg.ntid.{x,y,z}` | u32 |
| CTA index | `%ctaid.x` / `%ctaid.y` / `%ctaid.z` | `nvvm.read.ptx.sreg.ctaid.{x,y,z}` | u32 |
| Grid dim | `%nctaid.x` / `%nctaid.y` / `%nctaid.z` | `nvvm.read.ptx.sreg.nctaid.{x,y,z}` | u32 |
| Cluster geometry | `%clusterid.*`, `%nclusterid.*`, `%cluster_ctaid.*`, `%cluster_nctaid.*` | `nvvm.read.ptx.sreg.cluster*` | u32 |
| Cluster rank | `%cluster_ctarank`, `%cluster_nctarank` | `nvvm.read.ptx.sreg.cluster.ctarank` and sibling | u32 |
| SM / warp identity | `%smid`, `%nsmid`, `%warpid`, `%nwarpid`, `%laneid`, `%warpsize`, `%gridid` | matching `nvvm.read.ptx.sreg.*` | u32 |
| Lane-mask predicates | `%lanemask_{eq,ge,gt,le,lt}` | `nvvm.read.ptx.sreg.lanemask.{eq,ge,gt,le,lt}` | u32 |
| Clocks / timer | `%clock`, `%clock64`, `%globaltimer` | matching `nvvm.read.ptx.sreg.*` | u32 / u64 / u64 |
| Environment regs | `%envreg0` .. `%envreg31` | `nvvm.read.ptx.sreg.envreg{0..31}` | u32 |

`%dynamic_smem_size` reads through inline assembly. Tileiras exposes only
the combined u64 `%globaltimer` form, not separate high/low 32-bit halves.
Performance counters `%pm0` through `%pm7` go unregistered.
`nvvm.breakpoint` uses `%globaltimer` for a short busy-wait before
trapping.

The reader path is a one-line `mov` template keyed by the registered sreg
name. The fast path for thread and CTA coordinates skips the generic
instruction printer entirely:

```c
void print_sreg_read(Printer *p, SregReadInst *inst) {
    if (is_thread_or_cta_sreg(inst->sreg)) {
        /* Fast path: compact "mov.u32 %rN, %sreg;" emission. */
        write(p, "mov.u32 ");
        print_dest_reg(p, inst->dest);
        write(p, ", ");
        write_sreg_token(p, inst->sreg);
        write(p, ";");
        return;
    }
    /* Slow path: ordinary instruction printer handles cluster geometry, lane masks,
       clocks, environment regs, and any sreg that needs u64 typing. */
    print_inst_generic(p, inst);
}
```

## Fence and Mbarrier Family

Fence scope encodes two ways. `acq_rel` and `sc` fences use
scope-suffixed operation names; `acquire` and `release` fences carry scope
as an attribute. Mbarriers model initialization, arrival, expected
transactions, invalidation, and waits. Proxy fences model synchronization
between generic, async, cluster, and tensormap proxies.

| Op family | Operands / attrs | PTX lowering |
|---|---|---|
| `mbarrier.init` / `.shared` | smemPtr, count | `mbarrier.init[.shared].b64 [$p], $n;` |
| `mbarrier.arrive` / `.shared` | smemPtr | `mbarrier.arrive[.shared].b64 $r, [$p];` |
| `mbarrier.arrive.nocomplete` | smemPtr, count | `mbarrier.arrive.noComplete[.shared].b64 $r, [$p], $cnt;` |
| `mbarrier.arrive.expect_tx` | smemPtr, txCount | `mbarrier.arrive.expect_tx[.shared].b64 $r, [$p], $tx;` |
| `mbarrier.txn` | smemPtr, txCount, relaxed, noComplete, shared-space kind, scope, peer rank | `mbarrier.expect_tx{.relaxed}.{cta|cluster}.b64 [$p], $tx;` |
| `mbarrier.test.wait` | smemPtr, token | `mbarrier.test_wait[.shared].b64 $r, [$p], $token;` |
| `mbarrier.try_wait` | smemPtr, parity, suspendNs or timelimit | `mbarrier.try_wait*.b64 ...;` |
| `mbarrier.wait` | smemPtr, optional parity | `mbarrier.wait[.parity].b64 $r, [$p][, $par];` |
| `fence.acq_rel.{cta,cluster,gpu,sys}` | none | `fence.acq_rel.{cta,cluster,gpu,sys};` |
| `fence.sc.{cta,cluster,gpu,sys}` | none | `fence.sc.{cta,cluster,gpu,sys};` |
| `fence.acquire` / `fence.release` | scope, space | `fence.{acquire,release}.<scope>;` |
| `fence.mbarrier.init` | useIntrinsic | `fence.mbarrier_init.release.cluster;` |
| `fence.proxy` | kind, space, useIntrinsic | `fence.proxy.<kind>;` |
| `fence.proxy.acquire` / `fence.proxy.release` | fromProxy, scope, toProxy | `fence.proxy.<from>::<to>.{acquire,release}.<scope>.sync.aligned [addr], sz;` |
| `tensormap.cp_fenceproxy` | srcTmapPtr, dstTmapPtr, sizeBytes, scope | `tensormap.cp_fenceproxy.<scope>.tensormap::generic.release.<scope>.sync.aligned [dst], [src], sz;` |

Legacy `membar.{cta,gpu,sys}` remains as a fallback. Cluster-scope fences
have no pre-Hopper fallback and diagnose unsupported ordering/scope
combinations on older targets.

Fence emission resolves the scope-as-name-vs-scope-as-attribute split at print time:

```c
void print_fence(Printer *p, FenceInst *inst) {
    if (inst->order == ORDER_ACQ_REL || inst->order == ORDER_SEQ_CST) {
        /* Scope is folded into the operation name: fence.acq_rel.cta */
        write(p, "fence.");
        write_order_token(p, inst->order);
        write(p, ".");
        write_scope_token(p, inst->scope);
        write(p, ";");
        return;
    }

    /* Acquire and release fences carry scope as a separate token. */
    require(inst->order == ORDER_ACQUIRE || inst->order == ORDER_RELEASE,
            "fence ordering must be acquire, release, acq_rel, or seq_cst");
    write(p, "fence.");
    write_order_token(p, inst->order);
    write(p, ".");
    write_scope_token(p, inst->scope);
    write(p, ";");
}
```

Proxy and tensormap fences extend this with `<from>::<to>` proxy tokens
and an aligned-sync address payload; the underlying decision tree is the
same.

## SyncScope Mapping

LLVM SyncScope names get normalized before atomic and fence printing.
Tileiras keeps a small map from LLVM scope names to the NVPTX scope
vocabulary, then packs ordering and scope into the modifier word the final
printer consumes.

| LLVM SyncScope name | Backend scope | PTX token |
|---|---|---|
| `singlethread` | thread | no token |
| empty default scope | system | `.sys` |
| `block` | CTA | `.cta` |
| `cluster` | cluster | `.cluster` |
| `device` | device / GPU | no explicit token |

The default device scope deliberately prints no `.gpu` token for ordinary
atomics because PTX treats GPU scope as the default spelling. CTA scope
becomes the composite `.cta::cluster` spelling when the lowering path asks
for cluster-tail semantics. Cache-hint atomics accept only CTA and system
scope; cluster cache hints get rejected outright rather than silently
downgraded.

```c
typedef enum {
    NVPTX_SCOPE_THREAD,
    NVPTX_SCOPE_CTA,
    NVPTX_SCOPE_CLUSTER,
    NVPTX_SCOPE_DEVICE,
    NVPTX_SCOPE_SYSTEM,
} NvptxScope;

typedef enum {
    ORDER_RELAXED = 1,
    ORDER_ACQUIRE = 2,
    ORDER_RELEASE = 3,
    ORDER_ACQ_REL = 4,
    ORDER_SEQ_CST = 5,
} AtomicOrder;

NvptxScope map_sync_scope(const char *scope_name) {
    if (scope_name == NULL || scope_name[0] == '\0') {
        return NVPTX_SCOPE_SYSTEM;
    }
    if (strcmp(scope_name, "singlethread") == 0) {
        return NVPTX_SCOPE_THREAD;
    }
    if (strcmp(scope_name, "block") == 0) {
        return NVPTX_SCOPE_CTA;
    }
    if (strcmp(scope_name, "cluster") == 0) {
        return NVPTX_SCOPE_CLUSTER;
    }
    if (strcmp(scope_name, "device") == 0) {
        return NVPTX_SCOPE_DEVICE;
    }
    fail("unsupported NVPTX synchronization scope");
}

unsigned pack_atomic_modifier(AtomicOrder order, NvptxScope scope, bool cta_cluster_tail) {
    unsigned modifier = (unsigned)order;

    if (scope == NVPTX_SCOPE_CTA) {
        modifier |= 1u << 4;
    } else if (scope == NVPTX_SCOPE_SYSTEM) {
        modifier |= 2u << 4;
    } else if (scope == NVPTX_SCOPE_CLUSTER) {
        modifier |= 3u << 4;
    }

    if (cta_cluster_tail) {
        modifier |= 1u << 9;
    }

    return modifier;
}
```

The reimplementation rule is simple: preserve the high-level LLVM ordering first, map the scope name to the closest PTX scope second, then pick the printed suffix. Diagnose unsupported order/scope pairs at lowering time so the printer never recovers from an invalid modifier word.

## Cross-References

[AsmPrinter — MC Switch Shape Population Table](asm-printer-monster-and-windows.md#mc-switch-shape-population-table) documents the dispatcher that selects the print shape for these atomic, warp-collective, sreg, and fence opcodes. [ISelDAG and MatcherTable](iseldag-and-matchertable.md) covers the selector that consumes the same subtarget feature bitmap before any of these instructions reach the printer. [tcgen05 mbarrier Emission](tcgen05-wgmma-mbarrier-cluster.md#mbarrier-emission) and the [mbarrier State Machine](../topics/mbarrier-state-machine.md) cover the mbarrier and proxy-fence families that share scope and ordering vocabulary with this page.
