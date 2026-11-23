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
import io
import os
import re
import shutil
import struct
import sys
import time
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
    仮ファイルに書いて同期し、既にある物を置き換えない方法で正式な名前を付け、
    名前もディスクに書いてから返す。途中で失敗したらイメージには何も書かない。
    (守るのは、ふつうに使っていて壊さないことまで。別のプロセスが同時にパスやディレクトリを
    書き換える場合は対象外)"""
    dst = _backup_path(path)
    d = _backup_dir(dst)
    tmp = None
    leftover = None
    f.seek(0)
    try:
        fd, tmp = _create_staging(d)
        try:
            _copy_to_fd(f, fd)
        finally:
            os.close(fd)
        leftover = _publish_no_replace(tmp, dst)
        tmp = None
    except BaseException as e:  # Ctrl-C などでも仮ファイルを片付ける
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if isinstance(e, OSError):
            raise LdhddError(_BACKUP_FAIL % e)
        raise
    try:
        # 名前 (ディレクトリの項目) もディスクに書いてから、イメージに書き始める
        _fsync_dir(d)
    except OSError as e:
        raise LdhddError(_BACKUP_UNSURE % (dst, e))
    if leftover:
        # 正式な名前のバックアップはできている。仮の名前が消せなかっただけ
        print("注意: 仮ファイル %s を消せませんでした (バックアップ %s はできています)" % (leftover, dst), file=sys.stderr)
    return dst


_BACKUP_FAIL = "バックアップを作れませんでした (%s)。何も書き込んでいません"
_NO_LINK = ("このファイルシステムでは、既にある物を置き換えずにバックアップを置けません。"
            "自分でコピーを取ってから --no-backup を付けて実行してください")
_BACKUP_UNSURE = ("バックアップ %s の名前をディスクに書き切れませんでした (%s)。イメージには何も書き込んでいません"
                  " (バックアップはできているかもしれません)")


def _backup_dir(dst: str) -> str:
    # abspath は「..」を文字の上だけでたたむので使わない。対象のパスと同じたどり方をさせる
    return os.path.dirname(dst) or os.curdir


_IS_WINDOWS = os.name == "nt"


def _create_staging(d: str) -> Tuple[int, str]:
    """d に仮ファイルを作る。tempfile.mkstemp は d を abspath でたたむので使わない。
    名前 (17 文字) は正式な名前 (元の名前 + 20 文字以上) より必ず短くし、パスの長さの上限に先にかからないようにする"""
    for _ in range(100):
        tmp = os.path.join(d, ".ldhdd%s.part" % os.urandom(3).hex())
        try:
            return os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600), tmp
        except FileExistsError:
            continue
    raise OSError(errno.EEXIST, "仮ファイルの名前を決められません")


def _copy_to_fd(f, fd: int) -> None:
    """f の中身を fd に写して同期する。fd は閉じない"""
    with os.fdopen(fd, "wb", closefd=False) as out:
        shutil.copyfileobj(f, out, 1024 * 1024)
        out.flush()
    os.fsync(fd)


def _fsync_dir(path: str) -> None:
    """ディレクトリの項目をディスクに書く (POSIX のみ。Windows は名前付けを書き切りで行うので何もしない)"""
    if _IS_WINDOWS:
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_no_replace(tmp: str, dst: str) -> Optional[str]:
    """tmp を dst という名前にする。dst が既にあれば失敗する (置き換えない)。
    正式な名前を付けた後に仮の名前を消せなかった時は、その仮の名前を返す (失敗にはしない)。"""
    if _IS_WINDOWS:
        _windows_move_no_replace(tmp, dst)
        return None
    try:
        os.link(tmp, dst)  # 既にある名前には失敗する。途中で止まっても、正式な名前は全部できているか無いか
    except OSError as e:
        if e.errno in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EMLINK):
            # ハードリンクが使えないファイルシステム (Linux や macOS から見た FAT など)。
            # 置き換えずに一度で名前を付ける方法が無いので、正式な名前を作る前に止める
            raise OSError(e.errno, _NO_LINK)
        raise
    try:
        os.remove(tmp)
    except OSError:
        return tmp
    return None


MOVEFILE_WRITE_THROUGH = 0x8


MOVEFILE_REPLACE_EXISTING = 0x1


def _windows_move(src: str, dst: str, flags: int, kernel32=None) -> None:
    """MoveFileExW を呼ぶ。MOVEFILE_WRITE_THROUGH で、名前の変更がディスクに書き切られてから戻る"""
    import ctypes
    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.MoveFileExW(src, dst, flags):
        err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
        raise OSError(err, "MoveFileExW が失敗しました (%d)" % err)


def _windows_move_no_replace(src: str, dst: str, kernel32=None) -> None:
    """MOVEFILE_REPLACE_EXISTING 無し (既にある名前には失敗する) + 書き切り"""
    _windows_move(src, dst, MOVEFILE_WRITE_THROUGH, kernel32)


def _replace_durable(src: str, dst: str) -> None:
    """src で dst を置き換える。Windows は MoveFileExW の書き切り、POSIX は os.replace (後でディレクトリを fsync)"""
    if _IS_WINDOWS:
        _windows_move(src, dst, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)
    else:
        os.replace(src, dst)


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


def describe_fat(pb: bytes, p1: "Partition", vk: VhdKind) -> str:
    """第 1 パーティションの BPB を info 用に説明する (壊れていても例外にしない)"""
    if len(pb) < 62:
        return "BPB を読めません (パーティションがファイルの外)"
    if not looks_like_bpb(pb):
        return "BPB がありません"
    bps, spc, res, nf, root = _u16(pb, 11), pb[13], _u16(pb, 14), pb[16], _u16(pb, 17)
    spf = _u16(pb, 22)
    tot = _u16(pb, 19) or _u32(pb, 32)
    if spf == 0:
        return "FAT32 など (HDD.COM では使えません)"
    if not bps or not spc:
        return "BPB が壊れています"
    if (p1.lba + p1.count) * SECTOR > vk.data_size:
        return "パーティションがファイルの外にはみ出しています"
    if tot * bps > p1.count * SECTOR:
        return "BPB の大きさ (%d バイト) がパーティション (%d バイト) を越えています" % (tot * bps, p1.count * SECTOR)
    ds = res + nf * spf + (root * 32 + bps - 1) // bps
    cl = (tot - ds) // spc if tot > ds else 0
    # ラベル欄は拡張ブート署名 (29h) がある時だけ。表示できない文字は ? にする
    label = "".join(chr(c) if 0x20 <= c < 0x7F else "?" for c in pb[43:54]).rstrip() if pb[38] == 0x29 else "(なし)"
    return ("FAT%d、クラスタ %d バイト x %d (データ %.1fMB)、ラベル %s"
            % (12 if cl < 4085 else 16, bps * spc, cl, cl * bps * spc / 1048576.0, label))


def info(target: str, out=None) -> None:
    if out is None:
        out = sys.stdout
    with _open_target(target, False) as f:
        vk = vhd_kind_of(f)
        head = _read_at(f, 0, IPL_LOAD_SIZE)
        p1 = read_partitions(head)[0] if len(head) >= SECTOR else None
        pb1 = (_read_at(f, p1.lba * SECTOR, SECTOR)
               if p1 is not None and p1.ptype and (p1.lba + 1) * SECTOR <= vk.data_size else b"")
    print("対象      : %s (%s, データ %d バイト)" % (target, "固定 VHD" if vk.kind == "fixed" else "フッター無しの生イメージ", vk.data_size), file=out)
    if len(head) < SECTOR or head[510:512] != b"\x55\xaa":
        print("MBR       : なし", file=out)
    else:
        for p in read_partitions(head):
            if p.ptype:
                print("パーティション %d: 種類 %02X 開始 LBA %d (%d バイト目) %d セクタ" % (p.index, p.ptype, p.lba, p.lba * SECTOR, p.count), file=out)
        p1 = read_partitions(head)[0]
        if p1.ptype:
            print("FAT       : %s" % describe_fat(pb1, p1, vk), file=out)
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


# ---------------------------------------------------------------------------
# new: 容量を指定して、起動できる固定 VHD を新しく作る
# ---------------------------------------------------------------------------

NEW_PART_LBA = 128  # 64KB。IPL が読む 0xF000 と起動部の後ろ、HDD.COM の 32768 未満
NEW_MIN_SIZE = 1024 * 1024
NEW_MAX_SIZE = 512 * 1024 * 1024
NEW_RESERVED = 1
NEW_NFATS = 2
NEW_ROOT = 512
NEW_MEDIA = 0xF8
NO_LABEL = b"NO NAME    "
LABEL_CHARS = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 !#$%&'()-@^_`{}~")
VHD_EPOCH = 946684800  # 2000-01-01 00:00:00 UTC


def parse_size(text: str) -> int:
    """「128M」「16448K」「128」(単位なしは MB) をバイト数にする。1MB〜512MB"""
    m = re.fullmatch(r"\s*(\d{1,12})\s*([kKmM]?)\s*", text or "")
    if not m:
        raise LdhddError("容量の書き方が違います (%r)。例: 128M、16448K" % (text,))
    unit = (m.group(2) or "M").upper()
    size = int(m.group(1)) * (1024 if unit == "K" else 1024 * 1024)
    if size % SECTOR:
        raise LdhddError("容量は 512 バイトの倍数にしてください (%d バイト)" % size)
    if not NEW_MIN_SIZE <= size <= NEW_MAX_SIZE:
        raise LdhddError("容量は 1MB 以上 512MB 以下にしてください (%d バイト)" % size)
    return size


class FatLayout(NamedTuple):
    fat_bits: int
    spc: int
    spf: int
    clusters: int
    total: int  # パーティションのセクタ数
    reserved: int = NEW_RESERVED


def _fat_layout_for(total: int, spc: int, bits: int) -> Optional[FatLayout]:
    """spc と FAT の型を決めた時の spf とクラスタ数 (FAT が足りるまで spf を増やす)"""
    root_secs = NEW_ROOT * 32 // SECTOR
    spf = 1
    while True:
        # LSX-Dodgers の DPB_ADDCL (= データ領域の位置 - 2 クラスタ) が負にならないよう、
        # 足りなければ予約セクタを足してデータ領域を 2 クラスタ分より後ろにする
        reserved = max(NEW_RESERVED, 2 * spc - NEW_NFATS * spf - root_secs)
        data_start = reserved + NEW_NFATS * spf + root_secs
        if data_start >= total:
            return None
        clusters = (total - data_start) // spc
        if spf * SECTOR >= ((clusters + 2) * bits + 7) // 8:
            return FatLayout(bits, spc, spf, clusters, total, reserved)
        spf += 1


def choose_fat_layout(total: int, fat: str = "auto") -> FatLayout:
    """パーティション total セクタの FAT の形を決める。
    auto: 32768 セクタ (16MB) 未満は FAT12、以上は FAT16"""
    if fat == "auto":
        fat = "12" if total < 32768 else "16"
    if fat == "12":
        spc = 1
        while True:
            lay = _fat_layout_for(total, spc, 12)
            if lay is not None and 1 <= lay.clusters < 4085:
                break
            spc *= 2
            if spc > 128:
                raise LdhddError("この容量 (パーティション %d セクタ) は FAT12 にできません" % total)
    elif fat == "16":
        spc = 4 if total <= 262144 else 8 if total <= 524288 else 16
        lay = _fat_layout_for(total, spc, 16)
        while (lay is None or lay.clusters < 4085) and spc > 1:
            spc //= 2
            lay = _fat_layout_for(total, spc, 16)
        if lay is None or not 4085 <= lay.clusters <= 65524:
            raise LdhddError("この容量 (パーティション %d セクタ) は FAT16 にできません" % total)
    else:
        raise LdhddError("--fat は auto / 12 / 16 です (%r)" % (fat,))
    try:
        check_fat_bpb(build_bpb(lay, NEW_PART_LBA, NO_LABEL, 0), total)
    except LdhddError as e:
        raise LdhddError("この容量 (パーティション %d セクタ) では、HDD.COM で使える FAT%s にできません (%s)" % (total, fat, e))
    return lay


def parse_label(text: str) -> bytes:
    """ボリュームラベル: ASCII、英小文字は大文字に、FAT の短い名前と同じ文字、1〜11 バイト"""
    try:
        raw = text.upper().encode("ascii")
    except UnicodeEncodeError:
        raise LdhddError("ボリュームラベルは ASCII の文字だけにしてください (%r)" % (text,))
    if not 1 <= len(raw) <= 11:
        raise LdhddError("ボリュームラベルは 1〜11 文字にしてください (%r)" % (text,))
    if raw[0] == 0x20:
        raise LdhddError("ボリュームラベルの先頭に空白は使えません")
    bad = sorted(set(chr(c) for c in raw if c not in LABEL_CHARS))
    if bad:
        raise LdhddError("ボリュームラベルに使えない文字があります: %s" % " ".join(bad))
    return raw.ljust(11, b" ")


def build_bpb(lay: FatLayout, part_lba: int, label: bytes, volume_id: int) -> bytes:
    b = bytearray(SECTOR)
    b[0:3] = b"\xeb\x3c\x90"
    b[3:11] = b"MSDOS5.0"
    tot16 = lay.total if lay.total < 0x10000 else 0
    struct.pack_into("<HBHBHHBHHHI", b, 11, SECTOR, lay.spc, lay.reserved, NEW_NFATS, NEW_ROOT,
                     tot16, NEW_MEDIA, lay.spf, 63, 255, part_lba)
    struct.pack_into("<I", b, 32, 0 if tot16 else lay.total)
    b[36] = 0x80  # ドライブ番号
    b[38] = 0x29  # 拡張ブート署名
    struct.pack_into("<I", b, 39, volume_id)
    b[43:54] = label
    b[54:62] = b"FAT12   " if lay.fat_bits == 12 else b"FAT16   "
    b[510:512] = b"\x55\xaa"
    return bytes(b)


def fat_head(lay: FatLayout) -> bytes:
    return bytes([NEW_MEDIA, 0xFF, 0xFF]) + (b"\xff" if lay.fat_bits == 16 else b"")


def _lba_to_chs(lba: int) -> bytes:
    c = lba // (255 * 63)
    if c > 1023:
        return b"\xfe\xff\xff"
    h = (lba // 63) % 255
    s = lba % 63 + 1
    return bytes([h, ((c >> 2) & 0xC0) | s, c & 0xFF])


def partition_type(lay: FatLayout) -> int:
    if lay.fat_bits == 12:
        return 0x01
    return 0x04 if lay.total < 0x10000 else 0x06


def build_mbr(part_lba: int, part_sectors: int, ptype: int, disk_sig: int) -> bytes:
    """0〜0x1B7 は 0 (後で boot_record が起動セクタにする)、ディスク署名、第 1 パーティション"""
    m = bytearray(SECTOR)
    struct.pack_into("<I", m, 0x1B8, disk_sig)
    m[0x1BE:0x1CE] = (bytes([0x00]) + _lba_to_chs(part_lba) + bytes([ptype])
                      + _lba_to_chs(part_lba + part_sectors - 1) + struct.pack("<II", part_lba, part_sectors))
    m[510:512] = b"\x55\xaa"
    return bytes(m)


def vhd_geometry(total_sectors: int) -> Tuple[int, int, int]:
    """VHD 仕様の CHS の決め方。(シリンダ, ヘッド, トラックあたりセクタ)"""
    ts = min(total_sectors, 65535 * 16 * 255)
    if ts >= 65535 * 16 * 63:
        spt, heads = 255, 16
        cth = ts // spt
    else:
        spt = 17
        cth = ts // spt
        heads = max(4, (cth + 1023) // 1024)
        if cth >= heads * 1024 or heads > 16:
            spt, heads = 31, 16
            cth = ts // spt
        if cth >= heads * 1024:
            spt, heads = 63, 16
            cth = ts // spt
    return cth // heads, heads, spt


def build_vhd_footer(data_size: int, timestamp: int, uuid: bytes) -> bytes:
    f = bytearray(SECTOR)
    f[0:8] = b"conectix"
    cyl, heads, spt = vhd_geometry(data_size // SECTOR)
    struct.pack_into(">IIQI4sI4sQQHBBI", f, 8, 0x00000002, 0x00010000, 0xFFFFFFFFFFFFFFFF,
                     timestamp & 0xFFFFFFFF, b"ldhd", 0x00010000, b"Wi2k", data_size, data_size,
                     cyl, heads, spt, 2)
    f[68:84] = uuid
    struct.pack_into(">I", f, 64, vhd_checksum(bytes(f)))
    return bytes(f)


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def new_image(path: str, size: int, inputs: Inputs, input_paths: List[str], unit: int = 0,
              fat: str = "auto", label: Optional[str] = None, force: bool = False, out=None) -> FatLayout:
    """MBR + FAT12/16 + 起動部 + 固定 VHD フッターの新しい VHD を作る"""
    if out is None:
        out = sys.stdout
    # 入力・容量・ラベルの誤りは、ファイルを作る前に出す
    sys_bin = makesys(inputs.ld_bin, inputs.com, unit)
    total = size // SECTOR
    lay = choose_fat_layout(total - NEW_PART_LBA, fat)
    label_b = parse_label(label) if label is not None else NO_LABEL
    bpb = build_bpb(lay, NEW_PART_LBA, label_b, int.from_bytes(os.urandom(4), "little"))
    check_fat_bpb(bpb, lay.total)  # 作った形が HDD.COM で使えること (通らなければ作りの誤り)
    for p in input_paths:
        if p and _same_file(p, path):
            raise LdhddError("出力先 %s が入力 (%s) と同じファイルです" % (path, p))
    if os.path.isdir(path):
        raise LdhddError("%s はディレクトリです" % path)
    if os.path.lexists(path) and not force:
        raise LdhddError("%s は既にあります。置き換えるなら --force を付けてください" % path)
    disk_sig = 0
    while disk_sig == 0:
        disk_sig = int.from_bytes(os.urandom(4), "little")
    mbr = build_mbr(NEW_PART_LBA, lay.total, partition_type(lay), disk_sig)
    footer = build_vhd_footer(size, int(time.time()) - VHD_EPOCH, os.urandom(16))
    base = NEW_PART_LBA * SECTOR

    d = _backup_dir(path)
    if force:
        fd, created = _create_staging(d)
    else:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
        except FileExistsError:
            raise LdhddError("%s は既にあります。置き換えるなら --force を付けてください" % path)
        created = path
    committed = False
    mine = None
    f = None
    try:
        mine = os.fstat(fd)
        f = os.fdopen(fd, "r+b", buffering=0)
        with f:
            f.truncate(size)
            for off, data in ((0, mbr), (base, bpb)):
                f.seek(off)
                _write_all(f, data)
            for k in range(NEW_NFATS):
                f.seek(base + (lay.reserved + k * lay.spf) * SECTOR)
                _write_all(f, fat_head(lay))
            if label is not None:
                f.seek(base + (lay.reserved + NEW_NFATS * lay.spf) * SECTOR)
                _write_all(f, label_b + bytes([0x08]) + bytes(20))
            f.seek(size)
            _write_all(f, footer)
            os.fsync(f.fileno())
            # A1 の update と同じ検査を通して起動部を書く (作った物の確かめを兼ねる)
            _update_locked(f, path, inputs, sys_bin, unit, False, False, False, io.StringIO())
            os.fsync(f.fileno())
        if force:
            _replace_durable(created, path)  # ここが確定
        committed = True
    except BaseException as e:
        if f is None:
            try:
                os.close(fd)
            except OSError:
                pass
        left = None
        if not committed:
            # 自分が作ったファイルがその名前のままの時だけ消す (同一性を読めなかった時は、作った直後なので消す)
            try:
                if mine is None or os.path.samestat(os.stat(created), mine):
                    os.remove(created)
            except FileNotFoundError:
                pass
            except OSError as ce:
                left = "作りかけのファイル %s を消せませんでした (%s)。手で消してください" % (created, ce)
        if isinstance(e, OSError):
            if left:
                rest = left + ("。元のファイルはそのままです" if force else "")
            else:
                rest = "元のファイルはそのままです" if force else "何も残していません"
            raise LdhddError("作れませんでした (%s)。%s" % (e, rest))
        if left:
            print("注意: " + left, file=sys.stderr)
        raise
    try:
        _fsync_dir(d)
    except OSError as e:
        raise LdhddError("新しいイメージ %s は置かれましたが、ディスクへの書き込みが確かではありません (%s)" % (path, e))
    return lay


# ---------------------------------------------------------------------------
# FAT12/16 のボリューム (add / ls)
# ---------------------------------------------------------------------------

SHORT_CHARS = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'()-@^_`{}~")
DOT_NAME = b".          "
DOTDOT_NAME = b"..         "


def to_short_name(name: str) -> bytes:
    """ホストの名前を 8.3 の 11 バイトにする (英小文字は大文字に)。合わなければ LdhddError"""
    try:
        raw = name.upper().encode("ascii")
    except UnicodeEncodeError:
        raise LdhddError("%s: ASCII 以外の文字は使えません" % name)
    if not raw or raw.startswith(b"."):
        raise LdhddError("%s: 空の名前や「.」で始まる名前は使えません" % name)
    parts = raw.split(b".")
    if len(parts) > 2:
        raise LdhddError("%s: 「.」は 1 個までです" % name)
    base = parts[0]
    ext = parts[1] if len(parts) == 2 else b""
    if len(parts) == 2 and not ext:
        raise LdhddError("%s: 「.」で終わる名前は使えません" % name)
    if not 1 <= len(base) <= 8 or len(ext) > 3:
        raise LdhddError("%s: 名前は 8 文字まで、拡張子は 3 文字までです (8.3)" % name)
    bad = sorted(set(chr(c) for c in base + ext if c not in SHORT_CHARS))
    if bad:
        raise LdhddError("%s: 使えない文字があります: %s" % (name, " ".join(repr(c) for c in bad)))
    if base[0] == 0xE5:
        base = b"\x05" + base[1:]
    return base.ljust(8, b" ") + ext.ljust(3, b" ")


def short_to_text(name11: bytes) -> str:
    b = bytearray(name11)
    if b[0] == 0x05:
        b[0] = 0xE5
    base = bytes(b[:8]).rstrip(b" ")
    ext = bytes(b[8:11]).rstrip(b" ")
    text = base + (b"." + ext if ext else b"")
    return "".join(chr(c) if 0x20 <= c < 0x7F else "?" for c in text)


def _dos_datetime(ts: float) -> Tuple[int, int]:
    t = time.localtime(ts)
    if t.tm_year < 1980:
        return 0, (0 << 9) | (1 << 5) | 1
    if t.tm_year > 2107:
        return (23 << 11) | (59 << 5) | 29, (127 << 9) | (12 << 5) | 31
    return ((t.tm_hour << 11) | (t.tm_min << 5) | (min(t.tm_sec, 59) // 2),
            ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday)


def make_dir_entry(name11: bytes, attr: int, cluster: int, size: int, ts: float) -> bytes:
    tm, dt = _dos_datetime(ts)
    e = bytearray(32)
    e[0:11] = name11
    e[11] = attr
    struct.pack_into("<HHHHHHHI", e, 14, tm, dt, dt, 0, tm, dt, cluster, size)
    return bytes(e)


class DirEntry(NamedTuple):
    name: bytes  # 11 バイト
    attr: int
    cluster: int
    size: int
    off: int  # イメージの中の項目の位置
    mtime: int
    mdate: int

    @property
    def is_dir(self) -> bool:
        return bool(self.attr & 0x10)


class DirListing(NamedTuple):
    cluster: int  # 0 = ルート
    slots: List[int]  # 全項目の位置 (並び順)
    entries: List[DirEntry]  # ふつうのファイル・フォルダ
    free: List[int]  # 使える項目の位置 (E5h の所と、終わりの印から後ろ)
    end_index: Optional[int]  # 終わりの印 (00h) の項目の番号 (無ければ None)


class FatVolume:
    """開いた VHD の第 1 パーティションの FAT12/16"""

    def __init__(self, f):
        self.f = f
        vk = vhd_kind_of(f)
        mbr = _read_at(f, 0, SECTOR)
        if len(mbr) < SECTOR or mbr[510:512] != b"\x55\xaa":
            raise LdhddError("先頭に MBR (55AA) がありません")
        p1 = read_partitions(mbr)[0]
        if p1.ptype == 0:
            raise LdhddError("第 1 パーティションがありません")
        if p1.lba < 1 or p1.count < 1 or (p1.lba + p1.count) * SECTOR > vk.data_size:
            raise LdhddError("第 1 パーティションの範囲がおかしい (LBA %d から %d セクタ、データは %d バイト)"
                             % (p1.lba, p1.count, vk.data_size))
        self.base = p1.lba * SECTOR
        self.part_end = (p1.lba + p1.count) * SECTOR
        bpb = _read_at(f, self.base, SECTOR)
        check_fat_bpb(bpb, p1.count, lambda off, n: _read_at(f, self.base + off, n))
        self.bps = _u16(bpb, 11)
        self.spc = bpb[13]
        res, self.nfats, self.root_entries = _u16(bpb, 14), bpb[16], _u16(bpb, 17)
        self.spf = _u16(bpb, 22)
        tot = _u16(bpb, 19) or _u32(bpb, 32)
        self.csize = self.spc * self.bps
        self.fat_off = self.base + res * self.bps
        self.fat_bytes = self.spf * self.bps
        self.root_off = self.fat_off + self.nfats * self.fat_bytes
        data_start = res + self.nfats * self.spf + self.root_entries * 32 // self.bps
        self.data_off = self.base + data_start * self.bps
        self.nclusters = (tot - data_start) // self.spc
        self.max_cl = self.nclusters + 1
        self.bits = 12 if self.nclusters < 4085 else 16
        self.eoc = 0xFFF if self.bits == 12 else 0xFFFF
        if self.data_off + self.nclusters * self.csize > self.part_end or tot * self.bps > p1.count * SECTOR:
            raise LdhddError("第 1 パーティションの BPB の大きさがパーティションを越えています")
        fats = [bytearray(_read_at(f, self.fat_off + k * self.fat_bytes, self.fat_bytes)) for k in range(self.nfats)]
        if any(len(x) != self.fat_bytes for x in fats):
            raise LdhddError("FAT を読めません")
        for k in range(1, self.nfats):
            for c in range(self.max_cl + 1):
                if self._get(fats[0], c) != self._get(fats[k], c):
                    raise LdhddError("FAT の写しが食い違っています (クラスタ %d)。直してから使ってください" % c)
        self.fat = fats[0]
        self.fat_orig = bytes(fats[0])

    # --- FAT
    def _get(self, fat: bytearray, c: int) -> int:
        if self.bits == 16:
            return _u16(fat, c * 2)
        v = _u16(fat, c * 3 // 2)
        return v >> 4 if c & 1 else v & 0xFFF

    def get(self, c: int) -> int:
        return self._get(self.fat, c)

    def set(self, c: int, v: int) -> None:
        if self.bits == 16:
            struct.pack_into("<H", self.fat, c * 2, v)
            return
        o = c * 3 // 2
        cur = _u16(self.fat, o)
        cur = (cur & 0x000F) | (v << 4) if c & 1 else (cur & 0xF000) | v
        struct.pack_into("<H", self.fat, o, cur)

    def is_eoc(self, v: int) -> bool:
        return v >= (0xFF8 if self.bits == 12 else 0xFFF8)

    def chain(self, start: int, what: str) -> List[int]:
        out = []
        seen = set()
        c = start
        while True:
            if c < 2 or c > self.max_cl:
                raise LdhddError("FAT が壊れています (%s: 範囲外のクラスタ %d)" % (what, c))
            if c in seen:
                raise LdhddError("FAT が壊れています (%s: 鎖が循環しています)" % what)
            seen.add(c)
            out.append(c)
            v = self.get(c)
            if self.is_eoc(v):
                return out
            if v in (0, 1) or v == self.eoc - 8:  # 空き / 予約 / 不良クラスタ
                raise LdhddError("FAT が壊れています (%s: 鎖が途中で切れています)" % what)
            c = v

    def cluster_off(self, c: int) -> int:
        return self.data_off + (c - 2) * self.csize

    def free_clusters(self) -> List[int]:
        return [c for c in range(2, self.max_cl + 1) if self.get(c) == 0]

    # --- ディレクトリ
    def read_dir(self, cluster: int, what: str = "/") -> DirListing:
        if cluster == 0:
            raw = _read_at(self.f, self.root_off, self.root_entries * 32)
            offs = [self.root_off + i * 32 for i in range(self.root_entries)]
            chunks = [raw[i * 32:i * 32 + 32] for i in range(self.root_entries)]
        else:
            offs, chunks = [], []
            for c in self.chain(cluster, what):
                raw = _read_at(self.f, self.cluster_off(c), self.csize)
                for i in range(self.csize // 32):
                    offs.append(self.cluster_off(c) + i * 32)
                    chunks.append(raw[i * 32:i * 32 + 32])
        entries, free = [], []
        end_index = None
        for i, (off, e) in enumerate(zip(offs, chunks)):
            if end_index is not None:
                free.append(off)
                continue
            if e[0] == 0x00:
                end_index = i
                free.append(off)
                continue
            if e[0] == 0xE5:
                free.append(off)
                continue
            attr = e[11]
            if attr == 0x0F or attr & 0x08:  # 長い名前の項目・ボリュームラベル: 場所は使用中、中身としては扱わない
                continue
            name = bytes(e[0:11])
            if name in (DOT_NAME, DOTDOT_NAME):
                continue
            entries.append(DirEntry(name, attr, _u16(e, 26), _u32(e, 28), off, _u16(e, 22), _u16(e, 24)))
        return DirListing(cluster, offs, entries, free, end_index)

    def walk_check(self) -> None:
        """木全体をたどり、クラスタの持ち主が 1 つずつであること、フォルダが祖先を指さないこと、
        ファイルの鎖の長さがサイズに足りることを確かめる"""
        owner = {}
        stack = [(0, "/", frozenset([0]))]
        visits = 0
        while stack:
            cl, path, ancestors = stack.pop()
            visits += 1
            if visits > self.nclusters + 2:
                raise LdhddError("FAT が壊れています (フォルダが多すぎる、または循環しています)")
            listing = self.read_dir(cl, path)
            for e in listing.entries:
                p = path.rstrip("/") + "/" + short_to_text(e.name)
                if e.is_dir:
                    if e.cluster in ancestors or e.cluster == 0:
                        raise LdhddError("FAT が壊れています (%s が祖先のフォルダを指しています)" % p)
                    for c in self.chain(e.cluster, p):
                        if c in owner:
                            raise LdhddError("FAT が壊れています (%s と %s がクラスタ %d を共有しています)" % (p, owner[c], c))
                        owner[c] = p
                    stack.append((e.cluster, p, ancestors | {e.cluster}))
                else:
                    if e.cluster == 0:
                        if e.size:
                            raise LdhddError("FAT が壊れています (%s: サイズがあるのにクラスタがありません)" % p)
                        continue
                    ch = self.chain(e.cluster, p)
                    if len(ch) * self.csize < e.size:
                        raise LdhddError("FAT が壊れています (%s: 鎖がサイズに足りません)" % p)
                    for c in ch:
                        if c in owner:
                            raise LdhddError("FAT が壊れています (%s と %s がクラスタ %d を共有しています)" % (p, owner[c], c))
                        owner[c] = p

    def find(self, listing: DirListing, name11: bytes) -> Optional[DirEntry]:
        for e in listing.entries:
            if e.name == name11:
                return e
        return None

    def resolve_dir(self, path: str) -> int:
        """「/A/B」のフォルダのクラスタ (ルートは 0)。無ければ LdhddError"""
        cl = 0
        for part in [p for p in path.replace("\\", "/").split("/") if p]:
            e = self.find(self.read_dir(cl), to_short_name(part))
            if e is None or not e.is_dir:
                raise LdhddError("フォルダがありません: %s" % path)
            cl = e.cluster
        return cl


# --- add の計画

class HostNode(NamedTuple):
    name11: bytes
    path: str  # ホストのパス
    is_dir: bool
    size: int
    mtime: float
    children: List["HostNode"]


def scan_host(paths: List[str], skip_dotfiles: bool) -> Tuple[List[HostNode], List[str], List[str]]:
    """ホストのファイル・フォルダを読む。(木, 名前のエラー, 飛ばした物)"""
    errors, skipped = [], []

    def node(path: str, name: str, top: bool) -> Optional[HostNode]:
        if os.path.islink(path):
            errors.append("%s: シンボリックリンクは入れられません" % path)
            return None
        if name.startswith(".") and not top and skip_dotfiles:
            skipped.append(path)
            return None
        try:
            name11 = to_short_name(name)
        except LdhddError as e:
            errors.append("%s (%s)" % (e, path))
            name11 = None
        st = os.stat(path)
        if os.path.isdir(path):
            kids = []
            for entry in sorted(os.listdir(path)):
                k = node(os.path.join(path, entry), entry, False)
                if k is not None:
                    kids.append(k)
            seen = {}
            for k in kids:
                if k.name11 in seen:
                    errors.append("%s と %s は 8.3 にすると同じ名前になります" % (seen[k.name11], k.path))
                seen[k.name11] = k.path
            return None if name11 is None else HostNode(name11, path, True, 0, st.st_mtime, kids)
        if not os.path.isfile(path):
            errors.append("%s: ふつうのファイルでもフォルダでもありません" % path)
            return None
        return None if name11 is None else HostNode(name11, path, False, st.st_size, st.st_mtime, [])

    nodes = []
    for p in paths:
        if not os.path.lexists(p):
            errors.append("%s: ありません" % p)
            continue
        name = os.path.basename(os.path.normpath(p))
        n = node(p, name, True)
        if n is not None:
            nodes.append(n)
    seen = {}
    for n in nodes:
        if n.name11 in seen:
            errors.append("%s と %s は 8.3 にすると同じ名前になります" % (seen[n.name11], n.path))
        seen[n.name11] = n.path
    return nodes, errors, skipped


class AddPlan:
    def __init__(self):
        self.new_dirs = 0
        self.files = 0
        self.replaced = 0
        self.bytes = 0
        self.lines: List[str] = []
        self.ops = []  # 実行の手順 (下の execute で使う)
        self.need_clusters = 0


def _ceil(a: int, b: int) -> int:
    return (a + b - 1) // b


def plan_add(vol: FatVolume, nodes: List[HostNode], to_path: str, overwrite: bool) -> AddPlan:
    """書く前に、ぶつかり・容量・項目の数を全部決める"""
    plan = AddPlan()
    errors: List[str] = []

    # 入れる先のフォルダ (無ければ作る)
    target: Tuple[str, object] = ("existing", 0)  # ("existing", cluster) / ("new", NewDir)
    shown = "/"
    for part in [p for p in to_path.replace("\\", "/").split("/") if p]:
        n11 = to_short_name(part)
        shown = shown.rstrip("/") + "/" + short_to_text(n11)
        if target[0] == "existing":
            listing = vol.read_dir(target[1], shown)
            e = vol.find(listing, n11)
            if e is not None:
                if not e.is_dir:
                    raise LdhddError("%s はファイルです (フォルダではありません)" % shown)
                target = ("existing", e.cluster)
                continue
        nd = NewDir(n11, shown, time.time())
        _attach(plan, target, nd, None)
        plan.new_dirs += 1
        plan.lines.append("フォルダを作る: %s" % shown)
        target = ("new", nd)

    def merge(dest, node: HostNode, dest_path: str) -> None:
        p = dest_path.rstrip("/") + "/" + short_to_text(node.name11)
        existing = None
        if dest[0] == "existing":
            existing = vol.find(vol.read_dir(dest[1], dest_path), node.name11)
        if node.is_dir:
            if existing is not None:
                if not existing.is_dir:
                    errors.append("%s: VHD に同じ名前のファイルがあります" % p)
                    return
                sub = ("existing", existing.cluster)
            else:
                nd = NewDir(node.name11, p, node.mtime)
                _attach(plan, dest, nd, None)
                plan.new_dirs += 1
                plan.lines.append("フォルダを作る: %s" % p)
                sub = ("new", nd)
            for k in node.children:
                merge(sub, k, p)
            return
        if existing is not None:
            if existing.is_dir:
                errors.append("%s: VHD に同じ名前のフォルダがあります" % p)
                return
            if not overwrite:
                errors.append("%s: VHD に同じ名前のファイルがあります (置き換えるなら --overwrite)" % p)
                return
            plan.replaced += 1
            plan.lines.append("置き換える: %s (%d バイト)" % (p, node.size))
        else:
            plan.lines.append("入れる: %s (%d バイト)" % (p, node.size))
        plan.files += 1
        plan.bytes += node.size
        _attach(plan, dest, NewFile(node), existing)

    for n in nodes:
        merge(target, n, shown)
    if errors:
        raise LdhddError("入れられません:\n  " + "\n  ".join(errors))

    # 容量と項目の数
    need = 0
    for op in plan.ops:
        kind = op[0]
        if kind == "file":
            need += _ceil(op[2].node.size, vol.csize)
    for nd in _all_new_dirs(plan):
        need += max(1, _ceil((2 + len(nd.children)) * 32, vol.csize))
    for cl, count in _existing_dir_additions(plan).items():
        listing = vol.read_dir(cl)
        short = count - len(listing.free)
        if short > 0:
            if cl == 0:
                raise LdhddError("ルートの項目が足りません (あと %d 個要ります。ルートは %d 個まで)" % (short, vol.root_entries))
            need += _ceil(short * 32, vol.csize)
    free = len(vol.free_clusters())
    plan.need_clusters = need
    if need > free:
        raise LdhddError("空きが足りません (要る %d クラスタ = %d バイト、空き %d クラスタ = %d バイト)"
                         % (need, need * vol.csize, free, free * vol.csize))
    return plan


class NewFile(NamedTuple):
    node: HostNode


class NewDir:
    def __init__(self, name11: bytes, path: str, mtime: float):
        self.name11 = name11
        self.path = path
        self.mtime = mtime
        self.children: List[object] = []  # NewDir / NewFile
        self.cluster = None


def _attach(plan: AddPlan, dest, item, existing: Optional[DirEntry]) -> None:
    if dest[0] == "new":
        dest[1].children.append(item)
        if isinstance(item, NewFile):
            plan.ops.append(("file", dest, item, None))
        return
    if isinstance(item, NewDir):
        plan.ops.append(("dir", dest, item, None))
    else:
        plan.ops.append(("file", dest, item, existing))


def _all_new_dirs(plan: AddPlan) -> List[NewDir]:
    out = []
    stack = [op[2] for op in plan.ops if op[0] == "dir"]
    while stack:
        d = stack.pop()
        out.append(d)
        stack.extend(c for c in d.children if isinstance(c, NewDir))
    return out


def _existing_dir_additions(plan: AddPlan) -> Dict[int, int]:
    """既にあるフォルダごとの、新しく要る項目の数 (置き換えは同じ項目を使うので数えない)"""
    out: Dict[int, int] = {}
    for kind, dest, item, existing in plan.ops:
        if dest[0] == "existing" and existing is None:
            out[dest[1]] = out.get(dest[1], 0) + 1
    return out


def execute_add(vol: FatVolume, plan: AddPlan) -> None:
    """データ → FAT → ディレクトリ項目 の順に書く"""
    f = vol.f
    free = iter(vol.free_clusters())
    data_writes: List[Tuple[int, bytes]] = []
    entry_writes: Dict[int, bytes] = {}
    freed: List[int] = []

    def alloc(n: int) -> List[int]:
        cs = [next(free) for _ in range(n)]
        for a, b in zip(cs, cs[1:]):
            vol.set(a, b)
        if cs:
            vol.set(cs[-1], vol.eoc)
        return cs

    def write_file(item: NewFile) -> Tuple[int, int]:
        node = item.node
        cs = alloc(_ceil(node.size, vol.csize))
        with open(node.path, "rb") as src:
            for c in cs:
                chunk = src.read(vol.csize)
                data_writes.append((vol.cluster_off(c), chunk.ljust(vol.csize, b"\0")))
            if src.read(1):
                raise LdhddError("%s: 読んでいる間に大きくなりました" % node.path)
        return (cs[0] if cs else 0), node.size

    def build_new_dir(nd: NewDir, parent_cluster: int) -> None:
        n = max(1, _ceil((2 + len(nd.children)) * 32, vol.csize))
        cs = alloc(n)
        nd.cluster = cs[0]
        body = bytearray(n * vol.csize)
        body[0:32] = make_dir_entry(DOT_NAME, 0x10, nd.cluster, 0, nd.mtime)
        body[32:64] = make_dir_entry(DOTDOT_NAME, 0x10, parent_cluster, 0, nd.mtime)
        i = 2
        for child in nd.children:
            if isinstance(child, NewDir):
                build_new_dir(child, nd.cluster)
                ent = make_dir_entry(child.name11, 0x10, child.cluster, 0, child.mtime)
            else:
                cl, size = write_file(child)
                ent = make_dir_entry(child.node.name11, 0x20, cl, size, child.node.mtime)
            body[i * 32:i * 32 + 32] = ent
            i += 1
        for k, c in enumerate(cs):
            data_writes.append((vol.cluster_off(c), bytes(body[k * vol.csize:(k + 1) * vol.csize])))

    # 既にあるフォルダへの書き足し
    slots_by_dir: Dict[int, List[int]] = {}
    marker_by_dir: Dict[int, Optional[int]] = {}

    def take_slot(cl: int) -> int:
        if cl not in slots_by_dir:
            listing = vol.read_dir(cl)
            slots_by_dir[cl] = list(listing.free)
            marker_by_dir[cl] = listing.slots[listing.end_index] if listing.end_index is not None else None
            slots_by_dir[(cl, "all")] = listing.slots
        if not slots_by_dir[cl]:
            # フォルダの鎖を 1 クラスタ伸ばす (0 で埋める)
            last = vol.chain(cl, "フォルダ")[-1]
            c = alloc(1)[0]
            vol.set(last, c)
            data_writes.append((vol.cluster_off(c), bytes(vol.csize)))
            new_slots = [vol.cluster_off(c) + i * 32 for i in range(vol.csize // 32)]
            slots_by_dir[cl].extend(new_slots)
            slots_by_dir[(cl, "all")] = slots_by_dir[(cl, "all")] + new_slots
        return slots_by_dir[cl].pop(0)

    for kind, dest, item, existing in plan.ops:
        if dest[0] != "existing":
            continue
        cl = dest[1]
        if kind == "dir":
            build_new_dir(item, cl)
            entry_writes[take_slot(cl)] = make_dir_entry(item.name11, 0x10, item.cluster, 0, item.mtime)
        else:
            new_cl, size = write_file(item)
            ent = make_dir_entry(item.node.name11, 0x20, new_cl, size, item.node.mtime)
            if existing is not None:
                if existing.cluster:
                    freed.extend(vol.chain(existing.cluster, short_to_text(existing.name)))
                entry_writes[existing.off] = ent
            else:
                entry_writes[take_slot(cl)] = ent

    # 終わりの印を使ったフォルダは、使った最後の項目の次に印を置き直す
    for cl, marker in marker_by_dir.items():
        if marker is None:
            continue
        all_slots = slots_by_dir[(cl, "all")]
        used = [o for o in all_slots if o in entry_writes]
        if not used:
            continue
        last = max(all_slots.index(o) for o in used)
        if last >= all_slots.index(marker) and last + 1 < len(all_slots):
            nxt = all_slots[last + 1]
            if nxt not in entry_writes:
                entry_writes[nxt] = b"\0" + _read_at(f, nxt + 1, 31)

    for c in freed:
        vol.set(c, 0)

    # 1. データ
    for off, data in data_writes:
        f.seek(off)
        _write_all(f, data)
    # 2. FAT (全部の写し)
    for k in range(vol.nfats):
        f.seek(vol.fat_off + k * vol.fat_bytes)
        _write_all(f, bytes(vol.fat))
    # 3. ディレクトリ項目
    for off in sorted(entry_writes):
        f.seek(off)
        _write_all(f, entry_writes[off])
    os.fsync(f.fileno())
    # 読み戻し
    for k in range(vol.nfats):
        if _read_at(f, vol.fat_off + k * vol.fat_bytes, vol.fat_bytes) != bytes(vol.fat):
            raise LdhddError("書いた FAT を読み戻したら一致しませんでした")
    for off, data in entry_writes.items():
        if _read_at(f, off, 32) != data:
            raise LdhddError("書いたディレクトリ項目を読み戻したら一致しませんでした")


def add(target: str, paths: List[str], to_path: str = "/", overwrite: bool = False, backup: bool = True,
        dry_run: bool = False, skip_dotfiles: bool = False, out=None) -> Optional[str]:
    if out is None:
        out = sys.stdout
    nodes, errors, skipped = scan_host(paths, skip_dotfiles)
    for s in skipped:
        print("飛ばしました: %s" % s, file=out)
    if errors:
        raise LdhddError("入れられない名前があります (8.3 の名前、ASCII、英数字など):\n  " + "\n  ".join(errors))
    with _open_target(target, not dry_run) as f:
        unlock = _lock(f) if not dry_run else None
        try:
            vol = FatVolume(f)
            vol.walk_check()
            plan = plan_add(vol, nodes, to_path, overwrite)
            for line in plan.lines:
                print(line, file=out)
            print("合わせて: ファイル %d 個 (%d バイト、うち置き換え %d 個)、新しいフォルダ %d 個、要る %d クラスタ / 空き %d クラスタ"
                  % (plan.files, plan.bytes, plan.replaced, plan.new_dirs, plan.need_clusters, len(vol.free_clusters())), file=out)
            if dry_run:
                print("--dry-run なので書き込みません", file=out)
                return None
            bak = None
            if backup:
                bak = make_backup(f, target)
                print("バックアップ: %s" % bak, file=out)
            try:
                execute_add(vol, plan)
            except (OSError, LdhddError) as e:
                hint = ("バックアップ %s を %s にコピーして戻してください" % (bak, target)) if bak else "バックアップはありません"
                raise LdhddError("書き込みに失敗しました: %s。%s" % (e, hint))
        finally:
            if unlock is not None:
                unlock()
    print("完了しました", file=out)
    return bak


def ls(target: str, path: str = "/", recursive: bool = False, out=None) -> None:
    if out is None:
        out = sys.stdout
    with _open_target(target, False) as f:
        vol = FatVolume(f)
        if recursive:
            vol.walk_check()
        stack = [(vol.resolve_dir(path), "/" + "/".join(short_to_text(to_short_name(p)) for p in path.replace("\\", "/").split("/") if p))]
        while stack:
            cl, shown = stack.pop(0)
            print("%s:" % (shown or "/"), file=out)
            listing = vol.read_dir(cl, shown)
            subs = []
            for e in listing.entries:
                d, t = e.mdate, e.mtime
                stamp = "%04d-%02d-%02d %02d:%02d" % (1980 + (d >> 9), (d >> 5) & 15, d & 31, t >> 11, (t >> 5) & 63)
                print("  %-12s %10s  %s" % (short_to_text(e.name), "<DIR>" if e.is_dir else e.size, stamp), file=out)
                if e.is_dir and recursive:
                    subs.append((e.cluster, shown.rstrip("/") + "/" + short_to_text(e.name)))
            stack[0:0] = subs
        free = len(vol.free_clusters())
        print("空き: %d バイト (%d クラスタ x %d バイト)" % (free * vol.csize, free, vol.csize), file=out)


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

    p = sub.add_parser("new", help="容量を指定して、起動できる固定 VHD を新しく作る")
    p.add_argument("output", help="作る VHD のファイル名")
    p.add_argument("--size", required=True, help="VHD 全体の大きさ (例: 128M、16448K。単位なしは MB。1MB〜512MB)")
    add_inputs(p)
    p.add_argument("--fat", default="auto", choices=("auto", "12", "16"),
                   help="FAT の種類 (既定 auto: パーティションが 16MB 未満は FAT12、以上は FAT16)")
    p.add_argument("--label", help="ボリュームラベル (英数字など 11 文字まで)")
    p.add_argument("--force", action="store_true", help="出力先が既にあれば置き換える")

    p = sub.add_parser("add", help="ファイルやフォルダを VHD の第 1 パーティション (FAT) に入れる")
    p.add_argument("target", help="入れる先の VHD")
    p.add_argument("sources", nargs="+", help="入れるファイルやフォルダ (フォルダは中身ごと)")
    p.add_argument("--to", default="/", help="入れる先のフォルダ (既定 /。無ければ作る)")
    p.add_argument("--overwrite", action="store_true", help="同じ名前のファイルがあれば置き換える")
    p.add_argument("--skip-dotfiles", action="store_true", help="フォルダの中の「.」で始まる物を飛ばす")
    p.add_argument("--no-backup", action="store_true", help="バックアップを取らない")
    p.add_argument("--dry-run", action="store_true", help="何を入れるかを表示するだけで書かない")

    p = sub.add_parser("ls", help="VHD の第 1 パーティションの中を表示する")
    p.add_argument("target")
    p.add_argument("path", nargs="?", default="/", help="表示するフォルダ (既定 /)")
    p.add_argument("-R", "--recursive", action="store_true", help="下のフォルダも表示する")

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
        elif args.cmd == "new":
            size = parse_size(args.size)
            inputs = load_inputs(args.ldsys, args.ld_bin, args.hdd_com)
            new_image(args.output, size, inputs, [args.ldsys, args.ld_bin, args.hdd_com], unit=args.unit,
                      fat=args.fat, label=args.label, force=args.force)
            print("%s を作りました (%d バイト + VHD フッター)" % (args.output, size))
            info(args.output)
        elif args.cmd == "add":
            add(args.target, args.sources, to_path=args.to, overwrite=args.overwrite, backup=not args.no_backup,
                dry_run=args.dry_run, skip_dotfiles=args.skip_dotfiles)
        elif args.cmd == "ls":
            ls(args.target, args.path, args.recursive)
        elif args.cmd == "info":
            info(args.target)
    except (LdhddError, OSError) as e:
        print("エラー: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
