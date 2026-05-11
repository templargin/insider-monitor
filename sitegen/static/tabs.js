// Tabs + frequency toggles for the financials section
(function () {
  function activate(group, value, attr) {
    document.querySelectorAll(`[${attr}="${value}"]`).forEach(el => {
      if (el.classList.contains('tab') || el.classList.contains('freq')) {
        el.classList.add('active');
      } else {
        el.hidden = false;
      }
    });
    document.querySelectorAll(`.${group}[${attr}]:not([${attr}="${value}"])`).forEach(el => {
      el.classList.remove('active');
    });
    document.querySelectorAll(`.${group}-panel[${attr}]:not([${attr}="${value}"])`).forEach(el => {
      el.hidden = true;
    });
  }

  document.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => {
    activate('tab', b.dataset.tab, 'data-tab');
  }));
  document.querySelectorAll('.freq').forEach(b => b.addEventListener('click', () => {
    activate('freq', b.dataset.freq, 'data-freq');
  }));
})();
