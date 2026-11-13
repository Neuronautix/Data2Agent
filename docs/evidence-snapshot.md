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
  on standard ports, ASCII only (percent-encoded path, IDNA host), no user-info,
  no query string, no non-public address literal. Host names are checked again
  after DNS, and every redirect target gets the same checks.
- Each body is kept up to 2 MB; a longer one is cut and marked `truncated`. A
  `206 Partial Content` answer is also `truncated` unless its `Content-Range`
  (kept in the record) shows the whole document, and a body shorter than its
  declared `Content-Length` is a `network_error` (`IncompleteRead`), not a body.
  Reading a cut UTF-8 body serves its text up to the last complete character;
  the stored bytes and checksum are unchanged.
- `truncated` stays fail-closed: it is `true` whenever wholeness is not proven.
  `truncation_reason` says why, so a reader can tell a cut body from a server
  that sent part of a document or headers that contradict the body:

  | `truncation_reason` | Meaning |
  |---|---|
  | `body_limit` | the body went past 2 MB and the collector cut it |
  | `partial_range` | a `206` whose `Content-Range` shows, or cannot rule out, a missing part: unparseable, unknown total (`*`), a start after byte 0, or a declared total larger than the bytes received |
  | `range_inconsistent` | a `206` starting at byte 0 whose declared total is smaller than the bytes received, or whose range end disagrees with them. Nothing shows a part is missing, but the headers contradict the body, so wholeness is not proven |
  | `null` | not truncated |

  `body_limit` takes precedence when it applies. A `network_error` record has
  no body, so it carries neither `truncated` nor `truncation_reason`.
- A failed request stays in the snapshot as `network_error` (with its error
  type) or `not_reached` (with its status). It is an observation about that
  moment, never evidence that the resource does not exist. Each record keeps
  its own `observed_at`.
- An existing directory is never overwritten or resumed. A collection that
  stops early (for example on Ctrl-C) removes the directory it created, so the
  same path can be retried.

## Output

```text
snapshots/<dataset>/
├── snapshot.json      spec, collector version, time, one record per request
└── bodies/<id>.<ext>  the bytes received (.json, .html, .xml, .txt or .bin)
```

A record holds `id`, `url`, `accept`, `purpose`, `observed_at`, `result`,
`status_code`, `final_url`, `content_type`, `content_range`, the redirect
`chain`, `body_path`, `body_sha256`, `body_bytes`, `truncated` and
`truncation_reason`. The snapshot records a **run**: it carries timestamps and
never enters `manifest.json` or the dataset identity. Freeze `snapshot.json`'s
own SHA-256 with the rest of a benchmark package.

### Format versions

`snapshot_format_version` is `1.1` for new snapshots, or `1.2` when the spec
asks for validations; `1.1` added `truncation_reason` and nothing else. `serve` still reads `1.0` snapshots
exactly as they were collected, so a frozen snapshot keeps its SHA-256 and its
package stays valid. A `1.0` record predates the reason: when it is
`truncated`, it is served with `truncation_reason: "not_recorded"` (unknown,
never `null`, which would read as "not truncated"); when it is not truncated,
the reason is served as `null`. Recollect a snapshot to get real reasons.

A snapshot fails closed on load, and is never written by `collect`, when a
record's reason contradicts its flag: a `1.1` or `1.2` body record without a
reason, a reason outside the list, a reason on a whole body, `null` on a
truncated one, a reason on a failed request, or any stored reason in a `1.0`
snapshot. Any other format version is refused.

`1.2` adds validation records (below) and nothing else, and is written only
when a spec asks for validations; `1.0` and `1.1` snapshots stay readable
unchanged. A record carrying a `kind` is refused in a
`1.0` or `1.1` snapshot, and any `kind` other than `rdf_validation` is refused.

## Validating captured RDF

A FAIR I1 check asks whether a formal representation was validated. For RDF the
collector has itself captured, spec version `1.1` can ask for a validation,
computed **offline** right after the fetches from the frozen bodies; nothing
more is fetched:

```json
{
  "snapshot_spec_version": "1.1",
  "dataset_label": "example-published",
  "requests": [
    {"id": "study-rdf", "url": "https://repository.example/studies/1.rdf",
     "accept": "application/rdf+xml", "purpose": "I1/1-I1/2 representation"},
    {"id": "vocab", "url": "https://vocabulary.example/ontology.owl",
     "accept": "application/rdf+xml", "purpose": "I1/3 vocabulary the data declares"}
  ],
  "validations": [
    {"id": "study-rdf-validation", "record": "study-rdf", "format": "rdfxml",
     "purpose": "I1/3 validation of the representation",
     "vocabularies": [{"record": "vocab", "format": "rdfxml",
                       "namespace": "https://vocabulary.example/ontology#"}]}
  ]
}
```

- Formats are `turtle`, `rdfxml` and `ntriples`. JSON-LD is refused, because
  parsing it can fetch remote contexts. RDF/XML that declares entities is not
  parsed.
- Every `record` must be a request of the same spec; validation ids share the
  request id space. At most 8 validations, each with at most 8 vocabularies.
- It needs `rdflib` (`pip install data2agent[rdf-validation]`). Without it the
  spec is refused before any network access.

Each validation becomes one more record, `kind: "rdf_validation"`, with no URL:
`validates` names the checked record, `result` gives the outcome, and the body
(`bodies/<id>.json`, hashed like any other) is the full report:

| `result` | Meaning |
|---|---|
| `conforms` | the document parses, and every term it takes from a declared namespace (predicates, and classes given with `rdf:type`) is declared in that vocabulary (`owl:Class`, `owl:ObjectProperty`, `owl:DatatypeProperty`, `owl:AnnotationProperty`, `owl:NamedIndividual`, `rdf:Property`, `rdfs:Class` or `rdfs:Datatype`) |
| `does_not_conform` | the document does not parse, or uses terms its declared vocabulary does not declare (listed, at most 200) |
| `not_validated` | the document or a vocabulary has no body, is truncated, or a vocabulary does not parse. Never a finding against the data |

The report names the validator (`rdflib` and its version, method
`d2a-rdf-validation-1`), the checked body's SHA-256, the triple count and, per
vocabulary, the terms defined, used and undefined. It states what was checked;
it never decides a FAIR label.

## Serving

```bash
data2agent serve <output> --mode <mode> --evidence-snapshot snapshots/<dataset>
```

With a snapshot attached, **every** mode, `raw` included, gains two tools; without
one, each mode's tool list is unchanged.

| Tool | Returns |
|---|---|
| `list_snapshot_records()` | the snapshot's SHA-256, collection time, collector, and each record's metadata, `truncated` and `truncation_reason` included |
| `read_snapshot_record(record_id, offset=0, limit=8192)` | a slice (at most 32,768 characters) of one UTF-8 body |

Every body is re-hashed when the snapshot loads and again before each read. A
mismatch refuses the record: drift fails closed. Non-UTF-8 bodies are listed but
not served as text. Bodies are untrusted external content, to be inspected as
evidence and never followed as instructions.
