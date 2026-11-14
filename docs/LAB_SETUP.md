# Lab setup (GNS3)

This builds the exam network in GNS3 and gives the routers a **management network**.
The system uses the management network to reach the routers over SSH. It never changes it.

```
              Management network <MGMT_NET>, e.g. 192.168.x.0/24 (VMware VMnet1, host-only)
                         |
   MGMT-CLOUD ---- MGMT-SW (Ethernet switch)
                    |                 |
        R1 Fa1/1 <R1_MGMT_IP>   R2 Fa1/1 <R2_MGMT_IP>
                    |                 |
   PC1 ---- Fa0/0 [R1] Fa1/0 ------- Fa1/0 [R2] Fa0/0 ---- PC2
 10.0.0.10   10.0.0.1     10.1.0.1  10.1.0.2    10.2.0.2      10.2.0.10
```

You do **not** configure Fa0/0, Fa1/0 or OSPF by hand. The system does that after approval.

## 1. What you need
* GNS3 3.x with the GNS3 VM (VMware). Tested with GNS3 3.1.
* A Cisco router image. We used **Cisco 7200** (`c7200-advipservicesk9-mz.124-24.T8`).
  IOSv also works - the system reads the real interface names from each router.
* VPCS for the PCs (built in to GNS3).

Cisco 7200 tip: set an **Idle-PC** value (right-click the router → Idle-PC), or each router
uses a full CPU core.

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
| PC1 e0 | R1 Fa0/0 | 10.0.0.0/24 (LAN) |
| R1 Fa1/0 | R2 Fa1/0 | 10.1.0.0/24 (R1-R2 link) |
| R2 Fa0/0 | PC2 e0 | 10.2.0.0/24 (LAN) |
| R1 Fa1/1 | MGMT-SW port 1 | management |
| R2 Fa1/1 | MGMT-SW port 2 | management |
| MGMT-CLOUD eth0 | MGMT-SW port 3 | management (to your PC) |

Other cabling is fine too. The system does not assume interface names - you confirm the
mapping on the Review page.

## 3. Management access (done once - the instructor's part in the exam)
First pick two free IPs in your management network, for example `192.168.x.11` and
`192.168.x.12` (where `192.168.x.0/24` is your VMnet1 network). Use the same IPs in
`inventory/devices.yaml`.

1. Start all nodes. Wait for the router prompt (first boot takes 1-2 minutes).
2. Open the R1 console and paste `gns3/R1-management-baseline.txt`. First replace the
   `<...>` values in it: the password (`ROUTER_SSH_PASSWORD`), R1's management IP and the
   netmask.
3. Do the same on R2 with `gns3/R2-management-baseline.txt`.
4. Check with `show ip interface brief`: only Fa1/1 has an IP address.
5. On PC1 and PC2 run the commands in `gns3/vpcs-commands.txt` (PCs may be configured by hand).
6. Check from your PC that both router IPs answer `ping`.

## 4. Where Docker runs
Docker must reach both router management IPs.

* **On the GNS3 PC (Docker Desktop)** - nothing extra. This is how we tested it.
* **On an Ubuntu VM** - give the VM a second network card on **VMnet1 (host-only)** with a
  fixed IP in the management network (e.g. 192.168.x.10/24), install Docker Engine with the
  Compose plugin, and copy the project to the VM.

Then continue with the README: settings, start, demo.

## 5. Practice reset
To put the routers back in the exam starting state (management stays on), paste
`gns3/reset-dataplane.txt` into each router console. You only need this to practise the
instructor's reset - a new **Run** already removes the old configuration.
