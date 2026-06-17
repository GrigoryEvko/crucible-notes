# Pod Election (UltraServer / NUTD)

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. This page owns the pod-election engine — `v3/neuron_pelect.c` (1993 lines) and `v3/neuron_pelect.h` (117 lines), both read in full; every state value, register offset, mask, and constant below is transcribed verbatim. The DHAL thunks that reach these functions (`npe_*_v3`/`npe_*_v4`, the `npe_neighbor_eng_ids` table) are owned by [dhal-v3](dhal-v3.md) / [dhal-v4](dhal-v4.md); the ioctl that drives `npe_pod_ctrl` by [ioctl-pod](ioctl-pod.md); the FW_IO MiscRAM register block (`neuron_fw_io.h`), the B-link bases (`v3/address_map.h`), and the UDMA M2M transport are boundary edges, pinned by value. Other driver versions renumber these lines.*
> *Evidence grade: **Confirmed (source-anchored)** — open GPL C, no decompilation; the whole engine, both files, transcribed directly. · Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

A Trn3/V3 UltraServer is sixteen Neuron devices wired as one **node**, and up to four such nodes cabled together through PCIe "B-links" into a **pod**. There is nothing physical that a pod election *changes* — no register flips a die, no fabric reconfigures (`:12-14`). The election is a distributed *agreement*: every node independently discovers who its ring neighbors are, all nodes agree on a node-id numbering (0..3) and on a single pod-unique serial number, and that numbering then drives how downstream code maps cores and addressing across the pod — most visibly the V3 [die-flip](dhal-v3.md#5-the-die-flip-a-software-nc-remap-not-a-register), which renumbers cores on the odd-numbered (1/3) nodes. The file's own TODO calls the subsystem **NUTD — Neuron UltraServer Topology Discovery** (`:7`), which is the better name: discovery, not voting.

Within a node, device 0 is the **primary** and devices 1..15 are **secondaries** (`:51`). The primary runs the actual node-id math; the secondaries only vet their own neighbor links and report pass/fail. The cross-node exchange is done by reading each neighbor's **MiscRAM** over a B-link with a two-descriptor UDMA memory-to-memory read (`npe_pod_neighbor_io_read`, `:404`): the primary publishes its neighbors' serial numbers into its own MiscRAM, then reads the neighbor's published copy back to confirm the ring closes. Results are written back into MiscRAM (`FW_IO_REG_POD_ELECTION_STS` / `POD_SERNUM`) so a driver unload/reload reloads the answer without re-running the election (`:32-34`, `:1184-1191`). A single kernel thread, `"neuron election"`, drives all 16 devices **serially in the same order on every node** — that ordering is the deadlock-avoidance invariant for a cross-node, lock-step protocol (`:91-93`).

Three platform types share the engine. **STD** is not a pod — the DHAL thunks short-circuit before reaching here. **ULTRASERVER** runs the full link election described above. **PDS** (a discovery-server SKU) runs *no* link election at all: node-id, node-count, and server-id are **location-based**, read from FW_IO plus a hard-coded serial→node table (`npe_pds_config_init`, `:1863`). This page documents the election state machine, the per-role flows, the node-id algorithm, the MiscRAM result format, the lame-duck fault model, the operating-mode reinterpretation matrix, the PDS path, and the `npe_*` op surface the DHAL thunks bind to.

For reimplementation, the contract is:

- **The topology model** — 16 devices/node (dev0 primary, 1..15 secondary), up to 4 nodes in a PCIe B-link ring, the left/right link-pair wiring, and the `lr_mask` (`0x3` both / `0x2` right / `0x1` left) that selects which links the election uses.
- **The internal state machine** — `INIT → IN_PROGRESS → {SUCCESS, FAILURE}`, its two trigger paths (post-reset and on-demand ioctl), and the lame-duck retry that degrades `0x3 → 0x2 → 0x1` before giving up.
- **The neighbor-exchange protocol** — the MiscRAM register block, the serial-number / election-data / status handshake, the cabling checks (link-pair serials must match; secondaries publish their `device_index` to catch mis-wiring), and the completion interlock that clears election data on exit.
- **The node-id election** — the serial-ordering rule that picks the leader (lowest serial) and assigns 0..3 across a 2- or 4-node ring, plus the `diagonal` term that distinguishes the two non-adjacent nodes.
- **The MiscRAM result word** — the `[cnt:31..24][node_id:23..16][lr_mask:15..8][sts:7..0]` packing, the four `MR_STS` values, and the `DEPRECATED(1)` cache-invalidation trick.
- **The PDS divergence** — location-based assignment with no link election, and the `(rack<<1)|server` → node-id map.

| | |
|---|---|
| **Owned source** | `v3/neuron_pelect.c` (1993 lines) · `v3/neuron_pelect.h` (117 lines) |
| **Singleton state** | `static ndhal_pelect_data` (`:257-273`) — one `struct ndhal_v3ext_pelect` for the whole driver |
| **Driver thread** | `kthread_run(npe_election_thread_fn, "neuron election")` (`:1638`, `:1734`) |
| **Node size** | 16 devices: `pnd[16]` (`:249`), dev0 primary, 1..15 secondary (`:51`) |
| **Pod size** | up to 4 nodes; `node_cnt ∈ {2,4}` from election (`:777`/`:787`), PDS up to 4 (`:1915`) |
| **Neighbor transport** | UDMA M2M read of neighbor MiscRAM over PCIe B-link (`npe_pod_neighbor_io_read`, `:404`) |
| **Result store** | MiscRAM `FW_IO_REG_POD_ELECTION_STS 0x190` + `POD_SERNUM 0x194/0x198` (survives reload) |
| **Election timeout** | `userver_etimeout` param, default **600 s** (`:194`); lame-duck extension `NPE_ETIMEOUT_EXTENSION_MS = 120 s` (`:204`) |
| **Neighbor engine ids** | V3 `{{36,68}L,{4,100}R}` (`dhal_v3:214`) · V4 `{{40,72},{8,104}}` (`dhal_v4:132`) |
| **DHAL binding** | `ndhal->ndhal_npe.*` — thunked from `neuron_dhal_v3.c:1944-1952` / `_v4.c:440` |

---

## Topology — Node, Pod, and the B-Link Ring

A node is sixteen devices; only the primary (device 0) carries a B-link to a *neighbor node*. The secondaries' link checks are intra-node cabling vetting — they confirm the local wiring is sane and that each is reachable at its expected `device_index` — but the cross-node ring runs through the primaries. Each primary has a **left** link pair and a **right** link pair; "pair" because every B-link is two PCIe SENGs read in lock-step and cross-checked (a mis-cabled pair reads two different serial numbers and is rejected, `:528-533`).

```text
                       POD = up to 4 NODES in a PCIe B-link RING
                       (lr_mask 0x3 → both link pairs active)

         node 0 (leader, lowest serial)            node 1
        ┌────────────────────────────┐   right    ┌────────────────────────────┐
        │ dev0  PRIMARY  ── L pair ───┼────────────┤ ─── R pair  dev0 PRIMARY    │
        │ dev1..dev15  SECONDARIES    │            │ dev1..dev15  SECONDARIES    │
        └───────────┬────────────────┘            └───────────┬─────────────────┘
              L pair │  (left link)                    R pair  │
        ┌───────────┴────────────────┐            ┌───────────┴─────────────────┐
        │ dev0  PRIMARY               │   right    │ dev0  PRIMARY                │
        │ dev1..dev15  SECONDARIES    │────────────┤ dev1..dev15  SECONDARIES    │
        └────────────────────────────┘            └─────────────────────────────┘
         node 2                                     node 3
                          ▲                                  ▲
            diagonal = node 0 reads node 1's PUBLISHED right-neighbor serial,
            which is node 3 → that is the "diagonal" term in npe_get_node_id.

   Per primary, FOUR neighbor-IO DMA engines (a 2×2 pnio matrix):
        pnio[0][0], pnio[0][1] = LEFT  pair  (V3 eng ids 36, 68)
        pnio[1][0], pnio[1][1] = RIGHT pair  (V3 eng ids  4,100)
   base B0 address chosen by  eng_id / V3_NUM_DMA_ENG_PER_SENG(=32)  →
        {V3_PCIE_B0_0_BASE .. B0_3_BASE} = {0x10.., 0x14.., 0x18.., 0x1c..}  (:413,:425)
   remote read addr = base + V3_APB_MISC_RAM_OFFSET(0x8006c84000) + reg_offset   (:436)
```

A node never reads its own diagonal directly. It learns the diagonal by reading what its *left* neighbor **published** as that neighbor's right-hand serial — i.e. the election-data exchange (`:910`, `diagonal = nbr_serial_number_copy[0][1]`). This is the only way a 4-node ring closes with each node holding a single B-link in each direction: the published copies relay the far corner.

> **QUIRK —** "16 devices on a node" but only **one** of them (device 0) holds the inter-node B-links. The other fifteen run [`npe_secondary_device_vet`](#secondary-flow--npe_secondary_device_vet) purely to vet *local* cabling and to write their own `device_index` as election data so a mis-cable is caught (`:1027`/`:1044`). A reimplementer must not model all 16 devices as ring participants — fifteen are local-only health reporters, and the primary requires *all fifteen* to pass (`secondary_good_cnt == 15`, `:914`) before it will commit a node-id.

---

## The Internal State Machine

The engine has four internal states (`enum neuron_pod_state_internal`, `:210-215`) and two distinct status vocabularies — the internal `pod_state_internal` that drives the thread, and the `MR_STS` byte persisted in MiscRAM (`:221-226`). They are deliberately separate: the internal state is RAM-only driver state, the MiscRAM status is the durable cross-reboot cache.

```text
internal state (ndhal_pelect_data.pod_state_internal)        MiscRAM MR_STS byte
  0  INIT                  before any pod formation            0  INIT
  1  ELECTION_IN_PROGRESS  thread running                      1  DEPRECATED  (old success;
  2  ELECTION_SUCCESS      pod formed                              invalidates stale caches)
  3  ELECTION_FAILURE      failed / not formed                 2  FAILURE
                                                               3  SUCCESS
```

### State Transitions

```c
// STATES: INIT(0) -> IN_PROGRESS(1) -> {SUCCESS(2), FAILURE(3)}
// All writes below take ndhal_pelect_data.lock unless noted.

// -- TRIGGER A: driver load, after ALL 16 device resets complete ----------
function npe_election_exec_on_rst(nd, reset_successful):     // :1146
    if !reset_successful: return 0                           // :1154
    lock()
    pnd[nd->device_index] = nd                               // :1162  cache device ptr
    if nd->device_index == 0 && platform == ULTRASERVER:     // :1167
        if pod_ctl & CLR_MISCRAM:                            // :1172  test override
            clear neighbor election data + sts=INIT + sernum=0
        miscram_sts_info_get(&sts, &lr, &node_id, &node_cnt) // :1181  read cached result
        if sts == MR_STS_SUCCESS:                            // :1184  CACHE HIT
            load lr_mask/node_id/node_cnt/serial from MiscRAM
            pod_state_internal = SUCCESS                     // :1189  no election runs
            goto done
        if pod_ctl & RST_SKIP_ELECTION:                      // :1196
            pod_state_internal = FAILURE; goto done
    if pod_state_internal != INIT || (pod_ctl & RST_SKIP_ELECTION):
        goto done                                            // :1205  decision already made
    if !npe_all_rst_complete(): goto done                    // :1211  wait for all 16
    if platform == PDS:                                      // :1217
        npe_pds_config_init(); goto done                     // :1218  NO link election
    npe_initiate_election(nbr_data_read_timeout)             // :1222  -> wakes thread

// -- TRIGGER B: on-demand, ioctl POD_CTRL -> npe_pod_ctrl ------------------
function npe_pod_ctrl(nd, ctrl, mode, timeout, *state):      // :1571
    lock()
    if ctrl == REQ_SINGLE_NODE: ctrl = SET_MODE; mode = X1   // :1581  legacy rewrite
    switch ctrl:
      REQ_KILL:                                              // :1586
        if state == IN_PROGRESS: kill_election = true        //        abort running election
        elif state == INIT:      pod_state_internal = FAILURE
      SET_MODE:                                              // :1593
        if npe_pod_state_busy():            return -EBUSY    // :1594  INIT or IN_PROGRESS
        if mode set && mode != current:     return -EEXIST   // :1598  locked until cores free
        if !npe_mode_is_supported(mode):    return -ENOTSUPP // :1603
        ndhal_pelect_data.mode = mode
      REQ_POD:                                               // :1610
        if platform == PDS: return 0                         // :1614  no election on PDS
        if ncrwl_range_mark_cnt_get()==0 && all_rst_complete:// :1619  GATE: no cores in use
            npe_initiate_election(timeout * 1000); ret = 0
        else: ret = -EAGAIN                                  // :1625
    _npe_get_pod_status(state, &node_id)                     // :1633  always report status
    unlock()

// -- npe_initiate_election: INIT/anything -> IN_PROGRESS (caller holds lock)
function npe_initiate_election(timeout):                     // :1104
    if pod_state_internal == IN_PROGRESS: return             // :1106  idempotent
    kill_election = false
    nbr_data_read_deadline = now() + timeout                 // :1109
    pod_state_internal = IN_PROGRESS                          // :1111
    node_id = -1; node_cnt = 0; pod_serial_num = 0           // :1112-1114  reset result
    lr_mask = lr_mask_default                                // :1115  restore link mask
    wake_up(&wait_queue)                                     // :1116  -> election thread
```

> **GOTCHA —** the on-demand `REQ_POD` path is gated **only** on `ncrwl_range_mark_cnt_get() == 0` and "all devices reset" (`:1619`) — there is no capability or attach check at this layer; the `POD_CTRL` ioctl is an *ungated state-changer* once a caller holds a `/dev/neuronN` fd (see [ioctl-attack-surface](../security/ioctl-attack-surface.md)). A reimplementation that assumes the runtime library is the only caller will not reproduce the actual reachability: any `O_WRONLY` opener can request, kill, or re-mode the pod election, and the `mark_cnt==0` gate is the *whole* serialization against a model that is mid-execution.

---

## The Neighbor-Exchange Protocol

The cross-node exchange is a handshake over MiscRAM. Each device has a small register file (MiscRAM) reachable both locally (`readl`/`writel` to BAR0, `:708-715`) and remotely over a B-link (UDMA M2M read, `:404`). The protocol writes to *local* MiscRAM to publish, and reads *remote* MiscRAM to observe a neighbor.

### MiscRAM Register Block

| Reg | Offset | Written by | Read by | Meaning |
|---|---|---|---|---|
| `FW_IO_REG_SERIAL_NUMBER_LO` | `0x38` | firmware | neighbor | this device's 64-bit serial number (`:514`) |
| `FW_IO_REG_LH_NEIGHBOR_SERNUM_HI/LO` | `0x180/0x184` | self (election data) | neighbor | serial of my **left** neighbor (`:720-721`) |
| `FW_IO_REG_RH_NEIGHBOR_SERNUM_HI/LO` | `0x188/0x18c` | self (election data) | neighbor | serial of my **right** neighbor (`:722-723`) |
| `FW_IO_REG_POD_ELECTION_STS` | `0x190` | self | neighbor + reload | packed `[cnt][node_id][lr_mask][sts]` (`:734-736`) |
| `FW_IO_REG_POD_SERNUM_HI/LO` | `0x194/0x198` | self | reload | pod-unique serial (leader's serial) (`:749-752`) |

The `POD_ELECTION_STS` word is the durable result; its packing is the single most important layout on the page:

```c
// MiscRAM status word -- neuron_pelect.c:228-232
word = (sts & 0xFF)            // bits  7..0  : MR_STS  {INIT,DEPRECATED,FAILURE,SUCCESS}
     | (lr_mask  & 0xFF) << 8  // bits 15..8  : link mask used in the winning election
     | (node_id  & 0xFF) << 16 // bits 23..16 : elected node id (0..3, or 0xFF for -1)
     | (node_cnt & 0xFF) << 24;// bits 31..24 : node count (2 or 4)
```

> **NOTE —** `node_id` is stored as a byte and read back through `NPE_MR_NODE_ID_GET` (`:230`) into an `int`, so the "not yet elected" sentinel `-1` round-trips through MiscRAM as **`0xFF`**, not `-1`. The election deliberately writes `node_id = -1` as a *placeholder* SUCCESS (`:928`) before the real id is committed (`:963`), and a reader that does not interpret the byte will see `255`. The same sign hazard surfaces at the public boundary: `npe_get_pod_status(u8 *node_id)` hands an unsigned byte to a `s8`-typed internal (`:1549`/`:1515`), so `-1` reaches byte-typed callers as `0xFF` unless they re-fetch the `int` and print it signed (which `:1772` does correctly).

### The Three Read Phases

A primary runs three remote-read phases against each active neighbor, each a poll-until-valid loop with the shared deadline and cancel checks:

1. **Serial numbers** — `npe_read_neighbor_serial_numbers` (`:498`): read `0x38` over *both* engines of a link pair; the pair must read the **same** serial (`memcmp`, `:528`) or the cabling is bad → `-EPIPE`. Retry while the neighbor returns `0` or `0xdeadbeef` (not up yet).
2. **Election data** — `npe_read_neighbor_election_data` (`:570`): read the neighbor's published `0x180..0x18c` (its own neighbors' serials). This both supplies the `diagonal` term and acts as a **state-transition probe** — the primary will not advance to the status phase until the neighbor has published valid election data, proving the neighbor also completed phase 1.
3. **Status** — `npe_read_neighbor_read_election_status` (`:639`): poll the neighbor's `MR_STS` byte at `0x190` until it *differs* from the previous value (`:665`), i.e. until the neighbor transitions out of `INIT`. Then `npe_check_election_results` (`:693`) requires every participating neighbor to report `MR_STS_SUCCESS(3)` or fails `-ENODEV`.

### The Neighbor DMA Read

```c
// npe_pod_neighbor_io_read -- the core remote MiscRAM read.  neuron_pelect.c:404
function npe_pod_neighbor_io_read(pnio, *buf, offset, size):
    if size + 8 > pnio->data_size: return -E2BIG            // :417  room for 2 flag words
    data_va = pnio->data_mc->va                             // host buffer
    base = (pod_ctl & SPOOF_BASE) ? 0                        // :422  test override
         : engid_2_b0_base[eng_id / V3_NUM_DMA_ENG_PER_SENG] // :425  pick B0_0..B0_3
    memset(data_va, 0, size+8); data_va[0]=1; data_va[1]=0  // :430  arm completion flag
    // DESC 1: read neighbor MiscRAM[base + APB_MISC_RAM(0x8006c84000) + offset] -> data_va[2..]
    udma_m2m_copy_prepare_one(..., base + 0x8006c84000 + offset,
                              data_mc->pa+8 | pci_host_base, size, WRITE_BARRIER)   // :436
    // DESC 2: local copy data_va[0](=1) -> data_va[1] -- fires only after DESC 1 retires
    udma_m2m_copy_prepare_one(..., data_mc->pa+0 | host, data_mc->pa+4 | host, 4, WRITE_BARRIER) // :446
    mb(); udma_m2m_copy_start(2 desc)                        // :453-455
    for i in 0..1000:                                        // :459  ~4 ms budget
        if READ_ONCE(data_va[1]) == 1:                       // :462  completion observed
            ack 2 descriptors; memcpy(buf, &data_va[2], size); return 0
        udelay(4)                                            // :470
    return -EIO                                              // :476  timeout
```

> **QUIRK —** the completion is a *second DMA descriptor*, not an interrupt or a hardware completion ring. `data_va[0]` is pre-set to `1`; descriptor 2 copies it into `data_va[1]`, and because both descriptors carry `WRITE_BARRIER`, the local copy cannot retire before the remote read's data has landed. The poll on `data_va[1]==1` therefore proves the payload at `data_va[2..]` is valid. A reimplementer who issues only the read descriptor and polls the payload directly will race the DMA — the barrier-ordered flag write is the whole synchronization.

---

## Primary Flow — `npe_primary_device_do_election`

Device 0 runs the only flow that produces a node id. It reads its own serial, exchanges with both neighbors, validates the ring cabling, computes its node id, and commits the durable result.

```c
function npe_primary_device_do_election(nd, secondary_good_cnt, lr_mask): // :821
    fw_io_device_id_read(&routing_id)                       // :840  self
    fw_io_serial_number_read(&serial_number)                // :845  self
    init 4 neighbor-io engines from npe_neighbor_eng_ids[L/R][0/1]        // :855-859
    miscram_sts_info_set(INIT, 0, -1, 0)                    // :867  clear status

    npe_read_neighbor_serial_numbers(pnio, nbr_serial, lr_mask)   // :871  PHASE 1
    miscram_neighbor_election_data_set(nbr_serial[L], nbr_serial[R]) // :880  PUBLISH
    npe_read_neighbor_election_data(pnio, nbr_copy, lr_mask)      // :889  PHASE 2

    if lr_mask == 0x3:                                       // :896  4-link cabling check
        for i in {L,R}:
            if nbr_copy[i][0] <= 15:                         // :898  a dev_index, not a serial
                ret = -EPIPE  ("miss-wired to ndNN")         //        -> mis-cable
    diagonal = nbr_copy[0][1]                                // :910  far corner via left nbr
    if secondary_good_cnt != 15: ret = -EPIPE               // :914  ALL 15 must pass
    node_id = npe_get_node_id(serial, nbr_serial[L],        // :924  THE ELECTION
                              nbr_serial[R], diagonal, &node_cnt, &pod_serial)
    miscram_sts_info_set(SUCCESS, 0, -1, 0)                 // :928  placeholder node_id=-1
    npe_read_neighbor_read_election_status(...INIT...)      // :932  PHASE 3: wait neighbors
    npe_check_election_results(...)                          // :939  all SUCCESS or -ENODEV
done:
    destroy 4 io engines                                    // :945
    miscram_neighbor_election_data_clr(nd)                  // :952  INTERLOCK (see below)
    if ret: miscram_sts=FAILURE; sernum=0; pelect.{node_id=-1,cnt=0,serial=0}  // :957
    else:   miscram_sts=SUCCESS|lr_mask|node_id|node_cnt; sernum=pod_serial;    // :963
            pelect.{node_id,node_cnt,pod_serial}             // :965-967  commit to RAM
```

The election-data clear at `:952` is a deliberate **interlock**, not cleanup. Clearing the published neighbor serials means the *next* election cannot pick up stale election data and short-circuit phase 2 — every subsequent election must re-sequence through the full handshake on all participating nodes, which is what makes a re-election transactional across the pod (`:86`, `:1056-1058`).

---

## Secondary Flow — `npe_secondary_device_vet`

Devices 1..15 run the same connectivity steps as the primary but do **no** node-id math. The one substantive difference is what they publish: instead of neighbor serials, a secondary writes its own `device_index` into both election-data slots (`:1027`), so each of its neighbors can confirm it is wired to the *expected* device index.

```c
function npe_secondary_device_vet(nd, lr_mask):             // :987
    init 4 neighbor-io engines                              // :998
    miscram_write(POD_ELECTION_STS, MR_STS_INIT)            // :1012
    npe_read_neighbor_serial_numbers(...)                   // :1016  PHASE 1 (good read = good link)
    miscram_neighbor_election_data_set(device_index, device_index) // :1027  PUBLISH dev index
    npe_read_neighbor_election_data(pnio, nbr_copy, lr_mask)// :1031  PHASE 2
    for i in active links:                                  // :1042  cabling check
        if nbr_copy[i][0] != nd->device_index:              // :1044  wrong index -> mis-wired
            ret = -EPIPE
    miscram_sts_info_set(SUCCESS, 0, 0, 0)                  // :1060
    npe_read_neighbor_read_election_status(...); check      // :1063-1068
done:
    if !ret && (pod_ctl & FAULT_SEC_LNK_FAIL): ret = -EPIPE // :1074  fault injection
    destroy engines; miscram_neighbor_election_data_clr     // :1078-1085  interlock
    if ret: clr election data again; miscram_sts=FAILURE    // :1091-1092
```

> **GOTCHA —** mis-cable detection turns on the secondary publishing a *small integer* (`device_index`, 0..15) as election data, and the reader (`:1044` for secondaries, `:898` for the primary) testing whether the value read back is `<= 15`. A real serial number is a full 64-bit value; a value in `0..15` can only be a `device_index`, which means the link is cabled to the wrong device. A reimplementation that publishes serials uniformly on every device loses this check entirely — the secondary's *intentional* publication of a tiny sentinel is the mechanism.

---

## The Node-ID Election — `npe_get_node_id`

The leader is the node with the **lowest serial number**, and the pod-unique serial is the leader's serial. The function takes the self serial, both neighbor serials, and the `diagonal` (far-corner) serial, and returns a node id in `0..3`.

```c
// npe_get_node_id -- serial-ordering leader election.  neuron_pelect.c:768
function npe_get_node_id(self, left, right, diagonal, *node_cnt, *pod_serial):
    // -- 2-NODE: only one link pair active, OR both neighbors are the same node --
    if lr_mask != 0x3 || left == right:                     // :774
        neighbor = (lr_mask == 0x2) ? right : left          // :775
        *node_cnt = 2
        if self < neighbor: *pod_serial = self;     return 0  // leader
        else:               *pod_serial = neighbor; return 1
    // -- 4-NODE ring of {self, left, right, diagonal} --
    *node_cnt = 4
    if self < diagonal:                                     // :788  diagonal is NOT leader-side
        if self < left && self < right:                     // :790  I am lowest of all
            *pod_serial = self;     return 0                 // leader
    else:                                                   // diagonal <= self
        if diagonal < left && diagonal < right:             // :797  diagonal is the leader
            *pod_serial = diagonal; return 2                 // I sit opposite the leader
    if left < right:                                        // :803  the other node in my rack leads
        *pod_serial = left;  return 1
    *pod_serial = right;     return 3                        // :809  leader is in the other rack
```

The four-node assignment is a total order over the ring positions. Read it as: node **0** is the global minimum; node **2** is the node diagonally opposite the leader; nodes **1** and **3** are the two adjacent-to-leader positions, distinguished by which rack holds the leader. The numbering matters downstream because nodes **1 and 3** are exactly the ones whose die addressing is flipped — the [die-flip](dhal-v3.md#5-the-die-flip-a-software-nc-remap-not-a-register) predicate keys on `node_id ∈ {1,3}` (`dhal_v3:1554`).

| Ring condition (4-node) | node id | leader (pod_serial) |
|---|---|---|
| `self < diagonal,left,right` | **0** | self |
| `diagonal < self` and `diagonal < left,right` | **2** | diagonal |
| else, `left < right` | **1** | left |
| else | **3** | right |

> **QUIRK —** the 2-node branch fires not only when the election is degraded to one link pair (`lr_mask != 0x3`) but also when `left == right` (`:774`) — i.e. a 4-link topology where both link pairs reach the *same* neighbor node. That is two independent 2-node pairs wired as two separate rings, and the engine correctly treats each as a 2-node cluster rather than a broken 4-node ring. A reimplementation that branches only on `lr_mask` will mis-classify this physical cabling.

---

## Lame-Duck Fault Model

When a full 4-link election fails, the thread does not give up immediately; it **degrades** the link mask and retries, forming two 2-node pairs out of a broken 4-node ring. The progression is `0x3 → 0x2 → 0x1 → fail`, each retry extending the deadline so a slow degrade does not time out.

```c
// inside npe_election_thread_fn, after a failed primary election.  :1693
switch lr_mask:
  0x3: lr_mask = 0x2   // both failed -> retry RIGHT-only ("2 node UltraServer")  :1703-1706
  0x2: lr_mask = 0x1   // right failed -> retry LEFT-only                          :1699-1701
  0x1: pod_state = FAILURE  // left also failed -> give up                         :1695-1697
// if still trying and < 120 s left, bump the deadline by NPE_ETIMEOUT_EXTENSION_MS
if pod_state != FAILURE && deadline - now() < 120 s:
    nbr_data_read_deadline = now() + 120 s                  // :1716-1718
```

The same degraded masks can be forced from the start via module params: `USE_R_LNK_ONLY (1<<9)` sets `lr_mask_default = 0x2`, `USE_L_LNK_ONLY (1<<10)` sets `0x1` (`npe_init`, `:1977-1980`). The `userver_ctl` bitmask also injects faults to exercise the lame-duck path — `FAULT_L_LNK_FAIL (1<<7)` / `FAULT_R_LNK_FAIL (1<<8)` make a specific link's reads return error (`:299-302`), and the documented combos at `:132-135` (`0x041` clear+skip, `0x200` right-only, `0x440` left-only) drive these from the driver command line.

### Election Thread

```c
// npe_election_thread_fn -- the single serial driver.  :1638
while !should_stop && !stop:
  retry:
    wait_event(pod_state == IN_PROGRESS || stop)            // :1647
    if stop: break
    if !npe_all_rst_complete(): WARN_ONCE; pod_state=FAILURE; goto retry  // :1654
    secondary_good_cnt = 0
    for i in 1..15:                                         // :1673  SERIAL, same order on every node
        if npe_secondary_device_vet(pnd[i], lr_mask) == 0: secondary_good_cnt++
    ret = npe_primary_device_do_election(pnd[0], secondary_good_cnt, lr_mask) // :1686
    lock()
    if ret:  <lame-duck switch above>                       // :1693
    else:    pod_state = SUCCESS; log node_id/cnt/serial    // :1721
    unlock()
```

> **NOTE —** devices are processed **serially, in the same order (1..15 then 0) on every node** (`:91-93`, `:1673`/`:1686`). This is the deadlock-avoidance invariant of a lock-step cross-node protocol: if two nodes vetted devices in different orders, node A could block reading device *i* on node B while node B blocks on device *j* on node A, and both spin to the timeout. The serialization is not a performance choice — it is correctness.

---

## Operating-Mode Reinterpretation

The election produces one topology, but an application can *operate* a 4-node pod as 4-node (`X4`), two horizontal 2-node halves (`X2H`), two vertical halves (`X2V`), or four standalone nodes (`X1`). The `mode` is set via `POD_CTRL SET_MODE` and is locked until all cores are released (`:1598`, cleared by `npe_notify_mark` on unmark-to-zero, `:1244`). The `npe_get_modal_*` functions reinterpret the single elected `node_id`/`node_cnt`/`serial` per mode — no re-election occurs.

| Mode | value | node_id remap (from 4-node) | supported when |
|---|---|---|---|
| `X4` | 1 | identity; `-1` if `node_cnt != 4` (`:1281`) | `node_cnt == 4` (`:1416`) |
| `X2H` | 2 | `x4hmap{0,0,1,1}` (`:1288`) | `node_cnt == 2 && lr_mask & 0x2` (`:1420`) |
| `X2V` | 3 | `x4vmap{0,1,0,1}` (`:1295`) | `node_cnt >= 2 && lr_mask & 0x1` (`:1418`) |
| `X1` | 4 | always `-1` (`:1301`) | always (`:1422`) |

`npe_get_modal_serial_number` (`:1360`) additionally subtracts 1 from the pod serial for the second pair when splitting a 4-node pod into two `X2H`/`X2V` halves (`adj_serial` tables, `:1376`/`:1385`), so the two halves get distinct pod ids. The four sysfs `npe_class_*_show_data` functions (`:1753-1835`) surface `node_id`, `node_cnt`, `server_id`, and the supported-mode list under `/sys/class/neuron_device`, all returning `"busy"` while `npe_pod_state_busy()` (`:1255`).

---

## PDS Path — `npe_pds_config_init`

PDS (a discovery-server SKU) runs **no link election**. Node assignment is location-based: device 0's serial is matched against a hard-coded table, and failing that, node count and node id are derived from FW_IO platform reads. It runs exactly once (`static bool initialized`, `:1865`).

```c
function npe_pds_config_init():                             // :1863
    if initialized: return
    fw_io_serial_number_read(pnd[0], &serial)               // :1889
    for row in npe_pds_tmp_mapping_tbl:                     // :1897  hard-coded TRN3PDS US16
        if serial == row.d0_serial_number:                 // :1898
            node_id=row.node_id; pod_serial=row.server_num; node_cnt=row.node_cnt; goto done
    fw_io_instance_partition_sz_read(&inst, &part)          // :1908
    node_cnt = (part==-1 || inst<=0) ? 4 : part/inst        // :1913-1917  invalid -> default 4
    if node_cnt == 2: lr_mask = 0x1                          // :1920  2-node uses V-links
    fw_io_reservation_id_read(&pod_serial)                  // :1927
    if pod_serial == 0: pod_serial = pds_reservation_id     // :1932  param fallback (0x0001)
    fw_io_server_info_read(&server_id, &rack_id)            // :1939
    node_id = map[(rack_id<<1)|server_id]:  0->0,1->1,2->3,3->2  // :1948-1964  else -1
done:
    pod_state_internal = SUCCESS  // unconditional (TODO: real failure reporting, :1967)
```

The hard-coded table (`npe_pds_tmp_mapping_tbl`, `:1848-1853`) maps four specific device-0 serials, all sharing `server_num = 0x0000004005590728` and `node_cnt = 4`, to node ids 0/1/3/2 — the comments note two of the four have their physical labels swapped, which is precisely why a static map (not a serial-ordering election) is needed for this SKU.

> **QUIRK —** on PDS the `(rack<<1)|server → node_id` map is `{0,1,3,2}` (`:1948-1964`), not the identity. Cases 2 and 3 are *swapped* — `(rack=1,server=0)` → node 3 and `(rack=1,server=1)` → node 2 — mirroring the "flipped server_id in rack1" comments in the hard-coded table. A reimplementer must reproduce the swap exactly; an identity map gives the wrong die-flip parity on half the rack-1 nodes.

---

## `npe_*` Op Surface (DHAL Bindings)

The DHAL registrars publish these `npe_*` functions through the `ndhal->ndhal_npe` vtable plus the neighbor-engine-id table; the ioctl/sysfs/reset layers reach the engine only through those slots. The thunks themselves (`npe_pod_*_v3`, the `_v2` no-ops) live in the DHAL cells.

| Op (public symbol) | DHAL thunk / binding | Role | file:line | Confidence |
|---|---|---|---|---|
| `npe_init` | `neuron_dhal_v3.c:2000`, `_v4.c:457` | create thread, apply `USE_*_LNK_ONLY`, set timeout | `:1973` | HIGH |
| `npe_cleanup` | `ndhal_ext_cleanup_v3` (`dhal_v3:1844`) | stop thread, clear `pnd[0]` | `:1987` | HIGH |
| `npe_election_exec_on_rst` | reset post-config (`dhal_v3:427`) | per-device reset hook; cache-load or kick election | `:1146` | HIGH |
| `npe_notify_mark` | `npe_notify_mark_v3` (`dhal_v3:1616`) | on unmark-to-zero, reset `mode` to UNSET | `:1241` | HIGH |
| `npe_pod_ctrl` | `npe_pod_ctrl_v3` (`dhal_v3:1686`) | on-demand REQ_POD / KILL / SET_MODE / SINGLE_NODE | `:1571` | HIGH |
| `npe_get_pod_status` | `npe_pod_status_v3` (`dhal_v3:1666`) | state + node_id (die-flip predicate source) | `:1549` | HIGH |
| `npe_get_pod_id` | `npe_pod_info_v3` (`dhal_v3:1633`) | pod-unique serial (modal) | `:1461` | HIGH |
| `npe_get_pod_sz` | `npe_pod_info_v3` | modal node count | `:1475` | HIGH |
| `npe_get_pod_mode` | `npe_pod_info_v3` | current operating mode | `:1486` | HIGH |
| `npe_get_pod_modes_supported` | `npe_pod_info_v3` | bitmask of supported modes | `:1497` | HIGH |
| `npe_class_node_id_show_data` | sysfs (`dhal_v3` sysfs) | `/sys/class/neuron_device` node id | `:1753` | HIGH |
| `npe_class_node_cnt_show_data` | sysfs | node count (PDS only) | `:1775` | HIGH |
| `npe_class_server_id_show_data` | sysfs | server/pod id | `:1792` | HIGH |
| `npe_class_ultraserver_mode_show_data` | sysfs | supported-mode CSV | `:1812` | HIGH |
| `npe_neighbor_eng_ids` (table) | `npe_neighbor_eng_ids_v3/v4` | L/R link-pair DMA engine ids | `dhal_v3:214` / `dhal_v4:132` | HIGH |

---

## Function Map

| Function | Lines | Role | Confidence |
|---|---|---|---|
| `npe_election_thread_fn` | `:1638-1730` | the single serial election driver; lame-duck retry | HIGH |
| `npe_primary_device_do_election` | `:821-971` | device-0 main flow: exchange, vet, elect, commit | HIGH |
| `npe_secondary_device_vet` | `:987-1095` | device 1..15 cabling vet; publishes `device_index` | HIGH |
| `npe_get_node_id` | `:768-811` | serial-ordering leader election, 2/4-node id assign | HIGH |
| `npe_pod_neighbor_io_read` | `:404-477` | core remote-MiscRAM UDMA M2M read + flag handshake | HIGH |
| `npe_read_neighbor_serial_numbers` | `:498-561` | phase 1: read+pair-verify neighbor serials | HIGH |
| `npe_read_neighbor_election_data` | `:570-631` | phase 2: read neighbor's published serials → diagonal | HIGH |
| `npe_read_neighbor_read_election_status` | `:639-691` | phase 3: poll neighbor `MR_STS` for transition | HIGH |
| `npe_check_election_results` | `:693-706` | require all neighbors `MR_STS_SUCCESS` | HIGH |
| `npe_miscram_sts_info_set/get` | `:734-747` | pack/unpack the `[cnt][nid][lr][sts]` word | HIGH |
| `npe_initiate_election` | `:1104-1118` | INIT→IN_PROGRESS, reset result, wake thread | HIGH |
| `npe_election_exec_on_rst` | `:1146-1227` | reset hook: cache-load or trigger A | HIGH |
| `npe_pod_ctrl` | `:1571-1636` | on-demand trigger B (ioctl) | HIGH |
| `npe_get_modal_node_id/cnt/serial` | `:1267-1402` | reinterpret one topology per operating mode | HIGH |
| `npe_mode_is_supported` / `npe_node_cnt_to_mode` | `:1412-1453` | mode-validity matrix | HIGH |
| `npe_pds_config_init` | `:1863-1971` | PDS location-based assignment (no link election) | HIGH |
| `npe_init` / `npe_cleanup` | `:1973-1993` | thread create/teardown + link-mask defaults | HIGH |

> **CORRECTION (PELECT-doc) —** the header doc for `npe_cleanup` claims it cleans up "pod state left around (miscram)" (`neuron_pelect.h:29`). The implementation (`:1987-1993`) only stops the thread and clears `pnd[0]` — it issues **no** MiscRAM write. The durable MiscRAM result is intentionally *not* cleared on unload (that is what makes the cache-reload at `:1184` work). Trust the implementation: a reimplementation that wipes MiscRAM on cleanup breaks the reload-without-re-election path. (MED confidence: doc/impl mismatch, impl is authoritative.)

---

## Cross-References

- [DHAL V3 (Trn2)](dhal-v3.md) — the `npe_*_v3` thunks, `npe_neighbor_eng_ids_v3 {{36,68},{4,100}}`, the reset hook at `:427`, and the [die-flip](dhal-v3.md#5-the-die-flip-a-software-nc-remap-not-a-register) that reads `node_id ∈ {1,3}` from `npe_get_pod_status`
- [DHAL V4 (Trn3)](dhal-v4.md) — the V4 override that relays out `npe_neighbor_eng_ids_v4 {{40,72},{8,104}}` and the PDS **static** NC-swap (no runtime die-flip), the contrast to V3's dynamic XOR
- [Pod and Misc IOCTL Handlers](ioctl-pod.md) — the `POD_CTRL` ioctl that calls `npe_pod_ctrl` (REQ_POD / REQ_KILL / SET_MODE / REQ_SINGLE_NODE)
- [Silicon & Architecture Model](../arch/overview.md) — the platform-type taxonomy (STD / ULTRASERVER / PDS) the engine branches on
- [IOCTL Attack Surface](../security/ioctl-attack-surface.md) — `POD_CTRL` as an ungated state-changer and the `npe_notify_mark` amplification in the CRWL unmark loop (S8)
