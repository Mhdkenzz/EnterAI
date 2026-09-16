import http from "k6/http";
import { check, sleep } from "k6";
export const options = { vus: 5, duration: "15s", thresholds: { http_req_failed: ["rate<0.01"], http_req_duration: ["p(95)<800"] } };
export default function () { const response = http.get(__ENV.API_URL || "http://127.0.0.1:8000/health"); check(response, { "health is ok": (result) => result.status === 200 && result.json("ok") === true }); sleep(1); }
