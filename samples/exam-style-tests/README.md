# Exam-style test pictures

Each picture has the same parts as `samples/exam-topology.png`: a diagram, an address table
and the requirement note. All the information is there, but each one is harder to read and
uses different addresses, OSPF process and router IDs. `expected.json` holds the answers.

| Picture | What makes it hard |
|---|---|
| `X01-phone-photo.jpg` | Phone photo of the printed page: tilted, slight perspective, shadow, blur, noise |
| `X02-shuffled-table-masks.png` | Rows in a different order with different names ("Host A", "router1 interfaces"), masks like `255.255.255.192`, `/26` `/30` `/25` inside one `/24` |
| `X03-mirrored-pale.png` | Drawn right to left (R1 on the right), pale grey low-contrast print, routers on `.254` |
| `X04-chalkboard.jpg` | Chalkboard photo: dark board, chalk-style text, a list instead of a table |
| `X05-busy-screenshot.jpg` | Screenshot with small text, grid lines, a "DRAFT" watermark, heavy JPEG |
| `X06-scanned-sideways.png` | Page scanned sideways (turned about 90°) |
| `X07-names-in-diagram-numbers-in-table.png` | The diagram shows only segment names (LAN-A, TRANSIT); the numbers are in the table; `/29` link |
| `X08-low-resolution.png` | Shrunk to half size and enlarged again: soft, blocky letters |

Run them against the running app (stops before Approve, routers are not changed):

```bash
python scripts/test_pictures.py samples/exam-style-tests
```

## Results (DeepSeek Flash + Typhoon OCR, picture only, 2026-11-17)

Two full runs: every picture 12/12 correct both times (16/16), no wrong blocks.
