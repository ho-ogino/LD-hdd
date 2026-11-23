"""ldhdd.py のテスト

  python3 -m unittest discover -s ld-hddboot/tests

repo の MKLDHD.d88 / MKLDHD1.d88 / hdd.com と、テストの中で作る合成データだけを使う。
環境変数 LDHDD_LDSYS に LSX-Dodgers の ldsys.d88 を指定すると、実物での確認もする。
"""

import contextlib
import hashlib
import io
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BOOTDIR = os.path.dirname(HERE)
REPO = os.path.dirname(BOOTDIR)
sys.path.insert(0, BOOTDIR)

import ldhdd  # noqa: E402

MKLDHD = os.path.join(BOOTDIR, "MKLDHD.d88")
MKLDHD1 = os.path.join(BOOTDIR, "MKLDHD1.d88")
HDD_COM = os.path.join(REPO, "hdd.com")

# ldhdd_boot.asm を AILZ80ASM でアセンブルした結果の先頭 0x1BE バイト (0x43 以降は FF)
ASM_BOOT_HEAD = bytes.fromhex(
    "014c53582d446f646765727348445379"
    "732000f0000021000000000000000000"
    "00f3310000d92a0502d9ed5b01022a03"
    "02b7ed524d4421070209eb09eb1b2bed"
    "b8d9e9"
) + b"\xff" * (0x1BE - 0x43)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def read(path):
    with open(path, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 合成データ
# ---------------------------------------------------------------------------

def fake_ld_bin(top=0xD100, body_len=9721, exec_off=7, fill=0x5A):
    end = top + body_len
    body = bytes([fill]) * body_len
    return bytes([0xFE]) + struct.pack("<HHH", top, end, top + exec_off) + body


def make_d88(logical, ipl_style=False):
    """論理セクタ (720 x 512) から D88 を作る。
    ipl_style=True のときは ldsys.d88 と同じく、トラック 0 表の R=1 と R=10 を 256 バイトにする。"""
    tracks = []
    for t in range(80):
        c, h = t // 2, t % 2
        secs = []
        for r in range(1, 10):
            n = t * 9 + (r - 1)
            data = logical[n * 512:(n + 1) * 512]
            if ipl_style and t == 0 and r == 1:
                data = b"\x01LD78(IPL" + bytes(256 - 9)
            secs.append((c, h, r, data))
        if ipl_style and t == 0:
            secs.insert(1, (0, 0, 10, bytes(256)))
        tracks.append(secs)
    header = bytearray(0x2B0)
    body = bytearray()
    off = 0x2B0
    for i, secs in enumerate(tracks):
        struct.pack_into("<I", header, 0x20 + 4 * i, off + len(body))
        for (c, h, r, data) in secs:
            n = 1 if len(data) == 256 else 2
            body += struct.pack("<BBBBHBBB", c, h, r, n, len(secs), 0, 0, 0) + bytes(5) + struct.pack("<H", len(data))
            body += data
    struct.pack_into("<I", header, 0x1C, 0x2B0 + len(body))
    return bytes(header + body)


def make_fat12_2d(files, with_bpb=True, truncate=None):
    """2D FAT12 の論理イメージ (720 x 512) を作る。files は [(name, ext, data)]。
    truncate=name のとき、そのファイルの鎖を 1 クラスタ目で終わらせる。"""
    img = bytearray(720 * 512)
    if with_bpb:
        img[0:3] = b"\xeb\xfe\x90"
        img[3:11] = b"LD      "
        struct.pack_into("<HBHBHHBH", img, 11, 512, 2, 1, 2, 112, 720, 0xFD, 2)
    fat = bytearray(1024)
    fat[0:3] = b"\xfd\xff\xff"

    def setfat(c, v):
        o = c * 3 // 2
        cur = struct.unpack_from("<H", fat, o)[0]
        if c & 1:
            cur = (cur & 0x000F) | (v << 4)
        else:
            cur = (cur & 0xF000) | v
        struct.pack_into("<H", fat, o, cur)

    root = 5 * 512
    data_off = 12 * 512
    cl = 2
    for i, (name, ext, data) in enumerate(files):
        n = max(1, (len(data) + 1023) // 1024)
        start = cl
        for k in range(n):
            nxt = cl + 1 if k < n - 1 else 0xFFF
            if truncate == name and k == 0:
                nxt = 0xFFF
            setfat(cl, nxt)
            o = data_off + (cl - 2) * 1024
            img[o:o + 1024] = data[k * 1024:(k + 1) * 1024].ljust(1024, b"\0")
            cl += 1
        e = name.ljust(8).encode() + ext.ljust(3).encode() + bytes([0x20]) + bytes(14) + struct.pack("<HI", start, len(data))
        img[root + i * 32:root + i * 32 + 32] = e
    img[512:1536] = fat
    img[1536:2560] = fat
    return bytes(img)


def bpb_sector(total=384, spc=4, root=512, spf=None, bps=512):
    """FAT12/16 の BPB (パーティション total セクタに合わせる)"""
    b = bytearray(512)
    b[0:3] = b"\xeb\x3c\x90"
    b[3:11] = b"MSDOS5.0"
    if spf is None:
        spf = 1
        while True:
            data_start = 1 + 2 * spf + (root * 32 + bps - 1) // bps
            clusters = (total - data_start) // spc
            bits = 12 if clusters < 4085 else 16
            if spf * bps >= ((clusters + 2) * bits + 7) // 8:
                break
            spf += 1
    tot16 = total if total < 0x10000 else 0
    struct.pack_into("<HBHBHHBH", b, 11, bps, spc, 1, 2, root, tot16, 0xF8, spf)
    if not tot16:
        struct.pack_into("<I", b, 32, total)
    b[510:512] = b"\x55\xaa"
    return bytes(b)


def vhd_footer(size, disk_type=2):
    f = bytearray(512)
    f[0:8] = b"conectix"
    struct.pack_into(">IIQ", f, 8, 2, 0x00010000, 0xFFFFFFFFFFFFFFFF)
    struct.pack_into(">QQ", f, 40, size, size)
    struct.pack_into(">I", f, 60, disk_type)
    return bytes(f)


def make_vhd(path, sectors=512, parts=((0x06, 128, None),), footer=True, disk_type=2, first=None):
    """MBR + パーティション先頭の BPB + (固定 VHD フッター) の小さなイメージ"""
    img = bytearray(sectors * 512)
    mbr = bytearray(512) if first is None else bytearray(first)
    mbr[0:2] = b"\x33\xc0"
    for i, (ptype, lba, count) in enumerate(parts):
        if count is None:
            count = sectors - lba
        e = struct.pack("<B3sB3sII", 0, b"\0\0\0", ptype, b"\0\0\0", lba, count)
        mbr[0x1BE + 16 * i:0x1BE + 16 * i + 16] = e
        if lba * 512 + 512 <= len(img):
            img[lba * 512:lba * 512 + 512] = bpb_sector(count)
    mbr[510:512] = b"\x55\xaa"
    img[0:512] = mbr
    if footer:
        img += vhd_footer(sectors * 512, disk_type)
    with open(path, "wb") as f:
        f.write(img)
    return path


def inputs_from(ld_bin, com):
    return ldhdd.Inputs(ld_bin, "test", com, "test")


# ---------------------------------------------------------------------------
# テスト
# ---------------------------------------------------------------------------

class TestMakesys(unittest.TestCase):
    def test_reproduces_mkldhd(self):
        comb = ldhdd.files_from_d88(MKLDHD)["LD.BIN"]
        com = read(HDD_COM)
        inner = comb[135 + len(com):]
        self.assertEqual(ldhdd.makesys(inner, com), comb)

    def test_reproduces_mkldhd1_hdd1com(self):
        files = ldhdd.files_from_d88(MKLDHD1)
        comb = files["LD.BIN"]
        com = files["HDD1.COM"]
        inner = comb[135 + len(com):]
        self.assertEqual(ldhdd.makesys(inner, com), comb)

    def test_unit1_insertion(self):
        ld = fake_ld_bin()
        com = read(HDD_COM)
        u0 = ldhdd.makesys(ld, com, 0)
        u1 = ldhdd.makesys(ld, com, 1)
        self.assertEqual(len(u1), len(u0) + 5)
        self.assertEqual(u1[97:105], bytes.fromhex("3e31325d00c30001"))
        run0 = struct.unpack_from("<H", u0, 1)[0]
        run1 = struct.unpack_from("<H", u1, 1)[0]
        self.assertEqual(run1, run0 - 5)
        self.assertEqual(struct.unpack_from("<H", u1, 37)[0], run1 + 98)
        self.assertEqual(u1[105:140], u0[100:135])
        self.assertEqual(u1[116], 0x20)
        self.assertEqual(u0[111], 0x20)
        self.assertEqual(u1[140:], u0[135:])

    def test_unit_out_of_range(self):
        with self.assertRaises(ldhdd.LdhddError):
            ldhdd.makesys(fake_ld_bin(), b"\xc9", 4)

    def test_size_limits(self):
        # 実物 (LD.end = F6F9) は通る
        comb = ldhdd.files_from_d88(MKLDHD)["LD.BIN"]
        self.assertEqual(struct.unpack_from("<H", comb, 3)[0], 0xF6F9)
        com = b"\xc9" * 100
        # 0x200 + n がちょうど 0xF000 は通り、1 多いとエラー
        n_ld = 0xF000 - 0x200 - 135 - len(com)
        ld = fake_ld_bin(top=0xFFFF - (n_ld - 7), body_len=n_ld - 7)
        self.assertEqual(len(ldhdd.makesys(ld, com)), 0xF000 - 0x200)
        ld2 = fake_ld_bin(top=0xFFFF - (n_ld - 6), body_len=n_ld - 6)
        with self.assertRaises(ldhdd.LdhddError):
            ldhdd.makesys(ld2, com)

    def test_com_overwrites_stub(self):
        with self.assertRaises(ldhdd.LdhddError):
            ldhdd.makesys(fake_ld_bin(top=0xD100), b"\0" * 0x7000)

    def test_com_exactly_fits(self):
        ld = fake_ld_bin(top=0xD100)
        # run = D100 - 7 - C - 128, 0x100 + C == run  ->  C = (D100 - 7 - 128 - 0x100) / 2
        c = (0xD100 - 7 - 128 - 0x100) // 2
        out = ldhdd.makesys(ld, b"\0" * c)
        run = struct.unpack_from("<H", out, 1)[0]
        self.assertLessEqual(0x100 + c, run)
        with self.assertRaises(ldhdd.LdhddError):
            ldhdd.makesys(ld, b"\0" * (c + 1))


class TestLdBin(unittest.TestCase):
    def test_bad(self):
        good = fake_ld_bin()
        ldhdd.parse_msx_bin(good)
        with self.assertRaises(ldhdd.LdhddError):
            ldhdd.parse_msx_bin(b"\xff" + good[1:])
        with self.assertRaises(ldhdd.LdhddError):
            ldhdd.parse_msx_bin(good + b"\0")
        bad_exec = bytearray(good)
        struct.pack_into("<H", bad_exec, 5, 0x1000)
        with self.assertRaises(ldhdd.LdhddError):
            ldhdd.parse_msx_bin(bytes(bad_exec))


class TestD88(unittest.TestCase):
    def test_mkldhd_files(self):
        files = ldhdd.files_from_d88(MKLDHD)
        self.assertEqual({k: len(v) for k, v in files.items()},
                         {"HDD.COM": 1254, "MKLDHD.BAT": 128, "LD.BIN": 11117, "HDD1.COM": 1270, "MKLDHD1.BAT": 128})
        self.assertEqual(files["HDD.COM"], read(HDD_COM))

    def test_ipl_style_without_bpb(self):
        ld = fake_ld_bin()
        img = make_fat12_2d([("LD", "BIN", ld), ("HDD", "COM", b"\xc9" * 1254)], with_bpb=False)
        d88 = make_d88(img, ipl_style=True)
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "ldsys.d88")
            with open(p, "wb") as f:
                f.write(d88)
            files = ldhdd.files_from_d88(p)
        self.assertEqual(files["LD.BIN"], ld)
        self.assertEqual(files["HDD.COM"], b"\xc9" * 1254)

    def test_truncated_chain(self):
        img = make_fat12_2d([("LD", "BIN", fake_ld_bin()), ("HDD", "COM", b"\xc9" * 1254)], truncate="HDD")
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "bad.d88")
            with open(p, "wb") as f:
                f.write(make_d88(img))
            with self.assertRaises(ldhdd.LdhddError):
                ldhdd.files_from_d88(p)
            v = make_vhd(os.path.join(td, "t.vhd"))
            before = sha(v)
            rc = run_main(["update", v, "--ldsys", p])
            self.assertEqual(rc, 1)
            self.assertEqual(sha(v), before)


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = ldhdd.main(argv)
    run_main.out, run_main.err = out.getvalue(), err.getvalue()
    return rc


class TestBootRecord(unittest.TestCase):
    def test_layout(self):
        mbr = bytearray(512)
        mbr[0x1B8:0x1BE] = b"\x11\x22\x33\x44\0\0"
        mbr[0x1BE:0x1FE] = bytes(range(0x40, 0x80))
        mbr[0x1FE:0x200] = b"\x55\xaa"
        rec = ldhdd.boot_record(bytes(mbr))
        self.assertEqual(rec[:0x1B8], ASM_BOOT_HEAD[:0x1B8])
        # ディスク署名 (0x1B8) から後ろは対象の物を残す
        self.assertEqual(rec[0x1B8:], bytes(mbr[0x1B8:]))
        self.assertEqual(rec[0x82], 0xFF)


class TestUpdate(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = self.td.name
        self.com = read(HDD_COM)
        comb = ldhdd.files_from_d88(MKLDHD)["LD.BIN"]
        self.ld161 = comb[135 + len(self.com):]
        self.inputs = inputs_from(self.ld161, self.com)

    def tearDown(self):
        self.td.cleanup()

    def vhd(self, name="t.vhd", **kw):
        return make_vhd(os.path.join(self.dir, name), **kw)

    def quiet_update(self, path, inputs=None, **kw):
        return ldhdd.update(path, inputs or self.inputs, out=io.StringIO(), **kw)

    def test_success_and_legacy_equivalence(self):
        v = self.vhd()
        base = read(v)
        bak = self.quiet_update(v)
        after = read(v)
        sys_bin = ldhdd.makesys(self.ld161, self.com)
        n = 0x200 + len(sys_bin)
        # mkldhdd.py と同じ (ldhdd_boot.bin + HDDBASE[len:])。ただしディスク署名 (0x1B8〜0x1BD) は残す
        legacy_ipl = ASM_BOOT_HEAD[:0x1B8] + base[0x1B8:0x200] + sys_bin
        self.assertEqual(after, legacy_ipl + base[len(legacy_ipl):])
        self.assertEqual(after[n:], base[n:])
        self.assertEqual(read(bak), base)

    def test_second_update_replaces(self):
        v = self.vhd()
        self.quiet_update(v, backup=False)
        ld2 = fake_ld_bin(top=0xD100, body_len=9721, fill=0x77)
        self.quiet_update(v, inputs_from(ld2, self.com), backup=False)
        sys2 = ldhdd.makesys(ld2, self.com)
        self.assertEqual(read(v)[0x200:0x200 + len(sys2)], sys2)

    def assert_refused(self, v, inputs=None, **kw):
        before = sha(v)
        with self.assertRaises(ldhdd.LdhddError):
            self.quiet_update(v, inputs, **kw)
        self.assertEqual(sha(v), before)
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".bak") or ".bak." in f], [])

    def test_refuse_no_mbr(self):
        v = self.vhd()
        with open(v, "r+b") as f:
            f.seek(510)
            f.write(b"\0\0")
        self.assert_refused(v)

    def test_refuse_superfloppy(self):
        v = self.vhd(first=bpb_sector())
        with open(v, "r+b") as f:
            f.write(bpb_sector())
        self.assert_refused(v)

    def test_refuse_partition_too_close(self):
        self.assert_refused(self.vhd(parts=((0x06, 16, None),)))

    def test_refuse_partition_too_far(self):
        self.assert_refused(self.vhd(sectors=33000, parts=((0x06, 32768, None),)))

    def test_refuse_out_of_order_partition(self):
        v = self.vhd(parts=((0x06, 128, 200), (0x06, 8, 100)))
        self.assert_refused(v)
        self.assert_refused(v, force=True)
        self.quiet_update(self.vhd("ok.vhd"), backup=False)
        # 起動イメージがあっても重なりは不可
        v2 = self.vhd("t2.vhd", parts=((0x06, 128, 200), (0x06, 8, 100)))
        with open(v2, "r+b") as f:
            f.write(ldhdd.BOOT_SIGNATURE)
        self.assert_refused(v2, force=True)

    def test_gap_data_needs_force(self):
        v = self.vhd()
        with open(v, "r+b") as f:
            f.seek(0x1000)
            f.write(b"\x99")
        self.assert_refused(v)
        self.quiet_update(v, force=True, backup=False)

    def test_refuse_dynamic_vhd(self):
        self.assert_refused(self.vhd(disk_type=3))

    def test_refuse_no_bpb_in_partition(self):
        v = self.vhd()
        with open(v, "r+b") as f:
            f.seek(128 * 512)
            f.write(bytes(512))
        self.assert_refused(v)

    def test_raw_image_ok(self):
        v = self.vhd(footer=False)
        self.quiet_update(v, backup=False)
        self.assertEqual(read(v)[:len(ldhdd.BOOT_SIGNATURE)], ldhdd.BOOT_SIGNATURE)

    def test_larger_replacement_sentinel(self):
        small_ld = fake_ld_bin(top=0xE000, body_len=0x1000)
        v = self.vhd()
        self.quiet_update(v, inputs_from(small_ld, self.com), backup=False)
        old_n = len(ldhdd.makesys(small_ld, self.com))
        with open(v, "r+b") as f:
            f.seek(0x200 + old_n)
            f.write(b"\x42")
        self.assert_refused(v)
        # 番兵を消せば通る
        with open(v, "r+b") as f:
            f.seek(0x200 + old_n)
            f.write(b"\x00")
        self.quiet_update(v, backup=False)

    def test_disk_signature_kept(self):
        v = self.vhd()
        with open(v, "r+b") as f:
            f.seek(0x1B8)
            f.write(b"\xde\xad\xbe\xef")
        self.quiet_update(v, backup=False)
        self.assertEqual(read(v)[0x1B8:0x1BC], b"\xde\xad\xbe\xef")

    def test_shrink_then_grow_without_force(self):
        big = self.ld161
        small = fake_ld_bin(top=0xE000, body_len=0x1000)
        v = self.vhd()
        self.quiet_update(v, inputs_from(big, self.com), backup=False)
        self.quiet_update(v, inputs_from(small, self.com), backup=False)
        n_small = 0x200 + len(ldhdd.makesys(small, self.com))
        n_big = 0x200 + len(ldhdd.makesys(big, self.com))
        self.assertEqual(read(v)[n_small:n_big], bytes(n_big - n_small))
        self.quiet_update(v, inputs_from(big, self.com), backup=False)
        self.assertEqual(read(v)[0x200:n_big], ldhdd.makesys(big, self.com))

    def test_unverified_old_header_needs_force(self):
        v = self.vhd()
        with open(v, "r+b") as f:
            f.write(ldhdd.BOOT_SIGNATURE)
            f.seek(0x200)
            # 長さ 0x3000 を名乗るが MAKESYS の形ではない
            f.write(bytes([0xFE]) + struct.pack("<HHH", 0xC000, 0xF000 - 7, 0xC000) + b"\x11" * 16)
        self.assert_refused(v)
        self.quiet_update(v, force=True, backup=False)

    def test_refuse_bad_fat_bpb(self):
        for name, sec in (("tot0", bpb_sector(384)[:19] + b"\0\0" + bpb_sector(384)[21:]),
                          ("fat32", bpb_sector(384)[:22] + b"\0\0" + bpb_sector(384)[24:]),
                          ("toobig", bpb_sector(4000)),
                          ("fatsmall", bpb_sector(384, spf=1, spc=1, root=16))):
            v = self.vhd(name + ".vhd")
            with open(v, "r+b") as f:
                f.seek(128 * 512)
                f.write(sec)
            self.assert_refused(v)

    def test_refuse_small_image(self):
        self.assert_refused(self.vhd(sectors=100, parts=((0x06, 40, 60),)))

    def test_dry_run_and_backup_names(self):
        v = self.vhd()
        before = sha(v)
        self.quiet_update(v, dry_run=True)
        self.assertEqual(sha(v), before)
        self.assertEqual(os.listdir(self.dir), ["t.vhd"])
        b1 = self.quiet_update(v)
        b2 = self.quiet_update(v)
        self.assertNotEqual(b1, b2)
        self.assertTrue(os.path.exists(b1) and os.path.exists(b2))


class TestCli(unittest.TestCase):
    def test_ldsys_and_ld_bin_together(self):
        with tempfile.TemporaryDirectory() as td:
            ld = fake_ld_bin(fill=0x33)
            p_ld = os.path.join(td, "LD.BIN")
            with open(p_ld, "wb") as f:
                f.write(ld)
            out = os.path.join(td, "out.bin")
            self.assertEqual(run_main(["sys", "-o", out, "--ldsys", MKLDHD, "--ld-bin", p_ld]), 0)
            data = read(out)
            com = ldhdd.files_from_d88(MKLDHD)["HDD.COM"]
            self.assertEqual(data[135:135 + len(com)], com)
            self.assertEqual(data[135 + len(com):], ld)
            # 既にあれば上書きしない
            self.assertEqual(run_main(["sys", "-o", out, "--ldsys", MKLDHD, "--ld-bin", p_ld]), 1)
            # --hdd-com が --ldsys より優先
            p_com = os.path.join(td, "X.COM")
            with open(p_com, "wb") as f:
                f.write(b"\xc9" * 10)
            out2 = os.path.join(td, "out2.bin")
            self.assertEqual(run_main(["sys", "-o", out2, "--ldsys", MKLDHD, "--ld-bin", p_ld, "--hdd-com", p_com]), 0)
            self.assertEqual(read(out2)[135:145], b"\xc9" * 10)

    def test_missing_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            v = make_vhd(os.path.join(td, "t.vhd"))
            self.assertEqual(run_main(["update", v]), 1)
            self.assertIn("LD.BIN", run_main.err)


class TestInfo(unittest.TestCase):
    def test_info_variants(self):
        com = read(HDD_COM)
        files1 = ldhdd.files_from_d88(MKLDHD1)
        with tempfile.TemporaryDirectory() as td:
            v = make_vhd(os.path.join(td, "t.vhd"))
            self.assertEqual(run_main(["info", v]), 0)
            self.assertIn("起動部    : なし", run_main.out)

            comb = ldhdd.files_from_d88(MKLDHD)["LD.BIN"]
            ld = comb[135 + len(com):]
            ldhdd.update(v, inputs_from(ld, com), unit=1, backup=False, out=io.StringIO())
            run_main(["info", v])
            self.assertIn("version 1.61b", run_main.out)
            self.assertIn("スタブで 1 を指定", run_main.out)
            self.assertIn("HDD.COM (LD-hdd 0.14)", run_main.out)
            self.assertIn("パーティション 1: 種類 06 開始 LBA 128", run_main.out)

            # 旧来の HDD1 イメージ (MKLDHD1.d88 の合体版をそのまま起動部に置く)
            v2 = make_vhd(os.path.join(td, "t2.vhd"))
            with open(v2, "r+b") as f:
                mbr = f.read(512)
                f.seek(0)
                f.write(ldhdd.boot_record(mbr) + files1["LD.BIN"])
            run_main(["info", v2])
            self.assertIn("スタブでの指定なし", run_main.out)
            self.assertIn("HDD1.COM (旧来の HDD1 用)", run_main.out)


@unittest.skipUnless(os.environ.get("LDHDD_LDSYS"), "LDHDD_LDSYS が無い")
class TestRealLdsys(unittest.TestCase):
    def test_ldsys(self):
        p = os.environ["LDHDD_LDSYS"]
        inp = ldhdd.load_inputs(p, None, None)
        self.assertEqual(len(inp.ld_bin), 9728)
        self.assertIn("LSX-Dodgers for X1", ldhdd.ld_version(inp.ld_bin))
        self.assertEqual(inp.com, read(HDD_COM))
        out = ldhdd.makesys(inp.ld_bin, inp.com)
        fixed = {1, 2, 3, 4, 5, 6, 8, 9, 14, 15, 19, 20, 22, 23, 25, 26, 28, 29, 31, 32, 37, 38, 111}
        for i in range(135):
            if i not in fixed:
                self.assertEqual(out[i], ldhdd.STUB_TEMPLATE[i], "offset %d" % i)
        ms = ldhdd.files_from_d88(p).get("MAKESYS.COM")
        if ms:
            i = ms.find(b"AUTOEXEC ")
            self.assertEqual(ms[i + 9 - 135:i + 9], ldhdd.STUB_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
