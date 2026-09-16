#!/usr/bin/env sh
set -eu
schemathesis run http://127.0.0.1:8000/openapi.json --checks all --exclude-checks response_headers_conformance --phases examples,coverage,fuzzing
