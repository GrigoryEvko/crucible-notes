# Tree / CollNet (Dead Code)

> *All addresses on this page apply to `libnccom.so.2.31.24` from `aws-neuronx-collectives 2.31.24.0-1a31ba186` (build-id `9c00176c081788c9435d27d11bb40e92495463f0`; SONAME `libnccom.so.2`; internal tree **KaenaNCCL**, `/opt/workspace/KaenaNCCL/src`). ELF64 x86-64, **DYN, NOT stripped, with `.debug_info`**; `.text`/`.rodata` VMA == file offset for the cited band, so every `0x…` is both a file offset and an analysis VMA.*
> *Evidence grade: **Confirmed (byte-anchored, negative result)** — the deadness of `ncclGetBtree`/`ncclGetDtree` is proven four independent ways (zero `call` targets, zero `lea` fn-ptr takes, zero relocations, and an 8-byte-LE pointer sweep that finds their addresses only in DWARF/`.symtab`); every struct offset is from the binary's own DWARF (`gdb ptype /o`); the decompiled `ncclGetBtree` body is read from disassembly (`objdump -d 0x6d7d0..0x6d900`). Other versions will differ. · Part XII — Multi-Node Collectives · [back to index](../index.md)*

## Abstract

This page documents a **negative result**: the NCCL double-binary-tree reduction algorithm — single binary tree (`ncclGetBtree`) and double binary tree (`ncclGetDtree`) — and the CollNet collective family are **compiled into `libnccom.so` but are dead code**. Both tree builders are present and complete (faithful ports of upstream NVIDIA NCCL `graph/trees.cc`, addr2line → `trees.cc:31` and `trees.cc:88`), but a whole-file cross-reference sweep finds them with **zero callers, zero function-pointer references, and zero relocations**. Their addresses (`0x6d7d0`, `0x6d900`) appear in the binary only inside `.debug_info`/`.debug_aranges`/`.debug_line`/`.symtab` — never in `.text`, `.rodata`, `.data`, `.data.rel.ro`, or the GOT. Nothing the loader maps as executable or data can reach them. If you already read the positive counterpart ([algorithm-ring](algorithm-ring.md)) — which proves only RING is wired — this page is the matching proof for the *absent* half: the tree/CollNet machinery is structurally retained but functionally off.

The reference frame is upstream NCCL's three-graph topology build. In stock NCCL, `buildGraphTransportsRank` constructs **three** `ncclTopoGraph`s (ring / tree / collNet), `ncclTopoPreset` fills per-channel tree nodes via `ncclGetDtree → treeToParent/treeToChild0/treeToChild1`, `ncclTopoPostset` builds an inter-node tree pattern, and the channel setup loop P2P-connects each channel's `tree.up` and `tree.down[0..2]`. In this fork, every one of those tree steps is **removed or replaced**: exactly **one** graph is built — the RING template at `.rodata 0x8bec0` (`pattern = 4`, `collNet = 0`) — `ncclTopoCompute` has a **single** call site (`0x24f79`), `ncclTopoPreset` writes only the ring fields and reuses the `treeTo*` byte region for Neuron KangaRing data, `ncclTopoPostset` builds rings plus a Neuron pod inter-node graph (no `ncclGetDtree`), and the only two `ncclTransportP2pConnect` call sites in the whole binary (`0x25aec`, `0x48242`) connect ring neighbours and a mesh/RDH peer list — never a tree node.

The tree's storage survives for **ABI fidelity**, not function. Each `ncclChannel` still carries a `tree` slot (`+24`) and a `collTree` slot (`+44`), each a 20-byte `ncclTree {depth, up, down[3]}`; each `ncclTopoRanks` still carries `treeToParent[32]`/`treeToChild0[32]`/`treeToChild1[32]` (`+512`/`+640`/`+768`); and the `ncclPattern_t` enum still defines `ncclPatternTreeUp(4)…ncclPatternCollTreeDown(8)`. None of those slots is ever written by tree code. The device-communicator setup (`neuronDevCommSetup` @`0x44330`) `calloc`s `p2pnChannels × 512` bytes and blanket-`memcpy`s the channel array — including the **zero-filled** `tree`/`collTree` slots — to the device. The page is organized as: (1) the **deadness proof** (the xref/reloc/pointer-sweep evidence table); (2) the **retained algorithm** — `ncclGetBtree` decompiled as annotated pseudocode so the algorithm is documented even though it never runs; (3) the **tree-struct offset tables** from DWARF; (4) **what replaces the tree** at each integration point.

For reimplementation, the contract of this page is **what NOT to wire**:

- **Do not call the tree builders.** `ncclGetBtree` (`0x6d7d0`) and `ncclGetDtree` (`0x6d900`) are present and correct but must stay unreferenced. A reimplementer who wires `ncclTopoPreset` to call `ncclGetDtree` (as stock NCCL does) re-activates a path the rest of the fork does not support — the connect loop never connects tree peers, so a populated `tree.up`/`tree.down` would be built and then silently ignored at transport setup.
- **Build one graph, not three.** Only the RING template (`pattern = 4`, `collNet = 0`) at `0x8bec0` is constructed; `ncclTopoCompute` is called once. Do not allocate or search a tree or collNet graph.
- **Keep the struct slots, leave them zero.** The per-channel `tree`(+24)/`collTree`(+44) and per-rank `treeTo*`(+512/+640/+768) fields must exist for struct/ABI compatibility (the device receives a fixed 512-byte channel), but they are never populated. `neuronDevCommSetup` copies them as zeros; do not feed them.
- **There is no runtime chooser.** No cost model (`getAlgoInfo`/`computeColl`/`ncclTopoTuneModel`/`GetAlgoTime` are all absent), so nothing can ever *select* Tree or CollNet at runtime. The `"LL/LL128/Simple/Tree/Ring"` name table at `0x8be53` is dead constant data — referenced by no instruction. Protocol/algorithm is fixed upstream of this library (Neuron compiler / NRT plan).

| | |
|---|---|
| **Single binary tree builder** | `ncclGetBtree` @`0x6d7d0` (0x12c B) — `trees.cc:31` — **DEAD: 0 callers / 0 ptr-refs / 0 relocs** |
| **Double binary tree builder** | `ncclGetDtree` @`0x6d900` (0x434 B) — `trees.cc:88` — **DEAD: 0 callers / 0 ptr-refs / 0 relocs** |
| **Live ring counterpart (contrast)** | `ncclBuildRings` @`0x6d570` — **LIVE**, called from `ncclTopoPostset` (`call 0x6d570 @0x6c8cf`) |
| **Graphs built** | **1** — RING template `0x8bec0` (`pattern=4`, `collNet=0`); `ncclTopoCompute` 1 call site @`0x24f79` |
| **Tree node struct** | `ncclTree` — 20 B: `depth`@+0, `up`@+4, `down[3]`@+8 (`NCCL_MAX_TREE_ARITY=3`) |
| **Per-channel tree slots** | `ncclChannel.tree`@+24, `ncclChannel.collTree`@+44 — present, **never written** |
| **Per-rank tree fields** | `ncclTopoRanks.treeToParent[32]`@+512, `treeToChild0[32]`@+640, `treeToChild1[32]`@+768 — **never written** |
| **Tree pattern enums** | `ncclPatternTreeUp=4 … ncclPatternCollTreeDown=8` (DWARF) — present, never used |
| **CollNet flag** | `ncclTopoGraph.collNet`@+12 — set to **0** by the ring template; no collNet graph |
| **P2P connect sites (total)** | **2** — `0x25aec` (ring prev/next) · `0x48242` (mesh/RDH peer list) — **neither is a tree** |
| **Device copy of dead slots** | `neuronDevCommSetup` @`0x44330` — `calloc(p2pnChannels×512)` @`0x44351`, blanket `memcpy` @`0x4436e` |
| **Cost model** | **ABSENT** — `getAlgoInfo`/`computeColl`/`ncclTopoTuneModel`/`GetAlgoTime` no symbols |
| **Dead name table** | `"LL\0LL128\0Simple\0Tree\0Ring\0"` @`0x8be53` — referenced by no instruction |

> **QUIRK — the tree path is dead three layers deep: never built, never connected, never read.** A reimplementer porting NCCL's tree all-reduce will expect, at minimum, (a) a tree `ncclTopoGraph` searched alongside ring, (b) `ncclTopoPreset` calling `ncclGetDtree` to fill `treeToParent/Child0/Child1`, and (c) the channel-setup loop P2P-connecting `tree.up`/`tree.down[0..2]`. **None of the three exists here.** (a) `ncclTopoCompute` is called exactly once (`0x24f79`) with the RING template (`pattern=4`); no tree template exists in `.rodata`. (b) The `treeTo*` region of `ncclTopoRanks` (`+512`/`+640`/`+768`) is overwritten with KangaRing data on TRN3-SwitchV1 — the upstream `ncclGetDtree` block is gone. (c) The two total `ncclTransportP2pConnect` sites connect ring (`0x25aec`) and a mesh/RDH peer list (`0x48242`); the tree-connect that would pass `channel->tree.up`/`tree.down` is absent. The slots reach the device (`neuronDevCommSetup` `memcpy`s them) **zero-filled**. So even though `ncclGetBtree`/`ncclGetDtree` are byte-for-byte correct NCCL ports, no instruction in the binary can reach them, no struct field they would write is ever written, and no transport connects what they would produce. Document the algorithm (below) for completeness, but wire none of it.

---

## 1. The Deadness Proof — Zero-Caller Cross-Reference

### Purpose

The central claim of this page is reachability: in this binary, no execution path can ever invoke `ncclGetBtree` or `ncclGetDtree`. "Dead code" is a strong assertion that demands more than the absence of a call in one function — it requires proving the absence of *every* way a target could be reached: a direct `call`, a function-pointer `lea`/store into a table, a relocation the loader would patch, or a raw pointer constant in any mapped data. This part is that exhaustive sweep, with each line of evidence anchored to a tool observation and contrasted against the **live** sibling `ncclBuildRings` (`0x6d570`), which exhibits exactly the references the dead builders lack.

### Algorithm

The proof is a four-way reachability sweep. Each row is independently sufficient to fail "could be called"; together they are conclusive.

```c
// reachability sweep over libnccom.so.2.31.24  [HIGH: each row is a direct tool observation]
// targets: ncclGetBtree @0x6d7d0, ncclGetDtree @0x6d900
// control: ncclBuildRings @0x6d570 (LIVE — must show the references the targets lack)

(1) direct call sites:                                       // objdump -d | grep 'call .*<sym>'
      call <ncclGetBtree>  -> 0 matches                      // DEAD
      call <ncclGetDtree>  -> 0 matches                      // DEAD
      call <ncclBuildRings> -> 1 match @0x6c8cf (in Postset) // LIVE  <- the contrast

(2) function-pointer take (address loaded into a register/table):  // grep 'lea .*<sym>'
      lea <ncclGetBtree> / lea <ncclGetDtree> -> 0 matches   // never stored into a dispatch table

(3) ELF relocations naming the target addr:                  // readelf -r | grep 6d7d0|6d900
      -> 0 relocations                                       // loader patches nothing toward them

(4) 8-byte-LE raw pointer sweep of every mapped section:     // scan .text/.rodata/.data/.data.rel.ro/.got
      0x...6d7d0 / 0x...6d900 in .text         -> NONE
                                in .rodata      -> NONE
                                in .data        -> NONE
                                in .data.rel.ro -> NONE
                                in .got/.got.plt -> NONE
      same addrs in .debug_info / .aranges / .line / .symtab -> FOUND  // metadata only, not runtime
```

The only `<ncclGetBtree>`/`<ncclGetDtree>` mentions objdump emits are the functions' **own** internal branches (`jle`/`jne`/`jmp` with a `+0xNN` self-offset suffix, e.g. `jle 6d89b <…ncclGetBtree…+0xcb>`) — intra-function control flow, not inbound calls. No instruction outside the builder bodies names them.

### Function Map

| Symbol | Addr | Size | Liveness | Reachability evidence | Confidence |
|---|---|---|---|---|---|
| `ncclGetBtree(int,int,int*,int*,int*,int*)` | `0x6d7d0` | 0x12c | **DEAD** | 0 `call` targets · 0 `lea` · 0 relocs · ptr only in DWARF/symtab | HIGH |
| `ncclGetDtree(int,int,int*×8)` | `0x6d900` | 0x434 | **DEAD** | 0 `call` targets · 0 `lea` · 0 relocs · ptr only in DWARF/symtab | HIGH |
| `ncclBuildRings(int,int*,int,int,int*,int*)` | `0x6d570` | — | **LIVE** | `call 0x6d570` @`0x6c8cf` (in `ncclTopoPostset`) | HIGH |
| `ncclTopoCompute` | `0x6ab30` | — | **LIVE, RING-only** | single call site `0x24f79`, RING template `0x8bec0` | HIGH |
| `ncclTransportP2pConnect` | `0x3e9c0` | — | **LIVE** | 2 sites: `0x25aec` (ring) · `0x48242` (mesh/RDH) — no tree | HIGH |
| `getAlgoInfo` / `computeColl` / `ncclTopoTuneModel` / `GetAlgoTime` | — | — | **ABSENT** | no symbol — no runtime algorithm chooser exists | HIGH |

### Considerations

The pointer sweep is the decisive row, because it closes the one loophole a caller-only scan leaves open: an *indirect* call through a function-pointer table. If `ncclGetBtree`'s address were stored anywhere a dispatcher reads — a vtable, a `.data.rel.ro` jump table, an initialized `.data` array of handler pointers — the raw 8-byte value `0x...6d7d0` would appear in that section's bytes. It does not appear in any mapped section; it appears only in `.debug_info`/`.debug_aranges`/`.debug_line` (the DWARF that records *where the function is* for a debugger) and `.symtab` (the name→address table). Neither is loaded as code or data at runtime — both are pure metadata. The sweep was validated against two **controls** that *do* resolve: the RING template at `0x8bec0` is loaded by a `movdqa 0x66f6c(%rip)` at `0x24f4c`, and the print format at `0x92bc0` is loaded by a `lea` at `0x6ba16` — so the tool correctly finds live `.rodata` references when they exist, which means the *absence* of any reference to the dead builders and the dead name table is real, not a scanning artifact.

---

## 2. The Retained Algorithm — `ncclGetBtree` Decompiled

### Purpose

Even dead, the builder is worth documenting: it is a verbatim port of upstream NCCL's single binary tree (`graph/trees.cc:31`), so a reimplementer who *wanted* a tree path (in a future build, or a different family) has the exact algorithm here, byte-anchored. `ncclGetBtree` computes, for one rank, its position in a single binary tree over `nranks` ranks: its parent (`up`), up to two children (`down0`, `down1`), and a `parentChildType` bit that distinguishes the two halves of the tree. `ncclGetDtree` (`0x6d900`, 0x434 B) builds a *pair* of such trees — `tree0` directly, `tree1` as a shift/reflect mirror — so the double-binary-tree all-reduce can split bandwidth across two complementary trees (the Sanders/Träff construction); its body inlines the `ncclGetBtree` logic twice plus the parity-driven mirror (`and $0x1, rank` at `0x6d916` selects odd-shift vs even-reflect).

### Algorithm

The register mapping is read from the prologue: `edi = nranks`, `esi = rank`, `rdx→r10 = up`, `rcx→r11 = down0`, `r8 = down1`, `r9 = parentChildType`. The core is a lowest-set-bit walk to find the rank's "level bit", from which parent and children follow by XOR/add/subtract, each clamped to `< nranks`.

```c
// ncclGetBtree — 0x6d7d0  [HIGH: body read from disasm 0x6d7d0..0x6d8fc; trees.cc:31]
// outputs: *up=parent rank, *down0/*down1=child ranks, *parentChildType=which half
void ncclGetBtree(int nranks, int rank, int *up, int *down0, int *down1, int *parentChildType):
    // degenerate: nranks <= 1  (cmp $1,nranks; jle 0x6d89b)
    if (nranks <= 1):
        *up = -1; *down0 = -1; *down1 = -1;            // 0x6d8a4: movl $-1 into up/down0/down1
        *parentChildType = degenerate_pct(rank);       // single-node tree: no parent, no children
        return;

    // find 'bit' = the level delta: the lowest power of two that, combined with the
    // lowest-set-bit walk, locates this rank's level in the binary tree.
    // The loop doubles 'bit' while (2*bit < nranks); 'childStride' = bit>>1 is the child delta.
    int bit = 1;                                        // 0x6d7ef
    while ((bit & rank) == 0 && 2*bit < nranks):        // 0x6d800 test; 0x6d808 cmp; 0x6d80a jg
        bit <<= 1;                                      // 0x6d806 add eax,eax
    int childStride = bit >> 1;                         // 0x6d80e sar $1  — half-bit = child delta

    if (rank == 0):                                     // 0x6d810 test rank,rank; je 0x6d8a4
        *up = -1;                                       // root has no parent
        // children of the root are the two halves (computed by the child loops below)
    else:
        // parent = (rank ^ bit), folded back into [0,nranks) when it overshoots:
        int up_candidate = rank ^ bit;                  // 0x6d818 / 0x6d847 xor rank,bit
        if (up_candidate >= nranks):                    // 0x6d850 cmp; 0x6d852 cmovle
            up_candidate = fold_into_range(up_candidate, bit);   // or (bit<<2),(rank^bit)
        *up = up_candidate;                             // 0x6d864 mov edx,(r10)
        *parentChildType = (rank >= up_candidate);      // 0x6d857 cmp; 0x6d859 setle/setge -> (r9)

    // children: down0 = rank - childStride, down1 = rank + childStride, each walking
    // childStride downward (sar $1) until a valid child rank < nranks is found, else -1:
    *down0 = first_valid_child(rank, -childStride, nranks);   // 0x6d836 sub; loop 0x6d878
    *down1 = first_valid_child(rank, +childStride, nranks);   // 0x6d88c movl $-1 if none
    // 0x6d891 mov eax,(r11) -> *down0 ;  0x6d896 mov ebx,(r8) -> *down1
    return;                                             // 0x6d89a ret, eax=0 (ncclSuccess)
```

> **NOTE — the pseudocode is the *retained* algorithm, not a live path.** Every offset cited above is inside `0x6d7d0..0x6d8fc`, and every one of those instructions is a *self-relative* branch or a store through the output pointers `r8`/`r9`/`r10`/`r11` that the (never-supplied) caller would pass. The function is correct NCCL — a verifier can confirm it against upstream `graph/trees.cc:31` — but §1 proves no caller exists. The `down0`/`down1` clamping (a child rank past `nranks` becomes `-1`) and the `parentChildType` bit (`rank >= up`, distinguishing the two tree halves so the reduce/broadcast phases know their direction) are exactly the upstream semantics; they are documented here so a reimplementer who revives the tree family has the contract, not because anything calls it.

### Function Map

| Symbol | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `ncclGetBtree` | `0x6d7d0` | 0x12c | single binary tree: parent/two children/parentChildType from lowest-set-bit walk | HIGH (dead) |
| `ncclGetDtree` | `0x6d900` | 0x434 | double binary tree: `tree0` direct + `tree1` shift/reflect mirror (parity branch `0x6d916`) | HIGH (dead) |

### Considerations

`ncclGetDtree` (0x434 B, ~3.5× the size of `ncclGetBtree`) inlines the binary-tree logic for `tree0`, then constructs `tree1` as the complementary mirror: the head test `and $0x1, rank` at `0x6d916` branches on `nranks` parity. For **odd** `nranks`, `tree1` is `tree0` evaluated on a cyclically shifted index (`+1 mod nranks`) so the two trees together cover every rank as both an interior and a leaf node; for **even** `nranks`, `tree1` is the reflection `x → (nranks-1-x)` of `tree0`. This is the upstream double-binary-tree construction verbatim — its purpose in stock NCCL is to run two trees in parallel so each rank's NIC carries data for both, doubling tree bandwidth. None of that runs here; the function body was confirmed present and parity-branched (the `and $0x1` head, `0x6d916`) but was not transcribed instruction-by-instruction (the `tree0`/`tree1` output stores were not individually pinned — the exact mirror arithmetic is **MED**), because the deadness (§1) makes it moot for this fork. The live ring counterpart in the *same* `trees.cc`/`rings.cc` translation-unit band (`0x6d570` ring builder, `0x6d7d0` Btree, `0x6d900` Dtree — adjacent) is `ncclBuildRings`, documented on [algorithm-ring](algorithm-ring.md) §3.

---

## 3. The Tree-Struct Offset Tables (DWARF)

### Purpose

The tree storage survives in three structs even though no tree code writes it. A reimplementer must reproduce these layouts for **struct/ABI fidelity** — the device receives a fixed 512-byte `ncclChannel`, and the per-rank exchange blob is a fixed 960-byte `ncclTopoRanks` — but must leave the tree fields zero. All offsets below are from the binary's own DWARF (`gdb ptype /o`); they are byte-exact, not inferred.

### Data Tables

```text
ncclTree  (20 B) — the tree NODE; one per channel slot, NEVER populated
  off  size  field    meaning
  +0   4     depth    tree depth (root=0)
  +4   4     up       parent rank (-1 if root)             <- ncclGetBtree would write
  +8   12    down[3]  child ranks (NCCL_MAX_TREE_ARITY=3)  <- ncclGetBtree would write
```

`ncclChannel` (512 B; useful inner struct 112 B) carries **two** `ncclTree` slots — the binary tree and the collNet tree — both present, both never written:

| Field | Offset | Type | Liveness |
|---|---|---|---|
| `ring` | +0 | `ncclRing` (24 B: `prev`,`next`,`userRanks`,`devUserRanks`) | **LIVE** — written by ring code |
| `tree` | +24 | `ncclTree` (20 B) | **PRESENT, never populated** |
| `collTree` | +44 | `ncclTree` (20 B) | **PRESENT, never populated** (collNet) |
| `id` | +64 | int | LIVE |
| `peers` | +72 | `ncclPeer*` | LIVE |
| `devPeers` | +80 | `ncclPeer*` | LIVE |
| `workFifo` | +88 | `ncclWork*` | LIVE (ring work ring) |
| `workCount` | +96 | int | LIVE |
| `workFifoTail` | +104 | uint64 | LIVE |

`ncclTopoRanks` (960 B) — the per-rank exchange blob — carries the per-channel tree fields upstream NCCL fills from `ncclGetDtree`:

| Field | Offset | Liveness |
|---|---|---|
| `ringRecv[32]` | +0 | **LIVE** — written by Preset/Postset |
| `ringSend[32]` | +128 | LIVE |
| `ringPrev[32]` | +256 | LIVE |
| `ringNext[32]` | +384 | LIVE |
| `treeToParent[32]` | +512 | **PRESENT, NEVER written by tree code** |
| `treeToChild0[32]` | +640 | **PRESENT, NEVER written** |
| `treeToChild1[32]` | +768 | **PRESENT, NEVER written** — region reused for KangaRing |
| `JbogKangaringPath[4][4]` | +896 | **LIVE (fork)** — KangaRing data overwrites the `treeTo*` tail on TRN3-SwitchV1 |

`ncclPattern_t` (4 B enum, DWARF) still defines every tree/collTree variant — present in the enum, never used at runtime:

| Enumerator | Value | Status |
|---|---|---|
| `ncclPatternRing` | 0 | the only pattern reached |
| `ncclPatternRingTwice` | 1 | unused |
| `ncclPatternPipelineFrom` / `…To` | 2 / 3 | unused |
| `ncclPatternTreeUp` | 4 | **present, never used** |
| `ncclPatternTreeDown` | 5 | **present, never used** |
| `ncclPatternTreeUpDown` | 6 | **present, never used** |
| `ncclPatternCollTreeUp` | 7 | **present, never used** |
| `ncclPatternCollTreeDown` | 8 | **present, never used** |
| `ncclPatternMesh` | 9 | (Neuron mesh) |

And `ncclTopoGraph` (33080 B) carries the `collNet` selector the ring template sets to 0:

| Field | Offset | Ring template value (`0x8bec0`) | Meaning |
|---|---|---|---|
| `id` | +0 | 0 | graph 0 |
| `pattern` | +4 | **4** | `NCCL_TOPO_PATTERN_RING` — fixed RING |
| `crossNic` | +8 | 2 | |
| `collNet` | +12 | **0** | collNet **disabled** |
| `minChannels` / `maxChannels` | +16 / +20 | 1 / 16 | |
| `intra[8192]` | +52 | — | per-rank ring members |
| `inter[64]` | +32820 | — | per-channel NIC ids |
| `crossracktorack` | +33076 | — | **Neuron-added** (no stock-NCCL analogue) |

> **CORRECTION (ATREE-1) — `ncclPattern_t` is the enqueue-side op enum, distinct from the topology `NCCL_TOPO_PATTERN_*`.** It is tempting to equate `ncclPatternTreeUp(4)` with the value `4` stored in `ncclTopoGraph.pattern` — but `4` in the graph struct is `NCCL_TOPO_PATTERN_RING`, **not** a tree. The two enumerations are different namespaces: `ncclPattern_t` (the enqueue op-pattern: `Ring=0 … TreeUp=4 … Mesh=9`, DWARF-confirmed above) selects how an op decomposes, while `NCCL_TOPO_PATTERN_*` (`1=BALANCED_TREE, 2=SPLIT_TREE, 3=TREE, 4=RING`) selects how the topology search shapes channels. The ring template's `pattern=4` is `NCCL_TOPO_PATTERN_RING`. Both enumerations carry tree variants; **both are inert in this fork** — the op-pattern enum because no enqueue/cost path reaches it, the topo-pattern enum because only the RING value-4 template is ever constructed. A reimplementer must not cross-map the two by numeric value.

### Considerations

The `treeTo*` region of `ncclTopoRanks` (`+512`/`+640`/`+768`, 384 bytes total) is the clearest tell that the tree storage is **repurposed, not merely idle**: on TRN3-SwitchV1, `ncclTopoPreset`/`Postset` write KangaRing data into the tail of this region and the adjoining `JbogKangaringPath[4][4]` (`+896`) — the same bytes upstream NCCL fills from `ncclGetDtree`. So the offsets are vestigial (the *field names* `treeToParent`/`treeToChild0`/`treeToChild1` persist in DWARF for source compatibility with stock NCCL), but the *storage* is recycled for the fork's ring variant. The two per-channel `ncclTree` slots (`+24`, `+44`) are not recycled — they stay zero and are copied as zeros to the device (§4). A reimplementer that uses stock NCCL's `ncclTopoRanks` layout (which ends `treeToChild1[32]` at `+896` with no `JbogKangaringPath`) will mis-place the KangaRing fields; the layout here is fork-specific past `+512`, and [algorithm-ring](algorithm-ring.md) §4 owns the KangaRing materialize that writes it.

---

## 4. What Replaces the Tree — and Where the Zeros Go

### Purpose

Closing the negative result: at each integration point where stock NCCL builds, connects, or reads a tree, this fork either removed the step or substituted a Neuron-original one. This part maps the replacements and proves the dead slots still reach the device — zero-filled — so the boundary is explicit for a device-side (PRIMS) cell.

### Algorithm

```c
// the three integration points where upstream NCCL touches the tree, and the fork's substitute
// [HIGH: call sites byte-anchored; replacement semantics from algorithm-ring/topology cells]

// (1) GRAPH BUILD — upstream: 3 graphs (ring/tree/collNet); here: 1 (ring)
buildGraphTransportsRank @0x24ef0:
    movdqa 0x8bec0 -> ringGraph;         // 0x24f4c — RING template (pattern=4, collNet=0)
    ncclTopoCompute(comm, &ringGraph);   // 0x24f79 — ONE call, RING only
    // *** upstream's 2nd/3rd ncclTopoCompute (tree, collNet): ABSENT ***

// (2) PRESET — upstream: ncclGetDtree -> treeToParent/Child0/Child1; here: ring + KangaRing
ncclTopoPreset @0x6c1f0:
    fill ringRecv/ringSend/ringPrev/ringNext only;
    if neuronIsTrn3SwitchV1Family():
        write KangaRing into the treeToChild1/JbogKangaringPath region;  // reuses tree bytes
    // *** upstream's per-channel ncclGetDtree call: REMOVED ***

// (3) POSTSET — upstream: inter-node tree pattern; here: ring + pod inter-node graph
ncclTopoPostset @0x6c490:
    ncclBuildRings(...);                          // call 0x6d570 @0x6c8cf — LIVE ring order
    ncclTopoGetP2pPodInterNodeGraph(...);         // Neuron pod inter-node (replaces inter-node tree)
    // *** upstream's inter-node ncclGetDtree: REMOVED ***

// (4) CONNECT — upstream: P2pConnect tree.up + tree.down[0..2]; here: ring prev/next only
setupChannel loop (inlined in 0x24ef0):
    if (nRanks != 1):
        ncclTransportP2pConnect(comm, ch, 1,&ring.prev, 1,&ring.next, 0);  // 0x25aec — RING
    // *** upstream's P2pConnect(channel->tree.up, channel->tree.down[0..2]): ABSENT ***
    // the ONLY other P2pConnect (0x48242, neuronExpandConnectivity) is a MESH/RDH peer list

// (5) DEVICE COPY — the zero-filled tree slots still reach the device
neuronDevCommSetup @0x44330:
    sz = comm->p2pnChannels << 9;        // 0x44343 movslq +0x452c; 0x4434a shl $9 (×512)
    dst = calloc(sz);                    // 0x44351
    memcpy(dst, comm /*channels base*/, sz);   // 0x4436e — blanket copy INCL. zero tree/collTree slots
```

### Function Map

| Integration point | Upstream NCCL (tree) | This fork | Addr | Confidence |
|---|---|---|---|---|
| Graph build | 3 graphs (ring/tree/collNet) | **1** graph (RING template) | `0x24f79` (1 call) | HIGH |
| Per-channel preset | `ncclGetDtree → treeTo*` | ring fields only; KangaRing reuses tree region | `0x6c1f0` | HIGH |
| Inter-node | inter-node tree pattern | `ncclTopoGetP2pPodInterNodeGraph` | `0x6c490` | HIGH/MED |
| Channel connect | P2pConnect `tree.up`/`tree.down[0..2]` | P2pConnect ring `prev`/`next` only | `0x25aec` | HIGH |
| 2nd connect site | — | mesh/RDH peer list (not tree) | `0x48242` | HIGH |
| Device copy | (tree slots populated) | blanket `memcpy` of **zero-filled** tree slots | `0x44330` | HIGH |

### Considerations

`neuronDevCommSetup`'s copy is byte-exact and worth pinning: `movslq 0x452c(%rdi),%r12` loads `comm->p2pnChannels` (field @`comm+0x452c`), `shl $0x9,%r12` multiplies by 512 (one `ncclChannel`), `calloc` allocates the device-side channel array (`0x44351`), and `memcpy(dst, comm, sz)` (rsi = comm base, the channels-array head) copies the channels — `tree`(+24) and `collTree`(+44) included. Because no tree code ever wrote those slots, they are zero at copy time and arrive at the device as zeros. This is the latent boundary a PRIMS cell must check: **if** a Neuron device kernel ever read `channel.tree`/`channel.collTree`, it would read zeros (a correctness hazard) — but on this host side they are provably never populated, so the device program must not depend on them. The inter-node substitute, `ncclTopoGetP2pPodInterNodeGraph` (called from `ncclTopoPostset`), takes the structural role upstream's inter-node tree pattern held; whether *it* internally forms a tree-like inter-pod schedule (versus a ring/mesh) is **OPEN** — its body was not traced here, and the question is owned by [transport-efa](transport-efa.md). What is closed is the libnccom-local claim: this library never builds, connects, or reads an `ncclTree`.

---

## Related Components

| Name | Relationship |
|---|---|
| inherited `graph/trees.cc` (`ncclGetBtree`/`ncclGetDtree`) | reusable verbatim from public NCCL `2.31.24`; ported but **dead** in this fork |
| `ncclBuildRings` (`0x6d570`, same TU band) | the **live** ring counterpart of the dead tree builders; [algorithm-ring](algorithm-ring.md) §3 |
| `ncclTopoGetP2pPodInterNodeGraph` (`0x6c030`) | Neuron substitute for upstream's inter-node tree pattern (in `ncclTopoPostset`) |
| KangaRing materialize (`Preset`/`Postset` tail) | reuses the `treeTo*` byte region (`+512…`) for the fork's multi-rail ring |
| `neuronDevCommSetup` (`libnrt` bridge, `0x44330`) | blanket-`memcpy`s the zero-filled `tree`/`collTree` slots to the device |
| libnrt collective layer | the device side is **tree-free at the symbol level** — no tree builder exists there at all ([algorithm-taxonomy](../collectives/algorithm-taxonomy.md) §5) |

## Cross-References

- [Ring Algorithm Construction (Host-Side)](algorithm-ring.md) — the **positive counterpart**: the live RING wiring this page proves is the *only* algorithm built; its §1 names these dead builders, §3 documents the live `ncclBuildRings`
- [Algorithm Taxonomy (Ring / Mesh / Hier / Kangaring / RDH)](../collectives/algorithm-taxonomy.md) — the device-side proof that the collective stack is tree-free; §5 cites this page as the host-side half (where the dead tree code lives)
- [Topology Detection and Graph Build](topology.md) — produces the single RING `ncclTopoGraph` (`pattern=4`, `collNet=0`) and the `treeTo*`/KangaRing region of `ncclTopoRanks` this page's dead fields share
- [back to index](../index.md)
