# Register File & Occupancy Budget Tables

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base 0x400000 (non-PIE).*

Browsable data dump for the [register allocator](../../regalloc/overview.md)'s register-file model and occupancy budget. The consuming logic is on the [allocator architecture](../../regalloc/overview.md) and [fat-point algorithm](../../regalloc/algorithm.md) pages.

## Register File Limits

The architectural register-file header (consumed by the per-class init callback `vtable[896]`):

| Field | Value | Meaning |
|---|---|---|
| hdr[0] | 120 | GPR banks / count |
| hdr[1] | 120 | GPR count max |
| hdr[2] | 64 | predicate registers |
| hdr[3] | 256 | uniform registers |
| hdr[4] | 32 | barriers |
| hdr[5] | 8 | barrier alloc unit |
| hdr[6] | 32 | convergence barriers |
| hdr[7] | 4 | barrier alloc (b_alloc) |
| hdr[8] | 64 | datapath width |

The architectural per-thread GPR ceiling reported to the user remains 255; the 120 figure above is the per-bank count used in the warp-budget table below.

## Register Classes

Seven classes (0 skipped in the main loop). Class 6 is the Tensor/Accumulator file; classes 3 and 6 are the only ones eligible for coalescing.

| ID | Name | Width | Arch cap | Description |
|---|---|---|---|---|
| 0 | — | — | — | unified / cross-class (skipped) |
| 1 | R | 32-bit | 255 | general-purpose (R0–R254) |
| 2 | R (alt) | 32-bit | 255 | GPR variant (RZ sentinel / stat-collector) |
| 3 | UR | 32-bit | 63 | uniform GPR (UR0–UR62) — coalescing-eligible |
| 4 | UR (ext) | 32-bit | 63 | uniform GPR variant |
| 5 | P / UP | 1-bit | 7 | predicate (P0–P6, UP0–UP6) |
| 6 | Tensor/Acc | 32-bit | per-arch | MMA/WGMMA accumulator — coalescing-eligible; sub-register selectors 5/6/7 are accumulator lane indices |

Class records per SM generation: sm_10x has 97 records across 14 distinct classes (0–13); sm_8x has 24 records / 11 classes (0–10); sm_7x has 150 records / 17 classes (0–16). The class-record stride is 64 bytes; the extended-flag-2 count (the sub-variant pairs used during merge eligibility) is 9 for sm_10x, 0 for sm_8x, 20 for sm_7x.

## Warp Budget Table (`0x21CE6A0` & `0x21CEE60`)

The two budget tables map a per-thread register count to an allocatable-GPR count and a warps-per-scheduler figure. Both use warp granule 64, datapath 64, 32 warps/scheduler, alloc granularity 4. Table `0x21CE6A0` (mode_flag 8) is the GPR path; `0x21CEE60` (mode_flag 1, datapath 256) is the uniform path.

| reg/thread | GPR allocatable (`0x21CE6A0`) | UR allocatable (`0x21CEE60`) |
|---|---|---|
| 8 | 4 | 2 |
| 16 | 8 | 4 |
| 24 | 12 | 6 |
| 32 | 16 | 8 |
| 48 | 24 | 12 |
| 64 | 32 | 16 |
| 80 | 40 | 20 |
| 96 | 48 | 24 |
| 112 | 56 | 28 |
| 128 | 64 | 32 |
| 144 | 72 | 36 |
| 160 | 80 | 40 |
| 176 | 88 | 44 |
| 192 | 96 | 48 |
| 208 | 104 | 52 |
| 224 | 112 | 56 |
| 240 | 120 | 60 |
| 256 | 128 | 64 |

The GPR path allocatable count is `reg_per_thread / 2` (the 120-bank file is shared two-ways per the datapath); the UR path is `reg_per_thread / 4`.

## Occupancy Constants (`0x229C400`)

Eight 16-byte rodata records driving `max_warps(regs)`:

| Label | VA | dwords | Interpretation |
|---|---|---|---|
| sm90 regfile params | `0x229C400` | 6,128,32768,255 | alloc_granularity_log2=6; warps/subpartition=128; half_reg_file=32768; max_reg=255 |
| sm90 granularity | `0x229C410` | 63,7,7,16 | granularity_mask=63; round_shift=7; max_warps=16 |
| barrier / CTA | `0x229C420` | 4,2048,8,2 | barrier_alloc=4; regfile/part=2048; barriers/warp=8; cta_dim=2 |
| sm60/sm70 max warps | `0x229C430` | 32,32,64,32 | warp_alloc=32; gran=32; max_warps/sm=64; blocks=32 |
| sm53–sm62 regfile | `0x229C440` | 6,128,65536,255 | alloc_granularity_log2=6; warps=128; half_reg_file=65536; max_reg=255 |
| sm35/sm37 max warps | `0x229C450` | 32,32,48,16 | warp_alloc=32; gran=32; max_warps=48; blocks=16 |
| sm3x/sm5x max warps | `0x229C460` | 32,32,48,24 | warp_alloc=32; gran=32; max_warps=48; blocks=24 |
| sm70+ granularity | `0x229C470` | 80,7,7,16 | granularity=80; round=7; max_warps=16 |

Occupancy formula (`sub_A99FE0`): `max_warps(regs) = (-granularity_mask & (2 * half_reg_file_size / regs)) - warp_offset`. The negation rounds the warp count down to the hardware allocation-granularity boundary.

## Operand Register-Count Matrices

The per-operand register-count cost matrices (consumed during budget evaluation): `reg_base_sizes` = `[48, 50, 38, 41, 46, 46, 0, 0]` (per-class base register footprint). `matrix_a` and `matrix_b` are stepped cost tables; `matrix_b` floors at the **ABI minimum of 13** (entries 22–27 = 13) before ramping by 4 per step. The ABI floor of 13 GPRs is the same value enforced by `sub_4394D0`'s `> 13` threshold.

## Register-ID Arrays

The bank-indexed register-ID arrays (`bank << 16 | reg`) used to enumerate the physical register namespace per class:

| VA | count | bank | range | step | kind |
|---|---|---|---|---|---|
| `0x21EE460` | 240 | 1 | 640–879 | 1 | GP |
| `0x21EE820` | 34 | 1 | 576–609 | 1 | GP |
| `0x21EEBE0` | 240 | 1 | 400–639 | 1 | GP |
| `0x21EEFA0` | 32 | 1 | 160–191 | 1 | GP |
| `0x21EF020` | 136 | 2 | 192–462 | 2 | paired/wide (64-bit) |
| `0x21EF260` | 16 | 1 | 464–479 | 1 | GP |
| `0x21EF2A0` | 48 | 2 | 480–574 | 2 | paired/wide (64-bit) |
| `0x21EF360` | 240 | 1 | 160–399 | 1 | GP |
| `0x21EF720` | 168 | 1 | 640–807 | 1 | GP |
| `0x21EF9E0` | 64 | 1 | 808–871 | 1 | GP |

Step-2 arrays are the 64-bit pair namespace (bank 2); step-1 arrays are single 32-bit registers (bank 1).

## Cross-References

- [Allocator Architecture](../../regalloc/overview.md) — register classes, occupancy budget model, allocator state object
- [Fat-Point Algorithm](../../regalloc/algorithm.md) — pressure histogram, class-6 selector, coalescing model
- [Spilling](../../regalloc/spilling.md) — spill cost model and memory targets
