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

    helpers = set()

    def repl(m):
        s = m.group('s')
        # S = T; T = _60(VV, lane, c, out) with lane = c = 31; VV = V   (the last lane's shuffle of the reduced value)
        t = defs.get(s, '')
        call = defs.get(t.strip(), '')
        mm = re.fullmatch(r'(_\d+)\((_\d+), (_\d+), (_\d+), (_\d+)\)', call.strip())
        if not mm:
            raise SystemExit(f'unexpected shape at {s}: {t!r} -> {call!r}')
        if defs.get(mm.group(3), '').strip() != '31' or defs.get(mm.group(4), '').strip() != '31':
            raise SystemExit(f'{s}: not the shuffle of lane 31 ({mm.group(3)}, {mm.group(4)})')
        helpers.add(mm.group(1))
        if len(helpers) != 1:
            raise SystemExit(f'more than one shuffle helper: {sorted(helpers)}')
        v = defs.get(mm.group(2), '').strip()
        if not re.fullmatch(r'_\d+', v):
            raise SystemExit(f'unexpected value at {mm.group(2)}: {v!r}')
        r = f'red_or_{len(found)}'
        found.append((s, v, r, m.group('m')))
        return f'atomicOr({m.group("dst")}, uint({r}))'

    out = ATOMIC.sub(repl, glsl)
    if not found:
        raise SystemExit('no reduction found')
    # The OR must run where the shuffles ran, by every lane the ballot counted, not inside the `if` that lets one lane do the atomic:
    # it goes right after the last shuffle's result (S), which is in the same block as the ballot.
    for s, v, r, ballot in found:
        line = re.compile(r'^(?P<ind>\s*)int ' + s + r' = [^;]+;$', re.M)
        if len(line.findall(out)) != 1:
            raise SystemExit(f'{s}: not exactly one definition')
        # S must sit after the lane mask (from the ballot) and at the same brace depth, so every counted lane runs the OR
        bm = re.search(r'^\s*int ' + ballot + r' = [^;]+;', out, re.M)
        sm = line.search(out)
        if not bm or sm.start() < bm.start() or not same_block(out, bm.start(), sm.start()):
            raise SystemExit(f'{s}: not in the block of the ballot {ballot}')
        out = line.sub(lambda m: m.group(0) + f'\n{m.group("ind")}int {r} = subgroupOr({v});', out, count=1)
    ext = '#extension GL_KHR_shader_subgroup_ballot : require'
    if ext not in out:
        raise SystemExit('no GL_KHR_shader_subgroup_ballot extension line')
    out = out.replace(ext, ext + '\n#extension GL_KHR_shader_subgroup_arithmetic : require', 1)
    left = len(re.findall(r'\batomicOr\([^;]*findLSB', out))
    if left:
        print(f'warning: {left} OR reduction(s) of another shape left as translated', file=sys.stderr)
    out = drop_dead(out, helpers.pop())
    return out, len(found)


def same_block(text, i, j):
    """True when position j is in the same brace block as position i (no brace closes that block between them, and the depth is equal)."""
    d = low = 0
    for ch in text[i:j]:
        if ch == '{':
            d += 1
        elif ch == '}':
            d -= 1
            low = min(low, d)
    return d == 0 and low == 0


DECL = re.compile(r'^\s*(?:int|uint|float|bool|[iu]?vec[234]) (_\d+)(?: = (.*))?;\s*$')
SIDE_EFFECTS = re.compile(r'atomic|image(Store|Atomic)|barrier|memoryBarrier|EmitVertex|(?<![=!<>])=(?!=)|\+\+|--')
PURE_CALLS = {'int', 'uint', 'float', 'bool', 'ivec2', 'ivec3', 'ivec4', 'uvec2', 'uvec3', 'uvec4', 'vec2', 'vec3', 'vec4',
              'findLSB', 'findMSB', 'bitCount', 'bitfieldExtract', 'subgroupShuffle', 'subgroupBallot', 'subgroupOr'}


def drop_dead(glsl, helper):
    """Removes local declarations no one reads, repeatedly: the shuffle chains whose result subgroupOr replaced. A declaration goes only if
    its initialiser has no side effect and calls nothing but pure built-ins and the shuffle helper; a helper call goes only if its out
    argument (the last one) is read nowhere else, so no reader is left with an unset value."""
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
            init = (m.group(2) or '') if m else ''
            if m and uses.get(m.group(1), 0) == 1 and not SIDE_EFFECTS.search(init) and removable(init, helper, uses):
                removed += 1
                continue
            keep.append(l)
        lines = keep
        if not removed:
            return '\n'.join(lines)


def removable(init, helper, uses):
    for call in re.finditer(r'\b(\w+)\(', init):
        name = call.group(1)
        if name in PURE_CALLS:
            continue
        if name != helper:
            return False
        args = re.fullmatch(r'\s*' + helper + r'\((.*)\)\s*', init)
        out_arg = args.group(1).split(',')[-1].strip() if args else ''
        if not re.fullmatch(r'_\d+', out_arg) or uses.get(out_arg, 0) != 2:   # its declaration and this call
            return False
    return True


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
        g_run = subprocess.run([a.glslang, '-V', '--target-env', 'vulkan1.2', '-o', raw, e], capture_output=True, text=True)
        if g_run.returncode:
            sys.stderr.write(g_run.stdout + g_run.stderr)
            raise SystemExit(f'glslang failed ({g_run.returncode})')
        if a.adce:
            subprocess.run([a.adce, '--eliminate-dead-code-aggressive', raw, '-o', a.out], check=True)
        else:
            os.replace(raw, a.out) if os.path.dirname(os.path.abspath(raw)) == os.path.dirname(os.path.abspath(a.out)) else open(a.out, 'wb').write(open(raw, 'rb').read())
    print(f'{n} reductions replaced by subgroupOr -> {a.out}')


if __name__ == '__main__':
    main()
