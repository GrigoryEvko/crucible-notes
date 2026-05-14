# NVPTX Subtarget and Feature Matrix

## Abstract

Two stock LLVM subtarget tables identify an SM target: one lists every
accepted CPU string, one lists every individual feature bit. Each CPU row
carries a feature mask describing what that CPU implies. The runtime
`NVPTXSubtarget` copies the selected CPU mask, ORs in explicit `-mattr`
features, and answers `hasFeature(idx)` queries from the final bitset.

The reimplementation contract is four-fold: keep the 40 CPU strings sorted
lexicographically so `std::lower_bound` works; keep the 81 feature indices
stable so bit positions do not drift; use one generic scheduling model for
every CPU; and expose the `tmem` feature as the gate for tensor-memory and
`tcgen05` paths.

## The Two TableGen Tables

`NVPTXSubtarget` is built from three arrays: the CPU table, the feature table, and
a parallel CPU-name `StringRef` array used for early validation of `-mcpu`.

```c
struct SubtargetSubTypeKV {
    const char *cpu_key;
    uint64_t implies[6];
    uint64_t tune_implies[6];
    const MCSchedModel *sched_model;
};

struct SubtargetFeatureKV {
    const char *key;
    const char *description;
    uint64_t value;
    uint64_t implies[6];
};
```

Both tables are sorted: CPU rows by ASCII-lexicographic compare of the `CPUKey` string, feature rows by `Key`. That makes `std::lower_bound` against either array the canonical lookup path at runtime. Lexicographic CPU order produces one initially confusing artifact: `"sm_100" < "sm_100a" < "sm_100f" < "sm_101" < ... < "sm_121f" < "sm_20" < "sm_21" < ... < "sm_90" < "sm_90a"`. The Blackwell `sm_1NN` family appears before the legacy `sm_2N`/`sm_3N`/.../`sm_9N` family for the simple reason that `'1' < '2'` in ASCII; rows are sorted by string, not by silicon generation.

The third array mirrors the CPU table as `(pointer, length)` pairs. Its only
job is early `-mcpu` validation before a full subtarget object exists.

## The 81 Feature Indices

The full feature index table follows. Indices are stable across builds — a TableGen renumber would change PTX bit positions and break every cubin produced against this drop. Each row's `Implies` bitset is zero, so the only way a CPU acquires a feature bit is through the SubTypeKV row's `Implies` mask.

```text
idx  Feature             Description
  0  fma-level=0         FP fused-multiply-add fusion disabled
  1  fma-level=1         FMA fusion for FP32 only
  2  fma-level=2         FMA fusion everywhere (cicc default)
  3  ptx32               Use PTX version 32
  4  ptx40               Use PTX version 40
  5  ptx41               Use PTX version 41
  6  ptx42               Use PTX version 42
  7  ptx43               Use PTX version 43
  8  ptx50               Use PTX version 50
  9  ptx60               Use PTX version 60
 10  ptx61               Use PTX version 61
 11  ptx62               Use PTX version 62
 12  ptx63               Use PTX version 63
 13  ptx64               Use PTX version 64
 14  ptx65               Use PTX version 65
 15  ptx70               Use PTX version 70
 16  ptx71               Use PTX version 71
 17  ptx72               Use PTX version 72
 18  ptx73               Use PTX version 73
 19  ptx74               Use PTX version 74
 20  ptx75               Use PTX version 75
 21  ptx76               Use PTX version 76
 22  ptx77               Use PTX version 77
 23  ptx78               Use PTX version 78
 24  ptx80               Use PTX version 80
 25  ptx81               Use PTX version 81
 26  ptx82               Use PTX version 82
 27  ptx83               Use PTX version 83
 28  ptx84               Use PTX version 84
 29  ptx85               Use PTX version 85
 30  ptx86               Use PTX version 86
 31  ptx87               Use PTX version 87
 32  ptx88               Use PTX version 88
 33  prec-divf32=0       See definition in NVPTXISelLowering.cpp
 34  prec-divf32=1       See definition in NVPTXISelLowering.cpp
 35  prec-divf32=2       See definition in NVPTXISelLowering.cpp
 36  prec-divf32=3       See definition in NVPTXISelLowering.cpp
 37  prec-sqrtf32=0      See definition in NVPTXISelLowering.cpp
 38  prec-sqrtf32=1      See definition in NVPTXISelLowering.cpp
 39  sm_20               Target SM 20
 40  sm_21               Target SM 21
 41  sm_30               Target SM 30
 42  sm_32               Target SM 32
 43  sm_35               Target SM 35
 44  sm_37               Target SM 37
 45  sm_50               Target SM 50
 46  sm_52               Target SM 52
 47  sm_53               Target SM 53
 48  sm_60               Target SM 60
 49  sm_61               Target SM 61
 50  sm_62               Target SM 62
 51  sm_70               Target SM 70
 52  sm_72               Target SM 72
 53  sm_73               Target SM 73
 54  sm_75               Target SM 75
 55  sm_80               Target SM 80
 56  sm_82               Target SM 82
 57  sm_86               Target SM 86
 58  sm_89               Target SM 89
 59  sm_90               Target SM 90
 60  sm_90a              Accelerated Target SM 90
 61  sm_100              Target SM 100
 62  sm_100a             Accelerated Target SM 100
 63  sm_100f             Family Conditional Target SM 100
 64  sm_101              Target SM 101
 65  sm_101a             Accelerated Target SM 101
 66  sm_101f             Family Conditional Target SM 101
 67  sm_103              Target SM 103
 68  sm_103a             Accelerated Target SM 103
 69  sm_103f             Family Conditional Target SM 103
 70  sm_110              Target SM 110
 71  sm_110a             Accelerated Target SM 110
 72  sm_110f             Family Conditional Target SM 110
 73  sm_120              Target SM 120
 74  sm_120a             Accelerated Target SM 120
 75  sm_120f             Family Conditional Target SM 120
 76  sm_121              Target SM 121
 77  sm_121a             Accelerated Target SM 121
 78  sm_121f             Family Conditional Target SM 121
 79  sharedmem32bitptr   Use 32 bit ptrs for Shared Memory
 80  tmem                Has support for Tensor Memory
```

Indices 0..38 cluster the orthogonal compiler-knob features: three FMA-fusion levels, thirty PTX-version selectors, four FP32-division precision settings, and two FP32-sqrt precision settings. The driver layer (cicc / nvcc) sets these through `-mattr=+ptxNN` and `-mattr=+fma-level=N` flags alongside `-mcpu=sm_NN`; tileiras itself never propagates a PTX-version bit from any CPU row. Indices 39..78 cover the 40 SM-target feature bits, one per CPU row, in lexicographic CPU order. Index 79 is the Fermi-legacy `sharedmem32bitptr` toggle. Index 80 is the only NVIDIA-proprietary feature in the entire build: `tmem`, "Has support for Tensor Memory", absent from upstream LLVM 18.1.4 / 19 NVPTX, and the cross-feature implication that distinguishes datacenter Blackwell from consumer Blackwell.

The PTX-version selector range stops at `ptx88` — three versions past upstream LLVM 19 (capped at `ptx86`) and six past LLVM 18.1.4 (capped at `ptx82`). `ptx88` aligns with the CUDA 13.1 toolchain vintage that produced this binary.

## The 40 CPU Rows

The 40 CPU rows appear below in lexicographic table order, grouped by
silicon generation for readability. Each entry lists the row index, the
feature bit for the CPU itself, the known ELF target byte when the cubin
reader recognizes one, and whether the CPU implies `tmem`.

```text
Row  CPU       FeatKV  ELF byte    TMem  Variant   Family
---  --------  ------  ----------  ----  --------  -------------------------------------------
 18  sm_20      39     0x14        no    base      Fermi GF1xx
 19  sm_21      40     0x15        no    base      Fermi GF11x
 20  sm_30      41     0x1E        no    base      Kepler GK10x
 21  sm_32      42     0x20        no    base      Kepler (Tegra K1 / Logan)
 22  sm_35      43     0x23        no    base      Kepler GK110 / GK11x
 23  sm_37      44     0x25        no    base      Kepler GK210
 24  sm_50      45     0x32        no    base      Maxwell GM10x
 25  sm_52      46     0x34        no    base      Maxwell GM20x  -- DEFAULT CPU
 26  sm_53      47     0x35        no    base      Maxwell (Tegra X1 / Erista)
 27  sm_60      48     0x3C        no    base      Pascal GP100 (datacenter)
 28  sm_61      49     0x3D        no    base      Pascal GP10x (consumer)
 29  sm_62      50     0x3E        no    base      Pascal Tegra X2 / Parker / Drive-PX2
 30  sm_70      51     0x46        no    base      Volta GV100
 31  sm_72      52     0x48        no    base      Volta (Xavier)
 32  sm_73      53     (gap)       no    base      placeholder; no known HW product
 33  sm_75      54     0x4B        no    base      Turing TU10x
 34  sm_80      55     0x50        no    base      Ampere GA100 (datacenter)
 35  sm_82      56     (gap)       no    base      placeholder; no known HW product
 36  sm_86      57     0x56        no    base      Ampere GA10x (consumer)
 37  sm_89      58     0x59        no    base      Ada Lovelace AD10x
 38  sm_90      59     0x5A        no    base      Hopper GH100
 39  sm_90a     60     0x5A+0x800  no    a         Hopper GH100 + WGMMA/TMA arch-cond
  0  sm_100      61     0x64        no    base      Blackwell datacenter GB100/GB200/B100/B200
  1  sm_100a     62     (gap)       YES   a         Blackwell datacenter + tcgen05 arch-cond
  2  sm_100f     63     (gap)       YES   f         Blackwell datacenter + tcgen05 family-cond
  3  sm_101      64     (gap)       no    base      Blackwell datacenter (reserved variant)
  4  sm_101a     65     (gap)       YES   a         Blackwell datacenter + tcgen05 arch-cond
  5  sm_101f     66     (gap)       YES   f         Blackwell datacenter + tcgen05 family-cond
  6  sm_103      67     (gap)       no    base      Blackwell Ultra GB300 (datacenter)
  7  sm_103a     68     (gap)       YES   a         Blackwell Ultra GB300 + tcgen05 arch-cond
  8  sm_103f     69     (gap)       YES   f         Blackwell Ultra GB300 + tcgen05 family-cond
  9  sm_110      70     (gap)       no    base      Jetson Thor (embedded Blackwell-class)
 10  sm_110a     71     (gap)       YES   a         Jetson Thor + tcgen05 arch-cond
 11  sm_110f     72     (gap)       YES   f         Jetson Thor + tcgen05 family-cond
 12  sm_120      73     0x78        no    base      Blackwell consumer RTX 50** / Pro enterprise
 13  sm_120a     74     (gap)       NO    a         Consumer RTX 50** arch-cond (no tcgen05)
 14  sm_120f     75     (gap)       NO    f         Consumer RTX 50** family-cond (no tcgen05)
 15  sm_121      76     (gap)       no    base      DGX Spark / B40-class
 16  sm_121a     77     (gap)       NO    a         DGX Spark arch-cond (no tcgen05)
 17  sm_121f     78     (gap)       NO    f         DGX Spark family-cond (no tcgen05)
```

Two architecturally important findings live in this table.

The first is that exactly eight CPUs imply `tmem`, and they are exactly the datacenter Blackwell and Jetson Thor arch-conditional and family-conditional variants: `sm_100a`, `sm_100f`, `sm_101a`, `sm_101f`, `sm_103a`, `sm_103f`, `sm_110a`, `sm_110f`. Their `Implies` bitsets each carry two bits — the self-bit plus bit 80 — while every other CPU row has only its single self-bit. Tensor Memory and the `tcgen05.mma` instruction family it gates are physically datacenter-only in NVIDIA's silicon planning. The base CPUs `sm_100` / `sm_101` / `sm_103` / `sm_110` deliberately omit the bit so that plain `.target sm_100` PTX cannot reach tcgen05; the programmer must opt into `.target sm_100a` or `.target sm_100f`.

The second is that consumer Blackwell (`sm_120` and variants) and DGX Spark (`sm_121` and variants) never imply `tmem`, even in their arch-conditional or family-conditional forms. This is not build drift — `sm_121a` is alphabetically reachable through `std::lower_bound`, so a missing bit is a deliberate choice. Physical silicon for consumer Blackwell and Spark lacks Tensor Memory; consumer Blackwell gets `mma.sync.aligned.*.block_scale` as a weaker substitute, dispatched through a separate two-opcode MachineInstr path (5468 dense, 5469 sparse) rather than through TMEM-resident tcgen05 atoms.

Hopper's `sm_90a` is the only arch-conditional CPU that does not imply `tmem`.
Tensor Memory was introduced with Blackwell; Hopper's arch-conditional surface
covers WGMMA, TMA, and `setmaxnreg` instead. The plain `sm_100` row also lacks
`tmem`, so programmers must opt into `sm_100a` or `sm_100f` to reach tensor
memory.

Two CPU rows, `sm_73` and `sm_82`, behave like compatibility placeholders
with no known physical silicon. Conversely, the cubin reader recognizes
`sm_87`, but this subtarget table has no `sm_87` CPU row. A correct
reimplementation wires CPU selection and cubin classification symmetrically
so any recognized target is also selectable by `-mcpu`.

## Runtime Feature State

The runtime subtarget stores the target triple, selected CPU, feature string,
references to the CPU and feature tables, a generic scheduling model, the
populated feature bitset, and parsed numeric SM/PTX versions. Only this compact
state is needed for codegen legality checks.

```c
struct NvptxFeatureState {
    string triple;
    string cpu;
    string tune_cpu;
    string feature_string;

    ArrayRef<SubtargetSubTypeKV> cpu_rows;
    ArrayRef<SubtargetFeatureKV> feature_rows;
    ArrayRef<StringRef> cpu_names;

    const MCSchedModel *sched_model;
    uint64_t feature_bits[6];

    uint32_t sm_version_times_ten;
    uint32_t ptx_version_times_ten;
    uint32_t sm_minor;
};
```

`feature_bits` is the runtime bitset that `hasFeature` queries. It starts
empty; the selected CPU row's `implies` mask gets ORed in, then any
explicit `-mattr=+feature` flags. Masks and runtime bitset share the same
six-word shape, so population is a word-wise OR, not a per-bit loop.

```c
static bool nvptx_has_feature(const NVPTXSubtarget *st, unsigned idx) {
    return (st->FeatureBits[idx >> 6] >> (idx & 63)) & 1;
}

/* Concrete probes for the four interesting bits: */
/*   HasSM90   = hasFeature(59)  = (FeatureBits[0] >> 59) & 1                                */
/*   HasSM100  = hasFeature(61)  = (FeatureBits[0] >> 61) & 1                                */
/*   HasSM100a = hasFeature(62)  = (FeatureBits[0] >> 62) & 1                                */
/*   HasTMem   = hasFeature(80)  = (FeatureBits[1] >> 16) & 1   -- the lone NVIDIA-only bit  */
```

Every `TuneImplies` slot in every SubTypeKV row is zero. Upstream LLVM uses this field to separate tuning advice (latency model, scheduling hints) from architectural feature implication; the NVPTX fork in this build leaves it empty. A faithful reimplementation leaves the `TuneFeatures = [...]` clause off the TableGen records. Every `Implies` slot in every FeatureKV row is zero too — features never transitively pull in other features in this binary. CPU rows carry the full implied set directly.

## Cached Tensor-Memory Predicate

Hot instruction-selection paths use a cached boolean equivalent to
`hasFeature(80)`. Semantically this is `has_tmem`: the target supports Tensor
Memory and can select `tcgen05` instructions. The cache is an optimization.
Correctness still comes from the feature bitset.

```c
static bool nvptx_has_feature(const NvptxFeatureState *state, unsigned idx) {
    return (state->feature_bits[idx >> 6] >> (idx & 63)) & 1;
}

static bool nvptx_has_tmem(const NvptxFeatureState *state) {
    return nvptx_has_feature(state, 80);
}
```

Reimplementations may cache `has_tmem` after CPU/feature parsing, but the cached
value must be derived from the same feature bitset that services normal
`hasFeature` queries.

## Lookup at Runtime

The full `-mcpu` resolution path takes the user-supplied CPU string, runs
`std::lower_bound` against the alphabetically sorted CPU table, and on an
exact hit ORs that CPU row's `implies` mask into `feature_bits`. On a miss
Tileiras falls back to `sm_52`. Any `-mattr=+feature` flags parsed in the
same call set their respective bits directly, bypassing the CPU table.

After CPU parsing, `SMVersionTimesTen` derives from the numeric part of the
CPU name. `sm_90a` records 90, not 901, because the suffix is a variant
marker rather than a new major version. `PTXVersionTimesTen` only populates
when a `+ptxNN` feature is supplied.

```c
void parse_nvptx_subtarget(NvptxFeatureState *state, string cpu, FeatureList attrs) {
    const SubtargetSubTypeKV *row = lower_bound_cpu(state->cpu_rows, cpu);
    if (row == NULL) {
        row = lower_bound_cpu(state->cpu_rows, "sm_52");
    }

    or_feature_bits(state->feature_bits, row->implies);

    for (FeatureAttr attr : attrs) {
        set_feature_by_name(state, attr.name, attr.enabled);
    }

    state->sm_version_times_ten = parse_sm_major_times_ten(row->cpu_key);
    state->ptx_version_times_ten = parse_ptx_version(attrs);
    state->has_tmem_cache = nvptx_has_tmem(state);
}
```

## Cross-References

[Per-SM Emission Templates — Capability Matrix](per-sm-emission-templates.md#capability-matrix) walks the PTX templates
each CPU's implied feature set unlocks, including the consumer-Blackwell
[`mma.sync.aligned.*.block_scale`](per-sm-emission-templates.md#sm120--sm121) substitute used when `sm_120` and `sm_121` lack
`tmem`. [NVPTX Bring-up — Target Registration Chain](nvptx-bring-up-and-target-init.md#target-registration-chain) covers
the surrounding target setup. [tcgen05 / WGMMA / mbarrier / Cluster Emission](tcgen05-wgmma-mbarrier-cluster.md)
covers the codegen paths guarded by the tensor-memory predicate.
