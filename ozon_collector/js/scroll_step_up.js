
() => {
  /*step-up*/
  const before = window.scrollY;
  window.scrollBy({ top: -Math.round(window.innerHeight * 2.2), behavior: 'instant' });
  return window.scrollY <= 0 || window.scrollY === before;
}
