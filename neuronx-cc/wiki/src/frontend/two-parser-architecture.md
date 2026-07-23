# The Two-Parser Architecture

> *All symbols, addresses, and string literals on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython `.so` modules; cp311/cp312 share the same layout). Addresses are `.text` offsets within each `.so`; treat every offset as version-pinned.*

## Abstract

`neuronx_cc` parses command-line options through **two structurally independent parsers** that share nothing but the stdlib `argparse` machinery they each subclass or wrap. The first is the **driver lane** — `InterceptingArgumentParser` in `neuronxcc/driver/Arguments.cpython-310-x86_64-linux-gnu.so` — which owns the *user-facing* `neuronx-cc` CLI (`neuronx-cc compile …`, `--optlevel`, `--arch`, the public/hidden/internal flag catalog). It is an `argparse.ArgumentParser` subclass that intercepts every `add_argument` call and mirrors each flag into a side registry tagged `(argument, kind, job, default)`. The second is the **backend lane** — the singleton `CommandLineParser` in `neuronxcc/starfish/penguin/Options.cpython-310-x86_64-linux-gnu.so` — which owns the *Penguin/tensorizer* backend's own flag set and is fed a flat option **string** (e.g. `--stats-file=penguin.stats --debug-pass=ValueNumbering`), splits it on whitespace, and parses it with its own `argparse.ArgumentParser`.

The two lanes never converge into a unified `Options` object. There is no shared defaults dataclass, no `to_argv()` serialization that one feeds to the other, and no common registry. The driver lane resolves to an `argparse.Namespace` mirrored into `_ArgumentRegistry.arguments_by_context`; the backend lane resolves to its *own* `argparse.Namespace` produced from a passed-in string. Each flag's default lives where it is registered — `default=` on the driver's `add_argument`, or the `default` argument passed to `clOpt*`/`addBoolOption` in the backend. This page traces both registration paths, shows the registry record shape and the `clOpt` template, and makes the "no unified Options object" point structurally: two disjoint registries, two `parse_args` calls, no bridge.

A reimplementer who assumes a single global options table will get this wrong twice: they will look for backend flags (`--debug-pass`, `--stats-file`) in the driver's `--help` and not find them, and they will look for a config-file or response-file mechanism that **does not exist** in any of the three relevant binaries.

For reimplementation, the contract is:

- **The driver lane** — `InterceptingArgumentParser` (an `argparse.ArgumentParser` subclass), the `_AddArgumentCallInterceptor` group proxy, and the `_ArgumentRegistry` with its 4-field record `(argument, kind, job, default)` keyed by context, plus `ArgKind` (whose `.rodata` member set is `{PUBLIC, HIDDEN, INTERNAL, EARG, Harg}` — the three fully-wired tiers plus the experimental `EARG`/help-suppressed `Harg`; see [3.9](flag-visibility-argkind.md)).
- **The backend lane** — the `CommandLineParser` singleton (lazy `getOrCreateInstance`), the `CLOption` wrapper, the `clOpt{String,Integer,Float,Bool}` factories and `addBoolOption`'s `no-`/`trim_prefix` negation, and `parseOptions`/`parseKnownOptions` over a whitespace-split string.
- **Why there is no unified `Options` object** — the two parsers have disjoint storage, disjoint resolution, and no serialization path between them; the only thing they share is the `argparse` base + the `--no-`/negation idiom, which each implements independently.

| | |
|---|---|
| **Driver-lane module** | `neuronxcc/driver/Arguments…so` (823,192 B) |
| **Driver-lane parser** | `InterceptingArgumentParser(argparse.ArgumentParser)` |
| **Driver registry** | `_ArgumentRegistry.arguments_by_context` (`OrderedDict`); record = `argument,kind,job,default,` |
| **Driver flag kinds** | `ArgKind.{PUBLIC, HIDDEN, INTERNAL, EARG, Harg}` (3 fully-wired + EARG/Harg; see [3.9](flag-visibility-argkind.md)) |
| **Backend-lane module** | `neuronxcc/starfish/penguin/Options…so` (554,136 B) |
| **Backend-lane parser** | `CommandLineParser` (singleton; wraps `argparse.ArgumentParser(conflict_handler='resolve')`) |
| **Backend option model** | `CLOption` + `clOpt{String,Integer,Float,Bool}` / `addOption` / `addBoolOption` |
| **Backend input** | a flat option **string** → `str.split()` → `cl_parser.parse[_known]_args` |
| **Shared state** | none — two disjoint registries, two `Namespace` objects, no `to_argv` bridge |
| **Config / response-file / env** | **absent** in both modules (no `fromfile_prefix_chars`, no `'@'`, no `configparser`, no `getenv`) |

---

## Lane A — the driver parser: `InterceptingArgumentParser`

### Purpose

The driver lane is the parser a user actually invokes through the `neuronx-cc` executable. It is an `argparse.ArgumentParser` subclass whose `add_argument` family is *intercepted* so that, in addition to the normal argparse registration, every flag is recorded into a side registry. The recorded metadata — a kind classification (`PUBLIC`/`HIDDEN`/`INTERNAL`), the owning **job**, and the **default** — lets the driver (a) emit ordinary `--help` while hiding suppressed flags, (b) emit the hidden/internal help listings on demand, and (c) associate each flag with the pipeline Job (cf. [3.5](subtool-argv.md)) that consumes it. The intercept is the whole point of subclassing rather than using a bare `ArgumentParser`.

### Function Map

All symbols are local (`l F .text`) in `Arguments…so` — present, not stripped. Addresses are `.text` offsets; the wide `__pyx_pw_9neuronxcc_6driver_9Arguments_…` prefix is elided.

| Class / function | Address | Role | Confidence |
|---|---|---|---|
| `InterceptingArgumentParser.__init__` | `0x146a0` | subclass ctor; builds the registry | CERTAIN |
| `…add_argument` | `0x11320` | intercepted; records into registry then delegates | CERTAIN |
| `…add_argument_group` | `0x15340` | returns an `_AddArgumentCallInterceptor` proxy | CERTAIN |
| `…add_mutually_exclusive_group` | `0x12c40` | returns a proxy (group flags intercepted too) | CERTAIN |
| `…set_context` | `0xedd0` | threads the current context label to the registry | CERTAIN |
| `…get_registry` | `0x16be0` | accessor for the `_ArgumentRegistry` | CERTAIN |
| `…parse_args` | `0x1e370` | override; surfaces `unrecognized_args` explicitly | CERTAIN |
| `…_parse_optional` | `0x1ade0` | override of argparse option-token matching | CERTAIN |
| `…_check_value` | `0x1f610` | override; keeps the `invalid choice` validator | CERTAIN |
| `…print_help_hidden` | `0x1cc90` | prints `HIDDEN` flags from the registry | CERTAIN |
| `…print_help_hidden_list` | `0x17150` | prints the `HIDDEN`/`INTERNAL` listing | CERTAIN |
| `_ArgumentRegistry.__init__` | `0xe160` | `arguments_by_context = OrderedDict()` | CERTAIN |
| `_ArgumentRegistry.storeArgument` | `0x103d0` | appends a 4-field record under a context | CERTAIN |
| `_ArgumentRegistry.get_args_for_context` | `0xfb50` | returns the record list for a context | CERTAIN |
| `_AddArgumentCallInterceptor.__init__` | `0x16110` | wraps an argparse parser/group | CERTAIN |
| `_AddArgumentCallInterceptor.__getattr__` | `0xf440` | delegates unknown attrs to the wrapped object | CERTAIN |
| `_AddArgumentCallInterceptor.add_argument` | `0x19210` | the interception core (size `0x1bcf`) | CERTAIN |
| `_AddArgumentCallInterceptor.add_argument_group` | `0x11fb0` | nested-group proxy | CERTAIN |
| `_AddArgumentCallInterceptor.add_mutually_exclusive_group` | `0x13a10` | nested mutex-group proxy | CERTAIN |
| `_AddArgumentCallInterceptor.set_context` | `0xe760` | context-thread for group-added flags | CERTAIN |

`ArgKind` is an `enum.Enum` with **five** members: `{PUBLIC, HIDDEN, INTERNAL, EARG, Harg}`. The three fully-wired ones — `PUBLIC`, `HIDDEN`, `INTERNAL` — appear as interned *name* constants (`__pyx_n_s_*`, plus `__pyx_k_INTERNAL`, `__pyx_n_s_ArgKind`), and they are the only three this lane references as live enum identifiers. The remaining two sit in the `.rodata` pool in weaker forms: `EARG`, the experimental tier (help-suppressed, listed by `--help-hidden`), interned as the unicode *value* `__pyx_n_u_EARG`; and `Harg`, also help-suppressed, present only as a bare string. [3.9 Flag Visibility Taxonomy](flag-visibility-argkind.md) is authoritative on the full five-member enum.

> **GOTCHA —** enumerating `ArgKind` from this lane's live references yields three members and looks complete. It is not — two more tiers exist and are only visible in the string pool.

### Algorithm

The intercepting `add_argument` peels the two custom kwargs (`kind`, `job`), delegates to argparse to do the real registration, reads back the resolved default, and mirrors the record into the registry under the current context.

```c
function InterceptingArgumentParser.add_argument(self, *args, **kwargs):   // 0x11320
    kind = kwargs.pop("kind", ArgKind.PUBLIC)   // interned 'kind' / 'argkind'; default kind PUBLIC  [STRONG]
    job  = kwargs.pop("job",  None)             // interned 'job' — owning pipeline Job  [STRONG]
    action = super().add_argument(*args, **kwargs)   // real argparse registration  [CONFIRMED delegate]
    default = action.default                    // read back; 'get_default' present in pool  [CONFIRMED]
    self._registry.storeArgument(               // mirror into the side registry
        self._context, action, kind, job, default)
    return action
```

```c
function _ArgumentRegistry.storeArgument(self, context, argument, kind, job, default):  // 0x103d0
    // record fields are exactly the .rodata literal "argument,kind,job,default,"  [CONFIRMED]
    record = (argument, kind, job, default)
    self.arguments_by_context.setdefault(context, []).append(record)  // 'setdefault' interned  [CONFIRMED]
```

```c
function _ArgumentRegistry.get_args_for_context(self, context):  // 0xfb50
    return self.arguments_by_context.get(context, [])            // list of (argument,kind,job,default)
```

> **NOTE — the "defaults map" is distributed, not monolithic.** A reimplementer expecting one big defaults dataclass will not find it. Each flag's default *is* the argparse-registered `default=`, read back via `action.default`/`get_default` and mirrored into `arguments_by_context` alongside its `kind` and owning `job`. The resolved state is the ordinary argparse `Namespace`; the registry is a parallel introspection index, not a second source of truth.

### The group proxy: `_AddArgumentCallInterceptor`

argparse has a structural hole: when you call `parser.add_argument_group()` you get back a plain `_ArgumentGroup` whose `add_argument` goes **straight to argparse**, bypassing any parser-level override. To keep group-added flags inside the intercept, `InterceptingArgumentParser.add_argument_group` (`0x15340`) and `…add_mutually_exclusive_group` (`0x12c40`) return an `_AddArgumentCallInterceptor` (`0x16110`) wrapping the real group instead of the group itself.

```c
function _AddArgumentCallInterceptor.add_argument(self, *args, **kwargs):  // 0x19210 (size 0x1bcf)
    kind = kwargs.pop("kind", ArgKind.PUBLIC)
    job  = kwargs.pop("job",  None)
    action = self._wrapped.add_argument(*args, **kwargs)   // delegate to the real argparse group
    self._registry.storeArgument(self._context, action, kind, job, default=action.default)
    return action

function _AddArgumentCallInterceptor.__getattr__(self, name):  // 0xf440
    return getattr(self._wrapped, name)   // transparent proxy for everything not overridden
```

`set_context` (`0xe760` on the proxy, `0xedd0` on the parser) threads the current context label downward so that flags added to a group are stored under the correct context key. The proxy's `add_argument_group`/`add_mutually_exclusive_group` (`0x11fb0`/`0x13a10`) re-wrap nested groups so the intercept survives arbitrary group nesting.

> **GOTCHA — bare `add_argument_group()` would silently escape the registry.** The reason the driver needs a *proxy object* rather than just overriding `add_argument` is precisely that argparse groups bypass the parser. A reimplementation that overrides `add_argument` but returns argparse's native group from `add_argument_group` will register grouped flags with argparse but **never** record them in `arguments_by_context` — so they vanish from the hidden-help listings and lose their `job` association. The `_AddArgumentCallInterceptor` exists to close exactly this gap.

### Resolution overrides and the hidden-help path

The driver overrides three argparse internals and adds two help emitters:

- **`parse_args` (`0x1e370`)** wraps argparse's `parse_args` but handles leftover arguments through its own path — the literals `unrecognized_args` and `not unrecognized_args` are both present, indicating an explicit success/failure branch rather than argparse's default `error()`-and-exit.
- **`_parse_optional` (`0x1ade0`)** overrides how a token is matched to a registered flag (`startswith`-based prefix logic).
- **`_check_value` (`0x1f610`, size `0x28a5`)** overrides the choices validator while keeping the canonical message `invalid choice: %(value)s (choose from %(choices)s)` — this is the one place `choices` is validated.

Hidden and internal flags are registered with `help=argparse.SUPPRESS` (the interned `SUPPRESS` is in the pool) so they never show in ordinary `--help`, yet remain recoverable via `print_help_hidden` (`0x1cc90`) and `print_help_hidden_list` (`0x17150`). Both walk the registry and emit rows in the format string:

```text
%-15s %-8s --%s: %s          // group/dest | kind | flag | help   [CONFIRMED literal]
```

This is how `ArgKind` pays off: a `HIDDEN`/`INTERNAL`-tagged flag is suppressed from public help but listed by the hidden emitters, which read `kind` straight out of the 4-field record.

---

## Lane B — the backend parser: `CommandLineParser` singleton

### Purpose

The backend lane is the Penguin/tensorizer middle-end's *own* option system. It is **not** the top-level CLI. Its job is to parse a single flat option **string** — the kind of value a `--tensorizer-options`-style flag or a `NEURON_CC_FLAGS`-derived backend string carries — into an argparse `Namespace` the Penguin passes consult. The class docstring states the role verbatim: `A parser for command line options for Penguin`. It is a deliberate singleton, with the warning baked into the docstring: `Warning: CommandLineParser is a singleton class. It should not be used in tests.`

### Function Map

All symbols local (`l F .text`) in `Options…so`; `__pyx_pw_9neuronxcc_8starfish_7penguin_7Options_…` prefix elided.

| Class / function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `CommandLineParser.__init__` | `0x11ab0` | `0xbcf` | builds `argparse.ArgumentParser(conflict_handler='resolve')` | CERTAIN |
| `CommandLineParser.getOrCreateInstance` | `0x114a0` | `0x610` | lazy singleton accessor | CERTAIN |
| `CommandLineParser.addOption` | `0x12680` | `0x1861` | `cl_parser.add_argument(*names,**kw)` → wrap as `CLOption` | CERTAIN |
| `CommandLineParser.addBoolOption` | `0x14a30` | `0x23d3` | bool flag + `no-`/`trim_prefix` negation pairing | CERTAIN |
| `CommandLineParser.parseOptions` | `0xbf40` | `0xe15` | `options_str.split()` → `parse_args` | CERTAIN |
| `CommandLineParser.parseKnownOptions` | `0xadd0` | `0x116f` | → `parse_known_args` (tolerates unknown flags) | CERTAIN |
| `CLOption.__init__` | `0xcd60` | `0xd3f` | stores name, default, type, the argparse action | CERTAIN |
| `CLOption.value` | `0x13ef0` | `0xb38` | property; coerces by `type`, falls back to `default` | CERTAIN |
| `clOptString` | `0xe8f0` | `0xe43` | module factory; `type=str` | CERTAIN |
| `clOptInteger` | `0xf740` | `0xe43` | module factory; `type=int` | CERTAIN |
| `clOptFloat` | `0xdaa0` | `0xe43` | module factory; `type=float` | CERTAIN |
| `clOptBool` | `0x10590` | `0xf09` | module factory; bool (`store_true`/`store_false`) | CERTAIN |

> **QUIRK — three factories are byte-for-byte the same size.** `clOptString`, `clOptInteger`, and `clOptFloat` are each exactly `0xe43` bytes. That is not a coincidence: they are one Cython template instantiated with a different coercion `type` (`str`/`int`/`float`). `clOptBool` is larger (`0xf09`) because the bool path also wires `store_true`/`store_false` and the negation handling. So the four-way factory split is *three identical bodies + one bool variant*, not four hand-written functions.

### The singleton

The instance is held in the name-mangled private attribute `_CommandLineParser__instance` (interned `__pyx_k_CommandLineParser__instance`). `getOrCreateInstance` is the classic lazy-init guard:

```c
function CommandLineParser.getOrCreateInstance(cls):   // 0x114a0
    if cls._CommandLineParser__instance is None:       // mangled __instance  [CONFIRMED]
        cls._CommandLineParser__instance = CommandLineParser()
        return cls._CommandLineParser__instance
    // re-entry guard string present in .rodata:
    raise / return  "CommandLineParser is already created!"   // [CONFIRMED literal; STRONG branch]
```

```c
function CommandLineParser.__init__(self):   // 0x11ab0
    self.cl_parser = argparse.ArgumentParser(conflict_handler='resolve')  // 'resolve' interned  [CONFIRMED]
    self.compatible_mode = ...   // bool; help literal "Compatible mode for the old scheduler"  [CONFIRMED]
```

> **NOTE — `conflict_handler='resolve'` lets the backend re-register flags.** Penguin passes register their own flags into the one shared parser. With `resolve`, a later duplicate `--flag` silently overrides the earlier one instead of raising `ArgumentError`. This is a deliberate consequence of the singleton design: many passes touch the same parser, and `resolve` keeps that from being a hard error.

### Option registration and the bool negation

`addOption` forwards to `cl_parser.add_argument` and wraps the resulting action in a `CLOption`; the default, if any, is whatever the caller passed as `kwargs['default']`. `addBoolOption` is the largest function in the module (`0x23d3`) because it owns the negative-flag pairing. Its docstring (reconstructed from the string pool) is explicit about the two registration modes:

```text
Add a boolean tensorizer option
:param name: the name of the option
:param default: the default value
:param trim_prefix: if True, then the option name is trimmed by the first '-';
  if False (by default), it will pad 'no-' to the beginning of the flag
e.g. if name is 'disable-vectorize-dge-dma', then the trimmed name is
  'vectorize-dge-dma'
:return: a CLOption object
```

```c
function CommandLineParser.addBoolOption(self, name, default, trim_prefix=False):  // 0x14a30
    if trim_prefix:
        trimmed_name = name.split("-", 1)[1]   // 'disable-X' -> 'X'  [STRONG: local trimmed_name + split]
        opt = self.cl_parser.add_argument("--" + trimmed_name,
                                          action="store_true", default=default)
    else:
        // register --name (store_true) AND --no-<name> (store_false) as a pair
        opt = self.cl_parser.add_argument("--" + name, action="store_true",  default=default)
        neg = self.cl_parser.add_argument("--no-" + name, action="store_false", dest=opt.dest)
        // '--no-' literal present; add_mutually_exclusive_group in the pool pairs positive/negative  [STRONG]
    return CLOption(opt, default=default)
```

The `clOpt*` factories are thin module-level wrappers that build a `CLOption` with the right coercion type and register it through the singleton parser. Type coercion (`int`/`float`/`str`) is applied in `CLOption.value` (`0x13ef0`), which returns the parsed value or falls back to `default` when the flag was never seen — `store_true`/`store_false` both appear in `value`'s string window, so bool options resolve there too.

### Parsing the flat string

The backend never sees `sys.argv` directly. It receives a flat string and splits it itself:

```c
function CommandLineParser.parseOptions(self, options_str):   // 0xbf40
    tokens = options_str.split()                  // plain str.split — NOT shlex  [CONFIRMED]
    return self.cl_parser.parse_args(tokens)

function CommandLineParser.parseKnownOptions(self, options_str):   // 0xadd0
    tokens = options_str.split()
    namespace, unknown = self.cl_parser.parse_known_args(tokens)   // tolerates unknown flags  [CONFIRMED]
    return namespace, unknown
```

The docstring's own example pins the input shape: `option_str = '--stats-file=penguin.stats --debug-pass=ValueNumbering'`.

> **GOTCHA — whitespace split, not shell split.** `parseOptions` uses `str.split()`, and there is **no** `shlex` string anywhere in `Options…so`. A backend option value containing spaces (e.g. a quoted path) will be torn into separate tokens — the parser has no concept of shell quoting. A reimplementation that reaches for `shlex.split` to "be safe" would diverge from the binary's behavior.

---

## Why there is no unified `Options` object

The two lanes are independent by construction. The structural evidence — not just an assertion — is that they have **disjoint storage, disjoint resolution, and no serialization bridge**:

| Axis | Driver lane (`Arguments…so`) | Backend lane (`Options…so`) |
|---|---|---|
| Parser type | `InterceptingArgumentParser(argparse.ArgumentParser)` | `CommandLineParser` wrapping a private `argparse.ArgumentParser` |
| Input | `sys.argv` (the user CLI) | a flat option **string** (`parseOptions(str)`) |
| Token split | argparse's own argv handling | `str.split()` (whitespace) |
| Registration API | `add_argument(..., kind=, job=)` | `clOpt{String,Integer,Float,Bool}` / `addOption` / `addBoolOption` |
| Option wrapper | argparse `Action` + 4-field registry record | `CLOption` |
| Side registry | `_ArgumentRegistry.arguments_by_context` (`OrderedDict`) | none — defaults are per-`CLOption` |
| Default source | argparse `default=`, read back via `action.default` | the `default` arg passed at `clOpt*`/`addBoolOption` |
| Resolved state | an `argparse.Namespace` (+ the registry mirror) | a *separate* `argparse.Namespace` |
| Negation idiom | `Actions.NoableArgumentAction` / `EnableDisable…` (`--no`/`--enable`/`--disable`) | `addBoolOption` `--no-<name>` / `trim_prefix` |
| Lifecycle | per-parser instance, built by `CommandDriver.__init__` | process-wide singleton (`getOrCreateInstance`) |

There is no function in either binary that reads one lane's `Namespace` or registry and feeds it to the other. The driver does **not** call `parseOptions`; the backend's `CommandLineParser` is reached only from within Penguin passes, fed whatever flat string the backend assembles. The only thing the two share is the `argparse` base class and the *idiom* of a `--no-` negated boolean — and each implements that idiom independently (the driver via the custom `Actions` library, the backend via `addBoolOption`).

> **QUIRK — the negation pattern is duplicated, not shared.** Both lanes pair a positive flag with a `--no-`/`--disable` negative, but neither calls the other's code. The driver's negation lives in `neuronxcc/driver/Actions…so` (`NoableArgumentAction`, `EnableDisableArgumentAction`); the backend's lives inline in `addBoolOption`. Seeing the same `--no-` idiom on both sides is *not* evidence of a shared implementation — it is parallel evolution within one codebase.

The practical upshot for a reimplementer: **flag ownership is partitioned, not unified.** A flag like `--optlevel`, `--arch`, or `--fork-subcommand` is owned by the driver lane and appears in `neuronx-cc --help`. A flag like `--stats-file`, `--debug-pass`, or a `disable-vectorize-dge-dma`-style backend bool is owned by the Penguin lane and is invisible to the driver's help — it only takes effect when it is inside the backend option *string* the driver hands down. Reproduce two parsers, not one. See [3.8 (flag catalog)](flag-catalog.md) for which concrete flag lives in which lane, and [3.10 (opt-level planes)](opt-level-planes.md) for how `-O{0..3}` is a driver-lane value that *expands* into backend pass flags rather than being stored in `Options…so`.

---

## No config-file, no response-file, no environment read

A standing question for any argparse-based tool is whether options can also come from a config file, an `@response-file`, or an environment variable. For `neuronx_cc` the answer, verified by exhaustive string scan across `Arguments…so` and `Options…so` (and corroborated in `CommandDriver…so`), is **no on all three counts**:

| Mechanism | Driver lane | Backend lane | Evidence |
|---|---|---|---|
| `fromfile_prefix_chars` / `'@'` response-file | absent | absent | no `fromfile`/response-file string in either `.so` | 
| config-file reader (`.cfg`/`.ini`/`.json`/`configparser`) | absent | absent | the only `.ini` hits are ELF section names `.init`/`.init_array` |
| `getenv` / `os.environ` | absent | absent | zero `getenv`/`environ` strings in either `.so` |
| `shlex` (shell-quote split) | absent | absent | no `shlex` string in either `.so` |

```text
$ strings Arguments…so Options…so | rg -ic 'getenv|os\.environ'        -> 0
$ strings Arguments…so Options…so | rg -ic 'fromfile_prefix|response'  -> 0
$ strings Arguments…so | rg -i 'configparser|\.ini|\.cfg'              -> .init / .init_array  (ELF only)
$ strings Options…so   | rg -ic 'shlex'                                -> 0
```

> **NOTE — `NEURON_CC_FLAGS` is not read here.** The Neuron framework integration prepends `NEURON_CC_FLAGS` to `sys.argv` **outside** the wheel, so by the time either parser runs, those tokens are ordinary argv. Neither `Arguments…so` nor `Options…so` performs the environment read itself. A reimplementation that adds a `getenv("NEURON_CC_FLAGS")` inside the parser would be modeling the framework glue, not these two modules.

---

## Related Components

| Name | Relationship |
|---|---|
| `neuronxcc/driver/Actions…so` | the custom argparse `Action` library (`NoableArgumentAction`, `EnableDisableArgumentAction`, `str2bool`, `Help*Action`) the driver lane registers flags with; the driver-side counterpart to `addBoolOption` |
| `neuronxcc/driver/CommandDriver…so` | builds the top-level `InterceptingArgumentParser` in `CommandDriver.__init__`, runs `parse_known_args`, and dispatches to a `CommandInterface` subcommand |
| `neuronxcc/starfish/penguin/*` passes | the only callers of the `CommandLineParser` singleton; consume the backend `Namespace` |

## Cross-References

- [Flag Catalog](flag-catalog.md) — 3.8; which concrete flag is owned by which of the two lanes
- [Opt-Level Planes](opt-level-planes.md) — 3.10; how `-O{0..3}` is a driver-lane value that expands into backend pass flags
- [Sub-tool argv Construction](subtool-argv.md) — 3.5; how the driver `Namespace` becomes per-Job argv (no `to_argv` on a unified object)
- [The Compile Pipeline at a Glance](../front/pipeline.md) — where the driver `Namespace` enters `CompileCommand` and the Job pipeline
