# LSX-DodgersをX1のHDDから起動させる

`ldhdd.py` 1本で、既存のVHDファイルを「LSX-DodgersがHDDから起動するイメージ」にできます。エミュレーター、ndc、AILZ80ASMは要りません。

## 必要な物

- Python 3.8以上（追加のパッケージは不要です）
- LSX-DodgersのX1用システムディスク `ldsys.d88`
  - [tablacus/LSX-Dodgers の Release](https://github.com/tablacus/LSX-Dodgers/releases) のzip（例: `dsys_162c.zip`）に入っています
  - この中の `LD.BIN`（LSX-Dodgers本体）と `HDD.COM`（LD-hdd）を使います
- 起動させたいVHDファイル
  - **固定サイズのVHD**で、**MBRで区切って第1パーティションをFATでフォーマットした物**が必要です（可変VHDは使えません）
  - 作り方は[こちら（WindowsでのVHDファイルの作り方）](https://github.com/tablacus/LSX-Dodgers/issues/4)を参考にしてください。容量は512MBまでです
  - フッターの無い生のイメージ（.hddなど）も、MBRとFATがあれば使えます

## 使い方

### 既存のVHDを起動できるようにする / 起動するLSX-Dodgersを更新する

```
python3 ldhdd.py update LDHDD.vhd --ldsys ldsys.d88
```

- VHDの先頭（パーティションより前の空き）に、IPL・ローダー・LSX-Dodgers・HDD.COMを書き込みます。パーティションの中身（ファイル）は変わりません。
- 書き込む前に `LDHDD.vhd.<日時>.bak` としてVHD全体のバックアップを取ります（`--no-backup` で省略）。エミュレーターなどでVHDを開いたままにしないでください。
  - バックアップは、仮の名前で書いてディスクに同期してから正式な名前を付け、既にある同じ名前のファイルは置き換えません。バックアップが作れなかった時は、VHDには何も書きません。
  - 守っているのは「ふつうに使っていて壊さないこと」までです。`update` の実行中に、別のプログラムが同時に同じVHDやバックアップの置き場所（ディレクトリやシンボリックリンク）を書き換える場合は、このツールの対象外です。
- 書く前に次のことを確かめ、合わなければ何も書かずに止まります。
  - 先頭にMBRがあること（MBRの無いFATに書くと壊れるため）
  - どのパーティションも、起動部を書く範囲（先頭約12KB）に重ならないこと
  - 第1パーティションの開始が16MB未満で、そこにFAT12/16のBPBがあること（FAT32は使えません）
  - 固定VHDであること
  - 起動部を書く範囲に、前の起動部以外のデータが無いこと（あれば `--force` を付けた時だけ書きます）
    - 「前の起動部」として扱うのは、`ldhdd.py` が書いた物（起動セクタの0x190〜に長さとsha256の印を書きます）と、このリポジトリの過去の `MKLDHD.d88` / `MKLDHD1.d88` で作った物だけです。それ以外の方法で作った起動イメージを初めて更新するときは、内容を確かめた上で `--force` を付けてください
- `--dry-run` を付けると、確かめて表示するだけで書きません。
- 既に起動できるVHDに対して実行すると、起動するLSX-Dodgers（とHDD.COM）だけが新しくなります。

### 中身を確かめる

```
python3 ldhdd.py info LDHDD.vhd
```

起動部の有無、中のLSX-Dodgersの版、HDD.COM、パーティションを表示します。

### その他のオプション

- `--ld-bin LD.BIN` : LSX-Dodgersの `LD.BIN` を直接指定します（`--ldsys` の物より優先）。
- `--hdd-com HDD.COM` : 起動時に走らせるCOMを指定します（既定は `--ldsys` の中の `HDD.COM`）。
- `--unit 1` : 起動時にHDD.COMへHDD番号1を渡し、HDD1をH:に割り当てます（HDD.COMは書き換えません）。**試験中**です。うまく行かない場合は、従来どおり `MKLDHD1.d88` の中の `HDD1.COM` を `--hdd-com` で指定してください。
- `python3 ldhdd.py sys -o LD.BIN --ldsys ldsys.d88` : MAKESYSと同じ「LSX-Dodgers + HDD.COM」の合体版LD.BINだけを作ります（確認用）。

## 仕組み（なぜこういう作りなのか）

1. X1のIPL ROMは、HDDの先頭32バイトをIPL用のFCBとして読み、HDDの先頭0xF000バイトをメモリの0x0000から読み込んで0x0021へ飛びます。
2. 0x0021のローダー（`ldhdd_boot.asm`）は、HDDの0x0200に置いたLD.BINを本来の番地へ移して実行するだけです。
3. このLD.BINは、LSX-Dodgers付属の `MAKESYS` で作るのと同じ「LSX-DodgersのLD.BIN + 外部コマンド1つ（HDD.COM）」を合体させた物です。LSX-Dodgersが起動すると、まずHDD.COMが走ってHDDがH:に割り当てられ、続けてAUTOEXECが実行されます。
4. MBRとパーティションの間の空き（Windowsで作ったVHDなら64KBなど）に、この起動部（約12KB）を書いています。HDD.COMは、HDDの先頭がBPBでなければMBRの第1パーティションを見に行きます。

以前は、MAKESYSがX1用のプログラムなのでエミュレーターの中で動かし、出来たLD.BINをndcで取り出し、パーティション表とLD.BINをアセンブラで組み込む、という手順でした（下の「旧手順」）。`ldhdd.py` は、MAKESYSとローダーの組み立てをPythonで同じバイト列になるように行います。

`ldhdd_boot.asm` はローダーの元の資料として残しています（コメントにある「GRAM FAT image」や0xF000の読み込みサイズは、[元になった方式](https://github.com/tablacus/LSX-Dodgers/issues/9)の名残です）。

### 補足: MAKESYSのドライブ指定について

旧手順の `MKLDHD.BAT` は `MAKESYS B:HDD.COM B:LD.BIN H:` と最後に `H:` を指定しています。LSX-Dodgers 1.55で作っていた頃の起動イメージでは、この指定が効いて起動後にH:へ切り替わっていました。ところがLSX-Dodgers 1.61でファイル名解析（`_PFILE`）の仕様が変わったため、1.61以降で作った起動イメージではこの指定が効いていません（起動後のカレントドライブは変わりません）。`ldhdd.py` は今の起動イメージと同じになるよう、この挙動をそのまま再現しています。

## テスト

```
python3 -m unittest discover -s ld-hddboot/tests
```

環境変数 `LDHDD_LDSYS` に `ldsys.d88` のパスを入れると、実物のシステムディスクでの確認も行います（版は問いません。LD.BINとHDD.COMが読めること、合体版を作って分解すると元に戻ることを確かめます）。

---

## 旧手順（参考）

`ldhdd.py` を使わない、以前の手順です。

### 任意のサイズのVHDファイルを作成する
[こちらのVHDの作り方のページ](https://github.com/tablacus/LSX-Dodgers/issues/4) を参考に、任意のサイズのVHDファイルを作成します。
VHDファイルは「HDDBASE.VHD」として当フォルダに保存します。これが、HDDブートさせるHDDの元データになります。

※当方では128MBのVHDファイルで確認しています。

### HDD.COMを含むLD.BINをエミュレータのディスクイメージ内に作成する

※当フォルダには、LSX-Dodgers 1.61b + LD-hdd(hdd.com) 0.14のディスクイメージが既に入っていますので、そのバージョンで良い場合はこの項目は飛ばしてください。LSX-Dodgersまたはhdd.com(LD-hdd)を更新したい場合のみ、こちらの手順を行ってください。

最新のLSX-DodgersをエミュレーターのAドライブ、当フォルダにある「MKLDHD.d88」をBドライブに入れて起動します(ディスクイメージファイルのコピーなどはせず、当フォルダのファイルをそのままマウントしてください)。
MKLDHD.d88には LD-hdd (hdd.com) のv0.14が入っていますので、もし更新したい場合は、事前にhdd.comの更新を行ってください。

続いてエミュレーター上で下記のように入力します(Bドライブに移動してからMKLDHD.BATを実行)。

```
B:
MKLDHD
```

これにより、BドライブにLD.BINが出力されますので、出力された事を確認してからエミュレーターを終了してください。

### 作成バッチ(スクリプト)を実行する
Windowsの場合はmkldhdd.bat、それ以外の場合はmkldhdd.shを実行すると、内部で下記の手順を実行し、LSX-Dodgersの起動HDDイメージが作成されます。

1. MKLDHD.d88の中からLD.BINを抜き出す
2. ldhdd_boot.asmをアセンブルして、IPL部分のバイナリ(ldhdd_boot.bin)を作成する
3. HDDBASE.vhdとldhdd_boot.binを混ぜて、LDHDD.vhdを作成する

3で出力されたLDHDD.vhdが、起動可能なHDDイメージになります。

HDDイメージ内には1ファイルも入っていない状態ですので、任意の方法でお好きなファイルを書き込んでお使いください。

※作成バッチの実行には、[ndc](https://euee.web.fc2.com/tool/tool.html)、[AILZ80ASM](https://github.com/AILight/AILZ80ASM)、pythonが必要です。それぞれにパスを通すか、バッチのあるフォルダにndcとAILZ80ASMの実行ファイルを置いてからバッチを実行してください(バッチファイルの冒頭のNDCPATH、AILZPATH、PYTHONPATHに、それぞれの実行ファイルのフルパスを記載しても問題ありません)。

### HDD1から起動させる方法
デフォルトではHDD0から起動を行いますが、 当フォルダに含まれるMKLDHD1.d88 を MKLDHD.d88 にリネームして使用する事で、HDD1から起動するHDD起動イメージを作成出来ます(元のMKLDHD.d88は、MKLDHD0.d88など、テキトーな名前にリネームしておいてください)。

MKLDHD1.d88 の中には、通常のLD-hddとは別の、実行するとHDD1を自動でマウントするHDD1.COMというコマンドが含まれており、MKLDHD1.d88の中のLD.BINについては、こちらを適用した、HDD1を自動マウントするLSX-Dodgersが入っています。

### 既存のHDDイメージに含まれるHDブート用のLSX-Dodgersを更新したい場合
現在お使いのHDDイメージをコピーした上で「HDDBASE.vhd」にリネームし、当フォルダに置いた上で、「作成バッチ(スクリプト)を実行する」の項目を実行してください。HDDの中身は変わらず、起動するLSX-Dodgersのみバージョンアップ出来ます。

基本的には問題ないとは思いますが、最悪の場合はHDDイメージを破壊しますので、必ずバックアップをとってから行ってください。
