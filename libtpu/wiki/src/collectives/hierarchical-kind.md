# HierarchicalKind

> *All addresses, symbols, and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped, `.text` VMA == file offset `0xe63c000`, build `libtpu_lts_20260413_b_RC00`). Other versions will differ; treat every VA as version-pinned.*

## Abstract

`HierarchicalKind` is the single argument that decides whether a SparseCore-offload collective is built as a **flat single ring** or a **hierarchical multi-phase ring list**. It looks like a three-valued enum in the demangled builder signature, but it is not a dense `0/1/2` enum: it is the 16-bit **bit-packed value of an `xla::jellyfish::AutoOr<bool>`**, the wrapper type that distinguishes *"flag is set"* from *"flag holds value X"*. Every consumer in the offload-config plumbing treats it as a discriminant masked with `0x101` — bit `0x1` carries the contained boolean, bit `0x100` is the *engaged* bit. This page owns exactly that:

- **The `AutoOr<bool>` bit-encoding** — how `0x1` (value) and `0x100` (engaged) are packed by `AutoOr<bool>::FromProtoOrDie`, and why the discriminant is the 9-bit mask `0x101` rather than a low integer.
- **The `& 0x101` / `== 0x100` decode** — the exact arithmetic that classifies a `HierarchicalKind` into hierarchical / explicitly-flat / not-engaged, byte-anchored to `ShouldEnableSparseCoreHierarchicalAllReduce` and the two builder bodies.
- **The kind → decomposition mapping** — which of the four discriminant values selects which collective decomposition, and which of the three op families (AllGather / AllReduce / ReduceScatter) can ever observe each value.

The *builder body* that consumes the kind, and the `*OffloadConfig` structs that the kind feeds, are on **[SC-Offload Config Builder](sc-offload-config-builder.md)**. The actual *multi-phase emission* the `0x101` path produces — the D2D intra-chip ring + per-torus-axis inter-chip rings, and the `IciStrategyRingConfig` leaf field map — is on **[Hierarchical AllReduce / Pincer](allreduce-hierarchical-pincer.md)**. This page is the *meaning of the discriminant*; those pages are what it switches between.

Contract of `HierarchicalKind` as observed in the binary:

- The kind originates from the compile flag `xla_tpu_enable_sparse_core_hierarchical_all_reduce`, read through `AutoOr<bool>::FromProtoOrDie @0xf795300`, which packs `value | 0x100` when the flag is present and returns `0x000` when it is not.
- The flag's compiled-in default is **false** (`AbslFlagDefaultGenFor… @0x1d726a00` stores `0`); the hierarchical path is opt-in.
- The only `HierarchicalKind` value the builders treat as *flat* is exactly `0x100` (engaged + value-false). Anything matching the `0x101` discriminant is *hierarchical*; anything not engaged falls through to flat.
- **AllGather and ReduceScatter hardwire `HierarchicalKind = 0x100`** in their ND wrappers, so they are structurally pinned flat. **Only AllReduce** computes a real kind and can reach the hierarchical decomposition.

## At a glance

| Aspect | Value (byte-anchored) |
|--------|----------------------|
| Underlying type | `xla::jellyfish::AutoOr<bool>` packed into a 16-bit word |
| Source flag | `xla_tpu_enable_sparse_core_hierarchical_all_reduce` (default **false**) |
| Packer | `AutoOr<bool>::FromProtoOrDie @0xf795300` (`return value \| 0x100`; `return 0` if not engaged) |
| Bit `0x001` | the contained `bool` *value* |
| Bit `0x100` | the *engaged* bit (`AutoOr` holds a value) |
| Discriminant mask | `0x101` (every consumer masks with this) |
| Hierarchical encoding | `(kind & 0x101) == 0x101` (engaged + true) |
| Explicitly-flat encoding | `(kind & 0x101) == 0x100` (engaged + false) — what AG/RS pin |
| Default / not-engaged | `0x000` → flat fallback |
| Enable predicate | `ShouldEnableSparseCoreHierarchicalAllReduce @0x1d6b6d80` (`(~v & 0x101) == 0`) |
| Flat-vs-hier dispatch | AllReduce `@0x133c2dc0` line 806 `(kind & 0x101) != 0x100`; AllGather `@0x133c82c0` line 3102 `== 256` |
| Reachable hierarchical | AllReduce only (AG/RS pinned `0x100`) |

---

## 1. Why it is an `AutoOr<bool>`, not an enum

The demangled builder signature reads `(…, bool, bool, HierarchicalKind, optional<bool>, optional<bool>, optional<int>)`, which suggests `HierarchicalKind` is an enum class with a handful of named members. It is not. `HierarchicalKind` is a typedef/alias over the **16-bit packed representation of `xla::jellyfish::AutoOr<bool>`** — `jellyfish`'s "auto-or-explicit" flag wrapper, the same pattern used across the offload-config plumbing for flags that must distinguish *unset* from *set-to-false*.

An `AutoOr<bool>` carries two pieces of information that a bare `bool` cannot:

1. **Is the value engaged?** — was the option set at all (vs. left at its auto/default state)?
2. **What is the value?** — if engaged, the contained `bool`.

Rather than a `{tag, payload}` struct, the consumers pack both into one integer word with two non-adjacent bits, so the discriminant is the 9-bit mask `0x101` and *not* a small `0..2` range:

```text
 bit 15 ............ bit 8 ............ bit 0
  0  0  0  0  0  0  0  E  0  0  0  0  0  0  0  V
                       ^                       ^
                       |                       +-- 0x001 = contained bool VALUE
                       +-------------------------- 0x100 = ENGAGED bit
```

`E` (`0x100`) is the engaged bit; `V` (`0x1`) is the value bit. The interior bits are always zero in the values the builders see, so the *entire* semantic content is recovered by `kind & 0x101`.

This is why every consumer in this subsystem applies `& 0x101` before comparing — and why the "flat" sentinel is the otherwise-surprising constant `0x100` rather than `0` or `1`.

---

## 2. The packer — `AutoOr<bool>::FromProtoOrDie`

The kind is produced by reading the compile flag through `AutoOr<bool>::FromProtoOrDie @0xf795300`. The decompile shows the packing directly:

```c
// xla::jellyfish::AutoOr<bool>::FromProtoOrDie — 0xf795300
__int64 FromProtoOrDie(xla::jellyfish::AutoProto *a1)
{
  if ( !*((_DWORD *)a1 + 7) )       // AutoProto "engaged" word == 0 ?
    return 0;                       //   -> NOT ENGAGED : kind = 0x000
  AutoOrTypeTraits<bool>::FromAutoProto(&v10, a1);   // decode the contained bool -> v11
  // ... (on decode failure, LOG(FATAL) "Failed to convert AutoProto into an AutoOr") ...
  return v11 | 0x100u;              // ENGAGED : kind = (value & 1) | 0x100
}
```

> `FromProtoOrDie @0xf795300`: line 16-17 returns `0` when the proto's engaged word (`*((_DWORD*)a1 + 7)`) is zero, i.e. the flag was never set → `0x000`. Otherwise (line 40) it returns `v11 | 0x100u`, OR-ing the engaged bit `0x100` onto the contained boolean `v11`. So the only three values the packer can emit are `0x000` (not engaged), `0x100` (engaged + false), `0x101` (engaged + true). The source location it would `LOG(FATAL)` from is `platforms/xla/service/jellyfish/flag_types.h:845`, confirming the wrapper is `jellyfish`'s flag type.

The flag itself defaults to false:

```c
// AbslFlagDefaultGenForxla_tpu_enable_sparse_core_hierarchical_all_reduce::Gen — 0x1d726a00
*(_WORD *)this = 0;     // default value = false
```

> `AbslFlagDefaultGenFor… @0x1d726a00` stores `0` — the compiled-in default is `false`. The flag is registered into the `TpuCompilationEnvironment` (`GLOBAL__sub_I_tpu_compilation_environment.cc`), so it is a per-compilation environment option, read via `GetTpuCompEnv` at the AllReduce wrapper. Default → the contained bool is false; reaching `0x101` requires `--xla_tpu_enable_sparse_core_hierarchical_all_reduce=true`.

---

## 3. The decode — `& 0x101` and `== 0x100`

There are two distinct decode sites, with two slightly different shapes, and they agree.

### 3.1 The enable predicate — `ShouldEnableSparseCoreHierarchicalAllReduce`

```c
// xla::jellyfish::ShouldEnableSparseCoreHierarchicalAllReduce — 0x1d6b6d80
bool ShouldEnableSparseCoreHierarchicalAllReduce(__int64 comp_env)
{
  AutoProto *v1 = *(AutoProto **)(comp_env + 2632);   // the flag's AutoProto
  if ( !v1 ) v1 = &AutoProto_globals_;
  AutoOr<bool>::FromProtoOrDie(v1);                   // -> v2 (the packed kind)
  return (~v2 & 0x101) == 0;                          // true iff (v2 & 0x101) == 0x101
}
```

> `0x1d6b6d80` line 13: `return (~v2 & 0x101) == 0;`. The identity `(~v & M) == 0  ⟺  (v & M) == M` means this returns `true` exactly when **both** `0x100` and `0x1` are set — i.e. the kind is `0x101` (engaged + true). Any other value (`0x100`, `0x000`, or a stray `0x001`) returns `false`. This is the canonical "is the hierarchical AllReduce enabled" test.

### 3.2 The builder dispatch — `(kind & 0x101) != 0x100`

The templated builder reloads the 16-bit kind and tests it against the engaged-but-false sentinel `0x100`. Two encodings of the *same* predicate appear, one per op family:

```c
// AllReduce builder ConstructConfigForCollectiveUniDirNDGroups<AllReduceOffloadConfig,...> — 0x133c2dc0
// line 806:
v637 = (v63 & 0x101) != 256;       // is_hierarchical = (kind & 0x101) != 0x100   (256 == 0x100)
//        ^ mask discriminant        ^ 0x100 == engaged+false  -> FLAT (v637 = 0)
//                                                              -> else HIER (v637 = 1)
```

```c
// AllGather builder ConstructConfigForCollectiveUniDirNDGroups<AllGatherOffloadConfig,...> — 0x133c82c0
// line 3032:  LODWORD(v75) = v610 & 0x101;      // mask the discriminant
// line 3102:  if ( (_DWORD)v615 == 256 ) { ... flat ... }   // direct == 0x100 compare
// lines 3204/3262:  if ( (~(_WORD)v598 & 0x101) == 0 ) { ... hierarchical-only branch ... }
```

> AllReduce body `@0x133c2dc0` line 806 computes the `is_hierarchical` byte as `(kind & 0x101) != 0x100`. AllGather body `@0x133c82c0` line 3032 masks `& 0x101`, then at lines 3102/3525 compares the masked value directly against `256` (= `0x100`); the `(~v & 0x101) == 0` hierarchical-discriminant guard appears at lines 3204/3262. The two op families compile the *same* predicate differently (AllReduce materialises a `setne` byte from the stack arg; AllGather compares a stack slot inline) because AG can never satisfy the hierarchical arm — its wrapper pins the kind to `0x100`.

The decisive point: the builders treat **only `0x100`** as flat. Every other value falls into the hierarchical arm of `!= 0x100`. Combined with the packer (§2), which can only emit `0x000`/`0x100`/`0x101`, the reachable behaviour is:

- `0x101` (engaged + true) → hierarchical (`!= 0x100`).
- `0x100` (engaged + false) → flat (`== 0x100`).
- `0x000` (not engaged / default) → hierarchical *arm of the `!=` test*, but the `(~v & 0x101) == 0` guards inside that arm are false for `0x000`, so it collapses to the flat single-ring fallback. In practice the wrappers never forward a bare `0x000` to the builder (see §5).

---

## 4. The discriminant table

The four discriminant values and what each selects. The hierarchical decomposition itself (D2D intra-chip phase + per-torus-axis inter-chip phases) is detailed on **[Hierarchical AllReduce / Pincer](allreduce-hierarchical-pincer.md)**; here only the *selection*.

| `kind & 0x101` | engaged | value | decomposition selected | reached by |
|----------------|---------|-------|-------------------------|------------|
| `0x101` | yes | true | **HIERARCHICAL** — multi-phase ring list (D2D intra-chip + one inter-chip ring per torus axis, `IMPLICIT` neighbour) | AllReduce only (flag engaged + true) |
| `0x100` | yes | false | **EXPLICITLY FLAT** — single inter-chip ring (`EXPLICIT` neighbour + precomputed neighbour-table offset) | AG/RS (pinned) + AR (flag off) |
| `0x001` | no | — | flat fallback (engaged bit clear → `ShouldEnable`/`(~v&0x101)==0` both false) | not forwarded by wrappers |
| `0x000` | no | — | **DEFAULT** non-hierarchical (flat fallback) | the packer's not-engaged return |

The semantic split between the flat and hierarchical decompositions, summarised (full byte map on the pincer page):

| Aspect | FLAT (`0x100`) | HIERARCHICAL (`0x101`) |
|--------|----------------|------------------------|
| `phase_rings` per color | `[D2D?]` + **one** inter-chip ring | `[D2D?]` + **one ring per torus axis** |
| inter-chip neighbour | `ICI_RING_NEIGHBOR_EXPLICIT` (1) | `ICI_RING_NEIGHBOR_IMPLICIT` (2) |
| ring length carried | `core_count` set directly | implicit; `core_count_adjustment` (megacore delta) |
| neighbour table | `ring_neighbor_table_offset` + `has_reordering_map` | none (implicit ordering) |
| D2D intra-chip ring | emitted if megacore (Phase 0) | emitted if megacore (Phase 0) |

The intra-chip D2D phase is identical on both arms; the discriminant only changes the *inter-chip* phase shape — flat collapses all torus axes into a single `EXPLICIT` ring, hierarchical decomposes them into one `IMPLICIT` ring per axis. This is the SparseCore analog of the dense TensorCore reduce-scatter / all-gather phase split: a phase per torus dimension.

---

## 5. The kind → op-family mapping

`HierarchicalKind` is fixed per op family by the three public ND wrappers, *not* by the builder. Two of them hardwire flat:

| Wrapper | VA | `HierarchicalKind` passed | Can reach `0x101`? |
|---------|----|--------------------------|--------------------|
| `ConstructConfigForAllGatherUniDirND` | `0x133c76c0` | hardwired `0x100` | no — always flat |
| `ConstructConfigForReduceScatterUniDirND` | `0x133ccbe0` | hardwired `0x100` | no — always flat |
| `ConstructConfigForAllReduceUniDirND` | `0x133c2c80` | computed (see below) | **yes** |

The AllGather and ReduceScatter wrappers pass the engaged-but-false constant `0x100`, so their builder bodies always take the `== 0x100` flat branch. Only the AllReduce wrapper computes a real discriminant. Its leading `bool` argument to the templated builder is:

```c
// ConstructConfigForAllReduceUniDirND — 0x133c2c80, line 41
arg0 = ((~a8 & 0x101) != 0) & (unsigned __int8)~ShouldEnableSparseCoreHierarchicalAllReduce;
//        ^ a8 = caller's 16-bit HierarchicalKind         ^ the §3.1 enable predicate
```

> `0x133c2c80` line 35 calls `ShouldEnableSparseCoreHierarchicalAllReduce` on the result of `GetTpuCompEnv(module, all_reduce)`; line 41 folds it with `(~a8 & 0x101) != 0` (where `a8` is the `unsigned __int16` caller-supplied kind) into the leading `bool` forwarded to the builder. The RetCheck/error strings at lines 49/52/57 cite `platforms/xla/sparse_core/offload_collective_config.cc`, pinning the translation unit.

Reading line 41 as boolean logic, `arg0` is true (meaning **"use the flat path"**) unless **both** of the following hold:

1. `ShouldEnableSparseCoreHierarchicalAllReduce` is true — the compile flag is engaged + true (`0x101`), **and**
2. `(~caller_kind & 0x101) != 0` — i.e. `(caller_kind & 0x101) != 0x101`: the caller's own optional `HierarchicalKind` override does **not** itself force the hierarchical encoding closed.

So the hierarchical arm is reached only when the global flag is engaged+true *and* the caller's optional override does not veto it. With the flag's default of false (§2), AllReduce — like AllGather and ReduceScatter — runs flat unless `xla_tpu_enable_sparse_core_hierarchical_all_reduce` is explicitly enabled.

---

## 6. Reimplementation checklist

- Represent `HierarchicalKind` as a 16-bit word, **not** a dense enum. Pack it with `value(0/1) | 0x100` when the source flag is engaged; emit `0x000` when it is not.
- Read the source from an `AutoOr<bool>`-style wrapper over `xla_tpu_enable_sparse_core_hierarchical_all_reduce`; default the flag to false.
- Classify with the mask `0x101`: `== 0x101` → hierarchical, `== 0x100` → explicitly flat, anything else → flat fallback. Express "is hierarchical enabled" as `(~v & 0x101) == 0` (≡ `(v & 0x101) == 0x101`).
- In the builder, treat **only** `0x100` as flat (`(kind & 0x101) == 0x100`); route every other value to the multi-phase arm, guarded internally by `(~kind & 0x101) == 0`.
- Pin `HierarchicalKind = 0x100` in the AllGather and ReduceScatter wrappers. Only the AllReduce wrapper computes a kind; its leading "use-flat" bool is `(!ShouldEnable) | ((caller_kind & 0x101) == 0x101)`-negated — i.e. flat unless flag-engaged-true and the caller override does not force flat.

---

## 7. Verification notes

> Cross-checked against the IDA decompile of `libtpu.so` v0.0.40:
> - **Packing** — `AutoOr<bool>::FromProtoOrDie @0xf795300`: `return v11 | 0x100u` (engaged + value) / `return 0` when the engaged word is zero (line 16-17). Only `0x000`/`0x100`/`0x101` are emittable. The `LOG(FATAL)` source `flag_types.h:845` confirms the `jellyfish` flag wrapper.
> - **Default** — `AbslFlagDefaultGenFor… @0x1d726a00` stores `0` (flag default false); registered into `TpuCompilationEnvironment`.
> - **Enable predicate** — `ShouldEnableSparseCoreHierarchicalAllReduce @0x1d6b6d80` line 13: `(~v2 & 0x101) == 0` ≡ `(v2 & 0x101) == 0x101`.
> - **Dispatch** — AllReduce builder `@0x133c2dc0` line 806: `(v63 & 0x101) != 256`; AllGather builder `@0x133c82c0` line 3032 mask `& 0x101`, lines 3102/3525 `== 256`, lines 3204/3262 `(~v & 0x101) == 0`.
> - **Wrapper mapping** — AllReduce wrapper `@0x133c2c80` line 35 (`ShouldEnable…`) + line 41 (`((~a8 & 0x101) != 0) & ~ShouldEnable`); AG/RS wrappers (`0x133c76c0` / `0x133ccbe0`) pin `0x100`. TU `offload_collective_config.cc` (wrapper lines 49/52/57).
> - **Per-axis ringDim** — the kind only selects the decomposition; the per-axis ring dim is `2 - (NDPlaneInfo[+0xa0] & 1)` (AllReduce body `@0x133c2dc0` line 704 `2 - (v20[40] & 1)`), independent of `HierarchicalKind`.
>
> **[LOW]** The interior bits (`0x02..0x80`, `0x200..0x8000`) of the 16-bit word are assumed zero in every value the builders observe — consistent with the packer emitting only `0x000`/`0x100`/`0x101`, but a `HierarchicalKind` carrying arbitrary interior bits was not exercised. The `0x001` (not-engaged, value bit set) row of the §4 table is reachable only by arithmetic, not by the packer, and is never forwarded by the wrappers; it is included for completeness of the `& 0x101` decode.

---

## Cross-References

**The decomposition this kind switches between**
- [SC-Offload Config Builder](sc-offload-config-builder.md) — the templated builder body that consumes `HierarchicalKind`, and the three `*OffloadConfig` structs it feeds
- [Hierarchical AllReduce / Pincer](allreduce-hierarchical-pincer.md) — the `0x101` multi-phase emission (D2D + per-axis rings) and the `IciStrategyRingConfig` leaf field map

**Substrate and selection context**
- [On-Pod Collectives — Section Map](overview.md) — the SparseCore-offload substrate split and the strategy picker
- [SelectNDStrategy](strategy-nd-picker.md) — the dense `StrategyND` picker whose phase split this mirrors
- [ReduceScatter](reduce-scatter.md) — pinned-flat in the offload path (its wrapper hardwires `0x100`)
- [SC Core-Selection (Offload)](sc-core-selection-offload.md) — the offload op-type classification upstream of the builder
- [Tensor-split ND-plane](tensor-split-ndplane.md) — the `NDPlaneInfo[+0xa0]` parity word that picks per-axis `X_TORUS` vs `X_MESH` (orthogonal to the kind)
- [back to index](../index.md)
