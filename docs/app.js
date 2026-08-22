(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = { listings: [], status: null };
  const controls = {
    keyword: $("#keyword"), sort: $("#sort"), minPrice: $("#min-price"),
    maxPrice: $("#max-price"), endingHours: $("#ending-hours"), seller: $("#seller"),
    category: $("#hunt-category"), targetKeyword: $("#target-keyword"),
    multiplePhotos: $("#multiple-photos"), noBids: $("#no-bids"),
    undervalued: $("#undervalued")
  };

  const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
  const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
  const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
    })[char]);
  }

  function timeRemaining(endTime) {
    const milliseconds = new Date(endTime).getTime() - Date.now();
    if (!Number.isFinite(milliseconds)) return "End time unknown";
    if (milliseconds <= 0) return "Ended";
    const hours = Math.floor(milliseconds / 3_600_000);
    if (hours < 1) return `${Math.max(1, Math.ceil(milliseconds / 60_000))}m left`;
    if (hours < 24) return `${hours}h left`;
    return `${Math.floor(hours / 24)}d ${hours % 24}h left`;
  }

  function ageLabel(timestamp) {
    const seconds = Math.round((new Date(timestamp).getTime() - Date.now()) / 1000);
    if (!Number.isFinite(seconds)) return "unknown";
    if (Math.abs(seconds) < 60) return "just now";
    if (Math.abs(seconds) < 3600) return relative.format(Math.round(seconds / 60), "minute");
    if (Math.abs(seconds) < 86400) return relative.format(Math.round(seconds / 3600), "hour");
    return relative.format(Math.round(seconds / 86400), "day");
  }

  function searchableText(item) {
    return [item.title, item.description, item.seller, item.item_id, item.category,
      ...(item.discovered_by || []), ...(item.matched_keywords || item.matched_minerals || []),
      ...(item.hunt_labels || [])].join(" ").toLocaleLowerCase();
  }

  function activeFilterCount() {
    return Object.entries(controls).filter(([key, element]) => {
      if (["keyword", "sort"].includes(key)) return false;
      return element.type === "checkbox" ? element.checked : Boolean(element.value);
    }).length;
  }

  function visibleListings() {
    const query = controls.keyword.value.trim().toLocaleLowerCase();
    const seller = controls.seller.value.trim().toLocaleLowerCase();
    const targetKeyword = controls.targetKeyword.value.trim().toLocaleLowerCase();
    const category = controls.category.value;
    const minimum = controls.minPrice.value === "" ? null : Number(controls.minPrice.value);
    const maximum = controls.maxPrice.value === "" ? null : Number(controls.maxPrice.value);
    const endingHours = controls.endingHours.value === "" ? null : Number(controls.endingHours.value);
    const endCutoff = endingHours === null ? null : Date.now() + endingHours * 3_600_000;

    const filtered = state.listings.filter(item => {
      const price = Number(item.price || 0);
      const end = new Date(item.end_time).getTime();
      if (query && !searchableText(item).includes(query)) return false;
      if (seller && !String(item.seller || "").toLocaleLowerCase().includes(seller)) return false;
      if (targetKeyword && !searchableText(item).includes(targetKeyword)) return false;
      if (category && !(item.hunt_categories || []).includes(category)) return false;
      if (minimum !== null && price < minimum) return false;
      if (maximum !== null && price > maximum) return false;
      if (endCutoff !== null && (!Number.isFinite(end) || end > endCutoff)) return false;
      if (controls.multiplePhotos.checked && (item.images || []).length < 2) return false;
      if (controls.noBids.checked && Number(item.bids || 0) !== 0) return false;
      if (controls.undervalued.checked && !item.potentially_undervalued) return false;
      return true;
    });

    const sorters = {
      score: (a, b) => Number(b.score || 0) - Number(a.score || 0),
      newest: (a, b) => new Date(b.start_time) - new Date(a.start_time),
      ending: (a, b) => new Date(a.end_time) - new Date(b.end_time),
      "price-low": (a, b) => Number(a.price || 0) - Number(b.price || 0),
      "price-high": (a, b) => Number(b.price || 0) - Number(a.price || 0),
      bids: (a, b) => Number(a.bids || 0) - Number(b.bids || 0)
    };
    return filtered.sort(sorters[controls.sort.value] || sorters.score);
  }

  function setImage(img, source, alt) {
    img.src = source || "";
    img.alt = alt;
    img.addEventListener("error", () => img.classList.add("image-error"), { once: true });
  }

  function renderCard(item) {
    const fragment = $("#card-template").content.cloneNode(true);
    const card = $(".listing-card", fragment);
    const cardImage = $(".card-image", fragment);
    const thumbnails = item.thumbnails?.length ? item.thumbnails : item.images || [];
    setImage(cardImage, thumbnails[0] || item.images?.[0], item.title);
    $(".score-badge strong", fragment).textContent = item.score ?? 0;
    $(".ending-badge", fragment).textContent = timeRemaining(item.end_time);
    $(".price", fragment).textContent = currency.format(Number(item.price || 0));
    $(".bids", fragment).textContent = `${Number(item.bids || 0)} bid${Number(item.bids || 0) === 1 ? "" : "s"}`;
    $("h3", fragment).textContent = item.title;
    $(".seller-name", fragment).textContent = item.seller || "Seller not yet loaded";
    $(".item-id", fragment).textContent = `#${item.item_id}`;

    const thumbnailRow = $(".thumbnail-row", fragment);
    thumbnails.slice(0, 5).forEach((source, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.ariaLabel = `Show photo ${index + 1}`;
      const image = document.createElement("img");
      setImage(image, source, "");
      button.append(image);
      button.addEventListener("click", () => {
        setImage(cardImage, source, item.title);
      });
      thumbnailRow.append(button);
    });
    if (thumbnails.length > 5) {
      const more = document.createElement("span");
      more.className = "thumbnail-more";
      more.textContent = `+${thumbnails.length - 5}`;
      thumbnailRow.append(more);
    }

    const termRow = $(".term-row", fragment);
    const tags = [item.primary_hunt?.label, ...(item.discovered_by || [])].filter(Boolean);
    tags.slice(0, 3).forEach(term => {
      const tag = document.createElement("span");
      tag.textContent = term;
      termRow.append(tag);
    });

    $$(".image-button, .inspect-button", fragment).forEach(button => {
      button.addEventListener("click", () => openDetail(item));
    });
    card.dataset.itemId = item.item_id;
    return fragment;
  }

  function openDetail(item) {
    const dialog = $("#detail-dialog");
    const images = item.images || [];
    const shipping = item.shipping || {};
    const reasons = (item.score_reasons || []).map(reason => `<li>${escapeHtml(reason)}</li>`).join("");
    const imageMarkup = images.length
      ? images.map((source, index) => `<a href="${escapeHtml(source)}" target="_blank" rel="noopener"><img src="${escapeHtml(source)}" alt="${escapeHtml(item.title)} — photo ${index + 1}" loading="lazy"></a>`).join("")
      : `<div class="notice">No full-size images were returned for this listing.</div>`;
    $("#dialog-content").innerHTML = `
      <div class="detail-layout">
        <div class="detail-images">${imageMarkup}</div>
        <div class="detail-info">
          <p class="eyebrow">${escapeHtml(item.primary_hunt?.label || "Auction watch")} · Item #${escapeHtml(item.item_id)} · ${images.length} photo${images.length === 1 ? "" : "s"}</p>
          <h2 id="dialog-title">${escapeHtml(item.title)}</h2>
          <p class="detail-price">${currency.format(Number(item.price || 0))}</p>
          <p class="detail-sub">${Number(item.bids || 0)} bid${Number(item.bids || 0) === 1 ? "" : "s"} · ${escapeHtml(timeRemaining(item.end_time))}</p>
          <div class="detail-facts">
            <div><span>Seller</span><strong>${escapeHtml(item.seller || "Not yet loaded")}</strong></div>
            <div><span>Ends</span><strong>${item.end_time ? escapeHtml(dateTime.format(new Date(item.end_time))) : "Unknown"}</strong></div>
            <div><span>Category</span><strong>${escapeHtml(item.category || "Unknown")}</strong></div>
            <div><span>Shipping</span><strong>${shipping.pickup_only ? "Pickup only" : shipping.listed_price ? currency.format(shipping.listed_price) + " listed" : escapeHtml(shipping.carrier || "See listing")}</strong></div>
          </div>
          <div class="score-panel">
            <h3>Why it scored ${Number(item.score || 0)}</h3>
            <ul>${reasons}</ul>
          </div>
          <p class="detail-description">${escapeHtml(item.description || "The full description has not been loaded yet.")}</p>
          <a class="visit-listing" href="${escapeHtml(item.listing_url)}" target="_blank" rel="noopener">Open on ShopGoodwill ↗</a>
        </div>
      </div>`;
    dialog.showModal();
  }

  function render() {
    const listings = visibleListings();
    const gallery = $("#gallery");
    gallery.replaceChildren(...listings.map(renderCard));
    $("#result-summary").textContent = `${listings.length} listing${listings.length === 1 ? "" : "s"} worth a look`;
    $("#empty-state").hidden = listings.length !== 0;
    gallery.hidden = listings.length === 0;
    const count = activeFilterCount();
    $("#filter-count").hidden = count === 0;
    $("#filter-count").textContent = count;
  }

  function clearFilters() {
    Object.entries(controls).forEach(([key, element]) => {
      if (key === "sort") element.value = "score";
      else if (element.type === "checkbox") element.checked = false;
      else element.value = "";
    });
    render();
  }

  function populateCategories() {
    const categories = new Map();
    (state.status?.hunts || []).forEach(hunt => categories.set(hunt.id, hunt.label));
    state.listings.forEach(item => {
      if (item.primary_hunt?.id) categories.set(item.primary_hunt.id, item.primary_hunt.label);
    });
    controls.category.replaceChildren(new Option("All categories", ""));
    [...categories.entries()]
      .sort((a, b) => a[1].localeCompare(b[1]))
      .forEach(([id, label]) => controls.category.add(new Option(label, id)));
  }

  async function loadData() {
    try {
      const [listingsResponse, statusResponse] = await Promise.all([
        fetch("./data/listings.json", { cache: "no-store" }),
        fetch("./data/status.json", { cache: "no-store" })
      ]);
      if (!listingsResponse.ok) throw new Error(`Listings feed returned ${listingsResponse.status}`);
      state.listings = await listingsResponse.json();
      if (!Array.isArray(state.listings)) throw new Error("Listings feed is not an array");
      state.status = statusResponse.ok ? await statusResponse.json() : null;
      populateCategories();
      $("#active-count").textContent = state.listings.length.toLocaleString();
      const priorityCount = state.status?.high_priority_count ?? state.listings.filter(item => item.score >= 35).length;
      $("#priority-count").textContent = Number(priorityCount).toLocaleString();
      if (state.status?.generated_at) {
        $("#refresh-age").textContent = ageLabel(state.status.generated_at);
        $("#updated-at").textContent = `Data refreshed ${dateTime.format(new Date(state.status.generated_at))}`;
      } else {
        $("#refresh-age").textContent = "unknown";
      }
      if (state.status?.search_failures?.length) {
        const notice = $("#notice");
        notice.hidden = false;
        notice.textContent = `Partial refresh: ${state.status.search_failures.length} search request(s) failed gracefully. Existing active data was preserved.`;
      }
      render();
    } catch (error) {
      $("#result-summary").textContent = "The feed could not be loaded";
      const notice = $("#notice");
      notice.hidden = false;
      notice.textContent = `Start a local web server instead of opening index.html directly, then reload. ${error.message}`;
      $("#gallery").hidden = true;
    }
  }

  Object.values(controls).forEach(control => control.addEventListener("input", render));
  $("#filter-toggle").addEventListener("click", event => {
    const panel = $("#filters");
    panel.hidden = !panel.hidden;
    event.currentTarget.setAttribute("aria-expanded", String(!panel.hidden));
  });
  $("#clear-filters").addEventListener("click", clearFilters);
  $("#empty-clear").addEventListener("click", clearFilters);
  $("#dialog-close").addEventListener("click", () => $("#detail-dialog").close());
  $("#detail-dialog").addEventListener("click", event => {
    if (event.target === event.currentTarget) event.currentTarget.close();
  });
  loadData();
})();
