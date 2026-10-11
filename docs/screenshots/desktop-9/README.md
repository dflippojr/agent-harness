# Desktop 9: sheets and output modal

Before/after screenshots at 1440×900 and 1024×900, in explicit light and dark themes.

These are isolated synthetic session fixtures rendered by headless Edge with the real stylesheet, DOM builder, sheet, tool-row and session-menu modules; they use no daemon data. Before uses the Desktop 5 base commit c5e6f81. After uses this branch. Each pair has the same transcript and output.

- `*-confirm-*.png`: destructive confirmation, with Keep running as the safe initial focus.
- `*-output-*.png`: the full tool output with Wrap, Copy and Close.
- `metrics.json`: measured dialog bounds, focus, backdrop colour and button sizes for all 16 captures.

Browser assertions verify centring, 44 px controls, natural-width sheet actions, safe focus, Escape dismissal and restored focus at both desktop widths in both themes. Session menu ArrowDown/Escape also pass. At 390 px the sheet keeps its handle and 50 px buttons. The existing 700–767 px centred sheet keeps its 50 px buttons; output stays full-screen below 768 px. At 768 px the new desktop rules apply. Browsers and the ephemeral localhost server are closed after capture.
