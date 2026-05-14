# Subprocess Harness

## Abstract

One POSIX subprocess harness drives every external tool `tileiras` invokes.
The same launcher serves ptxas, nvdisasm, and any helper tool — argument
construction is tool-specific but process behavior is shared: argv/envp
setup, stdio redirection, optional timeout, child status decoding, stderr
capture, and resource-usage collection.

The harness itself is CUDA-agnostic. CUDA-specific arguments like
`--input-as-string`, `--knobs-file=<path>`, `--nv-host=<path>`, and
temporary cubin paths are assembled by the driver before it calls the
generic launcher.

## POSIX subprocess launcher

The launcher accepts an executable path, argv vector, envp vector,
redirection descriptors for stdin/stdout/stderr, optional timeout
information, and output buffers for diagnostics and resource usage.

| Path | Used when | Behavior |
| --- | --- | --- |
| `posix_spawn` | No session or resource-limit hook is needed. | Fast path with file actions and retry on interrupted spawn attempts. |
| `fork` + `exec` | `setsid` or process resource limits are requested. | Child applies redirection, limits, optional `setsid`, then executes the target. |

The fork path uses shell-compatible exit codes for exec failure — `127`
means the program was not found, `126` means the program existed but could
not be executed. The wait decoder maps those codes back into user-facing
diagnostics.

```c
int launch_child(ProcessSpec *spec, ChildProcess *child, string *error) {
    if (!spec->setsid && !spec->has_resource_limits)
        return spawn_with_posix_spawn(spec, child, error);

    pid_t pid = fork();
    if (pid < 0)
        return system_error(error, "Couldn't fork");
    if (pid == 0)
        exec_child_or_exit(spec);

    child->pid = pid;
    return 0;
}
```

## stderr-merge optimization

When stderr and stdout target the same sink, the launcher redirects stderr
with `dup2(stdout, stderr)` instead of opening the same destination twice.
This is the common ptxas shape — both streams fold into one in-memory
diagnostic buffer so the parent can report assembler failure with full
context.

## SIGALRM wait4 timeout

Timeout handling rides on `wait4` and `SIGALRM`. With a timeout enabled,
the parent installs a temporary alarm handler, arms `alarm(seconds)`, and
waits on the child. If `wait4` is interrupted by the alarm, the parent
sends `SIGKILL`, disarms the alarm, restores the old signal handler, and
reaps the child.

```c
int wait_child(pid_t pid, unsigned timeout_s, ProcessResult *result) {
    install_alarm_handler();
    if (timeout_s != 0)
        alarm(timeout_s);

    int status = 0;
    int rc = wait4(pid, &status, 0, &result->usage);
    if (rc < 0 && errno == EINTR && timeout_s != 0) {
        kill(pid, SIGKILL);
        alarm(0);
        restore_alarm_handler();
        waitpid(pid, &status, 0);
        return child_timed_out(result);
    }

    alarm(0);
    restore_alarm_handler();
    return decode_child_status(status, result);
}
```

Status decoding follows normal POSIX rules. A signaled child reports the
signal name and whether a core file was produced. Exit code `126` means the
program could not be executed; exit code `127` means command-not-found.
Other codes are returned directly to the caller.

## ptxas launcher

The ptxas adapter assembles a fixed argv shape around the serialized PTX text:

| Argv token | Origin | Purpose |
|---|---|---|
| `-arch sm_NN` | `--gpu-name` | Selects `sm_100`, `sm_103`, `sm_110`, `sm_120`, or `sm_121`. |
| `--opt-level N` | `--opt-level` | Forwards the driver optimization level, default `3`. |
| `--input-as-string <PTX>` | PTX serializer | Passes PTX text by argv instead of through a temporary PTX file. |
| `--knobs-file=<path>` | `MLIR_ENABLE_EVO` and `PTX_KNOBS_PATH` | Forwards a ptxas internal knob file when both env vars are set. |
| `--nv-host=<path>` | host-code path | Points ptxas at companion host-side code when present. |

The normal ptxas call takes the `posix_spawn` fast path and merges stdout
and stderr into one capture buffer. The assembled cubin comes back through
the child's stdout, not via a temporary output file.

## nvdisasm driver

The SASS dump adapter takes a command string, splits it into argv words,
and expects the first word to resolve through `PATH`. The documented
default is:

```text
nvdisasm -c
```

The adapter writes the ptxas-produced cubin to a temporary file, appends
that file path as the final argv element, launches the command, captures
stdout, and removes the temporary cubin if the driver created it. When the
caller provided an existing cubin path, lifetime management stays with the
caller.

```c
int dump_sass(const DumpOptions *opts, ByteSpan cubin, string *out) {
    Argv argv = split_command(opts->dump_sass_command);
    if (argv.len == 0)
        return error("Please provide a valid dumpSassCommand. For example, `nvdisasm -c`.");

    TempFile temp = write_temp_cubin(opts->input_base_name, cubin);
    argv_push(&argv, temp.path);

    int rc = run_child(argv, CAPTURE_STDOUT_AND_STDERR, out);
    remove_temp_file(&temp);
    return rc;
}
```

Dump-command failures surface as compile-path failures because the driver
treats SASS dumping as part of the requested output action.

## nvdisasm command-line construction

Once tileiras finishes its MLIR pipeline and PTX emission, the compile dispatcher `sub_57A8E0` shells out to two
external programs: `ptxas` (PTX to SASS) and `nvdisasm` (SASS to disassembled text, embedded as an ELF section). Both
invocations route through the subprocess helpers `sub_44A36C0` and `sub_44A3430`, with the raw-ostream sink `sub_6CF9C0`
draining the child's stdout back into the parent.

Every nvdisasm invocation starts with the literal 5-byte flag block `"-uumn"` stored at rodata `0x57BB97`. The literal
packs four flags into one argv token: `-u` (unsigned offsets), `-u` (literal second occurrence, triggering the
canonical re-entry behaviour known from nvdisasm pre-13.1), `-m` (mnemonic-only emission), and `-n` (no header). All
five bytes are pushed as one argv element rather than four separate flags.

With `config.sanitize == 1` — the only currently-defined sanitize mode, `memcheck` — the helper appends a 41-byte tail
starting with a leading space: ` -sanitize=memcheck -g-tmem-access-check`. The trailing flag `-g-tmem-access-check`
instruments tensor-memory (TMEM) loads and stores, a Blackwell-and-newer concern consistent with the
sm_100..sm_121 target table. The full sanitize-on argv therefore consists of the 5-byte `"-uumn"` token followed by the
41-byte tail token.

The dispatcher writes the literal `"nvdisasm -c"` at rodata `0x57B6BB` as the command-line prefix before the flag block.
Program path resolution leaves `nvdisasm` to PATH; the `-c` flag asks nvdisasm to emit a section-friendly format
suitable for embedding inside the final ELF relocatable.

| Rodata address | Literal contents | Role |
|---|---|---|
| `0x57B6BB` | `nvdisasm` | Program name, resolved through PATH. |
| `0x57B6C8` | `-c` | Section-friendly output format. |
| `0x57BB97` | `-uumn` | Combined flag block (unsigned / re-entry / mnemonic / no-header). |
| `0x57BB9C` | ` -sanitize=memcheck -g-tmem-access-check` | Sanitize tail, leading space included, appended only when `sanitize == 1`. |

The sibling ptxas invocation assembles its argv differently. The prefix is `"ptxas"`, followed by `--input-as-string`
and the PTX text as a sized string, then `--knobs-file=` with the optional knobs path from the env-var registry, and
finally `--nv-host=` with the host triple. The boundary-spec page
[ptxas Handoff Protocol](../boundaries/ptxas-handoff-protocol.md#subprocess-argv)
covers the ptxas side in detail; this section sticks to how the parent assembles the nvdisasm argv.

```c
void buildNvdisasmCmd(const TileirasProgram *p, ArgvBuilder *out) {
    argvPush(out, "nvdisasm");                                          // 0x57B6BB
    argvPush(out, "-c");                                                // 0x57B6C8
    argvPush(out, "-uumn");                                             // 0x57BB97 — 5 bytes, single token
    if (p->sanitize == 1) {
        argvPush(out, " -sanitize=memcheck -g-tmem-access-check");      // 0x57BB9C — leading space included
    }
}
```

With the argv vector built, `sub_44A3430` forks via `posix_spawn` and wires stdout and stderr through pipes back to
the parent. The parent drains the disassembly text from the stdout pipe via `sub_6CF9C0` (the raw_ostream sink) and
concatenates the captured bytes into the final ELF relocatable as a `.nvdisasm` section. The temporary cubin path
written by the SASS dump adapter is passed as the final argv element, exactly as the previous section described.

## Related pages

[ptxas Handoff Protocol](../boundaries/ptxas-handoff-protocol.md#subprocess-argv)
documents the ptxas side of the boundary including the PTX text protocol and
cubin return path; [Host Launch ABI and ptxas Knobs](host-launch-and-ptxas-knobs.md#ptxas-knobs-file-format)
covers the `--knobs-file=` grammar consumed by ptxas;
[Driver Env Vars and Runtime Gates](env-vars-and-runtime-gates.md#per-feature-runtime-gates)
catalogues the env-var registry that produces the optional `PTX_KNOBS_PATH`
forwarded into the ptxas argv;
[Driver CLI Options](cli-options.md#enum-valued-options-as-int32-codes)
documents the `--sanitize=memcheck` option that adds the nvdisasm sanitize
tail.
