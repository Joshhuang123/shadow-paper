/**
 * shadow-paper.js —— 论文阅读器前端公共工具
 *
 * 为什么要单独抽一个文件:
 *   书架页(shelf.js)和阅读器页(reader.js)都要用到三件事 ——
 *   「取数据」「处理公式占位符」「KaTeX 加载与降级」。
 *   这三件都跟页面无关,放一起改一处两处生效。
 *
 * 暴露 window.SP。不引任何第三方库(项目要求零构建步骤)。
 */
(function () {
    'use strict';

    var SP = {};

    /* ==========================================================
       1. 行内公式占位符
       ----------------------------------------------------------
       后端把行内公式写成 ⟦MATH:<LaTeX>⟧⟧(见 extensions/schema.py)。
       为什么不用 $...$:英文正文里 $ 是货币符号,切句/查词都会被误伤。
       占位符必须**整体**处理 —— 里面有点号、有 \frac、有 0.97,
       切开就毁了。
       ========================================================== */

    var MATH_RE = /⟦MATH:([\s\S]*?)⟧⟧/g;
    SP.MATH_RE = MATH_RE;

    /**
     * 把一段带占位符的文本拆成 [{type:'text'|'math', value}, …]。
     * 返回的片段拼接起来 === 原串,渲染层按顺序吐 DOM。
     */
    SP.splitMath = function (text) {
        var out = [];
        if (!text) return out;
        var last = 0, m;
        MATH_RE.lastIndex = 0;
        while ((m = MATH_RE.exec(text)) !== null) {
            if (m.index > last) out.push({ type: 'text', value: text.slice(last, m.index) });
            out.push({ type: 'math', value: m[1] });
            last = m.index + m[0].length;
        }
        if (last < text.length) out.push({ type: 'text', value: text.slice(last) });
        return out;
    };

    /** 文本里有没有公式占位符 —— 用来决定要不要走公式渲染分支。 */
    SP.hasMath = function (text) {
        if (!text) return false;
        MATH_RE.lastIndex = 0;
        return MATH_RE.test(text);
    };

    /* ==========================================================
       2. KaTeX:动态加载 + 降级
       ----------------------------------------------------------
       为什么不用 <script src="…katex.min.js"> 直接写死:
         CDN 不通时,写死的 script 会**阻塞** DOMContentLoaded ——
         页面要等它超时(可能几十秒)才出现内容,用户以为应用死了。
       为什么不用 $…$ + renderMathInElement:
         英文正文里 $ 是货币符号,自动渲染器会把 "$5" 之类当成公式,
         而且 LaTeX 里本身也可能带 $,边界不可控。
         这里改成**手动**按占位符逐个 katex.render —— 输入输出完全确定,
         而且顺带能精确标记「哪一段是公式」给查词用。
       降级路径:
         正文先用 <code class="math-fallback"> 显示 LaTeX 原文(永远不是空白),
         KaTeX 到位后再原地升级。
       ========================================================== */

    /* CDN 源清单,按优先级。
       为什么不止 jsdelivr 一个:实测 jsdelivr 在部分网络(尤其国内)根本连不上,
       单源失败就意味着整篇论文、上千个公式全变成 LaTeX 原文,体验损失太大。
       这里覆盖三个源(jsdelivr / unpkg / npmmirror 淘宝源),逐个试,
       第一个通的用哪个;全不通才降级成 <code> 原文。 */
    var KATEX_SOURCES = [
        {
            js: 'https://cdn.jsdelivr.net/npm/katex@0.16/dist/katex.min.js',
            css: 'https://cdn.jsdelivr.net/npm/katex@0.16/dist/katex.min.css'
        },
        {
            js: 'https://unpkg.com/katex@0.16/dist/katex.min.js',
            css: 'https://unpkg.com/katex@0.16/dist/katex.min.css'
        },
        {
            js: 'https://registry.npmmirror.com/katex/0.16.11/files/dist/katex.min.js',
            css: 'https://registry.npmmirror.com/katex/0.16.11/files/dist/katex.min.css'
        }
    ];
    // 单个源的等待上限。源之间串行,3×5s 最坏 15s;
    // 但正文在这期间已经可读(公式先显示原文),不会白屏干等。
    var SOURCE_TIMEOUT = 5000;

    // 'idle' | 'loading' | true(可用) | false(不可用)
    SP.katexState = 'idle';
    SP.katexCssOk = false;
    var katexListeners = [];

    SP.onKatexChange = function (fn) {
        if (typeof fn !== 'function') return;
        if (SP.katexState === true || SP.katexState === false) fn(SP.katexState);
        else katexListeners.push(fn);
    };

    function notifyKatex(state) {
        var list = katexListeners.slice();
        katexListeners.length = 0;
        list.forEach(function (fn) {
            try { fn(state); } catch (e) { console.error('[SP] katex listener', e); }
        });
    }

    /**
     * 动态加载 KaTeX。onChange(ok) 在状态确定时回调一次。
     * 幂等:重复调用不会重复注入 <script>。
     */
    SP.loadKatex = function (onChange) {
        if (onChange) SP.onKatexChange(onChange);
        if (SP.katexState !== 'idle') return;

        SP.katexState = 'loading';
        var jsIdx = 0;
        var cssIdx = 0;

        function finish(ok) {
            SP.katexState = ok;
            if (ok) document.documentElement.classList.add('katex-ready');
            notifyKatex(ok);
        }

        /**
         * 依次尝试各源的 CSS。
         * 为什么和 JS 分开:极少数情况 JS 通而 CSS 不通,
         * 分开才有机会换一个源把样式补上。
         */
        function loadCss() {
            if (cssIdx >= KATEX_SOURCES.length) {
                document.documentElement.classList.add('katex-nocss');
                return;
            }
            var src = KATEX_SOURCES[cssIdx++];
            var link = document.createElement('link');
            link.rel = 'stylesheet';
            link.href = src.css;
            link.crossOrigin = 'anonymous';
            link.onload = function () {
                SP.katexCssOk = true;
                document.documentElement.classList.add('katex-css-ok');
            };
            link.onerror = function () { loadCss(); };
            document.head.appendChild(link);
        }

        /** 依次尝试各源的 JS,第一个成功的就赢。 */
        function loadJs() {
            if (jsIdx >= KATEX_SOURCES.length) { finish(false); return; }
            var src = KATEX_SOURCES[jsIdx++];
            var settled = false;

            // 超时兜底:script 的 onerror 只在「DNS 失败/被墙」时触发,
            // 「连上了但很慢」不会 —— 所以自己加个定时器。
            var timer = setTimeout(function () {
                if (settled) return;
                settled = true;
                loadJs();
            }, SOURCE_TIMEOUT);

            var s = document.createElement('script');
            s.src = src.js;
            s.async = true;
            s.crossOrigin = 'anonymous';
            s.onload = function () {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                if (window.katex) { loadCss(); finish(true); }
                else loadJs();
            };
            s.onerror = function () {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                loadJs();
            };
            document.head.appendChild(s);
        }

        loadJs();
    };

    /**
     * 造一个公式节点。
     * 结构固定为 <span class="math-inline" data-latex="…">
     * 里面先放 <code class="math-fallback">LaTeX 原文</code>,
     * KaTeX 到位后被替换掉。这样「CDN 不通」和「还没加载完」两种情况下,
     * 用户看到的都不是空白,也不会看到 ⟦MATH:…⟧⟧⟧ 这种原始符号。
     */
    SP.buildMathSpan = function (latex, doc) {
        doc = doc || document;
        var span = doc.createElement('span');
        span.className = 'math-inline';
        span.setAttribute('data-latex', latex);
        span.setAttribute('data-math', '1');
        span.appendChild(SP.buildFallback(latex, doc));
        return span;
    };

    /** 降级形态:公式原文用 <code> 显示。 */
    SP.buildFallback = function (latex, doc) {
        doc = doc || document;
        var code = doc.createElement('code');
        code.className = 'math-fallback';
        code.textContent = latex;
        return code;
    };

    /**
     * 把一段带占位符的文本变成 DOM 片段。
     * 文本部分原样建 TextNode(不做逐词拆分 —— 26k 词逐词包 span 会
     * 让 DOM 膨胀到十几万个节点,滚动直接卡住;查词用
     * caretRangeFromPoint 定位,不需要 span)。
     */
    SP.buildTextNodes = function (text, doc) {
        doc = doc || document;
        var frag = doc.createDocumentFragment();
        SP.splitMath(text).forEach(function (part) {
            if (part.type === 'text') {
                frag.appendChild(doc.createTextNode(part.value));
            } else {
                frag.appendChild(SP.buildMathSpan(part.value, doc));
            }
        });
        return frag;
    };

    /**
     * 渲染单个公式节点。成功返回 true。
     *
     * 三层兜底(一层比一层「让公式更像公式」):
     *   ① 直接行内渲染 —— 绝大多数公式走这条
     *   ② 报 "Undefined control sequence" → 把这个宏换成直立体字母再渲染。
     *      论文的自定义宏(\Y \F \M \de 之类)后端只展开了 \newcommand 形式,
     *      剩下的 KaTeX 不认识;**一个不认识的宏会让整条公式报错**,
     *      整条退化成 LaTeX 原文比缺一个符号难看得多。
     *   ③ 报 "can be used only in display mode" → 改用行间模式再渲染
     *   ④ 都不行 → 退回 <code> 原文(最后的保底,不是常态)
     */
    SP.renderMathNode = function (span) {
        if (!window.katex) return false;
        var latex = span.getAttribute('data-latex');
        if (latex == null) return false;

        var display = span.getAttribute('data-display') === '1';
        var work = stripAnnotations(latex);
        var undef = [];
        var promoted = false;

        for (var attempt = 0; attempt < 8; attempt++) {
            var r = tryKatex(work, display);
            if (r.ok) {
                span.textContent = '';
                while (r.node.firstChild) span.appendChild(r.node.firstChild);
                span.classList.add('math-rendered');
                if (display) span.classList.add('math-rendered-display');
                if (undef.length) {
                    span.classList.add('math-macro-undef');
                    span.setAttribute('data-undef', undef.join(' '));
                    span.setAttribute('title',
                        '原文里的自定义宏 ' + undef.map(function (u) { return '\\' + u; }).join(' ')
                        + ' 没被后端展开,这里按字面显示');
                }
                return true;
            }

            var msg = r.err || '';

            // ② 不认识的宏 → 换成 \mathrm{X} 再试
            var m = /Undefined control sequence: \\([A-Za-z]+)/.exec(msg);
            if (m && undef.indexOf(m[1]) < 0) {
                undef.push(m[1]);
                work = replaceCommand(work, m[1]);
                continue;
            }

            // ③ 只能在行间显示 → 切 display 模式
            if (!display && /only in display mode/.test(msg)) {
                display = true;
                promoted = true;
                continue;
            }

            break;
        }

        // ④ 保底:LaTeX 原文
        span.textContent = '';
        span.appendChild(SP.buildFallback(latex));
        span.classList.add('math-failed');
        return false;
    };

    /** 在临时节点上试渲染。失败时不碰目标节点,避免留下半截 DOM。 */
    function tryKatex(latex, display) {
        var tmp = document.createElement('span');
        try {
            window.katex.render(latex, tmp, {
                displayMode: display,
                throwOnError: true,
                strict: 'ignore',
                output: 'html',
                trust: false,
                maxSize: 24,
                maxExpand: 512
            });
            return { ok: true, node: tmp };
        } catch (e) {
            return { ok: false, err: String((e && e.message) || e) };
        }
    }

    /** 把不认识的宏换成直立体字母:\Y → \mathrm{Y} */
    function replaceCommand(s, name) {
        var re = new RegExp('\\\\' + name + '(?![A-Za-z])', 'g');
        return s.replace(re, '\\mathrm{' + name + '}');
    }

    /**
     * 去掉 \footnote{...} / \thanks{...}。
     * 为什么:这两个是纯文字注脚,里面常带 $…$,在数学模式里是非法的
     * (KaTeX 会报 "$ is not allowed in math mode"),而且 KaTeX 本身不认它们。
     * 整条注脚删掉,留一个上标星号当标记。
     */
    function stripAnnotations(s) {
        var re = /\\(footnote|thanks)\s*\{/;
        var out = '', i = 0;
        for (;;) {
            var rest = s.slice(i);
            var m = re.exec(rest);
            if (!m) { out += rest; break; }
            out += rest.slice(0, m.index);
            var depth = 1;
            var j = i + m.index + m[0].length;
            while (j < s.length && depth > 0) {
                var c = s.charAt(j);
                if (c === '\\') { j += 2; continue; }
                if (c === '{') depth++;
                else if (c === '}') depth--;
                j++;
            }
            out += '^{\\ast}';
            i = j;
        }
        return out;
    }

    /**
     * 分批渲染 root 下所有公式。
     * 为什么要分批:这篇论文正文里有一千多个行内公式,
     * 一次性 katex.render 完会卡住主线程一两秒,页面看着像死了。
     * 每帧处理一小批,用户能一边读一边看到公式陆续成形。
     */
    SP.renderMathInAsync = function (root, onDone) {
        if (!window.katex) { if (onDone) onDone(0, []); return; }
        var spans = Array.prototype.slice.call(root.querySelectorAll('.math-inline[data-latex]'));
        var i = 0, ok = 0;
        var undef = {};
        var CHUNK = 60;
        var raf = window.requestAnimationFrame
            ? function (fn) { window.requestAnimationFrame(fn); }
            : function (fn) { setTimeout(fn, 0); };

        function step() {
            var end = Math.min(i + CHUNK, spans.length);
            for (; i < end; i++) {
                if (SP.renderMathNode(spans[i])) {
                    ok++;
                    var u = spans[i].getAttribute('data-undef');
                    if (u) u.split(' ').forEach(function (x) { if (x) undef[x] = true; });
                }
            }
            if (i < spans.length) {
                raf(step);
            } else if (onDone) {
                onDone(ok, Object.keys(undef));
            }
        }
        step();
    };

    /* ==========================================================
       3. 网络:统一封装
       ----------------------------------------------------------
       为什么自己包一层:
         后端失败时返回的**也是 JSON**({success:false,error:"…"}),
         直接用 fetch 的话 400/429/503 三种情况各写一遍 try。
       统一抛 SP.ApiError,带 status / message / retryable,
       调用方按 status 分支处理即可。
       ========================================================== */

    function ApiError(status, message, extra) {
        this.name = 'ApiError';
        this.status = status;
        this.message = message || '请求失败';
        this.extra = extra || {};
        this.retryable = !!(extra && extra.retryable);
    }
    ApiError.prototype = Object.create(Error.prototype);
    ApiError.prototype.constructor = ApiError;
    SP.ApiError = ApiError;

    function parseBody(text) {
        if (!text) return null;
        try { return JSON.parse(text); } catch (e) { return null; }
    }

    /**
     * 统一请求。resolve 的是 {success:true, …} 的 body;
     * 失败一律 reject 一个 ApiError。
     */
    SP.request = function (url, opts) {
        opts = opts || {};
        return fetch(url, {
            method: opts.method || 'GET',
            headers: opts.body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
            body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
            cache: opts.cache || 'default'
        }).then(function (res) {
            return res.text().then(function (text) {
                var data = parseBody(text);
                if (!res.ok || !data || data.success === false) {
                    var msg = (data && data.error) || ('请求失败（HTTP ' + res.status + '）');
                    throw new ApiError(res.status, msg, data || {});
                }
                return data;
            });
        }).catch(function (err) {
            if (err instanceof ApiError) throw err;
            // fetch 本身失败 = 断网 / 服务没起来
            throw new ApiError(0, '连不上服务器，检查一下服务是否在运行', { retryable: true });
        });
    };

    SP.getJSON = function (url, opts) { return SP.request(url, opts); };

    SP.postJSON = function (url, body, opts) {
        opts = opts || {};
        opts.method = 'POST';
        opts.body = body || {};
        return SP.request(url, opts);
    };

    /**
     * 文件/JSON 上传。用 XHR 而不是 fetch,是因为 XHR 有 upload.onprogress ——
     * 上传几十 MB 的 PDF 时能看到真实百分比,fetch 拿不到上传进度。
     */
    SP.upload = function (url, opts) {
        opts = opts || {};
        return new Promise(function (resolve, reject) {
            var xhr = new XMLHttpRequest();
            xhr.open(opts.method || 'POST', url, true);
            xhr.timeout = opts.timeout || 300000;   // arXiv 导入可能跑几十秒
            xhr.upload.onprogress = function (e) {
                if (e.lengthComputable && opts.onProgress) opts.onProgress(e.loaded, e.total);
            };
            xhr.onload = function () {
                var data = parseBody(xhr.responseText);
                if (xhr.status >= 200 && xhr.status < 300 && data && data.success) {
                    resolve(data);
                } else {
                    reject(new ApiError(
                        xhr.status,
                        (data && data.error) || ('导入失败（HTTP ' + xhr.status + '）'),
                        data || {}
                    ));
                }
            };
            xhr.onerror = function () { reject(new ApiError(0, '网络连接失败', { retryable: true })); };
            xhr.ontimeout = function () { reject(new ApiError(0, '请求超时', { retryable: true })); };
            xhr.onabort = function () { reject(new ApiError(0, '已取消', {})); };

            if (opts.form) {
                xhr.send(opts.form);
            } else {
                xhr.setRequestHeader('Content-Type', 'application/json');
                xhr.send(JSON.stringify(opts.json || {}));
            }
        });
    };

    /* ==========================================================
       4. 查词:定位光标下的单词
       ----------------------------------------------------------
       为什么用 caretRangeFromPoint 而不是给每个词包一层 <span>:
         这篇论文 26260 个词,逐词包 span 会造出十几万个 DOM 节点,
         滚动和选中都会明显变卡。caretRangeFromPoint 直接问浏览器
         「(x,y) 这个位置是哪个文本节点的第几个字符」,零 DOM 成本。
       公式内部不可点:公式渲染后是一堆嵌套 span,里面全是数学符号,
         在里面找单词会查到什么 "frac" "cdot" —— 所以命中 .math-inline
         就直接放弃。
       ========================================================== */

    var WORD_RE = /[A-Za-z]/;

    function isWordChar(ch) {
        return /[A-Za-z'’\-]/.test(ch);
    }

    /**
     * 取点击位置所在的英文单词。
     * 返回 {word, node, offset} 或 null。
     */
    SP.wordAtPoint = function (x, y) {
        var range = null;
        if (document.caretRangeFromPoint) {
            range = document.caretRangeFromPoint(x, y);
        } else if (document.caretPositionFromPoint) {
            var pos = document.caretPositionFromPoint(x, y);
            if (pos) range = pos.toRange ? pos.toRange() : pos;
        }
        if (!range) return null;

        var node = range.startContainer;
        if (!node || node.nodeType !== 3) return null;

        // 落在公式里 → 不查
        var host = node.parentElement;
        if (host && host.closest('.math-inline, .math-display, [data-math]')) return null;

        var text = node.data || '';
        var off = range.startOffset;

        // caret 可能刚好落在单词后面(浏览器把光标吸附到最近的断点),
        // 这时退一格再试。
        if (!WORD_RE.test(text.charAt(off))) {
            if (off > 0 && WORD_RE.test(text.charAt(off - 1))) off -= 1;
            else return null;
        }

        var start = off, end = off;
        while (start > 0 && isWordChar(text.charAt(start - 1))) start--;
        while (end < text.length && isWordChar(text.charAt(end))) end++;

        var word = text.slice(start, end);
        if (!word || word.length > 40) return null;
        if (!WORD_RE.test(word)) return null;

        return { word: word, node: node, offset: off };
    };

    /**
     * 取 root 下「不含公式」的纯文本,以及每个文本节点在纯文本里的区间。
     * 为什么:查词要拿到整句才能翻译,而 KaTeX 渲染后的公式在 textContent
     * 里会出现两遍(MathML 一份 + HTML 一份),直接取会把公式混进句子。
     */
    SP.plainText = function (root) {
        var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
            acceptNode: function (node) {
                var p = node.parentElement;
                if (!p) return NodeFilter.FILTER_REJECT;
                if (p.closest('.math-inline, .math-display, [data-math]')) return NodeFilter.FILTER_REJECT;
                return NodeFilter.FILTER_ACCEPT;
            }
        });
        var records = [], text = '';
        var n;
        while ((n = walker.nextNode())) {
            records.push({ node: n, start: text.length, end: text.length + n.data.length });
            text += n.data;
        }
        return { text: text, records: records };
    };

    /** 把 (node, offset) 换算成在 plain.text 里的绝对位置。 */
    SP.plainOffset = function (plain, node, offset) {
        for (var i = 0; i < plain.records.length; i++) {
            if (plain.records[i].node === node) return plain.records[i].start + offset;
        }
        return -1;
    };

    /**
     * 粗切句子。英文缩写会误切(et al. / Fig. 2),
     * 但「翻译整句」是辅助功能,切得不够准不影响主流程。
     */
    SP.sentenceAround = function (text, offset) {
        if (!text) return '';
        var start = 0, i;
        for (i = offset - 1; i >= 0; i--) {
            var c = text.charAt(i);
            if (c === '.' || c === '!' || c === '?') { start = i + 1; break; }
        }
        var end = text.length;
        for (i = offset; i < text.length; i++) {
            var c2 = text.charAt(i);
            if (c2 === '.' || c2 === '!' || c2 === '?') { end = i + 1; break; }
        }
        var s = text.slice(start, end).replace(/\s+/g, ' ').trim();
        return s.length > 400 ? s.slice(0, 400) + '…' : s;
    };

    /**
     * 整句翻译(MyMemory 直连)。
     * 后端没有翻译端点,只能前端直连。失败**静默**返回 null:
     * 翻译是锦上添花,不能因为它挂了就打断查词。
     */
    SP.translate = function (text, langpair) {
        if (!text) return Promise.resolve(null);
        var url = 'https://api.mymemory.translated.net/api/v1/get?q='
            + encodeURIComponent(text)
            + '&langpair=' + encodeURIComponent(langpair || 'en|zh-CN');
        var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
        var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, 8000) : null;
        return fetch(url, ctrl ? { signal: ctrl.signal } : undefined)
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (timer) clearTimeout(timer);
                var t = d && d.responseData && d.responseData.translatedText;
                return t || null;
            })
            .catch(function () {
                if (timer) clearTimeout(timer);
                return null;
            });
    };

    /* ==========================================================
       5. DOM 辅助
       ========================================================== */

    SP.esc = function (s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    };

    /** 建元素。attrs 支持 class/text/html/on* 事件。 */
    SP.el = function (tag, attrs, kids) {
        var e = document.createElement(tag);
        if (attrs) {
            Object.keys(attrs).forEach(function (k) {
                var v = attrs[k];
                if (v == null) return;
                if (k === 'class') e.className = v;
                else if (k === 'text') e.textContent = v;
                else if (k === 'html') e.innerHTML = v;
                else if (k.slice(0, 2) === 'on' && typeof v === 'function') {
                    e.addEventListener(k.slice(2), v);
                } else e.setAttribute(k, v);
            });
        }
        (kids || []).forEach(function (c) {
            if (c == null) return;
            e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
        });
        return e;
    };

    /** 清空并塞入子节点。 */
    SP.fill = function (parent, kids) {
        parent.textContent = '';
        (kids || []).forEach(function (c) {
            if (c == null) return;
            parent.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
        });
        return parent;
    };

    /** 防抖。 */
    SP.debounce = function (fn, wait) {
        var t = null;
        return function () {
            var self = this, args = arguments;
            if (t) clearTimeout(t);
            t = setTimeout(function () { t = null; fn.apply(self, args); }, wait);
        };
    };

    /** 节流(尾沿触发)。滚动监听用这个,不能每个像素都发请求。 */
    SP.throttle = function (fn, wait) {
        var last = 0, timer = null;
        return function () {
            var self = this, args = arguments;
            var now = Date.now();
            var remain = wait - (now - last);
            if (remain <= 0) {
                last = now;
                fn.apply(self, args);
            } else if (!timer) {
                timer = setTimeout(function () {
                    timer = null;
                    last = Date.now();
                    fn.apply(self, args);
                }, remain);
            }
        };
    };

    /** 12345 → "12,345" */
    SP.fmtInt = function (n) {
        return (Number(n) || 0).toLocaleString('zh-CN');
    };

    /** "2026-11-23T18:30:00" → "2026-11-23" */
    SP.fmtDate = function (s) {
        if (!s) return '';
        return String(s).slice(0, 10).replace(/-/g, '.');
    };

    /** 时间戳(ms) → "3 天后" / "今天到期" */
    SP.fmtDue = function (ts) {
        if (!ts) return '';
        var diff = ts - Date.now();
        var day = 86400000;
        if (diff <= 0) return '已到期';
        if (diff < day) return Math.max(1, Math.round(diff / 3600000)) + ' 小时后';
        return Math.round(diff / day) + ' 天后';
    };

    /* ==========================================================
       6. Service Worker
       ----------------------------------------------------------
       后端把静态文件挂在 /static/ 下,没有单独的 /service-worker.js 路由。
       想让 SW 接管整个站点需要两个条件:
         ① 脚本路径是作用域的祖先 → /static/service-worker.js 配 scope '/' 不满足
         ② 响应带 `Service-Worker-Allowed: /` 头 → 后端现在没发
       直接试 scope:'/' 浏览器会在控制台报 "path of the provided scope is not
       under the max scope allowed",所以老老实实用默认作用域(/static/)。
       那样至少 CSS/JS/图标能离线,页面本身仍需后端补一条根路径路由。
       ========================================================== */
    SP.registerSW = function () {
        if (!('serviceWorker' in navigator)) return;
        window.addEventListener('load', function () {
            navigator.serviceWorker.register('/static/service-worker.js')
                .catch(function (e) {
                    console.info('[SP] Service Worker 未生效:', e && e.message);
                });
        });
    };

    window.SP = SP;
})();
