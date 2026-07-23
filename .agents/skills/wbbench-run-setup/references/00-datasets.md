# Phase 0 — Datasets

Goal: the subset(s) the run needs are present on disk under `datasets/`.

Datasets are **not shipped in git**. Each subset lives as one `.tar.gz` on
HuggingFace and is downloaded on demand. A fresh checkout has an empty
`datasets/` (only `datasets/README.md`), so this must happen before Phase 4 (Job)
and Phase 5 (Launch) — a job's `dataset:` path must resolve to a real directory.

## Steps

1. Check what's already present: `ls -d datasets/wb-bench-*/ 2>/dev/null`.
   Nothing → nothing has been downloaded yet.
2. Ask the user which subset they're evaluating (code / web / office / sec), or
   `all`, then download it:
   ```bash
   ./scripts/dataset/fetch-dataset.sh code   # or web / office / sec / all
   ```
   The script downloads (via `huggingface-cli`, else `curl`/`wget`),
   checksum-verifies against the repo `SHA256SUMS`, and extracts to
   `datasets/wb-bench-<subset>-v1.0/`.
3. Confirm: `ls datasets/wb-bench-code-v1.0/tasks | wc -l` should match the
   subset's task count (code 80 / web 70 / office 50 / sec 60).

## Config

- `WB_BENCH_HF_REPO` — the HuggingFace repo id, defaults to
  `tencent/workbuddy-bench`. Override only to fetch from a mirror/fork:
  `WB_BENCH_HF_REPO=org/name ./scripts/dataset/fetch-dataset.sh code`.

## Common failure modes

- **checksum failed** → partial/corrupt download; re-run with `--force`.
- **already exists, skipped** → the subset is already installed; pass `--force`
  only if the user wants a clean re-download.

## Guardrail

Downloaded dataset/task files are read-only inputs. Only write under `configs/`
and `.env` during setup.
