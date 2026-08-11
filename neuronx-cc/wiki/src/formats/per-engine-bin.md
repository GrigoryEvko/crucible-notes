# Per-Engine `.bin` / `.json` Member Layout

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). The container writer lives in `libwalrus.so` (build-id `92b4d331`; `.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset; the `0x5e9020`–`0x62d650` `.plt` thunk band means a symbol such as `addToBom` appears twice — a thunk at `0x5fabd0` and the real body at `0x153fb80` — every address below is the real body). The TitleCase engine basenames are produced by `bir::EngineInfo2string` in `libBIR.so` (build-id `a9b1ea38`). Other wheels differ — treat every address as version-pinned. See [versions](../reference/versions.md).*

## Abstract

A NEFF is a [PAX tar of named members](neff-container.md), not an ELF; the bytes that hold the compiled program are the **per-engine instruction streams** — one flat `.bin` file per TPB compute engine, each a concatenation of [64-byte instruction bundles](../isa/instruction-bundle.md) in program order. There are exactly **five** such streams — `PE.bin`, `Pool.bin`, `Activation.bin`, `SP.bin`, `DVE.bin` — and each is paired with a same-stem `.json` **DMA-descriptor sidecar** (`PE.json`, …, `DVE.json`). DMA is *not* a sixth `.bin`: a DMA descriptor's bytes fold into the issuing compute engine's bundle stream (the `fwrite` routing is owned by [8.36](../walrus/bin-emission.md)), and the `.json` sidecar carries only the descriptor/queue-binding table the runtime programs the DMA rings from. This page is the **container view** of what 8.36 emits: where those streams land in the archive, how they are named, and which members are integrity-hashed.

The single most error-prone fact about these members is an **engine-name duality**. The on-disk basenames are **TitleCase** (`PE`, `Pool`, `Activation`, `SP`, `DVE`) — they come from `bir::EngineInfo2string` and are the names the shipped consumer `analyze_neff_artifacts.py` opens by. But the `def.json` `"definition"` index — the per-core table that tells the loader which basename belongs to which engine — keys engines by a **lowercase** token (`pool`, `act`, `pe`, `dma`, `dve`, `sp`) produced by a *different*, NEFF-local formatter (`sub_15248C0`, `neff_packager.cpp:49`), suffixed `_instr` / `_dbg` / `_asm_dbg`. A reimplementer who emits TitleCase keys into `def.json`, or lowercase basenames on disk, produces a NEFF the runtime cannot resolve. Both spellings, and the mapping between them, are pinned below.

The headline quirk concerns the **MD5 "IR signature"**. The NEFF writer hashes a *subset* of members and logs `IR signature (MD5): <hex>`. The membership predicate is byte-resolved in `addToBom` (`0x153fb80`): a member is hashed iff its **extension contains `.dbg`** or **equals `.npy`**, plus `info.json` (pre-seeded by the writer's constructor). The extension test means every `*.npy` constant-weight file is signed. The five engine `.bin` instruction streams — the actual program — are **not in the signature set** (extension `.bin` matches neither predicate). The signature certifies the debug-info and constant-weight members, not the code. This page proves that exclusion from the disassembly, names the real symbols, and gives the full per-member layout table.

For reimplementation, the contract is:

- **The member roster** — the five TitleCase `<Engine>.bin` instruction streams, their five same-stem `.json` DMA-descriptor sidecars, and the one-to-one pairing the producer asserts.
- **The engine-name duality** — TitleCase basenames (`bir::EngineInfo2string`, libBIR) vs. lowercase `def.json` tokens (`sub_15248C0`, `neff_packager.cpp:49`); the ordinal map; the `_instr`/`_dbg`/`_asm_dbg` suffixing.
- **The signature-subset predicate** — the `addToBom` extension test (`extension()==".npy"` / `extension()` contains `.dbg`) that selects MD5 membership, the `info.json` constructor seed, and the proof that `.bin` streams are excluded.

| | |
|---|---|
| **Instruction streams** | `PE.bin` `Pool.bin` `Activation.bin` `SP.bin` `DVE.bin` (64-byte bundles, program order) |
| **DMA-descriptor sidecars** | `PE.json` `Pool.json` `Activation.json` `SP.json` `DVE.json` (same stem) |
| **Basename formatter** | `bir::EngineInfo2string` (libBIR) — **TitleCase** |
| **`def.json` token formatter** | `sub_15248C0` (standalone engine→string fn; jump table @ `0x15248f1`) — **lowercase** `{1:pool,2:act,3:pe,4:dma,5:dve,6:sp}` |
| **Token → key suffix** | `<tok>_instr` / `<tok>_dbg` / `<tok>_asm_dbg` (`writeDefJson` `_M_append`) |
| **Pairing assert** | `bom.getEngInstrFile().count(eng)==1 && bom.getEngDMADescFile().count(eng)==1` (`DescGen::dumpToFile` `0x11dd610`) |
| **MD5-signed iff** | **extension contains `.dbg`** OR **extension equals `.npy`** (every `*.npy`), plus `info.json` |
| **`.bin` streams signed?** | **No** — not matched by either predicate; not in the signature set |
| **Consumer oracle** | `analyze_neff_artifacts.py` (md5 `125d8537…`) lines 24–28 / 74–75 |

---

## The engine instruction-stream members

### Purpose

Each TPB compute engine executes its own instruction queue, so the compiler emits one `.bin` file per engine — a flat array of [64-byte bundles](../isa/instruction-bundle.md) with no framing, no length prefix, and no inter-bundle padding (the file size is always a multiple of `0x40`). The runtime tar-extracts the member, maps it into that engine's instruction memory, and the engine sequences it. There are exactly five engines with a `.bin`: `PE`, `Pool`, `Activation`, `SP`, `DVE`. The producer never emits a `DMA.bin` — DMA descriptors are written *into* the issuing compute engine's stream by [`findBin`](../walrus/bin-emission.md) (`0x11f4b90`, keyed on `Inst+0x90`), so DMA is a *token* in `def.json` but not a stream of its own.

### Member roster

The five instruction streams and their paired sidecars, with the in-tar member ("wire") name each gets, the `def.json` token that indexes it, and whether its bytes feed the MD5 signature:

| On-disk basename | Content | Wire (tar) member | `def.json` token | MD5-signed |
|---|---|---|---|---|
| `PE.bin` | PE (matmul) instruction stream | `pe_instr` | `pe` | no |
| `Pool.bin` | Pool instruction stream | `pool_instr` | `pool` | no |
| `Activation.bin` | Activation instruction stream | `act_instr` | `act` | no |
| `SP.bin` | Sync/SP instruction stream | `sp_instr` | `sp` | no |
| `DVE.bin` | DVE instruction stream | `dve_instr` | `dve` | no |
| `PE.json` | PE DMA-descriptor / queue table | `pe` | `pe` | no |
| `Pool.json` | Pool DMA-descriptor table | `pool` | `pool` | no |
| `Activation.json` | Activation DMA-descriptor table | `act` | `act` | no |
| `SP.json` | SP DMA-descriptor table | `sp` | `sp` | no |
| `DVE.json` | DVE DMA-descriptor table | `dve` | `dve` | no |

The basename set is pinned by the shipped consumer `analyze_neff_artifacts.py` (md5 `125d8537cdc2d034ba1d171cd82d0138`, identical across cp310/311/312), which opens exactly these names:

```python
def analyzeDMA(prefix):                      # analyze_neff_artifacts.py lines 22-28
    poolFileName = "{}/Pool.json".format(prefix)       # the five .json sidecars
    spFileName   = "{}/SP.json".format(prefix)
    dveFileName  = "{}/DVE.json".format(prefix)
    peFileName   = "{}/PE.json".format(prefix)
    actFileName  = "{}/Activation.json".format(prefix)

def analyzeBins(self, prefix):               # DMAAnalysis, lines 74-75
    for engine in ['Pool', 'SP', 'DVE', 'PE', 'Activation']:   # the five .bin streams
        file_size = os.stat(f'{prefix}/{engine}.bin').st_size
```

The wire-member names (`pe_instr`, …) and the lowercase tokens are read from `writeDefJson` / `sub_15248C0` (next section). The `.bin`-content layout (64-byte bundle stream, DMA folded in) is owned by [the bundle page](../isa/instruction-bundle.md) and [8.36 bin-emission](../walrus/bin-emission.md); this page only fixes where they land in the container and how they are named.

> **GOTCHA — DMA has a `def.json` token but no `.bin`.** `sub_15248C0` maps ordinal `4` to `"dma"`, so `dma` is a legitimate engine token. There is nonetheless **no `DMA.bin`**: the DMA descriptor bytes fold into the issuing compute engine's `.bin` stream ([8.36](../walrus/bin-emission.md) — `findBin` rejects DMA/Unassigned/ALL as non-`.bin` engines). The per-engine `.json` sidecar — not a `.bin` — is the DMA-descriptor table (`{"dma":[{queue|instance_name, desc:[…]}]}`, 16 bytes/descriptor per `analyze_neff_artifacts.py:264`). A reimplementer who emits a sixth `DMA.bin` is wrong; the runtime expects DMA inside the compute streams.

### The pairing invariant

The producer asserts that every engine present has both an instruction file and a DMA-descriptor file. `DescGen::dumpToFile` (`0x11dd610` — the real body; `0x609460` is the per-symbol sidecar frame, the two-VA-frame artifact) carries the rodata assertion verbatim:

```text
bom.getEngInstrFile().count(eng) == 1 && bom.getEngDMADescFile().count(eng) == 1
```

`getEngInstrFile()` and `getEngDMADescFile()` are accessors on `bir::ModuleArtifactInfo` (the per-module object the backend source names `bom` — a *third* sense of "BOM", unrelated to the writer's file manifest or the JSON byte-order-mark). Each returns a `llvm::DenseMap<bir::EngineInfo, std::string, bir::EngineInfoDenseMapInfo>` mapping an engine to its file basename. So each `<Engine>.bin` has **exactly one** paired `<Engine>.json` sidecar; the assert fires (`NeuronAssertion`) if codegen ever registers one without the other.

---

## The engine-name duality

### Purpose

There are two name-producing functions for the same five engines, living at two layers, and they disagree by case. The on-disk filesystem uses one; the `def.json` index that *names those files* uses the other. Getting this wrong is the dominant integration error, because each spelling is internally consistent — a NEFF with all-TitleCase or all-lowercase names will look plausible and still fail to load.

### The two formatters

**On-disk basenames are TitleCase**, from `bir::EngineInfo2string` (libBIR). The whole tokens `Activation`, `Pool`, `DVE` appear as literals in `libBIR.so`, and the basenames match the shipped consumer's list (`['Pool','SP','DVE','PE','Activation']`). libBIR also documents the external→internal engine aliasing that resolves to these TitleCase names:

```text
// libBIR.so rodata — ExternalEngineType → internal EngineType
External: GPSIMD  Internal: Pool
External: Scalar  Internal: Activation
External: Vector  Internal: DVE
```

**`def.json` tokens are lowercase**, from the NEFF-local formatter `sub_15248C0` (`neff_packager.cpp:49`). The switch is byte-confirmed from the decompile — six `strcpy`-literal arms and a default that throws:

```c
// sub_15248C0(out, engineType) — neff_packager.cpp:49 — the NEFF-local lowercase formatter
char* EngineToken_NEFF(string* out, int engineType):     // 0x15248c0
    switch (engineType):
        case 1: return strcpy_literal(out, "pool");      // Pool   = ordinal 1
        case 2: return strcpy_literal(out, "act");       // Act    = ordinal 2
        case 3: return strcpy_literal(out, "pe");        // PE     = ordinal 3
        case 4: return strcpy_literal(out, "dma");       // DMA    = ordinal 4 (token only, no .bin)
        case 5: return strcpy_literal(out, "dve");       // DVE    = ordinal 5
        case 6: return strcpy_literal(out, "sp");        // SP     = ordinal 6
        default:                                          // 0=Unassigned / 7=ALL
            // logs "Assertion failure: false" then
            throw NeuronAssertion(1222, "false")          // neff_packager.cpp:49
```

The ordinals match the `bir::EngineType` enum (`Pool=1`, `Activation=2`, `PE=3`, `DMA=4`, `DVE=5`, `SP=6`), so the duality is purely casing + abbreviation (`Activation`→`act`), not a reordering.

### How the tokens become `def.json` keys

`writeDefJson` (`0x152a0e0`) builds the `"definition"` index by iterating the `getEngInstrFile` DenseMap at **stride 40** (each bucket is `{EngineType dword, lane/id dword, std::string value}`), formatting the token, suffixing it, and registering the file in the writer's BOM under that token:

```c
// NeffPackager::writeDefJson(writer, module)  — 0x152a0e0
def["definition"] = {}                                   // "definition" literal @ 0x152a0e0:214
for bucket in module.bom.getEngInstrFile():              // stride 40, line 267: 40*count
    EngineToken_NEFF(&tok, bucket.engineType)            // sub_15248C0, line 296  -> "pe"/"pool"/...
    key = tok._M_append("_instr")                        // line 299              -> "pe_instr"
    def["definition"][...]["_instr"] = bucket.basename   // "PE.bin"  (TitleCase value)
    addToBom(artifactDir/bucket.basename, override=key)  // line 340: member name = "pe_instr"

for bucket in module.bom.getEngDMADescFile():            // dma-descriptor loop, lines 392-549
    EngineToken_NEFF(&tok, bucket.engineType)            // line 418
    // reverse-look-up the engine into its instr group (memcmp tree walk, 441-463),
    // push the dma file under that engine's def.json array, then:
    addToBom(artifactDir/bucket.basename, override=tok)  // line 544: member name = "pe"/"pool"/...

for bucket in dbgFiles:    key = tok._M_append("_dbg");     addToBom(..., key)   // lines 682/685/727
for bucket in asmDbgFiles: key = tok._M_append("_asm_dbg"); addToBom(..., key)   // lines 794/797/839
```

So `def.json["definition"]["pe"]["_instr"] = "PE.bin"`, and the BOM separately maps the on-disk path `…/PE.bin` to the in-tar member name `pe_instr`. Only `_asm_dbg` survives as a whole rodata literal; `_instr` and `_dbg` are short `_M_append` fragments (consistent with the literal census). The `"_instr"` def.json key is gated by arch level (`*((int*)Module+43)<=49` at `0x152a0e0:284/398`).

> **QUIRK — the value is TitleCase, the key is lowercase, in the same JSON object.** Inside one `def.json`, the *key* `"pe"` indexes a *value* `"PE.bin"`. The key is `sub_15248C0`'s output; the value is `bir::EngineInfo2string`'s. A reimplementer must run *both* formatters — neither one alone produces a self-consistent `def.json`.

---

## The MD5 IR-signature subset

### Purpose

After the archive is written, the NEFF writer finalizes an MD5 over a *subset* of the members and logs `IR signature (MD5): <hex>` / `IR signature: <hex> for neff artifacts`. The signature is an integrity/identity tag for the IR-relevant artifacts — debug info and constant weights — **not** a hash of the whole archive and **not** a hash of the program. The selection happens one member at a time inside `addToBom`, so the subset is defined by a byte-resolvable string predicate, not by a member-type enum.

### The membership predicate

`addToBom` (`0x153fb80`) computes the entry's basename, then runs two independent string tests against it; a hit on either inserts the entry name into the IR-signature `std::set<std::string>` at `writer+0xD8` (= +216). Disassembled directly:

```text
0x153fc28:  call sub_175D1C0                   ; ext = file.extension()  (filename() then rfind('.'))
0x153fc65:  lea  rsi, aMlp0BiasNpy+0Bh        ; rsi -> ".npy"   (the ".npy" tail of "mlp0/bias.npy")
0x153fc6c:  mov  rdi, rbp                      ; rdi = ext  (the file's extension string)
0x153fc6f:  call std::string::compare(char const*)
            ; if ext.compare(".npy") == 0:      // TRUE for every *.npy file
0x153fc7d:  lea  rdi, [r12+0D8h]               ; rdi = writer+0xD8  (the IR-sig std::set)
0x153fc85:  call _Rb_tree::_M_insert_unique(entry)

0x153fc91:  lea  rsi, aDbg                     ; rsi -> ".dbg"
0x153fc98:  mov  rdi, rbp                      ; rdi = ext
0x153fc9b:  call std::string::find(char const*, ulong, ulong)
            ; if ext.find(".dbg") != npos:
0x153fcab:  lea  rdi, [r12+0D8h]               ; rdi = writer+0xD8
0x153fcb3:  call _Rb_tree::_M_insert_unique(entry)
```

with `r12 = this` (`mov r12, rdi` at the `0x153fb8b` prologue). The tested string `rbp` is the file's **extension**, not its whole basename: `sub_175D1C0` (`0x153fc28`) first calls the filename helper `sub_175B780` (rfind `'/'`) and then `rfind('.')` (`0x175d252: mov esi,0x2e`), returning the tail from the last dot — i.e. `boost::filesystem::path::extension()`. The `.npy` literal it compares against is itself the `.npy` tail of a weight-name string in `.rodata`. As annotated pseudocode:

```c
// NeffFileWriter::addToBom(file, entryNameOverride)  — 0x153fb80
void addToBom(this, path file, optional<string> entryNameOverride):
    entry = computeOutputPath(file)                       // 0x153fbc6 -> the member name inserted
    ext   = file.extension()                              // sub_175D1C0: filename() then rfind('.')

    // ---- IR-signature membership (the headline predicate) ----
    if ext.compare(".npy") == 0:                          // 0x153fc6f — extension EQUALS ".npy"
        this->irSigSet.insert(entry)                       // fires for every *.npy weight file
    if ext.find(".dbg") != npos:                          // 0x153fc9b — extension contains ".dbg"
        this->irSigSet.insert(entry)                       // [this+0xD8] = writer+216

    // ---- BOM upsert: path -> member name (no type/offset/hash field) ----
    node = rb_tree_find_or_insert(this->bom /*+0x90*/, file)   // root @ +0xA0
    node.value._M_assign(entry)                            // 0x153fd...; last-write-wins
```

The third member of the set — `info.json` — is not inserted here. The writer's **constructor** pre-seeds it:

```c
// NeffFileWriter::NeffFileWriter(modules, nc_count, uncompressed)  — 0x1543eb0
build_string(&tmp, "info.json")                            // "info.json" loaded @ 0x15440cf
this->irSigSet._M_get_insert_hint_unique_pos(/*set @*/ this+216, …, tmp)   // insert into IR-sig set
```

How solid is that seed? `0x1543eb0` is the `NeffFileWriter` constructor and it does load `"info.json"` (at `0x15440cf`, via `aKernelDebugInfoJson+0Dh`), so the string is demonstrably there. But `0x1543eb0` itself is the ctor's `__cxa_demangle`→`strlen`→`strstr` preamble, not the IR-signature insert, and the path from the string load to the insert was never single-stepped. Treat the pre-seed as corroborated rather than proven.

So the byte-proven signature set is exactly:

```text
IR-signature set  =  { info.json }  ∪  { members whose extension CONTAINS ".dbg" }
                                     ∪  { members whose extension EQUALS  ".npy" (every *.npy) }
```

`writeArchiveFile` (`0x153e030`) then, for each tar member in BOM key order, `_Rb_tree`-searches this set by the member name; on a hit it feeds the member's bytes (8 KiB `fread` chunks) into the MD5 and logs `Adding <n> bytes of <file> to the IR Signature.` — every literal is present in `libwalrus.so`.

> **QUIRK — the program is not signed; the debug info and weights are.** The five `<Engine>.bin` instruction streams are the compiled program, yet **none** of them enter the signature set: `PE.bin`'s extension is `.bin`, which is neither `.npy` nor contains `.dbg`, and only `info.json` is constructor-seeded. The MD5 "IR signature" therefore certifies the **debug-info** (`*.dbg`) and **constant-weight** (`*.npy`) members plus the `info.json` header — *not* the instruction bytes. A reimplementer who assumes the signature covers the code (a natural reading of "IR signature") is wrong, and a NEFF-diff that relies on the signature to detect code changes will silently miss them.

*Anchors: the predicate is read from the `addToBom` disassembly — `.npy` compare @ `0x153fc6f`, `.dbg` find @ `0x153fc9b`, both against the extension string from `sub_175D1C0` and both inserting into `[r12+0xD8]` — plus the constructor seed in `NeffFileWriter` @ `0x1543eb0`, which loads `"info.json"` @ `0x15440cf`.*

> **GOTCHA — the `.npy` test is against the file EXTENSION, so every `*.npy` weight is signed.** The compared string (`rbp`) is `sub_175D1C0`'s return — `file.filename()` then `rfind('.')`, i.e. `path::extension()` — not the whole basename. For a real weight `mlp_0_bias.npy`, `extension()` is `".npy"`, so `extension().compare(".npy") == 0` is **true** and the member is signed. Reading `compare(".npy") == 0` as "basename must literally be `.npy`" is the trap: `compare` runs on the extension substring, which equals `.npy` for the whole weight family. The `.npy` branch is live, not vestigial.

### Why typing is name-convention, not an enum

There is no numeric file-type field anywhere in a BOM entry — the BOM node stores only `{ path key, string member-name value }`, with the offset and size derived from `stat()` at write time and no per-entry checksum. Member typing is expressed three ways, all consistent with the above: (1) the **tar member name** encodes type by suffix convention (`*_instr` = instruction stream → `<Engine>.bin`; bare `<token>` = DMA-descriptor table → `<Engine>.json`; `*_dbg`/`*_asm_dbg` = debug info; `*.json` = metadata; `*.npy`/`*.bin` = const/weight); (2) the `def.json` `"var"` table types each buffer (`'file'`/`'input'`/`'output'`/`'virtual'`/`'tmp-buf'`, read by `analyze_neff_artifacts.py:147-194`); and (3) the boolean IR-signature set is the "is-this-member-hashed?" tag. A reimplementer keys the runtime off the member *name*, not off any type byte.

---

## Considerations

- **Member ordering.** The archive emits members in `std::map<path,member>` *key* order over the absolute on-disk source paths — deterministic, but **not** the order `addToBom` was called in. The per-engine `.bin`/`.json` files live under `nc<core>/sg<subgraph>/sgLnk/`; the consumer's `--prefix` points at that `sg00` directory.
- **Multi-stream variants.** A single engine may emit more than one stream; the secondary streams append a numeric id (`<Engine>.<id>.bin`). The base five-name set is the common case and the one the shipped consumer enumerates.
- **`.dbg` last-write-wins.** A `.dbg` file is registered twice — once by `writeDefJson` (`<tok>_dbg`/`<tok>_asm_dbg`) and once by `writePackageFile` (`0x15200e0:551`) under the literal override `"debug_info"`. `addToBom` is last-write-wins on the path key, so the final member name depends on call order; either way the basename contains `.dbg`, so it is in the signature set regardless.
- **The constructor seed runs unconditionally.** `info.json` enters the signature set even on the `internalOverride` "file.neff" fast path's sibling logic; the seed is in the ctor and is not gated by a member actually being added — there is no guard around it.

## Related Components

| Name | Relationship |
|---|---|
| [2.1 — The 64-Byte Instruction Bundle](../isa/instruction-bundle.md) | The bundle these `.bin` members are flat streams of; links back to this page |
| [8.36 — Per-Engine `.bin` Emission](../walrus/bin-emission.md) | The emitter (`findBin` `0x11f4b90`, 64-byte `fwrite`); this page is the container view of its output |
| `bir::ModuleArtifactInfo` (libBIR) | The per-module `bom` object owning `getEngInstrFile`/`getEngDMADescFile` (engine → basename) |
| `NeffFileWriter::addToBom` (`0x153fb80`) | The BOM upsert + signature-membership predicate |

## Cross-References

- [2.1 The 64-Byte Instruction Bundle](../isa/instruction-bundle.md) — the per-op 64-byte bundle these streams concatenate; the descriptor slot families and the universal header word.
- [8.36 Per-Engine `.bin` Emission](../walrus/bin-emission.md) — `findBin`/`createBin`, the `fwrite(0x40)` write primitive, and the per-instruction engine routing that folds DMA into compute streams.
- [12.3 `def.json` and the NEFF JSON sidecars](neff-json-sidecars.md) — the full `def.json`/`neff.json`/`tensor_map.json` schemas this page references for the engine-token index and the `var` type table.
- [The NEFF container](neff-container.md) — the PAX-tar member model, the in-process BOM `std::map<path,member>`, and the proof that a NEFF is not an ELF.
