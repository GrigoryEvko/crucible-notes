# ptxas instruction-selection tables

Facts recovered by static + behavioural analysis of the freely-distributed
`ptxas` (CUDA **13.0.88**, `V13.0.88`,
sha256 `daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2`) plus
its companion `nvdisasm`. Reverse engineering of a publicly distributed binary
for interoperability / research; DMCA 17 U.S.C. § 1201(f), *Sega v. Accolade*,
EU 2009/24/EC. Only our own tools and uncopyrightable factual data are
published — no NVIDIA source, no verbatim NVIDIA data tables.

## Files

### Static dispatch structure (in-binary, opcode-axis)
- `opcode_to_encoding.tsv` — the ptxas opcode → encoding-slot map. Column
  `sm_gen` is the **generation in which each opcode was introduced** (sm_70,
  sm_73, sm_82, sm_86, sm_89, sm_90 …), i.e. the opcode-registry growth axis,
  not a per-target dispatch. `encoding_slot` is the slot index into the encoder
  (sentinel `355` = EXTENDED handler).
- `isel_dispatch_groups.tsv` — the 144 ISel dispatch groups (start_slot, size,
  first/last target VA) — the grouping the selector walks.
- `isel_operand_constraint_records.tsv` — operand-type-signature records
  (per-node type-id list + constraint flags) consumed by ISel legality.
- `isel_node_vtables.tsv` — the polymorphic ISel-node method/query/operand
  vtable pools (FAM0..FAM3 + COORD).

### Per-arch instruction selection (behavioural, target-axis) — NEW
The opcode registry is largely target-invariant; the *selection* of which SASS
class lowers a given PTX op is **target-dependent**, and that dependence is what
distinguishes datacenter Blackwell (sm_100/103), Jetson Thor (sm_110) and
consumer Blackwell (sm_120/121). These two files capture the selection facts
by driving the real ptxas + nvdisasm on a fixed probe (see the extractor):

- `per_arch_opcode_histogram.tsv` — per-target count of each SASS mnemonic
  emitted for one fixed PTX program (sm_90a, sm_100, sm_103, sm_110, sm_120,
  sm_121).
- `per_arch_encoding_opbyte.tsv` — the same, keyed by the SASS **primary
  opcode byte** (low byte of the 128-bit encoding word) rather than mnemonic.
- `extract_per_arch_isel.py` — our extractor. Self-contained (embeds the probe
  PTX); regenerate with
  `python3 extract_per_arch_isel.py <ptxas> <nvdisasm> [outdir]`.

### Async / TMA / cluster / tcgen05 SASS opcode map — NEW
- `tma_cluster_async_opcodes.tsv` — the PTX → SASS lowering for the
  memory-ordering, async-copy, bulk/TMA, cluster/DSMEM, multicast, warpgroup-MMA
  and Blackwell tcgen05 instruction families that the bulk-async PTX exposes but
  which were missing from `opcode_to_encoding.tsv`. Eight uniform columns:
  `ptx_op`, `sass_opcode`, `op_byte` (low byte of the 128-bit word),
  `intro_sm`, `addr_form`, `operand_fields`, `completion`, `notes`. Every row was
  produced by assembling a one-variable probe kernel with ptxas (`sm_80` for
  `cp.async`/`LDGSTS`, `sm_90a` for TMA/cluster/wgmma, `sm_100a` for tcgen05) and
  reading the emitted machine code with `cuobjdump -sass` + `nvdisasm`. Notable
  recovered facts: `cp.async.bulk` → `UBLKCP.S.G`/`UBLKCP.G.S` (op `0xba`);
  tensor TMA → `UTMALDG`/`UTMASTG`/`UTMAREDG`/`UTMAPF` (`0xb4`–`0xb8`);
  `mbarrier.*` → the `SYNCS.{EXCH,ARRIVE,PHASECHK,CCTL}.TRANS64` family
  (`0xa7`/`0xb1`/`0xb2`); `fence.proxy.tensormap` → `UTMACCTL.IV` (`0xb9`);
  `barrier.cluster.{arrive,wait}` → `UCGABAR_{ARV,WAIT}` (`0xc7`, full words
  `0x79c7`/`0x7dc7`); `tcgen05.mma` → `UTCHMMA`/`UTCIMMA` (`0xea`, with `.2CTA` for
  the paired-issue group) over the explicit `gdesc[UR]`/`tmem[UR]`/`idesc[UR]`
  operand classes; `tcgen05.ld/st` → `LDTM`/`STTM` (`0xee`/`0xed`);
  `tcgen05.alloc/dealloc` → `UTCATOMSWS` (`0xe3`). Consumer Blackwell
  (`sm_120a`/`121a`) rejects all `tcgen05.*` — confirming TMEM is datacenter-only.

## Per-arch instruction-selection differences (the headline result)

For one identical PTX program, ptxas selects measurably different SASS per
target. Equivalence classes (verified at the SASS-text and 128-bit-encoding
level): `sm_100 ≡ sm_100a ≡ sm_100f`, `sm_103 ≡ sm_103a ≡ sm_103f`,
`sm_110 ≡ sm_110a ≡ sm_110f`, and `sm_120 ≡ sm_120a ≡ sm_120f ≡ sm_121 ≡
sm_121a ≡ sm_121f` (sm_120 and sm_121 are byte-identical). So along the
encoding-table axis there are exactly four new Blackwell-generation profiles:
**sm_100, sm_103, sm_110, sm_120(=121)**.

Selection of the **register-move idiom** is the cleanest discriminator:

| target            | move idiom         | integer-add idiom | half-add | imm-materialize | scheduler NOPs |
|-------------------|--------------------|-------------------|----------|-----------------|----------------|
| sm_90a / sm_100   | `IMAD.MOV.U32 …,RZ,RZ,x` | `VIADD` + `IADD3` | `HADD2`  | no `HFMA2`      | tight (≈13)    |
| sm_103            | `IMAD.MOV.U32`     | `VIADD` + `IADD3` | `HADD2`  | no `HFMA2`      | padded (≈150)  |
| sm_110 (Jetson)   | `IMAD.MOV.U32`     | `IADD3` only (no `VIADD`); uses `IMNMX` | `HADD2` | no `HFMA2` | padded (≈147) |
| sm_120 / sm_121   | plain `MOV` / `MOV.64` (no `IMAD.MOV`) | `IADD3` only (`VIMNMX` for min/max) | `HADD2` | `HFMA2 R,-RZ,RZ,0,imm` (4×) | padded (≈152) |

Concretely, for `mov.b32 %r,%r` ptxas emits on sm_100 the integer-pipe idiom
`IMAD.MOV.U32 R,RZ,RZ,src` (SASS opcode byte `0x24`), whereas on sm_120 it
emits the dedicated `MOV` (opcode byte `0x02`). Consumer Blackwell routes
register moves and many integer adds through a real uniform MOV/`IADD3` path
instead of occupying the integer-MAD pipe.

Note on the half path: the **half-add itself (`add.f16`) lowers to `HADD2`**
(`HADD2 R, R.H0_H0, R.H1_H1`) on *every* target including sm_120/121 — verified
by inspecting the emitted SASS, not just the histogram. What differs on
sm_120/121 is the **FP-immediate materialization idiom**: those targets emit 4×
`HFMA2 R, -RZ, RZ, 0, imm` (e.g. to splat a small half/FP constant), whereas
sm_100/103/110 emit zero `HFMA2`. The histogram's `HFMA2 = 4 (sm_120 only)`
and `HADD2 = 1 (all)` rows are correct; an earlier draft of this table mislabeled
the sm_120 *half-add* as `HFMA2`, which the SASS-level diff disproves.
Jetson Thor (sm_110) keeps the `IMAD.MOV` move idiom but drops the
vector-integer `VIADD`/`VIADDMNMX`/`VIMNMX` ops in favour of scalar
`IADD3`/`IMNMX`. The
heavy fixed NOP padding on sm_103/110/120/121 reflects a more conservative
fixed-latency scheduling model than the tight sm_100 schedule.

See `../ptxas-encoding-full/sass_class_presence_by_arch.tsv` for the
corresponding ISA-class coverage deltas (tcgen05/TMEM present on
sm_100/103/110 but absent on sm_120; RT/TTU and consumer tensor classes present
only on sm_120; etc.).

## Verification provenance

Both histogram TSVs were regenerated from scratch with
`extract_per_arch_isel.py <ptxas> <nvdisasm>` (ptxas `V13.0.88`, sha256
`daba837a…`; nvdisasm `V13.1.115`) and reproduce the committed files
byte-for-byte. The per-arch idioms in the table above were then re-confirmed by
inspecting the actual emitted SASS per target (compile → `nvdisasm -c -hex` →
diff mnemonic + low-encoding-byte sets), which is what surfaced the half-add
mislabel: `add.f16` → `HADD2` on all five targets, and the sm_120/121 `HFMA2`
is an FP-immediate splat, not the half-add. The opcode-byte mapping was
verified directly (NOP `0x18`, IMAD.MOV.U32 `0x24`, MOV `0x02`, IADD3 `0x10`).
The `sm_100 ≡ sm_100a/f` / `sm_120 ≡ sm_121` SASS equivalences were confirmed
via byte-level SASS hashes (sm_120 and sm_121 share SHA `714d057def7525c7`).
