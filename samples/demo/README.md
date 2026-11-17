# Demo pictures

Eight pictures for a live demo. They look very different from each other, and the first
seven were read correctly in every test run. The last one is unclear on purpose: it shows
that the system stops instead of guessing.

All of them use two routers (R1, R2) and two PCs, so each one can be deployed to the lab.
Upload the picture only - leave "Written requirements" empty.

## Order

| # | Picture | What it shows | After Generate you should see |
|---|---|---|---|
| 1 | `D1-exam-official.png` | The official exam hand-out | No blocking items |
| 2 | `D2-phone-photo.jpg` | A phone photo of a printed sheet: tilted, shadow, blur | Same quality result from a bad photo |
| 3 | `D3-chalkboard.jpg` | A chalkboard with a list instead of a table | Works with any layout |
| 4 | `D4-shuffled-table-masks.png` | Rows in a different order, masks like `255.255.255.192`, `/26` `/30` `/25` | The AI works out the networks itself |
| 5 | `D5-vertical-masks-only.png` | Drawn top to bottom, no network labels, masks only | Same |
| 6 | `D6-names-and-table.png` | The diagram has only names; the numbers are in the table | The AI joins the two |
| 7 | `D7-hand-corrections.png` | Values crossed out and rewritten in red | Warnings like "hand correction: R1 router-id 4.4.4.1 is crossed out, replaced by 4.4.4.4" |
| 8 | `D8-UNCLEAR-conflicts.png` | R2's address is outside its `/30`; PC2's gateway doesn't match R2 | **Blocking** items; Approve is not possible |

Warnings such as "gateway inferred from the router address" are normal: they tell the reviewer
what the AI assumed.

## Deploying one of them

The routers are set by the system. The PCs (VPCS) are not: before the ping, give them the
addresses from the picture. On each PC console:

| # | PC1 | PC2 | Ping from PC1 |
|---|---|---|---|
| 1 | `ip 10.0.0.10/24 10.0.0.1` | `ip 10.2.0.10/24 10.2.0.2` | `ping 10.2.0.10` |
| 2 | `ip 172.16.10.10/24 172.16.10.1` | `ip 172.16.30.10/24 172.16.30.1` | `ping 172.16.30.10` |
| 3 | `ip 10.50.10.50/24 10.50.10.1` | `ip 10.50.20.50/24 10.50.20.1` | `ping 10.50.20.50` |
| 4 | `ip 192.168.1.20/26 192.168.1.1` | `ip 192.168.1.200/25 192.168.1.129` | `ping 192.168.1.200` |
| 5 | `ip 192.168.50.66/26 192.168.50.65` | `ip 192.168.60.200/25 192.168.60.129` | `ping 192.168.60.200` |
| 6 | `ip 172.31.1.10/24 172.31.1.1` | `ip 172.31.2.10/24 172.31.2.1` | `ping 172.31.2.10` |
| 7 | `ip 10.40.0.10/24 10.40.0.1` | `ip 10.48.0.100/24 10.48.0.254` | `ping 10.48.0.100` |

Each new plan removes the previous plan's OSPF and port addresses first, so you can deploy
them one after another without resetting the routers.

Check that every picture still gives the expected result (stops before Approve):

```bash
python scripts/test_pictures.py samples/demo
```
