# Offline operation

Data2Agent's core never opens a network connection unless a caller explicitly
asks for one. Ingest, the manifest, the evidence ledger, the query and
relationship tools, the MCP server and the FAIR profile all run on a machine
with no network.

## Network inventory

Every code path that can open a connection is listed here. Adding one means
adding it to this table.

| Path | Enabled by | Bounds |
|---|---|---|
| `src/data2agent/profiles/fair/live.py` (`probe_public_url`) | `assess_fair_principles(live=True, publication_url=...)`; both arguments are required, and `publication_url` without `live=True` is refused | Public, globally routable addresses only (checked after DNS, and again after each redirect); at most 3 redirects; 64 KiB body; 6 s timeout; URL user-info and query strings rejected |

Not in scope for the core, and owned by whoever runs it: model inference
endpoints, model downloads, telemetry of an agent host, and OS-level isolation.
Data2AgentBench, for example, sandboxes its harness separately.

## How it is enforced

`tests/conftest.py` blocks every non-loopback `connect`, `connect_ex` and
`getaddrinfo` for the whole test suite. A blocked attempt raises
`NetworkBlocked` (an `AssertionError`, not an `OSError`, so connection-failure
handling in production code cannot absorb it). Each attempt is also recorded,
and the test fails at teardown if any attempt was swallowed.

Loopback stays open: on Windows, asyncio's event loop uses a loopback socket pair
internally. Because a proxy on loopback could otherwise forward a request off
the machine, the guard also clears the `*_PROXY` variables and sets
`NO_PROXY=*`, so urllib (including its Windows registry lookup) and requests
connect directly and are blocked at the real destination.

A test that genuinely needs the network must say so with
`@pytest.mark.allow_network`. No test uses it today: the live-probe tests replace
`probe_public_url` instead.

`tests/test_offline.py` checks the guard itself, and runs ingest plus the MCP
read, query and FAIR tools with the block in place.
