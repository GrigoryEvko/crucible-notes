# SM80-88 Ampere

The Ampere family in nvlink v13.0.88 covers four compute capabilities -- sm_80, sm_86, sm_87, and sm_88 -- all sharing the ISA class string `"Ampere"`, the same ISel backend at `0xCA0000`--`0xDA0000` (1 MB), and the same 128-bit SASS instruction encoding introduced by Turing. The sm_88 variant is new in CUDA 13.0. None of the four Ampere targets carry `'a'` or `'f'` suffix variants (unlike Blackwell targets), so the profile database contains exactly 12 Ampere profile entries: 4 real, 4 virtual, and 4 LTO.

For general Ampere architecture details (hardware specs, PTX ISA requirements, codegen factory encoding, scheduler parameters, latency tables), see the [ptxas wiki: Turing/Ampere](../../ptxas/targets/turing-ampere.html). For cicc-level feature gates and optimizer flag configuration, see the [cicc wiki: SM70-89](../../cicc/targets/sm70-89.html). This page focuses on the nvlink-internal per-sub-architecture data: profile registration, capability vectors, dispatch table function pointers, and the ISel backend shared by all four.

## Per-Architecture Profiles

### Profile Registration in sub_484F50

The profile database initializer `sub_484F50` (53,974 bytes) registers the four Ampere architectures in numeric order. Each architecture produces three profile objects via `sub_484DB0` (real `sm_`, virtual `compute_`, LTO `lto_`), which are inserted into the global hash map at `qword_2A5F8D8`. The Ampere block spans lines 288--462 of the decompiled source.

| SM | Registration Lines | `sub_484DB0` Args | Variables |
|---|---|---|---|
| sm_80 | 288--331 | `(0, 0, "sm_80", "sm_80", "Ampere", "-D__CUDA_ARCH__=800", "sm_80")` | `v14` (real), `v15` (virtual) |
| sm_86 | 332--377 | `(0, 0, "sm_86", "sm_86", "Ampere", "-D__CUDA_ARCH__=860", "sm_86")` | `v22` (real), `v23` (virtual) |
| sm_87 | 378--420 | `(0, 0, "sm_87", "sm_87", "Ampere", "-D__CUDA_ARCH__=870", "sm_87")` | `v31` (real), `v32` (virtual) |
| sm_88 | 421--462 | `(0, 0, "sm_88", "sm_88", "Ampere", "-D__CUDA_ARCH__=880", "sm_88")` | `v39` (real), `v40` (virtual) |

The registration pattern for each is identical:

```c
// 1. Create real profile (is_virtual=0, is_lto=0)
sm_80 = ArchProfile::create(0, 0, "sm_80", "sm_80", "Ampere",
                            "-D__CUDA_ARCH__=800", "sm_80");

// 2. Create virtual profile (is_virtual=1, is_lto=0)
compute_80 = ArchProfile::create(1, 0, "compute_80", "compute_80", "Ampere",
                                 "-D__CUDA_ARCH__=800", "compute_80");

// 3. Link real <-> virtual
sm_80->virtual_ptr = compute_80;       // offset +72
compute_80->virtual_ptr = compute_80;  // self-ref

// 4. Insert into hash map
LinkerHash::insert(qword_2A5F8D8, "sm_80", sm_80);
LinkerHash::insert(qword_2A5F8D8, "compute_80", compute_80);

// 5. Create LTO profile (is_virtual=1, is_lto=1)
lto_80 = ArchProfile::create(1, 1, "lto_80", "compute_80", NULL,
                             "-D__CUDA_ARCH__=800", "lto_80");
lto_80->virtual_ptr = compute_80;
LinkerHash::insert(qword_2A5F8D8, "lto_80", lto_80);

// 6. Build compatibility lists
list_append(compute_80->compat_list_2, sm_80);    // offset +64
list_append(sm_80->compat_list_2, compute_80);
list_append(sm_80->compat_list_1, sm_80);          // offset +56: family
list_append(sm_80->compat_list_0, sm_80);          // offset +48: cross-variant

// 7. Copy capability vectors from rodata
sm_80->capability[0] = xmmword_1D40F10;   // offset +80
sm_80->capability[1] = xmmword_1D40F40;   // offset +96
sm_80->capability[2] = xmmword_1D40F30;   // offset +112
```

After registering each sub-architecture, `sub_484F50` links it into the Ampere family chain. sm_86 is appended to sm_80's family list (`compat_list_0` and `compat_list_1`), sm_87 and sm_88 follow the same chaining to sm_86's lists (which transitively connect back to sm_80).

The `dword_2A5F8CC = 80` assignment (line 326) sets the default minimum architecture to sm_80 -- any link operation that does not specify an explicit `--arch` flag defaults to this value.

### Architecture Identity Matrix

| Property | sm_80 | sm_86 | sm_87 | sm_88 |
|---|---|---|---|---|
| ISA class string | `"Ampere"` | `"Ampere"` | `"Ampere"` | `"Ampere"` |
| Family string | `"Ampere"` | `"Ampere"` | `"Ampere"` | `"Ampere"` |
| `__CUDA_ARCH__` | `800` | `860` | `870` | `880` |
| Preprocessor define | `-D__CUDA_ARCH__=800` | `-D__CUDA_ARCH__=860` | `-D__CUDA_ARCH__=870` | `-D__CUDA_ARCH__=880` |
| Suffix variants | None | None | None | None |
| Profile byte[3] (finalization class) | 0 | 0 | 0 | 0 |
| Products | GA100 (A100, A30) | GA10x (RTX 3090/3080/3070/3060, A40, A16) | GA10B (Jetson Orin AGX/NX/Nano) | Undocumented (CUDA 13.0) |
| Same-decade group (SM/10) | 8 | 8 | 8 | 8 |

All four share same-decade group 8, which also includes sm_89 (Ada). This means code compiled for sm_80 is compatible with any target in the 80--89 range via the same-decade rule (see the [Compatibility](compatibility.md) page).

### Capability Vectors

Each profile stores three 128-bit XMM vectors at offsets +80, +96, and +112. These encode hardware capability bitmasks checked by the finalization pipeline (`sub_470DA0`). The vectors are loaded from rodata constants in `sub_484F50`:

| Architecture | Vec 0 (offset +80) | Vec 1 (offset +96) | Vec 2 (offset +112) | Source Lines |
|---|---|---|---|---|
| sm_75 (Turing) | `xmmword_1D40F10` | `xmmword_1D40F20` | `xmmword_1D40F30` | 283--287 |
| **sm_80** | `xmmword_1D40F10` | **`xmmword_1D40F40`** | `xmmword_1D40F30` | 325--331 |
| **sm_86** | `xmmword_1D40F10` | **`xmmword_1D40F50`** | `xmmword_1D40F30` | 370--374 |
| **sm_87** | `xmmword_1D40F10` | `xmmword_1D40F50` (copied from sm_86) | `xmmword_1D40F30` | 416--420 |
| **sm_88** | `xmmword_1D40F10` | `xmmword_1D40F50` (copied from sm_87) | `xmmword_1D40F30` | 458--462 |
| sm_89 (Ada) | `xmmword_1D40F10` | `xmmword_1D40F60` | `xmmword_1D40F30` | 499--505 |

The rodata symbols encode these capability tiers:

| Symbol | Role | Architectures Using It |
|---|---|---|
| `xmmword_1D40F10` | Universal base (Vec 0) | All architectures sm_75--sm_121 |
| `xmmword_1D40F20` | Turing feature set (Vec 1) | sm_75 only |
| `xmmword_1D40F30` | Pre-Blackwell ISA version (Vec 2) | sm_75 through sm_90a |
| `xmmword_1D40F40` | Ampere-base feature set (Vec 1) | sm_80, sm_90, sm_100, sm_103 |
| `xmmword_1D40F50` | Ampere-86+ feature set (Vec 1) | sm_86, sm_87, sm_88 |
| `xmmword_1D40F60` | Ada/Thor/RTX-50 feature set (Vec 1) | sm_89, sm_110, sm_120, sm_121 |
| `xmmword_1D40F70` | Blackwell ISA version (Vec 2) | sm_100 onward |

Key observations:

- **Vec 0 is constant** across all architectures. It encodes the universal baseline capabilities.
- **Vec 1 differentiates sub-architectures.** sm_80 gets `xmmword_1D40F40` (Ampere base), while sm_86/87/88 share `xmmword_1D40F50` (extended Ampere). The sm_86 value propagates to sm_87 and sm_88 through copy chains (`v211 = v29` at line 375; `v210 = v37` at line 418), not through independent rodata loads. This means sm_86, sm_87, and sm_88 are capability-identical from the finalization pipeline's perspective.
- **Vec 2 is constant** within the pre-Blackwell group. All Ampere targets use `xmmword_1D40F30`.
- **The Ampere-to-Ada boundary** is marked by the Vec 1 change from `xmmword_1D40F50` (sm_86/87/88) to `xmmword_1D40F60` (sm_89).

### What Distinguishes Each Sub-Architecture

**sm_80 (GA100 -- datacenter Ampere).** The base Ampere target and generational anchor. It uses `xmmword_1D40F40` for Vec 1, which differs from the sm_86+ value `xmmword_1D40F50`. This means sm_80's capability mask is a strict subset of sm_86's -- code finalized for sm_86 is not guaranteed to be re-finalizable for sm_80 without capability mask verification. sm_80's codegen factory is 28673 (`0x7001`), placing it at sub-variant 1 within generation 7. In the ptxas scheduler, sm_80 falls through to the default variant (0 or 1) and uses baseline latency tables.

**sm_86 (GA10x -- consumer/enterprise Ampere).** Extends sm_80 with `xmmword_1D40F50` capability bits. The codegen factory is 28674 (`0x7002`, sub-variant 2). In the ptxas scheduler, sm_86 maps to sub-architecture variant 2, receiving tuned scheduling parameters distinct from sm_80's baseline. The ptxas latency table for sm_86 (`sub_8E7D80`, 4.4 KB) is the largest in the Ampere family, reflecting the different pipeline characteristics of the RTX 30xx consumer die versus the A100 datacenter die.

**sm_87 (GA10B -- Jetson Orin).** Capability-identical to sm_86 (same Vec 1 `xmmword_1D40F50`, copied at line 418 via `v37 = _mm_load_si128(&v211)` where `v211` holds sm_86's vector). The codegen factory is 28675 (`0x7003`, sub-variant 3). The ptxas scheduler maps it to variant 3, giving Orin its own latency profile (`sub_8E8070`, 3.5 KB). This is the only material binary difference between sm_86 and sm_87 in nvlink -- the capability mask, ISel patterns, and encoding pipeline are identical.

**sm_88 (undocumented -- CUDA 13.0).** Capability-identical to sm_86 and sm_87 (same Vec 1, copied at line 458 via the sm_87 chain). The codegen factory is 28676 (`0x7004`, sub-variant 4). The ptxas scheduler maps it to variant 4, shared with sm_110 (Jetson Thor). No separate latency table was found in the ptxas sweep data -- sm_88 may share sm_86's or sm_87's table. No public product ships on sm_88; it may represent an unreleased Ampere derivative or internal validation target.

## Dispatch Table (sub_15C0CE0)

The dispatch table initializer `sub_15C0CE0` registers seven callback function pointers per architecture into hash maps (`qword_2A644B8` through `qword_2A64488`). Each callback serves a specific role in the embedded compilation pipeline.

### Slot Assignments

| Slot | Hash Map | sm_75 (Turing) | sm_80 | sm_86 | sm_87 | sm_88 |
|---|---|---|---|---|---|---|
| B8 | Pre-compilation | `sub_15C2AA0` | `sub_15C2BF0` | `sub_15C2C80` | `sub_15C2E30` | `sub_15C2DA0` |
| B0 | Compilation | `sub_15C2A70` | `sub_15C2BC0` | `sub_15C2CB0` | `sub_15C2D10` | `sub_15C2DD0` |
| A8 | Backend init | `sub_15C3210` | `sub_15C3310` | `sub_15C3B60` | `sub_15C3C60` | `sub_15C3A60` |
| A0 | Internal version | `byte_2A5EE40` | `byte_2A5EE3C` | `byte_2A5EE38` | `byte_2A5EE34` | `byte_2A5EE30` |
| 90 | Perf-stats | `sub_15C1C80` | `sub_15C1EB0` | `sub_15C1EF0` | `sub_15C1FD0` | `sub_15C1E30` |
| 88 | Resource calc | `sub_15C2610` | `sub_15C28B0` | `sub_15C1FF0` | `sub_15C2990` | `sub_15C2530` |

Each `compute_` prefix variant receives the same internal version constant as its real counterpart (e.g., `compute_80` = `byte_2A5EE3C`, `compute_86` = `byte_2A5EE38`).

### Backend Init Functions (Slot A8)

The A8 slot handlers are the only callbacks with architecturally significant differences between Ampere sub-architectures. Each allocates a per-function codegen context via `sub_189F230(a3)` and writes a per-SM codegen factory value at offset +348:

| SM | A8 Handler | Factory Value | Hex | Encoding |
|---|---|---|---|---|
| sm_75 | `sub_15C3210` | 24577 | `0x6001` | Generation 6, sub-variant 1 |
| sm_80 | `sub_15C3310` | 28673 | `0x7001` | Generation 7, sub-variant 1 |
| sm_86 | `sub_15C3B60` | 28674 | `0x7002` | Generation 7, sub-variant 2 |
| sm_87 | `sub_15C3C60` | 28675 | `0x7003` | Generation 7, sub-variant 3 |
| sm_88 | `sub_15C3A60` | 28676 | `0x7004` | Generation 7, sub-variant 4 |

The codegen factory encodes `(isa_generation << 12) | sub_variant`. All four Ampere targets are generation 7 (bits 12--15 = 0x7), differing only in the low 12-bit sub-variant field. The factory value controls:

1. **Scheduler profile selection** in `sub_8E4400`: all four fall into the 7-warp / 208-dispatch-slot bucket (factory range 24577--28676, threshold <= 32767).
2. **Sub-architecture variant** for latency tuning: sm_80 gets default variant, sm_86 gets variant 2, sm_87 gets variant 3, sm_88 gets variant 4.
3. **Instruction encoding table** selection: each sub-variant can enable/disable specific instruction forms.

All other fields at +344 (shared memory config base, `0x100000` = 1 MB) and the remaining context fields are identical across all four Ampere handlers.

### Internal Version Numbers

The internal version numbers at `qword_2A644A0` are stored as 4-byte integers at decreasing addresses, each 4 bytes apart. From the version mapping table in the [SM89 Ada page](sm89-ada.md):

| Address | SM | Internal Version | Notes |
|---|---|---|---|
| `byte_2A5EE40` | sm_75 | 14 | Turing baseline |
| `byte_2A5EE3C` | sm_80 | (inferred 25) | Ampere base |
| `byte_2A5EE38` | sm_86 | (inferred 26) | First sub-arch > 26 threshold triggers |
| `byte_2A5EE34` | sm_87 | (inferred 27) | Orin |
| `byte_2A5EE30` | sm_88 | (inferred 28) | Undocumented |
| `byte_2A5EE2C` | sm_89 | 29 | Ada (confirmed) |

The specific internal versions for sm_80--sm_88 are inferred from the address spacing pattern and the known threshold at `*(a1+376) > 26` (sm_86+, confirmed in the compilation driver `sub_1112F30` line 991). The sm_89 value of 29 is confirmed from decompiled code. The gap between sm_75 (internal 14) and sm_80 (inferred 25) reflects deprecated architectures in the 15--24 range that were removed from the profile database but whose internal version slots remain allocated.

## Ampere vs Turing: Generational Differences

The transition from Turing (sm_75, generation 6) to Ampere (sm_80, generation 7) manifests in nvlink at several levels. For hardware and ISA-level details, see [ptxas wiki: Turing/Ampere](../../ptxas/targets/turing-ampere.html). Within nvlink specifically:

### Profile-Level Changes

| Aspect | sm_75 (Turing) | sm_80 (Ampere) |
|---|---|---|
| ISA class string | `"Turing"` | `"Ampere"` |
| ISA generation | 6 | 7 |
| Codegen factory | 24577 (`0x6001`) | 28673 (`0x7001`) |
| Vec 1 capability | `xmmword_1D40F20` | `xmmword_1D40F40` |
| Same-decade group | 7 | 8 |
| Default arch flag | -- | `dword_2A5F8CC = 80` |

### Capability Boundary

The Vec 1 change from `xmmword_1D40F20` (Turing) to `xmmword_1D40F40` (Ampere) marks a hard capability boundary. Code finalized for sm_80 cannot be re-finalized for sm_75 -- the capability mask comparison in `sub_470DA0` will fail because Ampere capabilities are a superset of Turing capabilities. The reverse direction (sm_75 code on sm_80 targets) is permitted by the same-decade rule only if both are in the same family linked list, which they are not (different decades: 7 vs 8).

### ISel Backend Independence

Despite sharing the same 128-bit instruction encoding format, sm_75 and sm_80 use completely separate ISel backends:

| Property | SM75 (Turing) | SM80 (Ampere) |
|---|---|---|
| Address range | `0xF16000`--`0x100C000` (984 KB) | `0xCA0000`--`0xDA0000` (1 MB) |
| Mega-hub | `sub_FBB810` (280 KB) | `sub_D5FD70` (239 KB) |
| Pattern matchers | 276 | 259 |
| SASS opcodes | -- | 19 (in LTO subset) |

The Ampere ISel is slightly smaller than Turing's despite being a later generation. The reduction in pattern matchers (276 to 259) reflects instruction set refinement rather than feature removal -- Ampere reorganized and consolidated some multi-variant patterns.

### Feature Gates in cicc and ptxas

From the [cicc wiki: SM70-89](../../cicc/targets/sm70-89.html), the Ampere-specific features enabled at the sm_80 threshold include:

- **C++20 `__VA_OPT__` support** via `unk_4D041B8`
- **`llvm.nvvm.branch.if.convergent`** intrinsic (sm_80+, distinct from the sm_70+ `branch.if.all.convergent`)
- **L2 cache hint atomics** (PTX 7.3+)
- **cp.async.bulk patterns** for asynchronous memory copy

From the [ptxas wiki: Turing/Ampere](../../ptxas/targets/turing-ampere.html), the sm_82 SASS opcode boundary defines 22 Ampere-era opcode slots (indices 172--193) covering sparse MMA, binary tensor core shapes, FP64 tensor MMA, async copy infrastructure, and warp-wide reduction.

## Family Linkage

The Ampere family linked list built by `sub_484F50` chains all four sub-architectures through `compat_list_1` (offset +56) and `compat_list_0` (offset +48):

```text
sm_80.compat_list_1 -> { sm_80, sm_86, sm_87, sm_88 }
sm_86.compat_list_1 -> { sm_86 }
    also: sm_86 appended to sm_80's compat_list_0 and compat_list_1
sm_87.compat_list_1 -> { sm_87 }
    also: sm_87 appended to sm_86's compat_list_0 and compat_list_1
sm_88.compat_list_1 -> { sm_88 }
    also: sm_88 appended to sm_87's compat_list_0 and compat_list_1
```

Additionally, sm_89 (Ada) links into the Ampere family chain:

```c
// sub_484F50 lines 507--510:
list_append(sm_80->compat_list_0, sm_89);       // v14[3].m128i_i64[0] = sm_80's list
list_append(sm_80->compat_list_1, sm_89);       // v14[3].m128i_i64[1] = sm_80's list
list_append(sm_86->compat_list_0, sm_89);       // v22[3].m128i_i64[0] = sm_86's list
list_append(sm_86->compat_list_1, sm_89);       // v22[3].m128i_i64[1] = sm_86's list
```

This means sm_89 (Ada) is in the same compatibility family as the Ampere targets. The same-decade rule (80/10 = 89/10 = 8) already ensures this, but the explicit linkage provides direct traversal without arithmetic.

### Worked Compatibility Example: sm_80 vs sm_86

Consider linking a cubin compiled for `sm_80` into a final binary targeting `sm_86`.

1. **`sub_4878A0`** parses both architecture strings: `record_a = {arch_number=80, is_virtual=0, has_suffix=0}`, `record_b = {arch_number=86, is_virtual=0, has_suffix=0}`.
2. Neither record is virtual, so the function does not reject on the "both virtual" check.
3. `record_a.has_suffix` is 0, so the function enters Case 1: `profile_family_match(sm_80.family_list, sm_86)`.
4. `sub_465450` traverses sm_80's `compat_list_1`: `{ sm_80, sm_86, sm_87, sm_88 }`. sm_86 is found in the list.
5. **Result: compatible.** The linker accepts the sm_80 cubin into the sm_86 link.

The reverse direction (sm_86 cubin targeting sm_80) follows a different path:

1. Parse: `record_a = {86, ...}`, `record_b = {80, ...}`.
2. Case 1: `profile_family_match(sm_86.family_list, sm_80)`.
3. sm_86's `compat_list_1` contains `{ sm_86 }` -- it does **not** contain sm_80.
4. **Result: incompatible.** sm_86 code cannot be linked for sm_80.

This asymmetry reflects the forward-compatibility guarantee: code compiled for a lower SM within a family runs on higher SMs, but not the reverse.

However, capability mask verification at finalization time (`sub_470DA0`) adds a second check: even if the linker accepts the cubin, re-finalization will compare the capability vectors. Since sm_80 uses `xmmword_1D40F40` (Vec 1) and sm_86 uses `xmmword_1D40F50` (Vec 1), the sm_86 target has a superset of sm_80's capabilities. The finalization check passes for sm_80-compiled code targeting sm_86 but would fail for sm_86-compiled code targeting sm_80 if any sm_86-specific capability bits were used.

## ISel Backend (0xCA0000--0xDA0000)

All four Ampere sub-architectures share a single ISel backend. The backend is variant-agnostic -- it produces identical SASS encoding for sm_80, sm_86, sm_87, and sm_88. Any differences between sub-architectures are resolved upstream in the dispatch table (slot A8 codegen factory) and downstream in the scheduler (latency tables), not within the ISel code itself.

### Address Map

| Range | Size | Subsystem | Functions |
|---|---|---|---|
| `0xCA0000`--`0xCDC000` | 240 KB | Operand emission + bitfield packing | 137 |
| `0xCDD5F0`--`0xCDD690` | <1 KB | Operand type predicates | 15 |
| `0xCE2000`--`0xD5FD70` | 510 KB | ISel pattern matchers | 259 |
| **`0xD5FD70`** | **239 KB** | **ISel mega-hub dispatcher** | **1** |
| `0xD9A400`--`0xDA0000` | 23 KB | Binary instruction encoding | 17 |

Total analyzed functions (>3 KB): 413. The mega-hub at `sub_D5FD70` is 239 KB (55,985 instructions, 1,340 callees) and too large for Hex-Rays to decompile. It is the third-largest function in the binary after the SM75 mega-hub (`sub_FBB810`, 280 KB) and the `cuda_builtin_prototype_generator` (`sub_15B86A0`, 345 KB).

### Three-Phase Pipeline

Every IR instruction processed by this backend passes through three phases in sequence: instruction selection, operand emission, and binary encoding. The output is a 128-bit SASS instruction word ready for insertion into the device ELF `.text` section.

**Phase 1: Instruction Selection.** The mega-hub `sub_D5FD70` iterates all 259 pattern matcher functions. Each matcher receives `(ctx, ir_node, &pattern_id, &priority)` and tests a single candidate encoding. The highest-priority match wins.

```c
for each pattern_matcher in sm80_pattern_table[0..258]:
    pattern_matcher(ctx, ir_node, &pattern_id, &priority)
    if priority > best_priority:
        best_priority = priority
        best_id = pattern_id
sm80_emitter_table[best_id](ctx, ir_node)
```

Each pattern matcher queries: instruction attributes via `sub_A49150(ctx, ir_node, slot_id)` (up to 12 attribute checks per pattern), definition/use operand counts via `sub_530FD0`/`sub_530FC0`, individual operands via `sub_530FB0(ir_node, index)`, and operand type predicates: `isGPR` (`sub_CDD600`), `isPredicate` (`sub_CDD610`), `isUniformReg` (`sub_CDD630`), `isImmediate` (`sub_CDD670`), `isConstBuf` (`sub_CDD680`).

Priority values range from 14 (least specific, fallback patterns) to 34 (most specific, heavily constrained patterns). Higher priority means more attribute checks and narrower operand constraints.

**Phase 2: Operand Emission.** The winning pattern dispatches to an operand emission function from Zone 1 (`0xCA0000`--`0xCDC000`). Each emitter handles one (opcode, format) combination and populates a structured instruction descriptor: opcode ID at `*(a2+12)`, encoding format at `*(a2+14)`, max operand slot count at `*(a2+15)`, modifier flags via setter functions, and decoded register operands.

**Phase 3: Binary Encoding.** Zone 3 functions (`0xD9A400`--`0xDA0000`, 17 functions) produce the final 128-bit SASS binary instruction word using SSE2 intrinsics (`_mm_or_si128`) for efficient 128-bit bulk operations.

### Instruction Set Coverage

The SM80 backend handles 19 distinct SASS opcodes with a total of 80 (opcode, format) emission variants:

| Opcode ID | Mnemonic | Variants | Description | Max Operands |
|---|---|---|---|---|
| 34 | HMMA | 11 | Tensor Core half-precision matrix multiply-accumulate | 25 |
| 39 | S2R | 2 | Move special register to GPR | 10 |
| 40 | CS2R | 2 | Move control/status special register to GPR | 10 |
| 90 | IMAD | 4 | Integer multiply-add (32-bit) | 19 |
| 127 | FFMA | 12 | FP32 fused multiply-add | 25 |
| 195 | DSETP | 2 | FP64 set predicate (comparison) | 10 |
| 205 | LEA | 1 | Load effective address computation | 19 |
| 230 | IMAD.WIDE | 9 | Integer multiply-add with 64-bit result | 25 |
| 284 | DADD | 1 | FP64 addition | 25 |
| 285 | LDG | 9 | Global memory load | 25 |
| 289 | ISETP | 4 | Integer set predicate | 19/37 |
| 290 | IMNMX | 4 | Integer min/max selection | 19 |
| 292 | FSETP | 2 | FP32 set predicate | 19 |
| 293 | SEL | 4 | Select / conditional move | 19 |
| 294 | SHFL | 1 | Warp shuffle (inter-lane communication) | 3 |
| 295 | FADD | 4 | FP32 addition | 19 |
| 296 | FMUL | 4 | FP32 multiplication | 19 |
| 297 | MUFU | 4 | Multi-function unit (sin/cos/sqrt/rsq/rcp/lg2/ex2) | 19 |
| 299 | HADD2 | 2 | FP16x2 packed addition | 19 |

These 19 opcodes represent the core compute-intensive instructions that the linker's embedded ptxas must emit during LTO compilation. The full Ampere ISA is substantially larger; instructions that never appear in LTO-generated code (control flow, barriers, texture, surface, etc.) are handled by separate codec tables at `0xC00070`--`0xC50970`.

### ISel Pattern Classes

The 259 ISel pattern matchers divide into 9 functional classes:

| Class | Patterns | Address Range | Target Opcodes |
|---|---|---|---|
| Integer/comparison | 60 | `0xCE20F0`--`0xCF0040` | ISETP, IMNMX, IMAD |
| Floating-point | 30 | `0xCF0040`--`0xCFA770` | FADD, FMUL, FSETP, DSETP, DADD |
| Memory/load-store | 31 | `0xCFA770`--`0xD07000` | LDG, S2R, CS2R |
| Conversion/special | 17 | `0xD07000`--`0xD0E000` | MUFU, HADD2, SHFL |
| Wide multiply | 14 | `0xD0E000`--`0xD13000` | IMAD.WIDE |
| Fused multiply-add | 40 | `0xD13000`--`0xD39000` | FFMA |
| Complex ALU | 14 | `0xD39000`--`0xD3E000` | LEA, SEL (complex forms) |
| Tensor core | 27 | `0xD3E000`--`0xD52000` | HMMA (all TC formats) |
| Predicate/select | 26 | `0xD52000`--`0xD5FD70` | SEL, predicate combinations |

Priority levels range from 14 to 34. The priority system ensures more specific patterns win over generic fallbacks. Within each class, patterns form a lattice from general to specific:

| Priority Range | Attribute Checks | Operand Constraint Level | Example |
|---|---|---|---|
| 14--16 | 2--3 | Lightly constrained | GPR+Imm fallback |
| 17--19 | 4--5 | Standard | GPR+Pred+UReg+Imm |
| 20--23 | 6--7 | Moderately constrained | Multi-operand with specific attributes |
| 24--27 | 8--9 | Heavily constrained | UReg-only with 9 attribute checks |
| 28--34 | 10--12 | Highly constrained | FMA with 12 attribute checks, priority 34 |

### Encoding Formats

The format field at `*(a2+14)` selects the operand encoding layout. Formats observed across all 19 opcodes:

| Format ID | Name | Description |
|---|---|---|
| 0 | RR | Register-Register (both sources in GPRs) |
| 1 | RI | Register-Immediate (one immediate source) |
| 2 | RC | Register-ConstantBuffer (one source from constant memory) |
| 3 | RR.ALT | Register-Register alternate encoding |
| 4 | RR.P | Register-Register with predicate output |
| 5 | RI.P | Register-Immediate with predicate output |
| 6 | RC.P | Register-ConstantBuffer with predicate output |
| 7 | SHFL | Warp shuffle encoding |
| 8 | RR.3SRC | Register-Register with 3 register sources |
| 9 | RI.P2 | Register-Immediate with dual predicate output |
| 10 | RR.WIDE | Register-Register with wide (64-bit) result |
| 11 | RR.ADD | Register-Register addition-specific encoding |
| 13--18 | TCA--TCE | Tensor Core formats A through E |
| 19 | RR.MEM | Register-Register memory-mapped encoding |
| 23--24 | TC.ALT/TC.ALT2 | Tensor Core alternate compact formats |
| 42--45 | TC.WIDE1--4 | Tensor Core wide formats 1 through 4 |

### Emission Function Catalog

The following tables list all 80 operand emission functions in Zone 1, organized by opcode. Each row is one (opcode, format) combination.

#### HMMA (Tensor Core) -- 11 variants

| Address | Identity | Format | Size | Notes |
|---|---|---|---|---|
| `sub_CCE930` | sm80_emit_HMMA_TC.ALT | 23 | 3,185 B | 3-operand compact form |
| `sub_CCD8E0` | sm80_emit_HMMA_TC.ALT2 | 24 | 3,134 B | 3-operand compact form |
| `sub_CCECD0` | sm80_emit_HMMA_TCB | 14 | 3,151 B | 25-operand |
| `sub_CCF070` | sm80_emit_HMMA_TCA | 13 | 3,202 B | 25-operand |
| `sub_CD12D0` | sm80_emit_HMMA_TCC | 15 | 3,227 B | 25-operand |
| `sub_CD0E40` | sm80_emit_HMMA_TCD | 17 | 3,211 B | 25-operand |
| `sub_CD0230` | sm80_emit_HMMA_TCE | 18 | 3,160 B | 25-operand |
| `sub_CD6740` | sm80_emit_HMMA_TC.WIDE2 | 43 | 11,127 B | Complex multi-variant with fixup tables |
| `sub_CD7310` | sm80_emit_HMMA_TC.WIDE4 | 45 | 11,127 B | Complex multi-variant with fixup tables |
| `sub_CD7EE0` | sm80_emit_HMMA_TC.WIDE1 | 42 | 11,188 B | Complex multi-variant with fixup tables |
| `sub_CD8AC0` | sm80_emit_HMMA_TC.WIDE3 | 44 | 11,204 B | Complex multi-variant with fixup tables |

#### FFMA -- 12 variants

| Address | Identity | Format | Size |
|---|---|---|---|
| `sub_CC7380` | sm80_emit_FFMA_RR | 0 | 4,102 B |
| `sub_CC4F80` | sm80_emit_FFMA_RI | 1 | 3,866 B |
| `sub_CC7880` | sm80_emit_FFMA_RC | 2 | 4,384 B |
| `sub_CC58D0` | sm80_emit_FFMA_RR.ALT | 3 | 4,148 B |
| `sub_CAAFE0` | sm80_emit_FFMA_RR.P | 4 | 4,603 B |
| `sub_CC7D20` | sm80_emit_FFMA_RI.P | 5 | 4,397 B |
| `sub_CC8990` | sm80_emit_FFMA_RC.P | 6 | 4,413 B |
| `sub_CC4230` | sm80_emit_FFMA_SHFL | 7 | 3,669 B |
| `sub_CC3500` | sm80_emit_FFMA_RR.3SRC | 8 | 3,433 B |
| `sub_CC6B60` | sm80_emit_FFMA_RI.P2 | 9 | 3,888 B |
| `sub_CC4AF0` | sm80_emit_FFMA_RR.WIDE | 10 | 3,685 B |
| `sub_CC5440` | sm80_emit_FFMA_RR.ADD | 11 | 3,701 B |

#### LDG (Global Memory Load) -- 9 variants

| Address | Identity | Format | Size |
|---|---|---|---|
| `sub_CD5BE0` | sm80_emit_LDG_RR | 0 | 11,464 B |
| `sub_CD4520` | sm80_emit_LDG_RI | 1 | 11,399 B |
| `sub_CD5080` | sm80_emit_LDG_RC | 2 | 11,448 B |
| `sub_CD39E0` | sm80_emit_LDG_RR.ALT | 3 | 11,356 B |
| `sub_CBA5D0` | sm80_emit_LDG_RR.P | 4 | 3,480 B |
| `sub_CBA210` | sm80_emit_LDG_RI.P | 5 | 3,245 B |
| `sub_CBADD0` | sm80_emit_LDG_RC.P | 6 | 3,492 B |
| `sub_CBC350` | sm80_emit_LDG_SHFL | 7 | 3,680 B |
| `sub_CBA9D0` | sm80_emit_LDG_RR.3SRC | 8 | 3,476 B |

The RR/RI/RC/RR.ALT base forms are substantially larger (~11 KB each) than the predicated (.P) and shuffle forms (~3.5 KB), reflecting the complex cache hierarchy modifiers (`strongOrder`, `eviction`, `scope`, `cacheOp`, `memoryType`) that the base forms must encode.

#### IMAD.WIDE (64-bit Multiply-Add) -- 9 variants

| Address | Identity | Format | Size |
|---|---|---|---|
| `sub_CDBBD0` | sm80_emit_IMAD.WIDE_RR | 0 | 12,692 B |
| `sub_CDA300` | sm80_emit_IMAD.WIDE_RI | 1 | 12,627 B |
| `sub_CDAF60` | sm80_emit_IMAD.WIDE_RC | 2 | 12,676 B |
| `sub_CD96B0` | sm80_emit_IMAD.WIDE_RR.ALT | 3 | 12,577 B |
| `sub_CD1C80` | sm80_emit_IMAD.WIDE_RR.P | 4 | 4,874 B |
| `sub_CD1770` | sm80_emit_IMAD.WIDE_RI.P | 5 | 4,638 B |
| `sub_CD2730` | sm80_emit_IMAD.WIDE_RC.P | 6 | 4,889 B |
| `sub_CD2C90` | sm80_emit_IMAD.WIDE_SHFL | 7 | 5,078 B |
| `sub_CD21D0` | sm80_emit_IMAD.WIDE_RR.3SRC | 8 | 4,873 B |

IMAD.WIDE has the largest base-form emitters at ~12.7 KB each. The wide result format requires handling both halves of the 64-bit product, with separate register pair allocation.

#### Remaining Opcodes

| Address | Identity | Format | Size |
|---|---|---|---|
| `sub_CCA5D0` | sm80_emit_IMAD_RR | 0 | 4,835 B |
| `sub_CC2A20` | sm80_emit_IMAD_RC | 2 | 3,583 B |
| `sub_CBF4A0` | sm80_emit_IMAD_RR.P | 4 | 3,798 B |
| `sub_CC3D20` | sm80_emit_IMAD_RR.3SRC | 8 | 4,215 B |
| `sub_CABB10` | sm80_emit_S2R_TCC | 15 | 3,213 B |
| `sub_CABEA0` | sm80_emit_S2R_RR.MEM | 19 | 3,216 B |
| `sub_CAC230` | sm80_emit_CS2R_TCC | 15 | 3,314 B |
| `sub_CAB750` | sm80_emit_CS2R_RR.MEM | 19 | 3,317 B |
| `sub_CC9C30` | sm80_emit_DSETP_RI | 1 | 5,292 B |
| `sub_CC5D30` | sm80_emit_DSETP_RR.ALT | 3 | 4,903 B |
| `sub_CAE310` | sm80_emit_LEA_RR | 0 | 5,246 B |
| `sub_CBED30` | sm80_emit_DADD_RR.ADD | 11 | 3,153 B |
| `sub_CC46A0` | sm80_emit_ISETP_RR | 0 | 3,741 B |
| `sub_CC6260` | sm80_emit_ISETP_RI | 1 | 3,785 B |
| `sub_CC66E0` | sm80_emit_ISETP_RC | 2 | 3,917 B |
| `sub_CC84F0` | sm80_emit_ISETP_RR.ALT | 3 | 3,961 B |
| `sub_CB96B0` | sm80_emit_IMNMX_RR | 0 | 3,570 B |
| `sub_CB9AD0` | sm80_emit_IMNMX_RI | 1 | 3,614 B |
| `sub_CB8E80` | sm80_emit_IMNMX_RC | 2 | 3,467 B |
| `sub_CB9280` | sm80_emit_IMNMX_RR.ALT | 3 | 3,511 B |
| `sub_CC2660` | sm80_emit_FSETP_RR | 0 | 3,331 B |
| `sub_CC3930` | sm80_emit_FSETP_RI | 1 | 3,376 B |
| `sub_CC22A0` | sm80_emit_SEL_RR | 0 | 3,316 B |
| `sub_CC2EA0` | sm80_emit_SEL_RI | 1 | 3,361 B |
| `sub_CBF0F0` | sm80_emit_SEL_RC | 2 | 3,213 B |
| `sub_CC1ED0` | sm80_emit_SEL_RR.ALT | 3 | 3,258 B |
| `sub_CA02C0` | sm80_emit_SHFL_SHFL | 7 | 4,076 B |
| `sub_CB7B20` | sm80_emit_FADD_RR | 0 | 7,415 B |
| `sub_CBF900` | sm80_emit_FADD_RI | 1 | 27,061 B |
| `sub_CA08C0` | sm80_emit_FADD_RC | 2 | 44,490 B |
| `sub_CB71C0` | sm80_emit_FADD_RR.ALT | 3 | 6,705 B |
| `sub_CB60E0` | sm80_emit_FMUL_RR | 0 | 5,715 B |
| `sub_CBB1E0` | sm80_emit_FMUL_RI | 1 | 11,561 B |
| `sub_CBC940` | sm80_emit_FMUL_RC | 2 | 14,951 B |
| `sub_CB4BE0` | sm80_emit_FMUL_RR.ALT | 3 | 5,164 B |
| `sub_CB4390` | sm80_emit_MUFU_RR | 0 | 4,743 B |
| `sub_CB5A10` | sm80_emit_MUFU_RI | 1 | 5,377 B |
| `sub_CB8410` | sm80_emit_MUFU_RC | 2 | 8,046 B |
| `sub_CB3E20` | sm80_emit_MUFU_RR.ALT | 3 | 4,529 B |
| `sub_CB5380` | sm80_emit_HADD2_RR | 0 | 5,434 B |
| `sub_CB67F0` | sm80_emit_HADD2_RI | 1 | 5,648 B |

Notable size outliers: `sm80_emit_FADD_RC` at 44,490 bytes is the single largest emission function, followed by `sm80_emit_FADD_RI` at 27,061 bytes. Both require extensive fixup tables for the constant-buffer and immediate operand forms of FP32 addition with negation, absolute value, saturation, and data type modifiers.

### Bitfield Packing Functions

Zone 1 contains 75 bitfield packing functions. These translate the instruction descriptor's register IDs, opcode fields, and modifiers into bit positions within the 128-bit SASS instruction word:

| Class | Functions | Shift-Pack Ops | Notes |
|---|---|---|---|
| FADD/FMUL/MUFU/HADD2/SHFL | 26 | 11--16 | Arithmetic + special function encoding |
| IMAD/FFMA/LEA | 3 | 13--15 | Multiply-add class encoding |
| FFMA | 4 | 15 | Fused multiply-add specific |
| FFMA/DSETP | 12 | 13--16 | FMA and FP64 comparison encoding |
| HMMA (Tensor Core) | 4 | 16 | Tensor core with fixed 16-bitfield layout |
| HMMA/IMAD.WIDE | 10 | 14--20 | Wide operand encoding (most complex) |

### Operand Type Predicates

Fifteen small predicate functions at `0xCDD5F0`--`0xCDD690` classify operands by type:

| Address | Identity | Check |
|---|---|---|
| `sub_CDD5F0` | `getRegFile` | Extract register file from register ID |
| `sub_CDD600` | `isGPR` | Operand is a general-purpose register |
| `sub_CDD610` | `isPredicate` | Operand is a predicate register |
| `sub_CDD630` | `isUniformReg` | Operand is a uniform register |
| `sub_CDD670` | `isImmediate` | Operand is an immediate value |
| `sub_CDD680` | `isConstBuf` | Operand is a constant buffer reference |

### Instruction Modifier Setters

| Address | Identity | Modifier |
|---|---|---|
| `sub_509100` | `setDnzMode` | Denormalized-number-as-zero mode |
| `sub_509160` | `setRoundingMode` | IEEE rounding mode (RN, RZ, RP, RM) |
| `sub_509760` | `setAbsolute` | Absolute value modifier |
| `sub_509890` | `setEvictFirst` | Cache eviction hint |
| `sub_509950` | `setCacheLevel` | Cache level targeting |
| `sub_509B00` | `setScope` | Memory scope (CTA, GPU, SYS) |
| `sub_509B20` | `setEviction` | Eviction policy |
| `sub_50AC80` | `setCacheOp` | Cache operation type |
| `sub_50ACD0` | `setMemoryType` | Memory type qualifier |
| `sub_50B160` | `setComparison` | Comparison predicate (LT, GT, EQ, NE, ...) |
| `sub_50B300` | `setNegation` | Source operand negation |
| `sub_50B500` | `setDataType` | Data type (F32, F16, S32, U32, ...) |
| `sub_50B900` | `setStrongOrder` | Strong memory ordering |
| `sub_50BD00` | `setSaturation` | Output saturation clamp |
| `sub_50BDA0` | `setAddrSpace` | Address space (global, shared, local) |
| `sub_50C060` | `setFtzMode` | Flush-to-zero for denormals |

### External Dependencies

The SM80 backend calls into shared infrastructure functions:

| Address | Identity | Role |
|---|---|---|
| `sub_530FB0` | `getOperand(idx)` | Universal operand accessor (31,399 callers) |
| `sub_530FC0` | `getNumUses()` | Count use (input) operands |
| `sub_530FD0` | `getNumDefs()` | Count definition (output) operands |
| `sub_A49150` | `getInsnAttribute` | Query instruction attribute by slot ID (30,768 callers) |
| `sub_4FF010` | `emitRegOperand` | Emit register operand to descriptor |
| `sub_4FF150` | `emitPredicateOperand` | Emit predicate operand to descriptor |
| `sub_4FF280` | `emitAddrOperand` | Emit address/memory operand to descriptor |
| `sub_4C28B0` | `setBitfield` | Core primitive: pack value into 128-bit instruction at bit offset |
| `sub_4C2A90` | `initInsnFromTemplate` | Initialize instruction buffer from static template |
| `sub_4C4D60` | `encodeOperandSlot0` | Encode register into operand slot 0 |
| `sub_4C5C30` | `encodeOperandSlot1` | Encode register into operand slot 1 |
| `sub_A50D10` | `encodeRegId` | Translate virtual register ID to SASS encoding |

## Relationship to Other Backends

| Backend | Mega-Hub | Size | Pattern Matchers |
|---|---|---|---|
| SM50-7x (shared) | `sub_126CA30` | 239 KB | ~160 |
| SM75 (Turing) | `sub_FBB810` | 280 KB | 276 |
| **SM80 (Ampere)** | **`sub_D5FD70`** | **239 KB** | **259** |
| SM89/90 (Ada/Hopper) | `sub_119BF40` | 231 KB | ~160 |

Despite having fewer ISel patterns than SM75, the SM80 mega-hub matches the SM50-7x hub at 239 KB. The SM89/90 backend at 1.9 MB is substantially larger overall, though its mega-hub is smaller, because it includes 750 instruction encoder template instantiations that the SM80 backend does not require.

## Confidence Assessment

| Claim | Confidence | Verification |
|---|---|---|
| ISA class string `"Ampere"` for sm_80/86/87/88 | CONFIRMED | Decompiled `sub_484F50` lines 293, 337, 383, 426: `"Ampere"` for all four |
| `__CUDA_ARCH__` values: 800, 860, 870, 880 | CONFIRMED | Decompiled `sub_484F50` lines 294, 338, 384, 427 |
| Codegen factory values: 28673, 28674, 28675, 28676 | CONFIRMED | Decompiled `sub_15C3310` +348=28673, `sub_15C3B60` +348=28674, `sub_15C3C60` +348=28675, `sub_15C3A60` +348=28676 |
| Dispatch table: all 7 slots per SM | CONFIRMED | Decompiled `sub_15C0CE0` lines 75--102 |
| Dispatch table: sm_88 encoding table = `sub_15C3A60` | CONFIRMED | Decompiled `sub_15C0CE0` line 98 |
| Capability Vec 1: sm_80 = `xmmword_1D40F40`, sm_86/87/88 = `xmmword_1D40F50` | CONFIRMED | Decompiled `sub_484F50` lines 328 (F40), 370 (F50), 418 (copy), 458 (copy) |
| sm_86/87/88 capability-identical (same Vec 1 via copy chain) | HIGH | Copy chain `v211`->`v210` traced through lines 375, 418, 458 |
| sm_88 new in CUDA 13.0 | HIGH | String at `0x1d40a9a`; not in earlier toolkit versions |
| SM80 ISel backend at `0xCA0000`--`0xDA0000` (1 MB) | HIGH | Address range consistent with function catalog and mega-hub location |
| 259 ISel pattern matchers, 80 emission variants | HIGH | Derived from systematic sweep of address range |
| Internal version numbers (sm_80=25 through sm_88=28) | MEDIUM | Inferred from address spacing and `*(a1+376) > 26` threshold |
| Family linkage: sm_89 links into sm_80/sm_86 chains | CONFIRMED | Decompiled `sub_484F50` lines 507--510 |
| FADD_RC largest emitter at 44,490 B | MEDIUM | Size from function boundary analysis |
| Three-phase pipeline (ISel, emission, encoding) | HIGH | Architectural pattern consistent across all SM backends |

## Cross-References

### nvlink Internal
- [Architecture Profiles](arch-profiles.md) -- SM80 family profiles in the linker database, struct layout, capability vector table
- [SM75 Turing](sm75-turing.md) -- predecessor ISel backend
- [SM89 Ada](sm89-ada.md) -- successor backend, dispatch table comparison
- [Compatibility Checking](compatibility.md) -- same-decade rule, family linkage, capability mask verification
- [Architecture Dispatch](../ptxas/arch-dispatch.md) -- per-arch function pointer dispatch mechanism
- [ISel Hubs](../ptxas/isel-hubs.md) -- SM80 mega-hub `sub_D5FD70` (239 KB)

### Sibling Wikis
- [ptxas: Turing/Ampere](../../ptxas/targets/turing-ampere.html) -- standalone ptxas SM80 target documentation (codegen factory encoding, scheduler profiles, latency tables, SASS encoding format)
- [cicc: SM70-89](../../cicc/targets/sm70-89.html) -- cicc compiler SM80 through SM88 feature gates (`__VA_OPT__`, convergent branches, L2 cache hint atomics)
