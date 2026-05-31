# Legacy Backend A — `sub_A97600` Per-Target Operand-Class Query

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

`sub_A97600` is a 7,780-byte, 425-block per-target virtual method installed at vtable slot **+0x5F0** (index 190) on six of ptxas's legacy SM-target classes. Despite its size and the lure of its position adjacent to other scheduling-related virtuals, it is **not** a scheduler. It is the legacy implementation of the per-instruction, per-operand **source-slot-count / operand-class query** that the scheduling and register-pressure machinery consults whenever it needs to know "how many physical source-register slots does operand `k` of this instruction occupy under the current target's hardware encoding?". On modern SM tiers (sm_80+) this query is answered by a per-opcode dispatch table; on the six legacy target classes it is answered by this single 7.8-KB switch.

The `Wave 18D` reference in [post-schedule.md § Function Map](./post-schedule.md#function-map) describing `sub_A97600` as `PostSchedulePass::runOnFunction` is **incorrect on multiple counts** and is superseded by this page. The corrections, in summary:

* The function is not at sub-target vtable slot `+0x90` — it is at primary-target vtable slot `+0x5F0` (vtable index 190, byte offset 1520).
* The signature is `int(target *this, ptxInstr *, int operand_idx)` returning a small integer — not a `void post_schedule(this)` driver.
* The body contains no instruction-list re-ordering, no dependency-DAG construction, no register-pressure tracker, no priority queue. It is one big `switch` on `(*(uint32_t*)(instr+72) >> 8) & 0xCF` with per-opcode operand-slot accounting.
* It is referenced by exactly **6 vtables** (all the same offset 0x5F0), each a different legacy SM-target class — and it has zero callers via direct call: every invocation flows through `(target->vtable + 1520)`.

The remainder of this page documents what the function actually does, why it exists in a separate 7.8-KB switch instead of a dispatch table, how the modern path replaces it, and what role the result plays in scheduling/RA.

| | |
|---|---|
| **Symbol** | `sub_A97600` |
| **Address range** | `0xA97600` -- `0xA99464` (7,780 bytes) |
| **Basic blocks** | 425 |
| **Callees** | 18 distinct functions, 52 call edges (`sub_7E36C0`, `sub_7E3640`, `sub_7E3790`, `sub_7E3800`, `sub_7E40E0`, `sub_7E3EF0`, `sub_693CA0`, `sub_80B620`, `sub_8963B0`, `sub_91E610`, `sub_A97540`, ...) |
| **Direct callers** | 0 |
| **Indirect entry** | 6 vtables, all at slot offset `+0x5F0` (vtable index 190 / byte 1520) |
| **Helper** | `sub_A97540` (180 B, 3 BBs) — operand-range probe shared with the same six targets |
| **Signature** | `int(target *this, uint32_t *instr, int operand_idx)` |
| **Return value** | small non-negative integer (0, 1, 2, or `(end - begin)` operand-slot count) |
| **Variadic XMM form** | also called as `int(target *this, uint32_t *instr, int operand_idx, double, double, double, double, double, double, double, double)` from `sub_7EAD70` and `sub_7F5D50` (eight unused XMM slots reserved by the SysV ABI for the variadic latency variant; harmless on this side) |

## Why six vtables?

The six vtables that install `sub_A97600` at slot 190 are:

| Vtable | Slots | Sibling slot 189 (`sub_A90D60`) | Sibling slot 191 |
|---|---|---|---|
| `0x21D6860` | 470 | shared | `sub_AAEF20` |
| `0x21D82B0` | 466 | shared | `sub_8102C0` |
| `0x21F5B70` | 443 | shared | `sub_8102C0` |
| `0x21F9158` | 470 | shared | `sub_AAEF20` |
| `0x229D418` | 469 | shared | `sub_8102C0` |
| `0x22B2A58` | 455 | shared | `sub_8102C0` |

All six share the same slot-189 method (`sub_A90D60`, the predecessor-operand-eligibility query) and split into two slot-191 cohorts on a binary discriminator (`sub_AAEF20` vs `sub_8102C0`). The slot-190 (operand-class) method, slot-189 (pred-op-eligibility) method, and slot-191 (binary cohort discriminator) form a **tight three-method invariant of the legacy target hierarchy**. Modern targets at sm_80+ install different functions at every one of these slots — in particular, the modern slot-190 implementations are smaller (`sub_44A308`, `sub_7DB178`, etc.) and dispatch through the per-opcode tables documented in [codegen/encoding-tables.md § Per-SM-Tier Encoder Index Tables](../codegen/encoding-tables.md#per-sm-tier-encoder-index-tables--0x22a5aa0-family). The 7.8-KB switch is the legacy artifact of a path that was never table-driven.

The exact SM mapping is not in RTTI (the typeinfo and offset-to-top fields above each of the six vtables are zeroed in the binary), but the slot-size pattern (~466 slots), the sibling-method identity, and the lack of any Ampere-era specialisations on these six targets are consistent with the pre-sm_80 family (Maxwell sm_50/sm_52/sm_53, Pascal sm_60/sm_61/sm_62, Volta sm_70/sm_72, Turing sm_75 — nine SM variants, six target classes after deduplication of trivially identical sub_targets).

## What the function computes

`a1` is the target's `this` pointer (and through `*(QWORD*)a1` its own vtable); `a2` is a pointer to the 32-bit instruction-word stream of a single ptxInstr; `a3` is an operand index. The function dispatches on the masked primary-opcode field:

```c
opcode32 = *(uint32_t *)(a2 + 72)        // primary opcode + flags word
masked   = opcode32 with byte 1 ANDed by 0xCF
                                          // strips out the two "extension-class"
                                          // bits that don't change operand layout
switch (masked) {
    case 0x10:  // narrow integer ALU class
    case 0x16:  // load with computed index range
    case 0x32:  // tex/sampler class (xmmword opcode-mask templates)
    case 0x3D:  // single-source pure-predicate test
    case 0x4D:  // memory access with bit-encoded width/space
    case 0x77:
    case 0xCA:
    ...
    case 335:   // (special case for an opcode that lives at base 0x14F)
    default:
}
```

For each case the body:

1. Reads opcode flag bits — typically `(opcode32 >> 11) & 2` (operand-base bias) and `(opcode32 >> 4) & 7` (operand-class hint).
2. Looks up the operand-encoding window for the requested `a3` against per-opcode ranges supplied by helpers (`sub_7E40E0`, `sub_7E3640`, `sub_7E3790`, `sub_7E3800`, `sub_7E36C0`, `sub_693CA0`, `sub_80B620`, `sub_8963B0`).
3. Returns one of:
   * `0` — operand at index `a3` is not a register source for this instruction (e.g. immediate or predicate-only),
   * `1` — operand is one single register slot,
   * `2` — operand consumes two register slots (a 64-bit / paired operand),
   * `end - begin` — operand consumes an explicit run of consecutive slots, where `begin` / `end` come from the per-opcode range helpers (the vector / tex-coord / wide-load cases).

### Switch-case inventory

The switch has 33 explicit case labels grouped by operand-encoding family, plus a sprawling `default:` arm. The numeric value is the masked primary-opcode field `(*(uint32_t *)(instr+72)) & ~0x3000`:

| Case(s) | Operand-encoding family | Representative semantics |
|---|---|---|
| `0x10` | narrow integer ALU class | Operand index decoded from `*(instr+80)` minus a bias derived from `(opcode32 >> 11) & 2`. Returns 0 / 1 / `2*regs` / `3*regs` depending on operand-type bits `(slot >> 4) & 0x1F`. |
| `0x16` | computed-index window load | Walks the predecessor-instruction chain looking for opcode-0x29 chained-load entries; uses `sub_7E40E0(a2, 3..4)` to bracket the operand range. Returns `end - begin` for sources inside the range, `0` outside. |
| `0x32` | texture/sampler operand class | Materialises an 80-byte scratch buffer from `xmmword_21B2EC0` + three immediate `_QWORD` literals, indexes by `(slot >> 2) & 3` to pick the four-coordinate window. See QUIRK below. |
| `0x3D` | single-source predicate test | Returns `2` if the operand encodes a register and the instruction is sign-extended (`opcode32 ^ 0x70000000` != 0); `0` otherwise. |
| `0x4D` | memory access with bit-encoded space/width | Eight-way switch on `(slot >> 4) & 7`. Calls `sub_7E36C0(2, ...)` to resolve the operand bound, with a side helper `sub_A97540` testing whether the operand falls inside the base-load window. |
| `0x53` | wide register-set load | `sub_7E3640(a2, 3..4)` brackets, same chained-load walk as `0x16`. |
| `0x55` | constant-bank reference | Loads the constant-bank descriptor from `target.constantPool[insn.constID]`, returns the per-bank slot count. |
| `0x5A` | predicate-only test, all-sources negative | Returns 2 when the addressed operand AND operands 0 and 4 are all sign-encoded negatives. |
| `0xB7`, `0x120` | tex/surface load with vector destination | Returns `(slot & 7) + 1`, gated by the `0x4000` "vector-destination" bit, with a chained-load post-walk for opcode 288 (`ATOMS`). |
| `0xB8`, `0xB9` | per-operand surface store class | |
| `0xCB` | tex with separate sampler operand | |
| `0xD3` | LDG.E.128 / LDG.E.U.128 wide global load | |
| `0xDF`, `0xEA`, `0xEE` | vectorised store class | Three nearly-identical case labels share a body. |
| `0xE4` | atomic with packed return | |
| `0xE9`, `0xED` | atomic with separate return register | |
| `0x129` | warp-level reduction class | `sub_7E40E0(a2, 3..4)` brackets, returns `end - begin` for sources inside the range. |
| `0x12B`, `0x12C` | warp-level shuffle class | |
| `0x135` | tensor-load class | Materialises four xmmword templates into a 64-byte stack scratch, indexed by `(slot >> 18) & 3`. Uses `sub_7E39B0`/`sub_7E3BA0`/`sub_7E3C30` for the per-axis stride lookups. |
| `0x13A`, `0x144` | tensor-store class | |
| `0x13D`, `0x13E`, `0x13F` | TMA descriptor class | Three labels share a single body that returns `1` for the descriptor operand, `0` elsewhere. |
| `0x149`, `0x14A`, `0x14B` | bulk-transfer / cp.async class | |
| `0x14F` | shfl.idx.b32 with explicit lane mask | Special-cased because the lane-mask operand is at a non-canonical position. |
| `0x150`, `0x153`, `0x15D`, `0x160` | per-opcode niche cases | Cover instructions that don't fit any of the four window-resolver families. |
| `0x129` (revisit) / `0x135` | tensor descriptor encoding | `sub_7E3A70(scratch, 64, ...)` builds the 80-byte window descriptor. |
| **`default:`** | every opcode the explicit cases did not name | Multi-stage fallback that calls back into the same target's vtable+1024 (`isFlatMemorySpace`), vtable+1056 (`isCachedAddrMode`), vtable+1288 (`getOperandBaseBias`), and vtable+1480 (`predOperandClassifier`, optionally compared to `sub_A8D330`). The default arm is the largest single piece of the body — roughly 180 BBs — because it has to handle every opcode that's not in the explicit list, and the answer depends on per-target capabilities rather than a single closed-form rule. |

The return value flows directly into the scheduling latency/pressure computation. Two representative consumers:

* `sub_7E4150` (slot-1520 callsite around line 67) multiplies it into the per-instruction latency: `if (v14 > 4) v9 *= v14 >> 2;` — the per-operand slot count scales the latency contribution of that operand.
* `sub_7E8200` (slot-1520 callsite at line 12) uses it as a Boolean gate (`<= 0` -> bail) for whether the operand even participates in the read-port-conflict check.
* `sub_7EAD70` and `sub_7F5D50` use the variadic-double form of the same slot during the latency accumulator in the SASS-encoder-time scheduling-statistics walk.

Without this method the scheduler cannot know, for a given ptxInstr, how many slots in the register-port window are consumed by operand `k`. The pre-RA scheduler needs that count to size live-range bitvectors. Post-RA scheduling (Backend C, `sub_18FDAF0`'s `PressureCost`) uses the same query on a different per-target vtable to compute the per-instruction operand-pressure double in the RBT score.

### Reduced excerpt — case `0x10` (narrow integer ALU)

The shortest case in the body is `0x10`. It is reproduced here lightly cleaned to show the canonical shape every case follows:

```c
// inputs:  a1 = LegacyTarget*  (this)
//          a2 = ptxInstr*       (pointer to 32-bit instruction-word stream)
//          a3 = operand_idx     (which operand we are asking about)
case 0x10u:
{
    uint32_t opcode_word = *(uint32_t *)(a2 + 72);
    int       base_off   = *(int *)(a2 + 80) + ~((opcode_word >> 11) & 2);
    uint64_t  base       = a2 + 84;
    uint32_t  slot       = *(uint32_t *)(base + 8LL * base_off);
    int       width      = slot & 0xF;                  // operand-width bits
    int       cls        = (slot >> 4) & 0x1F;          // operand-class hint
    int       step;
    int       mult;

    if (cls - 12u <= 1 || cls == 2) {
        mult = 3 * width;
        step = 2;
    } else if (cls - 14u > 1) {
        if ((int)width <= 1) return 0;                  // not a register source
        mult = 2 * width;
        step = 1;
    } else {
        mult = width;
        step = 0;
    }
    int  bit = (*(int *)(base + 8LL * width) < 0);      // sign-encoded?
    if (bit + mult <= a3) return 0;                     // beyond the window
    if (width + bit <= a3) return width * step;         // inside the window, scaled
    if (width + bit - 1 == a3 && *(int *)(base + 8LL * width) < 0)
        return 1;                                       // sign-extended single slot
    return width;                                       // canonical: width regs
}
```

Every other case follows the same six-step template — read the opcode flag word, decode the operand-class field, pick a width / stride pair, bound-check `a3`, return the slot count. The body is 7.8 KB because there are 33 explicit cases plus a 180-BB default, each with a slightly different decoding rule for its opcode family.

## Why a 7.8-KB switch and not a dispatch table

Modern SM tiers (sm_80--sm_120) answer the same query through the per-opcode tables documented in [codegen/encoding-tables.md](../codegen/encoding-tables.md). The replacement is one `lea r10, [rip + table]; jmp [r10 + 8*opcode_category]` indirected through one of the 444-slot percase tables in the `0x22A5AA0` family — at most 470 cache-warm pointer chases, no per-case decision tree.

The legacy switch is a 425-block decision tree because the legacy targets predate the table-driven encoder family. The pre-sm_80 encoding-tables (`0x22A5AA0` and its cohort) have 93 nullsub holes for opcodes that did not exist in those architectures. Without a way to express "this opcode does not exist on this target" cleanly in the table, the original implementer fell back on a hand-written switch with explicit per-opcode operand accounting. The switch fans out into 425 BBs because every case has its own per-operand-index logic (read the type bits, decide whether to extend a vector window, branch on whether the operand is the destination or a source, ...).

The two designs are functionally equivalent on the opcodes both support. The switch is simply the older form, kept around for the six target classes whose opcode set never changed enough to justify migrating to the table-driven format.

## Position relative to PostSchedule (phase 110)

[Phase 110 -- PostSchedule](./post-schedule.md) is a 51-byte dispatcher that indirects through `(target->subtarget)->vtable[+0x90]`. The body's only purpose is to call into the sub-target's `postSchedule` virtual. That `+0x90` slot, **not** the `+0x5F0` slot on which `sub_A97600` lives, is the post-RA scheduling hook. The two slots are unrelated:

* `+0x90` (slot 18 on the **sub-target** vtable): post-RA scheduling driver. Installs `nullsub_45` on the legacy six targets (PostSchedule no-op), or `sub_1908D90` (Backend C) on sm_80+.
* `+0x5F0` (slot 190 on the **primary target** vtable): per-operand source-slot-count query. Installs `sub_A97600` on the legacy six targets, or per-opcode-table-driven equivalents on sm_80+.

`PostSchedule` running on a legacy SM (it does not — the SM-version gate at the top of `sub_C60640` aborts for `SmVersion <= 1`) would still not call `sub_A97600`. The two slots address different vtable, different offset, different signature, different role.

## Interaction with the scheduling pipeline

Every caller of `(target_vtable + 1520)` is a function whose body needs to know, for some instruction `instr` and some operand index `k`, how many register-file slots that operand consumes under the current target's encoding. The dominant clients are:

1. **Pre-RA dependency-graph build** (`sub_8D9930` / `sub_894290` -> `getOperandSrcSlotCount`): when constructing RAW / WAR / WAW edges, the engine must know which physical slots in the register-port window each operand occupies. The slot count returned by `sub_A97600` sizes the per-operand window over which the bitvector trackers `def_bv` and `use_bv` mask-OR.
2. **Latency cost computation** (`sub_7E4150`): the per-operand latency contribution to the priority score is scaled by `slot_count / 4` when `slot_count > 4`. A wide tensor-load operand thus contributes proportionally more pressure than a single 32-bit source.
3. **Read-port conflict accounting** (`sub_7E8200`): an operand whose `getOperandSrcSlotCount` returns zero is treated as "no register-port consumer" — immediate operands, predicate-only operands, and TMA-descriptor handles all gate out of read-port-conflict checks via this path.
4. **Post-RA pressure cost** (`sub_18FDAF0`'s `PressureCost` helper, on sm_80+ via a different vtable slot): the same query is performed against the per-opcode dispatch tables, returning identical semantics. The post-RA result feeds into key 2 of the RBT priority comparison.

All four clients treat the return value as load-bearing for correctness, not just performance: an operand wrongly classified as a register source would induce false RAW edges and serialise the schedule; an operand wrongly classified as non-register would let the scheduler issue back-to-back read-port-conflicting instructions, producing a wall-clock stall the encoder cannot express in the control word. The function is therefore in the small set of legacy-target methods that ptxas refuses to no-op even when other parts of the target stack are disabled.

## QUIRK -- 7.8 KB of code, zero direct callers

`sub_A97600` is **only** reachable through `(target->vtable + 1520)`. The IDA xref database returns no incoming direct calls — every entry is the function's own intra-body branches. This is normal for a virtual method, but the size makes the function feel like it should have direct entry points. It does not. A reimplementer hunting for callers must walk *every* user of `(target_vtable + 1520)`: at least 18 functions in the decompiled corpus call through this slot (sub_7E4150, sub_7E8200, sub_7EAD70, sub_7F5D50, sub_806F80, sub_887F00, sub_92EF10, sub_93A030, sub_93A0D0, sub_93BA60, sub_9511E0, sub_967000, sub_967860, sub_973550, sub_A90D60, sub_A94B80, sub_122AD60, ...). Searching for the symbol name yields nothing. Searching for the literal `0xA97600` in the rodata yields only the six vtable installs.

## QUIRK -- the case-`0x32` xmmword scratch buffer

Inside `case 0x32u` (the tex-sampler operand-class case) the body materialises three xmmword-sized constants from `.rodata:xmmword_21B2EC0` plus three immediate `_QWORD` literals (`433471489971520000LL`, `0xE0A0804000B07LL`, magic `336595972`) into an 80-byte stack scratch buffer `v229[80]`. The constants encode the operand-slot windows for the four sampler-coordinate combinations indexed by `(opcode32 >> 2) & 3`. The scratch buffer is **written three times** during the case (the constants are re-materialised before each lookup). This is a Hex-Rays artefact: the underlying assembly issues a single `movdqa` plus three `mov [rsp+...]`s and reuses the buffer; the decompiler renders each access as a fresh store because the buffer is unfreed in between. A reimplementer should not infer that the constants change between accesses — they do not. The pattern is "fetch 16-byte coordinate-class table into stack, index by sampler-mode, return slot-window width".

## QUIRK -- vtable+1024 / vtable+1056 / vtable+1288 / vtable+1480 self-callbacks

The `default:` arm of the switch (lines 1180--1364 of the decompilation) calls back into the **same target's** other vtable slots — vtable+1024, +1056, +1288, +1480 — to ask sub-queries like "does this target support 64-bit address mode?" and "does this target classify opcode 6 as a memory access?". The reason is that the `default` arm has to handle every opcode the seven explicit cases do not, and the precise answer depends on per-target capabilities. Rather than encoding all of those capabilities in a per-target operand-class table, the legacy implementation re-uses the target's other introspection hooks. The cost is that an unfamiliar reader watching a debugger step through the `default` arm sees the same target's `this` pointer bouncing through four different vtable slots before producing a final answer. This is the third-party indirection load that the modern table-driven replacement eliminated.

## Function Map

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_A97600` | 7,780 B | `LegacyTarget::getOperandSrcSlotCount(this, instr, operand_idx)` -- vtable slot +0x5F0 on six legacy SM-target classes | HIGH |
| `sub_A97540` | 180 B | `LegacyTarget::isOperandInBaseWindow(this, operand_idx)` -- helper used only by `sub_A97600`'s case `0x4D` arm | HIGH |
| `sub_A90D60` | 1,120 B | `LegacyTarget::isOperandEligibleAsPredecessor(this, instr, operand_idx, flag)` -- sibling at vtable slot +0x5E8 (slot 189) on the same six vtables | HIGH |
| `sub_7E36C0` / `sub_7E3640` / `sub_7E3790` / `sub_7E3800` / `sub_7E40E0` | 76--204 B | Per-operand-class range helpers (operand-window start/end resolvers) | HIGH |
| `sub_91E610` | 399 B | `getOperandTypeCode(instr, operand_idx)` -- shared helper used by 100+ functions | HIGH |
| `nullsub_45` (`0x680190`) | 2 B | The post-RA-no-op sentinel — **unrelated** to `sub_A97600`, lives on the sub-target vtable +0x90, not the primary target vtable +0x5F0 | CERTAIN |

## Cross-References

* [Scheduling Algorithm](./algorithm.md) -- the Backend A / B / C taxonomy. `sub_A97600` is not a scheduler; the scheduler queries it.
* [Phase 110 -- PostSchedule](./post-schedule.md) -- supersedes the misclassification of `sub_A97600` in that page's function map.
* [Latency Model](./latency-model.md) -- the per-operand latency model that multiplies `sub_A97600`'s return value into per-source register-port costs.
* [SASS Encoding Function-Pointer Dispatch Tables](../codegen/encoding-tables.md#per-sm-tier-encoder-index-tables--0x22a5aa0-family) -- the modern (sm_80+) table-driven replacement.
* [Pipeline Phase Table](../passes/index.md) -- shows the full 159-phase pipeline that this query is consulted from.
