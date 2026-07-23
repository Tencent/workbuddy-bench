# 配置体系

[English](README.md) | **简体中文**

一次评测 run 的全部输入都在这里。四个子目录各是一层，运行时先 **deep-merge**，
再解析成一份 **resolved manifest JSON**，最后落成传给 Harbor 的 **runtime config
YAML**（后两者区别见下方「产物」）：

```
configs/bench/<dataset_id>.yaml       ← 运行不变量 + 上下文窗口（按数据集）
configs/harnesses/<family>/…          ← 被测 agent CLI 的身份、参数、env
configs/models/<provider>/<slug>.yaml ← 模型身份、后端 URL/KEY env 名、采样参数
configs/jobs/<slug>.yaml              ← 上面三者的组合 + 单次 override
```

合并顺序（后者覆盖前者）：

```
bench/_default.yaml
  → bench/<dataset_id>.yaml
    → harnesses/<family>/<version>.yaml
      → models/<slug>.yaml
        → jobs/<slug>.yaml
```

合并由 `workbuddy_bench.runner.prepare_job` 完成，落成的 runtime config YAML 写到
`.workspace/data/generated/jobs/`（本地、gitignore 的工作目录）。凭据永远不进这些
YAML —— 文件只写 **env 变量名**，真实密钥留在 `.env`。

### 产物：manifest JSON vs runtime config YAML

一次 run 有两个易混淆的产物，路径和用途不同：

- **Resolved manifest JSON** ——
  `scripts/logs/instances/<instance-id>/manifest.json`。
  由 `resolve_manifest` 生成，是单次 run 的完整解析结果（模型/harness/连接/上下文
  窗口/`harness_runtime_config` 审计块等），供 `validate_model`、`proxy_config`、
  `prepare_job`、`sharded_eval` 等下游读取。
- **Harbor runtime config YAML** ——
  `.workspace/data/generated/jobs/<job>.yaml`。
  由 `prepare_job` 生成并传给 `harbor run -c`，是真正喂给 Harbor 的运行时配置。
  分片与非分片路径都写到此目录。

执行链路：

```
job/model/harness/bench YAML
  → resolve_manifest
  → scripts/logs/instances/<instance-id>/manifest.json   (resolved manifest JSON)
  → prepare_job
  → .workspace/data/generated/jobs/<job>.yaml            (Harbor runtime config YAML)
  → harbor run
```

---

## bench/ —— 按数据集的运行不变量

`load_bench` 先读 `_default.yaml`（bench-wide 兜底），再把 `<dataset_id>.yaml`
叠上去。`<dataset_id>` 是数据集目录名（也是 `dataset.toml` 的 `[dataset].id`），
由 job 的 `dataset:` 路径推导。

主要字段：

| 字段 | 含义 |
|---|---|
| `n_attempts` | 每个 task 重复跑几次（估方差） |
| `n_concurrent_trials` | 并发 trial 数（top-level） |
| `timeout_multiplier` | 所有 task 声明超时的全局倍数 |
| `jobs_dir` | 结果输出根（external = `results`） |
| `environment` | 容器环境：`type: docker` + 自定义 `WorkBuddyDockerEnvironment` |
| `agent_user` / `verifier_user` | 容器 exec 用户（默认 `dev`；置空 = root） |
| `context_window` / `context_compact_pct` | 上下文窗口 + 压缩触发比例 |
| `llm_judge` | 可选的 Layer-3 LLM judge（默认关） |

评分不在这里配 —— 每个数据集的 `dataset.toml` / `task.toml` 用
`[verifier].import_path` 指向 `workbuddy_bench.judge:CompositeVerifier`，
见 [判分](#判分compositeverifier)。

## harnesses/ —— 被测 agent CLI

harness = 被评测的 agent CLI（`codebuddy-code`、`claude-code`）。每个 family：

```
harnesses/<family>/
  _defaults.yaml           # 共享身份：name / import_path / protocol / params / mount 骨架
  versions/<version>.yaml  # 每版：CLI 版本号 + settings_file + env（loader 据此推导 mount 镜像 tag）
  presets/…                # 确定性配置预设（settings.json / models.json 的静态字段）
  docker/Dockerfile        # split-mount 镜像构建模板
  CONFIG.md                # 该 family 的机制说明
```

CLI **不烤进 task 镜像**：每个版本构建一个只读 split-mount 镜像，运行时挂进容器，
由 agent 的 `install()` 软链上 PATH。一个 task 镜像因此可测任意 harness / 版本。
机制与新增 harness 见 [`harnesses/HARNESS_AUTHORING.md`](harnesses/HARNESS_AUTHORING.md)；
各 family 的具体行为见对应的 `CONFIG.md`。

## models/ —— 模型身份 + 后端

```
models/<provider>/<slug>.yaml   # 一个模型一份
models/_template.model.yaml     # 空模板（runner 跳过 _template.*）
```

一份 model YAML 描述：`model.name`（发给后端的 id）、`protocols`（`openai` /
`anthropic`，首个为主）、`backend_url_env` / `backend_key_env`（读哪个 env）、
`params`（采样参数）。harness 不能原生转发的参数（`top_k` / `min_p` /
`chat_template_kwargs` 等）放进 `params.extra_body` —— **带 `extra_body` 的模型
必须用 `model_connection: local_proxy`**，direct 会 fail-fast。

Web v1.0 的 rollout 与 in-container verifier judge 都走同一套 model slug
配置。正式 `local_proxy` 运行时，`prepare_job` 会把 verifier 的统一
`WORKBUDDY_VERIFIER_LLM_*` 环境变量指向 job-private proxy：`BASE_URL=<proxy>/v1`，
`API_KEY=<judge-slug>`，`MODEL=<judge-slug>`。`llm_judge.enabled: false` 时不注入
这组三件套；真实后端 URL/KEY 只由 host proxy 通过 model YAML 的
`backend_url_env` / `backend_key_env` 读取。

## jobs/ —— 组合 + 入口

一个 job 是 model + harness + dataset 的纯组合，加可选 override。字段全集见
[`jobs/_reference.yaml`](jobs/_reference.yaml)，空模板见 `jobs/_template.job.yaml`。
最小必填字段：

```yaml
model: <provider>/<slug>            # configs/models/ 下的 slug
harness: codebuddy-code/<version>   # <family>/<version>
dataset: datasets/<dataset-id>/tasks
harness_backend: local              # harness/沙箱在哪里运行
model_connection: local_proxy       # direct | local_proxy
```

`dataset:` 路径必须在磁盘上真实存在。数据集不随 git 分发——先用
`./scripts/dataset/fetch-dataset.sh <subset>` 下载对应子集（见
[`datasets/README.md`](../datasets/README.md)）。

`configs/jobs/` 默认 gitignore，只有少数示例随仓库走（见 `.gitignore` 白名单）。

---

## 判分（CompositeVerifier）

评分由 `src/workbuddy_bench/judge/` 的 `CompositeVerifier`（一个 Harbor `BaseVerifier`
子类）驱动。运行配置只需要把 `import_path` 指向它；具体数据集逻辑由
`dataset.toml` 的 `[verifier]` contract 和数据集本地 `shared/verifier/plugin.py`
注册。`verifier.kwargs.profile` 已不再支持。

插件负责构造 `EvaluationContext` / `EvaluationPlan`，再交给
`CompositeVerifierEngine` 执行 evidence collector、rule / LLM / agent judge runner
和 scoring policy。Web v1.0 这类需要完整自定义流程的数据集可以通过
`custom_verify` 接管整个验证过程。

- `reward.json` —— numeric-only，Harbor 的 pass/fail gate 和 host metrics 读取它。
- `score.json` —— 富诊断，包含 judge/stage/evidence/plan 等调试信息。

### 最新数据集入口

| 数据集 | 判分入口 |
|---|---|
| `wb-bench-web-v1.0` | 数据集本地 fixed-judge Web verifier，执行 rule / LLM / VLM / agent judge 并导出 penalty score。 |
| `wb-bench-code-v1.0` | 读取 task-local `tests/verifier.toml`，执行任务自带的 pytest 或 native script verifier。 |
| `wb-bench-office-v1.0` | 运行 Office rule verifier，并在需要时合并 host-side LLM judge 结果。 |
| `wb-bench-sec-v1.0` | task-native 判分：每个任务自带 `tests/scoring.py`（或 `test_outputs.py`），直接写出 numeric `reward.json`（PoC 验证 / YARA 匹配 / ground-truth 比对）；不使用 workbuddy LLM judge 或 diff_capture。 |

```toml
[verifier]
import_path = "workbuddy_bench.judge:CompositeVerifier"
timeout_sec = 600.0
```

旧 `verifier.kwargs.profile` / `sources` / `judges` 和 `dataset.toml` 的
`[judging]` fallback 已不再支持。

## Harbor 参数

Harbor 无独立文档站，参数说明就在各 Pydantic model 的 `description=` 里：

```bash
python -c "from harbor.models.task.config import TaskConfig; print(TaskConfig.model_json_schema())"
```

源码在 `.venv/.../harbor/models/{task,job,trial}/config.py`。上表列的是 external
实际会用到的子集；bench 用四层配置组合后，由 `prepare_job` 落成 Harbor 运行时 YAML。
