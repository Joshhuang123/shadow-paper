// 改动 CSS/JS/HTML 后把版本号 +1。activate 时会删掉所有旧版缓存
// (见下方 CACHE_NAME 过滤),所以老设备不会继续吃旧样式。
//
// v20:从英语学习版改造成论文阅读器。
//   · 预缓存清单换成论文版的页面与脚本(/tutor /grammar /stats 这些英语页没有了)
//   · 加上 KaTeX CDN —— 公式渲染要它,离线时也得能用
//   · CACHE_NAME 从 shadow-ebook-* 改成 shadow-paper-*,不跟英语版共用缓存
// v23（2026-11-26）：修中英切换（前端不再自己拼 ?lang=zh，改由服务端 cookie 决定）
// v22（2026-11-26）：中文版彻底收口 —— 题名/摘要中文化、目录中文化、列表项中文化。
// v21（2026-11-26）：改了 reader.js / shelf.js（中文版切换），
// 但浏览器里跑的还是老 service worker 缓存的旧 JS ——
// **改了 web/ 下的资源就必须 bump 这个版本号**，否则老设备永远吃旧文件。
// （仓里已有的约定，这里只是继续遵守。）
const CACHE_NAME = 'shadow-paper-v23';

// 本地静态资源。全部走 /static/ —— 后端只挂了这一条静态路由。
const STATIC_ASSETS = [
  '/',
  '/static/manifest.json',
  '/static/theme.js',
  '/static/a11y.js',
  '/static/js/dispatcher.js',
  '/static/js/shadow-paper.js',
  '/static/js/shelf.js',
  '/static/js/reader.js',
  '/static/styles/tokens.css',
  '/static/styles/base.css',
  '/static/styles/icons.css',
  '/static/styles/components.css',
  '/static/styles/paper.css',
  '/static/icon-48.png',
  '/static/icon-72.png',
  '/static/icon-96.png',
  '/static/icon-128.png',
  '/static/icon-144.png',
  '/static/icon-152.png',
  '/static/icon-192.png',
  '/static/icon-384.png',
  '/static/icon-512.png'
];

// CDN 资源单独一份清单。
// 为什么不能混进上面的数组:addAll 是**原子**的 —— 有一个 404 就整批都不缓存。
// CDN 一旦不通(或者用户压根断网),连本地 CSS/JS 也缓存不上,离线能力直接归零。
// 所以本地走 addAll,CDN 逐个 add 并各自吞掉错误。
//
// KaTeX 列了三个源:jsdelivr 在部分网络(尤其国内)连不上,
// 前端会按这个顺序挨个试,哪个通用哪个;
// 离线时哪个缓存过就用哪个。
const CDN_ASSETS = [
  'https://cdn.jsdelivr.net/npm/katex@0.16/dist/katex.min.css',
  'https://cdn.jsdelivr.net/npm/katex@0.16/dist/katex.min.js',
  'https://unpkg.com/katex@0.16/dist/katex.min.css',
  'https://unpkg.com/katex@0.16/dist/katex.min.js',
  'https://registry.npmmirror.com/katex/0.16.11/files/dist/katex.min.css',
  'https://registry.npmmirror.com/katex/0.16.11/files/dist/katex.min.js'
];

// 安装 Service Worker
self.addEventListener('install', (event) => {
  console.log('[SW] Installing...');
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => {
        console.log('[SW] Caching static assets');
        return cache.addAll(STATIC_ASSETS).catch(err => {
          console.log('[SW] Cache failed, continuing:', err);
        });
      })
      .then(() => {
        // CDN 逐个缓存,失败不影响其它资源
        return caches.open(CACHE_NAME).then(cache =>
          Promise.all(CDN_ASSETS.map(url =>
            cache.add(url).catch(err => console.log('[SW] CDN asset skipped:', url, err))
          ))
        );
      })
      .then(() => {
        console.log('[SW] Skip waiting');
        return self.skipWaiting();
      })
  );
});

// 激活并清理旧缓存
self.addEventListener('activate', (event) => {
  console.log('[SW] Activating...');
  event.waitUntil(
    caches.keys()
      .then((cacheNames) => {
        return Promise.all(
          cacheNames
            // 英语版的 shadow-ebook-* 缓存一并清掉:两个应用共用一个源目录,
            // 留着只会占空间,还可能让论文页拿到英语版的样式。
            .filter((name) => (name.startsWith('shadow-') || name.startsWith('shadow-ebook-')) && name !== CACHE_NAME)
            .map((name) => {
              console.log('[SW] Deleting old cache:', name);
              return caches.delete(name);
            })
        );
      })
      .then(() => {
        console.log('[SW] Claiming clients');
        return self.clients.claim();
      })
  );
});

// 请求拦截
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // 跳过非 GET 请求
  if (request.method !== 'GET') return;

  // 跳过 Chrome 扩展和其他非 HTTP 请求
  if (!url.protocol.startsWith('http')) return;

  // API 请求:**纯网络,不缓存**。
  //
  // /api/ 下全是**可变状态** —— 阅读进度、笔记、生词本状态。
  // 缓存它们等于把「上次看到的数据」当真相:家长刚改完的东西
  // 可能几秒钟内还是旧的,生词本也会出现「刚删了还在列表里」的错觉。
  //
  // 现在不缓存 API,请求要么真到服务器(拿到新鲜数据),要么 reject 让前端
  // 走 catch 分支弹错误。宁可明着报错,也不要静默地给一份陈旧数据。
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(request).catch(() => new Response(
        JSON.stringify({ success: false, error: 'offline' }),
        { status: 503, headers: { 'Content-Type': 'application/json' } }
      ))
    );
    return;
  }

  // 论文插图:网络优先,断网时回落到缓存 —— 图是静态的,缓存住最划算。
  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const responseClone = response.clone();
            caches.open(CACHE_NAME).then((cache) => {
              cache.put(request, responseClone);
            });
          }
          return response;
        })
        .catch(() => caches.match(request))
    );
    return;
  }

  // HTML页面使用网络优先，回退到缓存
  // (/ 和 /read/<pid> 都在这一档:论文正文页也是靠运行时缓存兜底离线)
  if (request.headers.get('accept')?.includes('text/html')) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const responseClone = response.clone();
            caches.open(CACHE_NAME).then((cache) => {
              cache.put(request, responseClone);
            });
          }
          return response;
        })
        .catch(() => {
          return caches.match(request).then(cached => {
            return cached || caches.match('/') || new Response('Offline', { status: 503, statusText: 'Offline' });
          });
        })
    );
    return;
  }

  // KaTeX CDN:缓存优先。
  // 版本号锁死在 0.16,内容不会变,缓存住不会拿到旧版。
  if (url.hostname === 'cdn.jsdelivr.net' && url.pathname.includes('/katex@0.16/')) {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) return cached;
        return fetch(request).then((response) => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
          }
          return response;
        });
      })
    );
    return;
  }

  // 静态资源(CSS / JS):网络优先,缓存兜底。
  // 网络优先保留了离线兜底,又让改动立刻生效(缓存优先会让人误判成「改错了」)。
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
        }
        return response;
      })
      .catch(() => caches.match(request))
  );
});

// 处理消息
self.addEventListener('message', (event) => {
  if (event.data === 'skipWaiting') {
    self.skipWaiting();
  }
  if (event.data === 'clearCache') {
    caches.delete(CACHE_NAME).then(() => {
      console.log('[SW] Cache cleared');
    });
  }
});
