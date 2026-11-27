#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every function or variable the recomp headers declare with C linkage and leave to someone else to define must be provided at the
player's link of fefw.exe: exported by fefw_rt.dll (game-build/fefw/src/fefw_rt.def) or defined in game-build/fefw/src/fefw_hle.cpp,
which is compiled with the lifted libraries. A missing one makes Set up fail with an undefined symbol on the player's PC, which the
runtime-only CI build never links. Also checks that recomp/games/fefw/link_check.cpp stubs each of them.

Uses clang's JSON AST dump of a translation unit that includes every header in recomp/include/nxrecomp, so macros and #if are honoured.
Usage: python tools/ci/check_exports.py [--clang clang++]   (exit 1 with a list when something is missing)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INC = os.path.join(ROOT, 'recomp', 'include')
HDR_DIR = os.path.join(INC, 'nxrecomp')
DEF = os.path.join(ROOT, 'game-build', 'fefw', 'src', 'fefw_rt.def')
HLE = os.path.join(ROOT, 'game-build', 'fefw', 'src', 'fefw_hle.cpp')
LINK_CHECK = os.path.join(ROOT, 'recomp', 'games', 'fefw', 'link_check.cpp')


# The macro sets the game is built with (game-build/release/fefw_build.py, recomp/games/fefw/CMakeLists.txt): a declaration behind
# #ifdef (nx_fn_flat under NX_INLINE_INDIRECT) only shows when its macro is set, so the union over these sets is checked.
DEFINE_SETS = [
    [],
    ['NX_INLINE_INDIRECT', 'NX_NOESCAPE_CTX', 'NX_FAST_FMA'],
]
TARGET_FLAGS = ['-mfma', '-mcx16', '-mpopcnt']   # the release 'fma' variant's features, plus popcnt (as 'compat')


def declared(clang):
    found = {}
    for defs in DEFINE_SETS:
        found.update(declared_with(clang, defs))
    return found


def declared_with(clang, defs):
    headers = sorted(h for h in os.listdir(HDR_DIR) if h.endswith('.h'))
    with tempfile.NamedTemporaryFile('w', suffix='.cpp', delete=False) as f:
        for h in headers:
            f.write(f'#include <nxrecomp/{h}>\n')
        tu = f.name
    try:
        out = subprocess.run([clang, '-std=c++20', '-fsyntax-only', '-w', f'-I{INC}', *TARGET_FLAGS, *(f'-D{d}' for d in defs),
                              '-Xclang', '-ast-dump=json', tu],
                             check=True, capture_output=True, text=True).stdout
    finally:
        os.unlink(tu)
    ast = json.loads(out)
    found = {}
    cur = {'file': None}

    def note_file(n):
        for key in ('loc', 'range'):
            loc = n.get(key) or {}
            if key == 'range':
                loc = loc.get('begin') or {}
            for l in (loc, loc.get('spellingLoc') or {}, loc.get('expansionLoc') or {}):
                if 'file' in l:
                    cur['file'] = l['file']

    def in_headers():
        p = cur['file']
        return p is not None and os.path.abspath(os.path.join(ROOT, p) if not os.path.isabs(p) else p).startswith(HDR_DIR + os.sep)

    def walk(n, c_linkage, braces=False):
        note_file(n)
        kind = n.get('kind')
        if kind == 'LinkageSpecDecl':
            for ch in n.get('inner', []):
                walk(ch, n.get('language') == 'C', n.get('hasBraces', False))
            return
        if kind == 'FunctionDecl' and in_headers():
            has_body = any(ch.get('kind') == 'CompoundStmt' for ch in n.get('inner', []))
            # implicit: compiler builtins (__atomic_*, __builtin_*) clang declares where they are first used
            if c_linkage and not has_body and not n.get('isImplicit') and not n.get('inline') and n.get('storageClass') != 'static':
                found[n['name']] = 'function'
        elif kind == 'VarDecl' and in_headers():
            # `extern "C" T x;` (no braces) is a declaration too: clang gives it no storage class, and no initialiser
            # (inside `extern "C" { ... }` a variable without `extern` is a definition, not a declaration)
            if c_linkage and (n.get('storageClass') == 'extern' or (n.get('storageClass') is None and not braces)) and 'init' not in n \
                    and not n.get('isImplicit'):
                found[n['name']] = 'data'
        for ch in n.get('inner', []) if kind in ('TranslationUnitDecl', 'NamespaceDecl') else []:
            walk(ch, c_linkage)
        # other nodes: still track the file their children report, so later siblings get the right one
        if kind not in ('TranslationUnitDecl', 'NamespaceDecl'):
            stack = list(n.get('inner', []))
            while stack:
                ch = stack.pop(0)
                note_file(ch)
                stack[0:0] = ch.get('inner', [])

    walk(ast, False)
    return found


def def_exports():
    names = {}
    in_exports = False
    for line in open(DEF, encoding='utf-8'):
        line = line.split(';', 1)[0].strip()
        if not line:
            continue
        if line.upper() == 'EXPORTS':
            in_exports = True
            continue
        if in_exports:
            parts = line.split()
            names[parts[0]] = 'data' if 'DATA' in (p.upper() for p in parts[1:]) else 'function'
    return names


def defined_in(path, name):
    text = open(path, encoding='utf-8').read()
    return re.search(r'\b' + re.escape(name) + r'\s*(\(|=|;|\s)', text) is not None and re.search(
        r'(^|\n)[^\n;]*\b' + re.escape(name) + r'\s*\([^;{]*\)\s*\{', text) is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--clang', default=os.environ.get('CLANGXX', 'clang++'))
    a = ap.parse_args()
    decl = declared(a.clang)
    exports = def_exports()
    link_check = open(LINK_CHECK, encoding='utf-8').read()
    bad = []
    for name, kind in sorted(decl.items()):
        where = None
        if name in exports:
            where = 'fefw_rt.def'
            if exports[name] != kind:
                bad.append(f'{name}: declared as {kind} in recomp/include but exported as {exports[name]} in fefw_rt.def')
        elif kind == 'function' and defined_in(HLE, name):
            where = 'fefw_hle.cpp'
        else:
            bad.append(f'{name} ({kind}): declared in recomp/include with C linkage, but neither exported by fefw_rt.def nor defined in fefw_hle.cpp')
        if not re.search(r'\b' + re.escape(name) + r'\b', link_check):
            bad.append(f'{name}: no stub in recomp/games/fefw/link_check.cpp')
        print(f'  {name:28s} {kind:8s} {where or "MISSING"}')
    print(f'{len(decl)} C-linkage declarations in recomp/include/nxrecomp; {len(exports)} exports in fefw_rt.def')
    if not decl:
        bad.append('no declarations found: the AST walk is broken')
    for b in bad:
        print('error: ' + b, file=sys.stderr)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
