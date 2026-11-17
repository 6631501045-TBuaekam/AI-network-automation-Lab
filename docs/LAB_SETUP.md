# Lab setup (GNS3)

This builds the sample network in GNS3. Each router also gets a **management port** - the
system uses it to log in over SSH and never changes it.

```
              Management network, e.g. 192.168.x.0/24 (VMware VMnet1, host-only)
                         |
   MGMT-CLOUD ---- MGMT-SW (switch)
                    |                 |
        R1 Fa1/1 <R1_MGMT_IP>   R2 Fa1/1 <R2_MGMT_IP>
                    |                 |
   PC1 ---- Fa0/0 [R1] Fa1/0 ------- Fa1/0 [R2] Fa0/0 ---- PC2
 10.0.0.10   10.0.0.1     10.1.0.1  10.1.0.2    10.2.0.2      10.2.0.10
```

Fa0/0, Fa1/0 and OSPF are **not** set up by hand. The system does that after approval.

## 1. Requirements
* GNS3 **2.2 or 3.x**, with the GNS3 VM or the local server. Tested with GNS3 3.1.
  The system logs in to the routers over SSH, so the GNS3 version does not matter for
  the main workflow (see section 6 for the few 2.x differences).
* A Cisco router image. Tested with **Cisco 7200** (`c7200-advipservicesk9-mz.124-24.T8`).
  IOSv works too - the system reads the port names from each router.
* VPCS for the PCs (comes with GNS3).

Cisco 7200 tip: set an **Idle-PC** value (right-click the router → Idle-PC), or each
router uses a full CPU core.

## 2. Build the project
Create a project named `ai-netauto-ospf` and add:

| Name | Type |
|---|---|
| R1, R2 | Router (Cisco 7200: slot 0 `C7200-IO-FE`, slot 1 `PA-2FE-TX`) |
| PC1, PC2 | VPCS |
| MGMT-SW | Ethernet switch |
| MGMT-CLOUD | Cloud, port **eth0** (the GNS3 VM's host-only network card) |

Connect the cables:

| From | To | Network |
|---|---|---|
| PC1 e0 | R1 Fa0/0 | 10.0.0.0/24 (LAN 1) |
| R1 Fa1/0 | R2 Fa1/0 | 10.1.0.0/24 (R1-R2 link) |
| R2 Fa0/0 | PC2 e0 | 10.2.0.0/24 (LAN 2) |
| R1 Fa1/1 | MGMT-SW port 1 | management |
| R2 Fa1/1 | MGMT-SW port 2 | management |
| MGMT-CLOUD eth0 | MGMT-SW port 3 | management (to the host PC) |

Other ports are fine too - the port mapping is confirmed on the Review page.

## 3. Management setup (once)
Pick two free IPs in the management network, e.g. `192.168.x.11` and `192.168.x.12`.
Put the same IPs in `inventory/devices.yaml`.

1. Start all devices. Wait for the router prompt (first boot takes 1-2 minutes).
2. In `gns3/R1-management-baseline.txt`, fill in the `<...>` values: the password (same as
   `ROUTER_SSH_PASSWORD`), R1's management IP and the netmask. Paste it into R1's console.
3. Do the same for R2 with `gns3/R2-management-baseline.txt`.
4. Check with `show ip interface brief`: only Fa1/1 has an IP.
5. On PC1 and PC2, run the commands in `gns3/vpcs-commands.txt`.
6. From the host PC, `ping` both router IPs.

## 4. Where Docker runs
Docker must be able to reach both router management IPs.

* **On the GNS3 PC (Docker Desktop)** - nothing extra. This is the tested setup.
* **On an Ubuntu VM** - add a second network card on **VMnet1 (host-only)** with a fixed
  IP in the management network (e.g. 192.168.x.10/24), install Docker with Compose, and
  copy the project to the VM.

Then continue with the README: settings, start, and how to use it.

## 5. Optional: let Deploy set the PCs
Add the PCs' consoles to `inventory/devices.yaml` (see the `pcs:` part of
`inventory/devices.example.yaml`). Deploy then gives PC1 and PC2 each plan's addresses,
so no typing on the PCs. Find the port in GNS3: right-click the PC → **Show node
information** → Console port. Leave `pcs:` out to set the PCs by hand (the Deploy box
shows the commands).

## 6. GNS3 2.x

Everything above works the same in GNS3 2.2. Only these points differ:

| Topic | GNS3 2.2 |
|---|---|
| Routers | Any Cisco IOS image with SSH (c7200, IOSv, IOSvL2). Port names are read from each router |
| Cloud for management | Same idea: a Cloud node on the host-only network card (GNS3 VM) or on the host's adapter (local server) |
| PC consoles (`pcs:`) | **GNS3 VM**: `console_host` = the VM's IP. **Local server**: the consoles listen on `127.0.0.1` by default, which Docker cannot reach - set the server's console host to `0.0.0.0` (Edit → Preferences → Server) and use `console_host: host.docker.internal` |
| GNS3 cable check (optional) | Works with both: `GNS3_API=auto` detects the version, or set `GNS3_API=v2`. GNS3 2.2 often needs no login; the local server's address from Docker is `http://host.docker.internal:3080` |

## 7. Practice reset
To put the routers back to the starting state (management stays on), paste
`gns3/reset-dataplane.txt` into each router. This is only for practice - clicking
**Run** again already removes the old config.
