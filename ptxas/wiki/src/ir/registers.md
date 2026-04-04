# Register Model (R / UR / P / UP)

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

The fat-point allocator processes 7 register classes. Class 0 is the cross-class constraint propagation channel and is skipped in the main allocation loop. Classes 1--6 are allocated independently, in order:

| Class ID | Name | Width | HW limit | VR type field | Description |
|----------|------|-------|----------|---------------|-------------|
| 0 | (unified) | -- | -- | -- | Cross-class constraint propagation (skipped) |
| 1 | R | 32-bit | 255 | 1 | General-purpose registers (R0--R254) |
| 2 | P | 1-bit | 7 | 3 | Predicate registers (P0--P6) |
| 3 | B | 1-bit | 16 | 9 | Barrier registers (B0--B15) |
| 4 | UR | 32-bit | 63 | 1 | Uniform general-purpose (UR0--UR62) |
| 5 | UP | 1-bit | 7 | 3 | Uniform predicate (UP0--UP6) |
| 6 | UB | 1-bit | 16 | 9 | Uniform barrier (UB0--UB15) |

The VR type field at `vreg+64` distinguishes GPR (1), predicate (3), and barrier (9) within each class. The allocator class at `vreg+12` determines which of the 7 per-class allocation passes handles a given virtual register.

Per-class state is initialized via the target descriptor vtable call `vtable[896](alloc_state, class_id)`, which populates per-class register file descriptors at `alloc[114..156]` (three 8-byte entries per class: range min, range max, and file state pointer).

### Barrier Registers

Barrier registers (B and UB) are a distinct register file used by the `BAR`, `DEPBAR`, `BSSY`, and `BSYNC` instructions for warp-level and CTA-level synchronization. B0--B15 are the non-uniform barrier registers; UB0--UB15 are the uniform variant. The allocator handles them as class 3 (B) and class 6 (UB).

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

| Value | File | Description |
|-------|------|-------------|
| 1 | R | General-purpose register (32-bit) |
| 2 | R | General-purpose register (alternate, used in stat collector) |
| 3 | UR | Uniform register (32-bit) |
| 4 | -- | Extended / uniform (triggers flag update in constructor) |
| 5 | P | Predicate register (1-bit) |
| 6 | R | General register (alternate classification) |
| 7 | P | Predicate register (alternate, physical = 0 at init) |
| 9 | B | Barrier register |
| 10 | R2 | Extended register pair (64-bit, two consecutive R regs) |
| 11 | R4 | Extended register quad (128-bit, four consecutive R regs) |

The stat collector at `sub_A60B60` (24 KB) enumerates approximately 25 register sub-classes including R, P, B, UR, UP, UB, SRZ, PT, RZ, and others by iterating vtable getter functions per register class.

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

## Related Pages

- [Ori IR Overview](./overview.md) -- register files in the context of the full IR
- [Instructions](./instructions.md) -- packed operand format and opcode encoding
- [Allocator Architecture](../regalloc/overview.md) -- the 7-class fat-point allocator
- [Fat-Point Algorithm](../regalloc/algorithm.md) -- pressure arrays, constraint types, selection loop
- [GPU ABI](../regalloc/abi.md) -- reserved registers, parameter passing, return address
- [Spilling](../regalloc/spilling.md) -- spill/reload for each register class
- [Scheduler](../scheduling/overview.md) -- 9 per-block pressure counters
