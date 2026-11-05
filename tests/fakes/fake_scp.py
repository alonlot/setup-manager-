#!/usr/bin/env python3
"""Fake scp: copies the files on this PC (the "remote" folder is a local folder)."""
import shutil
import sys

args, raw = [], [a for a in sys.argv[1:] if a != "-q"]
while raw:  # skip the options: -P PORT, -o OPTION
    if raw[0] in ("-P", "-o"):
        raw = raw[2:]
    else:
        args, raw = raw, []
*files, dest = args
folder = dest.split(":", 1)[1]
for f in files:
    shutil.copy(f, folder)
