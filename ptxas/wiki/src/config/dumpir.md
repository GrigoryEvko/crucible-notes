# DUMPIR & NamedPhases

The DUMPIR knob and NamedPhases option are the two primary mechanisms for inspecting ptxas's internal IR at arbitrary points in the 159-phase optimization pipeline. DUMPIR is an OCG string knob that triggers an IR dump after a named phase completes. NamedPhases is a separate OCG string knob (index 298) that restricts the pipeline to execute only the specified phases, effectively allowing selective phase execution and reordering. Both knobs accept phase names resolved through a case-insensitive binary search over a sorted table of 144 phase names (`sub_C641D0`, 305 bytes).

| | |
|---|---|
| **DUMPIR knob** | OCG string knob (ROT13: `QhzcVE`), registered in `ctor_005` at `0x412B80` |
| **NamedPhases knob** | OCG knob index 298, runtime offset 21456 in knob value array |
| **Phase name lookup** | `sub_C641D0` (305 bytes, case-insensitive binary search) |
| **Table sort** | `sub_C63FA0` (on-demand iterative quicksort via `sub_C639A0`) |
| **Name table** | 144 entries at `off_22BD0C0` + 5 arch-specific additions |
| **NamedPhases parser** | `sub_798B60` (1,776 bytes) |
| **Phase fragment parser** | `sub_798280` (900 bytes) |
| **Report passes** | Phases 9, 96, 102, 126, 129, 130 |
| **Sentinel return** | 158 (NOP phase, returned on lookup failure) |

## DUMPIR Knob

The `DUMPIR` knob is a string-valued OCG knob that takes one or more phase names. When set, the compiler dumps the Ori IR state after the named phase executes. This is the primary IR inspection mechanism for NVIDIA developers debugging the optimization pipeline.

### Usage

```
ptxas -knob DUMPIR=AllocateRegisters input.ptx -o output.cubin
```

The knob value is a phase name string. The name is resolved through the phase name lookup function (`sub_C641D0`) using case-insensitive comparison, so `allocateregisters`, `ALLOCATEREGISTERS`, and `AllocateRegisters` all match.

The DUMPIR knob exists in two instantiations:
- **OCG instance** (ROT13: `QhzcVE` at `0x21BDBAD`): registered in `ctor_005` at `0x412B80`. This is the primary instance for the optimization pipeline.
- **DAG instance** (ROT13: `QhzcVE` at `0x21DCC95`): registered in `ctor_007` at `0x421920`. This controls IR dumps in the Mercury SASS/DAG pipeline.

### Diagnostic Reference

The DUMPIR knob is referenced in register allocation error diagnostics. When a register allocation verification failure occurs, `sub_A55D80` and `sub_A76030` emit:

```
Please use -knob DUMPIR=AllocateRegisters for debugging
```

This tells the developer to re-run with the DUMPIR knob set to `AllocateRegisters` to inspect the IR state entering register allocation, which helps diagnose mismatches between pre- and post-allocation reaching definitions.

## Related Dump Knobs

DUMPIR is part of a family of 15+ dump-related OCG knobs, all registered in `ctor_005` in the `0x4129*`--`0x412D*` address range:

| Knob Name | ROT13 | Address | Purpose |
|---|---|---|---|
| `DumpCallGraph` | `QhzcPnyyTencu` | `0x412A40` | Dump the inter-procedural call graph |
| `DumpCFG` | `QhzcPST` | `0x412A90` | Dump the control flow graph |
| `DumpFlow` | `QhzcSybj` | `0x412AE0` | Dump data flow information |
| `DumpInstPhase` | `QhzcVafgCunfr` | `0x412B30` | Dump per-instruction phase annotations |
| `DumpIR` | `QhzcVE` | `0x412B80` | Dump the Ori IR after a named phase |
| `DumpIRInfoAsInteger` | `QhzcVEVasbNfVagrtre` | `0x412BD0` | Dump IR with integer-format operand info |
| `DumpKnobs` | `QhzcXabof` | `0x412C20` | Dump all knob values to stderr |
| `DumpPerfMetricsForBlock` | `QhzcCresZrgevpfSbeOybpx` | `0x412C70` | Dump per-basic-block performance metrics |
| `DumpPerfStats` | `QhzcCresFgngf` | `0x412CC0` | Dump performance statistics |
| `DumpSASS` | `QhzcFNFF` | `0x412D10` | Dump generated SASS assembly |
| `DumpSBInstInfo` | `QhzcFOVafgVasb` | `0x412D60` | Dump scoreboard per-instruction info |

The DAG pipeline has its own set of dump knobs registered in `ctor_007` (`0x421880`--`0x421A10`):

| Knob Name | ROT13 | Address | Purpose |
|---|---|---|---|
| `DumpAnnot` | `QhzcNaabg` | `0x421880` | Dump instruction annotations |
| `DumpCFG` | `QhzcPST` | `0x4218D0` | Dump DAG pipeline CFG |
| `DumpIR` | `QhzcVE` | `0x421920` | Dump DAG pipeline IR |
| `DumpMercOpCounts` | `QhzcZrepBcPbhagf` | `0x421970` | Dump Mercury opcode distribution |
| `DumpReconstitutedBinary` | `QhzcErpbafgvghgrqOvanel` | `0x4219C0` | Dump reconstituted binary output |
| `DumpRPO` | `QhzcECB` | `0x421A10` | Dump reverse post-order traversal |

## NamedPhases Knob

The NamedPhases knob (OCG index 298) provides a mechanism to restrict the optimization pipeline to execute only specific phases. Unlike DUMPIR which passively observes, NamedPhases actively controls which phases run.

### Knob Location

NamedPhases is at OCG knob index 298. The runtime byte offset is `298 * 72 = 21456` from the knob state base. This is confirmed by the decompiled code in `sub_798B60`:

```c
// sub_798B60 (NamedPhases parser)
v11 = *(ctx + 72);                    // knob state base pointer
v12 = *(byte*)(v11 + 21456);          // type tag at knob index 298
if (!v12) return 0;                   // knob not set => no filtering
if (v12 == 5)                         // type 5 = string
    v14 = *(ptr*)(v11 + 21464);       // string value at +8 from type tag
```

### Parser -- `sub_798B60`

The NamedPhases parser (`sub_798B60`, 1,776 bytes) reads the knob value string and parses it into parallel arrays of up to 256 entries. It is called from two sites:

1. **OCG pipeline** (`sub_798B60` direct): parses the NamedPhases string from OCG knob index 298, referenced at address `0x798E90` where the string "NamedPhases" (`0x21B64C8`) appears in an error/diagnostic message.
2. **Mercury pipeline** (`sub_9F4040`): the Mercury encoder's phase reordering mechanism also references the "NamedPhases" string at `0x9F42B0`, using the same knob to control Mercury-side phase execution.

The parser operates as follows:

1. Reads knob value at offset 21456 from the knob state
2. If the knob is unset (type byte == 0), returns immediately (no filtering)
3. If the knob is a string (type byte == 5), extracts the string pointer
4. Copies the string into a pool-allocated buffer
5. Tokenizes using `strtok_r` with comma (`,`) as delimiter
6. For each token, calls `sub_798280` (ParsePhaseNameFragment) to split the phase name from optional parameters
7. Stores results in parallel arrays: names[], values[], full_strings[] (max 256 entries)

### Phase Name Fragment Parser -- `sub_798280`

Each comma-separated token in the NamedPhases string is parsed by `sub_798280` into two components:

- **Phase name**: characters up to the first `,` separator, uppercased during parsing
- **Parameter suffix**: characters after `,` up to the next `+` delimiter or end-of-string

The `+` character acts as an entry separator (analogous to how the DisablePhases string uses `+` to delimit multiple phase names). This allows:

```
-knob NamedPhases=PhaseA,param1+PhaseB,param2+PhaseC
```

### Mercury NamedPhases -- `sub_9F4040`

The Mercury encoder pipeline (`sub_9F4040`, 1,850 lines decompiled) uses the NamedPhases knob to support phase reordering within the Mercury backend. In addition to standard pipeline phase names, it recognizes Mercury-specific pseudo-phases:

| Name | Purpose |
|---|---|
| `shuffle` | Mercury instruction shuffle pass |
| `swap1` through `swap6` | Mercury register swap passes (6 levels) |
| `OriPerformLiveDead` | Liveness analysis within Mercury context |
| `OriCopyProp` | Copy propagation within Mercury context |

These Mercury-specific names are hardcoded in `sub_9F4040` and do not appear in the main phase name table.

## Phase Name Lookup -- `sub_C641D0`

The binary search function `sub_C641D0` (305 bytes) resolves a phase name string to a phase index. It is the core name resolution used by both DUMPIR and NamedPhases.

### Algorithm

```c
int PhaseManager::lookup_phase(const char* query) {
    ensure_sorted();                          // sub_C63FA0

    // Binary search over sorted {name_ptr, index} pairs
    // Each entry is 16 bytes: [8-byte name pointer, 4-byte phase index, 4-byte padding]
    int lo = 0, hi = sorted_count;
    while (hi > 0) {
        int mid = hi / 2;
        // Case-insensitive string comparison via tolower()
        int cmp = strcasecmp(table[lo + mid].name, query);
        if (cmp < 0) {
            hi -= mid + 1;
            lo += mid + 1;
        } else if (cmp == 0) {
            return table[lo + mid].index;     // found
        } else {
            hi = mid;
        }
    }

    // Verify final position (handles edge case)
    if (lo < end && strcasecmp(table[lo].name, query) == 0)
        return table[lo].index;

    return 158;                               // sentinel: NOP phase
}
```

The comparison uses `tolower()` on each character individually, making the search fully case-insensitive. On lookup failure, the function returns 158 (the sentinel NOP phase), not an error code. This means misspelled phase names silently resolve to a no-op rather than producing an error.

### Sorted Table Construction -- `sub_C63FA0`

The sorted name table is lazily constructed. `sub_C63FA0` checks whether the current sorted count matches the expected count (stored at `PhaseManager+104`). If they differ, it:

1. Grows the sorted table array if needed (1.5x growth policy)
2. Copies name pointers from the raw phase name table (`off_22BD0C0`)
3. Each entry is 16 bytes: `{char* name, int phase_index}`, where `phase_index` is the array position
4. Sorts using iterative quicksort (`sub_C639A0`) with median-of-three pivot selection

The sort is performed once and cached. Subsequent lookups reuse the sorted table without re-sorting.

## Report Passes

Six phases in the pipeline are dedicated diagnostic/dump passes. They are no-ops by default and activate only when specific debug options are enabled:

| Phase | Name | Trigger | Output |
|---|---|---|---|
| 9 | `ReportInitialRepresentation` | DUMPIR knob, `--keep` | Ori IR after initial lowering (pre-optimization) |
| 96 | `ReportBeforeScheduling` | DUMPIR knob, `--keep` | Ori IR entering scheduling/RA stage |
| 102 | `ReportAfterRegisterAllocation` | DUMPIR knob, `--keep` | Ori IR after register allocation |
| 126 | `ReportFinalMemoryUsage` | `--stat=phase-wise` | Memory pool consumption summary |
| 129 | `DumpNVuCodeText` | `--keep`, DUMPIR | SASS text disassembly (cuobjdump-style) |
| 130 | `DumpNVuCodeHex` | `--keep`, DUMPIR | Raw SASS hex dump |

Additionally, `ReportBeforeRegisterAllocation` (at `0x22BD068`) is a phase name in the table but is handled as an arch-specific phase (index >= 139), providing an IR dump point immediately before register allocation in backends that override it.

### Report Pass Activation

Report passes check their activation condition in the `isNoOp()` virtual method. When the DUMPIR knob is set to a phase name, the report pass compares the current phase name against the DUMPIR value. If they match, `isNoOp()` returns `false` and the pass executes its dump logic.

The dispatch loop in `sub_C64F70` constructs diagnostic context strings around each phase execution:

```c
// Before execution:
snprintf(buffer, size, "Before %s", phase_name);   // 0x2065726F666542 = "Before " in LE

// After execution:
strcpy(buffer, "After ");
strcat(buffer, phase_name);
```

These strings appear in diagnostic output when `--stat=phase-wise` is enabled:

```
Before GeneralOptimize  ::  [Total 1234 KB]  [Freeable 567 KB]  [Freeable Leaked 12 KB] (2%)
After GeneralOptimize   ::  [Total 1456 KB]  [Freeable 789 KB]  [Freeable Leaked 23 KB] (3%)
```

### Phase-Wise Statistics -- `--stat=phase-wise`

The `--stat` CLI option (processed in `sub_432A00` at `0x432E5A`) accepts a comma-separated list of report modes:

```
ptxas --stat=phase-wise input.ptx -o output.cubin
```

| Mode | Short | Description |
|---|---|---|
| `time` | `t` | Print compilation time |
| `memory` | `m` | Print peak memory usage |
| `phase-wise` | `p` | Print per-phase time and memory delta |
| `detailed` | `d` | Print all of the above |

When `phase-wise` is enabled (string comparison at `0x4460F8` in `sub_445EB0`), the dispatch loop's timing flag (`PhaseManager+72`) is set, and `sub_C64310` runs after every phase to print memory deltas.

## Phase Name Table

The static phase name table at `off_22BD0C0` contains 145 entries: 1 sentinel ("All Phases Summary") plus 144 phase names. After sorting by `sub_C63FA0`, the binary search in `sub_C641D0` provides O(log n) lookup -- approximately 8 comparisons for 145 entries.

The 144 non-sentinel entries include:
- **139 base pipeline phases** (indices 0--138) with fixed names
- **5 arch-specific phase aliases** that map to indices >= 139:
  - `LateEnforceArgumentRestrictions`
  - `UpdateAfterScheduleInstructions`
  - `UpdateAfterOriDoSyncronization`
  - `ReportBeforeRegisterAllocation`
  - `UpdateAfterOriAllocateRegisters`

The `AllocateRegisters` string (`0x21F0229`) also appears as a phase name referenced by the register allocation subsystem (`sub_A55D80`, `sub_A76030`) and is present in the name table at `0x22BD490`.

## Interaction with --keep

The `--keep` flag triggers output file retention and activates certain report passes. When `--keep` is set:

1. Phase 129 (`DumpNVuCodeText`) writes a human-readable SASS disassembly to a `.sass` file
2. Phase 130 (`DumpNVuCodeHex`) writes raw SASS binary as hex
3. Report phases 9, 96, and 102 may produce `.ori` intermediate representation dumps

The `--keep` flag is processed in the CLI option handler (`sub_43CC70` at `0x43D850`) which generates the `.sass` file extension.

## Function Map

| Address | Size | Function | Confidence |
|---|---|---|---|
| `sub_798280` | 900 | `ParsePhaseNameFragment` -- splits `NAME,PARAM` from NamedPhases token | MEDIUM |
| `sub_798B60` | 1,776 | `NamedPhases::ParsePhaseList` -- tokenizes NamedPhases knob string | CERTAIN |
| `sub_9F4040` | ~7,400 | `MercuryNamedPhases` -- Mercury pipeline phase selection/reordering | HIGH |
| `sub_C639A0` | ~800 | `QuicksortNameTable` -- iterative quicksort for phase name table | MEDIUM |
| `sub_C63FA0` | ~600 | `EnsureSortedNameTable` -- lazy sorted table construction | MEDIUM |
| `sub_C641D0` | 305 | `PhaseManager::LookupPhase` -- case-insensitive binary search | CERTAIN |
| `sub_C64310` | 3,168 | `PhaseManager::ReportPhaseStats` -- per-phase timing/memory reporter | HIGH |
| `sub_C64F70` | 1,455 | `PhaseManager::Dispatch` -- main phase execution loop | CERTAIN |
| `sub_A55D80` | ~2,000 | `RegAlloc::VerifyReachingDefs` -- references DUMPIR in error message | HIGH |
| `sub_A76030` | ~1,000 | `RegAlloc::VerifyMismatch` -- references DUMPIR in error message | HIGH |

## Reimplementation Notes

1. **DUMPIR is a string knob**, not a boolean. The value is a phase name that triggers a dump after that specific phase. To dump at multiple points, run separate compilations with different DUMPIR values. There is no comma-separated multi-phase dump syntax for DUMPIR itself.

2. **NamedPhases uses comma+plus syntax.** Commas separate name-from-parameter within a single entry; `+` separates multiple entries. The phase name portion is uppercased during parsing. Parameters are preserved as-is.

3. **Lookup failure is silent.** An unrecognized phase name in DUMPIR or NamedPhases resolves to phase index 158 (NOP sentinel), not an error. The compiler does not warn about misspelled phase names.

4. **The sorted table is 16 bytes per entry**: `{char* name, int32 index, int32 padding}`. The sort is stable only within the quicksort's three-way partitioning -- duplicate names (which do not occur in practice) would have undefined ordering.

5. **Two DumpIR knob instances** exist (OCG and DAG). They are independent -- setting one does not affect the other. The OCG instance controls the 159-phase optimization pipeline; the DAG instance controls the Mercury SASS pipeline.

6. **Memory statistics format** uses three thresholds: bytes (< 1 KB), kilobytes with 3 decimals (< 10 MB), megabytes with 3 decimals (>= 10 MB). The reporter is `sub_C64310`.

7. **NamedPhases in Mercury** (`sub_9F4040`) supports additional pseudo-phases (`shuffle`, `swap1`--`swap6`, `OriPerformLiveDead`, `OriCopyProp`) that are not in the main phase table. These are Mercury-specific and handled by hardcoded string comparisons.

## Cross-References

- [Knobs System](./knobs.md) -- DUMPIR and NamedPhases are OCG knobs; ROT13 encoding, type system, access patterns
- [CLI Options](./cli-options.md) -- `--stat=phase-wise`, `--keep` flags that activate report passes
- [Phase Manager](../passes/phase-manager.md) -- dispatch loop, phase factory, name table infrastructure
- [Pass Inventory](../passes/index.md) -- complete 159-phase table with report pass positions
- [Register Allocator](../regalloc/overview.md) -- DUMPIR=AllocateRegisters diagnostic reference
- [Mercury Encoder](../codegen/mercury.md) -- Mercury-side NamedPhases and DAG DumpIR knob
