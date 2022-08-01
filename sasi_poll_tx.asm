;------------------------------------
;SASIëóêM
;in
;	HL : data address
;	DE : length
;out
;	CF : 1=timeout
sasi_tx:
;adjust 16bit to 2x8bit counter
	inc	d
	dec	de
	inc	e
sasi_tx_req:
	ld	bc,IO_SASI_DATA
sasi_tx_r:
	ld	a,b
	in	a,(low IO_SASI_STS)
	rrca
	jr	nc,sasi_tx_nreq
	inc	b
	outi
	dec	e
	jp	nz,sasi_tx_r
	dec	d
	jp	nz,sasi_tx_r
	or	a
	ret
;------------------
;wait until REQ
sasi_tx_nreq:
	ld	bc,0		;reset timeout
sasi_tx_w:
	ld	a,high IO_SASI_STS
	in	a,(low IO_SASI_STS)
	rrca
	jr	c,sasi_tx_req
	djnz	sasi_tx_w
	dec	c
	jr	nz,sasi_tx_w
;timeout error
	scf
	ret
