# Scoreboards & Dependency Barriers

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The NVIDIA GPU hardware uses a software-managed scoreboard system for instruction-level hazard tracking. Unlike CPUs that detect dependencies in hardware, NVIDIA's warp schedulers rely on per-instruction metadata — encoded in a **control word** — to determine when an instruction's operands are available, when a warp should yield, and which dependency barriers to set or wait on. ptxas generates this metadata in three pipeline phases (114–116) that together produce the final scoreboard annotations embedded in the SASS binary.

| | |
|---|---|
| **Phase 114** | `FixUpTexDepBarAndSync` — texture dependency barrier fixup |
| **Phase 115** | `AdvancedScoreboardsAndOpexes` — full scoreboard generation (-O1+) |
| **Phase 116** | `ProcessO0WaitsAndSBs` — conservative scoreboard insertion (-O0) |
| **Control word generator** | `sub_A36360` (52 KB) — per-instruction control word encoder |
| **Scheduling heuristic** | `sub_A23CF0` (54 KB) — DAG list scheduler with dependency analysis |
| **Instruction dispatcher** | `sub_85C890` — opcode-based fast-path / slow-path router |
| **Mercury opex pass** | `sub_6FFDC0` (66 KB) — MercGenerateOpex, phase 120 |
| **HW barrier limit** | 6 dependency barriers per warp (hardware constraint) |

## Control Word Format

Every SASS instruction carries scheduling metadata in a control word. On sm_70+ architectures, the control word is packed into a dedicated **scheduling control instruction** that precedes each group of 3 real instructions. The control word encodes stall counts, yield hints, dependency barrier set/wait operations, and source operand reuse flags.

### Ori IR Control Word (Internal Representation)

Within ptxas, the control word is stored in the Ori IR instruction node at offsets +196 through +200. `sub_A36360` generates the fields, and per-field encoder functions write individual bit ranges.

The internal representation uses wider fields than the final SASS encoding to allow the encoder to track additional state during scoreboard computation:

| Field | Bits | Range | Description |
|-------|------|-------|-------------|
| Stall count | 4 | 0–15 | Minimum cycles to wait before issuing this instruction |
| Yield flag | 1 | 0–1 | Hint to warp scheduler: yield execution to another warp |
| Write barrier index | 3 | 0–5 | Which barrier register this instruction's result writes to |
| Read barrier mask | 6 | 0–63 | Bitmask of barriers this instruction must wait for (reads) |
| Wait barrier mask | 6 | 0–63 | Bitmask of barriers this instruction clears upon completion |
| Reuse flags | 6 | 0–63 | Per-source-operand register reuse cache hints |

Total: 26 bits of scheduling metadata per instruction in the internal representation.

### SASS Control Word (Binary Encoding)

In the final SASS binary, the control word is packed into 23 bits per instruction slot within a 128-bit scheduling control instruction. Three instruction slots share one control instruction, yielding a 4:3 instruction-to-encoding ratio. (MED — per-field widths confirmed against `sub_A36360` encoder shifts; the 3-slot packing layout is inferred from cuobjdump output and the encoder loop structure rather than a single literal in the binary.)

```text
128-bit scheduling control instruction:
  ┌─────────┬─────────┬─────────┬──────────────────┐
  │ Slot 0  │ Slot 1  │ Slot 2  │ Reserved / flags │
  │ 23 bits │ 23 bits │ 23 bits │    59 bits       │
  └─────────┴─────────┴─────────┴──────────────────┘

Per-slot 23-bit layout (sm_70+):
  bits [3:0]    Stall count (4 bits, values 0–15)
  bit  [4]      Yield flag (1 bit)
  bits [7:5]    Write barrier index (3 bits, values 0–5; 7 = none)
  bits [13:8]   Read barrier mask (6 bits, one-hot per barrier)
  bits [19:14]  Wait barrier mask (6 bits, one-hot per barrier)
  bits [22:20]  Reserved / extended flags (3 bits)
```

The reuse flags (6 bits per instruction) are encoded separately in the instruction word itself at architecture-defined bit positions, not in the scheduling control instruction.

### Bit-Field Diagram

```text
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
| **Stall count** (4 bits) | 0–15 cycles | Short-latency operations: ALU (4–6 cycles), shared memory (20–30 cycles when stall is sufficient) |
| **Dependency barriers** | Arbitrary | Long-latency operations: global memory (200–800 cycles), texture (200–400 cycles), where stall count is insufficient |

For operations with latency <= 15 cycles, the stall count alone suffices. For longer latencies, a dependency barrier must be used because 4 bits cannot encode delays beyond 15 cycles. The yield flag provides an additional hint: when set, it tells the warp scheduler that this warp is about to stall and should be descheduled in favor of another ready warp.

## Phase 114: FixUpTexDepBarAndSync

Phase 114 performs a pre-scoreboard fixup of dependency barriers for texture fetch instructions. It runs *before* the main scoreboard pass (phase 115), correcting barrier state that the instruction scheduler (phases 97–110) left inconsistent with texture pipeline requirements. The dispatch is doubly-indirect through a scheduling/scoreboard subsystem object owned by the architecture backend (`arch_backend+16 -> vtable slot 14`). See [sync-barriers.md](../passes/sync-barriers.md#phase-114----fixuptexdepbarandsync) for the full algorithm, per-SM scoreboard configuration table, and dispatch pseudocode.

### Summary

Texture fetches (Ori opcodes 60, 62, 78, 79) have latencies of 200–400+ cycles, exceeding the 4-bit stall-count range. Phase 91 (`OriCalcDependantTex`) pre-computes texture dependency metadata; phase 114 consumes it to validate write-barrier indices, wait-masks on consumers, and stall/yield settings. The per-SM scoreboard configs use a threshold of 56 cycles uniformly, with sm_100 (Blackwell) supporting up to 6 simultaneous scoreboard triplets per scheduling class — the primary motivation for this pass. The default vtable entry is `nullsub_43` (`0x680170`), making the pass a no-op on architectures that fold texture barrier fixup into phase 115.

## Phase 115: AdvancedScoreboardsAndOpexes

Phase 115 is the main scoreboard generation pass. It is an `AdvancedPhase` hook — a no-op in the default vtable, activated only when the architecture backend overrides it. At `-O1` and above, this phase runs the full dependency analysis and scoreboard assignment. At `-O0`, it is skipped entirely (phase 116 handles the conservative path instead).

### Architecture Dispatch

The phase entry point dispatches through the architecture backend vtable to `sub_85C890`, which acts as an opcode-aware router: depending on instruction type, it either handles the instruction via a fast path (direct barrier assignment for known patterns) or falls through to the full DAG list scheduler `sub_A23CF0`.

### Fast Path (sub_85C890)

`sub_85C890` classifies instructions by their masked opcode (`opcode & 0xFFFFCFFF`) and routes them:

**Handled by fast path** (direct barrier assignment without full DAG analysis):
- Opcodes 60, 62, 78, 79: Texture/surface operations — processed via `sub_A22B40` (write barrier assignment) after checking architecture capability at vtable+1928
- Opcode 4 with operand types (7, 6): Specific ALU patterns with predicate operands — dual operand processing via `sub_A220A0`
- Opcode 111 with operand types (7, 7, 6): Triple-source patterns — processed via triple `sub_A220A0` calls
- Opcodes 120, 121: GMMA/tensor operations — processed via `sub_A220A0` + `sub_A22B40` with variable operand counts
- Opcodes 126–128: Complex operations with architecture-specific operand counts (2–4 source operands)
- Opcodes 195, 270, 280, 281: Memory operations with specific addressing modes
- Opcodes 350, 351: Extended operations with operand subtype 11–12

**Fall-through to slow path** (full DAG scheduler):
- All other opcodes
- Fast-path opcodes that fail capability checks (vtable+1928 returns false)
- Instructions with the 0x1000 flag set (bit 12 of opcode word) — handled via `sub_A227F0` first, then fall through

The fast-path check at vtable+1928 tests `(*(_BYTE *)(a1 + 1090) & 4) != 0`, which corresponds to an architecture feature flag controlling whether the backend supports direct scoreboard assignment for specific instruction classes.

### Slow Path: DAG List Scheduler (sub_A23CF0, 54 KB)

When the fast path cannot handle an instruction, `sub_A23CF0` performs full dependency-driven scoreboard assignment. This function takes 14 parameters including floating-point latency weights and throughput factors.

The scheduler:

1. **Classifies instruction dependencies**: Iterates operands, extracting register IDs from the operand descriptors at `instr+84`. For each operand, looks up the register object via `*(*(func+88) + 8 * (operand_desc & 0xFFFFFF))` and checks the register type at offset +64.

2. **Walks the def-use chain**: For each source operand, traces back to the defining instruction. Determines the dependency distance (in instructions and cycles) from each producer to the current consumer.

3. **Assigns barrier or stall**: Based on the dependency distance and the producer's latency:
   - If the producer's latency fits within the stall count range (0–15), assigns a stall count
   - If the latency exceeds 15 cycles, allocates a dependency barrier from the pool of 6
   - If all 6 barriers are in use, finds the oldest barrier, inserts a wait for it, and recycles it

4. **Handles instruction-specific patterns**: Contains a large switch on opcode for architecture-specific scheduling decisions. Opcodes handled specially include:
   - Opcodes 2, 3, 20, 21, 24, 28, 60, 61, 62, 67, 78, 79, 98, 100, 110, 120, 126, 139, 141, 162, 164, 201, 209, 210, 213, 214, 272, 273, 311: Direct operand processing with known dependency patterns
   - Opcodes 5, 6, 7, 10, 11, 36, 63, 80, 106, 108, 112, 114: Memory/load operations with variable-latency handling

5. **Produces control word fields**: After analysis, the function sets the barrier assignment, stall count, and wait mask for the instruction.

#### Stall-vs-Barrier Decision Algorithm

The core decision lives in `sub_A220A0` (9 KB), called once per source operand that requires dependency tracking. This is the single most critical algorithm in the scoreboard pass — it decides whether a dependency is expressed as a stall count in the control word or as a barrier register allocation with a wait bit.

```c
function AssignOperandScoreboard(backend, func, instr, operand_idx, insert_pt, sched_state):
    operand_slot = &instr.operands[operand_idx]          // instr+84 + 8*idx
    operand_desc = *operand_slot                          // 32-bit packed descriptor
    desc_type    = (operand_desc >> 28) & 7               // 3-bit type field
    is_write_dep = operand_slot.byte7 & 1                 // high byte bit 0

    // ---- Gate 1: skip non-trackable registers ----
    if desc_type == 1 and not is_write_dep:
        reg_id   = operand_desc & 0xFFFFFF
        reg_obj  = func.reg_table[reg_id]                 // *(*(func+88) + 8*reg_id)
        reg_type = *(reg_obj + 64)
        if reg_type == 3 or reg_id == 41:                 // predicate reg or RZ
            return                                         // no scoreboard needed

    func.last_sched_ip   = instr.addr                     // *(func+232)
    func.last_sched_line = instr.line                     // *(func+264)

    // ---- Path A: non-register or write-dependent operand ----
    if desc_type != 1 or is_write_dep:
        dep_class = ClassifyOperand(instr, operand_idx)   // sub_91E7A0
        producer  = FindProducer(func, instr, operand_idx, &dep_class)  // sub_13A48E0

        // Try barrier tracker cache (avoids redundant allocation)
        if sched_state.has_extended_tracking:              // *(sched_state+24)
            cached = TryBarrierCacheLookup(sched_state, instr, operand_idx)
        else:
            cached = TryBarrierCacheLookup(sched_state+72, instr, operand_idx)
        if cached: rewrite operand_slot with cached barrier ref; return

        // Write-after-write reuse: check bits 25-26 of operand high word
        if operand_slot.high & 0x6000000:
            pipe_unit = backend.vtable[904](backend, dep_class)
            if pipe_unit == 4: dep_class = 6              // force class 6 for special units
            else:              dep_class = MapUnitToClass(pipe_unit)  // sub_7D6890

        // Query dependency distance via sub_92E800 -> sub_92C240
        //   Packs (dep_class & 0xFFFFFF) | 0x90000000 as a distance query
        //   Opcode 0x82 = overlap test against producer's live range
        StoreFlaggedProducer(func, instr)                 // *(func+240..252)
        LookupScoreboardConfig(&config, func, 3)          // sub_91BF30, 3 = read dep
        stall_result = QueryDepDistance(                   // sub_92E800
            func, 0x82, dep_class, config, &producer_desc)

        // Rewrite operand slot: replace virtual reg with barrier reg reference
        operand_slot.low  = (stall_result & 0xFFFFFF) | 0x10000000
        operand_slot.high &= 0xFEC00000
        func.sched_marker = 7                             // *(func+240)
        if *insert_pt == instr.next: *insert_pt = instr.prev
        return

    // ---- Path B: already-defined register operand ----
    dep_distance = ComputeDepDistance(func, instr, operand_idx)  // sub_91E610
    //
    // sub_91E610 returns distance by register type:
    //   reg_type 4 (constant bank)  --> 26  (always exceeds MAX_STALL -> barrier)
    //   reg_type 5 or 2 (UReg/SReg) --> 20  (always exceeds MAX_STALL -> barrier)
    //   Same-block short-circuit     --> 10  (may fit in stall if close enough)
    //   General case: sub_91A0F0(opcode, extra, operands, count, idx)
    //     walks the operand chain computing actual cycle distance

    LookupScoreboardConfig(&config, func, 3)
    stall_count = config[0]
    StoreFlaggedProducer(func, instr)

    // sub_92E760 -> sub_92C240 is the unified stall/barrier encoder:
    //   Packs (stall_count & 0xFFFFFF) | 0x90000000
    //   Opcode 0xB0 = stall encoding query
    //   sub_92C240 makes the final decision:
    //     if result fits 4-bit stall field [0..15] -> stall count in control word
    //     if result > 15 -> delegates to AllocateBarrier (see Allocation Pseudocode)
    EncodeStallOrBarrier(result, func, dep_distance, stall_count, operand_slot)

    *(func.last_sched_ip + 88) |= 0x800000               // mark producer as tracked
    func.sched_marker = 7
    operand_slot.low = (stall_count & 0xFFFFFF) | (operand_desc & 0xFF000000)
    return
```

**Decision summary.** For every source operand, the algorithm computes the distance to the producing instruction and selects one of three outcomes:

| Condition | Action | Control word encoding |
|-----------|--------|----------------------|
| `dep_distance <= 15` (MAX_STALL) | Stall: pipeline bubbles absorb the latency | 4-bit `stall_count` field, no barrier used |
| `dep_distance > 15` and free barrier exists | Barrier: allocate from pool of 6 | `wr_barrier` (3 bits) + `wait_mask` bit on consumer |
| `dep_distance > 15` and all 6 occupied | Recycle: evict oldest, insert DEPBAR.LE wait | DEPBAR inserted before consumer + barrier reused |

**Operand slot selection.** When an instruction has two candidate operand slots (e.g., dual-source ALU), `sub_13A5D50` (extended tracker) or `sub_13A4DA0` (basic tracker) selects which slot receives the barrier. Both walk the instruction chain backward up to `*(tracker+28)` instructions, matching operand descriptors with a 25-bit mask `(desc_a ^ desc_b) & 0x1FFFFFF` and preferring the slot whose producer appears earlier. This ensures the barrier covers the longer-latency dependency while the shorter one can be absorbed as a stall.

**Per-SM parameters** from `per_sm_scoreboard_configs.json`: each architecture provides up to 75 entries of 88-byte scoreboard configuration records. Each record holds up to 6 triplets of `(scoreboard_id, threshold, mask)` where `threshold` (typically 56) is the instruction-distance cutoff for barrier freeing, and `mask` (-1 = all register classes) controls tracking scope.

### Key Support Functions

| Address | Size | Purpose |
|---------|------|---------|
| `sub_A220A0` | 9 KB | Instruction attribute / property query — fills a scheduling descriptor for a specific operand |
| `sub_A22B40` | — | Write barrier assignment for a specific operand — determines which barrier index to assign |
| `sub_A22BC0` | — | Read barrier dependency — sets up wait mask for operand |
| `sub_A22CE0` | — | Instruction classification — determines if instruction needs scoreboard processing |
| `sub_A231E0` | — | Scheduling score computation — determines if full DAG analysis is needed |
| `sub_A227F0` | — | Pre-processing for flagged instructions (bit 12 set in opcode) |
| `sub_A22D00` | — | Dependency distance computation |

## Phase 116: ProcessO0WaitsAndSBs

Phase 116 implements the conservative scoreboard insertion path for `-O0` (no optimization) builds. At `-O0`, phase 115 is a no-op, and phase 116 takes over with a simple, safe strategy.

### Conservative Strategy

The `-O0` path does not perform dependency analysis. Instead, it applies maximum-safety defaults:

```c
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

1. Loading the function context from `*(a1+8)` and the SM backend from `*(func+1584)` (the `sm_backend` field; provides hardware latency profiles)
2. Calling `sub_7E1750` to classify the instruction
3. Extracting the opcode from `*(a2+72)` with the standard mask (`BYTE1 &= 0xCF`)
4. Switching on the masked opcode to determine the encoding strategy

### Per-Opcode Dispatch

The master switch at the entry of `sub_A36360` routes instructions by opcode class:

| Opcode Class | Handler | Description |
|---|---|---|
| 2, 3, 5, 7 | Inline (LABEL_18 path) | Standard ALU/memory with full barrier analysis. Checks operand subtype 9–10 and architecture feature at `*(sm_backend+1037) & 0x20`. Calls `sub_A32C70` for operand analysis, then `sub_A31040` for field encoding. |
| 6 | `sub_A34B70` | Wait barrier mask encoding for specific memory operations |
| 10, 149, 151, 290 | Inline (large block) | Extended operations with special barrier handling. Calls `sub_A32A20` for multi-operand setup, then processes register-type checks at offset +64 (type==5 triggers additional barrier logic). |
| All others | Per-field encoder chain | Default path through the six encoder functions |

### Per-Field Encoder Chain

For the default path, `sub_A36360` calls these encoders in sequence:

```c
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
| `sub_A318F0` | 4 KB | Barrier dependency distance computation — measures the instruction distance between a barrier set and its corresponding wait |
| `sub_A31390` | 4 KB | Barrier set intersection / conflict detection — checks whether two instructions' barrier usage conflicts |
| `sub_A32C70` | — | Source/destination operand dependency analysis — identifies which operands create dependencies |
| `sub_A31040` | — | Master field encoding dispatcher — coordinates all six per-field encoders |

## Dependency Barrier Allocation Algorithm

The barrier allocator manages the 6 hardware barrier registers as a resource pool. The algorithm must satisfy three constraints:
1. No two simultaneously-live long-latency operations share a barrier index
2. Every consumer instruction waits on the correct barrier before reading its operand
3. Barrier reuse is maximized to avoid unnecessary stalls

### Allocation State Machine

```text
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

The orchestrator (`sub_A356A0`, EncodeScoreboardFields) drives the three-phase allocation. Phase 1 resolves operand-to-register mappings and extracts the pipe class. Phase 2 selects a barrier index. Phase 3 applies conflict correction when the `ctx+1415` bit 5 flag is set (enabled for non-texture long-latency classes).

```c
function AllocateBarrier(ctx, producer_instr):
    pipe_class = LookupPipeClass(producer_instr)       // from dep_rules[unit_id]
    barrier_lat = dep_rules[pipe_class].barrier_latency // typically 56 cycles

    // Phase 1: distance-based freeing — release stale barriers (sub_A318F0)
    //   For each active barrier, compute the instruction distance from its
    //   producer to the current instruction.  sub_A318F0 calls sub_92F520
    //   (opcode 0x8B = forward distance, 0x8F = backward distance) to obtain
    //   paired min/max distances, then applies the min-distance operator
    //   (sub_9331F0, opcode 5) across both source operand pairs.
    for i in 0..5:
        if barrier[i].status == PENDING:
            dist = InstrDistance(barrier[i].producer, current_ip)  // sub_92F520
            if dist >= barrier_lat:                                // threshold from table
                barrier[i].status = FREE

    // Phase 2: type-affinity preference
    //   When pipe_class matches a previously-assigned barrier's class (e.g.,
    //   two TEX operations share the same texture pipeline), prefer that index.
    //   This reduces cross-class contention and lets the HW batch completions.
    best = -1
    for i in 0..5:
        if barrier[i].status == FREE:
            if barrier[i].last_pipe_class == pipe_class:
                best = i; break                         // affinity hit
            if best == -1: best = i                     // first-free fallback

    if best == -1:
        // All occupied: oldest-first eviction
        best = argmin(barrier[i].set_cycle for i in 0..5)
        InsertWaitForBarrier(ctx, best)                 // sub_9253C0

    barrier[best].status       = PENDING
    barrier[best].producer     = producer_instr
    barrier[best].set_cycle    = current_cycle
    barrier[best].last_pipe_class = pipe_class
    candidate = best

    // Phase 3: conflict avoidance (sub_A31390)
    //   Called when ctx+1415 bit 5 is set.  Builds operand-pair intersection
    //   vectors for the candidate barrier against all other active barriers.
    //   Uses sub_9331F0 (min-distance), sub_92FEB0 (opcode 0xC9, register
    //   bank conflict), sub_9300A0 (opcode 0x24, select-with-threshold),
    //   and sub_92E850 (opcode 0x82, overlap test) to detect whether any
    //   live barrier's source/destination set intersects the candidate's.
    //   If intersection is non-empty, the conflicting barrier's wait is
    //   merged into the current instruction's wait mask.
    if ctx.conflict_avoidance_enabled:                  // bit 5 of byte at ctx+1415
        for i in 0..5 where i != candidate:
            if barrier[i].status == PENDING:
                if BarrierSetsConflict(ctx, candidate, i):  // sub_A31390 core
                    consumer_instr.wait_mask |= (1 << i)

    return candidate

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

1. **Oldest-first eviction**: When all 6 barriers are occupied, the oldest (earliest `set_cycle`) is evicted. This maximizes the chance that the evicted operation has already completed.

2. **Type affinity**: The pipe class of the producer is compared against `barrier[i].last_pipe_class`. A match (e.g., two TEX operations, or two LDGSTS operations sharing unit IDs 4-10) wins over first-free. This is visible in `sub_A356A0` at the branch where opcode class 10 routes through the separate `sub_A31F80` path that preserves texture barrier grouping.

3. **Distance-based freeing**: `sub_A318F0` computes paired forward/backward instruction distances from each active barrier's producer to the current instruction. For each operand pair it calls `sub_92F520` with opcodes 0x8B (forward) and 0x8F (backward), then reduces the four distance values via `sub_9331F0` (opcode 5, signed minimum). A barrier is freed without an explicit wait when this distance exceeds `dep_rules[pipe_class].barrier_latency` (56 cycles on sm_100, consistent across all 430 entries in the table).

4. **Conflict avoidance**: When `ctx+1415` bit 5 is set, `sub_A31390` performs a 4-register intersection test between the candidate barrier's operand set and each other live barrier. It builds two operand-pair vectors per barrier (source and destination), runs bank-conflict detection (`sub_92FEB0`, opcode 0xC9), threshold selection (`sub_9300A0`, opcode 0x24 with constant 0x6000000D), and overlap tests (`sub_92E850`, opcode 0x82). The output is a 4-element conflict vector written back to the consumer's scoreboard state at `a10[0..3]`. Any nonzero element forces a wait bit for the conflicting barrier.

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

## Scoreboard Object Layout (952 bytes)

The scoreboard object is allocated by `sub_8D0640` (ScheduleInstructions) when the architecture feature flag `*(func+1385) & 4` is set. The 952-byte allocation goes through the function context's vtable-dispatched allocator at `*(func+16)`, and the constructor `sub_69A1A0` initializes it. The pointer is stored at `func+1864`.

The object has three regions: 35 reference-counted counter slots, a linked-list/tree node for active barrier tracking, and 14 barrier tracking records.

### Region 1: Counter Slots (offsets +0 to +272)

35 QWORD pointer slots, each pointing to an externally-allocated 24-byte counter node. Each counter node has the layout:

```text
Counter node (24 bytes):
  +0   QWORD   refcount (initialized to 1)
  +8   QWORD   value (initialized to 0)
  +16  QWORD   allocator back-reference
```

The 35 slots are organized as barrier state / stall counter pairs for each register class, plus additional scoreboard tracking counters:

| Offset | Slot | Purpose |
|---|---|---|
| +0 | 0 | R (general-purpose register) barrier state |
| +8 | 1 | R stall counter |
| +16 | 2 | P (predicate register) barrier state |
| +24 | 3 | P stall counter |
| +32 | 4 | UR (uniform register) barrier state |
| +40 | 5 | UR stall counter |
| +48 | 6 | UP (uniform predicate) barrier state |
| +56 | 7 | UP stall counter |
| +64 | 8 | B (barrier register) barrier state |
| +72 | 9 | B stall counter |
| +80 | 10 | Arch-specific class 5 barrier state |
| +88 | 11 | Arch-specific class 5 stall counter |
| +96 | 12 | Arch-specific class 6 barrier state |
| +104 | 13 | Arch-specific class 6 stall counter |
| +112 | 14 | Arch-specific class 7 barrier state |
| +120 | 15 | Arch-specific class 7 stall counter |
| +128 | 16 | Arch-specific class 8 barrier state |
| +136 | 17 | Arch-specific class 8 stall counter |
| +144--+272 | 18–34 | Additional scoreboard tracking counters (17 slots) |

Total: 35 slots x 8 bytes = 280 bytes.

### Region 2: Linked List / Tree Node (offsets +280 to +391)

This region contains an intrusive data structure (linked list or red-black tree node) used for tracking active barrier assignments. It cross-references counter slots from Region 1.

| Offset | Size | Type | Init | Purpose |
|---|---|---|---|---|
| +280 | 8 | `ptr` | from `a2+16` | Allocator reference (arena/memory pool) |
| +288 | 8 | `QWORD` | 0 | List sentinel / null node |
| +296 | 8 | `ptr` | `&self+304` | List head pointer |
| +304 | 8 | `ptr` | `&self+288` | Forward link (points to sentinel) |
| +312 | 8 | `QWORD` | 0 | Node data |
| +320 | 8 | `ptr` | `&self+288` | Backward link (points to sentinel) |
| +328 | 8 | `ptr` | `&self+304` | Secondary forward link |
| +336 | 4 | `DWORD` | 2 | Node type / RB-tree color (2 = initial) |
| +344 | 8 | `ptr` | slot 1 ref | Cross-reference to counter slot 1 (R stall counter); refcount incremented |
| +352 | 8 | `QWORD` | 0 | Pending producer instruction pointer |
| +360 | 8 | `QWORD` | 0 | Set cycle timestamp |
| +368 | 8 | `QWORD` | 0 | Consumer list head |
| +376 | 4 | `DWORD` | 0 | Active flag / barrier index |
| +384 | 8 | `ptr` | slot 19 ref | Cross-reference to counter slot 19 |

Total: 112 bytes.

### Region 3: Barrier Tracking Records (offsets +392 to +951)

14 identical 40-byte records, each tracking one dependency barrier register. The first 6 records correspond to the 6 hardware dependency barriers per warp. Records 6–12 are extended/spare slots for overflow or future barrier model expansion (sm_100+). Record 13 uses a different initialization path (`sub_6996C0` instead of `sub_69A120`), suggesting it serves as a sentinel or special-purpose record.

Per-record layout (40 bytes):

| Offset (within record) | Size | Type | Init | Purpose |
|---|---|---|---|---|
| +0 | 8 | `QWORD` | 0 | Barrier status: FREE (0), PENDING, COMPLETED |
| +8 | 8 | `QWORD` | 0 | Producer instruction pointer (or NULL when free) |
| +16 | 8 | `QWORD` | 0 | Set cycle / consumer tracking state |
| +24 | 4 | `DWORD` | 0 | Barrier flags / consumer count |
| +28 | 4 | — | — | (padding) |
| +32 | 8 | `ptr` | slot 19 ref | Cross-reference to counter slot 19 (allocator back-pointer) |

Record index to offset mapping:

| Record | Offset | Hardware Barrier |
|---|---|---|
| 0 | +392 | Dependency barrier 0 |
| 1 | +432 | Dependency barrier 1 |
| 2 | +472 | Dependency barrier 2 |
| 3 | +512 | Dependency barrier 3 |
| 4 | +552 | Dependency barrier 4 |
| 5 | +592 | Dependency barrier 5 |
| 6 | +632 | Extended / spare 0 |
| 7 | +672 | Extended / spare 1 |
| 8 | +712 | Extended / spare 2 |
| 9 | +752 | Extended / spare 3 |
| 10 | +792 | Extended / spare 4 |
| 11 | +832 | Extended / spare 5 |
| 12 | +872 | Extended / spare 6 |
| 13 | +912 | Sentinel record (different init via `sub_6996C0`) |

Tail pointer:

| Offset | Size | Type | Purpose |
|---|---|---|---|
| +944 | 8 | `ptr` | Counter reference for sentinel record (from slot 25) |

Total: 14 records x 40 bytes = 560 bytes + 8 byte tail = 568 bytes.

### Memory Layout Diagram

```text
ScoreboardObject (952 bytes)
+--------+--------+--------+--------+--------+--------+--------+--------+
|+0 slot0 (R bar) |+8 slot1 (R stl) |+16 slot2 (P bar)|+24 slot3 (P stl)|
+--------+--------+--------+--------+--------+--------+--------+--------+
|+32 slot4 (UR)   |+40 slot5 (UR)   |+48 slot6 (UP)   |+56 slot7 (UP)   |
+--------+--------+--------+--------+--------+--------+--------+--------+
|+64 slot8 (B)    |+72 slot9 (B)    |+80..+272 slots 10–34             |
+--------+--------+--------+--------+--------+--------+--------+--------+
|+280 allocRef    |+288 sentinel    |+296 listHead    |+304 fwdLink     |
+--------+--------+--------+--------+--------+--------+--------+--------+
|+312 nodeData    |+320 bwdLink     |+328 secFwd      |+336 type |      |
+--------+--------+--------+--------+--------+--------+--------+--------+
|+344 slotRef     |+352 producer    |+360 setCycle    |+368 consumers   |
+--------+--------+--------+--------+--------+--------+--------+--------+
|+376 flags|      |+384 slotRef19  |                                    |
+--------+--------+--------+--------+--------+--------+--------+--------+
|+392  barrierRecord[0]  (40B)      |+432  barrierRecord[1]  (40B)      |
+--------+--------+--------+--------+--------+--------+--------+--------+
|+472  barrierRecord[2]             |+512  barrierRecord[3]             |
+--------+--------+--------+--------+--------+--------+--------+--------+
|+552  barrierRecord[4]             |+592  barrierRecord[5]             |
+--------+--------+--------+--------+--------+--------+--------+--------+
|+632..+912  barrierRecords[6..13]  (8 extended / spare records)        |
+--------+--------+--------+--------+--------+--------+--------+--------+
|+944 tailPtr     |+952 END                                             |
+--------+--------+--------+--------+--------+--------+--------+--------+
```

### Design Notes

The counter nodes use reference counting (initial refcount = 1, incremented when cross-referenced from Region 2 or Region 3). This enables sharing counter state across multiple tracking contexts — for example, when the scheduling passes for pre-scheduling and post-scheduling need to track the same barrier state.

The 14 barrier records provide 6 slots for the hardware barrier registers plus 8 extended slots. Current architectures use exactly 6 dependency barriers per warp, but the extended slots provide headroom for the expanded barrier model hinted at in sm_100+ configurations (see `*(ctx+1040) & 0x10` extended barriers flag).

### State Machine Algorithms

The scoreboard object is a passive state store; three scheduling-time operations query and mutate it. All three are called from the control word encoder chain (`sub_A356A0` / `sub_A342E0` / `sub_A34B70`) during Phase 115.

**Counter slot usage.** Slots 0–17 are organized as `(barrier_state, stall_counter)` pairs for each of the 9 register classes (R, P, UR, UP, B, plus 4 arch-specific). The barrier\_state half tracks which hardware barrier index, if any, is currently live for that register class. The stall\_counter half accumulates the minimum stall cycles computed from dependency distances. Slots 18–34 are auxiliary scoreboard counters used by the linked-list node (Region 2) and the 14 barrier records (Region 3) as shared refcounted storage for cross-context state propagation.

```c
// Called from sub_A342E0 (EncodeWriteBarrierIndex) when a long-latency
// producer is issued and requires a dependency barrier.
function on_instruction_issued(sb, ctx, producer, reg_class, latency):
    pair_base = reg_class * 2                  // slot index: 0,2,4,...,16
    bar_slot  = sb.slots[pair_base]            // counter node for barrier state
    stl_slot  = sb.slots[pair_base + 1]        // counter node for stall counter

    if latency <= 15:
        // Short latency: stall count alone suffices.
        stl_slot.value = max(stl_slot.value, latency)
        return BARRIER_NONE                    // write_barrier = 7

    // Long latency: allocate a hardware barrier.
    idx = AllocateBarrier(sb, ctx, producer)   // see existing pseudocode above
    rec = sb.barrier_records[idx]              // Region 3, offset +392 + idx*40
    rec.status   = PENDING
    rec.producer = producer
    rec.set_cycle = ctx.current_seq            // from *(ctx+264)
    rec.flags    = 0
    bar_slot.value = idx                       // remember which barrier owns this class
    return idx
```

```c
// Called from sub_A356A0 (EncodeScoreboardFields) / sub_A34B70
// (EncodeWaitBarrierMask) to bind a barrier to a register class.
// Executes when the allocator assigns barrier idx to a producer.
function on_barrier_set(sb, idx, producer, reg_class_mask):
    rec = sb.barrier_records[idx]
    rec.status   = PENDING
    rec.producer = producer
    rec.set_cycle = ctx.current_seq
    rec.flags    = 0                           // consumer count reset

    // Cross-reference: bump refcount on the shared counter node.
    // Records 0-12 share slot 19; record 13 uses slot 25.
    shared = rec.counter_ref                   // ptr at record+32
    atomic_inc(shared.refcount)

    // Update the linked-list node in Region 2 when the first
    // barrier of the block is assigned (tracked via +376 active flag).
    if sb.region2.active_flag == 0:
        sb.region2.producer      = producer
        sb.region2.set_cycle     = ctx.current_seq
        sb.region2.active_flag   = idx | 0x1   // mark active + barrier index
```

```c
// Called from sub_A356A0 (EncodeScoreboardFields) when a consumer
// instruction needs the result protected by barrier idx.
// Produces wait_mask bits and manages barrier lifecycle.
function on_barrier_wait(sb, ctx, consumer, idx):
    rec = sb.barrier_records[idx]

    // Record the consumer; increment flags as a ref-count of waiters.
    rec.flags += 1

    // Distance-based early release check (sub_A318F0):
    // If instruction distance from producer exceeds max arch latency,
    // the barrier has certainly completed in hardware -- release it
    // without emitting a wait bit.
    dist = ctx.current_seq - rec.set_cycle
    if dist > arch_max_latency(rec.producer):
        release_barrier(sb, idx)
        return 0                               // no wait bit needed

    // Conflict check (sub_A31390): verify the barrier has not been
    // recycled since the producer was issued.  If producer != the
    // current owner, the barrier was force-evicted -- fall back to
    // a stall count on the consumer instead.
    if rec.producer != expected_producer(consumer, idx):
        return 0                               // resolved via stall

    // Normal path: return the wait mask bit for this barrier.
    return (1 << idx)

function release_barrier(sb, idx):
    rec = sb.barrier_records[idx]
    rec.status   = FREE
    rec.producer = NULL
    rec.set_cycle = 0
    rec.flags    = 0
    // Decrement shared counter refcount; if it drops to 0 the
    // counter node is returned to the arena (sub_69A120 destructor).
    shared = rec.counter_ref
    if atomic_dec(shared.refcount) == 0:
        arena_free(shared)
```

**Ref-counting protocol summary.** Every 24-byte counter node starts with `refcount = 1` (owned by its slot in Region 1). When Region 2 or Region 3 cross-references a node, the constructor increments the refcount by 2 (one logical ref for the record, one for the back-pointer chain). The destructor `sub_69A120` decrements and, on reaching zero, walks the node's value-list at `node+8` to deallocate chained sub-nodes before returning the node itself to the arena via `vtable[4]` (offset +32 in the allocator vtable). `sub_6996C0` is identical but used exclusively for the sentinel record (index 13), isolating its teardown from the main barrier pool.

## Stall Count Computation

The stall count is the minimum number of cycles the warp scheduler must wait before issuing the instruction. It is computed from the dependency distance to the instruction's producers.

### Algorithm

```c
function ComputeStallCount(ctx, instr):
    max_stall = 0
    for each source_operand in instr:
        producer = FindProducer(source_operand)
        if producer == NULL:
            continue
        distance = instr.cycle - producer.cycle
        latency = GetLatency(producer.opcode, ctx.sm_backend)
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

The stall count computation uses the SM backend at `*(func+1584)` (`sm_backend`) to look up per-opcode latencies from the architecture's hardware latency tables; see [Latency Model](latency-model.md).

### Yield Flag

The yield flag is computed by `sub_8F3650` (`EncodeYieldField`, 2.7 KB). The function resolves the predecessor instruction from the control word array and decides whether the warp scheduler should switch to another warp after issuing this instruction.

```c
function EncodeYieldField(out, cw_array):
    prev = resolve_predecessor(cw_array)          // cw_array[20] - offset - 4
    if (prev.ctrl_word ^ 0x70000000) & 0x70000000:
        return 0                                   // not a real instruction

    stall = out[7]                                 // raw stall count from earlier pass
    if prev.flags & 0x80:                          // bit 0 of byte at +7
        // Per-instruction override: clamp stall to *(prev_bb_desc+73)
        stall = min(stall, *(prev_bb_desc + 73))
    else:
        // Default path: hardcoded threshold
        if stall > 3:
            stall = 4                              // cap at yield_threshold

    out[7] = stall
    if stall <= 2:
        return 0                                   // short stall: no yield
    // stall >= 3: yield to another warp (goto LABEL_6)
    ...
```

#### Per-Architecture Yield Threshold

The default yield threshold of 4 (`stall > 3` at `sub_8F3650+0x54`) is hardcoded in the default code path and applies uniformly across all SM targets. Per-instruction overrides use a signed byte at `*(instr_desc+73)` in the basic block descriptor, which can lower (but not raise) the threshold for specific instructions.

The scheduling context stores the related stall threshold at `sched_ctx+408` (knob 741, `SchedCountLoadsPerTex`, default 3). This controls the minimum stall floor (not the yield decision directly), but feeds into the same pipeline:

| SM | Generation | Yield threshold | Stall floor (knob 741) | SM backend field |
|---|---|---|---|---|
| sm_70 | Volta | 4 | 3 | `*(sm_backend+912)` / 4 |
| sm_75 | Turing | 4 | 3 | `*(sm_backend+912)` / 4 |
| sm_80 | Ampere | 4 | 3 | `*(sm_backend+912)` / 4 |
| sm_86 | Ampere | 4 | 3 | `*(sm_backend+912)` / 4 |
| sm_89 | Ada | 4 | 3 | `*(sm_backend+912)` / 4 |
| sm_90 | Hopper | 4 | 3 | `*(sm_backend+912)` / 4 |
| sm_100 | Blackwell | 4 | 3 | `*(sm_backend+912)` / 4 |
| sm_120 | Blackwell | 4 | 3 | `*(sm_backend+912)` / 4 |

The yield threshold itself (the `> 3` comparison) does not vary by architecture — it is a compile-time constant in `sub_8F3650`. What varies per architecture is the **yield batch size** at `sm_backend+912`: for texture-opcode yield sequences (opcode 288 path at `sub_8F3650+0xA8`), the function reads `*(sm_backend+912) / 4` to determine how many texture operations share a single yield group. The SM backend vtable calls at offsets `+904` and `+936` provide the texture group index and batch divisor respectively.

## Mercury Opex Path (Phase 120)

In addition to the Ori IR scoreboard passes (phases 114–116), the Mercury backend has its own scoreboard generation pass: **MercGenerateOpex** (phase 120, `sub_703480` / `sub_6FFDC0`).

This pass runs after Mercury encode/decode (phase 117) and instruction expansion (phase 118), operating on the Mercury intermediate representation rather than Ori IR. It generates:
- DEPBAR instructions for explicit barrier management
- Scoreboard wait annotations
- Stall count adjustments for expanded instruction sequences
- Synchronization barriers for cross-warp dependencies

The Mercury opex pass and the Ori scoreboard passes serve different purposes:
- **Phases 114–116** generate scoreboard metadata at the Ori IR level, before Mercury encoding
- **Phase 120** generates additional scoreboard metadata for instructions introduced during Mercury expansion (pseudo-instruction expansion, WAR hazard insertion)
- The WAR pass must run twice (phases 119 and 121) because opex introduces new instructions that create additional write-after-read hazards

## Scheduling Output Encoding

After the control word fields are computed, the scheduling output pipeline (0x8F1EB0–0x8FDD60, ~57 KB) encodes them into the final SASS binary format.

### Encoding Pipeline

| Address | Size | Function | Purpose |
|---------|------|----------|---------|
| `sub_8F1EB0` | 15 KB | EncodeScheduleWords | Main scheduling output encoder — iterates all instructions and produces control words |
| `sub_8F3130` | 1.0 KB | EncodeStallField | Packs 4-bit stall count into control word |
| `sub_8F31F0` | 6.1 KB | EncodeBarrierField | Packs barrier set/wait fields with architecture-specific layout |
| `sub_8F3650` | 2.7 KB | EncodeYieldField | Packs yield flag; hardcoded threshold `stall > 3`; per-instruction override via `*(instr_desc+73)`; texture batch path via `*(sm_backend+912) / 4` |
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

Seven verification functions (0x8F7610–0x8F8CB0) validate the generated schedule:
1. Stall count bounds (0–15)
2. Barrier index validity (0–5 or 7=none)
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

### Barrier Assignment Lifecycle Reconciliation

Pre-scheduling (`sub_8D7760`) and post-scheduling (`sub_A36360` / `sub_A23CF0`) run on opposite sides of register allocation. Pre-scheduling operates on virtual registers with estimated latencies; post-scheduling sees physical registers with final instruction distances. Because register allocation may insert spill/reload instructions, reorder operands, or coalesce registers, the physical distances between a producer and its consumers can differ from the virtual-register estimates. The reconciliation protocol ensures the final control word reflects the true physical distances rather than the stale pre-scheduling guesses.

**Handoff mechanism.** `sub_8D7760` writes preliminary barrier state into the instruction's operand slots. For each long-latency producer, it stores the assigned barrier index in the operand descriptor's high word (bits 25–29, via the `sub_8C25B0` helper) and sets a tracking flag at `*(instr+88) |= 0x800000` (bit 23). The per-instruction stall hint goes to `*(func+240..252)` (register space 7 = scheduling complete). These annotations travel through register allocation unchanged — the allocator preserves the operand descriptor high bits and the tracking flag.

**Post-scheduling re-evaluation.** When `sub_A36360` processes each instruction in final order, it does not blindly copy pre-scheduling barrier indices. Instead, it runs a full three-step reconciliation:

```c
function reconcile_barriers(pre_sched_state, phys_reg_distances) -> final:
    // Step 1: Release stale barriers using physical instruction distances.
    //   sub_A32C70 resolves phys reg IDs via *(func+88) + 8*reg_id.
    //   sub_A318F0 computes forward distance with opcodes 0x8B/0x8F.
    for each active barrier i in scoreboard_object.barrier_records[0..5]:
        phys_dist = InstrDistance(barrier[i].producer, current_ip)  // sub_A318F0
        if phys_dist >= barrier_latency_threshold:                  // 56 cycles
            release_barrier(scoreboard_object, i)                   // stale -- free it

    // Step 2: Re-classify each source operand's dependency need.
    //   Phys register type at *(reg_obj+64) may differ from virtual-reg estimate
    //   (e.g., spill/reload changes distance). sub_91BF30 queries per-class config.
    for each source operand with tracking flag (bit 23 of *(instr+88)):
        reg_class = *(phys_reg_obj + 64)
        new_dist  = ComputePhysDistance(func, instr, operand_idx)
        if new_dist <= 15:
            final.stall = max(final.stall, new_dist)      // downgrade to stall
        else:
            // Re-allocate from live pool (sub_A356A0); pre-sched index discarded.
            idx = AllocateBarrier(scoreboard_object, ctx, producer)
            final.write_barrier = idx
            final.wait_mask |= pending_consumer_waits(idx)

    // Step 3: Conflict correction (sub_A31390).
    //   Re-run 4-register intersection test; spill code may have introduced
    //   new conflicts invisible to pre-scheduling.
    if ctx.conflict_avoidance_enabled:
        for each pair (candidate, other) in active barriers:
            if BarrierSetsConflict(ctx, candidate, other):
                final.wait_mask |= (1 << other)

    return final                                           // written by sub_9253C0
```

**Key design point.** The pre-scheduling barrier index is *not* preserved across the boundary. `sub_A36360` builds a fresh 952-byte scoreboard object per basic block and populates it in scheduled order. Only the tracking flag (bit 23) and operand classification bits survive from `sub_8D7760`; the barrier index itself is recomputed because final instruction ordering determines which slots are oldest, which have affinity, and which exceed the 56-cycle threshold.

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

The `sub_A2D340` (32 KB) function writes these fields through a large if/else cascade on the Ori IR opcode at `*(uint32_t*)(instr+72)` (masked via `BYTE1 & 0xCF`). The dispatched opcodes are control-flow and branch-stack instructions whose names come from the [Ori IR table](../ir/instructions.md) / [ROT13 mnemonic table](../reference/sass-opcodes.md): 50 (`FRND_X`, FP round extended), 61 (`BAR`, barrier synchronization), 73 (`BSSY`, branch-sync-stack push), 74 (`BREAK`, break out of convergence region), 77 (`EXIT`, thread exit), 78 (`RTT`, return-to-trap-handler), plus 35, 39, 40, 63, 83, 105, 125, 186, 220, 223, 270, 279, 297. Each arm writes a different Mercury control-byte template into `*(WORD*)(instr+196)` (e.g. `0xC5` for RTT, `0xAD` for BSSY, `0xAE` for BREAK) and an opcode-specific stall/barrier/reuse layout. The Enc-ID table column in [SASS opcode encoders](../reference/sass-opcodes.md#memory) reuses these same small integers for unrelated memory/tensor variants (50=ATOM, 73=BAR, 74=ST, 77=LDS/STS, 78=HMMA); that is the *encoder* dispatch space and is not what this switch reads.

## Function Map

| Address | Size | Identity |
|---------|------|---------|
| `sub_85C890` | 1.5 KB | ScoreboardDispatcher — opcode-based fast/slow path router |
| `sub_A220A0` | 9 KB | InstructionPropertyQuery — scheduling descriptor filler |
| `sub_A22B40` | — | WriteBarrierAssign — barrier index assignment for operand |
| `sub_A22BC0` | — | ReadBarrierAssign — wait mask assignment for operand |
| `sub_A22CE0` | — | InstructionClassify — scoreboard processing classification |
| `sub_A22D00` | — | DependencyDistance — compute instruction distance |
| `sub_A227F0` | — | FlaggedInstrPreprocess — bit-12-set instruction handling |
| `sub_A231E0` | — | SchedulingScore — full-DAG-analysis necessity check |
| `sub_A23CF0` | 54 KB | **DAGListScheduler** — full dependency-driven scoreboard |
| `sub_A265B0` | 10 KB | BarrierDependencyTracker — barrier assignment tracking |
| `sub_A29220` | 12 KB | InstructionEmissionFilter — instruction emission gating |
| `sub_A2BD90` | 23 KB | ArchControlWordConfig — architecture-specific parameter loader |
| `sub_A2D340` | 32 KB | InstructionControlWordEncoder — per-opcode field writer |
| `sub_A31040` | — | MasterFieldEncoder — coordinates per-field encoders |
| `sub_A31390` | 4 KB | BarrierConflictDetect — barrier set intersection check |
| `sub_A318F0` | 4 KB | BarrierDistanceCompute — dependency distance to barrier |
| `sub_A31F80` | 7 KB | ComputeReuseFlags — operand reuse buffer hints |
| `sub_A32C70` | — | OperandDependencyAnalysis — source/dest dep extraction |
| `sub_A333A0` | 3 KB | EncodeStallAndYield — 4-bit stall + 1-bit yield |
| `sub_A33660` | 7 KB | EncodeReadBarrierMask — 6-bit read barrier mask |
| `sub_A342E0` | 9 KB | EncodeWriteBarrierIndex — 3-bit write barrier index |
| `sub_A34B70` | 10 KB | EncodeWaitBarrierMask — 6-bit wait barrier mask |
| `sub_A356A0` | 12 KB | EncodeScoreboardFields — combined scoreboard encoder |
| `sub_A36360` | 52 KB | **GenerateControlWord** — master control word generator |
| `sub_8D7760` | 41 KB | StallAndBarrierInsertion — pre-scheduling control words |
| `sub_8E4920` | 6.9 KB | BuildScoreboardEntries — scoreboard BST construction |
| `sub_8E5CA0` | 20 KB | EmitScheduleOutput — control word output encoder |
| `sub_8F1EB0` | 15 KB | EncodeScheduleWords — SASS control word output |
| `sub_8F4140` | 5.6 KB | EncodeFullControlWord — 23-bit packing |
| `sub_A95DC0` | 36 KB | SASSControlWordEncoder — architecture-dispatched encoder |
| `sub_6FFDC0` | 66 KB | MercOpexBody — Mercury opex scoreboard generation |
| `sub_703480` | 1.4 KB | RunOpexPass — MercGenerateOpex entry |

## Cross-References

- [Scheduler Overview](overview.md) — 3-phase scheduler architecture, scheduling output pipeline
- [Scheduling Algorithm](algorithm.md) — priority list scheduling, dependency DAG construction
- [Latency Model](latency-model.md) — per-opcode latency tables used by stall count computation
- [Mercury Encoder](../codegen/mercury.md) — Mercury pipeline including MercGenerateOpex (phase 120)
- [SASS Encoding](../codegen/encoding.md) — instruction encoding format including control word bit layout
- [Phase Manager](../passes/phase-manager.md) — how phases 114–116 fit in the 159-phase pipeline
- [Sync & Barriers](../passes/sync-barriers.md) — software synchronization barriers (distinct from dependency barriers)
- [Knobs](../config/knobs.md) — scheduling knobs 741 (stall threshold), 805/806 (stall caps)
