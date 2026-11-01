# 推荐技能与工具

[English](asset-selection.md) · [源数据基准教程](SOURCE_BENCHMARK.md) ·
[第三方协议与正式基准](ASSET_BENCHMARK.md)

推荐功能从你配置的资产库发现并排序技能和 MCP 工具。推荐不会安装软件包、执行工具，
也不会批准修改操作（RM34–RM44）。

| 我想要…… | 从这里开始 |
| --- | --- |
| 使用自己的技能或 MCP 工具 | [本地配置](#使用自己的资产库)、[导入格式](TESTING.md#independent-asset-catalog-discovery-192-rm34ac25) |
| 开发替换算法 | [完整候选集 v2 协议](ASSET_BENCHMARK.md#the-skilltool-v2-boundary) |
| 准备外部索引 | [索引就绪](#接入团队算法)、[完整生命周期](ASSET_BENCHMARK.md#index-readiness-and-setup-cost) |
| 比较算法和语言组合 | [源数据流程](SOURCE_BENCHMARK.md#run-identical-cases-and-compare)、[正式格式](ASSET_BENCHMARK.md#built-in-team-a-and-team-b-on-the-same-fixture) |
| 明确选择一个结果 | [基准与用户选择](#基准与用户选择) |
| 检查数据或恢复访问 | [隐私与恢复参考](asset-privacy-security.md) |

## 选择正确的接口

| | 托管选择 v1 | 独立推荐 v2 |
| --- | --- | --- |
| 配置 | `[selection]` | `[recommendations]` 与显式资产源 |
| 示例 | `scripts/example_selector.py` | `scripts/example_asset_selector.py` |
| 用途 | 为托管流程路由技能 | 提议技能与 MCP 工具 |
| 权限 | 网关规则仍然有效 | 不授予宿主执行或审批权限 |

v2 是独立协议，不能只改名称就替换 v1。已发布的 schema：
[请求](../src/relaymind/selection/schemas/selection_request.v2.json)、
[结果](../src/relaymind/selection/schemas/selection_result.v2.json)、
[资产记录](../src/relaymind/store/schemas/asset_record.v1.json)、
[准备请求](../src/relaymind/selection/schemas/asset_preparation_request.v1.json)、
[准备响应](../src/relaymind/selection/schemas/asset_preparation_response.v1.json)。

流程为：配置资产源 → 已提交的词法目录 → 完整候选集 v2 请求 → 网关验证 → 候选列表/
用户选择 → 单独完成资格验证的宿主使用。需要索引的外部算法在基准查询前显式准备；
普通查询不会自动探测或构建外部索引。

## 使用自己的资产库

在仓库根目录手动安装 Python 3.12+ 环境和项目依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Windows 使用 `.venv\Scripts\Activate.ps1` 激活环境。下面使用 POSIX shell 和显式私有
目录；Windows 请使用对应的本地路径。创建
`/var/tmp/relaymind-customer/ordinary-skills/greeting/SKILL.md`：

```markdown
---
name: Greeting Helper
description: Help write a short greeting in English or Chinese
metadata:
  version: "1.0"
  languages: [en, zh-CN]
  dependencies: []
---
Write a greeting in the requested language.
```

保存 `/var/tmp/relaymind-customer/relaymind.toml`：

```toml
[server]
host = "127.0.0.1"
port = 8087
[store]
path = "/var/tmp/relaymind-customer/relaymind.sqlite3"
[external_model]
base_url = "http://127.0.0.1:1/v1"
model = "unused-by-retrieval"
context_limit = 4096
[recommendations]
enabled = true
user_id = "customer"
workspace_id = "local-library"
primary = "lexical"
total_ms = 500
primary_ms = 150
[[assets.sources]]
source_id = "my-skills"
kind = "local_directory"
path = "ordinary-skills"
```

配置解析要求声明一个模型；此示例的检索不访问该端点，也不需要本地模型、下载或外部
推理。在仓库根目录运行
`relaymind serve --config /var/tmp/relaymind-customer/relaymind.toml`。
在另一个终端输入启动时显示的应用令牌，输入不会回显：

```bash
read -r -s -p 'Application token: ' RELAYMIND_TOKEN
printf '\n'
curl -sS http://127.0.0.1:8087/relay/v1/recommendations/readiness \
  -H "Authorization: Bearer $RELAYMIND_TOKEN"
curl -sS http://127.0.0.1:8087/relay/v1/recommendations \
  -H "Authorization: Bearer $RELAYMIND_TOKEN" -H 'Idempotency-Key: greeting-001' \
  -H 'Content-Type: application/json' \
  --data-binary '{"schema_version":1,"task_text":"Find a greeting skill","requested_kinds":["skill"],"selection_scope":"discovery"}'
```

等待 `state=ready, reason=usable_catalog`，HTTP 200 本身不代表目录可用。
查看 `displayed_candidates` 与 `result.attempts`；词法匹配需要用户选择。
同一幂等键返回历史 POST 收据，新任务使用新键。
[GET 轮询、事件和重启](TESTING.md#recommendation-json-lifecycle-194-rm36ac27)
使用响应中的推荐 ID；重启后使用新的应用令牌。

本地发现只读取根目录 SKILL.md 或直属子目录软件包，不执行指令和依赖。
名称、路径、UTF-8/front matter 及固定大小上限均会检查。
[显式元数据导入](TESTING.md#independent-asset-catalog-discovery-192-rm34ac25)
也支持自己的工具和其他资产库，保留原始字节哈希、来源及源拥有的删除记录。
元数据里的来源 URL 不会自动抓取。本地扫描默认每 30 秒进行；刷新失败保留带过时
标记的上一份有效数据。远程刷新只接受[已安装且完成资格验证的配置](TESTING.md#qualified-public-source-refresh-spec-059-rm34rm38rm43)。
发现记录的可用性 `unknown` 不等于已经安装或可执行。

## 接入团队算法

在独立终端启动所需示例，再按教程生成
[生产与准备配置](SOURCE_BENCHMARK.md#connect-an-external-selector-and-declare-readiness)：

```bash
python scripts/example_asset_selector.py --port 8933
python scripts/example_asset_selector.py --port 8934 --preparation-path /index
```

在 `[recommendations]` 中设置查询 URL 与响应中完全一致的名称/版本。
两个示例均为 `example_lexical/1`；算法行为改变时更新版本。
各团队收到同一份有序的完整有效候选集，不收到目标标签或完整私有技能正文。
示例验证接入协议，不证明跨语言语义效果。

网关词法目录就绪、HTTP 服务开始监听、团队索引就绪是三件不同的事。
单个准备 URL 接收 POST JSON `operation=prepare|status|cancel`，使用相同的 ID、
binding、profile。只有 prepare 启动工作；status/cancel 的 catalog/budget 为 null。
没有独立的 `/status` 或 `/cancel` 路由。binding 绑定目录摘要/版本、作用域、权限、
资产种类及算法身份；查询必须返回对应的索引版本。取消确认不等于索引就绪，
也不等于本地计算已经完成清理。

## 基准与用户选择

先完成[离线构建/获取](SOURCE_BENCHMARK.md#acquire-and-reproduce-the-data)和
[配置生成](SOURCE_BENCHMARK.md#connect-an-external-selector-and-declare-readiness)。
示例目录已存在时，在所有命令中换用一个新的私有输出根目录。
教程提供 builtin、无状态 HTTP 与索引 HTTP 的完整运行命令，路径如下：

```bash
python scripts/source_asset_benchmark.py run \
  --catalog /var/tmp/rm-g1-source-guide/catalog-v2/catalog.json \
  --cases evaluation/assets/matrix-development/cases.v2.json \
  --evidence-root /var/tmp/rm-g1-source-guide/catalog-v2 \
  --production /var/tmp/rm-g1-source-guide/catalog-v2/builtin-production.toml \
  --track service --output /var/tmp/rm-g1-source-guide/builtin-service.json
python scripts/source_asset_benchmark.py compare \
  --left /var/tmp/rm-g1-source-guide/builtin-service.json \
  --right /var/tmp/rm-g1-source-guide/cold-service.json \
  --output /var/tmp/rm-g1-source-guide/comparison.json
python scripts/source_asset_benchmark.py choose \
  --report /var/tmp/rm-g1-source-guide/builtin-service.json \
  --case capability-01-zh > /var/tmp/rm-g1-source-guide/choice.json
```

源数据 compare 只输出内部 **JSON**。单独的
[正式流程](ASSET_BENCHMARK.md#built-in-team-a-and-team-b-on-the-same-fixture)
使用锁定输入，并通过 `asset_selection_benchmark.py compare ... --output ... --markdown ...`
输出 JSON/Markdown。策略、轨道、用例和目录必须相同；服务回退成功不能算作主算法成功。
cold/reuse 不兼容；必要准备失败时保留全部 not_run 槽位并以非零状态退出。
第三方未提供的成本仍为未知。

`choose` 向 stderr 显示选项，输入数字选择、输入 `0` 拒绝，或按 Ctrl-C 中断。
重定向保存的是新的 stdout 收据，不修改运行报告，不向后端写入反馈，也不改变宿主状态。
收据包含 `action_executed=false`。`ranked/choice_required` 展示候选；confident 不执行；
no_match 是终止的弃权。主算法未就绪、失败或超时时，可在剩余预算内使用一次已配置的
词法回退，实际尝试分别记录。

[412 资产/128 用例开发集](SOURCE_BENCHMARK.md#recorded-development-results)包含技能和 MCP、
en→en、zh→zh、zh→en、en→zh、混合查询、相近替代项、干扰项、负例和歧义。
原始正文语言和描述语言分别记录，缺失的 MCP 语言组合明确列出。
不共享拉丁标识的中文内存泄漏请求仍未命中英文原始目标；这些诊断不证明翻译能力、
语义质量或独立人工标注达标。开始使用工具不要求客户提供外部数据集或人工评审组。
生产语义模型资格验证，以及已安装宿主的 M10 执行、审批、纠正演练仍是单独的工作；
参见[当前状态](ROADMAP.md#m10--multilingual-asset-recommendations-spec-065)。

## 整任务试验、适配回合与平台导出

规范 0.60/0.64/0.65 第 12.6.5 节的离线工具服务于 #198（早期已安装技能试验）、#202（适配回合）、
#204（宿主 MCP 使用行）、#205（成对整任务）和 #206（平台生命周期）。它们校验冻结输入、
确定性地规划执行、通过真实推荐生命周期驱动适配回合，并重新评分导出的报告。工具不运行宿主、
不授予审批，也不执行任何工具：宿主试验的执行器由调用方提供，只有宿主执行工具。
本节是摘要；各项检查的规范性完整说明以英文版 [ASSET_BENCHMARK.md](ASSET_BENCHMARK.md#whole-host-trials-adaptation-episodes-and-platform-exports-spec-060064065-section-1265) 为准。
所有汇总都从经过验证的行重新计算，存储的汇总与之不符时报告被拒绝。`plan-host` 先依据 `HostFreezeV1` 校验
`AssetHostTasksV1`，并按 `{seed,task_id,arm}` 的 SHA-256 排列各组；评分命令的输出文件总是记录 `status`、`gate`
和固定的安全 `reason`，`plan-host` 只写出计划，被拒绝时不写文件并把原因输出到标准错误。引用的配置为严格 JSON 或 TOML
（不超过 4 MiB）；尚不支持关联锁（related locks），遇到时阻塞。重置证据必须是规范化（canonical）JSON 原始字节，
非规范字节或重复键会被拒绝。
规范 0.65（#240）要求提供并验证第 12.6.5 节规定的固定版本原生校验证据及适用的 schema／提取证据；
在此之前，宿主、提供方、采样和预算配置文档保持未验证状态，不能据此通过正式验收。

| 命令 | 作用 |
| --- | --- |
| `python scripts/asset_whole_task.py plan-host --tasks T --freeze F --output P` | 按冻结的任务顺序写出三组（native、provider_only、recommendations）执行计划 |
| `python scripts/asset_whole_task.py grade-host --report R --inputs M [--resolver X] --output O` | 解析配套输入清单，校验并重新评分宿主报告 |
| `python scripts/asset_whole_task.py grade-platform --report R --inputs M [--resolver X] --output O` | 校验平台报告的十一个生命周期步骤 |
| `python scripts/asset_whole_task.py grade-adaptation --report R --inputs M [--resolver X] --output O` | 校验并重新评分适配报告；探测标签来自清单中经过验证的语料 |

退出码：`0` 通过或不适用于验收（夹具平台报告、未达资格的适配报告）；`1` 已知门槛失败；
`2` 报告或输入被拒绝（摘要、身份、顺序、绑定或算术矛盾）；`3` 阻塞（缺少必需的文档、配置、
语料文档、运行器/协议清单中文件的原文（由 `--resolver` 提供）或证据）。阻塞永远不会被报告为通过。评分结果还会列出 `unverified`：
宿主与平台报告为 `configuration_semantics` 和 `qualified_evidence_review`；适配报告为
`qualified_evidence_review`，若两个对照组未引用冻结的 14.5.2 适配配置，还包括 `adaptation_config_semantics`
（被引用时会与其余引用闭包一同解析和验证）。关闭组与开启组的有效配置除适配开关外必须完全一致
（含选择器类型、名称、版本与地址），语料原文必须全部可用。

**输入。** 清单必须包含唯一的 `environment_lock`、`runner_inventory`、`protocol_inventory`，
报告指向的根输入及其冻结文档，冻结文档列出的每个配置（`referenced_config:<摘要>`），
以及冻结语料的 `lock:`、`catalog:`、`cases:`、`labels:` 文档和证据。语料只在私有临时目录中
重建，并由现有发布校验器验证。宿主任务绑定到用例的车道、任务族和目录资产的确切字节；
适配回合绑定到已存在的用例，目录中存在的纠正目标必须字节一致，不在池中的目标被保留而不会被替换。

**重置证据（规范 0.64）。** 证据登记项 `reset:<回合 ID>` 保存封闭的 JSON 文档
`{schema_version:1,episode_id,baseline,after}`。缺少该文档的重置声明不会被计入通过，
在没有更早的结构矛盾或已知门槛失败时，评分结果为阻塞。

**适配回合驱动。** `relaymind.evaluation.asset_episode_driver` 在进程内对真实推荐服务运行每个冻结回合：
每个对照组使用一次性存储，关闭组运行禁用基线；开启组记录重置基线，通过产品反馈存储依次提交三次纠正，
运行成对探测，经真实生命周期重置反馈后重复重置探测，并导出报告和重置证据。每次调用使用独立的幂等声明；
重置原像只来自就绪结果，重置探测失败时重置结果为未知。纠正被拒或异常只结束当前回合：已完成的工作被保留，
其余条目以固定原因标为 `not_run`，后续回合继续运行。中断（调用方的中断检查或任务被取消）
在每次探测和纠正之前检查，并结束整个运行：当前回合记为 `cancelled`，之后计划的每个回合都以原因
`interrupted` 标为 `not_run`。评分器根据经验证的语料和
对照组配置重新计算每个探测的请求与候选目录摘要。驱动使用的评估专用操作登记不是个人或宿主证据，
因此不能产生 `qualified` 报告。

**当前限制。** 尚无经过评审的资格证据校验器，因此资格验收报告只会评为阻塞，不会通过；
实测平台报告在绑定工作负载评分器并在真实 Apple Silicon macOS 与原生 Windows 上评审前同样阻塞。
真实回合、成对宿主任务和平台运行需要真实独立语料、宿主与硬件；夹具运行不构成任何测量结果。
