#!/bin/sh
TGT=hdd
TOOL=../../tool
AILZ80=$TOOL/AILZ80ASM

$AILZ80 -f -sym -lst -bin -i $TGT.asm
if [ $? -gt 0 ]; then
	echo error!
	exit 1
fi
rm $TGT.com
mv $TGT.bin $TGT.com

