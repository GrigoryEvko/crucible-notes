# ThinLTO Function Import

CICC v13.0 implements LLVM's ThinLTO function import pipeline with GPU-specific modifications to the threshold computation, candidate filtering, and provenance tracking. The core of the system lives in two functions -- `sub_1854A20` (the import driver, 4,326 bytes) and `sub_1853180` (the threshold computation engine, 5,059 bytes) -- with an entry point at `sub_1855B10` that parses the `-summary-file` / `-function-import` command line and orchestrates the whole-module import flow. The fundamental difference from CPU ThinLTO is that GPU compilation operates in a closed-world model: there are no shared libraries, no dynamic linking, and no PLT/GOT indirection. Every device function will be statically linked into the final PTX. This means CICC can afford far more aggressive import thresholds than CPU compilers, because the code size cost of importing is paid once per GPU binary rather than once per shared-object load.

The import subsystem reads `NVModuleSummary` data (built by `sub_D7D4E0`, see [Module Summary](./module-summary.md)) to make summary-guided decisions about which functions to pull from other translation units. Each candidate is evaluated against a floating-point threshold that incorporates callsite hotness, linkage type, and a per-priority-class multiplier. A global import budget caps the total number of imports to prevent compile-time explosion. After import, each materialized function receives `thinlto_src_module` metadata so downstream passes (particularly the [inliner](./inliner-cost.md)) know its origin module.

| | |
|---|---|
| **Import driver** | `sub_1854A20` (`0x1854A20`, 4,326 B) |
| **Threshold computation** | `sub_1853180` (`0x1853180`, 5,059 B) |
| **Threshold comparison gate** | `sub_18518A0` (`0x18518A0`) |
| **Import execution** | `sub_15E4B20` (`0x15E4B20`) |
| **Import candidate evaluator** | `sub_1852CC0` (`0x1852CC0`) |
| **Entry point** | `sub_1855B10` (`0x1855B10`, 10,503 B) |
| **Whole-module processing** | `sub_1858B90` (`0x1858B90`, 31,344 B) |
| **Type metadata propagation** | `sub_185E850` (`0x185E850`, 24,263 B) |
| **Pipeline registration** | `"function-import"` (slot 43, Module pass) |
| **Knob constructor** | `ctor_184_0` (`0x4DA920`, 13,693 B) + `ctor_029` (`0x489C80`, 1,120 B) |

## Why GPU ThinLTO Differs from CPU ThinLTO

Upstream LLVM's ThinLTO was designed for CPU executables and shared libraries where import decisions must balance code size (impacts disk, cache, page faults) against optimization opportunity (cross-module inlining, constant propagation). The default `import-instr-limit` is 100 instructions, the cold multiplier is 0, and the hot multiplier is 10x. These conservative defaults reflect a world where over-importing bloats `.text` sections shared across address spaces.

GPU compilation inverts these tradeoffs:

1. **No shared libraries.** Device code is statically linked into a fatbinary. There is no dynamic linker, no GOT, no PLT. Importing a function costs compile time but has zero runtime overhead beyond instruction cache pressure.

2. **Function calls are expensive.** As documented in the [inliner cost model](./inliner-cost.md), every GPU function call marshals arguments through `.param` address space via `st.param` / `ld.param` sequences. Inlining (which requires importing first) eliminates this overhead entirely.

3. **Closed-world optimization.** The compiler sees all device code. There are no opaque DSOs. This means aggressive import cannot break ABI contracts that don't exist.

4. **Register pressure is the real constraint.** On GPU, the limiting factor is not code size but register count, which determines occupancy. Import + inline can actually *reduce* register pressure by enabling cross-function register allocation and eliminating `.param`-space spills.

These factors push CICC toward much more aggressive import thresholds. The priority-class multiplier system (section below) allows CICC to tune import aggressiveness per-callsite rather than using a single global threshold.

## What Gets Imported and What Does Not

The `NVModuleSummary` builder (`sub_D7D4E0`) assigns a 4-level import priority to every global value when building the module summary index:

| Priority | Meaning | Import behavior |
|----------|---------|-----------------|
| 0 | Not importable | Local/hidden linkage, never imported |
| 1 | Importable, not preferred | Will import only if threshold is generous |
| 2 | Standard importable | Normal import candidate |
| 3 | Force-import | Highest priority, always imported if budget allows |

The priority is determined by querying the `ImportPriorityTable` (parameter `a4` of `sub_D7D4E0`) via `sub_D84370`, `sub_D84440` (force-import check), and `sub_D84450` (importable check). A global override at `dword_4F87C60` can force all symbols to priority 1 or higher.

**Functions that are imported:**
- `__device__` functions with internal or linkonce_odr linkage (template instantiations, inline functions)
- Math library implementations (libdevice functions) called from device code
- Helper functions from header-only libraries (Thrust, CUB, cutlass templates)
- Constant global variables with initializers (`import-constants-with-refs` = true by default)

**Functions that are NEVER imported:**
- **Kernels (`__global__` functions).** These are entry points. They are never candidates for cross-module import because they represent the root of execution; they are called from host code, not from other device functions. The summary builder marks them as non-importable.
- **Host functions.** Host code is handled by the host compiler (gcc/clang), not cicc. They never appear in the device module summary.
- **Functions in address space 25.** The summary builder at lines 1388-1395 explicitly skips functions whose type resolves to address space 25, with a `goto LABEL_495` that bypasses the import-eligible path. The raw report notes: "device functions can't be cross-module imported in ThinLTO" -- this refers specifically to functions that are *declarations only* with device-memory address space linkage, meaning they reference device-side symbols without a definition in the current TU.
- **Functions with the "not importable" flag.** Bit 4 (`0x10`) of the linkage byte at offset `+0x0C` in the function summary entry. The import driver checks `test byte [entry+0Ch], 0x10` and skips on set.

## Import Algorithm

The import process runs in two stages: threshold computation (`sub_1853180`) builds a list of qualifying candidates, then the import driver (`sub_1854A20`) materializes them.

### Stage 1: Threshold Computation (`sub_1853180`)

```
threshold_compute(summary_ctx, module_info, base_threshold,
                  guid_hash_table, result_array, visited_set):

    for each candidate in summary_ctx.candidate_array:
        guid = candidate.guid & ~0x7    // mask tag bits

        // --- GUID dedup via hash table ---
        if guid_hash_table.size > 0:
            slot = (guid * 37) & (table_size - 1)    // multiplicative hash
            if guid_hash_table[slot] == guid:
                continue    // already evaluated
            // linear probing on collision; sentinel -1 = empty

        // --- Linkage-type dispatch (11 cases) ---
        linkage = candidate.entry[0x0C] & 0x0F
        switch linkage:
            case 0,1,3,5,6,7,8:    // standard path
                if linkage in {7, 8}:    // weak/weak_odr
                    verify name match via memcmp
            case 2,4,9,10:             // special handling path

        // --- Priority-class threshold adjustment ---
        priority_class = (candidate.flags >> 0) & 0x7    // 3-bit field
        threshold_f = float(base_threshold)

        switch priority_class:
            case 3 (hot):       threshold_f *= hot_multiplier
            case 1 (cold):      threshold_f *= cold_multiplier
            case 4 (critical):  threshold_f *= critical_multiplier
            default:            threshold_f *= default_multiplier

        adjusted_threshold = int(threshold_f)

        // --- Cost comparison ---
        function_cost = candidate.entry[0x40]    // IR instruction count
        if adjusted_threshold < function_cost:
            continue    // too expensive

        // --- Global budget check ---
        if global_import_budget >= 0:
            if current_import_count >= global_import_budget:
                continue    // budget exhausted

        // --- Emit to result array (24-byte entries) ---
        result_array.push({guid, adjusted_threshold, import_record_ptr})
        current_import_count += 1

        // --- Max-threshold-wins for duplicates ---
        if guid already in result_array:
            if existing.threshold >= adjusted_threshold:
                skip
            else:
                existing.threshold = adjusted_threshold
```

The function uses up to a 4-level unrolled name comparison (offsets `[r12-8]`, `[r12]`, `[r12+8]`, `[r12+10h]`) for the common case of functions with up to 4 name components. This avoids loop overhead for the typical C++ mangled name lookup.

### Stage 2: Import Driver (`sub_1854A20`)

The driver runs a **triple-pass** evaluation over import candidate lists, processing them in priority order:

1. **Primary pass** -- highest-priority candidates from the forward-linked import list at `[rcx]`
2. **Secondary pass** -- medium-priority candidates from `[rcx+10h]`
3. **Tertiary pass** -- lowest-priority candidates from `[rcx+30h]`

For each candidate in each pass:

```
import_driver(import_ctx, module_summary_index, source_module, guid_map):

    for each pass in {primary, secondary, tertiary}:
        for each candidate in pass.linked_list:
            // sentinel check: skip -8 (empty) and -4 (deleted)
            if candidate == 0xFFFFFFFFFFFFFFF8: continue
            if candidate == 0xFFFFFFFFFFFFFFFC: continue

            // importable flag check
            if !(candidate[-0x21] & 0x20): continue

            summary = candidate[-0x38]
            sub_15E4EB0(candidate, summary)    // resolve name

            // threshold gate
            cost      = candidate[+0x10]
            hot_count = candidate[+0x08]
            if !sub_18518A0(hot_count, cost):  // threshold comparison
                continue

            // execute import
            sub_15E4B20(import_ctx, summary)

            // attach provenance metadata (if enabled)
            if byte_4FAAA20:
                source_name = sub_161FF10(summary)
                sub_1627100(function, "thinlto_src_module", source_name)
```

The linked-list data structure uses 8-byte entries with sentinels `-8` (`0xFFFFFFFFFFFFFFF8`) for empty slots and `-4` (`0xFFFFFFFFFFFFFFFC`) for deleted slots -- a standard open-addressing hash map pattern.

After all three passes complete, the driver runs a result iteration loop that calls `sub_1670560` to check whether each imported function already exists in the source module. Functions already present are skipped; the rest are materialized into the destination module.

## Threshold Multiplier Constants

The four floating-point multiplier constants are stored in the `.data` section and are set by the corresponding `cl::opt` registrations in `ctor_184_0`:

| Address | Knob | LLVM Default | Purpose |
|---------|------|-------------|---------|
| `dword_4FAAE80` | `import-hot-multiplier` | 10.0 | Multiplier for hot callsites |
| `dword_4FAACC0` | `import-cold-multiplier` | 0.0 | Multiplier for cold callsites |
| `dword_4FAADA0` | `import-critical-multiplier` | 100.0 | Multiplier for critical callsites |
| `dword_4FAB040` | (default path) | 1.0 | Multiplier when no priority class matches |

With the upstream default `import-instr-limit` of 100, a hot callsite gets threshold 1,000 instructions and a critical callsite gets threshold 10,000. The cold multiplier of 0.0 means cold functions are *never* imported by default -- the threshold evaluates to zero.

The evolution factors control how thresholds decay as imports cascade through the call graph:

| Knob | LLVM Default | Effect |
|------|-------------|--------|
| `import-instr-evolution-factor` | 0.7 | Each transitive import level reduces the threshold to 70% of the previous |
| `import-hot-evolution-factor` | 1.0 | Hot callsite chains do *not* decay (threshold stays constant through transitive imports) |

## Global Import Budget

Two globals control the total import count:

| Address | Role | Default |
|---------|------|---------|
| `dword_4FAB120` | Maximum allowed imports | -1 (unlimited) |
| `dword_4FAA770` | Running import counter | 0 (reset per module) |

The budget check at `0x185340A`:

```asm
mov  eax, cs:dword_4FAB120   ; load budget
test eax, eax
js   proceed                   ; negative = unlimited
cmp  cs:dword_4FAA770, eax   ; counter vs budget
jge  skip                     ; at or over budget -> skip
```

When the budget is -1 (the `import-cutoff` default), the `js` (jump-if-sign) branch is taken unconditionally, bypassing the budget check. Setting `-import-cutoff=N` limits the total number of imported functions to N, useful for debugging import-related miscompilations via bisection.

## Integration with the 20,000-Budget Inliner

The import + inline pipeline in CICC works as a two-phase system:

1. **Import phase** (this page): ThinLTO brings cross-module function bodies into the current module based on summary-guided threshold decisions. The imported functions are marked with `thinlto_src_module` metadata.

2. **Inline phase** ([inliner cost model](./inliner-cost.md)): The NVIDIA custom inliner at `sub_1864060` runs with a 20,000-unit per-caller budget. Imported functions are prime inlining candidates because they were specifically imported *because* they are called from this module.

The `inliner-function-import-stats` knob (registered in `ctor_186_0` at `0x4DBEC0`, values: `basic` or `verbose`) tracks how many imported functions were actually inlined. This provides feedback on whether the import thresholds are well-calibrated: if functions are imported but then not inlined (because they exceed the inline budget), the import was wasted compile time.

The typical flow for a template-heavy CUDA library like CUB or cutlass:

1. Each `.cu` file compiles to a ThinLTO bitcode module with a summary index
2. The thin link step reads all summaries and builds a combined index
3. For each module, `sub_1853180` evaluates import candidates using the combined index
4. Hot template instantiations (e.g., `cub::DeviceReduce::Sum<float>`) get threshold `base * 10.0` (hot) or `base * 100.0` (critical)
5. The imported function bodies arrive in the module and are immediately available to the 20,000-budget inliner
6. The inliner folds the imported template bodies into their callers, eliminating `.param` marshaling

## Knob Inventory

All knobs are registered across two constructors:

**`ctor_184_0` at `0x4DA920`** (ThinLTO Function Import options):

| Knob | Type | Default | Effect |
|------|------|---------|--------|
| `import-instr-limit` | unsigned | 100 | Base instruction count threshold |
| `import-cutoff` | int | -1 | Max total imports (-1 = unlimited) |
| `import-instr-evolution-factor` | float | 0.7 | Threshold decay per transitive level |
| `import-hot-evolution-factor` | float | 1.0 | Hot chain decay (1.0 = no decay) |
| `import-hot-multiplier` | float | 10.0 | Threshold multiplier for hot callsites |
| `import-critical-multiplier` | float | 100.0 | Threshold multiplier for critical callsites |
| `import-cold-multiplier` | float | 0.0 | Threshold multiplier for cold callsites |
| `print-imports` | bool | false | Print names of imported functions |
| `print-import-failures` | bool | false | Print rejected candidates with reasons |
| `compute-dead` | bool | true | Strip dead symbols from index |
| `enable-import-metadata` | bool | false | Attach `thinlto_src_module` / `thinlto_src_file` metadata |
| `summary-file` | string | (none) | Summary file path for `-function-import` |
| `import-all-index` | bool | false | Import every external function in the index |
| `import-declaration` | bool | false | Import function declarations as fallback |
| `force-import-all` | bool | false | Import even `noinline` functions |
| `thinlto-workload-def` | string | (none) | JSON file mapping root functions to import lists |
| `inliner-function-import-stats` | enum | (none) | Track import-to-inline conversion (`basic` / `verbose`) |

**`ctor_029` at `0x489C80`** (supplementary ThinLTO options):

| Knob | Type | Default | Effect |
|------|------|---------|--------|
| `propagate-attrs` | bool | true | Propagate attributes through the summary index |
| `import-constants-with-refs` | bool | true | Import constant globals that have references |

**`ctor_419` at `0x531850`** (FunctionAttrs inference):

| Knob | Type | Default | Effect |
|------|------|---------|--------|
| `disable-thinlto-funcattrs` | bool | false | Disable function attribute inference from ThinLTO summaries |

## Data Structures

### Import Candidate List

The linked list uses 8-byte entries:

| Offset | Content |
|--------|---------|
| `[node+0x00]` | Entry value (pointer or GUID) |
| `[node+0x08]` | Next slot |

Sentinels: `0xFFFFFFFFFFFFFFF8` (-8) = empty slot, `0xFFFFFFFFFFFFFFFC` (-4) = deleted slot.

### GUID Dedup Hash Table

| Field | Size | Description |
|-------|------|-------------|
| Slot size | 16 bytes | `{GUID (8B), metadata (8B)}` |
| Hash function | multiplicative | `slot = (GUID * 37) & (table_size - 1)` |
| Collision resolution | linear probing | Increment slot by 1, wrap at table_size |
| Empty sentinel | -1 | `0xFFFFFFFFFFFFFFFF` |

### Result Array

Growable array with 24-byte entries:

| Offset | Size | Content |
|--------|------|---------|
| `+0x00` | 8 | Function GUID |
| `+0x08` | 4 | Adjusted threshold value |
| `+0x10` | 8 | Import record pointer |

Header: `[+0x08]` = current count, `[+0x0C]` = capacity.

### Per-Function Summary Entry (import-relevant fields)

| Offset | Size | Content |
|--------|------|---------|
| `+0x08` | 4 | Entry type (2 = function summary) |
| `+0x0C` | 1 | Linkage byte: low 4 bits = linkage type, bit 4 = not-importable flag, bit 5 = importable flag |
| `+0x40` | 4 | Function cost (IR instruction count, used for threshold comparison) |

## Function Map

| Address | Size | Identity |
|---------|------|----------|
| `sub_1854A20` | 4,326 B | ThinLTO import driver (triple-pass candidate processing) |
| `sub_1853180` | 5,059 B | Threshold computation with GUID dedup and priority-class multipliers |
| `sub_18518A0` | -- | Threshold comparison gate (returns nonzero if candidate qualifies) |
| `sub_1852CC0` | -- | Import candidate evaluator |
| `sub_15E4B20` | -- | Execute import decision (materialize function into destination) |
| `sub_15E4EB0` | -- | Resolve function name/info from summary |
| `sub_1855B10` | 10,503 B | Entry point (parses `-function-import` / `-summary-file`) |
| `sub_1858B90` | 31,344 B | Whole-module ThinLTO processing |
| `sub_185E850` | 24,263 B | Type metadata propagation during import |
| `sub_1627100` | -- | Attach named metadata (used for `thinlto_src_module`) |
| `sub_161FF10` | -- | Resolve source module name string |
| `sub_1670560` | -- | Check if function exists in a given module |
| `sub_16704E0` | -- | Get "import source" module handle |
| `sub_16704F0` | -- | Get "import destination" module handle |
| `sub_16C1840` | -- | Format import remark (cost component) |
| `sub_16C1A90` | -- | Format import remark (threshold component) |
| `sub_16C1AA0` | -- | Finalize import remark string |
| `sub_1851560` | -- | Hash table insert (GUID dedup table) |
| `sub_1674380` | -- | Initialize resolved function summary storage |
| `sub_1851C60` | -- | Finalize empty-import path cleanup |
| `sub_161E7C0` | -- | Release import list entry data |

## Cross-References

- **[Inliner Cost Model](./inliner-cost.md)** -- the downstream consumer of imported functions. Import brings bodies into the module; the 20,000-budget inliner decides whether to fold them into callers.
- **[Module Summary](./module-summary.md)** -- `sub_D7D4E0` builds the `NVModuleSummary` that drives import decisions. The 4-level priority system, complexity budget, and CUDA-specific filtering all originate here.
- **[Pipeline & Ordering](../llvm/pipeline.md)** -- `function-import` is registered as pipeline slot 43, a Module-level pass.
- **[IP Memory Space Propagation](../passes/ipmsp.md)** -- after import, cross-module functions may carry address-space annotations that IPMSP must reconcile.
