# AlarmFlood Copilot · 告警洪水治理值班台

> **一次故障 = 上千条告警。** 值班工程师要在几十条重复通知里找出真正的病根，人工梳理平均 **40+ 分钟**。
>
> 本项目把告警洪水**压缩成少数几个洪水窗口 → 定位根因实体 → 生成可执行的处置单 → 一键派单**，
> 把"人找根因"变成"系统给候选、人做确认"。

---

## 一、快速开始（三行命令）

> **看板只读仓库里已提交的结果文件**（`alarmflood/results/*.parquet` + `results/reports/*.json`），
> **不需要重跑管线、不需要下载数据集、不需要 LLM API key** —— 开箱即可演示。
> 这是刻意的设计（"回放模式"），保证评审随时能打开、不受 API 限流影响。

在**仓库根目录**执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r alarmflood\requirements.txt
.\.venv\Scripts\python.exe -m streamlit run alarmflood\app.py --server.port 8502
```

然后浏览器打开 **http://127.0.0.1:8502**

**快捷方式**：`alarmflood\run_dashboard.bat` 会自动找 Python 并启动（端口 8502）。
它的查找顺序是 `AIC_PYTHON` 环境变量 → 仓库根 `.venv` → PATH 中的 `python`。

> 如果你的虚拟环境不在仓库根（例如在 `alarmflood\.venv`），启动看板时设一下：
> ```powershell
> $env:AIC_PYTHON = "D:\path\to\aic_repo\alarmflood\.venv\Scripts\python.exe"
> alarmflood\run_dashboard.bat
> ```

---

## 二、运行环境与技术栈

| 项 | 值 |
|---|---|
| 操作系统 | Windows（看板已在 Windows 实测；纯 Python 逻辑跨平台） |
| Python | **3.10**（`requirements.txt` 锁定基准）/ **3.12** 亦可运行 |
| 依赖清单 | `alarmflood/requirements.txt`（唯一来源，版本已锁定） |

| 库 | 版本 | 用途 |
|---|---|---|
| pandas | 2.3.3 | 时序与表格处理（M1–M4 全链路） |
| numpy | 2.2.6 | 数值计算 |
| pyarrow | 25.0.1 | parquet 读写（**模块间统一接口**） |
| drain3 | 0.9.11 | 日志模板挖掘（M1 模板归并） |
| matplotlib | 3.10.9 | 看板图表（瀑布图/时间轴，中文字体 SimHei） |
| streamlit | 1.65.0 | 演示看板（M5） |
| openai | 3.29.0 | LLM 调用（M3 路线 B / M4 处置单，OpenAI 兼容接口） |

---

## 三、目录结构

```
aic_repo/
├── README.md                      本文件
├── .env.example                   API 密钥模板（复制为 .env 后填写；.env 已被 gitignore）
├── .gitignore
└── alarmflood/
    ├── requirements.txt           依赖清单（唯一来源）
    ├── app.py                     M5 演示看板（Streamlit，两页签）
    ├── run_dashboard.bat          看板启动脚本（自动找 Python）
    ├── ALGORITHM_SPEC.md          《核心算法说明》：界面功能 ↔ 算法模块映射（要求2）
    ├── config/
    │   └── teams.csv              实体 → 责任团队映射（工单分派用）
    ├── loaders/
    │   ├── loader_aiops2021.py    数据源 1：AIOps2021 挑战赛数据 → 统一五列告警流
    │   └── loader_bank.py         数据源 2：其他告警源适配模板（统一中间格式示例）
    ├── modules/
    │   ├── m1_templater.py        M1 告警模板归并（drain3）
    │   ├── m2_window.py           M2 洪水窗口检测 + 三级分诊
    │   ├── m3_rank.py             M3 路线A：规则排序（四路加权）
    │   ├── m3_llm.py              M3 路线B：LLM 根因排序
    │   ├── m3_fusion.py           M3 路线C：rule × LLM 加权融合
    │   └── m4_report.py           M4 处置单 + 工单 + 幻觉校验
    ├── scripts/                   诊断与实验脚本（含 check_replay.py 回放自检，不参与主链路）
    ├── data/                      告警流中间产物（parquet）
    └── results/                   全部实验结果（parquet / json / md）
```

---

## 四、五个模块与命令

> 所有命令都在 **`alarmflood/` 目录下**执行（或用完整路径）。

### 主链路（按顺序）

```powershell
# M1 · 告警模板归并
.venv\Scripts\python.exe modules\m1_templater.py

# M2 · 洪水窗口检测 + 三级分诊（core / semi / novel）
.venv\Scripts\python.exe modules\m2_window.py

# M3 路线A · 规则排序（四路加权：新发数 / 新发最早 / 分层加权频次 / 活跃度）
.venv\Scripts\python.exe modules\m3_rank.py
```

### M3 路线B · LLM 根因排序（需要 API key）

先准备密钥（**`.env` 已被 gitignore，不会提交**）：

```powershell
copy ..\.env.example ..\.env
# 然后编辑 ..\.env，填入 OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL
```

```powershell
.venv\Scripts\python.exe modules\m3_llm.py --dry-run         # 不调 API，验证流水线
.venv\Scripts\python.exe modules\m3_llm.py --max-windows 3   # 先跑 3 个窗口
.venv\Scripts\python.exe modules\m3_llm.py                   # 全量
```

### M3 路线C · 融合（依赖路线A与B的结果）

```powershell
.venv\Scripts\python.exe modules\m3_fusion.py
```

### M4 · 处置单与工单（需要 API key）

```powershell
.venv\Scripts\python.exe modules\m4_report.py
```

### M5 · 演示看板

```powershell
.venv\Scripts\python.exe -m streamlit run app.py --server.port 8502
```

---

## 五、数据集准备（**只在重跑管线时需要**）

数据集体积较大，**不在本仓库内**。看板演示**不需要**它。

`loaders/loader_aiops2021.py` 按以下顺序解析数据集根目录，任选一种即可：

| 优先级 | 方式 | 说明 |
|---|---|---|
| 1 | 环境变量 **`AIOPS2021_RAW`** | 指向含 `aiops21_groundtruth.csv` 与 `aiops2021-2/` 的目录 |
| 2 | 仓库同级 `aic_data/raw/aiops2021/aiops2021-main/AIOps2021` | 无环境变量时的约定位置 |
| 3 | `E:/aic_data/AIOps2021` | 兜底路径 |

```powershell
$env:AIOPS2021_RAW = "D:\your\path\to\AIOps2021"
.venv\Scripts\python.exe loaders\loader_aiops2021.py
```

> 数据集许可为 **CC BY-NC 4.0（非商用）**，详见 `THIRD_PARTY.md`「二、许可要点」（含许可原文留档）。

---

## 六、查看实验结果

全部结果都在 **`alarmflood/results/`**：

| 文件 | 内容 |
|---|---|
| `experiments.md` | **主对比实验表** + D2/D3 文字分析。表体在 `<!-- AUTO-TABLE -->` 标记内，由 `modules/m3_rank.py` **统一渲染**（freq/rule 自算，llm 读 `llm_results.json`，fusion 读 `fusion_results.json`）→ **单一写入口，各方法互不覆盖** |
| `llm_results.json` | LLM 路线结果（供对比表自动渲染） |
| `llm_experiments.md` | LLM 路线（D3）实验记录，含截断喂消融 |
| `llm_examples.md` | LLM 推理链样例（答辩素材） |
| `fusion_results.json` | 融合路线结果（供对比表自动渲染） |
| `fusion_experiments.md` | 融合路线（D6）完整报告：α 网格、交叉验证、互补性、局限 |
| `REPLAY_MODE.md` | 「回放模式」设计说明（评审无需 API key 即可演示全流程） |
| `replay_check.md` | 回放模式可重复性自检记录（`scripts/check_replay.py`） |
| `rule_audit.md` | **告警规则自查记录**：3 条规则阈值从未生效的发现、量化收益与决策 |
| `data_choices.md` | 数据与阈值选型记录 |
| `reports/` | M4 处置单与工单（逐窗口 JSON）+ 抽检记录 |

---

## 七、常见问题

**Q：看板启动报字体相关警告 / 图表中文变方块？**
看板用了 `SimHei`／`Microsoft YaHei`，Windows 自带。若提示 matplotlib 字体缓存无写权限，那只是缓存警告，不影响图表显示。

**Q：`streamlit: command not found`？**
用 `python -m streamlit run ...`，不要直接敲 `streamlit`。

**Q：端口 8502 被占用？**
换一个端口：`--server.port 8503`。

**Q：脚本输出里的 emoji 在终端报 `UnicodeEncodeError: 'gbk' codec...`？**
中文 Windows 控制台默认 GBK。先执行 `chcp 65001`，或在脚本里
`sys.stdout.reconfigure(encoding="utf-8")`。

**Q：不重跑管线，能不能看到全部功能？**
可以。看板读的是仓库里已提交的结果文件（895 个洪水窗口 + M4 处置单与工单），开箱可演示。

---

## 八、许可

- **本仓库代码**：**归参赛团队所有**（仓库内未声明开源许可，即默认保留所有权利）。
- **数据集**：AIOps Challenge 2021 Dataset © 2021 by Dan Pei，
  licensed under **CC BY-NC 4.0（署名 — 非商业性使用）**
  → 竞赛等非商业用途可用；**商业化需替换为自有数据**。
  详见 `alarmflood/THIRD_PARTY.md`「二、许可要点」。
- **第三方库**许可清单：`alarmflood/THIRD_PARTY.md`
