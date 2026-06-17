# ISA Validator Architecture and Entry Tree

> *All addresses, offsets, struct ordinals, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce` (runtime-lib **2.31.24.0-0b044f4ce**; `libnrt.so.2.31.24.0`, ELF64, **not stripped**, DWARF present, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). `.text` VMA equals file offset for the cited ranges. Struct offsets are read from DWARF (`structures.json`), enum values from DWARF (`enums.json`), function addresses/sizes/caller-counts from the function table (`functions.json`), the dispatcher jump table from `switches.json`. Other versions will differ.*

## Abstract

Before any [64-byte instruction record](instruction-record.md) is staged into device HBM, the runtime runs it through an **auto-generated instruction-sanity layer**: a forest of pure, side-effect-free predicates that decode the fixed-width TPB (Tensor Processing Block) encoding and decide whether each field, enum, reserved bit, and tensor descriptor is structurally legal for that opcode on that NeuronCore generation. This is the runtime-side analog of an assembler's encode-time `assert` battery — except it runs at *model-load* time, on records the compiler already emitted, as a last gate against a malformed NEFF corrupting the sequencer. The layer touches no IOCTL, no `mmap`, no device memory, and no locks; it is a closed system of `bool`-returning leaf functions over the instruction union and its sub-fields.

The defining structural fact is that **every validator exists twice**. There is a *silent* family — `is_valid_<opcode>` / `<descriptor>_valid` / `has_valid_*` — that returns a bare `bool` and is the fast path the runtime actually gates on; and a *debug* family — `dbg_is_valid_<opcode>` / `dbg_has_valid_*` / `dbg_tensorNd_valid` — that runs the **same logical checks** but returns a 1281-byte `NEURON_ISA_TPB_DEBUG_BOOL` whose `err_msg[16][80]` matrix is filled, on failure, with the failing predicate's name: `"inst failed assertion check: '<name>'"`. The two families are mechanically related: each debug twin is the silent predicate with a `NEURON_ISA_TPB_DEBUG_BOOL` constructor/merge plumbing wrapped around every check. The size delta is dramatic and diagnostic — `is_valid_dtype` is 46 bytes (`@0x27b910`, a true leaf) while its twin `dbg_is_valid_dtype` is **2233 bytes** (`@0x27f940`); `has_valid_neuron_events` is 190 bytes (`@0x27da30`) while `dbg_has_valid_neuron_events` is **2452 bytes** (`@0x280e40`). The bloat is entirely the inlined `qmemcpy(…, 0x500)` of the 1280-byte message matrix and a recurring 16×80 SIMD compaction loop, not extra validation logic.

Both families are GENERATED. DWARF `addr2line` pins the silent/debug validator clones to `aws_neuron_isa_tpb_assert.h` under `neuron_<arch>_arch_isa/tpb/` — a per-arch ISA-specification header from which a code generator emits, for every opcode, one inline `is_valid` predicate, one `dbg_is_valid` twin, and the per-arch clone set. This page documents three artifacts a reimplementer must reproduce: (1) the **entry tree** — how `ib_is_valid_engine_instruction_v{2,3,4}` forks into a silent fast path (`is_valid_neuron_instruction`) and a debug diagnostic path (`debug_invalid_neuron_isa_instruction`, a 256-case opcode jump table); (2) the **`NEURON_ISA_TPB_DEBUG_BOOL`** return ABI and the err-message discipline; and (3) the **shape of a representative validator and its twin** — the silent leaf, the debug wrapper, and the shared `dbg_has_valid_neuron_header` / `dbg_has_valid_neuron_events` preamble that gates ~91 of the per-opcode validators. The per-opcode *field meanings* live next to the [64-byte record format](instruction-record.md); the per-arch deltas (the three emitted clone bands) live on [Per-Arch Validators](validators-per-arch.md).

For reimplementation, the contract is:

- **The two-family architecture** — every opcode has a silent `bool` validator (the gate) and a `dbg_` twin (the diagnostic), running identical checks; the twin differs only by returning `NEURON_ISA_TPB_DEBUG_BOOL` and naming the failing assertion.
- **The `DEBUG_BOOL` ABI** — `{bool result@+0; char err_msg[16][80]@+1}`, size `0x501` (1281), returned via hidden `__return_ptr`; failure writes the assertion name into a message slot.
- **The entry fork** — `ib_is_valid_engine_instruction_v{2,3,4}` maps the HAL engine id to an ISA engine, calls the silent `is_valid_neuron_instruction`, and *only when it fails* re-runs through `debug_invalid_neuron_isa_instruction` to obtain the human-readable reason.
- **The composition rule** — every per-opcode validator ANDs `has_valid_neuron_header` (opcode-enum + `inst_word_len==16`) and `has_valid_neuron_events` (the wait/update sync tuple) with opcode-specific field/enum/reserved-zero checks, merged into one result.
- **The generated origin** — validators are emitted from `aws_neuron_isa_tpb_assert.h`; the silent and debug forms and the three per-arch clones are all codegen products, not hand-written.

| | |
|---|---|
| **Per-arch entry points** | `ib_is_valid_engine_instruction_v2` `@0x2f6b80` (153 B) / `_v3` `@0x3aa1e0` (2747 B) / `_v4` `@0x43f810` (16087 B) |
| **Silent dispatcher** | `is_valid_neuron_instruction` `@0x2f2350` (17952 B, 530 BB, 183 callees) — returns `bool` |
| **Debug dispatcher** | `debug_invalid_neuron_isa_instruction` `@0x2ecbb0` (339 B) — **256-case** opcode jump table → `dbg_is_valid_<op>` |
| **Debug return type** | `NEURON_ISA_TPB_DEBUG_BOOL` (DWARF ordinal 8729, size **1281** = `0x501`) |
| **Shared header check** | `dbg_has_valid_neuron_header` `@0x280200` (699 B, **91 callers**) / silent `has_valid_neuron_events` `@0x27da30` |
| **DEBUG_BOOL ctor / merge** | `neuron_isa_tpb_new_dbg_bool` `@0x27b440` (119 B) / `neuron_isa_tpb_merge_dbg_bool` `@0x27b4c0` (392 B) |
| **Legality oracle** | `is_valid_enum` `@0x27b650` (691 B, leaf, **210 callers**) — the most-called predicate in the layer |
| **Generated source** | `aws_neuron_isa_tpb_assert.h` (DWARF, per-arch `neuron_<arch>_arch_isa/tpb/`); load-path TU `instruction_block_cayman.c` |

---

## 1. The Entry Tree

### Purpose

The validator layer has a single externally-visible entry per NeuronCore generation: `ib_is_valid_engine_instruction_v{2,3,4}`. Everything below it is a closed predicate forest. A reimplementer needs the fork structure first — which function is the gate, which is the diagnostic, and why the layer pays for two complete validator forests instead of one. The answer is a deliberate cost trade: the silent forest is small and branch-dense (it is what runs on every record of every model load), and the debug forest is large and message-heavy (it runs only after the silent forest has already said "no", to turn a boolean failure into a named reason).

### Entry Point

```text
ib_is_valid_engine_instruction_v2 (0x2f6b80, 153 B)        [v3: 0x3aa1e0 / v4: 0x43f810]
  ├─ ib_hal_to_isa_engine_v2     (0x2f6b30, 72 B)   ── HAL engine id → NEURON_ISA_TPB engine
  ├─ instruction_engine_check    (0x299220, 1189 B) ── opcode legal for THIS engine?
  ├─ is_valid_neuron_instruction (0x2f2350, 17952 B)── SILENT gate: bool, 183 leaf callees
  └─ ib_is_valid_engine_instruction_v2_0 (0x2f1810) ── thin wrapper / re-entry

  (failure path, to obtain a reason string)
debug_invalid_neuron_isa_instruction (0x2ecbb0, 339 B)     ── 256-case opcode jump table
  ├─ case 0xA1 → dbg_is_valid_halt          (0x28aa80, 3590 B)
  ├─ case 0xA6 → dbg_is_valid_notify        (0x28b890, 3571 B)
  ├─ case 0xB5 → dbg_is_valid_branch_prefetch_hint (0x328e20, 791 B)
  ├─ case 0xF0 → dbg_is_valid_extended_inst (0x28ca30, 1109 B)
  └─ … 252 more opcode cases → dbg_is_valid_<op>
       each → dbg_has_valid_neuron_header (0x280200) + dbg_has_valid_neuron_events (0x280e40)
            + opcode-specific dbg_is_valid_enum / dbg_ctrl_no_reserved_zero / …
            + neuron_isa_tpb_new_dbg_bool (0x27b440) / merge_dbg_bool (0x27b4c0)
```

`ib_hal_to_isa_engine_v{2,3,4}` (72 B each, `@0x2f6b30` / `@0x3aa190` / `@0x43f7c0`) maps the runtime's HAL engine enumeration onto the ISA's `NEURON_ISA_TPB` engine taxonomy; `instruction_engine_check` (`@0x299220`, 1189 B) then asserts the opcode is legal *on that engine* (a PE-array matmul on the SP engine is malformed regardless of field validity). Only after both pass does the per-record field validation run.

### Algorithm

```c
// Models ib_is_valid_engine_instruction_v2 @0x2f6b80 (153 B). One per arch (v2/v3/v4);
// the bodies differ only by which arch-clone of the validator forest they call.
function ib_is_valid_engine_instruction_v2(hal_eng, inst /* 64-byte record */):
    isa_eng = ib_hal_to_isa_engine_v2(hal_eng)        // 0x2f6b30: HAL→ISA engine id
    if !instruction_engine_check(isa_eng, inst):       // 0x299220: opcode∈engine's set?
        return false                                   //   wrong engine for this opcode
    return is_valid_neuron_instruction(inst)           // 0x2f2350: SILENT field gate (bool)
    // NOTE: the diagnostic twin is NOT called here. The caller (load pipeline) calls
    // debug_invalid_neuron_isa_instruction separately, only on a failure, to get the reason.

// Models is_valid_neuron_instruction @0x2f2350 (17952 B, 530 BB). The silent dispatcher:
// a giant switch on inst.raw.bytes[0], each arm an inlined is_valid_<opcode> predicate.
function is_valid_neuron_instruction(inst):
    if !has_valid_neuron_header(inst): return false    // 0x27da30-class: opcode enum + len==16
    if !has_valid_neuron_events(inst): return false    // event/sema wait+update tuple
    switch inst.raw.bytes[0]:                           // opcode discriminant
        case INDIRECT_COPY /*0xE7*/: return is_valid_indirect_copy(inst)   // 0x28ee00
        case S2_BN        /*100/109/112*/: return is_valid_s2_bn(inst)     // 0x28f280
        case D4_MR        /*77/73/75*/:    return is_valid_d4_mr(inst)      // 0x291ac0
        case REG_LOAD     /*0x4A*/:        return is_valid_reg_load(inst)   // 0x292480
        …                                                // ~70 real opcodes; HALT inlined here
    return false                                         // unknown opcode → reject

// Models debug_invalid_neuron_isa_instruction @0x2ecbb0 (339 B). The DEBUG twin dispatcher.
// switches.json: a 256-entry jump table keyed on the opcode byte (inst_addr 0x2ecc01).
function debug_invalid_neuron_isa_instruction(inst) -> NEURON_ISA_TPB_DEBUG_BOOL:
    switch inst.raw.bytes[0]:                            // ncases = 256
        case 0xF0: return dbg_is_valid_extended_inst(inst)        // → 0x2ecc03
        case 0xA1: return dbg_is_valid_halt(inst)                 // → 0x2ed1af region
        case 0xA6: return dbg_is_valid_notify(inst)
        case 0xB5: return dbg_is_valid_branch_prefetch_hint(inst)
        …                                                // 252 more cases
        default:   return neuron_isa_tpb_new_dbg_bool(false, "<no validator>")  // 0x27b440
```

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `ib_is_valid_engine_instruction_v2` | `0x2f6b80` | 153 B | arch-v2 entry: HAL→ISA engine, engine-check, silent gate | HIGH |
| `ib_is_valid_engine_instruction_v3` | `0x3aa1e0` | 2747 B | arch-v3 entry (inlines more leaves) | HIGH |
| `ib_is_valid_engine_instruction_v4` | `0x43f810` | 16087 B | arch-v4 entry (silent forest largely inlined) | HIGH |
| `ib_hal_to_isa_engine_v2` | `0x2f6b30` | 72 B | HAL engine id → ISA engine taxonomy | HIGH |
| `instruction_engine_check` | `0x299220` | 1189 B | opcode-legal-on-this-engine gate | HIGH |
| `is_valid_neuron_instruction` | `0x2f2350` | 17952 B | **silent** per-opcode dispatcher (`bool`) | HIGH |
| `is_valid_neuron_isa_instruction` | `0x3a60b0` | 16606 B | arch-clone of the silent dispatcher (v3 band) | HIGH |
| `debug_invalid_neuron_isa_instruction` | `0x2ecbb0` | 339 B | **debug** 256-case opcode jump table | HIGH |

> **QUIRK —** the debug dispatcher is named `debug_invalid_…`, not `debug_valid_…`. Its job is to explain *why* an instruction the silent gate already rejected is invalid. Calling it on a valid instruction returns a `DEBUG_BOOL` with `result=true` and an empty `err_msg`; the name encodes the intended call site (post-failure), not the predicate polarity. A reimplementer who only ever calls the debug forest will pay the full 1280-byte-copy cost on every record — the silent forest exists precisely to avoid that.

### Considerations

`is_valid_neuron_instruction` exists in two arch bands as `is_valid_neuron_instruction` (`@0x2f2350`, 530 BB) and `is_valid_neuron_isa_instruction` (`@0x3a60b0`, 499 BB), with a third copy folded into `ib_is_valid_engine_instruction_v4`. These are the v2/v3/v4 clones of one generated dispatcher; they decode the **same** 64-byte record but differ in the per-field legality constants for their silicon target (see [Per-Arch Validators](validators-per-arch.md)). The opcode→struct dispatch is identical across all three; only leaf legality values vary.

---

## 2. The `NEURON_ISA_TPB_DEBUG_BOOL` Return ABI

### Purpose

Every debug validator returns the same 1281-byte struct. Pinning its layout once lets a reimplementer decode any `dbg_*` function's result and understand why the debug forest is an order of magnitude larger than the silent one: the cost is the struct, not the logic.

### Encoding

```text
NEURON_ISA_TPB_DEBUG_BOOL  (DWARF ordinal 8729; size 1281 = 0x501)

byte: 0        1 ........................................................... 1280
     +--------+--------------------------------------------------------------+
     | result |                  err_msg[16][80]  (1280 B)                   |
     | bool   |   sixteen 80-char message slots; failures hold the asserts   |
     +--------+--------------------------------------------------------------+
       @+0                          @+1
```

| Offset | Width | Field | Meaning |
|---|---|---|---|
| `+0x000` | 1 | `result` (`bool`) | pass/fail; the only field the silent twin would compute |
| `+0x001` | 1280 | `err_msg` (`char[16][80]`) | 16 slots × 80 cols; on failure, slot(s) hold `"inst failed assertion check: '<name>'"` |

The struct is returned via hidden `__return_ptr` (a caller-allocated 1281-byte buffer passed as the first hidden argument). The body bloat in every `dbg_*` function is the inlined plumbing that maintains this buffer:

```c
// The recurring DEBUG_BOOL plumbing inlined into every dbg_is_valid_* body.
// This is why dbg_has_valid_neuron_events @0x280e40 is 2452 B vs the 190 B silent twin.
function dbg_check_pattern(retbuf /* __return_ptr, 1281 B */, inst):
    acc = neuron_isa_tpb_new_dbg_bool(true)            // 0x27b440: result=true, err_msg zeroed
    sub = neuron_isa_tpb_new_dbg_bool(predicate(inst)) //   one sub-result per check
    if !predicate(inst):
        // SIMD-assemble "inst failed assertion check: '<name>'" from the rodata template
        // pool (xmmword_850FA0.. region) into sub.err_msg via _mm_load_si128 stores
        fill_assertion_name(sub.err_msg, "<predicate_name>")
    neuron_isa_tpb_merge_dbg_bool(&acc, &sub)          // 0x27b4c0: acc.result &= sub.result;
                                                        //   first non-empty err_msg wins
    qmemcpy(retbuf, &acc, 0x500)                        // copy the 1280-byte matrix
    retbuf->err_msg[15][79] = acc.err_msg[15][79]       // patch the trailing 1281st byte
    return retbuf
```

`neuron_isa_tpb_new_dbg_bool` (`@0x27b440`, 119 B) is the constructor: it zero-initializes the 1280-byte matrix and sets `result`. `neuron_isa_tpb_merge_dbg_bool` (`@0x27b4c0`, 392 B) is the AND-combiner: `result` becomes the logical AND of two sub-results, and the merged `err_msg` carries the first failing message — so a validator that runs ten checks and merges them surfaces the *first* assertion that failed. Both have three arch clones (`_0` `@0x323e40`/`@0x323ec0`, `_1` `@0x3aad80`/`@0x3aae00`), one per silicon target, matching the three validator forests.

> **QUIRK —** the trailing byte `err_msg[15][79]` (offset `0x500`) is copied *separately* from the `qmemcpy(…, 0x500)` of the first 1280 bytes, because the compiler split the 1281-byte (odd-length, non-8-aligned) struct into a 1280-byte SIMD copy plus a 1-byte tail. IDA renders this as a `0x501`-length copy. It is an artifact of the struct size being `0x501` and is not semantically meaningful — a reimplementer copies all 1281 bytes as one unit.

### The err-message template pool

The failure strings are **not** stored as complete C literals per function. They are assembled at runtime from a packed, overlap-merged template pool in `.rodata` (the `xmmword_850FA0..` region, loaded via `_mm_load_si128`). The shared prefix `"inst failed assertion check: '"` is one pool entry; each predicate name (`'has_valid_inst_len'`, `'is_valid_enum'`, `'is_valid_addr8'`, `'tensor_scalar_valid_ops'`, `'is_opcode_halt'`, …) is spliced from adjacent, suffix-sharing fragments. Only two fully-pre-assembled strings survive as standalone literals in the binary — `'s4d3_mm_perf_opt_dtype'` (`@0x81f058`) and `'indirect_quadrant_check_srcmx_dst3d'` (`@0x81f0d0`) — the rest are constructed on the failure path. This is why the silent twins reference **zero** strings (they never assemble a message) and the debug twins reference the pool but own no complete literal.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `NEURON_ISA_TPB_DEBUG_BOOL` | DWARF ordinal 8729 | 1281 B | `{bool result; char err_msg[16][80]}` return type | CERTAIN |
| `neuron_isa_tpb_new_dbg_bool` | `0x27b440` | 119 B | constructor: zero matrix, set `result` | HIGH |
| `neuron_isa_tpb_merge_dbg_bool` | `0x27b4c0` | 392 B | AND-merge two results; keep first failing message | HIGH |
| `neuron_isa_tpb_new_dbg_bool_0` / `_1` | `0x323e40` / `0x3aad80` | 119 B | v3 / v4 arch clones of the constructor | HIGH |
| `neuron_isa_tpb_merge_dbg_bool_0` / `_1` | `0x323ec0` / `0x3aae00` | 392 B | v3 / v4 arch clones of the merge | HIGH |

---

## 3. A Representative Validator and its Twin

### Purpose

The whole layer is one shape repeated ~70 times per arch: a silent leaf and its debug twin, both built on a shared header/events preamble plus opcode-specific checks. Documenting one pair in full — the silent gate, the debug wrapper, the preamble — lets a reimplementer reconstruct every other validator by analogy. The clearest pair to anchor on is the *shared preamble* (`has_valid_neuron_header` + `has_valid_neuron_events`), because it gates ~91 of the per-opcode validators; the cleanest size-delta pair is `is_valid_dtype` / `dbg_is_valid_dtype`.

### Entry Point

```text
(silent)  is_valid_neuron_instruction (0x2f2350)
            └─ has_valid_neuron_header  (inlined; cf. silent has_valid_neuron_events 0x27da30)
            └─ has_valid_neuron_events  (0x27da30, 190 B)  ── 100 callers
            └─ is_valid_dtype           (0x27b910, 46 B, LEAF)  ── 112 callers
                 └─ is_valid_enum       (0x27b650, 691 B, LEAF) ── 210 callers (most-called)

(debug)   debug_invalid_neuron_isa_instruction (0x2ecbb0)
            └─ dbg_has_valid_neuron_header (0x280200, 699 B)  ── 91 callers
                 └─ dbg_is_valid_enum   (0x27eb50, 219 B)     ── 69 callers
            └─ dbg_has_valid_neuron_events (0x280e40, 2452 B) ── 91 callers
                 └─ dbg_is_valid_enum, is_update_semaphore (0x27b970), new_dbg_bool
            └─ dbg_is_valid_dtype       (0x27f940, 2233 B)    ── 153 callers
```

### Algorithm — the shared preamble

`has_valid_neuron_header` validates byte `[0]` (opcode) against the `OPCODE` enum list and requires `inst_word_len` (byte `[1]`) `== 16`; `has_valid_neuron_events` validates the inline synchronization tuple — `WAIT_MODE` (byte `[4]`) and `UPDATE_MODE` (byte `[6]`) enums, plus semaphore-update consistency.

```c
// Models dbg_has_valid_neuron_header @0x280200 (699 B, 91 callers). The silent twin
// (folded into is_valid_neuron_instruction) runs the identical two checks and returns bool.
function dbg_has_valid_neuron_header(inst) -> NEURON_ISA_TPB_DEBUG_BOOL:
    r = new_dbg_bool(true)
    // (1) opcode byte must be a defined NEURON_ISA_TPB_OPCODE enum value
    merge(&r, dbg_is_valid_enum(NEURON_ISA_TPB_ENUM_LIST_OPCODE, inst.raw.bytes[0]))  // 0x27eb50
    // (2) instruction length tag must be the canonical 16 (0x10)
    if inst.raw.bytes[1] != 16:                       // a3==16 path
        fill(&r, "'has_valid_inst_len'"); r.result = false
    return r

// Models has_valid_neuron_events @0x27da30 (190 B, silent) / dbg_ twin @0x280e40 (2452 B).
function has_valid_neuron_events(inst) -> bool:
    if !is_valid_enum(WAIT_MODE,   inst.raw.bytes[4]): return false   // pre-execute predicate
    if !is_valid_enum(UPDATE_MODE, inst.raw.bytes[6]): return false   // post-execute action
    if !is_update_semaphore_consistent(inst):          return false   // 0x27b970
    return true                                                        // 'valid_event_modes'
```

### Algorithm — a silent leaf and its twin

`is_valid_dtype` is the cleanest size-delta pair: 46 bytes silent versus 2233 bytes debug. Both check the *same* thing — a dtype byte is non-`INVALID`, `FP32R` only if the allow-flag is set, and is a defined `DTYPE` enum value.

```c
// Models is_valid_dtype @0x27b910 (46 B, LEAF). The fast-path gate.
function is_valid_dtype(dtype, allow_fp32r) -> bool:
    if dtype == 0:                       return false   // INVALID
    if dtype == 11 && !allow_fp32r:      return false   // FP32R gated
    return is_valid_enum(DTYPE, dtype)                   // 0x27b650: enum membership

// Models dbg_is_valid_dtype @0x27f940 (2233 B). The diagnostic twin — SAME logic, 48× larger.
function dbg_is_valid_dtype(dtype, allow_fp32r) -> NEURON_ISA_TPB_DEBUG_BOOL:
    r = new_dbg_bool(true)
    if dtype == 0:
        fill(&r, "'dtype_invalid_check'");      r.result = false; return r
    if dtype == 11 && !allow_fp32r:
        fill(&r, "'dtype_fp32r_illegal_check'"); r.result = false; return r
    merge(&r, dbg_is_valid_enum(DTYPE, dtype))           // 'is_valid_enum'
    return r
    // The 48× size ratio is entirely new_dbg_bool/merge/qmemcpy(0x500) plumbing.
    // dbg_is_valid_dtype_64 @0x328030 (1256 B) is the same with extra allow_u64/allow_i64 gates.
```

The legality oracle both forms reach is `is_valid_enum` (`@0x27b650`, 691 B, a true leaf, **210 callers** — the most-called function in the layer). It is a pure membership test over the `NEURON_ISA_TPB_ENUM_LIST_*` value sets, implemented as packed bitmask words per enum list. For the `OPCODE` list specifically, the four 64-bit bitmask constants (`0x400082F07FBFFFFF` / `0x024BC3FFFFDF3F7F` / `0x7FEFFFFFE08F7FFF` / `0x1001E0000003E`) encode which of the 256 opcode values are defined; these are documented alongside the [record format](instruction-record.md#6-instruction-block-io-ib_final_inst_validity_check).

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `has_valid_neuron_events` | `0x27da30` | 190 B | silent: WAIT/UPDATE enum + sema consistency (100 callers) | HIGH |
| `dbg_has_valid_neuron_header` | `0x280200` | 699 B | debug: opcode enum + `inst_word_len==16` (91 callers) | HIGH |
| `dbg_has_valid_neuron_events` | `0x280e40` | 2452 B | debug twin of events check (91 callers) | HIGH |
| `is_valid_dtype` | `0x27b910` | 46 B | silent dtype leaf (112 callers) | HIGH |
| `dbg_is_valid_dtype` | `0x27f940` | 2233 B | debug dtype twin (153 callers) | HIGH |
| `is_valid_enum` | `0x27b650` | 691 B | enum-membership oracle, leaf (210 callers) | HIGH |
| `is_update_semaphore` | `0x27b970` | 57 B | sema-update-mode predicate, leaf | HIGH |
| `tensor_scalar_valid_ops` / `dbg_` twin | `0x27d3c0` (231 B) / `0x2804c0` (612 B) | — | ALU op0/op1 legality, silent vs debug | HIGH |
| `dbg_is_valid_branch_prefetch_hint` | `0x328e20` | 791 B | sole caller = debug dispatcher; full enum gauntlet | HIGH |

> **QUIRK —** the validators are AUTO-GENERATED from `aws_neuron_isa_tpb_assert.h` (DWARF `addr2line` resolves the silent/debug clones to `neuron_<arch>_arch_isa/tpb/aws_neuron_isa_tpb_assert.h`; the load-path TU is `instruction_block_cayman.c`, confirmed at `@0x81f090`). One ISA-spec header drives **three** products per opcode: the silent `is_valid_<op>` predicate, the `dbg_is_valid_<op>` twin, and the per-arch clone set (v2/v3/v4 bands at `0x28xxxx` / `0x32xxxx` / `0x3bxxxx`). A reimplementer should treat these as machine-generated from a single field/enum/reserved-bit specification per opcode, not as independent hand-written checkers — the silent and debug forms are the *same* predicate compiled with and without the `DEBUG_BOOL` message machinery, and the assertion names (`'has_valid_inst_len'`, `'is_valid_addr8'`, `'tensor_scalar_valid_ops'`, …) are the generator's predicate identifiers verbatim.

### Considerations

The silent and debug forests are kept byte-for-byte consistent *because* they are generated from one source — there is no risk of the fast path accepting what the diagnostic path would reject, since both compile the same predicate tree. The only behavioral difference is observability: the silent gate yields a single bit, the debug twin yields that bit plus the name of the first failing assertion. For a reimplementation that does not need failure diagnostics, the entire `dbg_*` forest and the `NEURON_ISA_TPB_DEBUG_BOOL` machinery can be omitted — the silent forest alone is a complete, sound validator.

---

## Related Components

| Name | Relationship |
|---|---|
| `is_valid_neuron_instruction` / `debug_invalid_neuron_isa_instruction` | the silent gate and debug dispatcher this page documents |
| `NEURON_ISA_TPB_DEBUG_BOOL` | the 1281-byte return type of every debug validator |
| `has_valid_neuron_header` / `has_valid_neuron_events` | the shared preamble gating ~91 per-opcode validators |
| `is_valid_enum` / `is_valid_dtype` / `is_update_semaphore` | the shared leaf legality predicates both families reach |
| `aws_neuron_isa_tpb_assert.h` | the generated-header origin of the entire validator forest |

## Cross-References

- [The 64-Byte Instruction Record Format](instruction-record.md) — the `NEURON_ISA_TPB_INST_UNION` this layer validates; opcode→struct dispatch and the `ib_*` stream I/O that calls `ib_final_inst_validity_check`
- [Per-Arch Instruction Validators (SUNDA / CAYMAN / MARIANA Deltas)](validators-per-arch.md) — the three emitted validator clones (v2/v3/v4 bands) and the per-arch legality and address-aperture deltas
- [Overview: the TPB Engine Instruction Model](overview.md) — where instruction validation sits in the engine/load pipeline
- [The Load Pipeline (Parse → Build → Stage → Relocate)](../neff/load-pipeline.md) — the model-load stage that runs `ib_is_valid_engine_instruction` over each record before staging it into device HBM
