
() => {
  const els = document.querySelectorAll('[data-review-uuid]');
  if (els.length) {
    els[els.length - 1].scrollIntoView({ block: 'center' });
  } else {
    window.scrollBy(0, 1500);
  }
}
