import Std.Tactic.BVDecide

/-!
# Bit-level conversion semantics — typed by width, machine-checked

The conversions are dependently width-typed: a narrow→wide cast is a
`BitVec n → BitVec m` with `n` and `m` fixed in the type, so a width mismatch is
a type error rather than a silent truncation. Each identity below is closed by
`bv_decide`.

This module covers the conversions that are *exact bit surgery* (no rounding):
integer width casts, and the bf16 ↔ f32 pair. A bf16 word is exactly the high 16
bits of the f32 word with the same value, so widening is a zero-pad and
narrowing (round-toward-zero) is a drop of the low half — both lossless on the
retained bits. Conversions that require rounding or subnormal normalisation
(f16↔f32, the fp8/fp4 formats, float↔int) belong to the arithmetic layer, where
the rounding core is shared with addition/multiplication.
-/

namespace SassBitsem

/-! ## Integer width casts -/

/-- Zero-extend 16→32. -/
def zext16 (x : BitVec 16) : BitVec 32 := x.setWidth 32
/-- Sign-extend 16→32. -/
def sext16 (x : BitVec 16) : BitVec 32 := x.signExtend 32
/-- Truncate 32→16 (keep the low half). -/
def trunc16 (x : BitVec 32) : BitVec 16 := x.truncate 16

/-- Either extension round-trips through truncation. -/
theorem zext_roundtrip (x : BitVec 16) : trunc16 (zext16 x) = x := by
  unfold trunc16 zext16; bv_decide
theorem sext_roundtrip (x : BitVec 16) : trunc16 (sext16 x) = x := by
  unfold trunc16 sext16; bv_decide

/-- Zero-extension leaves the high half clear. -/
theorem zext_high_zero (x : BitVec 16) : zext16 x &&& (0xFFFF0000 : BitVec 32) = 0 := by
  unfold zext16; bv_decide

/-- Sign-extension replicates bit 15 into the high half: the high half is all
ones exactly when the source is negative. -/
theorem sext_high_fill (x : BitVec 16) :
    (sext16 x &&& (0xFFFF0000 : BitVec 32) = (0xFFFF0000 : BitVec 32))
      ↔ (x &&& (0x8000 : BitVec 16) = (0x8000 : BitVec 16)) := by
  unfold sext16; bv_decide

/-! ## bf16 ↔ f32 -/

/-- f32 → bf16, round toward zero: drop the low 16 bits. -/
def f32ToBf16Rz (x : BitVec 32) : BitVec 16 := (x >>> (16 : BitVec 32)).truncate 16
/-- bf16 → f32: place the bf16 word in the high half, zero the low half. -/
def bf16ToF32 (b : BitVec 16) : BitVec 32 := b ++ (0 : BitVec 16)

/-- Widening then narrowing recovers the bf16 word exactly. -/
theorem bf16_roundtrip (b : BitVec 16) : f32ToBf16Rz (bf16ToF32 b) = b := by
  unfold f32ToBf16Rz bf16ToF32; bv_decide

/-- A widened bf16 has a clear low half. -/
theorem bf16_widen_low_zero (b : BitVec 16) :
    bf16ToF32 b &&& (0xFFFF : BitVec 32) = 0 := by
  unfold bf16ToF32; bv_decide

/-- Narrow-then-widen keeps the high half and clears the low half: bf16 is the
high 16 bits of the f32 word. -/
theorem bf16_is_high_half (x : BitVec 32) :
    bf16ToF32 (f32ToBf16Rz x) = x &&& (0xFFFF0000 : BitVec 32) := by
  unfold bf16ToF32 f32ToBf16Rz; bv_decide

/-- The sign bit survives both directions of the bf16/f32 conversion. -/
theorem bf16_sign_preserved (x : BitVec 32) :
    (f32ToBf16Rz x).getLsbD 15 = x.getLsbD 31 := by
  unfold f32ToBf16Rz; bv_decide

end SassBitsem
