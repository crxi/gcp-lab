#!/usr/bin/env python3
"""glab -- a small Compute Engine control tool.

The same shape as `lab` for EC2: create instances, run commands on them, copy
files, and delete them again, with no local state. Everything it creates
carries the label `lab=1`, and it only touches resources carrying that label.

    glab project my-project
    glab zone asia-southeast1-b
    glab types
    glab init box
    glab run box 'nvidia-smi'
    glab destroy box

Most commands have run against a real project; `shell`, spot, public IP and
GPU launches have not. See the "Unverified" section of AGENTS.md.
"""

import argparse
import cmd as cmdlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import lru_cache

try:
    from google.cloud import compute_v1
    from google.api_core.exceptions import GoogleAPICallError, NotFound
    import google.auth
    from google.auth.transport.requests import AuthorizedSession, Request
except ImportError:
    sys.exit("missing dependencies. run ./setup.sh, or:\n"
             "  pip install google-cloud-compute google-auth")

# ---------------------------------------------------------------- constants

LABEL_KEY = "lab"
CONFIG_DIR = os.path.expanduser("~/.glab")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
PRICE_CACHE_PATH = os.path.join(CONFIG_DIR, "prices.json")
PRICE_CACHE_DAYS = 30

# Compute Engine's service id in the Cloud Billing Catalog. Prices come from
# that API, per region, the way `lab types` reads the AWS Pricing API.
COMPUTE_SERVICE = "services/6F81-5844-456A"
BILLING_API = "https://cloudbilling.googleapis.com/v1"

# The firewall rule that lets you in. 35.235.240.0/20 is Google's
# Identity-Aware Proxy range and the only source allowed; a connection from it
# still has to pass IAM. Nothing on the public internet can reach port 22.
IAP_RANGE = "35.235.240.0/20"
FIREWALL_NAME = "lab-allow-iap-ssh"
NETWORK_TAG = "lab"

# The CPU machine types `glab types` shows. GPU machine types and accelerators
# are not listed here: which ones a zone sells varies, and they are discovered
# from the zone. Hardcoding them is how you end up reporting that a zone has no
# GPUs because it has none of yours.
SHORTLIST = ["e2-micro", "e2-small", "e2-medium",
             "n2-standard-2", "n2-standard-4", "c3-standard-4"]

# Fallback prices, USD/hour, asia-southeast1, used only when the Billing
# Catalog is unreachable. Computed by `machine_price` from the catalog on
# 2026-09-25; the API is the source of truth and these drift.
PRICES = {
    "e2-micro": 0.0103, "e2-small": 0.0207, "e2-medium": 0.0413,
    "n1-standard-4": 0.2344, "n2-standard-2": 0.1198, "n2-standard-4": 0.2396,
    "c3-standard-4": 0.2487, "g2-standard-4": 0.8720, "g2-standard-8": 1.0531,
}
SPOT_PRICES = {
    "e2-micro": 0.0062, "e2-small": 0.0124, "e2-medium": 0.0248,
    "n1-standard-4": 0.0623, "n2-standard-2": 0.0459, "n2-standard-4": 0.0917,
    "c3-standard-4": 0.1341, "g2-standard-4": 0.5233, "g2-standard-8": 0.6319,
}

# Persistent disk, USD/GB-month, asia-southeast1. A stopped instance still pays
# this, which is the same trap as an EBS volume on a stopped EC2 instance.
DISK_GB_MONTH = {"pd-standard": 0.048, "pd-balanced": 0.120, "pd-ssd": 0.204}

# An external IPv4 address is billed whether or not traffic flows over it.
EXTERNAL_IP_HOUR = 0.005

BOOT_IMAGE = "projects/debian-cloud/global/images/family/debian-12"

# A custom image, USD/GiB-month, from the Billing Catalog's "Storage Image in
# Singapore" on 2026-09-25. `image-create` stores images in the working region
# so this is the rate that applies.
IMAGE_GB_MONTH = 0.055


# ---------------------------------------------------------------- plumbing

def die(message):
    sys.exit(f"error: {message}")


def api_message(e):
    """Keep what Google said. A bare 403 is unusable; the message names the
    permission and the project."""
    return f"google said no: {getattr(e, 'message', None) or e}"


def table(rows, headers):
    if not rows:
        return
    widths = [max(len(str(r[i])) for r in [headers] + rows)
              for i in range(len(headers))]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)))


def read_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_config(**kv):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    config = read_config()
    config.update({k: v for k, v in kv.items() if v is not None})
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


@lru_cache(maxsize=None)
def credentials():
    try:
        creds, default_project = google.auth.default()
    except Exception as e:
        die(f"no credentials. run `gcloud auth application-default login`.\n{e}")
    return creds, default_project


def current_project():
    project = read_config().get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if project:
        return project
    _, default_project = credentials()
    if not default_project:
        die("no project set. run `glab project PROJECT_ID`.")
    return default_project


def current_zone():
    zone = read_config().get("zone")
    if not zone:
        die("no zone set. run `glab zone asia-southeast1-b`. "
            "`glab zones` lists them.")
    return zone


def current_region(zone=None):
    return (zone or current_zone()).rsplit("-", 1)[0]


@lru_cache(maxsize=None)
def instances_client():
    return compute_v1.InstancesClient()


def wait(operation, zone=None, timeout=600):
    """Block on a zone or global operation. Every mutating call returns one."""
    if operation is None:
        return
    project = current_project()
    deadline = time.monotonic() + timeout
    # operations.wait returns after about two minutes whether or not the
    # operation is done, so call it again until it is. A stop can take longer.
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            die(f"operation {operation.name} did not finish within {timeout}s; "
                "it may still be running. Check `glab list --all-zones`.")
        if zone:
            client = compute_v1.ZoneOperationsClient()
            result = client.wait(project=project, zone=zone,
                                 operation=operation.name,
                                 timeout=min(150, remaining), retry=None)
        else:
            client = compute_v1.GlobalOperationsClient()
            result = client.wait(project=project, operation=operation.name,
                                 timeout=min(150, remaining), retry=None)
        if result.status == compute_v1.Operation.Status.DONE:
            break
    if result.error and result.error.errors:
        die("; ".join(e.message for e in result.error.errors))
    return result


def has_lab_label(resource):
    return (resource.labels or {}).get(LABEL_KEY) == "1"


# ---------------------------------------------------------------- reading

def describe(zone=None):
    """Every lab instance in one zone, as plain dicts."""
    zone = zone or current_zone()
    out = []
    # The flattened keyword form takes project and zone only; a filter has to
    # go in the request object.
    request = compute_v1.ListInstancesRequest(
        project=current_project(), zone=zone, filter=f"labels.{LABEL_KEY}=1")
    for i in instances_client().list(request=request):
        nic = i.network_interfaces[0] if i.network_interfaces else None
        access = nic.access_configs[0] if (nic and nic.access_configs) else None
        out.append({
            "name": i.name,
            "id": str(i.id),
            "type": i.machine_type.rsplit("/", 1)[-1],
            "state": i.status.lower(),
            "zone": zone,
            "private_ip": nic.network_i_p if nic else "-",
            "ip": getattr(access, "nat_i_p", None) or "-",
            "launched": i.creation_timestamp,
            "started": i.last_start_timestamp,
            "buy": ("spot" if i.scheduling
                    and i.scheduling.provisioning_model == "SPOT" else "on-demand"),
            "gpus": sum(a.accelerator_count for a in (i.guest_accelerators or [])),
            "accelerators": [(a.accelerator_type.rsplit("/", 1)[-1],
                              a.accelerator_count)
                             for a in (i.guest_accelerators or [])],
            "disks": [d.source.rsplit("/", 1)[-1]
                      for d in i.disks if getattr(d, "source", None)],
        })
    return sorted(out, key=lambda x: x["name"])


def find(name, pool=None):
    pool = describe() if pool is None else pool
    matches = [i for i in pool if i["name"] == name]
    if not matches:
        die(f"no instance named {name!r} in {current_zone()}")
    return matches[0]


def find_running(name):
    i = find(name)
    if i["state"] != "running":
        die(f"{name} is {i['state']}; try `glab start {name}`")
    return i


@lru_cache(maxsize=None)
def zone_names(region=None):
    client = compute_v1.ZonesClient()
    names = [z.name for z in client.list(project=current_project())
             if z.status == "UP"]
    if region:
        names = [z for z in names if z.startswith(region + "-")]
    return sorted(names)


@lru_cache(maxsize=None)
def zone_catalog(zone):
    """{machine type: spec} for everything one zone sells. Unfiltered, because
    the point is to find out what is there rather than confirm a guess."""
    client = compute_v1.MachineTypesClient()
    out = {}
    for m in client.list(project=current_project(), zone=zone):
        gpus = m.accelerators[0] if m.accelerators else None
        out[m.name] = {
            "vcpu": m.guest_cpus,
            "ram": round(m.memory_mb / 1024),
            "gpu": (f"{gpus.guest_accelerator_count} x "
                    f"{gpus.guest_accelerator_type}" if gpus else "-"),
            "gpus": gpus.guest_accelerator_count if gpus else 0,
            "accelerator": gpus.guest_accelerator_type if gpus else None,
            "ram_gb": m.memory_mb / 1024,
        }
    return out


@lru_cache(maxsize=None)
def zone_accelerators(zone):
    """{accelerator type: max per instance}. These are the GPUs you attach to
    an N1 machine; the G2/A2/A3 families have theirs built in and show up in
    the machine-type catalog instead."""
    client = compute_v1.AcceleratorTypesClient()
    return {a.name: a.maximum_cards_per_instance
            for a in client.list(project=current_project(), zone=zone)}


# ---------------------------------------------------------------- prices

def billing_session():
    creds, _ = credentials()
    return AuthorizedSession(creds)


@lru_cache(maxsize=None)
def compute_skus(region):
    """Every Compute Engine SKU that applies to one region. One paginated call,
    cached, because the catalog is thousands of rows and does not change often."""
    session = billing_session()
    skus, token = [], None
    while True:
        params = {"pageSize": 5000}
        if token:
            params["pageToken"] = token
        r = session.get(f"{BILLING_API}/{COMPUTE_SERVICE}/skus", params=params,
                        timeout=60)
        r.raise_for_status()
        body = r.json()
        for sku in body.get("skus", []):
            if region in sku.get("serviceRegions", []):
                skus.append(sku)
        token = body.get("nextPageToken")
        if not token:
            break
    return skus


def sku_hourly(sku):
    """USD/hour (or /GB-month) from a SKU's first pricing tier."""
    try:
        expr = sku["pricingInfo"][0]["pricingExpression"]
        tier = expr["tieredRates"][-1]["unitPrice"]
    except (KeyError, IndexError):
        return None
    return int(tier.get("units", 0)) + tier.get("nanos", 0) / 1e9


# Shared-core types are billed as this many vCPUs in total, not per vCPU.
SHARED_CORE = {"e2-micro": 0.25, "e2-small": 0.5, "e2-medium": 1.0,
               "f1-micro": 0.2, "g1-small": 0.5}

# Accelerator type -> the start of its Billing Catalog description. A GPU is a
# separate SKU from the cores and RAM, including on G2/A2/A3 where it comes
# with the machine type. Types not listed here (B200, TPUs) are priced some
# other way and come back as None.
GPU_SKU = {
    "nvidia-l4": "Nvidia L4 GPU",
    "nvidia-tesla-t4": "Nvidia Tesla T4 GPU",
    "nvidia-tesla-p4": "Nvidia Tesla P4 GPU",
    "nvidia-tesla-a100": "Nvidia Tesla A100 GPU",
    "nvidia-a100-80gb": "Nvidia Tesla A100 80GB GPU",
    "nvidia-h100-80gb": "Nvidia H100 80GB GPU",
    "nvidia-h100-mega-80gb": "Nvidia H100 80GB Mega GPU",
    "nvidia-rtx-pro-6000": "RTX 6000 96GB",
}


def gpu_price(accelerator, region, spot=False):
    """USD/hour for one GPU, or None if the catalog has no match."""
    name = GPU_SKU.get(accelerator)
    if not name:
        return None
    tail = "attached to Spot Preemptible VMs running in" if spot else "running in"
    pattern = rf"^{re.escape(name)} {tail} "
    for sku in compute_skus(region):
        if re.match(pattern, sku.get("description", "")):
            return sku_hourly(sku)
    return None


def machine_price(machine_type, region, spot=False):
    """Price one machine type by summing its vCPU, RAM and GPU SKUs.

    Compute Engine does not sell a machine type as a line item. It sells core
    hours and GB hours per family, plus GPU hours, and the machine type is a
    bundle of those. Matching is on the exact description, "E2 Instance Core"
    and not "E2 Custom Instance Core", because a near miss gives a plausible
    wrong number rather than an error. N1 alone says "N1 Predefined Instance
    Core"."""
    family = machine_type.split("-")[0].upper()
    prefix = "Spot Preemptible " if spot else ""
    core = ram = None
    for sku in compute_skus(region):
        desc = sku.get("description", "")
        if sku.get("category", {}).get("resourceFamily") != "Compute":
            continue
        if re.match(rf"^{prefix}{family} (Predefined )?Instance Core running in ", desc):
            core = sku_hourly(sku)
        elif re.match(rf"^{prefix}{family} (Predefined )?Instance Ram running in ", desc):
            ram = sku_hourly(sku)
    spec = None
    for zone in zone_names(region):
        spec = zone_catalog(zone).get(machine_type)
        if spec:
            break
    if not spec or core is None or ram is None:
        return None
    # G4 below 48 vCPU is a slice of one GPU (g4-standard-6 is 1/8), billed
    # under a separate vGPU SKU rather than the full-GPU one.
    if family == "G4" and spec["vcpu"] < 48:
        return None
    vcpu = SHARED_CORE.get(machine_type, spec["vcpu"])
    price = core * vcpu + ram * spec.get("ram_gb", spec["ram"])
    if spec.get("gpus"):
        each = gpu_price(spec.get("accelerator"), region, spot)
        if each is None:
            return None
        price += each * spec["gpus"]
    return price


def region_prices(region, names, spot=False, refresh=False):
    """{type: price or None}, cached on disk for a month."""
    names = sorted(names)
    key = f"{region}:{'spot' if spot else 'ondemand'}"
    try:
        with open(PRICE_CACHE_PATH) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    entry = cache.get(key) or {}
    fresh = (not refresh and
             time.time() - entry.get("fetched", 0) < PRICE_CACHE_DAYS * 86400)
    if not fresh:
        compute_skus.cache_clear()
    known = entry.get("prices", {}) if fresh else {}
    missing = [n for n in names if n not in known]
    if not missing:
        return {n: known[n] for n in names}, "cached"

    prices = dict(known)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(machine_price, n, region, spot): n for n in missing}
        for future in as_completed(futures):
            prices[futures[future]] = future.result()

    cache[key] = {"fetched": entry["fetched"] if fresh else time.time(),
                  "prices": prices}
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(PRICE_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)
    return {n: prices[n] for n in names}, "live"


def instance_price(machine_type, zone, spot=False, accelerators=()):
    """Regional compute estimate, including attached GPUs; None if incomplete."""
    region = current_region(zone)
    try:
        prices, source = region_prices(region, [machine_type], spot=spot)
        price = prices[machine_type]
        if price is None:
            return None, source
        # Built-in GPUs are already included by machine_price. N1 attachments
        # are instance properties and must be added separately.
        if accelerators and not zone_catalog(zone).get(machine_type, {}).get("gpus"):
            for model, count in accelerators:
                each = gpu_price(model, region, spot)
                if each is None:
                    return None, source
                price += each * count
        return price, source
    except Exception:
        # These tables cover one region and do not price attached GPUs.
        if region == "asia-southeast1" and not accelerators:
            return (SPOT_PRICES if spot else PRICES).get(machine_type), "fallback"
        return None, "unavailable"


# ---------------------------------------------------------------- shared setup

def ensure_firewall():
    """One ingress rule: port 22 from Google's IAP range, and only for
    instances tagged `lab`. IAP still checks IAM on every connection, so this
    is not an open port -- but it is an ingress rule, and `lab` on EC2 has
    none at all, because Systems Manager needs no listener. Compute Engine has
    no Systems Manager, so this is the closest equivalent."""
    client = compute_v1.FirewallsClient()
    project = current_project()
    try:
        rule = client.get(project=project, firewall=FIREWALL_NAME)
        sources = list(rule.source_ranges or [])
        if sources != [IAP_RANGE]:
            die(f"firewall {FIREWALL_NAME} allows {sources}, not just "
                f"{IAP_RANGE}. Someone widened it. Fix or delete it.")
        return rule.name
    except NotFound:
        pass
    firewall = compute_v1.Firewall(
        name=FIREWALL_NAME,
        description="glab: ssh from the IAP range only",
        network="global/networks/default",
        direction="INGRESS",
        source_ranges=[IAP_RANGE],
        target_tags=[NETWORK_TAG],
        allowed=[compute_v1.Allowed(I_p_protocol="tcp", ports=["22"])],
    )
    wait(client.insert(project=project, firewall_resource=firewall))
    print(f"created firewall {FIREWALL_NAME} ({IAP_RANGE} -> tcp:22)")
    return FIREWALL_NAME


# ---------------------------------------------------------------- commands

def cmd_login(args):
    credentials.cache_clear()
    try:
        creds, _ = google.auth.default()
        creds.refresh(Request())
    except Exception:
        print("no usable credentials. running `gcloud auth login`...")
        for command in (["gcloud", "auth", "login"],
                        ["gcloud", "auth", "application-default", "login"]):
            result = subprocess.run(command)
            if result.returncode:
                sys.exit(result.returncode)
        credentials.cache_clear()
    cmd_whoami(args)


def cmd_whoami(args):
    creds, default_project = credentials()
    # User credentials carry no email, and printing one is not wanted anyway;
    # say what kind of credential it is and which project quota is billed to.
    kind = ("service account" if getattr(creds, "service_account_email", None)
            else "user credentials")
    quota = getattr(creds, "quota_project_id", None) or "(none)"
    print(f"project : {current_project()}")
    print(f"identity: {kind}")
    print(f"quota   : {quota}")
    print(f"zone    : {read_config().get('zone', '(unset)')}")


def cmd_project(args):
    if args.name:
        write_config(project=args.name)
    print(current_project())


def cmd_zone(args):
    if args.name:
        if args.name not in zone_names():
            die(f"{args.name} is not a zone in this project. "
                f"`glab zones` lists them.")
        write_config(zone=args.name)
    print(current_zone())


def cmd_zones(args):
    # Every zone is ~120 zones at two calls each, so default to the region of
    # the working zone and fetch in parallel.
    region = args.region
    if not region and not args.all and read_config().get("zone"):
        region = current_region()

    def row(zone):
        gpus = zone_accelerators(zone)
        return [zone, len(zone_catalog(zone)), ", ".join(sorted(gpus)) or "-"]

    with ThreadPoolExecutor(max_workers=16) as pool:
        rows = list(pool.map(row, zone_names(region)))
    table(rows, ["zone", "machine types", "attachable GPUs"])


def cmd_types(args):
    zone = current_zone()
    region = current_region(zone)
    catalog = zone_catalog(zone)
    cpu = [n for n in SHORTLIST if n in catalog]

    # Every machine type in the zone with a GPU built in, grouped by family.
    gpu = sorted(n for n, s in catalog.items() if s["gpus"])
    families = {}
    for name in gpu:
        families.setdefault(name.split("-")[0], []).append(name)
    if args.gpu:
        shown = gpu
    else:
        shown = sorted(min(sizes, key=lambda n: catalog[n]["vcpu"])
                       for sizes in families.values())

    try:
        prices, source = region_prices(region, cpu + shown, spot=args.spot,
                                       refresh=args.no_cache)
    except Exception as e:
        print(f"could not read the Billing Catalog ({type(e).__name__}); "
              f"showing the built-in table instead\n")
        prices = dict(SPOT_PRICES if args.spot else PRICES)
        source = "fallback"

    def row(name):
        spec = catalog.get(name)
        price = prices.get(name)
        return [name, spec["vcpu"] if spec else "?",
                f"{spec['ram']}GB" if spec else "?",
                spec["gpu"] if spec else "?",
                f"${price:.4f}" if price else "-",
                f"${price * 24 * 30:,.0f}" if price else "-"]

    by_price = lambda n: (prices.get(n) is None, prices.get(n) or 0)
    headers = ["type", "vCPU", "RAM", "GPU", "per hour", "per month"]
    table([row(n) for n in sorted(cpu, key=by_price)], headers)

    attachable = zone_accelerators(zone)
    if shown:
        print(f"\n{len(gpu)} GPU machine types in {zone}, "
              f"{len(families)} families: {', '.join(sorted(families))}")
        table([row(n) for n in sorted(shown, key=by_price)], headers)
        if not args.gpu:
            print(f"smallest size of each family. `glab types --gpu` lists all "
                  f"{len(gpu)}.")
    if attachable:
        print(f"\nattachable to an N1 machine in {zone}: "
              f"{', '.join(f'{k} (max {v})' for k, v in sorted(attachable.items()))}")
    if not shown and not attachable:
        print(f"\n{zone} has no GPUs. `glab zones` shows which zones do.")

    kind = "spot" if args.spot else "on-demand"
    if source == "fallback":
        print(f"\nprices are the built-in {kind} table for asia-southeast1.")
    else:
        stale = "  (cached; --no-cache to refresh)" if source == "cached" else ""
        print(f"\n{kind} prices for {region}, from the Billing Catalog.{stale}")
        print(f"disk is extra, ${DISK_GB_MONTH['pd-balanced']}/GB-month "
              f"for pd-balanced.")


def cmd_quota(args):
    """GPU launches fail on quota long before they fail on capacity. Compute
    Engine has three that matter and they are checked in different places: a
    project-wide GPUS_ALL_REGIONS, a per-region count for each GPU model, and a
    separate per-region count for the preemptible/spot version of it."""
    project = current_project()
    p = compute_v1.ProjectsClient().get(project=project)
    globals_ = {q.metric: (q.limit, q.usage) for q in p.quotas}
    if "GPUS_ALL_REGIONS" in globals_:
        limit, usage = globals_["GPUS_ALL_REGIONS"]
        print(f"GPUS_ALL_REGIONS (project-wide): {usage:g} / {limit:g}")
        if not limit:
            print("  zero here blocks every GPU launch in every region, "
                  "whatever the regional quota says.")

    regions = ([args.region] if args.region else
               sorted({current_region()} if not args.all_regions else
                      {r.name for r in compute_v1.RegionsClient().list(project=project)}))
    client = compute_v1.RegionsClient()
    rows = []
    for name in regions:
        try:
            region = client.get(project=project, region=name)
        except GoogleAPICallError:
            continue
        for q in region.quotas:
            if "GPU" not in q.metric and q.metric not in ("CPUS", "PREEMPTIBLE_CPUS"):
                continue
            if not args.all and not q.limit and "GPU" in q.metric:
                continue
            rows.append([name, q.metric, f"{q.usage:g}", f"{q.limit:g}"])
    table(rows, ["region", "metric", "used", "limit"])
    print("\nraise them at "
          "https://console.cloud.google.com/iam-admin/quotas")
    print("a GPU request needs GPUS_ALL_REGIONS and the per-region metric; "
          "spot is a third.")


def image_path(value):
    """`--image` accepts a full path, `family/NAME` for the newest image in one
    of this project's families, or a bare image name in this project."""
    if not value:
        return BOOT_IMAGE
    if value.startswith(("projects/", "https://")):
        return value
    return f"projects/{current_project()}/global/images/{value}"


def cmd_init(args):
    name = args.name
    if any(i["name"] == name for i in describe()):
        die(f"an instance named {name!r} already exists")
    zone = current_zone()
    project = current_project()
    ensure_firewall()

    nic = compute_v1.NetworkInterface(network="global/networks/default")
    if args.public_ip:
        # Without one there is no route to the internet, so no apt and no model
        # download. IAP still reaches the instance either way. The address is
        # billed by the hour whether or not it carries traffic.
        nic.access_configs = [compute_v1.AccessConfig(
            name="External NAT", type_="ONE_TO_ONE_NAT")]

    disk = compute_v1.AttachedDisk(
        boot=True, auto_delete=True,
        initialize_params=compute_v1.AttachedDiskInitializeParams(
            source_image=image_path(args.image),
            disk_size_gb=args.disk, disk_type=f"zones/{zone}/diskTypes/{args.disk_type}"),
    )

    scheduling = compute_v1.Scheduling()
    if args.gpu or zone_catalog(zone).get(args.type, {}).get("gpus"):
        scheduling.on_host_maintenance = "TERMINATE"
    if args.spot:
        # STOP rather than DELETE, so `glab start` can bring it back the way
        # `lab start` does with a persistent EC2 spot request.
        scheduling.provisioning_model = "SPOT"
        scheduling.instance_termination_action = "STOP"
        scheduling.automatic_restart = False
        scheduling.on_host_maintenance = "TERMINATE"
    max_run = getattr(args, "max_run", None)
    if max_run:
        # Compute Engine deletes the instance, boot disk included, this long
        # after it starts, whether or not anything here is still running to
        # do it. DELETE also replaces STOP for a spot preemption.
        scheduling.max_run_duration = compute_v1.Duration(seconds=max_run * 60)
        scheduling.instance_termination_action = "DELETE"

    labels = {LABEL_KEY: "1"}
    if getattr(args, "run_id", None):
        labels["lab-run"] = args.run_id
    instance = compute_v1.Instance(
        name=name,
        machine_type=f"zones/{zone}/machineTypes/{args.type}",
        disks=[disk],
        network_interfaces=[nic],
        scheduling=scheduling,
        labels=labels,
        tags=compute_v1.Tags(items=[NETWORK_TAG]),
        # OS Login means SSH keys come from IAM, not from project metadata, so
        # nothing here creates or stores a keypair.
        metadata=compute_v1.Metadata(items=[
            compute_v1.Items(key="enable-oslogin", value="TRUE")]),
        shielded_instance_config=compute_v1.ShieldedInstanceConfig(
            enable_secure_boot=True, enable_vtpm=True,
            enable_integrity_monitoring=True),
    )
    if args.gpu:
        model, _, count = args.gpu.partition(":")
        instance.guest_accelerators = [compute_v1.AcceleratorConfig(
            accelerator_type=f"zones/{zone}/acceleratorTypes/{model}",
            accelerator_count=int(count or 1))]

    op = instances_client().insert(project=project, zone=zone,
                                   instance_resource=instance)
    wait(op, zone)
    print(f"{name}: {args.type} starting in {zone}")
    attached = [(model, int(count or 1))] if args.gpu else []
    price, source = instance_price(args.type, zone, args.spot, attached)
    if max_run:
        print(f"deleted by Compute Engine {max_run} minutes after it starts")
    if price:
        print(f"compute estimate: ~${price:.4f}/hour ({source}); "
              "disk, external IP and network usage are extra")
    else:
        print("compute estimate unavailable for this configuration")
    print(f"\nwait a minute, then: glab run {name} 'uname -a'")


def uptime(started, state, now=None):
    """How long an instance has been running since its last start, as 3d4h,
    5h12m or 7m. Only a running instance has one; a stopped one bills for its
    disk, not its hours, so it shows "-"."""
    if state != "running" or not started:
        return "-"
    try:
        t = datetime.fromisoformat(started)
    except ValueError:
        return "-"
    minutes = max(0, int(((now or datetime.now(timezone.utc)) - t).total_seconds() // 60))
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days}d{hours}h"
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def cmd_list(args):
    zones = zone_names(args.region) if (args.all_zones or args.region) \
        else [current_zone()]
    rows = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        for found in pool.map(describe, zones):
            for i in found:
                rows.append([i["name"], i["type"], i["state"], i["zone"],
                             uptime(i.get("started"), i["state"]),
                             i["private_ip"], i["ip"], i["buy"],
                             i["gpus"] or "-"])
    if not rows:
        where = "any zone" if args.all_zones else current_zone()
        print(f"no lab instances in {where}")
        return
    table(sorted(rows), ["name", "type", "state", "zone", "up", "private ip",
                         "public ip", "buy", "gpu"])


def cmd_start(args):
    i = find(args.name)
    wait(instances_client().start(project=current_project(), zone=i["zone"],
                                  instance=i["name"]), i["zone"])
    print(f"{args.name} running. ssh is ready about 30 seconds after this.")


def cmd_stop(args):
    i = find(args.name)
    wait(instances_client().stop(project=current_project(), zone=i["zone"],
                                 instance=i["name"]), i["zone"])
    print(f"{args.name} stopping. the disk still bills while it is stopped.")


def gcloud_ssh(name, zone, extra):
    """Shell out to gcloud for ssh. The IAP tunnel and the OS Login key
    exchange are both gcloud's, and reimplementing either here would mean
    handling keys, which this tool does not do."""
    return ["gcloud", "compute", "ssh", name, "--zone", zone,
            "--tunnel-through-iap", "--project", current_project()] + extra


def cmd_shell(args):
    i = find_running(args.name)
    os.execvp("gcloud", gcloud_ssh(i["name"], i["zone"], []))


def cmd_run(args):
    i = find_running(args.name)
    command = " ".join(args.command)
    # The whole command crosses a shell on the far side, so it goes over as one
    # quoted operand. A filename is data.
    r = subprocess.run(gcloud_ssh(i["name"], i["zone"],
                                  ["--command", command, "--", "-q"]))
    sys.exit(r.returncode)


def cmd_push(args):
    i = find_running(args.name)
    local = os.path.abspath(os.path.expanduser(args.local))
    if not os.path.exists(local):
        die(f"no such file or directory: {local}")
    remote = args.remote or f"~/{os.path.basename(local)}"
    r = subprocess.run(["gcloud", "compute", "scp", "--recurse",
                        "--tunnel-through-iap", "--zone", i["zone"],
                        "--project", current_project(),
                        local, f"{i['name']}:{remote}"])
    if r.returncode:
        sys.exit(r.returncode)
    print(f"{local} -> {args.name}:{remote}")


def cmd_pull(args):
    i = find_running(args.name)
    local = os.path.abspath(os.path.expanduser(
        args.local or os.path.basename(args.remote.rstrip("/"))))
    r = subprocess.run(["gcloud", "compute", "scp", "--recurse",
                        "--tunnel-through-iap", "--zone", i["zone"],
                        "--project", current_project(),
                        f"{i['name']}:{args.remote}", local])
    if r.returncode:
        sys.exit(r.returncode)
    print(f"{args.name}:{args.remote} -> {local}")


def cmd_cost(args):
    found = describe()
    if not found:
        print(f"nothing running in {current_zone()}")
        return
    rows, hourly, unknown = [], 0.0, []
    for i in found:
        price, source = (0.0, "stopped") if i["state"] == "terminated" else \
            instance_price(i["type"], i["zone"], i["buy"] == "spot",
                           i.get("accelerators", []))
        if i["state"] != "terminated":
            if price is None:
                unknown.append(i["name"])
            else:
                hourly += price
        rows.append([i["name"], i["type"], i["state"], i["buy"],
                     f"${price:.4f}" if price is not None else "?", source])
    table(rows, ["name", "type", "state", "buy", "per hour", "source"])
    label = "known compute subtotal" if unknown else "compute estimate"
    print(f"\n{label}: ~${hourly:.4f}/hour, ${hourly * 24:.2f}/day")
    if unknown:
        print(f"incomplete: no price for {', '.join(unknown)}")
    print("disks bill whether the instance runs or not; "
          "`glab list` shows what exists.")
    print("disk, external IP and network usage are excluded; "
          "estimates are not this project's bill.")


def cmd_destroy(args):
    targets = describe() if args.all else [find(args.name)]
    if not targets:
        print("nothing to destroy")
        return
    for t in targets:
        print(f"  {t['name']}  {t['type']}  {t['zone']}")
    if not getattr(args, "yes", False):
        if input(f"delete {len(targets)} instance(s)? [y/N] ").strip().lower() != "y":
            print("cancelled")
            return
    client = instances_client()
    ops = [(client.delete(project=current_project(), zone=t["zone"],
                          instance=t["name"]), t["zone"]) for t in targets]
    for op, zone in ops:
        wait(op, zone)
    print(f"deleted {len(ops)} instance(s). boot disks went with them.")


def images_client():
    return compute_v1.ImagesClient()


def lab_images():
    request = compute_v1.ListImagesRequest(
        project=current_project(), filter=f"labels.{LABEL_KEY}=1")
    return sorted(images_client().list(request=request),
                  key=lambda i: i.creation_timestamp)


def cmd_images(args):
    rows = []
    for i in lab_images():
        archive_gb = (i.archive_size_bytes or 0) / 1024**3
        rows.append([i.name, i.family or "-", i.status.lower(),
                     i.disk_size_gb, f"{archive_gb:.1f}",
                     f"${archive_gb * IMAGE_GB_MONTH:.2f}",
                     i.creation_timestamp[:16]])
    if not rows:
        print("no lab images")
        return
    table(rows, ["name", "family", "status", "disk GB", "stored GB",
                 "per month", "created"])
    print(f"\nstorage priced at ${IMAGE_GB_MONTH}/GiB-month on the stored size. "
          "`glab image-delete NAME` removes one.")


def cmd_image_create(args):
    """Make an image from a stopped instance's boot disk. The instance has to
    be stopped so the disk is not changing while it is copied."""
    i = find(args.source)
    if i["state"] != "terminated":
        die(f"{args.source} is {i['state']}; `glab stop {args.source}` first")
    image = compute_v1.Image(
        name=args.name,
        family=args.family,
        description=args.description,
        source_disk=f"projects/{current_project()}/zones/{i['zone']}"
                    f"/disks/{i['disks'][0]}",
        storage_locations=[current_region(i["zone"])],
        labels={LABEL_KEY: "1"},
    )
    print(f"creating image {args.name} from {args.source}'s disk "
          "(a few minutes)")
    wait(images_client().insert(project=current_project(),
                                image_resource=image))
    print(f"image {args.name} ready. use it with "
          f"`glab init NAME --image {args.name}`"
          + (f" or --image family/{args.family}" if args.family else ""))


def cmd_image_delete(args):
    try:
        image = images_client().get(project=current_project(), image=args.name)
    except NotFound:
        die(f"no image named {args.name!r}")
    if not has_lab_label(image):
        die(f"{args.name} does not carry the {LABEL_KEY}=1 label; not touching it")
    if not args.yes and input(f"delete image {args.name}? [y/N] ").strip().lower() != "y":
        print("cancelled")
        return
    wait(images_client().delete(project=current_project(), image=args.name))
    print(f"deleted image {args.name}")


# ---------------------------------------------------------------- parsing

def build_parser():
    p = argparse.ArgumentParser(prog="glab",
                                description="a small Compute Engine control tool")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("login", help="check or refresh credentials").set_defaults(fn=cmd_login)
    sub.add_parser("whoami", help="show the current identity").set_defaults(fn=cmd_whoami)

    sp = sub.add_parser("project", help="show or set the project")
    sp.add_argument("name", nargs="?")
    sp.set_defaults(fn=cmd_project)

    sp = sub.add_parser("zone", help="show or set the working zone")
    sp.add_argument("name", nargs="?")
    sp.set_defaults(fn=cmd_zone)

    sp = sub.add_parser("zones", help="zones, and which GPUs they sell")
    sp.add_argument("--region", help="only this region "
                    "(default: the working zone's region)")
    sp.add_argument("--all", action="store_true", help="every zone")
    sp.set_defaults(fn=cmd_zones)

    sp = sub.add_parser("types", help="machine types and prices in the zone")
    sp.add_argument("--gpu", action="store_true",
                    help="list every GPU machine type, not one per family")
    sp.add_argument("--spot", action="store_true", help="price them as spot")
    sp.add_argument("--no-cache", action="store_true",
                    help="re-fetch prices instead of using the cached copy")
    sp.set_defaults(fn=cmd_types)

    sp = sub.add_parser("quota", help="CPU and GPU limits, which gate launches")
    sp.add_argument("--region", help="one region")
    sp.add_argument("--all-regions", action="store_true")
    sp.add_argument("--all", action="store_true", help="include zero limits")
    sp.set_defaults(fn=cmd_quota)

    sp = sub.add_parser("init", help="create an instance")
    sp.add_argument("name")
    sp.add_argument("--type", default="e2-micro")
    sp.add_argument("--disk", type=int, default=20, help="boot disk in GB")
    sp.add_argument("--disk-type", default="pd-balanced",
                    choices=sorted(DISK_GB_MONTH))
    sp.add_argument("--gpu", metavar="MODEL[:N]",
                    help="attach GPUs, e.g. nvidia-tesla-t4:1")
    sp.add_argument("--image", help="source image: a full path, a name in this "
                    "project, or family/NAME; default is Debian 12")
    sp.add_argument("--spot", action="store_true", help="use spot pricing")
    sp.add_argument("--public-ip", action="store_true",
                    help="give it an external address, so it can reach the internet")
    sp.add_argument("--max-run", type=int, metavar="MINUTES",
                    help="have Compute Engine delete it this many minutes after "
                    "it starts")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("list", help="list lab instances")
    sp.add_argument("--all-zones", action="store_true")
    sp.add_argument("--region")
    sp.set_defaults(fn=cmd_list)

    for name, fn, helptext in [("start", cmd_start, "start a stopped instance"),
                               ("stop", cmd_stop, "stop a running instance"),
                               ("shell", cmd_shell, "open a shell over IAP")]:
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("name")
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("run", help="run a shell command on the instance")
    sp.add_argument("name")
    sp.add_argument("command", nargs=argparse.REMAINDER)
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("push", help="copy a file or directory to the instance")
    sp.add_argument("name")
    sp.add_argument("local")
    sp.add_argument("remote", nargs="?")
    sp.set_defaults(fn=cmd_push)

    sp = sub.add_parser("pull", help="copy a file or directory back")
    sp.add_argument("name")
    sp.add_argument("remote")
    sp.add_argument("local", nargs="?")
    sp.set_defaults(fn=cmd_pull)

    sub.add_parser("cost", help="rough cost of what is running").set_defaults(fn=cmd_cost)

    sp = sub.add_parser("destroy", help="delete an instance")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--yes", action="store_true",
                    help="skip the confirmation, for scripts")
    sp.set_defaults(fn=cmd_destroy)

    sub.add_parser("images", help="list lab images and their storage cost") \
        .set_defaults(fn=cmd_images)

    sp = sub.add_parser("image-create",
                        help="make an image from a stopped instance's disk")
    sp.add_argument("name")
    sp.add_argument("--from", dest="source", required=True, metavar="INSTANCE")
    sp.add_argument("--family", help="group images; family/NAME picks the newest")
    sp.add_argument("--description", default="")
    sp.set_defaults(fn=cmd_image_create)

    sp = sub.add_parser("image-delete", help="delete a lab image")
    sp.add_argument("name")
    sp.add_argument("--yes", action="store_true",
                    help="skip the confirmation, for scripts")
    sp.set_defaults(fn=cmd_image_delete)

    return p


def dispatch(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return
    args.fn(args)


class Console(cmdlib.Cmd):
    intro = ("glab console. type a command, `help` for the list, `exit` to leave.\n"
             "the same commands work from your shell: glab list, glab init box, ...")

    @property
    def prompt(self):
        return f"glab({read_config().get('zone', '?')})> "

    def default(self, line):
        if line.strip() in ("exit", "quit", "q"):
            return True
        try:
            dispatch(shlex.split(line))
        except SystemExit:
            pass
        except GoogleAPICallError as e:
            print(api_message(e))
        except Exception as e:
            print(f"error: {e}")

    def do_help(self, arg):
        build_parser().print_help()

    def do_EOF(self, arg):
        print()
        return True


def main():
    if len(sys.argv) == 1:
        Console().cmdloop()
        return
    try:
        dispatch(sys.argv[1:])
    except GoogleAPICallError as e:
        die(api_message(e))
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
