# String-Evidence and Confidence Policy

Two evidence rules govern every other page in this wiki: a three-tier confidence ladder (HIGH / MED / LOW) and a strict verbatim-string policy. Every wiki claim must trace back to corpus evidence, and every backticked string must be a byte-for-byte literal recoverable from `tileiras_strings.json` or its rodata anchor. The methodology page summarises these rules; this page is the full specification, including how contradictions resolve between strands and how errata propagate to dependent claims.

## Quick orientation for editors

Reach for this page when:

- adding a new wiki claim and choosing between HIGH, MED, and LOW;
- backticking a string and needing to confirm the literal exists in the corpus;
- resolving a contradiction between two reports (the nine-row table in [Contradiction Resolution](#contradiction-resolution) is authoritative);
- propagating a fix from `p4-AA*` errata to dependent pages.

Editors who want the short version should read the methodology page first; this page is the long specification.

## HIGH

A claim is HIGH when it carries **byte-level evidence at a known address** plus **independent corroboration in two or more reports**. Acceptable byte-level anchors are: a verbatim `.rodata` string extracted at a fixed offset and xref'd to one or more `sub_ADDR` consumers; a vtable or `__cxxabiv1` typeinfo match resolved against an MLIR/LLVM base class (`OpRewritePattern`, `OpConversionPattern`, `Pass`, `RegisteredOperationName::Impl`); or a structural fingerprint with no plausible alternative interpretation (the introsort depth bound `2*floor(log2(N))` at the sole call site of `sub_27F0830`, the BLAKE3 IV at `xmmword_503C080`, a modulo-schedule resource matrix). No HIGH claim ever rests on a single weak indicator; cross-corroboration is mandatory. HIGH is the default tag once a verbatim string anchor exists, and a re-decompile round promoted ten Tier-1 MED targets to HIGH after a second corroborating fingerprint landed.

## MED

A claim is MED when it carries **structural evidence without a verbatim string anchor**, or **single-report evidence with strong context**. Structural evidence includes: vtable shape (slot count, slot-stride, neighbour functions), field offsets recovered from caller arithmetic, header layouts inferred from `_BitScanReverse64`/`memcpy` width matches, and sibling-cloning from a HIGH-anchored template (one of the 43 byte-identical `_M_realloc_insert` clones in the arith-to-TileAS pattern set, for example). MED is the standing tag for callgraph-position identifications — a function whose role is fixed by its position relative to a HIGH neighbour but whose body has not yet been read line-by-line. The audit pass ranked all 1,619 MED occurrences across the corpus by NVIDIA-specificity, reimplementation-blocker severity, surface area, and upstream-delta risk; the top thirty seeded a follow-up task list.

## LOW

A claim is LOW when it rests on **inference from neighbouring code without a direct anchor**, when **evidence conflicts** between strands and the conflict has not yet been adjudicated, or when **corroboration is absent**. LOW is reserved for tiny helpers (typically less than 200 bytes) with no strings, no identified callers, no distinctive control flow, and no vtable-slot constraint. The 271 LOW occurrences cluster in the TileAS plan-cta helper region (`sub_7CB8B0`..`sub_7D1640`, 28 rows) and the convert-gpu-to-nvvm helper region (`sub_12F53D0`..`sub_12F7F30`, 15 rows). LOW must not appear in core prose; when a LOW tag is the only available evidence, the claim is rendered with explicit hedging or omitted.

## Verbatim String Requirements

Every backticked string in this wiki is byte-identical to an entry in `tileiras_strings.json` or to a substring of one (the printf prefix that gets concatenated with a runtime-substituted suffix, the trailing `Properties]` of a templated `llvm::getTypeName` instantiation, etc.). A corpus audit of 3,926 distinct quoted fragments against the 71,714-unique-value string table found 1,904 (48.5 %) exact matches, 646 (16.5 %) substring matches of a larger composite, and 1,376 (35.0 %) paraphrase or prose entries. Of 80 verbatim-claim-phrase occurrences, 79 verified and one was fabricated (`"Couldn't fork"` at `sub_45B2460`); of 1,270 dialect-op-name claims, 1,269 verified and one was fabricated (`"cuda_tile.print_tko"` at opcode 0x55 / `sub_5AD2C0`, where the rodata mnemonic is `cuda_tile.print`). Paraphrases are **not** backticked; representational differences (escape-form `\n` versus rodata `0x0A`, printf prefix versus its post-substitution form) are accepted but flagged.

## Contradiction Resolution

Phase ordering is **P5 > P4 > P3 > P2 > P1**, but **confidence outranks recency**: a later strand wins iff it brings stronger evidence. The contradiction sweep over the 9,182 (report, `sub_ADDR`) pairs produced exactly nine hard contradictions (CT-01..CT-09) and zero P2-versus-P2 conflicts; in every case the resolution cited at least one structural fingerprint that uniquely fixed the identity (introsort depth bound, binary-heap parent/child indices, twenty-offset destructor table match against the `sub_6D3460` option struct, MLIR `DIEmissionKind` declaration order). When two strands disagree without such a fingerprint, the case is logged as a soft contradiction and tracked separately (`CT-R1` response-pointer alignment, `CT-R2` stream-K slot count 14 → 28).

## Errata Propagation

P4-AA01 is the canonical errata log for the wiki: 2 fabricated-string fixes (FS-01, FS-02) and 9 hard-contradiction fixes (CT-01..CT-09), totalling 12 unique patch sites across 8 source files. Pages must update **transitively**: when an errata report supersedes a claim, every page that derives from the superseded claim is restamped with the corrected identification, the corrected confidence (typically MED to HIGH), and a reference to the superseding `p4-AA*` or `p5-*` report alongside the original P2/P3 source. The 9 hard contradictions are:

- **CT-01** `sub_27F0830` -- recursive SCEV visitor (P2-L11) -> `llvm::sort` introsort (P3-S06).
- **CT-02** `sub_27EE830` -- per-opcode visitor-callback dispatcher (P2-L11) -> `DenseMap<Value*, uint32_t>::operator[]` (P3-S06).
- **CT-03** `sub_27F05C0` -- recursion-budget check (P2-L11) -> ordinal-comparator `operator()` (P3-S06).
- **CT-04** `sub_27EEE80` -- `SCEVRewriter::rewriteOperand` (P2-L11) -> `std::__sift_down` (P3-S06).
- **CT-05** `sub_6CE840` -- pipeline-list finalizer (P2-I06, P3-W01) -> `TileIRPipelineOptions::~TileIRPipelineOptions()` (P3-W04).
- **CT-06** `sub_193BE20` `composed_layout` accessor (P3-N09) -> dispatches to `sub_193BDF0` not `sub_193B9C0` (P3-Q04).
- **CT-07** `sub_7D4C00` at vtable slot `+0x20` -- `initialize` (P2-D20) -> `match` predicate (P3-M01).
- **CT-08** `sub_2E13B60` -- `SynthesizeDebugInfoScopes` with arg `3 = LineTablesOnly` (P3-W01) -> upstream `DIScopeForLLVMFuncOp` with arg `3 = DebugDirectivesOnly` per MLIR `DIEnums.td` (P3-Y03).
- **CT-09** `byte_5B6D4C0` narrative summary in P3-V03 §6.1 -- corrected to align with P2-L14's already-correct identification as `-nvvm-lower-printf` storage.

Page authors must check the errata log before backticking any of these eight `sub_ADDR` or two strings (`"cuda_tile.print_tko"`, `"Couldn't fork"`); subsequent re-verification passes may add further supersessions, in which case the same propagation rule applies.

## Tier

T3 -- corpus methodology, applies to every wiki page.

## Confidence

HIGH for the policy itself. The HIGH/MED/LOW tier definitions are reproduced verbatim from the methodology page and are the operational standard used by every re-verification pass. The verbatim-string and contradiction-resolution rules carry HIGH self-confidence on their headline numbers. The nine-contradiction enumeration is reproduced from the errata log.
