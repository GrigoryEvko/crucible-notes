# ext-ISA Dispatch Tables and arch-id Scheme

> *All host-binary addresses on this page apply to `libnrtucode_extisa.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `7bb03bc42ce1530924a1797ec9d5e518a7ae5e44`, ELF64 x86-64 DYN, stripped, symtab/`nm -D` only, **no DWARF**, 9,656,488 B). `.text` (`@0x3180`) and `.rodata` (`@0xb000`) have VMA == file offset, so every `0x8…`/`0x92…` offset on this page is *both* an analysis VMA and a host file offset. `.data.rel.ro` (`@0x9348a0`) and `.data` (`@0x9350a0`) do **not** — subtract `0x1000` to get the file offset from the VMA (see the QUIRK in §5). Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — the five dispatch entry points, the three `.rodata` jump tables, the four `.data.rel.ro` handler-pointer tables, and the `arch_id − 6` index immediate are each re-derived byte-exact from `objdump -d` / `xxd` of the cited offsets, cross-checked against the IDA Hex-Rays decompile. · Part XI — GPSIMD / Q7 Microcode & ISA · [back to index](../index.md)*

## Abstract

`libnrtucode_extisa.so` ships device code for several Neuron silicon generations in one host library, and it must route an incoming microcode request — *"give me the ext-ISA library for this arch, library index N"* — to exactly one of 13 embedded Tensilica Xtensa ELF32 blobs (or one of 38 coretype-indexed memory-image structs). That routing is **entirely table-driven**: five short, near-leaf entry points each index a `.rodata` jump table to pick a code path, and the code path indexes a `.data.rel.ro` pointer table of trivial `{ptr,size}`-returning getter thunks. There is no `switch` the compiler could not have folded into a table, no string lookup, and no `dlopen` — the whole dispatch is `add`/`cmp`/`jmp *`/`call *`. Think of it the way a SelectionDAG `MatcherTable` or a syscall dispatch vector works: the entry point is a thin shim, the *decision* lives in the table, and the table is the artifact to reproduce.

The routing key for the two ext-ISA entry points (`get_ext_isa`, `get_num_ext_isa_libs`) is the **`arch_id`**, transformed to a jump-table index by `idx = arch_id − 6` (the immediate `add $0xfffffffa` = −6, confirmed byte-exact at two call sites). After a `cmp $0x17` in-range check (`idx ∈ [0,23]`, i.e. `arch_id ∈ [6,29]`), only **four** indices land on real handler tables: `idx 0 / 7 / 15 / 23` → `arch_id 6 / 13 / 21 / 29` → handler tables `T0 / T1 / T2 / T3`. Every other index falls through to a default error stub. This `{6,13,21,29}` set is the "scheme A" ext-isa coretype/arch param from the [overview](overview.md); it is **not** the `core->kind` "scheme B" `{2,9,17,25}`, and — as the in-place CORRECTION below records — it is **not** the `{5,12,20,28}` the firmware-side coretype keys would suggest. The off-by-one is real and the binary settles it.

A third entry point (`get_memory_image`) is a *different* dispatch on a different key: a 38-entry coretype table selecting a per-coretype struct, then an inner 4-way jump table selecting a "flavor" slot from the `NEURON_UCODE_FLAVOR` environment variable. A fourth (`get_hwdecode_table`) indexes a parallel 38-entry table of `{hwdecode, alt}` getter pairs. This page documents all five entry points, the three jump tables decoded from their verbatim self-relative bytes, the four handler-pointer tables, the getter `{ptr,size}` contract, and the `arch_id − 6` index math — everything a reimplementer needs to rebuild the routing layer without the blob *content* (owned by [q7-blobs](q7-blobs.md)) or the relocation machinery (owned by [microcode-loader](microcode-loader.md)).

For reimplementation, the contract is:

- **The five entry points and what each keys on** — two on `arch_id` (`idx = arch_id − 6`), two on a coretype id `[0,0x25]` (stride-16 pointer tables), and the shared `sub_8660` body the ext-ISA pair funnel into.
- **The `arch_id − 6` index math and the reachable `{6,13,21,29}` set** — the `add $0xfffffffa` / `cmp $0x17` immediates, and why only four of 24 indices route to real handlers.
- **The three `.rodata` jump tables** — `JT_EXT`@`0x920e18`, `JT_NUMLIBS`@`0x920e78`, `JT_MEMIMG`@`0x920e08` — as 32-bit self-relative displacements (`target = JT_base + s32`), decoded from their verbatim bytes.
- **The four `.data.rel.ro` handler tables** `T0..T3`@`0x934b00/10/50/90` — each a `{body_getter, hdr_getter}` pair per library (16 B/lib), and the `lib_idx*16` stride into them.
- **The getter `{ptr,size}` contract** — every getter is a 3-instruction leaf `lea blob,%rax; movq $size,(%rsi); ret`; SUNDA returns a real JSON manifest, the 12 Cayman headers return a 32-byte `{"dummy_message":"hello world"}` stub.
- **The `.data.rel.ro` VMA→file-offset delta** — `0x1000`; every `xxd` of a `T0..T3` / `MEMIMG` / `HWDECODE` table reads at `VMA − 0x1000`.

| | |
|---|---|
| **ext-ISA router (shared body)** | `sub_8660` @`0x8660` (`get_ext_isa` tail-jumps here with `arg4=0`) |
| **`nrtucode_get_ext_isa`** | @`0x87a0` — `xor %ecx,%ecx; jmp 0x8660` |
| **`nrtucode_get_num_ext_isa_libs`** | @`0x87b0` (index math @`0x87bc`) |
| **`nrtucode_get_memory_image`** | @`0x8490` — coretype × flavor dispatch |
| **`nrtucode_get_hwdecode_table`** | @`0x87f0` — coretype × sub `{0,1}` dispatch |
| **Index math** | `idx = arch_id − 6` (`add $0xfffffffa` @`0x870b`/`0x87bc`); `cmp $0x17` in-range |
| **Reachable arch_ids** | `{6, 13, 21, 29}` = SUNDA / CAYMAN(v3) / MARIANA(v4) / MARIANA_PLUS(v4_plus) |
| **`JT_EXT`** | `@0x920e18`, 24 × 4 B self-relative; default → `0x8789` (err) |
| **`JT_NUMLIBS`** | `@0x920e78`, 24 × 4 B; `arch6→1`, `{13,21,29}→4`, else err |
| **Handler tables** | `T0`@`0x934b00` (1 lib) · `T1`@`0x934b10` · `T2`@`0x934b50` · `T3`@`0x934b90` (4 libs each) |
| **`MEMIMG` / `HWDECODE`** | `@0x9348a0` (38 × `{kind,ptr}`) · `@0x934bd0` (38 × `{getter,alt}`) |
| **`.data.rel.ro` file-off delta** | `−0x1000` (VMA `0x9348a0` → file `0x9338a0`) |

---

## 1. The Five Entry Points and the Two Keys

### Purpose

The dispatch surface is five exported functions, and the first thing a reimplementer must internalize is that they split into **two key families that are not the same key**. The ext-ISA pair (`get_ext_isa`, `get_num_ext_isa_libs`) keys on `arch_id` and runs the `arch_id − 6` index math; the image/hwdecode pair (`get_memory_image`, `get_hwdecode_table`) keys on a coretype id in `[0,0x25]` and indexes a stride-16 pointer table directly with no `−6` bias. Conflating the two keys — or assuming `get_memory_image` shares the `arch_id` math — mis-routes every request. This section maps the five functions; §2 decodes the ext-ISA jump tables, §3 the handler tables and getter contract, §4 the two coretype dispatches.

### Entry Point

```text
nrtucode_get_ext_isa          (0x87a0)  ── xor %ecx,%ecx (arg4=image_flag=0); jmp 0x8660
  └─ sub_8660                 (0x8660)  ── SHARED ext-ISA router
       ├─ idx = arch_id − 6 ; cmp $0x17                    @0x870b/0x870f
       ├─ jmp *JT_EXT[idx]   (0x920e18)                    @0x8723
       │    ├─ arch6  → lea T0 (0x934b00)                  @0x8729
       │    ├─ arch13 → lea T1 (0x934b10)                  @0x8743
       │    ├─ arch21 → lea T2 (0x934b50)                  @0x8736
       │    ├─ arch29 → lea T3 (0x934b90)                  @0x8750
       │    └─ default → 0x8789 (eax=1, no out write)
       └─ rcx = Ti + lib_idx*16 ; call body_getter ; call hdr_getter   @0x8757
nrtucode_get_num_ext_isa_libs (0x87b0)  ── idx = arch_id − 6 ; jmp *JT_NUMLIBS[idx] (0x920e78)
nrtucode_get_memory_image     (0x8490)  ── coretype × flavor (env) ; MEMIMG @0x9348a0 ; JT_MEMIMG @0x920e08
nrtucode_get_hwdecode_table   (0x87f0)  ── coretype × sub{0,1} ; HWDECODE @0x934bd0 (stride 16)
```

### Algorithm

```c
// nrtucode_get_ext_isa(arch_id, lib_idx, out)            // 0x87a0
//   tail-jumps into the shared router with the 4th arg (image_flag) cleared:
function get_ext_isa(arch_id, lib_idx, out):
    return sub_8660(arch_id, lib_idx, out, /*image_flag=*/0)   // xor %ecx,%ecx @0x87a0; jmp 0x8660

// sub_8660 — the shared ext-ISA router (arch_id in %r15d, lib_idx in %r14, out in %rbx)
function ext_isa_router(arch_id, lib_idx, out, image_flag):
    idx = arch_id - 6                                    // add $0xfffffffa,%r15d  @0x870b
    if (u32)idx > 0x17:                                  // cmp $0x17 ; ja default  @0x870f
        return 1                                         // UNKNOWN_CORE
    handler = JT_EXT[idx]                                // jmp *(0x920e18 + s32[idx])  @0x8723
    switch handler:
      case arch6 : Ti = &T0   // 0x934b00 (lea @0x8729)
      case arch13: Ti = &T1   // 0x934b10 (lea @0x8743)
      case arch21: Ti = &T2   // 0x934b50 (lea @0x8736)
      case arch29: Ti = &T3   // 0x934b90 (lea @0x8750)
      default    : return 1                              // → 0x8789, eax=1, out untouched
    return invoke_getters(Ti, lib_idx, out)              // shared tail @0x8757
```

> **NOTE —** `get_ext_isa` is a *pure shim*: its only instruction before the tail-jump is `xor %ecx,%ecx` (`@0x87a0`), which zeroes the 4th argument (`image_flag`). Because the export always passes `0`, the alternate `image_flag != 0` path inside `sub_8660` is never reached through this export — whether any other caller sets it is **LOW confidence / not traced** (no other export tail-jumps into `0x8660`). A reimplementer can treat the ext-ISA path as `image_flag == 0` unconditionally and lose no observable behavior. CONFIDENCE: **HIGH** (the shim is two instructions; the unreached path is flagged, not invented).

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `nrtucode_get_ext_isa` | `0x87a0` | Shim → `sub_8660` with `image_flag=0`; fills the `0x20`-B out struct | HIGH |
| `sub_8660` | `0x8660` | Shared ext-ISA router: `idx=arch_id−6`, `JT_EXT`, `T0..T3`, getter pair | HIGH |
| `nrtucode_get_num_ext_isa_libs` | `0x87b0` | `idx=arch_id−6`; `JT_NUMLIBS` → `*out ∈ {1,4}` or err | HIGH |
| `nrtucode_get_memory_image` | `0x8490` | Coretype × flavor (env) dispatch; `MEMIMG` + `JT_MEMIMG` | HIGH |
| `nrtucode_get_hwdecode_table` | `0x87f0` | Coretype × sub `{0,1}`; `HWDECODE` stride-16 table | HIGH |

### Considerations

The two ext-ISA entry points share the *same* index math but **not** the same jump table: `get_ext_isa` uses `JT_EXT`@`0x920e18` (routing to handler tables), `get_num_ext_isa_libs` uses `JT_NUMLIBS`@`0x920e78` (routing to a `mov $imm` that returns the library count). The tables are deliberately parallel — both are 24 entries, both default to an error stub, both are reachable only at `idx 0/7/15/23` — so a loader that queries the count first (`get_num_ext_isa_libs`) and then iterates `lib_idx` against `get_ext_isa` always agrees on which arch_ids are valid. A reimplementer that hard-codes "4 libraries" without consulting `JT_NUMLIBS` will over-read SUNDA, which has exactly **1**.

---

## 2. The `arch_id − 6` Index Math and the ext-ISA Jump Tables

### Purpose

This is the heart of the page: how a numeric `arch_id` becomes a routed code path. The mechanism is a textbook compiler-emitted dense jump table — a bounds check, a self-relative table lookup, and an indirect jump — but the *constant* (`−6`) is the one fact the firmware-side keys get wrong, so it is decoded from the raw immediate, not paraphrased.

### Algorithm — the index transform

```c
// Identical math at both ext-ISA entry points (sub_8660 @0x870b; get_num_ext_isa_libs @0x87bc):
idx = arch_id - 6;            // add $0xfffffffa  (0xfffffffa = -6 as s32)
if ((u32)idx > 0x17)         // cmp $0x17  → unsigned; folds idx<0 and idx>23 into one ja
    goto default_error;
// table is self-relative: each entry is a signed-32 displacement from the table base.
target = JT_base + (s32)JT_base[idx];
goto *target;                // jmp *%rdx
```

The immediate is `41 83 c7 fa` at `0x870b` (`add $0xfffffffa,%r15d`) and `83 c7 fa` at `0x87bc` (`add $0xfffffffa,%edi`) — the literal byte `fa` is two's-complement `−6`. The in-range check is `41 83 ff 17` / `83 ff 17` (`cmp $0x17`), and because the compare is unsigned (`jae`/`ja`), a negative `idx` (i.e. `arch_id < 6`) wraps to a huge `u32` and is rejected by the same branch. There is no separate `idx < 0` test.

### The two ext-ISA tables, decoded

Both `JT_EXT` and `JT_NUMLIBS` are 24 × 4-byte signed-32 self-relative displacements; the absolute target is `JT_base + (s32)entry`. Decoding the verbatim bytes (`xxd` of `0x920e18` and `0x920e78`) gives:

| arch_id | idx | `JT_EXT` entry (verbatim) | → target | meaning | `JT_NUMLIBS` → `*out` |
|---|---|---|---|---|---|
| 6 | 0 | `0d 79 6e ff` | `0x8725` | `lea T0` (SUNDA) | `0x87db` → **1** |
| 13 | 7 | `27 79 6e ff` | `0x873f` | `lea T1` (CAYMAN v3) | `0x87d4` → **4** |
| 21 | 15 | `1a 79 6e ff` | `0x8732` | `lea T2` (CAYMAN v4) | `0x87d4` → **4** |
| 29 | 23 | `34 79 6e ff` | `0x874c` | `lea T3` (CAYMAN v4_plus) | `0x87d4` → **4** |
| *all others* | — | `71 79 6e ff` | `0x8789` | default: `eax=1`, no `out` write | `6d 79 6e ff` → `0x87e5` (err) |

The decode arithmetic is exact: e.g. `s32(0xff6e790d) = −0x9192f3`, and `0x920e18 − 0x9192f3 = 0x8725`. Twenty of the 24 `JT_EXT` slots hold the identical default displacement `0xff6e7971` (→ `0x8789`); only the four reachable indices differ. `JT_NUMLIBS` is the same shape: index 0 alone holds `0xff6e7963` (→ `0x87db`, `mov $1`), indices 7/15/23 hold `0xff6e795c` (→ `0x87d4`, `mov $4`), and all 20 others hold `0xff6e796d` (→ `0x87e5`, error).

> **QUIRK —** the four reachable arch_ids are **arithmetically spaced by 8** (`6, 13, 21, 29` → idx `0, 7, 15, 23`), not contiguous. The dense 24-entry table therefore wastes 20 slots on the default stub purely to keep the lookup a single `jmp *(base + idx*4)` instead of a sparse compare ladder. A reimplementer is free to collapse this to a 4-case switch — the binary's 24-wide table is a compiler space/time trade, not a semantic requirement. The *spacing* (`+7`/`+8`/`+8`) is the structural tell that the four silicon generations were assigned arch_ids out of a wider enum (`v2`/`v3`/`v4`/`v4_plus`), with gaps reserved for the intervening NEFF/arch-type encodings. CONFIDENCE: **HIGH** (all 24 entries decoded; only four non-default).

### The result map

```c
// get_num_ext_isa_libs result, after the JT_NUMLIBS dispatch:
//   arch6  → *out = 1     (SUNDA: one library, the real manifest + 54 KB Xtensa blob)
//   arch13 → *out = 4     (CAYMAN v3:      four Xtensa blobs)
//   arch21 → *out = 4     (CAYMAN v4:      four Xtensa blobs, byte-identical to arch29)
//   arch29 → *out = 4     (CAYMAN v4_plus: four Xtensa blobs, byte-identical to arch21)
//   else   → return 1, *out left at its caller-zeroed value
```

> **CORRECTION (UC-DISPATCH) —** an earlier scaffold (echoed by `reference/binary-layout.md` and the SCAN-10 seed) stated the reachable arch_ids are `{5, 12, 20, 28}` with `idx = arch_id − 5`. **The binary computes `idx = arch_id − 6`**, so the real, code-reachable arch_ids are **`{6, 13, 21, 29}`**. This is byte-proven at *both* ext-ISA entry points: `41 83 c7 fa` (`add $-6`) at `0x870b` and `83 c7 fa` at `0x87bc`, with `JT_EXT`/`JT_NUMLIBS` reachable only at indices `0/7/15/23`. The seed's `{5,12,20,28}` is a consistent `+1` off-by-one against the *firmware coretype keys*; do not confuse the ext-ISA `arch_id` (`{6,13,21,29}`) with either the firmware coretype keys (`{5,12,20,28}`) or the `core->kind` scheme B (`{2,9,17,25}`). The handler-table mapping (idx `0/7/15/23` → `T0/T1/T2/T3`) and `libnrt`'s `CSWTCH.113 = [6,13,21]` (`@0x86ada8`) both corroborate `{6,13,21,29}`. CONFIDENCE: **HIGH** (index immediate + both jump tables decoded byte-exact).

---

## 3. The Handler Tables and the Getter `{ptr,size}` Contract

### Purpose

Once `JT_EXT` routes an arch_id to a handler table `T0..T3`, the router indexes that table by `lib_idx` and invokes a *pair* of getter thunks. This section documents the 16-byte-per-library layout, the shared tail that calls the pair, and the uniform `{ptr,size}` contract every getter obeys — the contract that hands a `{Xtensa-ELF blob, opcode-manifest}` pair back to the caller.

### Algorithm — the shared getter-invocation tail

```c
// sub_8660 tail @0x8757, after Ti (handler table) is selected in %rcx:
function invoke_getters(Ti, lib_idx, out):              // out is the caller's 0x20-byte struct
    rcx = Ti + lib_idx * 16                             // add %r14,%rcx (r14 = lib_idx<<4)  @0x8757
    body_getter = *(void(**)())(rcx + 0)               // mov (%rcx),%rdx        @0x875a
    if body_getter == NULL: return 3                   // MISSING_IMAGE
    hdr_getter  = *(void(**)())(rcx + 8)               // mov 0x8(%rcx),%r14     @0x8767
    if hdr_getter == NULL:  return 3
    body_getter(/*%rdi=*/&out->body_ptr, /*%rsi=*/&out->body_size)  // call *%rdx  @0x8777
    hdr_getter (/*%rdi=*/&out->hdr_ptr,  /*%rsi=*/&out->hdr_size)    // call *%r14  @0x8784
    return 0                                            // SUCCESS

// The caller-allocated output struct (0x20 bytes). The tail passes each getter a
// (ptr_dest, size_dest) pair: body_getter(&out+0x00, &out+0x08); hdr_getter(&out+0x10, &out+0x18).
struct ext_isa_out {
    void* body_ptr;    // +0x00   Xtensa ELF32 blob     ← *(%rdi) of body_getter
    u64   body_size;   // +0x08   blob byte size        ← *(%rsi) of body_getter
    void* hdr_ptr;     // +0x10   opcode-manifest JSON  ← *(%rdi) of hdr_getter
    u64   hdr_size;    // +0x18   manifest byte size    ← *(%rsi) of hdr_getter
};
```

The stride is `lib_idx * 16` because each library occupies a `{body_getter, hdr_getter}` 8-byte pointer pair. The tail passes `&out+0x00` to the body getter and `&out+0x10` to the header getter (the `lea 0x8(%rbx)`/`lea 0x10(%rbx)` setup at `0x8770`/`0x8779`), so each getter writes its `{ptr, size}` into the matching half of the out struct.

### The getter thunk contract

Every getter is a **3-instruction leaf** with the identical shape — the structural ground truth that the 192 unnamed getters in this binary are all the same kind of object:

```asm
; SUNDA body getter @0xaf10 (args: %rdi = &out.ptr, %rsi = &out.size):
af14: 48 8d 05 45 67 91 00   lea    0x916745(%rip),%rax        ; %rax = 0x921660 (blob)
af1b: 48 89 07               mov    %rax,(%rdi)                 ; *out.ptr  = blob
af1e: 48 c7 06 08 d3 00 00   movq   $0xd308,(%rsi)             ; *out.size = 0xd308 = 54024
af25: c3                     ret
; SUNDA hdr getter @0xaef0:  lea 0x920fa0,%rax ; mov %rax,(%rdi) ; movq $0x6c0,(%rsi) ; ret  (real JSON)
; arch13 lib0 body @0x5320:  lea 0x5b0cc0,%rax ; mov %rax,(%rdi) ; movq $0xa260,(%rsi); ret
; arch13 lib0 hdr  @0x5300:  lea 0x5b0ca0,%rax ; mov %rax,(%rdi) ; movq $0x20,(%rsi)  ; ret  (dummy hdr)
```

### Handler tables decoded

The four `.data.rel.ro` tables, dumped at file offset `VMA − 0x1000` and resolved through their `R_X86_64_RELATIVE` addends, give the per-library `{body_getter, hdr_getter}` pairs and (via each getter) the `{blob, size}` they return:

| Table | arch_id | lib | body getter → `{blob, size}` | hdr getter → `{manifest, size}` |
|---|---|---|---|---|
| `T0`@`0x934b00` | 6 (SUNDA) | 0 | `0xaf10` → `{0x921660, 0xd308}` (ELF32, EM=0x5e) | `0xaef0` → `{0x920fa0, 0x6c0}` (**real** JSON) |
| `T1`@`0x934b10` | 13 (v3) | 0..3 | `0x5320/52e0/52a0/5260` → sizes `{0xa260, 0xf5c, 0x1500, 0x6974}` | `0x5300/52c0/5280/5240` → `{·, 0x20}` (dummy) |
| `T2`@`0x934b50` | 21 (v4) | 0..3 | `0x43a0/4360/4320/42e0` → sizes `{0xa260, 0xf5c, 0x1500, 0x6974}` | `0x4380/4340/4300/42c0` → `{·, 0x20}` (dummy) |
| `T3`@`0x934b90` | 29 (v4_plus) | 0..3 | `0x3420/33e0/33a0/3360` → sizes `{0xa260, 0xf5c, 0x1500, 0x6974}` | `0x3400/33c0/3380/3340` → `{·, 0x20}` (dummy) |

The verbatim `T0` bytes (file `0x933b00`) are `10 af 00 00 …` (`0xaf10`) followed by `f0 ae 00 00 …` (`0xaef0`); `T1` is `20 53 00 00 … 00 53 00 00 …` (`0x5320`, `0x5300`); the `R_X86_64_RELATIVE` relocation supplies the high bits. The three Cayman tables carry **identical per-library sizes** `{0xa260, 0xf5c, 0x1500, 0x6974}`, and a `memcmp` of the pointed-at blobs shows `T2.lib*` is **byte-identical** to `T3.lib*` (arch21 ≡ arch29) while `T1` (arch13) differs — the structural evidence behind the `MARIANA_PLUS = MARIANA-silicon-running-v4_plus-ucode` binding ([overview](overview.md) §4).

> **GOTCHA —** only the **SUNDA** header getter (`0xaef0` → `0x920fa0`, 1728 B) returns a real opcode manifest (`{"library":"all.stripped.so","ulib_to_ucode_version":"1.21.1.0",…}`, 17 ops). All **12 Cayman** header getters point `0x20` bytes *before* their ELF and return the 32-byte stub `{"dummy_message": "hello world"}` (verbatim at `0x5b0ca0`). A reimplementer that trusts the `get_ext_isa` header for Cayman will parse a placeholder — the real Cayman opcode→library manifest is sourced from `libnrt`'s `ext_isa_ucode_lib_def`, **not** from this provider ([microcode-loader](microcode-loader.md), [q7-blobs](q7-blobs.md)). The header getter still returns SUCCESS with a valid 32-byte JSON, so the failure is silent. CONFIDENCE: **HIGH** (manifest + stub dumped verbatim; the 12 getters all point at a 32-byte region).

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `sub_8660` tail | `0x8757` | `Ti + lib_idx*16`; load + null-check + call both getters | HIGH |
| SUNDA body / hdr getter | `0xaf10` / `0xaef0` | `{0x921660,0xd308}` blob / `{0x920fa0,0x6c0}` real JSON | HIGH |
| `T1`/`T2`/`T3` body getters | `0x52xx`/`0x43xx`/`0x33xx` | Per-arch blob `{ptr,size}` leaves (4 each) | HIGH |
| `T1`/`T2`/`T3` hdr getters | `0x52xx`/`0x43xx`/`0x33xx` | `{·, 0x20}` dummy-header leaves (4 each) | HIGH |

---

## 4. The Coretype Dispatches — `get_memory_image` and `get_hwdecode_table`

### Purpose

The remaining two entry points dispatch on a **coretype id** in `[0,0x25]` (38 values), not on `arch_id`, and they index 38-entry `.data.rel.ro` tables with a plain `*16` stride and *no* `−6` bias. `get_memory_image` adds an inner "flavor" selection driven by the `NEURON_UCODE_FLAVOR` environment variable; `get_hwdecode_table` selects one of two sub-slots per coretype. They are documented together because they share the coretype key and the stride-16 table shape.

### Algorithm — `get_hwdecode_table` (the simpler one)

```c
// nrtucode_get_hwdecode_table(coretype, sub, a3, a4)     // 0x87f0
function get_hwdecode_table(coretype, sub, a3, a4):
    if (u32)coretype > 0x25: return 1                    // cmp $0x25  @0x87f5
    rdi = HWDECODE + coretype * 16                       // shl $0x4 ; lea 0x934bd0 ; add  @0x87fc/0x8800/0x8807
    if sub == 0: getter = *(rdi + 0)                     // hwdecode slot
    else if sub == 1: getter = *(rdi + 8)                // add $0x8  @0x8818 ; alt slot
    else: return 1                                       // sub > 1
    if getter == NULL: return 2                          // UNKNOWN_IMAGE
    getter(a3, a4)                                       // call *%r8  @0x882b
    return 0
```

`HWDECODE`@`0x934bd0` is 38 × `{hwdecode_getter, alt_getter}` (16 B/slot). Most slots are NULL (`xxd` of file `0x933bd0` shows idx 0 = all zeros); only the per-arch hwdecode-override groups are populated — coretype `7-10` (arch13 group, e.g. idx 7 = `{0x5220, 0x5200}`), `15-18` (arch21), `23-26` (arch29), and `31-32` (SUNDA/special). The populated getters live in the *same* `0x5xxx`/`0x4xxx`/`0x3xxx` code regions as the matching ext-ISA getters, confirming the per-arch grouping. The payload format (e.g. `0x5220` → `{0x5a7a60, 0x400}`, the 1 KB opcode-CAM) is the [microcode-loader](microcode-loader.md) / hwdecode page's scope.

### Algorithm — `get_memory_image` (coretype × flavor)

```c
// nrtucode_get_memory_image(coretype, flavor_sel, has_flavor, out_ptr, out_size, …)   // 0x8490
function get_memory_image(coretype, flavor_sel, has_flavor, out):
    if (u32)coretype > 0x25: return 1
    flavor = has_flavor ? flavor_sel
                        : map_env(getenv("NEURON_UCODE_FLAVOR"))   // @0x9201d1
            // "debug"/"DEBUG" → 2 ; "test"/"TEST" → 3 ; else / NULL → 1
    entry = MEMIMG[coretype]                              // {u64 kind, struct_ptr}  @0x9348a0, stride 16
    if entry.struct_ptr == NULL: return 2
    // inner 4-way flavor selector via JT_MEMIMG @0x920e08:
    slot = JT_MEMIMG[flavor]  →  one of {+8, +0x10, +0x18, +0x20} of the struct's fnptr list
    getter = entry.struct_ptr->fnptr[slot]
    if getter == NULL: return 3
    getter(out_ptr, out_size)                            // e.g. 0x66a0 → {0x917010, 0x8f20}
    return 0
```

`MEMIMG`@`0x9348a0` is 38 × `{u64 kind, struct_ptr}`; idx 0 (file `0x9338a0`) is `{kind=1, struct_ptr=0x9350a8}`, and that struct (file `0x9340a8`) is `{kind=1, fnptr[4]={0x66a0, 0x6680, 0x6660, 0x6640}}` — the four `NEURON_UCODE_FLAVOR` slots. The inner `JT_MEMIMG`@`0x920e08` (4 × 4 B → `0x85c7/0x8606/0x85e0/0x85ee`) is the flavor selector. The `kind` field takes values `{0,1,2,3,6}` across the 38 entries (idx `13/21/29` are `kind=6`); the exact semantics of each `kind` are **not decoded** (only the `{kind, ptr}` layout and the 4-slot flavor struct are proven). CONFIDENCE: **HIGH** (layout), **LOW** (`kind` semantics).

### Dispatch-Dimension Table

The five entry points and their tables, summarized by axis rather than by exhaustive per-coretype enumeration:

| Entry point (symbol @ addr) | Keyed by | Table location | Targets | Confidence |
|---|---|---|---|---|
| `get_ext_isa` @`0x87a0` → `sub_8660` @`0x8660` | `arch_id` (`idx=arch_id−6`, `cmp 0x17`) | `JT_EXT`@`0x920e18` (24×4B) → `T0..T3`@`0x934b00/10/50/90` | 4 arch_ids → 13 getter pairs `{body,hdr}` | HIGH |
| `get_num_ext_isa_libs` @`0x87b0` | `arch_id` (`idx=arch_id−6`) | `JT_NUMLIBS`@`0x920e78` (24×4B) | `arch6→1`, `{13,21,29}→4`, else err | HIGH |
| `get_memory_image` @`0x8490` | coretype `[0,0x25]` × flavor (env) | `MEMIMG`@`0x9348a0` (38×`{kind,ptr}`) + `JT_MEMIMG`@`0x920e08` (4×4B) | 38 coretypes × 4 flavor slots | HIGH (layout) / LOW (`kind`) |
| `get_hwdecode_table` @`0x87f0` | coretype `[0,0x25]` × sub `{0,1}` | `HWDECODE`@`0x934bd0` (38×`{getter,alt}`) | populated groups 7-10/15-18/23-26/31-32 | HIGH |

---

## 5. The `.data.rel.ro` File-Offset Delta

### Purpose

Every handler/coretype table on this page lives in `.data.rel.ro` or `.data`, and a reimplementer reading the raw bytes with `xxd`/`dd` must translate the VMA to a file offset correctly — a translation that differs from the `.text`/`.rodata` convention used everywhere else in Part XI.

### The delta

> **QUIRK —** `.rodata` (where `JT_EXT`/`JT_NUMLIBS`/`JT_MEMIMG` and all the embedded blobs live) has **VMA == file offset** (`@0xb000` in both), so `xxd -s 0x920e18` reads the jump table directly. But `.data.rel.ro` (`T0..T3`, `MEMIMG`, `HWDECODE`) is at **VMA `0x9348a0` / file offset `0x9338a0`** — a **`0x1000` delta** (`readelf -SW`: `Address 9348a0`, `Off 9338a0`). To `xxd` any pointer table on this page, read at **`VMA − 0x1000`**: `T0`@`0x934b00` → `xxd -s 0x933b00`; `MEMIMG`@`0x9348a0` → `xxd -s 0x9338a0`; `HWDECODE`@`0x934bd0` → `xxd -s 0x933bd0`. Over-generalizing the `.rodata` "VMA==fileoffset" rule to `.data.rel.ro` reads `0x1000` bytes early — into the tail of `.rodata` — and produces spurious "wrong pointer" findings. The `.data` section (`@0x9350a0` / file `0x9340a0`, the `MEMIMG` flavor structs) carries the same `0x1000` delta. CONFIDENCE: **CERTAIN** (`readelf -SW` section headers).

This is the local instance of the general Neuron-runtime caveat: only `.text` and `.rodata` are loaded at their file offset; writable sections are page-aligned to a higher VMA than their on-disk position, and the delta is the difference the linker inserted for the writable segment.

---

## Related Components

| Name | Relationship |
|---|---|
| `nrtucode_get_ext_isa` / `get_num_ext_isa_libs` (exports) | The two ext-ISA entry points whose `arch_id−6` routing this page decodes |
| `sub_8660` (router body) | The shared ext-ISA dispatcher; selects `T0..T3` and invokes the getter pair |
| `T0..T3` / `MEMIMG` / `HWDECODE` (`.data.rel.ro`) | The four handler-pointer tables + two coretype tables the entry points index |
| 192 getter thunks (`0x33xx`..`0x5xxx`, `0x66xx`, `0x99xx`, `0xaexx`) | The `{ptr,size}` leaves the tables route to; blob *content* is [q7-blobs](q7-blobs.md) |
| `libnrt` `CSWTCH.113 = [6,13,21]` (`@0x86ada8`) | The consumer-side `arch_type-2 → coretype` map that feeds these arch_ids |

## Cross-References

- [arch_id ↔ Silicon and Coretype Numbering](overview.md#the-two-coretype-schemes) — scheme A `{6,13,21,29}` vs scheme B `{2,9,17,25}`; the SUNDA/CAYMAN/MARIANA binding
- [The Microcode Loader (RELA + FLIX, UCPL)](microcode-loader.md) — what `ll_create` does with the `{body,hdr}` pair this dispatch returns; the hwdecode payload formats
- [The ext-ISA Provider (`nrtucode_*` API)](extisa-provider.md) — the 52-export surface and the `get_*` family these five entry points belong to
- [Overview: the Q7 GPSIMD Engine](overview.md) — the provider→loader→device map and where this routing sits in it
- [The 13 Q7 Microcode Blobs](q7-blobs.md) — the ELF32 blobs the getter thunks return; the SUNDA manifest vs the Cayman stubs
- [back to index](../index.md)
