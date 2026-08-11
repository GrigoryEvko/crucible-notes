# Defaults and Config-File Model

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython modules under `neuronxcc/`). The cp311 and cp312 wheels carry the byte-identical module layout (same three `.so` files at the same paths) and the same negative result. Treat every address as version-pinned.*

## Abstract

This page documents a **negative result**, and the negative is the whole point: `neuronx_cc` has **no config-file mechanism**. There is no argparse response-file (`fromfile_prefix_chars` / `@file`), no `.cfg` / `.ini` / `.rc` / `.json` / `.toml` / `.yaml` settings reader, no `configparser`, and no `getenv` read in the option layer. Every option the compiler honors arrives through one channel — `sys.argv` — and every default is bound at the moment the flag is registered, by the ordinary argparse `default=` keyword on the `add_argument` call. A reader who goes looking for a `neuronx-cc.conf`, a `~/.neuronxccrc`, or an `@options.txt` response file will not find one because the code to read it was never written. This page proves that, so nobody hunts for a file that does not exist.

The proof rests on an exhaustive string-pool and symbol scan of the three modules that own the option machinery — `penguin/Options.so` (the Penguin backend's `CommandLineParser`), `driver/Arguments.so` (the top-level `InterceptingArgumentParser`), and `driver/CommandDriver.so` (the dispatcher that owns `sys.argv`) — widened to the whole `neuronxcc` wheel. None of them link argparse's fromfile internals; none import `configparser`; none call `getenv`. The single `environ` interned name that survives the scan lives in `CommandDriver` alongside `multiprocessing` / `Process` and is the child-process environment handed to the fork, not an option source. The familiar trap — ELF section names `.init` / `.init_array` matching a naive `.ini` grep — is called out below so it does not masquerade as a config reader.

The positive half is short. Defaults are *distributed*, not centralized: there is no monolithic "defaults dataclass." Each flag's default is the `default=` argument on its `add_argument` call; argparse seeds the result `Namespace` with those defaults before parsing; `sys.argv` overrides only the flags the user actually passed. `RecordUsedAction` (in `driver/Actions.so`) exists precisely to distinguish a user-supplied value from a pre-seeded default. That is the entire model.

For reimplementation, the contract is:

- **The only inbound option channel is `sys.argv`** — passed to `CommandDriver.main` → `InterceptingArgumentParser.parse_known_args`. Reproduce nothing else; there is nothing else.
- **Defaults are registration-time** — bound by `add_argument(..., default=…)` and mirrored into `_ArgumentRegistry.arguments_by_context` for help/introspection. There is no second resolution pass and no override file.
- **The absences are load-free** — a faithful reimplementation must *omit* `fromfile_prefix_chars`, omit any config reader, and omit env reads in the option path. Adding any of them is a divergence from the binary.

| | |
|---|---|
| **Inbound option source** | `sys.argv` only — `CommandDriver.main` @ `0x17a90` → `parse_known_args` |
| **Top parser** | `InterceptingArgumentParser` (`driver/Arguments.so`); defaults via argparse `default=` |
| **Default store (introspection)** | `_ArgumentRegistry.arguments_by_context` (`OrderedDict`; the literal is in the pool) |
| **Penguin backend parser** | `CommandLineParser` singleton (`penguin/Options.so`); flat-string `parseOptions` |
| **Config-file readers** | **none** (no `.cfg`/`.ini`/`.rc`/`.json`/`configparser`) |
| **Response-file (`@file`)** | **none** (no `fromfile_prefix_chars`, no `_read_args_from_files`) |
| **`getenv` in option path** | **none** (PLT has no `getenv`; one `environ` name = child-fork env) |

## The only option source is `sys.argv`

There is exactly one door. `CommandDriver.main` (`@0x17a90`) builds the top `InterceptingArgumentParser` and calls `parse_known_args` on the process argument vector — the interned names `argv`, `parse_known_args`, `known_args`, and `delegated_args` are all present in `CommandDriver.so`'s pool. `parse_known_args` splits the vector into the driver-level `Namespace` and the leftover `delegated_args`, which become the sub-command's argv. Nothing in this path consults a file or the environment.

```text
sys.argv
   │
   ▼
CommandDriver.main  (0x17a90)
   │  builds InterceptingArgumentParser (Arguments.so)
   ▼
parse_known_args(argv)        ── 'argv','known_args','delegated_args' in pool
   │
   ├─► driver Namespace        (global flags; defaults pre-seeded, argv overrides)
   └─► delegated_args ────────► CompileCommand consumes (subcommand argv)
```

> **NOTE — `NEURON_CC_FLAGS` is not an exception.** The well-known `NEURON_CC_FLAGS` environment variable is read by the *framework integration* (PyTorch-XLA / JAX glue) **outside** the wheel; that code prepends its tokens to `argv` before `neuronx-cc` ever runs. By the time `CommandDriver.main` sees them they are ordinary argv tokens. The wheel itself performs no `getenv` for options — see [3.11 Env-Var Catalog](env-var-catalog.md). This is why a string scan of the option modules finds no env read even though the compiler "respects" an env var.

### Defaults are bound at registration, not resolved from a file

A default is not looked up; it is *seeded*. When a flag is registered with `add_argument(name, default=X, …)`, argparse stores `X` on the action and writes it into the `Namespace` before parsing begins; `parse_known_args` then overwrites only the entries whose flags appeared on the command line. `Arguments.so`'s pool confirms the registration-time API — `add_argument`, `default`, `get_default`, `parse_known_args` — and notably **lacks** any `set_defaults`-from-file or second-pass override. The custom layer adds bookkeeping (`kind`, `job`) but changes none of this.

```c
// The defaults-resolution flow — models InterceptingArgumentParser
//   (driver/Arguments.so) over stock argparse. No file is ever consulted.

function register_flag(parser, name, default, kind, job, **kw):     // add_argument @0x11320 / 0x19210
    action = super_add_argument(name, default=default, **kw)        // argparse binds default onto the action
    captured = action.default                                       // == get_default(name); the registration-time value
    registry.storeArgument(context, action, kind, job, captured)    // mirror into arguments_by_context (B.2)
    return action                                                   // NO config/env consulted anywhere here

function resolve_options(argv):                                     // CommandDriver.main @0x17a90
    ns = Namespace()
    for action in parser._actions:                                  // argparse pre-seeds every default
        setattr(ns, action.dest, action.default)                   // <-- this IS the default mechanism
    driver_ns, delegated = parser.parse_known_args(argv, ns)        // argv overrides only the flags the user passed
    return driver_ns, delegated                                    // sole inputs: argv + the seeded defaults
```

> **QUIRK — defaults are distributed, not a dataclass.** There is no single "Options defaults object" to find. Each flag carries its own `default=`; the only aggregate view is `_ArgumentRegistry.arguments_by_context` (an `OrderedDict`, literal `arguments_by_context` confirmed in `Arguments.so`), which mirrors each `(argument, kind, job, default)` record purely for help/introspection — it is a *read model* for `print_help_hidden`, not the resolution mechanism. `RecordUsedAction` (`driver/Actions.so`) exists only because defaults are pre-seeded: it marks a flag as explicitly-set so downstream code can tell a real user value from a seeded default. See [3.2 Two-Parser Architecture](two-parser-architecture.md).

## The exhaustive absence scan

A negative result is only as strong as the scan behind it. The method: `strings -a` over each `.so`'s full string pool plus `objdump -T` over its dynamic symbol/PLT table, then widened to a wheel-wide `grep -rIl` over every `*.so`. Counts below are literal `rg -c` hits in the pool; `0` means the token does not appear anywhere in the module — neither as a Python identifier (`__pyx_n_s_*`), a literal, nor a docstring.

| Scanned-for | Options.so | Arguments.so | CommandDriver.so | Wheel-wide | Verdict |
|---|---|---|---|---|---|
| `fromfile_prefix_chars` | 0 | 0 | 0 | 0 files | **absent** — no `@`-response file |
| `_read_args_from_files` (argparse fromfile internal) | 0 | 0 | 0 | 0 files | **absent** — fromfile path never linked |
| `prefix_chars` (override) | 0 | 0 | 0 | — | **absent** — default `-` prefix only |
| `configparser` / `ConfigParser` | 0 | 0 | 0 | 0 files | **absent** — no INI parser |
| `.cfg` / `.rc` / `read_config` / `load_config` | 0 | 0 | 0 | — | **absent** — no config-file reader |
| `.toml` / `.yaml` / `.yml` | 0 | 0 | 0 | — | **absent** — no structured-config reader |
| `getenv` (PLT import) | 0 | 0 | 0 | — | **absent** — no C-level env read |
| `os.environ` read for an option | 0 | 0 | 1 name¹ | — | **absent as option source** (¹ = child-fork env) |
| `shlex.split` | 0 | 0 | 0² | — | **absent** — no shell-style re-tokenize |

¹ `CommandDriver.so` carries one `environ` interned name (`__pyx_n_s_environ`, `__pyx_k_environ`). It co-occurs in the pool with `multiprocessing`, `Process`, and `argv` and lives in `run_subcommand_in_process` (`@0x19110`) — it is the environment dict propagated to the forked child (C.3), not a read of any option key. No `getenv` appears in the PLT, so there is no C-level environment read at all.

² `CommandDriver.so` contains `shlex` paired only with `quote` (never `split`) — that is `shlex.quote` for the diagnostic command echo (`getReplayCommandLine`, [3.5 Sub-Tool Argv](subtool-argv.md)), not option parsing. `Options.so`'s `parseOptions` uses a bare `str.split()` (interned `split`, no `shlex` anywhere) to whitespace-tokenize its flat backend string — still not a file or env read.

> **GOTCHA — `.init` is not `.ini`.** A naive `rg -i '\.ini'` over any of these `.so` files returns two hits: `.init` and `.init_array`. Those are **ELF section names**, not an INI config reference — every shared object has them. The scan above excludes them by matching the literal token `configparser` / `.cfg` / `read_config` rather than the substring `ini`. A reimplementer auditing for config support must apply the same exclusion or they will "find" a config reader that is just the constructor section table.

### Where the brief expected a config and what is there instead

Three plausible config surfaces were checked explicitly, each resolving to an argv- or in-memory mechanism:

- **`@`-response files** (argparse's built-in): would require `fromfile_prefix_chars='@'` on a parser constructor and pull in `_read_args_from_files`. Both are absent in all three modules and wheel-wide. `neuronx-cc -O2 @opts.txt` would treat `@opts.txt` as a literal positional, not expand it.
- **A backend options file**: `penguin/Options.so`'s `CommandLineParser.parseOptions` (`@0xbf40`) does take a *string* of flags — but it is a string passed in memory (e.g. the value of a backend-options flag), split with `str.split()` and fed to `parse_args`. There is no `open()` of a settings file; `Options.so` is pure in-memory argparse over a passed-in string.
- **An env-driven defaults override**: would need `getenv` / `os.environ.get(KEY)` in the option path. The PLT has no `getenv` in any of the three; the one `environ` name is the fork env. The compiler's env-sensitivity is entirely upstream of the wheel ([3.11](env-var-catalog.md)).

## Evidence summary

Every claim on this page is an *absence*, so each one is stated with the specific token that would have betrayed a counter-example.

| Claim | How it was tested |
|---|---|
| No `fromfile_prefix_chars` / `@`-response file | argparse's fromfile support is opt-in through the constructor *and* through the `_read_args_from_files` method — either token would show. Both return zero across `Options.so`, `Arguments.so`, `CommandDriver.so`, and wheel-wide across every `*.so`. `InterceptingArgumentParser.__init__` (`@0x146a0`) passes no `fromfile_prefix_chars`. |
| No `configparser` / `.cfg` / `.ini` / `.rc` reader | scanned for the parser name, the file extensions, and the verbs `read_config` / `load_config`; all zero. The only `ini` substring hits are the ELF `.init` / `.init_array` sections, excluded by token-exact matching. |
| No `getenv` / env read in the option path | the PLT carries no `getenv` import in any of the three modules. The single Python `environ` name sits in `CommandDriver`'s `run_subcommand_in_process`, next to `multiprocessing`/`Process`/`argv` — the child-fork environment, not an option key. |
| `sys.argv` is the only inbound option source | a second source would surface as a file read or an env read feeding the parser; both are ruled out above. Positively, `argv`, `parse_known_args`, `known_args`, and `delegated_args` are all present and form the inbound chain documented here. |
| Defaults are bound at registration | a defaults file would need a `set_defaults`-from-file or a post-parse merge. `Arguments.so` exposes `add_argument`, `default`, `get_default`, and `arguments_by_context`, but no file-backed `set_defaults` and no second resolution pass. |

## Limits of this reading

- The exact instruction that touches `environ` in `run_subcommand_in_process` was not disassembled. Its use as the fork environment rests on pool co-residence with `multiprocessing`/`Process`/`argv` and the absence of any option-key string nearby; the narrower claim (it is not an option source) is firmer than the broader one (nothing in `CommandDriver` reads the environment at all).
- The registration-time binding of defaults is read directly. That *no* downstream code anywhere re-reads or overrides a default from a file is proven only for the three option modules — an override buried in an unrelated module cannot be excluded outright, though the wheel-wide config and fromfile scans returning zero make it unlikely.
- Input *model* files — the `.hlo`/`.pb` the compiler reads — are positional argv values, not an option config, and are out of scope here.

> **GOTCHA —** the name `penguin/Options.so` suggests the resolved top-level options object holding the `-O{0..3}` map. It is not that. `Options.so` is a *separate* backend lane — the Penguin `CommandLineParser` singleton — with per-flag defaults supplied at `addOption` time and no opt-level table at all. The top-level CLI and its defaults live in `driver/Arguments.so`, and the two parsers are independent; see [3.2 Two-Parser Architecture](two-parser-architecture.md).

## Related Components

| Component | Relationship |
|---|---|
| `driver/Arguments.so` — `InterceptingArgumentParser` | Owns the top-level argparse model; binds every default at `add_argument` time; mirrors into `arguments_by_context` |
| `driver/CommandDriver.so` — `main` / `run` | Receives `sys.argv`, runs `parse_known_args`; the sole inbound option door |
| `driver/Actions.so` — `RecordUsedAction` | Distinguishes a user-passed value from a pre-seeded default — exists *because* defaults are registration-time |
| `penguin/Options.so` — `CommandLineParser` | Independent backend parser over an in-memory flat string; has its own per-flag defaults, no config file |

## Cross-References

- [Two-Parser Architecture](two-parser-architecture.md) — 3.2; the top-level `InterceptingArgumentParser` vs. the Penguin `CommandLineParser`, and where each flag's default lives
- [Sub-Tool Argv Construction](subtool-argv.md) — 3.5; how the resolved `Namespace` is marshalled back into per-Job sub-tool argv (the only place `shlex.quote` appears)
- [Env-Var Catalog](env-var-catalog.md) — 3.11; `NEURON_CC_FLAGS` and friends are consumed by the framework integration *outside* the wheel, then prepended to argv
