(() => {
  const stateUrl = "/fixture-state";

  function getElement(selector) {
    return document.querySelector(selector);
  }

  function applyDashboardState(state) {
    const body = document.body;
    body.dataset.routeVariant = state.routeVariant;

    const u = new URL(window.location.href);
    const rv = state.routeVariant === "route-v1" ? "1" : "2";
    if (u.searchParams.get("fixtureRv") !== rv) {
      u.searchParams.set("fixtureRv", rv);
      history.replaceState({}, "", u.toString());
    }

    const expectedPath =
      state.routeVariant === "route-v1" ? "/dashboard.html" : "/dashboard_r2.html";
    if (
      (window.location.pathname === "/dashboard.html" ||
        window.location.pathname === "/dashboard_r2.html") &&
      window.location.pathname !== expectedPath
    ) {
      const next = new URL(expectedPath, window.location.origin);
      next.search = window.location.search;
      window.location.assign(next.toString());
      return;
    }

    const region = getElement("#region-orders");
    if (region) {
      region.dataset.regionVersion = String(state.regionVersion);
      const list = getElement("#orders-list");
      if (list) {
        list.innerHTML =
          state.regionVersion === 1
            ? "<li>Invoice A - queued</li><li>Invoice B - queued</li>"
            : "<li>Invoice A - approved</li><li>Invoice C - queued</li>";
      }
    }

    const modal = getElement("#dashboard-modal");
    if (modal) {
      modal.hidden = !state.modalOpen;
      modal.setAttribute("aria-hidden", state.modalOpen ? "false" : "true");
    }

    const primaryAction = getElement("[data-fixture-action='primary']");
    if (primaryAction) {
      const staleRefVersion = Number(state.staleRefVersion);
      const testId = staleRefVersion === 1 ? "primary-action" : "primary-action-v2";
      primaryAction.setAttribute("data-testid", testId);
      primaryAction.dataset.staleRefVersion = String(staleRefVersion);
    }
  }

  async function syncFixtureState() {
    try {
      const response = await fetch(stateUrl, { cache: "no-store" });
      if (!response.ok) {
        return;
      }
      const payload = await response.json();
      applyDashboardState(payload);
    } catch (error) {
      // Keep fixtures deterministic even if state polling briefly fails.
      console.warn("fixture-state-sync-failed", error);
    }
  }

  void syncFixtureState();
  window.setInterval(() => {
    void syncFixtureState();
  }, 250);
})();
