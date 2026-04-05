# Scoreboards & Dependency Barriers

The NVIDIA GPU hardware uses a software-managed scoreboard system for instruction-level hazard tracking. Unlike CPUs that detect dependencies in hardware, NVIDIA's warp schedulers rely on per-instruction metadata -- encoded in a **control word** -- to determine when an instruction's operands are available, when a warp should yield, and which dependency barriers to set or wait on. ptxas generates this metadata in three pipeline phases (114--116) that together produce the final scoreboard annotations embedded in the SASS binary.

| | |
|---|---|
| **Phase 114** | `FixUpTexDepBarAndSync` -- texture dependency barrier fixup |
| **Phase 115** | `AdvancedScoreboardsAndOpexes` -- full scoreboard generation (-O1+) |
| **Phase 116** | `ProcessO0WaitsAndSBs` -- conservative scoreboard insertion (-O0) |
| **Control word generator** | `sub_A36360` (52 KB) -- per-instruction control word encoder |
| **Scheduling heuristic** | `sub_A23CF0` (54 KB) -- DAG list scheduler with dependency analysis |
| **Instruction dispatcher** | `sub_85C890` -- opcode-based fast-path / slow-path router |
| **Mercury opex pass** | `sub_6FFDC0` (66 KB) -- MercGenerateOpex, phase 120 |
| **HW barrier limit** | 6 dependency barriers per warp (hardware constraint) |

## Control Word Format

Every SASS instruction carries scheduling metadata in a control word. On sm_70+ architectures, the control word is packed into a dedicated **scheduling control instruction** that precedes each group of 3 real instructions. The control word encodes stall counts, yield hints, dependency barrier set/wait operations, and source operand reuse flags.

### Ori IR Control Word (Internal Representation)

Within ptxas, the control word is stored in the Ori IR instruction node at offsets +196 through +200. `sub_A36360` generates the fields, and per-field encoder functions write individual bit ranges.

The internal representation uses wider fields than the final SASS encoding to allow the encoder to track additional state during scoreboard computation:

| Field | Bits | Range | Description |
|-------|------|-------|-------------|
| Stall count | 4 | 0--15 | Minimum cycles to wait before issuing this instruction |
| Yield flag | 1 | 0--1 | Hint to warp scheduler: yield execution to another warp |
| Write barrier index | 3 | 0--5 | Which barrier register this instruction's result writes to |
| Read barrier mask | 6 | 0--63 | Bitmask of barriers this instruction must wait for (reads) |
| Wait barrier mask | 6 | 0--63 | Bitmask of barriers this instruction clears upon completion |
| Reuse flags | 6 | 0--63 | Per-source-operand register reuse cache hints |

Total: 26 bits of scheduling metadata per instruction in the internal representation.

### SASS Control Word (Binary Encoding)

In the final SASS binary, the control word is packed into 23 bits per instruction slot within a 128-bit scheduling control instruction. Three instruction slots share one control instruction, yielding a 4:3 instruction-to-encoding ratio.

```
128-bit scheduling control instruction:
  ┌─────────┬─────────┬─────────┬──────────────────┐
  │ Slot 0  │ Slot 1  │ Slot 2  │ Reserved / flags │
  │ 23 bits │ 23 bits │ 23 bits │    59 bits       │
  └─────────┴─────────┴─────────┴──────────────────┘

Per-slot 23-bit layout (sm_70+):
  bits [3:0]    Stall count (4 bits, values 0--15)
  bit  [4]      Yield flag (1 bit)
  bits [7:5]    Write barrier index (3 bits, values 0--5; 7 = none)
  bits [13:8]   Read barrier mask (6 bits, one-hot per barrier)
  bits [19:14]  Wait barrier mask (6 bits, one-hot per barrier)
  bits [22:20]  Reserved / extended flags (3 bits)
```

The reuse flags (6 bits per instruction) are encoded separately in the instruction word itself at architecture-defined bit positions, not in the scheduling control instruction.

### Bit-Field Diagram

```
  22  21  20  19  18  17  16  15  14  13  12  11  10   9   8   7   6   5   4   3   2   1   0
 ┌───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┐
 │  rsvd  │         wait mask         │        read mask          │  wr_bar   │ Y │  stall    │
 └───┴───┴───┴───┴───┴───┴───┴───┴───┴───┴───┴───┴───┴───┴───┴───┴───┴───┴───┴───┴───┴───┴───┘
```

## Hardware Dependency Barrier Model

NVIDIA GPUs (sm_70+) provide **6 dependency barrier registers** per warp, numbered 0 through 5. These are finite hardware resources managed entirely by software (ptxas). The barrier mechanism works as follows:

1. **Set barrier (write barrier)**: When an instruction with long latency (e.g., global memory load, texture fetch) is issued, the compiler assigns it a barrier index from the pool of 6. The hardware marks that barrier as "pending" and associates it with the instruction's completion.

2. **Wait on barrier (read barrier / wait mask)**: When a subsequent instruction needs the result of the long-latency operation, the compiler sets the corresponding bit in the wait mask. The warp scheduler stalls the instruction until the barrier clears.

3. **Barrier release**: When the long-latency operation completes, the hardware automatically clears the associated barrier register, allowing waiting instructions to proceed.

The key constraint is the hardware limit of 6 simultaneous barriers. If a basic block has more than 6 outstanding long-latency operations, the compiler must either:
- Reuse a barrier (wait for an earlier operation to complete before reassigning its barrier)
- Insert explicit stall cycles to serialize operations
- Use the DEPBAR instruction to manage barrier state programmatically

### Stall Count vs. Dependency Barriers

The stall count and dependency barriers serve complementary purposes:

| Mechanism | Latency Range | Use Case |
|-----------|---------------|----------|
| **Stall count** (4 bits) | 0--15 cycles | Short-latency operations: ALU (4--6 cycles), shared memory (20--30 cycles when stall is sufficient) |
| **Dependency barriers** | Arbitrary | Long-latency operations: global memory (200--800 cycles), texture (200--400 cycles), where stall count is insufficient |

For operations with latency <= 15 cycles, the stall count alone suffices. For longer latencies, a dependency barrier must be used because 4 bits cannot encode delays beyond 15 cycles. The yield flag provides an additional hint: when set, it tells the warp scheduler that this warp is about to stall and should be descheduled in favor of another ready warp.

## Phase 114: FixUpTexDepBarAndSync

Phase 114 runs a texture-specific fixup pass on dependency barriers and synchronization instructions. It operates through a double-indirect vtable dispatch: the architecture backend object contains a secondary scheduling/scoreboard subsystem object (at offset +16), which provides the actual implementation through its vtable at offset +0x70.

### Purpose

Texture operations have complex dependency patterns that the general scoreboard pass may not handle optimally:
- Texture fetches have variable latency depending on cache hit rates
- Texture coordinates and sampler state create additional dependencies
- The texture pipeline has its own internal buffering that interacts with scoreboard barriers

Phase 114 patches up the dependency barrier assignments made by the scheduler to account for these texture-specific requirements. It may:
- Reassign barrier indices for texture operations to avoid conflicts with non-texture barriers
- Insert additional DEPBAR instructions where texture dependencies require explicit barrier management
- Adjust synchronization instructions that interact with texture pipeline state

### Implementation

The phase is architecture-specific. In the default vtable, it maps to a nullsub (`nullsub_43` at `0x680170`), indicating a no-op for architectures that handle texture dependencies entirely in the general scoreboard pass. Architecture backends that need texture-specific fixup override this vtable entry with their implementation.

```
Dispatch path:
  PhaseManager::execute(phase=114)
    → arch_backend->vtable[phase_114]()
      → secondary_object = *(arch_backend + 16)
        → (*(secondary_object->vtable + 0x70))(secondary_object, func)
```

## Phase 115: AdvancedScoreboardsAndOpexes

Phase 115 is the main scoreboard generation pass. It is an `AdvancedPhase` hook -- a no-op in the default vtable, activated only when the architecture backend overrides it. At `-O1` and above, this phase runs the full dependency analysis and scoreboard assignment. At `-O0`, it is skipped entirely (phase 116 handles the conservative path instead).

### Architecture Dispatch

The phase entry point dispatches through the architecture backend vtable to `sub_85C890`, which acts as an opcode-aware router: depending on instruction type, it either handles the instruction via a fast path (direct barrier assignment for known patterns) or falls through to the full DAG list scheduler `sub_A23CF0`.

### Fast Path (sub_85C890)

`sub_85C890` classifies instructions by their masked opcode (`opcode & 0xFFFFCFFF`) and routes them:

**Handled by fast path** (direct barrier assignment without full DAG analysis):
- Opcodes 60, 62, 78, 79: Texture/surface operations -- processed via `sub_A22B40` (write barrier assignment) after checking architecture capability at vtable+1928
- Opcode 4 with operand types (7, 6): Specific ALU patterns with predicate operands -- dual operand processing via `sub_A220A0`
- Opcode 111 with operand types (7, 7, 6): Triple-source patterns -- processed via triple `sub_A220A0` calls
- Opcodes 120, 121: GMMA/tensor operations -- processed via `sub_A220A0` + `sub_A22B40` with variable operand counts
- Opcodes 126--128: Complex operations with architecture-specific operand counts (2--4 source operands)
- Opcodes 195, 270, 280, 281: Memory operations with specific addressing modes
- Opcodes 350, 351: Extended operations with operand subtype 11--12

**Fall-through to slow path** (full DAG scheduler):
- All other opcodes
- Fast-path opcodes that fail capability checks (vtable+1928 returns false)
- Instructions with the 0x1000 flag set (bit 12 of opcode word) -- handled via `sub_A227F0` first, then fall through

The fast-path check at vtable+1928 tests `(*(_BYTE *)(a1 + 1090) & 4) != 0`, which corresponds to an architecture feature flag controlling whether the backend supports direct scoreboard assignment for specific instruction classes.

### Slow Path: DAG List Scheduler (sub_A23CF0, 54 KB)

When the fast path cannot handle an instruction, `sub_A23CF0` performs full dependency-driven scoreboard assignment. This function takes 14 parameters including floating-point latency weights and throughput factors.

The scheduler:

1. **Classifies instruction dependencies**: Iterates operands, extracting register IDs from the operand descriptors at `instr+84`. For each operand, looks up the register object via `*(*(func+88) + 8 * (operand_desc & 0xFFFFFF))` and checks the register type at offset +64.

2. **Walks the def-use chain**: For each source operand, traces back to the defining instruction. Determines the dependency distance (in instructions and cycles) from each producer to the current consumer.

3. **Assigns barrier or stall**: Based on the dependency distance and the producer's latency:
   - If the producer's latency fits within the stall count range (0--15), assigns a stall count
   - If the latency exceeds 15 cycles, allocates a dependency barrier from the pool of 6
   - If all 6 barriers are in use, finds the oldest barrier, inserts a wait for it, and recycles it

4. **Handles instruction-specific patterns**: Contains a large switch on opcode for architecture-specific scheduling decisions. Opcodes handled specially include:
   - Opcodes 2, 3, 20, 21, 24, 28, 60, 61, 62, 67, 78, 79, 98, 100, 110, 120, 126, 139, 141, 162, 164, 201, 209, 210, 213, 214, 272, 273, 311: Direct operand processing with known dependency patterns
   - Opcodes 5, 6, 7, 10, 11, 36, 63, 80, 106, 108, 112, 114: Memory/load operations with variable-latency handling

5. **Produces control word fields**: After analysis, the function sets the barrier assignment, stall count, and wait mask for the instruction.

### Key Support Functions

| Address | Size | Purpose |
|---------|------|---------|
| `sub_A220A0` | 9 KB | Instruction attribute / property query -- fills a scheduling descriptor for a specific operand |
| `sub_A22B40` | -- | Write barrier assignment for a specific operand -- determines which barrier index to assign |
| `sub_A22BC0` | -- | Read barrier dependency -- sets up wait mask for operand |
| `sub_A22CE0` | -- | Instruction classification -- determines if instruction needs scoreboard processing |
| `sub_A231E0` | -- | Scheduling score computation -- determines if full DAG analysis is needed |
| `sub_A227F0` | -- | Pre-processing for flagged instructions (bit 12 set in opcode) |
| `sub_A22D00` | -- | Dependency distance computation |

## Phase 116: ProcessO0WaitsAndSBs

Phase 116 implements the conservative scoreboard insertion path for `-O0` (no optimization) builds. At `-O0`, phase 115 is a no-op, and phase 116 takes over with a simple, safe strategy.

### Conservative Strategy

The `-O0` path does not perform dependency analysis. Instead, it applies maximum-safety defaults:

```
function ProcessO0WaitsAndSBs(func):
    for each bb in func.basic_blocks:
        for each instr in bb.instructions:
            // Set maximum stall count (15 cycles)
            instr.stall_count = 15

            // Wait on all active barriers before every instruction
            instr.wait_mask = 0x3F    // all 6 barriers

            // No barrier assignment (no long-latency tracking)
            instr.write_barrier = 7   // 7 = none

            // No read barriers
            instr.read_mask = 0

            // Yield after every instruction
            instr.yield = 1
```

This produces correct but extremely slow code: every instruction waits the maximum time and clears all barriers, eliminating any possibility of instruction-level parallelism. The primary use case is debugging, where correctness matters more than performance.

At `-O1` and above, phase 115 runs the full analysis, and phase 116's `isNoOp()` returns true, skipping execution entirely.

## Control Word Generation Pipeline (sub_A36360)

`sub_A36360` (52 KB) is the master control word generator, called via vtable for each instruction in the scheduled order. It orchestrates six per-field encoder functions to produce the complete control word.

### Dispatch Architecture

The function takes the scheduling context (`a1`), the instruction node (`a2`), and several SIMD/float parameters encoding latency weights and architecture-specific constants. It begins by:

1. Loading the function context from `*(a1+8)` and the hardware profile from `*(func+1584)`
2. Calling `sub_7E1750` to classify the instruction
3. Extracting the opcode from `*(a2+72)` with the standard mask (`BYTE1 &= 0xCF`)
4. Switching on the masked opcode to determine the encoding strategy

### Per-Opcode Dispatch

The master switch at the entry of `sub_A36360` routes instructions by opcode class:

| Opcode Class | Handler | Description |
|---|---|---|
| 2, 3, 5, 7 | Inline (LABEL_18 path) | Standard ALU/memory with full barrier analysis. Checks operand subtype 9--10 and architecture feature at `*(hwprofile+1037) & 0x20`. Calls `sub_A32C70` for operand analysis, then `sub_A31040` for field encoding. |
| 6 | `sub_A34B70` | Wait barrier mask encoding for specific memory operations |
| 10, 149, 151, 290 | Inline (large block) | Extended operations with special barrier handling. Calls `sub_A32A20` for multi-operand setup, then processes register-type checks at offset +64 (type==5 triggers additional barrier logic). |
| All others | Per-field encoder chain | Default path through the six encoder functions |

### Per-Field Encoder Chain

For the default path, `sub_A36360` calls these encoders in sequence:

```
function GenerateControlWord(ctx, instr):
    // 1. Initialize operand analysis
    sub_7E19E0(&operand_info, ctx.func, instr)
    barrier_type = sub_7E53D0(instr.operand_subtype)

    // 2. Analyze source/dest operand dependencies
    sub_A32C70(&ctx, instr, src_idx, dst_idx,
               &dep_info, &barrier_info)

    // 3. Encode all control word fields
    sub_A31040(&ctx, &dep_info, &barrier_info,
               &src_desc, &dst_desc, &flags,
               barrier_type, ...)

    // 4. Finalize: set register space = 7 (done)
    *(ctx.func + 240) = 7

    // 5. Emit the control word
    sub_9253C0(ctx.func, instr, 1)
```

### Encoder Function Details

| Address | Size | Function | Field Encoded |
|---------|------|----------|---------------|
| `sub_A333A0` | 3 KB | EncodeStallAndYield | 4-bit stall count + 1-bit yield flag. Called twice from `sub_A36360`. Computes the minimum stall cycles from the dependency distance to the nearest consumer. Sets yield=1 when stall > threshold (architecture-dependent, typically 4+ cycles). |
| `sub_A33660` | 7 KB | EncodeReadBarrierMask | 6-bit read barrier mask. Determines which barrier registers this instruction must wait for before reading its source operands. Calls `sub_935720` to query register-barrier associations. |
| `sub_A342E0` | 9 KB | EncodeWriteBarrierIndex | 3-bit write barrier index. Allocates a barrier from the pool of 6 for this instruction's result. Calls `sub_934630` to find a free barrier; if none available, forces a wait on the oldest active barrier via `sub_9253C0`. |
| `sub_A34B70` | 10 KB | EncodeWaitBarrierMask | 6-bit wait barrier mask. Determines which barriers are cleared when this instruction completes. |
| `sub_A356A0` | 12 KB | EncodeScoreboardFields | Combined scoreboard field encoder. Orchestrates read/write barrier assignment with dependency distance tracking via `sub_A318F0` and conflict detection via `sub_A31390`. |
| `sub_A31F80` | 7 KB | ComputeReuseFlags | 6-bit reuse flags. Determines which source register values should be cached in the operand reuse buffer. Calls `sub_7DB310` for register bank analysis and `sub_91BF30` for reuse eligibility checking. |

### Supporting Analysis Functions

| Address | Size | Purpose |
|---------|------|---------|
| `sub_A318F0` | 4 KB | Barrier dependency distance computation -- measures the instruction distance between a barrier set and its corresponding wait |
| `sub_A31390` | 4 KB | Barrier set intersection / conflict detection -- checks whether two instructions' barrier usage conflicts |
| `sub_A32C70` | -- | Source/destination operand dependency analysis -- identifies which operands create dependencies |
| `sub_A31040` | -- | Master field encoding dispatcher -- coordinates all six per-field encoders |

## Dependency Barrier Allocation Algorithm

The barrier allocator manages the 6 hardware barrier registers as a resource pool. The algorithm must satisfy three constraints:
1. No two simultaneously-live long-latency operations share a barrier index
2. Every consumer instruction waits on the correct barrier before reading its operand
3. Barrier reuse is maximized to avoid unnecessary stalls

### Allocation State Machine

```
State per barrier register (6 entries):
  barrier[i].status     ∈ {FREE, PENDING, COMPLETED}
  barrier[i].producer   = instruction pointer (or NULL)
  barrier[i].set_cycle  = cycle when barrier was assigned
  barrier[i].consumers  = list of waiting instructions

State transitions:
  FREE → PENDING:     Barrier allocated to a long-latency producer
  PENDING → COMPLETED: Hardware signals completion (implicit)
  COMPLETED → FREE:    All consumers have executed their wait
  PENDING → FREE:      Forced recycle (all barriers in use, oldest evicted)
```

### Allocation Pseudocode

```
function AllocateBarrier(ctx, producer_instr):
    // 1. Try to find a free barrier
    for i in 0..5:
        if barrier[i].status == FREE:
            barrier[i].status = PENDING
            barrier[i].producer = producer_instr
            barrier[i].set_cycle = current_cycle
            return i

    // 2. No free barrier: recycle the oldest
    oldest = argmin(barrier[i].set_cycle for i in 0..5)

    // 3. Force all consumers of oldest to wait NOW
    InsertWaitForBarrier(ctx, oldest)

    // 4. Recycle
    barrier[oldest].status = PENDING
    barrier[oldest].producer = producer_instr
    barrier[oldest].set_cycle = current_cycle
    return oldest

function AssignWaitMask(ctx, consumer_instr):
    wait_mask = 0
    for each source_operand in consumer_instr:
        producer = FindProducer(source_operand)
        if producer.barrier_index != NONE:
            if producer.latency > stall_count_range:
                wait_mask |= (1 << producer.barrier_index)
    consumer_instr.wait_mask = wait_mask
```

### Barrier Reuse Heuristics

The allocator uses several heuristics to maximize barrier reuse:

1. **Oldest-first eviction**: When all 6 barriers are occupied, the oldest (earliest set_cycle) is evicted. This maximizes the chance that the evicted operation has already completed.

2. **Type affinity**: Texture operations preferentially reuse barriers previously assigned to other texture operations, because texture latencies tend to be similar and the texture pipeline may batch completions.

3. **Distance-based freeing**: A barrier is marked free without an explicit wait if the instruction distance from the producer exceeds the architecture's maximum latency for that operation class. The `sub_A318F0` function computes this distance.

4. **Conflict avoidance**: `sub_A31390` checks whether a proposed barrier assignment would conflict with an existing barrier that has not yet been waited on. If a conflict is detected, the allocator tries a different barrier index.

## Scoreboard Tracking State

The scoreboard tracking state is maintained in the scheduling context object. Key fields:

| Offset | Type | Content |
|--------|------|---------|
| `ctx+232` | QWORD | Current instruction pointer |
| `ctx+240` | DWORD | Current register space ID (7 = done) |
| `ctx+244` | QWORD | Current operand descriptor pair |
| `ctx+248` | DWORD | Current write barrier index |
| `ctx+252` | DWORD | Current barrier assignment type (1 or 2) |
| `ctx+264` | DWORD | Current instruction sequence number |
| `ctx+1040` | BYTE | Architecture feature flags (bit 5 = texture scoreboard, bit 4 = extended barriers) |
| `ctx+1090` | BYTE | Capability flags (bit 2 = fast-path scoreboard, bit 4 = extended operand tracking) |

The `*(ctx+1040) & 0x20` flag controls whether the architecture supports texture-specific scoreboard handling. The `*(ctx+1090) & 4` flag enables the fast-path scoreboard assignment for known instruction patterns.

## Stall Count Computation

The stall count is the minimum number of cycles the warp scheduler must wait before issuing the instruction. It is computed from the dependency distance to the instruction's producers.

### Algorithm

```
function ComputeStallCount(ctx, instr):
    max_stall = 0
    for each source_operand in instr:
        producer = FindProducer(source_operand)
        if producer == NULL:
            continue
        distance = instr.cycle - producer.cycle
        latency = GetLatency(producer.opcode, ctx.hw_profile)
        required_wait = latency - distance
        if required_wait > 0:
            max_stall = max(max_stall, required_wait)

    // Clamp to 4-bit range
    stall = min(max_stall, 15)

    // Apply architecture minimum (knob 741, default 3)
    stall = max(stall, ctx.min_stall)

    // Cap at architecture maximum (knob 805/806, max 16)
    stall = min(stall, ctx.max_stall)

    return stall
```

The stall count computation uses the hardware latency profile at `*(func+1584)` to look up per-opcode latencies. The latency tables are architecture-specific; see [Latency Model](latency-model.md).

### Yield Flag

The yield flag is computed by `sub_A333A0` alongside the stall count. The decision:

```
function ComputeYield(ctx, instr, stall):
    if stall >= yield_threshold:     // arch-dependent, typically 4
        return 1                      // long stall: yield to another warp
    if instr is branch/exit:
        return 1                      // control flow: always yield
    if instr.is_last_in_bb and next_bb.has_barrier:
        return 1                      // barrier boundary: yield
    return 0
```

The yield threshold is read from the hardware profile and varies by architecture. On sm_80+ it is typically 4 cycles.

## Mercury Opex Path (Phase 120)

In addition to the Ori IR scoreboard passes (phases 114--116), the Mercury backend has its own scoreboard generation pass: **MercGenerateOpex** (phase 120, `sub_703480` / `sub_6FFDC0`).

This pass runs after Mercury encode/decode (phase 117) and instruction expansion (phase 118), operating on the Mercury intermediate representation rather than Ori IR. It generates:
- DEPBAR instructions for explicit barrier management
- Scoreboard wait annotations
- Stall count adjustments for expanded instruction sequences
- Synchronization barriers for cross-warp dependencies

The Mercury opex pass and the Ori scoreboard passes serve different purposes:
- **Phases 114--116** generate scoreboard metadata at the Ori IR level, before Mercury encoding
- **Phase 120** generates additional scoreboard metadata for instructions introduced during Mercury expansion (pseudo-instruction expansion, WAR hazard insertion)
- The WAR pass must run twice (phases 119 and 121) because opex introduces new instructions that create additional write-after-read hazards

## Scheduling Output Encoding

After the control word fields are computed, the scheduling output pipeline (0x8F1EB0--0x8FDD60, ~57 KB) encodes them into the final SASS binary format.

### Encoding Pipeline

| Address | Size | Function | Purpose |
|---------|------|----------|---------|
| `sub_8F1EB0` | 15 KB | EncodeScheduleWords | Main scheduling output encoder -- iterates all instructions and produces control words |
| `sub_8F3130` | 1.0 KB | EncodeStallField | Packs 4-bit stall count into control word |
| `sub_8F31F0` | 6.1 KB | EncodeBarrierField | Packs barrier set/wait fields with architecture-specific layout |
| `sub_8F3650` | 2.7 KB | EncodeYieldField | Packs yield flag |
| `sub_8F3860` | 3.0 KB | EncodeScoreboardField | Packs scoreboard dependencies |
| `sub_8F3AB0` | 5.0 KB | EncodeDependencyField | Packs inter-instruction dependency metadata |
| `sub_8F3DE0` | 1.3 KB | EncodeControlField | Packs control flags |
| `sub_8F3EA0` | 2.1 KB | ValidateEncoding | Checks encoded control word for consistency |
| `sub_8F3FE0` | 1.7 KB | EncodeWaitField | Packs wait mask |
| `sub_8F4140` | 5.6 KB | EncodeFullControlWord | Combines all fields into the final 23-bit encoding |

### Emission

`sub_8F4510` (EmitControlWordForInstr) writes the packed control word into the output buffer. `sub_8F4820` (EmitControlBlock) constructs the complete 128-bit scheduling control instruction from three consecutive instruction slots.

### Scoreboard Entry Construction

`sub_8E4920` (6.9 KB) constructs a balanced BST (red-black tree) of scoreboard entries during schedule output. Each entry contains:
- Instruction pointer
- 16-bit scoreboard register ID
- 16-bit dependency type

The tree is used by the verification pass to check that barrier assignments are consistent across the instruction stream.

### Verification

Seven verification functions (0x8F7610--0x8F8CB0) validate the generated schedule:
1. Stall count bounds (0--15)
2. Barrier index validity (0--5 or 7=none)
3. Wait mask consistency (only wait on barriers that have been set)
4. Scoreboard dependency completeness (every long-latency producer has a barrier)
5. Control word format correctness
6. Yield hint plausibility
7. Overall schedule integrity (no live-range violations)

## Alternative Control Word Path (sub_8D7760)

`sub_8D7760` (41 KB) is the stall/barrier insertion function used by the pre-scheduling passes. Unlike `sub_A36360` which generates control words for the final Ori IR, `sub_8D7760` operates during the scheduling algorithm itself, computing stall and barrier assignments as instructions are placed.

This function:
- Manages a 32-entry barrier tracking table
- Contains architecture-variant switches for different barrier models (sm_70 vs sm_80 vs sm_90+)
- Computes stall cycles as the distance in cycles to the nearest dependent consumer
- Assigns barriers from the pool of 6 using an oldest-first eviction policy
- Handles architecture-specific barrier count variations

The two control word generators (`sub_A36360` for final emission, `sub_8D7760` for scheduling) share the same barrier allocation algorithm but operate at different pipeline stages. `sub_8D7760` produces preliminary assignments that `sub_A36360` may refine during the final scoreboard pass.

## Architecture-Specific Control Word Configuration

`sub_A2BD90` (23 KB) configures architecture-dependent scheduling parameters by querying feature flags through the architecture vtable at `*(ctx+72)`. Configuration includes:
- Stall count thresholds and caps
- Barrier count (6 for all current architectures, but the infrastructure supports variation)
- Reuse buffer policy
- Yield threshold
- Texture scoreboard behavior
- Extended barrier modes for sm_100+

The function queries multiple feature IDs through vtable dispatch, building an architecture profile that `sub_A36360` and its encoder chain use for all per-instruction decisions.

## Per-Instruction Control Word (Internal Structure)

Within the scheduling context, the control word is maintained at instruction offsets +196 through +200:

| Field | Offset | Bits | Description |
|-------|--------|------|-------------|
| Stall count | `*(instr+200)` bits [0:4] | 5 | Internal stall count (wider than SASS encoding to allow values up to 31 during optimization) |
| Extended stall | `*(instr+200)` bits [5:9] | 5 | Second stall field for dual-issue scheduling |
| Barrier flags | `*(instr+200)` bits [10:14] | 5 | Barrier control flags |
| Control bits | `*(instr+48)` bits [17:13] | 5 | Barrier format in Mercury encoding |
| Scoreboard flag | `*(instr+32)` byte 13 bit 2 | 1 | Instruction has scoreboard information |
| Encoding format | `*(instr+56)` | DWORD | 4 = barrier format in Mercury |
| Stall bits | `*(instr+168)` | BYTE | Final stall value for encoding |

The `sub_A2D340` (32 KB) function writes these fields through a large opcode switch, handling opcodes 50 (atomics), 73 (BAR), 74 (ST), 77 (LDS/STS), 78 (HMMA), and others with instruction-specific field layouts.

## Function Map

| Address | Size | Identity |
|---------|------|---------|
| `sub_85C890` | 1.5 KB | ScoreboardDispatcher -- opcode-based fast/slow path router |
| `sub_A220A0` | 9 KB | InstructionPropertyQuery -- scheduling descriptor filler |
| `sub_A22B40` | -- | WriteBarrierAssign -- barrier index assignment for operand |
| `sub_A22BC0` | -- | ReadBarrierAssign -- wait mask assignment for operand |
| `sub_A22CE0` | -- | InstructionClassify -- scoreboard processing classification |
| `sub_A22D00` | -- | DependencyDistance -- compute instruction distance |
| `sub_A227F0` | -- | FlaggedInstrPreprocess -- bit-12-set instruction handling |
| `sub_A231E0` | -- | SchedulingScore -- full-DAG-analysis necessity check |
| `sub_A23CF0` | 54 KB | **DAGListScheduler** -- full dependency-driven scoreboard |
| `sub_A265B0` | 10 KB | BarrierDependencyTracker -- barrier assignment tracking |
| `sub_A29220` | 12 KB | InstructionEmissionFilter -- instruction emission gating |
| `sub_A2BD90` | 23 KB | ArchControlWordConfig -- architecture-specific parameter loader |
| `sub_A2D340` | 32 KB | InstructionControlWordEncoder -- per-opcode field writer |
| `sub_A31040` | -- | MasterFieldEncoder -- coordinates per-field encoders |
| `sub_A31390` | 4 KB | BarrierConflictDetect -- barrier set intersection check |
| `sub_A318F0` | 4 KB | BarrierDistanceCompute -- dependency distance to barrier |
| `sub_A31F80` | 7 KB | ComputeReuseFlags -- operand reuse buffer hints |
| `sub_A32C70` | -- | OperandDependencyAnalysis -- source/dest dep extraction |
| `sub_A333A0` | 3 KB | EncodeStallAndYield -- 4-bit stall + 1-bit yield |
| `sub_A33660` | 7 KB | EncodeReadBarrierMask -- 6-bit read barrier mask |
| `sub_A342E0` | 9 KB | EncodeWriteBarrierIndex -- 3-bit write barrier index |
| `sub_A34B70` | 10 KB | EncodeWaitBarrierMask -- 6-bit wait barrier mask |
| `sub_A356A0` | 12 KB | EncodeScoreboardFields -- combined scoreboard encoder |
| `sub_A36360` | 52 KB | **GenerateControlWord** -- master control word generator |
| `sub_8D7760` | 41 KB | StallAndBarrierInsertion -- pre-scheduling control words |
| `sub_8E4920` | 6.9 KB | BuildScoreboardEntries -- scoreboard BST construction |
| `sub_8E5CA0` | 20 KB | EmitScheduleOutput -- control word output encoder |
| `sub_8F1EB0` | 15 KB | EncodeScheduleWords -- SASS control word output |
| `sub_8F4140` | 5.6 KB | EncodeFullControlWord -- 23-bit packing |
| `sub_A95DC0` | 36 KB | SASSControlWordEncoder -- architecture-dispatched encoder |
| `sub_6FFDC0` | 66 KB | MercOpexBody -- Mercury opex scoreboard generation |
| `sub_703480` | 1.4 KB | RunOpexPass -- MercGenerateOpex entry |

## Cross-References

- [Scheduler Overview](overview.md) -- 3-phase scheduler architecture, scheduling output pipeline
- [Scheduling Algorithm](algorithm.md) -- priority list scheduling, dependency DAG construction
- [Latency Model](latency-model.md) -- per-opcode latency tables used by stall count computation
- [Mercury Encoder](../codegen/mercury.md) -- Mercury pipeline including MercGenerateOpex (phase 120)
- [SASS Encoding](../codegen/encoding.md) -- instruction encoding format including control word bit layout
- [Phase Manager](../passes/phase-manager.md) -- how phases 114--116 fit in the 159-phase pipeline
- [Sync & Barriers](../passes/sync-barriers.md) -- software synchronization barriers (distinct from dependency barriers)
- [Knobs](../config/knobs.md) -- scheduling knobs 741 (stall threshold), 805/806 (stall caps)
