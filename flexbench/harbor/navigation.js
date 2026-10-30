// Shared navigation and measurements, without modifying Harbor's assets.
(() => {
  const jobPattern = /^(?:gigacode-\d{8}T\d{6}-[0-9a-f]{8}|experiment-\d{8}T\d{6}-[0-9a-f]{8}-attempt-\d{4,}-[a-z]+)$/;
  const experimentPattern = /^experiment-\d{8}T\d{6}-[0-9a-f]{8}$/;
  let routeKey, timer, scheduled = false;
  const storage = {
    get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* optional */ } }
  };
  const route = () => location.pathname.match(/^\/jobs\/([^/]+)(?:\/trials\/([^/]+))?\/?$/);
  const currentJob = () => {
    const value = route()?.[1] || storage.get('flexbench.activeJob');
    const experiment = value?.match(/^(experiment-\d{8}T\d{6}-[0-9a-f]{8})-attempt-/)?.[1];
    if (experiment) return experiment;
    return jobPattern.test(value || '') || experimentPattern.test(value || '') ? value : null;
  };
  function theme() {
    if (!document.body.classList.contains('fb-launcher')) return;
    const preference = storage.get('theme') || 'system';
    const dark = preference === 'dark' || (preference === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.classList.toggle('dark', dark);
    document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
  }
  function nav() {
    const header = document.querySelector('header');
    if (!header) return;
    let links = document.getElementById('flexbench-nav');
    if (!links) {
      const toolbar = header.firstElementChild;
      // Keep Harbor's login controls and React tree. Hide only its original
      // launcher slot and add the common navigation alongside it.
      if (!document.body.classList.contains('fb-launcher')) toolbar.firstElementChild?.classList.add('fb-native-launch');
      links = document.createElement('nav'); links.id = 'flexbench-nav';
      links.className = 'fb-nav'; links.setAttribute('aria-label', 'FlexBench и Harbor');
      const brand = document.createElement('span'); brand.className = 'fb-brand'; brand.textContent = 'Harbor / GigaCode'; links.append(brand);
      for (const [id, label, href] of [['new', 'Новый прогон', '/run'], ['history', 'Прогоны Harbor', '/'], ['current', 'Текущий прогон', '#']]) {
        const link = document.createElement('a'); link.id = `fb-nav-${id}`; link.textContent = label; link.href = href; links.append(link);
      }
      toolbar.prepend(links);
    }
    const job = currentJob(), current = document.getElementById('fb-nav-current');
    current.hidden = !job;
    const currentUrl = experimentPattern.test(job || '') ? '/run?experiment=' + job : '/jobs/' + job;
    const label = experimentPattern.test(job || '') ? 'Текущий эксперимент' : 'Текущий прогон';
    if (current.textContent !== label) current.textContent = label;
    if (job && current.getAttribute('href') !== currentUrl) current.setAttribute('href', currentUrl);
    const active = location.pathname.startsWith('/run') ? 'new' : route() ? 'current' : 'history';
    for (const id of ['new', 'history', 'current']) {
      const link = document.getElementById(`fb-nav-${id}`);
      if (id === active) {
        if (!link.hasAttribute('aria-current')) link.setAttribute('aria-current', 'page');
      } else if (link.hasAttribute('aria-current')) link.removeAttribute('aria-current');
    }
  }
  function placeMetrics(section) {
    const tabs = document.querySelector('main [role="tablist"]')?.parentElement;
    if (tabs && tabs.previousElementSibling !== section) tabs.before(section);
  }
  function metrics() {
    const match = route(), key = match ? match[0] : '';
    const existing = document.getElementById('flexbench-metrics');
    if (key === routeKey && (!key || existing)) {
      if (existing) placeMetrics(existing);
      return;
    }
    routeKey = key; clearTimeout(timer);
    document.getElementById('flexbench-metrics')?.remove();
    if (!match || !jobPattern.test(match[1])) return;
    const main = document.querySelector('main');
    if (!main) { routeKey = undefined; return; }
    storage.set('flexbench.activeJob', match[1]);
    const section = document.createElement('section'); section.id = 'flexbench-metrics'; section.className = 'fb-panel fb-native-metrics';
    const heading = document.createElement('h2'); heading.textContent = match[2] ? 'Токены этой попытки' : 'Токены прогона';
    const content = document.createElement('div'); content.className = 'fb-metrics';
    content.textContent = 'Загружаем измерения GigaCode…'; section.append(heading, content); main.prepend(section);
    placeMetrics(section);
    async function refresh() {
      try {
        const suffix = match[2] ? '?trial=' + encodeURIComponent(decodeURIComponent(match[2])) : '';
        const response = await fetch(`/api/flexbench/jobs/${encodeURIComponent(match[1])}/summary${suffix}`);
        if (location.pathname !== key || !section.isConnected) return;
        if (!response.ok) {
          content.textContent = response.status === 404 ? 'Измерения появятся после завершения прогона.' : 'Не удалось загрузить измерения. Повторяем запрос…';
          timer = setTimeout(refresh, 5000); return;
        }
        window.flexbenchSummary(content, await response.json());
      } catch (_) {
        if (location.pathname === key) timer = setTimeout(refresh, 5000);
      }
    }
    refresh();
  }
  function update() { scheduled = false; nav(); metrics(); }
  function schedule() { if (!scheduled) { scheduled = true; requestAnimationFrame(update); } }
  document.addEventListener('click', event => {
    const link = event.target.closest?.('a[href]');
    if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const url = new URL(link.href, location.href);
    if (url.origin === location.origin && url.pathname.replace(/\/$/, '') === '/run') {
      event.preventDefault(); event.stopImmediatePropagation();
      const job = currentJob();
      if (job && !url.searchParams.has('experiment') && !url.searchParams.has('job')) {
        url.searchParams.set(experimentPattern.test(job) ? 'experiment' : 'job', job);
      }
      location.assign(url.href);
    }
  }, true);
  window.addEventListener('popstate', () => {
    if (location.pathname.replace(/\/$/, '') === '/run') location.reload(); else schedule();
  });
  window.addEventListener('flexbench:active-job', schedule);
  window.addEventListener('storage', event => { if (event.key === 'theme') theme(); else schedule(); });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', theme);
  theme(); update();
  new MutationObserver(schedule).observe(document.body, {childList: true, subtree: true});
})();
