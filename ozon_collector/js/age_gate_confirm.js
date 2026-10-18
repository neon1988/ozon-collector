() => {
  const m = document.querySelector('[data-widget="userAdultModal"]');
  if (!m) return null;
  const inp = m.querySelector('input');
  const btn = [...m.querySelectorAll('button')]
    .find(b => /Подтвердить/.test(b.innerText || ''));
  return {hasModal: true, hasInput: !!inp, hasBtn: !!btn,
          value: inp ? (inp.value || '') : null};
}