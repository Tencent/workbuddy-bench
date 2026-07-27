/* ============================================================
   site.js — shared bottom-of-body script for all pages
   (theme-init stays inline in <head> on every page — it must
   run before first paint to avoid a flash of the wrong theme)
   ============================================================ */

/* theme toggle */
(function(){
  var b = document.getElementById('themeToggle');
  if (!b) return;
  b.addEventListener('click', function(){
    var c = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', c);
    try { localStorage.setItem('cbTheme', c); } catch(e) {}
  });
})();

/* ============================================================
   TOC scrollspy — highlight the section currently in view
   No-ops gracefully on pages without .toc-nav (code/web/office/security have it).
   ============================================================ */
(function(){
  var links = Array.prototype.slice.call(document.querySelectorAll('.toc-nav a'));
  if (!links.length) return;
  var map = {};                 // id -> link
  var targets = [];             // observed section elements
  links.forEach(function(a){
    var id = a.getAttribute('href').slice(1);
    var el = document.getElementById(id);
    if (el){ map[id] = a; targets.push(el); }
  });
  if (!targets.length) return;

  function setActive(id){
    links.forEach(function(a){ a.classList.remove('is-active'); });
    if (map[id]) map[id].classList.add('is-active');
  }

  // pick the last section whose top has passed the sticky-header line
  var lineOffset = 0;
  (function(){
    var css = getComputedStyle(document.documentElement).getPropertyValue('--topbar-h');
    lineOffset = (parseInt(css, 10) || 60) + 24;
  })();

  function onScroll(){
    var line = lineOffset + 1, current = targets[0].id;
    for (var i = 0; i < targets.length; i++){
      if (targets[i].getBoundingClientRect().top <= line) current = targets[i].id;
      else break;
    }
    // near page bottom → force-activate the last item
    if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 2){
      current = targets[targets.length - 1].id;
    }
    setActive(current);
  }

  var ticking = false;
  window.addEventListener('scroll', function(){
    if (ticking) return;
    ticking = true;
    window.requestAnimationFrame(function(){ onScroll(); ticking = false; });
  }, { passive:true });
  window.addEventListener('resize', onScroll);
  window.addEventListener('load', onScroll);
  onScroll();
})();
