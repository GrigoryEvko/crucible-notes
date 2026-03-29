# Register Allocation

NVPTX register allocation in CICC v13.0 operates under a fundamentally different model from CPU targets. PTX has no fixed physical register file -- registers are virtual (`%r0`, `%r1`, `%f0`, ...) and the hardware scheduler maps them to physical resources at launch time. The "physical register" concept in LLVM's greedy allocator maps to register pressure constraints rather than actual hardware registers, making the allocator pressure-driven rather than assignment-driven. The primary constraint is the `-maxreg` limit (default 70), which bounds total live registers across all classes to control occupancy on the SM.

| | |
|---|---|
| **Greedy RA driver** | `sub_2F5A640` (466 lines) |
| **selectOrSplit core** | `sub_2F49070` (82KB, 2,314 lines) |
| **Live range splitting** | `sub_2F2D9F0` (93KB, 2,339 lines) |
| **Register coalescing** | `sub_2F71140` (80KB, 2,190 lines) |
| **Register info init (new)** | `sub_30590F0` |
| **Register info init (old)** | `sub_2163AB0` |
| **Allocation failure handler** | `sub_2F418E0` |

## Dual Greedy RA Instances

CICC contains two complete copies of the Greedy Register Allocator infrastructure, corresponding to the legacy and new LLVM pass managers:

- **Instance A** (legacy, `0x1EC0400` region): registered through the old pass manager pipeline.
- **Instance B** (new, `0x2F4C2E0` region): registered through `sub_2F504C0` as the factory function.

Both are registered under the pass name "Greedy Register Allocator" via `RAGreedyPass` (`sub_2342890`). The `selectOrSplit` entry point at `sub_2F4BAF0` is a thin wrapper that redirects to `sub_2F49070(this + 200, ...)`. A separate entry at `sub_2F4BB00` handles the spill-or-split path with `SplitEditor` integration.

## NVPTX Register Classes

CICC defines nine register classes plus one internal-only class. Each class is identified by a vtable pointer and encodes virtual registers as 32-bit values with a 4-bit class tag in bits `[31:28]` and a 28-bit register index in bits `[27:0]`:

| Vtable | PTX Prefix | Class | Encoded Bits | Description |
|---|---|---|---|---|
| `off_4A027A0` | `%p` | `Int1Regs` | `0x10000000` | 1-bit predicate |
| `off_4A02720` | `%qs` | `Int16Regs` | `0x20000000` | 16-bit integer |
| `off_4A025A0` | `%r` | `Int32Regs` | `0x30000000` | 32-bit integer |
| `off_4A024A0` | `%qd` | `Int64Regs` | `0x40000000` | 64-bit integer |
| `off_4A02620` | `%f` | `Float32Regs` | `0x50000000` | 32-bit float |
| `off_4A02520` | `%fd` | `Float64Regs` | `0x60000000` | 64-bit float |
| `off_4A02760` | `%h` | `Float16Regs` | `0x70000000` | 16-bit float |
| `off_4A026A0` | `%fh` | `Float16x2Regs` | `0x80000000` | packed 2xf16 |
| `off_4A02460` | `%rq` | `SpecialRegs` | `0x90000000` | special/env regs |

The classes are completely disjoint -- there is no cross-class interference. Each type lives in its own namespace: integer 32-bit values occupy `%r` registers, 32-bit floats occupy `%f` registers, and so on. The sign bit (`0x80000000`) distinguishes physical from virtual registers in LLVM's internal convention.

Copy instructions are class-specific. Each register class has both a same-class copy opcode and a cross-class copy opcode (from `sub_2162350`):

| Class | Same-Class Opcode | Cross-Class Opcode |
|---|---|---|
| `Int32Regs` | 39552 | 10816 |
| `Int64Regs` | 39680 | 11008 |
| `Float32Regs` | 30656 | 10880 |
| `Float64Regs` | 30784 | 11072 |
| `Float16Regs` | 30528 | 10688 |

Classes like `Int1Regs`, `Int16Regs`, `Float16x2Regs`, and `SpecialRegs` use identical opcodes for both same-class and cross-class copies, reflecting the absence of cross-class movement paths for these types.

## Greedy selectOrSplit

The core allocation algorithm (`sub_2F49070`, 82KB) follows LLVM's standard `RAGreedy::selectOrSplit` structure with NVPTX-specific adaptations for pressure-driven allocation.

**Initialization** (lines 381--484): The function loads the register count from `TargetRegisterInfo` (offset `+44`), allocates a `RegUnitStates` array at `this + 1112` (4 bytes per register), sets up a bitvector at `this + 736` for live-through registers, and initializes an interference cache at `this + 648` as an open-addressing hash map.

The interference cache uses hash function `37 * reg` (modulo table size) with sentinel values `-1` (empty) and `-2` (tombstone). The growth policy triggers at 75% load factor: `4 * (count + 1) >= 3 * capacity`.

**Operand scanning** (lines 690--1468) iterates each operand in the live range's segment list (40-byte stride per operand). Operand type byte at offset `+0` classifies entries: 0 = virtual register, 12 = regmask. Physical registers are marked in a reserved bitvector; virtual registers check copyability and tied-ness.

**Interference processing** (lines 714--955) calls `sub_2F43DC0` to scan interferences, then for each conflict decides between eviction (`sub_2F48CE0` for constrained operands) and simple assignment (`sub_2F47B00`).

**Copy coalescing hints** (lines 1060--1163) detect COPY-like operands (kinds 20 and 21). For kind 21 with a parent flag, the allocator chains through parent live ranges via `sub_2F41240` to record coalescing opportunities tracked at `this + 832`.

## Live Range Splitting

The splitting engine (`sub_2F2D9F0`, 93KB) implements `RAGreedy::splitAroundRegion` with `SplitAnalysis` integration. For each live range segment in the worklist at `this + 320`:

1. **Hash table initialization** -- clears and resizes a region-local hash table with 16-byte entries per tracked register.
2. **Segment enumeration** -- iterates the segment linked list (40-byte stride), checking gap flags (bit 2 of byte `[0]`) and sub-range flags (bit 3 of byte `[44]`).
3. **Copy hint detection** -- for COPY instructions (kinds 68 and 0), extracts register pairs and builds a conflict set. Calls `sub_2F2A2A0` for local split attempts; on success, `sub_2FDF330` materializes the new segments.
4. **Interference analysis** -- for non-COPY segments, scans operands and uses `_bittest` on regmask data at offset `+24` to find registers killed by masks. Killed entries are tombstoned in the tracking hash table.
5. **Coalescing / reassignment** -- dispatches through vtable offsets `[1064]` (tryReassign) and `[1072]` (canRecolorVirtReg); on success, marks the register used via `sub_2E88E20`.

## Register Pressure and the maxreg Constraint

The real allocation constraint on NVPTX is not register scarcity but register pressure. Each SM has a fixed register file shared among all active warps. Higher register usage per thread reduces occupancy (the number of concurrent warps), directly impacting throughput. The `-maxreg` CLI flag (parsed at `sub_900130`, stored at compilation context offset `+1192`) caps the total live register count.

Duplicate `-maxreg` definitions produce the error: `"libnvvm : error: -maxreg defined more than once"` (`sub_9624D0`).

NVIDIA provides an extensive set of custom rematerialization knobs to reduce pressure below the target threshold:

| Knob | Default | Description |
|---|---|---|
| `nv-remat-default-max-reg` | 70 | Default maximum register target |
| `nv-remat-max-times` | 10 | Max rematerialization iterations |
| `nv-remat-block-single-cost` | 10 | Single live pull-in cost limit |
| `nv-remat-block-max-cost` | 100 | Max clone cost for reducing one live |
| `nv-remat-block-loop-cost-factor` | 20 | Loop body cost scaling factor |
| `nv-remat-block-liveout-min-percentage` | 70 | Minimum live-out percentage for block remat |
| `nv-remat-block-map-size-limit` | 6 | Map size limit for block-level remat |
| `nv-remat-block-load-cost` | 10 | Load cost in Remat Machine Block |
| `nv-remat-threshold-for-spec-reg` | 20 | Threshold for special register remat |
| `load-remat` | (flag) | Enable load rematerialization |
| `no-mi-remat` | (flag) | Disable MI remat for specific functions |

The greedy allocator itself has additional tuning knobs:

| Knob | Default | Description |
|---|---|---|
| `split-spill-mode` | 1 | 0=default, 1=size, 2=speed |
| `lcr-max-depth` | 5 | Last chance recoloring max depth |
| `lcr-max-interf` | 8 | Last chance recoloring max interferences |
| `exhaustive-register-search` | (flag) | Bypass LCR depth/interference cutoffs |
| `enable-deferred-spilling` | (flag) | Defer spill code to end of allocation |
| `grow-region-complexity-budget` | 10000 | `growRegion()` edge budget |
| `split-threshold-for-reg-with-hint` | 75 | Split threshold percentage |

Additional rematerialization knobs registered separately include `do-remat` (default 3), `remat-maxreg-ceiling` (default 0), `remat-single-cost-limit` (default 6000), `remat-loop-trip` (default 20), and `remat-for-occ` (default 120, targeting higher occupancy).

## The RAGreedy Driver

The top-level driver (`sub_2F5A640`) orchestrates the full allocation pass:

1. Store `MachineFunction` at `a1[96]`, retrieve `SubTarget` (vtable `+128`).
2. Optional debug dump: `"Before greedy register allocator"`.
3. `sub_35B4B20` -- calculate register class info.
4. `sub_2F55040` -- check if any virtual registers need allocation.
5. `sub_2FAD5E0` -- setup spill costs.
6. `sub_2F54D60` -- compute live intervals.
7. Query vtable `+328` for `getRegPressureSetLimit` (stored at `a1[3633]`).
8. Look up `EvictionAdvisor` (`dword_5023BA8`) and `PriorityAdvisor` (`dword_5023AC8`) via `std::map` lookups.
9. Initialize advisors via vtable `[24]`.
10. Allocate `InterferenceCache` (0x2C0 bytes, `sub_2FB0E40`).
11. Allocate `SplitAnalysis` (0x738 bytes, `sub_2FB1ED0`).
12. `sub_3501A90` -- setup `RegAllocMatrix`.
13. Initialize `PhysRegEntries` array (32 entries, 144-byte stride).
14. `sub_2F55730` -- reset priority queue.
15. `sub_35B5380` -- seed queue from virtual registers.
16. `sub_2F58C00` -- main allocation loop.
17. Optional debug dump: `"Before post optimization"`.
18. Post-allocation optimization via vtable `[24]`.
19. `sub_2F5A580`, `sub_2F50510` -- finalize.

## Allocation Failure

When physical register assignment fails (`sub_2F418E0`), three error paths exist:

- **Empty allocation order**: `"no registers from class available to allocate"` -- the register class has zero allocatable registers. Diagnostic emitted via `sub_B6EB20`.
- **All registers occupied**: `"ran out of registers during register allocation"` -- the eviction/split pipeline exhausted all options. Uses `sub_B2BE50` for source location and `sub_B157E0` / `sub_B158E0` for diagnostic formatting.
- **Inline assembly overflow**: `"inline assembly requires more registers than available"` -- special handling for inline asm operands (kind values 1--2 at offset `+68`).

The `FailedRegAlloc` flag (bit 10 in `MachineFunction` properties, `sub_2E78A80`) is set to allow downstream passes to handle the failure gracefully rather than crashing.

## Architectural Uniqueness

NVPTX's register allocation differs from all other LLVM targets in several fundamental ways:

- **Unlimited virtual registers**: PTX has no fixed register count. The allocator manages pressure, not assignment to a finite set of physical registers.
- **Complete class separation**: The nine register classes are fully disjoint. An `Int32Regs` allocation never conflicts with a `Float32Regs` allocation.
- **Pressure as the primary constraint**: The `-maxreg` ceiling and NVIDIA's custom rematerialization infrastructure (`nv-remat-*` knobs) exist specifically to control occupancy, which has no equivalent in CPU register allocation.
- **Dual implementation**: Two complete RA copies exist (old at `0x1E*`--`0x1F*`, new at `0x2F*`--`0x35*`), one per pass manager generation.
