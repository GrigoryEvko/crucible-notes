# FindCustomOpData Staging, FunctionId Binding and ucode_lib.json

> *All symbols, offsets, and string-pool addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). The staging job is the Cython extension `neuronxcc/driver/jobs/support/FindCustomOpData.cpython-310-x86_64-linux-gnu.so` (BuildID `09fe533b…`, `with debug_info, not stripped`). The version-binding and packaging logic live in `neuronxcc/starfish/lib/libwalrus.so` (file offset == VA) and its `NEEDED` sibling `libBIR.so`. cp310/cp311/cp312 wheels share an identical `libwalrus.so` `.text` with a fixed per-build VA delta. Evidence anchors: **D-AC02** (staging job + version binding) and **D-AC01** (`ucode_lib.json` packaging). Treat every address as version-pinned.*

## Abstract

A custom op on Trainium is a kernel that runs on the 8-core Xtensa GPSIMD CPU cluster, not on the PE array. Before the compiler can emit a `bir::InstCustomOp` for it, two things must happen at compile time: the prebuilt CPU library (`.so`) that contains the kernel must be **located and staged** into the build, and the kernel's **function identity** must be assigned a stable numeric id that the wire encoding and the runtime both agree on. This page covers that staging-and-binding seam — the boundary between the Python driver, the C++ BIR registry, and the NEFF packager.

The seam has three layers. The **innermost** is `findCustomOpDataDir()`, a one-function Cython job that resolves the in-wheel `neuronxcc/data/custom_op/` directory through `importlib.resources` — pure path resolution, no environment probing and no version logic. Around it, the Python `Frontend` driver job joins that directory with the eight builtin library names (`libbuiltincustomop_cpu0..7`) and existence-checks them. **Downstream**, in C++, `bir::ModuleArtifactInfo` records each staged library in an `unordered_map<string, CustomOpLibInfo>`, hands every distinct kernel function a monotonically allocated `uint32` FunctionId via `allocateCustomOpFunctionId`, and `NeffPackager::writeUCodeLibDefinitions` serializes the whole table to `ucode_lib.json` as a NEFF tar member.

The reader's payoff is a precise mental model of who decides what. The compiler never invents ucode/ISA version numbers — those are byte values carried in from the user's custom-op build and round-tripped through `ucode_lib.json`. The compiler *does* own the FunctionId namespace (dense `0,1,2,…` per module) and the dedup/version-consistency guard on library re-registration. This page differs from a generic "resource loader" write-up in that the staging job has *no* policy — all policy (which arch, which libraries, what versions) is decided by its callers.

For reimplementation, the contract is:

- **`findCustomOpDataDir`**: the exact `importlib.resources.files('neuronxcc').joinpath('data').joinpath('custom_op')` chain, the `as_file`/`os.path` materialization tail, and the proof that there is no env override and no fallback.
- **The `Frontend` staging loop**: prefix `libbuiltincustomop_cpu` + index, `.is_file()` existence check, and the `Custom op data directory not found` error.
- **The C++ registry**: the 6-arg `addCustomOpLibFile` sink (with its dedup + version-consistency guard), the `(string, string, uint)` `addCustomOpFunction`, and the monotonic `uint32` `allocateCustomOpFunctionId`.
- **`ucode_lib.json`**: the schema, the constant `opcode: 133`, and the fact that the library `.so` binaries ride along as separate NEFF members.

| | |
|---|---|
| **Staging job** | `findCustomOpDataDir` body `__pyx_pw_…16FindCustomOpData_1findCustomOpDataDir` @ `0x7410` |
| **Staging consumer** | `neuronxcc/driver/jobs/Frontend.cpython-310…so` (imports `findCustomOpDataDir`) |
| **Staged artifacts** | `neuronxcc/data/custom_op/libbuiltincustomop_cpu{0..7}.stripped.so` (8 × 579 380 B) |
| **Registry sink** | `bir::ModuleArtifactInfo::addCustomOpLibFile(string,string,string,bool,uchar,uchar)` (6-arg) |
| **FunctionId allocator** | `bir::ModuleArtifactInfo::allocateCustomOpFunctionId()` → monotonic `uint32` |
| **Function binder** | `bir::ModuleArtifactInfo::addCustomOpFunction(string,string,uint)` |
| **Packager** | `NeffPackager::writeUCodeLibDefinitions` body @ `libwalrus 0x1522d60` → `ucode_lib.json` |
| **JSON string pool** | `library`…`ucode_lib.json` contiguous @ `libwalrus` file offset `0x1c8664f`–`0x1c866a8` |
| **CUSTOM_OP opcode** | constant `133` (`0x85`) per function; `sub_opcode` is the per-function FunctionId byte |

---

## findCustomOpDataDir — the in-wheel resource resolver

### Purpose

`findCustomOpDataDir()` answers exactly one question: *where on disk is the wheel's `custom_op` data directory?* The eight prebuilt GPSIMD CPU libraries (the SORT/TOPK builtins) ship inside the installed wheel at `neuronxcc/data/custom_op/`. Because the package can in principle be zip-imported, a raw `os.path.join(__file__, …)` would not always yield a real filesystem path; the job uses `importlib.resources` so the resource is materialized to a concrete directory even from a zip. The job has **no** environment override, **no** version logic, and **no** notion of FunctionId — it is pure path resolution. (CONFIRMED: the module imports neither `getenv`/`environ`/`PyOS_getenv` — `nm -D` shows no such symbol — nor carries any `NEURON_*`/`AWS_*`/`ucode`/`isa_version`/`FunctionId`/version string; exhaustive `strings(1)`.)

### Entry Point

```text
PyInit_FindCustomOpData                                       @ 0x4a96
  └─ __pyx_pw_…16FindCustomOpData_1findCustomOpDataDir         @ 0x7410   ── the body
       (NOARGS method; the pf/pw split is merged — body inlined into the wrapper)
       method def: __pyx_mdef_…_1findCustomOpDataDir           @ 0xd880

Importer:
  neuronxcc/driver/jobs/Frontend.cpython-310…so  ── `from …FindCustomOpData import findCustomOpDataDir`
```

### Algorithm

The complete module vocabulary is a short interned string pool (`.rodata`); the only domain nouns are `importlib(.resources)`, `files`, `joinpath`, `data`, `custom_op`, `os`, `path`, `data_path`, `customOpDataDir`. There is no other noun — that absence *is* the proof of "no policy."

```c
// neuronxcc/driver/jobs/support/FindCustomOpData.py  (Cython)
// body @ 0x7410 ; STRONG — string-pool + Cython attr-call disasm
function findCustomOpDataDir():
    // 0x750c: attr "resources" off module `importlib`  -> importlib.resources
    // 0x7582: attr "files" on it; 0x759c: call files('neuronxcc')
    base = importlib.resources.files('neuronxcc')          // Traversable = package root

    // 0x76e2: attr "joinpath" (mstate slot +0xC0), call @0x7755 (Py_EnterRecursiveCall guard)
    // 0x777f: SECOND "joinpath" (same slot +0xC0),  call @0x77f0
    data_path = base.joinpath('data').joinpath('custom_op')  // -> …/data/custom_op

    // 0x7720..0x78fb: __enter__/__exit__ + os.path tail
    //   -> the importlib.resources.as_file(...) idiom: materialize to a real fs path
    with importlib.resources.as_file(data_path) as customOpDataDir:
        return os.path.<...>(customOpDataDir)                // concrete directory string
```

The two chained `joinpath` calls reuse the same module-state slot (`+0xC0`), and the two remaining interned `u`-strings `data` and `custom_op` are the only path-component arguments left in the pool — natural order gives `joinpath('data')` then `joinpath('custom_op')`. The `__enter__`/`__exit__`/`os`/`path` strings confirm the `with … as f:` block that turns the `Traversable` into a concrete on-disk path before returning. (STRONG.)

> **NOTE —** the *only* path this function can return is `<site-packages>/neuronxcc/data/custom_op/`. There is no search of external directories, no `NEURON_CUSTOM_OP_PATH`-style env override, and no in-module "not found" handling — the existence check belongs to the caller (`Frontend`). A reimplementation that adds an env hook here is over-engineering the wrong layer.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `PyInit_FindCustomOpData` | `0x4a96` | module init / type table | CERTAIN |
| `__pyx_pw_…_1findCustomOpDataDir` | `0x7410` | the body (resolve `custom_op` dir) | CERTAIN |
| `__pyx_mdef_…_1findCustomOpDataDir` | `0xd880` | method def (NOARGS) | CERTAIN |

> **QUIRK —** the task brief and some downstream notes call the module path `starfish/driver/jobs/support/…`. The real dotted name is `neuronxcc.driver.jobs.support.FindCustomOpData` (no `starfish/` segment), confirmed against the wheel RECORD and the on-disk tree. The `starfish` tree holds the C++ `lib/` and `bin/`, not this Cython job.

---

## The Frontend staging loop — locating the builtin libraries

### Purpose

`findCustomOpDataDir()` returns a directory; it does not check that anything is *in* it. The Python `Frontend` driver job is the actual stager: it calls `findCustomOpDataDir()`, errors if the directory is missing, then for each of the eight builtin library names joins the name onto the directory and existence-checks it with `.is_file()`. Libraries that pass are wired into the BIR pipeline. (CONFIRMED — Frontend `.rodata` carries `findCustomOpDataDir`, `builtin_customop_names`, `builtin_libnames`, the prefix `libbuiltincustomop_cpu`, the error `Custom op data directory not found`, and `is_file`/`getArch`.)

### Algorithm

```c
// neuronxcc/driver/jobs/Frontend  ── custom-op staging step
// STRONG — string-pool + standard pathlib idiom
function Frontend_stage_custom_ops(arch):
    data_dir = findCustomOpDataDir()                 // the in-wheel custom_op dir
    if not os.path.isdir(data_dir):
        raise CompilerError("Custom op data directory not found")   // .rodata literal

    for libname in builtin_libnames:                 // names from builtin_customop_names
        p = pathlib.Path(data_dir) / libname         // "libbuiltincustomop_cpu" + index
        if p.is_file():                              // existence check
            register_builtin_custom_op_lib(p, arch)  // wire into the BIR pipeline
    // user custom ops: the user-provided lib dir is wired in separately (getArch-gated)
```

The library names are built from the prefix `libbuiltincustomop_cpu` plus an index, matching the eight `*.stripped.so` files on disk. `getArch`/`default` in the same pool indicate the staged set is arch-selected — the builtin set is offered for every target, while user-supplied custom-op directories are wired in per `getArch`. (The precise `getArch` control flow is STRONG, not byte-traced: the `Frontend` body was not fully decompiled, only its string pool mined.)

### The staged artifacts

```text
neuronxcc/data/custom_op/
  libbuiltincustomop_cpu0.stripped.so   579380 B   cpu_id 0
  libbuiltincustomop_cpu1.stripped.so   579380 B   cpu_id 1
  …
  libbuiltincustomop_cpu7.stripped.so   579380 B   cpu_id 7
```

Eight files, identical size, distinct sha256 per RECORD — one per GPSIMD core (`cpu_id = 0..7`, `total_cpus = 8`). These are the `is_builtin=true` SORT/TOPK GPSIMD builtins. (CONFIRMED on disk; the per-lib `cpu_id`/`total_cpus` *byte values* are filled in upstream by codegen — see the registry section — and are INFERRED from the file naming, not statically pinned per lib.)

> **GOTCHA —** `ucode_lib.json` is **not** shipped in the wheel. The `custom_op` data dir contains only the eight `*.stripped.so`. The JSON is generated per-build and embedded in the NEFF. So a reimplementer cannot read the concrete version numbers off the wheel — they originate from each custom-op library's build metadata at registration time.

---

## The BIR registry — CustomOpLibInfo, FunctionId, and the 6-arg sink

### Purpose

Once a library is staged, codegen records it on the module. The container is `std::unordered_map<std::string, bir::CustomOpLibInfo>` held by `bir::ModuleArtifactInfo` (a sub-object embedded in `bir::Module` at `+0xE8`). Each map entry binds one library file name to its ucode/ISA version strings, its `is_builtin`/`cpu_id`/`total_cpus` bytes, and an inner `functions` map of `funcName → FunctionId`. The sole upstream registrar is the codegen visitor `CoreV2GenImpl::visitInstCustomOp` (`libwalrus 0x12613c0`), which calls all three registry entry points during code generation.

### Entry Point

```text
CoreV2GenImpl::visitInstCustomOp   @ libwalrus 0x12613c0   ── sole registrar
  ├─ ModuleArtifactInfo::addCustomOpLibFile(name, ulib2ucode, ulib2isa, builtin, cpu, total)
  ├─ ModuleArtifactInfo::allocateCustomOpFunctionId()        ── id = counter++
  └─ ModuleArtifactInfo::addCustomOpFunction(name, fnName, id)

bodies in libBIR.so; reached from libwalrus via PLT thunks:
  addCustomOpLibFile          thunk @ libwalrus 0x5fb7d0
  allocateCustomOpFunctionId  thunk @ libwalrus 0x603ec0  (and bin/ copy @ 0x457fa0)
  addCustomOpFunction         thunk @ libwalrus 0x605350
```

> **NOTE —** in the shipped `.so` corpus these three symbols resolve only to 6-byte PLT thunks (`jmp cs:off_…`); the real bodies are static-archive copies in `libBIR.so` that Hex-Rays did not lift. The signatures below are recovered from the mangled names and the D-AC01/D-AC02 IDA-DB body analysis; the `.so` sidecars confirm the thunks and the argument types, not the instruction sequences.

### Algorithm — allocateCustomOpFunctionId

```c
// bir::ModuleArtifactInfo::allocateCustomOpFunctionId()
// STRONG — body from D-AC01/D-AC02 IDA DB; thunk confirmed @ libwalrus 0x603ec0
uint32 allocateCustomOpFunctionId(this):
    id = this->nextCustomOpFnId;        // uint32 counter (this+0xF0 / +240)
    this->nextCustomOpFnId = id + 1;    // post-increment
    return id;
```

A plain monotonic per-**module** counter: FunctionIds are dense `0,1,2,…` in allocation order, *not* per-library. The caller does `id = allocateCustomOpFunctionId(); addCustomOpFunction(lib, fn, id);` — the allocator is never called *inside* `addCustomOpFunction`. Because the FunctionId is stored as a single byte in the wire form (`sub_opcode`, see [CUSTOM_OP Wire Byte-Layout](customop-wire-layout.md)) with `0xFF` reserved as the "unset" sentinel, the namespace is hard-capped at ≤ 254 unique functions per module; the registrar enforces this with a `Number of unique custom op functions cannot exceed …` guard that fires past `0xFE` (254).

### Algorithm — addCustomOpLibFile (the 6-arg sink)

```c
// bir::ModuleArtifactInfo::addCustomOpLibFile(string file_name, string ulib2ucode,
//                                             string ulib2isa, bool is_builtin,
//                                             uint8 cpu_id, uint8 total_cpus)
// mangled tail ...S6_S6_bhh  =>  (string,string,string, bool,uchar,uchar)  [CONFIRMED sig]
// body @ libBIR; dedup/version guard from D-AC01 §5 / D-AC02 §3
void addCustomOpLibFile(this, file_name, ulib2ucode, ulib2isa, is_builtin, cpu_id, total_cpus):
    it = customOpLibs.find(file_name)            // map @ ModuleArtifactInfo+0xA0 (seed 0xC70F6907)
    if it != end:
        // dedup + version-consistency guard
        consistent = (it->ulib_to_ucode_version == ulib2ucode) &&
                     (it->ulib_to_isa_version   == ulib2isa)
        bir::reportError(consistent, /*ModuleArtifactInfo.cpp, addCustomOpLibFile*/)
        return                                   // idempotent on match; HARD ERROR on mismatch
    auto& e = customOpLibs[file_name]            // emplace new CustomOpLibInfo (operator new ~0xD0)
    e.file_name             = file_name          // +0x00
    e.ulib_to_ucode_version = ulib2ucode         // +0x20
    e.ulib_to_isa_version   = ulib2isa           // +0x40
    e.is_builtin            = is_builtin          // +0x60
    e.cpu_id                = cpu_id              // +0x61
    e.total_cpus            = total_cpus          // +0x62
```

The key is `file_name`, so each of `libbuiltincustomop_cpu0.so … _cpu7.so` is its own map entry with its own `cpu_id`, all sharing `total_cpus=8`. Re-registering the *same* library with *mismatched* version strings is a compile error — this is the dedup-with-consistency guard, and it is the only place version values are checked. The compiler never originates a version: the two `ulib_to_*` strings are passed in by the caller from the library's build metadata.

### Algorithm — addCustomOpFunction

```c
// bir::ModuleArtifactInfo::addCustomOpFunction(string lib_name, string fn_name, uint id)
// mangled tail ...S6_j  =>  (string,string, unsigned int)   [CONFIRMED sig]
void addCustomOpFunction(this, lib_name, fn_name, id):
    auto& e = customOpLibs.at(lib_name)          // reportError if lib not yet registered
    e.functions[fn_name] = (uint8)id             // 32-bit id TRUNCATED to one byte
```

The library must already exist (so `addCustomOpLibFile` precedes `addCustomOpFunction`). The `uint32` FunctionId is **truncated to a byte** as it lands in the inner `functions` map — that byte is the per-function `sub_opcode` the packager re-emits. Duplicate `fn_name` is idempotent (the existing node is kept, the new one freed). The inner container is `std::unordered_map<std::string, unsigned char>`, confirmed by the `IhSaIhEE` (`unsigned char`) value-type in the serializer's mangled name.

### CustomOpLibInfo struct layout

| Field | Offset | Type | JSON key (`to_json`) / use |
|---|---|---|---|
| `file_name` | `+0x00` | `std::string` | `file_name` (libBIR) / `library` (NEFF) |
| `ulib_to_ucode_version` | `+0x20` | `std::string` | `ulib_to_ucode_version` |
| `ulib_to_isa_version` | `+0x40` | `std::string` | `ulib_to_isa_version` |
| `is_builtin` | `+0x60` | `bool` | `is_builtin` (json bool, type 4) |
| `cpu_id` | `+0x61` | `uint8` | `cpu_id` (json uint, type 6) |
| `total_cpus` | `+0x62` | `uint8` | `total_cpus` (json uint, type 6) |
| `functions` | `+0x78` | `unordered_map<string,uint8>` | `functions` (`name → byte`) |

`sizeof(CustomOpLibInfo) ≈ 0xD0` (the `operator new(0xD0u)` at the emplace site). The three `std::string` fields are `0x20` apart (libstdc++ SSO: data ptr + size + 16-byte SSO buffer). (CONFIRMED offsets from the `to_json` reader and the live-node reads in `writeUCodeLibDefinitions`; the `from_json`/`to_json` bodies are PLT thunks in the corpus, so the struct offsets are grounded in the D-AC01 IDA-DB analysis cross-checked against the packager's live reads.)

> **QUIRK —** the *map* value type is `unordered_map<string,uchar>` even though the field name is `functions`. The value byte is the FunctionId/`sub_opcode`, not a pointer. Two serializations of the same struct exist: `libBIR` `to_json` emits `functions` as a JSON object `{name: byte}`; the NEFF packager re-walks the same nodes to emit `functions` as an *array* of `{name, opcode, sub_opcode}`. Same underlying container, two JSON shapes.

---

## writeUCodeLibDefinitions — packaging ucode_lib.json

### Purpose

`NeffPackager::writeUCodeLibDefinitions` (`libwalrus 0x1522d60`) walks the module's custom-op library set and serializes it twice: once embedded in `def.json` under the key `ucode_lib`, and once as the standalone `ucode_lib.json` NEFF tar member (the canonical artifact the runtime loads). It also stages each library `.so` binary as its own NEFF member. The whole step is guarded on the library count: an empty set emits nothing.

### Entry Point

```text
writeDefJson  @ libwalrus 0x152a0e0          ── builds def.json, sole caller
  └─ writeUCodeLibDefinitions  @ 0x1522d60   ── this body
       reads: module+0x190 (lib count guard), module+0x188 (lib list head)
       calls: NeffFileWriter::computeOutputPath, NeffFileWriter::addToBom
       then writeDefJson: bir::saveJsonFile(doc, dir/"ucode_lib.json", indent) @0x152c176
  └─ writeNEFFFeatures @ 0x15294b0           ── sets neff_feature_custom_ops bit when set nonempty
```

### Algorithm

```c
// NeffPackager::writeUCodeLibDefinitions(json& defJson, json& ucodeLibDoc,
//        NeffFileWriter& w, bir::Module const& m, fs::path const& dir)
// body @ libwalrus 0x1522d60 ; CONFIRMED — JSON key literals in disasm
void writeUCodeLibDefinitions(defJson, ucodeLibDoc, w, m, dir):
    if (m[+0x190] == 0) return                    // 0x1522d71 — lib-set count guard; empty => nothing
    json arr = json::array()
    for (lib : m.customOpLibs()):                 // head @ module+0x188, singly-linked nodes
        json funcs = json::array()
        for (fn : lib.functionNodes()):           // lib+0xA0 list ; node+8 name, node+0x28 sub_opcode
            funcs.push_back({ {"name",       fn.name},
                              {"opcode",     133},        // 0x85 CUSTOM_OP — HARD CONSTANT @0x1522f61
                              {"sub_opcode", fn.sub_opcode} })   // uint8 FunctionId byte
        json o
        o["library"]               = lib.file_name           // node+0x28 str   @0x152342c
        o["ulib_to_ucode_version"] = lib.ulib_to_ucode_version // node+0x48 str @0x15234b2
        o["ulib_to_isa_version"]   = lib.ulib_to_isa_version   // node+0x68 str @0x1523543
        o["is_builtin"]            = (bool)lib.is_builtin      // node+0x88 byte @0x15235aa
        o["cpu_id"]                = lib.cpu_id                // node+0x89 byte @0x1523619
        o["total_cpus"]            = lib.total_cpus            // node+0x8A byte @0x152367d
        o["functions"]             = funcs
        arr.push_back(o)
        // stage the library .so itself as a NEFF BOM member:
        p = w.computeOutputPath(dir / lib.file_name)          // 0x15231f6
        w.addToBom(p, optional<string>{ lib.file_name })      // 0x1523319
    defJson["ucode_lib"]     = arr                 // embedded in def.json   (key @0x1523731)
    ucodeLibDoc["ucode_lib"] = arr                 // standalone doc -> saveJsonFile("ucode_lib.json")
```

### Encoding — ucode_lib.json schema

```json
{
  "ucode_lib": [
    {
      "library":               "<string>",
      "ulib_to_ucode_version": "<string>",
      "ulib_to_isa_version":   "<string>",
      "is_builtin":            true,
      "cpu_id":                0,
      "total_cpus":            8,
      "functions": [
        { "name": "<fnName>", "opcode": 133, "sub_opcode": 0 }
      ]
    }
  ]
}
```

Every JSON key is a string literal at a contiguous file-offset run in `libwalrus.so` (CONFIRMED by `strings -t x`): `library` @ `0x1c8664f`, `ulib_to_ucode_version` @ `0x1c86657`, `ulib_to_isa_version` @ `0x1c8666d`, `is_builtin` @ `0x1c86681`, `cpu_id` @ `0x1c8668c`, `total_cpus` @ `0x1c86693`, `ucode_lib` @ `0x1c8669e`, `ucode_lib.json` @ `0x1c866a8`. The short keys `name`/`opcode`/`sub_opcode` are SSO-inlined (strcpy'd into the json node, not pooled), which is why they do not appear in the string table.

> **QUIRK —** `opcode` is **always** the constant `133` (`0x85` = `CUSTOM_OP` header) — it is never read from the struct; the immediate is baked in at `0x1522f61`. The *per-function* discriminator is `sub_opcode`, the FunctionId byte. The nlohmann `value_t` tags confirm `opcode:133` is a **signed** integer (type 5) while `cpu_id`/`total_cpus`/`sub_opcode` are **unsigned** (type 6) and `is_builtin` is a bool (type 4).

### NEFF member placement

The custom-op contribution to the NEFF tar member list is `{ ucode_lib.json } ∪ { each lib.file_name.so }`. Each library binary is copied in as a separate PAX member, its archive path = `dir / lib.file_name`, with the original library name carried in the `optional<string>` source-name argument to `addToBom`. The `ucode_lib.json` document is added afterwards. In parallel, `writeNEFFFeatures` (`0x15294b0`) sets the `neff_feature_custom_ops` flag (mask bit `0x8`, emitted @ `0x1529dd8`) whenever the lib set is nonempty — the runtime reads that flag to know the NEFF needs the GPSIMD subsystem.

> **NOTE —** `writeUCodeLibDefinitions` writes the table **twice** by design: embedded in `def.json` (`ucode_lib` key) and as the standalone `ucode_lib.json` member. `writeDefJson` builds a fresh json object for the standalone copy, then `bir::saveJsonFile`s it and `addToBom`s it. Both copies share the schema above.

---

## The full staging-to-NEFF chain

```text
Frontend job
  findCustomOpDataDir()                       ── in-wheel  …/neuronxcc/data/custom_op/   (no env, no version)
  Path(dir)/"libbuiltincustomop_cpuN" .is_file()  ── stage the 8 builtins  (else "Custom op data directory not found")
        │
        ▼  (penguin -> BIR codegen)
CoreV2GenImpl::visitInstCustomOp                ── per custom-op call site
  addCustomOpLibFile(name, ulib2ucode, ulib2isa, is_builtin, cpu_id, total_cpus)  ── dedup + version guard
  id = allocateCustomOpFunctionId()             ── monotonic uint32 (per module)
  addCustomOpFunction(name, fnName, id)         ── functions[fnName] = (uint8)id
        │
        ▼  (NEFF packaging)
NeffPackager::writeUCodeLibDefinitions          ── guard module+0x190>0
  -> def.json["ucode_lib"] = [...]              ── embedded copy
  -> ucode_lib.json (standalone NEFF member)    ── {library, ulib_to_*_version, is_builtin, cpu_id,
  -> each cpuN.so as a separate NEFF member         total_cpus, functions:[{name,opcode:133,sub_opcode}]}
  -> writeNEFFFeatures sets neff_feature_custom_ops
```

The seam is clean: the Python layer owns *location* (no policy), codegen owns *binding* (FunctionId namespace + version recording), and the packager owns *serialization*. Version numbers flow through untouched from the user's library build; the compiler's only authority over them is the consistency guard.

---

## Adversarial verification of the five strongest claims

| # | Claim | Tag | How re-verified |
|---|---|---|---|
| 1 | `findCustomOpDataDir` has **no env probing** | CONFIRMED | `nm -D` on the module shows no `getenv`/`environ`/`PyOS_getenv` import; `strings` shows no `NEURON_*`/`AWS_*`/`getenv`. The 9 `_PATH`-regex hits were all false positives (`__path__`, `data_path`) — none is an env var. |
| 2 | `findCustomOpDataDir` has **no version/FunctionId logic** | CONFIRMED | Exhaustive `strings(1)`: the only domain nouns are `importlib`/`files`/`joinpath`/`data`/`custom_op`/`os`/`path`; no `ucode`/`isa`/`version`/`FunctionId` (the only "version" hits are Cython binary-version boilerplate). |
| 3 | `allocateCustomOpFunctionId` is a **monotonic `uint32`** | STRONG | The corpus exposes only a 6-byte PLT thunk (`jmp cs:off_…` @ `0x457fa0`/`0x603ec0`); the post-increment body is from the D-AC01/D-AC02 IDA-DB analysis, consistent with the truncate-to-byte `sub_opcode` cap of ≤256. Not re-derivable from the `.so` sidecars (thunk only) → STRONG not CONFIRMED. |
| 4 | `addCustomOpLibFile` is the **6-arg** sink | CONFIRMED | Mangled tail `…S6_S6_bhh` decodes to `(string,string,string,bool,uchar,uchar)`; matches the D-AC01 demangled signature. |
| 5 | `ucode_lib.json` schema keys + `opcode:133` | CONFIRMED | All JSON keys are contiguous string literals at `libwalrus` file offsets `0x1c8664f`–`0x1c866a8` (`strings -t x`); `opcode` is the immediate `133` at `0x1522f61`. |

> **CORRECTION (AC02-self) —** an initial grep `_PATH` over `FindCustomOpData` returned 9 hits and momentarily looked like an env-var leak. Inspection showed every hit is a substring of `__path__`/`data_path`/`path` — Python dunder and the local variable, not an environment variable. The no-env finding stands.

---

## Open items

- The precise `Frontend` control flow that selects the builtin `cpu0..7` set versus a user-supplied custom-op directory per `getArch` is **STRONG** (string-pool), not byte-traced — the `Frontend` body was not fully decompiled.
- The `from_json`/`to_json`/`addCustomOp*`/`allocate*` *bodies* survive in the corpus only as PLT thunks; their struct offsets and instruction sequences are grounded in the D-AC01/D-AC02 IDA-DB analysis cross-checked against the packager's live reads, and are marked STRONG where not independently re-derivable here.
- The per-lib `cpu_id`/`total_cpus`/`is_builtin` **byte values** for the eight shipped builtins (`cpu_id 0..7`, `total_cpus 8`, `is_builtin true`) are INFERRED from the file naming and the field types; they are filled in upstream by `visitInstCustomOp` and not statically pinned per lib in the wheel.

---

## Related Components

| Name | Relationship |
|---|---|
| `findCustomOpDataDir` | resolves the in-wheel `custom_op` dir; produces the directory the `Frontend` stages |
| `Frontend` (driver job) | existence-checks and stages the eight builtins into the BIR pipeline |
| `CoreV2GenImpl::visitInstCustomOp` | sole registrar — fills `CustomOpLibInfo`, allocates FunctionIds |
| `ModuleArtifactInfo` | the `unordered_map<string,CustomOpLibInfo>` registry + FunctionId counter |
| `NeffPackager::writeUCodeLibDefinitions` | serializes the registry to `ucode_lib.json` + stages the `.so` members |

## Cross-References

- [CUSTOM_OP Wire Byte-Layout (0x85 / 0x86)](customop-wire-layout.md) — where the `CustomOpFunctionId` byte (= `sub_opcode`) lands in the header word, and the `0x85`/`0x86` chain it discriminates
- [The GPSIMD CPUs: 8-core Xtensa ELF Layout](gpsimd-xtensa-layout.md) — the eight `cpuN.so` libraries this page stages, and why `total_cpus = 8`
- [The Bitonic SORT / TOPK Builtin Algorithm](bitonic-sort-topk.md) — the `is_builtin=true` kernels the eight builtins contain
- [The Custom-Op CPU ABI: extended_isa::sdk](customop-cpu-abi.md) — the ABI the staged libraries expose to the runtime
