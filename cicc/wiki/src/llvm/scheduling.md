# Instruction Scheduling

CICC v13.0 implements three distinct scheduling subsystems: MRPA (Machine Register Pressure Analysis) for incremental pressure tracking during MCSE, a Swing Modulo Scheduling pipeliner for loop bodies, and `ScheduleDAGMILive` for post-RA instruction ordering. All three maintain per-register-class pressure arrays but differ in granularity and update frequency. A texture group merge pass (`sub_2DDE8C0`) acts as a scheduling-adjacent optimization that groups texture load instructions for hardware coalescing.

| | |
|---|---|
| **MRPA incremental tracker** | `sub_2E5A4E0` (primary), `sub_1E00370` (backend variant) |
| **MachinePipeliner (SMS)** | `sub_3563190` |
| **ScheduleDAGMILive** | `sub_355F610` |
| **Instruction selection heuristic** | `sub_3557A10` |
| **Texture group merge** | `sub_2DDE8C0` |
| **Scheduling mode switch** | `sub_21668D0` (post-RA), `sub_2165850` (pre-RA) |

## MRPA: Incremental Register Pressure Tracking

MRPA (Machine Register Pressure Analysis) provides incremental register pressure tracking for the Machine Common Subexpression Elimination (MCSE) pass. Rather than recomputing pressure from scratch after each instruction move or elimination, MRPA applies delta updates to maintain a running pressure state.

The primary implementation lives at `sub_2E5A4E0`, with a backend variant at `sub_1E00370`. Both use DenseMap hash tables for per-instruction pressure data with the hash function `(ptr >> 9) ^ (ptr >> 4)`, empty sentinel `-8`, tombstone sentinel `-16`, minimum 64 buckets, and power-of-two sizing.

The incremental update flow:

1. Build a worklist of basic blocks via DFS (visited set at `v292`--`v295`).
2. For each BB: create instruction tracking entries in the DenseMap at context offsets `+80` through `+104`.
3. Filter schedulable instructions via `sub_2E501D0`.
4. Scan operands (40-byte entries iterated at `v69`/`v70`).
5. For each virtual register operand:
   - `sub_2EBEF70` -- find existing rename mapping.
   - `sub_2EBEE10` -- query register info (class, constraints).
   - `sub_2EBE820` -- attempt register rename if profitable.
   - `sub_2EBF120` -- free old register after successful rename.
6. Check register class constraints via `sub_E922F0` (sub-register list).
7. Validate pressure feasibility via `sub_2E4F9C0` using per-BB data at `v279[36]`.
8. Track rename counts at `*((_DWORD*)v254 + 16)` and `*((_DWORD*)v254 + 17)`.
9. Erase unprofitable instructions via `sub_2E88E20`.

**Register liveness queries** (`sub_1DF76E0`) check whether a register is live in an instruction range `[a3, a4]` using `_bittest` on register class bitmaps. A compressed alias table at context offset `+240` stores sub-register overlap information in 24-byte entries containing alias counts and alias data offsets.

**Code motion feasibility** (`sub_1DF7A80`) validates whether an instruction can be moved by checking single-predecessor relationships between basic blocks, validating against an allocation bitmask at allocator offset `+38`, walking an instruction window bounded by offset `+296` (window size), and counting conflicting operands. An rb-tree set (offsets 56--88) tracks the affected registers.

### MRPA Verification

A debug-only verification path checks incremental update correctness against full recomputation. Four conditions must hold simultaneously:

1. Context enable flag (`v7 + 40`) is set -- always true during MCSE.
2. `verify-update-mcse` is ON -- user must explicitly enable this debug knob.
3. `incremental-update-mcse` is ON -- default is ON.
4. `sub_2E59B70` returns false -- full recomputation disagrees with the incremental state.

When all conditions hold, the error `"Incorrect RP info from incremental MRPA update"` fires via `sub_C64ED0`. The `print-verify` knob controls whether detailed per-register-class mismatch data is printed.

| Knob | Default | Description |
|---|---|---|
| `incremental-update-mcse` | true | Incrementally update register pressure analysis |
| `verify-update-mcse` | false | Verify incremental update by full RP analysis |
| `print-verify` | false | Print problematic RP info if verification failed |

To trigger verification: `cicc -Xcuda -verify-update-mcse input.cu`. NVIDIA keeps this check off by default since the full rescan is O(n) and expensive.

## MachinePipeliner: Swing Modulo Scheduling

The MachinePipeliner (`sub_3563190`) implements Swing Modulo Scheduling (SMS) for software pipelining of loop bodies. It overlaps iterations of a loop to improve throughput on pipelined hardware by interleaving instructions from different iterations.

The setup chain runs through `sub_2F97F60` -> `sub_3559990` -> `sub_3542B20` -> `sub_2F90200` -> `sub_354CBB0` -> `sub_35449F0`, creating a `ScheduleDAG` context of 616 bytes (`sub_3547FE0`). Working copies of 88-byte SUnit entries are allocated at `v416`/`v419`.

The core pipeline executes ten stages sequentially:

| Step | Function | Purpose |
|---|---|---|
| 1 | `sub_35476E0` | DAG construction / dependency analysis |
| 2 | `sub_35523F0` | Recurrence detection / RecMII computation |
| 3 | `sub_35546F0` | Resource usage / ResMII computation |
| 4 | `sub_3543340` | MII = max(RecMII, ResMII) finalization |
| 5 | `sub_35630A0` | Node ordering / priority assignment |
| 6 | `sub_35568E0` | Schedule table initialization |
| 7 | `sub_35433F0` | Pre-scheduling transforms |
| 8 | `sub_3557A10` | Instruction ordering/selection |
| 9 | `sub_354A760` | Schedule finalization / modulo expansion |
| 10 | `sub_355F610` | ScheduleDAGMILive integration |

The result flag is stored at `*(_BYTE *)(v4 + 3480)`.

**II search algorithm**: The pipeliner starts at `MII = max(RecMII, ResMII)` and tries up to `pipeliner-ii-search-range` (default 10) consecutive II values. For each candidate, it attempts to place all instructions. The first II that produces a valid schedule wins.

**Error conditions** during pipelining:

| Condition | Error Message |
|---|---|
| MII == 0 | `"Invalid Minimal Initiation Interval: 0"` |
| MII > `pipeliner-max-mii` | `"Minimal Initiation Interval too large: MII > SwpMaxMii"` |
| Scheduling failure | `"Unable to find schedule"` |
| numStages == 0 | `"No need to pipeline - no overlapped iterations in schedule."` |
| numStages > `pipeliner-max-stages` | `"Too many stages in schedule: numStages > SwpMaxStages"` |

### Pipeliner Knobs

| Knob | Default | Description |
|---|---|---|
| `enable-pipeliner` | true | Enable Swing Modulo Scheduling |
| `pipeliner-max-mii` | 27 | Maximum allowed MII |
| `pipeliner-max-stages` | 3 | Maximum pipeline stages |
| `pipeliner-register-pressure` | false | Enable RP-aware pipelining |
| `pipeliner-register-pressure-margin` | 5 | Pressure margin for feasibility |
| `pipeliner-ignore-recmii` | (hidden) | Ignore recurrence MII |
| `pipeliner-ii-search-range` | 10 | Number of II candidates to try |

## ScheduleDAGMILive: Post-RA Instruction Ordering

`ScheduleDAGMILive` (`sub_355F610`) is the post-RA machine instruction scheduler. It takes the pipeliner's output (or standalone scheduling regions) and determines the final instruction order while respecting register pressure limits.

Data structures:

- **SUnit** (Scheduling Unit): 88 bytes per instruction, consistent across both the pipeliner and `ScheduleDAGMILive`.
- **Instruction-to-node hash map**: 632-byte entries per instruction. The unusually large entry size suggests extensive caching of per-instruction metadata (RP deltas, latency info, dependency edges) to avoid recomputation.
- **RP tracking structure**: 112 bytes, with per-register-class pressure arrays at offsets 32--48 (current) and 56--72 (limits).

The scheduling flow:

1. Initialize RP tracking via `sub_3551AB0` (if `pipeliner-register-pressure` is set).
2. Set per-class pressure defaults via `sub_2F60A40`.
3. Walk BB instruction list, build instruction-to-node hash map.
4. Compute ASAP (earliest cycle) via `sub_354BFF0` -> `v369`.
5. Compute ALAP (latest cycle) via `sub_354BFF0` -> `v373`.
6. Place instructions via `sub_354C3A0` (returns success/failure).
7. Calculate stage count: `(lastCycle - firstCycle) / II` = `(v84 - v80) / v88`.
8. Verify placement via `sub_355C7C0`.
9. Build stage descriptors via `sub_355D7E0` (80 bytes per stage, 10 QWORDs each).

### Instruction Selection Heuristic

The instruction selection heuristic (`sub_3557A10`) iterates 88-byte SUnit entries in the scheduling region and selects the next instruction to schedule based on a multi-level priority:

1. **Latency/depth** -- compare offset `+240`; deeper instructions are scheduled first.
2. **Target priority table** at `a1 + 3944` -- 16-byte entries containing `[start, end, priority, window_width]`.
3. **Schedule window width** -- narrower windows are preferred.

Pattern matching on ready instructions proceeds through `sub_35540D0` and `sub_35543E0`, with applicability validation via `sub_3546B80`. Ready queue management is handled by `sub_3553D90`. Latency recomputation occurs via `sub_2F8F5D0` during priority comparison. A hash table at `a1 + 3976` maps instructions to schedule nodes.

## Texture Group Merge

The Texture Group Merge pass (`sub_2DDE8C0`) groups texture load instructions that access related memory locations, enabling the hardware texture unit to coalesce them into fewer requests.

The algorithm:

1. Walk the BB instruction list.
2. Call `sub_2DDC600` to identify texture load candidates.
3. Hash candidates using Fibonacci hashing: `hash = (ptr * 0xBF58476D1CE4E5B9) >> shift`. The constant `0xBF58476D1CE4E5B9` is the 64-bit golden ratio hash, also used in Linux kernel hash tables and other LLVM components.
4. Group candidates into hash buckets.

Group table entries are 56 bytes (7 QWORDs) containing the key pointer, data pointer, and count/capacity fields. Group members are 32 bytes each: `[instruction, symbol, debug_info, scope_info]`. Generated group names carry a `.Tgm` suffix via `sub_2241490`.

The pass operates through a general instruction grouper framework (`sub_3147BA0`) with four registered callbacks:

| Callback | Function | Purpose |
|---|---|---|
| Candidate identification | `sub_2DDC600` | Detect texture loads |
| Group formation | `sub_2DDBF40` | Build groups from candidates |
| Merge execution | `sub_2DDB3F0` | Apply the merge |
| Cleanup | `sub_2DDB400` | Release temporary state |

## Scheduling Mode: The usedessa Knob

The `usedessa` knob (`dword_4FD26A0`, default 2) controls the scheduling pass pipeline configuration despite its name suggesting deSSA (de-Static Single Assignment) method selection. Pre-RA scheduling dispatches through `sub_2165850`; post-RA through `sub_21668D0`.

**Mode 1 (simple)**: Pre-RA scheduling is skipped entirely. Post-RA runs only `unk_4FCE24C` (the post-RA scheduler). This minimal configuration is useful for debugging or when scheduling is harmful to performance.

**Mode 2 (full, default)**: Pre-RA scheduling runs `unk_4FC8A0C`. Post-RA scheduling runs three passes sequentially:

1. `unk_4FC8A0C` -- pre-RA pass (disabled/noop in post-RA context).
2. `unk_4FCE24C` -- post-RA scheduler.
3. `unk_4FC9D8C` -- extra scheduling pass.

After scheduling completes, the framework prints `"After Machine Scheduling"`, optionally runs `sub_21F9D90`, then runs `unk_4FCAC8C` and prints `"After StackSlotColoring"`.

The "disabled" passes in mode 2 are registered but gated internally, allowing the framework to maintain a uniform pass list while selectively activating passes based on the current compilation phase.

## Cross-Cutting Observations

Register pressure tracking appears in three distinct places within the scheduling infrastructure, each serving a different consumer:

| Tracker | Consumer | Update Frequency |
|---|---|---|
| MRPA incremental (`sub_2E5A4E0`) | MCSE decisions | Per instruction move/elimination |
| ScheduleDAGMILive (`sub_355F610`) | Scheduling decisions | Per scheduling region |
| MachinePipeliner stage tracking | II feasibility | Per pipeline stage |

All three maintain per-register-class pressure arrays but with different granularities. The MRPA tracker uses incremental delta updates for efficiency; the scheduler computes ASAP/ALAP bounds per region; the pipeliner tracks pressure per modulo stage.

The DenseMap hash function `(ptr >> 9) ^ (ptr >> 4)` is shared across both the 32-bit value variant (`sub_1DFB9D0`) and 64-bit value variant (`sub_1DFB810`), indicating a common template instantiation pattern consistent with LLVM's `DenseMap<K, V>` template.
