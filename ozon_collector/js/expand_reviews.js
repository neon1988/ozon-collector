
() => {
  const RE = /^(Читать полностью|Читать дальше|Развернуть)$/;
  const clicked = new Set();
  let total = 0;
  for (let pass = 0; pass < 20; pass++) {
    let got = 0;
    const btns = Array.from(document.querySelectorAll('[data-review-uuid] *'))
      .filter((el) => {
        const t = (el.innerText || '').trim();
        if (!RE.test(t)) return false;
        if (clicked.has(el)) return false;
        const kids = Array.from(el.children);
        return !kids.some((k) => RE.test((k.innerText || '').trim()));
      });
    for (const b of btns) {
      clicked.add(b);
      try { b.click(); got++; } catch (e) {}
    }
    if (!got) break;
    total += got;
  }
  return total;
}
