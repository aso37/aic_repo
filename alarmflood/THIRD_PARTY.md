# 第三方数据与依赖清单（THIRD_PARTY.md）

> 比赛规则：引用他人技术、算法或数据需在参赛材料中明确标注。本文件从第一天维护，最后导出为附录表格。

## 一、第三方依赖清单（逐条核实，2026-10-17）

> **核实方法（可复现）**：许可信息取自**已安装包的分发元数据**
> （`importlib.metadata` 的 `License` / `License-Expression` / `Classifier` 字段）；
> 本机未安装的包取自 **PyPI 官方 JSON API**（`https://pypi.org/pypi/<包>/<版本>/json`）。
> **不是凭记忆抄的**——每条都标了核实来源。

### 1.1 主链路运行时依赖（`alarmflood/requirements.txt`）

| 包 | requirements 锁定 | 本机实测 | 用途 / 用在哪 | 许可（核实来源） | 上游 |
|---|---|---|---|---|---|
| pandas | 2.3.3 | 3.0.6 | 表格与时序处理；全链路 | **BSD-3-Clause**（元数据 `License` 字段） | https://pandas.pydata.org |
| numpy | 2.2.6 | 2.5.4 | 数值计算；`m3_fusion.py` | **BSD-3-Clause**（`License-Expression: BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0`） | https://numpy.org |
| pyarrow | 25.0.1 | 25.0.1 | parquet 读写引擎（pandas 的底层后端，**间接必需**） | **Apache-2.0**（`License-Expression`） | https://arrow.apache.org |
| drain3 | 0.9.11 | 0.9.11 | 日志模板挖掘（M1 告警归并） | **MIT**（Classifier: MIT License） | https://github.com/IBM/Drain3 |
| matplotlib | 3.10.9 | 3.11.2 | 看板图表（瀑布图/时间轴） | **PSF-based**（Classifier: Python Software Foundation License） | https://matplotlib.org |
| streamlit | 1.65.0 | 1.65.0 | 演示看板（M5） | **Apache-2.0**（`License-Expression`） | https://streamlit.io |
| openai | 3.29.0 | 3.29.0 | LLM API 客户端（M3 路线 B / M4） | **Apache-2.0**（`License-Expression`） | https://github.com/openai/openai-python |

> **为什么有两列版本**：`requirements.txt` 是发给别人的**锁定版本**（以团队 conda 环境实测为基）；
> 本机开发环境因后续装包，pandas / numpy / matplotlib 的次版本比锁定值新。
> **两者都实测可运行**——`TEST_REPORT.md` 的 14 条用例全部在本机
> `3.0.6 / 2.5.4 / 3.11.2` 这套版本上通过。

### 1.2 自建试点栈依赖（`alarmflood/pilot/app/requirements.txt`，仅 L2 试点）

| 包 | 版本 | 用途 | 许可（核实来源） | 上游 |
|---|---|---|---|---|
| flask | 3.0.3 | 试点故障注入服务的 HTTP 接口 | **BSD-3-Clause**（PyPI Classifier: BSD License） | https://github.com/pallets/flask |
| psutil | 5.9.8 | 采集进程 CPU/内存以制造真实故障 | **BSD-3-Clause**（PyPI `license` 字段明写 `BSD-3-Clause`） | https://github.com/giampaolo/psutil |
| prometheus_client | 0.20.0 | 暴露 `/metrics` 供 Prometheus 抓取 | **Apache-2.0**（PyPI `license` 字段） | https://github.com/prometheus/client_python |
| requests | 2.32.3 | 调用链内 HTTP 调用 | **Apache-2.0**（PyPI `license` 字段） | https://github.com/psf/requests |

> 这 4 个包**只装在 Docker 试点的 mock 服务容器里**，不进入主看板与算法链路的运行时。

### 1.3 已从清单移除（早期列入，但代码实际未使用）

| 包 | 早期是否列入 | 核实结论 |
|---|---|---|
| **scikit-learn** | 曾列 `1.9.1` | **全仓库无任何 `import sklearn`**。`requirements.txt` 注释也写明了"未 import，故不纳入"（D6 融合用简单网格搜索）。→ **移除** |
| **python-dotenv** | 曾列 `1.2.4` | **全仓库无任何 `import dotenv`**。`m3_llm.py` 自己实现了 10 行极简 `load_dotenv()`，注释明确写"避免额外依赖 python-dotenv"。→ **移除** |
| **Loghub (HDFS)** | 曾列为 D6 消融数据，许可写"待核" | **该消融已取消**（改由自建 Prometheus 试点栈提供"不挑数据源"的证据，见第二节）。→ **移除**（不再有许可待核项） |

> 这三条是本轮核对的主要产出：**清单里曾包含 2 个从未被 import 的包和 1 个已取消的数据源**，
> 会让评审误判技术栈，也与 `requirements.txt` 自相矛盾。

### 1.4 ⚠️ 安全问题：试点容器有 2 个已知漏洞（建议升级）

核许可时顺带查了 PyPI 的漏洞公告，发现试点容器 pin 的两个版本存在已知 CVE：

| 包 | 当前 pin | 漏洞 | 触发条件 | 修复版本 | 建议 |
|---|---|---|---|---|---|
| flask | 3.0.3 | **CVE-2026-27205** | 部署在会缓存的代理之后 **且** 访问了 `session`，可能泄漏会话信息 | **3.1.3** | 升到 `flask>=3.1.3` |
| requests | 2.32.3 | **CVE-2024-47081** | 使用 `.netrc` **且** 访问特制恶意 URL，可能泄漏凭据 | **2.32.4** | 升到 `requests>=2.32.4` |

**影响评估（如实说明）**：

- 这两个包**只在 L2 试点的 mock 服务容器里**。该服务**不是对外产品**，
  只是为演示"真实监控栈会产生告警"而临时起的故障注入服务。
- **flask**：漏洞需要"缓存代理 + 使用 session"。我们的 mock 服务**既没有 session、也不在缓存代理后**，
  **实际不可利用**。
- **requests**：漏洞需要"使用 .netrc + 特制 URL"。mock 服务只调内部地址、**不读 .netrc**，
  **实际不可利用**。
- **主链路完全不受影响**：本机 `requests` 实际为 **2.34.2**（≥ 2.32.4，已不在影响范围），
  且 **flask 根本不在主链路依赖里**。
- **但仍建议升级**——"已知漏洞未修"在合规评审里是扣分项，而升级成本只是改两个版本号。

### 1.5 数据与外部服务（非 pip 依赖）

| 名称 | 版本 | 用途 | 许可 | 来源 |
|---|---|---|---|---|
| AIOps Challenge 2021 Dataset（银行场景，`aiops2021-2`） | 2021 | 主评测数据：metric / logs / trace + **159 个故障标注** | **CC BY-NC 4.0（署名—非商业性使用）** | 数据集自带 `README.md`，原文见第二节 |
| 智谱 BigModel **`glm-4-plus`**（经 OpenAI 兼容接口调用） | `glm-4-plus` | 根因推理（M3 路线 B）、处置单生成（M4） | **厂商服务条款**；本作品**仅通过公开 API 调用，未分发、未嵌入模型权重** | https://open.bigmodel.cn |

> 完整版本锁定见 **`alarmflood/requirements.txt`**（仓库内唯一清单；仓库根目录没有 `requirements.txt`）。

## 二、⚠️ 许可要点（必须写进材料）

**主数据集许可是 `CC BY-NC 4.0`，其中 `NC` = NonCommercial（非商业性使用）。**

来源：数据集自带 `README.md`。该文件只存在于下载包外层、无版本控制，
故**全文转录留档**于此，供附录逐项核对。

| 项 | 内容 |
|---|---|
| 数据集 | AIOps Challenge 2021 Dataset（2021 银行场景，`aiops2021-2`） |
| 著作权人 | **Dan Pei** |
| 年份 | 2021 |
| **许可** | **Creative Commons Attribution-NonCommercial 4.0 International** |
| 许可全文 | https://creativecommons.org/licenses/by-nc/4.0/ |
| 包含内容 | 日志（logs）、指标（metric）、调用链（trace）三部分 |

<details>
<summary>数据集自带 README.md 原文（点击展开）</summary>

```
# AIOps2021
数据来源于AIOps2021挑战赛，包含日志、指标、调用链三个部分。

# Licence
AIOps Challenge 2021 Dataset © 2021 by Dan Pei is licensed under Creative Commons Attribution-NonCommercial 4.0 International. To view a copy of this license, visit https://creativecommons.org/licenses/by-nc/4.0/
```

</details>

> **另一件必须单独说明的事**：该数据集**不含任何告警文件**（只有 metric / logs / trace）。
> 本项目的告警流是用阈值规则从 metric 生成的，**属于我们的构造、不是数据集原始内容**。
> 因此阈值的依据单独成文，见 **`ALARM_RULES_PRIOR.md`**（含 10 条规则表、先验性证据与反调参证据）。

**含义与影响**：

1. **本竞赛使用没问题**——AIC 是公益性学科竞赛，属非商业用途；且材料中已按 CC BY 要求署名标注来源
2. **但商业化路径受约束**：本作品（AlarmFlood Copilot）如进入商业转化，**不能继续使用该数据集或其衍生告警流**。
   答辩/商业计划书中的"商业转化潜力"必须说明这一点：产品算法与架构可迁移，**数据需替换为客户自有告警**
3. **接入新数据源的证据来自自建试点栈**：另建一套 Prometheus + Alertmanager 监控环境采集真实告警
   （27 条，故障为人工注入、真值已知），**仅新增一个 loader 即完成接入，下游 M1→M4 零改动**
   （详见 `results/pilot/pilot_report.md`）。
   原计划的 **Loghub 日志消融已取消**——上述真实告警接入是更强的证据
   （真实监控栈 ≠ 离线日志包），且同样证明"方法不挑数据源"。答辩若被问到，讲这个取舍即可。
4. 附录表格中应保留完整许可名称与出处链接

## 三、数据处理说明（答辩可引用）

- **数据集本身无告警文件**；告警流由 `loaders/loader_aiops2021.py` 的 `RULES` 表对 metric 指标
  生成（模拟监控系统的阈值告警行为）。
- **当前实际状态（如实披露）**：`RULES` 表共 **10 条**阈值规则，**实测 7 条触发**，
  生成 **14,806** 条告警（原始 101,478 条，重复抑制率 **85.4%**）。
  3 条未触发，原因已逐一查清，**且病因各不相同**（详见 `results/rule_audit.md` 第 3 节）：

  | 未触发的规则 | 10 天全量实测 | 病因 |
  |---|---|---|
  | `SWAP_SWPTotSwapUsedPercent` | 恒为 **0.0000**（78,563 点） | 该数据集未启用 Swap，**信号不存在** |
  | `NETWORK_ens160_NETBandwidthUtil` | 全期 max **0.3685**（即 **0.37%**，40,150 点） | 带宽利用率近乎恒为 0，**信号近乎不存在** |
  | `FILESYSTEM_-_FSInodeUsedPercent` | 全期 max **29.93**（区间 16.37~29.93） | 量纲正常，但**阈值 90 定得过高** |

  > 核查同时确认 **KPI 名称 10/10 全部正确**——「名字写错」这一假设被排除。

- **更优的规则校准方案：已量化评估，但本轮未启用**（**这一点必须如实说明**）。
  评估结论：把 `ens160_NETBandwidthUtil` 换成 `NETWORK_TCP-FIN-WAIT`、并删掉两条无信号规则后，
  规则变为 8 条且全部生效，根因实体覆盖率由 **80% 提升至 92%**（网络型 Top-1 由 0/20 到 16/46）。
  **但该方案会改变告警流，使已完成的 D3/D4 对比实验结果失效、前后不可横向比较**，
  因此**本轮保持原 10 条规则不变**，仅将评估过程与量化收益留存为记录
  （`results/rule_audit.md`；对应分支 `fix/alert-rule-calibration`，未合并）。
  答辩口径：**「我们发现了问题、量化了收益，但为保持实验可比性选择本轮不动」**。
- **⚠️ 阈值判据（重要，答辩可讲）**：阈值该不该改，判据**不是"够不够得到"**，而是
  "**改完后对目标故障类型的判别力是否提升、噪声代价是否可接受**"。
  因为"够不到就调低"可能只是把正常状态变成误报，而本项目主题恰恰是**治理告警洪水**，那样做自相矛盾。
- **⚠️ 方法论**：**任何"阈值不可达"的结论必须在全部 10 天上验证，单日样本不能代表。**
  实测教训：`sdb_DSKPercentBusy` 在 0304 的 max 只有 36，看似不可达，
  但 0305/0306/0310/0325 分别是 100.0/100.0/100.4/100.0，实际可达（10 天触发 13 次）。
  复核脚本 `scripts/probe_rule_coverage.py` 已改为默认扫描 10 天并打印逐日 max 对照
- **网络型故障的关键指标**：`NETWORK_TCP-FIN-WAIT`（TCP 半关闭连接数）——
  非零样本占比仅 1.18%，但在 71% 的网络故障窗内出现非零，对其他故障类型 0% 命中
  （原用的 `ens160_NETBandwidthUtil` 10 天全量 max 仅 **0.3685**，即 **0.37%**，阈值 90 永不可达）
- **重复报警抑制**：同 (entity, alarm_id) 600 秒内只保留首条，**抑制率 85.4%**（101,478 → 14,806 条）
- **时区注意**：groundtruth 的 `st_time`/`ed_time` 为数据时钟字符串（北京时间），
  epoch 一律以数据自带 `time` 列为锚点推算，**禁止用 `.timestamp()`**（naive datetime 会按本机时区转换）
- **已否决的方案（有量化记录）**：曾把 `FSInodeUsedPercent` 阈值从 90 降到 20 使其生效，
  结果产生 6,276 条常驻告警、落入 core 噪声层，把 CPU 型 Top-1 从 96% 拉低到 79%，
  而 DISK 型毫无改善 → **整条规则删除**。答辩可讲"我们试过并量化了副作用"
