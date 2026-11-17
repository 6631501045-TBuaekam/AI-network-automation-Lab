# AI Network Automation (OSPF)

Upload a picture of a network diagram. An AI reads it, and the system writes the Cisco
router config. An admin checks and approves it. Then a worker logs in to the routers over
SSH, applies the config and checks that OSPF works.

```
 Browser ──► Backend ──► AI (only reads the picture, never touches the routers)
                │  checks + admin approval
                ▼
           Kafka (job queue)
                │
                ▼
           Worker ──SSH──► R1 ── OSPF ── R2        PC1 → ping PC2 ✓
```

## What is in this project

| Folder / file | What it is |
|---|---|
| `backend/` | Web page, AI, config writer, checks, sends jobs to Kafka |
| `worker/` | Takes jobs from Kafka, sets up the routers over SSH, checks OSPF |
| `docker-compose.yml` | Starts everything |
| `.env.example` | Settings template (copy to `.env`) |
| `inventory/` | Router IP addresses (no passwords) |
| `gns3/` | Commands to paste into the routers and PCs |
| `docs/LAB_SETUP.md` | How to build the GNS3 lab |
| `docs/REPORT.md` | Short report and test results |
| `evidence/` | Screenshots from the live test |
| `samples/demo/` | 8 pictures for a live demo, including the exam diagram |

## 1. Build the lab
Follow [docs/LAB_SETUP.md](docs/LAB_SETUP.md).

## 2. Settings
```bash
cp .env.example .env
cp inventory/devices.example.yaml inventory/devices.yaml
```
In `.env`, fill in:
* `OPENAI_API_KEY` - a Gemini API key from Google AI Studio.
* `ADMIN_TOKEN` and `WORKER_TOKEN` - two long random strings (`openssl rand -hex 24`).
* `ROUTER_SSH_PASSWORD` - the routers' `netadmin` password.

In `inventory/devices.yaml`, replace `<R1_MGMT_IP>` and `<R2_MGMT_IP>` with the routers'
management IPs.

Never share `.env` - it holds passwords and keys.

### Other settings
Everything else works without changes. Add a line to `.env` only to change one:

| Setting | Default | Use it when |
|---|---|---|
| `ROUTER_ENABLE_SECRET` | empty | The router login shows `>` instead of `#` |
| `AI_TIMEOUT` / `LOCAL_AI_TIMEOUT` | `120` / `600` | The AI needs more seconds to answer |
| `AI_JSON_MODE` | `true` | Set `false` if the AI gives an error about `response_format` |
| `LOCAL_AI_BASE_URL`, `LOCAL_AI_API_KEY` | Ollama on the Docker host | A local AI is used (LM Studio uses port 1234) |
| `SSH_TIMEOUT` / `SSH_RETRIES` | `15` / `5` | Routers are slow to answer |
| `SSH_HOST_KEY_POLICY` | `accept-new` | Trusts a router the first time, then expects the same router |
| `VERIFY_TIMEOUT` | `180` | OSPF needs more seconds to come up |
| `OSPF_TRANSIT_P2P` | `true` | Makes the R1-R2 link come up in seconds |
| `DISCOVERY_MAX_AGE_MIN` | `120` | How many minutes the router port list stays valid |
| `GNS3_ENABLED`, `GNS3_URL`, login | off | GNS3 cable check - see section 6 |
| `GNS3_API` | `auto` | Set `v3` (GNS3 3.x) or `v2` (GNS3 2.2) if detection fails |
| `GNS3_TIMEOUT` | `5` | Seconds to wait for the GNS3 server |
| `OCR_BASE_URL`, `OCR_MODEL` | Typhoon, `typhoon-ocr-v2` | Another OCR service with an OpenAI-style API is used |

## 3. Start
```bash
docker compose up -d --build
docker compose ps        # everything should be running
```
* Web page: http://localhost:8000 (or the IP of the machine running Docker)
* Kafka UI: http://localhost:8080 → Topics → `network-config-jobs` → Messages
* Stop: `docker compose down`

Docker can run on the GNS3 PC (tested setup) or on an Ubuntu VM. It only needs to reach
the routers' management IPs.

## 4. How to use it
1. Start with routers that have only management SSH set up (see the lab setup).
2. Open the web page and paste the admin token (top right). The health box turns green.
3. Choose the diagram (e.g. `samples/demo/D1-exam-official.png`). "Written requirements" is
   optional: paste text there only if something is not in the picture.
   Don't copy the table from the PDF - the rows get mixed up and the AI misses an address.
4. Click **Run**. It uploads, runs the AI, reads the port names from the routers and
   checks everything. It stops before approval.
   If something is marked "blocking", fix it with **Edit JSON**, then click **Review**.
5. Check the port mapping and the configs. Click **Confirm mapping**.
6. Type the approver's name, tick the box, click **Approve**.
7. Click **Deploy**. The jobs turn SUCCESS: first deploy, then verify (OSPF neighbors FULL,
   remote LAN learned).
8. On PC1, `ping 10.2.0.10` - it replies.

You can also click the buttons one by one: Upload → Generate → Review → Approve → Deploy.

**Running again:** a new plan first removes the old OSPF and clears the LAN and link ports
(never the management port). Just click **Run** again - no manual reset needed.

## 5. Choose the AI
The AI must be able to read pictures. Change it in `.env`, then run
`docker compose up -d backend`.

| AI | Settings in `.env` |
|---|---|
| Google Gemini (default) | `OPENAI_API_KEY=...` is enough. The default is `AI_PROVIDER=openai`, the Gemini address and `AI_MODEL=gemini-4-hydrogen` |
| Claude | `AI_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=...` (needs paid API credits). Model: `claude-sonnet-5-5` |
| DeepSeek | `OPENAI_API_KEY=...`, `OPENAI_BASE_URL=https://api.deepseek.com/v1`, `AI_MODEL=deepseek-flash` (`deepseek-v4-pro` can't read pictures). Best with the OCR check on |
| OpenAI | `AI_PROVIDER=openai`, `OPENAI_API_KEY=...`, `OPENAI_BASE_URL=https://api.openai.com/v1`, `AI_MODEL=gpt-4o` |
| Groq / OpenRouter / GitHub Models | Like OpenAI, with `OPENAI_BASE_URL` = `https://api.groq.com/openai/v1`, `https://openrouter.ai/api/v1` or `https://models.github.ai/inference`, and that service's `AI_MODEL` |
| Local, free (Ollama) | Install Ollama, run `ollama pull qwen2.5vl:7b`, set `AI_PROVIDER=local`. Model: `qwen2.5vl:7b` |

Leave `AI_MODEL` empty to use the model shown for each AI.

**Tip:** always paste the requirements text with the picture. In testing, the local model
got everything right that way, but missed R2's link address from the picture alone (the
checks caught it, so nothing was deployed).

**OCR double-check (optional, free):** set `OCR_API_KEY` to a key from Typhoon
(opentyphoon.ai). Typhoon OCR reads the text in the picture first, and the AI gets that
text with the picture, so it copies the numbers instead of guessing them. If the AI's
network is not in the OCR text, a warning tells you to check it. If OCR is down, the AI
still runs. It helps most with hand-drawn or blurry pictures.

Ollama on Linux must accept outside connections: `sudo systemctl edit ollama` → add
`Environment="OLLAMA_HOST=0.0.0.0"` under `[Service]` → `sudo systemctl restart ollama`.

## 6. Optional: GNS3 cable check
Add to `.env`: `GNS3_ENABLED=true`, `GNS3_URL` (e.g. `http://<gns3-vm-ip>`), and either
`GNS3_USERNAME` + `GNS3_PASSWORD` or `GNS3_TOKEN`. Add `GNS3_PROJECT` only if the
project is not named `ai-netauto-ospf`.

It reads the cables in the GNS3 project, warns if the AI's result doesn't match, and
suggests which router port goes to which network. It only reads - it never changes the lab.

## 7. If something goes wrong

| Problem | Fix |
|---|---|
| Error 401 "Incorrect API key" from OpenAI | `OPENAI_BASE_URL` points to OpenAI - set the Gemini address (or delete the line), then `docker compose up -d backend` |
| Router `unreachable` | Can the Docker machine ping the router? Is its management port up? |
| `auth:` error | Check `ROUTER_SSH_USERNAME` / `ROUTER_SSH_PASSWORD` in `.env` |
| `host key changed` | The router's SSH key changed: `docker compose exec worker rm /data/known_hosts` |
| Verify fails, neighbor not FULL | Check the R1-R2 cable matches the confirmed mapping |
| "credit balance too low" | Claude's API needs prepaid credits (not the same as a Claude subscription) |
| Local AI `unreachable` | Is Ollama running? Check `LOCAL_AI_BASE_URL` |
| Error about `response_format` | Set `AI_JSON_MODE=false` |
| "GNS3 cross-check skipped" | Is the GNS3 VM on and the project open? Check `GNS3_URL` and the login |
| Kafka UI shows no topic | `docker compose logs kafka-init` |

Before using it, run `bash scripts/preflight.sh`. It checks the settings, that the routers
answer, and that everything is running.

**Built-in safety:** the config may only contain interface, IP and OSPF commands. The
worker saves a backup first. If a command fails it stops without saving, so a router
`reload` brings back the last good config. A wrong password is never retried, so the
router doesn't lock the account.
