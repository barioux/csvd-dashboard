import { assertEquals, assertThrows } from "@std/assert";

import { shard } from "../../scripts/adapt_fork_check.ts";

const SETS = ["exampleitis", "hard", "third"];

Deno.test("the legs of a shard split the answer sets between them, each set once", () => {
  assertEquals(shard(SETS, "1/2"), ["exampleitis", "third"]);
  assertEquals(shard(SETS, "2/2"), ["hard"]);
  assertEquals(shard(SETS, "1/1"), SETS);
  // A set added later lands in some leg without the matrix changing.
  for (const n of [1, 2, 3, 4]) {
    const legs = Array.from(
      { length: n },
      (_, i) => shard(SETS, `${i + 1}/${n}`),
    );
    assertEquals(legs.flat().sort(), [...SETS].sort());
  }
});

Deno.test("a leg past the last set runs nothing rather than failing", () => {
  assertEquals(shard(SETS, "4/4"), []);
});

Deno.test("a shard outside 1..N, or not K/N at all, is refused", () => {
  for (const spec of ["0/2", "3/2", "1/0", "1", "", "a/b", "1/2/3", " 1/2"]) {
    assertThrows(() => shard(SETS, spec), RangeError, "--shard takes K/N");
  }
});
