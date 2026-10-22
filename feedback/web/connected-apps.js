"use strict";
const ConnectedApps = (() => {
  let active = null;
  const path = (p) => "/projects/" + encodeURIComponent(p) + "/app-data";
  const text = (s, cls = "app-muted") => el("p", { class: cls }, s);
  const empty = (s) => el("div", { class: "app-empty" }, s);
  const skeleton = () => el("div", { class: "app-skeleton", "aria-hidden": "true" },
    ...Array.from({ length: 3 }, () => el("span")));
  const loadingView = (caption) => el("div", { class: "app-loading", role: "status" },
    text(caption), skeleton());
  function session(host, restart) {
    active?.stop();
    const controller = new AbortController();
    let stopped = false;
    const expiryTimer = setTimeout(
      () => {
        state.stop();
        host.replaceChildren(
          text("This app-data view expired."),
          button("Refresh view", restart),
        );
      },
      30 * 60 * 1000,
    );
    const state = {
      controller,
      host,
      stop() {
        if (stopped) return;
        stopped = true;
        clearTimeout(expiryTimer);
        controller.abort();
        host.replaceChildren(
          text("App data cleared while this view is inactive."),
        );
      },
      async request(url, method = "GET", body, signal = controller.signal) {
        const r = await fetch("/api/v1" + url, {
          method,
          cache: "no-store",
          signal,
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": decodeURIComponent(csrf()),
          },
          body: body === undefined ? undefined : JSON.stringify(body),
        });
        const d = await r.json();
        if (!r.ok) {
          const e = Error(d.error?.message || "The app could not be read.");
          e.code = d.error?.code;
          throw e;
        }
        if (stopped || signal.aborted || !host.isConnected)
          throw new DOMException("View closed", "AbortError");
        return d;
      },
      restart,
    };
    active = state;
    return state;
  }
  window.addEventListener("pagehide", () => active?.stop());
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) active?.stop();
    else active?.restart?.();
  });
  function subview(s, host) {
    host._appController?.abort();
    const controller = new AbortController();
    host._appController = controller;
    const signal = AbortSignal.any([s.controller.signal, controller.signal]);
    return {
      ...s,
      controller: { signal },
      request: (url, method, body, override) =>
        s.request(
          url,
          method,
          body,
          override ? AbortSignal.any([signal, override]) : signal,
        ),
    };
  }
  const actions = (...items) => el("div", { class: "actions" }, ...items);
  function error(host, e, retry) {
    if (e.name === "AbortError") return;
    if (
      ["conflict", "connection_disabled", "connection_missing"].includes(e.code)
    )
      retry = active?.restart;
    host.replaceChildren(
      el("div", { class: "app-status", role: "alert" }, e.message),
      ...(retry ? [button("Try again", retry)] : []),
    );
  }
  function date(s, zone = "UTC") {
    return s
      ? new Date(s).toLocaleString(undefined, {
          timeZone: zone,
          dateStyle: "medium",
          timeStyle: "short",
        })
      : "Not provided";
  }
  function choice(label, checked = true) {
    const input = el("input", { type: "checkbox", checked });
    return { input, node: el("label", { class: "app-choice" }, input, label) };
  }
  function external(label, url) {
    return el(
      "a",
      { href: url, target: "_blank", rel: "noopener noreferrer" },
      label + " ↗",
    );
  }
  async function prefs(s) {
    return await s.request("/admin/connected-apps/preferences");
  }
  function dates() {
    const from = el("input", {
        type: "date",
        value: new Date(Date.now() - 7 * 86400000).toISOString().slice(0, 10),
        "aria-label": "From date (UTC)",
      }),
      until = el("input", {
        type: "date",
        value: new Date(Date.now() + 86400000).toISOString().slice(0, 10),
        "aria-label": "Until date (UTC, exclusive)",
      });
    return {
      node: el(
        "div",
        { class: "app-toolbar" },
        el("label", {}, "From (UTC)", from),
        el("label", {}, "Until (UTC, exclusive)", until),
      ),
      query() {
        if (!from.value || !until.value || from.value >= until.value)
          throw Error("Choose a start date before the end date.");
        return {
          from: from.value + "T00:00:00Z",
          until: until.value + "T00:00:00Z",
        };
      },
    };
  }
  function settings(project) {
    const content = el("div", { class: "app-data" }),
      section = panel("App connection", content);
    let loaded = false;
    section._activate = () => {
      if (!loaded) {
        loaded = true;
        content.replaceChildren(loadingView("Loading app connection…"));
        load().catch((e) => {
          loaded = false;
          error(content, e, () => section._activate());
        });
      }
    };
    async function load() {
      const config = await api(path(project.id) + "/connection");
      let test = null;
      const url = el("input", {
          type: "url",
          value: config.base_url || "",
          placeholder: "https://your-app.example/suggestion-box",
          required: true,
        }),
        token = el("input", {
          type: "password",
          autocomplete: "new-password",
          placeholder: config.has_token
            ? "Saved securely · enter only to replace"
            : "Dedicated read-only app token",
          maxlength: 4096,
        });
      const status = el("div", { role: "status" }),
        review = el("div");
      content.replaceChildren(
        text(
          "Connect this project to its app for on-demand summaries, users and activity. Your app keeps its data.",
        ),
        el(
          "div",
          { class: "app-setup" },
          el("label", {}, "App protocol address", url),
          el("label", {}, "Read-only app token", token),
          text(
            "Saving leaves this connection off until you test and explicitly enable it. Tokens are never shown again.",
          ),
          actions(
            button("Save connection", async () => {
              await api(path(project.id) + "/connection", "PUT", {
                version: config.version,
                base_url: url.value,
                token: token.value || null,
              });
              token.value = "";
              await load();
              notify("Connection saved. Test it before enabling.");
            }),
            button("Test saved connection", async () => {
              if (url.value !== (config.base_url || "") || token.value)
                throw Error("Save your changes before testing.");
              test = await api(path(project.id) + "/connection/test", "POST", {
                version: config.version,
              });
              const namespace = choice(
                "These app user IDs use the same namespace as this project’s existing feedback and chat.",
                config.support_linked,
              );
              const origins = test.manifest.link_origins.map((x) => ({
                origin: x,
                ...choice(
                  "Allow links to " + x,
                  config.link_origins.includes(x),
                ),
              }));
              review.replaceChildren(
                text("Connection verified · " + test.manifest.app_instance_id),
                text(
                  "Available: " +
                    Object.entries(test.manifest.capabilities)
                      .filter(([, v]) => v)
                      .map(([k]) => k.replaceAll("_", " "))
                      .join(", "),
                ),
                ...origins.map((x) => x.node),
                namespace.node,
                text(
                  "Leave the namespace unchecked if unsure. User browsing will still work; local support links will remain off.",
                ),
                button(
                  "Enable this connection",
                  async () => {
                    await api(path(project.id) + "/connection/enable", "POST", {
                      version: config.version,
                      receipt: test.receipt,
                      link_origins: origins
                        .filter((x) => x.input.checked)
                        .map((x) => x.origin),
                      support_linked: namespace.input.checked,
                    });
                    await load();
                    notify("App connected");
                  },
                  "",
                ),
              );
            }),
          ),
          status,
          review,
        ),
      );
      status.append(
        text(config.enabled ? "Connected and enabled" : "Connection is off"),
        ...(config.app_instance_id
          ? [text("Bound app: " + config.app_instance_id)]
          : []),
      );
      if (config.enabled)
        content.append(
          actions(
            link(
              "Open app data →",
              "/admin/projects/" + project.id + "/app-data",
              "button",
            ),
            button("Disable connection", async () => {
              await api(path(project.id) + "/connection/disable", "POST", {
                version: config.version,
              });
              await load();
              notify("Connection disabled");
            }),
          ),
        );
      if (config.has_token)
        content.append(
          el(
            "details",
            {},
            el("summary", {}, "Disconnect this app"),
            text(
              "Removes the saved address and token. The app identity stays bound to this project; feedback and chat remain available.",
            ),
            button("Remove connection", async () => {
              await api(path(project.id) + "/connection/remove", "POST", {
                version: config.version,
              });
              await load();
              notify("Connection removed");
            }),
          ),
        );
    }
    return section;
  }
  async function overview(host) {
    const s = session(host, () => overview(host));
    host.classList.add("app-data");
    const initialLoading = loadingView("Loading connected apps…");
    host.replaceChildren(
      el("h2", {}, "Your connected apps"),
      text("Accounts across your products, fetched when you open this view."),
      initialLoading,
    );
    try {
      const allConfigs = (
          await s.request("/admin/connected-apps")
        ).items.filter((x) => x.enabled),
        configs = allConfigs.slice(0, 20);
      initialLoading.remove();
      if (allConfigs.length > 20)
        host.append(
          text(
            "Showing the first 20 connected apps. Open a project’s App data for other apps.",
          ),
        );
      if (!configs.length) {
        host.append(
          empty("Connect an app in Project settings → App connection."),
        );
        return;
      }
      const preference = await prefs(s);
      let summaryId = 0,
        summaryController = new AbortController();
      const range = dates(),
        cards = el("div", { class: "app-cards" }),
        totals = el("div", { class: "app-status", role: "status" });
      host.append(
        range.node,
        actions(
          button("Refresh summaries", load),
          link("App activity →", "/admin/app-activity"),
        ),
        totals,
        cards,
      );
      async function load() {
        const query = range.query();
        const id = ++summaryId;
        summaryController.abort();
        summaryController = new AbortController();
        const signal = AbortSignal.any([
          s.controller.signal,
          summaryController.signal,
        ]);
        cards.replaceChildren();
        totals.textContent = "Loading account summaries…";
        const results = [];
        let index = 0;
        await Promise.all(
          Array.from({ length: Math.min(4, configs.length) }, async () => {
            while (index < configs.length) {
              if (signal.aborted || id !== summaryId) return;
              const p = configs[index++],
                card = el(
                  "article",
                  { class: "app-card", "aria-busy": "true" },
                  link(
                    p.project_name,
                    "/admin/projects/" + p.project_id + "/app-data",
                  ),
                  skeleton(),
                );
              cards.append(card);
              try {
                const r = await s.request(
                  path(p.project_id) + "/summary/query",
                  "POST",
                  { version: p.version, query },
                  signal,
                );
                results.push(r.data);
                card.replaceChildren(
                  link(
                    p.project_name,
                    "/admin/projects/" + p.project_id + "/app-data",
                  ),
                  el(
                    "strong",
                    { class: "app-number" },
                    r.data.total_accounts.count ?? "Unavailable",
                  ),
                  text("Current accounts"),
                  text(
                    (r.data.new_accounts.count ?? "Unavailable") +
                      " new accounts · " +
                      (r.data.new_accounts.basis === "current_accounts"
                        ? "current records; excludes deleted accounts"
                        : "retained signup events"),
                  ),
                  text(
                    "Fetched " + date(r.data.observed_at, preference.timezone),
                  ),
                );
              } catch (e) {
                if (e.name !== "AbortError")
                  card.replaceChildren(
                    link(
                      p.project_name,
                      "/admin/projects/" + p.project_id + "/app-data",
                    ),
                    text(e.message),
                  );
              } finally {
                card.setAttribute("aria-busy", "false");
              }
            }
          }),
        );
        if (id !== summaryId || s.controller.signal.aborted) return;
        const available = results.filter(
            (x) => x.total_accounts.count !== null,
          ),
          groups = {};
        for (const r of results) {
          if (r.new_accounts.count !== null)
            groups[r.new_accounts.basis] =
              (groups[r.new_accounts.basis] || 0n) +
              BigInt(r.new_accounts.count);
        }
        totals.textContent =
          `${available.length}/${configs.length} apps reporting current totals: ${available.length ? available.reduce((a, x) => a + BigInt(x.total_accounts.count), 0n) : "unavailable"} accounts. ` +
          Object.entries(groups)
            .map(
              ([basis, n]) =>
                `${n} new accounts (${basis === "current_accounts" ? "current records" : "retained signup events"}).`,
            )
            .join(" ");
      }
      await load();
    } catch (e) {
      error(host, e, () => overview(host));
    }
  }
  async function workspace(project) {
    if (!requireAdmin()) return;
    root.replaceChildren(link("← Projects", "/admin#projects", "crumb"));
    const host = el("section", { class: "app-data" });
    root.append(host);
    const s = session(host, () => workspace(project));
    host.append(loadingView("Opening app data…"));
    try {
      const config = await s.request(path(project) + "/connection"),
        preference = await prefs(s);
      host.replaceChildren(
        hero(
          "APP DATA",
          config.project_name,
          "A live view of your app. Your app remains the source of truth.",
        ),
        actions(
          link(
            "Connection settings",
            "/admin/projects/" + config.project_id + "#app-connection",
          ),
        ),
      );
      if (!config.enabled) {
        host.append(
          empty("Connect and enable this app in its project settings."),
        );
        return;
      }
      const content = el("div"),
        tabs = el("nav", { class: "app-tabs", "aria-label": "App data views" });
      host.append(tabs, content);
      let viewGeneration = 0;
      const navigate = async (kind) => {
        content._appController?.abort();
        viewGeneration++;
        const generation = viewGeneration;
        [...tabs.children].forEach((x) =>
          x.setAttribute("aria-pressed", String(x.dataset.view === kind)),
        );
        content.replaceChildren();
        if (kind === "users")
          await users(
            content,
            s,
            config,
            preference.timezone,
            () => generation === viewGeneration,
          );
        else await timeline(content, s, [config], preference.timezone);
      };
      for (const kind of ["users", "activity"])
        tabs.append(
          el(
            "button",
            {
              type: "button",
              "data-view": kind,
              onclick: () => navigate(kind),
            },
            kind === "users" ? "Users" : "Activity",
          ),
        );
      await navigate("users");
    } catch (e) {
      error(host, e, () => workspace(project));
    }
  }
  async function users(host, s, config, zone, valid = () => true) {
    s = subview(
      { ...s, controller: active.controller, request: active.request },
      host,
    );
    const search = el("input", {
        type: "search",
        placeholder: "Name or email",
        maxlength: 200,
        "aria-label": "Search app users",
      }),
      range = dates(),
      filter = choice("Filter by signup dates", false),
      list = el("div", { class: "app-list" }),
      status = el("div", { class: "app-results-status", role: "status" }),
      more = button("Load more", () => load(false));
    let cursor = null,
      count = 0,
      query = null,
      requestId = 0,
      seen = new Set(),
      timer;
    range.node.hidden = true;
    filter.input.onchange = () => {
      range.node.hidden = !filter.input.checked;
    };
    host.append(
      el(
        "div",
        { class: "app-toolbar" },
        el("label", {}, "Find a user", search),
        button("Search", () => load(true)),
      ),
      filter.node,
      range.node,
      text(
        "Live user directory. Names, search matches and retained accounts may change while you browse. Refresh to start again.",
      ),
      status,
      list,
      actions(more),
    );
    search.oninput = () => {
      clearTimeout(timer);
      const id = ++requestId;
      timer = setTimeout(() => {
        if (id === requestId && valid() && host.isConnected)
          load(true).catch((e) => error(status, e, () => load(true)));
      }, 350);
    };
    let pendingRead;
    async function load(reset) {
      pendingRead?.abort();
      const id = ++requestId;
      let nextQuery;
      try {
        nextQuery = reset ? {
          limit: 50,
          query: search.value,
          joined: filter.input.checked ? range.query() : null,
        } : query;
      } catch (e) {
        more.disabled = false;
        list.setAttribute("aria-busy", "false");
        error(status, e, () => load(reset));
        return;
      }
      pendingRead = new AbortController();
      const signal = AbortSignal.any([s.controller.signal, pendingRead.signal]);
      more.disabled = true;
      if (reset) {
        cursor = null;
        count = 0;
        seen.clear();
        list.replaceChildren();
        query = nextQuery;
      }
      status.textContent = "Loading users…";
      const placeholder = reset ? skeleton() : null;
      if (placeholder) list.append(placeholder);
      list.setAttribute("aria-busy", "true");
      try {
        const r = await s.request(
          path(config.project_id) + "/users/query",
          "POST",
          { version: config.version, query, continuation: cursor },
          signal,
        );
        if (id !== requestId || !valid() || !host.contains(search)) return;
        placeholder?.remove();
        cursor = r.continuation;
        for (const u of r.data.items) {
          if (seen.has(u.id)) continue;
          seen.add(u.id);
          count++;
          list.append(
            el(
              "button",
              {
                type: "button",
                class: "app-user",
                onclick: () =>
                  profile(host, s, config, u.id, zone, () =>
                    users(host, s, config, zone, valid),
                  ),
              },
              el(
                "span",
                { class: "app-avatar", "aria-hidden": "true" },
                (u.display_name || "?").slice(0, 1),
              ),
              el(
                "span",
                { class: "app-user-copy" },
                el("strong", {}, u.display_name || "Unnamed account"),
                el(
                  "span",
                  { class: "app-muted" },
                  u.email || "Email not provided",
                ),
              ),
              el("time", { class: "app-muted", datetime: u.joined_at }, date(u.joined_at, zone)),
            ),
          );
        }
        status.textContent = `${count} users shown · fetched ${date(r.data.observed_at, zone)}`;
        if (!count) list.append(empty("No users matched this search."));
        more.hidden = !cursor || count >= 500;
        if (count >= 500)
          status.append(
            text(
              "Showing 500 users. Narrow your search or date range to continue.",
            ),
          );
      } catch (e) {
        if (id === requestId && valid()) error(status, e, () => load(reset));
      } finally {
        placeholder?.remove();
        if (id === requestId) {
          more.disabled = false;
          list.setAttribute("aria-busy", "false");
        }
      }
    }
    await load(true);
  }
  async function profile(host, s, config, id, zone, back) {
    s = subview(
      { ...s, controller: active.controller, request: active.request },
      host,
    );
    host.replaceChildren(
      button("← Back to users", back),
      loadingView("Loading profile…"),
    );
    try {
      const r = await s.request(
          path(config.project_id) + "/profile/query",
          "POST",
          { version: config.version, query: { user_id: id } },
        ),
        u = r.data.user;
      const details = el("dl", { class: "app-details" });
      for (const [key, value] of [
        ["Account ID", u.id],
        ["Email", u.email || "Not provided"],
        ["Phone number", u.phone_number || "Not provided"],
        ["Joined", date(u.joined_at, zone)],
        ["Last active", date(u.last_active_at, zone)],
        ["Status", u.status],
        ["Plan", u.plan || "Not provided"],
      ])
        details.append(el("dt", {}, key), el("dd", {}, value));
      for (const d of r.data.details)
        details.append(
          el("dt", {}, d.label),
          el(
            "dd",
            {},
            d.value === null
              ? "Not provided"
              : d.type === "link"
                ? external("Open in app", d.value)
                : d.type === "timestamp"
                  ? date(d.value, zone)
                  : String(d.value),
          ),
        );
      host.replaceChildren(
        button("← Back to users", back),
        el("h2", {}, u.display_name || "Unnamed account"),
        details,
        ...(r.data.url ? [external("Open in app", r.data.url)] : []),
      );
      const history = el("div");
      host.append(el("h3", {}, "Related support"), history);
      let offset = 0;
      async function related() {
        const data = await s.request(
          path(config.project_id) + "/related/query",
          "POST",
          { version: config.version, user_id: id, offset },
        );
        if (!data.linked) {
          history.replaceChildren(
            text("Support identity linking is off for this connection."),
          );
          return;
        }
        if (!data.requests.length && !data.conversations.length && !offset)
          history.append(text("No linked support history."));
        for (const x of data.requests)
          history.append(text(""), link(x.title, "/requests/" + x.id));
        for (const x of data.conversations)
          history.append(
            text(""),
            link(
              "Conversation · " + date(x.created_at, zone),
              "/admin/chat?conversation=" + encodeURIComponent(x.id),
            ),
          );
        if (data.has_more) {
          const b = button("More support history", async () => {
            b.remove();
            offset += 25;
            await related();
          });
          history.append(actions(b));
        }
      }
      await related();
      if (r.capabilities.user_activity)
        host.append(
          actions(
            button("View user activity", async () => {
              host.replaceChildren(
                button("← Back to profile", () =>
                  profile(host, s, config, id, zone, back),
                ),
              );
              const feed = el("div");
              host.append(feed);
              await timeline(feed, s, [config], zone, id);
            }),
          ),
        );
    } catch (e) {
      if (e.name === "AbortError") return;
      host.replaceChildren(button("← Back to users", back));
      const message = el("div");
      host.append(message);
      error(message, e);
    }
  }
  async function timeline(host, s, configs, zone, userId = null) {
    s = subview(
      { ...s, controller: active.controller, request: active.request },
      host,
    );
    const range = dates(),
      list = el("div"),
      status = el("div", { class: "app-results-status", role: "status" }),
      more = button("Load more activity", () => load()),
      choices = configs.map((p, i) => ({
        p,
        ...choice(p.project_name, i < 20),
      }));
    let lastDay = null,
      feed = null,
      query = null,
      selected = [],
      loading = false,
      loadId = 0,
      readController = new AbortController();
    host.append(
      range.node,
      ...(configs.length > 1
        ? [
            el(
              "details",
              {},
              el("summary", {}, "Choose apps (up to 20)"),
              ...choices.map((x) => x.node),
            ),
          ]
        : []),
      actions(button("Refresh activity", reset)),
      text(
        "Chronological activity from the selected apps. Refresh starts a new view; this is not a complete audit trail.",
      ),
      status,
      list,
      actions(more),
    );
    async function reset() {
      const interval = range.query();
      readController.abort();
      readController = new AbortController();
      loadId++;
      loading = false;
      feed?.clear();
      lastDay = null;
      list.replaceChildren();
      selected = choices.filter((x) => x.input.checked).map((x) => x.p);
      if (!selected.length || selected.length > 20) {
        status.textContent = "Choose between 1 and 20 apps, then refresh.";
        more.hidden = true;
        return;
      }
      query = { ...interval, limit: 50, user_id: userId };
      feed = new ConnectedFeed.Feed(selected);
      await load();
    }
    async function load() {
      if (loading) return;
      const id = loadId;
      loading = true;
      more.disabled = true;
      status.textContent = "Loading activity…";
      const placeholder = !feed.count ? skeleton() : null;
      if (placeholder) list.append(placeholder);
      list.setAttribute("aria-busy", "true");
      try {
        const result = await feed.page((p, c) =>
          s.request(
            path(p.project_id) + "/activity/query",
            "POST",
            { version: p.version, query, continuation: c },
            readController.signal,
          ),
        );
        if (id !== loadId) return;
        placeholder?.remove();
        for (const x of result.items) {
          const day = new Date(x.occurred_at).toLocaleDateString(undefined, {
            timeZone: zone,
            dateStyle: "full",
          });
          if (day !== lastDay) {
            list.append(el("h2", {}, day));
            lastDay = day;
          }
          const article = el(
            "article",
            { class: "app-event" },
            el("span", { class: "app-chip" }, x.project_name),
            el("time", { class: "app-muted", datetime: x.occurred_at }, date(x.occurred_at, zone)),
            el("h3", {}, x.title),
            ...(x.description ? [text(x.description, "")] : []),
            ...(x.url ? [external("Open in app", x.url)] : []),
          );
          if (x.user_id)
            article.append(
              actions(
                button("View user", () => {
                  feed.clear();
                  return profile(
                    host,
                    s,
                    configs.find((p) => p.project_id === x.project_id),
                    x.user_id,
                    zone,
                    () => {
                      host.replaceChildren();
                      return timeline(host, s, configs, zone, userId);
                    },
                  );
                }),
              ),
            );
          list.append(article);
        }
        if (!feed.count) list.append(empty("No activity in this interval."));
        const gaps = feed.streams
          .filter((x) => x.coverage && !x.coverage.complete)
          .map((x) => x.project.project_name);
        status.textContent =
          `${feed.count} events shown · ${selected.length} apps · times in ${zone}.` +
          (gaps.length
            ? " Incomplete retained history: " + gaps.join(", ") + "."
            : "") +
          (result.capped
            ? " Showing 500 events. Choose a narrower range to continue."
            : "");
        more.hidden = !result.more || result.capped;
      } catch (e) {
        if (id !== loadId || s.controller.signal.aborted) return;
        if (e.name !== "AbortError") {
          status.replaceChildren(
            text(e.message),
            button("Retry paused feed", load),
          );
          for (const failure of e.failures || []) {
            status.append(
              text(failure.project.project_name + ": " + failure.error.message),
              button(
                "Restart without " + failure.project.project_name,
                async () => {
                  choices.find(
                    (x) => x.p.project_id === failure.project.project_id,
                  ).input.checked = false;
                  await reset();
                },
              ),
            );
          }
        }
      } finally {
        placeholder?.remove();
        if (id === loadId) {
          loading = false;
          more.disabled = false;
          list.setAttribute("aria-busy", "false");
        }
      }
    }
    await reset();
  }
  async function activity() {
    if (!requireAdmin()) return;
    root.replaceChildren();
    const host = el("section", { class: "app-data" });
    root.append(host);
    const s = session(host, activity);
    host.append(loadingView("Loading app activity…"));
    try {
      const configs = (await s.request("/admin/connected-apps")).items.filter(
          (x) => x.enabled,
        ),
        preference = await prefs(s);
      host.replaceChildren(
        hero(
          "ACROSS YOUR APPS",
          "App activity",
          "See what happened across your products, in one chronological view.",
        ),
      );
      if (!configs.length) {
        host.append(
          empty("Connect an app in Project settings to see its activity."),
        );
        return;
      }
      const zone = el("input", {
        value: preference.timezone,
        "aria-label": "Display timezone",
      });
      host.append(
        el(
          "details",
          {},
          el("summary", {}, "Display timezone · " + preference.timezone),
          el(
            "div",
            { class: "app-toolbar" },
            el("label", {}, "IANA timezone", zone),
            button("Save timezone", async () => {
              await s.request("/admin/connected-apps/preferences", "PUT", {
                version: preference.version,
                timezone: zone.value,
              });
              await activity();
            }),
          ),
        ),
      );
      await timeline(host, s, configs, preference.timezone);
    } catch (e) {
      error(host, e, activity);
    }
  }
  return { settings, overview, workspace, activity };
})();
