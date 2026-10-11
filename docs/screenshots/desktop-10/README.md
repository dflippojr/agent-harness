# Desktop 10: keyboard

Before/after screenshots at 1440×900 and 1024×900, in explicit light and dark themes.

Headless Edge rendered the real `harness/web` bundle against a mocked API with synthetic sessions and jobs; no daemon data is used. Before is the Desktop 9 base commit b92befd, and after is this branch. Each capture loads `#/agents`, presses `J` twice, then presses `?`.

- `*-rows-*.png`: after `J J`, the second row has the 2 px accent focus ring. The Agents search shows its `/` key cap. At 1440 the empty pane lists the main shortcuts. Before, the keys do nothing and no ring is drawn.
- `*-shortcuts-*.png`: the `?` sheet with its two columns, starting on Close. Before, `?` does nothing.

Browser checks at 1440, 1024 and 390 px:

- `/` focuses search, and typing `n` there types an "n".
- `J`/`K` move through rows, and `Enter` opens one.
- `G` then `J`/`S`/`I`/`A` switch section, and `N` opens New task or New job.
- The `?` sheet starts on Close. `?` again or `Esc` closes it, and `J` does nothing while it is open.
- `Ctrl+Enter` sends from the session composer.
- `J` does nothing while the ⋯ menu is open, and `Esc` returns focus to ⋯.
- Tab order from 768 px runs rail → list pane → header bar → page → composer. The phone order is unchanged.
