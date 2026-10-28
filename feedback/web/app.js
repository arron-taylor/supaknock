"use strict";
const root = document.getElementById("app");
let me = null;
let noticeTimer;
const labels = {
  pending: "Awaiting approval",
  rejected: "Rejected",
  accepted: "Accepted",
  under_consideration: "Under consideration",
  planned: "Planned",
  in_progress: "In progress",
  shipped: "Shipped",
};
const statuses = ["under_consideration", "planned", "in_progress", "shipped"];
const events = [
  "request.created",
  "request.updated",
  "request.resubmitted",
  "request.accepted",
  "request.rejected",
  "request.status_changed",
  "request.deleted",
  "chat.message_created",
];
function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "value") n.value = v;
    else if (k === "checked") n.checked = v;
    else if (v !== false && v !== null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat())
    if (c !== null && c !== undefined)
      n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}
function link(text, href, cls = "") {
  return el("a", { href, class: cls }, text);
}
function notify(text, kind = "success") {
  const n = document.getElementById("notice");
  n.classList.toggle("is-error", kind === "error");
  n.replaceChildren(SuggestionShell.icon(kind === "error" ? "close" : "check"), el("span", {}, text), el("button", {type:"button", class:"notice-dismiss", "aria-label":"Dismiss notification", onclick:()=>{n.style.display="none";clearTimeout(noticeTimer);}}, SuggestionShell.icon("close")));
  n.style.display = "block";
  clearTimeout(noticeTimer);
  if (kind !== "error") noticeTimer = setTimeout(() => (n.style.display = "none"), 5000);
}
function csrf() {
  return (
    document.cookie
      .split("; ")
      .find((x) => x.startsWith("feedback_csrf="))
      ?.split("=")
      .slice(1)
      .join("=") || ""
  );
}
async function api(path, method = "GET", data) {
  const options = { method, headers: {} };
  if (method !== "GET") {
    options.headers["X-CSRF-Token"] = decodeURIComponent(csrf());
    if (data !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(data);
    }
  }
  const r = await fetch("/api/v1" + path, options);
  if (r.status === 204) return null;
  const body = await r.json();
  if (!r.ok) {
    const err = body.error || {};
    const error = new Error(
      (err.message || "The operation failed") +
        (err.fields
          ?.map((x) => " · " + x.field.replace("body.", "") + ": " + x.message)
          .join("") || ""),
    );
    error.fields = Array.isArray(err.fields) ? err.fields : [];
    error.summary = err.message;
    throw error;
  }
  return body;
}
function field(text, name, type = "text", value = "", extra = {}) {
  const input = el(type === "textarea" ? "textarea" : "input", {
    name,
    type: type === "textarea" ? null : type,
    value,
    ...extra,
  });
  return el("label", {}, text, input);
}
function select(text, name, options, value) {
  return el(
    "label",
    {},
    text,
    el(
      "select",
      { name },
      options.map(([v, t]) =>
        el("option", { value: v, selected: v === value }, t),
      ),
    ),
  );
}
function checkbox(text, name, checked = false, value = "true") {
  return el(
    "label",
    { class: "check" },
    el("input", { type: "checkbox", name, checked, value }),
    text,
  );
}
let formSequence = 0;
function form(fields, caption, submit) {
  const error = el("p", { class: "error", role: "alert", tabindex: "-1" });
  const button = el("button", { type: "submit" }, caption);
  const f = el("form", {}, fields, error, button);
  const prefix = "form-" + ++formSequence;
  const clearFieldError = input => {
    const id=input.dataset.errorId;
    if (!id) return;
    for(const message of f.querySelectorAll(".field-error"))if(message.id===id)message.remove();
    const remaining=(input.getAttribute("aria-describedby")||"").split(" ").filter(value=>value&&value!==id);
    if(remaining.length)input.setAttribute("aria-describedby",remaining.join(" "));else input.removeAttribute("aria-describedby");
    input.removeAttribute("aria-invalid");delete input.dataset.errorId;
  };
  f.addEventListener("input", event => clearFieldError(event.target));
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (f.dataset.busy) return;
    f.dataset.busy = "true";
    error.textContent = "";
    for (const input of f.querySelectorAll("[data-error-id]")) clearFieldError(input);
    const originalButtons = [...f.querySelectorAll("button")].map(control=>[control,control.disabled]);
    button.textContent = /sign in/i.test(caption) ? "Signing in…" : /create/i.test(caption) ? "Creating…" : /submit/i.test(caption) ? "Submitting…" : "Saving…";
    button.setAttribute("aria-busy", "true");
    f.setAttribute("aria-busy", "true");
    for (const [control] of originalButtons) control.disabled = true;
    try {
      await submit(new FormData(f), f);
    } catch (err) {
      error.textContent = err.summary || err.message;
      let firstInvalid = null;
      for (const [index, issue] of (err.fields || []).entries()) {
        const input = [...f.elements].find(control=>control.name===String(issue.field).replace(/^body\./, ""));
        if (!input || input.dataset.errorId) continue;
        const id=prefix+"-error-"+index;
        input.setAttribute("aria-invalid", "true");input.dataset.errorId=id;
        input.setAttribute("aria-describedby", [input.getAttribute("aria-describedby"),id].filter(Boolean).join(" "));
        input.insertAdjacentElement("afterend",el("span",{class:"field-error",id},issue.message));
        firstInvalid ||= input;
      }
      (firstInvalid || error).focus();
    } finally {
      delete f.dataset.busy;
      f.removeAttribute("aria-busy");
      button.removeAttribute("aria-busy");
      button.textContent = caption;
      for (const [control, disabled] of originalButtons) control.disabled = disabled;
    }
  });
  return f;
}
function button(text, action, cls = "secondary") {
  const b = el(
    "button",
    {
      type: "button",
      class: cls,
      onclick: async () => {
        if (b.disabled) return;
        b.disabled = true;
        b.setAttribute("aria-busy", "true");
        try {
          await action();
        } catch (err) {
          notify(err.message, "error");
        } finally {
          b.removeAttribute("aria-busy");
          b.disabled = false;
        }
      },
    },
    text,
  );
  return b;
}
function hero(eyebrow, title, description) {
  document.title = title.replace(/[.!]$/, "") + " · SupaKnock";
  return el(
    "div",
    { class: "hero" },
    el("p", { class: "eyebrow" }, eyebrow),
    el("h1", {}, title),
    description ? el("p", { class: "lead" }, description) : null,
  );
}
function panel(title, ...children) {
  return el("section", { class: "panel" }, el("h2", {}, title), children);
}
function badge(state) {
  return el("span", { class: "pill " + state }, labels[state] || state);
}
function empty(text) {
  return el("div", { class: "empty" }, SuggestionShell.icon("inbox"), el("span", {}, text));
}
function contentFields(title = "", description = "") {
  return [
    field("Title", "title", "text", title, { required: true, maxlength: 200 }),
    field("Description", "description", "textarea", description, {
      maxlength: 20000,
    }),
  ];
}
function resourceFields(item = {}, identifier = true) {
  return [
    identifier
      ? field(
          "Public identifier",
          "identifier",
          "text",
          item.identifier || "",
          { required: true, pattern: "[a-z][a-z0-9-]{0,59}", maxlength: 60 },
        )
      : null,
    field("Name", "name", "text", item.name || "", {
      required: true,
      maxlength: 100,
    }),
    field("Description", "description", "textarea", item.description || "", {
      maxlength: 20000,
    }),
  ].filter(Boolean);
}
function boardFields(item = {}, identifier = true) {
  return [
    ...resourceFields(item, identifier),
    select("Visibility", "visibility", [["public", "Public"], ["private", "Private"]], item.visibility || "public"),
    el("p", {class:"hint"}, "Private boards stay out of public browsing. Signed-in submitters can use a direct link and see only their own requests. API and chat intake stay available."),
  ];
}
function accountFields(display = true) {
  return [
    field("Email", "email", "email", "", {
      required: true,
      autocomplete: "email",
      maxlength: 254,
    }),
    field("Password", "password", "password", "", {
      required: true,
      minlength: 12,
      maxlength: 128,
      autocomplete: "new-password",
    }),
    display
      ? field("Display name (optional)", "display_name", "text", "", {
          maxlength: 100,
          autocomplete: "nickname",
        })
      : null,
  ].filter(Boolean);
}
function requestCard(r) {
  const state = r.moderation && r.moderation !== "accepted" ? r.moderation : r.status;
  const date = new Date(r.created_at).toLocaleDateString(undefined, {month:"short", day:"numeric"});
  const mobileBadge = badge(state); mobileBadge.classList.add("mobile-status");
  return el("article", {class:"request-row"},
    el("a", {href:"/requests/" + r.id, class:"vote-count", "aria-label":r.vote_count + " votes · " + r.title}, SuggestionShell.icon("chevron"), el("strong", {}, r.vote_count)),
    el("div", {class:"request-copy"},
      link(r.title, "/requests/" + r.id, "request-title"),
      el("p", {}, r.description || "An idea from the community."),
      el("div", {class:"meta"}, el("span", {}, r.project_name + " / " + r.board_name), el("span", {class:"meta-dot", "aria-hidden":"true"}), el("span", {}, date), mobileBadge)),
    badge(state));
}

function pager(page, load) {
  return el(
    "div",
    { class: "pager" },
    el(
      "span",
      { class: "hint" },
      "Showing " + (page.offset + 1) + "–" + (page.offset + page.items.length),
    ),
    page.offset
      ? button("Previous", () => load(Math.max(0, page.offset - page.limit)))
      : null,
    page.has_more ? button("Next", () => load(page.offset + page.limit)) : null,
  );
}
async function paged(target, path, render, offset = 0, emptyText = "Nothing here yet.") {
  const generation = String(Number(target.dataset.generation || 0) + 1);
  target.dataset.generation = generation;
  target.setAttribute("aria-busy", "true");
  target.replaceChildren(SuggestionShell.skeleton(target.classList.contains("grid") ? "cards" : "rows"));
  const separator = path.includes("?") ? "&" : "?";
  let page;
  try {
    page = await api(path + separator + "offset=" + offset);
  } catch (err) {
    if (target.dataset.generation !== generation) return;
    target.setAttribute("aria-busy", "false");
    target.replaceChildren(el("div", {class:"empty"},
      el("p", {class:"error", role:"alert"}, err.message),
      button("Try again", () => paged(target, path, render, offset, emptyText))));
    return;
  }
  if (target.dataset.generation !== generation) return;
  target.setAttribute("aria-busy", "false");
  target.replaceChildren(
    ...(page.items.length
      ? [render(page.items), pager(page, (o) => paged(target, path, render, o, emptyText))]
      : [empty(emptyText)]
    ).flat(),
  );
}
function showSecret(parent, text, secret) {
  const area = el(
    "div",
    { class: "secret" },
    el("p", {}, text),
    el("pre", {}, secret),
    button("Copy", async () => {
      await navigator.clipboard.writeText(secret);
      notify("Copied");
    }),
    button("Dismiss", () => area.remove()),
  );
  parent.append(area);
}
function requireLogin() {
  if (!me) {
    location.href = "/login?return=" + encodeURIComponent(location.pathname + location.search + location.hash);
    return false;
  }
  return true;
}
function requireAdmin() {
  if (!requireLogin()) return false;
  if (me.role !== "admin") {
    root.replaceChildren(
      hero(
        "Access restricted",
        "Administrator access required",
        "Sign in with an administrator account to manage this installation.",
      ),
    );
    return false;
  }
  return true;
}
function renderNav() {
  SuggestionShell.mount(me, async () => {
    await api("/auth/logout", "POST");
    location.href = "/";
  });
}
function projectIcon() {
  return el("span", {class:"project-icon", "aria-hidden":"true"}, SuggestionShell.icon("box"));
}
function settingTabs(panels) {
  const nav = el("div", {class:"settings-tabs", role:"tablist", "aria-label":"Project settings"});
  const keys = panels.map(p => p.querySelector("h2").textContent.toLowerCase().replace(/\s+/g, "-"));
  const views = panels.map((p, i) => {
    const view = el("div", {class:"settings-view", id:"settings-panel-"+i, role:"tabpanel", "aria-labelledby":"settings-tab-"+i, tabindex:"0", hidden:i!==0}, p);
    const tab = el("button", {type:"button", onclick:()=>activate(i)}, p.querySelector("h2").textContent);
    tab.id="settings-tab-"+i; tab.setAttribute("role","tab"); tab.setAttribute("aria-controls", view.id);
    tab.setAttribute("aria-selected", String(i===0)); tab.tabIndex=i===0?0:-1;
    nav.append(tab);return view;
  });
  function activate(index, focus=false, updateUrl=true) {
    [...nav.children].forEach((tab,i)=>{tab.setAttribute("aria-selected",String(i===index));tab.tabIndex=i===index?0:-1;views[i].hidden=i!==index;});
    panels[index]._activate?.();
    if(updateUrl && location.hash !== "#"+keys[index]) history.pushState(null,"","#"+keys[index]);
    if(focus) nav.children[index].focus();
  }
  function fromHash() {
    const index = keys.indexOf(location.hash.slice(1).split("/")[0]);
    if(index >= 0 || !location.hash) activate(Math.max(0,index), false, false);
  }
  window.addEventListener("hashchange", fromHash);
  window.addEventListener("popstate", fromHash);
  fromHash();
  nav.addEventListener("keydown", e=>{
    const index=[...nav.children].indexOf(document.activeElement);
    if(index<0) return;
    let next;
    if(e.key==="ArrowRight") next=(index+1)%views.length;
    else if(e.key==="ArrowLeft") next=(index+views.length-1)%views.length;
    else if(e.key==="Home") next=0;
    else if(e.key==="End") next=views.length-1;
    if(next!==undefined){e.preventDefault();activate(next,true);}
  });
  return [nav,...views];
}

function websiteChatPanel(project) {
  const content = el("div", {"aria-live":"polite"});
  const section = panel("Website chat", content);
  async function load() {
    content.setAttribute("aria-busy", "true");
    content.replaceChildren(SuggestionShell.skeleton("rows", "Loading website chat settings…"));
    try {
      const settings = await api("/admin/chat/projects/" + project.id);
      const enabled = checkbox("Enable website chat", "enabled", settings.enabled);
      const origins = field("Allowed website origins", "origins", "textarea", settings.origins.join("\n"), {
        rows:4, placeholder:"https://your-website.com", "aria-describedby":"chat-origins-help",
      });
      const settingsForm = form([
        el("p", {class:"hint"}, "Let visitors start a conversation with your team from your website."),
        enabled, origins,
        el("p", {class:"hint", id:"chat-origins-help"}, "One HTTPS origin per line, without a path or trailing slash. Localhost is supported for development. These settings apply only to " + project.name + "."),
      ], "Save chat settings", async data => {
        const result = await api("/admin/chat/projects/" + project.id, "PUT", {
          enabled:data.has("enabled"),
          origins:data.get("origins").split("\n").map(x=>x.trim()).filter(Boolean),
          version:settings.version,
        });
        settings.version = result.version;
        enabled.querySelector("input").checked = result.enabled;
        origins.querySelector("textarea").value = result.origins.join("\n");
        notify("Website chat settings saved for " + project.name);
      });
      const attribute = value => value.replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
      const server = attribute(location.origin);
      const snippet = `<div id="suggestion-box-chat"></div>\n<script defer src="${server}/widget.js"\n  data-server="${server}"\n  data-project="${attribute(settings.project_identifier)}"\n  data-target="#suggestion-box-chat"></script>`;
      const copy = button("Copy snippet", async () => {
        try { await navigator.clipboard.writeText(snippet); notify("Widget snippet copied"); }
        catch { notify("Select and copy the snippet below."); }
      });
      const installation = el("section", {class:"website-chat-install", "aria-labelledby":"chat-install-title"},
        el("div", {class:"section-heading"}, el("h3", {id:"chat-install-title"}, "Install on your website"), copy),
        el("p", {class:"hint"}, "Add this snippet to a page on an allowed website. It opens chat for " + project.name + ". No API key is needed."),
        el("pre", {tabindex:"0", "aria-label":"Widget installation snippet"}, el("code", {}, snippet)),
        el("p", {class:"hint"}, "Choose a floating bubble, bottom bar, or inline chat, with optional scroll reveal, custom text, and light/dark colors in your website's embed code. ",
          link("Customize the widget →", "https://supaknock.com/api-docs#widget-theme")),
      );
      const metric = settings.response_time;
      content.replaceChildren(settingsForm, BusinessHours.editor(project.id), installation,
        el("section", {class:"website-chat-response", "aria-labelledby":"chat-response-title"},
          el("h3", {id:"chat-response-title"}, "Typical response time"),
          el("p", {}, metric.label || "Not enough recent replies yet"),
          el("p", {class:"hint"}, `${metric.answered_count} answered by your team · ${metric.unanswered_count} awaiting a human reply in the last ${metric.window_days} days. AI replies do not count. The widget shows a response-time estimate once there are at least ${metric.minimum_answered} conversations with a human reply and recent reply activity.`)),
        link("Open shared inbox →", "/admin/chat?project=" + encodeURIComponent(project.id)),
      );
    } catch(error) {
      content.replaceChildren(el("p", {class:"error", role:"alert"}, error.message), button("Retry loading chat settings", load));
    } finally { content.setAttribute("aria-busy", "false"); }
  }
  load();
  return section;
}

async function home() {
  if (me?.role !== "admin") return myRequests();
  const container = el("div", { class: "grid" });
  root.replaceChildren(
    hero(
      "Community feedback",
      "Projects",
      "Choose a project to suggest a feature, report a bug, or see what’s being worked on.",
    ),
    el("div", {class:"section-heading"}, el("h2", {}, "Explore projects"), el("span", {}, "Feature requests and bug reports")),
    container,
  );
  await paged(container, "/projects", (items) =>
    items.map((p) =>
      el(
        "article",
        { class: "card project-card" },
        projectIcon(),
        link(p.name, "/projects/" + p.identifier, "title"),
        el("p", {}, p.description || "Browse boards and share your ideas."),
        link("Explore boards →", "/projects/" + p.identifier),
      ),
    ),
  );
  const state = await api("/setup");
  if (state.available)
    root.append(
      panel(
        "Welcome to your new installation",
        el(
          "p",
          { class: "hint" },
          "The installation owner can create the first administrator using the deployment setup token.",
        ),
        link("Set up SupaKnock", "/setup", "button"),
      ),
    );
}
async function authPage(kind) {
  const setup = kind === "setup",
    signup = kind === "register";
  const state = await api("/setup");
  if (setup && !state.available) {
    root.replaceChildren(
      hero(
        "Setup complete",
        "Your workspace is ready.",
        "Sign in to manage projects and feedback.",
      ),
      link("Sign in", "/login", "button"),
    );
    return;
  }
  if (signup && !state.registration_enabled) {
    root.replaceChildren(
      hero(
        "Accounts",
        "Registration is disabled",
        "Contact the installation owner for access.",
      ),
    );
    return;
  }
  const f = form(
    [
      ...(setup
        ? [
            field("Setup token", "setup_token", "password", "", {
              required: true,
              autocomplete: "off",
              minlength: 24,
            }),
          ]
        : []),
      ...(signup || setup
        ? accountFields()
        : [
            field("Email", "email", "email", "", {
              required: true,
              autocomplete: "email",
            }),
            field("Password", "password", "password", "", {
              required: true,
              autocomplete: "current-password",
              maxlength: 128,
            }),
          ]),
    ],
    setup ? "Create administrator" : signup ? "Create account" : "Sign in",
    async (data) => {
      await api(
        setup ? "/setup" : signup ? "/auth/register" : "/auth/login",
        "POST",
        Object.fromEntries(data),
      );
      const next = new URLSearchParams(location.search).get("return");
      location.href =
        next &&
        next.startsWith("/") &&
        !next.startsWith("//") &&
        !next.includes("\\")
          ? next
          : setup
            ? "/admin"
            : "/workspace";
    },
  );
  root.replaceChildren(
    el(
      "div",
      { class: "narrow" },
      hero(
        setup ? "First run" : "Welcome",
        setup
          ? "Set up SupaKnock"
          : signup
            ? "Create an account"
            : "Welcome back.",
        setup
          ? "Enter the token supplied with your deployment."
          : signup ? "Create an account to submit feedback, vote, and follow requests." : "Sign in to your support and feedback workspace.",
      ),
      panel(
        setup
          ? "Create the first admin"
          : signup
            ? "Create an account"
            : "Sign in",
        f,
      ),
      el(
        "p",
        { class: "hint" },
        "Email is used for login and is not verified. Password recovery is handled by the installation owner.",
      ),
      setup
        ? null
        : link(
            signup
              ? "Already have an account? Sign in"
              : "New here? Create an account",
            (signup ? "/login" : "/register") + (new URLSearchParams(location.search).has("return") ? "?return=" + encodeURIComponent(new URLSearchParams(location.search).get("return")) : ""),
          ),
    ),
  );
}
async function projectPage(ref) {
  const p = await api("/projects/" + ref);
  const list = el("div", { class: "grid" });
  root.replaceChildren(
    ...(me?.role === "admin" ? [link("← All projects", "/workspace", "crumb")] : []),
    hero("Project", p.name, p.description),
    list,
  );
  await paged(list, "/projects/" + p.id + "/boards", (items) =>
    items.map((b) =>
      el(
        "article",
        { class: "card project-card" },
        projectIcon(),
        link(b.name, "/boards/" + b.id, "title"),
        el("p", {}, b.description || "Ideas and improvements for " + p.name),
        link("View requests →", "/boards/" + b.id),
      ),
    ),
  );
}
async function boardPage(bid) {
  const b = await api("/boards/" + bid);
  const list = el("div", {class:"request-list"});
  const filters = el(
    "div",
    { class: "filters" },
    select(
      "Lifecycle",
      "status",
      [["", "All statuses"], ...statuses.map((s) => [s, labels[s]])],
      "",
    ),
    select(
      "Order",
      "order",
      [
        ["newest", "Newest"],
        ["popular", "Most voted"],
      ],
      "newest",
    ),
  );
  async function load() {
    const s = filters.querySelector("[name=status]").value,
      o = filters.querySelector("[name=order]").value;
    await paged(
      list,
      "/boards/" + bid + "/requests?order=" + o + ((b.archived_at || b.project_archived_at) ? "&moderation=all" : "") + (s ? "&status=" + s : ""),
      (items) => items.map(requestCard),
    );
  }
  filters.addEventListener("change", () =>
    load().catch((e) => notify(e.message, "error")),
  );
  const submit = me
    ? el(
        "details",
        {class:"compose-details"},
        el("summary", {}, "Share an idea"),
        panel(
          "New request",
          el(
            "p",
            { class: "hint" },
            b.visibility === "private" ? "This board is private. Only you and the team can view your request; the connected app can also access it through its authorized backend." : "Requests are reviewed before appearing on this board.",
          ),
          form(contentFields(), "Submit for review", async (data) => {
            const r = await api(
              "/boards/" + bid + "/requests",
              "POST",
              Object.fromEntries(data),
            );
            location.href = "/requests/" + r.id;
          }),
        ),
      )
    : link(
        "Sign in to share an idea",
        "/login?return=" + encodeURIComponent("/boards/" + bid),
        "button",
      );
  root.replaceChildren(
    link("← Back to project", "/projects/" + b.project_id, "crumb"),
    hero(b.visibility === "private" ? "Private feedback board" : "Feedback board", b.name, b.description),
    (b.archived_at || b.project_archived_at) ? panel("Archived",el("p",{},"This board is read-only. Its requests and history are preserved."),me?.role === "admin" ? link("Manage project", "/admin/projects/"+b.project_id) : null) : submit,
    el(
      "div",
      { class: "toolbar" },
      el("h2", {}, b.visibility === "private" && me?.role !== "admin" ? "Your accepted requests" : "All requests"),
      filters,
    ),
    list,
  );
  await load();
}
async function requestPage(rid) {
  const r = await api("/requests/" + rid);
  const main = el(
    "div",
    {class:"request-main"},
    hero("Feature request", r.title),
    el(
      "div",
      { class: "meta" },
      badge(
        r.moderation && r.moderation !== "accepted" ? r.moderation : r.status,
      ),
      el("span", {}, r.vote_count + (r.vote_count === 1 ? " vote" : " votes")),
      r.creator_name ? el("span", {}, "Suggested by " + r.creator_name) : null,
    ),
    el(
      "div",
      { class: "panel content request-description" },
      r.description || "No description provided.",
    ),
  );
  const aside = el("aside");
  if (r.archived) {
    aside.append(panel("Archived",el("p",{},"This request belongs to an archived board or project. Restore it to make changes."),link("Manage project", "/admin/projects/"+r.project_id)));
  } else if (!r.moderation || r.moderation === "accepted") {
    aside.append(
      panel(
        "Support this idea",
        el(
          "p",
          { class: "hint" },
          "Add your vote to help the team understand what matters.",
        ),
        me
          ? button(
              r.voted ? "Remove my vote" : "↑ Vote for this",
              async () => {
                await api("/requests/" + rid + "/vote", "PUT", {
                  present: !r.voted,
                });
                await requestPage(rid);
                root.querySelector(".vote-action")?.focus({preventScroll:true});
                notify(r.voted ? "Your vote was removed" : "Your vote was added");
              },
              r.voted ? "secondary vote-action" : "vote-action",
            )
          : link(
              "Sign in to vote",
              "/login?return=" + encodeURIComponent("/requests/" + rid),
              "button",
            ),
      ),
    );
  } else {
    aside.append(
      panel(
        "Your submission",
        badge(r.moderation),
        el(
          "p",
          { class: "hint" },
          r.moderation === "pending"
            ? "Your idea is waiting for an administrator to review it."
            : "You can update this idea and submit it for review again.",
        ),
        r.rejection_reason
          ? el("p", { class: "content" }, r.rejection_reason)
          : null,
      ),
    );
  }
  if (r.can_edit) {
    main.append(
      el(
        "details",
        {},
        el(
          "summary",
          {},
          r.moderation === "rejected"
            ? "Edit and resubmit"
            : "Edit your request",
        ),
        panel(
          "Update your idea",
          form(
            contentFields(r.title, r.description),
            r.moderation === "rejected"
              ? "Resubmit for review"
              : "Save changes",
            async (data) => {
              await api("/requests/" + rid, "PATCH", {
                ...Object.fromEntries(data),
                version: r.version,
              });
              notify("Request updated");
              await requestPage(rid);
            },
          ),
        ),
      ),
    );
    aside.append(
      button(
        "Delete request",
        async () => {
          if (!confirm("Delete this request? This cannot be undone.")) return;
          await api("/requests/" + rid + "?version=" + r.version, "DELETE");
          location.href = "/my-requests";
        },
        "danger",
      ),
    );
  }
  if (me?.role === "admin" && !r.archived) {
    aside.append(panel("Submission details",
      el("dl", {class:"metadata-list"},
        el("div", {}, el("dt", {}, "Submitted from"), el("dd", {class:"ip-address"}, r.submitted_ip_address || "Not recorded")),
        el("div", {}, el("dt", {}, "Submitted"), el("dd", {}, el("time", {datetime:r.created_at}, new Date(r.created_at).toLocaleString())))),
      el("p", {class:"hint"}, "Connection details are visible only to administrators.")));
    main.append(
      panel(
        "Moderation & progress",
        ...(r.moderation === "pending"
          ? [
              el(
                "div",
                { class: "actions" },
                button(
                  "Accept request",
                  async () => {
                    await api("/requests/" + rid + "/accept", "POST", {
                      version: r.version,
                    });
                    notify("Request accepted");
                    await requestPage(rid);
                  },
                  "",
                ),
              ),
              form(
                [
                  field(
                    "Rejection reason (optional)",
                    "reason",
                    "textarea",
                    "",
                    { maxlength: 2000 },
                  ),
                ],
                "Reject request",
                async (data) => {
                  await api("/requests/" + rid + "/reject", "POST", {
                    reason: data.get("reason") || null,
                    version: r.version,
                  });
                  await requestPage(rid);
                },
              ),
            ]
          : r.moderation === "accepted"
            ? [
                form(
                  [
                    select(
                      "Lifecycle",
                      "status",
                      statuses.map((s) => [s, labels[s]]),
                      r.status,
                    ),
                  ],
                  "Update status",
                  async (data) => {
                    await api("/requests/" + rid + "/status", "PUT", {
                      status: data.get("status"),
                      version: r.version,
                    });
                    notify("Status updated");
                    await requestPage(rid);
                  },
                ),
              ]
            : [
                el(
                  "p",
                  { class: "hint" },
                  "Rejected requests return to the queue when their creator edits and resubmits them.",
                ),
              ]),
        el(
          "details",
          {},
          el("summary", {}, "Edit content as administrator"),
          form(
            contentFields(r.title, r.description),
            "Save content",
            async (data) => {
              await api("/requests/" + rid + "/content", "PATCH", {
                ...Object.fromEntries(data),
                version: r.version,
              });
              await requestPage(rid);
            },
          ),
        ),
      ),
    );
  }
  const history = el("div");
  main.append(panel("History", history));
  root.replaceChildren(
    link("← " + r.board_name, "/boards/" + r.board_id, "crumb"),
    el("div", { class: "split request-layout" }, main, aside),
  );
  await paged(history, "/requests/" + rid + "/history", (items) =>
    items.map((e) =>
      el(
        "div",
        { class: "row history-row" },
        el("strong", {}, e.type.replace("request.", "").replaceAll("_", " ")),
        el("p", { class: "hint" }, new Date(e.occurred_at).toLocaleString()),
        me?.role === "admin"
          ? el("p", {class:"hint history-ip"}, "IP address · ", el("span", {class:"ip-address"}, e.ip_address || "Not recorded"))
          : null,
        e.request
          ? el(
              "p",
              { class: "hint" },
              e.request.rejection_reason || labels[e.request.status] || "",
            )
          : e.status
            ? el("p", { class: "hint" }, labels[e.status])
            : null,
      ),
    ),
  );
}
async function myRequests() {
  if (!requireLogin()) return;
  const list = el("div", {class:"request-list"});
  root.replaceChildren(
    hero(
      "Your ideas",
      "My requests",
      "Keep track of your submissions, including those still awaiting review.",
    ),
    list,
  );
  await paged(list, "/me/requests", (items) => items.map(requestCard));
}
async function accountActivity() {
  if (!requireAdmin()) return;
  const userId = new URLSearchParams(location.search).get("user_id");
  const list = el("div", {class:"account-activity"});
  const eventLabels = {signup:"Signed up", login:"Signed in", account_created:"Account created", setup:"First administrator created"};
  const transportLabels = {cookie:"Website", native:"Native app"};
  root.replaceChildren(
    hero("Administration", "Account activity", "Successful sign-ins and account creation across your installation."),
    ...(userId ? [el("div", {class:"account-activity-filter"}, el("p", {class:"hint"}, "Showing activity for one account."), link("Show all accounts", "/admin/account-activity", "button secondary"))] : []),
    list,
    el("p", {class:"hint"}, "Only administrators can view this history. Older activity may not have an IP address."));
  await paged(list, "/admin/account-activity?limit=25" + (userId ? "&user_id=" + encodeURIComponent(userId) : ""), (items) =>
    el("div", {class:"activity-table-wrap"},
      el("table", {class:"activity-table"},
        el("caption", {class:"visually-hidden"}, "Account creation and successful sign-in history"),
        el("thead", {}, el("tr", {}, ["Account", "Activity", "IP address", "When"].map(text => el("th", {scope:"col"}, text)))),
        el("tbody", {}, items.map(item => el("tr", {},
          el("td", {"data-label":"Account"},
            link(item.display_name || item.email || "Account", "/admin/account-activity?user_id=" + encodeURIComponent(item.user_id), "activity-account"),
            item.display_name && item.email ? el("span", {class:"activity-secondary"}, item.email) : null,
            el("span", {class:"activity-secondary"}, item.role === "admin" ? "Administrator" : "Participant")),
          el("td", {"data-label":"Activity"}, el("span", {}, eventLabels[item.event_type] || "Account activity"),
            transportLabels[item.transport] ? el("span", {class:"activity-secondary"}, transportLabels[item.transport]) : null),
          el("td", {"data-label":"IP address"}, el("span", {class:"ip-address"}, item.ip_address || "Not recorded")),
          el("td", {"data-label":"When"}, el("time", {datetime:item.occurred_at}, new Date(item.occurred_at).toLocaleString()))))))));
}
async function account() {
  if (!requireLogin()) return;
  root.replaceChildren(
    el(
      "div",
      { class: "narrow" },
      hero(
        "Your account",
        "Account settings",
        me.role === "admin" ? "Manage your password and shared support preferences." : "Manage your account password.",
      ),
      panel(
        "Password",
        form(
          [
            field("Current password", "current_password", "password", "", {
              required: true,
              autocomplete: "current-password",
            }),
            field("New password", "new_password", "password", "", {
              required: true,
              minlength: 12,
              maxlength: 128,
              autocomplete: "new-password",
            }),
          ],
          "Change password",
          async (data) => {
            await api("/auth/password", "POST", Object.fromEntries(data));
            location.href = "/login";
          },
        ),
      ),
    ),
  );
  if(me?.role==='admin')root.firstElementChild.append(BusinessHours.editor());
}
async function pushPage(offset = 0) {
  if (!requireAdmin()) return;
  const [settings, devices, deliveries] = await Promise.all([
    api('/admin/push'), api('/admin/devices'), api('/admin/push/deliveries?offset=' + offset),
  ]);
  root.replaceChildren(
    link('← Administration', '/admin', 'crumb'),
    hero('Admin notifications', 'Push notifications',
      'Connect the optional push relay, then enable notifications in the iPhone app.'),
    panel('Push connection',
      el('p', {}, settings.enabled ? 'Push is enabled.' : 'Push is off. Your feedback and chat still work.'),
      settings.last_error ? el('p', {class:'error',role:'status'}, 'Connection needs attention: ' + settings.last_error) : null,
      form([
        field('Relay URL', 'relay_url', 'url', settings.relay_url, {required:true, maxlength:2048, readonly:settings.connected}),
        checkbox('Enable push notifications', 'enabled', settings.enabled),
        el('p',{class:'muted'},'The relay receives device tokens and event identifiers. Notification text contains no feedback or chat content.'),
      ], 'Save push settings', async data => {
        await api('/admin/push', 'PUT', {enabled:data.has('enabled'),relay_url:data.get('relay_url')});
        await pushPage(); notify('Push settings saved');
      }),
      settings.enabled && settings.connected ? button('Rotate relay credential', async () => {
        await api('/admin/push/rotate','POST'); await pushPage(); notify('Relay credential rotated');
      }) : null,
    ),
    panel('Your registered devices',
      devices.items.length ? devices.items.map(d => el('article',{class:'card'},
        el('h3',{},d.name),
        el('p',{},d.enabled ? 'Enabled' : 'Disabled'),
        el('p',{class:'muted'},[d.request_notifications ? 'New requests' : null,d.chat_notifications ? 'New chat messages' : null,d.form_notifications ? 'Form submissions' : null].filter(Boolean).join(' · ') || 'No notification types selected'),
        button('Remove device',async () => {
          if (!confirm('Remove notifications for this device?')) return;
          await api('/admin/devices/'+d.id,'DELETE'); await pushPage();
        }),
      )) : empty('No devices registered. Sign in to the iPhone app and enable notifications.'),
    ),
    panel('Delivery history',
      el('p',{class:'muted'},'Succeeded means Apple accepted the notification. It does not confirm that it appeared on the device.'),
      deliveries.items.length ? deliveries.items.map(d => el('article',{class:'card'},
        el('h3',{},d.kind==='form.submission_created' ? 'New form submission' : d.kind==='chat.message' ? 'New chat message' : 'New request'),
        badge(d.state),
        el('p',{class:'muted'},new Date(d.created_at).toLocaleString()),
        d.error ? el('p',{class:'error'},d.error) : null,
        link('Open '+(d.kind==='form.submission_created' ? 'submission' : d.kind==='chat.message' ? 'conversation' : 'request'),d.kind==='form.submission_created' ? '/admin/form-submissions/'+d.resource_id : d.kind==='chat.message' ? '/admin/chat?conversation='+encodeURIComponent(d.resource_id) : '/requests/'+d.resource_id),
        d.state==='failed' ? button('Retry delivery', async () => {
          await api('/admin/push/deliveries/'+d.id+'/retry','POST'); await pushPage(offset);
        }) : null,
      )) : empty('No push deliveries yet.'),
      deliveries.items.length || offset ? pager(deliveries,next => pushPage(next)) : null,
      button('Refresh deliveries',() => pushPage(offset)),
    ),
  );
}

function archiveControl(kind, item, reload) {
  const archived = Boolean(item.archived_at), action = (archived ? "Restore " : "Archive ") + kind;
  const trigger = button(action, () => {
    const dialog = el("dialog", {class:"archive-dialog", "aria-labelledby":"archive-confirm-title"},
      el("h2", {id:"archive-confirm-title"}, action + "?"),
      el("p", {}, archived
        ? (kind === "project" ? "Existing settings will become active again. Boards archived separately will stay archived." : "This board will accept new activity again with its saved visibility. Its requests and history will stay intact.")
        : "This " + kind + " will be hidden from visitors and new activity will stop. History is kept, and you can restore it later."));
    const close = () => { dialog.close(); dialog.remove(); trigger.focus(); };
    const error = el("p", {class:"error", role:"alert", hidden:true});
    const cancel = button("Cancel", close);
    const confirm = button(action, async () => {
      cancel.disabled = true;
      try {
        await api("/" + (kind === "project" ? "projects" : "boards") + "/" + item.id + "/archive", "PUT", {archived: !archived, version: item.version});
        close(); await reload(); notify(archived ? "Restored" : "Archived — history preserved");
      } catch (err) { error.textContent = err.message; error.hidden = false; }
      finally { cancel.disabled = false; }
    }, "primary");
    dialog.append(error, el("div", {class:"actions"}, cancel, confirm));
    dialog.addEventListener("cancel", e => { e.preventDefault(); if (!cancel.disabled) close(); });
    document.body.append(dialog); dialog.showModal(); cancel.focus();
  });
  return trigger;
}

async function admin(archive = "active") {
  if (!requireAdmin()) return;
  const counts = await api("/admin/dashboard");
  const archiveFilter = select("Show projects", "archive", [["active","Active projects"],["archived","Archived projects"]], archive);
  archiveFilter.addEventListener("change", e => admin(e.target.value).catch(error => notify(error.message, "error")));
  const queue = el("div"),
    projects = el("div", { class: "grid" });
  root.replaceChildren(
    hero(
      "Workspace overview",
      "Overview",
      "Review new feedback and check your support activity.",
    ),
    el("div", {class:"stats"},
      el("div", {class:"stat"}, el("div", {class:"stat-top"}, "Awaiting approval", SuggestionShell.icon("inbox")), el("strong", {"data-count":"pending"}, counts.pending_requests), el("small", {class:counts.pending_requests ? "" : "stat-accent"}, counts.pending_requests ? "Ideas ready for your review" : "You’re all caught up")),
      el("div", {class:"stat"}, el("div", {class:"stat-top"}, "Webhook failures", SuggestionShell.icon("globe")), el("strong", {"data-count":"failed"}, counts.failed_deliveries), el("small", {class:counts.failed_deliveries ? "" : "stat-accent"}, counts.failed_deliveries ? "Deliveries that need attention" : "No failed deliveries")),
      el("div", {class:"stat shortcut"}, SuggestionShell.icon("chat"), el("strong", {}, "Customer messages"), el("small", {}, "Read and reply to support conversations."), link("Open live chat →", "/admin/chat"))),
    el("div", {class:"section-heading"}, el("h2", {}, "Moderation queue"), el("span", {}, "Review before sharing with your community")),
    queue,
    el(
      "section",
      { class: "section", id:"projects" },
      el("div", {class:"section-heading"}, el("h2", {}, "Your projects"), el("span", {}, "Manage feedback and support by project")),
      archiveFilter,
      projects,
      el(
        "details",
        {class:"form-disclosure"},
        el("summary", {}, "Create project"),
        panel(
          "New project",
          form(resourceFields(), "Create project", async (data) => {
            const p = await api("/projects", "POST", Object.fromEntries(data));
            location.href = "/admin/projects/" + p.id;
          }),
        ),
      ),
    ),
    el(
      "details",
      {class:"form-disclosure"},
      el("summary", {}, "Create another administrator"),
      panel(
        "Administrator account",
        form(accountFields(), "Create administrator", async (data) => {
          await api("/admin/accounts", "POST", Object.fromEntries(data));
          notify("Administrator created");
        }),
      ),
    ),
  );
  const connectedSummary=el("section",{class:"section"});
  root.querySelector(".stats").after(connectedSummary);
  ConnectedApps.overview(connectedSummary);
  await paged(queue, "/moderation", (items) =>
    items.map((r) =>
      el(
        "article",
        { class: "card moderation-card" },
        link(r.title, "/requests/" + r.id, "title"),
        el("p", { class: "meta" }, r.project_name + " / " + r.board_name),
        el("p", { class: "content" }, r.description),
        el(
          "div",
          { class: "actions" },
          button(
            "Accept",
            async () => {
              await api("/requests/" + r.id + "/accept", "POST", {
                version: r.version,
              });
              notify("Request accepted");
              await admin();
            },
            "",
          ),
          link("Review / reject", "/requests/" + r.id, "button secondary"),
        ),
      ),
    ),
  );
  await paged(projects, "/projects?archive=" + archive, (items) =>
    items.map((p) =>
      el(
        "article",
        { class: "card project-card" },
        projectIcon(),
        link(p.name, "/admin/projects/" + p.id, "title"),
        el("p", {}, p.description),
        link("Manage project →", "/admin/projects/" + p.id),
        p.archived_at ? badge("Archived") : link("App data →", "/admin/projects/" + p.id + "/app-data"),
      ),
    ),
  );
}
async function adminProject(ref) {
  if (!requireAdmin()) return;
  const p = await api("/projects/" + ref),
    w = await api("/projects/" + p.id + "/webhook");
  if (p.archived_at) {
    const history = el("div");
    root.replaceChildren(link("← Administration", "/admin", "crumb"), hero("Archived project", p.name, p.description),
      panel("Project archived", el("p", {}, "Hidden from visitors. New feedback, chat, AI work and app-data reads are paused. Your history, knowledge and settings are kept."),
        el("p", {class:"hint"}, "Restoring resumes saved settings. Separately archived boards stay archived; interrupted AI jobs must be started again."),
        el("div",{class:"actions"},archiveControl("project",p,()=>adminProject(ref)),link("Chat history", "/admin/chat", "button secondary"))),
      panel("Preserved boards", history));
    await paged(history,"/projects/"+p.id+"/boards?archive=all",items=>items.map(b=>el("article",{class:"row archive-history-row"},link(b.name,"/boards/"+b.id),badge(b.archived_at?"Board archived":"Paused with project"))));
    return;
  }
  const boardFilter = select("Show boards", "board_archive", [["active","Active boards"],["archived","Archived boards"]], "active");
  boardFilter.addEventListener("change",()=>loadBoards().catch(error=>notify(error.message, "error")));
  const boardList = el("div"),
    keyList = el("div"),
    deliveryList = el("div"),
    secretArea = el("div");
  const keyForm = form(
    [
      field("Credential name", "name", "text", "", {
        required: true,
        maxlength: 100,
      }),
      el(
        "p",
        { class: "hint" },
        "Choose only the permissions this trusted backend needs. Store the secret on your backend.",
      ),
      ...["read", "participate", "audience:read", "manage", "forms:submit"].map((s) =>
        checkbox(s, "permissions", s === "read" || s === "participate", s),
      ),
    ],
    "Create credential",
    async (data) => {
      const k = await api("/projects/" + p.id + "/keys", "POST", {
        name: data.get("name"),
        permissions: data.getAll("permissions"),
      });
      showSecret(
        secretArea,
        "Save this API secret now. It will only be shown once.",
        k.secret,
      );
      await loadKeys();
    },
  );
  const webhookForm = form(
    [
      field("Destination URL", "url", "url", w.url || "", { maxlength: 2048 }),
      checkbox("Enable delivery", "enabled", w.enabled),
      el("p", { class: "hint" }, "Send these events to your project backend:"),
      ...events.map((e) => checkbox(e, "events", w.events.includes(e), e)),
      checkbox("Rotate signing secret", "rotate_secret"),
      el(
        "p",
        { class: "hint" },
        "Existing queued deliveries keep their original destination and signing secret. Keep the previous signing secret until those jobs finish.",
      ),
    ],
    "Save webhook settings",
    async (data) => {
      const result = await api("/projects/" + p.id + "/webhook", "PUT", {
        url: data.get("url") || null,
        enabled: data.has("enabled"),
        events: data.getAll("events"),
        rotate_secret: data.has("rotate_secret"),
        version: w.version,
      });
      w.version = result.version;
      p.version = result.version;
      if (result.signing_secret)
        showSecret(
          secretArea,
          "Save this signing secret now. It will only be shown once.",
          result.signing_secret,
        );
      notify("Webhook settings saved");
    },
  );
  async function loadKeys() {
    await paged(keyList, "/projects/" + p.id + "/keys", (items) =>
      items.map((k) =>
        el(
          "div",
          { class: "row" },
          el("strong", {}, k.name),
          el(
            "p",
            { class: "hint" },
            k.prefix + "… · " + k.permissions.join(", "),
          ),
          k.revoked_at
            ? badge("Revoked")
            : button(
                "Revoke",
                async () => {
                  if (
                    !confirm(
                      "Revoke this credential? Integrations using it will lose access.",
                    )
                  )
                    return;
                  await api("/projects/" + p.id + "/keys/" + k.id, "DELETE");
                  await loadKeys();
                },
                "danger",
              ),
        ),
      ),
    );
  }
  async function loadDeliveries() {
    await paged(deliveryList, "/projects/" + p.id + "/deliveries", (items) =>
      items.map((d) => {
        const detail = el("div");
        return el(
          "div",
          { class: "row" },
          el(
            "div",
            { class: "meta" },
            badge(d.state),
            el("strong", {}, d.type),
            el("span", {}, d.attempt_count + " attempts"),
          ),
          el("p", { class: "hint" }, d.destination),
          d.error ? el("p", { class: "error" }, d.error) : null,
          el(
            "div",
            { class: "actions" },
            button("Inspect", async () => {
              const x = await api("/projects/" + p.id + "/deliveries/" + d.id);
              detail.replaceChildren(el("pre", {}, JSON.stringify(x, null, 2)));
            }),
            d.state === "failed"
              ? button("Retry delivery", async () => {
                  await api(
                    "/projects/" + p.id + "/deliveries/" + d.id + "/retry",
                    "POST",
                  );
                  notify("Retry queued");
                  await loadDeliveries();
                })
              : null,
          ),
          detail,
        );
      }),
    );
  }
  root.replaceChildren(
    link("← Administration", "/admin", "crumb"),
    hero("Project settings", p.name, "Manage your project’s feedback, website chat, and integrations."),
    secretArea,
    panel(
      "Project details",
      form(resourceFields(p, false), "Save project details", async (data) => {
        const result = await api("/projects/" + p.id, "PATCH", {
          ...Object.fromEntries(data),
          version: p.version,
        });
        p.version = result.version;
        p.name = result.name;
        p.description = result.description;
        w.version = result.version;
        root.querySelector(".hero")?.replaceWith(hero("Project settings",p.name,"Manage your project’s feedback, website chat, and integrations."));
        notify("Project updated");
      }),
      el("div",{class:"archive-section"},el("h3",{},"Archive project"),el("p",{class:"hint"},"Hide this project and pause new activity while keeping its boards, conversations and settings. You can restore it later."),el("div",{class:"actions"},archiveControl("project",p,()=>adminProject(ref)))),
    ),
    panel(
      "Boards",
      boardFilter,
      boardList,
      el(
        "details",
        {},
        el("summary", {}, "Create board"),
        form(boardFields(), "Create board", async (data) => {
          await api(
            "/projects/" + p.id + "/boards",
            "POST",
            Object.fromEntries(data),
          );
          await loadBoards();
        }),
      ),
    ),
    panel(
      "API credentials",
      keyList,
      el("details", {}, el("summary", {}, "Create credential"), keyForm),
    ),
    websiteChatPanel(p),
    SuggestionAI.projectPanel(p),
    ConnectedApps.settings(p),
    panel(
      "Webhook destination",
      webhookForm,
      el(
        "div",
        { class: "actions" },
        button("Send test webhook", async () => {
          await api("/projects/" + p.id + "/webhook/test", "POST");
          notify("Test webhook queued");
          await loadDeliveries();
        }),
      ),
    ),
    panel(
      "Delivery history",
      button("Refresh deliveries", loadDeliveries),
      deliveryList,
    ),
  );
  const settingsPanels = [...root.children].filter(n => n.classList.contains("panel"));
  root.append(...settingTabs(settingsPanels));
  async function loadBoards() {
    await paged(boardList, "/projects/" + p.id + "/boards?archive=" + boardFilter.querySelector("select").value, (items) =>
      items.map((b) =>
        el(
          "div",
          { class: "row board-settings-row" },
          link(b.name, "/boards/" + b.id),
          b.archived_at ? badge("Archived") : null,
          b.visibility === "private" ? badge("Private") : null,
          el("div",{class:"actions"},archiveControl("board",b,loadBoards)),
          b.archived_at ? null : el(
            "details",
            {},
            el("summary", {}, "Edit board"),
            form(boardFields(b, false), "Save board", async (data) => {
              const result = await api("/boards/" + b.id, "PATCH", {
                ...Object.fromEntries(data),
                version: b.version,
              });
              b.version = result.version;
              notify("Board updated");
              await loadBoards();
            }),
          ),
        ),
      ),
    );
  }
  await loadBoards();
  await loadKeys();
  await loadDeliveries();
}
async function boot() {
  try {
    try {
      me = await api("/auth/me");
    } catch {
      me = null;
    }
    renderNav();
    root.classList.toggle("settings-page", ["/account", "/admin/push"].includes(location.pathname));
    const parts = location.pathname.split("/").filter(Boolean);
    if (!parts.length || (parts.length === 1 && parts[0] === "workspace")) await home();
    else if (["login", "register", "setup"].includes(parts[0]))
      await authPage(parts[0]);
    else if (parts[0] === "projects") await projectPage(parts[1]);
    else if (parts[0] === "boards") await boardPage(parts[1]);
    else if (parts[0] === "requests") await requestPage(parts[1]);
    else if (parts[0] === "my-requests") await myRequests();
    else if (parts[0] === "account") await account();
    else if (parts[0] === "admin" && parts[1] === "form-submissions") await SupaForms.detail(parts[2]);
    else if (parts[0] === "admin" && parts[1] === "forms") {
      if(parts[2]==='new') await SupaForms.editor();
      else if(parts[2] && new URLSearchParams(location.search).has('edit')) await SupaForms.editor(parts[2]);
      else if(parts[2]) await SupaForms.inbox(parts[2]);
      else await SupaForms.list();
    }
    else if (parts[0] === "admin" && parts[1] === "push") await pushPage();
    else if (parts[0] === "admin" && parts[1] === "app-activity") await ConnectedApps.activity();
    else if (parts[0] === "admin" && parts[1] === "projects" && parts[3] === "app-data") await ConnectedApps.workspace(parts[2]);
    else if (parts[0] === "admin" && parts[1] === "account-activity") await accountActivity();
    else if (parts[0] === "admin" && parts[1] === "ai" && parts[2] === "tools") await SuggestionAI.toolConnections();
    else if (parts[0] === "admin" && parts[1] === "ai" && parts[2] === "providers") await SuggestionAI.accountKeys();
    else if (parts[0] === "admin" && parts[1] === "ai") await SuggestionAI.runtime();
    else if (parts[0] === "admin" && parts[1] === "projects" && parts[3] === "ai") {
      const section=location.hash.slice(1)||"setup";
      history.replaceState(null,"","/admin/projects/"+encodeURIComponent(parts[2])+"#ai-support/"+section);
      await adminProject(parts[2]);
    }
    else if (parts[0] === "admin" && parts[1] === "projects")
      await adminProject(parts[2]);
    else if (parts[0] === "admin") await admin();
    else throw new Error("Page not found");
  } catch (e) {
    root.replaceChildren(
      hero(
        "Unable to load this page",
        e.message,
        "Check your access and try again.",
      ),
      link("Sign in", "/login", "button"),
      ...(me?.role === "admin" ? [link("All projects", "/workspace", "button secondary")] : []),
    );
  } finally {
    root.setAttribute("aria-busy", "false");
    if(location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
  }
}
boot();
setInterval(async () => {
  if (location.pathname !== "/admin" || me?.role !== "admin") return;
  try {
    const c = await api("/admin/dashboard");
    document.querySelector("[data-count=pending]").textContent =
      c.pending_requests;
    document.querySelector("[data-count=failed]").textContent =
      c.failed_deliveries;
  } catch {}
}, 30000);
