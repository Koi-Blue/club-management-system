/* Keep the server-rendered navigation shell in place; every page still comes from
   the server, so authorization, flash messages and CSRF tokens stay authoritative. */
(() => {
  'use strict';
  const layout = document.querySelector('.layout[data-user-id]');
  const viewport = document.querySelector('.page-viewport');
  const nav = document.querySelector('.nav');
  let main = viewport?.querySelector('.main');
  if (!layout || !viewport || !nav || !main) return;

  const storageKey = `club-navigation:${layout.dataset.userId}`;
  const mobile = matchMedia('(max-width: 900px)');
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  let saved = { pages: {}, menu: {} };
  try {
    const value = JSON.parse(sessionStorage.getItem(storageKey));
    if (value && typeof value.pages === 'object' && value.pages && value.menu) saved = value;
  } catch { /* Storage may be unavailable in private browsing. */ }
  const newEntry = (depth = 0) => ({ id: `${Date.now()}-${Math.random().toString(36).slice(2)}`, depth, user: layout.dataset.userId });
  const validEntry = entry => entry && typeof entry.id === 'string' && Number.isFinite(entry.depth) && entry.user === layout.dataset.userId;
  let current = validEntry(history.state?.clubNavigation) ? history.state.clubNavigation : newEntry();
  history.replaceState({ ...history.state, clubNavigation: current }, '', location.href);
  let controller;
  let requestNumber = 0;
  let finishTransition = () => {};
  let saveTimer;

  function remember() {
    saved.menu = { x: nav.scrollLeft, y: nav.scrollTop };
    const chart = main.querySelector('.org-chart');
    saved.pages[current.id] = { x: main.scrollLeft, y: main.scrollTop, chartX: chart?.scrollLeft };
    const ids = Object.keys(saved.pages);
    for (const id of ids.slice(0, Math.max(0, ids.length - 50))) delete saved.pages[id];
    try { sessionStorage.setItem(storageKey, JSON.stringify(saved)); } catch { /* Navigation works without storage. */ }
  }

  function scheduleRemember() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(remember, 120);
  }

  function restoreMenu(position = saved.menu) {
    nav.scrollLeft = Number(position?.x) || 0;
    nav.scrollTop = Number(position?.y) || 0;
  }

  function moduleLinks() { return [...nav.querySelectorAll('a[href]')]; }

  function preparePage(position) {
    main.scrollTop = Number(position?.y) || 0;
    main.scrollLeft = Number(position?.x) || 0;
    const chart = main.querySelector('.org-chart');
    if (chart) chart.scrollLeft = Number.isFinite(position?.chartX) ? position.chartX : (chart.scrollWidth - chart.clientWidth) / 2;
    main.dataset.swipeEnabled = String(moduleLinks().some(link => new URL(link.href).pathname === location.pathname));
    for (const link of moduleLinks()) {
      if (link.classList.contains('active')) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
  }

  function showActiveModule() {
    // Only move a mobile strip far enough to reveal the newly selected module.
    // Desktop menu position is never reset or automatically centered.
    if (!mobile.matches) return;
    const active = nav.querySelector('a.active');
    if (!active) return;
    const item = active.getBoundingClientRect();
    const strip = nav.getBoundingClientRect();
    if (item.left < strip.left) nav.scrollLeft -= strip.left - item.left + 8;
    else if (item.right > strip.right) nav.scrollLeft += item.right - strip.right + 8;
  }

  function slide(previous, direction) {
    if (!mobile.matches || reducedMotion.matches || !main.animate) {
      previous.remove();
      return;
    }
    previous.classList.add('page-leaving');
    previous.setAttribute('aria-hidden', 'true');
    previous.inert = true;
    // Full-width, simultaneous movement; the navigation bar stays stationary.
    const options = { duration: 240, easing: 'cubic-bezier(.22,.7,.25,1)', fill: 'both' };
    const animations = [
      previous.animate([{ transform: 'translateX(0)' }, { transform: `translateX(${-direction * 100}%)` }], options),
      main.animate([{ transform: `translateX(${direction * 100}%)` }, { transform: 'translateX(0)' }], options),
    ];
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      previous.remove();
      for (const animation of animations) animation.cancel();
      if (finishTransition === finish) finishTransition = () => {};
    };
    finishTransition = finish;
    Promise.allSettled(animations.map(animation => animation.finished)).then(finish);
  }

  function directionTo(url, entry) {
    if (entry) return entry.depth < current.depth ? -1 : 1;
    if (url.pathname !== '/' && location.pathname.startsWith(`${url.pathname}/`)) return -1;
    const links = moduleLinks();
    const from = links.findIndex(link => link.classList.contains('active'));
    const to = links.findIndex(link => new URL(link.href).pathname === url.pathname);
    return from >= 0 && to >= 0 && to < from ? -1 : 1;
  }

  async function navigate(url, entry = null) {
    const number = ++requestNumber;
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    const timeout = setTimeout(() => controller?.signal === signal && controller.abort(), 15000);
    const direction = directionTo(url, entry);
    remember();
    finishTransition();
    viewport.setAttribute('aria-busy', 'true');
    const status = viewport.querySelector('.navigation-status');
    status.textContent = '正在加载…';
    try {
      const response = await fetch(url.href, { credentials: 'same-origin', cache: 'no-store', signal, headers: { Accept: 'text/html' } });
      if (number !== requestNumber) return;
      if (!response.ok || !response.headers.get('Content-Type')?.includes('text/html')) throw new Error('Use normal navigation');
      const documentNext = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (number !== requestNumber) return;
      const shellNext = documentNext.querySelector('.layout[data-user-id]');
      const mainNext = documentNext.querySelector('.page-viewport > .main');
      const navNext = documentNext.querySelector('.nav');
      const finalUrl = new URL(response.url || url.href);
      if (!shellNext || !mainNext || !navNext || mainNext.querySelector('script') ||
          shellNext.dataset.userId !== layout.dataset.userId || shellNext.dataset.shellVersion !== layout.dataset.shellVersion ||
          finalUrl.origin !== location.origin) {
        location.assign(finalUrl.href);
        return;
      }
      remember();
      const menuPosition = { ...saved.menu };
      const nextEntry = entry || newEntry(current.depth + 1);
      if (entry) history.replaceState({ ...history.state, clubNavigation: nextEntry }, '', finalUrl.href);
      else history.pushState({ clubNavigation: nextEntry }, '', finalUrl.href);
      current = nextEntry;
      document.title = documentNext.title;
      nav.replaceChildren(...navNext.childNodes);
      for (const selector of ['.brand', '.side-user']) {
        const currentPart = layout.querySelector(selector);
        const nextPart = shellNext.querySelector(selector);
        if (currentPart && nextPart) currentPart.replaceChildren(...nextPart.childNodes);
      }
      restoreMenu(menuPosition);
      const previous = main;
      main = document.importNode(mainNext, true);
      viewport.insertBefore(main, status);
      preparePage(entry ? saved.pages[entry.id] : null);
      if (!entry) showActiveModule();
      slide(previous, direction);
      main.focus({ preventScroll: true });
      if (finalUrl.hash) {
        const target = main.querySelector(`#${CSS.escape(decodeURIComponent(finalUrl.hash.slice(1)))}`);
        target?.scrollIntoView({ block: 'start' });
      }
      remember();
    } catch {
      if (number === requestNumber) location.assign(url.href);
    } finally {
      clearTimeout(timeout);
      if (number === requestNumber) {
        viewport.removeAttribute('aria-busy');
        status.textContent = '';
      }
    }
  }

  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey ||
        link.hasAttribute('download') || (link.target && link.target !== '_self') ||
        !(link.matches('[data-page-link]') || link.closest('.nav'))) return;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin || !['http:', 'https:'].includes(url.protocol)) return;
    if (!viewport.hasAttribute('aria-busy') && url.pathname === location.pathname && url.search === location.search) {
      if (!url.hash) {
        event.preventDefault();
        ++requestNumber;
        controller?.abort();
        finishTransition();
        viewport.removeAttribute('aria-busy');
        viewport.querySelector('.navigation-status').textContent = '';
        remember();
      }
      return;
    }
    event.preventDefault();
    navigate(url);
  });

  addEventListener('popstate', event => {
    if (!validEntry(event.state?.clubNavigation)) { location.reload(); return; }
    navigate(new URL(location.href), event.state.clubNavigation);
  });
  addEventListener('pagehide', remember);
  document.addEventListener('submit', remember, true); // POSTs/uploads/logout remain ordinary form submissions.
  document.addEventListener('scroll', scheduleRemember, true);
  viewport.addEventListener('input', () => { main.dataset.dirty = 'true'; });
  viewport.addEventListener('change', () => { main.dataset.dirty = 'true'; });
  addEventListener('pageshow', event => {
    if (event.persisted) {
      if (validEntry(history.state?.clubNavigation)) current = history.state.clubNavigation;
      restoreMenu();
    }
  });

  let gesture;
  viewport.addEventListener('pointerdown', event => {
    gesture = null;
    if (!mobile.matches || event.pointerType !== 'touch' || !event.isPrimary || main.dataset.swipeEnabled !== 'true' || main.dataset.dirty ||
        viewport.getAttribute('aria-busy') === 'true' || event.clientX < 24 || event.clientX > innerWidth - 24 ||
        event.target.closest('a, button, input, textarea, select, label, details, table, pre, .prose, .org-chart')) return;
    gesture = { id: event.pointerId, x: event.clientX, y: event.clientY, at: performance.now() };
  });
  viewport.addEventListener('pointercancel', () => { gesture = null; });
  viewport.addEventListener('pointerup', event => {
    const start = gesture;
    gesture = null;
    if (!start || start.id !== event.pointerId || performance.now() - start.at > 900 || getSelection()?.toString()) return;
    const dx = event.clientX - start.x;
    const dy = event.clientY - start.y;
    if (Math.abs(dx) < 80 || Math.abs(dx) < Math.abs(dy) * 1.8) return;
    const links = moduleLinks();
    const index = links.findIndex(link => link.classList.contains('active'));
    const target = links[index + (dx < 0 ? 1 : -1)];
    if (index >= 0 && target) navigate(new URL(target.href));
  });

  restoreMenu();
  preparePage(saved.pages[current.id]);
})();
