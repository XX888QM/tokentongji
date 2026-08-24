# Token Observatory HUD — Design QA

## Comparison target

- Source visual truth: `/Users/yunxin/.codex/generated_images/019fa310-6545-72e0-b00b-6222b89ea707/exec-729e9228-7810-4021-b469-50b0de441765.png`
- Browser-rendered implementation: `design-qa/observatory-implementation.png`
- Normalized source focus: `design-qa/observatory-source-focus.png`
- Side-by-side comparison: `design-qa/observatory-comparison.png` (source on the left, implementation on the right)
- State: desktop normal/online state, live local API data loaded; only sources with non-zero current consumption are shown.

## Capture normalization

- Source pixels: `1487 × 1058`.
- Browser content viewport: `1280 × 720` CSS px, reported device scale factor `2`.
- Implementation pixels: `1265 × 712`.
- Normalization: the source's primary header-and-observatory region was cropped to the same `16:9` composition, then scaled to `1265 × 712`. This matches the in-app browser's available desktop capture area without stretching the implementation.

## Evidence and interaction checks

- The local page at `http://127.0.0.1:8787/` rendered the generated local HUD asset at its native `1536px` width.
- The live page rendered three current non-zero source rows, with total, fee, label and percentage read from the API. Empty sources were intentionally omitted, matching the product rule to show only actual consumption.
- `告警设置` opened its modal, and `Escape` closed it again.
- Browser console errors and warnings: none.
- Full-view comparison used `design-qa/observatory-comparison.png`. The observatory, top signal band, center readout and source rails are all visible at the normalized desktop size.
- Dynamic evidence: the center orbit's computed transform changed during a 1.1-second capture while its animation name remained `observatoryOrbit`; static source labels and the numeric readout stay separate from the rotating image. Source signal rails pulse using each source's real display color, and the total count interpolates only when the real API total changes.

## Fidelity review

### Fonts and typography

The implementation retains the product's system/PingFang stack for legibility, with a spaced technical label, large tabular total, concise status text and clear source/value hierarchy. The source's decorative display type is approximated by the existing supported system fonts rather than introducing an external font dependency.

### Spacing and layout rhythm

The comparison keeps the same desktop-wide composition: a thin technical header, centered observatory, left/right source annotations and a dense data region below. The implementation keeps the dashboard's existing summary cards below the hero instead of replacing operational content with the source mock's single table; this is an intentional product constraint so current dashboard functions remain available.

### Colors and visual tokens

The source's black, blue and cyan instrument treatment is carried through the local HUD asset, frame lines, readout glow and controls. Actual source color is still driven from `SOURCE_META`, so a source's color stays consistent with its table, trend and session views.

### Image quality and asset fidelity

The center observatory is the generated raster asset `src/tokenstat/static/assets/observatory-hud.png`, rendered at native resolution and cropped centrally without stretching. A second clipped copy animates only the central ring, so no rectangular image edge rotates across the screen. It replaces no data: the live number and source data are HTML rendered over the asset.

### Copy and content

The title changed to `本地 Token 观测台`. `claude-mem` is displayed only by its own name. No `Codex 额度` or `Codex（直接）` label was reintroduced.

## Findings

- No actionable P0, P1 or P2 findings.
- [P3] The target concept displays six populated source annotations, while the current live data has three non-zero sources. The implementation deliberately omits zero-use sources instead of fabricating rows.
- [P3] The target concept includes date/period controls in the masthead; the existing dashboard retains its working period controls in the breakdown section to preserve its current interaction model.

## Comparison history

- Pass 1: full generated asset rotation revealed a visible rectangular edge when the image moved. This was a P2 fidelity issue because the HUD should read as a rotating instrument ring, not a turning card.
- Pass 2: the static full asset remains fixed; only a clipped central copy rotates. Source-name/value layout was also changed to a two-row value stack so narrow desktop capture columns do not overlap. The new source-focus/implementation comparison and browser capture show no rectangular moving edge, no source text overlap, no console errors, and no remaining P0/P1/P2 finding.

## Implementation checklist

- [x] Local generated HUD asset placed in the observatory center.
- [x] Real total, estimated fee, source labels, source totals and percentages remain API-driven.
- [x] Empty source rows are omitted.
- [x] Central ring rotates continuously, source rails pulse, and the live total animates on genuine refresh changes.
- [x] Existing navigation, audit actions, period controls, export and keyboard-close settings modal remain in place.
- [x] Static frontend and full test suite pass.

## Follow-up polish

- If more data sources become active, the source rails fill automatically; no static source row should be added solely for visual balance.

final result: passed
