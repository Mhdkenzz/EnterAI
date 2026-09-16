# Enter AI QA Report

## Passed

- Frontend TypeScript check: `npm run test`
- Frontend production build: `npm run build`
- Backend tests: `3 passed`
- API product-flow test covers login, project brief upload and drafting, project creation, document association, Team project lookup, task completion, and dashboard retention of completed work.

## Included Runners

- Playwright desktop and mobile specs: `cd frontend && npm run test:e2e`
- Schemathesis API fuzzing: `./qa/run-schemathesis.sh`
- K6 smoke load: `k6 run qa/k6/smoke.js`
- OWASP ZAP baseline: `./qa/run-zap.sh`

## Environment Limits During This Run

- Playwright package installed, but its Chromium download could not complete through the restricted runtime proxy, so browser specs were not run here.
- Docker, K6, ZAP, and Schemathesis executables are not installed in this runtime, so their runners could not be executed here.

Run the four commands above after Docker and the relevant binaries are installed, preferably in CI against a fresh test database.
