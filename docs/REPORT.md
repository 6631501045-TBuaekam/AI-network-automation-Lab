# Report - AI Network Automation with OSPF

**Student:** ______________________  **Date:** ____________

**Lab:** GNS3 3.1 with 2 Cisco 7200 routers and 2 VPCS PCs, plus a management switch.
The system runs with Docker Compose (tested on Docker Desktop on the GNS3 PC).

## 1. Goal
Build a web system that turns a network diagram picture into working, checked Cisco OSPF
configuration. The AI only reads the picture. Every change is checked and needs an admin's
approval. Only then does it go through Kafka to a Python worker, which uses SSH (Paramiko).

## 2. Parts of the system

| Part | Made with | What it does |
|---|---|---|
| Web page | HTML + JavaScript | Upload and preview, Run, Generate, Review, Approve, Deploy, job monitor |
| Backend | Python, FastAPI, SQLite | Calls the AI, builds the network model, writes configs, checks them, handles approval, sends Kafka jobs |
| AI | Any model that reads images: Claude, OpenAI, other OpenAI-style APIs, or a local model. **Test run: Qwen2.5-VL 7B (local)** | Picture → structured JSON (devices, networks, IPs, links, routing, unclear items) |
| Kafka | Apache Kafka 3.8, topic `network-config-jobs` | Carries approved jobs from the backend to the worker. Kafka UI shows the messages |
| Worker | Python, Paramiko | Reads jobs, re-checks approval, connects with SSH, deploys and verifies |
| GNS3 check (optional) | GNS3 API, read only | Compares the AI's topology with the real lab cables and suggests ports. Warnings only |

## 3. How a run works
1. **Upload** - the picture (PNG/JPG) and optional hand-out text.
2. **Generate** - the AI turns the picture into JSON. It must not guess: anything unclear
   is listed as an "ambiguity" (blocking ones stop approval). Then the worker reads the
   real interface names from each router over SSH, using only `show` commands.
3. **Review** - the backend matches each network to a real router port, writes the
   configuration and checks it. The admin confirms the port mapping.
   The **Run** button does steps 1-3 in one click and stops here.
4. **Approve** - needs the admin token, a name and a tick box. The checks run again.
   The approval is tied to a fingerprint (SHA-256) of the exact configs.
5. **Kafka** - one job per router. The job holds a reference to the approved config,
   not the config text.
6. **Worker** - takes the job (only once - no duplicates), asks the backend to confirm the
   approval, checks the config fingerprint, saves a backup of the router config, applies
   the config line by line, saves it, then opens a new SSH session to prove management
   still works.
7. **Verify** - the worker runs `show ip ospf neighbor`, `show ip route ospf` and
   `show ip protocols` until OSPF is ready. It checks FULL neighbors, the remote LAN as an
   OSPF route, process, router ID, passive and active interfaces.

## 4. The 10 lab tasks

| Task | How it is done |
|---|---|
| 1 Web page | Upload with preview; Generate, Review, Approve and Deploy buttons (plus Run) |
| 2 AI analysis | Image-capable model; JSON shown on the page; unclear values listed, not guessed |
| 3 IOS and OSPF | Per-router interfaces, OSPF process 1 area 0, three networks, LAN ports passive |
| 4 Checks and approval | Addressing, subnets, real interface names, OSPF settings, management reachability; configs shown; approval required |
| 5 Kafka producer | Topic `network-config-jobs`; job ID, device ID, config reference |
| 6 Worker | Consumes jobs, re-checks approval, reports progress, blocks duplicates |
| 7 Paramiko | SSH deploy, output collected, auth/timeout errors handled, no passwords in code |
| 8 OSPF check | The three show commands; FULL neighbors and opposite LAN learned |
| 9 Job monitor | PENDING / RUNNING / SUCCESS / FAILED with router, times, output, results |
| 10 End to end | Shown live: PC1 pings 10.2.0.10 (section 7) |

## 5. Example config (R1, current version - see evidence/10-run2-review-cleanup.png)
```
no router ospf 1                      <- clean-up of any earlier plan
default interface FastEthernet0/0     <- (management port never included)
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
The names FastEthernet0/0 and 1/0 were read from the routers, not typed by anyone.
The first three lines remove an earlier plan, so a new plan replaces an old one fully.
`ip ospf network point-to-point` makes the R1-R2 neighbors FULL within seconds.

## 6. Safety
* The AI has no access to the routers. Only the worker has the router password, and it
  only acts on jobs the backend confirms.
* Secrets live only in `.env`, which is not in git and not in the submission.
* Allowed commands are limited to interface, IP and OSPF lines. Static routes, the
  management port, SSH/VTY settings and `reload` are refused.
* Changing the plan after approval cancels the approval. A changed config is refused by
  both the backend and the worker.
* On errors: wrong passwords are not retried; network errors are retried. A backup is
  saved first, and nothing is saved if a command fails, so `reload` restores the last good
  config.

## 7. Test results

### 7.1 Automated tests - 52 passed
| Area | Tests |
|---|---|
| Reading router output (parsers) | 8 |
| Planning and checks (incl. a 3-router alternative topology, missing OSPF values) | 11 |
| Full run against simulated routers over real SSH (also: duplicate job, changed config, wrong password, second run with a new plan, text pasted after upload) | 5 |
| AI options (cloud, local, errors) | 8 |
| GNS3 check | 13 |
| SSH session handling | 4 |
| Clean-up of the previous plan | 3 |
| **Total** | **52** |

### 7.2 Live test in GNS3 (2026-11-14)
The routers were first reset to the exam starting state (data ports empty, no OSPF,
management on). Then everything was done **through the web page** with the full Docker
stack: upload → AI (38 s, no unclear items) → read interface names over Kafka → Review
(all checks PASS) → confirm mapping → Approve → Deploy → verify. 80 seconds from upload to
DEPLOYED. No router was configured by hand.

| Check | Result | Evidence |
|---|---|---|
| Web page accepts the diagram | ✔ | evidence/01-upload.png |
| AI made the topology JSON | ✔ | evidence/02-ai-json.png |
| Checks passed, configs shown | ✔ | evidence/03-review.png |
| Admin approved | ✔ | evidence/04-approve.png |
| Kafka jobs visible (keys R1 and R2) | ✔ | evidence/05-kafka.png |
| Worker deployed R1 and R2, verify SUCCESS | ✔ | evidence/06-jobs.png, evidence/06b-verify-details.png |
| OSPF neighbors FULL (R1 ↔ 2.2.2.2, R2 ↔ 1.1.1.1) | ✔ | evidence/07-ospf-neighbor.png |
| Remote LANs learned by OSPF (R1: 10.2.0.0/24, R2: 10.0.0.0/24) | ✔ | evidence/08-ospf-routes.png |
| PC1 → 10.2.0.10 ping, 5 of 5 replies | ✔ | evidence/09-ping.png |

The raw router and PC output is in `evidence/*.txt`; the run's jobs are in
`evidence/run-jobs.json`.

Later the **Run** button was tested twice in a row on the same lab: run 1 from reset
routers, run 2 over run 1's config. Both ended DEPLOYED with all jobs SUCCESS, and run 2's
config started with the clean-up lines (evidence/10-run2-review-cleanup.png,
evidence/11-run2-jobs.png). PC1 → 10.2.0.10 still worked afterwards.

### 7.3 Problems found and fixed
* **Extra SSH sessions.** Each job opened two SSH sessions and left one open. The router
  ran out of SSH lines and refused R1's verify. Fixed in `worker/ssh_client.py`, with a test.
* **"Interface names missing" from small AI models.** Interface names come from the
  routers, so this note is now ignored instead of blocking approval.
* **Local model accuracy.** From the picture alone Qwen2.5-VL 7B sometimes misreads one
  address (the checks catch it). With the hand-out text pasted in, every test was correct.

## 8. Conclusion
The full chain works: Upload → AI → Review → Approval → Kafka → Worker → Paramiko →
Verify, with no manual router configuration and no static routes. It was shown live in
GNS3 with a successful PC1 → PC2 ping. It also works with other topologies, because the
interface names are read from the routers and the plan comes from the checked,
admin-confirmed AI output.
