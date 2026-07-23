# DRAM / HBM Geometry & the DRAM Split

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). The geometry constants and the split/budget passes live in `neuronxcc/starfish/lib/libwalrus.so`. For `.text`/`.rodata` the virtual address equals the file offset. Other wheels differ — treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

Off chip, a NeuronCore sees one flat memory: **HBM** (high-bandwidth DRAM), the backing store for weights, constants, intermediate DRAM tensors, and DMA descriptor rings. Unlike the on-chip SBUF and PSUM ([1.05](sbuf-psum-geometry.md)), there is no partition axis and no bank array — HBM is a single byte-addressed region whose only geometric parameter is its **size**. That size is one `unsigned` GiB field per architecture, `Core::dramSizeGb`, read through the exact same `getArchModel → Board → Device → Core` singleton walk every other Part-1 geometry constant uses ([1.01](arch-object-model.md)), terminating at `Core+0x30` instead of an engine sub-object pointer. This page recovers that field for all four hardware generations, distinguishes it from the *whole-device* HBM size carried one level up at `Device+0x8`, documents the compile-time budget check (`HBMUsage`) that gates compilation against it, and documents the `split_huge_dram_tensor` pass that subdivides a DRAM tensor too large for the per-core window.

The single most important fact on this page is that **two different HBM sizes exist and the compiler gates on the smaller one.** `Core::dramSizeGb` is the per-NeuronCore HBM window; `Device::dramSizeGb` is the whole-device HBM. They are independent immediates and need not relate by `numCores` — on Inferentia the device carries 32 GiB but each of its four cores gets a 16 GiB window, so `4 × 16 ≠ 32`. The budget check (`HBMUsage::run`) reads `Core+0x30` (the per-core window), not `Device+0x8`. A reimplementer who sizes the allocator off device HBM will over-budget every part where the two disagree.

The numbers are corroborated three ways. The constructor immediates are read directly off the `<Arch>Core` ctor bodies (`movl $imm,0x60(%rsp)` into the `CoreParamSet`, then `Core::Core` copies `CoreParamSet+0x60 → Core+0x30`); the read-chain is read off `HBMUsage::run` (`mov 0x30(%rax),%eax; shl $0x1e`); and the field **name** comes from a real C++ header shipped in the wheel, `data/include/hwm/ctm/ctm.hpp`, which declares `const unsigned dramSizeGb; // dram/hbm per core` (`ctm.hpp:187`). All three agree.

For reimplementation, the contract is:

- The two `dramSizeGb` fields — `Core+0x30` (per-core window) and `Device+0x8` (whole-device) — their per-generation immediates, and the rule that the **budget gate uses the per-core one**.
- The `HBMUsage` read-chain: `getArchModel → Board+0x8 → Device+0x10 → Core+0x30`, `<< 30` to bytes, a `× 1.1` soft-margin warning gate, and a hard `usage > HBMLimit` failure.
- The `split_huge_dram_tensor` / `vn_split_dram_node` mechanism that subdivides an over-window DRAM tensor, and the `no_split_dram` / `hbm-usage-check` knobs that gate it.
- That HBM geometry is a **baked per-codename constant**, not a runtime device probe.

| | |
|---|---|
| **Geometry field (per core)** | `Core::dramSizeGb` @ `Core+0x30` (`ctm.hpp:187`) |
| **Geometry field (device)** | `Device::dramSizeGb` @ `Device+0x08` (`ctm.hpp:229`) |
| **Read path** | `getArchModel(codename)` @ `0x17344c0` → `Board+0x8` → `Device+0x10` → `Core+0x30` |
| **Per-core HBM (gen1/2/3/4)** | 16 / 16 / 24 / 36 GiB (`0x10/0x10/0x18/0x24`) |
| **Per-device HBM (gen1/2/3/4)** | 32 / 32 / 24 / 36 GiB (`0x20/0x20/0x18/0x24`) |
| **NeuronCores / device** | 4 / 2 / 2 / 2 |
| **Budget check** | `HBMUsage::run(vector<…Module>&)` @ `0x16b94b0`; read-chain @ `0x16ba2ef`–`0x16ba386` |
| **Soft margin** | `× 1.1` (`0x3FF199999999999A` @ `0x1dbf5b0`) |
| **Split pass** | `split_huge_dram_tensor` (`neuronxcc::backend::VNSplitter::runTransform` @ `0xd5a3d0`; cl::opt `splitHugeDramTensor` @ `0x3dfa780`) |
| **Split-disable knob** | `no_split_dram` / `noSplitDram` @ `0x3dfacc0`; budget-disable `hbm-usage-check` (flag var `DisableHBMUsageCheck` @ `0x3df7560`) |
| **Field-name source** | `data/include/hwm/ctm/ctm.hpp` (shipped header — the field names come from here) |

---

## The two dramSizeGb fields

### Purpose

HBM size enters the `Board → Device → Core` singleton tree at two distinct levels, and they answer two different questions. `Device::dramSizeGb` answers "how much HBM does the whole device package have?"; `Core::dramSizeGb` answers "how much HBM does *one* NeuronCore get to use?". On a multi-core device the per-core window is the address space a single core's allocator fills — the compiler compiles per-core, so the per-core window is the budget that gates. The whole-device figure is carried (and printed in some metrics) but is **not** the gating budget.

This split is invisible on parts where the numbers coincide and decisive where they do not. The `ctm.hpp` header makes the intent explicit with two doc comments on two fields of two classes — the per-core one on `Core`, the device one on `Device`.

### Record layout

Both fields are plain `unsigned` (4-byte) scalars. The names and types come from `ctm.hpp`; the offsets are read off the `Core::Core` / `Device::Device` constructor store sequences.

| Field | Class | Offset | Type | `ctm.hpp` | Meaning |
|---|---|---|---|---|---|
| `dramSizeGb` | `Core` | `+0x30` | `unsigned` | `:187` "dram/hbm per core" | **per-core HBM window, GiB** — the gating budget |
| `dramSizeGb` | `Device` | `+0x08` | `unsigned` | `:229` "dram/hbm for entire device" | whole-device HBM, GiB — carried, not gated on |
| `numCores` | `Device` | `+0x00` | `unsigned` | `:227` | NeuronCores per device |
| `numTpb` | `Device` | `+0x04` | `unsigned` | `:228` (alias of `numCores`) | compat alias |
| `numDevices` | `Board` | `+0x00` | `unsigned` | `:245` | devices per board |

The values flow from a per-arch `CoreParamSet` (`ctm.hpp:147`), where the per-core size lives at `CoreParamSet+0x60` (`std::optional<unsigned> dramSizeGb`, with the `std::optional` "engaged" flag at `+0x64`). `Core::Core` copies it inline:

```c
// Core::Core(const CoreParamSet& p)  @0x1734220
//   the dramSizeGb copy @0x1734294:
this[0x30] = *(uint32*)(p + 0x60);   // 0x1734294 mov 0x60(%rsi),%ecx
                                     // 0x173429b mov %ecx,0x30(%rdi)
```

The device size is passed as the third `Device::Device` argument and stored directly:

```c
// Device::Device(Core& core, unsigned numCores, unsigned dramSizeGb)  @0x1734480
//   @0x1734484:
this[0x08] = dramSizeGb;             // 0x1734484 mov %ecx,0x8(%rdi)
```

> **GOTCHA — 16/16/24/36 are GiB, not a count.** Read as bare immediates in the ctor bodies, these values invite a reading as a per-core ring or queue count. The shipped header settles it: `ctm.hpp:187` declares `const unsigned dramSizeGb; // dram/hbm per core`, and `ctm.hpp:229` gives `Device+0x8` as the whole-device `dramSizeGb`.

### Per-generation immediates

Each `<Arch>Core` constructor builds a `CoreParamSet` on the stack, writes the per-core size with a single `movl $imm,0x60(%rsp)`, and emits the inline `<Arch>Device` / `<Arch>Board` constructors that pass the device size (`ecx`), the core count (`edx`), and the device count. Every immediate below was re-disassembled from the ctor body at the cited address; **all four sets match byte-for-byte.**

| Arch (gen) | per-core (`CPS+0x60`) | addr | per-device (`Device` `ecx`) | addr | `numCores` (`edx`) | `numDevices` | Confidence |
|---|---|---|---|---|---|---|---|
| gen1 Inferentia | `0x10` = 16 GiB | `0x17347b6` | `0x20` = 32 GiB | `0x1734995` | `0x4` (4) | `0x2` (2) | CERTAIN |
| gen2 Sunda/Trn1 | `0x10` = 16 GiB | `0x1734ba6` | `0x20` = 32 GiB | `0x1734d85` | `0x2` (2) | `0x2` (2) | CERTAIN |
| gen3 Cayman/Trn2 | `0x18` = 24 GiB | `0x1734f96` | `0x18` = 24 GiB | `0x1735175` | `0x2` (2) | `0x2` (2) | CERTAIN |
| gen4 CoreV4/Trn3 | `0x24` = 36 GiB | `0x1735396` | `0x24` = 36 GiB | `0x1735575` | `0x2` (2) | `0x2` (2) | CERTAIN |

> **GOTCHA — the per-core figure is a budget window, not a physical partition, so it does not sum to the device figure.** gen1 sets `numCores=4`, `Device::dramSizeGb=32`, and `Core::dramSizeGb=16`, so `4 × 16 = 64 GiB` is twice the 32 GiB device figure. That is not a contradiction and neither immediate is misread: `Core::dramSizeGb` is the address-space/allocation window a single-core compile is allowed to fill (`ctm.hpp:187` "dram/hbm per core"), `Device::dramSizeGb` is whole-package physical HBM (`ctm.hpp:229` "dram/hbm for entire device"). Windows overlap into shared HBM — they are budgets, not disjoint slices — so `Σ windows` is a meaningless quantity and the two fields need not relate by `numCores`. The three cited immediates are each read directly: per-core `0x10` at `0x17347b6` (into `CoreParamSet+0x60`, copied to `Core+0x30`), per-device `0x20` passed as the 3rd `Device::Device` argument at `0x1734995` (stored to `Device+0x8`), and `numCores=4` at `0x173499a`. gen2 coincidentally satisfies `2 × 16 = 32`; gen3/gen4 set the device figure equal to the per-core window (24/24, 36/36). A reimplementer must drive the **per-core** window into the allocator and budget check, never a `numCores`-scaled device figure.

### Considerations

HBM size is a **baked compile-time constant per codename**, not device-config or runtime-probe driven: every value is a `movl $imm` in the per-arch ctor, and no runtime descriptor is read. gen5/`core_v5` has no `Core` ctor and `getArchModel` falls through to `__assert_fail`, so this build has no gen5 HBM figure ([1.01](arch-object-model.md)).

The per-arch Core constructors live at `0x1734720` (Inferentia), `0x1734b10` (Sunda), `0x1734f00` (Cayman), `0x1735300` (CoreV4) — the same addresses the SBUF/PSUM geometry reads from ([1.05](sbuf-psum-geometry.md)), because the HBM size is just another field of the same `Core` object those ctors build. Both `libwalrus.so` and `libBIR.so` compile the geometry tree from the one `ctm` source and carry it byte-identically; the budget check on this page reads the `libwalrus` copy.

| Function / Symbol | Address | Role | Confidence |
|---|---|---|---|
| `Core::Core(CoreParamSet const&)` | `0x1734220` | copies `CoreParamSet+0x60 → Core+0x30` | CERTAIN |
| `Device::Device(Core&,uint,uint)` | `0x1734480` | stores `dramSizeGb` at `Device+0x8` | CERTAIN |
| `Board::Board(Device&,uint)` | `0x17344a0` | builds `Board` (`numDevices` at `+0x0`) | CERTAIN |
| `InferentiaCore::InferentiaCore` | `0x1734720` | gen1 immediates (16/32, 4 cores) | CERTAIN |
| `SundaCore::SundaCore` | `0x1734b10` | gen2 immediates (16/32, 2 cores) | CERTAIN |
| `CaymanCore::CaymanCore` | `0x1734f00` | gen3 immediates (24/24, 2 cores) | CERTAIN |
| `CoreV4Core::CoreV4Core` | `0x1735300` | gen4 immediates (36/36, 2 cores) | CERTAIN |
| `getArchModel(string const&)` | `0x17344c0` | codename → `Board*` dispatch | CERTAIN |

---

## The HBM budget check — `HBMUsage`

### Purpose

`HBMUsage` (`neuronxcc::backend::HBMUsage`) is the backend pass that decides whether a compiled module's DRAM footprint fits the per-core HBM window. It is the consumer that turns `Core::dramSizeGb` from a geometry record into a hard compile-time gate: it sums every `MemoryType::DRAM` allocation, reads the per-core window through the standard `getArchModel` walk, scales it to bytes, and compares. Over-budget compilation fails with a named diagnostic; the check is disarmable with a flag for diagnostic runs.

There are two `run` overloads — a single-module form and the multi-module (`vector<unique_ptr<Module>>`) form. The multi-module overload carries the limit comparison documented below; the single-module form computes per-module usage that feeds it.

### Entry Point

```text
HBMUsage::run(vector<unique_ptr<bir::Module>>&)   0x16b94b0   ── THE budget gate
  ├─ HBMUsage::run(bir::Module&)                  0x16b5a10   ── per-module usage
  │    ├─ HBMUsage::getModelUsage(bir::Module&)   0x16b81a0   ── weights/constants bytes
  │    ├─ HBMUsage::getTensorUsage(bir::Module&)  0x16b84f0   ── live intermediate-tensor bytes
  │    └─ HBMUsage::getDescUsage(bir::Module&)    0x16b8710   ── DMA descriptor-ring bytes
  ├─ getArchModel(module.getArch())               0x17344c0   ── → Board → Device → Core+0x30
  └─ printHBMMetrics(logging::Logger, int, int)   0x10dbef0   ── usage / limit reporter
```

`TotalDRAMUsage = getModelUsage + getTensorUsage + getDescUsage` — the three categories named in the metric strings (`hbm_internal`, `hbm_intermediate`, `hbm_io`). Their sum is the `usage` compared against the limit.

### Algorithm

The limit read is the same four-hop `getArchModel` walk as SBUF/PSUM, terminating at `Core+0x30`. The decisive instructions were re-disassembled directly:

```c
// HBMUsage::run(vector<unique_ptr<Module>>&)  @0x16b94b0
//   limit read-chain @0x16ba2ef..0x16ba386, instruction-for-instruction
function HBMUsage_run(modules):
    codename = module.getArch()                      // 0x16ba2ef call bir::Module::getArch
    board    = getArchModel(codename)                // 0x16ba2f7 call getArchModel → rbx
    device   = *(Device**)(board  + 0x08)            // 0x16ba31f mov 0x8(%rbx),%rax
    core     = *(Core**)  (device + 0x10)            // 0x16ba323 mov 0x10(%rax),%rax
    hbm_gib  = *(uint32*)(core   + 0x30)             // 0x16ba327 mov 0x30(%rax),%eax  = dramSizeGb
    HBMLimit = (uint64)hbm_gib << 30                 // 0x16ba32a shl $0x1e,%rax       → BYTES

    usage    = TotalDRAMUsage                         // Σ over all MemoryType::DRAM locations
    soft     = (double)HBMLimit * 1.1                 // 0x16ba355 mulsd [0x1dbf5b0]    = ×1.1
    if (double)usage <= soft:                         // 0x16ba35d comisd; 0x16ba361 jbe → ok
        // within the 10% soft margin — pass (warning gate)
        ;
    if usage > HBMLimit:                              // 0x16ba381 cmp; ja → over-budget path
        log("DRAM Memory Usage ... Estimated peak HBM usage (<n>) "
            "exceeds HBM size of <HBMLimit>")         // rodata fragments, see below
    assert(TotalDRAMUsage <= HBMLimit)                // "TotalDRAMUsage <= HBMLimit"
```

> **QUIRK — two thresholds, one limit.** `HBMLimit` is the raw per-core window in bytes. The `mulsd` by `1.1` (the IEEE-754 double `0x3FF199999999999A` at `0x1dbf5b0`, bytes `9a 99 99 99 99 99 f1 3f`) is a **soft 10% over-budget margin**: usage in `[HBMLimit, 1.1 × HBMLimit]` passes the `comisd`/`jbe` warning gate, but the separate raw `cmp`/`ja` (`usage > HBMLimit`) still flags it for the over-budget diagnostic. A reimplementer must keep the warning margin and the hard limit distinct — the soft margin is a reporting nicety, not a relaxation of the assert.

`HBMLimit` per generation is `dramSizeGb << 30`: gen1/gen2 = `0x4_0000_0000` (16 GiB), gen3 = `0x6_0000_0000` (24 GiB), gen4 = `0x9_0000_0000` (36 GiB). This is the per-core window — `Device::dramSizeGb` is never read here.

### Diagnostics and metrics

The pass emits a "HBM scratchpad usage summary (post-allocation)" with a labelled breakdown. The strings are present verbatim in `libwalrus`:

| String fragment (rodata) | Role |
|---|---|
| `Estimated peak HBM usage (` … `) exceeds HBM size of ` | over-budget error (@ `0x1c8951d` / `0x1c8953a`), `usage` vs `HBMLimit` |
| `TotalDRAMUsage <= HBMLimit` | the hard assert message (@ `0x1c8954f`) |
| `HBM scratchpad usage summary (post-allocation):` | summary header |
| `HBM total usage: ` / `HBM intermediate usage: ` / `HBM I/O usage: ` / `HBM peak internal usage: ` | per-category lines |
| `Peak internal HBM memory usage of ` / `Total estimated HBM usage is: ` | summary totals |

The machine-readable metric map (`neuronxcc::backend::hbmMetrics`, populated by `printHBMMetrics`) is keyed by the strings `hbm_internal`, `hbm_intermediate`, `hbm_io`, `hbm_usage`, `hbm_size`, `hbm_limit`, plus `end_address` and `private_hbm` — all present in `libwalrus`. The usage keys `hbm_internal` / `hbm_intermediate` / `hbm_io` correspond to `getModelUsage` / `getTensorUsage` / `getDescUsage`; `hbm_limit` is the per-core window (`Core::dramSizeGb << 30`) above; `hbm_size` carries the same window figure for the summary line.

### The disable knob

| Knob | Type | Flag variable (.bss) | Option string | Effect |
|---|---|---|---|---|
| `hbm-usage-check` | `cl::opt<bool>` | `DisableHBMUsageCheck` @ `0x3df7560` | `0x1dc3562` | help: "Disable total HBM usage check" — when set, the pass still computes/reports metrics but does not assert/fail on over-budget |

The option string (`0x1dc3562`), help text, and `.bss` flag variable (`0x3df7560`) are all present — note the address `0x3df7560` is the flag *variable*, not the string literal. That the check is on by default is **[INFERRED]** from the `Disable…` naming. The pass is registered like the other backend generators (`register_generator_*` family).

---

## The DRAM split — `split_huge_dram_tensor`

### Purpose

A single logical DRAM tensor can exceed the per-core HBM window (or a per-split limit derived from it). Rather than fail outright, the backend can **split** that tensor into multiple smaller DRAM sub-tensors, each sized to fit, by guessing a block dimension to slice along. This is the `split_huge_dram_tensor` option of the `VNSplitter` walrus pass (`neuronxcc::backend::VNSplitter::runTransform` @ `0xd5a3d0`, registered via `register_generator_vn_splitter__`), with `vn_split_dram_node` as the node-level worker. It is *geometry-driven* — its threshold is expressed in GiB and bounded by the per-core window this page recovers — but it is a transformation, not the allocator: it changes how many DRAM `MemoryLocation`s exist, while the DRAM allocator (Part 8) decides where each lands.

> **NOTE — split is tensor subdivision, not address-space partitioning.** `split_huge_dram_tensor` does **not** decide "per-core vs unified DRAM address space". It splits one oversized DRAM `MemoryLocation` into several that each fit the window. The per-core vs shared-DRAM *address-space* question is a separate Part-8 concern (the DRAM allocator and shared-DRAM rotation). This page's split is purely "make one huge tensor into several window-sized ones."

### Knobs

These are the cl::opt option names recovered from `libwalrus` with their verbatim help text. The **option name is the underscore form** (`split_huge_dram_tensor`); the camelCase symbol (`splitHugeDramTensor`) is the `.bss` flag *variable* the pass reads:

| Option name | Flag variable (.bss) | Help string | Verbatim help text |
|---|---|---|---|
| `split_huge_dram_tensor` | `splitHugeDramTensor` @ `0x3dfa780` | `0x1dc31ec` | "Split huge DRAM tensors that are larger than the specified threshold in (GB). Guess block dims: if successful, proceed to split along the guessed block dimensions." |
| `vn_split_dram_node` | `vnSplitDRAMNode` @ `0x3dfa9c0` | `0x1dc3052` | "Splitting dram node along guessed block dimension" |
| `no_split_dram` | `noSplitDram` @ `0x3dfacc0` | `0x1dc302a` | "Do not split the vn if this would result in any of the splits having to be placed in DRAM (due to perSplitLimit exceeded)" |

All three option names, flag variables, help-string addresses, and help text are read verbatim from the binary.

> **GOTCHA — these option names use underscores, not hyphens.** Most cl::opt names in this codebase are hyphenated, so `no-split-dram` / `split-dram` / `enable-experimental-no-split-dram` are natural guesses — and none of those strings exists anywhere in the wheel. The split knobs are `split_huge_dram_tensor`, `vn_split_dram_node`, `no_split_dram`. The separate HBM-budget toggle *is* hyphenated: `hbm-usage-check`.

### Algorithm

The split is threshold-driven. The threshold is a GiB figure (the pass option) bounded by the per-core window; a DRAM tensor over threshold is sliced along a guessed block dimension into sub-tensors that each fit. The `no_split_dram` option suppresses the split when the resulting sub-tensors would themselves spill to DRAM beyond a `perSplitLimit`.

```c
// split_huge_dram_tensor (high-level; [INFERRED] from option help + symbol roles)
function split_huge_dram_tensor(module, thresholdGB):
    limit = thresholdGB << 30                         // bytes; bounded by Core::dramSizeGb window
    for each DRAM tensor T in module:
        if sizeof(T) <= limit:
            continue                                  // fits — leave alone
        dim = guess_block_dimension(T)                // "Guess block dims"
        if dim is None:
            continue                                  // cannot split safely
        if no_split_dram and any_split_exceeds_perSplitLimit(T, dim):
            continue                                  // 'no_split_dram': refuse spilling splits
        replace T with vn_split_dram_node(T, dim)     // emit window-sized sub-tensors
```

> **NOTE — what is read vs reconstructed here.** The names, symbols, addresses, help strings, and the implementing class (`VNSplitter::runTransform` @ `0xd5a3d0`, with `collectAccesses` @ `0xd52910`) are read from `libwalrus`. The *internal control flow* — the block-dimension guess and the `perSplitLimit` derivation — was not traced instruction-by-instruction, so the pseudocode above is **[INFERRED]** from the option help text and the symbol roles. Treat the knob contract as authoritative and the body as a sketch.

### Considerations

Split runs before the DRAM allocator so the allocator sees only window-sized tensors. Its interaction with the budget check is complementary: split *reduces* the chance `HBMUsage` fires by ensuring no single tensor blows the window, while `HBMUsage` is the *aggregate* gate over the post-split, post-allocation footprint. Disabling split (`no_split_dram`) makes oversized single tensors reach the allocator intact, where they may then trip the per-core budget.

---

## At-a-glance: the per-generation HBM matrix

Every cell is a constructor immediate, read at the cited address. The HBM rows extend the consolidated geometry matrix on the [SBUF/PSUM page](sbuf-psum-geometry.md#at-a-glance-the-full-geometry-matrix).

| HBM geometry | gen1 Inferentia | gen2 Sunda | gen3 Cayman | gen4 CoreV4 |
|---|---|---|---|---|
| **`Core::dramSizeGb`** (per-core window) | 16 GiB (`0x10`) | 16 GiB (`0x10`) | 24 GiB (`0x18`) | 36 GiB (`0x24`) |
| **`Device::dramSizeGb`** (whole device) | 32 GiB (`0x20`) | 32 GiB (`0x20`) | 24 GiB (`0x18`) | 36 GiB (`0x24`) |
| `Device::numCores` | 4 | 2 | 2 | 2 |
| `Board::numDevices` | 2 | 2 | 2 | 2 |
| `HBMLimit = window << 30` (budget) | `0x4_0000_0000` | `0x4_0000_0000` | `0x6_0000_0000` | `0x9_0000_0000` |
| per-core vs Σ-per-core | 16 vs 64 (disagree) | 16, Σ=32=device | 24=device | 36=device |
| addressing granule | per-core 4 KiB DRAM page (allocator, Part 8) | | | |

---

## Related Components

| Name | Relationship |
|---|---|
| `getArchModel` / `Board → Device → Core` tree | the singleton walk that exposes `dramSizeGb`; this page reads `Core+0x30` / `Device+0x8` of that tree ([1.01](arch-object-model.md)) |
| `HBMUsage` (backend pass) | the consumer that gates compilation on the per-core HBM window |
| `split_huge_dram_tensor` / `vn_split_dram_node` | the transformation that keeps single DRAM tensors within the window |
| DRAM allocator (Part 8) | the consumer that places window-sized DRAM tensors; fills the per-core window bottom-up with a 4 KiB page granule |
| shared-DRAM / DRAM rotation (Part 8) | the address-rotation scheme (`dram-rotation-size`, default 8G) layered over the same window |

## Cross-References

- [1.01 The Arch Object Model](arch-object-model.md) — the `getArchModel → Board/Device/Core` singleton tree; the `dramSizeGb` correction; the four-hop walk template this page's read-chain instantiates.
- [1.05 SBUF / PSUM Bank Geometry](sbuf-psum-geometry.md) — the on-chip side, read through the same `Core` object at `Core+0x20`/`0x28`; its at-a-glance matrix carries the HBM-per-core row.
- [1.07 LNC Memory Model](lnc-memory-model.md) — how the per-core HBM window composes across the Logical NeuronCore (multi-core) memory model.
- [DRAM allocator (Part 8)](../walrus/) — the consumer that fills the per-core HBM window: interval-tree placement, 4 KiB page granule, the DRAM-rotation cap (`dram-rotation-size`, default 8G).
- [shared-DRAM / multi-core (Part 8)](../walrus/) — shared-DRAM allocation and rotation layered over the per-core window.
