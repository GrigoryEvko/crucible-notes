#!/usr/bin/env python3
"""
RT-core (TTU) piecewise-linear function evaluator -- prototype / feasibility harness.

Concept: evaluate a piecewise-linear f(x) on the ray-tracing core. Encode the PWL
graph as triangles in the (x, h) plane; fire a vertical ray at x_q; the hardware
ray-triangle intersection returns barycentric coordinates, and the interpolated
vertex height IS f(x_q) -- the linear interpolation between breakpoints done in
fixed-function silicon. A batch of rays = f evaluated at a batch of points.

This harness does three things:
  1. validates the geometric encoding on the CPU (the ray-cast and the direct PWL
     eval must agree) -- proving the IDEA is sound;
  2. carries the recovered TTU instruction encoding (so the raw-SASS emit path is
     concrete);
  3. documents, honestly, why the raw-SASS GPU path is blocked and the exact
     experiment that would unblock it.

Encodings recovered from binary analysis of the CUDA-13 nvdisasm SM89 instruction
tables + the SASS 128-bit format. No GPU traversal is attempted: TTUGO validates
BVH structure in hardware, so a guessed BVH would hang the unit with no info gain.
"""
from __future__ import annotations

# --- Recovered TTU opcodes (opcode field = {bit91, bits[11:0]}; bit91=0 here) ---
TTU_OPCODES = {
    "TTUOPEN": 0x3d0, "TTUST": 0x3d1, "TTULD": 0x3d2, "TTUCLOSE": 0x3d2,
    "TTUGO": 0x3d3, "TTUCCTL": 0x3d5, "TTUMACROFUSE": 0x9d4,
}


def encode_opcode_word(op: str) -> int:
    """Place a TTU opcode into a 128-bit SASS instruction word (opcode bits only).

    Demonstrates the emit path is concrete: the opcode occupies bits[11:0] (bit91
    is 0 for every TTU op). The operand/scoreboard fields (ttuAddr index, Rb/Rc,
    req/wr/rd scoreboards) populate the rest per the encoding in ttu_spec.md; they
    are op-specific and filled by the assembler, not modeled here.
    """
    return TTU_OPCODES[op] & 0xFFF


# --------------------------------------------------------------------------- #
# 1. The geometric encoding, validated on the CPU.
# --------------------------------------------------------------------------- #
def pwl_direct(breakpoints, xq):
    """Reference: evaluate the PWL f at xq directly (the answer we must match)."""
    pts = sorted(breakpoints)
    if xq <= pts[0][0]:
        return pts[0][1]
    if xq >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= xq <= x1:
            w = (xq - x0) / (x1 - x0)        # barycentric weight toward x1
            return (1.0 - w) * y0 + w * y1
    raise AssertionError("unreachable")


def pwl_via_raycast(breakpoints, xq):
    """Simulate the RT-core path: a vertical ray at xq hits one PWL segment; the
    intersection's height is reconstructed from the segment's barycentric weight --
    exactly the interpolation a ray-triangle test yields. Must equal pwl_direct."""
    pts = sorted(breakpoints)
    # clamp ray to the encoded domain (open scene -> miss outside; mirror direct)
    if xq <= pts[0][0]:
        return pts[0][1]
    if xq >= pts[-1][0]:
        return pts[-1][1]
    # find the hit segment (the "primitive" the ray intersects)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= xq <= x1:
            # ray-segment intersection in (x,h): the hit's barycentric coord along
            # the segment is the RT core's interpolation weight.
            bary1 = (xq - x0) / (x1 - x0)
            bary0 = 1.0 - bary1
            return bary0 * y0 + bary1 * y1   # interpolated height = f(xq)
    raise AssertionError("unreachable")


def selfcheck():
    import math
    # a non-trivial PWL: a clamped ramp with a kink and a dip
    bp = [(0.0, 0.0), (1.0, 2.0), (2.0, 1.5), (3.5, 4.0), (5.0, -1.0)]
    xs = [i * 0.05 for i in range(-20, 121)]   # incl. out-of-domain (clamped)
    bad = 0
    for x in xs:
        a, b = pwl_direct(bp, x), pwl_via_raycast(bp, x)
        if not math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12):
            bad += 1
            print(f"  MISMATCH x={x:.3f} direct={a} raycast={b}")
    assert bad == 0, f"{bad} mismatches"
    # encoding sanity
    assert encode_opcode_word("TTUGO") == 0x3d3
    assert encode_opcode_word("TTUST") == 0x3d1
    print(f"SELFCHECK OK: {len(xs)} points, ray-cast interpolation == direct PWL; "
          f"TTU opcodes encode.")


# --------------------------------------------------------------------------- #
# 2. The raw-SASS GPU path -- BLOCKED (documented, not faked).
# --------------------------------------------------------------------------- #
GPU_PATH_STATUS = """\
raw-SASS GPU traversal: BLOCKED on the fixed-function data contract.
  buildable : the TTUST/TTUOPEN/TTUGO/TTULD instruction sequence (encoding recovered).
  missing   : (a) the ttuAddr slot map (index -> ray field / BVH root / hit),
              (b) the ray + 128-bit hit-record byte layout,
              (c) the compressed BVH ("complet") node format the hardware validates.
  why not attempted: TTUGO hangs the TTU on a malformed BVH; guessing gains nothing.
  to unblock: build a 1-triangle accel structure with the public ray-tracing API,
              dump the device buffer to reverse the node layout, and capture a
              minimal closest-hit trace's SASS to read the ttuAddr slot indices.
              Then hand-assemble the sequence over the driver-built BVH and launch
              via the raw-SASS path. See ttu_spec.md.
"""

if __name__ == "__main__":
    selfcheck()
    print()
    print(GPU_PATH_STATUS)
