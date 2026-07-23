# `build_custom_op.py` Codegen — the BUILD front-end

This page documents the **build front-end** of the GPSIMD custom-op toolchain: the
shipped Python driver `build_custom_op.py` that takes a user's compute kernel plus a
PyTorch schema and **code-generates** the C++ wrapper that splices the kernel into
the runtime's `customop_*` marshalling boilerplate and the **`switchBack`** stack-switch
launcher, then compiles once and links the result against a per-core linker spec —
**once** for single-core, **eight times** for the 8-way SPMD build.

Everything below is anchored to the **shipped artifact**
`script/build_custom_op.py` (395 lines, read verbatim) and the headers it targets.
All claims are tagged **`[HIGH/OBSERVED]`** (verbatim script/header/LSP bytes) or
**`[INFERRED]`** (ABI reasoning over those bytes). Line numbers are `build_custom_op.py:N`
unless another file is named. See also [Custom-Op Marshalling](customop-marshalling.md)
(the `customop_*` entry points this wraps), [Stack Switch](stack-switch.md)
(the `switchBack` / `switch_stack_or_call_wrapper` handoff), [Multicore SPMD](multicore-spmd.md)
(the 8-core model behind the 8× link), [LSP / ELF](lsp-elf.md) (the per-core linker
specs), and the end-to-end [Build Flow](build-flow.md).

## Artifacts

| Artifact | Path (under `…/opt/aws/neuron/gpsimd/`) | Role |
|---|---|---|
| Driver | `script/build_custom_op.py` | the codegen + build orchestrator |
| `wrapper_api.h` | `custom_op/neuron/wrapper_api.h` | declares `wrapper_fn`, the `customop_*` calls |
| `stack_switch.hpp` | `custom_op/neuron/stack_switch.hpp` | declares `switched_stack`, `switch_stack_or_call_wrapper`, `STACK_SIZE` |
| `custom_op.h` | `custom_op/neuron/custom_op.h` | device-side runtime APIs the *kernel* calls (`neuron_translate`/`neuron_hbm_allocate`/`neuron_memcpy`) |
| Runtime lib | `custom_op/neuron/libneuroncustomop.a` | defines `customop_*`/`switchBack`; **references** the generated `get_func_*` |
| Per-core LSPs | `custom_op/lsp_fll_load_cpus/lsp_fll_load_cpu{0..7,_single}/` | 9 linker specs (`ldscripts/elf32xtensa.x` + `specs`) |

> **NOTE.** The driver is a **standalone module** — no class, just module-level config
> plus free functions, public entry `compile()` (`build_custom_op.py:363`). A repo-wide
> sweep finds **zero importers** inside the package, so `compile()` is invoked by the
> *host-side* Neuron torch-extension build (out of this package), which supplies the
> kernel sources, the `fn_names` list, the schemas, and the `multicore` toggle.
> `[HIGH/OBSERVED: no in-package importer; INFERRED host caller.]`

## 1. The build pipeline — `compile()`

`compile()` (`build_custom_op.py:363-394`) is the public entry. Its signature
fixes the whole contract: `[HIGH/OBSERVED]`

```python
def compile(name, fn_names, src_files, schema, build_directory,
            includes=[], cflags=[], multicore=False, verbose=False):
```

| Param | Meaning |
|---|---|
| `name` | output library filename **with extension** (e.g. `"myop.so"`); `base_name` is the stem |
| `fn_names` | `list[str]` — the kernel compute-function names, one per op |
| `src_files` | `list[str]` — the user kernel `.cpp` source files |
| `schema` | `list[str]` PyTorch schemas, **one per `fn_name`**, form `"<ns>::<fn>(argtype argname, …) -> (rettype retname)"` |
| `build_directory` | where `.o` / `wrapper.cpp` / `.so` land |
| `includes`, `cflags` | extra `-I` dirs / extra compile flags (appended) |
| `multicore` | `False` → one `name.so` (`cpu_single` LSP); `True` → **eight** `name_cpuN.so` (per-core LSPs) — §6 |

The body is **five phases, in order** (`build_custom_op.py:380-394`): `[HIGH/OBSERVED]`

```python
    if not shutil.which(CC):                       # 381  tool sanity: xt-clang++ on PATH
        raise Exception(f'Could not find {CC}')
    objs = _compile_sources(src_files, …)          # 384  ① compile each USER kernel .cpp -> .o
    wrapper = _create_wrapper(schema, fn_names, …) # 385  ② CODEGEN wrapper.cpp  (this page §2-4)
    objs.append(_compile_wrapper(wrapper, …))      # 386  ③ compile the generated wrapper -> wrapper.o
    base_name = os.path.splitext(name)[0]          # 388
    libs = _link(base_name, objs, …, multicore)    # 389  ④ link 1x (single) or 8x (multicore)
    if '-g' not in cflags:                          # 391  ⑤ strip/pack each .so (gate below)
        libs = _strip(libs, verbose)               # 392
    return libs
```

| Phase | Helper (line) | What it does |
|---|---|---|
| ① compile sources | `_compile_sources` (281) → `_compile_single` (262) | compiles each user kernel `.cpp` **independently** to its own `.o` |
| ② codegen | `_create_wrapper` (202) → `_parse_schema` (53) + `_wrapper_gen` (113) | **emits `wrapper.cpp`** into `build_directory`; returns its path |
| ③ compile wrapper | `_compile_wrapper` (296) → `_compile_single` (262) | compiles `wrapper.cpp` → `wrapper.o`, **appended** to `objs` |
| ④ link | `_link` (329) → `_link_one` (306) | links the **whole `objs` set** once (single) or 8× (multicore) |
| ⑤ strip/pack | `_strip` (360) → `_strip_one` (339) | packs + strips each `.so`; **skipped iff user passed `-g` in `cflags`** |

> **GOTCHA — the codegen is a *separate* translation unit.** Phase ① compiles the
> user kernels **first and independently**; phase ② emits `wrapper.cpp` as its **own**
> TU compiled in phase ③. The user kernel and the wrapper **never share a TU** — they
> meet only at link (phase ④). The only thing wiring them together is the C++ forward
> declaration the codegen emits (§3a) and ordinary link-time symbol resolution.
> `[HIGH/OBSERVED: _compile_sources vs _compile_wrapper are distinct calls.]`

Version metadata is pinned at the module top (`build_custom_op.py:10-14`): `[HIGH/OBSERVED]`

```python
__version__ = '0.21.2.0'                       # matches the package version
versions = {
    'ulib_to_ucode_version': '1.21.1.0',       # == runtime lib's ucode-lib version
    'ulib_to_isa_version':   '1.0.2520.0'
}
```

`ulib_to_ucode_version='1.21.1.0'` equals the runtime lib's `SUNDA_UCODE_LIB_VERSION_STR`
in `parallel.o`'s `.data` — the codegen and the runtime lib **agree** on the ucode-lib
version. `[HIGH cross-check.]` These dict values are **defined but never re-referenced**
in the script; they are metadata the host build reads. `[HIGH/OBSERVED defined-never-used; INFERRED host reads them.]`

## 2. Schema parse → arg/return type lists — `_parse_schema`

`_parse_schema(schema)` (`build_custom_op.py:53-111`) reads the PyTorch schema **string**
and returns `(arg_types, ret_types)` — **types only**, names dropped. It is a hand-written
`find`/`split` scanner, **not a real grammar**. `[HIGH/OBSERVED]`

Byte-exact logic:

1. `sep = schema.find('::')` → `namespace = schema[:sep]` — **parsed but discarded** (line 67).
2. `paren = schema.find('(', sep)` → `name = schema[sep+2:paren]` — the fn name, **also discarded** (line 73). The codegen keys off the explicit `fn_names` list, *not* the name in the schema.
3. `cparen = schema.find(')', paren)` → `args = schema[paren+1:cparen]`.
4. **arg split** (line 83): `for arg in args.split(', ')` (comma-space), then `arg.split(' ')` (single space) → require exactly `[type, name]` else raise; keep `split_arg[0]` (the type).
5. Find the **return** paren pair after `cparen`, split the same way, keep `ret_types`.

> **GOTCHA — fragile string splitting.** `[HIGH/OBSERVED the split logic]`
> - Args are split on the **literal two-char string `', '`** (comma-space). A schema with no space after a comma, or extra spaces, mis-splits.
> - Each arg is split on a **single space** into **exactly** `[type, name]`. A type **containing a space** (e.g. `"long long"`) yields **3 tokens** and raises *"Found more than 2 entries for a single arg"* (line 86). So although `_wrapper_gen` (§3) **handles** an arg type `'long long'`, `_parse_schema` **cannot** produce it from a literal `"long long x"` schema token — the space-split blocks it. The `long long` arm is therefore **latent** at the parse layer. `[HIGH/OBSERVED the split; MED/INFERRED the consequence.]`
> - The namespace and fn-name extracted here are **unused**; the schema head is validated for *shape* only. `[HIGH/OBSERVED.]`

Parser raises (all fail-fast `raise Exception`): no `::` (l65), missing arg `(`/`)` (l71/77),
missing return `(`/`)` (l93/97), `>2` tokens per arg/return (l86/106). `[HIGH/OBSERVED.]`

## 3. The emitted wrapper — `_wrapper_gen` (per op)

`_wrapper_gen(funcname, args, ret)` (`build_custom_op.py:113-200`) emits, **for each op**,
three C++ constructs as a single string: a **forward declaration** of the user kernel, a
**`<fn>_wrapper()`** that drives the `customop_*` marshalling, and a **`<fn>_stack_switch()`**
launcher. `[HIGH/OBSERVED]`

### 3a. Signature → C++ forward declaration (the arg-type map)

`comb_args` is built from the parsed `arg_types` via this **exact** map (lines 119-146): `[HIGH/OBSERVED]`

| Schema arg | C++ param type emitted |
|---|---|
| `Tensor` | `const at::Tensor&` |
| `Scalar` **or** `number` | `const at::Scalar&` (comment: a `torch._C.Type` Scalar stringifies to `number`; both accepted) |
| `int` | `int` |
| `float` | `float` |
| `long long` | `long long` |
| *(anything else)* | `raise "unexpected arg type"` (l134) |

The **return** must be **exactly one `Tensor`**: `len(ret)!=1` → raise *"only a single return
value…"* (l140); `ret[0]!='Tensor'` → raise *"only Tensor return types…"* (l144);
`ret[0]=='Tensor'` → declared C++ return `at::Tensor`. The emitted forward decl is:

```cpp
at::Tensor <funcname>(<comb_args>);   // e.g. at::Tensor relu(const at::Tensor&);
```

This **is** the wrapper↔kernel ABI (§7): the user kernel sees PyTorch-level types
(`at::Tensor` / `at::Scalar` / plain `int`/`float`/`long long`), **never** the raw
`Q7PtrType` / TPB descriptors. `[HIGH/OBSERVED.]`

### 3b. The `<fn>_wrapper()` body — the `customop_*` call sequence

Emitted verbatim (lines 148-193); `<NEXT_CALLS>` selected per arg by the **same map**:

```cpp
void <funcname>_wrapper() {
    customop_setup(true);                                 // 152  hasReturn = HARDCODED true
    at::Tensor output = <funcname>( <NEXT_CALLS> );       // 160-182  call the user kernel
    customop_return_tensor(output);                       // 187  COPY result -> output HBM descriptor
    customop_cleanup();                                   // 191
    if (switched_stack)                                   // 192  if the runtime took the HBM-stack switch…
        asm("j switchBack");                              // 193  …hand control to switchBack instead of returning
}
```

where `<NEXT_CALLS>` is, **per arg in declared order** (lines 165-182), joined by `', '`:

| Schema arg | marshalling call emitted |
|---|---|
| `Tensor` | `customop_next_tensor()` |
| `Scalar` / `number` | `customop_next_scalar()` |
| `int` | `customop_next_int()` |
| `float` | `customop_next_float()` |
| `long long` | `customop_next_longlong()` |
| *(else)* | `raise "unexpected arg type"` (l177) |

So the generated `next_*` sequence is a **1:1, in-order image** of the user's parameter
list — arg #k of the kernel is fed by the k-th `customop_next_<type>()` call. This is
**the** signature→marshalling mapping. `[HIGH/OBSERVED.]`

The eight emitted symbols resolve **byte-exact** against `wrapper_api.o` in
`libneuroncustomop.a` (all defined `T`): `customop_setup(bool)`, `customop_next_tensor`,
`customop_next_scalar`, `customop_next_int`, `customop_next_longlong`, `customop_next_float`,
`customop_return_tensor(at::Tensor&)`, `customop_cleanup`. `[HIGH/OBSERVED nm.]` See
[Custom-Op Marshalling](customop-marshalling.md) for what each does — the `UTensor → Q7PtrType
→ NeuronStorageImpl → NeuronTensorImpl` chain that turns the on-wire TPB descriptors into
`at::Tensor`. The decls live in `wrapper_api.h:9-24`. `[HIGH/OBSERVED header.]`

> **NOTE — `customop_setup(true)` is always `true`.** Because the return is **forced** to a
> single `Tensor` (§3a), the `hasReturn` bool is the literal `true` from this codegen — the
> op **always** returns a Tensor. `[HIGH/OBSERVED literal 'true'.]`

> **NOTE — copy path only.** The codegen emits `customop_return_tensor(output)` (the
> **copy** write-back). It does **not** emit `get_dst_tensor()` — the in-place output path is
> a manual API the kernel may call itself, never auto-emitted. `[HIGH/OBSERVED: no `get_dst_tensor` in the template.]`

> **CORRECTION — stale docstring vs real emitter.** The docstring *example* inside
> `_create_wrapper` (lines 208-214) shows `int relu_wrapper() { … return 0; }`. The **actual
> emitter** (line 149) produces **`void <fn>_wrapper()`** ending in the `if (switched_stack)
> asm("j switchBack")` tail, **not** an `int`-returning `return 0`. The docstring is a stale
> sketch; trust the emitter. ABI-08 §3b flags the `void` return type but does not call out
> the docstring example specifically — recorded here. `[HIGH/OBSERVED: emitter line 149 vs docstring lines 208-214.]`

### 3c. The `switchBack` / launcher emission

Two pieces, **both** emitted by this codegen, closing the two halves of the stack-switch handoff:

**(i) The `switchBack` tail inside `<fn>_wrapper()`** (lines 192-193): `if (switched_stack) asm("j switchBack");`.
After the op finishes (`return_tensor` + `cleanup`), if the runtime took the HBM-stack switch
(`switched_stack == true`, the global set by `switch_stack_or_call_wrapper`), the wrapper does an
**unconditional `j switchBack`** instead of a normal return — handing control to
`switch_stack.o:switchBack` (the WINDOWBASE-rotate + SP/SR restore + jx-back routine). If the
switch was **not** taken, control falls off the end of `<fn>_wrapper` as a normal windowed
`retw` on the on-core stack. `switched_stack` is `extern bool` (`stack_switch.hpp:4`);
`switchBack` is a `T`/global in `switch_stack.o` — both resolvable link targets. `[HIGH/OBSERVED emitter + nm.]`

**(ii) The `<fn>_stack_switch()` launcher** (lines 196-198) — the `int()`-typed function
**actually registered** (§4):

```cpp
int <funcname>_stack_switch() {
    return switch_stack_or_call_wrapper((uint32_t)<funcname>_wrapper);
}
```

It calls `switch_stack_or_call_wrapper` (`stack_switch.hpp:20`) passing the **address** of
`<fn>_wrapper` cast to `uint32_t` as `wrapper_fn_ptr`. The second arg `stack_size` is **omitted**,
so it defaults to **`STACK_SIZE = 4196`** (`stack_switch.hpp:16`) — the deliberately tiny ~4 KB
default that makes the no-switch path the norm. `[HIGH/OBSERVED emitter + header.]`

> **The launch chain end-to-end:** `register_funcs` (lib) → `get_func_ptr(i)` =
> `<fn>_stack_switch` → `switch_stack_or_call_wrapper(&<fn>_wrapper, 4196)` → *[maybe HBM-stack
> switch]* → `<fn>_wrapper` → `setup`/`next_*`/**kernel**/`return_tensor`/`cleanup` → *(if switched)*
> `j switchBack`. Both halves of the handoff — the **call into** `switch_stack_or_call_wrapper`
> **and** the **return via** `switchBack` — are emitted here. See [Stack Switch](stack-switch.md). `[HIGH.]`

## 4. The registration trio + the `wrapper.cpp` skeleton — `_create_wrapper`

`_create_wrapper(schemas, funcs, build_directory)` (`build_custom_op.py:202-260`) assembles the
**whole** `wrapper.cpp`: a banner, one `_wrapper_gen` block per op, then **three** registration
functions, then writes `wrapper.cpp` to `build_directory`. `[HIGH/OBSERVED]`

```cpp
// WARNING: AUTO GENERATED DO NOT EDIT          // line 217
#include "neuron/wrapper_api.h"                 // line 218 — pulls in torch/torch.h + stack_switch.hpp

/* …one _wrapper_gen() block per op (§3): fwd-decl + _wrapper() + _stack_switch()… */

int get_func_cnt() { return <N>; }                          // 237-238  N = len(funcs)

char const* get_func_name(int idx) {                        // 241-245
    switch (idx) {
        case 0: return "<f0>";  case 1: return "<f1>";  …
        default: return "error";
    }
}

wrapper_fn get_func_ptr(int idx) {                          // 248-252
    switch (idx) {
        case 0: return <f0>_stack_switch;  case 1: return <f1>_stack_switch;  …
        default: return 0;
    }
}
```

`#include "neuron/wrapper_api.h"` transitively brings `torch/torch.h` + `stack_switch.hpp` into
scope (`wrapper_api.h:3-4`), so `at::Tensor`, `at::Scalar`, the `customop_*`, `switched_stack`,
`switch_stack_or_call_wrapper`, and `wrapper_fn` (`typedef int(*wrapper_fn)();`, `wrapper_api.h:6`)
are all declared for the emitted code. `[HIGH/OBSERVED header includes.]`

> **GOTCHA — 32-char op-name cap.** Per op, `if len(funcname) >= 32: raise "CustomOp function
> name is too long … Must be less than 32 characters."` (lines 221-223). `[HIGH/OBSERVED.]`

> **NOTE — `get_func_ptr` returns the launcher, not the wrapper.** It returns
> `<fn>_stack_switch` (the `int()` one), **not** `<fn>_wrapper` (the `void` one) — consistent
> with `wrapper_fn = int(*)()` (`wrapper_api.h:6`). `[HIGH/OBSERVED.]`

**The library↔codegen linkage.** The three symbols `get_func_cnt`/`get_func_name`/`get_func_ptr`
are **undefined references** in the shipped `libneuroncustomop.a`, resolved **only** by the
generated `wrapper.cpp`. `nm` over the archive shows
`start_exit.o: U _Z12get_func_cntv  U _Z12get_func_ptri  U _Z13get_func_namei`. The caller is
`register_funcs` (`start_exit.o`), which calls `get_func_cnt()` for `N`, then loops `get_func_name(i)`
+ `get_func_ptr(i)` and invokes the loader's registration callback `SUNDA_UCODE_LIB_STATUS(void*
handle, const char* name, int(*fn)())` per op. So the **generated `wrapper.cpp` is the
table-of-contents** the library's `register_funcs` reads to expose the customer's ops to the
runtime. `[HIGH/OBSERVED: the 3 `U` relocs + register_funcs disasm + demangled callback.]`

> **NOTE — why `-fno-jump-tables` matters here.** The two `switch()` functions compile to if-else
> chains, not relocatable jump tables — consistent with the loadable-lib model and with
> `register_funcs` being a plain linear loop. `[MED/INFERRED tie between the flag and the emitted switch().]`

## 5. The toolchain invocation — compile / link / strip commands

### 5a. The `XTENSA_CORE` selection (the default Vision-Q7 core)

```python
XTENSA_CORE = os.environ.get('Q7_CORE', 'ncore2gp')      # build_custom_op.py:25
```

The **default core is `ncore2gp`** — the Vision-Q7 GPSIMD Xtensa configuration. It is the
**same core the runtime images target**, and the same `--xtensa-core` value flows into every
compile/link/strip command below. The override path is the `Q7_CORE` environment variable.
`[HIGH/OBSERVED.]` Two sibling env knobs (lines 24, 26): `[HIGH/OBSERVED]`

```python
NEURON_ROOT   = os.environ.get('NEURON_CUSTOM_OP', '/opt/aws/neuron/gpsimd/custom_op')      # 24
XTENSA_SYSTEM = os.environ.get('Q7_TOOLS', f'/opt/aws/neuron/gpsimd/tools/{XTENSA_CORE}/config')  # 26
```

`XTENSA_SYSTEM` interpolates `XTENSA_CORE` into the config path, so overriding `Q7_CORE` also
relocates the config dir (unless `Q7_TOOLS` is set explicitly). The tools (`CC='xt-clang++'`,
`LLIB='xt-pkg-loadlib'`, lines 36-37) and a FlexLM license fallback (line 20) are also fixed at
module top.

### 5b. Compile — `CC_OPT` (`build_custom_op.py:42`)

Used for **both** user sources and the wrapper (`_compile_single`, line 269): `[HIGH/OBSERVED verbatim]`

```text
xt-clang++ -g -std=c++14 -stdlib=libc++-e -fno-jump-tables -Os -Wall -Werror
           -Wno-c99-designator -mcoproc -MMD -MP -fpic -mlongcalls -c
           --xtensa-core=ncore2gp --xtensa-system=<sys> <cflags> <INC> <includes> -o <name>.o <src>
```

Notable flags: `-Os` (size), `-fpic` (PIC for the loadable `.so`), `-mlongcalls` (far calls — the
lib spans windows), `-mcoproc` (the Q7 vector coprocessor), `-std=c++14`, `-stdlib=libc++-e` (the
embedded `libc++` "-e" variant), `-fno-jump-tables`, `-Werror`. `INC` is
`-I{ROOT} -I{ROOT}/torch/include -I{ROOT}/c10/include` (line 41). `[HIGH/OBSERVED.]`

### 5c. Link — `LINK_OPT` + per-variant LIBS (`_link_one`, line 314)

```text
xt-clang++ -g -std=c++14 -stdlib=libc++-e -fno-jump-tables -Os -Wall -Werror
           -Wno-c99-designator -mcoproc --xtensa-core=ncore2gp --xtensa-system=<sys>
           <INC> -o <lib> <all .o incl. wrapper.o> <LINKING_LIBS>
```

`LINKING_LIBS` (the **only** thing that differs across the 8/1 outputs — lines 45, 50): `[HIGH/OBSERVED]`

```text
{ROOT}/neuron/libneuroncustomop.a  {ROOT}/c10/lib/libc10.a  -lloader
  -mlsp={ROOT}/lsp_fll_load_cpus/lsp_fll_load_cpu<i|_single>
  -lxmem -lhal -lc++-e -lm -lgcc  {ROOT}/neuron/libcweak.a
```

i.e. the runtime custom-op static lib + `libc10` + the loader (`-lloader`, the "fll load" loader)
+ `xmem`/`hal` + `libc++-e`/`libm`/`libgcc` + a weak-symbol shim (`libcweak.a`). The **per-core
`-mlsp`** selects the linker spec (§6 + [LSP/ELF](lsp-elf.md)). `libc10.a`,
`libcweak.a`, `libneuroncustomop.a` are all confirmed present in the package; `-lloader`/`-lxmem`/`-lhal`
come from the toolchain. `[HIGH/OBSERVED verbatim + file existence.]`

### 5d. Strip / pack — `STRIP_OPT` (`_strip_one`, line 349; config string line 44)

```text
xt-pkg-loadlib -e lib_func --xtensa-core=ncore2gp --xtensa-system=<sys>
               -o <base>.packed.so -s <base>.stripped.so <lib>
```

Produces **both** a packed `.so` and a stripped `.so` per linked `.so`; the entry symbol is
`-e lib_func`. `[HIGH/OBSERVED.]` `lib_func` is **not** a C++ symbol in the `.a` (a sweep over all
members finds no `lib_func`) — it is the loadlib packer's **entry-name argument**, almost certainly
aliasing the library entry (`start_exit.o` exports `_start`/`entry_func`, the
`SUNDA_UCODE_LIB_STATUS()`-returning C++ entry). `[HIGH/OBS that lib_func is undefined; MED/INFERRED it is the packer alias for the entry.]`

> **GOTCHA — the `-g` strip-skip gate is asymmetric.** `compile()` gates stripping on
> `if '-g' not in cflags:` (line 391) — it skips `_strip` when the **user** passed `-g`. **But
> `CC_OPT`/`LINK_OPT` already contain a hardcoded `-g`** (lines 42-43), so **every** object/`.so`
> carries DWARF v4 regardless. The gate inspects only the user-supplied `cflags` list. So: default
> build (no user `-g`) → `.so` **is** stripped/packed to `*.stripped.so`/`*.packed.so`, even though
> the pre-strip `.so` had full DWARF; user passes `cflags=['-g']` → strip is **skipped** and the
> unstripped `.so` is returned. (This is why the shipped runtime `libneuroncustomop.a` is itself
> "not stripped, DWARF v4".) `[HIGH/OBSERVED gate vs hardcoded -g; MED/INFERRED the returned-output consequence.]`

## 6. The 8× per-core build matrix — `_link` / `MULTI_Q7_LIBS`

`NUM_CPUS = 8` (line 47). `MULTI_Q7_LIBS` is a comprehension over `range(NUM_CPUS)` (lines 48-50):
**eight** `LINKING_LIBS` strings, identical **except** the `-mlsp=` path
(`lsp_fll_load_cpu0` … `lsp_fll_load_cpu7`). `LIBS_SINGLE` (line 45) is the **ninth** recipe
(`lsp_fll_load_cpu_single`). `[HIGH/OBSERVED.]`

```python
def _link(name, objs, build_directory, multicore, verbose=False):     # 329
    if multicore:
        # Note: Compiler assumes that multi-core library is in format xxx_cpuX.so
        return [_link_one(f'{name}_cpu{i}', objs, build_directory, lib_cur_cpu, …)   # 8 outputs
                for i, lib_cur_cpu in enumerate(MULTI_Q7_LIBS)]                       # 335
    else:
        return [_link_one(name, objs, build_directory, LIBS_SINGLE, …)]              # 1 output (337)
```

From the **same object set** (user kernel `.o`s + the **single** `wrapper.o`, each compiled
**once**), the multicore path links **eight** shared objects `<base>_cpu0.so … <base>_cpu7.so`,
each against a **different per-core LSP**. The `xxx_cpuX.so` naming is a **hard contract** the
downstream loader relies on (explicit code comment, line 334). The single path → one `<base>.so`
linked against `cpu_single`. `[HIGH/OBSERVED.]`

**Why eight** — the GPSIMD device runs the **8-core SPMD model** (see [Multicore SPMD](multicore-spmd.md)):
eight Vision-Q7 cores each run an identical copy of the op over a partition of the work. Each core
needs its **own linked ELF** addressing its **own** 2 MiB HBM-scratch sub-window, baked in at link
time by `-mlsp`. **What differs per core is the LSP's `sram0_0_seg` origin** (re-diffed byte-exact
from the shipped `ldscripts/elf32xtensa.x`): `[HIGH/OBSERVED]`

| LSP | `sram0_0_seg` org | len |
|---|---|---|
| `lsp_fll_load_cpu0` | `0x84000000` | `0x200000` (2 MiB) |
| `lsp_fll_load_cpu1` | `0x84200000` | `0x200000` |
| `lsp_fll_load_cpu2` … `cpu6` | `0x84000000 + i·0x200000` | `0x200000` (2 MiB stride) |
| `lsp_fll_load_cpu7` | `0x84E00000` | `0x200000` |
| `lsp_fll_load_cpu_single` | `0x84000000` | `0x2000000` (32 MiB) |

The origin formula `org = 0x84000000 + i·0x200000` is verified for the sampled cores; `ENTRY(_start)`
and IRAM are identical across all. So the **core ID is baked into the `.so` at link time** via
`-mlsp` (the link-time half of the core's dual identity); the run-time half is the **PRID** the
device reads. The codegen drives all of this purely by choosing **which LSP string** to pass to
`_link_one`. See [LSP / ELF](lsp-elf.md) for the full SRAM window map. `[HIGH/OBSERVED.]`

> **NOTE — who sets `multicore=True`.** The default is `multicore=False` (line 363); a custom op
> is built single-core unless the **host** caller explicitly requests the 8-way SPMD build.
> `[HIGH/OBSERVED default; INFERRED host toggles it for pool-cluster SPMD ops.]`

## 7. The wrapper↔kernel ABI — what the kernel author writes

The codegen's forward declaration (§3a) **is** the boundary the user kernel must honor: `[HIGH/OBSERVED]`

- The user writes a C++ free function `at::Tensor <funcname>(<args matching the schema arg map>)`,
  where each schema arg maps to `Tensor→const at::Tensor&`, `Scalar/number→const at::Scalar&`,
  `int→int`, `float→float`, `long long→long long`. The return **must** be `at::Tensor`
  (only single-Tensor returns supported).
- The kernel sees **PyTorch-level types** (`at::Tensor`/`at::Scalar`/POD scalars), **not** the raw
  `Q7PtrType` / TPB descriptors. The generated wrapper does **all** the marshalling: `customop_next_*`
  turn the on-wire TPB descriptors into `at::Tensor`/`at::Scalar`/scalars, hand them to the kernel,
  and `customop_return_tensor` copies the result back to the output HBM descriptor.
- Inside the kernel, the user accesses tensor data via the **accessor API** (`q7_data_ptr()` +
  `sizes()` + `strides()`), may call the **multicore API** (`get_cpu_id()` / `get_cpu_count()`,
  `neuron-utils.hpp`) to self-partition SPMD work, the **runtime device APIs** in `custom_op.h`
  (`neuron_translate` / `neuron_hbm_allocate` / `neuron_dataram_allocate` / `neuron_memcpy`), and
  `get_dst_tensor()` for in-place output. **None of these are auto-emitted** — they are runtime APIs
  the kernel calls directly. `[HIGH on the boundary def; cross-report on the accessor/multicore APIs.]`
- The kernel compiles in its **own** TU(s) (`src_files`), separate from `wrapper.cpp`; they meet
  only at link. `[HIGH/OBSERVED.]`

## 8. Validation / codegen error inventory

All Python-level validation is fail-fast `raise Exception`: `[HIGH/OBSERVED]`

| Class | Condition | Message (line) |
|---|---|---|
| schema shape | no `::` | *"Malformed schema…"* (65) |
| | missing arg `(`/`)` | *"Missing … arg paren…"* (71/77) |
| | missing return `(`/`)` | *"Missing return … paren…"* (93/97) |
| | arg/return `>2` tokens | *"Found more than 2 entries…"* (86/106) |
| type/return | arg type ∉ {`Tensor`,`Scalar`,`number`,`int`,`float`,`long long`} | *"unexpected arg type"* (134/177) |
| | `len(ret) != 1` | *"only a single return value…"* (140/154/158/185) |
| | `ret[0] != Tensor` | *"only Tensor return types…"* (144/162/189) |
| name | `len(funcname) >= 32` | *"function name is too long…"* (223) |
| IO/toolchain | source file missing | *"Cant find source file…"* (289) |
| | `xt-clang++` not on PATH | *"Could not find xt-clang++"* (382) |
| | compile/link/strip subprocess nonzero | *"Error compiling/linking/stripping…"* (277/325/356) |

The codegen enforces **schema well-formedness, the restricted type vocabulary, the single-Tensor-return
rule, the `customop_setup(true)` always-has-return invariant, and the 32-char name cap**. Everything
else (dtype mismatch, rank, arg-count, inline-shape) is enforced at **runtime** by the `customop_*`
entry points (see [Custom-Op Marshalling](customop-marshalling.md)), **not** at codegen time. `[HIGH.]`

## 9. Synthesis — the full build front-end in one flow

1. **Host** calls `compile(name, fn_names, src_files, schema, build_dir, multicore=?)`.
2. **Compile user kernels** (`_compile_sources`): each `.cpp` → `.o` (`xt-clang++ -Os -fpic -mlongcalls -mcoproc -std=c++14 …`). The kernel is `at::Tensor <fn>(args…)`.
3. **Codegen `wrapper.cpp`** (`_create_wrapper`): banner + `#include "neuron/wrapper_api.h"`; **per op** (`_parse_schema` → `_wrapper_gen`) emit the fwd-decl, `void <fn>_wrapper()` (`setup(true)` → `output = <fn>(next_*…)` → `return_tensor` → `cleanup` → `if(switched_stack) j switchBack`), and `int <fn>_stack_switch()` (`switch_stack_or_call_wrapper((uint32_t)<fn>_wrapper)`); then **once** `get_func_cnt`/`get_func_name`/`get_func_ptr`.
4. **Compile `wrapper.cpp`** → `wrapper.o` (appended to `objs`).
5. **Link** (`_link`): multicore → **8×** `<base>_cpuI.so` (`-mlsp=cpuI`, `sram0_0_seg = 0x84000000 + i·2 MiB`); single → `<base>.so` (`-mlsp=cpu_single`, 32 MiB) — against `libneuroncustomop.a` + `libc10.a` + `-lloader` + `xmem`/`hal` + `libc++-e`/`m`/`gcc` + `libcweak.a`.
6. **Strip/pack** (`_strip`, unless user `cflags` has `-g`): `xt-pkg-loadlib -e lib_func` → `<base>.packed.so` + `<base>.stripped.so`.
7. **Runtime**: the loader places `<base>_cpuI.so` on Q7 core *I*; the library's `register_funcs()` calls the generated `get_func_cnt`/`get_func_name`/`get_func_ptr` to register each op's `_stack_switch` launcher; on invocation `_stack_switch → switch_stack_or_call_wrapper` (maybe HBM-stack switch) → `_wrapper → setup/next_*/KERNEL/return/cleanup → (if switched) j switchBack`. Each core self-IDs by **PRID** (`get_cpu_id`) and works its 2 MiB window (SPMD).

## Confidence ledger

- **`[HIGH/OBSERVED]`** — the `compile()` 5-phase pipeline + signature; `_parse_schema` scanner (incl. the `', '`/single-space split fragilities and discarded namespace/fn-name); the `_wrapper_gen` arg-type map and `customop_*` call order, the forced single-Tensor return, `customop_setup(true)`, the `void <fn>_wrapper`; the `if(switched_stack) asm("j switchBack")` emitter + `<fn>_stack_switch` launcher (`STACK_SIZE=4196` default); the `get_func_cnt`/`get_func_name`/`get_func_ptr` emitter and its `U` references resolved by `register_funcs`; the exact `CC_OPT`/`LINK_OPT`/`STRIP_OPT` command strings + flags; `XTENSA_CORE = os.environ.get('Q7_CORE','ncore2gp')`; `NUM_CPUS=8`, the 8 `MULTI_Q7_LIBS`, the multicore `_link` → `<base>_cpuI.so` + the `xxx_cpuX.so` comment; the 8 LSP `sram0_0_seg` origins (`0x84000000 + i·2 MiB`, single 32 MiB); all validation raises; `ulib_to_ucode_version='1.21.1.0'` == runtime lib.
- **`[HIGH/SOURCE]`** — `wrapper_fn=int(*)()`; the `customop_*` / `switch_stack_or_call_wrapper` / `switched_stack` / `switchBack` contracts (`wrapper_api.h` / `stack_switch.hpp`).
- **`[MED]`** — `long long` supported by `_wrapper_gen` but blocked by `_parse_schema`'s space-split (latent); `lib_func` (strip `-e` entry) = packer alias for the library entry; the `-g` strip-skip gate inspecting only user `cflags`; the `-fno-jump-tables` ↔ switch()-as-if-else tie.
- **`[INFERRED]`** — the host build supplies `fn_names`/`schema`/`multicore` and reads the versions dict; `multicore=True` is toggled by the host for pool-cluster SPMD ops.
- **`[GAPS]`** — the built `_cpuI.so` are produced on the customer machine (not in the package); this page decodes the **emitter** + recipes and verifies every emitted symbol resolves against the shipped runtime lib. The customer kernel source is likewise out of scope — the wrapper↔kernel ABI is read from the generated forward-decl, not a real kernel.

> **CORRECTIONs to ABI-08:** none of substance. One refinement: ABI-08 §3b notes the `void`
> `_wrapper` return type but does not explicitly flag that the **docstring example** in
> `_create_wrapper` (lines 208-214) is stale (`int relu_wrapper(){ … return 0; }` vs the real
> `void <fn>_wrapper()` with the `switchBack` tail) — recorded as the §3b **CORRECTION** above.
