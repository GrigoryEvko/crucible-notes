# NVVMPassOptions

NVVMPassOptions is NVIDIA's proprietary per-pass configuration system -- a 4,512-byte flat struct containing 221 option slots that controls every aspect of the NVVM optimization pipeline. It has no upstream LLVM equivalent. Where LLVM uses scattered `cl::opt<T>` globals that each pass reads independently, NVIDIA consolidates all pass configuration into a single contiguous struct that is allocated once and threaded through the entire pipeline assembler as a parameter. This design allows the pipeline to make pass-enable decisions through simple byte reads at known offsets rather than hash-table lookups, and it ensures that the complete configuration state can be copied between Phase I and Phase II of the two-phase compilation model.

The struct is populated by a single 125KB function (`sub_12D6300`) that reads from a `PassOptionRegistry` hash table and flattens the results into 221 typed slots. The pipeline assembler (`sub_12E54A0`) and its sub-pipeline builders (`sub_12DE330`, `sub_12DE8F0`) then read individual slots by offset to decide which passes to insert and how to configure them.

| | |
|---|---|
| **Initializer** | `sub_12D6300` (125KB, 4,786 lines) |
| **Struct size** | 4,512 bytes (`sub_22077B0(4512)`) |
| **Slot count** | 221 (1-based index: 1--221) |
| **Slot types** | 5: STRING (24B), BOOL_COMPACT (16B), BOOL_INLINE (16B), INTEGER (16B), STRING_PTR (28B) |
| **Type breakdown** | 114 string + 83 bool compact + 17 bool inline + 6 integer + 1 string pointer |
| **Registry lookup** | `sub_12D6170` (hash table at registry+120) |
| **PassDef resolver** | `sub_1691920` (64-byte stride table) |
| **Bool parser** | `sub_12D6240` (triple: lookup + lowercase + char test) |
| **Callers** | `sub_12E7E70` (Phase orchestrator), `sub_12F4060` (TargetMachine creation) |
| **Consumers** | `sub_12E54A0`, `sub_12DE330`, `sub_12DE8F0`, `sub_12DFE00` |

## Struct Layout

The struct is heap-allocated as a single 4,512-byte block. The first 16 bytes contain header fields, followed by 221 option slots packed contiguously, and a 32-byte zero trailer:

```
Offset  Size   Field
──────  ────   ─────
0       4      int opt_level (copied from registry+112)
4       4      (padding)
8       8      qword ptr to PassOptionRegistry
16      ~4464  221 option slots (variable-size, packed)
4480    32     zero trailer (4 qwords, sentinel)
```

Slot offsets are deterministic -- they depend on the type sequence hard-coded into `sub_12D6300`. String slots consume 24 bytes, boolean and integer slots consume 16 bytes, and the unique string-pointer slot at index 181 consumes 28 bytes. The initializer writes each slot at a compile-time-constant offset; there is no dynamic layout calculation.

## Slot Types

### Type A: String Option (24 bytes) -- `sub_12D6090`

114 slots. Stores a string value (pass name or parametric value) along with flags, optimization level, and pass ID.

```c
struct StringOption {       // 24 bytes, written by sub_12D6090
    char*    value;         // +0:  pointer to string data
    int32_t  option_index;  // +8:  1-based slot index
    int32_t  flags;         // +12: from PassDef byte 40
    int32_t  opt_level;     // +16: from header opt_level
    int32_t  pass_id;       // +20: resolved via sub_1691920
};
```

### Type B: Boolean Compact (16 bytes) -- `sub_12D6100`

83 slots. The most common boolean representation. The helper encapsulates the lookup-parse-resolve sequence.

```c
struct BoolCompactOption {  // 16 bytes, written by sub_12D6100
    uint8_t  value;         // +0:  0 or 1
    uint8_t  pad[3];        // +1:  padding
    int32_t  option_index;  // +4:  1-based slot index
    int32_t  flags;         // +8:  from PassDef byte 40
    int32_t  pass_id;       // +12: resolved via sub_1691920
};
```

### Type C: Boolean Inline (16 bytes) -- direct write

17 slots. Identical layout to Type B, but written directly by `sub_12D6300` rather than through the `sub_12D6100` helper. These correspond to option pairs where the boolean resolution requires checking `PassDef+36` (has_overrides byte) and resolving via `sub_1691920` inline. The 17 inline boolean slots are: 7, 11, 13, 49, 53, 55, 59, 61, 95, 103, 119, 127, 151, 159, 169, 177, 211.

```c
struct BoolInlineOption {   // 16 bytes, same layout as Type B
    uint8_t  value;         // +0:  0 or 1
    uint8_t  pad[3];        // +1
    int32_t  option_index;  // +4:  high 32 bits of sub_12D6240 return
    int32_t  opt_level;     // +8:  from header
    int32_t  pass_id;       // +12: resolved inline
};
```

### Type D: Integer (16 bytes) -- direct write via `sub_16D2BB0`

6 slots. The integer value is parsed from the registry string by `sub_16D2BB0` (string-to-int64). Layout is identical to boolean compact but the first 4 bytes store a full `int32_t` rather than a single byte.

```c
struct IntegerOption {      // 16 bytes
    int32_t  value;         // +0:  parsed integer
    int32_t  option_index;  // +4:  1-based slot index
    int32_t  opt_level;     // +8
    int32_t  pass_id;       // +12
};
```

### Type E: String Pointer (28 bytes) -- slot 181 only

Unique. Stores a raw `char*` plus length rather than a managed string. Likely a file path or regex pattern that requires direct C-string access.

```c
struct StringPtrOption {    // 28 bytes, slot 181 only
    char*    data;          // +0:  raw char pointer
    uint64_t length;        // +8:  string length
    int32_t  option_index;  // +16: 1-based slot index
    int32_t  opt_level;     // +20
    int32_t  pass_id;       // +24
};
```

## Pair Organization Pattern

The 221 slots follow a predominantly paired layout. Slots 1--6 are six standalone STRING options (likely the global compilation parameters: ftz, prec-div, prec-sqrt, fmad, opt-level, sm-arch). Starting at slot 7, slots are organized in `(EVEN, ODD)` pairs:

- **Even slot N**: STRING option -- the pass's parameter value or name
- **Odd slot N+1**: BOOLEAN or INTEGER option -- the enable/disable toggle

Each "pass knob" thus gets a string parameter slot and a boolean gate. The pipeline assembler reads the boolean to decide whether to insert the pass, and passes the string value as the pass's configuration parameter.

Exceptions to the pair pattern:

| Region | Anomaly |
|---|---|
| Slots 160--162 | Three consecutive STRING slots with a single boolean at 163 |
| Slots 191--193 | Slot 191 STRING, then two consecutive booleans at 192--193 |
| Slot 181 | STRING_PTR type instead of normal STRING |
| Slots 196--207 | Alternating STRING + INTEGER instead of STRING + BOOL |

## Helper Functions

### `sub_12D6170` -- `PassOptionRegistry::lookupOption`

Looks up an option by its 1-based slot index in the hash table at `registry+120`. Returns a pointer to an `OptionNode` or 0 if the option was not set from the command line:

```c
// Signature: int64 sub_12D6170(void* registry, int option_index)
// Returns: OptionNode* or 0
//
// OptionNode layout:
//   +40   int16   flags
//   +48   char**  value_array_ptr (array of string values)
//   +56   int     value_count
```

The hash table uses open addressing. The lookup computes `hash(option_index)` and probes linearly. When an option is not present in the registry (meaning the user did not supply a CLI override), the caller falls back to the hard-coded default in `sub_12D6300`.

### `sub_12D6240` -- `PassOptionRegistry::getBoolOption`

Resolves a boolean option with a default value. This is the critical function for all 100 boolean slots -- it performs a three-step resolution:

```
sub_12D6240(registry, option_index, default_string):
    1. Call sub_12D6170(registry, option_index)
    2. If found AND has value:
         lowercase the string via sub_16D2060
         result = (first_char == '1' || first_char == 't')  // "1" or "true"
    3. If not found OR no value:
         result = (default_string[0] == '1')  // "0" -> false, "1" -> true
    Return: packed(bool_value:8, flags:32) in low 40 bits
```

The packing convention is significant: the boolean value occupies the low 8 bits and the flags occupy bits 8--39. Callers unpack with `(result & 0xFF)` for the boolean and `(result >> 8)` for the flags.

### `sub_1691920` -- `PassDefTable::getPassDef`

Resolves a 1-based pass index to its PassDef entry in a table with 64-byte stride:

```c
// sub_1691920(table_ptr, pass_index):
//   return table_ptr[0] + (pass_index - 1) * 64
//
// PassDef layout (64 bytes):
//   +32   int     pass_id
//   +36   byte    has_overrides
//   +40   int16   override_index
```

The pass_id field is written into every option slot and later used by the pipeline assembler to map configuration back to the pass factory that should receive it.

### `sub_16D2BB0` -- parseInt

Parses a string to a 64-bit integer. Used for the 6 integer-typed option slots (9, 197, 203, 205, 207, 215).

## Default Values

Most boolean slots default to `0` (disabled). 14 slots default to `1` (enabled) -- these represent passes that run by default and must be explicitly disabled:

> **Confidence note:** Pass associations marked `[MEDIUM]` are inferred from pipeline guard cross-references (`a4[offset]`). Associations marked `[LOW]` are based solely on offset proximity or default-value patterns.

| Slot | Offset | Likely Pass | Confidence |
|---|---|---|---|
| 19 | 400 | Inliner (AlwaysInliner gate) | MEDIUM |
| 25 | 520 | NVIDIA-specific pass A | LOW |
| 93 | 1880 | ConstantMerge | HIGH |
| 95 | 1920 | NVVMIntrinsicLowering | HIGH |
| 117 | 2360 | NVVMUnreachableBlockElim | HIGH |
| 141 | 2840 | ADCE | HIGH |
| 143 | 2880 | LICM | HIGH |
| 151 | 3040 | CorrelatedValuePropagation | MEDIUM |
| 155 | 3120 | MemorySpaceOpt (second pass) | MEDIUM |
| 157 | 3160 | PrintModulePass (dump mode) | HIGH |
| 159 | 3200 | Optimization-level gating | MEDIUM |
| 165 | 3328 | Late-pipeline enable block | LOW |
| 211 | 4264 | (inline bool, late pass) | LOW |
| 219 | 4424 | (compact bool, late pass) | LOW |

Integer slot defaults:

| Slot | Offset | Default | Likely Meaning |
|---|---|---|---|
| 9 | 200 | 1 | Optimization threshold / iteration count |
| 197 | 3984 | 20 | Limit/threshold (e.g., unroll count) |
| 203 | 4104 | -1 | Thread count (sentinel for auto-detect via `get_nprocs()`) |
| 205 | 4144 | -1 | Thread count fallback |
| 207 | 4184 | -1 | Sentinel for unlimited/auto |
| 215 | 4344 | 0 | Disabled counter |

## CLI Flag Routing

The path from a user-visible flag to an NVVMPassOptions slot traverses four stages:

```
nvcc -Xcicc -opt "-do-licm=0"          ← user invocation
    │
    ▼
sub_9624D0 (flag catalog, 75KB)        ← parses -opt flags into opt_argv vector
    │   pushes "-do-licm=0" into v327 (opt vector)
    ▼
PassOptionRegistry (hash table)         ← opt-phase parser populates registry
    │   key = slot_index, value = "0"
    ▼
sub_12D6300 (125KB initializer)         ← flattens registry into 4512-byte struct
    │   sub_12D6240(registry, LICM_SLOT, "1") → returns 0 (overridden)
    │   writes opts[2880] = 0
    ▼
sub_12E54A0 / sub_12DE8F0              ← pipeline assembler reads opts[2880]
    if (opts[2880]) AddPass(LICM);     ← skipped because opts[2880] == 0
```

The `-opt` flag prefix is critical: it routes the argument to the optimizer phase vector rather than to the linker, LTO, or codegen phases. The flag catalog (`sub_9624D0`) recognizes several shorthand patterns:

| User Flag | Routes To | Effect |
|---|---|---|
| `--emit-optix-ir` | opt `"-do-ip-msp=0"`, opt `"-do-licm=0"` | Disables IPMSP and LICM for OptiX |
| `-Ofast-compile=max` | opt `"-fast-compile=max"`, opt `"-memory-space-opt=0"` | Disables MemorySpaceOpt |
| `-memory-space-opt=0` | opt `"-memory-space-opt=0"` | Direct pass disable |
| `-Xopt "-do-remat=0"` | opt `"-do-remat=0"` | Direct pass-through to opt phase |

## Pipeline Consumer: How Passes Read NVVMPassOptions

The pipeline assembler and its sub-pipeline builders receive the NVVMPassOptions struct as parameter `a4` (in `sub_12E54A0`) or `opts` (in `sub_12DE330`/`sub_12DE8F0`). They read individual boolean slots by dereferencing a byte at a known offset and branching:

```c
// Pattern 1: simple disable guard
if (!*(uint8_t*)(opts + 1760))           // opts[1760] = MemorySpaceOpt disable
    AddPass(PM, sub_1C8E680(0), 1, 0);  // insert MemorySpaceOpt

// Pattern 2: enable guard (inverted logic)
if (*(uint8_t*)(opts + 2880))            // opts[2880] = LICM enabled (default=1)
    AddPass(PM, sub_195E880(0), 1, 0);  // insert LICM

// Pattern 3: combined guard with opt-level gating
if (*(uint8_t*)(opts + 3200) &&          // opts[3200] = opt-level sufficient
    !*(uint8_t*)(opts + 880))            // opts[880] = NVVMReflect not disabled
    AddPass(PM, sub_1857160(), 1, 0);   // insert NVVMReflect

// Pattern 4: integer parameter read
v12 = *(int32_t*)(opts + 200);           // opts[200] = opt threshold (default=1)
// used to configure codegen dispatch in sub_12DFE00
```

The key insight is that the pipeline assembler never performs string comparison or hash-table lookup at pass-insertion time -- it reads pre-resolved values from the flat struct. This makes the ~150 pass-insertion decisions in `sub_12E54A0` essentially free in terms of runtime cost.

## Offset-to-Pass Mapping

The following table maps struct offsets (as seen in pipeline assembler guards `opts[OFFSET]`) to the passes they control. Offsets are byte offsets from the struct base. "Guard sense" indicates whether the pass runs when the byte is 0 (`!opts[X]` -- most common, where the option is a disable flag) or when it is nonzero (`opts[X]` -- the option is an enable flag).

| Offset | Slot | Guard Sense | Controlled Pass | Factory |
|---|---|---|---|---|
| 200 | 9 | value | Optimization threshold (integer, read by `sub_12DFE00`) | -- |
| 280 | 14-15 | `!opts` | DCE (DeadCodeElimination) | `sub_18DEFF0` |
| 320 | 16-17 | `!opts` | TailCallElim / JumpThreading | `sub_1833EB0` |
| 360 | 18-19 | `!opts` | NVVMLateOpt | `sub_1C46000` |
| 400 | 20-21 | `!opts` | AlwaysInliner gate A | `sub_1C4B6F0` |
| 440 | 22-23 | `!opts` | AlwaysInliner gate B | `sub_1C4B6F0` |
| 480 | 24-25 | `!opts` | Inliner gate C | `sub_1C4B6F0` |
| 520 | 26-27 | `!opts` | NVIDIA-specific pass A | `sub_1AAC510` |
| 560 | 28-29 | `!opts` | NVIDIA-specific pass B | `sub_1AAC510` |
| 600 | 30-31 | `!opts` | NVVMVerifier | `sub_12D4560` |
| 680 | 34-35 | `!opts` | FunctionAttrs | `sub_1841180` |
| 720 | 36-37 | `!opts` | SCCP | `sub_1842BC0` |
| 760 | 38-39 | `!opts` | DSE (DeadStoreElimination) | `sub_18F5480` |
| 880 | 44-45 | `!opts` | NVVMReflect | `sub_1857160` |
| 920 | 46-47 | `!opts` | IPConstantPropagation | `sub_185D600` |
| 960 | 48-49 | `!opts` | SimplifyCFG | `sub_190BB10` |
| 1000 | 50-51 | `!opts` | InstCombine | `sub_19401A0` |
| 1040 | 52-53 | `!opts` | Sink / SimplifyCFG (early) | `sub_1869C50` |
| 1080 | 54-55 | `!opts` | PrintModulePass (dump IR) | `sub_17060B0` |
| 1120 | 56-57 | `!opts` | NVVMPredicateOpt | `sub_18A3430` |
| 1160 | 58-59 | `!opts` | LoopIndexSplit | `sub_1952F90` |
| 1200 | 60-61 | `!opts` | SimplifyCFG (tier guard) | `sub_190BB10` |
| 1240 | 62-63 | `!opts` | LICM | `sub_195E880` |
| 1280 | 64-65 | `!opts` | Reassociate / Sinking | `sub_1B7FDF0` |
| 1320 | 66-67 | `!opts` | ADCE (AggressiveDeadCodeElimination) | `sub_1C76260` |
| 1360 | 68-69 | `!opts` | LoopUnroll | `sub_19C1680` |
| 1400 | 70-71 | `!opts` | SROA | `sub_1968390` |
| 1440 | 72-73 | `!opts` | EarlyCSE | `sub_196A2B0` |
| 1480 | 74-75 | `!opts` | ADCE extra guard | `sub_1C76260` |
| 1520 | 76-77 | `!opts` | LoopSimplify | `sub_198DF00` |
| 1640 | 82-83 | `!opts` | NVVMWarpShuffle | `sub_1C7F370` |
| 1680 | 84-85 | `!opts` | NVIDIA pass (early) | `sub_19CE990` |
| 1760 | 88-89 | `!opts` | MemorySpaceOpt (primary) | `sub_1C8E680` |
| 1840 | 92-93 | `!opts` | ADCE variant | `sub_1C6FCA0` |
| 1960 | 98-99 | `!opts` | ConstantMerge / GlobalDCE | `sub_184CD60` |
| 2000 | 100-101 | `!opts` | NVVMIntrinsicLowering | `sub_1CB4E40` |
| 2040 | 102-103 | `!opts` | MemCpyOpt | `sub_1B26330` |
| 2080 | 104-105 | `!opts` | BranchDist gate A | `sub_1CB73C0` |
| 2120 | 106-107 | `!opts` | BranchDist gate B | `sub_1CB73C0` |
| 2160 | 108-109 | `!opts` | NVVMPredicateOpt variant | `sub_18A3090` |
| 2200 | 110-111 | `!opts` | GenericToNVVM | `sub_1A02540` |
| 2240 | 112-113 | `!opts` | NVVMLowerAlloca gate A | `sub_1CBC480` |
| 2280 | 114-115 | `!opts` | NVVMLowerAlloca gate B | `sub_1CBC480` |
| 2320 | 116-117 | `!opts` | NVVMRematerialization | `sub_1A13320` |
| 2360 | 118-119 | `!opts` | NVVMUnreachableBlockElim | `sub_1CC3990` |
| 2400 | 120-121 | `!opts` | NVVMReduction | `sub_1CC5E00` |
| 2440 | 122-123 | `!opts` | NVVMSinking2 | `sub_1CC60B0` |
| 2560 | 128-129 | `!opts` | NVVMGenericAddrOpt | `sub_1CC71E0` |
| 2600 | 130-131 | `!opts` | NVVMIRVerification | `sub_1A223D0` |
| 2640 | 132-133 | `!opts` | LoopOpt / BarrierOpt | `sub_18B1DE0` |
| 2680 | 134-135 | `!opts` | MemorySpaceOpt (second invocation) | `sub_1C8E680` |
| 2720 | 136-137 | `!opts` | InstructionSimplify | `sub_1A7A9F0` |
| 2760 | 138-139 | `!opts` | LoopUnswitch variant | `sub_19B73C0` |
| 2840 | 141 | `opts` | ADCE (enabled by default, slot 141, default=1) | `sub_1C6FCA0` |
| 2880 | 143 | `opts` | LICM (enabled by default, slot 143, default=1) | `sub_195E880` |
| 2920 | 145 | value | LowerBarriers parameter | `sub_1C98270` |
| 3000 | 150-151 | `opts` | Early pass guard | `sub_18FD350` |
| 3040 | 151 | `opts` | CorrelatedValuePropagation (default=1) | `sub_18EEA90` |
| 3080 | 153 | `opts` | NVIDIA-specific loop pass | `sub_1922F90` |
| 3120 | 155 | `opts` | MemorySpaceOpt second-pass enable (default=1) | `sub_1C8E680` |
| 3160 | 157 | `opts` | PrintModulePass enable (default=1) | `sub_17060B0` |
| 3200 | 159 | `opts` | Optimization-level gate (default=1) | -- |
| 3328 | 165 | `opts` | Late-pipeline enable block (default=1) | multiple |
| 3488 | 174-175 | `opts` | NVVMBarrierAnalysis + LowerBarriers enable | `sub_18E4A00` |
| 3648 | 181 | string | Language string (`"ptx"`/`"mid"`) | path dispatch |
| 3704 | 185 | `opts` | Late optimization flag | `sub_1C8A4D0` |
| 3904 | 193 | `opts` | Debug / verification mode | `sub_12D3E60` |
| 3944 | 195 | `opts` | Basic block naming (`"F%d_B%d"`) | sprintf |
| 3984 | 197 | value | Integer limit (default=20) | -- |
| 4064 | 201 | value | Concurrent compilation override | `sub_12D4250` |
| 4104 | 203 | value | Thread count (default=-1, auto-detect) | `sub_12E7E70` |
| 4144 | 205 | value | Thread count fallback (default=-1) | `sub_12E7E70` |
| 4184 | 207 | value | Integer parameter (default=-1) | -- |
| 4224 | 209 | `opts` | Optimization enabled flag | tier dispatch |
| 4304 | 213 | `opts` | Device-code flag | Pipeline B |
| 4344 | 215 | value | Integer counter (default=0) | -- |
| 4384 | 217 | `opts` | Fast-compile bypass flag | Pipeline B dispatch |
| 4464 | 221 | `!opts` | Late CFG cleanup guard | `sub_1654860` |

## Known Option Names

Option names are stored in the `PassOptionRegistry` hash table, not in `sub_12D6300` itself. The following names are extracted from binary string references in global constructors and pass factories:

### Boolean Toggles (do-X / no-X)

| Name | Likely Slot Region | Default |
|---|---|---|
| `do-ip-msp` | MemorySpaceOpt area | enabled |
| `do-clone-for-ip-msp` | MemorySpaceOpt variant | -- |
| `do-licm` | offset 2880 (slot 143) | 1 (enabled) |
| `do-remat` | offset 2320 (slot 117) | enabled |
| `do-cssa` | CSSA pass area | -- |
| `do-scev-cgp` | SCEV-CGP area | -- |
| `do-function-scev-cgp` | function-level SCEV-CGP | -- |
| `do-scev-cgp-aggresively` [sic] | aggressive SCEV-CGP mode | -- |
| `do-base-address-strength-reduce` | BaseAddrSR area | -- |
| `do-base-address-strength-reduce-chain` | BaseAddrSR chain variant | -- |
| `do-comdat-renaming` | COMDAT pass | -- |
| `do-counter-promotion` | PGO counter promotion | -- |
| `do-lsr-64-bit` | 64-bit loop strength reduction | -- |
| `do-sign-ext-expand` | sign extension expansion | -- |
| `do-sign-ext-simplify` | sign extension simplification | -- |

### Dump/Debug Toggles

| Name | Purpose |
|---|---|
| `dump-ip-msp` | Dump IR around MemorySpaceOpt |
| `dump-ir-before-memory-space-opt` | IR dump pre-MSP |
| `dump-ir-after-memory-space-opt` | IR dump post-MSP |
| `dump-memory-space-warnings` | MSP diagnostic warnings |
| `dump-remat` / `dump-remat-add` / `dump-remat-iv` / `dump-remat-load` | Rematerialization diagnostics |
| `dump-branch-dist` | Branch distribution diagnostics |
| `dump-scev-cgp` | SCEV-CGP diagnostics |
| `dump-base-address-strength-reduce` | BaseAddrSR diagnostics |
| `dump-sink2` | Sinking2 diagnostics |
| `dump-before-cssa` | CSSA input dump |
| `dump-phi-remove` | PHI removal diagnostics |
| `dump-normalize-gep` | GEP normalization dump |
| `dump-simplify-live-out` | Live-out simplification dump |
| `dump-process-restrict` | Process-restrict dump |
| `dump-process-builtin-assume` | Builtin assume processing dump |
| `dump-conv-dot` / `dump-conv-func` / `dump-conv-text` | Convergence analysis dumps |
| `dump-nvvmir` | NVVM IR dump |
| `dump-va` | Value analysis dump |

### Parametric Knobs

| Name | Default | Purpose |
|---|---|---|
| `remat-for-occ` | 120 | Occupancy target for rematerialization |
| `remat-gep-cost` | 6000 | GEP rematerialization cost threshold |
| `remat-lli-factor` | 10 | Long-latency instruction factor |
| `remat-max-live-limit` | 10 | Maximum live range limit for remat |
| `remat-single-cost-limit` | -- | Single-instruction remat cost limit |
| `remat-loop-trip` | -- | Loop trip count for remat decisions |
| `remat-use-limit` | -- | Use count limit for remat candidates |
| `remat-maxreg-ceiling` | -- | Register ceiling for remat |
| `remat-move` | -- | Remat move control |
| `remat-load-param` | -- | Parameter load remat control |
| `remat-ignore-single-cost` | -- | Ignore single-cost heuristic |
| `branch-dist-block-limit` | -1 | Max blocks for branch distribution (-1 = unlimited) |
| `branch-dist-func-limit` | -1 | Max functions for branch distribution |
| `branch-dist-norm` | 0 | Branch distribution normalization mode |
| `scev-cgp-control` | -- | SCEV-CGP mode selector |
| `scev-cgp-norm` | -- | SCEV-CGP normalization |
| `scev-cgp-check-latency` | -- | Latency check threshold |
| `scev-cgp-cross-block-limit` | -- | Cross-block limit |
| `scev-cgp-idom-level-limit` | -- | Immediate dominator level limit |
| `scev-cgp-inst-limit` | -- | Instruction count limit |
| `scev-cgp-old-base` | -- | Old base address mode |
| `scev-cgp-tid-max-value` | -- | Thread ID max value |
| `base-address-strength-reduce-iv-limit` | -- | IV limit for base addr SR |
| `base-address-strength-reduce-max-iv` | -- | Max IV count |
| `cssa-coalesce` | -- | CSSA coalescing mode |
| `cssa-verbosity` | -- | CSSA diagnostic verbosity |
| `memory-space-opt-pass` | -- | MSP pass variant selector |
| `peephole-opt` | -- | Peephole optimizer control |
| `loop-index-split` | -- | Loop index split control |
| `va-use-scdg` | -- | Value analysis SCDG mode |
| `nvvm-peephole-optimizer` | -- | NVVM peephole enable |
| `nvvm-intr-range` | -- | Intrinsic range analysis control |

## Differences from Upstream LLVM

Upstream LLVM has nothing resembling this system. The closest analogue is the `cl::opt<T>` flag mechanism, but that scatters configuration across hundreds of global variables that each pass reads independently. The differences are architectural:

| Aspect | Upstream LLVM | cicc NVVMPassOptions |
|---|---|---|
| Storage | ~1,689 scattered `cl::opt` globals in BSS | Single 4,512-byte contiguous struct |
| Initialization | Global constructors register each flag | One 125KB function flattens all 221 slots |
| Access pattern | Each pass reads its own globals | Pipeline assembler reads all slots centrally |
| Copyability | Not designed for copying | Struct is trivially `memcpy`-able for Phase I/II |
| Thread safety | Global `cl::opt` requires careful coordination | Each thread gets its own struct copy |
| Override mechanism | `cl::opt` command-line parser | `PassOptionRegistry` hash table with fallback defaults |
| Pass gating | Pass decides internally whether to run | Pipeline assembler decides before constructing pass |

The thread-safety property is crucial for the two-phase concurrent compilation model. When Phase II runs per-function compilation in parallel threads, each thread receives a copy of the NVVMPassOptions struct. If NVIDIA used upstream `cl::opt` globals for pass configuration, they would need global locks or TLS for every option read during pass execution -- an unacceptable overhead for a GPU compiler that may process hundreds of kernels in a single translation unit.

## Interaction with Two-Phase Compilation

The NVVMPassOptions struct is allocated and populated before Phase I begins, in the orchestrator `sub_12E7E70`:

```c
// sub_12E7E70, line ~128
void* opts = malloc(4512);              // allocate NVVMPassOptions
sub_12D6300(opts, registry);            // populate from CLI-parsed registry
// ... pass opts to sub_12E54A0 for Phase I ...
// ... pass same opts to sub_12E54A0 for Phase II ...
```

Both phases receive the same `opts` pointer. Individual passes within the pipeline assembler check `qword_4FBB3B0` (the TLS phase counter) to skip themselves in the wrong phase -- but the NVVMPassOptions struct itself does not change between phases. This means a pass cannot be enabled in Phase I but disabled in Phase II through NVVMPassOptions alone; phase selection is handled by the separate TLS mechanism.

The second caller, `sub_12F4060` (TargetMachine creation in the standalone path), performs an identical allocation and initialization sequence, confirming that every compilation path goes through the same NVVMPassOptions infrastructure.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `NVVMPassOptions::init` | `sub_12D6300` | 125KB | Populate 221 slots from registry |
| `PassOptionRegistry::lookupOption` | `sub_12D6170` | ~200B | Hash-table lookup by slot index |
| `PassOptionRegistry::getBoolOption` | `sub_12D6240` | ~300B | Boolean resolution with default |
| `writeStringOption` | `sub_12D6090` | ~150B | Write 24-byte string slot |
| `writeBoolOption` | `sub_12D6100` | ~120B | Write 16-byte boolean slot |
| `PassDefTable::getPassDef` | `sub_1691920` | ~80B | 64-byte stride table lookup |
| `parseInt` | `sub_16D2BB0` | ~100B | String-to-int64 parser |
| `toLowercase` | `sub_16D2060` | ~80B | String lowercasing for bool parse |

## Cross-References

- [LLVM Optimizer](../pipeline/optimizer.md) -- pipeline assembler that consumes NVVMPassOptions
- [Configuration Knobs](./knobs.md) -- all three knob systems (cl::opt, NVVMPassOptions, codegen)
- [CLI Flags](./cli-flags.md) -- flag catalog and routing to opt phase vector
- [Optimization Levels](./optimization-levels.md) -- O-level encoding and fast-compile modes
- [Concurrent Compilation](../infra/concurrent-compilation.md) -- Phase I/II threading model
- [Entry Point & CLI](../pipeline/entry.md) -- wizard mode and -opt flag dispatching
- [OptiX IR](../pipeline/optix-ir.md) -- forces `do-ip-msp=0` and `do-licm=0`
