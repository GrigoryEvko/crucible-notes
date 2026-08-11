# `al_address_map_db.pkl` — Load, Schema, and Top Blocks

This page opens the **pkl-carve sub-lane** of the address-map subtree. It documents
how to **safely** recover the Annapurna-Labs address-map *source* database — the richer
register-tree DB from which the flat per-SoC YAML (see
[`soc-master-map.md`](./soc-master-map.md)) is derived — and establishes the
primitive that the six leaf-subtree pages reuse:
[TPB](./pkl-tpb-subtree.md), [DMA](./pkl-dma-subtree.md), [HBM](./pkl-hbm-subtree.md),
[PCIe/D2D fabric](./pkl-pcie-d2d-fabric.md),
[INTC/SPROT security](./pkl-intc-sprot-security.md), and
[TOP_SP coverage](./pkl-topsp-coverage.md).

The artifact is `al_address_map_db.pkl` plus its `.json` mirror, shipped (RTL-generated,
vendor data) inside the `aws-neuronx-gpsimd-customop-lib` package:

```
.../arch-headers/maverick/ext/al_address_map_db.pkl    216,631,794 bytes   (binary DB)
.../arch-headers/maverick/ext/al_address_map_db.json   514,276,583 bytes   (pretty mirror)
```

> **WALL — MAVERICK (v5) vs CAYMAN (NC-v3).** The directory is
> `arch-headers/`**`maverick`**`/ext/`, and `323,197/323,197` non-root records carry a
> `json` path rooted at `/proj/maverick/hannloui/pool_wa/…`. This DB describes the
> **MAVERICK SoC (v5)**. v5 is **header-OBSERVED only** in this tree: the *DB structure*
> below (records, field schema, view layout, counts) is **OBSERVED** — the file ships and
> is read structurally — but **any claim about the v5 SoC interior behind an address is
> INFERRED**. The byte-grounded cross-check is the **CAYMAN (NC-v3)** flat YAML
> (`address_map_flat.yaml`, 34,858 nodes). Same DB *format*, different SoC *instance*.
> `[HIGH · OBSERVED]` for structure; `[* · INFERRED]` for v5 interiors.

---

## 0. The safe-load recipe (the reusable primitive)

> **⚠ SECURITY — NEVER `pickle.load()` THIS FILE.** A `.pkl` is a *program*: the
> `GLOBAL`/`STACK_GLOBAL`/`REDUCE` opcodes resolve and call arbitrary callables at load
> time, so `pickle.load()` / `pickle.loads()` on an untrusted 216 MB blob is arbitrary
> code execution **and** an OOM risk (it materializes the entire object graph). The pages
> #904–#909 must use one of the two inert readers below. This section is the deliverable
> the whole sub-lane depends on.

### Recipe A — structural recovery with `pickletools.genops()` (no object construction)

`pickletools.genops()` is a pure **opcode iterator**: it walks the pickle stream and
yields `(opcode, arg, position)` tuples *without ever building objects* and *without ever
calling `find_class`*. It cannot execute embedded code. Use it to (a) prove the file inert
and (b) count records straight off the opcode stream.

```python
import pickletools
from collections import Counter

PKL = ".../arch-headers/maverick/ext/al_address_map_db.pkl"

# Opcodes that resolve/call a Python callable at load time — the attack surface.
DANGEROUS = {"GLOBAL", "STACK_GLOBAL", "REDUCE", "INST", "OBJ",
             "NEWOBJ", "NEWOBJ_EX", "BUILD",
             "EXT1", "EXT2", "EXT4", "PERSID", "BINPERSID"}

hist = Counter()
with open(PKL, "rb") as f:
    for opcode, arg, pos in pickletools.genops(f):   # streams; never builds objects
        hist[opcode.name] += 1

assert not (set(hist) & DANGEROUS), "DB is NOT pure-data — DO NOT load!"
records = hist["EMPTY_DICT"] // 2          # see §3 — each record == 2 EMPTY_DICT
print("records:", records)                 # -> 323198
```

**Why `EMPTY_DICT // 2` counts records:** every record is one outer dict *and* one inner
`tag` sub-dict, so `EMPTY_DICT` opcodes == `2 × records` (§3). This gives the record total
from the raw byte stream, *before* trusting any materialized object.

### Recipe B — guarded `Unpickler` (defense-in-depth, optional)

Even after Recipe A proves the file inert, a hardened load subclasses `pickle.Unpickler`
and makes `find_class()` **raise** on *any* module/name. If the load completes, no global
was ever requested — an independent confirmation that the DB is pure data.

```python
import pickle

class RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        raise pickle.UnpicklingError(f"BLOCKED global: {module}.{name}")

db = RestrictedUnpickler(open(PKL, "rb")).load()     # raises if ANY callable resolved
```

> **GOTCHA — Recipe B still materializes ~514 MB in RAM.** The `find_class` block stops
> *code execution*, not *memory growth*. For count/scan work on memory-constrained hosts,
> prefer Recipe A (opcode stream) or Recipe C (streaming JSON) — both are O(1) in memory.

### Recipe C — stream the `.json` mirror (no pickle risk at all)

The sibling `.json` is a pretty-printed mirror of the *same* list-of-dicts (identical first
record, identical field order — verified §6). Plain text → zero pickle risk. Stream it with
`ijson` (yajl2 C backend) so the 514 MB never lands in RAM at once:

```python
import ijson

JSON = ".../arch-headers/maverick/ext/al_address_map_db.json"
with open(JSON, "rb") as f:
    for rec in ijson.items(f, "item"):   # yields ONE record dict at a time, O(1) memory
        ...                              # rec is a plain dict; no code path executes
```

`jq -c '.[]'` (streaming) is the shell equivalent. **Do not** `json.load()` the whole file
— that is the 514 MB slurp you are avoiding.

---

## 1. Container format

| Property | Value | How verified |
| --- | --- | --- |
| Pickle header | `80 04 95 08 00 01 00 00` = `PROTO 4` + `FRAME` | `head -c8`; `genops` `PROTO`=1 |
| Protocol | 4 (Python 3.4+) | header `80 04` |
| Top-level type | `list` | `EMPTY_LIST`=646,397; first `[` in JSON |
| Top-level length | **323,198** records | §3 (two independent counts) |
| Element type | `dict`, one per address node | `EMPTY_DICT`=646,396 = 2×323,198 |
| Record shapes | **2** (23-key node / 22-key root) | §2 |
| Total opcodes | 33,065,559 | `genops` full scan |
| Encoding | FRAME-chunked (3,304 frames), MEMOIZE-heavy | `genops` |

The encoding is heavily **memoized**: 10,141,891 `MEMOIZE` entries and 8,024,869 `BINGET`
back-references store each repeated key-string *once*. This is why a 216 MB binary expands
to a 514 MB pretty JSON — the JSON re-spells every memoized string inline.

> **`genops` opcode scan:** `DANGEROUS ∩ present = ∅` — **zero**
> `GLOBAL`/`STACK_GLOBAL`/`REDUCE`/`NEWOBJ*`/`BUILD`/`EXT*`/`PERSID`. The stream is
> *pure data* (only `list`, `dict`, `str`, `int`, `tuple`, memo refs). Safe to materialize.

---

## 2. Record schema — **23 fields**

Streaming the JSON mirror and tallying `len(rec)` per record yields exactly two shapes:

* **323,197 records — 23 keys** (the full schema below, including `json`)
* **1 record — 22 keys** — the root `ADDRESS_MAP` node, which has **no `json` key**

> **NOTE — the 23-key set, verbatim.** The field-count distribution over the DB is
> `{23: 323197, 22: 1}` (23 fields per node; the root is 22-key, no `json`). The 23-key
> set, by `ijson` key union, is exactly: `InclSizeInBytes, address, as_array, base, count,
> instance_index, json, legacy, name, offset, parameters, parentName, parent_array_size,
> parent_name_no_idx, parent_names, parent_offset, parent_size, self_array_size,
> short_name, short_name_lc, size, tag, type`.

| Field | Type | Meaning (recovered) |
| --- | --- | --- |
| `name` | str | Full hierarchical node name, `_`-joined SoC-tree path; unique. e.g. `USER_INT_SENG_0_TPB_0_SP_0_LOCAL_REG` |
| `short_name` | str | Leaf name (last path component), e.g. `LOCAL_REG` |
| `short_name_lc` | str | Lower-cased `short_name` |
| `size` | int | Window size in **bytes**. Root = `2**64` (full-space sentinel = `18446744073709551616`) |
| `InclSizeInBytes` | int | Inclusive subtree span in bytes (`≥ size`) |
| `type` | str | Node class — 6 values (§3) |
| `offset` | int | **Parent-relative** byte offset within the immediate parent. *(The field the flat YAML DROPPED.)* e.g. `LOCAL_REG` = `0x60000` inside its `SP_n` parent |
| `parent_names` | list[str] | Ancestor chain `root…parent`, e.g. `['ADDRESS_MAP','user_int','seng_0','tpb_0','SP_0']`. `len` = depth |
| `base` | int | Absolute address **within the view domain** (== `address`; §4) |
| `address` | int | == `base` for **100%** of records (323,198/323,198). Redundant alias |
| `count` | int | Array multiplicity. Values `{1:269270, 4:43144, 8:6912, 2:2704, 32:1024, 9:144}` |
| `parent_size` | int | Parent's array multiplicity |
| `parent_offset` | int | Offset within parent's array stride (`-1` if not an array member) |
| `instance_index` | str(hex) | This instance's array index, hex string `"0x0"`…; root = `-1` (int) |
| `parent_name_no_idx` | str | Space-joined ancestor path, array indices stripped, e.g. `ADDRESS_MAP user_int seng_0 tpb` |
| `parent_array_size` | str | `*`-joined cumulative array dims of the ancestor path, e.g. `1*1*4*1*1*1*16*1*1*1*4` |
| `self_array_size` | str(int) | This node's own array size as a string, `{"1":…,"16":…,"4":…,"8":…}`; root = `-1` (int) |
| `as_array` | str | `"false"` for **all** records (arrays already materialized into separate records) |
| `tag` | dict | RTL back-annotation (§5). Empty `{}` for most; non-empty for 19,425 |
| `legacy` | str | `"false"` for **all** records |
| `parentName` | str | Lower-cased parent node name; `"None"` for root |
| `parameters` | list | RTL Verilog parameter overrides as `(KEY, value-string)` tuples (§5). Empty for many |
| `json` | str | Path to the register-file **schema** for this leaf (§6). Present on **all but the root** |

**The root record (the 22-key one), verbatim:**

```python
{ name='ADDRESS_MAP', short_name='ADDRESS_MAP', short_name_lc='ADDRESS_MAP',
  size=2**64, InclSizeInBytes=0, type='REGFILE', offset=0, parent_names=[],
  base=0, address=0, count=1, parent_size=1, parent_offset=-1, instance_index=-1,
  parent_name_no_idx='', parent_array_size='', self_array_size=-1, as_array='false',
  tag={}, legacy='false', parentName='None', parameters=[] }   # no "json" key
```

---

## 3. Node `type` taxonomy — **6 classes**

Streaming-count of the `type` field over all 323,198 records:

| `type` | count | meaning (recovered from sample nodes + `json` target) |
| --- | --- | --- |
| `REGFILE` | 268,201 | plain register-file / memory leaf (also the root) |
| `NODE` | 38,573 | pure container (`TPB_0`, `HBM`, `SENG_0`, the 5 view domains) |
| `TABLE` | 9,848 | table-style register block (e.g. DGE cmd-inject → `*_table.json`) |
| `INTC` | 5,904 | interrupt-controller register block (→ `ap_intc/*`) |
| `MEM_CTRL` | 336 | memory-controller register block (→ `udma_eth_mem_ctrl.json`) |
| `INDIRECT_ACCESS` | 336 | indirect-access register window (small, e.g. `0xC` bytes) |
| **sum** | **323,198** | ✔ matches record total |

The five leaf classes (`REGFILE/TABLE/INTC/MEM_CTRL/INDIRECT_ACCESS`) are a **typed leaf
taxonomy the flat YAML collapsed**: the YAML only recorded "has `json`?" vs "no `json`".
The pkl tells you *how* each CSR block is accessed.

> **`EMPTY_DICT` reconciliation.** The 6 `type` counts sum to 323,198,
> and the `genops` `EMPTY_DICT` count (646,396) is `2 × 323,198` — each record contributes
> one outer dict and one `tag` sub-dict. Two fully independent paths (opcode stream and
> materialized type-tally) agree on **323,198**.

---

## 4. Addressing model — **5 access-domain views**, view-relative base

The DB is organized **first by access-domain VIEW**, then by SoC engine, then by block.
There are exactly **five** top-level VIEW nodes — the direct children of the root
(`parent_names == ['ADDRESS_MAP']`):

| view record | `short_name` | `base` (bits 63:60) | `size` |
| --- | --- | --- | --- |
| `ADDRESS_MAP_USER_INT` | `USER_INT` | `0x0000000000000000` | `0x1000000000000000` |
| `ADDRESS_MAP_USER_PCIEA` | `USER_PCIEA` | `0x1000000000000000` | `0x1000000000000000` |
| `ADDRESS_MAP_SECURE_INT` | `SECURE_INT` | `0x2000000000000000` | `0x1000000000000000` |
| `ADDRESS_MAP_SECURE_PCIEM` | `SECURE_PCIEM` | `0x3000000000000000` | `0x1000000000000000` |
| `ADDRESS_MAP_USER_POD` | `USER_POD` | `0x4000000000000000` | `0x4000000000000000` |

All five are `type=NODE`, `count=1`. The **top nibble (bits 63:60)** selects the domain:
`{user-internal, user-PCIe-A, secure-internal, secure-PCIe-M, user-POD}` — the
**USER/SECURE × INT/PCIe** access matrix plus a POD domain.

**`base` vs `address`:** `base == address` for **100%** of records.
Both hold the node's absolute address *within its view domain* (the view's `0x_000…000`
prefix is the zero point). The richer, YAML-absent field is `offset` (parent-relative).

> **ANCHOR.** Sampled leaf `USER_INT_SENG_0_TPB_0_SP_0_LOCAL_REG`:
> `base = offset = 0x60000`, `size = 0x10000`, `parent_names =
> ['ADDRESS_MAP','user_int','seng_0','tpb_0','SP_0']`, `json = …/csrs/tpb/tpb_xt_local_reg.json`.
> The `LOCAL_REG` window sits `0x60000` inside its `SP_0` parent — a fact the flat YAML
> cannot express.

**Records per view** (streaming `parent_names[1]` tally, sums to 323,198):

| view | records |
| --- | --- |
| `secure_int` | 244,040 |
| `user_int` | 79,104 |
| `user_pciea` | 24 |
| `secure_pciem` | 24 |
| *L1 view nodes* | 5 |
| *root* | 1 |

> **QUIRK — `user_pod` is an empty domain.** `USER_POD` is *declared*
> (`size 0x4000000000000000`) but has **zero materialized children** — it never appears in
> the per-view record tally above. v5 INFERRED: the POD (multi-die pod) domain is a
> placeholder window reserved for inter-die addressing whose contents this MAVERICK build
> did not expand. `[HIGH · OBSERVED for emptiness; · INFERRED for purpose]`

**Engine layer** (`parent_names == ['ADDRESS_MAP','<view>']`): the internal views
(`user_int`/`secure_int`) hold 4 engines `SENG_0..3`, each `0x100000000000` (16 TiB); the
PCIe views (`user_pciea`/`secure_pciem`) hold 8 sparse PCIe blocks each (24 records/view).

> **NOTE — view-size sum.** The five view `size` fields sum to
> `0x8000000000000000` (4 × `0x1000…` + `0x4000…`), not a clean `2**64`. The top-nibble
> *bases* are nonetheless distinct and contiguous (`0x0,0x1,0x2,0x3,0x4`), so the domains
> do not overlap; the upper half of the 64-bit space (`0x5…0xF` prefixes) is simply
> unmapped in this DB.

---

## 5. Top-level block enumeration

The root's descendants partition into the hierarchy below (depths via `len(parent_names)`):

```
L0  ADDRESS_MAP                                                         (1, root)
L1  VIEW:   user_int | user_pciea | secure_int | secure_pciem | user_pod (5)
L2  ENGINE: SENG_0..3 (int views) | PCIEA_0..7 | PCIEM_0..7 (pcie views)
L3  SoC REGION families per engine: TPB_0/1, HBM, APB_IO,
            H_DIE_SCRATCHPAD_*, DDMA_DESC_*, CDMA_DESC_*, RESERVED_*,
            and the C_DIE / H_DIE / IO_DIE multi-die split
L4..L15  deep register hierarchy (peaks near depth 11)
```

**Cross-check against the CAYMAN flat YAML** (`address_map_flat.yaml`, 34,858 nodes; the
byte-grounded NC-v3 instance):

| Aspect | CAYMAN flat YAML (NC-v3, OBSERVED) | MAVERICK pkl (v5, OBSERVED structure) |
| --- | --- | --- |
| node total | 34,858 | 323,198 (~9.3×) |
| view layer | **none** — single flattened view, absolute addrs | **5** access-domain views |
| top tokens | `HBM_0`, `APB_SE_0`, `TPB`, `TOP`, `PEB`, `INTC`, `PCIE` … | `USER_INT/SECURE_INT/…` → `SENG_n` → `TPB_n/HBM/APB_IO/…` |
| DMA naming | **`SDMA`** (`APB_SE_0_SDMA_0_UDMA_M2S`) | **`DDMA`/`CDMA`/`UDMA`** (no `SDMA` token) |
| distinct schemas | **76** csrs files | **320** distinct `json` paths (superset) |
| dropped field | — | `offset` (parent-relative), array dims, HDL paths, RTL params |

> **CORRECTION/NOTE — DMA engine naming differs by instance.** The
> CAYMAN YAML names its DMA engine `SDMA` (`csrs/sdma/udma_m2s.json`); the MAVERICK pkl has
> **zero** `SDMA` tokens and instead names them `DDMA`/`CDMA`/`UDMA`. Downstream
> [DMA-subtree](./pkl-dma-subtree.md) extraction must grep `DDMA\|CDMA\|UDMA` on the pkl,
> and map to `SDMA` only when correlating to the Cayman YAML. The v5-only blocks
> (`ap_intc`, `d2d_top`, `xbar`, `tg_term`, `periphs`, `hdie_scratchpad`, `io_fabric` —
> absent from Cayman's 76-schema set) are **INFERRED** to be MAVERICK-specific additions.

> **WALL restated:** the *existence and counts* of these blocks are **OBSERVED** in the
> shipped pkl. What any block's address *does inside the v5 silicon* is **INFERRED** —
> the Cayman YAML is the only byte-grounded behavioral cross-check, and it is a *different
> SoC instance*. Treat all absolute v5 addresses as MAVERICK-specific.

---

## 6. The `json` schema binding

| Metric | Value |
| --- | --- |
| records with a `json` binding | 323,197 (every non-root node) |
| distinct `json` paths | **320** (vs the Cayman YAML's 76 curated `csrs` schemas) |
| paths under `/csrs/` | 59,792 records (the curated, YAML-visible set) |
| paths NOT under `/csrs/` | 263,405 records (raw RTL `Registers/` dirs — invisible to the YAML) |

The pkl points at the *real* RTL register-block JSONs (e.g.
`…/basic_cells/Registers/regfile/regfile_parity_log.json`, 61,024 refs;
`…/maverick_dma_ip/udma/…/fci/Registers/fci_channel.json`, 43,008 refs), not the YAML's
pruned `csrs/` copies. The block→schema mapping is detailed in
[`block-schema-xref.md`](./block-schema-xref.md).

**pkl ↔ json equivalence:** the streaming record count of the `.json`
mirror is **323,198**, identical to the pkl `len`, with identical first record and field
order. The two files are byte-different encodings of **one** database.

---

## 7. The safe record iterator (the primitive #904–#909 reuse)

This is the canonical extraction primitive for the leaf-subtree pages. It selects a subtree
by `(view, keyword)` and exposes the real field keys — never calling `pickle.load`:

```python
import ijson   # Recipe C — streaming, O(1) memory, zero pickle risk

def iter_subtree(json_path, view, keyword):
    """Yield each address-map record whose name contains <keyword> inside <view>.

    view    : one of 'user_int','user_pciea','secure_int','secure_pciem','user_pod'
    keyword : substring matched against rec['name'] (e.g. 'TPB', 'DDMA', 'HBM',
              'INTC', 'SPROT', 'TOP_SP')
    Each yielded record already carries (real keys):
        base / offset / size  : absolute (view-relative) addr, parent-rel offset, span
        type                  : REGFILE | NODE | TABLE | INTC | MEM_CTRL | INDIRECT_ACCESS
        parent_names          : full ancestor chain (root..parent)
        count / self_array_size / parent_array_size : array/instance dims
        tag                   : RTL back-annotation (HDL_PATH, APB_CHAIN_NUMBER, DIE_NAME…)
        parameters            : RTL Verilog (KEY, value) overrides
        json                  : RTL register-block schema path
    """
    with open(json_path, "rb") as f:
        for rec in ijson.items(f, "item"):          # one record dict at a time
            pn = rec.get("parent_names", [])
            if len(pn) >= 2 and pn[0] == "ADDRESS_MAP" and pn[1] == view \
               and keyword in rec["name"]:
                yield rec

# Example: every TPB register leaf in the user-internal view.
for r in iter_subtree(JSON, "user_int", "TPB_0"):
    print(f'{r["name"]:60s} base=0x{r["base"]:x} off=0x{r["offset"]:x} '
          f'size=0x{r["size"]:x} type={r["type"]} -> {r["json"]}')
```

> **NOTE — pkl variant of the same iterator.** If a page needs the binary directly,
> swap Recipe B in (`db = RestrictedUnpickler(open(PKL,"rb")).load()`) and iterate the
> in-memory list — but only after Recipe A has proven the file inert, and only on a host
> that can hold ~514 MB. The streaming `.json` reader above is the default.

---

## 8. Confidence summary

| Claim | Confidence | Basis |
| --- | --- | --- |
| Protocol 4; pure-data pickle (0 dangerous opcodes / 33,065,559) | HIGH · OBSERVED | `genops` full scan |
| Top-level `list` of **323,198** dicts; 2 shapes (23-key / root 22-key) | HIGH · OBSERVED | `ijson` stream + `EMPTY_DICT/2` |
| Full **23-field** record schema with types + semantics | HIGH · OBSERVED | streamed field union |
| **5** access-domain views (USER/SECURE × INT/PCIe + USER_POD), bases | HIGH · OBSERVED | `parent_names==['ADDRESS_MAP']` filter |
| `base == address` (100%); `offset` = parent-relative (YAML-absent) | HIGH · OBSERVED | streamed equality count |
| **6-class** typed leaf taxonomy (sum = 323,198) | HIGH · OBSERVED | streamed `type` tally |
| 320 distinct `json` schemas vs Cayman YAML's 76 | HIGH · OBSERVED | streamed `json` set + YAML grep |
| pkl & json sizes 216,631,794 / 514,276,583 B; json `len`==pkl `len` | HIGH · OBSERVED | `ls -l` + streamed count |
| v5 SoC *interior* behind any address | * · INFERRED | no v5 byte-behavioral source; Cayman = different instance |
| Exact numeric YAML(34,858 Cayman) ↔ pkl(323,198 Maverick) prune map | MED | structural same-schema, not 1:1 re-derived |
| No in-band version field; arch/version from `json` build paths | MED | `/proj/maverick/…`, `udma v.0.0.2`/`v.0.0.4` |

*Cross-refs:* [`soc-master-map.md`](./soc-master-map.md) ·
[`block-schema-xref.md`](./block-schema-xref.md) · subtree pages
[TPB](./pkl-tpb-subtree.md) · [DMA](./pkl-dma-subtree.md) · [HBM](./pkl-hbm-subtree.md) ·
[PCIe/D2D](./pkl-pcie-d2d-fabric.md) · [INTC/SPROT](./pkl-intc-sprot-security.md) ·
[TOP_SP](./pkl-topsp-coverage.md).
