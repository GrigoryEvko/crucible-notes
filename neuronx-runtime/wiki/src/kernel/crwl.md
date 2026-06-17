# Cooperative RW Lock (CRWL)

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The lock is read directly from `neuron_crwl.c` (283 lines) and `neuron_crwl.h` (112 lines); the embedded per-device array and the key type from `neuron_device.h` and `share/neuron_driver_shared.h`. The source is read, not reverse-engineered — every counter transition, retry cap, and `#define` is transcribed from the shipped `.c`/`.h`. The byte offsets of `struct neuron_crwl` fields are compiler-determined (a `struct mutex`/`struct neuron_uuid` precede the counters) and are **not** pinned in source, so the layout table gives declaration order with a Confidence column, never a hard `+N`. Other driver versions renumber lines. Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

`neuron_crwl.c` implements the per-NeuronCore **ownership arbitration** the runtime uses to serialize model-load against inference. One `struct neuron_crwl` exists per NeuronCore — `nd->crwl[MAX_NC_PER_DEVICE]` (`neuron_device.h:108`, `MAX_NC_PER_DEVICE == 8`, `:11`) — and arbitrates a single **writer** (a model-load that is rewriting the core's program) against multiple **readers** (in-flight inferences/exec of the *same* model). The shape is a classic reader/writer lock with writer preference, but the implementation is **not** a kernel `rw_semaphore`: it is hand-rolled from a `struct mutex` that guards two plain counters plus a cooperative `usleep_range()` spin with a hard retry cap. A waiter that cannot make progress within the cap returns `-EBUSY` rather than sleeping on a wait queue. The lock is keyed on a **32-byte model UUID** (`struct neuron_uuid { __u8 value[32]; }`, `neuron_driver_shared.h:150-152`) plus the writer's thread-group id, so "the same model in the same process" is the unit that shares read access.

The state of a core is the pair `(writer_acquired, reader_count)`. A reader may enter only when no writer holds the core; a writer may enter only when there is no other writer *and* `reader_count == 0` — i.e. it waits out the previous model's readers before it begins rewriting. The two counters are never touched outside `crwl->lock`, so the `volatile` qualifiers on them (`neuron_crwl.h:18,21`) are defensive, not a lock-free protocol — there is no lock-free reader anywhere in the driver. A `validate_uuid` gate fronts every reader/exit/downgrade path: the caller's 32-byte UUID must `memcmp`-match the core's recorded UUID *and* the recorded `writer_pid` must equal the caller's `task_tgid_nr(current)`, else `-ENOENT`. The cooperative-spin design means a crashed-but-not-exited writer blocks every reader until the cap trips; the *only* forced release is process-death cleanup (`ncrwl_release_current_process`, [§4](#4-release--downgrade--forced-cleanup)), invoked from the cdev close path — there is no preemptive steal.

This page documents the per-NC reader/writer lock — mechanism (A). The file also hosts a separate, device-spanning **NC-range reservation allocator** (the global `ncrwl_range_pids[]` first-fit bitmap keyed on tgid, with its own `ncrwl_range_lock` and the `npe_notify_mark` election hook). That allocator is the global-mutation half called out for security in [§5](#5-the-global-allocator-mutation-note); its first-fit algorithm, election amplification, and the cdev unmark-on-close path are owned by [pod-election](pod-election.md) and the [attack-surface page](../security/ioctl-attack-surface.md), and are summarized here only where they touch the per-NC lock's lifecycle.

For reimplementation, the contract is:

- **The two-counter state machine** — `(writer_acquired, reader_count)` and its five legal transitions; `(true, N>0)` is structurally unreachable as a *stable* state because writer-enter drains readers first and downgrade `BUG_ON`s on a non-zero reader count.
- **The UUID + tgid key** — a 32-byte `memcmp` against `crwl->uuid` *and* `crwl->writer_pid == task_tgid_nr(current)`; a reader joins an existing writer's model only if both match.
- **The cooperative spin and the caps** — drop the mutex, `usleep_range(10, 20)`, re-acquire, recheck; reader cap `50*1000` retries (`~500 ms`), writer cap `200*1000` (`~2 s`); over the cap returns `-EBUSY`. There is no wait queue and no priority inheritance.
- **The forced-release contract** — no cross-process steal exists; a stuck holder is cleared only by its own process exiting (`ncrwl_release_current_process`) or by a waiter's cap expiring.

| | |
|---|---|
| **Per-NC lock object** | `struct neuron_crwl` (`neuron_crwl.h:14-22`); one per NC |
| **Per-device array** | `nd->crwl[MAX_NC_PER_DEVICE]` (`neuron_device.h:108`); `MAX_NC_PER_DEVICE == 8` (`:11`) |
| **Key** | 32-byte UUID (`struct neuron_uuid`, `neuron_driver_shared.h:150-152`) + `writer_pid` (tgid) |
| **Lock primitive** | `struct mutex lock` + `usleep_range` spin — **not** a `rw_semaphore` |
| **Reader acquire / release** | `ncrwl_reader_enter` (`:41`) / `ncrwl_reader_exit` (`:78`) |
| **Writer acquire / downgrade** | `ncrwl_writer_enter` (`:105`) / `ncrwl_writer_downgrade` (`:154`) |
| **UUID gate** | `ncrwl_validate_uuid` (`:23`) — `memcmp` UUID + `writer_pid == tgid`, else `-ENOENT` |
| **Reader cap** | `NEURON_CRWL_READER_MAX_RETRY = 50 * 1000` (`neuron_crwl.c:20`) → `~500 ms` |
| **Writer cap** | `NEURON_CRWL_WRITER_MAX_RETRY = 200 * 1000` (`neuron_crwl.c:21`) → `~2 s` |
| **Spin sleep** | `usleep_range(NEURON_CRWL_SLEEP_MIN=10, NEURON_CRWL_SLEEP_MAX=20)` µs (`neuron_crwl.c:16-17`) |
| **Forced release** | `ncrwl_release_current_process` (`:267`) on process exit — no preemptive steal |
| **ioctl entry** | cmds 81–84 (`reader_enter`/`exit`, `writer_enter`/`downgrade`), `neuron_ioctl.h:762-765` |
| **Confidence** | HIGH — both files read in full; counters, transitions, caps, error codes verbatim. Byte offsets MED (declaration order only) |

> **NOTE (cite convention) —** a **bare** `:NN` citation on this page points into **`neuron_crwl.c`** (the implementation: function bodies, the `usleep_range` spin, and the four retry/sleep `#define`s at `neuron_crwl.c:16-21`). Only `struct neuron_crwl` itself and the `volatile` qualifiers live in the **header** — those are cited explicitly as `neuron_crwl.h:14-22` (struct) and `neuron_crwl.h:18,21` (the `writer_acquired`/`reader_count` qualifiers). The caps/sleep `#define`s are *not* in the header despite sitting next to the struct cite in the table above.

---

## 1. The `struct neuron_crwl` Layout and the Per-Device Array

### Purpose

`struct neuron_crwl` is the entire per-NeuronCore lock state: one mutex, the 32-byte model identity, the writer flag, the writer's pid, and the reader count. It is embedded by value — not by pointer — once per core in the owning `struct neuron_device`, so a core's lock has no separate allocation and no separate lifetime; it lives and dies with the device.

### Layout

The field *set and order* are HIGH (verbatim from `neuron_crwl.h:14-22`). The numeric offsets are MED: `struct mutex` and the embedded 32-byte `struct neuron_uuid` precede the scalars, and a `struct mutex`'s size is kernel-config-specific, so the table gives declaration order, never a hard `+N`.

| Field | Decl order | Type | Role | Confidence |
|---|---|---|---|---|
| `lock` | 0 | `struct mutex` | guards **all** of `uuid`/`writer_acquired`/`writer_pid`/`reader_count`; held across every counter read and write | HIGH |
| `uuid` | 1 | `struct neuron_uuid` (`__u8[32]`) | 32-byte identity of the model currently owning the core; the reader/exit/downgrade key | HIGH |
| `writer_acquired` | 2 | `volatile bool` | `true` iff a writer holds exclusive access (a model load is mid-flight); the reader-blocking flag | HIGH |
| `writer_pid` | 3 | `pid_t` | tgid of the last process to take/update the writer lock; the second half of the key | HIGH |
| `reader_count` | 4 | `volatile u64` | number of outstanding readers (in-flight inferences of `uuid`) | HIGH |

> **NOTE —** `writer_acquired` and `reader_count` are declared `volatile` (`neuron_crwl.h:18,21`) but are *only ever* read or written while `crwl->lock` is held — every access in `neuron_crwl.c` is bracketed by `mutex_lock`/`mutex_unlock`. The `volatile` is therefore redundant defensive decoration, **not** a lock-free signalling protocol. A reimplementer should not infer that any reader observes these fields outside the mutex; there is no such reader in the driver (cdev, sysfs, or otherwise). Modelling them as plain mutex-protected fields is faithful.

### The per-device array

```c
// neuron_device.h:108 -- one lock per NeuronCore, embedded in the device
struct neuron_crwl crwl[MAX_NC_PER_DEVICE];   // cooperative rw lock per NC
//                      ^ MAX_NC_PER_DEVICE == 8  (neuron_device.h:11)
```

Indexing is by `nc_index` in `[0, MAX_NC_PER_DEVICE)`. Every public entry point bounds-checks `nc_index` before dereferencing `&nd->crwl[nc_index]`, but two different ways: the reader/writer entry points return `-EINVAL` (`:47`, `:83`, `:111`, `:159`), whereas the internal `ncrwl_validate_uuid` uses a `BUG_ON(nc_index >= MAX_NC_PER_DEVICE)` (`:27`) — a hard kernel assertion, on the assumption its callers already validated.

> **GOTCHA —** the per-NC lock array is **per device** (`nd->crwl[8]`), but the *range reservation* allocator in the same file ([§5](#5-the-global-allocator-mutation-note)) is a single `static` array spanning **all 64 devices** (`ncrwl_range_pids[64 * 8]`, `:185`). The two mechanisms share a source file and a naming prefix but not a scope: one is device-local fine-grained R/W arbitration, the other is a global process-wide core reservation. A reimplementer who collapses them into one structure will mis-scope the reservation allocator's lock contention and its cross-device exhaustion surface.

---

## 2. The Key Model — 32-Byte UUID + Writer tgid

### Purpose

The lock identifies *whose* model owns a core and *which* model it is. Both questions are answered by one key: the 32-byte `struct neuron_uuid` the writer stamped when it loaded the model, plus the writer's thread-group id. A reader does not take a fresh lock — it *attaches* to the writer's existing ownership by proving it presents the same key.

### The gate

`ncrwl_validate_uuid` (`:23`) is the single chokepoint every reader/exit/downgrade passes through. It is two conjoined checks, both of which must pass:

```c
function ncrwl_validate_uuid(nd, nc_index, uuid):               // neuron_crwl.c:23
    BUG_ON(nc_index >= MAX_NC_PER_DEVICE)                        // :27  caller must have bounds-checked
    crwl = &nd->crwl[nc_index]

    // (1) the model must match -- 32-byte byte-compare against the stamped UUID
    if memcmp(&uuid, &crwl->uuid, sizeof(uuid)) != 0:           // :30  sizeof == 32
        return -ENOENT                                          // :31  wrong model

    // (2) the process must match -- the caller's tgid must equal the recorded writer
    if crwl->writer_pid != task_tgid_nr(current):              // :33
        pr_err("nd%dnc%d: pid:%d Invalid pid - writer:%d", ...) // :34
        return -ENOENT                                          // :36  right model, wrong process
    return 0
```

Two facts a reimplementer must preserve. First, the model match is a full 32-byte `memcmp` — there is no hashing or truncation; the UUID is compared byte-for-byte (`:30`). Second, ownership is **process-scoped to the thread-group**, not the thread: the key is `task_tgid_nr(current)` (the tgid / userspace pid), so any thread in the writer's process that presents the matching UUID is treated as the same owner. A *different* process holding the same UUID bytes still fails check (2) and gets `-ENOENT`.

> **QUIRK —** the two `-ENOENT` returns mean different things but share a code. A UUID `memcmp` mismatch (`:31`) means "this core is running a different model than you named"; a `writer_pid` mismatch (`:36`) means "this core's model belongs to another process." Only the second logs (`pr_err` at `:34`); the first is silent. A reimplementer surfacing diagnostics must reproduce that asymmetry — a reader that races a model swap sees a silent `-ENOENT`, while a cross-process intruder leaves a kernel-log trail.

### How a reader matches an existing writer

A reader never stamps the UUID — only a writer does (`memcpy(&crwl->uuid, &uuid, ...)` at `:147`, in `ncrwl_writer_enter`). The reader supplies the UUID it *expects* and `validate_uuid` confirms it is what the writer last stamped. So the join protocol is: a model-load process takes the writer lock and stamps `(uuid, writer_pid)`; it then downgrades to a reader ([§4](#4-release--downgrade--forced-cleanup)); subsequent inference calls from the same process present the same 32 bytes and increment `reader_count`. The UUID is the contract between the load and the inferences; the tgid pins them to one process.

---

## 3. The Acquire Algorithm

Both `enter` paths share one shape: take the mutex, spin-wait on a blocking condition (dropping the mutex across each sleep), bail out with `-EBUSY` if the retry cap trips, then mutate the counter under the mutex and release. The reader and writer differ only in the wait condition and the cap.

### Reader acquire — `ncrwl_reader_enter` (`:41`)

A reader blocks only on an active writer. Once no writer holds the core, it validates the key and bumps `reader_count`.

```c
function ncrwl_reader_enter(nd, nc_index, uuid):               // neuron_crwl.c:41
    if nc_index >= MAX_NC_PER_DEVICE: return -EINVAL            // :47
    crwl = &nd->crwl[nc_index]
    retry_count = 0
    mutex_lock(&crwl->lock)                                     // :52

    // ---- cooperative spin: wait out an in-flight writer (model load) ----
    while crwl->writer_acquired:                                // :55
        mutex_unlock(&crwl->lock)                              // :56  release WHILE sleeping
        if retry_count > NEURON_CRWL_READER_MAX_RETRY:         // :57  cap = 50*1000  (~500 ms)
            pr_err("nd%dnc%d: pid:%d - reader starved. writer:%d", ...)   // :58
            return -EBUSY                                       // :60  give up; NOT holding the mutex
        retry_count++                                          // :62
        usleep_range(SLEEP_MIN=10, SLEEP_MAX=20)               // :63  10..20 us
        mutex_lock(&crwl->lock)                                // :64  re-acquire and recheck

    // ---- no writer: validate the key, then take a reader ref ----
    ret = ncrwl_validate_uuid(nd, nc_index, uuid)              // :67  UUID memcmp + tgid
    if ret: goto done                                          // :68  -ENOENT -> bail with reader NOT counted
    crwl->reader_count++                                       // :71

done:
    mutex_unlock(&crwl->lock)                                  // :74
    return ret
```

The reader takes its reference only after `validate_uuid` passes (`:67-71`): a stale or cross-process reader is rejected with `-ENOENT` and does **not** increment the count, so a failed enter leaves the lock state untouched.

### Writer acquire — `ncrwl_writer_enter` (`:105`)

A writer blocks on *either* another writer *or* any outstanding reader — it must wait until the core is fully idle (`writer_acquired == false && reader_count == 0`) before it begins rewriting the model. Its cap is 4× the reader's, and it emits a progress log every 20000 retries.

```c
function ncrwl_writer_enter(nd, nc_index, uuid):               // neuron_crwl.c:105
    if nc_index >= MAX_NC_PER_DEVICE: return -EINVAL           // :111
    crwl = &nd->crwl[nc_index]
    retry_count = 0
    mutex_lock(&crwl->lock)                                    // :115

    // ---- spin until the core is fully idle: no other writer AND no readers ----
    while crwl->writer_acquired ||                             // :117  another writer holds it
          crwl->reader_count:                                  // :118  drain the OLD model's readers
        mutex_unlock(&crwl->lock)                             // :119
        if retry_count > NEURON_CRWL_WRITER_MAX_RETRY:        // :120  cap = 200*1000  (~2 s)
            pr_err("nd%dnc%d: pid:%d - writer starved. readers:%lld writer:%d", ...)  // :121
            return -EBUSY                                      // :124
        retry_count++                                         // :126
        if retry_count % 20000 == 0:                          // :127  periodic progress note
            pr_info("nd%dnc%d: pid:%d - writer retrying(%lld/%d) readers:%lld writer:%d", ...)  // :128
        usleep_range(SLEEP_MIN=10, SLEEP_MAX=20)              // :133
        mutex_lock(&crwl->lock)                               // :134

    // ---- core is idle: claim it ----
    crwl->writer_acquired = true                              // :137  flip the flag FIRST

    // idempotent fast-path: same process re-loading the SAME model -> no-op
    if crwl->writer_pid == task_tgid_nr(current) &&           // :140
       memcmp(&uuid, &crwl->uuid, sizeof(uuid)) == 0:         // :141
        ret = -EALREADY                                        // :142  already loaded; skip the restamp
        goto done

    // new owner / new model: stamp the key
    crwl->writer_pid = task_tgid_nr(current)                  // :146
    memcpy(&crwl->uuid, &uuid, sizeof(uuid))                  // :147  stamp the 32-byte UUID

done:
    mutex_unlock(&crwl->lock)                                 // :150
    return ret
```

The `-EALREADY` fast path (`:140-143`) is the "some other thread in my process already switched this core to the model I want" case (comment `:139`): the writer is granted (the flag is set), but the restamp is skipped because the recorded `(writer_pid, uuid)` already equals what would be written. The caller treats `-EALREADY` as success-without-work.

> **QUIRK —** `writer_acquired = true` is set at `:137` **before** the `-EALREADY` check, and the `-EALREADY` path returns *without resetting it*. So even the idempotent no-op leaves `writer_acquired == true`. That is correct only because the caller of an `-EALREADY` writer-enter is expected to follow with a `writer_downgrade` (which clears `writer_acquired` and installs `reader_count = 1`, [§4](#4-release--downgrade--forced-cleanup)) exactly as the success path does — `-EALREADY` is "you already own the write lock," not "no lock was taken." A reimplementer who treats `-EALREADY` as "lock not acquired, nothing to release" will strand `writer_acquired == true` and deadlock every later reader until the owning process exits.

> **GOTCHA —** the retry-cap comparison is `retry_count > MAX_RETRY` with a *strict* `>` (`:57`, `:120`), and `retry_count++` runs *after* the check (`:62`, `:126`). The loop therefore performs `MAX_RETRY + 1` sleep iterations before bailing — `50001` for a reader, `200001` for a writer. The `~500 ms` / `~2 s` figures in the header comments (`:20-21`) are nominal: actual wall time is `(MAX_RETRY+1) × usleep_range(10,20)`, so a reader waits roughly 500–1000 ms and a writer 2–4 s depending on where each sleep lands in its 10–20 µs window. Do not treat the cap as a precise timeout; it is an iteration count with jittered sleeps.

### Why writer-preference and reader-drain

The writer wait condition includes `reader_count` (`:118`) precisely because a model swap must not begin while the *previous* model's inferences are still reading the core — the writer is about to overwrite the program the readers are executing. Conversely the reader wait condition does **not** include `reader_count` (only `writer_acquired`, `:55`): readers freely share. This is textbook writer-preference with a reader-drain barrier, implemented by polling rather than by a wait queue. The cost of the polling form is the subject of the closing [§6 callout](#6-the-cooperative-spin-cost).

---

## 4. Release · Downgrade · Forced Cleanup

### Reader release — `ncrwl_reader_exit` (`:78`)

The mirror of enter: validate the key, guard against an underflow, decrement. There is no spin — release never blocks.

```c
function ncrwl_reader_exit(nd, nc_index, uuid):                // neuron_crwl.c:78
    if nc_index >= MAX_NC_PER_DEVICE: return -EINVAL           // :83
    crwl = &nd->crwl[nc_index]
    mutex_lock(&crwl->lock)                                    // :87
    ret = ncrwl_validate_uuid(nd, nc_index, uuid)             // :88  same UUID+tgid gate as enter
    if ret: goto done                                          // :89

    if crwl->reader_count == 0:                                // :92  underflow guard
        pr_err("nd%dnc%d: pid:%d - reader count is already 0", ...)   // :93
        ret = -EINVAL                                          // :95
        goto done
    crwl->reader_count--                                       // :98

done:
    mutex_unlock(&crwl->lock)                                  // :101
    return ret
```

The `reader_count == 0` guard (`:92-96`) turns a double-exit into a logged `-EINVAL` rather than wrapping the `u64` to `0xFFFF...` — a defensive check a reimplementer must keep, since a wrapped reader count would permanently block every future writer (its `reader_count` wait at `:118` would never see zero).

### Writer downgrade — `ncrwl_writer_downgrade` (`:154`)

The intended exit of a *writer* is not a "writer exit" — there is none. A writer that finishes loading the model **downgrades** to a single reader, atomically converting exclusive ownership into the first inference reference. This is how a load hands off to its own inferences without a window where the core is unowned.

```c
function ncrwl_writer_downgrade(nd, nc_index, uuid):           // neuron_crwl.c:154
    if nc_index >= MAX_NC_PER_DEVICE: return -EINVAL           // :159
    crwl = &nd->crwl[nc_index]
    mutex_lock(&crwl->lock)                                    // :163

    if !crwl->writer_acquired:                                 // :164  must currently hold the write lock
        pr_err("nd%dnc%d: pid:%d - writer downgrade called without enter", ...)  // :165
        ret = -EINVAL; goto done                               // :167

    ret = ncrwl_validate_uuid(nd, nc_index, uuid)             // :170  UUID+tgid gate
    if ret: goto done                                          // :171

    BUG_ON(crwl->reader_count != 0)                            // :173  invariant: no readers during write

    crwl->writer_acquired = false                              // :175  drop exclusive
    crwl->reader_count = 1                                     // :176  install the first reader atomically

done:
    mutex_unlock(&crwl->lock)                                 // :179
    return ret
```

The `BUG_ON(reader_count != 0)` at `:173` encodes the invariant that no reader can coexist with a held writer — which holds because `writer_enter` does not return until `reader_count == 0` (`:118`) and no reader can have entered since (`reader_enter` blocks on `writer_acquired`, `:55`). Together these make the state `(writer_acquired=true, reader_count>0)` structurally unreachable as a stable state, and the `BUG_ON` is the assertion that proves it.

### Forced cleanup — `ncrwl_release_current_process` (`:267`)

There is **no** cross-process steal API. The only way a stuck or crashed holder is released is its own process exiting: the cdev close path (`ncdev_flush`, after `npid_is_attached == 1`, draining DMA and waiting on reset — see [cdev-mmap](cdev-mmap.md)) calls this function, which walks all of the device's cores and force-resets any whose `writer_pid` matches the exiting tgid.

```c
function ncrwl_release_current_process(nd):                    // neuron_crwl.c:267
    for nc_index in 0 .. MAX_NC_PER_DEVICE-1:                  // :272  all 8 cores on this device
        crwl = &nd->crwl[nc_index]
        mutex_lock(&crwl->lock)                                // :274
        if crwl->writer_pid == task_tgid_nr(current):         // :275  this process owned the core
            crwl->reader_count = 0                             // :276  forget outstanding readers
            crwl->writer_pid = 0                              // :277
            crwl->writer_acquired = false                     // :278  drop any held write lock
            memset(&crwl->uuid, 0, sizeof(struct neuron_uuid))// :279  wipe the 32-byte key
        mutex_unlock(&crwl->lock)
```

Two scope facts a reimplementer must keep. First, cleanup keys on `writer_pid` (`:275`) — it releases a core only if the *exiting process was the writer* that stamped it; a process that only ever held *reader* references on someone else's core is not matched here (its readers are accounted to the writer's tgid, and the writer's own exit clears them). Second, this resets the **per-NC lock** (mechanism A) only. The global **range reservation** ([§5](#5-the-global-allocator-mutation-note)) is *not* touched here; it is released on a different cdev path (`ncdev_misc_flush` → `ncrwl_nc_range_unmark`, the `O_WRONLY`-close unmark documented in [cdev-mmap](cdev-mmap.md)). A reimplementer who unifies the two cleanups, or who routes both through one exit hook, diverges from the shipped lifecycle — and a process that dies without its reservation being unmarked on that other path would leak range marks.

### State machine

The whole per-NC lock is the pair `(writer_acquired, reader_count)`:

| State | `(writer_acquired, reader_count)` | Meaning |
|---|---|---|
| FREE | `(false, 0)` | idle; no owner, key may be stale-zero |
| READERS | `(false, N>0)` | `N` inferences of `uuid` share the core |
| WRITER | `(true, 0)` | exclusive; a model load is mid-flight (transient) |
| — | `(true, N>0)` | **unreachable as stable**: writer-enter drains readers first (`:118`); downgrade `BUG_ON`s (`:173`) |

```text
   FREE ──ncrwl_writer_enter (:137)──▶ WRITER ──ncrwl_writer_downgrade (:175-176)──▶ READERS(1)
    ▲          (waits writer_acquired==false && reader_count==0)                          │
    │                                                                                     │
    └──── ncrwl_release_current_process (:276-278, on writer's process exit) ◀────────────┤
    │                                                                                     │
 FREE/READERS ──ncrwl_reader_enter (:71, only if !writer_acquired)──▶ READERS+1           │
                                  READERS ──ncrwl_reader_exit (:98)──▶ READERS-1 ─────────┘
```

---

## 5. The Global-Allocator-Mutation Note

The same translation unit hosts a second, structurally distinct mechanism: the **NC-range reservation allocator**. Where the per-NC lock arbitrates *one already-reserved core's* R/W access, the allocator hands out *which cores a process reserves* across the whole machine. It is a single file-static array indexed by global NC index, mapping each core to its owning tgid:

```c
// neuron_crwl.c:185 -- one entry per global NC; value = owning tgid (0 == free)
static pid_t ncrwl_range_pids[MAX_NEURON_DEVICE_COUNT * MAX_NC_PER_DEVICE] = {0};  // 64*8 = 512 slots
static int   ncrwl_range_mark_cnt = 0;                                            // :186
DEFINE_MUTEX(ncrwl_range_lock);                                                   // :187  guards both
```

`ncrwl_nc_range_mark` (`:188`) is a first-fit consecutive-range allocator: under `ncrwl_range_lock` it scans `[start, end]` for `nc_count` contiguous free slots, and on a fit stamps each with `task_tgid_nr(current)`, `set_bit`s the result map, increments `ncrwl_range_mark_cnt`, and fires the pod-election hook `ndhal->ndhal_npe.npe_notify_mark(prev_cnt, true)` (`:214`). `ncrwl_nc_range_unmark` (`:232`) walks all 512 slots and clears the bits the caller owns (`ncrwl_range_pids[i] == current tgid`, `:237`).

This allocator is the **global-mutation** half of CRWL, and it is security-relevant because it is reachable on the **ungated `O_WRONLY` free-access lane**. An `O_WRONLY` opener of `/dev/neuronN` routes to `ncdev_misc_ioctl`, which dispatches `CRWL_NC_RANGE_MARK`/`UNMARK` (cmds 85/86, `neuron_ioctl.h:766-769`) with **no** `npid_is_attached` attach gate and **no** capability check — so any process that can open the node, even write-only, can mutate this single device-spanning array shared by all 64 devices. It cannot steal another tgid's marks (unmark is pid-scoped, `:237`), but it can exhaust the 512-slot allocator and starve legitimate model-load reservations, and on V3 pod hardware the `npe_notify_mark` call placed *inside* the 512-iteration unmark loop (`:242`) amplifies into up-to-512 election notifications and can reset another process's pod mode. These are findings **S5** (ungated CRWL mark/unmark) and **S8** (notify amplification + cross-process mode reset) on the [attack-surface page](../security/ioctl-attack-surface.md); the first-fit algorithm, the suspicious `i = j + 1` loop advance (`:226`), and the election-hook placement are owned by [pod-election](pod-election.md).

> **NOTE —** the per-NC lock ([§1–§4](#1-the-struct-neuron_crwl-layout-and-the-per-device-array)) is *not* exposed on the free-access lane the way the range allocator is: `CRWL_READER_ENTER`/`EXIT`/`WRITER_ENTER`/`DOWNGRADE` (cmds 81–84) dispatch on the normal path. The global-mutation security concern is specifically the *range allocator's* static array plus its `O_WRONLY` reachability — not the per-device `nd->crwl[]` locks. A reimplementer hardening this surface must gate the **range** ioctls, not the per-NC ones.

---

## 6. The Cooperative-Spin Cost

> **QUIRK —** CRWL is a **cooperative spin-poll lock, not a blocking lock.** There is no `wait_queue_head_t`, no `rw_semaphore`, no `mutex` held across the wait, and no priority inheritance. A waiter drops `crwl->lock`, `usleep_range(10, 20)`s, and re-polls (`:55-64` reader, `:117-134` writer); the holder is never preempted or signalled. The costs a reimplementer inherits if they copy this design:
>
> - **No fairness / no FIFO.** Waiters are not queued. When a holder releases, whichever poller happens to re-acquire `crwl->lock` first and find the condition clear wins; a thread can be repeatedly beaten and only escapes via its cap.
> - **A crashed-but-not-exited holder is catastrophic.** If a writer sets `writer_acquired = true` and then hangs (without its process exiting), *every* reader blocks until the `~500 ms` reader cap trips and returns `-EBUSY`, and every writer until the `~2 s` cap. The lock cannot be reclaimed except by the holder's process dying (`ncrwl_release_current_process`, [§4](#4-release--downgrade--forced-cleanup)) — there is no watchdog and no steal.
> - **Busy-ish polling, not true sleep.** `usleep_range(10,20)` does sleep (it is not a `cpu_relax` busy-spin), but a contended writer performs up to `200001` such sleeps, each a timer setup/teardown; under heavy model-swap contention this is measurable scheduler overhead versus a wait-queue wake.
> - **Caps surface as `-EBUSY`, which the runtime must handle.** The lock can *fail to acquire* — a blocking `rw_semaphore` cannot. The userspace runtime must treat `-EBUSY` from reader/writer enter as a retryable outcome, not a fatal error. A reimplementation that swaps in a blocking rwsem changes the ABI contract (no `-EBUSY`) and must audit every caller that today retries on it.
>
> The design is deliberate: it keeps the kernel side simple and lets a *cooperative* userspace runtime (one process loading, then reading, the same model) coordinate without kernel wait-queue machinery — at the price of no fairness, no preemptive recovery, and a userspace-visible `-EBUSY` timeout.

---

## Function Map

| Function | `file:line` | Role | Confidence |
|---|---|---|---|
| `ncrwl_validate_uuid` | `:23` | UUID `memcmp` + `writer_pid == tgid` gate; `-ENOENT` on mismatch; `BUG_ON` on OOB `nc_index` | HIGH |
| `ncrwl_reader_enter` | `:41` | spin while `writer_acquired`, cap `50*1000`; validate; `reader_count++` | HIGH |
| `ncrwl_reader_exit` | `:78` | validate; underflow-guard `reader_count==0` → `-EINVAL`; else `reader_count--` | HIGH |
| `ncrwl_writer_enter` | `:105` | spin while `writer_acquired \|\| reader_count`, cap `200*1000`; set `writer_acquired`; `-EALREADY` fast path; stamp `(pid, uuid)` | HIGH |
| `ncrwl_writer_downgrade` | `:154` | require `writer_acquired`; validate; `BUG_ON(reader_count!=0)`; `writer_acquired=false`, `reader_count=1` | HIGH |
| `ncrwl_release_current_process` | `:267` | per-device exit cleanup: reset every NC whose `writer_pid == tgid` to FREE + wipe UUID | HIGH |
| `ncrwl_nc_range_mark` | `:188` | first-fit consecutive reservation over global `ncrwl_range_pids[512]`; `npe_notify_mark` hook — [pod-election](pod-election.md) | HIGH |
| `ncrwl_nc_range_unmark` | `:232` | pid-scoped clear of caller-owned range bits; `npe_notify_mark` inside loop (S8) | HIGH |
| `ncrwl_nc_range_pid_get` | `:247` | look up owning tgid of a global NC index; `0` == no owner | HIGH |
| `ncrwl_range_mark_cnt_get` | `:258` | return `ncrwl_range_mark_cnt` under `ncrwl_range_lock` | HIGH |

---

## Related Components

| Component | Relationship |
|---|---|
| `nd->crwl[MAX_NC_PER_DEVICE]` (`neuron_device.h:108`) | the per-NC lock array this page documents, embedded in `struct neuron_device` |
| `struct neuron_uuid` (`neuron_driver_shared.h:150-152`) | the 32-byte model-identity key; stamped by `writer_enter`, matched by `validate_uuid` |
| `ncdev_flush` / `ncdev_misc_flush` (`neuron_cdev.c:3452` / `:3442`) | the cdev close paths that call `ncrwl_release_current_process` (per-NC) and `ncrwl_nc_range_unmark` (range) — see [cdev-mmap](cdev-mmap.md) |
| `ndhal->ndhal_npe.npe_notify_mark` (`neuron_dhal.h`) | the pod-election hook fired by the range allocator on every mark/unmark — owned by [pod-election](pod-election.md) |
| `npid_is_attached` (`neuron_pid.c:60`) | the attach gate the range ioctls *skip* on the `O_WRONLY` lane (S5) |

## Cross-References

- [Char Device, fops and mmap](cdev-mmap.md) — the cdev close path that calls `ncrwl_release_current_process` (per-NC cleanup) and the `O_WRONLY` `ncdev_misc_flush` → `ncrwl_nc_range_unmark` that unmarks all this fd's reserved cores
- [Kernel Driver Overview](overview.md) — where the CRWL lock sits in the state plane and the per-open lifecycle (attach, flush, release) that brackets it
- [Pod Election (UltraServer)](pod-election.md) — the NC-range reservation allocator's first-fit algorithm, the `npe_notify_mark` election hook fired on every mark/unmark, and the `mark_cnt == 0` gate
- [The IOCTL Attack Surface (14 Findings)](../security/ioctl-attack-surface.md) — the global-allocator-mutation findings: S5 (ungated CRWL mark/unmark on the `O_WRONLY` lane) and S8 (`npe_notify_mark` amplification + cross-process pod-mode reset)
- [back to index](../index.md)
