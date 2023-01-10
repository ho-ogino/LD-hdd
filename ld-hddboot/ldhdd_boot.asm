;--------------------------------------------------------------------
;LDX Dodgers HD bootcode
;license: MIT
;note:
;	MAKESYSでHDD.COMを含めたLD.SYSを配置するための
;	HDD先頭部分を作成する(のか？)
;--------------------------------------------------------------------

;|0000-001F | IPL-FBC (X1)
;|0020-01BC | bootcode (X1)
IPL_TOP				equ	0x0000
;|01BE-01FF | PartitionTable 16x4
PARTITION_TOP		equ	IPL_TOP + 0x01BE
PARTITION_SIG		equ	IPL_TOP + 0x01FE
;end of MBR

;|0200-2FFF | LSX-Dodgers 1.43 (0xCB00-0xF1FF)
LD_IMG_TOP			equ	IPL_TOP+0x0200
;ヘッダがついている
LD_H_SIG			equ	LD_IMG_TOP+0
LD_H_TOP			equ	LD_IMG_TOP+1
LD_H_END			equ	LD_IMG_TOP+3
LD_H_ENTRY			equ	LD_IMG_TOP+5
;BODY
LD_DATA_TOP			equ	LD_IMG_TOP+7

;|3000-EFFF | GRAM FAT image : HD Driver & autoexec
GRAM_IMG_TOP		equ	0x3000
GRAM_IMG_END		equ	GRAM_IMG_TOP+0xC000
;
IPL_SIZE			equ	GRAM_IMG_END-IPL_TOP
;
;--------------------------------------------------------------------
IO_DIPSW	equ	0x1FF0

IPL_DRIVE	equ	0xFF87

;--------------------------------------------------------------------
;|0000-001F | IPL-FBC (X1)
;--------------------------------------------------------------------
	org	IPL_TOP
;
IPL_FCB:
	db	01h		;type
	db	"LSX-DodgersHD"	;name
	db	"Sys"		;extenstion
	db	20h		;passeord
	dw	IPL_SIZE	;size
	dw	IPL_TOP		;top
	dw	ipl_entry	;entry
	db	0,0,0,0,0	;date
	db	0,0,0		;start recoard

;--------------------------------------------------------------------
;|0020-01BC | bootcode (X1)
;--------------------------------------------------------------------
B_PT1FD0:
	db	0x00
;
;--------------------------------------------------------------------
;FCBにより、LDイメージがロードされているので
;再配置して実行するだけででよい
ipl_entry:
	di
	ld	sp,0
;------------------------------------------------
;LDを正しい位置に転送
	exx
	ld	hl,(LD_H_ENTRY)
	exx
	ld	de,(LD_H_TOP)
	ld	hl,(LD_H_END)
	or	a
	sbc	hl,de
	ld	c,l
	ld	b,h
	ld	hl,LD_DATA_TOP
;	ldir
	add	hl,bc
	ex	de,hl
	add	hl,bc
	ex	de,hl
	dec	de
	dec	hl
	lddr
;------------------------------------------------
;LDを起動する
	exx
	jp	(hl)
;------------------------------------------------
;|01BE-01FF | PartitionTable 16x4
;------------------------------------------------
; if $ >= (IPL_FCB+PARTITION_TOP)
;	error	"Boot Code over area"
; endif
	org	PARTITION_TOP
;パーティションデータブロック
;HDイメージから抜き出したものをincludeしたが
;本来はSYSgenで、ここは書き込まない、ということをする。

;	include	"mbr.img",B
	include	"HDDBASE.vhd",B,0x01BE,0x0042
;------------------------------------------------
;|0200-2FFF | LSX-Dodgers 1.43 (0xCB00-0xF1FF)
;------------------------------------------------
;LSX Dodfers OS本体のイメージ
	org		LD_IMG_TOP
	include	"LD.BIN",B
