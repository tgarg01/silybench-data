# silybench-data

The open dataset behind the silybench site. It holds **measured** LLM serving performance on
rented GPUs, dated **GPU rental** and **API** price tables, and the **self-hosting vs API cost**
numbers derived from them. Every number on the site comes from this repo.

| Path | What | Written by |
|---|---|---|
| `experiments/<id>/` | One folder per experiment: `experiment.yaml` (exact provider, machine, zone, boot image, runtime, bench tag + commit, config, models, scenarios, run ids), `fingerprint.json` (reference hardware), `assets.json` (release files with sha256), `datasets.yaml`, `prices_at_run/` (GPU + API prices frozen on the experiment day) | maintainers |
| `runs/<run_id>/result.json` | One benchmark session (one model × precision × machine), schema in `derived/schema.json` | `gpubench submit` (a PR from the GPU box) |
| `runs/<run_id>/raw.tar.gz` | Raw evidence: per-repeat `vllm bench serve` JSON, 0.5 s GPU telemetry CSVs, vLLM log, lm-eval results and logs | same |
| **Releases** (`pipeline-2026-09-24`, `exp-<id>`) | Everything else, too big for git: complete run directories incl. per-question accuracy samples, VM logs, the exact code bundles that ran (private Terraform state removed; `MANIFEST.sha256` lists every original file), prompt datasets, and later Nsight reports | `gpubench publish-raw` |
| `prices/gpu_hourly.yaml` | GPU rental prices, USD per GPU-hour, with source and date. GCP (billing catalog) and Vast refresh automatically; RunPod/Lambda too once their API-key secrets are set | `scripts/update_gpu_prices.py` (CI, every 6 h) + hand-checked rows |
| `prices/api.yaml` | API list prices for the same models, per provider | `scripts/update_api_prices.py` (CI, every 6 h) |
| `prices/api_manual.yaml`, `prices/model_map.yaml` | API prices not on OpenRouter; HF id → OpenRouter slug | hand-maintained |
| `derived/` | **Generated, never edit.** Merged runs, cost model, CSV, SQLite, reproduction guide | CI: `gpubench dataset build` |
| `BENCH_REF` | Version of [silybench-bench](https://github.com/tgarg01/silybench-bench) used to build `derived/` | maintainers |

## Experiments

| id | status | what |
|---|---|---|
| `2026-10-qwen3.8-27b-h100` | planned | Qwen3.8-27B BF16 + FP8 on 1× H100 SXM (GCP a3-highgpu-1g Spot), 6 scenarios incl. 100k-token tool-calling, perf only |
| `2026-09-24-qwen3-8b-pipeline` | pipeline | Qwen3-8B runs used to build the pipeline, kept with all raw data; not featured |

Only `published` experiments feed the headline comparisons on the site.

## Use the data

```bash
# Everything as SQL:
sqlite3 derived/silybench.sqlite "select model, workload, hosted_provider, hosted_usd_per_1k_requests, api_provider, api_usd_per_1k_requests, savings_at_capacity from comparisons"
# Or pandas / DuckDB on derived/csv/*.csv, or JSON: derived/cost.json, derived/runs/*.json
```
Raw URLs work too:
`https://raw.githubusercontent.com/tgarg01/silybench-data/main/derived/cost.json`.

## Data dictionary

**Units**: latencies in ms; throughput in tokens/s or requests/s for the whole deployment; prices in USD.

### `runs` (one row per published deployment; repeated runs of one setup are merged)
| column | meaning |
|---|---|
| run_id | `<UTC start>_<gpu>x<count>_<model>_<precision>` |
| campaign | config in silybench-bench `configs/<campaign>.yaml` |
| provider, gpu_type, gpu_count, machine_type, provisioning | where it ran; `gpu_type` is normalised (e.g. `H100-80GB` = H100 SXM) |
| price_per_hour_usd | what the submitter paid for the whole machine |
| model, model_revision, precision, max_model_len | HF id, pinned commit SHA, bf16 / fp8 (dynamic FP8 quantization in vLLM) |
| tp, pp, dp, ep | tensor / pipeline / data / expert parallel sizes |
| engine_version, engine_image(_digest), runtime | vLLM version; the Docker image, or `native` = the same vLLM version from pip |
| kv_cache_tokens | KV-cache capacity vLLM reported at startup |
| git_commit, config_hash | silybench-bench commit and the hash of the exact config that ran |
| merged_from | other run_ids combined into this row (e.g. an accuracy-only re-run) |

### `perf_points` (one row per run × workload × concurrency; median of the repeats)
| column | meaning |
|---|---|
| workload, input_len, output_len | random prompts of exactly input_len tokens, generating exactly output_len (`--ignore-eos`) |
| concurrency | simultaneous users (closed loop: each user sends its next request when one finishes) |
| ttft_p95/p99_ms | time to first token |
| tpot_p95/p99_ms | time per output token after the first, per request |
| itl_median/p95/p99_ms | inter-token latency; the median is the headline "per-user speed" (1000/itl = tok/s per user) |
| e2el_p95/p99_ms | end-to-end request latency |
| request_throughput, output_throughput, total_token_throughput | req/s, output tok/s, (input+output) tok/s |
| avg_power_w, output_tokens_per_joule, peak_memory_gb | from nvidia-smi, sampled only during the measured window |
| usd_per_1m_output_tokens_at_run_price | at the submitter's price and this concurrency |
| slo_pass | p99 TTFT ≤ 2000 ms **and** median ITL ≤ 50 ms |

### `capacity`
| column | meaning |
|---|---|
| max_users_slo | most concurrent users meeting the SLO (bisection search). If nothing above it failed, it is a lower bound (`capacity_is_lower_bound` in cost.json) |
| max_users_kv_cache | requests of this size that fit in the KV cache at once |
| output_throughput_at_max_users | output tok/s at that load |

### `accuracy`
lm-evaluation-harness through the served model's chat API, temperature 0: `task`, `metric`, `value` (0-1), `stderr`, `num_samples`.

### `gpu_prices`, `api_prices`
The price tables above, flattened. `usd_per_gpu_hour` × GPU count = machine price. `min_gpus` > 1 = only sold in that size.

### `hosted_costs` (run × workload × every GPU offer for the same gpu_type)
`usd_per_hour`, `usd_per_month` (730 h), `usd_per_1m_output_tokens`, `usd_per_1m_total_tokens` and `usd_per_1k_requests`, **at SLO capacity**, i.e. an always-on GPU kept as busy as the latency target allows. `measured_price` = the submitter's own price (included when it isn't in the price table).

### `comparisons` (model × workload)
The cheapest self-hosted option (any precision, any provider) vs the cheapest API for the same model:
`hosted_usd_per_1k_requests`, `api_usd_per_1k_requests`, `api_median_usd_per_1k_requests`,
`breakeven_requests_per_day` (volume at which an always-on GPU costs the same as the API),
`breakeven_utilization` (that volume as a fraction of the GPU's SLO capacity), and
`savings_at_capacity` (1 − hosted/API per request when the GPU runs at capacity).

## Caveats (read before quoting numbers)
- Self-hosting costs assume steady load. Real traffic has peaks and idle time; the break-even
  utilization tells you how busy you must keep the GPU. The site's calculator sizes for peak users.
- API providers may serve quantized weights (see `quantization`). FP8 self-hosted accuracy is
  measured and published next to BF16.
- GPU prices are list prices on `checked_at`; marketplace (Vast) and spot prices move daily.
- Engineering and ops time isn't in the hourly price. That's the managed-hosting service the site offers.

## Reproduce any number
`derived/REPRODUCE.md` lists, for every run, the exact bench commit, vLLM image digest, model
revision, flags and the command to re-run it. To rebuild `derived/` locally:
```bash
uvx --from "git+https://github.com/tgarg01/silybench-bench@$(cat BENCH_REF)" gpubench dataset build
```

## Contribute a run
Rent a GPU, tell Claude *"Clone https://github.com/tgarg01/silybench-bench and follow AGENTS.md"*,
and it opens the PR here. See CONTRIBUTING.md for review rules.

License: data CC BY 4.0.
