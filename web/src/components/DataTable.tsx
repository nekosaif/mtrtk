import { isValidElement, useMemo, useState, type ReactNode } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import { cn } from "@/lib/utils";

export type SortDir = "asc" | "desc";
export type SortValue = number | string | boolean | null | undefined;

export interface Column<T> {
  key: string;
  header: string;
  cell: (row: T) => ReactNode;
  /** The value to sort by. Without it, a column sorts by its cell when the cell is a primitive. */
  sortValue?: (row: T) => SortValue;
  /** Right-aligned cells are numbers: they get `.num` and sort largest-first on the first click. */
  align?: "left" | "right";
  /** The direction of the first click; the default is `desc` for right-aligned columns, `asc` otherwise. */
  firstDir?: SortDir;
  width?: string;
  /**
   * The column's priority: below this width of the table's own box it is hidden, header and
   * cells. A container query, not the viewport — the same table sits in a full-width panel on
   * one page and a third of the grid on another. Columns without it are always shown. Where it
   * is hidden its value is not lost: it rides in the first cell as a small labelled line (see
   * `fold`), because below the width that hides it a phone scrolls the table anyway.
   */
  hideBelow?: keyof typeof HIDE_BELOW;
  /** The value a hidden column shows under the first cell; its cell when not given. Wraps anywhere. */
  fold?: (row: T) => ReactNode;
  /**
   * For one-token values (names, addresses) that must not break mid-word: past this CSS length
   * the cell ends in an ellipsis and the whole value is its tooltip.
   */
  truncate?: string;
  /** For free text (a user agent, a reason): the cell wraps at spaces instead of widening the table. */
  wrap?: boolean;
  /** The cell's tooltip; a truncated primitive cell gets its own text without it. */
  title?: (row: T) => string | undefined;
}

/**
 * The container widths a column's `hideBelow` names. Literal class names, so Tailwind sees them;
 * `@container` on the table's wrapper is what they query.
 */
export const HIDE_BELOW = {
  sm: "@max-[36rem]:hidden",
  md: "@max-[40rem]:hidden",
  lg: "@max-[44rem]:hidden",
  xl: "@max-[64rem]:hidden",
} as const;

/** The inverse of `HIDE_BELOW`: shown exactly where that priority's column is hidden. */
export const SHOW_BELOW = {
  sm: "hidden @max-[36rem]:block",
  md: "hidden @max-[40rem]:block",
  lg: "hidden @max-[44rem]:block",
  xl: "hidden @max-[64rem]:block",
} as const satisfies Record<keyof typeof HIDE_BELOW, string>;

/**
 * A hidden column's line under the first cell: small, labelled with its header, and free to wrap
 * anywhere so it never sets the lead column's minimum width.
 */
export function foldClass(priority: keyof typeof HIDE_BELOW): string {
  return cn("text-[12px] leading-4 text-ink-2 whitespace-normal wrap-anywhere", SHOW_BELOW[priority]);
}

/**
 * One line that ends in an ellipsis past `maxWidth`, with the whole text as its tooltip. Inside a
 * table cell the max-width also caps what the column asks for, so a long name cannot widen it.
 */
export function Truncate({ children, maxWidth, title, className }: { children: ReactNode; maxWidth: string; title?: string; className?: string }) {
  return (
    <span className={cn("block min-w-0 truncate", className)} style={{ maxWidth }} title={title ?? (typeof children === "string" ? children : undefined)}>
      {children}
    </span>
  );
}

const isPrimitive = (v: unknown): v is SortValue => v == null || typeof v === "string" || typeof v === "number" || typeof v === "boolean";

/** `sortValue` when the column has one, else the cell itself when it is a primitive. */
function valueOf<T>(c: Column<T>, row: T): SortValue {
  if (c.sortValue) return c.sortValue(row);
  const v = c.cell(row);
  return isValidElement(v) || !isPrimitive(v) ? null : v;
}

/** Nulls sort last in both directions; numbers numerically; everything else as text. */
function compare(a: SortValue, b: SortValue, dir: SortDir): number {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  let cmp: number;
  if (typeof a === "number" && typeof b === "number") cmp = a - b;
  else if (typeof a === "boolean" && typeof b === "boolean") cmp = Number(a) - Number(b);
  else cmp = String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
  return dir === "asc" ? cmp : -cmp;
}

/**
 * A sortable table. Click (or focus and press Enter/Space on) a header to sort by that column;
 * a second click reverses it; `aria-sort` on the header reports the state. The sort is stable and
 * puts missing values last. A column sorts by `sortValue` when given, else by its cell's primitive
 * value; a column whose cells are elements has no sort control. Right-aligned cells are numeric:
 * they carry `.num` so tabular figures line up and the page's stale rule can grey them.
 *
 * Fitting the panel: headers wrap between words, so "Dropped frames" costs its longer word, not
 * both; cells stay on one line unless a column says `wrap` (free text) or `truncate` (one long
 * token, cut with an ellipsis); `hideBelow` drops a low-priority column when the table's own box
 * is narrow, and its value moves under the first cell. Horizontal scrolling stays as the last
 * resort — a phone, or a value nobody planned for.
 */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  initialSort,
  dense,
  empty,
  className,
  "aria-label": ariaLabel,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  initialSort?: { key: string; dir: SortDir };
  dense?: boolean;
  /** Rendered in one full-width row when there are no rows (an `EmptyState`, a sentence). */
  empty?: ReactNode;
  className?: string;
  /** Names the table for assistive tech (`getByRole("table", {name})`) when the panel title is not enough. */
  "aria-label"?: string;
}) {
  const [sort, setSort] = useState<{ key: string; dir: SortDir } | null>(initialSort ?? null);

  // Which headers get a sort control: a column with `sortValue`, or one whose every cell is a
  // primitive. Finding the second means calling every cell, so it is worked out once per
  // `columns`/`rows`, not on every render (pages re-render on each live update).
  const sortableKeys = useMemo(
    () => new Set(columns.filter((c) => Boolean(c.sortValue) || (rows.length > 0 && rows.every((r) => isPrimitive(c.cell(r))))).map((c) => c.key)),
    [columns, rows],
  );
  const sortable = (c: Column<T>) => sortableKeys.has(c.key);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const col = columns.find((c) => c.key === sort.key);
    if (!col) return rows;
    return rows
      .map((row, i) => ({ row, i, v: valueOf(col, row) }))
      .sort((a, b) => compare(a.v, b.v, sort.dir) || a.i - b.i)
      .map((x) => x.row);
  }, [rows, sort, columns]);

  const toggle = (c: Column<T>) =>
    setSort((s) => (s?.key === c.key ? { key: c.key, dir: s.dir === "asc" ? "desc" : "asc" } : { key: c.key, dir: c.firstDir ?? (c.align === "right" ? "desc" : "asc") }));

  const cellPad = dense ? "py-1" : "py-1.5";
  const leadKey = columns.find((c) => !c.hideBelow)?.key;
  const folded = columns.filter((c) => c.hideBelow);

  return (
    <div className={cn("relative @container overflow-x-auto", className)}>
      <table className="w-full text-[14px] leading-5" aria-label={ariaLabel}>
        <thead>
          <tr className="border-b border-line text-left text-ink-2">
            {columns.map((c) => {
              const active = sort?.key === c.key;
              const ariaSort = active ? (sort!.dir === "asc" ? "ascending" : "descending") : "none";
              return (
                <th key={c.key} scope="col" style={{ width: c.width }} aria-sort={ariaSort} className={cn("pr-3 font-medium", cellPad, c.align === "right" && "text-right", c.hideBelow && HIDE_BELOW[c.hideBelow])}>
                  {sortable(c) ? (
                    <button
                      type="button"
                      onClick={() => toggle(c)}
                      className={cn("inline-flex items-center gap-1 rounded-sm hover:text-ink", active && "text-ink")}
                    >
                      {c.header}
                      {active ? sort!.dir === "asc" ? <ChevronUp className="size-3.5" aria-hidden /> : <ChevronDown className="size-3.5" aria-hidden /> : null}
                    </button>
                  ) : (
                    c.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="py-3 text-ink-2">
                {empty ?? "Nothing to show"}
              </td>
            </tr>
          ) : (
            sorted.map((row) => (
              <tr key={rowKey(row)} className="border-b border-line/60 last:border-0 hover:bg-panel-2/60">
                {columns.map((c) => {
                  const content = c.cell(row);
                  return (
                    <td
                      key={c.key}
                      title={c.truncate ? undefined : c.title?.(row)}
                      className={cn("pr-3", c.wrap ? "whitespace-normal" : "whitespace-nowrap", cellPad, c.align === "right" && "num text-right", c.hideBelow && HIDE_BELOW[c.hideBelow])}
                    >
                      {c.truncate ? (
                        <Truncate maxWidth={c.truncate} title={c.title?.(row)} className={c.align === "right" ? "ml-auto" : undefined}>
                          {content}
                        </Truncate>
                      ) : (
                        content
                      )}
                      {c.key === leadKey
                        ? folded.map((f) => (
                            <span key={f.key} className={foldClass(f.hideBelow!)}>
                              {f.header ? `${f.header}: ` : null}
                              {(f.fold ?? f.cell)(row)}
                            </span>
                          ))
                        : null}
                    </td>
                  );
                })}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
