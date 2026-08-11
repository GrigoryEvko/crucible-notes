# Thread-Block Clusters & Distributed Shared Memory

> *Recovered from the decoded per-architecture SASS instruction tables shipped in
> nvdisasm V13.1.115 (CUDA 13.1), cross-validated against real ptxas v13.1.115 SASS
> emitted for SM90a (Hopper). Every opcode, bit-field, address constant, and scope
> mapping below was confirmed by assembling probe PTX with `ptxas -arch sm_90a` and
> reading the resulting machine code, the cubin's `EIATTR_*` records, and the exact
> ptxas diagnostics. Clusters and distributed shared memory are a Hopper (SM90)
> feature; the model is presented for SM90a. Where the encoding tables show Blackwell
> (SM100+) extending the mechanism, it is called out as decoder-only.*

The mechanism carries forward unchanged across the whole Blackwell lineup. The
decoded tables hold the same four `cgabar*` classes and one `setctaid`,
`endcollective` and `cgaerrbar` class on SM90 through SM121, and the same probe
kernel (`mapa.shared::cluster` + `st.shared::cluster` + `barrier.cluster.arrive`/
`wait`) assembles for `sm_90`, `sm_100`, `sm_120` and `sm_121` alike, emitting the
identical `S2UR SR_CgaCtaId` / `UCGABAR_ARV` / `UCGABAR_WAIT` / `CGAERRBAR`
sequence. Clusters and DSMEM are present on consumer Blackwell, not a
datacenter-only feature, and they need no arch-conditional target.

A **thread-block cluster** — called a **CGA** (*cooperative grid array*) throughout
the machine model and the SASS encoding — is a second level of the launch hierarchy
that sits between the grid and the CTA. A grid of clusters is launched; each cluster
is a small, fixed-shape group of CTAs (up to **8** portably, **16** with an opt-in)
that the hardware guarantees co-reside on **one GPC**. Co-residency is what makes the
cluster useful: the CTAs in a cluster can name each other's shared memory directly
(**distributed shared memory**, DSMEM), synchronize through a hardware cluster
barrier, and elect a single leader without leaving the GPC. This page is the
hardware-facing companion to the [Execution Model](execution-model.md) (warp
scheduling and scoreboards) and the [Memory Model](memory-model.md) (the scope ladder
and ordering primitives that DSMEM plugs into).

## The cluster as a launch dimension

ptxas records a kernel's cluster shape in the cubin's kernel-info (`nvinfo`) stream,
not in the SASS body. Two directives drive it, and they are mutually exclusive — ptxas
rejects a kernel that carries both with `Conflicting directives:
.reqnctapercluster and .maxclusterrank cannot both be specified`:

| PTX directive | cubin `EIATTR_*` record | Meaning |
|---|---|---|
| `.explicitcluster` | `EIATTR_EXPLICIT_CLUSTER` (NVAL) | Kernel opts into cluster launch |
| `.reqnctapercluster N` / `nx ny nz` | `EIATTR_CTA_PER_CLUSTER` (SVAL = `x y z`) | Fixed cluster shape, baked in |
| `.maxclusterrank N` | `EIATTR_MAX_CLUSTER_RANK` (SVAL = `N`) | Upper bound only; shape chosen at launch |

`.reqnctapercluster 8` emits `EIATTR_CTA_PER_CLUSTER` with value `0x8 0x1 0x1` (the
three cluster dimensions); `.maxclusterrank 8` emits `EIATTR_MAX_CLUSTER_RANK` with
value `0x8`. ptxas itself does **not** enforce the 8-CTA portable maximum — it accepts
`.reqnctapercluster 16` (and larger) without diagnostic, because the portable-vs-
opt-in limit (8 portable, 16 non-portable) is a launch-time policy enforced by the
driver and the hardware's GPC residency, not by the assembler. What ptxas bakes in is
the shape; what the GPC enforces is whether that many CTAs can actually co-reside.

The hardware reflects the same limits. The CGA error-status register exposes a
**5-bit** `CTA_ID_IN_CGA` field (encoding ranks 0…31 — the architectural ceiling) and
an **8-bit** `GPC_LOCAL_CGA_ID` field (which cluster, of those resident on the GPC,
the faulting CTA belongs to), alongside a per-cluster barrier-table buffer. The
practical cluster size is bounded well below 32 by how many CTAs fit on one GPC's SMs;
on Hopper that resolves to the 8-portable / 16-opt-in figures above.

## Cluster builtins: one true register, the rest from the param bank

Only **one** cluster quantity is a genuine hardware special register: the flat rank of
the CTA within its cluster. Everything else — the cluster's shape, the grid's cluster
count, the per-dimension cluster coordinates — the driver computes at launch and
deposits in the kernel constant bank `c[0x0][…]`, where ptxas reads it with `LDC`. The
table below is the exact lowering, recovered by reading each builtin's SASS in
isolation:

| PTX special register | SASS lowering | Source |
|---|---|---|
| `%cluster_ctarank` | `S2R Rd, SR_CgaCtaId` | hardware SREG |
| `%cluster_nctarank` (CTAs in cluster) | `LDC Rd, c[0x0][0x188]` | constant bank |
| `%cluster_nctaid.{x,y,z}` (cluster shape) | `LDC Rd, c[0x0][0x144 / 0x148 / 0x14c]` | constant bank |
| `%nclusterid.x` (grid in clusters) | `LDC Rd, c[0x0][0x15c]` | constant bank |
| `%cluster_ctaid.x` (this CTA's coord) | `CTAID.X % cluster_nctaid.x` | derived |
| `%clusterid.x` (this cluster's coord) | `CTAID.X / cluster_nctaid.x` | derived |

`SR_CgaCtaId` is the name nvdisasm prints; it is the only cluster identity the SM
holds in a register, and — as the next section shows — it is exactly the field the
hardware needs to turn a shared address into a remote one.

The two **derived** per-dimension coordinates are computed without an integer-divide
unit. `%cluster_ctaid.x = CTAID.X % cluster_nctaid.x` lowers to a
float-reciprocal remainder: `I2FP.F32.U32` → reciprocal-multiply → `F2I.U32.TRUNC.NTZ`
→ `IMAD.U32` → `IADD3 Rd, CTAID, -(q*n)`. The machine model carries a dedicated
reciprocal register (`SR_CgaRid.{x,y,z}` in the model) precisely so this division by
the cluster extent costs a multiply, not a divide.

## Distributed shared memory: the address encoding

DSMEM lets a thread read, write, or atomically update the shared memory of *another*
CTA in its cluster. PTX expresses this with the `.shared::cluster` address space and
the `mapa` (*map address*) instruction, which takes a local `.shared` address and a
target CTA rank and returns a `.shared::cluster` address that points at the same offset
in the *target* CTA's shared window.

The encoding is the central fact of the whole model: **the target CTA rank lives in
bits `[31:24]` of the 32-bit shared address**, with the intra-CTA shared offset in the
low 24 bits. ptxas builds it with a byte-permute. For `mapa.shared::cluster.u32 %r, smem+16, %rank`:

```
MOV  R0, 0x400               ; the local shared offset (here 0x400)
S2R  R5, SR_CgaCtaId         ; this CTA's own rank
S2R  R6, SR_SWINHI           ; the shared-window-high aperture base (upper 32 bits)
LEA  R0, R5, R0, 0x18        ; R0 = (own_rank << 24) | offset  -> self DSMEM pointer
PRMT R7, R9, 0x654, R0       ; overwrite byte 3 with the *target* rank (R9)
```

`PRMT … 0x654` selects, byte-by-byte, `{ R9[0], R0[2], R0[1], R0[0] }` — i.e. it keeps
the low three bytes of the assembled address (the shared offset) and replaces byte 3
with the low byte of the target rank. Net result: target rank in `[31:24]`, shared
offset in `[23:0]`. The `LEA … 0x18` first stages the CTA's *own* rank into byte 3 (so
a `mapa` to one's own rank is a no-op self-pointer); the `PRMT` then swaps in whichever
rank the program asked for. `SR_SWINHI` supplies the high 32 bits of the 64-bit DSMEM
aperture address; the rank-in-byte-3 value is the low 32.

The compiler uses the identical `[31:24]` convention internally. When a kernel runs
under CGA and spills, the shared-memory **spill/stack pointer** must itself be a legal
DSMEM (self-referencing) pointer, so ptxas injects the rank with the same scale:

```
S2R Rc, SR_CgaCtaId          ; own rank
LEA Rsp, Rsp, Rc, RZ, 0x18   ; rank into bits [31:24] of the stack pointer
```

The `0x18` (= 24) scale is the entire encoding in both paths. A pointer constructed
this way that names a CTA which is not resident, or an offset past the target's shared
allocation, faults the SM with the CGA error-status register's `CTA_NOT_PRESENT`,
`OOR_ADDR` (out-of-range), or `POISON` error code respectively — the hardware-level
guard rails on DSMEM addressing.

### Remote loads, stores, atomics

Once an address carries a rank in byte 3, the ordinary memory pipe handles it: a remote
load/store is a plain `LD`/`ST` through the shared aperture, and a remote atomic is
`ATOM` on that aperture. ptxas does **not** emit a distinct "remote" opcode — the
remoteness is entirely in the address bits:

| PTX | SASS |
|---|---|
| `ld.shared::cluster.u32` | `LD.E` (shared aperture, rank in `[31:24]`) |
| `st.shared::cluster.u32` | `ST.E` |
| `atom.shared::cluster.add.u32` | `ATOM.E.ADD.STRONG.GPU` |
| `red.shared::cluster.add.u32` | `ATOM.E.ADD.STRONG.GPU` (no result) |

The inverse of `mapa` is **`getctarank.shared::cluster`** — given a
`.shared::cluster` address it returns the CTA rank it points at. ptxas lowers it
to the matching byte-extract (`PRMT`/shift pulling byte 3 back into a register),
the read-side mirror of the `LEA …0x18` + `PRMT 0x654` build. No dedicated opcode
is involved; like the remote accesses above, the rank is purely an address-bit
manipulation.

## Scope: `.cluster` is a locality unit, not a memory-ordering domain

The PTX memory-ordering scope ladder is `.cta` ⊂ `.cluster` ⊂ `.gpu` ⊂ `.sys`, but the
SASS encoding has **no distinct cluster scope**. The strong/weak qualifier on every
load, store, atomic, and reduce is a single 4-bit field whose enumerated scopes are
`SM` (≈ CTA), `GPU`, and `SYS` — there is no `CGA`/`CLUSTER` value. ptxas therefore
maps `.cluster` onto `.GPU`:

| PTX scope | SASS scope |
|---|---|
| `.cta` | `.SM` |
| `.cluster` | `.GPU` |
| `.gpu` | `.GPU` |
| `.sys` | `.SYS` |

This is consistent with the cluster being a *scheduling and locality* construct rather
than a coherence domain: because a whole cluster lives inside one GPC under one GPU
memory subsystem, cluster-scope ordering on global memory is exactly GPU-scope
ordering. A `fence.acq_rel.cluster` / `fence.sc.cluster` lowers to
`MEMBAR.{ALL,SC}.GPU` followed by `ERRBAR; CGAERRBAR` — the GPU-scope membar provides
the ordering, and the two error barriers drain the warp- and cluster-level error state
so a fault on any CTA in the cluster is observed before the fence completes.

## The cluster barrier

`barrier.cluster.arrive` / `barrier.cluster.wait` (and the `cluster.sync` they
compose into) are the hardware cluster barrier — a single arrive/wait counter shared by
every CTA in the cluster. They lower to the **uniform** CGA-barrier opcodes:

| PTX | SASS | Opcode |
|---|---|---|
| `barrier.cluster.arrive` | `UCGABAR_ARV` | `UCGABARARV` = `13198` |
| `barrier.cluster.wait` | `UCGABAR_WAIT` | `UCGABARWAIT` = `15246` |
| (state read) | `UCGABAR_GET` | `UCGABARGET` = `11150` |
| (state init) | `UCGABAR_SET` | `UCGABARSET` = `10126` |
| cluster error barrier | `CGAERRBAR` | `CGAERRBAR` = `2902` |

The arrive/wait/get/set distinction is carried **entirely in the opcode** (four
different full-opcode values), not in a shared mode field. In the emitted 128-bit
word the arrive and wait forms share the low primary byte `0xc7` but differ in the
adjacent bits — the disassembled low-words are `0x79c7` (`UCGABAR_ARV`) versus
`0x7dc7` (`UCGABAR_WAIT`), confirming the distinct-opcode model directly. The only
in-instruction mode bits are `UCGABARARV.ucgabar_syncall` (bit 72 —
arrive-and-sync-all variant) and `CGAERRBAR.errbar_cga` (bit 72 — selects the
CGA-wide error barrier; the cluster `CGAERRBAR` low-word is `0x75ab`, distinct from
the warp-level `ERRBAR` `0x79ab`). A release-ordered arrive
(`barrier.cluster.arrive.release`) prefixes the barrier with
`MEMBAR.ALL.GPU; ERRBAR; CGAERRBAR`; the plain relaxed arrive is just `UCGABAR_ARV`.

The machine model treats the cluster-barrier state as a hazarded resource: there must
be **6 cycles** between a writer (`UCGABAR_ARV`, `UCGABAR_SET`) and a reader
(`UCGABAR_ARV`, `UCGABAR_GET`, `UCGABAR_WAIT`, or `EXIT`) of that state, and the
get/set/wait forms block deferred dependency barriers. The `UCGABAR_GET`/`_SET`/`_WAIT`
forms take a 6-bit uniform register operand (`DestU6` at `16:21` for GET, `SrcUB6` at
`32:37` for SET/WAIT); all four are uniform-datapath instructions guarded by a uniform
predicate (`upg` at `12:14`).

### Cluster exit ordering

Every kernel exit under CGA emits `ERRBAR` immediately followed by `CGAERRBAR` before
the `EXIT`. This drains the cluster's error state before any CTA tears down: a cluster
cannot release its DSMEM windows while a peer CTA might still be mid-fault on a remote
access. The exit `CGAERRBAR` is therefore the bookend to the launch-time barrier-table
setup.

## `elect.sync` — one-instruction leader election

Within a converged set of threads, `elect.sync` picks a single leader lane and returns
a predicate (true on the leader) plus the leader's lane id. It is its own SASS
instruction:

```
elect.sync %r|%p, 0xffffffff   ->   ELECT P0, UR62, PT
```

`ELECT` writes the leader predicate and a **uniform** register holding the elected lane
id; the membermask is a uniform-register operand. It is the canonical way to nominate
the single thread that issues a cluster-wide async operation — most often a TMA bulk
copy, which the machine model forces to single-thread issue and wraps in an
`ELECT`-guarded peeling loop when convergence isn't already guaranteed.

## Cluster-scope mbarriers and transactions

DSMEM's producer/consumer handshakes run on `mbarrier` objects that can live in a
*remote* CTA's shared memory. An `mbarrier.arrive.shared::cluster` targets a peer's
barrier through the same byte-3 rank encoding; the barrier op itself is the shared
arrive/phase-check family (`SYNCS`):

| PTX | SASS |
|---|---|
| `mbarrier.arrive.shared.b64` | `SYNCS.ARRIVE.TRANS64` |
| `mbarrier.arrive.expect_tx.shared::cluster.b64` | `SYNCS.ARRIVE.TRANS64.RED` |
| `mbarrier.try_wait.parity.shared.b64` | `SYNCS.PHASECHK.TRANS64.TRYWAIT` |
| `mbarrier.test_wait` | `SYNCS.PHASECHK.TRANS64.ONCE` |

The `.expect_tx` form carries a **transaction count** (the byte count a subsequent
async copy will deposit), so a consumer can wait on bytes-arrived rather than
thread-arrivals — the mechanism that lets one elected thread launch a TMA copy and the
whole cluster wait on its completion. A cluster-scope (extended, 64-bit) mbarrier
pointer is normalized to a 32-bit shared offset before issue; the upper bits that
selected the remote CTA are folded back in through the rank-in-byte-3 path above.

## multimem — multicast load-reduce and reduction

`multimem` operates on a *multicast* memory handle that fans a single access out to
several physical copies (the NVLink-multicast mechanism, distinct from but often paired
with clusters). It has its own load-reduce and reduce opcodes:

| PTX | SASS | Opcode |
|---|---|---|
| `multimem.ld_reduce.global.add.u32` | `LDGMC.E.ADD.32.STRONG.SYS` | `LDGMC_FP` = `13130` (and `LDGMC_INT`) |
| `multimem.ld_reduce.global.add.f32` | `LDGMC.E.ADD.F32.RN.STRONG.SYS` | |
| `multimem.ld_reduce.global.min.s32` | `LDGMC.E.MIN.S32.STRONG.SYS` | |
| `multimem.red.global.add.u32` | `REDG.E.ADD.STRONG.{GPU,SYS}` | `REDG_INT` = `4892`, `REDG_FP` = `4940` |
| `multimem.st.global.u32` | `STG.E` (multicast in the descriptor) | |

`LDGMC` ("load global multicast") loads-and-reduces across all multicast copies in one
instruction; its op is a 2-bit field at `88:89` (`ADD`=0, `MIN`=1, `MAX`=2, `F32ADD`=3
for FP). `REDG` is the fire-and-forget reduction; its integer op is a 3-bit field at
`87:89` (`ADD`/`MIN`/`MAX`/`INC`/`DEC`/`AND`/`OR`/`XOR` = 0…7). `multimem.st` carries no
dedicated opcode — the multicast routing rides in the memory descriptor of an ordinary
`STG`. Because multicast spans devices, these default to `SYS` scope.

## Cross-References

- [Execution Model](execution-model.md) — the warp scheduler and the scoreboards that
  the 6-cycle `UCGABAR` writer→reader latency plugs into.
- [Memory Model](memory-model.md) — the strong/weak × scope ladder (`SM`/`GPU`/`SYS`)
  that `.shared::cluster` and `.cluster`-scope ops map onto, and the `MEMBAR`/`FENCE`
  primitives the cluster fence composes from.
- [Control Flow](control-flow.md) — convergence and the membermask that `ELECT` and the
  cluster barriers require.
- [Architecture Evolution](architecture-evolution.md) — where the CGA / `UCGABAR*` /
  `ELECT` / `SETCTAID` family first appears (Hopper, SM90) and how the SM75→SM121
  instruction set grew.
- [Legality & Capabilities](legality-and-capabilities.md) — the shader-type gate that
  makes the cluster, DSMEM, and async families compute-only. These families assemble
  for the plain `sm_90`/`sm_100`/`sm_120` targets; the `a`-suffix gate applies to
  `wgmma`, `tcgen05` and `setmaxnreg`, not to clusters, DSMEM or TMA.
