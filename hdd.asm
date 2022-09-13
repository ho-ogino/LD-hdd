;----------------------------------------------------------------------------
; X1/turbo LSX-Dodgers SASI Access Program
; usage
;   hdd [hdd drive] [offset] [target drive]
;     hdd drive: 0 - 3(HD0～HD3)
;     offset: 2MBごとのインデックス(1で2MBの位置、2で4MBの位置を先頭として処理する。VHDの場合は0固定にする事)
;     tareget drive: A-G 割り当てるドライブ
;
;note:
; LSX-DodgersにてHDDをドライブに割り当て、読み書きするための常駐プログラムです。
;
; 0x100から読み込まれるCOMファイルとしてバイナリ出力する前提で作っています。
; 
; C900からSASIドライブ読み書き処理が配置され、TPAの末尾(0x0006)をC900に動かし、
; C900にはJP 0xCC06(元々0x0006に書かれていたアドレス(LSX-Dodgersのシステム領域開始アドレス)) を書く事で強引に常駐させています。
; 正しい方法を知りたいところです……(リロケータブルにするのしんどいので固定にしてるのがダメ？)。
;
; 既存のDPB(ドライブパラメータブロック)をHDD用に上書きし、
; このプログラム実行後、任意の指定ドライブをHDDとしてアクセス出来るようになります。
;
; HDD用のDPBについては、VHD形式のファイルからBPB(BIOS Parameter Block)情報を読み込んで適切な値を設定します。
; BPBが見つからない場合は、デフォルト設定として2MBおきにHDDを区切って使う状態でDPBが設定されます。
; 
; 現時点で、正常に全領域アクセス出来るか確認できていません。
; あくまで現状、人柱版となります。
;

;----------------------------------------------------------------------------
; デフォルトBPB情報(BPB情報が存在する場合は上書きされるが、BPBが読めない場合はこの値が使われる)
;----------------------------------------------------------------------------
; VHD(15MB)
; SCTSIZ		equ	512			; 1セクタのサイズ
; CSTSEC		equ	8			; 1クラスタのセクタ数(512*8なので4096バイト)
; RSVSEC		equ	8			; 予備領域のセクタ数
; NUMFAT		equ	2			; FATの数
; ROOTCNT		equ	512			; ルートエントリのディレクトリエントリの数
; TOTALSEC	equ	30720			; ボリュームの総セクタ数
; FATSZ		equ	12			; 1個のFATが占めるセクタ数
; HIDSEC		equ	128			; (VHDの)隠しセクタサイズ
; 
; LDRSVSEC	equ	0x82			; LDの予備FAT領域の有無と論理セクタサイズ(予備あり、512バイト)

;----------------------------------------------------------------------------
; 2MB offset(1セクタ512バイト版)
SCTSIZ		equ	512			; 1セクタのサイズ
CSTSEC		equ	2			; 1クラスタのセクタ数(512*2で1024バイト)
RSVSEC		equ	2			; 予備領域のセクタ数
NUMFAT		equ	1			; FATの数
ROOTCNT		equ	160			; ルートエントリのディレクトリエントリの数
TOTALSEC	equ	4096			; ボリュームの総セクタ数
FATSZ		equ	8			; 1個のFATが占めるセクタ数
HIDSEC		equ	0			; (VHDの)隠しセクタサイズ

LDRSVSEC	equ	0x02			; LDの予備FAT領域の有無と論理セクタサイズ(予備なし、512バイト)

; LSX-Dodgers用にBPB情報から計算してやる
TOTALCST	equ	TOTALSEC/CSTSEC
FATHEAD		equ	HIDSEC+RSVSEC
ROOTHEAD	equ	FATHEAD+FATSZ*NUMFAT
ROOTSCNT	equ	ROOTCNT*32/SCTSIZ
DATAHEAD	equ	ROOTHEAD+ROOTSCNT-2	; ここの計算若干怪しい(VHD版だと動かないと思われるのでBPBからの計算の方を正とする事)

; LSX-Dodgers内部コール
_BPB2DPB	equ	0xecf1

;----------------------------------------------------------------------------
;
TOP		equ	0xc600			; ORGの頭
PROGSZ		equ	0x300			; 先頭(0x100)からの非常駐部のプログラムサイズ
DPBTOP		equ	0xed00			; DPB先頭(A:)

;------------------------------------
		org	0x0100
;----------------------------------------------------------------------------
start:
	; 常駐チェック(手抜き。0x0006に0xc900が書かれていれば常駐済とする……)
	ld	hl,0x0006
	ld	a,(hl)
	cp	LDSYS & 0xff
	jr	nz,registhddd
	inc	hl
	ld	a,(hl)
	cp	LDSYS/256
	jr	nz,registhddd
	jp	hdddcmd

registhddd:
	; TPA末尾を0xc900に動かす(不気味)
	; まずは現状の値をコピー
	ld	de,LDSYS+1
	ld	hl,0x0006
	ldi
	ldi

	; 0x0006の中身を0xc900にする
	ld	hl,0x0006
	ld	a,0
	ld	(hl),a
	inc	hl
	ld	a,0xc9
	ld	(hl),a

	; SASI読み書き処理をTPA末尾付近(0xc900)にコピーする
	ld	hl,0x0100+PROGSZ	; COMが0x100に読まれるので、ここがLDSYSHDRDCの先頭になる
	ld	de,0xc900
	ld	bc,endadr-LDSYS
	ldir

	ld	c,0x6f		;MSX-DOS のバージョン番号の獲得(_DOSVER)
	call	0x0005
	ld	hl,0x0138
	and	a
	sbc	hl,de
	jr	nc,hdddcmd

	ld	a,(0x000b)
	ld	(BPB2DPB+2),a
	ld	(b2dp1+2),a
	ld	(b2dp2+2),a
b2dp1:	ld	hl,(_BPB2DPB+1)
	ld	(BPB2DPB+1),hl
	ld	hl,bpb2dpb1
b2dp2:	ld	(_BPB2DPB+1),hl
	; これで常駐完了

	; コマンドライン解析
	; hdd [drive(0-3)] [block(2MB単位)] [target drive(A-G)]
hdddcmd:
	; コマンドライン末尾の文字がA-Gの場合ドライブ名として扱い、対象のDPB位置を特定する
	; A-Gではない場合はデフォルト H: で処理する
	ld	hl,0x80
	ld	a,(hl)
	add	a,l		; hl + hl + a
	ld	l,a
	adc	a,h
	sub	l
	ld	h,a
	ld	a,(hl)
	or	0x20
	cp	'a'
	jr	c,hddcmd1	; A以下のASCII CODEの場合は無視してデフォルトF:として処理する(微妙)
	cp	'i'		; A,B,C,D,E,F,G,Hのみ許す
	jr	c,hddcmd3ok

	; 2: Target drive error
	ld	c,2
	jp	hdisperr
hddcmd3ok:
	and	0xdf
	; 表示用にドライブ名を入れておく
	ld	(targetdrv),a

	; 指定ドライブのDPBアドレスを算出してdpbadrに入れる
	sub	'A'
	ld	l,a
	ld	h,0
	add	hl,hl		; 2
	add	hl,hl		; 4
	add	hl,hl		; 8
	add	hl,hl		; 16
	add	hl,hl		; 32
	ld	bc,DPBTOP
	add	hl,bc
	ld	(dpbadr),hl

hddcmd1:
	; 任意のドライブのDPBをHDDのもので上書き
	ld	hl,hdddpb		; コピー元のHDD用DPB
	ld	de,(dpbadr)
	ld	bc,32
	ldir

	ld	de,0x005d	; arg1
	call	getnum
	jr	nc,hddcmd2
	; 0: Drive number error
	ld	c,0
	jp	hdisperr
hddcmd2:
	ld	a,l
	; 0-3 check
	cp	4
	jr	c,hddrvok
	; 0: Drive number error
	ld	c,0
	jp	hdisperr
hddrvok:
	; Set Unit Number( HDD DRIVE Number 0-3 )
	ld	ix,(dpbadr)
	ld	(ix+0x13),a

	push	af

	add	a,0x30		; ドライブ名文字列の数字も変更しておく(HDD0～HDD3)
	ld	(ix+0x1f),a

	ld	de,0x006d	; arg2
	call	getnum
	jr	nc,hddcmd3
	pop	af
	; 1: Offset number error
	ld	c,1
	jp	hdisperr
hddcmd3:
	pop	af

	; 0→先頭、1→先頭から2MBの位置……と、なるようにHLの値を調整してやる。1が65536なので、2MBにするには32倍してやればいい(5ビットシフトとかないんかい)
	add	hl,hl	; 2
	add	hl,hl	; 4
	add	hl,hl	; 8
	add	hl,hl	; 16
	add	hl,hl	; 32
	ex	de,hl
	ld	(ix+HDLBA1),e
	ld	(ix+HDLBA2),d

	ld	de,hdrgmsg
	ld	c,0x09
	call	0005h

	; ターゲットドライブを表示
	ld	de,targetdrv
	ld	c,0x09
	call	0005h

	; 割り当てるHDD名を表示
	ld	hl,(dpbadr)
	ld	bc,0x1c
	add	hl,bc
	ld	b,4
	ld	c,0x06
hddndsp:
	ld	e,(hl)
	call	0x0005
	inc	hl
	dec	b
	jr	nz,hddndsp

; BPBからDPBを設定する
; この時点でとりあえずSASIドライバの常駐はすんでいるので、直接ドライバの読み出し処理を使ってBPBを読み込む
	ld	a,(ix+DPB_UNITNO)
	call	sasi_set_drive

	ld	hl,0x0000		; IMG-先頭から読む(BPB先頭)
	call	check_bpb
	jp	nc,0

	ld	hl,0x0100		; VHD-0x10000バイトから読む(BPB先頭。ただし先頭64KBが隠しセクタ固定(いいのかな))
	call	check_bpb
	jp	nc,0

	ld	hl,0x007e		; IMG-0x07e00バイトから読む(MBR付き)
	call	check_bpb
	jp	nc,0

	ld	hl,0x0098		; HDI-0x09800バイトから読む
	call	check_bpb
	jp	nc,0

	ld	hl,0x0120		; HDI-0x12000バイトから読む
	call	check_bpb
	jp	nc,0

	ld	hl,0x0202		; NHD-0x20200バイトから読む
	call	check_bpb
	jp	nc,0

	; BPBをオフにする
	res	4,(ix+$12)	;DPB_01_DEVICE
	jp	0

check_bpb:
	push	hl
	ld	a,08h			;READ
	ld	e,0
	ld	c,1			; C   = block size(BPBは1ブロック=256バイト以内に入る)
	call	sasi_setup_rw6
	call	sasi_cmd6_open
	jr	c,bpbrerr
;SASIデータ転送
	ld	hl,0x4000		; テキトーに0x4000から読む(空いてるはず)
	ld	de,256			; DE = trasnfer size
	call	sasi_transfer
	jr	c,bpbrerr
;SASIステータス、メッセージ、バスフリー
	call	sasi_close
	jr	c,bpbrerr
	pop	hl

;	0x4000からBPBが読まれている(はず)
bpbtodpb:
	ld	iy,$4000	; BPB

	ld	a,(iy+0)	;BS_JmpBoot
	cp	$eb
	jr	z,bpbeb
	cp	$e9
	jr	z,bpbok
	cp	$60		;X68K
	jr	z,bpbeb
notbpb:
	scf
	sbc	a,a
	ret

bpbrerr:
	pop	hl
	ei
	ld	c,3
	jp	hdisperr

bpbeb:
	ld	a,(iy+2)
	cp	0x90
	jr	nz,notbpb
bpbok:
	ld	a,(iy+12)	;BPB_BytsPerSec
	or	a
	jr	z,notbpb
	ld	b,a
	dec	b
	and	b
	jr	nz,notbpb

	ld	a,(iy+13)	;BPB_SecPerClus
	or	a
	jr	z,notbpb
	ld	b,a
	dec	b
	and	b
	jr	nz,notbpb

	set	5,(ix+$12)	;DPB_01_DEVICE
	; 隠しセクタぶんをLBAに足しておいてやる
	ld	e,(ix+HDLBA0)
	ld	d,(ix+HDLBA1)
	xor	a
	add	hl,de
	adc	a,(ix+HDLBA2)
	ld	(ix+HDLBA0),l
	ld	(ix+HDLBA1),h
	ld	(ix+HDLBA2),a
	xor	a
	ret

getnum:
	ld	hl,0
numloop:
	ld	a,(de)
	cp	0x20
	jr	z,cmdend
	
	sub	'0'
	jr	c,cmderr
	cp	10
	jr	nc,cmderr

;	hlを10倍にする(初回無駄だがまあいいや)
	add hl,hl	; x2
	push hl
	add hl,hl
	add hl,hl	; x8
	pop bc
	add hl,bc	; x10

	ld c,a		; hl + hl + a
	ld b,0
	add hl,bc

;	add a,l		; hl + hl + a	BCツブさない版？もっといい方法がありそう
;	ld  l,a
;	ld  a,h
;	adc a,0
;	ld  h,a

	inc de
	jr numloop

cmderr:
	scf
cmdend:
	ret

; b = error number
hdisperr:
	ld	b,0
	sla	c	; c = c * 2
	ld	hl,hdermsg
	add	hl,bc
	ld	e,(hl)
	inc	hl
	ld	d,(hl)
	ld	c,0x09
	call	0005h
	scf
	jp	0

; 書き換え対象のDPBアドレス(デフォルトドライブ H:)
dpbadr:
	DW	0xede0

; エラーメッセージ群
hdermsg:
	DW	hder1
	DW	hder2
	DW	hder3
	DW	hder4
hder1:
	db	7,'Invalid drive number$'
hder2:
	db	7,'Invalid offset number$'
hder3:
	db	7,'Invalid target drive$'
hder4:
	db	7,0x0d,0x0a,'HDD BPB read error$'

hdrgmsg:
	db	'LD HDD controller v0.07', 0x0d,0x0a, '$'
targetdrv:
	db	'H: $'


; ■VHD(15MB)のBPB情報(参考)
; BPB_BytsPerSec バイト単位のセクタサイズ: 512
; BPB_SecPerClus クラスタを構成するセクタ数？: 8 → 4kb
; BPB_RsvdSecCnt 予約領域のセクタ数: 8
; BPB_NumFATs FATの数: 2
; BPB_RootEntCnt ルートディレクトリに含まれるディレクトリエントリの数: 512
; BPB_TotSec16 ボリュームの総セクタ数: 30720
; BPB_Media: F8
; BPB_FATSz16 1個のFATが占めるセクタ数: 12
; BPB_SecPerTrk トラック当たりのセクタ数: 63
; BPB_NumHeads ヘッド数: 255
; BPB_HiddSec ストレージ上でこのボリュームの手前に存在する隠れた物理セクタの数: 128 ※128*512=64KBぶんがBPB手前の領域
; BPB_TotSec32 ボリュームの総セクタ数: 0 ※BPB_TotSec16を見る事

; HDD DPBのコピー元
hdddpb:
	DW	FATSZ		; +$00 FAT領域のセクタ単位でのサイズ(1byte)
	DW	HDRDC		; +$02 HLが書き込まれるメモリアドレス、DEが1を1kbとしたHDD読み込み位置？
	DW	HDWTC		; +$04 HLが読み込みメモリアドレス、DEが1を1kbとしたHDD書き込み位置？
	DB	$F8		; +$06 メディアバイト(HDD)
	DB	CSTSEC		; +$07 1クラスタの論理セクタ数(1,2,4,8,16,32,64,128のみ可) 0x08
	DW	TOTALCST	; +$08 総クラスタ数 0xefa
	DB	0		; +$0A フロッピーディスクのモード(1byte)
	DB	ROOTSCNT	; +$0B ルートディレクトリ領域の終了の論理セクタ番号+1(1byte) 0x40
HDDBL:
HDLBA0		equ	$-hdddpb
	DB	0		; +$0C LBA0 / フロッピーディスクのシリンダ数(1byte)
HDLBA1		equ	$-hdddpb
	DB	0		; +$0D LBA1 / フロッピーディスクの1トラックのセクタ数(1byte) 隠しセクタ64KBをオフセットしてやる
	DB	FATHEAD		; +$0E FAT領域の先頭論理セクタ番号(1byte) 0x08
	DB	LDRSVSEC	; +$0F 予備FAT領域と論理セクタのサイズ 0x82
				;	上位1ビット:予備FAT領域
				;		1:使用する
				;		0:使用しない
				;	下位3ビット: 論理セクタのサイズ
				;		1:256バイト
				;		2:512バイト
				;		4:1024バイト
	DW	ROOTHEAD	; +$10 ルートディレクトリ領域の先頭論理セクタ番号(1byte) 0x20
	DB	$29		; +$12 Device Number?? ★
DPB_UNITNO	equ	$-hdddpb
HDDDV:	DB	3		; +$13 デバイスドライバ内におけるユニット番号(1byte)
	DW	DATAHEAD	; +$14 データ格納領域の先頭論理セクタ番号-2クラスタ(1byte) 0x30
	DW	0		; 
HDLBA2		equ	$-hdddpb
	DW	0		; +$11 -> +$18 LBA2 / フロッピーディスクのセクタの最小値(1byte) ★LBA2として利用
	DW	0		; +$1A カレントディレクトリのクラスタ番号(2bytes)
	DB	"HDD0"		; +$1C

;---------------------------------------------------------------------------
;SASI DRIVER
;---------------------------------------------------------------------------
	org	TOP+PROGSZ,PROGSZ

; このアドレスが0x0006に書かれている。本来のシステムコールアドレスである0xcc06をここで呼ぶ(いいのかこれ)
LDSYS:
	JP	0xcc06		; このアドレスは常駐時にツブされる
BPB2DPB:
	JP	0

;---------------------------------------------------------------------------
;WRITE CLUSTER
;in
;	IX   : DPB
;	C DE : cluster number
;	HL   : memory address
;out
;	CF   : 0=no error,1=error
;	C DE : next cluster
;	HL   : next address
;---------------------------------------------------------------------------
HDWTC:
	ld	a,0Ah			;WRITE(6)
	jr	HDRWC
;	db	0x01			;SKIP 2,BC ※BCをツブしてはいけなくなったのでjrしておく
;---------------------------------------------------------------------------
;READ CLUSTER
;in
;	IX   : DPB
;	C DE : cluster number
;	HL   : memory address
;out
;	CF   : 0=no error,1=error
;	C DE : next cluster
;	HL   : next address
;---------------------------------------------------------------------------
HDRDC:
	ld	a,08h			;READ(6)
HDRWC:
	push bc
	push de
	push hl
	push	af			;SASI CMD
	ld	a,(ix+DPB_UNITNO)
	push bc
	call	sasi_set_drive
; deの単位に合わせて256バイト単位のsasi_*secsをn(1,2,4)倍する
	ex de,hl
	ld	a,c
	LD	c,(IX+00FH)	;DPB_0F_BPS(bit0-2:1:256バイト 2:512バイト 4:1024バイト)
mulsec1:
	rr	c
	jr	c,mulsec2
	add hl,hl
	adc	a,a
	jr	mulsec1
mulsec2:
	pop bc
;パーティションオフセットを加算
	ld	e,(ix+$0C)	;LBA0/ フロッピーディスクのシリンダ数
	ld	d,(ix+$0D)	;LBA1/ フロッピーディスクの1トラックのセクタ数
	add	hl,de
	adc	a,(ix+$18)	;LBA2/ フロッピーディスクのセクタの最小値
	jr	c,sasi_err2
;SASIコマンドを構築
	ld	e,a		;EHL = sasi LBA
	add	a,$100-$e0	;SASIの理論アドレスは21ビットなので超えたらエラー
	jr	c,sasi_err2
	ld	a,(ix+0x0f)	;DPB_0F_BPS(bit0-2:1:256バイト 2:512バイト 4:1024バイト)
	and	7
	ld	c,a		;C   = block size
	xor	a
mulblock:
	add	a,c
	djnz	mulblock
	ld	c,a
	ld	(tspat+2),a
	pop	af		;SASI CMD
;	call	sasi_setup_read6
;	call	sasi_setup_write6
	call	sasi_setup_rw6	;READ(08) OR write(08)
;SASIセレクション、コマンドフェーズ
	call	sasi_cmd6_open
	jr	c,sasi_err
;SASIデータ転送
	pop	hl		;HL = memory address
	push hl
tspat:	ld	de,0x200	;DE = trasnfer size
	call	sasi_transfer
	jr	c,sasi_err
;SASIステータス、メッセージ、バスフリー
	call	sasi_close
	jr	c,sasi_err
	pop hl
	pop de
	pop bc
	ld	a,(tspat+2)
	add	a,h		;memory addressを進める
	ld	h,a
	ld	a,e		;セクタ位置を進める
	add	a,b
	ld	e,a
	ld	a,d
	adc	a,0
	ld	d,a
	adc	a,c
	sub	d
	ld	c,a
	xor a
	ret
;---------------------------------------------------------------------------
sasi_transfer:
	ld	a,(B_SASI_CMD6)
	cp	08h			;READ(6)?
	jp	z,sasi_rx		;READ
	jp	sasi_tx			;WRITE

;---------------------------------------------------------------------------
sasi_err2:
	pop	af
sasi_err:
	pop	hl
	pop	de
	pop	bc
;	scf
	ret


bpb2dpb1:
	call	BPB2DPB
	ret	nc
	LD	A,(IY+0)	;BS_JmpBoot
	CP	0EBH		;Short jump
	JR	Z,bpb2k
	CP	0E9H		;Near jump
	SCF
	RET
bpb2k:				;1セクタ2KB対策
	LD	A,(IY+12)	;BPB_BytsPerSec
	cp	8		;1セクタ2KB
	scf
	ret	nz
	LD	A,(IX+00FH)	;DPB_0F_BPS
	xor	0x0c
	LD	(IX+00FH),A	;DPB_0F_BPS

	sla	(IX+7)		;DPB_07_SECPCL

	sla	(IX+0)		;DPB_00_FATLN
	rl	(IX+1)		;DPB_00_FATLN

	sla	(IX+00EH)	;DPB_0E_FATPS

	sla	(IX+010H)	;DPB_10_DIRPS
	rl	(IX+011H)

	sla	(IX+00BH)	;DPB_0B_DIRSCNT

	sla	(IX+014H)	;DPB_14_ADDCL16
	rl	(IX+015H)
	ret


;SASIドライバ本体
	include	"sasi_poll.asm"
	include	"sasi_poll_tx.asm"
	include	"sasi_poll_rx.asm"

endadr:

;---------------------------------------------------------------------------