
(opts) => {
  const T = document.body.innerText;
  const parse = (v) => {
    const f = parseFloat(String(v).replace(/\s/g, '').replace(',', '.'));
    return isNaN(f) ? null : f;
  };
  // Явный ноль отзывов из state-JSON виджета webReviewProductScore
  // (блок «Отзывы о товаре 0» машинно отдаёт reviewsCount/totalScore=0).
  const reviewZero = () => {
    const st = document.querySelector('[id^="state-webReviewProductScore"]');
    if (!st) return false;
    try {
      const state = JSON.parse(st.getAttribute('data-state'));
      const rcVal = state && (state.reviewsCount ?? state.totalScore);
      return typeof rcVal === 'number' && rcVal <= 0;
    } catch (e) {
      return false;
    }
  };
  const title = () => {
    const h1 = document.querySelector('h1');
    if (h1) return h1.innerText.trim();
    const m = T.match(/(?:\n|^)([^\n]{15,160})\n/);
    return m ? m[1].trim() : null;
  };
  const SCORE_WIDGETS = ["webProductScore", "webSingleProductScore", "webReviewProductScore"];
  const rating = () => {
    if (reviewZero()) return { value: 0, source: "no_reviews_widget" };
    for (const wname of SCORE_WIDGETS) {
      const w = document.querySelector('[data-widget="' + wname + '"]');
      if (!w) continue;
      const text = String(w.innerText);
      let m = text.match(/([\d.,]+)\s*\/\s*5/);
      if (m) {
        const v = parseFloat(m[1].replace(',', '.'));
        if (v > 0 && v <= 5) return { value: v, source: wname };
      }
      m = text.match(/([\d.,]+)\s*[•·]/);
      if (m) {
        const v = parseFloat(m[1].replace(',', '.'));
        if (v > 0 && v <= 5) return { value: v, source: wname };
      }
    }
    const m = T.match(/([\d.,]+)\s*\/\s*5\s*зв[её]зд/);
    if (m) {
      const v = parseFloat(m[1].replace(',', '.'));
      if (v > 0 && v <= 5) return { value: v, source: 'fallback_text' };
    }
    return null;
  };
  const ratingCount = () => {
    if (reviewZero()) return 0;
    for (const wname of SCORE_WIDGETS) {
      const w = document.querySelector('[data-widget="' + wname + '"]');
      if (!w) continue;
      const text = String(w.innerText);
      let m = text.match(/([\d\s\u00a0]+)\s*(?:оцен(?:ка|ок|ки))/i);
      if (m) return parse(m[1]);
      m = text.match(/([\d\s\u00a0]+)\s*отзыв/i);
      if (m) return parse(m[1]);
      const nums = [];
      const re = /\d+(?:[\s\u00a0]\d+)*/g;
      let mm;
      while ((mm = re.exec(text)) !== null) {
        const digits = mm[0].replace(/[\s\u00a0]/g, '');
        const num = parseInt(digits, 10);
        if (num > 5 && digits.length >= 2) nums.push(num);
      }
      if (nums.length) return Math.max(...nums);
    }
    const m = T.match(/([\d\s]+)\s*оцен(?:ка|ок|ки)/);
    return m ? parse(m[1]) : null;
  };
  const CHAR_WIDGETS = ["webCharacteristics", "webShortCharacteristics"];
  // Ozon 2026-10: hashed classes, no dl. A row is a div with exactly two DIV
  // children — the key cell and the value cell (brand is wrapped in <a>).
  const CHAR_HEADER = ['О товаре', 'Перейти к описанию'];
  const charRows = (w) => {
    const out = {};
    for (const row of w.querySelectorAll('div')) {
      const cells = [];
      for (const ch of row.children) {
        if (ch.tagName === 'DIV') cells.push(ch);
      }
      if (cells.length !== 2) continue;
      const k = cells[0].innerText.trim();
      const v = cells[1].innerText.trim();
      if (!k || !v || k.includes('\n')) continue;
      if (CHAR_HEADER.indexOf(k) !== -1) continue;
      if (k.length > 60 || v.length > 300) continue;
      if (!out[k]) out[k] = v;
    }
    return out;
  };
  const characteristics = () => {
    const out = {};
    const grab = (dl) => {
      const dt = dl.querySelector('dt');
      const dd = dl.querySelector('dd');
      if (dt && dd) {
        const k = dt.innerText.trim();
        const v = dd.innerText.trim();
        if (k && v && !out[k]) out[k] = v;
      }
    };
    const merge = (src) => {
      for (const k in src) {
        if (src[k] && !out[k]) out[k] = src[k];
      }
    };
    for (const wname of CHAR_WIDGETS) {
      const w = document.querySelector('[data-widget="' + wname + '"]');
      if (!w) continue;
      w.querySelectorAll('dl').forEach(grab);
      if (Object.keys(out).length > 0) return out;
    }
    // Structural rows beat the global dl/Row fallbacks: those can pick up
    // unrelated lists (nav/footer) and return junk before we ever look here.
    // Even one scoped row wins: the widget is scoped, a global dl is not.
    for (const wname of CHAR_WIDGETS) {
      const w = document.querySelector('[data-widget="' + wname + '"]');
      if (!w) continue;
      merge(charRows(w));
    }
    if (Object.keys(out).length > 0) return out;
    document.querySelectorAll('dl.tsBodyMBold').forEach(grab);
    if (Object.keys(out).length > 0) return out;
    document.querySelectorAll('dl').forEach((dl) => {
      const dt = dl.querySelector(':scope > dt');
      const dd = dl.querySelector(':scope > dd');
      if (dt && dd) {
        const k = dt.innerText.trim();
        const v = dd.innerText.trim();
        if (k && v && !out[k]) out[k] = v;
      }
    });
    if (Object.keys(out).length >= 5) return out;
    for (const wname of CHAR_WIDGETS) {
      const w = document.querySelector('[data-widget="' + wname + '"]');
      if (!w) continue;
      w.querySelectorAll('[class*="Row"]').forEach((r) => {
        const t = r.innerText.split('\n');
        if (t.length >= 2) {
          const k = t[0].trim();
          const v = t[t.length - 1].trim();
          if (k && v && !out[k]) out[k] = v;
        }
      });
    }
    // Last resort, opt-in only (owner 2026-10-19): pair consecutive lines of
    // the widget text. Generic text can look like a key/value pair, so this
    // never runs unless OZON_CHAR_TEXT_FALLBACK is set.
    if (opts && opts.char_text_fallback) {
      for (const wname of CHAR_WIDGETS) {
        const w = document.querySelector('[data-widget="' + wname + '"]');
        if (!w) continue;
        // Drop header lines BEFORE pairing: one leftover line shifts every
        // pair and invents rows (review 2026-10-19).
        const lines = String(w.innerText).split('\n')
          .map((s) => s.trim()).filter((s) => s)
          .filter((s) => CHAR_HEADER.indexOf(s) === -1);
        for (let i = 0; i + 1 < lines.length; i += 2) {
          const k = lines[i];
          const v = lines[i + 1];
          if (k.length > 60 || v.length > 300) continue;
          if (!out[k]) out[k] = v;
        }
        if (Object.keys(out).length >= 3) return out;
      }
    }
    return out;
  };
  // Metadata for the retry decision: a culled full widget returns {} while
  // the short widget may still yield 1-2 rows. Without this the collector
  // accepted the short subset as final (review 2026-10-19).
  const charMeta = () => {
    const full = document.querySelector('[data-widget="webCharacteristics"]');
    const short = document.querySelector('[data-widget="webShortCharacteristics"]');
    const fullRows = full ? Object.keys(charRows(full)).length : 0;
    const shortRows = short ? Object.keys(charRows(short)).length : 0;
    return {
      full_present: !!full,
      full_rows: fullRows,
      full_empty: !!full && !String(full.innerText || '').trim(),
      short_present: !!short,
      short_rows: shortRows,
      source: fullRows ? 'full' : (shortRows ? 'short' : ''),
    };
  };
  const gallery = () => {
    const out = [];
    const push = (i) => {
      const s = i.currentSrc || i.src || '';
      if (s && !out.includes(s) && !/\/video-[^/]+\/[^/]+\/cover\//.test(s)) out.push(s);
    };
    const GALLERY_WIDGETS = ['webGallery', 'webDescription', 'webProductDescription'];
    let found = 0;
    for (const wname of GALLERY_WIDGETS) {
      const w = document.querySelector('[data-widget="' + wname + '"]');
      if (!w) continue;
      w.querySelectorAll('img').forEach(push);
      found++;
    }
    if (found === 0) {
      document.querySelectorAll('img[src*="cdn1.ozone.ru"], img[src*="ir.ozone.ru"]')
        .forEach(push);
    }
    return out;
  };

  const DESC_WIDGETS = ["webProductDescription", "webDescription"];
  const descriptions = () => {
    const out = [];
    for (const wname of DESC_WIDGETS) {
      const w = document.querySelector('[data-widget="' + wname + '"]');
      if (!w) continue;
      const t = w.innerText.trim();
      if (t) out.push(t);
      if (out.length) return out;
    }
    document.querySelectorAll('[class*="ProductDescription"], [class*="description"]').forEach((n) => {
      const t = n.innerText.trim();
      if (t && t.length > 60) out.push(t);
    });
    if (out.length) return out;
    const charWidget = document.querySelector('[data-widget="webCharacteristics"]');
    if (charWidget) {
      let next = charWidget.nextElementSibling;
      while (next) {
        const t = next.innerText.trim();
        if (t.length > 100) {
          out.push(t);
          break;
        }
        next = next.nextElementSibling;
      }
    }
    return out;
  };

  const infographics = () => {
    const out = [];
    const GALLERY_WIDGETS = ['webMediaViewer', 'webGallery', 'webDescription', 'webProductDescription'];
    for (const wname of GALLERY_WIDGETS) {
      const w = document.querySelector('[data-widget="' + wname + '"]');
      if (!w) continue;
      w.querySelectorAll('img[src*="info"], img[src*="schema"]').forEach((i) => {
        const s = i.currentSrc || i.src || '';
        if (s) out.push(s);
      });
    }
    return out;
  };

  // ---- price: структурированная цена (TreeWalker по текстовым узлам) ----
  const priceParts = () => {
    const parts = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      const t = (n.textContent || '').replace(/\s+/g, ' ').trim();
      if (!t) continue;
      const p = n.parentElement;
      if (!p) continue;
      const st = p.getBoundingClientRect ? p.getBoundingClientRect() : null;
      let fs = 0;
      const cs = p && window.getComputedStyle ? window.getComputedStyle(p) : null;
      if (cs && cs.fontSize) fs = parseFloat(cs.fontSize) || 0;
      let weight = 0;
      if (cs && cs.fontWeight) weight = parseInt(cs.fontWeight, 10) || 0;
      const strike = !!(p.classList && (p.classList.contains('_8mQ_f') || (cs && cs.textDecorationLine && cs.textDecorationLine.includes('line-through'))));
      if (/[₽]|руб|р\.\s*$/.test(t) && t.length < 25) {
        parts.push({ t, y: st ? st.y : 0, x: st ? st.x : 0, fs, weight, strike });
      }
    }
    parts.sort((a, b) => (b.fs * (b.weight ? 2 : 1)) - (a.fs * (a.weight ? 2 : 1)));
    return parts.slice(0, 8);
  };

  const priceParsed = () => {
    const els = Array.from(document.querySelectorAll('[data-widget="webPrice"] [class*="mainPrice"], [class*="tsHeadline500Medium"]'));
    const seen = new Set();
    const out = [];
    els.forEach((e) => {
      const t = (e.innerText || '').replace(/\s+/g, ' ').trim();
      if (!t || seen.has(t)) return;
      seen.add(t);
      const n = parse(t);
      if (n) out.push({ raw: t, value: n });
    });
    return out;
  };

  const price_text = () => {
    const m = T.match(/[\d\s]{3,}\s*[₽р]/);
    return m ? m[0].trim() : null;
  };

  // ---- price: точная цена из виджета webPrice ----
  // current — крупная не-кнопочная цена (fs 20, bold); old — зачёркнутая/меньшая
  // рядом; bank — цена в кнопке банка («С банками»); рассрочки/аксессуары вне
  // виджета webPrice не попадают вовсе.
  const priceExtract = () => {
    const w = document.querySelector('[data-widget="webPrice"]');
    const res = { current: null, old: null, bank: null, confidence: 0, source: null };
    if (!w) return res;
    const priceRe = /^\s*[\d\s\u2009\u00a0]{1,12}\s*[₽р]\s*$/;
    const items = [];
    const walk = (el, depth) => {
      if (!el || depth > 14) return;
      const direct = el.childNodes.length === 1 && el.childNodes[0].nodeType === 3;
      const txt = direct ? (el.textContent || '').trim() : '';
      if (txt && priceRe.test(txt)) {
        const cs = getComputedStyle(el);
        let node = el, inBtn = false;
        for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
          if (node.tagName === 'BUTTON') { inBtn = true; break; }
        }
        items.push({ txt, fs: parseFloat(cs.fontSize) || 0, w: parseInt(cs.fontWeight, 10) || 0,
          strike: (cs.textDecorationLine || '').includes('line-through'), inBtn });
      }
      for (const c of el.children) walk(c, depth + 1);
    };
    walk(w, 0);
    const num = (s) => parseInt(String(s || '').replace(/[\s\u2009\u00a0]/g, '').replace(/[₽р]/g, ''), 10);
    const main = items.filter((i) => !i.inBtn).sort((a, b) => (b.fs * 100 + b.w) - (a.fs * 100 + a.w));
    const cur = main[0];
    if (!cur) return res;
    res.current = num(cur.txt);
    res.source = 'webPrice';
    const oldCand = main.slice(1).filter((i) => i.strike || i.fs < cur.fs);
    if (oldCand.length) res.old = num(oldCand[0].txt);
    const bank = items.find((i) => i.inBtn);
    if (bank) res.bank = num(bank.txt);
    res.confidence = cur.fs >= 16 ? 0.97 : 0.85;
    return res;
  };

  const ratingData = rating();

  // ---- «По мнению покупателей»: % рекомендуют + разбивка оценок по звёздам ----
  const customerOpinion = () => {
    const out = { recommend_percent: null, stars: {}, counts: {}, text: null };
    const starRe = /^([1-5])\s*зв[её]зд[а-я]*$/i;
    const rows = [];
    for (const el of document.querySelectorAll('div, span')) {
      const t = (el.innerText || '').trim();
      const m = starRe.exec(t);
      if (m && t.length <= 20) {
        const row = el.parentElement;
        if (row) rows.push([parseInt(m[1], 10), row]);
      }
    }
    rows.forEach(([star, row]) => {
      // процент из style="width: N%" внутри строки гистограммы
      const wEl = row.querySelector('[style*="width"]');
      const wm = wEl && (wEl.getAttribute('style') || '').match(/width:\s*([\d.]+)%/);
      if (wm) out.stars[star] = parseFloat(wm[1]);
      // количество отзывов — число в конце текста строки
      const t2 = (row.innerText || '').replace(/\s+/g, ' ').trim();
      const cm = t2.match(/(\d[\d\s\u00a0]*)$/);
      if (cm) out.counts[star] = parseInt(cm[1].replace(/[\s\u00a0]/g, ''), 10);
    });
    // «N% покупателей рекомендуют» / «N% рекомендуют» по всей странице
    if (!Object.keys(out.stars).length || out.recommend_percent === null) {
      const pageTxt = document.body ? (document.body.innerText || '') : '';
      const rec = pageTxt.match(/(\d{1,3})\s*%\s*покупател/i)
              || pageTxt.match(/покупател[^\n%]{0,25}\s*(\d{1,3})\s*%/i)
              || pageTxt.match(/(\d{1,3})\s*%\s*рекомендуют/i);
      if (rec) out.recommend_percent = parseInt(rec[1], 10);
    }
    // fallback: пары «N» + «M%» подряд (гистограмма без слов «звезда»)
    if (!Object.keys(out.stars).length) {
      const allEls = Array.from(document.querySelectorAll(
        'span, div, [class*="score"], [class*="bar"], [class*="percent"]'));
      const texts = allEls.map((n) => (n.innerText || '').trim()).filter(Boolean);
      for (let i = 0; i + 1 < texts.length; i++) {
        const star = /^([1-5])$/.exec(texts[i]);
        const pct = /^(\d{1,3})\s*%$/.exec(texts[i + 1]);
        if (star && pct) out.stars[parseInt(star[1], 10)] = parseInt(pct[1], 10);
      }
    }
    out.text = (document.body ? (document.body.innerText || '') : '')
      .match(/По\s+мнению\s+покупателей[\s\S]{0,1200}/)?.[0]
      || '';
    return out;
  };

  return {
    title: title(),
    rating: ratingData ? ratingData.value : null,
    rating_source: ratingData ? ratingData.source : null,
    rating_count: ratingCount(),
    price_text: price_text(),
    price_parts: priceParts(),
    price_parsed: priceParsed(),
    price: priceExtract(),
    characteristics: characteristics(),
    char_meta: charMeta(),
    gallery: gallery(),
    descriptions: descriptions(),
    infographics: infographics(),
    customer_opinion: customerOpinion(),
  };
}
