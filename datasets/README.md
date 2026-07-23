# Datasets

The WorkBuddy Bench subsets are **not stored in this git repository**. They are
published as one `.tar.gz` archive per subset on HuggingFace and downloaded into
this directory on demand.

| Subset | Archive | Tasks | Approx. size |
|--------|---------|-------|--------------|
| Code | `wb-bench-code-v1.0.tar.gz` | 80 | ~196 MB |
| Web | `wb-bench-web-v1.0.tar.gz` | 70 | ~22 MB |
| Office | `wb-bench-office-v1.0.tar.gz` | 50 | ~10 MB |
| Security | `wb-bench-sec-v1.0.tar.gz` | 60 | ~479 MB |

HuggingFace repo: [`tencent/workbuddy-bench`](https://huggingface.co/datasets/tencent/workbuddy-bench)
(override with `WB_BENCH_HF_REPO`).

## Download

```bash
./scripts/dataset/fetch-dataset.sh code       # one subset
./scripts/dataset/fetch-dataset.sh code web   # several
./scripts/dataset/fetch-dataset.sh all        # all four
```

The script prefers `huggingface-cli` and falls back to `curl`/`wget`, verifies
each archive against the repo's `SHA256SUMS`, then extracts it here. Point it at
a different repo with `WB_BENCH_HF_REPO=org/name ./scripts/dataset/fetch-dataset.sh ...`.

## Layout after download

Each archive extracts to `datasets/wb-bench-<subset>-v1.0/`:

```
datasets/wb-bench-code-v1.0/
  dataset.toml
  tasks/<task-id>/
    instruction.md
    task.toml
    environment/{Dockerfile, docker-compose.yaml, scorer.py, workspace.tar.gz}
    tests/
```

Job YAMLs reference this path via `dataset: datasets/wb-bench-<subset>-v1.0/tasks`.

There is only **one** extraction on the host — the subset archive. Each task's
`environment/workspace.tar.gz` **stays packed**: the task Dockerfile copies it
into the image and extracts it into `/workspace` inside the container. Do not
unpack it on the host. The unpacked `environment/workspace/` dir is a task-author
artifact only — never shipped in an archive, and gitignored.
