# Spec: `GET /health` endpoint

- **Status:** draft
- **Date:** 2026-10-17
- **Owner:** Dima Bognen

## 1. Context

`ieso-zonal-demand` is a single image-based Lambda (`ieso_zonal_demand_to_s3.py`) triggered once a day by an EventBridge schedule. It has no HTTP surface today, so nothing can confirm that a deployment is live or which build is running without reading CloudWatch logs.

This spec adds a read-only `GET /health` endpoint that reports the running **version** and the **uptime** of the execution environment that serves the request.

## 2. Goals and non-goals

**Goals**
- G1. Confirm a deployment is reachable and responding.
- G2. Report which build is running (version).
- G3. Report how long the serving execution environment has been alive (uptime).

**Non-goals**
- Dependency checks (IESO site reachability, S3 write access). These belong in a later `/health/ready` spec.
- Triggering or reporting on the daily scrape run.
- Authentication beyond what section 6 states.

## 3. Design decisions

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | The endpoint is served by the **existing** Lambda function behind a new HTTP route. It is not a separate function. | A separate function would report its own version and uptime, not the scraper's. |
| D2 | The handler tells the two invocation types apart by event shape. An HTTP API (API Gateway v2) event is routed to the health logic. Every other event keeps today's scrape behaviour. | Keeps the scheduled path unchanged. |
| D3 | **Uptime** means seconds since this Lambda execution environment was initialised (module import time at cold start). It is **not** time since deployment. | Lambda has no long-lived process. Environment lifetime is the only uptime that can be measured honestly. |
| D4 | **Version** is read from a deploy-time environment variable `APP_VERSION`. Its value is the release tag, or the short git SHA if there is no tag. | The repo has no version constant. Env vars are the standard way to pass system parameters. |
| D5 | Exposure is through an **API Gateway HTTP API** route `GET /health`. | It is cheap, gives a stable path, and allows throttling. A Lambda Function URL cannot restrict methods or paths per route. |
| D6 | The health path must not import or run the scrape modules' heavy work (pandas/pyarrow processing, network calls). | Keeps latency low and avoids side effects. |

## 4. API contract

### Request
```
GET /health
```
- No query parameters, body or required headers.
- Any query string is ignored.

### Response: `200 OK`
Headers:
- `Content-Type: application/json`
- `Cache-Control: no-store`

Body:
```json
{
  "status": "ok",
  "version": "1.4.0",
  "uptime_seconds": 1834.27,
  "started_at": "2026-10-17T03:15:02.114Z",
  "timestamp": "2026-10-17T03:45:36.384Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string, always `"ok"` | Liveness indicator. |
| `version` | string | Value of `APP_VERSION`. `"unknown"` if it is not set. |
| `uptime_seconds` | number, ≥ 0, 2 decimal places | `timestamp − started_at`, from a monotonic clock. |
| `started_at` | string, ISO-8601 UTC, ms precision, `Z` suffix | Time the execution environment initialised. |
| `timestamp` | string, ISO-8601 UTC, ms precision, `Z` suffix | Time the response was generated. |

No other fields are returned. The response must not contain secrets, environment variable values other than `APP_VERSION`, bucket names, ARNs or stack traces.

### Other methods
- `POST`, `PUT`, `DELETE` and `PATCH` on `/health` are not routed. API Gateway returns its default `404`/`405`, and the Lambda is not invoked.

### Error response: `500 Internal Server Error`
Returned only if building the response fails unexpectedly:
```json
{ "status": "error" }
```
Error details go to the logs, never into the response body.

## 5. Acceptance criteria (EARS)

- **AC1** (G1). When a `GET /health` request is received, the system shall respond with HTTP `200` and a JSON body that matches section 4.
- **AC2** (G2). When `APP_VERSION` is set, the system shall return its value unchanged in `version`.
- **AC3** (G2). If `APP_VERSION` is unset or empty, then the system shall return `"version": "unknown"` and still respond `200`.
- **AC4** (G3). When two health requests are served by the same warm execution environment, the system shall return the same `started_at` in both and a `uptime_seconds` in the second that is greater than or equal to the first.
- **AC5** (G3). When a health request is served by a cold-started environment, the system shall return `uptime_seconds` < 5.
- **AC6** (D2). When the function receives an EventBridge schedule event, the system shall run the existing scrape exactly as before and shall not return a health payload.
- **AC7** (D6). While serving a health request, the system shall make no outbound network calls and no S3 operations.
- **AC8**. When a health request is served, the system shall emit exactly one structured log line at `INFO` containing `event="health_check"`, `version`, `uptime_seconds` and the API Gateway `requestId`.
- **AC9**. If an unexpected exception occurs while building the response, then the system shall return `500` with body `{"status":"error"}` and log the exception at `ERROR` with its stack trace.
- **AC10**. The system shall include `Cache-Control: no-store` on every health response.

## 6. Non-functional requirements

| ID | Requirement | Target |
|----|-------------|--------|
| NFR1 | Handler latency on a warm environment (Lambda `Duration`) | p95 ≤ 50 ms |
| NFR2 | End-to-end latency on a warm environment, measured at the client in the same region | p95 ≤ 300 ms |
| NFR3 | Throttling on the route | 10 rps steady, 20 burst |
| NFR4 | Access | Public and unauthenticated. This is acceptable because the payload has only a version string and timings. Revisit if fields are added. |
| NFR5 | Cost | No provisioned concurrency. The health route must not keep the function warm on purpose. |

## 7. Infrastructure and configuration changes

- Add an API Gateway HTTP API with one route, `GET /health`, integrated with the existing function. Give it an explicit, human-readable name, for example `ieso-zonal-demand-${stage}-http-api`.
- Add an explicit, named access-log group for the API.
- Add the `APP_VERSION` environment variable, set at deploy time from the git tag or SHA. Do not hard-code it in the template.
- The function's IAM role needs no new permissions.
- Document the endpoint URL, a sample `curl`, and a sample API Gateway v2 test event for local invocation in the README.

## 8. Test plan

| Test | Covers |
|------|--------|
| Unit: HTTP API v2 `GET /health` event returns 200 and the schema from section 4 | AC1, AC10 |
| Unit: `APP_VERSION` set and unset | AC2, AC3 |
| Unit: two invocations in the same process return the same `started_at` and non-decreasing uptime | AC4 |
| Unit: schedule event still dispatches to the scrape path (mocked) | AC6 |
| Unit: health path with network and S3 clients mocked to fail if called | AC7 |
| Unit: forced exception returns 500 and a generic body | AC9 |
| Unit: log record captured with the required fields | AC8 |
| Post-deploy smoke: `curl -i https://<api>/health` returns 200 and `version` matches the deployed tag | AC1, AC2 |

## 9. Open questions

1. **Deployment tool.** The repo deploys with Serverless Framework (`serverless.yml`), but the team standard is AWS SAM (`template.yaml`). Should this change be made in `serverless.yml`, or wait for the SAM migration?
2. **Function naming.** The current name `ieso-zonal-demand-<stage>-to-s3` does not follow the `scrape-<functionality>` convention, for example `scrape-ieso-zonal-demand`. Is renaming in scope here?
3. **Versioning scheme.** Release tags (semver) or the short git SHA only?
4. **Access.** Is a public, unauthenticated endpoint acceptable, or should it require IAM auth or an API key?
