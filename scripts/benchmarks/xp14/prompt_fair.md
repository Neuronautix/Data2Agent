You are assessing a dataset for FAIR compliance.

Return a verdict for each of the twelve indicators listed below. Every verdict
must be one of exactly four values:

- `pass` — the dataset satisfies the indicator, and you observed what shows it.
- `fail` — the dataset does not satisfy the indicator, and you observed what
  shows that.
- `unknown` — you could not establish either, from what you were able to observe.
- `not_applicable` — the indicator does not apply to this dataset.

Indicators:

| rule_id | principle |
|---|---|
| F1-PID-METADATA | F1 |
| F1-PID-RESOLVABLE | F1 |
| F2-METADATA-PRESENT | F2 |
| F3-METADATA-LINKS-DATA | F3 |
| F4-METADATA-MACHINE-READABLE | F4 |
| A1-RETRIEVAL-PROTOCOL | A1 |
| I1-DATA-FORMATS-OPEN | I1 |
| I2-VOCABULARY-REFERENCED | I2 |
| R1.1-LICENCE-DECLARED | R1.1 |
| R1.2-PROVENANCE-DECLARED | R1.2 |
| R1.3-COMMUNITY-STANDARD | R1.3 |
| R1.3-MISSING-VALUES-DECLARED | R1.3 |

Base every verdict on this dataset as you find it. Do not report a value you did
not observe, and do not fill a gap from what a dataset of this kind usually
contains. If you report that something is present, you must have seen it.

Reply with JSON and nothing else — no preamble, no code fence, no commentary:

```
{"results": [{"rule_id": "...", "result": "...", "rationale": "..."}]}
```

Keep each `rationale` to one or two sentences, naming what you looked at.
