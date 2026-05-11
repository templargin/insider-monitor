// Fetches live-ish (delayed) share quote from Stooq and updates SP / MC / EV cells in place.
(function () {
  const table = document.querySelector('.valuation-table[data-ticker]');
  if (!table) return;
  const ticker = table.dataset.ticker.toLowerCase();
  const url = `https://stooq.com/q/l/?s=${ticker}.us&f=sd2t2c&h&e=csv`;

  const spCell = document.getElementById('live-sp');
  const mcCell = document.getElementById('live-mc');
  const evCell = document.getElementById('live-ev');
  if (!spCell || !mcCell || !evCell) return;

  fetch(url)
    .then(r => r.ok ? r.text() : null)
    .then(csv => {
      if (!csv) return;
      const lines = csv.trim().split('\n');
      if (lines.length < 2) return;
      const cols = lines[1].split(',');
      // Symbol,Date,Time,Close
      const close = parseFloat(cols[3]);
      if (!isFinite(close) || close <= 0) return;
      spCell.textContent = '$' + close.toFixed(2);
      spCell.title = `Stooq close ${cols[1]} ${cols[2]}`;

      // Recompute MC and EV with the new SP
      const fdso = parseFloat(mcCell.dataset.fdso);
      const cash = parseFloat(evCell.dataset.cash) || 0;
      const debt = parseFloat(evCell.dataset.debt) || 0;
      if (!isFinite(fdso) || fdso <= 0) return;
      const mc = close * fdso;
      const ev = mc + debt - cash;
      mcCell.textContent = formatMoney(mc);
      evCell.textContent = formatMoney(ev);
      mcCell.classList.add('live');
      evCell.classList.add('live');
    })
    .catch(() => { /* leave baked values */ });

  function formatMoney(v) {
    const m = v / 1_000_000;
    if (Math.abs(m) >= 1000) return `$${(m/1000).toFixed(2)}B`;
    return `$${m.toFixed(1)}M`;
  }
})();
