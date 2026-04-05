# Register Model (R / UR / P / UP)

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

ptxas models four hardware register files plus two auxiliary barrier register files. Every Ori instruction references registers from one or more of these files. During the optimization phases (0--158), registers carry virtual numbers; the fat-point register allocator (phase 159+) maps them to physical hardware slots. This page documents the register files, the virtual/physical register descriptor, the 7 allocator register classes, wide register conventions, special registers, the operand encoding format, pressure tracking, and SM-specific limits.

## Four Register Files

| File | Mnemonic | Width | Usable range | Zero/True | ABI type | Introduced |
|------|----------|-------|--------------|-----------|----------|------------|
| R | General-purpose | 32 bits | R0 -- R254 | RZ (R255) | 2 | sm\_30 |
| UR | Uniform | 32 bits | UR0 -- UR62 | URZ (UR63) | 3 | sm\_75 |
| P | Predicate | 1 bit | P0 -- P6 | PT (P7) | 5 | sm\_30 |
| UP | Uniform predicate | 1 bit | UP0 -- UP6 | UPT (UP7) | -- | sm\_75 |

**R registers** are per-thread 32-bit general-purpose registers. They hold integers, floating-point values, and addresses. 64-bit values occupy consecutive even/odd pairs (R4:R5); 128-bit values occupy aligned quads (R0:R1:R2:R3). The total R-register count for a function is `field[159] + field[102]` (reserved + allocated), stored in the Code Object at offsets +159 and +102. Maximum usable: 254 (R0--R254). R255 is the hardware zero register RZ -- reads return 0, writes are discarded.

**UR registers** (uniform general-purpose) are warp-uniform: every thread in a warp sees the same value. Available on sm\_75 and later. Range: UR0--UR62 usable, UR63 is the uniform zero register URZ. The UR count is at Code Object +99. Attempting to use UR on pre-sm\_75 targets triggers the diagnostic `"Uniform registers were disallowed, but the compiler required (%d) uniform registers for correct code generation."`.

**P registers** are 1-bit predicates used for conditional execution (`@P0 FADD ...`) and branch conditions. P0--P6 are usable; P7 is the hardwired always-true predicate PT. Writes to PT are discarded. The assembler uses PT as the default predicate for unconditional instructions. In the allocator, predicate registers support half-width packing: two virtual predicates can be packed into one physical predicate slot, with the hi/lo distinction stored in bit 23 (`0x800000`) of the virtual register flags.

**UP registers** are the uniform predicate variant. UP0--UP6 are usable; UP7 is UPT (always-true). Available on sm\_75+.

## Seven Allocator Register Classes

The fat-point allocator processes 7 register classes, indexed by the `reg_type` field at `vreg+64`. Class 0 is the cross-class constraint propagation channel and is skipped in the main allocation loop. Classes 1--6 are allocated independently, in order. The allocator distribution loop in `sub_9721C0` (lines 520--549) reads `*(int*)(vreg+64)` and uses it directly as the class bucket index, guarded by `reg_type <= 6`:

| Class ID | Name | Width | HW limit | Description |
|----------|------|-------|----------|-------------|
| 0 | (unified) | -- | -- | Cross-class constraint propagation (skipped) |
| 1 | R | 32-bit | 255 | General-purpose registers (R0--R254) |
| 2 | R (alt) | 32-bit | 255 | GPR variant (used for RZ sentinel, stat collector alternate) |
| 3 | UR | 32-bit | 63 | Uniform general-purpose (UR0--UR62) |
| 4 | UR (ext) | 32-bit | 63 | Uniform GPR variant (triggers flag update at +1369 in constructor) |
| 5 | P / UP | 1-bit | 7 | Predicate registers (P0--P6, UP0--UP6) |
| 6 | Tensor/Acc | 32-bit | varies | Tensor/accumulator registers for MMA/WGMMA operations |

The class ID is the `reg_type` value stored at `vreg+64`. The allocator class at `vreg+12` is a separate field used for instruction-level classification, not for the per-class allocation passes. The allocator's per-class linked lists at `alloc[3*reg_type + 138]` are populated directly from `vreg+64`.

Per-class state is initialized via the target descriptor vtable call `vtable[896](alloc_state, class_id)`, which populates per-class register file descriptors at `alloc[114..156]` (four 8-byte entries per class).

### Barrier Registers

Barrier registers (B and UB) are a distinct register file used by the `BAR`, `DEPBAR`, `BSSY`, and `BSYNC` instructions for warp-level and CTA-level synchronization. B0--B15 are the non-uniform barrier registers; UB0--UB15 are the uniform variant. Barrier registers have `reg_type = 9`, which is above the `<= 6` cutoff for the main allocator class buckets. They are handled by a separate allocation mechanism outside the 7-class system.

### Tensor/Accumulator Registers (Class 6)

Class 6 registers are created during intrinsic lowering of tensor core operations (MMA, WGMMA, HMMA, DMMA). Over 30 intrinsic lowering functions in the 0x6B--0x6D address range call `sub_91BF30(ptr, ctx, 6)` to create these registers. The GMMA pipeline pass (`sub_ADA740`, `sub_69E590`) identifies accumulator operands by checking `*(vreg+64) == 6`. The accumulator counting function at `sub_78C6B0` uses the pair-mode bits at `vreg+48` (bits 20--21) to determine whether a type-6 register consumes 1 or 2 physical R slots.

## Virtual Register Descriptor

Every virtual register in a function is represented by a 160-byte descriptor allocated from the per-function arena. The register file array is at Code Object +88, indexed as `*(ctx+88) + 8*regId`. The descriptor is created by `sub_91BF30` (register creation function).

### Descriptor Layout

| Offset | Size | Type | Field | Notes |
|--------|------|------|-------|-------|
| +0 | 8 | `ptr` | `next` | Linked list pointer (allocation worklist) |
| +8 | 4 | `i32` | `id` | Unique register ID within function |
| +12 | 4 | `i32` | `class_index` | Allocator register class (0--6) |
| +20 | 1 | `u8` | `flags_byte` | Bit 0x20 = live |
| +24 | 4 | `i32` | `bb_index` | Basic block of definition |
| +28 | 4 | `i32` | `epoch` | Epoch counter for liveness tracking |
| +32 | 8 | `ptr` | `alias_next` | Next aliased register (coalescing chain) |
| +36 | 8 | `ptr` | `alias_parent` | Coalesced parent pointer |
| +40 | 4 | `f32` | `spill_cost` | Accumulated spill cost |
| +48 | 8 | `u64` | `flags` | Multi-purpose flag word (see below) |
| +56 | 8 | `ptr` | `def_instr` | Defining instruction pointer |
| +64 | 4 | `i32` | `reg_type` | Register file type enum |
| +68 | 4 | `i32` | `physical_reg` | Physical register number (-1 = unassigned) |
| +72 | 1 | `u8` | `size` | 0 = scalar, nonzero = encoded width |
| +76 | 4 | `f32` | `secondary_cost` | Secondary spill cost |
| +80 | 4 | `i32` | `spill_flag` | 0 = not spilled, 1 = spilled |
| +97 | 2 | `u16` | `reserved` | |
| +104 | 8 | `ptr` | `use_chain` | Use chain head (instruction pointer) |
| +112 | 8 | `ptr` | `def_chain` | Definition chain |
| +120 | 8 | `ptr` | `regfile_next` | Next in register file linked list |
| +128 | 8 | `ptr` | `linked_next` | Next in linked-register chain |
| +136 | 8 | `ptr` | `reserved2` | |
| +144 | 8 | `ptr` | `constraint_list` | Constraint list head for allocator |
| +152 | 8 | `ptr` | `reserved3` | |

Initial values set by the constructor (`sub_91BF30`):

```c
vreg->next           = NULL;            // +0
vreg->id             = ctx->reg_count + 1;  // +8, auto-incrementing
vreg->class_index    = 0;               // +12
vreg->flags_byte     = 0;               // +20
vreg->alias_parent   = (ptr)-1;         // +20..27 (qword write)
vreg->physical_reg   = -1;              // +68 (unassigned)
vreg->reg_type       = a3;              // +64 (passed as argument)
vreg->size           = 0;               // +72
vreg->spill_flag     = 0;               // +80
vreg->use_chain      = NULL;            // +104
vreg->def_chain      = NULL;            // +112
vreg->constraint_list = NULL;           // +144
```

For predicate types (a3 == 2 or a3 == 3), the flags word at +48 is initialized to `0x1000` (4096). For all other types, it is initialized to `0x1018` (4120). If the type is 7 (alternate predicate classification), the physical register is initialized to 0 instead of -1.

### Flag Bits at +48

| Bit | Mask | Meaning |
|-----|------|---------|
| 9 | `0x200` | Pre-assigned / fixed register |
| 10 | `0x400` | Coalesced source |
| 11 | `0x800` | Coalesced target |
| 12 | `0x1000` | Base flag (set for all types) |
| 14 | `0x4000` | Spill marker (already spilled) |
| 18 | `0x40000` | Needs-spill (allocator sets when over budget) |
| 20--21 | (pair mode) | 0 = single, 1 = lo-half of pair, 3 = double-width |
| 22 | `0x400000` | Constrained to architecture limit |
| 23 | `0x800000` | Hi-half of pair (predicate half-width packing) |
| 27 | `0x8000000` | Special handling flag |

### Register File Type Enum (at +64)

This enum determines the register file a VR belongs to. It is used by the register class name table at `off_21D2400` to map type values to printable strings ("R", "UR", "P", etc.) for diagnostic output such as `"Referencing undefined register: %s%d"`.

| Value | File | Alloc class | Description |
|-------|------|-------------|-------------|
| 1 | R | 1 | General-purpose register (32-bit) |
| 2 | R (alt) | 2 | GPR variant (RZ sentinel in `sub_7D82E0`, stat collector alternate) |
| 3 | UR | 3 | Uniform register (32-bit) |
| 4 | UR (ext) | 4 | Uniform GPR variant (triggers flag update at +1369 in constructor) |
| 5 | P / UP | 5 | Predicate register (1-bit); covers both P and UP |
| 6 | Tensor/Acc | 6 | Tensor/accumulator register for MMA/WGMMA operations |
| 7 | P (alt) | -- | Predicate variant (physical = 0 at init); above allocator cutoff |
| 8 | -- | -- | Extended type (created by `sub_83EF00`); above allocator cutoff |
| 9 | B / UB | -- | Barrier register; above allocator cutoff, separate allocation |
| 10 | R2 | -- | Extended register pair (64-bit, two consecutive R regs) |
| 11 | R4 | -- | Extended register quad (128-bit, four consecutive R regs) |

Values 0--6 are within the allocator's class system (the distribution loop in `sub_9721C0` guards with `reg_type <= 6`). Values 7+ are handled by separate mechanisms. The `off_21D2400` name table is indexed by reg_type and provides display strings for diagnostic output.

The stat collector at `sub_A60B60` (24 KB) enumerates approximately 25 register sub-classes including R, P, B, UR, UP, UB, Tensor/Acc, SRZ, PT, RZ, and others by iterating vtable getter functions per register class.

## Wide Registers

NVIDIA GPUs have only 32-bit physical registers. Wider values are composed from consecutive registers.

### 64-Bit Pairs (R2)

A 64-bit value occupies two consecutive registers where the base register has an even index: R0:R1, R2:R3, R4:R5, and so on. The low 32 bits reside in the even register; the high 32 bits in the odd register. In the Ori IR, a 64-bit pair is represented by a single virtual register with:

- `vreg+64` (type) = 10 (extended pair)
- `vreg+48` bits 20--21 (pair mode) = 3 (double-width)

The allocator selects even-numbered physical slots by scanning with stride 2 instead of 1. The register consumption function (`sub_939CE0`) computes `slot + (1 << (pair_mode == 3)) - 1`, consuming two physical slots.

### 128-Bit Quads (R4)

A 128-bit value occupies four consecutive registers aligned to a 4-register boundary: R0:R1:R2:R3, R4:R5:R6:R7, etc. Used by texture instructions, wide loads/stores, and tensor core operations. In the Ori IR:

- `vreg+64` (type) = 11 (extended quad)
- Allocator scans with stride 4

### Alignment Constraints

| Width | Base alignment | Stride | Example |
|-------|---------------|--------|---------|
| 32-bit (scalar) | Any | 1 | R7 |
| 64-bit (pair) | Even | 2 | R4:R5 |
| 128-bit (quad) | 4-aligned | 4 | R8:R9:R10:R11 |

The texture instruction decoder (`sub_1170920`) validates even-register alignment via a dedicated helper (`sub_1170680`) that checks if a register index falls within the set {34, 36, 38, ..., 78} and returns 0 if misaligned.

The SASS instruction encoder for register pairs (`sub_112CDA0`, 8.9 KB) maps 40 register pair combinations (0/1, 2/3, ..., 78/79) to packed 5-bit encoding values at 0x2000000 (33,554,432) intervals.

## Special Registers

### Zero and True Registers

| Register | File | Index | Internal sentinel | Behavior |
|----------|------|-------|-------------------|----------|
| RZ | R | 255 | 1023 | Reads return 0; writes discarded |
| URZ | UR | 63 | 1023 | Uniform zero; reads return 0 |
| PT | P | 7 | 31 | Always-true predicate; writes discarded |
| UPT | UP | 7 | 31 | Uniform always-true |

The internal sentinel value 1023 (`0x3FF`) represents "don't care" or "zero register" throughout the Ori IR and allocator. During SASS encoding, hardware register index 255 is mapped to sentinel 1023 for R/UR files, and hardware index 7 is mapped to sentinel 31 for P/UP files. These sentinels are checked in encoders to substitute the default register value:

```c
// Decoder: extract register operand (sub_9B3C20)
if (reg_idx == 255)
    internal_idx = 1023;   // RZ sentinel

// Decoder: extract predicate operand (sub_9B3D60)
if (pred_idx == 7)
    internal_idx = 31;     // PT sentinel

// Encoder: emit register field
if (reg == 1023)
    use *(a1+8) as default;  // encode physical RZ
```

### Architectural Predicate Indices

The allocator skips architectural predicate registers by index number:

| Index | Register | Treatment |
|-------|----------|-----------|
| 39 | (special) | Skipped during allocation (skip predicate `sub_9446D0`) |
| 41 | PT | Skipped -- hardwired true predicate |
| 42 | P0 | Skipped -- architectural predicate |
| 43 | P1 | Skipped -- architectural predicate |
| 44 | P2 | Skipped -- architectural predicate |

The skip check in `sub_9446D0` returns `true` (skip) for register indices 41--44 and 39, regardless of register class. For other registers, it checks whether the instruction is a CSSA phi (opcode 195 with barrier type 9) or whether the register is in the exclusion set hash table at `alloc+360`.

### Special System Registers (S2R / CS2R)

Thread identity and hardware state are accessed through the S2R (Special Register to Register) and CS2R (Control/Status Register to Register) instructions. These read read-only hardware registers into R-file registers.

Common system register values (from PTX parser initialization at `sub_451730`):

| PTX name | Hardware | Description |
|----------|----------|-------------|
| `%tid` / `%ntid` | SR\_TID\_X/Y/Z | Thread ID within CTA |
| `%ctaid` / `%nctaid` | SR\_CTAID\_X/Y/Z | CTA ID within grid |
| `%laneid` | SR\_LANEID | Lane index within warp (0--31) |
| `%warpid` / `%nwarpid` | SR\_WARPID | Warp index within CTA |
| `%smid` / `%nsmid` | SR\_SMID | SM index |
| `%gridid` | SR\_GRIDID | Grid identifier |
| `%clock` / `%clock_hi` / `%clock64` | SR\_CLOCK / SR\_CLOCK\_HI | Cycle counter |
| `%lanemask_eq/lt/le/gt/ge` | SR\_LANEMASK\_\* | Lane bitmask variants |

The S2R register index must be between 0 and 255 inclusive, enforced by the string `"S2R register must be between 0 and 255 inclusive"`. Special system register ranges are tracked at Code Object offsets +1712 (start) and +1716 (count).

## Operand Encoding in Ori Instructions

Each instruction operand is encoded as a 32-bit packed value in the operand array starting at instruction offset +84. The operand at index `i` is at `*(instr + 84 + 8*i)`.

### Packed Operand Format (Ori IR)

```
 31   30  29  28  27            24  23  22  21  20  19                  0
+----+---+---+---+---------------+---+---+---+---+---------------------+
|sign|     type  |  modifier (8) |                index (20)           |
+----+---+---+---+---------------+---+---+---+---+---------------------+
 bit 31: sign/direction flag          bits 0-19: register/symbol index
 bits 28-30: operand type (3 bits)    bit 24: pair extension flag
```

Extraction pattern (50+ call sites):

```c
uint32_t operand = *(uint32_t*)(instr + 84 + 8 * i);
int type    = (operand >> 28) & 7;     // bits 28-30
int index   = operand & 0xFFFFF;       // bits 0-19
int mods    = (operand >> 20) & 0xFF;  // bits 20-27
bool is_neg = (operand >> 31) & 1;     // bit 31
```

| Type value | Meaning |
|------------|---------|
| 1 | Register operand (index into register file at `*(ctx+88) + 8*index`) |
| 5 | Symbol/constant operand (index into symbol table at `*(ctx+152)`) |
| 6 | Special operand (barrier, system register) |

For register operands (type 1), the index is masked as `operand & 0xFFFFFF` (24 bits) to extract the full register ID. Indices 41--44 are architectural predicates that are never allocated.

### SASS Instruction Register Encoding

During final SASS encoding, the register operand encoder (`sub_7BC030`, 814 bytes, 6147 callers) packs register operands into the 128-bit instruction word:

```
Encoded register field (16 bits at variable bit offset):
  bit 0:      presence flag (1 = register present)
  bits 1-4:   register file type (4 bits, 12 values)
  bits 5-14:  register number (10 bits)
```

The 4-bit register file type field in the SASS encoding maps the internal operand type tag to hardware encoding:

| Operand type tag | Encoded value | Register file |
|------------------|---------------|---------------|
| 1 | 0 | R (32-bit) |
| 2 | 1 | R pair (64-bit) |
| 3 | 2 | UR (uniform 32-bit) |
| 4 | 3 | UR pair (uniform 64-bit) |
| 5 | 4 | P (predicate) |
| 6 | 5 | (reserved) |
| 7 | 6 | (reserved) |
| 8 | 7 | B (barrier) |
| 16 | 8 | (extended) |
| 32 | 9 | (extended) |
| 64 | 10 | (extended pair) |
| 128 | 11 | (extended quad) |

The predicate operand encoder (`sub_7BCF00`, 856 bytes, 1657 callers) uses a different format: 2-bit predicate type, 3-bit predicate condition, and 8-bit value. It checks for PT (operand byte[0] == 14) and handles the always-true case.

### Register-Class-to-Hardware Encoding

The function `sub_1B6B250` (2965 bytes, 254 callers) implements the mapping from the compiler's abstract (register\_class, sub\_index) pair to hardware register numbers:

```c
hardware_reg = register_class * 32 + sub_index
```

For example: class 0, index 1 returns 1; class 1, index 1 returns 33; class 2, index 1 returns 65. The guard wrapper `sub_1B73060` (483 callers) returns 0 for the no-register case (class=0, index=0).

The register field writer (`sub_1B72F60`, 483 callers) packs the encoded register number into the 128-bit instruction word with the encoding split across two bitfields:

```c
*(v2 + 12) |= (encoded_reg << 9) & 0x3E00;       // bits [13:9]
*(v2 + 12) |= (encoded_reg << 21) & 0x1C000000;   // bits [28:26]
```

## Register Pressure Tracking

### Scheduling Phase Pressure Counters

The scheduler maintains 9 (or 10) per-block register pressure counters at offsets 48--84 of the per-BB scheduling record (72 bytes per basic block). These counters track live register counts for each register class:

| Counter offset | Register class |
|----------------|---------------|
| +48 (idx 12) | R (general-purpose) |
| +52 (idx 13) | P (predicate) |
| +56 (idx 14) | UR (uniform) |
| +60 (idx 15) | UP (uniform predicate) |
| +64 (idx 16) | B (barrier) |
| +68 (idx 17) | (reserved / extended) |
| +72 (idx 18) | (reserved / extended) |
| +76 (idx 19) | (reserved / extended) |
| +80 (idx 20) | (reserved / extended) |

The spill cost analyzer (`sub_682490`, 14 KB) allocates two stack arrays (`v94[511]` and `v95[538]`) as per-register-class pressure delta arrays. For each instruction, it computes pressure increments and decrements based on the instruction's register operand definitions and uses.

The register pressure coefficient is controlled by knob 740 (double, default 0.045). The pressure curve function uses a piecewise linear model with parameters (4, 2, 6) via `sub_8CE520`.

### Liveness Bitvectors

The Code Object maintains register liveness as bitvectors:

| Offset | Bitvector | Description |
|--------|-----------|-------------|
| +832 | Main register liveness | One bit per virtual register; tracks which registers are live at the current program point |
| +856 | Uniform register liveness | Separate bitvector for UR/UP registers |

These bitvectors are allocated via `sub_BDBAD0` (bitvector allocation, with size = register count + 1 bits) and manipulated via the SSE2-optimized bitvector primitives at `sub_BDBA60` / `sub_BDC180` / `sub_BDCDE0` / `sub_BDC300`.

For each basic block during dependency graph construction (`sub_A0D800`, 39 KB), the per-block liveness is computed by iterating instructions and checking operand types (`(v >> 28) & 7 == 1` for register operands), then updating the bitvector at +832 with set/clear operations.

### Allocator Pressure Arrays

The fat-point allocator (`sub_957160`) uses two 512-DWORD (2048-byte) arrays per allocation round:

| Array | Role |
|-------|------|
| Primary (`v12[512]`) | Per-physical-register interference count |
| Secondary (`v225[512]`) | Tie-breaking cost metric |

Both are zeroed with SSE2 vectorized `_mm_store_si128` loops at the start of each round. For each VR being allocated, the pressure builder (`sub_957020`) walks the VR's constraint list and increments the corresponding physical register slots. The threshold (knob 684, default 50) filters out congested slots.

## ABI Register Reservations

### Reserved Registers

Registers R0--R3 are unconditionally reserved by the ABI across all SM generations. The diagnostic `"Registers 0-3 are reserved by ABI and cannot be used for %s"` fires if they are targeted by parameter assignment or user directives.

### Minimum Register Counts by SM Generation

| SM generation | Value | SM targets | Minimum registers |
|---------------|-------|------------|-------------------|
| 3 | `(sm_target+372) >> 12 == 3` | sm\_35, sm\_37 | (no minimum) |
| 4 | `== 4` | sm\_50 -- sm\_53 | 16 |
| 5 | `== 5` | sm\_60 -- sm\_89 | 16 |
| 9 | `== 9` | sm\_90, sm\_90a | 24 |
| >9 | `> 9` | sm\_100+ | 24 |

Violating the minimum emits warning 7016: `"regcount %d specified below abi_minimum of %d"`.

### Per-Class Hardware Limits

| Class | Limit | Notes |
|-------|-------|-------|
| R | 255 | R0--R254 usable; controlled by `--maxrregcount` and `--register-usage-level` (0--10) |
| UR | 63 | UR0--UR62 usable; sm\_75+ only |
| P | 7 | P0--P6 usable |
| UP | 7 | UP0--UP6 usable; sm\_75+ only |
| B | 16 | B0--B15 |
| UB | 16 | UB0--UB15 |

The `--maxrregcount` CLI option sets a per-function hard ceiling for R registers. The `--register-usage-level` option (0--10, default 5) modulates the register allocation target: level 0 means no restriction, level 10 means minimize register usage as aggressively as possible. The per-class budget at `alloc + 32*class + 884` reflects the interaction between the CLI limit and the optimization level.

The `--device-function-maxrregcount` option overrides the kernel-level limit for device functions when compiling with `-c`.

### Dynamic Register Allocation (setmaxnreg)

sm\_90+ (Hopper and later) supports dynamic register allocation through the `setmaxnreg.inc` and `setmaxnreg.dec` instructions, which dynamically increase or decrease the per-thread register count at runtime. ptxas tracks these as internal states `setmaxreg.try_alloc`, `setmaxreg.alloc`, and `setmaxreg.dealloc`. Multiple diagnostics guard correct usage:

- `"setmaxnreg.dec has register count (%d) which is larger than the largest temporal register count in the program (%d)"`
- `"setmaxreg.dealloc/release has register count (%d) less than launch min target (%d) allowed"`
- `"Potential Performance Loss: 'setmaxnreg' ignored to maintain minimum register requirements."`

## Pair Modes and Coalescing

The pair mode at `vreg+48` bits 20--21 controls how the allocator handles wide registers:

| Pair mode | Value | Behavior |
|-----------|-------|----------|
| Single | 0 | Occupies one physical register slot |
| Lo-half | 1 | Low half of a register pair |
| Double-width | 3 | Occupies two consecutive physical slots |

The allocator computes register consumption via `sub_939CE0`:

```c
consumption = slot + (1 << (pair_mode == 3)) - 1;
// single:  slot + 0  = slot (1 slot)
// double:  slot + 1  = slot+1 (2 slots)
```

The coalescing pass (`sub_9B1200`, 800 lines) eliminates copy instructions by merging the source and destination VRs into the same physical register. The alias chain at `vreg+36` (coalesced parent) is followed during assignment (`sub_94FDD0`) to propagate the physical register through all aliased VRs:

```c
alias = vreg->alias_parent;     // vreg+36
while (alias != NULL) {
    alias->physical_reg = slot;  // alias+68
    alias = alias->alias_parent; // alias+36
}
```

## Register Name Table

The register class name table at `off_21D2400` is a pointer array indexed by the register file type enum (from `vreg+64`). Each entry points to a string: "R", "UR", "P", "UP", "B", "UB", etc. This table is used by diagnostic functions:

- `sub_A4B9F0` (StatsEmitter::emitUndefinedRegWarning): `"Referencing undefined register: %s%d"` where `%s` is `off_21D2400[*(vreg+64)]` and `%d` is `*(vreg+68)` (physical register number).
- `sub_A60B60` (RegisterStatCollector::collectStats, 24 KB): Enumerates ~25 register sub-classes by iterating vtable getters, one per register class. The enumerated classes include R, P, B, UR, UP, UB, SRZ, PT, RZ, and others.
- `"Fatpoint count for entry %s for regclass %s : %d"`: Prints per-function per-class allocation statistics.

## Key Functions

| Address | Size | Function | Description |
|---------|------|----------|-------------|
| `sub_91BF30` | 99 lines | `createVirtualRegister` | Allocates 160-byte VR descriptor, initializes fields, appends to register file array |
| `sub_9446D0` | 29 lines | `shouldSkipRegister` | Returns true for indices 41--44, 39 (architectural specials); checks CSSA phi and exclusion set |
| `sub_A4B8F0` | 248B | `emitInstrRegStats` | Emits `"instr/R-regs: %d instructions, %d R-regs"` |
| `sub_A4B9F0` | 774B | `emitUndefinedRegWarning` | Walks operands backward, formats `"Referencing undefined register: %s%d"` |
| `sub_A60B60` | 4560B | `collectRegisterStats` | Enumerates ~25 register sub-classes via vtable getters |
| `sub_7BC030` | 814B | `encodeRegOperand` | Packs register into SASS instruction: 1-bit presence + 4-bit type + 10-bit number |
| `sub_7BCF00` | 856B | `encodePredOperand` | Packs predicate into SASS: 2-bit type + 3-bit condition + 8-bit value |
| `sub_9B3C20` | -- | `decodeRegOperand` | Decoder helper: extracts register, maps 255 to 1023 (RZ) |
| `sub_9B3D60` | -- | `decodePredOperand` | Decoder helper: extracts predicate, maps 7 to 31 (PT) |
| `sub_1B6B250` | 2965B | `regClassToHardware` | Maps (class, sub\_index) to hardware number: `class * 32 + sub_index` |
| `sub_1B73060` | 19B | `regClassToHardwareGuard` | Guard wrapper: returns 0 for no-register case |
| `sub_1B72F60` | 32B | `writeRegField` | Packs encoded register into instruction word bits [13:9] and [28:26] |
| `sub_112CDA0` | 8.9KB | `encodeRegisterPair` | Maps 40 register pair combinations to 5-bit packed encoding values |
| `sub_939CE0` | 23 lines | `computeConsumption` | Pair-aware register slot consumption counter |
| `sub_94FDD0` | 155 lines | `assignRegister` | Commits physical register assignment, propagates through alias chain |
| `sub_A0D800` | 39KB | `buildDependencyGraph` | Per-block dependency graph with register-to-instruction mapping |
| `sub_A06A60` | 15KB | `scheduleWithPressure` | Per-block scheduling loop tracking live register set bitvector |
| `sub_682490` | 14KB | `computeRegPressureDeltas` | Per-instruction register pressure delta computation |
| `sub_B28E00` | -- | `getRegClass` | Returns register class (1023 = wildcard, 1 = GPR) |
| `sub_B28E10` | -- | `isRegOperand` | Predicate: is this a register operand? |
| `sub_B28E20` | -- | `isPredOperand` | Predicate: is this a predicate operand? |
| `sub_B28E90` | -- | `isUReg` | Predicate: is this a uniform register? |

## Opcode Register Class Table

Every Ori opcode carries an implicit register class contract: which register files its operands may reference, what data widths are valid, and which addressing modes apply. The function `sub_6575D0` (49 KB, `buildEncodingDescriptor`) is the central dispatch that translates each instruction's opcode into a packed encoding descriptor consumed by the SASS encoder.

### Function Signature

```c
// sub_6575D0 -- buildEncodingDescriptor
// a1 = compiler context
// a2 = Ori instruction node pointer
// a3 = output: 4-DWORD packed encoding descriptor
char buildEncodingDescriptor(Context *a1, Instruction *a2, uint32_t *a3);
```

### Architecture

The function is a two-level dispatch:

1. **Outer switch** on the Ori opcode at `*(instr->info + 8)` -- 168 unique case values spanning opcodes 3 (`IADD3`) through 0xF5 (`PIXLD`).

2. **Inner encoding** per opcode (or group): assigns an encoding category ID to `a3[0]`, then calls the bitfield packers to fill `a3[1..2]` with register class attributes.

Two helper functions pack fields into the descriptor:

| Function | Role | Call count | Field ID range |
|----------|------|------------|----------------|
| `sub_917A60` (`packRegClassField`) | Bitfield encoder -- field IDs 91--340 map to specific bit positions in `a3[1]` and `a3[2]` | 112 | 91--340 |
| `sub_A2FF00` (`packOperandField`) | Alternate encoder for operand-level slots (data type, memory space) | 28 | 3--71 |

### Encoding Category Assignment

The encoding category at `a3[0]` selects which SASS instruction format template the downstream per-SM encoder uses. Key mappings (opcode index to encoding category):

| Opcode(s) | SASS mnemonic | Category | Register class summary |
|-----------|--------------|----------|----------------------|
| 3 | `IADD3` | 489 | R dest, R/UR sources, P carry |
| 4 | `BMSK` | 106 | R only |
| 5--6 | `SGXT` / `LOP3` | 490--491 | R dest, R/UR sources |
| 7 | `ISETP` | 59 | P dest, R/UR sources + memory ordering fields |
| 8 | `IABS` | 60 | R dest, R source + memory ordering fields |
| 0x0E--0x10 | `FSET`/`FSEL`/`FSETP` | 510 | R/P dest, FP operation variant |
| 0x11/0x12/0x18 | `FSETP`/`MOV`/`PRMT` | 517 | FP comparison, combine, data width (IDs 288--299) |
| 0x15--0x16 | `P2R`/`R2P` | 524--525 | P-to-R or R-to-P conversion |
| 0x19 | `VOTE` | 526 | R dest, optional memory class |
| 0x1A | `CS2R` variant | 527 | UR source width (494--496), data type from `a2+92` |
| 0x1B | `CS2R_32` | 497 | Source width (494/495/496), predicate flag (ID 270) |
| 0x1E | `IPA` | 494 | Interpolation mode (440--442), flat/smooth (443/444) |
| 0x1F | `MUFU` | 501 | Subfunction (445--447), precision (450--459) |
| 0x20 | `SHF` | 502 | Direction (461--463), source class (464--466), clamp, data type |
| 0x21 | `SHFL` | 503 | Mode (470/471), operand classes (472--482) |
| 0x22--0x23 | `I2I`/`I2IP` | 55/56 | Integer conversion type (23 entries in `dword_2026B20`) |
| 0x28--0x2A | `IPA`/`MUFU` ext | 512 | Extended encoding variants (428--430) |
| 0x2B--0x2C | `F2F`/`F2F_X` | 513 | Conversion direction (432/433), saturation (434/435) |
| 0x2D | `FRND` | 516 | Rounding variant (526), mode (528/529) |
| 0x51--0x53 | `AL2P`, `AL2P_IDX` | 437--438 | Bindless flag (ID 148), predicate (ID 147) |
| 0x54--0x56 | `BMOV_B`/`BMOV_R`/`BMOV` | 423--424 | B-register class |
| 0x64--0x67 | `SETLMEMBASE`/`ATOM` | 156/463 | Atom-vs-red (ID 178), data width (ID 181) |
| 0x68 | `BRX` | 468 | Target (ID 190), call convention (IDs 191--192) |
| 0x6A/0x6C/0x6D | `JMP`/`JMX`/`CALL` | 469 | Control flow target class (ID 176) |
| 0x77--0x79 | `BSSY`/`BREAK`/`BSYNC` | 528--530 | Sync mode (ID 324), variant (ID 325) |
| 0x82 | `NANOTRAP` | 487 | Trap operation class (ID 257), has-source (ID 256) |
| 0x9E--0x9F | Hopper+ instrs | 535--536 | Hopper class A/B (IDs 337--338) |
| 0xAF--0xB2 | `LD`/`ST` variants | 431--446 | Full modifier set: uniform (91), pair (92--102) |
| 0xB8--0xBE | `LDG`/`STG`/`LDL`/`STL` | 449--456 | Cache policy (131), float mode (134), width (131) |
| 0xC1 | Conditional | 10/13 | Branch type (ID 167), divergent (ID 168) |
| 0xC8 | `PRMT` | 24 | Permute selector (ID 65/66) |
| 0xC9--0xD3 | Texture/surface | 61/455 | Texture data type (IDs 17/18), surface (IDs 19--22) |
| 0xD6--0xD7 | `DMMA`/`CVTA` | 515 | Direction (304), predicate (305), data type (306) |
| 0xDA--0xDB | `SUATOM` | 521/533 | Data width (326--331), sync mode (328) |
| 0xDC | `SURED` | 534 | Data width (331), type (335--336), sync (333) |
| 0xE0 | `WGMMA` | 500 | Data type (198), enable (199), barrier (201) |
| 0xF5 | `PIXLD` | 532 | Mode from `dword_2026AA0` (ID 323) |

### Extended Opcode Path (Memory/Atomic Sub-dispatch)

When the opcode falls in the 0xF6--0x10C range (memory/atomic extended instructions), a separate sub-dispatch applies. The function `sub_44AC80` gates entry; `sub_44AC60` and `sub_44AC70` select among three encoding categories:

| Category | Gate function | Meaning |
|----------|--------------|---------|
| 441 | default | Base memory operation |
| 442 | `sub_44AC60` true | Predicated memory variant |
| 443 | `sub_44AC70` true | Extended memory variant |

Within each category, the sub-opcode selects register class fields:

| Sub-opcode | Register class (field 115) | Data width (field 113) |
|------------|---------------------------|----------------------|
| 0xF6/0xFF/0x106 | 69 (class A) | 60 (standard) |
| 0xF7/0x100/0x107 | 71 (class B) | 60 (standard) |
| 0xF8/0x102/0x109 | 0 (default) | 63 (wide) |
| 0xF9/0x103/0x10A | 0 (default) | 61 (narrow) |
| 0xFA/0x104/0x10B | 0 (default) | 62 (medium) |
| 0xFB | 0 (default) | 65 (type A) |
| 0xFC | 0 (default) | 66 (type B) |
| 0xFD | 0 (default) | 68 (type C) |
| 0xFE/0x105/0x10C | 0 (from table) | 64 (from `dword_2026C30`) |
| 0x101/0x108 | 72 (class C) | 60 (standard) |

### Packed Descriptor Layout

The output descriptor `a3` is a 4-DWORD (16-byte) structure:

| DWORD | Content |
|-------|---------|
| `a3[0]` | Encoding category ID (0--542) -- selects SASS format template |
| `a3[1]` | Packed bitfield: memory space (bits 0--3), address type (bits 4--7) |
| `a3[2]` | Packed bitfield: register class attributes (data width, type, modifiers) |
| `a3[3]` | Auxiliary flags (bit 1 = texture scope, bit 29 = special) |
| `a3[4]` | Operand count override (set to 12 for KILL/extended mem ops) |

### Register Class Field Groups

The 112 calls to `packRegClassField` (`sub_917A60`) use field IDs organized into functional groups. Each field ID maps to a specific bit range in the output descriptor via a mask-and-OR encoding:

```c
// Example: field 113 (data width) -- bits 7-9 of a3[2]
case 113:
    val = dword_21DEB20[a3_value - 61];  // 8-entry lookup
    a3[2] = (val << 7) | (a3[2] & 0xFFFFF87F);
    break;

// Example: field 91 (uniform flag) -- bit 16 of a3[2]
case 91:
    a3[2] = ((value == 1) << 16) | (a3[2] & 0xFFFEFFFF);
    break;
```

| Field group | IDs | Bits written | Purpose |
|-------------|-----|-------------|---------|
| Core class | 91--102 | `a3[2]` bits 5--22 | Uniform, pair, predicate, data type, saturate, negate, abs, complement |
| Data width | 113--117 | `a3[2]` bits 0--9 | Width code, uniform-mem, source regclass, type specifier, write-back |
| Load/store | 118--134 | `a3[1]` + `a3[2]` | Memory space, address type, cache policy, atomic op, scope, float mode |
| Texture/surface | 135--165 | `a3[2]` bits 1--31 | Texture type, dimension, LOD mode, ordering, acquire, scope hint |
| Control flow | 167--202 | `a3[2]` bits 1--6 | Branch type, divergent, WGMMA data type/enable/barrier |
| FP/conversion | 230--264 | `a3[2]` various | FP operation, comparison, combine, interpolation, MUFU, SHF, SHFL |
| Extended | 269--299 | `a3[2]` various | CS2R, FSETP, rounding, data type wide, destination regclass |
| Hopper/Blackwell | 304--340 | `a3[2]` various | DMMA, WGMMA, TMA hints, surface sync, Hopper-specific classes |

### Sub-handler Functions

Complex opcode families delegate register class encoding to dedicated sub-functions:

| Function | Opcodes handled | Purpose |
|----------|----------------|---------|
| `sub_650390` | TEX, TLD, texture family | Texture register class (sampler, coordinate, LOD) |
| `sub_650220` | LDG, STG, LD, ST, ATOM, RED | Memory instruction register class |
| `sub_651330` | FMUL (opcode 0x0D) | FP multiply register class |
| `sub_650920` | LEA, special (0x09, 0x72, 0x74, 0x7A, 0x80, 0x81) | LEA / special instruction |
| `sub_650A90` | I2I, F2F, conversions (0x24--0x27, 0xE2--0xEB) | Type conversion register class |
| `sub_652190` | Branch/call (0x13, 0x14, 0x17) | Branch/call register class |
| `sub_653B90` | Misc (0x0C) | Miscellaneous instruction |
| `sub_650C80` | Memory barrier modifiers | Applied when `(a2+56) & 0x4F0` is nonzero |
| `sub_651A90` | Texture modifiers (0x83) | Applied before texture encoding |
| `sub_62D5D0` | Memory space computation | Computes memory space tag from operand types |

### Lookup Tables

The function references 28 static lookup tables that map instruction attribute values to register class encoding values:

| Table | Size | Used by field(s) | Content |
|-------|------|-------------------|---------|
| `dword_21DEB80` | 5 | 94 | Data type encoding |
| `dword_21DEB50` | 3 | 107, 115, 145, 157, 165 | 3-value encoding (reused across 5 fields) |
| `dword_21DEB20` | 8 | 113 | Data width code |
| `dword_21DEB00` | 7 | 116, 126, 131, 170 | Type encoding (reused across 4 fields) |
| `dword_21DEAE0` | 5 | 119/123, 136, 143, 159 | Variant table (reused across 4 fields) |
| `dword_21DEAA0` | 13 | 120 | Memory space code |
| `dword_21DEA60` | 10 | 121, 135/151 | Address/texture type |
| `dword_21DEA20` | 15 | 124/125 | Reduction type |
| `dword_21DE9F0` | 6 | 129/130, 150 | Scope code |
| `dword_2026C30` | 6 | 116 (ext path) | Sub-opcode to data type |
| `dword_2026C80` | 20 | 165 (surface) | Surface operation codes |
| `dword_2026E20` | 17 | 286 | Data type (wide) |
| `dword_2026AC0` | 16 | 198 | WGMMA data type |
| `dword_2026B20` | 23 | I2I conversion | Integer conversion type |

## Related Pages

- [Ori IR Overview](./overview.md) -- register files in the context of the full IR
- [Instructions](./instructions.md) -- packed operand format and opcode encoding
- [Allocator Architecture](../regalloc/overview.md) -- the 7-class fat-point allocator
- [Fat-Point Algorithm](../regalloc/algorithm.md) -- pressure arrays, constraint types, selection loop
- [GPU ABI](../regalloc/abi.md) -- reserved registers, parameter passing, return address
- [Spilling](../regalloc/spilling.md) -- spill/reload for each register class
- [Scheduler](../scheduling/overview.md) -- 9 per-block pressure counters
- [SASS Encoding](../codegen/encoding.md) -- how the descriptor drives instruction word layout
