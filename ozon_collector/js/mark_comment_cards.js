
() => {
  // Помечает карточки отзывов, у которых есть нераскрытая кнопка
  // «N комментарий» (N>=1). Сами клики делает Playwright (trusted) —
  // untrusted dispatchEvent игнорируется React (A/B-эксперимент 6/62 vs 31/31).
  const RE = /^\s*(\d+)\s*комментар/i;
  let total = 0;
  for (const b of document.querySelectorAll('button')) {
    const m = (b.innerText || '').trim().match(RE);
    if (!m || parseInt(m[1], 10) < 1) continue;
    const card = b.closest('[data-review-uuid]');
    if (!card || card.hasAttribute('data-comments-expanded')) continue;
    if (card.querySelector('[data-comment-uuid]')) continue;
    card.setAttribute('data-comments-expanded', '1');
    total++;
  }
  return total;
}
