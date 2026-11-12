# Lab build guide: GNS3 + Ubuntu Server VM

This guide builds the exam topology in GNS3, gives the routers an out-of-band management
network, and prepares an Ubuntu Server VM that runs the whole system with Docker Compose.

```
                      Management network 192.168.100.0/24 (host-only / VMnet1)
   +------------------+          |                                 
   | Ubuntu Server VM |----------+------ MGMT-SW (GNS3 Ethernet switch)
   | ens37 .10        |          |            |                  |
   | ens33 NAT (internet: Docker images, AI API)|                  |
   +------------------+   Cloud node      R1 Gi0/3 .11       R2 Gi0/3 .12
                                              |                  |
   PC1 ---- Gi0/0 [R1] Gi0/1 ------------- Gi0/1 [R2] Gi0/0 ---- PC2
   10.0.0.10   10.0.0.1    10.1.0.1     10.1.0.2   10.2.0.2      10.2.0.10
               (LAN, passive)      (OSPF area 0 adjacency)  (LAN, passive)
```

The data-plane interfaces (Gi0/0, Gi0/1) and OSPF are **not** configured by hand. The
system discovers the real interface names over SSH and pushes the config after approval.
Gi0/3 is management only and the system refuses to touch it.

---

## Part A - GNS3

### A1. Install
1. Install **GNS3 2.2.x** on your PC and the matching **GNS3 VM** (VMware Workstation/Player
   or VirtualBox). In GNS3 *Edit > Preferences > GNS3 VM* enable it.
2. Import the **Cisco IOSv** appliance (*File > Import appliance > cisco-iosv.gns3a*) with your
   legally obtained images (`vios-adventerprisek9-m.spa.159-3.m*.qcow2` and
   `IOSv_startup_config.img`, e.g. from Cisco Modeling Labs). Give it **4 adapters**
   (Gi0/0 - Gi0/3) - the default.
3. VPCS is built in (used for PC1 and PC2).

> Another router image (c7200, CSR1000v, IOL) also works: the system reads whatever
> interface names exist. Just keep one interface reserved for management.

### A2. Management (host-only) network
The Ubuntu VM and the routers' Gi0/3 must share one L2 network.

**VMware Workstation (Windows/Linux host)**
1. *Edit > Virtual Network Editor* > select **VMnet1 (Host-only)**.
2. Set subnet **192.168.100.0 / 255.255.255.0**. Keep DHCP on, but set its range to
   **192.168.100.128 - 192.168.100.254** (the GNS3 VM gets its address from DHCP there).
3. Restart the GNS3 VM so it takes a new VMnet1 address (GNS3 detects it automatically).

**VirtualBox**: *File > Tools > Network Manager > Host-only Networks* - create/edit
`vboxnet0` with 192.168.100.1/24, DHCP range .128-.254. In the GNS3 VM settings set the
host-only adapter's **Promiscuous Mode = Allow All**.

> If you prefer to keep your existing VMnet1 subnet, use it everywhere instead of
> 192.168.100.0/24 (router baseline files, inventory file, Ubuntu netplan).

### A3. Build the topology
New project `ai-netauto-ospf`, then drag in:

| Node | Type | Name |
|---|---|---|
| Router | Cisco IOSv | **R1** |
| Router | Cisco IOSv | **R2** |
| End host | VPCS | **PC1**, **PC2** |
| Switch | Ethernet switch | **MGMT-SW** |
| Cloud | Cloud (run on **GNS3 VM**) | **MGMT-CLOUD** |

Cabling:

| From | To | Purpose |
|---|---|---|
| PC1 Ethernet0 | R1 Gi0/0 | 10.0.0.0/24 LAN |
| R1 Gi0/1 | R2 Gi0/1 | 10.1.0.0/24 OSPF link |
| R2 Gi0/0 | PC2 Ethernet0 | 10.2.0.0/24 LAN |
| R1 Gi0/3 | MGMT-SW port 1 | management |
| R2 Gi0/3 | MGMT-SW port 2 | management |
| MGMT-CLOUD **eth0** | MGMT-SW port 3 | bridge to host-only VMnet1/vboxnet0 |

For the Cloud node: right-click > *Configure* > Ethernet interfaces > tick
*Show special Ethernet interfaces* if needed > add **eth0** (the GNS3 VM's host-only NIC).

> **This repo's lab files are set for a Cisco 7200 build** (no IOSv image available):
> Fa0/0 = LAN, Fa1/0 = R1-R2 link, Fa1/1 = management on MGMT-SW, and the management
> network is the existing VMnet1 subnet 192.168.192.0/24 (R1 .11, R2 .12). The files
> `gns3/*.txt` and `inventory/devices.example.yaml` use those values; read Gi0/0, Gi0/1 and
> Gi0/3 in this guide as Fa0/0, Fa1/0 and Fa1/1.

> Use any cabling you like - the system does not hard-code interface names. The review page
> shows the discovered interfaces and you confirm which one goes to which network.

### A4. Start and give the routers management access only
1. Start all nodes. Open the R1 console (wait for `Router>`; first boot takes ~2 min).
2. Paste `gns3/R1-management-baseline.txt` (replace `<ROUTER_PASSWORD>`).
3. Same on R2 with `gns3/R2-management-baseline.txt`.
4. Check: `show ip interface brief` - only Gi0/3 has an address; Gi0/0 and Gi0/1 are
   unassigned. **Do not configure them** - that is the system's job.
5. PCs: in each VPCS console run the commands in `gns3/vpcs-commands.txt`.

In the exam the instructor does steps A4.1-A4.4 (or resets your routers to this state).

---

## Part B - Ubuntu Server VM

### B1. Create the VM
* **Ubuntu Server 24.04 LTS**, 2 vCPU, 4 GB RAM (Kafka needs ~1 GB), 25 GB disk.
* **Adapter 1: NAT** (internet - Docker images and the AI API).
* **Adapter 2: Host-only VMnet1 / vboxnet0** (management network).
* Tick *Install OpenSSH server* during setup.

### B2. Static management IP
Find the interface names with `ip -br link` (typically `ens33` = NAT, `ens37` = host-only).

```bash
sudo tee /etc/netplan/60-mgmt.yaml >/dev/null <<'EOF'
network:
  version: 2
  ethernets:
    ens37:
      dhcp4: false
      addresses: [192.168.100.10/24]
EOF
sudo chmod 600 /etc/netplan/60-mgmt.yaml
sudo netplan apply
ip -br addr
ping -c 2 192.168.100.11 && ping -c 2 192.168.100.12
```

### B3. Install Docker Engine + Compose plugin
```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
 | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER && newgrp docker
docker compose version
```

### B4. Prove SSH management works (read-only check, not configuration)
```bash
ssh -o KexAlgorithms=+diffie-hellman-group14-sha1 -o HostKeyAlgorithms=+ssh-rsa netadmin@192.168.100.11 "show ip interface brief"
```
(OpenSSH 9 needs the legacy-algorithm flags for IOSv. The worker's Paramiko 3.5 supports
them natively.) Type `exit` if it opens a shell. Do the same for .12.

### B5. Copy the project to the VM
From your PC: `scp ai-netauto.zip <user>@<ubuntu-ip>:~` then on the VM
`sudo apt-get install -y unzip && unzip ai-netauto.zip && cd ai-netauto`
(or push it to a private Git repo and `git clone` it).

Continue with **README.md - "Run the system"**.
