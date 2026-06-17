# String Domain

> **Binary:** `extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so` → `libnrt.so.1` → `libnrt.so.2.31.24.0` · **Version** `2.31.24.0-0b044f4ce` (`.nrt_brazil_version` @`0xad41f0` = `"2.31.24.0"`; `.git_hash` @`0xad41c0` = `"0b044f4ce917b633a70eb3d0bc460f34ac3da620"`) · **BuildID[sha1]** `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e` · ELF64 LSB **DYN** x86-64, NOT stripped, DWARF v4.
>
> **Part II — Binary Anatomy & Forensics** / REFERENCE catalogue (the 9th and final forensics page — closes the Part) · **Evidence grade:** every literal below is read from the shipped binary — `readelf -SW`/`-p .rodata` for the section geometry and NUL-split pool, `strings -t x` for offset-anchored examples, and a per-callsite `objdump -d -j .text` operand-resolver (the `lea`/`mov`-immediate sequence feeding each `__assert_fail`/`al_abort_program`) for the assert provenance. Counts re-derived against this exact build-id; deltas from the source-string seed are flagged in-place as CORRECTIONs. · [back to forensics index](overview.md)

## Abstract

This page is the **evidentiary backbone of the entire reverse-engineering effort**: the catalogue of the string literals embedded in the shipped `libnrt.so` binary, and the method by which those literals — specifically the `__FILE__` source-path and `__assert_fail` condition strings the compiler baked into the image — recover translation-unit attribution and exact source `file:line` for code that ships with no usable line tables. Every other page in this book that writes "`tensor.c:341`" or "this is first-party `enc/` logic" rests on the mechanics established here. The strings discussed are **bytes inside the binary**, placed there by `__attribute__((section))`, `assert()`/`al_assert()` macro expansion, `printf`-family format constants, `getenv` name fragments, and `protoc`-generated descriptor pools — not text quoted from any external source.

The literal pool lives in one place: **`.rodata` @`0x7cf000`, size `0x3051b3` = 3,166,643 B (~3.02 MiB)** (`readelf -SW`), spanning VMA `0x7cf000`…`0xad41b3`, immediately followed by the four non-standard provenance sections (`.git_hash`, `.nrt_brazil_version`, `protodesc_cold`). Because the runtime is identity-mapped (every `PT_LOAD` segment has `p_offset == p_vaddr`; see [ELF Anatomy](elf-anatomy.md)), **the VMA of any literal equals its file offset** — a `lea`-target address read out of a disassembled instruction is the byte offset at which the NUL-terminated string can be read directly. That single fact is what makes the assert-string recovery technique mechanical rather than heuristic. Eight string classes are isolated below; the two with the highest forensic value are the **source-path/`__FILE__`** class (which reconstructs the `/opt/workspace/KaenaRuntime/` source tree and seeds the source map) and the **`__assert_fail` condition** class (which pins 3,582 unique source `file:line` sites and, by their conditions, reconstructs enums, state machines, and sizing constants the binary never names elsewhere).

The page is organized as a reference catalogue: the at-a-glance anchors, the reimplementation contract, the **category table** (eight string classes, each with a re-derived count, a verbatim example read at its offset, and its use in RE), then the centrepiece — the **assert-string → `file:line` recovery technique** explained with two fully-worked examples (one vendored, one first-party) showing how a single `__assert_fail(cond, file, line, func)` callsite yields a verbatim triple from the instruction stream. QUIRK/GOTCHA callouts close out the leaks and traps: the source-tree layout the `__FILE__` paths expose, the interned env-var mega-literal that inflates naïve greps, and the macro-doc strings that live in DWARF rather than `.rodata`.

For reimplementation, the contract this page establishes is:

- **The literal-pool geometry** — where strings live (`.rodata` @`0x7cf000`, 3.02 MiB), and the identity-map rule (VMA == file offset) that lets any `lea` target be dereferenced as a file offset to read the string.
- **The eight string classes and their counts** — what fraction of the pool is source-path, assert-condition, log/format, env-var, error-message, descriptor, version, and Rust-panic; which classes are first-party (reimplementation-relevant) and which are vendored/mechanical.
- **The `file:line` recovery technique** — how the `lea cond / lea file / mov $line,%edx / lea func` operand sequence preceding each `__assert_fail` reconstructs a verbatim `(condition, file, line, function)` triple, even for `-g`-less vendored TUs — the method the source map and the enum/invariant catalogue are built on.

| | |
|---|---|
| **Literal-pool home** | `.rodata` @`0x7cf000` / off `0x7cf000` / **`0x3051b3` = 3,166,643 B (~3.02 MiB)** (`readelf -SW`) |
| **Identity-map rule** | VMA == file offset for all `PT_LOAD` (so a `lea`-target VMA reads the string directly) — [ELF Anatomy](elf-anatomy.md) |
| **glibc assert template** | `0x83bad0` `"%s:%d: %s: Assertion '%s' failed."` — `__assert_fail@plt` @`0x3c880` |
| **al_hal assert template** | `0x80db18` `"%s:%d:%s: Assertion failed! (%s)\n"` — `al_abort_program` @`0x265c40` |
| **glibc assert callsites** | **5,928** (re-derived: `call __assert_fail@plt` = 5,928 ✓) |
| **al_hal abort callsites** | **283** (re-derived: `call al_abort_program` = 283 ✓; 278 `al_assert` + 5 bespoke FATAL) |
| **Unique `(file,line)` assert sites** | **3,582**; unique condition expressions **1,833** (operand-resolver over all 5,928) |
| **First-party `NEURON_RT_*` env vars** | **139** distinct (`strings \| rg -o … \| sort -u` = 139 ✓) |
| **Source roots** | first-party `/opt/workspace/KaenaRuntime/`; vendored `/opt/brazil-pkg-cache/packages/<Pkg>-<ver>/` |

> **NOTE — these are binary-embedded strings, not an external source quote.** Every literal on this page was read out of `libnrt.so` at a named offset. A `__FILE__` path like `/opt/workspace/KaenaRuntime/tdrv/tensor.c` is a *byte sequence the compiler embedded* when it expanded an `assert()`; it is recovered from the shipped image exactly as any other `.rodata` constant. Nothing here is derived from, or compared against, any vendor source tree — the source-path strings are themselves the only artifact, and they are forensic evidence, not citation.

---

## 1. The literal pool and the identity-map rule

The string domain has one physical home and one rule that makes it tractable.

**The home** is `.rodata` (`readelf -SW`: `[15] .rodata PROGBITS 7cf000 7cf000 3051b3 … A`). The 3.02 MiB span `0x7cf000`…`0xad41b3` holds the entire first-party + vendored read-only literal corpus: format strings, condition strings, `__FILE__` paths, `__PRETTY_FUNCTION__` names, env-var names, error messages, and the hot half of the protobuf descriptor tables. The cold half of the descriptor pool spills into the adjacent non-standard `protodesc_cold` section (@`0xad4200`, `0x64a8` B); the two provenance micro-sections `.git_hash` (41 B) and `.nrt_brazil_version` (10 B) sit between them (see [ELF Anatomy §1](elf-anatomy.md)). The Rust panic/format corpus is a contiguous packed run inside `.rodata` (the `strings` dump shows it interned end-to-end with no inter-string padding; §3.4).

**The rule** is the identity map. `readelf -lW` shows all four `LOAD` segments with `Offset == VirtAddr`, so for `.text` and `.rodata` alike, **a virtual address is a file offset**. When a disassembled instruction reads `lea 0x…(%rip),%rsi  # 819428`, the resolved target `0x819428` is simultaneously (a) the VMA the CPU loads into `%rsi` and (b) the byte offset in the file where the NUL-terminated string begins. Reading `f.seek(0x819428); read().split(b'\0')[0]` yields the literal verbatim. This is the entire foundation of the assert-recovery technique (§4): no relocation, no debug-info lookup, no symbolization is needed to turn an operand address into a string.

> **CORRECTION (STRING-DOMAIN vs F-STRINGS seed) — `.data` is identity-mapped too; there is no `0x400000` delta.** The source-string seed's section table noted "`.data` delta = `0x400000`". That figure is **wrong for `libnrt.so`** — it is carried over from a different runtime image (the libtpu / Kaena-profiler binary). `readelf -lW libnrt.so.2.31.24.0` shows the RW `LOAD` at `Offset == VirtAddr` and `.data` at `0xc07e00 == 0xc07e00`; the delta is **zero** for every PROGBITS section. No literal on this page requires a delta correction. See [ELF Anatomy §1](elf-anatomy.md).

---

## 2. The category table

The pool partitions into eight string classes. The table gives, per class, a re-derived magnitude, a **verbatim example read at its offset**, the subsystem that owns it, and its use in the RE effort. Counts are HIGH where they are a NUL-split distinct-list or a callsite count (`__assert_fail`/env-var) and MED where a substring grep over the merged corpus over-counts RTTI fragments and sub-strings (flagged per row). "Count basis" follows the table.

| Class | Magnitude | Example (verbatim, at offset) | Owner | Use in RE | Conf |
|---|---|---|---|---|---|
| **(a) source-path / `__FILE__`** | first-party: 203 CUs (DWARF) / **204** distinct `.c/.cc/.cpp` path strings; vendored: **223** TU path strings | `0x819428` `/opt/workspace/KaenaRuntime/tdrv/tensor.c` | every C/C++ TU | TU attribution; seeds the [source map](overview.md#2-first-party-subsystem-tree) | HIGH |
| **(b) `__assert_fail` condition** | **5,928** glibc callsites → **3,582** unique `(file,line)`, **1,833** unique conditions | `0x846f44` `tensor->sto` | every asserting TU | `file:line` recovery (§4); enum/state-machine/bounds reconstruction | HIGH |
| **(a′) assert templates** | 2 runtime templates + 2 macro-doc strings | `0x83bad0` `%s:%d: %s: Assertion '%s' failed.` | glibc / al_hal | identify the two assert mechanisms (§4) | HIGH |
| **(c) nlog / log-format** | ~**4,158** `printf`-conversion-bearing literals (MED — substring) | `0x7cf138` `Failed to create epoll fd, err: %d` | `nlog`/`tdrv`/`enc`/`nrt` | diagnostic-path map; error-code seeds | MED |
| **(d) env-var name** | **139** distinct `NEURON_RT_*` (HIGH) + ~1,550 non-RT `NEURON_*` constant tokens (not env vars) | `NEURON_RT_VISIBLE_CORES`, `NEURON_RT_NUM_CORES` | `nrt/nrt_config.cpp` | env-knob catalogue → [Env Vars](../runtime/env-vars.md) | HIGH |
| **(e) error / FATAL message** | F2/F3 FATAL set; 557 of the (c) literals carry a failure keyword | `0x8021b0` `FATAL Runtime Bug: ring offset must be %u bytes aligned but got 0x%lx` | `tdrv`/`enc`/`nrt` | abort-path map; runtime-error taxonomy | HIGH |
| **(f) embedded proto / descriptor** | `9,637` `ntff` token hits (MED — substring); 6 `.proto` descriptor file-names | `ntff.proto`, `.ntff.block_type`, `ntff.cpu_node_info.name` | `nrt_sys_trace*`, vendored protobuf | NTFF wire-format → [NTFF Format](../trace/ntff-format.md) | HIGH/MED |
| **(g) version / build banner** | ~5 banner formats + `2.31.24.0` / `0b044f4ce` provenance | `0x83dd9d` `NRT version: %s (%s)`; `0x83e038` `Neuron Runtime %s built on %s` | `nrt_init`/`nrt_infodump` | version handshake; build pin | HIGH |
| **(h) Rust panic / format** | one packed `.rodata` run; full `std`/`core`/`alloc` corpus + `neuron_rustime` | `called \`Option::unwrap()\` on a \`None\` value`; `assertion failed: num_cores <= MAX_VIRTUAL_TPB` | Rust object (`sys_trace`/`logging`/`string_db`) | Rust sub-runtime map; panic-path | HIGH |

**Count basis.** (a) `strings | rg -o '/opt/workspace/KaenaRuntime/…\.(c|cc|cpp)' | sort -u` = 204 distinct source-TU paths (222 including headers); the DWARF-authoritative first-party CU count is **203** (`readelf --debug-dump=info`, see [Overview §1.1](overview.md#11-by-dwarf-compile-unit-the-authoritative-function-partition)). (b) `rg -c 'call.*<__assert_fail@plt>'` over the `.text` disassembly = **5,928**; the unique-pair / unique-condition figures from the §4 operand-resolver. (c) `rg -c '%[-+ #0-9.l]*[diouxXeEfFgGaAcspn]'` over `strings` = 4,158 (MED). (d) `rg -o '\bNEURON_RT_[A-Z0-9_]+' | sort -u` = **139**. (f) `rg -c 'ntff'` = 9,637 (MED — includes RTTI/field-name fragments). (g) §1 provenance sections + banner literals.

> **CORRECTION (STRING-DOMAIN, counts re-grounded) —** two seed figures move under re-derivation against this build-id:
> - **fmt-bearing literals: ~4,158, not 4,262.** The seed counted `%[flags][conv]` with a looser pattern; a stricter `printf`-conversion regex over the same `strings` corpus returns **4,158**. Both are MED (substring counts over a merged pool over-count interned fragments); treat "~4,200 format-bearing literals" as the honest magnitude.
> - **first-party source-TU paths: 204 distinct `.c/.cc/.cpp` (222 incl. headers), not a flat 221.** The seed's "221 distinct first-party TUs" sits between the source-only (204) and with-headers (222) string counts; the **authoritative** function-partition figure is the DWARF **203 CUs** ([Overview §1.1](overview.md)). Cite 203 CUs for "how many TUs of first-party code", and 204/222 only when specifically counting `.rodata` path *strings*.
> Every other seed count (5,928 / 283 / 139 / 3,582 / 1,833 / 223 vendored TU paths / `ntff` 9,637 / `KaenaRuntime` 2,122 / `/opt/workspace` 2,224 / `brazil-pkg-cache` 1,030) reproduced **exactly**.

### 2.1 Which classes are reimplementation-relevant

Classes **(a)**, **(b)**, **(d)**, **(e)**, **(g)** are first-party forensic gold: they attribute code, reconstruct invariants, enumerate knobs, and pin the version. Class **(f)** is split — the `.proto` *schema* is AWS-authored (reimplementation-relevant), but the generated descriptor *bytes* are `protoc` output (mechanical; regenerate). Class **(h)** is the Rust sub-runtime's panic surface — useful to map `neuron_rustime` but mostly stock `std`/`core` corpus a port would link, not rewrite. The vendored share of class (a) (223 TU paths under `/opt/brazil-pkg-cache/`) and the Abseil/Protobuf share of class (b) (§4.4) are **boundary** — they identify which library at which pinned version to link, not code to reverse (see [Vendored SBOM](vendored-sbom.md)).

---

## 3. The eight classes in detail

### 3.1 Source-path / `__FILE__` (class a) — the source map's seed

The build embeds `__FILE__` as a `.rodata` literal at every `assert()` and `nlog` site, so the source tree is reconstructable from the binary alone. Two roots partition the corpus cleanly:

```text
first-party : /opt/workspace/KaenaRuntime/<subsys>/<file>      (204 distinct .c/.cc/.cpp paths)
vendored    : /opt/brazil-pkg-cache/packages/<Pkg>/<Pkg>-<ver>/…/src/<file>   (223 distinct)
```

The vendored root is **version-pinned in the path itself** — `KaenaHal/KaenaHal-2.31.0.0`, `Protobuf/Protobuf-26.x.13.0`, `Abseil/Abseil-20230802.x.5134.0`, `KaenaDriverLib/KaenaDriverLib-2.27.4.0`, the three `*ArchHeaders` (`CaymanArchHeaders-1.0.826.0`, `MarianaArchHeaders-1.0.1133.0`, `SundaArchHeaders-1.0.1851.0`), `GoogleProtoBuf-5.x.4041.0` — so each path string is also an SBOM data point ([Vendored SBOM](vendored-sbom.md) owns the reconciliation). The first-party tree's subsystem split (`tdrv`/`enc`/`nrt`/`kmgr`/`kelf`/`nds`/…) is owned by [Overview §2](overview.md#2-first-party-subsystem-tree); this page only establishes that the path *strings* exist and are the evidence.

### 3.2 Log-format and error messages (classes c/e)

The `printf`-family format constants are the runtime's diagnostic vocabulary. A representative span at the head of `.rodata` (offsets verbatim):

```text
0x7cf138  "Failed to create epoll fd, err: %d"
0x8021b0  "FATAL Runtime Bug: ring offset must be %u bytes aligned but got 0x%lx"
0x80bab0  "Internal runtime error. Please open a bug report the the Neuron Runtime Team. Sync event %u must be less than %u"
```

The last is quoted with its original typo (`"the the"`, doubled) — a small but useful fingerprint that this is the *shipped* literal, not a paraphrase. The `FATAL Runtime Bug` / `Internal runtime error` family marks the runtime's hard-abort paths and seeds the error-code taxonomy; 557 of the format literals carry a `failed`/`ERROR`/`cannot` keyword.

### 3.3 Env-var names (class d)

The 139 distinct `NEURON_RT_*` names are the runtime's knob surface (`NEURON_RT_VISIBLE_CORES`, `NEURON_RT_NUM_CORES`, `NEURON_RT_LOG_LEVEL`, the `NEURON_RT_DBG_*` debug family, …). These are read by `nrt/nrt_config.cpp` and resolved to parser + default + `gconf` field by [Env Vars](../runtime/env-vars.md), which owns the catalogue. The 139 figure is de-duplicated after excluding the interned mega-literal (§5) that fuses ~40 names into one overlapping run.

### 3.4 Descriptor, version, and Rust classes (f/g/h)

Class (f) is the `ntff` (Neuron Trace File Format) protobuf surface — message/field names (`.ntff.block_type`, `ntff.collectives_op_info`, `ntff.cpu_node_info.name`) and the embedded `.proto` descriptor file-names (`ntff.proto`, `neuron_trace.proto`, plus the stock `google/protobuf/{descriptor,cpp_features}.proto`); the schema is owned by [NTFF Format](../trace/ntff-format.md). Class (g) is the version-banner trio (`NRT version: %s (%s)` @`0x83dd9d`, `libnrt version %s` @`0x83df68`, `Neuron Runtime %s built on %s` @`0x83e038`) plus the `.git_hash`/`.nrt_brazil_version` provenance sections. Class (h) is the Rust panic corpus — one contiguous packed `.rodata` run carrying the full `std`/`core`/`alloc` set (`called \`Option::unwrap()\` on a \`None\` value`, `index out of bounds: the len is `, `attempt to add with overflow`, `internal error: entered unreachable code`) intermixed with the first-party `neuron_rustime` panics (`assertion failed: num_cores <= MAX_VIRTUAL_TPB`, `nrt_gconf() returned null`) and the `std_detect` CPU-feature tokens (`avx512cd`, `amx-tile`, `amx-int8`, …).

---

## 4. The assert-string → `file:line` recovery technique

This is the method the rest of the book rests on. The problem it solves: large parts of `libnrt.so` ship with **no usable DWARF line table** for some TUs (the vendored protobuf/Abseil subtrees are `-g`-less; see [Overview §1.1 correction](overview.md#11-by-dwarf-compile-unit-the-authoritative-function-partition)). Yet `assert()` macro expansion forces the compiler to embed `__FILE__`, `__LINE__`, and the stringized `#COND` as constant operands at every callsite — so a function's source identity and a precise line number are recoverable from the **instruction stream** even when the line table is absent.

### 4.1 The two mechanisms

Two assert paths coexist, distinguished by which template and which terminator they use:

| Mechanism | Template (verbatim @offset) | Operand convention | Terminator | Callsites |
|---|---|---|---|---|
| **M1 glibc** `assert()` | `0x83bad0` `"%s:%d: %s: Assertion '%s' failed."` | `rdi`=cond, `rsi`=file, `edx`=line, `rcx`=`__PRETTY_FUNCTION__` | `__assert_fail@plt` @`0x3c880` | **5,928** |
| **M2 al_hal** `al_assert()` | `0x80db18` `"%s:%d:%s: Assertion failed! (%s)\n"` | file/func/line passed to `al_hal_log` (@`0x265b80`), then abort | `al_abort_program` @`0x265c40` | **283** (278 assert + 5 FATAL) |

Both counts are re-derived exactly: `rg -c 'call.*<__assert_fail@plt>'` = **5,928**, `rg -c 'call.*<al_abort_program>'` = **283** (3 further references to `al_abort_program` are its own label and two tail-`jmp`s, not calls). The al_hal logger path is corroborated by `call al_hal_log` = 1,052.

> **NOTE — the macro-doc strings live in DWARF, not `.rodata`.** The al_hal *macro definition* itself appears verbatim — `al_assert(COND) do { if (!(COND)) { al_err("%s:%d:%s: Assertion failed! (%s)\n", __FILE__, __LINE__, __func__, #COND); al_abort_program(-1); } } while (0)` — but at file offset **`0x36c4ee3`**, which is **past the `.rodata` end** (`0xad41b3`): it is a captured doc-comment in `.debug_str`, not a runtime literal. The *runtime* template is the short `0x80db18` form. A reimplementer harvesting templates from `.rodata` will not find the macro-doc string there; it is DWARF, and only present because the binary is not stripped.

### 4.2 The recovery, step by step

For an M1 site, the four operands are loaded by a fixed `lea`/`mov`-immediate sequence in the basic block that falls through to the `__assert_fail` call. The resolver: (1) find each `call …<__assert_fail@plt>`; (2) scan backward for the `lea …,%rsi` (file), `lea …,%rdi` (condition), `mov $0x…,%edx` (line), `lea …,%rcx` (function); (3) for each `lea`, the `# <hex>` comment is the resolved RIP-relative target — which, by the identity-map rule (§1), is a file offset; (4) read the NUL-terminated string there. The line is the `%edx` immediate, decoded as decimal. No relocation or symbolization is involved.

```c
// recover_triple(callsite) — models the per-site operand resolver
function recover_triple(call_addr):                 // call …<__assert_fail@plt>
    for insn in preceding_block(call_addr):         // scan back ≤ ~16 insns
        if insn == "lea …,%rsi # FILE_VMA": file = FILE_VMA
        if insn == "lea …,%rdi # COND_VMA": cond = COND_VMA
        if insn == "lea …,%rcx # FUNC_VMA": func = FUNC_VMA
        if insn == "mov $LINE,%edx":         line = LINE       // immediate, decimal
    // identity map: VMA == file offset (ELF Anatomy §1)
    return (read_cstr(cond), read_cstr(file), line, read_cstr(func))
```

### 4.3 Worked example A — a vendored TU with no line table

The first `__assert_fail` callsite in `aws_hal_notific_nq_write_hw_head_value_mariana.part.0` (a KaenaHal vendored TU, `-g`-less) — the four operand loads and the call, read from the `.text` disassembly:

```asm
0x6235a: lea 0x988d9f(%rip),%rcx   # 9eb100   ; __PRETTY_FUNCTION__
0x62361: lea 0x7c3c78(%rip),%rsi   # 825fe0   ; __FILE__
0x62368: lea 0x7c3d01(%rip),%rdi   # 826070   ; #COND
0x62373: mov $0x66,%edx                        ; __LINE__ = 102
0x62378: call 3c880 <__assert_fail@plt>
```

Dereferencing each operand target as a file offset:

```text
%rdi 0x826070 → "new_head < nq->init_params.size"
%rsi 0x825fe0 → "/opt/brazil-pkg-cache/packages/KaenaHal/KaenaHal-2.31.0.0/…/mariana/notific/aws_hal_notific_nq_mariana.c"
%edx 0x66     → 102
%rcx 0x9eb100 → "aws_hal_notific_nq_write_hw_head_value_mariana"
```

The recovered triple — **`aws_hal_notific_nq_mariana.c:102` : `assert(new_head < nq->init_params.size)`** — comes entirely from the instruction stream. This TU has no DWARF line program, yet its source file, exact line, and the invariant it enforces are all recovered verbatim. The condition is itself reimplementation evidence: it names the field `nq->init_params.size` and bounds `new_head` against it.

### 4.4 Worked example B — a first-party TU

The same technique on a first-party site, in `tensor_block_while_exec` (`tdrv/tensor.c`):

```asm
0x30e185: lea 0x6d16b4(%rip),%rcx  # 9df840   ; __PRETTY_FUNCTION__
0x30e18c: mov $0x155,%edx                      ; __LINE__ = 341
0x30e191: lea 0x50b290(%rip),%rsi  # 819428   ; __FILE__
0x30e198: lea 0x538da5(%rip),%rdi  # 846f44   ; #COND
0x30e19f: call 3c880 <__assert_fail@plt>
```

```text
%rdi 0x846f44 → "tensor->sto"
%rsi 0x819428 → "/opt/workspace/KaenaRuntime/tdrv/tensor.c"
%edx 0x155    → 341
%rcx 0x9df840 → "tensor_block_while_exec"
```

Recovered: **`tdrv/tensor.c:341` : `assert(tensor->sto)` in `tensor_block_while_exec`** — a non-null guard on the tensor's storage pointer. Run over all 5,928 M1 callsites, the resolver yields **3,582 unique `(file,line)` pairs** and **1,833 unique condition expressions**, with **zero unresolved operands** for the cleanly-recoverable sites. Bucketing each site's recovered `__FILE__` by root reproduces the provenance split independently:

| Provenance | M1 asserts | Notes |
|---|---:|---|
| first-party `KaenaRuntime` | **3,231** | `tdrv` 1,689 · `enc` 1,402 · `kmgr` 74 · `nrt` 30 · `kelf` 14 · `inc` 8 · `ndebug` 7 · `utils` 3 · `dve_config` 2 · `ucode` 2 |
| vendored Abseil | ~1,985 | boundary (link, do not reverse) |
| vendored KaenaHal (via glibc `assert`) | 426 | boundary |
| vendored Protobuf | 280 | boundary |

(The Abseil figure resolves to 1,985 with a 16-instruction backward window vs the seed's 1,989; the four-site gap is sites whose `%rsi` is materialized further back than the window, and is the only sub-percent divergence. First-party totals are byte-exact to the seed.)

> **QUIRK — the condition strings reconstruct what the binary never spells out.** Because each `#COND` is the *stringized expression*, the assert corpus is a free enum/state-machine/sizing dump. `tensor->sto->type != NRT_TENSOR_MEM_TYPE_INVALID` (and its siblings `…== DMA`, `…== FAKE`) reveal the `NRT_TENSOR_MEM_TYPE` enum members; `al_hal_tpb_get_arch_type() < AL_HAL_TPB_ARCH_TYPE_CAYMAN` proves the arch enum is *ordered* (`INVALID < … < CAYMAN < MARIANA < SUNDA`); `curr_subtype_id < ENC_MAX_MESH_SUBTYPES` (×36 sites) names a sizing constant. None of these names appear as standalone symbols — they exist only inside assert conditions. This is why the assert catalogue harvests them to rebuild enums and limits a reimplementer would otherwise have to guess.

---

## 5. Quirks and gotchas

> **QUIRK — the `__FILE__` paths leak the original build-tree layout.** Because `__FILE__` is the path *as the compiler saw it*, the literal pool exposes the full source-tree shape: the first-party root `/opt/workspace/KaenaRuntime/` with its 15 subsystem directories (`tdrv/`, `enc/`, `nrt/`, `kmgr/`, `kelf/`, `nds/`, `nlog/`, `utils/`, …), the per-silicon arch subtrees (`tdrv/cayman/`, `tdrv/mariana/`, `tdrv/sunda/`, `tdrv/encd/archs/`), and the version-pinned vendored layout `/opt/brazil-pkg-cache/packages/<Pkg>/<Pkg>-<ver>/`. The `DW_AT_comp_dir` (`…/build/private/develop/almalinux/RelWithDebInfo`) corroborates a RelWithDebInfo build on AlmaLinux. A reimplementation does not need this layout, but it is the single richest provenance signal in the binary and the seed of the entire source map.

> **GOTCHA — the interned env-var mega-literal inflates naïve greps.** At `0x8500d0` the linker has overlap-interned ~40 `NEURON_RT_*` names into one run where each name shares its tail with the next name's head: `…nrt_get_visible_isible_vnc_countNEURON_RT_VISIBLNEURON_RT_NUM_CONEURON_RT_EXEC_T…`. A substring grep for `NEURON_RT_` over this blob double-counts and produces truncated fragments (`NEURON_RT_VISIBL`, `NEURON_RT_NUM_CO`). The correct env-var count (**139**) comes from a NUL-split distinct list *after* excluding this interned run — `rg -o '\bNEURON_RT_[A-Z0-9_]+' | sort -u`. Any count derived by a raw substring grep over `.rodata` is wrong; ground env-var counts on the distinct-name list.

> **GOTCHA — `ntff` and `nrt_` substring hits are not literal counts.** The `9,637` `ntff` "hits" and the `93,561` `_ZN…` mangled-name hits in the seed are substring magnitudes over the merged corpus, inflated by RTTI fragments and field-name repetition; they are MED-confidence *indicators of mass*, not literal counts. Treat them as "this token dominates the pool", never as "there are N strings". The HIGH-confidence figures are the distinct lists (139 env vars, 204/223 TU paths, the 6 `.proto` names) and the callsite counts (5,928 / 283), all re-derived above.

> **NOTE — `file:line` is recovered, not pre-baked.** Unlike the embedded NeuronUcode (which packs `"path:line condition"` into one literal), KaenaRuntime/KaenaHal pass `__FILE__`, `__LINE__`, and `#COND` as *separate* operands (standard `assert()`/`al_assert` expansion). So no `"KaenaRuntime/…:NNN"` packed string exists — the line number lives in a `mov $0xNNN,%edx` immediate, and the triple is assembled from the instruction stream (§4). A reimplementer searching `.rodata` for packed `path:line` strings will find none; the technique is the disassembly walk, not a string grep.

---

## Related Components

| Component | Relationship to the string domain |
|---|---|
| [Overview & Heavy-Frame Census](overview.md) | consumes class (a) `__FILE__` paths as the first-party/vendored provenance split |
| [ELF Anatomy](elf-anatomy.md) | establishes the `.rodata` bounds and the identity-map rule this page's recovery depends on |
| [Static-Init Pipeline](static-init.md) | the `.init_array` ctors that register the class (f) protobuf descriptor pools |
| [Dispatch-Table Taxonomy](dispatch-tables.md) | the status/algorithm stringifier tables that index class (c/e) message literals |

## Cross-References

- [Overview and Heavy-Frame Census](overview.md) — the first-party/vendored function split built on the class (a) `__FILE__` path provenance this page catalogues
- [ELF Anatomy](elf-anatomy.md) — the `.rodata` @`0x7cf000` `0x3051b3` bounds and the proof that all `PT_LOAD` segments are identity-mapped (VMA == file offset), which makes the §4 operand-to-string recovery mechanical
- [Static-Init Pipeline](static-init.md) — the load-time constructors that register the NTFF/NEFF protobuf descriptor pools whose descriptor strings are class (f)
- [Environment Variable Catalog (NEURON_RT_*)](../runtime/env-vars.md) — the catalogue that resolves the 139 class (d) `NEURON_RT_*` names to parser, default, and `gconf` field
- [back to forensics index](overview.md)
