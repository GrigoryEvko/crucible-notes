# Target Capability Bitfield (Target+0x628)

> *All addresses, offsets, and bit positions on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

`xla::jellyfish::Target` carries a single 64-bit "has-bits" qword at `Target+0x628`. Despite being read and written as a full `uint64_t`, an entire-`.text` operand scan proves that this build sets and tests exactly **two** of its bits: bit-0 (mask `0x1`) and bit-2 (mask `0x4`). Every other bit — bit-1, bit-3, and all higher bits — has zero set sites and zero test sites; the qword is a feature word with room to grow that this build uses only two slots of.

The qword sits immediately after a two-entry inline array of *continuation-queue scoped-memory-region* descriptors (`Target+0x580` and `Target+0x5F0`). Each bit is the presence flag of one descriptor: bit-0 records that the TensorCore region was appended, bit-2 records that the SparseCore region was appended. Both appends run under the same megachip / SparseCore-offload feature-detect predicate, so bit-2 ends up being the operative term of `Target::IsMegachip()` — the gate that 102 inlined `testb $0x4,0x628` sites across the SparseCore lowering and Deepsea compiler driver read.

> **QUIRK —** entry-B is `GetContinuationQueues(2)`, and `2` is **`kSparseCore`** in the runtime `TpuCoreType` enum (`{kTensorCore=0, kBarnaCore=1, kSparseCore=2}`, the alternative order of the `tpu::TpuCoreProgram` variant; corroborated by `IsMegacore`'s fatal-log on `core==2 → "Megacore SparseCore is not supported"`). Do **not** confuse this with the *protobuf* `nProto` ordering `{TENSOR_CORE=1, BARNA_CORE=2, SPARSE_CORE=3}` used inside `chip_parts.binarypb` — there, `2` is BarnaCore. The has-bits field is built from the runtime enum, so bit-2 is the SparseCore region.

This page documents:

- The **bit layout**: which capability each live bit gates, where it is set in `Target::Init`, and the per-bit consumer surface.
- The **`IsMegachip` identity**: why bit-2 is the megachip/SC-offload has-bit, and why bit-0 belongs to neither megachip nor megacore.
- The **backing data**: the two-entry continuation-queue scoped-memory-region array the bits flag, and the predicate that sets them.

| | |
|---|---|
| **Bitfield member** | `Target+0x628` — `uint64_t` has-bits qword |
| **Live bits** | bit-0 (`0x1`), bit-2 (`0x4`) — all others reserved/unused in this build |
| **Set site (both)** | `xla::jellyfish::Target::Init` @ `0x1D60FC20` |
| **bit-2 accessor** | `xla::jellyfish::Target::IsMegachip` @ `0x10914F60` (inlined at 102 `testb $0x4` sites) |
| **bit-0 consumer** | `DeepseaCompilerBase::CompileInternal` @ `0x10928B40` (gates `LloRegionBuilder::ShaltInternal`) |
| **Backing array** | continuation-queue scoped-mem descriptors @ `Target+0x580` / `Target+0x5F0` |
| **Simulator fallback** | `Target+0x540` byte (`platform_type == iss`) |

---

## Bit Layout

The qword is a `_has_bits_`-style flags member. **Bit numbering is LSB-first**: bit-`n` is mask `1 << n`, so bit-0 = `0x1`, bit-2 = `0x4` — the convention every `or`/`testb` immediate on this page uses. The set sites are two load-or-store sequences in `Target::Init` (`or $0x1,%rcx` then `mov %rcx,0x628(%r12)` at `0x1D611D52`; `or $0x4,%rcx` then `mov %rcx,0x628(%r12)` at `0x1D612121`); the test sites are `testb $imm,0x628(reg)`. An exhaustive operand scan of offset `0x628` across `.text` found *only* these immediates against a `Target`-pointer base register: `or $0x1` once, `or $0x4` once, `testb $0x1` twice, `testb $0x4` 102 times. No `$0x2`, `$0x8`, or higher mask appears anywhere.

> **GOTCHA —** a raw operand grep for `testb $0x1,0x628` returns *three* hits, but one (`0xFE3B600`, inside `PostorderDFSVisitor::PostOrderDFSVisit`) is `testb $0x1,0x628(%rsp)` — a stack local that coincidentally lives at frame offset `0x628`, not the `Target` field. Only the two `%r12`/`%r14`-based reads (`0x10928083`, `0x1090EB6E`) are genuine bit-0 tests. The 102 `testb $0x4` sites are all pointer-register-based, with no `%rsp`/`%rbp` false positive in the set.

| bit | mask | semantic / name | set site (`Target::Init`) | consumers (test sites) | Confidence |
|---:|------|---|---|---|---|
| 0 | `0x1` | TensorCore continuation-queue scoped-memory region present (entry-A) | `0x1D611D52` (`or $0x1`), after the entry-A append | 2 genuine `testb $0x1,0x628` reads; operative reader `DeepseaCompilerBase::CompileInternal` @ `0x10928083` → gate `LloRegionBuilder::ShaltInternal` | CONFIRMED |
| 1 | `0x2` | reserved / unused in v0.0.40 | (never set) | (never tested) | CONFIRMED (absent) |
| 2 | `0x4` | megachip / SparseCore-offload capability has-bit — the operative term of `Target::IsMegachip()` | `0x1D612121` (`or $0x4`), after the entry-B append | `testb $0x4,0x628` ×102 = inlined `IsMegachip()`; SC lowering, `CompileSparseCorePrograms`, `EmitSparseCoreAsyncStart/Done`, `IsValidReduceScatterForSparseCoreOffload`, Deepsea `Lower`/`RunBackend`/`MakeAot` | CONFIRMED |
| 3+ | `0x8+` | reserved / unused in v0.0.40 | (never set) | (never tested) | CONFIRMED (absent) |

> **NOTE —** the qword is loaded and stored with a REX.W (`mov`) — 64-bit — so it physically has room for 62 more flags. Whether bits 1 and 3+ are dead-in-source or forward-reserved for a later silicon generation is not distinguishable from this binary alone; what is certain is that v0.0.40 drives exactly two of them.

---

## Bit-2 == `IsMegachip()`

The 102 `testb $0x4,0x628` sites are all inlined copies of `Target::IsMegachip`. The accessor reads byte-exact as a three-term conjunction:

```c
char Target::IsMegachip(Target *this):                 // sub_10914F60
    cfg = *(TpuChipConfig**)(*(void**)(this+0x3B8) + 0x18);  // Target+0x3B8 -> deref -> +0x18 chip config
    if (!cfg->Megachip()                               //  TpuChipConfig+0x9  (byte)
        || *(int32*)(cfg+0x94) <= 0)                   //  TpuChipConfig+0x94 = CoresPerChip(kSparseCore)
        return 0
    if ((this[0x628] & 4) == 0)                         //  bit-2 of the has-bits qword
        return this[0x540]                              //  platform_type == iss fallback byte
    return 1                                            //  bit-2 set: megachip
```

So bit-2 *is* the megachip capability: a part is a megachip when its chip config declares `Megachip()`, has at least one SparseCore, and *either* the `Target::Init` predicate set bit-2 *or* the platform is the `iss` simulator (`Target+0x540`). The simulator path force-takes the capability even when the hardware predicate did not set the bit. `Megachip()` itself is a one-byte read:

```c
bool TpuChipConfig::Megachip(TpuChipConfig *this):     // sub_20AFCC00
    return *((uint8_t*)this + 9)                        //  TpuChipConfig+0x9
```

### Why bit-2 is *not* `IsMegacore`

`Target::IsMegacore` is a different accessor that never touches `Target+0x628`:

```c
bool Target::IsMegacore(Target *this, TpuCoreType core):   // sub_13699EE0
    if (core == kSparseCore=2)
        LOG(FATAL) << "Megacore SparseCore is not supported."  // target.h:1210
    cfg = *(TpuChipConfig**)(*(void**)(this+0x3B8) + 0x18);  // Target+0x3B8 -> deref -> +0x18 (same chain as IsMegachip)
    if (!cfg->Megacore())                                    // TpuChipConfig+0x8 (byte)
        return 0
    if (core >= 3) BUG()
    return *(int32*)(cfg + 0x7C + core*12) >= 2              // per-core count: TpuChipConfig+0x7C + core*12
```

It reads the *Megacore* flag at `TpuChipConfig+0x8` and a per-core count at `+0x7C+core*12`, with no reference to the has-bits qword. Bit-2 belongs to megachip alone; bit-0 belongs to neither — it is the TensorCore continuation-queue presence flag described below.

| accessor | VA | reads `Target+0x628`? | formula |
|---|---|:--:|---|
| `Target::IsMegachip` | `0x10914F60` | yes (bit-2) | `Megachip ∧ CoresPerChip(SC)>0 ∧ ((+0x628 & 4) ∨ +0x540)` |
| `Target::IsMegacore` | `0x13699EE0` | no | `Megacore ∧ CoresPerChip(core) ≥ 2` |

---

## The Backing Data: Continuation-Queue Scoped-Memory Regions

The two bits are the presence flags of a two-entry inline array of continuation-queue scoped-memory-region descriptors that `Target::Init` builds at `Target+0x580`..`Target+0x627`, with the has-bits qword sitting right after entry B's vector. Each descriptor is `0x70` bytes wide:

| entry | presence bit | `name` (`std::string`) | `MemorySpace` (int32) | `std::vector<MemoryPart>` `{begin,end,cap}` | source |
|---|---|---|---|---|---|
| A | bit-0 (`0x1`) | `Target+0x580` | `Target+0x59C` | `+0x5A0` / `+0x5A8` / `+0x5B0` | `GetContinuationQueues(kTensorCore=0)` |
| B | bit-2 (`0x4`) | `Target+0x5F0` | `Target+0x60C` | `+0x610` / `+0x618` / `+0x620` | `GetContinuationQueues(kSparseCore=2)` |

Each entry is built by copying the per-core continuation-queue list — a `0x30`-byte-stride source array returned by `TpuChipConfig::GetContinuationQueues` — into a `0x1C`-byte-stride `MemoryPart` vector, mapping each element's shared-memory type through `TpuSharedMemoryTypeToMemorySpace` (`0x1D6224E0`). After each append the corresponding bit is OR'd into `Target+0x628`. `GetContinuationQueues` is a small presence-gated lookup:

```c
auto TpuChipConfig::GetContinuationQueues(TpuChipConfig *this, TpuCoreType core):  // sub_20AFCCC0
    if (!_bittest64(this[0xC8], core))           //  presence bitmask at TpuChipConfig+0xC8
        return {nullptr, 0}
    if (core >= 3) BUG()
    return this[0x80 + core*0x18]                //  EnumMap base +0x80, stride 0x18
```

The append predicate (for both entries) is the megachip / SparseCore-offload feature-detect, identical in shape to `IsMegachip`: `Megachip() ∧ CoresPerChip(kSparseCore)>0 ∧ ((Target+0x628 & 4) ∨ Target+0x540) ∧ continuation_queue[0]==2`.

> **QUIRK —** the descriptor array is C++ runtime-only. `Target::ToArgumentsProto` (`0x1D60F560`) does not serialize the `Target+0x540`..`Target+0x628` region, so it has no protobuf `FieldDescriptor`. The per-entry layout (`{std::string, MemorySpace int32, std::vector<MemoryPart>}`, `0x70`-byte stride) and the `GetContinuationQueues(core)` source are byte-exact, but the C++ member *name* is attributed from the source getter, not recovered from a descriptor. A reimplementation should treat the qword as runtime state, never as wire state.

---

## Bit-0: The TensorCore Continuation-Queue Presence

Bit-0 is set in `Target::Init` (`0x1D611D52`) when entry A — the region built from `GetContinuationQueues(kTensorCore=0)` — is appended. It is read at exactly two genuine `testb $0x1,0x628` sites (a third syntactic match at `0xFE3B600` is a `%rsp` stack local, not this field — see the GOTCHA in [Bit Layout](#bit-layout)). The operative reader is `DeepseaCompilerBase::CompileInternal` (`0x10928083`): the test is followed by `jne` over a `call LloRegionBuilder::ShaltInternal` (`0x1D520D20`), so a **set bit-0 skips** the shalt-region emission and a **clear bit-0 calls** `ShaltInternal`. Bit-0 thus gates whether a stall/halt region is emitted for the TensorCore continuation-queue capability. The second read site is `TpuCompactionIsaEmitterCodegen::Create` (`0x1090EB6E`), where it likewise guards the same emission, immediately after an inlined `TpuChipConfig::Megachip` check.

---

## SparseCore-Offload Override Knobs

Two compile-time knobs in `TpuCompilationEnvironment` (TCE) override the hardware default that the bit-2/megachip gate establishes. Both are getters in the `xla::jellyfish` namespace that take an `ObjectView<TpuCompilationEnvironment>` plus a `TpuTopology&`, read a single TCE proto field, and fall back to a generation default when the field is left `AUTO`. Because the `ObjectView` byte-offset equals the TCE `_impl_` struct offset equals the `_InternalSerialize` field-write offset, each struct offset maps 1:1 to a recovered proto field number.

```c
char ShouldEnableConcurrentSparseCoreOffloading(ObjectView<TCE> env,            // sub_1D6B6F80
                                                TpuTopology &topo, bool force_off):
    hw  = (topo.chip_parts->version == 5) & ~force_off    //  chip_parts+0 == TpuVersionProto 5 (ghostlite/v6e)
    p   = env[0x458] ?: &AutoProto_globals_               //  TCE +0x458 = field 923
    v   = AutoOr<bool>::FromProtoOrDie(p)                 //  ret = AUTO?0 : (value | 0x100)
    if ((v & 0x100) == 0) v = hw                          //  no explicit value -> hw default
    return v & 1

char EnableSparseCoreOffloadQueuingInLhs(ObjectView<TCE> env, TpuTopology &topo):  // sub_1D6B81E0
    hw  = (topo.chip_parts->version == 5)                 //  chip_parts+0 == TpuVersionProto 5 (ghostlite/v6e)
    p   = env[0x730] ?: &AutoProto_globals_               //  TCE +0x730 = field 1021
    v   = AutoOr<bool>::FromProtoOrDie(p)
    if ((v & 0x100) == 0) v = hw
    return v & 1
```

The override mechanism is `AutoOr<bool>::FromProtoOrDie` (`0xF795300`): it reads the `AutoProto` state word at `AutoProto+0x1C`; state `0` (AUTO/unset) returns `0` so the `& 0x100` test fails and the hardware default wins; any other state returns `value | 0x100`, so bit-8 marks "explicit value present" and bit-0 carries the bool, overriding the chip-gen default in either direction.

```c
int64 AutoOr<bool>::FromProtoOrDie(AutoProto *a1):     // sub_F795300
    if (a1[0x1C/4] == 0) return 0                       //  AUTO: no presence -> 0
    val = AutoOrTypeTraits<bool>::FromAutoProto(a1)     //  (LOG(FATAL) on conversion error)
    return val | 0x100                                  //  bit-8 = present, bit-0 = value
```

| getter (`xla::jellyfish::`) | VA | TCE offset | field # | TCE field name | hardware default |
|---|---|---|---:|---|---|
| `ShouldEnableConcurrentSparseCoreOffloading` | `0x1D6B6F80` | `+0x458` | 923 | `xla_tpu_enable_concurrent_sparse_core_offloading` | `(proto_version==5: ghostlite/v6e) && !force_off` |
| `EnableSparseCoreOffloadQueuingInLhs` | `0x1D6B81E0` | `+0x730` | 1021 | `xla_tpu_enable_sparse_core_offload_queuing_in_lhs` | `proto_version==5` (ghostlite/v6e) |

> **NOTE —** the field numbers (923, 1021) come from the `mov $field#,%edi` immediate that precedes each `InternalWriteMessage` call in `TpuCompilationEnvironment::_InternalSerialize` (`0x1DB41DC0`): `0x1DB50028` loads `0x458` then writes field `0x39B` (923), and `0x1DB50DBE` loads `0x730` then writes field `0x3FD` (1021), so the struct-offset ↔ field-number binding is direct, not inferred. The `version == 5` comparison disassembles to `cmpl $0x5,(%rax)` against `TpuTopology+8 -> chip_parts+0` (`0x1D6B6F8C`). That field carries the **`TpuVersionProto`** (1-based, the proto's field-1 value the `chip_parts.binarypb` blob leads with), **not** the 0-based internal `TpuVersion`; so `5` resolves to **ghostlite (TPU v6e)**, and `6` would be `6acc60406`. See the [Codename Matrix](tpu-version-codename-matrix.md) for the 0-based↔1-based reconciliation. Four sibling SC-offload knobs share the identical `AutoOr<bool>` pattern, each confirmed by its `0xNNN(%r14)` load + `mov $field#,%edi` pair in the same serializer: field 930 (`disable_sparse_core_collective_offload_remover`, `+0x490` → `0x3A2`), 853 (`enable_hbm_test_buffer_for_sc_collective_offload`, `+0x2E0` → `0x355`), 857 (`enable_outfeed_sanity`, `+0x2E8` → `0x359`), 959 (`xla_shardy_options`, `+0x568` → `0x3BF`).

---

## Related Components

| Name | Relationship |
|---|---|
| `Target::Init` (`0x1D60FC20`) | sets bit-0 / bit-2 after appending the two continuation-queue regions |
| `Target::IsMegachip` (`0x10914F60`) | reads bit-2; the 102 inlined `testb $0x4` sites are its body |
| `TpuChipConfig::GetContinuationQueues` (`0x20AFCCC0`) | source of the per-core queue list copied into the regions |
| `AutoOr<bool>::FromProtoOrDie` (`0xF795300`) | override decode for the two SC-offload knobs |
| `TpuCompilationEnvironment::_InternalSerialize` (`0x1DB41DC0`) | maps TCE struct offsets to proto field numbers |

## Cross-References

- [TpuChipConfig](tpu-chip-config.md) — the chip-config object whose `Megachip`/`CoresPerChip`/`GetContinuationQueues` fields the gate reads
- [Codename Matrix](tpu-version-codename-matrix.md) — `TpuVersion` ↔ codename mapping; disambiguates the `version == 5` SC-offload default
- [TPU Compilation Environment](../config/tpu-compilation-environment.md) — the TCE proto whose fields 923 / 1021 override the bit-2 hardware default
- [chip_parts.binarypb Decode](chip-parts-binarypb.md) — the proto `nProto` core ordering (`TENSOR_CORE=1, BARNA_CORE=2, SPARSE_CORE=3`) that the runtime `TpuCoreType` enum (`kSparseCore=2`) must not be confused with; also the `version==5 → ghostlite/v6e` mapping
