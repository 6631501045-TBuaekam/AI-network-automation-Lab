# Report - AI-Driven Network Configuration and Automation Using OSPF

**Student:** ______________________ **Date:** ____________ **Lab:** GNS3 (Cisco IOSv ×2, VPCS ×2) + Ubuntu Server 24.04 VM (Docker Compose)

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
| AI | Claude (Anthropic API) or GPT-4o, vision input | Diagram → structured JSON (devices, networks, router interfaces, hosts, links, routing, ambiguities) |
| Message bus | Apache Kafka 3.8 (KRaft), topic `network-config-jobs` (3 partitions, key = device id) | Decouples approval from execution; Kafka UI for inspection |
| Worker | Python 3.12, confluent-kafka, Paramiko 3.5 | Consume, claim, re-check approval, SSH discover/deploy/verify, report |
| Network | GNS3: R1, R2 (IOSv), PC1, PC2 (VPCS), out-of-band management switch | Target network |

Workflow and trust boundaries:
1. **Upload** - PNG/JPG checked by magic bytes, ≤10 MB, stored with SHA-256.
2. **Generate** - the image (and optional written requirements) goes to the AI with a strict
   schema. The prompt forbids guessing: unknown values are `null` + an ambiguity
   (`blocking` stops approval, `warning` is shown). The backend then queues **read-only
   discovery** jobs on Kafka; the worker runs fixed `show` commands to learn the real GNS3
   interface names and the management interface/subnet.
3. **Review** - the backend builds a normalized model (any number of routers/LANs - nothing
   hard-coded), suggests the interface mapping (inventory hint → CDP → ordered), renders
   per-router IOS config, and validates. The admin confirms the mapping.
4. **Approve** - admin token + approver name + explicit confirmation; validation is re-run
   at approval time; the approval stores a hash over all per-router config hashes.
5. **Kafka** - one job per router: `{job_id, job_type, device_id, plan_id, deployment_id,
   config_ref{url, sha256}, approval_ref}`. Config text is not in the message.
6. **Worker** - claims the job atomically (duplicate prevention), asks the backend to
   re-confirm approval, receives the config, recomputes its SHA-256 and compares with the
   Kafka reference, backs up running-config, pushes line by line (stops on `%` errors),
   saves, then re-opens SSH to prove management survived.
7. **Verify** - after all deploy jobs succeed the backend queues verify jobs. The worker
   polls the three show commands until OSPF converges (or times out) and checks FULL
   neighbors, remote networks as `O` routes, process id, router-id, passive/active
   interfaces and advertised networks; it also pings remote hosts sourced from the LAN.

## 3. Generated configuration (exam topology, R1)
```
hostname R1
interface GigabitEthernet0/0
 description LAN PC1
 ip address 10.0.0.1 255.255.255.0
 no shutdown
interface GigabitEthernet0/1
 description Link to R2
 ip address 10.1.0.1 255.255.255.0
 ip ospf network point-to-point
 no shutdown
router ospf 1
 router-id 1.1.1.1
 network 10.0.0.0 0.0.0.255 area 0
 network 10.1.0.0 0.0.0.255 area 0
 passive-interface GigabitEthernet0/0
```
R2 is symmetrical (router-id 2.2.2.2, 10.1.0.0/24 + 10.2.0.0/24, Gi0/0 passive).
`ip ospf network point-to-point` on the router-to-router link (configurable with
`OSPF_TRANSIT_P2P`) removes the 40 s DR/BDR wait so adjacency is FULL within seconds.

## 4. Security and safety controls
* AI has no network path or credentials for the routers; the worker only acts on jobs
  the backend re-authorizes. Device addresses come from the worker's own inventory, so a
  Kafka message cannot redirect it to another host.
* Secrets in `.env` only, split per container; non-root containers; tokens compared in
  constant time; Kafka UI read-only.
* Config allow-list (hostname, interface, description, ip address, no shutdown, OSPF
  commands); management interface, VTY/SSH/AAA, static routes and reload are impossible.
* Approval binding: editing topology/mapping revokes approval; content tampering after
  approval is refused by both backend and worker (tested).
* Failure handling: typed SSH errors (auth / timeout / unreachable / host-key / CLI),
  no retry on bad credentials, pre-change backup, no `write memory` after an error,
  lease-based recovery of jobs stuck in RUNNING.

## 5. Test results

### 5.1 Automated tests (`python -m pytest -q tests/`)
| Suite | Tests | Result |
|---|---|---|
| IOS output parsers (real IOSv formats: broadcast & p2p neighbors, "subnetted" & "variably subnetted" routes, CDP wrap) | 7 | Pass |
| Planning / validation (exam topology, unconfirmed mapping, management interface refused, non-existent interface, router-id not guessed, subnet mismatch, wrong gateway, blocking ambiguity, no discovery, **3-router alternative topology**) | 10 | Pass |
| End-to-end over SSH with simulated IOS routers (full workflow → OSPF FULL + routes + ping; duplicate Kafka delivery ignored; post-approval tampering refused; bad credentials reported) | 3 | Pass |
| **Total** | **20** | **20 passed** |

### 5.2 Live GNS3 demonstration (fill in)
| Acceptance step | Result | Evidence |
|---|---|---|
| Frontend loads, diagram accepted | ☐ | evidence/01-upload.png |
| AI produced topology JSON / OSPF plan | ☐ | evidence/02-ai-json.png |
| Validation passed, configs reviewed | ☐ | evidence/03-review.png |
| Approved by administrator | ☐ | evidence/04-approve.png |
| Kafka jobs visible | ☐ | evidence/05-kafka.png |
| Worker deployed R1 and R2 via Paramiko | ☐ | evidence/06-jobs.png |
| OSPF neighbors FULL | ☐ | evidence/07-ospf-neighbor.png |
| Remote LAN routes learned via OSPF | ☐ | evidence/08-ospf-routes.png |
| PC1 → 10.2.0.10 ping successful | ☐ | evidence/09-ping.png |

## 6. Conclusion
The system satisfies the end-to-end chain Upload → AI → Review → Approval → Kafka →
Worker → Paramiko → Verification without manual router configuration or static routes,
and adapts to alternative topologies because interface names are discovered and the
addressing plan is derived from the AI's (validated, human-confirmed) output.
