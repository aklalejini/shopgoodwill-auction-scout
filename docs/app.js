(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = { listings: [], status: null, evaluations: {} };
  const controls = {
    keyword: $("#keyword"), sort: $("#sort"), minPrice: $("#min-price"),
    maxPrice: $("#max-price"), maxBuyNow: $("#max-buy-now"),
    endingHours: $("#ending-hours"), seller: $("#seller"),
    category: $("#hunt-category"), source: $("#source"), targetKeyword: $("#target-keyword"),
    multiplePhotos: $("#multiple-photos"), noBids: $("#no-bids"),
    buyNowOnly: $("#buy-now-only"),
    highPotential: $("#high-potential"), hideEvaluated: $("#hide-evaluated")
  };
  const ending24 = $("#ending-24");

  const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
  const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
  const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

  try {
    state.evaluations = JSON.parse(localStorage.getItem("auctionScoutEvaluations") || "{}") || {};
  } catch (_) {
    state.evaluations = {};
  }

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
      ...(item.hunt_labels || []), item.source_label, item.location].join(" ").toLocaleLowerCase();
  }

  function potentialLabel(item) {
    if (item.high_priority || item.score_high_priority || item.potentially_high_value) return "High potential";
    const score = Number(item.score || 0);
    if (score >= 30) return "Promising";
    if (score >= 20) return "Worth review";
    return "Possible lead";
  }

  function deliveryLabel(item) {
    const shipping = item.shipping || {};
    if (shipping.pickup_only) return "Local pickup";
    if (Number(shipping.listed_price || 0) > 0) return `${currency.format(Number(shipping.listed_price))} shipping`;
    return shipping.carrier || "See listing";
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
    const source = controls.source.value;
    const minimum = controls.minPrice.value === "" ? null : Number(controls.minPrice.value);
    const maximum = controls.maxPrice.value === "" ? null : Number(controls.maxPrice.value);
    const maximumBuyNow = controls.maxBuyNow.value === "" ? null : Number(controls.maxBuyNow.value);
    const endingHours = controls.endingHours.value === "" ? null : Number(controls.endingHours.value);
    const endCutoff = endingHours === null ? null : Date.now() + endingHours * 3_600_000;

    const filtered = state.listings.filter(item => {
      const price = Number(item.price || 0);
      const buyNowPrice = Number(item.buy_now_price || 0);
      const end = new Date(item.end_time).getTime();
      if (query && !searchableText(item).includes(query)) return false;
      if (seller && !String(item.seller || "").toLocaleLowerCase().includes(seller)) return false;
      if (targetKeyword && !searchableText(item).includes(targetKeyword)) return false;
      if (category && !(item.hunt_categories || []).includes(category)) return false;
      if (source && String(item.source || "shopgoodwill") !== source) return false;
      if (minimum !== null && price < minimum) return false;
      if (maximum !== null && price > maximum) return false;
      if (maximumBuyNow !== null && (!item.has_buy_now || buyNowPrice > maximumBuyNow)) return false;
      if (endCutoff !== null && (!Number.isFinite(end) || end < Date.now() || end > endCutoff)) return false;
      if (controls.multiplePhotos.checked && (item.images || []).length < 2) return false;
      if (controls.noBids.checked && Number(item.bids || 0) !== 0) return false;
      if (controls.buyNowOnly.checked && !item.has_buy_now) return false;
      if (controls.highPotential.checked && !(item.high_priority || item.score_high_priority || item.potentially_high_value)) return false;
      if (controls.hideEvaluated.checked && state.evaluations[item.item_id]) return false;
      return true;
    });

    const sorters = {
      score: (a, b) => Number(b.score || 0) - Number(a.score || 0),
      newest: (a, b) => new Date(b.start_time) - new Date(a.start_time),
      ending: (a, b) => new Date(a.end_time) - new Date(b.end_time),
      "price-low": (a, b) => Number(a.price || 0) - Number(b.price || 0),
      "price-high": (a, b) => Number(b.price || 0) - Number(a.price || 0),
      "buy-now": (a, b) => {
        const aPrice = a.has_buy_now ? Number(a.buy_now_price || 0) : Number.POSITIVE_INFINITY;
        const bPrice = b.has_buy_now ? Number(b.buy_now_price || 0) : Number.POSITIVE_INFINITY;
        return aPrice - bPrice;
      },
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
    $(".score-inline strong", fragment).textContent = item.score ?? 0;
    const potential = $(".potential-label", fragment);
    potential.textContent = potentialLabel(item);
    potential.classList.toggle("high", Boolean(item.high_priority || item.score_high_priority || item.potentially_high_value));
    $(".ending-badge", fragment).textContent = timeRemaining(item.end_time);
    const buyNowPrice = Number(item.buy_now_price || 0);
    $(".price-label", fragment).textContent = item.has_buy_now && Number(item.bids || 0) === 0 ? "Listed price" : "Current bid";
    $(".price", fragment).textContent = currency.format(Number(item.price || 0));
    if (item.has_buy_now) {
      const buyNowRow = $(".buy-now-row", fragment);
      buyNowRow.hidden = false;
      $(".buy-now-price", fragment).textContent = currency.format(buyNowPrice);
    }
    $(".delivery", fragment).textContent = deliveryLabel(item);
    $(".photo-count", fragment).textContent = `${thumbnails.length || (item.images || []).length} available`;
    $(".bids", fragment).textContent = `${Number(item.bids || 0)} bid${Number(item.bids || 0) === 1 ? "" : "s"}`;
    $("h3", fragment).textContent = item.title;
    $(".seller-name", fragment).textContent = item.seller || item.location || "Seller not yet loaded";
    $(".item-id", fragment).textContent = `${item.source_label || "ShopGoodwill"} #${item.source_native_id || item.item_id}`;

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
    const clusterTag = item.seller_cluster ? `${item.seller_cluster.count} same-seller closings` : null;
    const tags = [item.source_label || "ShopGoodwill", item.primary_hunt?.label, clusterTag, ...(item.discovered_by || [])].filter(Boolean);
    tags.slice(0, 3).forEach(term => {
      const tag = document.createElement("span");
      tag.textContent = term;
      termRow.append(tag);
    });

    const reasonList = $(".card-reasons ul", fragment);
    (item.score_reasons || []).slice(0, 5).forEach(reason => {
      const entry = document.createElement("li");
      entry.textContent = reason;
      reasonList.append(entry);
    });

    function setEvaluation(status) {
      if (state.evaluations[item.item_id] === status) delete state.evaluations[item.item_id];
      else state.evaluations[item.item_id] = status;
      localStorage.setItem("auctionScoutEvaluations", JSON.stringify(state.evaluations));
      render();
    }
    $(".watch-button", fragment).addEventListener("click", () => setEvaluation("watch"));
    $(".reject-button", fragment).addEventListener("click", () => setEvaluation("rejected"));
    const evaluation = state.evaluations[item.item_id];
    card.dataset.evaluation = evaluation || "";
    const watchButton = $(".watch-button", fragment);
    const rejectButton = $(".reject-button", fragment);
    watchButton.classList.toggle("active", evaluation === "watch");
    rejectButton.classList.toggle("active", evaluation === "rejected");
    watchButton.textContent = evaluation === "watch" ? "Watching" : "Watch";
    rejectButton.textContent = evaluation === "rejected" ? "Rejected" : "Reject";
    watchButton.setAttribute("aria-pressed", String(evaluation === "watch"));
    rejectButton.setAttribute("aria-pressed", String(evaluation === "rejected"));

    $$(".image-button, .inspect-button", fragment).forEach(button => {
      button.addEventListener("click", () => openDetail(item));
    });
    card.dataset.itemId = item.item_id;
    return fragment;
  }

  function openDetail(item) {
    const dialog = $("#detail-dialog");
    const images = item.images || [];
    const buyNowPrice = Number(item.buy_now_price || 0);
    const buyNowMarkup = item.has_buy_now
      ? `<div class="detail-buy-now"><span>Buy It Now</span><strong>${currency.format(buyNowPrice)}</strong></div>`
      : "";
    const reasons = (item.score_reasons || []).map(reason => `<li>${escapeHtml(reason)}</li>`).join("");
    const cluster = item.seller_cluster;
    const clusterMarkup = cluster ? `<div class="cluster-note">${cluster.count} auctions from this seller close within ${cluster.close_window_hours}h.</div>` : "";
    const ocrMarkup = item.ocr_hits?.length ? `<div class="ocr-note"><strong>Image text:</strong> ${item.ocr_hits.map(escapeHtml).join(", ")}</div>` : "";
    const visualMarkup = item.visual_hits?.length ? `<div class="ocr-note"><strong>Image color clue:</strong> ${item.visual_hits.map(escapeHtml).join(", ")}</div>` : "";
    const imageMarkup = images.length
      ? images.map((source, index) => `<a href="${escapeHtml(source)}" target="_blank" rel="noopener"><img src="${escapeHtml(source)}" alt="${escapeHtml(item.title)} — photo ${index + 1}" loading="lazy"></a>`).join("")
      : `<div class="notice">No full-size images were returned for this listing.</div>`;
    $("#dialog-content").innerHTML = `
      <div class="detail-layout">
        <div class="detail-images">${imageMarkup}</div>
        <div class="detail-info">
          <p class="eyebrow">${escapeHtml(item.primary_hunt?.label || "Auction watch")} · Item #${escapeHtml(item.item_id)} · ${images.length} photo${images.length === 1 ? "" : "s"}</p>
          <h2 id="dialog-title">${escapeHtml(item.title)}</h2>
          <p class="detail-price-label">${item.has_buy_now && Number(item.bids || 0) === 0 ? "Listed price" : "Current bid"}</p>
          <p class="detail-price">${currency.format(Number(item.price || 0))}</p>
          ${buyNowMarkup}
          <p class="detail-sub">${Number(item.bids || 0)} bid${Number(item.bids || 0) === 1 ? "" : "s"} · ${escapeHtml(timeRemaining(item.end_time))}</p>
          <div class="detail-potential">
            <span>${escapeHtml(potentialLabel(item))}</span>
            <strong>${Number(item.score || 0)}/100</strong>
          </div>
          ${clusterMarkup}${ocrMarkup}${visualMarkup}
          <div class="detail-facts">
            <div><span>Seller</span><strong>${escapeHtml(item.seller || "Not yet loaded")}</strong></div>
            <div><span>Ends</span><strong>${item.end_time ? escapeHtml(dateTime.format(new Date(item.end_time))) : "Unknown"}</strong></div>
            <div><span>Category</span><strong>${escapeHtml(item.category || "Unknown")}</strong></div>
            <div><span>Source</span><strong>${escapeHtml(item.source_label || "ShopGoodwill")}</strong></div>
            <div><span>Location</span><strong>${escapeHtml(item.location || "See listing")}</strong></div>
            <div><span>Delivery</span><strong>${escapeHtml(deliveryLabel(item))}</strong></div>
            <div><span>Photos</span><strong>${images.length}</strong></div>
          </div>
          <div class="score-panel">
            <h3>Why it stands out</h3>
            <ul>${reasons}</ul>
          </div>
          <p class="detail-description">${escapeHtml(item.description || "The full description has not been loaded yet.")}</p>
          <a class="visit-listing" href="${escapeHtml(item.listing_url)}" target="_blank" rel="noopener">Open on ${escapeHtml(item.source_label || "ShopGoodwill")} ↗</a>
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
    ending24.checked = false;
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

  function populateSources() {
    const sources = new Map([["shopgoodwill", "ShopGoodwill"]]);
    (state.status?.sources || []).forEach(source => sources.set(source.id, source.label));
    state.listings.forEach(item => sources.set(item.source || "shopgoodwill", item.source_label || "ShopGoodwill"));
    controls.source.replaceChildren(new Option("All sources", ""));
    [...sources.entries()].sort((a, b) => a[1].localeCompare(b[1]))
      .forEach(([id, label]) => controls.source.add(new Option(label, id)));
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
      populateSources();
      $("#active-count").textContent = state.listings.length.toLocaleString();
      const priorityCount = state.status?.high_priority_count ?? state.listings.filter(item => item.high_priority).length;
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
      $("#active-count").textContent = "error";
      $("#priority-count").textContent = "error";
      $("#refresh-age").textContent = "unavailable";
      $("#result-summary").textContent = "The feed could not be loaded";
      const notice = $("#notice");
      notice.hidden = false;
      notice.textContent = `Start a local web server instead of opening index.html directly, then reload. ${error.message}`;
      $("#gallery").hidden = true;
    }
  }

  Object.values(controls).forEach(control => control.addEventListener("input", render));
  ending24.addEventListener("input", () => {
    controls.endingHours.value = ending24.checked ? "24" : "";
    render();
  });
  controls.endingHours.addEventListener("input", () => {
    ending24.checked = controls.endingHours.value === "24";
  });
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
