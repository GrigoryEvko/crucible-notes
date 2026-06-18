# BIR-JSON Wire Versioning

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310), `libBIR.so` md5 `12bb979f7ca41248252abb0f16b2da98`. VA == file offset for `.text` and `.rodata`; the function bodies cited live in the per-symbol IDA sidecars (two-VA-frame corpus: the thunk frame `0x1816xx`/`0x17axxx` tail-calls the real body frame `0x3bxxxx`/`0x48xxxx` — every address below is the body frame unless noted). cp311/cp312 VAs drift; the dispatch shape and key spellings are stable across them.*

## Abstract

A BIR-JSON document carries a single top-level integer key, `"version"`, whose only allowed values are `1` (old) and `2` (new). The naive reading — that this number selects a whole serialization schema, the way a NEFF container version or a protobuf message version would — is **wrong**, and the wrongness is the entire point of this page. The `version` int gates **exactly one** code path in the whole of `libBIR.so`: the (de)serialization of `pelican::Expr` affine-expression trees inside `bir::QuasiAffineExpr`. Every other layer of BIR — the Module/Function/BasicBlock/Instruction container wrapping, the operand/AccessPattern encoding, every per-opcode field schema — reads and writes identically regardless of the version. The proof is a single fact recovered from the binary: `bir::Module::getVersion()` has **exactly one caller** across all of `libBIR.so`, and that caller is `QuasiAffineExpr::createFromJson`.

The two pelican encodings the version selects are structurally different. **v1** is a flat, op-tagged, affine-only form: a node is either a bare affine sum `{terms:[{AxisLabel,coef}], c}` or that same flat affine *decorated* with an `{op, op_param}` pair (e.g. `{"op":"Mod", "op_param":<denom>, …affine body…}`). It cannot express a nested arithmetic tree — `isV1` (`@0x3b4110`) hard-rejects anything but a flat affine, three scalar-parametric wrappers, and a Div/Mod/CCDiv whose numerator is itself a flat affine. **v2** is the faithful recursive tree: every node is `{"kind":"<Tag>", …}` with child expressions serialized inline as nested objects. v2 adds the kinds v1 structurally could not encode — `SumKind`, `MultKind`, `AffIVKind`, `IntRuntimeValueKind`, a standalone `ShardIDKind`, plus the `channel_id`/`replica_groups_id` fields and arbitrary-depth `numer` sub-trees — which is exactly what software-pipeline modulo addressing, collective rank/shard math, and runtime-register indices require.

The third decisive fact: the compiler **always emits v2**. Every one of the five `QuasiAffineExpr::toJson` call sites loads `mov edx, 2` before the call; there is no `mov edx, 1` feeding a `toJson` anywhere in `libBIR.so`. v1 survives only as a *reader* (`fromJsonv1` plus the `version==1` branch) to ingest older or external BIR-JSON. So on the write side BIR-JSON v1 is dead code; v2 is the sole produced form.

This page documents the **version gate** — where the int is read, how it routes, and the v1↔v2 *shape* delta and added kinds at the level needed to understand the gate. The per-kind `pelican::Expr` wire field layouts, the refcount/DAG discipline, and the read-side factory ladder are owned by the [pelican Expr wire format](pelican-wire.md) page (7.19); the `pelican::Expr` algebra itself is owned by [Affine Expression Algebra](../penguin/affine-expr-algebra.md).

For reimplementation, the contract is:

- **The read dispatch** — top-level `"version"` → `Module::setVersion`, then the *single* `getVersion` consumer inside `QuasiAffineExpr::createFromJson` routing `{2→fromJsonv2, 1→fromJsonv1, else→fatal}`.
- **The write dispatch** — `QuasiAffineExpr::toJson(json&, uint version)` routing on its parameter `{2→toJsonv2, 1→toJsonv1, else→fatal}`, plus the five live emitters that all pass `2` and the ADL hook that hardwires `2`.
- **The v1↔v2 shape delta** — flat op-tagged affine (v1) vs recursive `{kind,…}` tree (v2), and the precise set of kinds/keys v2 adds, so a reimplementer knows which documents each reader must accept.
- **The non-changes** — that the container, operand, AccessPattern, and per-opcode schemas are version-invariant, and that the memloc-alias "old/new" split and the NEFF container version are *separate* concepts a reader must not conflate with this one.

| | |
|---|---|
| **Read driver** | `adl_serializer<bir::Module>::from_json` @`0x48df10` — reads `"version"`, calls `setVersion` |
| **`"version"` key string** | `0x70f872` (`"version\0"`, read via `basic_json::at<char const(&)[8]>`) |
| **Version field accessors** | `Module::setVersion(uint)` @`0x354ea0`, `Module::getVersion()` @`0x354e90` |
| **Sole `getVersion` consumer** | `QuasiAffineExpr::createFromJson` @`0x3bd8d0` (1 of 1 callers in `libBIR.so`) |
| **Read dispatch** | `cmp ebp,2` / `cmp ebp,1` @`0x3bd900`/`0x3bd909` → `fromJsonv2`/`fromJsonv1`/fatal |
| **Write dispatch** | `QuasiAffineExpr::toJson(json&,uint)` @`0x3bbda0` — `cmp edx,1` / `cmp edx,2` |
| **v1-representability test** | `isV1(RefPtr<Expr>)` @`0x3b4110` (kinds `{17}∪{2,3}∪{25,26,27 w/ affine numer}`) |
| **Produced version** | **always 2** — 5 emit sites all `mov edx,2`; ADL hook @`0x482620` hardwires `2` |
| **Read fatal string** | `0x741110` — `"Check the version of bir json file - should be 1 (old) or 2 (new)"` |
| **Write fatal string** | `0x740e20` — `"Check the version - should be 1 (old) or 2 (new)"` |

> **GOTCHA —** "version" is one of three unrelated "version"/"old vs new" notions in this stack, and conflating them is the most likely reimplementation error. (1) The BIR-JSON `version:1/2` *is* the pelican-Expr encoding gate, documented here. (2) The NEFF container version (`"0.6"` schema string + a `+0=2` binary-format constant in the NEFF header, Part 12) is a **different** number on a **different** file, owned by the NEFF packager, **absent** from `libBIR.so`. (3) The memloc-alias "old vs new" split (`addMemLocAliasesFromJson` @`0x26f020`) is detected by JSON *shape*, not by the version int. None of the three reads the other.

---

## The Version Field on the Module

### Purpose

The version is a single `uint32` stored on the `Module`. It is set exactly once, from the top-level `"version"` key, when the module is loaded; it is read exactly once, when a `QuasiAffineExpr` reconstructs its `pelican::Expr` from JSON. Between those two events nothing touches it. That one-write/one-read pattern is what makes the gate so narrow.

### Entry Point

```text
adl_serializer<bir::Module>::from_json  @0x48df10   ── the top-level BIR-JSON read driver
  └─ basic_json::at<char const(&)[8]>   @0x48df3b   ── j.at("version")   (key @0x70f872 = "version\0")
  └─ detail::from_json<…,unsigned int>  @0x48df51   ── parse the value as uint
  └─ bir::Module::setVersion(uint)      @0x48df60   ── store it on Module
```

### Algorithm

```c
function adl_serializer_Module_from_json(json& j, Module& m):   // 0x48df10
    // ── the version read: ONCE, at the top of the module driver ──
    ver = j.at("version")                  // 0x48df3b  at<char const(&)[8]>  ("version\0", 8 bytes incl NUL)
    ver_u = from_json<uint>(ver)           // 0x48df51  parse as unsigned int
    m.setVersion(ver_u)                    // 0x48df60  store on Module (uint32 field)

    // 0x48df65: cmpb $0x1,(%rbx)  is NOT a version test — it is the nlohmann
    //           value_t discriminant byte (json+0). The version is never
    //           re-compared here; setVersion is the ONLY thing done with it.

    // ── the rest of the driver is version-AGNOSTIC ──
    read "arch", "archRev", "functions", (NKI) "functions", module DMAQueues …
    // two-pass build/resolve; none of these read getVersion()
```

> **NOTE —** the instruction immediately after `setVersion` is `cmpb $0x1,(%rbx)`, which *looks* like a `version == 1` branch but is the nlohmann `basic_json` type-tag check (object vs other). Do not transcribe it as a version dispatch — the version is consumed nowhere in this driver. [CONFIRMED — disasm @`0x48df10`; `at<char const(&)[8]>` confirms the 8-byte `"version\0"` key.]

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `adl_serializer<bir::Module>::from_json` | `0x48df10` | reads `"version"`, calls `setVersion`; rest version-agnostic | CERTAIN |
| `bir::Module::setVersion(uint)` | `0x354ea0` | stores the `uint32` version field | CERTAIN |
| `bir::Module::getVersion()` | `0x354e90` | reads it; **exactly one caller** in `libBIR.so` | CERTAIN |

### Considerations

`getVersion` having a single caller is the structural backbone of this whole page, so it is worth stating how it is established rather than asserted: a sweep of every disassembled function in the `libBIR.so` sidecar for a `call` to `Module::getVersion` returns **one** file — `QuasiAffineExpr::createFromJson`. No container deserializer, no per-opcode `readFieldsFromJson`, no operand or AccessPattern reader calls it. [CONFIRMED — `rg -l 'call.*ZNK3bir6Module10getVersionEv'` over the disasm corpus = 1 hit, that file.] If a reimplementer's version int reaches any reader other than the affine-expression reader, they have diverged from the binary.

---

## The Read Dispatch — `QuasiAffineExpr::createFromJson`

### Purpose

This is the sole place the module version is consulted. A `QuasiAffineExpr` (the BIR wrapper around a `RefPtr<pelican::Expr>` plus its loop-axis set) reconstructs its expression tree by branching on the module version: `2` selects the recursive-tree reader, `1` selects the legacy flat reader, anything else is a fatal error.

### Entry Point

```text
bir::QuasiAffineExpr::createFromJson(Instruction*, json const&)   @0x3bd8d0
  ├─ bir::Instruction::getModule()           @0x3bd8e6
  ├─ bir::Module::getVersion()               @0x3bd8ee   ◀ THE ONLY getVersion call in libBIR
  ├─ bir::Instruction::getPelicanContext()   @0x3bd8f8   ── the interning arena for the reader
  ├─ fromJsonv1(PelicanContext*, Instruction*, json const&)   @0x3bbe90   (version==1 arm)
  └─ fromJsonv2(PelicanContext*, Instruction*, json const&)   @0x3bc8c0   (version==2 arm)
```

### Algorithm

```c
function QuasiAffineExpr_createFromJson(Instruction* instr, json const& j):   // 0x3bd8d0
    mod = instr.getModule()                  // 0x3bd8e6
    ver = mod.getVersion()                   // 0x3bd8ee  ── ebp = version (the only getVersion site)
    ctx = instr.getPelicanContext()          // 0x3bd8f8  ── arena that mints/interns nodes

    if ver == 2:                             // 0x3bd900  cmp ebp,2 ; je 0x3bd990
        return fromJsonv2(ctx, instr, j)     // 0x3bd99c  ── recursive {kind,…} tree reader
    if ver != 1:                             // 0x3bd909  cmp ebp,1 ; jne 0x3bd9b2
        report_fatal_error(                  // 0x3bd9be  lea rdi, str@0x741110
            "Check the version of bir json file - should be 1 (old) or 2 (new)")
    return fromJsonv1(ctx, instr, j)         // 0x3bd91e  ── legacy flat op-tagged reader
```

> **QUIRK —** both readers take `(PelicanContext*, Instruction*, json const&)`, not just the json. The `PelicanContext` is the hash-consing arena that mints/uniques nodes; the `Instruction` is needed because the wire stores some leaves *by name* — an `AffIVKind` carries an `"axis"` name resolved via `Instruction::findAxis`, an `IntRuntimeValueKind` a `"regref"` name resolved via the owning `Function`. The writer is context-free (it walks a live tree), but the reader cannot rebuild named leaves without the instruction. This asymmetry is detailed on the [pelican Expr wire](pelican-wire.md) page.

### Considerations

`AffinePredicate::createFromJson` (`@0x22ebe0`) calls `QuasiAffineExpr::createFromJson` transitively, so the affine predicates on predicated dependency edges ride the **same** gate — a predicated dependency builds a `vector<QuasiAffineExpr>` plus a `vector<AffinePredicate>`, both of which are pelican expressions and both version-routed through this one branch. [STRONG — `AffinePredicate::createFromJson` calls `QuasiAffineExpr::createFromJson`; the version routing is inherited, not re-implemented.]

`fromJsonv1` and `fromJsonv2` are each reached from **exactly this one branch** — neither has any other caller. There is therefore no parallel "pelican version" selector; the pelican wire version *is* the BIR module version threaded down through `getModule()->getVersion()`. [CONFIRMED — each reader's call graph has the single caller `createFromJson`.]

---

## The Write Dispatch — `QuasiAffineExpr::toJson`

### Purpose

Emission is also version-gated, but the writer takes the version as an **argument**, not from `getVersion`. `QuasiAffineExpr::toJson(json&, uint version)` branches on its second parameter to call either the v2 or v1 emitter. This separation is what lets every live emitter hardcode `2` while the v1 emitter still physically exists.

### Entry Point

```text
bir::QuasiAffineExpr::toJson(json&, uint version)   @0x3bbda0
  ├─ toJsonv2(RefPtr<Expr>, json&)   @0x3bab90   (version==2 arm, the je target)
  └─ toJsonv1(RefPtr<Expr>, json&)   @0x3b9f30   (version==1 arm; asserts isV1 first)
```

### Algorithm

```c
function QuasiAffineExpr_toJson(json& out, uint version):   // 0x3bbda0
    if version == 1:                         // 0x3bbda5  cmp edx,1 ; je 0x3bbdf0
        // toJsonv1 itself asserts isV1(this.expr) && "Pelican Expr should be in V1 format"
        return toJsonv1(this.expr, out)      // 0x3bbe08  ── flat op-tagged emitter
    if version != 2:                         // 0x3bbdaa  cmp edx,2 ; jne 0x3bbe5d
        report_fatal_error(                  // 0x3bbe69  lea rdi, str@0x740e20
            "Check the version - should be 1 (old) or 2 (new)")
    return toJsonv2(this.expr, out)          // 0x3bbdcb  ── recursive {kind,…} tree emitter
```

> **NOTE —** the read and write fatal strings are *distinct* literals, which is a useful tell for whoever is staring at a crash: `0x741110` ("…of bir json file…") fires from the reader, `0x740e20` ("Check the version…") fires from the writer. Both were confirmed at those addresses. [CONFIRMED — string table addrs `0x740e20`/`0x741110`.]

### The producer always writes v2

Every live caller of `QuasiAffineExpr::toJson` loads `mov edx, 2` before the call. There are five such call sites, across three emitter functions:

| Emit site | Caller | Version passed |
|---|---|---|
| `0x25e719` | `InstDoWhile::toJson` | `mov edx,2` |
| `0x2e3320` | `Instruction::toJson` (the common path) | `mov edx,2` |
| `0x324275` | `InstDynamicForLoop::toJson` | `mov edx,2` |
| `0x3242a3` | `InstDynamicForLoop::toJson` | `mov edx,2` |
| `0x3242ce` | `InstDynamicForLoop::toJson` | `mov edx,2` |

There is no `mov edx, 1` feeding a `toJson` call anywhere in `libBIR.so`. [CONFIRMED — all three callers of `QuasiAffineExpr::toJson` disassembled; every preceding `mov edx,*` is `mov edx,2`.] The consequence for a reimplementer is precise: **v1 is dead on the write side.** `toJsonv1` and the `version==1` write arm exist, but no in-binary caller selects them; they would only run if external code passed `version=1`, which none does. v1 is therefore a *read-only legacy ingest format*, and v2 the sole produced one.

> **QUIRK —** one more emitter forces v2 unconditionally and ignores even the (always-2) argument plumbing: `adl_serializer<bir::QuasiAffineExpr>::to_json` @`0x482620` hardwires `mov edx, 2` @`0x482623` before delegating. Any `QuasiAffineExpr` serialized through the nlohmann ADL hook — notably `DynamicAPINFO.offset_expr`, the dynamic-DMA byte offset — is emitted in v2 regardless of any module version. Explicit `toJson(json&, version)` callers honour the version argument (which is always 2); ADL callers cannot be v1 even in principle. [CONFIRMED — `mov edx,2` @`0x482623`.]

---

## The v1↔v2 Shape Delta

Because the version gates only pelican, the entire v1↔v2 diff is the `pelican::Expr` wire delta. There is no change to any `bir::Inst*` op-key, no change to the container/operand/header schema. The delta is per-Expr-kind, and it has one headline shape change plus a set of added kinds and keys.

### The encoding-shape change

```text
v1 (flat, op-tagged, affine-only)            v2 (recursive {kind,…} tree)
─────────────────────────────────           ──────────────────────────────────────
affine leaf:                                 every node is an object:
  { terms:[{AxisLabel,coef}…], c }             { kind:"AffExprKind",
                                                 terms:[{idx:<RECURSE>, coeff}…], c }
binary/op (Div/Mod/CCDiv/CCGetRank):         each binary kind is explicit + recursive:
  { op:"Mod", op_param:<denom>,                { kind:"ModuloKind",
    …flat-affine body of numer… }                numer:<RECURSE>, denom:<long> }

  ↑ ONE int param + a forced-flat numer       ↑ numer is an arbitrary nested sub-expr
```

In v1 a Div/Mod/CCDiv node is the affine body of its (mandatory kind-17) numerator *decorated* with two extra keys, `"op"` and `"op_param"`, all in one flat object; there is no nested-expression recursion. In v2 every node is `{"kind":"<Tag>", …}` and a child expression is serialized inline as a nested object — a faithful tree.

### What `isV1` proves about v1's expressive ceiling

`isV1` (`@0x3b4110`) is the binary's own statement of what v1 can encode; `toJsonv1` asserts it up front. It reads `kind = *(Expr+0x10)` and returns true iff:

```c
function isV1(Expr* e):                       // 0x3b4110
    kind = *(e + 0x10)
    if kind == 17: return true                // AffineExpr — the flat affine form
    if kind <= 17: return (kind - 2) <= 1      // kind in {2,3}  (CCGetRank, IndirectArg)
    if (kind - 25) > 2: return false           // not FloorDiv/Modulo/CCDiv → v2-only
    numer = *(e + 0x20)                         // the numerator operand (Expr+0x20)
    return *(numer + 0x10) == 17                // … OK only if that numer is itself a flat AffineExpr
```

So v1 can encode **only**: `AffineExpr(17)`, `CCGetRankExpr(2)`, `IndirectArgExpr(3)`, and `FloorDivExpr(25)`/`ModuloExpr(26)`/`CCDivExpr(27)` *when their numerator is a flat affine*. Anything else — a `numer` that is itself a Sum/Mult/Div, or any of the new leaf kinds — is not v1-representable, which is precisely the reason v2 exists. [CONFIRMED — `isV1` body read firsthand @`0x3b4110`; the refcount dance around the `numer` read elided.]

### Added kinds and keys in v2

v2 adds the kinds v1 has no encoding for, plus two fields on existing kinds. The set is the headline-level delta; the per-kind JSON field layouts (offsets, value types, emit order) belong to the [pelican Expr wire](pelican-wire.md) page (7.19).

| Change | Kind(s) | v1 form | v2 form | Status |
|---|---|---|---|---|
| **ADDED kind** | `SumExpr(18)` | — (none) | `{kind:"SumKind", n_terms, terms:[…]}` | STRONG |
| **ADDED kind** | `MultExpr(23)` | — | `{kind:"MultKind", var:<sub>, scale}` | STRONG |
| **ADDED kind** | `AffineIV(6)` | — | `{kind:"AffIVKind", axis:<name>}` | STRONG |
| **ADDED kind** | `IntRuntimeValue(7)` | — | `{kind:"IntRuntimeValueKind", regref:<name>}` | STRONG |
| **ADDED (standalone)** | `ShardId(13)` | flat affine leaf token | `{kind:"ShardIDKind", ub}` (byte-identical in both) | STRONG |
| **ADDED key** | `CCGetRank(2)` | `op_param` only | `+ channel_id` | STRONG |
| **ADDED key** | `CCDiv(27)` | not separable | `+ replica_groups_id` | STRONG |
| **RESTRUCTURED** | `FloorDiv(25)`,`Modulo(26)`,`CCDiv(27)` | `{op,op_param,…flat numer…}` | `{kind, numer:<sub-tree>, denom}` | STRONG |
| **RENAMED** | `AffineExpr(17)` terms | `{AxisLabel:<name>, coef}` | `{idx:<RECURSE expr>, coeff}` | STRONG |
| **REMOVED** | (v1-only keys) | `"op"`, `"op_param"`, `"AxisLabel"` | gone | STRONG |

> **NOTE —** the AffineExpr term key change is subtle and worth flagging: v1 stores a term's index *by axis name* (`AxisLabel`, a string from the index node's name accessor) and spells the coefficient `coef`; v2 embeds the index as a *nested expression* (`idx`, recursive) and spells it `coeff` (one extra letter, both spellings present in rodata). A reimplementer's reader must accept both. `ShardIDKind`/`ub` and `IndirectArg` round-trip through either version. [STRONG — kind set and key spellings cross-anchored to the serializer bodies and the rodata tag set; the per-key offsets are on the 7.19 page.]

### Why v2 — the features it unlocked

v1 is structurally incapable of expressing a nested arithmetic/index tree; v2 was introduced to serialize exactly the expr kinds those features need:

- **Software-pipeline / multi-buffer PSUM address rotation** needs `ModuloExpr(26)`/`FloorDivExpr(25)` whose `numer` is itself a Sum/Mult tree (a PSUM offset rewritten as `(affine) mod bank_period`). v1's `{op:"Mod", op_param:denom, flat-numer}` cannot carry that.
- **Collective rank/shard math** (FSDP / sharded collectives) needs `CCGetRank(2)+channel_id`, `CCDiv(27)+replica_groups_id`, and a standalone `ShardId(13)`. v1 collapsed `CCGetRank` to a single `op_param` and had no `replica_groups_id`/`channel_id` slot.
- **Dynamic-shape / runtime-register indices** (dynamic loop bounds, indirect/gather offsets read from device registers) need `AffineIV(6)` by axis-name and `IntRuntimeValueBase(7)` `regref`. v1 had no register-backed leaf.

[STRONG — the v2-only kind set *is* exactly these features' nodes; the calendar date when v2 was introduced is not byte-recoverable from this snapshot — SPECULATIVE.]

> **GOTCHA —** v2 is **not** about MX or LNC, two tempting but wrong guesses. `InstMatmultMx`/`InstQuantizeMx` add *zero* JSON keys (the former thunks to `MatmultBase`, the latter's `readFieldsFromJson` is a bare `ret`); MX-ness rides in operand dtype enums plus a scale-tensor operand, not in any version-gated field. Dynamic-DMA/LNC are likewise dtype/operand/queue features. The single `getVersion` caller forbids any other version-keyed path, so none of these can be a version concern. [CONFIRMED via the single-caller constraint + the MX no-key bodies.]

---

## What Does NOT Change Across the Version

The most reimplementation-relevant content here is the list of things the version does *not* gate, because each is a plausible-but-wrong place to branch.

- **Container wrapping** — Module / Function / BasicBlock / Instruction read and write identically in both versions. The driver (`@0x48df10`) reads `arch`, `archRev`, `functions`, DMAQueues the same way regardless; the two-pass build/resolve is version-agnostic. [CONFIRMED — `getVersion` has no caller in the driver or any container deserializer.]
- **Operand / AccessPattern encoding** — `Argument::createFromJson` (`@0x235500`) dispatches on an 8-way `"kind"` string (`physical_ap`/`symbolic_ap`/`register_ap`/`imm_value`/`symbolic_imm_value`/`imm_array`/`register_access`/…) that is **not** version-keyed. What changes with the version is the *content* of a symbolic AP's embedded `QuasiAffineExpr` `addrs` (the pelican trees), never the AP container shape. [STRONG — AP kind tags are version-invariant; the embedded pelican expr is the variant part.]
- **Per-opcode field schemas** — no `bir::Inst*` `readFieldsFromJson` branches on the version. Every op-key is present in both versions. [CONFIRMED — zero `if(version==…)` guards in the opcode readers; they cannot read it, since `getVersion` has one caller.]

> **CORRECTION (S05) —** an early framing treated the BIR module version as a top-level switch over op schemas, and treated the "pelican v1/v2" as an *independent* counter. Both are wrong. There is no module-level schema switch: the version is `setVersion`'d once and consumed by exactly one code path. And the pelican v1/v2 *selection* is the BIR module version itself (`createFromJson` → `getVersion`) — the *formats* are a separate code path, but the *selector* is the single shared int. The one exception is the ADL hook, which forces v2 irrespective of the module version. [CONFIRMED — single `getVersion` caller; `fromJsonv1`/`v2` each have one caller = the gate.]

### The memloc-alias "old/new" is a different axis

`addMemLocAliasesFromJson` (`@0x26f020`) selects between an OLD (memloc-keyed, 2-tuple `[destMemLoc, kind]`) and a NEW (set-keyed, `["memorylocations"]`, 3-tuple `[destSet, destMemLoc, kind]`) alias schema. This is detected by JSON **shape**, not by the version int — `addMemLocAliasesFromJson` does not (and structurally cannot) call `getVersion`, since that function's sole caller is the pelican reader. "old vs new" is therefore overloaded in BIR: the numeric `version 1/2` is the pelican split, while the alias-schema old/new is a separate shape-detected choice. They may correlate in practice but are dispatched independently. [CONFIRMED — `addMemLocAliasesFromJson` has no `getVersion` call.]

### The NEFF container version is unrelated

The BIR-JSON `version:1/2` is not the NEFF container version. NEFF carries its own versioning in a different file and a different component: an `info.json` schema-version *string* (`"0.6"`) plus a binary NEFF-format constant (`+0 = 2` in the NEFF header), both owned by the NEFF packager (Part 12), and **absent** from `libBIR.so` — a scan of the `libBIR.so` disassembly for any `0.5`/`0.6`/`NeffVersion` notion returns nothing. A reader who sees "version 2" must keep these separate: BIR-JSON v2 is the pelican-tree encoding; NEFF format 2 / schema `"0.6"` is the container envelope. [CONFIRMED — NEFF version literals absent from `libBIR.so`; grounded against the NEFF JSON layout, Part 12.]

---

## Adversarial Self-Verification

Each of the five strongest claims was re-checked against the binary firsthand; the verification ceiling is stated honestly.

| Claim | Verification | Ceiling |
|---|---|---|
| The version gates **pelican only** | `rg -l 'call.*getVersion'` over the whole `libBIR.so` disasm → **1 file**, `QuasiAffineExpr::createFromJson`. No container/opcode/operand reader calls it. | **CONFIRMED** — exhaustive over the disassembled corpus. The corpus is the IDA-resolved function set; a hypothetical hand-written `call` IDA failed to resolve is not excluded, but the caller graph is otherwise complete. |
| The producer **always writes v2** | All three callers of `QuasiAffineExpr::toJson` disassembled; the five `mov edx,*` immediates are all `2`; the ADL hook hardwires `2` @`0x482623`. No `mov edx,1` feeds any `toJson`. | **CONFIRMED for this build snapshot.** No CLI/env knob to force v1 emission was found, and none could be invoked without a `mov edx,1` site that does not exist — but "no external caller passes 1" is a one-build statement, not a proof over all configurations. |
| **v1 = flat, v2 = recursive tree** | `isV1` body read firsthand: accepts only `{17}∪{2,3}∪{25,26,27-with-affine-numer}`; `toJsonv1` asserts `isV1`. v2 emitter dispatches over `{2,3,6,7,13,17,18,23,25,26,27}` with a `default→"Unsupported expression kind"`. | **CONFIRMED** for the gate-level shape (which kinds each can encode). The per-kind field byte-layouts are STRONG (serializer bodies, 7.19), not re-disassembled key-by-key here. |
| **The added kinds** (`Sum/Mult/AffIV/IntRuntime/ShardId`, `channel_id`, `replica_groups_id`) | v2 KIND-tag strings confirmed present in rodata (`SumKind`, `MultKind`, `AffIVKind`, `IntRuntimeValueKind`, `ShardIDKind`, …); `isV1` rejects them from v1. | **STRONG.** The *kind set* and *tag spellings* are anchored; the exact intra-body field read order inside `fromJsonv1`/`v2` is taken from the per-kind analysis (7.19), not re-traced end-to-end on this page. |
| **NEFF version distinctness** | `libBIR.so` disasm scan for `0.5`/`0.6`/`NeffVersion` → 0 hits; the BIR version is purely the `1/2` int. NEFF's `"0.6"`/`+0=2` live in the NEFF packager (Part 12). | **CONFIRMED** at the level that matters — the two version notions do not co-reside in `libBIR.so` and neither reads the other. |

What is **CONFIRMED firsthand** on this page: the version read site and `"version\0"` key; the single `getVersion` caller; the read-side `{2→v2, 1→v1, else→fatal}` branch and its fatal string; the write-side `{1/2/fatal}` branch and its distinct fatal string; all five emit sites passing `2`; the ADL hardwire; `isV1`'s exact kind set; the absence of NEFF version literals in `libBIR.so`.

What is **STRONG (cross-anchored, not re-disassembled key-by-key here):** the per-kind v1/v2 JSON field layouts (owned by 7.19) and the v2-only kind tag set in rodata.

What is **SPECULATIVE / not pinnable from this snapshot:** the calendar date or build at which v2 was introduced (no changelog or date literal in `libBIR.so`); whether any out-of-binary configuration could force v1 emission (none found, but unprovable from one build).

---

## Related Components

| Name | Relationship |
|---|---|
| `bir::QuasiAffineExpr` | The wrapper the version gate lives on; owns the `RefPtr<pelican::Expr>` that the gate (de)serializes |
| `pelican::Expr` family | The thing v1/v2 actually encode differently; the per-kind wire layouts are the 7.19 page |
| `bir::AffinePredicate` | Rides the same gate transitively (`createFromJson` calls `QuasiAffineExpr::createFromJson`) |
| `adl_serializer<bir::Module>::from_json` | The only writer of the version field (`setVersion`) |
| NEFF packager (Part 12) | Carries a *separate* container version (`"0.6"` / `+0=2`); must not be conflated |

## Cross-References

- [pelican Expr Wire Format](pelican-wire.md) — 7.19, the per-kind `fromJsonv1/v2` + `toJsonv1/v2` field layouts, refcount/DAG discipline, and the read-side factory ladder this gate selects between
- [BIR JSON Loader](json-loader.md) — 7.12, the two-pass `from_json` driver that the version-read site (`adl<Module>::from_json`) heads
- [BIR JSON Writer / Dumper](json-dumper.md) — 7.13, the emit path whose `Instruction::toJson` hardcodes `version=2`
- [Affine Expression Algebra](../penguin/affine-expr-algebra.md) — the `pelican::Expr` / `QuasiAffineExpr` algebra whose trees the gate encodes
- [Argument / AccessPattern / Value Model](value-model.md) — the `SymbolicAccessPattern.addrs` and `SymbolicImmediateValue` that embed version-gated `QuasiAffineExpr` trees
- [NEFF JSON Schema](../formats/neff-json.md) — 12.3, the NEFF container `"0.6"` schema version (a *different* version number)
- [NEFF Header & Format Version](../formats/neff-header.md) — 12.7, the NEFF binary-format constant (`+0=2`), distinct from BIR-JSON v2
