#!/usr/bin/env sh
set -eu
schemathesis run http://127.0.0.1:8000/openapi.json --checks all --exclude-checks allow_header_conformance,positive_data_acceptance --phases examples,coverage,fuzzing
