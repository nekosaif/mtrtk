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
 */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  initialSort,
  dense,
  empty,
  className,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  initialSort?: { key: string; dir: SortDir };
  dense?: boolean;
  /** Rendered in one full-width row when there are no rows (an `EmptyState`, a sentence). */
  empty?: ReactNode;
  className?: string;
}) {
  const [sort, setSort] = useState<{ key: string; dir: SortDir } | null>(initialSort ?? null);

  const sortable = (c: Column<T>) => Boolean(c.sortValue) || (rows.length > 0 && rows.every((r) => isPrimitive(c.cell(r))));

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

  return (
    <div className={cn("overflow-x-auto", className)}>
      <table className="w-full text-[14px] leading-5">
        <thead>
          <tr className="border-b border-line text-left text-ink-2">
            {columns.map((c) => {
              const active = sort?.key === c.key;
              const ariaSort = active ? (sort!.dir === "asc" ? "ascending" : "descending") : "none";
              return (
                <th key={c.key} scope="col" style={{ width: c.width }} aria-sort={ariaSort} className={cn("pr-3 font-medium whitespace-nowrap", cellPad, c.align === "right" && "text-right")}>
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
                {columns.map((c) => (
                  <td key={c.key} className={cn("pr-3", cellPad, c.align === "right" && "num text-right")}>
                    {c.cell(row)}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
