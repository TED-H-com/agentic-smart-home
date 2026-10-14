/* Choose before paint; no external scripts or fonts. */
(function () {
  var root = document.documentElement;
  root.classList.add('js');
  function normalize(value) {
    return /^zh/i.test(value || '') ? 'zh' : /^en/i.test(value || '') ? 'en' : null;
  }
  var saved;
  try { saved = localStorage.getItem('tedh-project-lang'); } catch (_) {}
  var browser = (navigator.languages || [navigator.language]).some(function (v) { return /^zh/i.test(v); }) ? 'zh' : 'en';
  var lang = normalize(new URLSearchParams(location.search).get('lang')) || normalize(saved) || browser;
  root.dataset.lang = lang;
  root.lang = lang === 'zh' ? 'zh-Hant' : 'en';
  document.title = root.getAttribute('data-title-' + lang);
  var description = document.querySelector('meta[name="description"]');
  if (description) description.content = root.getAttribute('data-desc-' + lang);
})();
