# AI Network Automation (OSPF)

You upload a picture of a network diagram. An AI reads it. The system writes the Cisco
router configuration, checks it, and waits for an admin to approve it. After approval it
sends the job through **Kafka** to a **Python worker**, which logs in to the routers with
**SSH (Paramiko)**, applies the configuration and checks that OSPF works.

```
 Browser ──► Backend ──► AI (reads the picture only, never touches routers)
                │  checks + admin approval
                ▼
           Kafka topic "network-config-jobs"
                │
                ▼
           Python worker ──SSH──► R1 ── OSPF area 0 ── R2      PC1 → ping PC2 ✓
```

## What is in this project

| Folder / file | What it is |
|---|---|
| `backend/` | Web page, API, AI client, config generator, checks, Kafka sender |
| `worker/` | Kafka reader, SSH to the routers, deploy and verify |
| `docker-compose.yml` | Starts everything: Kafka, Kafka UI, backend, worker |
| `.env.example` | Settings template (copy to `.env`) |
| `inventory/` | Router management IP addresses (no passwords) |
| `gns3/` | Router management setup, PC commands, practice reset script |
| `docs/LAB_SETUP.md` | How to build the GNS3 lab |
| `docs/REPORT.md` | Short report: design and test results |
| `evidence/` | Screenshots from the live test |
| `samples/` | The exam diagram and its requirements as text (`exam-requirements.txt`) |
| `tests/` | 52 automated tests |

## 1. Build the lab
Follow [docs/LAB_SETUP.md](docs/LAB_SETUP.md): GNS3 routers, PCs, and the management
network the system uses to reach the routers.

## 2. Settings
```bash
cp .env.example .env
cp inventory/devices.example.yaml inventory/devices.yaml
```
In `.env` set at least:
* `ADMIN_TOKEN` and `WORKER_TOKEN` - two long random strings
  (make one with `openssl rand -hex 24`).
* `ROUTER_SSH_PASSWORD` - the routers' `netadmin` password.
* The AI model - see "Choose the AI model" below.

In `inventory/devices.yaml` replace `<R1_MGMT_IP>` and `<R2_MGMT_IP>` with your routers'
management IPs.

Never share or submit `.env` - it holds your secrets. Git already ignores it.

## 3. Start
```bash
docker compose up -d --build
docker compose ps        # all running: kafka healthy, backend healthy, worker up
```
* Web page: http://localhost:8000 (or the IP of the machine running Docker)
* Kafka UI: http://localhost:8080 → Topics → `network-config-jobs` → Messages
* Stop: `docker compose down`

Docker can run on the PC that runs GNS3 (Docker Desktop - this is what we tested) or on
an Ubuntu VM. It only needs to reach the routers' management IPs.

## 4. Demo steps
1. Instructor resets the routers (management SSH stays on).
2. Open the web page. Paste the admin token (top right). The health box turns green.
3. Choose the diagram (`samples/exam-topology.png`, the one with the table). Paste the
   requirements into "Written requirements" - **one line per item, label first**. For the
   exam it is ready in `samples/exam-requirements.txt`. Do not paste the table straight from
   the PDF: the copy mixes up the rows and the AI then misses R2's link address.
4. Click **Run**. It uploads, runs the AI, reads the real interface names from the
   routers, and checks everything. It **stops before approval**.
   If the AI marks something as "blocking", fix it with **Edit JSON** and click **Review**.
5. Check the interface mapping and the configs. Click **Confirm mapping**.
6. Type your name, tick the box, click **Approve**.
7. Click **Deploy**. Watch the jobs: deploy → verify, each turns SUCCESS.
   Verify shows OSPF neighbors FULL and the remote LAN learned through OSPF.
8. Instructor runs `ping 10.2.0.10` on PC1 - it replies.

You can also use the buttons one by one: Upload → Generate → Review → Approve → Deploy.

**Running again:** each new plan first removes the old OSPF and resets the data ports
(never the management port). These lines are at the top of the config you approve. So you
can just click **Run** again with a new diagram - no manual reset needed.

## 5. Choose the AI model
The model must be able to read images. Set it in `.env`, then run
`docker compose up -d backend`.

| Option | Settings in `.env` |
|---|---|
| Claude | `AI_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=...` (needs API credits) |
| OpenAI | `AI_PROVIDER=openai`, `OPENAI_API_KEY=...` |
| Other cloud API (Gemini, Groq, OpenRouter, GitHub Models) | `AI_PROVIDER=openai`, `OPENAI_API_KEY=...`, `OPENAI_BASE_URL=...`, `AI_MODEL=...` (URLs are in `.env.example`) |
| Local model, free (Ollama) | Install Ollama, run `ollama pull qwen2.5vl:7b`, set `AI_PROVIDER=local` |

**Tested:** local `qwen2.5vl:7b` on a laptop GPU, about 25-40 seconds per diagram. With the
picture that has the table **and** `samples/exam-requirements.txt` pasted, it got every value
right in every test. With the picture alone, or with text copied straight from the PDF, it
misses R2's link address (the checks catch it and nothing is deployed).

Ollama on Linux: allow outside connections, or the Docker container cannot reach it:
`sudo systemctl edit ollama` → add `Environment="OLLAMA_HOST=0.0.0.0"` under `[Service]`
→ `sudo systemctl restart ollama`.

## 6. Optional: GNS3 cable check
Turn on with `GNS3_ENABLED=true` and set `GNS3_URL` (your GNS3 server, e.g.
`http://<gns3-vm-ip>`), `GNS3_PROJECT` and `GNS3_TOKEN` in `.env`.

It reads the GNS3 project (devices and cables) and:
* warns if the AI's topology does not match the lab;
* suggests which router port goes to which network, from the real cables.

It only **reads** GNS3. It never changes the lab or the routers, and it only gives
warnings - you still confirm and approve. Works with GNS3 3.x and 2.2 (detected
automatically).

## 7. If something goes wrong

| Problem | Fix |
|---|---|
| Discovery says `unreachable` | Can the Docker machine ping the router? Is the management port up? |
| `auth:` error | Check `ROUTER_SSH_USERNAME` / `ROUTER_SSH_PASSWORD` in `.env` |
| `host key changed` | Router got new SSH keys: `docker compose exec worker rm /data/known_hosts` |
| Verify fails, neighbor not FULL | Check the R1-R2 cable matches the confirmed mapping |
| AI error "credit balance too low" | Claude API needs prepaid credits (separate from a Claude subscription) |
| AI error "unreachable" (local model) | Is Ollama running? Check `LOCAL_AI_BASE_URL` |
| AI error about `response_format` | Set `AI_JSON_MODE=false` |
| "GNS3 cross-check skipped" | Is the GNS3 VM on and the project open? Check `GNS3_URL` and token |
| Kafka UI shows no topic | `docker compose logs kafka-init` |

Before a demo you can run `bash scripts/preflight.sh` - it checks settings, router
reachability and the containers.

**Safety built in:** the config can only contain interface, IP and OSPF commands (no static
routes, never the management port or SSH settings). Before a change the worker saves a
backup of the router config. If a command fails, it stops and does not save, so a router
`reload` brings back the last good config. Wrong passwords are not retried (to avoid
lock-out).

## 8. Tests
```bash
pip install -r backend/requirements.txt -r worker/requirements.txt pytest
python -m pytest -q tests/     # 52 passed
```
The tests include a full run against simulated routers over real SSH.

Practice without GNS3: `docker compose -f docker-compose.yml -f docker-compose.sim.yml up -d --build`
uses two simulated routers instead (not for the exam).
