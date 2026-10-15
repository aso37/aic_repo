# 回放模式可用性验收（D5 · B 角色）

> 由 `python scripts/check_replay.py` 生成。回放模式 = 界面只读预计算产物、运行时零 LLM 调用。

| 检查项 | 结果 |
|---|---|
| app.py 无 LLM 依赖（禁词 ['openai', 'm3_llm', 'OPENAI_API_KEY']） | PASS |
| 默认加载无异常 | PASS |
| 点击「派单」无异常 | PASS |
| 无处置单窗口优雅降级 | PASS |

## 处置单覆盖

- 洪水窗口总数：895
- 已有处置单：111（12%）
- 侧边栏前 15 个窗口均已有处置单：True

**总体：PASS**