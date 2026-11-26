"""ldfsck.py の試験 (合成の VHD。LSX-Dodgers の COPEN の誤りは lsx_grow で真似る)"""

import contextlib
import io
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import ldhdd  # noqa: E402
import ldfsck  # noqa: E402
from test_ldhdd import HDD_COM, MKLDHD, fat_copies, inputs_from, read, sha, vol_read_file  # noqa: E402


def run_fsck(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = ldfsck.main(argv)
    run_fsck.out, run_fsck.err = out.getvalue(), err.getvalue()
    return rc


def run_hdd(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = ldhdd.main(argv)
    run_hdd.out = out.getvalue()
    return rc


def entry(name, cluster, size, attr=0x20):
    n11 = name if isinstance(name, bytes) else ldhdd.to_short_name(name)
    return ldhdd.make_dir_entry(n11, attr, cluster, size, time.time())


def write_fat1(f, vol):
    """LSX のように FAT1 だけを書く"""
    f.seek(vol.fat_off)
    f.write(bytes(vol.fat))


def alloc_data(f, vol, data, free):
    """free (空きの一覧、先頭から使う) にデータを書いて鎖を作る。先頭クラスタを返す"""
    n = -(-len(data) // vol.csize)
    cs = [free.pop(0) for _ in range(n)]
    for a, b in zip(cs, cs[1:]):
        vol.set(a, b)
    vol.set(cs[-1], vol.eoc)
    for i, c in enumerate(cs):
        f.seek(vol.cluster_off(c))
        f.write(data[i * vol.csize:(i + 1) * vol.csize].ljust(vol.csize, b"\0"))
    return cs[0]


def lsx_grow(path, dirpath, name, data=b"FILE 02 OK\r\n", count=256, entries=None):
    """LSX-Dodgers 1.55〜1.62c の COPEN の誤りを真似る: いっぱいのフォルダに空きを count 個つなぎ、
    最後の 1 つだけを 0 で埋めて項目を書く (FAT1 だけ)。空きが足りなければ途中まで (終わりの印なし)。
    entries を渡すと、最後のクラスタの 0 番から、その 32 バイトの項目を並べる。新しいクラスタの一覧を返す"""
    with ldhdd._open_target(path, True) as f:
        vol = ldhdd.FatVolume(f)
        chain = vol.chain(vol.resolve_dir(dirpath), dirpath)
        free = vol.free_clusters()
        new = free[:count]
        rest = free[count:]
        vol.set(chain[-1], new[0])
        for a, b in zip(new, new[1:]):
            vol.set(a, b)
        if len(new) < count:  # 空きが尽きた: WRDF は SCF / RET で、終わりの印も 0 埋めも項目も無い
            write_fat1(f, vol)
            return new
        vol.set(new[-1], vol.eoc)
        f.seek(vol.cluster_off(new[-1]))
        f.write(bytes(vol.csize))
        if entries is None:
            entries = [entry(name, alloc_data(f, vol, data, rest) if data else 0, len(data))]
        for i, e in enumerate(entries):
            f.seek(vol.cluster_off(new[-1]) + i * 32)
            f.write(e)
        write_fat1(f, vol)
        return new


def lsx_create(path, dirpath, name, data=b"FILE 03 OK\r\n"):
    """LSX のように、鎖の頭から最初の空き (00h か E5h) に項目を書く (FAT1 だけ)"""
    with ldhdd._open_target(path, True) as f:
        vol = ldhdd.FatVolume(f)
        chain = vol.chain(vol.resolve_dir(dirpath), dirpath)
        for c in chain:
            for i in range(vol.csize // 32):
                off = vol.cluster_off(c) + i * 32
                if ldhdd._read_at(f, off, 1)[0] in (0x00, 0xE5):
                    cl = alloc_data(f, vol, data, vol.free_clusters()) if data else 0
                    f.seek(off)
                    f.write(entry(name, cl, len(data)))
                    write_fat1(f, vol)
                    return off
    raise AssertionError("空きがありません")


class FsckBase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = self.td.name
        com = read(HDD_COM)
        self.ld = ldhdd.files_from_d88(MKLDHD)["LD.BIN"][135 + len(com):]
        self.com = com

    def tearDown(self):
        self.td.cleanup()

    def vhd(self, size="40M", name="t.vhd"):
        path = os.path.join(self.dir, name)
        ldhdd.new_image(path, ldhdd.parse_size(size), inputs_from(self.ld, self.com), [], out=io.StringIO())
        return path

    def full_dir(self, path, dirname="MANY"):
        """1 クラスタでいっぱいのフォルダ (. と .. + ファイル) を ldhdd で作る"""
        with ldhdd._open_target(path, False) as f:
            per = ldhdd.FatVolume(f).csize // 32
        src = os.path.join(self.dir, "src_" + os.path.basename(path), dirname)
        os.makedirs(src)
        for i in range(per - 2):
            with open(os.path.join(src, "F%03d.TXT" % i), "wb") as fh:
                fh.write(b"FILE %02d OK\r\n" % i)
        ldhdd.add(path, [src], backup=False, out=io.StringIO())
        return per

    def out(self, name="fixed.vhd"):
        return os.path.join(self.dir, name)

    def leftovers(self):
        return [n for n in os.listdir(self.dir) if n.endswith(".part")]

    def read(self, path, name):
        with ldhdd._open_target(path, False) as f:
            return vol_read_file(ldhdd.FatVolume(f), name)

    def chain_of(self, path, dirpath):
        with ldhdd._open_target(path, False) as f:
            vol = ldhdd.FatVolume(f)
            return vol.chain(vol.resolve_dir(dirpath), dirpath)

    def free_count(self, path):
        with ldhdd._open_target(path, False) as f:
            return len(ldhdd.FatVolume(f).free_clusters())

    def assert_fixed_ok(self, src, out):
        """直した写しが、ldfsck・ldhdd から見て問題が無いこと"""
        self.assertEqual(run_fsck(["check", out]), 0, run_fsck.out)
        self.assertIn("問題はありません", run_fsck.out)
        self.assertEqual(run_hdd(["info", out]), 0)
        self.assertIn("FAT の写し: 一致", run_hdd.out)
        self.assertEqual(run_hdd(["ls", out, "-R"]), 0)
        c = fat_copies(out)
        self.assertTrue(all(x == c[0] for x in c))

    def assert_refused(self, src, regex):
        before = sha(src)
        out = self.out("refused.vhd")
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 1)
        self.assertRegex(run_fsck.err, regex)
        self.assertFalse(os.path.exists(out))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(sha(src), before)


class TestFix(FsckBase):
    def test_fresh_shape_fat16_and_fat12(self):
        for size in ("40M", "15M"):
            src = self.vhd(size, "s%s.vhd" % size)
            self.full_dir(src)
            orig = self.chain_of(src, "/MANY")
            new = lsx_grow(src, "/MANY", "G02.TXT")
            lsx_create(src, "/MANY", "G03.TXT")
            self.assertEqual(len(self.chain_of(src, "/MANY")), len(orig) + 256)
            # LSX の読み方では G02 が見えない
            run_hdd(["ls", src, "/MANY"])
            self.assertNotIn("G02.TXT", run_hdd.out)
            self.assertIn("G03.TXT", run_hdd.out)
            self.assertEqual(run_fsck(["check", src]), 2)
            self.assertIn("余分なクラスタ 255 個", run_fsck.out)  # 新しい 256 個のうち 1 つ目 (G03) は残す
            self.assertRegex(run_fsck.out, r"隠れた項目: G02\.TXT +クラスタ \d+、12 バイト → 移せます")
            self.assertIn("直せます", run_fsck.out)
            self.assertIn("FAT2 が FAT1 と", run_fsck.out)  # LSX は FAT1 だけを書く
            before = sha(src)
            free0 = self.free_count(src)
            out = self.out("f%s.vhd" % size)
            self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
            self.assertEqual(sha(src), before)
            self.assertEqual(self.chain_of(out, "/MANY"), orig + [new[0]])
            self.assertEqual(self.read(out, "/MANY/G02.TXT"), b"FILE 02 OK\r\n")
            self.assertEqual(self.read(out, "/MANY/G03.TXT"), b"FILE 03 OK\r\n")
            self.assertEqual(self.free_count(out), free0 + 255)
            run_hdd(["ls", out, "/MANY"])
            self.assertIn("G02.TXT", run_hdd.out)
            self.assert_fixed_ok(src, out)

    def test_before_g03(self):
        src = self.vhd()
        self.full_dir(src)
        new = lsx_grow(src, "/MANY", "G02.TXT")
        out = self.out()
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
        with ldhdd._open_target(out, False) as f:
            vol = ldhdd.FatVolume(f)
            raw = ldhdd._read_at(f, vol.cluster_off(new[0]), 64)
        self.assertEqual(raw[:11], b"G02     TXT")  # 1 つ目の新しいクラスタの 0 番
        self.assertEqual(raw[32], 0)  # 次が終わりの印
        self.assert_fixed_ok(src, out)

    def test_already_aligned_mirror_stays_aligned(self):
        src = self.vhd()
        self.full_dir(src)
        lsx_grow(src, "/MANY", "G02.TXT")
        with ldhdd._open_target(src, True) as f:  # FAT2 も FAT1 にそろえておく
            vol = ldhdd.FatVolume(f)
            f.seek(vol.fat_off + vol.fat_bytes)
            f.write(bytes(vol.fat))
        out = self.out()
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0)
        self.assert_fixed_ok(src, out)

    def test_sjis_name_is_moved(self):
        src = self.vhd()
        self.full_dir(src)
        name = b"\x95\x5cABC   TXT"  # 「表」の 2 バイト目は 5Ch
        lsx_grow(src, "/MANY", None, entries=[entry(name, 0, 0)])
        out = self.out()
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
        with ldhdd._open_target(out, False) as f:
            vol = ldhdd.FatVolume(f)
            names = [e.name for e in vol.read_dir(vol.resolve_dir("/MANY")).entries]
        self.assertIn(name, names)


class TestCapacity(FsckBase):
    """「移す項目 k 個 + 終わりの印 1 個」が、終わりの印のあるクラスタに入るか"""

    def grow_with_room(self, room, k, count=256):
        src = self.vhd(name="r%d_k%d_c%d.vhd" % (room, k, count))
        per = self.full_dir(src)
        ents = [entry("H%d.TXT" % i, 0, 0) for i in range(k)]
        new = lsx_grow(src, "/MANY", None, count=count, entries=ents)
        for i in range(per - room):  # 1 つ目の新しいクラスタを、終わりの印から後ろが room 個になるまで埋める
            lsx_create(src, "/MANY", "N%03d.TXT" % i, data=b"")
        return src, new, per

    def check_fixed(self, src, out, k, chain_len):
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
        self.assertEqual(len(self.chain_of(out, "/MANY")), chain_len)
        run_hdd(["ls", out, "/MANY"])
        for i in range(k):
            self.assertIn("H%d.TXT" % i, run_hdd.out)
        self.assert_fixed_ok(src, out)

    def test_exact_fit_with_marker(self):
        src, new, per = self.grow_with_room(2, 1)  # 項目 1 + 終わりの印 1
        self.check_fixed(src, self.out("a.vhd"), 1, 2)

    def test_entry_fits_but_marker_does_not(self):
        src, new, per = self.grow_with_room(1, 1)  # 項目は入るが、終わりの印が入らない
        self.check_fixed(src, self.out("b.vhd"), 1, 3)

    def test_two_entries_one_slot(self):
        src, new, per = self.grow_with_room(1, 2)
        self.check_fixed(src, self.out("c.vhd"), 2, 3)

    def test_spill_into_last_cluster_itself(self):
        # 終わりの印が 255 番目の新しいクラスタにあり、余分なクラスタが最後の 1 つだけ (それが隠れた項目のあるクラスタ)
        # の時に、そこへ続きを置く
        src = self.vhd(name="spill.vhd")
        per = self.full_dir(src)
        new = lsx_grow(src, "/MANY", None, entries=[entry("H%d.TXT" % i, 0, 0) for i in range(2)])
        n = 0
        with ldhdd._open_target(src, True) as f:
            vol = ldhdd.FatVolume(f)
            for j, c in enumerate(new[:255]):
                count = per if j < 254 else per - 1
                buf = bytearray(vol.csize)
                for i in range(count):
                    buf[i * 32:i * 32 + 32] = entry("N%05d.TXT" % n, 0, 0)
                    n += 1
                f.seek(vol.cluster_off(c))
                f.write(bytes(buf))
        self.check_fixed(src, self.out("d.vhd"), 2, 257)


class TestRefuse(FsckBase):
    def grown(self, name="g.vhd"):
        src = self.vhd(name=name)
        self.full_dir(src)
        new = lsx_grow(src, "/MANY", "G02.TXT")
        return src, new

    def poke(self, path, off, data):
        with open(path, "r+b") as f:
            f.seek(off)
            f.write(data)

    def cl_off(self, path, c):
        with ldhdd._open_target(path, False) as f:
            return ldhdd.FatVolume(f).cluster_off(c)

    def test_out_of_space_chain_without_end(self):
        src = self.vhd("15M")
        self.full_dir(src)
        with ldhdd._open_target(src, False) as f:
            vol = ldhdd.FatVolume(f)
            free, cs = len(vol.free_clusters()), vol.csize
        big = os.path.join(self.dir, "BIG.BIN")
        with open(big, "wb") as fh:
            fh.write(bytes((free - 100) * cs))
        ldhdd.add(src, [big], backup=False, out=io.StringIO())
        new = lsx_grow(src, "/MANY", "G02.TXT")
        self.assertEqual(len(new), 100)
        self.assertEqual(run_fsck(["check", src]), 2)
        self.assertRegex(run_fsck.out, r"壊れ方: /MANY: .*切れて")
        self.assert_refused(src, "切れて")

    def test_garbage_in_middle_tail(self):
        src, new = self.grown()
        self.poke(src, self.cl_off(src, new[5]) + 100, b"old data")
        self.assertEqual(run_fsck(["check", src]), 2)
        self.assertIn("余分なクラスタ %d が 0 ではありません" % new[5], run_fsck.out)
        self.assert_refused(src, "形と違います")

    def test_garbage_after_marker_in_marker_cluster(self):
        src, new = self.grown()
        lsx_create(src, "/MANY", "G03.TXT")  # 1 つ目の新しいクラスタの 0 番。1 番が終わりの印
        self.poke(src, self.cl_off(src, new[0]) + 5 * 32 + 3, b"x")
        self.assertEqual(run_fsck(["check", src]), 2)
        self.assertIn("終わりの印のあるクラスタの、印より後ろが 0 ではありません", run_fsck.out)
        self.assert_refused(src, "形と違います")

    def test_valid_looking_stale_entry_after_marker(self):
        # (a) 最後でない余分なクラスタに、正しい形の空のファイルの項目
        src, new = self.grown("a.vhd")
        self.poke(src, self.cl_off(src, new[3]), entry("OLD.TXT", 0, 0))
        self.assert_refused(src, "形と違います")
        # (b) 最後の余分なクラスタで、項目の並びの後ろに隙間を挟んで同じ形の項目
        src, new = self.grown("b.vhd")
        self.poke(src, self.cl_off(src, new[-1]) + 3 * 32, entry("OLD.TXT", 0, 0))
        self.assert_refused(src, "形と違います")

    def test_valid_looking_stale_entry_before_marker_is_kept_and_flagged(self):
        # (c) 1 つ目の新しいクラスタの 0 番 (終わりの印の前): 本物と見分けられないので見える項目として残す (限界)
        src, new = self.grown()
        self.poke(src, self.cl_off(src, new[0]), entry("OLD.TXT", 0, 0))
        self.assertEqual(run_fsck(["check", src, "-v"]), 2)
        self.assertIn("確かめてください: OLD.TXT", run_fsck.out)
        out = self.out()
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
        run_hdd(["ls", out, "/MANY"])
        self.assertIn("OLD.TXT", run_hdd.out)
        self.assertIn("G02.TXT", run_hdd.out)

    def test_verbose_warns_for_all_added_clusters(self):
        # 終わりの印が 2 つ目の足されたクラスタにある時、1 つ目の足されたクラスタの見える項目にも確かめを促す
        src, new = self.grown("v.vhd")
        with ldhdd._open_target(src, True) as f:
            vol = ldhdd.FatVolume(f)
            per = vol.csize // 32
            buf = bytearray(vol.csize)
            buf[0:32] = entry("OLD.TXT", 0, 0)
            for i in range(1, per):
                buf[i * 32:i * 32 + 32] = entry("N%03d.TXT" % i, 0, 0)
            f.seek(vol.cluster_off(new[0]))
            f.write(bytes(buf))
            f.seek(vol.cluster_off(new[1]))
            f.write(entry("NEWER.TXT", 0, 0))
        self.assertEqual(run_fsck(["check", src, "-v"]), 2)
        self.assertIn("確かめてください: OLD.TXT", run_fsck.out)
        self.assertIn("確かめてください: NEWER.TXT", run_fsck.out)
        self.assertNotIn("確かめてください: F000.TXT", run_fsck.out)  # 足す前のクラスタは出さない

    def test_orphan_chain_into_tail(self):
        src, new = self.grown()
        with ldhdd._open_target(src, True) as f:
            vol = ldhdd.FatVolume(f)
            x = vol.free_clusters()[0]
            vol.set(x, new[10])
            write_fat1(f, vol)
        self.assertEqual(run_fsck(["check", src]), 2)
        self.assertIn("外のクラスタ %d が、このフォルダの鎖のクラスタ %d を指しています" % (x, new[10]), run_fsck.out)
        self.assert_refused(src, "外のクラスタ")

    def test_orphan_chain_into_kept_marker_cluster(self):
        # 残す (終わりの印のある) クラスタを外から指す鎖も、直すと途中で切れるので止まる
        src, new = self.grown("k.vhd")
        with ldhdd._open_target(src, True) as f:
            vol = ldhdd.FatVolume(f)
            x = vol.free_clusters()[0]
            vol.set(x, new[0])
            write_fat1(f, vol)
        self.assertEqual(run_fsck(["check", src]), 2)
        self.assertIn("外のクラスタ %d が、このフォルダの鎖のクラスタ %d を指しています" % (x, new[0]), run_fsck.out)
        self.assert_refused(src, "外のクラスタ")

    def test_hidden_entry_not_movable(self):
        cases = {
            "dup": (lambda vol, used: entry("F000.TXT", 0, 0), "同じ名前"),
            "overlap": (lambda vol, used: entry("X.TXT", used, 10), "クラスタ %d を /MANY/F001.TXT と共有しています"),
            "dir": (lambda vol, used: entry("D", 0, 0, attr=0x10), "フォルダの項目"),
            "lower": (lambda vol, used: entry(b"abc     TXT", 0, 0), "8.3"),
        }
        for key, (mk, why) in cases.items():
            src = self.vhd(name="h_%s.vhd" % key)
            self.full_dir(src)
            with ldhdd._open_target(src, False) as f:
                vol = ldhdd.FatVolume(f)
                used = vol.find(vol.read_dir(vol.resolve_dir("/MANY")), ldhdd.to_short_name("F001.TXT")).cluster
                e = mk(vol, used)
            lsx_grow(src, "/MANY", None, entries=[e])
            self.assertEqual(run_fsck(["check", src]), 2, key)
            self.assertIn("移せません", run_fsck.out, key)
            self.assert_refused(src, why % used if "%d" in why else why)
        # サイズと鎖の長さが合わない (鎖そのものは、ほかと重ならない)
        src = self.vhd(name="h_size.vhd")
        self.full_dir(src)
        new = lsx_grow(src, "/MANY", "X.TXT", data=b"x" * 100)
        with ldhdd._open_target(src, True) as f:
            vol = ldhdd.FatVolume(f)
            f.seek(vol.cluster_off(new[-1]) + 28)
            f.write((vol.csize * 5).to_bytes(4, "little"))
        self.assertEqual(run_fsck(["check", src]), 2)
        self.assert_refused(src, "合いません")


class TestCli(FsckBase):
    def test_clean_image(self):
        src = self.vhd()
        self.full_dir(src)
        self.assertEqual(run_fsck(["check", src]), 0)
        self.assertIn("問題はありません", run_fsck.out)
        out = self.out()
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0)
        self.assertIn("直す所はありません", run_fsck.out)
        self.assertFalse(os.path.exists(out))
        self.assertEqual(self.leftovers(), [])

    def test_short_preallocated_dir_is_not_lsx_damage(self):
        # 2 クラスタのフォルダ: 終わりの印が 1 つ目にあり、2 つ目の頭に正しく見える古い項目 → LSX の誤りの形ではない
        src = self.vhd()
        self.full_dir(src)
        with ldhdd._open_target(src, True) as f:
            vol = ldhdd.FatVolume(f)
            ch = vol.chain(vol.resolve_dir("/MANY"), "m")
            c2 = vol.free_clusters()[0]
            vol.set(ch[-1], c2)
            vol.set(c2, vol.eoc)
            for k in range(vol.nfats):
                f.seek(vol.fat_off + k * vol.fat_bytes)
                f.write(bytes(vol.fat))
            f.seek(vol.cluster_off(ch[-1]) + 10 * 32)  # 1 つ目のクラスタの 10 番から後ろを空ける (終わりの印)
            f.write(bytes(vol.csize - 10 * 32))
            f.seek(vol.cluster_off(c2))
            f.write(entry("GHOST.TXT", 0, 0) + bytes(vol.csize - 32))
        self.assertEqual(run_fsck(["check", src]), 2)
        self.assertIn("LSX-Dodgers の誤り", run_fsck.out)
        self.assertIn("と合いません", run_fsck.out)
        self.assert_refused(src, "形と違います")

    def test_out_exists_same_and_dry_run(self):
        src = self.vhd()
        self.full_dir(src)
        lsx_grow(src, "/MANY", "G02.TXT")
        out = self.out()
        with open(out, "wb") as fh:
            fh.write(b"keep")
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 1)
        self.assertIn("既にあります", run_fsck.err)
        self.assertEqual(read(out), b"keep")
        self.assertEqual(run_fsck(["fix", src, "--out", src]), 1)
        out2 = self.out("dry.vhd")
        before = sha(src)
        self.assertEqual(run_fsck(["fix", src, "--out", out2, "--dry-run"]), 0)
        self.assertIn("直す: /MANY", run_fsck.out)
        self.assertIn("--dry-run", run_fsck.out)
        self.assertFalse(os.path.exists(out2))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(sha(src), before)

    def test_lost_clusters_reported_and_kept(self):
        src = self.vhd()
        self.full_dir(src)
        with ldhdd._open_target(src, True) as f:
            vol = ldhdd.FatVolume(f)
            x = vol.free_clusters()[-1]
            vol.set(x, vol.eoc)
            write_fat1(f, vol)
        self.assertEqual(run_fsck(["check", src]), 2)
        self.assertIn("どこからも使われていないクラスタ: 1 個 (鎖の頭: %d)" % x, run_fsck.out)
        out = self.out()
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0)
        self.assertIn("使われていないクラスタは、そのまま残します", run_fsck.out)
        self.assertFalse(os.path.exists(out))
        # 直す所もある時: 直して、使われていないクラスタは残す
        lsx_grow(src, "/MANY", "G02.TXT")
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
        with ldhdd._open_target(out, False) as f:
            self.assertNotEqual(ldhdd.FatVolume(f).get(x), 0)
        self.assertEqual(run_fsck(["check", out]), 2)
        self.assertIn("まとめ: 直せるフォルダ 0 個、直せないフォルダ 0 個、壊れ方 0 件、使われていないクラスタ 1 個", run_fsck.out)

    def test_verify_failure_publishes_nothing(self):
        src = self.vhd()
        self.full_dir(src)
        lsx_grow(src, "/MANY", "G02.TXT")
        orig = ldfsck.apply_plan

        def skip_entries(f, vol, plan):  # 移す項目を書かずに FAT だけ書く
            orig(f, vol, plan._replace(entry_writes=[]))

        def bad_fat2(f, vol, plan):
            orig(f, vol, plan)
            f.seek(vol.fat_off + vol.fat_bytes + 7)
            f.write(b"\x99")
        for fake, why in ((skip_entries, "ディレクトリ項目 \\(位置 \\d+\\) が計画と違います"), (bad_fat2, "FAT2 が計画と違います")):
            ldfsck.apply_plan = fake
            try:
                self.assert_refused(src, why)
            finally:
                ldfsck.apply_plan = orig

    def test_plan_comes_from_copy(self):
        src, out = self.vhd(), self.out()
        self.full_dir(src)
        new = lsx_grow(src, "/MANY", "G02.TXT")
        orig = ldfsck._copy_source
        with ldhdd._open_target(src, False) as f:
            garbage_at = ldhdd.FatVolume(f).cluster_off(new[5])

        def copy_then_change(sf, fd):
            orig(sf, fd)
            with open(src, "r+b") as fh:  # 写した後で元が変わっても、計画は写しから作る
                fh.seek(garbage_at)
                fh.write(b"changed")
        ldfsck._copy_source = copy_then_change
        try:
            self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
        finally:
            ldfsck._copy_source = orig
        self.assert_fixed_ok(src, out)
        self.assertEqual(run_fsck(["check", src]), 2)  # 元は変わった後の形 (形が違う) のまま
        self.assertIn("0 ではありません", run_fsck.out)


class TestPathsNamesAndTails(FsckBase):
    def test_detached_long_name_before_marker_is_invalidated(self):
        src = self.vhd()
        self.full_dir(src)
        new = lsx_grow(src, "/MANY", "G02.TXT")
        n11 = ldhdd.to_short_name("G02.TXT")
        lfn = bytearray(32)
        lfn[0] = 0x41
        for i, ch in enumerate("Old.txt"):
            lfn[1 + 2 * i] = ord(ch)
        lfn[11] = 0x0F
        lfn[13] = ldhdd._lfn_checksum(n11)
        with open(src, "r+b") as fh:  # 1 つ目の新しいクラスタの 0 番に、G02 と組になる長い名前 (宙に浮いている)
            with ldhdd._open_target(src, False) as f:
                off = ldhdd.FatVolume(f).cluster_off(new[0])
            fh.seek(off)
            fh.write(bytes(lfn))
        out = self.out()
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
        self.assertIn("長い名前の項目 1 個を消した印にする", run_fsck.out)
        with ldhdd._open_target(out, False) as f:
            vol = ldhdd.FatVolume(f)
            e = vol.find(vol.read_dir(vol.resolve_dir("/MANY")), n11)
            self.assertIsNotNone(e)
            self.assertIsNone(e.long_name)
            self.assertEqual(ldhdd._read_at(f, off, 1), b"\xe5")
        self.assert_fixed_ok(src, out)

    def test_copy_failure_leaves_nothing(self):
        src = self.vhd()
        self.full_dir(src)
        lsx_grow(src, "/MANY", "G02.TXT")
        orig = ldfsck._copy_source

        def fail(sf, fd):
            os.write(fd, b"partial")
            raise OSError(28, "injected ENOSPC")
        ldfsck._copy_source = fail
        try:
            self.assert_refused(src, "injected ENOSPC")
        finally:
            ldfsck._copy_source = orig

    @unittest.skipIf(os.name == "nt", "シンボリックリンクの試験は POSIX だけ")
    def test_out_through_symlink_parent(self):
        src = self.vhd()
        self.full_dir(src)
        lsx_grow(src, "/MANY", "G02.TXT")
        a, b = os.path.join(self.dir, "a"), os.path.join(self.dir, "b")
        os.makedirs(os.path.join(b, "nested"))
        os.makedirs(a)
        os.symlink(os.path.join(b, "nested"), os.path.join(a, "link"))
        out = os.path.join(a, "link", "..", "fixed.vhd")  # ファイルシステムでは b/fixed.vhd
        same = os.path.join(a, "fixed.vhd")  # 文字の上では out と同じ名前になる元 (別のファイル)
        os.rename(src, same)
        src = same
        seen = []
        orig_stage, orig_sync = ldhdd._create_staging, ldhdd._fsync_dir

        def stage(d):
            seen.append(("stage", os.path.realpath(d)))
            return orig_stage(d)

        def sync(d):
            seen.append(("sync", os.path.realpath(d)))
            return orig_sync(d)
        ldhdd._create_staging, ldhdd._fsync_dir = stage, sync
        try:
            self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
        finally:
            ldhdd._create_staging, ldhdd._fsync_dir = orig_stage, orig_sync
        # 仮の写しも、名前を付けた後の同期も、出力と同じ所 (b) で行う
        self.assertEqual(seen, [("stage", os.path.realpath(b)), ("sync", os.path.realpath(b))])
        self.assertTrue(os.path.isfile(os.path.join(b, "fixed.vhd")))
        self.assertEqual(sorted(os.listdir(a)), ["fixed.vhd", "link"])
        self.assertEqual([n for n in os.listdir(b) if n.endswith(".part")], [])

    def test_empty_tail_is_only_a_note(self):
        src = self.vhd()
        self.full_dir(src)
        lsx_grow(src, "/MANY", None, entries=[])  # 余分なクラスタが全部 0 (隠れた項目なし)
        with ldhdd._open_target(src, True) as f:  # FAT2 もそろえておく (ほかの道具が前もって伸ばした形)
            vol = ldhdd.FatVolume(f)
            f.seek(vol.fat_off + vol.fat_bytes)
            f.write(bytes(vol.fat))
        self.assertEqual(run_fsck(["check", src]), 0, run_fsck.out)
        self.assertIn("全部 0 のクラスタが 255 個つながっています", run_fsck.out)
        out = self.out()
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0)
        self.assertIn("直す所はありません", run_fsck.out)
        self.assertFalse(os.path.exists(out))

    def test_hidden_name_collides_with_long_name(self):
        src = self.vhd()
        self.full_dir(src)
        new = lsx_grow(src, "/MANY", "GHOST.TXT")
        alias = ldhdd.to_short_name("GHOST~1.TXT")
        lfn = bytearray(32)
        lfn[0] = 0x41
        units = "Ghost.txt"
        for i, ch in enumerate(units):
            pos = [1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30][i]
            lfn[pos] = ord(ch)
        pos_end = [1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30][len(units)]
        lfn[pos_end:pos_end + 2] = b"\0\0"
        lfn[11] = 0x0F
        lfn[13] = ldhdd._lfn_checksum(alias)
        with ldhdd._open_target(src, True) as f:  # 1 つ目の新しいクラスタに、長い名前 Ghost.txt を持つ GHOST~1.TXT
            vol = ldhdd.FatVolume(f)
            f.seek(vol.cluster_off(new[0]))
            f.write(bytes(lfn) + entry(alias, 0, 0))
        with ldhdd._open_target(src, False) as f:
            vol = ldhdd.FatVolume(f)
            e = vol.find(vol.read_dir(vol.resolve_dir("/MANY")), alias)
            self.assertEqual(e.long_name, "Ghost.txt")
        self.assertEqual(run_fsck(["check", src]), 2)
        self.assertIn("長い名前と重なります", run_fsck.out)
        self.assert_refused(src, "長い名前と重なります")

    def test_skipped_long_name_invalidation_is_caught(self):
        src = self.vhd()
        self.full_dir(src)
        new = lsx_grow(src, "/MANY", "G02.TXT")
        lfn = bytearray(32)
        lfn[0] = 0x41
        for i, ch in enumerate("Old.txt"):
            lfn[1 + 2 * i] = ord(ch)
        lfn[11] = 0x0F
        lfn[13] = ldhdd._lfn_checksum(ldhdd.to_short_name("G02.TXT"))
        with ldhdd._open_target(src, True) as f:
            vol = ldhdd.FatVolume(f)
            f.seek(vol.cluster_off(new[0]))
            f.write(bytes(lfn))
        orig = ldfsck.apply_plan

        def skip_lfn(f, vol, plan):  # 長い名前の項目を消した印にする書き込みだけを飛ばす
            orig(f, vol, plan._replace(entry_writes=[w for w in plan.entry_writes if w[1][:1] != b"\xe5"]))
        ldfsck.apply_plan = skip_lfn
        try:
            self.assert_refused(src, "ディレクトリ項目 \\(位置 \\d+\\) が計画と違います")
        finally:
            ldfsck.apply_plan = orig

    def test_fix_uses_streaming_reads_only(self):
        # 直せるフォルダと、全部 0 の余分なクラスタを持つフォルダが両方ある。確かめでも項目ごとの読み込み (read_dir) を使わない
        src = self.vhd()
        self.full_dir(src, "D1")
        self.full_dir(src, "D2")
        lsx_grow(src, "/D1", "G02.TXT")
        lsx_grow(src, "/D2", None, entries=[])
        orig = ldhdd.FatVolume.read_dir

        def boom(self_, *a, **k):
            raise AssertionError("read_dir を使いました")
        ldhdd.FatVolume.read_dir = boom
        out = self.out()
        try:
            self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
        finally:
            ldhdd.FatVolume.read_dir = orig
        self.assertIn("全部 0 のクラスタが 255 個", run_fsck.out)
        self.assertEqual(len(self.chain_of(out, "/D2")), 257)  # 全部 0 の余分なクラスタはそのまま
        self.assertEqual(len(self.chain_of(out, "/D1")), 2)

    def test_long_name_runs_are_bounded(self):
        # 崩れた長い名前の項目がたくさん並んでも、持つのは正しい並びだけ (終わりの印の直前の宙に浮いた物は 31 個まで)
        src = self.vhd()
        self.full_dir(src)
        new = lsx_grow(src, "/MANY", "G02.TXT")
        with ldhdd._open_target(src, True) as f:
            vol = ldhdd.FatVolume(f)
            per = vol.csize // 32
            buf = bytearray(vol.csize)
            for i in range(per - 1):  # 40h の無い (並びの崩れた) 長い名前の項目で、1 つ目の新しいクラスタを埋める
                buf[i * 32] = 0x05
                buf[i * 32 + 11] = 0x0F
            f.seek(vol.cluster_off(new[0]))
            f.write(bytes(buf))
        with ldhdd._open_target(src, False) as f:
            vol = ldhdd.FatVolume(f)
            rep_ = ldfsck.scan(vol)
        d = [x for x in rep_.dirs if x.path == "/MANY"][0]
        self.assertEqual(d.detached_lfn, [])
        self.assertTrue(d.repairable)
        out = self.out()
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
        self.assert_fixed_ok(src, out)

    def test_empty_tail_with_stale_padding_does_not_block(self):
        src = self.vhd()
        self.full_dir(src, "D1")
        self.full_dir(src, "D2")
        lsx_grow(src, "/D1", "G02.TXT")
        new2 = lsx_grow(src, "/D2", None, entries=[])
        with ldhdd._open_target(src, True) as f:  # D2 の終わりの印 (1 つ目の新しいクラスタの 0 番) の後ろに古いバイト
            vol = ldhdd.FatVolume(f)
            f.seek(vol.cluster_off(new2[0]) + 5 * 32 + 1)
            f.write(b"stale")
        out = self.out()
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
        self.assertIn("全部 0 のクラスタが 255 個", run_fsck.out)
        self.assertEqual(len(self.chain_of(out, "/D1")), 2)
        self.assertEqual(len(self.chain_of(out, "/D2")), 257)

    def test_dir_sync_failure_after_publication(self):
        src = self.vhd()
        self.full_dir(src)
        lsx_grow(src, "/MANY", "G02.TXT")
        out = self.out()
        orig = ldhdd._fsync_dir

        def fail(d):
            raise OSError(5, "injected sync")
        ldhdd._fsync_dir = fail
        try:
            self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
        finally:
            ldhdd._fsync_dir = orig
        self.assertIn("直した写しを書きました: %s" % out, run_fsck.out)
        self.assertIn("同期を確かめられませんでした (", run_fsck.out)
        self.assertTrue(os.path.isfile(out))
        self.assert_fixed_ok(src, out)

    def test_orphan_into_hidden_file_chain(self):
        # 隠れたファイルのデータの鎖を、外の (どこからも使われていない) 鎖も指している → 移さない
        src = self.vhd()
        self.full_dir(src)
        lsx_grow(src, "/MANY", "G02.TXT", data=b"x" * 5000)
        with ldhdd._open_target(src, True) as f:
            vol = ldhdd.FatVolume(f)
            ch = vol.chain(vol.resolve_dir("/MANY"), "m")
            raw = ldhdd._read_at(f, vol.cluster_off(ch[-1]), 32)
            data_cl = ldhdd._u16(raw, 26)
            y = vol.free_clusters()[0]
            vol.set(y, data_cl)
            write_fat1(f, vol)
        self.assertEqual(run_fsck(["check", src]), 2)
        self.assertIn("外のクラスタ %d が、このファイルの鎖のクラスタ %d を指しています" % (y, data_cl), run_fsck.out)
        self.assert_refused(src, "このファイルの鎖")

    def test_hidden_name_displays_like_visible_name(self):
        # CP932 で同じ「≒」に見える 2 つの短い名前 (81 E0 と 87 90) → 移さない
        src = self.vhd()
        self.full_dir(src)
        new = lsx_grow(src, "/MANY", None, entries=[entry(b"\x87\x90         ", 0, 0)])
        with ldhdd._open_target(src, True) as f:  # 1 つ目の新しいクラスタの 0 番に、見える項目 81 E0
            vol = ldhdd.FatVolume(f)
            f.seek(vol.cluster_off(new[0]))
            f.write(entry(b"\x81\xe0         ", 0, 0))
        self.assertEqual(run_fsck(["check", src]), 2)
        self.assertIn("表示した名前が同じになります", run_fsck.out)
        self.assert_refused(src, "表示した名前が同じ")

    def test_dry_run_makes_no_copy(self):
        src = self.vhd()
        self.full_dir(src)
        lsx_grow(src, "/MANY", "G02.TXT")
        orig = ldhdd._create_staging

        def boom(d):
            raise AssertionError("--dry-run で仮の写しを作りました")
        ldhdd._create_staging = boom
        try:
            self.assertEqual(run_fsck(["fix", src, "--out", self.out(), "--dry-run"]), 0, run_fsck.err)
            self.assertIn("直す: /MANY", run_fsck.out)
            self.assertIn("写しも作りません", run_fsck.out)
            # 直せない形でも、写しを作らずに止まる
            with ldhdd._open_target(src, True) as f:
                vol = ldhdd.FatVolume(f)
                ch = vol.chain(vol.resolve_dir("/MANY"), "m")
                f.seek(vol.cluster_off(ch[5]) + 7)
                f.write(b"junk")
            self.assertEqual(run_fsck(["fix", src, "--out", self.out(), "--dry-run"]), 1)
            self.assertIn("形と違います", run_fsck.err)
        finally:
            ldhdd._create_staging = orig
        self.assertFalse(os.path.exists(self.out()))

    def test_cp932_names_that_decode_alike(self):
        src = self.vhd()
        self.full_dir(src, "D1")
        self.full_dir(src, "D2")
        lsx_grow(src, "/D1", "G02.TXT")
        lsx_grow(src, "/D2", "G02.TXT")
        raw_names = {b"D1         ": b"\x81\xe0         ", b"D2         ": b"\x87\x90         "}
        self.assertEqual(ldfsck.name_text(b"\x81\xe0         "), ldfsck.name_text(b"\x87\x90         "))
        with ldhdd._open_target(src, True) as f:
            vol = ldhdd.FatVolume(f)
            for e in vol.read_dir(0).entries:
                if e.name in raw_names:
                    f.seek(e.off)
                    f.write(raw_names[e.name])
        out = self.out()
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
        with ldhdd._open_target(out, False) as f:
            vol = ldhdd.FatVolume(f)
            for e in vol.read_dir(0).entries:
                if e.is_dir:
                    names = [x.name for x in vol.read_dir(e.cluster).entries]
                    self.assertIn(b"G02     TXT", names)
                    self.assertEqual(len(vol.chain(e.cluster, "d")), 2)
        self.assert_fixed_ok(src, out)


class TestDotEntriesAndDeepTrees(FsckBase):
    def set_dotdot(self, path, dirpath, cluster):
        with ldhdd._open_target(path, True) as f:
            vol = ldhdd.FatVolume(f)
            off = vol.cluster_off(vol.resolve_dir(dirpath)) + 32  # 1 番が ..
            assert ldhdd._read_at(f, off, 11) == ldhdd.DOTDOT_NAME
            f.seek(off + 26)
            f.write(cluster.to_bytes(2, "little"))

    def test_dotdot_into_tail_blocks_repair(self):
        src = self.vhd()
        self.full_dir(src, "A")
        self.full_dir(src, "B")
        new = lsx_grow(src, "/A", "G02.TXT")
        self.set_dotdot(src, "/B", new[10])  # B の .. が、A の空きに戻す予定のクラスタを指す
        self.assertEqual(run_fsck(["check", src]), 2)
        self.assertIn("/B の .. が、親 ではなくクラスタ %d を指しています" % new[10], run_fsck.out)
        self.assert_refused(src, "空きに戻す予定のクラスタ %d" % new[10])

    def test_dotdot_elsewhere_is_only_a_note(self):
        src = self.vhd()
        self.full_dir(src, "A")
        self.full_dir(src, "B")
        lsx_grow(src, "/A", "G02.TXT")
        with ldhdd._open_target(src, False) as f:
            vol = ldhdd.FatVolume(f)
            a_head = vol.resolve_dir("/A")
        self.set_dotdot(src, "/B", a_head)  # 合わないが、空きに戻す所ではない
        out = self.out()
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
        self.assertIn("/B の .. が、親 ではなくクラスタ %d を指しています" % a_head, run_fsck.out)
        self.assertEqual(len(self.chain_of(out, "/A")), 2)

    def test_deep_tree_memory(self):
        self._deep_tree(bad_dotdot=False)

    def test_deep_tree_with_wrong_dotdot_memory(self):
        # どの .. も親を指していない深い木: 注は数を限って持ち、道筋の文を何千も持たない
        rep_ = self._deep_tree(bad_dotdot=True)
        self.assertEqual(len(rep_.notes), ldfsck.LIMIT)
        self.assertEqual(rep_.more_notes, 4000 - ldfsck.LIMIT)

    def test_deep_tree_dotdot_into_tail_refusal_memory(self):
        # 深い木のどの .. も、直す予定のフォルダの空きに戻すクラスタを指す: 止める理由の文も数を限って作る
        import tracemalloc
        src = self.vhd()
        self.full_dir(src, "A")
        new = lsx_grow(src, "/A", "G02.TXT")
        self._deep_tree(bad_dotdot=True, src=src, dotdot_to=new[10], measure=False)
        tracemalloc.start()
        try:
            with self.assertRaisesRegex(ldhdd.LdhddError, "ほか \\d+ 件"):
                ldfsck.fix(src, self.out(), dry_run=True, out=io.StringIO())
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 30 * 1024 * 1024, "fix --dry-run の最大 %d バイト" % peak)

    def _deep_tree(self, bad_dotdot, src=None, dotdot_to=None, measure=True):
        import tracemalloc
        depth = 4000
        src = src or self.vhd()
        with ldhdd._open_target(src, True) as f:
            vol = ldhdd.FatVolume(f)
            cs = vol.free_clusters()[:depth]
            for i, c in enumerate(cs):
                vol.set(c, vol.eoc)
                buf = bytearray(vol.csize)
                buf[0:32] = entry(ldhdd.DOT_NAME, c, 0, attr=0x10)
                dd = (dotdot_to or c) if bad_dotdot else (cs[i - 1] if i else 0)
                buf[32:64] = entry(ldhdd.DOTDOT_NAME, dd, 0, attr=0x10)
                if i + 1 < depth:
                    buf[64:96] = entry("DEEPNAME", cs[i + 1], 0, attr=0x10)  # 8 字: 道筋の文字列を全部持つと 70MB 近くになる
                f.seek(vol.cluster_off(c))
                f.write(bytes(buf))
            for k in range(vol.nfats):
                f.seek(vol.fat_off + k * vol.fat_bytes)
                f.write(bytes(vol.fat))
            off = vol.root_off
            while ldhdd._read_at(f, off, 1)[0] not in (0, 0xE5):  # ルートの空きに置く
                off += 32
            f.seek(off)
            f.write(entry("DEEPNAME", cs[0], 0, attr=0x10))
        if not measure:
            return None
        with ldhdd._open_target(src, False) as f:
            vol = ldhdd.FatVolume(f)
            tracemalloc.start()
            try:
                rep_ = ldfsck.scan(vol)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        self.assertEqual(len(rep_.dirs), depth)
        self.assertTrue(rep_.clean)
        self.assertLess(peak, 30 * 1024 * 1024, "scan の最大 %d バイト" % peak)
        if bad_dotdot:
            self.assertEqual(run_fsck(["check", src]), 0)
            self.assertIn("注: ほか %d 件" % (depth - ldfsck.LIMIT), run_fsck.out)
        return rep_

    def test_control_characters_are_escaped(self):
        src = self.vhd()
        self.full_dir(src)
        lsx_grow(src, "/MANY", None, entries=[entry(b"A\nB\x1b[2J TXT", 0, 0)])
        self.assertEqual(run_fsck(["check", src]), 2)
        self.assertIn("A\\x0aB\\x1b[2J.TXT", run_fsck.out)
        self.assertNotIn("\x1b", run_fsck.out)
        self.assertFalse(any("A" == line.strip()[-1:] for line in run_fsck.out.splitlines() if "隠れた項目" in line))
        self.assert_refused(src, "8.3")
        self.assertNotIn("\x1b", run_fsck.err)


class TestPrefixOrphansAndLocks(FsckBase):
    def test_e5_in_pre_growth_prefix_is_ambiguous(self):
        src = self.vhd()
        self.full_dir(src)
        lsx_grow(src, "/MANY", "G02.TXT")
        with ldhdd._open_target(src, True) as f:  # 足す前のクラスタの 5 番を消した印にする
            vol = ldhdd.FatVolume(f)
            ch = vol.chain(vol.resolve_dir("/MANY"), "m")
            f.seek(vol.cluster_off(ch[0]) + 5 * 32)
            f.write(b"\xe5")
        self.assertEqual(run_fsck(["check", src]), 2)
        self.assertIn("消した項目の跡 (E5h) があり", run_fsck.out)
        self.assert_refused(src, "形と違います")

    def test_e5_after_growth_is_fine(self):
        # 足した後のクラスタ (終わりの印より前) で消した項目の跡は、LSX の誤りの後で消した物として直してよい
        src = self.vhd()
        self.full_dir(src)
        new = lsx_grow(src, "/MANY", "G02.TXT")
        off = lsx_create(src, "/MANY", "G03.TXT", data=b"")  # 消すので中身は無し (LSX の DEL なら鎖も空きに戻る)
        lsx_create(src, "/MANY", "G04.TXT")
        with open(src, "r+b") as fh:
            fh.seek(off)
            fh.write(b"\xe5")
        out = self.out()
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
        self.assert_fixed_ok(src, out)

    def orphan(self, src, links):
        with ldhdd._open_target(src, True) as f:
            vol = ldhdd.FatVolume(f)
            for c, v in links:
                vol.set(c, v)
            write_fat1(f, vol)

    def free_cl(self, src, n):
        with ldhdd._open_target(src, False) as f:
            return ldhdd.FatVolume(f).free_clusters()[-n:]

    def test_broken_orphan_chains_block_repair(self):
        cases = {
            "to_free": lambda a, b, c: [(a, b)],  # b は空きのまま
            "cycle": lambda a, b, c: [(a, b), (b, a)],
            "merge": lambda a, b, c: [(a, c), (b, c), (c, 0xFFFF)],
            "range": lambda a, b, c: [(a, 1)],
        }
        msgs = {"to_free": "途中で切れています", "cycle": "循環しています", "merge": "合流しています", "range": "範囲外"}
        for key, mk in cases.items():
            src = self.vhd(name="o_%s.vhd" % key)
            self.full_dir(src)
            lsx_grow(src, "/MANY", "G02.TXT")
            a, b, c = self.free_cl(src, 3)
            self.orphan(src, [(x, min(v, 0xFFFF)) for x, v in mk(a, b, c)])
            self.assertEqual(run_fsck(["check", src]), 2, key)
            self.assertIn(msgs[key], run_fsck.out, key)
            self.assert_refused(src, msgs[key])

    def test_intact_orphan_chain_is_advisory(self):
        src = self.vhd()
        self.full_dir(src)
        lsx_grow(src, "/MANY", "G02.TXT")
        a, b, c = self.free_cl(src, 3)
        self.orphan(src, [(a, b), (b, c), (c, 0xFFFF)])
        out = self.out()
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0, run_fsck.err)
        self.assertIn("どこからも使われていないクラスタ: 3 個 (鎖の頭: %d)" % a, run_fsck.out)

    @unittest.skipIf(os.name == "nt", "flock の試験は POSIX だけ")
    def test_read_paths_take_a_shared_lock(self):
        import fcntl
        src = self.vhd()
        self.full_dir(src)
        lsx_grow(src, "/MANY", "G02.TXT")
        # 書き手 (ldhdd) の排他のロックがある間は、check・fix --dry-run・fix とも止まる
        with open(src, "r+b") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            for argv in (["check", src], ["fix", src, "--out", self.out("l1.vhd"), "--dry-run"],
                         ["fix", src, "--out", self.out("l2.vhd")]):
                self.assertEqual(run_fsck(argv), 1, argv)
                self.assertIn("ほかのプロセスがこのファイルを使っています", run_fsck.err)
        self.assertFalse(os.path.exists(self.out("l2.vhd")))
        self.assertEqual(self.leftovers(), [])
        # 読み手どうし (共有のロック) なら一緒に使える
        with open(src, "rb") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            self.assertEqual(run_fsck(["check", src]), 2)
            self.assertEqual(run_fsck(["fix", src, "--out", self.out("l3.vhd")]), 0, run_fsck.err)


class TestVerifyAndChainWalks(FsckBase):
    def test_verify_failure_on_deep_image_memory(self):
        import tracemalloc
        src = self.vhd()
        self.full_dir(src, "A")
        lsx_grow(src, "/A", "G02.TXT")
        TestDotEntriesAndDeepTrees._deep_tree(self, bad_dotdot=False, src=src, measure=False)
        orig, orig_verify = ldfsck.apply_plan, ldfsck.verify

        def skip_entries(f, vol, plan):
            orig(f, vol, plan._replace(entry_writes=[]))

        def verify_without_slots(f, plan):  # 項目のバイトの確かめを外し、見える項目の比べで見付けさせる
            orig_verify(f, plan._replace(slots={}))
        ldfsck.apply_plan, ldfsck.verify = skip_entries, verify_without_slots
        tracemalloc.start()
        try:
            with self.assertRaisesRegex(ldhdd.LdhddError, "見える項目が計画と違います \\(/A\\)"):
                ldfsck.fix(src, self.out(), out=io.StringIO())
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
            ldfsck.apply_plan, ldfsck.verify = orig, orig_verify
        self.assertLess(peak, 30 * 1024 * 1024, "確かめの失敗の最大 %d バイト" % peak)
        self.assertEqual(self.leftovers(), [])

    def test_shared_chain_is_walked_once(self):
        src = self.vhd()
        big = os.path.join(self.dir, "BIG.BIN")
        with ldhdd._open_target(src, False) as f:
            cs = ldhdd.FatVolume(f).csize
        with open(big, "wb") as fh:
            fh.write(bytes(4096 * cs))
        ldhdd.add(src, [big], backup=False, out=io.StringIO())
        many = os.path.join(self.dir, "srcm", "M")
        os.makedirs(many)
        for i in range(600):
            with open(os.path.join(many, "F%03d.TXT" % i), "wb"):
                pass
        ldhdd.add(src, [many], backup=False, out=io.StringIO())
        with ldhdd._open_target(src, True) as f:  # M の 600 個の項目を、全部 BIG.BIN の鎖に向ける
            vol = ldhdd.FatVolume(f)
            head = vol.find(vol.read_dir(0), ldhdd.to_short_name("BIG.BIN")).cluster
            for e in vol.read_dir(vol.resolve_dir("/M")).entries:
                f.seek(e.off + 26)
                f.write(head.to_bytes(2, "little") + (4096 * cs).to_bytes(4, "little"))
        calls = [0]
        orig = ldhdd.FatVolume.get

        def counting(self_, c):
            calls[0] += 1
            return orig(self_, c)
        ldhdd.FatVolume.get = counting
        try:
            with ldhdd._open_target(src, False) as f:
                rep_ = ldfsck.scan(ldhdd.FatVolume(f))
        finally:
            ldhdd.FatVolume.get = orig
        self.assertEqual(len(rep_.problems) + rep_.more_problems, 600)
        self.assertIn("共有しています", rep_.problems[0])
        self.assertLess(calls[0], 300000, "FAT を %d 回読みました" % calls[0])

    def test_mirror_only_difference_is_advisory(self):
        # FAT の写しの食い違いだけ (X1 で書いた後のふつうの形): check は 0 で、そのことを伝える。fix は写しを作らない
        from test_ldhdd import x1_write
        src = self.vhd()
        x1_write(src, "X1.TXT", b"from x1")
        c = fat_copies(src)
        self.assertNotEqual(c[0], c[1])
        self.assertEqual(run_fsck(["check", src]), 0, run_fsck.out)
        self.assertIn("このままでも使えます", run_fsck.out)
        self.assertIn("問題はありません", run_fsck.out)
        out = self.out()
        self.assertEqual(run_fsck(["fix", src, "--out", out]), 0)
        self.assertIn("直す所はありません", run_fsck.out)
        self.assertFalse(os.path.exists(out))


class TestHiddenChainsAndScale(FsckBase):
    def hidden_on_one_chain(self, n_chain=4000):
        """64 個の隠れた項目が、どれも同じ長い鎖 (どこからも使われていない) を指す。サイズが合わないので移せない"""
        src = self.vhd(name="h%d.vhd" % n_chain)
        per = self.full_dir(src)
        with ldhdd._open_target(src, True) as f:
            vol = ldhdd.FatVolume(f)
            cs = vol.free_clusters()[-n_chain:]
            for a, b in zip(cs, cs[1:]):
                vol.set(a, b)
            vol.set(cs[-1], vol.eoc)
            write_fat1(f, vol)
        lsx_grow(src, "/MANY", None, entries=[entry("H%02d.TXT" % i, cs[0], 1) for i in range(per)])
        return src, per

    def test_rejected_hidden_entries_walk_a_chain_once(self):
        src, per = self.hidden_on_one_chain()
        calls = [0]
        orig = ldhdd.FatVolume.get

        def counting(self_, c):
            calls[0] += 1
            return orig(self_, c)
        ldhdd.FatVolume.get = counting
        try:
            with ldhdd._open_target(src, False) as f:
                rep_ = ldfsck.scan(ldhdd.FatVolume(f))
        finally:
            ldhdd.FatVolume.get = orig
        d = [x for x in rep_.dirs if x.path == "/MANY"][0]
        self.assertEqual(len(d.hidden_bad), per)
        self.assertLess(calls[0], 150000, "FAT を %d 回読みました" % calls[0])

    def test_rejected_hidden_reasons_are_capped_in_report(self):
        src, per = self.hidden_on_one_chain(n_chain=10)
        self.assertEqual(run_fsck(["check", src]), 2)
        self.assertEqual(run_fsck.out.count("移せません: "), ldfsck.LIMIT)
        self.assertEqual(run_fsck.out.count("移せません (理由の表示は省きます)"), per - ldfsck.LIMIT)
        self.assertIn("ほか %d 件は省きました" % (per - ldfsck.LIMIT), run_fsck.out)

    def test_dense_directories_memory(self):
        # 4 つのフォルダに 5 万個ずつの項目と、直せるフォルダ。fix 全体 (読み・計画・確かめ) のメモリが限られる
        import tracemalloc
        src = self.vhd()
        self.full_dir(src, "A")
        lsx_grow(src, "/A", "G02.TXT")
        n = 50000
        with ldhdd._open_target(src, True) as f:
            vol = ldhdd.FatVolume(f)
            per = vol.csize // 32
            free = vol.free_clusters()
            off = vol.root_off
            for k in range(4):
                ncl = -(-(n + 2) // per)
                cs = [free.pop(0) for _ in range(ncl)]
                for a, b in zip(cs, cs[1:]):
                    vol.set(a, b)
                vol.set(cs[-1], vol.eoc)
                buf = bytearray(ncl * vol.csize)
                buf[0:32] = entry(ldhdd.DOT_NAME, cs[0], 0, attr=0x10)
                buf[32:64] = entry(ldhdd.DOTDOT_NAME, 0, 0, attr=0x10)
                e = bytearray(entry("X", 0, 0))
                for i in range(n):
                    e[0:11] = ("N%07d" % i).encode() + b"   "
                    buf[(i + 2) * 32:(i + 3) * 32] = e
                for j, c in enumerate(cs):
                    f.seek(vol.cluster_off(c))
                    f.write(bytes(buf[j * vol.csize:(j + 1) * vol.csize]))
                while ldhdd._read_at(f, off, 1)[0] not in (0, 0xE5):
                    off += 32
                f.seek(off)
                f.write(entry("BIG%d" % k, cs[0], 0, attr=0x10))
            for k in range(vol.nfats):
                f.seek(vol.fat_off + k * vol.fat_bytes)
                f.write(bytes(vol.fat))
        tracemalloc.start()
        try:
            self.assertEqual(ldfsck.fix(src, self.out(), out=io.StringIO()), 0)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, DENSE_LIMIT, "fix の最大 %d バイト" % peak)
        self.assert_fixed_ok(src, self.out())

    @unittest.skipIf(os.name == "nt", "ハードリンクの試験は POSIX だけ")
    def test_publication_without_hard_links(self):
        import errno
        src = self.vhd()
        self.full_dir(src)
        lsx_grow(src, "/MANY", "G02.TXT")
        orig = os.link

        def no_link(a, b):
            raise OSError(errno.EPERM, "Operation not permitted")
        os.link = no_link
        try:
            self.assert_refused(src, "--out には、ふつうのフォルダ")
        finally:
            os.link = orig
        self.assertNotIn("--no-backup", run_fsck.err)


DENSE_LIMIT = 40 * 1024 * 1024


class TestNames(unittest.TestCase):
    def test_valid_name(self):
        ok = [b"G02     TXT", b"\x95\x5cABC   TXT", b"\x05\x40ABC   TXT", b"\xb1\xb2      TXT", b"A          ",
              b"\x88\x9f\x88\xa0    \x95\x5c "]
        ng = [b"\x81\xadABC   TXT", b" ABC    TXT", b"abc     TXT", b"ABCDEFG\x95TXT", b"\x95\x20ABC   TXT", b"A B     TXT", b"A*      TXT",
              b"A\x7f      TXT", b"A       \x95  ", b"A\x00      TXT"]
        for n in ok + ng:
            self.assertEqual(len(n), 11, n)
        for n in ok:
            self.assertTrue(ldfsck.valid_name(n), n)
        for n in ng:
            self.assertFalse(ldfsck.valid_name(n), n)


if __name__ == "__main__":
    unittest.main()
