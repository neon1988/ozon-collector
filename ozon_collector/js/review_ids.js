
() => {
  return Array.from(document.querySelectorAll('[data-review-uuid]'))
    .map((c) => (c.getAttribute('data-review-uuid') || '').trim())
    .filter(Boolean);
}
