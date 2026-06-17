# Summary

[neuronx-cc Internals](index.md)

---

# Part 0 — Reference Apparatus

- [The Compile Pipeline at a Glance](front/pipeline.md)
- [Worked Example A — a matmul end-to-end](front/worked-example-matmul.md)
- [Worked Example B — a flash-attention end-to-end](front/worked-example-flash-attention.md)
- [Methodology & the Confidence Model](methodology.md)
- [Binary Inventory & the .so Map](reference/binary-inventory.md)
- [Build & Version Provenance](reference/versions.md)
- [Glossary & Naming Conventions](glossary.md)

# Part 1 — Hardware & Engine Model

- [The Arch Object Model (getArchModel → Board/Device/Core)](arch/arch-object-model.md)
- [Per-Generation Hardware-Constant Matrix](arch/hardware-constant-matrix.md)
- [SBUF / PSUM Bank Geometry](arch/sbuf-psum-geometry.md)
- [Vestigial Generations — CoreV1 (Inferentia) & CoreV5](arch/vestigial-generations.md)
- [DRAM / HBM Geometry & the DRAM Split](arch/dram-hbm-geometry.md)
- [The multi-core (LNC) memory model](arch/lnc-memory-model.md)
- [PE Engine — the Systolic Matmul Array](arch/pe-engine.md)
- [Pool Engine — Windowed Pooling and the Reduce Leg](arch/pool-engine.md)

<!-- Roadmap: pages below land part-by-part as they are written.
     The full 355-page plan is tracked in the task board (one task per page).
     Section headers are kept here so the book's shape is visible from day one.

# Part 1 — Hardware & Engine Model            (arch/)        14 pages
# Part 2 — The Tonga ISA                        (isa/)         28 pages
# Part 3 — Frontend, Driver & Diagnostics       (frontend/)    21 pages
# Part 4 — hlo-opt + hlo2penguin                (hlo-opt/)     45 pages
# Part 5 — Penguin IR & Middle-End              (penguin/)     27 pages
# Part 6 — NKI Kernel DSL                        (nki/)         61 pages
# Part 7 — BIR, libBIR & the Simulator          (bir/)         41 pages
# Part 8 — The libwalrus Backend                (walrus/)      52 pages
# Part 9 — Numeric Semantics                     (numerics/)    10 pages
# Part 10 — Activation & PWP                     (activation/)   7 pages
# Part 11 — Custom Ops & GPSIMD                  (customop/)    10 pages
# Part 12 — NEFF Container & Packaging           (formats/)      8 pages
# Part 13 — Distribution & Collectives           (distribution/) 11 pages
# Part 14 — Appendices                           (appendix/)    12 pages
-->
