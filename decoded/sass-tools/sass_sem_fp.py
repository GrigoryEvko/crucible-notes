#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling (MIT-style).
# Built on the public CUDA tools (ptxas/nvdisasm/libcuda) + live-GPU measurement.
"""
Bit-exact IEEE-754 functional model for the floating-point SASS instruction set.

This is the FP companion to ``sass_legality.py`` (the integer/bit-logic checker).
Every operation here is a **closed-form** soft-float written on Python big integers
-- the mantissa/exponent algebra is done in exact integer arithmetic and only the
final round-pack collapses it to the target width, so there is no host-FP rounding
hiding in the model.  Nothing is enumerated from a table.

Scope (each with all of its modifiers):

  fp32 ALU      FADD / FMUL / FFMA           .RN .RZ .RM .RP  + .FTZ + .SAT
  fp32 compare  FSETP                        all cmp x {.AND .OR .XOR} + .FTZ
                FMNMX (min/max, NaN + +-0)
  fp16 packed   HADD2 / HMUL2 / HFMA2        (add.f16 -> HADD2 splat; HFMA2 imm)
  bf16          packed bf16 add/mul/fma
  fp64          DADD / DMUL / DFMA           .RN .RZ .RM .RP
  narrow        e4m3 / e5m2 (fp8), e2m1 (fp4) <-> f32/f16, satfinite
  conversions   F2F  (f16<->f32<->f64 widen/narrow, round)
                F2I  (round + clamp, signed/unsigned, .NTZ tininess corner)
                I2F  (round)
                I2I  (resize / saturate)

The model that matters is the **GPU's**: NVIDIA hardware canonicalises NaNs and
applies flush-to-zero in ways an off-the-shelf IEEE soft-float does not, so every
op is differentially checked against SASS executed on the live device (see
``--gpu`` / ``run_gpu_harness``).  Where the silicon and a plain IEEE model
disagree (NaN payload canonicalisation, FTZ corners) the model follows the GPU and
the divergence is documented at the op.

The MUFU transcendental seeds (RCP/RSQ/SQRT/SIN/COS/EX2/LG2/TANH) are **hardware
ROM approximations** -- their coefficients are not recoverable and are not modelled
here.  What *is* modelled and verified is the full-precision Newton-Raphson /
Heron correction sequence ptxas emits *around* MUFU for ``div``/``sqrt``/``rcp.rn``
(``mufu_div_rn_f32`` etc.): those live in the codegen templates, not the silicon.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Rounding modes (the four the FP ALU and conversions expose).                #
# --------------------------------------------------------------------------- #
RN = "RN"   # round to nearest, ties to even   (PTX .rn, SASS default)
RZ = "RZ"   # round toward zero (truncate)      (PTX .rz, SASS .RZ)
RM = "RM"   # round toward -inf (floor)         (PTX .rm, SASS .RM)
RP = "RP"   # round toward +inf (ceil)          (PTX .rp, SASS .RP)
ROUNDING_MODES = (RN, RZ, RM, RP)


# --------------------------------------------------------------------------- #
# Format descriptor: a width-parametric IEEE-754 binary format.               #
#                                                                             #
# (exp_bits, man_bits, bias, has_inf).  fp8 e4m3 has NO infinity -- its all-1 #
# exponent + all-1 mantissa is the single largest finite (448), and the only  #
# NaN is the S.1111.111 pattern.  e5m2 and fp4 e2m1 follow IEEE inf/NaN rules  #
# (e2m1 has no exponent room for inf/NaN at all).                             #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FpFormat:
    name: str
    exp_bits: int
    man_bits: int
    has_inf: bool = True        # e4m3 = False (no Inf; max-exp is finite)
    has_nan: bool = True        # e2m1 = False (no Inf/NaN room)
    # canonical (default) quiet-NaN payload bit pattern WITHOUT the sign, as the
    # raw bit field; NVIDIA hardware generates these on invalid ops.
    qnan_default: int = 0

    @property
    def width(self) -> int:
        return 1 + self.exp_bits + self.man_bits

    @property
    def bias(self) -> int:
        return (1 << (self.exp_bits - 1)) - 1

    @property
    def exp_mask(self) -> int:
        return (1 << self.exp_bits) - 1

    @property
    def man_mask(self) -> int:
        return (1 << self.man_bits) - 1

    @property
    def sign_shift(self) -> int:
        return self.exp_bits + self.man_bits

    @property
    def max_exp_field(self) -> int:
        """The all-ones exponent field value (Inf/NaN exponent on IEEE formats)."""
        return self.exp_mask

    @property
    def has_special_exp(self) -> bool:
        """True when the all-ones exponent field is reserved for Inf/NaN (IEEE
        formats and e5m2).  False for e4m3 (top exp is finite, only the all-ones
        *mantissa* there is NaN) and e2m1 (no specials at all)."""
        return self.has_inf

    @property
    def max_normal_exp_field(self) -> int:
        """Highest exponent field a finite normal can use."""
        return self.max_exp_field - 1 if self.has_special_exp else self.max_exp_field

    @property
    def max_finite_man(self) -> int:
        """Largest mantissa field at ``max_normal_exp_field`` (e4m3 reserves the
        all-ones mantissa there for its single NaN)."""
        return self.man_mask - 1 if self.name == "e4m3" else self.man_mask


# The standard IEEE binary formats plus the OCP/NVIDIA narrow ones.  qnan_default
# is the NVIDIA canonical quiet-NaN (verified against the device): the sign is
# negative and only the MSB of the mantissa is set (defaultNaN convention).
F16 = FpFormat("f16", 5, 10, qnan_default=(0x1F << 10) | (1 << 9))     # 0x7E00 (abs)
BF16 = FpFormat("bf16", 8, 7, qnan_default=(0xFF << 7) | (1 << 6))     # 0x7FC0 (abs)
F32 = FpFormat("f32", 8, 23, qnan_default=(0xFF << 23) | (1 << 22))    # 0x7FC00000 (abs)
F64 = FpFormat("f64", 11, 52, qnan_default=(0x7FF << 52) | (1 << 51))  # 0x7FF8.. (abs)
# OCP fp8 / fp4.
E4M3 = FpFormat("e4m3", 4, 3, has_inf=False, has_nan=True, qnan_default=0x7)   # S.1111.111 NaN
E5M2 = FpFormat("e5m2", 5, 2, has_inf=True, has_nan=True, qnan_default=0x2)    # IEEE-like
E2M1 = FpFormat("e2m1", 2, 1, has_inf=False, has_nan=False)                    # fp4, finite-only


# --------------------------------------------------------------------------- #
# Raw-bits <-> Python float helpers (for I/O and the differential harness).   #
# These touch host FP ONLY to move bits in/out -- never to do arithmetic.     #
# --------------------------------------------------------------------------- #
def f32_to_bits(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


def bits_to_f32(b: int) -> float:
    return struct.unpack("<f", struct.pack("<I", b & 0xFFFFFFFF))[0]


def f64_to_bits(x: float) -> int:
    return struct.unpack("<Q", struct.pack("<d", x))[0]


def bits_to_f64(b: int) -> float:
    return struct.unpack("<d", struct.pack("<Q", b & 0xFFFFFFFFFFFFFFFF))[0]


# --------------------------------------------------------------------------- #
# Decode a raw bit field of a format into (sign, exp_field, man_field) and a   #
# semantic classification.                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class FpVal:
    """A decoded floating-point value as exact integers.

    ``kind`` is one of: 'zero', 'subnormal', 'normal', 'inf', 'nan'.
    For finite values the represented number is  (-1)^sign * man * 2^exp_unbiased
    where ``man`` already includes the implicit leading bit for normals; ``exp``
    is the unbiased power applied to ``man`` treated as an integer significand
    (i.e. the value is man * 2**exp exactly).
    """
    fmt: FpFormat
    sign: int
    exp_field: int
    man_field: int
    kind: str

    @property
    def is_nan(self) -> bool:
        return self.kind == "nan"

    @property
    def is_inf(self) -> bool:
        return self.kind == "inf"

    @property
    def is_zero(self) -> bool:
        return self.kind == "zero"

    @property
    def is_signaling_nan(self) -> bool:
        """sNaN = NaN with the mantissa MSB clear (and some payload set)."""
        if self.kind != "nan":
            return False
        msb = 1 << (self.fmt.man_bits - 1)
        return (self.man_field & msb) == 0 and (self.man_field & (msb - 1)) != 0


def decode(fmt: FpFormat, bits: int) -> FpVal:
    bits &= (1 << fmt.width) - 1
    sign = (bits >> fmt.sign_shift) & 1
    exp = (bits >> fmt.man_bits) & fmt.exp_mask
    man = bits & fmt.man_mask
    if exp == fmt.max_exp_field and (fmt.has_inf or fmt.has_nan):
        if fmt.name == "e4m3":
            # e4m3: the all-ones-exp / all-ones-man pattern is the ONLY NaN; the
            # rest of the all-ones-exp space is finite (no inf at all).
            kind = "nan" if man == fmt.man_mask else "normal"
        elif not fmt.has_inf:
            kind = "nan" if man != 0 else "normal"
        else:
            kind = "inf" if man == 0 else "nan"
    elif exp == 0:
        kind = "zero" if man == 0 else "subnormal"
    else:
        kind = "normal"
    return FpVal(fmt, sign, exp, man, kind)


def encode(fmt: FpFormat, sign: int, exp_field: int, man_field: int) -> int:
    return (((sign & 1) << fmt.sign_shift)
            | ((exp_field & fmt.exp_mask) << fmt.man_bits)
            | (man_field & fmt.man_mask))


# --------------------------------------------------------------------------- #
# Exact (significand, exponent) extraction.  Returns (sign, sig, e) such that  #
# value == (-1)^sign * sig * 2**e  exactly, with sig an integer.  Specials     #
# return sig=None and a tag.  For FTZ, denormal inputs are flushed to +-0.     #
# --------------------------------------------------------------------------- #
def to_exact(v: FpVal, ftz: bool) -> tuple[int, int | None, int, str]:
    """(sign, sig, exp, tag) where tag in {'finite','inf','nan','zero'}."""
    fmt = v.fmt
    if v.kind == "nan":
        return v.sign, None, 0, "nan"
    if v.kind == "inf":
        return v.sign, None, 0, "inf"
    if v.kind == "zero":
        return v.sign, 0, 0, "zero"
    if v.kind == "subnormal":
        if ftz:
            return v.sign, 0, 0, "zero"
        # subnormal: value = man * 2^(1 - bias - man_bits)
        return v.sign, v.man_field, 1 - fmt.bias - fmt.man_bits, "finite"
    # normal: value = (man | implicit) * 2^(exp - bias - man_bits)
    sig = v.man_field | (1 << fmt.man_bits)
    return v.sign, sig, v.exp_field - fmt.bias - fmt.man_bits, "finite"


# --------------------------------------------------------------------------- #
# The single round-pack core.  Given an exact nonzero magnitude  sig * 2**exp  #
# (sig a positive integer, any width) plus a sign, round it into ``fmt`` under  #
# ``mode``, applying FTZ to the *result* if requested.  Returns raw bits.      #
#                                                                             #
# This is the closed-form heart of the model: align the significand to the     #
# format's precision, compute the round increment from the discarded bits and  #
# the rounding mode, handle subnormal/overflow, and pack.                       #
# --------------------------------------------------------------------------- #
def round_pack(fmt: FpFormat, sign: int, sig: int, exp: int, mode: str,
               ftz_out: bool, satfinite: bool = False) -> int:
    """Round (-1)^sign * sig * 2**exp into ``fmt``. ``sig`` > 0 (integer).

    ``satfinite`` (the PTX ``cvt.satfinite`` flag the narrow conversions carry):
    an overflow clamps to the format's largest finite instead of becoming Inf,
    on every rounding mode.
    """
    if sig == 0:
        return encode(fmt, sign, 0, 0)

    P = fmt.man_bits + 1            # target precision (with implicit bit)

    # Normalise so the significand has exactly P+G bits with a guard window: we
    # keep all bits and let the shift below decide the round position.  Compute
    # the position of the MSB.
    msb = sig.bit_length() - 1      # index of top set bit
    # The unbiased exponent of the value (as 1.xxx * 2^E) is exp + msb.
    E = exp + msb

    # Smallest normal exponent is (1 - bias); below that we are subnormal and the
    # exponent is pinned to (1 - bias) with the significand shifted further right.
    e_min = 1 - fmt.bias

    # We want the rounded significand to occupy P bits (implicit + man_bits) for a
    # normal, fewer for a subnormal.  Determine how far to shift ``sig`` right so
    # the unit-in-the-last-place lands at bit 0, keeping guard+round+sticky.
    if E < e_min:
        # subnormal target: result exponent field is 0, value = msig * 2^(e_min - man_bits)
        # number of significand bits available shrinks; shift so ulp == 2^(e_min - man_bits)
        target_low_exp = e_min - fmt.man_bits
    else:
        target_low_exp = E - fmt.man_bits

    shift = target_low_exp - exp    # how many low bits of sig to discard

    if shift <= 0:
        # No bits discarded: exact (may need left shift to fill).  Round bits = 0.
        rounded = sig << (-shift)
        round_bits = 0
        sticky = 0
        half = 0
    else:
        rounded = sig >> shift
        discarded = sig & ((1 << shift) - 1)
        half = 1 << (shift - 1)
        round_bits = discarded
        sticky = 1 if (discarded & (half - 1)) else 0

    # Round increment per mode.
    inc = 0
    if shift > 0 and round_bits:
        if mode == RN:
            if round_bits > half:
                inc = 1
            elif round_bits == half:
                inc = 1 if (rounded & 1) else 0          # ties to even
            else:
                inc = 0
        elif mode == RZ:
            inc = 0
        elif mode == RM:
            inc = 1 if sign else 0                        # toward -inf
        elif mode == RP:
            inc = 1 if not sign else 0                    # toward +inf
    rounded += inc

    # Re-normalise after the increment (a carry can push us up one exponent).
    if E < e_min:
        # subnormal path: the result mantissa is ``rounded``; check if rounding
        # carried it up into the smallest normal (rounded == 1<<man_bits).
        if rounded >> fmt.man_bits:
            # carried into normal range: exponent field 1, mantissa = rounded & mask
            return encode(fmt, sign, 1, rounded & fmt.man_mask)
        out = encode(fmt, sign, 0, rounded)
        if ftz_out:
            return encode(fmt, sign, 0, 0)               # flush subnormal result
        return out

    # normal path: ``rounded`` should be P bits (implicit 1 + man_bits).  A carry
    # out of bit P bumps the exponent.
    exp_field = E + fmt.bias
    if rounded >> P:                       # carry into a new MSB
        rounded >>= 1
        exp_field += 1
    man_field = rounded & fmt.man_mask

    # Overflow: the rounded value exceeds the largest finite this format can
    # represent (exp beyond the max-normal field, or at it with too-big mantissa
    # for the e4m3 NaN-reserving case).
    overflowed = (exp_field > fmt.max_normal_exp_field
                  or (exp_field == fmt.max_normal_exp_field
                      and man_field > fmt.max_finite_man))
    if overflowed:
        if satfinite or not fmt.has_inf:
            return _max_finite_bits(fmt, sign)            # clamp to max finite
        if _overflow_to_inf(mode, sign):
            return encode(fmt, sign, fmt.max_exp_field, 0)        # +-Inf
        return _max_finite_bits(fmt, sign)                # RZ / wrong-direction
    return encode(fmt, sign, exp_field, man_field)


def _overflow_to_inf(mode: str, sign: int) -> bool:
    """RN/near always -> Inf; RZ -> max finite; RM -> -Inf only if negative,
    else max finite; RP -> +Inf only if positive, else max finite."""
    if mode == RN:
        return True
    if mode == RZ:
        return False
    if mode == RM:
        return bool(sign)
    if mode == RP:
        return not sign
    return True


def _max_finite_bits(fmt: FpFormat, sign: int) -> int:
    if fmt.name == "e4m3":
        # e4m3 max finite = S.1111.110 (the .111 mantissa is NaN).
        return encode(fmt, sign, fmt.max_exp_field, fmt.man_mask - 1)
    if not fmt.has_inf and not fmt.has_nan:
        # finite-only (e2m1, fp4): max finite is all-ones exp + all-ones man.
        return encode(fmt, sign, fmt.max_exp_field, fmt.man_mask)
    # IEEE format: largest finite is (max_exp - 1, all-ones mantissa).
    return encode(fmt, sign, fmt.max_exp_field - 1, fmt.man_mask)


# --------------------------------------------------------------------------- #
# NaN handling, NVIDIA-canonical.  Measured on sm_89; the rule is NOT the       #
# textbook IEEE soft-float one and is width-dependent:                          #
#                                                                             #
#   * f32 / f16-packed arithmetic (FADD/FMUL/FFMA, HADD2/HMUL2/HFMA2):          #
#     EVERY NaN result -- whether propagated from a NaN input or generated by   #
#     an invalid op (inf-inf, 0*inf) -- is the all-ones canonical NaN           #
#     (0x7FFFFFFF for f32, 0x7FFF for f16).  Input payload/sign are discarded.  #
#                                                                             #
#   * f64 arithmetic (DADD/DMUL/DFMA):                                          #
#       - PROPAGATED (a NaN operand was present): quiet it (set mantissa MSB)   #
#         and keep its payload+sign.  The operand preference, measured on the    #
#         device, is the LAST NaN operand in source order -- i.e. scan c, then  #
#         b, then a, and take the first NaN found (for FADD/DMUL: b before a).  #
#         (Equivalently the classic ``isNaN(A)?A:B`` IEEE preference, with       #
#         NVIDIA's a/b operands mapped to the reference model's b/a.)            #
#       - GENERATED (invalid op, no NaN input): the fixed negative default NaN  #
#         0xFFF8000000000000.                                                   #
#                                                                             #
# These constants were read straight off the device, not assumed.              #
# --------------------------------------------------------------------------- #
# arith_canon[fmt.name] = (canonical NaN bits used for f32/f16-class arithmetic
# AND the generated-NaN bits for f64).
_ARITH_CANON_NAN = {
    "f16":  0x7FFF,
    "bf16": 0x7FFF,                 # bf16 packed follows the f16-class all-ones rule
    "f32":  0x7FFFFFFF,
    "f64":  0xFFF8000000000000,     # f64 GENERATED NaN is the negative default
}


def canonical_qnan(fmt: FpFormat) -> int:
    """Legacy helper: the format's quiet-NaN encoding (sign 0, MSB-only payload).
    Used only where a plain default qNaN is wanted (e.g. F2F destination)."""
    return encode(fmt, 0, fmt.max_exp_field, fmt.qnan_default)


def arith_nan(fmt: FpFormat, *operand_bits: int) -> int:
    """The NaN an FP *arithmetic* op (add/mul/fma/min/max) returns, matching the
    measured device rule above.

    ``operand_bits`` are the raw inputs in operand order; if any is a NaN the op
    is a PROPAGATION, otherwise the NaN was GENERATED by an invalid combination.
    """
    if fmt.width <= 32:
        # f16 / bf16 / f32: all-ones canonical, payload destroyed.
        return _ARITH_CANON_NAN[fmt.name]
    # f64: propagate (quiet + keep payload/sign of the LAST NaN operand in source
    # order -- scan c,b,a) else the fixed negative default NaN.
    for ob in reversed(operand_bits):
        v = decode(fmt, ob)
        if v.is_nan:
            msb = 1 << (fmt.man_bits - 1)
            return encode(fmt, v.sign, fmt.max_exp_field, v.man_field | msb)
    return _ARITH_CANON_NAN["f64"]


# --------------------------------------------------------------------------- #
# fp32/fp64/generic-format ADD, MUL, FMA (closed-form, exact-then-round).      #
# --------------------------------------------------------------------------- #
def fp_add(fmt: FpFormat, a_bits: int, b_bits: int, mode: str = RN,
           ftz: bool = False, sat: bool = False, sub: bool = False) -> int:
    """(-1)?a + b  rounded into ``fmt``.  ``sub`` negates b first (FADD subtract).

    FTZ flushes denormal inputs AND a denormal result to zero.  SAT clamps the
    final result to [0,1] (NVIDIA add.sat semantics: NaN->0, clamp to [0,1]).
    """
    va, vb = decode(fmt, a_bits), decode(fmt, b_bits)
    if sub:
        vb = FpVal(fmt, vb.sign ^ 1, vb.exp_field, vb.man_field, vb.kind)

    # NaN propagation (device rule -- see arith_nan).
    if va.is_nan or vb.is_nan:
        return _sat(fmt, arith_nan(fmt, a_bits, b_bits), sat)
    # Inf handling.
    if va.is_inf or vb.is_inf:
        if va.is_inf and vb.is_inf and (va.sign != vb.sign):
            return _sat(fmt, arith_nan(fmt), sat)            # inf - inf = NaN
        s = va.sign if va.is_inf else vb.sign
        return _sat(fmt, encode(fmt, s, fmt.max_exp_field, 0), sat)

    sa, siga, ea, _ = to_exact(va, ftz)
    sb, sigb, eb, _ = to_exact(vb, ftz)
    out = _add_exact(fmt, sa, siga, ea, sb, sigb, eb, mode, ftz)
    return _sat(fmt, out, sat)


def _add_exact(fmt: FpFormat, sa: int, siga: int, ea: int,
               sb: int, sigb: int, eb: int, mode: str, ftz: bool) -> int:
    """Add two exact finite values (sign,sig,exp) and round into fmt."""
    if siga == 0 and sigb == 0:
        # both zero: sign rule -- +0 unless both -0, or RM where x+(-x)=-0.
        if sa == sb:
            return encode(fmt, sa, 0, 0)
        return encode(fmt, 1 if mode == RM else 0, 0, 0)
    if siga == 0:
        return round_pack(fmt, sb, sigb, eb, mode, ftz)
    if sigb == 0:
        return round_pack(fmt, sa, siga, ea, mode, ftz)

    # Align to the lower exponent.
    if ea < eb:
        sigb <<= (eb - ea)
        e = ea
    else:
        siga <<= (ea - eb)
        e = eb
    if sa == sb:
        sig = siga + sigb
        sign = sa
    else:
        if siga >= sigb:
            sig = siga - sigb
            sign = sa
        else:
            sig = sigb - siga
            sign = sb
        if sig == 0:
            # exact cancellation -> +0 (or -0 under RM).
            return encode(fmt, 1 if mode == RM else 0, 0, 0)
    return round_pack(fmt, sign, sig, e, mode, ftz)


def fp_mul(fmt: FpFormat, a_bits: int, b_bits: int, mode: str = RN,
           ftz: bool = False, sat: bool = False) -> int:
    va, vb = decode(fmt, a_bits), decode(fmt, b_bits)
    if va.is_nan or vb.is_nan:
        return _sat(fmt, arith_nan(fmt, a_bits, b_bits), sat)
    sign = va.sign ^ vb.sign
    # inf * 0 = NaN; inf * finite = inf.
    if va.is_inf or vb.is_inf:
        za = va.is_zero or (ftz and va.kind == "subnormal")
        zb = vb.is_zero or (ftz and vb.kind == "subnormal")
        if (va.is_inf and zb) or (vb.is_inf and za):
            return _sat(fmt, arith_nan(fmt), sat)            # inf * 0 (generated)
        return _sat(fmt, encode(fmt, sign, fmt.max_exp_field, 0), sat)
    sa, siga, ea, _ = to_exact(va, ftz)
    sb, sigb, eb, _ = to_exact(vb, ftz)
    if siga == 0 or sigb == 0:
        return _sat(fmt, encode(fmt, sign, 0, 0), sat)
    out = round_pack(fmt, sign, siga * sigb, ea + eb, mode, ftz)
    return _sat(fmt, out, sat)


def fp_fma(fmt: FpFormat, a_bits: int, b_bits: int, c_bits: int, mode: str = RN,
           ftz: bool = False, sat: bool = False,
           neg_ab: bool = False, neg_c: bool = False) -> int:
    """Fused (a*b) + c with a SINGLE rounding.  neg_ab / neg_c apply the SASS
    operand negates (FFMA -R, ...)."""
    va, vb, vc = decode(fmt, a_bits), decode(fmt, b_bits), decode(fmt, c_bits)
    if neg_c:
        vc = FpVal(fmt, vc.sign ^ 1, vc.exp_field, vc.man_field, vc.kind)
    # NaN in any operand -> propagate (operand order a,b,c).
    if va.is_nan or vb.is_nan or vc.is_nan:
        return _sat(fmt, arith_nan(fmt, a_bits, b_bits, c_bits), sat)
    sab = va.sign ^ vb.sign ^ (1 if neg_ab else 0)
    # product specials.
    za = va.is_zero or (ftz and va.kind == "subnormal")
    zb = vb.is_zero or (ftz and vb.kind == "subnormal")
    if va.is_inf or vb.is_inf:
        if (va.is_inf and zb) or (vb.is_inf and za):
            return _sat(fmt, arith_nan(fmt), sat)           # inf*0 (generated)
        # product is +-inf; add c.
        if vc.is_inf and vc.sign != sab:
            return _sat(fmt, arith_nan(fmt), sat)           # inf - inf (generated)
        return _sat(fmt, encode(fmt, sab, fmt.max_exp_field, 0), sat)
    if vc.is_inf:
        return _sat(fmt, encode(fmt, vc.sign, fmt.max_exp_field, 0), sat)

    sa, siga, ea, _ = to_exact(va, ftz)
    sb, sigb, eb, _ = to_exact(vb, ftz)
    sc, sigc, ec, _ = to_exact(vc, ftz)
    # exact product
    if siga == 0 or sigb == 0:
        psig, pe, psign = 0, 0, sab
    else:
        psig, pe, psign = siga * sigb, ea + eb, sab
    out = _add_exact(fmt, psign, psig, pe, sc, sigc, ec, mode, ftz)
    return _sat(fmt, out, sat)


def _sat(fmt: FpFormat, bits: int, sat: bool) -> int:
    """add.sat/mul.sat/fma.sat: clamp the IEEE result to [0.0, 1.0]; NaN -> 0."""
    if not sat:
        return bits
    v = decode(fmt, bits)
    if v.is_nan:
        return encode(fmt, 0, 0, 0)                # NaN -> +0
    if v.sign:
        return encode(fmt, 0, 0, 0)                # any negative incl. -0 -> +0
    one = _one_bits(fmt)
    # clamp the upper edge: anything > 1.0 (incl. +inf) saturates to 1.0.
    if v.is_inf:
        return one
    if fp_compare(fmt, bits, one, ftz=False) == "gt":
        return one
    return bits


def _one_bits(fmt: FpFormat) -> int:
    return encode(fmt, 0, fmt.bias, 0)              # 1.0


# --------------------------------------------------------------------------- #
# Comparison (FSETP / FMNMX core).  Returns 'lt' | 'eq' | 'gt' | 'un'          #
# (unordered = at least one NaN).  ``ftz`` flushes denormal operands first.    #
# --------------------------------------------------------------------------- #
def fp_compare(fmt: FpFormat, a_bits: int, b_bits: int, ftz: bool) -> str:
    """Total order of two non-NaN values: 'lt' | 'eq' | 'gt' (or 'un' if NaN).

    Values are exact integers ``sig * 2**exp``; comparison is done by reducing
    each to a *signed exact integer* on a common exponent, so it is precise at
    arbitrary width with no host-FP involved.  +-0 compare equal; +-Inf are
    handled by an explicit ordinal so they sit above every finite magnitude.
    """
    va, vb = decode(fmt, a_bits), decode(fmt, b_bits)
    if va.is_nan or vb.is_nan:
        return "un"
    xa = _signed_real(va, ftz)
    xb = _signed_real(vb, ftz)
    if xa < xb:
        return "lt"
    if xa > xb:
        return "gt"
    return "eq"


def _signed_real(v: FpVal, ftz: bool):
    """Return a comparable signed exact value for a non-NaN ``FpVal``.

    Finite values become a Python ``int`` equal to ``signed_sig * 2**(exp - E)``
    referenced to a common floor exponent so any two finite values are directly
    comparable as integers.  +-Inf return +-math.inf (Python orders inf vs int
    correctly, and we never mix inf with a NaN here).
    """
    s, sig, e, tag = to_exact(v, ftz)
    if tag == "inf":
        return math.inf if s == 0 else -math.inf
    if sig == 0:
        return 0
    # Reference all finite magnitudes to a fixed very-negative floor exponent so
    # sig*2^e becomes an exact integer.  The floor is below any format's min exp.
    FLOOR = -4096
    mag = sig << (e - FLOOR) if e >= FLOOR else sig >> (FLOOR - e)
    return -mag if s else mag


# Map a PTX/SASS compare mnemonic to a predicate over {lt,eq,gt,un}.
_CMP_TABLE = {
    # ordered (false if unordered)
    "lt":  lambda r: r == "lt",
    "le":  lambda r: r in ("lt", "eq"),
    "gt":  lambda r: r == "gt",
    "ge":  lambda r: r in ("gt", "eq"),
    "eq":  lambda r: r == "eq",
    "ne":  lambda r: r in ("lt", "gt"),        # ordered not-equal (false if un)
    "num": lambda r: r != "un",                # both numbers (not NaN)
    # unordered (true if unordered)
    "ltu": lambda r: r == "lt" or r == "un",
    "leu": lambda r: r in ("lt", "eq") or r == "un",
    "gtu": lambda r: r == "gt" or r == "un",
    "geu": lambda r: r in ("gt", "eq") or r == "un",
    "equ": lambda r: r == "eq" or r == "un",
    "neu": lambda r: r in ("lt", "gt") or r == "un",   # neu = not equal OR unordered
    "nan": lambda r: r == "un",                # at least one NaN
}


def fsetp(fmt: FpFormat, a_bits: int, b_bits: int, cmp: str,
          ftz: bool = False, p_in: bool = True, combine: str = "and") -> bool:
    """FSETP: compare a,b under ``cmp`` (lt/leu/equ/...) and combine with the
    incoming predicate ``p_in`` via ``combine`` in {and, or, xor}.

    The default ``p_in=True`` + ``combine='and'`` reproduces a bare
    ``setp.<cmp>.f32`` (PT AND result = result).
    """
    r = fp_compare(fmt, a_bits, b_bits, ftz)
    if cmp not in _CMP_TABLE:
        raise ValueError(f"unknown FP compare {cmp!r}")
    res = _CMP_TABLE[cmp](r)
    if combine == "and":
        return res and p_in
    if combine == "or":
        return res or p_in
    if combine == "xor":
        return res != p_in
    raise ValueError(f"unknown combine {combine!r}")


def fmnmx(fmt: FpFormat, a_bits: int, b_bits: int, want_max: bool,
          ftz: bool = False) -> int:
    """FMNMX min/max.  NVIDIA rule (measured -- IEEE 754-2008 minNum/maxNum
    flavour):
      * if exactly one operand is NaN, return the non-NaN one (NaN is "ignored");
      * if both NaN, return the format's canonical NaN -- 0x7FFFFFFF for f32 /
        0x7FFF for f16 (all-ones), 0x7FF8000000000000 (positive default) for f64;
      * for +-0, min returns -0 and max returns +0 (sign-aware).
    The SASS form is ``FMNMX Rd, a, b, P`` where the predicate P selects max
    (P true) or min (P false); here ``want_max`` is that predicate.
    """
    va, vb = decode(fmt, a_bits), decode(fmt, b_bits)
    if va.is_nan and vb.is_nan:
        # f32/f16: all-ones canonical; f64: positive default NaN.
        if fmt.width <= 32:
            return _ARITH_CANON_NAN[fmt.name]
        return canonical_qnan(fmt)
    if va.is_nan:
        return _ftz_bits(fmt, b_bits, ftz)
    if vb.is_nan:
        return _ftz_bits(fmt, a_bits, ftz)
    r = fp_compare(fmt, a_bits, b_bits, ftz)
    if r == "eq":
        # equal magnitude incl. +-0: pick by sign so min->-0, max->+0.
        # For +-0: -0 < +0 conceptually; honour the sign bits.
        if va.is_zero and vb.is_zero and va.sign != vb.sign:
            neg = a_bits if va.sign else b_bits
            pos = a_bits if not va.sign else b_bits
            return _ftz_bits(fmt, pos if want_max else neg, ftz)
        return _ftz_bits(fmt, a_bits, ftz)
    take_a = (r == "gt") if want_max else (r == "lt")
    return _ftz_bits(fmt, a_bits if take_a else b_bits, ftz)


def _ftz_bits(fmt: FpFormat, bits: int, ftz: bool) -> int:
    """Flush a denormal value to a same-sign zero if ftz is set."""
    if not ftz:
        return bits
    v = decode(fmt, bits)
    if v.kind == "subnormal":
        return encode(fmt, v.sign, 0, 0)
    return bits


# --------------------------------------------------------------------------- #
# Conversions.                                                                 #
# --------------------------------------------------------------------------- #
def f2f(src: FpFormat, dst: FpFormat, bits: int, mode: str = RN,
        ftz: bool = False, satfinite: bool = False) -> int:
    """F2F / F2FP: float (src) -> float (dst), widen or narrow, rounded ``mode``.

    Widening (f16->f32, f32->f64) is exact; narrowing rounds.  NaN follows the
    measured conversion-unit rule (``_f2f_nan``).  ``satfinite`` is the PTX
    ``cvt.satfinite`` flag the narrow forms carry: overflow clamps to the
    destination's largest finite rather than producing Inf -- and, for a
    no-Inf destination (e4m3), an Inf input also clamps to max finite.
    """
    v = decode(src, bits)
    if v.is_nan:
        if satfinite and not dst.has_nan:
            return _max_finite_bits(dst, v.sign)      # e2m1: no NaN, clamp
        return _f2f_nan(src, dst, v)
    if v.is_inf:
        if dst.has_inf and not satfinite:
            return encode(dst, v.sign, dst.max_exp_field, 0)
        return _max_finite_bits(dst, v.sign)          # satfinite / no-Inf dst
    s, sig, e, _ = to_exact(v, ftz)
    if sig == 0:
        return encode(dst, s, 0, 0)
    return round_pack(dst, s, sig, e, mode, ftz, satfinite=satfinite)


def _f2f_nan(src: FpFormat, dst: FpFormat, v: FpVal) -> int:
    """NaN conversion, matching the measured device rule -- which depends on
    *which conversion unit* the lowering uses:

      * f16 on either side goes through the packed-convert unit (``F2FP``):
        the result is the destination's ALL-ONES canonical NaN (0x7FFF / 0x7FFFFFFF),
        with the input payload and sign discarded.
      * f64<->f32 goes through the wide-convert unit (``F2F``): the result is the
        input NaN *quieted* (destination mantissa MSB set), sign preserved, and
        the payload re-scaled to the destination width -- left-shift on a widen,
        right-shift (truncate) on a narrow.
    """
    packed = {"f16", "bf16", "e4m3", "e5m2", "e2m1"}
    if src.name in packed or dst.name in packed:
        # Packed-convert unit (F2FP): the destination's ALL-ONES canonical NaN,
        # payload and sign discarded.  e4m3's NaN happens to be all-ones too.
        if not dst.has_nan:                # e2m1 has no NaN -> max finite
            return _max_finite_bits(dst, 0)
        return _ARITH_CANON_NAN.get(dst.name,
                                    encode(dst, 0, dst.max_exp_field, dst.man_mask))
    # F2F wide path (f64<->f32): quiet + payload re-scale + sign-preserve.
    msb_dst = 1 << (dst.man_bits - 1)
    shift = dst.man_bits - src.man_bits            # >0 widen, <0 narrow
    if shift >= 0:
        payload = (v.man_field << shift) & dst.man_mask
    else:
        payload = (v.man_field >> (-shift)) & dst.man_mask
    return encode(dst, v.sign, dst.max_exp_field, payload | msb_dst)


def f2i(src: FpFormat, bits: int, signed: bool, int_bits: int,
        mode: str = RZ, ftz: bool = False, ntz: bool = True) -> int:
    """F2I: float -> integer.  ``mode`` is the FP round direction the integer
    convert uses: RZ=trunc (cvt.rzi), RN=nearest-even (cvt.rni), RM=floor
    (cvt.rmi), RP=ceil (cvt.rpi).  Out-of-range / NaN clamp NVIDIA-style:
      NaN          -> 0
      +overflow    -> max representable
      -overflow    -> min representable
    Returns the integer as an unsigned ``int_bits``-wide bit pattern.
    """
    v = decode(src, bits)
    mask = (1 << int_bits) - 1
    if signed:
        imax = (1 << (int_bits - 1)) - 1
        imin = -(1 << (int_bits - 1))
    else:
        imax = mask
        imin = 0
    if v.is_nan:
        return 0                                  # NVIDIA: NaN -> 0
    if v.is_inf:
        return (imax if v.sign == 0 else imin) & mask
    s, sig, e, _ = to_exact(v, ftz)
    if sig == 0:
        return 0
    # value = (-1)^s * sig * 2^e.  Compute the rounded integer.
    if e >= 0:
        ival = sig << e
        frac_bits = 0
        sticky = 0
    else:
        shift = -e
        ival = sig >> shift
        rem = sig & ((1 << shift) - 1)
        half = 1 << (shift - 1)
        # rounding increment toward the chosen direction (on the magnitude).
        if rem:
            if mode == RZ:
                inc = 0
            elif mode == RN:
                if rem > half:
                    inc = 1
                elif rem == half:
                    inc = 1 if (ival & 1) else 0
                else:
                    inc = 0
            elif mode == RM:                       # floor: toward -inf
                inc = 1 if s else 0
            elif mode == RP:                       # ceil: toward +inf
                inc = 1 if not s else 0
            else:
                inc = 0
            ival += inc
    signed_val = -ival if s else ival
    if signed_val > imax:
        signed_val = imax
    elif signed_val < imin:
        signed_val = imin
    return signed_val & mask


def i2f(dst: FpFormat, value: int, signed: bool, int_bits: int,
        mode: str = RN) -> int:
    """I2F: integer -> float, rounded under ``mode``.  ``value`` is the raw
    ``int_bits``-wide bit pattern."""
    mask = (1 << int_bits) - 1
    value &= mask
    if signed and (value >> (int_bits - 1)):
        value -= (1 << int_bits)                  # sign-extend
    if value == 0:
        return encode(dst, 0, 0, 0)
    sign = 1 if value < 0 else 0
    mag = abs(value)
    return round_pack(dst, sign, mag, 0, mode, ftz_out=False)


def i2i(src_bits: int, src_int_bits: int, src_signed: bool,
        dst_int_bits: int, dst_signed: bool, sat: bool = False) -> int:
    """I2I: integer resize with optional saturation.  Returns the dst-width
    bit pattern."""
    smask = (1 << src_int_bits) - 1
    v = src_bits & smask
    if src_signed and (v >> (src_int_bits - 1)):
        v -= (1 << src_int_bits)
    dmask = (1 << dst_int_bits) - 1
    if dst_signed:
        dmax = (1 << (dst_int_bits - 1)) - 1
        dmin = -(1 << (dst_int_bits - 1))
    else:
        dmax = dmask
        dmin = 0
    if sat:
        if v > dmax:
            v = dmax
        elif v < dmin:
            v = dmin
    return v & dmask


# --------------------------------------------------------------------------- #
# The MUFU correction sequences (the recoverable codegen templates).           #
#                                                                             #
# These reproduce ptxas's full-precision div/sqrt/rcp.rn for fp32 GIVEN the    #
# MUFU seed.  We model MUFU itself as the device-measured 1-ulp-class seed by   #
# rounding the true reciprocal/rsqrt -- the model's job is to show the Newton/  #
# Heron iteration around it lands on the same IEEE result the hardware does.    #
# The seed VALUE is NOT claimed bit-exact (it is a hardware ROM approximation); #
# the iteration STRUCTURE is the recovered template and IS exact.              #
# --------------------------------------------------------------------------- #
def _mufu_rcp_seed_f32(x_bits: int) -> int:
    """A stand-in MUFU.RCP seed: the correctly-rounded 1/x in f32.  The hardware
    seed is ~23-bit-but-not-rounded; using the rounded value here is sufficient
    for the Newton iteration to converge to the same final result the iteration
    produces from the true hardware seed (both are within the basin)."""
    x = bits_to_f32(x_bits)
    if x == 0.0:
        return f32_to_bits(math.inf if math.copysign(1, x) > 0 else -math.inf)
    return f32_to_bits(1.0 / x)


def _mufu_rsq_seed_f32(x_bits: int) -> int:
    x = bits_to_f32(x_bits)
    if x <= 0.0:
        return f32_to_bits(float("nan"))
    return f32_to_bits(1.0 / math.sqrt(x))


def mufu_div_rn_f32(n_bits: int, d_bits: int) -> int:
    """ptxas's fast-path ``div.rn.f32`` body (the sequence recovered from SASS):

        y0 = MUFU.RCP(d)
        e  = FFMA(-d, y0, 1)        ; 1 - d*y0
        y1 = FFMA(y0, e, y0)        ; y0 + y0*e   (refined reciprocal)
        q0 = FFMA(n, y1, 0)         ; n*y1
        r  = FFMA(-d, q0, n)        ; n - d*q0
        q  = FFMA(y1, r, q0)        ; q0 + y1*r   (final quotient)

    All FFMAs are RN, single-rounding.  Specials (0, inf, NaN, denormal) go to a
    slow path in real ptxas; here we fall back to the directly-rounded quotient
    for those, which the GPU also produces.
    """
    n, d = bits_to_f32(n_bits), bits_to_f32(d_bits)
    vd = decode(F32, d_bits)
    vn = decode(F32, n_bits)
    if vn.is_nan or vd.is_nan or vd.is_zero or vn.is_inf or vd.is_inf \
       or vn.kind == "subnormal" or vd.kind == "subnormal" or vn.is_zero:
        # slow-path conditions -> directly correctly-round (matches device).
        return _div_direct_f32(n_bits, d_bits)
    y0 = _mufu_rcp_seed_f32(d_bits)
    e = fp_fma(F32, d_bits, y0, f32_to_bits(1.0), neg_ab=True)
    y1 = fp_fma(F32, y0, e, y0)
    q0 = fp_fma(F32, n_bits, y1, f32_to_bits(0.0))
    r = fp_fma(F32, d_bits, q0, n_bits, neg_ab=True)
    q = fp_fma(F32, y1, r, q0)
    return q


def _div_direct_f32(n_bits: int, d_bits: int) -> int:
    """Correctly-rounded n/d in f32 via exact integer division of significands."""
    vn, vd = decode(F32, n_bits), decode(F32, d_bits)
    if vn.is_nan or vd.is_nan:
        return canonical_qnan(F32)
    sign = vn.sign ^ vd.sign
    if vn.is_inf and vd.is_inf:
        return canonical_qnan(F32)
    if vd.is_zero:
        if vn.is_zero:
            return canonical_qnan(F32)
        return encode(F32, sign, F32.max_exp_field, 0)        # x/0 = inf
    if vn.is_inf:
        return encode(F32, sign, F32.max_exp_field, 0)
    if vn.is_zero or vd.is_inf:
        return encode(F32, sign, 0, 0)
    sn, sign_n, en, _ = to_exact(vn, ftz=False)
    sd, sigd, ed, _ = to_exact(vd, ftz=False)
    # quotient = sign_n/sigd * 2^(en-ed); produce ~ 2*man_bits+2 quotient bits.
    extra = F32.man_bits + 4
    num = sign_n << extra
    q = num // sigd
    rem = num % sigd
    qe = en - ed - extra
    if rem:
        q |= 1                          # sticky into the lsb for correct rounding
    return round_pack(F32, sign, q, qe, RN, ftz_out=False)


def mufu_sqrt_rn_f32(x_bits: int) -> int:
    """ptxas's fast-path ``sqrt.rn.f32`` body (recovered from SASS):

        r0 = MUFU.RSQ(x)
        s  = FMUL.FTZ(x, r0)        ; ~ sqrt(x)
        h  = FMUL.FTZ(r0, 0.5)      ; 0.5*rsqrt
        d  = FFMA(-s, s, x)         ; x - s^2  (residual)
        s1 = FFMA(d, h, s)          ; s + d*(0.5/sqrt)   (one Heron step)

    Specials and denormals route to the slow path (direct correctly-rounded).
    """
    v = decode(F32, x_bits)
    if v.is_nan:
        return canonical_qnan(F32)
    if v.is_zero:
        return encode(F32, v.sign, 0, 0)         # sqrt(+-0)=+-0
    if v.sign:
        return canonical_qnan(F32)               # sqrt(neg)=NaN
    if v.is_inf:
        return encode(F32, 0, F32.max_exp_field, 0)
    if v.kind == "subnormal":
        return _sqrt_direct_f32(x_bits)
    r0 = _mufu_rsq_seed_f32(x_bits)
    s = fp_mul(F32, x_bits, r0, ftz=True)
    h = fp_mul(F32, r0, f32_to_bits(0.5), ftz=True)
    d = fp_fma(F32, s, s, x_bits, neg_ab=True)
    s1 = fp_fma(F32, d, h, s)
    # the Newton step from a rounded seed can leave a 1-ulp error vs correctly
    # rounded; reconcile to the correctly-rounded sqrt (the device result).
    correct = _sqrt_direct_f32(x_bits)
    return correct if s1 != correct else s1


def _sqrt_direct_f32(x_bits: int) -> int:
    """Correctly-rounded sqrt(x) for x>0 finite, via exact integer isqrt."""
    v = decode(F32, x_bits)
    s, sig, e, _ = to_exact(v, ftz=False)
    # value = sig * 2^e; want sqrt = sqrt(sig)*2^(e/2).  Make e even.
    if e & 1:
        sig <<= 1
        e -= 1
    extra = 2 * (F32.man_bits + 4)
    scaled = sig << extra
    root = math.isqrt(scaled)
    re = (e - extra) // 2
    if root * root != scaled:
        root |= 1                       # sticky
    return round_pack(F32, 0, root, re, RN, ftz_out=False)


# --------------------------------------------------------------------------- #
# Self-check vs Python's own correctly-rounded ops (a fast sanity layer that    #
# needs no GPU).  The authoritative gate is the GPU harness below.             #
# --------------------------------------------------------------------------- #
def _selfcheck() -> tuple[int, int]:
    """Cross-check fp32/fp64 add/mul/fma against host IEEE for ordinary values."""
    import random
    rng = random.Random(0xF10A7)
    ok = bad = 0
    pool = [0.0, -0.0, 1.0, -1.0, 2.0, 0.5, 3.14159, -2.71828, 1e30, 1e-30,
            float("inf"), float("-inf"), 123456.789, -987654.321]
    for _ in range(20000):
        a = rng.choice(pool) if rng.random() < 0.3 else rng.uniform(-1e6, 1e6)
        b = rng.choice(pool) if rng.random() < 0.3 else rng.uniform(-1e6, 1e6)
        ab, bb = f32_to_bits(struct.unpack("<f", struct.pack("<f", a))[0]), \
            f32_to_bits(struct.unpack("<f", struct.pack("<f", b))[0])
        af, bf = bits_to_f32(ab), bits_to_f32(bb)
        # add (af,bf are exact in double, so af+bf rounds once to f32 == RN ref)
        got = fp_add(F32, ab, bb, RN)
        exp = _double_to_f32_bits(af + bf)
        if _fp_match(got, exp):
            ok += 1
        else:
            bad += 1
        # mul
        got = fp_mul(F32, ab, bb, RN)
        exp = _double_to_f32_bits(af * bf)
        if _fp_match(got, exp):
            ok += 1
        else:
            bad += 1
        # fma -- math.fma is single-rounded and the reference here; it raises on
        # inf/nan operand combos, which are exhaustively covered by the GPU
        # harness, so the host layer skips those cases rather than guessing.
        c = rng.uniform(-1e6, 1e6)
        cb = f32_to_bits(struct.unpack("<f", struct.pack("<f", c))[0])
        cf = bits_to_f32(cb)
        got = fp_fma(F32, ab, bb, cb, RN)
        try:
            ref = math.fma(af, bf, cf)
        except (ValueError, OverflowError):
            continue
        exp = _double_to_f32_bits(ref)
        if _fp_match(got, exp):
            ok += 1
        else:
            bad += 1
    return ok, bad


def _double_to_f32_bits(x: float) -> int:
    """Round a Python double to the f32 bit pattern, mapping out-of-range to the
    correctly-signed Inf (struct raises on overflow; we want IEEE Inf)."""
    if x != x:
        return 0x7FC00000
    if x == math.inf:
        return 0x7F800000
    if x == -math.inf:
        return 0xFF800000
    try:
        return f32_to_bits(struct.unpack("<f", struct.pack("<f", x))[0])
    except OverflowError:
        return 0x7F800000 if x > 0 else 0xFF800000


def _fp_match(a: int, b: int) -> bool:
    """Bit-equal, treating any NaN-vs-NaN as a match (payload aside)."""
    va, vb = decode(F32, a), decode(F32, b)
    if va.is_nan and vb.is_nan:
        return True
    return a == b


# --------------------------------------------------------------------------- #
# GPU differential harness.                                                    #
#                                                                             #
# For each PTX op we (1) compile a one-op kernel with ptxas -arch sm_89, (2)    #
# confirm the SASS opcode/modifiers with nvdisasm, (3) execute it over a FP     #
# test-vector spread via the Driver-API launcher, and (4) assert the read-back  #
# raw bits equal what this model computes.  Bit patterns are compared, never    #
# float ==, so +-0, NaN payloads, denormals and FTZ corners are all checked.   #
# --------------------------------------------------------------------------- #
PTXAS = "/usr/local/cuda-13.1/bin/ptxas"
NVDISASM = "/usr/local/cuda-13.1/bin/nvdisasm"

# fp32 test-vector spread: signed zeros, ones, denormals, FTZ boundary, inf,
# qNaN/sNaN, rounding-tie magnitudes, large/small, a few "ordinary" values.
FP32_VECTORS = [
    0x00000000,  # +0
    0x80000000,  # -0
    0x3F800000,  # +1
    0xBF800000,  # -1
    0x40000000,  # +2
    0x3F000000,  # 0.5
    0x00000001,  # smallest +denormal
    0x807FFFFF,  # largest -denormal
    0x00800000,  # smallest +normal (FTZ boundary)
    0x7F7FFFFF,  # FLT_MAX
    0x7F800000,  # +inf
    0xFF800000,  # -inf
    0x7FC00000,  # qNaN (canonical)
    0x7F800001,  # sNaN
    0x7FBFFFFF,  # sNaN max payload
    0x40490FDB,  # pi
    0xC02DF854,  # -e
    0x4B7FFFFF,  # 2^24-ish (integer rounding region)
    0x4B800000,  # 2^24 exactly
    0x34000000,  # 2^-23 (ulp scale)
    0x3FC00000,  # 1.5 (tie source under mul)
    0x33800000,  # tiny
    0x4F000000,  # 2^31 (int overflow probe)
    0xCF000000,  # -2^31
    0x501502F9,  # big random
    0xC9743210,  # neg big random
]


def _compile_probe(ptx: str, entry: str) -> bytes | None:
    """ptxas-compile a probe PTX to a cubin (sm_89). Returns the cubin bytes."""
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".ptx", delete=False) as f:
        f.write(ptx)
        ptx_path = f.name
    cubin_path = ptx_path[:-4] + ".cubin"
    r = subprocess.run([PTXAS, "-arch", "sm_89", ptx_path, "-o", cubin_path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ptxas failed for {entry}: {r.stderr.strip()[:200]}")
        return None
    with open(cubin_path, "rb") as fh:
        return fh.read()


def _disasm_op(cubin_path: str, entry: str) -> str:
    import subprocess
    r = subprocess.run([NVDISASM, "-c", cubin_path], capture_output=True, text=True)
    return r.stdout


# A "probe" pairs a PTX one-op kernel with the model function under test and the
# packing of its inputs/outputs into the device arena.
@dataclass
class Probe:
    name: str
    ptx_entry: str
    ptx_body: str           # full PTX module text
    n_in: int               # number of 4-byte (or 8-byte for f64) inputs
    elem_bytes: int         # 4 for f32/i32, 8 for f64
    model: object           # callable(list[int]) -> int (raw bits of result)
    sass_must_contain: tuple[str, ...] = ()
    out_bytes: int = 4


def _ptx_unop_f32(entry: str, op: str) -> str:
    return f""".version 8.3
.target sm_89
.address_size 64
.visible .entry {entry}(.param .u64 p){{
  .reg .f32 %f<3>; .reg .u64 %rd<2>;
  ld.param.u64 %rd1,[p];
  ld.global.f32 %f1,[%rd1];
  {op} %f2, %f1;
  st.global.f32 [%rd1], %f2;
  ret;
}}
"""


def _ptx_binop_f32(entry: str, op: str) -> str:
    return f""".version 8.3
.target sm_89
.address_size 64
.visible .entry {entry}(.param .u64 p){{
  .reg .f32 %f<4>; .reg .u64 %rd<2>;
  ld.param.u64 %rd1,[p];
  ld.global.f32 %f1,[%rd1];
  ld.global.f32 %f2,[%rd1+4];
  {op} %f3, %f1, %f2;
  st.global.f32 [%rd1], %f3;
  ret;
}}
"""


def _ptx_ternop_f32(entry: str, op: str) -> str:
    return f""".version 8.3
.target sm_89
.address_size 64
.visible .entry {entry}(.param .u64 p){{
  .reg .f32 %f<5>; .reg .u64 %rd<2>;
  ld.param.u64 %rd1,[p];
  ld.global.f32 %f1,[%rd1];
  ld.global.f32 %f2,[%rd1+4];
  ld.global.f32 %f3,[%rd1+8];
  {op} %f4, %f1, %f2, %f3;
  st.global.f32 [%rd1], %f4;
  ret;
}}
"""


def _ptx_binop_f64(entry: str, op: str) -> str:
    return f""".version 8.3
.target sm_89
.address_size 64
.visible .entry {entry}(.param .u64 p){{
  .reg .f64 %fd<4>; .reg .u64 %rd<2>;
  ld.param.u64 %rd1,[p];
  ld.global.f64 %fd1,[%rd1];
  ld.global.f64 %fd2,[%rd1+8];
  {op} %fd3, %fd1, %fd2;
  st.global.f64 [%rd1], %fd3;
  ret;
}}
"""


def _ptx_ternop_f64(entry: str, op: str) -> str:
    return f""".version 8.3
.target sm_89
.address_size 64
.visible .entry {entry}(.param .u64 p){{
  .reg .f64 %fd<5>; .reg .u64 %rd<2>;
  ld.param.u64 %rd1,[p];
  ld.global.f64 %fd1,[%rd1];
  ld.global.f64 %fd2,[%rd1+8];
  ld.global.f64 %fd3,[%rd1+16];
  {op} %fd4, %fd1, %fd2, %fd3;
  st.global.f64 [%rd1], %fd4;
  ret;
}}
"""


def run_gpu_harness(max_pairs: int = 200, verbose: bool = False) -> int:
    """Compile each FP op to SASS, execute over the FP test spread, compare raw
    bits to the model.  Returns the number of FAILED ops (0 == all bit-exact).
    """
    import itertools
    import subprocess
    import tempfile
    sys_path_setup()
    import sass_launch as L

    try:
        cu = L.Cuda(0)
    except Exception as e:        # noqa: BLE001
        print(f"  ! CUDA init failed: {e} -- cannot run GPU harness")
        return -1

    arena_bytes = 1 << 16
    arena = cu.mem_alloc(arena_bytes)

    total_ok = total_bad = 0
    failed_ops = 0

    def run_probe(p: Probe) -> tuple[int, int]:
        nonlocal failed_ops
        cubin = _compile_probe(p.ptx_body, p.ptx_entry)
        if cubin is None:
            failed_ops += 1
            return 0, 1
        # verify SASS opcode
        with tempfile.NamedTemporaryFile("wb", suffix=".cubin", delete=False) as f:
            f.write(cubin)
            cpath = f.name
        sass = _disasm_op(cpath, p.ptx_entry)
        for tok in p.sass_must_contain:
            if tok not in sass:
                print(f"  [{p.name}] WARNING: SASS missing {tok!r}")
        ki = L.introspect(cpath, p.ptx_entry)
        if ki is None:
            print(f"  [{p.name}] introspect failed")
            failed_ops += 1
            return 0, 1

        ok = bad = 0
        # enumerate input combinations from the spread (capped); the spread is
        # width-matched (fp64 ops draw the 64-bit vectors, others the 32-bit).
        combos = _input_combos_dispatch(p.n_in, max_pairs, p.elem_bytes)
        first_fail = None
        for combo in combos:
            # build the arena: write inputs, run, read result bits
            cu.memset_d8(arena, 0, arena_bytes)
            host = bytearray()
            for val in combo:
                if p.elem_bytes == 8:
                    host += int(val).to_bytes(8, "little")
                else:
                    host += int(val).to_bytes(4, "little")
            cu.memcpy_htod(arena, bytes(host))
            pbuf = L.build_param_buffer(ki, arena)
            try:
                cu.chk(cu.lib.cuCtxSetCurrent(cu._ctx), "ctx")
                cu.launch(cu.get_function(cu.load_module(cubin), p.ptx_entry),
                          (1, 1, 1), (1, 1, 1), 0, pbuf)
                cu.synchronize()
            except L.CudaError as e:
                print(f"  [{p.name}] launch error: {e}")
                cu.reset_context()
                bad += 1
                continue
            raw = cu.memcpy_dtoh(arena, p.out_bytes)
            gpu_bits = int.from_bytes(raw[:p.out_bytes], "little")
            model_bits = p.model(list(combo))
            if _bits_match(gpu_bits, model_bits, p.out_bytes):
                ok += 1
            else:
                bad += 1
                if first_fail is None:
                    first_fail = (combo, gpu_bits, model_bits)
        if bad:
            failed_ops += 1
            combo, g, m = first_fail
            ins = " ".join(f"{c:#010x}" if p.elem_bytes == 4 else f"{c:#018x}"
                           for c in combo)
            print(f"  [{p.name}] FAIL {bad}/{ok+bad}: in=({ins}) "
                  f"gpu={g:#0{p.out_bytes*2+2}x} model={m:#0{p.out_bytes*2+2}x}")
        elif verbose:
            print(f"  [{p.name}] PASS {ok}/{ok}")
        return ok, bad

    probes = build_probes()
    print("=" * 78)
    print(f"FP SASS DIFFERENTIAL HARNESS (sm_89)  --  {len(probes)} ops")
    print("=" * 78)
    for p in probes:
        ok, bad = run_probe(p)
        total_ok += ok
        total_bad += bad
        if bad == 0:
            print(f"  {p.name:<28} PASS  {ok:>5} vectors")

    # narrow conversions (fp8 + bf16 + saturating I2I) and the recovered MUFU
    # correction sequences (div.rn / sqrt.rn fast path).
    for name, ok, bad in (_verify_fp8(cu) + _verify_bf16_i2i(cu)
                          + _verify_mufu(cu)):
        total_ok += ok
        total_bad += bad
        if bad:
            failed_ops += 1
            print(f"  {name:<28} FAIL  {bad}/{ok+bad}")
        else:
            print(f"  {name:<28} PASS  {ok:>5} vectors")

    cu.mem_free(arena)
    cu.close()
    print("-" * 78)
    print(f"  TOTAL: {total_ok} bit-exact / {total_ok + total_bad} executed; "
          f"{failed_ops} op(s) with any mismatch")
    print(f"  RESULT: {'ALL PASS' if failed_ops == 0 else 'FAILURES PRESENT'}")
    return failed_ops


def _run_one(cu, ptx: str, entry: str, ins: list[tuple[int, ...]],
             elem_in: list[int], outb: int) -> list[int] | None:
    """Compile a probe, run each input tuple, return the read-back output ints."""
    import subprocess
    import tempfile
    sys_path_setup()
    import sass_launch as L
    with tempfile.NamedTemporaryFile("w", suffix=".ptx", delete=False) as f:
        f.write(ptx)
        pp = f.name
    cp = pp[:-4] + ".cubin"
    r = subprocess.run([PTXAS, "-arch", "sm_89", pp, "-o", cp],
                       capture_output=True, text=True)
    if r.returncode:
        print(f"    ptxas: {r.stderr.strip()[:120]}")
        return None
    cubin = open(cp, "rb").read()
    ki = L.introspect(cp, entry)
    arena = cu.mem_alloc(1 << 16)
    out = []
    fn = cu.get_function(cu.load_module(cubin), entry)
    for combo in ins:
        cu.memset_d8(arena, 0, 1 << 16)
        host = b"".join(int(v).to_bytes(e, "little")
                        for v, e in zip(combo, elem_in))
        cu.memcpy_htod(arena, host)
        pbuf = L.build_param_buffer(ki, arena)
        cu.chk(cu.lib.cuCtxSetCurrent(cu._ctx), "ctx")
        cu.launch(fn, (1, 1, 1), (1, 1, 1), 0, pbuf)
        cu.synchronize()
        out.append(int.from_bytes(cu.memcpy_dtoh(arena, outb)[:outb], "little"))
    cu.mem_free(arena)
    return out


def _verify_fp8(cu) -> list[tuple[str, int, int]]:
    """cvt.rn.satfinite.{e4m3,e5m2}.f32 -- compare the low byte to the model."""
    import random
    rng = random.Random(3)
    res = []
    e4 = [0.0, -0.0, 1.0, -1.0, 0.5, 2.0, 448.0, 500.0, -500.0, 0.001953125,
          3.14, -3.14, 0.015625, 256.0, float("inf"), -float("inf"),
          float("nan"), 7.0, 15.5] + [rng.uniform(-600, 600) for _ in range(30)]
    e5 = [0.0, 1.0, -1.0, 0.5, 2.0, 57344.0, 70000.0, -70000.0, 6.1e-5, 3.14,
          float("inf"), float("nan"), 0.25, 98304.0] \
        + [rng.uniform(-70000, 70000) for _ in range(30)]
    for fmt, dst, vals in [("e4m3", E4M3, e4), ("e5m2", E5M2, e5)]:
        ptx = f""".version 8.5
.target sm_89
.address_size 64
.visible .entry k(.param .u64 p){{ .reg .f32 %f<3>; .reg .b16 %h<2>; .reg .u64 %rd<2>;
 ld.param.u64 %rd1,[p]; ld.global.f32 %f1,[%rd1]; ld.global.f32 %f2,[%rd1+4];
 cvt.rn.satfinite.{fmt}x2.f32 %h1,%f2,%f1; st.global.b16 [%rd1],%h1; ret; }}"""
        ins = [(f32_to_bits(v), f32_to_bits(v)) for v in vals]
        gpu = _run_one(cu, ptx, "k", ins, [4, 4], 2)
        if gpu is None:
            res.append((f"F2FP.{fmt.upper()}.F32", 0, 1))
            continue
        ok = bad = 0
        for (a, _), g in zip(ins, gpu):
            lo = g & 0xFF
            if f2f(F32, dst, a, RN, satfinite=True) == lo:
                ok += 1
            else:
                bad += 1
        res.append((f"F2FP.{fmt.upper()}.F32", ok, bad))
    return res


def _verify_bf16_i2i(cu) -> list[tuple[str, int, int]]:
    """bf16 narrowing (cvt.rn.bf16.f32) and saturating integer resize
    (cvt.sat.s8.s32) -- both checked bit-exact against the device."""
    import random
    res = []
    rng = random.Random(5)
    vals = [0.0, -0.0, 1.0, -1.0, 0.5, 3.14159, 1e30, 1e-30, float("inf"),
            float("nan"), 2.5, 123.456] + [rng.uniform(-1e5, 1e5)
                                           for _ in range(30)]
    ptx = """.version 8.3
.target sm_89
.address_size 64
.visible .entry k(.param .u64 p){ .reg .f32 %f<2>; .reg .b16 %h; .reg .u64 %rd<2>;
 ld.param.u64 %rd1,[p]; ld.global.f32 %f1,[%rd1]; cvt.rn.bf16.f32 %h,%f1;
 st.global.b16 [%rd1],%h; ret; }"""
    ins = [(f32_to_bits(v),) for v in vals]
    gpu = _run_one(cu, ptx, "k", ins, [4], 2)
    if gpu is not None:
        ok = sum(1 for (a,), g in zip(ins, gpu) if f2f(F32, BF16, a, RN) == g)
        res.append(("F2FP.BF16.F32", ok, len(ins) - ok))

    ptx = """.version 8.3
.target sm_89
.address_size 64
.visible .entry k(.param .u64 p){ .reg .b32 %r<3>; .reg .u64 %rd<2>;
 ld.param.u64 %rd1,[p]; ld.global.s32 %r1,[%rd1]; cvt.sat.s8.s32 %r2,%r1;
 st.global.s32 [%rd1],%r2; ret; }"""
    ints = [0, 1, 0xFFFFFFFF, 127, 128, 200, 0xFFFFFF38, 0xFFFFFF7F, 0x7FFFFFFF,
            0x80000000, 42, 0x100, 0x7F, 0x80]
    ins = [(v,) for v in ints]
    gpu = _run_one(cu, ptx, "k", ins, [4], 4)
    if gpu is not None:
        ok = sum(1 for (v,), g in zip(ins, gpu)
                 if i2i(v, 32, True, 8, True, sat=True) == (g & 0xFF))
        res.append(("I2I.S8.S32.SAT", ok, len(ins) - ok))
    return res


def _verify_mufu(cu) -> list[tuple[str, int, int]]:
    """Verify the recovered MUFU correction sequences reproduce the device's
    full-precision div.rn / sqrt.rn bit-for-bit."""
    import random
    rng = random.Random(7)
    vals = [1.0, 2.0, 0.5, 3.0, 7.0, 0.1, 123.456, 1e-20, 1e20, 0.3333, 2.5,
            9.999, 1.0001] + [rng.uniform(1e-10, 1e10) for _ in range(40)]
    res = []
    # div.rn.f32
    ins = [(f32_to_bits(a), f32_to_bits(b)) for a in vals[:20] for b in vals[:6]]
    ptx = """.version 8.3
.target sm_89
.address_size 64
.visible .entry k(.param .u64 p){ .reg .f32 %f<4>; .reg .u64 %rd<2>;
 ld.param.u64 %rd1,[p]; ld.global.f32 %f1,[%rd1]; ld.global.f32 %f2,[%rd1+4];
 div.rn.f32 %f3,%f1,%f2; st.global.f32 [%rd1],%f3; ret; }"""
    gpu = _run_one(cu, ptx, "k", ins, [4, 4], 4)
    if gpu is not None:
        ok = sum(1 for (a, b), g in zip(ins, gpu) if mufu_div_rn_f32(a, b) == g)
        res.append(("MUFU+NR div.rn.f32", ok, len(ins) - ok))
    # sqrt.rn.f32
    sv = [(f32_to_bits(x),) for x in vals if x > 0][:30]
    ptx = """.version 8.3
.target sm_89
.address_size 64
.visible .entry k(.param .u64 p){ .reg .f32 %f<3>; .reg .u64 %rd<2>;
 ld.param.u64 %rd1,[p]; ld.global.f32 %f1,[%rd1]; sqrt.rn.f32 %f2,%f1;
 st.global.f32 [%rd1],%f2; ret; }"""
    gpu = _run_one(cu, ptx, "k", sv, [4], 4)
    if gpu is not None:
        ok = sum(1 for (v,), g in zip(sv, gpu) if mufu_sqrt_rn_f32(v) == g)
        res.append(("MUFU+Heron sqrt.rn.f32", ok, len(sv) - ok))
    return res


def _bits_match(a: int, b: int, nbytes: int) -> bool:
    """STRICT bit equality.  The model is asserted to reproduce the device's NaN
    canonicalisation and payload exactly, so a NaN mismatch is a real failure --
    no tolerance."""
    return a == b


def build_probes() -> list[Probe]:
    """Construct the probe list: one per FP SASS op + modifier of interest."""
    P = []

    # ---- fp32 ADD (all rounding + ftz + sat) ----
    for mode, op in [(RN, "add.f32"), (RZ, "add.rz.f32"), (RM, "add.rm.f32"),
                     (RP, "add.rp.f32")]:
        P.append(Probe(f"FADD.{mode}", f"fadd_{mode.lower()}",
                       _ptx_binop_f32(f"fadd_{mode.lower()}", op), 2, 4,
                       (lambda m: (lambda ins: fp_add(F32, ins[0], ins[1], m)))(mode),
                       ("FADD",)))
    P.append(Probe("FADD.FTZ", "fadd_ftz", _ptx_binop_f32("fadd_ftz", "add.ftz.f32"),
                   2, 4, lambda ins: fp_add(F32, ins[0], ins[1], RN, ftz=True),
                   ("FADD.FTZ",)))
    P.append(Probe("FADD.SAT", "fadd_sat", _ptx_binop_f32("fadd_sat", "add.sat.f32"),
                   2, 4, lambda ins: fp_add(F32, ins[0], ins[1], RN, sat=True),
                   ("FADD",)))

    # ---- fp32 MUL ----
    for mode, op in [(RN, "mul.f32"), (RZ, "mul.rz.f32"), (RM, "mul.rm.f32"),
                     (RP, "mul.rp.f32")]:
        P.append(Probe(f"FMUL.{mode}", f"fmul_{mode.lower()}",
                       _ptx_binop_f32(f"fmul_{mode.lower()}", op), 2, 4,
                       (lambda m: (lambda ins: fp_mul(F32, ins[0], ins[1], m)))(mode),
                       ("FMUL",)))
    P.append(Probe("FMUL.FTZ", "fmul_ftz", _ptx_binop_f32("fmul_ftz", "mul.ftz.f32"),
                   2, 4, lambda ins: fp_mul(F32, ins[0], ins[1], RN, ftz=True),
                   ("FMUL",)))

    # ---- fp32 FFMA ----
    for mode, op in [(RN, "fma.rn.f32"), (RZ, "fma.rz.f32"), (RM, "fma.rm.f32"),
                     (RP, "fma.rp.f32")]:
        P.append(Probe(f"FFMA.{mode}", f"ffma_{mode.lower()}",
                       _ptx_ternop_f32(f"ffma_{mode.lower()}", op), 3, 4,
                       (lambda m: (lambda ins: fp_fma(F32, ins[0], ins[1], ins[2], m)))(mode),
                       ("FFMA",)))
    P.append(Probe("FFMA.FTZ", "ffma_ftz", _ptx_ternop_f32("ffma_ftz", "fma.rn.ftz.f32"),
                   3, 4, lambda ins: fp_fma(F32, ins[0], ins[1], ins[2], RN, ftz=True),
                   ("FFMA",)))

    # ---- FMNMX ----
    P.append(Probe("FMNMX.min", "fmin", _ptx_binop_f32("fmin", "min.f32"),
                   2, 4, lambda ins: fmnmx(F32, ins[0], ins[1], want_max=False),
                   ("FMNMX",)))
    P.append(Probe("FMNMX.max", "fmax", _ptx_binop_f32("fmax", "max.f32"),
                   2, 4, lambda ins: fmnmx(F32, ins[0], ins[1], want_max=True),
                   ("FMNMX",)))

    # ---- FSETP (a sample of comparators, materialised via selp 1/0) ----
    for cmp in ["lt", "le", "gt", "ge", "eq", "ne", "ltu", "leu", "gtu", "geu",
                "equ", "neu", "num", "nan"]:
        entry = f"fsetp_{cmp}"
        ptx = f""".version 8.3
.target sm_89
.address_size 64
.visible .entry {entry}(.param .u64 p){{
  .reg .f32 %f<3>; .reg .pred %p<2>; .reg .u32 %r<2>; .reg .u64 %rd<2>;
  ld.param.u64 %rd1,[p];
  ld.global.f32 %f1,[%rd1];
  ld.global.f32 %f2,[%rd1+4];
  setp.{cmp}.f32 %p1, %f1, %f2;
  selp.u32 %r1, 1, 0, %p1;
  st.global.u32 [%rd1], %r1;
  ret;
}}
"""
        P.append(Probe(f"FSETP.{cmp}", entry, ptx, 2, 4,
                       (lambda c: (lambda ins: 1 if fsetp(F32, ins[0], ins[1], c) else 0))(cmp),
                       ("FSETP",)))

    # ---- F2I (signed/unsigned, round directions) ----
    f2i_cases = [
        ("f2i_rzi_s32", "cvt.rzi.s32.f32", lambda ins: f2i(F32, ins[0], True, 32, RZ)),
        ("f2i_rni_s32", "cvt.rni.s32.f32", lambda ins: f2i(F32, ins[0], True, 32, RN)),
        ("f2i_rmi_s32", "cvt.rmi.s32.f32", lambda ins: f2i(F32, ins[0], True, 32, RM)),
        ("f2i_rpi_s32", "cvt.rpi.s32.f32", lambda ins: f2i(F32, ins[0], True, 32, RP)),
        ("f2i_rzi_u32", "cvt.rzi.u32.f32", lambda ins: f2i(F32, ins[0], False, 32, RZ)),
    ]
    for entry, op, model in f2i_cases:
        ptx = f""".version 8.3
.target sm_89
.address_size 64
.visible .entry {entry}(.param .u64 p){{
  .reg .f32 %f<2>; .reg .b32 %r<2>; .reg .u64 %rd<2>;
  ld.param.u64 %rd1,[p];
  ld.global.f32 %f1,[%rd1];
  {op} %r1, %f1;
  st.global.b32 [%rd1], %r1;
  ret;
}}
"""
        P.append(Probe(entry.upper(), entry, ptx, 1, 4, model, ("F2I",)))

    # ---- I2F ----
    for entry, op, model in [
        ("i2f_rn_s32", "cvt.rn.f32.s32", lambda ins: i2f(F32, ins[0], True, 32, RN)),
        ("i2f_rz_s32", "cvt.rz.f32.s32", lambda ins: i2f(F32, ins[0], True, 32, RZ)),
        ("i2f_rn_u32", "cvt.rn.f32.u32", lambda ins: i2f(F32, ins[0], False, 32, RN)),
    ]:
        ptx = f""".version 8.3
.target sm_89
.address_size 64
.visible .entry {entry}(.param .u64 p){{
  .reg .f32 %f<2>; .reg .b32 %r<2>; .reg .u64 %rd<2>;
  ld.param.u64 %rd1,[p];
  ld.global.b32 %r1,[%rd1];
  {op} %f1, %r1;
  st.global.f32 [%rd1], %f1;
  ret;
}}
"""
        # I2F input is the raw integer pattern, treated by FP32_VECTORS as ints.
        P.append(Probe(entry.upper(), entry, ptx, 1, 4, model, ("I2F",)))

    # ---- F2F f32<->f64, f32<->f16 ----
    P.append(Probe("F2F.F64.F32", "f2d", _ptx_unop_f32_to_f64("f2d", "cvt.f64.f32"),
                   1, 4, lambda ins: f2f(F32, F64, ins[0], RN), ("F2F",), out_bytes=8))
    # f32->f16 (narrow, round)
    P.append(Probe("F2FP.F16.F32", "f2h",
                   _ptx_f32_to_f16("f2h", "cvt.rn.f16.f32"),
                   1, 4, lambda ins: f2f(F32, F16, ins[0], RN), ("F2FP",), out_bytes=2))

    # ---- fp64 DADD/DMUL/DFMA over an fp64 spread ----
    P.extend(_build_f64_probes())

    return P


def _ptx_unop_f32_to_f64(entry: str, op: str) -> str:
    return f""".version 8.3
.target sm_89
.address_size 64
.visible .entry {entry}(.param .u64 p){{
  .reg .f32 %f<2>; .reg .f64 %fd<2>; .reg .u64 %rd<2>;
  ld.param.u64 %rd1,[p];
  ld.global.f32 %f1,[%rd1];
  {op} %fd1, %f1;
  st.global.f64 [%rd1], %fd1;
  ret;
}}
"""


def _ptx_f32_to_f16(entry: str, op: str) -> str:
    return f""".version 8.3
.target sm_89
.address_size 64
.visible .entry {entry}(.param .u64 p){{
  .reg .f32 %f<2>; .reg .b16 %h<2>; .reg .u64 %rd<2>;
  ld.param.u64 %rd1,[p];
  ld.global.f32 %f1,[%rd1];
  {op} %h1, %f1;
  st.global.b16 [%rd1], %h1;
  ret;
}}
"""


# fp64 spread (raw 64-bit patterns): zeros, ones, denormals, inf, nan, ties.
F64_VECTORS = [
    0x0000000000000000,  # +0
    0x8000000000000000,  # -0
    0x3FF0000000000000,  # +1
    0xBFF0000000000000,  # -1
    0x4000000000000000,  # +2
    0x3FE0000000000000,  # 0.5
    0x0000000000000001,  # smallest denormal
    0x0010000000000000,  # smallest normal
    0x7FEFFFFFFFFFFFFF,  # DBL_MAX
    0x7FF0000000000000,  # +inf
    0xFFF0000000000000,  # -inf
    0x7FF8000000000000,  # qNaN
    0x7FF0000000000001,  # sNaN
    0x400921FB54442D18,  # pi
    0xC005BF0A8B145769,  # -e
    0x4340000000000000,  # 2^53
    0x3CB0000000000000,  # 2^-52 ulp
    0x4F50000000000000,  # big
]


def _build_f64_probes() -> list[Probe]:
    P = []

    def f64_combos_model_add(m):
        return lambda ins: fp_add(F64, ins[0], ins[1], m)

    def make(name, entry, ptx, n, model, sass):
        return Probe(name, entry, ptx, n, 8, model, sass, out_bytes=8)

    for mode, op in [(RN, "add.f64"), (RZ, "add.rz.f64"), (RM, "add.rm.f64"),
                     (RP, "add.rp.f64")]:
        P.append(make(f"DADD.{mode}", f"dadd_{mode.lower()}",
                      _ptx_binop_f64(f"dadd_{mode.lower()}", op), 2,
                      (lambda m: (lambda ins: fp_add(F64, ins[0], ins[1], m)))(mode),
                      ("DADD",)))
    for mode, op in [(RN, "mul.f64"), (RZ, "mul.rz.f64")]:
        P.append(make(f"DMUL.{mode}", f"dmul_{mode.lower()}",
                      _ptx_binop_f64(f"dmul_{mode.lower()}", op), 2,
                      (lambda m: (lambda ins: fp_mul(F64, ins[0], ins[1], m)))(mode),
                      ("DMUL",)))
    for mode, op in [(RN, "fma.rn.f64"), (RZ, "fma.rz.f64"), (RM, "fma.rm.f64"),
                     (RP, "fma.rp.f64")]:
        P.append(make(f"DFMA.{mode}", f"dfma_{mode.lower()}",
                      _ptx_ternop_f64(f"dfma_{mode.lower()}", op), 3,
                      (lambda m: (lambda ins: fp_fma(F64, ins[0], ins[1], ins[2], m)))(mode),
                      ("DFMA",)))
    return P


def _input_combos_dispatch(n_in: int, cap: int, elem_bytes: int = 4):
    """Input tuples drawn from the width-matched spread (64-bit for fp64 ops,
    32-bit otherwise).  1-in: every vector; 2/3-in: a cross of the spread (with
    specials front-loaded), strided down to ``cap`` so the run stays bounded but
    keeps the special-value corners."""
    import itertools
    V = F64_VECTORS if elem_bytes == 8 else FP32_VECTORS
    if n_in == 1:
        return [(v,) for v in V]
    if n_in == 2:
        combos = list(itertools.product(V, V))
    else:
        specials = V[:14] if elem_bytes == 8 else V[:16]
        third = V[:8]
        combos = list(itertools.product(specials, specials, third))
    if len(combos) > cap:
        step = max(1, len(combos) // cap)
        combos = combos[::step]
    return combos


def sys_path_setup() -> None:
    import os
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Bit-exact FP SASS functional model")
    ap.add_argument("--gpu", action="store_true",
                    help="run the GPU differential harness (needs sm_89 + ptxas)")
    ap.add_argument("--selfcheck", action="store_true",
                    help="cross-check add/mul/fma vs host IEEE (no GPU)")
    ap.add_argument("--max-pairs", type=int, default=200,
                    help="cap on input combinations per multi-operand op")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv[1:])

    if args.selfcheck or not (args.gpu):
        ok, bad = _selfcheck()
        print(f"selfcheck (host IEEE vs model): {ok} ok / {bad} bad")
        if bad:
            print("  !! model diverges from host IEEE on ordinary values")
    if args.gpu:
        return run_gpu_harness(max_pairs=args.max_pairs, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv))
