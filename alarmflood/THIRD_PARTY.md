# 第三方数据与依赖清单（THIRD_PARTY.md）

> 比赛规则：引用他人技术、算法或数据需在参赛材料中明确标注。本文件从第一天维护，最后导出为附录表格。

## 一、数据集与依赖

| 名称 | 版本 | 用途 | 许可 | 获取 |
|---|---|---|---|---|
| AIOps 挑战赛数据（2021 银行场景，aiops2021-2） | 2021 | 主评测数据：metric/logs/trace + 根因标注（159 窗口） | **CC BY-NC 4.0（署名-非商业性使用）** | 天池 / GitHub AIOps2021 |
| Loghub (HDFS) | — | D6 消融实验（迁移验证） | 开源（具体协议待核） | github.com/logpai |
| pandas | **3.0.6** | 表格处理 | BSD-3-Clause | pip |
| pyarrow | 25.0.1 | parquet 读写 | Apache-2.0 | pip |
| drain3 | 0.9.11 | 告警模板归并（M1） | MIT | pip |
| scikit-learn | 1.9.1 | 相似度 / 统计工具 | BSD-3-Clause | pip |
| Streamlit | 1.65.0 | 演示看板（M5） | Apache-2.0 | pip |
| matplotlib | 3.11.2 | 图表 | PSF-based | pip |
| python-dotenv | 1.2.4 | 环境变量 | BSD-3-Clause | pip |
| openai (SDK) | 3.29.0 | LLM 调用（M3 路线 B / M4） | Apache-2.0 | pip |
| （D3 填）LLM 服务 | | 根因推理 / 处置单 / 工单 | 商用条款 | |

> 完整版本锁定见仓库根 `requirements.txt`。

## 二、⚠️ 许可要点（必须写进材料）

**主数据集许可是 `CC BY-NC 4.0`，其中 `NC` = NonCommercial（非商业性使用）。**

来源：数据集自带 `README.md`：

> AIOps Challenge 2021 Dataset © 2021 by Dan Pei is licensed under Creative Commons
> Attribution-NonCommercial 4.0 International.

**含义与影响**：

1. **本竞赛使用没问题**——AIC 是公益性学科竞赛，属非商业用途；且材料中已按 CC BY 要求署名标注来源
2. **但商业化路径受约束**：本作品（AlarmFlood Copilot）如进入商业转化，**不能继续使用该数据集或其衍生告警流**。
   答辩/商业计划书中的"商业转化潜力"必须说明这一点：产品算法与架构可迁移，**数据需替换为客户自有告警**
3. **D6 的 Loghub 消融因此更有价值**——它证明"方法不挑数据源"，弱化对单一数据集的依赖
4. 附录表格中应保留完整许可名称与出处链接

## 三、数据处理说明（答辩可引用）

- **数据集本身无告警文件**；告警流由 **8 条阈值规则**对 metric 指标生成（模拟监控系统行为），规则与阈值记录于 `loaders/loader_aiops2021.py` 的 `RULES` 表
- **阈值经逐 KPI 实测校准**（2026-10-14）：原方案对全部 KPI 统一使用阈值 90，导致 3 条规则从未触发。
  实测各 KPI 正常范围差异极大（内存常态 84%、inode 常态 16%、sdb 磁盘常态 0%、网卡带宽仅 3.2%），
  统一阈值不可行。校准后 8/8 规则全部生效，根因实体覆盖率由 80% 提升至 92%
- **网络型故障的关键指标**：`NETWORK_TCP-FIN-WAIT`（TCP 半关闭连接数）——
  非零样本占比仅 1.18%，但在 71% 的网络故障窗内出现非零，对其他故障类型 0% 命中
  （原用的 `ens160_NETBandwidthUtil` 是 0~1 比例，实测 max 仅 0.032，阈值 90 永不可达）
- **重复报警抑制**：同 (entity, alarm_id) 600 秒内只保留首条，**抑制率 84.8%**（103,387 → 15,766 条）
- **时区注意**：groundtruth 的 `st_time`/`ed_time` 为数据时钟字符串（北京时间），
  epoch 一律以数据自带 `time` 列为锚点推算，**禁止用 `.timestamp()`**（naive datetime 会按本机时区转换）
- **已否决的方案（有量化记录）**：曾把 `FSInodeUsedPercent` 阈值从 90 降到 20 使其生效，
  结果产生 6,276 条常驻告警、落入 core 噪声层，把 CPU 型 Top-1 从 96% 拉低到 79%，
  而 DISK 型毫无改善 → **整条规则删除**。答辩可讲"我们试过并量化了副作用"
