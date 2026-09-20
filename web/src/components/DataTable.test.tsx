import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DataTable, type Column } from "./DataTable";

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
});
