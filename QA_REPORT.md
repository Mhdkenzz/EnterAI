# Enter AI QA Report

## Executed

- Frontend TypeScript check: `npm run test`
- Frontend production build: `npm run build`
- Backend tests: `4 passed` via `cd backend && PYTHONPATH=. /tmp/enterai-venv/bin/python -m pytest -q`.
- Frontend type and lint gates: `npm run test` and `npm run lint` passed.
- Frontend production build: `npm run build` passed.
- API product-flow tests cover login, brief upload/drafting, XLSX extraction, project creation/editing, duplicate rejection, document association, team lookup, task completion timestamps, and dashboard retention.

## Included Runners

- Playwright desktop and mobile specs: `cd frontend && npm run test:e2e`
- Schemathesis API fuzzing: `./qa/run-schemathesis.sh`
- K6 smoke load: `k6 run qa/k6/smoke.js`
- OWASP ZAP baseline: `./qa/run-zap.sh`
- OWASP dependency scan: `./qa/run-dependency-check.sh`
- Tryme smoke gate: `./qa/run-tryme.sh`

## Environment Limits During This Run

- Playwright ran discovery for 8 tests but could not launch because Chromium is not installed. Install it with `cd frontend && npx playwright install chromium`, then rerun `npm run test:e2e`.
- Docker, K6, ZAP, Schemathesis, and Tryme are not installed in this runtime. Run the included scripts in CI or a host with those tools available.
- The dependency scan requires Docker and writes `qa/dependency-check-report/`.

Run the six commands above after Docker and the relevant binaries are installed, preferably in CI against a fresh test database and a running API/web pair.
