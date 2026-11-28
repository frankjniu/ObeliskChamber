# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of Set up's exefs patch handling (fefw_build.py) on a synthetic NSO: patches inside a segment, code caves in the padding
after text, mods left out instead of failing Set up, and the pchtxt rules. No game files needed.

    python -I game-build/release/tests/test_exefs_patches.py
"""
import hashlib
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('fefw_build', os.path.join(HERE, '..', 'fefw_build.py'))
fb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fb)

BID = 'ab' * 20                          # a made-up build ID (the 20 bytes dump_hashes.json keeps)
TEXT = (0x0000, 0x12F0)                  # memory offset, size: the padding after text runs to 0x2000
RO = (0x2000, 0x100)
DATA = (0x3000, 0x80)


def make_nso():
    """A plain NSO: header, then text, rodata and data back to back in the file, each filled with its own byte."""
    segs = [(TEXT, 0x11), (RO, 0x22), (DATA, 0x33)]
    hdr = bytearray(0x100)
    hdr[0:4] = b'NSO0'
    body = bytearray()
    fo = 0x100
    for i, ((mo, sz), fill) in enumerate(segs):
        raw = bytes([fill]) * sz
        hdr[0x10 + 0x10 * i:0x1C + 0x10 * i] = fo.to_bytes(4, 'little') + mo.to_bytes(4, 'little') + sz.to_bytes(4, 'little')
        hdr[0x60 + 4 * i:0x64 + 4 * i] = sz.to_bytes(4, 'little')
        hdr[0xA0 + 0x20 * i:0xC0 + 0x20 * i] = hashlib.sha256(raw).digest()
        body += raw
        fo += sz
    hdr[0x3C:0x40] = (0x40).to_bytes(4, 'little')
    hdr[0x40:0x54] = bytes.fromhex(BID)
    return bytes(hdr + body)


def read_nso(data):
    """[(memory offset, bytes)] per segment, checking the header's file sizes and hashes against the bytes."""
    out = []
    for i in range(3):
        fo, mo, sz = (int.from_bytes(data[0x10 + 0x10 * i + k:0x14 + 0x10 * i + k], 'little') for k in (0, 4, 8))
        assert int.from_bytes(data[0x60 + 4 * i:0x64 + 4 * i], 'little') == sz, 'file size of segment %d' % i
        raw = data[fo:fo + sz]
        assert len(raw) == sz, 'segment %d cut short' % i
        assert hashlib.sha256(raw).digest() == data[0xA0 + 0x20 * i:0xC0 + 0x20 * i], 'hash of segment %d' % i
        out.append((mo, raw))
    return out


def bl(frm, to):
    return (0x94000000 | (((to - frm) // 4) & 0x3FFFFFF)).to_bytes(4, 'little')


def b(frm, to):
    return (0x14000000 | (((to - frm) // 4) & 0x3FFFFFF)).to_bytes(4, 'little')


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.main = os.path.join(self.dir, 'main')
        with open(self.main, 'wb') as f:
            f.write(make_nso())
        self.notes = []
        self._say, self._sites = fb.say, fb.site_addresses
        fb.say = lambda kind, *parts: self.notes.append(' '.join(str(p) for p in parts))
        fb.site_addresses = lambda: {}

    def tearDown(self):
        fb.say, fb.site_addresses = self._say, self._sites
        shutil.rmtree(self.dir, ignore_errors=True)

    def apply(self, patches, mod_sites=()):
        applied, left = fb.apply_exefs_patches(self.main, patches, mod_sites)
        with open(self.main, 'rb') as f:
            return applied, left, read_nso(f.read())


class Segments(Base):
    def test_patch_inside_text(self):
        applied, left, segs = self.apply([('m', 'm/exefs/a.ips', [(0x10, b'\xAA\xBB')])])
        self.assertEqual((applied, left), (['m'], {}))
        self.assertEqual(segs[0][1][0x10:0x12], b'\xAA\xBB')
        self.assertEqual(len(segs[0][1]), TEXT[1])
        self.assertEqual(segs[1][1], b'\x22' * RO[1])

    def test_code_cave_after_text_grows_it(self):
        cave = TEXT[1]                       # the first byte after main's code
        code = bytes(0x20)
        applied, left, segs = self.apply([('cave', 'cave/exefs/c.ips', [(0x40, bl(0x40, cave)), (cave, code)])])
        self.assertEqual((applied, left), (['cave'], {}))
        self.assertEqual(len(segs[0][1]), TEXT[1] + 0x20)
        self.assertEqual(segs[0][1][cave:cave + 0x20], code)
        self.assertEqual(segs[1], (RO[0], b'\x22' * RO[1]))       # rodata and data moved in the file, unchanged
        self.assertEqual(segs[2], (DATA[0], b'\x33' * DATA[1]))
        self.assertFalse([n for n in self.notes if 'no B or BL' in n], self.notes)

    def test_cave_without_a_branch_into_it_is_noted(self):
        applied, _, _ = self.apply([('cave', 'cave/exefs/c.ips', [(TEXT[1] + 8, b'\x01\x02\x03\x04')])])
        self.assertEqual(applied, ['cave'])
        self.assertTrue([n for n in self.notes if 'no B or BL' in n], self.notes)

    def test_cave_reached_by_a_tail_branch(self):
        cave = TEXT[1]
        self.apply([('cave', 'cave/exefs/c.ips', [(0x80, b(0x80, cave)), (cave, bytes(8))])])
        self.assertFalse([n for n in self.notes if 'no B or BL' in n], self.notes)

    def test_past_the_page_is_left_out_and_others_apply(self):
        applied, left, segs = self.apply([('a-bad', 'a-bad/exefs/x.ips', [(0x1FFE, b'\x00\x00\x00\x00')]),
                                          ('b-good', 'b-good/exefs/y.ips', [(0x20, b'\x55')])])
        self.assertEqual(applied, ['b-good'])
        self.assertIn('a-bad', left)
        self.assertEqual(len(segs[0][1]), TEXT[1])
        self.assertEqual(segs[0][1][0x20], 0x55)

    def test_past_data_is_left_out(self):
        applied, left, _ = self.apply([('d', 'd/exefs/x.ips', [(DATA[0] + DATA[1], b'\x00')])])
        self.assertEqual((applied, list(left)), ([], ['d']))

    def test_two_mods_in_the_same_padding(self):
        applied, left, _ = self.apply([('a', 'a/exefs/x.ips', [(TEXT[1], b'\x01' * 8)]),
                                       ('b', 'b/exefs/y.ips', [(TEXT[1] + 4, b'\x02' * 8)])])
        self.assertEqual(applied, ['a'])
        self.assertIn('a/exefs/x.ips', left['b'])

    def test_one_bad_record_leaves_the_whole_mod_out(self):
        applied, left, segs = self.apply([('m', 'm/exefs/x.ips', [(0x10, b'\x77'), (0x5000, b'\x00')])])
        self.assertEqual((applied, list(left)), ([], ['m']))
        self.assertEqual(segs[0][1][0x10], 0x11)

    def test_site_clash_leaves_the_mod_out(self):
        fb.site_addresses = lambda: {0x40: 'patch'}
        applied, left, _ = self.apply([('m', 'm/exefs/x.ips', [(0x41, b'\x00')]), ('n', 'n/exefs/y.ips', [(0x80, b'\x00')])])
        self.assertEqual(applied, ['n'])
        self.assertIn('main.sites', left['m'])

    def test_rodata_padding(self):
        applied, _, segs = self.apply([('r', 'r/exefs/x.ips', [(RO[0] + RO[1], b'\x99' * 4)])])
        self.assertEqual(applied, ['r'])
        self.assertEqual(len(segs[1][1]), RO[1] + 4)
        self.assertEqual(segs[2], (DATA[0], b'\x33' * DATA[1]))


class Pchtxt(unittest.TestCase):
    def parse(self, text):
        return fb.parse_pchtxt(text, 'm/exefs/p.pchtxt', BID)

    def test_title_lines_are_not_patches(self):
        recs = self.parse('@nsobid-%s\n\n@flag offset_shift 0x100\n\n// Add 60 FPS\n@enabled\nAdd 60 FPS\n00001000 1F2003D5\n@stop\n' % BID)
        self.assertEqual(recs, [(0x1100, bytes.fromhex('1F2003D5'))])

    def test_short_and_0x_offsets_apply(self):
        # EmblemWeave (patches.rs parse_offset) takes an offset of any length, with or without 0x
        self.assertEqual(self.parse('@nsobid-%s\n@enabled\n1000 1F2003D5\n0x2000 AA BB\n' % BID),
                         [(0x1000, bytes.fromhex('1F2003D5')), (0x2000, b'\xAA\xBB')])

    def test_lines_before_any_section_apply(self):
        # EmblemWeave starts enabled; a file with no @enabled line is patches, not nothing
        self.assertEqual(self.parse('@nsobid-%s\n00001000 00\n@disabled\n00002000 11\n@enabled\n00003000 22\n' % BID),
                         [(0x1000, b'\x00'), (0x3000, b'\x22')])

    def test_last_record_before_next_section_applies(self):
        recs = self.parse('@nsobid-%s\n@enabled\n00001000 01\n00001004 02\n@disabled\n00002000 03\n' % BID)
        self.assertEqual(recs, [(0x1000, b'\x01'), (0x1004, b'\x02')])

    def test_offset_shift_is_hex(self):
        self.assertEqual(self.parse('@nsobid-%s\n@flag offset_shift 100\n00001000 01\n' % BID), [(0x1100, b'\x01')])
        self.assertEqual(self.parse('@nsobid-%s\n@flag offset_shift -0x100\n00001000 01\n' % BID), [(0xF00, b'\x01')])
        self.assertEqual(self.parse('@nsobid-%s\n@flag offset_shift\n00001000 01\n' % BID), [(0x1000, b'\x01')])

    def test_comments_and_strings(self):
        recs = self.parse('@nsobid-%s\n; note\n# note\n00001000 "a//b\\n" // tail\n' % BID)
        self.assertEqual(recs, [(0x1000, b'a//b\n')])

    def test_bad_bytes_on_a_full_offset_name_the_line(self):
        with self.assertRaises(fb.Fail) as e:
            self.parse('@nsobid-%s\n00001000 1F2\n' % BID)
        self.assertIn('p.pchtxt:2', str(e.exception))

    def test_bad_offset_shift_names_the_line(self):
        with self.assertRaises(fb.Fail) as e:
            self.parse('@nsobid-%s\n@flag offset_shift 0xZZ\n' % BID)
        self.assertIn('p.pchtxt:2', str(e.exception))


class Collect(unittest.TestCase):
    def setUp(self):
        self.game = tempfile.mkdtemp()
        for name, fname, data in (('a-bad', 'x.pchtxt', b'@enabled\n00001000 00\n'),            # no @nsobid: left out
                                  ('b-good', BID + '.ips', b'PATCH\x00\x01\x10\x00\x01\xAAEOF'),
                                  ('c-other', 'cd' * 20 + '.ips', b'PATCH\x00\x01\x10\x00\x01\xAAEOF')):
            d = os.path.join(self.game, 'mods', name, 'exefs')
            os.makedirs(d)
            with open(os.path.join(d, fname), 'wb') as f:
                f.write(data)

    def tearDown(self):
        shutil.rmtree(self.game, ignore_errors=True)

    def test_ips32_and_subfolders(self):
        d = os.path.join(self.game, 'mods', 'd-deep', 'exefs', 'Sub', 'More')
        os.makedirs(d)
        with open(os.path.join(d, BID + '.ips32'), 'wb') as f:
            f.write(b'IPS32\x00\x00\x01\x20\x00\x01\xBBEEOF')
        h = os.path.join(self.game, 'mods', 'd-deep', 'exefs', '.git')
        os.makedirs(h)
        with open(os.path.join(h, BID + '.ips'), 'wb') as f:
            f.write(b'not a patch')
        found, left, _ = fb.collect_exefs_patches(self.game, BID)
        self.assertIn(('d-deep', 'd-deep/exefs/Sub/More/%s.ips32' % BID, [(0x20, b'\xBB')]), found)
        self.assertNotIn('d-deep', left)

    def test_left_out_used_and_unused(self):
        found, left, unused = fb.collect_exefs_patches(self.game, BID)
        self.assertEqual([(n, m) for n, _, m in found], [('b-good', [(0x10, b'\xAA')])])
        self.assertEqual(list(left), ['a-bad'])
        self.assertEqual(unused, ['c-other'])
        self.assertNotEqual(fb.exefs_patch_stamp(found, left), fb.exefs_patch_stamp(found, {}))

    def test_record_file(self):
        fb.write_exefs_record(self.game, ['b-good'], {'a-bad': 'why\tnot'}, ['c-other'])
        lines = open(os.path.join(self.game, fb.EXEFS_RECORD), encoding='utf-8').read().splitlines()
        self.assertEqual(lines[1:], ['applied\tb-good', 'left out\ta-bad\twhy not', 'not used\tc-other'])
        fb.write_exefs_record(self.game, [], {}, [])
        self.assertFalse(os.path.exists(os.path.join(self.game, fb.EXEFS_RECORD)))


class LiftedSites(unittest.TestCase):
    def setUp(self):
        self.game = tempfile.mkdtemp()
        with open(os.path.join(self.game, 'ObeliskChamber.sites'), 'w', encoding='utf-8') as f:
            f.write('# written by Set up\n@nsobid %s\npatch 0x7100001000\ninline 0x7100002000   # a/exefs/a.sites:3 (é)\n'
                    'midhook 0x7100003000   # b/exefs/b.sites:2\n' % BID)

    def tearDown(self):
        shutil.rmtree(self.game, ignore_errors=True)

    def test_subset_is_current(self):
        self.assertTrue(fb.mod_sites_lifted(self.game, [('a:3', 'inline', 0x7100002000)], BID))
        self.assertTrue(fb.mod_sites_lifted(self.game, [], BID))

    def test_new_site_or_other_build_is_not(self):
        self.assertFalse(fb.mod_sites_lifted(self.game, [('c:1', 'inline', 0x7100004000)], BID))
        self.assertFalse(fb.mod_sites_lifted(self.game, [('b:2', 'inline', 0x7100003000)], BID))   # same address, other kind
        self.assertFalse(fb.mod_sites_lifted(self.game, [], 'cd' * 20))
        os.remove(os.path.join(self.game, 'ObeliskChamber.sites'))
        self.assertFalse(fb.mod_sites_lifted(self.game, [], BID))


class ModSites(unittest.TestCase):
    def setUp(self):
        self.game = tempfile.mkdtemp()
        for name, addr in (('on', 0x7100002000), ('_off', 0x7100003000), ('.hidden', 0x7100004000)):
            d = os.path.join(self.game, 'mods', name, 'exefs')
            os.makedirs(d)
            with open(os.path.join(d, 'm.sites'), 'w', encoding='utf-8') as f:
                f.write('@nsobid %s\ninline 0x%x\n' % (BID, addr))

    def tearDown(self):
        shutil.rmtree(self.game, ignore_errors=True)

    def test_switched_off_mods_keep_their_sites(self):
        # as EmblemWeave's sites.rs: a switched-off mod's sites stay, so switching it back on needs no Set up
        self.assertEqual(sorted(a for _, _, a in fb.collect_mod_sites(self.game, BID)), [0x7100002000, 0x7100003000])
        self.assertEqual([n for n, _ in fb.find_mod_dirs(os.path.join(self.game, 'mods'))], ['on'])


class PchtxtBytes(unittest.TestCase):
    def test_little_endian_writes_bytes_as_typed(self):
        """@little-endian is the usual header of a pchtxt; Eden, Ryujinx and EmblemWeave write the bytes as typed either way."""
        self.assertEqual(fb.pchtxt_bytes('1F2003D5', True), bytes.fromhex('1F2003D5'))
        self.assertEqual(fb.pchtxt_bytes('1F2003D5', False), bytes.fromhex('1F2003D5'))


if __name__ == '__main__':
    unittest.main(verbosity=1)

