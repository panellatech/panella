# K1 resolver taxonomy v2

`taxonomy.yaml` is the authoritative, machine-validated taxonomy. This document
explains its intent only.

The generic framework is Tiago Forte's **PARA** (Projects, Areas, Resources,
Archives). PARA is used here only as a personal-information organising frame:
stable Areas such as health, home, work, relationships, and finance are split
into specific resolver slots. It is not a source of example keys or aliases.

The two evidence-backed breadth additions are kept deliberately narrow:
`digital_services` covers durable personal service choices, and
`entertainment_media` covers media and entertainment preferences. All other
domains derive from the generic PARA framing.

| Domain | Provenance |
| --- | --- |
| identity | generic: PARA |
| health | generic: PARA |
| household | generic: PARA |
| work | generic: PARA |
| transport | generic: PARA |
| devices | generic: PARA |
| digital_services | findings:digital-services |
| schedule | generic: PARA |
| social | generic: PARA |
| pets | generic: PARA |
| hobbies_lifestyle | generic: PARA |
| finance | generic: PARA |
| safety_constraints | generic: PARA |
| entertainment_media | findings:entertainment/media |

Slot allocation, aliases, and blocking terms are validated from the YAML
artifacts; this prose is intentionally non-authoritative.
