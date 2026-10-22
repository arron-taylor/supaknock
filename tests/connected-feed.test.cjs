const assert = require("node:assert/strict");
const { Feed, compare } = require("../feedback/web/connected-feed.js");
const fixture = require("../docs/protocol/v1/fixtures/order.json");
assert.deepEqual(
  fixture.events
    .map((x, i) => ({ ...x, index: i }))
    .sort((a, b) => compare(b, a))
    .map((x) => x.index),
  fixture.descending,
);
(async () => {
  const projects = ["busy", "sparse", "empty", "failing"].map((project_id) => ({
    project_id,
    project_name: project_id,
  }));
  const events = Array.from({ length: 130 }, (_, i) => ({
    id: String(1000 - i),
    occurred_at:
      "2026-10-05T12:00:00." + String(1000 - i).padStart(6, "0") + "Z",
  }));
  const feed = new Feed(projects);
  let fail = true;
  async function fetch(p, cursor) {
    if (p.project_id === "failing" && fail) throw Error("Unavailable");
    const all =
      p.project_id === "busy"
        ? events
        : p.project_id === "sparse"
          ? [events[53], events[99]]
          : [];
    const offset = Number(cursor || 0);
    return {
      data: {
        items: all.slice(offset, offset + 7),
        coverage: { complete: true },
      },
      continuation: offset + 7 < all.length ? String(offset + 7) : null,
      expires_at: new Date(Date.now() + 100000).toISOString(),
    };
  }
  await assert.rejects(() => feed.page(fetch));
  assert.equal(feed.count, 0);
  fail = false;
  let rows = [],
    page;
  do {
    page = await feed.page(fetch);
    rows.push(...page.items);
  } while (page.more);
  assert.equal(rows.length, 132);
  assert.deepEqual(
    rows,
    [...rows].sort((a, b) => compare(b, a)),
  );
  assert.equal(
    new Set(rows.map((x) => JSON.stringify([x.project_id, x.id]))).size,
    132,
  );
  // Refill failure retains staged rows and pauses before an unknown head.
  const f = new Feed(projects.slice(0, 1));
  let attempts = 0;
  await assert.rejects(() =>
    f.page(async (p, c) => {
      if (++attempts === 2) throw Error("Down");
      return fetch(p, c);
    }),
  );
  const recovered = await f.page(fetch);
  assert.equal(recovered.items.length, 25);
  assert.equal(recovered.items[0].id, "1000");
  f.clear();
  assert.equal(f.streams.length, 0);
  console.log(
    "Connected feed: shared precision fixture, 4-app merge, refill and failure recovery passed",
  );
})();
