// Stable selectors — map to data-testid attributes in the app
export const S = {
  // ── Navigation (Header) ────────────────────────────────────────────────
  TAB_UPLOAD:   '[data-testid="tab-upload"]',
  TAB_EXPLORER: '[data-testid="tab-explorer"]',
  TAB_CHAT:     '[data-testid="tab-chat"]',
  BTN_LOGIN:    '[data-testid="btn-login"]',
  BTN_LOGOUT:   '[data-testid="btn-logout"]',
  APP_MAIN:     '[data-testid="app-main"]',

  // ── Chat (ChatView) ────────────────────────────────────────────────────
  CHAT_CONTAINER:         '[data-testid="chat-container"]',
  CHAT_MESSAGES:          '[data-testid="chat-messages"]',
  CHAT_MESSAGE_USER:      '[data-testid="chat-message-user"]',
  CHAT_MESSAGE_ASSISTANT: '[data-testid="chat-message-assistant"]',
  CHAT_INPUT:             '[data-testid="chat-input"]',
  CHAT_SEND:              '[data-testid="chat-send"]',
  CHAT_CLEAR:             '[data-testid="chat-clear"]',
  CHAT_THINKING:          '[data-testid="chat-thinking"]',   // "Moby is thinking…" indicator

  // ── Chat — ActionableTable action buttons ─────────────────────────────
  CHAT_BTN_EXPORT_CSV:     '[data-testid="chat-btn-export-csv"]',
  CHAT_BTN_HIGHLIGHT:      '[data-testid="chat-btn-highlight"]',
  CHAT_BTN_OPEN_EXPLORER:  '[data-testid="chat-btn-open-explorer"]',
  CHAT_BTN_ADD_COLUMNS:    '[data-testid="chat-btn-add-columns"]',

  // ── AIResultTable (used inside Chat responses) ─────────────────────────
  AI_RESULT_TABLE: '[data-testid="ai-result-table"]',
  AI_RESULT_ROW:   '[data-testid="ai-result-row"]',
  AI_RESULT_CELL:  '[data-testid="ai-result-cell"]',

  // ── Explorer (ExplorerView) ────────────────────────────────────────────
  EXPLORER_FILTER_PANEL:    '[data-testid="explorer-filter-panel"]',
  EXPLORER_SEARCH_BTN:      '[data-testid="explorer-search-btn"]',
  EXPLORER_RESULTS_COUNT:   '[data-testid="explorer-results-count"]',
  EXPLORER_GLOBAL_SEARCH:   '[data-testid="explorer-global-search"]',
  EXPLORER_PAGE_SIZE:       '[data-testid="explorer-page-size"]',
  FILTER_BUILDER:           '[data-testid="filter-builder"]',

  // Explorer results table (TanStack React Table)
  EXPLORER_RESULTS_TABLE:   '[data-testid="explorer-results-table"]',
  EXPLORER_TABLE_HEADER:    '[data-testid="explorer-table-header"]',
  EXPLORER_TABLE_ROW:       '[data-testid="explorer-table-row"]',
  EXPLORER_TABLE_CELL:      '[data-testid="explorer-table-cell"]',

  // Explorer pagination
  EXPLORER_PAGINATION:      '[data-testid="explorer-pagination"]',
  EXPLORER_PAGE_PREV:       '[data-testid="explorer-page-prev"]',
  EXPLORER_PAGE_NEXT:       '[data-testid="explorer-page-next"]',
  EXPLORER_PAGE_FIRST:      '[data-testid="explorer-page-first"]',
  EXPLORER_PAGE_LAST:       '[data-testid="explorer-page-last"]',
  EXPLORER_PAGE_INFO:       '[data-testid="explorer-page-info"]',

  // Explorer toolbar buttons
  EXPLORER_BTN_EXPORT_TSV:  '[data-testid="explorer-btn-export-tsv"]',
  EXPLORER_BTN_CHART:       '[data-testid="explorer-btn-chart"]',

  // Explorer filter chips
  EXPLORER_FILTER_CHIP:     '[data-testid="explorer-filter-chip"]',

  // ── Site Details Modal ─────────────────────────────────────────────────
  SITE_DETAILS_MODAL:       '[data-testid="site-details-modal"]',
  SITE_DETAILS_CLOSE:       '[data-testid="site-details-close"]',
  SITE_DETAILS_TITLE:       '[data-testid="site-details-title"]',
  SITE_DETAILS_LOADING:     '[data-testid="site-details-loading"]',
  SITE_DETAILS_ERROR:       '[data-testid="site-details-error"]',

  // ── Qualification section ──────────────────────────────────────────────
  QUAL_SECTION:             '[data-testid="qual-section"]',
  QUAL_VISIT_ROW:           '[data-testid="qual-visit-row"]',

  // ── Map ───────────────────────────────────────────────────────────────
  MAP_CONTAINER: '[data-testid="map-container"]',
} as const;
