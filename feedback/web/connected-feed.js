/* The same order/stream rules are exercised by Swift using shared fixtures. */
(function (global) {
  const encoder = new TextEncoder();
  function bytes(a, b) {
    a = encoder.encode(a);
    b = encoder.encode(b);
    for (let i = 0; i < Math.min(a.length, b.length); i++)
      if (a[i] !== b[i]) return a[i] - b[i];
    return a.length - b.length;
  }
  function instant(value) {
    const [head, tail] = value.slice(0, -1).split(".");
    return head + "." + (tail || "").padEnd(6, "0");
  }
  function compare(a, b) {
    const x = instant(a.occurred_at),
      y = instant(b.occurred_at);
    return x === y
      ? bytes(a.id, b.id) || bytes(a.project_id, b.project_id)
      : x > y
        ? 1
        : -1;
  }
  class Feed {
    constructor(projects) {
      if (!projects.length || projects.length > 20)
        throw Error("Choose between 1 and 20 apps.");
      this.streams = projects.map((p) => ({
        project: p,
        items: [],
        cursor: null,
        loaded: false,
        done: false,
      }));
      this.count = 0;
      this.seen = new Set();
      this.pending = [];
      this.expires = Date.now() + 1800000;
    }
    needs() {
      return this.streams.filter((s) => !s.done && !s.items.length);
    }
    supply(id, result) {
      const s = this.streams.find((x) => x.project.project_id === id);
      if (!s) throw Error("Unknown app");
      s.items = result.data.items.map((x) => ({
        ...x,
        project_id: id,
        project_name: s.project.project_name,
      }));
      s.cursor = result.continuation;
      s.loaded = true;
      s.done = !s.items.length && !s.cursor;
      this.expires = Math.min(this.expires, Date.parse(result.expires_at));
      s.coverage = result.data.coverage;
    }
    take() {
      if (Date.now() >= this.expires)
        throw Error("This activity view expired. Refresh to start again.");
      if (this.needs().length) return null;
      const s = this.streams
        .filter((x) => x.items.length)
        .sort((a, b) => compare(b.items[0], a.items[0]))[0];
      if (!s) return null;
      const item = s.items.shift();
      if (!s.items.length && !s.cursor) s.done = true;
      const key = JSON.stringify([item.project_id, item.id]);
      if (this.seen.has(key))
        throw Error("The app repeated an event. Refresh this view.");
      this.seen.add(key);
      this.count++;
      return item;
    }
    async page(fetch) {
      let requests = 0;
      const started = Date.now();
      while (this.pending.length < 25 && this.count < 500) {
        const needs = this.needs();
        if (needs.length) {
          if (requests + needs.length > 40 || Date.now() - started > 30000)
            break;
          requests += needs.length;
          let index = 0;
          const failures = [];
          await Promise.all(
            Array.from({ length: Math.min(4, needs.length) }, async () => {
              while (index < needs.length) {
                const s = needs[index++];
                try {
                  this.supply(
                    s.project.project_id,
                    await fetch(s.project, s.cursor),
                  );
                } catch (error) {
                  failures.push({ project: s.project, error });
                }
              }
            }),
          );
          if (failures.length) {
            const e = Error(
              "Activity paused. One or more apps could not be read.",
            );
            e.failures = failures;
            throw e;
          }
        }
        const item = this.take();
        if (!item) break;
        this.pending.push(item);
      }
      const items = this.pending;
      this.pending = [];
      return {
        items,
        more: this.streams.some((s) => !s.done),
        capped: this.count >= 500,
      };
    }
    clear() {
      this.streams = [];
      this.pending = [];
      this.seen.clear();
    }
  }
  const api = { compare, instant, Feed };
  if (typeof module !== "undefined") module.exports = api;
  else global.ConnectedFeed = api;
})(globalThis);
