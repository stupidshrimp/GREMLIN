/* The Weibull card's working figures, on the Documentation page.
 *
 * The card explains the method in prose and equations; these figures let a
 * reader move the numbers and watch what happens. Six of them, by their figure
 * numbers on the card:
 *
 *  W4  the distribution: a shape, a scale and an age, and the curves they give;
 *  W5  the likelihood: a guess at β and η scored life by life against the
 *      worked example, beside the best the lives allow;
 *  W6  the four charts GREMLIN draws for a fit, for the worked example, read
 *      at any age;
 *  W7  the probability plot built in three steps from a plain CDF;
 *  W8  four shapes a probability plot takes, and what each one says;
 *  W9  the lab: lives typed in, fitted and read the way GREMLIN would.
 *
 * Three layers, so each can be checked on its own:
 *
 *  - The method. GREMLIN's Weibull arithmetic, step for step the same as
 *    LifeDataService's: the root search for β (_fit_weibull_2p), the intervals
 *    (_weibull_confidence_intervals), the Kaplan-Meier points, the probability
 *    plot R² and its review threshold, and the reading rules of the
 *    interpretation summary. tests/test_standards_pages.py runs it under Node
 *    against the service, so a figure cannot quietly show a fit GREMLIN would
 *    not make.
 *  - The drawings. Pure functions from a figure's state to markup. The
 *    template carries every figure already drawn at its opening state, by
 *    these same functions (tools/prerender_weibull_figures.py), so the card
 *    reads and prints in full with no script; the test above checks that copy
 *    is still what they draw.
 *  - The controls, which exist only in a browser: each figure's sliders and
 *    buttons, redrawing it as they move.
 *
 * Colours are the page's figure classes in standards.css, so the drawings
 * follow the theme toggle like every other figure on the page. The lives each
 * figure draws come from the JSON block #weibull-figure-data in the template,
 * where the tests read them too.
 */
(function (factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else if (typeof document !== "undefined") api.mount(document);
})(function () {
  "use strict";

  /* ------------------------------------------------------------------ method */

  // The fewest lives ending in a failure GREMLIN fits (MIN_WEIBULL_FAILURE_LIVES).
  const MIN_FAILURE_LIVES = 5;
  // How wide an interval may be, as a share of its estimate, to read as stable.
  const BETA_INTERVAL_STABLE_FRACTION = 0.7;
  const ETA_INTERVAL_STABLE_FRACTION = 0.4;
  // R_SQUARED_REVIEW_THRESHOLDS: lives ending in a failure, and the R² below
  // which the fit is flagged for review. Counts in between interpolate.
  const R_SQUARED_REVIEW_THRESHOLDS = [
    [5, 0.806], [6, 0.812], [7, 0.821], [8, 0.831], [9, 0.84], [10, 0.846],
    [12, 0.861], [15, 0.876], [20, 0.893], [25, 0.905], [30, 0.913], [40, 0.928],
    [50, 0.938], [75, 0.952], [100, 0.961], [150, 0.97], [200, 0.976],
  ];
  const Z_95 = 1.959963984540054;
  // Life hours in a calendar week on the plant default schedule, 20 hours a day
  // Monday to Friday, and the weeks the Most likely to fail soon list looks ahead.
  const WEEK_LIFE_HOURS = 100;
  const RISK_WINDOW_WEEKS = 4;

  function sum(values) {
    let total = 0;
    values.forEach((value) => {
      total += value;
    });
    return total;
  }

  function reliability(t, beta, eta) {
    return Math.exp(-Math.pow(t / eta, beta));
  }

  function unreliability(t, beta, eta) {
    return 1 - reliability(t, beta, eta);
  }

  function density(t, beta, eta) {
    return (beta / eta) * Math.pow(t / eta, beta - 1) * reliability(t, beta, eta);
  }

  function hazard(t, beta, eta) {
    return (beta / eta) * Math.pow(t / eta, beta - 1);
  }

  // Equation C5: the chance a life that has lasted t ends in a failure within
  // the next `span` hours. Written with the exponents subtracted, not as a ratio
  // of reliabilities, so a long life does not divide zero by zero.
  function failureWithin(t, span, beta, eta) {
    return 1 - Math.exp(Math.pow(t / eta, beta) - Math.pow((t + span) / eta, beta));
  }

  // Equation W6, the log-likelihood of lives given as [hours, ends in a failure].
  function logLikelihood(lives, beta, eta) {
    const failures = lives.filter((life) => life[1]).map((life) => life[0]);
    return (
      sum(failures.map((t) => Math.log(beta) - beta * Math.log(eta) + (beta - 1) * Math.log(t))) -
      sum(lives.map((life) => Math.pow(life[0] / eta, beta)))
    );
  }

  function logLikelihoodFromLogParams(lives, logBeta, logEta) {
    const beta = Math.exp(logBeta);
    const eta = Math.exp(logEta);
    if (!lives.some((life) => life[1]) || beta <= 0 || eta <= 0) return -Infinity;
    return logLikelihood(lives, beta, eta);
  }

  // Equation W7: the η that is best for a given β.
  function etaForBeta(lives, beta) {
    const failures = lives.filter((life) => life[1]).length;
    return Math.pow(sum(lives.map((life) => Math.pow(life[0], beta))) / failures, 1 / beta);
  }

  // _fit_weibull_2p: the root of equation W8, bracketed on 400 points from 0.1
  // to 20 and bisected 80 times, then η from W7. Null when there is no failure
  // or no root, where GREMLIN stops rather than report another kind of estimate.
  function fit(lives) {
    const failures = lives.filter((life) => life[1]).map((life) => life[0]);
    const times = lives.map((life) => life[0]);
    const d = failures.length;
    if (!d) return null;
    const meanLogFail = sum(failures.map((t) => Math.log(t))) / d;
    const score = (beta) => {
      const weights = times.map((t) => Math.pow(t, beta));
      const weightedLog = sum(weights.map((w, i) => w * Math.log(times[i]))) / sum(weights);
      return 1 / beta + meanLogFail - weightedLog;
    };
    const lo = 0.1;
    const hi = 20;
    let prevX = lo;
    let prevY = score(prevX);
    let bracket = null;
    for (let i = 1; i < 400; i += 1) {
      const x = lo + ((hi - lo) * i) / 399;
      const y = score(x);
      if (prevY === 0 || y === 0 || prevY * y < 0) {
        bracket = [prevX, x];
        break;
      }
      prevX = x;
      prevY = y;
    }
    if (!bracket) return null;
    let [a, b] = bracket;
    for (let i = 0; i < 80; i += 1) {
      const mid = (a + b) / 2;
      if (score(a) * score(mid) <= 0) b = mid;
      else a = mid;
    }
    const beta = (a + b) / 2;
    const eta = Math.pow(sum(times.map((t) => Math.pow(t, beta))) / d, 1 / beta);
    return { beta, eta, logLikelihood: logLikelihood(lives, beta, eta) };
  }

  // _weibull_confidence_intervals: equation W9 from a finite-difference
  // Hessian on log β and log η. Nulls wherever GREMLIN reports Not available.
  function confidenceIntervals(lives, beta, eta) {
    const none = [null, null, null, null];
    if (beta <= 0 || eta <= 0 || lives.length < 3) return none;
    const tb = Math.log(beta);
    const te = Math.log(eta);
    const hb = Math.max(1e-4, Math.abs(tb) * 1e-4);
    const he = Math.max(1e-4, Math.abs(te) * 1e-4);
    const ll = (x, y) => logLikelihoodFromLogParams(lives, x, y);
    const f00 = ll(tb, te);
    if (!isFinite(f00)) return none;
    const h11 = (ll(tb + hb, te) - 2 * f00 + ll(tb - hb, te)) / (hb * hb);
    const h22 = (ll(tb, te + he) - 2 * f00 + ll(tb, te - he)) / (he * he);
    const h12 = (ll(tb + hb, te + he) - ll(tb + hb, te - he) - ll(tb - hb, te + he) + ll(tb - hb, te - he)) / (4 * hb * he);
    const i11 = -h11;
    const i12 = -h12;
    const i22 = -h22;
    const det = i11 * i22 - i12 * i12;
    if (![i11, i12, i22, det].every(isFinite) || det <= 0 || i11 <= 0 || i22 <= 0) return none;
    const varBeta = i22 / det;
    const varEta = i11 / det;
    if (![varBeta, varEta].every(isFinite) || varBeta <= 0 || varEta <= 0) return none;
    const seBeta = Math.sqrt(varBeta);
    const seEta = Math.sqrt(varEta);
    return [Math.exp(tb - Z_95 * seBeta), Math.exp(tb + Z_95 * seBeta), Math.exp(te - Z_95 * seEta), Math.exp(te + Z_95 * seEta)];
  }

  // _kaplan_meier_points: equation W13 at each age where a complete life ends,
  // with its probability plot coordinates (equation W14), y null where the
  // estimate is 0 or 1 and the plot cannot draw it.
  function kaplanMeier(lives) {
    const groups = new Map();
    lives.forEach(([t, failed]) => {
      const group = groups.get(t) || { failures: 0, censored: 0 };
      if (failed) group.failures += 1;
      else group.censored += 1;
      groups.set(t, group);
    });
    let survival = 1;
    let atRisk = lives.length;
    const points = [];
    Array.from(groups.keys())
      .sort((p, q) => p - q)
      .forEach((t) => {
        const { failures, censored } = groups.get(t);
        if (failures && atRisk > 0) {
          survival *= Math.max(0, 1 - failures / atRisk);
          points.push({
            t,
            atRisk,
            failures,
            survival,
            cdf: 1 - survival,
            x: Math.log(t),
            y: survival > 0 && survival < 1 ? Math.log(-Math.log(Math.max(survival, 1e-12))) : null,
          });
        }
        atRisk -= failures + censored;
      });
    return points;
  }

  // _probability_plot_r_squared: equation W15 over the points the plot draws.
  function rSquared(points) {
    const xy = points.filter((p) => p.y != null && p.x != null);
    if (xy.length < 3) return null;
    const meanX = sum(xy.map((p) => p.x)) / xy.length;
    const meanY = sum(xy.map((p) => p.y)) / xy.length;
    const sxx = sum(xy.map((p) => (p.x - meanX) ** 2));
    const syy = sum(xy.map((p) => (p.y - meanY) ** 2));
    const sxy = sum(xy.map((p) => (p.x - meanX) * (p.y - meanY)));
    if (sxx <= 0 || syy <= 0) return null;
    return Math.min(1, (sxy * sxy) / (sxx * syy));
  }

  function rSquaredThreshold(failures) {
    const table = R_SQUARED_REVIEW_THRESHOLDS;
    if (failures <= table[0][0]) return table[0][1];
    for (let i = 1; i < table.length; i += 1) {
      const [lowN, lowR] = table[i - 1];
      const [highN, highR] = table[i];
      if (failures <= highN) return lowR + ((highR - lowR) * (failures - lowN)) / (highN - lowN);
    }
    return table[table.length - 1][1];
  }

  // Lanczos' approximation, good to about 15 digits: all MTTF needs.
  const LANCZOS = [
    0.99999999999980993, 676.5203681218851, -1259.1392167224028, 771.32342877765313,
    -176.61502916214059, 12.507343278686905, -0.13857109526572012, 9.9843695780195716e-6,
    1.5056327351493116e-7,
  ];

  function gamma(z) {
    if (z < 0.5) return Math.PI / (Math.sin(Math.PI * z) * gamma(1 - z));
    const x = z - 1;
    let series = LANCZOS[0];
    for (let i = 1; i < LANCZOS.length; i += 1) series += LANCZOS[i] / (x + i);
    const t = x + 7.5;
    return Math.sqrt(2 * Math.PI) * Math.pow(t, x + 0.5) * Math.exp(-t) * series;
  }

  function meanLife(beta, eta) {
    return eta * gamma(1 + 1 / beta);
  }

  // Equation W11 for any share p: the age by which p of the lives have failed.
  function bLife(p, beta, eta) {
    return eta * Math.pow(-Math.log(1 - p), 1 / beta);
  }

  // The interpretation summary's readings, worded as the table under Reading
  // the result words them.
  const READINGS = {
    early: { label: "Early-life or defect-driven failures", direction: "Look at installation, setup, commissioning and repair quality, and latent defects. Age-based replacement is not supported." },
    random: { label: "Random failures", direction: "Improve detection with inspections or condition checks, and plan spares. A fixed replacement age is weak justification." },
    wearOut: { label: "Wear-out", direction: "Evaluate an age-based PM or planned replacement, set with η, downtime impact and cost." },
    betaUncertain: { label: "Pattern still uncertain", direction: "Check whether the population mixes mechanisms or needs more failures before choosing a strategy." },
    betaStable: { label: "Stable", direction: "More confidence in the direction above. The upper limit is then no more than about twice the lower, which about 20 failures reach at any β." },
    betaCaution: { label: "Treat with caution", direction: "Check whether the population mixes mechanisms, the data is weak, or there are still too few failures before choosing a strategy." },
    etaStable: { label: "Stable η", direction: "Usable for planning comparisons and timing discussions." },
    etaDirectional: { label: "Directional only", direction: "Tighten the population or collect more data before treating η as a decision point." },
    rSquaredLow: { label: "Less straight than chance alone explains", direction: "Review the population before acting on β: mixed mechanisms, a missed start point or a duplicate work order." },
    rSquaredOk: { label: "No sign of a mixed population", direction: "Still read the plot: a bend or two slopes matters even then." },
  };

  function betaReading(beta) {
    if (beta < 0.9) return "early";
    if (beta <= 1.1) return "random";
    return "wearOut";
  }

  function readings(result) {
    const { beta, eta } = result.fit;
    const rows = [{ metric: "β", value: fmt(beta, 3), key: betaReading(beta) }];
    const ci = result.ci;
    if (ci) {
      const crosses = ci.betaLo < 1 && ci.betaHi > 1;
      const stable = ci.betaHi - ci.betaLo <= BETA_INTERVAL_STABLE_FRACTION * beta;
      rows.push({
        metric: "β 95% interval",
        value: `${fmt(ci.betaLo, 3)} to ${fmt(ci.betaHi, 3)}, ${fmt((100 * (ci.betaHi - ci.betaLo)) / beta, 0)}% of β`,
        key: crosses ? "betaUncertain" : stable ? "betaStable" : "betaCaution",
      });
      rows.push({
        metric: "η 95% interval",
        value: `${hours(ci.etaLo)} to ${hours(ci.etaHi)}, ${fmt((100 * (ci.etaHi - ci.etaLo)) / eta, 0)}% of η`,
        key: (ci.etaHi - ci.etaLo) / eta <= ETA_INTERVAL_STABLE_FRACTION ? "etaStable" : "etaDirectional",
      });
    } else {
      rows.push({ metric: "β and η 95% intervals", value: "Not available", label: "Not available", direction: "Treat the reading with care until the fit and its data are reviewed." });
    }
    if (result.rSquared == null) {
      rows.push({ metric: "Probability plot R²", value: "Not available", label: "Not available", direction: "Fewer than three points on the plot, so how straight they lie cannot be judged." });
    } else {
      rows.push({
        metric: "Probability plot R²",
        value: `${fmt(result.rSquared, 3)}, review below ${fmt(result.threshold, 3)}`,
        key: result.rSquared < result.threshold ? "rSquaredLow" : "rSquaredOk",
      });
    }
    return rows.map((row) => (row.key ? { ...row, ...READINGS[row.key] } : row));
  }

  // Everything the lab and the chart figures show for one set of lives. `lives`
  // are the lives that ended in a failure or a PM reset; `current` is the life
  // running at the cutoff, censored like a PM reset but drawn as the red line.
  function analyse(set) {
    const lives = set.lives.slice();
    const current = set.current > 0 ? set.current : null;
    if (current) lives.push([current, 0]);
    const failures = lives.filter((life) => life[1]).length;
    const km = kaplanMeier(lives);
    const result = {
      lives,
      current,
      pmResets: set.lives.filter((life) => !life[1]).map((life) => life[0]),
      n: lives.length,
      r: failures,
      km,
      rSquared: rSquared(km),
      threshold: rSquaredThreshold(failures),
      fit: null,
      ci: null,
      problem: null,
    };
    if (failures < MIN_FAILURE_LIVES) {
      result.problem = "few";
      return result;
    }
    const fitted = fit(lives);
    if (!fitted) {
      result.problem = "no-root";
      return result;
    }
    result.fit = fitted;
    const [betaLo, betaHi, etaLo, etaHi] = confidenceIntervals(lives, fitted.beta, fitted.eta);
    result.ci = betaLo == null ? null : { betaLo, betaHi, etaLo, etaHi };
    result.mttf = meanLife(fitted.beta, fitted.eta);
    result.b10 = bLife(0.1, fitted.beta, fitted.eta);
    result.b50 = bLife(0.5, fitted.beta, fitted.eta);
    result.aic = 4 - 2 * fitted.logLikelihood;
    result.bic = 2 * Math.log(lives.length) - 2 * fitted.logLikelihood;
    result.readings = readings(result);
    return result;
  }

  /* -------------------------------------------------------------- formatting */

  const FORMATS = new Map();

  function fmt(value, digits) {
    if (value == null || !isFinite(value)) return "n/a";
    let format = FORMATS.get(digits);
    if (!format) {
      format = new Intl.NumberFormat("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
      FORMATS.set(digits, format);
    }
    // Rounded first, so a value that rounds to nothing never prints as "-0.0".
    const rounded = Number(value.toFixed(digits));
    return format.format(rounded === 0 ? 0 : rounded).replace("-", "−");
  }

  // Hours as the card writes them: one decimal under a thousand, none above.
  function hours(value, digits) {
    if (digits == null) digits = Math.abs(value) >= 1000 ? 0 : 1;
    return `${fmt(value, digits)} h`;
  }

  // A share as a percentage, with more digits when it is small.
  function pct(share, digits) {
    if (digits == null) {
      if (share > 0 && share < 0.0001) return "under 0.01%";
      digits = share > 0 && share < 0.001 ? 2 : 1;
    }
    return `${fmt(100 * share, digits)}%`;
  }

  function esc(text) {
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  /* ----------------------------------------------------------------- drawing */

  // An SVG coordinate: one decimal, and never so far off the drawing that a
  // curve heading for infinity breaks the path.
  function coord(value) {
    const r = Math.round(Math.max(-5000, Math.min(5000, value)) * 10) / 10;
    return Object.is(r, -0) ? "0" : String(r);
  }

  function tag(name, attrs, inner) {
    const attributes = Object.entries(attrs)
      .filter(([, value]) => value != null && value !== false)
      .map(([key, value]) => ` ${key}="${esc(value)}"`)
      .join("");
    return inner == null ? `<${name}${attributes}/>` : `<${name}${attributes}>${inner}</${name}>`;
  }

  function label(x, y, text, cls, anchor) {
    return tag("text", { class: cls || "fig-text-small", x: coord(x), y: coord(y), "text-anchor": anchor || null }, esc(text));
  }

  function linePath(points, sx, sy) {
    let d = "";
    let pen = false;
    points.forEach(([x, y]) => {
      if (!isFinite(x) || !isFinite(y)) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${coord(sx(x))} ${coord(sy(y))}`;
      pen = true;
    });
    return d;
  }

  function curve(points, sx, sy, cls) {
    const d = linePath(points, sx, sy);
    return d ? tag("path", { class: cls, d }) : "";
  }

  function sample(fn, from, to, steps) {
    const points = [];
    for (let i = 0; i <= steps; i += 1) {
      const t = from + ((to - from) * i) / steps;
      points.push([t, fn(t)]);
    }
    return points;
  }

  function dot(x, y, cls, r) {
    return tag("circle", { class: cls, cx: coord(x), cy: coord(y), r: r || 4.5 });
  }

  function decimalsFor(step) {
    for (let d = 0; d < 7; d += 1) {
      const scaled = step * Math.pow(10, d);
      if (Math.abs(Math.round(scaled) - scaled) < 1e-6) return d;
    }
    return 6;
  }

  function niceStep(span, count) {
    const raw = span / count;
    const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / magnitude;
    const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
    return step * magnitude;
  }

  function linearTicks(min, max, count) {
    if (!(max > min)) return [];
    const step = niceStep(max - min, count || 5);
    const digits = decimalsFor(step);
    const ticks = [];
    const first = Math.ceil(min / step - 1e-9);
    for (let i = first; i * step <= max + step * 1e-9; i += 1) {
      const v = i * step;
      ticks.push({ v, label: fmt(v, digits) });
    }
    return ticks;
  }

  // Ticks for an axis of hours on a log scale: 1, 2 and 5 of each decade when
  // the axis is short enough to hold them, the decades alone when it is not.
  function logHourTicks(lnMin, lnMax) {
    const lo = Math.exp(lnMin);
    const hi = Math.exp(lnMax);
    const ticks = [];
    for (let k = Math.floor(Math.log10(lo)) - 1; k <= Math.ceil(Math.log10(hi)); k += 1) {
      [1, 2, 5].forEach((m) => {
        const v = m * Math.pow(10, k);
        if (v >= lo && v <= hi) ticks.push(v);
      });
    }
    const chosen = ticks.length > 7 ? ticks.filter((v) => Math.abs(Math.log10(v) - Math.round(Math.log10(v))) < 1e-9) : ticks;
    return chosen.map((v) => ({ v: Math.log(v), label: fmt(v, v < 1 ? 1 : 0) }));
  }

  // The probability plot's y axis, ln(-ln R), labelled with the share failed.
  // Past 2 every label would read 100.0%, so the ticks stop there.
  function weibullShareTicks(yMin, yMax) {
    const ticks = [];
    let step = 1;
    if (Math.floor(yMax) - Math.ceil(yMin) < 1) step = 0.5;
    for (let y = Math.ceil(yMin / step) * step; y <= Math.min(yMax, 2) + 1e-9; y += step) {
      if (y < -7) continue;
      ticks.push({ v: y, label: pct(1 - Math.exp(-Math.exp(y)), 1) });
    }
    return ticks;
  }

  const SHARE_TICKS = [0, 0.25, 0.5, 0.75, 1].map((v) => ({ v, label: `${v * 100}%` }));

  // One plotting area: frame, gridlines, ticks and axis titles, and the scales
  // and clip that the caller draws into it with.
  function pane(box, spec) {
    const m = Object.assign({ l: 58, r: 14, t: 30, b: 42 }, spec.margin || {});
    const left = box.x + m.l;
    const right = box.x + box.w - m.r;
    const top = box.y + m.t;
    const bottom = box.y + box.h - m.b;
    const [x0, x1] = spec.x;
    const [y0, y1] = spec.y;
    const sx = (v) => left + ((v - x0) / (x1 - x0 || 1)) * (right - left);
    const sy = (v) => bottom - ((v - y0) / (y1 - y0 || 1)) * (bottom - top);
    const parts = [];
    if (spec.title) parts.push(label(left, box.y + 16, spec.title, "fig-text-strong"));
    (spec.yTicks || []).forEach((tick) => {
      parts.push(tag("path", { class: "fig-grid", d: `M${coord(left)} ${coord(sy(tick.v))} H${coord(right)}` }));
      parts.push(label(left - 6, sy(tick.v) + 3.5, tick.label, "fig-text-small", "end"));
    });
    (spec.xTicks || []).forEach((tick) => {
      parts.push(tag("path", { class: "fig-grid", d: `M${coord(sx(tick.v))} ${coord(top)} V${coord(bottom)}` }));
      parts.push(label(sx(tick.v), bottom + 14, tick.label, "fig-text-small", "middle"));
    });
    parts.push(tag("path", { class: "fig-axis", d: `M${coord(left)} ${coord(top)} V${coord(bottom)} H${coord(right)}` }));
    if (spec.xLabel) parts.push(label((left + right) / 2, bottom + 32, spec.xLabel, "fig-text-small", "middle"));
    if (spec.yLabel) {
      parts.push(
        tag("text", { class: "fig-text-small", transform: `translate(${coord(box.x + 13)} ${coord((top + bottom) / 2)}) rotate(-90)`, "text-anchor": "middle" }, esc(spec.yLabel))
      );
    }
    const clip = tag("clipPath", { id: spec.id }, tag("rect", { x: coord(left), y: coord(top), width: coord(right - left), height: coord(bottom - top) }));
    return {
      left,
      right,
      top,
      bottom,
      sx,
      sy,
      inX: (v) => v >= x0 && v <= x1,
      frame: clip + parts.join(""),
      clipped: (markup) => tag("g", { "clip-path": `url(#${spec.id})` }, markup),
    };
  }

  // A key along the top right of a pane, laid out from the right.
  function legend(p, y, items) {
    let x = p.right;
    const parts = [];
    items
      .slice()
      .reverse()
      .forEach((item) => {
        const width = item.text.length * 5.4;
        parts.push(label(x, y, item.text, "fig-text-small", "end"));
        x -= width + 6;
        if (item.line) parts.push(tag("path", { class: item.line, d: `M${coord(x - 18)} ${coord(y - 3.5)} H${coord(x)}` }));
        if (item.point) parts.push(dot(x - 6, y - 3.5, item.point, 3.5));
        x -= 26;
      });
    return parts.join("");
  }

  function guide(p, x, y, cls) {
    return tag("path", { class: cls || "fig-guide", d: `M${coord(p.left)} ${coord(y)} H${coord(x)} V${coord(p.bottom)}` });
  }

  function vertical(p, x, cls) {
    return tag("path", { class: cls, d: `M${coord(x)} ${coord(p.top)} V${coord(p.bottom)}` });
  }

  // A label beside a point, on whichever side keeps it inside the pane.
  function sideLabel(p, x, y, text, cls) {
    const right = x < p.left + (p.right - p.left) * 0.62;
    const inside = Math.max(p.top + 10, Math.min(p.bottom - 4, y - 6));
    return label(right ? x + 8 : x - 8, inside, text, cls || "fig-text-small", right ? "start" : "end");
  }

  const QUADRANTS = [
    { x: 0, y: 0, w: 380, h: 280 },
    { x: 380, y: 0, w: 380, h: 280 },
    { x: 0, y: 280, w: 380, h: 280 },
    { x: 380, y: 280, w: 380, h: 280 },
  ];

  // Densities and hazards read as failures per 1,000 life hours on the axes:
  // per-hour values are too small to label.
  const PER_THOUSAND = 1000;

  function upperTicks(max) {
    return linearTicks(0, max, 4);
  }

  /* W4: the distribution explorer --------------------------------------------- */

  const EXPLORER_MAX_AGE = 2500;

  function drawExplorer(state) {
    const { beta, eta, age } = state;
    const X = EXPLORER_MAX_AGE;
    const xTicks = linearTicks(0, X, 5);
    const ageOn = age > 0 && age <= X;
    const parts = [];

    // F(t) and R(t).
    const p1 = pane(QUADRANTS[0], { id: "wbl-ex-clip-1", title: "F(t) and R(t)", x: [0, X], y: [0, 1], xTicks, yTicks: SHARE_TICKS, xLabel: "Age, life hours", yLabel: "Share of lives" });
    const fPoints = sample((t) => unreliability(t, beta, eta), 0, X, 160);
    let inner = "";
    if (eta <= X) inner += guide(p1, p1.sx(eta), p1.sy(1 - Math.exp(-1)));
    inner += curve(fPoints.map(([t, f]) => [t, 1 - f]), p1.sx, p1.sy, "fig-line-1 fig-dashed");
    inner += curve(fPoints, p1.sx, p1.sy, "fig-accent-line");
    if (ageOn) {
      const f = unreliability(age, beta, eta);
      inner += vertical(p1, p1.sx(age), "fig-age-line");
      inner += dot(p1.sx(age), p1.sy(f), "fig-age-dot") + dot(p1.sx(age), p1.sy(1 - f), "fig-age-dot");
    }
    parts.push(p1.frame, p1.clipped(inner));
    if (eta <= X) parts.push(label(p1.sx(eta) + 4, p1.sy(1 - Math.exp(-1)) - 5, "η: 63.2% failed", "fig-text-small"));
    if (ageOn) {
      const f = unreliability(age, beta, eta);
      parts.push(sideLabel(p1, p1.sx(age), p1.sy(f) + (f < 0.5 ? 18 : 0), `F = ${pct(f)}`, "fig-text-accent"));
      parts.push(sideLabel(p1, p1.sx(age), p1.sy(1 - f) + (f < 0.5 ? 0 : 18), `R = ${pct(1 - f)}`, "fig-text-strong"));
    }
    parts.push(legend(p1, QUADRANTS[0].y + 16, [{ line: "fig-accent-line", text: "F, failed" }, { line: "fig-line-1 fig-dashed", text: "R, running" }]));

    // f(t), with the area up to the age shaded: that area is F(t).
    const fFrom = beta < 1 ? X * 0.04 : 0;
    const densityMax = Math.max(...sample((t) => density(t, beta, eta), fFrom, X, 200).map(([, v]) => v).filter(isFinite)) * PER_THOUSAND * 1.12;
    const p2 = pane(QUADRANTS[1], { id: "wbl-ex-clip-2", title: "f(t), probability density", x: [0, X], y: [0, densityMax], xTicks, yTicks: upperTicks(densityMax), xLabel: "Age, life hours", yLabel: "Per 1,000 h" });
    const dPoints = sample((t) => density(t, beta, eta) * PER_THOUSAND, 0, X, 160);
    inner = "";
    if (ageOn) {
      const area = dPoints.filter(([t]) => t <= age).concat([[age, density(age, beta, eta) * PER_THOUSAND]]);
      const d = `M${coord(p2.sx(0))} ${coord(p2.sy(0))}` + linePath(area, p2.sx, p2.sy).replace(/^M/, "L") + `L${coord(p2.sx(age))} ${coord(p2.sy(0))}Z`;
      inner += tag("path", { class: "fig-area", d });
    }
    const mttf = meanLife(beta, eta);
    if (mttf <= X) inner += vertical(p2, p2.sx(mttf), "fig-guide");
    inner += curve(dPoints, p2.sx, p2.sy, "fig-accent-line");
    if (ageOn) inner += vertical(p2, p2.sx(age), "fig-age-line");
    parts.push(p2.frame, p2.clipped(inner));
    if (mttf <= X) parts.push(label(p2.sx(mttf) + 4, p2.top + 12, "MTTF", "fig-text-small"));
    if (ageOn) parts.push(sideLabel(p2, p2.sx(age), p2.top + 34, `shaded area = F = ${pct(unreliability(age, beta, eta))}`, "fig-text-accent"));

    // h(t), against the level hazard of β = 1 at the same η.
    const level = (1 / eta) * PER_THOUSAND;
    const hazardMax = Math.max(level, ...sample((t) => hazard(t, beta, eta), X * 0.04, X, 200).map(([, v]) => v).filter(isFinite).map((v) => v * PER_THOUSAND)) * 1.15;
    const p3 = pane(QUADRANTS[2], { id: "wbl-ex-clip-3", title: "h(t), hazard rate", x: [0, X], y: [0, hazardMax], xTicks, yTicks: upperTicks(hazardMax), xLabel: "Age, life hours", yLabel: "Per 1,000 h" });
    inner = tag("path", { class: "fig-guide", d: `M${coord(p3.left)} ${coord(p3.sy(level))} H${coord(p3.right)}` });
    inner += curve(sample((t) => hazard(t, beta, eta) * PER_THOUSAND, 0, X, 160), p3.sx, p3.sy, "fig-accent-line");
    if (ageOn) {
      const h = hazard(age, beta, eta) * PER_THOUSAND;
      inner += vertical(p3, p3.sx(age), "fig-age-line") + dot(p3.sx(age), p3.sy(h), "fig-age-dot");
    }
    parts.push(p3.frame, p3.clipped(inner));
    parts.push(label(p3.right - 4, Math.max(p3.top + 10, p3.sy(level) - 5), "β = 1 at this η", "fig-text-small", "end"));
    if (ageOn) {
      const h = hazard(age, beta, eta) * PER_THOUSAND;
      if (h <= hazardMax) parts.push(sideLabel(p3, p3.sx(age), Math.max(p3.top + 14, p3.sy(h)), `h = ${fmt(h, h < 10 ? 2 : 0)}`, "fig-text-accent"));
    }

    // The probability plot: a straight line of slope β through ln η.
    const lnMin = Math.log(10);
    const lnMax = Math.log(10000);
    const p4 = pane(QUADRANTS[3], { id: "wbl-ex-clip-4", title: "Probability plot", x: [lnMin, lnMax], y: [-5, 2], xTicks: logHourTicks(lnMin, lnMax), yTicks: weibullShareTicks(-5, 2), xLabel: "Age, life hours (log scale)", yLabel: "Share failed" });
    const lnEta = Math.log(eta);
    inner = guide(p4, p4.sx(lnEta), p4.sy(0));
    inner += curve([[lnMin, beta * (lnMin - lnEta)], [lnMax, beta * (lnMax - lnEta)]], p4.sx, p4.sy, "fig-accent-line");
    if (age >= 10) {
      const y = Math.log(-Math.log(reliability(age, beta, eta)));
      inner += vertical(p4, p4.sx(Math.log(age)), "fig-age-line") + dot(p4.sx(Math.log(age)), p4.sy(y), "fig-age-dot");
    }
    parts.push(p4.frame, p4.clipped(inner));
    parts.push(label(p4.left + 8, p4.top + 14, `slope = β = ${fmt(beta, 2)}`, "fig-text-accent"));
    parts.push(label(p4.sx(lnEta) + 4, p4.sy(0) - 6, "η", "fig-text-small"));
    return parts.join("");
  }

  function explorerReading(beta) {
    const shape =
      beta < 1
        ? "The hazard falls with age: a part that has run a while is safer than a new one."
        : beta === 1
          ? "The hazard is level: age makes no difference to the risk."
          : "The hazard rises with age: an older part is more likely to fail than a new one.";
    const rule =
      beta < 0.9
        ? "The interpretation summary would read it as early-life or defect-driven failures."
        : beta <= 1.1
          ? "The interpretation summary would read it as random failures (β from 0.9 to 1.1)."
          : "The interpretation summary would read it as wear-out.";
    return `<strong>β ${fmt(beta, 2)}.</strong> ${shape} ${rule}`;
  }

  function explorerReadout(state) {
    const { beta, eta, age } = state;
    const stats = [
      ["η, 63.2% failed", hours(eta)],
      ["B10, 10% failed", hours(bLife(0.1, beta, eta))],
      ["B50, the median life", hours(bLife(0.5, beta, eta))],
      ["MTTF, the mean life", hours(meanLife(beta, eta))],
    ];
    const html = [`<p>${explorerReading(beta)}</p>`];
    html.push(`<dl class="wbl-stats">${stats.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>`);
    if (age > 0) {
      const r = reliability(age, beta, eta);
      const h = hazard(age, beta, eta) * PER_THOUSAND;
      html.push(
        `<p><strong>At ${esc(hours(age, 0))}:</strong> F = ${pct(1 - r)} and R = ${pct(r)}, so of 100 parts started new together about ${fmt(100 * r, 0)} are still running. The hazard is ${fmt(h, h < 10 ? 2 : 0)} per 1,000 life hours.</p>`
      );
      const fresh = failureWithin(0, WEEK_LIFE_HOURS, beta, eta);
      const aged = failureWithin(age, WEEK_LIFE_HOURS, beta, eta);
      const ratio = aged / fresh;
      let comparison;
      if (ratio > 1.05) {
        const times = ratio >= 1000 ? "over 1,000" : fmt(ratio, ratio >= 10 ? 0 : 1);
        comparison = `${times} times as likely to fail in them as a new part (${pct(aged)} against ${pct(fresh)}). Renewing it now would lower the risk.`;
      } else if (ratio < 0.95) {
        comparison = `less likely to fail in them than a new part (${pct(aged)} against ${pct(fresh)}). Renewing it now would raise the risk.`;
      } else {
        comparison = `about as likely to fail in them as a new part (${pct(aged)} against ${pct(fresh)}). Renewing it now would change little.`;
      }
      html.push(`<p><strong>The next 100 life hours</strong>, a calendar week on the 20-hour schedule (equation C5): a part that has already run ${esc(hours(age, 0))} is ${comparison}</p>`);
    } else {
      html.push("<p>Move the age above zero to read the curves there and to compare a part that has already run with a new one.</p>");
    }
    return html.join("");
  }

  /* W5: the likelihood explorer ----------------------------------------------- */

  const LIKELIHOOD_MAX_AGE = 1500;
  const LIKELIHOOD_DENSITY_MAX = 3;
  const PROFILE_BETA = [0.3, 6];

  function profile(lives, beta) {
    return logLikelihood(lives, beta, etaForBeta(lives, beta));
  }

  function drawLikelihood(state, model) {
    const { beta, eta } = state;
    const { lives, best, ci } = model;
    const parts = [];
    const top = { x: 0, y: 0, w: 760, h: 290 };
    const p1 = pane(top, {
      id: "wbl-ll-clip-1",
      title: `Your guess, f(t) at β ${fmt(beta, 2)} and η ${hours(eta, 0)}, and each life's score`,
      x: [0, LIKELIHOOD_MAX_AGE],
      y: [0, LIKELIHOOD_DENSITY_MAX],
      xTicks: linearTicks(0, LIKELIHOOD_MAX_AGE, 6),
      yTicks: linearTicks(0, LIKELIHOOD_DENSITY_MAX, 3),
      xLabel: "Life hours",
      yLabel: "Per 1,000 h",
    });
    let inner = curve(sample((t) => density(t, beta, eta) * PER_THOUSAND, 0, LIKELIHOOD_MAX_AGE, 200), p1.sx, p1.sy, "fig-accent-line");
    const labels = [];
    lives.forEach(([t, failed]) => {
      const x = p1.sx(t);
      if (failed) {
        const y = p1.sy(Math.min(density(t, beta, eta) * PER_THOUSAND, LIKELIHOOD_DENSITY_MAX));
        inner += tag("path", { class: "fig-vote", d: `M${coord(x)} ${coord(p1.bottom)} V${coord(y)}` }) + dot(x, y, "fig-point", 5);
        labels.push(label(x, Math.max(p1.top + 10, y - 10), hours(t, 0), "fig-text-small", "middle"));
      } else {
        const y = p1.bottom - 12;
        inner += tag("path", { class: "fig-danger-line", d: `M${coord(x)} ${coord(y)} H${coord(x + 40)}` });
        inner += tag("path", { class: "fig-danger", d: `M${coord(x + 40)} ${coord(y - 5)} L${coord(x + 50)} ${coord(y)} L${coord(x + 40)} ${coord(y + 5)} Z` });
        inner += dot(x, y, "fig-censored", 4.5);
        labels.push(label(x, y - 10, hours(t, 0), "fig-text-danger", "middle"));
      }
    });
    parts.push(p1.frame, p1.clipped(inner), labels.join(""));
    parts.push(legend(p1, 16, [{ point: "fig-point", text: "failure: scored by f(t)" }, { point: "fig-censored", text: "censored: scored by R(t)" }]));

    const bottom = { x: 0, y: 300, w: 760, h: 220 };
    const yMax = best.logLikelihood + 1.5;
    const yMin = best.logLikelihood - 10;
    const p2 = pane(bottom, {
      id: "wbl-ll-clip-2",
      title: "Profile log-likelihood: the best ℓ for each β, with η from equation W7",
      x: PROFILE_BETA,
      y: [yMin, yMax],
      xTicks: linearTicks(PROFILE_BETA[0], PROFILE_BETA[1], 8),
      yTicks: linearTicks(yMin, yMax, 4),
      xLabel: "β",
      yLabel: "ℓ",
    });
    inner = "";
    if (ci) inner += tag("rect", { class: "fig-band", x: coord(p2.sx(ci.betaLo)), y: coord(p2.top), width: coord(p2.sx(ci.betaHi) - p2.sx(ci.betaLo)), height: coord(p2.bottom - p2.top) });
    inner += tag("path", { class: "fig-guide", d: `M${coord(p2.left)} ${coord(p2.sy(best.logLikelihood))} H${coord(p2.right)}` });
    inner += curve(sample((b) => profile(lives, b), PROFILE_BETA[0], PROFILE_BETA[1], 160), p2.sx, p2.sy, "fig-accent-line");
    inner += dot(p2.sx(best.beta), p2.sy(best.logLikelihood), "fig-point", 5);
    const yours = logLikelihood(lives, beta, eta);
    const shownY = Math.max(yMin, yours);
    inner += vertical(p2, p2.sx(beta), "fig-age-line");
    inner += dot(p2.sx(beta), p2.sy(shownY), "fig-age-dot", 5.5);
    parts.push(p2.frame, p2.clipped(inner));
    if (ci) parts.push(label(p2.sx((ci.betaLo + ci.betaHi) / 2), p2.bottom - 8, "95% interval for β, equation W9", "fig-text-small", "middle"));
    parts.push(label(p2.sx(best.beta), p2.sy(best.logLikelihood) - 10, `best: β ${fmt(best.beta, 3)}, ℓ ${fmt(best.logLikelihood, 2)}`, "fig-text-strong", "middle"));
    parts.push(sideLabel(p2, p2.sx(beta), p2.sy(shownY) + 22, yours < yMin ? `yours: ℓ ${fmt(yours, 2)}, below the chart` : `yours: ℓ ${fmt(yours, 2)}`, "fig-text-highlight"));
    return parts.join("");
  }

  function lifeKind(life, index, lives, current) {
    if (life[1]) return "Failure";
    return current != null && index === lives.length - 1 ? "Current life" : "PM reset";
  }

  function likelihoodReadout(state, model) {
    const { beta, eta } = state;
    const { lives, best } = model;
    const rows = lives
      .map((life, index) => {
        const [t, failed] = life;
        const score = failed ? density(t, beta, eta) : reliability(t, beta, eta);
        const scoredBy = failed ? `f(${fmt(t, 0)}), the density` : `R(${fmt(t, 0)}), surviving`;
        return `<tr><td>${index + 1}</td><td>${lifeKind(life, index, lives, model.current)}</td><td class="is-numeric">${fmt(t, 0)}</td><td>${scoredBy}</td><td class="is-numeric">${fmt(score, failed ? 5 : 3)}</td><td class="is-numeric">${fmt(Math.log(score), 2)}</td></tr>`;
      })
      .join("");
    const total = logLikelihood(lives, beta, eta);
    const gap = best.logLikelihood - total;
    const table =
      '<div class="std-table-scroll"><table class="std-table wbl-table">' +
      '<caption class="visually-hidden">Each life\'s score under the guess</caption>' +
      '<thead><tr><th scope="col">Life</th><th scope="col">Ends in</th><th scope="col" class="is-numeric">Hours</th><th scope="col">Scored by</th><th scope="col" class="is-numeric">Score</th><th scope="col" class="is-numeric">ln score</th></tr></thead>' +
      `<tbody>${rows}</tbody>` +
      `<tfoot><tr><th scope="row" colspan="5">Log-likelihood ℓ, the sum of the ln scores</th><td class="is-numeric">${fmt(total, 2)}</td></tr></tfoot>` +
      "</table></div>";
    const verdict =
      gap < 0.005
        ? "That is the maximum: no other β and η make these lives more likely."
        : `That is ${fmt(gap, 2)} below the best these lives allow, ${fmt(best.logLikelihood, 2)} at β ${fmt(best.beta, 3)} and η ${hours(best.eta)}: the fitted values. A gap of ${fmt(gap, 2)} in ℓ means the lives were about ${fmt(Math.exp(Math.min(gap, 50)), Math.exp(gap) < 10 ? 1 : 0)} times less likely under your guess.`;
    return `${table}<p>${verdict}</p>`;
  }

  /* W6 and W9: the four charts ------------------------------------------------- */

  function chartDomain(result) {
    const maxLife = Math.max(1, ...result.lives.map((life) => life[0]));
    return result.fit ? Math.max(maxLife * 1.15, result.fit.eta * 1.25, 1) : maxLife * 1.15;
  }

  function noFit(p, text) {
    return label((p.left + p.right) / 2, (p.top + p.bottom) / 2, text, "fig-text-small", "middle");
  }

  function drawCharts(result, opts) {
    const id = opts.id;
    const age = opts.age > 0 ? opts.age : null;
    const fitted = result.fit;
    const beta = fitted && fitted.beta;
    const eta = fitted && fitted.eta;
    const ci = result.ci;
    const ciPairs = ci ? [[ci.betaLo, ci.etaLo], [ci.betaHi, ci.etaHi]] : [];
    let upper = chartDomain(result);
    if (age && age > upper) upper = age * 1.05;
    const steps = 120;
    const parts = [];

    // 1) The Weibull Probability Plot, scaled as Perform an Analysis scales it.
    const points = result.km.filter((p) => p.y != null);
    const markers = result.current ? [Math.log(result.current)] : [];
    const xs = points.map((p) => p.x).concat(markers);
    let xMin;
    let xMax;
    let yMin;
    let yMax;
    if (fitted) {
      const lnEta = Math.log(eta);
      xMin = Math.min(xs.length ? Math.min(...xs) : lnEta - 1, lnEta - 1) - 0.3;
      xMax = Math.max(xs.length ? Math.max(...xs) : lnEta + 1, lnEta + 1) + 0.3;
      const ys = points.map((p) => p.y).concat([beta * (xMin - lnEta), beta * (xMax - lnEta)]);
      yMin = Math.min(...ys) - 0.3;
      yMax = Math.max(...ys) + 0.3;
    } else {
      xMin = (xs.length ? Math.min(...xs) : Math.log(10)) - 0.3;
      xMax = (xs.length ? Math.max(...xs) : Math.log(1000)) + 0.3;
      const ys = points.map((p) => p.y);
      yMin = (ys.length ? Math.min(...ys) : -3) - 0.3;
      yMax = (ys.length ? Math.max(...ys) : 1) + 0.3;
      if (yMax - yMin < 1.5) {
        yMin -= 0.75;
        yMax += 0.75;
      }
    }
    const p1 = pane(QUADRANTS[0], { id: `${id}-clip-1`, title: "Weibull Probability Plot", x: [xMin, xMax], y: [yMin, yMax], xTicks: logHourTicks(xMin, xMax), yTicks: weibullShareTicks(yMin, yMax), xLabel: "ln(life hours), labelled in hours", yLabel: "ln(−ln R), as % failed" });
    let inner = "";
    if (fitted) {
      const lnEta = Math.log(eta);
      if (opts.annotate) inner += guide(p1, p1.sx(lnEta), p1.sy(0));
      ciPairs.forEach(([b, e]) => {
        inner += curve([[xMin, b * (xMin - Math.log(e))], [xMax, b * (xMax - Math.log(e))]], p1.sx, p1.sy, "fig-ci-line");
      });
      inner += curve([[xMin, beta * (xMin - lnEta)], [xMax, beta * (xMax - lnEta)]], p1.sx, p1.sy, "fig-accent-line");
    }
    markers.forEach((x) => {
      inner += vertical(p1, p1.sx(x), "fig-danger-line");
    });
    if (age && fitted && p1.inX(Math.log(age))) {
      const x = Math.log(age);
      inner += vertical(p1, p1.sx(x), "fig-age-line") + dot(p1.sx(x), p1.sy(beta * (x - Math.log(eta))), "fig-age-dot");
    }
    points.forEach((p) => {
      inner += dot(p1.sx(p.x), p1.sy(p.y), "fig-point");
    });
    if (opts.ring != null) {
      const ringed = points.find((p) => p.t === opts.ring);
      if (ringed) inner += dot(p1.sx(ringed.x), p1.sy(ringed.y), "fig-ring", 10);
    }
    parts.push(p1.frame, p1.clipped(inner));
    if (fitted && opts.annotate) {
      parts.push(label(p1.sx(Math.log(eta)) + 4, p1.bottom - 6, "η", "fig-text-small"));
      parts.push(label(p1.left + 8, p1.top + 14, `slope = β = ${fmt(beta, 2)}`, "fig-text-accent"));
    }

    // 2) CDF.
    const xTicks = linearTicks(0, upper, 4);
    const p2 = pane(QUADRANTS[1], { id: `${id}-clip-2`, title: "CDF", x: [0, upper], y: [0, 1], xTicks, yTicks: SHARE_TICKS, xLabel: "Life hours", yLabel: "CDF, share failed" });
    inner = "";
    if (fitted) {
      ciPairs.forEach(([b, e]) => {
        inner += curve(sample((t) => unreliability(t, b, e), 0, upper, steps), p2.sx, p2.sy, "fig-ci-line");
      });
      inner += curve(sample((t) => unreliability(t, beta, eta), 0, upper, steps), p2.sx, p2.sy, "fig-accent-line");
    }
    if (result.current) inner += vertical(p2, p2.sx(result.current), "fig-danger-line");
    if (age && fitted) inner += vertical(p2, p2.sx(age), "fig-age-line") + dot(p2.sx(age), p2.sy(unreliability(age, beta, eta)), "fig-age-dot");
    if (fitted && opts.annotate) {
      [result.b10, result.b50, eta].forEach((t) => {
        const y = p2.sy(unreliability(t, beta, eta));
        inner += tag("path", { class: "fig-guide", d: `M${coord(p2.sx(t))} ${coord(y)} V${coord(p2.bottom)}` });
        inner += dot(p2.sx(t), y, "fig-accent", 3.5);
      });
    }
    result.km.forEach((p) => {
      inner += dot(p2.sx(p.t), p2.sy(p.cdf), "fig-point");
    });
    if (fitted) {
      result.pmResets.forEach((t) => {
        inner += dot(p2.sx(t), p2.sy(unreliability(t, beta, eta)), "fig-censored");
      });
    }
    parts.push(p2.frame, p2.clipped(inner));
    if (fitted && opts.annotate) {
      [
        [result.b10, "B10"],
        [result.b50, "B50"],
        [eta, "η"],
      ].forEach(([t, name]) => {
        parts.push(label(p2.sx(t) - 3, p2.bottom - 5, name, "fig-text-accent", "end"));
      });
    }

    // 3) PDF and 4) Hazard Rate: the fitted curves alone.
    const curvePanes = [
      { box: QUADRANTS[2], n: 3, title: "Probability Density (PDF)", fn: density, yLabel: "Density, per 1,000 h" },
      { box: QUADRANTS[3], n: 4, title: "Hazard Rate", fn: hazard, yLabel: "Hazard, per 1,000 h" },
    ];
    curvePanes.forEach((spec) => {
      if (!fitted) {
        const p = pane(spec.box, { id: `${id}-clip-${spec.n}`, title: spec.title, x: [0, upper], y: [0, 1], xTicks, yTicks: [], xLabel: "Life hours", yLabel: spec.yLabel });
        const why = result.problem === "few" ? "No fit: fewer than five lives end in a failure" : "No fit: no β between 0.1 and 20 maximises the likelihood";
        parts.push(p.frame, noFit(p, why));
        return;
      }
      const values = sample((t) => spec.fn(t, beta, eta) * PER_THOUSAND, upper / steps, upper, steps - 1);
      const max = Math.max(...values.map(([, v]) => v).filter(isFinite), 1e-9) * 1.05;
      const p = pane(spec.box, { id: `${id}-clip-${spec.n}`, title: spec.title, x: [0, upper], y: [0, max], xTicks, yTicks: upperTicks(max), xLabel: "Life hours", yLabel: spec.yLabel });
      let body = "";
      if (spec.n === 3 && opts.annotate && result.mttf <= upper) body += vertical(p, p.sx(result.mttf), "fig-guide");
      body += curve([[0, spec.fn(0, beta, eta) * PER_THOUSAND]].concat(values), p.sx, p.sy, "fig-accent-line");
      if (result.current) body += vertical(p, p.sx(result.current), "fig-danger-line");
      if (age) body += vertical(p, p.sx(age), "fig-age-line") + dot(p.sx(age), p.sy(spec.fn(age, beta, eta) * PER_THOUSAND), "fig-age-dot");
      parts.push(p.frame, p.clipped(body));
      if (spec.n === 3 && opts.annotate && result.mttf <= upper) parts.push(label(p.sx(result.mttf) + 4, p.top + 12, "MTTF", "fig-text-small"));
    });
    return parts.join("");
  }

  function chartsReadout(result, age) {
    if (!result.fit) return "";
    const { beta, eta } = result.fit;
    const html = [];
    if (age > 0) {
      const f = unreliability(age, beta, eta);
      html.push(
        `<p><strong>At ${esc(hours(age, 0))}</strong>, on the fitted curve: <strong>CDF</strong> ${pct(f)} of lives have failed by then; <strong>PDF</strong> ${fmt(density(age, beta, eta) * PER_THOUSAND, 2)} per 1,000 h, how thickly failures fall at that age; <strong>Hazard Rate</strong> ${fmt(hazard(age, beta, eta) * PER_THOUSAND, 2)} per 1,000 h, the risk for a part that has got that far; <strong>Probability Plot</strong> ln(−ln R) = ${fmt(Math.log(-Math.log(1 - f)), 2)}, the same ${pct(f)} on the stretched scale.</p>`
      );
    }
    if (result.current) {
      const c = result.current;
      const window = RISK_WINDOW_WEEKS * WEEK_LIFE_HOURS;
      html.push(
        `<p><strong>The red line</strong> is the current life, ${esc(hours(c, 0))}. ${pct(unreliability(c, beta, eta))} of lives would have failed by that age; the chance this one ends in a failure within the next ${RISK_WINDOW_WEEKS} weeks (${fmt(window, 0)} life hours on the 20-hour schedule) is ${pct(failureWithin(c, window, beta, eta))}, by equation C5.</p>`
      );
    }
    return html.join("");
  }

  /* W7: straightening the curve ------------------------------------------------ */

  const STRAIGHTEN_BOX = { x: 0, y: 0, w: 640, h: 300 };
  const STRAIGHTEN_MARGIN = { l: 80, r: 80, t: 30, b: 50 };
  const STRAIGHTEN_LN = [5.5, 7.3];
  const STRAIGHTEN_LINEAR = [0, 1500];

  // Where a life of t hours with share failed F sits at each step.
  function straightenXY(step, t, f) {
    const x = step === 1 ? t : Math.log(t);
    const y = step === 3 ? Math.log(-Math.log(1 - f)) : f;
    return [x, y];
  }

  function straightenPane(step) {
    const linear = step === 1;
    const weibull = step === 3;
    const xTicks = linear ? linearTicks(0, 1500, 6) : [250, 500, 1000, 1400].map((v) => ({ v: Math.log(v), label: `${fmt(v, 0)} h` }));
    const yTicks = weibull ? [-3, -2, -1, 0, 1].map((v) => ({ v, label: pct(1 - Math.exp(-Math.exp(v))) })) : SHARE_TICKS;
    return pane(STRAIGHTEN_BOX, {
      id: "wbl-st-clip",
      margin: STRAIGHTEN_MARGIN,
      x: linear ? STRAIGHTEN_LINEAR : STRAIGHTEN_LN,
      y: weibull ? [-3, 1] : [0, 1],
      xTicks,
      yTicks,
      xLabel: linear ? "Life hours" : "Life hours (log scale)",
      yLabel: weibull ? "Unreliability F(t), Weibull scale" : "Unreliability F(t)",
    });
  }

  function drawStraighten(step, model, from, progress) {
    const { beta, eta } = model.fit;
    const target = straightenPane(step);
    const origin = from ? straightenPane(from) : target;
    const k = from ? progress : 1;
    // A position on screen, part way from where it was to where it is going.
    const place = (t, f) => {
      const [x1, y1] = straightenXY(step, t, f);
      const end = [target.sx(x1), target.sy(y1)];
      if (!from || k >= 1) return end;
      const [x0, y0] = straightenXY(from, t, f);
      const start = [origin.sx(x0), origin.sy(y0)];
      return [start[0] + (end[0] - start[0]) * k, start[1] + (end[1] - start[1]) * k];
    };
    const screen = (pairs) => {
      let d = "";
      pairs.forEach(([x, y], i) => {
        d += `${i ? "L" : "M"}${coord(x)} ${coord(y)}`;
      });
      return d;
    };
    const ts = [];
    for (let i = 0; i <= 200; i += 1) ts.push(40 + ((1600 - 40) * i) / 200);
    const curvePoints = ts.map((t) => place(t, unreliability(t, beta, eta)));
    const etaPoint = place(eta, 1 - Math.exp(-1));
    let inner = tag("path", { class: "fig-guide", d: `M${coord(target.left)} ${coord(etaPoint[1])} H${coord(etaPoint[0])} V${coord(target.bottom)}` });
    inner += tag("path", { class: "fig-accent-line", d: screen(curvePoints) });
    const pointLabels = [];
    model.km.forEach((p) => {
      const [x, y] = place(p.t, p.cdf);
      inner += dot(x, y, "fig-point", 5);
      pointLabels.push(label(x - 12, y - 9, hours(p.t, 0), "fig-text-small", "end"));
    });
    const parts = [target.frame, target.clipped(inner), pointLabels.join("")];
    if (k >= 1) {
      parts.push(label(etaPoint[0] + 8, target.bottom - 14, `η = ${hours(eta, 0)}`, "fig-text-small"));
      if (step === 3) parts.push(label(target.sx(Math.log(560)), target.sy(-0.65) + 40, `slope β = ${fmt(beta, 2)}`, "fig-text-accent"));
    }
    return parts.join("");
  }

  /* W8: probability plot patterns ---------------------------------------------- */

  const PATTERN_BOX = { x: 0, y: 0, w: 640, h: 300 };

  // Least squares through some of the plotted points: a line, or a parabola
  // when `bend` is set, drawn dashed to show the shape the points make.
  function leastSquares(points, bend) {
    const n = points.length;
    const mx = sum(points.map((p) => p.x)) / n;
    const rows = points.map((p) => (bend ? [1, p.x - mx, (p.x - mx) ** 2] : [1, p.x - mx]));
    const size = rows[0].length;
    const a = [];
    for (let i = 0; i < size; i += 1) {
      a.push([]);
      for (let j = 0; j < size; j += 1) a[i].push(sum(rows.map((row) => row[i] * row[j])));
      a[i].push(sum(rows.map((row, r) => row[i] * points[r].y)));
    }
    for (let i = 0; i < size; i += 1) {
      for (let r = 0; r < size; r += 1) {
        if (r === i) continue;
        const factor = a[r][i] / a[i][i];
        for (let c = i; c <= size; c += 1) a[r][c] -= factor * a[i][c];
      }
    }
    const coef = a.map((row, i) => row[size] / row[i]);
    return (x) => coef[0] + coef[1] * (x - mx) + (bend ? coef[2] * (x - mx) ** 2 : 0);
  }

  function drawPattern(key, result) {
    const { beta, eta } = result.fit;
    const lnEta = Math.log(eta);
    const points = result.km.filter((p) => p.y != null);
    const xs = points.map((p) => p.x);
    const xMin = Math.min(...xs, lnEta - 1) - 0.3;
    const xMax = Math.max(...xs, lnEta + 1) + 0.3;
    const ys = points.map((p) => p.y).concat([beta * (xMin - lnEta), beta * (xMax - lnEta)]);
    const yMin = Math.min(...ys) - 0.3;
    const yMax = Math.max(...ys) + 0.3;
    const p = pane(PATTERN_BOX, { id: "wbl-pt-clip", margin: { l: 80, r: 30, t: 24, b: 50 }, x: [xMin, xMax], y: [yMin, yMax], xTicks: logHourTicks(xMin, xMax), yTicks: weibullShareTicks(yMin, yMax), xLabel: "Life hours (log scale)", yLabel: "Unreliability F(t), Weibull scale" });
    let inner = curve([[xMin, beta * (xMin - lnEta)], [xMax, beta * (xMax - lnEta)]], p.sx, p.sy, "fig-accent-line");
    const notes = [];
    if (key === "mixed") {
      // Split the points at the widest gap in age and draw each side's own line.
      let split = 1;
      for (let i = 1; i < points.length; i += 1) if (points[i].x - points[i - 1].x > points[split].x - points[split - 1].x) split = i;
      [points.slice(0, split), points.slice(split)].forEach((side) => {
        if (side.length < 2) return;
        const lineFn = leastSquares(side, false);
        const a = side[0].x - 0.25;
        const b = side[side.length - 1].x + 0.25;
        inner += curve([[a, lineFn(a)], [b, lineFn(b)]], p.sx, p.sy, "fig-line-6 fig-dashed");
      });
      notes.push(label(p.sx((points[split - 1].x + points[split].x) / 2), p.top + 14, "two groups, two slopes", "fig-text-highlight", "middle"));
    } else if (key === "failure_free") {
      const bendFn = leastSquares(points, true);
      inner += curve(sample(bendFn, xs[0] - 0.1, xs[xs.length - 1] + 0.1, 60), p.sx, p.sy, "fig-line-6 fig-dashed");
      notes.push(label(p.left + 10, p.top + 14, "the points bend: steep at the left, flatter to the right", "fig-text-highlight", "start"));
    } else if (key === "duplicate") {
      const first = points[0];
      inner += dot(p.sx(first.x), p.sy(first.y), "fig-ring", 11);
      notes.push(label(p.sx(first.x) + 16, p.sy(first.y) + 4, `a ${hours(first.t)} life: a duplicate work order`, "fig-text-highlight"));
    }
    points.forEach((pt) => {
      inner += dot(p.sx(pt.x), p.sy(pt.y), "fig-point", 4.5);
    });
    return [p.frame, p.clipped(inner), notes.join("")].join("");
  }

  function patternNumbers(result) {
    const passes = result.rSquared >= result.threshold;
    return `<strong>In this plot: β ${fmt(result.fit.beta, 2)}, η ${esc(hours(result.fit.eta, 0))}.</strong> ${result.r} lives end in a failure. R² ${fmt(result.rSquared, 3)} is ${passes ? "at or above" : "below"} its review threshold of ${fmt(result.threshold, 3)}${passes ? "" : ", so the interpretation summary flags the fit for review"}.`;
  }

  /* W9: the lab ---------------------------------------------------------------- */

  function labResults(result, age) {
    const html = [];
    const lives = `${result.n} ${result.n === 1 ? "life" : "lives"}, ${result.r} ending in a failure`;
    if (result.problem === "few") {
      html.push(`<p class="wbl-status is-stop">GREMLIN would not fit these: ${lives}. It needs at least ${MIN_FAILURE_LIVES} lives that end in a failure, so the run stops and says so. The points show what the lives say on their own.</p>`);
    } else if (result.problem === "no-root") {
      html.push(`<p class="wbl-status is-stop">GREMLIN would not fit these: ${lives}, but no β between 0.1 and 20 maximises the likelihood, so the fit did not converge. Nearly identical lives, often duplicate work orders, do this.</p>`);
    } else {
      html.push(`<p class="wbl-status">GREMLIN would fit these: ${lives}.</p>`);
    }
    const items = [];
    const fitted = result.fit;
    if (fitted) {
      const ci = result.ci;
      items.push(["β, equation W8", `<span class="std-result">${fmt(fitted.beta, 3)}</span>${ci ? `, 95% interval ${fmt(ci.betaLo, 3)} to ${fmt(ci.betaHi, 3)}` : ", interval not available"}`]);
      items.push(["η, equation W7", `<span class="std-result">${hours(fitted.eta)}</span>${ci ? `, 95% interval ${hours(ci.etaLo)} to ${hours(ci.etaHi)}` : ""}`]);
      items.push(["MTTF, equation W10", `${hours(result.mttf)}, about ${fmt(result.mttf / WEEK_LIFE_HOURS, 1)} calendar weeks on the 20-hour schedule`]);
      items.push(["B10 and B50, equation W11", `${hours(result.b10)} and ${hours(result.b50)}`]);
      if (age > 0) {
        const r = reliability(age, fitted.beta, fitted.eta);
        items.push([`Reliability at ${hours(age, 0)}, equation W2`, `R = ${fmt(r, 3)}, so F = ${fmt(1 - r, 3)}`]);
      }
      if (result.current) {
        const window = RISK_WINDOW_WEEKS * WEEK_LIFE_HOURS;
        items.push([`Current life ${hours(result.current, 0)}: chance of a failure within ${RISK_WINDOW_WEEKS} weeks, equation C5`, pct(failureWithin(result.current, window, fitted.beta, fitted.eta))]);
      }
    }
    items.push(["Probability plot R², equation W15", result.rSquared == null ? "Not available: fewer than three points" : `${fmt(result.rSquared, 3)}; review threshold ${fmt(result.threshold, 3)} for ${result.r} failures`]);
    if (fitted) items.push(["Log-likelihood, AIC, BIC", `${fmt(fitted.logLikelihood, 2)}; ${fmt(result.aic, 2)}; ${fmt(result.bic, 2)}`]);
    html.push(`<ul class="std-calc">${items.map(([k, v]) => `<li><span>${esc(k)}</span><span>${v}</span></li>`).join("")}</ul>`);
    return html.join("");
  }

  // What the interpretation summary would say, in the columns of the table
  // under Reading the result.
  function labReadings(result) {
    if (!result.fit) return "";
    const rows = result.readings.map((row) => `<tr><td>${esc(row.metric)}</td><td>${esc(row.value)}</td><td><strong>${esc(row.label)}</strong></td><td>${esc(row.direction)}</td></tr>`).join("");
    return (
      '<div class="std-table-scroll"><table class="std-table wbl-table"><caption class="visually-hidden">What the interpretation summary would say</caption>' +
      '<thead><tr><th scope="col">Result</th><th scope="col">Value</th><th scope="col">Reading</th><th scope="col">Suggested direction</th></tr></thead>' +
      `<tbody>${rows}</tbody></table></div>`
    );
  }

  /* ---------------------------------------------------- opening states, prerender */

  function workedModel(data) {
    const preset = data.presets.worked;
    const result = analyse(preset);
    return { result, lives: result.lives, current: result.current, best: result.fit, ci: result.ci, km: result.km, fit: result.fit };
  }

  function openingStates(data) {
    const worked = workedModel(data);
    return {
      explorer: { beta: worked.fit.beta, eta: worked.fit.eta, age: 400 },
      likelihood: { beta: 1, eta: 1200, lock: false },
      charts: { age: 400 },
      straighten: { step: 3 },
      patterns: { key: data.patterns[0] },
      lab: { preset: "worked", age: 400 },
    };
  }

  // Every figure at its opening state, keyed by the names of the macros in
  // templates/documentation_weibull_drawings.html that hold them.
  // tools/prerender_weibull_figures.py writes that file from this; the test
  // compares the two.
  function prerender(data) {
    const states = openingStates(data);
    const worked = workedModel(data);
    const patternResult = analyse(data.presets[states.patterns.key]);
    const lab = analyse(data.presets[states.lab.preset]);
    return {
      explorer_plot: drawExplorer(states.explorer),
      explorer_readout: explorerReadout(states.explorer),
      likelihood_plot: drawLikelihood(states.likelihood, worked),
      likelihood_readout: likelihoodReadout(states.likelihood, worked),
      charts_plot: drawCharts(worked.result, { id: "wbl-ch", age: states.charts.age, annotate: true }),
      charts_readout: chartsReadout(worked.result, states.charts.age),
      straighten_plot: drawStraighten(states.straighten.step, worked),
      patterns_plot: drawPattern(states.patterns.key, patternResult),
      patterns_numbers: patternNumbers(patternResult),
      lab_plot: drawCharts(lab, { id: "wbl-lab", age: states.lab.age }),
      lab_results: labResults(lab, states.lab.age),
      lab_readings: labReadings(lab),
    };
  }

  /* ---------------------------------------------------------------- controls */

  const reduceMotion = () => typeof window !== "undefined" && window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Redraws at most once a frame, however fast a slider fires.
  function scheduler(draw) {
    let pending = false;
    return () => {
      if (pending) return;
      pending = true;
      requestAnimationFrame(() => {
        pending = false;
        draw();
      });
    };
  }

  function bindSlider(figure, key, onInput) {
    const input = figure.querySelector(`[data-input="${key}"]`);
    const shown = figure.querySelector(`[data-value="${key}"]`);
    if (input) input.addEventListener("input", () => onInput(Number(input.value)));
    return {
      input,
      set(value, text) {
        if (input && Number(input.value) !== value) input.value = String(value);
        if (input) input.setAttribute("aria-valuetext", text);
        if (shown) shown.textContent = text;
      },
    };
  }

  function pressOnly(buttons, chosen) {
    buttons.forEach((button) => button.setAttribute("aria-pressed", String(button === chosen)));
  }

  function setupExplorer(figure, data, states) {
    const state = { ...states.explorer };
    const worked = workedModel(data);
    const plot = figure.querySelector("[data-plot]");
    const readout = figure.querySelector("[data-readout]");
    const presets = Array.from(figure.querySelectorAll("[data-beta-preset]"));
    const render = () => {
      plot.innerHTML = drawExplorer(state);
      readout.innerHTML = explorerReadout(state);
      sliders.beta.set(state.beta, fmt(state.beta, 2));
      sliders.eta.set(state.eta, hours(state.eta, 0));
      sliders.age.set(state.age, hours(state.age, 0));
    };
    const schedule = scheduler(render);
    const sliders = {
      beta: bindSlider(figure, "beta", (v) => {
        state.beta = v;
        pressOnly(presets, null);
        schedule();
      }),
      eta: bindSlider(figure, "eta", (v) => {
        state.eta = v;
        pressOnly(presets, null);
        schedule();
      }),
      age: bindSlider(figure, "age", (v) => {
        state.age = v;
        schedule();
      }),
    };
    presets.forEach((button) => {
      button.addEventListener("click", () => {
        if (button.dataset.betaPreset === "worked") {
          state.beta = worked.fit.beta;
          state.eta = worked.fit.eta;
        } else {
          state.beta = Number(button.dataset.betaPreset);
        }
        pressOnly(presets, button);
        render();
      });
    });
    render();
  }

  function setupLikelihood(figure, data, states) {
    const model = workedModel(data);
    const state = { ...states.likelihood };
    const plot = figure.querySelector("[data-plot]");
    const readout = figure.querySelector("[data-readout]");
    const lock = figure.querySelector("[data-input='lock']");
    const render = () => {
      if (state.lock) state.eta = etaForBeta(model.lives, state.beta);
      plot.innerHTML = drawLikelihood(state, model);
      readout.innerHTML = likelihoodReadout(state, model);
      sliders.beta.set(Math.round(state.beta * 100) / 100, fmt(state.beta, 2));
      sliders.eta.set(Math.round(state.eta), hours(state.eta, 0));
      if (sliders.eta.input) sliders.eta.input.disabled = state.lock;
    };
    const schedule = scheduler(render);
    const sliders = {
      beta: bindSlider(figure, "beta", (v) => {
        state.beta = v;
        schedule();
      }),
      eta: bindSlider(figure, "eta", (v) => {
        state.eta = v;
        schedule();
      }),
    };
    if (lock) {
      lock.addEventListener("change", () => {
        state.lock = lock.checked;
        render();
      });
    }
    figure.querySelectorAll("[data-action]").forEach((button) => {
      button.addEventListener("click", () => {
        if (button.dataset.action === "best") {
          state.beta = model.best.beta;
          state.eta = model.best.eta;
        } else {
          Object.assign(state, states.likelihood);
          if (lock) lock.checked = state.lock;
        }
        render();
      });
    });
    render();
  }

  function setupCharts(figure, data, states) {
    const model = workedModel(data);
    const state = { ...states.charts };
    const plot = figure.querySelector("[data-plot]");
    const readout = figure.querySelector("[data-readout]");
    const render = () => {
      plot.innerHTML = drawCharts(model.result, { id: "wbl-ch", age: state.age, annotate: true });
      readout.innerHTML = chartsReadout(model.result, state.age);
      slider.set(state.age, hours(state.age, 0));
    };
    const schedule = scheduler(render);
    const slider = bindSlider(figure, "age", (v) => {
      state.age = v;
      schedule();
    });
    render();
  }

  function setupStraighten(figure, data, states) {
    const model = workedModel(data);
    const plot = figure.querySelector("[data-plot]");
    const buttons = Array.from(figure.querySelectorAll("[data-step]"));
    const notes = Array.from(figure.querySelectorAll("[data-step-note]"));
    let step = states.straighten.step;
    let animation = null;
    const show = (next) => {
      const from = step;
      step = next;
      buttons.forEach((button) => button.setAttribute("aria-pressed", String(Number(button.dataset.step) === step)));
      notes.forEach((note) => {
        note.hidden = Number(note.dataset.stepNote) !== step;
      });
      if (animation) cancelAnimationFrame(animation);
      if (from === next || reduceMotion()) {
        plot.innerHTML = drawStraighten(step, model);
        return;
      }
      const started = performance.now();
      const frame = (now) => {
        const linear = Math.min(1, (now - started) / 700);
        const eased = linear < 0.5 ? 2 * linear * linear : 1 - Math.pow(-2 * linear + 2, 2) / 2;
        plot.innerHTML = drawStraighten(step, model, from, eased);
        animation = linear < 1 ? requestAnimationFrame(frame) : null;
      };
      animation = requestAnimationFrame(frame);
    };
    buttons.forEach((button) => button.addEventListener("click", () => show(Number(button.dataset.step))));
    show(step);
  }

  function setupPatterns(figure, data, states, openInLab) {
    const plot = figure.querySelector("[data-plot]");
    const numbers = figure.querySelector("[data-pattern-numbers]");
    const buttons = Array.from(figure.querySelectorAll("[data-pattern]"));
    const notes = Array.from(figure.querySelectorAll("[data-pattern-note]"));
    const open = figure.querySelector("[data-open-in-lab]");
    let key = states.patterns.key;
    const show = (next) => {
      key = next;
      const result = analyse(data.presets[key]);
      plot.innerHTML = drawPattern(key, result);
      numbers.innerHTML = patternNumbers(result);
      buttons.forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.pattern === key)));
      notes.forEach((note) => {
        note.hidden = note.dataset.patternNote !== key;
      });
    };
    buttons.forEach((button) => button.addEventListener("click", () => show(button.dataset.pattern)));
    if (open && openInLab) open.addEventListener("click", () => openInLab(key));
    show(key);
  }

  function setupLab(figure, data, states) {
    const plot = figure.querySelector("[data-plot]");
    const results = figure.querySelector("[data-results]");
    const readingsNode = figure.querySelector("[data-readings]");
    const body = figure.querySelector("[data-lab-rows]");
    const select = figure.querySelector("[data-lab-preset]");
    const current = figure.querySelector("[data-lab-current]");
    const age = figure.querySelector("[data-lab-age]");
    const add = figure.querySelector("[data-lab-add]");
    let rows = [];

    data.presetOrder.forEach((key) => {
      const option = document.createElement("option");
      option.value = key;
      option.textContent = data.presets[key].label;
      select.appendChild(option);
    });
    const custom = document.createElement("option");
    custom.value = "";
    custom.textContent = "Your own lives";
    custom.hidden = true;
    select.appendChild(custom);

    const readRows = () => {
      const lives = [];
      rows.forEach((row, index) => {
        const input = body.querySelectorAll("[data-life-hours]")[index];
        const value = Number(row.hours);
        const valid = row.hours !== "" && isFinite(value) && value > 0;
        if (input) input.setAttribute("aria-invalid", String(row.hours !== "" && !valid));
        if (valid) lives.push([value, row.failed ? 1 : 0]);
      });
      return lives;
    };

    const compute = () => {
      const lives = readRows();
      const currentValue = Number(current.value);
      const set = { lives, current: current.value !== "" && currentValue > 0 ? currentValue : null };
      const ageValue = Number(age.value);
      const result = analyse(set);
      const readAge = age.value !== "" && ageValue > 0 ? ageValue : null;
      plot.innerHTML = drawCharts(result, { id: "wbl-lab", age: readAge });
      results.innerHTML = labResults(result, readAge);
      readingsNode.innerHTML = labReadings(result);
    };
    const schedule = scheduler(compute);

    const markCustom = () => {
      custom.hidden = false;
      select.value = "";
    };

    const renderRows = () => {
      body.textContent = "";
      rows.forEach((row, index) => {
        const tr = document.createElement("tr");
        const n = index + 1;
        tr.innerHTML =
          `<th scope="row">${n}</th>` +
          `<td><input class="wbl-input" type="number" min="0" step="any" inputmode="decimal" data-life-hours aria-label="Life ${n}, life hours"></td>` +
          `<td><select class="wbl-input" data-life-ends aria-label="Life ${n} ends in"><option value="1">Failure</option><option value="0">PM reset</option></select></td>` +
          `<td><button type="button" class="wbl-remove" data-life-remove aria-label="Remove life ${n}">&times;</button></td>`;
        const input = tr.querySelector("[data-life-hours]");
        const ends = tr.querySelector("[data-life-ends]");
        input.value = row.hours;
        ends.value = row.failed ? "1" : "0";
        input.addEventListener("input", () => {
          row.hours = input.value;
          markCustom();
          schedule();
        });
        ends.addEventListener("change", () => {
          row.failed = ends.value === "1";
          markCustom();
          schedule();
        });
        tr.querySelector("[data-life-remove]").addEventListener("click", () => {
          rows.splice(index, 1);
          markCustom();
          renderRows();
          const removes = body.querySelectorAll("[data-life-remove]");
          (removes[Math.min(index, removes.length - 1)] || add).focus();
          compute();
        });
        body.appendChild(tr);
      });
    };

    const load = (key) => {
      const preset = data.presets[key];
      rows = preset.lives.map(([t, failed]) => ({ hours: String(t), failed: Boolean(failed) }));
      current.value = preset.current ? String(preset.current) : "";
      custom.hidden = true;
      select.value = key;
      renderRows();
      compute();
    };

    select.addEventListener("change", () => {
      if (select.value) load(select.value);
    });
    add.addEventListener("click", () => {
      rows.push({ hours: "", failed: true });
      markCustom();
      renderRows();
      const inputs = body.querySelectorAll("[data-life-hours]");
      inputs[inputs.length - 1].focus();
      compute();
    });
    current.addEventListener("input", () => {
      markCustom();
      schedule();
    });
    age.addEventListener("input", schedule);
    age.value = String(states.lab.age);
    load(states.lab.preset);

    return {
      load(key) {
        load(key);
        figure.scrollIntoView({ block: "start", behavior: reduceMotion() ? "auto" : "smooth" });
        select.focus({ preventScroll: true });
      },
    };
  }

  function mount(doc) {
    const source = doc.getElementById("weibull-figure-data");
    if (!source) return;
    let data;
    try {
      data = JSON.parse(source.textContent);
    } catch (error) {
      return;
    }
    const states = openingStates(data);
    let lab = null;
    const setups = {
      lab: (figure) => {
        lab = setupLab(figure, data, states);
      },
      explorer: (figure) => setupExplorer(figure, data, states),
      likelihood: (figure) => setupLikelihood(figure, data, states),
      charts: (figure) => setupCharts(figure, data, states),
      straighten: (figure) => setupStraighten(figure, data, states),
      patterns: (figure) => setupPatterns(figure, data, states, lab ? (key) => lab.load(key) : null),
    };
    const figures = Array.from(doc.querySelectorAll("[data-weibull-figure]"));
    // The lab first: the patterns figure's "Open in the lab" loads into it.
    figures.sort((a, b) => (b.dataset.weibullFigure === "lab") - (a.dataset.weibullFigure === "lab"));
    figures.forEach((figure) => {
      const setup = setups[figure.dataset.weibullFigure];
      if (!setup) return;
      // One figure that cannot be wired keeps its drawing, without controls
      // that would not work, and leaves the others alone.
      try {
        setup(figure);
      } catch (error) {
        console.error(`Weibull figure "${figure.dataset.weibullFigure}" could not be set up`, error);
        return;
      }
      figure.querySelectorAll("[data-controls]").forEach((node) => {
        node.hidden = false;
      });
      figure.classList.add("is-live");
    });
  }

  return {
    method: {
      MIN_FAILURE_LIVES,
      BETA_INTERVAL_STABLE_FRACTION,
      ETA_INTERVAL_STABLE_FRACTION,
      R_SQUARED_REVIEW_THRESHOLDS,
      READINGS,
      reliability,
      unreliability,
      density,
      hazard,
      failureWithin,
      logLikelihood,
      etaForBeta,
      fit,
      confidenceIntervals,
      kaplanMeier,
      rSquared,
      rSquaredThreshold,
      gamma,
      meanLife,
      bLife,
      analyse,
    },
    prerender,
    mount,
  };
});
