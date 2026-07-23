import SassBitsem.Logic

/-!
# Inventing instructions: specify a bit-function, realize it on the real ISA

You cannot add an opcode to fixed silicon, but the primitive set is functionally
complete, so any bit-function you invent is realizable as a *composition* of real
instructions — and the realization is checkable. Each "made-up" operation below is
defined by its spec and then proved equal to a sequence of real primitives by
`bv_decide`. The proof IS the guarantee that the invented instruction runs on the
hardware exactly as specified.

The cost is the instruction count: every 3-input bitwise function is exactly one
`lop3` (the LUT is its truth table); wider functions decompose into a `lop3` tree.
-/

namespace SassBitsem

/-! ## Invented op 1 — per-bit multiplex `sel ? a : b`.
No native bitwise-select opcode exists; it is exactly one `lop3`. -/

/-- Spec of the invented MUX. -/
def mux (a b sel : BitVec 32) : BitVec 32 := (a &&& sel) ||| (b &&& (~~~sel))

/-- One real `lop3` realizes it (LUT `0xE4` is the truth table of `sel ? a : b`). -/
theorem mux_is_one_lop3 (a b sel : BitVec 32) :
    mux a b sel = lop3 a b sel (0xE4 : BitVec 8) := by
  unfold mux lop3; bv_decide

/-! ## Invented op 2 — 32-bit rotate-right.
SASS has no rotate opcode; it is one funnel shift with both halves equal. -/

/-- Spec of the invented rotate (concrete distance 8). -/
def ror8 (x : BitVec 32) : BitVec 32 := (x >>> (8 : BitVec 32)) ||| (x <<< (24 : BitVec 32))

/-- One real funnel shift realizes it. -/
theorem ror8_is_one_funnel (x : BitVec 32) : ror8 x = funnelR x x (8 : BitVec 32) := by
  unfold ror8 funnelR; bv_decide

/-! ## Invented op 3 — 4-input fused `(a∧b) ∨ (c∧d)`.
Four inputs exceed one LUT, so it costs two real `lop3`s. -/

/-- Spec of the invented 4-input fuse. -/
def fuse4 (a b c d : BitVec 32) : BitVec 32 := (a &&& b) ||| (c &&& d)

/-- Two real `lop3`s realize it: an inner `a∧b` (LUT `0xC0`), then `t ∨ (c∧d)`
(LUT `0xF8`). The cost is the synthesis answer — wider invented ops are deeper
trees, never new silicon. -/
theorem fuse4_is_two_lop3 (a b c d : BitVec 32) :
    fuse4 a b c d
      = lop3 (lop3 a b (0 : BitVec 32) (0xC0 : BitVec 8)) c d (0xF8 : BitVec 8) := by
  unfold fuse4 lop3; bv_decide

end SassBitsem
