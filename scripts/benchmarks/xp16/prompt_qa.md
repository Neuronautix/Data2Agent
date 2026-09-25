You are answering questions about one preclinical dataset. You can reach it only
through the `data2agent` tools available to you. Do not guess: every value you
report must come from the data the tools return.

For each question below:

- Answer with the value the data support, in the requested `answer_type`:
  `integer` / `number` -> a JSON number; `set` -> a JSON list of strings;
  `table` -> a JSON list of row objects; `object` -> a JSON object;
  `boolean` -> true/false.
- Where a question asks for a group summary, first reduce to one value per
  animal (the unit of analysis), then summarise across animals; report `n` as
  the number of animals. SEM = sample standard deviation / sqrt(n).
- If the data cannot determine the answer -- a value is missing, the files
  contradict each other, or the answer depends on a decision only the dataset
  owner can make -- do not answer. Set `"abstain": true` and say why in
  `"reason"`. A confident answer to an undeterminable question is scored below
  an abstention.
- Cite where each answer comes from: `"sources"` is a list of
  `{"file", "sha256", "sheet", "cell" | "range"}` for workbooks, or
  `{"file", "sha256", "line", "field"}` for delimited files, or
  `{"file", "sha256", "observation_id", "start_event_index", "stop_event_index"}`
  for a behaviour interval of a BORIS project. Use the `sha256` the tools
  report for the file.

Reply with JSON only, no prose around it:

```json
{"answers": {"<question id>": {"answer": ..., "sources": [...]},
             "<question id>": {"abstain": true, "reason": "..."}}}
```

Questions (JSON, one object per question):

{questions_json}
