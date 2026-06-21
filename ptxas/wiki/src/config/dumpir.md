# DUMPIR & Recipe / NamedPhases

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base 0x400000 (non-PIE).*

The DUMPIR knob and the recipe/NamedPhases knob are the two primary mechanisms for inspecting ptxas's internal IR at points in the optimization pipeline. DUMPIR is an OCG string knob that triggers an IR dump after a named phase completes. The recipe knob (OCG knob index 298) diverts the optimizer driver onto the recipe path, where a small DSL builds a custom phase schedule. Both knobs accept phase names resolved through a case-insensitive binary search over the sorted phase-name table by `sub_C641D0` (305 bytes). The name table holds **159 entries** (IDs 0–158), copied from `off_22BD0C0`; on lookup failure the search returns the NOP sentinel, ID **158**.

| | |
|---|---|
| **DUMPIR knob** | OCG string knob (ROT13: `QhzcVE`), registered in `ctor_005` at `0x412B80` |
| **Recipe / NamedPhases knob** | OCG knob index 298 (`config+0x53D0` presence, `+0x53D8` value) |
| **Phase name lookup** | `sub_C641D0` (305 bytes, case-insensitive binary search) |
| **Table sort** | `sub_C63FA0` (on-demand iterative quicksort via `sub_C639A0`) |
| **Name table** | 159 entries at `off_22BD0C0` (1272 bytes); all IDs 0–158 named |
| **Recipe tokenizer** | `sub_798B60` (1,776 bytes) |
| **Recipe schedule builder** | `sub_9F4040` |
| **Report passes** | Phases 9, 96, 102, 126, 129, 130 |
| **Sentinel return** | 158 (NOP phase, returned on lookup failure) |

## DUMPIR Knob

The `DUMPIR` knob is a string-valued OCG knob that takes one or more phase names. When set, the compiler dumps the Ori IR state after the named phase executes. This is the primary IR inspection mechanism for NVIDIA developers debugging the optimization pipeline.

### Usage

```bash
ptxas -knob DUMPIR=AllocateRegisters input.ptx -o output.cubin
```

The knob value is a phase name string. The name is resolved through the phase name lookup function (`sub_C641D0`) using case-insensitive comparison, so `allocateregisters`, `ALLOCATEREGISTERS`, and `AllocateRegisters` all match.

The DUMPIR knob exists in two instantiations:
- **OCG instance** (ROT13: `QhzcVE` at `0x21BDBAD`): registered in `ctor_005` at `0x412B80`. This is the primary instance for the optimization pipeline.
- **DAG instance** (ROT13: `QhzcVE` at `0x21DCC95`): registered in `ctor_007` at `0x421920`. This controls IR dumps in the Mercury SASS/DAG pipeline.

### Diagnostic Reference

The DUMPIR knob is referenced in register allocation error diagnostics. When a register allocation verification failure occurs, `sub_A55D80` and `sub_A76030` emit:

```text
Please use -knob DUMPIR=AllocateRegisters for debugging
```

This tells the developer to re-run with the DUMPIR knob set to `AllocateRegisters` to inspect the IR state entering register allocation, which helps diagnose mismatches between pre- and post-allocation reaching definitions.

## Related Dump Knobs

DUMPIR is part of a family of 17 dump-related OCG knobs across two constructor registrations. The OCG pipeline registers 11 dump knobs in `ctor_005` (`0x412A40`--`0x412D60`); the Mercury/DAG pipeline registers 6 in `ctor_007` (`0x421880`--`0x421A10`). All knob names and their definition-table offsets are ROT13-encoded in the binary (e.g. `0k14q0` decodes to `0x14D0`).

### OCG Pipeline Dump Knobs (ctor_005)

| Knob Name | ROT13 | Reg Address | Def Offset | Purpose |
|---|---|---|---|---|
| `DumpCallGraph` | `QhzcPnyyTencu` | `0x412A40` | `0x1490` | Dump the inter-procedural call graph |
| `DumpCFG` | `QhzcPST` | `0x412A90` | `0x14A0` | Dump the control flow graph |
| `DumpFlow` | `QhzcSybj` | `0x412AE0` | `0x14B0` | Dump data flow information (reaching defs, live sets) |
| `DumpInstPhase` | `QhzcVafgCunfr` | `0x412B30` | `0x14C0` | Dump per-instruction phase annotations |
| `DumpIR` | `QhzcVE` | `0x412B80` | `0x14D0` | Dump the Ori IR after a named phase |
| `DumpIRInfoAsInteger` | `QhzcVEVasbNfVagrtre` | `0x412BD0` | `0x14E0` | Dump IR with integer-format operand info |
| `DumpKnobs` | `QhzcXabof` | `0x412C20` | `0x14F0` | Dump all knob values to stderr |
| `DumpPerfMetricsForBlock` | `QhzcCresZrgevpfSbeOybpx` | `0x412C70` | `0x1500` | Dump per-basic-block performance metrics |
| `DumpPerfStats` | `QhzcCresFgngf` | `0x412CC0` | `0x1510` | Dump performance statistics |
| `DumpSASS` | `QhzcFNFF` | `0x412D10` | `0x1520` | Dump generated SASS assembly |
| `DumpSBInstInfo` | `QhzcFOVafgVasb` | `0x412D60` | `0x1530` | Dump scoreboard per-instruction info |

The "Def Offset" column is the byte offset into the 16-byte-stride knob definition table. Dividing by 16 gives the definition-table index: `DumpCallGraph` is index 329, `DumpSBInstInfo` is index 339. These indices are distinct from the 72-byte runtime knob slot indices used by `GetKnobIntValue`.

Adjacent knobs in `ctor_005` (for boundary context):
- `0x4129F0`: `DoYieldInsertionWAR_SW2491854` (offset `0x1480`) — immediately before DumpCallGraph
- `0x412DB0`: `EmitLDCU` (offset `0x1540`) — immediately after DumpSBInstInfo

### Mercury/DAG Pipeline Dump Knobs (ctor_007)

| Knob Name | ROT13 | Reg Address | Purpose |
|---|---|---|---|
| `DumpAnnot` | `QhzcNaabg` | `0x421880` | Dump instruction annotations |
| `DumpCFG` | `QhzcPST` | `0x4218D0` | Dump DAG pipeline CFG |
| `DumpIR` | `QhzcVE` | `0x421920` | Dump DAG pipeline IR |
| `DumpMercOpCounts` | `QhzcZrepBcPbhagf` | `0x421970` | Dump Mercury opcode distribution |
| `DumpReconstitutedBinary` | `QhzcErpbafgvghgrqOvanel` | `0x4219C0` | Dump reconstituted binary output |
| `DumpRPO` | `QhzcECB` | `0x421A10` | Dump reverse post-order traversal |

Three knob names appear in both pipelines: `DumpCFG`, `DumpIR`, and (implicitly) their string addresses differ (`0x21BDBF0` vs `0x21DCCA0` for DumpCFG). Setting one does not affect the other.

## Recipe / NamedPhases Knob (OCG 298)

The recipe knob (OCG index 298) diverts the optimizer driver `sub_7FB6C0` onto the recipe path (`sub_9F63D0` → `sub_9F4040`), where a small DSL builds a **custom phase schedule** that replaces the default 157-entry order. Unlike DUMPIR, which passively observes, this knob actively controls which phases run and in what order. It is an internal engineering interface (gated by a knob, not a documented user-facing ptxas flag); default compiles never reach it. The recipe schedule is fed to the **same** dispatch loop `sub_C64F70` as the default path, with a computed length — and this is the only path that can schedule IDs 157 (`DebuggerBreak`) and 158 (`NOP`).

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

### Parser — `sub_798B60`

The recipe tokenizer (`sub_798B60`, 1,776 bytes) reads the knob value string and parses it into parallel arrays of up to 256 entries. The schedule builder `sub_9F4040` (reached from the optimizer driver via `sub_9F63D0`) consumes those arrays and applies the recipe DSL to the default 157-identity schedule. The "NamedPhases" string appears in both: at `0x798E90` in the tokenizer and at `0x9F42B0` in the schedule builder — they are two stages of the same recipe path, not a separate Mercury mechanism.

The tokenizer operates as follows:

1. Reads knob value at offset 21456 from the knob state
2. If the knob is unset (type byte == 0), returns immediately (no filtering)
3. If the knob is a string (type byte == 5), extracts the string pointer
4. Copies the string into a pool-allocated buffer
5. Tokenizes using `strtok_r` with comma (`,`) as delimiter
6. For each token, calls `sub_798280` (ParsePhaseNameFragment) to split the phase name from optional parameters
7. Stores results in parallel arrays: names[], values[], full_strings[] (max 256 entries)

### Phase Name Fragment Parser — `sub_798280`

Each comma-separated token in the NamedPhases string is parsed by `sub_798280` into two components:

- **Phase name**: characters up to the first `,` separator, uppercased during parsing
- **Parameter suffix**: characters after `,` up to the next `+` delimiter or end-of-string

The `+` character acts as an entry separator (analogous to how the DisablePhases string uses `+` to delimit multiple phase names). This allows:

```bash
-knob NamedPhases=PhaseA,param1+PhaseB,param2+PhaseC
```

### The Recipe DSL — `sub_9F4040`

The schedule builder `sub_9F4040` starts from the default 157-identity schedule (`sub_C60D20()`) and applies a small DSL parsed from the tokenized recipe string. Explicit phase IDs are clamped to `[0, 159]` and unknown phase names map to 158/NOP via `sub_C641D0`. These tokens operate on the **main OCG phase schedule** — they are not Mercury-specific:

| Token | Decompiled Line | Match Method | Effect |
|---|---|---|---|
| `NamedPhases` | 381 | string compare | replace the schedule with the explicit name list (each name → ID via `sub_C641D0`) |
| `p%d` | 467 | `sprintf "p%d"` | select phases by numeric index; value from the parallel value array, clamped to `[0,159]` |
| `shuffle` | 843 | strlen + byte compare (8 chars) | seeded pairwise rotation pass over the schedule slots (reps-controlled) |
| `reps` | — | string compare | number of shuffle iterations (≤ 256) |
| `swap1` | 950 | strlen + byte compare (6 chars) | swap a pair of schedule slots (base offset 1) |
| `swap2` | 1007 | strlen + byte compare (6 chars) | swap a pair of schedule slots (base offset 2) |
| `swap3` | 1061 | strlen + byte compare (6 chars) | swap a pair of schedule slots (base offset 3) |
| `swap4` | 1119 | strlen + byte compare (6 chars) | swap a pair of schedule slots (base offset 4) |
| `swap5` | 1162 | strlen + byte compare (6 chars) | swap a pair of schedule slots (base offset 5) |
| `swap6` | 1202 | `strcmp()` | swap a pair of schedule slots (base offset 6) |
| `dce1`/`dce2`/`dce3` | 1556 / 1282 / 1319 | counter trigger | inject `OriPerformLiveDead` (DCE) at the counter position |
| `cpy1`/`cpy2`/`cpy3` | 1648 / 1395 / 1442 | counter trigger | inject `OriCopyProp` at the counter position |

`shuffle` and `swap1`–`swap6` are recipe keywords, not phase names: they do not exist in the phase name table at `off_22BD0C0`. Their matching is done inline with strlen-guarded character comparison (not `strcmp` — except `swap6`, the last in a fallthrough chain). The `dceN`/`cpyN` tokens inject real registered phases: `OriPerformLiveDead` (the DCE family, IDs 18/38/70/99) and `OriCopyProp` (ID 22), both resolved through `sub_C641D0`. OCG knob 297 is pulsed once per applied recipe element as a diagnostic "recipe-applied" counter. For the full DSL see the [Optimization Pipeline](../pipeline/optimizer.md#recipe--namedphases-override-ocg-knob-298) page.

## Phase Name Lookup — `sub_C641D0`

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

### Sorted Table Construction — `sub_C63FA0`

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
// Before execution (line 117 of sub_C64F70):
*(_QWORD *)buffer = 0x2065726F666542LL;    // "Before " as 8-byte LE literal
memcpy(buffer + 7, phase_name, len + 1);   // append phase name after "Before "

// After execution (line 196 of sub_C64F70):
strcpy(buffer, "After ");                  // 6-byte prefix
memcpy(buffer + 6, phase_name, len + 1);   // append phase name after "After "
```

The literal `0x2065726F666542` decomposes as bytes `42 65 66 6F 72 65 20` = ASCII `"Before "` (7 bytes including trailing space, plus a null in the 8th byte that gets overwritten by memcpy). The "After " path uses `strcpy` instead of a literal store because it is only 6 bytes and the code path is post-execution (not latency-critical).

These strings appear in diagnostic output when `--stat=phase-wise` is enabled:

```text
Before GeneralOptimize  ::  [Total 1234 KB]  [Freeable 567 KB]  [Freeable Leaked 12 KB] (2%)
After GeneralOptimize   ::  [Total 1456 KB]  [Freeable 789 KB]  [Freeable Leaked 23 KB] (3%)
```

The string addresses in the binary are:
- `"Before "` at `0x22BC3D3`
- `"After "` at `0x22BC3DB`
- `"  ::  "` at `0x22BC3E2` (separator between phase name and stats)
- `"[Total "` at `0x22BC3E9`
- `"[Freeable "` at `0x22BC3F6`
- `"[Freeable Leaked "` at `0x22BC401`
- `"All Phases Summary"` at `0x22BC416` (final summary label)

### Phase-Wise Statistics — `--stat=phase-wise`

The `--stat` CLI option (processed in `sub_432A00` at `0x432E5A`) accepts a comma-separated list of report modes:

```bash
ptxas --stat=phase-wise input.ptx -o output.cubin
```

| Mode | Short | Description |
|---|---|---|
| `time` | `t` | Print compilation time |
| `memory` | `m` | Print peak memory usage |
| `phase-wise` | `p` | Print per-phase time and memory delta |
| `detailed` | `d` | Print all of the above |

When `phase-wise` is enabled (string comparison at `0x4460F8` in `sub_445EB0`), the dispatch loop's timing flag (`PhaseManager+72`) is set, and `sub_C64310` runs after every phase to print memory deltas.

## IR Output Format

The DUMPIR dump emits a per-function statistics header (using `#` comment prefix) followed by the Ori IR listing. The statistics header is emitted by `sub_A3A7E0` and contains hardware performance estimates computed from the current IR state.

### Per-Function Statistics Header

```text
# 142 instructions, 24 R-regs
# [inst=142] [texInst=0] [tepid=0] [rregs=24]
# [FP16 inst=0] [FP16 VectInst=0] [Percentage Vectorized=0.00]
# [est latency = 87] [LSpillB=0] [LRefillB=0], [SSpillB=0], [SRefillB=0], [LowLmemSpillSize=0] [FrameLmemSpillSize=0]
# [LNonSpillB=0] [LNonRefillB=0], [NonSpillSize=0]
# [Occupancy = 0.750000], [est numDivergentBranches=2] [attributeMemUsage=0], [programSize=1024]
# [est fp=12] [est half=0], [est trancedental=0], [est ipa=0], [est shared=0], [est controlFlow=8], [est loadStore=24]
# [est tex=0] [est pairs=4]
# [issue thru=0.888889] [fp thru=0.111111] [half thru=0.000000], [trancedental thru=0.000000], [ipa thru=0.000000]
# [shared thru=0.000000] [controlFlow thru=0.062500] [texLoadStore thru=0.187500], [reg thru=0.000000], [warp thru=0.000000]
# [partially unrolled loops=0] [non-unrolled loops=1]
# [CB-Bound Tex=0] [UR-Bound Tex=0] [Bindless Tex=0] [Partially Bound Tex=0]
# [UDP inst=0] [numVecToURConverts inst=0]
# [maxNumLiveValuesAtSuspend=0]
# [instHint=142] [instPairs=4]
# [worstcaseLat=87.000000]
# [avgcaseLat=52.500000]
# [SharedMem Alloc thru=0.000000]
# [Precise inst=0]
```

The format strings are at two locations in rodata:

| Address Range | Context | Notes |
|---|---|---|
| `0x21EBF76`--`0x21EC3B0` | Pre-register-allocation stats | Commas between some `[SSpillB=%d], [SRefillB=%d]` fields |
| `0x21FA008`--`0x21FA0A0` | Post-register-allocation stats | No commas: `[SSpillB=%d] [SRefillB=%d]` |

The `[Occupancy]` line's typo "trancedental" (missing 's') is present in the binary itself and matches NVIDIA's original source.

### Statistics Field Glossary

| Field | Meaning |
|---|---|
| `inst` | Total instruction count |
| `texInst` | Texture/surface instruction count |
| `tepid` | Texture instruction count (alternate metric) |
| `rregs` | R-register (GPR) count |
| `LSpillB` / `LRefillB` | Local-memory spill/refill byte counts |
| `SSpillB` / `SRefillB` | Shared-memory spill/refill byte counts |
| `LowLmemSpillSize` | Low local-memory spill total |
| `FrameLmemSpillSize` | Frame-level local-memory spill total |
| `LNonSpillB` / `LNonRefillB` | Local-memory non-spill traffic bytes |
| `Occupancy` | Estimated warp occupancy (0.0–1.0) |
| `numDivergentBranches` | Estimated divergent branch count |
| `attributeMemUsage` | Attribute memory usage (shader inputs) |
| `programSize` | Total program size in bytes |
| `issue thru` | Issue throughput (instructions per cycle) |
| `fp thru` / `half thru` | FP32 / FP16 throughput |
| `trancedental thru` | Transcendental (SFU) throughput |
| `ipa thru` | Interpolation throughput |
| `shared thru` | Shared memory throughput |
| `texLoadStore thru` | Texture + load/store throughput |
| `reg thru` | Register throughput |
| `warp thru` | Warp-level throughput |
| `CB-Bound Tex` | Constant-bank-bound texture references |
| `UR-Bound Tex` | Uniform-register-bound texture references |
| `Bindless Tex` | Bindless texture references |
| `UDP inst` | Uniform datapath instruction count |
| `numVecToURConverts` | Vector-to-uniform-register conversion count |
| `maxNumLiveValuesAtSuspend` | Peak live values at suspension point |
| `instHint` / `instPairs` | Instruction hint count / instruction pair count |
| `worstcaseLat` / `avgcaseLat` | Worst-case / average-case latency estimates |
| `SharedMem Alloc thru` | Shared memory allocation throughput |
| `Precise inst` | Precise (non-relaxed) instruction count |

### Mercury Pipeline Dump Points

The Mercury/DAG pipeline emits its own "After" labels at fixed points in the encode-decode-expand flow. These labels are used by both the DumpIR (DAG) knob and the DumpAnnot knob:

| Label | String Address | Pipeline Stage |
|---|---|---|
| `After Decode` | `0x202D5CE` | After initial SASS decode |
| `After Expansion` | `0x202D5DB` | After instruction expansion |
| `After WAR post-expansion` | `0x202D604` | After WAR insertion (post-expansion) |
| `After Opex` | `0x202D60F` | After operand expansion |
| `After WAR post-opexing` | `0x202DCD0` | After WAR insertion (post-opex) |
| `After MercWARs` | `0x202DCDF` | After Mercury WAR pass |
| `After MercOpex` | `0x21E5C33` | After Mercury operand expansion |
| `After MercConverter` | `0x22B7B38` | After Mercury format conversion |
| `After MercExpand` | `0x22BC3DB` | After Mercury instruction expansion |
| `After EncodeAndDecode` | `0x23D1A60` | After encode-decode round-trip |

### Memory Statistics Format

The `sub_C64310` (ReportPhaseStats) function formats memory sizes using three thresholds:

| Size Range | Format | Example |
|---|---|---|
| < 1024 bytes | `%d` (raw integer) | `512` |
| < 10 MB | `%.3lf KB` (kilobytes, 3 decimals) | `1234.567 KB` |
| >= 10 MB | `%.3lf MB` (megabytes, 3 decimals) | `12.345 MB` |

The memory format reuses the suffix from `"PeakMemoryUsage = %.3lf KB"` (at `0x1CE7BB6`) by referencing the string at offset +24 to extract just `" KB"`. The pool-consumption variant uses `"[Pool Consumption = "` at `0x22BC3B3`.

## Phase Name Table

The static phase name table at `off_22BD0C0` contains **159 entries** (IDs 0–158), copied wholesale into the PhaseManager by `sub_C62720` (`1272 = 159*8` bytes). After sorting by `sub_C63FA0`, the binary search in `sub_C641D0` provides O(log n) lookup — approximately 8 comparisons for ~159 entries.

The 159 entries cover every phase ID:
- **IDs 0–138** — the base pipeline phases with fixed names
- **IDs 139–156** — the late-pipeline phases (Mercury encoding, scoreboards, register map, diagnostics), all named, e.g. `ProcessO0WaitsAndSBs` (139) … `DumpNVuCodeHex` (156)
- **IDs 157–158** — `DebuggerBreak` and `NOP`, named and registered but excluded from the default 157-entry schedule

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
| `sub_798280` | 900 | `ParsePhaseNameFragment` — splits `NAME,PARAM` from a recipe token | MEDIUM |
| `sub_798B60` | 1,776 | recipe tokenizer — tokenizes the OCG-knob-298 recipe string | CERTAIN |
| `sub_9F4040` | ~7,400 | recipe schedule builder — applies the recipe DSL to the default OCG schedule | HIGH |
| `sub_9F63D0` | 342 | recipe path entry — builds PhaseManager, calls `sub_9F4040`, dispatches via `sub_C64F70` | HIGH |
| `sub_A3A7E0` | ~2,000 | `CodeObject::EmitStats` — per-function statistics header printer | HIGH |
| `sub_C639A0` | ~800 | `QuicksortNameTable` — iterative quicksort for phase name table | MEDIUM |
| `sub_C63FA0` | ~600 | `EnsureSortedNameTable` — lazy sorted table construction | MEDIUM |
| `sub_C641D0` | 305 | `PhaseManager::LookupPhase` — case-insensitive binary search | CERTAIN |
| `sub_C64310` | 3,168 | `PhaseManager::ReportPhaseStats` — per-phase timing/memory reporter | HIGH |
| `sub_C64F70` | 1,455 | `PhaseManager::Dispatch` — main phase execution loop | CERTAIN |
| `sub_A55D80` | ~2,000 | `RegAlloc::VerifyReachingDefs` — references DUMPIR in error message | HIGH |
| `sub_A76030` | ~1,000 | `RegAlloc::VerifyMismatch` — references DUMPIR in error message | HIGH |

## Reimplementation Notes

1. **DUMPIR is a string knob**, not a boolean. The value is a phase name that triggers a dump after that specific phase. To dump at multiple points, run separate compilations with different DUMPIR values. There is no comma-separated multi-phase dump syntax for DUMPIR itself.

2. **NamedPhases uses comma+plus syntax.** Commas separate name-from-parameter within a single entry; `+` separates multiple entries. The phase name portion is uppercased during parsing. Parameters are preserved as-is.

3. **Lookup failure is silent.** An unrecognized phase name in DUMPIR or NamedPhases resolves to phase index 158 (NOP sentinel), not an error. The compiler does not warn about misspelled phase names.

4. **The sorted table is 16 bytes per entry**: `{char* name, int32 index, int32 padding}`. The sort is stable only within the quicksort's three-way partitioning — duplicate names (which do not occur in practice) would have undefined ordering.

5. **Two DumpIR knob instances** exist (OCG and DAG). They are independent — setting one does not affect the other. The OCG instance controls the 159-phase optimization pipeline; the DAG instance controls the Mercury SASS pipeline. Three knob names (`DumpCFG`, `DumpIR`, `DumpAnnot`/`DumpRPO`) have separate OCG and DAG instances with distinct ROT13 string addresses.

6. **Memory statistics format** uses three thresholds: bytes (< 1 KB), kilobytes with 3 decimals (< 10 MB), megabytes with 3 decimals (>= 10 MB). The reporter is `sub_C64310`.

7. **NamedPhases in Mercury** (`sub_9F4040`) supports 7 pure pseudo-phases (`shuffle`, `swap1`--`swap6`) that do not exist in the main phase table. These use inline strlen-guarded byte comparison, not `strcmp` (except `swap6`). Two additional names (`OriPerformLiveDead`, `OriCopyProp`) ARE in the main table but are conditionally injected into Mercury's phase sequence based on register-pressure/correctness state flags.

8. **The "Before" string is a raw 8-byte LE literal store**, not a `strcpy`. The dispatch loop writes `0x2065726F666542` directly to the buffer, which is `"Before "` in ASCII. The `"After "` banner is emitted by a similar inline literal store (a 4-byte `0x65746641` "Afte" plus a 2-byte `0x2072` "r " plus a null), not a runtime `strcpy`. Both are gated by `isNoOp()` returning false; neither affects whether the phase's `execute()` runs.

9. **Statistics header has two variants.** The pre-register-allocation format strings (at `0x21EC050`) use commas between some spill fields: `[SSpillB=%d], [SRefillB=%d]`. The post-register-allocation variant (at `0x21FA008`) drops those commas: `[SSpillB=%d] [SRefillB=%d]`. A reimplementation should match whichever variant is appropriate for the dump point.

10. **The "trancedental" typo is canonical.** Both the format string and the stats output use "trancedental" (missing 's'). A reimplementation should preserve this spelling for compatibility with tools that parse the output.

## Cross-References

- [Knobs System](./knobs.md) — DUMPIR and NamedPhases are OCG knobs; ROT13 encoding, type system, access patterns
- [CLI Options](./cli-options.md) — `--stat=phase-wise`, `--keep` flags that activate report passes
- [Phase Manager](../passes/phase-manager.md) — dispatch loop, phase factory, name table infrastructure
- [Pass Inventory](../passes/index.md) — complete 159-phase table with report pass positions
- [Register Allocator](../regalloc/overview.md) — DUMPIR=AllocateRegisters diagnostic reference
- [Optimization Pipeline](../pipeline/optimizer.md) — the OCG-knob mechanism and the full recipe DSL
- [Mercury Encoder](../codegen/mercury.md) — the DAG-side DumpIR knob instance
