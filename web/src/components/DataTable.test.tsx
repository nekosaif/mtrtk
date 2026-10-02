import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DataTable, HIDE_BELOW, SHOW_BELOW, type Column } from "./DataTable";

interface Row { id: string; name: string; n: number | null }

const columns: Column<Row>[] = [
  { key: "name", header: "Name", cell: (r) => r.name },
  { key: "n", header: "Count", cell: (r) => (r.n == null ? "—" : String(r.n)), sortValue: (r) => r.n, align: "right" },
  { key: "tag", header: "Tag", cell: () => <span>x</span> },
];
const rows: Row[] = [
  { id: "a", name: "bravo", n: 2 },
  { id: "b", name: "alpha", n: null },
  { id: "c", name: "charlie", n: 1 },
  { id: "d", name: "delta", n: 2 },
];
const firstCol = () => within(screen.getByRole("table")).getAllByRole("row").slice(1).map((r) => within(r).getAllByRole("cell")[0].textContent);

describe("DataTable", () => {
  it("renders headers and rows unsorted by default", () => {
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />);
    expect(screen.getAllByRole("columnheader")).toHaveLength(3);
    expect(firstCol()).toEqual(["bravo", "alpha", "charlie", "delta"]);
    for (const th of screen.getAllByRole("columnheader")) expect(th).toHaveAttribute("aria-sort", "none");
  });

  it("sorts on header click, toggles direction, exposes aria-sort and keeps nulls last", async () => {
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />);
    const count = screen.getByRole("columnheader", { name: "Count" });
    await userEvent.click(within(count).getByRole("button"));
    expect(count).toHaveAttribute("aria-sort", "descending"); // numeric columns start with the largest
    expect(firstCol()).toEqual(["bravo", "delta", "charlie", "alpha"]); // 2, 2 (stable), 1, null last
    await userEvent.click(within(count).getByRole("button"));
    expect(count).toHaveAttribute("aria-sort", "ascending");
    expect(firstCol()).toEqual(["charlie", "bravo", "delta", "alpha"]); // 1, 2, 2 (stable), null last
  });

  it("sorts a column without sortValue by the cell's primitive, text ascending first", async () => {
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />);
    const name = screen.getByRole("columnheader", { name: "Name" });
    await userEvent.click(within(name).getByRole("button"));
    expect(name).toHaveAttribute("aria-sort", "ascending");
    expect(firstCol()).toEqual(["alpha", "bravo", "charlie", "delta"]);
  });

  it("does not offer sorting on a column whose cells are not primitives", () => {
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />);
    expect(within(screen.getByRole("columnheader", { name: "Tag" })).queryByRole("button")).toBeNull();
  });

  it("is keyboard operable: the header control is a button that sorts on Enter", async () => {
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />);
    const btn = within(screen.getByRole("columnheader", { name: "Count" })).getByRole("button");
    btn.focus();
    await userEvent.keyboard("{Enter}");
    expect(screen.getByRole("columnheader", { name: "Count" })).toHaveAttribute("aria-sort", "descending");
  });

  it("applies initialSort, puts .num on numeric cells and honours dense", () => {
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} initialSort={{ key: "n", dir: "asc" }} dense />);
    expect(firstCol()).toEqual(["charlie", "bravo", "delta", "alpha"]);
    const cells = within(within(screen.getByRole("table")).getAllByRole("row")[1]).getAllByRole("cell");
    expect(cells[1].className.split(/\s+/)).toContain("num");
    expect(cells[0].className.split(/\s+/)).not.toContain("num");
    expect(cells[0].className.split(/\s+/)).toContain("py-1");
  });

  it("renders the empty slot when there are no rows", () => {
    render(<DataTable columns={columns} rows={[]} rowKey={(r) => r.id} empty={<p>Nothing here</p>} />);
    expect(screen.getByText("Nothing here")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(2); // header + the slot row
  });

  // F3 — deciding which headers can sort called every cell of every column on every render.
  it("decides sortability once per columns/rows, not on every render", () => {
    const cell = vi.fn((r: { n: number }) => r.n);
    const columns = [{ key: "n", header: "N", cell }];
    const rows = [{ n: 1 }, { n: 2 }, { n: 3 }];
    const { rerender } = render(<DataTable columns={columns} rows={rows} rowKey={(r) => String(r.n)} />);
    expect(screen.getByRole("button", { name: "N" })).toBeInTheDocument(); // primitive cells: sortable
    const first = cell.mock.calls.length;
    rerender(<DataTable columns={columns} rows={rows} rowKey={(r) => String(r.n)} className="x" />);
    expect(cell.mock.calls.length - first).toBe(rows.length); // drawing the cells, nothing more
  });

  // Task 3 (2026-10-02) — at 1440 px three tables scrolled sideways: every cell and every header
  // was nowrap, so a long user agent or a two-word header widened the table past its panel.
  describe("fitting its panel", () => {
    interface Rover { id: string; addr: string; ua: string; dropped: number; created: string }
    const rover: Rover = { id: "1", addr: "fd7a:115c:a1e0::5f:51234", ua: "NTRIP RTKLIB/2.4.3 demo5 b34k", dropped: 0, created: "2026-10-02 12:05:12 UTC" };
    const cols: Column<Rover>[] = [
      { key: "addr", header: "Address", cell: (r) => r.addr, truncate: "11rem" },
      { key: "ua", header: "Client", cell: (r) => r.ua, wrap: true },
      { key: "dropped", header: "Dropped frames", cell: (r) => String(r.dropped), sortValue: (r) => r.dropped, align: "right" },
      { key: "created", header: "Created", cell: (r) => <span>{r.created}</span>, hideBelow: "xl", title: (r) => `created ${r.created}` },
    ];
    const cellsOf = () => within(within(screen.getByRole("table")).getAllByRole("row")[1]).getAllByRole("cell");
    const classes = (el: Element) => (el.getAttribute("class") ?? "").split(/\s+/);

    it("lets a header wrap between words instead of setting the table's minimum width", () => {
      render(<DataTable columns={cols} rows={[rover]} rowKey={(r) => r.id} />);
      for (const th of screen.getAllByRole("columnheader")) expect(classes(th)).not.toContain("whitespace-nowrap");
    });

    it("truncates a one-token cell at its width with an ellipsis, keeping the whole value as the tooltip", () => {
      render(<DataTable columns={cols} rows={[rover]} rowKey={(r) => r.id} />);
      const td = cellsOf()[0];
      expect(classes(td)).toContain("whitespace-nowrap"); // never broken mid-token
      const inner = within(td).getByText(rover.addr);
      expect(classes(inner)).toContain("truncate");
      expect(inner.style.maxWidth).toBe("11rem");
      expect(inner).toHaveAttribute("title", rover.addr);
    });

    it("lets a free-text cell wrap at spaces; other cells stay on one line", () => {
      render(<DataTable columns={cols} rows={[rover]} rowKey={(r) => r.id} />);
      const [, ua, dropped] = cellsOf();
      expect(classes(ua)).toContain("whitespace-normal");
      expect(classes(ua)).not.toContain("whitespace-nowrap");
      expect(classes(dropped)).toContain("whitespace-nowrap");
    });

    it("hides a low-priority column, header and cells, when its own box is narrow (a container query)", () => {
      const { container } = render(<DataTable columns={cols} rows={[rover]} rowKey={(r) => r.id} />);
      expect(classes(container.firstElementChild!)).toContain("@container");
      // Positioned: an absolute child (an sr-only header) is clipped by the table's own scroller.
      expect(classes(container.firstElementChild!)).toContain("relative");
      const created = screen.getByRole("columnheader", { name: "Created" });
      expect(classes(created)).toContain("@max-[64rem]:hidden");
      expect(classes(cellsOf()[3])).toContain("@max-[64rem]:hidden");
      // The other columns are always shown.
      for (const name of ["Address", "Client", "Dropped frames"]) expect(classes(screen.getByRole("columnheader", { name })).join(" ")).not.toMatch(/hidden/);
    });

    // Final fix wave (2026-10-03) — a hidden column stayed hidden at phone width, where the table
    // scrolls anyway, so its value was out of reach. It now rides in the first cell, shown only
    // where its own column is hidden.
    it("folds a hidden column's value into the first cell, shown only where the column is hidden", () => {
      const withFold: Column<Rover>[] = [...cols.slice(0, 3), { ...cols[3], fold: (r) => r.created }];
      render(<DataTable columns={withFold} rows={[rover]} rowKey={(r) => r.id} />);
      const lead = cellsOf()[0];
      const folded = within(lead).getByText((_, el) => el?.tagName === "SPAN" && el.textContent === `Created: ${rover.created}`);
      expect(classes(folded)).toEqual(expect.arrayContaining(["hidden", "@max-[64rem]:block", "whitespace-normal", "wrap-anywhere"]));
      // Without a fold, the cell itself rides along.
      render(<DataTable columns={cols} rows={[rover]} rowKey={(r) => r.id} aria-label="plain" />);
      const plainLead = within(within(screen.getByRole("table", { name: "plain" })).getAllByRole("row")[1]).getAllByRole("cell")[0];
      expect(plainLead).toHaveTextContent(`Created: ${rover.created}`);
      // The truncated address keeps its own ellipsis and tooltip; the fold is beside it, not in it.
      expect(within(lead).getByText(rover.addr)).toHaveAttribute("title", rover.addr);
    });

    it("maps each priority's fold to the width that hides its column", () => {
      expect(SHOW_BELOW).toEqual({ sm: "hidden @max-[36rem]:block", md: "hidden @max-[40rem]:block", lg: "hidden @max-[44rem]:block", xl: "hidden @max-[64rem]:block" });
    });

    it("uses the column's title for a cell that is an element", () => {
      render(<DataTable columns={cols} rows={[rover]} rowKey={(r) => r.id} />);
      expect(cellsOf()[3]).toHaveAttribute("title", `created ${rover.created}`);
    });

    it("maps each priority to one container width", () => {
      expect(HIDE_BELOW).toEqual({ sm: "@max-[36rem]:hidden", md: "@max-[40rem]:hidden", lg: "@max-[44rem]:hidden", xl: "@max-[64rem]:hidden" });
    });
  });
});
