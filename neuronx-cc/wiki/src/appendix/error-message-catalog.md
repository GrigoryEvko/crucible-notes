# Appendix — Error-Message Catalog

> *Every code, count, string, and address on this page applies to `neuronx_cc 2.24.5133.0+58f8de22` (cp310 wheel; the catalog is byte-identical across cp310/cp311/cp312 — md5 `2c25078c8065b0381143fabf7aff60be` over the sorted `(cat,idx,cause,res)` tuples). Other versions will differ.*

## Abstract

This appendix is the generated reference dump behind the diagnostics narrative pages ([§3.20](../frontend/diagnostic-error-catalog.md), [§6.4.5](../nki/diagnostic-catalog.md)). Those pages explain *how* neuronx-cc's four diagnostic systems are wired; this page is the **lookup table** — given an error code or message fragment, find its cause, its resolution, and which subsystem emits it. It reproduces three distinct catalogs in full:

1. The **`ErrorMessages.so` flat dict** — `NEURON_ASSERT_ERROR_MESSAGES: Dict[(category:str, index:int), (cause:str, resolution:str)]`, the backend BIR / walrus / codegen / scheduler / NKI / ISA message bodies. **2556 entries** across **353 categories**, dumped verbatim by importing the shipped `.so`.
2. The **224 native `NCC_<CAT><idx>` codes** baked per-call-site into the HLO/penguin frontend binaries (`hlo2penguin`, `hlo-opt`, `hlo-neff-wrapper`, `xla_infergoldens`) — a *separate* catalog whose code→text binding lives in those binaries' own rodata.
3. The **NKI `err_*` funnel** — 78 `err_*` diagnostic leaves + 24 `assert_*` + 32 `check_*` validators in `nki/.../sema.so`, the front-end semantic-analysis layer.

> **NOTE —** the central artifact here is the `ErrorMessages.so` dict, and it is not reverse-engineered text. The `.so` imports standalone — its only dependency is `typing` — so the live Python object was dumped directly rather than reconstructed from `strings`. Every `(category,index)→(cause,resolution)` row in [§4](#4-errormessagesso--the-full-2556-entry-catalog) is the exact runtime value.

### How to use this page

| You have… | Look in… |
|---|---|
| A `[NCC_<CAT><idx>]` token from a **backend** failure (BIR/walrus/codegen/ISA/scheduler) | [§4](#4-errormessagesso--the-full-2556-entry-catalog) — find category `<CAT>`, then index `<idx>` |
| A `[NCC_<CAT><idx>]` from a **frontend** failure (`EVRF`/`PYP`/`ISPP`/`MOD`/`HPM`/`OPT`/…) | [§3](#3-the-224-native-ncc_-codes-frontend-binaries) — these are *not* in the `ErrorMessages` dict |
| A `[F134]`/`[F137]`/`[F139]` fatal | [§2](#2-driver-fatal-exit-codes) |
| An NKI tracing error (`err_*`, raised from `nl.*`/`nisa.*`) | [§5](#5-nki-err_-funnel-sema) |
| A free-form `error:`/`warning:` (MLIR/LLVM) | not a closed catalog — see [§3.20 §6](../frontend/diagnostic-error-catalog.md) |

| | |
|---|---|
| **`ErrorMessages.so` entries** | 2556 `(cat,idx)→(cause,res)` pairs |
| **Categories** | 353 (252 `I…` internal · 24 `E…` external · 77 bare/legacy) |
| **Bespoke resolutions** | 461; the other 2095 are `{RESOLUTION_CONTACT_SUPPORT}` only |
| **Generated `901`/`902` family** | 446 entries (217 passes × 2 wrappers + extras) |
| **OBSOLETE codes** | 4 — `BIR199`, `BIR202`, `BIR203`, `BIR204` |
| **Native `NCC_*` codes** | 224 (separate catalog, frontend binaries) |
| **NKI `err_*` / `assert_*` / `check_*`** | 78 / 24 / 32 (+1 `warn_*`) |
| **Cross-ABI** | md5 `2c25078c8065b0381143fabf7aff60be` (cp310 = cp311 = cp312) |

---

## 1. Count Reconciliation — the binary says 2556

The dict holds **2556** entries: `import neuronxcc.logging.ErrorMessages; len(NEURON_ASSERT_ERROR_MESSAGES)` returns `2556`.

> **GOTCHA — a `strings` sweep of `ErrorMessages.so` undercounts by ~4×.** Extracting the catalog with `strings` / `grep -a` yields roughly 600 cause/resolution pairs, because the string section carries the interned blob and the category tokens but *not* the `(cat,idx)→text` adjacency — that binding lives in a frozen Cython constant-tuple's pointer layout. Two things account for the gap: the 446-entry generated `901`/`902` per-pass family, and the many distinct `(cat,idx)` keys that share an identical cause string (five `BIR0xx` codes all say `Requested Argument index {index} out of bounds`), which `strings` deduplicates and the dict does not. Import the module; do not scrape it.

Cross-checks on the dump: `2556` `CAUSE:` lines and `353` `---- … (N codes) ----` headers, with the per-header counts summing to `2556` and no mismatch. First-letter histogram of the category headers: `I=252 E=24 X=15 L=12 B=9 S=8 M=5 N=4 D=4 C=4 T=3 P=3 A=3 O=2 J=2 V=1 R=1 G=1`. Bespoke (non-`{RESOLUTION_CONTACT_SUPPORT}`) resolutions: `461`.

### Container shape & render path

```text
NEURON_ASSERT_ERROR_MESSAGES : Dict[(category:str, index:int), (cause:str, resolution:str)]
NEURON_ASSERT_GLOBAL_KEYWORDS: {'RESOLUTION_CONTACT_SUPPORT': "Please open a support ticket at
   https://github.com/aws-neuron/aws-neuron-sdk/issues/new. You may also be able to obtain more
   information using the 'XLA_IR_DEBUG' and 'XLA_HLO_DEBUG' environment variables."}

  logging/__init__.so  ──  register_namespace("neuronxcc", NEURON_ASSERT_ERROR_MESSAGES)   (one call)
                                │
  neuron_assert(category, index, condition, **kwargs)        ── Assert.so / neuronxlogger.error
                                │ if not condition:
  cause, res = namespaces["neuronxcc"][(category, index)]     ── the (cat,idx) lookup
                                │
  "[{INTERNAL_ERROR|ERROR}] [NCC_<CAT><idx:03>] {cause.format(**kw)} - {res.format(**kw, RESOLUTION_CONTACT_SUPPORT=…)}"
```

> **QUIRK —** the `[INTERNAL_ERROR]` vs `[ERROR]` prefix is **not** chosen by parsing the category's first letter. It is chosen by *which assert function the caller invokes* — `neuron_internal_assert` emits `[INTERNAL_ERROR]`, `neuron_external_assert` emits `[ERROR]`. The `I…`/`E…` category naming is a strong convention that co-varies with the prefix (I-cats are raised internally, E-cats externally), but no code branches on `CAT[0]`. A reimplementer that switches on the first letter will be *usually* right and *occasionally* wrong — drive off the assert function, not the name. *Anchor: `neuronxlogger/error.py`.*

> **NOTE —** the lone "namespace" string registered with `neuronxlogger` is the literal `"neuronxcc"`; all 353 categories are an *internal sub-key* of the one flat dict, **not** 353 separate `register_namespace` calls. The per-error "namespace" is the category embedded in the key tuple.

### The generated 901/902 per-pass family

446 of the 2556 entries are a templated per-compiler-pass catch-all pair. For a pass class `<PassName>` with category abbreviation `<CAT>`:

```text
(<CAT>, 901) = ("<PassName> assertion error: {baseMessage}", "{RESOLUTION_CONTACT_SUPPORT}")
(<CAT>, 902) = ("<PassName> error: {baseMessage}",           "{RESOLUTION_CONTACT_SUPPORT}")
```

`901` = a failed C++ assert inside the pass (`{baseMessage}` = the assert text); `902` = a non-assert pass error (`{baseMessage}` = the thrown message). 217 categories carry *exactly* `{901, 902}` and nothing else (pure per-pass wrappers); others add `901`/`902` on top of hand-authored low-index codes. In the catalog below these appear as ordinary rows — the `<PassName>` is read directly from the `901`/`902` cause text.

---

## 2. Driver Fatal Exit Codes

The only three top-level `[F1xx]` codes. Emitted by `driver/CommandDriver.so` `handleError` after the subcommand child process exits abnormally. The codes are the POSIX `128 + signal` exit codes of the killed child; the signal identity is **[INFERRED]** from the code numbers and the OOM wording.

| Code | Message body | Trigger | Severity |
|---|---|---|---|
| `[F134]` | `neuronx-cc terminated abnormally - Please open a support ticket at https://github.com/aws-neuron/aws-neuron-sdk/issues/new` | child exit 134 = 128 + 6 (SIGABRT — failed C++ assert / `std::abort` / uncaught native exception) | FATAL |
| `[F137]` | `neuronx-cc was forcibly killed - This most commonly occurs due to insufficient system memory. Using a smaller data type, dimensions, batch size, or a larger instance type may help.` | child exit 137 = 128 + 9 (SIGKILL — overwhelmingly the Linux OOM-killer) | FATAL |
| `[F139]` | `neuronx-cc terminated abnormally - Please open a support ticket at https://github.com/aws-neuron/aws-neuron-sdk/issues/new` | child exit 139 = 128 + 11 (SIGSEGV — segfault) | FATAL |

`grep -a '\[F[0-9]{2,3}\]'` over the entire package returns exactly these three, all in `CommandDriver.so` — a closed set. A structured `[NCC_…]` line is typically printed by the failing subprocess *before* it exits, so a user often sees both an `[NCC_…]` line and a trailing `[F1xx]`.

---

## 3. The 224 Native `NCC_*` Codes (Frontend Binaries)

These are a **separate** catalog from the `ErrorMessages.so` dict in [§4](#4-errormessagesso--the-full-2556-entry-catalog). The fully-rendered token `NCC_<CAT><idx>` is baked per-call-site into the rodata of `hlo2penguin` / `hlo-opt` / `hlo-neff-wrapper` / `xla_infergoldens`; the cause/resolution body for these frontend categories is **not** in `NEURON_ASSERT_ERROR_MESSAGES` (the dict holds the backend bodies). The two catalogs overlap only at `IIOT`, which appears in the dict solely as the generated `(IIOT,901)`/`(IIOT,902)` pair — established by category presence and absence in the dumped dict.

> **GOTCHA —** do not look up `NCC_EVRF001`, `NCC_PYP001`, `NCC_ISPP001`, `NCC_MOD001`, `NCC_HPM001`, `NCC_OPT001`, `NCC_INL001`, `NCC_INFG001`, `NCC_IHCA001`, `NCC_ITUP001`, or `NCC_IHNW001` in [§4](#4-errormessagesso--the-full-2556-entry-catalog) — those frontend categories have **zero** entries in the dict. Their text lives in the native HLO binaries' rodata and is out of scope for the runtime dump. The `ErrorMessages` dict is the *backend* (BIR/walrus/codegen) catalog.

### 3a. Code format & severity convention

```text
[<prefix>] [NCC_<CATEGORY><III>] <cause> - <resolution>
   <prefix>   = "[INTERNAL_ERROR]"  (I-categories)  |  "[ERROR]"  (E-categories)
   <CATEGORY> = 2–5 uppercase letters; first letter convention: I…=INTERNAL, E…=EXTERNAL
   <III>      = 3-digit zero-padded index  (ErrorCode.__str__ = f"{category}{index:03}")
```

Index sets are **sparse** (gaps = retired/obsolete codes); the catalog ranges are enumerated-with-gaps, not contiguous.

### 3b. Native category → emitting binary

| Binary | Categories present |
|---|---|
| `hlo-opt` | `CTR EUDT EUOC HPM IARE INL ITUP MOD OPT` |
| `hlo2penguin` | `CTR DRV ECFT EHCA EMOD ESFH ESPP ETUP EUET EUOC EVRF HPM IARC IARE IHCA IMOD IMUT INL ISPP ITUP IVRF MOD NCO PYP SPP` |
| `hlo-neff-wrapper` | `IHNW IIOT` |
| `xla_infergoldens` | `CTR EUDT HPM INFG ITUP` |
| `libwalrus.so` | internal `NCC_I…` walrus asserts (bodies in §4 via the linked `logging::NeuronAssertion`) |

### 3c. Native code families (category decode)

| Category | Decode | Index range | Component | Severity |
|---|---|---|---|---|
| `OPT` | opt-driver / HLO optimization driver | `001-003` | `hlo-opt` | ERROR(ext) |
| `HPM` | Hlo-Pass-Metadata / pass-manager invariant | `001-003` | all native | INTERNAL |
| `EVRF` / `IVRF` | HLO/penguin IR **VeRiFier** | `EVRF 001-058` · `IVRF 003/015/100` | `hlo2penguin` | ext / int |
| `PYP` | **PY**thon **P**ass (pybind HLO→penguin frontend) | `001-058` (no 002) | `hlo2penguin` | INTERNAL |
| `ISPP` / `ESPP` / `SPP` | **S**etup-**P**enguin-**P**ass / penguin pipeline | `ISPP 001-061` · `ESPP 003-059` | `hlo2penguin` | int / ext |
| `MOD` / `IMOD` / `EMOD` | (modular) **Mod**ule compilation | `MOD 001-016` · `IMOD 020` · `EMOD 018-019` | `hlo2penguin`/`hlo-opt` | mixed |
| `IHCA` / `EHCA` | **H**lo-**C**ustom-call **A**ttr handling | `IHCA 001-004` · `EHCA 005` | `hlo2penguin` | int / ext |
| `ITUP` / `ETUP` | **TUP**le flatten/handling | `ITUP 001` · `ETUP 002` | `hlo-opt`/`hlo2penguin`/`xla_infergoldens` | int / ext |
| `INL` | **IN****L**ine pass | `001-002` | `hlo-opt`/`hlo2penguin` | INTERNAL |
| `INFG` | **INF**er-**G**oldens | `001-002` | `xla_infergoldens` | INTERNAL |
| `IHNW` | **H**lo-**N**eff-**W**rapper | `001-003` | `hlo-neff-wrapper` | INTERNAL |
| `IIOT` | **I**o-transp**O**se **T**ranspose | `001-002` | `hlo-neff-wrapper` | INTERNAL |
| `EUDT` / `EUOC` / `EUET` | **U**nsupported-**D**type / **O**p / … | `EUDT 001` · `EUOC 001-002` · `EUET 001` | `hlo-opt`/`hlo2penguin`/`xla_infergoldens` | ERROR(ext) |
| `ECFT` | Custom-call / Cost **F**a**T**al | `001-003` | `hlo2penguin` | ERROR(ext) |
| `CTR` | (compiler) **C**on**TR**ol / context | common infra | all native | mixed |

### 3d. The handful with verbatim bodies (frontend rodata)

| Code | Message / trigger | Component | Severity |
|---|---|---|---|
| `NCC_OPT001-003` | `No valid Hlo passes specified, exiting...` / `AddPass cannot be called after Run` / `Error: Required pass not found! Possible causes: …` (exact code↔message binding not individually pinned) | `hlo-opt` (offsets `0x28ae5`/`0x30844`/`0x2c6f5`) | ERROR(ext) |
| `NCC_HPM001-003` | `HloPassMetadata for currently running pass not found, either because the pass did not call RecordPassStart or because a pass is creating/switching modules without using HloModuleGroup::ReplaceModule.` | `hlo-opt` + all native | INTERNAL |
| `NCC_IIOT001` | `Duplicate io_transpose found for input: <name>` (trigger: `combineIoTransposes` `0x1e50760` dup-check) | `hlo-neff-wrapper` | INTERNAL |
| `NCC_IIOT002` | `Forbidden io_transpose found for input: <name>` (trigger: io_transpose on an `IntermediateIOs` cross-partition tensor) | `hlo-neff-wrapper` | INTERNAL |
| `NCC_IHNW003` | `Reshape size mismatch for input: <name>` (trigger: `processParameters` `0x1e5b1d0` — declared reshape ≠ actual param shape) | `hlo-neff-wrapper` | INTERNAL |

> **NOTE —** the `n001`/`n002`/`n003` "numbered" codes attributed to `StaticIOTranspose` (missing-netlist / dup-forbidden / reshape-mismatch) are that module's *ad-hoc* numbered diagnostics — **not** `NCC_` codes, and distinct from `NCC_IIOT`/`NCC_IHNW`. See [§3.20 §6b/§6d](../frontend/diagnostic-error-catalog.md). Whether the CC-ops legalizers emit `n00x` codes at all is **[UNRESOLVED]** — no such emission was found.

---

## 4. `ErrorMessages.so` — the Full 2556-Entry Catalog

The complete verbatim runtime dump of `NEURON_ASSERT_ERROR_MESSAGES` (cp310; byte-identical cp311/cp312), grouped by category in ascending index. The rendered code token is `NCC_<Code>` (render prefix and `[INTERNAL_ERROR]`/`[ERROR]` bracket per [§1](#1-count-reconciliation--the-binary-says-2556)). Resolution column: **`*(contact)*`** abbreviates the auto-appended `{RESOLUTION_CONTACT_SUPPORT}` support sentence (the 2095 entries that carry only it); a verbatim string means a *bespoke* resolution (the 461 hand-authored ones). `{kwarg}` placeholders are runtime-substituted `str.format` slots. Every row is verbatim from the imported `.so`.

> **NOTE —** this is the complete table: all 2556 entries across all 353 categories are reproduced below, none dropped. The `(category, index)` key in each subsection heading plus row is the exact dict key; the cause/resolution cells are the exact tuple value. A reader can look up any backend `NCC_<CAT><idx>` code by jumping to category `<CAT>` and finding index `<idx>`.

### 4a. Category index (jump table)

| Severity class | Categories |
|---|---|
| **INTERNAL** (`I…`, 252) | `IACF IACI IACT IADE IADI IADQ IADR IADV IAGO IAHE IALB IALS IANK IANS IAPR IATL IATN IBCC IBCG IBFC IBIR IBRD IBRW IBSN IBVF ICCC ICCF ICDG ICDL ICIO ICIR ICMA ICMC ICNE ICOE IDCE IDDI IDDT IDEC IDEL IDFS IDFV IDGE IDGM IDIE IDLI IDLO IDMA IDML IDMP IDNT IDSE IDSH IDST IDTP IDVR IEAD IEBN IEDV IEID IEIL IEIM IFAT IFBD IFFD IFGC IFML IFSG IFTA IGAS IGCA IGLO IHAG IHFC IICB IICR IIFG IIGL IIIC IIIN IIIT IIIV IILT IINK IINL IINT IIOT IIPS IISA IISM IIST ILBR ILCB ILCC ILCM ILCX ILDM ILFD ILFU ILIN ILIS ILKK ILLI ILLO ILLP ILLR ILLT ILMM ILNI ILNK ILNM ILOA ILOP ILPP ILPR ILPT ILPX ILRA ILSA ILSC ILSM ILSP ILSR ILSX ILTA ILTO ILTR ILTS ILTY IMCE IMDD IMDT IMGN IMPR IMS IMSA IMSH INBU INCP INDI INDR INDV INEC INIC INIS INKN INLC INLF INLI INPN INSM INSP INST INTC INVN INVR IOAC IOLV IONK IOPF IPAA IPAS IPCC IPDE IPER IPF IPFN IPLF IPLN IPLO IPLX IPMG IPMN IPNI IPNK IPSF IPST IRAC IRCP IRCX IRMT IRNS IROE IROI IRPR IRPX IRRM IRRW IRSA IRSP IRSW ISAB ISAG ISAR ISAU ISCH ISDC ISDD ISFD ISFV ISI ISIM ISIS ISMP ISMT ISMX ISNT ISOE ISPA ISPC ISPR ISPS ISSA ISSD ISSL ISSS ISST ISTA ISTL ISTN ISTP ISUP ISUS ISYC ITCC ITCO ITCT ITDM ITEN ITIN ITLA ITLX ITNM ITOE ITOS ITOT ITPO ITPR ITRF ITSH ITST IUBK IURM IUTL IVDM IVLP IVMM IVNU IWCO IXCG IXLV IZST` |
| **EXTERNAL** (`E…`, 24) | `EAE EARG EBCG EBIR EBRD EBVF EDGE EDMA EDP EDRV EDVR EGCA EIL ELMM ELUR ENKI EOCP EOOM ERP ESL ESMP ETST EXSP EXTP` |
| **bare** (77) | `ADA ALR ALS BFD BHT BIB BIR BIRSERDES BLN BMT BUG BVF CCO CFG CFP CUT DAE DMA DRV DVR GCA JIO JOB LCL LDD LDM LEG LKK LKN LLC LLR LNK LRS LSA LUR MFP MLC MLO MMP MSA NAS NKI NKS NLA OAC OQS PER PGT PRS RDP SCH SCP SDD SFP SIM SIO SQI SSA TCE TEN TST VNS XAQ XBI XCG XDR XEI XGM XLB XLS XLV XNP XRA XRO XTP XVL XXU` |


### 4b. Full catalog (all 2556 entries, by category)

#### ADA — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ADA001` | ADA computation should be called only once per batch. The history should be empty, but not. | *(contact)* |

#### ALR — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ALR001` | Live-in registers are only supported in legacy loops-on-chip flow. Register {reg_name} defined by Instruction {def_name} in BasicBlock {def_block_name} is last used by Instruction {use_name} in BasicBlock {use_block_name} | *(contact)* |

#### ALS — bare (12 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ALS010` | Instruction must exist | *(contact)* |
| `ALS011` | No local semaphore update found in instruction updates | *(contact)* |
| `ALS012` | DMA Block has more than one local semaphore update | *(contact)* |
| `ALS013` | DMA Block was assigned a different local semaphore on reuse after queue switch | *(contact)* |
| `ALS014` | No CoreBarrier that the remote SB-to-SB DMA instruction is dependent on | *(contact)* |
| `ALS015` | No CoreBarrier which is dependent on the given remote SB-to-SB DMA instruction | *(contact)* |
| `ALS016` | Closest CoreBarrier after the given remote SB-to-SB DMA instruction has no dependency relation with it | *(contact)* |
| `ALS017` | Closest CoreBarrier before the given remote SB-to-SB DMA instruction has no dependency relation with it | *(contact)* |
| `ALS018` | Got a pair of DMA transpose {transpose} and remote SB-to-SB DMA {sb2sb} with no CoreBarrier in between | *(contact)* |
| `ALS019` | Got a pair of remote SB-to-SB DMA {sb2sb} and DMA transpose {transpose} with no CoreBarrier in between | *(contact)* |
| `ALS020` | Only Call/SwitchQueueInstance/TensorCompletion instructions are allowed in main. But Instruction '{name}' is of type '{type}' | *(contact)* |
| `ALS021` | Expect InstSwitchQueueInstance/InstCall/InstTensorCompletion in main to not have any dependencies or dependents | *(contact)* |

#### BFD — bare (4 codes)

| Code | Cause | Resolution |
|---|---|---|
| `BFD701` | {file} at {line} Memloc has no writer or reader | *(contact)* |
| `BFD702` | unexpected num of writers | *(contact)* |
| `BFD703` | unexpected instruction type | *(contact)* |
| `BFD704` | expected a loop instruction | *(contact)* |

#### BHT — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `BHT001` | branch {branchName}'s target does not have first instruction | *(contact)* |
| `BHT002` | no scope when attempting to insert branch hint for {branchName} | *(contact)* |

#### BIB — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `BIB001` | Cannot create non-FP32 PSUM tensor {tname} in CoreV2 | *(contact)* |

#### BIR — bare (354 codes)

| Code | Cause | Resolution |
|---|---|---|
| `BIR001` | No hardware model found | *(contact)* |
| `BIR002` | No hardware model found | *(contact)* |
| `BIR003` | Invalid AccessPattern | *(contact)* |
| `BIR004` | Different MemoryLocations for sameAccessPatterns: {memloc} | *(contact)* |
| `BIR005` | Range of toIntervalSet comes from different MemoryLocations | *(contact)* |
| `BIR006` | Requested Argument index {index} out of bounds ({num_args}) | *(contact)* |
| `BIR007` | Requested Argument index {index} out of bounds ({num_args}) | *(contact)* |
| `BIR008` | Requested Output index {index} out of bounds ({num_outputs}) | *(contact)* |
| `BIR009` | Requested Output index {index} out of bounds ({num_outputs}) | *(contact)* |
| `BIR010` | Requested Argument index {index} out of bounds ({num_args}) | *(contact)* |
| `BIR011` | Requested Argument index {index} out of bounds ({num_args}) | *(contact)* |
| `BIR012` | Requested Output index {index} out of bounds ({num_outputs}) | *(contact)* |
| `BIR013` | Requested Output index {index} out of bounds ({num_outputs}) | *(contact)* |
| `BIR014` | Instruction with name {name} not found | *(contact)* |
| `BIR015` | Json field with name {name} not found | *(contact)* |
| `BIR016` | Register with name {name} not found | *(contact)* |
| `BIR017` | Register with name {name} not found | *(contact)* |
| `BIR018` | ctx is nulltpr | *(contact)* |
| `BIR019` | ctx is nulltpr | *(contact)* |
| `BIR020` | ctx is nulltpr | *(contact)* |
| `BIR021` | ctx is nulltpr | *(contact)* |
| `BIR022` | ctx is nulltpr | *(contact)* |
| `BIR023` | Unknown dtype '{dtype}' | *(contact)* |
| `BIR024` | Unknown dtype string '{dtype}' | *(contact)* |
| `BIR025` | Malformed nki_functions json field | *(contact)* |
| `BIR026` | Number of input arguments must be the same as recvFomRank size | *(contact)* |
| `BIR027` | Output dtype must match input | *(contact)* |
| `BIR028` | Invalid compression ratio: {compress_ratio}! Valid compression ratios are 2, 3, or 4. | *(contact)* |
| `BIR029` | Expected first free dimension of MatmultSparse ({ifmap_1_num}) to equal compression ratio ({compress_ratio})! | *(contact)* |
| `BIR030` | DynamicAPINFO::IndirectDimMaxIndex must be > 0 | *(contact)* |
| `BIR031` | For CoreV3+, Matmult in transpose mode must have same input and output dtype (moving input {intype} != output {outtype}) | *(contact)* |
| `BIR032` | For CoreV3+, Matmult in transpose mode must have same input and output dtype (stationary input {intype} != output {outtype}) | *(contact)* |
| `BIR033` | neuron_core_id is not set | *(contact)* |
| `BIR034` | Mixing of 32-bit and non-32-bit Matmult inputs not supported ({intype1}, {intype2}) | *(contact)* |
| `BIR035` | Transpose matmult requires matching input types ({intype1}, {intype2}) | *(contact)* |
| `BIR036` | MemoryLocation with name {name} not found | *(contact)* |
| `BIR037` | Size of input tensors for instruction exceed state buffer partition size {input1} + {input2} > {partitionSize} | *(contact)* |
| `BIR038` | Output size doesn't fit PSUM {outputSize} bytes > {psumSize} bytes | *(contact)* |
| `BIR039` | Matmult moving input tile size must be <= {sbufMaxRows}x{psumMaxCols} but it was = {ifmapRows}x{ifmapCols} | *(contact)* |
| `BIR040` | Matmult stationary input tile size must be <= {sbufMaxRows}x{psumMaxRows} but it was = {weightRows}x{weightCols} | *(contact)* |
| `BIR041` | Matmult output tile size must be <= {psumMaxRows}x{psumMaxCols} But it was = {outputRows}x{outputCols} | *(contact)* |
| `BIR042` | No support for uninitialized read check in loops | *(contact)* |
| `BIR043` | Lowered precise schedule loops only support a loop count of one | *(contact)* |
| `BIR044` | split_BB is deprecated, but got the split_BB attribute on function {function_name} | *(contact)* |
| `BIR045` | Cannot find already deserialized instruction | *(contact)* |
| `BIR046` | 'unroll_dependencies' must be a JSON array | *(contact)* |
| `BIR047` | 'unroll_dependencies' element must be a JSON array | *(contact)* |
| `BIR048` | Cannot find unrolled dependency | *(contact)* |
| `BIR049` | Invalid number of AccessPattern dimensions; must be in [2, 5] | *(contact)* |
| `BIR050` | PE Tile position and size must both be fully specified | *(contact)* |
| `BIR051` | Matmult instruction does not support PE column tile position 96 on CoreV2 | *(contact)* |
| `BIR052` | PE tile position ({row_tile_pos}, {col_tile_pos}) must be >= start partition of access pattern ({ap_row_pos}, {ap_col_pos}) | *(contact)* |
| `BIR053` | PE tile position ({row_tile_pos}, {col_tile_pos}) + accessed partition number ({ap_row_partitions}, {ap_col_partitions}) must be <= PE array size ({pe_row_size}, {pe_col_size}) | *(contact)* |
| `BIR054` | PE tile size ({row_tile_size}, {col_tile_size}) must be >= accessed partition number ({ap_row_partitions}, {ap_col_partitions}) | *(contact)* |
| `BIR055` | Symbolic Matmult second input base partition mismatch across iterations | *(contact)* |
| `BIR056` | Symbolic Matmult output base partition mismatch across iterations | *(contact)* |
| `BIR057` | Matmult output PE tile position cannot be allocated to 96 on CoreV2 | *(contact)* |
| `BIR058` | Matmult instruction does not support PE tile size 32x32 on CoreV2 | *(contact)* |
| `BIR059` | DMACopy does not support {cce_op} in CCE mode in instruction {name} | *(contact)* |
| `BIR060` | DMACopy with SW DGE does not support {cce_op} in CCE mode in instruction {name} | *(contact)* |
| `BIR061` | DMACopy with HW DGE does not support {cce_op} in CCE mode in instruction {name} | *(contact)* |
| `BIR062` | Unexpected DGE mode {dge_type} in DMACopy instruction {name} | *(contact)* |
| `BIR063` | DMADescriptorCCE does not support {op} in instruction {name} | *(contact)* |
| `BIR064` | GenericIndirectSave does not support {op} in instruction {name} | *(contact)* |
| `BIR065` | IndirectSaveAccumulate does not support {op} in instruction {name} | *(contact)* |
| `BIR066` | {kind} CollectiveCompute does not support {op} in instruction {name} | *(contact)* |
| `BIR067` | {kind} CollectiveCompute does not support {op} in instruction {name} | *(contact)* |
| `BIR068` | Unexpected CollectiveCompute kind {kind} in instruction {name} | *(contact)* |
| `BIR069` | Unexpected instruction type {opcode} in check CCE AluType in instruction {name} | *(contact)* |
| `BIR070` | DoWhile instruction missing loop condition | *(contact)* |
| `BIR071` | DoWhile instruction condition must be an IntRuntimeValue | *(contact)* |
| `BIR072` | CompareAndBranch instruction has: {num_args} arguments. Expected 2 arguments. | *(contact)* |
| `BIR073` | CompareAndBranch expects both args to have same dtype. Dtype for arg1: {dtype_1}; Dtype for arg2: {dtype_2} | *(contact)* |
| `BIR074` | {function_name}: Function had already been visited by BIR verifier | *(contact)* |
| `BIR075` | Collective compute instructions are not allowed in DoWhile loops | *(contact)* |
| `BIR076` | `--dge-levels dst_reduce` not enabled, cannot have DGE instruction with a destination reduction operation | *(contact)* |
| `BIR077` | DMACopy does not support {cce_op} with {mode} mode in instruction {name} | *(contact)* |
| `BIR078` | Empty basic blocks are not allowed: {block_name} | *(contact)* |
| `BIR079` | Basic block: {block_name} does not end with Terminator instruction. Last instruction: {inst_name} is not a terminator | *(contact)* |
| `BIR080` | Basic block: {block_name} non-terminator Instruction {inst_name} is not allowed after the first terminator Instruction | *(contact)* |
| `BIR081` | Basic block: {block_name} is a loop block that must contain only sequencer/terminator instructions. Instruction {inst_name} is not a sequencer or terminator instruction | *(contact)* |
| `BIR082` | Control flow graph of function {function_name} has multiple exit nodes: {block_a}, {block_b} | *(contact)* |
| `BIR083` | Control flow graph of function {function_name} does not have an exit node | *(contact)* |
| `BIR084` | Control flow graph of function {function_name} has multiple entry nodes: {block_a}, {block_b} | *(contact)* |
| `BIR085` | Control flow graph of function {function_name} does not have an entry node | *(contact)* |
| `BIR086` | Found duplicate targets for a back edge from {source_block} to {target_block_1} and {target_block_2} in control flow graph of function {function_name} | *(contact)* |
| `BIR087` | Symbolic Access Pattern input arguments are not allowed in DoWhile loops post unroll stage | *(contact)* |
| `BIR088` | Symbolic Access Pattern output arguments are not allowed in DoWhile loops post unroll stage | *(contact)* |
| `BIR089` | Control flow graph basic block count {cfg_block_count} mismatches the function {function_name} it is derived from: {function_block_count} | *(contact)* |
| `BIR090` | Function {function_name} basic blocks not in topological order: {block1} found before predecessor {block2} | *(contact)* |
| `BIR091` | Unknown ArgumentKind | *(contact)* |
| `BIR092` | Live-in/Live-out MemoryLocation: {memloc_name} not allocated to MemoryType: DRAM in function: {func_name} | *(contact)* |
| `BIR093` | InstDoWhile instruction incompatible with legacy loop implementations '--loops-on-chip' and '--loops-in-backend' | *(contact)* |
| `BIR095` | Different function attribute: `stage_unroll_completed` values for function: {func1_name} and function: {func2_name} | *(contact)* |
| `BIR096` | Invalid Module JSON filepath {fp} | *(contact)* |
| `BIR097` | Unreachable block {unreachable_block_name} found in {function_name} | *(contact)* |
| `BIR098` | Unsupported DGE Type: expect one of DGE types {valid_dge_types} but got {target_dge_type} | *(contact)* |
| `BIR099` | Gather instruction expects maximum 4 dimensions in params access pattern but found {dimensions} | *(contact)* |
| `BIR100` | Gather instruction expects maximum 4 dimensions in indices access pattern but found {dimensions} | *(contact)* |
| `BIR101` | Gather instruction expects maximum 4 dimensions in outputs access pattern but found {dimensions} | *(contact)* |
| `BIR102` | Gather instruction expects the same data type for params: {params_dtype} and outputs: {outputs_dtype} | *(contact)* |
| `BIR103` | Gather instruction expects the same number of elements per partition for indices: {indices_per_partition} and outputs: {outputs_per_partition} | *(contact)* |
| `BIR104` | Gather instruction expects the same number of partitions for params: {params_partitions} and indices: {indices_partitions} | *(contact)* |
| `BIR105` | Gather instruction expects the same number of partitions for params: {params_partitions} and outputs: {outputs_partitions} | *(contact)* |
| `BIR106` | Gather instruction expects type uint32 for indices: {indices_dtype} | *(contact)* |
| `BIR107` | Only 1 terminator is allowed on each engine, but engine {engine_name} has 2 | *(contact)* |
| `BIR108` | If there is a terminator on EngineType::ALL/Unassigned, there must not be terminators on any other engine | *(contact)* |
| `BIR109` | If there is not a terminator on EngineType::ALL/Unassigned, there must be a terminator on all {num_datapath_engines} engines, but there are only terminators on {actual} engines | *(contact)* |
| `BIR110` | Matmult cannot write to multiple PSUM banks for architecture {arch} | *(contact)* |
| `BIR111` | Unsupported data type Dtype::{actual} for InstCompareAndBranch 1st argument. The supported types are Dtype::float32/int32/uint32 | *(contact)* |
| `BIR112` | Unsupported data type Dtype::{actual} for InstCompareAndBranch 2nd argument. The supported types are Dtype::float32/int32/uint32 | *(contact)* |
| `BIR113` | SB CollectiveKind {kind} requires non-empty replica groups | *(contact)* |
| `BIR114` | Unhandled data type {dtype} for bir::ImmediateValue::toString | *(contact)* |
| `BIR115` | Unhandled data type {dtype} for bir::ImmediateValue::createFromJson | *(contact)* |
| `BIR116` | Unhandled data type {dtype} for bir::ImmediateValue::dataToJson | *(contact)* |
| `BIR117` | Unhandled data type {dtype} for bir::ImmediateValue::clone | *(contact)* |
| `BIR118` | Unhandled data type {dtype} for bir::SymbolicImmediateValue::createFromJson | *(contact)* |
| `BIR119` | Unhandled data type {dtype} for bir::SymbolicImmediateValue::toJson | *(contact)* |
| `BIR120` | Unhandled data type {dtype} for bir::ImmediateArray::createFromJson | *(contact)* |
| `BIR121` | Unhandled data type {dtype} for bir::ImmediateArray::toJson | *(contact)* |
| `BIR122` | GetSequenceBounds instruction can only be assigned to Pooling engine, but it is assigned to {actual} | *(contact)* |
| `BIR123` | GetSequenceBounds instruction expects source AccessPattern to be three dimension, but is {actual} | *(contact)* |
| `BIR124` | GetSequenceBounds instruction expects destination AccessPattern to be three dimension, but is {actual} | *(contact)* |
| `BIR125` | GetSequenceBounds instruction expects partition dimension of source AccessPatttern to be 1, but it is {actual} | *(contact)* |
| `BIR126` | GetSequenceBounds instruction expects partition dimension of destination AccessPatttern to be 1, but it is {actual} | *(contact)* |
| `BIR127` | GetSequenceBounds instruction expects last dimension of destination AccessPattern to be 2, but it is {actual} | *(contact)* |
| `BIR128` | GetSequenceBounds instruction expects data type of input AccessPattern to be Dtype::int32 or Dtype::float32, but it is {acutal} | RESOLUTION_CONTACT_SUPPORT |
| `BIR129` | GetSequenceBounds instruction expects data type of output AccessPattern to be Dtype::int32 or Dtype::float32, but it is {acutal} | RESOLUTION_CONTACT_SUPPORT |
| `BIR130` | GetSequenceBounds instruction expects destination AccessPattern to be twice the size of source AccessPattern, but source has {source_num} and destination has {dst_num} | RESOLUTION_CONTACT_SUPPORT |
| `BIR131` | Custom operations not yet supported for TRN2 | *(contact)* |
| `BIR132` | Unsupported expression kind ({kind}) in iterator | *(contact)* |
| `BIR133` | ScalarTensorTensor instruction requires 2D or 3D inputs, but received inputs with {pattern1_size} and {pattern2_size} dimensions respectively | *(contact)* |
| `BIR134` | ScalarTensorTensor instruction requires 2D or 3D output, but received output with {output_pattern_size} dimensions | *(contact)* |
| `BIR135` | Lower bound expression for DynamicForLoop is of unexpected kind {kind} | *(contact)* |
| `BIR136` | Lower bound expression for DynamicForLoop expected to be constant | *(contact)* |
| `BIR137` | Upper bound expression for DynamicForLoop is of unexpected kind {kind} | *(contact)* |
| `BIR138` | Upper bound expression for DynamicForLoop expected to contain only one term | *(contact)* |
| `BIR139` | Upper bound expression for DynamicForLoop expected to contain a single integer runtime value | *(contact)* |
| `BIR140` | Stride expression for DynamicForLoop is of unexpected kind {kind} | *(contact)* |
| `BIR141` | Stride expression for DynamicForLoop expected to be constant | *(contact)* |
| `BIR142` | One of InstCompareAndBranch's targets must be the next BasicBlock ({next}), but they are {true} and {false} | *(contact)* |
| `BIR143` | InstTensorLoad/InstTensorSave must have at least 1 input, but it has none | *(contact)* |
| `BIR144` | InstTensorLoad/InstTensorSave must have at least 1 output, but it has none | *(contact)* |
| `BIR145` | InstTensorLoad/InstTensorSave can only reference Const/Internal/Pointer tensors, but it is referencing a {type} tensor | *(contact)* |
| `BIR146` | Attempted to find loop header for {bb} which is not in a loop body | *(contact)* |
| `BIR147` | Attempted to find loopy body for {bb} is not a loop header | *(contact)* |
| `BIR148` | Upper bound expression for DynamicForLoop references undefined register | *(contact)* |
| `BIR149` | DynamicForLoop instruction not in a top-level basic block | *(contact)* |
| `BIR150` | Unknown argument type for transpose matrix multiplication outputs! | *(contact)* |
| `BIR151` | PSUM partition on transpose matrix multiplication outputs must be 0, got partition {partition} instead! | *(contact)* |
| `BIR152` | Array data structure expects even number of elements but got: {nibbleCount} | *(contact)* |
| `BIR153` | Array index out of of bounds: Attempted to fetch nibble at index {index} but array size is {nibbleCount} | *(contact)* |
| `BIR154` | Array index out of of bounds: Attempted to set nibble at index {index} but array size is {nibbleCount} | *(contact)* |
| `BIR155` | Output of xbar transpose must have steps must be 32 byte aligned. Output dimension {dim} has step size of {step} which is not divisible by 32 when multiplied with the dtype size {dtype_size}. | *(contact)* |
| `BIR156` | Tensor size must be < 4GB, but it is {actual} bytes | *(contact)* |
| `BIR157` | {inst_name}: Replication not supported for MatmultMX instructions | *(contact)* |
| `BIR160` | DEPRECATED, see BIR606 -- {inst_name}: MatmultMx is only supported for engine: Tensor. Got engine: {engine} | *(contact)* |
| `BIR161` | DEPRECATED, see BIR530 -- {inst_name}: MatmultMx is only supported for arch: core_v4 or newer. Got older arch: {arch} | *(contact)* |
| `BIR162` | DEPRECATED, see BIR606 -- {inst_name}: QuantizeMx is only supported for engine: Vector. Got engine: {engine} | *(contact)* |
| `BIR163` | DEPRECATED, see BIR530 -- {inst_name}: QuantizeMx is only supported for arch: core_v4 or newer. Got arch: {arch} | *(contact)* |
| `BIR164` | {inst_name}: QuantizeMx has invalid input access pattern: {iap}. Innermost dimension invalid. Step of innermost dimension must be 1 and num of innermost dimension should be a multiple of 4 | *(contact)* |
| `BIR165` | {inst_name}: QuantizeMx has invalid input access pattern: {iap}. Step for all dimensions except innermost dimension should be an even number | *(contact)* |
| `BIR166` | {inst_name}: Input AP and Output AP should access same number of elements. Input AP accesses {numIap} elements and Output AP accesses {numOap} elements | *(contact)* |
| `BIR167` | {inst_name}: Starting quadrants for ifmap: {if_quad} and ifmap_scales: {iscales_quad} dont match | *(contact)* |
| `BIR168` | {inst_name}: Starting quadrants for weights: {w_quad} and weights_scales: {wscales_quad} dont match | *(contact)* |
| `BIR169` | {inst_name}: Start partition for ifmap_scales: {if_start} and weights_scales: {w_start} must be a multiple of 4 | *(contact)* |
| `BIR170` | {inst_name}: Expected num_active_channels: {channels} to be either 32. 64. 96 or 128 | *(contact)* |
| `BIR171` | Unsupported TransposeOps::{actual} for architecture {arch}. Only XZYW is supported. | *(contact)* |
| `BIR172` | Transpose dst must be in SB, but it is in {actual} | *(contact)* |
| `BIR173` | DGE xbar transpose src must be in SB or DRAM, but it is in {actual} | *(contact)* |
| `BIR174` | DGE xbar transpose datatype must be 2 bytes, but it is {type} ({size} bytes) | *(contact)* |
| `BIR175` | DGE xbar transpose src must have 16 partitions, but it has {actual} | *(contact)* |
| `BIR176` | DGE xbar transpose expected src tile to have {expected} columns, but it has {actual} | *(contact)* |
| `BIR177` | DGE xbar transpose src columns must be consecutive. Last dimension step should be 1 but it is {actual} | *(contact)* |
| `BIR178` | DGE xbar transpose dst columns must be consecutive. Last dimension step should be 1 but it is {actual} | *(contact)* |
| `BIR179` | DGE xbar transpose dst rows must be consecutive. Partition dimension step should be {expected} but it is {actual} | *(contact)* |
| `BIR180` | DGE xbar transpose dst tile starting address must be a multiple of 32, but it is {actual} (remainder of {remainder}) | *(contact)* |
| `BIR181` | Unsupported TransposeOps::{actual}. Only XZYW and XYZW are supported. | *(contact)* |
| `BIR182` | Instruction reads un-initialized memory locations: {details} | *(contact)* |
| `BIR183` | Num elements per partition for ifmap {num} should be 4x num elements per partition for ifmap_scales {scales_num} | *(contact)* |
| `BIR184` | Num elements per partition for weights {num} should be 4x num elements per partition for weights_scales {scales_num} | *(contact)* |
| `BIR185` | Start partition of ifmap_scales: {part} should be < 16 | *(contact)* |
| `BIR186` | Start partition of weights_scales: {part} should be < 16 | *(contact)* |
| `BIR187` | Max number of elements per partition for ifmap: {num} should be <= {max_ifmap_elements} ({max_elements_per_psum_bank} * 4(PE quad-row mode) * 8(PSUM banks)) for output dtype {output_dtype_name} | *(contact)* |
| `BIR188` | Max number of elements per partition for weights: {num} should be <= 512 (128 * 4) | *(contact)* |
| `BIR189` | Num active rows for instruction must be 32, 64 or 128. Got: {numRows} | *(contact)* |
| `BIR190` | Number of partitions accessed by ifmap {ifmap} should equal number of partitions accessed by weights {weights} | *(contact)* |
| `BIR191` | Number of elements per partition in weights {weights} should be 4x number of output partitions {op} | *(contact)* |
| `BIR192` | Number of elements per partition in ifmap {ifmap} should equal to 4x number of elements per partition in output {op} | *(contact)* |
| `BIR193` | Partitions accessed by ifmap {ifmap} should match partitions accessed by corresponding scales {scales} | *(contact)* |
| `BIR194` | Partitions accessed by weights {weights} should match partitions accessed by corresponding scales {scales} | *(contact)* |
| `BIR195` | PE Row tile size {row_tile_size} of MatmultMx must match number of input partitions accessed {num_partitions} | *(contact)* |
| `BIR196` | MatmultMx does not support column tiling | *(contact)* |
| `BIR197` | More than one entry found in CurrentCallsites | *(contact)* |
| `BIR198` | More than one entry found in CurrentCallers | *(contact)* |
| `BIR199` | OBSOLETE ERROR CODE. DO NOT USE. - More than one entry found in CurrentNKICallsites | *(contact)* |
| `BIR200` | Expected CurrentCallsites size to be 0 before visiting InstCall {name} | *(contact)* |
| `BIR201` | Expected CurrentCallers size to be 0 before visiting InstCall {name} | *(contact)* |
| `BIR202` | OBSOLETE ERROR CODE. DO NOT USE. - Expected CurrentNKICallsites size to be 0 before visiting {name} | *(contact)* |
| `BIR203` | OBSOLETE ERROR CODE. DO NOT USE. - Expected CurrentNKICallers size to be 0 before visiting NKI Function {name} | *(contact)* |
| `BIR204` | OBSOLETE ERROR CODE. DO NOT USE. - More than one entry found in CurrentNKICallers | *(contact)* |
| `BIR205` | NKI Kernel function pointer is empty in instruction {name} | *(contact)* |
| `BIR206` | DGE xbar transpose dst start partition must be a multiple of {expected}, but it is {actual} (remainder of {remainder}) | *(contact)* |
| `BIR207` | CopyPredicated only allows max as Reduce Operator. Given: {actual} | *(contact)* |
| `BIR208` | CopyPredicated only allows EngineAccumulationType of Idle, ZeroAccumulate, or AddAccumulate. Given: {actual} | *(contact)* |
| `BIR209` | CopyPredicatedReduce has AccumulationCmd set to non-Idle ({accumulation_cmd}), but does not have a corresponding ReductionCmd ({reduce_cmd}). | *(contact)* |
| `BIR210` | CopyPredicated is set to use reduction mode, requires 3 arguments. (pred_tensor, src_tensor, immediate) Received: {actual} | *(contact)* |
| `BIR211` | CopyPredicatedReduce requires src1 to be a tensor | *(contact)* |
| `BIR212` | CopyPredicatedReduce requires src2 to be a scalar | *(contact)* |
| `BIR213` | CopyPredicatedReduce allows any dtype permitted by DtypePair, except float32r, uint64, int64, int32, uint32. given: src type: {src_type}, predicate type: {pred_type} | *(contact)* |
| `BIR214` | CopyPredicated requires predicates to have same number of elements as Output. Given predicates: {pred_shape}, and output={out_shape} elements respectively | *(contact)* |
| `BIR215` | CopyPredicated requires that src data (if a tensor) must have the same shape as output and predicate tensor. Given src: {src_shape}, predicate={pred_shape}, and output={out_shape} elements respectively. | *(contact)* |
| `BIR216` | CopyPredicatedReduce immediate value must be float32. But it is {actual}. | *(contact)* |
| `BIR217` | CopyPredicatedScalar Immediate value type ({imm_type}) must match output type ({out_type}). | *(contact)* |
| `BIR218` | Non-scalar and Non-Reduce CopyPredicated cannot have reverse predication | *(contact)* |
| `BIR219` | Start partition {start_partition} of QuantizeMx scales be a multiple of 32 (start of quadrant) plus 0, 4, 8, or 12 | *(contact)* |
| `BIR220` | Number of data partitions {op} should match number of scale partitions {scales} | *(contact)* |
| `BIR221` | {inst_name}: ActivationCopy cannot have an MX (microscaled) source or destination | *(contact)* |
| `BIR222` | cast_fp32_to_float4e2m1fn_x2 expects count {count} to be a multiple of 2 | *(contact)* |
| `BIR223` | cast_fp32_to_float8e4m3fn_x4 expects count {count} to be a multiple of 4 | *(contact)* |
| `BIR224` | cast_fp32_to_float8e5m2_x4 expects count {count} to be a multiple of 4 | *(contact)* |
| `BIR225` | {inst_name}: QuantizeMx input elements per partition {inp_elts} should be 4x number of output scales per partition {scales} | *(contact)* |
| `BIR226` | InstBNStatsAggregate must output 2 elements per partition, but it outputs {actual} | *(contact)* |
| `BIR227` | Axis {axis} not found in current loop nest | *(contact)* |
| `BIR228` | State buffer allocation failed: total size of tensors that need to be allocated in state buffer to execute {inst} exceeds state buffer per-partition capacity.\\nTotal accessed bytes per partition: {instruction_accessed}\\nTensor details:\\n{memloc_accessed}\\nSB partition size: {partition_size} | *(contact)* |
| `BIR229` | State buffer allocation failed: total size of tensors that need to be allocated in state buffer to execute {inst} exceeds state buffer capacity.\\nTensor details:\\n{memloc_accessed}\\nTotal available SB Size: {total_size} | *(contact)* |
| `BIR230` | Indirect tensor `{name}` of Vector DGE DMACopy must have MemoryType::DRAM, but it has MemoryType::{actual} | Do not load the indirect tensor into SB first |
| `BIR231` | Non-indirect tensor `{name}` of Vector DGE DMACopy must have MemoryType::SB, but it has MemoryType::{actual} | Use an SB tensor as the non-indirect tensor |
| `BIR232` | Index tensor `{name}` of Vector DGE DMACopy must be in SB, but it is in {actual} | Load the indices into SB first, then use them in this instruction |
| `BIR233` | TransposeDMA with dynamic offset cannot fallback to static DMA | *(contact)* |
| `BIR234` | CCE op is only valid on DMACopys that have mode=CCE or use vector DGE (dst_reduce) | This is likely a bug in NKI. You may be able to get around it by not setting `dst_rmw_op` or using vector DGE on the `nisa.dma_copy` |
| `BIR250` | DMA that is not valid for DGE must have DGEType::None, but it has DGEType::{actual} | *(contact)* |
| `BIR251` | Static DMA that is in a dynamic loop can only read from Internal tensor, but it reads from {actual} tensor | *(contact)* |
| `BIR252` | Static DMA that is in a dynamic loop can only write to Internal tensor, but it writes to {actual} tensor | *(contact)* |
| `BIR253` | Instruction Type InstTensorScalarPtr does not support EngineAccumulationType Accumulate, only supports Idle, Zero, ZeroAccumulate, and AddAccumulate | *(contact)* |
| `BIR254` | Instruction Type InstTensorTensor does not support EngineAccumulationType Accumulate, only supports Idle, Zero, ZeroAccumulate, and AddAccumulate | *(contact)* |
| `BIR255` | Instruction Type InstRangeSelect does not support EngineAccumulationType Accumulate, only supports Idle, Zero, ZeroAccumulate, and AddAccumulate | *(contact)* |
| `BIR256` | (Instruction: {inst}) ShardId can only be in one block dimension. | Consider rewriting the ShardId-related access to use ShardId in only one block dimension. |
| `BIR257` | (Instruction: {inst}) Block dimension expression with ShardId can only be an AffineExpr or ShardId; got a non-AffineExpr. | Consider rewriting the ShardId-related access to use an affine expression instead of a more complex expression. |
| `BIR258` | (Instruction: {inst}) Block dimension expression with ShardId can only be an AffineExpr with a single ShardId index; got multiple ShardId indices | Consider rewriting the ShardId-related access to use only one ShardId as an index in the affine expression. |
| `BIR259` | Number of packed elements per partition for weights ({num}) should be even | *(contact)* |
| `BIR293` | Found MemoryLocationSet that has not been read, {name} | *(contact)* |
| `BIR294` | Found MemoryLocationSet that has not been read, {name} | *(contact)* |
| `BIR295` | Fail to find tensor scope parent | *(contact)* |
| `BIR296` | tensor_scope_parent can only be Function or InstLoop/InstDynamicForLoop/InstDoWhile | *(contact)* |
| `BIR297` | For specific instructions, base partition for access is expected to be equal if both inputs are in SB, but {instr} violates this constraint. | *(contact)* |
| `BIR298` | Could not find indirection argument {argId} parent={parent} location={location}{additionalInfo} | *(contact)* |
| `BIR299` | Unexpected instruction argument type: {type} | *(contact)* |
| `BIR300` | Unexpected instruction argument type string: {name} | *(contact)* |
| `BIR301` | Unexpected instruction argument type ({type}) when adding argument or output | *(contact)* |
| `BIR309` | Expected start address {addr} of output to be 2B aligned (NOT 8B/4B aligned) if outputAP step_elem_x=-1 | *(contact)* |
| `BIR310` | Expected matmult instructions in same accumulation group: {instr1} with output dtype {dtype1} and {instr2} with output dtype: {dtype2} to have same output dtypes | *(contact)* |
| `BIR311` | Only Matmult and Memset instructions can write BF16 outputs to PSUM | *(contact)* |
| `BIR312` | Expected start address {addr} of output to be 4B aligned if outputAP step_elem_x=1 | *(contact)* |
| `BIR400` | Couldn't open file '{file}' to write BIR json | *(contact)* |
| `BIR401` | Memset mode 'Random' is only supported on Vector Engine for NeuronCore V2 | *(contact)* |
| `BIR402` | Memset mode 'Random' is only supported on Vector Engine and GpSIMD Engine for NeuronCore V3 | *(contact)* |
| `BIR403` | Seed datatype must be 32 bits | *(contact)* |
| `BIR404` | TensorIndirect Symbolic AP must not directly call "Unroll" API since it needs additional dynamic AP handling | *(contact)* |
| `BIR406` | Requested Indirect Argument index {index} out of bounds ({num_indirect_args}) | *(contact)* |
| `BIR407` | Requested Indirect Argument index {index} out of bounds ({num_indirect_args}) | *(contact)* |
| `BIR410` | Inferring psum_zero_region for matmul but output {mloc} is not allocated | *(contact)* |
| `BIR411` | Inferring psum_zero_region for matmul but its size or start addr not consistent with psum zero region | *(contact)* |
| `BIR412` | Instruction: {opcode} has invalid memory location type: {actual_type}. Supported memory location types are: {expected_type} | *(contact)* |
| `BIR420` | SB CC kind {kind} not supported for dimension {dim} | *(contact)* |
| `BIR421` | Argument or output {arg} not contiguous for SB global CC | *(contact)* |
| `BIR422` | Argument or output {arg} not on SB for SB global CC | *(contact)* |
| `BIR423` | {kind} on SB input and output shapes mismatch, got:\\n  input 0 shape: {input0_shape}\\n  input 1 shape: {input1_shape}\\n  output shape: {output_shape} | *(contact)* |
| `BIR424` | AllReduce on SB input and output shapes mismatch, got:\\n  input shape: {input_shape}\\n  output shape: {output_shape} | *(contact)* |
| `BIR425` | AllGather on SB on {dim} input, output, and replica groups shapes mismatch, got:\\n  input shape: {input_shape}\\n  output shape: {output_shape}\\n  replica groups shape: {replica_groups_shape} | *(contact)* |
| `BIR426` | ReduceScatter on SB on {dim} input, output, and replica groups shapes mismatch, got:\\n  input shape: {input_shape}\\n  output shape: {output_shape}\\n  replica groups shape: {replica_groups_shape} | *(contact)* |
| `BIR427` | SB CC should only have a single output. | *(contact)* |
| `BIR428` | SB CC is only supported on trn2+. | *(contact)* |
| `BIR429` | Access must be aligned to datatype, {mem_type} start address {start_addr} for memory location {mem} must be {dtype_size} bytes aligned for data type {dtype}. | *(contact)* |
| `BIR430` | Expect no InstLoop after unroll, violation: {inst}. | *(contact)* |
| `BIR431` | Expect no InstDynamicForLoop after unroll, violation: {inst}. | *(contact)* |
| `BIR432` | Expect no InstDoWhile after unroll, violation: {inst}. | *(contact)* |
| `BIR433` | Expect no InstGenericIndirectLoad/Save after unroll, violation: {inst}. | *(contact)* |
| `BIR434` | Expect no SymbolicAccessPattern after unroll, violation: {inst}. | *(contact)* |
| `BIR435` | Expect no tensor scope parent on memory location sets after unroll, violation: {mset}. | *(contact)* |
| `BIR436` | Fine grained InstCollectiveCompute cannot enter as coalesced, this is an internal feature, violation: {inst}, {kind}. | *(contact)* |
| `BIR437` | Consecutive InstCollectiveCompute cannot have the same kind after coalesce_multichannel_cc_ops, violation: {inst1}, {inst2}. | *(contact)* |
| `BIR438` | CCE DMA instructions must have at most {arg_limit} arguments after legalization, violation: {inst} with {num_args} arguments. | *(contact)* |
| `BIR439` | Expect no InstAbstractCopy after lower_ac, violation: {inst}. | *(contact)* |
| `BIR440` | Expect all {mem_type} memory locations to be allocated after {pass_name}, violation: {mem}. | *(contact)* |
| `BIR441` | Expect {inst} write to PSUM with 4-byte aligned dataType, violation: {mem}. {exceptions_msg} | *(contact)* |
| `BIR444` | Invalid InstTensorScalarPtr {tsp} on Activation Engine, op0: {op0}, op1: {op1}, rev0: {rev0}, rev0: {rev1}. | *(contact)* |
| `BIR450` | GPSIMDSB2SB is only available for LNC=2 | *(contact)* |
| `BIR451` | GPSIMDSB2SB is only available in trn2 and later | *(contact)* |
| `BIR452` | GPSIMDSB2SB cannot be used to cast | *(contact)* |
| `BIR453` | GPSIMDSB2SB can only move data between access patterns of the same size | *(contact)* |
| `BIR454` | GPSIMDSB2SB access patterns must have a valid number of dimensions | *(contact)* |
| `BIR455` | GPSIMDSB2SB can only move data between SBs | *(contact)* |
| `BIR456` | GPSIMDSB2SB does not support dynamic access patterns | *(contact)* |
| `BIR457` | GPSIMDSB2SB tensors must have partition dimension that's a multiple of 16 | *(contact)* |
| `BIR458` | GPSIMDSB2SB is only available in GPSIMD engine | *(contact)* |
| `BIR459` | GPSIMDSB2SB must have a source and destination | *(contact)* |
| `BIR460` | GPSIMDSB2SB can only transfer less than {maxBPP} bytes/partition, but is trying to transfer {bpp} bytes/partition{maybeOverhead} | *(contact)* |
| `BIR480` | Expected attribute max (value: {max_val}) to be greater than min (value: {min_val}) for random number generation | *(contact)* |
| `BIR481` | Invalid seed for engine: GPSIMD. Seed must be an nx6 tensor. Got seed tensor of dimensions nx{dim} | *(contact)* |
| `BIR482` | Invalid seed for engine: Vector. Seed must be an nx24 tensor. Got seed tensor of dimensions nx{dim} | *(contact)* |
| `BIR500` | DGE xbar dynamic transpose can only use DGEType::SWDGE, but it uses DGEType::{actual} | *(contact)* |
| `BIR501` | DGE xbar dynamic transpose must be on EngineType::Pool, but it is on EngineType::{actual} | *(contact)* |
| `BIR503` | DGE xbar dynamic transpose source tensor ('{tensor_name}') address must be {expected} byte aligned, but it is {actual} | *(contact)* |
| `BIR504` | DGE xbar dynamic transpose index tensor ('{tensor_name}') must have Dtype::uint32, but it has Dtype::{actual} | *(contact)* |
| `BIR505` | DGE xbar dynamic transpose index tensor ('{tensor_name}') must have MemoryType::SB, but it has MemoryType::{actual} | *(contact)* |
| `BIR506` | DGE xbar dynamic transpose must use a multiple of 16 indices, but it uses {actual} | *(contact)* |
| `BIR507` | DGE xbar dynamic transpose index tensor ('{tensor_name}') must have 1 element per partition, but it has {actual} | *(contact)* |
| `BIR508` | DGE xbar dynamic transpose index tensor ('{tensor_name}') address must be {expected} byte aligned, but it is {actual} | *(contact)* |
| `BIR510` | DGE xbar dynamic transpose only supports TransposeOps::XZYW, but it is TransposeOps::{actual} | *(contact)* |
| `BIR511` | DMA transpose input and output should have the same type, but it has in Dtype::{in} out Dtype::{out} | *(contact)* |
| `BIR512` | DMA transpose outer dimensions are not transposed. [{in0}, {in3}] (in) -> [{out0}, {out3}] (out) | *(contact)* |
| `BIR514` | DGE xbar dynamic transpose src dimension 1 or 2 should be padding (Num=1), but neither is (dim1={dim1}, dim2={dim2}) | *(contact)* |
| `BIR515` | DGE xbar dynamic transpose dst dimension 1 or 2 should be padding (Num=1), but neither is (dim1={dim1}, dim2={dim2}) | *(contact)* |
| `BIR516` | Unable write json to {path}. | Please ensure diretory exists and disk space is available. |
| `BIR518` | DGE xbar dynamic transpose must have 1 AccessPattern output, but it has {actual} | *(contact)* |
| `BIR519` | DMA transpose with a vector of dynamic offsets is not supported on trn1 | Try compiling for trn2 instead |
| `BIR520` | DGE xbar dynamic transpose src actual pattern num partitions ({src}) doesn't match number of indices ({idx}) | *(contact)* |
| `BIR521` | DGE xbar dynamic transpose outer dimensions are not transposed. [{in0}, {in3}] (in) -> [{out0}, {out3}] (out) | *(contact)* |
| `BIR530` | {opcode} is not supported on arch {arch}, must be {min_arch} or greater | Do not use {opcode} or switch to at least {min_arch} architecture |
| `BIR531` | {opcode} is not supported on arch {arch}, must be {max_arch} or less | Do not use {opcode} or switch to at most {max_arch} architecture |
| `BIR532` | {opcode} instruction must have enum of type {enumType} initialized | Initialize enum values for instruction |
| `BIR533` | {enumVal} is not a valid enum value for field {fieldName} on arch {arch} | Choose a valid value for field {fieldName} |
| `BIR534` | TensorTensor only supports EngineAccumulationType::Idle, but got {actual} | Use ScalarTensorTensor instead, or give a supproted AccumulationEngine. |
| `BIR535` | Compilation with --target=trn1 generated local collective compute instruction. Local collectives supported only for trn2+. Please re-compile by setting compilation target to trn2+ | *(contact)* |
| `BIR600` | DMA trigger has DMA blocks specifying QoS class {actual} but only {mappedCount} classes are mapped by runtime | *(contact)* |
| `BIR601` | Instruction specifies DMA QoS class {actual} but only {mappedCount} classes are mapped by runtime | *(contact)* |
| `BIR602` | Command line specifies {given} QoS classes are available on the target but the ISA encoding limits this to {max} | *(contact)* |
| `BIR603` | Failed to encode DMA QoS priority for JSON/ISA from value {value} | *(contact)* |
| `BIR604` | Failed to decode DMA QoS priority for JSON/ISA from value {value} | *(contact)* |
| `BIR605` | Bias parameter for InstActivation can only be an ImmediateValue if the activation function is Copy or Reciprocal, but got {actual}. This restriction does not apply to trn2+ architectures. | Use a vector-immediate (Nx1 tensor) as the bias parameter instead, or recompile for trn2+ |
| `BIR606` | Engine {engine} is invalid for {opcode} instruction on arch {arch}. Must be {validEngines}. | Set engine to {validEngines} |
| `BIR700` | Unrecognized InstIO type {type} at index {index} | *(contact)* |
| `BIR701` | Tensor indirection is only supported on core_v4 and later | *(contact)* |
| `BIR702` | Indirection tensor for argument {index} must be in {expected} memory but is instead in {actual} | *(contact)* |
| `BIR703` | Indirection tensor for output {index} must be in {expected} memory but is instead in {actual} | *(contact)* |
| `BIR704` | Indirection tensor for output {index} must be in the same partition as the argument, partition {expected}, but is instead in partition {actual} | *(contact)* |
| `BIR705` | Tensor indirection in tensor engine can only gather data of FP16, BF16, and FP8 data types, but argument {index} is of type {actual} | *(contact)* |
| `BIR706` | Tensor indirection in scalar engine cannot scatter to PSUM, but output {index} does | *(contact)* |
| `BIR707` | Scatter tensor indirection requires that destination's indirection tensor be in the same partition as any source, but the source data is in {src} and the destination's indirection tensor is in {dst} | *(contact)* |
| `BIR709` | Tensor indirection is not supported for TensorReduce when axis type is C or XYZWC | *(contact)* |
| `BIR710` | Tensor indirection is only supported for CopyPredicated without a reduction operation | *(contact)* |
| `BIR711` | Tensor indirection is only supported for CopyPredicated involving a scalar true value | *(contact)* |
| `BIR712` | Tensor indirection is not supported for tensor-scalar-addr cases of TensorScalarPtr | *(contact)* |
| `BIR713` | Tensor indirection is not supported for scalar-tensor-tensor cases of TensorScalarPtr | *(contact)* |
| `BIR714` | Tensor indirection is not supported for tensor-tensor-scan cases of TensorScalarPtr | *(contact)* |
| `BIR715` | Tensor indirection is not supported for transpose matmul | *(contact)* |
| `BIR716` | Tensor indirection is not supported for row-tiled matmul | *(contact)* |
| `BIR717` | Tensor indirection is not supported for column-tiled matmul | *(contact)* |
| `BIR718` | {ioDirn} {ioIndex} can't be tensor-indirect because it maps to a {ioDirn} on the TensorTensor ISA that does not support it | *(contact)* |
| `BIR719` | Argument {index} doesn't support tensor indirection | *(contact)* |
| `BIR720` | Output {index} doesn't support tensor indirection | *(contact)* |
| `BIR721` | AccessPattern::getNumElementsPerPartitionConsideringIndirection(): AP must have more than 1 dim | *(contact)* |
| `BIR722` | Argument {index} doesn't support tensor indirection | *(contact)* |

#### BIRSERDES — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `BIRSERDES001` | BIR signatures mismatch before and after serialization + deserialization | *(contact)* |

#### BLN — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `BLN001` |  |  |

#### BMT — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `BMT001` | Unknown BackendMetricType {unknown_type}. | *(contact)* |

#### BUG — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `BUG001` | Walrus driver ran with flag --bugpoint-throw-after to trigger this error for testing | *(contact)* |

#### BVF — bare (29 codes)

| Code | Cause | Resolution |
|---|---|---|
| `BVF001` | InstTensorScalarCache must have 2 or 3 inputs, but has {actual} | *(contact)* |
| `BVF002` | InstTensorScalarCache first input must be an AccessPattern but it is a {actual} | *(contact)* |
| `BVF003` | InstTensorScalarCache must have 1 or 2 outputs, but has {actual} | *(contact)* |
| `BVF004` | InstTensorScalarCache first output must be an AccessPattern but it is a {actual} | *(contact)* |
| `BVF005` | InstTensorScalarCache first immediate value must have Dtype::float32, but it has Dtype::{actual} | *(contact)* |
| `BVF006` | InstTensorScalarCache second immediate value must have Dtype::float32, but it has Dtype::{actual} | *(contact)* |
| `BVF007` | InstTensorScalarCache with 3 arguments must have EngineAccumulationType::LoadAccumulate, but it has EngineAccumulationType::{actual} | *(contact)* |
| `BVF008` | InstTensorScalarCache with EngineAccumulationType::LoadAccumulate must have 3 arguments, but it has {actual} | *(contact)* |
| `BVF009` | InstTensorScalarCache reduce output must be an AccessPattern but it is a {actual} | *(contact)* |
| `BVF010` | InstTensorScalarCache reduce output must have 1 element per partition, but it has {actual} | *(contact)* |
| `BVF011` | InstTensorScalarCache reduce output must have same number of partitions as first argument ({expected}), but it has {actual} | *(contact)* |
| `BVF012` | InstTensorScalarCache reduce output must have a floating-point Dtype, but it has Dtype::{actual} | *(contact)* |
| `BVF013` | InstTensorScalarCache first input has invalid type Dtype::{actual} | *(contact)* |
| `BVF014` | InstTensorScalarCache first output has invalid type Dtype::{actual} | *(contact)* |
| `BVF015` | InstTensorScalarCache op0 must be arithmetic, but it is AluOpType::{actual} | *(contact)* |
| `BVF016` | InstTensorScalarCache op1 must be add/subtract/mult/min/max, but it is AluOpType::{actual} | *(contact)* |
| `BVF017` | InstTensorScalarCache with TSCMode::TensorScan must have AluOpType::bypass for op0, but it has AluOpType::{actual} | *(contact)* |
| `BVF018` | InstTensorScalarCache with TSCMode::TensorScan cannot have reverse0 = true | *(contact)* |
| `BVF019` | InstTensorScalarCache on Sunda cannot have reverse0 = true | *(contact)* |
| `BVF020` | InstTensorScalarCache on Sunda cannot have reverse1 = true | *(contact)* |
| `BVF021` | InstTensorScalarCache must be on the DVE engine, but it is on {actual} | *(contact)* |
| `BVF022` | InstTensorScalarCache EngineAccumulationType must be one of ZeroAccumulate/AddAccumulate/LoadAccumulate, but it is {actual} | *(contact)* |
| `BVF023` | InstTensorScalarCache with TSCMode::TensorScan must have an ImmediateValue as its 2nd input, but it has a {actual} | *(contact)* |
| `BVF024` | InstTensorScalarCache with TSCMode::TensorScan must have immediate value of 0 as its 2nd input, but it has {actual} | *(contact)* |
| `BVF025` | InstTensorScalarCache does not support AluOpType::rsqrt | *(contact)* |
| `BVF026` | InstTensorScalarCache SymbolicImmediateValue values must all be 0, but one of them is {actual} | *(contact)* |
| `BVF027` | Instruction can only read one of its non-scalar inputs from PSUM, but inputs {args} are read from PSUM | Copy tensor(s) from PSUM to SB prior to using this instruction |
| `BVF028` | Instruction can only read one of its inputs from PSUM, but inputs {args} are read from PSUM | Copy tensor(s) from PSUM to SB prior to using this instruction |
| `BVF029` | Invalid in-place write at {inst}: {arg} and {out} overlap in memory, and read is not guaranteed to complete before write at overlapping location | *(contact)* |

#### CCO — bare (10 codes)

| Code | Cause | Resolution |
|---|---|---|
| `CCO001` | Expected all consecutive CC compute ops to be of the same Op, but got {first} != {curr} | *(contact)* |
| `CCO002` | Expected all consecutive CC compute ops to be of the same Engine, but got {first} != {curr} | *(contact)* |
| `CCO003` | Expected all consecutive CC compute ops to be of the same Kind, but got {first} != {curr} | *(contact)* |
| `CCO004` | Expected all consecutive CC compute ops to be of the same ReplicaGroups | *(contact)* |
| `CCO005` | Expected all consecutive CC compute ops to be of the same HasOrder | *(contact)* |
| `CCO006` | Expected all consecutive CC compute ops to be of the same CanReadUninit | *(contact)* |
| `CCO007` | Expected all consecutive CC compute ops to be of the same IsLocal | *(contact)* |
| `CCO008` | Expected all consecutive CC compute ops to be of the same SrcTargetPairs | *(contact)* |
| `CCO009` | Expected all consecutive CC compute ops to be of channel id starting at 0 and increasing by 1, but got first: {first}, curr: {curr} | *(contact)* |
| `CCO010` | Expected CC op to not have both ReplicaGroups and SrcTargetPairs | *(contact)* |

#### CFG — bare (19 codes)

| Code | Cause | Resolution |
|---|---|---|
| `CFG001` | Control flow graph of function {function_name} has multiple exit nodes: {block_a}, {block_b} | *(contact)* |
| `CFG002` | Control flow graph of function {function_name} does not have an exit node | *(contact)* |
| `CFG003` | Control flow graph of function {function_name} has multiple entry nodes: {block_a}, {block_b} | *(contact)* |
| `CFG004` | Control flow graph of function {function_name} does not have an entry node | *(contact)* |
| `CFG005` | Unreachable block {unreachable_block_name} found in {function_name} | *(contact)* |
| `CFG006` | Found duplicate targets for a back edge from {source_block} to {target_block_1} and {target_block_2} in control flow graph of function {function_name} | *(contact)* |
| `CFG007` | Unable to find predecessor of non-entry block {block_name} | *(contact)* |
| `CFG008` | Block {block_name} is not the entry block, but is listed as having no immediate dominator | *(contact)* |
| `CFG009` | Unable to find successor of non-exit block {block_name} | *(contact)* |
| `CFG010` | Block {block_name} is not the exit block, but is listed as having no immediate postdominator | *(contact)* |
| `CFG011` | Function {function_name} basic blocks not in topological order: {block1} found before predecessor {block2} | *(contact)* |
| `CFG012` | Cannot find Basic Block {bb_name} in cfg in getPredecessors() call | *(contact)* |
| `CFG013` | Cannot find Basic Block {bb_name} in cfg in getSuccessors() call | *(contact)* |
| `CFG014` | Cannot find Basic Block {bb_name} in cfg in getOutDegrees() call | *(contact)* |
| `CFG015` | Loop must be reducible, detected violation back-edge: {back_edge} where {dst} does not dominate {src}. | *(contact)* |
| `CFG016` | Loop headers require exactly one external in-edge as we currently do not support IF statements.\\nDetected the following loop header: {header} with entries: {entry_edges}. | *(contact)* |
| `CFG017` | Loop structures require exactly one non-exit external out-edge as we currently do not support BREAK statements.\\nDetected the following loop structure:\\n  {basic_blocks}\\nwith exits:\\n{exit_edges}. | *(contact)* |
| `CFG018` | Loop structures require exactly one internal back-edge as we currently do not support CONTINUE statements or nested loops.\\nDetected the following loop structure:\\n  {basic_blocks}\\nwith internal back-edges:\\n{back_edges}. | *(contact)* |
| `CFG019` | Basic blocks require exactly one forward edge with the exception of loop entries and exit blocks, as we currently do not support IF statements.\\nDetected {basic_block} with forward edges: {forward_edges}. | *(contact)* |

#### CFP — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `CFP001` | CoreForkPass called with single module {name} | *(contact)* |
| `CFP002` | Compilation failed for modules targeting the following cores:{errors}. | *(contact)* |

#### CUT — bare (8 codes)

| Code | Cause | Resolution |
|---|---|---|
| `CUT001` | Missing {name} vertex in NX wavegraph | *(contact)* |
| `CUT002` | Length of wavegraphs differ after transform | *(contact)* |
| `CUT003` | Missing {name} in graph. | *(contact)* |
| `CUT004` | Input must be multiple of 2 | *(contact)* |
| `CUT005` | Invalid input type {dtype} | *(contact)* |
| `CUT006` | Can't convert to NC format, need (N,C,1,1) dims | *(contact)* |
| `CUT007` | Unable to compute left padding | *(contact)* |
| `CUT008` | Unable to compute right padding | *(contact)* |

#### DAE — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `DAE001` | Lock directory does not exist | *(contact)* |
| `DAE002` | Unknown verbosity level, choose from debug\|info\|warning\|user | *(contact)* |

#### DMA — bare (15 codes)

| Code | Cause | Resolution |
|---|---|---|
| `DMA126` | Unrecognized Architecture: {target} | *(contact)* |
| `DMA127` | Unrecognized Architecture: {target} | *(contact)* |
| `DMA128` | Undefined DRAM Memloc {mem} | *(contact)* |
| `DMA129` | Undefined SB Memloc {mem} | *(contact)* |
| `DMA130` | Unrecognized Architecture: {target} | *(contact)* |
| `DMA131` | Unrecognized Architecture: {arch}\\n | *(contact)* |
| `DMA132` | Unrecognized Architecture: {arch}\\n | *(contact)* |
| `DMA133` | DRAM Memloc with zero access {mem} | *(contact)* |
| `DMA134` | Attempted to merge load for a memloc that shouldn't be removed: {mem} | *(contact)* |
| `DMA135` | Attempted to transform Load to TensorCopy for an instruction that shouldn't be touched: {inst} | *(contact)* |
| `DMA136` | Undefined local memloc: {mem} | *(contact)* |
| `DMA137` | Try to access removed memorylocation | *(contact)* |
| `DMA138` | Could not find PSUM partition range index {end} among live track of size {live_size}; partition range indexes into ranges 0-32, 32-64, 64-96, 96-128. | *(contact)* |
| `DMA139` | Need to build valid CFG for DMA optimization pass | *(contact)* |
| `DMA140` | Found Instruction with unsupported DGE type DGEType::HWDGE on Sunda Arch | *(contact)* |

#### DRV — bare (78 codes)

| Code | Cause | Resolution |
|---|---|---|
| `DRV001` | Deprecated store true action does not permit any arguments | *(contact)* |
| `DRV002` | Enable disable action does not permit any arguments | *(contact)* |
| `DRV003` | Enable disable action expected a default value | *(contact)* |
| `DRV004` | Enable disable action expected one option string | *(contact)* |
| `DRV005` | {initial_option} does not start with '--enable' or '--disable' | *(contact)* |
| `DRV006` | No-able action does not permit an argument | *(contact)* |
| `DRV007` | No-able action expected a default value | *(contact)* |
| `DRV008` | No-able action expected one option string | *(contact)* |
| `DRV009` | {initial_option} does not start with '--' or '--no-' | *(contact)* |
| `DRV010` | Set argument action expected a const to use for the value of the action | *(contact)* |
| `DRV011` | While parsing {arg_string} {a} | *(contact)* |
| `DRV013` | Unable to find base classes of Job | *(contact)* |
| `DRV014` | Unknown verbosity level, choose from debug\|info\|warning\|user | *(contact)* |
| `DRV015` | Start time not set in TimeRegion context __exit__ | *(contact)* |
| `DRV016` | Delta could not be computed in TimeRegion context __exit__ | *(contact)* |
| `DRV017` | XLA flow only takes 1 model file | *(contact)* |
| `DRV018` | Meta module not specified | *(contact)* |
| `DRV019` | Meta module '{abs_meta_module}' not found | *(contact)* |
| `DRV020` | No fp32-cast option in args: {codegen_args} | *(contact)* |
| `DRV021` | Expected target of 'cpu' or 'tonga', but got {target} | *(contact)* |
| `DRV022` | Insufficient target info | *(contact)* |
| `DRV023` | Expected to find 'dataflow' or 'functions' in ir_type data | *(contact)* |
| `DRV024` | Expected ir_type data to be a dict or list | *(contact)* |
| `DRV025` | HhSubgraphs expected hhRes to be a tuple | *(contact)* |
| `DRV026` | NEFF archive without accelerator or CPU compute ops is not supported. Please check that your model has non-constant computation that is dependent on the inputs listed in --io-config | *(contact)* |
| `DRV027` | Output {a} not found in network | *(contact)* |
| `DRV028` | CPU nodes not supported | *(contact)* |
| `DRV029` | Expected length of from format and shape to match | *(contact)* |
| `DRV030` | Expected length of from and to formats | *(contact)* |
| `DRV031` | Expected layout to be 'native' | *(contact)* |
| `DRV032` | --jf_data_layout=tonga not supported with --io-config | *(contact)* |
| `DRV033` | Expected shape to have a non-zero length | *(contact)* |
| `DRV034` | Unhandled data type {a} | *(contact)* |
| `DRV035` | Missing act_info for used function set {used_act_set} | *(contact)* |
| `DRV036` | Expected NEFF hash-length to be 32 | *(contact)* |
| `DRV037` | Expected NEFF version string to be 'Dynamic' | *(contact)* |
| `DRV038` | Expected fewer than 64 feature bits in NEFF | *(contact)* |
| `DRV039` | Expected 16-byte UUID | *(contact)* |
| `DRV040` | Header size is {a} | *(contact)* |
| `DRV041` | Only versions 2+ supports non-zero feature bits | *(contact)* |
| `DRV042` | Experimental --autoloop-spec {autoloop_file} not found | *(contact)* |
| `DRV043` | Could not read kelpInfo for NEFF | *(contact)* |
| `DRV044` | Input {a} for sg{idoffset} not found in network inputs or previous graph | *(contact)* |
| `DRV045` | Output {a} not found in network | *(contact)* |
| `DRV046` | Expected single input BIR file | *(contact)* |
| `DRV047` | Meta module doesn't exist | *(contact)* |
| `DRV048` | Module has no reference_inputs | *(contact)* |
| `DRV049` | Module has no model object | *(contact)* |
| `DRV050` | Expected module to contain a 'vm_inputs' entry | *(contact)* |
| `DRV051` | Expected module to contain a 'mod' entry | *(contact)* |
| `DRV052` | InferGoldens failed in creating inference io context using {a} | *(contact)* |
| `DRV053` | BIRSim output {tensor_name} is not in output name map! | *(contact)* |
| `DRV054` | Multiple MLA nodes are not supported | *(contact)* |
| `DRV055` | Kelf json file does not match expected format | *(contact)* |
| `DRV056` | Compiler is not saving temps | *(contact)* |
| `DRV057` | Expected lengths of source and target layouts to match | *(contact)* |
| `DRV058` | Expected non-negative indices in transform | *(contact)* |
| `DRV059` | Expected two entries in random range match | *(contact)* |
| `DRV060` | Expected two entries in line space range match | *(contact)* |
| `DRV061` | No value available for tensor {name} | *(contact)* |
| `DRV062` | Got {a} outputs but expected {b} - {outputs} | *(contact)* |
| `DRV063` | {metadata} does not exist | *(contact)* |
| `DRV064` | {weight_file} does not exist | *(contact)* |
| `DRV065` | Model has {a} inputs while {b} given by --images | *(contact)* |
| `DRV066` | Invalid keep rate index | *(contact)* |
| `DRV067` | Tensor {name} has NaN | *(contact)* |
| `DRV068` | HLO module {input} doesn't exist | *(contact)* |
| `DRV069` | {metadata} does not exist | *(contact)* |
| `DRV070` | Discovered outputs length is {a} while only {b} golden output produced | *(contact)* |
| `DRV071` | Multiple entries added for same file key | *(contact)* |
| `DRV072` | Unknown --list argument {arg}, choose from supported\|all\|missing | *(contact)* |
| `DRV073` | Illegal arguments. Please use --option-preset dma_tensor=<name>,<name>... | *(contact)* |
| `DRV074` | Unsupported arguments in --option-preset! | *(contact)* |
| `DRV075` | Unsupported --output type {arg}, choose from text\|json | *(contact)* |
| `DRV076` | Unexpected NEFF header | *(contact)* |
| `DRV077` | Compilation artifacts already exist in the output directory ({working_dir}). | Please ensure to run the compiler with a clean artifacts directory to avoid unintended mixing of compilation artifacts between multiple models. |
| `DRV078` | Discovered intermediates length is {a} while only {b} golden intermediates produced | *(contact)* |
| `DRV079` | Only accepts at most 1 model file | *(contact)* |

#### DVR — bare (24 codes)

| Code | Cause | Resolution |
|---|---|---|
| `DVR000` | Expect input file specified with -i | See neuronx-cc --help for CLI usage |
| `DVR001` | Internal validation failed | *(contact)* |
| `DVR002` | Internal transformation failed | *(contact)* |
| `DVR003` | Cannot specify --optlevel and --pass at the same time | See neuronx-cc --help for CLI usage |
| `DVR005` | Cannot override storage allocator with --allocator when passing --pass options | See neuronx-cc --help for CLI usage |
| `DVR006` | Internal pipeline construction error | *(contact)* |
| `DVR007` | --enable_partitioner may not be used with -O0 | See neuronx-cc --help for CLI usage |
| `DVR008` | --enable_partitioner may not be used with -O6 | See neuronx-cc --help for CLI usage |
| `DVR009` | --enable_partitioner may not be used with -O8 | See neuronx-cc --help for CLI usage |
| `DVR010` | Unsupported --allocator '{allocator}' | See neuronx-cc --help for CLI usage |
| `DVR011` | Unspported --optlevel '{optlevel}' | See neuronx-cc --help for CLI usage |
| `DVR012` | Unspported --optlevel '{optlevel}' | See neuronx-cc --help for CLI usage |
| `DVR013` | --enable-call-graph may not be used with -O0 | See neuronx-cc --help for CLI usage |
| `DVR014` | --enable-call-graph may not be used with -O6 | See neuronx-cc --help for CLI usage |
| `DVR015` | --enable-call-graph may not be used with -O8 | See neuronx-cc --help for CLI usage |
| `DVR016` | --enable_partitioner may not be used with -O8 | See neuronx-cc --help for CLI usage |
| `DVR017` | --enable-call-graph may not be used with -O8 | See neuronx-cc --help for CLI usage |
| `DVR018` | Expected subgraph name in the format 'ncXX/sgXX', but got {subgraph_name} | *(contact)* |
| `DVR019` | Expected NC prefix of 'ncXX', but got {nc_name} from subgraph {subgraph_name} | *(contact)* |
| `DVR020` | Expected NC index below {vnc_nc_count}, but got {nc_idx} from subgraph {subgraph_name} | *(contact)* |
| `DVR021` | Linear-scan allocator does not currently support VNC, -O2 or -O3 is recommended for VNC. | See neuronx-cc --help for CLI usage |
| `DVR022` | Expected numeric NC index in {nc_name}, but got {nc_num} in subgraph {subgraph_name} | *(contact)* |
| `DVR023` | Internal error: Duplicate attempts to setup console logging. | *(contact)* |
| `DVR024` | Internal error: Duplicate attempts to setup file logging. | *(contact)* |

#### EAE — EXTERNAL (43 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EAE001` | EngineType::ALL is not supported in legacy loops-on-chip flow | *(contact)* |
| `EAE003` | InstCompareAndBranch must have EngineType::ALL in non-loops-on-chip flow, but it has EngineType::{actual} | *(contact)* |
| `EAE004` | InstCompareAndBranch must not have a RegisterAccess argument before expand_inst_late | *(contact)* |
| `EAE005` | InstCompareAndBranch must have 2 arguments, but it has {actual} | *(contact)* |
| `EAE006` | Unsupported Instruction type on EngineType::ALL: {actual} | *(contact)* |
| `EAE007` | InstUnconditionalBranch must have EngineType::ALL in non-loops-on-chip flow, but it has EngineType::{actual} | *(contact)* |
| `EAE008` | InstUnconditionalBranch must have 0 arguments, but it has {actual} | *(contact)* |
| `EAE009` | InstUnconditionalBranch must have 0 outputs, but it has {actual} | *(contact)* |
| `EAE010` | InstCompareAndBranch must have 0 outputs, but it has {actual} | *(contact)* |
| `EAE011` | InstTensorLoad must have EngineType::ALL in non-loops-on-chip flow, but it has EngineType::{actual} | *(contact)* |
| `EAE012` | InstTensorLoad must have 1 argument, but it has {actual} | *(contact)* |
| `EAE013` | InstTensorLoad must have 1 output, but it has {actual} | *(contact)* |
| `EAE014` | InstTensorLoad argument must be a PhysicalAccessPattern, but it is a {actual} | *(contact)* |
| `EAE015` | InstTensorLoad output must be a RegisterAccess, but it is a {actual} | *(contact)* |
| `EAE016` | InstRegisterAlu must have EngineType::ALL in non-loops-on-chip flow, but it has EngineType::{actual} | *(contact)* |
| `EAE017` | InstRegisterAlu must have 2 arguments, but it has {actual} | *(contact)* |
| `EAE018` | InstRegisterAlu must have 1 output, but it has {actual} | *(contact)* |
| `EAE019` | InstRegisterAlu arguments must be RegisterAccesses, but they are {actual0} and {actual1} | *(contact)* |
| `EAE020` | InstRegisterAlu output must be a RegisterAccess, but it is a {actual} | *(contact)* |
| `EAE021` | InstCompareAndBranch cannot have any dependencies | *(contact)* |
| `EAE022` | InstCompareAndBranch cannot have any descendents | *(contact)* |
| `EAE023` | InstUnconditionalBranch cannot have any dependencies | *(contact)* |
| `EAE024` | InstUnconditionalBranch cannot have any descendents | *(contact)* |
| `EAE025` | InstRegisterMove must have EngineType::ALL in non-loops-on-chip flow, but it has EngineType::{actual} | *(contact)* |
| `EAE026` | InstRegisterMove must have 1 argument, but it has {actual} | *(contact)* |
| `EAE027` | InstRegisterMove must have 1 output, but it has {actual} | *(contact)* |
| `EAE028` | InstRegisterMove argument must be a RegisterAccess or ImmediateValue, but it is a {actual} | *(contact)* |
| `EAE029` | InstRegisterMove output must be a RegisterAccess, but it is a {actual} | *(contact)* |
| `EAE030` | InstCompareAndBranch 1st argument must be a PhysicalAccessPattern/RegisterAccess/ImmediateValue, but it is a {actual} | *(contact)* |
| `EAE031` | InstCompareAndBranch 2nd argument must be a PhysicalAccessPattern/RegisterAccess/ImmediateValue, but it is a {actual} | *(contact)* |
| `EAE032` | Register ({reg}) not found in oldReg2Engine2NewReg! | *(contact)* |
| `EAE033` | InstExit must have EngineType::ALL, but it has EngineType::{actual} | *(contact)* |
| `EAE034` | InstExit must have 0 arguments, but it has {actual} | *(contact)* |
| `EAE035` | InstExit must have 0 outputs, but it has {actual} | *(contact)* |
| `EAE036` | InstExit cannot have any dependencies | *(contact)* |
| `EAE037` | InstExit cannot have any descendents | *(contact)* |
| `EAE038` | Late expansion instruction must have 0 arguments, but it has {actual} | Remove all arguments from Instruction. |
| `EAE039` | Late expansion instruction must have 0 outputs, but it has {actual} | Remove all outputs from Instruction |
| `EAE040` | Late expansion instruction cannot have any dependencies | Remove all dependencies from Instruction. |
| `EAE041` | Late expansion instruction cannot have any descendents | Remove all descendents from Instruction. |
| `EAE042` | Instruction {opcode} must have EngineType::ALL in in non-loops-on-chip flow, but it has EngineType::{actual} | Reassign to ALL so that the instruction can be expanded properly. |
| `EAE043` | Late expansion instruction cannot have any Synchronization info. | Remove all Synchronization information from instruciton. |
| `EAE044` | Instruction {opcode} must have EngineType::ALL in in non-loops-on-chip flow, but it has EngineType::{actual} | Reassign to ALL so that the instruction can be expanded properly. |

#### EARG — EXTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EARG001` | Illegal argument(s) - Logical Neuron Core size {lnc} is not supported on trn1. The specified architecture only supports Logical Neuron Core size of 1. | Update the Logical Neuron Core size to 1 or check that the target architecture is correct. |
| `EARG002` | Illegal argument(s) - the following argument(s) are unrecognized: {unrecognized_args}. | Check that only recognized arguments are used: {usage} |

#### EBCG — EXTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EBCG001` | Custom operations not yet supported for {hardware_target} | *(contact)* |

#### EBIR — EXTERNAL (21 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EBIR006` | Integer TensorTensor Ops has PSUM argument. | Move argument to SB to allow for execution on GPSIMD engine (which cannot access PSUM), or change operands to float so they can execute on Vector Engine |
| `EBIR010` | All of InstRegisterAlu's input registers must have the same bitwidth. Got {str} | Ensure all Register inputs to InstRegisterAlu have the same bitwidth |
| `EBIR011` | InstTensorLoad/InstTensorSave register and tensor must have the same number of bytes. Got src={src_type} dst={dst_type} | Ensure a 32-bit index is accompanied by a 32-bit register, and a 64-bit index is accompanied by a 64-bit register |
| `EBIR012` | InstRegisterMove src and dst must have the same bitwidth. Got src={src_type}, dst={dst_type} | Ensure InstRegisterMove's input and output have the same type (or at least the same number of bits) |
| `EBIR013` | InstRegisterAlu with 64-bit operands can only perform AluOpType::add, subtract, or mult. Got AluOpType::{actual} | Use 32-bit math or use one of add/subtract/mult |
| `EBIR014` | NKI Kernel call references undefined Kernel | Ensure the NKI kernel call references a valid Kernel. |
| `EBIR015` | NKI Kernel number of partitions {kernel_num_partitions} doesn't match architecture number of partitions {arch_num_partitions}. This is not supported today. | Update the kernel's SBUF num partitions to match the target architecture num partitions. |
| `EBIR016` | NKI Kernel has invalid SBUF shape: sb_num_partitions={sb_num_partitions}, sb_per_partition_size={sb_per_partition_size}. | If SBUF is allocated (non-zero size per partition), there must be at least one partition. |
| `EBIR017` | NKI Kernel PSUM bank count {psum_bank_count} exceeds architecture limit of {arch_psum_banks}. | Reduce the number of PSUM banks requested by the kernel. |
| `EBIR018` | NKI Kernel PSUM partition count {psum_num_partitions} exceeds architecture limit of {arch_num_partitions}. | Reduce the number of PSUM partitions requested by the kernel. |
| `EBIR019` | NKI Kernel has insufficient outputs: {actual_outputs} provided but {expected_min_outputs} required ({num_sb_buffers} SBUF + {num_psum_buffers} PSUM buffers). | Add the required SBUF / PSUM outputs to the kernel. |
| `EBIR020` | NKI Kernel has invalid address rotation scope: {scope}. | Address rotation scope must be None (0), Global (1), or Kernel (2). |
| `EBIR021` | Invalid architecture revision v2 on {archlevel} | Revision v2 is only available on trn3 |
| `EBIR022` | DVE exponential is not supported on trn3pre | Move Exponential operation from Vector to Scalar engine, or use '--target trn3' |
| `EBIR023` | MLP kernel intermediate size {intermediate_size} exceeds the maximum supported value of 4096. | Consider tiling large intermediate tensors in your kernel to stay within the supported limit, or increase tensor parallelism to shard the intermediate dimension across more cores. |
| `EBIR024` | Invalid dtype for SelectReduce input on_true tensor. Given: src type: {src_type} | Use a supported dtype such as uint8, int8, uint16, int16, float16, bfloat16, float32, or float8 variants (float8e3, float8e4, float8_e4m3fn, float8e5) |
| `EBIR025` | Invalid dtype for SelectReduce input predicates tensor. Given: predicate type: {pred_type} | Use a supported dtype such as uint8, int8, uint16, int16, uint32, int32 for predicate tensors. |
| `EBIR030` | InstDMACopy number of in dimensions ({in}) must equal the number of out dimensions ({out}) | Make sure the in/out AccessPatterns have the same number of dimensions |
| `EBIR031` | Number of elements in dimension {index} of InstDMACopy's input and output AccessPatterns must match, but got in={in} and out={out} | Make sure the number of elements is the same in all dimensions of the in/out AccessPatterns |
| `EBIR032` | InstDMACopy number of in dimensions ({in}) must equal the number of out dimensions ({out}) | Make sure the in/out AccessPatterns have the same number of dimensions |
| `EBIR033` | Number of elements in dimension {index} of InstDMACopy's input and output AccessPatterns must match, but got in={in} and out={out} | Make sure the number of elements is the same in all dimensions of the in/out AccessPatterns |

#### EBRD — EXTERNAL (3 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EBRD001` | Mismatched number of modules {num_modules} and condensed BIR input files {num_condensed} | Make sure there is 1 condensed file input for each module (only 1 module currently supported) |
| `EBRD002` | Unable to open condensed format file {file_name} | Ensure your condensed .bir file exists and that the full path is provided |
| `EBRD003` | Unable to find instruction in condensed format file | Try regenerating your condensed format file? |

#### EBVF — EXTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EBVF030` | Instructions generated by compiler {total_count} exceeds the typical limit of {max_instruction_limit}. Input computation graph is too big due to large operators | Consider using smaller batches or sequence length, or applying tensor parellelism. For further troubleshooting visit https://awsdocs-neuron.readthedocs-hosted.com/en/latest/libraries/nxd-training/app_notes/nxd-training-tp-appnote.html |

#### EDGE — EXTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EDGE001` | DMAs that use HWDGE can only be assigned to EngineType::Activation (nki.isa.engine.scalar) or EngineType::SP (nki.isa.engine.sync), but got EngineType::{actual} | If you want this DMA to use HWDGE, set its engine to `engine=nki.isa.engine.scalar` or `engine=nki.isa.engine.sync` instead. If you want this DMA to be on the engine you've selected, then do not set `dge_mode=dge_mode.hwdge`. |

#### EDMA — EXTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EDMA002` | Kernel partition size {kernel_num_partitions} does not match architecture partition size {arch_num_partitions}. | Ensure the kernel's partition configuration matches the target architecture. |

#### EDP — EXTERNAL (3 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EDP001` | InstDevicePrint must have 1 argument, but it has {actual} | *(contact)* |
| `EDP002` | InstDevicePrint argument must be a PhysicalAccessPattern, but it is a {actual} | *(contact)* |
| `EDP003` | InstDevicePrint can only print a tensor in HBM or SBUF, but tensor {name} is in {type} | *(contact)* |

#### EDRV — EXTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EDRV080` | No subgraph directories (sgXX) found in NC directory: {nc_dir} | *(contact)* |

#### EDVR — EXTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EDVR001` | Transpose DMA with dynamic offset cannot exist without transpose DGE level enabled | Please add `transpose` to the `--internal-enable-dge-levels` flag |

#### EGCA — EXTERNAL (7 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EGCA110` | Memory allocation failed for {memloc}. Spilling is disabled for kernels today. | Please reduce memory usage by optimizing tensor sizes or splitting operations into smaller chunks. |
| `EGCA111` | Memory allocation failed for {memloc}. Spilling is disabled for kernels today. | Please reduce memory usage by optimizing tensor sizes or splitting operations into smaller chunks. |
| `EGCA112` | Kernel {function} requires loop unrolling for proper memory allocation in graph compiler flow. | Please use new NKI flow when doing partially allocated kernel. |
| `EGCA113` | Kernel {function} requires loop unrolling for proper memory allocation in graph compiler flow. | Please use new NKI flow when doing partially allocated kernel. |
| `EGCA114` | Conflicting partition constraints detected in the same constraint group. | Please examine tensor reader/writer to ensure they do not have conflicting constraint requirements. |
| `EGCA115` | If partition num > 96, tensor {tensor} base partition constraint must be 0 | Ensure >96 partition tensors do not have non-0 tile position. |
| `EGCA116` | Conflicting base partition constraints from matmul instructions. | Tensors accessed by matmuls have incompatible partition requirements based on tile positions. |

#### EIL — EXTERNAL (11 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EIL001` | InstCompareAndBranch should have exactly 2 arguments (got {actual}) | *(contact)* |
| `EIL002` | InstCompareAndBranch can only handle PhysicalAccessPatterns (got a {actual}) | *(contact)* |
| `EIL003` | Loop condition MemoryLocation '{name}' must be <= 4 bytes (it is {actual_size} bytes) | *(contact)* |
| `EIL004` | Loop condition MemoryLocation '{name}' must have exactly 1 element (it has {actual_size} elements) | *(contact)* |
| `EIL005` | InstCompareAndBranch cannot be on the ALL engine, it should have been expanded by expand_all_engine | *(contact)* |
| `EIL006` | InstCompareAndBranch cannot have any dependencies | *(contact)* |
| `EIL007` | InstCompareAndBranch cannot have any descendents | *(contact)* |
| `EIL008` | InstCompareAndBranch that is not expanded by expand_inst_late must not have SyncInfo | *(contact)* |
| `EIL009` | No dynamic axis register (IntRuntimeValue) found in expression with hasRuntimeValue true! | *(contact)* |
| `EIL010` | InstCompareAndBranch must only have ordered dependencies, but it has a {actual} dependency | *(contact)* |
| `EIL011` | Register within dynamic axis of instruction does not match engine of instruction! | *(contact)* |

#### ELMM — EXTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ELMM001` | Matrix Multiplication with write indirection of type bfloat16 cannot use less quadrants than previous matrix multiplications | Before the culprit matrix multiplication, insert a dummy matrix multiplication that covers all quadrants |

#### ELUR — EXTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ELUR015` | Instructions generated by compiler {total_count} exceeds the typical limit of {max_instruction_limit}. Input computation graph is too big due to large operators | Consider using smaller batches or sequence length, or applying tensor parellelism. For further troubleshooting visit https://awsdocs-neuron.readthedocs-hosted.com/en/latest/libraries/nxd-training/app_notes/nxd-training-tp-appnote.html |

#### ENKI — EXTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ENKI001` |  |  |

#### EOCP — EXTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EOCP001` | Tensor {legacy_tensor} has legacy type {legacy_type} but tensor {ocp_tensor} has OCP-compliant type {ocp_type}. Mixed use of the two mutually-exclusive types is not supported | Ensure that only one of the two types is used. |

#### EOOM — EXTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EOOM001` | Maximum peak HBM usage of {usage} (at instruction {inst}) exceeds HBM limit of {maxsize} for {arch}. This consists of {hbm_io} I/O tensors, {hbm_intermediate} intermediate tensors, and {hbm_internal} internal (scratchpad) allocations (of which {sbuf} is anticipated spills from SBUF) | Check the target architecture flag to ensure you are compiling for correct target, or consider using smaller batches or applying model/tensor parallelism. |
| `EOOM002` | Maximum peak HBM usage of {usage} exceeds HBM limit of {maxsize} for {arch}. This consists of {constants} model constants, {unallocated} I/O tensors, {allocated} internal (scratchpad) tensors, {dma_ring_io} DMA ring I/O, and {dma_ring_spill} DMA ring spills | Check the target architecture flag to ensure you are compiling for correct target, or consider using smaller batches or applying model/tensor parallelism. |

#### ERP — EXTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ERP001` | {inst_name}: Replication not supported for MatmultMX instructions | *(contact)* |

#### ESL — EXTERNAL (17 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ESL001` | Expected to find a first definition for shared tensor {name} on at least one core. | *(contact)* |
| `ESL002` | Expected to find a definition before read for shared tensor {name}. | *(contact)* |
| `ESL003` | Expected shared memory definition for {name} to have a synchronization primitive before it. | *(contact)* |
| `ESL004` | Expected shared memory location for {name} on core {core_id}, but found none. | *(contact)* |
| `ESL005` | Expected core barrier ahead of shared memory definition for {name}, but none found on core {core_id}. | *(contact)* |
| `ESL006` | Expected shared memory reference for {name} to have a synchronization primitive after it. | *(contact)* |
| `ESL007` | Expected shared memory location for {name} on core {core_id}, but found none. | *(contact)* |
| `ESL008` | Expected core barrier after shared memory read for {name}, but none found on core {core_id} exists. | *(contact)* |
| `ESL009` | Expected to find {expected_count} functions in {sgId} modules for all cores, but found {actual_count} in module for core {ncId}. | *(contact)* |
| `ESL010` | Expected to find a function named {function_name} in {sgId} modules for all cores, but it does not exist in module for core {ncId}. | *(contact)* |
| `ESL011` | Expected the {function_name} function in {sgId} modules of all cores to have {expected_size} blocks, but core {ncId} has {actual_size} blocks. | *(contact)* |
| `ESL012` | Did not expect any shared internal tensors to be live on input to function {functionName} in subgraph {sgId}, but found {liveInShared} live. | *(contact)* |
| `ESL013` | Expected to find shared tensor {name}, but it is missing in function {functionName} in subgraph {sgId} on core {ncId}. | *(contact)* |
| `ESL014` | Encountered an incomplete GPSIMDSB2SB instruction | *(contact)* |
| `ESL015` | Encountered an incomplete GPSIMDSB2SB instruction | *(contact)* |
| `ESL016` | Encountered an incomplete GPSIMDSB2SB instruction | *(contact)* |
| `ESL017` | Encountered an incomplete GPSIMDSB2SB instruction | *(contact)* |

#### ESMP — EXTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ESMP001` | Constant simplification would produce negative index values. This will cause out-of-bounds access. Tensor: {tensor_name}, Op ID: {op_id}, Const values: {const_values} | Review the indexing logic to ensure indices are non-negative. Check the base offset and step calculations in the index expression. |
| `ESMP002` | Constant simplification would produce index values (max: {max_index}) exceeding the target tensor dimension size ({dim_size}) at dimension {dim}. This will cause out-of-bounds access. Source tensor: {tensor_name}, Target tensor: {target_tensor_name}, Op ID: {op_id}, Const values: {const_values} | Review the indexing logic to ensure indices remain within the bounds of the target tensor dimension being indexed. Check that the index values are appropriate for the tensor shape at the specified dimension. |

#### ETST — EXTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ETST001` | Example external error message 1. | *(contact)* |

#### EXSP — EXTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EXSP001` | Size of HBM memory required for the model exceeds HBM limit of {hardware_target}. Needed more than {usage_in_comma} ({usage_in_gb}GB) vs. available bytes {threshold_in_comma} ({threshold_in_gb}GB). This may be due to compiler allocating high scratch pad memory for this model. | Check the target architecture flag to ensure you are compiling for correct target, or consider using smaller batches or applying model/tensor parallelism. |

#### EXTP — EXTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `EXTP003` | Instructions generated by compiler {insts} exceeds the typical limit of {threshold}. Input computation graph is too big due to large operators | Consider using smaller batches or sequence length, or applying tensor parellelism. For further troubleshooting visit https://awsdocs-neuron.readthedocs-hosted.com/en/latest/libraries/nxd-training/app_notes/nxd-training-tp-appnote.html |
| `EXTP004` | Instructions generated by compiler {insts} exceeds the typical limit of {threshold}. Input computation graph is too big due to a large number of operators | Consider using compiler options --optlevel=1, or applying pipeline parallelism. For further troubleshooting visit https://awsdocs-neuron.readthedocs-hosted.com/en/latest/libraries/nxd-training/app_notes/nxd-training-pp-appnote.html. |

#### GCA — bare (89 codes)

| Code | Cause | Resolution |
|---|---|---|
| `GCA013` | couldn't allocate every tensor in PSUM, likely due to illegal accumulation group ! | *(contact)* |
| `GCA014` | Spill insertion at instruction {inst}, illegal accumulation group! | *(contact)* |
| `GCA022` | DRAM usage for Internal DRAM tensor exceeds {hbm_limit}GB of device space limit, cannot fit into device, model requires too much HBM memory ! | *(contact)* |
| `GCA023` | Allocation of {loc} crossing runtime page boundary with address range of [{long_address},{end_address}) | *(contact)* |
| `GCA024` | Illegal IR, encountered undefined use: {first_bad_use}! | *(contact)* |
| `GCA025` | Allocator bit_matrix and adjacency vectors require {required_mem} bytes, which is more than 64GB limit | *(contact)* |
| `GCA026` | Unrecognized architecture: {arch} | *(contact)* |
| `GCA027` | Expected reload to PSUM | *(contact)* |
| `GCA028` | Expected reload to PSUM | *(contact)* |
| `GCA029` | Unrecognized architecture: {arch} | *(contact)* |
| `GCA030` | {error_message} | *(contact)* |
| `GCA031` | {error_message} | *(contact)* |
| `GCA032` | Illegal tensor height | *(contact)* |
| `GCA034` | Illegal tensor height | *(contact)* |
| `GCA035` | {error_message} | *(contact)* |
| `GCA036` | Unexpected value for ml_base = {ml_base} | *(contact)* |
| `GCA037` | {node} is too big for SB, requires {bytes_per_partition} bytes | *(contact)* |
| `GCA038` | The state buffer: {stateBuffer}\\nin sb_select, a bad allocation\\n  {node_index} {node_name} [{first_address} {last_address}) x [{first_partition}  {last_partition})\\n  {neighbor} {neighbor_name} [{firsta} {lasta}) x [{firstp} {lastp})\\n | *(contact)* |
| `GCA039` | In sb_select, a call to allocate() failed, in {memloc_name} | *(contact)* |
| `GCA040` | In sb_select, a call to allocate() failed, in {memloc_name} | *(contact)* |
| `GCA041` | Illegal tensor height | *(contact)* |
| `GCA042` | Expected reload to SB | *(contact)* |
| `GCA043` | Unexpected value for ml_base = {ml_base} | *(contact)* |
| `GCA044` | {error_message} | *(contact)* |
| `GCA045` | Couldn't color the DRAM even with 100GB of DRAM space assumption, model needs too much HBM memory ! | *(contact)* |
| `GCA046` | Some infinite-cost nodes remain = {size} | *(contact)* |
| `GCA047` | Cannot spill must-be-pinned or pre-allocated tensor, spilled tensor = {tensor_name} | *(contact)* |
| `GCA048` | Instruction must have exactly 1 argument | *(contact)* |
| `GCA049` | Instruction must have exactly 1 output | *(contact)* |
| `GCA050` | Unexpected SB allocator run after linking | *(contact)* |
| `GCA051` | Unexpected address space {addr_space} for SB allocator | *(contact)* |
| `GCA052` | Unexpected PSUM allocator run after linking | *(contact)* |
| `GCA053` | Unexpected address space {addr_space} for PSUM allocator | *(contact)* |
| `GCA054` | Illegal IR, attempt to use improperly defined tensor | *(contact)* |
| `GCA055` | Illegal IR, encountered undefined use: {first_bad_use}, and unused define: {first_bad_def}! | *(contact)* |
| `GCA056` | Illegal IR, encountered unused define: {first_bad_def}! | *(contact)* |
| `GCA057` | Identity matrix of dtype {dtype} not found | *(contact)* |
| `GCA058` | Could not find memloc {loc_name} in accumulation group cache | *(contact)* |
| `GCA059` | Could not find next use | *(contact)* |
| `GCA060` | Next use instruction {instruction_name} has not been visited | *(contact)* |
| `GCA061` | Illegal first use of internal tensor | *(contact)* |
| `GCA062` | Could not find next use | *(contact)* |
| `GCA063` | Next use instruction {instruction_name} has not been visited | *(contact)* |
| `GCA064` | Illegal first use of internal tensor | *(contact)* |
| `GCA065` | Shouldn't need initial loads with a single basic block | *(contact)* |
| `GCA066` | Cannot have both transpose MM and MM write to the same memloc | *(contact)* |
| `GCA067` | Could not find first accumulation group inst of memloc | *(contact)* |
| `GCA068` | Tried to insert PSUM spill at end of block | *(contact)* |
| `GCA069` | Illegal PSUM spilling instruction type for instruction {spill_instruction_name} | *(contact)* |
| `GCA070` | Illegal PSUM spilling instruction type for instruction {spill_instruction_name} | *(contact)* |
| `GCA071` | Tried to insert PSUM spill at end of block | *(contact)* |
| `GCA072` | Tried to insert reload to PSUM at end of block | *(contact)* |
| `GCA073` | Expected identity matrix to be in DRAM but got {mem_type} | *(contact)* |
| `GCA074` | Cannot have both transpose MM and MM write to the same memloc | *(contact)* |
| `GCA075` | Expected a 128x128 identity matrix but got total size {size_in_elements} | *(contact)* |
| `GCA076` | Expected reload to PSUM | *(contact)* |
| `GCA077` | Identity matrix is too small. Need num_partitions at least {lhs_mm_num_partitions} but got {identity_sb_num_partitions} | *(contact)* |
| `GCA078` | {memloc_name}: Allocated base partition mismatch in MatMult output location | *(contact)* |
| `GCA079` | {inst_name}: Allocated base partition + access pattern must be <= PE array size | *(contact)* |
| `GCA080` | {memloc_name}: PSUM memory location accessed partitions must be > 0 | *(contact)* |
| `GCA081` | {memloc_name}: PSUM memory location cannot have uninitialized DebugInfo | *(contact)* |
| `GCA082` | {memloc_name}: location must have valid def/use pair | *(contact)* |
| `GCA083` | {memloc_name}: location has no access in current BB. Need at least one access if location is only in live-in | *(contact)* |
| `GCA084` | {memloc_name}: location has no access in current BB. Need at least one access if location is only in live-out | *(contact)* |
| `GCA085` | {memloc_name}: location must have valid def/use pair | *(contact)* |
| `GCA086` | {memloc_name}: find no live interval for global location | *(contact)* |
| `GCA087` | {memloc_name}: location with only use and no def at allocation stage | *(contact)* |
| `GCA088` | {inst_name}: Requesting more PSUM memory than hardware constraint | *(contact)* |
| `GCA089` | Live-in registers are only supported in legacy loops-on-chip flow. Register {reg_name} defined by Instruction {def_name} in BasicBlock {def_block_name} is last used by Instruction {use_name} in BasicBlock {use_block_name} | *(contact)* |
| `GCA090` | {memloc_name}: no def/use pair find for memorylocation | *(contact)* |
| `GCA091` | {memloc_name}: Currently cannot spill multiple-BB access memorylocation | *(contact)* |
| `GCA092` | {memloc_name}: Currently cannot spill multiple-BB access memorylocation | *(contact)* |
| `GCA093` | {memloc_name}: location has no definition | *(contact)* |
| `GCA094` | Unable to allocate register '{reg}' on engine '{engine}' because there are too many registers live concurrently. Register spill/reload is not supported | *(contact)* |
| `GCA095` | {reg_name}: register with only use and no def at allocation stage | *(contact)* |
| `GCA096` | {reg_name}: register has no definition | *(contact)* |
| `GCA097` | {reg_name}: register has no access in current BB. Need at least one access if register is only in live-in | *(contact)* |
| `GCA098` | {reg_name}: register has no access in current BB. Need at least one access if register is only in live-out | *(contact)* |
| `GCA099` | {reg_name}: find no live interval for global register | *(contact)* |
| `GCA100` | {reg_name}: register must have valid def/use pair | *(contact)* |
| `GCA101` | Must have valid first_def and last_use. {error_str} | *(contact)* |
| `GCA102` | {bb_name}: Strongly connected graph cannot be empty | *(contact)* |
| `GCA103` | {memloc_name}: Cannot spill memorylocation defined by DMA skipping inside of loop, without initial define outside of loop. Please do a dummy define outside of loop body for this tensor | *(contact)* |
| `GCA104` | No def/use pair found for register '{reg_name}' | *(contact)* |
| `GCA105` | Register '{reg_name}' must have valid def/use pair | *(contact)* |
| `GCA106` | Each register must have at least one live interval. There are {num_regs} registers but only {num_intervals} intervals. | *(contact)* |
| `GCA107` | GCA reload instruction can only be inserted in basicblock before next user | *(contact)* |
| `GCA108` | {memloc_name}:Tensor with loop-carried depednency has to be defined outside of loop | *(contact)* |
| `GCA109` | Infinite memloc picked to spill, cannot allocate. memloc name {mam_name}. This is due to single instruction accessing memlocs needing more than total PSUM size. | *(contact)* |

#### IACF — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IACF901` | AutoCastFP32 assertion error: {baseMessage} | *(contact)* |
| `IACF902` | AutoCastFP32 error: {baseMessage} | *(contact)* |

#### IACI — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IACI901` | AutoCastInputs assertion error: {baseMessage} | *(contact)* |
| `IACI902` | AutoCastInputs error: {baseMessage} | *(contact)* |

#### IACT — INTERNAL (14 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IACT001` | Expect that the start time of previous activation instruction {prevInst} to be less than or equal to the current activation instruction {currInst}, but it is not. | *(contact)* |
| `IACT002` | The activation table load move candidate should not be nullptr | *(contact)* |
| `IACT003` | The activation table load instruction should not be nullptr | *(contact)* |
| `IACT005` |  |  |
| `IACT006` |  |  |
| `IACT007` |  |  |
| `IACT008` |  |  |
| `IACT009` |  |  |
| `IACT010` |  |  |
| `IACT011` |  |  |
| `IACT012` |  |  |
| `IACT013` |  |  |
| `IACT901` | AutoCastTCInputs assertion error: {baseMessage} | *(contact)* |
| `IACT902` | AutoCastTCInputs error: {baseMessage} | *(contact)* |

#### IADE — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IADE901` | AliasDependencyElimination assertion error: {baseMessage} | *(contact)* |
| `IADE902` | AliasDependencyElimination error: {baseMessage} | *(contact)* |

#### IADI — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IADI901` | AliasDependencyInduction assertion error: {baseMessage} | *(contact)* |
| `IADI902` | AliasDependencyInduction error: {baseMessage} | *(contact)* |

#### IADQ — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IADQ901` | AssignDMAQoSLabels assertion error: {baseMessage} | *(contact)* |
| `IADQ902` | AssignDMAQoSLabels error: {baseMessage} | *(contact)* |

#### IADR — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IADR901` | AliasDependencyReset assertion error: {baseMessage} | *(contact)* |
| `IADR902` | AliasDependencyReset error: {baseMessage} | *(contact)* |

#### IADV — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IADV901` | AliasDependencyVerificationPass assertion error: {baseMessage} | *(contact)* |
| `IADV902` | AliasDependencyVerificationPass error: {baseMessage} | *(contact)* |

#### IAGO — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IAGO901` | AGOrderingAnalysis assertion error: {baseMessage} | *(contact)* |
| `IAGO902` | AGOrderingAnalysis error: {baseMessage} | *(contact)* |

#### IAHE — INTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IAHE001` |  |  |

#### IALB — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IALB901` | AllocateBlocks assertion error: {baseMessage} | *(contact)* |
| `IALB902` | AllocateBlocks error: {baseMessage} | *(contact)* |

#### IALS — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IALS001` |  |  |
| `IALS002` |  |  |

#### IANK — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IANK901` | AnalyzeKernel assertion error: {baseMessage} | *(contact)* |
| `IANK902` | AnalyzeKernel error: {baseMessage} | *(contact)* |

#### IANS — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IANS901` | AnnotateNoSpill assertion error: {baseMessage} | *(contact)* |
| `IANS902` | AnnotateNoSpill error: {baseMessage} | *(contact)* |

#### IAPR — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IAPR901` | AffinePredicateResolution assertion error: {baseMessage} | *(contact)* |
| `IAPR902` | AffinePredicateResolution error: {baseMessage} | *(contact)* |

#### IATL — INTERNAL (40 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IATL001` |  |  |
| `IATL002` |  |  |
| `IATL003` |  |  |
| `IATL004` |  |  |
| `IATL005` |  |  |
| `IATL006` |  |  |
| `IATL007` |  |  |
| `IATL008` |  |  |
| `IATL009` |  |  |
| `IATL010` |  |  |
| `IATL011` |  |  |
| `IATL012` |  |  |
| `IATL013` |  |  |
| `IATL014` |  |  |
| `IATL015` |  |  |
| `IATL016` |  |  |
| `IATL017` |  |  |
| `IATL018` |  |  |
| `IATL019` |  |  |
| `IATL020` |  |  |
| `IATL021` |  |  |
| `IATL022` |  |  |
| `IATL023` |  |  |
| `IATL024` |  |  |
| `IATL025` |  |  |
| `IATL026` |  |  |
| `IATL027` |  |  |
| `IATL028` |  |  |
| `IATL029` |  |  |
| `IATL030` |  |  |
| `IATL031` |  |  |
| `IATL032` |  |  |
| `IATL033` |  |  |
| `IATL034` |  |  |
| `IATL035` |  |  |
| `IATL036` |  |  |
| `IATL037` |  |  |
| `IATL038` |  |  |
| `IATL039` |  |  |
| `IATL040` |  |  |

#### IATN — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IATN901` | Autotuner assertion error: {baseMessage} | *(contact)* |
| `IATN902` | Autotuner error: {baseMessage} | *(contact)* |

#### IBCC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IBCC901` | BucketizeCCOp assertion error: {baseMessage} | *(contact)* |
| `IBCC902` | BucketizeCCOp error: {baseMessage} | *(contact)* |

#### IBCG — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IBCG901` | BIRCodeGenLoop assertion error: {baseMessage} | *(contact)* |
| `IBCG902` | BIRCodeGenLoop error: {baseMessage} | *(contact)* |

#### IBFC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IBFC901` | BFComputeCutting assertion error: {baseMessage} | *(contact)* |
| `IBFC902` | BFComputeCutting error: {baseMessage} | *(contact)* |

#### IBIR — INTERNAL (55 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IBIR094` | All Matmult tiling parameters must have matching sizes. Given: Size={row_size}x{col_size}, Position={row_pos}x{col_pos} | Adjust Matmult inputs to have matching expressions. |
| `IBIR158` | Access pattern out of bounds on instruction '{inst}'. Pattern: {pattern} | *(contact)* |
| `IBIR238` | Instruction Activate2 is supported only for trn3 architecture. Got architecture: {arch} | *(contact)* |
| `IBIR239` | Activate2 params imm0, dtype: {imm0_dtype}; imm1, dtype: {imm1_dtype}; relu_param, dtype: {relu_param_dtype} have mismatching dtypes | *(contact)* |
| `IBIR240` | Invalid operator combination ({op0}, {op1}) for Activate2. Valid operator params are (mult,add), (mult, subtract), (mult, bypass), (add, bypass), (subtract, bypass), (bypass, bypass) | *(contact)* |
| `IBIR241` | Invalid accumulation type: {acc}. Must be one of Idle, Zero, AddAccumulate or ZeroAccumulate | *(contact)* |
| `IBIR242` | Expected src_tensor (quadrant: {src_quadrant}), imm0 (quadrant: {imm0_quadrant}), imm1 (quadrant: {imm1_quadrant}) and relu_param (quadrant: {relu_param_quadrant}) to be in the same sbuf quadrant | *(contact)* |
| `IBIR243` | Access pattern out of bounds. Pattern: {pattern} | *(contact)* |
| `IBIR246` |  |  |
| `IBIR265` |  |  |
| `IBIR266` |  |  |
| `IBIR267` |  |  |
| `IBIR269` |  |  |
| `IBIR270` |  |  |
| `IBIR272` |  |  |
| `IBIR273` |  |  |
| `IBIR274` |  |  |
| `IBIR275` |  |  |
| `IBIR276` |  |  |
| `IBIR277` |  |  |
| `IBIR279` |  |  |
| `IBIR280` | Function {func} requires at least one BasicBlock. | *(contact)* |
| `IBIR281` | Register '{name}' which uses {num} physical 32-bit registers must have its lowest physical register allocated to an even ID, but it is '{actual}' | *(contact)* |
| `IBIR282` |  |  |
| `IBIR283` | DMA Transpose is not allowed for MX data types. | Consider using another data type instead. |
| `IBIR284` | DMA non-bypass CCE is not allowed for MX data types. | Consider using another data type instead. |
| `IBIR285` | {instruction_type_name} instructions don't support data type casting for MX data types (src data type: {in_dtype}, dst data dtype: {out_dtype}). | Consider using another data type instead. |
| `IBIR286` | MX data type is not supported for compute instructions other than QuantizeMx and MatmultMx but got {instruction_type_name}. | Consider using another data type instead. |
| `IBIR289` |  |  |
| `IBIR290` | bir::PhysicalAccessPattern::getAddresses forbids setting startingAddress >= 0 and perPartition = false! | *(contact)* |
| `IBIR291` | PE Tile position and size must both be fully specified | *(contact)* |
| `IBIR292` |  |  |
| `IBIR302` | Instruction has invalid tile group row position {tileGroupRowPosition}, should be one of 0, 32, 64, 96 | *(contact)* |
| `IBIR303` | Instruction has invalid tile group row position {tileGroupColPosition}, should be one of 0, 32, 64, 96 | *(contact)* |
| `IBIR304` | Instruction has invalid row tile position ({tileGroupRowPosition}) and tile size ({tileGroupRowSize}) specified, expect tile size to be one of {possibleTileSizes} at tile position | *(contact)* |
| `IBIR305` | Instruction has invalid col tile position ({tileGroupColPosition}) and tile size ({tileGroupColSize}) specified, expect tile size to be one of {possibleTileSizes} at tile position | *(contact)* |
| `IBIR308` |  |  |
| `IBIR313` |  |  |
| `IBIR314` |  |  |
| `IBIR315` |  |  |
| `IBIR316` |  |  |
| `IBIR317` |  |  |
| `IBIR318` |  |  |
| `IBIR319` |  |  |
| `IBIR320` |  |  |
| `IBIR322` |  |  |
| `IBIR351` |  |  |
| `IBIR352` |  |  |
| `IBIR353` |  |  |
| `IBIR354` |  |  |
| `IBIR355` |  |  |
| `IBIR356` | Unsupported expression kind ({kind}) in visitor | *(contact)* |
| `IBIR357` |  |  |
| `IBIR366` |  |  |
| `IBIR367` |  |  |

#### IBRD — INTERNAL (4 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IBRD001` | Duplicate instruction name {inst_name} found in condensed format file | Changes in condensed format dumping or parsing are probably needed |
| `IBRD002` | Instruction has no source line | Possible issue in source map loading |
| `IBRD003` | Unreachable debugger stop reason in print | Update method to handle new reasons |
| `IBRD004` | Core index out of bounds | Check core index is within valid range |

#### IBRW — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IBRW901` | BroadcastWeights assertion error: {baseMessage} | *(contact)* |
| `IBRW902` | BroadcastWeights error: {baseMessage} | *(contact)* |

#### IBSN — INTERNAL (15 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IBSN001` | PoisonTracker::mergePoison argument [const boost::icl::interval_set<uint64_t>& boundingIntervals] must fully contain argument [const boost::icl::interval_set<uint64_t>& poisonedIntervals] | *(contact)* |
| `IBSN002` | PoisonTracker::checkPoisonedBytesNotNullptr assertion failed! Called from {callee} | *(contact)* |
| `IBSN008` | birsan::InstVisitor::elementToElementMap expects the same number of elements in the inputAP and the outputAP (got {inputNumElements} != {outputNumElements}) | *(contact)* |
| `IBSN009` | birsan::InstVisitor::transformReduction produced and invalid outputIdx from inputIdxToOutputIdxs (inputIdxToOutputIdxs({inputIdx}) = {outputIdx} >= {outputIdxMax}) | *(contact)* |
| `IBSN010` | birsan::InstVisitor::elementToElementByteCopy produced and invalid inputIdx from outputIdxToInputIdxMap (outputIdxToInputIdxMap[{outputIdx}] = {inputIdx} >= {inputIdxMax}) | *(contact)* |
| `IBSN011` | birsan::InstVisitor::transformSelect expected predsAddresses.size() == dstAddresses.size(), but got {predsSize} != {dstSize} | *(contact)* |
| `IBSN012` | birsan::InstVisitor::handleDMAVectorIndirect does not support both vectorSrcIndirect and vectorDstIndirect | *(contact)* |
| `IBSN013` | birsan::InstVisitor::handleDMATranspose found InstDMACopy without bir::CopyMode::Transpose | *(contact)* |
| `IBSN014` | birsan::InstVisitor::handleDMATranspose only handles InstDMACopy with bir::CopyMode::Transpose and InstDMADescriptorTranspose | *(contact)* |
| `IBSN015` | birsan::InstVisitor::handleDMAReplicate found InstDMACopy without bir::CopyMode::Replicate | *(contact)* |
| `IBSN016` | birsan::InstVisitor::handleDMAReplicate only handles InstDMACopy with bir::CopyMode::Replicate and InstDMADescriptorReplicate | *(contact)* |
| `IBSN017` | birsan::InstVisitor::handleDMAReplicate requires input/output APs to have two dimensions | *(contact)* |
| `IBSN018` | birsan::InstVisitor::handleDMACCE found InstDMACopy without bir::CopyMode::CCE | *(contact)* |
| `IBSN019` | birsan::InstVisitor::handleDMACCE only handles InstDMACopy with bir::CopyMode::CCE and InstDMADescriptorCCE | *(contact)* |
| `IBSN020` | birsan::InstVisitor::transformBasicBlockHolder expects a bir::InstructionBasicBlockHolder | *(contact)* |

#### IBVF — INTERNAL (9 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IBVF032` | The bias argument must be of type float32 when activation function type is of Copy or Reciprocal, but is {dtype} | Check that the activation function type and bias arugment dtype |
| `IBVF033` | The bias argument must be of type float32 on CoreV2+, but is {dtype} | Check that the activation instruction bias arugment dtype |
| `IBVF034` |  |  |
| `IBVF036` | The bias argument must be scalar or of shape at least N ({input_partitions}) x 1 but has shape {shape} | Check the activation instruction bias argument shape |
| `IBVF037` | The bias argument tensor may not be indirect | Use a non-indirect tensor for bias |
| `IBVF038` | Input and output dtypes must be valid and the same for non-bitvec operations: {inputDtype} != {outputDtype} | *(contact)* |
| `IBVF039` |  |  |
| `IBVF040` |  |  |
| `IBVF041` |  |  |

#### ICCC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ICCC901` | CoalesceCCOp assertion error: {baseMessage} | *(contact)* |
| `ICCC902` | CoalesceCCOp error: {baseMessage} | *(contact)* |

#### ICCF — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ICCF901` | CCOpFusion assertion error: {baseMessage} | *(contact)* |
| `ICCF902` | CCOpFusion error: {baseMessage} | *(contact)* |

#### ICDG — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ICDG901` | CanonicalizeDAG assertion error: {baseMessage} | *(contact)* |
| `ICDG902` | CanonicalizeDAG error: {baseMessage} | *(contact)* |

#### ICDL — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ICDL901` | ConcatDelinearizer assertion error: {baseMessage} | *(contact)* |
| `ICDL902` | ConcatDelinearizer error: {baseMessage} | *(contact)* |

#### ICIO — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ICIO901` | ConvertIOBufferToMustAlias assertion error: {baseMessage} | *(contact)* |
| `ICIO902` | ConvertIOBufferToMustAlias error: {baseMessage} | *(contact)* |

#### ICIR — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ICIR901` | CanonicalizeIR assertion error: {baseMessage} | *(contact)* |
| `ICIR902` | CanonicalizeIR error: {baseMessage} | *(contact)* |

#### ICMA — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ICMA901` | ConvertMustAliasToIOBuffer assertion error: {baseMessage} | *(contact)* |
| `ICMA902` | ConvertMustAliasToIOBuffer error: {baseMessage} | *(contact)* |

#### ICMC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ICMC901` | CommuteConcat assertion error: {baseMessage} | *(contact)* |
| `ICMC902` | CommuteConcat error: {baseMessage} | *(contact)* |

#### ICNE — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ICNE901` | ConcatOpElimination assertion error: {baseMessage} | *(contact)* |
| `ICNE902` | ConcatOpElimination error: {baseMessage} | *(contact)* |

#### ICOE — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ICOE901` | CastOpElimination assertion error: {baseMessage} | *(contact)* |
| `ICOE902` | CastOpElimination error: {baseMessage} | *(contact)* |

#### IDCE — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDCE901` | DeadCodeElimination assertion error: {baseMessage} | *(contact)* |
| `IDCE902` | DeadCodeElimination error: {baseMessage} | *(contact)* |

#### IDDI — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDDI901` | DetailDynInst assertion error: {baseMessage} | *(contact)* |
| `IDDI902` | DetailDynInst error: {baseMessage} | *(contact)* |

#### IDDT — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDDT901` | DramToDramTranspose assertion error: {baseMessage} | *(contact)* |
| `IDDT902` | DramToDramTranspose error: {baseMessage} | *(contact)* |

#### IDEC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDEC901` | DeConcat assertion error: {baseMessage} | *(contact)* |
| `IDEC902` | DeConcat error: {baseMessage} | *(contact)* |

#### IDEL — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDEL901` | Delinearization assertion error: {baseMessage} | *(contact)* |
| `IDEL902` | Delinearization error: {baseMessage} | *(contact)* |

#### IDFS — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDFS901` | DFSStackAllocator assertion error: {baseMessage} | *(contact)* |
| `IDFS902` | DFSStackAllocator error: {baseMessage} | *(contact)* |

#### IDFV — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDFV901` | DataflowViewer assertion error: {baseMessage} | *(contact)* |
| `IDFV902` | DataflowViewer error: {baseMessage} | *(contact)* |

#### IDGE — INTERNAL (4 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDGE001` |  |  |
| `IDGE002` |  |  |
| `IDGE003` |  |  |
| `IDGE004` |  |  |

#### IDGM — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDGM901` | DumpGraphAndMetadata assertion error: {baseMessage} | *(contact)* |
| `IDGM902` | DumpGraphAndMetadata error: {baseMessage} | *(contact)* |

#### IDIE — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDIE901` | DynamicInstEstimator assertion error: {baseMessage} | *(contact)* |
| `IDIE902` | DynamicInstEstimator error: {baseMessage} | *(contact)* |

#### IDLI — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDLI901` | DelinearIndices assertion error: {baseMessage} | *(contact)* |
| `IDLI902` | DelinearIndices error: {baseMessage} | *(contact)* |

#### IDLO — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDLO901` | DataLocalityOpt assertion error: {baseMessage} | *(contact)* |
| `IDLO902` | DataLocalityOpt error: {baseMessage} | *(contact)* |

#### IDMA — INTERNAL (4 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDMA001` |  |  |
| `IDMA002` |  |  |
| `IDMA003` |  |  |
| `IDMA004` |  |  |

#### IDML — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDML901` | DMALocalityOpt assertion error: {baseMessage} | *(contact)* |
| `IDML902` | DMALocalityOpt error: {baseMessage} | *(contact)* |

#### IDMP — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDMP901` | DMAProfiler assertion error: {baseMessage} | *(contact)* |
| `IDMP902` | DMAProfiler error: {baseMessage} | *(contact)* |

#### IDNT — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDNT901` | DoNothing assertion error: {baseMessage} | *(contact)* |
| `IDNT902` | DoNothing error: {baseMessage} | *(contact)* |

#### IDSE — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDSE901` | DeadStoreElimination assertion error: {baseMessage} | *(contact)* |
| `IDSE902` | DeadStoreElimination error: {baseMessage} | *(contact)* |

#### IDSH — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDSH901` | DimensionSharding assertion error: {baseMessage} | *(contact)* |
| `IDSH902` | DimensionSharding error: {baseMessage} | *(contact)* |

#### IDST — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDST901` | DataStreaming assertion error: {baseMessage} | *(contact)* |
| `IDST902` | DataStreaming error: {baseMessage} | *(contact)* |

#### IDTP — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDTP901` | DMATilingProfiler assertion error: {baseMessage} | *(contact)* |
| `IDTP902` | DMATilingProfiler error: {baseMessage} | *(contact)* |

#### IDVR — INTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IDVR004` |  |  |

#### IEAD — INTERNAL (3 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IEAD001` | EnforceAluDTAcc: promoting tensor {tensor_name} from {src_dtype} to {dst_dtype} would overflow SB partition ({promoted_bytes} > {limit_bytes} bytes) | Remove the --accumulate-on-alu-dtype flag to disable this optimization |
| `IEAD901` | EnforceAluDTAcc assertion error: {baseMessage} | *(contact)* |
| `IEAD902` | EnforceAluDTAcc error: {baseMessage} | *(contact)* |

#### IEBN — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IEBN901` | ExpandBatchNorm assertion error: {baseMessage} | *(contact)* |
| `IEBN902` | ExpandBatchNorm error: {baseMessage} | *(contact)* |

#### IEDV — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IEDV901` | EliminateDivs assertion error: {baseMessage} | *(contact)* |
| `IEDV902` | EliminateDivs error: {baseMessage} | *(contact)* |

#### IEID — INTERNAL (4 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IEID001` |  |  |
| `IEID002` |  |  |
| `IEID003` |  |  |
| `IEID004` |  |  |

#### IEIL — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IEIL012` |  |  |
| `IEIL013` |  |  |

#### IEIM — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IEIM901` | ExpandISAMacro assertion error: {baseMessage} | *(contact)* |
| `IEIM902` | ExpandISAMacro error: {baseMessage} | *(contact)* |

#### IFAT — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IFAT901` | FlattenAxesForTiling assertion error: {baseMessage} | *(contact)* |
| `IFAT902` | FlattenAxesForTiling error: {baseMessage} | *(contact)* |

#### IFBD — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IFBD901` | FactorizeBlkDims assertion error: {baseMessage} | *(contact)* |
| `IFBD902` | FactorizeBlkDims error: {baseMessage} | *(contact)* |

#### IFFD — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IFFD901` | FactorizeFreeDims assertion error: {baseMessage} | *(contact)* |
| `IFFD902` | FactorizeFreeDims error: {baseMessage} | *(contact)* |

#### IFGC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IFGC901` | FineGrainedCCOpFusion assertion error: {baseMessage} | *(contact)* |
| `IFGC902` | FineGrainedCCOpFusion error: {baseMessage} | *(contact)* |

#### IFML — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IFML901` | FlattenMacroLoop assertion error: {baseMessage} | *(contact)* |
| `IFML902` | FlattenMacroLoop error: {baseMessage} | *(contact)* |

#### IFSG — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IFSG901` | FastSpillGeneration assertion error: {baseMessage} | *(contact)* |
| `IFSG902` | FastSpillGeneration error: {baseMessage} | *(contact)* |

#### IFTA — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IFTA901` | FactorizeThreadAxesInFreeDims assertion error: {baseMessage} | *(contact)* |
| `IFTA902` | FactorizeThreadAxesInFreeDims error: {baseMessage} | *(contact)* |

#### IGAS — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IGAS901` | GenericAccessSimplifier assertion error: {baseMessage} | *(contact)* |
| `IGAS902` | GenericAccessSimplifier error: {baseMessage} | *(contact)* |

#### IGCA — INTERNAL (7 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IGCA001` | Pre-allocated or already allocated tensor {tensor_name} should not be in the allocation stack during DRAM allocation. | *(contact)* |
| `IGCA003` |  |  |
| `IGCA004` | Constraint group has mixed constrained and unconstrained tensors. | *(contact)* |
| `IGCA117` | Conflicting AP-level partition constraints detected within a constraint group. | *(contact)* |
| `IGCA118` | Conflicting hard partition constraints in a constraint group. | *(contact)* |
| `IGCA119` | No valid quadrant-aligned base partition assignment exists for a constraint group. | *(contact)* |
| `IGCA120` | Cannot assign computed base partition to tensor. | *(contact)* |

#### IGLO — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IGLO901` | GlobalLayoutOpt assertion error: {baseMessage} | *(contact)* |
| `IGLO902` | GlobalLayoutOpt error: {baseMessage} | *(contact)* |

#### IHAG — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IHAG901` | HoistAllGather assertion error: {baseMessage} | *(contact)* |
| `IHAG902` | HoistAllGather error: {baseMessage} | *(contact)* |

#### IHFC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IHFC901` | HoistFSDPCollectives assertion error: {baseMessage} | *(contact)* |
| `IHFC902` | HoistFSDPCollectives error: {baseMessage} | *(contact)* |

#### IICB — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IICB901` | InsertCoreBarrier assertion error: {baseMessage} | *(contact)* |
| `IICB902` | InsertCoreBarrier error: {baseMessage} | *(contact)* |

#### IICR — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IICR901` | InsertConflictResolutionOps assertion error: {baseMessage} | *(contact)* |
| `IICR902` | InsertConflictResolutionOps error: {baseMessage} | *(contact)* |

#### IIFG — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IIFG901` | InlineFusionGroup assertion error: {baseMessage} | *(contact)* |
| `IIFG902` | InlineFusionGroup error: {baseMessage} | *(contact)* |

#### IIGL — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IIGL901` | IPGlobalLayoutOpt assertion error: {baseMessage} | *(contact)* |
| `IIGL902` | IPGlobalLayoutOpt error: {baseMessage} | *(contact)* |

#### IIIC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IIIC901` | InferIntrinsicOnCC assertion error: {baseMessage} | *(contact)* |
| `IIIC902` | InferIntrinsicOnCC error: {baseMessage} | *(contact)* |

#### IIIN — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IIIN901` | InferIntrinsic assertion error: {baseMessage} | *(contact)* |
| `IIIN902` | InferIntrinsic error: {baseMessage} | *(contact)* |

#### IIIT — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IIIT901` | InsertIOTransposes assertion error: {baseMessage} | *(contact)* |
| `IIIT902` | InsertIOTransposes error: {baseMessage} | *(contact)* |

#### IIIV — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IIIV901` | InferInitValue assertion error: {baseMessage} | *(contact)* |
| `IIIV902` | InferInitValue error: {baseMessage} | *(contact)* |

#### IILT — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IILT901` | InsertLocalTransposes assertion error: {baseMessage} | *(contact)* |
| `IILT902` | InsertLocalTransposes error: {baseMessage} | *(contact)* |

#### IINK — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IINK901` | InlineNativeKernels assertion error: {baseMessage} | *(contact)* |
| `IINK902` | InlineNativeKernels error: {baseMessage} | *(contact)* |

#### IINL — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IINL901` | InferNonlocalTensors assertion error: {baseMessage} | *(contact)* |
| `IINL902` | InferNonlocalTensors error: {baseMessage} | *(contact)* |

#### IINT — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IINT901` | InferNeuronTensor assertion error: {baseMessage} | *(contact)* |
| `IINT902` | InferNeuronTensor error: {baseMessage} | *(contact)* |

#### IIOT — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IIOT901` | InsertOfflaodedTransposes assertion error: {baseMessage} | *(contact)* |
| `IIOT902` | InsertOfflaodedTransposes error: {baseMessage} | *(contact)* |

#### IIPS — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IIPS901` | IPSimplifier assertion error: {baseMessage} | *(contact)* |
| `IIPS902` | IPSimplifier error: {baseMessage} | *(contact)* |

#### IISA — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IISA901` | InferShardAxis assertion error: {baseMessage} | *(contact)* |
| `IISA902` | InferShardAxis error: {baseMessage} | *(contact)* |

#### IISM — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IISM901` | InferSharedMemLoc assertion error: {baseMessage} | *(contact)* |
| `IISM902` | InferSharedMemLoc error: {baseMessage} | *(contact)* |

#### IIST — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IIST901` | IPSubgraphTensorAnalysis assertion error: {baseMessage} | *(contact)* |
| `IIST902` | IPSubgraphTensorAnalysis error: {baseMessage} | *(contact)* |

#### ILBR — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILBR901` | LowerBroadcast assertion error: {baseMessage} | *(contact)* |
| `ILBR902` | LowerBroadcast error: {baseMessage} | *(contact)* |

#### ILCB — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILCB901` | LowerCCOpBlockAxis assertion error: {baseMessage} | *(contact)* |
| `ILCB902` | LowerCCOpBlockAxis error: {baseMessage} | *(contact)* |

#### ILCC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILCC901` | LegalizeCCOpLayout assertion error: {baseMessage} | *(contact)* |
| `ILCC902` | LegalizeCCOpLayout error: {baseMessage} | *(contact)* |

#### ILCM — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILCM901` | LICM assertion error: {baseMessage} | *(contact)* |
| `ILCM902` | LICM error: {baseMessage} | *(contact)* |

#### ILCX — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILCX901` | LowerComplexBroadcast assertion error: {baseMessage} | *(contact)* |
| `ILCX902` | LowerComplexBroadcast error: {baseMessage} | *(contact)* |

#### ILDM — INTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILDM002` |  |  |

#### ILFD — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILFD901` | LinearizeFreeDim assertion error: {baseMessage} | *(contact)* |
| `ILFD902` | LinearizeFreeDim error: {baseMessage} | *(contact)* |

#### ILFU — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILFU901` | LoopFusion assertion error: {baseMessage} | *(contact)* |
| `ILFU902` | LoopFusion error: {baseMessage} | *(contact)* |

#### ILIN — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILIN901` | LowerIntrinsics assertion error: {baseMessage} | *(contact)* |
| `ILIN902` | LowerIntrinsics error: {baseMessage} | *(contact)* |

#### ILIS — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILIS901` | LegalizeInstSize assertion error: {baseMessage} | *(contact)* |
| `ILIS902` | LegalizeInstSize error: {baseMessage} | *(contact)* |

#### ILKK — INTERNAL (3 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILKK003` |  |  |
| `ILKK004` |  |  |
| `ILKK005` |  |  |

#### ILLI — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILLI901` | LateLegalizeInst assertion error: {baseMessage} | *(contact)* |
| `ILLI902` | LateLegalizeInst error: {baseMessage} | *(contact)* |

#### ILLO — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILLO901` | LateLowerTensorOp assertion error: {baseMessage} | *(contact)* |
| `ILLO902` | LateLowerTensorOp error: {baseMessage} | *(contact)* |

#### ILLP — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILLP901` | LateLegalizePostSplit assertion error: {baseMessage} | *(contact)* |
| `ILLP902` | LateLegalizePostSplit error: {baseMessage} | *(contact)* |

#### ILLR — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILLR901` | LateLowerReshapeOp assertion error: {baseMessage} | *(contact)* |
| `ILLR902` | LateLowerReshapeOp error: {baseMessage} | *(contact)* |

#### ILLT — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILLT901` | LocalLegalizeType assertion error: {baseMessage} | *(contact)* |
| `ILLT902` | LocalLegalizeType error: {baseMessage} | *(contact)* |

#### ILMM — INTERNAL (3 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILMM001` |  |  |
| `ILMM002` |  |  |
| `ILMM003` |  |  |

#### ILNI — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILNI901` | LateNeuronInstComb assertion error: {baseMessage} | *(contact)* |
| `ILNI902` | LateNeuronInstComb error: {baseMessage} | *(contact)* |

#### ILNK — INTERNAL (4 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILNK002` |  |  |
| `ILNK003` |  |  |
| `ILNK004` |  |  |
| `ILNK005` |  |  |

#### ILNM — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILNM901` | LegalizeNeuronMacro assertion error: {baseMessage} | *(contact)* |
| `ILNM902` | LegalizeNeuronMacro error: {baseMessage} | *(contact)* |

#### ILOA — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILOA901` | LegalizeOpLevelAlias assertion error: {baseMessage} | *(contact)* |
| `ILOA902` | LegalizeOpLevelAlias error: {baseMessage} | *(contact)* |

#### ILOP — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILOP901` | LocalLayoutOpt assertion error: {baseMessage} | *(contact)* |
| `ILOP902` | LocalLayoutOpt error: {baseMessage} | *(contact)* |

#### ILPP — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILPP901` | LayoutPreprocessing assertion error: {baseMessage} | *(contact)* |
| `ILPP902` | LayoutPreprocessing error: {baseMessage} | *(contact)* |

#### ILPR — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILPR901` | LegalizePartitionReduce assertion error: {baseMessage} | *(contact)* |
| `ILPR902` | LegalizePartitionReduce error: {baseMessage} | *(contact)* |

#### ILPT — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILPT901` | LegalizePartitionTile assertion error: {baseMessage} | *(contact)* |
| `ILPT902` | LegalizePartitionTile error: {baseMessage} | *(contact)* |

#### ILPX — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILPX901` | LowerPartitionTile assertion error: {baseMessage} | *(contact)* |
| `ILPX902` | LowerPartitionTile error: {baseMessage} | *(contact)* |

#### ILRA — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILRA901` | LayoutRequirementAnalysis assertion error: {baseMessage} | *(contact)* |
| `ILRA902` | LayoutRequirementAnalysis error: {baseMessage} | *(contact)* |

#### ILSA — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILSA901` | LegalizeSundaAccess assertion error: {baseMessage} | *(contact)* |
| `ILSA902` | LegalizeSundaAccess error: {baseMessage} | *(contact)* |

#### ILSC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILSC901` | LNCShardingConstraintOp assertion error: {baseMessage} | *(contact)* |
| `ILSC902` | LNCShardingConstraintOp error: {baseMessage} | *(contact)* |

#### ILSM — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILSM901` | LegalizeSundaMacro assertion error: {baseMessage} | *(contact)* |
| `ILSM902` | LegalizeSundaMacro error: {baseMessage} | *(contact)* |

#### ILSP — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILSP901` | LoopSplitting assertion error: {baseMessage} | *(contact)* |
| `ILSP902` | LoopSplitting error: {baseMessage} | *(contact)* |

#### ILSR — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILSR901` | LowerToSendRecv assertion error: {baseMessage} | *(contact)* |
| `ILSR902` | LowerToSendRecv error: {baseMessage} | *(contact)* |

#### ILSX — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILSX901` | LowerShardAxis assertion error: {baseMessage} | *(contact)* |
| `ILSX902` | LowerShardAxis error: {baseMessage} | *(contact)* |

#### ILTA — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILTA901` | LegalizeTongaAccess assertion error: {baseMessage} | *(contact)* |
| `ILTA902` | LegalizeTongaAccess error: {baseMessage} | *(contact)* |

#### ILTO — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILTO901` | LowerTensorOp assertion error: {baseMessage} | *(contact)* |
| `ILTO902` | LowerTensorOp error: {baseMessage} | *(contact)* |

#### ILTR — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILTR901` | LowerTranspose assertion error: {baseMessage} | *(contact)* |
| `ILTR902` | LowerTranspose error: {baseMessage} | *(contact)* |

#### ILTS — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILTS901` | LegalizeTongaStore assertion error: {baseMessage} | *(contact)* |
| `ILTS902` | LegalizeTongaStore error: {baseMessage} | *(contact)* |

#### ILTY — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ILTY901` | LegalizeType assertion error: {baseMessage} | *(contact)* |
| `ILTY902` | LegalizeType error: {baseMessage} | *(contact)* |

#### IMCE — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IMCE901` | MemcpyElimination assertion error: {baseMessage} | *(contact)* |
| `IMCE902` | MemcpyElimination error: {baseMessage} | *(contact)* |

#### IMDD — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IMDD901` | ModDivDelinear assertion error: {baseMessage} | *(contact)* |
| `IMDD902` | ModDivDelinear error: {baseMessage} | *(contact)* |

#### IMDT — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IMDT901` | MutateDataType assertion error: {baseMessage} | *(contact)* |
| `IMDT902` | MutateDataType error: {baseMessage} | *(contact)* |

#### IMGN — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IMGN901` | MacroGeneration assertion error: {baseMessage} | *(contact)* |
| `IMGN902` | MacroGeneration error: {baseMessage} | *(contact)* |

#### IMPR — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IMPR901` | MaskPropagation assertion error: {baseMessage} | *(contact)* |
| `IMPR902` | MaskPropagation error: {baseMessage} | *(contact)* |

#### IMS — INTERNAL (5 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IMS705` | common loop not found | *(contact)* |
| `IMS706` | unexpected dead mem locs | *(contact)* |
| `IMS707` | missing a matmult | *(contact)* |
| `IMS708` | No loop instruction created | *(contact)* |
| `IMS709` | No loop instruction created | *(contact)* |

#### IMSA — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IMSA901` | MeshAxis assertion error: {baseMessage} | *(contact)* |
| `IMSA902` | MeshAxis error: {baseMessage} | *(contact)* |

#### IMSH — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IMSH901` | Mesh assertion error: {baseMessage} | *(contact)* |
| `IMSH902` | Mesh error: {baseMessage} | *(contact)* |

#### INBU — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INBU901` | NeuronBufferUsageAnalysis assertion error: {baseMessage} | *(contact)* |
| `INBU902` | NeuronBufferUsageAnalysis error: {baseMessage} | *(contact)* |

#### INCP — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INCP901` | NkiCodegenPass assertion error: {baseMessage} | *(contact)* |
| `INCP902` | NkiCodegenPass error: {baseMessage} | *(contact)* |

#### INDI — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INDI901` | NeuronAliasDependencyInduction assertion error: {baseMessage} | *(contact)* |
| `INDI902` | NeuronAliasDependencyInduction error: {baseMessage} | *(contact)* |

#### INDR — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INDR901` | NeuronAliasDependencyReset assertion error: {baseMessage} | *(contact)* |
| `INDR902` | NeuronAliasDependencyReset error: {baseMessage} | *(contact)* |

#### INDV — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INDV901` | NeuronAliasDependencyVerificationPass assertion error: {baseMessage} | *(contact)* |
| `INDV902` | NeuronAliasDependencyVerificationPass error: {baseMessage} | *(contact)* |

#### INEC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INEC901` | NeuronEliminateCasts assertion error: {baseMessage} | *(contact)* |
| `INEC902` | NeuronEliminateCasts error: {baseMessage} | *(contact)* |

#### INIC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INIC901` | NeuronInstComb assertion error: {baseMessage} | *(contact)* |
| `INIC902` | NeuronInstComb error: {baseMessage} | *(contact)* |

#### INIS — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INIS901` | NeuronISel assertion error: {baseMessage} | *(contact)* |
| `INIS902` | NeuronISel error: {baseMessage} | *(contact)* |

#### INKN — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INKN901` | InlineNKIKernels assertion error: {baseMessage} | *(contact)* |
| `INKN902` | InlineNKIKernels error: {baseMessage} | *(contact)* |

#### INLC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INLC901` | NeuronLICM assertion error: {baseMessage} | *(contact)* |
| `INLC902` | NeuronLICM error: {baseMessage} | *(contact)* |

#### INLF — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INLF901` | NeuronLoopFusion assertion error: {baseMessage} | *(contact)* |
| `INLF902` | NeuronLoopFusion error: {baseMessage} | *(contact)* |

#### INLI — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INLI901` | NeuronLoopInterchange assertion error: {baseMessage} | *(contact)* |
| `INLI902` | NeuronLoopInterchange error: {baseMessage} | *(contact)* |

#### INPN — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INPN901` | NeuronPerfectLoopNest assertion error: {baseMessage} | *(contact)* |
| `INPN902` | NeuronPerfectLoopNest error: {baseMessage} | *(contact)* |

#### INSM — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INSM901` | NeuronSimplifier assertion error: {baseMessage} | *(contact)* |
| `INSM902` | NeuronSimplifier error: {baseMessage} | *(contact)* |

#### INSP — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INSP901` | NeuronSimplifyPredicates assertion error: {baseMessage} | *(contact)* |
| `INSP902` | NeuronSimplifyPredicates error: {baseMessage} | *(contact)* |

#### INST — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INST901` | NeuronSizeTiling assertion error: {baseMessage} | *(contact)* |
| `INST902` | NeuronSizeTiling error: {baseMessage} | *(contact)* |

#### INTC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INTC901` | NKITensorCanonicalization assertion error: {baseMessage} | *(contact)* |
| `INTC902` | NKITensorCanonicalization error: {baseMessage} | *(contact)* |

#### INVN — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INVN901` | NeuronValueNumbering assertion error: {baseMessage} | *(contact)* |
| `INVN902` | NeuronValueNumbering error: {baseMessage} | *(contact)* |

#### INVR — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `INVR901` | NeuronVerifier assertion error: {baseMessage} | *(contact)* |
| `INVR902` | NeuronVerifier error: {baseMessage} | *(contact)* |

#### IOAC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IOAC901` | OptimizeAliasedCopyChain assertion error: {baseMessage} | *(contact)* |
| `IOAC902` | OptimizeAliasedCopyChain error: {baseMessage} | *(contact)* |

#### IOLV — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IOLV901` | OperatorLayoutVisualizer assertion error: {baseMessage} | *(contact)* |
| `IOLV902` | OperatorLayoutVisualizer error: {baseMessage} | *(contact)* |

#### IONK — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IONK901` | OptimizeNKIKernels assertion error: {baseMessage} | *(contact)* |
| `IONK902` | OptimizeNKIKernels error: {baseMessage} | *(contact)* |

#### IOPF — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IOPF901` | OperatorFusion assertion error: {baseMessage} | *(contact)* |
| `IOPF902` | OperatorFusion error: {baseMessage} | *(contact)* |

#### IPAA — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPAA901` | ParAxesAnnotation assertion error: {baseMessage} | *(contact)* |
| `IPAA902` | ParAxesAnnotation error: {baseMessage} | *(contact)* |

#### IPAS — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPAS901` | PredicateAffineSelect assertion error: {baseMessage} | *(contact)* |
| `IPAS902` | PredicateAffineSelect error: {baseMessage} | *(contact)* |

#### IPCC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPCC901` | PComputeCutting assertion error: {baseMessage} | *(contact)* |
| `IPCC902` | PComputeCutting error: {baseMessage} | *(contact)* |

#### IPDE — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPDE901` | PadElimination assertion error: {baseMessage} | *(contact)* |
| `IPDE902` | PadElimination error: {baseMessage} | *(contact)* |

#### IPER — INTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPER012` |  |  |

#### IPF — INTERNAL (17 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPF001` | InsertPTCOMFlat can only run on a program with 1 subgraph, but there are {actual} subgraphs. | *(contact)* |
| `IPF002` | InsertPTCOMFlat can only handle modules with a single Function, but module {name} has {actual} | *(contact)* |
| `IPF003` | No BasicBlock named '{bb_name}' found on core0 | *(contact)* |
| `IPF004` | No CoreBarrier with matching id ({id}) found for '{cb_name}' in Module '{mod_name}' | *(contact)* |
| `IPF005` | Invalid value '{value}' for '--ptcom-corebarrier-delay-count'; it must be >0 | *(contact)* |
| `IPF006` | Malformed BIR - exit block must not be a part of any cycles | *(contact)* |
| `IPF007` | Malformed BIR - entry block must not be a part of any cycles | *(contact)* |
| `IPF008` | findCBOnCore0 expected a CoreBarrier but got null | *(contact)* |
| `IPF009` | expected CFG of function '{function}' to have a single exit node but it has {actual} | *(contact)* |
| `IPF010` | findNewLastWrite expected a MemoryLocation but got null | *(contact)* |
| `IPF011` | expected 1 Module for LNC1 but got {actual} | *(contact)* |
| `IPF012` | unable to find a valid extendedWrite instruction | *(contact)* |
| `IPF013` | expected extendedWrite to be an Instruction but got null | *(contact)* |
| `IPF014` | findMemLocOnCore0 expected >0 Modules | *(contact)* |
| `IPF015` | findMemLocOnCore0 expected just 1 Function on core0 but got {actual} | *(contact)* |
| `IPF016` | findMemLocOnCore0 expected to find a MemoryLocation on core0, but found null | *(contact)* |
| `IPF017` | identifyPTCOMBlocks skipped over ExternalOutput MemoryLocation '{name}' | *(contact)* |

#### IPFN — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPFN901` | PrintFunction assertion error: {baseMessage} | *(contact)* |
| `IPFN902` | PrintFunction error: {baseMessage} | *(contact)* |

#### IPLF — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPLF901` | PartialLoopFusion assertion error: {baseMessage} | *(contact)* |
| `IPLF902` | PartialLoopFusion error: {baseMessage} | *(contact)* |

#### IPLN — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPLN901` | PerfectLoopNest assertion error: {baseMessage} | *(contact)* |
| `IPLN902` | PerfectLoopNest error: {baseMessage} | *(contact)* |

#### IPLO — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPLO901` | ParLayoutOpt assertion error: {baseMessage} | *(contact)* |
| `IPLO902` | ParLayoutOpt error: {baseMessage} | *(contact)* |

#### IPLX — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPLX901` | PartitionLocalityOpt assertion error: {baseMessage} | *(contact)* |
| `IPLX902` | PartitionLocalityOpt error: {baseMessage} | *(contact)* |

#### IPMG — INTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPMG001` | DRAM2DBlockTensor requires npartitions=1: {error_msg} | *(contact)* |

#### IPMN — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPMN901` | PassManager assertion error: {baseMessage} | *(contact)* |
| `IPMN902` | PassManager error: {baseMessage} | *(contact)* |

#### IPNI — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPNI901` | PeepholeNeuronInstComb assertion error: {baseMessage} | *(contact)* |
| `IPNI902` | PeepholeNeuronInstComb error: {baseMessage} | *(contact)* |

#### IPNK — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPNK901` | PrintNKIKernelLayout assertion error: {baseMessage} | *(contact)* |
| `IPNK902` | PrintNKIKernelLayout error: {baseMessage} | *(contact)* |

#### IPSF — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPSF901` | PartialSimdFusion assertion error: {baseMessage} | *(contact)* |
| `IPSF902` | PartialSimdFusion error: {baseMessage} | *(contact)* |

#### IPST — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IPST901` | InferPSumTensor assertion error: {baseMessage} | *(contact)* |
| `IPST902` | InferPSumTensor error: {baseMessage} | *(contact)* |

#### IRAC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IRAC901` | ResolveAccessConflict assertion error: {baseMessage} | *(contact)* |
| `IRAC902` | ResolveAccessConflict error: {baseMessage} | *(contact)* |

#### IRCP — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IRCP901` | Recompute assertion error: {baseMessage} | *(contact)* |
| `IRCP902` | Recompute error: {baseMessage} | *(contact)* |

#### IRCX — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IRCX901` | ResolveComplicatePredicates assertion error: {baseMessage} | *(contact)* |
| `IRCX902` | ResolveComplicatePredicates error: {baseMessage} | *(contact)* |

#### IRMT — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IRMT901` | Rematerialization assertion error: {baseMessage} | *(contact)* |
| `IRMT902` | Rematerialization error: {baseMessage} | *(contact)* |

#### IRNS — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IRNS901` | RelocateNKIStack assertion error: {baseMessage} | *(contact)* |
| `IRNS902` | RelocateNKIStack error: {baseMessage} | *(contact)* |

#### IROE — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IROE901` | ReshapeOpElimination assertion error: {baseMessage} | *(contact)* |
| `IROE902` | ReshapeOpElimination error: {baseMessage} | *(contact)* |

#### IROI — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IROI901` | RecognizeOpIdiom assertion error: {baseMessage} | *(contact)* |
| `IROI902` | RecognizeOpIdiom error: {baseMessage} | *(contact)* |

#### IRPR — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IRPR901` | ResolvePredicates assertion error: {baseMessage} | *(contact)* |
| `IRPR902` | ResolvePredicates error: {baseMessage} | *(contact)* |

#### IRPX — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IRPX901` | RelaxPredicates assertion error: {baseMessage} | *(contact)* |
| `IRPX902` | RelaxPredicates error: {baseMessage} | *(contact)* |

#### IRRM — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IRRM901` | RewriteReplicationMatmul assertion error: {baseMessage} | *(contact)* |
| `IRRM902` | RewriteReplicationMatmul error: {baseMessage} | *(contact)* |

#### IRRW — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IRRW901` | RewriteWeights assertion error: {baseMessage} | *(contact)* |
| `IRRW902` | RewriteWeights error: {baseMessage} | *(contact)* |

#### IRSA — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IRSA901` | ReinsertShardAxis assertion error: {baseMessage} | *(contact)* |
| `IRSA902` | ReinsertShardAxis error: {baseMessage} | *(contact)* |

#### IRSP — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IRSP901` | RemoveShardedPartitionAxes assertion error: {baseMessage} | *(contact)* |
| `IRSP902` | RemoveShardedPartitionAxes error: {baseMessage} | *(contact)* |

#### IRSW — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IRSW901` | ReshapeWeights assertion error: {baseMessage} | *(contact)* |
| `IRSW902` | ReshapeWeights error: {baseMessage} | *(contact)* |

#### ISAB — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISAB901` | InsertImplicitShardAxisBeforeISel assertion error: {baseMessage} | *(contact)* |
| `ISAB902` | InsertImplicitShardAxisBeforeISel error: {baseMessage} | *(contact)* |

#### ISAG — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISAG901` | SplitAccGrp assertion error: {baseMessage} | *(contact)* |
| `ISAG902` | SplitAccGrp error: {baseMessage} | *(contact)* |

#### ISAR — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISAR901` | SimpleAllReduceTiling assertion error: {baseMessage} | *(contact)* |
| `ISAR902` | SimpleAllReduceTiling error: {baseMessage} | *(contact)* |

#### ISAU — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISAU901` | SplitAPUnionSets assertion error: {baseMessage} | *(contact)* |
| `ISAU902` | SplitAPUnionSets error: {baseMessage} | *(contact)* |

#### ISCH — INTERNAL (3 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISCH001` |  |  |
| `ISCH002` |  |  |
| `ISCH003` |  |  |

#### ISDC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISDC901` | SpmdDCE assertion error: {baseMessage} | *(contact)* |
| `ISDC902` | SpmdDCE error: {baseMessage} | *(contact)* |

#### ISDD — INTERNAL (11 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISDD013` |  |  |
| `ISDD014` |  |  |
| `ISDD015` |  |  |
| `ISDD016` |  |  |
| `ISDD017` |  |  |
| `ISDD018` |  |  |
| `ISDD019` |  |  |
| `ISDD020` |  |  |
| `ISDD021` |  |  |
| `ISDD901` | SoftmaxDivisionDelay assertion error: {baseMessage} | *(contact)* |
| `ISDD902` | SoftmaxDivisionDelay error: {baseMessage} | *(contact)* |

#### ISFD — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISFD901` | SplitFreeDim assertion error: {baseMessage} | *(contact)* |
| `ISFD902` | SplitFreeDim error: {baseMessage} | *(contact)* |

#### ISFV — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISFV901` | SFKVectorizer assertion error: {baseMessage} | *(contact)* |
| `ISFV902` | SFKVectorizer error: {baseMessage} | *(contact)* |

#### ISI — INTERNAL (3 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISI001` | All Collective Compute Instructions using the same src-target pairs must have the same stream ID | *(contact)* |
| `ISI002` | All Collective Compute Instructions using the same replica groups must have the same stream ID | *(contact)* |
| `ISI003` | All GetCurProcessingRankID Instructions using the same replica groups must have the same stream ID | *(contact)* |

#### ISIM — INTERNAL (145 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISIM137` |  |  |
| `ISIM138` |  |  |
| `ISIM139` |  |  |
| `ISIM140` |  |  |
| `ISIM141` |  |  |
| `ISIM142` |  |  |
| `ISIM143` |  |  |
| `ISIM144` |  |  |
| `ISIM145` |  |  |
| `ISIM146` |  |  |
| `ISIM147` |  |  |
| `ISIM148` |  |  |
| `ISIM149` |  |  |
| `ISIM150` |  |  |
| `ISIM151` |  |  |
| `ISIM152` |  |  |
| `ISIM153` |  |  |
| `ISIM154` |  |  |
| `ISIM155` |  |  |
| `ISIM156` |  |  |
| `ISIM157` |  |  |
| `ISIM158` |  |  |
| `ISIM159` |  |  |
| `ISIM160` |  |  |
| `ISIM161` |  |  |
| `ISIM162` |  |  |
| `ISIM163` |  |  |
| `ISIM164` |  |  |
| `ISIM165` |  |  |
| `ISIM166` |  |  |
| `ISIM167` |  |  |
| `ISIM168` |  |  |
| `ISIM169` |  |  |
| `ISIM170` |  |  |
| `ISIM171` |  |  |
| `ISIM172` |  |  |
| `ISIM173` |  |  |
| `ISIM174` |  |  |
| `ISIM175` |  |  |
| `ISIM176` |  |  |
| `ISIM177` |  |  |
| `ISIM178` |  |  |
| `ISIM179` |  |  |
| `ISIM180` |  |  |
| `ISIM181` |  |  |
| `ISIM182` |  |  |
| `ISIM183` |  |  |
| `ISIM184` |  |  |
| `ISIM185` |  |  |
| `ISIM186` |  |  |
| `ISIM187` |  |  |
| `ISIM188` |  |  |
| `ISIM189` |  |  |
| `ISIM190` |  |  |
| `ISIM191` |  |  |
| `ISIM192` |  |  |
| `ISIM193` |  |  |
| `ISIM194` |  |  |
| `ISIM195` |  |  |
| `ISIM196` |  |  |
| `ISIM197` |  |  |
| `ISIM198` |  |  |
| `ISIM199` |  |  |
| `ISIM200` |  |  |
| `ISIM201` |  |  |
| `ISIM202` |  |  |
| `ISIM203` |  |  |
| `ISIM204` |  |  |
| `ISIM205` |  |  |
| `ISIM206` |  |  |
| `ISIM207` |  |  |
| `ISIM208` |  |  |
| `ISIM209` |  |  |
| `ISIM210` |  |  |
| `ISIM211` |  |  |
| `ISIM212` |  |  |
| `ISIM213` |  |  |
| `ISIM214` |  |  |
| `ISIM215` |  |  |
| `ISIM216` |  |  |
| `ISIM217` |  |  |
| `ISIM218` |  |  |
| `ISIM219` |  |  |
| `ISIM220` |  |  |
| `ISIM221` |  |  |
| `ISIM222` |  |  |
| `ISIM223` |  |  |
| `ISIM224` |  |  |
| `ISIM225` |  |  |
| `ISIM226` |  |  |
| `ISIM227` |  |  |
| `ISIM228` |  |  |
| `ISIM229` |  |  |
| `ISIM230` |  |  |
| `ISIM231` |  |  |
| `ISIM232` |  |  |
| `ISIM233` |  |  |
| `ISIM234` |  |  |
| `ISIM235` |  |  |
| `ISIM236` |  |  |
| `ISIM237` |  |  |
| `ISIM238` |  |  |
| `ISIM239` |  |  |
| `ISIM240` |  |  |
| `ISIM241` |  |  |
| `ISIM242` |  |  |
| `ISIM243` |  |  |
| `ISIM244` |  |  |
| `ISIM245` |  |  |
| `ISIM246` |  |  |
| `ISIM247` | Functions may not have multiple shard ids, but {func_name} has {num_shard_ids}. | *(contact)* |
| `ISIM248` | Functions may not have multiple shard ids, but {func_name} has {num_shard_ids}. | *(contact)* |
| `ISIM249` |  |  |
| `ISIM250` |  |  |
| `ISIM251` |  |  |
| `ISIM252` |  |  |
| `ISIM253` |  |  |
| `ISIM254` |  |  |
| `ISIM255` |  |  |
| `ISIM256` |  |  |
| `ISIM257` |  |  |
| `ISIM258` |  |  |
| `ISIM259` |  |  |
| `ISIM260` |  |  |
| `ISIM261` |  |  |
| `ISIM262` | Load from memory with unsupported data type {data_type} into register | *(contact)* |
| `ISIM263` | The instruction cannot have an unassigned engine | *(contact)* |
| `ISIM264` | Expected all cores to reach collective op {ccOPInstIdx} but core {core_id} is at {coreCCOPInstIdx} | *(contact)* |
| `ISIM267` |  |  |
| `ISIM268` |  |  |
| `ISIM269` |  |  |
| `ISIM270` |  |  |
| `ISIM271` |  |  |
| `ISIM272` |  |  |
| `ISIM273` |  |  |
| `ISIM275` |  |  |
| `ISIM276` | {inst_type} BIRSim simulation internal error: not all engines are waiting on the same {inst_type}, inst1.name = {inst1_name}, inst2.name = {inst2_name} | *(contact)* |
| `ISIM284` | {inst_type} BIRSim simulation internal error: all engines waiting on all-engine {inst_type} but there are pending sync waiters | *(contact)* |
| `ISIM285` |  |  |
| `ISIM286` |  |  |
| `ISIM287` |  |  |
| `ISIM291` |  |  |
| `ISIM293` | Random mode not supported for int64/uint64 memset | *(contact)* |
| `ISIM901` | SimulatorPass assertion error: {baseMessage} | *(contact)* |
| `ISIM902` | SimulatorPass error: {baseMessage} | *(contact)* |

#### ISIS — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISIS901` | SundaISel assertion error: {baseMessage} | *(contact)* |
| `ISIS902` | SundaISel error: {baseMessage} | *(contact)* |

#### ISMP — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISMP901` | Simplifier assertion error: {baseMessage} | *(contact)* |
| `ISMP902` | Simplifier error: {baseMessage} | *(contact)* |

#### ISMT — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISMT901` | SMTAllocationPass assertion error: {baseMessage} | *(contact)* |
| `ISMT902` | SMTAllocationPass error: {baseMessage} | *(contact)* |

#### ISMX — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISMX901` | SimplifyMacroPredicates assertion error: {baseMessage} | *(contact)* |
| `ISMX902` | SimplifyMacroPredicates error: {baseMessage} | *(contact)* |

#### ISNT — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISNT901` | SimplifyNeuronTensor assertion error: {baseMessage} | *(contact)* |
| `ISNT902` | SimplifyNeuronTensor error: {baseMessage} | *(contact)* |

#### ISOE — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISOE901` | SliceOpElimination assertion error: {baseMessage} | *(contact)* |
| `ISOE902` | SliceOpElimination error: {baseMessage} | *(contact)* |

#### ISPA — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISPA901` | ShardingPropagationAnalysis assertion error: {baseMessage} | *(contact)* |
| `ISPA902` | ShardingPropagationAnalysis error: {baseMessage} | *(contact)* |

#### ISPC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISPC901` | SPMDCodeGen assertion error: {baseMessage} | *(contact)* |
| `ISPC902` | SPMDCodeGen error: {baseMessage} | *(contact)* |

#### ISPR — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISPR901` | SimplifyPredicates assertion error: {baseMessage} | *(contact)* |
| `ISPR902` | SimplifyPredicates error: {baseMessage} | *(contact)* |

#### ISPS — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISPS901` | SpillPSum assertion error: {baseMessage} | *(contact)* |
| `ISPS902` | SpillPSum error: {baseMessage} | *(contact)* |

#### ISSA — INTERNAL (5 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISSA004` |  |  |
| `ISSA005` |  |  |
| `ISSA006` |  |  |
| `ISSA007` |  |  |
| `ISSA008` |  |  |

#### ISSD — INTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISSD001` |  |  |

#### ISSL — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISSL901` | SimplifySlice assertion error: {baseMessage} | *(contact)* |
| `ISSL902` | SimplifySlice error: {baseMessage} | *(contact)* |

#### ISSS — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISSS901` | SymbolicStackAllocator assertion error: {baseMessage} | *(contact)* |
| `ISSS902` | SymbolicStackAllocator error: {baseMessage} | *(contact)* |

#### ISST — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISST901` | SundaSizeTiling assertion error: {baseMessage} | *(contact)* |
| `ISST902` | SundaSizeTiling error: {baseMessage} | *(contact)* |

#### ISTA — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISTA901` | StackAllocator assertion error: {baseMessage} | *(contact)* |
| `ISTA902` | StackAllocator error: {baseMessage} | *(contact)* |

#### ISTL — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISTL901` | StaticTransposeLocalTensor assertion error: {baseMessage} | *(contact)* |
| `ISTL902` | StaticTransposeLocalTensor error: {baseMessage} | *(contact)* |

#### ISTN — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISTN901` | SimplifyTensor assertion error: {baseMessage} | *(contact)* |
| `ISTN902` | SimplifyTensor error: {baseMessage} | *(contact)* |

#### ISTP — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISTP901` | StaticProfiler assertion error: {baseMessage} | *(contact)* |
| `ISTP902` | StaticProfiler error: {baseMessage} | *(contact)* |

#### ISUP — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISUP901` | SuperSimplifier assertion error: {baseMessage} | *(contact)* |
| `ISUP902` | SuperSimplifier error: {baseMessage} | *(contact)* |

#### ISUS — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISUS901` | SplitUnionSets assertion error: {baseMessage} | *(contact)* |
| `ISUS902` | SplitUnionSets error: {baseMessage} | *(contact)* |

#### ISYC — INTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ISYC001` |  |  |

#### ITCC — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITCC901` | TileCCOps assertion error: {baseMessage} | *(contact)* |
| `ITCC902` | TileCCOps error: {baseMessage} | *(contact)* |

#### ITCO — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITCO901` | TransformConvOp assertion error: {baseMessage} | *(contact)* |
| `ITCO902` | TransformConvOp error: {baseMessage} | *(contact)* |

#### ITCT — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITCT901` | TCTransform assertion error: {baseMessage} | *(contact)* |
| `ITCT902` | TCTransform error: {baseMessage} | *(contact)* |

#### ITDM — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITDM901` | TransformerDimensionMatcher assertion error: {baseMessage} | *(contact)* |
| `ITDM902` | TransformerDimensionMatcher error: {baseMessage} | *(contact)* |

#### ITEN — INTERNAL (23 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITEN404` | Internal tensorizer error: {baseMessage} | *(contact)* |
| `ITEN405` | Internal tensorizer maximum recursion depth exceeded, {stack} | *(contact)* |
| `ITEN406` | Too many partition dimensions detected at {partition_set}. This is usually due to unsupported (strided) access pattern | *(contact)* |
| `ITEN407` | QuantizeMXTensorOp verification error: {error_msg} | *(contact)* |
| `ITEN408` | QuantizeMXTensorOp verification error: {error_msg} | *(contact)* |
| `ITEN409` | QuantizeMXTensorOp verification error: {error_msg} | *(contact)* |
| `ITEN410` | QuantizeMXTensorOp verification error: {error_msg} | *(contact)* |
| `ITEN411` | QuantizeMXTensorOp verification error: {error_msg} | *(contact)* |
| `ITEN412` | QuantizeMXTensorOp verification error: {error_msg} | *(contact)* |
| `ITEN413` | QuantizeMXTensorOp verification error: {error_msg} | *(contact)* |
| `ITEN414` | QuantizeMXTensorOp verification error: {error_msg} | *(contact)* |
| `ITEN420` | ScaledTensorContractTensorOp verifier error : {error_msg} | *(contact)* |
| `ITEN421` | ScaledTensorContractTensorOp verifier error : {error_msg} | *(contact)* |
| `ITEN422` | ScaledTensorContractTensorOp verifier error : {error_msg} | *(contact)* |
| `ITEN423` | ScaledTensorContractTensorOp verifier error : {error_msg} | *(contact)* |
| `ITEN424` | ScaledTensorContractTensorOp verifier error : {error_msg} | *(contact)* |
| `ITEN425` | ScaledTensorContractTensorOp verifier error : {error_msg} | *(contact)* |
| `ITEN426` | ScaledTensorContractTensorOp verifier error : {error_msg} | *(contact)* |
| `ITEN427` | ScaledTensorContractTensorOp verifier error : {error_msg} | *(contact)* |
| `ITEN428` | ScaledTensorContractTensorOp verifier error : {error_msg} | *(contact)* |
| `ITEN429` | ScaledTensorContractTensorOp verifier error : {error_msg} | *(contact)* |
| `ITEN430` | ScaledTensorContractTensorOp verifier error : {error_msg} | *(contact)* |
| `ITEN431` | ScaledTensorContractTensorOp verifier error : {error_msg} | *(contact)* |

#### ITIN — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITIN901` | TensorInitialization assertion error: {baseMessage} | *(contact)* |
| `ITIN902` | TensorInitialization error: {baseMessage} | *(contact)* |

#### ITLA — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITLA901` | TensorLifetimeAnalysis assertion error: {baseMessage} | *(contact)* |
| `ITLA902` | TensorLifetimeAnalysis error: {baseMessage} | *(contact)* |

#### ITLX — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITLX901` | TransformLayout assertion error: {baseMessage} | *(contact)* |
| `ITLX902` | TransformLayout error: {baseMessage} | *(contact)* |

#### ITNM — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITNM901` | TensorNamer assertion error: {baseMessage} | *(contact)* |
| `ITNM902` | TensorNamer error: {baseMessage} | *(contact)* |

#### ITOE — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITOE901` | TransposeOpElimination assertion error: {baseMessage} | *(contact)* |
| `ITOE902` | TransposeOpElimination error: {baseMessage} | *(contact)* |

#### ITOS — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITOS901` | TensorOpSimplifier assertion error: {baseMessage} | *(contact)* |
| `ITOS902` | TensorOpSimplifier error: {baseMessage} | *(contact)* |

#### ITOT — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITOT901` | TensorOpTransform assertion error: {baseMessage} | *(contact)* |
| `ITOT902` | TensorOpTransform error: {baseMessage} | *(contact)* |

#### ITPO — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITPO901` | TensorParallelOpt assertion error: {baseMessage} | *(contact)* |
| `ITPO902` | TensorParallelOpt error: {baseMessage} | *(contact)* |

#### ITPR — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITPR901` | TilingProfiler assertion error: {baseMessage} | *(contact)* |
| `ITPR902` | TilingProfiler error: {baseMessage} | *(contact)* |

#### ITRF — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITRF901` | TritiumFusion assertion error: {baseMessage} | *(contact)* |
| `ITRF902` | TritiumFusion error: {baseMessage} | *(contact)* |

#### ITSH — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITSH901` | TensorSharding assertion error: {baseMessage} | *(contact)* |
| `ITSH902` | TensorSharding error: {baseMessage} | *(contact)* |

#### ITST — INTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `ITST001` | Example internal error message 1. | *(contact)* |

#### IUBK — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IUBK901` | UnshardBIRKernelForSimulation assertion error: {baseMessage} | *(contact)* |
| `IUBK902` | UnshardBIRKernelForSimulation error: {baseMessage} | *(contact)* |

#### IURM — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IURM901` | UnrollReduceMacro assertion error: {baseMessage} | *(contact)* |
| `IURM902` | UnrollReduceMacro error: {baseMessage} | *(contact)* |

#### IUTL — INTERNAL (3 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IUTL001` |  |  |
| `IUTL002` |  |  |
| `IUTL003` |  |  |

#### IVDM — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IVDM901` | VectorizeDMA assertion error: {baseMessage} | *(contact)* |
| `IVDM902` | VectorizeDMA error: {baseMessage} | *(contact)* |

#### IVLP — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IVLP901` | VectorizeLoop assertion error: {baseMessage} | *(contact)* |
| `IVLP902` | VectorizeLoop error: {baseMessage} | *(contact)* |

#### IVMM — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IVMM901` | VectorizeMatMult assertion error: {baseMessage} | *(contact)* |
| `IVMM902` | VectorizeMatMult error: {baseMessage} | *(contact)* |

#### IVNU — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IVNU901` | ValueNumbering assertion error: {baseMessage} | *(contact)* |
| `IVNU902` | ValueNumbering error: {baseMessage} | *(contact)* |

#### IWCO — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IWCO901` | WeightCoalescing assertion error: {baseMessage} | *(contact)* |
| `IWCO902` | WeightCoalescing error: {baseMessage} | *(contact)* |

#### IXCG — INTERNAL (32 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IXCG013` | ISA check {checkName} failed: {msg} | *(contact)* |
| `IXCG014` | ISA check {checkName} failed: {msg} | *(contact)* |
| `IXCG015` | ISA check {checkName} failed: {msg} | *(contact)* |
| `IXCG016` |  |  |
| `IXCG017` |  |  |
| `IXCG018` | Instruction engine check failed ({engine}) | *(contact)* |
| `IXCG019` | ISA check {checkName} failed: {msg} | *(contact)* |
| `IXCG020` | ISA check failed | *(contact)* |
| `IXCG021` |  |  |
| `IXCG022` |  |  |
| `IXCG023` |  |  |
| `IXCG024` |  |  |
| `IXCG025` |  |  |
| `IXCG026` |  |  |
| `IXCG027` |  |  |
| `IXCG028` |  |  |
| `IXCG029` | Unknown sync wait mode |  |
| `IXCG030` | Unknown sync update mode |  |
| `IXCG031` | Unimplemented Dtype {type} |  |
| `IXCG034` |  |  |
| `IXCG035` |  |  |
| `IXCG036` |  |  |
| `IXCG037` |  |  |
| `IXCG038` |  |  |
| `IXCG039` |  |  |
| `IXCG040` |  |  |
| `IXCG041` |  |  |
| `IXCG042` |  |  |
| `IXCG043` | Unknown EngineType |  |
| `IXCG044` | Instruction engine check failed ({engine}) |  |
| `IXCG045` | ISA check {checkName} failed: {msg} |  |
| `IXCG046` | ISA check failed: {msg} |  |

#### IXLV — INTERNAL (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IXLV035` |  |  |

#### IZST — INTERNAL (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `IZST901` | ZeroSizeTensorElimination assertion error: {baseMessage} | *(contact)* |
| `IZST902` | ZeroSizeTensorElimination error: {baseMessage} | *(contact)* |

#### JIO — bare (18 codes)

| Code | Cause | Resolution |
|---|---|---|
| `JIO001` | Unable open json file {path} | Please ensure file exists and disk space is available. |
| `JIO002` | Unable parse empty json file {path} | Please ensure disk space is available. |
| `JIO003` | Encountered parsing error, {error_msg}, while attempting to read {path} | *(contact)* |
| `JIO004` | Encountered json error, {error_msg}, while attempting to parse {path}. | *(contact)* |
| `JIO005` | Encountered exception, {error_msg}, while attempting to parse {path}. IO diagnostic checks: {diagnostics} | *(contact)* |
| `JIO006` | Encountered unexpected exception while attempting to parse {path}. IO diagnostic checks: {diagnostics} | *(contact)* |
| `JIO007` | Unable write json to {path}. IO diagnostic checks: {diagnostics} | Please ensure diretory exists and disk space is available. |
| `JIO008` | Unable write json to {path} due to {error_msg} | Please ensure json fields contain encodable unicode characters. |
| `JIO009` | Encountered json error, {error_msg}, while attempting to write json to {path} | *(contact)* |
| `JIO010` | Encountered exception, {error_msg}, while attempting to write json to {path}. IO diagnostic checks: {diagnostics} | *(contact)* |
| `JIO011` | Encountered unexpected exception while attempting to write json to {path}. IO diagnostic checks: {diagnostics} | *(contact)* |
| `JIO012` | Unable write json to {path} because of error writing to file. IO diagnostic checks: {diagnostics} | Please ensure diretory exists and disk space is available. |
| `JIO013` | Unable write json to {path}. IO diagnostic checks: {diagnostics} | Please ensure diretory exists and disk space is available. |
| `JIO014` | Unable write json to {path} due to {error_msg} | Please ensure json fields contain encodable unicode characters. |
| `JIO015` | Encountered json error, {error_msg}, while attempting to write json to {path} | *(contact)* |
| `JIO016` | Encountered exception, {error_msg}, while attempting to write json to {path}. IO diagnostic checks: {diagnostics} | *(contact)* |
| `JIO017` | Encountered unexpected exception while attempting to write json to {path}. IO diagnostic checks: {diagnostics} | *(contact)* |
| `JIO018` | Unable write json to {path} because of error writing to file.  IO diagnostic checks: {diagnostics} | Please ensure diretory exists and disk space is available. |

#### JOB — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `JOB001` | Expected Job {name} to supply a 3-letter, uppercase abbreviation, but got {abbrev} | *(contact)* |

#### LCL — bare (26 codes)

| Code | Cause | Resolution |
|---|---|---|
| `LCL731` | Unrecognized AxisListType | *(contact)* |
| `LCL732` | User step must fit reduced location, load reduction error | *(contact)* |
| `LCL741` | Expected a matmult | *(contact)* |
| `LCL742` | Unexpected argument | *(contact)* |
| `LCL743` | Unexpected number of arguments | *(contact)* |
| `LCL744` | Expected a matmult | *(contact)* |
| `LCL745` | Unexpected instruction type | *(contact)* |
| `LCL746` | Unexpected access pattern | *(contact)* |
| `LCL747` | Expected a tensor scalar pointer | *(contact)* |
| `LCL748` | Expected a load | *(contact)* |
| `LCL749` | unexpected null pointer | *(contact)* |
| `LCL750` | Index not in the split range | *(contact)* |
| `LCL751` | Unexpected node count | *(contact)* |
| `LCL752` | Expected a load | *(contact)* |
| `LCL755` | Cluster not found | *(contact)* |
| `LCL756` | Cluster not found | *(contact)* |
| `LCL757` | Unexpected access pattern | *(contact)* |
| `LCL758` | User access pattern read range exceeds load range, load reduction error | *(contact)* |
| `LCL759` | Start address not aligned | *(contact)* |
| `LCL760` | Memory location is not dead, load reduction error | *(contact)* |
| `LCL761` | Expected a load | *(contact)* |
| `LCL762` | Load reads from DRAM, the under partition AP cannot have a stride >1 | *(contact)* |
| `LCL763` | Expected a load | *(contact)* |
| `LCL764` | Expected a load | *(contact)* |
| `LCL765` | Overlapping live range among cloned loads | *(contact)* |
| `LCL777` | Expected a clean cluster | *(contact)* |

#### LDD — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `LDD001` | SWDGE DMAs slow dimension must be a multiple of 16 to avoid performance penalties (it is {actual}) | *(contact)* |

#### LDM — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `LDM001` | Instruction {name} not found in any DMA blocks | *(contact)* |

#### LEG — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `LEG194` | A PSUM node that cannot be in PSUM | *(contact)* |
| `LEG195` | Tensorizer generated tensor is out of bounds post PSUM legalization. | *(contact)* |

#### LKK — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `LKK001` | Only user-allocated kernels may have dependencies, violation: {inst} -> {edge_kind} -> {dep}. | *(contact)* |
| `LKK002` | All dependencies coming from NKI should be EdgeKind::Ordered, violation: {inst} -> {edge_kind} -> {dep}. | *(contact)* |

#### LKN — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `LKN001` | Internal error with kernel lowering. | *(contact)* |

#### LLC — bare (64 codes)

| Code | Cause | Resolution |
|---|---|---|
| `LLC001` | Expected local collective compute operation to have a single replication group, but found {size} groups | *(contact)* |
| `LLC002` | Unsupported local collective compute operation {kind} | *(contact)* |
| `LLC003` | Expected module name in the format 'ncXX/sgXX', but got {mod_name} | *(contact)* |
| `LLC004` | Expected NC prefix of 'ncXX', but got {nc_name} from module {mod_name} | *(contact)* |
| `LLC005` | Local CollectiveCompute input ({input_dim}) and output ({output_dim}) dimensions differ. | *(contact)* |
| `LLC006` | Expected numeric NC index in {nc_name}, but got {nc_num} in module {mod_name} | *(contact)* |
| `LLC007` | Expected to find a splittable entry in AP, but none found. | *(contact)* |
| `LLC008` | AllReduce local collective operation currently only supports addition | *(contact)* |
| `LLC009` | ReduceScatter local collective operation currently only supports addition | *(contact)* |
| `LLC010` | Expected memory location used in local collective to have tensor id. | *(contact)* |
| `LLC011` | Input and output tensors have different dimensions {to_split_size} vs {related_size} | *(contact)* |
| `LLC012` | Input and output tensors have different dimensions {pattern1_size} vs {pattern2_size} | *(contact)* |
| `LLC013` | Unable to find suitable split dimension for output tensor {tensor} AP | *(contact)* |
| `LLC014` | Unable to find suitable split dimension for input tensor {tensor} AP | *(contact)* |
| `LLC015` | Unable to find suitable split dimension for input tensor {input} AP and output tensor {output} AP | *(contact)* |
| `LLC016` | Splitting tensor {tensor} AP has a max offset of {max_byte_offset} (in bytes), but referenced memory location is only {memloc_size} bytes. | *(contact)* |
| `LLC017` | Expected {expected_mod_count} modules, but got {actual_mod_count} modules. | *(contact)* |
| `LLC018` | Expected {expected_ccop_count} collective operations, but found {actual_ccop_count} in core {core_id} subgraph(s). | *(contact)* |
| `LLC019` | Expected one input and one output for local collective, but got {arg_count} arguments and {out_count} outputs. | *(contact)* |
| `LLC020` | Expected local collective to share input dimension {expected_arg_dims}, but core {core_id} has input dimension {actual_arg_dims}. | *(contact)* |
| `LLC021` | Expected local collective to have matching replica groups {expected_cores}, but core {core_id} has replica group {actual_cores}. | *(contact)* |
| `LLC022` | Expected local collective to have matching kinds {expected_kind}, but core {core_id} has kind {actual_kind}. | *(contact)* |
| `LLC023` | Expected scatter dimension to match {expected_scatter_dim} for all cores, but core {core_id} has scatter dimension {actual_scatter_dim}. | *(contact)* |
| `LLC024` | Expected scatter dimension to match {expected_scatter_dim} for all cores, but core {core_id} has scatter dimension {actual_scatter_dim}. | *(contact)* |
| `LLC025` | Expected gather dimension to match {expected_gather_dim} for all cores, but core {core_id} has gather dimension {actual_gather_dim}. | *(contact)* |
| `LLC026` | Expected gather dimension to match {expected_gather_dim} for all cores, but core {core_id} has gather dimension {actual_gather_dim}. | *(contact)* |
| `LLC027` | Expected address space to match {expected_addr_space} for all cores, but core {core_id} has address space {actual_addr_space}. | *(contact)* |
| `LLC028` | Expected scatter dimension to match {expected_scatter_dim} for all cores, but core {core_id} has scatter dimension {actual_scatter_dim}. | *(contact)* |
| `LLC029` | Expected gather dimension to match {expected_gather_dim} for all cores, but core {core_id} has gather dimension {actual_gather_dim}. | *(contact)* |
| `LLC030` | Shared remote memory locations ({remote}) are not currently supported in local collective operations. | *(contact)* |
| `LLC031` | Expected address space to match {expected_addr_space} for all cores, but core {core_id} has address space {actual_addr_space}. | *(contact)* |
| `LLC032` | Expected local collective to share output dimension {expected_out_dims}, but core {core_id} has output dimension {actual_out_dims}. | *(contact)* |
| `LLC033` | legalizeAPs() failed: Input/output dimension expected to be {expected_dims}, but core {core_id} could not be shrunk beyond {actual_dims}. | *(contact)* |
| `LLC034` | legalizeAPs() failed: Input/output dimension {dimension} expected to be {expected_num}, but core {core_id} has {actual_num}. | *(contact)* |
| `LLC035` | Expected shared memory definition for {name} to have a synchronization primitive before it. | *(contact)* |
| `LLC036` | Expected core barrier ahead of shared memory definition for {name}, but no core barrier at index {index} on core {core_id} exists. | *(contact)* |
| `LLC037` | Expected shared memory reference for {name} to have a synchronization primitive after it. | *(contact)* |
| `LLC038` | Expected core barrier after shared memory reference for {name}, but no core barrier at index {index} on core {core_id} exists. | *(contact)* |
| `LLC039` | Expected function {name} to have a single basic block at this point, but it has {count}. | *(contact)* |
| `LLC040` | Expected function {name} to have a single basic block at this point, but it has {count}. | *(contact)* |
| `LLC041` | Expected shared memory location for {name} con core {core_id}, but found none. | *(contact)* |
| `LLC042` | Expected shared memory location for {name} con core {core_id}, but found none. | *(contact)* |
| `LLC043` | Updating dependencies failed: expect core barrier at the beginning. | *(contact)* |
| `LLC044` | Updating dependencies failed: expect core barrier at the end when remote semaphores disabled! | *(contact)* |
| `LLC045` | Expected to find a first definition for shared tensor {name} on at least one core. | *(contact)* |
| `LLC046` | lower_local_collectives failed with the following errors:{errors} | *(contact)* |
| `LLC047` | Expected to find a definition before read for shared tensor {name}. | *(contact)* |
| `LLC048` | Expected matching SendRecv to {expected} initial core barrier, but found core {ncId} {actual} initial core barrier | *(contact)* |
| `LLC049` | Expected matching SendRecvCCE to {expected} initial core barrier, but found core {ncId} {actual} initial core barrier | *(contact)* |
| `LLC050` | Expected {expected} CoreBarrier insturctions in subgraph {sgId}, but found {actual} on core {ncId} | *(contact)* |
| `LLC051` | Expected CoreBarriers {cb_index} in subgraph {sgId} to be named {expected_name} and have id {expected}, but found {actual_name} and {actual} on core {ncId} | *(contact)* |
| `LLC052` | Expected {expected_ccop_count} local collective operations, but found {actual_ccop_count} in core {core_id} subgraph(s). | *(contact)* |
| `LLC053` | Expected exactly one LNC-predicated global collective on core 0 corresponding to this instruction, but got {num_ccops}. | *(contact)* |
| `LLC054` | Expected the module of this CollectiveCompute instruction to have a valid NeuronCoreId. | *(contact)* |
| `LLC055` | Expected the module of this CollectiveCompute instruction to have a NeuronCoreId of 0. | Try assigning LNC-predicated CollectiveCompute instructions to core 0 only. |
| `LLC056` | CollectiveCompute {ccName} on core 0 reads or writes to the shared MemoryLocation {accessName}. Expected all other cores to have no real reads or writes to this shared MemoryLocation, but got this instruction reading or writing it. | Try removing all reads and writes to that shared MemoryLocation; otherwise don't predicate the CollectiveCompute. |
| `LLC057` | LNC-predicated global collective involving SB is not supported | *(contact)* |
| `LLC058` | Could not find Function named {function_name} on core {nc_id} | *(contact)* |
| `LLC059` | Could not find MemoryLocation named {memloc_name} on core {nc_id} | *(contact)* |
| `LLC060` | Can't map sendrecv to GPSIMDSB2SB because peer IDs aren't a swap | *(contact)* |
| `LLC061` | Can't map sendrecv to GPSIMDSB2SB as requested | *(contact)* |
| `LLC062` | Internal invariant violated | *(contact)* |
| `LLC063` | Could not find BasicBlock named {block_name} on core {nc_id} | *(contact)* |
| `LLC064` | Could not find corresponding CollectiveCompute instruction on core 0 to this CollectiveCompute instruction on core {ncid} | Try assigning LNC-predicated CollectiveCompute instructions to core 0 only. |

#### LLR — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `LLR739` | last instruction, doesn't need spilling | *(contact)* |
| `LLR740` | expected a save instruction | *(contact)* |

#### LNK — bare (5 codes)

| Code | Cause | Resolution |
|---|---|---|
| `LNK001` | MemoryLocation '{name}' cannot be written by more than 1 function call | *(contact)* |
| `LNK020` | tensor_map verification failed after linking | *(contact)* |
| `LNK021` | tensor_map verification failed: output {out} was used as input earlier | *(contact)* |
| `LNK022` | Queue '{queue}' does not exist in linked module | *(contact)* |
| `LNK023` | Could not write tensor_map file '{path}' after linking | *(contact)* |

#### LRS — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `LRS778` | Unexpected AP | *(contact)* |
| `LRS779` | Split spill instruction is not save | *(contact)* |

#### LSA — bare (37 codes)

| Code | Cause | Resolution |
|---|---|---|
| `LSA047` | Internal invariant violated | *(contact)* |
| `LSA048` | Internal invariant violated | *(contact)* |
| `LSA049` | Internal invariant violated | *(contact)* |
| `LSA050` | Internal invariant violated | *(contact)* |
| `LSA051` | Internal invariant violated | *(contact)* |
| `LSA052` | Internal invariant violated | *(contact)* |
| `LSA053` | Internal invariant violated | *(contact)* |
| `LSA054` | Internal invariant violated | *(contact)* |
| `LSA055` | Internal invariant violated | *(contact)* |
| `LSA056` | Internal invariant violated | *(contact)* |
| `LSA057` | Internal invariant violated | *(contact)* |
| `LSA058` | Internal invariant violated | *(contact)* |
| `LSA059` | Internal invariant violated | *(contact)* |
| `LSA060` | Internal invariant violated | *(contact)* |
| `LSA061` | Internal invariant violated | *(contact)* |
| `LSA062` | Internal invariant violated | *(contact)* |
| `LSA063` | Internal invariant violated | *(contact)* |
| `LSA064` | Internal invariant violated | *(contact)* |
| `LSA065` | Internal invariant violated | *(contact)* |
| `LSA066` | Internal invariant violated | *(contact)* |
| `LSA067` | Illegal instruction type for spilling | *(contact)* |
| `LSA068` | Internal invariant violated | *(contact)* |
| `LSA069` | Internal invariant violated | *(contact)* |
| `LSA070` | Internal invariant violated | *(contact)* |
| `LSA072` | Illegal instruction type for spilling | *(contact)* |
| `LSA073` | Internal invariant violated | *(contact)* |
| `LSA074` | Internal invariant violated | *(contact)* |
| `LSA075` | Cannot deallocate unknown base partition | *(contact)* |
| `LSA076` | Cannot deallocate unknown memory type | *(contact)* |
| `LSA077` | Internal invariant violated | *(contact)* |
| `LSA078` | Could not find {memoryLocation} in block list | *(contact)* |
| `LSA079` | Internal invariant violated | *(contact)* |
| `LSA080` | Internal invariant violated | *(contact)* |
| `LSA081` | Internal invariant violated | *(contact)* |
| `LSA082` | Internal invariant violated | *(contact)* |
| `LSA083` | Internal invariant violated | *(contact)* |
| `LSA084` | Internal invariant violated | *(contact)* |

#### LUR — bare (12 codes)

| Code | Cause | Resolution |
|---|---|---|
| `LUR001` | Expected neuron_core_id module attirbute in module {name} when --lnc=2 | *(contact)* |
| `LUR002` | Expected number of index map entries to match number of dependency loopnest axes | *(contact)* |
| `LUR003` | MemoryLocation of {memloc_size} bytes needed to be offset by the reserved DGE scratchpad size, and got new address {new_address}, which exceeds the SB size {sb_size}. Please allocate tensors taking the reserved DGE scratchpad size into account. | *(contact)* |
| `LUR004` | Failed to allocate MemoryLocation of {memloc_size} bytes to new address {new_address}, which needed to be offset by the reserved DGE scratchpad size. Consider turning off DGE if possible, to work around this error. | *(contact)* |
| `LUR005` | Dynamic axis already exists, please ensure that nested dynamic loops do not use the same name for their axis | *(contact)* |
| `LUR006` | Dynamic axis not found! Please ensure all instructions in the dynamic for loop use the correct axis | *(contact)* |
| `LUR007` | Axis in affine expression with runtime value is not dynamic! | *(contact)* |
| `LUR016` | Incoming IR error, tensor address expression and block-dimension are not compatible | *(contact)* |
| `LUR017` | Tensor labelled as allocated but no address expression assigned in incoming IR | *(contact)* |
| `LUR018` | PSUM Tensor labelled as allocated but no bank expression/address expr assigned in incoming IR | *(contact)* |
| `LUR019` | Local tensor not allocated in smt allocation flow | *(contact)* |
| `LUR020` | Local tensor not allocated in smt allocation flow | *(contact)* |

#### MFP — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `MFP001` | ModuleForkPass called with single module {name} | *(contact)* |
| `MFP002` | Compilation failed for the following modules:{errors}. | *(contact)* |

#### MLC — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `MLC001` | Block AP request is invalid, partition or free size requested is larger than MemoryLocation | *(contact)* |

#### MLO — bare (3 codes)

| Code | Cause | Resolution |
|---|---|---|
| `MLO001` | Expected SB <-> DRAM or PSUM <-> SB, but got {src_mem_type} -> {dst_mem_type} | *(contact)* |
| `MLO002` | OptinPass must be a number | *(contact)* |
| `MLO003` | OptinPass index {actual} from bir.json is not valid. Must be in [0, {max}). | *(contact)* |

#### MMP — bare (72 codes)

| Code | Cause | Resolution |
|---|---|---|
| `MMP110` | Cannot mmpacking multi-block functions. | *(contact)* |
| `MMP111` |  | *(contact)* |
| `MMP112` |  | *(contact)* |
| `MMP115` |  | *(contact)* |
| `MMP117` | Cannot mmpacking multi-block functions. | *(contact)* |
| `MMP121` | Instruction getPrivateIndex() not following tensorizer order? | *(contact)* |
| `MMP123` |  | *(contact)* |
| `MMP125` |  | *(contact)* |
| `MMP129` |  | *(contact)* |
| `MMP131` |  | *(contact)* |
| `MMP133` |  | *(contact)* |
| `MMP135` |  | *(contact)* |
| `MMP137` | do not compute_partition_constraint if there're no shared mm pairs. | *(contact)* |
| `MMP139` |  | *(contact)* |
| `MMP141` |  | *(contact)* |
| `MMP143` |  | *(contact)* |
| `MMP151` |  | *(contact)* |
| `MMP155` |  | *(contact)* |
| `MMP157` |  | *(contact)* |
| `MMP161` | Can not handle empty accumulative groups. | *(contact)* |
| `MMP163` | Can not handle empty accumulative groups. | *(contact)* |
| `MMP171` | Cannot mmpacking multi-block functions. | *(contact)* |
| `MMP173` | Contradicting base partition found for an memloc. | *(contact)* |
| `MMP177` |  | *(contact)* |
| `MMP181` | Cannot compute feasible col_grps for empty AGs! | *(contact)* |
| `MMP185` |  | *(contact)* |
| `MMP189` |  | *(contact)* |
| `MMP193` |  | *(contact)* |
| `MMP201` |  | *(contact)* |
| `MMP207` |  | *(contact)* |
| `MMP215` |  | *(contact)* |
| `MMP219` |  | *(contact)* |
| `MMP223` |  | *(contact)* |
| `MMP227` |  | *(contact)* |
| `MMP229` | psum_id not found! | *(contact)* |
| `MMP231` |  | *(contact)* |
| `MMP237` |  | *(contact)* |
| `MMP241` |  | *(contact)* |
| `MMP245` |  | *(contact)* |
| `MMP251` |  | *(contact)* |
| `MMP253` |  | *(contact)* |
| `MMP255` | Invalid topo_order or g has cycles. | *(contact)* |
| `MMP257` | max_dist < min_dist | *(contact)* |
| `MMP259` | Rhs psum_id already belongs to this agpack! | *(contact)* |
| `MMP261` |  | *(contact)* |
| `MMP263` |  | *(contact)* |
| `MMP265` |  | *(contact)* |
| `MMP267` |  | *(contact)* |
| `MMP269` |  | *(contact)* |
| `MMP271` |  | *(contact)* |
| `MMP273` |  | *(contact)* |
| `MMP275` | Should not invoke compute_pe_grp_occupancy on empty AGPacks. | *(contact)* |
| `MMP277` | get_pack_id(.) is undefined for standalone MMs! | *(contact)* |
| `MMP279` | use_ml is not used by mm. | *(contact)* |
| `MMP280` |  | *(contact)* |
| `MMP281` |  | *(contact)* |
| `MMP283` |  | *(contact)* |
| `MMP285` |  | *(contact)* |
| `MMP291` |  | *(contact)* |
| `MMP295` |  | *(contact)* |
| `MMP297` | Rpo doesn't cover all f.allocs()!\\n | *(contact)* |
| `MMP301` |  | *(contact)* |
| `MMP303` |  | *(contact)* |
| `MMP305` |  | *(contact)* |
| `MMP307` | Ready queue node already in Active.\\n | *(contact)* |
| `MMP311` |  | *(contact)* |
| `MMP313` |  | *(contact)* |
| `MMP315` |  | *(contact)* |
| `MMP317` |  | *(contact)* |
| `MMP319` | undefined accumulation group! | *(contact)* |
| `MMP320` | Invalid number of matmult rows {num}, must fit in 32 bits | *(contact)* |
| `MMP321` | Invalid number of matmult cols {num}, must fit in 32 bits | *(contact)* |

#### MSA — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `MSA300` | Memloc legalization pass cannot be apply to allocated memlocs | *(contact)* |
| `MSA301` | Memloc data type not divisible by round up size | *(contact)* |

#### NAS — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `NAS001` | No message provided for {code}. | Add {code} to message file. |

#### NKI — bare (16 codes)

| Code | Cause | Resolution |
|---|---|---|
| `NKI001` | NKI Kernel Instruction does not have a valid function | *(contact)* |
| `NKI002` | Cannot find lowering function {fname} after unroll | *(contact)* |
| `NKI003` | Insufficient number of outputs param, expect {num_sb_buf} SB buffers and {num_psum_buf} PSUM buffers, but only got {num_actual} outputs | *(contact)* |
| `NKI004` | SBUF scratchpad memset must have exactly one memory location | *(contact)* |
| `NKI005` | DRAM location of kind {mem_kind} mapping failed. Only input/output/const DRAM location is supported! | *(contact)* |
| `NKI006` | Input/Output DRAM memory location set should only has one location, but got {memloc_count} locations | *(contact)* |
| `NKI007` | Failed to map input scratchpad memory location to {target_memloc_name} | *(contact)* |
| `NKI008` | Failed to map output scratchpad memory location to {target_memloc_name} | *(contact)* |
| `NKI009` | Unrecognized DRAM input location | *(contact)* |
| `NKI010` | Unrecognized DRAM output location | *(contact)* |
| `NKI011` | Unrecognized dependency | *(contact)* |
| `NKI012` | Instruction which copied into a loop instruction is not a loop instruction | *(contact)* |
| `NKI013` | Expected copied and original instruction names to match, but got {copied_name} != {original_name} | *(contact)* |
| `NKI014` | Expected scheduling unit instruction | *(contact)* |
| `NKI015` | Expected exactly one block in scheduling unit, but got {num_blocks} | *(contact)* |
| `NKI016` | Kernel validation exception: {error_text} | Please check the validation message and adjust kernel inputs accordingly |

#### NKS — bare (4 codes)

| Code | Cause | Resolution |
|---|---|---|
| `NKS000` | Unsupported KLR version/patch. Supported (12,0,0); found: ({major},{minor},{patch}) | *(contact)* |
| `NKS001` | Incorrect kernel tag | *(contact)* |
| `NKS002` | BIRSim did not finish successfully | *(contact)* |
| `NKS003` | Could not open input KLR file: {file} | *(contact)* |

#### NLA — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `NLA001` | Unhandled exception with message: {msg} | *(contact)* |
| `NLA002` | Unhandled exception with message: {msg} | *(contact)* |

#### OAC — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `OAC001` | Loop header basic block {bb_name} has incorrect number of predecessors ({pred}) |  |
| `OAC002` | Loop header {bb_name} cannot contain predecessor {pred_name} inside of its loop | *(contact)* |

#### OQS — bare (12 codes)

| Code | Cause | Resolution |
|---|---|---|
| `OQS001` | Main function should be a single basic block in modular flow | *(contact)* |
| `OQS002` | Previous function scope stacks not fully processed | *(contact)* |
| `OQS003` | Failed to generate CFG for function {fn_name} | *(contact)* |
| `OQS004` | Unexpected unclosed scope in function {fn_name}, malformed CFG found | *(contact)* |
| `OQS005` | Failed to find CFG for basic block {bb_name} in function {fn_name} | *(contact)* |
| `OQS006` | Function scope for function {fn_name} unexpectedly ended in basic block {bb_name}, malformed CFG found | *(contact)* |
| `OQS007` | Main function in modular flow cannot contain and DMAs or SQI instructions | *(contact)* |
| `OQS008` | Loop header {bb_name} cannot contain predecessor {pred_name} inside of its loop | *(contact)* |
| `OQS009` | Loop header (or Function entry block) {bb_name} is a header for multiple loops at once, check if CFG is malformed | *(contact)* |
| `OQS010` | SQI cannot reuse an instance in a queue, {prev_instr} also uses instance {instance} | *(contact)* |
| `OQS011` | SQI instruction cannot have inputs/outputs | *(contact)* |
| `OQS012` | Basic block {bb} found in loop body of processed scope with header {loop_header}, toplogical ordering does not keep loop bodies contiguous | *(contact)* |

#### PER — bare (11 codes)

| Code | Cause | Resolution |
|---|---|---|
| `PER001` | Could not find PerfSim event corresponding to the instruction | *(contact)* |
| `PER002` | File cost setup failed: {filename} | *(contact)* |
| `PER003` | Invalid cost file: {filename} | *(contact)* |
| `PER004` | Instruction {inst_name} was not ready at ready time {ready_time} | *(contact)* |
| `PER005` | Can't dump trace to an empty filename | *(contact)* |
| `PER006` | Trace dumping failed: {filename} | *(contact)* |
| `PER007` | Loop or scheduling unit modeling currently unsupported | *(contact)* |
| `PER008` | Loop modeling unsupported | *(contact)* |
| `PER009` | Got stream_id {stream_id} when max_stream_id is 1 | *(contact)* |
| `PER010` | Got stream_id {stream_id} when max_stream_id is 2 | *(contact)* |
| `PER011` | Tried to get the CC stream bandwidth of a non-CC timeline: {name} | *(contact)* |

#### PGT — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `PGT002` | Too many instructions after unroll! | Compiling under --optlevel=1 may result in smaller graphs. If you are using a transformer model, try using a smaller context_length_estimate value. |

#### PRS — bare (15 codes)

| Code | Cause | Resolution |
|---|---|---|
| `PRS001` | INTERNAL: trying to move producer backwards will move it further from consumers, while this optimization is trying to do the opposite | *(contact)* |
| `PRS002` | INTERNAL: instructions to move are not in ascending order, which may cause 'extra jumps' when moving them forward | *(contact)* |
| `PRS003` | INTERNAL: trying to move consumer forwards will move it further from producers, while this optimization is trying to do the opposite | *(contact)* |
| `PRS004` | INTERNAL: instructions to move are not in descending order, which may cause 'extra jumps' when moving them backward | *(contact)* |
| `PRS710` | unexpected addresses | *(contact)* |
| `PRS711` | unexpected addresses | *(contact)* |
| `PRS712` | unexpected size relationships | *(contact)* |
| `PRS713` | bytes per partition should be > 0 | *(contact)* |
| `PRS714` | SpillSave AP should only has two dimensions | *(contact)* |
| `PRS715` | expected a load instruciton | *(contact)* |
| `PRS717` | mloc with read out of range {loc} | *(contact)* |
| `PRS718` | non empty memloc {mem} | *(contact)* |
| `PRS771` | Unexpected number of locations | *(contact)* |
| `PRS772` | Unexpected level {level} | *(contact)* |
| `PRS773` | Expected 1 function in the module | *(contact)* |

#### RDP — bare (7 codes)

| Code | Cause | Resolution |
|---|---|---|
| `RDP716` | Unexpected engine type | *(contact)* |
| `RDP733` | Unexpected engine type | *(contact)* |
| `RDP734` | Unexpected DMA instruction type | *(contact)* |
| `RDP735` | Unexpected engine type | *(contact)* |
| `RDP736` | Instruction not found in map | *(contact)* |
| `RDP737` | unexpected lifetime metric calculation mode {mode} | *(contact)* |
| `RDP738` | Instruction not found in map | *(contact)* |

#### SCH — bare (43 codes)

| Code | Cause | Resolution |
|---|---|---|
| `SCH711` | Unexpected dependence within accumulation group | *(contact)* |
| `SCH712` | Scheduler deadocked | *(contact)* |
| `SCH713` | Violation of accumulation group interleaving | *(contact)* |
| `SCH714` | Violation of accumulation group interleaving: got 'earlier' read {er} happening after 'later' write {lw} | *(contact)* |
| `SCH715` | Violation of accumulation group interleaving | *(contact)* |
| `SCH716` | Memory Location not found in the set | *(contact)* |
| `SCH717` | Accumulation Group object not found in the set | *(contact)* |
| `SCH718` | Cycle in output dependencies {y} : {y_name} > {x} : {x_name} | *(contact)* |
| `SCH719` | topological order violations | *(contact)* |
| `SCH720` |  |  |
| `SCH721` |  |  |
| `SCH722` | Unexpected policy value {policy} | *(contact)* |
| `SCH723` | expected one top block in a function | *(contact)* |
| `SCH724` | Unexpected opcode | *(contact)* |
| `SCH725` | cycle in dependence graph | *(contact)* |
| `SCH726` | Expected a matmult | *(contact)* |
| `SCH727` | All siblings of a scheduled matmult should be ready | *(contact)* |
| `SCH728` | Unexpected engine | *(contact)* |
| `SCH729` | Unexpected engine | *(contact)* |
| `SCH730` | Instruction not found in ready list | *(contact)* |
| `SCH731` | Ready list is empty | *(contact)* |
| `SCH732` | Ready list is empty | *(contact)* |
| `SCH733` | Unexpected negative value | *(contact)* |
| `SCH734` | Instruction should have been scheduled at this point | *(contact)* |
| `SCH735` | Predecessor should already be scheduled | *(contact)* |
| `SCH736` | Predecessor should already be scheduled | *(contact)* |
| `SCH737` | Predecessor should already be scheduled | *(contact)* |
| `SCH738` | Unexpected call to this function in this context | *(contact)* |
| `SCH739` | Nothing ready | *(contact)* |
| `SCH768` | Unexpected instruction mix | *(contact)* |
| `SCH769` | Run out of instructions | *(contact)* |
| `SCH770` | Unexpected dependence | *(contact)* |
| `SCH771` | CoreBarrier is not assigned to an engine | *(contact)* |
| `SCH900` | Time-aware scheduler failed: could not find any ready inst | *(contact)* |
| `SCH901` | Time-aware scheduler only supports scheduling units with unrolled loopcount=1 and precise schedule, but got block {block_name} | *(contact)* |
| `SCH902` | Expected more than one module with LNC > 1 | *(contact)* |
| `SCH903` | Expected the same number of blocks for all cores, but got {core_0_num_blocks} blocks for core 0 and {core_n_num_blocks} blocks for core {core_id} | *(contact)* |
| `SCH906` | When scheduling instruction, found {remaining_preds} remaining predecessors which were not scheduled | *(contact)* |
| `SCH907` | LNC-aware scheduler failed: could not find any ready instruction. The number of scheduled instructions so far is {num_scheduled_insts}. | *(contact)* |
| `SCH908` | When trying to schedule instruction, could not find any ready instruction at start time {start_time} | *(contact)* |
| `SCH909` | LNC-aware scheduler failed: got a null candidate | *(contact)* |
| `SCH910` | LNC-aware scheduler failed: could not find LNC sync instruction candidate {cb_candidate_name} | *(contact)* |
| `SCH911` | LNC-aware scheduler failed: could not find LNC sync instruction candidate corresponding to the current instruction, on neuron core id {ncid} | *(contact)* |

#### SCP — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `SCP001` | Invalid predicate value for Select {name} | *(contact)* |

#### SDD — bare (12 codes)

| Code | Cause | Resolution |
|---|---|---|
| `SDD001` | Dynamic DMA can only have one output, got {num_outputs} outputs instead | *(contact)* |
| `SDD002` | Scalar DGE Operation should have exactly 2 arguments, got {num_args} outputs instead | *(contact)* |
| `SDD003` | Actual input ({input_dim}D) and actual output ({output_dim}D) of dynamic DMA must have same number of dimensions | *(contact)* |
| `SDD004` | Dynamic DMA shape has {num_dim} dimensions, must be either 2D or 3D | *(contact)* |
| `SDD005` | Input ({input}) and output ({output}) at dimension {i} of dynamic DMA must have same number of elements | *(contact)* |
| `SDD006` | Last dimension of access pattern is not contiguous, step size {last_step} is not equal to 1 | *(contact)* |
| `SDD007` | Max Descriptor Size violated! Access pattern has {last_num} elements, maximum is {max_num} | *(contact)* |
| `SDD008` | Partition dimension element number ({part_dim}) must be divisible by 16 or equal to 1 with the free dimension ({free_dim}) divisible by 16 | *(contact)* |
| `SDD009` | Generated offsets from unrolling don't match ({in_offsets} vs {out_offsets}), check to see if input and output nums match | *(contact)* |
| `SDD010` | Dynamic instruction must be either SW DGE or HW DGE | *(contact)* |
| `SDD011` | Vector DGE shape has {num_dim} dimensions, must be 2D | *(contact)* |
| `SDD012` | Transpose instruction does not have legal shape | *(contact)* |

#### SFP — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `SFP001` | SubgraphForkPass called with single module {name} | *(contact)* |
| `SFP002` | Compilation failed for the following subgraphs:{errors}. | *(contact)* |

#### SIM — bare (141 codes)

| Code | Cause | Resolution |
|---|---|---|
| `SIM001` | BirSim failed with the following errors:{errors} | *(contact)* |
| `SIM002` | Expected collective or core barrier instruction type for {inst} | *(contact)* |
| `SIM003` | Core {coreId} has a different collective or core barrier instruction type than core 0 for {inst} | *(contact)* |
| `SIM004` | Expected Core Barrier Instruction type for {inst} in {coreId} | *(contact)* |
| `SIM005` | DMA CCE: Scale array must match number of inputs | *(contact)* |
| `SIM006` | Tensor {name} is not found in remote core {core} | *(contact)* |
| `SIM007` | Function {name} is not found in remote core {core} | *(contact)* |
| `SIM008` | Unable split {dims} free dimensions into {count} parts for local collective operation. | *(contact)* |
| `SIM009` | Unsupported OP type {op} for collective compute reduction. | *(contact)* |
| `SIM010` | Expected replica size ({replica_count}) to match number of NCs per VC ({nc_count}) for local collective. | *(contact)* |
| `SIM011` | Expected input size ({in_count}) to match output size ({out_count}) | *(contact)* |
| `SIM012` | Expected input size ({in_count}) to match output size ({out_count}) | *(contact)* |
| `SIM013` | Expected input size ({in_count}) to match output size ({out_count}) | *(contact)* |
| `SIM014` | Expected input size ({in_count}) to match output size ({out_count}) | *(contact)* |
| `SIM015` | Expected input size ({in_count}) to match output size ({out_count}) | *(contact)* |
| `SIM016` | Expected input size ({in_count}) to match output size ({out_count}) | *(contact)* |
| `SIM017` | Expected to only handle Permute or PermulteImplicit | *(contact)* |
| `SIM019` | Uninitialized read: {name}\\nMemory location: {location} ({location_type})\\nPartition index: {partition_index}\\nAddress interval: {address_interval} | Check whether the memory was clobbered or left uninitialized. {prev_info} |
| `SIM020` | Expected instruction using CC get-rank to provide replica groups, but got {opcode} | *(contact)* |
| `SIM021` | Expected insruction to provide at least one replica group | *(contact)* |
| `SIM022` | Unsupported NCCL topology {actual_tp_rank}, exected one of {expected_tp_ranks} | *(contact)* |
| `SIM023` | Unsupported NCCL Channel {actual_channel}, expected either 0, 1 | *(contact)* |
| `SIM024` | Expected dma copy insruction to provide at least one replica group | *(contact)* |
| `SIM025` | Unsupported NCCL Channel {actual_channel}, expected either 0, 1 | *(contact)* |
| `SIM026` | Expected insruction to provide at least one replica group | *(contact)* |
| `SIM027` | Expected dma copy insruction to provide at least one replica group | *(contact)* |
| `SIM028` | Expected instruction using CC get-rank to provide replica groups, but got {opcode} | *(contact)* |
| `SIM029` | Unsupported NCCL topology {actual_tp_rank}, exected one of {expected_tp_ranks} | *(contact)* |
| `SIM030` | Unsupported NCCL topology {actual_tp_rank}, exected one of {expected_tp_ranks} | *(contact)* |
| `SIM031` | Unsupported NCCL Channel {actual_channel}, expected either 0, 1 | *(contact)* |
| `SIM032` | Unsupported NCCL topology {actual_tp_rank}, exected one of {expected_tp_ranks} | *(contact)* |
| `SIM033` | Instruction missing BasicBlock parent. | *(contact)* |
| `SIM034` | BasicBlock missing Function parent. | *(contact)* |
| `SIM035` | Function missing Module parent. | *(contact)* |
| `SIM036` | NCCL Topology is not available for {arch} architecture. | *(contact)* |
| `SIM037` | NCCL Topology is not available for {arch} architecture. | *(contact)* |
| `SIM038` | NCCL Topology is not available for {arch} architecture. | *(contact)* |
| `SIM039` | Expected pelican affine expr of kind CCGetRankExpr. | *(contact)* |
| `SIM040` | InstTensorScalarAffineSelect with NaN fill value should have exactly 2 arguments, but found {args} | *(contact)* |
| `SIM041` | InstTensorScalarAffineSelect with non-NaN fill value '{value}' should have exactly 1 argument, but found {args} | *(contact)* |
| `SIM042` | Remote semaphore update '{update}' not supported (yet) | *(contact)* |
| `SIM043` | Engine is out of buffer loop | *(contact)* |
| `SIM044` | Engine is out of buffer: {type} | *(contact)* |
| `SIM045` | More than one Return instruction in BasicBlock | *(contact)* |
| `SIM046` | Engine unassigned | *(contact)* |
| `SIM047` | Unable to read PWP tables from {path} | *(contact)* |
| `SIM048` | Memory location not found | *(contact)* |
| `SIM049` | Memory object not found | *(contact)* |
| `SIM050` | Memory location not found in Memory map | *(contact)* |
| `SIM051` | Alias memory location not found | *(contact)* |
| `SIM052` | Memory location '{location}' not found | *(contact)* |
| `SIM053` | Cannot access PSUM location {location} via DMA indirection | *(contact)* |
| `SIM054` | DRAM '{location}' must be allocated in order to reach here! | *(contact)* |
| `SIM055` | doCopyIndirect accumulate: entry size ({entry}) must be multiple of dtype ({dtype}) | *(contact)* |
| `SIM056` | Output of {inst} has NaN when writing to {mloc} in subgraph {sg} | *(contact)* |
| `SIM057` | Output of {inst} has NaN when writing to {mloc} in subgraph {sg} | *(contact)* |
| `SIM058` | No such HW arch '{arch}' yet! | *(contact)* |
| `SIM059` | Memory type '{type}' not supported yet! | *(contact)* |
| `SIM060` | Indirect read should not read from Psum! | *(contact)* |
| `SIM061` | Target memory type '{type}' not supported yet! | *(contact)* |
| `SIM062` | Memory type '{type}' not supported yet! | *(contact)* |
| `SIM063` | Output of {inst} has NaN when writing to {mloc} in subgraph {sg} | *(contact)* |
| `SIM064` | Indirect write should not write to Psum! | *(contact)* |
| `SIM065` | Memory type '{type}' not supported yet! | *(contact)* |
| `SIM066` | Target memory type '{type}' not supported yet! | *(contact)* |
| `SIM067` | Target output has NaN | *(contact)* |
| `SIM068` | Memory type '{type}' not supported yet! | *(contact)* |
| `SIM069` | writeAccumulate shouldn't happen for SB! | *(contact)* |
| `SIM070` | writeAccumulate shouldn't happen for DRAM! | *(contact)* |
| `SIM071` | Memory type '{type}' not supported yet! | *(contact)* |
| `SIM072` | Memory object for '{location}' is null in readLocal | *(contact)* |
| `SIM073` | Memory location not found in Memory map | *(contact)* |
| `SIM074` | Uninitialized read: {inst}\\nMemory location: {loc} ({type}) | Check if this Memory location has empty filename or has not been written to yet or the read/write reference count doesn't match |
| `SIM075` | Uninitialized access of aliased input location: {alias}\\nActual location accessed: {loc} | Check if this Memory location has empty filename or has not been written to yet or the read/write reference count doesn't match |
| `SIM076` | InstVisitor.writeOutputs(): Attempted to write output at MemoryLocation {name} to file, but the MemoryLocation has no associated filename. | *(contact)* |
| `SIM077` | InstVisitor.writeOutputs(): Invalid output filename {file} provided. | *(contact)* |
| `SIM078` | SB Data pinned by memory location {prev_loc}, which was written by {prev_inst}, is overwritten by instruction {cur_inst}, at {type} memory Partition index: {partition} and Address interval: {addr} | *(contact)* |
| `SIM079` | Execution hanged waiting on {waiters} | *(contact)* |
| `SIM080` | All VNC cores hanged | *(contact)* |
| `SIM081` | Double Event clear to event {id} | *(contact)* |
| `SIM082` | SendRecv does not support more than 1 input/output argument | *(contact)* |
| `SIM083` | SendRecv is not supported for global collective | *(contact)* |
| `SIM084` | Multiple LNCs are not supported yet | *(contact)* |
| `SIM085` | Number of input arguments must be the same as recvFomRank size | *(contact)* |
| `SIM086` | SendRecvCCE is not supported for global collective | *(contact)* |
| `SIM087` | Multiple LNCs are not supported yet | *(contact)* |
| `SIM088` | Expected input size ({in_count}) to match output size ({out_count}) | *(contact)* |
| `SIM089` | Indirect index ({index}) must be less than IndirectDimMaxIndex ({max_index}) | *(contact)* |
| `SIM090` | cast_accumulate shouldn't be called for Overwrite op | *(contact)* |
| `SIM091` | BIRSIM mismatch for tensors (symbolic memory simulation). | List of failing tensors and respective histograms can be found in log-neuron-cc.txt. |
| `SIM092` | BIRSIM mismatch for tensors (physical memory simulation). | List of failing tensors and respective histograms can be found in log-neuron-cc.txt. |
| `SIM093` | Expected instruction of type (DMACopy), got ({inst_type}) instead | *(contact)* |
| `SIM094` | Kernel simulation not supported for {kernel_name} yet | *(contact)* |
| `SIM095` | InstRangeSelect expects the input and output access patterns have the same data types, but input is {inType} type and output is {outType} type | *(contact)* |
| `SIM096` | InstRangeSelect expects the input and output access patterns to have the same number of elements, but input has {inputSize} elements and output has {outputSize} elements | *(contact)* |
| `SIM097` | InstRangeSelect expects the number of partitions of {bound} ({boundNumPartitions}) to be equal to input access pattern ({numInElementsPerPartition}) and shape to be one element per partition | *(contact)* |
| `SIM098` | InstRangeSelect expects {name} access patterns to be 3D, but it is {size}D | *(contact)* |
| `SIM099` | InstRangeSelect expects the {compare_op} to be one of isEQ, IsGT, IsGE, IsLT, IsLE | *(contact)* |
| `SIM100` | InstRangeSelect only supports max reduce op, but got {op} op | *(contact)* |
| `SIM101` | Non-scalar CopyPredicated cannot have reverse pred | *(contact)* |
| `SIM102` | Invalid AluOpType '{given}' for MemoryReductionOp. Must be 'bypass' or 'add' | *(contact)* |
| `SIM103` | Duplicate indices {indices} not allowed on DGE DstReduce instruction | *(contact)* |
| `SIM104` | Invalid CCE op type '{given}' for DGE DstReduce. Must be 'add' | *(contact)* |
| `SIM105` | InstCompareAndBranch does not support arguments of type {dtype} | *(contact)* |
| `SIM106` | Cannot read scalar value from {kind} argument, must be Physical AP, Register or Immediate | *(contact)* |
| `SIM107` | Dynamic offset of scalar DGE is out of bounds | *(contact)* |
| `SIM108` | InstDoWhile does not support arguments of type {dtype} | *(contact)* |
| `SIM109` | Golden: {golden_size} and BIRSim output: {birsim_output_size} sizes do not match. | *(contact)* |
| `SIM110` | Parameter index {paramsIndex} is equal to or greater than parameters per partition {paramsPerPartition} at partition {partitionOffset} and index {indicesOffset} | *(contact)* |
| `SIM111` | Simulation with kernel inline only works after unroll pass. | *(contact)* |
| `SIM112` | Static loop axis not found in loop map! | *(contact)* |
| `SIM113` | Vector DGE instruction must have Indirect Arg ID as a top level expression! | *(contact)* |
| `SIM114` | CopyPredicatedReduce requires third Immediate Argument to be Scalar. | *(contact)* |
| `SIM115` | CopyPredicatedScalar Immediate value type ({imm_type}) must match output type ({out_type}) | *(contact)* |
| `SIM116` | CopyPredicatedScalar Immediate value type ({imm_type}) must match output type ({out_type}) | *(contact)* |
| `SIM117` | CopyPredicated instruction failed. Predicate and Out must have same shape but have: {pred_part}x{pred_num} and {out_part}x{out_num} respectively | *(contact)* |
| `SIM118` | CopyPredicated instruction failed. Predicate and Src must have same number of elements but have: {pred_num} and {src_num} respectively | *(contact)* |
| `SIM119` | CopyPredicated instruction failed. Predicate and OptionalImmediate must have same number of elements but have: {pred_num} and {opt_num} respectively | *(contact)* |
| `SIM120` | Reduction Outputs must be cast to float32 before usage. Given: {actual} | *(contact)* |
| `SIM121` | Must be FP32. Given: {actual} | *(contact)* |
| `SIM122` | Gold {gold_file} not found for output {output_name} | *(contact)* |
| `SIM123` | DevicePrint only supports 2D tensor | *(contact)* |
| `SIM124` | DevicePrint does not support arguments of type {dtype} | *(contact)* |
| `SIM125` | Cannot find Memory location {loc} information in tensormap | *(contact)* |
| `SIM126` | No golden found for output location {loc} | *(contact)* |
| `SIM127` | AllEngineBarrier BIRSim simulation internal error: expected AllEngineBarrier in instruction stream | *(contact)* |
| `SIM128` | Dynamic DMA transpose indices can only be OOB after first OOB index found. But index {index} is {actual}, which is not OOB | *(contact)* |
| `SIM129` | Dynamic DMA transpose number of non-OOB indices must be a multiple of 16, but it is {actual} | *(contact)* |
| `SIM130` | AllEngineBarrier instruction should be assigned to engine ALL, but it is assigned to engine {engine} | *(contact)* |
| `SIM131` | AllEngineBarrier BIRSim simulation internal error: waiters should be empty | *(contact)* |
| `SIM132` | AllEngineBarrier BIRSim simulation internal error: unexpected end of instruction stream | *(contact)* |
| `SIM133` | AllEngineBarrier BIRSim simulation internal error: not all engines are waiting on the same AllEngineBarrier, inst1.name = {inst1_name}, inst2.name = {inst2_name} | *(contact)* |
| `SIM134` | AllEngineBarrier BIRSim simulation internal error: unexpected nullptr instruction | *(contact)* |
| `SIM135` | AllEngineBarrier instruction should not have wait/update conditions | *(contact)* |
| `SIM200` | GPSIMDSB2SB instructions are only supported for LNC=2 (not {LNC}). | *(contact)* |
| `SIM201` | Only local SendRecvs can be mapped to use GPSIMD's DMA engine. | *(contact)* |
| `SIM202` | Unknown shared memory location {location}. | *(contact)* |
| `SIM203` | Invalid seed for engine: GPSIMD. Seed must be an nx6 tensor. Got seed tensor of dimensions nx{dim} | *(contact)* |
| `SIM204` | Invalid seed for engine: Vector. Seed must be an nx24 tensor. Got seed tensor of dimensions nx{dim} | *(contact)* |
| `SIM205` | Output dtype must be int32. Got dtype: {type} | *(contact)* |
| `SIM206` | Input dtype must be int32. Got dtype: {type} | *(contact)* |

#### SIO — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `SIO001` | Register {reg_name} is used in multiple BasicBlocks - Registers must be defined and used within a single BasicBlock | *(contact)* |

#### SQI — bare (7 codes)

| Code | Cause | Resolution |
|---|---|---|
| `SQI001` | Expect only Spill/Reload queues are used in loop body, but found {queueName} of type {queueType} | *(contact)* |
| `SQI002` | SQI switches to a queue that does not exist | *(contact)* |
| `SQI003` | SQI instance should not be NULL | *(contact)* |
| `SQI004` | SQI instance's parent should be a DMAQueue | *(contact)* |
| `SQI005` | RQI resets to a queue that does not exist | *(contact)* |
| `SQI006` | RQI instance should not be NULL | *(contact)* |
| `SQI007` | RQI instance's parent should be a DMAQueue | *(contact)* |

#### SSA — bare (3 codes)

| Code | Cause | Resolution |
|---|---|---|
| `SSA001` | Got a formerly shared memory location ({mloc_name}) which is unallocated and does not have a remote local target; all such memory locations should be allocated at this point | *(contact)* |
| `SSA002` | Memory location ({mloc_name}) is unallocated, and its corresponding remote target ({remote_mloc_name}) on core {core} is also unallocated | *(contact)* |
| `SSA003` | For the current memory location, could not find its remote target {remote_name} | *(contact)* |

#### TCE — bare (3 codes)

| Code | Cause | Resolution |
|---|---|---|
| `TCE720` | Unexpected Access Pattern | *(contact)* |
| `TCE721` | Expected >=  2 arguments | *(contact)* |
| `TCE722` | Unexpected copy type | *(contact)* |

#### TEN — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `TEN404` | Internal tensorizer error: {baseMessage} | *(contact)* |
| `TEN405` | Internal tensorizer maximum recursion depth exceeded, {stack} | *(contact)* |

#### TST — bare (7 codes)

| Code | Cause | Resolution |
|---|---|---|
| `TST001` | Testing basic assertion | Skip this test |
| `TST002` | Testing with string ({msg}) source | Skip this test |
| `TST003` | Testing with DebugLocation source | Skip this test |
| `TST004` | Testing with DebugLocation and file info source | Skip this test |
| `TST005` | Testing with Instruction no DebugLocation source | Skip this test |
| `TST006` | Testing with Instruction w/DebugLocation source | Skip this test |
| `TST007` | Testing with Instruction w/DebugLocation and file info source | Skip this test |

#### VNS — bare (25 codes)

| Code | Cause | Resolution |
|---|---|---|
| `VNS601` |  |  |
| `VNS604` |  | *(contact)* |
| `VNS605` | This huge DRAM tensor has a cross-partition access! You guessed the wrong block dims maybe. | *(contact)* |
| `VNS606` | This huge DRAM tensor has a cross-partition access! You guessed the wrong block dims maybe. | *(contact)* |
| `VNS607` |  | *(contact)* |
| `VNS608` | Cannot vnsplit multi-block functions. | *(contact)* |
| `VNS609` | Cannot vnsplit multi-block functions. | *(contact)* |
| `VNS610` | Cannot vnsplit multi-block functions. | *(contact)* |
| `VNS611` | Try to remove instruction not in bb. | *(contact)* |
| `VNS612` | Try to remove MemoryLocation not in function. | *(contact)* |
| `VNS613` |  | *(contact)* |
| `VNS614` | Unitialized vnode. | *(contact)* |
| `VNS615` | Empty writeAPs in collectStats()! | *(contact)* |
| `VNS616` | Uninitialized read | *(contact)* |
| `VNS617` | Unsupported output ap type. | *(contact)* |
| `VNS618` | unsupported arg type! | *(contact)* |
| `VNS620` | Cannot shrink dn on multi-block functions. | *(contact)* |
| `VNS621` | Write AP into DN is never read.\\n | *(contact)* |
| `VNS622` | ap's parent inst has to be either AbstractCopy or Load!\\n | *(contact)* |
| `VNS623` | We've checked pred_pap is also partition contiguous earlier?! | *(contact)* |
| `VNS624` | ap's parent inst has to be either AbstractCopy or Load!\\n | *(contact)* |
| `VNS625` | ap and its pred ap must have the same Pattern size.\\n | *(contact)* |
| `VNS627` | Cannot vertical fuse multi-block functions. | *(contact)* |
| `VNS628` | try to remove MemoryLocation not in function. | *(contact)* |
| `VNS629` | try to remove instruction not in bb. | *(contact)* |

#### XAQ — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `XAQ001` | Unexpected tensor kind {kind} | *(contact)* |
| `XAQ002` | Expected at most {expected} HWDGE queues (there are {num}) | *(contact)* |

#### XBI — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `XBI001` | Internal compilation error: unexpected parallelism variant | *(contact)* |
| `XBI002` | Internal compilation error: "skip-pass-before" specified but not visited | *(contact)* |

#### XCG — bare (217 codes)

| Code | Cause | Resolution |
|---|---|---|
| `XCG001` | DMA block must have sync info | *(contact)* |
| `XCG002` | DMA block must have one local sync update | *(contact)* |
| `XCG003` | DMA block must by synchronized using semaphore | *(contact)* |
| `XCG004` | InstGroupResetSemaphores must be on EngineType::ALL, but it is on EngineType::{actual} | *(contact)* |
| `XCG005` | InstTensorCompletion must be on a datapath engine, but it is on EngineType::{actual} | *(contact)* |
| `XCG006` | InstAllEngineBarrier must be a datapath engine. Given EngineType::{actual}. | Assign to EngineType::ALL and allow it to be expanded to all engines by the expand_all_engine pass. |
| `XCG008` | One of CompareAndBranch's target must be the lexicographical next block in the BasicBlockHolder ({next})! | *(contact)* |
| `XCG009` | CompareAndBranch's second argument must be a RegisterAccess or an ImmediateValue (got {actual}) | *(contact)* |
| `XCG010` | InstCompareAndBranch cannot be on EngineType::ALL; it must be on a TPB engine, and should have been expanded by expand_all_engine | *(contact)* |
| `XCG011` | InstUnconditionalBranch cannot be on EngineType::ALL; it must be on a TPB engine, and should have been expanded by expand_all_engine | *(contact)* |
| `XCG012` | InstDevicePrint should not make it to codegen. It should have been expanded and removed by expand_device_print | *(contact)* |
| `XCG033` | Internal compilation failed: {msg} | *(contact)* |
| `XCG100` | Unknown sync wait mode | *(contact)* |
| `XCG101` | Unknown sync update mode | *(contact)* |
| `XCG102` | Unknown EngineType | *(contact)* |
| `XCG103` | Cannot convert Accum cmd to Reduce cmd. Unknown Accum cmd type | *(contact)* |
| `XCG149` | Couldn't encode completion info for extended instruction because it has no APs | *(contact)* |
| `XCG150` | Couldn't encode completion info for extended instruction because number of partitions/ports must be 0 < x <= 128 and a multiple of 16 | *(contact)* |
| `XCG151` | Couldn't encode completion info for extended instruction because base partition must be a multiple of 16 | *(contact)* |
| `XCG152` | Couldn't encode completion info for extended instruction because instruction reads and writes a different number of partitions | *(contact)* |
| `XCG164` | ISA check failed | *(contact)* |
| `XCG166` | Instruction engine check failed ({engine}) | *(contact)* |
| `XCG400` | LNC {actual} is not supported for GPSIMDSB2SB, which only supports LNC {supported} | *(contact)* |
| `XCG800` | Unknown sync wait mode | *(contact)* |
| `XCG801` | Unknown sync update mode | *(contact)* |
| `XCG802` | Unknown EngineType | *(contact)* |
| `XCG804` | Function name too long: {funcName} | *(contact)* |
| `XCG805` | Unimplemented Activation function '{op}' | *(contact)* |
| `XCG806` | Unimplemented ALU opcode '{op}' | *(contact)* |
| `XCG807` | Unimplemented ALU opcode '{op}' | *(contact)* |
| `XCG808` | Unimplemented ALU opcode '{op}' | *(contact)* |
| `XCG809` | Unimplemented Axis List Type '{type}' | *(contact)* |
| `XCG810` | Unimplemented Dtype {type} | *(contact)* |
| `XCG811` | Unimplemented Compare Branch operator '{op}' | *(contact)* |
| `XCG812` | Queue instance name too long: {queName} | *(contact)* |
| `XCG813` | Expect axis type to be XYZWC | *(contact)* |
| `XCG814` | Unimplemented Engine Accumulator Command {acc} | *(contact)* |
| `XCG815` | Estimated peak HBM usage ({value}) exceeds {hbm_limit}GB. Neff might be unable to load on chip. If you believe this estimation to be inaccurate, you can disable the check using: `--internal-backend-options=' --disable-hbm-usage-check '` | *(contact)* |
| `XCG816` | TensorScalarPtr must have 2 or 3 inputs | *(contact)* |
| `XCG817` | TensorScalarPtr must have 2 or 3 inputs | *(contact)* |
| `XCG818` | Pool input AP must have 5 dimensions | *(contact)* |
| `XCG819` | Expect BNStatsAggr output 2 elements | *(contact)* |
| `XCG820` | Expect BNGradients mean and var type to be same | *(contact)* |
| `XCG821` | Malformed access pattern | *(contact)* |
| `XCG822` | Params should be 4 floats | *(contact)* |
| `XCG823` | StreamShuffle Mask must have 32 elements | *(contact)* |
| `XCG824` | StreamTranspose src pattern num_elem must be 1 | *(contact)* |
| `XCG825` | StreamTranspose op must have 1-D mem_pattern or read a factor of 32 elements | *(contact)* |
| `XCG826` | Collective {kind} does not support an ALU op | *(contact)* |
| `XCG827` | AllToAll does not support an ALU op | *(contact)* |
| `XCG828` | float32r matmult inputs must have same dtype | *(contact)* |
| `XCG829` | two pass matmult doesn't reuse previous weight | *(contact)* |
| `XCG830` | two pass matmult doesn't reuse previous weight | *(contact)* |
| `XCG831` | float32r matmult inputs must have same dtype | *(contact)* |
| `XCG832` | TensorSave destination must be SB on trn1, due to a HW bug | *(contact)* |
| `XCG833` | Unimplemented Compare Branch operator {op} | *(contact)* |
| `XCG834` | Function name too long: {targetName} | *(contact)* |
| `XCG835` | Temporary Limitation: Tensor library only supports 1 partition now! | *(contact)* |
| `XCG836` | Queue instance name too long: {Name} | *(contact)* |
| `XCG837` | MAX8 Instruction writes exactly 8 output elements per partition | *(contact)* |
| `XCG838` | MAX8 Instruction expects at least 8 input elements per partition | *(contact)* |
| `XCG839` | MAX8 Instruction supports at most 16384 input elements per partition | *(contact)* |
| `XCG840` | MAX8 Instruction doesn't allow more than 5 dimensions in input access pattern | *(contact)* |
| `XCG841` | MAX8 Instruction doesn't allow more than 3 dimensions in output access pattern | *(contact)* |
| `XCG842` | MAX8 Instruction expects the same number of partitions in input and output | *(contact)* |
| `XCG843` | MAX8 Instruction output is restricted to 2 dimensions | *(contact)* |
| `XCG844` | MATCH_VALUE_LOAD expects exactly 8 input elements per partition | *(contact)* |
| `XCG845` | MATCH_VALUE_LOAD Instruction doesn't allow more than 3 dimensions in input access pattern | *(contact)* |
| `XCG846` | expecting physical ap at in0 of MATCH_VALUE_LOAD Instruction | *(contact)* |
| `XCG847` | FIND_INDEX8 Instruction writes exactly 8 output elements per partition | *(contact)* |
| `XCG848` | FIND_INDEX8 Instruction expects at least 8 input elements per partition | *(contact)* |
| `XCG849` | FIND_INDEX8 Instruction supports at most 16384 input elements per partition | *(contact)* |
| `XCG850` | FIND_INDEX8 Instruction doesn't allow more than 5 dimensions in input access pattern | *(contact)* |
| `XCG851` | FIND_INDEX8 Instruction doesn't allow more than 3 dimensions in output access pattern | *(contact)* |
| `XCG852` | FIND_INDEX8 Instruction expects the same number of partitions in input and output | *(contact)* |
| `XCG853` | FIND_INDEX8 Instruction output is restricted to 2 dimensions | *(contact)* |
| `XCG854` | MATCH_VALUE_LOAD expects exactly 8 input elements per partition | *(contact)* |
| `XCG855` | MATCH_VALUE_LOAD Instruction doesn't allow more than 3 dimensions in input access pattern | *(contact)* |
| `XCG856` | MATCH_REPLACE8 Instruction expects at least 8 input elements per partition | *(contact)* |
| `XCG857` | MATCH_REPLACE8 Instruction supports at most 16384 input elements per partition | *(contact)* |
| `XCG858` | MATCH_REPLACE8 Instruction expects at least 8 input elements per partition | *(contact)* |
| `XCG859` | MATCH_REPLACE8 Instruction supports at most 16384 input elements per partition | *(contact)* |
| `XCG860` | MATCH_REPLACE8 Instruction doesn't allow more than 5 dimensions in input access pattern | *(contact)* |
| `XCG861` | MATCH_REPLACE8 Instruction doesn't allow more than 5 dimensions in output access pattern | *(contact)* |
| `XCG862` | MATCH_REPLACE8 Instruction expects the same number of partitions in input and output | *(contact)* |
| `XCG863` | ISA check failed | *(contact)* |
| `XCG864` | ISA check failed | *(contact)* |
| `XCG865` | Unsupported MatmultPerfMode {mode} | *(contact)* |
| `XCG866` | Target does not support Matmul replication | *(contact)* |
| `XCG867` | Matmult's Fmap ({ifmap}) and Weight ({weight}) partitions must match | *(contact)* |
| `XCG868` | Matmult's Output ({output}) partitions must match Weight ({weight}) elements per partition | *(contact)* |
| `XCG869` | Matmult's Output ({output}) and Fmap ({ifmap}) elements per partition must match | *(contact)* |
| `XCG870` | Invalid register ({regId}) | *(contact)* |
| `XCG871` | Could not open {file} - {strerr} | Verify that your filesystem is writeable |
| `XCG872` | ISA mem_pattern should only be SB/PSUM accesses | *(contact)* |
| `XCG873` | InstTensorScalarAffineSelect should have NaN fill value in codegen, instead has {value} | *(contact)* |
| `XCG874` | CollectiveKind {kind} not supported | *(contact)* |
| `XCG875` | CollectiveKind {kind} not supported | *(contact)* |
| `XCG876` | CollectiveKind {kind} requires 1 arguments per output, got num_arguments={num_arguments}, num_outputs={num_outputs} | *(contact)* |
| `XCG877` | CollectiveKind {kind} requires 2 arguments per output, got num_arguments={num_arguments}, num_outputs={num_outputs} | *(contact)* |
| `XCG878` | Tensor2D AP must have 2 dimensions, got {dim} | *(contact)* |
| `XCG879` | CollectiveKind {kind} does not support arguments outside of DRAM | *(contact)* |
| `XCG880` | CollectiveKind {kind} does not support outputs outside of DRAM | *(contact)* |
| `XCG890` | Matmul accumulation flag not valid, may caused by is still `auto` but should be specific value | *(contact)* |
| `XCG891` | Matmul accumulation flag not valid, may caused by is still `auto` but should be specific value | *(contact)* |
| `XCG892` | MatMulSparse accumulation flag not valid, may caused by is still `auto` but should be specific value | *(contact)* |
| `XCG893` | MatmulMX accumulation flag not valid, may caused by is still `auto` but should be specific value | *(contact)* |
| `XCG900` | Unknown sync wait mode | *(contact)* |
| `XCG901` | Unknown sync update mode | *(contact)* |
| `XCG902` | Unknown EngineType | *(contact)* |
| `XCG903` | Unimplemented Activation function {op} | *(contact)* |
| `XCG904` | Unimplemented ALU opcode {op} | *(contact)* |
| `XCG905` | Unimplemented ALU opcode {op} | *(contact)* |
| `XCG906` | Unimplemented ALU opcode {op} | *(contact)* |
| `XCG907` | Unimplemented Axis List Type {type} | *(contact)* |
| `XCG908` | Unimplemented Dtype {type} | *(contact)* |
| `XCG909` | Unimplemented Compare Branch operator {op} | *(contact)* |
| `XCG910` | Queue name too long: {queName} | *(contact)* |
| `XCG911` | Expect axis type to be XYZWC | *(contact)* |
| `XCG914` | Permute does not support an ALU op | *(contact)* |
| `XCG915` |  |  |
| `XCG916` | TensorScalarPtr must have 2 or 3 inputs | *(contact)* |
| `XCG917` | TensorScalarPtr must have 2 or 3 inputs | *(contact)* |
| `XCG918` | Unimplemented: TensorSave dest is not DRAM | *(contact)* |
| `XCG919` | bias can only take imm_fp32 when activation function has >= 128 code | *(contact)* |
| `XCG920` | Pool input AP must have 5 dimensions | *(contact)* |
| `XCG921` | out.getNumElementsPerPartition() == 2 | *(contact)* |
| `XCG922` | Expect BNGradients mean and var type to be same | *(contact)* |
| `XCG923` | Malformed access pattern | *(contact)* |
| `XCG924` | Params should be 4 floats | *(contact)* |
| `XCG925` | StreamShuffle Mask must have 32 elements | *(contact)* |
| `XCG926` | StreamTranspose src pattern num_elem must be 1 | *(contact)* |
| `XCG927` | StreamTranspose op must have 1-D mem_pattern or read a factor of 32 elements | *(contact)* |
| `XCG928` | AllGather does not support an ALU op | *(contact)* |
| `XCG929` | Queue instance name too long: {Name} | *(contact)* |
| `XCG930` | float32r matmult inputs must have same dtype | *(contact)* |
| `XCG931` | float32r matmult inputs must have same dtype | *(contact)* |
| `XCG932` | two pass matmult doesn't reuse previous weight | *(contact)* |
| `XCG933` | two pass matmult doesn't reuse previous weight | *(contact)* |
| `XCG934` | float32r matmult inputs must have same dtype | *(contact)* |
| `XCG935` | float32r matmult inputs must have same dtype | *(contact)* |
| `XCG937` | MAX8 Instruction writes exactly 8 output elements per partition | *(contact)* |
| `XCG938` | MAX8 Instruction expects at least 8 input elements per partition | *(contact)* |
| `XCG939` | MAX8 Instruction supports at most 16384 input elements per partition | *(contact)* |
| `XCG940` | MAX8 Instruction doesn't allow more than 5 dimensions in input access pattern | *(contact)* |
| `XCG941` | MAX8 Instruction doesn't allow more than 3 dimensions in output access pattern | *(contact)* |
| `XCG942` | MAX8 Instruction expects the same number of partitions in input and output | *(contact)* |
| `XCG943` | MAX8 Instruction output is restricted to 2 dimensions | *(contact)* |
| `XCG944` | MATCH_VALUE_LOAD expects exactly 8 input elements per partition | *(contact)* |
| `XCG945` | MATCH_VALUE_LOAD Instruction doesn't allow more than 3 dimensions in input access pattern | *(contact)* |
| `XCG946` | expecting physical ap at in0 of MATCH_VALUE_LOAD Instruction | *(contact)* |
| `XCG947` | FIND_INDEX8 Instruction writes exactly 8 output elements per partition | *(contact)* |
| `XCG948` | FIND_INDEX8 Instruction expects at least 8 input elements per partition | *(contact)* |
| `XCG949` | FIND_INDEX8 Instruction supports at most 16384 input elements per partition | *(contact)* |
| `XCG950` | FIND_INDEX8 Instruction doesn't allow more than 5 dimensions in input access pattern | *(contact)* |
| `XCG951` | FIND_INDEX8 Instruction doesn't allow more than 3 dimensions in output access pattern | *(contact)* |
| `XCG952` | FIND_INDEX8 Instruction expects the same number of partitions in input and output | *(contact)* |
| `XCG953` | FIND_INDEX8 Instruction output is restricted to 2 dimensions | *(contact)* |
| `XCG954` | MATCH_VALUE_LOAD expects exactly 8 input elements per partition | *(contact)* |
| `XCG955` | MATCH_VALUE_LOAD Instruction doesn't allow more than 3 dimensions in input access pattern | *(contact)* |
| `XCG956` | MATCH_REPLACE8 Instruction expects at least 8 input elements per partition | *(contact)* |
| `XCG957` | MATCH_REPLACE8 Instruction supports at most 16384 input elements per partition | *(contact)* |
| `XCG958` | MATCH_REPLACE8 Instruction expects at least 8 input elements per partition | *(contact)* |
| `XCG959` | MATCH_REPLACE8 Instruction supports at most 16384 input elements per partition | *(contact)* |
| `XCG960` | MATCH_REPLACE8 Instruction doesn't allow more than 5 dimensions in input access pattern | *(contact)* |
| `XCG961` | MATCH_REPLACE8 Instruction doesn't allow more than 5 dimensions in output access pattern | *(contact)* |
| `XCG962` | MATCH_REPLACE8 Instruction expects the same number of partitions in input and output | *(contact)* |
| `XCG963` | Unimplemented Compare Branch operator {op} | *(contact)* |
| `XCG964` | Function name too long: {targetName} | *(contact)* |
| `XCG965` | Instruction engine check failed ({engine}) | *(contact)* |
| `XCG966` | Instruction engine check failed ({engine}) | *(contact)* |
| `XCG967` | Value that is out-of-bounds for corresponding ISA field found: {details} | *(contact)* |
| `XCG970` | Innermost dimension step of transpose matmult output must be 1 | *(contact)* |
| `XCG971` | Outer dimension step sizes of transpose matmult output must be even or 1 | *(contact)* |
| `XCG972` | Invalid ALU {op} for RangeSelect instruction, expect one of IsEQ, isGT, IsGE, IsLE, IsLT | *(contact)* |
| `XCG973` | RangeSelect instruction expects the input and output access patterns to have the same data type (input is of {dtype0} type, but output is of {dtype1} type) | *(contact)* |
| `XCG974` | RangeSelect instruction expects the input and output access patterns to be one of float32, bfloat32, float16, float8 data types, but is {dtype} type | *(contact)* |
| `XCG975` | RangeSelect instruction expects the input and output access patterns to access the same number of elements (input has {inNum} elements, output has {outNum} elements) | *(contact)* |
| `XCG976` | RangeSelect instruction expects bound to be float32 type, but is {dtype} type | *(contact)* |
| `XCG977` | RangeSelect instruction base to be within DVE_RANGE = {DVE_RANGE} | *(contact)* |
| `XCG978` | RangeSelect instruction bound ({boundName}) + num bytes in partition of access pattern ({apName}) to be within DVE_RANGE = {DVE_RANGE} | *(contact)* |
| `XCG979` | RangeSelect instruction expects number of partitions accessed to be > 0 and <= 128, but {numPartitions} were accessed | *(contact)* |
| `XCG980` | InstRangeSelect only supports max reduce op, but received {opType} op | *(contact)* |
| `XCG981` | Matmult instruction must have PE tile position fully set or fully unset (let compiler decide) | Consult nc_matmult documentation for valid tile positions |
| `XCG983` | Matmult instruction must have PE tile size fully set or fully unset (let compiler decide) | Consult nc_matmult documentation for valid tile sizes |
| `XCG985` | CopyPredicated does not support casting in scalar mode | *(contact)* |
| `XCG986` | Invalid AluOpType '{given}' for DGE_COMPUTE_OP. Must be 'bypass' or 'add' | *(contact)* |
| `XCG987` | DVE_READ_INDICES Instruction expects total number of elements to be 8, but got {actual} | *(contact)* |
| `XCG988` | DVE_READ_INDICES Instruction expects AP type to be Dtype::Uint16 or Dtype::Uint32 | *(contact)* |
| `XCG989` | DVE_READ_INDICES Instruction expects number of partitions to be between 1 - 128 | *(contact)* |
| `XCG990` | Detected duplicate engine ({engine}) and label ({label}) for basic block {bb1} and {bb2}. Basic block must have unique (engine, label) pair | *(contact)* |
| `XCG991` | Expect ResetQueueInstance to have same instance as active instance on queue, but is not | *(contact)* |
| `XCG992` | Expect module attribute neff_feature_SQI_no_rearm to be set by previous SwitchQueueInstance instruction, but is not | *(contact)* |
| `XCG993` | Unimplemented ALU opcode '{op}' | *(contact)* |
| `XCG994` | CopyPredicatedReduce instruction immediate value must be a float32, but it is a {actual} | *(contact)* |
| `XCG995` | DveReadAccumulator output must be a floating-point type, but it is Dtype::{actual} | *(contact)* |
| `XCG996` | Unsupported branch hint outcome type {hint_outcome} | *(contact)* |
| `XCG997` | Expect function program counter for all datapath engines to be 1 for the preamble, but found {count} for {engine} | *(contact)* |
| `XCG998` | Instruction is not assigned an engine when updating program counter | *(contact)* |
| `XCG999` | branch label for BasicBlock {BasicBlockName} was not generated in enterFunction | *(contact)* |
| `XCG1000` | Cannot find program counter for location instruction {instName} engine {engineType} | *(contact)* |
| `XCG1001` | Expect there to be only one ISA instruction from BIR BranchHint instruction, but found multiple at PCs {oldPC} and {newPC} | *(contact)* |
| `XCG1002` | InstExit must have a datapath engine type after expansion, but it has EngineType::{actual} | Please be sure that Instruction is assigned to ALL so that it can properly be expanded to all datapath engines by expand_all_engine |
| `XCG1003` | CollectiveCompute does not have a streamID set. Make sure a compatible opt level which runs `InferStreamIds' is set. | *(contact)* |
| `XCG1004` | Matmult instruction has invalid PE row tile position with respect to the accessed start partition of stationary tensor | Consult nc_matmult documentation for valid tile positions |
| `XCG1005` | Matmult instruction has invalid PE column tile position with respect to the accessed start partition of PSUM output tensor | Consult nc_matmult documentation for valid tile positions |
| `XCG1006` | Matmult instruction has invalid PE row tile size with respect to the accessed start partition of stationary tensor | Consult nc_matmult documentation for valid tile sizes |
| `XCG1007` | Matmult instruction has invalid PE column tile size with respect to the accessed start partition of PSUM output tensor | Consult nc_matmult documentation for valid tile sizes |
| `XCG1008` | Compiler generates invalid PE row tile size {row_tile_size} for Matmult instruction with accessed start partition {in1_base_partition} of stationary tensor | *(contact)* |
| `XCG1009` | Compiler generates invalid PE column tile size {col_tile_size} for Matmult instruction with accessed start partition {out_base_partition} of PSUM output tensor | *(contact)* |
| `XCG1010` | Vector DGE using shape register has {dim} dimensions, shape register supports only 5 dimensions | Consult nc_matmult documentation for valid tile sizes |
| `XCG1011` | Unrecognized UniqueTensorsType | *(contact)* |
| `XCG1012` | InstAllEngineBarrier must be a datapath engine. Given EngineType::{actual}. | Assign to EngineType::ALL and allow it to be expanded to all engines by the expand_all_engine pass. |
| `XCG1013` | CollectiveDimension {dimension} not supported | *(contact)* |
| `XCG1014` | Unrecognized EventSemaphoreClearMode {mode} | *(contact)* |
| `XCG1015` | InstGroupResetSemaphores must be a datapath engine. Given EngineType::{actual}. | Assign to EngineType::ALL and allow it to be expanded to all engines by the expand_all_engine pass. |
| `XCG1016` | InstGroupResetSemaphores has an empty SemaGroup vector. Should have been eliminated before this point. | Ensure that instruction has EngineType::ALL so that expand_all_engines can run and expand or eliminate as needed. |

#### XDR — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `XDR001` | No state for predecessor instruction ({name}) available. | Make sure graph is topologically sorted. |

#### XEI — bare (8 codes)

| Code | Cause | Resolution |
|---|---|---|
| `XEI001` | Internal testing error, used in walrus pass test on module: {name}. | Should only occur during walrus_pass_test. |
| `XEI002` | Internal testing error, used in walrus pass test on modules: {names}. | Should only occur during walrus_pass_test. |
| `XEI418` | I'm a teapot | Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. |
| `XEI419` | I'm a teapot | Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. |
| `XEI420` | I'm a teapot | Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. |
| `XEI421` | I'm a teapot | Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. |
| `XEI422` | I'm a teapot | Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. |
| `XEI423` | A small formatted test with the number {test_arg_1} and some {test_arg_2}. | *(contact)* |

#### XGM — bare (6 codes)

| Code | Cause | Resolution |
|---|---|---|
| `XGM000` | Expected the subgraph {sgId} for all cores to have {expected} functions, but on core {ncId} it has {actual} functions | *(contact)* |
| `XGM001` | Expected subgraph{sgId} on core {ncId} to have a function named {name} | *(contact)* |
| `XGM002` | Expected function {name} in subgraph {sgId} to have {expected} basic blocks, but on core {ncId} it has {actual} basic blocks | *(contact)* |
| `XGM003` | Expected the subgraph {sgId} for all cores to have {expected} functions, but on core {ncId} it has {actual} functions | *(contact)* |
| `XGM004` | Expected subgraph{sgId} on core {ncId} to have a function named {name} | *(contact)* |
| `XGM005` | Expected function {name} in subgraph {sgId} to have {expected} basic blocks, but on core {ncId} it has {actual} basic blocks | *(contact)* |

#### XLB — bare (35 codes)

| Code | Cause | Resolution |
|---|---|---|
| `XLB000` | The number of modules should be the same as LNC. Current module size: {module}, current LNC: {lnc} | *(contact)* |
| `XLB001` | Identical function name must exist. The function name {funcName} is missing in module {modName} | *(contact)* |
| `XLB002` | The number of basicblocks should be the same across LNC modules. bbCnt: {bbNum1} in module {modName1}, {bbNum2} in module {modName2} | *(contact)* |
| `XLB003` | parallelBB size ({parallelBBSize}) should be the same as the number of LNC modules ({modNum}) | *(contact)* |
| `XLB004` | The number of basicblocks should be the same across LNC modules. bbCnt: {bbNum1} in module {modName1}, {bbNum2} in module {modName2} | *(contact)* |
| `XLB005` | DummyCall should not appear in the middle of BIRs. LowerDMADummycall inst: {instName} has predecessor. | *(contact)* |
| `XLB006` | Non DMA instNode or non dummy core barrier instNode should have instruction pointer. InstNode {instNodeName} does not have instruction | *(contact)* |
| `XLB007` | Matmult instruction's engine should be always PE. Currently instruction {instName}'s engine is {engineName} | *(contact)* |
| `XLB008` | Dynamic DMA instruction should have an assigned queue. Currently instruction {instName} does not have an assigned queue. | *(contact)* |
| `XLB009` | Missing barrier check only supports LNC=2. The number of LNC modules: {modNum} | *(contact)* |
| `XLB010` | InstBarrierRangeMap should have been initialized | *(contact)* |
| `XLB011` | InstBarrierRangeMap should have been initialized | *(contact)* |
| `XLB012` | Max barrier id should be greater than Min barrier id. Current Max barrier id: {maxId} Min barrier id: {minId} for engine {engineName}. | *(contact)* |
| `XLB013` | Engine name should not be duplicated. Engine {engineName} appears twice. | *(contact)* |
| `XLB014` | Barrier range start should less than or equal to end. Current start id: {startId}, end id: {endId} of instruction {instName} for engine {engineName} | *(contact)* |
| `XLB015` | Barrier range start should less than or equal to end. Current start id: {startId}, end id: {endId} of instruction {instName} for engine {engineName} | *(contact)* |
| `XLB016` | Tensor {name} is not found in remote core {core} | *(contact)* |
| `XLB017` | Function {name} is not found in remote core {core} | *(contact)* |
| `XLB018` | Symbolic Access pattern exists on an argument of inst: {readerInst}. | *(contact)* |
| `XLB019` | Symbolic Access pattern exists on an output of inst: {writerInst}. | *(contact)* |
| `XLB020` | Unexpected Memory spec which is shared but allocated - Name: {memoryName}, type: {memoryType}, kind: {kind}, addressSpace: {addressSpace}, isAllocated: {isAllocated}. | *(contact)* |
| `XLB021` | Symbolic Access pattern exists on an argument of inst: {readerInst}. | *(contact)* |
| `XLB022` | Symbolic Access pattern exists on an output of inst: {writerInst}. | *(contact)* |
| `XLB023` | Symbolic Access pattern exists on an argument of inst: {readerInst}. | *(contact)* |
| `XLB024` | Symbolic Access pattern exists on an output of inst: {writerInst}. | *(contact)* |
| `XLB025` | Remote target for non-SB and non-DRAM - Name: {memoryName}, type: {memoryType}, kind: {kind}, addressSpace: {addressSpace}, isAllocated: {isAllocated}. | *(contact)* |
| `XLB026` | Unexpected Memory spec - Name: {memoryName}, type: {memoryType}, kind: {kind}, addressSpace: {addressSpace}, isAllocated: {isAllocated}. | *(contact)* |
| `XLB027` | CoreBarrier instruction happens other than ACT/PE/DVE/SP/Pool engines. engine name: {engineName}. | *(contact)* |
| `XLB028` | Collective Compute instruction should have an assigned queue. Currently instruction {instName} does not have an assigned queue. | *(contact)* |
| `XLB029` | CoreBarrier appears in core 0 for {engineName}, but core 1 does not have any CoreBarrier in the same engine. | *(contact)* |
| `XLB030` | Dynamic offset expression size is not 1 from dynamic AP. | *(contact)* |
| `XLB031` | Dynamic offset expression size is not 1 from dynamic AP. | *(contact)* |
| `XLB032` | Indirect arg id does not fit to the number of arguments in input of {instName}. | *(contact)* |
| `XLB033` | Indirect arg id does not fit to the number of arguments in input of {instName}. | *(contact)* |
| `XLB034` | Found {races} pair(s) of instructions missing core barriers. | *(contact)* |

#### XLS — bare (5 codes)

| Code | Cause | Resolution |
|---|---|---|
| `XLS001` | Unable to clone or move module artifact directory when concretizing module from source={src} to destination={dst}. Error obtained: {error} | *(contact)* |
| `XLS002` | The artifact directory path {path} associated with the input module does not exist on disk. | *(contact)* |
| `XLS003` | A directory already exists at {path}. Therefore, cannot create an artifact directory for concrete module | *(contact)* |
| `XLS004` | Unable to clone or move module artifact directory when concretizing module from source={src} to destination={dst} | *(contact)* |
| `XLS005` | Error when copying {src} to {dst}. Error obtained: {error} | *(contact)* |

#### XLV — bare (35 codes)

| Code | Cause | Resolution |
|---|---|---|
| `XLV000` | Expected {expected} core barriers in subgraph {sgId}, but found {actual} on core {ncId} | *(contact)* |
| `XLV001` | Expected core barriers to have id {expected}, but core barrier {expected} in subgraph {sgId} on core {ncId} has id {actual} | *(contact)* |
| `XLV002` | Expected core barriers to have name '{expected}', but core barrier {expectedIdx} in subgraph {sgId} on core {ncId} has name '{actual}'. This likely indicates mismatching names on a local SendRecv or AllReduce instruction in the input program - the corresponding instructions on different cores must have the same name. | *(contact)* |
| `XLV003` | lnc_verifier failed with the following errors:{errors} | *(contact)* |
| `XLV004` | Subgraph {sgId} expected to find function {name}, but it is missing on core {ncId}. | *(contact)* |
| `XLV005` | Function {name} in subgraph {sgId} has {actual_size} basic blocks for core {ncId}, but expected {expected_size} basic blocks. | *(contact)* |
| `XLV006` | Subgraph {sgId} was expected to have {expected_size} functions, but core {ncId} has {actual_size} functions. | *(contact)* |
| `XLV007` | Expected block name {expected_block_name}, but got {actual_block_name} on core {ncId} in function {name} of subgraph {sgId}. | *(contact)* |
| `XLV008` | Remote target memory location {remote_target_name} from subgraph {sgId} not found in core {ncId}. | *(contact)* |
| `XLV009` | Shared tensor with address {address} of subgraph {sgId} missing on core {ncId}. | *(contact)* |
| `XLV010` | The shared memory location {name} in subgraph {sgId} on core {ncId} doesn't have remoteLocalTarget set. | *(contact)* |
| `XLV011` | Expected {expected} GPSIMDSB2SBs in subgraph {sgId}, but found {actual} on core {ncId} | *(contact)* |
| `XLV012` | Modules may not have both concrete neuron core ids and symbolic shard ids, but both are in function {func_name} for core {nc_id} | *(contact)* |
| `XLV013` | Only one symbolic shard id is expected per function. Multiple found instead in function {func_name}. | *(contact)* |
| `XLV014` | The execution of the core barrier instruction {cb} is masked out on some lnc cores. | *(contact)* |
| `XLV015` | Mismatching GPSIMDSB2SB instructions across LNC cores. | *(contact)* |
| `XLV016` | Multiple modules without neuron core ids found. | *(contact)* |
| `XLV017` | Data tensor in tensor indirect AP must be in either PSUM or SBUF, tensor was in {mem_type} instead | *(contact)* |
| `XLV018` | Expected {indices} elements in tensor indirect AP, but found {cols} instead | *(contact)* |
| `XLV019` | Index tensor in tensor indirect AP must be in SBUF and have type uint16, index is in {idx_mem} and has type {dtype} instead | *(contact)* |
| `XLV020` | Index AP for tensor indirection must access number of partitions divisible by 16 and start at partition divisible by 32, Index AP accesses {idx_accessed} starting at partition {idx_base} instead | *(contact)* |
| `XLV021` | Index AP for tensor indirection must access more or equal partitions than the tensor indirect AP, index AP: {idx_accessed} vs tensor indirect AP {parts} | *(contact)* |
| `XLV022` | Indirect Tensor AP cannot be on activation engine with PSUM indirect destination | *(contact)* |
| `XLV023` | First source AP and index AP for tensor indirection must start in the same quadrant for scatter style indirection. Data AP starts at partition {base} vs index AP {idx_base} | *(contact)* |
| `XLV024` | Source data AP must be contiguous for scatter style indirection | *(contact)* |
| `XLV025` | Data AP and index AP for gather style tensor indirection must start in same quadrant, data AP starts at {base} vs index AP starts at {idx_base} | *(contact)* |
| `XLV026` | For scatter and gather tensor indirection, index tensors must be in same quadrant, src index base partition {src_idx_base} vs dst index base partition {dst_idx_base} | *(contact)* |
| `XLV027` | Data tensor in symbolic tensor indirect AP must be in either PSUM or SBUF, tensor was in {mem_type} instead | *(contact)* |
| `XLV028` | Number of indices ({indices}) cannot exceed the total number of elements per partition group ({elems_per_partition_group}) in tensor indirect index AP | *(contact)* |
| `XLV029` | Expected {indices} elements in symbolic tensor indirect AP, but found {cols} instead | *(contact)* |
| `XLV030` | Index tensor in symbolic tensor indirect AP must be in SBUF and have type uint16, index is in {idx_mem} and has type {dtype} instead | *(contact)* |
| `XLV031` | Index AP for symbolic tensor indirection must access more or equal partitions than the symbolic tensor indirect AP, index AP: {idx_accessed} vs tensor indirect AP {parts} | *(contact)* |
| `XLV032` | Symbolic indirect Tensor AP cannot be on activation engine with PSUM indirect destination | *(contact)* |
| `XLV033` | Source symbolic data AP must be contiguous for scatter style indirection | *(contact)* |
| `XLV034` | Number of indices ({indices}) cannot exceed the total number of elements per partition group ({elems_per_partition_group}) in symbolic tensor indirect index AP | *(contact)* |

#### XNP — bare (17 codes)

| Code | Cause | Resolution |
|---|---|---|
| `XNP001` | Unknown EngineType: {engine} | *(contact)* |
| `XNP002` | Unknown DMA queue type | *(contact)* |
| `XNP003` | Wrong arch type is used, must use CoreV2 or newer | *(contact)* |
| `XNP004` | Expected all subgraphs to have the same arch {target_arch}, but module {mod_name} targets {arch} | *(contact)* |
| `XNP016` | Expected description file '{path}' does not exist | *(contact)* |
| `XNP017` | Missing file name for DVE table entry. | *(contact)* |
| `XNP018` | Missing act_info for used func set {usedActSet} | *(contact)* |
| `XNP019` | Unable to open file '{path}' | *(contact)* |
| `XNP020` | Unable to open file '{srcPath}' | *(contact)* |
| `XNP021` | Compiler did not generate an activation entry. | *(contact)* |
| `XNP022` | Unexpected non-CC tensor {memLoc} between ranges [0 - {range_start}) or [{range_end} - {high_water_mark}), but got {addr} | *(contact)* |
| `XNP023` | Expected function {function} to have hbm related function attributes | *(contact)* |
| `XNP024` | Expected collective tensor addresses to start at {range_start}, but found one at {addr}. | *(contact)* |
| `XNP025` | Expected non-collective tensor addresses to start at {main_start}, but found one at {addr}. | *(contact)* |
| `XNP026` | DRAM tensor '{name}' has unexpected MemoryAddressSpace::{space}, only Local is supported at this point. | *(contact)* |
| `XNP027` | Expected {vnc_nc_count} modules, but got {module_size} in neff_packager | *(contact)* |
| `XNP028` | Unrecognized runtime reserve memory type | *(contact)* |

#### XRA — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `XRA001` | Expected number of target cores {core_count} to match number of subgraphs {mod_count} | *(contact)* |
| `XRA002` | Expected to find {remote} on target core {core}, but got null for {local} | *(contact)* |

#### XRO — bare (2 codes)

| Code | Cause | Resolution |
|---|---|---|
| `XRO001` | Undefined DRAM Memloc {mem} | *(contact)* |
| `XRO002` | Undefined SB Memloc {mem} | *(contact)* |

#### XTP — bare (1 codes)

| Code | Cause | Resolution |
|---|---|---|
| `XTP002` | Number of instructions ({insts}) is over the threshold ({threshold}). Tiling could potentially do a better job. | *(contact)* |

#### XVL — bare (4 codes)

| Code | Cause | Resolution |
|---|---|---|
| `XVL001` | Expected number of target cores {core_count} to match number of subgraphs {mod_count} | *(contact)* |
| `XVL002` | Expected to find {remote} on target core {core}, but got null for {local} | *(contact)* |
| `XVL003` | Expected all subgraphs to share the same number of DMA blocks that modify remote tensors, but found {sizes} | *(contact)* |
| `XVL004` | Expected to find local semphore update in instruction {name} | *(contact)* |

#### XXU — bare (3 codes)

| Code | Cause | Resolution |
|---|---|---|
| `XXU001` | Immediate value for addRegAluOneRegArgOneImm ({value}) does not fit in 32-bits | *(contact)* |
| `XXU002` | {inst_name}: Invalid operation - tile position only available for Matmult | *(contact)* |
| `XXU003` | {inst_name}: Source and destination must be allocated before calculating tile position | *(contact)* |

---

## 5. NKI `err_*` Funnel (sema)

The NKI front-end semantic-analysis layer (`nki/compiler/backends/neuron/sema.so`, md5 `755c15de65e6cc00083fab0e5838e4a0`) validates every `nl.*`/`nisa.*` API call's operands during kernel *tracing*, before lowering to penguin.ir. On failure it raises a user-facing exception. The ~150 diagnostic symbols form a three-tier funnel: `check_*` validators (32) → `assert_*` engine helpers (24) → `err_*` diagnostic leaves (78). Every `err_*` is wrapped by `sema_err_url`, which appends the docs link `https://awsdocs-neuron.readthedocs-hosted.com/en/latest/nki/api/nki.errors.html`. (full per-diagnostic reference on [§6.4.5](../nki/diagnostic-catalog.md).)

> **NOTE —** these are a *fourth*, separate surface: `err_*` raise Python exceptions (`NKISyntaxError` / `ValueError` / `TypeError` / `IndexError` / `RuntimeError`) that propagate up to the `nki.jit` driver and are then surfaced by the A08/A13 logger layers — i.e. the `ErrorMessages`/`NCC_*` machinery sits *above* this layer and reports what it raises. All 78 `err_*` are hard compile-time errors; the lone non-fatal diagnostic is `warn_block_dimension_is_deprecated` (`DeprecationWarning`).

### 5a. `err_*` leaves by concern (78)

Addresses are `sema.so` file VAs; line numbers are `sema.py` source lines (Cython `AddTraceback`). `exc` = the raised exception class (`NKISyntaxError*` = a Cython-cached class slot whose literal name was not recovered; `NKISyntaxError` is the documented NKI default, class at `0xf6c20`).

| `err_*` | Addr · `sema.py:L` | exc | Message (interpolated) |
|---|---|---|---|
| `err_num_partition_exceed_arch_limit` | `0x99e90` · 2420 | ValueError | `number of partitions <par_dim> exceed architecture limitation of <max_p>` (128) |
| `err_num_partition_mismatch` | `0x807f0` · 2398 | ValueError | `number of partitions mismatch in parameters (<shapes>)` |
| `err_size_of_dimension_exceed_arch_limit` | `0x77500` · 2366 | ValueError | `size of dimension <dim> in '<name><shape>' of '<api>' exceed architecture limitation of <max_size>` |
| `err_exceed_max_supported_dimension` | `0xbf3a0` · 2343 | ValueError | `'<name><shape>' … exceed max supported number of dimension <max_rank>` |
| `err_stack_overflow_sbuf` | `0x813a0` · 1763 | RuntimeError | `stack overflow: required sbuf size <sbuf_size> exceeds available sbuf size <target>` |
| `err_stack_overflow_psum` | `0x81f60` · 1756 | RuntimeError | `stack overflow: required psum banks <n> exceeds available psum banks <target>` |
| `err_valid_size` | `0x63bd0` · 2867 | NKISyntaxError* | `<name> size must be in [<valid_size>]` |
| `err_tile_shape_mismatch` | `0x4ba30` · 1330 | NKISyntaxError* | `tile shape mismatch, expected '<expected_shape>' … <name>/<tile>` |
| `err_annotation_shape_mismatch` | `0x4cd30` · 2441 | TypeError | `shape of \`<name><shape>\` does not match the expected shape of <annotation_shape>` |
| `err_param_shape_mismatch` | `0x90430` · 360 | ValueError | `Parameter shapes (<shapes>) … has mismatched shapes` |
| `err_param_shape_incompatible_with_numpy` | `0x8f6d0` · 354 | ValueError | `Parameter shapes (<shapes>) … could not be broadcast together` |
| `err_param_shape_incompatible_with_matmul` | `0xee450` · 343 | ValueError | matmul contraction-shape incompatibility (`<transpose_x>`, `<shapes>`) |
| `err_store_dst_shape_smaller_than_other_shape` | `0xbe290` · 1958 | ValueError | `Illegal assignment destination shape in '<expr>': … '<dst_name>' is smaller than other parameter shape <shapes>` |
| `err_indices_shape_mismatch` | `0x705c0` · 1985 | IndexError | `shape mismatch: indices indexing tensor <tensor_name> … <shapes>` |
| `err_leading_dimension_of_tensor_must_be_partition` | `0x7f070` · 2519 | NKISyntaxError* | `The leading dimension of SBUF/PSUM tensors must be the partition dimension. The tensor has shape <tensor_shape>` |
| `err_supported_operand_dtype` | `0xce320` · 332 | TypeError | `'<name>' … expected one of the following dtypes: <expected_dtypes>` |
| `err_unsupported_dtype_value` | `0xd3ec0` · 327 | ValueError | `Unsupported dtype '<dtype_value>' …, expected one of: <expected_dtypes>` |
| `err_unsupported_args` | `0x628b0` · 313 | TypeError | `'<api>' with unsupported arguments on nki tensor: <e>/<sign>` |
| `err_activation_bias_invalid_type` | `0x7fc30` · 1573 | TypeError | `'bias' param of '<instr_name>' must be a vector of type float32, float16, or bfloat16, got '<dtype>'` |
| `err_activation_scale_invalid_type` | `0x84330` · 1558 | TypeError | `'scale' param of '<instr_name>' must be a scalar or vector of type float32, got '<dtype>'` |
| `err_activation_scale_scalar_or_vector` | `0x4c6a0` · 1540 | NKISyntaxError* | `'scale' param of 'activation' can only be a scalar or a vector in partition dimension, scale.shape=<shape>` |
| `err_src_dst_same_dtype` | `0x50c70` · 2861 | NKISyntaxError* | `<api> src and dst must have same data type but got <src>/<dst>` |
| `err_tile_index_add_non_integer_scalar` | `0xa73e0` · 1752 | TypeError | `tile_index can only add with integer scalar, got scalar with dtype <dtype>` |
| `err_unexpected_type_of_operand` | `0x8a800` · 1339 | TypeError | operand type not among `<expected_types>` |
| `err_operand_cannot_be_none` | `0x941e0` · 323 | ValueError | `'<name>' cannot be None.` |
| `err_incorrect_target_type` | `0xd5170` · 1481 | NKISyntaxError* | target type `<target_type>` not in `<expected_targets>` for op `<name>` |
| `err_unsupported_memory` | `0xda290` · 1810 | TypeError | `Expected operand '<name>' of '<api>' to be in address space '<expected_addr_space>', but got a <tile> instead.` |
| `err_shared_hbm_must_in_kernel_level` | `0xeb420` · 2080 | RuntimeError | `shared_hbm buffer can only be created top level kernel scope <scope>` |
| `err_hbm_tensor_with_init_value_not_supported` | `0x44850` · 1499 | NKISyntaxError* | `Creating HBM tensor with init value is not supported.` |
| `err_tensor_creation_on_scratchpad_with_init_value_not_supported` | `0x44a20` · 1485 | NKISyntaxError* | `Creating SBUF/PSUM tensor with init value is not supported in allocated kernels.` |
| `err_bias_tensor_must_be_specified_in_allocation` | `0x44680` · 1521 | NKISyntaxError* | `Bias tensor for activation op must be specified in allocated kernel!` |
| `err_transpose_on_tensor_engine_not_allowed_in_allocated_kernel` | `0x444b0` · 1590 | NKISyntaxError* | feature gate (nc_transpose on TensorEngine / matmul w/o transpose_x in allocated kernel) |
| `err_instruction_unsupported_op` | `0x5d880` · 1399 | NKISyntaxError* | `<op_name> is not supported in '<inst_name>'` |
| `err_instruction_engine_unsupported_op_comb` | `0xba320` · 1409 | NKISyntaxError* | unsupported `(op0, op1)` pair on engine |
| `err_instruction_supported_dtypes` | `0x98ac0` · 1420 | NKISyntaxError* | `<inst>.<param>` on `<target>` dtype `<dtype>`, supported: `<supported_dtypes>` |
| `err_instruction_unsupported_dtypes` | `0x975f0` · 1430 | NKISyntaxError* | dtype `<dtype>` ∈ per-target blacklist for inst/param/target |
| `err_tensor_int32_add_multiply_supported_engine` | `0xb2490` · 2898 | NKISyntaxError* | int32 add/multiply only on engine(s) `<multiple_engines>` |
| `err_par_reduce_unsupported_op` | `0xeace0` · 1454 | NKISyntaxError* | `par_reduce does not support operator=<op_name>` |
| `err_reduce_unsupported_negate` | `0x76650` · 1458 | NKISyntaxError* | `negate option can only be used with arithmetic ops, unsupported op=<op_name>` |
| `err_reduce_bitvec_op_invalid_dtype` | `0x85c20` · 1440 | NKISyntaxError* | bitvec reduce op requires integer dtype, got `<dtype>` |
| `err_par_reduce_bitvec_op_invalid_dtype` | `0x84ee0` · 1447 | NKISyntaxError* | partition bitvec reduce requires integer dtype, got `<dtype>` |
| `err_bitvec_operand_must_be_integer` | `0xbc170` · 1462 | NKISyntaxError* | bitvec op operand `<operand_name>` dtype `<dtype>` must be integer |
| `err_mask_not_equal_not_supported` | `0x44bf0` · 1468 | NKISyntaxError* | `'not equal' mask is not supported` |
| `err_logic_or_not_supported` | `0x94f50` · 339 | ValueError | `'logical or' is not supported for the \`<name>\`` |
| `err_exact_arch_support_for_inst` | `0xbd240` · 2910 | NKISyntaxError* | inst `<inst_name>` available only on `<available_targets>` |
| `err_min_arch_support_for_inst` | `0xe3620` · 2919 | NKISyntaxError* | inst `<inst_name>` requires min target `<min_target>` |
| `err_min_arch_support_for_hwdge` | `0x69130` · 2927 | NKISyntaxError* | HW DGE mode `<dge_mode>` for inst `<inst_name>` requires min target `<min_target>` |
| `err_multi_cores_spmd_not_supported` | `0x7d0a0` · 2064 | RuntimeError | `SPMD grid with multi neuron cores is not supported on <target>` |
| `err_tensor_access_out_of_bound` | `0x5ae50` · 1840 | IndexError | `Out-of-bound access for tensor \`<tensor_name>\` on dimension … index range <oob_range> exceed dimension size of <shape>` |
| `err_tensor_access_out_of_bound_1range_string` | `0xa1500` · 1885 | NKISyntaxError* | helper formatting `<range_tuple>` for the OOB message |
| `err_tensor_index_not_supported` | `0x9cda0` · 2124 | NKISyntaxError* | tensor `<tensor_name>` indexed with unsupported index `<index>` (`<type_name>`) |
| `err_cannot_assign_to_index` | `0x46510` · 2008 | TypeError | `'index' tensor does not support item assignment` |
| `err_unsupported_mixing_basic_advanced_tensor_indexing` | `0x43d70` · 1934 | NKISyntaxError* | `Mixing basic tensor indexing and advanced tensor indexing is not supported.` |
| `err_indirect_indices_free_dim` | `0x439d0` · 2133 | NKISyntaxError* | `Indirect indexing on free dimension not supported, must be partition dimension or block dimension.` |
| `err_indirect_indices_sbuf` | `0x43800` · 2158 | NKISyntaxError* | `Indirect indices tile must be on SBUF.` |
| `err_indirect_indices_are_not_supported_with_nki_api` | `0xaafb0` · 2459 | NKISyntaxError* | `cannot use indirect indexing access with the current NKI API for <indirect_operands>` |
| `err_copy_dynamic_indirect_indices_not_natively_supported` | `0xebba0` · 2484 | NKISyntaxError* | `cannot copy a tensor with indirect memory reference access to \`<lhs>\`` (use `nisa.tensor_copy_dynamic_src`) |
| `err_atomic_rmw_add_only` | `0x46b10` · 2163 | NKISyntaxError* | `atomic_rmw \`op\` param only supports 'add' operation currently. … op=<op>` |
| `err_atomic_rmw_dynamic_index` | `0x43630` · 2170 | NKISyntaxError* | `atomic_rmw \`dst\` only supports indirect dynamic indexing currently.` |
| `err_1d_arange_not_supported` | `0xa2240` · 1716 | NKISyntaxError* | `<tensor_name> with 1d arange is not supported.` |
| `err_unexpected_output_dependencies` | `0xec2e0` · 1895 | NKISyntaxError* | `Unexpected output dependencies <missing_indices>` (parallel iterations write same location) |
| `err_dynamic_control_flow_not_supported` | `0x442e0` · 1620 | NKISyntaxError* | `dynamic control-flow depending on tensor value is not supported.` |
| `err_control_flow_condition_depending_on_arange` | `0x43f40` · 1688 | NKISyntaxError* | `Control-flow depending on \`nl.arange\` or \`nl.mgrid\` is not supported.` |
| `err_unsupported_expression_in_mask` | `0x44110` · 1635 | ValueError | `NKI mask expressions must be affine expressions of static loop indices. …` |
| `err_while_loop_requires_unconditional_entry` | `0x43290` · 2584 | ValueError | `Traditional while loops are not supported in NKI. Use the do-while pattern instead: …` |
| `err_ambiguous_tensor_truth_value` | `0x43460` · 2533 | ValueError | cannot evaluate truth value of a multi-element tensor (use `~`/`&`/`|`) |
| `err_nki_api_outside_of_nki_kernel` | `0x43ba0` · 2027 | RuntimeError | `calling NKI API outside of NKI kernels is not supported.` |
| `err_nested_kernel_with_spmd_grid` | `0xb4270` · 2038 | NKISyntaxError* | `<func_name>[<grid>]) inside another kernel is not supported.` |
| `err_program_id_axis_out_of_bounds` | `0x83730` · 1323 | IndexError | `axis in \`program_id\` is out-of-bound(s) of the spmd launch grid (axis=<axis>, rank <rank>)` |
| `err_local_variable_used_out_of_scope` | `0xb4f00` · 1257 | NKISyntaxError* | `Local variable '<name>' is referenced outside of its parent scope <parent_scope>` |
| `err_tensor_output_not_written_to` | `0xeca20` · 2187 | ValueError | `<tensor_name> … never written to` (often a dead loop) |
| `err_cannot_update_immutable_parameter` | `0x45f10` · 2287 | TypeError | `Cannot update immutable parameter \`<tensor_name>\`` |
| `err_mutable_parameter_not_returned` | `0xaa470` · 2242 | NKISyntaxError* | `<tensor_names> … mutable kernel parameter not returned` |
| `err_failed_to_infer_tile_from_local_tensor` | `0x64f90` · 1770 | TypeError | `Failed to infer tile from tensor '<tensor_name>': the first dimension … is not the partition dimension` |
| `err_tiled_offloaded_memcpy_same_shape` | `0x4aec0` · 2175 | NKISyntaxError* | `src and dst must have the same shape, src.shape=<src> dst.shape=<dst>` |
| `err_tiled_offloaded_fma_same_length` | `0x95cc0` · 2181 | NKISyntaxError* | `srcs and scales must have the same length, len(srcs)=<srcs>, len(scales)=<scales>` |
| `err_expected_constant_value` | `0x7c4a0` · 1472 | NKISyntaxError* | expected a compile-time constant `<expected>`, got `<value>` |
| `err_nki_param_not_hashable` | `0x82b20` · 2071 | ValueError | `'<name>' to be hashable, but got type <ty>` |
| `err_unsupported_memory` (addr cross-listed above) | — | — | — |
| `warn_block_dimension_is_deprecated` | `0xd5ea0` · 2512 | DeprecationWarning | `Block dimension is deprecated. The leading dimension of <tensor>…` (non-fatal) |

### 5b. `check_*` validators → `err_*` dispatch (32)

| `check_*` validator | Addr | Dispatches to |
|---|---|---|
| `check_tensor` | `0x61bb0` | `err_unexpected_type_of_operand` |
| `check_tile` | `0x61010` | `assert_is_tile` |
| `check_dtype` | `0xc0510` | `err_operand_cannot_be_none`, `err_unsupported_dtype_value` |
| `check_tensor_dtype` | `0xcf5c0` | `err_supported_operand_dtype` |
| `check_tensor_addr_space` | `0xd2c60` | `err_unsupported_memory` |
| `check_param_type` | `0xcaae0` | `err_operand_cannot_be_none`, `err_unexpected_type_of_operand` |
| `check_parameter_shapes_np_broadcast` | `0x789a0` | `err_param_shape_incompatible_with_numpy` |
| `check_parameter_shapes_without_broadcast` | `0xabeb0` | `err_param_shape_mismatch` |
| `check_par_dim_sizes` | `0x6f060` | `err_num_partition_exceed_arch_limit` |
| `check_par_dim_size_match` | `0x8c0e0` | `err_num_partition_mismatch` |
| `check_free_dim_size_sbuf` | `0x712e0` | `assert_free_dim_sbuf` |
| `check_matmul_high_level_shape` | `0xf2a30` | `err_param_shape_incompatible_with_matmul` |
| `check_matmul_f_dim_sizes` | `0xe7f90` | `err_size_of_dimension_exceed_arch_limit` |
| `check_transpose_shape` | `0x73e60` | `err_exceed_max_supported_dimension`, `err_size_of_dimension_exceed_arch_limit` |
| `check_store_shape` | `0xa5ac0` | `err_store_dst_shape_smaller_than_other_shape` |
| `check_tensor_reduce_supported_ops` | `0x5ec40` | `err_instruction_unsupported_op` |

The remaining validators (`check_matmul_mx_low_level_shape`, `check_param_against_dtype_hints`, `check_modified_type`, `check_parameter_types`, `check_and_update_list_param`, `check_and_update_param`, `check_direct_type_match`, `check_tensor_tile_promotion`, `check_modified_tensor_type`, `check_parameter_shapes`, `check_parameter_shapes_no_broadcast`, `check_parameter_shapes_low_level`, `check_matmul_low_level_shape`, `check_local_gather_shape`, `check_stream_shuffle_shape`, `check_quantize_mx_shape`) perform leaf checks or dispatch via a Cython-cached slot; several (`check_quantize_mx_shape`, `check_matmul_mx_low_level_shape`, `check_local_gather_shape`) raise *inline* messages with no fronting `err_*` symbol.

### 5c. `assert_*` → `err_*` (the named-err subset)

The 24 `assert_*` funnel through one primitive `nki_assert(exception, condition, msg)` (`0xbb9a0`, `sema.py:1233`). Most build an inline message; six dispatch to a named `err_*`:

| `assert_*` | Raises |
|---|---|
| `assert_num_partition` / `assert_par_dim_sbuf` / `assert_par_dim_psum` | `err_num_partition_exceed_arch_limit` |
| `assert_tile_has_expected_shape` | `err_tile_shape_mismatch` |
| `assert_is_tile` | `err_failed_to_infer_tile_from_local_tensor` \| `err_unexpected_type_of_operand` |
| `assert_constant_value` | `err_expected_constant_value` |
| `assert_tensor_in_valid_addr_spaces` | `err_unsupported_memory` |

---

## 6. Evidence summary

The catalog was extracted programmatically from the runtime dump of the live dict, not hand-typed.

- **Total count.** The dump has `2556` `CAUSE:` lines and `353` `---- (N codes) ----` headers; the per-header counts sum to `2556` with zero mismatch, and the table above renders `2556` `| \`Code\` |` rows — matching `len(NEURON_ASSERT_ERROR_MESSAGES) == 2556` at runtime.
- **Histogram.** First-letter category histogram, independently re-derived: `I=252 E=24 X=15 L=12 B=9 S=8 M=5 N=4 D=4 C=4 T=3 P=3 A=3 O=2 J=2 V=1 R=1 G=1`.
- **Resolution split.** `2095` rows carry only `{RESOLUTION_CONTACT_SUPPORT}` (rendered `*(contact)*`); `461` carry a bespoke resolution.
- **Five representative rows** spot-checked against the string table and the dump:

| Key | Cause (verbatim) | Resolution | Tag |
|---|---|---|---|
| `(ADA, 1)` | `ADA computation should be called only once per batch. The history should be empty, but not.` | `*(contact)*` | CERTAIN |
| `(BIR, 18)` | `ctx is nulltpr` *(typo preserved from binary)* | `*(contact)*` | CERTAIN |
| `(EARG, 1)` | `Illegal argument(s) - Logical Neuron Core size {lnc} is not supported on trn1. …` | `Update the Logical Neuron Core size to 1 or check that the target architecture is correct.` | CERTAIN |
| `(NKI, 16)` | `Kernel validation exception: {error_text}` | `Please check the validation message and adjust kernel inputs accordingly` | CERTAIN |
| `(IIOT, 901)` | `InsertOfflaodedTransposes assertion error: {baseMessage}` *(generated 901 family; spelling `Offlaoded` preserved)* | `*(contact)*` | CERTAIN |

All five match the dump byte-for-byte, including preserved typos (`nulltpr`, `Offlaoded`, `diretory`, `parellelism`) — which is itself a sign the table was dumped rather than paraphrased. The `(NKI, 16)` body pins it to `nki/.../kernel_assert.py:26`.

## Limits of this reading

This page gives every `ErrorMessages` code's *text* but not its native C++ *call site*. Which `libwalrus` / `libBIR` / codegen instruction-check invokes `neuron_assert("…", CAT, idx, …)` for a given `(CAT, idx)` lives in the statically-linked C++, not in `ErrorMessages.so`, and is **[UNRESOLVED]** here. The only Python call sites pinned to source are `DAE001` / `DAE002` (`cli/Daemon.py`) and `NKI016` (`kernel_assert.py`). The 224 frontend `NCC_*` code bodies ([§3](#3-the-224-native-ncc_-codes-frontend-binaries)) are a separate catalog in the HLO binaries' rodata, out of scope for the dict dump and not reproduced verbatim here. `{kwarg}` *values* are runtime-supplied; only the static templates are recovered.

---

## Cross-References

- [Diagnostic & Error-Code Catalog — Four Systems](../frontend/diagnostic-error-catalog.md) — §3.20, the narrative behind this dump: how the four diagnostic systems are wired, the `CommandDriver.handleError` `[F1xx]` path, the two parallel `NeuronAssertion` namespaces, and the MLIR/LLVM in-flight diagnostics.
- [NKI Diagnostic Catalog — the err_/check_/assert_ Funnel](../nki/diagnostic-catalog.md) — §6.4.5, the per-diagnostic reference for the `err_*` leaves in [§5](#5-nki-err_-funnel-sema): full `__doc__` strings, message fragments with rodata addresses, triggers, and raising-op index.
- [NKI sema Legality-Assert Engine](../nki/sema-legality.md) — the `nki_assert` core and the `assert_*` engine layer that drives the `err_*` leaves.
