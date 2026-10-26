// Keep Harbor's result pages, but route its New Run link to the GigaCode form.
document.addEventListener('click', event => {
  const link = event.target.closest?.('a[href]');
  if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  const url = new URL(link.href, location.href);
  if (url.origin === location.origin && url.pathname.replace(/\/$/, '') === '/run') {
    event.preventDefault();
    event.stopImmediatePropagation();
    location.assign(url.href);
  }
}, true);
window.addEventListener('popstate', () => {
  if (location.pathname === '/run') location.reload();
});
