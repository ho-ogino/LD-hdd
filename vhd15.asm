;----------------------------------------------------------------------------
; X1/turbo LSX-Dodgers SASI Access Program
; usage
;   hdd [hdd drive] [offset] [target drive]
;     hdd drive: 0 - 3(HD0～HD3)
;     offset: 2MBごとのインデックス(1で2MBの位置、2で4MBの位置を先頭として処理する)
;     tareget drive: A-G 割り当てるドライブ
;
;note:
; LSX-DodgersにてHDDをドライブに割り当て、読み書きするための常駐プログラムです。
; VHDファイルの15MB専用
;
; 0x100から読み込まれるCOMファイルとしてバイナリ出力する前提で作っています。
; 
; C900からSASIドライブ読み書き処理が配置され、TPAの末尾(0x0006)をC900に動かし、
; C900にはJP 0xCC06(元々0x0006に書かれていたアドレス(LSX-Dodgersのシステム領域開始アドレス)) を書く事で強引に常駐させています。
; 正しい方法を知りたいところです……(リロケータブルにするのしんどいので固定にしてるのがダメ？)。
;
; また、Fドライブを定義するDPB(ドライブパラメータブロック)をHDD用に上書きしているため、
; このプログラム実行後、FドライブをHDDとしてアクセス出来るようになります。
;
; VHDファイルのメモリマップは以下を想定しています。
;  0x00000 -   0x0ffff : 
;  0x10000 -   0x10fff : BPB等
;  0x11000 -   0x127ff : FAT(6KB)
;  0x12800 -   0x13fff : 予備FAT(6KB)
;  0x14000 -   0x17fff : ルートディレクトリ領域(16KB)
;  0x18000 -           : データ格納領域(2MB - 11KB)
;
; とりあえずこのプログラムにより、エミュレータにおいてフォーマット、ファイルの読み書きが出来る事を
; 確認済です(が、前述のとおりDPBはある程度整えてあるものの、正しく全領域が使われるかも不明)。
; あくまで現状、人柱版となります。
;


;----------------------------------------------------------------------------
;
TOP		equ	0xc700			; ORGの頭
PROGSZ		equ	0x200			; 先頭(0x100)からの非常駐部のプログラムサイズ
DPBTOP		equ	0xed00			; DPB先頭(A:)

;------------------------------------
		org	TOP+0x0000
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
	jp	hdddcmd-TOP+0x100

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
	ld	hl,LDSYS-TOP+0x100	; COMが0x100に読まれるので、ここがLDSYSHDRDCの先頭になる
	ld	de,0xc900
	ld	bc,endadr-LDSYS
	ldir

	; これで常駐完了

	; コマンドライン解析
	; hddd [drive(0-3)] [block(2MB単位)] [target drive(A-G)]
hdddcmd:
	; コマンドライン末尾の文字がA-Gの場合ドライブ名として扱い、対象のDPB位置を特定する
	; A-Gではない場合はデフォルト H: で処理する
	ld	hl,0x80
	ld	a,(hl)
	add	a,l		; hl + hl + a
	ld	l,a
	ld	a,h
	adc	a,0
	ld	h,a
	ld	a,(hl)
	or	0x20
	cp	'a'
	jr	c,hddcmd1	; A以下のASCII CODEの場合は無視してデフォルトF:として処理する(微妙)
	cp	'h'		; A,B,C,D,E,F,Gのみ許す
	jr	c,hddcmd3ok

	; 2: Target drive error
	ld	c,2
	jp	hdisperr-TOP+0x100
hddcmd3ok:
	and	0xdf
	; 表示用にドライブ名を入れておく
	ld	(targetdrv-TOP+0x100),a

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
	ld	(dpbadr-TOP+0x100),hl

hddcmd1:
	; 任意のドライブのDPBをHDDのもので上書き
	ld	hl,hdddpb-TOP+0x100		; コピー元のHDD用DPB
	ld	de,(dpbadr-TOP+0x100)
	ld	bc,32
	ldir

	ld	de,0x005d	; arg1
	call	getnum-TOP+0x100
	jr	nc,hddcmd2
	; 0: Drive number error
	ld	c,0
	jp	hdisperr-TOP+0x100
hddcmd2:
	ld	a,l
	; 0-3 check
	cp	4
	jr	c,hddrvok
	; 0: Drive number error
	ld	c,0
	jp	hdisperr-TOP+0x100
hddrvok:
	; Set Unit Number( HDD DRIVE Number 0-3 )
	ld	ix,(dpbadr-TOP+0x100)
	ld	(ix+0x13),a

	push	af

	add	a,0x30		; ドライブ名文字列の数字も変更しておく(HDD0～HDD3)
	ld	(ix+0x1f),a

	ld	de,0x006d	; arg2
	call	getnum-TOP+0x100
	jr	nc,hddcmd3
	pop	af
	; 1: Offset number error
	ld	c,1
	jp	hdisperr-TOP+0x100
hddcmd3:
	pop	af

	; 0→先頭、1→先頭から2MBの位置……と、なるようにHLの値を調整してやる。1が65536なので、2MBにするには32倍してやればいい(5ビットシフトとかないんかい)
	add	hl,hl	; 2
	add	hl,hl	; 4
	add	hl,hl	; 8
	add	hl,hl	; 16
	add	hl,hl	; 32
	ex	de,hl
;	ld	(ix+HDLBA1),e
;	ld	(ix+HDLBA2),d

	ld	de,hdrgmsg-TOP+0x100
	ld	c,0x09
	call	0005h

	; ターゲットドライブを表示
	ld	de,targetdrv-TOP+0x100
	ld	c,0x09
	call	0005h

	; 割り当てるHDD名を表示
	ld	hl,(dpbadr-TOP+0x100)
	ld	bc,0x1c
	add	hl,bc
	ld	b,4
	ld	c,0x02
hddndsp:
	ld	e,(hl)
	call	0x0005
	inc	hl
	dec	b
	jr	nz,hddndsp

	; 終了
	jp	0

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
	ld	hl,hdermsg-TOP+0x100
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
	DW	hder1-TOP+0x100
	DW	hder2-TOP+0x100
	DW	hder3-TOP+0x100
hder1:
	db	7,'Invalid drive number$'
hder2:
	db	7,'Invalid offset number$'
hder3:
	db	7,'Invalid target drive$'

hdrgmsg:
	db	'LD VHD controller v0.00', 0x0d,0x0a, '$'
targetdrv:
	db	'H: $'


	; HDD DPBのコピー元
hdddpb:
	DB	12	; +$00 FAT領域のセクタ単位でのサイズ(1byte) ★クラスタ数÷(1024 or 512(クラスタサイズ))×1.5の切り上げサイズが必要？2048なので3となる？
	DB	$F8	; +$01 メディアバイト(HDD)
	DW	HDRDC	; +$02 HLが書き込まれるメモリアドレス、DEが1を1kbとしたHDD読み込み位置？
	DW	HDWTC	; +$04 HLが読み込みメモリアドレス、DEが1を1kbとしたHDD書き込み位置？
	DB	32+32-8*2	; +$06 データ格納領域の先頭論理セクタ番号-2(1byte)、論理セクタとクラスタの関係(1byte)→0？？ ★つまり先頭から11KBの位置からデータが格納される
	DB	8	; +$07 1クラスタの論理セクタ数
	DW	3840-6	; +$08 総クラスタ数 ★(FDDだと356だがとりあえず2048にしてみた……)
	DB	0	; +$0A フロッピーディスクのモード(1byte)
	DB	32+32	; +$0B ルートディレクトリ領域の終了の論理セクタ番号+1(1byte) ★つまり先頭から10KB目までがルートディレクトリ領域となる
HDDBL:
HDLBA0		equ	$-hdddpb
	DB	0	; +$0C LBA0 / フロッピーディスクのシリンダ数(1byte)
HDLBA1		equ	$-hdddpb
	DB	1	; +$0D LBA1 / フロッピーディスクの1トラックのセクタ数(1byte)
	DB	8	; +$0E FAT領域の先頭論理セクタ番号(1byte) これより前は予備(1KB)
	DB	$82	; +$0F 予備FAT領域と論理セクタのサイズ
	DB	32	; +$10 ルートディレクトリ領域の先頭論理セクタ番号(1byte) ★つまり先頭から5KB～10KB目までがルートディレクトリ領域で、
			; 領域が5KBあるので、5KB÷32で160ファイル置ける事になる。ちなみにFAT/FAT予備の直後にあれば良いため予備なしなら3でいい(予備ありなら6の方がいいが……)
HDLBA2		equ	$-hdddpb
	DB	0	; +$11 LBA2 / フロッピーディスクのセクタの最小値(1byte) ★LBA2として利用
	DB	9	; +$12 Device Number?? ★？
DPB_UNITNO	equ	$-hdddpb
HDDDV:	DB	3	; +$13 デバイスドライバ内におけるユニット番号(1byte)
	DW	0	; +$14
	DW	0	; GNCLデフォルトを使う ; +$16 FATの内容を読み出すルーチンの実行アドレス(2bytes)   ※ Fドライブのものを残すので使わない(が、この値で正しいはず)
	DW	0	; SNCLデフォルトを使う ; +$18 FATにデータを書き込むルーチンの実行アドレス(2bytes) ※ 同上
	DW	0	; +$1A カレントディレクトリのクラスタ番号(2bytes)
	DB	"HDD0"  ; +$1C

;---------------------------------------------------------------------------
;SASI DRIVER
;---------------------------------------------------------------------------
	org	TOP+PROGSZ

; このアドレスが0x0006に書かれている。本来のシステムコールアドレスである0xcc06をここで呼ぶ(いいのかこれ)
LDSYS:
	JP	0xcc06		; このアドレスは常駐時にツブされる

;---------------------------------------------------------------------------
;WRITE CRUSTER
;in
;	IX   : DPB
;	DE   : cruster number
;	HL   : memory address
;out
;	CF   : 0=no error,1=error
;	DE   : next cruster
;	HL   : next address
;---------------------------------------------------------------------------
HDWTC:
	ld	a,0Ah			;WRITE(6)
;	jr	HDRWC
	db	0x01			;SKIP 2,BC
;---------------------------------------------------------------------------
;READ CRUSTER
;in
;	IX   : DPB
;	DE   : cruster number
;	HL   : memory address
;out
;	CF   : 0=no error,1=error
;	DE   : next cruster
;	HL   : next address
;---------------------------------------------------------------------------
HDRDC:
	ld	a,08h			;READ(6)
HDRWC:
	push de
	push hl
	push	af			;SASI CMD
	ld	a,(ix+DPB_UNITNO)
	call	sasi_set_drive
; deは512b単位の数値だがsasi_*secsは256バイト単位なので2倍する
	ex de,hl
	xor	a
	add hl,hl
	adc	a,a
;パーティションオフセットを加算
	ld	e,(ix+$0C)	;LBA0/ フロッピーディスクのシリンダ数
	ld	d,(ix+$0D)	;LBA1/ フロッピーディスクの1トラックのセクタ数
	add	hl,de
	adc	a,(ix+$11)	;LBA2/ フロッピーディスクのセクタの最小値
;SASIコマンドを構築
	ld	e,a		;EHL = sasi LBA
	ld	c,512/256	;C   = block size
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
	ld	de,512		;DE = trasnfer size
	call	sasi_transfer
	jr	c,sasi_err
;SASIステータス、メッセージ、バスフリー
	call	sasi_close
	jr	c,sasi_err
	pop hl
	pop de
	inc h		;memory addressを進める
	inc h
	inc de		;セクタ位置を進める
	xor a
	ret
;---------------------------------------------------------------------------
sasi_transfer:
	ld	a,(B_SASI_CMD6)
	cp	08h			;READ(6)?
	jp	z,sasi_rx		;READ
	jp	sasi_tx			;WRITE

;---------------------------------------------------------------------------
sasi_err:
	pop	hl
	pop	de
;	scf
	ret

;SASIドライバ本体
	include	"sasi_poll.asm"
	include	"sasi_poll_tx.asm"
	include	"sasi_poll_rx.asm"

endadr:

;---------------------------------------------------------------------------
