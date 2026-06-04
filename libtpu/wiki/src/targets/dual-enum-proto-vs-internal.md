# Dual Enum — Proto vs Internal

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

TPU generation is represented by two distinct C++ enums inside `libtpu.so`, and they are deliberately off by one. The internal runtime enum `tpu::TpuVersion` is 0-based and chronological: `kJellyfish=0` through `k6acc60406=5`. The on-wire enum `tpu::TpuVersionProto` is 1-based, reserving `0` for an unspecified/invalid sentinel: `TPU_VERSION_JELLYFISH=1` through `TPU_VERSION_6acc60406=6`. Everything that crosses a serialization boundary — a `TpuChipPartsProto`, a program ABI proto, an RPC carrying a target description — uses the proto enum; everything that dispatches at runtime uses the internal enum. The bridge between them is the single arithmetic identity `internal = proto − 1`.

This is the standard protobuf-versus-runtime split, and it exists for the standard reason. Protocol Buffers convention reserves enum value `0` for a default/unknown that the wire format assigns to any field that was never explicitly set; if the first real generation were proto value `0`, an unset version field would silently decode as Jellyfish. So the proto enum starts its real values at `1` and keeps `0` as `TPU_VERSION_INVALID`. The runtime enum has no such constraint — it is never default-decoded, only ever assigned explicitly — so it starts at `0` for a dense, zero-based dispatch index that maps directly onto pointer-table and jump-table offsets. The cost of those two locally sensible choices is that the same silicon carries two different integers, and a reader who sees "version 6" in a serialized blob and "version 5" in a runtime log is looking at the same chip.

The page documents both conversion functions (`TpuVersionFromProto`, `TpuVersionToProto`), the flag parse/unparse pair that round-trips through codename strings, the full wire-value table, and the one place this off-by-one surfaces as a real-world gotcha: the embedded `chip_parts` blob whose `version` field reads `6` for the silicon the runtime calls version `5`.

For reimplementation, the contract is:

- The two enums and their value spaces: internal `TpuVersion` ∈ `[0,5]`, proto `TpuVersionProto` ∈ `[1,6]` with `0 = TPU_VERSION_INVALID`.
- The bidirectional conversion: `ToProto(v) = v + 1` (total), `FromProto(p) = p − 1` for `p ∈ [1,6]`, error otherwise.
- Which API surface speaks which enum, so serialized data and runtime dispatch agree on the silicon.

| | |
|---|---|
| **Internal enum** | `tpu::TpuVersion` — 0-based, `kJellyfish=0` … `k6acc60406=5` |
| **Wire enum** | `tpu::TpuVersionProto` — 1-based, `TPU_VERSION_INVALID=0`, `TPU_VERSION_JELLYFISH=1` … `TPU_VERSION_6acc60406=6` |
| **proto → internal** | `tpu::TpuVersionFromProto` @ `0x20b3a8c0` — `switch`, `internal = proto − 1`, `StatusOr` |
| **internal → proto** | `tpu::TpuVersionToProto` @ `0x20b3a8a0` — `return version + 1;` (1 instruction, total) |
| **proto → internal (fatal)** | `tpu::TpuVersionFromProtoOrDie` @ `0x20b3aa20` — wraps FromProto, dies at `tpu_version.cc:428` |
| **flag parse** | `tpu::AbslParseFlag` @ `0x20b3aaa0` — codename string → internal enum |
| **flag unparse** | `tpu::AbslUnparseFlag` @ `0x20b3ab40` — internal enum → codename string |
| **the v6 trap** | embedded `chip_parts.binarypb` `version` field = `6` (proto) = internal `5` (6acc60406) |

---

## Two Enums, One Silicon

> **Note: the off-by-one is a deliberate dual axis, not a bug.** Two correct enums describe the same silicon on two different number lines, and the `±1` between them is intentional. The internal axis is `tpu::TpuVersion` ∈ `[0,5]`: `kJellyfish=0, kDragonfish=1, kPufferfish=2, kViperfish=3, kGhostlite=4, k6acc60406=5`. The wire axis is `tpu::TpuVersionProto` ∈ `[0,6]`: `TPU_VERSION_INVALID=0, TPU_VERSION_JELLYFISH=1, TPU_VERSION_DRAGONFISH=2, TPU_VERSION_PUFFERFISH=3, TPU_VERSION_VIPERFISH=4, TPU_VERSION_GHOSTLITE=5, TPU_VERSION_6acc60406=6`. A reimplementation must carry **both** axes and convert at the seam (`internal = proto − 1`, `proto = internal + 1`); collapsing them onto one number line either re-introduces the unset-field hazard the proto axis exists to avoid (if you make the wire enum 0-based) or wastes a dispatch slot and forces a `−1` at every table index (if you make the internal enum 1-based). Do not "fix" the gap — respect it.

The two enums are not redundant; each is the right shape for where it lives.

`TpuVersion` (the internal enum) is a dense, zero-based dispatch index. It is what `TpuVersionToString` indexes into the `off_22011BF0` pointer table, what `TpuCodec::Create` switches on, what the HAL factories are constructed with. Zero-based density matters because the value is used as an array subscript: `off_22011BF0[version]` is a direct load, and a one-based enum would waste slot 0 or force a `-1` at every index site.

`TpuVersionProto` (the wire enum) is a one-based protobuf enum with a reserved `0`. The enumerator names are codename-derived — `TPU_VERSION_JELLYFISH`, `TPU_VERSION_DRAGONFISH`, `TPU_VERSION_PUFFERFISH`, `TPU_VERSION_VIPERFISH`, `TPU_VERSION_GHOSTLITE`, `TPU_VERSION_6acc60406` — with `TPU_VERSION_INVALID` occupying value `0`. These names are recovered as `.rodata` reflection strings the protobuf descriptor carries for text-format printing and parsing.

> **Note:** The proto enumerator strings are `TPU_VERSION_<CODENAME>` (with `TPU_VERSION_INVALID=0`), confirmed in the binary's reflection-string table — *not* `TPU_V2`/`TPU_V3`/`TPU_V7X`. Those `TPU_v*` tokens are the *external display* and *Cloud accelerator-type* names (the `"TPU v4"` / `"TPU7x"` strings emitted by `TpuVersionToExternalName`), a separate axis. The numeric mapping (proto 1↔Jellyfish … proto 6↔6acc60406) is the same either way; the enumerator labels are the codename form.

---

## The Conversion Functions

### `TpuVersionToProto` — internal → wire

The forward conversion is as simple as a conversion gets. `tpu::TpuVersionToProto` (`0x20b3a8a0`) is a single arithmetic instruction:

```c
TpuVersionProto TpuVersionToProto(TpuVersion version) {   // sub_20B3A8A0
    return version + 1;                                    // total function, no bounds check
}
```

It is total and unchecked — there is no range guard, because the caller is expected to hold a valid internal enum, and `version + 1` maps `[0,5] → [1,6]` exactly onto the proto range. The absence of a guard is itself evidence of the contract: the producer side trusts its own enum and just shifts it up by one when serializing.

### `TpuVersionFromProto` — wire → internal

The reverse conversion must validate, because the proto value arrives from outside (a deserialized blob, an RPC field) and could be `0` (unset) or out of range. `tpu::TpuVersionFromProto` (`0x20b3a8c0`) returns a `StatusOr<TpuVersion>`:

```c
StatusOr<TpuVersion> TpuVersionFromProto(TpuVersionProto proto) {   // sub_20B3A8C0
    switch (proto) {
        case 1: return ok(0);   // TPU_VERSION_JELLYFISH  -> kJellyfish
        case 2: return ok(1);   // TPU_VERSION_DRAGONFISH -> kDragonfish
        case 3: return ok(2);   // TPU_VERSION_PUFFERFISH -> kPufferfish
        case 4: return ok(3);   // TPU_VERSION_VIPERFISH  -> kViperfish
        case 5: return ok(4);   // TPU_VERSION_GHOSTLITE  -> kGhostlite
        case 6: return ok(5);   // TPU_VERSION_6acc60406  -> k6acc60406
        default:                 // 0 (INVALID/unset) or > 6
            return error("Invalid TPU version: " + proto,
                         ".../tpu_version.cc", 421);
    }
}
```

Every valid arm computes `internal = proto − 1`. The `default` arm covers both the protobuf-unset case (`proto == 0`, `TPU_VERSION_INVALID`) and any future/garbage value `> 6`, returning a non-OK `Status` with message `"Invalid TPU version: <N>"` from line 421. The asymmetry with `ToProto` is the whole point: serialization is total and trusted, deserialization is checked and fallible.

`tpu::TpuVersionFromProtoOrDie` (`0x20b3aa20`) is the convenience wrapper for call sites that know the proto is well-formed (e.g. one freshly produced by `ToProto`, or read from a build-time-validated chip-parts blob). It calls `FromProto`, and if the status is not OK, raises `LogMessageFatal` at `tpu_version.cc:428` with `"Could not read TPU version from protobuf."`. The bundle-encoder dispatcher `tpu::ProgramProtoUtil::BundleCount` (`0x1e830e80`) uses this OrDie form: it pulls a proto-side version field (the `version` member of the embedded `TpuCoreProgramAbiProto`), converts with `FromProtoOrDie`, then switches on the internal value to pick the encoder family — pairing internal `0,1 → CreateEncoderJfDf`, `2 → CreateEncoderPf`, `3 → CreateEncoderVf`, `4,5 → CreateEncoderGlGf`.

### `AbslParseFlag` / `AbslUnparseFlag` — the string round-trip

A third conversion pair bridges the internal enum and its codename string for command-line flags. `tpu::AbslUnparseFlag` (`0x20b3ab40`) turns an internal `TpuVersion` into its codename by indexing the same `off_22011BF0` pointer table and `qword_BDF3BD8` length table that `TpuVersionToString` uses. `tpu::AbslParseFlag` (`0x20b3aaa0`) is its inverse: it takes a codename `string_view` and resolves it back to an internal enum value, with an error output string for an unrecognized codename. The two together let a `--tpu_version=ghostlite` flag round-trip to `kGhostlite=4` and back. Both operate purely in the *internal* enum space and the *codename* string space — neither touches the proto enum. A reimplementation must keep the flag path on the internal enum; routing it through the proto enum would introduce the off-by-one into user-facing flag values.

---

## Full Wire-Value Table

The complete cross-walk between the two enums, with the codename each shares and the external display name. The proto enumerator names are the codename-derived `TPU_VERSION_*` identifiers from the descriptor; the external names are the `TpuVersionToExternalName` outputs.

| Internal `TpuVersion` | Internal tag | `TpuVersionProto` | Proto enumerator | Codename | External name | Confidence |
|---:|---|---:|---|---|---|---|
| — | (none) | 0 | `TPU_VERSION_INVALID` | (sentinel) | (error) | HIGH |
| 0 | `kJellyfish` | 1 | `TPU_VERSION_JELLYFISH` | `jellyfish` | `TPU v2` | HIGH |
| 1 | `kDragonfish` | 2 | `TPU_VERSION_DRAGONFISH` | `dragonfish` | `TPU v3` | HIGH |
| 2 | `kPufferfish` | 3 | `TPU_VERSION_PUFFERFISH` | `pufferfish` | `TPU v4` | HIGH |
| 3 | `kViperfish` | 4 | `TPU_VERSION_VIPERFISH` | `viperfish` | `TPU v5` | HIGH |
| 4 | `kGhostlite` | 5 | `TPU_VERSION_GHOSTLITE` | `ghostlite` | `TPU v6 lite` | HIGH |
| 5 | `k6acc60406` | 6 | `TPU_VERSION_6acc60406` | `6acc60406` | `TPU7x` | HIGH |

The relationship is uniform: proto value = internal value + 1, and proto value 0 has no internal counterpart. The external "TPU vN" name is a *third* numbering that aligns with neither enum's integer — `TPU v2` is internal 0 / proto 1, `TPU7x` is internal 5 / proto 6 — which is exactly why all three numberings must be kept distinct.

The `TpuVersionProto` descriptor is exactly seven entries wide and no wider. Its serialized reflection table — file `tpu_version.proto`, package `tpu`, recovered as a contiguous `.rodata` block — lists `TPU_VERSION_INVALID`, then the six codename enumerators, and stops; the `descriptor` template instantiation `TpuVersionProto_descriptorEv…Li0ELi6E` carries the bounds `0..6` as compile-time constants. There are **no** proto-only codenames, no `_LITE` variants, and no compatibility sentinels inside `TpuVersionProto`: every one of its values has a `FromProto` arm. The `PUFFYLITE`, `VIPERLITE`, `DARWINN`, and `CPU_ONLY` tokens that also appear in the binary belong to *other* enums — `TpuTypeProto` (descriptor `TpuType`/`UNKNOWN_TPU_TYPE`) and the xprof accelerator-type enum (`UNKNOWN_DEVICE`/`NVIDIA_GPU`/`SEASTAR`) — not to `TpuVersionProto`. They share codename spellings but live in different descriptors and never reach `TpuVersionFromProto`.

> **Note:** `TPU_V1_COMPAT_BRIDGE` is not a `TpuVersionProto` enumerator — it is a substring of the unrelated MLIR-bridge phase string `TFXLA_PHASE_ONE_MLIR_TPU_V1_COMPAT_BRIDGE`. The `*LITE`/`DARWINN`/`CPU_ONLY` tokens are enumerators of `TpuTypeProto` and the xprof accelerator enum, not version values. The `TpuVersionProto` reflection block contains exactly `TPU_VERSION_INVALID` plus the six `TPU_VERSION_<CODENAME>` values — seven entries, no more.

---

## Why Both Exist

The two enums encode two different stability contracts, and merging them would break one of them.

`TpuVersionProto` is a **wire-stability** contract. Once a proto enum value ships, it is frozen forever: a serialized `chip_parts` blob, a stored program ABI, an RPC from an older binary must all decode the same way years later. The proto enum can only ever *append* (a future generation gets value 7), never renumber. The reserved `0` and the one-based numbering are the protobuf idioms that make an unset field decode to a recognizable `INVALID` rather than to a real generation.

`TpuVersion` is a **dispatch-efficiency** contract. It is a private, in-process index; nothing serializes it. It is zero-based so it can subscript arrays directly (`off_22011BF0[version]`, jump tables, per-version constant tables) with no offset. Because it never crosses a process boundary, the runtime is free to keep it dense and chronological without worrying about wire compatibility.

Keeping them separate lets each evolve under its own rules: the proto enum guarantees old data still parses, the internal enum guarantees fast indexing. The `± 1` conversion is the small, total/checked seam between the two worlds — and the conversion functions being so trivial (`+1` one instruction, `-1` a six-arm switch) is the signal that the separation is intentional and cheap, not an accident of history.

> **Note: the chip_parts "version 6" trap.** The embedded resource `6acc60406_chip_parts.binarypb` (and its `6acc60406_tensornode_chip_parts.binarypb` sibling) begins with the bytes `08 06`: protobuf field 1 (`version`), wire-type 0 (varint), value `6`. That `6` is a `TpuVersionProto` value (`TPU_VERSION_6acc60406`), **not** an internal `TpuVersion`. The runtime feeds it through `TpuVersionFromProto`, which maps proto `6 → internal 5`. So the blob that says "version 6" describes the silicon the runtime dispatches as version `5`. A reimplementation that reads the chip-parts `version` field and uses it directly as a dispatch index will be off by one on every embedded blob — and on the newest generation specifically, it will index past the end of a 6-slot table. Always run a serialized `version` field through the `proto − 1` conversion before using it as an internal index.

---

## Cross-References

- [TPU Version Codename Matrix](tpu-version-codename-matrix.md) — the canonical enum-to-codename pointer table and the full five-axis cross-walk
- [Part IV Overview](overview.md) — where the proto and internal enums each surface in the pipeline
- [Chip Parts binarypb](chip-parts-binarypb.md) — the embedded `chip_parts` blobs whose `version` field is the proto enum
- [HAL Families](hal-families.md) — the factory routing that consumes the internal enum
- [ISA Overview](../isa/overview.md) — the codec/encoder families selected by the internal enum
