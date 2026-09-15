// F-030-01 / Bug-9365 / Bug-9054: SPA /health and /readiness fan-out.
//
// nginx auth_request does not run inside subrequests (r->main != r), so a
// nested auth_request chain never probes the gateway. This handler issues
// three sibling subrequests and returns 503 unless every one is 2xx.
//
// Probe owners of these URLs (do not add a reciprocal probe):
//   model-service /health and /readiness — each checks its own metadata DB
//   query-router /health and /readiness  — each checks its own metadata DB
//   gateway /health                      — JDBC liveness only, self-contained
//   frontend /readiness uses gateway /health as a direct probe. Its sibling
//   model-service and query-router readiness probes cover the same direct
//   serving dependencies without nesting the gateway's own peer fan-out.
// Frontend /health and /readiness are serving checks; frontend /liveness is
// process-only. No backend may call back into the frontend, so the fan-out
// remains finite and cannot form a restart cascade (Bug-9054).
// Frontend container probes stay on /liveness or `/` (Compose, Cloud Run
// startupProbe). They must not use /health or /readiness or a dead gateway would
// restart nginx.

function _ok(status) {
    return status >= 200 && status < 300;
}

function _fanout(r, checks) {
    return Promise.all(checks.map(function (c) {
        return r.subrequest(c.uri).then(
            function (reply) {
                return { name: c.name, status: reply.status };
            },
            function () {
                return { name: c.name, status: 503 };
            }
        );
    })).then(function (results) {
        var failed = [];
        for (var i = 0; i < results.length; i++) {
            if (!_ok(results[i].status)) {
                failed.push(results[i].name);
            }
        }
        r.headersOut["Content-Type"] = "application/json";
        if (failed.length) {
            r.return(503, '{"status":"degraded","service":"frontend"}');
            return;
        }
        r.return(200, '{"status":"ok","service":"frontend"}');
    });
}

function servingHealth(r) {
    return _fanout(r, [
        { name: "model-service", uri: "/__health/model" },
        { name: "query-router", uri: "/__health/query-router" },
        { name: "gateway", uri: "/__health/gateway" },
    ]);
}

function servingReadiness(r) {
    return _fanout(r, [
        { name: "model-service", uri: "/__readiness/model" },
        { name: "query-router", uri: "/__readiness/query-router" },
        { name: "gateway", uri: "/__readiness/gateway" },
    ]);
}

export default { servingHealth, servingReadiness };
