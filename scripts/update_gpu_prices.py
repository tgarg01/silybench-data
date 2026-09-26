"""Refresh prices/gpu_hourly.yaml from provider price APIs (run by CI every 6 hours).

    uv run --with pyyaml --with httpx scripts/update_gpu_prices.py

Rows that have a live source are updated in place (price + checked_at); rows without one, or
whose API isn't configured, keep their hand-checked values. Experiment-day prices are never
touched: they're frozen in experiments/<id>/prices_at_run/.

Sources (CI secrets in brackets; a missing secret just skips that provider):
  gcp     Cloud Billing Catalog API, SKU prices summed per machine type  [GCP_BILLING_API_KEY]
  vast    public marketplace search: median of verified, rentable offers  (no key)
  runpod  GraphQL gpuTypes secure/community prices                       [RUNPOD_API_KEY]
  lambda  instance-types API                                             [LAMBDA_API_KEY]
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
PRICES = ROOT / "prices" / "gpu_hourly.yaml"
TODAY = datetime.now(UTC).date().isoformat()

# --- GCP: machine = GPU + vCPU + RAM SKUs (per hour) ----------------------------------------
GCP_SERVICE = "6F81-5844-456A"  # Compute Engine
GCP_MACHINES = {
    # product label in gpu_hourly.yaml -> (gpus, vcpus, ram GiB, SKU descriptions)
    "a3-highgpu-1g Spot, us-central1": (1, 26, 234, "spot", {
        "gpu": "Nvidia H100 80GB GPU attached to Spot Preemptible VMs running in Americas",
        "cpu": "Spot Preemptible A3 Instance Core running in Americas",
        "ram": "Spot Preemptible A3 Instance Ram running in Americas",
    }),
    "a3-highgpu-1g, us-central1": (1, 26, 234, "on-demand", {
        "gpu": "Nvidia H100 80GB GPU running in Americas",
        "cpu": "A3 Instance Core running in Americas",
        "ram": "A3 Instance Ram running in Americas",
    }),
}


def gcp_prices() -> dict[str, float]:
    key = os.environ.get("GCP_BILLING_API_KEY")
    token = os.environ.get("GCP_ACCESS_TOKEN")  # local runs: $(gcloud auth print-access-token)
    if not key and not token:
        return {}
    wanted = {d for *_, skus in GCP_MACHINES.values() for d in skus.values()}
    found: dict[str, float] = {}
    page = ""
    while True:
        params = {"pageSize": 5000, "currencyCode": "USD"}
        if page:
            params["pageToken"] = page
        headers = {}
        if key:
            params["key"] = key
        else:
            headers["Authorization"] = f"Bearer {token}"
        r = httpx.get(f"https://cloudbilling.googleapis.com/v1/services/{GCP_SERVICE}/skus",
                      params=params, headers=headers, timeout=60)
        r.raise_for_status()
        d = r.json()
        for s in d.get("skus", []):
            if s["description"] in wanted and "us-central1" in s.get("serviceRegions", []):
                unit = s["pricingInfo"][0]["pricingExpression"]["tieredRates"][-1]["unitPrice"]
                found[s["description"]] = int(unit["units"]) + unit["nanos"] / 1e9
        page = d.get("nextPageToken")
        if not page:
            break
    out = {}
    for product, (gpus, vcpus, ram, _, skus) in GCP_MACHINES.items():
        if all(v in found for v in skus.values()):
            machine = found[skus["gpu"]] * gpus + found[skus["cpu"]] * vcpus \
                + found[skus["ram"]] * ram
            out[product] = round(machine / gpus, 4)
    return out


# --- Vast: public marketplace ----------------------------------------------------------------
VAST_GPUS = {"H100-80GB": "H100 SXM", "H100-PCIe-80GB": "H100 PCIE", "H200-141GB": "H200",
             "A100-80GB": "A100 SXM4", "L40S-48GB": "L40S", "RTX4090-24GB": "RTX 4090"}


def vast_prices() -> dict[str, tuple[float, int]]:
    out = {}
    for gpu_type, name in VAST_GPUS.items():
        q = {"gpu_name": {"eq": name}, "num_gpus": {"eq": 1}, "rentable": {"eq": True},
             "verified": {"eq": True}, "type": "on-demand", "limit": 200}
        try:
            r = httpx.get("https://console.vast.ai/api/v0/bundles/",
                          params={"q": json.dumps(q)}, timeout=60)
            r.raise_for_status()
            prices = [o["dph_total"] for o in r.json().get("offers", [])]
        except (httpx.HTTPError, ValueError, KeyError):
            continue
        if len(prices) >= 3:  # a median of 1-2 listings says nothing
            out[gpu_type] = (round(statistics.median(prices), 4), len(prices))
    return out


# --- RunPod: GraphQL -------------------------------------------------------------------------
RUNPOD_IDS = {"H100-80GB": "NVIDIA H100 80GB HBM3", "H100-PCIe-80GB": "NVIDIA H100 PCIe",
              "H100-NVL-94GB": "NVIDIA H100 NVL", "H200-141GB": "NVIDIA H200",
              "A100-80GB": "NVIDIA A100-SXM4-80GB", "A100-PCIe-80GB": "NVIDIA A100 80GB PCIe",
              "L40S-48GB": "NVIDIA L40S", "RTX4090-24GB": "NVIDIA GeForce RTX 4090",
              "B200-180GB": "NVIDIA B200"}


def runpod_prices() -> dict[tuple[str, str], float]:
    key = os.environ.get("RUNPOD_API_KEY")
    if not key:
        return {}
    r = httpx.post("https://api.runpod.io/graphql", params={"api_key": key}, timeout=60,
                   json={"query": "query { gpuTypes { id securePrice communityPrice } }"})
    r.raise_for_status()
    by_id = {g["id"]: g for g in r.json()["data"]["gpuTypes"]}
    out = {}
    for gpu_type, rid in RUNPOD_IDS.items():
        g = by_id.get(rid)
        if not g:
            continue
        if g.get("communityPrice"):
            out[(gpu_type, "Community Cloud")] = float(g["communityPrice"])
        if g.get("securePrice"):
            out[(gpu_type, "Secure Cloud")] = float(g["securePrice"])
    return out


# --- Lambda ----------------------------------------------------------------------------------
LAMBDA_TYPES = {"gpu_1x_h100_sxm5": ("H100-80GB", "1x H100 SXM"),
                "gpu_8x_h100_sxm5": ("H100-80GB", "8x H100 SXM"),
                "gpu_1x_h100_pcie": ("H100-PCIe-80GB", "1x H100 PCIe"),
                "gpu_1x_gh200": ("GH200-96GB", "1x GH200"),
                "gpu_1x_a10": ("A10-24GB", "1x A10"),
                "gpu_1x_b200_sxm6": ("B200-180GB", "1x B200 SXM6")}


def lambda_prices() -> dict[tuple[str, str], float]:
    key = os.environ.get("LAMBDA_API_KEY")
    if not key:
        return {}
    r = httpx.get("https://cloud.lambdalabs.com/api/v1/instance-types", auth=(key, ""),
                  timeout=60)
    r.raise_for_status()
    out = {}
    for name, entry in r.json()["data"].items():
        if name in LAMBDA_TYPES:
            it = entry["instance_type"]
            gpus = int(name.split("_")[1].rstrip("x"))
            out[LAMBDA_TYPES[name]] = it["price_cents_per_hour"] / 100 / gpus
    return out


def main() -> int:
    data = yaml.safe_load(PRICES.read_text())
    offers = data["offers"]
    updated = 0

    def set_price(row: dict, price: float, note: str | None = None) -> None:
        nonlocal updated
        row["usd_per_gpu_hour"] = price
        row["checked_at"] = TODAY
        row["live"] = True
        if note:
            row["notes"] = note
        updated += 1

    sources = {
        "gcp": gcp_prices, "vast": vast_prices, "runpod": runpod_prices, "lambda": lambda_prices,
    }
    results = {}
    for name, fn in sources.items():
        try:
            results[name] = fn()
        except Exception as e:  # one provider's outage must not block the others
            print(f"{name}: failed ({e}); keeping previous prices", file=sys.stderr)
            results[name] = {}
        print(f"{name}: {len(results[name])} live prices")

    for row in offers:
        p, product = row["provider"], row.get("product") or ""
        if p == "gcp" and product in results["gcp"]:
            set_price(row, results["gcp"][product])
            row["source"] = ("https://cloud.google.com/billing/v1/how-tos/catalog-api "
                             "(Compute Engine SKUs: GPU + vCPU + RAM)")
        elif p == "vast" and row["gpu_type"] in results["vast"]:
            price, n = results["vast"][row["gpu_type"]]
            set_price(row, price, f"median of {n} verified rentable 1x offers; "
                                  "marketplace prices move daily")
        elif p == "runpod":
            tier = "Community Cloud" if "Community" in product else "Secure Cloud"
            if (row["gpu_type"], tier) in results["runpod"]:
                set_price(row, results["runpod"][(row["gpu_type"], tier)])
        elif p == "lambda" and (row["gpu_type"], product) in results["lambda"]:
            set_price(row, results["lambda"][(row["gpu_type"], product)])

    data["refreshed_at"] = datetime.now(UTC).isoformat(timespec="minutes")
    header = PRICES.read_text().split("offers:")[0].split("refreshed_at:")[0]
    body = yaml.safe_dump({"refreshed_at": data["refreshed_at"], "offers": offers},
                          sort_keys=False, width=200)
    PRICES.write_text(header.rstrip() + "\n" + body)
    print(f"updated {updated} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
