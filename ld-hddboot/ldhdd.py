#!/usr/bin/env python3
"""ldhdd.py - LSX-Dodgers を X1 の HDD (SASI) から起動させるイメージを作る道具

標準ライブラリだけで動きます (Python 3.8 以上)。

  ldhdd.py update TARGET.vhd --ldsys ldsys.d88    既存の VHD の起動部を書き換える
  ldhdd.py sys -o LD.BIN --ldsys ldsys.d88        MAKESYS 相当の合体版 LD.BIN を作る
  ldhdd.py info TARGET.vhd                        VHD の起動部とパーティションを表示する

仕組み:
  X1 の IPL ROM は HDD の先頭 32 バイトを IPL 用 FCB として読み、先頭 0xF000 バイトを
  メモリ 0x0000 から読み込んで 0x0021 へ飛ぶ。0x0021 のローダーは 0x0200 に置いた
  合体版 LD.BIN (MSX バイナリ形式) を本来の番地へ移して実行する。合体版 LD.BIN は
  LSX-Dodgers 付属の MAKESYS と同じ形で「スタブ + HDD.COM + LD.BIN」をつないだ物で、
  起動時に HDD.COM が走って HDD がドライブに割り当てられる。
  起動部はパーティションより前の空き (MBR の後ろ) に書く。
"""

from __future__ import annotations

import argparse
import datetime
import errno
import hashlib
import os
import re
import shutil
import struct
import sys
import tempfile
from typing import Dict, List, NamedTuple, Optional, Tuple

__version__ = "1.0.0"


class LdhddError(Exception):
    """入力や対象の検査で止まるときのエラー (終了コード 1)"""


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

SECTOR = 512

# X1 の IPL ROM が読み込む範囲 (ldhdd_boot.asm の IPL_SIZE)
IPL_LOAD_SIZE = 0xF000

# 合体版 LD.BIN を置く位置 (ldhdd_boot.asm の LD_IMG_TOP)
SYS_OFFSET = 0x0200

# ldhdd_boot.asm を AILZ80ASM でアセンブルした先頭 0x43 バイト (IPL 用 FCB + ローダー)。
# 0x43〜0x1BD は AILZ80ASM が org の隙間を埋める 0xFF、0x1BE〜0x1FF は対象の MBR。
BOOT_LOADER = bytes.fromhex(
    "014c53582d446f64676572734844537973"  # 01 "LSX-DodgersHD" "Sys"
    "2000f000002100000000000000000000"  # 20 size=F000 top=0000 entry=0021 date rec
    "f3310000d92a0502d9ed5b01022a0302b7"  # di / ld sp,0 / exx / ld hl,(0205) / exx / ld de,(0201) / ld hl,(0203) / or a
    "ed524d4421070209eb09eb1b2bedb8d9e9"  # sbc hl,de / ld c,l / ld b,h / ld hl,0207 / ... / lddr / exx / jp (hl)
)
assert len(BOOT_LOADER) == 0x43
BOOT_FILL = 0xFF
MBR_TABLE_OFFSET = 0x1BE
MBR_KEEP_OFFSET = 0x1B8  # ここから後ろ (ディスク署名・予約・パーティション表・55AA) は対象の物を残す
BOOT_SIGNATURE = b"\x01LSX-DodgersHD"

# MAKESYS.COM (tablacus/LSX-Dodgers cmd/makesys, MIT) の IPL..IPL_E (135 バイト)。
# 先頭 7 バイトは MSX バイナリのヘッダ。値を埋める所は 0 または仮の値。
STUB_TEMPLATE = bytes.fromhex(
    "fe000000000000210000110001010000edb02a0bcb229202210000220bcbc30000cd0000"
    "21d4021151ff012300edb02a01007c326aff232252ff5e235613ed5355ff1151ff722b73"
    "d52102202280003e20215d00060b7710fd216d00060b7710fdc30001210000110000732372"
    "d51e000e0e7b1db7c40500116bffc3fdef4155544f4558454320"
)
STUB_LEN = 135
_JP_0100_POS = 97  # "JP $0100" (C3 00 01)
_TSS_POS = 100  # TSS のファイル位置
_DRIVE_POS = 111  # "LD E,<DRIVE>" の値
# MAKESYS の第 3 引数 (ドライブ) は LSX-Dodgers 1.61 以降の _PFILE の仕様変更で
# 0x20 (空白) が入るようになった (=カレントドライブは変わらない)。今の挙動のまま再現する。
STUB_DRIVE = 0x20
# スタブが起動時に TSS を写す先 (MAKESYS.ASM の TSD)。TSS_E-TSS = 0x23 バイト
STUB_TSD = 0xFF51

# HDD.COM (hdd.asm) が扱える論理セクタの大きさ (256/512/1024 はそのまま、2048 は変換して扱う)
SUPPORTED_BPS = (512, 1024, 2048)
# SASI のアドレスは 21 ビット x 256 バイト
SASI_LIMIT = (1 << 21) * 256

# 既知の COM (info で名前を出すため)
KNOWN_COMS = {
    "ebb2949baea45e3b6ee9b5344d41ba854529d5b2161fe4986d945528525ff5cb": "HDD.COM (LD-hdd 0.14)",
    "5124b98a63c06b2fb3296b75698427ab2d34c0c974c3427d1a797cd1f6db3c91": "HDD1.COM (旧来の HDD1 用)",
    "52ead99ae76116b1b13046d89154c6bf99930147d2f1d66a726307acc1b4a5ff": "HDD.COM (LD-hdd 0.13)",
}

# 旧手順 (このリポジトリの過去の MKLDHD.d88 / MKLDHD1.d88) で作られた合体版 LD.BIN の sha256。
# 印の無い前の起動部は、これと一致した時だけ長さを信じる。
KNOWN_PAYLOADS = {
    "e4b59618a9d85cd52f32448f4eccd168e4145c890926484e8672c6a2bf0d3cff",  # 1.61b + HDD.COM 0.14
    "aa37ebd54f3bbab82d03977a60bcd2526db2d40e0a1c6808113160d8cda83cf8",  # 1.61b + HDD1.COM
    "94f3bee4ea338ac1686c7c7202e2317e97b53715e7ec53944b8ae3c60128e3e6",  # 1.61b + HDD.COM 0.13
    "a290b1a73219f335a8aab96d0a782a20ee55492ac4a9de5e9f752f3550ee647d",  # 1.61b + HDD1.COM (0.13 系)
    "ff62984ddba781cda92cdfe1153157eb505ac90117e5a6bf88a6ad33e21eab6d",  # 1.61 + HDD.COM 0.13
    "b193c47ad73d1b56fd8e41779410c59af261148e2b0b4c9725206f0c90a4a9a9",  # 1.61 + HDD1.COM (0.13 系)
    "50770f7541c15f365533dc0a0b8f916fb30087694b31426992a8bdafb827175b",  # 1.55 + HDD.COM
    "4df12ba9fcd11523c71b7c092e776f4b594e8f0e4872b6487761529bb3f96e1e",  # 1.55 + HDD1.COM
}

# ldhdd.py が書いた起動部の印 (起動セクタの 0x190〜。ここは IPL で 0x0190 に読まれるが、
# スタブが最初に COM を 0x0100 から写すので、使われる前に上書きされる)
META_OFFSET = 0x190
META_MAGIC = b"LDHDDPY\x01"
META_DIGEST_LEN = 24
META_LEN = len(META_MAGIC) + 4 + META_DIGEST_LEN

VERSION_RE = re.compile(rb"LSX-Dodgers for [^\r\n\x00]{1,60}")


def _u16(b: bytes, off: int) -> int:
    return struct.unpack_from("<H", b, off)[0]


def _u32(b: bytes, off: int) -> int:
    return struct.unpack_from("<I", b, off)[0]


def _put16(b: bytearray, off: int, v: int) -> None:
    struct.pack_into("<H", b, off, v & 0xFFFF)


# ---------------------------------------------------------------------------
# D88 / FAT12 読み
# ---------------------------------------------------------------------------

def read_d88(data: bytes) -> Dict[Tuple[int, int, int], bytes]:
    """D88 イメージを {(C, H, R): データ} にする (最初のディスクのみ)"""
    if len(data) < 0x2B0:
        raise LdhddError("D88 として短すぎます")
    disk_size = _u32(data, 0x1C)
    if disk_size < 0x2B0 or disk_size > len(data):
        raise LdhddError("D88 のヘッダ (ディスクサイズ) が壊れています")
    # トラック表は 164 個 (0x2B0 まで) または 160 個 (0x2A0 まで) などがある。
    # 先頭から順に読み、いちばん小さいトラックの位置を表の終わりとして、それより前の項目だけを使う。
    offsets: List[int] = []
    table_end = 0x2B0
    for i in range(164):
        pos = 0x20 + 4 * i
        if pos + 4 > table_end:
            break
        off = _u32(data, pos)
        if off:
            if off < 0x20 + 4 * (i + 1) or off >= disk_size:
                raise LdhddError("D88 のトラック表が壊れています (項目 %d)" % i)
            table_end = min(table_end, off)
        offsets.append(off)
    if not any(offsets):
        raise LdhddError("D88 にトラックがありません")
    secs: Dict[Tuple[int, int, int], bytes] = {}
    for i, off in enumerate(offsets):
        if not off:
            continue
        p = off
        count = None
        n = 0
        while count is None or n < count:
            if p + 16 > disk_size:
                raise LdhddError("D88 のセクタが範囲外です (トラック %d)" % i)
            c, h, r, _n, nsec = struct.unpack_from("<BBBBH", data, p)
            size = _u16(data, p + 14)
            if count is None:
                count = nsec
                if count == 0 or count > 64:
                    raise LdhddError("D88 のセクタ数がおかしい (トラック %d)" % i)
            if p + 16 + size > disk_size:
                raise LdhddError("D88 のセクタデータが範囲外です (トラック %d)" % i)
            secs[(c, h, r)] = data[p + 16:p + 16 + size]
            p += 16 + size
            n += 1
    return secs


def logical_2d(secs: Dict[Tuple[int, int, int], bytes]) -> bytes:
    """2D (40 トラック x 2 面 x 9 セクタ x 512 バイト) を論理セクタ順に並べる。
    論理 N -> (C=N/18, H=(N/9)%2, R=N%9+1)。
    論理 0 が 256 バイト (X1 の IPL 起動ディスク) のときは 0 で埋める。"""
    out = bytearray()
    for n in range(720):
        key = (n // 18, (n // 9) % 2, n % 9 + 1)
        sec = secs.get(key)
        if sec is None:
            raise LdhddError("2D (512 バイト x 9 セクタ) の D88 ではありません (C=%d H=%d R=%d が無い)" % key)
        if len(sec) != SECTOR:
            if n == 0:
                sec = bytes(SECTOR)
            else:
                raise LdhddError("2D (512 バイト x 9 セクタ) の D88 ではありません (C=%d H=%d R=%d が %d バイト)"
                                 % (key + (len(sec),)))
        out += sec
    return bytes(out)


def _is_pow2(v: int) -> bool:
    return v > 0 and (v & (v - 1)) == 0


class FatGeometry(NamedTuple):
    reserved: int
    nfats: int
    sectors_per_fat: int
    root_entries: int
    sectors_per_cluster: int
    total_sectors: int


# X1 2D の DPB (LSX-Dodgers X1DPB.ASM の A:)
X1_2D_GEOMETRY = FatGeometry(reserved=1, nfats=2, sectors_per_fat=2, root_entries=112,
                             sectors_per_cluster=2, total_sectors=720)


def _floppy_geometry(img: bytes) -> FatGeometry:
    b = img[:SECTOR]
    if (b[0] == 0xEB and b[2] == 0x90) or b[0] == 0xE9:
        bps = _u16(b, 11)
        spc = b[13]
        res = _u16(b, 14)
        nf = b[16]
        root = _u16(b, 17)
        tot = _u16(b, 19)
        spf = _u16(b, 22)
        if bps == SECTOR and _is_pow2(spc) and res >= 1 and nf in (1, 2) and root > 0 and 0 < tot <= len(img) // SECTOR and spf > 0:
            return FatGeometry(res, nf, spf, root, spc, tot)
        raise LdhddError("D88 の BPB が想定外です (bps=%d spc=%d tot=%d)" % (bps, spc, tot))
    return X1_2D_GEOMETRY


def read_fat12_files(img: bytes) -> Dict[str, bytes]:
    """FAT12 のルートディレクトリにあるファイルを {"NAME.EXT": 中身} で返す"""
    g = _floppy_geometry(img)
    fat_off = g.reserved * SECTOR
    fat = img[fat_off:fat_off + g.sectors_per_fat * SECTOR]
    root_off = fat_off + g.nfats * g.sectors_per_fat * SECTOR
    root_secs = (g.root_entries * 32 + SECTOR - 1) // SECTOR
    data_off = root_off + root_secs * SECTOR
    csize = g.sectors_per_cluster * SECTOR
    max_cluster = (g.total_sectors * SECTOR - data_off) // csize + 1  # 有効なクラスタ番号の上限
    if data_off > len(img) or max_cluster < 2:
        raise LdhddError("FAT の配置が壊れています")

    def next_cluster(c: int) -> int:
        o = c * 3 // 2
        if o + 1 >= len(fat):
            raise LdhddError("FAT の範囲外のクラスタです (%d)" % c)
        v = _u16(fat, o)
        return v >> 4 if c & 1 else v & 0xFFF

    files: Dict[str, bytes] = {}
    for i in range(g.root_entries):
        e = img[root_off + i * 32:root_off + i * 32 + 32]
        if len(e) < 32 or e[0] == 0x00:
            break
        if e[0] == 0xE5 or e[11] & 0x18 or e[11] == 0x0F:
            continue
        base = e[0:8].decode("ascii", "replace").rstrip()
        ext = e[8:11].decode("ascii", "replace").rstrip()
        name = (base + "." + ext if ext else base).upper()
        start = _u16(e, 26)
        size = _u32(e, 28)
        buf = bytearray()
        seen = set()
        c = start
        while len(buf) < size:
            if c >= 0xFF8:
                raise LdhddError("%s: FAT の鎖がファイルサイズ (%d バイト) に届く前に終わっています" % (name, size))
            if c < 2 or c > max_cluster:
                raise LdhddError("%s: FAT の鎖が範囲外のクラスタを指しています (%d)" % (name, c))
            if c in seen:
                raise LdhddError("%s: FAT の鎖が循環しています" % name)
            seen.add(c)
            o = data_off + (c - 2) * csize
            buf += img[o:o + csize]
            c = next_cluster(c)
        files[name] = bytes(buf[:size])
    return files


def files_from_d88(path: str) -> Dict[str, bytes]:
    with open(path, "rb") as f:
        data = f.read()
    return read_fat12_files(logical_2d(read_d88(data)))


# ---------------------------------------------------------------------------
# LD.BIN と MAKESYS
# ---------------------------------------------------------------------------

class MsxBin(NamedTuple):
    top: int
    end: int  # 終わりの次の番地
    exec: int


def parse_msx_bin(data: bytes, what: str = "LD.BIN") -> MsxBin:
    if len(data) < 8 or data[0] != 0xFE:
        raise LdhddError("%s: MSX バイナリ形式 (先頭 FE) ではありません" % what)
    top, end, exe = struct.unpack_from("<HHH", data, 1)
    if not top < end:
        raise LdhddError("%s: ヘッダの開始 (%04X) と終了 (%04X) がおかしい" % (what, top, end))
    if len(data) != 7 + (end - top):
        raise LdhddError("%s: 長さ (%d) がヘッダ (7 + %04X - %04X = %d) と合いません"
                         % (what, len(data), end, top, 7 + end - top))
    if not top <= exe < end:
        raise LdhddError("%s: 実行番地 (%04X) が範囲 (%04X-%04X) の外です" % (what, exe, top, end))
    return MsxBin(top, end, exe)


def ld_version(data: bytes) -> str:
    m = VERSION_RE.search(data)
    return m.group(0).decode("ascii", "replace").strip() if m else "不明"


def build_stub(unit: int) -> Tuple[bytearray, int, int]:
    """(スタブ, TSS のファイル位置, DRIVE のファイル位置) を返す。
    unit 0 は MAKESYS と同じ。unit 1〜3 は JP $0100 の前に LD A,'n' / LD ($005D),A を足し、
    HDD.COM の第 1 引数 (FCB1 の名前部) に番号を渡す (HDD.COM は書き換えない)。"""
    if unit not in (0, 1, 2, 3):
        raise LdhddError("unit は 0〜3 です (%r)" % (unit,))
    stub = bytearray(STUB_TEMPLATE)
    tss = _TSS_POS
    drive = _DRIVE_POS
    if unit:
        ins = bytes([0x3E, 0x30 + unit, 0x32, 0x5D, 0x00])
        stub[_JP_0100_POS:_JP_0100_POS] = ins
        tss += len(ins)
        drive += len(ins)
    return stub, tss, drive


class SysLayout(NamedTuple):
    run: int
    end: int
    com_start: int
    com_size: int
    stub_len: int


def makesys(ld_bin: bytes, com: bytes, unit: int = 0) -> bytes:
    """MAKESYS com out と同じ合体版 LD.BIN を新しく作る"""
    ld = parse_msx_bin(ld_bin)
    # スタブは LD.BIN の初期化の CALL (exec+6) の飛び先 (exec+7, +8) を差し替える
    hook = 7 + (ld.exec + 6 - ld.top)
    if ld.exec + 9 > ld.end or ld_bin[hook] != 0xCD:
        raise LdhddError("LD.BIN の実行番地 %04X の 6 バイト先に CALL (CD) がありません。MAKESYS が対応している LD.BIN ではありません" % ld.exec)
    return assemble_sys(ld_bin, com, unit)


def assemble_sys(ld_bin: bytes, com: bytes, unit: int = 0, drive: int = STUB_DRIVE) -> bytes:
    """合体版 LD.BIN を組み立てる (新しく作る時は makesys を使う)。
    既にある起動部を見分ける時の作り直しにも使うので、LD.BIN の中身の検査はここではしない。
    drive はスタブの DRIVE 値 (既定 0x20 = 今の MAKESYS と同じ)"""
    ld = parse_msx_bin(ld_bin)
    if len(com) < 1:
        raise LdhddError("COM が空です")
    stub, tss_pos, drive_pos = build_stub(unit)
    s = len(stub)
    c = len(com)
    com_start = ld.top - 7 - c
    run = com_start - (s - 7)
    n = s + c + len(ld_bin)
    # IPL で読み込まれる側 (読み込み元)
    if SYS_OFFSET + n > IPL_LOAD_SIZE:
        raise LdhddError("合体版 LD.BIN が大きすぎます (0x200 + %d > 0x%X)" % (n, IPL_LOAD_SIZE))
    # ローダーの lddr: 写し元 [0x207, ...) 写し先 [RUN, ...)。後ろから写すので RUN >= 0x207 なら安全
    if run < SYS_OFFSET + 7:
        raise LdhddError("COM が大きすぎます (配置先 %04X がローダーの写し元 0207 より前)" % (run & 0xFFFF))
    # スタブの COM 複写 (ldir) の写し先 [0x0100, 0x0100+C) が実行中のスタブに重ならない
    if 0x0100 + c > run:
        raise LdhddError("COM が大きすぎます (0100+%04X がスタブの番地 %04X を越える)" % (c, run))
    if run + n - 7 != ld.end:
        raise LdhddError("合体版の終わりが LD.BIN の終わりと合いません")  # 起きないはず
    # スタブは起動時に TSD (FF51h〜) を作業場所に使うので、LD.BIN がそこにかからないこと
    if ld.end > STUB_TSD:
        raise LdhddError("LD.BIN の終わり (%04X) がスタブの作業場所 (%04X〜) にかかります" % (ld.end, STUB_TSD))

    t = stub
    _put16(t, 1, run)               # RUN
    _put16(t, 3, ld.end)            # LAST
    _put16(t, 5, run)               # EXEC
    _put16(t, 8, com_start)         # COM_START
    _put16(t, 14, c)                # COM_SIZE
    _put16(t, 19, ld.exec + 7)      # _INIT1
    _put16(t, 22, run + 27)         # _INIT_X1 (INITX+1)
    _put16(t, 25, run + 26)         # _INITX
    _put16(t, 28, ld.exec + 7)      # _INIT2
    _put16(t, 31, ld.exec)          # RUN_LD
    _put16(t, 37, run + tss_pos - 7)  # _TSS
    t[drive_pos] = drive & 0xFF
    return bytes(t) + bytes(com) + bytes(ld_bin)


class SysInfo(NamedTuple):
    length: int
    unit_override: Optional[int]  # スタブで指定した unit (無ければ None)
    com: bytes
    ld_bin: bytes


def parse_sys(sys_bin: bytes) -> SysInfo:
    """合体版 LD.BIN を分解する (info 用)"""
    hdr = parse_msx_bin(sys_bin, "起動部の LD.BIN")
    for unit in (0, 1, 2, 3):
        stub, tss_pos, drive_pos = build_stub(unit)
        s = len(stub)
        if len(sys_bin) < s + 8:
            continue
        cand = sys_bin[:s]
        fixed = [1, 2, 3, 4, 5, 6, 8, 9, 14, 15, 19, 20, 22, 23, 25, 26, 28, 29, 31, 32, 37, 38, drive_pos]
        if all(cand[i] == stub[i] for i in range(s) if i not in fixed):
            c = _u16(cand, 14)
            com = sys_bin[s:s + c]
            ld_bin = sys_bin[s + c:]
            try:
                rebuilt = assemble_sys(ld_bin, com, unit, drive=cand[drive_pos])
            except LdhddError:
                continue
            # 外側のヘッダ・スタブの番地・中の LD.BIN のヘッダがすべて噛み合っていること
            if rebuilt != sys_bin:
                continue
            return SysInfo(7 + hdr.end - hdr.top, unit if unit else None, com, ld_bin)
    raise LdhddError("起動部の LD.BIN が MAKESYS の形ではありません")


# ---------------------------------------------------------------------------
# 起動セクタ / 対象の検査
# ---------------------------------------------------------------------------

def boot_meta(payload: bytes) -> bytes:
    return META_MAGIC + struct.pack("<I", len(payload)) + hashlib.sha256(payload).digest()[:META_DIGEST_LEN]


def boot_record(mbr: bytes, payload: Optional[bytes] = None) -> bytes:
    """IPL 用 FCB + ローダー + 0xFF 埋め + 対象の MBR の 0x1B8〜0x1FF
    (ディスク署名・予約・パーティション表・55AA)。
    旧手順 (ldhdd_boot.asm) はディスク署名も 0xFF にしていたが、ローダーは 0x43 で終わり
    起動時にこの番地は使わないので、対象の値を残す。"""
    if len(mbr) < SECTOR:
        raise LdhddError("MBR が短すぎます")
    rec = bytearray([BOOT_FILL]) * SECTOR
    rec[0:len(BOOT_LOADER)] = BOOT_LOADER
    rec[MBR_KEEP_OFFSET:SECTOR] = mbr[MBR_KEEP_OFFSET:SECTOR]
    if payload is not None:
        rec[META_OFFSET:META_OFFSET + META_LEN] = boot_meta(payload)
    return bytes(rec)


def looks_like_bpb(sec: bytes) -> bool:
    """hdd.asm の check_bpb と同じ判定"""
    if len(sec) < 14:
        return False
    if sec[0] in (0xEB, 0x60):
        if sec[2] != 0x90:
            return False
    elif sec[0] != 0xE9:
        return False
    return _is_pow2(sec[12]) and _is_pow2(sec[13])


def check_fat_bpb(sec: bytes, part_sectors: int, read_part=None) -> None:
    """第 1 パーティションの BPB が LSX-Dodgers で使える FAT12/16 かを確かめる。
    read_part(off, n) はパーティション先頭からの読み出し (FAT の先頭を確かめるのに使う)"""
    if not looks_like_bpb(sec):
        raise LdhddError("第 1 パーティションの先頭に BPB がありません (FAT でフォーマットされていない?)")
    bps = _u16(sec, 11)
    spc = sec[13]
    res = _u16(sec, 14)
    nf = sec[16]
    root = _u16(sec, 17)
    tot = _u16(sec, 19) or _u32(sec, 32)
    spf = _u16(sec, 22)
    if bps not in SUPPORTED_BPS:
        raise LdhddError("第 1 パーティションの BPB: セクタサイズ %d に HDD.COM が対応していません (512/1024/2048 のみ)" % bps)
    if bps == 2048 and sec[0] != 0xEB:
        # hdd.asm の 2048 バイト向けの変換 (bpb2k) は先頭が EB の時だけ行われる
        raise LdhddError("第 1 パーティションの BPB: 2048 バイトのセクタは先頭が EB の物だけ使えます")
    if not (_is_pow2(spc) and spc <= 128):
        raise LdhddError("第 1 パーティションの BPB: クラスタのセクタ数 %d がおかしい" % spc)
    if spf == 0:
        raise LdhddError("第 1 パーティションが FAT12/16 ではありません (FAT32 などは使えません)")
    if res < 1 or nf not in (1, 2) or root == 0 or tot == 0:
        raise LdhddError("第 1 パーティションの BPB が壊れています (予約 %d, FAT 数 %d, ルート %d, 総セクタ %d)"
                         % (res, nf, root, tot))
    if tot * bps > part_sectors * SECTOR:
        raise LdhddError("第 1 パーティションの BPB の大きさ (%d バイト) がパーティション (%d バイト) を越えています"
                         % (tot * bps, part_sectors * SECTOR))
    # LSX-Dodgers の BPB2DPB と hdd.asm (2048 バイトのセクタは 1024 バイト単位に直して倍にする)
    # が入れる DPB の欄の幅に収まること
    k = 2 if bps == 2048 else 1
    if (root * 32) % bps:
        raise LdhddError("第 1 パーティションの BPB: ルートディレクトリ (%d 項目) がセクタの区切りに合いません" % root)
    dir_secs = root * 32 // bps
    if res * k > 255:
        raise LdhddError("第 1 パーティションの BPB: 予約セクタ数 %d が多すぎます (HDD.COM の DPB に入りません)" % res)
    if dir_secs * k > 255:
        raise LdhddError("第 1 パーティションの BPB: ルートディレクトリ (%d セクタ) が大きすぎます" % dir_secs)
    if spc * k > 128 or spc * bps > 65536:
        raise LdhddError("第 1 パーティションの BPB: クラスタ (%d セクタ) が大きすぎます" % spc)
    if (res + nf * spf) * k > 0xFFFF or tot >= (1 << 24):
        raise LdhddError("第 1 パーティションの BPB: FAT か総セクタ数が大きすぎます")
    data_start = res + nf * spf + dir_secs
    # BPB2DPB はデータ領域の位置 (DIRPS + DIRSCNT) を元のセクタ単位・16 ビットで計算する。
    # DPB_14_ADDCL16 = データ領域の位置 - 2 クラスタ は 16 ビットの欄で、2048 バイトのセクタでは倍にされる
    # (倍にされる DIRPS・DIRSCNT は上で確かめた)
    if not (data_start <= 0xFFFF and 0 <= (data_start - 2 * spc) * k <= 0xFFFF):
        raise LdhddError("第 1 パーティションの BPB: データ領域の位置 (%d セクタ) が HDD.COM の DPB に入りません" % data_start)
    clusters = (tot - data_start) // spc if tot > data_start else 0
    if not 1 <= clusters < 65525:
        raise LdhddError("第 1 パーティションの BPB: データ領域がおかしい (クラスタ数 %d)" % clusters)
    fat_bits = 12 if clusters < 4085 else 16
    if spf * bps < ((clusters + 2) * fat_bits + 7) // 8:
        raise LdhddError("第 1 パーティションの BPB: FAT (%d セクタ) がクラスタ数 %d に足りません" % (spf, clusters))
    media = sec[21]
    if not (media == 0xF0 or media >= 0xF8):
        raise LdhddError("第 1 パーティションの BPB: メディア記述子 %02X がおかしい" % media)
    if read_part is not None:
        for k in range(nf):
            head = read_part((res + k * spf) * bps, 4)
            if fat_bits == 12:
                ok = len(head) >= 3 and head[0] == media and head[1] == 0xFF and head[2] == 0xFF
            else:
                # 1 番目の項目の上位 2 ビットは Windows が状態の印に使うので見ない
                ok = (len(head) >= 4 and head[0] == media and head[1] == 0xFF
                      and head[2] == 0xFF and (head[3] & 0x3F) == 0x3F)
            if not ok:
                raise LdhddError("第 1 パーティションの FAT %d の先頭がおかしい (フォーマットされていない?)" % (k + 1))


class Partition(NamedTuple):
    index: int
    boot_flag: int
    ptype: int
    lba: int
    count: int


def read_partitions(mbr: bytes) -> List[Partition]:
    out = []
    for i in range(4):
        e = mbr[MBR_TABLE_OFFSET + 16 * i:MBR_TABLE_OFFSET + 16 * i + 16]
        out.append(Partition(i + 1, e[0], e[4], _u32(e, 8), _u32(e, 12)))
    return out


class VhdKind(NamedTuple):
    kind: str  # "fixed" / "raw"
    data_size: int  # フッターを除いた大きさ


VHD_TYPES = {2: "固定", 3: "可変", 4: "差分"}


def vhd_checksum(footer: bytes) -> int:
    """VHD フッターのチェックサム (チェックサム欄 64〜67 を除いた全バイトの和の 1 の補数)"""
    return ~(sum(footer[:64]) + sum(footer[68:SECTOR])) & 0xFFFFFFFF


def _read_at(f, off: int, n: int) -> bytes:
    """off から n バイト (ファイルの終わりまで) を読む。バッファなしのファイルでも全部読む"""
    f.seek(off)
    out = bytearray()
    while len(out) < n:
        b = f.read(n - len(out))
        if not b:
            break
        out += b
    return bytes(out)


def _write_all(f, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = f.write(view)
        if not n:
            raise OSError("書き込めませんでした")
        view = view[n:]


def vhd_kind_of(f) -> VhdKind:
    size = os.fstat(f.fileno()).st_size
    if _read_at(f, 0, 8) == b"conectix":
        raise LdhddError("可変 (dynamic) または差分 VHD です。固定サイズの VHD だけに対応しています")
    foot = _read_at(f, size - SECTOR, SECTOR) if size >= SECTOR else b""
    if foot[:8] == b"conectix":
        dtype = struct.unpack_from(">I", foot, 60)[0]
        if dtype != 2:
            raise LdhddError("%s VHD です。固定サイズの VHD だけに対応しています"
                             % VHD_TYPES.get(dtype, "種類 %d の" % dtype))
        if vhd_checksum(foot) != struct.unpack_from(">I", foot, 64)[0]:
            raise LdhddError("VHD のフッターのチェックサムが合いません")
        current = struct.unpack_from(">Q", foot, 48)[0]
        if current != size - SECTOR:
            raise LdhddError("VHD のフッターの大きさ (%d バイト) が中身 (%d バイト) と合いません" % (current, size - SECTOR))
        return VhdKind("fixed", size - SECTOR)
    return VhdKind("raw", size)


class ExistingBoot(NamedTuple):
    present: bool
    old_len: int  # 確かめられた前の起動部 (合体版 LD.BIN) の長さ。確かめられなければ 0


def existing_boot(head: bytes) -> ExistingBoot:
    """前の起動部を探す。MAKESYS の形として全体を確かめられた時だけ長さを返す"""
    if head[:len(BOOT_SIGNATURE)] != BOOT_SIGNATURE:
        return ExistingBoot(False, 0)
    hdr = head[SYS_OFFSET:SYS_OFFSET + 7]
    if len(hdr) == 7 and hdr[0] == 0xFE:
        top, end = struct.unpack_from("<HH", hdr, 1)
        old = 7 + (end - top)
        if top < end and SYS_OFFSET + old <= min(IPL_LOAD_SIZE, len(head)):
            payload = head[SYS_OFFSET:SYS_OFFSET + old]
            # ヘッダの長さだけでは、後ろの無関係なバイトまで含めて名乗れてしまう。
            # ldhdd.py が書いた印か、旧手順の既知の物と一致した時だけ長さを信じる。
            trusted = (head[META_OFFSET:META_OFFSET + META_LEN] == boot_meta(payload)
                       or hashlib.sha256(payload).hexdigest() in KNOWN_PAYLOADS)
            if trusted:
                try:
                    parse_sys(payload)
                except LdhddError:
                    return ExistingBoot(True, 0)
                return ExistingBoot(True, old)
    return ExistingBoot(True, 0)


class TargetCheck(NamedTuple):
    vhd: VhdKind
    partitions: List[Partition]
    new_end: int  # 新しい起動部の終わり
    write_end: int  # 書く範囲の終わり (前の起動部の方が長ければ、その残りを 0 で消す所まで)
    existing: ExistingBoot
    nonzero_gap: Optional[int]  # 隙間で 0 でなかった最初の位置


def check_target(f, write_len: int) -> TargetCheck:
    """開いたファイル f に書き込んでよいかを調べる。書いてはいけないなら LdhddError。
    隙間に 0 でない所があるかは nonzero_gap で返す (--force の判断は呼び出し側)。"""
    vk = vhd_kind_of(f)
    if vk.data_size < IPL_LOAD_SIZE:
        raise LdhddError("イメージが小さすぎます (%d バイト。IPL が読む 0x%X バイト以上が必要)"
                         % (vk.data_size, IPL_LOAD_SIZE))
    new_end = SYS_OFFSET + write_len
    if new_end > IPL_LOAD_SIZE:
        raise LdhddError("起動部が大きすぎます")
    head = _read_at(f, 0, IPL_LOAD_SIZE)
    if len(head) < IPL_LOAD_SIZE or head[510:512] != b"\x55\xaa":
        raise LdhddError("先頭に MBR (55AA) がありません。MBR で区切った VHD が必要です")
    if looks_like_bpb(head[:256]):
        raise LdhddError("先頭が BPB です (MBR の無い FAT)。書くと壊れるので止めます。MBR で区切った VHD が必要です")
    ex = existing_boot(head)
    old_end = SYS_OFFSET + ex.old_len if ex.old_len else 0
    write_end = max(new_end, old_end)
    parts = read_partitions(head)
    used = [p for p in parts if p.ptype != 0]
    if not used:
        raise LdhddError("パーティションがありません")
    for p in used:
        start = p.lba * SECTOR
        stop = (p.lba + p.count) * SECTOR
        if start < write_end:
            raise LdhddError("パーティション %d (LBA %d から) が起動部を書く範囲 (先頭 %d バイト) に重なります"
                             % (p.index, p.lba, write_end))
        if stop > vk.data_size:
            raise LdhddError("パーティション %d がファイルの外にはみ出しています" % p.index)
    p1 = parts[0]
    if p1.ptype == 0:
        raise LdhddError("第 1 パーティションがありません (HDD.COM は第 1 パーティションを使います)")
    if p1.lba >= 32768:
        raise LdhddError("第 1 パーティションの開始 (LBA %d) が遠すぎます (HDD.COM は 32768 未満まで)" % p1.lba)
    if (p1.lba + p1.count) * SECTOR > SASI_LIMIT:
        raise LdhddError("第 1 パーティションが 512MB を越えています (SASI で扱えるのは 512MB まで)")
    base = p1.lba * SECTOR
    check_fat_bpb(_read_at(f, base, SECTOR), p1.count, lambda off, n: _read_at(f, base + off, n))
    keep_from = old_end if ex.old_len else SYS_OFFSET
    nonzero = None
    for i in range(keep_from, new_end):
        if head[i] != 0:
            nonzero = i
            break
    return TargetCheck(vk, parts, new_end, write_end, ex, nonzero)


# ---------------------------------------------------------------------------
# 入力の解決
# ---------------------------------------------------------------------------

class Inputs(NamedTuple):
    ld_bin: bytes
    ld_from: str
    com: bytes
    com_from: str


def load_inputs(ldsys: Optional[str], ld_bin: Optional[str], hdd_com: Optional[str]) -> Inputs:
    disk: Optional[Dict[str, bytes]] = None
    if ldsys:
        disk = files_from_d88(ldsys)
    if ld_bin:
        with open(ld_bin, "rb") as f:
            ld = f.read()
        ld_from = ld_bin
    elif disk is not None:
        if "LD.BIN" not in disk:
            raise LdhddError("%s に LD.BIN がありません" % ldsys)
        ld = disk["LD.BIN"]
        ld_from = "%s の LD.BIN" % ldsys
    else:
        raise LdhddError("LSX-Dodgers の LD.BIN がありません。--ldsys ldsys.d88 か --ld-bin LD.BIN を指定してください")
    if hdd_com:
        with open(hdd_com, "rb") as f:
            com = f.read()
        com_from = hdd_com
    elif disk is not None:
        if "HDD.COM" not in disk:
            raise LdhddError("%s に HDD.COM がありません。--hdd-com で指定してください" % ldsys)
        com = disk["HDD.COM"]
        com_from = "%s の HDD.COM" % ldsys
    else:
        raise LdhddError("HDD.COM がありません。--ldsys ldsys.d88 か --hdd-com HDD.COM を指定してください")
    parse_msx_bin(ld)
    return Inputs(ld, ld_from, com, com_from)


def describe_com(com: bytes) -> str:
    h = hashlib.sha256(com).hexdigest()
    name = KNOWN_COMS.get(h)
    return "%d バイト sha256=%s%s" % (len(com), h[:16], (" " + name) if name else "")


# ---------------------------------------------------------------------------
# コマンド
# ---------------------------------------------------------------------------

def _backup_path(path: str) -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = "%s.%s.bak" % (path, stamp)
    cand = base
    i = 1
    while os.path.exists(cand):
        cand = "%s.%d" % (base, i)
        i += 1
    return cand


def make_backup(f, path: str) -> str:
    """開いているファイル f の中身を、path の隣に新しい名前でコピーする。
    仮の名前に書いて同期し、既にある物を置き換えない方法で正式な名前を付け、
    その名前もディスクに書き切ってから返す。途中で失敗したらイメージには何も書かない。"""
    dst = _backup_path(path)
    f.seek(0)
    if os.name == "nt":
        return _make_backup_windows(f, path, dst)
    return _make_backup_posix(f, path, dst)


_BACKUP_FAIL = "バックアップを作れませんでした (%s)。何も書き込んでいません"
_NO_LINK = ("このファイルシステムでは、既にある物を置き換えずにバックアップを置けません"
            " (自分でコピーを取ってから --no-backup を付けてください)")


def _copy_to_fd(f, fd: int) -> None:
    with os.fdopen(fd, "wb") as out:
        shutil.copyfileobj(f, out, 1024 * 1024)
        out.flush()
        os.fsync(out.fileno())


def _make_backup_posix(f, path: str, dst: str) -> str:
    """親ディレクトリを 1 回だけ開き、仮ファイルの作成・名前付け・後始末・同期をすべてその記述子で行う
    (途中で親のパスが別の場所を指すようになっても、同じディレクトリで済ませる)"""
    dirpath = os.path.dirname(os.path.abspath(dst))
    name = os.path.basename(dst)
    dfd = os.open(dirpath, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    staging = None
    leftover = None
    try:
        try:
            for _ in range(100):
                cand = "%s.%d.%s.part" % (name, os.getpid(), os.urandom(4).hex())
                try:
                    fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dfd)
                except FileExistsError:
                    continue
                staging = cand
                break
            else:
                raise OSError("仮ファイルの名前を決められません")
            _copy_to_fd(f, fd)
            try:
                os.link(staging, name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
            except OSError as e:
                if e.errno in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV, errno.EMLINK):
                    raise OSError(e.errno, _NO_LINK)
                raise
        except OSError as e:
            if staging is not None:
                try:
                    os.unlink(staging, dir_fd=dfd)
                except OSError:
                    pass
            raise LdhddError(_BACKUP_FAIL % e)
        try:
            os.unlink(staging, dir_fd=dfd)
        except OSError:
            leftover = os.path.join(dirpath, staging)
        try:
            # 名前 (ディレクトリの項目) をディスクに書き切ってから、イメージに書き始める
            os.fsync(dfd)
            # 表示する戻し先のパスが、今作ったバックアップを指していること
            published = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if not os.path.samestat(published, os.stat(dst)):
                raise OSError("バックアップのパス %s が別の物を指しています" % dst)
        except OSError as e:
            raise LdhddError("バックアップ %s の名前をディスクに書き切れませんでした (%s)。イメージには何も書き込んでいません"
                             " (バックアップはできているかもしれません)" % (dst, e))
    finally:
        os.close(dfd)
    if leftover:
        # 正式な名前のバックアップはできている。仮の名前が消せなかっただけ
        print("注意: 仮ファイル %s を消せませんでした (バックアップ %s はできています)" % (leftover, dst), file=sys.stderr)
    return dst


MOVEFILE_WRITE_THROUGH = 0x8


def _windows_move_no_replace(src: str, dst: str, kernel32=None) -> None:
    """MoveFileExW を MOVEFILE_REPLACE_EXISTING 無しで呼ぶ (既にある名前には失敗する)。
    MOVEFILE_WRITE_THROUGH で、名前の変更がディスクに書き切られてから戻る。"""
    import ctypes
    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.MoveFileExW(src, dst, MOVEFILE_WRITE_THROUGH):
        err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
        raise OSError(err, "MoveFileExW が失敗しました (%d)" % err)


def _make_backup_windows(f, path: str, dst: str, kernel32=None) -> str:
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".part",
                                   dir=os.path.dirname(os.path.abspath(dst)))
        _copy_to_fd(f, fd)
        _windows_move_no_replace(tmp, dst, kernel32)
        tmp = None
    except OSError as e:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise LdhddError(_BACKUP_FAIL % e)
    return dst


def _lock_windows(f, msvcrt_mod):
    """msvcrt.locking は記述子の今の位置から数えるので、Python のバッファではなく記述子そのものを 0 に置く。
    解除に失敗しても、閉じれば外れるので、書き込み済みの更新を失敗扱いにしない。"""
    fd = f.fileno()
    f.flush()
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt_mod.locking(fd, msvcrt_mod.LK_NBLCK, IPL_LOAD_SIZE)

    def unlock() -> None:
        try:
            f.flush()
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt_mod.locking(fd, msvcrt_mod.LK_UNLCK, IPL_LOAD_SIZE)
        except OSError:
            pass
    return unlock


def _lock(f, msvcrt_mod=None):
    """協力する書き手どうしの同時書き込みを避ける。取れなければ止める。
    POSIX は flock (ファイル全体)、Windows は先頭 0xF000 バイトの範囲ロック。
    どちらもファイルを閉じると外れる。"""
    fcntl = None
    if msvcrt_mod is None:
        try:
            import fcntl
        except ImportError:
            import msvcrt as msvcrt_mod
    try:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lambda: fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return _lock_windows(f, msvcrt_mod)
    except OSError:
        raise LdhddError("ほかのプロセスがこのファイルを使っています")


def _open_target(target: str, write: bool):
    if not os.path.isfile(target):
        raise LdhddError("ファイルがありません: %s" % target)
    # バッファなしで開く。閉じる時に、残ったバッファを (ロックを外した後に) 書き直すことが無いように
    return open(target, "r+b" if write else "rb", buffering=0)


def update(target: str, inputs: Inputs, unit: int = 0, backup: bool = True,
           force: bool = False, dry_run: bool = False, out=None) -> Optional[str]:
    """検査・バックアップ・書き込み・読み戻しを、同じ 1 つの開いたファイルで行う"""
    if out is None:
        out = sys.stdout
    sys_bin = makesys(inputs.ld_bin, inputs.com, unit)
    with _open_target(target, not dry_run) as f:
        unlock = _lock(f) if not dry_run else None
        try:
            return _update_locked(f, target, inputs, sys_bin, unit, backup, force, dry_run, out)
        finally:
            if unlock is not None:
                unlock()


def _update_locked(f, target, inputs, sys_bin, unit, backup, force, dry_run, out) -> Optional[str]:
    chk = check_target(f, len(sys_bin))
    p1 = chk.partitions[0]
    print("対象      : %s (%s)" % (target, "固定 VHD" if chk.vhd.kind == "fixed" else "フッター無しの生イメージ"), file=out)
    print("LSX-Dodgers: %s (%s)" % (ld_version(inputs.ld_bin), inputs.ld_from), file=out)
    print("COM       : %s (%s)" % (describe_com(inputs.com), inputs.com_from), file=out)
    print("unit      : %s" % ("スタブで %d を指定" % unit if unit else "指定なし (COM 次第。HDD.COM なら 0)"), file=out)
    print("書く範囲  : 先頭 %d バイト (第 1 パーティションは LBA %d = %d バイト目から)"
          % (chk.write_end, p1.lba, p1.lba * SECTOR), file=out)
    if not chk.existing.present:
        print("前の起動部: なし", file=out)
    elif chk.existing.old_len:
        print("前の起動部: あり (%d バイト)%s" % (chk.existing.old_len,
              " 。新しい方が短いので残りを 0 で消します" if chk.write_end > chk.new_end else ""), file=out)
    else:
        print("前の起動部: IPL はあるが中身を確かめられません (前の起動部として扱いません)", file=out)
    if chk.nonzero_gap is not None:
        msg = "起動部を書く範囲の %d バイト目に 0 でないデータがあります" % chk.nonzero_gap
        if not force:
            raise LdhddError(msg + "。上書きしてよい場合だけ --force を付けてください")
        print("注意      : " + msg + " (--force で上書きします)", file=out)
    if dry_run:
        print("--dry-run なので書き込みません", file=out)
        return None
    mbr = _read_at(f, 0, SECTOR)
    area = boot_record(mbr, sys_bin) + sys_bin
    area += bytes(chk.write_end - len(area))
    bak = None
    if backup:
        bak = make_backup(f, target)
        print("バックアップ: %s" % bak, file=out)
    try:
        f.seek(0)
        _write_all(f, area)
        os.fsync(f.fileno())
        if _read_at(f, 0, len(area)) != area:
            raise LdhddError("書いた内容を読み戻したら一致しませんでした")
    except (OSError, LdhddError) as e:
        hint = ("バックアップ %s を %s にコピーして戻してください" % (bak, target)) if bak else "バックアップはありません"
        raise LdhddError("書き込みに失敗しました: %s。%s" % (e, hint))
    print("完了しました", file=out)
    return bak


def info(target: str, out=None) -> None:
    if out is None:
        out = sys.stdout
    with _open_target(target, False) as f:
        vk = vhd_kind_of(f)
        head = _read_at(f, 0, IPL_LOAD_SIZE)
    print("対象      : %s (%s, データ %d バイト)" % (target, "固定 VHD" if vk.kind == "fixed" else "フッター無しの生イメージ", vk.data_size), file=out)
    if len(head) < SECTOR or head[510:512] != b"\x55\xaa":
        print("MBR       : なし", file=out)
    else:
        for p in read_partitions(head):
            if p.ptype:
                print("パーティション %d: 種類 %02X 開始 LBA %d (%d バイト目) %d セクタ" % (p.index, p.ptype, p.lba, p.lba * SECTOR, p.count), file=out)
    ex = existing_boot(head)
    if not ex.present:
        print("起動部    : なし", file=out)
        return
    if not ex.old_len:
        print("起動部    : IPL はあるが LD.BIN を確かめられません", file=out)
        return
    si = parse_sys(head[SYS_OFFSET:SYS_OFFSET + ex.old_len])
    print("起動部    : あり (LD.BIN %d バイト)" % ex.old_len, file=out)
    print("LSX-Dodgers: %s" % ld_version(si.ld_bin), file=out)
    print("COM       : %s" % describe_com(si.com), file=out)
    if si.unit_override is not None:
        print("unit      : スタブで %d を指定" % si.unit_override, file=out)
    else:
        print("unit      : スタブでの指定なし (unit は COM 次第)", file=out)


def write_new_file(path: str, data: bytes, force: bool) -> None:
    mode = "wb" if force else "xb"
    try:
        with open(path, mode) as f:
            f.write(data)
    except FileExistsError:
        raise LdhddError("%s は既にあります。上書きするなら --force を付けてください" % path)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ldhdd.py",
        description="LSX-Dodgers を X1 の HDD から起動させるイメージを作る (version %s)" % __version__)
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True

    def add_inputs(p: argparse.ArgumentParser) -> None:
        p.add_argument("--ldsys", metavar="D88", help="LSX-Dodgers のシステムディスク (ldsys.d88)。LD.BIN と HDD.COM をここから取る")
        p.add_argument("--ld-bin", metavar="FILE", help="LSX-Dodgers の LD.BIN (--ldsys の物より優先)")
        p.add_argument("--hdd-com", metavar="FILE", help="起動時に走らせる COM (既定は --ldsys の HDD.COM)")
        p.add_argument("--unit", type=int, default=0, choices=(0, 1, 2, 3),
                       help="起動時に HDD.COM へ渡す HDD 番号 (既定 0。1〜3 は試験中)")

    p = sub.add_parser("update", help="既存の VHD の起動部 (LSX-Dodgers と HDD.COM) を書き換える")
    p.add_argument("target", help="書き換える VHD (固定サイズ、MBR で区切った物)")
    add_inputs(p)
    p.add_argument("--no-backup", action="store_true", help="バックアップを取らない")
    p.add_argument("--force", action="store_true", help="起動部を書く範囲に 0 でないデータがあっても書く")
    p.add_argument("--dry-run", action="store_true", help="検査と表示だけして書かない")

    p = sub.add_parser("sys", help="MAKESYS 相当の合体版 LD.BIN をファイルに出す")
    p.add_argument("-o", "--output", required=True, help="出力ファイル")
    add_inputs(p)
    p.add_argument("--force", action="store_true", help="出力ファイルがあれば上書きする")

    p = sub.add_parser("info", help="VHD の起動部とパーティションを表示する")
    p.add_argument("target")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "update":
            inputs = load_inputs(args.ldsys, args.ld_bin, args.hdd_com)
            update(args.target, inputs, unit=args.unit, backup=not args.no_backup,
                   force=args.force, dry_run=args.dry_run)
        elif args.cmd == "sys":
            inputs = load_inputs(args.ldsys, args.ld_bin, args.hdd_com)
            data = makesys(inputs.ld_bin, inputs.com, args.unit)
            write_new_file(args.output, data, args.force)
            print("%s を作りました (%d バイト, %s + %s)" % (args.output, len(data), ld_version(inputs.ld_bin), describe_com(inputs.com)))
        elif args.cmd == "info":
            info(args.target)
    except (LdhddError, OSError) as e:
        print("エラー: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
