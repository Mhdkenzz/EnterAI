#!/usr/bin/env sh
set -eu
mkdir -p qa/dependency-check-report
docker run --rm -v "$PWD:/src" owasp/dependency-check:latest \
  --scan /src --format HTML --out /src/qa/dependency-check-report --failOnCVSS 7
