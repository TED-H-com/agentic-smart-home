(function () {
  var root = document.documentElement;
  function language(lang) {
    root.dataset.lang = lang;
    root.lang = lang === 'zh' ? 'zh-Hant' : 'en';
    document.title = root.getAttribute('data-title-' + lang);
    document.querySelector('meta[name="description"]').content = root.getAttribute('data-desc-' + lang);
    document.querySelectorAll('[data-language]').forEach(function (button) { button.setAttribute('aria-pressed', String(button.dataset.language === lang)); });
  }
  language(root.dataset.lang);
  document.querySelectorAll('[data-language]').forEach(function (button) {
    button.addEventListener('click', function () {
      language(button.dataset.language);
      try { localStorage.setItem('tedh-project-lang', button.dataset.language); } catch (_) {}
      var url = new URL(location.href); url.searchParams.set('lang', button.dataset.language === 'zh' ? 'zh-TW' : 'en');
      history.replaceState(null, '', url);
    });
  });
  document.querySelectorAll('[data-screen]').forEach(function (button) {
    button.addEventListener('click', function () {
      document.querySelectorAll('[data-screen]').forEach(function (item) { item.setAttribute('aria-pressed', String(item === button)); });
      document.querySelectorAll('[data-panel]').forEach(function (panel) { panel.hidden = panel.dataset.panel !== button.dataset.screen; });
    });
  });
  var menu = document.querySelector('.menu');
  if (menu) menu.addEventListener('click', function () {
    var open = menu.getAttribute('aria-expanded') !== 'true';
    menu.setAttribute('aria-expanded', String(open)); document.querySelector('.links').classList.toggle('open', open);
  });
})();
