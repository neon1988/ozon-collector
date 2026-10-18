
() => {
  const w = document.querySelector('[data-widget="webReviewsVariantMode"]');
  const totalEl = document.querySelector('[data-widget="webReviewProductScore"]');
  const tm = (totalEl ? (totalEl.innerText || '') : '').match(/(\d[\d\s]*)/);
  const total_count = tm ? parseInt(tm[1].replace(/\s/g, ''), 10) : null;
  if (!w) {
    return { found: false, options: [], variant_present: false,
             variant_count: null, active: null, total_count };
  }
  const activeByClass = (cls) => /(?:^|\s)[\w\d-]*-a8(?:\s|$)/.test(cls);
  const opts = Array.from(w.querySelectorAll('div[tabindex="0"]')).map((o) => {
    const text = (o.innerText || '').replace(/\s+/g, ' ').trim();
    const cls = String(o.className || '');
    const bg = getComputedStyle(o).backgroundColor;
    const active = activeByClass(cls)
      || (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent');
    return { text, active };
  });
  const variantOpt = opts.find((o) => /^Этот вариант товара/.test(o.text));
  const variant_count = variantOpt
    ? (variantOpt.text.match(/\((\d+)\)/) ? parseInt(variantOpt.text.match(/\((\d+)\)/)[1], 10) : null)
    : null;
  const activeOpt = opts.find((o) => o.active);
  return {
    found: true,
    options: opts.map((o) => o.text),
    variant_present: !!variantOpt,
    variant_count,
    active: activeOpt ? activeOpt.text.split(' (')[0] : null,
    total_count,
  };
}
