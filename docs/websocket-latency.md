# HTTP and WSK latency measurements

Measured in the browser against the local test site on 2026-09-09, using the
current two compatibility worktrees. No transport implementation was changed
for this benchmark. The fetch path exists only in the benchmark page.

## Method

Same `probe` method and input (`value=7`), ordinary ephemeral-page construction,
Bag/XML result processing through the legacy RPC resultHandler. Each result is
checked before taking the next measurement. One request is outstanding at a time.
The websocket and page channel are opened before timing. Normal HTTP connection
reuse is allowed. The test runs on localhost with the development test-server
configuration; this is not a WAN, throughput or production-load measurement.

Elapsed browser time starts immediately before dispatch and ends in the result
callback after parsing. Server time is the corresponding X-GnrTime header, so it
does not represent the entire core routing/queue/transport cost. Milliseconds below.

## Initial comparison

12 warm-up pairs, then three rounds of 50 alternating pairs (150 measured calls
per transport). Median total: HTTP/Dojo 51.90 ms, WSK 5.60 ms. Median server:
HTTP/Dojo 4.01 ms, WSK 3.41 ms. HTTP p95 52.60 ms; WSK p95 10.70 ms.

The approximately fixed 52 ms HTTP completion prompted a control experiment.
Legacy Dojo's `_ioWatch` uses `setInterval(_watchInFlight, 50)` in
`dojo_libs/dojo_11/dojo_src/dojo/_base/xhr.js:566`; the deployed compact version
has the same interval at `dojo/dojo/_base/xhr.js:278`. The interval watches async
request completion. It is not a mandatory cost of HTTP itself.

## Control experiment

Added native fetch POST, using the same legacy parameter serializer and RPC
resultHandler. 12 warm-up triplets, then three rounds of 50 triplets with rotated
transport order (150 measured calls for each transport, 450 total). No failed or
invalid replies were reported; the run stops on those rather than dropping them.

| Transport/client | Median total | p95 total | Mean total | Median server |
| --- | ---: | ---: | ---: | ---: |
| HTTP, legacy Dojo | 51.80 | 52.50 | 51.79 | 3.06 |
| HTTP, native fetch | 5.60 | 8.30 | 5.96 | 2.91 |
| WebSocket, current WSX client | 4.50 | 6.20 | 4.72 | 2.76 |

Per-round median total:

| Round | HTTP/Dojo | HTTP/fetch | WSK |
| --- | ---: | ---: | ---: |
| 1 | 51.80 | 5.60 | 4.60 |
| 2 | 51.85 | 5.55 | 4.45 |
| 3 | 51.80 | 5.95 | 4.60 |

## Interpretation

The present WSK client completes this lightweight call much earlier than the
legacy HTTP client. The control and source inspection attribute most of that
gap to the old client's completion polling, not inherently to HTTP versus
WebSocket. Compared with native fetch, observed median WSK latency is 1.10 ms
lower (about 20%). This is a local observation across three short rounds, not
a general speedup guarantee. Server work is similar across all three paths.

No conclusion is drawn about page residency: every measured call still builds
an ephemeral page. Heavy methods, other payloads, concurrent users and network
latency require separate measurements. Background polling was left unchanged.

## Reproduction

The reusable page is `tests/fixtures/wsx_benchmark.py`. Install it as a temporary
site webpage using the same setup described in `websocket-reception.md`, and
press **Run benchmark**. The measured control page remains at
`http://127.0.0.1:18971/webpages/wsx_benchmark_control` while this test server runs.
It displays aggregate and per-round statistics plus raw samples in a textarea.

The benchmark waits for channel readiness, excludes warm-ups, validates returned
values and aborts on failure. The performance timer also includes client work
specific to each path; it intentionally measures callback latency, not just wire time.

## Page class cache enabled

On 2026-09-09, enabled `experimental.page?page_class_cache=True` in the
`test_invoice_pg` instanceconfig and restarted the test server. A diagnostic
confirmed the flag was true and two successive requests reused the same class
identity. All 12 websocket tests passed with the flag enabled.

Repeated the unchanged browser control benchmark (12 warm-up triplets,
three rotating rounds, 150 measured requests per transport):

| Transport | Median total ms | p95 total ms | Median server ms |
| --- | ---: | ---: | ---: |
| HTTP/Dojo | 52.00 | 52.50 | 3.68 |
| HTTP/fetch | 6.40 | 9.90 | 3.23 |
| WSK | 5.40 | 8.20 | 3.21 |

This run does not demonstrate a speedup over the earlier cache-off run.
The runs were taken at different times with a server restart between them;
these differences cannot establish the cache's causal effect. The probe is a
lightweight page, not a component-heavy TableHandler page. Cache remains enabled.

A second cache-enabled run on the same page, without restarting or changing
configuration, completed all 150 measurements per transport:

| Transport | Median total ms | p95 total ms | Median server ms |
| --- | ---: | ---: | ---: |
| HTTP/Dojo | 51.80 | 52.40 | 2.87 |
| HTTP/fetch | 5.60 | 8.10 | 2.73 |
| WSK | 4.40 | 6.40 | 2.59 |

The change between the two cache-enabled runs illustrates run-to-run variation.
The repeat is close to the original cache-off control; it still does not isolate
the effect of the class cache. No configuration was changed for this repeat.
