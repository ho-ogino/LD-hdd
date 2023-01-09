#!/bin/sh

NDCPATH=./ndc
AILZPATH=./AILZ80ASM
PYTHONPATH=python

function Error() {
  echo ERROR! $1
  cd $CURPATH
  exit 1
}

$NDCPATH G MKLDHD.d88 0 LD.BIN .
if [ $? -ne 0 ]; then
  Error "ndc error"
fi

$AILZPATH ldhdd_boot.asm -f
if [ $? -ne 0 ]; then
  Error ""
fi

$PYTHONPATH mkldhdd.py
if [ $? -ne 0 ]; then
  Error ""
fi
