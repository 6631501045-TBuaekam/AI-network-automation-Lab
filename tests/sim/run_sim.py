"""Rehearsal lab: two simulated IOS routers over SSH (R1 on :2201, R2 on :2202).

NOT for the exam demo - the demo must use the real GNS3 routers. Use this to check the
Docker stack, Kafka, worker and UI end-to-end on the Ubuntu VM before GNS3 is ready:
    docker compose -f docker-compose.yml -f docker-compose.sim.yml up -d --build
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fake_ios import Lab  # noqa: E402

lab = Lab(adjacency_delay=float(os.getenv("SIM_ADJACENCY_DELAY", "8")),
          username=os.getenv("SIM_USERNAME", "netadmin"),
          password=os.getenv("SIM_PASSWORD", "lab-pass"))
lab.add_router("R1", "192.168.100.11")
lab.add_router("R2", "192.168.100.12")
lab.add_host("10.0.0.10", "10.0.0.0/24", "10.0.0.1")
lab.add_host("10.2.0.10", "10.2.0.0/24", "10.2.0.2")
lab.serve("R1", "0.0.0.0", 2201)
lab.serve("R2", "0.0.0.0", 2202)
print("simulated R1 on :2201, R2 on :2202 (Cisco IOS look-alike)", flush=True)
while True:
    time.sleep(3600)
