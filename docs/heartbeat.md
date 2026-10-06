# Direct Firestore heartbeat client

This dependency-free client replaces one latest-state Firestore document using a
site-scoped Firebase Authentication identity. It does not operate a server, claim
that every Internet connection works, or authorize an interruption test.

```python
from connection_monitoring.heartbeat_transport import HeartbeatClient

client = HeartbeatClient({
    'project_id': 'example-project',
    'site_id': 'example-site',
    'collection': 'siteHeartbeats',
    'api_key': 'PUBLIC_FIREBASE_API_IDENTIFIER',
    'expected_uid': 'enrolled-machine-uid',
    'service_keys': ['collector', 'dashboard'],
}, '/etc/example-heartbeat/credential.json', '/var/lib/example-heartbeat/token.json')

client.publish({
    'version': 1, 'site': 'example-site', 'observed_at': 1791288000,
    'uptime_seconds': 120, 'root_free_percent': 47.2,
    'services': {'collector': 'healthy', 'dashboard': 'unknown'},
})
```

`publish()` returns `{'accepted': True}` only after a successful commit response.
It makes one write with an atomic Firebase server receipt timestamp transform.
The caller freshly collects each snapshot and owns its scheduling; no backlog,
retry loop, server, system integration or estate configuration is included.

The producer has exactly six fields shown above. The stored document additionally
contains `received_at`, a Firestore timestamp generated using `REQUEST_TIME`.
Consumer liveness must use this receipt instead of the sender's wall clock.
`observed_at` and uptime are finite nonboolean numbers between 0 and 1e12;
root free percentage is between 0 and 100. Uptime/free may be null when unknown.
Services contain one to eight identifier keys, each healthy, unhealthy, or unknown.
Configuration optionally binds validation to the exact configured service keys.

The credential file contains exactly `version: 1`, `project_id`, `uid`, and
`refresh_token`. Both credential and ID-token cache require owner-only files and
directories. Symlinks, wrong owner, broad file access, or identity mismatch are
rejected. Rotated refresh credentials are saved atomically. The API key identifies
the Firebase project; authorization comes from the enrolled Auth UID and database
rules. Never provide broad IAM/service-account database credentials to site agents.

Configure database rules to permit only each machine's own schema-valid snapshot,
require server receipt timestamps, and restrict update rate. A verified operator
may read only the intended documents. This package does not create users or rules.
ID tokens are defensively checked for expected project, issuer, UID, custom
provider and expiration before sending; Firestore verifies their signatures.

Each Google HTTPS request has a ten-second timeout, response size bound, and no
redirect following. `HeartbeatError` messages are sanitized and never contain
credential or provider-response content. A caller should log only a safe outcome
and try again at its next regular scheduled invocation.

Common producer and stored-document fixtures are in
`tests/fixtures/heartbeat-v1.json`. They include typed Firestore REST values and
normalized timestamp strings for readers in another language, with valid, stale,
future, malformed, and missing receipt examples. Fresh liveness is independent of
reported component health.
