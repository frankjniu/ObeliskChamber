# SPDX-License-Identifier: GPL-3.0-or-later
"""First-run game build of the release package: turns the player's own dump into ObeliskChamber.exe on their PC.

    python -I tools/build/fefw_build.py --exefs <dump>/updates/<v>/exefs --game <install folder> [--jobs N] [--keep] [--cpu V]
    python -I tools/build/fefw_build.py --check --game <install folder> [--cpu V]

--cpu picks the build for the processor (CPU_VARIANTS): fma (FMA3 + AVX, the default and faster one) or compat (no FMA3: Sandy
Bridge, Ivy Bridge, Xeon E5 v1/v2, AMD Bulldozer, ...; issue 33). auto (the default; the launcher passes the one it detected) asks the
bundled Zig what this processor has. The variant is part of the build stamp, so an ObeliskChamber.exe built for the other one is rebuilt.

The package ships no game code. This script
  1. verifies the dump's ExeFS against dump_hashes.json (the supported build: sha256 of every module + main's build ID) and writes
     <game>/exefs: the same NSOs with main uncompressed (the console's main is ZBIC-compressed; tools/build/nso_unpack.py),
  2. lifts the six NSOs with the bundled lifter (recomp/nxrecomp) into <game>/build/lift,
  3. compiles the generated C++ with the bundled Zig (zig c++, mingw target) into <game>/build/objcache, in parallel; an object
     an earlier build made from the same inputs is reused (fefw-recomp #208, metrist38),
  4. links ObeliskChamber.exe = sdk/fefw_stub.cpp + the lifted objects + sdk/fefw_rt.lib (the runtime, fefw_rt.dll in the package),
  5. deletes the generated sources and other build files, keeping the lifted code's objects (all of build/ with --keep), and
     records the result in <game>/build/done.json.

The build stamp (state.json, done.json) is the dump (main build ID, module hashes) plus tool_stamp(): everything in this
package that goes into ObeliskChamber.exe (lifter sources, main.sites, compile flags and processor variant, the SDK files with fefw_rt.lib). A release that
changes any of them makes an older stamp stale: Set up then lifts and links again (the old ObeliskChamber.exe stays until the new
one is linked over it) and compiles the units whose inputs changed (object_key: the unit's source, the headers, the flags, Zig's
version). Deleting <game>/build/objcache makes the next build compile everything. --check compares <game>/build/done.json with this package's tools only (no
dump check, well under a second) and prints one JSON line {"current": bool, "reason": text}; exit 0 current, 3 not.

Resumable: a module's lift counts once its marker file is written, an object once it is renamed into place (its name carries
its own hash, so one damaged since is compiled again), so a stopped build continues where it stopped. A file named <game>/build/cancel stops it (the launcher's Pause).
Output for the launcher, one per line on stdout:  @progress <phase> <done> <total> <text>  /  @error <text>
Exit codes: 0 done, 1 error, 2 cancelled.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

HERE = os.path.dirname(os.path.abspath(__file__))
RECOMP = os.path.join(HERE, 'recomp')
SDK = os.path.join(HERE, 'sdk')
WINDOWS = os.name == 'nt'
# the same Windows target from either host: on Linux the launcher starts ObeliskChamber.exe through Wine/Proton
ZIG = os.path.join(HERE, 'zig', 'zig.exe' if WINDOWS else 'zig')
FORMAT = 2          # bump when the build layout or the stamp changes: older builds (partial or done) are then started over
# the lifter as game-build/scripts/make-release.ps1 ships it: paths under RECOMP, the same in the zip (tools/build/recomp/...)
# and in the repo (recomp/...)
LIFTER = ['nxrecomp', 'include/nxrecomp', 'games/fefw/CMakeLists.txt', 'games/fefw/config.py', 'games/fefw/hot.txt',
          'games/fefw/inline.txt']
SITES = 'games/fefw/main.sites'
# what the link takes from sdk/ besides the lifted objects and fefw_rt.lib (stamped on its own, as 'runtime')
SDK_FILES = ['fefw_stub.cpp', 'fefw_rt_api.h', 'fefw_fma.c', 'fefw.rc', 'fefw.manifest', 'fefw.ico', 'fefw_hle.cpp', 'zstd']
# fefw_hle.cpp: native routines the lifted code calls (GameConfig.inline_replace); zstd/: zstd 1.5.7's decoder sources (lib/zstd.h,
# lib/common, lib/decompress; BSD), which fefw_hle.cpp uses. Both are compiled into fefw.exe at the link step.

# name, NSO file, guest base, NxModule.index, translation units (as recomp/games/fefw/CMakeLists.txt)
MODULES = [
    ('main', 'main', 0x7100000000, 0, 153),
    ('sdk', 'sdk', 0x7500000000, 1, 50),
    ('subsdk0', 'subsdk0', 0x7600000000, 2, 5),
    ('subsdk1', 'subsdk1', 0x7640000000, 3, 14),
    ('subsdk2', 'subsdk2', 0x7680000000, 4, 3),
    ('rtld', 'rtld', 0x7700000000, 5, 1),
]
EXEFS_FILES = ['main', 'sdk', 'subsdk0', 'subsdk1', 'subsdk2', 'rtld', 'main.npdm']
# The builds of the game code, one per kind of processor. What the lifted code needs: CMPXCHG16B (the guest's 128-bit exclusive
# pairs, ctx.h) and nothing else beyond x86-64; everything more is speed. fefw_rt.dll is baseline x86-64 (its AVX2 / FMA3 code: the
# BC7 encoder, the UCRT's math, picked by CPUID at run time), so the processor this needs is the game code's.
#   fma     FMA3 (with AVX, which it implies in LLVM): the guest's FMADD / FMLA family is one vfmadd. Intel Haswell (2013), AMD
#           Piledriver (2012) and newer.
#   compat  no FMA3: std::fma / fmaf are calls into sdk/fefw_fma.c (musl's, correctly rounded: the same bits as vfmadd; the fma
#           Zig links otherwise is not exact), so slower in float-heavy code. SSE4.2 and POPCNT keep rounding (FRINT*) and CNT
#           inline. Intel Nehalem (2008) and newer, AMD Bulldozer (2011) and newer: Sandy / Ivy Bridge, Xeon E5 v1 / v2 (issue 33).
# 'needs' is what fefw_rt.dll checks before the first game instruction (fefw_rt_api.h FEFW_CPU_*, the stub passes it on);
# launcher/src-tauri/src/cpu.rs picks the variant by the same rule (variant_for).
CPU_VARIANTS = {
    'fma': {'mcpu': 'x86_64+cx16+fma', 'needs': 'FEFW_CPU_NEEDS_FMA_BUILD', 'features': ('cx16', 'sse4_2', 'avx', 'fma'), 'link': []},
    'compat': {'mcpu': 'x86_64+cx16+sse4_2+popcnt', 'needs': 'FEFW_CPU_NEEDS_COMPAT_BUILD', 'features': ('cx16', 'sse4_2', 'popcnt'),
               'link': ['fefw_fma.c']},
}
# recomp/cmake/NxRecomp.cmake flags for the lifted code, in clang (not clang-cl) syntax, plus NX_INLINE_INDIRECT
# (recomp/games/fefw/CMakeLists.txt); -mcpu per variant. -ffp-contract=off: no multiply and add is fused that the guest kept apart.
# The bundled zig c++ turns -O1, -O2 and -O3 alike into clang -O2 (plus loop and SLP vectorisation; `zig c++ -###` shows it). -g0: no
# debug info (zig adds CodeView by default; nothing reads it, and the compile is faster without). -fomit-frame-pointer: one more host
# register for the lifted code (zig keeps RBP frames by default); host unwinding uses the .pdata tables, not frame pointers.
# NX_NOESCAPE_CTX + the capture-tracking limit (perf/codegen2): the context pointer is `noescape` at every call (recomp ctx.h NX_NOESC), and
# LLVM follows its thousands of uses instead of giving up after 100, so guest loads and stores no longer force the context registers
# in use to be reloaded (hot code: -20 % context loads, -17 % context stores, -4 % instructions; a cold unit: -11 % instructions).
CXXFLAGS_COMMON = ['-std=c++20', '-O1', '-g0', '-fomit-frame-pointer', '-w', '-fno-exceptions', '-fno-rtti', '-fno-strict-aliasing', '-ffp-contract=off', '-fno-math-errno',
                   '-fno-threadsafe-statics', '-DNX_INLINE_INDIRECT',
                   '-DNX_NOESCAPE_CTX', '-mllvm', '-capture-tracking-max-uses-to-explore=100000']


# ---- build tuning (<game>/build_options.json) -------------------------------------------------------------------------------
# Optional, for an ObeliskChamber.exe built for one PC. With no file (or an empty one) the build is the release's, stamp unchanged. Keys:
#   "cpu":         "native" (the processor Set up runs on, as the bundled Zig names it, e.g. raptorlake), an LLVM CPU name
#                  ("raptorlake", "alderlake", "znver4", "x86_64_v3", ...) or "" (the variant's -mcpu). cx16 and popcnt are always
#                  added. A ObeliskChamber.exe built this way may not start on an older processor: build it on the PC that plays.
#   "opt":         "O1" (the release's), "O2" or "O3": optimisation of the lifted code. Higher builds slower and takes more memory.
#   "main_shards": translation units of main (the release: 153). Fewer, larger units let more of main's code inline across
#                  functions, at more memory per compile. 40 to 400.
#   "job_gib":     memory assumed per compile job when the job count is capped by free memory (the release: 2). Raise it for
#                  "O2" / "O3" or fewer shards if compiles fail for memory (a failed unit is retried alone anyway).
#   "fast_fma":    true (the release's) or false. The compatible build only (no FMA3): true does the guest's fused multiply-adds as a
#                  multiply then an add (two roundings, inline), false calls the exactly rounded fma / fmaf of sdk/fefw_fma.c. Savage
#                  Wind measured the compatible build 130 -> 151 fps that way (big win #4). Results may differ in a float's last bit;
#                  false gives the FMA3 build's bits exactly.
# Every key that differs from the release's is part of the build stamp, so changing the file makes Set up build again.
TUNE_DEFAULTS = {'cpu': '', 'opt': 'O1', 'main_shards': MODULES[0][4], 'job_gib': 2, 'fast_fma': True}
TUNE = dict(TUNE_DEFAULTS)


def native_cpu():
    """The bundled Zig's name for this processor (`zig targets`: .native .cpu .name), or None."""
    try:
        out = subprocess.run([ZIG, 'targets'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             timeout=120, **NO_WINDOW).stdout.decode('utf-8', 'replace')
    except (OSError, subprocess.SubprocessError):
        return None
    i = out.find('.native')
    j = out.find('.cpu', i) if i >= 0 else -1
    m = re.search(r'\.name\s*=\s*"([A-Za-z0-9_]+)"', out[j:j + 400]) if j >= 0 else None
    return m.group(1) if m else None


def load_tune(game, quiet=False):
    """Read <game>/build_options.json into TUNE (missing or empty: the release's build). A bad value stops Set up with its name.
    quiet: no progress line (--check prints one JSON line only)."""
    TUNE.clear()
    TUNE.update(TUNE_DEFAULTS)
    path = os.path.join(game, 'build_options.json') if game else ''
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, encoding='utf-8-sig') as f:
            raw = f.read().strip()
        opts = json.loads(raw) if raw else {}
    except (OSError, ValueError) as e:
        raise Fail('build_options.json cannot be read: %s' % e)
    if not isinstance(opts, dict):
        raise Fail('build_options.json must hold one JSON object, e.g. {"cpu": "native", "opt": "O2"}')
    for k, v in opts.items():
        if k.startswith('_'):
            continue                                     # "_comment" keys
        if k not in TUNE_DEFAULTS:
            raise Fail('build_options.json: unknown key "%s" (cpu, opt, main_shards, job_gib, fast_fma)' % k)
        if k == 'cpu':
            if not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_]*', v):
                raise Fail('build_options.json: "cpu" is "native", a CPU name such as "raptorlake", or ""')
            if v == 'native':
                v = native_cpu()
                if not v:
                    raise Fail('build_options.json: "cpu": "native" but the bundled Zig did not name this processor; '
                               'give its name instead (e.g. "raptorlake")')
        elif k == 'opt':
            if v not in ('O1', 'O2', 'O3'):
                raise Fail('build_options.json: "opt" is "O1", "O2" or "O3"')
        elif k == 'main_shards':
            if not isinstance(v, int) or not 40 <= v <= 400:
                raise Fail('build_options.json: "main_shards" is a whole number from 40 to 400')
        elif k == 'job_gib':
            if not isinstance(v, (int, float)) or not 1 <= v <= 16:
                raise Fail('build_options.json: "job_gib" is a number from 1 to 16')
        elif k == 'fast_fma':
            if not isinstance(v, bool):
                raise Fail('build_options.json: "fast_fma" is true or false')
        TUNE[k] = v
    changed = {k: TUNE[k] for k in TUNE if TUNE[k] != TUNE_DEFAULTS[k]}
    if changed and not quiet:
        progress('verify', 0, 1, 'Build options: %s' % ', '.join('%s %s' % kv for kv in sorted(changed.items())))


def tune_stamp():
    """The build options that change ObeliskChamber.exe (job_gib does not), when any differs from the release's; else None."""
    st = {k: TUNE[k] for k in ('cpu', 'opt', 'main_shards', 'fast_fma') if TUNE[k] != TUNE_DEFAULTS[k]}
    return st or None


# Profile-guided builds (developer option, perf/codegen): --pgo-gen <profile runtime .a> builds an instrumented fefw.exe (IR PGO; its
# stub writes the counters to LLVM_PROFILE_FILE every 15 s, since the game ends with TerminateProcess); --pgo-use <.profdata> builds
# with that profile. The lifted sources must be the same in both builds (same package). --mcpu overrides the variant's -mcpu.
# Not used for release builds (a profile fits one build of the lifted code and the PC it was recorded on).
PGO = {'gen': None, 'use': None, 'mcpu': None}


def mcpu(variant):
    """-mcpu of the lifted code: --mcpu (developer), else build_options.json's processor (with cx16 and popcnt), else the variant's."""
    if PGO['mcpu']:
        return PGO['mcpu']
    return TUNE['cpu'] + '+cx16+popcnt' if TUNE['cpu'] else CPU_VARIANTS[variant]['mcpu']


def module_shards(name, shards):
    return TUNE['main_shards'] if name == 'main' else shards


def cxxflags(variant):
    """The compile flags of the lifted code for a CPU_VARIANTS key ('fma': the flags every release before 0.1.7 used), with
    build_options.json's processor and optimisation level when set."""
    common = ['-' + TUNE['opt'] if f == '-O1' else f for f in CXXFLAGS_COMMON]
    extra = []
    if PGO['gen']:
        extra += ['-fprofile-generate', '-mllvm', '-enable-name-compression=false']   # (the LLVM 22 llvm-profdata release has no zlib)
    if PGO['use']:
        extra += ['-fprofile-use=' + PGO['use'], '-Wno-profile-instr-out-of-date', '-Wno-profile-instr-unprofiled',
                  '-mllvm', '-enable-ext-tsp-block-placement']   # profile-driven block order (ext-TSP) for the instruction cache
    if variant == 'compat' and TUNE['fast_fma'] and not TUNE['cpu']:
        extra += ['-DNX_FAST_FMA']   # recomp fpexact.h / cxx.h / neon.h: fused multiply-adds as a multiply and an add (no FMA3 here)
    return ['-target', 'x86_64-windows-gnu', '-mcpu=' + mcpu(variant)] + common + extra
CREATE_NO_WINDOW = 0x08000000
# subprocess's creationflags exist on Windows only: elsewhere a non-zero value raises ValueError (fefw-recomp #172, metrist38)
NO_WINDOW = {'creationflags': CREATE_NO_WINDOW} if WINDOWS else {}
BELOW_NORMAL = 0x00004000


def say(kind, *parts):
    print('@' + kind + ' ' + ' '.join(str(p) for p in parts), flush=True)


def progress(phase, done, total, text):
    say('progress', phase, done, total, text)


class Fail(Exception):
    pass


class Cancelled(Exception):
    pass


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def tree_hash(root, rels):
    """One sha256 over the files under root/rel for each rel (a file or a folder, walked in sorted order): each file's path
    relative to root and its sha256. Python's bytecode caches are left out (the lifter's own runs write them); a missing
    path counts as missing, so the hash still compares."""
    h = hashlib.sha256()
    for rel in rels:
        top = os.path.join(root, *rel.split('/'))
        if os.path.isfile(top):
            files = [rel]
        elif os.path.isdir(top):
            files = []
            for d, dirs, names in os.walk(top):
                dirs[:] = [x for x in dirs if x != '__pycache__']
                files += [os.path.relpath(os.path.join(d, n), root).replace('\\', '/') for n in names if not n.endswith('.pyc')]
        else:
            h.update(('%s\0missing\0' % rel).encode())
            continue
        for f in sorted(files):
            h.update(('%s\0%s\0' % (f, sha256(os.path.join(root, *f.split('/'))))).encode())
    return h.hexdigest()


# The launcher's SHADER_TRANSLATOR_VERSION (launcher/src-tauri/src/setup.rs): keep the two equal.
SHADER_TRANSLATOR_VERSION = 4


def tool_stamp(variant, patch_stamp=None, sites_stamp=None):
    """The part of the build stamp this package decides: a release that changes any of it rebuilds ObeliskChamber.exe. The processor
    variant is in it as well, so an ObeliskChamber.exe built for the other one is rebuilt (the lift is kept: see main). The exefs patches of
    the installed mods (exefs_patch_stamp) and their hook sites (mod_sites_stamp) are in it only when there are any, so a build without
    them keeps its old stamp."""
    stamp = {'lifter': tree_hash(RECOMP, LIFTER), 'sites': tree_hash(RECOMP, [SITES]), 'sdk': tree_hash(SDK, SDK_FILES),
             'runtime': sha256(os.path.join(SDK, 'fefw_rt.lib')), 'cxxflags': cxxflags(variant), 'cpu': variant,
             'modules': [list(m) for m in MODULES]}
    if patch_stamp:
        stamp['exefs_patches'] = patch_stamp
    if sites_stamp:
        stamp['mod_sites'] = sites_stamp
    if tune_stamp():
        stamp['tune'] = tune_stamp()
    return stamp


# FEFW_CPU_FORCE_MISSING names (as fefw_rt.dll and the launcher read them) -> Zig's feature names
FORCE_NAMES = {'fma': ('fma',), 'avx': ('avx', 'fma'), 'cx16': ('cx16',), 'sse42': ('sse4_2',), 'sse4.2': ('sse4_2',), 'popcnt': ('popcnt',)}


def host_features():
    """This processor's features as the bundled Zig detects them (`zig targets`: .native .cpu .features), or None when that
    cannot be read. Zig leaves AVX and FMA out when the operating system does not save the YMM registers, as the check in
    fefw_rt.dll does. FEFW_CPU_FORCE_MISSING (a test hook, as for the launcher and fefw_rt.dll) hides features."""
    try:
        out = subprocess.run([ZIG, 'targets'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             timeout=120, **NO_WINDOW).stdout.decode('utf-8', 'replace')
    except (OSError, subprocess.SubprocessError):
        return None
    i = out.find('.native')
    j = out.find('.features', i) if i >= 0 else -1
    if j < 0:
        return None
    feats = set(re.findall(r'"([^"]+)"', out[j:out.find('}', j)]))
    if not feats:
        return None
    for name in os.environ.get('FEFW_CPU_FORCE_MISSING', '').split(','):
        feats.difference_update(FORCE_NAMES.get(name.strip().lower(), ()))
    return feats


def pick_variant(want):
    """--cpu (fma / compat / auto) -> (variant, None), or (None, why) when this processor runs neither. auto: FEFW_BUILD_CPU when
    set to a variant, else the fastest one this processor runs; fma when the processor cannot be read."""
    if want in (None, '', 'auto'):
        want = os.environ.get('FEFW_BUILD_CPU', '').strip().lower() or 'auto'
    if want in CPU_VARIANTS:
        return want, None
    if want != 'auto':
        return None, 'unknown --cpu %s (fma, compat or auto)' % want
    feats = host_features()
    if feats is None:
        return 'fma', None
    for v in ('fma', 'compat'):
        if all(f in feats for f in CPU_VARIANTS[v]['features']):
            return v, None
    missing = [f for f in CPU_VARIANTS['compat']['features'] if f not in feats]
    return None, ("This PC's processor does not support %s, which the game needs (an Intel Core i3 / i5 / i7 of 2008 or newer, or "
                  "an AMD FX of 2011 or newer)." % ', '.join(f.replace('cx16', 'CMPXCHG16B').replace('sse4_2', 'SSE4.2').upper() for f in missing))


def check(game, variant):
    """Whether ObeliskChamber.exe and <game>/build/done.json were built with this package's tools for this processor: (current, reason)."""
    if not os.path.isfile(os.path.join(game, 'ObeliskChamber.exe')):
        return False, 'ObeliskChamber.exe is not built yet'
    done = read_json(os.path.join(game, 'build', 'done.json'))
    if not isinstance(done, dict):
        return False, 'ObeliskChamber.exe has no finished build record'
    if done.get('format') != FORMAT or not isinstance(done.get('tools'), dict):
        return False, 'ObeliskChamber.exe was built by an older release'
    try:
        load_tune(game, quiet=True)
        bid = json.load(open(os.path.join(HERE, 'dump_hashes.json'), encoding='utf-8'))['main_build_id']
        found, left_out, _ = collect_exefs_patches(game, bid)
        patch_stamp = exefs_patch_stamp(found, left_out)
        sites_stamp = mod_sites_stamp(collect_mod_sites(game, bid))
    except Fail as e:
        return False, str(e)
    want = tool_stamp(variant, patch_stamp, sites_stamp)
    changed = [k for k in sorted(set(want) | set(done['tools'])) if done['tools'].get(k) != want.get(k)]
    if changed == ['mod_sites'] and mod_sites_lifted(game, collect_mod_sites(game, bid), bid):
        changed = []                     # mods only took sites away: the build still has every site the installed mods need
    if changed == ['exefs_patches']:
        return False, 'the exefs patches in mods changed (Set up applies them)'
    if changed and set(changed) <= {'exefs_patches', 'mod_sites'}:
        return False, 'the hook sites of mods changed (Set up lifts the game with them)'
    if changed and set(changed) <= {'tune', 'cxxflags'}:
        return False, 'build_options.json changed (Set up builds with it)'
    if 'cpu' in changed and done['tools'].get('cpu') in CPU_VARIANTS:
        return False, ('ObeliskChamber.exe was built for processors with FMA3; this one needs the compatible build' if variant == 'compat'
                       else 'ObeliskChamber.exe is the compatible build; this processor runs the faster FMA3 build')
    if changed:
        return False, 'this release changes ' + ', '.join(changed)
    shaders = read_json(os.path.join(game, 'shader_cache', 'setup.json'))
    if isinstance(shaders, dict) and shaders.get('translator_version') != SHADER_TRANSLATOR_VERSION:
        return False, 'this release changes the shader translator (shaders are translated again)'
    return True, 'ObeliskChamber.exe matches this release'


def read_json(path):
    """The JSON in path, or None if it is missing or unreadable (a file cut short by a crash or power loss starts over)."""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, value):
    """Write through a temporary file and rename, so a stop mid-write never leaves half a file behind."""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(value, f, indent=1)
    os.replace(tmp, path)


def verify(exefs):
    want = json.load(open(os.path.join(HERE, 'dump_hashes.json'), encoding='utf-8'))
    missing = [n for n in want['sha256'] if not os.path.isfile(os.path.join(exefs, n))]
    if missing:
        raise Fail('The game files folder %s is missing %s. Pick the exefs of a complete dump.' % (exefs, ', '.join(missing)))
    with open(os.path.join(exefs, 'main'), 'rb') as f:
        head = f.read(0x60)
    if head[:4] != b'NSO0':
        raise Fail('exefs/main is not a decrypted NSO. Extract the game files with your own keys first (decrypted exefs and romfs).')
    bid = head[0x40:0x54].hex()
    if bid != want['main_build_id']:
        raise Fail('This is a different version of the game (main build ID %s). This release supports %s (build ID %s).'
                   % (bid, want['version'], want['main_build_id']))
    names = list(want['sha256'])
    for i, n in enumerate(names):
        progress('verify', i, len(names), 'Checking your game files: %s' % n)
        got = sha256(os.path.join(exefs, n))
        if n == 'main' and got == want.get('main_plain_sha256'):
            continue                     # a dump tool that already wrote main uncompressed
        if got != want['sha256'][n]:
            raise Fail('exefs/%s does not match the supported %s dump (sha256 %s). Re-dump the game, or check that the update %s is installed.'
                       % (n, want['version'], got, want['version']))
    progress('verify', len(names), len(names), 'Game files match %s' % want['version'])
    return want


def write_exefs(exefs, out):
    """<game>/exefs: the dump's NSOs, main uncompressed (the lifter and the runtime read plain NSOs only)."""
    sys.path.insert(0, HERE)
    from nso_unpack import unpack
    os.makedirs(out, exist_ok=True)
    for n in EXEFS_FILES:
        src = os.path.join(exefs, n)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(out, n)
        if n == 'main':
            data = open(src, 'rb').read()
            flags = int.from_bytes(data[12:16], 'little')
            if flags & 0x87:
                progress('verify', 0, 1, 'Unpacking main (ZBIC)')
                data, _ = unpack(data)
            tmp = dst + '.tmp'
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, dst)
        else:
            shutil.copyfile(src, dst)


# ---- exefs patches (mods/<name>/exefs/*.ips, *.pchtxt) ----------------------------------------------------------------------
# The game code is lifted ahead of time, so a patch cannot be applied when the game starts as on an emulator. Set up applies the
# patches of the installed mods to <game>/exefs/main (the plain NSO the lifter and the runtime both read) before lifting: code
# patches are then compiled into ObeliskChamber.exe, data patches are loaded by the runtime. Only patches for main's build ID are used. The
# patched bytes are part of the build stamp, so adding, changing or removing a patch makes Set up build again. A patch must not touch
# an instruction that main.sites patches or hooks (the hook would run against code it was not written for): Set up stops and names it.
PROGRAM_ID = 0x04007EF01F8AE000
NSO_HEADER = 0x100              # IPS offsets (and pchtxt addresses with @flag offset_shift 0x100) count from the NSO header


def find_mod_dirs(root, include_off=False):
    """The mods as the runtime finds them (runtime/src/services/common.cpp find_mods): a folder below mods/ that has romfs/ or
    exefs/ is a mod; names starting with -, _ or . are off (with include_off, those starting with - or _ are found too); a
    16-digit hex folder that is another title's program ID is skipped. Returns [(name, path)] sorted by name, so the mod whose
    name sorts last wins where two patch the same bytes."""
    out = []

    def walk(d, name, depth):
        if depth > 0 and (os.path.isdir(os.path.join(d, 'romfs')) or os.path.isdir(os.path.join(d, 'exefs'))):
            out.append((name, d))
            return
        if depth >= 4:
            return
        try:
            subs = sorted(os.listdir(d))
        except OSError:
            return
        for sub in subs:
            p = os.path.join(d, sub)
            if not os.path.isdir(p) or not sub or sub[0] in ('.' if include_off else '-_.'):
                continue
            if len(sub) == 16 and all(c in '0123456789abcdefABCDEF' for c in sub) and int(sub, 16) != PROGRAM_ID:
                continue
            walk(p, sub if not name else name + '/' + sub, depth + 1)

    if os.path.isdir(root):
        walk(root, '', 0)
    return sorted(out, key=lambda t: t[0].lower())


def bid_matches(text, build_id):
    """An IPS file name or a pchtxt @nsobid against main's build ID (hex, 32 bytes): a prefix of it, the rest zeros, as Atmosphere."""
    t = text.strip().lower()
    full = build_id.lower().ljust(64, '0')          # dump_hashes.json keeps the 20 bytes of the ID; the header field is 32
    if len(t) < 16 or len(t) > 64 or any(c not in '0123456789abcdef' for c in t):
        return False
    return full.startswith(t) and set(full[len(t):]) <= {'0'}


def parse_ips(data, where):
    """IPS (PATCH ... EOF, 3-byte offsets) or IPS32 (IPS32 ... EEOF, 4-byte offsets); RLE records too. -> [(file offset, bytes)]."""
    if data[:5] == b'IPS32':
        osz, end, p = 4, b'EEOF', 5
    elif data[:5] == b'PATCH':
        osz, end, p = 3, b'EOF', 5
    else:
        raise Fail('%s is not an IPS patch (no PATCH or IPS32 header)' % where)
    out = []
    while True:
        if data[p:p + len(end)] == end and p + len(end) >= len(data) - 3:     # the end marker (a truncation size may follow)
            return out
        if p + osz + 2 > len(data):
            raise Fail('%s ends without %s' % (where, end.decode()))
        off = int.from_bytes(data[p:p + osz], 'big')
        size = int.from_bytes(data[p + osz:p + osz + 2], 'big')
        p += osz + 2
        if size == 0:                    # RLE: u16 count, one byte
            if p + 3 > len(data):
                raise Fail('%s: an RLE record is cut short' % where)
            count = int.from_bytes(data[p:p + 2], 'big')
            out.append((off, bytes([data[p + 2]]) * count))
            p += 3
        else:
            if p + size > len(data):
                raise Fail('%s: a record at 0x%x is cut short' % (where, off))
            out.append((off, bytes(data[p:p + size])))
            p += size


PCHTXT_LINE = re.compile(r'^(?:0[xX])?([0-9A-Fa-f]+)\s+(.+)$')


def pchtxt_strip_comment(raw):
    """The line without a // comment; a // inside a "string" (after a backslash-free quote) is part of the string."""
    q = False
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == '\\' and q:
            i += 2
            continue
        if c == '"':
            q = not q
        elif c == '/' and not q and raw[i + 1:i + 2] == '/':
            return raw[:i].strip()
        i += 1
    return raw.strip()


def pchtxt_bytes(val, little):
    """The value of a pchtxt patch line: "a string" (C escapes) or hex bytes, run together (IPSwitch) or two digits per token
    (EmblemWeave's patches.rs) -> bytes, or None when it is neither."""
    if val.startswith('"'):
        if not val.endswith('"') or len(val) < 2:
            return None
        try:
            return val[1:-1].encode('utf-8').decode('unicode_escape').encode('latin-1')
        except (UnicodeDecodeError, UnicodeEncodeError):
            return None
    hx = re.sub(r'\s+', '', val)
    if not hx or len(hx) % 2 or any(c not in '0123456789abcdefABCDEF' for c in hx):
        return None
    b = bytes.fromhex(hx)
    return b   # written as typed: @little-endian / @big-endian change nothing, as on Eden (common/hex_util.cpp), Ryujinx and EmblemWeave


def parse_pchtxt(text, where, build_id):
    """IPSwitch .pchtxt: @nsobid-<id>, @flag offset_shift <n>, @enabled / @disabled blocks, @stop, @little-endian / @big-endian,
    `ADDR HEXBYTES` and `ADDR "string"` lines, // ; # comments. -> [(file offset, bytes)], or None when it is for another build.

    Read as EmblemWeave reads it (apps/emblemweave/src/patches.rs), so a mod made for either applies the same here: lines before
    the first @enabled / @disabled apply, ADDR may have any number of digits and a 0x, and the offset shift is hexadecimal with
    or without 0x. Beyond that, a block's title line that is not an address and bytes ("Add 60 FPS") is passed over, where
    EmblemWeave refuses the file."""
    out = []
    shift = 0
    enabled = True
    little = False
    bid = None
    for ln, raw in enumerate(text.splitlines(), 1):
        line = pchtxt_strip_comment(raw)
        if not line or line[0] in ';#':
            continue
        low = line.lower()
        if low.startswith('@nsobid-'):
            bid = line[8:].strip()
            if not bid_matches(bid, build_id):
                return None
            continue
        if low == '@stop':
            break
        if low == '@enabled':
            enabled = True
            continue
        if low == '@disabled':
            enabled = False
            continue
        if low == '@little-endian':
            little = True
            continue
        if low == '@big-endian':
            little = False
            continue
        if low.startswith('@flag'):
            parts = line.split()
            if len(parts) >= 3 and parts[1].lower() == 'offset_shift':
                v = parts[2]
                neg = v.startswith('-')
                body = v[1:] if neg else v
                if body[:2].lower() == '0x':
                    body = body[2:]
                try:
                    shift = int(body, 16) * (-1 if neg else 1)
                except ValueError:
                    raise Fail('%s:%d: @flag offset_shift needs a hexadecimal number (such as 0x100), not "%s"'
                               % (where, ln, ' '.join(parts[2:])))
            continue                     # "@flag offset_shift" with no number changes nothing, as in fefw-recomp
        if low.startswith('@'):
            continue                     # other @ lines (e.g. @flag print_values) change nothing here
        m = PCHTXT_LINE.match(line)
        if not m:
            continue                     # a title line between blocks ("Add 60 FPS")
        val = m.group(2).strip()
        b = pchtxt_bytes(val, little)
        if b is None:
            if len(m.group(1)) == 8 and not line[:2].lower() == '0x' and enabled:
                raise Fail('%s:%d: %s is not hex bytes or a closed "string"' % (where, ln, val))
            continue                     # a title that starts with a hex-looking word ("Add 60 FPS", "Fade in")
        if not enabled:
            continue
        out.append((int(m.group(1), 16) + shift, b))
    if bid is None:
        raise Fail('%s has no @nsobid line, so it cannot be matched to the game version' % where)
    return out


def mod_exefs_on(d):
    """The exefs switch of the mod's mod.ini (the game's mod menu writes it; runtime/src/services/common.cpp read_mod_state): a line
    "exefs = off" (or 0, false, no) leaves the mod's exefs patches out. A missing file or line means on."""
    try:
        with open(os.path.join(d, 'mod.ini'), encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.split('#')[0].split(';')[0]
                if '=' not in line:
                    continue
                k, v = (x.strip().lower() for x in line.split('=', 1))
                if k == 'exefs':
                    return v not in ('off', '0', 'false', 'no')
    except OSError:
        pass
    return True


def exefs_files(ex):
    """Every file in a mod's exefs/ and the folders in it (EmblemWeave's patches.rs walks them too), skipping folders whose name
    starts with "." -> [(path relative to exefs/ with /, full path)], sorted by that relative path, case ignored."""
    out = []
    for root, dirs, files in os.walk(ex):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            full = os.path.join(root, f)
            out.append((os.path.relpath(full, ex).replace(os.sep, '/'), full))
    out.sort(key=lambda t: t[0].lower())
    return out


def collect_exefs_patches(game, build_id):
    """The exefs patches of the installed mods for main: ([(mod name, file, [(memory offset, bytes)])] in the order they apply,
    {mod name: why it is left out}, [mod names with patch files but none for this main]). IPS files count when their name is main's
    build ID; pchtxt files when their @nsobid is. A mod with a patch file that cannot be read is left out whole (the others still
    apply): half of a mod's patches could leave the game in a state its author never tried."""
    found, left_out, unused = [], {}, []
    for name, d in find_mod_dirs(os.path.join(game, 'mods')):
        ex = os.path.join(d, 'exefs')
        if not os.path.isdir(ex) or not mod_exefs_on(d):
            continue
        mine, any_file = [], False
        try:
            for rel, p in exefs_files(ex):
                f = os.path.basename(rel)
                stem, ext = os.path.splitext(f)
                ext = ext.lower()
                where = '%s/exefs/%s' % (name, rel)
                if ext in ('.ips', '.ips32'):
                    any_file = True
                    if not bid_matches(stem, build_id):
                        continue         # another module or another version
                    recs = parse_ips(open(p, 'rb').read(), where)
                elif ext == '.pchtxt':
                    any_file = True
                    recs = parse_pchtxt(open(p, encoding='utf-8-sig', errors='replace').read(), where, build_id)
                    if recs is None:
                        continue
                else:
                    continue
                mem = []
                for off, b in recs:
                    if off < NSO_HEADER:
                        raise Fail('%s: offset 0x%x is inside the NSO header (patches start at 0x100)' % (where, off))
                    if b:
                        mem.append((off - NSO_HEADER, b))
                if mem:
                    mine.append((name, where, mem))
        except Fail as e:
            left_out[name] = str(e)
            continue
        except OSError as e:
            left_out[name] = 'cannot read %s/exefs: %s' % (name, e)
            continue
        if mine:
            found += mine
        elif any_file:
            unused.append(name)
    return found, left_out, unused


def exefs_patch_stamp(patches, left_out=None):
    """One hash over what the patches write, in order (a changed disabled block or comment does not build again), and the mods
    left out with the reason, so fixing or removing one builds again."""
    if not patches and not left_out:
        return None
    h = hashlib.sha256()
    for name, where, mem in patches:
        h.update(('%s\0' % where).encode())
        for off, b in mem:
            h.update(off.to_bytes(8, 'little') + len(b).to_bytes(4, 'little') + b)
    for name in sorted(left_out or {}):
        h.update(('left out %s\0%s\0' % (name, left_out[name])).encode())
    return h.hexdigest()


def site_addresses():
    """main.sites: {module offset: kind} of every site but noinline (patch, inline, midhook, entryhook, dynimm, jtable)."""
    out = {}
    try:
        lines = open(os.path.join(RECOMP, *SITES.split('/')), encoding='utf-8').read().splitlines()
    except OSError:
        return out
    for line in lines:
        t = line.split('#', 1)[0].split()
        if len(t) >= 2 and t[0] != 'noinline':
            out[int(t[1], 16) - MODULES[0][2]] = t[0]
    return out


PAGE = 0x1000


def nso_segments(data):
    """[(file offset, memory offset, size)] of text, rodata and data from an NSO header."""
    return [tuple(int.from_bytes(data[0x10 + 0x10 * i + k:0x14 + 0x10 * i + k], 'little') for k in (0, 4, 8)) for i in range(3)]


def segment_limit(segs, i):
    """Where segment i may be patched up to. Text and rodata: their end-of-page padding too, up to the next page boundary and never
    into the next segment (a "code cave": that padding is mapped with the segment on the console and in emulators). Data: its end,
    since what follows it is the zeroed .bss."""
    _, mo, sz = segs[i]
    if i >= 2:
        return mo + sz
    return min((mo + sz + PAGE - 1) & ~(PAGE - 1), segs[i + 1][1])


def sext(v, bits):
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def branch_target(word, pc):
    """(kind, target) of a direct branch at pc: 'b' / 'bl' for B and BL, 'cond' for B.cond, CBZ / CBNZ and TBZ / TBNZ; else None."""
    if word & 0x7C000000 == 0x14000000:
        return ('bl' if word & 0x80000000 else 'b'), pc + sext(word & 0x3FFFFFF, 26) * 4
    if word & 0xFF000010 == 0x54000000 or word & 0x7E000000 == 0x34000000:
        return 'cond', pc + sext((word >> 5) & 0x7FFFF, 19) * 4
    if word & 0x7E000000 == 0x36000000:
        return 'cond', pc + sext((word >> 5) & 0x3FFF, 14) * 4
    return None


def apply_exefs_patches(main_path, patches, mod_sites=()):
    """Write the patches into the plain main NSO (uncompressed segments) and fix its header. Mod by mod: a mod with a patch that
    cannot be applied (outside main, on a main.sites site or one a mod adds, or in padding another mod already wrote) is left out whole
    with a note, and the rest apply. A patch in the padding after text or rodata (a code cave, segment_limit) grows that segment: the
    bytes go in after it, the later segments move up in the file, and the sizes and hashes in the header follow, so the lifter lifts
    the cave's code. -> ([mod names applied], {mod name: why it was left out})."""
    if not patches:
        return [], {}
    with open(main_path, 'rb') as f:
        data = bytearray(f.read())
    flags = int.from_bytes(data[12:16], 'little')
    if flags & 7:
        raise Fail('exefs/main has LZ4-compressed segments; exefs patches need the plain main Set up writes')
    segs = nso_segments(data)
    body = [bytearray(data[fo:fo + sz]) for fo, _, sz in segs]
    sites = site_addresses()
    for where, kind, addr in mod_sites:
        sites.setdefault(addr - MODULES[0][2], '%s (%s)' % (kind, where))
    owner = {}                           # memory offset -> file, for bytes patched so far
    cave_owner = {}                      # the same for bytes in a segment's padding: two mods there is an error, not "the later wins"
    caves = {}                           # mod name -> [lo, hi) it wrote in the padding after text
    by_mod = []
    for name, where, mem in patches:
        if not by_mod or by_mod[-1][0] != name:
            by_mod.append((name, []))
        by_mod[-1][1].append((where, mem))
    applied, left_out, records = [], {}, 0
    for name, files in by_mod:
        plan, problem, mine = [], None, {}
        for where, mem in files:
            for off, b in mem:
                end = off + len(b)
                seg = next((i for i, (_, mo, _) in enumerate(segs) if mo <= off and end <= segment_limit(segs, i)), None)
                if seg is None:
                    problem = ("%s: 0x%x..0x%x is outside main's text, rodata and data (and the padding after text or rodata, up to "
                               "the next page)" % (where, off, end))
                    break
                a = next((a for a in range(off & ~3, end, 4) if a in sites), None)
                if a is not None:
                    problem = ('%s patches 0x%x, where this release has a main.sites %s site: the two cannot be combined. '
                               'Remove the patch or ask for the site to be moved.' % (where, a, sites[a]))
                    break
                seg_end = segs[seg][1] + segs[seg][2]
                other = next((cave_owner[a] for a in range(max(off, seg_end), end) if cave_owner.get(a, where) != where), None)
                if other:
                    problem = '%s and %s both write the padding after main\'s %s at 0x%x' % (other, where, ('text', 'rodata')[seg], max(off, seg_end))
                    break
                for a in range(max(off, seg_end), end):
                    mine[a] = where
                plan.append((seg, off, b, where))
            if problem:
                break
        if problem:
            left_out[name] = problem
            progress('verify', 0, 1, 'Note: %s left out: %s' % (name, problem))
            continue
        for seg, off, b, where in plan:
            clash = next((owner[a] for a in range(off, off + len(b)) if owner.get(a, where) != where), None)
            if clash:
                progress('verify', 0, 1, 'Note: %s overwrites bytes %s patched at 0x%x (the later mod wins)' % (where, clash, off))
            for a in range(off, off + len(b)):
                owner[a] = where
            mo = segs[seg][1]
            if off + len(b) - mo > len(body[seg]):
                body[seg].extend(bytes(off + len(b) - mo - len(body[seg])))
            body[seg][off - mo:off - mo + len(b)] = b
            if seg == 0 and off + len(b) > segs[0][1] + segs[0][2]:
                lo, hi = caves.get(name, (off + len(b), 0))
                caves[name] = (min(lo, max(off, segs[0][1] + segs[0][2])), max(hi, off + len(b)))
            records += 1
        cave_owner.update(mine)
        applied.append(name)
    if not applied:
        return applied, left_out
    order = sorted(range(3), key=lambda i: segs[i][0])
    out = bytearray(data[:segs[order[0]][0]])
    prev = None
    for i in order:
        if prev is not None:
            out += data[prev:segs[i][0]]
        fo = len(out)
        out += body[i]
        prev = segs[i][0] + segs[i][2]
        out[0x10 + 0x10 * i:0x14 + 0x10 * i] = fo.to_bytes(4, 'little')
        out[0x18 + 0x10 * i:0x1C + 0x10 * i] = len(body[i]).to_bytes(4, 'little')
        out[0x60 + 4 * i:0x64 + 4 * i] = len(body[i]).to_bytes(4, 'little')
        out[0xA0 + 0x20 * i:0xC0 + 0x20 * i] = hashlib.sha256(body[i]).digest()
    out += data[prev:]
    for i, name in ((0, 'text'), (1, 'rodata')):
        if len(body[i]) > segs[i][2]:
            progress('verify', 0, 1, "Main's %s grows by %d bytes for a code cave (0x%x..0x%x)"
                     % (name, len(body[i]) - segs[i][2], segs[i][1] + segs[i][2], segs[i][1] + len(body[i])))
    # the lifter finds code by the B / BL that reach it (recomp/nxrecomp/funcs.py); a cave entered another way (a pointer in data, a
    # conditional branch) may not run as the mod meant. Only the mods' own bytes can branch into a cave, so only those are read.
    ts = segs[0][1]
    for name, (lo, hi) in caves.items():
        kinds = set()
        for _, where, mem in patches:
            if where.split('/exefs/')[0] not in applied:
                continue
            for off, b in mem:
                for a in range((off + 3) & ~3, off + len(b) - 3, 4):
                    if lo <= a < hi or not ts <= a < ts + len(body[0]):
                        continue
                    k = branch_target(int.from_bytes(body[0][a - ts:a - ts + 4], 'little'), a)
                    if k and lo <= k[1] < hi:
                        kinds.add(k[0])
        if not kinds & {'b', 'bl'}:
            progress('verify', 0, 1, 'Note: %s has code after main\'s code (0x%x..0x%x) that no B or BL of its patches reaches: if the '
                     'game gets there another way, that path may crash. Tell the mod\'s author.' % (name, lo, hi))
        elif 'cond' in kinds:
            progress('verify', 0, 1, 'Note: %s branches into its code after main\'s code with a conditional branch as well; only B and BL '
                     'are sure to be lifted as entries.' % name)
    tmp = main_path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(out)
    os.replace(tmp, main_path)
    progress('verify', 1, 1, 'Applied %d exefs patch record(s) from %s' % (records, ', '.join(applied)))
    return applied, left_out


EXEFS_RECORD = 'exefs_mods.txt'


def write_exefs_record(exefs, applied, left_out, unused):
    """<game>/exefs/exefs_mods.txt: what the last Set up did with each mod's exefs patches, for the runtime's start-up log
    (runtime/src/services/fs.cpp). Lines: applied<TAB>mod, left out<TAB>mod<TAB>why, not used<TAB>mod (none for this main)."""
    dst = os.path.join(exefs, EXEFS_RECORD)
    rows = [('applied', n, '') for n in applied] + [('left out', n, left_out[n]) for n in sorted(left_out)] + \
           [('not used', n, '') for n in unused]
    if not rows:
        if os.path.isfile(dst):
            os.remove(dst)
        return
    tmp = dst + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('# The exefs patches of mods as the last Set up used them (written by Set up)\n')
        for st, n, why in rows:
            f.write('%s\t%s%s\n' % (st, n, '\t' + why.replace('\t', ' ').replace('\n', ' ') if why else ''))
    os.replace(tmp, dst)


# ---- hook sites of mods (mods/<name>/exefs/*.sites) --------------------------------------------------------------------------------
# A code mod (exefs/*.dll, runtime/include/nxrt/mod_api.h) can hook only an instruction the game was lifted with as a site. A mod
# that needs sites of its own lists them in exefs/<anything>.sites, in the syntax of main.sites but only with the kinds that just add
# a call into the runtime (inline, midhook, entryhook: the instructions are lifted as before), plus one line
#     @nsobid <main build ID>          (as in a .pchtxt)
# so that sites for another version of the game are left out. Set up lifts main with main.sites plus these sites; they are part
# of the build stamp, so installing, updating or removing such a mod lifts again. The sites of a mod with exefs = off in mod.ini,
# or switched off by a - or _ at the start of its folder name, are kept (as EmblemWeave's sites.rs keeps them): a site no hook is
# attached to only passes through, so switching the mod on or off (the game's Mods menu, or renaming it) needs no Set up, only the
# next start, like any DLL mod. The list main was lifted with is written next to ObeliskChamber.exe (ObeliskChamber.sites,
# write_lifted_sites), so the runtime can tell a mod whose sites are missing from this build.
MOD_SITE_KINDS = ('inline', 'midhook', 'entryhook')
WARNED_SITES = set()   # (file:line) of the sites of an unknown kind already reported (collect_mod_sites)


def collect_mod_sites(game, build_id):
    """The hook sites the installed mods add to main: [(file:line, kind, guest address)], in mod order."""
    found = []
    for name, d in find_mod_dirs(os.path.join(game, 'mods'), include_off=True):
        ex = os.path.join(d, 'exefs')
        if not os.path.isdir(ex):        # exefs = off and a switched-off folder do not count here (see above)
            continue
        for f in sorted(os.listdir(ex), key=str.lower):
            if not f.lower().endswith('.sites') or not os.path.isfile(os.path.join(ex, f)):
                continue
            where = '%s/exefs/%s' % (name, f)
            bid, recs, skipped = None, [], []
            text = open(os.path.join(ex, f), encoding='utf-8-sig', errors='replace').read()
            for ln, line in enumerate(text.splitlines(), 1):
                t = line.split('#', 1)[0].split()
                if not t:
                    continue
                if t[0].lower() == '@nsobid':
                    if len(t) != 2:
                        raise Fail('%s:%d: @nsobid takes one build ID' % (where, ln))
                    bid = t[1]
                    continue
                if t[0] not in MOD_SITE_KINDS:
                    # A kind another port defines, or a later one: skipped with a warning (below, once the build ID matches) rather
                    # than stopping Set up, as the three ports agreed in fefw-recomp issue #165.
                    skipped.append((ln, t[0]))
                    continue
                try:
                    if len(t) != 2:
                        raise ValueError
                    addr = int(t[1], 16)
                except ValueError:
                    raise Fail('%s:%d: expected "<kind> <guest address in hex>"' % (where, ln))
                recs.append((ln, t[0], addr))
            if bid is None:
                raise Fail('%s has no @nsobid line (the build ID of the main its sites are for)' % where)
            if not bid_matches(bid, build_id):
                continue                 # another version of the game
            for ln, kind in skipped:     # stderr, once per run: --check prints one JSON line on stdout
                key = (where, ln)
                if key not in WARNED_SITES:
                    WARNED_SITES.add(key)
                    print('warning: %s:%d: "%s" is not a site a mod can add here (%s): skipped'
                          % (where, ln, kind, ', '.join(MOD_SITE_KINDS)), file=sys.stderr, flush=True)
            for ln, kind, addr in recs:
                if addr % 4 or not MODULES[0][2] <= addr < MODULES[1][2]:
                    raise Fail('%s:%d: 0x%x is not an instruction address in main (main is at 0x%x)' % (where, ln, addr, MODULES[0][2]))
                found.append(('%s:%d' % (where, ln), kind, addr))
    return found


def mod_sites_stamp(mod_sites):
    """One hash over the sites the mods add (comments, order and duplicates do not count)."""
    if not mod_sites:
        return None
    return hashlib.sha256(''.join('%s %x\n' % ka for ka in sorted({(k, a) for _, k, a in mod_sites})).encode()).hexdigest()


# Midhook sites and the condition flags (runtime/include/nxrt/hooks.h): lifted functions keep NZCV in a local, so a midhook that
# runs the rest of the function itself starts it with stale flags. Its site has to be where the flags are set again before they are
# read, never between a compare and the instruction that uses it. Set up follows the code forward from the site (flags_warning) and
# notes a midhook whose flags are read before they are set again. (mask, value, name)
FLAG_READERS = (
    (0xFF000010, 0x54000000, 'b.cond'),
    (0x3FE00000, 0x1A800000, 'csel / csinc / csinv / csneg (cset, cinc, cneg ...)'),
    (0x3FE00000, 0x3A400000, 'ccmp / ccmn'),
    (0xFF200C00, 0x1E200400, 'fccmp / fccmpe'),
    (0xFF200C00, 0x1E200C00, 'fcsel'),
    (0x1FE0FC00, 0x1A000000, 'adc / sbc'),
    (0xFFFFFFE0, 0xD53B4200, 'mrs nzcv'),
)
FLAG_SETTERS = (
    (0x3F800000, 0x31000000, 'adds / subs (immediate; cmp / cmn)'),
    (0x3F000000, 0x2B000000, 'adds / subs (register; cmp / cmn)'),
    (0x7F800000, 0x72000000, 'ands (immediate; tst)'),
    (0x7F000000, 0x6A000000, 'ands / bics (register; tst)'),
    (0x3FE0FC00, 0x3A000000, 'adcs / sbcs'),
    (0x3FE00000, 0x3A400000, 'ccmp / ccmn'),
    (0xFF203C00, 0x1E202000, 'fcmp / fcmpe'),
    (0xFF200C00, 0x1E200400, 'fccmp / fccmpe'),
    (0xFFFFFFE0, 0xD51B4200, 'msr nzcv'),
)


def insn_kind(word, table):
    return next((name for mask, value, name in table if word & mask == value), None)


# Instructions that end the forward scan. A call (bl / blr) or a return leaves the flags dead (the ABI does not keep them across
# either); br goes somewhere the scan cannot follow. b is followed to its target; cbz / cbnz / tbz / tbnz, which do not touch the
# flags, are followed on their fall-through path.
FLAG_SCAN_STOP = (
    (0xFC000000, 0x94000000),   # bl
    (0xFFFFFC1F, 0xD63F0000),   # blr
    (0xFFFFFC1F, 0xD65F0000),   # ret
    (0xFFFFFC1F, 0xD61F0000),   # br
    (0xFFE0001F, 0xD4000001),   # svc
    (0xFFE00000, 0xD4200000),   # brk
)
FLAG_SCAN_LIMIT = 64            # instructions; past that the site counts as fine


def flags_warning(word_at, off, lo, hi):
    """Why a midhook at main offset `off` may start a continuation with stale flags, or None. Follows the code from the site
    (word_at(offset) = instruction word, code in [lo, hi)) until the flags are set again (fine), read (a note), or the path
    leaves what can be followed (fine)."""
    at, seen = off, set()
    for _ in range(FLAG_SCAN_LIMIT):
        if not lo <= at < hi or at in seen:
            return None
        seen.add(at)
        w = word_at(at)
        reads = insn_kind(w, FLAG_READERS)
        if reads:                                   # checked first: ccmp / adcs read the flags before they set them
            if at == off:
                return 'its instruction reads the condition flags (%s)' % reads
            return 'the condition flags are read at 0x%x (%s) before they are set again' % (at + MODULES[0][2], reads)
        if insn_kind(w, FLAG_SETTERS):
            return None
        if any(w & m == v for m, v in FLAG_SCAN_STOP):
            return None
        if w & 0xFC000000 == 0x14000000:            # b: follow it
            imm = w & 0x3FFFFFF
            at += (imm - (1 << 26) if imm & (1 << 25) else imm) * 4
            continue
        at += 4                                     # anything else, and cbz / tbz falling through
    return None


def write_sites(main_path, mod_sites, build):
    """The site list main is lifted with: main.sites itself when no mod adds sites, else build/main.sites = main.sites plus the
    sites of the mods, each checked to lie in main's code."""
    base = os.path.join(RECOMP, *SITES.split('/'))
    if not mod_sites:
        return base
    with open(main_path, 'rb') as f:
        data = f.read()
    text_file, text_off, text_size = (int.from_bytes(data[o:o + 4], 'little') for o in (0x10, 0x14, 0x18))
    word_at = lambda off: int.from_bytes(data[text_file + off - text_off:text_file + off - text_off + 4], 'little')
    own = open(base, encoding='utf-8').read()
    have = set()
    for line in own.splitlines():
        t = line.split('#', 1)[0].split()
        if len(t) >= 2:
            have.add((t[0], int(t[1], 16)))
    lines = []
    for where, kind, addr in mod_sites:
        off = addr - MODULES[0][2]
        if not text_off <= off < text_off + text_size:
            raise Fail("%s: 0x%x is outside main's code" % (where, addr))
        if kind == 'midhook':
            why = flags_warning(word_at, off, text_off, text_off + text_size)
            if why:
                progress('verify', 1, 1, 'Note: %s: midhook 0x%x: %s. A hook there that runs the rest of the function itself '
                         '(call_guest at its site) would start it with stale flags; one that returns 0 is fine '
                         '(runtime/include/nxrt/hooks.h)' % (where, addr, why))
        if (kind, addr) not in have:
            have.add((kind, addr))
            lines.append('%s 0x%x   # %s' % (kind, addr, where))
    out = os.path.join(build, 'main.sites')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(own.rstrip('\n') + '\n\n# ---- added by mods (exefs/*.sites)\n' + ''.join(l + '\n' for l in lines))
    progress('verify', 1, 1, 'Hook sites from mods: %d (%s)' % (len(lines), ', '.join(sorted({w.rsplit(':', 1)[0] for w, _, _ in mod_sites}))))
    return out


def mod_sites_lifted(game, mod_sites, build_id):
    """Whether ObeliskChamber.sites (the list main was lifted with, write_lifted_sites) is for this main and has every site the
    installed mods add. Mod sites only add a call into the runtime at an instruction lifted as before, and a site no hook is
    attached to passes through, so a build with more sites than the mods need is current: removing or switching off a mod with
    .sites does not lift again (ranbinrang-rgb, fefw-recomp #88)."""
    try:
        text = open(os.path.join(game, 'ObeliskChamber.sites'), encoding='utf-8').read()
    except OSError:
        return False
    have, bid = set(), None
    for line in text.splitlines():
        t = line.split('#', 1)[0].split()
        if len(t) == 2 and t[0].lower() == '@nsobid':
            bid = t[1]
        elif len(t) >= 2:
            try:
                have.add((t[0], int(t[1], 16)))
            except ValueError:
                pass
    if bid is None or not bid_matches(bid, build_id):
        return False
    return all((kind, addr) in have for _, kind, addr in mod_sites)


def write_lifted_sites(game, sites, build_id):
    """<game>/ObeliskChamber.sites: the site list main was lifted with (main.sites plus the mods' sites) under an @nsobid line, read by
    the runtime to check hooks and the sites of installed mods against this build (runtime/src/services/mods_dll.cpp)."""
    dst = os.path.join(game, 'ObeliskChamber.sites')
    tmp = dst + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('# The hook sites ObeliskChamber.exe was built with (written by Set up)\n@nsobid %s\n' % build_id)
        f.write(open(sites, encoding='utf-8').read())
    os.replace(tmp, dst)


def popen(cmd, **kw):
    if WINDOWS:
        kw['creationflags'] = CREATE_NO_WINDOW | BELOW_NORMAL
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)


class Runner:
    """Runs tool processes; kills them all when the cancel file appears."""

    def __init__(self, cancel_file):
        self.cancel_file = cancel_file
        self.procs = set()
        self.lock = threading.Lock()
        self.cancelled = False
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        while True:
            if os.path.exists(self.cancel_file):
                self.cancelled = True
                with self.lock:
                    for p in list(self.procs):
                        try:
                            p.kill()
                        except OSError:
                            pass
            time.sleep(0.3)

    def check(self):
        if self.cancelled or os.path.exists(self.cancel_file):
            self.cancelled = True
            raise Cancelled()

    def run(self, cmd, env=None, cwd=None, on_line=None):
        self.check()
        p = popen(cmd, env=env, cwd=cwd)
        with self.lock:
            self.procs.add(p)
        tail = []
        for raw in p.stdout:
            line = raw.decode('utf-8', 'replace').rstrip()
            tail.append(line)
            del tail[:-30]
            if on_line:
                on_line(line)
        rc = p.wait()
        with self.lock:
            self.procs.discard(p)
        self.check()
        return rc, '\n'.join(tail)


def write_plain_ranges(path, patches, applied):
    """The bytes the applied mods patched in main, as merged word-aligned `lo hi` memory offsets (hex), for the lifter's --plain: the
    functions holding them are lifted without the flags and tregs optimizations (recomp/nxrecomp/program.py Env.plain), because
    hand-written mod code need not keep the AAPCS64 those assume (a stub reached by BL that takes x9..x11 or returns in w16, a loop
    split over two stubs with its flags live across the B). -> the path, or None when no mod patched main (the file is removed)."""
    rs = sorted((off & ~3, (off + len(b) + 3) & ~3) for name, _, mem in patches if name in applied for off, b in mem if b)
    merged = []
    for lo, hi in rs:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    if not merged:
        if os.path.isfile(path):
            os.remove(path)
        return None
    with open(path, 'w') as f:
        f.write('# bytes the installed mods patched in main (memory offsets): lifted without flags / tregs\n')
        f.writelines('%x %x\n' % (lo, hi) for lo, hi in merged)
    return path


def lift(runner, exefs, gen, jobs, sites, plain_ranges=None):
    """One lifter process per module (the lifter itself runs `jobs` worker processes); main with the site list `sites` and the ranges
    the mods patched (`plain_ranges`, write_plain_ranges)."""
    for k, (name, nso, base, index, shards) in enumerate(MODULES):
        out = os.path.join(gen, name)
        marker = os.path.join(out, '.lifted')
        if os.path.isfile(marker):
            continue
        progress('lift', k, len(MODULES), 'Translating the game code: %s (%d of %d)' % (name, k + 1, len(MODULES)))
        if os.path.isdir(out):
            shutil.rmtree(out)
        os.makedirs(out)
        args = ['lift', os.path.join(exefs, nso), out, '--name', name, '--base', hex(base), '--shards', str(module_shards(name, shards)),
                '--jobs', str(jobs), '--game', 'fefw', '--index', str(index)]
        if name == 'main':
            args += ['--sites', sites]
            if plain_ranges:
                args += ['--plain', plain_ranges]
        cmd = [sys.executable, '-I', os.path.abspath(__file__), '--lift-one', json.dumps(args)]
        rc, tail = runner.run(cmd, cwd=RECOMP)
        if rc != 0:
            raise Fail('Translating %s failed (exit %d):\n%s' % (name, rc, tail))
        open(marker, 'w').write('ok\n')
    progress('lift', len(MODULES), len(MODULES), 'Game code translated')


def free_memory():
    """Available physical memory in bytes, or None."""
    if not WINDOWS:
        try:
            with open('/proc/meminfo') as f:
                for line in f:
                    if line.startswith('MemAvailable:'):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        return None
    try:
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [('len', ctypes.c_uint32), ('load', ctypes.c_uint32)] + [(n, ctypes.c_uint64) for n in
                        ('total', 'avail', 'pagetotal', 'pageavail', 'virttotal', 'virtavail', 'extavail')]
        m = MS(len=ctypes.sizeof(MS))
        return m.avail if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)) else None
    except (AttributeError, OSError):
        return None


def pgo_gen_skip(src):
    """--pgo-gen: units built without instrumentation. clang's instrumented compile of the unit holding the lifted original of the
    game's zstd decoder (the NX_ORIG fallback of nx_hle_zstd_multiframe, one huge function) ran for over 45 CPU-minutes without
    finishing (perf/codegen, 13 Nov); that code runs only when the native decoder declines, so it needs no profile."""
    try:
        with open(src, encoding='utf-8', errors='replace') as f:
            return 'nx_hle_zstd_multiframe' in f.read()
    except OSError:
        return False


# The objects of the lifted code are kept in <game>/build/objcache/<module> from one build to the next, as
# <unit>.<object_key>.<hash of the object>.o: most releases that make Set up build again change few of the generated files, or none.
def compile_inputs(runner, env, variant):
    """What every object depends on besides its module's generated files: Zig, the flags and the shared headers."""
    rc, version = runner.run([ZIG, 'version'], env=env)
    if rc != 0:
        raise Fail('The bundled compiler does not run (exit %d):\n%s' % (rc, version))
    return [version, cxxflags(variant), tree_hash(RECOMP, ['include'])]


def object_key(shared, headers, src):
    """The name part that says what an object was built from: shared (compile_inputs), the module's generated headers (every
    file the lifter wrote there that is not a unit; module_headers) and the unit's source."""
    return hashlib.sha256(json.dumps([shared, headers, sha256(src)]).encode()).hexdigest()[:16]


def module_headers(d):
    return [(f, sha256(os.path.join(d, f))) for f in sorted(os.listdir(d)) if not f.endswith('.cpp') and f != '.lifted']


def cached_objects(d):
    """{(unit, key): path} of the objects in d that are still what was written (a power loss can leave a renamed file short or
    zeroed); the rest, and the half-written files of a stopped build, are removed."""
    found = {}
    for f in os.listdir(d):
        path = os.path.join(d, f)
        parts = f.split('.')
        try:
            if len(parts) == 4 and parts[3] == 'o' and sha256(path)[:12] == parts[2]:
                found[parts[0], parts[1]] = path
            else:
                os.remove(path)
        except OSError:                  # held open (an antivirus): not used this time
            pass
    return found


def prune_objects(cache, objects):
    """After a successful link: remove the objects this build did not use."""
    keep = {os.path.normcase(p) for paths in objects.values() for p in paths}
    for name, _, _, _, _ in MODULES:
        d = os.path.join(cache, name)
        for f in os.listdir(d):
            path = os.path.join(d, f)
            if os.path.normcase(path) not in keep:
                try:
                    os.remove(path)
                except OSError:          # held open (an antivirus): the next build removes it
                    pass


def compile_all(runner, gen, cache, jobs, env, variant):
    """Compile every unit that has no object in the cache yet -> {module: [object paths]} for link."""
    units = []
    for name, _, _, _, shards in MODULES:
        d = os.path.join(gen, name)
        for f in sorted(os.listdir(d)):
            if f.endswith('.cpp'):
                units.append((name, os.path.join(d, f)))
    total = len(units)
    shared = compile_inputs(runner, env, variant)
    headers, cached = {}, {}
    for name, _, _, _, _ in MODULES:
        d = os.path.join(cache, name)
        os.makedirs(d, exist_ok=True)
        cached[name] = cached_objects(d)
        headers[name] = module_headers(os.path.join(gen, name))
    objects = {name: [] for name, _, _, _, _ in MODULES}
    todo = []
    for name, src in units:
        unit = os.path.basename(src)[:-4]
        key = object_key(shared, headers[name], src)
        if (unit, key) in cached[name]:
            objects[name].append(cached[name][unit, key])
        else:
            todo.append((name, src, os.path.join(cache, name, '%s.%s' % (unit, key))))
    done = total - len(todo)
    if done:
        print('%d of %d units are unchanged since an earlier build: their objects are reused' % (done, total), flush=True)
    progress('compile', done, total, 'Compiling: %d of %d' % (done, total))
    # biggest first, so the long ones do not end up last on a single core
    todo.sort(key=lambda t: -os.path.getsize(t[1]))

    def one(t):
        name, src, stem = t
        tmp = stem + '.o.tmp'
        cmd = [ZIG, 'c++'] + cxxflags(variant) + ['-I', os.path.join(RECOMP, 'include'), '-I', os.path.dirname(src), '-c', src, '-o', tmp]
        if PGO['gen'] and pgo_gen_skip(src):     # see pgo_gen_skip
            cmd = [x for x in cmd if x not in ('-fprofile-generate',)]
        rc, tail = runner.run(cmd, env=env)
        if rc != 0:
            return 'Compiling %s/%s failed (exit %d):\n%s' % (name, os.path.basename(src), rc, tail)
        try:
            o = '%s.%s.o' % (stem, sha256(tmp)[:12])
            replace_retry(tmp, o)
            objects[name].append(o)
        except OSError as e:     # a failed unit, retried one at a time below, not an abort of the whole build
            return 'Compiling %s/%s: %s' % (name, os.path.basename(src), e)
        return None

    # a big unit takes a GB or two in clang; with too many at once memory runs out and clang dies on every unit with
    # exit 541541187 (0x20474343: an uncaught C++ exception, std::bad_alloc)
    # ponytail: 2 GiB per job is a guess from the unit sizes; measure if machines with plenty of RAM get capped
    # The free memory is looked at again before every start, not once at the beginning: a player who closes a browser while the
    # build runs gets the extra jobs, and one who opens something big gets fewer new ones (running jobs are never stopped).
    # At most one job is started per second, because a job that just started has not taken its memory yet.
    per_job = int(TUNE['job_gib'] * (1 << 30))   # build_options.json job_gib (the release: 2)
    failed = []
    queue = list(todo)
    limited = False                      # the last start was held back by low memory
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        pending = {}
        try:
            while queue or pending:
                if queue and len(pending) < jobs:
                    avail = free_memory()
                    held = bool(pending) and avail is not None and avail < per_job    # one job always runs, whatever the memory
                    if not held:
                        t = queue.pop(0)
                        pending[ex.submit(one, t)] = t
                    if held != limited:
                        limited = held
                        progress('compile', done, total, 'Compiling: %d of %d%s' % (done, total, ' (free memory is low, %d at a time)' % len(pending) if limited else ''))
                # wake up soon to look at the memory again while jobs are held back or can still be added
                room = bool(queue) and len(pending) < jobs
                fin, _ = wait(pending, timeout=1.0 if room else None, return_when=FIRST_COMPLETED)
                for f in fin:
                    t = pending.pop(f)
                    if f.result():
                        failed.append(t)
                        continue
                    done += 1
                    progress('compile', done, total, 'Compiling: %d of %d%s' % (done, total, ' (free memory is low, %d at a time)' % max(1, len(pending)) if limited else ''))
        except BaseException:
            for f in pending:
                f.cancel()
            raise
    for t in failed:                     # again one at a time with all the memory: only a failure here is real
        err = one(t)
        if err:
            raise Fail(err)
        done += 1
        progress('compile', done, total, 'Compiling: %d of %d' % (done, total))
    return objects


def replace_retry(src, dst):
    """os.replace, retried for a few seconds: an antivirus scanning a freshly written file holds it open (PermissionError)."""
    delay = 0.1
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if delay > 3.2:
                raise
            time.sleep(delay)
            delay *= 2


def clear_high_entropy_va(exe):
    """ObeliskChamber.exe keeps host allocations low (the guest ranges at 0x71_0000_0000 must stay free): no high-entropy ASLR,
    as /HIGHENTROPYVA:NO does for the clang-cl build. zig's linker has no flag for it, so clear the PE bit."""
    with open(exe, 'r+b') as f:
        f.seek(0x3c)
        pe = int.from_bytes(f.read(4), 'little')
        f.seek(pe + 24)
        if f.read(2) != b'\x0b\x02':
            raise Fail('ObeliskChamber.exe is not a PE32+ image')
        off = pe + 24 + 70
        f.seek(off)
        dc = int.from_bytes(f.read(2), 'little') & ~0x20
        f.seek(off)
        f.write(dc.to_bytes(2, 'little'))


def link(runner, objects, obj, game, env, variant):
    """ObeliskChamber.exe from the lifted code's objects (compile_all) and the SDK; the archives and other objects go to obj."""
    progress('link', 0, 1, 'Linking ObeliskChamber.exe')
    os.makedirs(obj, exist_ok=True)
    libs = []
    for name, _, _, _, _ in MODULES:
        a = os.path.join(obj, 'fefw_%s.a' % name)
        if os.path.isfile(a):
            os.remove(a)
        objs = sorted(objects[name])
        rsp = os.path.join(obj, '%s.rsp' % name)
        # UTF-8, not the ANSI code page: zig ar reads the response file as UTF-8, so a game folder path with non-ASCII
        # characters (an accented user name) failed the archive step.
        with open(rsp, 'w', encoding='utf-8') as f:
            f.write('\n'.join('"%s"' % p.replace('\\', '/') for p in objs))
        rc, tail = runner.run([ZIG, 'ar', 'rcs', a, '@' + rsp], env=env)
        if rc != 0:
            raise Fail('Archiving %s failed (exit %d):\n%s' % (name, rc, tail))
        libs.append(a)
    # the variant's own sources (compat: sdk/fefw_fma.c), as C and ahead of the libraries, so their fma / fmaf are the ones linked
    extra = []
    # the native routines (sdk/fefw_hle.cpp) and the zstd decoder they use
    zdir = os.path.join(SDK, 'zstd')
    hle = [(os.path.join(SDK, 'fefw_hle.cpp'), 'c++')] + [(os.path.join(zdir, sub, f), 'cc') for sub in ('common', 'decompress')
                                                       for f in sorted(os.listdir(os.path.join(zdir, sub))) if f.endswith('.c')]
    for src, lang in hle:
        o = os.path.join(obj, 'hle_' + os.path.basename(src) + '.o')
        cmd = [ZIG, lang, '-target', 'x86_64-windows-gnu', '-mcpu=' + CPU_VARIANTS[variant]['mcpu'], '-O2', '-g0', '-w', '-DZSTD_DISABLE_ASM',
               '-DZSTD_LEGACY_SUPPORT=0', '-I', zdir, '-I', os.path.join(RECOMP, 'include')]
        if lang == 'c++':
            cmd += ['-std=c++20', '-fno-exceptions', '-fno-rtti']
            if variant == 'compat' and TUNE['fast_fma'] and not TUNE['cpu']:
                cmd += ['-DNX_FAST_FMA']   # fefw_hle.cpp's culling tests: fused multiply-adds as the lifted code of this build does them
        rc, tail = runner.run(cmd + ['-c', src, '-o', o], env=env)
        if rc != 0:
            raise Fail('Compiling %s failed (exit %d):\n%s' % (os.path.basename(src), rc, tail))
        extra.append(o)
    for src in CPU_VARIANTS[variant]['link']:
        o = os.path.join(obj, os.path.splitext(src)[0] + '.o')
        rc, tail = runner.run([ZIG, 'cc', '-target', 'x86_64-windows-gnu', '-mcpu=' + mcpu(variant), '-O1', '-ffp-contract=off',
                               '-fno-math-errno', '-c', os.path.join(SDK, src), '-o', o], env=env)
        if rc != 0:
            raise Fail('Compiling %s failed (exit %d):\n%s' % (src, rc, tail))
        extra.append(o)
    tmp = os.path.join(obj, 'ObeliskChamber.exe')
    # FEFW_CPU_NEEDS: what fefw_rt.dll checks the processor for before the game starts (fefw_rt_api.h)
    cmd = ([ZIG, 'c++', '-target', 'x86_64-windows-gnu', '-mcpu=x86_64+cx16', '-O1', '-fno-exceptions', '-fno-rtti', '-I', SDK,
            '-DFEFW_CPU_NEEDS=' + CPU_VARIANTS[variant]['needs']] + (['-DFEFW_PGO_GEN'] if PGO['gen'] else []) +
           [os.path.join(SDK, 'fefw_stub.cpp'), os.path.join(SDK, 'fefw.rc')] + extra + libs + ([PGO['gen']] if PGO['gen'] else []) +
           [os.path.join(SDK, 'fefw_rt.lib'), '-o', tmp, '-Wl,--stack,134217728', '-Wl,--subsystem,windows'])
    rc, tail = runner.run(cmd, env=env, cwd=SDK)
    if rc != 0:
        raise Fail('Linking ObeliskChamber.exe failed (exit %d):\n%s' % (rc, tail))
    clear_high_entropy_va(tmp)
    dst = os.path.join(game, 'ObeliskChamber.exe')
    replace_retry(tmp, dst)
    progress('link', 1, 1, 'ObeliskChamber.exe built')
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exefs')
    ap.add_argument('--game')
    ap.add_argument('--jobs', type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument('--keep', action='store_true', help='keep the generated sources and all build files')
    ap.add_argument('--check', action='store_true', help='only say whether <game>/build/done.json matches these tools')
    ap.add_argument('--cpu', default='auto', help='the build for the processor: fma, compat or auto (CPU_VARIANTS)')
    ap.add_argument('--pgo-gen', help='developer: build instrumented for a profile, linking this profile runtime library (.a)')
    ap.add_argument('--pgo-use', help='developer: build with this profile (.profdata)')
    ap.add_argument('--mcpu', help='developer: -mcpu for the lifted code instead of the variant one (e.g. znver2: this PC only)')
    ap.add_argument('--lift-one', help=argparse.SUPPRESS)
    a = ap.parse_args()
    PGO['gen'] = os.path.abspath(a.pgo_gen) if a.pgo_gen else None
    PGO['use'] = os.path.abspath(a.pgo_use) if a.pgo_use else None
    PGO['mcpu'] = a.mcpu
    if a.lift_one:                       # child process: one lifter run (keeps the lifter's own pool out of this process)
        sys.path.insert(0, RECOMP)
        from nxrecomp.__main__ import main as nx_main
        return nx_main(json.loads(a.lift_one)) or 0
    if a.check:
        if not a.game:
            ap.error('--check needs --game')
        variant, why = pick_variant(a.cpu)
        try:
            current, reason = check(os.path.abspath(a.game), variant) if variant else (False, why)
        except OSError as e:                 # a tool file that cannot be read: Set up then runs and names it
            current, reason = False, 'cannot read the build tools: %s' % e
        print(json.dumps({'current': current, 'reason': reason}), flush=True)
        return 0 if current else 3
    if not a.exefs or not a.game:
        ap.error('--exefs and --game are required')
    if not WINDOWS:
        try:
            os.nice(10)                  # the BELOW_NORMAL of the Windows build: the lifter and the compilers inherit it
        except OSError:
            pass
    game = os.path.abspath(a.game)
    build = os.path.join(game, 'build')
    os.makedirs(build, exist_ok=True)
    cancel_file = os.path.join(build, 'cancel')
    if os.path.exists(cancel_file):
        os.remove(cancel_file)
    t0 = time.time()
    times = {}
    try:
        # zig reads the compiler's Makefile-style .d files back and chokes on a ':' in a path ("illegal char ... expected_colon"),
        # which fails the link only after the whole compile. Only a Windows drive letter ('C:') may have one.
        if ':' in os.path.splitdrive(game)[1]:
            raise Fail('The game folder path contains a ":" (%s), which the bundled compiler cannot handle. '
                       'Rename the folder so it has no ":" and run Set up again.' % game)
        if not os.path.isfile(ZIG):
            raise Fail('The bundled compiler is missing (%s). Extract the release zip again.' % ZIG)
        variant, why = pick_variant(a.cpu)
        if not variant:
            raise Fail(why)
        if variant == 'compat':
            progress('verify', 0, 1, 'Building the compatible version of the game (for processors without FMA3)')
        load_tune(game)
        want = verify(a.exefs)
        # the NSOs are written, patched and lifted in build/exefs and move to <game>/exefs only once ObeliskChamber.exe is linked: a
        # cancelled or failed build leaves the old ObeliskChamber.exe with the main it was built from (upstream 5cf14f7)
        plain = os.path.join(build, 'exefs')
        write_exefs(a.exefs, plain)
        patches, left_out, unused = collect_exefs_patches(game, want['main_build_id'])
        for name in sorted(left_out):
            progress('verify', 0, 1, 'Note: %s left out: %s' % (name, left_out[name]))
        mod_sites = collect_mod_sites(game, want['main_build_id'])
        applied, refused = apply_exefs_patches(os.path.join(plain, 'main'), patches, mod_sites)
        left_out_all = dict(left_out, **refused)
        plain_ranges = write_plain_ranges(os.path.join(build, 'mod_patched.txt'), patches, applied)
        sites = write_sites(os.path.join(plain, 'main'), mod_sites, build)
        stamp = {'format': FORMAT, 'main_build_id': want['main_build_id'], 'sha256': want['sha256'],
                 'tools': tool_stamp(variant, exefs_patch_stamp(patches, left_out), mod_sites_stamp(mod_sites))}
        state_p = os.path.join(build, 'state.json')
        # another dump or another release (or no readable state): start over. Removing lift/ drops the modules' .lifted
        # markers too, so a changed lifter or main.sites lifts everything again, not only recompiles
        if read_json(state_p) != stamp:
            for d in ('lift', 'obj'):
                shutil.rmtree(os.path.join(build, d), ignore_errors=True)
            write_json(state_p, stamp)
        times['verify'] = round(time.time() - t0, 1)
        runner = Runner(cancel_file)
        env = dict(os.environ)
        env['ZIG_GLOBAL_CACHE_DIR'] = os.path.join(build, 'zig-cache')
        env['ZIG_LOCAL_CACHE_DIR'] = os.path.join(build, 'zig-cache')
        gen, obj = os.path.join(build, 'lift'), os.path.join(build, 'obj')
        t = time.time()
        lift(runner, plain, gen, a.jobs, sites, plain_ranges)
        times['lift'] = round(time.time() - t, 1)
        t = time.time()
        # a profile-guided build's objects are not kept: the key does not cover the profile's contents
        cache = os.path.join(obj if PGO['gen'] or PGO['use'] else build, 'objcache')
        objects = compile_all(runner, gen, cache, a.jobs, env, variant)
        times['compile'] = round(time.time() - t, 1)
        t = time.time()
        exe = link(runner, objects, obj, game, env, variant)
        prune_objects(cache, objects)
        live = os.path.join(game, 'exefs')
        os.makedirs(live, exist_ok=True)
        for n in EXEFS_FILES:
            if os.path.isfile(os.path.join(plain, n)):
                replace_retry(os.path.join(plain, n), os.path.join(live, n))
        write_exefs_record(live, applied, left_out_all, unused)
        write_lifted_sites(game, sites, want['main_build_id'])
        times['link'] = round(time.time() - t, 1)
        if not a.keep:
            progress('finish', 0, 1, 'Removing the build files')
            for d in ('lift', 'obj', 'zig-cache'):
                shutil.rmtree(os.path.join(build, d), ignore_errors=True)
        done = dict(stamp, exe_sha256=sha256(exe), seconds=times, finished=time.strftime('%Y-%m-%d %H:%M:%S'))
        write_json(os.path.join(build, 'done.json'), done)
        progress('finish', 1, 1, 'ObeliskChamber.exe is ready (%d s)' % (time.time() - t0))
        return 0
    except Cancelled:
        say('cancelled', 'Build paused. Set up again continues where it stopped.')
        return 2
    except Fail as e:
        say('error', str(e).replace('\n', '\\n'))
        return 1
    except Exception as e:            # an unexpected failure: say what it was, plainly
        say('error', '%s: %s' % (type(e).__name__, str(e).replace('\n', '\\n')))
        return 1


if __name__ == '__main__':
    sys.exit(main())
