
() => ({
  ready: document.readyState,
  y: Math.round(window.scrollY),
  h: document.documentElement.scrollHeight,
  ih: window.innerHeight,
  body: document.body ? document.body.innerText.length : 0,
  widgets: document.querySelectorAll('div[data-widget]').length,
  scripts: document.scripts.length,
})
