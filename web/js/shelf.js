/**
 * shelf.js —— 书架页逻辑
 *
 * 职责:
 *   1. 拉论文列表并渲染成卡片
 *   2. arXiv 编号导入 / PDF 上传导入,带可见的进度反馈
 *   3. 删除论文
 *
 * 所有按钮走 dispatcher.js 的 data-action 路由(见 web/js/dispatcher.js),
 * 所以这里的动作函数要挂到 window 上。
 */
(function () {
    'use strict';

    var SP = window.SP;

    var els = {};
    var busy = false;          // 导入中,防止重复提交

    /* ==========================================================
       列表
       ========================================================== */

    // 语言偏好（服务端按 cookie 判定：默认中文）。
    // 中文版时卡片主标题用中文，英文原名降为副标题。
    var shelfLang = 'zh';

    function renderPapers(papers) {
        SP.fill(els.grid, []);

        if (!papers.length) {
            els.empty.hidden = false;
            els.grid.hidden = true;
            els.count.textContent = '';
            return;
        }
        els.empty.hidden = true;
        els.grid.hidden = false;
        els.count.textContent = papers.length + ' 篇';

        papers.forEach(function (p) {
            els.grid.appendChild(paperCard(p));
        });
    }

    function paperCard(p) {
        var card = SP.el('button', { class: 'sp-paper-card', type: 'button' });

        // 卡片本身是「打开这篇论文」的入口 → 整个卡片可点。
        card.addEventListener('click', function (ev) {
            // 点删除按钮时不要顺手打开论文
            if (ev.target.closest('.sp-paper-del')) return;
            window.location.href = '/read/' + encodeURIComponent(p.id);
        });

        var zhTitle = (p.title_zh || '').trim();
        var showZh = shelfLang === 'zh' && !!zhTitle;
        card.appendChild(SP.el('h3', {
            class: 'sp-paper-title',
            text: showZh ? zhTitle : (p.title || p.id)
        }));
        if (showZh) {
            card.appendChild(SP.el('p', { class: 'sp-paper-title-en', text: p.title || p.id }));
        }

        var authors = (p.authors || []).join('、');
        card.appendChild(SP.el('p', {
            class: 'sp-paper-authors',
            text: authors || '未标注作者'
        }));

        var meta = SP.el('div', { class: 'sp-paper-meta' });
        if (p.has_zh) {
            // 中文版标记：这篇已经有中文版，点进去可以切中文/原文
            meta.appendChild(SP.el('span', {
                class: 'sp-paper-zh',
                title: '这篇有中文版，进入后可切换中文/原文'
            }, ['中文版']));
        }
        meta.appendChild(SP.el('span', { html: '<b>' + SP.fmtInt(p.n_sections) + '</b> 节' }));
        meta.appendChild(SP.el('span', { html: '<b>' + SP.fmtInt(p.n_words) + '</b> 词' }));
        meta.appendChild(SP.el('span', { html: '<b>' + SP.fmtInt(p.n_blocks) + '</b> 块' }));
        meta.appendChild(SP.el('span', {
            text: (p.source_type === 'pdf' ? 'PDF' : 'arXiv') + ' · ' + SP.fmtDate(p.imported_at)
        }));
        card.appendChild(meta);

        var del = SP.el('button', {
            class: 'sp-paper-del',
            type: 'button',
            title: '删除这篇论文',
            'aria-label': '删除《' + (p.title || p.id) + '》'
        }, [SP.el('span', { class: 'ico ico-trash', 'aria-hidden': 'true' })]);
        del.addEventListener('click', function (ev) {
            ev.stopPropagation();
            deletePaper(p);
        });
        card.appendChild(del);

        return card;
    }

    function loadPapers() {
        return SP.getJSON('/api/papers').then(function (d) {
            shelfLang = d.lang || 'zh';
            renderPapers(d.papers || []);
        }).catch(function (err) {
            showStatus('读取论文列表失败', err.message, 'error');
        });
    }

    /* ==========================================================
       导入状态
       ========================================================== */

    /**
     * @param {string} title   标题
     * @param {string} detail  细节(耗时/百分比)
     * @param {string} kind    'run' | 'ok' | 'error'
     */
    function showStatus(title, detail, kind) {
        els.status.hidden = false;
        els.status.classList.remove('is-error', 'is-ok');
        els.statusTitle.textContent = title;
        els.statusDetail.textContent = detail || '';

        if (kind === 'error') {
            els.status.classList.add('is-error');
            els.spinner.hidden = true;
            els.bar.hidden = true;
        } else if (kind === 'ok') {
            els.status.classList.add('is-ok');
            els.spinner.hidden = true;
            els.bar.hidden = true;
        } else {
            els.spinner.hidden = false;
            els.bar.hidden = false;
        }
    }

    function hideStatus() {
        els.status.hidden = true;
    }

    /** 上传百分比已知时把进度条变成确定进度。 */
    function setProgress(ratio) {
        els.bar.classList.remove('is-indeterminate');
        els.bar.firstElementChild.style.width = Math.round(ratio * 100) + '%';
    }

    function resetProgress() {
        els.bar.classList.add('is-indeterminate');
        els.bar.firstElementChild.style.width = '';
    }

    /** 开始一次导入:禁用按钮,起一个计时器报 elapsed。 */
    function beginImport(title) {
        busy = true;
        els.btnArxiv.disabled = true;
        els.btnPdf.disabled = true;
        els.input.disabled = true;
        resetProgress();
        showStatus(title, '准备中…', 'run');

        var t0 = Date.now();
        var timer = setInterval(function () {
            var sec = Math.round((Date.now() - t0) / 1000);
            els.statusDetail.textContent =
                '已用 ' + sec + ' 秒 · arXiv 导入要下载源码、解析 LaTeX、抽公式和插图,通常 30–90 秒,请不要关闭页面。';
        }, 1000);
        return { t0: t0, timer: timer };
    }

    function endImport() {
        busy = false;
        els.btnArxiv.disabled = false;
        els.btnPdf.disabled = false;
        els.input.disabled = false;
    }

    /* ==========================================================
       arXiv 导入
       ========================================================== */

    /**
     * 规整 arXiv 输入。用户可能粘一整条 URL,也可能带版本号(v1/v2)。
     * 后端自己也能用正则认,但先在前端规整一下,能把「要导入什么」明确回显给用户。
     */
    function normalizeArxiv(raw) {
        var s = String(raw || '').trim();
        if (!s) return '';
        var m = s.match(/(\d{4}\.\d{4,5})(v\d+)?/);
        if (m) return m[1];
        var m2 = s.match(/([a-zA-Z-]+(?:\.[A-Z]{2})?\/\d{7})/);
        if (m2) return m2[1];
        return s;
    }

    function importArxiv() {
        if (busy) return;
        var id = normalizeArxiv(els.input.value);
        if (!id) {
            showStatus('请先填 arXiv 编号', '例如 2611.05417', 'error');
            els.input.focus();
            return;
        }

        var run = beginImport('正在导入 arXiv:' + id);
        SP.upload('/api/paper/import/arxiv', {
            json: { id: id },
            timeout: 300000
        }).then(function (d) {
            clearInterval(run.timer);
            var st = d.stats || {};
            showStatus('导入成功:' + (d.title || id),
                '共 ' + SP.fmtInt(st.blocks) + ' 块、' + SP.fmtInt(st.words) + ' 词、'
                + SP.fmtInt(st.figures) + ' 张图 · 用时 '
                + Math.round((Date.now() - run.t0) / 1000) + ' 秒', 'ok');
            els.input.value = '';
            endImport();
            return loadPapers();
        }).catch(function (err) {
            clearInterval(run.timer);
            endImport();
            // 后端返回的 error 原文直接显示 —— 它比前端编的提示准得多
            // (比如「认不出 arXiv 编号」「导入太频繁,30 秒后再试」)
            showStatus('导入失败', err.message || '未知错误', 'error');
        });
    }

    /* ==========================================================
       PDF 导入
       ========================================================== */

    function pickPdf() {
        if (busy) return;
        els.pdfInput.click();
    }

    function importPdf(file) {
        if (busy) return;
        if (!file) return;
        if (!/\.pdf$/i.test(file.name) && file.type !== 'application/pdf') {
            showStatus('不是 PDF 文件', '只接受 .pdf 文件', 'error');
            return;
        }

        var form = new FormData();
        form.append('file', file);

        var run = beginImport('正在上传:' + file.name);
        SP.upload('/api/paper/import/pdf', {
            form: form,
            timeout: 600000,
            onProgress: function (loaded, total) {
                var pct = total ? Math.round(loaded / total * 100) : 0;
                setProgress(loaded / (total || 1));
                els.statusDetail.textContent = '上传中 ' + pct + '%（' +
                    SP.fmtInt(Math.round(loaded / 1024)) + ' / ' +
                    SP.fmtInt(Math.round(total / 1024)) + ' KB）';
            }
        }).then(function (d) {
            clearInterval(run.timer);
            var st = d.stats || {};
            showStatus('导入成功:' + (d.title || file.name),
                '共 ' + SP.fmtInt(st.blocks) + ' 块、' + SP.fmtInt(st.words) + ' 词 · 用时 '
                + Math.round((Date.now() - run.t0) / 1000) + ' 秒', 'ok');
            endImport();
            return loadPapers();
        }).catch(function (err) {
            clearInterval(run.timer);
            endImport();
            showStatus('导入失败', err.message || '未知错误', 'error');
        });
    }

    /* ==========================================================
       删除
       ========================================================== */

    function deletePaper(p) {
        if (!window.confirm('删除《' + (p.title || p.id) + '》？\n阅读进度和笔记会一起删掉,不可恢复。')) return;
        SP.request('/api/paper/' + encodeURIComponent(p.id), { method: 'DELETE' })
            .then(function () {
                hideStatus();
                return loadPapers();
            })
            .catch(function (err) {
                showStatus('删除失败', err.message || '未知错误', 'error');
            });
    }

    /* ==========================================================
       启动
       ========================================================== */

    function init() {
        els.grid = document.getElementById('paper-grid');
        els.empty = document.getElementById('shelf-empty');
        els.count = document.getElementById('shelf-count');
        els.input = document.getElementById('arxiv-input');
        els.pdfInput = document.getElementById('pdf-input');
        els.status = document.getElementById('import-status');
        els.statusTitle = document.getElementById('import-status-title');
        els.statusDetail = document.getElementById('import-status-detail');
        els.spinner = document.getElementById('import-spinner');
        els.bar = document.getElementById('import-bar');
        els.btnArxiv = document.querySelector('[data-action="shelfImportArxiv"]');
        els.btnPdf = document.querySelector('[data-action="shelfPickPdf"]');
        els.askInput = document.getElementById('ask-input');
        els.askOut = document.getElementById('ask-out');
        els.askBtn = document.querySelector('[data-action="shelfAsk"]');

        els.askInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); askQuestion(); }
        });

        // 回车直接导入
        els.input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); importArxiv(); }
        });
        els.pdfInput.addEventListener('change', function () {
            var f = els.pdfInput.files && els.pdfInput.files[0];
            els.pdfInput.value = '';       // 允许连续选同一个文件
            importPdf(f);
        });

        loadPapers();
    }


    /* ==========================================================
       问这个库
       ========================================================== */


    /* 答案里模型会写 **粗体**、还有换行和列表。
       这里做一层轻量渲染：先转义、再把 **…** 变成 <strong>，其余保留换行。
       不引 Markdown 库（这个仓是零前端依赖的）。 */
    function renderAnswer(text) {
        var frag = document.createDocumentFragment();
        var esc = String(text)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        var parts = esc.split(/(\*\*[^*]+\*\*)/g);
        parts.forEach(function (p) {
            if (/^\*\*[^*]+\*\*$/.test(p)) {
                frag.appendChild(SP.el('strong', {}, [p.slice(2, -2)]));
            } else if (p) {
                frag.appendChild(document.createTextNode(p));
            }
        });
        return frag;
    }

    function renderAsk(res) {
        SP.fill(els.askOut, []);

        if (res.error && !res.answer) {
            els.askOut.appendChild(SP.el('div', { class: 'sp-ask-note' },
                ['检索/生成失败：' + res.error]));
            els.askOut.hidden = false;
            return;
        }

        els.askOut.appendChild(SP.el('div', { class: 'sp-ask-body' },
            [renderAnswer(res.answer || '（没有正文）')]));

        if (res.citations && res.citations.length) {
            var cites = SP.el('div', { class: 'sp-ask-cites' });
            res.citations.forEach(function (c) {
                var chip = SP.el('button', {
                    class: 'sp-ask-cite', type: 'button',
                    title: c.title || ''
                }, [(c.paper_id || '') + ' · ' + (c.section || '')]);
                chip.addEventListener('click', function () {
                    window.location.href = '/read/' + encodeURIComponent(c.paper_id);
                });
                cites.appendChild(chip);
            });
            els.askOut.appendChild(cites);
        }

        var note = '检索到 ' + (res.passages ? res.passages.length : 0) + ' 段原文' +
                   (res.used_llm ? '，答案由模型基于这些原文生成' : '（未调用模型，只给原文片段）');
        els.askOut.appendChild(SP.el('div', { class: 'sp-ask-note' }, [note]));

        els.askOut.hidden = false;
    }

    function askQuestion() {
        var q = (els.askInput.value || '').trim();
        if (!q) return;
        els.askBtn.disabled = true;
        els.askOut.hidden = false;
        SP.fill(els.askOut, []);
        els.askOut.appendChild(SP.el('div', { class: 'sp-ask-note' }, ['正在检索并作答…']));

        SP.postJSON('/api/ask', { question: q, top_k: 8 })
            .then(renderAsk)
            .catch(function (e) {
                els.askOut.appendChild(SP.el('div', { class: 'sp-ask-note' },
                    ['问库失败：' + (e && e.message ? e.message : e)]));
            })
            .then(function () { els.askBtn.disabled = false; });
    }

    // dispatcher.js 路由用的动作(参数顺序由 dispatcher 约定:arg, el, ev)
    window.shelfImportArxiv = importArxiv;
    window.shelfPickPdf = pickPdf;
    window.shelfDeletePaper = deletePaper;
    window.shelfAsk = askQuestion;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
