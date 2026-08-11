# Firmware-Image Catalog Capstone

The **image-lane capstone**: the single completeness-checked index of *every*
GPSIMD firmware-image getter in `libnrtucode_internal.so`. The
[accessor index](./image-catalog-index.md) (#750) is the flat per-getter matrix;
this page is its **indexed, byte-grounded successor** — it organizes the same
getters into the `(generation × engine × flavor × region)` hierarchy, adds the
inventory roll-up, the container model, the three resolvers reproduced as
annotated C, the catalog-level evolution roll-up, and a closed completeness
check.

Everything below is grounded against the shipped binary
(`nm` / `objdump -d` / `readelf -SW` / `dd` / `python hashlib`). The page default is
`[HIGH/OBSERVED]`; claims that depart from it carry an explicit tag. Where a figure is
derived directly from the binary rather than carried from a sibling page, it is marked
**[binary-derived]**.

> **Scope vs siblings.** The exhaustive per-getter `symbol / VA / img-ptr / size`
> dump is the [accessor index](./image-catalog-index.md). The per-blob byte
> semantics live on the per-generation pages
> ([`sunda-pool`](./sunda-pool.md), [`sunda-sp-remaining`](./sunda-sp-remaining.md),
> [`sunda-arch5-extisa`](./sunda-arch5-extisa.md),
> [`cayman-act`](./cayman-act.md), [`mariana-act`](./mariana-act.md),
> [`mariana-plus-act`](./mariana-plus-act.md), [`maverick-act`](./maverick-act.md)),
> the [EXTISA inventory](./extisa-inventory.md), and the
> [PROF CAM/TABLE formats](./prof-cam-table-formats.md). The kernel *content* per
> image is the [cross-gen kernel_info matrix](./cross-gen-kernel-info-matrix.md).
> Generation framing is the
> [codename / generation map](../generations/codename-generation-map.md) and the
> [Maverick profile](../generations/maverick-profile.md). The resolver-mechanism
> deep dive is the Part-8 page
> [runtime/image-hwdecode-resolvers.md](../runtime/image-hwdecode-resolvers.md).
> This capstone **indexes the containers, not the kernels.**

---

## 0. Target binary

| Field | Value |
|---|---|
| Library | `libnrtucode_internal.so` |
| Path | `…/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/custom_op/c10/lib/` |
| sha256 | `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b` |
| Size | `10,276,288` B |
| BuildID | `9cbf78c6f59cdb5839f155fdb2113bbe51e585fd` |
| Class | ELF64 x86-64 DYN, **NOT stripped** — the 5-generation symbol "twin" |

The container hash **MATCHES** every image/runtime anchor. This is the only
shipped binary that carries all five generations of getters; the front lib
`libnrtucode.so` (`06d3f0b1…`, stripped) ships only four (no MAVERICK), and the
EXTISA container `libnrtucode_extisa.so` (`dc00763d…`, cited) holds the standalone
SUNDA EXTISA device ELF. `[HIGH/OBSERVED]`

### Section / offset model (so every address below is reproducible)

`readelf -SW` confirms three layout deltas. Getters resolve **VA-aware**; confirm
per-section before carving. `[HIGH/OBSERVED]`

| Section | VA | File off | Delta (VA − fileoff) | Holds |
|---|---|---|---|---|
| `.rodata` | `0x46b0` | `0x46b0` | **0** (identity) | the 225 firmware blobs (`<NAME>_get.data`) |
| `.data.rel.ro` | `0x9b8cf0` | `0x9b6cf0` | `0x2000` | `image_list` (`0x9b8d20`), the five `*_libs`, `hwdecode_table_list` (`0x9b9090`) |
| `.data` | `0x9ba4a8` | `0x9b74a8` | `0x3000` | the per-(gen,engine) descriptor arrays |

> **GOTCHA.** Only `.rodata` is identity-mapped, so a getter's `lea`-target
> img-ptr is *both* the runtime VA *and* the file offset — `dd skip=$imgptr
> count=$size` (or `file[imgptr:imgptr+size]`) carves the exact blob. The
> resolver tables are **not** identity-mapped (`image_list`/`*_libs` delta
> `0x2000`; descriptors delta `0x3000`) — subtract the delta before `xxd`/`dd`
> or you read the wrong struct. The `tbl` pointer field inside each `image_list`
> slot is a load-time relocation (`.data.rel.ro`), so the static file shows
> `tbl=0x0` while the **`count`** field is statically valid.

---

## 1. The catalog verdict, up front

| Quantity | Value | Grounding |
|---|---|---|
| Local (`t`) image getters | **386** | `nm \| rg ' t ' \| rg '_get$'` = 386 `[binary-derived]` |
| Weak-undef (`w`) getters | **2** | the SUNDA EXTISA `SO`/`JSON` placeholders `[binary-derived]` |
| Total accessor symbols | **388** (386 in-lib bodies + 2 container-resolved) | §6 |
| Real getters (`size > 0`) | **225** | binary stub-parse `[binary-derived]` |
| Zero-cursor getters (`size == 0`) | **161** | binary stub-parse `[binary-derived]` |
| Distinct img-ptr addresses | **225** (== the real count) | every cursor reuses a real boundary address `[binary-derived]` |
| Resolvers | **3** (`get_memory_image`, `get_ext_isa`, `get_hwdecode_table`) | all symbols present at documented VAs `[binary-derived]` |

> **The grand total is 386**, and it closes three independent ways (§3, §8). The
> task brief's loose "~266/386" reflects two valid counts of the *same* table —
> 386 is all getters, 324 is the `get_memory_image`-reachable subset (base+DKL),
> 288 is the base-region subset; no single resolver produces 266. This page uses
> **386** as the catalog grand total throughout.

> **NOTE — no count correction.** The earlier survey's 386 and the
> [accessor index](./image-catalog-index.md)'s 386 both match the binary
> exactly; no in-place CORRECTION to the grand total is needed. The one
> *refinement* this capstone adds is the `.a` member arithmetic (§4): the archive
> is **435 = 420 image members + 15 framework objects**, where the 420 is the
> "124×3 + 48 + 0" figure and the 15 framework `.c.o` objects were previously
> folded into the headline number without being itemized.

---

## 2. The naming grammar and the getter stub (the index key)

Every getter symbol decodes as:

```
<GEN>_<CLS>_<ENGINE>_<VARIANT>_<REGION>_get
```

| Field | Values |
|---|---|
| `GEN` | `SUNDA` \| `CAYMAN` \| `MARIANA` \| `MARIANA_PLUS` \| `MAVERICK` |
| `CLS` | `NX` (NX-core instruction sequencer — the `S:` SEQ engine) \| `Q7` (Xtensa Q7 / GPSIMD compute core — the `P%i:` POOL core) |
| `ENGINE` | `ACT` \| `DVE` \| `PE` \| `POOL` \| `SP` (NX side); `POOL` only (Q7 side) |
| `VARIANT` | `RELEASE` (SUNDA only) \| `DEBUG` \| `PERF` \| `TEST` \| `PROF` (→ CAM/TABLE) \| `DYNAMIC_KERNEL_LOAD_{DEBUG,PERF,TEST}` (Q7_POOL only) \| `PERF` over `EXTISA_0..3` (Q7_POOL only) |
| `REGION` | `IRAM` (code) \| `DRAM` (data + log strings) \| `SRAM` \| `EXTRAM`; or `CAM`/`TABLE` (under PROF); or `EXTISA_0..3_{SO,JSON}` (under the Q7_POOL EXTISA variant) |

### The stub (binary-exact, verified instruction-exact on 7 getters §6)

```asm
<NAME>_get:
    lea  <blob>(%rip),%rax     ; image pointer == nm <NAME>_get.data symbol
    mov  %rax,(%rdi)           ; *out_ptr  = blob
    movq $<size>,(%rsi)        ; *out_size = size
    ret
```

i.e. `void <NAME>_get(void** out_ptr, size_t* out_size)`. Because `.rodata` is
identity-mapped, each `<NAME>_get.data` address is **both** the VA and the file
offset of the blob; carve `= file[ptr : ptr+size]`. `[HIGH/OBSERVED]`

> **QUIRK — the cursor stub.** A `(cursor, size 0)` getter is still a real,
> non-NULL function: it returns a valid pointer at the *next blob's* start (the
> contiguous-layout boundary cursor) with `size == 0`. The resolver treats this
> as "empty-but-present" (`ret 0`, `*out_len == 0`) — distinct from a NULL
> getter slot, which means "variant not linked" (`ret 3`). This is why 161
> zero-cursor getters reuse the same **225 distinct** addresses as the real ones.

---

## 3. The getter index — per `(gen × engine × flavor)` cell

Computed by parsing all 386 getter stubs out of the binary
(`nm` + `objdump -d` of the getter `.text` span). Each cell shows the getter
count and the `flavor:region-count` breakdown. The exhaustive per-getter
`VA/img-ptr/size` is the [accessor index](./image-catalog-index.md);
the per-block `.text` VA spans are in §3.6.

### 3.1 SUNDA — total **24**  `[binary-derived]`

| Cell | Getters | Flavor:region |
|---|---|---|
| `NX_ACT` | 4 | RELEASE:4 (IRAM/DRAM/SRAM/EXTRAM) |
| `NX_DVE` | 4 | RELEASE:4 |
| `NX_PE` | 4 | RELEASE:4 |
| `NX_POOL` | 4 | RELEASE:4 |
| `NX_SP` | 4 | RELEASE:4 |
| `Q7_POOL` | 4 | RELEASE:4 (carries the **sole non-zero EXTRAM** — §3.5) |

Single-flavor (RELEASE) floor: no DEBUG/PERF/TEST, no PROF, no DKL, no *in-lib*
EXTISA. The SUNDA EXTISA Q7 kernel is **weak-undef** → standalone container (§4).

### 3.2 CAYMAN — total **100** (the reference shape) `[binary-derived]`

| Cell | Getters | Flavor:region |
|---|---|---|
| `NX_ACT` | 14 | DEBUG:4 PERF:4 PROF:2 TEST:4 |
| `NX_DVE` | 14 | DEBUG:4 PERF:4 PROF:2 TEST:4 |
| `NX_PE` | 14 | DEBUG:4 PERF:4 PROF:2 TEST:4 |
| `NX_POOL` | 14 | DEBUG:4 PERF:4 PROF:2 TEST:4 |
| `NX_SP` | 12 | DEBUG:4 PERF:4 TEST:4 *(no PROF — SP has no HW-decode CAM)* |
| `Q7_POOL` | 32 | DEBUG:4 PERF:12 TEST:4 DKL_DEBUG:4 DKL_PERF:4 DKL_TEST:4 |

`PERF:12` on `Q7_POOL` = 4 base regions + 8 EXTISA (`4 SO + 4 JSON`, tagged PERF).

### 3.3 MARIANA / MARIANA_PLUS — total **100** each `[binary-derived]`

**Identical shape to CAYMAN** (`NX_{ACT,DVE,PE,POOL}=14`, `NX_SP=12`, `Q7_POOL=32`).

> **NOTE.** MARIANA and MARIANA_PLUS Q7 POOL ucode is **byte-identical** (same
> EXTISA sha256; [extisa inventory](./extisa-inventory.md)) — distinct gen labels
> over one shipped Q7 kernel set. The NX images differ: MPLUS is a
> recompile-relocation of MARIANA. The 386-count counts MARIANA disjoint from
> MARIANA_PLUS (`rg '^MARIANA_NX|^MARIANA_Q7'` excludes MPLUS).

### 3.4 MAVERICK — total **62** (the leaner v5 set) `[binary-derived]`

| Cell | Getters | Flavor:region |
|---|---|---|
| `NX_ACT` | **0** | — *(ACT folded into DVE)* |
| `NX_DVE` | 14 | DEBUG:4 PERF:4 PROF:2 TEST:4 *(the head engine — full set kept)* |
| `NX_PE` | 10 | PERF:4 PROF:2 TEST:4 *(no DEBUG)* |
| `NX_POOL` | 10 | PERF:4 PROF:2 TEST:4 *(no DEBUG)* |
| `NX_SP` | 8 | PERF:4 TEST:4 *(no DEBUG, no PROF)* |
| `Q7_POOL` | 20 | DEBUG:4 PERF:12 TEST:4 *(no DYNAMIC_KERNEL_LOAD)* |

The v5 shrink: ACT-fold (−14), DEBUG-drop on PE/POOL/SP, DKL-drop. `NX_DVE` and
`Q7_POOL` keep the full DEBUG+PERF+TEST. `[SPOT-VERIFIED §6]`

### 3.5 Flavor-presence-per-gen `[binary-derived]`

| Flavor | SUNDA | CAYMAN | MARIANA | MPLUS | MAVERICK |
|---|:---:|:---:|:---:|:---:|:---:|
| RELEASE | Y | — | — | — | — |
| PERF | — | Y | Y | Y | Y |
| DEBUG | — | Y | Y | Y | DVE + Q7 only |
| TEST | — | Y | Y | Y | Y |
| PROF (CAM/TABLE) | — | Y | Y | Y | DVE/PE/POOL only (no SP/ACT) |
| DKL (Q7_POOL) | — | Y | Y | Y | **—** (dropped) |
| EXTISA (Q7_POOL) | * | Y | Y | Y | Y  (*SUNDA = container-only) |

### 3.6 Where each `(gen, engine)` block lives in the getter `.text`

| GEN | `.text` VA range | first img-ptr |
|---|---|---|
| CAYMAN | `0x9b2fa0 … 0x9b3c80` | `0x58f40` (PERF_IRAM) |
| MARIANA | `0x9b3ca0 … 0x9b4900` | `0x30b8a0` |
| MARIANA_PLUS | `0x9b4920 … 0x9b5580` | `0x5a5480` |
| MAVERICK | `0x9b55a0 … 0x9b5d40` | `0x871300` |
| SUNDA | `0x9b2d20 … 0x9b3000` | `0x55f0` |

Getters are laid out by flavor then engine within each gen. `[HIGH/OBSERVED]`

---

## 4. The image-inventory summary

### 4a. Per-generation `[binary-derived — nm]`

| GEN | GETTERS | shape note |
|---|---:|---|
| SUNDA | 24 | 6 eng × RELEASE × 4 regions (single flavor) |
| CAYMAN | 100 | reference shape |
| MARIANA | 100 | == CAYMAN shape |
| MARIANA_PLUS | 100 | == CAYMAN shape |
| MAVERICK | 62 | no ACT, no DEBUG on PE/POOL/SP, no DKL |
| **TOTAL** | **386** | — |

### 4b. Per-engine (across all gens) `[binary-derived — nm]`

| ENGINE | GETTERS | composition |
|---|---:|---|
| `NX_ACT` | 46 | SUNDA 4 + 14×3; MAVERICK 0 |
| `NX_DVE` | 60 | SUNDA 4 + 14×4 |
| `NX_PE` | 56 | SUNDA 4 + 14×3 + MAVERICK 10 |
| `NX_POOL` | 56 | SUNDA 4 + 14×3 + MAVERICK 10 |
| `NX_SP` | 48 | SUNDA 4 + 12×3 + MAVERICK 8 |
| `Q7_POOL` | 120 | SUNDA 4 + 32×3 + MAVERICK 20 |
| **TOTAL** | **386** | — |

### 4c. Per-category (the resolver partition) `[binary-derived — nm]`

| Category | GETTERS | Resolver |
|---|---:|---|
| base (IRAM/DRAM/SRAM/EXTRAM) | 288 | `get_memory_image` |
| DKL (× 4 regions) | 36 | `get_memory_image` (flavor 17/18/19) |
| EXTISA (SO + JSON) | 32 | `get_ext_isa` |
| PROF (CAM + TABLE) | 30 | `get_hwdecode_table` |
| **TOTAL** | **386** | three resolvers, no overlap |

EXTISA verified per gen: CAYMAN/MARIANA/MARIANA_PLUS/MAVERICK = 8 each (4 SO + 4
JSON) = 32; SUNDA = **0** `t` (the 2 weak-undef). PROF verified: CAYMAN/MARIANA/
MARIANA_PLUS = 8 each + MAVERICK 6 = 30. `[binary-derived]`

### 4d. Region population `[binary-derived]`

Region getters = base (288) + DKL (36) = 324; EXTISA (32) and PROF (30) are not
`IRAM/DRAM/SRAM/EXTRAM` region getters.

| REGION | total | nonzero | zero | note |
|---|---:|---:|---:|---|
| IRAM | 81 | 76 | 5 | code; 5 zero = the MAVERICK SRAM-resident anomaly |
| DRAM | 81 | 81 | 0 | **always** non-zero (data + firmware log strings) |
| SRAM | 81 | 5 | 76 | almost always an empty boundary cursor |
| EXTRAM | 81 | 1 | 80 | almost always empty; the 1 = SUNDA Q7_POOL |
| **TOTAL** | **324** | **163** | **161** | base 288 + DKL 36 |

> **QUIRK — the two region anomalies (both in §6).**
> 1. **5 zero-IRAM getters** = `MAVERICK_NX_SP_{PERF,TEST}_IRAM` +
>    `MAVERICK_Q7_POOL_{DEBUG,PERF,TEST}_IRAM`. MAVERICK runs SP & Q7 *out of
>    SRAM*: e.g. `MAVERICK_NX_SP_PERF_IRAM` size `0x0`, but
>    `MAVERICK_NX_SP_PERF_SRAM` `0xf580` (code lives in SRAM). The zero-IRAM stub
>    even reuses the DRAM blob's boundary address (`8eeae0`, labelled
>    `MAVERICK_NX_SP_PERF_DRAM_get.data`).
> 2. **1 nonzero EXTRAM** = `SUNDA_Q7_POOL_RELEASE_EXTRAM` (`0x1b40`) — SUNDA's Q7
>    POOL carries an extra EXTRAM segment.

The split underneath: base region nonzero = 145 (IRAM 67, DRAM 72, SRAM 5,
EXTRAM 1); DKL region nonzero = 18 (IRAM 9, DRAM 9, SRAM 0, EXTRAM 0). Combined
nonzero region getters = **163**; + 62 nonzero EXTISA/PROF = **225 real** total.
`[binary-derived]`

### 4e. The real-vs-cursor closure `[binary-derived]`

Parsing all 386 stubs directly out of the binary (`lea`-target + `movq` size):

| | count |
|---|---:|
| real (`size > 0`) | **225** |
| zero-cursor (`size == 0`) | **161** |
| **total** | **386** |
| distinct img-ptr addresses | **225** (== the real count) |

The 386 getters are backed by **exactly 225 distinct img-ptr addresses**; every
zero-cursor reuses a real boundary address; **0 spurious aliases**.

---

## 5. The container model (which gen lives where)

The 386 getters' blobs are **one firmware corpus** surfaced through up to four
packaging views. They are byte-identical across views (sha256-proven): the
internal.so getter blob == the `libnrtucode.a` `img_*`/`hwdecode_*` member
`.rodata` == the EXTISA container's device ELF. `[HIGH/OBSERVED]`

| GEN | `internal.so` (5-gen twin) | `libnrtucode.a` (static) | extisa container (4-gen) | front lib (4-gen shipped) |
|---|---|---|---|---|
| SUNDA | base getters; **EXTISA weak-UNDEF** | base members; **no EXTISA member** | EXTISA Q7 ELF — *the only home* | base getters (EXTISA weak) |
| CAYMAN | full | full members | EXTISA Q7 ELFs | full |
| MARIANA | full | full members | EXTISA Q7 ELFs | full |
| MARIANA_PLUS | full | full members | EXTISA Q7 ELFs (== MARIANA Q7 bytes) | full |
| MAVERICK | **full (EXCLUSIVE)** | 0 members | absent | absent (NULL slots → `ret 3`) |

### Key container facts

**(a) Three-source byte-identity** `[SPOT-VERIFIED §6]`. The internal.so getter
blob == the `.a` member `.rodata` == the EXTISA container device ELF. Verified:
carve internal.so `[0x2ef7e0 : +0xa260]` → sha256 `910d41c3ededce67…` (ELF magic
`7f454c46`), == the container's `CAYMAN_EXTISA_0`.

**(b) SUNDA = standalone-only** `[binary-derived]`.
`SUNDA_Q7_POOL_RELEASE_EXTISA_0_{SO,JSON}_get` are `w` (weak-undef) in internal.so
— **no body present**. The SUNDA EXTISA Q7 device ELF (the largest single image,
the only real JSON manifest) lives **only** in `libnrtucode_extisa.so`, which
self-resolves the weak-undef getter at load. The EXTISA dispatch's SUNDA case
(§6) loads `sunda_libs @0x9b8f80` — whose first entry's symbol is literally
`SUNDA_Q7_POOL_RELEASE_EXTISA_0_SO_get@Base`, the weak placeholder.

**(c) MAVERICK = internal-twin-only**. The 4 MAVERICK EXTISA Q7 ELFs (clang-15 /
XtensaTools-15.05, ET_DYN, stripped) exist **only** in internal.so `.rodata`; 0
members in the `.a`; absent from the container. The shipped front lib reserves
the MAVERICK image_list slots but leaves the getters NULL → a MAVERICK request
returns `3`.

**(d) The `.a` is a superset, with framework objects** `[binary-derived — refinement]`.
`ar t libnrtucode.a` = **435 members**, decomposing as:

| Component | members |
|---|---:|
| CAYMAN image members | 124 |
| MARIANA image members | 124 |
| MARIANA_PLUS image members | 124 |
| SUNDA image members | 48 |
| MAVERICK image members | 0 |
| **subtotal (image members)** | **420** |
| framework `.c.o` objects (`nrtucode*.c.o`, `prelink*.c.o`, `pi_library_load.c.o`, `common.c.o`) | 15 |
| **TOTAL** | **435** |

> **CORRECTION (refinement, not a contradiction).** The "435 = 124 each + 48 + 0"
> shorthand in the accessor index (#750) actually describes the **420 image members**;
> the remaining **15** are the framework C objects (`nrtucode.c.o`,
> `nrtucode_context.c.o`, `nrtucode_core.c.o`, `nrtucode_images.c.o`,
> `nrtucode_loadable_library.c.o`, `nrtucode_opset.c.o`,
> `nrtucode_objcount.c.o`, `nrtucode_platform_memhandle_dummy.c.o`, the five
> `prelink*.c.o`, `common.c.o`, `pi_library_load.c.o`). The `.a` carries more
> per-gen members than internal.so surfaces as getters because it bundles
> DEBUG/TEST SUNDA builds the host never surfaces; MAVERICK (0 `.a` members) is
> linked into internal.so from a different external archive.

**Conclusion.** One firmware corpus; the "which container" axis is
SUNDA standalone-only / CAYMAN-MARIANA-MPLUS both / MAVERICK internal-twin-only.
The shipped runtime path is 4-gen; the 5th gen is internal-twin-exclusive.

---

## 6. The three resolvers (annotated C)

Three resolvers over three tables partition the 386 getters with no residue.
All resolver/table symbols present:

| Symbol | VA | type | role |
|---|---|---|---|
| `nrtucode_get_memory_image` | `0x9b2960` | `T` | base + DKL (324) |
| `nrtucode_get_ext_isa` | `0x9b2c80` | `T` | EXTISA public entry → internal |
| `nrtucode_get_ext_isa_internal` | `0x9b2b30` | `t` | EXTISA dispatch (32) |
| `nrtucode_get_num_ext_isa_libs` | `0x9b2c90` | `T` | per-coretype lib count |
| `nrtucode_get_hwdecode_table` | `0x9b2cd0` | `T` | PROF CAM/TABLE (30) |
| `image_list` | `0x9b8d20` | `d` | 38 × 16B `{count; descriptor*}` |
| `sunda_libs` / `cayman_libs` / `mariana_libs` / `mariana_plus_libs` / `maverick_libs` | `0x9b8f80 … 0x9b9050` | `d` | per-gen `{SO_get, JSON_get}` tables |
| `hwdecode_table_list` | `0x9b9090` | `d` | 38 × 16B `{CAM_get; TABLE_get}` |

`[HIGH/OBSERVED — nm]`

### 6a. `nrtucode_get_memory_image` @ `0x9b2960` — base + DKL (324 getters)

Disassembled. The `idx<=37` guard, the `NRTUCODE_MPLUS_ON_MARIANA`
tripwire, the `NEURON_UCODE_FLAVOR` env auto-path, the 0x28-stride descriptor
linear scan, and the 4-way region jump table at VA `0x555c` all confirmed.

```c
// int get_memory_image(uint idx, int region, int flavor,
//                       void** out_ptr, size_t* out_len);
int nrtucode_get_memory_image(uint idx, int region, int flavor,
                              void** out_ptr, size_t* out_len) {
    if (idx > 37) return 1;                          // cmp $0x25,%edi ; ja

    // --- removed-flag tripwire (NOT a live selector) ---
    const char* mom = getenv("NRTUCODE_MPLUS_ON_MARIANA");   // @0x4fae
    if (mom && (mom[0]=='1' ? mom[1] : mom[0])) {            // any truthy value
        fwrite("Error: the undocumented NRTUCODE_MPLUS_ON_MARIANA flag has "
               "been removed from libnrtucode; please transition to Neuron's "
               "NEURON_RT_DBG_V4_PLUS=0/1 env var",          // @0x4877, 0x99 B
               0x99, 1, stderr);
        return 8;                                            // BEFORE any table access
    }

    image_t* slot = &image_list[idx];               // idx<<4 + image_list (16B stride)

    if (flavor == 0) {                              // AUTO via env
        const char* f = getenv("NEURON_UCODE_FLAVOR");       // @0x52e2
        if      (!f)                       flavor = 1;       // PERF (default)
        else if (!strcmp(f,"debug"))       flavor = 2;       // @0x4733
        else if (!strcmp(f,"DEBUG"))       flavor = 2;       // @0x51b8
        else if (!strcmp(f,"test"))        flavor = 3;       // @0x503e
        else if (!strcmp(f,"TEST"))        flavor = 3;       // @0x4ddf -> sete*2+1
        else                               flavor = 1;       // unrecognized -> PERF
        // NOTE: no env value selects DKL (17/18/19) — that needs an explicit driver flavor.
    }

    uint64_t   count = slot->count;
    descriptor* d    = slot->table;                 // 40B / 0x28 stride
    if (count == 0) return 2;
    for (uint64_t i = 0; i < count; i++, d++) {     // linear scan for flavor_key
        if (d->flavor_key == flavor) {
            if (region > 3) return 2;               // region enum: 0..3
            // 4-way jump table @0x555c selects the region getter slot:
            //   region 0 -> d->IRAM_get  (+0x08)
            //   region 1 -> d->DRAM_get  (+0x10)
            //   region 2 -> d->SRAM_get  (+0x18)
            //   region 3 -> d->EXTRAM_get(+0x20)
            getter_fn g = d->region_get[region];
            if (!g) return 3;                       // NULL slot -> variant not linked
            g(out_ptr, out_len);                    // cursor stub returns (ptr, 0) -> ret 0
            return 0;
        }
    }
    return 2;                                        // flavor not found
}
```

| return | meaning |
|---|---|
| `0` | ok (incl. empty-but-present cursor, `*out_len == 0`) |
| `1` | `idx > 37` |
| `2` | flavor/region not found, or empty slot |
| `3` | region getter NULL (variant not linked) |
| `8` | removed-env-flag tripwire |

> **The `image_list` 38-slot map** `[binary-derived]`. Reading `.count` statically
> (the `.tbl` pointer is a load-time reloc): SUNDA `il[0..6]` count=1 (RELEASE
> single-flavor), CAYMAN `il[7..14]`, MARIANA `il[15..22]`, MPLUS `il[23..30]`,
> MAVERICK `il[31..35,37]`; `il[36]` is **EMPTY** (the reserved gap). Most slots
> have `count=3` (DEBUG/PERF/TEST); the Q7_POOL slots `il[13]`, `il[21]`,
> `il[29]` have **`count=6`** (3 base + 3 DKL flavors) — DKL is visible as the
> doubled descriptor count. MAVERICK's slots are `count=2/3` (DEBUG dropped).
> 37 of 38 slots populated.

### 6b. `nrtucode_get_ext_isa` / `_internal` — EXTISA (32 getters)

The public entry hardcodes `lib = 0` and tail-calls the internal dispatch:

```c
int nrtucode_get_ext_isa(uint coretype, int flavor, void* out, void** /*lib?*/) {
    return nrtucode_get_ext_isa_internal(coretype, flavor, out, /*lib=*/0);
    //  9b2c80: xor %ecx,%ecx ; jmp 9b2b30
}
```

The internal resolver keys on **raw coretype** (not the flat idx) via a 32-entry
jump table at VA `0x556c`, indexed by `coretype - 6`; the live set is exactly
`{6,13,21,29,37}`:

```c
int nrtucode_get_ext_isa_internal(uint coretype, int flavor,
                                  ext_isa_out_t* out, uint lib) {
    if (flavor == 0) {                              // AUTO via env (gates validity only)
        const char* f = getenv("NEURON_UCODE_FLAVOR");
        if (!f || !strcmp(f,"debug") || !strcmp(f,"DEBUG")
               || !strcmp(f,"test")  || !strcmp(f,"TEST"))
            flavor = (matched) ? mapped : default;  // same debug/test/PERF mapping as §6a
        if ((flavor - 4u) >= 0xfffffffd) ...        // flavor range gate -> ret 2 on bad
    } else if ((flavor - 4u) <= 0xfffffffc) {
        return 2;                                   // bad flavor
    }

    // 32-entry jump table @0x556c, index = coretype - 6 :
    uint k = coretype - 6;
    if (k > 0x1f) return 1;                          // bad coretype
    libs_t* base;
    switch (coretype) {                             // live arms only; all others -> ret 1
      case  6: base = sunda_libs;        break;     // @0x9b8f80  (1 lib)
      case 13: base = cayman_libs;       break;     // @0x9b8f90  (4 libs)
      case 21: base = mariana_libs;      break;     // @0x9b8fd0  (4 libs)
      case 29: base = mariana_plus_libs; break;     // @0x9b9010  (4 libs)
      case 37: base = maverick_libs;     break;     // @0x9b9050  (4 libs)
      default: return 1;
    }
    libs_t* e = (libs_t*)((char*)base + lib*16);    // {SO_get, JSON_get} 16B entry
    if (!e->SO_get)   return 3;                     // lib slot NULL (incl. SUNDA weak-undef)
    if (!e->JSON_get) return 3;
    e->SO_get  (&out->so_ptr,   &out->so_len);      // fills 0x20-byte out struct
    e->JSON_get(&out->json_ptr, &out->json_len);
    return 0;
}
```

`nrtucode_get_num_ext_isa_libs` @ `0x9b2c90` returns the per-coretype count via a
bitmask, instruction-exact:

```c
int nrtucode_get_num_ext_isa_libs(uint coretype, uint64_t* out) {
    *out = 0;
    if (coretype > 37) return 1;
    if (0x2020202000ULL & (1ULL << coretype)) {     // bits {13,21,29,37}
        *out = 4; return 0;
    }
    if (coretype == 6) { *out = 1; return 0; }      // SUNDA -> 1 lib
    return 1;
}
```

| return | meaning |
|---|---|
| `0` | ok |
| `1` | bad coretype |
| `2` | bad flavor |
| `3` | lib slot NULL (incl. SUNDA weak-undef unresolved) |

> **NOTE — coretype ↔ arch_id.** SUNDA 6/5, CAYMAN 13/12, MARIANA 21/20, MPLUS
> 29/28, MAVERICK 37/**36\***. `coretype = arch_id + 1`; the MAVERICK arch_id 36
> is **INFERRED** (no ncfw v5 image to read it from); coretype 37 is OBSERVED in
> the live jump-table arm. `[ct37 HIGH/OBSERVED; arch_id 36 MED/INFERRED]`

### 6c. `nrtucode_get_hwdecode_table` @ `0x9b2cd0` — PROF (30 getters)

The leanest resolver: no flavor logic, no env (PROF tables are
flavor-independent). Disassembled and confirmed instruction-exact:

```c
// int get_hwdecode_table(uint idx, int kind, void** out_ptr, size_t* out_len);
int nrtucode_get_hwdecode_table(uint idx, int kind,
                                void** out_ptr, size_t* out_len) {
    if (idx > 37) return 1;                         // cmp $0x25 ; ja
    hwdecode_t* e = &hwdecode_table_list[idx];      // idx<<4 + list (16B stride)
    getter_fn g;
    if      (kind == 0) g = e->CAM_get;             // +0x00
    else if (kind == 1) g = e->TABLE_get;           // +0x08
    else                return 1;                   // bad kind
    if (!g) return 2;                               // NULL getter
    g(out_ptr, out_len);
    return 0;
}
```

| return | meaning |
|---|---|
| `0` | ok |
| `1` | bad idx or bad kind |
| `2` | NULL getter |

`PROF_CAM` = `0x400` (64 slots × 16B `{opcode_id; mask; enable; reserved}`);
`PROF_TABLE` = `0x2000` (64 records × 128B). 15 `(gen,engine)` entries ×
`{CAM,TABLE}` = 30: CAYMAN/MARIANA/MPLUS × `{ACT,DVE,PE,POOL}` (12) + MAVERICK ×
`{DVE,PE,POOL}` (3) = 15. See [PROF CAM/TABLE formats](./prof-cam-table-formats.md).

### 6d. The partition

```
324 (get_memory_image: base 288 + DKL 36)
 + 30 (get_hwdecode_table: PROF)
 + 32 (get_ext_isa: EXTISA SO+JSON)
 = 386                       three resolvers, three tables, no overlap, no residue.
```

### 6e. Version getters (the catalog's identity stamps) `[binary-derived]`

| Getter | VA | Value |
|---|---|---|
| `nrtucode_get_build_version` | `0x9b0270` | `"1.21.1.0"` (@`0x5460`) |
| `nrtucode_get_git_version` | `0x9b0280` | `"6db9edc09474e79d0c0e283c14c16e51ee7417e5"` (@`0x51e8`) |
| `nrtucode_get_api_level` | `0x9b0260` | `3` (immediate) |

Identical in the 4-gen front lib and the 5-gen twin — adding MAVERICK did **not**
bump the version; it tracks the ulib/opcode contract, not the gen count.
The phantom-version risk is ruled out. `[build/git HIGH/OBSERVED; "tracks ulib
contract" MED/INFERRED]`

---

## 7. The catalog-level evolution roll-up (condensed)

This is the catalog summary only. The deep per-engine evolution narrative
(handler/opcode/RNG/MX/DGE/reset/PROF mechanisms, the structural-transition
timeline) lives on the
[generation map](../generations/codename-generation-map.md) and the
[Maverick profile](../generations/maverick-profile.md); the per-image kernel
content is the [cross-gen kernel_info matrix](./cross-gen-kernel-info-matrix.md).
This page records only the **catalog-visible** deltas.

### 7a. Getter-count trajectory

```
SUNDA 24  ->  CAYMAN 100  ==  MARIANA 100  ==  MPLUS 100  ->  MAVERICK 62
```

* **SUNDA → CAYMAN (24 → 100, ~4×): the flavoring jump.** SUNDA is RELEASE-only;
  CAYMAN adds DEBUG/PERF/TEST (×3), PROF (CAM/TABLE), DKL (Q7_POOL), and in-lib
  EXTISA — the table-dispatch + multi-flavor build model.
* **CAYMAN == MARIANA == MPLUS (100 each):** the getter **count** is invariant;
  feature growth is **inside** the images (handlers/opcodes/RNG/MX/PROF/DGE), not
  new getters.
* **MPLUS → MAVERICK (100 → 62): a net shrink** despite capability uplift —
  ACT-fold (−14), DEBUG-drop on PE/POOL/SP, DKL-drop. Capability moved into
  fewer/denser images + the host CSR.

### 7b. Flavor / region / container transitions (catalog-visible)

* RELEASE-only (SUNDA) → `{DEBUG,PERF,TEST}` multi-flavor (CAYMAN+).
* PROF: absent (SUNDA) → present on the 4 sequencer engines (CAYMAN/MARIANA/
  MPLUS) → DVE/PE/POOL only (MAVERICK; SP/ACT none).
* DKL (Q7_POOL DYNAMIC_KERNEL_LOAD): present CAYMAN/MARIANA/MPLUS → **dropped**
  MAVERICK.
* DEBUG: all-engines (CAYMAN/MARIANA/MPLUS) → NX_DVE + Q7_POOL only (MAVERICK).
* IRAM-resident code everywhere **except** MAVERICK SP & Q7_POOL → SRAM-resident
  (IRAM size 0). EXTRAM empty everywhere except `SUNDA_Q7_POOL_RELEASE` (`0x1b40`).
  DRAM always non-zero.
* SUNDA EXTISA = standalone-container-only (weak-undef host getter);
  CAYMAN/MARIANA/MPLUS = both; MAVERICK = internal-twin-only (clang-15/ET_DYN;
  absent from container + `.a`). MARIANA Q7 ucode == MPLUS Q7 ucode (byte-identical).
* Build toolchain: clang-10 / XtensaTools-14.09 (SUNDA…MPLUS) → clang-15 / 15.05
  (MAVERICK).

> **WALL.** MAVERICK image **interiors** are INFERRED (header-observed): the four
> v5 EXTISA Q7 ELFs and the SRAM-resident SP/Q7 code are read at the
> ELF-header/getter level (sizes, segment placement, magic) — the per-handler
> decode of v5 device code is a header-observed wall on this binary.

---

## 8. The completeness check

**(1) Count closure.** `nm` `t` getters = **386**. Three independent partitions
all close to 386: per-gen `24+100+100+100+62`; per-engine
`46+60+56+56+48+120`; per-category `288+32+30+36`. No getter unaccounted.
`[binary-derived]`

**(2) Resolver closure.** Every getter maps to exactly one resolver category:
`base 288 + DKL 36 = 324` (`get_memory_image`) + `PROF 30`
(`get_hwdecode_table`) + `EXTISA 32` (`get_ext_isa`) = **386**. No getter is
reachable by zero resolvers, none by two. `[HIGH/OBSERVED]`

**(3) Real-vs-cursor closure.** `225 real + 161 zero-cursor = 386`, backed by
exactly **225 distinct img-ptr addresses** (== the real count); every
zero-cursor reuses a real boundary address, 0 spurious aliases. `[binary-derived]`

**(4) Weak-undef accounting.** The 2 SUNDA EXTISA getters are `w` (weak-undef),
resolved from the standalone container at load (§5b). They are **not** among the
386 `t` getters — they are link-time placeholders. Full accessor surface =
**386 `t` (in-lib) + 2 `w` (container-resolved) = 388** symbols, of which 386
carry in-lib bodies. No unresolved/dangling getter beyond these 2 documented
placeholders. `[binary-derived]`

**(5) Unmapped-getter sweep.** Zero getters fall outside the
`(gen × engine × flavor × region)` grammar (all 386 parse with 0 residue); zero
lack a resolver; zero lack an `nm .data` blob symbol (386/386 `lea` targets ==
`nm .data`). **The catalog is complete.** `[HIGH/OBSERVED]`

### Every absence explained

| Absence | Reason | Class |
|---|---|---|
| MAVERICK `NX_ACT` (0 getters) | ACT folded into DVE | FILE-ABSENT |
| MAVERICK PE/POOL/SP DEBUG (0) | DEBUG dropped on v5 non-head engines | FILE-ABSENT |
| MAVERICK DKL (0) | DYNAMIC_KERNEL_LOAD removed on v5 | FILE-ABSENT |
| MAVERICK SP/ACT PROF (0) | PROF never on SP, ACT folded | FILE-ABSENT |
| SUNDA EXTISA `t` body (0) | standalone-container-only | weak-undef |
| MAVERICK SP/Q7 IRAM (size 0) | code is SRAM-resident | cursor |
| most SRAM/EXTRAM getters (size 0) | empty boundary cursor | cursor |
| MAVERICK in `.a` / container (0 members) | internal-twin-exclusive | FILE-ABSENT |

---

## 9. Spot-verification ledger

All against `libnrtucode_internal.so` (sha256 `b7c67e8…`, MATCH).
Stock `nm`/`objdump`/`readelf` + `python hashlib`.

| # | Check | Result |
|---|---|---|
| SV-1 | getter counts | `nm` `t` `_get$` = **386**; per-gen 24/100/100/100/62; SUNDA EXTISA = 2 `w`. ✓ |
| SV-2 | category partition | base 288, EXTISA 32, PROF 30, DKL 36 = 386. ✓ |
| SV-3 | 7 getters disassembled (all 5 gens + anomalies) | every `(img-ptr, size)` byte-exact (table below). ✓ |
| SV-4 | carve sha256 | `EXTISA_0_SO` `0x2ef7e0/0xa260` → `910d41c3…` (ELF magic); `ACT_DEBUG_DRAM` `0x169400/0x6260` → `f6c5136e…`. ✓ |
| SV-5 | resolver + table symbols | all 5 tables + 5 resolvers at documented VAs. ✓ |
| SV-6 | real-vs-cursor + distinct-ptr | 225 real / 161 zero / 225 distinct img-ptr; region IRAM 76, DRAM 81, SRAM 5, EXTRAM 1. ✓ |
| SV-7 | MAVERICK structural drops | `NX_ACT`=0, `NX_DVE_DEBUG`=4, `{PE,POOL,SP}_DEBUG`=0, `Q7_POOL_DEBUG`=4, DKL=0. ✓ |

### SV-3 — the 7 disassembled getters

| Getter | `.text` VA | img-ptr | size |
|---|---|---|---|
| `CAYMAN_NX_ACT_DEBUG_DRAM_get` | `0x9b3540` | `0x169400` | `0x6260` |
| `MARIANA_NX_PE_PERF_IRAM_get` | `0x9b3da0` | `0x335ca0` | `0x12ce0` |
| `MARIANA_PLUS_NX_DVE_TEST_DRAM_get` | `0x9b4c40` | `0x659800` | `0x3660` |
| `MAVERICK_NX_SP_PERF_IRAM_get` | `0x9b5920` | `0x8eeae0` | `0x0` *(IRAM empty; reuses DRAM addr)* |
| `MAVERICK_NX_SP_PERF_SRAM_get` | `0x9b5960` | `0x8f0fa0` | `0xf580` *(code in SRAM)* |
| `SUNDA_Q7_POOL_RELEASE_EXTRAM_get` | `0x9b3000` | `0x57400` | `0x1b40` *(sole EXTRAM)* |
| `CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get` | `0x9b3aa0` | `0x2ef7e0` | `0xa260` *(ELF)* |

### Spot-shas (one real blob per generation)

| Sample | img-ptr / size | sha256 (16) | magic |
|---|---|---|---|
| SUNDA `Q7_POOL_RELEASE_EXTRAM` | `0x57400 / 0x1b40` | `da19ac4dc535fea6…` | `3661003f` |
| CAYMAN `Q7_POOL_PERF_EXTISA_0_SO` | `0x2ef7e0 / 0xa260` | `910d41c3ededce67…` | `7f454c46` (ELF) |
| CAYMAN `NX_ACT_DEBUG_DRAM` | `0x169400 / 0x6260` | `f6c5136e99c09f04…` | `34cb9960` (DRAM hdr) |
| MARIANA `NX_PE_PERF_IRAM` | `0x335ca0 / 0x12ce0` | `a077f1106a74b692…` | `067d0000` (code) |
| MARIANA_PLUS `NX_DVE_TEST_DRAM` | `0x659800 / 0x3660` | `b76d97bd677b0138…` | `34cb9960` (DRAM hdr) |
| MAVERICK `NX_SP_PERF_SRAM` | `0x8f0fa0 / 0xf580` | `08e3594546fce8c8…` | `06780000` (code) |

> **NOTE.** DRAM blobs share the data-segment header magic `34cb9960`; IRAM/SRAM
> code blobs carry a device-code header (`067d…`/`0678…`); the EXTISA blob is a
> real ELF (`7f454c46`). A full 386-way sha sweep was **not** run — byte-identity
> is SPOT-VERIFIED on these samples; the `(movq-size == blob-size)` and
> `(lea-target == nm .data)` uniformity holds for all 386, establishing the
> mechanism uniformly. `[carved shas HIGH/OBSERVED; un-swept members MED]`

---

## 10. Confidence ledger

**HIGH / OBSERVED (binary-grounded):** the 386-getter index +
per-gen counts 24/100/100/100/62; the category partition 288+32+30+36=386;
per-engine 46/60/56/56/48/120; the real-vs-cursor tally 225/161 with 225 distinct
img-ptr; 7 getters' `(img-ptr,size)` + 6 carve shas; the three-resolver mechanism
+ all 5 table symbols + the `image_list`/coretype jump tables; the container
model (SUNDA standalone-only / MAVERICK twin-only) with the weak-undef SUNDA
EXTISA; the MAVERICK structural drops (ACT-fold, DEBUG-drop, DKL-drop,
SRAM-resident SP/Q7); the flavor/region matrices; the version stamps.

**MED / INFERRED (carried forward):** engine_idx POOL=1 /
ACT=2 (PE=0, DVE=3 OBSERVED); the reason `NX_SP` occupies two `image_list` slots
per gen; MAVERICK arch_id 36 (`coretype = arch_id+1`); "build_version tracks the
ulib contract, not gen-count."

**WALL / BOUNDARY:** MAVERICK image **interiors** are header-observed (the per-v5
device-code decode is a wall on this binary). Which silicon part each
`(gen,engine)` image binds to, and which image index/flavor the live host driver
selects at load, are upstream of these binaries. The deep evolution narrative is
the [generation map](../generations/codename-generation-map.md) /
[Maverick profile](../generations/maverick-profile.md); the per-image opcode
content is the [cross-gen kernel_info matrix](./cross-gen-kernel-info-matrix.md).

---

## 11. Reproduction

```bash
F=…/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/\
custom_op/c10/lib/libnrtucode_internal.so

# total getters (386) and per-gen
nm "$F" | rg ' t ' | rg '_get$' | wc -l
nm "$F" | rg ' t ' | rg '_get$' | rg -c '^SUNDA_'            # 24
nm "$F" | rg ' t ' | rg '_get$' | rg -c '^MARIANA_NX|^MARIANA_Q7'  # 100 (disjoint)

# SUNDA EXTISA weak placeholders (2)
nm "$F" | rg ' w ' | rg EXTISA

# category partition
G=$(nm "$F" | rg ' t ' | rg '_get$' | awk '{print $3}')
echo "$G" | rg '_(IRAM|DRAM|SRAM|EXTRAM)_get$' | rg -v DYNAMIC_KERNEL_LOAD | rg -v EXTISA | wc -l  # 288
echo "$G" | rg 'EXTISA_[0-3]_(SO|JSON)_get$' | wc -l                                                # 32
echo "$G" | rg 'PROF_(CAM|TABLE)_get$' | wc -l                                                      # 30
echo "$G" | rg DYNAMIC_KERNEL_LOAD | rg '_get$' | wc -l                                             # 36

# a getter's (img-ptr, size)
va=$(nm "$F" | rg " t SUNDA_Q7_POOL_RELEASE_EXTRAM_get\$" | awk '{print $1}')
objdump -d --start-address=0x$va --stop-address=$((0x$va+0x20)) "$F"

# carve sha (.rodata identity-mapped)
python3 -c 'import hashlib;d=open("'"$F"'","rb").read();print(hashlib.sha256(d[0x2ef7e0:0x2ef7e0+0xa260]).hexdigest())'

# resolvers + tables
nm "$F" | rg 'image_list|_libs$|hwdecode_table_list|get_memory_image|get_ext_isa|get_hwdecode_table|get_num_ext_isa'
objdump -d --start-address=0x9b2960 --stop-address=0x9b2d20 "$F"   # all three resolvers

# .a member arithmetic (435 = 420 image + 15 framework)
ar t …/libnrtucode.a | wc -l
ar t …/libnrtucode.a | rg -vi 'cayman|mariana|sunda|maverick' | wc -l   # 15 framework objects
```

---

*Capstone status: built and completeness-checked. Three independent partitions
close to 386; resolver closure `324+30+32=386`; real-vs-cursor `225+161=386` with
225 distinct img-ptr; 0 unmapped getters; 2 documented SUNDA weak-undef
placeholders. The image lane is complete.*
