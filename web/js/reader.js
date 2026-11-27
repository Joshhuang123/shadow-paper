/**
 * reader.js —— 论文阅读器逻辑
 *
 * 职责:
 *   1. 拉论文 → 渲染目录 + 正文(段落/公式/图/表/列表/引用)
 *   2. 公式渲染:KaTeX,CDN 不通时退回 <code> 原文
 *   3. 点词查义 → 弹窗(音标 + 释义)→ 加入生词本 / 翻译整句
 *   4. 右侧面板:生词本(状态桶 / 到期复习 / 词表)、笔记
 *   5. 阅读进度:滚动时记 section + 块序号,打开时跳回
 */
(function () {
    'use strict';

    var SP = window.SP;

    var state = {
        pid: '',
        paper: null,
        lang: 'zh',              // 当前显示语言：zh 中文版 / en 英文原文
        hasZh: false,            // 这篇有没有中文版
        paperLang: 'en',
        sections: [],          // {id, title, level, el, blocks:[]}
        tocItems: [],          // 目录按钮
        activeToc: null,
        lastSaved: '',         // 上次存进后端的进度,避免重复 POST
        restoring: true,       // 恢复进度期间不写进度(否则会把顶部位置存回去)
        paperReady: false,     // 正文已渲染(公式占位已就位)
        katexReady: false,     // KaTeX 已加载
        mathRendered: false,   // 公式已渲染过(只跑一次)
        progressRestored: false,
        dictWord: '',
        dictSentence: '',
        pendingNote: null      // 待保存的笔记 {text, section_id, block_idx}
    };

    var els = {};

    /* ==========================================================
       启动
       ========================================================== */

    function init() {
        els.article = document.getElementById('paper-body');
        els.content = document.getElementById('paper-content');
        els.title = document.getElementById('paper-title');
        els.authors = document.getElementById('paper-authors');
        els.tocList = document.getElementById('toc-list');
        els.tocPanel = document.getElementById('toc-panel');
        els.sidePanel = document.getElementById('side-panel');
        els.scrim = document.getElementById('scrim');
        els.modal = document.getElementById('dict-modal');
        els.modalWord = document.getElementById('dict-word');
        els.modalPhon = document.getElementById('dict-phon');
        els.modalBody = document.getElementById('dict-body');
        els.modalHint = document.getElementById('dict-hint');
        els.btnAdd = document.getElementById('dict-add');
        els.btnTranslate = document.getElementById('dict-translate');
        els.btnSpeak = document.getElementById('dict-speak');
        els.muteBtn = document.getElementById('mute-btn');
        els.formulaModal = document.getElementById('formula-modal');
        els.formulaBig = document.getElementById('formula-big');
        els.formulaSrc = document.getElementById('formula-src');
        els.formulaNum = document.getElementById('formula-num');
        els.formulaHint = document.getElementById('formula-hint');
        els.noteFab = document.getElementById('note-fab');
        els.noteForm = document.getElementById('note-form');
        els.noteQuote = document.getElementById('note-quote');
        els.noteText = document.getElementById('note-text');
        els.noteList = document.getElementById('note-list');
        els.vocabBuckets = document.getElementById('vocab-buckets');
        els.vocabList = document.getElementById('vocab-list');
        els.vocabDue = document.getElementById('vocab-due');
        els.reviewArea = document.getElementById('review-area');
        els.mathNote = document.getElementById('math-note');

        state.pid = parsePid();
        if (!state.pid) {
            renderFatal('地址不对:应该是 /read/<论文 id>');
            return;
        }

        // 点词查义
        els.article.addEventListener('click', onArticleClick);
        // 选中文本 → 浮出「记笔记」
        els.article.addEventListener('mouseup', onSelectionEnd);
        document.addEventListener('keydown', onKeydown);
        window.addEventListener('scroll', onScroll);
        window.addEventListener('resize', onScroll);

        syncMuteBtn();
        els.noteForm.addEventListener('submit', onNoteSubmit);
        els.noteText.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') cancelNote();
        });

        loadPaper();
        loadVocab();
        loadNotes();

        // KaTeX 动态加载:正文先用公式原文占位,KaTeX 到位后原地升级。
        //
        // 这里有个竞态必须处理:论文是异步拉的,KaTeX 也是异步加载的,
        // 谁先到是不确定的。如果 KaTeX 先到、正文还是空的,
        // renderMathInAsync 扫不到任何公式就结束了 —— 之后正文渲染出来,
        // 公式就永远停在 <code> 原文。所以两个条件都满足才渲染。
        SP.loadKatex(function (ok) {
            if (!ok) {
                showMathNote('没能加载 KaTeX(离线或 CDN 不通),公式暂时显示为 LaTeX 原文。');
                restoreProgress();
                return;
            }
            state.katexReady = true;
            maybeRenderMath();
        });
    }

    /** 正文和 KaTeX 都到位了 → 渲染公式。只跑一次。 */
    function maybeRenderMath() {
        if (!state.paperReady || !state.katexReady || state.mathRendered) return;
        state.mathRendered = true;
        SP.renderMathInAsync(els.article, function (n, undef) {
            // 后端只展开了 \newcommand 形式的自定义宏,
            // 剩下的(比如 \Y \F \M)在公式里按字面显示 —— 说清楚,别让读者以为是原文如此。
            if (undef && undef.length) {
                showMathNote('文中有 ' + undef.length + ' 个自定义宏('
                    + undef.map(function (u) { return '\\' + u; }).join(' ')
                    + ')没被后端展开,公式里按字面显示了。');
            }
            // 公式渲染完高度会变,重新定位一次阅读位置
            onScroll();
            // 必须在公式渲染完之后再跳回上次读的位置:
            // 公式从一行 LaTeX 变成渲染后的高度差得不少,
            // 先跳再渲染的话,落点会偏几千像素。
            restoreProgress();
        });
    }

    function parsePid() {
        var m = location.pathname.match(/\/read\/([^/?#]+)/);
        return m ? decodeURIComponent(m[1]) : '';
    }

    function renderFatal(msg) {
        // 只清正文容器:整个 article 里还有公式提示条
        SP.fill(els.content || els.article, [
            SP.el('p', { class: 'text-secondary', text: msg })
        ]);
    }

    /* ==========================================================
       载入 + 渲染论文
       ========================================================== */

    function loadPaper() {
        // ⚠️ 这里**不能**自己拼 `?lang=zh`（2026-11-26 实测，用户报「点了切换回不到原文」）：
        //   语言偏好存在服务端 cookie 里，但前端每次重载都把 state.lang 重置成 'zh'，
        //   于是请求永远带 `?lang=zh` → 服务端 q_lang 优先、**cookie 被忽略** →
        //   点「切换」写完 cookie、页面一刷新又强行拉中文，永远切不回原文。
        // 现在：**不带参数**（让服务端按 cookie 决定，没 cookie 默认中文），
        // 再用服务端返回的 lang 作为前端的真实状态。
        SP.getJSON('/api/paper/' + encodeURIComponent(state.pid)).then(function (d) {
            state.paper = d.paper;
            state.hasZh = !!d.has_zh;
            state.paperLang = d.lang || 'en';
            state.lang = state.paperLang;     // ← 以服务端返回为准
            renderHead(d.paper);
            renderToc(d.paper);
            renderBody(d.paper);
            syncLangBtn();
            state.paperReady = true;
            maybeRenderMath();
        }).catch(function (err) {
            renderFatal('打开失败:' + (err.message || '未知错误'));
        });
    }

    /** 顶栏 + 正文开头的题名区。 */
    function renderHead(paper) {
        // 中文版：主标题用中文（title_zh），英文原名降为副标题。
        // 英文原文：主标题就是英文。
        // ⚠ 论文标题是专有名词，所以**中文版里两个都要有** ——
        //   只剩中文会搜不到原论文，只剩英文又不算「中文版」。
        var isZh = paper.lang === 'zh';
        var titleZh = (paper.title_zh || '').trim();
        var mainTitle = (isZh && titleZh) ? titleZh : (paper.title || paper.id);
        var subTitle = (isZh && titleZh && paper.title) ? paper.title : '';

        els.title.textContent = mainTitle;
        document.title = mainTitle + ' · Shadow Paper';

        var authors = (paper.authors || []).join('、');
        els.authors.textContent = authors;

        var front = SP.el('div', { class: 'sp-paper-front' });
        front.appendChild(SP.el('h1', { text: mainTitle }));
        if (subTitle) {
            front.appendChild(SP.el('p', { class: 'sp-paper-title-en', text: subTitle }));
        }
        front.appendChild(SP.el('p', { class: 'sp-paper-authors-line', text: authors || '未标注作者' }));

        var venue = SP.el('p', { class: 'sp-paper-venue' });
        var bits = [];
        if (paper.venue) bits.push(paper.venue);
        if (paper.date) bits.push(paper.date);
        if (paper.source && paper.source.url) {
            bits.push('<a href="' + SP.esc(paper.source.url) + '" target="_blank" rel="noopener">原文链接</a>');
        }
        venue.innerHTML = bits.join(' · ');
        front.appendChild(venue);

        // 摘要：中文版优先用 abstract_zh，没有就退回英文摘要
        var abstract = (isZh && (paper.abstract_zh || '').trim())
            ? paper.abstract_zh : (paper.abstract || '');
        if (abstract) {
            var abs = SP.el('div', { class: 'sp-paper-abstract' });
            abs.appendChild(SP.el('span', { class: 'sp-eyebrow', text: isZh ? '摘要' : 'Abstract' }));
            abs.appendChild(SP.el('p', { style: 'margin:0' }));
            abs.lastChild.appendChild(SP.buildTextNodes(abstract));
            front.appendChild(abs);
        }

        // 清掉「正在载入论文…」占位,题名区放最前面
        SP.fill(els.content, []);
        els.content.appendChild(front);
    }

    /** 目录:按 level 缩进,点击平滑滚动。 */
    function renderToc(paper) {
        SP.fill(els.tocList, []);
        state.tocItems = [];

        (paper.toc || []).forEach(function (t) {
            var btn = SP.el('button', {
                class: 'sp-toc-item',
                type: 'button',
                'data-level': String(t.level || 1),
                text: t.title || '(无标题)'
            });
            btn.addEventListener('click', function () {
                gotoSection(t.id);
                closeDrawers();
            });
            els.tocList.appendChild(btn);
            state.tocItems.push({ id: t.id, btn: btn });
        });
    }

    /** 正文:section → blocks 顺序渲染。 */
    function renderBody(paper) {
        state.sections = [];

        var frag = document.createDocumentFragment();
        (paper.sections || []).forEach(function (sec) {
            var secEl = SP.el('section', {
                class: 'sp-sec',
                'data-section-id': sec.id
            });

            // level 1..6 → h2..h7。h1 留给论文标题,所以正文里从 h2 起。
            var tag = 'h' + Math.min(6, Math.max(2, (sec.level || 1) + 1));
            secEl.appendChild(SP.el(tag, { text: sec.title || '' }));

            var blocks = [];
            (sec.blocks || []).forEach(function (b, i) {
                var el = renderBlock(b, i);
                if (!el) return;
                el.setAttribute('data-block-idx', String(i));
                secEl.appendChild(el);
                blocks.push(el);
            });

            frag.appendChild(secEl);
            state.sections.push({
                id: sec.id,
                title: sec.title,
                level: sec.level || 1,
                el: secEl,
                blocks: blocks
            });
        });

        // 追加在题名区之后
        els.content.appendChild(frag);
    }

    /* ==========================================================
       块渲染
       ========================================================== */

    var QUOTE_LABELS = {
        theorem: '定理',
        lemma: '引理',
        definition: '定义',
        remark: '注',
        proof: '证明',
        corollary: '推论',
        proposition: '命题',
        example: '例',
        remark_: '注'
    };

    function renderBlock(b, idx) {
        switch (b.k) {
            case 'p': return renderPara(b);
            case 'eq': return renderEq(b);
            case 'fig': return renderFig(b);
            case 'table': return renderTable(b);
            case 'list': return renderList(b);
            case 'quote': return renderQuote(b);
            default: return null;
        }
    }

    function renderPara(b) {
        var p = SP.el('p');
        p.appendChild(SP.buildTextNodes(b.t || ''));
        return p;
    }

    /** 独立公式:居中 + 可选编号。 */
    function renderEq(b) {
        var wrap = SP.el('div', { class: 'sp-eq sp-eq-box' });
        var span = SP.el('span', {
            class: 'math-inline math-display',
            'data-latex': b.latex || '',
            'data-math': '1',
            'data-display': '1'
        });
        span.appendChild(SP.buildFallback(b.latex || ''));
        wrap.appendChild(span);
        if (b.num) {
            wrap.appendChild(SP.el('span', { class: 'sp-eq-num', text: '(' + b.num + ')' }));
        }
        return wrap;
    }

    /** 插图:src 已经是 "assets/xxx.png",要拼成 "/assets/xxx.png"。 */
    function renderFig(b) {
        var fig = SP.el('figure');
        var img = SP.el('img', {
            src: assetUrl(b.src),
            alt: b.caption || '论文插图',
            loading: 'lazy',
            decoding: 'async'
        });
        fig.appendChild(img);
        if (b.caption) {
            var cap = SP.el('figcaption');
            cap.appendChild(SP.buildTextNodes(b.caption));
            fig.appendChild(cap);
        }
        return fig;
    }

    function assetUrl(src) {
        if (!src) return '';
        if (/^https?:\/\//i.test(src)) return src;
        if (src.charAt(0) === '/') return src;
        return '/' + src;
    }

    /** 表格:第一行当表头,单元格里的公式也要还原。 */
    function renderTable(b) {
        var rows = b.rows || [];
        if (!rows.length) return null;

        var wrap = SP.el('div', { class: 'sp-table-wrap' });
        var table = SP.el('table');

        var head = SP.el('thead');
        var htr = SP.el('tr');
        (rows[0] || []).forEach(function (c) {
            var th = SP.el('th', { scope: 'col' });
            th.appendChild(SP.buildTextNodes(String(c == null ? '' : c)));
            htr.appendChild(th);
        });
        head.appendChild(htr);
        table.appendChild(head);

        var body = SP.el('tbody');
        rows.slice(1).forEach(function (row) {
            var tr = SP.el('tr');
            (row || []).forEach(function (c) {
                var td = SP.el('td');
                td.appendChild(SP.buildTextNodes(String(c == null ? '' : c)));
                tr.appendChild(td);
            });
            body.appendChild(tr);
        });
        table.appendChild(body);
        wrap.appendChild(table);

        if (b.caption) {
            var cap = SP.el('div', { class: 'sp-table-cap' });
            cap.appendChild(SP.buildTextNodes(b.caption));
            wrap.appendChild(cap);
        }
        return wrap;
    }

    function renderList(b) {
        var list = SP.el(b.ordered ? 'ol' : 'ul');
        (b.items || []).forEach(function (it) {
            var li = SP.el('li');
            li.appendChild(SP.buildTextNodes(String(it == null ? '' : it)));
            list.appendChild(li);
        });
        return list;
    }

    function renderQuote(b) {
        var bq = SP.el('blockquote');
        if (b.kind) {
            var label = QUOTE_LABELS[String(b.kind).toLowerCase()] || b.kind;
            bq.appendChild(SP.el('span', {
                class: 'sp-quote-label',
                'data-kind': String(b.kind).toLowerCase(),
                text: label
            }));
        }
        var p = SP.el('p', { style: 'margin:0' });
        p.appendChild(SP.buildTextNodes(b.t || ''));
        bq.appendChild(p);
        return bq;
    }

    function showMathNote(text) {
        els.mathNote.hidden = false;
        els.mathNote.textContent = text;
    }

    /* ==========================================================
       点词查义
       ========================================================== */

    function onArticleClick(e) {
        // 拖选完松手会触发 click,这时不该去查词
        var sel = window.getSelection();
        if (sel && !sel.isCollapsed) return;

        /* 点公式 → 开公式卡片(放大 + LaTeX 源码)。
           放在查词前面:公式里那些字母不是词,不该拿去查义。 */
        var mathEl = e.target && e.target.closest ? e.target.closest('.math-inline') : null;
        if (mathEl) {
            e.preventDefault();
            openFormula(mathEl);
            return;
        }

        var hit = SP.wordAtPoint(e.clientX, e.clientY);
        if (!hit) return;
        e.preventDefault();
        lookup(hit);
    }

    /* ==========================================================
       公式卡片:点公式 → 放大看 + 看 LaTeX 源码
       ----------------------------------------------------------
       为什么要有:论文里的公式是**信息密度最高**的部分,
       但行内公式挤在文字中间,放大看不了、源码也看不见。
       → 弹一张卡:上面放大渲染,下面是 LaTeX 原式(可复制)。
       ========================================================== */

    var stateFormula = { latex: '', num: '' };

    function openFormula(el) {
        var latex = el.getAttribute('data-latex') || '';
        if (!latex) return;
        stateFormula.latex = latex;
        var wrap = el.closest('.sp-eq');
        var numEl = wrap ? wrap.querySelector('.sp-eq-num') : null;
        stateFormula.num = numEl ? (numEl.textContent || '') : '';
        els.formulaNum.textContent = stateFormula.num;
        els.formulaSrc.textContent = latex;
        els.formulaHint.textContent = '';

        // 放大渲染:用 KaTeX 重新渲染一遍(displayMode),失败就把原来的搬过来
        SP.fill(els.formulaBig, []);
        var rendered = false;
        try {
            if (window.katex) {
                window.katex.render(latex, els.formulaBig, {
                    displayMode: true, throwOnError: true, strict: 'ignore', output: 'html'
                });
                rendered = true;
            }
        } catch (e) {
            rendered = false;
        }
        if (!rendered) {
            // KaTeX 没加载 / 这条公式 KaTeX 不认 → 退回原文显示,别空着
            els.formulaBig.appendChild(SP.el('code', { class: 'math-fallback', text: latex }));
            els.formulaHint.textContent = '这条公式没能渲染成图（KaTeX 没加载或不认这个写法），下面是 LaTeX 原文。';
        }
        els.formulaModal.hidden = false;
        els.formulaModal.setAttribute('aria-modal', 'true');
        var close = els.formulaModal.querySelector('.sp-modal-close');
        if (close) close.focus();
    }

    function closeFormula() {
        els.formulaModal.hidden = true;
    }

    function copyFormula() {
        var text = stateFormula.latex || '';
        if (!text) return;
        var done = function () { els.formulaHint.textContent = 'LaTeX 源码已复制。'; };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(function () {
                els.formulaHint.textContent = '复制失败，手动选中下面的源码复制。';
            });
        } else {
            els.formulaHint.textContent = '这个浏览器不让脚本复制，手动选中下面的源码复制。';
        }
    }

    /* ==========================================================
       朗读
       ----------------------------------------------------------
       「每个单词点了要能读出来」——
       点词查义的同时把词念一遍;弹窗里也有个「🔊 朗读」可以再念一遍。
       顶栏有开关(静音状态存 localStorage,关掉就只查义不读)。
       ========================================================== */

    function speakWord() {
        if (!state.dictWord) return;
        els.btnSpeak.disabled = true;
        SP.speak(state.dictWord).then(function () {
            els.btnSpeak.disabled = false;
        });
    }

    function toggleMute() {
        var muted = SP.setMuted(!SP.muted);
        syncMuteBtn();
        return muted;
    }

    function syncMuteBtn() {
        if (!els.muteBtn) return;
        els.muteBtn.classList.toggle('is-off', SP.muted);
        els.muteBtn.setAttribute('aria-pressed', SP.muted ? 'true' : 'false');
        els.muteBtn.title = SP.muted ? '点词朗读已关' : '点词朗读已开';
    }

    function lookup(hit) {
        var word = hit.word;
        if (!word || word.length < 2) return;

        state.dictWord = word.toLowerCase();
        state.dictSentence = sentenceFor(hit);

        openDict();
        els.modalWord.textContent = word;
        els.modalPhon.textContent = '';
        els.modalHint.textContent = '';
        els.btnAdd.disabled = true;
        els.btnTranslate.disabled = !state.dictSentence;
        SP.fill(els.modalBody, [
            SP.el('p', { class: 'sp-modal-msg', text: '查询中…' })
        ]);

        /* 「每个单词点了要能读出来」——查义的同时把词念一遍。
           静音开关在顶栏(SP.muted),关了就不读。 */
        SP.speak(word);

        SP.getJSON('/api/dict/' + encodeURIComponent(word)).then(function (d) {
            renderDict(d);
        }).catch(function (err) {
            renderDictError(err);
        });
    }

    /**
     * 查词失败要分三种情况说,不能笼统报「查询失败」:
     *   400 → 这个词不存在(多半是误点)
     *   429 → 查太快了
     *   503 → 词典上游连不上(网络问题,值得重试)
     */
    function renderDictError(err) {
        var text;
        if (err.status === 400) text = '这不是一个单词。';
        else if (err.status === 429) text = '查得太快啦,歇一下再查。';
        else if (err.status === 503) text = '词典连不上,检查一下网络。';
        else text = err.message || '查询失败。';

        SP.fill(els.modalBody, [
            SP.el('p', { class: 'sp-modal-msg is-error', text: text })
        ]);
        els.btnAdd.disabled = true;
        els.modalHint.textContent = err.status === 503 ? '这是网络问题,过一会儿再点同一个词试试。' : '';
    }

    function renderDict(d) {
        var meanings = d.meanings || [];
        if (!meanings.length) {
            SP.fill(els.modalBody, [
                SP.el('p', { class: 'sp-modal-msg', text: '没查到这个词的释义。' })
            ]);
            els.btnAdd.disabled = true;
            return;
        }

        els.modalPhon.textContent = d.phonetic ? '/' + d.phonetic + '/' : '';
        var ul = SP.el('ul', { class: 'sp-meanings' });
        meanings.forEach(function (m) {
            var li = SP.el('li', { class: 'sp-meaning' });
            li.appendChild(SP.el('span', { class: 'sp-meaning-part', text: m.part || '' }));
            li.appendChild(SP.el('p', { class: 'sp-meaning-cn', text: m.cn || '' }));
            ul.appendChild(li);
        });
        SP.fill(els.modalBody, [ul]);
        els.btnAdd.disabled = false;
        els.modalHint.textContent = d.source ? '释义来源:' + d.source : '';
    }

    /** 取点击词所在的那句话(公式不参与,否则句子里会混进 LaTeX)。 */
    function sentenceFor(hit) {
        var block = hit.node.parentElement &&
            hit.node.parentElement.closest('.sp-paper p, .sp-paper blockquote, .sp-paper li, .sp-paper td, .sp-paper th');
        if (!block) return '';
        var plain = SP.plainText(block);
        var abs = SP.plainOffset(plain, hit.node, hit.offset);
        if (abs < 0) return '';
        return SP.sentenceAround(plain.text, abs);
    }

    /* ---- 查词弹窗 ---- */

    function openDict() {
        els.modal.hidden = false;
        els.modal.setAttribute('aria-modal', 'true');
        var close = els.modal.querySelector('.sp-modal-close');
        if (close) close.focus();
    }

    function closeDict() {
        els.modal.hidden = true;
    }

    function addVocab() {
        if (!state.dictWord) return;
        els.btnAdd.disabled = true;
        SP.postJSON('/api/vocab/lookup', { word: state.dictWord }).then(function () {
            els.btnAdd.textContent = '已加入生词本';
            loadVocab();
        }).catch(function (err) {
            els.btnAdd.disabled = false;
            els.btnAdd.textContent = '加入生词本';
            els.modalHint.textContent = '加入失败:' + (err.message || '未知错误');
        });
    }

    function translateSentence() {
        if (!state.dictSentence) return;
        els.btnTranslate.disabled = true;
        els.btnTranslate.textContent = '翻译中…';
        /* 翻译方向按句子里的汉字自动判：
           中文论文 → 中译英；英文论文 → 英译中。
           （方向写死的话，拿它读中文论文时会把中文翻成中文。） */
        var hasCJK = /[一-鿿]/.test(state.dictSentence);
        var pair = hasCJK ? 'zh-CN|en' : 'en|zh-CN';
        SP.translate(state.dictSentence, pair).then(function (t) {
            els.btnTranslate.disabled = false;
            els.btnTranslate.textContent = '翻译整句';
            if (!t) {
                els.modalHint.textContent = '翻译服务暂时连不上。';
                return;
            }
            var box = SP.el('div', { class: 'sp-translation' });
            box.appendChild(SP.el('div', { class: 'sp-eyebrow', text: '整句翻译 · ' + (hasCJK ? '中→英' : '英→中') }));
            box.appendChild(SP.el('p', { style: 'margin:6px 0 0', text: t }));
            els.modalBody.appendChild(box);
        });
    }

    /* ==========================================================
       生词本面板
       ========================================================== */

    function loadVocab() {
        Promise.all([
            SP.getJSON('/api/vocab/stats').catch(function () { return null; }),
            SP.getJSON('/api/vocab/words').catch(function () { return null; }),
            SP.getJSON('/api/vocab/review-queue?limit=20').catch(function () { return null; })
        ]).then(function (r) {
            renderBuckets(r[0]);
            renderWords(r[1]);
            renderQueue(r[2]);
        });
    }

    var BUCKETS = [
        { key: 'learning', label: '学习中' },
        { key: 'practicing', label: '巩固' },
        { key: 'familiar', label: '熟悉' },
        { key: 'mastered', label: '掌握' }
    ];

    function renderBuckets(stats) {
        SP.fill(els.vocabBuckets, []);
        if (!stats) return;
        BUCKETS.forEach(function (b) {
            var box = SP.el('div', { class: 'sp-bucket', 'data-state': b.key });
            box.appendChild(SP.el('div', { class: 'sp-bucket-n', text: String(stats[b.key] || 0) }));
            box.appendChild(SP.el('div', { class: 'sp-bucket-l', text: b.label }));
            els.vocabBuckets.appendChild(box);
        });
        els.vocabDue.textContent = stats.due_now ? '(' + stats.due_now + ' 个到期)' : '';
    }

    var STATE_LABELS = {
        learning: '学习中', practicing: '巩固', familiar: '熟悉', mastered: '掌握'
    };

    function renderWords(d) {
        SP.fill(els.vocabList, []);
        var words = (d && d.words) || [];
        if (!words.length) {
            els.vocabList.appendChild(SP.el('li', {
                class: 'sp-panel-empty',
                text: '还没有生词。读论文时点一下英文单词,再点「加入生词本」就会出现在这里。'
            }));
            return;
        }
        words.forEach(function (w) {
            var li = SP.el('li', { class: 'sp-word-row' });
            li.appendChild(SP.el('span', { class: 'sp-word-text', text: w.word }));
            li.appendChild(SP.el('span', {
                class: 'sp-word-state',
                'data-state': w.state || 'learning',
                text: STATE_LABELS[w.state] || w.state
            }));
            var del = SP.el('button', {
                class: 'sp-word-del',
                type: 'button',
                title: '从生词本移除',
                'aria-label': '移除 ' + w.word
            }, [SP.el('span', { class: 'ico ico-trash', 'aria-hidden': 'true' })]);
            del.addEventListener('click', function () { removeWord(w.word); });
            li.appendChild(del);
            els.vocabList.appendChild(li);
        });
    }

    function removeWord(word) {
        SP.postJSON('/api/vocab/remove', { word: word }).then(function () {
            loadVocab();
        }).catch(function (err) {
            els.vocabDue.textContent = '删除失败:' + (err.message || '');
        });
    }

    function renderQueue(d) {
        SP.fill(els.reviewArea, []);
        var queue = (d && d.queue) || [];
        if (!queue.length) return;

        var card = SP.el('div', { class: 'sp-review-card' });
        card.appendChild(SP.el('p', { class: 'sp-review-word', text: queue[0].word }));
        card.appendChild(SP.el('p', {
            class: 'sp-review-sub',
            text: '到期复习 · ' + (STATE_LABELS[queue[0].state] || queue[0].state)
                + (queue.length > 1 ? ' · 还有 ' + (queue.length - 1) + ' 个' : '')
        }));
        var actions = SP.el('div', { class: 'sp-review-actions' });
        var yes = SP.el('button', { class: 'btn btn-primary', type: 'button', text: '认识' });
        var no = SP.el('button', { class: 'btn btn-secondary', type: 'button', text: '不认识' });
        yes.addEventListener('click', function () { reviewWord(queue[0].word, true); });
        no.addEventListener('click', function () { reviewWord(queue[0].word, false); });
        actions.appendChild(yes);
        actions.appendChild(no);
        card.appendChild(actions);
        els.reviewArea.appendChild(card);
    }

    function reviewWord(word, correct) {
        SP.postJSON('/api/vocab/review', { word: word, correct: !!correct }).then(function () {
            loadVocab();
        }).catch(function (err) {
            els.vocabDue.textContent = '复习失败:' + (err.message || '');
        });
    }

    /* ==========================================================
       笔记
       ========================================================== */

    function loadNotes() {
        SP.getJSON('/api/paper/' + encodeURIComponent(state.pid) + '/notes').then(function (d) {
            renderNotes(d.notes || []);
        }).catch(function () {
            SP.fill(els.noteList, [
                SP.el('li', { class: 'sp-panel-empty', text: '笔记读不出来。' })
            ]);
        });
    }

    function renderNotes(notes) {
        SP.fill(els.noteList, []);
        if (!notes.length) {
            els.noteList.appendChild(SP.el('li', {
                class: 'sp-panel-empty',
                text: '还没有笔记。选中正文里的一段话,就会出现「记笔记」按钮。'
            }));
            return;
        }
        notes.forEach(function (n) {
            var li = SP.el('li', { class: 'sp-note-item' });
            if (n.section_id) {
                var anchor = SP.el('button', {
                    class: 'sp-note-anchor',
                    type: 'button',
                    text: sectionTitle(n.section_id)
                });
                anchor.addEventListener('click', function () {
                    gotoSection(n.section_id);
                    closeDrawers();
                });
                li.appendChild(anchor);
            }
            li.appendChild(SP.el('p', { class: 'sp-note-text', text: n.text }));
            li.appendChild(SP.el('p', { class: 'sp-note-time', text: SP.fmtDateTime(n.created_at) }));
            els.noteList.appendChild(li);
        });
    }

    function sectionTitle(sid) {
        for (var i = 0; i < state.sections.length; i++) {
            if (state.sections[i].id === sid) return state.sections[i].title;
        }
        return sid;
    }

    /** 选中文本后,把「记笔记」按钮摆到选区旁边。 */
    function onSelectionEnd() {
        var sel = window.getSelection();
        if (!sel || sel.isCollapsed || !sel.rangeCount) {
            els.noteFab.classList.remove('is-on');
            return;
        }
        var range = sel.getRangeAt(0);
        if (!els.article.contains(range.commonAncestorContainer)) {
            els.noteFab.classList.remove('is-on');
            return;
        }
        var rect = range.getBoundingClientRect();
        if (!rect.width && !rect.height) {
            els.noteFab.classList.remove('is-on');
            return;
        }
        els.noteFab.style.left = Math.round(rect.left + rect.width / 2) + 'px';
        els.noteFab.style.top = Math.max(4, Math.round(rect.top) - 38) + 'px';
        els.noteFab.classList.add('is-on');
        state.pendingNote = {
            text: sel.toString().trim(),
            section_id: sectionOf(range.startContainer),
            block_idx: blockOf(range.startContainer)
        };
    }

    function sectionOf(node) {
        var el = node.nodeType === 3 ? node.parentElement : node;
        var sec = el && el.closest('[data-section-id]');
        return sec ? sec.getAttribute('data-section-id') : '';
    }

    function blockOf(node) {
        var el = node.nodeType === 3 ? node.parentElement : node;
        var blk = el && el.closest('[data-block-idx]');
        return blk ? parseInt(blk.getAttribute('data-block-idx'), 10) || 0 : 0;
    }

    function startNote() {
        if (!state.pendingNote || !state.pendingNote.text) return;
        els.noteQuote.textContent = state.pendingNote.text.length > 300
            ? state.pendingNote.text.slice(0, 300) + '…'
            : state.pendingNote.text;
        els.noteText.value = '';
        els.noteForm.hidden = false;
        els.noteFab.classList.remove('is-on');
        window.getSelection().removeAllRanges();
        // 窄屏上把面板拉出来,不然用户点了按钮看不到输入框
        if (window.innerWidth <= 1180) openPanel();
        els.noteText.focus();
    }

    function cancelNote() {
        els.noteForm.hidden = true;
        els.noteText.value = '';
    }

    function onNoteSubmit(e) {
        e.preventDefault();
        var text = els.noteText.value.trim();
        if (!text) return;
        var pending = state.pendingNote || { text: '', section_id: '', block_idx: 0 };

        SP.postJSON('/api/paper/' + encodeURIComponent(state.pid) + '/notes', {
            text: text,
            section_id: pending.section_id || '',
            block_idx: pending.block_idx || 0
        }).then(function () {
            cancelNote();
            loadNotes();
        }).catch(function (err) {
            els.noteQuote.textContent = '保存失败:' + (err.message || '未知错误');
        });
    }

    /* ==========================================================
       阅读进度
       ========================================================== */

    /** 当前阅读线(视口顶部往下一点点)落在哪个 section / 第几块。 */
    function currentPosition() {
        var line = window.scrollY + 140;
        var sec = null;
        for (var i = 0; i < state.sections.length; i++) {
            var top = state.sections[i].el.getBoundingClientRect().top + window.scrollY;
            if (top <= line) sec = state.sections[i];
            else break;
        }
        if (!sec) return { section_id: '', block_idx: 0 };

        var blockIdx = 0;
        for (var j = 0; j < sec.blocks.length; j++) {
            var btop = sec.blocks[j].getBoundingClientRect().top + window.scrollY;
            if (btop <= line) blockIdx = j;
            else break;
        }
        return { section_id: sec.id, block_idx: blockIdx };
    }

    var onScroll = SP.throttle(function () {
        var pos = currentPosition();

        // 目录高亮
        if (pos.section_id) {
            state.tocItems.forEach(function (t) {
                var on = t.id === pos.section_id;
                t.btn.classList.toggle('is-active', on);
                if (on) state.activeToc = t.btn;
            });
        }

        if (state.restoring) return;

        // 只有真的变了才发请求 —— 滚动事件每秒能来几十次
        var key = pos.section_id + ':' + pos.block_idx;
        if (key === state.lastSaved || !pos.section_id) return;
        state.lastSaved = key;
        SP.postJSON('/api/paper/' + encodeURIComponent(state.pid) + '/progress', {
            section_id: pos.section_id,
            block_idx: pos.block_idx
        }).catch(function () { /* 进度存不上不该打扰用户 */ });
    }, 400);

    /**
     * 滚到某个元素。
     *
     * ⚠ 为什么不能无脑用 window.scrollTo({behavior:'smooth'})：
     *   base.css 里有 `html { scroll-behavior: smooth }`（从英语版继承来的），
     *   论文正文把页面撑到 **9 万多像素**，Chrome 对这种超高页面的
     *   **CSS 平滑滚动会卡住不动** ——
     *   实测：目标 21916px，scrollY 一直停在 0（同一目标用 behavior:'instant' 一次就到）。
     *   → 而且就算能滚，平滑滚过两万像素要好几秒，读者会晕。
     *   → 策略：距离超过一屏半就用**瞬时跳转**，近处才用平滑。
     *   → 配合 paper.css 里把 `html{scroll-behavior}` 改回 auto，双保险。
     */
    function jumpTo(el, offset) {
        if (!el) return;
        var off = offset == null ? 80 : offset;
        var top = Math.max(0, el.getBoundingClientRect().top + window.scrollY - off);
        var far = Math.abs(top - window.scrollY) > window.innerHeight * 1.5;
        window.scrollTo({ top: top, behavior: far ? 'instant' : 'smooth' });
    }

    function gotoSection(sid) {
        var sec = null;
        for (var i = 0; i < state.sections.length; i++) {
            if (state.sections[i].id === sid) { sec = state.sections[i]; break; }
        }
        if (!sec) return;
        jumpTo(sec.el);
    }

    /** 打开时跳回上次读到的地方。 */
    function restoreProgress() {
        if (state.progressRestored) return;
        state.progressRestored = true;

        SP.getJSON('/api/paper/' + encodeURIComponent(state.pid) + '/progress').then(function (d) {
            var p = d.progress;
            if (!p || !p.section_id) { state.restoring = false; return; }

            var sec = null;
            for (var i = 0; i < state.sections.length; i++) {
                if (state.sections[i].id === p.section_id) { sec = state.sections[i]; break; }
            }
            if (!sec) { state.restoring = false; return; }

            var target = sec.blocks[Math.min(p.block_idx || 0, sec.blocks.length - 1)] || sec.el;

            setTimeout(function () {
                /* ⚠ behavior:'auto' 是「跟随 CSS」而不是「瞬时」——
                   CSS 里 html{scroll-behavior:smooth} 时它会变成平滑滚动，
                   超高页面上就可能卡住不动。统一走 jumpTo。 */
                jumpTo(target);
                state.lastSaved = p.section_id + ':' + (p.block_idx || 0);
                state.restoring = false;
                onScroll();
            }, 80);
        }).catch(function () {
            state.restoring = false;
        });
    }

    /* ==========================================================
       抽屉(窄屏)
       ========================================================== */

    function openToc() {
        els.sidePanel.classList.remove('is-open');
        els.tocPanel.classList.add('is-open');
        els.scrim.classList.add('is-on');
    }

    function openPanel() {
        els.tocPanel.classList.remove('is-open');
        els.sidePanel.classList.add('is-open');
        els.scrim.classList.add('is-on');
    }

    function closeDrawers() {
        els.tocPanel.classList.remove('is-open');
        els.sidePanel.classList.remove('is-open');
        els.scrim.classList.remove('is-on');
    }

    function toggleToc() {
        if (els.tocPanel.classList.contains('is-open')) closeDrawers();
        else openToc();
    }

    function togglePanel() {
        if (els.sidePanel.classList.contains('is-open')) closeDrawers();
        else openPanel();
    }

    /* ==========================================================
       键盘
       ========================================================== */

    /**
     * a11y.js 的 MODAL_IDS 是硬编码的英语版弹窗列表,不认识我们的查词弹窗,
     * 所以 ESC 在这里自己处理。
     */
    function onKeydown(e) {
        if (e.key !== 'Escape') return;
        if (!els.formulaModal.hidden) {
            e.preventDefault();
            closeFormula();
            return;
        }
        if (!els.modal.hidden) {
            e.preventDefault();
            closeDict();
            return;
        }
        if (els.tocPanel.classList.contains('is-open') ||
            els.sidePanel.classList.contains('is-open')) {
            closeDrawers();
        }
    }

    /* ==========================================================
       动作(dispatcher.js 路由)
       ========================================================== */

    /** 语言开关：中文版 / 英文原文。切换时整篇重取（服务端按 lang 返回对应版本）。 */
    function syncLangBtn() {
        // 两个入口（顶栏 + 底部条）一起同步：窄屏时顶栏那个会被响应式藏掉
        var btns = document.querySelectorAll('.sp-lang-btn');
        if (!btns.length) return;
        for (var i = 0; i < btns.length; i++) {
            var btn = btns[i];
            if (!state.hasZh) { btn.hidden = true; continue; }
            btn.hidden = false;
            var lab = btn.querySelector('.sp-lang-label');
            // ⚠️ 按钮上的字 = **点下去会得到什么**，不是「现在是什么」：
            //   显示当前语言的话，正在读中文时按钮写着「中文」，
            //   点一下却变成英文 —— 字和结果对不上，用户会以为按钮坏了。
            if (lab) lab.textContent = state.lang === 'zh' ? '原文' : '中文';
            btn.setAttribute('aria-label', state.lang === 'zh' ? '切到英文原文' : '切到中文版');
            btn.setAttribute('title', state.lang === 'zh' ? '切到英文原文' : '切到中文版');
        }
    }

    function toggleLang() {
        if (!state.hasZh) return;
        state.lang = state.lang === 'zh' ? 'en' : 'zh';
        // 偏好记在服务端 cookie 里：跨论文记住，而且不依赖前端拼 URL
        // （前端加 ?lang= 会被浏览器 JS 缓存卡住 —— 改完 reader.js 还在跑旧版）。
        SP.postJSON('/api/paper/lang', { lang: state.lang })
            .catch(function () {})
            .then(function () { window.location.reload(); });
    }

    window.toggleLang = toggleLang;
    window.toggleToc = toggleToc;
    window.togglePanel = togglePanel;
    window.closeDrawers = closeDrawers;
    window.closeDict = closeDict;
    window.addVocab = addVocab;
    window.translateSentence = translateSentence;
    window.speakWord = speakWord;
    window.toggleMute = toggleMute;
    window.openFormula = openFormula;
    window.closeFormula = closeFormula;
    window.copyFormula = copyFormula;
    window.startNote = startNote;
    window.cancelNote = cancelNote;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
