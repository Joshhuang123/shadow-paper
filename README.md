# Shadow Paper · 论文阅读器

> 从 **shadow-ebook**（`~/shadow-learning`，英语电子书跟读学习 App）派生出的**新版本**：
> 保留「点词查义 + 生词本 + 艾宾浩斯复习 + PWA 离线」这套真正通用的能力，
> **砍掉**全部英语学习功能（蓝思值 AR、跟读录音评分、章节理解测验、语法模块、家长监控）。

论文不是给孩子跟读的英文读物：它不需要朗读评分、不需要章节测验、不需要按平均句长估阅读等级。
把这些塞进阅读器只会让界面变吵。所以**另起一个项目**，而不是在原 App 上加开关。

---

## 一句话

**把 arXiv 的 LaTeX 源码变成一篇能点词、能查术语、能记笔记的单栏论文。**

---

## 为什么要走 LaTeX 源码而不是 PDF

PDF→纯文本这条路，**对堆叠公式没有解**：

| | pypdf 裸抽取 | pdftotext -layout |
|---|---|---|
| 分式 `0.3·r/(1−Q/200N)` | `P(Q) =0.3 / r / 1− Q200N ,` | 一样烂（拆成三行） |
| 空格 | `a deep market` → `adeep market` | `a deep market` ✅ |
| 带横杠的字母 | `ȳ∗` → `¯y∗` | `ȳ∗` ✅ |
| 双栏 | 靠猜 | 靠猜 |

而 LaTeX 源码是**排版之前**的东西：公式是 `$...$` 原式，结构是 `\section` / `\begin{figure}`，
**什么都不用猜**。实测一篇 108 页的论文：

```
90 个小节 · 502 个块 · 32 张图 · 4 张表 · 26,260 词 · 1,921 个公式（带原式）
```

---

## 两条导入路径

### A. arXiv（推荐，公式完美）

```
arXiv ID ──► e-print 源码包 ──► LaTeX 清洗 ──► pandoc JSON AST ──► 论文结构 ──► PNG 图
```

`pandoc -t json` 给的是**结构化 AST**，公式在 `Math`/`InlineMath` 节点里带着 LaTeX 原式，
小节在 `Header` 节点里带着干净的标题和 id —— 正好是阅读器要的。

**清洗器要解决的三件事**（`extensions/latex_clean.py`）：

1. **带可选参数的调用** —— `\label[appendix]{x}`、`\cite[see][p.~3]{k}`，
   pandoc 的 LaTeX reader 直接报错。
2. **自定义宏** —— pandoc 遇到不认识的宏会**一路吃到文件尾**，报 `unexpected end of input`。
   解法是用花括号深度扫描把宏定义抠出来，再把调用点就地展开成参数。
3. **`\input` / `\include`** —— pandoc 不会自己找子文件，必须内联。

外加**迭代收敛**：pandoc 报哪个宏不认，就把它加进「展开名单」重跑，直到通过
（实测这篇收敛在 5 个宏：`nameref / quad / ref / textbf / hyperref`）。
展开名单缓存在 `data/unwrap_cache.json`，同一篇第二次导入只要几秒。

### B. 本地 PDF（通用，但公式救不回来）

```
PDF ──► pdfplumber 逐行取（文本 + 字号 + 坐标） ──► 双栏几何切分 ──► 论文结构
```

- **双栏检测**：用词的几何分布找中缝（中间 8% 宽度里几乎没有词的起点 → 判定双栏），
  然后左栏右栏各取一遍再拼。
- **小节标题判定**要同时过三关：
  1. 编号长得像节号（`1` / `1.2` / `A` / `A.1`，每段 < 100 —— 挡掉图注里的 `1000 Scale threshold`）
  2. 标题部分**像散文**（挡掉公式碎片 `H N 1 2 ≈`、`I ASI`）
  3. 字号不小于正文（这篇论文标题与正文同号，所以这条只是兜底）

**实测踩到的两个天花板**（不是工具的问题，是 PDF 文字层本身）：

1. **这篇论文的 PDF 文字层里没有空格字符** —— 词间距是排版定位给的视觉间隙。
   所以 pdftotext 和 pdfplumber 抽出来都会**粘词**
   （`howintelligence`、`competence,which capturesdomain-specificcapabilities`）。
2. 中心对齐的标题空格丢得更狠：`2 AN ILLUSTRATIVE EXAMPLE` → `2 ANILLUSTRATIVEEXAMPLE`。

→ **这条路径只保证「能读全文、能点词查义、能收生词本」**，小节标题可能粘词。
**公式密集的论文请走 arXiv 路径。**

---

## 论文文档结构（前后端的唯一真相）

```json
{
  "id": "2611.05417", "title": "Organizing Intelligence",
  "authors": ["Alex Imas", "Andrew Koh", "Shane Legg"],
  "sections": [
    {"id": "s1", "title": "Introduction", "level": 1,
     "blocks": [
       {"k": "p",     "t": "段落正文，⟦MATH:9,\; 1⟧⟧ 是行内公式占位符"},
       {"k": "eq",    "latex": "\\bm y = (y_1, y_2, y_*)", "num": "1"},
       {"k": "fig",   "src": "assets/2611.05417_fig-x.png", "caption": "图注"},
       {"k": "table", "rows": [["Role", "Task skills"], ["Coder", "⟦MATH:9,\; 1⟧⟧"]], "caption": ""},
       {"k": "list",  "items": ["第一条"], "ordered": false},
       {"k": "quote", "t": "定理内容", "kind": "theorem"}
     ]}
  ],
  "toc": [{"id": "s1", "title": "Introduction", "level": 1}],
  "refs": [{"text": "参考文献条目"}]
}
```

**行内公式占位符 `⟦MATH:<LaTeX>⟧⟧⟧`**：不用 `$...$`，因为英文正文里 `$` 太常见（货币、变量）。
占位符在切句、点词时必须**当整体**处理 —— 里面有点号，不保护住的话一句公式会被切成好几段。

---

## 从 shadow-ebook 搬来了什么 / 砍掉了什么

**搬**（照搬语义，不是复制粘贴）：

| 能力 | 来源 | 说明 |
|---|---|---|
| 查词 | `extensions/dictionary.py` | 有道 jsonapi 主源 + datamuse 兜底，SQLite 缓存 30 天/负缓存 1 天 |
| SRS | `extensions/parent_data.py` 的 vocab 部分 | 状态机 learning→practicing→familiar→mastered，间隔 0/2/5/14 天 |
| 句子切分 | `extensions/books.py` 的 `_split_sentences*` | 缩写/首字母/小数点/省略号保护，**一字未改** |
| 连字还原 | `_normalize_ligatures` | PDF 文本里的 ﬁ/ﬂ/ﬃ 还原成普通字母 |
| 前端壳 | `web/styles/*`、`a11y.js`、`theme.js`、`dispatcher.js` | 深浅色主题、ESC 关弹窗、点击路由 |

**砍**：AR 蓝思值、跟读录音评分、章节理解测验、语法模块、音素、知识图谱、家长监控、TTS/ASR。

**修**（shadow-ebook 里本来就是坏的）：

1. **两套 SRS 互不相通** —— 原项目服务端一套（0/2/5/14 天）、前端 localStorage 一套（1/3/7/14/30 天），
   而且服务端的 `/api/vocab/lookup` **前端根本没接上**（查词不进生词本）。
   论文版只留服务端那套，并把查词点击**真正接进** `/api/vocab/lookup` ——
   读论文遇到术语点一下就该进生词本，这正是这个 App 对论文最有用的地方。
2. **蓝思值两个公式**（后端 `calc_lexile` vs 前端 `calculateAR()`）—— 论文版直接不要这个功能。

---

## 跑起来

```bash
cd ~/shadow-paper
python3 -m venv .venv && source .venv/bin/activate
pip install flask pdfplumber pypdf
python app.py                 # http://127.0.0.1:5003
```

**外部依赖**（导入用，跑阅读器不需要）：
- `pandoc`（brew install pandoc）—— LaTeX → AST
- `pdftotext` / `pdftoppm`（poppler）—— PDF 文本与图
- `python3.9+`（`tarfile.extractall(filter=)` 是 3.12+，本项目做了版本兼容）

导入：

```bash
curl -X POST http://127.0.0.1:5003/api/paper/import/arxiv \
     -H 'Content-Type: application/json' -d '{"id":"2611.05417"}'
```

测试：

```bash
python3 -m pytest tests/ -q      # 51 个，全部离线，不打网
```

---

## 已知限制

| 限制 | 说明 |
|---|---|
| PDF 路径没有公式 | PDF→文本对堆叠公式无解，请走 arXiv 路径 |
| PDF 路径会粘词 | 这类 PDF 的文字层没有空格字符，词间距是视觉间隙（`howintelligence`）；中心对齐的标题更狠（`ANILLUSTRATIVEEXAMPLE`） |
| PDF 路径没有图 | PDF 里的图是矢量/整页，抽不出独立图；当前 PDF 路径**不抽图** |
| 表格只保留结构 | pandoc 的 Table 结构能拿到，但复杂的 booktabs 表会有损 |
| 参考文献 | arXiv 路径需要 `--citeproc`，正文里的 `\citep` 已统一成 `\cite`；实测这篇 105 条 |
| 查词打公网 | 有道/datamuse 的可达性依赖网络；断了会返回 503 而不是假装「没这个词」 |

---

## 和 shadow-ebook 的关系

```
shadow-ebook (~/shadow-learning)          Shadow Paper (~/shadow-paper)
  英语电子书跟读学习                       论文阅读
  EPUB/朗读/跟读评分/语法/AR值/家长端       arXiv+PDF/公式/图/目录/查词/生词本/笔记
        └── 查词 · SRS · 句切 · 前端壳 ──────┘  （复用）
```

两个项目**各自独立**，不共享数据库也不互相依赖；只有几个模块是按同一套语义搬过来的。
