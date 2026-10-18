
() => {
  const lists = Array.from(document.querySelectorAll('div[data-widget="webListReviews"]'));
  const cards = Array.from(document.querySelectorAll('[data-review-uuid]'));
  const RE_DATE = /(\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+\d{4})/;
  const RE_ASPECT = /^[^:\n]{1,40}:\s*\S/;
  const blockIsAspects = (t) =>
    t.split('\n').filter((l) => l.trim()).every((l) => RE_ASPECT.test(l.trim()));
  const isStale = (el) => {
    if (el.getAttribute?.('title') === 'Отзыв устарел') return true;
    const t = el.querySelector?.('[title="Отзыв устарел"], [title*="устарел"]');
    if (t) return true;
    for (const n of el.querySelectorAll('div, span')) {
      const s = (n.innerText || '').trim();
      if (s && /^Отзыв\s+устарел$/i.test(s)) return true;
    }
    return false;
  };
  const extract = (card) => {
    const wrapper = card.querySelector('div[data-review-uuid]') || card;
    const uuid = wrapper.getAttribute('data-review-uuid');
    const publishedAt = wrapper.getAttribute('publishedat');
    const fullText = (wrapper.innerText || '');
    const dateMatch = fullText.match(RE_DATE);
    const date = dateMatch ? dateMatch[1] : '';
    const spans = Array.from(wrapper.querySelectorAll('span.tsCompactControl500Medium'));
    let author = (spans[0]?.innerText || '').trim();
    let author2 = (spans[1]?.innerText || '').trim();
    if (!author) {
      const alt = wrapper.querySelector('span[class*="tsCompact"]');
      if (alt) author = (alt.innerText || '').trim();
    }
    if (author2 && author2 === author) author2 = '';
    let variant = '';
    let aspectsEl = null;
    let bestLen = -1;
    Array.from(wrapper.querySelectorAll('a[href*="/product/"]')).forEach((a) => {
      const t = (a.innerText || '').trim();
      if (!RE_ASPECT.test(t)) return;
      let cur = a;
      while (cur && cur !== wrapper) {
        const inner = (cur.innerText || '').trim();
        if (inner.length <= 600 && cur.children.length <= 3 && blockIsAspects(inner)) {
          if (inner.length >= bestLen) {
            bestLen = inner.length;
            variant = inner;
            aspectsEl = cur;
          }
        }
        cur = cur.parentElement;
      }
    });
    let text = '';
    if (aspectsEl && aspectsEl.parentElement) {
      let bestT = '';
      let bestTlen = -1;
      Array.from(aspectsEl.parentElement.children).forEach((c) => {
        if (c === aspectsEl) return;
        const t = (c.innerText || '').trim();
        if (t.length > bestTlen) { bestTlen = t.length; bestT = t; }
      });
      text = bestT;
    }
    if (!text) {
      text = fullText
        .replace(/^[^\n]*\n/, '')
        .split('\n').filter((l) => !RE_ASPECT.test(l.trim())).join('\n');
    }
    text = text
      .replace(RE_DATE, '')
      .replace(/\n\s*(?:Вам помог этот отзыв\?.*|[\d\s]+\s*комментар.*)$/s, '')
      .replace(/\s*Да \d+\s*Нет \d+/, '')
      .replace(/\s*\d+\s*комментар\S*/, '')
      .replace(/\n\s*(?:Читать полностью|Читать дальше|Развернуть|Свернуть)\s*$/g, '')
      .trim();
    // имя автора дублируется первой строкой тела текста (DOM: имя отзыва
    // попадает в innerText карточки из-за наложения блока заголовка) — срезаем
    // ведущую строку, если она совпадает с именем автора.
    if (author) {
      const firstLine = (text.split('\n')[0] || '').trim();
      if (firstLine && firstLine === author) {
        text = text.split('\n').slice(1).join('\n').trim();
      }
    }
    const svgs = Array.from(wrapper.querySelectorAll('svg'));
    let stars = 0;
    svgs.forEach((s) => {
      const cls = s.getAttribute('class') || '';
      if (cls.includes('a9')) stars++;
    });
    if (!stars && svgs.length === 5) stars = 0;
    const helpful = fullText.match(/Да (\d+)\s*Нет (\d+)/);
    const comments = fullText.match(/(\d+)\s*комментар\S*/);
    return {
      uuid, publishedAt, author, author2, date, variant, stars,
      stale: isStale(wrapper),
      text: text.slice(0, 20000),
      helpful_yes: helpful ? parseInt(helpful[1], 10) : 0,
      helpful_no: helpful ? parseInt(helpful[2], 10) : 0,
      comments_count: comments ? parseInt(comments[1], 10) : 0,
      comments: parseComments(card),
    };
  };
  const parseComments = (card) => {
    const parent = card.parentElement;
    const out = [];
    if (!parent) return out;
    Array.from(parent.querySelectorAll('[data-comment-uuid]')).forEach((cm) => {
      const author = (cm.querySelector('span.tsCompactControl500Medium')?.innerText || '').trim();
      const replyEl = cm.querySelector('a.rpProduct_c4a') || cm.querySelector('a.l9j_25');
      let replyTo = replyEl ? (replyEl.innerText || '').trim() : '';
      if (/^в ответ/i.test(replyTo)) replyTo = replyTo.replace(/^в ответ\s*/i, '').trim();
      const date = (cm.querySelector('div.rpProduct_a4c')?.innerText || cm.querySelector('div.j9l_25')?.innerText || '').trim();
      const text = (cm.querySelector('span.rpProduct_ca5')?.innerText || cm.querySelector('span.jm0_25')?.innerText || '').trim();
      const votes = Array.from(cm.querySelectorAll('div.tsBodyControl400Small'))
        .map((v) => parseInt((v.innerText || '').trim().replace(/\D/g, ''), 10) || 0);
      out.push({
        uuid: cm.getAttribute('data-comment-uuid'),
        author, reply_to: replyTo, date,
        text: text.slice(0, 20000),
        like: votes[0] || 0,
        dislike: votes[1] || 0,
      });
    });
    return out;
  };
  const reviews = cards.map(extract).filter((r) => r.uuid);
  let pag = null;
  for (let i = lists.length - 1; i >= 0 && !pag; i--) {
    const c = lists[i].children[2];
    if (c && (c.innerText || '').trim()) pag = c;
  }
  let pages = 1;
  if (pag) {
    const txt = (pag.innerText || '');
    const m = txt.match(/(\d+)/);
    if (m && /страниц/i.test(txt)) pages = parseInt(m[1], 10);
  }
  const total = document.querySelector('[data-widget="webReviewProductScore"]');
  return {
    ok: reviews.length > 0,
    reviews,
    pages,
    totalText: total ? total.innerText.trim() : null,
  };
}
