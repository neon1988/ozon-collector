() => {
    const q = (sel) => !!document.querySelector(sel);
    const hasAccount = q('a[href*="/my/"]') || q('[data-widget*="webHeaderUser"]')
        || /Мой Ozon|Выйти/i.test((document.body && document.body.innerText) || '');
    const hasLogin = q('a[href*="/auth/"]') || q('[data-widget*="webLogin"]')
        || /(^|\n)\s*Войти\s*(\n|$)/.test((document.body && document.body.innerText) || '');
    return {hasLogin: hasLogin, hasAccount: hasAccount};
}