# CEDAR reference fixture

`SimpleTemplate.json` is the unmodified upstream CEDAR Artifact Library fixture
at commit ad9d9a114a729176ffb4f03f6e81323ed41c97f3:
https://github.com/metadatacenter/cedar-artifact-library/blob/ad9d9a114a729176ffb4f03f6e81323ed41c97f3/src/test/resources/templates/SimpleTemplate.json
Raw SHA-256: 22a00307d2ed8b8116b709e34b3e7dcaa46d4df8a4f0eb295025fefcbad71798
See LICENSE.txt for the source license. It includes examples of `classes`,
`branches`, defaults, min/max length and temporal fields. It is not research data.

The unmodified `template-meta-schema.json` is from the CEDAR Model Validation
Library at commit `3932db823a0da4a584bc9587e73c907e212d3ad1`:
https://github.com/metadatacenter/cedar-model-validation-library/blob/3932db823a0da4a584bc9587e73c907e212d3ad1/src/main/resources/template-meta-schema.json
SHA-256 `602d86e62f7a9662050550c98899ec131b0cd2770703de6564e421a6acd4604d`.
It recursively validates the template and nested fields offline under Draft 4.
Its separate license is `MODEL-VALIDATION-LICENSE.txt`. Export construction uses
shapes derived from `SimpleTemplate.json` and the upstream Artifact Library's
`src/test/resources/elements/element-004.json` at commit
`ad9d9a114a729176ffb4f03f6e81323ed41c97f3` (Git blob SHA-256
`e803cc85b6cfdd1d62169135f1475ee37f9baa023c6e7302be0d648a56ce6ee3`).
No upstream instance identity, author or timestamp is attributed to the draft.
