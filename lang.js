// Tencent WorkBuddy Bench bilingual toggle — shared by detail pages and index.
(function () {
  function set(lang) {
    var l = (lang === 'zh') ? 'zh' : 'en';
    document.body.classList.remove('lang-en', 'lang-zh');
    document.body.classList.add('lang-' + l);
    try { localStorage.setItem('cbLang', l); } catch (e) {}
    document.querySelectorAll('[data-lang-btn]').forEach(function (b) {
      b.classList.toggle('active', b.getAttribute('data-lang-btn') === l);
    });
  }
  window.cbSetLang = set;
  document.addEventListener('DOMContentLoaded', function () {
    var saved = 'zh';
    try { saved = localStorage.getItem('cbLang') || 'zh'; } catch (e) {}
    var m = /[?&]lang=(en|zh)/.exec(window.location.search);
    if (m) saved = m[1];
    set(saved);
    document.querySelectorAll('[data-lang-btn]').forEach(function (b) {
      b.addEventListener('click', function () { set(b.getAttribute('data-lang-btn')); });
    });
  });
})();
