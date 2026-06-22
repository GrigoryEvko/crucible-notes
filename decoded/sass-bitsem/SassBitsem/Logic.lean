import Std.Tactic.BVDecide

/-!
# Bit-level logic / shift / permute semantics — typed and machine-checked

Every operation here is a pure `BitVec` function: input bits to output bits,
with no dependence on any instruction-naming taxonomy. Each `theorem` is closed
by `bv_decide`, which bit-blasts the goal to SAT and checks an LRAT proof, so it
certifies the bit identity for **all** inputs — the same obligation a QF_BV
solver discharges externally, but as a kernel-checked term.
-/

namespace SassBitsem

/-- 3-input bitwise lookup. For each of the 32 bit positions the three source
bits select an index `i = 4·a + 2·b + c` (a is the MSB) and the result bit is
bit `i` of the 8-bit `lut`. Written as the sum (OR) of the eight minterms so it
is a closed bit-vector expression. -/
def lop3 (a b c : BitVec 32) (lut : BitVec 8) : BitVec 32 :=
  (if lut.getLsbD 0 then (~~~a) &&& (~~~b) &&& (~~~c) else 0) |||
  (if lut.getLsbD 1 then (~~~a) &&& (~~~b) &&& c        else 0) |||
  (if lut.getLsbD 2 then (~~~a) &&& b      &&& (~~~c)   else 0) |||
  (if lut.getLsbD 3 then (~~~a) &&& b      &&& c        else 0) |||
  (if lut.getLsbD 4 then a      &&& (~~~b) &&& (~~~c)   else 0) |||
  (if lut.getLsbD 5 then a      &&& (~~~b) &&& c        else 0) |||
  (if lut.getLsbD 6 then a      &&& b      &&& (~~~c)   else 0) |||
  (if lut.getLsbD 7 then a      &&& b      &&& c        else 0)

/-- The three canonical operand selectors that generate every `lut` constant:
`0xF0`→a, `0xCC`→b, `0xAA`→c. Any 3-input boolean is `lut` evaluated on these. -/
theorem lop3_selA (a b c : BitVec 32) : lop3 a b c (0xF0 : BitVec 8) = a := by
  unfold lop3; bv_decide
theorem lop3_selB (a b c : BitVec 32) : lop3 a b c (0xCC : BitVec 8) = b := by
  unfold lop3; bv_decide
theorem lop3_selC (a b c : BitVec 32) : lop3 a b c (0xAA : BitVec 8) = c := by
  unfold lop3; bv_decide

/-- Composite LUTs reduce to the matching boolean expression. -/
theorem lop3_and2 (a b c : BitVec 32) : lop3 a b c (0xC0 : BitVec 8) = a &&& b := by
  unfold lop3; bv_decide
theorem lop3_or2 (a b c : BitVec 32) : lop3 a b c (0xFC : BitVec 8) = a ||| b := by
  unfold lop3; bv_decide
theorem lop3_xor3 (a b c : BitVec 32) :
    lop3 a b c (0x96 : BitVec 8) = a ^^^ b ^^^ c := by
  unfold lop3; bv_decide
theorem lop3_maj3 (a b c : BitVec 32) :
    lop3 a b c (0xE8 : BitVec 8) = (a &&& b) ||| (a &&& c) ||| (b &&& c) := by
  unfold lop3; bv_decide

/-- Right funnel shift, wrap variant: the low 32 bits of `({hi:lo} >> (n mod 32))`
where `{hi:lo}` is the 64-bit value with `hi` in the high half. The shift count
is masked to 5 bits. Source order matches the datapath: low word, high word,
count. -/
def funnelR (lo hi shift : BitVec 32) : BitVec 32 :=
  let n : BitVec 64 := (shift &&& (0x1F : BitVec 32)).setWidth 64
  ((hi ++ lo) >>> n).truncate 32

/-- Left funnel shift, wrap variant: the high 32 bits of `({hi:lo} << (n mod 32))`. -/
def funnelL (lo hi shift : BitVec 32) : BitVec 32 :=
  let n : BitVec 64 := (shift &&& (0x1F : BitVec 32)).setWidth 64
  ((hi ++ lo) <<< n >>> (32 : BitVec 64)).truncate 32

/-- Funnel-right by a concrete count splits cleanly across the word boundary. -/
theorem funnelR_8 (lo hi : BitVec 32) :
    funnelR lo hi (8 : BitVec 32)
      = (lo >>> (8 : BitVec 32)) ||| (hi <<< (24 : BitVec 32)) := by
  unfold funnelR; bv_decide

/-- A zero count is the identity on the low word. -/
theorem funnelR_0 (lo hi : BitVec 32) : funnelR lo hi (0 : BitVec 32) = lo := by
  unfold funnelR; bv_decide

/-- Funnel-left by a concrete count, mirror of `funnelR_8`. -/
theorem funnelL_4 (lo hi : BitVec 32) :
    funnelL lo hi (4 : BitVec 32)
      = (hi <<< (4 : BitVec 32)) ||| (lo >>> (28 : BitVec 32)) := by
  unfold funnelL; bv_decide

/-- Contiguous mask of `width` ones starting at `base`, wrap variant: both base
and width are taken mod 32. -/
def bmskWrap (base width : BitVec 32) : BitVec 32 :=
  let b := base &&& (0x1F : BitVec 32)
  let w := width &&& (0x1F : BitVec 32)
  (((1 : BitVec 32) <<< w) - 1) <<< b

/-- A width-8 mask at base 4 is exactly bits [11:4]. -/
theorem bmsk_8_at_4 : bmskWrap (4 : BitVec 32) (8 : BitVec 32) = (0xFF0 : BitVec 32) := by
  unfold bmskWrap; bv_decide

end SassBitsem
