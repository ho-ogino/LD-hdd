#!/usr/bin/env python3
"""ldfsck.py: LSX-Dodgers のフォルダの鎖の誤りを見付ける・直す (VHD の第 1 パーティションの FAT12/16)

LSX-Dodgers 1.55〜1.62c は、いっぱいのフォルダに 1 クラスタ足す所 (LDFILE.ASM の COPEN) で
`LD B,1` のまま WRDF (BC 個のクラスタを確保する) を呼ぶ。C は必ず 0 なので、空きクラスタを 256 個
つなぎ、最後の 1 つだけを 0 で埋めて、そこに新しい項目を書く。LSX は鎖の頭から読んで 00h で止まるので、
その項目は見えなくなる。

  check VHD [-v]           読むだけ。フォルダの鎖の様子と FAT の誤りを出す
  fix VHD --out NEW.vhd    元は書き換えず、新しい写しに直す (直した写しを確かめてから名前を付ける)

終わり方: 0 = 問題なし (fix は直して確かめた)、2 = check で問題が見つかった、1 = 誤り
"""

import argparse
import errno
import os
import sys
from typing import Callable, Dict, List, NamedTuple, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ldhdd  # noqa: E402
from ldhdd import LdhddError, _read_at, _u16, _u32  # noqa: E402

FORBIDDEN = set(b'"*+,./:;<=>?[\\]|')
NO_LINK_ERRNOS = {getattr(errno, n) for n in ("EPERM", "EOPNOTSUPP", "ENOTSUP", "EMLINK") if hasattr(errno, n)}
LIMIT = 50  # 詳しく出す壊れ方・注の数 (それより多い分は数だけ)
GROWTH = 256  # COPEN の誤りで、いっぱいのフォルダに足されるクラスタの数 (BC = 0100h)


class Slot(NamedTuple):
    off: int  # イメージの中の位置
    raw: bytes  # 32 バイト

    @property
    def name11(self) -> bytes:
        return self.raw[:11]

    @property
    def attr(self) -> int:
        return self.raw[11]

    @property
    def cluster(self) -> int:
        return _u16(self.raw, 26)

    @property
    def size(self) -> int:
        return _u32(self.raw, 28)


class Node:
    """フォルダ・ファイルの場所。道筋の文字列は持たず、親と自分の名前だけを持つ (深い木でもメモリを食わない)"""
    __slots__ = ("parent", "name11", "cluster")

    def __init__(self, parent: Optional["Node"], name11: Optional[bytes], cluster: int):
        self.parent = parent
        self.name11 = name11
        self.cluster = cluster

    def path(self) -> str:
        parts = []
        n = self
        while n is not None and n.name11 is not None:
            parts.append(show(n.name11))
            n = n.parent
        return "/" + "/".join(reversed(parts))


class DirInfo:
    """サブフォルダ 1 つの鎖の様子"""

    def __init__(self, node: Node, chain: List[int]):
        self.node = node
        self.chain = chain
        self.dots: List[Tuple[bytes, int]] = []  # . と .. の項目 (名前, 指すクラスタ)
        self.dot_refs: List[Tuple["DirInfo", bytes, int]] = []  # 合わない . / .. が、空きに戻す予定のクラスタを指している物 (どのフォルダの, 名前, クラスタ)
        self.marker: Optional[Tuple[int, int]] = None  # 終わりの印 (00h) の (鎖の何番目, 何番の項目)
        self.visible: List[Slot] = []  # 終わりの印より前の、ふつうのファイル・フォルダの項目
        self.after_count = 0  # 終わりの印より後ろの、00h・E5h でない項目の数
        self.detached_lfn: List[Slot] = []  # 終わりの印の直前にある、短い名前と組になっていない長い名前の項目
        self.long_names: Set[str] = set()  # 見える項目の VFAT の長い名前 (casefold)
        self.empty_tail = False  # 余分なクラスタが全部 0 (LSX の誤りの印である項目が無い)
        self.shape_ok = False  # 余分なクラスタが LSX の誤りの形とぴったり同じか
        self.shape_reason = ""
        self.hidden: List[Slot] = []  # 形が合う時の、最後の余分なクラスタの項目 (移す候補)
        self.hidden_bad: List[Tuple[Slot, Callable[[], str]]] = []  # 移せない隠れた項目と、理由の文を作る関数 (表示の時に作る)
        self.incoming: List[Tuple[int, int]] = []  # 外から余分なクラスタへのつなぎ (指す側, 指される側)
        self.added: Set[int] = set()  # 足されたと思われるクラスタのうち、終わりの印まで (check -v で確かめを促す)
        self.data_off = 0
        self.csize = 1
        self.first_e5: Optional[int] = None  # 終わりの印より前で、消した項目の跡 (E5h) が最初にある、鎖の何番目のクラスタ

    @property
    def path(self) -> str:
        return self.node.path()

    @property
    def kept(self) -> List[int]:
        return self.chain if self.marker is None else self.chain[:self.marker[0] + 1]

    @property
    def tail(self) -> List[int]:
        return [] if self.marker is None else self.chain[self.marker[0] + 1:]

    @property
    def key(self) -> int:
        return self.chain[0]

    @property
    def repairable(self) -> bool:
        return bool(self.tail) and self.shape_ok and not self.hidden_bad and not self.incoming and not self.dot_refs


class Report:
    def __init__(self):
        self.mirror = ""
        self.mirror_ok = True
        self.dirs: List[DirInfo] = []
        self.problems: List[str] = []  # 壊れ方 (直さない)。詳しい文は LIMIT 件まで、残りは数だけ
        self.notes: List[str] = []
        self.more_problems = 0
        self.more_notes = 0
        self.lost: List[int] = []  # どこからも使われていない使用中のクラスタ
        self.lost_heads: List[int] = []
        self.visible: Dict[int, bytes] = {}  # フォルダの先頭クラスタ (ルートは 0) → 見える項目を並びのまま 18 バイトずつ (record)

    def add_problem(self, make) -> None:
        """文は件数が LIMIT 未満の時だけ作る (深い木で道筋の文を何千も持たない)"""
        if len(self.problems) < LIMIT:
            self.problems.append(make())
        else:
            self.more_problems += 1

    def add_note(self, make) -> None:
        if len(self.notes) < LIMIT:
            self.notes.append(make())
        else:
            self.more_notes += 1

    @property
    def tail_dirs(self) -> List[DirInfo]:
        """終わりの印の後ろに余分なクラスタがあり、そこが全部 0 ではないフォルダ (全部 0 のフォルダは注として知らせるだけ)"""
        return [d for d in self.dirs if d.tail and not d.empty_tail]

    @property
    def blocking(self) -> List[str]:
        """fix が止まる理由。文は LIMIT 件まで作り、残りは数だけ数える (作ってから捨てない)"""
        out: List[str] = []
        more = self.more_problems

        def add(make) -> None:
            nonlocal more
            if len(out) < LIMIT:
                out.append(make())
            else:
                more += 1
        for p in self.problems:
            add(lambda p=p: p)
        for d in self.tail_dirs:
            if not d.shape_ok:
                add(lambda d=d: "%s: 余分なクラスタが LSX の誤りの形と違います (%s)。推測では直しません" % (d.path, d.shape_reason))
            for s, why in d.hidden_bad:
                add(lambda d=d, s=s, why=why: "%s: 隠れた項目 %s を移せません (%s)" % (d.path, show(s.name11), why()))
            for src, dst in d.incoming:
                add(lambda d=d, src=src, dst=dst: "%s: 外のクラスタ %d が、このフォルダの鎖のクラスタ %d を指しています"
                    % (d.path, src, dst))
            for owner_d, name11, c in d.dot_refs:
                add(lambda d=d, o=owner_d, n=name11, c=c: "%s: %s の %s が、空きに戻す予定のクラスタ %d を指しています"
                    % (d.path, o.path, n.decode("ascii").strip(), c))
        if more:
            out.append("ほか %d 件" % more)
        return out

    @property
    def clean(self) -> bool:
        return not self.problems and not self.tail_dirs and not self.lost


def name_text(name11: bytes) -> str:
    b = bytearray(name11)
    if b[0] == 0x05:
        b[0] = 0xE5
    base = bytes(b[:8]).rstrip(b" ").decode("cp932", "replace")
    ext = bytes(b[8:11]).rstrip(b" ").decode("cp932", "replace")
    return base + ("." + ext if ext else "")


def show(name11: bytes) -> str:
    """表示用の名前。制御文字などは \\xNN の形にする (比べるのには name_text を使う)"""
    return "".join(c if c.isprintable() else ("\\x%02x" % ord(c) if ord(c) < 0x100 else "\\u%04x" % ord(c))
                   for c in name_text(name11))


def valid_name(name11: bytes) -> bool:
    """8.3 の名前として正しいか。名前と拡張子をそれぞれシフト JIS として 1 字ずつ読む"""
    b = bytearray(name11)
    if len(b) != 11 or b[0] == 0x20:
        return False
    if b[0] == 0x05:  # E5h の代わり
        b[0] = 0xE5
    for fld in (b[:8], b[8:11]):
        i = 0
        padding = False
        while i < len(fld):
            c = fld[i]
            if c == 0x20:
                padding = True
                i += 1
                continue
            if padding:
                return False  # 空白の埋めの後ろに字がある
            if 0x81 <= c <= 0x9F or 0xE0 <= c <= 0xFC:
                if i + 1 >= len(fld) or not (0x40 <= fld[i + 1] <= 0x7E or 0x80 <= fld[i + 1] <= 0xFC):
                    return False
                i += 2
                continue
            if 0xA1 <= c <= 0xDF:
                i += 1
                continue
            if c < 0x21 or c >= 0x7F or c in FORBIDDEN or 0x61 <= c <= 0x7A:
                return False
            i += 1
        try:
            bytes(fld).decode("cp932")  # 範囲は合っていても字の割り当てが無い 2 バイト (81h ADh など) は不可
        except UnicodeDecodeError:
            return False
    return True


def record(name11: bytes, attr: int, cluster: int, size: int) -> bytes:
    """見える項目 1 つを比べるための 18 バイト (名前, 属性, 先頭クラスタ, サイズ)"""
    return name11 + bytes([attr]) + cluster.to_bytes(2, "little") + size.to_bytes(4, "little")


def records(slots: List[Slot]) -> bytes:
    return b"".join(record(x.name11, x.attr, x.cluster, x.size) for x in slots)


def ranges(cs: List[int]) -> str:
    out = []
    i = 0
    while i < len(cs):
        j = i
        while j + 1 < len(cs) and cs[j + 1] == cs[j] + 1:
            j += 1
        out.append(str(cs[i]) if i == j else "%d〜%d" % (cs[i], cs[j]))
        i = j + 1
    return ", ".join(out)


def _is_entry(s: Slot) -> bool:
    """ふつうのファイル・フォルダの項目 (長い名前・ラベル・. と .. を除く)"""
    if s.raw[0] in (0x00, 0xE5) or s.attr == 0x0F or s.attr & 0x08:
        return False
    return s.name11 not in (ldhdd.DOT_NAME, ldhdd.DOTDOT_NAME)


def scan(vol) -> Report:
    """読むだけ。フォルダをたどり、鎖の様子と FAT の誤りを集める"""
    rep = Report()
    rep.mirror = vol.mirror_status()
    rep.mirror_ok = not vol.mirror_diffs
    # クラスタの持ち主。道筋の文字列ではなく (場所, ファイル名 or None, 添え書き) を持ち、表示する時に組み立てる
    owner: Dict[int, Tuple[Node, Optional[bytes], str]] = {}

    def who_text(w: Tuple[Node, Optional[bytes], str]) -> str:
        node, name11, tag = w
        p = node.path()
        if name11 is not None:
            p = p.rstrip("/") + "/" + show(name11)
        return p + tag

    bad_seen: Set[int] = set()  # 壊れた鎖・重なる鎖の途中と分かったクラスタ (2 度たどらない)

    def follow(start: int, who: Tuple[Node, Optional[bytes], str], register: bool = True,
               report: bool = True) -> Tuple[Optional[List[int]], Optional[Callable[[], str]]]:
        """鎖をたどる。ほかの持ち主・壊れた鎖に当たったら、そこで止める (どのクラスタも、たどるのはほぼ 1 回)。
        通れば (鎖, None)、だめなら (None, 理由の文を作る関数)。register なら持ち主として登録する"""
        out: List[int] = []
        seen: Set[int] = set()
        c = start
        while True:
            if c < 2 or c > vol.usable_max:
                why = (lambda c=c: "範囲外のクラスタ %d があります" % c)
            elif c in owner:
                why = (lambda c=c, o=owner[c]: "クラスタ %d を %s と共有しています" % (c, who_text(o)))
            elif c in bad_seen:
                why = (lambda c=c: "壊れた鎖 (ほかで知らせた物) にクラスタ %d で合流しています" % c)
            elif c in seen:
                why = (lambda c=c: "鎖が循環しています (クラスタ %d)" % c)
            else:
                seen.add(c)
                out.append(c)
                v = vol.get(c)
                if vol.is_eoc(v):
                    if register:
                        for x in out:
                            owner[x] = who
                    return out, None
                if v in (0, 1) or v == vol.eoc - 8:
                    why = (lambda c=c: "鎖が途中で切れています (クラスタ %d の次が空き・予約・不良)" % c)
                else:
                    c = v
                    continue
            bad_seen.update(out)
            if report:
                rep.add_problem(lambda: "%s: %s" % (who_text(who), why()))
            return None, why

    # 1. 見える木: ルートから、終わりの印までの項目をたどる。
    # フォルダは、たどる番が来た時に読む (読み込んだ項目を持つのは 1 フォルダ分だけ。直す候補のフォルダは後で使うので残す)
    root_raw = _read_at(vol.f, vol.root_off, vol.root_entries * 32)
    root_vis = []
    for i in range(vol.root_entries):
        sl = Slot(vol.root_off + i * 32, root_raw[i * 32:i * 32 + 32])
        if sl.raw[0] == 0:
            break
        if _is_entry(sl):
            root_vis.append(sl)
    rep.visible[0] = records(root_vis)
    root_node = Node(None, None, 0)
    dot_bad: List[Tuple[DirInfo, bytes, int]] = []  # 自分・親と合わない . / .. (フォルダ, 名前, 指すクラスタ)

    def walk_entries(node: Node, entries: List[Slot]) -> None:
        for e in entries:
            if e.attr & 0x10:
                child = Node(node, e.name11, e.cluster)
                if e.cluster == 0:
                    rep.add_problem(lambda: "%s がルートを指しています" % child.path())
                    continue
                # 祖先のフォルダを指す物は、その先頭クラスタがもう祖先の持ち物なので、ここで「共有」として止まる
                chain, _ = follow(e.cluster, (child, None, ""))
                if chain is not None:
                    stack.append((child, chain))
            else:
                if e.cluster == 0:
                    if e.size:
                        rep.add_problem(lambda: "%s/%s: サイズがあるのにクラスタがありません" % (node.path().rstrip("/"), show(e.name11)))
                    continue
                chain, _ = follow(e.cluster, (node, e.name11, ""))
                if chain is not None and len(chain) * vol.csize < e.size:
                    rep.add_problem(lambda: "%s/%s: 鎖がサイズに足りません" % (node.path().rstrip("/"), show(e.name11)))

    stack: List[Tuple[Node, List[int]]] = []
    walk_entries(root_node, root_vis)
    del root_vis
    visits = 0
    while stack:
        node, chain = stack.pop()
        visits += 1
        if visits > vol.nclusters + 2:
            rep.add_problem(lambda: "フォルダが多すぎる、または循環しています")
            break
        d = _read_dir_info(vol, node, chain)
        rep.dirs.append(d)
        rep.visible[d.key] = records(d.visible)
        for name11, c in d.dots:
            want = node.cluster if name11 == ldhdd.DOT_NAME else node.parent.cluster
            if c != want:
                dot_bad.append((d, name11, c))
        d.dots = []
        walk_entries(node, d.visible)
        if not d.tail or d.empty_tail:
            d.visible = []  # 直す候補でなければ、もう要らない
            d.long_names = set()

    # FAT のつなぎの逆引き (どのクラスタから指されているか)
    incoming: Dict[int, List[int]] = {}
    for c in range(2, vol.usable_max + 1):
        v = vol.get(c)
        if 2 <= v <= vol.usable_max:
            incoming.setdefault(v, []).append(c)

    # 2. 隠れた項目 (形が合うフォルダだけ、移せるか見る)
    for d in rep.tail_dirs:
        if not d.shape_ok:
            continue
        names = {s.name11 for s in d.visible}
        texts = {name_text(s.name11).casefold() for s in d.visible}  # 表示した名前 (CP932 で同じに見える物も重なりとする)
        for s in d.hidden:
            why = _hidden_reason(s, names, d.long_names, texts)
            if why:
                d.hidden_bad.append((s, (lambda w=why: w)))
                continue
            if s.cluster:
                who = (d.node, None, " (隠れた項目 %s)" % show(s.name11))
                # たどれた鎖は、移せるかに関わらずこの項目の物として登録する。同じ鎖を指すほかの隠れた項目は、
                # 頭で「共有」として止まり、同じ鎖を何度もたどらない
                chain, why2 = follow(s.cluster, who, report=False)
                if chain is None:
                    d.hidden_bad.append((s, why2))
                    continue
                why = _hidden_chain_reason(vol, s, chain, incoming)
                if why:
                    d.hidden_bad.append((s, (lambda w=why: w)))
                    continue
            names.add(s.name11)
            texts.add(name_text(s.name11).casefold())
    for d in rep.dirs:
        if d.empty_tail:
            rep.add_note(lambda: "%s: 終わりの印の後ろに、全部 0 のクラスタが %d 個つながっています。LSX-Dodgers の誤りの印"
                             "(隠れた項目) が無いので、そのままにします" % (d.path, len(d.tail)))
        elif d.after_count and not d.tail:
            rep.add_note(lambda: "%s: 終わりの印の後ろに %d 個の項目の跡があります (LSX からは見えず、そのままにします)"
                             % (d.path, d.after_count))

    # 3. 外からフォルダの鎖へのつなぎ
    for d in rep.tail_dirs:
        # 直すとこの鎖の途中に終わりの印を書くので、鎖のどのクラスタへでも、外からのつなぎがあれば直さない
        prev = {b: a for a, b in zip(d.chain, d.chain[1:])}
        for t in d.chain:
            for src in incoming.get(t, []):
                if src != prev.get(t):
                    d.incoming.append((src, t))

    # 3b. 自分・親と合わない . / .. (壊れたフォルダ)。空きに戻す予定のクラスタを指していれば、そのフォルダは直さない
    if dot_bad:
        freeing: Dict[int, DirInfo] = {}
        for d in rep.tail_dirs:
            for t in d.tail:
                freeing[t] = d
        for d, name11, c in dot_bad:
            rep.add_note(lambda: "%s の %s が、%s ではなくクラスタ %d を指しています"
                         % (d.path, name11.decode("ascii").strip(), "自分" if name11 == ldhdd.DOT_NAME else "親", c))
            if c in freeing:
                freeing[c].dot_refs.append((d, name11, c))

    # 4. どこからも使われていない使用中のクラスタ
    bad = vol.eoc - 8
    rep.lost = [c for c in range(2, vol.usable_max + 1) if vol.get(c) not in (0, bad) and c not in owner]
    lost_set = set(rep.lost)
    pointed = {v for c in rep.lost for v in [vol.get(c)] if v in lost_set}
    rep.lost_heads = [c for c in rep.lost if c not in pointed]
    # どこからも使われていない鎖も、1 回ずつたどって確かめる。終わりの印で終わる物は知らせるだけ。
    # 空き・範囲外・使用中のクラスタを指す物、循環・合流する物は壊れ方 (直さない)
    into: Dict[int, int] = {}
    for c in rep.lost:
        v = vol.get(c)
        if vol.is_eoc(v):
            continue
        if not 2 <= v <= vol.usable_max:
            rep.add_problem(lambda: "どこからも使われていない鎖に範囲外の値があります (クラスタ %d: %Xh)" % (c, v))
        elif vol.get(v) == 0:
            rep.add_problem(lambda: "どこからも使われていない鎖が途中で切れています (クラスタ %d が空きのクラスタ %d を指しています)" % (c, v))
        elif v not in lost_set:
            rep.add_problem(lambda: "どこからも使われていない鎖が、使用中のクラスタ %d を指しています (クラスタ %d から)" % (v, c))
        else:
            into[v] = into.get(v, 0) + 1
            if into[v] == 2:
                rep.add_problem(lambda: "どこからも使われていない鎖どうしが、クラスタ %d で合流しています" % v)
    state: Dict[int, int] = {}  # 1 = たどっている途中, 2 = 済み
    for c in rep.lost:
        walk = []
        x = c
        while x in lost_set and x not in state:
            state[x] = 1
            walk.append(x)
            v = vol.get(x)
            x = v if 2 <= v <= vol.usable_max else -1
        if x in state and state[x] == 1:
            rep.add_problem(lambda: "どこからも使われていない鎖が循環しています (クラスタ %d)" % x)
        for w in walk:
            state[w] = 2
    return rep


def _read_dir_info(vol, node: Node, chain: List[int]) -> DirInfo:
    """フォルダの鎖を 1 クラスタずつ読む (長い鎖でも、残すのは見える項目と最後のクラスタの候補だけ)"""
    d = DirInfo(node, chain)
    d.data_off, d.csize = vol.data_off, vol.csize
    per = vol.csize // 32
    zero = bytes(vol.csize)
    lfn: List[Slot] = []  # 直前に続いている長い名前の項目
    marker_rest_zero = True
    nonzero_tail: Optional[int] = None  # 最後でない余分なクラスタで、0 でない最初の物
    last_raw = b""
    for ci, c in enumerate(chain):
        off0 = vol.cluster_off(c)
        raw = _read_at(vol.f, off0, vol.csize)
        if len(raw) != vol.csize:
            raise LdhddError("%s: クラスタ %d を読めません" % (d.path, c))
        if d.marker is None:
            for si in range(per):
                s = Slot(off0 + si * 32, raw[si * 32:si * 32 + 32])
                b0 = s.raw[0]
                if b0 == 0:
                    d.marker = (ci, si)
                    d.detached_lfn = list(lfn)
                    marker_rest_zero = not any(raw[si * 32:])
                    d.after_count += sum(1 for k in range(si + 1, per) if raw[k * 32] not in (0x00, 0xE5))
                    break
                if b0 == 0xE5:
                    lfn = []
                    if d.first_e5 is None:
                        d.first_e5 = ci
                elif s.attr == 0x0F:
                    # 並びの正しい長い名前の項目だけを持つ (40h の付いた項目から、番号が 1 つずつ減る。番号は 1Fh まで)。
                    # 崩れた並びは捨てるので、長く壊れたフォルダでも持つ数は 31 個まで
                    if b0 & 0x40:
                        lfn = [s]
                    elif lfn and (lfn[-1].raw[0] & 0x1F) == (b0 & 0x1F) + 1:
                        lfn.append(s)
                    else:
                        lfn = []
                else:
                    if s.name11 in (ldhdd.DOT_NAME, ldhdd.DOTDOT_NAME) and s.attr & 0x10:
                        d.dots.append((s.name11, s.cluster))
                    elif _is_entry(s):
                        d.visible.append(s)
                        ln = ldhdd._long_name([x.raw for x in lfn], s.name11) if lfn else None
                        if ln:
                            d.long_names.add(ln.casefold())
                    lfn = []
            continue
        d.after_count += sum(1 for k in range(per) if raw[k * 32] not in (0x00, 0xE5))
        if ci == len(chain) - 1:
            last_raw = raw
        elif nonzero_tail is None and raw != zero:
            nonzero_tail = c
    if not d.tail:
        return d
    if nonzero_tail is None and not any(last_raw):
        # 余分なクラスタが全部 0: COPEN は必ず項目を書くので、LSX-Dodgers の誤りの証拠が無い (注として知らせるだけ)
        d.empty_tail = True
        return d
    # 決まり 2: 終わりの印のあるクラスタの、印から後ろが全部 0
    if not marker_rest_zero:
        d.shape_reason = "終わりの印のあるクラスタの、印より後ろが 0 ではありません"
        return d
    # 決まり 3: 最後でない余分なクラスタは全部 0
    if nonzero_tail is not None:
        d.shape_reason = "余分なクラスタ %d が 0 ではありません" % nonzero_tail
        return d
    # 決まり 4: 最後の余分なクラスタは、0 番から続けて k 個 (1 個以上) の項目、その後ろは全部 0
    k = 0
    while k < per and last_raw[k * 32] != 0:
        k += 1
    if any(last_raw[k * 32:]):
        d.shape_reason = "最後の余分なクラスタ %d の項目の後ろが 0 ではありません" % chain[-1]
        return d
    # 決まり 5: COPEN はいっぱいのフォルダ (1 クラスタ以上) に、ちょうど 256 クラスタ足す。
    # 足す前のクラスタには空きが無いので、終わりの印は足した 256 クラスタの中にある
    mi = d.marker[0]
    if len(chain) < 1 + GROWTH or mi < len(chain) - GROWTH:
        d.shape_reason = ("鎖の長さ (%d クラスタ) と終わりの印の位置 (%d 番目のクラスタ) が、LSX-Dodgers の誤り "
                          "(いっぱいのフォルダに %d クラスタ足す) と合いません" % (len(chain), mi + 1, GROWTH))
        return d
    d.added = set(chain[max(0, len(chain) - GROWTH):mi + 1])
    # 決まり 6: COPEN は空き (00h・E5h) が 1 つも無い時にだけ足すので、足す前のクラスタに E5h があれば
    # (前もって伸ばした鎖の後で消した形と) 見分けられない
    if d.first_e5 is not None and d.first_e5 < len(chain) - GROWTH:
        d.shape_reason = ("足す前と思われるクラスタ (鎖の %d 番目) に消した項目の跡 (E5h) があり、LSX-Dodgers の誤りと"
                          "見分けられません" % (d.first_e5 + 1))
        return d
    last_off = vol.cluster_off(chain[-1])
    d.hidden = [Slot(last_off + i * 32, last_raw[i * 32:i * 32 + 32]) for i in range(k)]
    d.shape_ok = True
    return d


def _hidden_reason(s: Slot, names: Set[bytes], long_names: Set[str], texts: Set[str]) -> Optional[str]:
    """隠れた項目を移せない理由 (名前・属性・サイズ。鎖は _hidden_chain_reason で見る)。移せるなら None"""
    if s.attr == 0x0F or s.attr & 0x08:
        return "長い名前かボリュームラベルの項目です"
    if s.attr & 0x10:
        return "フォルダの項目は移しません"
    if s.attr & 0xC0:
        return "属性 %02Xh がおかしい" % s.attr
    if s.raw[0] == 0xE5 or not valid_name(s.name11):
        return "名前が 8.3 として正しくありません"
    if s.name11 in names:
        return "同じ名前の項目がフォルダにあります"
    if name_text(s.name11).casefold() in long_names:
        return "フォルダの項目の長い名前と重なります"
    if name_text(s.name11).casefold() in texts:
        return "フォルダの項目と、表示した名前が同じになります"
    if s.cluster == 0:
        return "サイズがあるのにクラスタがありません" if s.size else None
    if not s.size:
        return "サイズ 0 なのにクラスタがあります"
    return None


def _hidden_chain_reason(vol, s: Slot, chain: List[int], incoming: Dict[int, List[int]]) -> Optional[str]:
    """隠れた項目のデータの鎖 (たどれて、ほかと重ならない物) が、サイズと合い、外から指されていないか"""
    if len(chain) * vol.csize < s.size or (len(chain) - 1) * vol.csize >= s.size:
        return "鎖の長さ (%d クラスタ) がサイズ %d バイトと合いません" % (len(chain), s.size)
    prev = {b: a for a, b in zip(chain, chain[1:])}
    for c in chain:
        for src in incoming.get(c, []):
            if src != prev.get(c):
                return "外のクラスタ %d が、このファイルの鎖のクラスタ %d を指しています" % (src, c)
    return None

# ---------------------------------------------------------------------------
# 直す計画

class Plan(NamedTuple):
    entry_writes: List[Tuple[int, bytes]]  # (位置, 書く物)
    fat: bytes  # 直した後の FAT1
    freed: List[int]
    lines: List[str]
    visible: Dict[int, bytes]  # 直した後の見える項目 (フォルダの先頭クラスタ → record を並べた物)
    lost: List[int]
    slots: Dict[int, bytes]  # 書いた所の、直した後の 32 バイト (後から書く物が勝つ)


def make_plan(vol, rep: Report) -> Plan:
    blocking = rep.blocking  # 1 回だけ作る
    if blocking:
        raise LdhddError("直せない所があるので、何も書きません:\n  " + "\n  ".join(blocking))
    per = vol.csize // 32
    fat_save = bytes(vol.fat)
    writes: List[Tuple[int, bytes]] = []
    freed: List[int] = []
    lines: List[str] = []
    visible = dict(rep.visible)
    try:
        for d in rep.tail_dirs:
            mi, ms = d.marker
            kept = list(d.kept)
            tail = list(d.tail)
            room = per - ms  # 終わりの印のあるクラスタの、印から後ろの項目の数
            k = len(d.hidden)
            pos = [vol.cluster_off(kept[-1]) + (ms + i) * 32 for i in range(min(k, room))]
            if k and room < k + 1:
                # 「移す項目 k 個 + 終わりの印 1 個」が入らない: 余分なクラスタの最初の 1 つも残し、0 で埋め直して続きを置く
                extra = tail.pop(0)
                kept.append(extra)
                rest = k - len(pos)
                if rest + 1 > per:
                    raise LdhddError("%s: 移す項目が入りきりません" % d.path)
                writes.append((vol.cluster_off(extra), bytes(vol.csize)))
                pos += [vol.cluster_off(extra) + i * 32 for i in range(rest)]
            if d.hidden:
                # 終わりの印の直前の、組になっていない長い名前の項目は、移した項目に付いてしまうので消した印にする
                for s in d.detached_lfn:
                    writes.append((s.off, b"\xe5" + s.raw[1:]))
            for p, s in zip(pos, d.hidden):
                writes.append((p, s.raw))
            vol.set(kept[-1], vol.eoc)
            for t in tail:
                vol.set(t, 0)
            freed += tail
            visible[d.key] = visible[d.key] + records(d.hidden)
            lines.append("%s: 鎖を %d クラスタから %d クラスタにし、%d クラスタを空きに戻す (%s)。移す項目: %s%s"
                         % (d.path, len(d.chain), len(kept), len(tail), ranges(sorted(tail)),
                            ", ".join(show(s.name11) for s in d.hidden),
                            "。組になっていない長い名前の項目 %d 個を消した印にする" % len(d.detached_lfn)
                            if d.detached_lfn else ""))
        fat = bytes(vol.fat)
    finally:
        vol.fat[:] = fat_save
    slots: Dict[int, bytes] = {}
    for off, data in writes:
        for i in range(0, len(data), 32):
            slots[off + i] = data[i:i + 32]
    return Plan(writes, fat, freed, lines, visible, list(rep.lost), slots)


def apply_plan(f, vol, plan: Plan) -> None:
    for off, data in plan.entry_writes:
        f.seek(off)
        ldhdd._write_all(f, data)
    os.fsync(f.fileno())
    for k in range(vol.nfats):
        f.seek(vol.fat_off + k * vol.fat_bytes)
        ldhdd._write_all(f, plan.fat)
    os.fsync(f.fileno())


def verify(f, plan: Plan) -> None:
    """直した写しを読み直して、計画どおりの姿かを確かめる (フォルダは 1 クラスタずつ読む)"""
    vol = ldhdd.FatVolume(f)
    for off, want in sorted(plan.slots.items()):
        if _read_at(f, off, 32) != want:
            raise LdhddError("直した写しのディレクトリ項目 (位置 %d) が計画と違います" % off)
    for k in range(vol.nfats):
        if _read_at(f, vol.fat_off + k * vol.fat_bytes, vol.fat_bytes) != plan.fat:
            raise LdhddError("直した写しの FAT%d が計画と違います" % (k + 1))
    rep = scan(vol)
    tails = rep.tail_dirs
    if rep.problems or tails or not rep.mirror_ok:
        what = rep.problems[:LIMIT] + [d.path for d in tails[:LIMIT]]  # 道筋の文は数を限って作る
        if rep.more_problems or len(tails) > LIMIT:
            what.append("ほか %d 件" % (rep.more_problems + max(0, len(tails) - LIMIT)))
        if not rep.mirror_ok:
            what.append(rep.mirror)
        raise LdhddError("直した写しにまだ問題があります: %s" % "; ".join(what))
    if rep.visible != plan.visible:
        keys = sorted(k for k in set(plan.visible) | set(rep.visible) if plan.visible.get(k) != rep.visible.get(k))
        by_key = {d.key: d for d in rep.dirs}
        shown = ["/" if k == 0 else (by_key[k].path if k in by_key else "クラスタ %d" % k) for k in keys[:LIMIT]]
        if len(keys) > LIMIT:
            shown.append("ほか %d 個" % (len(keys) - LIMIT))
        raise LdhddError("直した写しの見える項目が計画と違います (%s)" % ", ".join(shown))
    if rep.lost != plan.lost:
        raise LdhddError("直した写しの、どこからも使われていないクラスタが変わりました")


# ---------------------------------------------------------------------------
# 出力

def print_report(rep: Report, verbose: bool, out) -> None:
    shown_reasons = 0  # 移せない理由の文 (重なる相手の道筋を含む) を出した数。LIMIT を越えた分は省く
    omitted_reasons = 0
    print("FAT の写し: %s" % rep.mirror, file=out)
    if not rep.mirror_ok:
        print("  (LSX-Dodgers の HDD.COM は FAT1 だけを書くので、X1 で書いた後はこうなります。FAT1 を正として読むので、"
              "このままでも使えます。ldhdd.py add で書く時や、ldfsck.py fix でフォルダを直す時にそろいます)", file=out)
    for d in rep.dirs:
        if not d.tail and not verbose:
            continue
        where = ("%d 番目のクラスタの %d 番" % (d.marker[0] + 1, d.marker[1])) if d.marker else "無し (鎖の終わりまで項目)"
        print("%s: 鎖 %d クラスタ (%s)、終わりの印 %s" % (d.path, len(d.chain), ranges(d.chain), where), file=out)
        if not d.tail:
            continue
        print("  余分なクラスタ %d 個 (%s)" % (len(d.tail), ranges(d.tail)), file=out)
        bad = dict((s.off, why) for s, why in d.hidden_bad)  # 理由の文を作る関数
        for s in d.hidden:
            if s.off not in bad:
                result = "移せます"
            elif shown_reasons < LIMIT:
                shown_reasons += 1
                result = "移せません: " + bad[s.off]()
            else:
                omitted_reasons += 1
                result = "移せません (理由の表示は省きます)"
            print("  隠れた項目: %-12s クラスタ %d、%d バイト → %s" % (show(s.name11), s.cluster, s.size, result), file=out)
        if not d.shape_ok:
            print("  形が違う: %s" % d.shape_reason, file=out)
        for src, dst in d.incoming:
            print("  外のクラスタ %d が、このフォルダの鎖のクラスタ %d を指しています" % (src, dst), file=out)
        print("  → %s" % ("直せます (LSX-Dodgers のフォルダの誤りの形)" if d.repairable else "直せません"), file=out)
        if verbose:
            # 足されたクラスタ (鎖の最後の 256 クラスタのうち、終わりの印のクラスタまで) の見える項目は、
            # 使い込んだディスクでは古い中身の場合がある (本物と見分けられない)
            for s in d.visible:
                if (s.off - d.data_off) // d.csize + 2 in d.added:
                    print("  確かめてください: %s は、足されたクラスタにある見える項目です (古い中身でないか)"
                          % show(s.name11), file=out)
    if omitted_reasons:
        print("注: 移せない隠れた項目の理由は %d 件まで出し、ほか %d 件は省きました" % (LIMIT, omitted_reasons), file=out)
    for p in rep.problems:
        print("壊れ方: %s" % p, file=out)
    if rep.more_problems:
        print("壊れ方: ほか %d 件" % rep.more_problems, file=out)
    for n in rep.notes:
        print("注: %s" % n, file=out)
    if rep.more_notes:
        print("注: ほか %d 件" % rep.more_notes, file=out)
    if rep.lost:
        print("どこからも使われていないクラスタ: %d 個 (鎖の頭: %s)" % (len(rep.lost), ranges(rep.lost_heads[:20])
                                                         + (" ほか" if len(rep.lost_heads) > 20 else "")), file=out)
    fixable = [d for d in rep.tail_dirs if d.repairable]
    print("まとめ: 直せるフォルダ %d 個、直せないフォルダ %d 個、壊れ方 %d 件、使われていないクラスタ %d 個"
          % (len(fixable), len(rep.tail_dirs) - len(fixable), len(rep.problems) + rep.more_problems, len(rep.lost)), file=out)


def check(path: str, verbose: bool = False, out=None) -> int:
    if out is None:
        out = sys.stdout
    with ldhdd._open_target(path, False) as f:
        unlock = _lock_read(f)  # 書き手 (ldhdd) の排他のロックと両立しない共有のロック。FAT と項目を同じ時点で読む
        try:
            vol = ldhdd.FatVolume(f)
            rep = scan(vol)
        finally:
            unlock()
        print("対象: %s" % path, file=out)
        print_report(rep, verbose, out)
    if rep.clean:
        print("問題はありません", file=out)
        return 0
    return 2


def _lock_read(f):
    """読むだけの時のロック。POSIX は共有の flock (読み込みで開いたファイルでも取れる。書き手の排他の
    ロックとは両立しない)。Windows は ldhdd と同じ範囲ロック"""
    try:
        import fcntl
    except ImportError:
        return ldhdd._lock(f)
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        raise LdhddError("ほかのプロセスがこのファイルを使っています")
    return lambda: fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _copy_source(src_f, fd: int) -> None:
    """元を仮の写しへ写す (試験で差し替える所)"""
    src_f.seek(0)
    ldhdd._copy_to_fd(src_f, fd)


def fix(src: str, out_path: str, dry_run: bool = False, out=None) -> int:
    if out is None:
        out = sys.stdout
    if os.path.lexists(out_path):
        raise LdhddError("出力が既にあります: %s" % out_path)  # 元と同じ名前もここで止まる
    # abspath は「..」を文字の上だけでたたむので使わない。仮の写し・同期・名前付けは、--out と同じたどり方の所で行う
    d = ldhdd._backup_dir(out_path)
    if dry_run:
        # 写しを作らず、元を読むだけで計画を出す (ロックを取って、ldhdd・ldfsck どうしの同時の書き込みを避ける)
        with ldhdd._open_target(src, False) as sf:
            unlock = _lock_read(sf)
            try:
                vol = ldhdd.FatVolume(sf)
                rep = scan(vol)
                print("対象: %s" % src, file=out)
                print_report(rep, False, out)
                plan = make_plan(vol, rep)
            finally:
                unlock()
        if not plan.lines:
            print("直す所はありません", file=out)
            return 0
        for line in plan.lines:
            print("直す: %s" % line, file=out)
        if not rep.mirror_ok:
            print("直す: FAT の全部の写しを FAT1 にそろえる", file=out)
        print("--dry-run なので書き込みません (写しも作りません)", file=out)
        return 0
    tmp = None
    f = None
    published = False
    try:
        with ldhdd._open_target(src, False) as sf:
            unlock = _lock_read(sf)
            try:
                fd, tmp = ldhdd._create_staging(d)
                # 作った仮の写しは、名前で開き直さずに、同じファイルのまま直して確かめる
                f = os.fdopen(fd, "r+b", buffering=0)
                _copy_source(sf, fd)
            finally:
                unlock()
        # ここから先は写しだけを見る (元は読まない)
        vol = ldhdd.FatVolume(f)
        rep = scan(vol)
        print("対象: %s" % src, file=out)
        print_report(rep, False, out)
        plan = make_plan(vol, rep)
        if not plan.lines:
            if rep.lost:
                print("直す所はありません (どこからも使われていないクラスタは、そのまま残します)", file=out)
            else:
                print("直す所はありません", file=out)
            return 0
        for line in plan.lines:
            print("直す: %s" % line, file=out)
        if not rep.mirror_ok:
            print("直す: FAT の全部の写しを FAT1 にそろえる", file=out)
        apply_plan(f, vol, plan)
        verify(f, plan)
        f.close()
        f = None
        try:
            leftover = ldhdd._publish_no_replace(tmp, out_path)
        except OSError as e:
            if e.errno in NO_LINK_ERRNOS:
                raise LdhddError("この置き場 (%s) では、既にある物を置き換えずに名前を付けられません (ハードリンクが使えない"
                                 "ファイルシステム。Linux や macOS から見た FAT の USB メモリなど)。--out には、ふつうのフォルダ"
                                 "など別の置き場を指定してください。元の VHD は書き換えていません" % d)
            raise
        published = True
        try:
            ldhdd._fsync_dir(d)
        except OSError as e:
            print("直した写しを書きました: %s (元は書き換えていません)。ただし置き場のディスクへの同期を確かめられませんでした (%s)"
                  % (out_path, e), file=out)
            return 0
        if leftover:
            print("注意: 仮のファイル %s を消せませんでした" % leftover, file=out)
        print("直した写しを書きました: %s (元は書き換えていません)" % out_path, file=out)
        return 0
    finally:
        if f is not None:
            f.close()
        if tmp is not None and not published:
            try:
                os.remove(tmp)
            except OSError:
                pass


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ldfsck.py", description="LSX-Dodgers のフォルダの鎖の誤りを見付ける・直す")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("check", help="読むだけ。フォルダの鎖の様子と FAT の誤りを出す")
    p.add_argument("vhd")
    p.add_argument("-v", "--verbose", action="store_true", help="問題の無いフォルダも出す")
    p = sub.add_parser("fix", help="元は書き換えず、新しい写しに直す")
    p.add_argument("vhd")
    p.add_argument("--out", required=True, help="直した写しの名前 (既にあれば止まる)")
    p.add_argument("--dry-run", action="store_true", help="直す内容を出すだけ")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        if a.cmd == "check":
            return check(a.vhd, a.verbose)
        if a.cmd == "fix":
            return fix(a.vhd, a.out, a.dry_run)
        ap.print_help()
        return 1
    except (LdhddError, OSError) as e:
        print("エラー: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
