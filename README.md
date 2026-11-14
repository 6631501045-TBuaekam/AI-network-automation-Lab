# AI-Driven Network Configuration & Automation (OSPF)

Web-based system that takes a network diagram image, uses an image-capable AI model to
extract the topology and addressing plan, generates Cisco IOS OSPFv2 configurations,
validates them, waits for explicit administrator approval, and deploys them to the
routers through **Apache Kafka → Python Worker → Paramiko SSH**, then verifies OSPF
automatically.

```
 Browser ──HTTP──► Backend (FastAPI) ──► AI vision API (diagram → JSON only)
   ▲                  │  SQLite state, Jinja2 IOS templates, validator, approval
   │ polling          │
   │                  ▼ produce (job id, device id, config reference + sha256)
   │             Kafka topic: network-config-jobs   (Kafka UI :8080 to inspect)
   │                  │
   │                  ▼ consume (manual commit)
   └── status ◄── Python Worker ──claim / re-check approval──► Backend
                      │
                      ▼ Paramiko SSH (management network)
                 R1 ──── OSPF area 0 ──── R2        PC1 ping 10.2.0.10 ✓
```

| Folder | Content |
|---|---|
| `backend/` | FastAPI API + web UI (`app/static`), AI client, topology model, IOS template, validator, Kafka producer |
| `worker/` | Kafka consumer, Paramiko IOS session, deploy / discover / verify handlers, IOS output parsers |
| `docker-compose.yml` | Kafka (KRaft), topic init, Kafka UI, backend, worker |
| `inventory/` | Router management addresses (no credentials) |
| `gns3/` | Router management baselines, practice reset script, VPCS commands |
| `docs/` | GNS3 + Ubuntu build guide, architecture & test report |
| `tests/` | 27 automated tests incl. an SSH-based simulated IOS lab |
| `samples/` | The exam diagram as PNG (with and without the requirements table) |

---

## 1. Requirement → implementation map

| Task | Where | How it is satisfied |
|---|---|---|
| 1 Web frontend | `backend/app/static/` | PNG/JPG upload with drag-drop + preview; **Generate, Review, Approve, Deploy** buttons; step rail |
| 2 AI analysis | `backend/app/ai.py` | Image sent to Claude (or GPT-4o) with a strict JSON schema; JSON displayed; missing/unclear values go to `ambiguities` (blocking/warning) instead of being guessed |
| 3 IOS + OSPF | `generator.py`, `templates/cisco_ios_ospf.j2` | Per-router interfaces, `router ospf <pid>`, router-id, one `network … area 0` per connected /24, `passive-interface` on LAN links, R1–R2 link active (optionally point-to-point) |
| 4 Validation & approval | `topology.py`, `validator.py` | Addressing, subnet consistency, gateways, duplicate IPs, **real discovered interface names**, management interface protection, OSPF settings, command allow-list, management reachability (recent SSH discovery). Approval needs admin token + approver + confirmation and is bound to a SHA-256 of the configs |
| 5 Kafka producer | `kafka_producer.py`, `main.py: dispatch()` | Topic `network-config-jobs`, key = device id, message has `job_id`, `device_id`, `plan_id`, `config_ref {url, sha256}`, `approval_ref` - never the config text |
| 6 Consumer/worker | `worker/worker.py` | Manual-commit consumer; **atomic claim** (PENDING→RUNNING) prevents duplicate execution; **re-checks approval** and config hash with the backend; reports progress events |
| 7 Paramiko | `worker/ssh_client.py` | `invoke_shell` session, enable mode, prompt tracking, IOS error detection, backup of running-config, `write memory`; typed errors for auth / timeout / unreachable; credentials only from env |
| 8 OSPF verification | `worker/parsers.py` | Runs `show ip ospf neighbor`, `show ip route ospf`, `show ip protocols`; checks FULL adjacency, remote LAN learned as `O`, router-id, process, passive interfaces; retries until converged; router-sourced ping |
| 9 Monitoring | UI section 5 | PENDING / RUNNING / SUCCESS / FAILED per job with router, timestamps, Kafka partition/offset, event log, device output, verification table |
| 10 End-to-end | whole stack | Demo runbook below |

Technical restrictions: no manual router config (system does everything); no static routes
(allow-list rejects `ip route`); AI never connects to routers (only the worker has
credentials, and only acts on approved jobs); approval mandatory and tamper-checked;
secrets only in `.env` (git-ignored, split per container); management interface and
VTY/SSH lines can never be touched; alternative topologies work (nothing hard-coded).

---

## 2. Build the lab
Follow **[docs/GNS3_UBUNTU_SETUP.md](docs/GNS3_UBUNTU_SETUP.md)** (GNS3 topology, management
network, Ubuntu Server VM, Docker install). Then come back here.

## 3. Run the system (Ubuntu Server VM)

```bash
cd ~/ai-netauto
cp .env.example .env
nano .env            # AI key, ADMIN_TOKEN, WORKER_TOKEN, ROUTER_SSH_PASSWORD
chmod 600 .env
cp inventory/devices.example.yaml inventory/devices.yaml
nano inventory/devices.yaml   # R1/R2 management IPs from the instructor

openssl rand -hex 24           # use output for ADMIN_TOKEN, run again for WORKER_TOKEN

docker compose up -d --build
docker compose ps              # kafka healthy, kafka-init exited 0, backend healthy, worker up
docker compose logs -f worker  # "worker ... consuming network-config-jobs"
bash scripts/preflight.sh      # config + reachability + stack check
```

* Web UI: `http://<ubuntu-ip>:8000`
* Kafka UI: `http://<ubuntu-ip>:8080` → Topics → `network-config-jobs` → Messages

Stop: `docker compose down` (data kept in volumes) · full reset: `docker compose down -v`.

### Choosing the AI model
Set these in `.env`, then `docker compose up -d backend`. The model must accept images.

| Option | `.env` |
|---|---|
| Claude (default) | `AI_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=...` |
| OpenAI | `AI_PROVIDER=openai`, `OPENAI_API_KEY=...` |
| Any OpenAI-compatible cloud API (Gemini, Groq, OpenRouter, GitHub Models - several have free keys) | `AI_PROVIDER=openai`, `OPENAI_API_KEY=<that service's key>`, `OPENAI_BASE_URL=<its URL, listed in .env.example>`, `AI_MODEL=<its vision model>` |
| Local model, no key (Ollama on the Docker host) | install Ollama, `ollama pull llama3.2-vision`, then `AI_PROVIDER=local` |
| Other local server (LM Studio, vLLM, llama.cpp) | `AI_PROVIDER=local`, `LOCAL_AI_BASE_URL=http://host.docker.internal:<port>/v1`, `AI_MODEL=<model>` |

For a local model on Linux, let Ollama listen beyond localhost so the backend container
can reach it: `sudo systemctl edit ollama` → add `[Service]` and
`Environment="OLLAMA_HOST=0.0.0.0"` → `sudo systemctl restart ollama`. On CPU only, one
analysis can take several minutes (`LOCAL_AI_TIMEOUT`, default 600 s), and small local
models read diagrams less reliably than Claude - check every value on the Review page.
If a service rejects `response_format`, set `AI_JSON_MODE=false`.

### Optional rehearsal without GNS3
`docker compose -f docker-compose.yml -f docker-compose.sim.yml up -d --build` swaps the
routers for two simulated IOS routers (SSH on the same Docker network). Everything else -
AI, Kafka, worker, Paramiko - is real. **Not for the exam demo**; switch back with
`docker compose up -d --remove-orphans`.

---

## 4. Exam-day runbook (acceptance tests)

| # | Instructor / you | In the system |
|---|---|---|
| 1 | Instructor resets R1/R2 data plane (management SSH kept) | - |
| 2 | `docker compose up -d`, open UI, paste admin token (top right) | Health pill green |
| 2 | Upload the instructor's diagram | Preview shown, plan `UPLOADED` |
| 3 | **Generate** | AI JSON + ambiguities; discovery jobs appear in the jobs table (Kafka offsets) |
| 3 | Fix blocking ambiguities if any: **Edit JSON** → Save, or add the missing requirement text and Generate again | |
| 3 | **Review** → check mapping (dropdowns = real interfaces) → **Confirm mapping** | All validation cards PASS, configs shown per router |
| 3 | Enter approver name, tick confirmation, **Approve** | `APPROVED` + approval hash |
| 4 | **Deploy** | 2 deploy jobs PENDING → RUNNING → SUCCESS (Kafka topic/partition/offset shown) |
| 5 | Automatic | 2 verify jobs: neighbors FULL, `10.2.0.0/24` on R1 and `10.0.0.0/24` on R2 as OSPF routes; click a job for the full show-command output |
| 6 | Instructor: `PC1> ping 10.2.0.10` | Replies ✓ |

Evidence screenshots for submission (save in `evidence/`):
1. UI - AI analysis JSON 2. UI - validation + proposed configs 3. UI - approval
4. Kafka UI messages (or `scripts/show-kafka-jobs.sh`) 5. UI - jobs table SUCCESS
6. UI - verify job details (neighbors FULL, OSPF routes) 7. `PC1> ping 10.2.0.10`
8. Router consoles: `show ip ospf neighbor`, `show ip route ospf`.

---

## 5. Safety, failure reporting and recovery
* Generated config never contains the management interface, VTY, AAA, SSH, `ip route`,
  `reload` or anything outside a strict allow-list (validator + worker re-hash).
* Before changing a router the worker saves `show running-config` to
  `worker-data:/data/backups/<device>-<job>-pre.cfg`.
* On an IOS error the push stops at the failing line, leaves config mode, does **not**
  `write memory`, and marks the job FAILED with the exact `%` message.
* After a push the worker opens a **new** SSH session to prove management still works.
* Auth failures are not retried (avoids lockout); timeouts/unreachable are retried twice.
* Recovery: the running config was not saved after a failure, so `reload` restores the
  last saved config; or copy the backup out
  (`docker compose cp worker:/data/backups/. ./backups/`) and re-apply; or fix the plan,
  re-approve and redeploy (config is idempotent). To undo a successful deployment use
  `gns3/reset-dataplane.txt` (practice only).
* Jobs stuck RUNNING (worker crash) are re-claimable after `JOB_LEASE_SECONDS` (600 s).

## 6. Troubleshooting
| Symptom | Fix |
|---|---|
| Discovery FAILED `unreachable` | `ping` the router from the VM; check MGMT-CLOUD bridging and Gi0/3 `no shutdown` |
| `auth:` error | `.env` username/password; `username … privilege 15` on the router (or set `ROUTER_ENABLE_SECRET`) |
| `host key … changed` | Router re-generated RSA keys: `docker compose exec worker rm /data/known_hosts` |
| SSH negotiation failed | IOS needs `crypto key generate rsa modulus 2048` and `ip ssh version 2` |
| Verify FAILED neighbor INIT/2WAY | check R1–R2 cabling matches the confirmed mapping; `VERIFY_TIMEOUT` larger |
| AI error 401/404 | API key / `AI_MODEL` name in `.env`, then `docker compose up -d backend` |
| AI error "credit balance too low" | Claude API needs prepaid credits (Console → Billing), separate from a Claude subscription |
| Local model `unreachable` | Ollama running? `OLLAMA_HOST=0.0.0.0` on Linux; check `LOCAL_AI_BASE_URL` port |
| AI error mentioning `response_format` | `AI_JSON_MODE=false` |
| Kafka UI empty | `docker compose logs kafka-init` - topic must be created |

## 7. Tests
```bash
pip install -r backend/requirements.txt -r worker/requirements.txt pytest
python -m pytest -q tests/     # 27 passed
```
`tests/test_end_to_end.py` runs the complete workflow over real SSH against simulated IOS
routers: discovery, review, approval, deploy, verification (FULL + routes + ping),
duplicate-delivery prevention, post-approval tampering refusal and bad-credential reporting.

## 8. Security notes
* `.env` holds all secrets; it is git-ignored and must not be submitted. The backend gets
  only the AI key + tokens; the worker gets only router credentials + worker token.
* Admin actions (approve, deploy, edit plan/mapping) require `X-Admin-Token`; worker API
  requires `X-Worker-Token`; tokens are compared in constant time.
* Containers run as a non-root user. Kafka UI is read-only.
