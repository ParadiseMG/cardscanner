"""B7: Smoke tests for static asset presence of Round 2 frontend features."""
import re
import pytest
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture(scope="module")
def app_js_src():
    """Fetch app.js once for the whole module."""
    client = TestClient(app)
    r = client.get("/static/app.js")
    assert r.status_code == 200, f"Expected 200 from /static/app.js, got {r.status_code}"
    return r.text


@pytest.fixture(scope="module")
def index_html_src():
    """Fetch index.html once for the whole module."""
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200, f"Expected 200 from /, got {r.status_code}"
    return r.text


# ---------------------------------------------------------------------------
# B1: Virtual scroller
# ---------------------------------------------------------------------------

def test_virtualize_inventory_function_defined(app_js_src):
    """B1: virtualizeInventory function must be present in app.js."""
    assert "virtualizeInventory" in app_js_src, (
        "virtualizeInventory not found in app.js"
    )


def test_virt_padding_references(app_js_src):
    """B1: Virtual padding variables used for top/bottom spacers."""
    assert "virtPaddingTop" in app_js_src
    assert "virtPaddingBottom" in app_js_src


def test_row_height_constant(app_js_src):
    """B1: ROW_HEIGHT constant drives the virtual window calculation."""
    assert "ROW_HEIGHT" in app_js_src


def test_inv_scroll_container_in_html(index_html_src):
    """B1: The virtual scroller container element must exist."""
    assert "inv-scroll" in index_html_src


# ---------------------------------------------------------------------------
# B2: Server-driven filter / sort / search
# ---------------------------------------------------------------------------

def test_apply_filters_function(app_js_src):
    """B2: applyFilters() orchestrates server-side re-fetch."""
    assert "applyFilters" in app_js_src


def test_facets_endpoint_reference(app_js_src):
    """B2: Code must call /api/inventory/facets."""
    assert "/api/inventory/facets" in app_js_src


def test_sort_by_parameter_sent(app_js_src):
    """B2: sortBy param is included when building inventory query."""
    assert "sortBy" in app_js_src
    assert "sort" in app_js_src


def test_era_filter_toggle(app_js_src):
    """B2: toggleEraFilter drives multi-valued era filter."""
    assert "toggleEraFilter" in app_js_src


def test_clear_all_filters(app_js_src):
    """B2: clearAllFilters resets everything."""
    assert "clearAllFilters" in app_js_src


def test_search_debounced_in_html(index_html_src):
    """B2: Search input must use Alpine debounce."""
    assert "debounce" in index_html_src


def test_filter_sidebar_present(index_html_src):
    """B2: Sidebar with sort select must exist."""
    assert "sortBy" in index_html_src
    assert "applyFilters" in index_html_src


# ---------------------------------------------------------------------------
# B3: Bulk actions
# ---------------------------------------------------------------------------

def test_bulk_ids_endpoint(app_js_src):
    """B3: selectAllMatching must call /api/bulk/ids."""
    assert "/api/bulk/ids" in app_js_src


def test_bulk_patch_endpoint(app_js_src):
    """B3: bulkPatchStatus/Channel must call /api/bulk/patch."""
    assert "/api/bulk/patch" in app_js_src


def test_bulk_delete_endpoint(app_js_src):
    """B3: bulkDelete must call /api/bulk/delete."""
    assert "/api/bulk/delete" in app_js_src


def test_bulk_recompute_comps_endpoint(app_js_src):
    """B3: bulkRecomputeComps must call /api/bulk/recompute-comps."""
    assert "/api/bulk/recompute-comps" in app_js_src


def test_bulk_move_to_bulk_lot_endpoint(app_js_src):
    """B3: bulkMoveToBulkLot must call /api/bulk/move-to-bulk-lot."""
    assert "/api/bulk/move-to-bulk-lot" in app_js_src


def test_select_all_matching_function(app_js_src):
    """B3: selectAllMatching() fetches all IDs matching current filter."""
    assert "selectAllMatching" in app_js_src


def test_bulk_toolbar_in_html(index_html_src):
    """B3: Bulk action toolbar rendered in HTML."""
    assert "selectAllMatching" in index_html_src
    assert "bulkDelete" in index_html_src


def test_confirm_before_delete(app_js_src):
    """B3: confirm() dialog must gate the delete action."""
    # Find the bulkDelete function and check it calls confirm
    assert re.search(r"async bulkDelete\b.*?confirm\(", app_js_src, re.DOTALL), (
        "bulkDelete should call confirm() before deleting"
    )


def test_confirm_before_move_to_bulk_lot(app_js_src):
    """B3: confirm() dialog must gate move-to-bulk-lot."""
    assert re.search(r"async bulkMoveToBulkLot\b.*?confirm\(", app_js_src, re.DOTALL), (
        "bulkMoveToBulkLot should call confirm() before moving"
    )


# ---------------------------------------------------------------------------
# B4: Keyboard navigation
# ---------------------------------------------------------------------------

def test_keyboard_handler_defined(app_js_src):
    """B4: keyboardHandler must be defined (per spec)."""
    assert "keyboardHandler" in app_js_src


def test_keyboard_j_k_navigation(app_js_src):
    """B4: j and k move focus in inventory."""
    # Both keys must be handled
    assert "case 'j'" in app_js_src
    assert "case 'k'" in app_js_src


def test_keyboard_x_toggle_select(app_js_src):
    """B4: x toggles selection on focused row."""
    assert "case 'x'" in app_js_src
    assert "toggleSelectRow" in app_js_src


def test_keyboard_e_opens_edit(app_js_src):
    """B4: e opens edit modal."""
    assert "case 'e'" in app_js_src
    assert "openCard" in app_js_src


def test_keyboard_l_opens_listing(app_js_src):
    """B4: l opens eBay listing preview."""
    assert "case 'l'" in app_js_src
    assert "previewListing" in app_js_src


def test_keyboard_slash_focuses_search(app_js_src):
    """B4: / focuses the search input."""
    assert "inv-search" in app_js_src
    assert "key === '/'" in app_js_src


def test_keyboard_g_h_jump_top(app_js_src):
    """B4: g then h jumps to top of list."""
    assert "_ghPending" in app_js_src
    assert "case 'h'" in app_js_src


def test_keyboard_question_mark_help(app_js_src):
    """B4: ? toggles keyboard help overlay."""
    assert "showKeyboardHelp" in app_js_src


def test_keyboard_escape_closes(app_js_src):
    """B4: Escape closes modals/clears focus."""
    assert "key === 'Escape'" in app_js_src


def test_keyboard_skips_input_elements(app_js_src):
    """B4: Shortcuts must not fire while typing in inputs."""
    assert "isEditable" in app_js_src


def test_keyboard_help_overlay_in_html(index_html_src):
    """B4: Keyboard help overlay must be in the HTML."""
    assert "showKeyboardHelp" in index_html_src
    assert "Keyboard shortcuts" in index_html_src


def test_keyboard_hint_chip_in_html(index_html_src):
    """B4: First-visit hint chip must be in HTML."""
    assert "showKeyboardHint" in index_html_src


# ---------------------------------------------------------------------------
# B5: Activity stripe
# ---------------------------------------------------------------------------

def test_load_stripe_function(app_js_src):
    """B5: loadStripe() computes last-24h stats."""
    assert "loadStripe" in app_js_src


def test_stripe_text_function(app_js_src):
    """B5: stripeText() builds the human-readable summary."""
    assert "stripeText" in app_js_src


def test_stripe_in_html(index_html_src):
    """B5: Activity stripe element must exist."""
    assert "stripeText" in index_html_src


def test_stripe_progress_in_html(index_html_src):
    """B5: Stripe doubles as progress bar during job."""
    assert "jobProgressPct" in index_html_src


def test_stripe_filter_today(app_js_src):
    """B5: filterToday() filters inventory to today's cards."""
    assert "filterToday" in app_js_src


def test_view_new_cards_link_in_html(index_html_src):
    """B5: 'View N new cards' link appears after job completes."""
    assert "new cards" in index_html_src
    assert "stripe.newCardIds" in index_html_src


# ---------------------------------------------------------------------------
# B6: Performance polish
# ---------------------------------------------------------------------------

def test_lazy_loading_images(index_html_src):
    """B6: Images must use loading='lazy'."""
    assert 'loading="lazy"' in index_html_src


def test_chart_memoization(app_js_src):
    """B6: Charts only redraw when hash changes."""
    assert "_lastChartHash" in app_js_src
    assert "_jsonHash" in app_js_src


def test_idle_callback_facets(app_js_src):
    """B6: Facet refresh uses requestIdleCallback."""
    assert "requestIdleCallback" in app_js_src


# ---------------------------------------------------------------------------
# General: API contract references
# ---------------------------------------------------------------------------

def test_bulk_api_prefix_present(app_js_src):
    """General: /api/bulk/ endpoints are referenced."""
    assert "/api/bulk/" in app_js_src


def test_facets_loaded_on_inventory_tab(app_js_src):
    """B2: loadFacets is called when entering inventory tab."""
    assert "loadFacets" in app_js_src


def test_static_app_js_served(app_js_src):
    """Baseline: app.js is served as text."""
    assert len(app_js_src) > 1000, "app.js appears empty or truncated"


def test_index_html_served(index_html_src):
    """Baseline: index.html is served."""
    assert "cardscanner" in index_html_src
    assert "Alpine" in index_html_src or "alpinejs" in index_html_src


# ---------------------------------------------------------------------------
# Round 3 — Intelligence (Suggestions tab, confidence chips, low-comp queue)
# ---------------------------------------------------------------------------

def test_suggestions_tab_in_nav(index_html_src):
    """R3-C1: Suggestions tab is in the tab nav array."""
    assert "'Suggestions'" in index_html_src


def test_suggestions_section_present(index_html_src):
    """R3-C1: The Suggestions tab body section exists."""
    assert "tab==='Suggestions'" in index_html_src


def test_load_suggestions_function(app_js_src):
    """R3-C1: loadSuggestions fetches /api/suggestions."""
    assert "loadSuggestions" in app_js_src
    assert "/api/suggestions" in app_js_src


def test_suggestions_grouped_function(app_js_src):
    """R3-C1: suggestionsGrouped groups items by kind for the UI."""
    assert "suggestionsGrouped" in app_js_src
    for kind in ("grade", "bulk", "list", "reshoot", "verify_comps"):
        assert kind in app_js_src, f"missing kind handling: {kind}"


def test_suggestion_action_dispatcher(app_js_src):
    """R3-C1: suggestionAction dispatches per kind."""
    assert "suggestionAction" in app_js_src


def test_confidence_dot_color_helper(app_js_src):
    """R3-C2: confidenceDotColor renders the per-row comp-confidence dot."""
    assert "confidenceDotColor" in app_js_src


def test_action_queue_low_comp_row(index_html_src):
    """R3-C4: Action Queue panel includes the low_comp_confidence row."""
    assert "low_comp_confidence" in index_html_src
    assert "filterLowCompConfidence" in index_html_src


def test_filter_low_comp_confidence_helper(app_js_src):
    """R3-C4: filterLowCompConfidence shows the matching cards in inventory."""
    assert "filterLowCompConfidence" in app_js_src


def test_reidentify_wired(app_js_src):
    """R3-C3: 'Re-identify with Claude' button calls /api/inventory/{id}/reidentify."""
    assert "reidentify" in app_js_src
    assert "/reidentify" in app_js_src


# ---------------------------------------------------------------------------
# Round 4 Workstream C — Money tab, grading flow, bulk-lot UI
# ---------------------------------------------------------------------------

def test_money_tab_in_nav(index_html_src):
    """C1: 'Money' is in the tab nav array."""
    assert "'Money'" in index_html_src


def test_money_section_present(index_html_src):
    """C1: Money tab body section exists in HTML."""
    assert "tab==='Money'" in index_html_src


def test_bulk_lot_proposals_heading(index_html_src):
    """C3: 'Bulk lot proposals' heading is visible in HTML."""
    assert "Bulk lot proposals" in index_html_src


def test_load_sales_summary_function(app_js_src):
    """C1: loadSalesSummary function is defined in app.js."""
    assert "loadSalesSummary" in app_js_src


def test_sales_api_reference(app_js_src):
    """C1: /api/sales/ endpoint family is referenced in app.js."""
    assert "/api/sales/" in app_js_src


def test_grading_api_reference(app_js_src):
    """C2: /api/grading/ endpoint family is referenced in app.js."""
    assert "/api/grading/" in app_js_src


def test_bulk_lots_api_reference(app_js_src):
    """C3: /api/bulk-lots/ endpoint family is referenced in app.js."""
    assert "/api/bulk-lots/" in app_js_src


def test_stats_last_24h_api_reference(app_js_src):
    """C5: /api/stats/last_24h endpoint is referenced in app.js."""
    assert "/api/stats/last_24h" in app_js_src


def test_reidentify_endpoint_referenced(app_js_src):
    """C4: /reidentify endpoint path is in app.js."""
    assert "/reidentify" in app_js_src


def test_grading_queue_state(app_js_src):
    """C2: gradingQueue state variable is defined in app.js."""
    assert "gradingQueue" in app_js_src


def test_load_bulk_lot_proposals_function(app_js_src):
    """C3: loadBulkLotProposals function is defined in app.js."""
    assert "loadBulkLotProposals" in app_js_src


def test_grading_queue_section_in_html(index_html_src):
    """C2: Grading Queues panel is present in HTML."""
    assert "Grading" in index_html_src
    assert "gradingQueue" in index_html_src


def test_sales_charts_in_html(index_html_src):
    """C1: Sales chart canvas elements are present in HTML."""
    assert "salesByMonthChart" in index_html_src
    assert "salesChannelChart" in index_html_src


def test_sync_ebay_sales_button_in_html(index_html_src):
    """C1: Sync eBay sales button is present in Money tab HTML."""
    assert "syncEbaySales" in index_html_src


def test_manual_sale_modal_in_html(index_html_src):
    """C1: Manual sale modal markup is present in HTML."""
    assert "showManualSaleModal" in index_html_src
    assert "manualSale" in index_html_src
