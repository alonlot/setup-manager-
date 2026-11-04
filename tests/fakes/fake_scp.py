#!/usr/bin/env python3
"""Fake scp: copies the files on this PC (the "remote" folder is a local folder)."""
import shutil
import sys

args = [a for a in sys.argv[1:] if a != "-q"]
if args and args[0] == "-P":
    args = args[2:]
*files, dest = args
folder = dest.split(":", 1)[1]
for f in files:
    shutil.copy(f, folder)
