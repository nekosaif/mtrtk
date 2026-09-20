import { useLayoutEffect, useRef, useState } from "react";
import type { Spectrum as SpectrumT } from "@/lib/types";
import { Legend } from "./Legend";

/**
 * Series colours by RF block: block 0 (L1) wears the brass, block 1 (L2/L5) the GPS blue, a
 * third block the Galileo green. The receiver has at most two.
 */
export const BLOCK_COLORS = ["var(--brass)", "var(--sys-gps)", "var(--sys-galileo)"] as const;

/** Bin amplitude scale (MON-SPAN gives one byte per bin) and its grid lines. */
export const LEVEL_MAX = 255;
export const LEVEL_REFS = [64, 128, 192] as const;

const LEFT = 34;
const RIGHT = 8;
const TOP = 6;
const BOTTOM = 18;
const ROW_GAP = 12;
const MIN_WIDTH = 320;
const DEFAULT_WIDTH = 640;
/** Every k-th bin goes in the table; 256 bins per block would be more than anyone reads. */
const TABLE_STEP = 8;

interface Block {
  s: SpectrumT;
  color: string;
  startMhz: number;
  endMhz: number;
  stepMhz: number;
  peak: { level: number; mhz: number };
}

const mhzOf = (b: Pick<Block, "startMhz" | "stepMhz">, j: number) => b.startMhz + j * b.stepMhz;
const fmtMhz = (mhz: number, digits = 1) => `${mhz.toFixed(digits)} MHz`;

function blocksOf(spectra: SpectrumT[]): Block[] {
  return [...spectra]
    .sort((a, b) => a.block_id - b.block_id)
    .map((s, i) => {
      const startMhz = (s.center_hz - s.span_hz / 2) / 1e6;
      const stepMhz = s.span_hz / 1e6 / Math.max(1, s.bins.length - 1);
      let peakIdx = 0;
      s.bins.forEach((v, j) => {
        if (v > s.bins[peakIdx]) peakIdx = j;
      });
      return {
        s,
        color: BLOCK_COLORS[i % BLOCK_COLORS.length],
        startMhz,
        endMhz: (s.center_hz + s.span_hz / 2) / 1e6,
        stepMhz,
        peak: { level: s.bins[peakIdx] ?? 0, mhz: startMhz + peakIdx * stepMhz },
      };
    });
}

/**
 * The receiver's spectrum analyser (MON-SPAN): one strip per RF block, stacked, each with its
 * own x axis in MHz (`center ± span/2` — the blocks sit on different bands, so one shared axis
 * would be mostly empty) and the same 0–255 amplitude scale. Hovering puts a crosshair through
 * every strip and prints MHz and level per block. Drawn at pixel size to the container's width,
 * scrolling sideways below 320 px. A `<details>` table lists every eighth bin.
 */
export function Spectrum({ spectra, rowHeight = 120, className }: { spectra: SpectrumT[]; rowHeight?: number; className?: string }) {
  const frameRef = useRef<HTMLDivElement>(null);
  const [frameWidth, setFrameWidth] = useState(0);
  const [hover, setHover] = useState<number | null>(null); // fraction across the plot, 0…1

  useLayoutEffect(() => {
    const el = frameRef.current;
    if (!el) return;
    const measure = () => setFrameWidth(el.clientWidth);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  if (spectra.length === 0) return <p className="text-ink-2">No spectrum data yet.</p>;

  const blocks = blocksOf(spectra);
  const width = Math.max(MIN_WIDTH, frameWidth || DEFAULT_WIDTH);
  const plotW = width - LEFT - RIGHT;
  const plotH = rowHeight - TOP - BOTTOM;
  const height = blocks.length * rowHeight + (blocks.length - 1) * ROW_GAP;
  const rowTop = (i: number) => i * (rowHeight + ROW_GAP);
  const x = (j: number, n: number) => LEFT + (n > 1 ? (j / (n - 1)) * plotW : 0);
  const y = (i: number, v: number) => rowTop(i) + TOP + plotH - (Math.max(0, Math.min(LEVEL_MAX, v)) / LEVEL_MAX) * plotH;

  const hoverAt = (b: Block) => {
    if (hover == null) return null;
    const idx = Math.round(hover * Math.max(0, b.s.bins.length - 1));
    return { idx, mhz: mhzOf(b, idx), level: b.s.bins[idx] ?? 0 };
  };

  const bands = blocks.map((b) => `${b.startMhz.toFixed(0)}–${b.endMhz.toFixed(0)} MHz`).join(" and ");
  const summary = `RF spectrum: ${blocks.length} block${blocks.length > 1 ? "s" : ""}, ${bands}`;
  const readout =
    hover == null
      ? "amplitude 0–255 per bin · hover for MHz and level"
      : blocks
          .map((b) => {
            const h = hoverAt(b)!;
            return `${blocks.length > 1 ? `block ${b.s.block_id} ` : ""}${fmtMhz(h.mhz, 2)} · ${h.level}`;
          })
          .join(" / ");

  return (
    <div className={className}>
      <div ref={frameRef} className="overflow-x-auto">
        <svg
          role="img"
          aria-label={summary}
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          className="block"
          onMouseMove={(e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            // jsdom (no layout) gives a zero rect: fall back to the drawn width so the hover math still holds
            const scale = rect.width > 0 ? width / rect.width : 1;
            const px = (e.clientX - rect.left) * scale;
            setHover(Math.max(0, Math.min(1, (px - LEFT) / plotW)));
          }}
          onMouseLeave={() => setHover(null)}
        >
          {blocks.map((b, i) => {
            const n = b.s.bins.length;
            const points = b.s.bins.map((v, j) => `${x(j, n).toFixed(1)},${y(i, v).toFixed(1)}`).join(" ");
            const ticks: [number, "start" | "middle" | "end"][] = [
              [b.startMhz, "start"],
              [b.s.center_hz / 1e6, "middle"],
              [b.endMhz, "end"],
            ];
            const h = hoverAt(b);
            return (
              <g key={b.s.block_id} data-row={b.s.block_id}>
                {LEVEL_REFS.map((lvl) => (
                  <g key={lvl}>
                    <line x1={LEFT} x2={width - RIGHT} y1={y(i, lvl)} y2={y(i, lvl)} stroke="var(--line)" strokeDasharray="3 3" />
                    <text data-level={lvl} x={LEFT - 6} y={y(i, lvl) + 4} textAnchor="end" fontSize={12} fill="var(--ink-3)" className="num">
                      {lvl}
                    </text>
                  </g>
                ))}
                <line x1={LEFT} x2={width - RIGHT} y1={y(i, 0)} y2={y(i, 0)} stroke="var(--line)" />
                <polyline data-block={b.s.block_id} points={points} fill="none" stroke={b.color} strokeWidth={1.5} strokeLinejoin="round">
                  <title>{`RF block ${b.s.block_id}: ${b.startMhz.toFixed(1)}–${b.endMhz.toFixed(1)} MHz, peak ${b.peak.level} at ${fmtMhz(b.peak.mhz, 2)}, PGA ${b.s.pga_db} dB`}</title>
                </polyline>
                {ticks.map(([mhz, anchor], k) => (
                  <text
                    key={k}
                    data-tick
                    x={anchor === "start" ? LEFT : anchor === "end" ? width - RIGHT : LEFT + plotW / 2}
                    y={rowTop(i) + rowHeight - 4}
                    textAnchor={anchor}
                    fontSize={12}
                    fill="var(--ink-3)"
                    className="num"
                  >
                    {fmtMhz(mhz)}
                  </text>
                ))}
                {h ? <circle cx={x(h.idx, n)} cy={y(i, h.level)} r={3} fill={b.color} stroke="var(--bg)" /> : null}
              </g>
            );
          })}
          {hover != null ? <line data-crosshair x1={LEFT + hover * plotW} x2={LEFT + hover * plotW} y1={0} y2={height} stroke="var(--ink-3)" strokeDasharray="2 2" /> : null}
        </svg>
      </div>
      <div className="mt-2 flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
        <Legend label="RF blocks" items={blocks.map((b) => ({ label: `RF block ${b.s.block_id}`, color: b.color, value: `centre ${fmtMhz(b.s.center_hz / 1e6)} · PGA ${b.s.pga_db} dB` }))} />
        <span data-testid="spectrum-readout" className="num min-h-4 text-[12px] leading-4 text-ink-2">
          {readout}
        </span>
      </div>
      <details className="mt-2 text-[12px] leading-4">
        <summary className="cursor-pointer text-ink-2 hover:text-ink">Table</summary>
        <p className="mt-2 text-ink-2">
          {blocks.map((b) => `Block ${b.s.block_id}: centre ${fmtMhz(b.s.center_hz / 1e6)}, span ${(b.s.span_hz / 1e6).toFixed(1)} MHz, resolution ${(b.s.res_hz / 1e3).toFixed(1)} kHz, PGA ${b.s.pga_db} dB, peak ${b.peak.level} at ${fmtMhz(b.peak.mhz, 2)}.`).join(" ")}{" "}
          Every {TABLE_STEP}th bin follows.
        </p>
        <table className="mt-2 w-full">
          <thead>
            <tr className="text-ink-2">
              <th scope="col" className="py-1 text-left font-medium">Block</th>
              <th scope="col" className="py-1 text-right font-medium">MHz</th>
              <th scope="col" className="py-1 text-right font-medium">Level</th>
            </tr>
          </thead>
          <tbody>
            {blocks.flatMap((b) =>
              b.s.bins
                .map((v, j) => [v, j] as const)
                .filter(([, j]) => j % TABLE_STEP === 0)
                .map(([v, j]) => (
                  <tr key={`${b.s.block_id}-${j}`} className="border-t border-line">
                    <td className="num py-1">{b.s.block_id}</td>
                    <td className="num py-1 text-right">{mhzOf(b, j).toFixed(1)}</td>
                    <td className="num py-1 text-right">{v}</td>
                  </tr>
                )),
            )}
          </tbody>
        </table>
      </details>
    </div>
  );
}
