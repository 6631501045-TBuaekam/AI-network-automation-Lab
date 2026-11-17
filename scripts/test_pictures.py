"""Picture tests: send every picture in the given folders to the running app with NO requirements
text (upload -> Generate -> Review), and compare the result with that folder's expected.json.

Checks the values (router IPs, PC IPs and gateways, OSPF process, area, router IDs), that every
hand correction gets a warning, that clear pictures are not blocked, that red ink is not taken for
a correction, and that unclear pictures are stopped. Stops before Approve - routers are never changed.

    python scripts/test_pictures.py samples/demo
    (needs the app running: docker compose up -d; API address: env API, default localhost:8000)"""
import ipaddress, json, os, re, sys, time
import httpx
c = httpx.Client(base_url=os.environ.get("API", "http://localhost:8000"), timeout=300)
FIX = re.compile(r"struck|strike|cross|correct|replac|cancel|X mark|changed", re.I)
REVIEW_CATS = {"addressing", "subnet", "ospf", "safety", "ai_ambiguity"}
IGNORE = re.compile(r"not in the device inventory|no interface mapped|no configuration could be generated|not been confirmed", re.I)

def same(a, b):
    try: return ipaddress.ip_address(str(a).split("/")[0]) == ipaddress.ip_address(b)
    except ValueError: return False

rows = []
for folder in sys.argv[1:]:
    for t in json.load(open(os.path.join(folder, "expected.json"))):
        f = t["file"]
        with open(os.path.join(folder, f), "rb") as fh:
            pid = c.post("/api/diagrams", files={"file": (f, fh, "image/jpeg" if f.endswith(".jpg") else "image/png")},
                         data={"requirements": ""}).json()["id"]
        for attempt in range(3):
            r = c.post(f"/api/plans/{pid}/generate", json={"requirements": ""})
            if r.status_code == 200: break
            print("  retry:", r.text[:120]); time.sleep(20)
        else:
            rows.append((f, "AI ERROR", "")); continue
        topo = r.json()["topology"]; amb = topo.get("ambiguities") or []
        txt = lambda a: f"{a.get('item')} {a.get('issue')}"
        blocking = [a for a in amb if str(a.get("severity")).lower() == "blocking"]
        rev = c.post(f"/api/plans/{pid}/review").json()
        errs = [e for e in (rev.get("validation") or {}).get("errors", [])
                if e.get("category") in REVIEW_CATS and not IGNORE.search(e["message"])]
        problems = []
        print(f"\n=== {f}  ({t['why']})")
        for a in amb: print(f"   [{a.get('severity')}] {txt(a)[:170]}")
        for e in errs: print(f"   review: {e['message'][:170]}")
        if t.get("unclear"):
            verdict = "STOPPED (good)" if blocking or errs else "NOT STOPPED (BAD)"
            if not (blocking or errs): problems.append("should have stopped: " + t["should_flag"])
        else:
            got = {(i.get("device"), str(i.get("network"))): i.get("ip") for i in topo.get("interfaces", [])}
            wrong, total = [], 0
            for dev, nets in t["interfaces"].items():
                for net, ip in nets.items():
                    total += 1
                    if not same(got.get((dev, net)), ip): wrong.append(f"{dev} {net}: got {got.get((dev, net))} want {ip}")
            hosts = {h.get("name") or h.get("id"): h for h in topo.get("hosts", [])}
            for name, (ip, gw) in t["hosts"].items():
                h = hosts.get(name, {})
                for lab, g, w in ((name + " IP", h.get("ip"), ip), (name + " gw", h.get("gateway"), gw)):
                    total += 1
                    if not same(g, w): wrong.append(f"{lab}: got {g} want {w}")
            rt = topo.get("routing", {})
            for lab, g, w in (("process", rt.get("process_id"), t["ospf"]["process_id"]), ("area", rt.get("area"), t["ospf"]["area"])):
                total += 1
                if str(g) != str(w): wrong.append(f"{lab}: got {g} want {w}")
            for dev, rid in t["ospf"]["router_ids"].items():
                total += 1
                if str((rt.get("router_ids") or {}).get(dev)) != rid: wrong.append(f"{dev} RID: got {(rt.get('router_ids') or {}).get(dev)} want {rid}")
            if t.get("must_not_have_network"):
                total += 1
                if any(t["must_not_have_network"] == k[1] for k in got) or any(
                        t["must_not_have_network"] in json.dumps(n) for n in topo.get("networks", [])):
                    wrong.append(f"removed network {t['must_not_have_network']} is in the plan")
            if t.get("must_not_have_host"):
                total += 1
                if t["must_not_have_host"] in hosts: wrong.append(f"removed host {t['must_not_have_host']} is in the plan")
            problems += wrong
            for tok in t.get("corrections", []):
                short = "." + tok.split(".")[-1] if tok.count(".") == 3 else tok
                if not any((tok in txt(a) or short in txt(a)) and FIX.search(txt(a)) and str(a.get("severity")).lower() == "warning" for a in amb):
                    problems.append(f"no correction warning naming {tok}")
            if t.get("no_corrections"):
                problems += [f"false correction warning: {txt(a)[:90]}" for a in amb if FIX.search(txt(a))]
            if blocking: problems.append(f"wrongly blocked by AI: {[a.get('item') for a in blocking]}")
            if errs: problems.append(f"review errors: {[e['message'][:80] for e in errs]}")
            verdict = f"{total - len(wrong)}/{total}"
        for p in problems: print("   PROBLEM", p)
        rows.append((f, verdict, "OK" if not problems else f"{len(problems)} problem(s)"))
        time.sleep(2)
print("\nSUMMARY")
for f, v, p in rows: print(f"  {f:<40} {v:<20} {p}")
