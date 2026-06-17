# Optional Client/Daemon IPC Server

> *All symbols, line numbers, and string literals on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (`__buildtime__ = 'Apr 08 2026, 21:07:10 UTC'`). The two relevant modules ship as readable Python — `neuronxcc/cli/Client.py` (4609 B) and `neuronxcc/cli/Daemon.py` (9263 B) under the cp310/cp311/cp312 wheels; this page cites the cp310 copy. Other wheels differ; treat every line number as version-pinned.*

## Abstract

`neuronx_cc` has a second, rarely-used way to run a compile: a **persistent warm-process daemon** that a thin client drives over HTTP/1.1 carried on a Linux **abstract-namespace `AF_UNIX`** socket. The default `neuronx-cc compile …` invocation does *not* use this path — it runs `CompileCommand` in-process under `CommandDriver:main`. The Client/Daemon pair is a separate facility, reached only by `python -m neuronxcc.cli.Client`, whose purpose is to amortise the Python interpreter start-up plus the heavy `CompileCommand` import across many compiles by keeping one daemon resident (default 300 s idle TTL).

Mechanically: a `Client` (`Client.__init__`, `Client.py:19`) consults a `PIDLockFile`; if no live daemon is found it spawns one detached (with `OMP_NUM_THREADS=1`), polls the pidfile until it populates, then opens a `requests_unixsocket.Session`. To compile, `Client.run` (`Client.py:84`) `shlex.quote`-joins the user's argv and `POST`s it to `http+unix://\0com.amazon.neuronxcc.<pid>/compile?working_directory=<cwd>`. The `Daemon` (`Daemon.run`, `Daemon.py:180`) lives inside a `daemon.DaemonContext`, binds a bare `socketserver.UnixStreamServer` on the abstract address `"\0com.amazon.neuronxcc.<pid>"`, and dispatches each request through a `BaseHTTPRequestHandler` subclass (`Daemon.HttpHandler`, `Daemon.py:58`). The two routes are **`POST /compile`** and **`POST /exit`**; everything else is `404`.

The reader who has reverse-engineered the standalone `neuronx-cc` driver will recognise this as a classic pre-fork/warm-server wrapper. The deltas worth internalising: the transport is an *abstract* unix socket (leading-NUL name, no inode, no filesystem permission bits), the server is **single-threaded and fully serialized** (no `ThreadingMixIn`), and — the structural observation this page closes on — there is **no authentication or authorization** on the socket: `SO_PEERCRED` is read only to label a log line.

For reimplementation, the contract is:

- **The wire protocol** — abstract `AF_UNIX` `SOCK_STREAM` + HTTP/1.1; the `http+unix://\0…` URL encoding; the `POST /compile` request/response and the `/exit`, `404`, and `400` branches.
- **The lifecycle** — `PIDLockFile` probe → spawn → pidfile-poll backoff; `DaemonContext` double-fork; per-request `chdir` + fresh `CompileCommand`; SIGALRM idle shutdown.
- **The access-control surface** — what identity the daemon checks (none) and what a peer can therefore drive.

| | |
|---|---|
| **Client entry** | `neuronxcc.cli.Client` (prog `neuron-cc-client`), `Client.run` @ `Client.py:84` |
| **Daemon entry** | `neuronxcc.cli.Daemon` (prog `neuron-cc-daemon`), `Daemon.run` @ `Daemon.py:180` |
| **Transport** | HTTP/1.1 over abstract `AF_UNIX` `SOCK_STREAM` (`UnixStreamServer`, `Daemon.py:208`) |
| **Socket address** | `"\0com.amazon.neuronxcc.<daemonPid>"` (`Daemon.py:204,208`; `Client.py:81,96`) |
| **Routes** | `POST /compile` (`Daemon.py:150`), `POST /exit` (`Daemon.py:162`) |
| **Concurrency** | single-threaded `serve_forever`; `close_connection=True` (no keep-alive) |
| **Idle shutdown** | `SIGALRM`, default 300 s, re-armed per request (`Daemon.py:131,217`) |
| **Access control** | none — `SO_PEERCRED` read for logging only (`Daemon.py:134–138`) |
| **In-package callers** | zero — the shipped `neuronx-cc` entry point never imports `cli.*` |

---

## It is not the default path

> **NOTE —** This subsystem is optional and has no internal caller. The wheel's only `console_scripts` entry (`dist-info/entry_points.txt`) is `neuronx-cc = neuronxcc.driver.CommandDriver:main`. A grep across every `.py`/`.so` for `cli.Client`, `cli.Daemon`, `neuron-cc-client`, `neuron-cc-daemon`, `com.amazon.neuronxcc`, `requests_unixsocket`, or `neuron.pid` matches **only** `Client.py` and `Daemon.py` themselves; `neuronxcc/cli/__init__.py` is 0 bytes. **CONFIRMED.** The IPC layer is reachable solely via `python -m neuronxcc.cli.Client …`; the default compile runs `CompileCommand` in-process (see [3.3](compilecommand-pipeline.md)).

The runtime dependencies are pinned in `dist-info/METADATA`: `python-daemon>=2.2.4`, `requests-unixsocket>=0.1.5`, `psutil>=5.6.7`. None of these are vendored in the wheel — `PIDLockFile` (arriving transitively via `python-daemon`), `daemon.DaemonContext`, and `requests_unixsocket.Session` are third-party and their bodies are not bytes in this artifact; their semantics below are taken from those pinned versions and standard behaviour, tagged where so.

---

## Protocol Specification

### Purpose

A single HTTP/1.1 verb (`POST`) over a stream socket carries one compile request, frames the argv as the request body, names the working directory in the query string, and returns a status code with an optional text body. The protocol is deliberately minimal: no content negotiation, no keep-alive, no body beyond the argv string.

### Transport and address

The transport is a `SOCK_STREAM` `AF_UNIX` socket in the Linux **abstract namespace** — the address string begins with a single NUL byte, so it has no filesystem path, no inode, and is auto-unlinked when the last reference closes. `requests_unixsocket` provides the `http+unix://` URL scheme; its netloc carries the URL-decoded socket address, and `/`-separated path components after the netloc become the HTTP path. **CONFIRMED.**

```c
// Daemon side — Daemon.run, Daemon.py:204-208
abstractSocket = "com.amazon.neuronxcc." + str(os.getpid());   // py:204
// "Cannot have slashes!"  (comment py:203 — '/' would be eaten by the client URL parser)
server = UnixStreamServer("\0" + abstractSocket, HttpHandler); // py:208  leading NUL ⇒ abstract

// Client side — Client.__init__ / Client.run
abstractSocket = "com.amazon.neuronxcc." + str(daemonPid);     // Client.py:81 (pid read from pidfile)
url = "http+unix://\0" + abstractSocket + "/compile";          // Client.py:96
```

> **QUIRK — the daemon names its socket after its *own* pid, and the client rebuilds the same string from the pidfile.** There is no negotiation: the address is a pure function of the daemon pid (`com.amazon.neuronxcc.<pid>`), which the client learns by reading `PIDLockFile.read_pid()`. The component prefix `com.amazon.neuronxcc.` is fixed; the comment "Cannot have slashes!" (`Daemon.py:203`) records *why* the name avoids `/` — the client's URL parser path-separates on it.

### Request/response matrix

The dispatch lives in `do_POST` (`Daemon.py:128–178`). Only `POST` is implemented on the handler; `BaseHTTPRequestHandler` has no `do_GET`/`do_PUT`, so any other HTTP method yields the stdlib `501 Unsupported method` before this code runs.

| Verb | Path | Body | Query | Success resp | Source |
|---|---|---|---|---|---|
| POST | `/compile` | shlex-quoted argv (ASCII), `Content-Length`-framed | `?working_directory=<abs cwd>` (optional) | `200`, **empty body** | `Daemon.py:150–161` |
| POST | `/exit` | ignored | ignored | `200`; sets `server._BaseServer__shutdown_request=True` | `Daemon.py:162–166` |
| POST | *(other path)* | ignored | ignored | `404` `text/plain` body `Unrecognized command <path>` | `Daemon.py:167–171` |
| POST | *(any, on raise)* | — | — | `400` `text/plain` body `str(exception)` | `Daemon.py:172–178` |

Confidence: all four rows **CONFIRMED** from the literal branch structure in `do_POST`; the `501` for non-POST is **HIGH** (stdlib `BaseHTTPRequestHandler` behaviour, no `do_*` override exists).

> **GOTCHA — a successful `/compile` returns `200` with an empty body.** `send_response(200); end_headers()` (`Daemon.py:160–161`) is reached *after* `runPipeline` returns, with no `wfile.write`. The compiler's textual output goes to the logfile/console, **not** the HTTP body. `Client.run` only logs `response.text` when it is non-empty (`Client.py:107`). A reimplementer who expects the build log in the response will get nothing.

> **QUIRK — `/exit` does not call `server.shutdown()`.** It flips the private `_BaseServer__shutdown_request` flag (`Daemon.py:164`) so `serve_forever`'s own loop exits cleanly after the current request, rather than blocking on a cross-thread `shutdown()`. `/exit` has **no in-package caller**: `Client.run` only ever hits `/compile` (`Client.py:96`). It is an out-of-band admin/DoS hook. **HIGH.**

### Request body construction

The argv survives a `shlex.quote` → `shlex.split` round-trip exactly — whitespace, quotes, and globs are preserved as literal tokens.

```c
// Client — Client.run, Client.py:85-96
argString  = " ".join(shlex.quote(arg) for arg in compile_args);  // py:85
queryParams = {"working_directory": os.getcwd()};                  // py:89-90
response = session.post("http+unix://\0" + sock + "/compile",
                        data=argString, params=queryParams);        // py:96

// Daemon — HttpHandler.__compile, Daemon.py:68-70
size = int(self.headers.get("Content-Length", 0));                 // py:68
body = self.rfile.read(size).decode("ascii");                      // py:69  ASCII ONLY
argv = shlex.split(body);                                          // py:70
```

> **GOTCHA — the body is decoded `ascii`, not `utf-8` (`Daemon.py:69`).** Compile args containing non-ASCII (e.g. a Unicode path) would raise `UnicodeDecodeError` inside `do_POST`, get caught by the `except BaseException`, and return `400`. The Client does not re-encode, so this is latent rather than exercised, but a reimplementer who switches to `utf-8` on either side changes observable behaviour. **MED.**

### Peer-credential read (not an auth check)

For every POST, the handler reads `SO_PEERCRED` off the connected socket and unpacks the `(pid, uid, gid)` triple:

```c
// HttpHandler.do_POST, Daemon.py:134-138
peercred = self.server.socket.getsockopt(SOL_SOCKET, SO_PEERCRED,
                                         struct.calcsize("3i"));   // py:134
(pid, uid, gid) = struct.unpack("3i", peercred);                  // py:135
self.client_address = (pid,);   // workaround: unix sockets have no client_address; // py:138
                                // BaseHTTPRequestHandler.log_message assumes one exists
```

The unpacked `pid` exists **only** so `BaseHTTPRequestHandler.log_message` (overridden at `Daemon.py:64` to funnel through `logger.info`) has something to print — the comment at `Daemon.py:137` says exactly this. The `uid`/`gid` are read and discarded. **No branch consumes the credentials for an access decision.** **CONFIRMED** — see [§ Access-control surface](#access-control-surface).

---

## Server Model

### Purpose

The daemon is a single resident process serving compiles one at a time. There is no worker pool, no per-request thread, and no fork-per-request; the only thread it ever creates is a transient one used to break the SIGALRM-shutdown deadlock.

### Algorithm

```c
function Daemon_run(self):                                  // Daemon.py:180
    HttpHandler.inactivity_timeout = self.inactivity_timeout            // py:181
    // snapshot every open fd so detach does not close profiler/log fds
    files_preserve = [int(f) for f in os.listdir("/proc/self/fd/")]    // py:184
    Path(self.pid_file).parent.mkdir(parents=True, exist_ok=True)      // py:192

    with daemon.DaemonContext(                                          // py:194-202
            working_directory=".",          // stays in spawn cwd, NOT '/'
            stdout=sys.stdout, stderr=sys.stderr,
            detach_process=self.detach,      // default True (py:239)
            prevent_core=False,              // core dumps permitted (debug aid)
            files_preserve=files_preserve,   // keep inherited fds open
            pidfile=PIDLockFile(self.pid_file)):  // acquire lock + write post-fork pid
        abstractSocket = "com.amazon.neuronxcc." + str(os.getpid())    // py:204
        with UnixStreamServer("\0" + abstractSocket, HttpHandler) as server:  // py:208
            if self.inactivity_timeout:                                // py:209
                signal.signal(SIGALRM, timeoutHandler)                 // py:216
                signal.alarm(self.inactivity_timeout)                  // py:217
            server.serve_forever()                                     // py:219
    return os.EX_OK                                                     // py:221
```

### Concurrency

The server is a bare `socketserver.UnixStreamServer` (`Daemon.py:208`) — **no** `ThreadingMixIn`, **no** `ForkingMixIn`. Requests are fully serialized: one `do_POST` runs at a time inside `serve_forever`. A second client connecting mid-compile blocks in the accept backlog until the first returns. **CONFIRMED.** Keep-alive is disabled per request via `self.close_connection = True` (`Daemon.py:139`) — one request per connection.

Within a compile, `CompileCommand.runPipeline` (`Daemon.py:124`) runs its `driver.Pipeline` of `driver.Job`s sequentially, each `Job` spawning exactly one native `subprocess.Popen` child and `communicate()`-blocking before the next. Intra-stage native parallelism is pinned to one thread by `OMP_NUM_THREADS=1` in the daemon's inherited environment — set by the Client at spawn (`Client.py:56`). That env var is the only thread-control knob in this layer. **HIGH** (pipeline/job internals are Cython `.so`; symbol names recovered, bodies not decompiled — see [3.3](compilecommand-pipeline.md)).

### Idle shutdown via SIGALRM

```c
function timeoutHandler(signum, frame):                    // Daemon.py:212-214
    logger.info("Shutting down daemon due to inactivity")
    threading.Thread(target=server.shutdown).start()       // py:214  thread is MANDATORY
```

`signal.alarm(N)` is armed once at startup (`Daemon.py:217`, default `N=300`, `--inactivity-timeout 0` disables) and **re-armed on every POST** at the top of `do_POST` (`Daemon.py:130–131`), giving idle-TTL semantics. On fire, `timeoutHandler` spawns a thread to call `server.shutdown()`.

> **GOTCHA — the shutdown must run on a *separate* thread.** `BaseServer.shutdown()` blocks until `serve_forever`'s loop acknowledges; calling it directly from the signal handler (which runs on the main thread *inside* `serve_forever`) would deadlock. The `threading.Thread(...).start()` at `Daemon.py:214` is the deadlock-breaker, not an optimisation. **CONFIRMED.**

> **NOTE — a compile longer than `N` seconds is not interrupted.** The single-threaded server cannot deliver the alarm's effect until the current `do_POST` returns to the select loop, and the per-request re-arm (`Daemon.py:131`) pushes the deadline out anyway. The daemon shuts down only after an *idle* gap of `N` s. **HIGH.**

Beyond `SIGALRM`, `Daemon.py` installs no `SIGTERM`/`SIGINT`/`SIGCHLD` handler. `daemon.DaemonContext` installs its own default `signal_map` (python-daemon default: `SIGTTIN`/`SIGTTOU`/`SIGTSTP`→ignore, `SIGTERM`→terminate raising `SystemExit`), so `SIGTERM` triggers `DaemonContext.__exit__` → `PIDLockFile.release()` (clean pidfile removal); `SIGKILL` leaves a stale pidfile for the next client to garbage-collect. **HIGH** (DaemonContext behaviour is library-derived, not bytes in this wheel).

---

## Lifecycle

### Client startup and daemon spawn

```c
function Client_init(pid_file, inactivity_timeout, log_file, log_level):  // Client.py:19
    pidFile   = PIDLockFile(pid_file)              // default "neuron.pid"  // py:28
    daemonPid = pidFile.read_pid()                                          // py:30
    if daemonPid and psutil.pid_exists(daemonPid):     // LIVE daemon       // py:32
        reuse it (debug log)                                                // py:33
    else:
        if daemonPid and not psutil.pid_exists(daemonPid):  // STALE pidfile // py:35
            warn "Daemon pid … no such process … may have crashed"          // py:36-38
            os.remove(pid_file)                                             // py:39
        env = os.environ.copy(); env["OMP_NUM_THREADS"] = "1"              // py:55-56
        subprocess.check_call(                                             // py:65
            [sys.executable, "-m", "neuronxcc.cli.Daemon",
             "--log-level", log_level, "--pid-file", pid_file,
             "--inactivity-timeout", str(inactivity_timeout)
             (+ "--log-file", log_file if given)],   // py:43-60
            env=env, stdout=<logfile or None>, stderr=STDOUT)
        for attempt in 0..4:                          // poll pidfile        // py:69-75
            time.sleep(0.5 * pow(2, attempt))         // 0.5,1,2,4,8 s
            daemonPid = pidFile.read_pid(); if daemonPid: break
        if not daemonPid: raise "Failed to read daemon pidfile after 5 attempts"  // py:77-78
    self.session       = requests_unixsocket.Session()                     // py:80
    self.abstractSocket = "com.amazon.neuronxcc." + str(daemonPid)         // py:81
```

The pidfile-poll backoff is **0.5, 1.0, 2.0, 4.0, 8.0 s** (≈15.5 s max before raising), computed directly from `0.5 * pow(2, attempt)` (`Client.py:70`). **CONFIRMED.** `subprocess.check_call` blocks, but the spawned daemon detaches (`DaemonContext` double-fork), so `check_call` returns as soon as the intermediate process exits; the pidfile poll closes the race between detach and the post-fork pid write.

> **CORRECTION (D-A09/S2-01) —** an earlier pass implied `--verbose`/`--logfile`/`--logfile-verbose` are part of the daemon spawn argv. They are **not**. The spawn (`Client.py:43–60`) passes only `--log-level --pid-file --inactivity-timeout [--log-file]`; note `--verbose` is the *Client* CLI flag name (`Client.py:132`), whereas the daemon takes `--log-level` (`Daemon.py:242`). The `--verbose`/`--logfile`/`--logfile-verbose` triplet is injected later by the daemon into the *per-request* `CompileCommand`, not into the daemon's own argv (see below).

### PIDLockFile lifecycle

| Stage | Call | Effect |
|---|---|---|
| Client probe | `PIDLockFile(pid_file).read_pid()` (`Client.py:28,30`) | parse int from pidfile, or `None` |
| stale cleanup | `os.remove(pid_file)` (`Client.py:39`) | client deletes pidfile when pid is not alive (`psutil`) |
| daemon acquire | `DaemonContext(pidfile=PIDLockFile(...))` (`Daemon.py:201`) | on `__enter__`: atomic `O_CREAT\|O_EXCL` lock, write own post-fork pid |
| daemon release | `DaemonContext.__exit__` | `release()`: remove lockfile on clean shutdown |
| crash | — | pidfile orphaned; next client detects via `psutil.pid_exists→False`, removes, respawns |

The authoritative liveness check is `psutil.pid_exists(daemonPid)` (`Client.py:32,35`), not the lock — the pidfile is advisory. A `SIGKILL`'d daemon leaves a stale pidfile the next client garbage-collects. **HIGH.** A TOCTOU window exists (two clients racing on a stale pidfile, both removing+respawning); it is mitigated only by the losing daemon's `PIDLockFile.acquire()` `O_EXCL` failing, after which that daemon exits.

### Per-request `chdir` and fresh CompileCommand

```c
// do_POST /compile branch, Daemon.py:150-161
if path == "/compile":
    with ExitStack() as stack:
        if "working_directory" in queryParams:                   // py:152
            workingDir = Path(queryParams["working_directory"])  // py:153
            workingDir.mkdir(parents=True, exist_ok=True)        // py:155  CREATES if absent
            stack.enter_context(chdir(workingDir))               // py:156  restored on exit
        self.__compile()                                         // py:158
    send_response(200); end_headers()                            // py:160-161
```

`chdir` is `neuronxcc.driver.ContextUtils.chdir` — a context-manager class that saves cwd on enter, `os.chdir`s to the target, and restores on exit. Because it sits in an `ExitStack`, cwd is **always restored** after `__compile()`, even on exception. Each compile therefore runs in the client's cwd, then the daemon returns to its own. **CONFIRMED.**

> **GOTCHA — the daemon `mkdir -p`s the requested working directory.** `workingDir.mkdir(parents=True, exist_ok=True)` (`Daemon.py:155`) creates the path if it does not exist, *before* the chdir. The directory name comes verbatim from the client's `working_directory` query parameter. **CONFIRMED.** This matters for the access-control discussion below: a peer chooses the path and the daemon will create and enter it.

Inside `__compile` (`Daemon.py:67–126`) a **fresh** `CompileCommand(parent_command="neuronx-cc")` is built per request (`Daemon.py:72`); the `FIXME` at `Daemon.py:71` explains why — `CompileCommand.run()` mutates the shared argparser, so reuse would leak state. The daemon then injects three args into `compileCommand.optional_group`:

| Injected arg | Default | Semantics | Source |
|---|---|---|---|
| `--verbose` | `'user'` | console verbosity `{trace,debug,info,warning,error,critical,user}` via `to_numeric_level` | `Daemon.py:89` |
| `--logfile` | `'log-neuron-cc.txt'` | logfile name, `Path(val).resolve()` to abs | `Daemon.py:97` |
| `--logfile-verbose` | `'info'` | logfile verbosity via `to_numeric_level` | `Daemon.py:105` |

`to_numeric_level` (`Daemon.py:74–87`) maps `0→WARNING, 1→INFO, 2→DEBUG`, passes a bare digit string through, else asserts the lowercased token is in the level list via `neuron_assert('DAE', 2, …)` and returns `getattr(logging, level.upper())`. Then `buildPipeline(argv, logger)` (`Daemon.py:113`) assembles the pipeline; the daemon copies `vars(args)`, pops `metrics_timestamp` (changes every run) and `inputs` (input files are content-hashed elsewhere — comments `Daemon.py:116–117`), `pprint`-formats the rest at debug level, and calls `runPipeline` (`Daemon.py:124`). **CONFIRMED** for the Python glue; the `buildPipeline`/`runPipeline` internals are Cython `.so` (symbol names only) — see [3.3](compilecommand-pipeline.md).

### Client-side retry and exit code

`Client.run` retries the POST up to 5 times with the same 0.5/1/2/4/8 s backoff on any connection `Exception` (`Client.py:92–104`), then returns `os.EX_OK` if a `Response` arrived (`Client.py:106–110`) or `os.EX_DATAERR` (=65) if every attempt failed (`Client.py:111–113`).

> **GOTCHA — a daemon `400`/`404` still exits `EX_OK`.** `if response:` (`Client.py:106`) is truthy for *any* returned `requests.Response`, including `400` and `404`. A compile that failed inside the daemon (caught → `400` + error text) returns `EX_OK` from `neuron-cc-client` with the error merely logged. The caller cannot distinguish a clean compile from a daemon error by exit status. **HIGH.**

---

## Lifecycle Diagram

```text
  neuron-cc-client (ephemeral)                 neuron-cc-daemon (persistent, 1/pidfile)
  ============================                 =========================================
  PIDLockFile(neuron.pid).read_pid()
        |
        +-- live? (psutil.pid_exists) --YES--> reuse pid -----------------------------+
        |                                                                             |
        NO/stale (rm pidfile)                                                         |
        | check_call(python -m …Daemon --log-level L --pid-file P                     |
        |   --inactivity-timeout N [--log-file F]; env OMP_NUM_THREADS=1) --> DaemonContext.__enter__
        |                                                                     double-fork+setsid+detach
        |                                                                     files_preserve=all fds
        |                                                                     PIDLockFile.acquire()→write pid
        | poll read_pid() backoff .5,1,2,4,8s <----------------------------- UnixStreamServer("\0com.amazon.neuronxcc.<pid>")
        | abstractSocket = com.amazon.neuronxcc.<pid>                         SIGALRM handler; alarm(N)
        v                                                                    serve_forever()  <--------------+
  run(compile_args):                                                                                         |
    argString = ' '.join(shlex.quote(a))                                                                     |
    POST http+unix://\0<sock>/compile?working_directory=<cwd>                                                |
    body = argString  --(AF_UNIX abstract, HTTP/1.1, close_connection)--> do_POST:                           |
                                                                            signal.alarm(N)   # reset TTL     |
                                                                            SO_PEERCRED→(pid,uid,gid) [log]   |
                                                                            path=='/compile':                |
                                                                              mkdir -p cwd; chdir(cwd)        |
                                                                              CompileCommand(fresh)           |
                                                                              +--verbose/--logfile/...        |
                                                                              buildPipeline(argv)             |
                                                                              runPipeline → Pipeline          |
                                                                                Job1.Popen(native)→wait       |
                                                                                Job2.Popen(native)→wait …     |
                                                                              chdir back (ExitStack)          |
    <------------------- 200 (empty body) ------------------------------- send_response(200)                 |
    response.text logged; return EX_OK(0) / EX_DATAERR(65)                                                    |
                                                                            (loop) ---------------------------+
                                                                            ── N s idle ──> SIGALRM:
                                                                               Thread(server.shutdown).start()
                                                                               serve_forever() returns
                                                                               DaemonContext.__exit__
                                                                               PIDLockFile.release()
```

---

## Wire Example — `POST /compile`

A single warm compile of `model.hlo` with `--optlevel 2`. The client's cwd is `/work/job-7`; assume a live daemon at pid `40912`.

Client constructs (`Client.py:85,90,96`):

```text
argString   = "compile model.hlo --optlevel 2"          # shlex.quote no-ops here (no metachars)
queryParams = {"working_directory": "/work/job-7"}
URL         = http+unix://\0com.amazon.neuronxcc.40912/compile?working_directory=%2Fwork%2Fjob-7
```

Bytes on the abstract socket (`\0` shown as `<NUL>`; `requests` adds `Content-Length`):

```text
POST /compile?working_directory=%2Fwork%2Fjob-7 HTTP/1.1
Host: %00com.amazon.neuronxcc.40912
Accept-Encoding: gzip, deflate
Accept: */*
Connection: keep-alive
Content-Length: 31

compile model.hlo --optlevel 2
```

Daemon path (`Daemon.py:128→158`): re-arm `alarm(300)` → read `SO_PEERCRED` (logs `pid=…`) → `urlparse` path `/compile`, `parse_qsl` → `{"working_directory":"/work/job-7"}` → `mkdir -p /work/job-7`; `chdir` into it → `__compile`: `Content-Length=31`, read+`decode('ascii')`, `shlex.split` → `['compile','model.hlo','--optlevel','2']` → fresh `CompileCommand`, inject `--verbose/--logfile/--logfile-verbose`, `buildPipeline`, `runPipeline` (forks native Jobs) → chdir back. On success:

```text
HTTP/1.0 200 OK
Server: BaseHTTP/0.6 Python/3.10.x
Date: …
```

(empty body). The Client logs nothing from the body (`response.text` is empty), logs `"Compilation completed"`, and returns `EX_OK`. On any raised exception inside the `try` (`Daemon.py:172`), the response is instead `400 text/plain` with body `str(e)`; on an unknown path, `404 text/plain` body `Unrecognized command <path>`. **CONFIRMED** for the request/response structure; exact header text (`Server`, `HTTP/1.0`) is **HIGH** (stdlib `BaseHTTPRequestHandler` defaults, not re-derived from this wheel).

---

## Access-Control Surface

> **NOTE (security — structural observation, CONFIRMED) —** The daemon performs **no authentication and no authorization** on the abstract `AF_UNIX` socket. The only identity the handler reads is `SO_PEERCRED` (`Daemon.py:134–138`), and the unpacked `(pid, uid, gid)` is used solely to populate `self.client_address` for log formatting — no branch in `do_POST`/`__compile` consumes it for an access decision. There is no allow-list, no `uid` comparison, no token, no rate limiting, and no request-body cap beyond `Content-Length`.

This is a factual property of the code path, not an exploit. Stated neutrally for a reimplementer reasoning about the trust boundary:

- **The socket is in the abstract namespace.** Abstract `AF_UNIX` names are not protected by filesystem permission bits or an inode; reachability is scoped only by the Linux **network namespace**. Any process in the same net namespace that knows the address `com.amazon.neuronxcc.<pid>` (a pure function of the daemon pid, which is in the advisory pidfile and visible via `/proc`) can connect.
- **`/compile` runs an arbitrary compiler argv** (`shlex.split` of the request body → `CompileCommand`), in a **peer-chosen `working_directory`** that the daemon will `mkdir -p` and `chdir` into (`Daemon.py:155–156`), and which ultimately drives native `subprocess.Popen` stages (the Jobs).
- **`/exit` is an unauthenticated shutdown** (`Daemon.py:162–166`) that any peer can POST.

The earlier-pass label "no authn/authz" is upheld and tagged **CONFIRMED** here because the *absence* is directly visible in the code: every read of peer identity flows into logging, and the two routes execute unconditionally on path match. A reimplementer who needs a multi-tenant or networked deployment must add the access-control layer that this design intentionally omits — the design assumes a single trusted local user reusing their own warm compiler.

---

## Dead and Diagnostic Code

| Symbol | Location | Status |
|---|---|---|
| `LockDirectory` (class) | `Daemon.py:38–49` | **defined but never referenced** anywhere in `Daemon.py` — dead/legacy (`flock`-based dir lock) |
| `files_preserve_names` | `Daemon.py:185–190` | computed (`/proc/self/fd` readlinks) but **never used** — diagnostic remnant; only `files_preserve` (the fd ints) is passed to `DaemonContext` |

Both confirmed by reading every reference in `Daemon.py`. **HIGH.** A reimplementer can omit them with no behavioural change.

---

## Gaps & Provenance

- **G1 — pipeline internals.** `CompileCommand.buildPipeline`/`runPipeline`, the `driver.Pipeline`/`driver.Job` bodies, and `ContextUtils.chdir`/`Actions.str2bool`/`logging.Assert.neuron_assert` are Cython `.so` modules; only symbol names and string literals are recovered, bodies not decompiled. Job ordering is covered in [3.3](compilecommand-pipeline.md).
- **G2 — third-party semantics.** `PIDLockFile`, `daemon.DaemonContext`, and `requests_unixsocket.Session` are not vendored; their `O_EXCL` lock, double-fork/`signal_map`, and `http+unix://` URL handling are taken from the pinned versions (`python-daemon>=2.2.4`, `requests-unixsocket>=0.1.5`, `psutil>=5.6.7`) and standard behaviour, tagged **HIGH** where cited.
- **G3 — exact HTTP framing.** Response header bytes (`Server`, protocol version) are stdlib `BaseHTTPRequestHandler` defaults, shown illustratively; not re-derived from this wheel.

---

## Related Components

| Name | Relationship |
|---|---|
| `CommandDriver:main` | the **default**, in-process entry point; the daemon path is the alternative warm-process wrapper around the same `CompileCommand` (see [3.1](command-dispatcher.md)) |
| `CompileCommand` | the unit of work the daemon builds fresh per `/compile`; its `buildPipeline`/`runPipeline` do the real compile (see [3.3](compilecommand-pipeline.md)) |
| `driver.Pipeline` / `driver.Job` | the per-request native sub-tool process model run sequentially under `runPipeline` |

## Cross-References

- [The neuronx-cc Command Dispatcher & Subcommand Model](command-dispatcher.md) — the default `CommandDriver:main` path the daemon parallels; subcommand/argv model
- [CompileCommand Pipeline & the Canonical Job Order](compilecommand-pipeline.md) — what `buildPipeline`/`runPipeline` assemble and execute inside each `/compile` request
