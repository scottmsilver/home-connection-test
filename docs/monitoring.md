# Shared connection monitoring

`connection_monitoring` is a Python 3.8+ standard-library package for bounded
alert delivery, Firewalla observation, and supervised single-WAN recovery drills.
It can run from a source checkout or a zipapp. It contains no inventory,
credentials, deployment paths, alert provisioning, or model/provider policy.
The deployment owns those inputs and supplies them explicitly.

| Shared package owns | Deployment owns |
| --- | --- |
| Alert parsing, prose contracts, durable delivery and queue semantics | Alert labels, dashboards, notification routes, credentials and summary policy |
| Firewalla restricted command dispatch, converters and readiness checks | WAN identities, allowed target, pinned SSH keys and service units |
| DIRT fault lifecycle, observation and independent recovery | Site configuration, inventory, operator authorization and evidence files |
| Generic unit and transport tests | Inventory and provisioning integration tests |

## CLI and configuration

```sh
python3 -m connection_monitoring --version
python3 -m connection_monitoring --config /path/to/config.json validate
python3 -m connection_monitoring --config /path/to/config.json notifier
python3 -m connection_monitoring firewalla-quality --help
python3 -m connection_monitoring firewalla-readiness --help
```

A zipapp exposes the same commands through `python3 /path/to/runtime.pyz`.
Configuration is JSON with integer `version: 1`. Duplicate fields, nonfinite
numbers and unsupported configuration versions fail closed. No configuration
file is loaded implicitly. The restricted gate requires `--allowlist` and reads
`SSH_ORIGINAL_COMMAND`; the SSH forced-command launcher must supply a fixed
allowlist and ignore caller arguments.

The notifier requires an explicit `notifier` object:

| Field | Required contract |
| --- | --- |
| `secrets` | Environment variable names for `telegram_token`, `telegram_chat_id`, `webhook_token`, `grafana_token`, `openrouter_key`; never secret values |
| `delivery_database`, `queue_database` | Persistent absolute SQLite paths; keep them across upgrades |
| `listen` | Loopback `host` and integer `port` |
| `renderer` | Dashboard UID, allowed panel IDs and local Grafana port |
| `queue` | Explicit bounded `capacity` and `retry_delay` |
| `summary` | Two free branches, one paid branch, hedge and race deadlines, zero retention and denied data collection |

Each summary branch declares `model`, disjoint free-branch `providers`,
`request_timeout`, `total_timeout` and `max_price` (`prompt`, `completion`,
`request`). The package validates this contract; a deployment chooses policy.
Configuration examples used by tests contain synthetic IDs and providers, not
recommended services. Existing durable databases are adopted at their configured
paths; replacing an executable must not replace those databases.

Firewalla converters receive explicit `--site`, `--router`, `--target`, and
repeated `--wan UUID=NAME` arguments. `--state` converts native boolean WAN state.
Input arrives on stdin; malformed or conflicting input fails without invented
measurements. Readiness receives a fixed command gate, expected history keys and
a freshness cutoff. The gate confines requests to its declared WANs and target.

## DIRT configuration and use

DIRT uses a version 1 `sites` mapping. A deployment should derive it from its
existing inventory, rather than maintain a second site map. Every site requires:

| Field | Meaning |
| --- | --- |
| `primary`, `wans` | Exactly two named WANs; each declares unique `interface` and `uuid`; primary names one |
| `management_interface` | Declared LAN interface, distinct from both WANs |
| `state_dir`, `timer_prefix` | Router-local recovery storage and timer namespace |
| `router`, `monitor` | SSH `host`, `user`, `known_hosts`, and explicit `key_path`; router requires a key, monitor may declare `null` to preserve default identity/agent authentication |
| `observer` | `collector_unit`, HTTPS `probe_url`, `body_marker`, `grafana_port`, `token_env_file`, `token_env_name`, `delivery_db` |
| `alerts` | One entry per WAN with explicit `labels`, `title` and `uid` |

Use invented site and WAN names in reusable examples:

```sh
python3 -m connection_monitoring --config /path/to/drill.json dirt \
  --site demo-site --suite
python3 -m connection_monitoring --config /path/to/drill.json dirt \
  --site demo-site --wan uplink-b --preflight
python3 -m connection_monitoring --config /path/to/drill.json dirt \
  --site demo-site --suite --execute --output /path/to/new-evidence.jsonl
```

Preview is the default. Preflight reads state and observations. Execution writes
an exclusively created mode 0600 evidence file and stops the suite on failure.
It tests standby loss first, then primary loss, requiring full restoration after
each case. Native WAN UUIDs, roles, exact booleans and the management LAN must
match before arming and injection. SSH uses pinned host keys.

Before a fault is installed, the router saves a mode 0600 standalone recovery
helper containing only that site's validated fault configuration and verifies an
independent recovery timer. The saved helper restores owned IPv4 and IPv6 DIRT
chains even if the source checkout, external configuration and zipapp disappear.
Unknown firewall inspection preserves recovery resources. Cleanup cannot change
a failed or interrupted drill into a pass. Alert observation requires exactly one
matching configured rule title and matching delivery history after drill start.

## Development

```sh
python3 -m pip install pytest
python3 -m pytest tests -q
```

Tests run without deployment credentials or router faults. A few HTTP transport
tests bind loopback sockets, so run them where local sockets are permitted.
Preserve Python 3.8 syntax and standard-library-only runtime dependencies.
Deployment repositories should pin an exact commit, build from committed package
files, and verify artifact provenance and inventory adapters separately.
