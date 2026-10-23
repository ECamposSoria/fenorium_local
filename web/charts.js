(function (root) {
  "use strict";

  const P = root.FenoriumPortfolio || {};

  const esc = (value) => String(value ?? "").replace(
    /[<>&"]/g,
    (character) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" })[character],
  );
  const pct = (value) => `${(value / 100).toFixed(1)}%`;
  const usd = (value) => {
    if (value >= 1e9) return `$${(value / 1e9).toFixed(1)}B`;
    if (value >= 1e6) return `$${(value / 1e6).toFixed(1)}M`;
    if (value >= 1e3) return `$${(value / 1e3).toFixed(0)}K`;
    return `$${Math.round(value)}`;
  };
  const countLabel = (count) => `${count} instrument${count === 1 ? "" : "s"}`;
  const assetCountLabel = (count) => `${count} asset${count === 1 ? "" : "s"}`;
  const buckets = [
    ["<3M", 0, 90],
    ["3–6M", 90, 180],
    ["6–12M", 180, 365],
    ["1–2Y", 365, 730],
    ["2–3Y", 730, 1095],
    ["3–5Y", 1095, 1825],
    ["5–10Y", 1825, 3650],
    ["10Y+", 3650, Infinity],
  ];

  function quantile(values, fraction) {
    if (!values.length) return null;
    const sorted = values.slice().sort((left, right) => left - right);
    const position = (sorted.length - 1) * fraction;
    const lower = Math.floor(position);
    const remainder = position - lower;
    return sorted[lower] + (sorted[lower + 1] === undefined
      ? 0
      : remainder * (sorted[lower + 1] - sorted[lower]));
  }

  function yieldByMaturity(rows) {
    const sets = buckets.map(([label, minimum, maximum]) => ({
      label,
      values: rows
        .filter((row) => row.rate_bps != null
          && row.days_to_maturity != null
          && row.days_to_maturity >= minimum
          && row.days_to_maturity < maximum)
        .map((row) => Number(row.rate_bps)),
    }));
    const values = sets.flatMap((set) => set.values);
    const width = 500;
    const height = 130;
    const left = 35;
    const top = 8;
    const bottom = 23;
    const x = (index) => left + index * (width - left - 8) / (sets.length - 1);
    if (!values.length) {
      let emptyBuckets = "";
      sets.forEach((set, index) => {
        const detail = `${set.label} | No instruments in this bucket`;
        const halfWidth = (width - left - 8) / (sets.length - 1) / 2;
        emptyBuckets += `<text x="${x(index)}" y="${height - 5}" fill="#8ea0ad" font-size="8" text-anchor="middle">${esc(set.label)}</text>`;
        emptyBuckets += `<rect class="chart-target" data-chart-target="yield-bucket" data-tooltip="${esc(detail)}" tabindex="0" role="img" aria-label="${esc(detail)}" x="${Math.max(left, x(index) - halfWidth)}" y="${top}" width="${index === 0 || index === sets.length - 1 ? halfWidth : halfWidth * 2}" height="${height - top - bottom}" fill="transparent"></rect>`;
      });
      return `<div class="yield-chart-layout"><div class="empty">INSUFFICIENT COVERAGE</div><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Headline yield by maturity unavailable; no observations">${emptyBuckets}</svg></div>`;
    }
    const minimum = Math.min(0, ...values);
    const maximum = Math.max(...values);
    const y = (value) => top + (maximum - value) * (height - top - bottom) / (maximum - minimum || 1);
    let drawing = "";

    for (let index = 0; index < 4; index += 1) {
      const value = minimum + (maximum - minimum) * index / 3;
      drawing += `<line x1="${left}" y1="${y(value)}" x2="${width - 8}" y2="${y(value)}" stroke="#173244"></line>`;
      drawing += `<text x="30" y="${y(value) + 3}" fill="#718693" font-size="8" text-anchor="end">${pct(value)}</text>`;
    }

    const series = [
      [0.25, "#27c7d8"],
      [0.5, "#d770cf"],
      [0.75, "#758cff"],
    ];
    for (const [fraction, color] of series) {
      const points = sets.map((set, index) => {
        const value = quantile(set.values, fraction);
        return value == null ? null : [x(index), y(value)];
      });
      let path = "";
      let open = false;
      for (const point of points) {
        if (!point) {
          open = false;
          continue;
        }
        path += `${open ? "L" : "M"}${point[0]},${point[1]} `;
        open = true;
      }
      drawing += `<path d="${path}" fill="none" stroke="${color}" stroke-width="1.6"></path>`;
      for (const point of points) {
        if (point) drawing += `<circle cx="${point[0]}" cy="${point[1]}" r="2.2" fill="${color}"></circle>`;
      }
    }
    sets.forEach((set, index) => {
      drawing += `<text x="${x(index)}" y="${height - 5}" fill="#8ea0ad" font-size="8" text-anchor="middle">${esc(set.label)}</text>`;
      const quartiles = [0.25, 0.5, 0.75].map((fraction) => quantile(set.values, fraction));
      const detail = set.values.length
        ? `${set.label} | 25p ${pct(quartiles[0])} | Median ${pct(quartiles[1])} | 75p ${pct(quartiles[2])} | ${countLabel(set.values.length)}`
        : `${set.label} | No instruments in this bucket`;
      const halfWidth = (width - left - 8) / (sets.length - 1) / 2;
      drawing += `<rect class="chart-target" data-chart-target="yield-bucket" data-tooltip="${esc(detail)}" tabindex="0" role="img" aria-label="${esc(detail)}" x="${Math.max(left, x(index) - halfWidth)}" y="${top}" width="${index === 0 || index === sets.length - 1 ? halfWidth : halfWidth * 2}" height="${height - top - bottom}" fill="transparent"></rect>`;
    });

    return `<div class="yield-chart-layout"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Headline yield quartiles by maturity bucket">${drawing}</svg><div class="chart-series-legend" aria-label="Yield chart series"><span><i class="series-p25"></i>25p</span><span><i class="series-median"></i>Median</span><span><i class="series-p75"></i>75p</span></div></div>`;
  }

  function verdictDonut(rows) {
    const order = ["BID", "NO BID", "NO QUOTE", "UNRATED"];
    const labels = ["Score 60+", "Below 60", "Not scored", "Not rated"];
    const colors = ["#42c98a", "#e5a52f", "#ef6b62", "#89959d"];
    const applicable = rows.map((row) => typeof P.marketVerdict === "function"
      ? P.marketVerdict(row)
      : (["rwa", "vault"].includes(String(row?.category || "").toLowerCase())
        ? null
        : String(row?.verdict || "UNRATED").toUpperCase()))
      .filter((value) => value != null);
    const counts = order.map((verdict) => applicable.filter((value) => value === verdict).length);
    const total = applicable.length || 1;
    let offset = 0;
    let circles = "";

    counts.forEach((count, index) => {
      const share = count / total * 100;
      const detail = `${labels[index]} | ${countLabel(count)} | ${share.toFixed(1)}%`;
      circles += `<circle class="chart-target" data-chart-target="verdict" data-tooltip="${esc(detail)}" tabindex="0" role="img" aria-label="${esc(detail)}" cx="65" cy="65" r="42" fill="none" stroke="${colors[index]}" stroke-width="12" pathLength="100" stroke-dasharray="${share} ${100 - share}" stroke-dashoffset="${-offset}"></circle>`;
      offset += share;
    });
    const legend = order.map((verdict, index) => `
      <span><i style="background:${colors[index]}"></i><b>${esc(labels[index])}</b><small>${counts[index]} · ${(counts[index] / total * 100).toFixed(1)}%</small></span>
    `).join("");

    return `<div class="verdict-chart-layout"><svg viewBox="0 0 130 130" role="img" aria-label="Fenorium Score distribution for applicable instruments">${circles}<text x="65" y="62" fill="#edf3f6" text-anchor="middle" font-size="15">${applicable.length}</text><text x="65" y="76" fill="#8ea0ad" text-anchor="middle" font-size="8">APPLICABLE</text></svg><div class="verdict-legend">${legend}</div></div>`;
  }

  function depthByChain(rows) {
    const depths = new Map();
    rows.forEach((row) => {
      if (row.depth_usd != null) {
        const chain = row.chain || "UNKNOWN";
        depths.set(chain, (depths.get(chain) || 0) + Number(row.depth_usd));
      }
    });
    const items = [...depths].sort((left, right) => right[1] - left[1]).slice(0, 6);
    const maximum = items[0]?.[1] || 1;
    const total = items.reduce((sum, item) => sum + item[1], 0) || 1;
    const drawing = items.map(([chain, value], index) => {
      const share = value / total * 100;
      const detail = `${chain} | ${usd(value)} reported depth | ${share.toFixed(1)}% share | reported depth, not TVL`;
      return `
      <text x="2" y="${17 + index * 20}" fill="#8ea0ad" font-size="9">${esc(chain).slice(0, 16)}</text>
      <rect class="chart-target" data-chart-target="depth" data-tooltip="${esc(detail)}" tabindex="0" role="img" aria-label="${esc(detail)}" x="90" y="${7 + index * 20}" width="${250 * value / maximum}" height="10" rx="3" fill="${index % 2 ? "#758cff" : "#27c7d8"}"></rect>
      <text x="${96 + 250 * value / maximum}" y="${16 + index * 20}" fill="#edf3f6" font-size="8">${usd(value)} · ${share.toFixed(1)}%</text>
    `;
    }).join("");
    return `<div class="depth-chart-layout"><svg viewBox="0 0 420 130" role="img" aria-label="Reported executable depth by chain">${drawing}</svg></div>`;
  }

  const portfolioUsd = (micros) => {
    if (micros == null || micros === "") return "UNKNOWN";
    try {
      const value = BigInt(String(micros));
      const sign = value < 0n ? "-" : "";
      const absolute = value < 0n ? -value : value;
      const whole = (absolute / 1000000n).toLocaleString("en-US");
      const fraction = (absolute % 1000000n).toString().padStart(6, "0").slice(0, 2);
      return `${sign}$${whole}.${fraction}`;
    } catch (_) {
      return "UNKNOWN";
    }
  };

  const portfolioMicros = (micros) => {
    try {
      if (micros == null || micros === "") return null;
      return BigInt(String(micros));
    } catch (_) {
      return null;
    }
  };

  const assetNames = (row) => (Array.isArray(row?.assets) ? row.assets : [])
    .map((asset) => asset?.name || asset?.symbol || asset?.id || "UNKNOWN")
    .map(String);

  function portfolioDonut(exposure, {summary = false} = {}) {
    const rows = Array.isArray(exposure?.rows) ? exposure.rows : [];
    const total = portfolioMicros(exposure?.total_usd_micros);
    const unresolved = Number(exposure?.unresolved_count || 0);
    const coverage = unresolved > 0
      ? `Partial coverage · ${unresolved} unpriced asset${unresolved === 1 ? "" : "s"}`
      : "All discovered assets priced · REGISTERED_ASSETS_ONLY";
    if (total === null) {
      return `<div class="portfolio-donut-layout"><div class="empty">UNKNOWN</div><p class="cell-sub">${esc(coverage)} · priced NAV is UNKNOWN</p></div>`;
    }
    const items = rows.map((row, rowIndex) => ({ row, rowIndex, value: portfolioMicros(row?.priced_value_usd_micros) }));
    const positive = items
      .filter((item) => item.value !== null && item.value > 0n);
    if (total <= 0n || !positive.length) {
      return `<div class="portfolio-donut-layout"><div class="empty">NO POSITIVE PRICED VALUE</div><p class="cell-sub">${esc(coverage)} · no positive priced value was observed</p></div>`;
    }
    const subtotal = positive.reduce((sum, item) => sum + item.value, 0n);
    // Summary palette sampled from the owner's mockup legend -- the median saturated pixel of
    // each dot, in the order the mockup lists its categories -- rather than chosen by eye.
    const colors = summary
      ? ["#1b4fe2", "#5fe1f2", "#d482f5", "#805fe3"]
      : ["#27c7d8", "#d770cf", "#758cff", "#42c98a", "#e5a52f", "#ef6b62"];
    // The mockup gives its brightest blue to the largest class and reads the ring and the legend
    // from largest to smallest. Colour follows size, not the order the classes happened to be
    // discovered in, so the same class keeps its colour between the ring and the legend.
    const order = summary
      ? new Map(positive.slice().sort((left, right) => right.value > left.value ? 1 : right.value < left.value ? -1 : left.rowIndex - right.rowIndex)
          .map((item, rank) => [item.rowIndex, rank]))
      : new Map(items.map((item) => [item.rowIndex, item.rowIndex]));
    const paletteIndex = (rowIndex) => (order.has(rowIndex) ? order.get(rowIndex) : order.size + rowIndex) % colors.length;
    let offset = 0;
    let circles = "";
    const legend = [];
    const legendItems = items.map(({ row, rowIndex, value }) => ({ row, rowIndex, value }));
    if (summary) legendItems.sort((left, right) => paletteIndex(left.rowIndex) - paletteIndex(right.rowIndex));
    const drawn = summary
      ? positive.slice().sort((left, right) => paletteIndex(left.rowIndex) - paletteIndex(right.rowIndex))
      : positive;
    drawn.forEach(({ row, rowIndex, value }, index) => {
      const share = Number(value) / Number(subtotal) * 100;
      const suppliedShare = row?.share_bps == null ? null : Number(row.share_bps);
      const shareText = Number.isFinite(suppliedShare) ? pct(suppliedShare) : "UNKNOWN";
      const names = assetNames(row);
      const mixed = Number(row?.unresolved_count || 0) > 0
        || row?.value_usd_micros == null
        || portfolioMicros(row?.value_usd_micros) !== value;
      const valueText = mixed ? "UNKNOWN" : portfolioUsd(row?.value_usd_micros);
      const detail = `${row?.label || "UNKNOWN"} | Value ${valueText} | Priced subtotal ${portfolioUsd(value)} | Share ${shareText} | ${assetCountLabel(names.length)} | ${names.length ? names.join(", ") : "UNKNOWN"}`;
      circles += `<circle class="chart-target" data-chart-target="portfolio-exposure" data-tooltip="${esc(detail)}" tabindex="0" role="img" aria-label="${esc(detail)}" cx="${summary ? 84 : 65}" cy="${summary ? 84 : 65}" r="${summary ? 62 : 42}" fill="none" stroke="${colors[paletteIndex(rowIndex)]}" stroke-width="${summary ? 22 : 12}" pathLength="100" stroke-dasharray="${share} ${100 - share}" stroke-dashoffset="${-offset}"></circle>`;
      offset += share;
    });
    legendItems.forEach(({ row, rowIndex, value }) => {
      const suppliedShare = row?.share_bps == null ? null : Number(row.share_bps);
      const shareText = Number.isFinite(suppliedShare) ? pct(suppliedShare) : "UNKNOWN";
      if (summary) {
        // The reference legend is a dot, a class and its share -- nothing else. The amount is
        // still one hover away, and the Exposure screen prints it in full.
        const amount = `${portfolioUsd(row.value_usd_micros)}${row.value_usd_micros == null && value != null ? ` · Priced subtotal ${portfolioUsd(value)}` : ""}`;
        legend.push(`<span title="${esc(amount)}"><i style="background:${colors[paletteIndex(rowIndex)]}" aria-hidden="true"></i><b>${esc(root.FenoriumUI?.portfolioLabel?.(row?.label) || row?.label || "UNKNOWN")}</b><span class="portfolio-legend-percent">${esc(shareText)}</span></span>`);
        return;
      }
      legend.push(`<span><i style="background:${colors[paletteIndex(rowIndex)]}"></i><b>${esc(root.FenoriumUI?.portfolioLabel?.(row?.label) || row?.label || "UNKNOWN")}</b><small>${esc(shareText)} · ${esc(portfolioUsd(row.value_usd_micros))}${row.value_usd_micros == null && value != null ? ` · Priced subtotal ${esc(portfolioUsd(value))}` : ""}</small></span>`);
    });
    if (summary) {
      // A number the reader can act on belongs in the ring, so the exact figure is printed
      // whenever it fits the 102-unit hole; only a longer one falls back to the abbreviation,
      // and the exact figure stays in the title either way.
      const exact = portfolioUsd(total).replace(/\.\d{2}$/, "");
      const centre = exact.length <= 11 ? exact : compactUsd(total);
      // Nothing is unpriced here, so this really is the total, not a lower bound. When
      // something is unpriced the label says so and the coverage line below stays.
      const navLabel = unresolved > 0 ? "Priced NAV" : "Total NAV";
      // The coverage line only earns its place when coverage is partial. With everything
      // priced it repeats the Valuation Coverage card word for word.
      const note = unresolved > 0 ? `<p class="cell-sub">${esc(coverage)}</p>` : "";
      return `<div class="portfolio-donut-layout"><svg viewBox="0 0 168 168" role="img" aria-label="Portfolio priced exposure distribution">${circles}<text x="84" y="81" fill="#edf3f6" text-anchor="middle" font-size="16" font-weight="600"><title>${esc(portfolioUsd(total))}</title>${esc(centre)}</text><text x="84" y="99" fill="#8ea0ad" text-anchor="middle" font-size="11">${esc(navLabel)}</text></svg><div class="portfolio-donut-legend">${legend.join("")}</div>${note}</div>`;
    }
    return `<div class="portfolio-donut-layout"><svg viewBox="0 0 130 130" role="img" aria-label="Portfolio priced exposure distribution">${circles}<text x="65" y="60" fill="#edf3f6" text-anchor="middle" font-size="9" textLength="48" lengthAdjust="spacingAndGlyphs">${esc(portfolioUsd(total))}</text><text x="65" y="74" fill="#8ea0ad" text-anchor="middle" font-size="8">Priced NAV</text></svg><div class="portfolio-donut-legend">${legend.join("")}</div><p class="cell-sub">${esc(coverage)}</p></div>`;
  }

  // Bar labels sit 46 units apart on a 300-unit viewBox, anchored middle, at font-size 8.
  // A full "$250,000.00" is about 44 units wide, so it overlapped its neighbours and the first
  // bucket's label was clipped at the left edge -- which hides leading digits and makes a
  // portfolio value read as smaller than it is. Abbreviate the label; the exact figure stays in
  // the tooltip and aria-label, which already carry the total and the priced subtotal.
  function compactUsd(micros) {
    const value = Number(micros) / 1e6;
    // Choose the unit by what the number will READ as after rounding, not before it. Picking on
    // the raw magnitude turned $999,999 into "$1000.0K" -- eight characters, wide enough to
    // overprint its neighbour -- where it should carry into "$1.0M".
    // The sign goes outside the currency, which is how every other figure in the terminal reads
    // it: "$-125.0K" puts a minus where a reader expects a digit.
    const sign = value < 0 ? "\u2212" : "";
    const size_ = Math.abs(value);
    for (const [size, suffix] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]]) {
      const rounded = Number((size_ / size).toFixed(1));
      if (rounded >= 1 && rounded < 1000) return `${sign}$${rounded.toFixed(1)}${suffix}`;
    }
    return `${sign}$${size_.toFixed(size_ === Math.trunc(size_) ? 0 : 2)}`;
  }

  // Round numbers on an axis, whatever the span happens to be: a reader takes bearings from 0,
  // 500, 1,000, never from thirds of an arbitrary range.
  function niceTicks(min, max, wanted = 4) {
    if (max <= min) return [min];
    const reading = Number(max - min) / 1e6 / wanted;
    if (!Number.isFinite(reading) || reading <= 0) return [min, max];
    const magnitude = 10 ** Math.floor(Math.log10(reading));
    const step = [1, 2, 2.5, 5, 10].map(multiple => multiple * magnitude).find(candidate => candidate >= reading);
    if (!step) return [min, max];
    const scaled = BigInt(Math.round(step * 1e6));
    // The first tick at or above the minimum. BigInt division truncates toward zero, which is
    // already the ceiling for a negative number -- adjusting it too put a -$200K gridline under
    // a series whose deepest point was -$125K, below the plot and on top of the legend.
    const first = (min >= 0n ? (min + scaled - 1n) / scaled : min / scaled) * scaled;
    const ticks = [];
    for (let value = first; value <= max && ticks.length < 9; value += scaled) ticks.push(value);
    return ticks.length >= 2 ? ticks : [min, max];
  }

  // Opening balance + inflows - outflows + P&L = ending balance. The columns are what crossed
  // the wallet's boundary each period and the green line is the balance those movements plus
  // the P&L produce. One scale for all of it, because every figure here is dollars: a second
  // axis can make a small balance stand taller than a large deposit, which is the exact
  // comparison this chart exists to make honestly.
  //
  // The P&L does not get a line of its own beside them. On a book that takes in half a million
  // a week and earns five hundred dollars, its line is the zero line -- so each period states
  // its figure under its own label, and the P&L filter gives it the whole plot when it is what
  // you came to look at.
  function portfolioOverview(buckets, options = {}) {
    if (!Array.isArray(buckets) || !buckets.length) return '<div class="empty">UNKNOWN</div>';
    const integer = value => typeof value === 'string' && /^-?\d+$/.test(value) ? BigInt(value) : null;
    const fields = ['inflows_usd_micros', 'outflows_usd_micros', 'pnl_usd_micros', 'equity_usd_micros'];
    if (buckets.some(bucket => fields.some(field => integer(bucket[field]) === null))) return '<div class="empty">UNKNOWN · Incomplete capital flow series</div>';
    const read = key => buckets.map(bucket => BigInt(bucket[key]));
    const inflows = read('inflows_usd_micros'), outflows = read('outflows_usd_micros');
    const pnl = read('pnl_usd_micros'), balance = read('equity_usd_micros');

    const mode = ['all', 'inflows', 'outflows', 'pnl', 'balance'].includes(options.series) ? options.series : 'all';
    const showIn = mode === 'all' || mode === 'inflows';
    const showOut = mode === 'all' || mode === 'outflows';
    const showBalance = mode === 'all' || mode === 'balance';
    const showPnl = mode === 'pnl';

    const left = 44, right = 300, top = 12, bottom = 100;
    const width = right - left, height = bottom - top;
    const band = width / buckets.length;
    const centre = index => left + band * (index + 0.5);

    // One scale over everything drawn, and it always contains nought: bars grow from a line,
    // and a loss or a withdrawal has to sit below it.
    const drawnValues = [0n, ...(showIn ? inflows : []), ...(showOut ? outflows : []),
      ...(showPnl ? pnl : []), ...(showBalance ? balance : [])];
    const min = drawnValues.reduce((a, b) => a < b ? a : b);
    const max = drawnValues.reduce((a, b) => a > b ? a : b);
    const span = max - min || 1n;
    const y = value => bottom - Number((value - min) * BigInt(height * 1000) / span) / 1000;

    const grid = niceTicks(min, max).map(value =>
      `<line x1="${left}" y1="${y(value).toFixed(1)}" x2="${right}" y2="${y(value).toFixed(1)}" stroke="#1d3550" stroke-width="0.4" stroke-dasharray="3 3"></line>`
      + `<text class="movement-tick" x="${left - 5}" y="${(y(value) + 2).toFixed(1)}" fill="#8ea0ad" font-size="6" text-anchor="end">${esc(compactUsd(value))}</text>`).join('');
    // The plot's own frame: a rule down the left and one along nought, so the columns and the
    // line are read against an edge rather than floating in the panel.
    const frame = `<line x1="${left}" y1="${top - 2}" x2="${left}" y2="${bottom + 2}" stroke="#2b4a63" stroke-width="0.6"></line>`
      + `<line class="movement-baseline" x1="${left}" y1="${y(0n).toFixed(1)}" x2="${right}" y2="${y(0n).toFixed(1)}" stroke="#2b4a63" stroke-width="0.8"></line>`;

    // Labels only while they fit. Past that the axis carries the reading and the pointer carries
    // the figure; printing them anyway overlaps one period's number onto the next.
    const labelled = buckets.length <= 8;
    const unitWord = {day: 'Daily', week: 'Weekly', month: 'Monthly'}[buckets[0].unit] || 'Period';
    const barWidth = Math.min(18, band * 0.44);
    // Money in and money out share a column rather than standing beside each other: they are
    // two halves of one period's movement, and the column's reach is what that period did.
    // Text is measured, not guessed: these figures run about 4.34 units a character at 6.5 and
    // 4.0 at 6. Under-measuring them is how two labels that overlap get judged clear of each
    // other, so every placement carries the box it actually occupies.
    const box = (text, at, atY, anchor, size) => {
      const reach = text.length * (size >= 6.5 ? 4.4 : 4.05);
      const from = anchor === 'start' ? at : anchor === 'end' ? at - reach : at - reach / 2;
      return {text, at, y: atY, anchor, from, to: from + reach, height: size};
    };
    const hits = (one, other) => one.to + 2 > other.from && other.to + 2 > one.from
      && Math.abs(one.y - other.y) < (one.height + other.height) / 2 + 1;
    const bar = (values, index, colour, fill) => {
      const value = values[index];
      if (value === 0n) return {rect: '', label: null};
      const line = y(0n), edge = y(value);
      const topY = Math.min(line, edge), size = Math.max(0.8, Math.abs(edge - line));
      // A bar too short to see is a label sitting on the zero line, where the other column's
      // figure and the balance line already are. The readout still has it.
      const place = labelled && size >= 5
        ? {...box(compactUsd(value), centre(index), value < 0n ? topY + size + 6 : topY - 2.5, 'middle', 6), colour}
        : null;
      return {
        rect: `<rect x="${(centre(index) - barWidth / 2).toFixed(1)}" y="${topY.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${size.toFixed(1)}" rx="2" fill="url(#${fill})" filter="url(#movement-glow-${value < 0n ? 'out' : 'in'})"></rect>`,
        label: place,
      };
    };
    const asText = place => `<text x="${place.at.toFixed(1)}" y="${place.y.toFixed(1)}" fill="${place.colour}" font-size="${place.height}" font-weight="600" text-anchor="${place.anchor}">${esc(place.text)}</text>`;
    const drawn = [...(showIn ? buckets.map((_, index) => bar(inflows, index, '#6aa6ff', 'flow-in')) : []),
      ...(showOut ? buckets.map((_, index) => bar(outflows, index, '#f2837b', 'flow-out')) : [])];
    const barShapes = drawn.map(shape => shape.rect).join('');
    const barPlaces = drawn.map(shape => shape.label).filter(Boolean);
    const barLabels = barPlaces.map(asText).join('');

    const path = values => values.map((value, index) => `${index ? 'L' : 'M'}${centre(index).toFixed(1)} ${y(value).toFixed(1)}`).join(' ');
    const dots = (values, ring, glow) => values.map((value, index) =>
      `<circle class="movement-dot" cx="${centre(index).toFixed(1)}" cy="${y(value).toFixed(1)}" r="2.6" fill="#eafff6" stroke="${ring}" stroke-width="1.4" filter="url(#${glow})"></circle>`).join('');
    // The same treatment the P&L card has: a line over a fill that fades out beneath it, so the
    // series reads as a level the account reached rather than as a wire through the columns.
    // The fill closes on the zero line, which is the only floor either series has.
    const area = values => `${path(values)} L${centre(values.length - 1).toFixed(1)} ${y(0n).toFixed(1)} L${centre(0).toFixed(1)} ${y(0n).toFixed(1)} Z`;
    const wash = (values, fill) => `<path d="${area(values)}" fill="url(#${fill})"></path>`;
    const stroke = (values, colour, width, glow) =>
      `<path d="${path(values)}" fill="none" stroke="${colour}" stroke-width="${width}" stroke-linejoin="round" stroke-linecap="round" filter="url(#${glow})"></path>`;
    // The wash goes under the columns and the line over them: a bar is a fact about one period
    // and is never veiled, while the line has to stay legible where it crosses them.
    const fills = (showBalance ? wash(balance, 'balance-fill') : '') + (showPnl ? wash(pnl, 'movement-pnl-fill') : '');
    const balanceLine = !showBalance ? '' : stroke(balance, '#42c98a', 2, 'movement-glow-balance') + dots(balance, '#42c98a', 'movement-glow-balance');
    const pnlLine = !showPnl ? '' : stroke(pnl, '#956bd6', 1.8, 'movement-glow-pnl') + dots(pnl, '#956bd6', 'movement-glow-pnl');
    // The balance's figure goes above its dot only when the line has climbed clear of the
    // column; otherwise it goes inside the column, which is empty. Hugging the dot put it on
    // the bar's own figure every time the two were close, which on a rising book is most of them.
    const lineLabel = (values, colour) => {
      if (!labelled) return '';
      const placements = values.map((value, index) => {
        const barTop = showIn && inflows[index] > 0n ? y(inflows[index]) : null;
        const above = barTop === null || y(value) < barTop - 10;
        const text = compactUsd(value);
        // A segment always arrives from the left, so the clear side of a dot is its right. Where
        // the plot has no room left for it, the figure stays centred and lifts further instead
        // -- directly above a dot the line has just climbed to is empty.
        const sloped = above && index > 0 && values[index - 1] !== value;
        const room = sloped && centre(index) + text.length * 4.4 <= right;
        const anchor = room ? 'start' : 'middle';
        const at = Math.min(right, Math.max(left, room ? centre(index) + 3 : centre(index)));
        const lift = above ? (sloped && !room ? 12 : 7) : 0;
        const atY = Math.min(bottom - 2, Math.max(top + 5, above ? y(value) - lift : Math.max(y(value) + 8, barTop + 9)));
        // Two places to try, in order of preference. The second is the strip above the plot,
        // which nothing else uses: a figure that collides gets moved there before it is given
        // up, because the closing balance is the number this chart is about and dropping it in
        // favour of one column's figure is the wrong way round.
        return [{...box(text, at, atY, anchor, 6.5), colour},
          ...(above ? [{...box(text, at, 7, anchor, 6.5), colour}] : [])];
      });
      // Two figures cannot share the same space -- not two of these, and not one of these and a
      // column's own number. Read from the right so the closing one always survives, since it is
      // the figure the panel is about, and drop any earlier one that would land on something
      // already drawn. Every one of them is still in the period's readout and in the breakdown
      // table; none of them is legible printed on top of another.
      const kept = [];
      for (let index = placements.length - 1; index >= 0; index -= 1) {
        const free = placements[index].find(place =>
          !kept.some(other => hits(place, other)) && !barPlaces.some(other => hits(place, other)));
        if (free) kept.push(free);
      }
      return kept.map(asText).join('');
    };
    const lineLabels = (showBalance ? lineLabel(balance, '#42c98a') : '') + (showPnl ? lineLabel(pnl, '#c3b6f5') : '');

    // Each period names itself and states what it earned, which is where the P&L lives when it
    // has no line: a figure under the label rather than a flat line along the axis.
    const dates = buckets.map((bucket, index) => {
      const earned = compactUsd(pnl[index]);
      const sign = pnl[index] > 0n ? '+' : '';
      return `<text x="${centre(index).toFixed(1)}" y="114" fill="#8ea0ad" font-size="6.5" text-anchor="middle">${esc(bucket.label)}</text>`
        + (mode !== 'all' || !labelled ? '' : `<text x="${centre(index).toFixed(1)}" y="122" fill="${pnl[index] < 0n ? '#f2837b' : '#7fd9ab'}" font-size="5.5" text-anchor="middle">${esc(`${sign}${earned}`)} P&amp;L</text>`);
    }).join('');
    // One target per period over the full height, so the pointer finds a period rather than a
    // two-pixel dot, and the readout states every figure that period has -- including the ones
    // this view does not draw.
    const targets = buckets.map((bucket, index) => {
      const detail = [`${bucket.label}${bucket.observations > 1 ? ` · ${bucket.observations} observations` : ''}`,
        showIn ? `Inflows ${compactUsd(inflows[index])}` : '',
        showOut ? `Outflows ${compactUsd(outflows[index])}` : '',
        showPnl || mode === 'all' ? `P&L ${compactUsd(pnl[index])}` : '',
        showBalance ? `Ending balance ${compactUsd(balance[index])}` : ''].filter(Boolean).join(' | ');
      return `<rect data-chart-target="portfolio-overview" tabindex="0" role="img" aria-label="${esc(detail.split(' | ').join(', '))}"`
        + ` data-tooltip="${esc(detail)}" x="${(centre(index) - band / 2).toFixed(1)}" y="${top}" width="${band.toFixed(1)}" height="${height}" fill="transparent"></rect>`;
    }).join('');

    const key = [[showBalance, '#42c98a', 'Ending balance', 'movement-glow-balance'],
      [showIn, '#3e8cff', 'Inflows', 'movement-glow-in'],
      [showOut, '#ef6b62', 'Outflows', 'movement-glow-out'],
      [showPnl, '#956bd6', `${unitWord} P&L`, 'movement-glow-pnl']].filter(([on]) => on);
    // Each entry sits in a chip of its own, sized to its own words rather than to a fixed slot:
    // "Ending balance" is twice the width of "Inflows", and evenly spaced slots left one
    // crowding its neighbour while the other floated in space.
    const chips = key.map(([, colour, label, glow]) => ({colour, label, glow, width: label.length * 3.6 + 18}));
    const spread = chips.reduce((total, chip) => total + chip.width, 0) + (chips.length - 1) * 6;
    let cursor = 160 - spread / 2;
    const legend = chips.map(chip => {
      const at = cursor;
      cursor += chip.width + 6;
      return `<rect x="${at.toFixed(1)}" y="134" width="${chip.width.toFixed(1)}" height="12" rx="6" fill="#0c2033" stroke="#22405a" stroke-width="0.5"></rect>`
        + `<circle cx="${(at + 8).toFixed(1)}" cy="140" r="2.4" fill="${chip.colour}" filter="url(#${chip.glow})"></circle>`
        + `<text x="${(at + 14).toFixed(1)}" y="142.4" fill="#c3d0d9" font-size="6.2">${esc(chip.label)}</text>`;
    }).join('');

    return `<svg viewBox="0 0 320 150" role="img" aria-label="Inflows, outflows, P&amp;L and ending balance over ${buckets.length} ${buckets[0].unit || 'period'}s">`
      + `<defs>`
      + `<linearGradient id="flow-in" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#5aa6ff"></stop><stop offset="100%" stop-color="#1745c8"></stop></linearGradient>`
      + `<linearGradient id="flow-out" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#c73b34"></stop><stop offset="100%" stop-color="#ff8a7e"></stop></linearGradient>`
      + ['movement-glow-balance #42c98a 1.6', 'movement-glow-pnl #956bd6 1.6', 'movement-glow-in #3e8cff 1.2', 'movement-glow-out #ef6b62 1.2']
        .map(spec => { const [id, , blur] = spec.split(' ');
          return `<filter id="${id}" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="${blur}" result="bloom"></feGaussianBlur><feMerge><feMergeNode in="bloom"></feMergeNode><feMergeNode in="SourceGraphic"></feMergeNode></feMerge></filter>`;
        }).join('')
      + `<linearGradient id="balance-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#42c98a" stop-opacity="0.30"></stop><stop offset="100%" stop-color="#42c98a" stop-opacity="0"></stop></linearGradient>`
      + `<linearGradient id="movement-pnl-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#956bd6" stop-opacity="0.34"></stop><stop offset="100%" stop-color="#956bd6" stop-opacity="0"></stop></linearGradient>`
      + `</defs>`
      + grid + frame
      + `<text x="10" y="${(top + height / 2).toFixed(1)}" fill="#8ea0ad" font-size="6" text-anchor="middle" transform="rotate(-90 10 ${(top + height / 2).toFixed(1)})">Amount (USD)</text>`
      + fills + barShapes + balanceLine + pnlLine + barLabels + lineLabels
      + dates + targets + legend
      + `</svg>`;
  }

  // A 2px line over a gradient fill fading to transparent, matching the mockup's P&L card.
  // Only ever called with a series that exists; the panel decides that, not this.
  function portfolioArea(points, options = {}) {
    const integer = value => typeof value === 'string' && /^-?\d+$/.test(value) ? BigInt(value) : null;
    if (!Array.isArray(points) || !points.length || points.some(p => integer(p.nav_usd_micros) === null)) return '<div class="empty">UNKNOWN</div>';
    const nav = points.map(p => integer(p.nav_usd_micros));
    // One chart, and it draws the P&L wherever the record has one. Where it does not -- a
    // declared series that never said what was paid in, so its flow-adjusted P&L is UNKNOWN --
    // it draws the portfolio's value instead and says so, rather than presenting a change in
    // value as a gain.
    const declaredPnl = points.map(p => integer(p.pnl_usd_micros));
    const wholePnl = declaredPnl.every(value => value !== null);
    // A record that is supposed to carry a P&L and does not has a broken series, and a broken
    // series is never drawn as something else. Only a record that never claimed one -- a
    // declared series that did not say what was paid in -- falls back to the value.
    if (options.expect === "pnl" && !wholePnl) return '<div class="empty">UNKNOWN · Incomplete P&amp;L series</div>';
    const pnl = wholePnl ? declaredPnl : null;
    // A four-figure P&L on an axis sized for a six-figure NAV is a flat line along the baseline,
    // which is what drawing both together cost: the panel is called Portfolio P&L and the P&L
    // was the one thing in it you could not see.
    const bounds = (series) => {
      const low = series.reduce((a,b)=>a<b?a:b), high = series.reduce((a,b)=>a>b?a:b);
      return {min: low, span: high - low || 1n, max: high};
    };
    // In percent the line is the return since the window opened, rebased so a filtered range
    // starts at nought rather than wherever the whole series had reached. Returns compound, so
    // rebasing is the ratio of the two linked factors, not their difference.
    const wantsPercent = Boolean(pnl && options.unit === "pct");
    const declaredReturns = points.map((point) => integer(point.return_bps));
    const base = declaredReturns[0] === null ? 0n : declaredReturns[0];
    const percent = wantsPercent && declaredReturns.slice(1).every((value) => value !== null)
      ? declaredReturns.map((value) => ((10000n + (value === null ? 0n : value)) * 10000n) / (10000n + base) - 10000n)
      : null;
    // The summary panel is titled Portfolio P&L and plots exactly that: one series, one axis,
    // and no second line to mistake it for. The portfolio's value has its own card above and its
    // own chart on the Performance screen, so drawing it here only ever cost the P&L its scale.
    const plotted = percent || pnl || nav;
    const only = bounds(plotted);
    const min = only.min, max = only.max, span = only.span;
    // Percent is scaled in basis points, money in micros, and each formats the way it reads.
    const display = percent ? (value) => `${(Number(value) / 100).toFixed(2)}%` : compactUsd;
    const displayScale = percent ? 100 : 1e6;
    const timestamps = points.map(p=>Date.parse(p.as_of));
    if (timestamps.some(t=>!Number.isFinite(t)) || timestamps.some((t,i)=>i && t < timestamps[i-1])) return '<div class="empty">UNKNOWN · Invalid timeline</div>';
    const duration = timestamps.at(-1)-timestamps[0] || 1;
    // The plot keeps clear of both gutters: the left axis labels the NAV and the right the P&L,
    // and the line used to run underneath each of them.
    const x = index => 38 + 226 * (timestamps[index]-timestamps[0]) / duration;
    // Convert only bounded plotting coordinates to Number; money stays BigInt.
    // This panel plots one thing: the P&L compounded since the window opened. Deposits and
    // withdrawals used to sit under it as bars, which cost the curve a third of the plot to say
    // something the Account Movement chart on the Performance screen now says properly, against
    // a scale of their own. A flow is still never folded into the line.
    const baseY = 88;
    const plotSpan = 70;
    const scaled = (value, scale) => baseY - Number((value-scale.min)*BigInt(plotSpan * 1000)/scale.span)/1000;
    const y = value => scaled(value, only || navScale);
    const line = (data, scale) => data.map((value,index)=>`${index ? 'L':'M'}${x(index).toFixed(1)} ${scaled(value, scale).toFixed(1)}`).join(' ');
    const series = plotted;
    const floor = baseY + 4;
    const navLine = line(series, only || navScale), fill = `${navLine} L${x(points.length-1).toFixed(1)} ${floor} L38 ${floor} Z`;
    // Left value axis with gridlines, as the reference has, plus a callout over the last point.
    // Both restate values already in the series; neither adds a figure.
    // Round numbers on the axis. Thirds of an arbitrary span produce labels like $618.07, which
    // is a true figure and a useless gridline; a reader takes bearings from 0, 500, 1,000.
    const niceStep = (amount) => {
      const reading = Number(amount) / displayScale / 3;
      if (!Number.isFinite(reading) || reading <= 0) return null;
      const magnitude = 10 ** Math.floor(Math.log10(reading));
      const step = [1, 2, 2.5, 5, 10].map((multiple) => multiple * magnitude).find((candidate) => candidate >= reading);
      return step ? BigInt(Math.round(step * displayScale)) : null;
    };
    const step = only ? niceStep(span) : null;
    const rounded = [];
    if (step && step > 0n) {
      const first = (min + (min < 0n ? -step + 1n : step - 1n)) / step * step;
      for (let value = first; value <= max && rounded.length < 5; value += step) rounded.push(value);
    }
    const axisTicks = rounded.length >= 2
      ? rounded.map((value) => ({ y: scaled(value, only), text: display(value) }))
      : [0, 0.5, 1].map((fraction) => {
          const value = min + (span * BigInt(Math.round(fraction * 1000))) / 1000n;
          return { y: baseY - plotSpan * fraction, text: display(value) };
        });
    const axis = axisTicks.map((tick) =>
      `<line x1="36" y1="${tick.y.toFixed(1)}" x2="266" y2="${tick.y.toFixed(1)}" stroke="#1c2d3c" stroke-width="0.4"></line>`
      + `<text x="34" y="${(tick.y + 2.5).toFixed(1)}" fill="#5fe1f2" font-size="6" text-anchor="end">${esc(tick.text)}</text>`).join('');

    // A date every few observations rather than only the endpoints, which is what makes the
    // shape readable as a timeline.
    const tickCount = Math.min(5, points.length);
    // A day with several transfers in it produces several marks, and evenly spaced slots can
    // land on two of them: at a week's range that printed "09-03" twice, a few pixels apart, as
    // "090903". A tick is dropped when it repeats the date before it or crowds its position.
    const drawnTicks = [];
    for (let slot = 0; slot < tickCount; slot += 1) {
      const index = tickCount === 1 ? 0 : Math.round(slot * (points.length - 1) / (tickCount - 1));
      const label = points[index].as_of.slice(5, 10);
      const at = x(index);
      const last = drawnTicks[drawnTicks.length - 1];
      if (last && (last.label === label || at - last.at < 28)) continue;
      drawnTicks.push({label, at, last: slot === tickCount - 1});
    }
    const dateTicks = drawnTicks.map((tick, position) => {
      const anchor = position === 0 ? 'start' : tick.last ? 'end' : 'middle';
      return `<text x="${tick.at.toFixed(1)}" y="104" fill="#8ea0ad" font-size="6.5" text-anchor="${anchor}">${esc(tick.label)}</text>`;
    }).join('');
    // The amount, and under it the move it represents. On the single-series chart it is not a
    // fixed label but the readout: it rides the curve as the pointer moves and states what that
    // point is worth. The date is already the axis underneath it, so it is not repeated here.
    const calloutAt = (index) => ({
      x: Math.min(198, Math.max(40, x(index) - 30)),
      y: Math.min(60, Math.max(4, y(series[index]) - 24)),
    });
    const home = calloutAt(points.length - 1);
    const callout = `<g class="pnl-callout" display="none" transform="translate(${home.x.toFixed(1)}, ${home.y.toFixed(1)})">`
      + `<rect width="60" height="19" rx="4" fill="#3b2a7a" stroke="#805fe3" stroke-width="0.6"></rect>`
      + `<text class="pnl-callout-amount" x="30" y="9" fill="#edf3f6" font-size="7.5" font-weight="600" text-anchor="middle"></text>`
      + `<text class="pnl-callout-change" x="30" y="16" fill="#c3b6f5" font-size="6" text-anchor="middle"></text>`
      + `</g>`;
    // One target over the whole plot, so the pointer does not have to find a two-pixel dot: the
    // nearest point to wherever it is gets the card. The per-point rings stay for the keyboard.
    const plot = `<rect class="pnl-plot" data-chart-target="portfolio-pnl-plot" x="38" y="4" width="226" height="88" fill="transparent"></rect>`;
    return `<svg viewBox="0 0 300 108" role="img" aria-label="${pnl ? 'Cumulative P&amp;L' : 'Portfolio value'} over ${points.length} observations">`
      + `<defs><linearGradient id="pnl-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#6f5cf0" stop-opacity="0.42"></stop><stop offset="100%" stop-color="#6f5cf0" stop-opacity="0"></stop></linearGradient></defs>`
      + axis
      + `<path d="${fill}" fill="url(#pnl-fill)"></path><path d="${navLine}" fill="none" stroke="#6f8cff" stroke-width="2"></path>`
      + points.map((point, index) => {
        const cx = x(index).toFixed(1), cy = y(series[index]).toFixed(1);
        const dot = `<circle cx="${cx}" cy="${cy}" r="2" fill="#6f8cff"></circle>`;
        // What the readout says at this point: what has been made since the chart began, and
        // what that is as a return. The return is the record's own time-weighted figure --
        // money paid in raises the value without being a gain, so a ratio of the two amounts
        // would call a deposit performance.
        // Drawing the P&L, the card states what has been made since the chart began. Drawing
        // the value because there is no P&L, it states the value -- a change in value is not a
        // gain, and labelling it one is the substitution this panel keeps having to refuse.
        const gain = pnl ? pnl[index] - pnl[0] : null;
        const amount = gain === null
          ? portfolioUsd(nav[index])
          : `${gain < 0n ? '−' : '+'}${portfolioUsd(gain < 0n ? -gain : gain)}`;
        // The same rebasing the line uses, computed once for both units so the card can never
        // state a return the curve is not drawing. Where the series has none it says so instead
        // of showing a nought.
        const rebased = !pnl || declaredReturns[index] === null
          ? null
          : ((10000n + declaredReturns[index]) * 10000n) / (10000n + base) - 10000n;
        const move = rebased === null
          ? 'UNKNOWN'
          : `${rebased < 0n ? '−' : '+'}${(Number(rebased < 0n ? -rebased : rebased) / 100).toFixed(2)}%`;
        // Whichever unit is selected leads; the other reads underneath it.
        const head = percent ? move : amount;
        const sub = percent ? amount : move;
        const seat = calloutAt(index);
        // A two-pixel dot is not a hover target. The ring around it takes the pointer and the
        // keyboard, and carries nothing visible of its own.
        return dot + `<circle class="chart-target" data-chart-target="portfolio-pnl"`
          + ` data-pnl-amount="${esc(head)}" data-pnl-change="${esc(sub)}"`
          + ` data-callout-x="${seat.x.toFixed(1)}" data-callout-y="${seat.y.toFixed(1)}"`
          + ` tabindex="0" role="img" aria-label="${esc(`${String(point.as_of).slice(0, 10)}. ${head}, ${sub}`)}"`
          + ` cx="${cx}" cy="${cy}" r="7" fill="transparent"></circle>`;
      }).join('')
      + callout
      + plot
      + dateTicks + `</svg>`
      + (only
        ? ''
        : `<p class="cell-sub">Cyan: portfolio value, ${esc(portfolioUsd(navScale.min))} to ${esc(portfolioUsd(navScale.max))} on the left axis${pnl ? ` · Violet dashed: cumulative P&amp;L, ${esc(portfolioUsd(pnlScale.min))} to ${esc(portfolioUsd(pnlScale.max))} on the right. The two scales are independent, so heights are not comparable between them.` : '.'}</p>`);
  }

  function portfolioMaturity(exposure, {summary = false} = {}) {
    const rows = Array.isArray(exposure?.rows) ? exposure.rows : [];
    const fixed = ["< 6M", "6M–1Y", "1–3Y", "3–5Y", "5Y+"];
    const aliases = {
      "<6M": "< 6M", "< 6 M": "< 6M", "6M-1Y": "6M–1Y", "6M–1Y": "6M–1Y",
      "1-3Y": "1–3Y", "1–3Y": "1–3Y", "3-5Y": "3–5Y", "3–5Y": "3–5Y", "5Y+": "5Y+",
    };
    const groups = new Map(fixed.map((label) => [label, { label, value: 0n, total: 0n, totalUnknown: false, hasValue: false, seen: false, count: 0 }]));
    const extras = new Map();
    rows.forEach((row) => {
      const raw = String(row?.label || "UNKNOWN");
      const label = aliases[raw.replace(/\s+/g, "")] || ({ matured: "Matured", "n/a": "N/A", unknown: "UNKNOWN" }[raw.toLowerCase()] || raw);
      const group = groups.get(label) || extras.get(label) || { label, value: 0n, total: 0n, totalUnknown: false, hasValue: false, seen: false, count: 0 };
      const value = portfolioMicros(row?.priced_value_usd_micros);
      const rowTotal = portfolioMicros(row?.value_usd_micros);
      group.seen = true;
      // A dust holding can price to a genuine 0 micros. That is an observed value, not a
      // missing one, and marking it absent reported the bucket's known asset count as UNKNOWN.
      if (value !== null && value >= 0n) { group.value += value; group.hasValue = true; }
      if (rowTotal === null) group.totalUnknown = true;
      else group.total += rowTotal;
      group.count += Array.isArray(row?.assets) ? row.assets.length : 0;
      if (groups.has(label)) groups.set(label, group); else extras.set(label, group);
    });
    const orderedExtras = ["Matured", "N/A", "UNKNOWN"].filter((label) => extras.has(label)).map((label) => extras.get(label));
    for (const [label, group] of extras) if (!["Matured", "N/A", "UNKNOWN"].includes(label)) orderedExtras.push(group);
    const groupsToDraw = [...groups.values(), ...orderedExtras];
    const max = Math.max(1, ...groupsToDraw.map((group) => Number(group.value)));
    // The mockup labels this chart with a value axis and leaves the bars clean. Per-bar currency
    // labels were three rounds of the same defect -- clipped at the viewBox edge, then
    // overprinting each other once anchored inward -- because eight buckets across 300 units
    // cannot hold eight currency strings at a readable size. An axis states the scale once, and
    // every exact figure stays in the tooltip and aria-label.
    const axisRight = summary ? Math.max(360, groupsToDraw.length * 48 + 44) : 300;
    const plotLeft = 44;
    const span = axisRight - plotLeft - (summary ? 40 : 8);
    const ticks = [0, 0.5, 1].map((fraction) => ({
      fraction,
      y: 92 - 76 * fraction,
      text: max > 0n ? compactUsd(BigInt(Math.round(Number(max) * fraction))) : "$0",
    }));
    const axis = ticks.map((tick) => (
      `<line x1="${plotLeft - 4}" y1="${tick.y}" x2="${axisRight}" y2="${tick.y}" stroke="#1c2d3c" stroke-width="0.5"></line>`
      + `<text x="${plotLeft - 8}" y="${tick.y + 2.5}" fill="#8ea0ad" font-size="7" text-anchor="end">${esc(tick.text)}</text>`
    )).join("");
    const bars = groupsToDraw.map((group, index) => {
      const known = group.hasValue;
      const height = known ? Math.max(1, 76 * Number(group.value) / max) : 0;
      const x = plotLeft + (summary ? 20 : 0) + (groupsToDraw.length > 1 ? index * span / (groupsToDraw.length - 1) : span / 2);
      const valueText = known ? portfolioUsd(group.value) : (group.seen ? "UNKNOWN" : "0 assets");
      const totalText = group.seen ? (group.totalUnknown ? "UNKNOWN" : portfolioUsd(group.total)) : valueText;
      const detail = `${group.label} | Total ${totalText} | Priced subtotal ${known ? portfolioUsd(group.value) : "UNKNOWN"} | ${assetCountLabel(group.count)}`;
      return `<text x="${x}" y="116" fill="#8ea0ad" font-size="7" text-anchor="middle">${esc(group.label)}</text>`
        + `<rect class="chart-target" data-chart-target="portfolio-maturity" data-tooltip="${esc(detail)}" tabindex="0" role="img" aria-label="${esc(detail)}" x="${x - (summary ? 17 : 9)}" y="${92 - height}" width="${summary ? 34 : 18}" height="${height || 2}"${summary ? ' rx="2"' : ""} fill="${known ? (summary ? "url(#summary-maturity-gradient)" : "#27c7d8") : "#526672"}"></rect>`
        + (known || !group.seen ? "" : `<text x="${x}" y="86" fill="#8ea0ad" font-size="6" text-anchor="middle">UNKNOWN</text>`);
    }).join("");
    return `<div class="portfolio-maturity-layout"><svg viewBox="0 0 ${axisRight} 125" role="img" aria-label="Portfolio maturity value histogram">${summary ? '<defs><linearGradient id="summary-maturity-gradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#3b82f6"></stop><stop offset="100%" stop-color="#8b5cf6"></stop></linearGradient></defs>' : ""}${axis}${bars}</svg><p class="cell-sub">Bars show priced value against the axis; UNKNOWN categories remain visible without an invented zero value. Exact figures are in each bar's tooltip.</p></div>`;
  }

  function bindTooltips(container) {
    const host = container || root.document;
    if (!host?.addEventListener || host.__fenoriumChartTooltipsBound) return;
    host.__fenoriumChartTooltipsBound = true;
    let tooltip = null;
    let activeTarget = null;

    const ensureTooltip = () => {
      if (tooltip) return tooltip;
      tooltip = root.document.createElement("div");
      tooltip.id = "chart-tooltip";
      tooltip.className = "chart-tooltip";
      tooltip.setAttribute("role", "tooltip");
      tooltip.hidden = true;
      root.document.body.appendChild(tooltip);
      return tooltip;
    };

    const place = (target, pointer) => {
      const current = ensureTooltip();
      const targetRect = target.getBoundingClientRect();
      const panelRect = target.closest?.(".panel")?.getBoundingClientRect?.() || {
        left: 0,
        top: 0,
        right: root.innerWidth,
        bottom: root.innerHeight,
      };
      const margin = 8;
      // A tooltip wider than its panel fits against neither edge, so the clamp below cannot keep
      // it inside: cap its width to the panel (and the viewport) before measuring it.
      const panelWidth = panelRect.right - panelRect.left;
      current.style.maxWidth = `${Math.max(120, Math.min(270, root.innerWidth - margin * 3, panelWidth - margin * 2))}px`;
      const width = current.offsetWidth;
      const height = current.offsetHeight;
      const minimumX = Math.max(margin, panelRect.left + margin);
      const maximumX = Math.max(minimumX, Math.min(root.innerWidth - width - margin, panelRect.right - width - margin));
      const anchorX = pointer?.clientX ?? targetRect.left + targetRect.width / 2;
      let left = Math.min(maximumX, Math.max(minimumX, anchorX - width / 2));
      let top = targetRect.top - height - margin;
      if (top < Math.max(margin, panelRect.top + margin)) top = targetRect.bottom + margin;
      const maximumY = Math.max(margin, Math.min(root.innerHeight - height - margin, panelRect.bottom - height - margin));
      top = Math.min(maximumY, Math.max(Math.max(margin, panelRect.top + margin), top));
      current.style.left = `${left}px`;
      current.style.top = `${top}px`;
    };

    // The P&L chart has no text tooltip: its callout is the readout, and it moves to whichever
    // point the pointer is on. Nothing about the date is repeated, because the axis under the
    // curve is already saying it.
    const calloutFor = (target) => target?.ownerSVGElement?.querySelector?.(".pnl-callout") || null;
    const setCallout = (callout, x, y, amount, change) => {
      if (!callout) return;
      callout.setAttribute("transform", `translate(${x}, ${y})`);
      const amountNode = callout.querySelector(".pnl-callout-amount");
      const changeNode = callout.querySelector(".pnl-callout-change");
      if (amountNode && amount) amountNode.textContent = amount;
      if (changeNode) changeNode.textContent = change || "";
    };
    const rideCallout = (target) => {
      const callout = calloutFor(target);
      if (!callout || !target?.dataset?.pnlAmount) return false;
      setCallout(callout, target.dataset.calloutX, target.dataset.calloutY,
        target.dataset.pnlAmount, target.dataset.pnlChange);
      callout.removeAttribute("display");
      return true;
    };
    // At rest the card is not shown at all. The period's figure is stated by the panel, so a
    // card pinned to the last point would only repeat it over the top of the line.
    const restCallout = (target) => {
      const callout = calloutFor(target);
      if (callout && callout.classList.contains("pnl-callout")) callout.setAttribute("display", "none");
    };
    // The pointer should not have to find a two-pixel dot. Anywhere over the plot picks the
    // point nearest to it, measured along the axis in the chart's own coordinates.
    const nearestPoint = (plot, clientX) => {
      const svg = plot.ownerSVGElement;
      const rect = svg?.getBoundingClientRect?.();
      if (!rect?.width) return null;
      const width = svg.viewBox?.baseVal?.width || 300;
      const pointerX = (clientX - rect.left) / rect.width * width;
      let best = null;
      let bestDistance = Infinity;
      for (const candidate of svg.querySelectorAll('[data-chart-target="portfolio-pnl"]')) {
        const distance = Math.abs(Number(candidate.getAttribute("cx")) - pointerX);
        if (distance < bestDistance) {
          bestDistance = distance;
          best = candidate;
        }
      }
      return best;
    };

    const show = (target, pointer) => {
      if (rideCallout(target)) {
        activeTarget = target;
        return;
      }
      const detail = target?.dataset?.tooltip;
      if (!detail) return;
      activeTarget = target;
      const current = ensureTooltip();
      current.textContent = detail.split(" | ").join("\n");
      current.hidden = false;
      target.setAttribute("aria-describedby", current.id);
      place(target, pointer);
    };

    const hide = (target) => {
      if (target && activeTarget !== target) return;
      if (activeTarget) {
        activeTarget.removeAttribute("aria-describedby");
        restCallout(activeTarget);
      }
      activeTarget = null;
      if (tooltip) tooltip.hidden = true;
    };

    host.addEventListener("pointerover", (event) => {
      const target = event.target.closest?.("[data-chart-target]");
      if (target) show(target, event);
    });
    host.addEventListener("pointermove", (event) => {
      const plot = event.target.closest?.('[data-chart-target="portfolio-pnl-plot"]');
      if (plot) {
        const point = nearestPoint(plot, event.clientX);
        if (point && rideCallout(point)) activeTarget = plot;
        return;
      }
      if (activeTarget?.matches?.(":hover")) place(activeTarget, event);
    });
    host.addEventListener("pointerout", (event) => {
      const target = event.target.closest?.("[data-chart-target]");
      if (target && !target.contains(event.relatedTarget) && root.document.activeElement !== target) hide(target);
    });
    host.addEventListener("focusin", (event) => {
      const target = event.target.closest?.("[data-chart-target]");
      if (target) show(target);
    });
    host.addEventListener("focusout", (event) => {
      const target = event.target.closest?.("[data-chart-target]");
      if (target) hide(target);
    });
  }

  // Analytics dashboard (owner 2026-10-20). Both draw from the aligned daily portfolio returns the
  // engine computes its risk metrics from, so the curves and the figures above them agree.
  // The engine exports each day's return in basis points as a decimal string (six places: a
  // weighted book moves by fractions of a point); integers are accepted too.
  const RETURN_ROW = (row) => row && typeof row === "object" && typeof row.day === "string"
    && /^\d{4}-\d{2}-\d{2}$/.test(row.day)
    && (Number.isSafeInteger(row.return_bps) || (typeof row.return_bps === "string" && /^-?\d+\.\d{6}$/.test(row.return_bps)))
    && Number.isFinite(Number(row.return_bps));

  function riskCurves(dailyReturns, {valuedMicros = null, instrument = false} = {}) {
    const rows = Array.isArray(dailyReturns) ? dailyReturns.filter(RETURN_ROW) : [];
    if (rows.length < 2) return null;
    const returns = rows.map((row) => Number(row.return_bps));
    // Today's valued holdings walked back through each day's return; without a value the path is
    // an index starting at 100. One point more than returns: the baseline before the first day,
    // so the first day's fall counts in the drawdown (review dd).
    const valued = typeof valuedMicros === "string" && /^\d+$/.test(valuedMicros) ? Number(valuedMicros) / 1e6 : null;
    const wealth = new Array(returns.length + 1);
    wealth[returns.length] = valued ?? 100;
    for (let index = returns.length; index > 0; index -= 1) {
      const factor = 1 + returns[index - 1] / 10000;
      wealth[index - 1] = factor > 0 ? wealth[index] / factor : wealth[index];
    }
    let peak = wealth[0], maxDrawdown = 0;
    const drawdown = wealth.map((value) => {
      peak = Math.max(peak, value);
      const fall = peak > 0 ? (peak - value) / peak * 10000 : 0;
      maxDrawdown = Math.max(maxDrawdown, fall);
      return fall;
    });
    const x = (index) => 38 + 226 * index / (wealth.length - 1);
    const path = (series, low, span, baseY, plotSpan) => series.map((value, index) =>
      `${index ? "L" : "M"}${x(index).toFixed(1)} ${(baseY - plotSpan * (value - low) / span).toFixed(1)}`).join(" ");
    const money = (value) => valued == null ? value.toFixed(1) : compactUsd(BigInt(Math.round(value * 1e6)));
    const dates = `<text x="38" y="104" fill="#8ea0ad" font-size="6.5">${esc(rows[0].day)}</text>`
      + `<text x="264" y="104" fill="#8ea0ad" font-size="6.5" text-anchor="end">${esc(rows[rows.length - 1].day)}</text>`;
    const low = Math.min(...wealth), high = Math.max(...wealth), span = high - low || 1;
    const navLine = path(wealth, low, span, 88, 70);
    const navTicks = [low, (low + high) / 2, high].map((value) => {
      const y = 88 - 70 * (value - low) / span;
      return `<line x1="36" y1="${y.toFixed(1)}" x2="266" y2="${y.toFixed(1)}" stroke="#1c2d3c" stroke-width="0.4"></line>`
        + `<text x="34" y="${(y + 2.5).toFixed(1)}" fill="#5fe1f2" font-size="6" text-anchor="end">${esc(money(value))}</text>`;
    }).join("");
    const nav = `<svg viewBox="0 0 300 108" role="img" aria-label="${valued == null ? "Index" : "Valued holdings"} over ${rows.length} aligned days">`
      + navTicks + `<path d="${navLine} L264 92 L38 92 Z" fill="#27c7d81a"></path><path d="${navLine}" fill="none" stroke="#27c7d8" stroke-width="1.2"></path>`
      + dates + `</svg>`;
    const ddSpan = maxDrawdown || 1;
    const ddLine = drawdown.map((value, index) => `${index ? "L" : "M"}${x(index).toFixed(1)} ${(10 + 78 * value / ddSpan).toFixed(1)}`).join(" ");
    const ddTicks = [0, maxDrawdown].map((value) => {
      const y = 10 + 78 * value / ddSpan;
      return `<line x1="36" y1="${y.toFixed(1)}" x2="266" y2="${y.toFixed(1)}" stroke="#1c2d3c" stroke-width="0.4"></line>`
        + `<text x="34" y="${(y + 2.5).toFixed(1)}" fill="#ef6b62" font-size="6" text-anchor="end">-${(value / 100).toFixed(2)}%</text>`;
    }).join("");
    const drawdownSvg = `<svg viewBox="0 0 300 108" role="img" aria-label="Drawdown over ${rows.length} aligned days, at most ${(maxDrawdown / 100).toFixed(2)} percent">`
      + ddTicks + `<path d="${ddLine} L264 10 L38 10 Z" fill="#ef6b621a"></path><path d="${ddLine}" fill="none" stroke="#ef6b62" stroke-width="1.2"></path>`
      + dates + `</svg>`;
    const instrumentDrawdown = instrument ? instrumentLine({label: "Drawdown", xLabel: "Date", yLabel: "Drawdown %",
      xFormat: value => new Date(value).toISOString().slice(0, 10), yFormat: pct,
      series: [{name: "Drawdown", points: drawdown.map((value, index) => ({
        x: index ? Date.parse(rows[index - 1].day) : Date.parse(rows[0].day) - 86400000, y: -value,
      }))}]}) : null;
    return {nav, drawdown: instrumentDrawdown || drawdownSvg, max_drawdown_bps: Math.round(maxDrawdown), start: wealth[0], end: wealth[wealth.length - 1], observations: rows.length};
  }

  function returnHistogram(dailyReturns, es95Bps = null, {instrument = false} = {}) {
    const rows = Array.isArray(dailyReturns) ? dailyReturns.filter(RETURN_ROW) : [];
    if (rows.length < 2) return "";
    const values = rows.map((row) => Number(row.return_bps));
    const low = Math.min(...values), high = Math.max(...values), span = high - low || 1;
    const bins = 24, counts = new Array(bins).fill(0);
    const binOf = (value) => Math.min(bins - 1, Math.floor((value - low) / span * bins));
    for (const value of values) counts[binOf(value)] += 1;
    const tallest = Math.max(...counts) || 1;
    const xOf = (value) => 38 + 226 * (value - low) / span;
    const width = 226 / bins;
    // The tail is the worst 5% of days, the same ceil(n × 5%) the engine averages; the ES95
    // figure is that average, a loss, so its marker sits at minus that return (review dd: the
    // marker is the tail's mean, not its boundary).
    const tailCount = Math.max(1, Math.ceil(values.length * 0.05));
    const tailBins = new Set(values.slice().sort((a, b) => a - b).slice(0, tailCount).map(binOf));
    const marker = Number.isSafeInteger(es95Bps) && es95Bps > 0 ? -es95Bps : null;
    const bars = counts.map((count, index) => {
      const left = low + span * index / bins;
      const inTail = tailBins.has(index);
      const height = 78 * count / tallest;
      return `<rect x="${xOf(left).toFixed(1)}" y="${(88 - height).toFixed(1)}" width="${(width - 0.6).toFixed(1)}" height="${height.toFixed(1)}" fill="${inTail ? "#ef6b62" : "#27c7d8"}" opacity="${count ? "0.85" : "0"}"></rect>`;
    }).join("");
    const markerLine = marker != null && marker >= low && marker <= high
      ? `<line x1="${xOf(marker).toFixed(1)}" y1="6" x2="${xOf(marker).toFixed(1)}" y2="90" stroke="#ef6b62" stroke-width="0.8" stroke-dasharray="2 2"></line>`
        + `<text x="${xOf(marker).toFixed(1)}" y="5" fill="#ef6b62" font-size="6" text-anchor="middle">ES95 ${(marker / 100).toFixed(2)}%</text>`
      : "";
    const ticks = [low, 0, high].filter((value, index, all) => all.indexOf(value) === index && value >= low && value <= high)
      .map((value) => `<text x="${xOf(value).toFixed(1)}" y="100" fill="#8ea0ad" font-size="6.5" text-anchor="middle">${(value / 100).toFixed(2)}%</text>`).join("");
    return `<svg viewBox="0 0 ${instrument ? '540 194.4' : '300 108'}" role="img" aria-label="${instrument ? 'Daily-return histogram · ' : ''}Distribution of ${rows.length} daily returns${marker != null ? `, Expected Shortfall marker at ${(marker / 100).toFixed(2)} percent` : ""}">`
      + `<g${instrument ? ' transform="scale(1.8)"' : ''}><line x1="38" y1="88" x2="264" y2="88" stroke="#1c2d3c" stroke-width="0.4"></line>` + bars + markerLine + ticks + `</g></svg>`;
  }

  const instrumentColors = ["var(--cyan)", "var(--purple)"];
  const finiteChartValue = (value) => value != null && Number.isFinite(Number(value));
  const instrumentTarget = (detail, name = "instrument") => `class="chart-target" data-chart-target="${name}" data-tooltip="${esc(detail)}" tabindex="0" role="img" aria-label="${esc(detail)}"`;

  function instrumentTicks(low, high) {
    // niceTicks uses fixed-point micros; sub-micro price ranges need a decimal fallback.
    if (high - low < 0.0001) return [low, (low + high) / 2, high];
    return niceTicks(BigInt(Math.floor(low * 1e6)), BigInt(Math.ceil(high * 1e6)))
      .map((value) => Number(value) / 1e6);
  }

  function instrumentSvg(drawing, height, label, legend = "", width = 560) {
    return `<div class="instrument-chart-layout"><svg class="instrument-chart" style="max-width: ${width}px; height: auto" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(label)}">${drawing}</svg>${legend}</div>`;
  }

  function instrumentLine({label, xLabel = "", yLabel = "", series = [], logX = false,
    xFormat = String, yFormat = String, markers = [], scatter = false, width = 560, yDomain = null, niceX = false} = {}) {
    const points = series.flatMap((set) => set.points || []).filter((point) => finiteChartValue(point.x) && (!logX || point.x > 0));
    const values = points.filter((point) => finiteChartValue(point.y)).map((point) => Number(point.y));
    if (!points.length) return '<p class="empty">NOT COMPUTABLE · No chart observations</p>';
    const transform = (value) => logX ? Math.log10(Number(value)) : Number(value);
    const xs = points.map((point) => transform(point.x));
    const validMarkers = markers.filter((marker) => finiteChartValue(marker.x) && (!logX || marker.x > 0));
    const minX = Math.min(...xs, ...validMarkers.map((marker) => transform(marker.x)));
    const maxX = Math.max(...xs, ...validMarkers.map((marker) => transform(marker.x)));
    const rawMin = values.length ? Math.min(...values) : 0;
    const rawMax = values.length ? Math.max(...values) : 1;
    const padding = (rawMax - rawMin || Math.abs(rawMax) || 1) * 0.12;
    const minY = yDomain?.[0] ?? rawMin - padding, maxY = yDomain?.[1] ?? rawMax + padding;
    const right = width - 36;
    const x = (value) => 64 + (transform(value) - minX) / (maxX - minX || 1) * (right - 64);
    const y = (value) => 180 - (Number(value) - minY) / (maxY - minY) * 142;
    let drawing = `<text x="64" y="13" fill="var(--muted)" font-size="10">${esc(yLabel)}</text>`;
    for (const value of instrumentTicks(minY, maxY)) {
      drawing += `<line x1="64" y1="${y(value)}" x2="${right}" y2="${y(value)}" stroke="var(--soft)"></line><text x="57" y="${y(value) + 3}" fill="var(--muted)" text-anchor="end" font-size="9">${esc(yFormat(value))}</text>`;
    }
    const xTicks = niceX ? instrumentTicks(logX ? 10 ** minX : minX, logX ? 10 ** maxX : maxX) : logX ? []
      : Array.from({length: 5}, (_, index) => minX + (maxX - minX) * index / 4);
    if (logX && !niceX) {
      for (let power = Math.ceil(minX); power <= Math.floor(maxX); power += 1) xTicks.push(10 ** power);
      const last = Math.max(...points.map(point => Number(point.x)));
      if (!xTicks.includes(last)) xTicks.push(last);
    }
    // A short span puts several ticks inside one day; a label is printed once.
    let previousLabel = null;
    for (const value of xTicks) {
      const label = xFormat(value);
      if (label === previousLabel) continue;
      previousLabel = label;
      drawing += `<text data-instrument-x-tick="${value}" x="${x(value)}" y="199" fill="var(--muted)" text-anchor="middle" font-size="9">${esc(label)}</text>`;
    }
    validMarkers.forEach((marker, index) => {
      drawing += `<line ${instrumentTarget(marker.label, "instrument-reference")} x1="${x(marker.x)}" y1="32" x2="${x(marker.x)}" y2="180" stroke="var(--muted)" stroke-width="2" stroke-dasharray="3 4"></line>`;
      const stacked = validMarkers.slice(0, index).some(previous => Math.abs(x(previous.x) - x(marker.x)) < 140);
      drawing += `<text data-instrument-marker-label="true" x="${x(marker.x)}" y="${26 + (stacked ? index * 14 : 0)}" text-anchor="${x(marker.x) > width / 2 ? "end" : "start"}" fill="var(--text)" font-size="9">${esc(marker.label)}</text>`;
    });
    series.forEach((set, index) => {
      const color = instrumentColors[index % instrumentColors.length];
      let path = "", open = false;
      const setPoints = set.points || [];
      for (const point of setPoints) {
        if (!finiteChartValue(point.x) || (logX && point.x <= 0) || !finiteChartValue(point.y)) { open = false; continue; }
        path += `${open ? "L" : "M"}${x(point.x)},${y(point.y)} `;
        open = true;
      }
      if (!scatter) drawing += `<path data-instrument-series="${esc(set.name)}" d="${path}" fill="none" stroke="${color}" stroke-width="2"${set.dashed ? ' stroke-dasharray="6 4"' : ""}></path>`;
      for (const [pointIndex, point] of setPoints.entries()) {
        if (!finiteChartValue(point.x) || (logX && point.x <= 0)) continue;
        const detail = point.tooltip || `${set.name} · ${xFormat(point.x)} · ${finiteChartValue(point.y) ? yFormat(point.y) : "NOT COMPUTABLE"}`;
        // Refusals remain keyboard reachable without drawing a zero or joining the gap.
        if (!finiteChartValue(point.y)) {
          drawing += `<rect ${instrumentTarget(detail, "instrument-gap")} x="${x(point.x) - 5}" y="170" width="10" height="10" fill="none" stroke="var(--muted)"></rect>`;
          continue;
        }
        const px = x(point.x), py = y(Math.max(minY, Math.min(maxY, point.y)));
        if (scatter && (point.y < minY || point.y > maxY)) {
          const direction = point.y > maxY ? -1 : 1;
          drawing += `<path ${instrumentTarget(detail, "instrument-peer-edge")} d="M${px},${py + direction * 5} L${px - 4},${py - direction * 3} L${px + 4},${py - direction * 3} Z" fill="${color}"></path>`;
        } else if (scatter) {
          drawing += `<circle ${instrumentTarget(detail)} cx="${px}" cy="${py}" r="4" fill="${color}"></circle>`;
        } else {
          const previous = setPoints[pointIndex - 1], next = setPoints[pointIndex + 1];
          // One hover column per x, listing every series: per-series columns overlap and the
          // last series drawn would answer for all of them.
          if (index === 0) {
            const shared = series.length > 1 ? `${xFormat(point.x)} · ${series.map((other) => {
              const match = (other.points || []).find((candidate) => candidate.x === point.x);
              return `${other.name} ${match && finiteChartValue(match.y) ? yFormat(match.y) : "NOT COMPUTABLE"}`;
            }).join(" · ")}` : detail;
            const left = previous && finiteChartValue(previous.x) ? (x(previous.x) + px) / 2 : 64;
            const end = next && finiteChartValue(next.x) ? (x(next.x) + px) / 2 : right;
            drawing += `<g class="instrument-hover"><rect ${instrumentTarget(shared)} x="${left}" y="38" width="${Math.max(1, end - left)}" height="142" fill="transparent"></rect><line class="instrument-crosshair" x1="${px}" x2="${px}" y1="38" y2="180" stroke="var(--muted)" stroke-dasharray="3 4" pointer-events="none"></line></g>`;
          }
          // A point with no drawable neighbour is a zero-length path; it needs its own mark.
          if (!finiteChartValue(previous?.y) && !finiteChartValue(next?.y)) drawing += `<circle data-instrument-isolated="true" cx="${px}" cy="${py}" r="4" fill="${color}"></circle>`;
        }
        if (point.highlight) drawing += `<circle cx="${px}" cy="${py}" r="7" fill="none" stroke="var(--text)" stroke-width="2"></circle>`;
        if (point.label) drawing += `<text x="${px + (px > width / 2 ? -10 : 10)}" y="${py - 9}" text-anchor="${px > width / 2 ? "end" : "start"}" fill="var(--text)" font-size="10">${esc(point.label)}</text>`;
      }
    });
    drawing += `<text x="${width / 2}" y="221" fill="var(--muted)" text-anchor="middle" font-size="10">${esc(xLabel)}</text>`;
    const legend = series.length > 1 ? `<div class="chart-series-legend">${series.map((set, index) => `<span><svg width="24" height="10" aria-hidden="true"><line x1="1" x2="23" y1="5" y2="5" stroke="${instrumentColors[index % instrumentColors.length]}" stroke-width="2"${set.dashed ? ' stroke-dasharray="5 3"' : ""}></line></svg>${esc(set.name)}</span>`).join("")}</div>` : "";
    return instrumentSvg(drawing, 230, label, legend, width);
  }

  function instrumentSizeCurve(points, {sizeUsd, depthUsd, toMaturity = false} = {}) {
    const series = [["Headline", "headline_bps"], ["Net", "net_bps"]].map(([name, key], index) => ({
      name, dashed: index === 0,
      points: points.map((point) => ({x: point.size_usd, y: point.refusal ? null : point[key],
        tooltip: `${usd(point.size_usd)} · ${name}${toMaturity && index === 0 ? " after credit, to maturity" : ""}: ${point.refusal || (finiteChartValue(point[key]) ? pct(point[key]) : "NOT COMPUTABLE")}${Number(point.fenorium_score) < 60 && point.fenorium_score != null ? ` · Fenorium Score ${point.fenorium_score} below 60 · score cap ${point.score_cap ?? "UNKNOWN"}` : ""}`})),
    }));
    return instrumentLine({label: "Yield at size", xLabel: "Position size USD · logarithmic", yLabel: toMaturity ? "Yield % · to maturity, not annualized" : "Annualized yield %",
      series, logX: true, xFormat: value => usd(value).replace('.0M', 'M'), yFormat: pct,
      markers: [{x: sizeUsd, label: `Selected ${usd(sizeUsd)}`}, {x: depthUsd, label: `Reported depth ${usd(depthUsd)}`} ]});
  }

  function instrumentWaterfall(items) {
    if (!items.length || items.some((item) => !finiteChartValue(item.value_bps))) return '<p class="empty">NOT COMPUTABLE · Yield waterfall</p>';
    let balance = 0;
    const bars = items.map((item, index) => {
      const start = item.total || index === 0 ? 0 : balance;
      const end = item.total || index === 0 ? Number(item.value_bps) : balance + Number(item.value_bps);
      balance = end;
      return {...item, start, end};
    });
    const low = Math.min(0, ...bars.flatMap((bar) => [bar.start, bar.end]));
    const high = Math.max(0, ...bars.flatMap((bar) => [bar.start, bar.end]));
    const y = (value) => 155 - (value - low) / (high - low || 1) * 125;
    let drawing = "";
    for (const value of instrumentTicks(low, high)) {
      drawing += `<line x1="58" y1="${y(value)}" x2="540" y2="${y(value)}" stroke="var(--soft)"></line><text x="52" y="${y(value) + 3}" text-anchor="end" fill="var(--muted)" font-size="9">${pct(value)}</text>`;
    }
    bars.forEach((bar, index) => {
      const x = 75 + index * 94;
      const detail = `${bar.label}: ${pct(bar.value_bps)}`;
      drawing += `<rect ${instrumentTarget(detail, "instrument-waterfall")} x="${x}" y="${Math.min(y(bar.start), y(bar.end))}" width="50" height="${Math.max(2, Math.abs(y(bar.start) - y(bar.end)))}" fill="${bar.total || index === 0 ? "var(--cyan)" : "var(--purple)"}"></rect><text x="${x + 25}" y="177" text-anchor="middle" fill="var(--text)" font-size="9">${esc(bar.label)}</text>`;
    });
    return instrumentSvg(drawing, 195, `Yield waterfall: ${bars.map((bar) => `${bar.label} ${pct(bar.value_bps)}`).join(", ")}`);
  }

  function instrumentPriceHistory(prices) {
    let peak = null;
    const points = (prices || []).map(([day, price]) => ({x: Date.parse(day), y: finiteChartValue(price) ? Number(price) : null, tooltip: `${day} · Price ${price}`}));
    const drawdown = points.map((point) => {
      if (!finiteChartValue(point.y) || point.y <= 0) return {...point, y: null};
      peak = Math.max(peak || point.y, point.y);
      const value = (point.y / peak - 1) * 10000;
      return {...point, y: value, tooltip: `${new Date(point.x).toISOString().slice(0, 10)} · Drawdown ${pct(value)}`};
    });
    const common = {xLabel: "Date", xFormat: (value) => new Date(value).toISOString().slice(0, 10)};
    return {price: instrumentLine({...common, width: 1080, label: "Price history", yLabel: "Price", series: [{name: "Price", points}], yFormat: (value) => value.toFixed(4)}),
      drawdown: instrumentLine({...common, label: "Drawdown", yLabel: "Drawdown %", series: [{name: "Drawdown", points: drawdown}], yFormat: pct})};
  }

  function instrumentReturnHistogram(dailyReturns, es95Bps = null) {
    return `<div class="instrument-return-histogram">${returnHistogram(dailyReturns, es95Bps, {instrument: true})}</div>`;
  }

  function instrumentCashflows(cashflows) {
    const flows = (cashflows || []).filter((flow) => Number.isFinite(Date.parse(flow.at)));
    if (!flows.length) return '<p class="empty">No scheduled cashflows</p>';
    const times = flows.map((flow) => Date.parse(flow.at));
    const low = Math.min(Date.now(), ...times), high = Math.max(...times);
    const x = (value) => 60 + (value - low) / (high - low || 1) * 960;
    const drawing = flows.map((flow) => {
      const detail = `${flow.at} · ${flow.per_unit} ${flow.asset_id} per unit`;
      return `<circle ${instrumentTarget(detail, "instrument-cashflow")} cx="${x(Date.parse(flow.at))}" cy="35" r="5" fill="var(--cyan)"></circle><text x="${x(Date.parse(flow.at))}" y="62" text-anchor="${x(Date.parse(flow.at)) > 350 ? "end" : "start"}" fill="var(--muted)" font-size="10">${esc(String(flow.at).slice(0, 10))}</text>`;
    }).join("");
    return instrumentSvg(`<line x1="60" x2="1020" y1="35" y2="35" stroke="var(--soft)" stroke-width="2"></line>${drawing}`, 80, "Scheduled cashflow timeline", "", 1080);
  }

  function instrumentYieldCurve(points) {
    return instrumentLine({width: 1080, label: "Price versus yield · computed from the declared rate", xLabel: "Declared yield %", yLabel: "Price per unit", xFormat: pct, yFormat: (value) => value.toFixed(4),
      series: [{name: "Price", points: points.map((point) => ({x: point.yield_bps, y: point.price, highlight: point.current, label: point.current ? "Current" : ""}))}]});
  }

  function instrumentPeerScale(points) {
    const values = points.filter(point => finiteChartValue(point.net_bps)).map(point => Number(point.net_bps));
    if (!values.length) return {domain: null, beyond: 0};
    const selected = points.filter(point => point.selected && finiteChartValue(point.net_bps)).map(point => Number(point.net_bps));
    const low = Math.min(quantile(values, 0.05), ...selected);
    const high = Math.max(quantile(values, 0.95), ...selected);
    const domain = low === high ? [low - 1, high + 1] : [low, high];
    return {domain, beyond: values.filter(value => value < domain[0] || value > domain[1]).length};
  }

  function instrumentPeers(points, {logX = false, xLabel = "Days to maturity"} = {}) {
    return instrumentLine({label: "Against its peers", xLabel, yLabel: "Annualized net yield %", logX, scatter: true,
      width: 1080, niceX: !logX, yDomain: instrumentPeerScale(points).domain,
      xFormat: logX ? usd : (value) => String(Math.round(value)), yFormat: pct,
      series: [{name: "Peers", points: points.map((point) => ({x: point.x, y: point.net_bps, highlight: point.selected, label: point.selected ? point.name : "",
        tooltip: `${point.name} · ${xLabel}: ${logX ? usd(point.x) : Math.round(point.x)} · Net ${pct(point.net_bps)}`}))}]});
  }

  root.FenoriumCharts = { yieldByMaturity, verdictDonut, depthByChain, portfolioDonut, portfolioMaturity, portfolioArea, portfolioOverview, riskCurves, returnHistogram, bindTooltips,
    instrumentLine, instrumentSizeCurve, instrumentWaterfall, instrumentPriceHistory, instrumentReturnHistogram, instrumentCashflows, instrumentYieldCurve, instrumentPeers, instrumentPeerScale };
}(typeof globalThis !== "undefined" ? globalThis : this));
