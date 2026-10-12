'use strict';

/* Маленький markdown ровно под те файлы, что пишет само приложение или нейросеть:
   заголовки, жирный, курсив, код, цитаты, списки, линии.
   Библиотеку не тянем: окно должно работать офлайн и без единого запроса в сеть.

   Разметка применяется к уже экранированному тексту, поэтому «<b>» из файла
   останется текстом, а не станет тегом. */

(function () {
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ESC[c]);

  // Метка для вынутых кусков кода. Символ из служебной области Unicode:
  // в расшифровке разговора он не встретится, а нулевые байты в файле
  // приложение считает следом антивируса, их нельзя даже во временных строках
  const MARK = '';
  const MARKED = new RegExp(MARK + '(\\d+)' + MARK, 'g');

  /** Разметка внутри строки. */
  function inline(src) {
    let s = esc(src);

    // Код вынимаем первым: внутри него ничего разбирать не нужно
    const code = [];
    s = s.replace(/`([^`]+)`/g, (_, c) => MARK + (code.push(c) - 1) + MARK);

    s = s.replace(/\*\*([^*]+)\*\*/g, (_, t) => `<b>${t}</b>`);
    s = s.replace(/(^|[\s(«"])\*([^*\n]+)\*/g, (_, p, t) => `${p}<i>${t}</i>`);
    s = s.replace(/(^|[\s(«"])_([^_\n]+)_/g, (_, p, t) => `${p}<i>${t}</i>`);

    // Ссылки не делаем кликабельными: открывать браузер из окна записи незачем,
    // но адрес видно по наведению
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
      (_, t, href) => `<span class="md-link" title="${href}">${t}</span>`);

    return s.replace(MARKED, (_, i) => `<code>${code[i]}</code>`);
  }

  const RE = {
    head: /^(#{1,6})\s+(.*)$/,
    hr: /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/,
    quote: /^>\s?(.*)$/,
    ul: /^\s*[-*•]\s+(.*)$/,
    ol: /^\s*\d+[.)]\s+(.*)$/,
  };

  /** Markdown → HTML. */
  function render(text) {
    const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let para = [];      // копящийся абзац
    let list = null;    // {tag, items}
    let quote = [];

    const flushPara = () => {
      if (!para.length) return;
      out.push(`<p>${para.map(inline).join('<br>')}</p>`);
      para = [];
    };
    const flushList = () => {
      if (!list) return;
      const items = list.items.map((i) => `<li>${inline(i)}</li>`).join('');
      out.push(`<${list.tag}>${items}</${list.tag}>`);
      list = null;
    };
    const flushQuote = () => {
      if (!quote.length) return;
      out.push(`<blockquote>${quote.map(inline).join('<br>')}</blockquote>`);
      quote = [];
    };
    const flushAll = () => { flushPara(); flushList(); flushQuote(); };

    for (const line of lines) {
      if (!line.trim()) { flushAll(); continue; }

      const q = RE.quote.exec(line);
      if (q) { flushPara(); flushList(); quote.push(q[1]); continue; }
      flushQuote();

      if (RE.hr.test(line)) { flushAll(); out.push('<hr>'); continue; }

      const h = RE.head.exec(line);
      if (h) {
        flushAll();
        // «#» из файла показываем как h2: h1 в окне уже занят названием записи
        const level = Math.min(h[1].length + 1, 6);
        out.push(`<h${level}>${inline(h[2])}</h${level}>`);
        continue;
      }

      const ul = RE.ul.exec(line);
      const ol = ul ? null : RE.ol.exec(line);
      if (ul || ol) {
        flushPara();
        const tag = ul ? 'ul' : 'ol';
        if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
        list.items.push((ul || ol)[1]);
        continue;
      }
      flushList();

      para.push(line);
    }

    flushAll();
    return out.join('\n');
  }

  window.md = { render, inline, escape: esc };
})();
