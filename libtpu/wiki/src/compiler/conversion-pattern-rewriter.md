# ConversionPatternRewriter

> *All addresses, symbols, and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped, `.text` VA == file offset). Other versions will differ; treat every VA as version-pinned.*

## Abstract

The [DialectConversion Legalizer](dialect-conversion-legalizer.md) tries patterns *speculatively*: it picks the depth-cheapest pattern rooted at an op, applies it, then recurses on whatever that pattern produced — and if any produced op cannot itself be legalized, it must **undo everything the pattern did** and try the next candidate. That undo is not free in MLIR: a pattern that ran `replaceOp`, inserted three ops, moved a fourth, and mutated a fifth's attributes has touched the IR in five places. Making that reversible is the job of `mlir::detail::ConversionPatternRewriterImpl`, the private engine behind every `ConversionPatternRewriter&` a TPU lowering pattern receives. This page owns that engine: the **rewrite-log** (an undo record stack), the **rollback** that unwinds it, the **1:N result replacement** path (one `tpu` op replaced by several `sparse_core` values), and the **unresolved-cast materialization** that bridges still-unconverted types.

The reframe a reimplementer needs is this: a `ConversionPatternRewriter` is **not** a `RewriterBase` that mutates IR in place. It is a *recording* rewriter. Every mutation a pattern performs — insert, replace, move, in-place modify, block splice — is intercepted by the Impl's listener interface and appended as one `IRRewrite` record to a `SmallVector<unique_ptr<IRRewrite>>` at `Impl+0x48`. The IR *is* mutated (so recursion sees the new ops), but every change is also logged with enough saved state to reverse it. On a failed legalization the log is replayed backwards (`undoRewrites`, newest-first); on success it is replayed forwards twice (`applyRewrites`: commit-all, then erase-all). The value-replacement records do not rewire SSA uses at record time — they record the *intent* and defer the real `replaceAllUsesWith` to commit, which is what makes 1:N replacement and cast materialization clean.

Hold the upstream MLIR `DialectConversion.cpp` frame: `ConversionPatternRewriterImpl`, the `IRRewrite` class hierarchy, `RewriterState`, `undoRewrites`/`resetState`/`applyRewrites`, `buildUnresolvedMaterialization`, `findOrBuildReplacementValue`. This page is the byte-level recovery of that frame as it ships in `libtpu.so`, with the 11-record hierarchy, the 5-virtual record ABI, and the 0x1e0-byte Impl layout all pinned to relocations and to the three driver functions that read them. Where the recovered behavior is subtle — the deferred two-pass commit, the LIFO/FIFO asymmetry, the `operator new` size split between create- and move-records — it is called out.

For reimplementation, the contract is:

- **The rewrite-log.** A `SmallVector<unique_ptr<IRRewrite>, 6>` at `Impl+0x48`; 11 concrete record types under an abstract `IRRewrite` base; each record carries a `{vptr, tag, Impl*, target}` header plus leaf-specific saved state, and is appended by the rewriter's *listener* methods, never by the pattern author.
- **The record ABI.** A 5-slot vtable `{D1 dtor, D0 dtor, rollback, commit, cleanup}`; the rollback engine dispatches purely through `[vtable+0x10/0x18/0x20]` and never downcasts.
- **The rollback.** `undoRewrites(numToKeep)` pops the log newest-first, calling `rollback()` on each — so each undo sees the IR exactly as it was just after that record was pushed. `resetState(checkpoint)` layers ignored-op and replaced-op bookkeeping rewind on top.
- **The commit + 1:N + materialization.** `applyRewrites` replays forward in two passes (commit, then cleanup-erase). `ReplaceOperationRewrite::commit` calls `findOrBuildReplacementValue` per result — which, when a result's mapped replacement still has the wrong type, inserts a `builtin.unrealized_conversion_cast` (an *unresolved materialization*) and logs it. The 1:N replace path (`replaceOp` with `SmallVector<SmallVector<Value>>`) maps each original result to a *vector* of replacements through the `ConversionValueMapping`.

| | |
|---|---|
| **Engine** | `mlir::detail::ConversionPatternRewriterImpl` (private to MLIR's `DialectConversion.cpp`) |
| **Impl size** | `0x1e0` bytes (heap `operator new(0x1e0)`); built by the `ConversionPatternRewriter` ctor `0x1c9512a0`; stored at outer `this+0x10` and `this+0x28` |
| **Rewrite-log** | `SmallVector<unique_ptr<(anon)::IRRewrite>,6>` at `Impl+0x48` (data) / `+0x50` (size, u32) / `+0x54` (cap, u32; ctor inits cap=6) |
| **Record hierarchy** | 11 concrete `(anon)::IRRewrite` leaves; abstract bases `IRRewrite`/`BlockRewrite`/`OperationRewrite` carry no own vtable |
| **Record ABI** | 5-slot vtable: `+0x00` D1 dtor · `+0x08` D0 dtor · `+0x10` rollback · `+0x18` commit · `+0x20` cleanup |
| **Rollback** | `undoRewrites(unsigned, StringRef)` — `0x1c94d060` (LIFO, calls `[vtable+0x10]`) |
| **Checkpoint rollback** | `resetState((anon)::RewriterState, StringRef)` — `0x1c95bf60` |
| **Commit** | `applyRewrites()` — `0x1c94c1c0` (FORWARD, two passes: `[vtable+0x18]` then `[vtable+0x20]`) |
| **Log-append origin** | `notifyOperationInserted` — `0x1c950260`; `replaceOp` (1:N) — `0x1c950540`; `replaceValueUses` (1:1) — `0x1c94f740` |
| **Materialization** | `buildUnresolvedMaterialization` — `0x1c94dcc0`; `findOrBuildReplacementValue` — `0x1c94fde0` |
| **Confidence** | CONFIRMED (byte-anchored) for the log layout, the record ABI, the rollback/commit drivers, the 1:N replace path, and the materialization; HIGH/LOW where a row says so |

---

## The Recording Rewriter

### Purpose

Before the log, fix *why* a recording rewriter exists. The legalizer's whole strategy is "try a pattern, recurse, undo on failure." That only works if undo is possible after the IR has already been mutated — which means every mutation must be reversible. The `ConversionPatternRewriterImpl` makes mutation reversible by interposing on the `RewriterBase::Listener` interface: the pattern thinks it is calling an ordinary rewriter, but each call is both *applied to the live IR* and *recorded* as an undo entry.

### The interposition

A `ConversionPattern`'s `matchAndRewrite` receives a `ConversionPatternRewriter&`. Calls like `rewriter.create<...>()`, `rewriter.replaceOp(op, vals)`, `rewriter.eraseOp(op)`, `rewriter.modifyOpInPlace(...)` route through `RewriterBase`, whose listener is the Impl. Each listener callback does two things:

1. **Mutate the live IR** so the recursive legalizer sees the new state (the produced ops must exist for `legalize` to recurse onto them).
2. **Append one `IRRewrite` record** to the log at `Impl+0x48`, capturing whatever saved state its `rollback()` will need.

```text
pattern code                Impl listener (0x1c95xxxx)          rewrite-log @Impl+0x48
------------                --------------------------          ----------------------
rewriter.create<MloOp>() →  notifyOperationInserted   0x1c950260 →  push CreateOperationRewrite
rewriter.replaceOp(...)  →  replaceOp / replaceValueUses 0x1c950540 →  push ReplaceOperationRewrite
                                                       /0x1c94f740 →  (or ReplaceValueRewrite)
rewriter.modifyOpInPlace →  (start/finalize modify)             →  push ModifyOperationRewrite
move into block          →  notifyOperationInserted w/ parent   →  push MoveOperationRewrite
type-cast bridge         →  buildUnresolvedMaterialization 0x1c94dcc0 →  push UnresolvedMaterializationRewrite
block splice / signature →  inlineBlockBefore / convertRegionTypes →  push *BlockRewrite
```

> **NOTE — the pattern author never touches the log.** The append path *is* the listener interface; a reimplementer wires the recording into the `RewriterBase::Listener` callbacks, not into the pattern API. A pattern written against a plain `OpBuilder` would mutate the IR but produce no undo records, and the legalizer's rollback would silently do nothing — the single most dangerous reimplementation trap on this page.

> **GOTCHA — recording is gated by the conversion mode.** Every listener checks a byte at `ConversionConfig+0x29` (read as `[Impl+0x178][+0x29]`, the field at `*(_BYTE*)(v7+41)` in `notifyOperationInserted`/`replaceOp`; `Impl+0x178` is the `ConversionConfig*` stored by the ctor as its second argument). When it is `1` (rollback-enabled conversion), the listener takes the *recording* path (allocate record, append to log). Otherwise it forwards straight to the real listener and skips the log. So the same Impl behaves as a recording rewriter or a pass-through depending on the driver's mode — a reimplementer must thread that flag, or rollback will record nothing.

---

## The Rewrite-Log Structure

### Purpose

The log is the central data structure. This unit pins its container, its element type, and the 0x1e0-byte Impl it lives inside, because every driver function indexes these offsets directly.

### The Impl layout (0x1e0 bytes)

The Impl is a single heap object (`operator new(0x1e0)` at the ctor `0x1c9512da`). The outer `ConversionPatternRewriter` stores the pointer at both `this+0x10` and `this+0x28`. Field map, recovered from the ctor initialization (`0x1c9512da..0x1c95143d`) and cross-checked against the offsets the four drivers read:

```text
ConversionPatternRewriterImpl  (0x1e0 bytes)
  +0x00 .. 0x40   OpBuilder / RewriterBase base (vptr, insertion point, listener)
  +0x48           rewrite-log: data ptr        } SmallVector<unique_ptr<IRRewrite>,6>
  +0x50           rewrite-log: size  (u32)      }   THE ACTION LOG  (drivers read [+0x50])
  +0x54           rewrite-log: capacity (u32)
  +0x58           rewrite-log: inline buffer
  +0x88 .. 0xa8   ignoredOps  : SetVector<Operation*>   (live count at +0xa8)
  +0xc0/+0xc8/+0xd0  replacedOps : DenseMap<Operation*> (count at +0xd0)
  +0xd8 .. 0xf8   newlyCreatedOps SetVector  (the "produced new ops" set; count +0xe8)
  +0x100 .. 0x120 rootReplacedOps SetVector  (the "produced replaced-root ops" set)
  +0x128 .. 0x150 trackedUnrealizedCasts / appliedRecursivePatterns guard
  +0x140/+0x148/+0x150 unresolvedMaterializations : DenseMap<Operation*>  (cast tracking)
  +0x178          ConversionConfig*  (ctor arg 2; mode byte at [+0x178]+0x29)
  +0x180          OperationConverter*  backref (ctor arg 3; owns this)
  +0x1c0          MLIRContext*
  +0x1c8          context actionHandler
```

The three offsets the rollback machinery touches most are `+0x50` (log size), `+0xa8` (ignoredOps count), and `+0xd0` (replacedOps count) — these three `u32`s are exactly the `RewriterState` checkpoint (below). `+0xd8` (newlyCreatedOps) and `+0x100` (rootReplacedOps) are the two sets the legalizer drains to find the ops it must recurse onto; `+0x140` (unresolvedMaterializations) is the cast registry the materialization rollback prunes.

### The log container

```text
rewrite-log : llvm::SmallVector<std::unique_ptr<(anon)::IRRewrite>, 6>
  +0x48  data    (T* — array of unique_ptr; inline buffer at +0x58 until it spills)
  +0x50  size    (u32 — number of live records; THE log depth)
  +0x54  capacity(u32)
```

It is an *append-only stack* during legalization: records are only pushed (by listeners) and popped from the tail (by `undoRewrites`). It is never indexed randomly except by the commit/rollback walks. The `unique_ptr` element means each record's deleting dtor (`[vtable+0x08]`) is what frees it on pop. The 6-element inline buffer (`SmallVector<…,6>`; the ctor inits capacity to 6 at `[Impl+0x54]` and points data at the inline buffer `Impl+0x58`) means a conversion of up to six records never heap-allocates the array.

> **QUIRK — the log size *is* the checkpoint coordinate.** There is no separate "version counter." A checkpoint is just the triple `(log.size, ignoredOps.count, replacedOps.count)` captured by value; rolling back to a checkpoint means popping the log down to the captured size and shrinking the two side-bookkeeping containers to their captured counts. This is why the legalizer can checkpoint in three `mov`s and roll back without copying any IR.

---

## The IRRewrite Record Hierarchy

### Purpose

The 11 record types are the alphabet of reversible mutations. This unit inventories them, pins each leaf's vtable and its rollback/commit/cleanup, and fixes the 5-virtual ABI the drivers dispatch through.

### The 11 concrete records

Every record is a leaf of an `(anonymous namespace)::IRRewrite` single-base hierarchy. The intermediate abstract bases `BlockRewrite` and `OperationRewrite` carry no own vtable (typeinfo only) — only the 11 concrete leaves do, and all 11 vtables were verified against their `_ZTV` symbols. Each leaf's `{vtable, rollback, commit, cleanup}` addresses:

| IRRewrite leaf | vtable | rollback | commit | cleanup | Confidence |
|---|---|---|---|---|---|
| `UnresolvedMaterializationRewrite` | `0x21c234a0` | `0x1c95b660` | (base) | (base) | CONFIRMED |
| `InlineBlockRewrite` | `0x21c235e0` | `0x1c962920` | (base) | (base) | HIGH |
| `BlockTypeConversionRewrite` | `0x21c23648` | `0x1c9629e0` | `0x1c962a60` | (base) | HIGH |
| `CreateOperationRewrite` | `0x21c23698` | `0x1c962f60` | `0x1c963080` | (base) | CONFIRMED |
| `MoveOperationRewrite` | `0x21c236e8` | `0x1c9630c0` | `0x1c963160` | (base) | CONFIRMED |
| `ReplaceOperationRewrite` | `0x21c23738` | `0x1c963500` | `0x1c9635a0` | `0x1c963920` | CONFIRMED |
| `ReplaceValueRewrite` | `0x21c23788` | `0x1c963b00` | `0x1c963b60` | (base) | HIGH |
| `EraseBlockRewrite` | `0x21c237f0` | `0x1c963fc0` | `0x1c964020` | `0x1c9640a0` | HIGH |
| `CreateBlockRewrite` | `0x21c23840` | `0x1c9641e0` | `0x1c9642e0` | (base) | HIGH |
| `MoveBlockRewrite` | `0x21c23890` | `0x1c964340` | `0x1c964400` | (base) | HIGH |
| `ModifyOperationRewrite` | `0x21c238e0` | `0x1c964860` | `0x1c964960` | (base) | CONFIRMED |

`(base)` = the leaf inherits the no-op `IRRewrite::commit` (`0x1c95b760`, a bare `ret`) or `IRRewrite::cleanup` (`0x1c95b780`). `IRRewrite::~IRRewrite` (D1, slot0) is `0x1c964300`, shared across all leaves. Only `ReplaceOperationRewrite` and `EraseBlockRewrite` carry a non-trivial `cleanup` — the deferred-erase step (below).

The six operation-level records split into three families:

- **Structural-create/move**: `CreateOperationRewrite`, `MoveOperationRewrite` — appended by `notifyOperationInserted`.
- **Value-replacement**: `ReplaceOperationRewrite` (replace a whole op's results), `ReplaceValueRewrite` (replace one value's uses) — appended by `replaceOp` / `replaceValueUses`. These are the 1:N and 1:1 replacement records.
- **In-place + cast**: `ModifyOperationRewrite` (attrs/operands/successors/regions saved for `modifyOpInPlace`), `UnresolvedMaterializationRewrite` (a `builtin.unrealized_conversion_cast` bridge).

The five `*Block*` records cover region signature conversion and SCF structural conversion — they are the rollback half of the func/scf dynamic-legality lambdas (see [DialectConversion Legalizer](dialect-conversion-legalizer.md)). Their vtables and rollback addresses are pinned above; their bodies are HIGH (not individually decompiled) and out of scope here.

### The 5-virtual ABI

Slot-walked from the `ReplaceOperationRewrite` vtable relocation set (`0x21c23738`; the address-point used by `unique_ptr` is `vtable+0x10 = 0x21c23748`, which holds slot0):

```text
record vptr layout (all 11 leaves share this shape)
  vptr+0x00  slot0  ~IRRewrite()  (complete/D1)        = 0x1c964300  (shared)
  vptr+0x08  slot1  ~Leaf()       (deleting/D0)        = leaf-specific (frees the record)
  vptr+0x10  slot2  rollback()                          ← undoRewrites calls [+0x10]
  vptr+0x18  slot3  commit(RewriterBase&)               ← applyRewrites calls [+0x18]
  vptr+0x20  slot4  cleanup(RewriterBase&)              ← applyRewrites calls [+0x20]
```

The drivers dispatch *purely* through these slots — the rollback engine never inspects a record's tag to decide what to do, it just calls `[vtable+0x10]`. This is verified byte-exact: `undoRewrites` calls `[rax+0x10]` (rollback), `applyRewrites` calls `[rax+0x18]` (commit) then `[rax+0x20]` (cleanup). The tag at `record+0x8` (e.g. `5` for `MoveOperationRewrite`, `7` for `ReplaceOperationRewrite`, `8` for `CreateOperationRewrite`) exists for `dyn_cast` elsewhere, but the undo/commit walks ignore it.

### The common record header

```text
IRRewrite record header (offsets common to all 11 leaves)
  +0x00  vptr            (one of the 11 vtables above)
  +0x08  kind tag (u32)  (RTTI-style discriminator; e.g. Create=8, Move=5, Replace=7)
  +0x10  Impl*           (back-reference to the owning ConversionPatternRewriterImpl)
  +0x18  target          (Operation*  for op-rewrites; Block* for block-rewrites)
  +0x20+ leaf-specific saved state (see per-record rollback bodies below)
```

> **CORRECTION (CPR-1) — the `CreateOperationRewrite` record is 32 bytes, not 48.** An earlier reading attributed `operator new(0x30)` (48 B) to `CreateOperationRewrite`. The decompile of `notifyOperationInserted` (`0x1c950260`) shows the *create* path (tag `8`, vtable address-point `0x21c236a8`) allocates `operator new(0x20)` = 32 bytes — header only, no extra saved state, because rollback just unlinks the op. The `operator new(0x30)` (48 B) allocation in the same function is the *move* path: tag `5`, vtable address-point `0x21c236f8` (`MoveOperationRewrite`), which saves the previous block (`+0x20`) and the insert-before op (`+0x28`) so rollback can move the op back. The two records are allocated by the same listener method but at different sizes for different saved state.

---

## The Rollback

### Purpose

This is the failure path: discard every IR mutation made after a checkpoint, in the exact reverse order they were made, each record reversing its own change. Two functions implement it — `undoRewrites` (the log primitive) and `resetState` (the public checkpoint rollback that wraps it).

### `undoRewrites` — the LIFO unwind

`ConversionPatternRewriterImpl::undoRewrites(unsigned numRewritesToKeep, StringRef)` at `0x1c94d060`. The control flow, byte-decoded:

```c
// undoRewrites(numToKeep)   @ 0x1c94d060
void undoRewrites(Impl* this, unsigned numToKeep):
    unsigned n = this->log.size;                  // [Impl+0x50]
    if (n == numToKeep) return;                   // nothing to undo

    // PASS 1 — ROLLBACK, newest-first (LIFO)
    for (i = n - 1; i >= numToKeep; --i):          // walk DOWN from size-1
        IRRewrite* rec = log[i];
        rec->rollback();                           // call [rec_vtable+0x10]  (slot2)

    // PASS 2 — POP + free the popped records
    for (i = n - 1; i >= numToKeep; --i):
        IRRewrite* rec = log[i];
        log[i] = nullptr;
        if (rec) rec->~Leaf();                     // call [rec_vtable+0x08]  (D0, frees record)
    // shrink the SmallVector: realloc-down (mallocForGrow 0x208d1820) if it
    // drops below the inline threshold, else memset the freed tail to 0

    this->log.size = numToKeep;                    // [Impl+0x50] = numToKeep
```

The decompile confirms every step: line 28 reads `[a1+80]` (size); line 29 is the early-return guard; the unwind loop at line 35-40 walks `v8` (= `8*size`) **down** to `v7` (= `8*numToKeep`) calling `(**(rec) + 16)` = `[vtable+0x10]` = rollback; the pop loop at line 48-56 / 127-138 calls `(*rec + 8)` = `[vtable+0x08]` = deleting dtor and nulls each slot; line 85 sets `[a1+80] = numToKeep`.

> **QUIRK — rollback runs newest-first; this is mandatory, not stylistic.** Records are unwound in reverse insertion order so that each `rollback()` sees the IR in exactly the state it was in *just after that record was pushed*. A `ReplaceOperationRewrite` pushed before a `CreateOperationRewrite` that used the replacement value must be rolled back *after* the create — undo the dependent change first. Running the log forward on rollback would have a record reverse a change whose prerequisite no longer exists. The LIFO order is the inverse of commit's FIFO order (below), and the asymmetry is deliberate.

### Per-record rollback semantics

The four operation-level rollbacks plus the materialization rollback, decoded:

```c
// CreateOperationRewrite::rollback   @ 0x1c962f60
//   The op was inserted; unlink it. Walk the created op's nested block lists
//   and detach each node (ilist_traits<Block>::removeNodeFromList 0x1d8e05c0),
//   removing the inserted op from the IR. The op is NOT erased here — erase is
//   deferred (it has no cleanup; rollback just detaches).

// ReplaceOperationRewrite::rollback   @ 0x1c963500
//   For each OpResult of the replaced op (getNextResultAtOffset), erase that
//   value's entry from the ConversionValueMapping (0x1c95b7a0). This undoes the
//   result -> replacement remap so the original op's uses snap back to it.
//   (Confirmed: loops over *(op+0x24) results, calls ConversionValueMapping::erase
//    per result with a {value, range} key.)

// MoveOperationRewrite::rollback   @ 0x1c9630c0
//   transferNodesFromList (0x1d8cccc0) moves the op back to its saved location
//   {previousBlock @rec+0x20, insertBeforeOp @rec+0x28}.

// ModifyOperationRewrite::rollback   @ 0x1c964860
//   Full in-place-modification undo: restore saved loc (rec+0x28 -> op+0x18),
//   setAttrs(saved @rec+0x30, 0x1d8cbd60), setOperands(saved @rec+0x38/+0x40,
//   0x1d8cc3a0), per-successor setSuccessor (rec+0x88/+0x90, 0x1d8cd400),
//   and restore saved regions (rec+0xa8).

// UnresolvedMaterializationRewrite::rollback   @ 0x1c95b660
//   1. erase the cast's results from the ConversionValueMapping (if any mapped);
//   2. remove the cast op from unresolvedMaterializations DenseMap [Impl+0x140]
//      (open-address probe, tombstone -8192 / sentinel -4096);
//   3. Operation::erase() the builtin.unrealized_conversion_cast (0x1d8ccd20).
```

The `ReplaceOperationRewrite::rollback` decompile (`0x1c963500`) is the clearest: it iterates the replaced op's results (`getNextResultAtOffset`) and calls `ConversionValueMapping::erase` on each with key `{result, {1, 2}}` — un-mapping the value→replacement relation. Because the replacement was *recorded* not *applied* (no SSA rewire happened at record time), rolling back is just deleting the map entries; the original op and its uses were never disturbed.

### `resetState` — rollback to a labelled checkpoint

`ConversionPatternRewriterImpl::resetState((anon)::RewriterState state, StringRef)` at `0x1c95bf60`. `RewriterState` is a 12-byte POD passed by value:

```text
RewriterState (12 bytes; the legalizer's checkpoint coordinate)
  +0x00  numRewrites    (u32 — target log.size)        captured from [Impl+0x50]
  +0x04  numIgnoredOps  (u32 — target ignoredOps.count) captured from [Impl+0xa8]
  +0x08  numReplacedOps (u32 — target replacedOps.count)captured from [Impl+0xd0]
```

```c
// resetState(state)   @ 0x1c95bf60
LogicalResult resetState(Impl* this, RewriterState state):
    undoRewrites(state.numRewrites);                 // 1. unwind the action log (KF above)

    while (this->ignoredOps.count > state.numIgnoredOps):  // [Impl+0xa8]
        ignoredOps.pop_back();                       // 2. drop ops marked ignored after checkpoint
                                                     //    (SetVector::pop_back 0x1c95c080)

    while (this->replacedOps.count > state.numReplacedOps): // [Impl+0xd0]
        remove most-recent replacedOps entry;        // 3. shrink the replaced-op DenseMap
                                                     //    (open-address probe, tombstone 0xffffffffffffe000)
```

The decompile confirms: line 17 calls `undoRewrites`; lines 18-23 pop the ignoredOps SetVector (`a1[42]` = `Impl+0xa8` as a `DWORD` index) until it reaches `numIgnoredOps`; lines 24-76 shrink the replacedOps DenseMap (`a1[52]` = `Impl+0xd0`) with the `-8192`/`-4096` tombstone/sentinel probe. `resetState` is the function every legalize-with-pattern failure arm calls to atomically rewind before trying the next depth-sorted candidate.

> **NOTE — three containers, one rewind.** A checkpoint is not just a log position. Between checkpoints a pattern may have (a) pushed log records, (b) marked ops *ignored* (already-legal / erased-in-place), and (c) recorded *replaced* ops. `resetState` rewinds all three to the captured counts. A reimplementer who rolls back only the log will leave stale ignored/replaced bookkeeping that desyncs the next legalization attempt.

---

## The Commit and 1:N Replacement

### Purpose

The success path. When a sub-tree fully legalizes, its log must be made permanent: speculative replacements turn into real SSA rewires, and detached ops are erased. This is also where the 1:N result replacement and the cast materialization actually happen — they were only *recorded* during pattern application.

### `applyRewrites` — the two-pass FORWARD commit

`ConversionPatternRewriterImpl::applyRewrites()` at `0x1c94c1c0`:

```c
// applyRewrites()   @ 0x1c94c1c0
void applyRewrites(Impl* this):
    IRRewriter rw(context);                          // stack rewriter, vtable 0x217e9608

    // PASS 1 — COMMIT, oldest-first (FIFO)
    for (i = 0; i < this->log.size; ++i):            // FORWARD over [Impl+0x48]
        log[i]->commit(rw);                          // call [rec_vtable+0x18]  (slot3)

    // PASS 2 — CLEANUP-ERASE, oldest-first
    SingleEraseRewriter erw;                          // vtable 0x21c23388
    for (i = 0; i < this->log.size; ++i):
        log[i]->cleanup(erw);                        // call [rec_vtable+0x20]  (slot4)
```

The decompile confirms both passes: lines 41-48 are the commit loop (`v6` from 0 upward, `(*rec + 24)` = `[vtable+0x18]`), lines 75-80 the cleanup loop (`v13` from 0 upward, `(*rec + 32)` = `[vtable+0x20]`). Pass 1 binds an `mlir::IRRewriter`; pass 2 binds a `SingleEraseRewriter` whose `eraseOp` is the actual delete.

> **GOTCHA — commit and erase are two separate FORWARD passes, never interleaved.** All commits run first, then all erases. If commit-and-erase were fused per record, a later record's `commit` could dereference an op an earlier record already erased. Splitting into commit-all-then-erase-all guarantees no commit references a freed op. The `ReplaceOperationRewrite` and `EraseBlockRewrite` records are the only ones with a non-trivial `cleanup` (the deferred erase, slot4); every other record's cleanup is the base no-op. `ReplaceOperationRewrite::cleanup` (`0x1c963920`) is a one-liner: `erw.eraseOp(rec->op)` via `[erw_vtable+0x10]` on the saved op at `rec+0x18`.

> **QUIRK — commit is FIFO, rollback is LIFO.** Commit replays oldest-first because each commit *builds on* the prior ones (a later op's operands may be an earlier op's results, already rewired). Rollback replays newest-first because each undo *depends on* the later ones being gone first. Same log, opposite directions — the order is dictated by data dependence, and reversing either is a correctness bug, not a performance one.

### `ReplaceOperationRewrite::commit` — where replacement and materialization happen

`ReplaceOperationRewrite::commit(RewriterBase&)` at `0x1c9635a0`. This is the function that turns a *recorded* replacement into a *real* `replaceAllUsesWith`, materializing casts where the replacement's type does not yet match the use:

```c
// ReplaceOperationRewrite::commit(rw)   @ 0x1c9635a0
void commit(ReplaceOperationRewrite* this, RewriterBase& rw):
    Operation* op = this->op;                        // rec+0x18
    SmallVector<Value> replacements;                 // one final value per result

    // 1. resolve the FINAL replacement value for each result of `op`
    for (OpResult r : op->getResults()):             // getNextResultAtOffset
        Value v = findOrBuildReplacementValue(impl, r, typeConverter);  // 0x1c94fde0
        replacements.push_back(v);                    //   -- may INSERT a cast (below)

    // 2. rewire all uses: real replaceAllUsesWith via the listener
    listener.replaceAllUsesWith(op, ValueRange(replacements));  // [listener_vtable+0x38]

    // 3. unlink op from its parent block (the erase itself is deferred to cleanup)
    //    (reportFatalInternalError if op has no parent block)
    op->parentBlock.removeNodeFromList(op);
```

The decompile (`0x1c9635a0`) shows the per-result loop calling `findOrBuildReplacementValue(v3[2]=Impl, result, v3[4]=typeConverter)` (line 106), assembling a `ValueRange` over the resolved values (line 116), invoking the listener's replace-all-uses at `[vtable+0x38]` (line 118), and finally unlinking the op (lines 207-217, with a fatal error if `op->parentBlock` is null at line 209).

### `findOrBuildReplacementValue` — the cast materialization point

`findOrBuildReplacementValue(Value, TypeConverter*)` at `0x1c94fde0` is the function that bridges a still-unconverted type. For a given result value it:

```c
// findOrBuildReplacementValue(value, typeConverter)   @ 0x1c94fde0
Value findOrBuildReplacementValue(Impl* this, Value value, TypeConverter* tc):
    // 1. look the value up in the ConversionValueMapping (typed lookup first)
    if (mapped = lookupOrNull(value, /*desiredType=*/value.getType())):
        return mapped;                               // already have a matching replacement
    // 2. untyped lookup: the latest mapping regardless of type
    vals = lookupOrNull(value, /*any type*/);
    // 3. if no usable replacement exists, BUILD an unresolved materialization cast
    insertPt = computeInsertPoint(vals)  or  value.getParentBlock();
    cast = buildUnresolvedMaterialization(this, /*kind=*/1, insertPt, ...,   // 0x1c94dcc0
                                          loc, vals, /*outputType=*/value.getType(), tc);
    return cast.getResult(0);                        // a builtin.unrealized_conversion_cast result
```

The decompile shows the typed `lookupOrNull` (line 62), the untyped fallback (line 119), and — when neither yields a usable value — the `buildUnresolvedMaterialization(a1, 1u, ...)` call (line 206) returning the cast's first result (line 222). The `1u` is the materialization *kind* (source/target). The produced cast is itself logged as an `UnresolvedMaterializationRewrite`, so it participates in rollback like any other record. These casts are the "unresolved" bridges that a later `reconcileUnrealizedCasts` pass (or the DMA-bridge consumer) resolves.

### The 1:N replace path

The two replacement entry points differ by arity:

| Entry point | VA | Signature | Arity |
|---|---|---|---|
| `replaceOp` | `0x1c950540` | `replaceOp(Operation*, SmallVector<SmallVector<Value,6>,1>&&)` | **1:N** (each result → a *vector* of values) |
| `replaceValueUses` | `0x1c94f740` | `replaceValueUses(Value, ValueRange, TypeConverter*, function_ref)` | 1:1 (one value → a value range) |

The 1:N path is `replaceOp` (mangled `replaceOp(Operation*, llvm::SmallVector<llvm::SmallVector<mlir::Value,6u>,1u>&&)` — confirmed). It is the substrate behind MLIR's `replaceOpWithMultiple`, used when one source op's single result expands into several values (the `TupleType`/`I32PairType` 1:N type conversions). Decoded:

```c
// replaceOp(op, SmallVector<SmallVector<Value>>&& newValues)   @ 0x1c950540
void replaceOp(Impl* this, Operation* op, SmallVector<SmallVector<Value>>&& vals):
    if (recordingMode == 1):                          // [OperationConverter+0x29]
        // 1. map EACH original result to ITS vector of replacement values
        for (i = 0; i < op->numResults; ++i):
            OpResult r = op->getResult(i);            // getNextResultAtOffset
            ConversionValueMapping::map(this->valueMapping, r, vals[i]);  // 1:N map
        // 2. push a ReplaceOperationRewrite record (32-byte hdr + saved op)
        rec = operator new(0x28); rec.tag = 7; rec.vtable = 0x21c23748;
        rec.Impl = this; rec.op = op;
        log.push_back(rec);                           // append to [Impl+0x48]/[+0x50]
        // 3. walk the now-detached op's region, recording produced ops ($_2)
        walk(op, replaceOp::$_2);
    else:
        // pass-through (non-recording): build replacement ValueRange, real replaceOp
        getReplacementValues(...); rw.replaceOp(op, range);
```

The decompile confirms the recording branch (line 40, `if (*(_BYTE*)(a1[47]+41))`): the per-result loop (lines 52-71) calls `ConversionValueMapping::map<SmallVector<Value,2>,SmallVector<Value,2>>` — the **value-vector-to-value-vector** map that is the heart of 1:N — then `operator new(0x28)` (40 bytes) allocates the `ReplaceOperationRewrite` (tag `7`, vtable address-point `0x21c23748`) and appends it to the log (lines 73-118). The non-recording branch (lines 127-150) builds a `ValueRange` and forwards to `RewriterBase::replaceOp`.

> **QUIRK — 1:N replacement is recorded as a value-*vector* mapping, but `commit` collapses it to one value per result.** During pattern application, result `r` maps to a `SmallVector<Value>` (possibly several). At commit, `findOrBuildReplacementValue` resolves that to a *single* replacement value — if `r` mapped to multiple values of different types than the uses expect, it builds a materialization cast that takes the value vector and produces one value of the original type. So the 1:N expansion lives in the *mapping* and is reconciled into the SSA graph by cast materialization at commit, not by leaving multiple defs for one use. This is the precise mechanism by which `tpu` 1:N lowerings (Iota → vlaneseq + index_cast; see [LowerToMlo DMA Bridge](lower-to-mlo-dma-bridge.md)) stay type-correct through conversion.

---

## How the Log Is Populated

### Purpose

Close the loop: which listener method pushes which record. This is the append origin the [DialectConversion Legalizer](dialect-conversion-legalizer.md) drives implicitly every time a pattern mutates IR.

### `notifyOperationInserted` — create vs move

`ConversionPatternRewriterImpl::notifyOperationInserted(Operation*, InsertPoint)` at `0x1c950260` is the single listener for op insertion. It pushes one of two record types depending on whether the op already had a parent (a *move*) or is freshly inserted (a *create*):

```c
// notifyOperationInserted(op, insertPoint)   @ 0x1c950260
void notifyOperationInserted(Impl* this, Operation* op, InsertPoint ip):
    if (recordingMode != 1):
        realListener.notifyOperationInserted(op, ip); // pass-through
        return;

    if (op had a previous parent block):              // ip.priorBlock != null
        rec = operator new(0x30);                     // MOVE record (48 B)
        rec.tag = 5; rec.vtable = 0x21c236f8;         // MoveOperationRewrite
        rec.Impl = this; rec.op = op;
        rec.previousBlock = ip.block; rec.insertBeforeOp = ip.beforeOp;  // +0x20/+0x28
    else:
        ignoredOps/replacedOps bookkeeping ...
        newlyCreatedOps.insert(op);                   // [Impl+0xd8] produced-op set
        rec = operator new(0x20);                     // CREATE record (32 B)
        rec.tag = 8; rec.vtable = 0x21c236a8;         // CreateOperationRewrite
        rec.Impl = this; rec.op = op;
    log.push_back(rec);                               // append to [Impl+0x48]; ++[Impl+0x50]
```

The decompile pins both: the move path (lines 54-95, `operator new(0x30u)`, tag `5`, vtable `off_21C236F8`, saving `a2` at `+0x20` and the insert-before op at `+0x28`) and the create path (lines 112-153, `operator new(0x20u)`, tag `8`, vtable `off_21C236A8`, with the `newlyCreatedOps` `lookupOrInsertIntoBucket` at `_RBX+216` = `Impl+0xd8`). Both append to the log SmallVector at `_RBX+72`/`+80` (= `Impl+0x48`/`+0x50`).

### The full append map

| Rewriter call | Listener / method | VA | Record pushed |
|---|---|---|---|
| `create<Op>()` (fresh insert) | `notifyOperationInserted` | `0x1c950260` | `CreateOperationRewrite` (32 B) |
| move op into block | `notifyOperationInserted` (prior parent) | `0x1c950260` | `MoveOperationRewrite` (48 B) |
| `replaceOp(op, vecs)` (1:N) | `replaceOp` | `0x1c950540` | `ReplaceOperationRewrite` (40 B) |
| `replaceUsesOfWith` (1:1) | `replaceValueUses` | `0x1c94f740` | `ReplaceValueRewrite` |
| `modifyOpInPlace` | start/finalize modify | — | `ModifyOperationRewrite` |
| type-mismatch bridge | `buildUnresolvedMaterialization` | `0x1c94dcc0` | `UnresolvedMaterializationRewrite` |
| region signature convert | `convertRegionTypes` | `0x1c94e7a0` | `BlockTypeConversionRewrite` |
| `inlineBlockBefore` | inline-block | — | `InlineBlockRewrite` |

---

## Function Map

| Function | VA | Role | Confidence |
|---|---|---|---|
| `ConversionPatternRewriter` ctor | `0x1c9512a0` | builds the 0x1e0-byte Impl (`operator new(0x1e0)`); field init | CONFIRMED |
| `ConversionPatternRewriterImpl::undoRewrites` | `0x1c94d060` | LIFO rollback: pop log to `numToKeep`, call each `rollback()` | CONFIRMED |
| `ConversionPatternRewriterImpl::resetState` | `0x1c95bf60` | checkpoint rollback: `undoRewrites` + ignored/replaced rewind | CONFIRMED |
| `ConversionPatternRewriterImpl::applyRewrites` | `0x1c94c1c0` | FORWARD two-pass commit (`commit` then `cleanup`-erase) | CONFIRMED |
| `ConversionPatternRewriterImpl::notifyOperationInserted` | `0x1c950260` | push Create (32 B) / Move (48 B) record on insert | CONFIRMED |
| `ConversionPatternRewriterImpl::replaceOp` (1:N) | `0x1c950540` | map each result → value-vector; push `ReplaceOperationRewrite` | CONFIRMED |
| `ConversionPatternRewriterImpl::replaceValueUses` (1:1) | `0x1c94f740` | push `ReplaceValueRewrite` | HIGH |
| `ConversionPatternRewriterImpl::buildUnresolvedMaterialization` | `0x1c94dcc0` | insert `builtin.unrealized_conversion_cast`; push materialization record | HIGH |
| `ConversionPatternRewriterImpl::findOrBuildReplacementValue` | `0x1c94fde0` | resolve final replacement; build cast on type mismatch | CONFIRMED |
| `ReplaceOperationRewrite::rollback` | `0x1c963500` | erase per-result entries from `ConversionValueMapping` | CONFIRMED |
| `ReplaceOperationRewrite::commit` | `0x1c9635a0` | `findOrBuildReplacementValue` per result + `replaceAllUsesWith` | CONFIRMED |
| `ReplaceOperationRewrite::cleanup` | `0x1c963920` | deferred `eraseOp` of the replaced op (slot4) | CONFIRMED |
| `CreateOperationRewrite::rollback` | `0x1c962f60` | detach the inserted op (`removeNodeFromList`) | CONFIRMED |
| `MoveOperationRewrite::rollback` | `0x1c9630c0` | `transferNodesFromList` back to saved block/before-op | CONFIRMED |
| `ModifyOperationRewrite::rollback` | `0x1c964860` | restore saved loc/attrs/operands/successors/regions | CONFIRMED |
| `UnresolvedMaterializationRewrite::rollback` | `0x1c95b660` | un-map cast results + prune `unresolvedMaterializations` + erase cast | CONFIRMED |
| `IRRewrite::~IRRewrite` (D1, shared slot0) | `0x1c964300` | record dtor | CONFIRMED |
| `IRRewrite::commit` (base no-op) | `0x1c95b760` | bare `ret` (inherited by 2 leaves: UnresolvedMaterialization, InlineBlock) | CONFIRMED |
| `IRRewrite::cleanup` (base no-op) | `0x1c95b780` | bare `ret` (inherited by 9 leaves; only EraseBlock + ReplaceOperation override) | CONFIRMED |
| `ConversionValueMapping::erase` | `0x1c95b7a0` | drop a value→replacement mapping (used by rollbacks) | HIGH |
| `mallocForGrow` (SmallVector) | `0x208d1820` | log realloc-down on pop | HIGH |

---

## What Is Not On This Page

- **The depth recurrence and the legality model.** *Which* pattern fires, in *what* order, and *which* ops are legal is owned by [DialectConversion Legalizer](dialect-conversion-legalizer.md) (the `computeOpLegalizationDepth` recurrence, the `ConversionTarget` `LegalizationAction` enum, the three TPU passes' legality declarations). This page owns only the rewrite-log + rollback + 1:N + materialization substrate that the legalizer drives.
- **The `legalizeWithPattern` lambdas** (`$_0` canApply `0x1c95c100`, `$_1` onSuccess `0x1c95c1a0`, `$_2` recurse `0x1c95c6e0`) — the harness that *calls* `resetState`/`undoRewrites` on the recurse/failure arms. These belong to the legalizer driver; this page documents the primitives they invoke, not the harness.
- **What each `tpu` op lowers to.** The per-op `matchAndRewrite` bodies (Iota, DeviceId, the DMA bridge casts, the 1:N expansions) are owned by [LowerToMlo DMA Bridge](lower-to-mlo-dma-bridge.md), [tpu → LLO Lowering](tpu-to-llo-ods.md), and [LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md).
- **The type converter.** `registerConversion`/`registerTypeAttributeConversion` and the 1:N `TupleType`/`I32PairType` type rules are on [SparseCore Type Converter](sc-type-converter.md); this page documents only how a 1:N *result replacement* is recorded and committed, not how the *types* are computed.
- **The block-level rollback bodies** (`BlockTypeConversionRewrite` `0x1c9629e0`, `InlineBlockRewrite` `0x1c962920`, `CreateBlockRewrite` `0x1c9641e0`, `MoveBlockRewrite` `0x1c964340`, `EraseBlockRewrite` `0x1c963fc0`). Vtables and addresses are pinned (Table above); the bodies are HIGH (not decompiled) — region signature / SCF structural conversion undo. Out of scope here.
- **`reconcileUnrealizedCasts`** — the end-of-conversion pass that resolves the surviving `builtin.unrealized_conversion_cast` bridges. This page documents how casts are *created and logged*; how they are later *eliminated* is a separate pass.
- **The `ReplaceValueRewrite` vs `ReplaceOperationRewrite` selection at every replace site** (`replaceOp` `0x1c950540` vs `replaceValueUses` `0x1c94f740`) — both append records; which is chosen per replace-mode is inferred from the signatures (1:N vs 1:1), not branch-pinned at every call site (**LOW** on the exhaustive selection rule).

---

## Cross-References

- [DialectConversion Legalizer](dialect-conversion-legalizer.md) — the depth-aware driver that captures a `RewriterState` checkpoint, applies a candidate pattern, recurses, and calls `resetState`/`undoRewrites` (rollback) or `applyRewrites` (commit) through this engine.
- [LowerToMlo DMA Bridge](lower-to-mlo-dma-bridge.md) — the `tpu → sparse_core` pattern bodies whose 1:N replacements and DMA-bridge `unrealized_conversion_cast`s are recorded and committed here.
- [LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md) — the `sparse_core → LLVM` pass, the heaviest 1:N type path (`I32PairType → 2×i32`, `TupleType → element types`) exercising the value-vector replacement map.
- [SparseCore Type Converter](sc-type-converter.md) — the 1:N type conversion rules behind the value-vector mappings this rewriter records.
- [tpu → LLO Lowering](tpu-to-llo-ods.md) — the TensorCore lowering, predominantly 1:1, whose patterns drive the same recording rewriter.
- [The TPU Compiler](overview.md) — the five-phase dialect descent in which all of these conversions run.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part V — Compiler: Lowering & Optimization Passes / MLIR lowering chain — [back to index](../index.md)
