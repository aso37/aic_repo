# -*- coding: utf-8 -*-
"""
entity_extract.py — 告警文件没有实体列时的兜底：从告警内容文本里抽实体

适用：很多告警系统只有一列文本（"主机 web01 CPU使用率95%超过阈值"），
      实体藏在文本里。这时不要求用户去改造文件，而是
        ① 按优先级正则抽取（IP / 键值对 / 中文前缀 / 域名 / 带编号名 / 连字符名）
        ② 仍抽不出的行，用「模板差分」补：同一模板下多条告警，
           找出那个在同一位置变化的 ASCII token，它就是实体（svc-a / svc-b）
        ③ 还抽不出就记作 (未识别)，并把覆盖率如实报给值班人核对

设计原则：宁可报"未识别"，也不编造一个实体。抽取结果全部摊到界面上让人确认。
"""
import re
import pandas as pd

UNKNOWN = "(未识别)"

# 边界说明：中文字符在 Python re 里也算 \w，用 \b 会失效（"调用svc-a超时" 里 svc-a 两侧
# 没有 \b）。这里统一用"ASCII 词字符"前后瞻做边界，保证嵌在中文里的名字也能被抓到。
_L = r"(?<![A-Za-z0-9_.\-])"
_R = r"(?![A-Za-z0-9_.\-])"

# 按优先级排列：(方法名, 正则)。命中即停，取第一个捕获组
PATTERNS = [
    ("IP地址", re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?::\d+)?(?![\d.])")),
    ("键值对", re.compile(
        r"(?:host|hostname|instance|pod|node|server|service|entity|cmdb_id|ci|target|"
        r"主机|服务器|服务|实例|节点|设备|网元|对象)\s*[:=＝]\s*([A-Za-z0-9_.\-]{2,64})", re.I)),
    ("中文前缀", re.compile(r"(?:主机|服务器|服务|实例|节点|设备|网元)\s*([A-Za-z0-9_.\-]{2,64})")),
    ("域名", re.compile(_L + r"((?:[A-Za-z0-9_-]+\.){2,}[A-Za-z]{2,})" + _R)),
    # 带编号名：Redis02 / Mysql01 / node1 / Tomcat02（AIOps 数据集的实体命名风格）
    ("带编号名", re.compile(_L + r"([A-Za-z][A-Za-z0-9_-]*\d+)" + _R)),
    # 连字符名：svc-a / order-service / web-01
    ("连字符名", re.compile(_L + r"([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+)" + _R)),
]

# 模板差分用：ASCII token（中文里的 svc-a、Redis02 这类会被单独切出来）
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*")
PURE_NUM = re.compile(r"^\d+(\.\d+)?$")


def compile_custom(pattern: str):
    """用户自定义正则；编译失败抛出可读错误（界面直接显示）"""
    try:
        return re.compile(pattern)
    except re.error as e:
        raise ValueError(f"自定义正则无法编译：{e}")


def extract(texts: pd.Series, custom: str | None = None):
    """第一轮：按优先级正则逐行抽取。返回 (实体 Series, 方法 Series)"""
    pats = []
    if custom:
        pats.append(("自定义正则", compile_custom(custom)))
    pats += PATTERNS

    ents, meths = [], []
    for t in texts.astype(str):
        hit = None
        for name, p in pats:
            m = p.search(t)
            if not m:
                continue
            val = (m.group(1) if m.groups() else m.group(0)).strip(" \t,，.。;；:：")
            if val:
                hit = (val, name)
                break
        ents.append(hit[0] if hit else UNKNOWN)
        meths.append(hit[1] if hit else "未识别")
    return pd.Series(ents, index=texts.index), pd.Series(meths, index=texts.index)


def refine_by_template_diff(df: pd.DataFrame, ent: pd.Series, meth: pd.Series):
    """第二轮：模板差分，补正则抓不到的行。两条路子都走：

      A. 模板内差分 —— Drain3 把实体归成通配时（"svc-<*> 调用超时"），
         同一模板下多条告警里"在同一位置变化的非数字 token"就是实体。
      B. 模板间差分 —— Drain3 给每个实体单独建了模板时（"节点A…"/"节点B…"），
         找只有一处 token 不同的同族模板，那处差异就是实体变量位。

    两者都只在"其余位置完全相同"时才认，避免把两个不相干的模板硬凑成一家。
    """
    ent, meth = ent.copy(), meth.copy()

    # ---- A. 模板内差分 ----
    for _, idx in df.groupby("template_id").groups.items():
        idx = list(idx)
        if len(idx) < 2:
            continue
        unk = [i for i in idx if ent[i] == UNKNOWN]
        if not unk:
            continue
        toks = {i: [t for t in TOKEN.findall(str(df.at[i, "content"]))
                    if not PURE_NUM.match(t)] for i in idx}
        lengths = {len(toks[i]) for i in idx}
        if len(lengths) == 1:
            for pos in range(lengths.pop()):
                if len({toks[i][pos] for i in idx}) == 1:
                    continue          # 该位置不变，不是变量
                for i in unk:
                    ent[i], meth[i] = toks[i][pos], "模板差分(模板内)"
                break
        else:
            common = set.intersection(*[set(toks[i]) for i in idx])
            for i in unk:
                cand = [t for t in toks[i] if t not in common]
                if cand:
                    ent[i], meth[i] = cand[0], "模板差分(模板内)"

    # ---- B. 模板间差分（并查集把同族模板连起来）----
    if "template" not in df.columns:
        return ent, meth
    tpl = df.groupby("template_id")["template"].first()
    toks = {t: [x for x in TOKEN.findall(str(txt)) if not PURE_NUM.match(x)]
            for t, txt in tpl.items()}
    ids = [t for t in tpl.index if toks[t]]
    parent = {t: t for t in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pos_of = {}
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            ta, tb = toks[a], toks[b]
            if len(ta) != len(tb):
                continue
            diff = [k for k, (x, y) in enumerate(zip(ta, tb)) if x != y]
            if len(diff) != 1:        # 只有"恰好一处不同"才算同族
                continue
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
            pos_of.setdefault(find(a), diff[0])

    fam = {}
    for t in ids:
        fam.setdefault(find(t), []).append(t)
    for root, members in fam.items():
        pos = pos_of.get(root)
        if pos is None or len(members) < 2:
            continue
        for t in members:
            for i in df.index[df["template_id"] == t]:
                if ent[i] == UNKNOWN:
                    ent[i], meth[i] = toks[t][pos], "模板差分(模板间)"
    return ent, meth


def summarize(ent: pd.Series, meth: pd.Series) -> dict:
    """抽取结果的诚实统计：覆盖率、各方法命中数、Top 实体、样例"""
    n = len(ent)
    unk = int((ent == UNKNOWN).sum())
    ok = ent[ent != UNKNOWN]
    return {
        "n": n,
        "unknown": unk,
        "coverage": (n - unk) / n if n else 0.0,
        "n_entities": int(ok.nunique()),
        "methods": meth.value_counts().to_dict(),
        "top": ok.value_counts().head(10).to_dict(),
    }
