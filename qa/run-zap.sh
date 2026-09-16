#!/usr/bin/env sh
set -eu
docker run --rm --network host -v "$PWD:/zap/wrk:rw" zaproxy/zap-stable zap-baseline.py -t http://127.0.0.1:3000 -r zap-report.html
