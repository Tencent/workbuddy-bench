<p align="center">
  <img src="docs/assets/wb-bench-banner-dark.png#gh-light-mode-only" style="height: 5em" alt="WorkBuddy Bench" />
  <img src="docs/assets/wb-bench-banner.png#gh-dark-mode-only" style="height: 5em" alt="WorkBuddy Bench" />
</p>

<p align="center">
  <a href="http://workbuddybench.com/"><img alt="Website" src="https://img.shields.io/badge/Website-workbuddybench.com-28B894"></a>
  <a href="https://huggingface.co/datasets/tencent/workbuddy-bench"><img alt="Dataset" src="https://img.shields.io/badge/%F0%9F%A4%97%20Dataset-HuggingFace-FFD21E"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Tencent-blue"></a>
  <img alt="Tracks" src="https://img.shields.io/badge/tracks-Code%20%C2%B7%20Web%20%C2%B7%20Office%20%C2%B7%20Security-28B894">
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <strong>简体中文</strong>
</p>

---

## 总览

WorkBuddy Bench 是一套面向编程 Agent 的评测基准，题目取自真实的开发、PM、算法、QA、运维与
安全工作，再逆向改写成角色化的任务。拿到一道题和一个沙箱工作区，Agent 要交出正确的产物
（一个 patch、一份文件、一份报告），最终由一套测试判分。

基准包含四个子集：

| 子集 | 题量 | 领域 | Agent 做什么 |
|------|------|------|--------------|
| **Code** | 80 | 仓库级软件工程（Python） | 放进某个基线 commit 的真实项目，跨模块定位改代码，通过测试。18 类任务，横跨开发、PM、算法、QA、运维等角色。 |
| **Web** | 70 | 前端 / GUI（HTML/CSS/JS） | 前端工作的生成、修改、分析与质量保障：页面交互、数据可视化、视觉设计、前端项目分析、代码测试、页面实现、文档转换。 |
| **Office** | 50 | 办公数据 / 文件工作流 | 在混合格式文件（xlsx / csv / pdf / docx）上操作，产出完全正确的目标产物。难度来自结构、关系、状态与证据链，而非行数。 |
| **Security** | 60 | 安全 / 漏洞（红蓝对抗） | 挖掘并安全复现真实漏洞、分析恶意软件、执行安全运营、评估 Agent 攻击面，覆盖 6 个 domain。 |

本仓库是这套基准的**评测框架**。它把一个 agent CLI（即 *harness*）放进本地 Docker sandbox，
跑一批任务，记录下过程中的一切（patch、轨迹、测试结果、效率），再为整轮运行打分。框架构建在
[Harbor](https://github.com/harbor-framework/harbor) 之上。

## 快速开始

跑通 WorkBuddy Bench 最省事的方式，是让 Agent 帮你把整个流程走完。本仓库自带一个
**`wbbench-run-setup` skill**（位于 [`.agents/skills/`](.agents/skills/)，`.claude/skills/`
也软链到此），它会带你端到端跑通——从一个未配置的 checkout，一路带到跑起来、出结果：
数据集、环境、model 配置、`.env` 凭据、job 文件、启动运行，最后到分析报告。
用支持 skill 的 Agent（Claude Code、CodeBuddy Code 等）打开本仓库，调用该 skill（例如
`/wbbench-run-setup`），或直接让它*帮你配置一次 WorkBuddy Bench 运行*，它会一步一步引导你。

想自己手动配置，或者想看看 skill 背后到底做了什么？下面的**安装**与**用法**两节，把同样的
步骤手动走一遍。

## 安装

每道题都在 Docker 里运行以保证可复现。需要 Python ≥ 3.12、[uv](https://docs.astral.sh/uv/)
与 Docker。

```bash
uv sync            # 装依赖
cp .env.example .env
```

任务数据集**不在本仓库**，而是托管在 HuggingFace 的
[`tencent/workbuddy-bench`](https://huggingface.co/datasets/tencent/workbuddy-bench),
每个子集一个归档。把需要的子集下载到 `datasets/`：

```bash
./scripts/dataset/fetch-dataset.sh code   # 或：web / office / sec / all
```

脚本会下载、按校验和验证并解压到 `datasets/wb-bench-<subset>-v1.0/`。归档清单、
体积与 `WB_BENCH_HF_REPO` 覆盖方式见 [`datasets/README.md`](datasets/README.md)。

指定要评测的 model。从模板复制一份，填好即可：

```bash
cp configs/models/_template.model.yaml configs/models/<provider>/<slug>.yaml
```

```yaml
model:
  name: Hy3                         # 后端认识的模型 id
  protocols: [openai]               # openai 或 anthropic
  backend_url_env: MODEL_BASE_URL   # 存 base URL 的 .env 变量名
  backend_key_env: MODEL_API_KEY    # 存 API key 的 .env 变量名
```

`backend_url_env`、`backend_key_env` 填的是**变量名**；真正的 URL 和 key 写进 `.env`，密钥
就不会进版本库。

## 用法

一次评测由一个 **job** 定义：把 model、harness、dataset 三者凑到一起。从模板复制一份填好：

```bash
cp configs/jobs/_template.job.yaml configs/jobs/<slug>.yaml
```

```yaml
model: <provider>/<slug>                  # configs/models/ 下的某个 model
harness: codebuddy-code/<version>         # 要评测的 agent CLI
dataset: datasets/wb-bench-code-v1.0/tasks
```

然后跑起来：

```bash
uv run ./scripts/run.sh --help                  # 列出可用的 job
uv run ./scripts/run.sh --job <slug> --dry-run  # 预览这次会跑什么，然后退出
uv run ./scripts/run.sh --job <slug>            # 正式跑
```

结果落在 `results/` 下。job 与 model 的完整字段说明见
[`configs/README.md`](configs/README.md)。

## 引用与许可

采用 Tencent 许可，见 [`LICENSE`](LICENSE)。

如果 WorkBuddy Bench 对你有帮助，欢迎引用：

```bibtex
@misc{workbuddybench2026,
    title  = {WorkBuddy Bench: Evaluating Coding Agents on Real Role-Played Work},
    author = {Tencent Youtu Lab and Keen Security Lab and WorkBuddy and Yunding Security Lab},
    year   = {2026},
    note   = {https://github.com/Tencent/workbuddy-bench}
}
```
