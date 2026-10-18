
() => {
  /*step-down*/
  const before = window.scrollY;
  window.scrollBy({ top: Math.round(window.innerHeight * 2.2), behavior: 'instant' });
  return window.scrollY === before
      || window.scrollY + window.innerHeight
      >= document.documentElement.scrollHeight - 2;
}
