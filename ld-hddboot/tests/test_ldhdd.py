"""ldhdd.py のテスト

  python3 -m unittest discover -s ld-hddboot/tests

repo の MKLDHD.d88 / MKLDHD1.d88 / hdd.com と、テストの中で作る合成データだけを使う。
環境変数 LDHDD_LDSYS に LSX-Dodgers の ldsys.d88 を指定すると、実物での確認もする。
"""

import contextlib
import hashlib
import io
import os
import shutil
import struct
import sys
import tempfile
import time
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
    body = bytearray([fill]) * body_len
    if exec_off + 8 < body_len:
        body[exec_off + 6:exec_off + 9] = b"\xcd\x58\xd1"  # 初期化の CALL (MAKESYS が差し替える)
    body = bytes(body)
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


def bpb_sector(total=384, spc=4, root=512, spf=None, bps=512, res=1):
    """FAT12/16 の BPB (パーティション total セクタに合わせる)"""
    b = bytearray(512)
    b[0:3] = b"\xeb\x3c\x90"
    b[3:11] = b"MSDOS5.0"
    if spf is None:
        spf = 1
        while True:
            data_start = res + 2 * spf + (root * 32 + bps - 1) // bps
            clusters = (total - data_start) // spc
            bits = 12 if clusters < 4085 else 16
            if spf * bps >= ((clusters + 2) * bits + 7) // 8:
                break
            spf += 1
    tot16 = total if total < 0x10000 else 0
    struct.pack_into("<HBHBHHBH", b, 11, bps, spc, res, 2, root, tot16, 0xF8, spf)
    if not tot16:
        struct.pack_into("<I", b, 32, total)
    b[510:512] = b"\x55\xaa"
    return bytes(b)


def vhd_footer(size, disk_type=2, current=None, bad_checksum=False):
    """固定 VHD のフッター (VHD 仕様: 大きさは big endian、チェックサムは 64〜67 を除く和の 1 の補数)"""
    f = bytearray(512)
    f[0:8] = b"conectix"
    struct.pack_into(">IIQ", f, 8, 2, 0x00010000, 0xFFFFFFFFFFFFFFFF)
    struct.pack_into(">QQ", f, 40, size, size if current is None else current)
    struct.pack_into(">I", f, 60, disk_type)
    csum = (~(sum(f[:64]) + sum(f[68:])) & 0xFFFFFFFF) ^ (1 if bad_checksum else 0)
    struct.pack_into(">I", f, 64, csum)
    return bytes(f)


def fat_init(bpb):
    """BPB に合わせて FAT の先頭 (予約項目) を作った区画先頭のバイト列 (BPB + FAT の先頭)"""
    bps, spc, res, nf, root, tot16, media, spf = struct.unpack_from("<HBHBHHBH", bpb, 11)
    tot = tot16 or struct.unpack_from("<I", bpb, 32)[0]
    data_start = res + nf * spf + (root * 32 + bps - 1) // bps
    clusters = (tot - data_start) // spc
    head = bytes([media, 0xFF, 0xFF]) + (b"\xff" if clusters >= 4085 else b"")
    out = []
    for k in range(nf):
        out.append(((res + k * spf) * bps, head))
    return out


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
            bpb = bpb_sector(count)
            img[lba * 512:lba * 512 + 512] = bpb
            for off, head in fat_init(bpb):
                o = lba * 512 + off
                if o + len(head) <= len(img):
                    img[o:o + len(head)] = head
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
        ld = fake_ld_bin(top=0xFF51 - (n_ld - 7), body_len=n_ld - 7)
        self.assertEqual(len(ldhdd.makesys(ld, com)), 0xF000 - 0x200)
        ld2 = fake_ld_bin(top=0xFF51 - (n_ld - 6), body_len=n_ld - 6)
        with self.assertRaisesRegex(ldhdd.LdhddError, "大きすぎ"):
            ldhdd.makesys(ld2, com)

    def test_stub_scratch_area(self):
        # LD.BIN の終わりはスタブの作業場所 FF51h まで
        ldhdd.makesys(fake_ld_bin(top=0xFF51 - 0x2000, body_len=0x2000), b"\xc9")
        with self.assertRaisesRegex(ldhdd.LdhddError, "作業場所"):
            ldhdd.makesys(fake_ld_bin(top=0xFF52 - 0x2000, body_len=0x2000), b"\xc9")

    def test_needs_init_call(self):
        ld = bytearray(fake_ld_bin())
        ld[7 + 7 + 6] = 0x00  # exec+6 の CALL を消す
        with self.assertRaisesRegex(ldhdd.LdhddError, "CALL"):
            ldhdd.makesys(bytes(ld), b"\xc9")
        # 実行番地が終わりの直前 (CALL の飛び先が LD.BIN の外)
        short = fake_ld_bin(top=0xD100, body_len=0x100, exec_off=0x100 - 2)
        with self.assertRaisesRegex(ldhdd.LdhddError, "CALL"):
            ldhdd.makesys(short, b"\xc9")

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

    def test_160_track_table(self):
        # トラック表が 160 個 (0x2A0 まで) の D88 でも同じファイルが読める
        data = read(MKLDHD)
        size = struct.unpack_from("<I", data, 0x1C)[0]
        offs = [struct.unpack_from("<I", data, 0x20 + 4 * i)[0] for i in range(164)]
        self.assertEqual(min(o for o in offs if o), 0x2B0)
        hdr = bytearray(data[:0x2A0])
        struct.pack_into("<I", hdr, 0x1C, size - 16)
        for i in range(160):
            struct.pack_into("<I", hdr, 0x20 + 4 * i, offs[i] - 16 if offs[i] else 0)
        packed = bytes(hdr) + data[0x2B0:]
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t160.d88")
            with open(p, "wb") as f:
                f.write(packed)
            self.assertEqual(ldhdd.files_from_d88(p), ldhdd.files_from_d88(MKLDHD))

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

    def test_meta(self):
        mbr = bytearray(512)
        mbr[0x1FE:0x200] = b"\x55\xaa"
        payload = b"\xfe" + b"\x11" * 40
        rec = ldhdd.boot_record(bytes(mbr), payload)
        # 印は 0x190〜。0x80〜0xFF (コマンド行に読まれる所) を含め、それより前は asm と同じ
        self.assertEqual(rec[:0x190], ASM_BOOT_HEAD[:0x190])
        self.assertEqual(rec[0x190:0x198], b"LDHDDPY\x01")
        self.assertEqual(struct.unpack_from("<I", rec, 0x198)[0], len(payload))
        self.assertEqual(rec[0x19C:0x1B4], hashlib.sha256(payload).digest()[:24])
        self.assertEqual(rec[0x1B4:0x1B8], b"\xff" * 4)


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
        # 0x190〜0x1B3 は ldhdd.py の印 (長さと sha256)
        meta = b"LDHDDPY\x01" + struct.pack("<I", len(sys_bin)) + hashlib.sha256(sys_bin).digest()[:24]
        legacy_ipl = ASM_BOOT_HEAD[:0x190] + meta + ASM_BOOT_HEAD[0x1B4:0x1B8] + base[0x1B8:0x200] + sys_bin
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

    def test_tampered_old_payload_not_trusted(self):
        small = fake_ld_bin(top=0xE000, body_len=0x1000)
        v = self.vhd()
        self.quiet_update(v, inputs_from(small, self.com), backup=False)
        sys_small = ldhdd.makesys(small, self.com)
        old_n = len(sys_small)
        with open(v, "r+b") as f:
            # 外側の読み込み番地を 9 下げ、中の LD.BIN の終わりを 9 伸ばして、長さだけ辻褄を合わせる
            run = struct.unpack_from("<H", sys_small, 1)[0]
            f.seek(0x201)
            f.write(struct.pack("<H", run - 9))
            inner = 0x200 + 135 + len(self.com)
            f.seek(inner + 3)
            f.write(struct.pack("<H", 0xE000 + 0x1000 + 9))
            f.seek(0x200 + old_n)
            f.write(b"\x5a" * 9)
            f.seek(0)
            head = f.read(0xF000)
        self.assertEqual(ldhdd.existing_boot(head), ldhdd.ExistingBoot(True, 0))
        # 確かめられない前の起動部は「知らないデータ」なので、短い版でも --force が要る
        tiny = fake_ld_bin(top=0xE800, body_len=0x800)
        self.assert_refused(v, inputs_from(tiny, self.com))
        # --force でも、消すのは新しい起動部の範囲だけで、後ろの番兵は残る
        self.quiet_update(v, inputs_from(tiny, self.com), backup=False, force=True)
        self.assertEqual(read(v)[0x200 + old_n:0x200 + old_n + 9], b"\x5a" * 9)

    def test_same_end_shift_not_trusted(self):
        # 外側と中の終わりを同じだけ伸ばすと、ヘッダとスタブは噛み合ってしまう (R3-F1)。印で見破る
        small = fake_ld_bin(top=0xE000, body_len=0x1000)
        v = self.vhd()
        self.quiet_update(v, inputs_from(small, self.com), backup=False)
        sys_small = ldhdd.makesys(small, self.com)
        old_n = len(sys_small)
        with open(v, "r+b") as f:
            f.seek(0x203)
            f.write(struct.pack("<H", struct.unpack_from("<H", sys_small, 3)[0] + 9))
            inner = 0x200 + 135 + len(self.com)
            f.seek(inner + 3)
            f.write(struct.pack("<H", 0xE000 + 0x1000 + 9))
            f.seek(0x200 + old_n)
            f.write(b"\x5a" * 9)
            f.seek(0)
            head = f.read(0xF000)
        self.assertEqual(ldhdd.existing_boot(head), ldhdd.ExistingBoot(True, 0))
        tiny = fake_ld_bin(top=0xE800, body_len=0x800)
        self.assert_refused(v, inputs_from(tiny, self.com))
        self.quiet_update(v, inputs_from(tiny, self.com), backup=False, force=True)
        self.assertEqual(read(v)[0x200 + old_n:0x200 + old_n + 9], b"\x5a" * 9)

    def test_legacy_known_payload_trusted(self):
        # 旧手順で作った起動部 (印なし、MKLDHD.d88 の合体版そのもの) は既知の物として長さを信じる
        comb = ldhdd.files_from_d88(MKLDHD)["LD.BIN"]
        v = self.vhd()
        with open(v, "r+b") as f:
            mbr = f.read(512)
            f.seek(0)
            f.write(ldhdd.boot_record(mbr) + comb)
            f.seek(0)
            head = f.read(0xF000)
        self.assertEqual(ldhdd.existing_boot(head), ldhdd.ExistingBoot(True, len(comb)))
        # 短い版へ --force なしで置き換えられ、残りは 0 で消える
        small = fake_ld_bin(top=0xE000, body_len=0x1000)
        self.quiet_update(v, inputs_from(small, self.com), backup=False)
        n_small = 0x200 + len(ldhdd.makesys(small, self.com))
        self.assertEqual(read(v)[n_small:0x200 + len(comb)], bytes(0x200 + len(comb) - n_small))

    def test_legacy_unknown_payload_needs_force(self):
        comb = bytearray(ldhdd.files_from_d88(MKLDHD)["LD.BIN"])
        comb[200] ^= 0xFF  # COM の中身を 1 バイト変える (形は MAKESYS のまま)
        v = self.vhd()
        with open(v, "r+b") as f:
            mbr = f.read(512)
            f.seek(0)
            f.write(ldhdd.boot_record(mbr) + bytes(comb))
        self.assert_refused(v)
        self.quiet_update(v, backup=False, force=True)

    def write_part_bpb(self, v, bpb):
        with open(v, "r+b") as f:
            f.seek(128 * 512)
            f.write(bpb)
            for off, head in fat_init(bpb):
                f.seek(128 * 512 + off)
                f.write(head)

    def test_2048_sector_needs_eb(self):
        bpb = bpb_sector(96, bps=2048, spc=1, root=64)
        v = self.vhd("eb.vhd")
        self.write_part_bpb(v, bpb)
        self.quiet_update(v, backup=False)
        v2 = self.vhd("e9.vhd")
        self.write_part_bpb(v2, b"\xe9" + bpb[1:])
        with self.assertRaisesRegex(ldhdd.LdhddError, "EB"):
            self.quiet_update(v2, backup=False)

    def test_backup_failure_leaves_nothing(self):
        v = self.vhd()
        before = sha(v)
        orig = ldhdd.shutil.copyfileobj

        def fail(src, dst, n=0):
            dst.write(b"partial")
            raise OSError(28, "No space left on device")
        ldhdd.shutil.copyfileobj = fail
        try:
            with self.assertRaisesRegex(ldhdd.LdhddError, "バックアップを作れません"):
                self.quiet_update(v)
        finally:
            ldhdd.shutil.copyfileobj = orig
        self.assertEqual(sha(v), before)
        self.assertEqual(os.listdir(self.dir), ["t.vhd"])

    def test_data_start_wrap_refused(self):
        # ルートの位置 (65535) と ADDCL16 (65528) は 16 ビットに収まるが、データ領域の位置が 65536 になる
        lba = 128
        count = 65536 + 4 * 128
        v = self.vhd("wrap.vhd", sectors=lba + count, parts=((0x06, lba, count),))
        bpb = bpb_sector(count, spc=4, root=16, spf=32767)
        self.write_part_bpb(v, bpb)
        with self.assertRaisesRegex(ldhdd.LdhddError, "データ領域の位置"):
            self.quiet_update(v, backup=False)
        # 1 つ手前 (データ領域の位置 65534) は通る
        ldhdd.check_fat_bpb(bpb_sector(count, spc=4, root=16, spf=32766), count)

    def test_unbuffered_target_and_failure_hint(self):
        v = self.vhd()
        with ldhdd._open_target(v, True) as f:
            self.assertIsInstance(f, io.FileIO)  # 閉じる時に書き直すバッファが無い
        orig = ldhdd.os.fsync
        calls = []

        target_ino = os.stat(v).st_ino

        def fsync(fd):
            calls.append(fd)
            if os.fstat(fd).st_ino == target_ino:  # 対象への書き込みの後の fsync だけ失敗させる
                raise OSError(5, "I/O error")
            return orig(fd)
        ldhdd.os.fsync = fsync
        try:
            with self.assertRaises(ldhdd.LdhddError) as cm:
                self.quiet_update(v)
        finally:
            ldhdd.os.fsync = orig
        baks = [x for x in os.listdir(self.dir) if x.endswith(".bak")]
        self.assertEqual(len(baks), 1)
        self.assertIn(os.path.join(self.dir, baks[0]), str(cm.exception))
        self.assertIn("戻してください", str(cm.exception))

    def test_backup_never_replaces_or_deletes(self):
        v = self.vhd()
        existing = os.path.join(self.dir, "t.vhd.taken.bak")
        stray = os.path.join(self.dir, "t.vhd.taken.bak.part")
        for p in (existing, stray):
            with open(p, "wb") as f:
                f.write(b"keep")
        orig = ldhdd._backup_path
        ldhdd._backup_path = lambda path: existing
        try:
            before = sha(v)
            with self.assertRaisesRegex(ldhdd.LdhddError, "バックアップを作れません"):
                self.quiet_update(v)
        finally:
            ldhdd._backup_path = orig
        self.assertEqual(sha(v), before)
        self.assertEqual(read(existing), b"keep")
        self.assertEqual(read(stray), b"keep")
        self.assertEqual(sorted(os.listdir(self.dir)), ["t.vhd", "t.vhd.taken.bak", "t.vhd.taken.bak.part"])

    def test_tagged_payload_without_call_is_replaceable(self):
        # 以前の版 (CALL を確かめていなかった) が書いて印を付けた起動部も、前の起動部として扱う
        ld = bytearray(fake_ld_bin(top=0xE000, body_len=0x1000))
        ld[7 + 7 + 6] = 0x00
        old = ldhdd.assemble_sys(bytes(ld), self.com)
        v = self.vhd()
        with open(v, "r+b") as f:
            mbr = f.read(512)
            f.seek(0)
            f.write(ldhdd.boot_record(mbr, old) + old)
            f.seek(0)
            head = f.read(0xF000)
        self.assertEqual(ldhdd.existing_boot(head), ldhdd.ExistingBoot(True, len(old)))
        tiny = fake_ld_bin(top=0xE800, body_len=0x800)
        self.quiet_update(v, inputs_from(tiny, self.com), backup=False)
        n_new = 0x200 + len(ldhdd.makesys(tiny, self.com))
        self.assertEqual(read(v)[n_new:0x200 + len(old)], bytes(0x200 + len(old) - n_new))

    @unittest.skipIf(os.name == "nt", "ハードリンクのテスト")
    def test_backup_without_hardlink_stops(self):
        # ハードリンクが使えないファイルシステムでは、正式な名前を作る前に止め、--no-backup を案内する
        import errno as _errno
        v = self.vhd()
        before = sha(v)
        orig_link = ldhdd.os.link

        def nolink(a, b, **kw):
            raise OSError(_errno.EPERM, "Operation not permitted")
        ldhdd.os.link = nolink
        try:
            with self.assertRaisesRegex(ldhdd.LdhddError, "--no-backup"):
                self.quiet_update(v)
        finally:
            ldhdd.os.link = orig_link
        self.assertEqual(sha(v), before)
        self.assertEqual(os.listdir(self.dir), ["t.vhd"])
        # --no-backup なら書ける
        self.quiet_update(v, backup=False)

    @unittest.skipIf(os.name == "nt", "ハードリンクのテスト")
    def test_backup_interrupted_leaves_nothing(self):
        # Ctrl-C で止まっても、正式な名前の物も仮ファイルも残らない
        v = self.vhd()
        before = sha(v)
        orig_link = ldhdd.os.link

        def interrupt(a, b, **kw):
            raise KeyboardInterrupt
        ldhdd.os.link = interrupt
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.quiet_update(v)
        finally:
            ldhdd.os.link = orig_link
        self.assertEqual(sha(v), before)
        self.assertEqual(os.listdir(self.dir), ["t.vhd"])

    @unittest.skipIf(os.name == "nt", "ハードリンクのテスト")
    def test_backup_content_matches_original(self):
        v = self.vhd()
        base = read(v)
        bak = self.quiet_update(v)
        self.assertEqual(read(bak), base)

    @unittest.skipIf(os.name == "nt", "ハードリンクのテスト")
    def test_backup_name_taken_during_publish(self):
        v = self.vhd()
        before = sha(v)
        orig = ldhdd.os.link
        taken = []

        def racing_link(a, b, **kw):
            p = os.path.join(self.dir, b)
            with open(p, "wb") as f:  # 名前を付ける直前に、ほかの誰かが同じ名前を作る
                f.write(b"other")
            taken.append(p)
            return orig(a, b, **kw)
        ldhdd.os.link = racing_link
        try:
            with self.assertRaisesRegex(ldhdd.LdhddError, "バックアップを作れません"):
                self.quiet_update(v)
        finally:
            ldhdd.os.link = orig
        self.assertEqual(sha(v), before)
        self.assertEqual(read(taken[0]), b"other")
        self.assertEqual(sorted(os.listdir(self.dir)), sorted(["t.vhd", os.path.basename(taken[0])]))

    @unittest.skipIf(os.name == "nt", "ハードリンクのテスト")
    def test_backup_staging_cleanup_failure_is_not_fatal(self):
        v = self.vhd()
        orig = ldhdd.os.remove

        def noremove(p):
            if str(p).endswith(".part"):
                raise OSError(13, "Permission denied")
            return orig(p)
        ldhdd.os.remove = noremove
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                bak = self.quiet_update(v)
        finally:
            ldhdd.os.remove = orig
        self.assertTrue(os.path.exists(bak))
        self.assertIn("消せませんでした", err.getvalue())

    @unittest.skipIf(os.name == "nt", "ディレクトリの同期のテスト")
    def test_backup_dir_fsync_observed(self):
        # os.fsync をディレクトリの記述子で呼んだ時点で、バックアップが正式な名前で既にあること。
        # そして、それがイメージへの書き込みより前であること
        import stat as _stat
        v = self.vhd()
        events = []
        orig_fsync, orig_write = ldhdd.os.fsync, ldhdd._write_all

        def fsync(fd):
            if _stat.S_ISDIR(os.fstat(fd).st_mode):
                events.append(("dir", sorted(x for x in os.listdir(self.dir) if x.endswith(".bak"))))
            return orig_fsync(fd)
        ldhdd.os.fsync = fsync
        ldhdd._write_all = lambda f, d: (events.append(("write", None)), orig_write(f, d))[1]
        try:
            bak = self.quiet_update(v)
        finally:
            ldhdd.os.fsync, ldhdd._write_all = orig_fsync, orig_write
        self.assertEqual(events, [("dir", [os.path.basename(bak)]), ("write", None)])

    @unittest.skipIf(os.name == "nt", "ディレクトリの同期のテスト")
    def test_backup_dir_fsync_failure_keeps_image(self):
        import stat as _stat
        v = self.vhd()
        before = sha(v)
        orig = ldhdd.os.fsync

        def fsync(fd):
            if _stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(5, "I/O error")
            return orig(fd)
        ldhdd.os.fsync = fsync
        try:
            with self.assertRaisesRegex(ldhdd.LdhddError, "何も書き込んでいません") as cm:
                self.quiet_update(v)
        finally:
            ldhdd.os.fsync = orig
        self.assertEqual(sha(v), before)
        baks = [x for x in os.listdir(self.dir) if x.endswith(".bak")]
        self.assertEqual(len(baks), 1)
        self.assertIn(baks[0], str(cm.exception))

    class FakeKernel32:
        """MoveFileExW の偽物。MOVEFILE_REPLACE_EXISTING (0x1) が立っていれば置き換える"""
        def __init__(self, after_move=None):
            self.calls = []
            self.after_move = after_move

        def MoveFileExW(self, src, dst, flags):
            self.calls.append(flags)
            if os.path.exists(dst) and not flags & 0x1:
                return 0
            os.replace(src, dst)
            if self.after_move:
                self.after_move(dst)
            return 1

    def test_windows_branch_through_update(self):
        # Windows の名前付け (MoveFileExW の偽物) を通して update する: 中身・名前のぶつかり・仮ファイルの後始末
        k = self.FakeKernel32()
        v = self.vhd()
        base = read(v)
        orig = (ldhdd._IS_WINDOWS, ldhdd._windows_move_no_replace)
        ldhdd._IS_WINDOWS = True
        ldhdd._windows_move_no_replace = lambda s_, d_: orig[1](s_, d_, kernel32=k)
        try:
            bak = self.quiet_update(v)
            self.assertEqual(read(bak), base)
            self.assertEqual(k.calls, [0x8])
            orig_path = ldhdd._backup_path
            ldhdd._backup_path = lambda path: bak
            try:
                before = sha(v)
                with self.assertRaisesRegex(ldhdd.LdhddError, "バックアップを作れません"):
                    self.quiet_update(v)
                self.assertEqual(sha(v), before)
            finally:
                ldhdd._backup_path = orig_path
        finally:
            ldhdd._IS_WINDOWS, ldhdd._windows_move_no_replace = orig
        self.assertEqual(read(bak), base)
        self.assertEqual(sorted(os.listdir(self.dir)), sorted(["t.vhd", os.path.basename(bak)]))

    def test_windows_backup_move(self):
        # Windows の名前付けは MoveFileExW(MOVEFILE_WRITE_THROUGH = 0x8 のみ。REPLACE_EXISTING = 0x1 は付けない)
        k = self.FakeKernel32()
        src = os.path.join(self.dir, "s.part")
        dst = os.path.join(self.dir, "w.bak")
        with open(src, "wb") as f:
            f.write(b"data")
        ldhdd._windows_move_no_replace(src, dst, kernel32=k)
        self.assertEqual(k.calls, [0x8])
        self.assertEqual(read(dst), b"data")
        # 既にある名前には失敗する (置き換えるフラグなら、ここで置き換わってテストが落ちる)
        with open(src, "wb") as f:
            f.write(b"new")
        with self.assertRaises(OSError):
            ldhdd._windows_move_no_replace(src, dst, kernel32=k)
        self.assertEqual(read(dst), b"data")

    @unittest.skipIf(os.name == "nt", "シンボリックリンクのテスト")
    def test_backup_next_to_image_with_symlink_dotdot(self):
        # lnk -> a/b のとき、lnk/../t.vhd はカーネルでは a/t.vhd。バックアップも a/ にできること
        a = os.path.join(self.dir, "a")
        os.makedirs(os.path.join(a, "b"))
        make_vhd(os.path.join(a, "t.vhd"))
        os.symlink(os.path.join(a, "b"), os.path.join(self.dir, "lnk"))
        v = os.path.join(self.dir, "lnk", "..", "t.vhd")
        created = []
        orig_open = ldhdd.os.open

        def rec_open(p, flags, *a, **kw):
            if str(p).endswith(".part"):
                created.append(p)
            return orig_open(p, flags, *a, **kw)
        ldhdd.os.open = rec_open
        try:
            bak = self.quiet_update(v)
        finally:
            ldhdd.os.open = orig_open
        # 仮ファイルもバックアップと同じディレクトリ (a/) に作られたこと
        self.assertEqual(len(created), 1)
        self.assertTrue(os.path.samefile(os.path.dirname(created[0]), a))
        self.assertTrue(os.path.samefile(os.path.dirname(bak), a))
        self.assertEqual([x for x in os.listdir(a) if x.endswith(".bak")], [os.path.basename(bak)])
        self.assertEqual([x for x in os.listdir(self.dir) if x.endswith(".bak")], [])

    @unittest.skipIf(os.name == "nt", "Windows はパス全体の長さの上限に先にかかる")
    def test_long_image_name(self):
        # 名前の長さの上限 (255 バイト) 近くの名前でも、仮ファイルの名前が長くなりすぎない
        name = "x" * 226 + ".vhd"
        v = self.vhd(name)
        bak = self.quiet_update(v)
        self.assertTrue(os.path.exists(bak))

    @unittest.skipIf(os.name == "nt", "flock のテスト")
    def test_locked_by_other_process(self):
        import subprocess
        v = self.vhd()
        before = sha(v)
        code = ("import fcntl,sys; f=open(sys.argv[1],'r+b'); fcntl.flock(f.fileno(), fcntl.LOCK_EX);"
                "print('locked', flush=True); sys.stdin.read()")
        p = subprocess.Popen([sys.executable, "-c", code, v], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self.assertEqual(p.stdout.readline().strip(), b"locked")
            with self.assertRaisesRegex(ldhdd.LdhddError, "ほかのプロセス"):
                self.quiet_update(v, backup=False)
            self.assertEqual(sha(v), before)
        finally:
            p.stdin.close()
            p.wait(10)
            p.stdout.close()
        self.quiet_update(v, backup=False)

    def test_refuse_vhd_footer_mismatch(self):
        v = self.vhd()
        with open(v, "r+b") as f:
            f.seek(-512, 2)
            f.write(vhd_footer(512 * 512, bad_checksum=True))
        self.assert_refused(v)
        with open(v, "r+b") as f:
            f.seek(-512, 2)
            f.write(vhd_footer(512 * 512, current=256 * 512))
        self.assert_refused(v)

    def test_refuse_empty_fat(self):
        v = self.vhd()
        with open(v, "r+b") as f:
            f.seek(129 * 512)
            f.write(bytes(4))
        self.assert_refused(v)

    def test_refuse_4096_sector(self):
        v = self.vhd()
        bpb = bpb_sector(384 // 8, spc=1, root=128, bps=4096)
        with open(v, "r+b") as f:
            f.seek(128 * 512)
            f.write(bpb)
            for off, head in fat_init(bpb):
                f.seek(128 * 512 + off)
                f.write(head)
        with self.assertRaisesRegex(ldhdd.LdhddError, "セクタサイズ 4096"):
            self.quiet_update(v, backup=False)

    def test_refuse_partition_over_512mb(self):
        v = os.path.join(self.dir, "big.vhd")
        sectors = (512 * 1024 * 1024) // 512 + 2048
        count = sectors - 128
        mbr = bytearray(512)
        mbr[0x1BE:0x1CE] = struct.pack("<B3sB3sII", 0, b"\0\0\0", 0x06, b"\0\0\0", 128, count)
        mbr[510:512] = b"\x55\xaa"
        bpb = bpb_sector(count, spc=64)
        with open(v, "wb") as f:
            f.write(mbr)
            f.seek(128 * 512)
            f.write(bpb)
            for off, head in fat_init(bpb):
                f.seek(128 * 512 + off)
                f.write(head)
            f.truncate(sectors * 512)
            f.seek(sectors * 512)
            f.write(vhd_footer(sectors * 512))
        with self.assertRaisesRegex(ldhdd.LdhddError, "512MB"):
            self.quiet_update(v, backup=False)

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


class TestWindowsLock(unittest.TestCase):
    """msvcrt.locking を偽物に差し替えて、記述子の位置が 0 で呼ばれることを確かめる"""

    class FakeMsvcrt:
        LK_NBLCK = 2
        LK_UNLCK = 0

        def __init__(self, fail_unlock=False):
            self.calls = []
            self.fail_unlock = fail_unlock

        def locking(self, fd, mode, n):
            self.calls.append((mode, os.lseek(fd, 0, os.SEEK_CUR), n))
            if mode == self.LK_UNLCK and self.fail_unlock:
                raise OSError("unlock failed")

    def test_positions(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "x.bin")
            with open(p, "wb") as f:
                f.write(bytes(100000))
            for fail in (False, True):
                fake = self.FakeMsvcrt(fail_unlock=fail)
                with open(p, "r+b") as f:
                    f.read(3000)  # バッファで記述子の位置が先に進む
                    unlock = ldhdd._lock(f, fake)
                    f.seek(0)
                    f.read(100)  # 短い読み戻し (バッファの中で済み、記述子は先にある)
                    f.seek(0)
                    unlock()  # 失敗しても例外にしない
                self.assertEqual(fake.calls, [(2, 0, 0xF000), (0, 0, 0xF000)])


class TestDpbLimits(unittest.TestCase):
    def check(self, bpb, part_sectors):
        ldhdd.check_fat_bpb(bpb, part_sectors)

    def test_reserved_sectors(self):
        self.check(bpb_sector(4000, res=255), 4000)
        with self.assertRaisesRegex(ldhdd.LdhddError, "予約セクタ"):
            self.check(bpb_sector(4000, res=256), 4000)
        # 2048 バイトのセクタは HDD.COM が倍にするので 127 まで
        self.check(bpb_sector(1000, bps=2048, spc=1, root=512, res=127), 4000)
        with self.assertRaisesRegex(ldhdd.LdhddError, "予約セクタ"):
            self.check(bpb_sector(1000, bps=2048, spc=1, root=512, res=128), 4000)

    def test_2048_data_offset(self):
        # 2048 バイトのセクタでは、データ領域の位置 - 2 クラスタを倍にした値が 16 ビットに収まること
        def bpb(spf):
            data_start = 1 + 2 * spf + 512 * 32 // 2048
            return bpb_sector(data_start + 100, bps=2048, spc=1, root=512, spf=spf)
        big = 40000 * 4
        # ADDCL16 = (データ領域の位置 - 2) を倍にして 16 ビットに収まるのは spf=16380 まで
        self.check(bpb(16380), big)
        with self.assertRaisesRegex(ldhdd.LdhddError, "データ領域の位置"):
            self.check(bpb(16381), big)
        # 倍にする前のデータ領域の位置が 32768 で、倍にした後のルートの終わりが 65535 になる形は使える
        self.check(bpb_sector(32768 + 100, bps=2048, spc=1, root=448, spf=16380), big)

    def test_root_dir_sectors(self):
        with self.assertRaisesRegex(ldhdd.LdhddError, "ルートディレクトリ"):
            self.check(bpb_sector(20000, root=4096 + 16), 20000)  # 257 セクタ


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


class TestNewParts(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(ldhdd.parse_size("128M"), 128 << 20)
        self.assertEqual(ldhdd.parse_size("128m"), 128 << 20)
        self.assertEqual(ldhdd.parse_size("128"), 128 << 20)
        self.assertEqual(ldhdd.parse_size("1024K"), 1 << 20)
        self.assertEqual(ldhdd.parse_size("512M"), 512 << 20)
        for bad in ("0", "513M", "1K", "1.5M", "abc", "-1M", "", "9" * 5000):
            with self.assertRaises(ldhdd.LdhddError, msg=bad):
                ldhdd.parse_size(bad)

    def test_choose_fat_layout(self):
        mb = 2048  # 1MB のセクタ数
        cases = [(1 * mb, 12, None), (15 * mb, 12, 8), (32767, 12, None), (32768, 16, 4),
                 (128 * mb, 16, 4), (129 * mb, 16, 8), (256 * mb, 16, 8), (512 * mb - 128, 16, 16)]
        for total, bits, spc in cases:
            lay = ldhdd.choose_fat_layout(total)
            self.assertEqual(lay.fat_bits, bits, total)
            if spc:
                self.assertEqual(lay.spc, spc, total)
            self.assertTrue(lay.clusters < 4085 if bits == 12 else 4085 <= lay.clusters <= 65524, total)
            ldhdd.check_fat_bpb(ldhdd.build_bpb(lay, 128, ldhdd.NO_LABEL, 1), total)

    def test_forced_fat(self):
        mb = 2048
        lay = ldhdd.choose_fat_layout(128 * mb - 128, "12")
        self.assertEqual((lay.fat_bits, lay.spc), (12, 128))
        self.assertLess(lay.clusters, 4085)
        # データ領域の位置が 2 クラスタ分 (256 セクタ) 以上になるよう、予約セクタを足している
        self.assertGreaterEqual(lay.reserved + 2 * lay.spf + 32, 2 * lay.spc)
        with self.assertRaisesRegex(ldhdd.LdhddError, "FAT12 にできません"):
            ldhdd.choose_fat_layout(512 * mb - 128, "12")
        with self.assertRaisesRegex(ldhdd.LdhddError, "FAT16 にできません"):
            ldhdd.choose_fat_layout(1 * mb - 128, "16")
        lay = ldhdd.choose_fat_layout(5 * mb - 128, "16")
        self.assertEqual(lay.fat_bits, 16)
        self.assertGreaterEqual(lay.clusters, 4085)
        self.assertLess(lay.spc, 4)

    def test_parse_label(self):
        self.assertEqual(ldhdd.parse_label("ABCDEFGHIJK"), b"ABCDEFGHIJK")
        self.assertEqual(ldhdd.parse_label("ldhdd"), b"LDHDD      ")
        for bad in ("ABCDEFGHIJKL", "", " X", "A*B", "A.B", 'A"B', "ラベル"):
            with self.assertRaises(ldhdd.LdhddError, msg=bad):
                ldhdd.parse_label(bad)

    def test_vhd_geometry(self):
        mb = 2048
        self.assertEqual(ldhdd.vhd_geometry(128 * mb), (963, 16, 17))
        self.assertEqual(ldhdd.vhd_geometry(192 * mb), (393216 // 31 // 16, 16, 31))
        self.assertEqual(ldhdd.vhd_geometry(256 * mb), (524288 // 63 // 16, 16, 63))
        self.assertEqual(ldhdd.vhd_geometry(512 * mb), (1048576 // 63 // 16, 16, 63))
        self.assertEqual(ldhdd.vhd_geometry(1 * mb), (2048 // 17 // 4, 4, 17))

    def test_footer_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "x.vhd")
            with open(p, "wb") as f:
                f.truncate(1 << 20)
                f.seek(1 << 20)
                f.write(ldhdd.build_vhd_footer(1 << 20, 12345, b"\x01" * 16))
            with ldhdd._open_target(p, False) as f:
                self.assertEqual(ldhdd.vhd_kind_of(f), ldhdd.VhdKind("fixed", 1 << 20))


class TestNew(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = self.td.name
        self.com = read(HDD_COM)
        self.ld = ldhdd.files_from_d88(MKLDHD)["LD.BIN"][135 + len(self.com):]
        self.inputs = inputs_from(self.ld, self.com)

    def tearDown(self):
        self.td.cleanup()

    def new(self, name="n.vhd", size="16M", **kw):
        path = os.path.join(self.dir, name)
        lay = ldhdd.new_image(path, ldhdd.parse_size(size), kw.pop("inputs", self.inputs), kw.pop("paths", []),
                              out=io.StringIO(), **kw)
        return path, lay

    def part_bpb(self, path):
        with open(path, "rb") as f:
            f.seek(128 * 512)
            return f.read(512)

    def test_sizes_and_types(self):
        for size, ptype, bits in (("1M", 0x01, 12), ("15M", 0x01, 12), ("16M", 0x01, 12),
                                  ("16448K", 0x04, 16), ("128M", 0x06, 16)):
            path, lay = self.new("t%s.vhd" % size, size)
            data_size = ldhdd.parse_size(size)
            self.assertEqual(os.path.getsize(path), data_size + 512, size)
            self.assertEqual(lay.fat_bits, bits, size)
            with open(path, "rb") as f:
                head = f.read(0xF000)
            p1 = ldhdd.read_partitions(head)[0]
            self.assertEqual((p1.ptype, p1.lba, p1.count), (ptype, 128, data_size // 512 - 128), size)
            # A1 の検査をすべて通る
            with ldhdd._open_target(path, False) as f:
                ldhdd.check_target(f, len(ldhdd.makesys(self.ld, self.com)))
            sys_bin = ldhdd.makesys(self.ld, self.com)
            self.assertEqual(head[0x200:0x200 + len(sys_bin)], sys_bin)
            self.assertEqual(head[0x190:0x190 + ldhdd.META_LEN], ldhdd.boot_meta(sys_bin))
            self.assertNotEqual(head[0x1B8:0x1BC], bytes(4))
            run_main(["info", path])
            self.assertIn("パーティション 1: 種類 %02X 開始 LBA 128" % ptype, run_main.out)
            self.assertIn("FAT%d" % bits, run_main.out)
            self.assertIn("version 1.61b", run_main.out)

    def test_unit_label_and_update_after(self):
        path, lay = self.new(size="128M", unit=1, label="ldhdd")
        bpb = self.part_bpb(path)
        self.assertEqual(bpb[43:54], b"LDHDD      ")
        with open(path, "rb") as f:
            f.seek((128 + 1 + 2 * lay.spf) * 512)
            ent = f.read(32)
        self.assertEqual(ent[:11], b"LDHDD      ")
        self.assertEqual(ent[11], 0x08)
        run_main(["info", path])
        self.assertIn("スタブで 1 を指定", run_main.out)
        # 作った物にそのまま update をかけられる
        ld2 = fake_ld_bin(top=0xD100, body_len=9721, fill=0x77)
        ldhdd.update(path, inputs_from(ld2, self.com), backup=False, out=io.StringIO())
        with open(path, "rb") as f:
            f.seek(0x200)
            self.assertEqual(f.read(len(ldhdd.makesys(ld2, self.com))), ldhdd.makesys(ld2, self.com))

    def test_forced_fat12_128m_end_to_end(self):
        path, lay = self.new("f12.vhd", "128M", fat="12")
        self.assertEqual((lay.fat_bits, lay.spc), (12, 128))
        # update --dry-run (A1 の検査) も通る
        self.assertEqual(run_main(["update", path, "--dry-run", "--ldsys", MKLDHD, "--ld-bin", self.raw_ld()]), 0, run_main.err)
        if shutil.which("fsck.fat"):
            self.assertEqual(self.fsck(path), 0)

    def raw_ld(self):
        p = os.path.join(self.dir, "rawld.bin")
        if not os.path.exists(p):
            with open(p, "wb") as f:
                f.write(self.ld)
        return p

    def fsck(self, path):
        import subprocess
        data = read(path)
        part = os.path.join(self.dir, "part.img")
        with open(part, "wb") as f:
            f.write(data[128 * 512:len(data) - 512])
        r = subprocess.run([shutil.which("fsck.fat"), "-n", part], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        os.remove(part)
        return r.returncode

    def test_info_truncated_and_fat32(self):
        # パーティションがファイルの外 / FAT32 の BPB でも info は落ちない
        path = os.path.join(self.dir, "x.vhd")
        mbr = bytearray(512)
        mbr[0x1BE:0x1CE] = struct.pack("<B3sB3sII", 0, b"\0\0\0", 0x06, b"\0\0\0", 5000, 100)
        mbr[510:512] = b"\x55\xaa"
        with open(path, "wb") as f:
            f.write(bytes(mbr) + bytes(0x10000))
        self.assertEqual(run_main(["info", path]), 0, run_main.err)
        self.assertIn("BPB を読めません", run_main.out)
        path2, lay = self.new("y.vhd", "16M")
        with open(path2, "r+b") as f:
            f.seek(128 * 512 + 22)
            f.write(b"\0\0")  # FAT16 の spf を 0 に (FAT32 の形)
        self.assertEqual(run_main(["info", path2]), 0, run_main.err)
        self.assertIn("FAT32", run_main.out)

    @unittest.skipIf(os.name == "nt", "Windows は開いているファイルを置き換えられない")
    def test_failed_creator_keeps_others_output(self):
        # 失敗した時の後始末は、自分が作ったファイルのままの時だけ消す
        path = os.path.join(self.dir, "n.vhd")
        orig = ldhdd._write_all
        calls = []
        replaced = []

        def fail(f, d):
            calls.append(1)
            if len(calls) == 2:
                other = os.path.join(self.dir, "other.tmp")
                with open(other, "wb") as o:
                    o.write(b"other")
                os.replace(other, path)  # ほかの誰かがその名前に置いた
                replaced.append(True)
                raise OSError(5, "injected")
            return orig(f, d)
        ldhdd._write_all = fail
        try:
            with self.assertRaisesRegex(ldhdd.LdhddError, "injected"):
                self.new()
        finally:
            ldhdd._write_all = orig
        self.assertEqual(replaced, [True])
        self.assertEqual(read(path), b"other")

    def test_fstat_failure_leaves_nothing(self):
        orig = ldhdd.os.fstat
        for force in (False, True):
            path = os.path.join(self.dir, "n.vhd")
            if force:
                with open(path, "wb") as f:
                    f.write(b"keep")
            state = {"done": False}

            def fstat(fd):
                if not state["done"]:
                    state["done"] = True
                    raise OSError(5, "injected")
                return orig(fd)
            ldhdd.os.fstat = fstat
            try:
                with self.assertRaisesRegex(ldhdd.LdhddError, "injected"):
                    self.new(force=force)
            finally:
                ldhdd.os.fstat = orig
            self.assertEqual(os.listdir(self.dir), ["n.vhd"] if force else [], force)
            if force:
                self.assertEqual(read(path), b"keep")
                os.remove(path)

    class FakeKernel32:
        """MoveFileExW の偽物。MOVEFILE_REPLACE_EXISTING (0x1) が立っていれば置き換える。fail で失敗させる"""
        def __init__(self, fail=False):
            self.calls = []
            self.fail = fail

        def MoveFileExW(self, src, dst, flags):
            self.calls.append(flags)
            if self.fail or (os.path.exists(dst) and not flags & 0x1):
                return 0
            os.replace(src, dst)
            return 1

    def windows_mode(self, k):
        orig = (ldhdd._IS_WINDOWS, ldhdd._windows_move)
        ldhdd._IS_WINDOWS = True
        ldhdd._windows_move = lambda s_, d_, fl, kernel32=None: orig[1](s_, d_, fl, kernel32=k)
        return orig

    def test_windows_force_replace_write_through(self):
        path = os.path.join(self.dir, "n.vhd")
        with open(path, "wb") as f:
            f.write(b"keep")
        k = self.FakeKernel32()
        orig = self.windows_mode(k)
        try:
            self.new(force=True)
        finally:
            ldhdd._IS_WINDOWS, ldhdd._windows_move = orig
        self.assertEqual(k.calls, [0x1 | 0x8])  # REPLACE_EXISTING | WRITE_THROUGH
        self.assertEqual(os.path.getsize(path), (16 << 20) + 512)
        self.assertEqual(os.listdir(self.dir), ["n.vhd"])
        # 置き換えに失敗したら、元のファイルはそのまま・仮ファイルは残らない
        with open(path, "wb") as f:
            f.write(b"keep")
        k2 = self.FakeKernel32(fail=True)
        orig = self.windows_mode(k2)
        try:
            with self.assertRaisesRegex(ldhdd.LdhddError, "元のファイルはそのまま"):
                self.new(force=True)
        finally:
            ldhdd._IS_WINDOWS, ldhdd._windows_move = orig
        self.assertEqual(read(path), b"keep")
        self.assertEqual(os.listdir(self.dir), ["n.vhd"])

    def test_cleanup_failure_is_reported(self):
        orig_w, orig_rm = ldhdd._write_all, ldhdd.os.remove
        calls = []

        def fail(f, d):
            calls.append(1)
            if len(calls) == 2:
                raise OSError(5, "injected")
            return orig_w(f, d)

        def norm(p):
            raise OSError(30, "Read-only file system")
        ldhdd._write_all, ldhdd.os.remove = fail, norm
        try:
            with self.assertRaisesRegex(ldhdd.LdhddError, "消せませんでした") as cm:
                self.new()
        finally:
            ldhdd._write_all, ldhdd.os.remove = orig_w, orig_rm
        self.assertIn("injected", str(cm.exception))
        self.assertNotIn("何も残していません", str(cm.exception))

    def test_info_bpb_larger_than_partition(self):
        path, lay = self.new("b.vhd", "2M")
        with open(path, "r+b") as f:
            f.seek(128 * 512 + 19)
            f.write(b"\0\0")
            f.seek(128 * 512 + 32)
            f.write(struct.pack("<I", 261632))  # 127.75MB を名乗る
        run_main(["info", path])
        self.assertIn("パーティション", run_main.out)
        self.assertIn("を越えています", run_main.out)
        self.assertNotIn("127.", run_main.out)

    def test_info_label_needs_signature_and_is_sanitized(self):
        path, lay = self.new("l.vhd", "2M", label="ABC")
        with open(path, "r+b") as f:
            f.seek(128 * 512 + 43)
            f.write(b"A\x1b[2JB")
        run_main(["info", path])
        self.assertIn("ラベル A?[2JB", run_main.out)
        self.assertNotIn("\x1b", run_main.out)
        with open(path, "r+b") as f:
            f.seek(128 * 512 + 38)
            f.write(b"\x00")
        run_main(["info", path])
        self.assertIn("ラベル (なし)", run_main.out)

    @unittest.skipUnless(shutil.which("fsck.fat"), "fsck.fat が無い")
    def test_fsck_fat(self):
        import subprocess
        for size in ("1M", "16M", "16448K", "128M"):
            path, lay = self.new("f%s.vhd" % size, size, label="TEST")
            data = read(path)
            part = os.path.join(self.dir, "part.img")
            with open(part, "wb") as f:
                f.write(data[128 * 512:len(data) - 512])
            r = subprocess.run([shutil.which("fsck.fat"), "-n", part], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertEqual(r.returncode, 0, (size, r.stdout))

    def test_existing_and_force(self):
        path = os.path.join(self.dir, "n.vhd")
        with open(path, "wb") as f:
            f.write(b"keep")
        with self.assertRaisesRegex(ldhdd.LdhddError, "既にあります"):
            self.new()
        self.assertEqual(read(path), b"keep")
        self.new(force=True)
        self.assertEqual(os.path.getsize(path), (16 << 20) + 512)
        self.assertEqual(sorted(os.listdir(self.dir)), ["n.vhd"])

    def test_same_file_as_input(self):
        for opt in ("--ldsys", "--ld-bin", "--hdd-com"):
            p = os.path.join(self.dir, "in" + opt.strip("-").replace("-", "") + ".bin")
            if opt == "--ldsys":
                src = read(MKLDHD)
            elif opt == "--ld-bin":
                src = self.ld
            else:
                src = self.com
            with open(p, "wb") as f:
                f.write(src)
            args = ["new", p, "--size", "16M", "--force", opt, p]
            if opt == "--ldsys":
                # MKLDHD.d88 の LD.BIN は合体版なので、素の LD.BIN は別に渡す (HDD.COM は p から取る)
                raw = os.path.join(self.dir, "raw.bin")
                with open(raw, "wb") as f:
                    f.write(self.ld)
                args += ["--ld-bin", raw]
            else:
                args += ["--ldsys", MKLDHD]
                if opt == "--hdd-com":
                    raw = os.path.join(self.dir, "raw.bin")
                    with open(raw, "wb") as f:
                        f.write(self.ld)
                    args += ["--ld-bin", raw]
            self.assertEqual(run_main(args), 1, opt)
            self.assertIn("同じファイル", run_main.err)
            self.assertEqual(read(p), src)

    def test_input_errors_create_nothing(self):
        bad = inputs_from(b"\xff" * 100, self.com)
        with self.assertRaises(ldhdd.LdhddError):
            self.new(inputs=bad)
        with self.assertRaises(ldhdd.LdhddError):
            self.new(label="BAD*")
        with self.assertRaises(ldhdd.LdhddError):
            self.new(size="1M", fat="16")
        self.assertEqual(os.listdir(self.dir), [])

    def test_failures_leave_nothing(self):
        orig = ldhdd._write_all
        for exc in (OSError(5, "I/O error"), KeyboardInterrupt()):
            calls = []

            def fail(f, d, exc=exc):
                calls.append(1)
                if len(calls) == 2:
                    raise exc
                return orig(f, d)
            ldhdd._write_all = fail
            try:
                with self.assertRaises((ldhdd.LdhddError, KeyboardInterrupt)):
                    self.new()
            finally:
                ldhdd._write_all = orig
            self.assertEqual(os.listdir(self.dir), [], exc)
        # --force: 元のファイルはそのまま、仮ファイルも残らない
        path = os.path.join(self.dir, "n.vhd")
        with open(path, "wb") as f:
            f.write(b"keep")
        calls = []

        def fail2(f, d):
            calls.append(1)
            if len(calls) == 2:
                raise OSError(5, "I/O error")
            return orig(f, d)
        ldhdd._write_all = fail2
        try:
            with self.assertRaisesRegex(ldhdd.LdhddError, "元のファイルはそのまま"):
                self.new(force=True)
        finally:
            ldhdd._write_all = orig
        self.assertEqual(read(path), b"keep")
        self.assertEqual(os.listdir(self.dir), ["n.vhd"])

    def test_dir_fsync_failure_after_commit(self):
        orig = ldhdd._fsync_dir

        def fail(p):
            raise OSError(5, "I/O error")
        for force in (True, False):
            path = os.path.join(self.dir, "n.vhd")
            if force:
                with open(path, "wb") as f:
                    f.write(b"old")
            elif os.path.exists(path):
                os.remove(path)
            ldhdd._fsync_dir = fail
            try:
                with self.assertRaisesRegex(ldhdd.LdhddError, "置かれましたが"):
                    self.new(force=force)
            finally:
                ldhdd._fsync_dir = orig
            self.assertEqual(os.path.getsize(path), (16 << 20) + 512, force)
            run_main(["info", path])
            self.assertIn("起動部    : あり", run_main.out)
            self.assertEqual(sorted(os.listdir(self.dir)), ["n.vhd"])

    def test_cli(self):
        path = os.path.join(self.dir, "c.vhd")
        p_ld = os.path.join(self.dir, "LD.BIN")
        with open(p_ld, "wb") as f:
            f.write(fake_ld_bin(fill=0x33))
        self.assertEqual(run_main(["new", path, "--size", "2M", "--ldsys", MKLDHD, "--ld-bin", p_ld, "--label", "x"]), 0)
        self.assertIn("を作りました", run_main.out)
        self.assertIn("ラベル X", run_main.out)
        with open(path, "rb") as f:
            f.seek(0x200)
            self.assertEqual(f.read(135 + 1254 + 9728)[135:135 + 1254], ldhdd.files_from_d88(MKLDHD)["HDD.COM"])
        before = sha(path)
        self.assertEqual(run_main(["new", path, "--size", "2M", "--ldsys", MKLDHD, "--ld-bin", p_ld]), 1)
        self.assertIn("既にあります", run_main.err)
        self.assertEqual(sha(path), before)


def vol_read_file(vol, path):
    parts = [p for p in path.split("/") if p]
    cl = vol.resolve_dir("/".join(parts[:-1]))
    e = vol.find(vol.read_dir(cl), ldhdd.to_short_name(parts[-1]))
    assert e is not None and not e.is_dir, path
    if e.cluster == 0:
        return b""
    data = b"".join(ldhdd._read_at(vol.f, vol.cluster_off(c), vol.csize) for c in vol.chain(e.cluster, path))
    return data[:e.size]


class TestShortName(unittest.TestCase):
    def test_names(self):
        self.assertEqual(ldhdd.to_short_name("README.TXT"), b"README  TXT")
        self.assertEqual(ldhdd.to_short_name("readme.txt"), b"README  TXT")
        self.assertEqual(ldhdd.to_short_name("A"), b"A          ")
        self.assertEqual(ldhdd.to_short_name("ABCDEFGH.ABC"), b"ABCDEFGHABC")
        for bad in ("ABCDEFGHI.TXT", "A.ABCD", "A.B.C", ".PROFILE", "A B.TXT", "A*B", "日本語.TXT", "", "A.",
                    "straße", "\u017f"):
            with self.assertRaises(ldhdd.LdhddError, msg=bad):
                ldhdd.to_short_name(bad)


class TestAdd(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = self.td.name
        self.com = read(HDD_COM)
        self.ld = ldhdd.files_from_d88(MKLDHD)["LD.BIN"][135 + len(self.com):]
        self.src = os.path.join(self.dir, "src")
        os.mkdir(self.src)

    def tearDown(self):
        self.td.cleanup()

    def vhd(self, size="15M", name="t.vhd"):
        path = os.path.join(self.dir, name)
        ldhdd.new_image(path, ldhdd.parse_size(size), inputs_from(self.ld, self.com), [], out=io.StringIO())
        return path

    def put(self, rel, data=b"x"):
        p = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def add(self, path, sources, **kw):
        kw.setdefault("backup", False)
        return ldhdd.add(path, sources, out=io.StringIO(), **kw)

    def open_vol(self, path):
        f = ldhdd._open_target(path, False)
        return f, ldhdd.FatVolume(f)

    def assert_refused(self, path, sources, regex, **kw):
        before = sha(path)
        with self.assertRaisesRegex(ldhdd.LdhddError, regex):
            self.add(path, sources, **kw)
        self.assertEqual(sha(path), before)

    def test_files_and_tree_fat12_fat16(self):
        for size in ("15M", "128M"):
            path = self.vhd(size, "t%s.vhd" % size)
            f, vol = self.open_vol(path)
            cs = vol.csize
            f.close()
            files = {"E0.BIN": b"", "E1.BIN": b"\x01", "C1.BIN": b"\x02" * cs, "C2.BIN": b"\x03" * (cs + 1),
                     "MANY.BIN": os.urandom(cs * 3 + 17)}
            top = os.path.join(self.src, "T" + size[:-1])
            for n, d in files.items():
                self.put(os.path.join("T" + size[:-1], n), d)
            self.put(os.path.join("T" + size[:-1], "A", "B", "C", "DEEP.TXT"), b"deep")
            self.add(path, [top])
            f, vol = self.open_vol(path)
            try:
                for n, d in files.items():
                    self.assertEqual(vol_read_file(vol, "/T%s/%s" % (size[:-1], n)), d, (size, n))
                self.assertEqual(vol_read_file(vol, "/T%s/A/B/C/DEEP.TXT" % size[:-1]), b"deep")
                # . と .. の項目
                a = vol.find(vol.read_dir(vol.resolve_dir("/T" + size[:-1])), ldhdd.to_short_name("A"))
                raw = ldhdd._read_at(f, vol.cluster_off(a.cluster), 64)
                self.assertEqual(raw[0:11], ldhdd.DOT_NAME)
                self.assertEqual(struct.unpack_from("<H", raw, 26)[0], a.cluster)
                self.assertEqual(raw[32:43], ldhdd.DOTDOT_NAME)
                self.assertEqual(struct.unpack_from("<H", raw, 32 + 26)[0], vol.resolve_dir("/T" + size[:-1]))
                top_cl = vol.resolve_dir("/T" + size[:-1])
                raw = ldhdd._read_at(f, vol.cluster_off(top_cl), 64)
                self.assertEqual(struct.unpack_from("<H", raw, 32 + 26)[0], 0)  # ルート直下の .. は 0
                vol.walk_check()
            finally:
                f.close()
            run_main(["info", path])
            self.assertIn("起動部    : あり", run_main.out)
            if shutil.which("fsck.fat"):
                self.assertEqual(self.fsck(path), 0, size)

    def fsck(self, path):
        import subprocess
        data = read(path)
        part = os.path.join(self.dir, "part.img")
        with open(part, "wb") as f:
            f.write(data[128 * 512:len(data) - 512])
        r = subprocess.run([shutil.which("fsck.fat"), "-n", part], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        os.remove(part)
        return r.returncode

    def test_directory_grows(self):
        path = self.vhd("15M")  # 4KB クラスタ = 128 項目
        for i in range(200):
            self.put(os.path.join("MANY", "F%d.TXT" % i), b"%d" % i)
        self.add(path, [os.path.join(self.src, "MANY")])
        f, vol = self.open_vol(path)
        try:
            cl = vol.resolve_dir("/MANY")
            self.assertGreaterEqual(len(vol.chain(cl, "MANY")), 2)
            self.assertEqual(len(vol.read_dir(cl).entries), 200)
            self.assertEqual(vol_read_file(vol, "/MANY/F199.TXT"), b"199")
        finally:
            f.close()
        # 既にあるフォルダにさらに足して伸びる
        for i in range(200, 330):
            self.put(os.path.join("MORE", "MANY", "G%d.TXT" % i), b"%d" % i)
        self.add(path, [os.path.join(self.src, "MORE", "MANY")])
        f, vol = self.open_vol(path)
        try:
            self.assertEqual(len(vol.read_dir(vol.resolve_dir("/MANY")).entries), 330)
            vol.walk_check()
        finally:
            f.close()
        if shutil.which("fsck.fat"):
            self.assertEqual(self.fsck(path), 0)

    def test_existing_names(self):
        path = self.vhd()
        p = self.put("A.TXT", b"old" * 3000)
        self.add(path, [p])
        f, vol = self.open_vol(path)
        free0 = len(vol.free_clusters())
        f.close()
        self.assert_refused(path, [p], "同じ名前のファイル")
        p2 = os.path.join(self.dir, "new", "A.TXT")
        os.makedirs(os.path.dirname(p2))
        with open(p2, "wb") as fh:
            fh.write(b"new")
        self.add(path, [p2], overwrite=True)
        f, vol = self.open_vol(path)
        try:
            self.assertEqual(vol_read_file(vol, "/A.TXT"), b"new")
            self.assertEqual(len(vol.free_clusters()), free0 + 3 - 1)  # 古い 3 クラスタが空き、新しく 1
        finally:
            f.close()
        # 同じ名前のフォルダには合わせて入る
        self.put(os.path.join("D", "X.TXT"), b"x")
        self.add(path, [os.path.join(self.src, "D")])
        self.put(os.path.join("D2", "D", "Y.TXT"), b"y")
        self.add(path, [os.path.join(self.src, "D2", "D")])
        f, vol = self.open_vol(path)
        try:
            self.assertEqual(vol_read_file(vol, "/D/X.TXT"), b"x")
            self.assertEqual(vol_read_file(vol, "/D/Y.TXT"), b"y")
        finally:
            f.close()
        # ファイルとフォルダのぶつかり
        self.put(os.path.join("D3", "A.TXT", "Z.TXT"), b"z")
        self.assert_refused(path, [os.path.join(self.src, "D3", "A.TXT")], "同じ名前のファイル")
        # 大文字にするとぶつかる入力
        self.put(os.path.join("CASE", "a.txt"), b"1")
        self.put(os.path.join("CASE", "A.TXT"), b"2")
        if len(os.listdir(os.path.join(self.src, "CASE"))) == 2:  # 大文字小文字を区別するファイルシステムだけ
            self.assert_refused(path, [os.path.join(self.src, "CASE")], "同じ名前になります")

    def test_capacity_and_root_entries(self):
        path = self.vhd("1M")
        big = self.put("BIG.BIN", b"\0" * (2 << 20))
        self.assert_refused(path, [big], "空きが足りません")
        root = os.path.join(self.dir, "root")
        os.mkdir(root)
        ps = []
        for i in range(513):
            p = os.path.join(root, "R%d" % i)
            with open(p, "wb"):
                pass
            ps.append(p)
        self.assert_refused(path, ps, "ルートの項目が足りません")

    def test_name_errors_and_dotfiles(self):
        path = self.vhd()
        self.put(os.path.join("P", "LONGFILENAME.TXT"), b"1")
        self.put(os.path.join("P", "OK.TXT"), b"2")
        self.put(os.path.join("P", "BAD NAME.TXT"), b"3")
        before = sha(path)
        with self.assertRaises(ldhdd.LdhddError) as cm:
            self.add(path, [os.path.join(self.src, "P")])
        self.assertIn("LONGFILENAME.TXT", str(cm.exception))
        self.assertIn("BAD NAME.TXT", str(cm.exception))
        self.assertEqual(sha(path), before)
        self.put(os.path.join("Q", ".git", "HEAD"), b"x")
        self.put(os.path.join("Q", "OK.TXT"), b"ok")
        self.assert_refused(path, [os.path.join(self.src, "Q")], "「.」で始まる")
        self.add(path, [os.path.join(self.src, "Q")], skip_dotfiles=True)
        f, vol = self.open_vol(path)
        try:
            self.assertEqual(vol_read_file(vol, "/Q/OK.TXT"), b"ok")
            self.assertEqual([ldhdd.short_to_text(e.name) for e in vol.read_dir(vol.resolve_dir("/Q")).entries], ["OK.TXT"])
        finally:
            f.close()
        if os.name != "nt":
            os.symlink(os.path.join(self.src, "Q"), os.path.join(self.src, "LNK"))
            self.assert_refused(path, [os.path.join(self.src, "LNK")], "シンボリックリンク")
            self.assert_refused(path, [os.path.join(self.src, "LNK") + "/"], "シンボリックリンク")

    def test_dry_run_and_to(self):
        path = self.vhd()
        p = self.put("X.TXT", b"x")
        before = sha(path)
        self.add(path, [p], dry_run=True)
        self.assertEqual(sha(path), before)
        self.add(path, [p], to_path="/A/B")
        f, vol = self.open_vol(path)
        try:
            self.assertEqual(vol_read_file(vol, "/A/B/X.TXT"), b"x")
        finally:
            f.close()

    def corrupt(self, path, fn):
        with ldhdd._open_target(path, True) as f:
            vol = ldhdd.FatVolume(f)
            fn(f, vol)

    def write_fats(self, f, vol):
        for k in range(vol.nfats):
            f.seek(vol.fat_off + k * vol.fat_bytes)
            f.write(bytes(vol.fat))

    def test_broken_fat_refused(self):
        p = self.put("N.TXT", b"n")
        # 循環
        path = self.vhd(name="c1.vhd")
        self.add(path, [self.put("A.BIN", b"a" * 5000)])

        def cycle(f, vol):
            e = vol.find(vol.read_dir(0), ldhdd.to_short_name("A.BIN"))
            ch = vol.chain(e.cluster, "A")
            vol.set(ch[-1], ch[0])
            self.write_fats(f, vol)
        self.corrupt(path, cycle)
        self.assert_refused(path, [p], "壊れています")
        # 鎖の後ろの共有
        path = self.vhd(name="c2.vhd")
        self.add(path, [self.put("A.BIN", b"a" * 9000), self.put("B.BIN", b"b" * 9000)])

        def share(f, vol):
            ea = vol.find(vol.read_dir(0), ldhdd.to_short_name("A.BIN"))
            eb = vol.find(vol.read_dir(0), ldhdd.to_short_name("B.BIN"))
            ca, cb = vol.chain(ea.cluster, "A"), vol.chain(eb.cluster, "B")
            vol.set(cb[0], ca[1])  # B の 2 番目から先を A と共有
            for c in cb[1:]:
                vol.set(c, 0)
            self.write_fats(f, vol)
        self.corrupt(path, share)
        self.assert_refused(path, [p], "共有")
        self.assert_refused(path, [self.put(os.path.join("o", "A.BIN"), b"z")], "共有", overwrite=True)
        # 祖先を指すフォルダ
        path = self.vhd(name="c3.vhd")
        self.put(os.path.join("P1", "P2", "F.TXT"), b"f")
        self.add(path, [os.path.join(self.src, "P1")])

        def loop(f, vol):
            p1 = vol.find(vol.read_dir(0), ldhdd.to_short_name("P1"))
            p2 = vol.find(vol.read_dir(p1.cluster), ldhdd.to_short_name("P2"))
            f.seek(p2.off + 26)
            f.write(struct.pack("<H", p1.cluster))
        self.corrupt(path, loop)
        self.assert_refused(path, [p], "祖先")
        self.assertEqual(run_main(["ls", path, "-R"]), 1)
        # 第 2 FAT の食い違い
        path = self.vhd(name="c4.vhd")
        self.add(path, [self.put("A.BIN", b"a" * 5000)])

        def second(f, vol):
            f.seek(vol.fat_off + vol.fat_bytes + 3 * 3 // 2 + 10)
            f.write(b"\x77")
        self.corrupt(path, second)
        self.assert_refused(path, [p], "食い違って")

    def test_partition_over_footer(self):
        path = self.vhd()
        with open(path, "r+b") as f:
            f.seek(0x1BE + 12)
            n = struct.unpack("<I", f.read(4))[0]
            f.seek(0x1BE + 12)
            f.write(struct.pack("<I", n + 1))
        self.assert_refused(path, [self.put("N.TXT")], "範囲")

    def test_existing_directory_metadata(self):
        path = self.vhd()
        self.add(path, [self.put("A.TXT", b"a")])
        with open(path, "r+b") as f:
            f.seek(0)
            vol = ldhdd.FatVolume(f)
            listing = vol.read_dir(0)
            slots = listing.slots
            # [0] A.TXT / [1] 長い名前の項目 / [2] ボリュームラベル / [3] 消した項目 / [4] 終わり / [5] 古い項目
            lfn = bytes([0x41]) + b"x\0" * 5 + bytes([0x0F, 0, 0]) + b"\0" * 18
            self.assertEqual(len(lfn), 32)
            label = b"MYLABEL    " + bytes([0x08]) + bytes(20)
            deleted = b"\xe5LD     TXT" + bytes([0x20]) + bytes(20)
            stale = ldhdd.make_dir_entry(b"STALE   TXT", 0x20, 0, 0, time.time())
            for i, e in ((1, lfn), (2, label), (3, deleted), (4, bytes(32)), (5, stale)):
                f.seek(slots[i])
                f.write(e)
        run_main(["ls", path])
        self.assertNotIn("STALE", run_main.out)
        self.assertNotIn("MYLABEL", run_main.out)
        self.add(path, [self.put("B.TXT", b"b"), self.put("C.TXT", b"c")])
        with open(path, "rb") as f:
            vol = ldhdd.FatVolume(f)
            slots = vol.read_dir(0).slots
            self.assertEqual(ldhdd._read_at(f, slots[1], 32), lfn)
            self.assertEqual(ldhdd._read_at(f, slots[2], 32), label)
            names = sorted(ldhdd.short_to_text(e.name) for e in vol.read_dir(0).entries)
            self.assertEqual(names, ["A.TXT", "B.TXT", "C.TXT"])
            self.assertEqual(ldhdd._read_at(f, slots[3], 11), b"B       TXT")  # 消した項目の場所を使った
            self.assertEqual(ldhdd._read_at(f, slots[4], 11), b"C       TXT")  # 終わりの印の場所を使った
            self.assertEqual(ldhdd._read_at(f, slots[5], 1), b"\0")  # 印を置き直した
        run_main(["ls", path])
        self.assertNotIn("STALE", run_main.out)

    def test_overwrite_entry_write_failure_keeps_original(self):
        path = self.vhd()
        self.add(path, [self.put("A.TXT", b"old" * 3000)])
        p2 = os.path.join(self.dir, "n2", "A.TXT")
        os.makedirs(os.path.dirname(p2))
        with open(p2, "wb") as fh:
            fh.write(b"new" * 5000)
        f, vol = self.open_vol(path)
        free0 = len(vol.free_clusters())
        f.close()
        orig = ldhdd._write_all
        state = {"failed": False}

        def fail(f, d):
            if len(d) == 32 and not state["failed"]:  # ディレクトリ項目の書き込み (FAT の後) を 1 回だけ失敗させる
                state["failed"] = True
                raise OSError(5, "injected")
            return orig(f, d)
        ldhdd._write_all = fail
        try:
            with self.assertRaisesRegex(ldhdd.LdhddError, "injected"):
                self.add(path, [p2], overwrite=True)
        finally:
            ldhdd._write_all = orig
        f, vol = self.open_vol(path)
        try:
            vol.walk_check()  # 古い鎖はまだ使用中で、持ち主も 1 つ
            self.assertEqual(vol_read_file(vol, "/A.TXT"), b"old" * 3000)
            self.assertEqual(len(vol.free_clusters()), free0)  # 新しく割り当てた分も空きに戻った
        finally:
            f.close()

    def test_overlapping_partitions_refused(self):
        path = self.vhd()
        with open(path, "r+b") as f:
            f.seek(0x1CE)
            f.write(struct.pack("<B3sB3sII", 0, b"\0\0\0", 0x06, b"\0\0\0", 200, 100))
        self.assert_refused(path, [self.put("N.TXT")], "重なっています")

    def test_source_size_change(self):
        for change in (b"short", b"x" * 4099):
            path = self.vhd(name="s%d.vhd" % len(change))
            p = self.put("S%d.TXT" % len(change), b"y" * 4097)
            orig = ldhdd.plan_add

            def plan_then_change(*a, **kw):
                r = orig(*a, **kw)
                with open(p, "wb") as fh:
                    fh.write(change)
                return r
            ldhdd.plan_add = plan_then_change
            try:
                self.assert_refused(path, [p], "調べた後で")
            finally:
                ldhdd.plan_add = orig

    def test_long_name_conflict(self):
        path = self.vhd()
        self.add(path, [self.put("README~1.TXT", b"r")])
        short = b"README~1TXT"
        lname = "ReadMe.txt"
        units = [ord(c) for c in lname] + [0] + [0xFFFF] * (13 - len(lname) - 1)
        e = bytearray(32)
        e[0] = 0x41
        e[11] = 0x0F
        e[13] = ldhdd._lfn_checksum(short)
        b = struct.pack("<13H", *units)
        e[1:11], e[14:26], e[28:32] = b[0:10], b[10:22], b[22:26]
        with open(path, "r+b") as f:
            vol = ldhdd.FatVolume(f)
            listing = vol.read_dir(0)
            ent = vol.find(listing, short)
            raw = ldhdd._read_at(f, ent.off, 32)
            i = listing.slots.index(ent.off)
            # [i] に長い名前、[i+1] に短い名前の項目を置き直す
            f.seek(listing.slots[i])
            f.write(bytes(e))
            f.seek(listing.slots[i + 1])
            f.write(raw)
        with open(path, "rb") as f:
            vol = ldhdd.FatVolume(f)
            self.assertEqual(vol.find(vol.read_dir(0), short).long_name, "ReadMe.txt")
        self.assert_refused(path, [self.put(os.path.join("n", "README.TXT"), b"x")], "長い名前")
        self.assert_refused(path, [self.put(os.path.join("n", "README.TXT"), b"x")], "長い名前", overwrite=True)

    def stale_fixture(self):
        """[0] A.TXT / [1] 終わりの印 / [2] それらしい古い項目"""
        path = self.vhd()
        self.add(path, [self.put("A.TXT", b"a")])
        with open(path, "r+b") as f:
            vol = ldhdd.FatVolume(f)
            slots = vol.read_dir(0).slots
            f.seek(slots[2])
            f.write(ldhdd.make_dir_entry(b"STALE   TXT", 0x20, 0, 0, time.time()))
        return path, slots

    def test_marker_relocation_failure_keeps_stale_hidden(self):
        path, slots = self.stale_fixture()
        orig = ldhdd._write_all

        state = {"failed": False}

        def fail(f, d):
            if len(d) == 32 and d[0] == 0 and not state["failed"]:  # 終わりの印を置き直す書き込み (1 回だけ)
                state["failed"] = True
                raise OSError(5, "injected")
            return orig(f, d)
        ldhdd._write_all = fail
        try:
            with self.assertRaisesRegex(ldhdd.LdhddError, "injected"):
                self.add(path, [self.put("B.TXT", b"b")])
        finally:
            ldhdd._write_all = orig
        run_main(["ls", path])
        self.assertNotIn("STALE", run_main.out)
        self.assertNotIn("B.TXT", run_main.out)
        # 失敗しなければ、印は STALE の位置に置き直され、STALE は見えないまま
        self.add(path, [self.put("B.TXT", b"b")])
        run_main(["ls", path])
        self.assertIn("B.TXT", run_main.out)
        self.assertNotIn("STALE", run_main.out)

    def test_partial_entry_write_restores_original(self):
        path = self.vhd()
        self.add(path, [self.put("A.TXT", b"old" * 3000)])
        p2 = os.path.join(self.dir, "n2", "A.TXT")
        os.makedirs(os.path.dirname(p2))
        with open(p2, "wb") as fh:
            fh.write(b"new")
        orig = ldhdd._write_all
        state = {"failed": False}

        def partial(f, d):
            if len(d) == 32 and not state["failed"]:  # 項目を 28 バイトだけ書いて失敗する (1 回だけ)
                state["failed"] = True
                orig(f, d[:28])
                raise OSError(5, "injected")
            return orig(f, d)
        ldhdd._write_all = partial
        try:
            with self.assertRaisesRegex(ldhdd.LdhddError, "injected"):
                self.add(path, [p2], overwrite=True)
        finally:
            ldhdd._write_all = orig
        f, vol = self.open_vol(path)
        try:
            vol.walk_check()
            self.assertEqual(vol_read_file(vol, "/A.TXT"), b"old" * 3000)
        finally:
            f.close()

    def test_detached_long_name_is_not_inherited(self):
        path = self.vhd()
        self.add(path, [self.put("A.TXT", b"a")])
        units = [ord(c) for c in "Old.txt"] + [0] + [0xFFFF] * 5
        e = bytearray(32)
        e[0], e[11], e[13] = 0x41, 0x0F, ldhdd._lfn_checksum(b"NEW     TXT")
        b = struct.pack("<13H", *units)
        e[1:11], e[14:26], e[28:32] = b[0:10], b[10:22], b[22:26]
        with open(path, "r+b") as f:
            vol = ldhdd.FatVolume(f)
            slots = vol.read_dir(0).slots
            # [0] A.TXT / [1] 宙に浮いた長い名前 / [2] 消した項目 (空き) / [3] 終わり
            f.seek(slots[1])
            f.write(bytes(e))
            f.seek(slots[2])
            f.write(b"\xe5" + b"X      TXT" + bytes(21))
        self.add(path, [self.put("NEW.TXT", b"n")])
        with open(path, "rb") as f:
            vol = ldhdd.FatVolume(f)
            ent = vol.find(vol.read_dir(0), b"NEW     TXT")
            self.assertIsNotNone(ent)
            self.assertIsNone(ent.long_name)
            self.assertEqual(ldhdd._read_at(f, slots[1], 1), b"\xe5")

    @unittest.skipIf(os.name == "nt", "シンボリックリンクのテスト")
    def test_symlink_spellings_and_replacement(self):
        path = self.vhd()
        self.put(os.path.join("Q", "OK.TXT"), b"ok")
        os.symlink(os.path.join(self.src, "Q"), os.path.join(self.src, "LNK"))
        for spelling in ("LNK", "LNK/", "LNK/.", "LNK/./"):
            self.assert_refused(path, [os.path.join(self.src, spelling)], "シンボリックリンク")
        # 調べた後でファイルがシンボリックリンクに置き換わったら止める
        p = self.put("R.TXT", b"real")
        other = self.put("OTHER.TXT", b"other")
        orig = ldhdd.plan_add

        def plan_then_swap(*a, **kw):
            r = orig(*a, **kw)
            os.remove(p)
            os.symlink(other, p)
            return r
        ldhdd.plan_add = plan_then_swap
        try:
            self.assert_refused(path, [p], "置き換わりました|Too many levels|ELOOP|シンボリック")
        finally:
            ldhdd.plan_add = orig

    @unittest.skipIf(os.name == "nt", "シンボリックリンクのテスト")
    def test_skip_dotfiles_skips_hidden_symlink(self):
        path = self.vhd()
        self.put(os.path.join("H", "OK.TXT"), b"ok")
        os.symlink(os.path.join(self.src, "H", "OK.TXT"), os.path.join(self.src, "H", ".lnk"))
        self.add(path, [os.path.join(self.src, "H")], skip_dotfiles=True)
        f, vol = self.open_vol(path)
        try:
            self.assertEqual([ldhdd.short_to_text(x.name) for x in vol.read_dir(vol.resolve_dir("/H")).entries], ["OK.TXT"])
        finally:
            f.close()

    def test_fat_mirror_write_failure_restores(self):
        # 2 つ目の FAT の写しの書き込みを 1 回だけ失敗させる: 割り当ての時と、置き換えの後始末の時
        for phase in ("alloc", "cleanup"):
            path = self.vhd(name="fm_%s.vhd" % phase)
            self.add(path, [self.put("A.TXT", b"old" * 3000)])
            f, vol = self.open_vol(path)
            free0, fat_bytes, fat_off = len(vol.free_clusters()), vol.fat_bytes, vol.fat_off
            f.close()
            p2 = os.path.join(self.dir, "fm", phase, "A.TXT")
            os.makedirs(os.path.dirname(p2))
            with open(p2, "wb") as fh:
                fh.write(b"new" * 5000)
            orig = ldhdd._write_all
            state = {"n": 0, "failed": False}
            target_call = 2 if phase == "alloc" else 4  # FAT の書き込みは 写し1, 写し2 (割り当て), 写し1, 写し2 (後始末)

            def fail(fh, d):
                if len(d) == fat_bytes and not state["failed"]:
                    state["n"] += 1
                    if state["n"] == target_call:
                        state["failed"] = True
                        raise OSError(5, "injected")
                return orig(fh, d)
            ldhdd._write_all = fail
            try:
                with self.assertRaisesRegex(ldhdd.LdhddError, "injected"):
                    self.add(path, [p2], overwrite=True)
            finally:
                ldhdd._write_all = orig
            f, vol = self.open_vol(path)  # 写しが食い違っていれば、ここで開けない
            try:
                vol.walk_check()
                if phase == "alloc":
                    self.assertEqual(vol_read_file(vol, "/A.TXT"), b"old" * 3000)
                    self.assertEqual(len(vol.free_clusters()), free0)
                else:
                    self.assertEqual(vol_read_file(vol, "/A.TXT"), b"new" * 5000)
            finally:
                f.close()

    def test_directory_failure_releases_allocations(self):
        path = self.vhd()
        f, vol = self.open_vol(path)
        free0 = len(vol.free_clusters())
        f.close()
        orig = ldhdd._write_all
        state = {"failed": False}

        def fail(fh, d):
            if len(d) == 32 and not state["failed"]:
                state["failed"] = True
                raise OSError(5, "injected")
            return orig(fh, d)
        ldhdd._write_all = fail
        try:
            with self.assertRaisesRegex(ldhdd.LdhddError, "injected"):
                self.add(path, [self.put("BIG.BIN", b"z" * 20000)])
        finally:
            ldhdd._write_all = orig
        f, vol = self.open_vol(path)
        try:
            self.assertEqual(len(vol.free_clusters()), free0)
            self.assertEqual(vol.read_dir(0).entries, [])
        finally:
            f.close()

    def test_trailing_detached_long_name_on_growth(self):
        # いっぱいのサブディレクトリの最後に宙に浮いた長い名前があっても、伸ばした先の項目に付かない
        path = self.vhd()  # 4KB クラスタ = 128 項目
        for i in range(126):  # . と .. と 126 個 = 128 個でいっぱい
            self.put(os.path.join("FULL", "F%d.TXT" % i), b"%d" % i)
        self.add(path, [os.path.join(self.src, "FULL")])
        with open(path, "r+b") as f:
            vol = ldhdd.FatVolume(f)
            listing = vol.read_dir(vol.resolve_dir("/FULL"))
            self.assertEqual(listing.free, [])
            last = listing.slots[-1]
            units = [ord(c) for c in "Old.com"] + [0] + [0xFFFF] * 5
            e = bytearray(32)
            e[0], e[11], e[13] = 0x41, 0x0F, ldhdd._lfn_checksum(b"HDD     COM")
            b = struct.pack("<13H", *units)
            e[1:11], e[14:26], e[28:32] = b[0:10], b[10:22], b[22:26]
            f.seek(last)
            f.write(bytes(e))  # 最後の項目 (F125.TXT) を宙に浮いた長い名前にする
        self.put(os.path.join("MORE", "FULL", "HDD.COM"), b"h")
        self.add(path, [os.path.join(self.src, "MORE", "FULL")])
        with open(path, "rb") as f:
            vol = ldhdd.FatVolume(f)
            ent = vol.find(vol.read_dir(vol.resolve_dir("/FULL")), b"HDD     COM")
            self.assertIsNotNone(ent)
            self.assertIsNone(ent.long_name)

    def test_extension_zero_fill_synced_before_fat(self):
        path = self.vhd()
        for i in range(126):
            self.put(os.path.join("FULL", "F%d.TXT" % i), b"%d" % i)
        self.add(path, [os.path.join(self.src, "FULL")])
        f0, vol0 = self.open_vol(path)
        fat_bytes, csize = vol0.fat_bytes, vol0.csize
        ext = vol0.free_clusters()[1]  # 1 つ目は X.TXT、2 つ目がディレクトリを伸ばす先
        ext_off = vol0.cluster_off(ext)
        f0.close()
        # 伸ばす先のクラスタに、それらしい古い項目を置いておく
        with open(path, "r+b") as fh:
            fh.seek(ext_off)
            fh.write(ldhdd.make_dir_entry(b"STALE   TXT", 0x20, 0, 0, time.time()) * (csize // 32))
        events = []
        orig_w, orig_s = ldhdd._write_all, ldhdd.os.fsync

        def w(fh, d):
            pos = fh.tell()
            if len(d) == fat_bytes:
                events.append("fat")
            elif pos == ext_off and len(d) == csize and d == bytes(csize):
                events.append("zero")
            else:
                events.append("w")
            return orig_w(fh, d)
        ldhdd._write_all = w
        ldhdd.os.fsync = lambda fd: (events.append("sync"), orig_s(fd))[1]
        try:
            self.put(os.path.join("MORE", "FULL", "X.TXT"), b"x")
            self.put(os.path.join("MORE", "FULL", "Y.TXT"), b"y")
            self.add(path, [os.path.join(self.src, "MORE", "FULL")])
        finally:
            ldhdd._write_all, ldhdd.os.fsync = orig_w, orig_s
        self.assertIn("zero", events)
        first_fat = events.index("fat")
        zi = events.index("zero")
        self.assertLess(zi, first_fat)
        self.assertIn("sync", events[zi:first_fat])  # 0 埋めを同期してから FAT でつなぐ
        run_main(["ls", path, "/FULL"])
        self.assertNotIn("STALE", run_main.out)

    @unittest.skipIf(os.name == "nt", "FIFO のテスト")
    def test_source_replaced_by_fifo(self):
        path = self.vhd()
        p = self.put("R.TXT", b"real")
        orig = ldhdd.plan_add

        def plan_then_fifo(*a, **kw):
            r = orig(*a, **kw)
            os.remove(p)
            os.mkfifo(p)
            return r
        ldhdd.plan_add = plan_then_fifo
        import signal

        def timeout(signum, frame):
            raise AssertionError("FIFO を開いたまま止まりました")
        old = signal.signal(signal.SIGALRM, timeout)
        signal.alarm(20)
        try:
            self.assert_refused(path, [p], "置き換わりました")
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
            ldhdd.plan_add = orig

    def reserved_fixture(self, name, total, bits):
        path = make_vhd(os.path.join(self.dir, name), sectors=128 + total, parts=((0x01 if bits == 12 else 0x06, 128, total),))
        with open(path, "r+b") as f:
            bpb = bpb_sector(total, spc=1, root=512)
            spf = struct.unpack_from("<H", bpb, 22)[0]
            f.seek(128 * 512)
            f.write(bytes((1 + 2 * spf + 32 + 16) * 512))  # 前の形の FAT の先頭を消す
            f.seek(128 * 512)
            f.write(bpb)
            for off, head in fat_init(bpb):
                f.seek(128 * 512 + off)
                f.write(head)
        return path

    def test_reserved_cluster_numbers_not_used(self):
        # FAT12 で 4084 クラスタ: 番号 0xFF0〜0xFF5 は予約の値と重なるので使わない
        path = self.reserved_fixture("r12.vhd", 4141, 12)
        f, vol = self.open_vol(path)
        try:
            self.assertEqual((vol.bits, vol.nclusters), (12, 4084))
            self.assertEqual(max(vol.free_clusters()), 0xFEF)
            usable = len(vol.free_clusters())
        finally:
            f.close()
        self.assert_refused(path, [self.put("BIG.BIN", b"z" * ((usable + 1) * 512))], "空きが足りません")
        self.add(path, [self.put("FIT.BIN", b"z" * (usable * 512))])
        f, vol = self.open_vol(path)
        try:
            e = vol.find(vol.read_dir(0), ldhdd.to_short_name("FIT.BIN"))
            ch = vol.chain(e.cluster, "FIT")
            self.assertEqual(max(ch), 0xFEF)
            self.assertTrue(all(vol.get(c) < 0xFF0 or vol.is_eoc(vol.get(c)) for c in ch))
        finally:
            f.close()
        # new の FAT12 は予約の番号に届かない形を選ぶ
        for total in range(32640, 32768, 16):
            lay = ldhdd.choose_fat_layout(total)
            self.assertLessEqual(lay.clusters + 1, 0xFEF, total)

    def test_reserved_cluster_numbers_fat16(self):
        path = self.reserved_fixture("r16.vhd", 65524 + 1 + 2 * 256 + 32, 16)
        f, vol = self.open_vol(path)
        try:
            self.assertEqual(vol.bits, 16)
            self.assertEqual(max(vol.free_clusters()), 0xFFEF)
        finally:
            f.close()

    def full_dir_with_stale_extension(self):
        path = self.vhd()
        for i in range(126):
            self.put(os.path.join("FULL", "F%d.TXT" % i), b"%d" % i)
        self.add(path, [os.path.join(self.src, "FULL")])
        f0, vol0 = self.open_vol(path)
        csize = vol0.csize
        ext_off = vol0.cluster_off(vol0.free_clusters()[1])
        free0 = len(vol0.free_clusters())
        f0.close()
        with open(path, "r+b") as fh:
            fh.seek(ext_off)
            fh.write(ldhdd.make_dir_entry(b"STALE   TXT", 0x20, 2, 0, time.time()) * (csize // 32))
        return path, ext_off, csize, free0

    def test_growth_rollback_does_not_restore_stale(self):
        path, ext_off, csize, free0 = self.full_dir_with_stale_extension()
        orig = ldhdd._write_all
        state = {"failed": False}

        def fail(fh, d):
            if len(d) == 32 and not state["failed"]:
                state["failed"] = True
                raise OSError(5, "injected")
            return orig(fh, d)
        ldhdd._write_all = fail
        try:
            self.put(os.path.join("MORE", "FULL", "X.TXT"), b"x")
            with self.assertRaisesRegex(ldhdd.LdhddError, "injected"):
                self.add(path, [os.path.join(self.src, "MORE", "FULL")])
        finally:
            ldhdd._write_all = orig
        # 戻した後の伸ばした先は 0 (古い項目に戻っていない)
        self.assertEqual(read(path)[ext_off:ext_off + csize], bytes(csize))
        f, vol = self.open_vol(path)
        try:
            vol.walk_check()
            self.assertEqual(len(vol.free_clusters()), free0)
        finally:
            f.close()

    def test_dropped_marker_write_never_exposes_stale(self):
        path, slots = self.stale_fixture()
        orig_w, orig_s = ldhdd._write_all, ldhdd.os.fsync
        state = {"dropped": False}
        seen = []

        def drop(fh, d):
            if len(d) == 32 and d[0] == 0 and not state["dropped"]:
                state["dropped"] = True
                return None  # 成功を返したが書いていない
            return orig_w(fh, d)

        def sync(fd):
            orig_s(fd)
            raw = read(path)
            if raw[slots[1]] != 0 and raw[slots[2]:slots[2] + 5] == b"STALE":
                seen.append(True)  # 古い項目が見えている状態で同期した
        ldhdd._write_all, ldhdd.os.fsync = drop, sync
        try:
            with self.assertRaisesRegex(ldhdd.LdhddError, "一致しません"):
                self.add(path, [self.put("B.TXT", b"b")])
        finally:
            ldhdd._write_all, ldhdd.os.fsync = orig_w, orig_s
        self.assertEqual(seen, [])
        run_main(["ls", path])
        self.assertNotIn("STALE", run_main.out)

    def test_fat_mirror_readback_mismatch_restores(self):
        for phase in ("alloc", "cleanup"):
            path = self.vhd(name="rb_%s.vhd" % phase)
            self.add(path, [self.put("A.TXT", b"old" * 3000)])
            f, vol = self.open_vol(path)
            free0, fat_bytes = len(vol.free_clusters()), vol.fat_bytes
            f.close()
            p2 = os.path.join(self.dir, "rb", phase, "A.TXT")
            os.makedirs(os.path.dirname(p2))
            with open(p2, "wb") as fh:
                fh.write(b"new" * 5000)
            orig = ldhdd._write_all
            state = {"n": 0}
            target_call = 2 if phase == "alloc" else 4

            def skip(fh, d):
                if len(d) == fat_bytes:
                    state["n"] += 1
                    if state["n"] == target_call:
                        return None  # 2 つ目の写しを書いたふりをする
                return orig(fh, d)
            ldhdd._write_all = skip
            try:
                with self.assertRaisesRegex(ldhdd.LdhddError, "一致しません"):
                    self.add(path, [p2], overwrite=True)
            finally:
                ldhdd._write_all = orig
            f, vol = self.open_vol(path)
            try:
                vol.walk_check()
                if phase == "alloc":
                    self.assertEqual(vol_read_file(vol, "/A.TXT"), b"old" * 3000)
                    self.assertEqual(len(vol.free_clusters()), free0)
                else:
                    self.assertEqual(vol_read_file(vol, "/A.TXT"), b"new" * 5000)
            finally:
                f.close()

    def test_snapshot_read_failure_leaves_image(self):
        path = self.vhd()
        self.add(path, [self.put("A.TXT", b"a")])
        orig_plan, orig_read = ldhdd.plan_add, ldhdd._read_at
        state = {"armed": False}

        def plan(*a, **kw):
            r = orig_plan(*a, **kw)
            state["armed"] = True
            return r

        def rd(fh, off, n):
            if state["armed"] and n == 32:
                raise OSError(5, "injected")
            return orig_read(fh, off, n)
        ldhdd.plan_add, ldhdd._read_at = plan, rd
        try:
            self.assert_refused(path, [self.put("B.TXT", b"b")], "injected")
        finally:
            ldhdd.plan_add, ldhdd._read_at = orig_plan, orig_read

    def test_too_deep_refused(self):
        path = self.vhd()
        p = self.put("X.TXT", b"x")
        self.assert_refused(path, [p], "深すぎます", to_path="/" + "/".join("D%d" % i for i in range(70)))

    def test_timestamps(self):
        path = self.vhd()
        p = self.put("T.TXT", b"t")
        ts = time.mktime((2000, 1, 2, 3, 4, 6, 0, 0, -1))
        os.utime(p, (ts, ts))
        p2 = self.put("OLD.TXT", b"o")
        os.utime(p2, (86400 * 10, 86400 * 10))
        self.add(path, [p, p2])
        f, vol = self.open_vol(path)
        try:
            e = vol.find(vol.read_dir(0), ldhdd.to_short_name("T.TXT"))
            self.assertEqual(e.mdate, (20 << 9) | (1 << 5) | 2)
            self.assertEqual(e.mtime, (3 << 11) | (4 << 5) | 3)
            o = vol.find(vol.read_dir(0), ldhdd.to_short_name("OLD.TXT"))
            self.assertEqual(o.mdate, (0 << 9) | (1 << 5) | 1)
        finally:
            f.close()

    def test_write_failure_reports_backup(self):
        path = self.vhd()
        orig = ldhdd._write_all
        calls = []

        def fail(f, d):
            calls.append(1)
            if len(calls) == 2:
                raise OSError(5, "injected")
            return orig(f, d)
        ldhdd._write_all = fail
        try:
            with self.assertRaisesRegex(ldhdd.LdhddError, "戻してください"):
                self.add(path, [self.put("A.TXT", b"a" * 9000)], backup=True)
        finally:
            ldhdd._write_all = orig

    def test_ls(self):
        path = self.vhd()
        self.put(os.path.join("L", "S", "F.TXT"), b"f")
        self.add(path, [os.path.join(self.src, "L")])
        self.assertEqual(run_main(["ls", path]), 0)
        self.assertIn("L", run_main.out)
        self.assertIn("<DIR>", run_main.out)
        self.assertEqual(run_main(["ls", path, "/L", "-R"]), 0)
        self.assertIn("/L/S:", run_main.out)
        self.assertIn("F.TXT", run_main.out)
        self.assertIn("空き:", run_main.out)
        self.assertEqual(run_main(["ls", path, "/NONE"]), 1)

    def test_cli_add(self):
        path = self.vhd()
        p = self.put(os.path.join("C", "A.TXT"), b"a")
        self.assertEqual(run_main(["add", path, os.path.join(self.src, "C"), "--no-backup"]), 0, run_main.err)
        self.assertIn("完了しました", run_main.out)
        self.assertEqual(run_main(["add", path, os.path.join(self.src, "C"), "--no-backup"]), 1)
        self.assertIn("同じ名前のファイル", run_main.err)


@unittest.skipUnless(os.environ.get("LDHDD_LDSYS"), "LDHDD_LDSYS が無い")
class TestRealLdsys(unittest.TestCase):
    def test_ldsys(self):
        p = os.environ["LDHDD_LDSYS"]
        # 版を問わない確認: LD.BIN と HDD.COM が読め、合体版を作って分解すると元に戻る
        inp = ldhdd.load_inputs(p, None, None)
        self.assertIn("LSX-Dodgers for X1", ldhdd.ld_version(inp.ld_bin))
        out = ldhdd.makesys(inp.ld_bin, inp.com)
        si = ldhdd.parse_sys(out)
        self.assertEqual((si.com, si.ld_bin, si.unit_override), (inp.com, inp.ld_bin, None))
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
