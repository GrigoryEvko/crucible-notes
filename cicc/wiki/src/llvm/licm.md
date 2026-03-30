# LICM (Loop-Invariant Code Motion) -- Redirect

> **This page previously contained LoopUnroll content due to a sweep misidentification.** The LoopUnroll pass factory at `sub_19B73C0` was incorrectly labeled as LICM because the two passes are adjacent in the binary. All LoopUnroll content has been merged into the [Loop Unrolling](./loop-unroll.md) page.

**For the actual LICM documentation, see: [LICM (Loop-Invariant Code Motion)](./licm-real.md)**

The LICM page covers:
- IR-level LICM (`"licm"`, backed by MemorySSA) -- hoist and sink modes
- Machine-level LICM (`"early-machinelicm"`, `"machinelicm"`) -- pre-RA and post-RA
- GPU-specific considerations: register pressure, occupancy cliffs, NVVM AA cross-address-space independence
- All pipeline positions, knobs, and diagnostic strings
- Interaction with downstream passes (rematerialization, Sinking2)
