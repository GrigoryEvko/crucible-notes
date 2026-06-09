# `pthread_atfork` Policy in libnrt

`libnrt.so.2` calls `pthread_atfork` exactly once, from inside
`nrt_init` immediately after `kmgr_init` returns. The three handlers
registered there encode the runtime's complete fork policy.

## The registration site

In `nrt_init` (decompiled from `0x95e88..0x95ea4`):

```
95e88   lea -0x2d7f(%rip), %rdx    # %rdx = atfork_child   (0x93110)
95e8f   lea -0x25b6(%rip), %rsi    # %rsi = atfork_parent  (0x938e0)
95e96   lea -0x2d9d(%rip), %rdi    # %rdi = atfork_prepare (0x93100)
95e9d   call __pthread_atfork      # (0x7ce650, bundled musl-style stub)
```

The bundled `__pthread_atfork` calls glibc's
`__register_atfork(prep, parent, child, dso_handle)` so the
handlers are unregistered if libnrt is ever `dlclose`d (it isn't).

If `__pthread_atfork` returns nonzero, `nrt_init` returns 1 (NRT_FAILURE)
without cleaning up already-spawned worker threads. This is a
documented but unfixable corner case — pthread_atfork can fail only
when out of memory, in which case the process is doomed regardless.

## The three handlers, in full

All three are decompiled below in their entirety:

### `atfork_prepare` (`0x93100`)

```
0000000000093100 <_Z14atfork_preparev>:
   93100:    c3    ret
```

That's it. `atfork_prepare` is a single `ret`. **No locks are acquired
before fork.** The runtime relies on the child being poisoned (see
below) to make pre-fork lock state irrelevant.

### `atfork_parent` (`0x938e0`)

```
00000000000938e0 <_Z13atfork_parentv>:
   938e0:    c3    ret
```

Also a single `ret`. Nothing is released or restored in the parent
post-fork. The parent's worker threads continue exactly as they were;
the only side effect of fork() in the parent is the additional cost
of the kernel-side `copy_process` work.

### `atfork_child` (`0x93110`)

```
0000000000093110 <_Z12atfork_childv>:
   93110:    bf 02 00 00 00      mov $0x2, %edi
   93115:    e9 76 5f 02 00      jmp b9090 <nrt_state_set>
```

Two instructions: load the constant `2` (= `NRT_STATE_CHILD`) into
`%edi`, then jump-tail-call into `nrt_state_set`. The state field
at `0xc5d1a0` flips from 1 (INIT) to 2 (CHILD) in the child's
address space only.

## How the child guard kicks in

Every public entry point in libnrt starts with a state guard. In
`nrt_init`:

```
if (nrt_init_state == NRT_STATE_INIT)
    return NRT_SUCCESS;                  // idempotent re-init
if (nrt_init_state == NRT_STATE_CLOSED)
    return NRT_FAILURE_AFTER_CLOSE;      // 14
// any other state, including CHILD (2):
nlog_write("Incompatible runtime state: %s", nrt_state_get_string());
return 1;                                // NRT_FAILURE
```

In `nrt_execute`:

```
if (nrt_init_state != NRT_STATE_INIT) {
    if (nrt_init_state == NRT_STATE_START)   return NRT_UNINITIALIZED;  // 13
    if (nrt_init_state == NRT_STATE_CLOSED)  return NRT_CLOSED;         // 14
    return NRT_FAILURE;                                                  // 1 (child case)
}
```

`nrt_close` accepts state-2:

```
nrt_close():
    nrt_state_set(NRT_STATE_CLOSED);  // 3
    // no destructors run on the worker threads — they don't exist in the child
```

This means the child can call `nrt_close()` safely, but any
non-`nrt_close` API returns failure with a descriptive string.

## Why the empty prepare/parent handlers?

The conventional pthread_atfork pattern is:

```c
atfork_prepare:  pthread_mutex_lock(every_lock_in_existence);
atfork_parent:   pthread_mutex_unlock(every_lock);
atfork_child:    pthread_mutex_unlock(every_lock);
```

This is sound but expensive — and for libnrt, ineffective. Reasons:

1. **The worker threads are gone in the child anyway.** `fork()` only
   carries the calling thread. The XU worker, async-exec worker,
   enc_proxy worker, OFI service, KV-store trio — all gone. The
   child's data structures reference threads that don't exist; a
   `submit_work_lock` released in the child can never be re-acquired
   by the missing worker.

2. **The device-side state is invalidly shared.** The kernel-side
   CRWL claim and the BAR mmaps still belong to the parent's fd.
   The child has copies of the fd numbers via fork(), but using them
   would race the parent and corrupt the device.

3. **Acquiring locks in prepare is itself a deadlock hazard.** With
   approximately 25 mutexes and a heterogeneous acquisition order,
   even taking them all in some canonical order risks blocking on
   a lock currently held by a non-calling thread.

The chosen policy — poison the child, do nothing in prepare/parent —
is the only sane response. It makes fork() safe (the child can
exit cleanly) without claiming the child can use the runtime.

## Practical implications

### Python `multiprocessing` modes

- **`spawn`** (default on macOS, recommended on Linux for libnrt):
  child does fork+exec, so the libnrt state in the child is fresh
  (state = START), and the child can call `nrt_init` to bring up its
  own runtime. This is the supported mode.

- **`forkserver`** (Linux only): same — fork+exec at the forkserver,
  fresh state in children.

- **`fork`** (Linux default in Python ≤ 3.7, deprecated in 3.13+):
  child inherits libnrt's `NRT_STATE_CHILD`, cannot run inference.
  Frameworks document this with environment variable hints like
  `NEURON_PYTHON_MP_USE_SPAWN=1`.

### PyTorch DataLoader workers

DataLoader workers do `fork+exec` on Linux when configured with
`multiprocessing_context='spawn'`, and `fork-only` when configured
with `'fork'`. If a DataLoader worker tries to invoke libtorchneuron
inference (which it shouldn't), the fork-only mode will fail with
"Incompatible runtime state: CHILD". This is by design.

### Direct `os.fork()`

A user that calls `os.fork()` in a libnrt-using process gets a
poisoned child. The recommended pattern is:

```python
pid = os.fork()
if pid == 0:
    # child
    os._exit(0)  # do not run atexit handlers
else:
    # parent: continue normally
    os.waitpid(pid, 0)
```

`os._exit` bypasses the atexit handlers, avoiding any chance the
child reaches `nrt_close` (which would still set state=3 cleanly,
but uses the global error log mutex on the way out).

### C/C++ frameworks

A C++ framework using libnrt should:

- Either avoid fork() entirely
- Or call `_exit(2)` in the child immediately, before any libnrt
  destructor or any code that could re-enter the runtime
- Or `execve()` immediately

## What about exec() after fork()?

`execve()` replaces the process image, releasing all heap state,
TLS, locks, fds (except those with `FD_CLOEXEC`). The eventfds and
epoll fd created by libnrt do **not** have `*_CLOEXEC` set, so they
remain open in the post-exec child until explicitly closed. This is
mostly cosmetic: the new image won't access them, but `lsof` would
show them dangling until the process exits.

A defensive framework could set `FD_CLOEXEC` on the eventfds after
they're created by calling `fcntl(fd, F_SETFD, FD_CLOEXEC)`. This
would require either a libnrt patch or a post-init userland sweep
through `/proc/self/fd`.

## Race with worker threads at fork-time

Suppose a worker thread is mid-iteration of `tpb_xu_step` when the
main thread calls `fork()`. The fork-time semantics are:

1. The kernel pauses all other threads (only the calling thread is
   carried into the child).
2. The child's address space is a copy-on-write image of the parent's.
3. The child runs `atfork_child` → state = CHILD.
4. The child does whatever the application does next.

The worker's mid-iteration state is captured in the child's memory
image but the worker thread itself doesn't exist there. The `mark_comp_lock`
that the worker was holding stays "locked" in the child's mutex copy,
but no code will ever try to acquire it (the child can't reach
`tpb_xu_step` because of the state guard).

The parent's worker thread continues without disruption, holding
the lock it was holding before fork, and releasing it at the next
`pthread_mutex_unlock` call. The fork is genuinely transparent to
the parent.

## Edge case: fork during `nrt_init`

If fork() happens during `nrt_init` (before `pthread_atfork` is
called), then the atfork handlers are not yet registered. The child
would inherit whatever partial state `nrt_init` had set up. This is
exceedingly rare (`nrt_init` is normally called once at startup) but
the runtime cannot guard against it.

If fork happens AFTER pthread_atfork but BEFORE state is set to
INIT (i.e., between `__pthread_atfork` returning and
`nrt_state_set(NRT_STATE_INIT)` running at the end of nrt_init), the
atfork_child handler runs, setting state to CHILD. But state was
START (0) before atfork_child, so the transition is `START → CHILD`,
not `INIT → CHILD`. Any subsequent API call in the child sees
state=2 and returns NRT_FAILURE. This is still correct, just
slightly counter-intuitive — the child has a CHILD state without
having had an INIT state in this process image.

## Summary of decisions

The fork policy is summarized in one sentence:

> The child is poisoned, the parent is unchanged, the lock state is
> irrelevant.

This is sound because:

1. Worker threads do not survive fork(), so the in-flight inference
   state is unrecoverable in the child anyway.
2. The device-side CRWL claim and BAR mmaps belong to the parent's
   fd, not the child's.
3. The user has clear escape hatches: `_exit(2)`, `execve()`, or
   the multiprocessing `spawn` mode.

Frameworks that need post-fork inference must use spawn (fork+exec)
or run inference in a dedicated process from the start.
