// F-030-01 / Bug-9365: SPA /health fan-out.
//
// nginx auth_request does not run inside subrequests (r->main != r), so a
// nested auth_request chain never probes the gateway. This handler issues
// three sibling subrequests and returns 503 unless every one is 2xx.
//
// Probe owners of these /health URLs (do not add a reciprocal probe):
//   model-service /health  — own Compose/Helm/Cloud Run probes; this SPA AND
//   query-router /health   — own Compose/Helm/Cloud Run probes; this SPA AND
//   gateway /health        — own Compose/Helm probes; MUST stay self-contained
//                            (Bug-8974: no call back into the SPA, no restart loop)
// Frontend container probes stay on /liveness or `/` (Compose, Cloud Run
// startupProbe). They must not use this /health or a dead gateway would
// restart nginx.

function _ok(status) {
    return status >= 200 && status < 300;
}

function servingHealth(r) {
    var checks = [
        { name: "model-service", uri: "/__health/model" },
        { name: "query-router", uri: "/__health/query-router" },
        { name: "gateway", uri: "/__health/gateway" },
    ];
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

export default { servingHealth };
