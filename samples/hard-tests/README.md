# Hard test diagrams

Paint-style diagrams for testing the AI. Each one is uploaded **without** requirements text.
`expected.json` holds the correct answer for each picture.

| Picture | What makes it hard | Result (Gemini, 2026-11-16) |
|---|---|---|
| `01-handdrawn-zigzag.png` | Shaky lines, tilted labels, a /27 LAN, values spread out | 12/12 correct |
| `02-dotted-masks-vertical.png` | Masks like `255.255.255.192`, no network labels, drawn top to bottom | 12/12 correct |
| `03-switch-mgmt-legend.png` | Two PCs on a switch, a management cloud to ignore, a legend | 14/15 - added the management network to the plan, so Review blocked it |
| `04-crossed-out-corrections.png` | Values crossed out and rewritten in red | 12/12 correct (used the new values) |
| `05-UNCLEAR-missing-values.png` | R2's link IP is `.?` on a /29; no OSPF process or router IDs | Stopped - AI marked all 4 missing values as blocking |
| `06-UNCLEAR-conflicts.png` | R2's IP is outside the /30; PC2's gateway doesn't match R2 | Stopped - AI and Review both caught both problems |
| `07-bad-photo.jpg` | Tilted, blurry, small, low-quality JPEG, grey paper | 12/12 correct |
| `08-three-routers.png` | Three routers in a triangle, tilted labels | 17/17 correct. Can't be deployed: the lab has only R1 and R2 |

"Correct" counts every router IP, PC IP and gateway, OSPF process, area and router ID.
Every test stopped before approval. Nothing wrong could reach the routers.
