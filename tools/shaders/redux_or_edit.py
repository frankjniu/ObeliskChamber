#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Measurement tool: replace the 32-lane OR reductions of a translated compute shader by subgroupOr.

Our Ampere front end (tools/shaders/ampere/frontend/AmpereEmit.cs, EmitReduxAmpere) turns the game's REDUX.OR into a shuffle of every
lane (33 calls of the shuffle helper) and an OR of the lanes the ballot marks active. With 32-lane subgroups that is exactly
subgroupOr over the active invocations. This rewrites each such reduction's use in a spirv-cross decompile to `subgroupOr(v)`, so the
shuffles become dead code, for an A/B through [nvn] shader_replace_dir. It is not for shipping: the result is only right on GPUs whose
subgroups are 32 wide (NVIDIA, and compute on AMD RDNA / Intel when the runtime asks for 32), and the proper home is the translator.

usage: redux_or_edit.py <module.orig.spv> <out.spv> [--spirv-cross exe] [--glslang exe] [--adce spirv-opt] [--keep-glsl dir]
Prints the number of reductions replaced; exits 1 if none are found or the shape is not the expected one.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

ATOMIC = re.compile(
    r'atomicOr\((?P<dst>[^,]+?), uint\(\(\(\((?P<m>_\d+) & int\(0x80000000\)\) != 0\) && \(int\(uint\(findLSB\(uint\((?P=m)\)\)\)\) != 31\)\) '
    r'\? \((?P<a>_\d+) \| (?P<s>_\d+)\) : (?P=a)\)\)')


def edit(glsl):
    defs = dict(re.findall(r'\bint (_\d+) = ([^;]+);', glsl))
    found = []

    def repl(m):
        s = m.group('s')
        # S = T; T = _60(VV, lane, 31, out); VV = V   (the last lane's shuffle of the reduced value)
        t = defs.get(s, '')
        call = defs.get(t.strip(), '')
        mm = re.fullmatch(r'(_\d+)\((_\d+), (_\d+), (_\d+), (_\d+)\)', call.strip())
        if not mm:
            raise SystemExit(f'unexpected shape at {s}: {t!r} -> {call!r}')
        v = defs.get(mm.group(2), '').strip()
        if not re.fullmatch(r'_\d+', v):
            raise SystemExit(f'unexpected value at {mm.group(2)}: {v!r}')
        r = f'red_or_{len(found)}'
        found.append((s, v, r))
        return f'atomicOr({m.group("dst")}, uint({r}))'

    out = ATOMIC.sub(repl, glsl)
    if not found:
        raise SystemExit('no reduction found')
    # The OR must run where the shuffles ran, by every lane the ballot counted, not inside the `if` that lets one lane do the atomic:
    # it goes right after the last shuffle's result (S), which is in the same block as the ballot.
    for s, v, r in found:
        line = re.compile(r'^(?P<ind>\s*)int ' + s + r' = [^;]+;$', re.M)
        hits = line.findall(out)
        if len(re.findall(r'^\s*int ' + s + r' = [^;]+;$', out, re.M)) != 1:
            raise SystemExit(f'{s}: not exactly one definition')
        out = line.sub(lambda m: m.group(0) + f'\n{m.group("ind")}int {r} = subgroupOr({v});', out, count=1)
    out = out.replace('#extension GL_KHR_shader_subgroup_ballot : require',
                      '#extension GL_KHR_shader_subgroup_ballot : require\n#extension GL_KHR_shader_subgroup_arithmetic : require', 1)
    out = drop_dead(out)
    return out, len(found)


DECL = re.compile(r'^\s*(?:int|uint|float|bool|[iu]?vec[234]) (_\d+)(?: = (.*))?;\s*$')
SIDE_EFFECTS = re.compile(r'atomic|image(Store|Atomic)|barrier|memoryBarrier|EmitVertex|(?<![=!<>])=(?!=)|\+\+|--')


def drop_dead(glsl):
    """Removes local declarations no one reads, repeatedly: the shuffle chains whose result subgroupOr replaced. Only a declaration whose
    initialiser has no side effect goes (the shuffle helper's only effect is its out parameter, itself one of these locals)."""
    lines = glsl.split('\n')
    body_start = next(i for i, l in enumerate(lines) if l.startswith('void main()'))
    while True:
        text = '\n'.join(lines)
        uses = {}
        for m in re.finditer(r'\b_\d+\b', text):
            uses[m.group(0)] = uses.get(m.group(0), 0) + 1
        removed = 0
        keep = []
        for i, l in enumerate(lines):
            m = DECL.match(l) if i > body_start else None
            if m and uses.get(m.group(1), 0) == 1 and not SIDE_EFFECTS.search(m.group(2) or ''):
                removed += 1
                continue
            # a call of the helper whose result is unread: `int _a = _60(v, lane, c, out);` counts the out local as a use, so the
            # call goes when its result is unread, and the out local's declaration goes on the next round
            keep.append(l)
        lines = keep
        if not removed:
            return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('out')
    ap.add_argument('--spirv-cross', default='spirv-cross')
    ap.add_argument('--glslang', default='glslang')
    ap.add_argument('--adce', default=None, help='spirv-opt to run --eliminate-dead-code-aggressive only (otherwise glslang output as is)')
    ap.add_argument('--keep-glsl', default=None)
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as td:
        g = os.path.join(td, 'in.comp')
        subprocess.run([a.spirv_cross, a.src, '--vulkan-semantics', '--output', g], check=True)
        text, n = edit(open(g).read())
        e = os.path.join(td, 'edit.comp')
        open(e, 'w').write(text)
        if a.keep_glsl:
            os.makedirs(a.keep_glsl, exist_ok=True)
            open(os.path.join(a.keep_glsl, 'orig.comp'), 'w').write(open(g).read())
            open(os.path.join(a.keep_glsl, 'edit.comp'), 'w').write(text)
        raw = os.path.join(td, 'edit.spv')
        subprocess.run([a.glslang, '-V', '--target-env', 'vulkan1.2', '-o', raw, e], check=True, stdout=subprocess.DEVNULL)
        if a.adce:
            subprocess.run([a.adce, '--eliminate-dead-code-aggressive', raw, '-o', a.out], check=True)
        else:
            os.replace(raw, a.out) if os.path.dirname(os.path.abspath(raw)) == os.path.dirname(os.path.abspath(a.out)) else open(a.out, 'wb').write(open(raw, 'rb').read())
    print(f'{n} reductions replaced by subgroupOr -> {a.out}')


if __name__ == '__main__':
    main()
