# The `bir::Instruction` Base Struct

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, the cp310 build of `libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`, sha256 `2387d0d4…dc5a4cfa`). The cp311/cp312 builds carry the identical ABI but their virtual addresses drift; offsets and ordinals are version-stable.*

## Abstract

`bir::Instruction` is the single polymorphic root of the BIR (Backend IR) — the L1 instruction representation that `libBIR.so` defines, the simulator interprets, and the `birverifier` checks. Every one of the 110 `InstructionType` opcodes is an `Inst<Name>` class that *derives* from `bir::Instruction` and only **appends** its own operands above the shared header; the base bytes are byte-identical across all opcodes. This page reconstructs that base object in full — from the vtable pointer at offset 0, through the inline header, to the heap-allocated scheduling/dependency block hung off `+0xD0` — and enumerates the 19 JSON keys the common header serializes.

The object is physically split in two. Part (A) is the **inline header**, `+0x00 .. +0xD8`: the vptr, the `bir::NamedObject` CRTP sub-object (name / origin / optin-mask / unique-id), the `InstructionType` tag at `+0x58`, the loopnest and predicate vectors, the engine assignment, the three intrusive operand lists, and a single pointer at `+0xD0`. Part (B) is a separately heap-allocated **scheduling/dependency block** of `0xA98` (2712) bytes — "Storage/DepInfo" — that holds the dependency edge lists, the symbolic schedule order, the resolved schedule cycles, the sync info, and the debug provenance. The constructor `operator new`s the block and stores it at `+0xD0`; no opcode overrides this split.

BIR is not built on LLVM's `Value`/`User` model, but it borrows two LLVM data structures wholesale: the operand lists are `llvm::ilist`-style intrusive circular sentinels, and each dependency edge is an `llvm::PointerIntPair<Instruction*, 3>` that packs the target pointer and a 3-bit `EdgeKind` tag into one word. A reimplementer who knows those two structures and the table below can rebuild the node.

The page is structured as: the inline header layout ([§1](#1-the-inline-header--0x00--0xd8)), the `+0xD0` block layout ([§2](#2-the-0xd0-schedulingdependency-block)), the constructor as annotated pseudocode ([§3](#3-the-constructor)), the 19-key JSON header ([§4](#4-the-json-header--19-keys)), the dependency-edge model ([§5](#5-dependency-edges-the-pointerintpair-model)), the subclass graph and the 113-vs-401 reconciliation ([§6](#6-the-subclass-graph)), and the shared vtable ([§7](#7-the-shared-vtable)).

### Reimplementation contract

To rebuild a BIR node a reimplementer must reproduce:

- The **two-part object split**: an inline header ending near `+0xD8` plus a `0xA98`-byte heap block referenced at `+0xD0`. Opcodes append fields starting near `+0xF0`.
- The **`InstructionType` tag at `+0x58`** as the single dispatch key. Visitor dispatch, equality (`sameInst`), the JSON factory, and the verifier all switch on this int — there is no C++ `accept()` virtual.
- The **three intrusive operand sentinels** at `+0xA0`/`+0xB0`/`+0xC0` (`ins` / `indirection_ins` / `outs`), self-initialized empty.
- The **dependency model**: a `PointerIntPair<Instruction*,3>` edge node, with normal dependencies and loop-carried dependencies as *separate list heads in the block* but FLOW/ANTI/OUTPUT/ORDERED kinds distinguished by the *3-bit tag on each node*, not separate heads.
- The **19-key JSON header** emitted by `Instruction::toJson`, in order, with the per-key emit guards.

| | |
|---|---|
| **Defining binary** | `libBIR.so` (cp310), 22151 functions |
| **Base ctor** | `bir::Instruction::Instruction(string&, BasicBlock*, InstructionType)` @ `0x2dafb0` |
| **Common JSON emit** | `bir::Instruction::toJson(json&)` @ `0x2e2ea0` |
| **Inline header size** | `~0xD8` bytes (first op field near `+0xF0`) |
| **`+0xD0` block size** | `0xA98` (2712) bytes, heap, `operator new` in ctor |
| **IT tag offset** | `+0x58` (`InstructionType`, 0..109) |
| **vtable** | `_ZTVN3bir11InstructionE` @ `0x8fced8`; vptr = symbol `+0x10` = `0x8fcee8` |
| **Opcodes** | 110 `InstructionType` values; 6 are abstract bases |
| **Polymorphic Inst classes** | 113 distinct `_ZTSN3bir…Inst*` typeinfo strings |
| **Source path (asserts)** | `neuronxcc/walrus/ir/lib/IR/Instruction.cpp` |

> **NOTE —** IDA's `structures.json` for `libBIR.so` contains only ELF/libc types — no `bir::*` struct. The layout below is reconstructed from the constructor's stores and the accessor/serializer reads, every offset anchored to one of them. There is no struct definition in the binary to copy; this table *is* the definition.

---

## 1. The Inline Header — `+0x00 .. +0xD8`

All offsets are byte offsets into `this`. Each row is pinned to a constructor store (ctor @ `0x2dafb0`, [§3](#3-the-constructor)) and, where one exists, a named accessor or the `toJson` read.

| Field | Offset | Type | Meaning / JSON key | Anchor | Conf |
|---|---|---|---|---|---|
| `vptr` | `+0x00` | `void**` | vtable ptr (`_ZTVN3bir11InstructionE + 0x10`); slot `+0x60` = `getValidEngines` | ctor `*this = off_8FCEE8` | CERTAIN |
| *(NamedObject hdr)* | `+0x08` | `qword[2]` | `NamedObjectBase` holder slot (zeroed) | ctor `*(OWORD*)(this+24)-1 = 0` | HIGH |
| `name.data` | `+0x18` | `char*` | `std::string` name data ptr (SSO → `this+0x28`) | ctor `this+0x18 = this+0x28` | CERTAIN |
| `name.size` | `+0x20` | `qword` | name length | ctor (`sub_2D3970`) | CERTAIN |
| `name.sso` | `+0x28` | `char[16]` | SSO buffer (cap 15) | ctor | CERTAIN |
| `origin` | `+0x38` | `int` (`NamedObjectOrigin`) | `"origin"` — `{Internal,Penguin,NKI}` | ctor `*(this+0x38) = 0`; `toJson [a1+56]` | CERTAIN |
| `optin_passes_mask` | `+0x40` | `qword` (bitset) | `"optin_passes"` bitmask | ctor `*(this+0x40) = 15`; `optinPassesToJson` | HIGH |
| `unique_id` | `+0x48` | `int` | unique object id (`-1` sentinel → `getUniqueId`) | ctor `*(this+0x48) = -1` then `= UniqueId` | CERTAIN |
| `parent` | `+0x50` | `bir::BasicBlock*` | owning basic block (`a3`) | ctor `*(this+0x50) = a3` | CERTAIN |
| `IT tag` | `+0x58` | `int` (`InstructionType`) | `"opcode"` (via `InstructionType2string`) | ctor `*(this+0x58) = a4`; `toJson [a1+88]` | CERTAIN |
| *(pad)* | `+0x5C` | `int` | alignment pad | — | HIGH |
| `loopnest` | `+0x60` | `vector<LoopAxis*>` | `"loopnest"` — begin/end/cap | ctor `OWORD@+0x60 = 0`; `getLoopnest = this+0x60` | CERTAIN |
| `predicates` | `+0x78` | `vector<AffinePredicate>` | `"predicates"` (record stride 40) | ctor `OWORD@+0x70/+0x80 = 0` | CERTAIN |
| `engine` | `+0x90` | `int` (`EngineType`) | `"engine"` (current engine assignment) | ctor `qword@+0x90 = 0`; `setEngineId` idx36; `toJson *(p)+144` | CERTAIN |
| `engine_id` | `+0x94` | `uint32` | `"engine_id"` (per-engine instance index) | `setEngineId` writes idx37; `toJson *(p)+148` | CERTAIN |
| *(scratch)* | `+0x98` | `int` (=0) | reserved | ctor `*(this+0x98) = 0` | HIGH |
| `ins` HEAD | `+0xA0` | circular-dlist `{next,prev}` | `"ins"` — `InstructionArgumentType` 0 | ctor `movddup self@+0xA0`; `toJson this+160` | CERTAIN |
| `indirection_ins` HEAD | `+0xB0` | circular-dlist `{next,prev}` | `"indirection_ins"` — IAT 1 | ctor `movddup self@+0xB0`; `toJson this+176` | CERTAIN |
| `outs` HEAD | `+0xC0` | circular-dlist `{next,prev}` | `"outs"` — IAT 2 | ctor `movddup self@+0xC0`; `toJson this+192` | CERTAIN |
| **block ptr** | `+0xD0` | `Storage/DepInfo*` | pointer to the `0xA98` heap block ([§2](#2-the-0xd0-schedulingdependency-block)) | ctor `*(this+0xD0) = new(0xA98)` | CERTAIN |
| *(tail scratch)* | `+0xD8` | `qword[3] = 0` | ctor zeroes `+0xD8/+0xE0/+0xE8`; op fields follow | ctor | HIGH |

The base header is opcode-independent. The first op-specific field begins after `+0xD8`: the matmul family's first `MaybeAffine` is at `+0xF0`, `InstDMA`'s `DGEType` at `+0xF8`. So `+0xD8 .. +0xEF` is a subclass-dependent gap before the per-op payload.

> **CORRECTION —** an earlier survey placed `engine`/`engine_id` (`+0x90`/`+0x94`) inside `bir::InstDMA`. They are **base** `Instruction` fields inherited by all 110 opcodes: the ctor zeroes `qword@+0x90`, `setEngineId` (@ `0x2d6ab0`) reads idx 36 / writes idx 37 on the base class, and the base `toJson` reads both. `InstDMA`'s own first field is `DGEType @ +0xF8`.

### The `NamedObject` CRTP sub-object (`+0x08 .. +0x4F`)

`bir::Instruction` is itself a `bir::NamedObject<bir::Instruction, bir::BasicBlock>` (CRTP), whose typeinfo string `_ZTSN3bir11NamedObjectINS_11InstructionENS_10BasicBlockEEE` is present in the binary. This sub-object supplies the name `std::string` (`+0x18`, SSO-inlined into `+0x28`), the `origin` enum (`+0x38`), the `optin_passes` bitmask (`+0x40`), and the `unique_id` (`+0x48`). The ctor calls `bir::NamedObject<…>::getUniqueId(this+8)` to allocate the id, and — a self-consistency check baked into every construction — *throws* `"Id has already been initialzed"` (sic) if `+0x48` is not still `-1` at that point.

> **GOTCHA —** the `optin_passes` default of `15` (ctor `mov [this+0x40], 15`) is read as the optin bitmask by `optinPassesToJson`, but it has not been cross-checked against an optin-pass *writer*; treat the specific value `15` as HIGH, not CERTAIN.

### The operand sentinels (`+0xA0` / `+0xB0` / `+0xC0`)

Each of the three operand lists is an `llvm::ilist`-style intrusive **circular** doubly-linked sentinel: a `{next, prev}` head node that the ctor seeds with `movddup xmm, &head` so that `next == prev == &head` (empty list). Appending is done by a single templated helper, `addArgumentIndirectionArgumentOrOutput<T>` (@ `0x237780`), keyed by `InstructionArgumentType`: `0 → +0xA0`, `1 → +0xB0`, `2 → +0xC0`. List order is insertion order. The nodes are `bir::Argument` subclasses (`{vptr, next, prev, ArgumentKind, parent Inst*, id}` at `+0x00/+0x08/+0x10/+0x18/+0x20/+0x28`); the operand model is documented in [`bir/value-model.md`](value-model.md).

---

## 2. The `+0xD0` Scheduling/Dependency Block

`Instruction+0xD0` points at a heap aggregate the ctor builds with `operator new(0xA98)` followed by `memset(…, 0, 0xA98)`. It is accessed everywhere as `*((qword*)this + 26) + K` (26 × 8 = `0xD0`). The constant `K` is the literal offset *within the block*. It holds the dependency edge lists, the symbolic and resolved schedule slots, the sync info, and the debug provenance — the per-instruction *scheduling state* that is logically distinct from the IR-structural header.

| Block off | Type | Meaning / JSON key | Anchor | Conf |
|---|---|---|---|---|
| `+0x008` | `qword` count | `numDependencies()` (predecessor count) | `numDependencies` @ `0x2d69c0`: `*(*(this+26)+8)` | CERTAIN |
| `+0x020` | intrusive list head | `"dependencies"` head (predecessor edges) | `toJson` walks `*(blk)+32` (`0x2e31d0`: `lea rbx,[rax+0x20]`); ctor leaves it zeroed | CERTAIN |
| `+0x270` | `qword` head | descendents / successors list head | `clearDescendents` (`v1[78] = 0`) | CERTAIN |
| `+0x4A8` | `qword` guard | `"loop_carried_dependencies"` count/guard | `toJson` `v80[149]` | CERTAIN |
| `+0x4C0` | intrusive list head | `"loop_carried_dependencies"` head | `toJson` walks `*(blk)+1216` | CERTAIN |
| `+0x948`/`+0x950` | `vector<…>` begin/end | `"unroll_dependencies"` | `toJson +2376/+2384` | CERTAIN |
| `+0x960` | `bir::OpDebugInfo` (opt) | `"debug"` / `ir_debug_info`; flag byte `@+0x960` | `getDebugInfo` @ `0x2d82c0`: `*(this+26)+2400`; `toJson` guard `*(blk+2400)` | CERTAIN |
| `+0x9F0` | `bir::SyncInfo` (opt) | `"sync_info"`; payload maps `@+0x9F8`/`+0xA30`; flag `@+0x9F0` | `getSyncInfo` @ `0x2d7dd0`: `*(this+26)+2544`; `setSyncInfo` @ `0x2e49a0` | CERTAIN |
| `+0xA68`/`+0xA70` | `vector<QuasiAffineExpr>` | `"order"` (symbolic multi-dim schedule index) begin/end | `hasOrder` @ `0x2d8ea0` (`blk+2664/+2672`); `toJson` | CERTAIN |
| `+0xA80` | `QuasiAffineExpr*` (cache) | linearized loopnest-expr cache (lazy) | `getLoopnestExpr` @ `0x2d76e0` (`blk+2688`) | CERTAIN |
| `+0xA88` | `int64` | `"scheduled_start"` | `toJson [p+0xA88]`; ctor zeroes | CERTAIN |
| `+0xA90` | `int64` | `"scheduled_end"` | `toJson [p+0xA90]` | CERTAIN |

> **CORRECTION — the dependency edges live in *two* structures, not one intrusive list.** `addDependency(EdgePtr, bool)` @ `0x2e6e90` selects a container base by the `loop_carried` flag (`0x2e6ea4: mov rbp,[rdi+0xD0]` then `0x2e6eb0: lea rdx,[rbp+0x4A8]` / `0x2e6eb7: add rbp,8` / `0x2e6ebe: cmovnz rbp,rdx`): **normal → `blk+0x08`, loop-carried → `blk+0x4A8`**. That base is the header of a `tbb::…::concurrent_unordered_set<bir::EdgePtr>` (the mangled `concurrent_unordered_set_traits<bir::EdgePtr, …EdgeHash, …EdgeEqual>` appears in the call at `0x2e6fb2`): `[base+0]` is the element **count** (`0x2e7056: lock xadd [rbp+0], rdx`), `[base+8]` the segment-table size used as the hash divisor (`0x2e6f9b: mov rcx,[rbp+8]` → `div rcx`), and `[base+0x10]` a `4.0f` max-load-factor compared via `comiss` (`0x2e708d`) — which is the `0x40800000` the ctor writes at `blk+0x18`/`blk+0x4B8` (encoded as the `+0x1A`/word `0x4080` and the explicit `movss`). So the `+0x08`/`+0x4A8` qwords this table calls "count"/"guard" are the **set element counts**, byte-exact. The serialized `"dependencies"`/`"loop_carried_dependencies"` *ordered* lists at `+0x20`/`+0x4C0` are a **separate** intrusive structure that `toJson` walks (`0x2e31d0: lea rbx,[rax+0x20]`, sentinel `byte[rbx+8]&1`; `0x2e3d7a: lea rbx,[rax+0x4C0]`). The OPEN-LEAD suspect ctor stores `+0x40 = blk+0x48` and `lea rcx,[rdx+0x240]` are the concurrent-set's embedded 63-segment bucket array (zeroed `[0x48..0x240)`), **not** the `+0x20` head — this page's four offsets (`+0x08`, `+0x20`, `+0x4A8`, `+0x4C0`) are all correct; only the old `+0x20`-row anchor that cited `+0x40 = blk+0x48` was misattributed.

> **NOTE — symbolic vs resolved schedule.** `"order"` (`+0xA68`, a `vector<QuasiAffineExpr>`) is the *symbolic* multi-dimensional schedule key; `"scheduled_start"`/`"scheduled_end"` (`+0xA88`/`+0xA90`, scalar `int64`) are the *resolved* cycle slots. The pre-scheduling representation carries `order`; the post-scheduling representation fills `scheduled_start/end`. There is no `getHeight`/`getDepth` accessor on `Instruction` — schedule height/depth are computed over the edge lists and the `order` vector, not stored as named base fields.

### What is *not* serialized

The block is mostly scheduler scratch. The ctor seeds several intrusive list/map roots not reached by `toJson` — at `+0x40` (`= blk+0x48`), `+0x290`, `+0x4E0`, `+0x730` (each set to a self-relative root) — and writes a recurring float `1082130432` (`0x40800000` = `4.0f`) at `+0x268`, `+0x4B8`, and `+0x708` (three `movss [rdx+off], xmm0` of `dword_781930`), plausibly a default per-edge weight or schedule priority.

> **CORRECTION —** an earlier draft cited the float slots as `+0x4A0` and `+0x4B8`/`+0x614`. The ctor at `0x2dafb0` instead `movss`-stores `dword_781930` (= `0x40800000` = `4.0f`, dumped byte-for-byte from `.rodata` at file offset `0x79930`) at `+0x268` (`0x2db148`), `+0x4B8` (`0x2db1b8`), and `+0x708` (`0x2db21d`); `+0x4A0` receives a *byte 0* (`0x2db13a`), not the float. Note `0x40800000` is IEEE-754 `4.0f`, **not** `2.0f` (`2.0f` = `0x40000000`); an earlier survey mis-decoded the constant. Only the dependency/loop-carried/unroll lists, `order`, `scheduled_start/end`, `sync_info`, and `debug` reach JSON; the rest is internal state the scheduler passes own.

> **INFERRED —** the precise semantics of the `4.0f` constant and the extra map roots are not pinned to a named accessor. They are scheduling scratch (mobility / slack / weight); naming them exactly needs the scheduler passes that read them.

---

## 3. The Constructor

`bir::Instruction::Instruction(name, parent, IT)` @ `0x2dafb0` is the single point that establishes the layout above. It is short and entirely a sequence of field stores plus the block allocation — no opcode-specific logic. Annotated:

```c
// bir::Instruction::Instruction(string& name, BasicBlock* parent, InstructionType it)
//   @ 0x2dafb0   (a1=this, a2=&name, a3=parent, a4=it)
void Instruction(void* this, string* name, BasicBlock* parent, int it) {
    // --- NamedObject sub-object: name std::string with SSO ---
    *(OWORD*)(this + 0x08) = 0;             // NamedObjectBase holder slot
    *(char**)(this + 0x18) = this + 0x28;   // name.data -> SSO buffer (empty)
    sub_2D3970(this + 0x18, name->data, name->data + name->size);  // assign name

    *(BasicBlock**)(this + 0x50) = parent;  // parent BB
    *(int*)(this + 0x38)         = 0;       // origin = Internal
    *(int*)(this + 0x48)         = -1;      // unique_id sentinel
    *(qword*)(this + 0x40)       = 15;      // optin_passes default mask

    int uid = NamedObject::getUniqueId(this + 0x08);
    if (*(int*)(this + 0x48) != -1)         // self-check: must still be the sentinel
        throw runtime_error("Id has already been initialzed");   // (sic)
    *(int*)(this + 0x48) = uid;

    *(int*)(this + 0x58) = it;              // InstructionType tag  <-- THE dispatch key

    *(OWORD*)(this + 0x60) = 0;             // loopnest vector  (begin/end)
    *(OWORD*)(this + 0x70) = 0;             // loopnest.cap + predicates.begin
    *(OWORD*)(this + 0x80) = 0;             // predicates end/cap
    *(qword*)(this + 0x90) = 0;             // engine + engine_id = 0
    *(int*)(this + 0x98)   = 0;             // scratch

    *this = &_ZTVN3bir11InstructionE[2];    // vptr (= symbol + 0x10)

    // three operand sentinels: next = prev = &head  (movddup of the head's own addr)
    *(m128*)(this + 0xA0) = dup(this + 0xA0);   // ins
    *(m128*)(this + 0xB0) = dup(this + 0xB0);   // indirection_ins
    *(m128*)(this + 0xC0) = dup(this + 0xC0);   // outs

    // --- the heap scheduling/dependency block ---
    void* blk = operator new(0xA98);
    memset(blk, 0, 0xA98);
    *(byte*)(blk + 0x10)  = 8;
    *(word*)(blk + 0x1A)  = 16512;
    *(qword*)(blk + 0x30) = 1;
    *(qword*)(blk + 0x40) = blk + 0x48;     // dependencies list root (self-relative)
    /* … seed the descendents / loop-carried / map roots at +0x290/+0x4E0/+0x730,
       write 4.0f (0x40800000) at the per-edge-weight slots,
       zero the order/sched/sync/debug regions … */
    *(byte*)(blk + 0x960) = 0;              // debug optional = empty
    *(byte*)(blk + 0x9F0) = 0;              // sync_info optional = empty
    *(OWORD*)(blk + 0xA68) = 0;             // order vector empty
    // scheduled_start/end already zero from memset

    *(void**)(this + 0xD0) = blk;           // hang the block off +0xD0
    *(qword*)(this + 0xD8) = 0;             // tail scratch (op payload starts here)
    *(qword*)(this + 0xE0) = 0;
    *(qword*)(this + 0xE8) = 0;
}
```

The throw on a non-`-1` id is not dead code: `getUniqueId` writes `+0x48` through the CRTP sub-object, and the ctor verifies the write landed on a fresh object. It is the only control flow in the ctor besides the block-init loops.

---

## 4. The JSON Header — 19 Keys

`bir::Instruction::toJson(json&)` @ `0x2e2ea0` emits the common header shared by all opcodes; each `Inst<X>::toJson` chains this base first, then appends its own keys. The reverse path is `createFromJsonHelper` @ `0x2e01b0` (opcode string → per-op `createFromJson` → `readFieldsFromJson`). The keys below are emitted in this exact order, each pinned to its source offset and emit guard in the `toJson` body.

| # | JSON key | Source | Type | Emitted when | Conf |
|---|---|---|---|---|---|
| 1 | `"origin"` | INLINE `+0x38` | `NamedObjectOrigin` | always | CERTAIN |
| 2 | `"opcode"` | INLINE `+0x58` (IT) | `InstructionType2string` | always | CERTAIN |
| 3 | `"engine"` | INLINE `+0x90` | `EngineType` | always | CERTAIN |
| 4 | `"engine_id"` | INLINE `+0x94` | `uint32` | always | CERTAIN |
| 5 | `"scheduled_start"` | BLK `+0xA88` | `int64` | always | CERTAIN |
| 6 | `"scheduled_end"` | BLK `+0xA90` | `int64` | always | CERTAIN |
| 7 | `"optin_passes"` | INLINE `+0x40` | `array<string>` (bit→passname) | always | CERTAIN |
| 8 | `"dependencies"` | BLK `+0x20` list | `array<{id,kind}>` (`EdgeKind & 7`) | `numDependencies != 0` | CERTAIN |
| 9 | `"loop_carried_dependencies"` | BLK `+0x4C0` list (guard `+0x4A8`) | `array<{id,kind}>` | list non-empty | CERTAIN |
| 10 | `"unroll_dependencies"` | BLK `+0x948`/`+0x950` | `array<…>` | `begin != end` | CERTAIN |
| 11 | `"sync_info"` | BLK `+0x9F0` | object (`SyncInfo::toJson`) | flag byte `@+0x9F0` set | CERTAIN |
| 12 | `"ins"` | INLINE `+0xA0` list | `array<Argument>` | always (empty OK) | CERTAIN |
| 13 | `"indirection_ins"` | INLINE `+0xB0` list | `array<Argument>` | list non-empty | CERTAIN |
| 14 | `"outs"` | INLINE `+0xC0` list | `array<Argument>` | always | CERTAIN |
| 15 | `"debug"` | BLK `+0x960` | object (`OpDebugInfo::to_json`) | flag byte `@+0x960` set | CERTAIN |
| 16 | `"loopnest"` | INLINE `+0x60` vector | `array<string>` (LoopAxis names) | `begin != end` | CERTAIN |
| 17 | `"predicates"` | INLINE `+0x78` vector | `array<AffinePredicate>` (stride 40) | `begin != end` | CERTAIN |
| 18 | `"order"` | BLK `+0xA68`/`+0xA70` vector | `array<QuasiAffineExpr>` | `begin != end` | CERTAIN |
| 19 | `"dma_trigger_debug_update_semaphore_id"` | per-op (IT == 67) | — | `IT == InstDMATrigger` | HIGH |

The first 18 keys are the genuine base header. Key 19 is emitted by the *base* `toJson` body via an `IT == 67` guard (`if (*(int*)(*(this)+0x58) == 67)`), reading a `DMABlock` operand through `InstDMATrigger::getDmaBlock` rather than a base field — it lives in the base serializer only because the DMATrigger debug hook is wired there. Op-specific keys proper (e.g. the matmul family's 13 keys at `+0xF0..+0x328`) are appended by the leaf `toJson` *after* this base returns.

> **NOTE —** the four short keys `"opcode"`, `"engine"`, `"ins"`, `"outs"`, `"order"` do not appear as standalone entries in the strings table because nlohmann constructs them as inline `operator[]` key literals; they are confirmed by the `lea`-string operands in the `toJson` body at the line offsets above (e.g. the `"dependencies"` key is corroborated by the adjacent assert `"'dependencies' must be a JSON array"`).

---

## 5. Dependency Edges: the PointerIntPair Model

Each dependency edge is an `llvm::PointerIntPair<bir::Instruction*, 3>`: a single machine word whose low 3 bits hold the `EdgeKind` and whose high bits (mask `~7`) hold the target `Instruction*`. This is proven byte-exact by the asserts in `addDependency(Instruction*, EdgeKind, bool)` @ `0x2e7640`:

```c
// bir::Instruction::addDependency(Instruction* former, EdgeKind kind, bool loop_carried)  @ 0x2e7640
int addDependency(Instruction* this, Instruction* former, char kind_and_lc) {
    assert(this != former && "this != former");                 // Instruction.cpp:0x150
    assert((former & 7) == 0  && "Pointer is not sufficiently aligned");   // PointerIntPair.h:0xCA, IntBits=3
    assert((kind  & 0xF8) == 0 && "Integer too large for field"); // PointerIntPair.h:0xD1, IntMask=~7
    // append { former | kind } to the dependencies list (loop_carried selects the head)
    …
}
```

The edge node layout (in either list) is `{ next@+0x00, byte&1 sentinel-flag @+0x08, EdgePtr @+0x10 }`. `toJson` decodes each node as `EdgeKind2string(*(node+0x10) & 7)` for the kind and `*(node+0x10) & ~7` for the target.

> **GOTCHA — kinds are tags, lists are heads, and they are orthogonal.** There are *two* edge list heads in the block — normal **`dependencies`** (head `+0x20`, count `+0x08`) and **`loop_carried_dependencies`** (head `+0x4C0`, guard `+0x4A8`) — chosen by `addDependency`'s `loop_carried` argument. The four edge *kinds* — `EdgeKind = {Invalid 0, Ordered 1, Anti 2, Output 3, Flow 4}` — are **not** separate heads; they are the 3-bit tag on each node within whichever list. FLOW vs ANTI vs OUTPUT is a per-edge property; normal vs loop-carried is a per-list property. *(The `EdgeKind` value→name mapping is STRONG — recovered from the `EdgeKind2string` switch — not pinned to a named enum, since IDA carries no `EdgeKind` enum.)*

The `+0x270` head holds the reverse **descendents/successors** list, maintained alongside the predecessor edges and cleared by `clearDescendents`.

---

## 6. The Subclass Graph

There is exactly **one** polymorphic root, `bir::Instruction` (`_ZTSN3bir11InstructionE`). The tree is two levels deep for most opcodes, three for five families, and four for two chains. Six `InstructionType` values are **abstract intermediate bases** — never instantiated directly, their concrete children carry the real opcode:

| IT | Abstract base | Family / accept-set | Children |
|---|---|---|---|
| 0 | `InstGeneric` | `{≤3}` | `InstGenericCopy`(1), `InstGenericRelu`(2), `InstEventSemaphore`(13) → `InstAbstractCopy`(3) |
| 7 | `InstMatmultBase` | `{7,8,9,95}` | `InstMatmult`(8), `InstMatmultSparse`(9), `InstMatmultMx`(95) |
| 18 | `InstDMA` | `{18,19,22,32,41-46,67}` | `InstLoad`(19) → `InstSave`(22), `InstDMACopy`(32), `InstDMATrigger`(67), `InstTensorLoad`(75), `InstTensorSave`(76) |
| 47 | `InstCollective` | `{47,48,49,50}` | `InstCollectiveCompute`(48), `InstCollectiveSend`(49), `InstCollectiveRecv`(50) |
| 68 | `InstDMADescriptor` | `{68-72}` | `…Copy`(69), `…CCE`(70), `…Transpose`(71), `…Replicate`(72) |
| 77 | `InstTerminator` | `{77,78,79,81,82,83}` | `CompareAndBranch`(78), `UnconditionalBranch`(79), `Return`(81), `Exit`(82), `Break`(83) |

These six are exactly the ITs that route to the *base* `visitInstruction` in the simulator and have family-level bodies in the verifier — three independent confirmations that they are abstract. The two deep chains, both confirmed by ctor base-calls: `Instruction → InstGeneric → InstEventSemaphore → InstAbstractCopy` and `Instruction → InstDMA → InstLoad → InstSave` (where `InstSave::readFieldsFromJson` *tail-jumps* to `InstLoad`'s — "Save = Load on the wire"). All other ~90 opcodes derive directly from `bir::Instruction`. The full leaf→opcode crosswalk is the `InstructionType` table; see [`bir/instruction-type.md`](instruction-type.md).

> **QUIRK — `InstDMADescriptor` is NOT under `InstDMA`.** Despite the name, the descriptor family (IT 68–72) derives directly from `Instruction`, not from `InstDMA`: its ctor calls `Instruction::Instruction` directly and the subtree has no `dge_type`/`queue` field. Likewise `InstQuantizeMx`(96) is a *sibling* of the matmul family, not a `InstMatmultBase` child. And `InstBranchHint`(80) is a control op that is *excluded* from terminator-class equality — its base edge is STRONG-not-confirmed.

### The 113-vs-401 reconciliation

A naive count of typeinfo-name *hits* mentioning a `bir::Inst` type returns ~401. That is not the class count. The actual polymorphic population, counted as distinct `_ZTSN3bir…Inst*` typeinfo **strings** in the RTTI table, is **113**:

| Axis | Count | Source |
|---|---|---|
| Distinct `_ZTS` bir-Inst typeinfo strings | **113** | `rtti.json` (`rg '^_ZTSN3bir.*Inst' \| sort -u`) |
| of which: root + CRTP base | 2 | `InstructionE`, `NamedObject<Instruction,BasicBlock>E` |
| of which: abstract intermediate bases | 6 | Generic, MatmultBase, DMA, Collective, DMADescriptor, Terminator |
| ⇒ concrete leaf classes (own typeinfo) | **105** | matches the 105 `validEngines` maps ([§7](#7-the-shared-vtable)) |

The inflation from 113 to 401 is (a) the per-class RTTI **triplet** — `_ZTV` vtable / `_ZTI` typeinfo / `_ZTS` type-string (~3×) — and (b) template instantiations whose mangled names merely *mention* an `Inst` type (`ilist_iterator<…Instruction…>`, `DenseSet<Instruction const*>`, the ~60 `bir::Hwm::getLatency(InstX&)` overloads, the `visitInst<X>` method symbols). None of those are subclasses. The hard floor is independent: `getValidEngines` is overridden by ~105 concrete leaves, and there are 113 typeinfo strings — both an order of magnitude below 401.

> **NOTE — the `113` here is the *loose-typeinfo* lens.** The 113-string count (`_ZTSN3bir.*Inst`, including the `NamedObject<Instruction,…>` template) and the strict-RTTI-triplet count (`112`, `_ZTSN3bir[0-9]+Inst…E` only) are two of six interlocking lenses on one ~112-class population. The full **121 / 113 / 112 / 110 / 104–105** decomposition — with the nine non-IR symbol-token contaminants enumerated — lives in the canonical reconciliation table at [cross-language wire-keys §2](cross-language-wirekeys.md#2-the-birinst-class-family-and-the-121--113--110--105-reconciliation). Use **112** for the polymorphic-class population, **110** for opcodes, **104–105** for concrete leaves; this page's `113 ⇒ 105` is the loose-grep route to the same floor.

---

## 7. The Shared Vtable

The vtable is `_ZTVN3bir11InstructionE` @ `0x8fced8`; the object's vptr is the symbol `+0x10` (= `0x8fcee8`, past the Itanium `{offset-to-top=0, &typeinfo}` header). Every leaf installs a same-shaped vtable with the same slot meanings. The single hard, reloc-proven slot is **`getValidEngines` at vt+0x60** (slot 12): on `InstMatmult`'s vtable the slot relocates to `_ZNK3bir11InstMatmult15getValidEnginesEv`, and each of ~105 concrete leaves overrides it with an 8-byte stub returning its own `::validEngines` table. The matmul family alone shows the override on `InstMatmult`, `InstMatmultSparse`, `InstMatmultMx`, **and** the abstract `InstMatmultBase`.

The central design point is that the vtable is *small*: most per-opcode behavior is reached not through the vptr but through the `InstructionType` switch at `+0x58`.

| Dispatch | Mechanism | Anchor | Conf |
|---|---|---|---|
| `getValidEngines` | **virtual**, vt+0x60 | reloc on `InstMatmult` vt | CERTAIN |
| `toJson` | **virtual** (leaf chains base) | base @ `0x2e2ea0` | CERTAIN |
| `getDebugInfoSource` | **virtual**, slot 2 | — | STRONG |
| destructor + Itanium pair | **virtual** | — | HIGH |
| visitor `visit`/`accept` | **opcode switch** on `+0x58` (110 arms) | sim + verifier dispatch | CERTAIN |
| `sameInst` | base 110-arm switch + 16 leaf delegates | base @ `0x2db7b0` | CERTAIN |
| `createFromJson` | **static factory** (not virtual) | `createFromJsonHelper` @ `0x2e01b0` | CERTAIN |
| `verify` | **external** (libwalrus visitor); one libBIR body | `InstGPSIMDSB2SB::verify` @ `0x294e00` | CERTAIN |

> **QUIRK — no `accept()` virtual.** BIR does *not* use C++ double-dispatch. Both the simulator and the `birverifier` dispatch with an explicit 110-arm `switch` on `Instruction+0x58`. There is no per-leaf `accept()` vtable slot; the `IRVisitor`/`InstVisitor` switch *is* the dispatch. `sameInst` is a hybrid — a base switch that tail-calls a real per-leaf override for only 16 opcodes (Activation, MatmultBase, the TensorScalar trio, DMACopy, StreamShuffle, CollectiveCompute, CustomOp, BIRKernel, Rand, Iota, AffineSelect, RangeSelect, GetCurProcessingRankID, CoreBarrier). And there is essentially no `Instruction::verify` in `libBIR` at all: verification lives in `libwalrus`'s `birverifier` ([`bir/value-model.md`](value-model.md) and the verifier page), keyed by the same opcode int.

---

## Cross-References

- [`bir/instruction-type.md`](instruction-type.md) — the 110-value `InstructionType` enum and the full leaf→opcode crosswalk (the `+0x58` tag's value space).
- [`bir/value-model.md`](value-model.md) — `bir::Argument` operand subclasses, `ArgumentKind`, and the `ins`/`indirection_ins`/`outs` list contents.
- [`bir/brewer-generator.md`](brewer-generator.md) — the Python `brewer` layer that emits BIR JSON consumed by `createFromJson`.
- [`nki/bircodegenloop.md`](../nki/bircodegenloop.md) — the Penguin→BIR driver that constructs these instructions.
