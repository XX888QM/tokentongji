#!/bin/bash
export PYTHONPATH="/Users/yunxin/Desktop/开发/token统计/src"
export TOKENSTAT_PORT=8787
exec /usr/bin/python3 -m tokenstat.server
