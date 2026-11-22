// Descriptive statistics for dose values. Quantiles use linear interpolation
// between order statistics (type 7, the default in R and NumPy).

export function quantile(sorted, p) {
  if (!sorted.length) return null;
  const i = (sorted.length - 1) * p;
  const lo = Math.floor(i);
  const hi = Math.ceil(i);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo);
}

/** { n, min, q1, median, mean, q3, p75, max, iqr } — p75 equals q3, named for DRL work. */
export function summary(values) {
  const v = values.filter((x) => x !== null && x !== undefined && !Number.isNaN(x)).sort((a, b) => a - b);
  if (!v.length) return { n: 0 };
  const q1 = quantile(v, 0.25);
  const q3 = quantile(v, 0.75);
  return {
    n: v.length, min: v[0], q1, median: quantile(v, 0.5), mean: v.reduce((a, b) => a + b, 0) / v.length,
    q3, p75: q3, max: v[v.length - 1], iqr: q3 - q1,
  };
}

/** Group rows by one or two keys: groupBy(rows, r => r.protocol, r => r.device) → Map<k1, Map<k2, rows>> */
export function groupBy(rows, key1, key2) {
  const out = new Map();
  for (const r of rows) {
    const a = key1(r);
    if (!out.has(a)) out.set(a, key2 ? new Map() : []);
    if (key2) {
      const b = key2(r);
      const inner = out.get(a);
      if (!inner.has(b)) inner.set(b, []);
      inner.get(b).push(r);
    } else out.get(a).push(r);
  }
  return out;
}

export function histogram(values, bins = 14) {
  const v = values.filter((x) => x !== null && x !== undefined);
  if (!v.length) return [];
  const min = Math.min(...v);
  const max = Math.max(...v);
  const w = (max - min) / bins || 1;
  const counts = Array.from({ length: bins }, (_, i) => ({ from: min + i * w, to: min + (i + 1) * w, n: 0 }));
  for (const x of v) counts[Math.min(bins - 1, Math.floor((x - min) / w))].n += 1;
  return counts;
}
