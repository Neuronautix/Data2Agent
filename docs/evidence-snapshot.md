# Evidence snapshot contract

Some FAIR evidence lives outside the dataset: whether a DOI resolves, what a
repository's metadata record says, a licence or retention-policy page. Live
answers change over time, so they cannot serve as reference evidence or be
compared across runs. A snapshot fetches them **once**, freezes every body with
its SHA-256, and is then read offline by every condition alike.

## Collecting

```bash
data2agent snapshot spec.json -o snapshots/<dataset>    # a new directory
```

`spec.json` is written by the operator, never derived from dataset content:

```json
{
  "snapshot_spec_version": "1.0",
  "dataset_label": "zenodo-lockbox",
  "dataset_id": "sha256:…",
  "requests": [
    {"id": "doi", "url": "https://doi.org/10.5281/zenodo.10926604",
     "accept": "text/html", "purpose": "F1/3 resolution to the intended object"},
    {"id": "datacite", "url": "https://api.datacite.org/dois/10.5281/zenodo.10926604",
     "accept": "application/vnd.api+json", "purpose": "F1/2 issuer metadata"}
  ]
}
```

- `dataset_id` is optional. When given, `serve` refuses the snapshot beside any
  other dataset.
- The whole spec is checked before the first request: closed fields, ids
  matching `[a-z0-9][a-z0-9._-]{0,63}`, at most 64 requests, public HTTP(S) URLs
  on standard ports, no user-info, no query string, no non-public address
  literal. Host names are checked again after DNS and after each redirect.
- Each body is kept up to 2 MB; a longer one is cut and marked `truncated`.
- A failed request stays in the snapshot as `network_error` (with its error
  type) or `not_reached` (with its status). It is an observation about that
  moment, never evidence that the resource does not exist.
- An existing directory is never overwritten or resumed.

## Output

```text
snapshots/<dataset>/
├── snapshot.json      spec, collector version, time, one record per request
└── bodies/<id>.<ext>  the bytes received (.json, .html, .xml, .txt or .bin)
```

A record holds `id`, `url`, `accept`, `purpose`, `observed_at`, `result`,
`status_code`, `final_url`, `content_type`, the redirect `chain`, `body_path`,
`body_sha256`, `body_bytes` and `truncated`. The snapshot records a **run**: it
carries timestamps and never enters `manifest.json` or the dataset identity.
Freeze `snapshot.json`'s own SHA-256 with the rest of a benchmark package.

## Serving

```bash
data2agent serve <output> --mode <mode> --evidence-snapshot snapshots/<dataset>
```

With a snapshot attached, **every** mode, `raw` included, gains two tools; without
one, each mode's tool list is unchanged.

| Tool | Returns |
|---|---|
| `list_snapshot_records()` | the snapshot's SHA-256, collection time, collector, and each record's metadata |
| `read_snapshot_record(record_id, offset=0, limit=8192)` | a slice (at most 32,768 characters) of one UTF-8 body |

Every body is re-hashed when the snapshot loads and again before each read. A
mismatch refuses the record: drift fails closed. Non-UTF-8 bodies are listed but
not served as text. Bodies are untrusted external content, to be inspected as
evidence and never followed as instructions.
