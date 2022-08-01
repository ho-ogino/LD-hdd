;------------------------------------
;SASIéÛêM
;in
;	HL : data address
;	DE : length
;out
;	CF : 1=timeout
sasi_rx:
;adjust 16bit counter : d=d+(e!=0)
	inc	d
	dec	de
	inc	e
sasi_rx_req:
	ld	bc,IO_SASI_DATA
sasi_rx_r:
	ld	a,b
	in	a,(low IO_SASI_STS)
	rrca
	jr	nc,sasi_rx_nreq
	ini
	inc	b
	dec	e
	jp	nz,sasi_rx_r
	dec	d
	jp	nz,sasi_rx_r
	or	a
	ret
;------------------
;wait until REQ
sasi_rx_nreq:
	ld	bc,0		;reset timeout
sasi_rx_w:
	ld	a,high IO_SASI_STS
	in	a,(low IO_SASI_STS)
	rrca
	jr	c,sasi_rx_req
	djnz	sasi_rx_w
	dec	c
	jr	nz,sasi_rx_w
;timeout error
	scf
	ret
