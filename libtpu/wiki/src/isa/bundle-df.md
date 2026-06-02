# Dragonfish Bundle

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

Dragonfish (`TpuVersion::kDragonfish = 1`) does not have a bundle layout of its own. Its TensorCore VLIW bundle is the **identical 41-byte (328-bit) Jellyfish bundle** — same internal-struct/12-byte-strip mechanism, same slot-mask dispatch, same per-slot bit positions, same `kNeverExecute` prefill. The shared layout is not an inference from width coincidence; it is structural in the binary. `EncoderDf` is a C++ subclass of `EncoderJf` that overrides only three slot writers and contributes **no** `EncodeBundleInternal`, **no** `BundleSizeBytes`, and **no** `DragonfishCodecMetadata` — the bundle assembler, the width constant, and the codec-metadata class are all inherited from Jellyfish. Dragonfish and Jellyfish are paired everywhere the codec is selected: the encoder factory builds one shared `CreateEncoderJfDf` for versions 0+1, and Dragonfish reuses `JellyfishBundleRestrictions` rather than carrying its own.

The only Dragonfish-specific behaviour is a set of **MXU-validity checks** layered on top of the inherited Jellyfish encoders. `EncoderDf` overrides exactly the three slot writers that name an MXU — `EncodeVectorExtendedInstruction`, `EncodeVectorResultInstruction`, and `EncodeMiscInstruction` — and each override calls the Jellyfish encoder verbatim, then runs `CheckMxuValid<T>` to reject any op whose `mxu_num >= 2`. These checks write **no bundle bits**; they are pure legality gates plus an internal encoder-state flag at `this+12`, reflecting Dragonfish's different physical MXU configuration. The wire format is byte-for-byte the Jellyfish 41-byte bundle.

This page is therefore short by design: it documents the shared-codec evidence and the precise Dragonfish delta, and defers the full slot map to the canonical Jellyfish page. For reimplementation, the contract is:

- Dragonfish encodes the **same 41-byte bundle** as Jellyfish — reuse the [Jellyfish 41B Bundle](bundle-jf-41b.md) slot map verbatim.
- `EncoderDf` **inherits** `EncodeBundleInternal`, `BundleSizeBytes`, and `JellyfishCodecMetadata`; do not implement a separate Dragonfish codec.
- The only Dragonfish-specific code is `CheckMxuValid<T>` rejecting `mxu_num >= 2` in the three MXU-bearing slot encoders — a *validity* delta, not a *layout* delta.

| | |
|---|---|
| **Encoder class** | `EncoderDf` (subclass of `EncoderJf`); ctor `0x1e85e340` calls `EncoderJf::EncoderJf(this, 1)` |
| **Bundle assembler** | inherited `EncoderJf::EncodeBundleInternal` @ `0x1e86c7c0` (no `EncoderDf` override) |
| **Wire width** | **41 bytes / 328 bits** — inherited `JellyfishCodecMetadata::BundleSizeBytes` @ `0x1ecf7460` |
| **Codec metadata** | shares `JellyfishCodecMetadata`; no `DragonfishCodecMetadata` symbol exists |
| **Encoder factory** | `CreateEncoderJfDf` services versions 0 (Jellyfish) + 1 (Dragonfish) |
| **Bundle restrictions** | shares `JellyfishBundleRestrictions` (no Dragonfish-specific class) |
| **Dragonfish delta** | `CheckMxuValid<T>` (`mxu_num < 2`) in `EncodeVectorExtended/Result/MiscInstruction` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Shared-Codec Evidence

Four independent pieces of binary evidence establish that Dragonfish reuses the Jellyfish 41-byte bundle codec rather than carrying its own.

**1 — `EncoderDf` is a subclass of `EncoderJf`.** The Dragonfish encoder constructor base-constructs an `EncoderJf` and then installs its own vtable:

```c
// EncoderDf::EncoderDf()  @ 0x1e85e340 (decompiled)
EncoderJf::EncoderJf(this, 1);          // construct the Jellyfish base (config arg = 1)
*(void**)this = off_21D36BF0;           // install the EncoderDf vtable
// vmovaps/vmovups: store a 16-byte config blob at this+0x40
*((uint32*)this + 20) = 4;              // set the per-gen config word
```

The `EncoderJf::EncoderJf(this, 1)` call is the literal IS-A: a Dragonfish encoder *is* a Jellyfish encoder with a different vtable and config word. Every Jellyfish method `EncoderDf` does not override is dispatched to the Jellyfish implementation.

**2 — `EncoderDf` has no `EncodeBundleInternal` and no `BundleSizeBytes`.** A symbol-table sweep finds an `EncoderJf::EncodeBundleInternal` (`0x1e86c7c0`) and an `EncoderBcsDf::EncodeBundleInternal` (`0x1e85cd20`, the *BarnaCore-sequencer* encoder, a different sequencer type) but **no `EncoderDf::EncodeBundleInternal`** and **no `EncoderDf::BundleSizeBytes`**. The bundle assembler and width constant are inherited unchanged from Jellyfish.

**3 — there is no `DragonfishCodecMetadata`.** The codec-metadata classes are `JellyfishCodecMetadata`, `PufferfishCodecMetadata`, `ViperfishCodecMetadata`, `GhostliteCodecMetadata` — there is no Dragonfish entry. Dragonfish's width comes from the *same* `JellyfishCodecMetadata::BundleSizeBytes` (`0x1ecf7460`) that returns 41 for the TensorCore (component 0) and 16 for BarnaCore (component 1). The [Bundle Model](bundle-model-overview.md) lists Dragonfish as sharing the Jellyfish codec metadata for exactly this reason.

**4 — the factory and bundle restrictions pair 0+1.** The encoder factory routes versions 0 and 1 to one shared `CreateEncoderJfDf`, and Dragonfish reuses `JellyfishBundleRestrictions` rather than a Dragonfish-specific class. The pairing is direct binary evidence of the shared codec — see the [Codename Matrix](../targets/tpu-version-codename-matrix.md) and the [JXC Family](../targets/jxc-family.md) page, where one `TpuHalJxcHardwareFactory` and one shared compiler-side `xla::jellyfish::isa` namespace serve both generations.

> **NOTE — `TpuCodecDragonfish` exists, but it is not a separate *bundle* codec.** `TpuCodec::Create` has a case 1 that builds a named `TpuCodecDragonfish` object, and `TpuCodecDragonfish::EncodeBundle` (`0x1e8369a0`) exists. But that codec wrapper delegates bundle assembly to the shared `CreateEncoderJfDf` encoder — the named codec is a dispatch/RTTI wrapper, not a distinct 41-byte layout. A reimplementation should model one 41-byte bundle codec for both generations, dispatched by a `TpuVersion` argument, not two.

---

## The Dragonfish Delta: MXU-Validity Checks

`EncoderDf` overrides exactly three slot writers — the three that reference an MXU — and each follows the same shape: call the Jellyfish encoder verbatim (which writes all the bundle bits), then run `CheckMxuValid<T>` on the slot's `mxu_num`, then update an internal encoder-state flag. No override touches a bundle byte position.

```c
// EncoderDf::EncodeVectorExtendedInstruction  @ 0x1e85e520 (decompiled, representative)
status = EncoderJf::EncodeVectorExtendedInstruction(this, inst, bundle);  // identical bit layout
if (!status.ok()) return status;                              // encoder_df.cc:65
if (!CheckMxuValid<VectorExtendedInstruction>(inst, inst->mxu_num).ok())  // mxu_num < 2 gate
    return status;                                            // encoder_df.cc:66
if (inst->mxu_num == 1 && (this->state[+12] & 0x600000000) == 0)
    this->state[+12] |= 2;                                    // internal MXU-1 tracking flag
return ok;
```

`CheckMxuValid<T>` is a pure legality gate — it never writes the bundle:

```c
// EncoderDf::CheckMxuValid<T>(inst, mxu_num)  @ 0x1e85e5c0 / 0x1e85e420 / (Misc template)
function CheckMxuValid(inst, mxu_num):
    if mxu_num >= 2:
        return InvalidArgument("invalid mxu_num for " + inst.ShortDebugString());  // encoder_df.cc:36
    return ok;
```

The three overrides and the slot whose `mxu_num` they validate:

| Slot encoder (`EncoderDf`) | Address | Validates `mxu_num` of | After calling | Confidence |
|---|---|---|---|---|
| `EncodeVectorExtendedInstruction` | `0x1e85e520` | the vector-extended / matmul / latch op (`inst+0x70`) | `EncoderJf::EncodeVectorExtendedInstruction` @ `0x1e869f00` | CONFIRMED |
| `EncodeVectorResultInstruction` | `0x1e85e380` | the matres / result-FIFO op (`inst+0x4C`) | `EncoderJf::EncodeVectorResultInstruction` @ `0x1e865ae0` | CONFIRMED |
| `EncodeMiscInstruction` | `0x1e85e6c0` | the misc op's MXU operand (e.g. `ClearResultFifoOperands`) | `EncoderJf::EncodeMiscInstruction` @ `0x1e86be80` | CONFIRMED |

These are precisely the three slots that name an MXU. The vector-ALU, scalar, vector-load, and vector-store slots have *no* `EncoderDf` override at all — they are encoded by the inherited Jellyfish writers without any Dragonfish-specific check, because they do not address the matrix unit.

> **QUIRK — the Dragonfish delta is a *validity* delta, not a *layout* delta.** A reimplementer must encode the Dragonfish bundle bytes with the Jellyfish slot map (the bit positions are byte-identical), and additionally reject any matmul / matres / misc op that names `mxu_num >= 2`. The check changes *which programs encode*, not *where bits land*. Skipping the check produces a bit-identical-but-illegal bundle for an out-of-range MXU; adding a layout difference where there is none corrupts every Dragonfish bundle.

The internal `this+12` state update (OR a bit when `mxu_num == 1`) is encoder bookkeeping — it tracks which MXU a bundle has already committed to so a later slot in the same bundle cannot claim a conflicting MXU. It lives in the encoder object, not the bundle word, and is invisible on the wire.

---

## Cross-References

- [Jellyfish 41B Bundle](bundle-jf-41b.md) — the canonical 41-byte slot map, prefill, and packing model Dragonfish reuses verbatim.
- [Codename Matrix](../targets/tpu-version-codename-matrix.md) — `kDragonfish = 1`, its codename string, and the 0+1 shared-encoder pairing.
- [JXC Family](../targets/jxc-family.md) — the single HAL family and `xla::jellyfish::isa` namespace that serve both Jellyfish and Dragonfish.
