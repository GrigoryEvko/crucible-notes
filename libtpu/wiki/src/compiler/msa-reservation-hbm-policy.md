# MSA Reservation & HBM Policy

> *All addresses, symbols, offsets, proto names, and magic constants on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Other versions will differ.*

## Abstract

`TpuCustomCallMemorySpacePolicy` is the HLO pass that decides, per custom-call, **where the buffer lands**: it either reserves a slice of VMEM for the memory-space-assignment ([MSA](msa-overview.md)) optimizer to spend, or it forces the custom-call's buffers into HBM. The knob it consumes is a single proto sub-message, `TpuCustomCallMemorySpaceSpec`, carried as an `AutoOr<spec>` field on the TPU compilation environment — so the user either pins a spec or leaves it `AUTO` and lets the compiler synthesize one.

This page is the specification for two byte-exact things the decompile pins down:

- **The two policy field dictionaries.** `TpuCustomCallMemorySpaceSpec` is a `oneof policy { MsaReservationPolicy msa_reservation_policy = 1; HbmPolicy hbm_policy = 2; }`. `MsaReservationPolicy` carries exactly **one** field — `uint64 msa_reservation_size_bytes = 1` — and `HbmPolicy` is a **zero-field marker** (selecting it *is* the instruction). Both default-instances are byte-zero, triple-sourced from the generated `_table_`, the `_globals_`, and the serialized `DescriptorProto`.
- **`ResolveMemorySpaceSpec` @ `0x11036320`.** This resolver *always* synthesizes a candidate `MsaReservationPolicy` whose size is computed from the target's VMEM, then keeps that candidate iff the spec is `AUTO`/absent, otherwise returns the user's spec verbatim. The AUTO size is `max(floor(free_vmem / 4), 10 MiB)`, where `free_vmem = VmemSizeBytes − (OverlayReservedVmemBytes + DefaultScopedVmemBytes)`.

The downstream dispatch (`RunImpl` @ `0x110364a0`) switches on the resolved `oneof` case: case 1 runs the MSA reservation sized by `msa_reservation_size_bytes`; case 2 forces HBM placement; case 0 is a no-op. The whole pass is gated on memory-space assignment being enabled (`ShouldEnablePass` @ `0x110362a0`).

| | |
|---|---|
| **Pass class** | `xla::jellyfish::TpuCustomCallMemorySpacePolicy` |
| **Knob proto** | `TpuCustomCallMemorySpaceSpec` (oneof `policy`), arm 1 `MsaReservationPolicy`, arm 2 `HbmPolicy` |
| **Knob carrier** | `AutoOr<TpuCustomCallMemorySpaceSpec>` on the comp-env (`AutoProto*` slot at comp-env `+0xbb0` = `+2992`) |
| **Resolver** | `ResolveMemorySpaceSpec` @ `0x11036320` (0x16f B) |
| **Dispatcher** | `RunImpl` @ `0x110364a0` — switch on resolved oneof case |
| **Gate** | `ShouldEnablePass` @ `0x110362a0` → `IsMemorySpaceAssignmentEnabled` @ `0x12fc1280` |
| **MSA arm action** | `RunMsaReservationPolicy` @ `0x110367c0` |
| **HBM arm action** | `RunHbmPolicy` @ `0x11038120` |
| **AUTO size formula** | `max(floor((Vmem − Overlay − ScopedVmem) × 0.25), 10 MiB)` |
| **Magic constants** | `0.25f` (`.rodata` @ `0x84a2724` = `0x3e800000`); 10 MiB floor (`0x00a00000`) |
| **Source file** | `platforms/xla/service/jellyfish/tpu_custom_call_memory_space_policy.cc` (recovered from log strings) |
| **Confidence** | HIGH (symbol/string/byte-anchored) unless a row or callout says otherwise |

---

## The Policy Knob: `TpuCustomCallMemorySpaceSpec`

`TpuCustomCallMemorySpaceSpec` is the structured arm of the per-custom-call placement knob. It is a single `oneof` named `policy` with two arms; selecting an arm both chooses the *placement action* and supplies its (one) parameter. The wire structure is recovered three independent ways and they agree to the byte:

1. the generated `<Msg>::_table_` `TcParseTableBase` + `FieldEntry` rows in `.data.rel.ro`,
2. the `<Msg>_globals_` default-instance bytes in `.data`, and
3. the serialized `google.protobuf.DescriptorProto` embedded in `protodesc_cold` (the `message_type` sub-record of `tpu_compilation_environment.proto`).

```proto
// xla.jellyfish (recovered, byte-exact)
message TpuCustomCallMemorySpaceSpec {
  oneof policy {                                       // oneof_decl[0] = "policy"
    MsaReservationPolicy msa_reservation_policy = 1;   // oneof_index 0
    HbmPolicy            hbm_policy             = 2;    // oneof_index 0
  }

  message MsaReservationPolicy {
    uint64 msa_reservation_size_bytes = 1;             // the ONLY field of either arm
  }

  message HbmPolicy {}                                 // zero fields — a pure marker
}
```

Both fields share `oneof_index 0`, so they are **mutually exclusive** — a spec is either an MSA reservation *or* an HBM placement, never both, and by default neither (`case 0`, unset). The structured knob therefore collapses to one of three states: *unset* (let AUTO synthesize), *reserve N bytes of VMEM for MSA*, or *force HBM*.

### Field dictionaries (`_table_` ⇔ `_globals_` ⇔ `DescriptorProto`)

`c++off` is the byte offset of the field's storage within the message object; `has_idx` is the hasbit index; `type_card` is the parse-table type-card (singular-uint64 = `0x08d1`; the oneof message arms = `0x0436`). All three messages' `_globals_` are byte-zero, so the proto default is *unset oneof / size 0 / HBM-marker absent*.

| Message | field# | name | proto type | c++off | has_idx | type_card | default |
|---|---|---|---|---|---|---|---|
| `TpuCustomCallMemorySpaceSpec` | — | *(oneof `policy`)* | — | — | — | — | unset (case 0) |
| &nbsp;&nbsp;arm 1 | 1 | `msa_reservation_policy` | message | `0x10` (union) | case @ `0x1c` | aux0 / `0x0436` | absent |
| &nbsp;&nbsp;arm 2 | 2 | `hbm_policy` | message | `0x10` (union) | case @ `0x1c` | aux1 / `0x0436` | absent |
| `MsaReservationPolicy` | 1 | `msa_reservation_size_bytes` | uint64 | `0x18` | 128 | `0x08d1` | 0 |
| `HbmPolicy` | — | *(no fields — marker)* | — | — | — | — | — |

```text
_table_   VAs:  spec 0x21cfa708 · MSA 0x21cfa658 · HBM 0x21cfa6b8
_globals_ VAs:  spec 0x223c8920 · MSA 0x223c8538 · HBM 0x223c8558   (all byte-zero)
ctors:          spec 0x1db25fa0 · MSA via Arena::DefaultConstruct 0x1db638e0 · HBM 0x1db25ea0
```

[NOTE] The parent `_table_` @ `0x21cfa708` carries an oneof: both arms store at object `+0x10` (the union), with the active case discriminator at `+0x1c`. The aux sub-table pointers at `_table_+0x68` relocate to the `MsaReservationPolicy` table (`0x21cfa658`) and the `HbmPolicy` table (`0x21cfa6b8`), confirmed by a `readelf -r` reloc walk.

The `MsaReservationPolicy::ByteSizeLong` decompile (@ `0x1db25e60`) corroborates the one-field dictionary directly: it tests the hasbit at object `+0x10` bit 0, reads the uint64 at `*((qword*)this + 3)` (= `+0x18`), and adds the standard single-uint64 varint length `(9·bsr64(v|1) + 137) >> 6`. No other field is serialized — there is none.

---

## The Resolver: `ResolveMemorySpaceSpec` @ `0x11036320`

`ResolveMemorySpaceSpec(out, Target&, HloModule&, AutoOr<spec>)` is a 0x16f-byte sret function. Its defining behavior is that it **always** builds a candidate `MsaReservationPolicy` sized from VMEM — *even when the user supplied a spec* — and only then decides whether to keep that candidate or return the user's spec. The decompile structure:

```c
// 0x11036320 — recovered control flow (cleaned)
TpuCustomCallMemorySpaceSpec *ResolveMemorySpaceSpec(
    TpuCustomCallMemorySpaceSpec *out, Target *target,
    const HloModule *module, const AutoOr<spec> *spec)
{
  TpuCustomCallMemorySpaceSpec local;                 // -0x50, case = 0
  TpuCustomCallMemorySpaceSpec::ctor(&local, /*arena=*/0);

  MsaReservationPolicy *msa;
  if (local._case == 1) {                             // (never on the fresh local)
    msa = local._union_ptr;
  } else {
    local.clear_policy();
    local._case = 1;                                  // select msa_reservation_policy
    msa = Arena::DefaultConstruct<MsaReservationPolicy>(arena_of(local));
    local._union_ptr = msa;
  }

  // --- AUTO size: one quarter of the FREE vmem, floored at 10 MiB ---
  int64_t vmem    = Target::VmemSizeBytes(target);              // Target+0x458, i32 -> i64
  int64_t overlay = (*target->vptr[0x220/8])(target);          // OverlayReservedVmemBytes()
  int64_t scoped  = scoped_memory_util::DefaultScopedVmemBytes(target, module);
  int64_t free    = vmem - (overlay + scoped);
  int64_t quarter = (int64_t) truncf((float)free * 0.25f);     // mulss 0x84a2724 ; cvttss2si
  uint64_t size   = (quarter >= 0xa00001) ? quarter : 0xa00000; // max(quarter, 10 MiB)

  msa->msa_reservation_size_bytes = size;             // store at msa+0x18
  msa->_hasbits[0] |= 1;                              // hasbit at msa+0x10 bit 0

  // --- keep the AUTO candidate, or return the user's spec ---
  if (spec->present == 0) {                           // AUTO  (byte at AutoOr+0x28 == 0)
    out = TpuCustomCallMemorySpaceSpec::ctor(out, 0);
    /* CopyFrom / InternalSwap the local AUTO candidate into out */
  } else {                                            // user-supplied spec
    const spec *user = (spec->variant_idx == 1)       // variant index at AutoOr+0x20
                         ? *(spec**)spec               //   1 = pointer
                         : spec;                       //   0 = inline; else throw
    out = TpuCustomCallMemorySpaceSpec::ctor(out, 0, user);  // copy the USER spec
  }
  local.~TpuCustomCallMemorySpaceSpec();
  return out;
}
```

[NOTE] The `AutoOr<T>` wrapper stores its presence flag at `+0x28` (0 = `AUTO`/absent) and a variant discriminator at `+0x20` (0 = inline value, 1 = pointer-to-value; any other value triggers `std::__throw_bad_variant_access`). `RunImpl` materializes the `AutoOr` from the comp-env via `AutoOr<spec>::FromProtoOrDie` (@ `0x1103f2e0`) before calling the resolver.

The practical upshot: **AUTO is not "do nothing"** — AUTO means "synthesize an MSA reservation of one quarter of free VMEM." A user spec (whichever arm) replaces that candidate wholesale.

---

## The AUTO Size Formula

The free-VMEM derivation, with byte-exact constants:

```text
free  = Target::VmemSizeBytes()                                  ; movslq [Target+0x458]   (i32 -> i64)
        − ( Target::OverlayReservedVmemBytes()                   ; (*vptr)[0x220]
            + scoped_memory_util::DefaultScopedVmemBytes(T, M) )  ; @0x1c864e40
size  = (uint64) truncf( (float)free * 0.25f )                   ; mulss [0x84a2724] = 0x3e800000 = 0.25f
size  = max(size, 0x00a00000)                                    ; cmp $0xa00001 / cmovge → 10 MiB floor
```

| term | source | VA / constant |
|---|---|---|
| total VMEM | `Target::VmemSizeBytes` (reads `Target+0x458`, i32) | `0x1d615e00` |
| − overlay reserve | `Target::OverlayReservedVmemBytes` (vtable call `*0x220`) | base `0x1d48fc20` returns 0; Ghostlite = `ChunkSizeBytes() << 4`; Viperfish = `ChunkSizeBytes() << 4` except the `"lite"` (v5e) SKU which returns 0 |
| − default scoped VMEM | `scoped_memory_util::DefaultScopedVmemBytes` | `0x1c864e40` |
| × `0.25f` | `.rodata` float | `0x84a2724` (`0x3e800000`) |
| trunc → uint64 | `cvttss2si` | — |
| floor `0x00a00000` | 10 MiB lower clamp | `cmp $0xa00001 ; cmovge` |
| → `msa_reservation_size_bytes` | stored at `MsaPolicy+0x18`, hasbit `MsaPolicy+0x10` bit 0 | — |

### The scoped-VMEM working-set term

`DefaultScopedVmemBytes` (@ `0x1c864e40`) is the working set carved out of VMEM before the quarter is taken. With no `HloModule` it tail-calls the per-target cap directly; otherwise it reserves a ring-sum buffer region and clamps to the cap. The cap is **not** simply the platform-default vtable call: it is a comp-env field at `TpuCompEnv+0x10f0` interpreted in KiB (`<< 10`), and the `(*vptr)[0x228]` platform default is used only as the fallback when that field reads `-1`:

```text
capKiB  = *(int64*)(TpuCompEnv + 0x10f0)                ; signed; -1 means "unset"
cap     = (capKiB == -1) ? (*vptr)[0x228]()             ; DefaultPlatformScopedMemoryBytes() fallback
                         : (capKiB << 10)                ; else comp-env override, KiB -> bytes
total   = Target::VmemSizeBytes()
overlay = (*vptr)[0x220]()                              ; OverlayReservedVmemBytes()
chunkB  = Target::ChunkBytes()                          ; (*(Target+0x3b8))[0x1a8] * 4
chunks  = ring_sum_emitter_utils::GetReservedVmemBufferSizeChunks(Target, TpuCompEnv)   ; @0x1c86a820
scoped  = total − (chunks * chunkB + overlay)
return    (scoped >= cap) ? cap : scoped                ; min(scoped, cap)
```

[NOTE] The comp-env scoped-cap override field lives at `TpuCompEnv+0x10f0` and is read as a signed `int64` in KiB; back-mapping it to its named TCE knob is **OPEN** (LOW). When the field is `-1` the per-SKU `DefaultPlatformScopedMemoryBytes` (the `*0x228` vtable slot) supplies the cap.

`GetReservedVmemBufferSizeChunks` (@ `0x1c86a820`) returns `16 · *(TpuCompEnv+0x12c0)` (i.e. `(TpuCompEnv+0x12c0) << 4`) when `inference_short_ring_sum_emitter_utils::IsSupported` holds, else 0 — i.e. the ring-sum buffer chunk count is a comp-env field at `+0x12c0`. Back-mapping that field to its named TCE knob is **OPEN** (LOW).

### Per-target constants

The vtable slots resolve through the **`vptr = vtable_symbol + 0x10`** ABI offset (proven from the `Target` base ctor's `add $0x10,%rax` after `lea` of the vtable symbol). A naïve "reloc at symbol+offset" reading mis-names these slots; the corrected map:

| call offset | resolves to | method |
|---|---|---|
| `*0x220` | symbol `+0x230` | `Target::OverlayReservedVmemBytes() const` |
| `*0x228` | symbol `+0x238` | `Target::DefaultPlatformScopedMemoryBytes() const` |

`DefaultPlatformScopedMemoryBytes` per concrete SKU (each a 6-byte `mov $imm,%eax ; ret`):

| Target | scoped cap | overlay reserve |
|---|---|---|
| `JellyfishTarget` | `0x1000000` = 16 MiB | 0 |
| `ViperfishTarget` | `0x1000000` = 16 MiB | `0` on the `"lite"` (v5e) SKU, else `ChunkSizeBytes() << 4` (v5p) |
| `PufferfishTarget` | `0x1000000` = 16 MiB | 0 |
| `GhostliteTarget` | `0x2000000` = 32 MiB | `ChunkSizeBytes() << 4` |
| `Target` (base) | string-keyed lookup @ `0x1d61d200` (LOW: not transcribed) | 0 |

So on Jellyfish/Pufferfish the AUTO reservation is `max(free/4, 10 MiB)` with `free = VMEM − (Overlay + ScopedVmem)`, a zero overlay reserve, and the scoped working set capped at 16 MiB; Ghostlite caps the working set at 32 MiB and adds an overlay reserve of `16 · ChunkSizeBytes()`. See [MSA Per-Version Defaults](msa-per-version-defaults.md) for the per-gen MSA tuning that *spends* this reservation.

---

## The Dispatcher: `RunImpl` @ `0x110364a0`

`RunImpl` is the pass entry. It reads the comp-env, resolves the spec, runs alias analysis, then switches on the resolved oneof case. The recovered flow:

```c
// 0x110364a0 — recovered (cleaned)
StatusOr<bool> RunImpl(HloModule *module, const flat_hash_set<string_view> &execution_threads)
{
  Target *target = module->target();                          // [module+8]
  AutoProto *env  = GetTpuCompEnv(module)->slot(2992) ?: &AutoProto_globals_;

  AutoOr<spec> au; AutoOr<spec>::FromProtoOrDie(&au, env);     // 0x1103f2e0
  spec resolved;
  ResolveMemorySpaceSpec(&resolved, target, module, &au);     // 0x11036320
  // ... copy `resolved` into module-side spec_ (CopyFrom / InternalSwap) ...
  destroy(au);

  VLOG(1) << "Memory space spec: " << module->spec_;          // .cc:511

  switch (module->spec_._case /* [module+52] = +0x34, mirrors spec+0x1c */) {
    case 0:  return true;                                     // unset → no placement change
    default: {
      auto aa = HloAliasAnalysis::Run(module, alias_info);    // .cc:518 on error
      switch (module->spec_._case) {
        case 2: return RunHbmPolicy(this, module, aa);        // 0x11038120  (HBM)
        case 1: return RunMsaReservationPolicy(this, module, aa); // 0x110367c0 (MSA)
        default: return true;
      }
    }
  }
}
```

[NOTE] The resolved oneof case is read from `[module + 52]` (= `+0x34`), which mirrors the spec's `_case_` field at spec `+0x1c`. Case 0 short-circuits *before* alias analysis is even built — no `HloAliasAnalysis::Run`, immediate `StatusOr<bool>` OK. Cases 1 and 2 first build alias analysis, then dispatch. Each arm re-guards its own `oneof` case via a `DCHECK` (below), so the dispatch invariant is checked twice.

### Arm 1 — `RunMsaReservationPolicy` @ `0x110367c0`

Re-guards `DCHECK(spec_.has_msa_reservation_policy())` (`.cc:534`), logs `"Running MSA reservation policy"`, and walks `HloModule::MakeNonfusionComputations` (@ `0x1e5e2320`) against the alias analysis. It reads the resolved reservation byte-count from the spec union (`MsaReservationPolicy+0x18`) and carves that many bytes of VMEM as a reservation the [MSA optimizer](msa-overview.md) is allowed to spend. The per-instruction work runs through an internal `MsaReservationPolicyInstructionProcessor` (instructions sorted by IO size).

### Arm 2 — `RunHbmPolicy` @ `0x11038120`

Re-guards `DCHECK(spec_.has_hbm_policy())` (`.cc:578`), logs `"Running HBM policy"`, and (like the MSA arm) walks the non-fusion computations against alias analysis. Because `HbmPolicy` has **no fields**, there is no size to read — the arm forces the matched custom-call buffers into HBM unconditionally via a `MemorySpaceColorMap`. Selecting the `hbm_policy` arm *is* the entire instruction.

[NOTE] **OPEN — placement write site & match predicate.** Both arms confirmedly walk `MakeNonfusionComputations` + alias analysis and (MSA arm) consume `+0x18`; the exact `MemorySpace` enum integer each arm stamps via the color map / shape mutator, and the custom-call *match predicate* (which target names are eligible), were not byte-traced to the store immediate. Confidence on the placement *effect*: HIGH that MSA reserves VMEM and HBM forces HBM; LOW on the concrete enum value written.

---

## The Gate: `ShouldEnablePass` @ `0x110362a0`

The pass only runs when memory-space assignment is enabled for the module. `ShouldEnablePass` calls `IsMemorySpaceAssignmentEnabled(Target, ObjectView<TpuCompilationEnvironment>, HloModule)` (@ `0x12fc1280`) and returns its `StatusOr<bool>`:

```c
// 0x110362a0 — recovered (cleaned)
StatusOr<bool> ShouldEnablePass(/* Target, env, module */)
{
  StatusOr<bool> enabled = IsMemorySpaceAssignmentEnabled(/* ... */);
  if (enabled.ok()) {
    return {.ok = true, .value = enabled.value};   // movb $0,+8 ; movq $1,+0  (StatusOr ok rep)
  } else {
    return Status::AddSourceLocation(enabled.status(), /*line*/475,
                                     "platforms/.../tpu_custom_call_memory_space_policy.cc");
  }
}
```

So per-custom-call placement is conditional on the broader MSA enable flag — if the module does not run memory-space assignment, this pass is skipped entirely and the AUTO reservation is never synthesized. The exact TCE flag(s) `IsMemorySpaceAssignmentEnabled` reads were not transcribed (LOW); the gating relationship itself is HIGH.

---

## Reimplementation Contract

A faithful reimplementation must:

1. Model `TpuCustomCallMemorySpaceSpec` as a `oneof policy` of `MsaReservationPolicy { uint64 msa_reservation_size_bytes = 1; }` and `HbmPolicy {}`, defaulting to unset (case 0).
2. On `AUTO`/absent spec, synthesize an `MsaReservationPolicy` with `msa_reservation_size_bytes = max(floor((Vmem − Overlay − ScopedVmem) × 0.25), 10 MiB)`, using single-precision float multiply + truncation (not double, not integer divide) and the exact `0x00a00000` floor.
3. Derive `ScopedVmem` as `min(Vmem − (ring_sum_chunks · ChunkBytes + Overlay), PlatformScopedCap)` with the per-SKU caps (16 MiB JF/VF/PF, 32 MiB GL) and per-SKU overlay reserve.
4. On a user-supplied spec, return it verbatim — do **not** merge the AUTO candidate into it.
5. Dispatch on the resolved oneof case: 1 → reserve VMEM sized by the field; 2 → force HBM; 0 → no-op. Build alias analysis only for cases 1/2.
6. Gate the entire pass on memory-space-assignment-enabled.

---

## Cross-References

- [MSA Overview](msa-overview.md) — the memory-space-assignment ILP optimizer that *spends* the reservation this page carves.
- [MSA AllocateSegment](msa-allocate-segment.md) — the allocation body + config proto that consumes the VMEM budget.
- [MSA Per-Version Defaults](msa-per-version-defaults.md) — per-gen overlap ratios / outstanding-copy caps applied on top of the reservation.
- [Custom-Call Lowering & the Target Registry](custom-call-lowering.md) — the custom-call dispatch layer; this placement pass is one consumer of custom-call metadata.
- [VMEM Allocator](../memory/vmem-allocator.md) — the runtime VMEM space the AUTO reservation is sized against.
- [HBM Allocator](../memory/hbm-allocator.md) — the HBM space the `hbm_policy` arm forces buffers into.
- [Memory Subsystem Overview](../memory/overview.md) — VMEM/HBM/CMEM/SMEM space map.
- [back to index](../index.md)
