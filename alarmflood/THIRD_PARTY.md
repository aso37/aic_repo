# 第三方数据与依赖清单（THIRD_PARTY.md）

> 比赛规则：引用他人技术、算法或数据需在参赛材料中明确标注。本文件从第一天维护，最后导出为附录表格。

| 名称 | 版本 | 用途 | 许可 | 获取 |
|---|---|---|---|---|
| AIOps 挑战赛数据（2021 银行场景，aiops2021-2） | - | 主评测数据：metric/logs/trace + 根因标注（159 窗口） | 比赛条款（待复核） | 天池 |
| Loghub (HDFS) | - | D6 消融实验（迁移验证） | 开源 | github.com/logpai |
| pandas | 2.3.0 | 表格处理 | BSD | pip |
| pyarrow | (pip 安装版) | parquet 读写 | Apache-2.0 | pip |
| drain3 | (pip 安装版) | 告警模板归并（M1） | MIT | pip |
| （D3 填）LLM API | | 根因推理/处置单/工单 | 商用条款 | |

## 数据处理说明（答辩可引用）

- 数据集本身无告警文件；告警流由 10 条阈值规则对 metric 指标生成（模拟监控系统行为），规则与阈值记录于 `loaders/loader_aiops2021.py` 的 RULES 表
- 重复报警抑制：同 (entity, alarm_id) 600 秒内只保留首条（抑制率 85.4%）
- 时区注意：groundtruth 的 st_time/ed_time 为数据时钟字符串，epoch 一律以其自带 time 列为锚点推算，禁止用 `.timestamp()`（本地时区不一致，实测差 8 小时）
