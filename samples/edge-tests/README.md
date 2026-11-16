# Edge-case test diagrams

Paint-style diagrams for the tricky cases, mostly hand corrections. Each one is uploaded
**without** requirements text. `expected.json` holds the correct answer for each picture,
and which warnings must (or must not) appear.

Run them, together with the hard tests, against the running app:

```bash
python scripts/test_pictures.py samples/hard-tests samples/edge-tests
```

It stops before Approve, so the routers are never changed.

| Picture | Edge case | What must happen |
|---|---|---|
| `E01-strike-right-black.png` | R1's `.1` struck with one black line, new `.9` written to its right, also in black | Use `.9`; warn about the correction |
| `E02-arrow.png` | R2's `.9` struck, arrow down to the new `.6` | Use `.6`; warn |
| `E03-chain.png` | PC2 changed twice: `.10` struck, `.20` struck, `.30` | Use the last one, `.30`; warn |
| `E04-UNCLEAR-crossed-no-new.png` | R2's LAN address crossed out, nothing written instead | **Stop** (blocking) |
| `E05-network-label-corrected.png` | The link network itself is corrected (`10.55.0.0/30` → `10.55.9.0/30`) | Build `.1`/`.2` on the new network; warn |
| `E06-ospf-values-corrected.png` | OSPF process `6` → `16`; R2 router ID `6.6.6.2` → `6.6.6.20`, written below | Use the new values; warn |
| `E07-UNCLEAR-bad-correction.png` | R2's link address corrected to `.7`, which is outside the /30 | **Stop** |
| `E08-device-removed.png` | A third PC and its LAN crossed out with a big X and "removed" | Leave PC3 and its network out |
| `E09-red-ink-no-corrections.png` | Everything written in red, nothing struck | Read normally; **no** correction warnings |
| `E10-UNCLEAR-gateway-not-updated.png` | R2's LAN `.1` → `.254`, but the note still says PC2's gateway is `.1` | **Stop**: the picture contradicts itself |
| `E11-UNCLEAR-scribbled.png` | R1's link address scribbled over, nothing instead | **Stop**, even though `.1` is the only free address in the /30 |

## How the rules are enforced

The AI reports, for every address, whether it is **written** in the picture or **inferred**,
and lists every hand correction (old → new). The backend then decides, so the safety does
not depend on the AI remembering to raise a warning:

* An inferred **router or PC address** blocks the plan.
* An inferred **gateway** is a warning (normal for "gateway = router LAN IP").
* Every **hand correction** becomes a warning that names the old and the new value.

## Results (DeepSeek Flash + Typhoon OCR, picture only, 2026-11-17)

Three full runs with the final rules. "OK" = right values, right warnings; "blocked" = a clear
picture was stopped (safe, but needs Edit JSON); "**slipped**" = an unclear picture was not stopped.

| Picture | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| E01 strike, right, black | OK | OK, but no correction warning (the AI said "unclear mark" instead) | OK |
| E02 arrow | OK | OK | OK |
| E03 chain | OK | OK | OK |
| E04 crossed, nothing new | stopped | stopped | stopped |
| E05 network corrected | OK | OK | OK |
| E06 OSPF values corrected | blocked (read "6 16" as "6.16") | OK | blocked (same) |
| E07 bad correction | stopped | stopped | stopped |
| E08 device removed | OK | OK | OK |
| E09 red ink | OK | OK | OK |
| E10 gateway not updated | stopped | stopped | stopped |
| E11 scribbled | stopped | stopped | **slipped** - the AI claimed R1's hidden address was written (`.1`) |

The 8 hard tests in the same runs: all correct or stopped, except two safe blocks (01 once,
08's R3 twice) where the AI could not tie a short label to its router.

**Known limit:** a value that is completely blacked out can still be "read" by the AI as the
most likely number (E11, 1 run in 3). Nothing checks this automatically - the OCR loses too
many short labels (`.1`, `.2`) on other pictures to count them reliably. The person reviewing
the config is the last check: compare every address with the picture before Approve.
