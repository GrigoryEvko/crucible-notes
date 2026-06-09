// Sticky AI-disclaimer bar, injected under the mdBook menu bar on every page.
// Self-contained (styles + element) so it works across light and navy themes.
(function () {
  var CSS =
    '#ai-disclaimer-bar{position:-webkit-sticky;position:sticky;' +
    'top:var(--menu-bar-height,50px);z-index:99;box-sizing:border-box;' +
    'max-width:var(--content-max-width,750px);margin:0 auto 1rem;' +
    'padding:8px 18px;background:#b05a55;color:#f4e8d0;' +
    'font-size:1.75rem;font-weight:600;letter-spacing:0.02em;text-transform:uppercase;' +
    'text-align:center;line-height:1.4;border:1px solid rgba(0,0,0,0.20);border-top:none;' +
    'border-radius:0 0 6px 6px;box-shadow:0 2px 4px rgba(0,0,0,0.14);}' +
    '@media print{#ai-disclaimer-bar{position:static;}}' +
    // shrink the disclaimer bar on narrow screens so it doesn't eat the viewport
    '@media (max-width:800px){#ai-disclaimer-bar{font-size:1.15rem;padding:5px 10px;' +
    'letter-spacing:0;line-height:1.3;}}' +
    // let tables wrap and shrink on narrow screens instead of forcing horizontal scroll
    '@media (max-width:800px){' +
    '.content main table{font-size:1.05rem;line-height:1.35;}' +
    '.content main table th,.content main table td{padding:0.3em 0.45em;' +
    'overflow-wrap:anywhere;word-break:break-word;}' +
    '.content main table code,.content main table tt{white-space:normal;' +
    'overflow-wrap:anywhere;word-break:break-word;}}';
  var TEXT = "AI-GENERATED REVERSE-ENGINEERING NOTES — AUTHOR’S PERSONAL REFERENCE ONLY. " +
             "EVERYTHING HERE IS A BEST-GUESS RECONSTRUCTION, NOT A RELIABLE SOURCE.";
  function build() {
    if (!document.getElementById('ai-disclaimer-style')) {
      var s = document.createElement('style');
      s.id = 'ai-disclaimer-style';
      s.textContent = CSS;
      (document.head || document.documentElement).appendChild(s);
    }
    if (document.getElementById('ai-disclaimer-bar')) return;
    var bar = document.createElement('div');
    bar.id = 'ai-disclaimer-bar';
    bar.setAttribute('role', 'note');
    bar.textContent = TEXT;
    var menu = document.getElementById('menu-bar');
    if (menu && menu.parentNode) {
      menu.parentNode.insertBefore(bar, menu.nextSibling);
    } else {
      document.body.insertBefore(bar, document.body.firstChild);
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', build);
  } else {
    build();
  }
})();
