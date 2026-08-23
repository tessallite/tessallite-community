import { describe, it, expect } from "vitest";
import { countDroppedJoins, partitionJoinsByEndpoints } from "./joinFilter";
import type { Join } from "../../api/types_domains/dimensions";

function join(id: string, left: string, right: string): Join {
  return {
    id,
    left_table_id: left,
    right_table_id: right,
    join_type: "inner",
    left_column_id: `${left}-col`,
    right_column_id: `${right}-col`,
    left_column_name: "k",
    right_column_name: "k",
  };
}

describe("partitionJoinsByEndpoints", () => {
  it("keeps joins whose both endpoints are on the canvas", () => {
    const nodeIds = new Set(["t1", "t2", "t3"]);
    const joins = [join("j1", "t1", "t2"), join("j2", "t2", "t3")];
    const { linked, dropped } = partitionJoinsByEndpoints(joins, nodeIds);
    expect(linked.map((j) => j.id)).toEqual(["j1", "j2"]);
    expect(dropped).toEqual([]);
  });

  it("drops a join to a hidden calendar table absent from the node set", () => {
    // t1..t3 are rendered; cal is an autocreated calendar table excluded by
    // the /tables endpoint but still referenced by join j3.
    const nodeIds = new Set(["t1", "t2", "t3"]);
    const joins = [join("j1", "t1", "t2"), join("j3", "t3", "cal")];
    const { linked, dropped } = partitionJoinsByEndpoints(joins, nodeIds);
    expect(linked.map((j) => j.id)).toEqual(["j1"]);
    expect(dropped.map((j) => j.id)).toEqual(["j3"]);
  });

  it("drops a join when either endpoint is missing", () => {
    const nodeIds = new Set(["t1"]);
    const joins = [
      join("left-missing", "x", "t1"),
      join("right-missing", "t1", "y"),
      join("both-missing", "x", "y"),
    ];
    const { linked, dropped } = partitionJoinsByEndpoints(joins, nodeIds);
    expect(linked).toEqual([]);
    expect(dropped.map((j) => j.id)).toEqual([
      "left-missing",
      "right-missing",
      "both-missing",
    ]);
  });

  it("handles an empty join list", () => {
    const { linked, dropped } = partitionJoinsByEndpoints([], new Set(["t1"]));
    expect(linked).toEqual([]);
    expect(dropped).toEqual([]);
  });

  it("counts hidden joins for the canvas warning (Bug-9395)", () => {
    const nodeIds = new Set(["t1", "t2"]);
    expect(countDroppedJoins([join("j1", "t1", "t2"), join("j2", "t2", "calendar")], nodeIds)).toBe(1);
  });
});
