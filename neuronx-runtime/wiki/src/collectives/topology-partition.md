# Topology Partitioning (Union-Find)

> *All addresses, offsets, and sizes on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present. The partitioner's DWARF comp-paths are `inc/utils/disjoint_sets.h` and `inc/utils/connected_components.h`; its sole consumer is `/opt/workspace/KaenaRuntime/enc/enc.cc`. `.text`/`.rodata` VMA == file offset, so every `0x…` is an analysis VMA. Other versions will differ.*
> *Evidence grade: **Confirmed (struct-, callgraph-, and disasm-anchored)** — both struct layouts are verbatim from `structures.json` (DWARF); the four member functions' addresses and sizes are from `functions.json`; the `find_set` 4-deep unroll and self-recursive tail are read line-by-line from the decompile at `@0x141dc0`; the sole-consumer fact is callgraph-confirmed (`get_connected` and the ctor have exactly one caller each, `enc_parse_src_target_pairs`). · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

When a NEFF declares a collective not as a symmetric replica group but as an explicit list of `(src_rank → target_rank)` participant pairs — the form an `all-to-all-v` or a point-to-point `send`/`recv` schedule takes — the runtime faces a graph question it cannot answer by enumeration: *given this flat edge list, which ranks are transitively reachable from THIS device, and therefore belong to my collective sub-domain?* The answer is a textbook **union-find**. libnrt ships exactly that — a `disjoint_set<unsigned int, true>` (112 B) and a `connected_components<unsigned int, true>` (168 B) helper — and runs it once, at NEFF-load time, to derive a rank's replica group from the `source_target_pairs` edge set. This page documents that partitioner.

The reference frame is the classic disjoint-set forest with **union-by-rank** and **path-compressed find**. Each `(src, target)` pair is one undirected edge; the connected-components helper makes a singleton set per distinct vertex (rank), unions the two endpoints of every edge, and then asks `get_connected(local_device_id)` for the member list of the component containing the local rank. That member list is the replica group. The data layout is the minimal one: the `disjoint_set` is *two* `std::unordered_map<uint, uint>` — a parent map (`representative` @+0, `x → parent[x]`) and a height map (`rank` @+56) — and the `connected_components` embeds that disjoint set plus the edge list and a deduplicated vertex `universe`. There is no separate `make_set`/`union_sets` symbol: both are inlined into the ctor; only `find_set` survives as a standalone function, and it is the one piece with non-obvious shape — a **4-deep manually-unrolled** parent walk with a self-recursive tail for chains longer than four, performing full path compression on the way back.

This page documents three artifacts a reimplementer must reproduce: (1) the **`disjoint_set` (112 B) and `connected_components` (168 B) layouts**, grounded in DWARF offsets; (2) the **`find_set` algorithm** — the 4-deep unroll, the self-recursive fallback, and the write-back path compression that touches every node on the path; and (3) the **partition chain** — `build_cc_context` → `enc_parse_src_target_pairs` → the ctor (build) → `get_connected` (query) → `enc_parse_replica_groups` (group) — that turns a NEFF's `src_target_pairs` into an `enc_src_target_pairs_info::participants` vector. A correction in place pins the one structural surprise: the mesh composer makes **no** union-find call; the partitioner's sole consumer is `enc_parse_src_target_pairs`.

For reimplementation, the contract is:

- **The disjoint-set forest is two hash maps, not an array.** `representative` (@+0) is the parent map (`x → parent[x]`); `rank` (@+56) is the union-by-rank height map. `make_set(v)` is `representative[v] = v; rank[v] = 0`. `rank` is **write-only at union time** — `find_set` never reads it. A reimplementer who folds rank into the find walk diverges from the binary (which is correct: union-by-rank consults rank only during union, and union is fully inlined into the ctor).
- **`find_set` is a 4-deep unrolled walk with a self-recursive tail and full path compression.** Four inline parent-lookups handle chains of depth ≤4; a chain deeper than four falls through to `call find_set(parent)` (the recursion at `@0x141ed8`); on unwind every node on the path is rewritten to point at the found root. Reproduce both the unroll and the write-back, not just a generic while-loop — the write-back is what makes the amortized cost near-constant.
- **The partition is built once, queried once, per `src_target_pairs` set.** The ctor copies the edge list into `edges`, dedups endpoints into `universe`, `make_set`s every vertex, then unions each edge's two endpoints by rank. `get_connected(g_device_id)` re-runs `find_set` over `universe`, buckets each vertex under its root into a `map<root, members[]>`, and returns the local device's component root. The member vector under that root is the replica group handed to `enc_parse_replica_groups`.

| | |
|---|---|
| **`disjoint_set<uint,true>`** | 112 B — `representative` @+0 (parent map), `rank` @+56 (height map) |
| **`connected_components<uint,true>`** | 168 B — `ds` @+0, `edges` @+112, `universe` @+136, `num_vertices` @+160 |
| **`find_set`** | `disjoint_set<uint,true>::find_set(uint)` `@0x141dc0` (335 B) — 4-deep unroll + self-recursion @`0x141ed8` |
| **Parent-map accessor** | `_Map_base<uint,…>::operator[]` `@0x140320` (the `representative`/`rank` map `operator[]`) |
| **Build (ctor)** | `connected_components<uint,true>::C1(vector<vector<uint>>)` `@0x144400` (3401 B) — make_set + union-by-rank inlined |
| **Query** | `connected_components<uint,true>::get_connected(uint)` `@0x142fb0` (1667 B) → `pair<map<uint,vector<uint>>, uint>` |
| **Dtor** | `disjoint_set<uint,true>::~disjoint_set()` (D1==D2) `@0x13c250` (231 B) — frees `rank` then `representative` |
| **Partition entry** | `build_cc_context` `@0x26fee0` (446 B, `tdrv/instr_collectives.c`) |
| **Sole consumer** | `enc_parse_src_target_pairs` `@0x130620` (4548 B, `enc/enc.cc`) |
| **Group materializer** | `enc_parse_replica_groups` `@0x11e850` (3895 B) → `participants` vector |
| **Consumer record** | `enc_src_target_pairs_info` (80 B) — `pairs` @+0, `participants` @+24, `signature` @+48, `rank`/`rank_n`/`replica_group_id`/`src_target_pairs_id` @+64/68/72/76 |

> **CORRECTION (TOPO-1) — `alg_mesh_build_full_mesh` makes no union-find call; the partitioner's sole consumer is `enc_parse_src_target_pairs`.** An earlier collectives survey recorded a "65× `alg_mesh_build_full_mesh`" caller edge onto `find_set` (and the [Mesh Composer](mesh-composer.md) page still cross-references the grouped-mesh path as "union-find over peer rids … coalesce ranks into groups of 6"). That edge is an **address-band artifact**: the vendored libstdc++14 STL leaves physically sit in the same `0x14xxxx` band as the `alg_mesh_*` code, and a band-based caller heuristic mis-attributed them. The callgraph (`functions.json`) is unambiguous: `alg_mesh_build_full_mesh` `@0x125fb0` and `alg_mesh_build_subtypes` `@0x133cd0` have **zero** callees to `find_set` `@0x141dc0`, `get_connected` `@0x142fb0`, or the ctor `@0x144400`. The callers of `get_connected` are exactly `enc_parse_src_target_pairs` (`@0x130b6f`, `@0x130bb4`); the caller of the ctor is exactly `enc_parse_src_target_pairs` (`@0x130af0`). The `ENC_ALG_GROUPED_MESH` `rank_n % 6 == 0` grouping is done by `alg_mesh_build_subtypes`' own arithmetic, not by this disjoint set. Confidence **HIGH** (callgraph-verified; both mesh builders' callee lists contain none of the three union-find functions).

---

## 1. The Disjoint-Set Forest

### Purpose

`disjoint_set<unsigned int, true>` is the union-find substrate: a forest of rooted trees where each tree is one set, the root is the set's canonical representative, and two ranks are in the same set iff they share a root. The `true` template argument selects union-by-rank (height-balanced union); the `unsigned int` element type is the global device-id space. It is never used standalone — it is embedded as the first member of `connected_components` (@+0) — but its layout and `find_set` are the heart of the partition.

### Layout — `disjoint_set<unsigned int,true>` (112 B, ordinal 12124)

The forest is stored as two `std::unordered_map<uint, uint>`, not an index array — because the vertex set is the *sparse* global device-id space (a sub-world NEFF names a handful of arbitrary global rank ids, not a dense `0..n`). The first map is the parent forest; the second is the per-root height counter that union-by-rank consults.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `representative` | +0 | `std::unordered_map<uint,uint>` (56 B) | parent links: `x → parent[x]`; a root has `parent[x] == x` | HIGH |
| `rank` | +56 | `std::unordered_map<uint,uint>` (56 B) | union-by-rank height counter, keyed by root; **written only at union, never read by `find_set`** | HIGH |

Each `std::unordered_map<uint,uint>` is the libstdc++14 `_Hashtable` (56 B: `_M_buckets`/`_M_bucket_count`/`_M_before_begin`/`_M_element_count`/`_M_rehash_policy`(16)/`_M_single_bucket`). The `representative@+0` / `rank@+56` ordering is doubly confirmed: `find_set` passes `this` directly (offset 0) to the parent-map `operator[]`, and the destructor (`@0x13c250`) frees the **`rank` map first, then the `representative` map** — reverse declaration order, the C++ guarantee, so `representative` is the earlier-declared (lower-offset) member.

> **QUIRK —** the `rank` map is write-only on the read path. `find_set` (`@0x141dc0`) touches **only** the `representative` map — it never reads `rank`. The height counter is consulted exclusively inside the ctor's inlined union step (`rank[rootA]` vs `rank[rootB]`). This is correct union-by-rank: rank governs *which* root becomes the parent during a union, and union is fully inlined into the ctor, so a standalone `find_set` has no reason to read it. A reimplementer who reads rank in `find_set` (e.g. to break ties) is adding a behavior the binary does not have; the only place rank matters is the union.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `disjoint_set<uint,true>::find_set(uint)` | `0x141dc0` | 335 B | path-compressed find; 4-deep unroll + self-recursion (§2) | HIGH |
| `disjoint_set<uint,true>::~disjoint_set()` (D1==D2) | `0x13c250` | 231 B | frees `rank` then `representative` map (reverse decl order) | HIGH |
| `_Map_base<uint,pair<uint const,uint>,…,true>::operator[]` | `0x140320` | — | the `representative`/`rank` map accessor; `find_set`'s only callee | HIGH |

> **CORRECTION (TOPO-2) — `find_set`'s map accessor is `@0x140320`, not the seed-cited `0x1419e0`/`0x141cd0`.** An earlier scaffold attributed `find_set`'s `operator[]` to `0x1419e0`/`0x141cd0`; those serve unrelated `mesh_group_types` and `int→…` maps. The first `operator[]` call inside `find_set` is at `@0x141ddd` (`call …ixERS2_`), whose reloc target is `0x140320`, and every one of `find_set`'s sixteen `operator[]` calls resolves there. Confidence **HIGH** (disasm reloc + callgraph callee list).
>
> **CORRECTION (TOPO-3) — `find_set` has SIXTEEN `operator[]` calls, not seventeen.** An earlier draft counted "seventeen". Disassembly of `find_set` `@0x141dc0` (body `0x141dc0`–`0x141f0f`) has exactly sixteen `call 0x140320` (`operator[]`) sites — at `141ddd, 141df0, 141e1b, 141e2e, 141e43, 141e56, 141e66, 141e74, 141e82, 141e90, 141eab, 141ebe, 141ece, 141ee6, 141ef4, 141f02` — plus one self-recursive `call 0x141dc0` at `@0x141ed8` (which is **not** an `operator[]`). The off-by-one came from counting the self-recursive tail call as a seventeenth accessor. Confidence **HIGH** (disasm site enumeration).

---

## 2. `find_set` — Unrolled Walk and Path Compression

### Purpose

`find_set(x)` returns the root of `x`'s tree and, as a side effect, compresses the path: every node from `x` up to the root is rewritten to point directly at the root, so a later `find_set` on any of them is O(1). It is the only standalone union-find op (make_set and union are inlined into the ctor), and it is the one with non-obvious structure — the compiler emitted a **manually-unrolled** walk of depth four with a self-recursive tail, not a generic loop.

### Algorithm

The decompile at `@0x141dc0` reads four parent-lookups in a nest: read `parent[x]`; if it equals `x`, `x` is the root, return. Otherwise descend one level, comparing `parent[p]` against `parent[parent[p]]` to test whether `p` is already a root. Four such levels are inlined; a fifth (a chain deeper than four) falls through to `call find_set(parent)` at `@0x141ed8`. On the way back out, each visited node is rewritten to the discovered root — the write-back path compression. The sixteen `operator[]` calls in the body are the read/write halves of these four levels plus the final return read (the self-recursive tail at `@0x141ed8` is a `call find_set`, not an `operator[]` — see CORRECTION TOPO-3).

```c
// disjoint_set<uint,true>::find_set @0x141dc0
// rep = this->representative (the parent map @+0); rep[k] is operator[] @0x140320.
// 4-deep manual unroll; self-recursive tail @0x141ed8 for chains > 4; full path compression.
function find_set(this, x):                         // 0x141dc0
    rep = &this->representative;                     // offset 0; rank (@+56) is NOT touched here

    // depth 0: is x already a root?
    if rep[x] == x:                                  // 0x141ddd: first operator[]
        return rep[x];                               // x is its own parent -> root

    p1 = rep[x];                                     // p1 = parent(x)
    if rep[p1] == p1:                                // depth 1: is parent a root?
        rep[x] = p1;                                 // compress: x -> p1
        return p1;

    p2 = rep[p1];                                    // p2 = parent(parent(x))
    if rep[p2] == p2:                                // depth 2
        root = p2;
    else:
        p3 = rep[p2];                                // depth 3
        if rep[p3] == p3:
            root = p3;
        else:
            p4 = rep[p3];                            // depth 4
            if rep[p4] == p4:
                root = p4;
            else:
                // chain deeper than 4: recurse on the parent of p4
                root = find_set(this, rep[p4]);      // 0x141ed8 — the self-recursive tail
                rep[p4] = root;                       // compress p4

            rep[p3] = rep[p4];                        // compress p3 -> root
        rep[p2] = rep[p3];                            // compress p2 -> root
    rep[p1] = rep[p2];                                // compress p1 -> root
    rep[x]  = rep[p1];                                // compress x  -> root
    return rep[x];
```

> **QUIRK —** the unroll depth of four is a compiler/source choice, not a hardware bound. For a chain of length ≤4 the entire find is straight-line — no call, no loop — which is the common case once the forest has been compressed by earlier finds. A chain longer than four recurses, and the recursion depth is bounded by the tree height, which is itself bounded by the vertex count (`num_vertices` @+160, i.e. the distinct rank ids in the `src_target_pairs` set). With union-by-rank that height is `O(log n)`, so the recursion is shallow and the `O(depth)` stack use is a non-issue for any realistic world size. A reimplementer is free to use a plain iterative two-pass find (walk to root, then compress) — it is behaviorally identical; the unroll is purely a codegen detail of `inc/utils/disjoint_sets.h:29`.

> **GOTCHA —** the path compression writes back the root to *every* node on the path, including the deep recursive tail. A reimplementer who compresses only the queried node `x` (leaving `p1..p4` pointing mid-chain) is still correct but loses the amortized near-constant cost the binary achieves — and, more subtly, will produce a *different* parent forest after the same sequence of finds. Since `get_connected` (§3) re-runs `find_set` over the whole `universe`, a partial-compression reimplementation reaches the same component membership (the roots are identical) but with more map writes; the membership result is unaffected, only the cost. Reproduce the full write-back to match the binary's forest state exactly.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `find_set` (self-recursion) | call `@0x141ed8` | — | the depth-≥5 tail: `call find_set(rep[p4])` | HIGH |
| `find_set` (first `operator[]`) | call `@0x141ddd` → `0x140320` | — | `rep[x]` — the depth-0 root test | HIGH |
| `_Map_base::operator[]` | `0x140320` | — | the parent-map accessor (16 call sites in `find_set`; see CORRECTION TOPO-3) | HIGH |

---

## 3. The Partition Chain

### Purpose

The partitioner answers one question per `src_target_pairs` set: *which ranks share a connected component with the local device?* The chain that asks it runs at NEFF-load time, inside the collective-instruction translators. `build_cc_context` gates it on the `src_target_pairs_initialized` flag; `enc_parse_src_target_pairs` builds the connected-components graph over the NEFF edges, queries the local device's component, and hands the resulting member list to `enc_parse_replica_groups`, which materializes the `participants` vector that the rest of the collective stack treats as the replica group.

### Entry Point

```text
instr_col_translate_{pcprid,ptc,ptc2_full}        ── NEFF collective-instruction translators
  └─ build_cc_context (0x26fee0)                    ── tdrv/instr_collectives.c; gates on
       │                                              cc_ctx->src_target_pairs_initialized
       └─ enc_parse_src_target_pairs (0x130620)     ── enc/enc.cc; per kbin_src_target_pairs entry
            ├─ connected_components::C1(edges) (0x144400)   ── BUILD: make_set + union-by-rank   §3.1
            ├─ get_connected (0x142fb0)                     ── QUERY: find_set over universe     §3.2
            │    └─ find_set (0x141dc0) ×|universe|          ── re-run per vertex
            └─ enc_parse_replica_groups (0x11e850)          ── GROUP: members -> participants
                 └─ encd_get_global_comm (0x24e9d0)         ── g_device_id = the query key
```

### Algorithm — build, query, group

```c
// enc_parse_src_target_pairs @0x130620 — partition one src_target_pairs set into a replica group.
// Runs once per kbin_src_target_pairs entry the NEFF declares.
function enc_parse_src_target_pairs(enc_ctx, stp_set):           // 0x130620
    glb = encd_get_global_comm(nec_dev, ctx_id);                 // 0x24e9d0
    if !glb: reject  // "Global communicator was not created. Unable to parse targets. (hint: was
                     //  `nrt_load_collectives` called?)"
    g_device_id = glb->g_device_id;                              // THIS rank's vertex id (the query key)

    for stp in stp_set.pairs[0 .. num_pairs]:                    // each kbin_src_target_pairs
        // P1. Build the edge list: each (src, target) pair is one 2-element inner vector.
        info.pairs = vector<vector<uint>>();                     // enc_src_target_pairs_info.pairs @+0
        for (src, target) in stp:
            info.pairs.push_back({src, target});                 // one undirected edge

        // P2. Deterministic edge ordering so the partition (and the §comm-context signature) is
        //     reproducible across ranks. Sort by element value via the pair-compare lambda.
        sort(info.pairs, enc_parse_src_target_pairs_info::lambda);

        // P3. BUILD — connected_components ctor @0x144400:
        //       copy edges -> cc.edges; dedup endpoints -> cc.universe; num_vertices = |universe|;
        //       make_set(v): rep[v]=v, rank[v]=0  for all v;
        //       for each edge (a,b): union_by_rank(find_set(a), find_set(b)).
        cc = connected_components(info.pairs);                   // 0x144400 (make_set+union inlined)

        // P4. QUERY — get_connected(g_device_id) @0x142fb0:
        //       re-run find_set over cc.universe; bucket each vertex under its root;
        //       returns ( map<root, members[]>, find_set(g_device_id) ).
        (components, my_root) = cc.get_connected(g_device_id);   // 0x142fb0

        // P5. GROUP — the local device's component is the replica group.
        members = components[my_root];                           // the member-rank vector
        rg_view = { ptr: members.data, count: members.size };
        if enc_parse_replica_groups(cc_ctx, &rg_view, &info):    // 0x11e850
            reject  // "Failed to parse replica group generated from source_target_pairs %d"
        // -> info.participants (@+24) now holds the sub-domain ranks;
        //    info.{rank, rank_n, replica_group_id} are set.

        // P6. Commit the per-set record into cc_ctx->src_target_pairs (a1+288).
        push_back(cc_ctx->src_target_pairs, info);               // _M_realloc_append 0x145150
```

### Algorithm — the ctor's inlined build (P3 detail)

The ctor is where `make_set` and the union live; neither has a standalone symbol. It copies the edge list, dedups vertices through a temporary counting map into `universe`, sizes the forest, and then unions every edge's endpoints by rank.

```c
// connected_components<uint,true>::C1(vector<vector<uint>> edges) @0x144400
function connected_components(this, edges):                      // 0x144400
    this->edges = copy(edges);                                   // @+112
    // dedup endpoints into universe via a temp unordered_map<uint,uint> 'vertices' counter
    seen = unordered_map<uint,uint>();
    for e in edges:
        for v in e:                                              // v = e[0] (src) or e[1] (target)
            if v not in seen:
                seen[v] = 1;
                this->universe.push_back(v);                     // @+136
    this->num_vertices = this->universe.size();                 // @+160 (= finish - start)

    // make_set for every vertex (inlined — no make_set symbol)
    for v in this->universe:
        this->ds.representative[v] = v;                          // rep[v] = v  (singleton root)
        this->ds.rank[v]          = 0;                           // height 0

    // union every edge by rank (inlined — no union_sets symbol)
    for e in edges:
        rootA = find_set(&this->ds, e[0]);                       // 0x141dc0 (compresses)
        rootB = find_set(&this->ds, e[1]);
        if rootA != rootB:
            if this->ds.rank[rootA] >= this->ds.rank[rootB]:     // union-by-rank
                this->ds.representative[rootB] = rootA;
                if this->ds.rank[rootA] == this->ds.rank[rootB]:
                    ++this->ds.rank[rootA];                      // equal heights -> grow A
            else:
                this->ds.representative[rootA] = rootB;
```

> **NOTE —** `get_connected` returns its result by sret as `pair<unordered_map<uint, vector<uint>>, uint>`: `.first` is the full `root → members[]` map (every component, not just the local one), and `.second` is `find_set(g_device_id)` — the local device's component root. The decompile shows `get_connected` called at two sites in `enc_parse_src_target_pairs` (`@0x130b6f`, `@0x130bb4`); both return identical data (one fetch reads `.second` for the root, the other reads `.first` for the member map). Whether this is one source-level call the compiler duplicated or two source calls is **MEDIUM** — the net effect is identical either way, because the map and the root are derived from the same forest in the same pass.

> **GOTCHA —** the query key is the **global** device id (`glb->g_device_id`, from `encd_get_global_comm`), not a NEFF-local index. The `src_target_pairs` edges name global rank ids, and the partition is a global-numbering operation; a reimplementer who keys `get_connected` on a node-local or NEFF-relative rank index will query the wrong vertex and return a component that does not contain the local device — silently producing an empty or wrong replica group. The same global numbering is what lets the partition reconcile across ranks (each rank queries its own `g_device_id` against the *same* edge set and gets a consistent component decomposition).

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `build_cc_context` | `0x26fee0` | 446 B | gates the partition on `src_target_pairs_initialized`; drives `enc_parse_src_target_pairs` | HIGH |
| `enc_parse_src_target_pairs` | `0x130620` | 4548 B | the sole consumer: build → query → group, per `src_target_pairs` set | HIGH |
| `connected_components::C1(edges)` | `0x144400` | 3401 B | BUILD: make_set + union-by-rank inlined | HIGH |
| `get_connected` | `0x142fb0` | 1667 B | QUERY: `find_set` over `universe`; `(map<root,members>, root)` by sret | HIGH |
| `enc_parse_replica_groups` | `0x11e850` | 3895 B | GROUP: member list → `participants`; sets `rank`/`rank_n`/`replica_group_id` | HIGH |
| `encd_get_global_comm` | `0x24e9d0` | — | yields `g_device_id`, the query key (the local vertex) | HIGH |
| `disjoint_set::~disjoint_set` | `0x13c250` | 231 B | temp cleanup after build/query | HIGH |

---

## 4. The Consumer Record (`enc_src_target_pairs_info`)

### Purpose

The 80-byte `enc_src_target_pairs_info` is the per-set record the partition produces: it carries both the input edges (`pairs`) and the derived output (`participants`), plus the 16-byte op-signature that ranks reconcile during communicator setup. It is stored in `enc_context.src_target_pairs` (stride 80) and is the point-to-point analog of `enc_replica_group_info`; both feed the comm-pool key and the two-level MD5 op-signature ([Comm Context and Bootstrap](comm-context.md)).

### Layout — `enc_src_target_pairs_info` (80 B, ordinal 10139)

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `pairs` | +0 | `vector<vector<uint>>` (24 B) | the `(src,target)` edge vectors — the CC ctor's input | HIGH |
| `participants` | +24 | `vector<uint>` (24 B) | the **derived** replica-group member ranks (`get_connected` output → `enc_parse_replica_groups`) | HIGH |
| `signature` | +48 | `uint8_t[16]` | the per-set op-signature MD5, reconciled across ranks | HIGH |
| `rank` | +64 | `int` | this rank's position in the derived group | HIGH |
| `rank_n` | +68 | `int` | derived group size | HIGH |
| `replica_group_id` | +72 | `int` | the group index this set maps to | HIGH |
| `src_target_pairs_id` | +76 | `int` | back-link to the NEFF `kbin_src_target_pairs` entry | HIGH |

> **NOTE —** the partitioner family owns **zero** strings — `find_set`, the ctor, `get_connected`, and the dtor are pure template leaves with no user-facing text (every diagnostic, e.g. `"Failed to parse replica group generated from source_target_pairs %d"` and `"Global communicator was not created. Unable to parse targets…"`, belongs to the consumer `enc_parse_src_target_pairs`). A reimplementer matching the binary by string anchors will find none on the union-find functions themselves; pin them by struct ordinal, address, and the callgraph instead.

---

## Related Components

| Name | Relationship |
|---|---|
| `enc_parse_src_target_pairs` (`@0x130620`) | the sole consumer; builds the CC graph and queries the local component |
| `build_cc_context` (`@0x26fee0`) | gates and drives the partition from the NEFF collective-instruction translators |
| `connected_components::C1` / `get_connected` (`@0x144400` / `@0x142fb0`) | the build and query halves of the partitioner |
| `find_set` (`@0x141dc0`) | the path-compressed find shared by build and query |
| `enc_parse_replica_groups` (`@0x11e850`) | turns the queried member list into the `participants` vector |
| `enc_src_target_pairs_info` | the 80-byte record the partition populates (`pairs` in, `participants` out) |

## Cross-References

- [Comm Context and Bootstrap](comm-context.md) — parses replica groups (`enc_parse_replica_groups`) and reconciles the per-set MD5 signature; this partitioner feeds the `participants` it consumes
- [Mesh Composer (alg_mesh_initializer)](mesh-composer.md) — the grouped-mesh `rank_n % 6` grouping that an earlier survey wrongly attributed to this union-find (see CORRECTION TOPO-1); the mesh builders make no `find_set` call
- [NEFF Metadata Schema (simdjson-DOM Consumed Keys)](../neff/metadata-schema.md) — where `src_target_pairs` enters the runtime: `def.json`'s `src_target_pairs` key → `parse_src_target_pairs` → the edge set this partitioner reads
- [Overview: the Collective-Compute Architecture](overview.md) — the two-layer composer/emitter map; the `src_target_pairs` replica-group derivation is the point-to-point / all-to-all-v variant of replica-group production
- [back to index](../index.md)
