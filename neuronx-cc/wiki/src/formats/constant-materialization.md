# Constant / Weight Materialization & De-dup

> *Addresses on this page belong to three ELF objects from `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp311/cp312 are byte-identical rebuilds). The HLO stage lives in `neuronxcc/starfish/bin/hlo2penguin` (full `.symtab`). The BIR const-tensor model lives in `neuronxcc/starfish/lib/libBIR.so` (`.rodata` @ `0x708000`, VMA == file-offset). The file/dedup/NEFF stages live in `neuronxcc/starfish/lib/libwalrus.so` (`.text` @ `0x62d660`, `.rodata` @ `0x1c72000`, VMA == file-offset). Each address is tagged for the object it belongs to. Treat every offset as version-pinned.*

## Abstract

A model's weights start life as out-of-band `.npy` files referenced by an HLO `Parameter`, and end as **deduplicated byte-blob members of the NEFF tar**. This page traces that descent end to end and names the real symbol at each hop:

1. **HLO** — `xla::InlineWeights` reads the weight payload off disk with `hilo::npy2literal`, wraps it in a `kConstant` via `HloInstruction::CreateConstant`, splices it into the computation, and drops the now-dead weight `Parameter` (`RemoveParameter`). After this pass the module has *no weight parameters* — only constants.
2. **BIR** — the constant lowers to a tensor whose `bir::TensorClass` is one of **three** distinct const/weight classes (`NeuronWeightTensor`, `IdentityWeightTensor`, `ImmutableParameter`); the data sits in a `klr::SharedConstantFile` and is wired to the tensor by `const_ap_offset` / `const_sub_tensor_id`.
3. **FILE** — `TranslateNKIASTToBIR::copyConstFileToArtifact` relocates the const `.bin` into the module's artifact directory so the NEFF packager can pick it up.
4. **DEDUP** — `MemlocFileDeDuper` collapses identical const blobs to a single physical member. **The dedup key is `MD5(bytes) + Dtype2string(dtype) + MemoryType2string(memtype)`** — two consts with identical bytes but different dtype *or* memory-type are **not** merged. That deterministic key is what makes the NEFF's [reproducible-tar guarantee](./neff-container.md) cover its constant section.

The headline a reimplementer must replicate exactly is **the three-part dedup key** and the fact that it is assembled across two cooperating functions (the byte-MD5 in one, the dtype+memtype suffix in the other), joined by the public `getUniqFileName` entry.

| | |
|---|---|
| **HLO inliner** | `xla::InlineWeights::Run` @ `0x1fb8f30` (hlo2penguin); `name()` → `"inline-weights"` @ `0x1fb7220` |
| **npy loader** | `hilo::npy2literal` @ `0x21e35f0` (hlo2penguin) |
| **const class enum** | `bir::TensorClass2string` @ `0x3ca7f0` (libBIR); 13 values `0..12`, three const/weight members |
| **const-file copy** | `TranslateNKIASTToBIR::copyConstFileToArtifact` @ `0xf08980` → `copyFileToFolder` @ `0xf98340` (libwalrus) |
| **dedup (key)** | `FileDeDuper::getHash` @ `0x10b4130` (MD5) + `MemlocFileDeDuper::getHash` @ `0x10b45c0` (dtype+memtype) |
| **dedup (entry)** | `MemlocFileDeDuper::getUniqFileName` @ `0x10b7480` → `FileDeDuper::addFileAndFindUniq` @ `0x10b4c30` (libwalrus) |
| **dedup map** | `unordered_map<string, vector<FileDeDuper::FileData>>` (MD5-bucket collision vector) |
| **NEFF writer** | `NeffPackager::writeVarDefinitions` @ `0x1526530`; `NeffFileWriter::writeArchiveFile` @ `0x153e030` (libwalrus) |

---

## 1 — HLO stage: `xla::InlineWeights`

`xla::InlineWeights::Run(HloModule*, const absl::flat_hash_set<string_view>&)` is a standard XLA `HloPassInterface` pass, registered through `hilo::RegisterInlineWeights` (the `_Function_handler` thunk for it is at `0x1e714a0` in hlo-opt). Its `name()` (`0x1fb7220`) returns the 14-byte string `"inline-weights"` (fileoff `0x46463` in hlo2penguin) — `[VERIFIED: the bytes at `0x46463` read `inline-weights`]`.

**The pass exists in *both* front-end ELFs.** It is compiled into `hlo2penguin` (the F0 `HLOToTensorizer` job) at `0x1fb8f30` and again into `hlo-opt` (the F2 `Frontend` job) at `0x1ee8c60`. The two are the same source pass at different link bases. The deep pass-internals (the `metadata.json` reader, the diagnostic family) are documented on the Part-4 sibling that owns the `hlo-opt` instance — [HLO misc-cleanup sweep §3.1](../hlo-opt/hlo-misc-cleanup-sweep.md); this page pins the `hlo2penguin` instance because it is the HLO→Penguin lowering that feeds the BIR const-tensor model below. `[CONFIRMED — both `_ZN3xla13InlineWeights3RunE…` symbols present, one per ELF.]`

The body of `Run` (`0x1fb8f30`, ends at `0x1fbb6f0`) calls, in order — every callee demangled from the disassembly:

```c
// xla::InlineWeights::Run  —  hlo2penguin 0x1fb8f30   [CONFIRMED — call graph]
HloPassResult InlineWeights_Run(HloModule *m, const flat_hash_set<string_view> &allow)
{
    // The pass reads an out-of-band model directory: "<dir>/metadata.json"
    // (string @ hlo2penguin 0x4a2e6 "/metadata.json"), whose JSON lists
    // "model_files" (0x324fe) / "weight_files" (0x669d6) → per-parameter .npy paths.
    for (HloComputation *comp : m->computations()) {
        for (HloInstruction *p : weight_parameters(comp)) {     // entry params flagged as weights
            Literal lit = hilo::npy2literal(npy_path_of(p),      // 0x21e35f0 — reads .npy off disk
                                            /*transpose=*/false);
            //   ↓ wrap the materialized tensor in a constant HLO instruction
            auto k = HloInstruction::CreateConstant(move(lit));  // 0x930b700 — the kConstant
            comp->AddInstruction(move(k), name_view);            // splice; uses now point at the const
            comp->RemoveParameter(p->parameter_number());        // drop the dead weight Parameter
        }
        comp->ComputeProgramShape(/*include_ids=*/true);         // rebuild entry signature after removal
    }
}
```

The `flat_hash_set<string_view>` argument is the allow-set of computations/instructions the pass is permitted to touch; it is not the weight list. **Net effect:** after `InlineWeights`, the HLO module carries `kConstant` instructions whose `Literal` data came from `.npy` files, and has no weight *parameters* left. Those constants are what the BIR stage (§2) lowers into device constant buffers.

> **CORRECTION — the `.npy` path is not `frontend_attributes["weight_file"]`.** An earlier internal note placed the weight path under an HLO `frontend_attributes["weight_file"]` key, citing a string at `0x66996`. The bytes at hlo2penguin `0x66996` are in fact `"binary_op_name"`, not `"weight_file"`; there is **no** `weight_file` (singular) string in the binary. The real source is the model-directory JSON: `/metadata.json` (`0x4a2e6`) listing `model_files` (`0x324fe`) and `weight_files` (`0x669d6`), with the hard-error `"Corrupted input dir, model file does not exist!"` (`0xf65b8`) and `"Please ensure metadata.json exists and has read permissions."` (`0xb1fb0`) guarding it. The `frontend_attributes` strings that *do* exist (`0x1a2c7`, `mhlo.frontend_attributes` @ `0x11aa3`) belong to unrelated MHLO attribute plumbing. `[CONFIRMED — string-offset reads disagree with the prior note.]`

---

## 2 — BIR const-tensor model: the `TensorClass` enum

A lowered constant becomes a `bir` tensor whose **buffer family** is named by the `bir::TensorClass` enum — a 13-value enum (`0..12`) decoded firsthand from the jump table of `bir::TensorClass2string(bir::TensorClass)` @ **libBIR `0x3ca7f0`** (`_ZN3bir18TensorClass2stringB5cxx11ENS_11TensorClassE`). The body bounds-checks `cmp $0xc,%esi / ja default` (valid `0..12`, default throws `"Unknown tensor class type"` @ `0x70b6aa`), then dispatches through a 13-entry `int32` self-relative jump table @ `.rodata 0x78a524`. Each case does `lea <name>(%rip),%rsi`; decoding the table and matching every target to its `lea` gives the full ordinal→name map:

| Ordinal | Class name (rodata addr) | Const/weight role |
|--:|---|---|
| 0 | `Tensor` (`0x70b67f`) | generic base |
| 1 | `SingleValueTensor` (`0x70b5d4`) | scalar/broadcast |
| 2 | `NeuronTensor` (`0x70b5e6`) | on-chip generic |
| **3** | **`NeuronWeightTensor`** (`0x70b5f3`) | **weight** — matmul stationary / PE-array weight stream |
| **4** | **`IdentityWeightTensor`** (`0x70b606`) | **weight** — the 128×128 identity (matmul-transpose helper) |
| 5 | `NeuronBlockTensor` (`0x70b61b`) | blocked on-chip |
| 6 | `DRAM2DBlockTensor` (`0x70b62d`) | off-chip, 2-D AP |
| 7 | `Neuron3DBlockTensor` (`0x70b63f`) | on-chip, 3-D AP |
| 8 | `DRAM3DBlockTensor` (`0x70b653`) | off-chip, 3-D AP |
| 9 | `NeuronLocalTensor` (`0x70b665`) | per-partition local |
| 10 | `NeuronSBTensor` (`0x70b677`) | state-buffer resident |
| 11 | `NeuronPSUMTensor` (`0x70b686`) | PSUM resident |
| **12** | **`ImmutableParameter`** (`0x70b697`) | **read-only constant** — the inlined weight/const data |

`[CONFIRMED — jump table @ 0x78a524 decoded: ordinal 3→0x3ca870→`lea 0x70b5f3`, ordinal 4→0x3ca880→`lea 0x70b606`, ordinal 12→0x3ca828→`lea 0x70b697`; all 13 strings present at the listed offsets.]`

> **CORRECTION — there are THREE distinct const/weight classes, not two.** Prior siblings listed "`ImmutableParameter` / `IdentityWeightTensor`" as if those were the const classes, conflating roles. The enum cleanly separates **three** members: `NeuronWeightTensor` (3, a tensor flagged as a weight), `IdentityWeightTensor` (4, the identity/passthrough weight), and `ImmutableParameter` (12, the read-only constant data the inliner planted). They are distinct ordinals, not synonyms. `[CONFIRMED — three separate jump-table cases.]`

`TensorClass` round-trips through BIR's JSON by **name string**, never by ordinal — `nlohmann::adl_serializer<bir::TensorClass>::to_json` @ libBIR `0x484f90` / `from_json` @ `0x483f80`, plus the symmetric `string2TensorClass` @ `0x3ca940`. The serializer is reached only from `MemoryLocationSet::{toJson,createFromJson}`, i.e. the class is a **Set-level** property of the logical tensor (see [BIR MemoryLocation §TensorClass](../bir/memory-location.md) for the Set/Location split and the full roster, and the [Penguin tensor/buffer nodes](../penguin/tensor-buffer-node.md) page for the Python-side class hierarchy).

The const-bearing tensor carries three extra JSON fields (tensor-level schema cluster @ libBIR `0x70c880`):

| Field | Meaning |
|---|---|
| `const_ap_offset` | byte offset of this tensor's data within its const `.bin` |
| `const_sub_tensor_id` | id of the sub-tensor a const tensor references — lets several tensors share one const file by index, decoupling the logical tensor from the physical blob |
| `is_regloc_offset` | whether the offset is register-relative |

The physical bytes live behind a `klr::SharedConstantFile` (serialized into the KLIR/BIR stream): `klr::SharedConstantFile_ser(FILE*, shared_ptr<…>)` @ libwalrus `0xf46400`, `…_des(FILE*)` @ `0xf4ec40`, the `List_` variants @ `0xf49ab0` / `0xf6a810`, `to_string` @ `0xf82440`. A function carries a **list** of these (each a `shared_ptr`); "Shared" means one physical const file can back several tensors — the in-memory precursor to the on-NEFF dedup of §4. `[CONFIRMED — all symbols defined at the listed addresses.]`

---

## 3 — Const-file artifact copy: `copyConstFileToArtifact`

Before NEFF packaging, the const data is materialized on disk as a standalone file inside the module's artifact directory. The mover is:

`neuronxcc::backend::TranslateNKIASTToBIR::copyConstFileToArtifact(bir::InstNKIKLIRKernel&, bir::Function&, const std::string&)` @ libwalrus **`0xf08980`**.

Disassembled control flow:

```c
// copyConstFileToArtifact  —  libwalrus 0xf08980   [CONFIRMED — disasm]
void copyConstFileToArtifact(InstNKIKLIRKernel &k, Function &fn, const string &constFileName)
{
    // 1. Walk the function's memory locations, filtering for const-bearing ones.
    //    (boost transformed_range<filtered_range<MemoryLocationSet→MemoryLocation>>;
    //     range ctor @ 0xf08a94.)
    for (MemoryLocation &ml : fn.allocs_filtered_const()) {
        // 2. Resolve the owning module from the kernel instruction.
        Function *f  = k.getInstruction()->getFunction();   // plt 0x6130e0, call @ 0xf08daa
        Module   *m  = *(Module**)((char*)f + 0xe8);        // module ptr @ Function+0xe8
        assert(*(void**)((char*)m + 0x48) != nullptr);      // null-checked @ f08db6/f08dbd

        // 3. Destination = the module's artifact directory.
        string dst = m->getArtifactAbsPath();               // plt 0x5fadf0, call @ 0xf08dc6
                                                            // (setter setArtifactAbsPath @ libBIR 0x355310)

        // 4. Relocate the const file into <artifactAbsPath>.
        copyFileToFolder(constFileName, srcFolder, dst);    // plt 0x5fa0b0 → 0xf98340, call @ 0xf08ddb

        // 5. Append an artifact_path / artifact_info record (module-schema fields).
        //    nlohmann basic_json ctor @ f0944e / f094c4; destroyed @ f09528.
    }
}
```

`copyConstFileToArtifact` is invoked once per NKI-KLIR kernel instruction that owns const files: the direct call site is `0xf0b2c9` (through PLT `0x5f2b30`), with inlined clones at `0xf1145f` / `0xf1178f` / `0xf11aef` / `0xf11e4f` inside the kernel-lowering neighbourhood (`lowerKernelInst` @ `0xf0b610`). `[CONFIRMED — call sites in disasm.]`

`neuronxcc::backend::copyFileToFolder(const string& file, const string& from, const string& to)` @ libwalrus **`0xf98340`** composes the destination path with `std::filesystem::path` machinery — `path::_M_split_cmpts` (plt `0x627a50`), `path::_List` ctor/deleter, and the `'/'` separator (`0x1c82442`) appended via `basic_string::_M_append`. The byte copy is `std::filesystem::copy_file` (imported `_ZNSt10filesystem9copy_fileE…@GLIBCXX_3.4.26`, PLT stub `0x5f4a80`). `[CONFIRMED — path machinery + copy_file import.]`

> **CORRECTION — the literal `copy_file` *call site* is not inside `copyFileToFolder`.** The single direct call to `std::filesystem::copy_file` (PLT `0x5f4a80`) in the whole of `libwalrus` is at `0x16d2ca6`, inside `LncSplitter::concretizeInstruction` (`0x16d23d0`) — not in `copyFileToFolder` (`0xf98340`). `copyFileToFolder`'s own PLT call set is purely path-composition (`_M_split_cmpts`, `_List` ctor, `_M_append`, `_M_replace`, `memcpy`, `__assert_fail`); the actual data move it performs reaches the filesystem layer through internal (non-PLT) helpers rather than the imported `copy_file` overload. The earlier note's "copy_file inlined into copyFileToFolder" is imprecise: a reimplementer should model `copyFileToFolder` as *path composition + a filesystem copy*, but should not expect the `copy_file@GLIBCXX_3.4.26` symbol to be reached from this function specifically. `[CONFIRMED — only call site of PLT 0x5f4a80 is at 0x16d2ca6.]`

---

## 4 — NEFF const/weight dedup: `MemlocFileDeDuper`

This is the page's headline. Two cooperating classes in `neuronxcc::backend` deduplicate const blobs.

### 4a — The dedup key: `MD5(bytes) + dtype + memtype`

The key is assembled across **two** functions joined by the public entry. Confirmed by disassembly:

```c
// FileDeDuper::getHash(vector<char>&)  —  libwalrus 0x10b4130   [CONFIRMED — disasm]
// Computes ONLY the content MD5 of the byte blob. Does NOT touch dtype/memtype.
string FileDeDuper_getHash(const vector<char> &bytes) {
    uint8_t digest[16];
    md5(bytes.data(), bytes.size(), digest);     // inline MD5; IV @ .rodata 0x1dd3590 =
                                                 //   67452301 efcdab89 98badcfe 10325476 (standard MD5 IV)
    return getMd5String(digest);                 // 0x1088df0 (plt 0x606f30, call @ 0x10b439b) → 32-hex string
}

// MemlocFileDeDuper::getHash(const bir::MemoryLocation&)  —  libwalrus 0x10b45c0   [CONFIRMED — disasm]
// Produces the dtype + memtype SUFFIX. Does NOT read the file or compute MD5.
string MemlocFileDeDuper_getHash(const MemoryLocation &ml) {
    bir::Dtype      dt = ml.getDtype();                       // plt 0x5f99a0, call @ 0x10b45e1/0x10b479b
    bir::MemoryType mt = *(int*)((char*)ml_inner(ml) + 0xd8); // memtype read from field +0xd8
    string s;
    s += Dtype2string(dt);        // plt 0x5f6530 → libBIR 0x2641e0   (call @ 0x10b47a5)
    s += MemoryType2string(mt);   // plt 0x5ee060 → libBIR 0x3ca040   (call @ 0x10b4a12)
    return s;                     // joined via basic_string::_M_append (plt 0x600aa0)
}

// MemlocFileDeDuper::getUniqFileName  —  libwalrus 0x10b7480   [CONFIRMED — disasm]
// THE PUBLIC ENTRY. Joins the two halves and looks up the canonical unique file.
string getUniqFileName(const MemoryLocation &ml, const string &path) {
    string suffix = MemlocFileDeDuper_getHash(ml);            // plt 0x61ddf0, call @ 0x10b74b0
    // addFileAndFindUniq reads the file bytes, MD5s them via FileDeDuper::getHash,
    // and combines with `suffix` to form the map key.
    return addFileAndFindUniq(/* path + suffix-keyed */);     // plt 0x601160, call @ 0x10b7591
}
```

So the composite dedup key is:

| Key component | Source symbol | Address |
|---|---|---|
| `MD5(file bytes)` → 32-hex | `FileDeDuper::getHash(vector<char>&)` + `getMd5String` | libwalrus `0x10b4130` + `0x1088df0` |
| `Dtype2string(dtype)` | `bir::Dtype2string` (fed by `MemoryLocation::getDtype` @ plt `0x5f99a0`) | libBIR `0x2641e0` |
| `MemoryType2string(memtype)` | `bir::MemoryType2string` (memtype @ `MemoryLocation+0xd8`) | libBIR `0x3ca040` |

`MemoryType` (stringifier @ libBIR `0x3ca040`, rodata roster @ `0x70b52c…`) covers `REG`, `Unallocated`, `DRAM`, `RNGSTATE`, `ExternalInput`/`ExternalInputParameter`, `ExternalOutput`/`ExternalOutputParameter`, `Const`, `Internal`, `Pointer`, `InternalInterface`. **Consequence:** a const blob in `DRAM` versus `SB` with byte-identical contents hashes to a *different* key and is **not** merged. Identical bytes + identical dtype + identical memtype is the only thing the deduper collapses. `[CONFIRMED — all three append sites in disasm.]`

> **CORRECTION — which function appends dtype/memtype.** An earlier note attributed the dtype/memtype append to `FileDeDuper::getHash(vector<char>&)` (`0x10b4130`). Firsthand that overload calls **only** `getMd5String` (no `Dtype2string`/`MemoryType2string` calls in its body) — it produces the pure content MD5. The dtype+memtype suffix is produced by the *memloc-aware* specialization `MemlocFileDeDuper::getHash` (`0x10b45c0`), which is the one that calls `Dtype2string` (plt `0x5f6530`) and `MemoryType2string` (plt `0x5ee060`). The two halves are joined by `getUniqFileName` (`0x10b7480`). The **resulting key is identical** (`MD5 + dtype + memtype`); only the attribution of *where* each part is computed is corrected. `[CONFIRMED — call-target sets of the two getHash bodies differ exactly as described.]`

### 4b — The dedup map and the lookup

`FileDeDuper::addFileAndFindUniq(const std::string&)` @ libwalrus **`0x10b4c30`** is the worker:

```c
// addFileAndFindUniq  —  libwalrus 0x10b4c30   [CONFIRMED — disasm]
string addFileAndFindUniq(const string &path /* carries the dtype+memtype suffix */) {
    // 1. stat + read the file bytes into a vector<char>:
    //    istream::tellg (0x60fa20) → seekg (0x5f3160) → read (0x6179d0).
    vector<char> bytes = read_whole_file(path);
    // 2. content MD5:
    string hash = FileDeDuper::getHash(bytes);          // plt 0x5fdee0 → 0x10b4130
    // 3. bucket lookup in the collision-tolerant map:
    auto &bucket = uniq_map_[hash];                     // operator[] @ 0x10b9490
    for (FileData &fd : bucket)                          // vector handles MD5 collisions
        if (fd.matches(bytes, suffix)) return fd.uniqName; // "FileDeDuper found match" (0x1c8255f)
    bucket.push_back(FileData{...});                     // "FileDeDuper added new uniq"  (0x1c82578)
    return newUniqName;
}
```

The map is:

```c
std::unordered_map< std::string,                                 // key = MD5(bytes)+dtype+memtype
                    std::vector<FileDeDuper::FileData> >          // bucket: MD5-collision tolerant
```

— confirmed by the demangled `_Map_base<…basic_string…, pair<…string, vector<FileDeDuper::FileData>>…>::operator[]` @ `0x10b9490` and the hashtable nodes @ `0x10b9270`. The collision *vector* is the reason the value is `vector<FileData>` and not a bare `FileData`: two distinct blobs sharing an MD5 stay separable. `[CONFIRMED — map type demangled.]`

Diagnostic strings (libwalrus `.rodata`, human-readable):

| Offset | String |
|---|---|
| `0x1c82527` | `FileDeDuper file not found` |
| `0x1c82543` | `FileDeDuper failed to read` |
| `0x1c8255f` | `FileDeDuper found match` |
| `0x1c82578` | `FileDeDuper added new uniq` |
| `0x1d38258` | `FileDeDuper failed to get filesize for ` |
| `0x1d38280` | `MemlocFileDeDuper new entry for ` |
| `0x1d382a8` | `MemlocFileDeDuper found match for ` |
| `0x1c8689d` | `Const File de-dup saved ` (… `KB of memory footprint`) |

Savings counters: `MemlocFileDeDuper::getBytsDedupedTotal()` @ `0x10b45a0`, `FileDeDuper::getNumUniqFiles()` @ `0x10b4560`. The user-visible `"Const File de-dup saved <N> KB"` line is emitted from `writeVarDefinitions` (§4d). `[CONFIRMED — strings at listed offsets.]`

### 4c — In-DRAM alignment vs on-NEFF padding

Two different alignments apply, and conflating them is a classic error:

- **`const_ap_offset`** (the tensor's byte position inside the const `.bin`, and its in-DRAM/SBUF placement) is set by `neuronxcc::backend::getAlignmentSize(bir::MemoryLocation*, const PassOptions&, bool)` @ libwalrus `0x1091890`. Alignment is *not* a global constant: it is derived per memory-location from the tensor's `Dtype` and its `PhysicalAccessPattern` (one branch forces an 8-element alignment, another uses `0xc0` = 192), via `isValidTileSize` and `PhysicalAccessPattern::getOffsetInBasePartition`. `[STRONG]`
- **On-NEFF tar padding** is libarchive's PAX default — each member's data is padded to a 512-byte tar record. There is no extra walrus-imposed per-member alignment in `writeArchiveFile`. See [NEFF container §determinism](./neff-container.md). `[CONFIRMED — no `archive_write_set_bytes_per_block` override in the writer.]`

A weight's NEFF byte offset is therefore the tile/partition-aligned `const_ap_offset` from `getAlignmentSize`; the tar member itself only carries 512-B PAX block padding.

### 4d — Wiring const data to variables

`NeffPackager::writeVarDefinitions(json&, NeffFileWriter&, const bir::Module&, const boost::filesystem::path&)` @ libwalrus `0x1526530` emits the per-variable records that point logical tensors at the deduped physical member:

- variable fields (rodata cluster `0x1c8682a`): `var_id`, `var_offset`, `referenced_var_id`, `backing_buf`, `backing_variable_off`, `virtual`, `pointer`, `state-buffer`, `remote`, `tmp-buf`.
- const-tensor fields (tensor schema `0x70c880`): `const_ap_offset`, `const_sub_tensor_id`, `is_regloc_offset`.
- the const payload is recorded as a `.bin` (or `.npy`) member with a `TypeDescriptor` and the access-pattern descriptor `to_sizes / to_off / to_dtype / to_steps / from_off / from_dtype / from_steps` (rodata `0x1c83f44`) — the runtime DMA AP that copies the const from its NEFF member into DRAM/SBUF at load time.

Multiple `var_id`s sharing one deduped blob point at the same `referenced_var_id` / `const_ap_offset`, which is exactly how the §4a/4b dedup pays off at the variable layer.

### 4e — Why dedup feeds reproducibility

`NeffFileWriter::writeArchiveFile` (`0x153e030`) builds the NEFF as a **deterministic** gzip-filtered PAX tar: `archive_write_set_format_pax`, `archive_write_add_filter_gzip` with `compression-level=1` and `timestamp=false`, owner `"nobody"`, and `mtime/ctime/atime` all zeroed (full detail on [NEFF container](./neff-container.md)). Because `MemlocFileDeDuper`'s key is a pure function of `(bytes, dtype, memtype)` — no timestamps, no allocation addresses, no iteration-order nondeterminism in the *key* itself — the **set of unique const members** is identical across two compiles of the same IR. The deduper is the component that makes the *constant section* of the reproducible-tar guarantee hold: two builds dedupe to the same members, which the deterministic tar then packs to the same bytes. `[STRONG — key is content+dtype+memtype only; tar determinism is CONFIRMED separately.]`

---

## End-to-end summary

```text
HLO   weight Parameter (path via <dir>/metadata.json → model_files/weight_files)
      ── InlineWeights "inline-weights" (0x1fb8f30) ──►
      npy2literal(W.npy) (0x21e35f0) → Literal
      → HloInstruction::CreateConstant (0x930b700) → kConstant spliced; param dropped (RemoveParameter)
        │
BIR   constant → bir tensor; TensorClass = ImmutableParameter(12)
      (or NeuronWeightTensor(3) / IdentityWeightTensor(4) for weight roles);
      MemoryLocation carries dtype + memtype; data behind klr::SharedConstantFile;
      tensor JSON: const_ap_offset / const_sub_tensor_id
        │
FILE  TranslateNKIASTToBIR::copyConstFileToArtifact (0xf08980)
      → copyFileToFolder (0xf98340) drops the const .bin into the module artifact dir;
      artifact_path / artifact_info appended
        │
DEDUP MemlocFileDeDuper.getUniqFileName (0x10b7480):
      key = MD5(bytes) [FileDeDuper::getHash 0x10b4130]
          + Dtype2string + MemoryType2string [MemlocFileDeDuper::getHash 0x10b45c0]
      → addFileAndFindUniq (0x10b4c30) collapses identical (bytes,dtype,memtype) to ONE member
      → getAlignmentSize (0x1091890) gives the tile-aligned const_ap_offset
        │
NEFF  writeVarDefinitions (0x1526530) wires var_id → referenced_var_id / const_ap_offset;
      writeArchiveFile (0x153e030) packs BOM members into a deterministic gzip PAX tar.
      Runtime DMAs each const from its tar member via the to_*/from_* access-pattern descriptor.
```

---

## Adversarial self-verification

The five strongest claims, re-checked against the binaries:

1. **Dedup key = `MD5(bytes) + dtype + memtype`.** `MemlocFileDeDuper::getHash` (`0x10b45c0`) disassembles to calls of `MemoryLocation::getDtype` (plt `0x5f99a0`), `Dtype2string` (plt `0x5f6530`), and `MemoryType2string` (plt `0x5ee060`), joined with `_M_append`; `FileDeDuper::getHash` (`0x10b4130`) calls `getMd5String` (plt `0x606f30`) over the byte blob. **CONFIRMED.**
2. **The dedup map is `unordered_map<string, vector<FileDeDuper::FileData>>`.** `operator[]` symbol @ `0x10b9490` demangles to exactly that `_Map_base<basic_string, pair<…, vector<FileDeDuper::FileData>>>`. **CONFIRMED.**
3. **Three distinct const/weight `TensorClass` ordinals: 3, 4, 12.** Jump table @ libBIR `0x78a524` decoded entry-by-entry: ordinal 3→`NeuronWeightTensor`, 4→`IdentityWeightTensor`, 12→`ImmutableParameter`; bounds `cmp $0xc / ja`. **CONFIRMED.**
4. **`copyConstFileToArtifact` (`0xf08980`) → `getArtifactAbsPath` → `copyFileToFolder`.** Call sites at `0xf08dc6` (plt `0x5fadf0`) and `0xf08ddb` (plt `0x5fa0b0`) in the disasm. **CONFIRMED.**
5. **`InlineWeights` exists in both hlo2penguin (`0x1fb8f30`) and hlo-opt (`0x1ee8c60`); chain is `npy2literal → CreateConstant → AddInstruction → RemoveParameter`.** Both `Run` symbols present; the four callees demangle out of the hlo2penguin body. **CONFIRMED.**

**Honest verification ceiling.** What is *not* fully pinned: (a) the exact predicate that selects which entry parameters `InlineWeights` treats as weights (the `metadata.json` schema parse is owned by the [Part-4 sibling](../hlo-opt/hlo-misc-cleanup-sweep.md), not re-derived here); (b) the precise internal helper inside `copyFileToFolder` that performs the byte copy — the function composes the path and reaches the filesystem layer, but the single PLT `copy_file` call lives in `LncSplitter::concretizeInstruction`, so the exact copy mechanism `copyFileToFolder` uses is `[INFERRED]` (filesystem copy via internal helper) rather than a confirmed `copy_file@plt` call; (c) the `FileData` struct layout (the `matches`/equality fields beyond the hash bucket) is `[INFERRED]` from the collision-vector shape, not field-decoded; (d) `getAlignmentSize`'s full per-dtype alignment table is `[STRONG]` (two branches read: 8-element and `0xc0`), not exhaustively enumerated.

---

## Cross-References

- [NEFF Container](./neff-container.md) — the deterministic gzip PAX-tar writer (`writeArchiveFile`) whose reproducible-build guarantee the §4 dedup feeds; member naming, BOM, 512-B padding.
- [HLO misc-cleanup sweep §3.1](../hlo-opt/hlo-misc-cleanup-sweep.md) — the Part-4 deep-dive on `InlineWeights::Run` (the `hlo-opt` instance `0x1ee8c60`, the `metadata.json` reader, the `NCC_INL` diagnostics).
- [BIR MemoryLocation](../bir/memory-location.md) — the Set/Location split, `TensorClass` as a Set-level property, and the wire schema.
- [Penguin Tensor / Buffer Node](../penguin/tensor-buffer-node.md) — the Python-side class hierarchy (`NeuronWeightTensor` / `IdentityWeightTensor` / `ImmutableParameter`) that lowers into these BIR classes.
