() => {
  const b = (document.body && document.body.innerText) || '';
  const gate = /Подтвердите возраст/.test(b);
  const ready = !!document.querySelector(
    '[data-widget="webPrice"],[data-widget="webRating"],'
    + '[data-widget="webReviews"],a[href*="tab=reviews"]');
  return {gate: gate, ready: ready};
}