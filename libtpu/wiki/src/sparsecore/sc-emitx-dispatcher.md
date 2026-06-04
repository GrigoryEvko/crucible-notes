# SC EmitX Dispatcher

> *Every opcode value, oneof tag, jump-table bound, RE2 regex literal, and engine-tag constant on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`; full C++ symbols, not stripped) — from the decompiled `ConsumeScalarAluInstruction<glc::SparseCoreTacBundle>` body, `ScSection::ComputeKind`, `GhostliteEmitter::ConsumeProgram`, and their `.rodata` jump table at `0xae8db28`. Other versions differ.*

## Abstract

This page documents the two pieces that decide, per decoded `MCInst`, **which EmitX template populates which engine's proto bundle**: (1) the **`crc32` section classifier** that picks the *engine* — SCS / TAC / TEC, by matching the LLVM section name against a static RE2 regex table — and (2) the per-bundle-type **`ConsumeScalarAluInstruction` jump table** at `0xae8db28` that picks the *op + EmitX template* from the `MCInst` opcode. Together they are the SparseCore analog of an LLVM backend's "select instruction → bind functional unit → call the matching emitter": the section name routes the op to one of three engine programs, then a 111-entry jump table on the scalar-ALU opcode block (`0x222..0x290`) routes each op to a `SparseCoreScalarAlu` proto submessage and the `EmitScalar{Unop,Binop,CompareOp,YUnop,Weird}` / `EmitSetRegister` template that fills it.

The dispatcher sits between two pages already in this wiki. Upstream, the [SC Backend Pipeline](sc-backend-pipeline.md) lowers the SC dialect to MLO and hands an `SCStreamer` of `MCInst`s to `RunCodeGen`. Downstream, the [OneSlot Scalar Router](oneslot-router.md) decides *which bundle slot* a scalar op occupies and tail-calls the per-slot `Consume<Slot>Instruction` leaf. **This page is the leaf one level below the OneSlot router for the ScalarAlu slot**: `ConsumeScalarAluInstruction` is the function the router reaches for any opcode in the scalar-ALU block, and the jump table inside it is the op→EmitX selection. The OneSlot router answers "which slot"; this jump table answers "which op + which EmitX template fills the ScalarAlu proto."

The single most important structural fact is that **op selection is split across two tables keyed on two different things.** The *engine* is keyed on the **section name** (a runtime RE2 match producing a `SectionKind` ordinal that doubles as the engine tag `6/7/8`); the *op + EmitX template* is keyed on the **`MCInst` opcode** (a `.rodata` jump table per `(generation, bundle-type)`). Neither is an MLIR pattern table or a TableGen itinerary — both are hand-rolled tables read directly from the binary. A reimplementer needs both the regex table and the per-`(gen,bundle)` opcode jump table; one without the other mis-routes every op.

For reimplementation, the contract is:

- **The engine classifier is a static RE2 section-name table.** `ScSection::ComputeKind` (`0x13afefe0`) lazily builds nine `RE2` regexes (`__cxa_guard` `0x224de388`) and `FullMatchN`s the section name in fixed order; the first match returns a `SectionKind` int. `GhostliteEmitter::ConsumeProgram` then `crc32`-probes a `flat_hash_map<SectionKind, ScSection>` and routes on the matched section's engine tag at `slot+0x28`: `8 → Scs`, `7 → Tec`, `6 → Tac`.
- **The op classifier is the `0xae8db28` jump table.** `ConsumeScalarAluInstruction<glc::SparseCoreTacBundle>` (`0x139f09c0`) reads `DWORD[MCInst]`, subtracts base `0x222`, bound-checks `0x6e` (111 entries), and indirect-jumps. 37 distinct in-table arms (20 direct-accessor, 16 two-opcode `EmitX` pairs, 1 `Halt`) select a `SparseCoreScalarAlu` proto op and an `EmitScalar*` / `EmitSetRegister` template; two out-of-block opcodes (`0xb25 → Halt`, sharing the in-table `Halt` arm, and `0xf86 → IsInfOrNan`) are handled in the default fall-through; every other opcode hits "Unsupported opcode for Scalar Alu slot."
- **Each `(generation, bundle-type)` owns its own jump table over the same opcode block.** `glc::TacBundle` → `0xae8db28`, `glc::ScsBundle` → `0xaea9df8`, `vfc::TacBundle` → `0xae667d8`; all share base `0x222`, bound `0x6e`, and the same op family. Only the `EmitX` bundle-template argument differs (`SparseCoreTacBundle` vs `SparseCoreScsBundle`). The scalar-ALU ISA is shared across the SCS and TAC engines.
- **EmitX populates proto; it does not pack bits.** Each arm's `EmitScalar*` template fills a `SparseCoreScalarAlu` oneof submessage (discriminator `[proto+0x50]`, active message `[proto+0x48]`). The absolute bundle bit positions are written later by the `<Slot>Encoder::Encode` → `BitCopy` stage, downstream of `ConvertToTpuCoreProgram`.

| | |
|---|---|
| **Op jump table (glc TAC)** | `ConsumeScalarAluInstruction<glc::SparseCoreTacBundle>` @ `0x139f09c0`; jt `0xae8db28`, base `0x222`, bound `0x6e` (111 entries) |
| **Sibling jump tables** | `glc::ScsBundle` @ `0x13a4f7c0` (jt `0xaea9df8`) · `vfc::TacBundle` @ `0x13985100` (jt `0xae667d8`) — same block, same op family |
| **Op classifier input** | `DWORD[MCInst]` opcode; index `opcode − 0x222`; 37 in-table arms (20 A + 16 B-pairs + 1 Halt) + default + 2 OOB |
| **Engine classifier** | `ScSection::ComputeKind` @ `0x13afefe0` — 9 static `RE2` regexes (`__cxa_guard` `0x224de388`), first `FullMatchN` wins → `SectionKind` |
| **Engine route** | `GhostliteEmitter::ConsumeProgram` @ `0x139ed5e0` — `crc32` `flat_hash_map<SectionKind,ScSection>` probe → engine tag `slot+0x28`: `8`=Scs, `7`=Tec, `6`=Tac |
| **Proto oneof** | `SparseCoreScalarAlu`; discriminator `[proto+0x50]` (`_oneof_case_[0]`), active msg `[proto+0x48]` |
| **EmitX families** | `EmitScalarUnop` · `EmitScalarBinop` · `EmitScalarCompareOp<PredicateDest,…>` · `EmitScalarYUnop` · `EmitScalarWeird<PredicateDest,…>` · `EmitSetRegister` + 20 direct `mutable_<op>()` accessors |
| **Default error** | `"Unsupported opcode for Scalar Alu slot: $0 : $1"` (`isa_emitter.cc`) |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise. The TABLE A opcode→op mappings are read directly from the `0x139f09c0` `switch`. |

> **NOTE — "EmitX dispatcher" is two tables, not one.** Do not look for a single op→EmitX map. The engine is chosen by the *section name* (a runtime RE2 match), the op by the *opcode* (a `.rodata` jump table). The OneSlot router ([oneslot-router.md](oneslot-router.md)) inserts a third decision — the *slot within the bundle* — between them. A reimplementer needs all three; this page owns the engine classifier and the ScalarAlu op table.

---

## The Engine Classifier — Section Name → Engine Tag

### Why the engine is keyed on the section name

A SparseCore program is emitted as an LLVM object with named sections — `.text.tile_access`, `.text.tile_execute`, `.text` (with an optional `.scs*` suffix), plus data sections (`smem`, `tilespmem`, `spmem`, `hbm`, `sflag`). Each `.text*` section corresponds to one of the three SC sequencer engines. Rather than carrying the engine as an `MCInst` attribute, libtpu derives it from the **section the instruction was emitted into**: `SCStreamer::changeSection` classifies each section once, stores the resulting `SectionKind` into an `ScSection`, and `ConsumeProgram` later reads that kind back as the engine tag.

### `ScSection::ComputeKind` (`0x13afefe0`)

The classifier is a static RE2 regex table built lazily under a `__cxa_guard` (`0x224de388`). The decompiled body constructs nine regexes in this exact order, then `FullMatchN`s the section name against each in sequence and returns the first match's `SectionKind` int:

```c
// ScSection::ComputeKind(string_view section_name)   // 0x13afefe0
//   returns { ok_flag@+0, kind@+8 }
if (!kSectionRegexes_guard) {                          // lazy __cxa_guard 0x224de388
    RE2::RE2(&e[0].re, "\\.(data|bss)\\.smem");        e[0].kind = 0;   // SMEM
    RE2::RE2(&e[1].re, "\\.(data|bss)\\.tilespmem");   e[1].kind = 1;   // TILE-SPMEM
    RE2::RE2(&e[2].re, "\\.(data|bss)\\.spmem");       e[2].kind = 2;   // SPMEM
    RE2::RE2(&e[3].re, "\\.(data|bss)\\.hbm");         e[3].kind = 3;   // HBM
    RE2::RE2(&e[4].re, "\\.(data|bss)\\.sflag");       e[4].kind = 4;   // SFLAG
    RE2::RE2(&e[5].re, "\\.text\\.tile_execute");      e[5].kind = 7;   // TEC code
    RE2::RE2(&e[6].re, "\\.text\\.tile_access");       e[6].kind = 6;   // TAC code
    RE2::RE2(&e[7].re, "\\.text(\\.scs.*)?$");         e[7].kind = 8;   // SCS code (fallback .text)
    RE2::RE2(&e[8].re, ".note.GNU-stack");             e[8].kind = 9;   // ELF note marker
    kSectionRegexes = e;
}
for (i = 0; i < 9; ++i)
    if (RE2::FullMatchN(name, &e[i].re, 0, 0)) {
        result.ok   = true;
        result.kind = e[i].kind;                       // *(int*)matched_entry
        return result;
    }
return Error("Unknown section kind for \"...\"");      // no regex matched
```

> **GOTCHA — the regex *construction* order is not the kind *ordinal* order.** The table is built `smem(0), tilespmem(1), spmem(2), hbm(3), sflag(4), tile_execute(7), tile_access(6), .text/.scs(8), GNU-stack(9)` — the two `.text.tile_*` entries store kinds `7` and `6` (note `tile_execute=7` is built before `tile_access=6`), and the catch-all `.text` entry stores `8`. The `SectionKind` is the entry's `int` field (written one DWORD ahead of each regex slot: `*v8=0`, `v8[38]=1`, … `v8[266]=8`, `v8[304]=9` in the decompile), **not** its position in the table. A reimplementer who assigns kinds by table index will swap TEC/TAC and mis-tag SCS.

> **GOTCHA — `.text(\.scs.*)?$` is the SCS *fallback*.** Plain `.text` (no `.tile_access` / `.tile_execute` suffix), with or without a `.scs*` suffix, classifies as SCS (kind 8 — the same value `ConsumeProgram` routes on, so no `9→8` remap exists). The two tile sections must therefore be tested *before* the bare-`.text` regex, which the construction order guarantees (entries 5/6 before entry 7). Any SC text section that is neither tile-access nor tile-execute is SCS by default.

### The classifier map and the engine route

`SCStreamer::changeSection` (`0x13b03e20`) calls `ComputeKind`, then `EmplaceDecomposable`s a `{SectionKind → ScSection}` entry into a `flat_hash_map` at `SCStreamer+0x140`, stamping the kind into `ScSection+0x20` (= the map slot's `+0x28`). Under a per-generation flag at `SCStreamer+0x240`, a `TAC(6) → TEC(7)` key remap is applied to the map key (`cmove`).

`GhostliteEmitter::ConsumeProgram` (`0x139ed5e0`) walks the ordered `SectionKind` list at `SCStreamer+0x128`, `crc32`-probes the map, and routes on the matched section's engine tag at `slot+0x28`. The decompiled body uses SSE4.2 `crc32` on the key and its triple (`_mm_crc32_u64(0, key)` and `_mm_crc32_u64(…, 3*key)`), then default-constructs the matching engine program:

```c
// GhostliteEmitter::ConsumeProgram   // 0x139ed5e0 (engine route)
R8  = _mm_crc32_u64(0, key);
h   = _mm_crc32_u64(*(u16*)(a2+0x148), 3*key) | (R8 << 32);   // flat_hash_map probe
... SSE4.2 group probe (vpcmpeqb / vpmovmskb / tzcnt) → matched slot ...
tag = *(u32*)(slot + 0x28);                                   // engine tag
if (tag == 8) DefaultConstruct<SparseCoreScsProgram>();       // SCS  (seq3)
if (tag == 7) DefaultConstruct<SparseCoreTecProgram>();       // TEC  (seq5)
if (tag == 6) DefaultConstruct<SparseCoreTacProgram>();       // TAC  (seq4)
```

| `SectionKind` (engine tag) | Engine program (`DefaultConstruct`) | Seqtype | Bundle | Source section |
|---|---|---|---|---|
| `6` | `SparseCoreTacProgram` (`gxc::glc::isa`) | 4 TAC | `SparseCoreTacBundle` | `.text.tile_access` |
| `7` | `SparseCoreTecProgram` | 5 TEC | `SparseCoreTecBundle` | `.text.tile_execute` |
| `8` | `SparseCoreScsProgram` | 3 SCS | `SparseCoreScsBundle` | `.text` / `.text.scs*` (regex kind 8) |

The `crc32` probe and the `tag == 7/6/8` dispatch and the `gxc::glc::isa::SparseCore{Tec,Tac,Scs}Program` default-constructs are confirmed in the `ConsumeProgram` decompile. `ViperfishEmitter::ConsumeProgram` (`0x13981d20`) is identical in shape with the `vfc` program types.

> **NOTE — the SCS regex kind and the SCS engine tag are the same value (8); no normalization edge.** `ComputeKind` assigns the `.text(\.scs.*)?$` regex `SectionKind = 8` (the `int` stored at `v8[266]` in the decompile), and `ConsumeProgram` routes SCS on engine tag `8` — the regex kind feeds the map key directly and matches the route tag with no `9→8` step. Kind `9` is the `.note.GNU-stack` marker (`v8[304]`), which is not an SC code section and never reaches a `DefaultConstruct`. The only key transform `changeSection` applies is the `6→7` (TAC→TEC) remap under the `+0x240` flag. The full regex kind SET `{smem=0, tilespmem=1, spmem=2, hbm=3, sflag=4, tile_access=6, tile_execute=7, .text/.scs=8, GNU-stack=9}` and the engine-tag SET `{6=TAC, 7=TEC, 8=SCS}` are both CONFIRMED against the decompile. See [§Confidence Summary](#confidence-summary).

---

## The Op Jump Table — Opcode → Proto Op → EmitX

### `ConsumeScalarAluInstruction<glc::SparseCoreTacBundle>` (`0x139f09c0`)

Once the engine program is built and the [OneSlot router](oneslot-router.md) has placed a scalar-ALU op into the ScalarAlu slot, this function lowers the op into the slot's `SparseCoreScalarAlu` proto. It is a single indirect jump on the opcode:

```c
// ConsumeScalarAluInstruction<glc::SparseCoreTacBundle>   // 0x139f09c0
//   args: (printer a2/*MCInst*/, a4, proto a3 /*SparseCoreScalarAlu*/)
opcode = *(u32*)MCInst;                  // DWORD[MCInst]
idx    = opcode - 0x222;                 // jt base 0x222
if ((unsigned)idx > 0x6e)                // bound 0x6e (111 entries)
    goto OUT_OF_BLOCK;
switch jt[idx]:                          // jt @0xae8db28, 111×int32 rel offsets, 37 in-table arms
   ... per-arm: select SparseCoreScalarAlu oneof + EmitX template ...
OUT_OF_BLOCK:
   if (opcode == 0xb25) { ...clear_inst; oneof=6; DefaultConstruct Halt; return OK; }   // 2853
   if (opcode == 0xf86) { p = mutable_is_inf_or_nan(proto);
                          return EmitScalarWeird<PredicateDest, IsInfOrNan>(MCInst, p); } // 3974
   ... else → DEFAULT error ...
```

The decompile renders the jump table as a C `switch (*(_DWORD *)a2)`; the underlying prologue is the indirect jump the raw narration records (`lea eax,[r15-0x222]; cmp eax,0x6e; ja OOB; lea rdx,[rip→0xae8db28]; movsxd rax,[rdx+rax*4]; add rax,rdx; jmp rax`). The two out-of-block opcodes (`0xb25`, `0xf86`) are handled in the `default:` block after the bound check, not in the table itself.

### Arm shapes

Each of the 37 in-table arms is one of three shapes, byte-confirmed in the decompile (the OOB `IsInfOrNan` is a fourth, `EmitScalarWeird`-shaped arm reached only via the `default:` block):

| Shape | What it does | Example |
|---|---|---|
| **A — direct accessor** | `p = SparseCoreScalarAlu::mutable_<op>(proto)`, then either tail-`EmitScalarUnop`/`EmitSetRegister` with `p`, or fill the submessage inline and `return OK`. 20 ops. | `0x222`: `mutable_ceiling` → `EmitScalarUnop<…,Ceiling>` |
| **B — two-opcode EmitX** | Both opcodes of a pair test the oneof discriminator `[proto+0x50] == <tag>`; if not already set, `clear_inst`, set `[proto+0x50] = <tag>`, `Arena::DefaultConstruct<SparseCoreScalarAlu_<Op>>`, store at `[proto+0x48]`; then tail-`EmitScalar*`/`EmitSetRegister<Op,Bundle>`. 16 arm-pairs. | `0x223,0x224`: oneof `37`, `EmitScalarCompareOp<PredicateDest,…,CompareFloatingPointEq>` |
| **H — Halt** | `clear_inst`; set oneof `[proto+0x50] = 6`; `DefaultConstruct<SparseCoreScalarAlu_Halt>`; `return OK`. Reached from `0x23b` (in-table) and `0xb25` (OOB). | `0x23b`: `goto Halt` |

The two opcodes of a B-arm pair are the **predicated and non-predicated MCInst forms** of the same op — both route to the identical oneof tag and EmitX template. (The exact MCInst-operand difference between the two forms was not operand-decoded; **INFERRED** from the shared target.)

### TABLE A — the complete `0xae8db28` opcode → op → EmitX map (glc TacBundle; decompile-verified)

Every row below is read from the `ConsumeScalarAluInstruction<glc::SparseCoreTacBundle>` (`0x139f09c0`) decompiled `switch`. The `oneof` column is the discriminator value written to `[proto+0x50]` (decimal in the binary; hex shown for cross-reference).

| opcode(s) | oneof (dec/hex) | op (`SparseCoreScalarAlu_<X>`) | arm | EmitX template / accessor (TacBundle) |
|---|---|---|---|---|
| `0x222` | — | `Ceiling` | A | `mutable_ceiling` → `EmitScalarUnop<…,Ceiling>` |
| `0x223,0x224` | 37 / `0x25` | `CompareFloatingPointEq` | B | `EmitScalarCompareOp<PredicateDest,…,…Eq>` |
| `0x225,0x226` | 40 / `0x28` | `CompareFloatingPointGte` | B | `EmitScalarCompareOp<PredicateDest,…,…Gte>` |
| `0x227,0x228` | 39 / `0x27` | `CompareFloatingPointGt` | B | `EmitScalarCompareOp<PredicateDest,…,…Gt>` |
| `0x229,0x22a` | 42 / `0x2a` | `CompareFloatingPointLte` | B | `EmitScalarCompareOp<PredicateDest,…,…Lte>` |
| `0x22b,0x22c` | 41 / `0x29` | `CompareFloatingPointLt` | B | `EmitScalarCompareOp<PredicateDest,…,…Lt>` |
| `0x22d,0x22e` | 38 / `0x26` | `CompareFloatingPointNeq` | B | `EmitScalarCompareOp<PredicateDest,…,…Neq>` |
| `0x22f` | — | `Floor` | A | `mutable_floor` → `EmitScalarUnop<…,Floor>` |
| `0x231,0x232` | 29 / `0x1d` | `MaxOfTwoFloatingPointValues` | B | `EmitScalarBinop<…,Max…>` |
| `0x233,0x234` | 30 / `0x1e` | `MinOfTwoFloatingPointValues` | B | `EmitScalarBinop<…,Min…>` |
| `0x237,0x238` | 17 / `0x11` | `ConvertFloat32ToInt32` | B | `EmitScalarUnop<…,ConvertF32ToI32>` |
| `0x23b` | 6 | `Halt` | H | `clear_inst` → `DefaultConstruct Halt` |
| `0x24d` | — | `delay` | A | `mutable_delay`; inline `[+24]=operand`, `[+16] \|= 1`; `return OK` |
| `0x254` | — | `scalar_fence` | A | `mutable_scalar_fence` |
| `0x255` | — | `scalar_fence_scmf` | A | `mutable_scalar_fence_scmf` |
| `0x256,0x257` | 64 / `0x40` | `ScalarFenceSelect` | B | `EmitScalarYUnop<…,ScalarFenceSelect>` |
| `0x259` | — | `scalar_fence_stream_hbm` | A | `mutable_scalar_fence_stream_hbm` |
| `0x25a` | — | `scalar_fence_stream_spmem` | A | `mutable_scalar_fence_stream_spmem` |
| `0x25d,0x25e` | 16 / `0x10` | `ConvertInt32ToFloat32` | B | `EmitScalarUnop<…,ConvertI32ToF32>` |
| `0x265` | — | `pop_drf` | A | `mutable_pop_drf` |
| `0x269` | — | `read_register_dif_depth_register` | A | `mutable_read_register_dif_depth_register` |
| `0x26b` | — | `read_register_fence_status` | A | `mutable_read_register_fence_status` |
| `0x26c` | — | `read_register_gtc_high` | A | `mutable_read_register_gtc_high` |
| `0x26d` | — | `read_register_gtc_low` | A | `mutable_read_register_gtc_low` |
| `0x26e` | — | `read_register_lcc_high` | A | `mutable_read_register_lcc_high` |
| `0x26f` | — | `read_register_lcc_low` | A | `mutable_read_register_lcc_low` |
| `0x270` | — | `read_register_sparse_core_id` | A | `mutable_read_register_sparse_core_id` |
| `0x271` | — | `read_register_tag` | A | `mutable_read_register_tag` |
| `0x272` | — | `read_register_task_bitmap` | A | `mutable_read_register_task_bitmap` |
| `0x273` | — | `read_register_tracemark` | A | `mutable_read_register_tracemark` |
| `0x274` | — | `read_register_yield_request` | A | `mutable_read_register_yield_request` |
| `0x27b,0x27c` | 73 / `0x49` | `SetDmaCredit` | B | `EmitSetRegister<SetDmaCredit,…>` |
| `0x27d,0x27e` | 72 / `0x48` | `SetIndirectFilterValue` | B | `EmitSetRegister<SetIndirectFilterValue,…>` |
| `0x27f,0x280` | 71 / `0x47` | `SetPrefetchDepth` | B | `EmitSetRegister<SetPrefetchDepth,…>` |
| `0x283,0x284` | 74 / `0x4a` | `SetDmaThrottleSflagRange` | B | `EmitSetRegister<SetDmaThrottleSflagRange,…>` |
| `0x285,0x286` | 8 | `SetTag` | B | `EmitSetRegister<SetTag,…>` |
| `0x290` | — | `read_register_tileid` | A | `mutable_read_register_tileid` |
| `0xb25` (OOB) | 6 | `Halt` | H | OOB: `opcode == 2853` → Halt arm |
| `0xf86` (OOB) | — | `IsInfOrNan` | — | OOB: `mutable_is_inf_or_nan` → `EmitScalarWeird<PredicateDest,IsInfOrNan>` |
| **DEFAULT** | — | — | — | 58 opcodes (`0x230, 0x235, 0x236, 0x239, 0x23a, 0x23c–0x24c, 0x24e–0x253, 0x258, 0x25b, 0x25c, 0x25f–0x264, 0x266–0x268, 0x26a, 0x275–0x27a, 0x281, 0x282, 0x287–0x28f`) → `"Unsupported opcode for Scalar Alu slot: $0 : $1"` |

### EmitX template families (proto-population layer)

The arms reach exactly six `EmitX` template families plus the 20 direct accessors (two of which — `Ceiling`, `Floor` — pair a `mutable_<op>` accessor with `EmitScalarUnop`, and `IsInfOrNan`'s `mutable_is_inf_or_nan` feeds the OOB `EmitScalarWeird` arm). These templates **populate** the proto submessage; they do not write bundle bits (that is the downstream `<Slot>Encoder::Encode`).

| Family | Ops reaching it | Notes |
|---|---|---|
| `EmitScalarUnop<Bundle,Op>` | `Ceiling`, `Floor`, `ConvertFloat32ToInt32`, `ConvertInt32ToFloat32` | single-source scalar op |
| `EmitScalarBinop<Bundle,Op>` | `MaxOfTwoFloatingPointValues`, `MinOfTwoFloatingPointValues` | two-source scalar op |
| `EmitScalarCompareOp<PredicateDest,Bundle,Op>` | `CompareFloatingPoint{Eq,Gt,Gte,Lt,Lte,Neq}` | result is a predicate (note `PredicateDest`) |
| `EmitScalarYUnop<Bundle,Op>` | `ScalarFenceSelect` | Y-form unop |
| `EmitScalarWeird<PredicateDest,Op>` | `IsInfOrNan` | reached only via OOB opcode `0xf86` |
| `EmitSetRegister<Op,Bundle>` | `SetDmaCredit`, `SetIndirectFilterValue`, `SetPrefetchDepth`, `SetDmaThrottleSflagRange`, `SetTag` | register-write ops |
| direct `mutable_<op>()` | `delay`, `pop_drf`, `scalar_fence[_scmf/_stream_hbm/_stream_spmem]`, `read_register_*` (12) | simple ops; no separate `EmitX` |

> **NOTE — `EmitScalarCompareOp` and `EmitScalarWeird` carry `PredicateDest`.** The six float compares and `IsInfOrNan` produce a predicate result, so their template's first parameter is `PredicateDest` (the predicate-register destination), not the bundle type. The bundle type is the second parameter for compares, the only parameter implied for `Weird`. A reimplementer must thread the predicate destination through these arms, unlike the `Unop`/`Binop`/`SetRegister` arms which write a scalar register.

---

## The Per-(Generation, Bundle) Jump-Table Family

`ConsumeScalarAluInstruction` is a template on the bundle type, and each `(generation × bundle-type)` pair instantiates it with its own `.rodata` jump table over the **same** `0x222..0x290` opcode block. The op→proto mapping is identical across them; only the `EmitX` bundle-template argument and the per-generation namespace differ.

| Consumer | Address | Jump table | Base | Bound | Block |
|---|---|---|---|---|---|
| `glc ConsumeScalarAluInstruction<…TacBundle>` | `0x139f09c0` | `0xae8db28` | `0x222` | `0x6e` | `0x222..0x290` |
| `glc ConsumeScalarAluInstruction<…ScsBundle>` | `0x13a4f7c0` | `0xaea9df8` | `0x222` | `0x6e` | `0x222..0x290` |
| `vfc ConsumeScalarAluInstruction<…TacBundle>` | `0x13985100` | `0xae667d8` | `0x222` | `0x6e` | `0x222..0x290` |
| `vfc ConsumeScalarAluInstruction<…ScsBundle>` | `0x139d71a0` | (own jt) | `0x222` | `0x6e` | `0x222..0x290` |
| `glc/vfc ConsumeScalarAluInstruction<…TecBundle>` | `0x13a14b60` / `0x139a8dc0` | (own jt) | `0x222` | `0x6e` | `0x222..0x290` |

The `glc::ScsBundle` consumer reaches the identical `SparseCoreScalarAlu` op family with the `SparseCoreScsBundle` template argument (`EmitScalarUnop/Binop/CompareOp<PredicateDest>/YUnop<…,SparseCoreScsBundle,…>`). **So the scalar-ALU ISA is shared across the SCS (seq3) and TAC (seq4) engines** — the only per-engine difference is the bundle slot the EmitX writes into (chosen by the engine classifier above, and the slot by the [OneSlot router](oneslot-router.md)).

> **GOTCHA — the same opcode, different bundle, same op but different slot.** A scalar-ALU opcode like `0x222` (Ceiling) routes to `EmitScalarUnop<…,Ceiling>` in *both* the TAC and SCS consumers — same proto op, different bundle-template argument, different physical slot. A reimplementer writes the op-selection switch once and parameterizes it on the bundle; it must not be duplicated per engine. The 38-arm structure, base, and bound are invariant across all instantiations.

> **NOTE — the TEC bundle's non-scalar ops are dispatched elsewhere.** The TEC `ConsumeScalarAluInstruction` instantiations above handle only the TEC bundle's *scalar-ALU* slot. The TEC vector slots (`VectorAlu`/`VectorLoad`/`VectorStore`/`VectorExtended`) are routed by the separate `ConsumeOneTecBundleInstruction` (`0x13a08e00`), reaching the 142-op `VectorAlu` table via `ConsumeVectorAluInstruction` — see the [OneSlot Scalar Router](oneslot-router.md) and the [TEC Vector Opcode Enumeration](vector-opcode-enum.md).

---

## End-to-End Routing

The complete path from a decoded `MCInst` to a populated proto op, with each table and its key:

```text
SC dialect → LowerToSparseCoreLlvmPass → LLVM backend → MCInst (in a named section)
   │
   ▼  SCStreamer::changeSection (0x13b03e20)
   │     ScSection::ComputeKind (0x13afefe0)  ── RE2 section-name table → SectionKind
   │     EmplaceDecomposable {SectionKind → ScSection}  into flat_hash_map @SCStreamer+0x140
   │     stamp kind into ScSection+0x20 (engine tag);  TAC(6)→TEC(7) key remap under +0x240
   │
   ▼  RunCodeGen → MakeTpuCoreProgram (gen switch: variant_name → glc / vfc)
   │
   ▼  {Ghostlite,Viperfish}Emitter::ConsumeProgram (0x139ed5e0 / 0x13981d20)
   │     crc32 probe flat_hash_map → engine tag @slot+0x28
   │        8 → SparseCoreScsProgram (SCS, seq3)
   │        7 → SparseCoreTecProgram (TEC, seq5)
   │        6 → SparseCoreTacProgram (TAC, seq4)
   │     per-engine: GetScalarAluSlot + EmitPredicationToSlot + ↓
   │
   ▼  ConsumeScalarAluInstruction<gen::Bundle>   ── JUMP TABLE on DWORD[MCInst]  (THIS PAGE)
   │     idx = opcode - 0x222; bound 0x6e; jt[idx] → 1 of 37 in-table arms (+ 2 OOB + default)
   │     arm: select SparseCoreScalarAlu oneof [proto+0x50] + active msg [proto+0x48]
   │
   ▼  EmitScalar{Unop,Binop,CompareOp,YUnop,Weird} / EmitSetRegister<Op,Bundle>
   │     populate the proto submessage (NO bit packing)
   │
   ▼  ConvertToTpuCoreProgram → <Engine>CodecBase::Encode → <Slot>Encoder::Encode → BitCopy
         the absolute bundle bit positions (downstream; not this page)
```

The two classifier keys are orthogonal: the **section name** (RE2 → `SectionKind` → engine tag) picks the engine program and therefore which `ConsumeScalarAluInstruction<…Bundle>` instantiation runs; the **`MCInst` opcode** (`0xae8db28` jt) picks the op + EmitX template inside it.

> **GOTCHA — two `0xC0`-adjacent traps in the op/kind mapping.** Two assignments are easy to get backwards and are byte-pinned here: the SCS regex `.text(\.scs.*)?$` stores `SectionKind` **8** — the same value `ConsumeProgram` reads back as the SCS engine tag (`v8[266]=8`), so there is no `9↔8` normalization edge; kind **9** belongs only to `.note.GNU-stack` (`v8[304]`). And `0x25d,0x25e` is `ConvertInt32ToFloat32` (oneof 16), **not** `Max`/`Min` — `Max`/`Min` live at `0x231,0x232` / `0x233,0x234` (oneof 29/30).

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| Op selection is a jump table on `DWORD[MCInst]`; base `0x222`, bound `0x6e`, 111 entries, 37 in-table arms | `0x139f09c0` decompile `switch (*(_DWORD*)a2)`; jt `0xae8db28` | CONFIRMED |
| Proto oneof discriminator `[proto+0x50]`, active msg `[proto+0x48]` | decompile `*(_DWORD*)(a3+80)` / `*(_QWORD*)(a3+72)` per arm | CONFIRMED |
| TABLE A opcode→op→EmitX→oneof mappings (all 37 in-table arms + 2 OOB) | `0x139f09c0` decompile, per-case `mutable_<op>` / `DefaultConstruct<…>` + `*(a3+80)=tag` + tail `Emit*` | CONFIRMED (re-derived) |
| TABLE A corrects the raw narration's compare/`Set*`/`Max`/`Min`/`delay`/`Halt` assignments | side-by-side of decompile cases vs raw TABLE A | CONFIRMED (correction) |
| Two OOB opcodes `0xb25`→Halt, `0xf86`→`EmitScalarWeird<PredicateDest,IsInfOrNan>` | decompile `default:` `v7 == 2853` / `v7 == 3974` | CONFIRMED |
| Default error `"Unsupported opcode for Scalar Alu slot: $0 : $1"` | decompile `SubstituteAndAppendArray(…, 47, …)` | CONFIRMED |
| Per-`(gen,bundle)` jump-table family, same block/base/bound, only bundle-template arg differs | sibling consumers `0x13a4f7c0` (glc Scs), `0x13985100` (vfc Tac); EmitX `<…SparseCoreScsBundle>` vs `<…TacBundle>` | CONFIRMED |
| `ComputeKind` = 9 static RE2 regexes (`__cxa_guard`), first `FullMatchN` wins → `SectionKind` | `0x13afefe0` decompile: 9 `RE2::RE2(...)` + `FullMatchN` chain + `*v4` kind store | CONFIRMED |
| The 9 regex literals and their kinds (TABLE in §Engine Classifier) | decompile string literals + per-entry kind store | CONFIRMED |
| Engine route `crc32` map probe → tag `slot+0x28`: `8`=Scs, `7`=Tec, `6`=Tac | `0x139ed5e0` decompile `_mm_crc32_u64` + `v27 == 7` + `DefaultConstruct<SparseCore{Tec,Tac,Scs}Program>` | CONFIRMED |
| `TAC(6) → TEC(7)` map-key remap under `SCStreamer+0x240` | `changeSection` `0x13b03e20`: `if (*((_BYTE*)this+576)==1) if (kind==6) key=7` before the `crc32` map insert | CONFIRMED |
| SCS regex kind = engine tag = `8` (no normalization); kind `9` is GNU-stack | `ComputeKind` stores `v8[266]=8` for `.text/.scs`, `v8[304]=9` for GNU-stack; `ConsumeProgram` routes SCS on tag `8` | CONFIRMED |
| Pred / non-pred semantics of the two-opcode B-arm pairs | both opcodes share oneof tag + EmitX template; operand-level predicate not decoded | INFERRED |
| The 58 DEFAULT opcodes are SCS-sequencer / other-slot ops | confirmed they hit the TAC "Unsupported" error; their home consumer not cross-walked here | INFERRED |

---

## Cross-References

- [SC Backend Pipeline](sc-backend-pipeline.md) — the twelve-pass MLIR pipeline that lowers SC dialect to the `MCInst` stream this dispatcher consumes; the layer above `RunCodeGen`.
- [OneSlot Scalar Router](oneslot-router.md) — the scalar-slot placement router that chooses *which bundle slot* a scalar op occupies and tail-calls `ConsumeScalarAluInstruction` (this page) for the ScalarAlu slot.
- [TEC Vector Opcode Enumeration](vector-opcode-enum.md) — the 142-op `VectorAlu` table reached by the separate vector dispatcher; the vector analog of this scalar op table.
- [SCS Scalar Opcode Enumeration](scalar-opcode-enum.md) — the scalar opcode roster and the `setSlotFlagInMCInst` slot-flag discipline upstream of this dispatcher.
- [SCS (Scalar) Engine](scs-engine.md) — the seq3 SCS engine (engine tag 8); shares the scalar-ALU op family with TAC.
- [TAC Engine](tac-engine.md) — the seq4 tile-access engine (engine tag 6); the owner of the `glc::TacBundle` jump table documented here.
- [TEC (Vector) Engine](tec-engine.md) — the seq5 vector engine (engine tag 7); its scalar-ALU slot reuses this op table, its vector slots do not.
- [getSequencerType](getsequencertype.md) — the SCS/TAC/TEC engine-selection function; the engine-tag counterpart to the section classifier on this page.
- [SparseCore Overview](overview.md) — the three engine classes, per-generation presence (vfc / glc / gfc), and the codec-template sequencer enum.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore back-end — [back to index](../index.md)
