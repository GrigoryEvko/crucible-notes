# SM-version coverage matrix — full Blackwell lineup (sm_100/103/110/120/121)

Authoritative coverage + correctness audit of every per-architecture table across all
`decoded/` directories against the full Blackwell lineup: **sm_100** (datacenter Blackwell
GB100, sched-gen 8), **sm_103** (Blackwell Ultra GB300, sched-gen 8), **sm_110** (Thor /
GB10-class, sched-gen 9), **sm_120** and **sm_121** (consumer Blackwell, sched-gen 8).

Per-arch correctness (verified / discrepancy / gap) is in the matrix below; the full
empirical ground truth, every discrepancy, and the cross-domain consistency verdict are in
**`blackwell_validation_report.md`** (independent verification backbone).

## Method / ground truth

- Binary: `ptxas` CUDA **13.0.88** (`ptxas/ptxas`, sha256
  `daba837a…b849f2`, 37.74 MB x86-64 ELF) — the binary the tables were decoded from.
  Cross-checked with a second oracle `ptxas`/`nvdisasm`/`cuobjdump`/`nvcc` from CUDA
  **13.1.115**. `.text`/`.rodata` use VMA == file_offset + 0x400000.
- All five SM targets (and their `a`/`f` variants) are accepted by both ptxas binaries.
  sm_110 requires PTX `.version 9.0`; sm_100/103/120/121 accept `8.8`.
  `sm_101`/`sm_104`/`sm_107`/`sm_130` are **all rejected** by the gpu-name parser
  (internal enum / cap-bit normalize targets only).
- **Empirically confirmed this pass** (full corpus arith/FP64/control/convert/vadd compiled
  across all 5 + disassembled + ELF-parsed):
  - `e_flags` = `0x06006402 / 6702 / 6e02 / 7802 / 7902` (real-SM byte 0x64/67/6e/78/79).
  - `__CUDA_ARCH__` = 1000 / 1030 / 1100 / 1200 / 1210 (nvcc-verified).
  - max registers = **255** for all five (256 rejected).
  - FP64 dependent-DFMA gap: sm_100 = **0x10** (full-rate); sm_103/110/120/121 = **0x50**
    (rate-limited, 4 stall slots).
  - SASS: **sm_120 ≡ sm_121** byte-identical (all kernels). sm_100 has its own
    scheduling/encoder tables; **sm_103 ≡ sm_110 ≡ sm_120 ≡ sm_121 share the sm_103
    Blackwell sched tables** (dep-rules + scoreboard + 72-B latency data-identical), but
    each has distinct *codegen* (Thor IADD3, consumer IADD).
  - sm_110-only GB10B WAR (`__nv_reservedSMEM_gb10b_war_var` + `.nv.shared.reserved.0`,
    128-B object) — verified present on sm_110, absent on sm_120/121.
  - EICOMPAT CAN_FASTPATH_FINALIZE = 0x50 on sm_120, 0x00 on sm_110 & sm_121 (the
    sm_120 ≠ sm_121 container distinction) — read straight from `.nv.compat`.

Status legend: **verified** = table value cross-checked equal to ground truth this pass;
**covered** = present, not re-measured; **arch-independent** = not keyed on SM version;
**family** = keyed by SM family (sm_10x) which subsumes the lineup; **discrepancy** =
flagged in `blackwell_validation_report.md` (owner noted).

## Correctness matrix (all decoded dirs × full Blackwell lineup)

Per-cell: status for sm_100 / sm_103 / sm_110 / sm_120 / sm_121. **verified** = cross-checked
equal to ground truth this pass; **D** = discrepancy (see `blackwell_validation_report.md`);
empty cells inherit the row status.

| Directory | Per-arch keyed? | sm_100 | sm_103 | sm_110 | sm_120 | sm_121 | Note / verification |
|---|---|---|---|---|---|---|---|
| **ptxas-targets** | yes (per-SM enum) | verified | verified | verified | verified | verified | `sass_elf_eflags` + `sm_target_properties` match ground truth exactly: e_flags 0x06006402/6702/6e02/7802/7902, `__CUDA_ARCH__` 1000/1030/1100/1200/1210 (nvcc-verified). `sm_version_codes` legacy table ends sm_90f by design (Blackwell uses decimal codes). sm_id_enum carries a latent sm_101 row (arch rejected). |
| **ptxas-elf-output** | yes (container fields) | verified | verified | verified | verified | verified | e_flags/GB10B/CAN_FASTPATH_FINALIZE(0x50 sm_120, 0x00 sm_110/sm_121)/symtab(12/10/10) all verified. **D1**: cuinfo.virtualSM claimed 110/120/121 but is **100 for all** (family base). **D2**: bits[16:23] mislabeled "virtual SM" (always 0; real marker = bits[24:31]=0x06). **D3**: "sm_110==sm_120 SASS" is sample-kernel/13.0.88-scoped, not general. |
| **ptxas-encoding-full** | per-arch encoder block | verified | verified | verified | verified | verified | `per_arch_encoder_blocks`: block3=sm_90/90a/100/103, block6=sm_110(NEW), block7=sm_120/121(NEW). `sass_class_presence_by_arch` matches the nvdisasm SASS-ISA dumps exactly (TTU/consumer-qmma sm_120-only; TMEM/tcgen05 datacenter). opbyte counts verified on the discriminating bytes (0x24/0x36/0x18). sm_121 shares sm_120 column. |
| **ptxas-sched-full** | yes (per-SM/family) | verified | verified | verified | verified | verified | sm_100 has its own dep-rules+scoreboard+latency table; **sm_103 ≡ sm_110 ≡ sm_120 ≡ sm_121 data-identical** (md5-confirmed minus comments). FP64 dependent-DFMA gap 0x10(sm_100)/0x50(rest) confirmed by address-spacing. `blackwell_consumer_stall_deltas` direction confirmed by raw control-word stall read. Thor codegen distinct (fp64 SASS differs from sm_103). |
| **ptxas-isel** | yes (per-arch histogram) | verified | verified | verified | verified* | verified* | `per_arch_opcode_histogram` reproduces exactly on ptxas 13.0.88; move/ALU idiom table (sm_100/103 VIADD+IMAD.MOV; sm_110 IADD3+IMNMX; sm_120/121 IADD+MOV+HFMA2) verified. **N1**: sm_120/121 IADD3=34 is 13.0.88-pinned (→1 on 13.1.115). |
| **ptxas-regalloc** | yes (selector code) | verified | verified | verified (alias) | verified | verified | Blackwell family shares regclass table 0x21FB680, mode_flag=2, **255 budget** (256 rejected — probe-confirmed). sm_110 aliases (no own class). `fp64_throughput_class` (sm_100 FAST 0x10; sm_103/110/120/121 RATE-LIMITED 0x50) byte-confirmed by DFMA gap. |
| **ptxas-mercury** | yes (cap-bit table) | consistent | consistent | consistent | consistent | consistent | `arch_compat_capbits` (0x01/0x08/0x02/0x10/0x40) = internal `sub_60F290` jump-table outputs; structurally self-consistent, family-normalize phantoms (sm_101→110, sm_104→120) all confirmed rejected by gpu-name parser. Not serialized in any cubin → behaviorally unverifiable (marked consistent, not verified). |
| **nvdisasm-sass-isa** | yes (one file per SM) | verified | verified | verified | verified | verified | `sass_isa_SM100/103/110/120/121.txt`; **SM120 ≡ SM121 byte-identical** (md5-confirmed). Class presence cross-checked against encoding-full. |
| **ptxas-gap-closure** | yes (version-code model) | verified | verified | verified | verified | verified | F5 version-code-family model (var0=sm_100, var1=sm_101/110, var3=sm_103, var4=sm_120, var5=sm_121). **Terminology flag**: its "gen-9" is the legacy 0x9xxx version-hash family (all Blackwell), distinct from targets' scheduler "gen9" (sm_110 only). |
| sass-tools | partly (stall matrix) | family | family | family | covered | covered | `coupled_stall_matrix`: sm10x family incl 100/103/110; explicit sm120/sm121 override rows. |
| ptxas-knobs-builtins | family (sm10x col) | family | family | family | family | family | `pipeline_flags_sm10x` subsumes all gen≥100. |
| ptxas-scheduling | no (representative) | arch-independent | | | | | single sched-class table (sm_8x family) at VMA 0x2297C00. |
| ptxas-ir | no (intro era) | arch-independent | | | | | opcode enum single table; sm_gen = first-appearance generation. |
| ptxas-fp-debug / -driver / -passes / -passes-detail | no | arch-independent | | | | | FP fold / driver / phase pipeline — no SM keying. |
| ptxas-instr-defs / -messages / -tokens | no | arch-independent | | | | | instruction registry / diagnostic catalog / PTX lexer — no SM keying. |
| ptxas-pseudo-instructions | family | family | | | | | `macro_catalog` arch col = sm10x (Blackwell), not per-SM. |
| ptxas-ptx-macro-pool / nvlink-ptx-macro-pool | no (single pool) | n/a | | | | | one arch-independent lowering pool; tokens top out at sm_100 / family sm_10x. |
| cicc-tables | no | n/a | | | | | LLVM/Clang/PTX name dictionaries; no SM keying. |
| reference | no (3rd-party) | n/a | | | | | redplait extractor + decrypted snapshot; not redistributed. |

## Discrepancies & version-skew (full detail in `blackwell_validation_report.md`)

| item | severity | owner | summary |
|---|---|---|---|
| **D1** cuinfo.virtualSM | MEDIUM | ptxas-elf-output | `per_arch_sm110_120_121.tsv` says virtualSM = 110/120/121; truth = **100 for all** (compute_100 family base), on both 13.0.88 & 13.1. |
| **D2** e_flags virtual-SM bit-slice | LOW | ptxas-elf-output | bits[16:23] labeled "virtual SM" are always 0; the real family marker is bits[24:31]=0x06 (= targets' `0x0600`). |
| **D3** "sm_110==sm_120 SASS" | LOW | ptxas-elf-output | true only for the simple sample kernel on 13.0.88; arith/isel kernels and 13.1.115 diverge (sm_110 IADD3 vs sm_120 IADD). sm_120≡sm_121 IS general. |
| **N1** isel IADD3=34 | INFO | ptxas-isel | sm_120/121 IADD3=34 is 13.0.88-pinned (→1 on 13.1.115); pin the version when quoting. |
| **gen-9 collision** | MEDIUM (clarity) | ptxas-gap-closure + ptxas-targets | both use "gen-9" for orthogonal axes (version-hash family vs scheduler gen). Label the axis explicitly. |

## Cross-domain consistency verdict (summary)

1. **Instruction-class set — CONSISTENT.** nvdisasm-sass-isa ↔ encoding-full ↔ sched-full
   agree on the Blackwell class universe (TMEM/tcgen05 datacenter sm_100/103/110; TTU +
   consumer-tensor sm_120/121).
2. **Version codes / e_flags — CONSISTENT (one mislabel D1/D2).** targets ↔ elf-output ↔
   mercury agree on e_flags real-SM bytes, the 0x06 virtual marker, `__CUDA_ARCH__` codes,
   and the phantom-arch story; only the elf-output cuinfo field is wrong.
3. **FP64 / regalloc — CONSISTENT.** regalloc `fp64_throughput_class` ↔ sched-full ↔ the
   255-register budget all tell the same story (sm_100 full-rate; sm_103/110/120/121
   rate-limited; all share regclass table 0x21FB680).

## Previously-flagged gaps — now CLOSED by sibling agents (verified this pass)

The earlier coverage listed three MISSING gaps; the owning agents have since filled them and
they are verified here:

- **ptxas-sched-full** — now covers sm_110/120/121 (dep-rules, scoreboard, latency for all
  five; sm_103-shared confirmed data-identical). ✓
- **ptxas-encoding-full** — block 6 (sm_110 Thor) + block 7 (sm_120/121) added; class
  presence cross-checked. ✓
- **ptxas-isel** — `per_arch_opcode_histogram.tsv` covers sm_100..sm_121 (reproduced on
  13.0.88). ✓

## Genuinely arch-independent directories (no per-arch gap)

**ptxas-fp-debug, ptxas-driver, ptxas-passes-detail, ptxas-passes, ptxas-ir** are
arch-independent (PTX-level / ptxas-internal). **ptxas-knobs-builtins**,
**ptxas-pseudo-instructions** are family-keyed (sm10x subsumes the lineup). Also
arch-independent: ptxas-instr-defs, ptxas-messages, ptxas-tokens, cicc-tables, the three
PTX-macro pools, and reference.
