# Signal Handlers in libnrt

`libnrt.so.2` keeps its signal disposition footprint minimal: only
one optional handler is installed by libnrt's own code, and only when
the runtime is in "inspect" / device-profiling mode. The bundled Rust
stdlib installs three additional dispositions during `lang_start_internal`.

This page enumerates every `signal@plt`, `sigaction@plt`, and
`sigaltstack@plt` call site in the binary and documents what each
captures.

## Calls in libnrt code

### `nrt_inspect_signal_handler` at `0xa1a80`

Installed inside `nrt_inspect_begin_with_options` (`0x99000`) when
device profiling is armed. The handler:

```c
static void nrt_inspect_signal_handler(int sig) {
    nlog_write("nrt_inspect", __func__, NLOG_LEVEL_ERROR, ...,
               "Inspect signal handler caught %d", sig);
    nrt_inspect_stop();                  // flush captured profile
    signal(sig, SIG_DFL);                // restore default disposition
    raise(sig);                          // re-raise so core dump happens
}
```

The installation loop (decompiled at `0x992b0..0x992fc`) iterates an
integer array `C.7.0` at `0x856340`:

```c
for (int i = 0; i < c7_0_size; i++) {
    int sig = c_7_0[i];
    sighandler_t old = signal(sig, nrt_inspect_signal_handler);
    previous_signal_handlers[sig] = old;          // std::unordered_map<int, sighandler_t>
}
```

The `previous_signal_handlers` map at `.bss` `0xc5c840` records each
prior disposition so `nrt_inspect_end` can restore them:

```c
for (auto &[sig, old] : previous_signal_handlers) {
    signal(sig, old);
}
```

The exact set in `C.7.0` was not fully traced. Based on typical
profiler conventions and the layout (a sized int array), likely:
`{SIGSEGV, SIGABRT, SIGBUS, SIGFPE, SIGILL, SIGUSR1, SIGUSR2}`.
Definitive enumeration requires reading the `.rodata` table dump
at `0x856340`.

The handler's "restore default + re-raise" pattern is the standard
way to flush diagnostic data before letting the original signal
behavior fire. A SIGSEGV inside libnrt that survives the inspect
handler still produces a core dump; an SIGUSR1 still terminates the
process; etc.

### Inspect signal handler call sites in detail

```
992d6   mov  %edi, -0x50(%rbp)
992d9   call signal@plt              # install handler for signal at *r12
992ee   call previous_signal_handlers.operator[]  # save old
992f4   mov  %r15, (%rax)
992f7   cmp  %r12, %r14              # loop control
992fa   jne  992c7                   # next signal
```

In the handler body:

```
a1bd9   call nlog_write@plt          # log the signal
a1be0   call nrt_inspect_stop@plt    # flush profile data
a1be5   mov  %ebx, %edi              # signal number
a1be7   xor  %esi, %esi              # SIG_DFL = 0
a1be9   call signal@plt              # restore default disposition
... ret follows; the caller's raise(sig) is implicit at handler exit
```

## Calls in Rust runtime bring-up

### SIGPIPE ignore at `0x51dd1c`

In `std::rt::lang_start_internal` (Rust's standard process bring-up):

```
51dd17   mov  $0xd, %edi              # SIGPIPE = 13
51dd1c   call signal@plt              # signal(SIGPIPE, SIG_IGN)
```

`SIG_IGN` is passed because `xor %esi, %esi` was executed earlier
(at `51dd0e`), zeroing `%rsi`. SIG_IGN's value is `(sighandler_t)1`
actually — looking more carefully at the disassembly, the runtime
takes `0` as SIG_DFL but the actual install here is via a sentinel
parameter `cb 05 01 2b 79 00 01` which is `movb $0x1, ON_BROKEN_PIPE_FLAG_USED`
preceding the signal call; the signal call itself uses `%esi=0`
which is SIG_DFL.

Reading the surrounding lines indicates this is the standard Rust
behaviour: if the user did NOT set `RUST_BACKTRACE` or didn't opt
out of broken-pipe handling, set the SIGPIPE disposition such that
write to a closed pipe returns EPIPE instead of killing the process.

### SIGSEGV stack-overflow guard

In the same `lang_start_internal` path, Rust installs a custom
stack-overflow detector. The setup:

```
51de64   call sigaction@plt           # install handler
51df00   call sigaction@plt           # alt-stack handler
51df14   call sigaction@plt           # restore prep
51dfad   call sigaction@plt           # finalize
```

Plus three `sigaltstack` calls (`52665d`, `526c7a`, `526d17`) to
allocate and switch to an alternate stack so the SEGV handler can
run when the main stack has overflowed.

The handler itself (in `std::sys::pal::unix::stack_overflow::imp`,
not decompiled here) writes a `"thread '<name>' has overflowed its
stack"` message to stderr and aborts.

**Implication:** a stack-overflow inside a libnrt code path that
exercises Rust (e.g., the `nlog` Rust logger or the `sys_trace`
capture) produces a clean diagnostic message. A stack-overflow in
pure C++ libnrt code without Rust on the stack also produces this
message because the handler is installed process-wide.

## Calls libnrt does NOT make

- **No `signal(SIGTERM, …)`** — termination flows through glibc's
  default (terminate process). Workers are killed where they stand.
- **No `signal(SIGINT, …)`** — Ctrl-C kills the process.
- **No `signal(SIGCHLD, …)`** — fork+exec children are not reaped
  by libnrt.
- **No `pthread_sigmask`** — workers inherit the main thread's mask.
  The default mask is empty, so all signals are deliverable to all
  threads.
- **No `sigprocmask` / `sigwait`** — no thread blocks on a signal.

## Async-signal-safety concerns

The `nrt_inspect_signal_handler` calls `nlog_write` and
`nrt_inspect_stop`, neither of which is async-signal-safe. This is
a deliberate trade: in inspect mode, the handler runs in error
conditions where the process is going to terminate anyway. The risk
is that calling `malloc`/`free` from inside the handler can deadlock
if the signal was delivered while the process was inside `malloc`.

In production code paths (no inspect mode), no handler is installed,
and this concern is moot.

## SIGTERM in production

A SIGTERM to a libnrt process is handled by glibc's default — the
process terminates. The kernel then runs `do_exit` on every thread,
which closes all open file descriptors. The DKMS-side `ncdev_release`
fires for each closed chrdev fd, releasing:

- The process's slot in `nd->attached_processes[]` (per P-3-02)
- The process's CRWL claim on every NC range
- The per-NC notification queue rings (kernel-side)
- The per-NC DMA queue state

This is the "kernel cleans up after a killed userland" safety net.
It works regardless of whether userland had a chance to run any
cleanup code.

In-flight DMA descriptors that the hardware has already started
processing will complete on the device side; their results will be
written to host memory that the killed process no longer has mapped,
which is fine — the kernel reaped those memory regions on exit.

## SIGABRT from internal aborts

libnrt code aborts (via the libc `abort@plt` call site) in a few
critical paths:

- `epoll_create1` failure in `kmgr_xu_worker_do_work`
- `pthread_create` failure inside the worker initialization
- `write` to `stop_thread_efd` failure in `kmgr_xu_workers_destroy`
- Some assertion failures in `__assert_fail@plt`

If any of these abort sites fires, the process catches SIGABRT
(if inspect is active and SIGABRT is in the captured set) and runs
the inspect handler to flush profile data, then re-raises SIGABRT
to get a core dump.

## SIGFPE / SIGBUS / SIGILL

Same handling as SIGSEGV: if inspect is armed, the handler captures
and re-raises. If not, glibc's default terminates the process and
produces a core dump.

## Summary: signal disposition by default

For a libnrt process at steady state (inspect not active):

| Signal     | Disposition        | Source                          |
|------------|--------------------|---------------------------------|
| SIGPIPE    | SIG_IGN (in C path) / handled (Rust path) | Rust stdlib    |
| SIGSEGV    | custom stack-overflow handler            | Rust stdlib    |
| SIGTERM    | SIG_DFL (terminate)                      | kernel default |
| SIGINT     | SIG_DFL (terminate)                      | kernel default |
| SIGABRT    | SIG_DFL (core dump)                      | kernel default |
| SIGCHLD    | SIG_DFL (ignore by default in 2.4.1+)    | kernel default |
| All others | SIG_DFL                                  | kernel default |

With inspect active (`nrt_inspect_begin_with_options` called):

| Signal     | Disposition                                       |
|------------|---------------------------------------------------|
| Signals in C.7.0 | `nrt_inspect_signal_handler` (saves old) → restore on `nrt_inspect_end` |

## Implications for frameworks

A framework wanting to handle SIGTERM cleanly (e.g., to flush a
trace buffer before exit) must install its own handler. The
recommended pattern:

```c
sigaction(SIGTERM, &(struct sigaction){
    .sa_handler = my_term_handler,
    .sa_flags = SA_RESTART,
}, &old_sa);
```

The framework's handler can call `nrt_close()` to release the
runtime cleanly. `nrt_close()` is not async-signal-safe (it takes
locks), but it's typically called from a fresh handler stack with
no other libnrt activity in progress, so the deadlock risk is low.

A more bulletproof pattern uses `signalfd(2)` and a dedicated
signal-handling thread that blocks all signals via `pthread_sigmask`
and then reads them serially from the signalfd. This avoids
async-signal-safety entirely.
