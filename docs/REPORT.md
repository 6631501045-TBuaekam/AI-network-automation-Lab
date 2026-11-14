# Report - AI-Driven Network Configuration and Automation Using OSPF

**Student:** ______________________ **Date:** 2026-11-14 (evidence run) **Lab:** GNS3 3.1 (Cisco 7200 ×2, VPCS ×2, out-of-band management switch) + Docker Compose (Docker Desktop on Windows)

## 1. Objective
Build a web-based system that turns a topology diagram into deployed, verified Cisco OSPF
configuration, with AI limited to diagram analysis and every change gated by validation
and explicit administrator approval before it travels through Kafka to a Python worker
that uses Paramiko SSH.

## 2. Architecture

| Component | Technology | Responsibility |
|---|---|---|
| Web UI | HTML/JS served by FastAPI | Upload + preview, Generate, Review, Approve, Deploy, live job monitor |
| Backend | Python 3.12, FastAPI, SQLite | Plans, AI call, topology model, config generation (Jinja2), validation, approval, Kafka producer, worker API |
| AI | Any image-capable model: Claude (Anthropic API), OpenAI or any OpenAI-compatible vision API (`OPENAI_BASE_URL`), or a local model (`AI_PROVIDER=local`, Ollama/LM Studio). **Evidence run: Qwen2.5-VL 7B, local via Ollama** | Diagram → structured JSON (devices, networks, router interfaces, hosts, links, routing, ambiguities) |
| Message bus | Apache Kafka 3.8 (KRaft), topic `network-config-jobs` (3 partitions, key = device id) | Decouples approval from execution; Kafka UI for inspection |
| Worker | Python 3.12, confluent-kafka, Paramiko 3.5 | Consume, claim, re-check approval, SSH discover/deploy/verify, report |
| GNS3 cross-check (optional, read-only) | GNS3 REST API v3 / v2, GET only | Compares the AI topology with the lab's real cables and suggests the interface mapping; warnings only |
| Network | GNS3: R1, R2 (Cisco 7200, FastEthernet), PC1, PC2 (VPCS); management on Fa1/1, 192.168.192.0/24 | Target network |

Workflow and trust boundaries:
1. **Upload** - PNG/JPG checked by magic bytes, ≤10 MB, stored with SHA-256.
2. **Generate** - the image (and optional written requirements from the hand-out) goes to
   the AI with a strict schema. The prompt forbids guessing: unknown values are `null` + an
   ambiguity (`blocking` stops approval, `warning` is shown). Physical interface names are
   never taken from the diagram. The backend then queues **read-only discovery** jobs on
   Kafka; the worker runs fixed `show` commands to learn the real interface names and the
   management interface/subnet.
3. **Review** - the backend builds a normalized model (any number of routers/LANs - nothing
   hard-coded), suggests the interface mapping (inventory hint → GNS3 lab cable → CDP →
   ordered; a name is only used if SSH discovery found it), renders per-router IOS config,
   and validates. The admin confirms the mapping.
4. **Approve** - admin token + approver name + explicit confirmation; validation is re-run
   at approval time; the approval stores a hash over all per-router config hashes.
5. **Kafka** - one job per router: `{job_id, job_type, device_id, plan_id, deployment_id,
   config_ref{url, sha256}, approval_ref}`. Config text is not in the message.
6. **Worker** - claims the job atomically (duplicate prevention), asks the backend to
   re-confirm approval, receives the config, recomputes its SHA-256 and compares with the
   Kafka reference, backs up running-config, pushes line by line (stops on `%` errors),
   saves, then re-opens SSH to prove management survived.
7. **Verify** - after all deploy jobs succeed the backend queues verify jobs. The worker
   polls `show ip ospf neighbor`, `show ip route ospf` and `show ip protocols` until OSPF
   converges (or times out) and checks FULL neighbors, the opposite LAN as an `O` route,
   process id, router-id, passive/active interfaces and advertised networks; it also pings
   the remote host sourced from the LAN (informational).

## 3. Generated configuration (exam topology, R1 - as deployed in the evidence run)
```
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
R2 is symmetrical (router-id 2.2.2.2, 10.1.0.0/24 + 10.2.0.0/24, Fa0/0 passive). The
FastEthernet names were not typed anywhere: they come from SSH discovery on the routers.
`ip ospf network point-to-point` on the router-to-router link (configurable with
`OSPF_TRANSIT_P2P`) removes the 40 s DR/BDR wait so adjacency is FULL within seconds.

## 4. Security and safety controls
* AI has no network path or credentials for the routers; the worker only acts on jobs
  the backend re-authorizes. Device addresses come from the worker's own inventory, so a
  Kafka message cannot redirect it to another host. The GNS3 cross-check only reads the
  GNS3 API and never contacts routers.
* Secrets in `.env` only (git-ignored, not in the submission), split per container;
  non-root containers; tokens compared in constant time; Kafka UI read-only.
* Config allow-list (hostname, interface, description, ip address, no shutdown, OSPF
  commands); management interface, VTY/SSH/AAA, static routes and reload are impossible.
* Approval binding: editing topology/mapping revokes approval; content tampering after
  approval is refused by both backend and worker (tested).
* Failure handling: typed SSH errors (auth / timeout / unreachable / host-key / CLI),
  no retry on bad credentials, retries on timeout/unreachable (`SSH_RETRIES`), pre-change
  backup, no `write memory` after an error, lease-based recovery of jobs stuck in RUNNING.
  Recovery: `reload` restores the last saved config, or re-apply the backup, or fix the
  plan, re-approve and redeploy (the configuration is idempotent).

## 5. Test results

### 5.1 Automated tests (`python -m pytest -q tests/`)
| Suite | Tests | Result |
|---|---|---|
| IOS output parsers (broadcast & p2p neighbors, "subnetted" & "variably subnetted" routes, CDP wrap) | 7 | Pass |
| Planning / validation (exam topology, unconfirmed mapping, management interface refused, non-existent interface, router-id not guessed, subnet mismatch, wrong gateway, blocking ambiguity, no discovery, **3-router alternative topology**) | 10 | Pass |
| End-to-end over SSH with simulated IOS routers (full workflow → OSPF FULL + routes + ping; duplicate Kafka delivery ignored; post-approval tampering refused; bad credentials reported) | 3 | Pass |
| AI providers (OpenAI-compatible base URL, local model without key, JSON mode, unreachable server, interface-name ambiguities dropped) | 8 | Pass |
| GNS3 cross-check (GET only, switch look-through, management port excluded, cabling beats ordered guess, mismatch warning, API v2/v3 detection, basic auth, unreachable/closed project) | 12 | Pass |
| SSH session handling (exactly one SSH session per `with open_session()`, retries kept) | 3 | Pass |
| **Total** | **43** | **43 passed** |

### 5.2 Live GNS3 demonstration (2026-11-14, full Docker Compose stack)
Routers were first reset to the instructor's starting state (`gns3/reset-dataplane.txt`:
data-plane interfaces unassigned and shut, no OSPF, management SSH kept). The whole
workflow was then driven **from the web UI**: upload → Generate (Qwen2.5-VL, 38 s, no
ambiguities) → discovery via Kafka → Review (all checks PASS, GNS3 cross-check suggested
all four ports from the real cables) → mapping confirmed → Approve → Deploy → verify.
Total time from upload to DEPLOYED: 80 s. No router was configured by hand.

| Acceptance step | Result | Evidence |
|---|---|---|
| Frontend loads, diagram accepted | ☑ | evidence/01-upload.png |
| AI produced topology JSON / OSPF plan | ☑ | evidence/02-ai-json.png |
| Validation passed, configs reviewed | ☑ | evidence/03-review.png |
| Approved by administrator | ☑ | evidence/04-approve.png |
| Kafka jobs visible (10 messages, keys R1/R2, partitions 0 and 2) | ☑ | evidence/05-kafka.png |
| Worker deployed R1 and R2 via Paramiko (deploy + verify SUCCESS) | ☑ | evidence/06-jobs.png, evidence/06b-verify-details.png |
| OSPF neighbors FULL (R1 ↔ 2.2.2.2, R2 ↔ 1.1.1.1) | ☑ | evidence/07-ospf-neighbor.png |
| Remote LAN routes learned via OSPF (R1: O 10.2.0.0/24, R2: O 10.0.0.0/24) | ☑ | evidence/08-ospf-routes.png |
| PC1 → 10.2.0.10 ping successful (5/5, trace via 10.0.0.1 → 10.1.0.2) | ☑ | evidence/09-ping.png |

Raw console text for 07-09 is in `evidence/*.txt`; the job list of the run is in
`evidence/run-jobs.json` and the step log in `evidence/run-log.txt`.

### 5.3 Problems found during live testing and how they were fixed
* **Leaked SSH sessions.** `open_session()` returned a connected session and the `with`
  statement connected a second time, leaking the first session and skipping the retry
  loop. On the router this showed up as R1 refusing SSH during verification (its VTY
  lines filled up). Fixed in `ssh_client.py` (`__enter__` only connects when not yet
  connected) with a regression test; the evidence run passed after the fix.
* **Small models flag "interface names missing".** Interface names are discovered over
  SSH, so the backend drops that kind of AI ambiguity instead of blocking approval.
* **Local model accuracy.** From the image alone Qwen2.5-VL 7B sometimes misreads one
  address; validation catches it. With the hand-out's requirement text pasted into the
  requirements box (as in the evidence run) every value was correct in every test.

## 6. Conclusion
The system satisfies the end-to-end chain Upload → AI → Review → Approval → Kafka →
Worker → Paramiko → Verification without manual router configuration or static routes,
demonstrated live in GNS3 with a successful PC1 → PC2 ping, and adapts to alternative
topologies because interface names are discovered and the addressing plan is derived from
the AI's (validated, human-confirmed) output.
