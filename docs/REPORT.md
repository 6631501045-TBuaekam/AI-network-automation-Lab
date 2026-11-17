# Report - AI Network Automation with OSPF

**Lab:** GNS3 3.1, 2 Cisco 7200 routers, 2 VPCS PCs and a management switch.
The system runs in Docker on the GNS3 PC.

## 1. Goal
A web system that turns a picture of a network diagram into working Cisco OSPF config.
The AI only reads the picture. Every config is checked and needs an admin's approval.
Then it goes through Kafka to a Python worker, which sets up the routers over SSH.

## 2. Parts

| Part | Made with | What it does |
|---|---|---|
| Web page | HTML + JavaScript | Upload, Run, Generate, Review, Approve, Deploy, job list |
| Backend | Python, FastAPI, SQLite | Calls the AI, writes and checks configs, handles approval, sends jobs to Kafka |
| AI | Any AI that reads pictures (Gemini, DeepSeek, Claude, OpenAI, or a local one). Tested with Qwen2.5-VL 7B (local) and DeepSeek Flash with Typhoon OCR | Reads the picture and returns the devices, networks, IPs, links and OSPF settings |
| Kafka | Apache Kafka 3.8, topic `network-config-jobs` | Carries approved jobs to the worker. Kafka UI shows them |
| Worker | Python, Paramiko | Takes jobs, checks approval again, sets up the routers over SSH, checks OSPF |
| OCR (optional) | Typhoon OCR | Reads the text in the picture so the AI copies numbers instead of guessing them |
| GNS3 check (optional) | GNS3 API, read only | Compares the AI's result with the real cables and suggests ports |
| Lab PCs (optional) | GNS3 console of each VPCS PC | On Deploy, the worker gives the PCs the plan's addresses (approved with the routers) |

## 3. How a run works
1. **Upload** - the picture (PNG/JPG) and the requirements text.
2. **Generate** - the AI reads the picture. It must not guess: anything unclear is listed,
   and "blocking" items stop approval. The worker then reads the real port names from
   each router (`show` commands only).
3. **Review** - the backend matches each network to a router port, writes the config and
   checks it. The admin confirms the ports. (**Run** does steps 1-3 in one click.)
4. **Approve** - needs the admin token, a name and a tick. The checks run again. The
   approval is locked to the exact config: any change cancels it.
5. **Kafka** - one job per router. The job points to the approved config.
6. **Worker** - takes each job only once, asks the backend to confirm the approval, checks
   the config wasn't changed, backs up the router, applies the config line by line, saves
   it, and logs in again to prove SSH still works. If the router changed since Review (for
   example an old OSPF process is back), it stops without changing anything. If the PCs'
   consoles are in the inventory, it also sets the PCs to the plan's addresses.
7. **Verify** - runs `show ip ospf neighbor`, `show ip route ospf` and
   `show ip protocols` until OSPF is up. It checks for FULL neighbors, the other LAN in the
   route table, the router ID and the passive ports.

## 4. The 10 lab tasks

| Task | How it is done |
|---|---|
| 1 Web page | Upload with preview; Generate, Review, Approve, Deploy and Run buttons |
| 2 AI analysis | AI that reads pictures; result shown on the page; unclear values listed, not guessed |
| 3 IOS and OSPF | Ports per router, OSPF process 1 area 0, three networks, LAN ports passive |
| 4 Checks and approval | IPs, subnets, real port names, OSPF settings, management reachable; configs shown; approval needed |
| 5 Kafka producer | Topic `network-config-jobs`; job ID, router ID, link to the config |
| 6 Worker | Takes jobs, checks approval again, reports progress, blocks duplicates |
| 7 Paramiko | Sets up routers over SSH, collects output, handles login and timeout errors, no passwords in code |
| 8 OSPF check | The three `show` commands; FULL neighbors and the other LAN learned |
| 9 Job list | PENDING / RUNNING / SUCCESS / FAILED with router, times, output and results |
| 10 End to end | Shown live: PC1 pings 10.2.0.10 (section 7) |

## 5. Example config (R1 - see evidence/10-run2-review-cleanup.png)
```
no router ospf 1                      <- removes the old plan
default interface FastEthernet0/0     <- (never the management port)
default interface FastEthernet1/0
hostname R1
interface FastEthernet0/0
 description LAN PC1
 ip address 10.0.0.1 255.255.255.0
 no shutdown
interface FastEthernet1/0
 description Link to R2
 ip address 10.1.0.1 255.255.255.0
 ip ospf network point-to-point
 no shutdown
router ospf 1
 router-id 1.1.1.1
 network 10.0.0.0 0.0.0.255 area 0
 network 10.1.0.0 0.0.0.255 area 0
 passive-interface FastEthernet0/0
```
R2 is the same with router ID 2.2.2.2 and networks 10.1.0.0/24 and 10.2.0.0/24.
* The port names were read from the routers, not typed by anyone.
* The first three lines remove the old plan, so a new plan fully replaces it.
* `ip ospf network point-to-point` makes R1 and R2 FULL neighbors within seconds.

## 6. Safety
* The AI can't reach the routers. Only the worker has the router password, and it only
  runs jobs the backend confirms.
* Passwords and keys are only in `.env`, which is not in git or the submission.
* Only interface, IP and OSPF commands are allowed. Static routes, the management port,
  SSH settings and `reload` are refused.
* Changing the plan after approval cancels the approval. A changed config is refused by
  both the backend and the worker.
* A wrong password is never retried. Network errors are retried. The router is backed up
  first, and nothing is saved if a command fails, so `reload` brings back the last good
  config.

## 7. Test results

### 7.1 Live test in GNS3 (2026-11-14)
The routers were reset to the starting state (LAN and link ports empty, no OSPF,
management on). Everything was done **through the web page**: upload → AI (38 s, nothing
unclear) → read port names → Review (all checks passed) → confirm ports → Approve →
Deploy → verify. 80 seconds from upload to done. No router was set up by hand.

| Check | Result | Evidence |
|---|---|---|
| Web page accepts the diagram | ✔ | evidence/01-upload.png |
| AI read the topology | ✔ | evidence/02-ai-json.png |
| Checks passed, configs shown | ✔ | evidence/03-review.png |
| Admin approved | ✔ | evidence/04-approve.png |
| Kafka jobs visible (R1 and R2) | ✔ | evidence/05-kafka.png |
| Worker set up R1 and R2, verify SUCCESS | ✔ | evidence/06-jobs.png, evidence/06b-verify-details.png |
| OSPF neighbors FULL (R1 ↔ 2.2.2.2, R2 ↔ 1.1.1.1) | ✔ | evidence/07-ospf-neighbor.png |
| Other LAN learned by OSPF (R1: 10.2.0.0/24, R2: 10.0.0.0/24) | ✔ | evidence/08-ospf-routes.png |
| PC1 → 10.2.0.10 ping, 5 of 5 replies | ✔ | evidence/09-ping.png |

Router and PC output is in `evidence/*.txt`; the jobs are in `evidence/run-jobs.json`.

**Run** was then used twice in a row: first on reset routers, then on top of the first
run's config. Both finished with all jobs SUCCESS, and the second config started with the
lines that remove the old plan (evidence/10-run2-review-cleanup.png,
evidence/11-run2-jobs.png). The ping still worked afterwards.

### 7.2 Live test with different pictures (2026-11-17)
Three demo pictures were deployed one after another with DeepSeek Flash + Typhoon OCR,
picture only, nothing typed on the routers or the PCs: `D4-shuffled-table-masks.png`
(/26, /30, /25 inside one /24), `D2-phone-photo.jpg` (phone photo), then
`D1-exam-official.png`. Each plan removed the previous one, set both PCs, and finished
with all jobs SUCCESS, OSPF FULL and the other LAN learned on both routers (about 40 s per
deploy). After D1, PC1 → 10.2.0.10 replied. The unclear picture `D8-UNCLEAR-conflicts.png`
was stopped at Review, as intended.

### 7.3 Problems found and fixed
* **SSH sessions left open.** Each job left one SSH session open, so the router ran out
  and refused R1's verify. Fixed in `worker/ssh_client.py`.
* **Small AIs asking for port names.** Port names come from the routers, so this note is
  now ignored instead of blocking approval.
* **Local AI misreading an address.** From the picture alone, Qwen2.5-VL 7B sometimes
  misread one address (the checks caught it). With the requirements text added, it was
  always correct.
* **AI guessing or misreading values.** The AI must now say which addresses are written
  in the picture and which it inferred; an inferred router or PC address blocks approval,
  and every hand correction (crossed-out value) is shown as a warning. Typhoon OCR fixed
  DeepSeek misreading hand-drawn numbers.
* **Old OSPF process left on a router.** A router read could come back empty, so the next
  plan did not remove the old process. Reads now wait for the command's echo, Generate
  always reads the routers again, and Deploy refuses a router that changed since Review.
* **PC ping 0% after a new picture.** The PCs kept the last plan's addresses. Deploy now
  sets them too.

## 8. Conclusion
The whole chain works: Upload → AI → Review → Approve → Kafka → Worker → SSH → Verify,
with no router set up by hand and no static routes. It was shown live in GNS3 with a
working PC1 → PC2 ping. It also works with other networks, because port names are read
from the routers and the plan comes from the checked, admin-confirmed AI result.
