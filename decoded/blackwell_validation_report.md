# Blackwell-lineup cross-validation report (sm_100/103/110/120/121)

Independent ground-truth verification of every per-arch decoded table across all
`decoded/` directories, for the full Blackwell lineup. This is a **read-only audit**;
discrepancies are flagged with the owning sibling directory, not fixed here.

## Method / ground truth

Two ptxas binaries were used as oracles:

- **ptxas 13.0.88** (`ptxas/ptxas`) — the binary the decoded tables were extracted from.
- **ptxas 13.1.115** (`/usr/local/cuda-13.1/bin/ptxas`) — newer reference; plus
  `nvdisasm`/`cuobjdump`/`nvcc` 13.1.115.

A broad PTX corpus (arith / FP64 / control-flow / conversion / simple vadd / a decoupled
math probe + the isel histogram probe) was compiled with `ptxas -arch sm_1XX` across all
five Blackwell targets (sm_110 with `.version 9.0`, the rest with `8.8`), then disassembled
with `nvdisasm -c [-hex]` and inspected with `cuobjdump -elf`/`readelf`. Raw 128-bit SASS
words were parsed straight out of `.text.*` to read the control-word usched stall field; ELF
`e_flags`, `.nv.compat`, `.nv.cuinfo` and symtab were parsed straight out of the cubins.
`__CUDA_ARCH__` was confirmed with `nvcc -E`.

All five arches (and their `a`/`f` variants) are accepted by both ptxas binaries.
`sm_101`, `sm_104`, `sm_107`, `sm_130` are **all rejected** by the gpu-name parser in both —
they exist only as internal enum slots / cap-bit normalize targets.

## Ground-truth facts (all empirically measured this pass)

| # | Fact | Value |
|---|---|---|
| 1 | `e_flags` (13.0.88 ≡ 13.1) | sm_100 `0x06006402`, sm_103 `0x06006702`, sm_110 `0x06006e02`, sm_120 `0x06007802`, sm_121 `0x06007902` |
| 2 | `__CUDA_ARCH__` (nvcc) | 1000 / 1030 / 1100 / 1200 / 1210 |
| 3 | `.note.nv.cuinfo` virtualSM | **= 100 for ALL five** (family base, not per-arch) — both 13.0.88 & 13.1 |
| 4 | max registers | **255** for all five (`--maxrregcount=256` rejected, 255 accepted) |
| 5 | FP64 dependent-DFMA address gap | sm_100 = **0x10** (16 B, back-to-back); sm_103/110/120/121 = **0x50** (80 B, 4 stall slots) |
| 6 | SASS byte-identity | sm_120 ≡ sm_121 (all kernels, both versions); sm_100 ≡ sm_103 ≡ sm_110 (simple vadd, both versions) |
| 7 | int-ALU / move idiom (13.0.88) | sm_100/103 = `VIADD`+`IMAD.MOV`; sm_110 = `IADD3`+`IMNMX` (no `VIADD`, keeps `IMAD.MOV`); sm_120/121 = `IADD`+plain `MOV`+`HFMA2` |
| 8 | sched tables (dep-rules + scoreboard + 72-B latency) | sm_100 **distinct**; sm_103 ≡ sm_110 ≡ sm_120 ≡ sm_121 **byte-identical data rows** |
| 9 | SASS-ISA class presence | tcgen05 / TMEM (`ldtm_`/`sttm_`/`utc*`) on sm_100/103/110; TTU (RT: `ttugo_`/`ttuld_`) + consumer tensor (`qmma_`/`omma_`/`mxqmma_scale_`) on sm_120/121 |
| 10 | GB10B reserved-SMEM WAR | `__nv_reservedSMEM_gb10b_war_var` (128-B WEAK OBJECT) + `.nv.shared.reserved.0` section: **sm_110 ONLY**. sm_120/121 carry only `__nv_reservedSMEM_offset_0_alias` (no gb10b string) |
| 11 | EICOMPAT CAN_FASTPATH_FINALIZE (attr 11, SVAL) | sm_120 = **0x50**; sm_110 & sm_121 = **0x00** (read straight from `.nv.compat` bytes) |
| 12 | symtab count (vadd kernel) | sm_110 = 12; sm_120 = sm_121 = 10 |
| 13 | usched stall (control-word bit≈105) | sm_120 differs from sm_100/103/110 on `I2FP`/`FSETP` decoupled bands (sm_100 ≡ sm_103 ≡ sm_110) |
| 14 | scheduler generation | sm_110 = **gen9** (Thor); sm_100/103/120/121 = **gen8** |

## Correctness matrix (Blackwell arch × decoded table)

V = verified against ground truth; V* = verified, version-pinned to 13.0.88 (see notes);
N = note/caveat (not an error); D = discrepancy (flag to owner); — = arch-independent / n/a.

| decoded table | owner | sm_100 | sm_103 | sm_110 | sm_120 | sm_121 |
|---|---|---|---|---|---|---|
| **ptxas-targets** `sass_elf_eflags.tsv` | targets | V | V | V | V | V |
| **ptxas-targets** `sm_target_properties.tsv` (`__CUDA_ARCH__`) | targets | V | V | V | V | V |
| **ptxas-targets** `sm_version_codes.tsv` (legacy ends sm_90f) | targets | — | — | — | — | — |
| **ptxas-targets** `sm_scheduling_seeds.tsv` (gen8/gen9) | targets | V | V | V (gen9) | V | V |
| **ptxas-targets** `sm_id_enumeration.tsv` (sm_101 slot) | targets | V | V | V | V | V (N: latent sm_101 row) |
| **ptxas-elf-output** `header_notes_compat.tsv` (e_flags) | elf-output | V | V | V | V | V |
| **ptxas-elf-output** `per_arch_sm110_120_121.tsv` e_flags / GB10B / fastpath / symtab | elf-output | n/a | n/a | V | V | V |
| **ptxas-elf-output** `per_arch_sm110_120_121.tsv` **cuinfo virtualSM = 110/120/121** | elf-output | — | — | **D** | **D** | **D** |
| **ptxas-elf-output** "SASS sm_110==sm_120==sm_121" general claim | elf-output | — | — | **D (scoping)** | **D (scoping)** | V |
| **ptxas-encoding-full** `per_arch_encoder_blocks.tsv` | encoding | V | V | V | V | V |
| **ptxas-encoding-full** `sass_class_presence_by_arch.tsv` | encoding | V | V | V | V | V (col absent) |
| **ptxas-encoding-full** `per_arch_encoding_opbyte.tsv` | encoding | V* | V* | V* | V* | V* |
| **ptxas-isel** `per_arch_opcode_histogram.tsv` | isel | V* | V* | V* | V* (N) | V* (N) |
| **ptxas-isel** README move/ALU idiom table | isel | V | V | V | V | V |
| **ptxas-sched-full** dep-rules / scoreboard / latency (identical_to) | sched | V | V | V | V | V |
| **ptxas-sched-full** `blackwell_consumer_stall_deltas.tsv` | sched | V (dir) | V (dir) | V (dir) | V (dir) | V (dir) |
| **ptxas-sched-full** `sm_coverage_summary.tsv` (Thor codegen distinct) | sched | V | V | V | V | V |
| **ptxas-regalloc** `per_arch_regalloc_binding.tsv` (255 budget) | regalloc | V | V | V (alias) | V | V |
| **ptxas-regalloc** `fp64_throughput_class.tsv` (0x10 vs 0x50 gap) | regalloc | V | V | V | V | V |
| **ptxas-mercury** `arch_compat_capbits.tsv` | mercury | C | C | C | C | C |
| **nvdisasm-sass-isa** `sass_isa_SM*.txt` (SM120≡SM121) | (n/a, data dir) | V | V | V | V | V |

"V (dir)" = direction/pattern verified (sm_120 ≠ sm_100/103/110, sm_110 groups with datacenter)
but absolute stall enum uses a different normalization than my raw control-word read.
"C" = structurally consistent but behaviorally unverifiable (cap-bits are internal
`sub_60F290` jump-table outputs, not serialized in any cubin).

## Discrepancies (flagged to owning directory)

### D1 — elf-output: cuinfo.virtualSM is NOT per-arch (MEDIUM) — owner: **ptxas-elf-output**

`per_arch_sm110_120_121.tsv` row `.note.nv.cuinfo virtualSM` claims
`110 / 120 / 121` "u16 in cuinfo desc; tracks -arch SM". **Ground truth: cuinfo.virtualSM
= 100 for every Blackwell arch**, on BOTH 13.0.88 and 13.1.115 (parsed straight from the
`.nv.cuinfo` note descriptor: `noteVer=2, virtualSM=100, cudaApi=130`). The virtual-SM
field is the *compute-capability family base* (compute_100), constant across the lineup —
`cuobjdump -elf` independently confirms "CUDA Virtual SM: sm_100" for all five. The per-arch
distinction lives in the e_flags real-SM byte (0x64/0x67/0x6e/0x78/0x79) and the cap-bits,
**not** in cuinfo. Fix: change that row to `100 / 100 / 100` and the note to
"= family virtual SM (compute_100); does NOT track -arch SM".

### D2 — elf-output: "virtual SM = e_flags bits[16:23]" mislabel (LOW) — owner: **ptxas-elf-output**

`per_arch_sm110_120_121.tsv` and `header_notes_compat.tsv` label e_flags **bits[16:23]**
as `EF_CUDA_VIRTUAL_SM` ("0 when virtual==real"). Those bits are **always 0** for these
kernels regardless of virtual/real match. The byte that `cuobjdump` actually reads as the
virtual-SM family marker is **bits[24:31] = 0x06** (constant = compute_100 family). The
targets `sass_elf_eflags.tsv` captures this correctly as `ef_cuda_virtual_sm = 0x0600`
(bits[31:16]). Recommend elf-output adopt the targets framing: bits[31:24]=0x06 = virtual
family; bits[23:16]=0 = (unused/zero here), not "virtual SM".

### D3 — elf-output: "sm_110 == sm_120 == sm_121 byte-for-byte" is kernel-scoped, not general (LOW) — owner: **ptxas-elf-output**

`per_arch_sm110_120_121.tsv` "SASS encoding (sample kernel)" row states
`sm_110==sm_120==sm_121 byte-for-byte in .text`. This holds for the **specific simple
sample (vadd)** on 13.0.88 — verified. It does **not** generalize: on the `arith`/isel
kernels (even on 13.0.88) sm_110 emits `IADD3`/`VIADD`-free-but-IMAD.MOV codegen while
sm_120 uses the consumer `IADD`/plain-`MOV` idiom, so `.text` differs. On 13.1.115 even the
simple kernel diverges (sm_120/121 switched to the consumer idiom). The byte-identity is
real but must be scoped to "the sample kernel under 13.0.88", not stated as a general
property. (sm_120 ≡ sm_121 IS general and holds in all tests.)

## Version-skew notes (NOT errors — tables are correct for their pinned 13.0.88)

### N1 — isel histogram IADD3 count (sm_120/121): 34 (13.0.88) → 1 (13.1.115)

`ptxas-isel/per_arch_opcode_histogram.tsv` lists sm_120/121 `IADD3 = 34`. This reproduces
**exactly** on ptxas 13.0.88 (the source binary). On 13.1.115 it collapses to `IADD3 = 1`
with `IADD` taking over — consumer Blackwell's integer-ALU idiom evolved between toolkit
releases (MOV stays 55; the histogram is otherwise stable). The decoded value is correct
for 13.0.88; downstream prose should pin "13.0.88" when quoting it.

### N2 — cudaApi / Mercury patch are toolkit-keyed

`.note.nv.cuinfo` cudaApi = 130 on 13.0.88, 131 on 13.1.115 (elf-output's "130" is right
for 13.0.88). The `.nv.capmerc` Mercury-ISA-patch trailer also differs by family
(sm_100/103 tail `…5006`, sm_110/120/121 tail `…5005`) — orthogonal to the
`MERCURY_ISA_MAJOR_MINOR = 0x0101` claim, which holds.

## Cross-domain consistency verdict

1. **Instruction-class set — CONSISTENT.** `nvdisasm-sass-isa` ↔ `ptxas-encoding-full`
   (`sass_class_presence_by_arch.tsv`) agree exactly: SM120-only classes (`qmma_`,
   `omma_scale_`, `mxqmma_scale_`, `ttugo_`, `ttuld_*`) and datacenter-only classes
   (`ldtm_`/`sttm_`/`utc*` TMEM, `genmetadata_`, `credux_`) match between the two domains and
   against my direct grep of the SASS-ISA dumps. The apparent "SM100 qmma=0 but 8 hits"
   is correctly disambiguated: SM100's hits are `utcmxqmma_*` (TMEM datacenter tensor),
   distinct from SM120's standalone consumer `qmma_`. `ptxas-sched-full` shares the same
   Blackwell class universe.

2. **Version codes / e_flags — CONSISTENT (one mislabel).** `ptxas-targets`
   (`sass_elf_eflags`, `sm_target_properties`), `ptxas-elf-output`
   (`header_notes_compat`), and `ptxas-mercury` agree on the e_flags real-SM bytes, the
   `0x06`-family virtual marker, and `__CUDA_ARCH__` codes (nvcc-verified). The only
   inconsistency is the elf-output **cuinfo.virtualSM** field (D1) and the **bits[16:23]**
   virtual-SM label (D2). The phantom-arch story (sm_101/104/107/130 internal-only) is
   consistent across targets / regalloc / mercury.

3. **FP64 / regalloc — CONSISTENT.** `ptxas-regalloc/fp64_throughput_class.tsv`
   (sm_100 FAST 0x10 gap; sm_103/110/120/121 RATE-LIMITED 0x50 gap) is confirmed
   byte-for-byte by my dependent-DFMA address-spacing measurement (16 B vs 80 B). The
   `ptxas-sched-full` model (sm_100 distinct table; sm_103/110/120/121 share the sm_103
   Blackwell tables) and the regalloc binding (all Blackwell share regclass table
   0x21FB680, mode_flag=2, 255 budget) tell the same story and agree with the 255-register
   probe.

4. **Two "gen-9" labels collide (terminology — flag, not a contradiction).** MEDIUM clarity
   issue spanning **ptxas-gap-closure** ↔ **ptxas-targets**: gap-closure's F5 model calls all
   Blackwell "gen-9" (the legacy `0x9xxx` version-hash-code family ordinal: var0=sm_100,
   var1=sm_101/110, var3=sm_103, var4=sm_120, var5=sm_121), while targets'
   `sm_scheduling_seeds` calls only sm_110 "gen9" (the *scheduler* generation; the rest are
   gen8). These are orthogonal axes that happen to share the word "gen-9", and their variant
   ordinals also differ (gap-closure var4=sm_120 vs targets seed var7=sm_120). Neither is
   wrong; a cross-reading is confusing. Recommend each side label its axis explicitly
   ("version-code-family gen" vs "scheduler gen").

## Owner map for flagged items

| item | severity | owning dir |
|---|---|---|
| D1 cuinfo.virtualSM = 100 (not per-arch) | MEDIUM | ptxas-elf-output |
| D2 e_flags bits[16:23] mislabel as virtual-SM | LOW | ptxas-elf-output |
| D3 "sm_110==sm_120 byte-for-byte" needs kernel/version scoping | LOW | ptxas-elf-output |
| N1 isel IADD3=34 pin to 13.0.88 | INFO | ptxas-isel |
| N2 cudaApi/Mercury-patch are toolkit-keyed | INFO | ptxas-elf-output |
| "gen-9" terminology collision | MEDIUM (clarity) | ptxas-gap-closure + ptxas-targets |
