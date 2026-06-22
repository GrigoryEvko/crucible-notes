# ptxas full per-SM SASS scheduling model — binary-derived artifacts

Complete per-SM latency / dependency-rule / scoreboard tables and the
dependency/scoreboard hazard model, recovered purely from the `ptxas` binary
(CUDA 13.0.88, sha256 `daba837a…849f2`). No machine-description source is
consulted; the build-time latency DSL (`SM*.latencies_`) is compiled away and
ships in no binary, so everything below is the *shipped* form read from
`.rodata` and the OCG scheduler functions. This page upgrades the prior
"representative" coverage in `decoded/ptxas-scheduling/` to the full set.

## Three table families (all SMs through sm_121)

| Table | Record | Indexed by | Variants | Coverage |
|---|---|---|---|---|
| **Latency / sched-class descriptor** | 72 B | scheduling-class id | 3 (one per SM *family*) | sm_7x=619, sm_8x=256, sm_10x=430 classes |
| **Dependency rule** | 40 B | scheduling-class id | 11 (per individual SM) | 10 distinct sets (sm_86 == sm_90) |
| **Scoreboard config** | 88 B | config index | 7 | config = N `{sb,thr,mask}` rows (`mask` = pipe bitset, not a count); sm_100 ≤6 rows/cfg, sm_103 1 row/cfg with wider masks (same Blackwell schema) |

Latency descriptor tables are **shared per family**: `0x2297C00` (sm_8x, shared
by sm_80/86/89/90/90a), `0x226C880` (sm_10x: sm_100/103 **and sm_110/120/121**),
`0x2245060` (sm_7x: sm_60/70/72/75). Dependency-rule tables are **per-SM** at the
VA ranges in `per_sm_dependency_rules.json`.

### sm_11x / sm_12x (Jetson Thor / consumer Blackwell) — shared, not new

The full Blackwell lineup (sm_110, sm_120, sm_121) ships in this same ptxas and
compiles cleanly, but the sm_11x/sm_12x latency / dependency / scoreboard tables
are **not at new VMAs** — they are the **sm_103 Blackwell tables**, shared:

- An exhaustive `.rodata` scan finds **exactly** 3 latency tables (72 B), 11
  dependency-rule tables (40 B), and 7 scoreboard tables (88 B). There is **no
  12th/13th** of any of them and no dedicated sm_11x/sm_12x byte region — the
  consumer Blackwell and Thor latency tables searched for at "not-yet-located"
  VMAs simply do not exist as separate tables.
- The per-SM table installer (`sub_ABF590`) dispatches on an internal
  family-enum; its **entire Blackwell branch hard-codes only two cases**:
  `0x4000 → lat10x(0x226C880) + dep_sm100(0x2268440) + sb_sm100(0x2266A60)` and
  `0x4001 → lat10x(0x226C880) + dep_sm103(0x2262720) + sb_sm103(0x2261740)`. The
  Blackwell **latency family is a single shared table**; only the dep-rule +
  scoreboard tables differ (sm_100 GB100 vs sm_103 GB300 — 165/430 rows differ).
- **Empirical resolution** (identical PTX compiled with this exact ptxas,
  disassembled with nvdisasm 13.1, reading the per-instruction `usched` stall):
  **sm_120a and sm_121a emit byte-identical SASS**, and their decoupled-op
  stalls match **sm_103** (not sm_100) on the F2I / F2F / DFMA / LOP3 bands → so
  **sm_120 ≡ sm_121, both resolving to the sm_103 (enum 0x4001) tables**. sm_110
  (Jetson Thor, internal generation "Thor") routes through the same Blackwell
  lat10x latency family; its codegen is a genuinely distinct generation (a fixed
  probe expands to 3.4× more SASS), but it introduces no new 72/40/88-B table.

So sm_110/120/121 **share** the sm_103 Blackwell hardware tables. The emitted
`*_sm_110/120/121.tsv` carry the byte-identical sm_103 data with a provenance
header. The residual per-arch scheduling deltas — POPC, I2FP, FSETP, MUFU
collect timing differing between consumer/Thor and datacenter Blackwell — are the
OCG's per-arch adjustments **on top of** the shared table; we measured them by
differential SASS analysis and ship them in `blackwell_consumer_stall_deltas.tsv`
(our own result, not binary bytes).

## Artifacts

| File | Contents |
|---|---|
| `latency_table_sm{7x,8x,10x}.tsv` | full 72-B sched-class descriptors, all classes, all families |
| `latency_table_sm{11x,12x}.tsv` | sm_110 / sm_120-121 72-B descriptors = byte-identical to `sm10x` (shared Blackwell family) |
| `dependency_rules_<sm>.tsv` (×11) | full 40-B dependency rules per SM |
| `dependency_rules_sm_{110,120,121}.tsv` | sm_11x/sm_12x 40-B rules = byte-identical to `sm_103` (shared); sm_120 == sm_121 |
| `dependency_rules_all.tsv` | long-form union (4616 rows), `sm` column prepended |
| `scoreboard_configs_<sm>.tsv` (×7) | flattened scoreboard `(id, threshold, mask)` triplets |
| `scoreboard_configs_sm_{110,120,121}.tsv` | sm_11x/sm_12x 88-B configs = byte-identical to `sm_103` (shared) |
| `blackwell_consumer_stall_deltas.tsv` | **our** differential-analysis result: consumer/Thor-vs-datacenter Blackwell per-op stall deltas (POPC, I2FP, FSETP, MUFU, …) |
| `opcode_pipeline_map.tsv` | Ori opcode → pipeline-flags (sm_7x, sm_10x) |
| `scalar_latency_oracle.tsv` | per-Ori-opcode latency band — **binary-corrected** (see below) |
| `sm_coverage_summary.tsv` | per-SM coverage: entry count, family, disabled units, identity (now through sm_121) |
| `render_sched_full.py` | reproducible renderer (reads the extracted JSON) |
| `extract_blackwell_consumer.py` | re-reads the sm_103 Blackwell tables from `.rodata` and emits the sm_110/120/121 TSVs with provenance |

## Field semantics

### Latency / sched-class descriptor (72 B)
`class_id`(u32) · `reserved`(u32) · `pipeA`(8 B) · `pipeB`(8 B) · `p0..p11`(12×u32).
- `pipeA` = per-pipe eligibility byte vector (`0xFF` = pipe N/A); `pipeB` =
  dual-issue eligibility vector.
- `p1` = throughput class (sm_8x: `{0,1,2,4,132}`; sm_10x/7x add `{64,68,84,132,…}`).
- `p5` = max-stall cycles `{0..7}` (dominated by 3 and 7).
- `p7` = the class id itself (self-reference; equals `class_id` in **every** record, all 3 families — verified).
- `p11` = always 0 (all families). `p0` is a packed flag word (high bit set in a few rows). `p2,p3,p4,p6,p8,p9,p10` are small enums emitted as observed columns; exact semantics not asserted.

### Dependency rule (40 B) — verified byte-exact against the binary
10 × i32: `unit_id` · `rule_type` · `latency` · `throughput_inv` ·
`barrier_latency` · `barrier_throughput` · `read_latency` · `write_latency` ·
`stall_cycles` · `issue_slots`.
- `rule_type`: **4 = disabled / unit-absent** (always paired with `latency=255`);
  `0/1/2` = active. (Confirmed: every `rule_type==4` row has `latency==255`, and
  no active row does.)
- `latency` = producer→consumer pipeline latency; `255` is the not-present sentinel.
- `throughput_inv` = inverse throughput (issue interval).
- `barrier_latency` / `barrier_throughput` = dependency-barrier wait/interval used
  for decoupled (scoreboard-tracked) ops. `barrier_throughput == -1` = none.
- `read_latency` / `write_latency` = explicit RAW / WAW operand-latency overrides;
  `-1` (0xFFFFFFFF) = "unset, use `latency`". These are populated for a minority of
  units (e.g. sm_70 read_latency set on 190 classes) and are the binary's explicit
  per-class hazard cells.
- `stall_cycles` = static stall hint; `issue_slots` = dual-issue slot count.

### Scoreboard config (88 B) — verified byte-exact
7 triplets `{scoreboard_id, threshold, mask}` (3×i32 each) + `count`(i32 at +84).
- `mask == -1` (0xFFFFFFFF) = all-lanes / unconditional wait; small masks
  (`2,4,8`) = pipe-specific. `threshold` is the barrier-count threshold (56 dominant).
- sm_100 uses up to **6** scoreboards per config (the Hopper/Blackwell async
  dependency-barrier model); sm_80/86/89/90/90a/103 use a single triplet with a
  pipe mask.

## Binary correction (oracle band 13)

The prior `decoded/ptxas-scheduling/scalar_latency_oracle.txt` listed band-13 as
`{38, 60, 61, 63, 68, 78, 79, 106, 162, 180, 182, 192, 194, 199, 215, 221}`.
The actual `sub_738E20` switch (`result = opcode_index − 16`; cases
`22,43,44,46,51,62,63,90,146,164,166,176,178,183,199,205`) yields band-13 =
**`{38, 59, 60, 62, 67, 78, 79, 106, 162, 180, 182, 192, 194, 199, 215, 221}`**.
Three entries were off by one (`61,63,68` → `59,62,67`). Bands 300/24/30 match.
`scalar_latency_oracle.tsv` carries the corrected set. (Mnemonics are
best-effort: the oracle indexes the sm_9x/Blackwell Ori opcode space, not the
sm_70 opcode_master used for naming — keep the numeric Ori id authoritative.)

## The hazard model (recovered from the OCG scheduler functions)

```
Per-instruction latency query        sub_8BF3A0 (oracle+744)
Long-latency predicate (> 19 cyc)    sub_8CCF80
Memory-space classifier              sub_693BC0
Resource / scoreboard occupancy      sub_A08A00  (3 modes: 1=issue, 2=commit, 3=revert)
Per-operand register cost            sub_A08910
Stall-cycle accumulation             sub_A09530  (node+12, 9-bit & 0x1FF)
Oracle constructor (bands)           sub_738E20
HW-profile / table builder           sub_8E5CA0
Warp/dispatch profile (smem→params)  sub_8E4400
Cutlass barrier override (FNV-1a)    sub_939370
```

- **Latency lookup** (`sub_8BF3A0`): for a node, if its operand flags
  (`*(op+108) & 5`) mark it a special form, return `oracle+92` (the default seed
  `{300,0,0,0}`); else if `*(op+104)` is non-zero use it directly; else index the
  flat scalar oracle `oracle[744 + 4·(OriOpcode & 0xFFFFCFFF)]`. This flat array
  is the per-opcode latency band table built by `sub_738E20`.
- **RAW handling**: producer latency comes from the dependency-rule `latency`
  (or the per-class `read_latency` override when set ≠ −1); the consumer is held
  for that many cycles. `sub_A08A00` mode-1 walks source operands
  (`sub_A08910`), adds each operand's cost (`v77[0]`) into a per-functional-unit
  cumulative array `acc[4·unit_id]`, and sets the operand's register bit in the
  live scoreboard (`sub_BDBB80`).
- **WAW / WAR handling**: destination operands use `write_latency` (override) and
  the same scoreboard bitset; mode-3 (revert) clears bits via `sub_BDBC70`. The
  read/write split is exactly the `read_latency` / `write_latency` columns.
- **Barrier (scoreboard) assignment**: decoupled ops (oracle property bit `0x02`)
  get a scoreboard wait seed of **5** (`oracle+2212[op]=5`, set in `sub_738E20`).
  The per-SM scoreboard-config table supplies the `(scoreboard_id, threshold,
  mask)` triplets that the control-word generator (`scoreboards.md` Phase 116 /
  `sub_A36360`) installs; `barrier_latency`/`barrier_throughput` from the
  dependency rule set the wait depth.
- **Long-latency / scheduling priority**: `sub_8CCF80` returns true when the
  queried latency `> 19`, which gates whether an op is treated as latency-hiding
  material (memory/MMA) vs short ALU. Threshold 19 is binary-fixed.
- **Cutlass tuning**: `sub_939370` FNV-1a-hashes the basic-block id (seed
  `0x811C9DC5`, prime `16777619`) against a per-kernel barrier table; a hit
  returns a packed `(stall_target, register_limit)` overriding default barrier
  insertion around MMA groups; a miss returns sentinel `0x7FFFFFFF00000000`.

## Opcode → pipeline → latency band (end-to-end)

```
OriOpcode  --(opcode_pipeline_map.tsv)-->  pipeline_flags {0..4}
OriOpcode  --(sub_89FBA0 SetOpcodeLatencies)-->  scheduling class_id (2..771)
class_id   --(latency_table_sm{fam}.tsv)-->  pipe-eligibility + throughput class + max-stall
class_id   --(dependency_rules_<sm>.tsv)-->  latency / tput_inv / barrier / RAW(read) / WAW(write)
OriOpcode  --(scalar_latency_oracle.tsv via sub_738E20)-->  flat latency band {6,13,24,30,300}
decoupled? --(oracle bit 0x02)-->  scoreboard wait seed = 5 + per-SM scoreboard config triplets
```
The flat scalar oracle is the fast per-opcode path the OCG consults directly;
the per-class dependency-rule table is the richer producer/consumer model keyed
by the scheduling class that `sub_89FBA0` assigns. They agree on anchors
(ALU=6, long-mem=300) but the 13/24/30 oracle bands are OCG-internal scalars and
need not equal any single dependency-rule cell.

## Per-SM coverage and divergence (highlights)

- 3 latency tables cover the whole SM lineup by family; 10 distinct
  dependency-rule sets cover 11 individual SMs (`sm_86` and `sm_90` are
  **byte-identical**). sm_110/120/121 add no new tables — they share the sm_103
  Blackwell set (sm_120 ≡ sm_121).
- `sm_90a` enables every class (0 disabled); `sm_90` disables exactly 6 — units
  `{41, 561, 562, 563, 566, 567}` (the WGMMA / async-MMA classes; 564/565 do not
  exist) — which is the sole sm_90 vs sm_90a difference.
- Disabled-unit counts grow for restricted/older arches: sm_103=129, sm_75=173,
  sm_72=170, sm_70=146, sm_60=136, sm_80=19, sm_100=10. sm_110/120/121 = 129
  (inherited from sm_103). See `sm_coverage_summary.tsv`.

### Consumer / Thor vs datacenter Blackwell (binary-derived + differential)

All five Blackwell arches share the lat10x latency descriptors and the sm_103
dependency-rule + scoreboard tables; the **coupled-math** stalls
(FADD/FFMA/FMUL/IMAD/IADD3/SHF, LDG/STG) are identical across all of them. The
**decoupled** bands carry the only divergence (measured per-instruction from
emitted SASS, `blackwell_consumer_stall_deltas.tsv`):

| op | sm_100 (GB100) | sm_103 (GB300) | sm_120/121 (consumer) | sm_110 (Thor) |
|---|---|---|---|---|
| POPC | 8 | 5 | **6** | 5 |
| I2FP | 7 | 7 | **4** | **4** |
| FSETP | 13 | 2 | **1** | 2 |
| F2I | 8 | 1 | 1 | 1 |
| F2F / DFMA / LOP3 | (low) | 15/15/6 | 15/15/6 | 15/15/6 |

sm_120/121 track sm_103 on F2I/F2F/DFMA/LOP3 (hence "resolves to sm_103") but
shorten the I2FP convert and the FSETP predicate latch and add one cycle to the
POPC collect — the consumer-Blackwell SFU/conversion datapath. sm_110 (Thor)
also tracks sm_103 but adopts the consumer-short I2FP. sm_100 (GB100) is the
outlier with long F2I/FSETP latches. These deltas are OCG per-arch adjustments
layered on the shared sm_103 table (no separate table exists in the binary).

## Confidence

| Claim | Confidence | Basis |
|---|---|---|
| 72/40/88-B record layouts | **High** | byte-exact re-read of `.rodata` vs JSON |
| dependency-rule field names | **High** | values + scheduler-function usage |
| `rule_type 4 = disabled`, `latency 255 = absent` | **High** | perfect correlation across all SMs |
| scoreboard triplet `{id,threshold,mask}` + count@+84 | **High** | byte-exact |
| oracle band-13 correction | **High** | verbatim `sub_738E20` switch cases |
| `p7=self`, `p11=0` | **High** | holds for every record, all families |
| sm_11x/sm_12x have **no** new latency/dep/scoreboard table | **High** | exhaustive `.rodata` scan (0 extra ≥200-entry 72-B/40-B tables) + installer `sub_ABF590` Blackwell branch hard-codes only 0x4000/0x4001 |
| sm_120 ≡ sm_121, both → sm_103 tables | **High** | byte-identical SASS; decoupled stalls track sm_103 (F2I/F2F/DFMA/LOP3) not sm_100 |
| sm_110 (Thor) → Blackwell lat10x family | **High** | empirical SASS shares lat10x coupled timing; gen "Thor" codegen distinct |
| consumer/Thor stall deltas (POPC/I2FP/FSETP) | **Medium** | differential SASS measurement (our result), one probe family |
| latency-descriptor `p0,p2,p3,p4,p6,p8,p9,p10` semantics | **Medium/Low** | columns emitted; not individually named |
| oracle mnemonics | **Low** | Ori-opcode↔name space mismatch (numeric id authoritative) |

## Wiki outline (representative → full)

**`scheduling/latency-model.md`**
- Replace "Representative Per-SM Latency Values" with the **three full tables**
  (sm_7x/8x/10x) — link the TSVs; note the per-family sharing (3 tables, 26 SMs).
- Add the **dependency-rule** section: 40-B layout, all 11 SMs, the
  RAW(`read_latency`)/WAW(`write_latency`) override columns, `rule_type 4 =
  disabled`, `latency 255 = absent`. Link `dependency_rules_all.tsv`.
- Correct the band-13 oracle list (`59,62,67`, not `61,63,68`); cite the
  `sub_738E20` switch. Keep the 5-band scalar oracle as the fast path.

**`scheduling/scoreboards.md`**
- Under "Hardware Dependency Barrier Model" add the **per-SM scoreboard-config
  tables** (7 SMs): triplet `{scoreboard_id, threshold, mask}`, `mask=-1`
  unconditional vs pipe-masked, sm_100 ≤6 scoreboards vs single-triplet
  pre-Blackwell. Cross-link `sub_939370` (Cutlass FNV-1a override) and the
  `barrier_latency`/`barrier_throughput` dependency-rule columns.

**`scheduling/overview.md`**
- In "Hardware Latency Profiles" replace the representative table with the
  full-family note + TSV links; add the end-to-end opcode→pipeline→class→band map
  and the `sm_90` vs `sm_90a` (WGMMA enable) divergence as a worked example.

## Reproduce
```
# sm_7x / sm_8x / sm_10x tables + dep rules + scoreboards (reshapes extracted JSON):
python3 render_sched_full.py /path/to/ptxas/extracted

# sm_11x / sm_12x (Thor / consumer Blackwell) — re-reads the sm_103 Blackwell
# bytes straight from your own ptxas .rodata and emits the shared TSVs:
objcopy -O binary --only-section=.rodata "$(command -v ptxas)" ptxas_rodata.bin
python3 extract_blackwell_consumer.py ptxas_rodata.bin
```
The extracted JSON is itself reproducible from your own CUDA 13.0 `ptxas`
`.rodata` at the VAs in `manifest.json`; no NVIDIA bytes ship in this repo. The
consumer/Thor stall deltas in `blackwell_consumer_stall_deltas.tsv` are
regenerated by compiling a mixed-op probe with your own ptxas and reading the
`usched` stall field via `decoded/sass-tools/sass_ctrl_decode.py`.
