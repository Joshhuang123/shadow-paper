# Shadow Paper · 论文阅读器

> 一个**本地跑**的论文阅读器：把论文变成单栏、公式可点开放大、术语可点词查义、可记笔记。
> 中英文论文都能读。

给「要读很多论文、但不想在 PDF 阅读器里做笔记」的人用。
它从 [shadow-ebook](https://github.com/Joshhuang123/shadow-ebook)（英语电子书跟读学习 App）派生：
保留了「点词查义 + 生词本 + 艾宾浩斯复习 + PWA 离线」这套真正通用的能力，
砍掉了全部英语学习功能（蓝思值 AR、跟读录音评分、章节理解测验、语法模块、家长监控）。

论文不是给孩子跟读的英文读物：它不需要朗读评分、不需要章节测验、不需要按平均句长估阅读等级。
把这些塞进阅读器只会让界面变吵。所以**另起一个项目**，而不是在原 App 上加开关。

---

## 快速开始

```bash
git clone https://github.com/Joshhuang123/shadow-paper.git
cd shadow-paper

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 导入依赖（只有这两个需要；不装也能读已入库的论文）
brew install pandoc poppler          # macOS
pip install pdfplumber pypdf

./run.sh                              # → http://127.0.0.1:5003
```

导一篇论文：

```bash
# arXiv（公式最准）
curl -X POST localhost:5003/api/paper/import/arxiv \
     -H 'Content-Type: application/json' -d '{"id":"2611.05417"}'

# 或者在网页上「上传 PDF」
```

**朗读**用的是 macOS 自带的 `say`（离线可用），Linux 上会退回浏览器的 speechSynthesis。

---

## 中文论文能用吗

**能用，但打折的地方要知道**：

| 能力 | 中文论文 | 说明 |
|---|---|---|
| 全文阅读 | ✅ | 中文 PDF 的文字层抽取正常 |
| 小节目录 | ✅ | 认得「第 X 章 / 一、/ 1 引言 / 2.1 xxx」这些中文写法，**并且会跳过目录页**（目录里的条目长得跟正文标题一模一样） |
| 公式 | ⚠️ | **arXiv 路径**（拿到 LaTeX 源）里是**原式**，能重排、能 KaTeX 渲染、能点开放大；**PDF 路径的公式会被压平**（分子分母挤成一行）。中文论文基本只有 PDF，所以中文论文的公式只能看个大概 |
| 朗读 | ✅ | 中文用系统音色（Tingting），英文用 Samantha。macOS 用 `say` 离线合成；Linux 上没有 `say`，会退回浏览器的 speechSynthesis（音质差一点，但能用） |
| 整句翻译 | ✅ | **方向自动判断**：中文句子中译英，英文句子英译中 |
| 点词查义 | ❌ | 查词只认英文单词。中文论文里的**英文术语**照样能点、能查、能收进生词本 |
| 生词本 / 复习 | ✅ | 与语言无关 |
| 笔记 / 进度 | ✅ | 与语言无关 |

一句话：**中文论文适合「读 + 记笔记 + 收集英文术语」**，
但如果是要精读公式密集的中文论文，去找它的 LaTeX 源或 arXiv 版会准得多。

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
- **中文标题**另有一套写法：`第 X 章/节`、`一、二、…`、`1 引言`、`2.1 xxx`，
  并且会**跳过目录页**（目录里的条目长得跟正文标题一模一样，认了会生成一堆空章节）。

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
| SRS | `extensions/vocab.py` | 状态机 learning→practicing→familiar→mastered，间隔 0/2/5/14 天 |
| 句子切分 | `extensions/sentences.py` 的 `_split_sentences*` | 缩写/首字母/小数点/省略号保护，**一字未改** |
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

## 能干什么

| 功能 | 怎么用 | 实现 |
|---|---|---|
| **点词查义** | 点正文里任意英文单词 | `/api/dict/<word>`（有道 + datamuse，SQLite 缓存 30 天） |
| **点词朗读** | 点词的同时**自动念出来**；弹窗里也有「🔊 朗读」再念一遍 | 服务端 `macOS say` 合成 → m4a；顶栏有静音开关 |
| **整句翻译** | 查义弹窗里「翻译整句」 | 服务端 `/api/translate`（MyMemory，带 30 天缓存） |
| **公式可视化** | 点正文里任意公式 → 弹卡片：**放大渲染 + LaTeX 源码（可复制）** | KaTeX 重新渲染一遍（displayMode） |
| 生词本 + 复习 | 查义时「加入生词本」，右侧面板按艾宾浩斯曲线复习 | 服务端 SRS（0/2/5/14 天） |
| 笔记 | 选中正文文字 → 「记笔记」 | `paper_notes` 表 |
| 阅读进度 | 自动记，回到上次读到的位置 | `reading_progress` 表 |
| 目录跳转 | 左侧 90 个小节，点了直接跳 | 见下面的坑 |
| 离线 / 装桌面 | PWA，可添加到主屏幕 | service-worker + manifest |

### 朗读为什么用 macOS 自带的 `say`

`edge-tts`（微软 Edge 的免费 TTS）要连 `wss://speech.platform.bing.com`，
实测这台机器 **403**（WSServerHandshakeError）。macOS 自带的 `say` **完全离线、秒回**，
Samantha / Alex 这些音色读单词足够了。

`say` 只出 AIFF，**Chrome 播不了**，所以服务端再转一道 m4a（`afconvert`），
浏览器通吃，体积还小一个数量级（8.9KB vs 46KB）。

### 翻译为什么挪到服务端

前端原来直连 MyMemory，URL 写的是 `/api/v1/get` —— **那个路径是 404**，
所以「翻译整句」按钮一直没反应（正确路径没有 `/api/v1/`）。
现在改成调服务端 `/api/translate`：能缓存、能给出明确的中文错误提示，
前端直连只留作兜底。

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

### ⚠️ 一个必须记住的坑：**后台标签页里 rAF 会被暂停**

症状：页面看起来「公式全是 LaTeX 原文」，但 KaTeX 明明已经加载好了（`katex-ready` 类也在）。

根因：前端把公式渲染**分批**做（每帧 60 个，免得主线程卡住），
而**后台标签页的 `requestAnimationFrame` 会被 Chrome 暂停** → 分批渲染永远停在第一批。

→ 分批调度**必须用 `setTimeout`**，不能用 rAF（后台只被限到 1s 一次，不会停）。
这个坑今天在两个项目里各踩一次（视频平台的 Vue 弹窗不渲染，也是同一个根因）。

---

## 踩坑记录（2026-11-23 建这个项目时）

| 现象 | 根因 | 修法 |
|---|---|---|
| **点目录没反应（时好时坏）** | `base.css` 里 `html{scroll-behavior:smooth}`（英语版继承来的）；论文正文把页面撑到 **9 万像素**，Chrome 对这种超高页面的 CSS 平滑滚动**会卡住不动**（实测目标 21916px，scrollY 一直是 0；同一个目标用 `behavior:'instant'` 一次就到） | paper.css 里把 `html{scroll-behavior}` 改回 auto；`reader.js` 的 `jumpTo()` 按距离选：超过一屏半用 instant，近处才 smooth |
| pandoc 报 `unexpected end of input` | 遇到不认识的宏会一路吃到文件尾 | 抽取并就地展开自定义宏；再迭代收敛剩下的 |
| pandoc 报 `unexpected %` | `\label[appendix]{x}` 这类带可选参数的调用 | 剥掉可选参数 |
| 表全变成空行 | pandoc 的 Table 是 **6 段**结构（`[attr, caption, colspecs, head, bodies, foot]`），按 2 段读了 | 按 6 段读 |
| 图全丢了 | Figure 的 target 是**inline 节点列表**（Plain→Image），不是 `[url, title]` | 递归挖出 Image 的 url |
| Div 里的正文全没了 | pandoc 的 Div 是 `[attr, blocks]`（**2 段**），按 3 段读了 | 按 2 段读 |
| 参考文献 0 条 | `--citeproc` 没加；refs 是 csl-entry 的 Div 套 Div 套 Para | 加 `--citeproc` + 递归取文本 |
| `File ref.bib not found` | pandoc 按 cwd 找 .bib | 用绝对路径 + cwd 设为源码目录 |
| 公式全是原文 | 后台页签 rAF 停摆 | 分批调度改 setTimeout |
| 切句把公式切碎 | 占位符里的 LaTeX 有小数点 | 占位符用**独立哨兵字符**（不能和切句的 `.` 保护共用） |
| 「翻译整句」点了没反应 | 前端直连的 MyMemory 路径写错（多了 `/api/v1/` → 404） | 改走服务端 `/api/translate` |
| 播放没有声音 | edge-tts 403（微软端点不通） | 改用 macOS 自带 `say`，并转成 m4a 给 Chrome 播 |

---

## 和 shadow-ebook 的关系

```
shadow-ebook (~/shadow-learning)          Shadow Paper (~/shadow-paper)
  英语电子书跟读学习                       论文阅读
  EPUB/朗读/跟读评分/语法/AR值/家长端       arXiv+PDF/公式/图/目录/查词/生词本/笔记
        └── 查词 · SRS · 句切 · 前端壳 ──────┘  （复用）
```

两个项目**各自独立**，不共享数据库也不互相依赖；只有几个模块是按同一套语义搬过来的。

---

## 问这个库（论文库 + MCP）

> shadow-paper 不只是「给人读论文的」，也是**给 agent 问论文的**。
> 论文都在本地了，那也该让别的 agent 能直接问 —— 不然每次查都得人翻页面。

### 网页上问

书架页顶部有「问这个库」：只依据**库内论文原文**回答，每个结论标 `[篇号 / 章节]`，
点出处 chip 直接跳回原文那篇。**答不出就说答不出**，不给像模像样的假答案。

### 给别的 agent 用（MCP）

> 配置里要把 `<shadow-paper 仓库的绝对路径>` 换成你本机 clone 后的**绝对路径**
> （MCP 配置不支持 `~`，也不认相对路径）。

```json
{
  "mcpServers": {
    "shadow-paper": {
      "command": "<shadow-paper 仓库的绝对路径>/.venv/bin/python",
      "args": ["<shadow-paper 仓库的绝对路径>/mcp_server.py"],
      "env": { "MINIMAX_API_KEY": "..." }
    }
  }
}
```

stdio JSON-RPC（MCP 标准），和 `~/.semantix/mcp/server.mjs` 同一套写法。

| 工具 | 作用 | 花不花钱 |
|---|---|---|
| `list_papers()` | 库里有什么（篇号/标题/作者/词数/节数） | 不花 |
| `search_papers(query, limit, paper_id)` | BM25 检索 → 带出处的原文片段 | 不花 |
| `get_paper(paper_id, section)` | 取一篇（或某节）正文 | 不花 |
| `ask_library(question, top_k)` | 提问 → 基于原文的带引用答案 | **调一次模型** |

网页上的答案和 agent 拿到的答案**同源**（共用 `extensions/library.py` 的检索+取证内核）。

### 三条设计上的硬规矩

1. **不许编。** 检索不到就说检索不到；材料不足以回答就说不足 —— 宁可答「库里没有」。
2. **每个断言都要有出处。** 答案带 `[篇号 / 章节]`，能一键跳回原文。
3. **默认不调模型。** search / get 全是纯本地 BM25 + SQLite，零成本、确定性、离线可用。

### 检索是怎么做的（以及踩过的坑）

- **BM25（纯 Python，零第三方依赖）**，不用向量：论文问答里问题里的术语
  （方法名、指标名、benchmark）几乎总是**字面出现在原文**里，BM25 对术语精确匹配更准，
  也不需要 embedding 模型、不花钱、可复现。语义泛化是 semantix 那条线的活。
- **中文问句要先翻成英文检索词**：库里全是英文，BM25 是字面匹配，
  中文问句一个 token 都命中不了。`ask_library` 检测到中文（或零命中）时，
  先让模型翻一版检索词再搜 —— 只在这种情况下多花一次调用。
- **表格的数字在 `rows` 里、章节标题也要进索引**：不然「推理速度快多少」
  这种问题永远捞不到附录里那张 Clock-Time 表（表里的 token 是 `0.26` / `0.09`，
  跟「速度」一个词都对不上）。

### 中文版（2026-11-26 新增）

论文都在本地了，**中文读者/中文 agent 也该能直接用**。所以每篇都会生成一份中文版：

- **存在同一篇的 `zh_json` 字段里**，不是另建一篇 —— 书架不会出现重复条目，英文原文永远在、随时切回去对照。
- 阅读器顶栏有「**中文 / 原文**」开关（没有中文版的论文不显示这个按钮）。
- **检索层是中英同块索引**：每个块索引成「中文译文 + 英文原文」，所以
  中文提问命中中文部分、英文提问命中英文部分 —— 一次提问不必再翻检索词。
- **公式不译**：`⟦MATH:…⟧` 行内公式占位符在译文里原样保留，译文和原文的公式**逐个对齐**，
  点开放大看到的是同一式子。
  ⚠️ 校验比的是**多重集**不是顺序：中文语序和英文不同，模型把两个占位符对调位置很正常，
  占位符自带公式、逐个渲染，顺序不影响正确性（按顺序判会误杀好译文 → 整段变空）。
- **表格只译图注，表内数字和单元格一律不译** —— 数字不能被改，这是科普内容的底线。
- 术语全库统一（`extensions/zh.py` 里的术语表）：latent space→潜空间、
  continuous thought→连续思维、pseudo-reasoning→伪推理、inference latency→推理时延…

生成命令（带断点续跑，缓存在 `data/zh_cache/`）：

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from extensions import zh
print(zh.translate_all(['2412.06769','2506.09985','2604.12493',
                         '2608.13570','2512.21711','2603.09184']))
"
```

**三条质量硬规矩**（都在 `extensions/zh.py` 里自动校验）：

1. **公式占位符个数与顺序必须完全一致** —— 对不上就当这段译坏了，重译（最多 3 次）。
2. **数字不许增删** —— 译文多出原文没有的数字会被记成警告（人再看一眼）。
3. **章节语境不许进译文** —— 语境放在 system 消息里，不是和正文拼在一起
   （2026-11-26 实测踩中：拼在一起时模型把「（这一段所在的章节：总体结果）」也译了）。

工具链：`MiniMax-M3.1-Flash`（`SHADOW_PAPER_ZH_MODEL` 可改），
并发 6（`SHADOW_PAPER_ZH_CONCURRENCY` 可改）。6 篇 5.4 万词实测约 25–30 分钟。

### 导入这条路上修掉的坑（都在 `extensions/arxiv_import.py`）

1. **下载被截断 → 静默出废件。** arxiv.org 的 e-print / PDF 在弱网下会被截断，
   而 `tarfile.is_tarfile()` 只看文件头、照样 `True` → 解出一半 → 整篇论文被当成
   只有附录的论文导进去，**而且返回 success**。现在：分块 Range 下载 + 长度校验 +
   让 tar 走完整个归档，截断直接报错。
2. **主文件不能靠「最大的 .tex」挑。** 多文件论文里附录常比正文还长
   （`sec/7_suppl.tex` 20535 字节 > 正文 6317 字节）→ 挑中附录。
   现在认 `\documentclass`，并且 `download_source` 返回**解包根目录**
   （以前返回「最大的 .tex 所在目录」，把主文件搜索限死在子目录里，图也解析不到）。
3. **参考文献要可失败。** `\bibliography{xxx}` 会让 pandoc 去解析它写的那个 bib
   （不存在 / 语法读不了 → 整篇导入失败）。现在先把这条命令中和掉，
   bib 由 `--bibliography` 显式给；bib 读不了就退回不带 citeproc，正文照进。
4. **裸自定义宏不展开 → 整篇论文掉词（2026-11-26）。** 论文写
   `\newcommand{\ours}{\textsc{Coconut}\xspace}`，正文用 `\section{\ours: Chain of Continuous Thought}`
   这种**不带花括号**的用法 → pandoc 不认识 `\ours`，把它连同后面的内容一起吃掉 →
   方法名 "Coconut" 在全文消失，标题变成「: Chain of Continuous Thought」。
   现在带参、裸用两种宏都展开（裸用还要按后一个字符决定补不补空格）。
5. **pandoc 自己去啃本地 .sty → 题名块被搅乱（2026-11-26）。**
   `\usepackage{iclr2026_conference}` 指向本地 .sty 时 pandoc 会尝试解析它，
   解析到一半失败 → 正文开头多出一段「maketitle thanks aketitle」，还凭空多一节「正文」。
   现在把 .sty/.cls 里的 `\newcommand/\renewcommand` 搬进 tex，再把引用中和掉
   （系统包如 amsmath 原样留着，pandoc 找不到它们，无风险）。

> 4、5 这类 bug **不会报错**，只会安静地产出一篇残缺的论文。所以改完导入链路必须抽查三处：
> 方法名/专有名词的全文出现次数、小节标题有没有「凭空多冒号 / 缺首词」、正文开头有没有 LaTeX 残渣。

### 清洗器/翻译器改了之后，怎么把存量数据刷新（2026-11-26）

```bash
# 1) 先备份（重建要拿旧译文做复用，不能没有）
.venv/bin/python -c "
import sqlite3; src=sqlite3.connect('data/shadow-paper.db')
dst=sqlite3.connect('../shadow-paper-snapshots/pre-fix.db'); src.backup(dst)"

# 2) 重导（修完导入链路必须重导，否则库里还是旧的残缺版本）
.venv/bin/python tools/reimport_all.py

# 3) 重建中文版（原文变了哪段就重译哪段，没变的复用旧译文）
.venv/bin/python tools/rebuild_zh.py

# 4) 重建检索索引（中英同块索引的内容变了）
.venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from extensions import library; library.build_index(force=True)"
```

第 3 步的复用判据是**英文原文完全一致**才复用旧译文 —— 不是按位置对齐
（节数一变就全错位：2604.12493 删掉假「正文」节后，后面 52 节整体前移一位）。

### 中文版的质量体检（tools/fix_zh_quality.py）

翻译完**必须体检**，因为下面这些坑都不会报错，只会安静地产出脏内容：

| 症状 | 原因 | 现在的防线 |
|---|---|---|
| 译文里混进 `<think>…</think>` | 模型偶尔把思维链吐进 `content` | `_strip_think()` 剥掉，只留最后一段答案 |
| 译文等于原文 | 模型没翻 | `_looks_untranslated()` 判定失败 → 重译 |
| 整段译文变空 | 公式占位符校验太严（**按顺序**比，中文语序一换就判死） | 改成**多重集**比对；失败时兜底保留原文 |
| 方法名被意译（Coconut→椰子） | 标题提示词没写死 | 标题规则：方法名/系统名**原样保留英文，禁止意译** |
| 列表项没译 | `items` 数组不在「段落」分支里 | `segments()` 统一枚举要翻的单元 |
| 目录还是英文 | `toc` 是独立数组 | 组装时**按 id 复用小节译文**（零成本） |

```bash
.venv/bin/python tools/fix_zh_quality.py          # 只体检
.venv/bin/python tools/fix_zh_quality.py --fix    # 体检 + 只重译脏的
```

其他工具：

| 工具 | 干什么 |
|---|---|
| `tools/reimport_all.py` | 重导全部论文并报告内容变化（节数/标题/块数） |
| `tools/rebuild_zh.py` | 重建中文版（复用未变的译文，只重译真变了的） |
| `tools/fix_zh_quality.py` | 中文版质量体检 + 只重译脏的 |
| `tools/probe_llm_raw.py` | 看模型原始返回（排查思维链混入） |
| `tools/debug_import.py` | **导入失败时查完整 pandoc 报错**（只取 stderr 第一行会看不到原因） |
| `tools/diff_docs.py` | **导入前后逐段比对**，查「内容有没有悄悄少掉」 |

⚠️ 改完清洗器**必须重导全部论文并逐段比对**：
清洗器是全局的，只导新论文那篇不算数。而且**"没报错"不等于"没丢东西"** ——
实测清洗器连着三次把正文整段吃掉（注释里的宏调用吃后文、`\DeclareTextFontCommand`
重定义的宏吞掉实参、清理注释留下空行截断多行参数），全程不报任何错。
判据用「每段找最佳匹配、相似度低的才算丢」——**不能用"前 N 字是否原样出现"**，
因为修复本身会改段首（"Coconut" 找回来了），按前缀比会把「改好了」误判成「丢了」。
